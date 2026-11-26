"""低层 ZIP/XML 合成夹具专门覆盖导出方言；不包含真实数据。"""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
from attachments.__main__ import snapshot
from attachments.core import Asset, Cell, Sheet, classify, safe
from attachments.xlsx import read_xlsx
from attachments.clipboard import fragment, read_html
from attachments.openapi import request_spec

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1kAAAAASUVORK5CYII=')
PDF = b'%PDF-1.4\nsynthetic fixture\n%%EOF'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
D = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def fixture(path):
    # 顺序故意打乱：sheetA 使用 drawing7，目标图片位于 drawings/media。
    parts = {
      'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="合成报账" sheetId="1" r:id="s9"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="s9" Target="worksheets/sheetA.xml"/></Relationships>',
      'xl/sharedStrings.xml': f'<sst xmlns="{S}"><si><t>传感器19.62.pdf</t></si></sst>',
      'xl/worksheets/sheetA.xml': f'''<worksheet xmlns="{S}" xmlns:r="{R}"><sheetData>
      <row r="2"><c r="V2" t="s"><v>0</v></c><c r="U2"><f>_xlfn.DISPIMG("ID_PAY",1)</f></c></row>
      <row r="4"><c r="V4" t="s"><v>0</v></c><c r="W4" vm="1"><v>0</v></c></row>
      </sheetData><mergeCells><mergeCell ref="V2:V3"/></mergeCells><drawing r:id="d7"/>
      <hyperlinks><hyperlink ref="V4" r:id="pdf"/></hyperlinks></worksheet>''',
      'xl/worksheets/_rels/sheetA.xml.rels': '<Relationships><Relationship Id="d7" Target="../drawings/drawing7.xml"/><Relationship Id="pdf" Target="https://example.test/invoice.pdf?secret=omit" TargetMode="External"/></Relationships>',
      'xl/drawings/drawing7.xml': f'''<xdr:wsDr xmlns:xdr="{D}" xmlns:a="{A}" xmlns:r="{R}">
      <xdr:oneCellAnchor><xdr:from><xdr:col>19</xdr:col><xdr:row>1</xdr:row></xdr:from><xdr:pic><a:blip r:embed="image"/></xdr:pic></xdr:oneCellAnchor>
      <xdr:twoCellAnchor><xdr:from><xdr:col>19</xdr:col><xdr:row>2</xdr:row></xdr:from><xdr:pic><a:blip r:embed="image"/></xdr:pic></xdr:twoCellAnchor>
      <xdr:oneCellAnchor><xdr:from><xdr:col>19</xdr:col><xdr:row>3</xdr:row></xdr:from><xdr:pic><a:blip r:embed="image"/></xdr:pic></xdr:oneCellAnchor>
      </xdr:wsDr>''',
      'xl/drawings/_rels/drawing7.xml.rels': '<Relationships><Relationship Id="image" Target="media/image.png"/></Relationships>',
      'xl/drawings/media/image.png': PNG,
      'xl/cellimages.xml': f'<cellImages xmlns:xdr="{D}" xmlns:a="{A}" xmlns:r="{R}"><cellImage><xdr:pic><xdr:cNvPr name="ID_PAY"/><a:blip r:embed="pay"/></xdr:pic></cellImage></cellImages>',
      'xl/_rels/cellimages.xml.rels': '<Relationships><Relationship Id="pay" Target="drawings/media/image.png"/></Relationships>',
    }
    with ZipFile(path,'w') as z:
        for name,data in parts.items(): z.writestr(name,data)


class AttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_xlsx_relationship_merge_dispimg_isolation_and_hash(self):
        f = self.path/'test.xlsx'; fixture(f)
        sheets = read_xlsx(f)
        self.assertEqual(sheets[0].cell(3,22).text,'传感器19.62.pdf')
        self.assertEqual(sheets[0].cells[2,21].assets[0].data,PNG)
        root,r = classify(sheets,self.path/'out',fetch=lambda _:PDF)
        self.assertEqual([x['rows'] for x in r['records']], [[2,3],[4]])
        self.assertEqual(r['saved'],4)
        self.assertEqual(r['duplicates'],1)
        self.assertIn('发票',r['records'][0]['missing'])
        self.assertNotIn('发票',r['records'][1]['missing'])
        self.assertTrue(any('unsupported_rich_value' in x for x in r['warnings']))
        files = list(root.rglob('*.png'))
        self.assertEqual(len(files),3)
        for f in files: self.assertEqual(hashlib.sha256(f.read_bytes()).digest(),hashlib.sha256(PNG).digest())
        again,_ = classify(sheets,self.path/'out')
        self.assertNotEqual(root,again)
        self.assertEqual(len(list(root.rglob('*.png'))),3)

    def test_plain_invoice_is_missing_and_no_url_in_errors(self):
        s = Sheet('test',{(2,22):Cell('商品100.pdf'),(2,20):Cell(assets=[Asset('https://example.test?secret=hidden')])})
        root,r = classify([s],self.path)
        self.assertEqual(r['saved'],0)
        self.assertEqual(len(r['records'][0]['missing']),4)
        self.assertNotIn('secret', (root/'manifest.json').read_text(encoding='utf-8'))

    def test_export_link_markers_follow_invoice_header_range_and_merges(self):
        link = Asset('https://example.test/invoice.pdf')
        sheet = Sheet('S', {
            (1, 3): Cell('发票附件', [link]),
            (1, 4): Cell('支付截图'),
            (2, 3): Cell('合并的发票', [link]),
            (3, 4): Cell('支付附件', [link]),
            (3, 22): Cell('旧默认列', [link]),
            (4, 3): Cell('跨类型合并', [link]),
            (5, 3): Cell('发票', [link]),
            (6, 3): Cell('仅文件名.pdf'),
            (7, 3): Cell('已有内嵌发票', [Asset('embedded', PDF)]),
            (8, 3): Cell('超出范围', [link]),
        }, [(2, 3, 3, 3), (4, 3, 4, 4)])
        config = {'headers': {'发票': '发票附件', '支付记录': '支付截图'}, 'start_row': 3, 'end_row': 7}
        marked = snapshot([sheet], invoice_config=config)[0]['cells']
        self.assertEqual({(c['row'], c['col']) for c in marked if c.get('invoice_link')}, {(2, 3), (5, 3)})
        self.assertTrue(all('invoice_link' not in c for c in snapshot([sheet])[0]['cells']))

    def test_html_spans_data_images_and_void_tags(self):
        src = 'data:image/png;base64,'+base64.b64encode(PNG).decode()
        html = f'<table><tr><td rowspan="2"><img src="{src}"></td><td><br>x</td><td rowspan="2">商品2.pdf</td><td></td></tr><tr><td></td><td><img src="{src}"></td></tr></table>'
        sheets = read_html(html)
        self.assertEqual(sheets[0].cell(3,22).text,'商品2.pdf')
        _,r = classify(sheets,self.path)
        self.assertEqual(len(r['records']),1)
        self.assertEqual(r['saved'],2)
        self.assertEqual(r['duplicates'],1)

    def test_cf_html_offsets_are_bytes(self):
        body = '<table><tr><td>中文</td></tr></table>'.encode()
        header = b'StartFragment:0000000054\r\nEndFragment:0000000000\r\n'
        header = f'StartFragment:{len(header):010d}\r\nEndFragment:{len(header)+len(body):010d}\r\n'.encode()
        self.assertEqual(fragment(header+body),body.decode())

    def test_configured_headers_and_columns(self):
        s = Sheet('S',{(1,2):Cell('附件'),(2,1):Cell('同名'),(2,2):Cell(assets=[Asset('embedded',PNG)]),(3,1):Cell('同名'),(3,2):Cell(assets=[Asset('embedded',PNG)])})
        _,r = classify([s],self.path,{'headers':{'订单截图':'附件'},'product_column':'A','required':['订单截图']})
        self.assertEqual(len(r['records']),2)
        self.assertEqual(r['saved'],2)
        self.assertFalse(any(x['missing'] for x in r['records']))
        with self.assertRaises(ValueError): classify([s],self.path,{'headers':{'x':'不存在'}})

    def test_fetch_rejects_login_html(self):
        s = Sheet('S',{(2,22):Cell('商品1.pdf',assets=[Asset('url')])})
        _,r = classify([s],self.path,fetch=lambda _:b'<html>Login</html>')
        self.assertEqual(r['saved'],0)
        self.assertEqual(r['errors'][0]['code'],'unsupported_content')

    def test_cross_category_merge_is_not_counted_twice(self):
        s = Sheet('S',{(2,20):Cell(assets=[Asset('image',PNG)]),(2,22):Cell('商品1.pdf')},[(2,20,2,21)])
        _,r = classify([s],self.path)
        self.assertEqual(r['saved'],0)
        self.assertIn('订单截图',r['records'][0]['missing'])
        self.assertTrue(r['warnings'])

    def test_api_request_is_readonly_and_validated(self):
        env = dict(TENCENT_ACCESS_TOKEN='test',TENCENT_OPEN_ID='test',TENCENT_CLIENT_ID='test')
        url,h = request_spec('file','sheet','T2:W10',env)
        self.assertIn('/spreadsheet/v3/files/file/sheet/T2%3AW10',url)
        self.assertEqual(h['Access-Token'],'test')
        with self.assertRaises(ValueError): request_spec('f','s','A1:ZZ10000',env)
        with self.assertRaises(ValueError): request_spec('f','s','A1:A2',{})

    def test_safe_names_and_sheet_selection(self):
        self.assertEqual(safe('CON'),'_CON')
        self.assertNotIn('/',safe('../path'))
        f = self.path/'test.xlsx'; fixture(f)
        with self.assertRaises(ValueError): read_xlsx(f,'missing')


if __name__ == '__main__': unittest.main()
