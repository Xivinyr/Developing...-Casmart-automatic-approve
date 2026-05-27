import os
import sys
import time
import tkinter as tk
from tkinter import messagebox

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.edge.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# --- 配置区域 ---
# Edge 浏览器程序路径。这个是 msedge.exe，不是驱动。
EDGE_BINARY_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# Edge WebDriver 路径。这里必须是 msedgedriver.exe；留空则让 Selenium 自动查找。
EDGE_DRIVER_PATH = ""

# Casmart 采购管理 -> 线下采购单据列表页。
TARGET_URL = (
    "https://member.casmart.com.cn/supplier/offline/bill/list"
    "?client-key=eadd9901df4bdaed0b2b1d95e0c77534"
)

# 如果需要保持登录态，可以填 Edge 用户数据目录，例如：
# r"C:\Users\你的用户名\AppData\Local\Microsoft\Edge\User Data"
EDGE_USER_DATA_DIR = ""

WAIT_TIME = 15
LOGIN_VERIFY_WAIT_SECONDS = 10
AFTER_SUBMIT_CLICK_SECONDS = 0.3
AFTER_CONFIRM_CLICK_SECONDS = 3
MAX_PAGES = 0  # 0 表示不限制页数
SUBMIT_KEYWORDS = ("送审", "提交审核", "提交审批")
CONFIRM_KEYWORDS = ("确定", "确认", "提交", "送审")
CANCEL_KEYWORDS = ("取消", "关闭", "返回", "暂不")
# ------------------


def ask_credentials():
    """用 Tkinter 获取 Casmart 登录账号密码。"""
    result = {"username": "", "password": "", "cancelled": True}

    root = tk.Tk()
    root.title("Casmart 登录")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    width, height = 340, 170
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width - width) // 2
    y = (screen_height - height) // 2
    root.geometry(f"{width}x{height}+{x}+{y}")

    tk.Label(root, text="Casmart 账号：").grid(row=0, column=0, padx=18, pady=(22, 8), sticky="e")
    username_entry = tk.Entry(root, width=28)
    username_entry.grid(row=0, column=1, padx=(0, 18), pady=(22, 8))

    tk.Label(root, text="Casmart 密码：").grid(row=1, column=0, padx=18, pady=8, sticky="e")
    password_entry = tk.Entry(root, width=28, show="*")
    password_entry.grid(row=1, column=1, padx=(0, 18), pady=8)

    def paste_to_entry(entry):
        try:
            text = root.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        entry.insert(tk.INSERT, text)
        return "break"

    context_menu = tk.Menu(root, tearoff=0)
    active_entry = {"widget": username_entry}
    context_menu.add_command(label="粘贴", command=lambda: paste_to_entry(active_entry["widget"]))

    def show_context_menu(event):
        active_entry["widget"] = event.widget
        event.widget.focus_set()
        context_menu.tk_popup(event.x_root, event.y_root)

    for entry in (username_entry, password_entry):
        entry.bind("<Control-v>", lambda _event, e=entry: paste_to_entry(e))
        entry.bind("<Control-V>", lambda _event, e=entry: paste_to_entry(e))
        entry.bind("<Shift-Insert>", lambda _event, e=entry: paste_to_entry(e))
        entry.bind("<Button-3>", show_context_menu)

    def submit():
        username = username_entry.get().strip()
        password = password_entry.get()
        if not username or not password:
            messagebox.showwarning("提示", "请填写账号和密码。", parent=root)
            return
        result.update({"username": username, "password": password, "cancelled": False})
        root.destroy()

    def cancel():
        root.destroy()

    button_frame = tk.Frame(root)
    button_frame.grid(row=2, column=0, columnspan=2, pady=(12, 0))
    tk.Button(button_frame, text="登录并开始", width=12, command=submit).pack(side="left", padx=8)
    tk.Button(button_frame, text="取消", width=8, command=cancel).pack(side="left", padx=8)

    root.bind("<Return>", lambda _event: submit())
    root.protocol("WM_DELETE_WINDOW", cancel)
    username_entry.focus_set()
    root.mainloop()

    if result["cancelled"]:
        return None
    return result["username"], result["password"]


def init_driver():
    """初始化 Edge 浏览器。"""
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")

    if EDGE_BINARY_PATH and os.path.exists(EDGE_BINARY_PATH):
        options.binary_location = EDGE_BINARY_PATH

    if EDGE_USER_DATA_DIR:
        options.add_argument(f"--user-data-dir={EDGE_USER_DATA_DIR}")

    if EDGE_DRIVER_PATH and os.path.exists(EDGE_DRIVER_PATH):
        service = Service(EDGE_DRIVER_PATH)
        driver = webdriver.Edge(service=service, options=options)
    else:
        driver = webdriver.Edge(options=options)

    return driver


def wait_page_ready(driver, timeout=WAIT_TIME):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def wait_loading_finished(driver, timeout=WAIT_TIME):
    """等待 Element UI / 页面加载遮罩消失。"""
    try:
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located(
                (
                    By.CSS_SELECTOR,
                    ".el-loading-mask, .v-modal, .el-loading-spinner",
                )
            )
        )
    except TimeoutException:
        pass


def safe_click(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.4)

    click_errors = []
    for clicker in (
        lambda: element.click(),
        lambda: ActionChains(driver).move_to_element(element).pause(0.2).click().perform(),
        lambda: driver.execute_script(
            """
            const element = arguments[0];
            element.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
            element.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
            element.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
            element.click();
            """,
            element,
        ),
    ):
        try:
            clicker()
            time.sleep(0.5)
            return True
        except Exception as exc:
            click_errors.append(type(exc).__name__)

    print(f"点击失败，已尝试普通点击/鼠标点击/JS 点击：{', '.join(click_errors)}")
    return False


def find_visible_elements(driver, by, value):
    elements = driver.find_elements(by, value)
    return [element for element in elements if element.is_displayed()]


def compact_text(text):
    return "".join((text or "").split())


def element_text(element):
    text = element.text or ""
    if not text.strip():
        text = element.get_attribute("innerText") or ""
    if not text.strip():
        text = element.get_attribute("textContent") or ""
    if not text.strip():
        text = element.get_attribute("title") or ""
    return compact_text(text)


def clickable_ancestor(driver, element):
    return driver.execute_script(
        """
        const node = arguments[0];
        return node.closest(
            "button,a,[role='button'],.el-button,.ant-btn,.layui-btn,.login-btn,.layui-layer-btn0,.layui-layer-btn1,[onclick]"
        );
        """,
        element,
    )


def is_disabled(element):
    classes = element.get_attribute("class") or ""
    return bool(
        element.get_attribute("disabled")
        or element.get_attribute("aria-disabled") == "true"
        or "is-disabled" in classes
        or "disabled" in classes
    )


def visible_modal_containers(driver):
    modal_xpath = (
        "//*[contains(@class, 'layui-layer') or contains(@class, 'el-dialog') "
        "or contains(@class, 'el-message-box') or contains(@class, 'ant-modal') "
        "or contains(@class, 'ant-popover') or contains(@class, 'el-popover')]"
    )
    modals = find_visible_elements(driver, By.XPATH, modal_xpath)
    return [
        modal
        for modal in modals
        if "layui-layer-shade" not in (modal.get_attribute("class") or "")
    ]


def describe_element(element):
    try:
        tag_name = element.tag_name
        classes = element.get_attribute("class") or ""
        text = element_text(element)
        return f"<{tag_name} class='{classes}' text='{text}'>"
    except Exception:
        return "<unknown element>"


def find_submit_buttons_in_current_context(driver):
    xpath = (
        "//*[self::button or self::a or @role='button' or contains(@class, 'el-button') "
        "or contains(@class, 'ant-btn') or @onclick or self::span]"
        "[contains(normalize-space(.), '送审') "
        "or contains(normalize-space(.), '提交审核') "
        "or contains(normalize-space(.), '提交审批')]"
    )
    candidates = find_visible_elements(driver, By.XPATH, xpath)

    buttons = []
    seen = set()
    for candidate in candidates:
        text = element_text(candidate)
        if not any(keyword in text for keyword in SUBMIT_KEYWORDS):
            continue

        button = clickable_ancestor(driver, candidate)
        if not button or not button.is_displayed() or not button.is_enabled():
            continue

        button_text = element_text(button)
        if not any(keyword in button_text for keyword in SUBMIT_KEYWORDS):
            continue

        if is_disabled(button):
            continue

        element_id = button.id
        if element_id not in seen:
            seen.add(element_id)
            buttons.append(button)

    return buttons


def find_submit_buttons(driver):
    """查找当前列表中可点击的“送审”按钮，兼容 iframe 和文字按钮。"""
    driver.switch_to.default_content()

    buttons = find_submit_buttons_in_current_context(driver)
    if buttons:
        return buttons

    return find_submit_buttons_in_frames(driver)


def find_submit_buttons_in_frames(driver, depth=0, max_depth=3):
    if depth >= max_depth:
        return []

    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for index in range(len(frames)):
        try:
            driver.switch_to.frame(index)
            buttons = find_submit_buttons_in_current_context(driver)
            if buttons:
                print(f"在第 {depth + 1} 层 iframe 中找到送审按钮。")
                return buttons

            nested_buttons = find_submit_buttons_in_frames(driver, depth + 1, max_depth)
            if nested_buttons:
                return nested_buttons

            driver.switch_to.parent_frame()
        except Exception:
            driver.switch_to.default_content()

    driver.switch_to.default_content()
    return []


def print_clickable_debug(driver):
    """没找到送审时，打印页面上脚本看到的可点击文本，方便定位。"""
    driver.switch_to.default_content()
    texts = collect_clickable_texts(driver)

    if not texts:
        print("调试信息：当前页面没有扫描到可见按钮/链接文本，可能内容在未识别的 iframe 中。")
        return

    print("调试信息：当前页面可见按钮/链接文本如下：")
    for text in texts[:30]:
        print(f"  - {text}")


def collect_clickable_texts(driver, depth=0, max_depth=3):
    texts = []
    elements = driver.find_elements(
        By.XPATH,
        "//button | //a | //*[@role='button'] | "
        "//*[contains(@class, 'el-button')] | //*[contains(@class, 'ant-btn')]",
    )
    for element in elements:
        try:
            if element.is_displayed():
                text = element_text(element)
                if text and text not in texts:
                    texts.append(text)
        except StaleElementReferenceException:
            continue

    if depth >= max_depth:
        return texts

    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for index in range(len(frames)):
        try:
            driver.switch_to.frame(index)
            for text in collect_clickable_texts(driver, depth + 1, max_depth):
                if text not in texts:
                    texts.append(text)
            driver.switch_to.parent_frame()
        except Exception:
            driver.switch_to.default_content()

    return texts


def countdown(seconds):
    for remaining in range(seconds, 0, -1):
        print(f"\r等待登录/页面确认：还剩 {remaining:>2} 秒", end="", flush=True)
        time.sleep(1)
    print("\r等待登录/页面确认：倒计时结束，开始自动送审。      ")


def visible_inputs(driver):
    inputs = driver.find_elements(By.CSS_SELECTOR, "input")
    return [
        element
        for element in inputs
        if element.is_displayed()
        and element.is_enabled()
        and (element.get_attribute("type") or "text").lower() not in ("hidden", "submit", "button")
    ]


def find_login_fields(driver):
    inputs = visible_inputs(driver)
    password_inputs = [
        element
        for element in inputs
        if (element.get_attribute("type") or "").lower() == "password"
    ]
    if not password_inputs:
        return None, None

    password_input = password_inputs[0]
    password_index = inputs.index(password_input)

    username_candidates = [
        element
        for element in inputs[:password_index]
        if (element.get_attribute("type") or "text").lower() in ("text", "tel", "email", "")
    ]
    if not username_candidates:
        username_candidates = [
            element
            for element in inputs
            if element != password_input
            and (element.get_attribute("type") or "text").lower() in ("text", "tel", "email", "")
        ]

    username_input = username_candidates[-1] if username_candidates else None
    return username_input, password_input


def find_login_button(driver):
    xpath = (
        "//button[contains(normalize-space(.), '登录')]"
        " | //a[contains(normalize-space(.), '登录')]"
        " | //*[@role='button' and contains(normalize-space(.), '登录')]"
        " | //div[contains(normalize-space(.), '登录')]"
        " | //span[contains(normalize-space(.), '登录')]"
        " | //*[contains(@class, 'login') or contains(@class, 'Login')]"
        " | //*[contains(@class, 'layui-btn') and contains(normalize-space(.), '登录')]"
        " | //input[@type='submit' or @type='button']"
        "[contains(@value, '登录')]"
    )
    candidates = find_visible_elements(driver, By.XPATH, xpath)
    buttons = []
    seen = set()
    for candidate in candidates:
        text = element_text(candidate) or candidate.get_attribute("value") or ""
        classes = candidate.get_attribute("class") or ""
        if "登录" not in text and "login" not in classes.lower():
            continue

        button = clickable_ancestor(driver, candidate) or candidate
        if button.is_enabled() and not is_disabled(button):
            element_id = button.id
            if element_id not in seen:
                seen.add(element_id)
                buttons.append(button)

    def priority(element):
        tag_name = element.tag_name.lower()
        classes = element.get_attribute("class") or ""
        text = element_text(element) or element.get_attribute("value") or ""
        score = 0
        if text == "登录":
            score -= 30
        if "login" in classes.lower():
            score -= 20
        if tag_name in ("button", "a"):
            score -= 10
        return score

    buttons.sort(key=priority)
    return buttons[0] if buttons else None


def set_input_value(driver, element, value):
    driver.execute_script(
        """
        const element = arguments[0];
        const value = arguments[1];
        element.focus();
        element.value = "";
        element.dispatchEvent(new Event("input", {bubbles: true}));
        element.value = value;
        element.dispatchEvent(new Event("input", {bubbles: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
        """,
        element,
        value,
    )


def submit_login(driver, login_button, password_input):
    if login_button:
        print(f"正在点击登录按钮：{describe_element(login_button)}")
        if safe_click(driver, login_button):
            return True

    print("尝试在密码框按回车登录。")
    try:
        password_input.send_keys(Keys.ENTER)
        time.sleep(0.5)
        return True
    except Exception:
        pass

    print("尝试提交登录表单。")
    try:
        driver.execute_script(
            """
            const input = arguments[0];
            const form = input.closest("form");
            if (form) {
                form.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
                form.submit();
                return true;
            }
            return false;
            """,
            password_input,
        )
        time.sleep(0.5)
        return True
    except Exception:
        return False


def auto_login_if_needed(driver, username, password):
    """如果当前页面出现登录框，就自动输入账号密码并点击登录。"""
    print("正在检查是否需要登录...")
    end_time = time.time() + 12

    while time.time() < end_time:
        switch_to_latest_window(driver)
        username_input, password_input = find_login_fields(driver)
        if username_input and password_input:
            print("发现登录表单，正在自动填写账号密码...")
            set_input_value(driver, username_input, username)
            set_input_value(driver, password_input, password)
            time.sleep(0.3)

            login_button = find_login_button(driver)
            if not submit_login(driver, login_button, password_input):
                print("自动点击登录失败，请手动点击登录按钮。")

            print(f"请在浏览器中完成人机验证，脚本将在 {LOGIN_VERIFY_WAIT_SECONDS} 秒后继续...")
            countdown(LOGIN_VERIFY_WAIT_SECONDS)
            wait_page_ready(driver)
            wait_loading_finished(driver)
            return True

        time.sleep(0.5)

    print("未发现登录表单，可能已经处于登录状态。")
    return False


def choose_approval_flow_if_needed(driver):
    """如果出现“审批流类型选择”弹窗，默认选择第一个可选项。"""
    radio_xpath = (
        "//div[contains(@class, 'el-dialog') and not(contains(@style, 'display: none'))]"
        "//label[contains(@class, 'el-radio') and not(contains(@class, 'is-disabled'))]"
    )
    radios = find_visible_elements(driver, By.XPATH, radio_xpath)
    if radios:
        selected = [
            radio for radio in radios if "is-checked" in (radio.get_attribute("class") or "")
        ]
        if not selected:
            safe_click(driver, radios[0])
            print("已选择第一个审批流类型。")


def find_confirm_buttons_in_current_context(driver, root=None):
    xpath = (
        "//button | //a | //*[@role='button'] | "
        "//*[contains(@class, 'el-button')] | //*[contains(@class, 'ant-btn')] | "
        "//*[contains(@class, 'layui-layer-btn')] | //span"
    )
    search_root = root or driver
    candidates = [
        element
        for element in search_root.find_elements(By.XPATH, xpath)
        if element.is_displayed()
    ]

    buttons = []
    seen = set()
    for candidate in candidates:
        text = element_text(candidate)
        if not text:
            continue
        if not any(keyword in text for keyword in CONFIRM_KEYWORDS):
            continue
        if any(keyword in text for keyword in CANCEL_KEYWORDS):
            continue

        button = clickable_ancestor(driver, candidate)
        if not button or not button.is_displayed() or not button.is_enabled():
            continue
        if is_disabled(button):
            continue
        if "layui-laypage" in (button.get_attribute("class") or ""):
            continue

        element_id = button.id
        if element_id not in seen:
            seen.add(element_id)
            buttons.append(button)

    def priority(element):
        classes = element.get_attribute("class") or ""
        text = element_text(element)
        score = 0
        if "layui-layer-btn0" in classes:
            score -= 40
        if "layui-layer-btn" in classes:
            score -= 30
        if "primary" in classes or "success" in classes:
            score -= 20
        if text in ("确定", "确认"):
            score -= 10
        if "送审" in text or "提交" in text:
            score -= 5
        return score

    return sorted(buttons, key=priority)


def find_confirm_button_in_frames(driver, depth=0, max_depth=3):
    if depth >= max_depth:
        return None

    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for index in range(len(frames)):
        try:
            driver.switch_to.frame(index)
            buttons = find_confirm_buttons_in_current_context(driver)
            if buttons:
                print(f"在第 {depth + 1} 层 iframe 中找到确认按钮。")
                return buttons[0]

            nested_button = find_confirm_button_in_frames(driver, depth + 1, max_depth)
            if nested_button:
                return nested_button

            driver.switch_to.parent_frame()
        except Exception:
            driver.switch_to.default_content()

    driver.switch_to.default_content()
    return None


def find_confirm_button(driver):
    modals = visible_modal_containers(driver)
    for modal in reversed(modals):
        buttons = find_confirm_buttons_in_current_context(driver, root=modal)
        if buttons:
            return buttons[0]

    driver.switch_to.default_content()
    modals = visible_modal_containers(driver)
    for modal in reversed(modals):
        buttons = find_confirm_buttons_in_current_context(driver, root=modal)
        if buttons:
            return buttons[0]

    return find_confirm_button_in_frames(driver)


def print_dialog_debug(driver):
    driver.switch_to.default_content()
    modal_xpath = (
        "//*[contains(@class, 'layui-layer') or contains(@class, 'el-dialog') "
        "or contains(@class, 'el-message-box') "
        "or contains(@class, 'ant-modal') or contains(@class, 'ant-popover') "
        "or contains(@class, 'el-popover')]"
    )
    modals = find_visible_elements(driver, By.XPATH, modal_xpath)
    if modals:
        print("调试信息：当前可见弹窗文本如下：")
        for modal in modals[-3:]:
            text = compact_text(modal.get_attribute("innerText") or modal.text)
            if text:
                print(f"  - {text[:160]}")
    else:
        print("调试信息：未扫描到常见弹窗容器。")

    texts = collect_clickable_texts(driver)
    if texts:
        print("调试信息：当前可见按钮/链接文本如下：")
        for text in texts[:40]:
            print(f"  - {text}")


def has_visible_modal(driver):
    return bool(visible_modal_containers(driver))


def confirm_submit_dialog(driver):
    """确认送审弹窗。"""
    time.sleep(AFTER_SUBMIT_CLICK_SECONDS)

    end_time = time.time() + WAIT_TIME
    while time.time() < end_time:
        choose_approval_flow_if_needed(driver)
        confirm_btn = find_confirm_button(driver)
        if confirm_btn:
            print(f"发现送审确认按钮，正在确认：{describe_element(confirm_btn)}")
            if safe_click(driver, confirm_btn):
                try:
                    WebDriverWait(driver, 5).until(lambda d: not has_visible_modal(d))
                    print("确认弹窗已关闭。")
                except TimeoutException:
                    print("已尝试点击确认，但确认弹窗仍未关闭。")
                    print_dialog_debug(driver)
                    return False
                wait_loading_finished(driver)
                time.sleep(AFTER_CONFIRM_CLICK_SECONDS)
                return True
            return False

        time.sleep(0.5)

    print("未找到确认按钮，本次送审可能没有弹窗或弹窗文案已变化。")
    print(f"点击送审后的当前地址：{driver.current_url}")
    print_dialog_debug(driver)
    return False


def print_toast_message(driver):
    messages = find_visible_elements(driver, By.CSS_SELECTOR, ".el-message, .el-notification")
    for message in messages:
        text = message.text.strip()
        if text:
            print(f"系统提示：{text}")


def process_current_page(driver):
    """
    处理当前页所有可送审的线下采购单据。
    返回本页成功触发送审的数量。
    """
    processed_count = 0

    while True:
        wait_loading_finished(driver)
        buttons = find_submit_buttons(driver)

        if not buttons:
            print("当前页没有更多可送审单据。")
            if processed_count == 0:
                print_clickable_debug(driver)
            break

        try:
            button = buttons[0]
            print(f"找到送审按钮，正在处理第 {processed_count + 1} 条：{describe_element(button)}")
            if not safe_click(driver, button):
                break

            if confirm_submit_dialog(driver):
                print("已点击确认按钮，等待系统返回结果...")
            print_toast_message(driver)

            processed_count += 1
            time.sleep(1.2)

        except (StaleElementReferenceException, NoSuchElementException):
            time.sleep(1)
            continue
        except Exception as exc:
            print(f"处理当前单据时出错：{exc}")
            break

    return processed_count


def click_next_page(driver):
    """点击分页中的下一页。"""
    wait_loading_finished(driver)
    next_btn = find_next_page_button(driver)

    if not next_btn:
        print("未找到可用的下一页按钮，可能已经是最后一页。")
        return False

    try:
        print("正在翻到下一页...")
        safe_click(driver, next_btn)
        time.sleep(2)
        wait_loading_finished(driver)
        return True
    except Exception as exc:
        print(f"翻页出错：{exc}")
        return False


def find_next_page_button(driver):
    driver.switch_to.default_content()
    button = find_next_page_button_in_current_context(driver)
    if button:
        return button

    return find_next_page_button_in_frames(driver)


def find_next_page_button_in_current_context(driver):
    next_xpath = (
        "//button[contains(@class, 'btn-next') and not(@disabled) "
        "and not(contains(@class, 'is-disabled'))]"
        " | //li[contains(@class, 'next') and not(contains(@class, 'disabled'))]"
    )

    try:
        next_btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, next_xpath)))
        return None if next_btn.get_attribute("disabled") else next_btn
    except TimeoutException:
        return None


def find_next_page_button_in_frames(driver, depth=0, max_depth=3):
    if depth >= max_depth:
        return None

    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for index in range(len(frames)):
        try:
            driver.switch_to.frame(index)
            button = find_next_page_button_in_current_context(driver)
            if button:
                return button

            nested_button = find_next_page_button_in_frames(driver, depth + 1, max_depth)
            if nested_button:
                return nested_button

            driver.switch_to.parent_frame()
        except Exception:
            driver.switch_to.default_content()

    driver.switch_to.default_content()
    return None


def switch_to_latest_window(driver):
    handles = driver.window_handles
    if handles:
        driver.switch_to.window(handles[-1])


def ensure_target_page(driver):
    """登录等待结束后确认浏览器仍在目标页面。"""
    switch_to_latest_window(driver)
    current_url = driver.current_url
    print(f"当前浏览器地址：{current_url}")

    if "member.casmart.com.cn" not in current_url:
        print("当前不在 Casmart 页面，正在重新跳转...")
        driver.get("about:blank")
        time.sleep(1)
        driver.execute_script("window.location.href = arguments[0];", TARGET_URL)
        wait_page_ready(driver)
        wait_loading_finished(driver)


def main():
    credentials = ask_credentials()
    if not credentials:
        print("已取消运行。")
        return

    username, password = credentials
    driver = init_driver()
    total_processed = 0

    try:
        print(f"正在打开 Casmart 线下采购单据列表：{TARGET_URL}")
        driver.get("about:blank")
        time.sleep(1)
        driver.execute_script("window.location.href = arguments[0];", TARGET_URL)
        wait_page_ready(driver)
        auto_login_if_needed(driver, username, password)
        ensure_target_page(driver)

        page_count = 1
        while True:
            print(f"\n--- 正在处理第 {page_count} 页 ---")
            current_count = process_current_page(driver)
            total_processed += current_count

            if MAX_PAGES and page_count >= MAX_PAGES:
                print(f"已达到最大处理页数：{MAX_PAGES}")
                break

            if click_next_page(driver):
                page_count += 1
                continue

            break

        print(f"\n全部处理完成，本次共触发送审 {total_processed} 条线下采购单据。")

    except KeyboardInterrupt:
        print("\n用户中断脚本。")
        sys.exit(130)
    except Exception as exc:
        print(f"程序异常终止：{exc}")
    finally:
        print("脚本运行结束，浏览器将保持打开，便于你检查结果。")
        # 如需自动关闭浏览器，取消下一行注释：
        # driver.quit()


if __name__ == "__main__":
    main()
