"""本地补发票只用虚构文件名、临时目录与合成文件。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import unicodedata

from attachments.core import Asset, Cell, Sheet, classify


PDF = b'%PDF-1.4\nsynthetic invoice\n%%EOF'
INVOICE = {'required': ['发票']}


class LocalInvoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.folder = root / 'invoices'
        self.folder.mkdir()
        self.output = root / 'output'

    def test_exact_unicode_caseless_match_preserves_pdf_bytes(self):
        name = '合成Café.PDF'
        (self.folder / unicodedata.normalize('NFD', name.lower())).write_bytes(PDF)
        sheet = Sheet('样例', {(2, 22): Cell(name)}, [(2, 22, 3, 22)])
        root, report = classify([sheet], self.output, INVOICE, invoice_dir=self.folder)
        record = report['records'][0]
        self.assertEqual(record['rows'], [2, 3])
        self.assertEqual(report['saved'], 1)
        self.assertEqual(record['missing_reasons'], {})
        self.assertEqual((root / record['files'][0]['path']).read_bytes(), PDF)

    def test_names_repeated_across_sheets_are_ambiguous(self):
        name = '合成.pdf'
        (self.folder / name).write_bytes(PDF)
        sheets = [Sheet(title, {(2, 22): Cell(name)}) for title in ('表一', '表二')]
        _, report = classify(sheets, self.output, INVOICE, invoice_dir=self.folder)
        self.assertEqual(report['saved'], 0)
        self.assertEqual([r['missing_reasons']['发票'] for r in report['records']], ['invoice_filename_ambiguous'] * 2)

    def test_duplicate_normalized_filenames_in_folder_are_ambiguous(self):
        name = 'Café.pdf'
        (self.folder / name).write_bytes(PDF)
        alternate = self.folder / unicodedata.normalize('NFD', name)
        if alternate.exists():
            self.skipTest('文件系统本身已合并 Unicode 等价文件名')
        alternate.write_bytes(PDF)
        sheet = Sheet('样例', {(2, 22): Cell(name)})
        _, report = classify([sheet], self.output, INVOICE, invoice_dir=self.folder)
        self.assertEqual(report['saved'], 0)
        self.assertEqual(report['records'][0]['missing_reasons'], {'发票': 'invoice_filename_ambiguous'})

    def test_missing_invalid_nested_and_explicit_sources_are_distinguished(self):
        (self.folder / '错误内容.pdf').write_bytes(b'<html>not a PDF</html>')
        nested = self.folder / 'nested'
        nested.mkdir()
        (nested / '子文件夹.pdf').write_bytes(PDF)
        (self.folder / '外链.pdf').write_bytes(PDF)
        sheet = Sheet('样例', {
            (2, 22): Cell('错误内容.pdf'),
            (3, 22): Cell('子文件夹.pdf'),
            (4, 22): Cell('外链.pdf', [Asset('https://example.test/attachment.pdf')]),
            (5, 22): Cell('../外链.pdf'),
        })
        _, report = classify([sheet], self.output, INVOICE, invoice_dir=self.folder)
        self.assertEqual(report['saved'], 0)
        self.assertEqual([r['missing_reasons']['发票'] for r in report['records']], [
            'invoice_local_invalid', 'invoice_not_in_folder', 'resource_unavailable', 'no_source',
        ])
        self.assertEqual([error['code'] for error in report['errors']], ['online_fetch_required'])

    def test_file_write_failure_does_not_satisfy_required_category(self):
        sheet = Sheet('样例', {(2, 22): Cell(assets=[Asset('fixture', PDF)])})
        path_open = Path.open

        def fail_output(path, mode='r', *args, **kwargs):
            if mode == 'xb':
                raise PermissionError('synthetic write failure')
            return path_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', fail_output):
            _, report = classify([sheet], self.output, INVOICE)
        self.assertEqual(report['saved'], 0)
        self.assertEqual(report['records'][0]['missing'], ['发票'])
        self.assertEqual(report['records'][0]['missing_reasons'], {'发票': 'resource_unavailable'})
        self.assertEqual([error['code'] for error in report['errors']], ['PermissionError'])


if __name__ == '__main__':
    unittest.main()
