"""采购列表的只读扫描、翻页与行内按钮定位。"""
from dataclasses import dataclass, replace
from pathlib import Path
from unicodedata import normalize
from urllib.parse import urlsplit

from selenium.common.exceptions import NoAlertPresentException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import songshen as shared


PROJECT = Path(__file__).resolve().parent
PRODUCT_COLUMNS = frozenset({"商品", "商品名", "商品名称", "采购商品", "采购商品名称", "品名",
                             "物品名称", "货品名称"})
LIST_COLUMNS = PRODUCT_COLUMNS | {"操作", "状态", "单据状态", "采购状态"}
IGNORED_COLUMNS = {"序号", "行号", "#", "单据编号", "单据号", "订单号", "订单编号", "采购单号",
                   "采购单据编号", "线下单据编号", "线下采购单据编号"}
EMPTY_LABELS = {"暂无数据", "暂无相关数据", "无数据", "暂无记录"}
MAX_SCAN_PAGES = 200


def product_key(value):
    """按完整商品名比较，消除显示换行和 Unicode 组合差异。"""
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


# Element UI / Ant Design 可能将表头和表体分开渲染在一个组件中。
READ_TABLES = r"""
const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
const containers = [...document.querySelectorAll('.el-table,.ant-table,table')]
    .filter(e => visible(e) && !e.parentElement.closest('.el-table,.ant-table,table'));
return containers.map(table => {
    const headers = [...table.querySelectorAll('thead th')].filter(visible);
    return {
        headers: headers.map(e => e.innerText.trim()),
        complex: headers.some(e => e.colSpan !== 1 || e.rowSpan !== 1),
        rows: [...table.querySelectorAll('tbody tr')].filter(visible).map(row => ({
            row,
            cells: [...row.children].filter(e => e.tagName === 'TD').map(e => e.innerText.trim()),
            merged: [...row.children].some(e => e.colSpan !== 1 || e.rowSpan !== 1)
        }))
    };
});
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


class OrderListBrowser:
    row_labels = frozenset()
    operation = "操作"
    require_product = False

    def __init__(self, driver, product_column=None, timeout=shared.WAIT_TIME):
        self.driver = driver
        self.product_column = shared.compact_text(product_column)
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

    def table_rows(self, table, headers, product_index):
        rows, rendered = [], []
        for entry in table["rows"]:
            cells = tuple(entry["cells"])
            if len(cells) == 1 and shared.compact_text(cells[0]) in EMPTY_LABELS:
                continue
            if entry["merged"] or len(cells) != len(headers):
                raise OrderListStopped("表头与订单行不能一一对应。")
            buttons = self.actions(entry["row"], self.row_labels)
            if len(buttons) > 1:
                raise OrderListStopped(f"同一订单行存在多个{self.operation}入口。")
            product = cells[product_index] if product_index is not None else ""
            order = Order(headers, cells, product, bool(buttons))
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
            context_tables = d.execute_script(READ_TABLES)
            for table in context_tables:
                headers = tuple(shared.compact_text(label) for label in table["headers"])
                product_labels = {self.product_column} if self.product_column else PRODUCT_COLUMNS
                indices = [i for i, label in enumerate(headers) if label in product_labels]
                if self.require_product and not indices:
                    continue
                known_list = bool(set(headers) & LIST_COLUMNS or indices
                                  or self.scope is not None and self.scope[1:] == (path, headers))
                if not known_list:
                    # 某些列表的列名不同，行内精确操作按钮也能定位列表。
                    if not headers or not any(self.actions(item["row"], self.row_labels) for item in table["rows"]):
                        continue
                if table["complex"] or any(not other["headers"] and other["rows"] for other in context_tables):
                    raise OrderListStopped("表头或表体结构不明确，无法确认完整范围。")
                if self.require_product and len(indices) != 1:
                    raise OrderListStopped("商品名称列不唯一，请指定使用的表头。")
                rows, rendered = self.table_rows(table, headers, indices[0] if len(indices) == 1 else None)
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
            message = "翻页后未看到稳定的新列表。" if changed else "列表未稳定或无法识别单据表格。"
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
