"""仅用虚构编号及模拟浏览器，验证删除授权边界和不确定结果的停止行为。"""
from contextlib import redirect_stdout
from io import StringIO
from unittest import TestCase, main
from unittest.mock import Mock, patch

import order_delete as deletion


def order(value, *, deletable=True, page=1, product="合成商品"):
    return deletion.Order(value, ("单据编号", "商品", "操作"),
        (value, product, "删除" if deletable else "查看"), deletable, page)


class FakeBrowser:
    def __init__(self, orders):
        self.orders = orders.copy()
        self.deleted = []
        self.scan_count = 0
        self.on_delete = None

    def scan(self, ids):
        self.scan_count += 1
        return {value: self.orders.get(value, []).copy() for value in ids}

    def delete(self, target, journal):
        self.deleted.append(target.order_id)
        if self.on_delete:
            self.on_delete(target)
        else:
            self.orders.pop(target.order_id)


class DeletePlanTests(TestCase):
    def setUp(self):
        self.browser = FakeBrowser({"ERR-001": [order("ERR-001")], "ERR-002": [order("ERR-002", page=2)],
            "SENT-003": [order("SENT-003", deletable=False)], "GOOD-004": [order("GOOD-004")]})
        self.ids = ("ERR-001", "ERR-002", "SENT-003", "MISSING-005")
        self.plan = deletion.build_plan(self.browser, self.ids)
        self.events = []
        self.journal = lambda event, **fields: self.events.append((event, fields))

    def run_plan(self, confirmed=True):
        return deletion.execute_plan(self.browser, self.plan, confirmed=confirmed, journal=self.journal)

    def test_exact_ids_only_and_skips_are_explicit(self):
        self.assertEqual([e.status for e in self.plan], ["pending", "pending", "skipped", "skipped"])
        results = self.run_plan()
        self.assertEqual(self.browser.deleted, ["ERR-001", "ERR-002"])
        self.assertIn("GOOD-004", self.browser.orders)
        self.assertIn("SENT-003", self.browser.orders)
        self.assertEqual([r["status"] for r in results], ["success", "success", "skipped", "skipped"])
        self.assertEqual(len(self.events), 4)

    def test_no_confirmation_means_no_further_browser_actions(self):
        initial = self.browser.scan_count
        self.assertTrue(all(r["status"] == "skipped" for r in self.run_plan(False)))
        self.assertEqual(self.browser.scan_count, initial)
        self.assertEqual(self.browser.deleted, [])

    def test_duplicate_number_blocks_entire_batch(self):
        self.browser.orders["ERR-002"].append(order("ERR-002", page=3))
        self.plan = deletion.build_plan(self.browser, self.ids)
        result = self.run_plan()
        self.assertEqual(result[1]["status"], "failed")
        self.assertEqual(self.browser.deleted, [])

    def test_order_changed_after_preview_stops_remaining_batch(self):
        self.browser.orders["ERR-001"] = [order("ERR-001", product="已更正的正常商品")]
        result = self.run_plan()
        self.assertEqual([r["status"] for r in result], ["failed", "skipped", "skipped", "skipped"])
        self.assertEqual(self.browser.deleted, [])

    def test_order_becomes_undeletable_between_preview_and_execution(self):
        self.browser.orders["ERR-001"] = [order("ERR-001", deletable=False)]
        self.assertEqual(self.run_plan()[0]["status"], "skipped")
        self.assertEqual(self.browser.deleted, ["ERR-002"])

    def test_missing_or_duplicate_before_click_stops(self):
        for fresh in ([], [order("ERR-001"), order("ERR-001", page=2)]):
            with self.subTest(fresh=fresh):
                self.browser.orders["ERR-001"] = fresh
                self.assertEqual(self.run_plan()[0]["status"], "failed")
                self.assertEqual(self.browser.deleted, [])

    def test_exception_after_business_click_is_never_retried(self):
        def uncertain(_):
            raise TimeoutError("synthetic only")
        self.browser.on_delete = uncertain
        result = self.run_plan()
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(result[1]["status"], "skipped")
        self.assertEqual(self.browser.deleted, ["ERR-001"])

    def test_interrupt_after_click_is_recorded_without_retry(self):
        self.browser.on_delete = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.run_plan()[0]["status"], "failed")
        self.assertEqual(self.browser.deleted, ["ERR-001"])

    def test_row_remaining_after_confirmation_is_not_success(self):
        self.browser.on_delete = lambda _: None
        result = self.run_plan()
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(result[1]["status"], "skipped")
        self.assertEqual(self.browser.deleted, ["ERR-001"])

    def test_incomplete_verification_stops_batch(self):
        original_scan = self.browser.scan
        def scan(ids):
            if self.browser.deleted:
                raise deletion.DeleteStopped("合成翻页失败")
            return original_scan(ids)
        self.browser.scan = scan
        self.assertEqual(self.run_plan()[0]["status"], "failed")
        self.assertEqual(self.browser.deleted, ["ERR-001"])


class DeleteInputTests(TestCase):
    def test_no_empty_wildcard_names_row_indices_or_duplicate_ids(self):
        for values in ([], [""], ["ERR-*"], ["商品"], ["1"], ["ERR-001", "ERR-001"], ["x"] * 101):
            with self.subTest(values=values), self.assertRaises(ValueError):
                deletion.parse_order_ids(values)
        self.assertEqual(deletion.parse_order_ids(["  ERR-001 ", "20261127000002"]),
                         ("ERR-001", "20261127000002"))

    def test_unrelated_or_mismatched_dialog_never_confirms(self):
        for text in ("确定送审？", "确认操作？", "删除全部订单？", "批量删除？", "删除订单号：GOOD-001？",
                     "该单据已送审，确定删除？"):
            with self.subTest(text=text), self.assertRaises(deletion.DeleteStopped):
                deletion.DeleteBrowser.validate_dialog(text, "ERR-001")
        for text in ("确认删除此单据？", "确认删除订单号：ERR-001？"):
            deletion.DeleteBrowser.validate_dialog(text, "ERR-001")

    def test_session_origin_path_and_window_are_rechecked(self):
        driver = Mock(current_window_handle="main", window_handles=["main"], current_url=deletion.shared.TARGET_URL)
        browser = deletion.DeleteBrowser(driver)
        with patch.object(deletion.shared, "is_authenticated_casmart", return_value=True):
            browser.guard()
            for url in ("http://member.casmart.com.cn" + deletion.shared.TARGET_PATH,
                        "https://member.casmart.com.cn.evil.test" + deletion.shared.TARGET_PATH,
                        "https://member.casmart.com.cn/other", "https://user@member.casmart.com.cn" + deletion.shared.TARGET_PATH):
                driver.current_url = url
                with self.subTest(url=url), self.assertRaises(deletion.DeleteStopped):
                    browser.guard()
            driver.current_url = deletion.shared.TARGET_URL
            driver.window_handles = ["main", "other"]
            with self.assertRaises(deletion.DeleteStopped):
                browser.guard()

    def run_main(self, browser, args, confirm=False, login_ok=True):
        journal = Mock(completed=set())
        with patch.object(deletion.shared, "load_school_login_url", return_value="https://synthetic.invalid"), \
             patch.object(deletion.shared, "ask_credentials", return_value={}), \
             patch.object(deletion.shared, "init_driver"), \
             patch.object(deletion.shared, "auto_login_if_needed", return_value=login_ok), \
             patch.object(deletion.shared, "ensure_target_page", return_value=True), \
             patch.object(deletion, "DeleteBrowser", return_value=browser), \
             patch.object(deletion, "RunLog", return_value=journal), \
             patch.object(deletion, "ask_delete_confirmation", return_value=confirm) as dialog, redirect_stdout(StringIO()):
            code = deletion.main(["--order-id", "ERR-001", *args])
        return code, dialog, journal

    def test_default_preview_and_cancelled_execute_never_delete(self):
        for args in ([], ["--execute"]):
            with self.subTest(args=args):
                browser = FakeBrowser({"ERR-001": [order("ERR-001")]})
                code, dialog, _ = self.run_main(browser, args)
                self.assertEqual(code, 0)
                self.assertEqual(dialog.call_count, int(bool(args)))
                self.assertEqual(browser.deleted, [])
                self.assertEqual(browser.scan_count, 1)

    def test_login_failure_records_requested_ids_without_scanning(self):
        browser = FakeBrowser({"ERR-001": [order("ERR-001")]})
        code, dialog, journal = self.run_main(browser, ["--execute"], login_ok=False)
        self.assertEqual(code, 2)
        dialog.assert_not_called()
        self.assertEqual(browser.scan_count, 0)
        self.assertTrue(any(c.args == ("result",) and c.kwargs.get("status") == "failed"
                            and c.kwargs.get("order_id") == "ERR-001" for c in journal.call_args_list))


if __name__ == "__main__":
    main()
