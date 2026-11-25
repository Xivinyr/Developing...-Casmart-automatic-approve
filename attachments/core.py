"""统一单元格模型、记录隔离和分类保存（仅使用标准库）。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Asset:
    source: str
    data: bytes | None = None


@dataclass
class Cell:
    text: str = ""
    assets: list[Asset] = field(default_factory=list)


@dataclass
class Sheet:
    name: str
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def origin(self, row, col):
        for r1, c1, r2, c2 in self.merges:
            if r1 <= row <= r2 and c1 <= col <= c2:
                return r1, c1
        return row, col

    def cell(self, row, col):
        # 合并单元格共享文字及附件，但不按相同商品名合并不同记录。
        origin = self.origin(row, col)
        first = self.cells.get(origin, Cell())
        own = self.cells.get((row, col), Cell())
        return Cell(own.text or first.text, own.assets if own.assets else first.assets)


def column(value):
    if isinstance(value, int):
        return value
    result = 0
    for ch in value.upper():
        if not 'A' <= ch <= 'Z':
            raise ValueError('Invalid column')
        result = result * 26 + ord(ch) - 64
    return result


def coordinate(ref):
    m = re.fullmatch(r'\$?([A-Za-z]+)\$?(\d+)', ref)
    if not m:
        raise ValueError('Invalid cell reference')
    return int(m[2]), column(m[1])


def safe(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:80]
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', value):
        value = '_' + value
    return value or '未命名'


def product(text):
    text = re.sub(r'\.(pdf|png|jpe?g|webp)$', '', text.strip(), flags=re.I)
    return re.sub(r'\s*\d+(?:\.\d+)?$', '', text).strip() or '未识别商品'


def extension(data):
    # 根据实际文件签名判断，不把 .pdf 文字或登录 HTML 算作已下载附件。
    if data.startswith(b'%PDF-'): return '.pdf'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if data.startswith(b'\xff\xd8\xff'): return '.jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return '.webp'
    raise ValueError('unsupported_content')


DEFAULT = dict(header_row=1, start_row=2, end_row=None, product_column=None,
               record_column=None, columns={'T':'订单截图','U':'支付记录','V':'发票','W':'开箱实物图'},
               headers={}, required=['订单截图','支付记录','发票','开箱实物图'])


def classify(sheets, output, config=None, fetch=None):
    config = DEFAULT | (config or {})
    # 每次独立目录 + 排他创建文件，任何重跑均不覆盖上次结果。
    root = Path(output) / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + __import__('uuid').uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    report = {'records': [], 'errors': [], 'warnings': [], 'saved': 0, 'duplicates': 0}
    for si, sheet in enumerate(sheets, 1):
        report['warnings'].extend(sheet.warnings)
        cols = {column(k):v for k,v in config['columns'].items()}
        if config['headers']:
            cols = {}
            for label, title in config['headers'].items():
                matches = [c for (r,c), v in sheet.cells.items() if r == config['header_row'] and v.text.strip() == title]
                if len(matches) != 1: raise ValueError('Header missing or ambiguous: ' + title)
                cols[matches[0]] = label
        ambiguous = set()
        for r1,c1,r2,c2 in sheet.merges:
            if len({v for c,v in cols.items() if c1 <= c <= c2}) > 1:
                ambiguous.update((r,c) for r in range(r1,r2+1) for c in cols if c1 <= c <= c2)
                report['warnings'].append(f'ambiguous_cross_category_merge:sheet{si}:row{r1}')
        pc = column(config['product_column']) if config['product_column'] else None
        rc = column(config['record_column']) if config['record_column'] else None
        invoice = next((c for c,v in cols.items() if v == '发票'), None)
        end = config['end_row'] or max([r for r,c in sheet.cells] + [m[2] for m in sheet.merges] + [1])
        groups = {}
        for row in range(config['start_row'], end + 1):
            if not any(sheet.cell(row,c).text or sheet.cell(row,c).assets for c in set(cols) | {x for x in (pc,rc) if x}): continue
            # 仅显式记录列或商品/发票列的纵向合并作为记录归属证据。
            anchor = rc or pc or invoice
            origin = sheet.origin(row, anchor)[0] if anchor else row
            key = origin
            groups.setdefault(key, []).append(row)
        for origin, rows in groups.items():
            name = sheet.cell(rows[0],pc).text if pc else product(sheet.cell(rows[0],invoice).text if invoice else '')
            folder = root / f'{si:02d}-{safe(sheet.name)}' / f'row-{origin:06d}-{safe(name)}'
            seen = {}
            found = set()
            rec = {'sheet_index':si, 'row':origin, 'rows':rows, 'product':name, 'files':[], 'missing':[]}
            for row in rows:
                for col, label in cols.items():
                    if (row,col) in ambiguous: continue
                    for asset in sheet.cell(row,col).assets:
                        try:
                            data = asset.data if asset.data is not None else (fetch(asset.source) if fetch else None)
                            if data is None: raise ValueError('online_fetch_required')
                            ext = extension(data)
                            digest = hashlib.sha256(data).hexdigest()
                            found.add(label)
                            # 类型内去重，跨类型/跨订单保留业务归属。
                            if (label,digest) in seen:
                                report['duplicates'] += 1
                                continue
                            folder.mkdir(parents=True, exist_ok=True)
                            filename = f'{safe(label)}-{len(rec["files"])+1:03d}-{digest[:12]}{ext}'
                            with (folder / filename).open('xb') as f: f.write(data)
                            seen[label,digest] = filename
                            rec['files'].append({'category':label,'path':str((folder/filename).relative_to(root)), 'sha256':digest,'bytes':len(data)})
                            report['saved'] += 1
                        except Exception as exc:
                            # 不记录 URL、查询参数、响应正文或异常全文，避免泄露临时签名/令牌。
                            code = str(exc) if str(exc) in ('online_fetch_required','unsupported_content') else type(exc).__name__
                            report['errors'].append({'sheet_index':si,'row':row,'column':col,'code':code})
            rec['missing'] = [v for v in config['required'] if v not in found]
            report['records'].append(rec)
    (root/'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'errors.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in report['errors']), encoding='utf-8')
    return root, report
