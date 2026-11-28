"""按商品名和分类文件夹核对签收材料。"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from attachments.core import DEFAULT, extension, safe
from order_list import product_key


REQUIRED = tuple(DEFAULT["required"])
MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp"}


class ReceiptStopped(RuntimeError):
    """签收缺少足够的单据、文件或页面证据。"""


@dataclass(frozen=True)
class Attachment:
    category: str
    path: Path
    sha256: str
    size: int

    def verify(self):
        """上传前再次核对，避免用户预览期间文件被替换。"""
        try:
            with self.path.open("rb") as stream:
                head = stream.read(16)
                if extension(head) != self.path.suffix.lower():
                    raise ValueError
                digest = hashlib.sha256(head)
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                if stream.tell() != self.size or digest.hexdigest() != self.sha256:
                    raise ValueError
        except (OSError, ValueError) as exc:
            raise ReceiptStopped(f"附件内容与分类清单不一致：{self.path.name}。请重新分类或核对文件。") from exc


@dataclass(frozen=True)
class ReceiptPlan:
    folder: Path
    product: str
    files: tuple
    warnings: int
    supplier: str = ""

    @property
    def names(self):
        return tuple(file.path.name for file in self.files)


@dataclass(frozen=True)
class SupplierPlan:
    supplier: str
    products: tuple[ReceiptPlan, ...]
    files: tuple[Attachment, ...]

    @property
    def names(self):
        return tuple(file.path.name for file in self.files)


def local_path(path):
    resolved = Path(path).resolve(strict=True)
    if any(part.casefold() == ".feishu-edge-profile" for part in resolved.parts):
        raise ReceiptStopped("请选择附件分类输出目录中的文件。")
    return resolved


def load_manifest(path):
    try:
        path = local_path(path)
        if path.is_dir():
            folder = path
            path = local_path(folder / "manifest.json")
            if path.parent != folder:
                raise ValueError
        if path.name != "manifest.json" or not path.is_file():
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError
        records = data["records"]
        if not isinstance(records, list) or not records or not all(isinstance(row, dict) for row in records):
            raise ValueError
        if not all(isinstance(data.get(key, []), list) for key in ("errors", "warnings")):
            raise ValueError
    except (OSError, ValueError, KeyError) as exc:
        raise ReceiptStopped("无法读取分类输出中的 manifest.json，或清单没有记录。") from exc
    return path, data


def manifest_file(root, item, sheet, folder_name):
    """只接受本次分类目录下的清单路径，校验实际文件格式、大小和哈希。"""
    try:
        ref, category, digest, size = (item[key] for key in ("path", "category", "sha256", "bytes"))
        if re.search(r'[:\x00-\x1f]', ref) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError
        relative = PurePosixPath(ref.replace("\\", "/"))
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 3
                or not relative.parts[0].startswith(f"{sheet:02d}-")
                or relative.parts[1] != folder_name):
            raise ValueError
        path = local_path(root / Path(*relative.parts))
        folder = root / relative.parts[0] / folder_name
        if (not path.is_relative_to(folder) or not path.is_file() or path.suffix.lower() not in MIME
                or category not in REQUIRED or type(size) is not int or size <= 0):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ReceiptStopped("清单中有无法用于签收的附件路径或分类。") from exc
    attachment = Attachment(category, path, digest, size)
    attachment.verify()
    return attachment, folder


def catalog(report):
    """同名记录即使附件尚未补齐，也参与重名判断。"""
    groups = {}
    for record in report["records"]:
        name = record.get("product")
        key = product_key(name) if isinstance(name, str) else ""
        if key and key != "未识别商品":
            groups.setdefault(key, []).append(record)
    return groups


def record_supplier(record):
    value = record.get("supplier")
    return product_key(value) if isinstance(value, str) else ""


def build_plan(manifest, report, product, groups=None, *, supplier=None):
    groups = catalog(report) if groups is None else groups
    selected = groups.get(product_key(product), ())
    if not selected:
        raise ReceiptStopped("分类批次没有同名商品文件夹。")
    if supplier:
        tagged = [record for record in selected if record_supplier(record)]
        if tagged:
            selected = [record for record in tagged if record_supplier(record) == product_key(supplier)]
            if not selected:
                raise ReceiptStopped("同名商品的文件夹属于其他供应商。")
    if len(selected) != 1:
        raise ReceiptStopped("分类批次有同名商品文件夹，请先核对。")
    record = selected[0]
    if record.get("supplier_conflict"):
        raise ReceiptStopped("分类记录跨多个供应商，无法确认附件归属。")
    sheet, row = record.get("sheet_index"), record.get("row")
    rows, name, entries = record.get("rows"), record["product"], record.get("files")
    if type(sheet) is not int or sheet < 1 or type(row) is not int or row < 1:
        raise ReceiptStopped("商品文件夹缺少有效的分类来源。")
    if (not isinstance(rows, list) or not rows or not all(type(r) is int and r > 0 for r in rows)
            or not isinstance(entries, list) or not entries):
        raise ReceiptStopped("分类记录不完整，无法核对签收附件。")
    if record.get("missing"):
        raise ReceiptStopped("该记录有缺失附件，请先在附件分类任务补齐后选择新的清单。")
    if any(isinstance(error, dict) and error.get("sheet_index") == sheet
           and error.get("row") in {*rows, row} for error in report.get("errors", [])):
        raise ReceiptStopped("该记录有附件下载或分类错误，请先处理错误。")
    # 记录的商品名、实际记录目录名和附件路径必须指向同一商品。
    folder_name = f"row-{row:06d}-{safe(name)}"
    located = [manifest_file(manifest.parent, entry, sheet, folder_name) for entry in entries]
    folders = {folder for _, folder in located}
    if len(folders) != 1:
        raise ReceiptStopped("该商品的附件分属多个文件夹，需重新核对分类批次。")
    files = tuple(file for file, _ in located)
    if missing := set(REQUIRED).difference(file.category for file in files):
        raise ReceiptStopped("该记录缺少签收所需附件：" + "、".join(sorted(missing)) + "。")
    if len({file.path.name.casefold() for file in files}) != len(files):
        raise ReceiptStopped("待上传文件重名，无法逐个核对上传结果。")
    return ReceiptPlan(folders.pop(), name, files, len(report.get("warnings", [])), record_supplier(record))


def build_supplier_plan(manifest, report, supplier, products):
    """完整核对这张合并签收单的所有商品后，再汇总要上传的材料。"""
    if not products or any(not product_key(name) for name in products):
        raise ReceiptStopped("签收单没有完整的商品名称。")
    if len({product_key(name) for name in products}) != len(products):
        raise ReceiptStopped("该供应商签收页有同名商品，无法分清每个商品对应的文件夹。")
    groups = catalog(report)
    matched = []
    for name in products:
        try:
            matched.append(build_plan(manifest, report, name, groups, supplier=supplier))
        except ReceiptStopped as exc:
            raise ReceiptStopped(f"商品「{name}」无法匹配材料。{exc}") from exc
    files = {}
    for item in matched:
        for file in item.files:
            # 同一家、同一张签收单里的同类同内容文件只需上传一次。
            files.setdefault((file.category, file.sha256), file)
    unique = tuple(files.values())
    if len({file.path.name.casefold() for file in unique}) != len(unique):
        raise ReceiptStopped("签收单有同名但内容不同的附件，无法逐个核对上传结果。")
    return SupplierPlan(supplier, tuple(matched), unique)


def accepts_file(accept, attachment):
    """用实际分类格式核对网页 accept，PDF 拒绝时不转换或重命名。"""
    types = {item.strip().lower() for item in (accept or "").split(",") if item.strip()}
    suffix = attachment.path.suffix.lower()
    mime = MIME[suffix]
    return not types or bool(types.intersection({suffix, mime, mime.split("/")[0] + "/*", "*/*"}))
