"""显式启用时仅访问 127.0.0.1，用真实 Edge 验证合成 DOM、弹窗和分页。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlencode, urlsplit

from selenium import webdriver
from selenium.webdriver.edge.service import Service
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
            raise deletion.DeleteStopped("合成页面来源改变")
        self.driver.switch_to.default_content()

    def enter(self, path):
        self.driver.switch_to.default_content()
        for index in path:
            frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
            self.driver.switch_to.frame(frames[index])
            if self.driver.execute_script("return location.origin") != self.local_origin:
                raise deletion.DeleteStopped("合成 iframe 来源改变")


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

    def open(self, mode="normal", *, iframe=False, column=None):
        path = "/frame-root" if iframe else "/list"
        self.url = self.base + path + "?" + urlencode({"mode": mode})
        self.driver.get(self.url)
        self.path = (0,) if iframe else ()
        browser = LocalBrowser(self.driver, column, timeout=2)
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

    def execute(self, ids):
        plan = deletion.build_plan(self.browser, ids)
        return deletion.execute_plan(self.browser, plan, confirmed=True, journal=self.journal)

    def tearDown(self):
        # 不带着未处理的原生 alert 导航到下一条夹具。
        try:
            self.driver.switch_to.alert.dismiss()
        except deletion.NoAlertPresentException:
            pass

    def test_01_preview_does_not_open_or_confirm_any_delete_dialog(self):
        self.open()
        plan = deletion.build_plan(self.browser, ("ERR-002", "SENT-004", "MISSING-006"))
        self.assertEqual([e.status for e in plan], ["pending", "skipped", "skipped"])
        deletion.execute_plan(self.browser, plan, confirmed=False, journal=self.journal)
        self.assertEqual(self.script("return events"), [])
        self.assertEqual(self.script("return submits"), 0)

    def test_02_delete_refresh_pagination_and_only_requested_ids(self):
        self.open("delayed-no-mask")
        results = self.execute(("ERR-002", "ERR-003", "SENT-004", "MISSING-006"))
        self.assertEqual([r["status"] for r in results], ["success", "success", "skipped", "skipped"])
        self.assertEqual(self.script("return deletions"), ["ERR-002", "ERR-003"])
        self.assertEqual(self.script("return rows"), ["GOOD-001", "SENT-004", "GOOD-005"])
        self.assertEqual(self.script("return submits"), 0)

    def test_03_duplicate_across_pages_blocks_whole_batch(self):
        self.open("duplicate")
        result = self.execute(("ERR-003", "ERR-002"))
        self.assertEqual([r["status"] for r in result], ["skipped", "failed"])
        self.assertEqual(self.script("return events"), [])

    def test_04_wrong_dialog_or_id_is_never_confirmed(self):
        for mode in ("wrong-dialog", "wrong-id", "multiple-dialogs"):
            with self.subTest(mode=mode):
                self.open(mode)
                result = self.execute(("ERR-002", "ERR-003"))
                self.assertEqual([r["status"] for r in result], ["failed", "skipped"])
                self.assertEqual(self.script("return events"), ["open:ERR-002"])
                self.assertEqual(self.script("return submits"), 0)

    def test_05_native_confirmation(self):
        self.open("native")
        self.assertEqual(self.execute(("ERR-002",))[0]["status"], "success")
        self.assertEqual(self.script("return events"), ["open:ERR-002", "confirm:ERR-002"])

    def test_06_list_iframe_and_dialog_iframe(self):
        self.open("confirm-frame", iframe=True)
        result = self.execute(("ERR-002",))
        self.assertEqual(result[0]["status"], "success", result)
        self.assertEqual(self.script("return deletions"), ["ERR-002"])

    def test_07_failure_or_absent_dialog_does_not_retry(self):
        for mode in ("server-failure", "no-dialog"):
            with self.subTest(mode=mode):
                self.open(mode)
                result = self.execute(("ERR-002", "ERR-003"))
                self.assertEqual([r["status"] for r in result], ["failed", "skipped"])
                events = self.script("return events")
                self.assertEqual(events.count("open:ERR-002"), 1)
                self.assertNotIn("open:ERR-003", events)

    def test_08_changed_filters_invalidate_verification(self):
        self.open("change-filter")
        result = self.execute(("ERR-002", "ERR-003"))
        self.assertEqual([r["status"] for r in result], ["failed", "skipped"])
        self.assertEqual(self.script("return deletions"), ["ERR-002"])

    def test_09_unknown_or_ambiguous_id_column_and_stuck_pagination_stop(self):
        for mode in ("unknown-header", "two-id-columns", "stuck-page"):
            with self.subTest(mode=mode):
                self.open(mode)
                with self.assertRaises(deletion.DeleteStopped):
                    deletion.build_plan(self.browser, ("ERR-002",))
                self.assertEqual(self.script("return events"), [])

    def test_10_existing_dialog_blocks_preflight(self):
        self.open()
        self.script("openDelete('ERR-003')")
        with self.assertRaises(deletion.DeleteStopped):
            deletion.build_plan(self.browser, ("ERR-002",))
        self.assertEqual(self.script("return events"), ["open:ERR-003"])

    def test_11_split_tables_and_explicit_id_column(self):
        for mode, column in (("split-table", None), ("two-id-columns", "单据编号")):
            with self.subTest(mode=mode):
                self.open(mode, column=column)
                result = self.execute(("ERR-002",))
                self.assertEqual(result[0]["status"], "success", result)
                self.assertEqual(self.script("return submits"), 0)

    def test_12_error_toast_cannot_be_recorded_as_success(self):
        self.open("error-toast")
        result = self.execute(("ERR-002", "ERR-003"))
        self.assertEqual([r["status"] for r in result], ["failed", "skipped"])
        self.assertEqual(self.script("return events"), ["open:ERR-002", "confirm:ERR-002"])

    def test_13_orphan_body_is_not_mistaken_for_empty_list(self):
        self.open("orphan-body")
        with self.assertRaises(deletion.DeleteStopped):
            deletion.build_plan(self.browser, ("ERR-002",))
        self.assertEqual(self.script("return events"), [])

    def test_14_virtual_list_and_unknown_pager_cannot_claim_full_scan(self):
        for mode in ("virtual-list", "unknown-pager"):
            with self.subTest(mode=mode):
                self.open(mode)
                with self.assertRaises(deletion.DeleteStopped):
                    deletion.build_plan(self.browser, ("ERR-002",))
                self.assertEqual(self.script("return events"), [])


if __name__ == "__main__":
    unittest.main()
