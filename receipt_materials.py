"""将一条已分类的附件记录和用户指定的 Casmart 单号绑定。"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from attachments.core import DEFAULT, extension
from order_delete import ID_PATTERN


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
    manifest: Path
    sheet: int
    row: int
    rows: tuple
    product: str
    order_id: str
    files: tuple
    warnings: int

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


def manifest_file(root, item, sheet, row):
    """只接受本次分类目录下的清单路径，校验实际文件格式、大小和哈希。"""
    try:
        ref, category, digest, size = (item[key] for key in ("path", "category", "sha256", "bytes"))
        if re.search(r'[:\x00-\x1f]', ref) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError
        relative = PurePosixPath(ref.replace("\\", "/"))
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 3
                or not relative.parts[0].startswith(f"{sheet:02d}-")
                or not relative.parts[1].startswith(f"row-{row:06d}-")):
            raise ValueError
        path = local_path(root / Path(*relative.parts))
        if (not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in MIME
                or category not in REQUIRED or type(size) is not int or size <= 0):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ReceiptStopped("清单中有无法用于签收的附件路径或分类。") from exc
    attachment = Attachment(category, path, digest, size)
    attachment.verify()
    return attachment


def build_plan(manifest, report, sheet, row, order_id):
    order_id = order_id.strip()
    if not ID_PATTERN.fullmatch(order_id):
        raise ReceiptStopped("请输入完整 Casmart 单号。编号须为 2–100 位字母、数字或 . _ / -。")
    if type(sheet) is not int or sheet < 1 or type(row) is not int or row < 1:
        raise ReceiptStopped("请选择一个明确的工作表和记录起始行。")
    selected = [r for r in report["records"] if (r.get("sheet_index"), r.get("row")) == (sheet, row)]
    if len(selected) != 1:
        raise ReceiptStopped("无法按表序号和起始行唯一找到分类记录。")
    record = selected[0]
    rows, product, entries = record.get("rows"), record.get("product"), record.get("files")
    if (not isinstance(rows, list) or not rows or not all(type(r) is int and r > 0 for r in rows)
            or not isinstance(product, str) or not isinstance(entries, list) or not entries):
        raise ReceiptStopped("分类记录不完整，无法核对签收附件。")
    if record.get("missing"):
        raise ReceiptStopped("该记录有缺失附件，请先在附件分类任务补齐后选择新的清单。")
    if any(isinstance(error, dict) and error.get("sheet_index") == sheet
           and error.get("row") in {*rows, row} for error in report.get("errors", [])):
        raise ReceiptStopped("该记录有附件下载或分类错误，请先处理错误。")
    files = tuple(manifest_file(manifest.parent, entry, sheet, row) for entry in entries)
    if missing := set(REQUIRED).difference(file.category for file in files):
        raise ReceiptStopped("该记录缺少签收所需附件：" + "、".join(sorted(missing)) + "。")
    if len({file.path.name.casefold() for file in files}) != len(files):
        raise ReceiptStopped("待上传文件重名，无法逐个核对上传结果。")
    return ReceiptPlan(manifest, sheet, row, tuple(rows), product, order_id, files,
                       len(report.get("warnings", [])))


def accepts_file(accept, attachment):
    """用实际分类格式核对网页 accept，PDF 拒绝时不转换或重命名。"""
    types = {item.strip().lower() for item in (accept or "").split(",") if item.strip()}
    suffix = attachment.path.suffix.lower()
    mime = MIME[suffix]
    return not types or bool(types.intersection({suffix, mime, mime.split("/")[0] + "/*", "*/*"}))
