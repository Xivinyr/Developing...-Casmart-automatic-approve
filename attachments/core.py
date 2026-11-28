"""统一单元格模型、记录隔离和分类保存（仅使用标准库）。"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .invoices import index_invoices, invoice_key, read_invoice


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
               headers={}, required=['订单截图','支付记录','发票','开箱实物图'], supplier_column=None)

SUPPLIER_HEADERS = frozenset({"供应商", "供应商名称", "供货商", "供货商名称", "商家", "商家名称", "店铺", "店铺名称"})
PRODUCT_HEADERS = frozenset({"商品", "商品名", "商品名称", "采购商品", "采购商品名称", "品名",
                             "物品名称", "货品名称"})


def header_label(text):
    """原采购表用星号标记必填列，识别表头时去掉该标记。"""
    return "".join(text.strip().lstrip("*＊").rstrip("：: ").split())


def attachment_columns(sheet, config):
    if not config['headers']:
        return {column(key): label for key, label in config['columns'].items()}
    columns = {}
    for label, title in config['headers'].items():
        matches = [c for (r,c), value in sheet.cells.items()
                   if r == config['header_row'] and value.text.strip() == title]
        if len(matches) != 1:
            raise ValueError('Header missing or ambiguous: ' + title)
        columns[matches[0]] = label
    return columns


def supplier_column(sheet, config):
    """完整工作簿可识别供应商表头，裁切后的旧选区仍按原流程分类。"""
    if config["supplier_column"]:
        return column(config["supplier_column"])
    matches = [col for (row, col), cell in sheet.cells.items()
               if row == config["header_row"] and header_label(cell.text) in SUPPLIER_HEADERS]
    return matches[0] if len(matches) == 1 else None


def cross_category_merges(sheet, columns):
    return [merge for merge in sheet.merges
            if len({label for col, label in columns.items() if merge[1] <= col <= merge[3]}) > 1]


def invoice_link_cells(sheet, config=None, *, include_filenames=False):
    """标记确属发票列、能交给浏览器尝试读取的链接；不把其他列的链接混进来。"""
    config = DEFAULT | (config or {})
    columns = attachment_columns(sheet, config)
    # 纵向合并的附件存放在起始格，范围内的任一行仍可使用该附件。
    ambiguous = cross_category_merges(sheet, columns)
    start = config['start_row']
    end = config['end_row']
    invoice_cols = {col for col, label in columns.items() if label == '发票'}
    marked = set()
    if end and end < start:
        return marked
    for (row, col), cell in sheet.cells.items():
        if col not in invoice_cols:
            continue
        external = any(a.data is None and a.source for a in cell.assets)
        selected_filename = include_filenames and invoice_key(cell.text) is not None
        if not (external or selected_filename):
            continue
        if end and row > end:
            continue
        if row < start and not any(r1 == row and c1 == c2 == col and r2 >= start
                                   for r1,c1,r2,c2 in sheet.merges):
            continue
        if not any(r1 <= row <= r2 and c1 <= col <= c2 for r1,c1,r2,c2 in ambiguous):
            marked.add((row, col))
    return marked


def _save_file(data, label, root, folder, record, seen):
    ext = extension(data)
    digest = hashlib.sha256(data).hexdigest()
    # 类型内去重，跨类型/跨订单保留业务归属。
    identity = label, digest
    if identity in seen:
        return False
    folder.mkdir(parents=True, exist_ok=True)
    filename = f'{safe(label)}-{len(record["files"])+1:03d}-{digest[:12]}{ext}'
    dest = folder / filename
    with dest.open('xb') as stream:
        stream.write(data)
    seen.add(identity)
    record['files'].append({'category':label, 'path':str(dest.relative_to(root)), 'sha256':digest, 'bytes':len(data)})
    return True


def classify(sheets, output, config=None, fetch=None, invoice_dir=None):
    config = DEFAULT | (config or {})
    # 每次独立目录 + 排他创建文件，任何重跑均不覆盖上次结果。
    root = Path(output) / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + __import__('uuid').uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    report = {'records': [], 'errors': [], 'warnings': [], 'saved': 0, 'duplicates': 0}
    invoice_references = Counter()
    invoice_candidates = []
    for si, sheet in enumerate(sheets, 1):
        report['warnings'].extend(sheet.warnings)
        cols = attachment_columns(sheet, config)
        ambiguous = set()
        for r1,c1,r2,c2 in cross_category_merges(sheet, cols):
            ambiguous.update((r,c) for r in range(r1,r2+1) for c in cols if c1 <= c <= c2)
            report['warnings'].append(f'ambiguous_cross_category_merge:sheet{si}:row{r1}')
        pc = column(config['product_column']) if config['product_column'] else None
        rc = column(config['record_column']) if config['record_column'] else None
        supplier_col = supplier_column(sheet, config)
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
            seen = set()
            found = set()
            explicit_sources = set()
            invoice_names = {}
            rec = {'sheet_index':si, 'row':origin, 'rows':rows, 'product':name, 'files':[]}
            if supplier_col:
                # 供应商信息保留在记录中，仍以商品或记录列作为附件的归属边界。
                suppliers = {" ".join(sheet.cell(r, supplier_col).text.split()) for r in rows} - {""}
                if len(suppliers) == 1:
                    rec["supplier"] = suppliers.pop()
                elif suppliers:
                    rec["supplier_conflict"] = True
                    report["warnings"].append(f"supplier_conflict:sheet{si}:row{origin}")
            for row in rows:
                for col, label in cols.items():
                    if (row,col) in ambiguous: continue
                    cell = sheet.cell(row,col)
                    if cell.assets:
                        explicit_sources.add(label)
                    if label == '发票':
                        key = invoice_key(cell.text)
                        if key is not None:
                            invoice_names.setdefault(key, (row,col))
                    for asset in cell.assets:
                        try:
                            data = asset.data if asset.data is not None else (fetch(asset.source) if fetch else None)
                            if data is None: raise ValueError('online_fetch_required')
                            if _save_file(data, label, root, folder, rec, seen):
                                report['saved'] += 1
                            else:
                                report['duplicates'] += 1
                            found.add(label)
                        except Exception as exc:
                            # 不记录 URL、查询参数、响应正文或异常全文，避免泄露临时签名/令牌。
                            code = str(exc) if str(exc) in ('online_fetch_required','unsupported_content') else type(exc).__name__
                            report['errors'].append({'sheet_index':si,'row':row,'column':col,'code':code})
            rec['missing'] = [v for v in config['required'] if v not in found]
            rec['missing_reasons'] = {
                label: 'resource_unavailable' if label in explicit_sources else 'no_source'
                for label in rec['missing']
            }
            # 先收集各工作表的引用，再匹配本地文件，避免相同文件名误归入多笔记录。
            invoice_references.update(invoice_names.keys())
            if invoice_names and '发票' not in found and '发票' not in explicit_sources:
                if '发票' in rec['missing_reasons']:
                    rec['missing_reasons']['发票'] = 'invoice_filename_only'
                if invoice_dir is not None:
                    invoice_candidates.append((invoice_names, folder, rec))
            report['records'].append(rec)
    if invoice_candidates:
        _backfill_invoices(invoice_dir, invoice_references, invoice_candidates, root, report)
    (root/'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'errors.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in report['errors']), encoding='utf-8')
    return root, report


def _backfill_invoices(directory, references, candidates, root, report):
    try:
        local_files = index_invoices(directory)
    except OSError:
        local_files = None

    for names, folder, record in candidates:
        key, (row, col) = next(iter(names.items()))
        if len(names) != 1 or references[key] != 1:
            reason = 'invoice_filename_ambiguous'
        elif local_files is None:
            reason = 'invoice_folder_unavailable'
        else:
            matches = local_files.get(key, [])
            if len(matches) > 1:
                reason = 'invoice_filename_ambiguous'
            elif not matches:
                reason = 'invoice_not_in_folder'
            else:
                try:
                    data = read_invoice(matches[0])
                except (OSError, ValueError):
                    reason = 'invoice_local_invalid'
                else:
                    try:
                        _save_file(data, '发票', root, folder, record, set())
                    except OSError as exc:
                        reason = 'resource_unavailable'
                        report['errors'].append({'sheet_index':record['sheet_index'], 'row':row, 'column':col, 'code':type(exc).__name__})
                    else:
                        report['saved'] += 1
                        record['missing'] = [label for label in record['missing'] if label != '发票']
                        record['missing_reasons'].pop('发票', None)
                        continue
        if '发票' in record['missing_reasons']:
            record['missing_reasons']['发票'] = reason
