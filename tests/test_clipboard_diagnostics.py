"""使用合成表格与模拟读取，不接触 Windows 的真实剪贴板。"""
import unittest
from unittest.mock import patch

from attachments import clipboard
from attachments.clipboard import ClipboardInputError, read_html, read_windows_html


TABLE = b'<table><tr><td>included</td></tr></table>'


class ClipboardDiagnosticsTests(unittest.TestCase):
    def test_retries_when_clipboard_is_busy_or_html_arrives_late(self):
        with patch.object(clipboard, '_read_windows_html_once', side_effect=[
            ClipboardInputError('clipboard_busy'),
            ClipboardInputError('clipboard_has_no_html'),
            TABLE,
        ]), patch.object(clipboard.time, 'sleep') as sleep:
            self.assertEqual(read_windows_html(), TABLE)
        self.assertEqual(sleep.call_count, 2)

    def test_non_html_retry_is_bounded_and_preserves_reason(self):
        with patch.object(clipboard, '_read_windows_html_once', side_effect=ClipboardInputError('clipboard_has_no_html')) as read, \
             patch.object(clipboard.time, 'sleep'):
            with self.assertRaises(ClipboardInputError) as raised:
                read_windows_html()
        self.assertEqual(raised.exception.code, 'clipboard_has_no_html')
        self.assertEqual(read.call_count, 8)

    def test_invalid_offsets_use_explicit_fragment_without_surrounding_tables(self):
        raw = (
            b'StartFragment:0000000020\r\nEndFragment:9999999999\r\n'
            b'<table><tr><td>outside</td></tr></table><!--StartFragment-->'
            + TABLE + b'<!--EndFragment-->'
        )
        sheet, = read_html(raw)
        self.assertEqual([cell.text for cell in sheet.cells.values()], ['included'])

    def test_format_errors_have_distinct_redacted_codes(self):
        for raw, code in (
            (b'StartFragment:00\nEndFragment:9999\n' + TABLE, 'invalid_cf_html'),
            (b'<img alt="sensitive_test_value" src="blob:irrelevant">', 'clipboard_no_table'),
            (b'<table><tr><td colspan="sensitive_test_value"></td></tr></table>', 'clipboard_invalid_span'),
            (b'<table><tr><td><img src="data:;base64,sensitive_test_value"></td></tr></table>', 'clipboard_invalid_attachment'),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ClipboardInputError) as raised:
                    read_html(raw)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('sensitive_test_value', str(raised.exception))
