"""从原采购表补齐分类清单的供应商字段，按完整商品名或原发票文件名匹配。"""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from unicodedata import normalize

from .core import DEFAULT, PRODUCT_HEADERS, column, header_label, product, supplier_column
from .xlsx import read_xlsx


def with_suppliers(report, sheets):
    aliases = {}
    for sheet in sheets:
        vendor_col = supplier_column(sheet, DEFAULT)
        if vendor_col is None:
            continue
        product_cols = [col for (row, col), cell in sheet.cells.items()
                        if row == DEFAULT["header_row"] and header_label(cell.text) in PRODUCT_HEADERS]
        if len(product_cols) > 1:
            raise ValueError("原采购表有多个商品名称列，无法确认供应商归属。")
        invoice_col = next(column(letter) for letter, label in DEFAULT["columns"].items() if label == "发票")
        end = max((row for row, _col in sheet.cells), default=1)
        for row in range(DEFAULT["start_row"], end + 1):
            supplier = " ".join(normalize("NFC", sheet.cell(row, vendor_col).text).split())
            if not supplier:
                continue
            names = {sheet.cell(row, product_cols[0]).text} if product_cols else set()
            invoice = sheet.cell(row, invoice_col).text.strip()
            if invoice.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp")):
                names.add(product(invoice))
            for name in names:
                key = " ".join(normalize("NFC", name).split())
                if key and key != "未识别商品":
                    aliases.setdefault(key, set()).add(supplier)

    if not aliases:
        raise ValueError("原采购表未读到可匹配的商品和供应商，请核对供应商名称列及商品名称列。")
    updated = deepcopy(report)
    matched = 0
    for record in updated["records"]:
        key = " ".join(normalize("NFC", record.get("product", "")).split())
        suppliers = aliases.get(key, set())
        existing = record.get("supplier", "")
        if len(suppliers) > 1:
            if not existing or existing not in suppliers:
                raise ValueError(f"商品「{key}」在原表属于多个供应商，请先核对归属。")
        elif suppliers:
            supplier = next(iter(suppliers))
            if existing and existing != supplier:
                raise ValueError(f"商品「{key}」的现有供应商与原表不一致。")
            if record.get("supplier_conflict"):
                raise ValueError(f"商品「{key}」已标记供应商冲突，请先核对归属。")
            if not existing:
                record["supplier"] = supplier
                matched += 1
    return updated, matched


def enrich_manifest(path, source):
    """锁定并核对原文件后原子替换。保留补齐前的本地备份。"""
    path = Path(path).resolve(strict=True)
    source = Path(source).resolve(strict=True)
    if any(part.casefold() == ".feishu-edge-profile" for part in (*path.parts, *source.parts)):
        raise ValueError("请选择分类清单和原采购表。")
    sheets = read_xlsx(source)
    lock = path.with_name("manifest.supplier.lock")
    temporary = None
    lock_stream = lock.open("x", encoding="utf-8")
    try:
        original = path.read_bytes()
        report = json.loads(original.decode("utf-8-sig"))
        updated, matched = with_suppliers(report, sheets)
        if not matched:
            return updated, 0
        data = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(prefix=".manifest-suppliers-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise ValueError("分类清单在补齐期间发生变化，请重新读取。")
        backup = path.with_name("manifest.before-suppliers-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        with backup.open("xb") as stream:
            stream.write(original)
        os.replace(temporary, path)
        temporary = None
        return updated, matched
    finally:
        lock_stream.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()
