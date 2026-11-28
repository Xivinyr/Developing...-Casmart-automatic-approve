"""采购列表的只读扫描、翻页与行内按钮定位。"""
from dataclasses import dataclass, replace
from pathlib import Path
from unicodedata import normalize
from urllib.parse import urlsplit

from selenium.common.exceptions import NoAlertPresentException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import songshen as shared
from attachments.core import PRODUCT_HEADERS as PRODUCT_COLUMNS, SUPPLIER_HEADERS


PROJECT = Path(__file__).resolve().parent
LIST_COLUMNS = PRODUCT_COLUMNS | {"操作", "状态", "单据状态", "采购状态"}
IGNORED_COLUMNS = {"序号", "行号", "#", "单据编号", "单据号", "订单号", "订单编号", "采购单号",
                   "采购单据编号", "线下单据编号", "线下采购单据编号"}
EMPTY_LABELS = {"暂无数据", "暂无相关数据", "无数据", "暂无记录"}
MAX_SCAN_PAGES = 200


def product_key(value):
    """完整名称匹配，统一网页显示空白及等价 Unicode 字符。"""
    return " ".join(normalize("NFC", value).split())


class OrderListStopped(RuntimeError):
    """列表或页面状态无法可靠定位。"""


@dataclass(frozen=True)
class Order:
    columns: tuple
    cells: tuple
    product: str
    actionable: bool
    page: int = 0
    position: int = 0
    supplier: str = ""

    @property
    def details(self):
        return tuple((name, value) for name, value in zip(self.columns, self.cells)
                     if name and name not in IGNORED_COLUMNS)

    @property
    def fingerprint(self):
        # 用可见内容及按钮状态检查范围变化，不读取编号作为匹配键。
        return self.details, self.actionable


@dataclass(frozen=True)
class Page:
    rows: tuple
    pager: object
    marker: str
    rendered: tuple

    @property
    def content(self):
        return tuple(order.fingerprint for order, _button in self.rows)


# 同一个表格组件可能用不同的 HTML 表格渲染表头和表体。嵌在单元格里的表格不属于外层。
READ_TABLE_GRID = r"""
const hasLayout = e => e.getClientRects().length > 0;
const visible = e => hasLayout(e) && getComputedStyle(e).visibility !== 'hidden';
const readGrid = (container, allowMerges) => {
    const parts = container.matches('table') ? [container] : [...container.querySelectorAll('table')]
        .filter(part => !container.contains(part.parentElement.closest('table')));
    const headers = parts.flatMap(part => [...part.querySelectorAll('thead tr')]
        .filter(row => row.closest('table') === part).flatMap(row => [...row.cells]
            .filter(cell => (allowMerges || cell.tagName === 'TH') &&
                (allowMerges ? hasLayout(cell) : visible(cell)))));
    const headerLeft = Math.min(...headers.map(cell => cell.getBoundingClientRect().left));
    const headerRight = Math.max(...headers.map(cell => cell.getBoundingClientRect().right));
    const bodies = parts.flatMap(part => [...part.tBodies]);
    const rows = bodies.flatMap((body, group) => [...body.rows]
        .filter(row => allowMerges ? hasLayout(row) : visible(row)).map(row => {
            const cells = [...row.cells].filter(cell =>
                allowMerges ? hasLayout(cell) : cell.tagName === 'TD');
            const single = cells.length === 1 ? cells[0] : null;
            const bounds = single?.getBoundingClientRect();
            return {
                row,
                cells: cells.map(cell => allowMerges && !visible(cell) ? '' : cell.innerText.trim()),
                spans: cells.map(cell => [cell.rowSpan, cell.colSpan]),
                group,
                visible: visible(row),
                merged: cells.some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1),
                // 订单抬头占满整行，网页可能给它设置比明细表头更大的 colspan。
                section: !!(allowMerges && single && single.rowSpan === 1 && single.colSpan > 1 &&
                    bounds.left <= headerLeft + 1 && bounds.right >= headerRight - 1),
            };
        }));
    return {
        headers: headers.map(cell => visible(cell) ? cell.innerText.trim() : ''),
        complex: headers.some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1),
        rows,
    };
};
"""

READ_TABLES = READ_TABLE_GRID + r"""
const containers = [...document.querySelectorAll('.el-table,.ant-table,table')]
    .filter(e => visible(e) && !e.parentElement.closest('.el-table,.ant-table,table'));
return containers.map(table => readGrid(table, arguments[0]));
"""

READ_SCOPE = r"""
const address = new URL(location.href);
for (const key of ['page','pageNo','pageNum','currentPage']) address.searchParams.delete(key);
return [address.href,
    [...document.querySelectorAll('input,select,textarea')]
        .filter(e => !e.closest('table,.el-table,.ant-table,.el-pagination__jump,.ant-pagination-options-quick-jumper') &&
            !['hidden','password','button','submit'].includes(e.type))
        .map(e => [e.id,e.name,e.type,e.value,e.checked]),
    [...document.querySelectorAll('th.ascending,th.descending,th[aria-sort]')]
        .map(e => [e.innerText,e.className,e.getAttribute('aria-sort')])];
"""

LOADING = ".el-loading-mask,.el-loading-spinner,.ant-spin-spinning,[aria-busy='true']"
PAGERS = ".el-pagination,.ant-pagination,.layui-laypage"


def aligned_rows(entries, width, *, allow_merges=False, name_columns=(), allow_sections=False):
    """按表头展开合并格。跨列的名称记为 None，按钮仍归实际所在行。"""
    group_ends = {entry["group"]: i for i, entry in enumerate(entries)}
    pending = {}
    group = None
    for number, entry in enumerate(entries):
        cells = tuple(entry["cells"])
        if (len(cells) == 1 and shared.compact_text(cells[0]) in EMPTY_LABELS
                and entry["spans"][0][0] == 1):
            if pending:
                raise OrderListStopped("合并格跨越了空占位行。")
            continue
        if pending and entry["group"] != group:
            raise OrderListStopped("合并行跨越了多个表体。")
        group = entry["group"]
        if allow_sections and entry["section"]:
            if pending:
                raise OrderListStopped("订单信息行与明细合并格交叠，无法核对。")
            yield entry, tuple(None if index in name_columns else "" for index in range(width))
            continue
        if allow_merges and (entry["merged"] or pending):
            visual = {column: value for column, (value, _last) in pending.items()}
            pending = {column: item for column, item in pending.items() if item[1] > number}
            column = 0
            for text, (vertical, horizontal) in zip(cells, entry["spans"]):
                while column in visual:
                    column += 1
                end = column + horizontal
                if end > width or any(index in visual for index in range(column, end)):
                    raise OrderListStopped(
                        f"表格第 {number + 1} 行第 {column + 1} 列的合并格无法对齐"
                        f"（跨 {horizontal} 列，表头有 {width} 列）。")
                # rowspan=0 覆盖当前表体的剩余行。纵向合并只在当前表体内生效。
                last = group_ends[group]
                if vertical > 0:
                    last = min(last, number + vertical - 1)
                for index in range(column, end):
                    value = text if index == column else ""
                    if horizontal > 1 and index in name_columns:
                        value = None
                    visual[index] = value
                    if last > number:
                        pending[index] = (value, last)
                column = end
            cells = tuple(visual[index] for index in sorted(visual))
        if (entry["merged"] and not allow_merges) or len(cells) != width:
            raise OrderListStopped(
                f"表格第 {number + 1} 行对齐后有 {len(cells)} 列，表头有 {width} 列，无法核对。")
        if entry["visible"]:
            yield entry, cells


class OrderListBrowser:
    row_labels = frozenset()
    operation = "操作"
    require_supplier = False

    def __init__(self, driver, product_column=None, timeout=shared.WAIT_TIME, *, supplier_column=None):
        self.driver = driver
        self.product_column = shared.compact_text(product_column)
        self.supplier_column = shared.compact_text(supplier_column)
        self.timeout = timeout
        self.window = driver.current_window_handle
        self.scope = None

    def guard(self):
        d = self.driver
        if d.current_window_handle != self.window or d.window_handles != [self.window]:
            raise OrderListStopped("窗口范围已改变，停止本次单据操作。")
        d.switch_to.default_content()
        url = urlsplit(d.current_url)
        if (url.scheme != "https" or url.hostname != "member.casmart.com.cn"
                or url.port not in (None, 443) or url.username is not None
                or url.path.rstrip("/") != shared.TARGET_PATH or not shared.is_authenticated_casmart(d)):
            raise OrderListStopped("登录或单据列表地址已改变，停止本次单据操作。")

    def enter(self, path):
        self.driver.switch_to.default_content()
        for index in path:
            frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
            self.driver.switch_to.frame(frames[index])
            if self.driver.execute_script("return location.origin") != "https://member.casmart.com.cn":
                raise OrderListStopped("单据位于无法确认来源的 iframe 中。")

    def contexts(self, path=()):
        self.enter(path)
        yield path
        if len(path) < 3:
            for index, frame in enumerate(self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")):
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

    def table_rows(self, table, headers, product_index, supplier_index):
        rows, rendered = [], []
        name_columns = {index for index in (supplier_index, product_index) if index is not None}
        for entry, cells in aligned_rows(table["rows"], len(headers), allow_merges=self.require_supplier,
                                         name_columns=name_columns, allow_sections=self.require_supplier):
            buttons = self.actions(entry["row"], self.row_labels)
            if len(buttons) > 1:
                raise OrderListStopped(f"同一订单行存在多个{self.operation}入口。")
            product = cells[product_index] if product_index is not None else ""
            supplier = cells[supplier_index] if supplier_index is not None else ""
            if self.require_supplier and buttons:
                if supplier is None:
                    raise OrderListStopped("带签收按钮的行把供应商跨列合并了，无法核对按钮归属。")
                if not self.driver.execute_script(
                        "return arguments[1].closest('tr') === arguments[0];", entry["row"], buttons[0]):
                    raise OrderListStopped("签收按钮位于嵌套表格中，无法核对按钮归属。")
            if self.require_supplier and entry["section"]:
                continue
            order = Order(headers, tuple(value or "" for value in cells), product or "", bool(buttons),
                          supplier=supplier or "")
            rows.append((order, buttons[0] if buttons else None))
            rendered.append(entry["row"].id)
        return tuple(rows), tuple(rendered)

    def read_page_once(self):
        self.guard()
        tables, scopes = [], []
        for path in self.contexts():
            d = self.driver
            if (d.execute_script("return document.readyState") != "complete"
                    or shared.find_visible_elements(d, By.CSS_SELECTOR, LOADING)):
                return None
            if shared.find_visible_elements(d, By.CSS_SELECTOR,
                    ".el-table-v2,.ant-table-tbody-virtual,.rc-virtual-list,.vue-recycle-scroller,"
                    ".virtual-scroller,.virtual-list,[data-virtualized='true']"):
                raise OrderListStopped("检测到虚拟滚动列表，无法确认完整范围。")
            scopes.append((path, d.execute_script(READ_SCOPE)))
            context_tables = d.execute_script(READ_TABLES, self.require_supplier)
            for table in context_tables:
                headers = tuple(shared.compact_text(label) for label in table["headers"])
                product_labels = {self.product_column} if self.product_column else PRODUCT_COLUMNS
                indices = [i for i, label in enumerate(headers) if label in product_labels]
                supplier_labels = {self.supplier_column} if self.supplier_column else SUPPLIER_HEADERS
                vendors = [i for i, label in enumerate(headers) if label in supplier_labels]
                if self.require_supplier and not vendors:
                    continue
                known_list = bool(set(headers) & LIST_COLUMNS or indices or (self.require_supplier and vendors)
                                  or self.scope is not None and self.scope[1:] == (path, headers))
                if not known_list:
                    # 某些列表的列名不同，行内精确操作按钮也能定位列表。
                    if not headers or not any(self.actions(item["row"], self.row_labels) for item in table["rows"]):
                        continue
                if table["complex"] or any(not other["headers"] and other["rows"] for other in context_tables):
                    raise OrderListStopped("表头或表体结构不明确，无法确认完整范围。")
                if self.require_supplier and len(vendors) != 1:
                    raise OrderListStopped("供应商列不唯一，请指定使用的表头。")
                rows, rendered = self.table_rows(table, headers, indices[0] if len(indices) == 1 else None,
                                                 vendors[0] if len(vendors) == 1 else None)
                tables.append((path, headers, rows, rendered))
        if not tables:
            return None
        if len(tables) != 1:
            raise OrderListStopped("存在多个候选单据表，无法唯一定位。")
        path, headers, rows, rendered = tables[0]
        scope = scopes, path, headers
        if self.scope is not None and self.scope != scope:
            raise OrderListStopped("筛选、排序、分页设置或表格已改变，请重新预览。")
        self.enter(path)
        pagers = shared.find_visible_elements(d, By.CSS_SELECTOR, PAGERS)
        if len(pagers) > 1:
            raise OrderListStopped("存在多个分页控件，无法确认扫描范围。")
        pager = pagers[0] if pagers else None
        if pager is None and d.execute_script(r"""
            return [...document.querySelectorAll('button,a,[role="button"]')].some(e =>
                e.getClientRects().length && ['上一页','下一页','上页','下页'].includes(e.innerText.replace(/\s/g,'')));
        """):
            raise OrderListStopped("检测到未支持的分页控件，扫描不完整。")
        marker = ""
        if pager:
            marker = "|".join(e.text for e in pager.find_elements(By.CSS_SELECTOR,
                ".el-pager .active,.el-pager .is-active,.ant-pagination-item-active,.layui-laypage-curr"))
        self.scope = scope
        return Page(rows, pager, marker, rendered)

    def page(self, changed=None):
        previous = None

        def stable(_):
            nonlocal previous
            try:
                current = self.read_page_once()
            except StaleElementReferenceException:
                current = None
            if current is None or changed is not None and not changed(current):
                previous = None
                return False
            key = current.content, current.marker, current.rendered
            ready = key == previous
            previous = key
            return current if ready else False

        try:
            return WebDriverWait(self.driver, self.timeout, poll_frequency=0.2).until(stable)
        except TimeoutException as exc:
            if changed:
                message = "翻页后未看到稳定的新列表。"
            elif self.require_supplier:
                message = "列表未稳定或无法识别供应商列。可按实际表头指定供应商列。"
            else:
                message = "列表未稳定或无法识别单据表格。"
            raise OrderListStopped(message) from exc

    def move(self, direction, page):
        if not page.pager:
            return None
        selector = {"prev": ".btn-prev,.ant-pagination-prev,.layui-laypage-prev",
                    "next": ".btn-next,.ant-pagination-next,.layui-laypage-next"}[direction]
        buttons = page.pager.find_elements(By.CSS_SELECTOR, selector)
        if len(buttons) != 1:
            raise OrderListStopped("分页结构不明确，扫描不完整。")
        button = buttons[0]
        if not button.is_enabled() or shared.is_disabled(button):
            return None
        self.click_once(button)

        def updated(new):
            if page.marker and new.marker:
                if new.marker == page.marker:
                    return False
                # 同名甚至完全相同的下一页仍可翻页，须看到行元素重新渲染。
                return new.content != page.content or new.rendered != page.rendered
            return new.content != page.content

        return self.page(changed=updated)

    def first_page(self):
        page = self.page()
        for _ in range(MAX_SCAN_PAGES):
            previous = self.move("prev", page)
            if previous is None:
                return page
            page = previous
        raise OrderListStopped("无法确认首页，扫描停止。")

    def scan(self):
        """读取当前筛选的全部分页，保留各行按钮状态。"""
        self.guard()
        self.require_no_dialog()
        page = self.first_page()
        rows, seen = [], set()
        for number in range(1, MAX_SCAN_PAGES + 1):
            key = ("marker", page.marker) if page.marker else ("content", page.content)
            if key in seen:
                raise OrderListStopped("分页内容重复，无法确认完整范围。")
            seen.add(key)
            rows.extend(replace(order, page=number, position=position)
                        for position, (order, _button) in enumerate(page.rows, 1))
            next_page = self.move("next", page)
            if next_page is None:
                return tuple(rows)
            page = next_page
        raise OrderListStopped("超过安全扫描页数，无法确认完整范围。")

    def click_once(self, button):
        # 业务点击只发送一次。点击异常后也可能已经由网页受理。
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'})", button)
        button.click()

    def alert(self):
        try:
            return self.driver.switch_to.alert
        except NoAlertPresentException:
            return None

    def modals(self):
        result = []
        for path in self.contexts():
            items = {e.id: e for e in shared.visible_modal_containers(self.driver)}
            items.update({e.id: e for e in shared.find_visible_elements(self.driver, By.CSS_SELECTOR, "[role='dialog']")})
            for modal in items.values():
                if not any(other.id != modal.id and self.driver.execute_script(
                    "return arguments[0].contains(arguments[1])", other, modal) for other in items.values()):
                    result.append((path, modal))
        return result

    def require_no_dialog(self):
        if self.alert() is not None or self.modals():
            raise OrderListStopped("存在未处理的弹窗，请人工关闭并重新预览。")

    def row_action(self, target):
        """使用当前页的行位置找按钮，并重新核对该行的可见内容。"""
        self.guard()
        self.require_no_dialog()
        page = self.first_page()
        for _ in range(1, target.page):
            page = self.move("next", page)
            if page is None:
                raise OrderListStopped("目标行的分页已改变，请重新预览。")
        if not 1 <= target.position <= len(page.rows):
            raise OrderListStopped("目标行在点击前已变化，请重新预览。")
        order, button = page.rows[target.position - 1]
        if not button or order.fingerprint != target.fingerprint:
            raise OrderListStopped("行内容或可用按钮在点击前已变化，请重新预览。")
        return button, page
