"""从明确指定的本地目录读取发票，不使用表格文字拼接路径。"""

import os
from pathlib import Path, PureWindowsPath
import re
import stat
import unicodedata


MAX_INVOICE_BYTES = 30 * 1024 * 1024
PDF_HEADER = b'%PDF-'


def invoice_key(name):
    """只接受安全、完整的 PDF 文件名，不裁剪或猜测名称。"""
    if (len(name) <= 4 or not name.casefold().endswith('.pdf')
            or name.strip() != name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or PureWindowsPath(name).is_reserved()):
        return None
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', name).casefold())


def _is_reparse_point(info):
    return bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def index_invoices(directory):
    """仅列出指定目录的直属条目，同名条目保留以检测歧义。"""
    directory = Path(os.path.abspath(directory))
    for component in (*reversed(directory.parents), directory):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise OSError('invoice_folder_unavailable')

    matches = {}
    for entry in directory.iterdir():
        key = invoice_key(entry.name)
        if key is not None:
            matches.setdefault(key, []).append(entry)
    return matches


def read_invoice(path):
    """仅返回普通文件中的 PDF 字节，拒绝链接、过大或签名不符的文件。"""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or _is_reparse_point(before)
            or not len(PDF_HEADER) <= before.st_size <= MAX_INVOICE_BYTES):
        raise ValueError('invoice_local_invalid')

    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(before, opened):
            raise ValueError('invoice_local_invalid')
        data = stream.read(MAX_INVOICE_BYTES + 1)
    if len(data) > MAX_INVOICE_BYTES or not data.startswith(PDF_HEADER):
        raise ValueError('invoice_local_invalid')
    return data
