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
from receipt_materials import ReceiptStopped, accepts_file, build_plan, build_supplier_plan, load_manifest


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRAAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\nSynthetic fixture\n%%EOF\n"
PRODUCT = "合成商品"
OTHER = "另一个商品"
SUPPLIER = "合成供应商甲"
OTHER_SUPPLIER = "合成供应商乙"


def synthetic_manifest(folder, second=OTHER, suppliers=(), *, merges=()):
    cells = {}
    for row, name in ((2, PRODUCT), (3, second)):
        for col in (20, 21, 22, 23):
            # 两种商品共用发票，其余材料由各商品分别提供。
            data = PDF if col == 22 else PNG + bytes([row])
            cells[row, col] = Cell(name + ".pdf" if col == 22 else "",
                [Asset("fixture", data)])
    if suppliers:
        cells[1, 5] = Cell("供应商名称")
        for row, supplier in enumerate(suppliers, start=2):
            cells[row, 5] = Cell(supplier)
    root, report = classify([Sheet("合成工作表", cells, list(merges))], Path(folder))
    return root / "manifest.json", report


def web_row(supplier, actionable=True):
    return Order(("供应商", "操作"), (supplier, "签收" if actionable else "查看"), "", actionable, 1, 1, supplier)


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

    def test_duplicate_supplier_buttons_are_skipped(self):
        ready, skipped = receipt.supplier_choices(
            [web_row(SUPPLIER), web_row(SUPPLIER), web_row(OTHER_SUPPLIER), web_row(OTHER_SUPPLIER, False), web_row("")])
        self.assertEqual([row.supplier for row in ready], [OTHER_SUPPLIER])
        self.assertIn("多个", skipped[0][1])
        self.assertIn("没有", skipped[-1][1])

    def test_untagged_duplicate_products_are_refused(self):
        path, report = synthetic_manifest(self.root, second=PRODUCT)
        report["records"][1]["missing"] = ["发票"]
        with self.assertRaisesRegex(ReceiptStopped, "商品「合成商品」.*同名"):
            build_supplier_plan(path, report, SUPPLIER, [PRODUCT])

    def test_supplier_column_distinguishes_same_named_products(self):
        path, report = synthetic_manifest(self.root, second=PRODUCT, suppliers=(SUPPLIER, OTHER_SUPPLIER))
        self.assertEqual([item["supplier"] for item in report["records"]], [SUPPLIER, OTHER_SUPPLIER])
        plan = build_supplier_plan(path, report, OTHER_SUPPLIER, [PRODUCT])
        self.assertEqual(plan.products[0].folder.name, "row-000003-合成商品")
        self.assertEqual(plan.products[0].supplier, OTHER_SUPPLIER)
        report["records"][1].pop("supplier")
        with self.assertRaisesRegex(ReceiptStopped, "属于其他供应商"):
            build_supplier_plan(path, report, "第三家供应商", [PRODUCT])

    def test_merged_supplier_column_keeps_each_product_folder(self):
        path, report = synthetic_manifest(self.root, suppliers=(SUPPLIER,), merges=((2, 5, 3, 5),))
        plan = build_supplier_plan(path, report, SUPPLIER, [PRODUCT, OTHER])
        self.assertEqual([item.supplier for item in plan.products], [SUPPLIER, SUPPLIER])
        self.assertEqual([item.folder.name for item in plan.products], ["row-000002-合成商品", "row-000003-另一个商品"])

    def test_complete_supplier_group_uploads_shared_invoice_once(self):
        plan = build_supplier_plan(self.path, self.report, SUPPLIER, [PRODUCT, OTHER])
        self.assertEqual([item.product for item in plan.products], [PRODUCT, OTHER])
        self.assertEqual(len(plan.files), 7)
        self.assertEqual(sum(file.category == "发票" for file in plan.files), 1)
        self.assertTrue(all(not item.supplier for item in plan.products))
        self.report["records"][1]["missing"] = ["支付记录"]
        with self.assertRaisesRegex(ReceiptStopped, "商品「另一个商品」.*缺失"):
            build_supplier_plan(self.path, self.report, SUPPLIER, [PRODUCT, OTHER])
        with self.assertRaisesRegex(ReceiptStopped, "签收页有同名商品"):
            build_supplier_plan(self.path, self.report, SUPPLIER, [PRODUCT, PRODUCT])

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

    def test_form_reads_explicit_products_and_verifies_supplier_when_shown(self):
        for text, expected in (
            (f"签收\n供应商：{SUPPLIER}\n商品名称：{PRODUCT}\n采购商品\n{OTHER}\n材料", (PRODUCT, OTHER)),
            (f"备注 {PRODUCT}", ()),
        ):
            form = dict(products=[], suppliers=[], productTables=[], text=text, incomplete=False)
            self.assertEqual(receipt.form_products(form, SUPPLIER), expected)
        form.update(suppliers=[OTHER_SUPPLIER], products=[PRODUCT])
        with self.assertRaisesRegex(ReceiptStopped, "供应商.*不一致"):
            receipt.form_products(form, SUPPLIER)
        form.update(suppliers=[SUPPLIER], incomplete=True)
        with self.assertRaisesRegex(ReceiptStopped, "完整商品"):
            receipt.form_products(form, SUPPLIER)

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
        order = web_row(SUPPLIER)
        with patch.object(browser, "scan", return_value=(order, order)):
            with self.assertRaisesRegex(ReceiptStopped, "唯一"):
                browser.find_supplier(order)

    def test_cancel_before_upload_and_after_upload_is_respected(self):
        for consent, upload_count in (([False], 0), ([True, False], 1)):
            with self.subTest(consent=consent), redirect_stdout(StringIO()):
                browser = Mock()
                order = web_row(SUPPLIER)
                browser.find_supplier.return_value = order
                browser.inspect_products.return_value = (PRODUCT, OTHER)
                receipt.run_receipt(browser, order, self.path, self.report, confirm_action=Mock(side_effect=consent))
                self.assertEqual(browser.upload.call_count, upload_count)
                browser.finish.assert_not_called()

    def test_cli_preflight_accepts_folder_and_does_not_start_browser(self):
        with patch.object(receipt.shared, "init_driver") as launch, redirect_stdout(StringIO()):
            code = receipt.main(["--folder", str(self.path.parent)])
        self.assertEqual(code, 0)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
