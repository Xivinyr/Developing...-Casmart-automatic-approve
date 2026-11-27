"""仅按用户指定的唯一编号删除线下单据；默认只读预览，不调用送审流程。"""
import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit
from uuid import uuid4

import songshen as shared
from selenium.common.exceptions import (
    NoAlertPresentException, StaleElementReferenceException, TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


ID_COLUMNS = ("单据编号", "单据号", "订单号", "订单编号", "采购单号", "采购单据编号")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,99}\Z")
DELETE_LABELS = {"删除", "删除订单", "删除单据"}
CONFIRM_LABELS = {"确定", "确认", "确定删除", "确认删除", "删除"}
MAX_SCAN_PAGES = 200
PROJECT = Path(__file__).resolve().parent


class OrderListStopped(RuntimeError):
    """单据列表不能可靠定位时停止，保留页面供人工复核。"""


DeleteStopped = OrderListStopped


@dataclass(frozen=True)
class Order:
    order_id: str
    columns: tuple
    cells: tuple
    actionable: bool
    page: int

    @property
    def fingerprint(self):
        return self.columns, self.cells

    @property
    def deletable(self):
        return self.actionable


@dataclass(frozen=True)
class PlanEntry:
    order_id: str
    order: Order | None
    status: str
    reason: str


def parse_order_ids(values):
    ids = tuple(value.strip() for value in values)
    if not ids or len(ids) > 100:
        raise ValueError("请明确填写 1–100 个错误导入单据的完整编号，每行一个。")
    if any(not ID_PATTERN.fullmatch(value) for value in ids):
        raise ValueError("编号须为 2–100 位字母、数字或 . _ / -，不支持通配符、商品名或行号。")
    if len(ids) != len(set(ids)):
        raise ValueError("输入的编号有重复，请先核对。")
    return ids


# 只读取表格结构。Element UI / Ant Design 的表头与表体可能分属不同 table。
# 固定列副本、多层表头、合并业务行等无法唯一映射时，不猜测对应关系。
READ_TABLES = """
const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
const containers = [...document.querySelectorAll('.el-table,.ant-table,table')]
    .filter(e => visible(e) && !e.parentElement.closest('.el-table,.ant-table,table'));
return containers.map(table => ({
    headers: [...table.querySelectorAll('thead th')].filter(visible).map(e => e.innerText.trim()),
    complex: [...table.querySelectorAll('thead th')].filter(visible).some(e => e.colSpan !== 1 || e.rowSpan !== 1),
    rows: [...table.querySelectorAll('tbody tr')].filter(visible).map(row => ({
        row,
        cells: [...row.children].filter(e => e.tagName === 'TD').map(e => e.innerText.trim()),
        merged: [...row.children].some(e => e.colSpan !== 1 || e.rowSpan !== 1)
    }))
}));
"""


class OrderListBrowser:
    """按编号扫描单据和分页，为独立业务任务提供行定位。"""
    row_labels = frozenset()
    operation = "操作"

    def __init__(self, driver, id_column=None, timeout=shared.WAIT_TIME):
        self.driver, self.id_column, self.timeout = driver, id_column, timeout
        self.window = driver.current_window_handle
        self.scope = None
        self.frame_path = ()

    def guard(self):
        d = self.driver
        if d.current_window_handle != self.window or d.window_handles != [self.window]:
            raise OrderListStopped("窗口范围已改变，停止本次单据操作。")
        d.switch_to.default_content()
        url = urlsplit(d.current_url)
        if (url.scheme != "https" or url.hostname != "member.casmart.com.cn"
                or url.port not in (None, 443) or url.username is not None
                or url.path.rstrip("/") != shared.TARGET_PATH
                or not shared.is_authenticated_casmart(d)):
            raise OrderListStopped("登录或单据列表地址已改变，停止本次单据操作。")

    def enter(self, path):
        self.driver.switch_to.default_content()
        for index in path:
            frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
            self.driver.switch_to.frame(frames[index])
            # 不对跨站 iframe 内的控件执行业务操作。
            origin = self.driver.execute_script("return location.origin")
            if origin != "https://member.casmart.com.cn":
                raise DeleteStopped("单据位于无法确认来源的 iframe 中。")

    def contexts(self, path=()):
        self.enter(path)
        yield path
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
        if len(path) < 3:
            for index, frame in enumerate(frames):
                if frame.is_displayed():
                    yield from self.contexts(path + (index,))
        self.enter(path)

    def actions(self, root, labels):
        candidates = root.find_elements(By.CSS_SELECTOR,
            "button,a,[role='button'],.el-button,.ant-btn,.layui-layer-btn0,.layui-layer-btn1,[onclick],span")
        found = {}
        for item in candidates:
            if not item.is_displayed() or shared.element_text(item) not in labels:
                continue
            button = shared.clickable_ancestor(self.driver, item)
            if (button and self.driver.execute_script("return arguments[0].contains(arguments[1])", root, button)
                    and button.is_displayed() and button.is_enabled() and not shared.is_disabled(button)
                    and shared.element_text(button) in labels):
                found[button.id] = button
        return list(found.values())

    def read_page_once(self):
        self.guard()
        tables = []
        scopes = []
        for path in self.contexts():
            if shared.find_visible_elements(self.driver, By.CSS_SELECTOR,
                    ".el-table-v2,.ant-table-tbody-virtual,.rc-virtual-list,.vue-recycle-scroller,"
                    ".virtual-scroller,.virtual-list,[data-virtualized='true']"):
                raise OrderListStopped("检测到虚拟滚动列表，无法确认完整范围。")
            # 筛选、每页数量、排序或地址改变后，旧的预览不再有效。
            scopes.append((path, self.driver.execute_script("""
                return [location.href,
                    [...document.querySelectorAll('input,select,textarea')]
                    .filter(e => !e.closest('table,.el-table,.ant-table') &&
                        !['hidden','password','button','submit'].includes(e.type) &&
                        !e.closest('.el-pagination__jump,.ant-pagination-options-quick-jumper'))
                    .map(e => [e.id,e.name,e.type,e.value,e.checked]),
                    [...document.querySelectorAll('th.ascending,th.descending,th[aria-sort]')]
                    .map(e => [e.innerText,e.className,e.getAttribute('aria-sort')])];
            """)))
            if shared.find_visible_elements(self.driver, By.CSS_SELECTOR, ".el-loading-mask,.el-loading-spinner"):
                return None
            context_tables = self.driver.execute_script(READ_TABLES)
            for table in context_tables:
                headers = tuple(shared.compact_text(t) for t in table["headers"])
                matching = [i for i, label in enumerate(headers)
                            if label == self.id_column or (self.id_column is None and label in ID_COLUMNS)]
                if not matching:
                    continue
                if any(not other["headers"] and other["rows"] for other in context_tables):
                    raise DeleteStopped("存在未能关联表头的独立表体，扫描不完整。")
                if len(matching) != 1 or table["complex"]:
                    raise DeleteStopped("编号列不唯一或表头含合并单元格，请指定编号列或人工检查。")
                index = matching[0]
                rows = []
                for record in table["rows"]:
                    cells = tuple(record["cells"])
                    if record["merged"] or len(cells) != len(headers):
                        # 常见空表提示不属于订单行。
                        if len(cells) == 1 and shared.compact_text(cells[0]) in {"暂无数据", "暂无相关数据", "无数据"}:
                            continue
                        raise OrderListStopped("表头与订单行不能一一对应。")
                    order_id = cells[index].strip()
                    if not ID_PATTERN.fullmatch(order_id):
                        raise OrderListStopped("编号列包含无法可靠识别的编号。")
                    buttons = self.actions(record["row"], self.row_labels)
                    if len(buttons) > 1:
                        raise OrderListStopped(f"同一订单行存在多个{self.operation}入口。")
                    rows.append((Order(order_id, headers, cells, bool(buttons), 0), buttons))
                tables.append((path, headers, rows))
        if not tables:
            return None
        if len(tables) != 1:
            raise DeleteStopped("存在多个单据表或固定列副本，无法唯一定位。")
        if self.scope is None:
            self.scope = scopes
        elif self.scope != scopes:
            raise DeleteStopped("筛选条件、排序、每页数量或页面范围已改变，请重新预览。")
        self.frame_path, headers, rows = tables[0]
        self.enter(self.frame_path)
        pagers = shared.find_visible_elements(self.driver, By.CSS_SELECTOR,
            ".el-pagination,.ant-pagination,.layui-laypage")
        if len(pagers) > 1:
            raise DeleteStopped("存在多个分页控件，无法确认扫描范围。")
        pager = pagers[0] if pagers else None
        if pager is None and self.driver.execute_script(r"""
            return [...document.querySelectorAll('button,a,[role="button"]')].some(e =>
                e.getClientRects().length && ['上一页','下一页','上页','下页'].includes(e.innerText.replace(/\s/g,'')));
        """):
            raise DeleteStopped("检测到未支持的分页控件，扫描不完整。")
        marker = ""
        if pager:
            marker = "|".join(e.text for e in pager.find_elements(By.CSS_SELECTOR,
                ".el-pager .active,.ant-pagination-item-active,.layui-laypage-curr"))
        signature = (headers, tuple((o.order_id, o.cells, o.actionable) for o, _ in rows), marker)
        return rows, pager, signature

    def page(self):
        previous = None

        def stable(_):
            nonlocal previous
            current = self.read_page_once()
            if current is None:
                previous = None
                return False
            same = current[2] == previous
            previous = current[2]
            return current if same else False

        try:
            return WebDriverWait(self.driver, self.timeout, poll_frequency=0.2,
                ignored_exceptions=(StaleElementReferenceException,)).until(stable)
        except TimeoutException as exc:
            raise DeleteStopped("列表未稳定或缺少可靠编号列，扫描不完整。") from exc

    def move(self, direction):
        rows, pager, old = self.page()
        if pager is None:
            return False
        selector = {"prev": ".btn-prev,.ant-pagination-prev,.layui-laypage-prev",
                    "next": ".btn-next,.ant-pagination-next,.layui-laypage-next"}[direction]
        buttons = pager.find_elements(By.CSS_SELECTOR, selector)
        if len(buttons) != 1:
            raise DeleteStopped("分页结构不明确，扫描不完整。")
        button = buttons[0]
        if not button.is_enabled() or shared.is_disabled(button):
            return False
        self.click_once(button)
        try:
            WebDriverWait(self.driver, self.timeout, poll_frequency=0.2,
                ignored_exceptions=(StaleElementReferenceException,)).until(
                    lambda _: (current := self.read_page_once()) is not None and current[2][1] != old[1])
            self.page()
        except TimeoutException as exc:
            raise DeleteStopped("翻页未得到可核验的新页面，扫描不完整。") from exc
        return True

    def first_page(self):
        for _ in range(MAX_SCAN_PAGES):
            if not self.move("prev"):
                return
        raise DeleteStopped("无法确认首页，扫描停止。")

    def scan(self, ids):
        """始终从首页完整扫描，避免行移位和跨页漏项。"""
        self.require_no_dialog()
        self.first_page()
        found = {value: [] for value in ids}
        seen = set()
        for number in range(1, MAX_SCAN_PAGES + 1):
            rows, _, signature = self.page()
            if signature in seen:
                raise DeleteStopped("分页内容重复，无法确认完整范围。")
            seen.add(signature)
            for order, _ in rows:
                if order.order_id in found:
                    found[order.order_id].append(Order(order.order_id, order.columns, order.cells,
                                                      order.actionable, number))
            if not self.move("next"):
                return found
        raise OrderListStopped("超过安全扫描页数，无法确认完整范围。")

    def click_once(self, button):
        # 业务操作只点击一次，抛错后可能已被服务器受理。
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'})", button)
        button.click()

    def alert(self):
        try:
            return self.driver.switch_to.alert
        except NoAlertPresentException:
            return None

    def modals(self):
        found = []
        for path in self.contexts():
            items = {e.id: e for e in shared.visible_modal_containers(self.driver)}
            items.update({e.id: e for e in shared.find_visible_elements(self.driver, By.CSS_SELECTOR, "[role='dialog']")})
            # 原有定位包含弹窗子节点；只保留最外层可见容器。
            for modal in items.values():
                if not any(other.id != modal.id and self.driver.execute_script(
                    "return arguments[0].contains(arguments[1])", other, modal) for other in items.values()):
                    found.append((path, modal))
        return found

    def require_no_dialog(self):
        if self.alert() is not None or self.modals():
            raise DeleteStopped("存在未处理的弹窗，请人工关闭并重新预览。")

    def row_action(self, approved):
        """回到已核对的页，重新定位同一条记录的唯一行操作。"""
        self.guard()
        self.require_no_dialog()
        self.first_page()
        for _ in range(1, approved.page):
            if not self.move("next"):
                raise OrderListStopped("目标订单的分页已改变，请重新预览。")
        rows, _, _ = self.page()
        matches = [(o, buttons) for o, buttons in rows if o.order_id == approved.order_id]
        if len(matches) != 1 or matches[0][0].fingerprint != approved.fingerprint or len(matches[0][1]) != 1:
            raise OrderListStopped("点击前目标订单已改变或无法唯一定位。")
        return matches[0][1][0]


class DeleteBrowser(OrderListBrowser):
    """单据删除只处理明确指定的目标，每次确认只点击一次。"""
    row_labels = DELETE_LABELS
    operation = "删除"

    @staticmethod
    def validate_dialog(text, order_id):
        compact = shared.compact_text(text)
        if "删除" not in compact or any(word in compact for word in ("送审", "审核", "审批", "提交", "全部", "批量")):
            raise DeleteStopped("弹窗不是明确的单条删除确认，已停止。")
        mentioned = re.findall(r"(?:单据编号|单据号|订单号|订单编号|采购单号|采购单据编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)", text)
        if mentioned and any(value != order_id for value in mentioned):
            raise DeleteStopped("弹窗中的编号与本次目标不同，已停止。")

    def dialog_actions(self, path, root):
        """只在新出现的弹窗内部查找确认，兼容弹窗内 iframe。"""
        self.enter(path)
        text = root.text
        buttons = [(path, button) for button in self.actions(root, CONFIRM_LABELS)]
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
        contained = {f.id for f in root.find_elements(By.CSS_SELECTOR, "iframe,frame") if f.is_displayed()}
        if len(path) < 3:
            for index, frame in enumerate(frames):
                if frame.id in contained:
                    child_path = path + (index,)
                    self.enter(child_path)
                    child_text, child_buttons = self.dialog_actions(child_path, self.driver.find_element(By.TAG_NAME, "body"))
                    text += "\n" + child_text
                    buttons.extend(child_buttons)
        return text, buttons

    def delete(self, approved, journal):
        button = self.row_action(approved)
        journal("row_delete_intent", order_id=approved.order_id)
        self.click_once(button)

        def new_dialog(_):
            native = self.alert()
            if native is not None:
                return "native", native
            self.guard()
            modals = self.modals()
            if len(modals) > 1:
                raise DeleteStopped("同时出现多个弹窗，未自动确认。")
            return ("modal", modals[0]) if modals else False

        try:
            kind, dialog = WebDriverWait(self.driver, self.timeout, poll_frequency=0.2).until(new_dialog)
            if kind == "native":
                self.validate_dialog(dialog.text, approved.order_id)
                journal("confirm_intent", order_id=approved.order_id)
                dialog.accept()
            else:
                path, root = dialog
                text, buttons = self.dialog_actions(path, root)
                self.validate_dialog(text, approved.order_id)
                if len(buttons) != 1:
                    raise DeleteStopped("弹窗内确认按钮不唯一，未自动确认。")
                self.enter(buttons[0][0])
                journal("confirm_intent", order_id=approved.order_id)
                self.click_once(buttons[0][1])

            def completed(_):
                if self.alert() is not None or self.modals():
                    return False
                current = self.read_page_once()
                for message in shared.find_visible_elements(self.driver, By.CSS_SELECTOR,
                        ".el-message,.el-notification,.ant-message-notice"):
                    if "error" in (message.get_attribute("class") or "") or any(
                            word in message.text for word in ("删除失败", "操作失败", "请求失败", "系统异常", "无权限")):
                        raise DeleteStopped("系统报告操作失败，停止且不重试，请人工复核。")
                return current is not None and all(o.order_id != approved.order_id for o, _ in current[0])

            WebDriverWait(self.driver, self.timeout, poll_frequency=0.2,
                ignored_exceptions=(StaleElementReferenceException,)).until(completed)
            shared.print_toast_message(self.driver)
        except TimeoutException as exc:
            raise DeleteStopped("删除响应未能确认；本条可能已受理，停止且不重试，请人工复核。") from exc


def build_plan(browser, ids):
    found = browser.scan(ids)
    plan = []
    for value in ids:
        matches = found[value]
        if len(matches) > 1:
            plan.append(PlanEntry(value, None, "failed", "编号匹配多条，整批不执行"))
        elif not matches:
            plan.append(PlanEntry(value, None, "skipped", "当前筛选的全部分页中未找到"))
        elif not matches[0].deletable:
            plan.append(PlanEntry(value, matches[0], "skipped", "无可用删除按钮（已送审或不可删除）"))
        else:
            plan.append(PlanEntry(value, matches[0], "pending", "待本人确认错误导入并删除"))
    return plan


def execute_plan(browser, plan, *, confirmed, journal):
    """出现任何不确定结果就停止整批；从不自动重试业务点击。"""
    blocked = not confirmed or any(entry.status == "failed" for entry in plan)
    results = []
    for entry in plan:
        status, reason = entry.status, entry.reason
        if status == "pending":
            if blocked:
                status, reason = "skipped", "未确认执行或本批已停止"
            else:
                try:
                    fresh = browser.scan([entry.order_id])[entry.order_id]
                    if len(fresh) != 1:
                        raise DeleteStopped("执行前编号不再唯一或已消失，请重新预览。")
                    if not fresh[0].deletable:
                        status, reason = "skipped", "执行前已无可用删除按钮"
                    else:
                        if fresh[0].fingerprint != entry.order.fingerprint:
                            raise DeleteStopped("订单内容与预览不同，请重新预览。")
                        browser.delete(fresh[0], journal)
                        if browser.scan([entry.order_id])[entry.order_id]:
                            raise DeleteStopped("完整分页复查仍有目标编号，删除结果不确定。")
                        status, reason = "success", "确认后完整分页复查未再找到该编号"
                except (Exception, KeyboardInterrupt) as exc:
                    status = "failed"
                    reason = str(exc) if isinstance(exc, DeleteStopped) else f"结果不确定（{type(exc).__name__}），请人工复核"
                    blocked = True
        record = dict(order_id=entry.order_id, status=status, reason=reason)
        journal("result", **record)
        results.append(record)
    return results


class RunLog:
    def __init__(self):
        folder = PROJECT / "outputs" / "order-delete"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"run-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.jsonl"
        self.file = self.path.open("x", encoding="utf-8")
        self.completed = set()

    def __call__(self, event, **fields):
        record = dict(time=datetime.now().astimezone().isoformat(), event=event, **fields)
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()
        if event == "result":
            self.completed.add(fields["order_id"])
            print(f"[{fields['status']}] {fields['order_id']}：{fields['reason']}")


def ask_order_ids():
    result = None
    root = tk.Tk()
    root.title("指定错误导入订单")
    body = ttk.Frame(root, padding=18)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="仅填写本人确认导入错误的完整单号，每行一个。\n下一步只读扫描列表，核对范围后还需二次确认。").pack(anchor="w")
    text = tk.Text(body, width=62, height=10)
    text.pack(fill="both", expand=True, pady=10)
    column = ttk.Combobox(body, values=("自动识别唯一编号列",) + ID_COLUMNS, state="readonly", width=26)
    column.current(0)
    column.pack(anchor="w")

    def submit():
        nonlocal result
        try:
            ids = parse_order_ids(text.get("1.0", "end").strip().splitlines())
        except ValueError as exc:
            messagebox.showwarning("请核对编号", str(exc), parent=root)
            return
        result = ids, (column.get() if column.current() else None)
        root.destroy()

    buttons = ttk.Frame(body)
    buttons.pack(pady=(14, 0))
    ttk.Button(buttons, text="登录并预览", command=submit).pack(side="left", padx=8)
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="left", padx=8)
    text.focus_set()
    root.mainloop()
    return result


def plan_text(plan):
    lines = []
    for entry in plan:
        lines.append(f"{entry.order_id}  [{entry.status}] {entry.reason}")
        if entry.order:
            lines.append(f"  第 {entry.order.page} 页；" + "；".join(
                f"{key}：{value}" for key, value in zip(entry.order.columns, entry.order.cells)))
    return "\n".join(lines)


def ask_delete_confirmation(plan):
    result = False
    count = sum(e.status == "pending" for e in plan)
    phrase = f"删除 {count} 条"
    root = tk.Tk()
    root.title("二次确认：删除错误导入订单")
    body = ttk.Frame(root, padding=18)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=f"指定 {len(plan)} 条；可删除 {count} 条；跳过 {len(plan) - count} 条。\n"
        "范围：当前筛选条件下的全部分页。请逐条核实单号及内容，删除无法由本程序撤销。",
        foreground="#9a2525").pack(anchor="w")
    preview_area = ttk.Frame(body)
    preview_area.pack(fill="both", expand=True, pady=12)
    preview = tk.Text(preview_area, width=100, height=18, wrap="word")
    scrollbar = ttk.Scrollbar(preview_area, command=preview.yview)
    preview.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    preview.pack(fill="both", expand=True)
    preview.insert("1.0", plan_text(plan))
    preview.configure(state="disabled")
    ttk.Label(body, text=f"确认上述可删除条目均为错误导入后，输入“{phrase}”：").pack(anchor="w")
    value = tk.StringVar(root)
    entry = ttk.Entry(body, textvariable=value, width=30)
    entry.pack(anchor="w", pady=6)

    def confirm():
        nonlocal result
        if value.get() == phrase and count and not any(e.status == "failed" for e in plan):
            result = True
            root.destroy()

    buttons = ttk.Frame(body)
    buttons.pack(pady=(10, 0))
    button = ttk.Button(buttons, text=f"确认删除 {count} 条", command=confirm, state="disabled")
    button.pack(side="left", padx=8)
    ttk.Button(buttons, text="仅预览，取消删除", command=root.destroy).pack(side="left", padx=8)
    value.trace_add("write", lambda *_: button.configure(state="normal" if value.get() == phrase else "disabled"))
    entry.focus_set()
    root.mainloop()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="按明确单号删除错误导入订单，默认只读预览")
    parser.add_argument("--order-id", action="append", help="完整编号，可重复指定；不填则弹窗输入")
    parser.add_argument("--id-column", choices=ID_COLUMNS, help="多个编号列时指定实际采用的列")
    parser.add_argument("--execute", action="store_true", help="预览后允许弹出二次确认；仍需本人输入确认短语")
    parser.add_argument("--via-casmart", action="store_true", help="从商城入口开始登录")
    parser.add_argument("--wait-on-exit", action="store_true", help="桌面任务结束后保留结果窗口，回车关闭")
    args = parser.parse_args(argv)
    log, ids = None, ()
    try:
        if args.order_id is None:
            selection = ask_order_ids()
            if selection is None:
                return 0
            ids, column = selection
            column = args.id_column or column
        else:
            ids, column = parse_order_ids(args.order_id), args.id_column
        start = shared.TARGET_URL if args.via_casmart else shared.load_school_login_url()
        accounts = shared.ask_credentials(login_only=True, direct=not args.via_casmart)
        if accounts is None:
            return 0
        log = RunLog()
        log("requested", ids=ids, id_column=column, mode="execute" if args.execute else "preview")
        driver = shared.init_driver()
        driver.get(start)
        if not shared.auto_login_if_needed(driver, accounts) or not shared.ensure_target_page(driver):
            raise DeleteStopped("未完成登录和列表验证，未执行删除。")
        browser = DeleteBrowser(driver, column)
        print("正在只读扫描当前筛选的全部分页；不自动判断哪些订单导入错误。")
        plan = build_plan(browser, ids)
        print(plan_text(plan))
        log("preview", entries=[dict(order_id=e.order_id, status=e.status, reason=e.reason,
            cells=e.order.cells if e.order else None) for e in plan])
        confirmed = bool(args.execute and any(e.status == "pending" for e in plan)
                         and not any(e.status == "failed" for e in plan) and ask_delete_confirmation(plan))
        log("authorization", confirmed=confirmed)
        results = execute_plan(browser, plan, confirmed=confirmed, journal=log)
        counts = {status: sum(r["status"] == status for r in results) for status in ("success", "skipped", "failed")}
        log("summary", **counts)
        print(f"完成：成功 {counts['success']}；跳过 {counts['skipped']}；失败 {counts['failed']}。")
        return 2 if counts["failed"] else 0
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) if isinstance(exc, (DeleteStopped, ValueError)) else type(exc).__name__
        print(f"已停止：{reason}。若发生过点击，请人工复核后再运行。")
        if log:
            log("batch_stopped", reason=reason)
            for value in ids:
                if value not in log.completed:
                    log("result", order_id=value, status="failed", reason="本批已停止：" + reason)
        return 2
    finally:
        if log:
            print(f"本地记录：{log.path}\n浏览器保持打开，便于复核。")
            log.file.close()
        if args.wait_on_exit:
            try:
                input("按回车关闭任务窗口。")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    sys.exit(main())
