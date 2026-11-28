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
from receipt_materials import ReceiptStopped, build_supplier_plan, load_manifest
from test_order_receipt import OTHER, OTHER_SUPPLIER, PRODUCT, SUPPLIER, synthetic_manifest


FIXTURE = r"""<!doctype html><meta charset="utf-8">
<main></main>
<script>
window.events = [];
const categories = ['订单截图','支付记录','发票','开箱实物图'];
const first = '合成供应商甲';
const second = '合成供应商乙';
const button = supplier => '<button onclick="openReceipt(\'' + supplier + '\')">签收</button>';
const rows = mode === 'split' ?
    '<tr><td rowspan="2">' + first + '</td><td>合成商品</td><td rowspan="2">' + button(first) + '</td></tr>' +
    '<tr><td>另一个商品</td></tr><tr><td>' + second + '</td><td>合成商品</td><td>' + button(second) + '</td></tr>' :
    '<tr><td>' + first + '</td><td>' + button(first) + '</td></tr>' +
    '<tr><td>' + second + '</td><td>' + button(second) + '</td></tr>';
document.querySelector('main').innerHTML =
    '<table><thead><tr><th>供应商名称</th>' + (mode === 'split' ? '<th>商品名称</th>' : '') +
    '<th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>';
if (mode === 'merged-grid') {
    document.querySelector('main').innerHTML =
        '<table><thead><tr><th>供应商名称</th><th>商品名称</th><th>金额</th><th>备注</th><th>操作</th></tr></thead>' +
        '<tbody><tr><td colspan="5">分组抬头<table><tbody><tr><td>组内说明</td></tr></tbody></table></td></tr>' +
        '<tr><td rowspan="0">' + first + '</td><td>合成商品</td><td colspan="2">合计</td>' +
        '<td rowspan="5">' + button(first) + '</td></tr>' +
        '<tr><td style="display:none"></td><td>另一个商品</td><td colspan="2">合计</td></tr></tbody>' +
        '<tbody><tr><td>' + second + '</td><td>合成商品</td><td colspan="2">合计</td><td>' +
        button(second) + '</td></tr></tbody></table>';
}
if (mode === 'ambiguous-grid') {
    document.querySelector('main tbody').insertAdjacentHTML(
        'afterbegin', '<tr><td colspan="2">混合内容' + button(first) + '</td></tr>');
}
if (['order-sections', 'mixed-headers', 'section-action'].includes(mode)) {
    const select = mode === 'mixed-headers' ? '<td>全选</td>' : '<th>商品信息</th>';
    const section = '<tr><td colspan="6">线下单号：合成示例　日期：合成日期　待签收</td></tr>';
    document.querySelector('main').innerHTML =
        '<table style="width:100%"><thead><tr>' + select + '<th>订单金额</th><th>订单归属</th>' +
        '<th>供应商</th><th>操作</th></tr></thead>' +
        '<tbody>' + section +
        '<tr><td>合成商品<br>另一个商品</td><td>合成金额</td><td>合成课题组</td><td>' + first +
        '</td><td>' + button(first) + '<a>查看详情</a></td></tr></tbody>' +
        '<tbody>' + section +
        '<tr><td>合成商品</td><td>合成金额</td><td>合成课题组</td><td>' + second +
        '</td><td>' + button(second) + '<a>查看详情</a></td></tr></tbody></table>';
    if (mode === 'section-action') {
        document.querySelector('tbody tr td').insertAdjacentHTML('beforeend', button(first));
    }
}
function openReceipt(supplier) {
    events.push(['open', supplier]);
    const box = document.createElement('div');
    box.className = 'el-dialog';
    const vendor = mode === 'wrong-supplier' ? second : supplier;
    const item = mode === 'unmatched' ? '无材料商品' : '另一个商品';
    let info = mode === 'split' ?
        '<div class="el-table"><table><thead><tr><th>供应商</th><th>商品名称</th></tr></thead></table>' +
        '<table><tbody><tr><td rowspan="2">' + vendor + '</td><td>合成商品</td></tr>' +
        '<tr><td>' + item + '</td></tr></tbody></table></div>' :
        '<p>供应商：' + vendor + '</p><table><thead><tr><th>商品名称</th></tr></thead>' +
        '<tbody><tr><td>合成商品</td></tr><tr><td>' + item + '</td></tr></tbody></table>';
    if (mode === 'merged-grid') {
        info = '<p>供应商：' + vendor + '</p><table><thead><tr><th>商品名称</th><th>金额</th><th>备注</th></tr></thead>' +
            '<tbody><tr><td>合成商品</td><td colspan="2">合计</td></tr>' +
            '<tr><td>' + item + '</td><td colspan="2">合计</td></tr></tbody></table>';
    }
    if (mode === 'ambiguous-detail-grid') {
        info = '<table><thead><tr><th>商品名称</th><th>备注</th></tr></thead>' +
            '<tbody><tr><td colspan="2">合成商品</td></tr></tbody></table>';
    }
    box.innerHTML = '<h2>单据签收</h2>' + info +
        '<div class="fields"></div><ul class="files"></ul><button class="final" disabled>签收</button>';
    if (mode === 'paginated') box.insertAdjacentHTML('beforeend', '<div class="el-pagination">下一页</div>');
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
        input.multiple = category !== '发票';
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
                    final.disabled = list.querySelectorAll('[data-upload-status="success"]').length !== 7;
                }, 140);
            });
        };
        box.querySelector('.fields').append(label, input);
    });
    final.onclick = () => {
        events.push(['finish', supplier]);
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
        path, report = synthetic_manifest(temp.name, suppliers=(SUPPLIER, SUPPLIER))
        self.manifest, self.report = path, report
        self.plan = build_supplier_plan(path, report, SUPPLIER, [PRODUCT, OTHER])

    def start(self, mode):
        html = "<script>const mode=" + json.dumps(mode) + ";</script>" + FIXTURE
        url = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode("ascii")
        self.driver.get(url)
        return FixtureBrowser(self.driver, timeout=3)

    def events(self):
        return self.driver.execute_script("return window.events")

    def execute(self, browser, consent=lambda *_: True):
        ready, _skipped = receipt.supplier_choices(browser.scan())
        self.assertCountEqual([row.supplier for row in ready], [SUPPLIER, OTHER_SUPPLIER])
        selection = next(row for row in ready if row.supplier == SUPPLIER)
        receipt.run_receipt(browser, selection, self.manifest, self.report, confirm_action=consent)

    def test_upload_all_and_confirm_same_order_for_single_and_split_forms(self):
        for mode in ("single", "split"):
            with self.subTest(mode=mode), redirect_stdout(StringIO()) as output:
                browser = self.start(mode)
                self.execute(browser)
                events = self.events()
                self.assertEqual(events[0], ["open", SUPPLIER])
                self.assertEqual(events[-1], ["finish", SUPPLIER])
                sent = [item for item in events if item[0] == "upload"]
                self.assertCountEqual([name for _, _, names in sent for name in names], self.plan.names)
                if mode == "split":
                    self.assertCountEqual([label for _, label, _ in sent], {file.category for file in self.plan.files})
                    for _, label, names in sent:
                        self.assertEqual(names, [file.path.name for file in self.plan.files if file.category == label])
                self.assertIn("签收成功", output.getvalue())

    def test_merged_grid_with_legacy_manifest(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path, _report = synthetic_manifest(temp.name)
        self.manifest, self.report = load_manifest(path)
        with redirect_stdout(StringIO()) as output:
            browser = self.start("merged-grid")
            self.execute(browser)
        self.assertIn("清单未记录供应商", output.getvalue())
        self.assertEqual([event[0] for event in self.events()], ["open", "upload", "finish"])
        self.assertEqual(self.events()[-1], ["finish", SUPPLIER])

    def test_action_with_supplier_spread_across_columns_stops_scan(self):
        browser = self.start("ambiguous-grid")
        with self.assertRaisesRegex(receipt.OrderListStopped, "供应商.*跨列"):
            browser.scan()
        self.assertEqual(self.events(), [])

    def test_order_sections_may_span_more_columns_than_header(self):
        for mode in ('order-sections', 'mixed-headers'):
            with self.subTest(mode=mode), redirect_stdout(StringIO()):
                browser = self.start(mode)
                self.execute(browser)
                self.assertEqual(self.events()[0], ["open", SUPPLIER])
                self.assertEqual(self.events()[-1], ["finish", SUPPLIER])
                uploaded = [name for event in self.events() if event[0] == "upload" for name in event[2]]
                self.assertCountEqual(uploaded, self.plan.names)

    def test_receipt_button_in_section_is_not_assigned_to_next_supplier(self):
        browser = self.start("section-action")
        with self.assertRaisesRegex(receipt.OrderListStopped, "供应商.*跨列"):
            browser.scan()
        self.assertEqual(self.events(), [])

    def test_unsafe_detail_stops_before_upload(self):
        for mode, reason in (("wrong-supplier", "供应商.*不一致"), ("unmatched", "无材料商品"),
                             ("paginated", "完整商品"), ("existing", "已经有附件"),
                             ("ambiguous-detail-grid", "商品或供应商名称跨列")):
            with self.subTest(mode=mode), redirect_stdout(StringIO()):
                browser = self.start(mode)
                with self.assertRaisesRegex(ReceiptStopped, reason):
                    self.execute(browser)
                self.assertEqual(self.events(), [["open", SUPPLIER]])

    def test_products_changed_after_confirmation_are_never_uploaded(self):
        browser = self.start("single")

        def consent(*_args):
            self.driver.execute_script("document.querySelector('.el-dialog tbody tr:last-child td').textContent = '变更商品'")
            return True

        with redirect_stdout(StringIO()), self.assertRaisesRegex(ReceiptStopped, "清单.*变化"):
            self.execute(browser, consent=consent)
        self.assertEqual(self.events(), [["open", SUPPLIER]])

    def test_upload_error_does_not_reach_final_confirmation(self):
        browser = self.start("failed")
        consent = Mock(return_value=True)
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ReceiptStopped, "上传失败"):
                self.execute(browser, consent=consent)
        consent.assert_called_once_with("上传", SUPPLIER)
        self.assertEqual([event[0] for event in self.events()], ["open", "upload"])

    def test_ambiguous_completion_causes_no_retry(self):
        browser = self.start("uncertain")
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ReceiptStopped, "未看到明确"):
                self.execute(browser)
        self.assertEqual([event[0] for event in self.events()], ["open", "upload", "finish"])


if __name__ == "__main__":
    unittest.main()
