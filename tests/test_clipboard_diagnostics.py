"""使用合成表格与模拟读取，不接触 Windows 的真实剪贴板。"""
import ctypes
import unittest
from unittest.mock import Mock, call, patch

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

    def test_automatic_copy_waits_for_a_different_sequence(self):
        with patch.object(clipboard, '_read_windows_html_once', side_effect=[
            ClipboardInputError('clipboard_not_updated'),
            TABLE,
        ]) as read, patch.object(clipboard.time, 'sleep'):
            self.assertEqual(read_windows_html(after_sequence=42), TABLE)
        self.assertEqual(read.call_args_list, [call(42), call(42)])

    def test_stale_clipboard_is_not_read(self):
        api = Mock()
        with patch.object(ctypes, 'windll', api, create=True), patch.object(clipboard.os, 'name', 'nt'), \
             patch.object(clipboard, 'clipboard_sequence', return_value=42), \
             self.assertRaises(ClipboardInputError) as raised:
            clipboard._read_windows_html_once(after_sequence=42)
        self.assertEqual(raised.exception.code, 'clipboard_not_updated')
        api.user32.GetClipboardData.assert_not_called()
        api.user32.CloseClipboard.assert_called_once()

    def test_automatic_copy_rejects_geometry_mismatch(self):
        four_cells = b'<td></td>' * 4
        for raw in (
            b'<table><tr>' + four_cells + b'</tr><tr><td>too narrow</td></tr></table>',
            b'<table><tr>' + four_cells + b'</tr></table>',
            b'<table><tr>' + four_cells + b'<td>extra</td></tr><tr>' + four_cells + b'</tr></table>',
            b'<table><tr><td rowspan="3">outside</td>' + b'<td></td>' * 3 + b'</tr></table>',
        ):
            with self.subTest(raw=raw), self.assertRaises(ClipboardInputError) as raised:
                read_html(raw, expected_end=(3, 23), trim_trailing_empty=True)
            self.assertEqual(raised.exception.code, 'clipboard_range_mismatch')

    def test_trim_preserves_internal_blank_rows_and_image_only_data(self):
        raw = (
            b'<table><tr><td rowspan="4">record</td><td></td><td></td><td></td></tr>'
            b'<tr><td>&nbsp;</td><td> </td><td></td></tr>'
            b'<tr><td></td><td></td><td><img src="data:image/png;base64,eA=="></td></tr>'
            b'<tr><td></td><td> </td><td></td></tr></table>'
        )
        sheet, = read_html(raw, expected_end=(5, 23), trim_trailing_empty=True)
        self.assertEqual({row for row, col in sheet.cells}, {2, 3, 4})
        self.assertEqual(sheet.merges, [(2, 20, 4, 20)])
        self.assertTrue(sheet.cells[(4, 23)].assets)

    def test_automatic_copy_with_only_whitespace_has_no_records(self):
        raw = b'<table><tr><td>&nbsp;</td><td></td><td> </td><td></td></tr></table>'
        with self.assertRaises(ClipboardInputError) as raised:
            read_html(raw, expected_end=(2, 23), trim_trailing_empty=True)
        self.assertEqual(raised.exception.code, 'clipboard_empty_selection')

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
