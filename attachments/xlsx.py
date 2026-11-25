"""直接解析 ZIP/XML，避免表格样式兼容性影响附件读取。"""
import posixpath as pp
import re
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from .core import Asset, Cell, Sheet, coordinate

R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def local(tag): return tag.rsplit('}',1)[-1]


class Package:
    def __init__(self, path):
        self.zip = ZipFile(path)
        if sum(i.file_size for i in self.zip.infolist()) > 512*1024*1024:
            self.zip.close()
            raise ValueError('XLSX exceeds 512 MiB expanded limit')

    def xml(self, name): return ET.fromstring(self.zip.read(name))

    def rels(self, name):
        rp = pp.join(pp.dirname(name), '_rels', pp.basename(name)+'.rels')
        if rp not in self.zip.namelist(): return {}
        result = {}
        for node in self.xml(rp):
            target = node.get('Target','')
            external = node.get('TargetMode') == 'External'
            path = target if external else pp.normpath(target.lstrip('/') if target.startswith('/') else pp.join(pp.dirname(name),target))
            if not external and (path.startswith('../') or path.startswith('/')): raise ValueError('Invalid package relationship')
            result[node.get('Id')] = (path, external)
        return result

    def asset(self, pair):
        path, external = pair
        return Asset(path, None if external else self.zip.read(path))


def read_xlsx(path, selected=None):
    p = Package(path)
    try:
        return _read(p, selected)
    finally:
        p.zip.close()


def _read(p, selected):
    shared = []
    if 'xl/sharedStrings.xml' in p.zip.namelist():
        shared = [''.join(x.text or '' for x in n.iter() if local(x.tag)=='t') for n in p.xml('xl/sharedStrings.xml')]
    # WPS/DISPIMG 单元格图片：名称 -> blip -> relationships，不猜测 media 文件顺序。
    cell_images = {}
    if 'xl/cellimages.xml' in p.zip.namelist():
        rels = p.rels('xl/cellimages.xml')
        for node in p.xml('xl/cellimages.xml'):
            name = next((n.get('name') for n in node.iter() if local(n.tag)=='cNvPr'), None)
            blip = next((n for n in node.iter() if local(n.tag)=='blip'), None)
            if name and blip is not None:
                rid = blip.get(R+'embed') or blip.get(R+'link')
                if rid in rels: cell_images[name] = rels[rid]
    workbook = p.xml('xl/workbook.xml')
    books = p.rels('xl/workbook.xml')
    sheets = []
    for node in workbook.iter():
        if local(node.tag) != 'sheet': continue
        name = node.get('name','Sheet')
        if selected and selected != name: continue
        sheet = Sheet(name)
        file, external = books[node.get(R+'id')]
        if external: raise ValueError('External worksheet unsupported')
        xml = p.xml(file)
        rels = p.rels(file)
        for n in xml.iter():
            kind = local(n.tag)
            if kind == 'mergeCell':
                a,b = n.get('ref').split(':')
                sheet.merges.append((*coordinate(a),*coordinate(b)))
            elif kind == 'c':
                loc = coordinate(n.get('r'))
                v = next((x.text or '' for x in n if local(x.tag)=='v'),'')
                if n.get('t') == 's': v = shared[int(v)] if v else ''
                elif n.get('t') == 'inlineStr': v = ''.join(x.text or '' for x in n.iter() if local(x.tag)=='t')
                c = Cell(v)
                formula = next((x.text or '' for x in n if local(x.tag)=='f'),'')
                match = re.search(r'DISPIMG\("([^"]+)"', formula, re.I)
                if match:
                    if match[1] in cell_images: c.assets.append(p.asset(cell_images[match[1]]))
                    else: sheet.warnings.append('unresolved_cell_image:' + n.get('r'))
                if n.get('vm'):
                    sheet.warnings.append('unsupported_rich_value_metadata:' + n.get('r'))
                sheet.cells[loc] = c
            elif kind == 'hyperlink' and n.get(R+'id') in rels:
                # 超链接是可获取候选；签名验证拒绝返回的网页/纯文字。
                for ref in n.get('ref').split():
                    if ':' in ref:
                        sheet.warnings.append('hyperlink_range_not_expanded:' + ref)
                        continue
                    sheet.cells.setdefault(coordinate(ref), Cell()).assets.append(p.asset(rels[n.get(R+'id')]))
        for n in xml.iter():
            if local(n.tag) != 'drawing': continue
            drawing, external = rels[n.get(R+'id')]
            if external:
                sheet.warnings.append('external_drawing_unsupported')
                continue
            dr = p.rels(drawing)
            for anchor in p.xml(drawing):
                start = next((x for x in anchor if local(x.tag)=='from'), None)
                if start is None:
                    sheet.warnings.append('unmapped_absolute_anchor')
                    continue
                vals = {local(x.tag): int(x.text) for x in start}
                loc = (vals['row']+1, vals['col']+1)
                for blip in anchor.iter():
                    if local(blip.tag) != 'blip': continue
                    rid = blip.get(R+'embed') or blip.get(R+'link')
                    if rid in dr: sheet.cells.setdefault(loc,Cell()).assets.append(p.asset(dr[rid]))
                    else: sheet.warnings.append('unresolved_drawing_relationship')
        sheets.append(sheet)
    if not sheets: raise ValueError('No matching worksheet')
    return sheets
