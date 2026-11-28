"""预览当前筛选范围，按可用的行内删除按钮逐条处理。"""
import argparse
from collections import Counter
from datetime import datetime
import json
import sys
from uuid import uuid4

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import songshen as shared
from order_list import OrderListBrowser, OrderListStopped, PROJECT


DELETE_LABELS = {"删除", "删除订单", "删除单据"}
CONFIRM_LABELS = {"确定", "确认", "确定删除", "确认删除", "删除"}


class DeleteBrowser(OrderListBrowser):
    row_labels = DELETE_LABELS
    operation = "删除"

    @staticmethod
    def validate_dialog(text):
        compact = shared.compact_text(text)
        if "删除" not in compact or any(word in compact for word in ("送审", "审核", "审批", "提交", "全部", "批量")):
            raise OrderListStopped("弹窗不是明确的单条删除确认，已停止。")

    def dialog_actions(self, path, root):
        """只从新弹窗内部定位确认按钮，兼容弹窗内的同源 iframe。"""
        self.enter(path)
        text = root.text
        buttons = [(path, button) for button in self.actions(root, CONFIRM_LABELS)]
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
        contained = {frame.id for frame in root.find_elements(By.CSS_SELECTOR, "iframe,frame") if frame.is_displayed()}
        for index, frame in enumerate(frames):
            if len(path) < 3 and frame.id in contained:
                child = path + (index,)
                self.enter(child)
                child_text, child_buttons = self.dialog_actions(child, self.driver.find_element(By.TAG_NAME, "body"))
                text += "\n" + child_text
                buttons.extend(child_buttons)
        return text, buttons

    def delete(self, target, journal):
        button, before = self.row_action(target)
        fields = position(target)
        journal("row_delete_intent", **fields)
        self.click_once(button)

        def new_dialog(_):
            native = self.alert()
            if native is not None:
                return "native", native
            self.guard()
            modals = self.modals()
            if len(modals) > 1:
                raise OrderListStopped("同时出现多个弹窗，未自动确认。")
            return ("modal", modals[0]) if modals else False

        wait = WebDriverWait(self.driver, self.timeout, poll_frequency=0.2,
                             ignored_exceptions=(StaleElementReferenceException,))
        try:
            kind, dialog = wait.until(new_dialog)
            if kind == "native":
                self.validate_dialog(dialog.text)
                journal("confirm_intent", **fields)
                dialog.accept()
            else:
                path, root = dialog
                text, buttons = self.dialog_actions(path, root)
                self.validate_dialog(text)
                if len(buttons) != 1:
                    raise OrderListStopped("弹窗内确认按钮不唯一，未自动确认。")
                self.enter(buttons[0][0])
                journal("confirm_intent", **fields)
                self.click_once(buttons[0][1])

            def refreshed(_):
                if self.alert() is not None or self.modals():
                    return False
                current = self.read_page_once()
                for message in shared.find_visible_elements(self.driver, By.CSS_SELECTOR,
                        ".el-message,.el-notification,.ant-message-notice"):
                    if "error" in (message.get_attribute("class") or "") or any(
                            word in message.text for word in ("删除失败", "操作失败", "请求失败", "系统异常", "无权限")):
                        raise OrderListStopped("系统提示删除失败，请人工核对。")
                return current is not None and (current.content != before.content or current.rendered != before.rendered)

            wait.until(refreshed)
        except TimeoutException as exc:
            raise OrderListStopped("未确认删除后的列表刷新。可能已受理，请核对页面，勿直接重试。") from exc


def position(row):
    return dict(page=row.page, row=row.position, product=row.product)


def preview_text(rows):
    lines = [f"第 {row.page} 页第 {row.position} 行  " + " ｜ ".join(
        f"{name} {value}" for name, value in row.details if name != "操作" and value)
        for row in rows if row.actionable]
    return "\n".join(lines)


def inventory(rows):
    return Counter(row.fingerprint for row in rows)


def execute_plan(browser, preview, *, confirmed, journal):
    count = sum(row.actionable for row in preview)
    result = dict(success=0, skipped=len(preview), failed=0)
    if not confirmed or not count:
        return result

    current = browser.scan()
    if inventory(current) != inventory(preview):
        raise OrderListStopped("列表与刚才预览不同，请重新确认范围。")
    for _ in range(count):
        target = next(row for row in current if row.actionable)
        expected = inventory(current)
        expected[target.fingerprint] -= 1
        try:
            browser.delete(target, journal)
            current = browser.scan()
            # 同名行可以同时在预览中。一条成功删除只应使该可见行的计数减少一。
            if inventory(current) != expected:
                raise OrderListStopped("分页复查未能确认仅少一条预期的可删除行。请核对页面。")
        except (Exception, KeyboardInterrupt) as exc:
            reason = str(exc) if isinstance(exc, OrderListStopped) else type(exc).__name__
            journal("result", status="failed", reason=reason, **position(target))
            result["failed"] += 1
            result["skipped"] -= 1
            break
        journal("result", status="success", reason="确认后列表减少一条预期记录", **position(target))
        result["success"] += 1
        result["skipped"] -= 1
    return result


class RunLog:
    def __init__(self):
        folder = PROJECT / "outputs" / "order-delete"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"run-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.jsonl"
        self.file = self.path.open("x", encoding="utf-8")

    def __call__(self, event, **fields):
        record = dict(time=datetime.now().astimezone().isoformat(), event=event, **fields)
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()
        if event == "result":
            print(f"[{fields['status']}] 第 {fields['page']} 页第 {fields['row']} 行 {fields['product']}：{fields['reason']}")


def preview_choice(count):
    phrase = f"删除 {count} 条"
    try:
        return input(f"输入“{phrase}”确认。如在浏览器调整了筛选，输入 r 刷新预览，回车取消：").strip()
    except EOFError:
        return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="按可用按钮删除当前筛选范围内的订单，默认只读预览")
    parser.add_argument("--execute", action="store_true", help="预览后输入确认语句，再逐条删除")
    parser.add_argument("--via-casmart", action="store_true", help="从商城入口开始登录")
    parser.add_argument("--wait-on-exit", action="store_true", help="桌面任务结束后按回车关闭")
    args = parser.parse_args(argv)
    log = None
    try:
        start = shared.TARGET_URL if args.via_casmart else shared.load_school_login_url()
        accounts = shared.ask_credentials(login_only=True, direct=not args.via_casmart)
        if accounts is None:
            return 0
        log = RunLog()
        driver = shared.init_driver()
        driver.get(start)
        if not shared.auto_login_if_needed(driver, accounts) or not shared.ensure_target_page(driver):
            raise OrderListStopped("未完成登录和列表验证。")
        while True:
            browser = DeleteBrowser(driver)
            print("正在只读扫描当前筛选条件的全部分页，查找可用的行内删除按钮。")
            preview = browser.scan()
            count = sum(row.actionable for row in preview)
            print(f"扫描 {len(preview)} 行，可删除 {count} 行，无可用删除按钮 {len(preview) - count} 行。")
            if count:
                print(preview_text(preview))
            log("preview", scanned=len(preview), candidates=[position(row) for row in preview if row.actionable])
            choice = preview_choice(count) if args.execute else ""
            if choice.lower() != "r":
                break
        confirmed = count > 0 and choice == f"删除 {count} 条"
        log("authorization", confirmed=confirmed)
        result = execute_plan(browser, preview, confirmed=confirmed, journal=log)
        log("summary", **result)
        print(f"完成：成功 {result['success']}，跳过 {result['skipped']}，失败 {result['failed']}。")
        return 2 if result["failed"] else 0
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) if isinstance(exc, OrderListStopped) else type(exc).__name__
        print(f"任务停止：{reason}。若发生过点击，请先核对列表。")
        if log:
            log("stopped", reason=reason)
        return 2
    finally:
        if log:
            print(f"本地记录：{log.path}")
            log.file.close()
        if args.wait_on_exit:
            try:
                input("按回车关闭任务窗口。")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    sys.exit(main())
