"""按本人选定的单据和附件清单签收。默认仅作本地预检。"""
import argparse
import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlsplit

from selenium.common.exceptions import NoSuchFrameException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import songshen as shared
from order_delete import ID_COLUMNS, OrderListBrowser, OrderListStopped, PROJECT
from receipt_materials import REQUIRED, ReceiptStopped, accepts_file, build_plan, load_manifest


ROW_LABELS = frozenset({"签收", "签收单据", "订单签收"})
FINAL_LABELS = ("签收", "确认签收", "提交签收")
UPLOAD_WAIT = 90
ID_LINE = re.compile(r"(?:^|\n)\s*(?:线下)?(" + "|".join(ID_COLUMNS)
                     + r")\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)(?![A-Za-z0-9._/-])")
ID_EQUIVALENTS = ({"单据编号", "单据号"}, {"订单编号", "订单号"}, {"采购单号", "采购单据编号"})

# 仅从有明确签收操作的可见表单读取上传状态。file input 本身可以由上传组件隐藏。
READ_RECEIPT_FORM = r"""
const [labels, fullPage] = arguments;
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
    forms.push({root, text: root.innerText, buttons, inputs, files});
}
return forms;
"""


def identity_from_form(text, column, order_id):
    labels = next((group for group in ID_EQUIVALENTS if column in group), {column})
    matches = {value for label, value in ID_LINE.findall(text) if label in labels}
    if matches and matches != {order_id}:
        raise ReceiptStopped("签收表单上的单号与已核对的单据不一致。")
    return matches == {order_id}


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
    """复用列表、分页与单次点击机制。签收页只操作已核对单号的表单。"""
    row_labels = ROW_LABELS
    operation = "签收"

    def __init__(self, driver, id_column=None, timeout=shared.WAIT_TIME):
        super().__init__(driver, id_column, timeout)
        self.receipt_column = None
        self.open_url = None

    def find_order(self, plan, previous=None):
        matches = self.scan([plan.order_id])[plan.order_id]
        if len(matches) != 1:
            reason = "匹配多条" if matches else "未找到"
            raise ReceiptStopped(f"当前筛选范围内{reason}该单号，请核对条件和单号。")
        order = matches[0]
        if not order.actionable:
            raise ReceiptStopped("该单据没有可用的签收按钮，可能已签收或状态不符。")
        products = [value for label, value in zip(order.columns, order.cells)
                    if label in {"商品", "商品名称", "采购商品", "品名"} and value.strip()]
        product = shared.compact_text(plan.product)
        if (len(products) == 1 and product not in {"", "未识别商品"}
                and shared.compact_text(products[0]) != product):
            raise ReceiptStopped("清单商品与网页商品名称不同，请核对单据和分类记录。")
        if previous is not None and order.fingerprint != previous.fingerprint:
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

    def read_form(self, order_id):
        self.guard_receipt()
        forms = []
        for path in self.contexts():
            # 完整页面只有在行按钮引起页面跳转后才允许作为签收区域。
            full_page = self.driver.execute_script("return location.href") != self.open_url
            for form in self.driver.execute_script(READ_RECEIPT_FORM, FINAL_LABELS, full_page):
                forms.append((path, form))
        if len(forms) > 1:
            raise ReceiptStopped("页面上有多个可能的签收表单，无法唯一定位。")
        if not forms:
            return None
        path, form = forms[0]
        if not identity_from_form(form["text"], self.receipt_column, order_id):
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
        button = self.row_action(order)
        self.receipt_column = self.id_column or next(column for column in order.columns if column in ID_COLUMNS)
        self.open_url = self.driver.execute_script("return location.href")
        self.click_once(button)

    def upload(self, plan):
        _path, form = self.wait(lambda: self.read_form(plan.order_id),
            "未找到带相同单号的签收表单，尚未上传。需要先核对实际网页。")
        if form["files"] or any(slot["selected"] for slot in form["inputs"]):
            raise ReceiptStopped("签收表单已经有附件，请先在网页核对，避免重复上传。")
        groups = upload_groups(form["inputs"], plan.files)
        if not all(slot["input"].is_enabled() for slot, _files in groups):
            raise ReceiptStopped("签收页有上传区尚不可用。")
        for file in plan.files:
            file.verify()
        sent = ()
        for index, (original, files) in enumerate(groups):
            # 上传一组后页面可能重绘，下一组使用相同单号下重新读取的控件。
            state = self.read_form(plan.order_id)
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
        state = self.read_form(plan.order_id)
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


class ReceiptSelection(simpledialog.Dialog):
    def __init__(self, parent, records):
        self.records = records
        super().__init__(parent, title="选择签收附件和单号")

    def body(self, parent):
        ttk.Label(parent, text="选择一条分类记录，并填写对应 Casmart 完整单号。").grid(sticky="w", pady=(4, 12))
        values = [f"表 {r.get('sheet_index')} · 起始行 {r.get('row')} · {str(r.get('product', ''))[:64]}"
                  for r in self.records]
        self.record = ttk.Combobox(parent, values=values, state="readonly", width=62)
        self.record.grid(sticky="ew")
        ttk.Label(parent, text="Casmart 单号").grid(sticky="w", pady=(10, 3))
        self.number = ttk.Entry(parent, width=28)
        self.number.grid(sticky="w")
        ttk.Label(parent, text="用于匹配的列表列").grid(sticky="w", pady=(10, 3))
        self.column = ttk.Combobox(parent, values=("自动识别唯一编号列", *ID_COLUMNS), state="readonly", width=25)
        self.column.current(0)
        self.column.grid(sticky="w")
        return self.record

    def validate(self):
        index = self.record.current()
        if index < 0 or not self.number.get().strip():
            messagebox.showwarning("缺少信息", "请明确选择一条记录并填写完整单号。", parent=self)
            return False
        record = self.records[index]
        self.result = (record.get("sheet_index"), record.get("row"), self.number.get(), self.column.get()
                       if self.column.current() > 0 else None)
        return True


def choose_plan():
    root = tk.Tk()
    root.withdraw()
    try:
        file = filedialog.askopenfilename(parent=root, title="选择附件分类输出的 manifest.json",
            initialdir=PROJECT / "outputs", filetypes=[("附件分类清单", "manifest.json"), ("JSON 文件", "*.json")])
        if not file:
            return None
        manifest, report = load_manifest(file)
        selected = ReceiptSelection(root, report["records"]).result
        if selected is None:
            return None
        sheet, row, order_id, column = selected
        return build_plan(manifest, report, sheet, row, order_id), column
    finally:
        root.destroy()


def show_plan(plan):
    print(f"\n签收预检 | Casmart 单号 {plan.order_id}")
    print(f"分类记录：表 {plan.sheet}，起始行 {plan.row}，商品 {plan.product or '未填写'}")
    if plan.warnings:
        print(f"分类批次另有 {plan.warnings} 条格式警告，请查阅原清单。")
    print(f"待上传 {len(plan.files)} 个文件：")
    for file in plan.files:
        print(f"  {file.category}  {file.path.name}  ({file.size} 字节)")


def confirm(action, order_id):
    phrase = f"{action} {order_id}"
    try:
        return input(f"核对后输入“{phrase}”继续，直接回车取消：").strip() == phrase
    except EOFError:
        return False


def run_receipt(browser, plan, *, check_only=False, confirm_action=confirm):
    order = browser.find_order(plan)
    print(f"已在当前筛选的第 {order.page} 页找到唯一单据：")
    for column, value in zip(order.columns, order.cells):
        print(f"  {column}：{value}")
    if check_only or not confirm_action("上传", plan.order_id):
        print("本次仅核对单据，未上传或签收。")
        return
    fresh = browser.find_order(plan, previous=order)
    browser.open_receipt(fresh)
    browser.upload(plan)
    print(f"页面已确认 {len(plan.files)} 个文件都上传成功。请在浏览器核对附件后确认签收。")
    if not confirm_action("签收", plan.order_id):
        print("最终签收已取消。附件可能已暂存，请在网页核对。")
        return
    browser.finish(plan)
    print(f"单据 {plan.order_id} 签收成功，已看到明确的网页提示。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="按明确单号上传已分类附件并签收，默认本地预检")
    parser.add_argument("--manifest", type=str, help="由附件分类任务生成的 manifest.json")
    parser.add_argument("--sheet-index", type=int, help="清单中的工作表序号，从 1 开始")
    parser.add_argument("--row", type=int, help="清单中的记录起始行")
    parser.add_argument("--order-id", help="完整 Casmart 单号")
    parser.add_argument("--id-column", choices=ID_COLUMNS, help="列表的编号列，若不确定则自动识别唯一列")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-list", action="store_true", help="登录并只读核对编号")
    mode.add_argument("--execute", action="store_true", help="进入页面并在本人两次确认后上传与签收")
    parser.add_argument("--via-casmart", action="store_true", help="从商城入口登录")
    parser.add_argument("--wait-on-exit", action="store_true", help="保留任务窗口，回车关闭")
    args = parser.parse_args(argv)
    try:
        provided = (args.manifest, args.sheet_index, args.row, args.order_id)
        if all(value is None for value in provided):
            selection = choose_plan()
            if selection is None:
                return 0
            plan, column = selection
        else:
            if any(value is None for value in provided):
                raise ReceiptStopped("请同时指定 --manifest、--sheet-index、--row 和 --order-id。")
            manifest, report = load_manifest(args.manifest)
            plan = build_plan(manifest, report, args.sheet_index, args.row, args.order_id)
            column = None
        show_plan(plan)
        if not (args.check_list or args.execute):
            print("本地预检通过。")
            return 0
        start = shared.TARGET_URL if args.via_casmart else shared.load_school_login_url()
        accounts = shared.ask_credentials(login_only=True, direct=not args.via_casmart)
        if accounts is None:
            return 0
        driver = shared.init_driver()
        driver.get(start)
        if not shared.auto_login_if_needed(driver, accounts) or not shared.ensure_target_page(driver):
            raise ReceiptStopped("未完成登录或进入采购单据列表。")
        run_receipt(ReceiptBrowser(driver, args.id_column or column), plan, check_only=args.check_list)
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
