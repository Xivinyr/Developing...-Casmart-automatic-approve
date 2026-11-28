"""按钮驱动的删除用虚构采购行验证范围和结果。"""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import unittest
from unittest.mock import Mock, patch

import order_delete as deletion
from order_list import Order, OrderListStopped


def row(product, actionable=True):
    action = "删除" if actionable else "查看"
    return Order(("商品", "操作"), (product, action), product, actionable)


class FakeBrowser:
    def __init__(self, rows):
        self.rows = list(rows)
        self.attempts = []
        self.scan_count = 0
        self.after_delete = None

    def scan(self):
        self.scan_count += 1
        return tuple(replace(item, page=i // 2 + 1, position=i % 2 + 1) for i, item in enumerate(self.rows))

    def delete(self, target, journal):
        self.attempts.append(target.product)
        if self.after_delete is None:
            self.rows.pop((target.page - 1) * 2 + target.position - 1)
        else:
            self.after_delete(target)


class DeleteButtonTests(unittest.TestCase):
    def setUp(self):
        self.browser = FakeBrowser([row("保留", False), row("合成同名"), row("合成同名"), row("已送审", False)])
        self.preview = self.browser.scan()
        self.journal = Mock()

    def execute(self, confirmed=True):
        return deletion.execute_plan(self.browser, self.preview, confirmed=confirmed, journal=self.journal)

    def test_uses_enabled_buttons_and_allows_identical_display_rows(self):
        self.assertEqual(self.execute(), dict(success=2, skipped=2, failed=0))
        self.assertEqual(self.browser.attempts, ["合成同名", "合成同名"])
        self.assertEqual([item.product for item in self.browser.rows], ["保留", "已送审"])

    def test_preview_and_cancel_do_not_click_or_rescan(self):
        self.assertEqual(self.execute(False), dict(success=0, skipped=4, failed=0))
        self.assertEqual(self.browser.scan_count, 1)
        self.assertEqual(self.browser.attempts, [])

    def test_an_additional_eligible_row_invalidates_preview(self):
        self.browser.rows.append(row("刚出现的可删除单据"))
        with self.assertRaisesRegex(OrderListStopped, "预览不同"):
            self.execute()
        self.assertEqual(self.browser.attempts, [])

    def test_button_becoming_available_or_disappearing_invalidates_preview(self):
        for changed in ([row("保留"), *self.browser.rows[1:]], [self.browser.rows[0], row("合成同名", False), *self.browser.rows[2:]]):
            with self.subTest(changed=changed):
                self.browser.rows = changed
                with self.assertRaisesRegex(OrderListStopped, "预览不同"):
                    self.execute()
        self.assertEqual(self.browser.attempts, [])

    def test_no_list_change_or_unexpected_removal_is_failed_and_stops(self):
        for remove_wrong in (False, True):
            with self.subTest(remove_wrong=remove_wrong):
                self.browser = FakeBrowser([row("保留", False), row("目标一"), row("目标二")])
                self.preview = self.browser.scan()
                self.browser.after_delete = lambda _target: self.browser.rows.pop(0) if remove_wrong else None
                self.assertEqual(self.execute(), dict(success=0, skipped=2, failed=1))
                self.assertEqual(self.browser.attempts, ["目标一"])

    def test_click_error_is_never_retried(self):
        def browser_error(_target):
            raise TimeoutError("synthetic")
        self.browser.after_delete = browser_error
        self.assertEqual(self.execute(), dict(success=0, skipped=3, failed=1))
        self.assertEqual(self.browser.attempts, ["合成同名"])

    def test_unrelated_or_bulk_modal_is_rejected(self):
        for text in ("确定送审？", "确认操作？", "删除全部订单？", "批量删除？"):
            with self.subTest(text=text), self.assertRaises(OrderListStopped):
                deletion.DeleteBrowser.validate_dialog(text)
        deletion.DeleteBrowser.validate_dialog("确认删除此单据？")

    def test_default_cli_previews_without_asking_for_confirmation(self):
        browser = Mock()
        browser.scan.return_value = self.preview
        log = Mock()
        with patch.object(deletion.shared, "ask_credentials", return_value={}), \
             patch.object(deletion.shared, "init_driver"), \
             patch.object(deletion.shared, "auto_login_if_needed", return_value=True), \
             patch.object(deletion.shared, "ensure_target_page", return_value=True), \
             patch.object(deletion, "DeleteBrowser", return_value=browser), \
             patch.object(deletion, "RunLog", return_value=log), \
             patch.object(deletion, "preview_choice") as confirm, redirect_stdout(StringIO()):
            self.assertEqual(deletion.main(["--via-casmart"]), 0)
        confirm.assert_not_called()
        browser.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
