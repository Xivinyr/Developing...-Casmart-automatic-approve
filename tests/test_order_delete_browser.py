"""显式启用时仅访问 127.0.0.1，用真实 Edge 验证合成 DOM、弹窗和分页。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlencode, urlsplit

from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import order_delete as deletion


FIXTURE = Path(__file__).with_name("fixtures") / "order_delete.html"


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            # 关闭合成浏览器时可中断 keep-alive，不属于被测业务失败。
            pass

    def log_message(self, *_):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/list":
            body = FIXTURE.read_text(encoding="utf-8")
        elif url.path == "/frame-root":
            body = '<meta charset="utf-8"><iframe style="width:100%;height:700px" src="/list?' + url.query + '"></iframe>'
        elif url.path == "/confirm":
            body = '<meta charset="utf-8"><p>确认删除此单据？</p><button onclick="parent.finishDelete(parent.pending)">确定</button>'
        else:
            self.send_error(404)
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LocalBrowser(deletion.DeleteBrowser):
    """测试专用本机来源；生产入口没有禁用来源检查的参数。"""
    def guard(self):
        if self.driver.current_url != self.local_url or self.driver.window_handles != [self.window]:
            raise deletion.OrderListStopped("合成页面来源改变")
        self.driver.switch_to.default_content()

    def enter(self, path):
        self.driver.switch_to.default_content()
        for index in path:
            frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
            self.driver.switch_to.frame(frames[index])
            if self.driver.execute_script("return location.origin") != self.local_origin:
                raise deletion.OrderListStopped("合成 iframe 来源改变")


@unittest.skipUnless(os.environ.get("CASMART_DELETE_BROWSER_TEST") == "1", "需显式启用本机 Edge 合成测试")
class DeleteBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        options = webdriver.EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,1000")
        options.add_argument("--disable-background-networking")
        options.add_argument("--no-first-run")
        options.set_capability("unhandledPromptBehavior", "ignore")
        cls.driver = webdriver.Edge(service=Service(os.environ["CASMART_TEST_EDGEDRIVER"]), options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def open(self, mode="normal", *, iframe=False):
        path = "/frame-root" if iframe else "/list"
        self.url = self.base + path + "?" + urlencode({"mode": mode})
        self.driver.get(self.url)
        self.path = (0,) if iframe else ()
        browser = LocalBrowser(self.driver, timeout=3)
        browser.local_url, browser.local_origin = self.url, self.base
        browser.enter(self.path)
        WebDriverWait(self.driver, 2).until(lambda d: d.execute_script("return window.ready === true"))
        self.browser = browser
        self.events = []
        return browser

    def journal(self, event, **fields):
        self.events.append((event, fields))

    def script(self, text):
        self.browser.enter(self.path)
        return self.driver.execute_script(text)

    def execute(self):
        return deletion.execute_plan(self.browser, self.browser.scan(), confirmed=True, journal=self.journal)

    def tearDown(self):
        try:
            self.driver.switch_to.alert.dismiss()
        except NoAlertPresentException:
            pass

    def test_preview_only_even_without_order_number_column(self):
        self.open()
        preview = self.browser.scan()
        self.assertEqual(sum(row.actionable for row in preview), 2)
        deletion.execute_plan(self.browser, preview, confirmed=False, journal=self.journal)
        self.assertEqual(self.script("return events"), [])
        self.assertEqual(self.script("return submits"), 0)

    def test_enabled_buttons_across_delayed_pages_and_after_rows_shift(self):
        self.open("delayed-no-mask")
        self.assertEqual(self.execute(), dict(success=2, skipped=3, failed=0))
        self.assertEqual(self.script("return deletions"), ["ERR-002", "ERR-003"])
        self.assertEqual(self.script("return rows"), ["GOOD-001", "SENT-004", "GOOD-005"])
        self.assertEqual(self.script("return submits"), 0)

    def test_identical_display_rows_can_be_deleted_across_pages(self):
        self.open("duplicate")
        self.assertEqual(self.execute(), dict(success=4, skipped=0, failed=0))
        self.assertEqual(self.script("return deletions"), ["ERR-001", "ERR-002", "ERR-003", "ERR-004"])

    def test_unrelated_or_multiple_dialogs_are_never_confirmed(self):
        for mode in ("wrong-dialog", "multiple-dialogs"):
            with self.subTest(mode=mode):
                self.open(mode)
                self.assertEqual(self.execute(), dict(success=0, skipped=4, failed=1))
                self.assertEqual(self.script("return events"), ["open:ERR-002"])
                self.assertEqual(self.script("return submits"), 0)

    def test_native_confirmation_without_any_number(self):
        self.open("native")
        self.assertEqual(self.execute()["success"], 2)
        self.assertEqual(self.script("return events")[:2], ["open:ERR-002", "confirm:ERR-002"])

    def test_list_and_confirmation_iframes(self):
        self.open("confirm-frame", iframe=True)
        self.assertEqual(self.execute()["success"], 2)
        self.assertEqual(self.script("return deletions"), ["ERR-002", "ERR-003"])

    def test_failure_absent_dialog_or_error_toast_stops_without_retry(self):
        for mode in ("server-failure", "no-dialog", "error-toast"):
            with self.subTest(mode=mode):
                self.open(mode)
                self.assertEqual(self.execute(), dict(success=0, skipped=4, failed=1))
                events = self.script("return events")
                self.assertEqual(events.count("open:ERR-002"), 1)
                self.assertNotIn("open:ERR-003", events)

    def test_changed_filters_invalidate_verification(self):
        self.open("change-filter")
        self.assertEqual(self.execute(), dict(success=0, skipped=4, failed=1))
        self.assertEqual(self.script("return deletions"), ["ERR-002"])

    def test_existing_dialog_blocks_scanning(self):
        self.open()
        self.script("openDelete('ERR-003')")
        with self.assertRaises(deletion.OrderListStopped):
            self.browser.scan()
        self.assertEqual(self.script("return events"), ["open:ERR-003"])

    def test_split_table_is_supported(self):
        self.open("split-table")
        self.assertEqual(self.execute()["success"], 2)
        self.assertEqual(self.script("return submits"), 0)

    def test_incomplete_pagination_or_table_stops_before_any_delete(self):
        for mode in ("stuck-page", "orphan-body", "virtual-list", "unknown-pager"):
            with self.subTest(mode=mode):
                self.open(mode)
                with self.assertRaises(deletion.OrderListStopped):
                    self.browser.scan()
                self.assertEqual(self.script("return events"), [])


if __name__ == "__main__":
    unittest.main()
