"""签收匹配仅使用分类器生成的合成材料和商品。"""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from attachments.core import Asset, Cell, Sheet, classify
from order_list import Order
import order_receipt as receipt
from receipt_materials import ReceiptStopped, accepts_file, build_plan, load_manifest


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRAAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\nSynthetic fixture\n%%EOF\n"
PRODUCT = "合成商品"
OTHER = "另一个商品"


def synthetic_manifest(folder, second=OTHER):
    cells = {}
    for row, name in ((2, PRODUCT), (3, second)):
        for col in (20, 21, 22, 23):
            cells[row, col] = Cell(name + ".pdf" if col == 22 else "",
                [Asset("fixture", PDF if col == 22 else PNG)])
    root, report = classify([Sheet("合成工作表", cells)], Path(folder))
    return root / "manifest.json", report


def web_row(product, actionable=True):
    return Order(("商品名称", "操作"), (product, "签收" if actionable else "查看"), product, actionable, 1, 1)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path, self.report = synthetic_manifest(self.root)
        self.plan = build_plan(self.path, self.report, PRODUCT)

    def test_exact_name_maps_to_only_its_real_record_folder(self):
        path, report = load_manifest(self.path.parent)
        plan = build_plan(path, report, "  合成商品\n")
        self.assertEqual(plan.product, PRODUCT)
        self.assertEqual(plan.folder.name, "row-000002-合成商品")
        self.assertEqual({f.category for f in plan.files}, {"订单截图", "支付记录", "发票", "开箱实物图"})
        self.assertTrue(all(f.path.parent == plan.folder for f in plan.files))
        with self.assertRaisesRegex(ReceiptStopped, "没有同名"):
            build_plan(path, report, "合成")

    def test_folder_and_manifest_name_must_agree(self):
        report = deepcopy(self.report)
        report["records"][0]["product"] = "错误的文件夹名"
        with self.assertRaisesRegex(ReceiptStopped, "附件路径"):
            build_plan(self.path, report, "错误的文件夹名")

    def test_duplicate_names_are_not_matched_and_unique_products_still_ready(self):
        ready, skipped = receipt.match_receipts(
            [web_row(PRODUCT), web_row(PRODUCT, False), web_row(OTHER)], self.path, self.report)
        self.assertEqual([plan.product for _, plan in ready], [OTHER])
        self.assertIn("同名", skipped[0][1])
        path, report = synthetic_manifest(self.root, second=PRODUCT)
        report["records"][1]["missing"] = ["发票"]
        with self.assertRaisesRegex(ReceiptStopped, "同名"):
            build_plan(path, report, PRODUCT)

    def test_missing_attachments_or_selected_record_error_is_rejected(self):
        self.report["errors"] = [{"sheet_index": 1, "row": 3, "code": "fixture"}]
        self.assertEqual(build_plan(self.path, self.report, PRODUCT), self.plan)
        self.report["errors"][0]["row"] = 2
        with self.assertRaisesRegex(ReceiptStopped, "错误"):
            build_plan(self.path, self.report, PRODUCT)
        self.report["errors"].clear()
        self.report["records"][0]["missing"] = ["发票"]
        with self.assertRaisesRegex(ReceiptStopped, "缺失"):
            build_plan(self.path, self.report, PRODUCT)

    def test_another_products_file_and_traversal_are_rejected(self):
        for ref in (self.report["records"][1]["files"][0]["path"], r"..\outside.png", r"C:\outside.png"):
            with self.subTest(ref=ref):
                report = deepcopy(self.report)
                report["records"][0]["files"][0]["path"] = ref
                with self.assertRaisesRegex(ReceiptStopped, "附件路径"):
                    build_plan(self.path, report, PRODUCT)

    def test_recheck_detects_replacement_even_if_extension_and_size_match(self):
        asset = self.plan.files[0]
        content = asset.path.read_bytes()
        asset.path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        with self.assertRaisesRegex(ReceiptStopped, "内容"):
            asset.verify()

    def test_upload_fields_match_categories_and_actual_types(self):
        invoice = next(file for file in self.plan.files if file.category == "发票")
        self.assertFalse(accepts_file("image/*", invoice))
        self.assertTrue(accepts_file("image/png, application/pdf", invoice))
        slots = [dict(category=f.category, accept=".pdf" if f.category == "发票" else "image/*",
                      multiple=False, targetOK=True) for f in reversed(self.plan.files)]
        groups = receipt.upload_groups(slots, self.plan.files)
        self.assertTrue(all(slot["category"] == files[0].category for slot, files in groups))
        slots[0]["category"] = "未标明上传类型"
        with self.assertRaisesRegex(ReceiptStopped, "分类不明确"):
            receipt.upload_groups(slots, self.plan.files)

    def test_form_only_accepts_explicit_exact_matching_product(self):
        for text in ("签收\n商品名称：合成商品\n材料", "签收\n采购商品\n合成商品\n材料"):
            self.assertTrue(receipt.product_from_form(dict(products=[], text=text), PRODUCT))
        self.assertFalse(receipt.product_from_form(dict(products=[], text="备注 合成商品"), PRODUCT))
        for values in (["合成商品替代"], [PRODUCT, OTHER]):
            with self.assertRaisesRegex(ReceiptStopped, "不一致"):
                receipt.product_from_form(dict(products=values, text=""), PRODUCT)

    def test_upload_feedback_requires_each_exact_filename_and_success_status(self):
        done = [dict(name=name, status="success") for name in self.plan.names]
        self.assertFalse(receipt.uploads_complete(done[:-1], self.plan.names))
        self.assertTrue(receipt.uploads_complete(done, self.plan.names))
        done[-1]["status"] = "uploading"
        self.assertFalse(receipt.uploads_complete(done, self.plan.names))
        done[-1]["status"] = "error"
        with self.assertRaisesRegex(ReceiptStopped, "上传失败"):
            receipt.uploads_complete(done, self.plan.names)

    def test_lookup_rejects_new_duplicate_after_preview(self):
        browser = receipt.ReceiptBrowser(Mock(current_window_handle="synthetic"))
        order = web_row(PRODUCT)
        with patch.object(browser, "scan", return_value=(order, order)):
            with self.assertRaisesRegex(ReceiptStopped, "唯一"):
                browser.find_product(PRODUCT, previous=order)

    def test_cancel_before_upload_and_after_upload_is_respected(self):
        for consent, upload_count in (([False], 0), ([True, False], 1)):
            with self.subTest(consent=consent), redirect_stdout(StringIO()):
                browser = Mock()
                order = web_row(PRODUCT)
                browser.find_product.return_value = order
                receipt.run_receipt(browser, order, self.plan, confirm_action=Mock(side_effect=consent))
                self.assertEqual(browser.upload.call_count, upload_count)
                browser.finish.assert_not_called()

    def test_cli_preflight_accepts_folder_and_does_not_start_browser(self):
        with patch.object(receipt.shared, "init_driver") as launch, redirect_stdout(StringIO()):
            code = receipt.main(["--folder", str(self.path.parent)])
        self.assertEqual(code, 0)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
