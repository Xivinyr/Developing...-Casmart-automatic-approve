"""供应商字段补齐使用合成原表，验证必填表头、重名与原子保存。"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from attachments.core import Cell, DEFAULT, Sheet, supplier_column
from attachments.suppliers import enrich_manifest, with_suppliers


def original_sheet():
    return Sheet("原采购表", {
        (1, 1): Cell("*商品名称"), (1, 11): Cell("*供应商名称"),
        (2, 1): Cell("原表商品名称"), (2, 11): Cell("合成供应商"),
        (2, 22): Cell("分类商品26.5.pdf"),
        (3, 1): Cell("第二商品"), (3, 11): Cell("合成供应商"),
    })


class SupplierTests(unittest.TestCase):
    def test_starred_headers_and_filename_alias_enrich_each_record(self):
        sheet = original_sheet()
        self.assertEqual(supplier_column(sheet, DEFAULT), 11)
        report = {"records": [{"product": "分类商品"}, {"product": "第二商品"}]}
        updated, count = with_suppliers(report, [sheet])
        self.assertEqual(count, 2)
        self.assertTrue(all(record["supplier"] == "合成供应商" for record in updated["records"]))
        self.assertTrue(all("supplier" not in record for record in report["records"]))

    def test_unknown_product_is_left_for_review_and_conflicting_vendor_stops(self):
        report = {"records": [{"product": "未找到"}]}
        updated, count = with_suppliers(report, [original_sheet()])
        self.assertEqual((updated, count), (report, 0))
        report = {"records": [{"product": "分类商品", "supplier": "另一个供应商"}]}
        with self.assertRaisesRegex(ValueError, "不一致"):
            with_suppliers(report, [original_sheet()])

    def test_same_product_from_two_suppliers_needs_existing_assignment(self):
        first = original_sheet()
        second = deepcopy(first)
        second.cells[2, 11] = Cell("第二供应商")
        report = {"records": [{"product": "分类商品"}]}
        with self.assertRaisesRegex(ValueError, "多个供应商"):
            with_suppliers(report, [first, second])
        report["records"][0]["supplier"] = "第二供应商"
        self.assertEqual(with_suppliers(report, [first, second]), (report, 0))

    def test_atomic_write_keeps_backup_and_is_idempotent(self):
        with TemporaryDirectory() as temp, patch("attachments.suppliers.read_xlsx", return_value=[original_sheet()]):
            root = Path(temp)
            path, source = root / "manifest.json", root / "source.xlsx"
            source.touch()
            original = json.dumps({"records": [{"product": "分类商品"}]}).encode()
            path.write_bytes(original)
            report, count = enrich_manifest(path, source)
            self.assertEqual(count, 1)
            self.assertEqual(json.loads(path.read_bytes()), report)
            backups = list(root.glob("manifest.before-suppliers-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(enrich_manifest(path, source)[1], 0)
            self.assertFalse((root / "manifest.supplier.lock").exists())

    def test_failed_replace_leaves_original_and_releases_lock(self):
        with TemporaryDirectory() as temp, patch("attachments.suppliers.read_xlsx", return_value=[original_sheet()]):
            root = Path(temp)
            path, source = root / "manifest.json", root / "source.xlsx"
            source.touch()
            original = json.dumps({"records": [{"product": "分类商品"}]}).encode()
            path.write_bytes(original)
            with patch("attachments.suppliers.os.replace", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    enrich_manifest(path, source)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "manifest.supplier.lock").exists())
            self.assertFalse(list(root.glob(".manifest-suppliers-*.tmp")))

    def test_existing_lock_preserves_manifest_and_other_writer_lock(self):
        with TemporaryDirectory() as temp, patch("attachments.suppliers.read_xlsx", return_value=[original_sheet()]):
            root = Path(temp)
            path, source = root / "manifest.json", root / "source.xlsx"
            source.touch()
            original = json.dumps({"records": [{"product": "分类商品"}]}).encode()
            path.write_bytes(original)
            lock = root / "manifest.supplier.lock"
            lock.write_text("synthetic writer")
            with self.assertRaises(FileExistsError):
                enrich_manifest(path, source)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(lock.read_text(), "synthetic writer")
