"""按供应商进入整张签收单，按组内商品核对附件。"""
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
from order_list import (OrderListBrowser, OrderListStopped, PRODUCT_COLUMNS, PROJECT, SUPPLIER_HEADERS,
                        aligned_rows, product_key)
from receipt_materials import (REQUIRED, ReceiptStopped, accepts_file, build_plan, build_supplier_plan,
                              catalog, load_manifest, record_supplier)


ROW_LABELS = frozenset({"签收", "签收单据", "订单签收"})
FINAL_LABELS = ("签收", "确认签收", "提交签收")
UPLOAD_WAIT = 90

# 仅从有明确签收操作的可见表单读取上传状态。file input 本身可以由上传组件隐藏。
READ_RECEIPT_FORM = r"""
const [labels, fullPage, productLabels, supplierLabels] = arguments;
const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
const buttonSelector = 'button,a,[role="button"],.el-button,.ant-btn,.layui-btn,[onclick]';
const dialogSelector = 'dialog,[role="dialog"],.el-dialog,.ant-modal,.layui-layer-dialog,.layui-layer-page';
if (document.readyState !== 'complete') return [];
const roots = new Set([...document.querySelectorAll('input[type="file"]')].map(field =>
    field.closest(dialogSelector) || field.closest('form,.el-form') ||
    (fullPage && (field.closest('main') || document.body))).filter(root => root && visible(root)));
const forms = [];
for (const root of roots) {
    if ([...root.querySelectorAll('.el-loading-mask,.ant-spin-spinning,[aria-busy="true"]')].some(visible)) continue;
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
    const tableSelector = '.el-table,.ant-table,table';
    const isHeader = (label, titles) => titles.includes(label.replace(/[\s：:]/g, ''));
    const products = [];
    const suppliers = [];
    const productTables = [];
    let incomplete = [...root.querySelectorAll('.el-pagination,.ant-pagination,.layui-laypage,' +
        '.el-table-v2,.ant-table-tbody-virtual,.rc-virtual-list,.vue-recycle-scroller,' +
        '.virtual-scroller,.virtual-list,[data-virtualized="true"]')].some(visible) ||
        [...root.querySelectorAll(buttonSelector)].some(e =>
            visible(e) && ['上一页','下一页','上页','下页'].includes((e.innerText || '').replace(/\s/g, '')));
    for (const table of root.querySelectorAll(tableSelector)) {
        const parent = table.parentElement.closest(tableSelector);
        if (!visible(table) || parent && root.contains(parent)) continue;
        const headers = [...table.querySelectorAll('thead th')].filter(visible);
        const goods = headers.flatMap((cell, i) => isHeader(cell.innerText, productLabels) ? [i] : []);
        if (!goods.length) continue;
        if (goods.length !== 1 || headers.some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1)) {
            incomplete = true;
            continue;
        }
        const vendors = headers.flatMap((cell, i) => isHeader(cell.innerText, supplierLabels) ? [i] : []);
        const bodies = [...table.querySelectorAll('tbody')];
        const rows = [...table.querySelectorAll('tbody tr')].filter(visible).map(row => {
            const cells = [...row.cells].filter(visible);
            return {cells: cells.map(cell => cell.innerText), spans: cells.map(cell => [cell.rowSpan, cell.colSpan]),
                merged: cells.some(cell => cell.rowSpan !== 1 || cell.colSpan !== 1), group: bodies.indexOf(row.parentElement)};
        });
        productTables.push({width: headers.length, product: goods[0], vendors, rows});
    }
    for (const label of root.querySelectorAll('label')) {
        if (label.control?.type !== 'text' || !visible(label.control)) continue;
        if (!productTables.length && isHeader(label.innerText, productLabels)) products.push(label.control.value);
        if (isHeader(label.innerText, supplierLabels)) suppliers.push(label.control.value);
    }
    forms.push({root, text: root.innerText, products, suppliers, buttons, inputs, files,
        productTables, incomplete: incomplete || productTables.length > 1});
}
return forms;
"""


def labelled_names(text, labels):
    pattern = r"(?:^|\n)[ \t]*(?:" + "|".join(map(re.escape, sorted(labels, key=len, reverse=True)))
    pattern += r")[ \t]*(?:[:：][ \t]*|\n[ \t]*|\t)([^\n\t]+)"
    return re.findall(pattern, text)


def form_products(form, supplier, product_labels=PRODUCT_COLUMNS, supplier_labels=SUPPLIER_HEADERS):
    if form["incomplete"]:
        raise ReceiptStopped("签收页有分页或商品表结构不清，无法核对完整商品清单。")
    vendors, products = list(form["suppliers"]), list(form["products"])
    for table in form["productTables"]:
        for _entry, cells in aligned_rows(table["rows"], table["width"], allow_rowspan=True):
            products.append(cells[table["product"]])
            vendors.extend(cells[index] for index in table["vendors"])
    shown = {product_key(name) for name in (vendors or labelled_names(form["text"], supplier_labels)) if name.strip()}
    if shown and shown != {product_key(supplier)}:
        raise ReceiptStopped("签收表单上的供应商与所选供应商不一致。")
    if not products and not form["productTables"]:
        products = labelled_names(form["text"], product_labels)
    return tuple(product_key(name) for name in products)


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
    """同一张合并单以供应商定位，再核对完整商品清单。"""
    row_labels = ROW_LABELS
    operation = "签收"
    require_supplier = True

    def __init__(self, driver, product_column=None, timeout=shared.WAIT_TIME, *, supplier_column=None):
        super().__init__(driver, product_column, timeout, supplier_column=supplier_column)
        self.open_url = None

    def find_supplier(self, previous):
        matches = [order for order in self.scan()
                   if order.actionable and product_key(order.supplier) == product_key(previous.supplier)]
        if len(matches) != 1:
            raise ReceiptStopped("所选供应商未能唯一对应到一个可用的签收入口。")
        order = matches[0]
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

    def read_form(self, supplier):
        self.guard_receipt()
        forms = []
        products = PRODUCT_COLUMNS | ({self.product_column} if self.product_column else set())
        suppliers = SUPPLIER_HEADERS | ({self.supplier_column} if self.supplier_column else set())
        for path in self.contexts():
            # 完整页面只有在行按钮引起页面跳转后才允许作为签收区域。
            full_page = self.driver.execute_script("return location.href") != self.open_url
            for form in self.driver.execute_script(READ_RECEIPT_FORM, FINAL_LABELS, full_page,
                                                   sorted(products), sorted(suppliers)):
                forms.append((path, form))
        if len(forms) > 1:
            raise ReceiptStopped("页面上有多个可能的签收表单，无法唯一定位。")
        if not forms:
            return None
        path, form = forms[0]
        form["product_names"] = form_products(form, supplier, products, suppliers)
        if not form["product_names"]:
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

    def inspect_products(self, supplier):
        previous = None

        def stable():
            nonlocal previous
            state = self.read_form(supplier)
            products = state[1]["product_names"] if state else None
            if products and products == previous:
                return products
            previous = products
            return False

        return self.wait(stable, "未读到该供应商签收表单的商品清单，尚未上传。")

    def checked_form(self, plan):
        state = self.read_form(plan.supplier)
        names = [product_key(item.product) for item in plan.products]
        if state and Counter(state[1]["product_names"]) != Counter(names):
            raise ReceiptStopped("签收商品清单在核对后发生变化。")
        return state

    def upload(self, plan):
        _path, form = self.wait(lambda: self.checked_form(plan), "无法再次核对该供应商的签收表单。")
        if form["files"] or any(slot["selected"] for slot in form["inputs"]):
            raise ReceiptStopped("签收表单已经有附件，请先在网页核对，避免重复上传。")
        groups = upload_groups(form["inputs"], plan.files)
        if not all(slot["input"].is_enabled() for slot, _files in groups):
            raise ReceiptStopped("签收页有上传区尚不可用。")
        for product in plan.products:
            for file in product.files:
                file.verify()
        sent = ()
        for index, (original, files) in enumerate(groups):
            # 上传一组后页面可能重绘，下一组重新核对商品及控件。
            state = self.checked_form(plan)
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
        state = self.checked_form(plan)
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


def supplier_choices(rows):
    """同一个供应商的合并行可共享一个按钮。多个可用按钮时让本人核对。"""
    counts = Counter(product_key(row.supplier) for row in rows if row.actionable)
    ready, skipped = [], []
    for row in rows:
        if not row.actionable:
            continue
        name = product_key(row.supplier)
        if not name:
            skipped.append((row, "网页没有可读的供应商名称"))
        elif counts[name] != 1:
            skipped.append((row, "该供应商有多个可用的签收入口"))
        else:
            ready.append(row)
    return ready, skipped


def show_plan(plan):
    vendor = f"（{plan.supplier}）" if plan.supplier else "（清单未记录供应商）"
    print(f"\n商品「{plan.product}」{vendor}。文件夹 {plan.folder.name}")
    for file in plan.files:
        print(f"  {file.category}  {file.path.name}  ({file.size} 字节)")


def confirm(action, supplier):
    try:
        return input(f"核对供应商「{supplier}」及商品材料后输入“{action}”，直接回车取消：").strip() == action
    except EOFError:
        return False


def choose_supplier(ready):
    if len(ready) == 1:
        return ready[0]
    try:
        answer = input("选择本次要查看的供应商序号，直接回车取消：").strip()
        choice = int(answer)
    except (EOFError, ValueError):
        return None
    return ready[choice - 1] if 1 <= choice <= len(ready) else None


def run_receipt(browser, order, manifest, report, *, confirm_action=confirm):
    fresh = browser.find_supplier(order)
    browser.open_receipt(fresh)
    products = browser.inspect_products(fresh.supplier)
    plan = build_supplier_plan(manifest, report, fresh.supplier, products)
    print(f"\n供应商「{plan.supplier}」的签收单有 {len(plan.products)} 个商品。")
    for item in plan.products:
        show_plan(item)
    if any(not item.supplier for item in plan.products):
        print("清单未记录供应商的文件夹按商品名找出，请核对这些材料属于当前供应商。")
    warnings = plan.products[0].warnings
    if warnings:
        print(f"分类批次有 {warnings} 条格式警告，请查阅原清单。")
    print(f"本次上传 {len(plan.files)} 个文件，同一签收单的同类同内容文件合并上传。")
    if not confirm_action("上传", plan.supplier):
        print("已取消上传。")
        return
    browser.upload(plan)
    print(f"页面已确认 {len(plan.files)} 个文件都上传成功。请在浏览器核对附件后确认签收。")
    if not confirm_action("签收", plan.supplier):
        print("最终签收已取消。附件可能已暂存，请在网页核对。")
        return
    browser.finish(plan)
    print(f"供应商「{plan.supplier}」签收成功，已看到明确的网页提示。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="按供应商选择签收单，按组内商品匹配分类文件夹")
    parser.add_argument("--folder", "--manifest", dest="source", help="分类批次文件夹或其中的 manifest.json")
    parser.add_argument("--supplier-column", help="网页供应商名称列，默认识别常用供应商表头")
    parser.add_argument("--product-column", help="签收明细的商品名称列，默认识别常用商品表头")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-list", action="store_true", help="登录，只读预览供应商和签收入口")
    mode.add_argument("--execute", action="store_true", help="选择供应商并核对商品材料，确认后上传及签收")
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
            for name, records in groups.items():
                for supplier in sorted({record_supplier(record) for record in records}):
                    try:
                        show_plan(build_plan(manifest, report, name, groups, supplier=supplier))
                        checked += 1
                    except ReceiptStopped as exc:
                        print(f"[跳过] {name} {supplier}：{exc}")
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
        browser = ReceiptBrowser(driver, args.product_column, supplier_column=args.supplier_column)
        print("正在读取当前筛选中的供应商及签收入口。")
        ready, skipped = supplier_choices(browser.scan())
        for row, reason in skipped:
            print(f"[跳过] 第 {row.page} 页第 {row.position} 行 {row.supplier}：{reason}")
        for index, row in enumerate(ready, 1):
            print(f"{index}. {row.supplier}（第 {row.page} 页第 {row.position} 行）")
        print(f"可选择 {len(ready)} 家供应商，待核对 {len(skipped)} 个入口。")
        if args.execute and ready:
            selection = choose_supplier(ready)
            if selection:
                run_receipt(browser, selection, manifest, report)
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
