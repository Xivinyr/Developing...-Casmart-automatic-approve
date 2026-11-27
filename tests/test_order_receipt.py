"""签收仅使用分类器生成的合成附件、模拟单号和浏览器。"""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from attachments.core import Asset, Cell, Sheet, classify
from order_delete import Order
import order_receipt as receipt
from receipt_materials import ReceiptStopped, accepts_file, build_plan, load_manifest


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRAAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\nSynthetic fixture\n%%EOF\n"
ORDER_ID = "SYN-123"


def synthetic_manifest(folder):
    # 同名的两行各自成组，第二条仅用来检验不会混入其他订单的文件。
    cells = {}
    for row in (2, 3):
        for col in (20, 21, 22, 23):
            cells[row, col] = Cell("合成商品.pdf" if col == 22 else "",
                [Asset("fixture", PDF if col == 22 else PNG)])
    root, report = classify([Sheet("合成工作表", cells)], Path(folder))
    return root / "manifest.json", report


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path, self.report = synthetic_manifest(temp.name)
        self.plan = build_plan(self.path, self.report, 1, 2, ORDER_ID)

    def test_selects_only_explicit_row_even_when_product_name_is_identical(self):
        path, report = load_manifest(self.path)
        plan = build_plan(path, report, 1, 2, ORDER_ID)
        self.assertEqual((plan.product, plan.rows, plan.order_id), ("合成商品", (2,), ORDER_ID))
        self.assertEqual({f.category for f in plan.files}, {"订单截图", "支付记录", "发票", "开箱实物图"})
        self.assertTrue(all("row-000002-合成商品" in f.path.parts for f in plan.files))

    def test_duplicate_record_or_missing_attachments_never_plans(self):
        duplicate = deepcopy(self.report)
        duplicate["records"].append(deepcopy(duplicate["records"][0]))
        with self.assertRaisesRegex(ReceiptStopped, "唯一"):
            build_plan(self.path, duplicate, 1, 2, ORDER_ID)
        missing = deepcopy(self.report)
        missing["records"][0]["missing"] = ["发票"]
        with self.assertRaisesRegex(ReceiptStopped, "缺失"):
            build_plan(self.path, missing, 1, 2, ORDER_ID)

    def test_another_rows_file_and_traversal_paths_are_rejected(self):
        for ref in (self.report["records"][1]["files"][0]["path"], r"..\outside.png", r"C:\outside.png"):
            with self.subTest(ref=ref):
                report = deepcopy(self.report)
                report["records"][0]["files"][0]["path"] = ref
                with self.assertRaisesRegex(ReceiptStopped, "附件路径"):
                    build_plan(self.path, report, 1, 2, ORDER_ID)

    def test_recheck_detects_replacement_even_if_extension_and_size_match(self):
        asset = self.plan.files[0]
        content = asset.path.read_bytes()
        asset.path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        with self.assertRaisesRegex(ReceiptStopped, "内容"):
            asset.verify()

    def test_selected_rows_errors_block_but_other_rows_errors_do_not(self):
        self.report["errors"] = [{"sheet_index": 1, "row": 3, "code": "fixture"}]
        self.assertEqual(build_plan(self.path, self.report, 1, 2, ORDER_ID), self.plan)
        self.report["errors"][0]["row"] = 2
        with self.assertRaisesRegex(ReceiptStopped, "错误"):
            build_plan(self.path, self.report, 1, 2, ORDER_ID)

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
        with self.assertRaisesRegex(ReceiptStopped, "文件类型"):
            receipt.upload_groups([dict(category="", accept="image/*", multiple=True, targetOK=True)], self.plan.files)

    def test_displayed_form_id_must_include_the_complete_selected_number(self):
        self.assertTrue(receipt.identity_from_form("采购签收\n单据号：SYN-123\n商品", "单据编号", ORDER_ID))
        self.assertFalse(receipt.identity_from_form("商品备注 SYN-123", "单据编号", ORDER_ID))
        for text in ("单据编号：SYN-1234", "单据编号：SYN-123\n单据号：SYN-999"):
            with self.assertRaisesRegex(ReceiptStopped, "不一致"):
                receipt.identity_from_form(text, "单据编号", ORDER_ID)

    def test_upload_feedback_requires_each_exact_filename_and_success_status(self):
        done = [dict(name=name, status="success") for name in self.plan.names]
        self.assertFalse(receipt.uploads_complete(done[:-1], self.plan.names))
        self.assertTrue(receipt.uploads_complete(done, self.plan.names))
        done[-1]["status"] = "uploading"
        self.assertFalse(receipt.uploads_complete(done, self.plan.names))
        done[-1]["status"] = "error"
        with self.assertRaisesRegex(ReceiptStopped, "上传失败"):
            receipt.uploads_complete(done, self.plan.names)
        with self.assertRaisesRegex(ReceiptStopped, "无法对应"):
            receipt.uploads_complete([dict(name="other.png", status="success")], self.plan.names)

    def test_lookup_refuses_duplicate_orders_or_mismatched_product(self):
        driver = Mock(current_window_handle="synthetic")
        browser = receipt.ReceiptBrowser(driver)
        order = Order(ORDER_ID, ("单据编号", "商品名称"), (ORDER_ID, "其他商品"), True, 1)
        with patch.object(browser, "scan", return_value={ORDER_ID: [order]}):
            with self.assertRaisesRegex(ReceiptStopped, "名称不同"):
                browser.find_order(self.plan)
        with patch.object(browser, "scan", return_value={ORDER_ID: [order, order]}):
            with self.assertRaisesRegex(ReceiptStopped, "匹配多条"):
                browser.find_order(self.plan)

    def test_read_only_and_upload_cancellation_do_not_open_business_form(self):
        order = Order(ORDER_ID, ("单据编号",), (ORDER_ID,), True, 1)
        for read_only in (True, False):
            with self.subTest(read_only=read_only):
                browser = Mock()
                browser.find_order.return_value = order
                consent = Mock(return_value=False)
                with redirect_stdout(StringIO()):
                    receipt.run_receipt(browser, self.plan, check_only=read_only, confirm_action=consent)
                self.assertEqual(consent.call_count, int(not read_only))
                browser.open_receipt.assert_not_called()
                browser.upload.assert_not_called()
                browser.finish.assert_not_called()

    def test_final_confirmation_is_after_verified_upload_and_can_be_cancelled(self):
        order = Order(ORDER_ID, ("单据编号",), (ORDER_ID,), True, 1)
        browser = Mock()
        browser.find_order.return_value = order
        def consent(action, order_id):
            self.assertEqual(order_id, ORDER_ID)
            if action == "签收":
                browser.upload.assert_called_once_with(self.plan)
                return False
            return True
        with redirect_stdout(StringIO()):
            receipt.run_receipt(browser, self.plan, confirm_action=consent)
        browser.find_order.assert_called_with(self.plan, previous=order)
        browser.open_receipt.assert_called_once_with(order)
        browser.finish.assert_not_called()

    def test_cli_preflight_does_not_start_browser(self):
        with patch.object(receipt.shared, "init_driver") as launch, redirect_stdout(StringIO()):
            code = receipt.main(["--manifest", str(self.path), "--sheet-index", "1", "--row", "2",
                                 "--order-id", ORDER_ID])
        self.assertEqual(code, 0)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
