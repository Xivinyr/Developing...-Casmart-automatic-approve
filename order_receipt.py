"""按网页商品名匹配分类文件夹，默认仅作本地预检。"""
import argparse
from collections import Counter
import re
import sys
import tkinter as tk
from tkinter import filedialog
from urllib.parse import urlsplit

from selenium.common.exceptions import NoSuchFrameException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import songshen as shared
from order_list import OrderListBrowser, OrderListStopped, PRODUCT_COLUMNS, PROJECT, product_key
from receipt_materials import REQUIRED, ReceiptStopped, accepts_file, build_plan, catalog, load_manifest


ROW_LABELS = frozenset({"签收", "签收单据", "订单签收"})
FINAL_LABELS = ("签收", "确认签收", "提交签收")
UPLOAD_WAIT = 90

# 仅从有明确签收操作的可见表单读取上传状态。file input 本身可以由上传组件隐藏。
READ_RECEIPT_FORM = r"""
const [labels, fullPage, productLabels] = arguments;
const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
const buttonSelector = 'button,a,[role="button"],.el-button,.ant-btn,.layui-btn,[onclick]';
const dialogSelector = 'dialog,[role="dialog"],.el-dialog,.ant-modal,.layui-layer-dialog,.layui-layer-page';
if (document.readyState !== 'complete') return [];
const roots = new Set([...document.querySelectorAll('input[type="file"]')].map(field =>
    field.closest(dialogSelector) || field.closest('form,.el-form') ||
    (fullPage && (field.closest('main') || document.body))).filter(root => root && visible(root)));
const forms = [];
for (const root of roots) {
    const buttons = [...root.querySelectorAll(buttonSelector)].filter(e =>
        visible(e) && labels.includes((e.innerText || e.title || e.getAttribute('aria-label') || '').replace(/\s/g, '')) &&
        !e.parentElement.closest('button,a,[role="button"],.el-button,.ant-btn,.layui-btn'));
    if (!buttons.length) continue;
    const files = [...root.querySelectorAll('.el-upload-list__item,.ant-upload-list-item,[data-upload-name]')]
        .filter(visible).map(item => {
            const label = item.querySelector('.el-upload-list__item-name,.el-upload-list__item-file-name,.ant-upload-list-item-name');
            const name = item.dataset.uploadName || (label && (label.title || label.innerText)) || '';
            let status = item.dataset.uploadStatus || item.dataset.status || '';
            if (item.matches('.is-success,.ant-upload-list-item-done')) status = 'success';
            if (item.matches('.is-fail,.is-error,.ant-upload-list-item-error')) status = 'error';
            return {name: name.trim(), status};
        });
    const inputs = [...root.querySelectorAll('input[type="file"]')].map(input => {
        const field = input.closest('.el-form-item,.ant-form-item,.layui-form-item,[data-upload-category]');
        const label = input.labels?.[0] || field?.querySelector('.el-form-item__label,.ant-form-item-label,label');
        const category = field?.dataset.uploadCategory || label?.innerText || '';
        return {input, accept: input.accept, multiple: input.multiple, selected: input.files.length,
            category: category.trim().replace(/[：:]$/, '').trim(),
            // 表单显式指向站外时，不把材料交给该控件。
            targetOK: !input.form || new URL(input.form.action, location.href).origin === location.origin};
    });
    const isProduct = label => productLabels.includes(label.replace(/[\s：:]/g, ''));
    const products = [];
    for (const table of root.querySelectorAll('table')) {
        const columns = [...table.querySelectorAll('thead th')].map(e => isProduct(e.innerText));
        for (const row of table.querySelectorAll('tbody tr')) {
            const cells = [...row.cells];
            if (cells.length === columns.length)
                cells.forEach((cell, index) => { if (columns[index]) products.push(cell.innerText); });
        }
    }
    for (const label of root.querySelectorAll('label')) {
        if (isProduct(label.innerText) && label.control?.type === 'text' && visible(label.control))
            products.push(label.control.value);
    }
    forms.push({root, text: root.innerText, products, buttons, inputs, files});
}
return forms;
"""


def product_from_form(form, product, labels=PRODUCT_COLUMNS):
    pattern = r"(?:^|\n)[ \t]*(?:" + "|".join(map(re.escape, sorted(labels, key=len, reverse=True)))
    pattern += r")[ \t]*(?:[:：][ \t]*|\n[ \t]*|\t)([^\n\t]+)"
    values = form["products"] or re.findall(pattern, form["text"])
    matches = {product_key(value) for value in values if value.strip()}
    expected = {product_key(product)}
    if matches and matches != expected:
        raise ReceiptStopped("签收表单上的商品名称与已核对的商品不一致。")
    return matches == expected


def uploads_complete(rows, names):
    actual = [row["name"] for row in rows]
    if any(row["status"].lower() in {"error", "fail", "failed"} for row in rows):
        raise ReceiptStopped("页面报告附件上传失败，请在网页核对。")
    named = [name for name in actual if name]
    if len(set(named)) != len(named) or set(named).difference(names):
        raise ReceiptStopped("页面出现无法对应的附件或重名文件，请人工核对。")
    return set(actual) == set(names) and all(row["status"].lower() in {"success", "done"} for row in rows)


def upload_groups(slots, files):
    """单个附件区接收全部材料，分开的栏位必须逐类标明名称。"""
    if len(slots) == 1:
        groups = [(slots[0], files)]
    else:
        by_category = {slot["category"]: slot for slot in slots}
        if len(by_category) != len(slots) or set(by_category) != set(REQUIRED):
            raise ReceiptStopped("多个上传区的分类不明确，需要先核对真实网页布局。")
        groups = [(by_category[category], tuple(file for file in files if file.category == category))
                  for category in REQUIRED]
    for slot, group in groups:
        if not slot["targetOK"]:
            raise ReceiptStopped("无法确认上传控件属于 Casmart。")
        if len(group) > 1 and not slot["multiple"]:
            raise ReceiptStopped("该上传区不能一次接收对应分类的多个文件。")
        if any(not accepts_file(slot["accept"], file) for file in group):
            raise ReceiptStopped("网页上传区不接受清单中的文件类型，请核对 PDF 或图片要求。")
    return groups


class ReceiptBrowser(OrderListBrowser):
    """复用列表和分页，只处理名称能唯一对应的商品。"""
    row_labels = ROW_LABELS
    operation = "签收"
    require_product = True

    def __init__(self, driver, product_column=None, timeout=shared.WAIT_TIME):
        super().__init__(driver, product_column, timeout)
        self.open_url = None

    def find_product(self, product, previous):
        matches = [order for order in self.scan() if product_key(order.product) == product_key(product)]
        if len(matches) != 1:
            raise ReceiptStopped("当前筛选范围内未能唯一找到该商品，请重新核对。")
        order = matches[0]
        if not order.actionable:
            raise ReceiptStopped("该单据没有可用的签收按钮，可能已签收或状态不符。")
        if order.fingerprint != previous.fingerprint:
            raise ReceiptStopped("单据内容在预览后已变化，请重新核对。")
        return order

    def guard_receipt(self):
        d = self.driver
        if self.alert() is not None:
            raise ReceiptStopped("网页出现需人工处理的确认弹窗，请检查当前单据。")
        if d.current_window_handle != self.window or d.window_handles != [self.window]:
            raise ReceiptStopped("签收打开了其他窗口，暂停以供人工核对。")
        d.switch_to.default_content()
        url = urlsplit(d.current_url)
        if (url.scheme != "https" or url.hostname != "member.casmart.com.cn"
                or url.port not in (None, 443) or url.username is not None):
            raise ReceiptStopped("已离开 Casmart 会员中心，停止签收。")

    def read_form(self, product):
        self.guard_receipt()
        forms = []
        labels = PRODUCT_COLUMNS | ({self.product_column} if self.product_column else set())
        for path in self.contexts():
            # 完整页面只有在行按钮引起页面跳转后才允许作为签收区域。
            full_page = self.driver.execute_script("return location.href") != self.open_url
            for form in self.driver.execute_script(READ_RECEIPT_FORM, FINAL_LABELS, full_page, sorted(labels)):
                forms.append((path, form))
        if len(forms) > 1:
            raise ReceiptStopped("页面上有多个可能的签收表单，无法唯一定位。")
        if not forms:
            return None
        path, form = forms[0]
        if not product_from_form(form, product, labels):
            return None
        if len(form["buttons"]) != 1:
            raise ReceiptStopped("签收表单的最终签收按钮不唯一，请先核对页面。")
        # 原生上传可以对隐藏 input 工作，因此也确认没有其他弹窗挡在表单上面。
        blockers = self.modals()
        self.enter(path)
        if len(blockers) > 1 or blockers and (
                blockers[0][0] != path or not self.driver.execute_script(
                    "return arguments[0].contains(arguments[1])", blockers[0][1], form["root"])):
            raise ReceiptStopped("网页另有活动弹窗，暂停上传或签收。")
        return path, form

    def wait(self, read, reason, timeout=None):
        try:
            return WebDriverWait(self.driver, self.timeout if timeout is None else timeout, poll_frequency=0.25,
                ignored_exceptions=(StaleElementReferenceException, NoSuchFrameException)).until(lambda _: read())
        except TimeoutException as exc:
            raise ReceiptStopped(reason) from exc

    def open_receipt(self, order):
        button, _page = self.row_action(order)
        self.open_url = self.driver.execute_script("return location.href")
        self.click_once(button)

    def upload(self, plan):
        _path, form = self.wait(lambda: self.read_form(plan.product),
            "未能在签收表单核对同名商品，尚未上传。需要先核对实际网页。")
        if form["files"] or any(slot["selected"] for slot in form["inputs"]):
            raise ReceiptStopped("签收表单已经有附件，请先在网页核对，避免重复上传。")
        groups = upload_groups(form["inputs"], plan.files)
        if not all(slot["input"].is_enabled() for slot, _files in groups):
            raise ReceiptStopped("签收页有上传区尚不可用。")
        for file in plan.files:
            file.verify()
        sent = ()
        for index, (original, files) in enumerate(groups):
            # 上传一组后页面可能重绘，下一组重新核对商品及控件。
            state = self.read_form(plan.product)
            if not state:
                raise ReceiptStopped("上传前签收页面发生变化。")
            current = upload_groups(state[1]["inputs"], plan.files)
            if len(current) != len(groups) or current[index][0]["category"] != original["category"]:
                raise ReceiptStopped("上传区在处理中发生变化，请人工核对。")
            slot = current[index][0]
            if slot["selected"] or not slot["input"].is_enabled() or not uploads_complete(state[1]["files"], sent):
                raise ReceiptStopped("上传区或已上传文件在处理中发生变化，请人工核对。")
            for file in files:
                file.verify()
            # 每组只上传一次。不确定时保留页面，避免重复发送。
            slot["input"].send_keys("\n".join(str(file.path) for file in files))
            sent += tuple(file.path.name for file in files)
            self.wait(lambda: self.verified_upload(plan, sent),
                "未能逐个确认附件上传成功，请检查网页。程序尚未点击最终签收。", timeout=UPLOAD_WAIT)

    def verified_upload(self, plan, names=None):
        state = self.read_form(plan.product)
        if state is None:
            return False
        _path, form = state
        return state if uploads_complete(form["files"], plan.names if names is None else names) else False

    def messages(self):
        self.guard_receipt()
        found = {}
        for _path in self.contexts():
            for element in shared.find_visible_elements(self.driver, By.CSS_SELECTOR,
                    ".el-message,.el-notification,.ant-message-notice,[role='status'],[role='alert']"):
                found[element.id] = shared.compact_text(element.text).strip("！!。")
        return found

    def finish(self, plan):
        old_messages = self.messages()
        state = self.verified_upload(plan)
        if not state:
            raise ReceiptStopped("最终确认前附件或表单已变化，请人工核对。")
        _path, form = state
        submit = form["buttons"][0]
        if not submit.is_enabled() or shared.is_disabled(submit):
            raise ReceiptStopped("最终签收按钮当前不可用，请检查页面。")
        print("正在点击最终签收，等待页面给出具体结果……", flush=True)
        self.click_once(submit)

        def completed():
            messages = self.messages()
            new = [text for key, text in messages.items() if old_messages.get(key) != text]
            if any("签收失败" in text or "签收未完成" in text for text in new):
                raise ReceiptStopped("网页报告签收失败，请人工检查。")
            return any(text in {"签收成功", "签收完成"} for text in new)

        self.wait(completed, "未看到明确的签收成功提示。可能已受理，请人工核对单据状态，勿直接重试。")


def choose_source():
    root = tk.Tk()
    root.withdraw()
    try:
        return filedialog.askdirectory(parent=root, title="选择包含 manifest.json 的分类批次文件夹",
                                       initialdir=PROJECT / "outputs", mustexist=True)
    finally:
        root.destroy()


def match_receipts(rows, manifest, report):
    groups = catalog(report)
    counts = Counter(product_key(row.product) for row in rows)
    ready, skipped = [], []
    for row in rows:
        if not row.actionable:
            continue
        name = product_key(row.product)
        if not name:
            skipped.append((row, "网页没有可读的商品名称"))
        elif counts[name] != 1:
            skipped.append((row, "当前筛选范围内有同名商品"))
        else:
            try:
                ready.append((row, build_plan(manifest, report, row.product, groups)))
            except ReceiptStopped as exc:
                skipped.append((row, str(exc)))
    return ready, skipped


def show_plan(plan):
    print(f"\n商品：{plan.product}。文件夹：{plan.folder.name}")
    if plan.warnings:
        print(f"分类批次有 {plan.warnings} 条格式警告，请查阅原清单。")
    for file in plan.files:
        print(f"  {file.category}  {file.path.name}  ({file.size} 字节)")


def confirm(action, product):
    try:
        return input(f"核对「{product}」后输入“{action}”继续，直接回车取消：").strip() == action
    except EOFError:
        return False


def choose_match(ready):
    if len(ready) == 1:
        return ready[0]
    try:
        answer = input("选择本次要签收的商品序号，直接回车取消：").strip()
        choice = int(answer)
    except (EOFError, ValueError):
        return None
    return ready[choice - 1] if 1 <= choice <= len(ready) else None


def run_receipt(browser, order, plan, *, confirm_action=confirm):
    show_plan(plan)
    print(f"网页匹配位置：第 {order.page} 页第 {order.position} 行。")
    for column, value in order.details:
        print(f"  {column}：{value}")
    if not confirm_action("上传", plan.product):
        print("已取消上传。")
        return
    fresh = browser.find_product(plan.product, previous=order)
    browser.open_receipt(fresh)
    browser.upload(plan)
    print(f"页面已确认 {len(plan.files)} 个文件都上传成功。请在浏览器核对附件后确认签收。")
    if not confirm_action("签收", plan.product):
        print("最终签收已取消。附件可能已暂存，请在网页核对。")
        return
    browser.finish(plan)
    print(f"商品「{plan.product}」签收成功，已看到明确的网页提示。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="按商品名匹配分类文件夹，默认只检查本地材料")
    parser.add_argument("--folder", "--manifest", dest="source", help="分类批次文件夹或其中的 manifest.json")
    parser.add_argument("--product-column", help="网页商品名称列，默认识别常用商品表头")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-list", action="store_true", help="登录，只读核对商品和文件夹匹配")
    mode.add_argument("--execute", action="store_true", help="选择匹配商品并确认上传和签收")
    parser.add_argument("--via-casmart", action="store_true", help="从商城入口登录")
    parser.add_argument("--wait-on-exit", action="store_true", help="保留任务窗口，回车关闭")
    args = parser.parse_args(argv)
    try:
        source = args.source if args.source is not None else choose_source()
        if not source:
            return 0
        manifest, report = load_manifest(source)
        groups = catalog(report)
        if not groups:
            raise ReceiptStopped("分类批次没有可按名称匹配的商品。")
        if not (args.check_list or args.execute):
            checked = 0
            for name in groups:
                try:
                    show_plan(build_plan(manifest, report, name, groups))
                    checked += 1
                except ReceiptStopped as exc:
                    print(f"[跳过] {name}：{exc}")
            print(f"本地预检完成，共 {checked} 个商品的材料通过。")
            return 0 if checked else 2

        start = shared.TARGET_URL if args.via_casmart else shared.load_school_login_url()
        accounts = shared.ask_credentials(login_only=True, direct=not args.via_casmart)
        if accounts is None:
            return 0
        driver = shared.init_driver()
        driver.get(start)
        if not shared.auto_login_if_needed(driver, accounts) or not shared.ensure_target_page(driver):
            raise ReceiptStopped("未完成登录或进入采购单据列表。")
        browser = ReceiptBrowser(driver, args.product_column)
        print("正在只读核对当前筛选中的商品和分类文件夹。")
        ready, skipped = match_receipts(browser.scan(), manifest, report)
        for row, reason in skipped:
            print(f"[跳过] 第 {row.page} 页第 {row.position} 行 {row.product}：{reason}")
        for index, (row, plan) in enumerate(ready, 1):
            print(f"{index}. {row.product} → {plan.folder.name}（{len(plan.files)} 个附件）")
        print(f"唯一匹配且文件通过预检 {len(ready)} 条，待人工核对 {len(skipped)} 条。")
        if args.execute and ready:
            selection = choose_match(ready)
            if selection:
                run_receipt(browser, *selection)
        return 0
    except (ReceiptStopped, OrderListStopped) as exc:
        print(f"签收任务停止：{exc}")
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        print(f"签收任务停止（{type(exc).__name__}）。请核对页面和已上传附件，勿重复提交。")
        return 2
    finally:
        if args.wait_on_exit:
            try:
                input("按回车关闭任务窗口。")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    sys.exit(main())
