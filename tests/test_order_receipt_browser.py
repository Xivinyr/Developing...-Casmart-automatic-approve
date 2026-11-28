"""使用断网 Edge 的 data: 页面，真实驱动上传合成文件及签收按钮。"""
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from selenium import webdriver
from selenium.webdriver.edge.service import Service

import order_receipt as receipt
from receipt_materials import ReceiptStopped, build_plan
from test_order_receipt import PRODUCT, synthetic_manifest


FIXTURE = """<!doctype html><meta charset="utf-8">
<table><thead><tr><th>序号</th><th>商品名称</th><th>操作</th></tr></thead>
<tbody>
<tr><td>1</td><td>合成商品</td><td><button onclick="openReceipt('合成商品')">签收</button></td></tr>
<tr><td>2</td><td>另一个商品</td><td><button onclick="openReceipt('另一个商品')">签收</button></td></tr>
</tbody></table>
<script>
window.events = [];
const categories = ['订单截图','支付记录','发票','开箱实物图'];
function openReceipt(product) {
    events.push(['open', product]);
    const box = document.createElement('div');
    box.className = 'el-dialog';
    const shown = mode === 'wrong-product' ? '另一个商品' : product;
    const info = mode === 'split' ? '<table><thead><th>商品名称</th></thead><tbody><tr><td>' + shown +
        '</td></tr></tbody></table>' : '<p>商品名称：' + shown + '</p>';
    box.innerHTML = '<h2>单据签收</h2>' + info +
        '<div class="fields"></div><ul class="files"></ul><button class="final" disabled>签收</button>';
    const list = box.querySelector('.files');
    function addStatus(name, status) {
        const row = document.createElement('li');
        row.className = 'el-upload-list__item';
        row.dataset.uploadStatus = status;
        const label = document.createElement('span');
        label.className = 'el-upload-list__item-name';
        label.textContent = name;
        row.append(label);
        list.append(row);
        return row;
    }
    if (mode === 'existing') addStatus('synthetic-existing.png', 'success');
    const slots = mode === 'split' ? categories : [''];
    const final = box.querySelector('.final');
    slots.forEach((category, index) => {
        const input = document.createElement('input');
        input.id = 'upload-' + index;
        input.type = 'file';
        input.style.display = 'none';
        input.multiple = !category;
        input.accept = category === '发票' ? 'application/pdf' : category ? 'image/*' : 'image/*,application/pdf';
        const label = document.createElement('label');
        label.htmlFor = input.id;
        label.textContent = category || '上传附件';
        input.onchange = () => {
            const files = [...input.files].map(file => file.name);
            events.push(['upload', category, files]);
            files.forEach(name => {
                const row = addStatus(name, 'uploading');
                setTimeout(() => {
                    row.dataset.uploadStatus = mode === 'failed' ? 'error' : 'success';
                    final.disabled = list.querySelectorAll('[data-upload-status="success"]').length !== 4;
                }, 140);
            });
        };
        box.querySelector('.fields').append(label, input);
    });
    final.onclick = () => {
        events.push(['finish', product]);
        box.remove();
        const message = document.createElement('div');
        message.setAttribute('role', 'status');
        message.textContent = mode === 'uncertain' ? '操作已提交' : '签收成功';
        document.body.append(message);
    };
    document.body.append(box);
}
</script>"""


class FixtureBrowser(receipt.ReceiptBrowser):
    def guard(self):
        self.guard_receipt()

    def guard_receipt(self):
        assert self.driver.current_url.startswith("data:text/html;")
        assert self.driver.window_handles == [self.window]
        self.driver.switch_to.default_content()


@unittest.skipUnless(os.environ.get("CASMART_RECEIPT_BROWSER_TEST") == "1", "需显式开启本机 Edge 合成签收测试")
class ReceiptBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = webdriver.EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-first-run")
        options.add_argument("--disable-background-networking")
        cls.driver = webdriver.Edge(service=Service(os.environ["CASMART_TEST_EDGEDRIVER"]), options=options)
        cls.addClassCleanup(cls.driver.quit)
        cls.driver.execute_cdp_cmd("Network.enable", {})
        cls.driver.execute_cdp_cmd("Network.emulateNetworkConditions",
            {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path, report = synthetic_manifest(temp.name)
        self.manifest, self.report = path, report
        self.plan = build_plan(path, report, PRODUCT)

    def start(self, mode):
        html = "<script>const mode=" + json.dumps(mode) + ";</script>" + FIXTURE
        url = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode("ascii")
        self.driver.get(url)
        return FixtureBrowser(self.driver, timeout=3)

    def events(self):
        return self.driver.execute_script("return window.events")

    def execute(self, browser, consent=lambda *_: True):
        ready, _skipped = receipt.match_receipts(browser.scan(), self.manifest, self.report)
        selection = next(pair for pair in ready if pair[1].product == PRODUCT)
        receipt.run_receipt(browser, *selection, confirm_action=consent)

    def test_upload_all_and_confirm_same_order_for_single_and_split_forms(self):
        for mode in ("single", "split"):
            with self.subTest(mode=mode), redirect_stdout(StringIO()) as output:
                browser = self.start(mode)
                self.execute(browser)
                events = self.events()
                self.assertEqual(events[0], ["open", PRODUCT])
                self.assertEqual(events[-1], ["finish", PRODUCT])
                sent = [item for item in events if item[0] == "upload"]
                self.assertCountEqual([name for _, _, names in sent for name in names], self.plan.names)
                if mode == "split":
                    self.assertCountEqual([label for _, label, _ in sent], [file.category for file in self.plan.files])
                    for _, label, names in sent:
                        self.assertEqual(names, [file.path.name for file in self.plan.files if file.category == label])
                self.assertIn("签收成功", output.getvalue())

    def test_wrong_product_or_existing_attachments_stop_before_upload(self):
        for mode, reason in (("wrong-product", "不一致"), ("existing", "已经有附件")):
            with self.subTest(mode=mode), redirect_stdout(StringIO()):
                browser = self.start(mode)
                with self.assertRaisesRegex(ReceiptStopped, reason):
                    self.execute(browser)
                self.assertEqual(self.events(), [["open", PRODUCT]])

    def test_upload_error_does_not_reach_final_confirmation(self):
        browser = self.start("failed")
        consent = Mock(return_value=True)
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ReceiptStopped, "上传失败"):
                self.execute(browser, consent=consent)
        consent.assert_called_once_with("上传", PRODUCT)
        self.assertEqual([event[0] for event in self.events()], ["open", "upload"])

    def test_ambiguous_completion_causes_no_retry(self):
        browser = self.start("uncertain")
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ReceiptStopped, "未看到明确"):
                self.execute(browser)
        self.assertEqual([event[0] for event in self.events()], ["open", "upload", "finish"])


if __name__ == "__main__":
    unittest.main()
