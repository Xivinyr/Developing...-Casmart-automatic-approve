"""用虚构单号和可控时间验证异步列表的送审等待。"""
from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import Mock, call, patch

import songshen as approval


def listing(*rows, actions=(), page="1", next_button=None, table="table"):
    return approval.ApprovalList(table, rows, tuple((row,) for row in rows), tuple(actions), page, next_button)


class ApprovalWaitTests(unittest.TestCase):
    def setUp(self):
        self.driver = Mock(current_url=approval.TARGET_URL)
        self.now = 0
        self.serial = []
        self.output = StringIO()

    def run_with_page(self, read, action):
        def sleep(seconds):
            self.now += seconds

        with patch.object(approval.time, "monotonic", side_effect=lambda: self.now), \
             patch.object(approval.time, "sleep", side_effect=sleep), \
             patch.object(approval, "read_approval_list", side_effect=lambda _d: read(self.now)), \
             patch.object(approval, "APPROVAL_LIST_WAIT_SECONDS", 6), \
             redirect_stdout(self.output):
            return action()

    def test_ready_document_with_late_buttons_keeps_polling_and_checks_stability(self):
        placeholder = listing("SYN-001")
        short_lived = listing("SYN-001", actions=[("SYN-001", True, "old-element")])
        final = listing("SYN-001", actions=[("SYN-001", True, "new-element")])
        def read(now):
            if now < 2:
                return placeholder
            if now < 2.5:
                return short_lived
            return final
        result = self.run_with_page(read, lambda: approval.wait_for_approval_list(self.driver))
        self.assertEqual(result.first_action, ("SYN-001", "new-element"))
        self.assertEqual(self.now, 3.5)

    def test_empty_or_disabled_list_waits_full_window_and_recheck(self):
        for view in (listing(), listing("SYN-001", actions=[("SYN-001", False, "disabled")])):
            with self.subTest(view=view):
                self.now = 0
                self.assertEqual(self.run_with_page(lambda _: view,
                    lambda: approval.wait_for_approval_list(self.driver)), view)
                self.assertEqual(self.now, 6 + approval.APPROVAL_EMPTY_RECHECK_SECONDS)

    def test_button_appearing_during_recheck_still_gets_processed(self):
        no_button = listing("SYN-001")
        loaded = listing("SYN-001", actions=[("SYN-001", True, "late-action")])
        result = self.run_with_page(lambda now: no_button if now < 7 else loaded,
            lambda: approval.wait_for_approval_list(self.driver))
        self.assertEqual(result.first_action, ("SYN-001", "late-action"))
        self.assertEqual(self.now, 8)
        self.assertIn("复查发现可用的送审按钮", self.output.getvalue())

    def test_recheck_starts_again_when_list_refreshes(self):
        initial = listing("SYN-001")
        refreshed = listing("SYN-002")
        def read(now):
            if now < 7:
                return initial
            return None if now < 9 else refreshed
        result = self.run_with_page(read, lambda: approval.wait_for_approval_list(self.driver))
        self.assertEqual(result, refreshed)
        self.assertEqual(self.now, 9 + approval.APPROVAL_EMPTY_RECHECK_SECONDS)

    def test_unfinished_recheck_fails_instead_of_reporting_no_buttons(self):
        initial = listing("SYN-001")
        with self.assertRaisesRegex(approval.ApprovalListError, "复查期间单据列表未恢复稳定"):
            self.run_with_page(lambda now: initial if now < 7 else None,
                lambda: approval.wait_for_approval_list(self.driver))

    def test_unready_list_times_out_without_clicking(self):
        with patch.object(approval, "safe_click") as click:
            with self.assertRaisesRegex(approval.ApprovalListError, "未等到单据列表稳定"):
                self.run_with_page(lambda _: None, lambda: approval.process_current_page(self.driver))
        click.assert_not_called()

    def test_confirmation_refresh_does_not_click_the_old_or_other_button_too_early(self):
        start = listing("SYN-001", "SYN-002", actions=[
            ("SYN-001", True, "first"), ("SYN-002", True, "second"),
        ])
        old_disabled = listing(*start.rows, actions=[
            ("SYN-001", False, "disabled-replacement"), ("SYN-002", True, "second"),
        ])
        updated = listing(*start.rows, actions=[("SYN-002", True, "second")])
        empty = listing(*start.rows)

        def click(_driver, button):
            self.serial.append((button, self.now))
            return True

        def read(now):
            if len(self.serial) == 2:
                return empty
            if len(self.serial) == 1:
                return old_disabled if now < self.serial[0][1] + 2 else updated
            return start

        with patch.object(approval, "safe_click", side_effect=click), \
             patch.object(approval, "confirm_submit_dialog", return_value=True), \
             patch.object(approval, "print_toast_message"):
            count, view = self.run_with_page(read, lambda: approval.process_current_page(self.driver))
        self.assertEqual((count, view), (2, empty))
        self.assertEqual(self.serial, [("first", 1), ("second", 4)])

    def test_confirmation_failure_stops_without_retrying_or_turning_page(self):
        view = listing("SYN-001", actions=[("SYN-001", True, "first")], next_button="next")
        with patch.object(approval, "safe_click", return_value=True) as click, \
             patch.object(approval, "confirm_submit_dialog", return_value=False):
            with self.assertRaises(approval.ApprovalListError):
                self.run_with_page(lambda _: view, lambda: approval.process_current_page(self.driver, view))
        self.assertEqual(click.call_args_list, [call(self.driver, "first")])

    def test_pagination_uses_stable_new_rows_instead_of_an_early_page_marker(self):
        previous = listing("SYN-001", next_button="next")
        unchanged_rows = listing("SYN-001", page="2", actions=[("SYN-001", True, "old-action")])
        updated = listing("SYN-002", page="2", actions=[("SYN-002", True, "new-action")])
        with patch.object(approval, "safe_click", return_value=True) as click:
            result = self.run_with_page(lambda now: unchanged_rows if now < 3 else updated,
                lambda: approval.click_next_page(self.driver, previous))
        self.assertEqual(result.first_action, ("SYN-002", "new-action"))
        self.assertEqual(self.now, 4)
        click.assert_called_once_with(self.driver, "next")

    def test_pagination_timeout_is_distinct_from_last_page(self):
        previous = listing("SYN-001", next_button="next")
        unchanged_rows = listing("SYN-001", page="2")
        with patch.object(approval, "safe_click", return_value=True) as click:
            with self.assertRaisesRegex(approval.ApprovalListError, "翻页后未看到新的稳定单据数据"):
                self.run_with_page(lambda _: unchanged_rows,
                    lambda: approval.click_next_page(self.driver, previous))
        click.assert_called_once_with(self.driver, "next")

    def test_main_processes_two_pages_with_late_pagination_and_old_rows(self):
        def click(_driver, button):
            self.serial.append((button, self.now))
            return True

        def read(now):
            if not self.serial:
                return listing("SYN-001", actions=[("SYN-001", True, "first")], next_button="next")
            if len(self.serial) == 1:
                # 第一次没有送审按钮时分页也未就绪。复查必须使用最新分页控件。
                next_button = "next" if now >= self.serial[0][1] + 7 else None
                return listing("SYN-001", next_button=next_button)
            if len(self.serial) == 2:
                if now < self.serial[1][1] + 2:
                    return listing("SYN-001", page="2", actions=[("SYN-001", True, "first")])
                return listing("SYN-002", page="2", actions=[("SYN-002", True, "second")])
            return listing("SYN-002", page="2")

        with patch.object(approval, "ask_credentials", return_value={}), \
             patch.object(approval, "init_driver", return_value=self.driver), \
             patch.object(approval, "auto_login_if_needed", return_value=True), \
             patch.object(approval, "ensure_target_page", return_value=True), \
             patch.object(approval, "safe_click", side_effect=click), \
             patch.object(approval, "confirm_submit_dialog", return_value=True), \
             patch.object(approval, "print_toast_message"), patch("builtins.input", return_value="") as close:
            code = self.run_with_page(read, lambda: approval.main(["--via-casmart", "--wait-on-exit"]))
        self.assertEqual(code, 0)
        self.assertEqual([button for button, _ in self.serial], ["first", "next", "second"])
        self.assertGreaterEqual(self.serial[2][1] - self.serial[1][1], 3)
        self.assertIn("本次检查 2 页，共触发送审 2 条", self.output.getvalue())
        close.assert_called_once()

    def test_desktop_console_waits_on_startup_error_too(self):
        with patch.object(approval, "ask_credentials", return_value={}), \
             patch.object(approval, "init_driver", side_effect=OSError("synthetic")), \
             patch("builtins.input", return_value="") as close, redirect_stdout(self.output):
            code = approval.main(["--via-casmart", "--wait-on-exit"])
        self.assertEqual(code, 2)
        self.assertIn("程序异常终止：OSError", self.output.getvalue())
        close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
