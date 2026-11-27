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

    def run_with_page(self, read, action):
        def sleep(seconds):
            self.now += seconds

        with patch.object(approval.time, "monotonic", side_effect=lambda: self.now), \
             patch.object(approval.time, "sleep", side_effect=sleep), \
             patch.object(approval, "read_approval_list", side_effect=lambda _d: read(self.now)), \
             patch.object(approval, "APPROVAL_LIST_WAIT_SECONDS", 6), \
             redirect_stdout(StringIO()):
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

    def test_definite_empty_or_no_button_list_waits_full_window(self):
        for view in (listing(), listing("SYN-001", actions=[("SYN-001", False, "disabled")])):
            with self.subTest(view=view):
                self.now = 0
                self.assertEqual(self.run_with_page(lambda _: view,
                    lambda: approval.wait_for_approval_list(self.driver)), view)
                self.assertEqual(self.now, 6)

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


if __name__ == "__main__":
    unittest.main()
