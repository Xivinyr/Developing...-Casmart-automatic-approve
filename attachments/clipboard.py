"""Windows CF_HTML 与 rowspan/colspan 解析；不读取其他剪贴板格式。"""
import base64
import os
import re
import time
from html.parser import HTMLParser
from .core import Asset, Cell, Sheet


class ClipboardInputError(ValueError):
    """可向用户解释的剪贴板错误，代码不包含复制的数据。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def read_windows_html():
    # 浏览器可能稍后才发布 HTML 格式，或剪贴板暂时被其他程序占用。
    for attempt in range(8):
        try:
            return _read_windows_html_once()
        except ClipboardInputError as exc:
            if exc.code not in ('clipboard_busy', 'clipboard_has_no_html') or attempt == 7:
                raise
            time.sleep(0.15)


def _read_windows_html_once():
    if os.name != 'nt':
        raise ClipboardInputError('clipboard_not_supported')

    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    fmt = user32.RegisterClipboardFormatW('HTML Format')
    if not fmt:
        raise ClipboardInputError('clipboard_not_supported')
    if not user32.OpenClipboard(None):
        raise ClipboardInputError('clipboard_busy')
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            raise ClipboardInputError('clipboard_has_no_html')
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardInputError('clipboard_lock_failed')
        try:
            return ctypes.string_at(pointer, kernel32.GlobalSize(handle))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def fragment(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    # 偏移以整个 CF_HTML 的字节计数；只在 HTML 之前读取头部。
    header = raw[:4096].split(b'<', 1)[0]
    start = re.search(rb'(?:\A|[\r\n])(?:\xef\xbb\xbf)?StartFragment:[ \t]*(\d{1,12})(?=\s|$)', header)
    end = re.search(rb'(?:\A|[\r\n])EndFragment:[ \t]*(\d{1,12})(?=\s|$)', header)
    if start and end and len(header) <= int(start[1]) < int(end[1]) <= len(raw):
        raw = raw[int(start[1]):int(end[1])]
    else:
        # 偏移不可用时仅接受明确的片段标记，避免误取前后的表格。
        marker_start = re.search(rb'<!--\s*StartFragment\s*-->', raw, re.I)
        marker_end = re.search(rb'<!--\s*EndFragment\s*-->', raw, re.I)
        if marker_start and marker_end and marker_start.end() < marker_end.start():
            raw = raw[marker_start.end():marker_end.start()]
        elif marker_start or marker_end or re.search(rb'(?:\A|[\r\n])(?:\xef\xbb\xbf)?(?:Version|StartHTML|StartFragment|EndFragment):', header):
            raise ClipboardInputError('invalid_cf_html')
    return raw.decode('utf-8-sig', errors='replace').rstrip('\x00')


class TableParser(HTMLParser):
    def __init__(self, start_row=2, start_col=20):
        super().__init__(convert_charrefs=True)
        self.sheet = Sheet('clipboard')
        self.row = start_row-1
        self.col = start_col
        self.start_col = start_col
        self.current = None
        self.occupied = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self.row += 1
            self.col = self.start_col
        if tag in ('td','th'):
            while (self.row,self.col) in self.occupied: self.col += 1
            try:
                rs, cs = int(attrs.get('rowspan', 1)), int(attrs.get('colspan', 1))
            except (TypeError, ValueError):
                raise ClipboardInputError('clipboard_invalid_span') from None
            if not 1 <= rs <= 10000 or not 1 <= cs <= 16384:
                raise ClipboardInputError('clipboard_invalid_span')
            self.current = Cell()
            self.sheet.cells[self.row,self.col] = self.current
            if rs > 1 or cs > 1:
                self.sheet.merges.append((self.row,self.col,self.row+rs-1,self.col+cs-1))
                for r in range(self.row,self.row+rs):
                    for c in range(self.col,self.col+cs): self.occupied.add((r,c))
            self.col += cs
        elif self.current is not None:
            url = attrs.get('src') if tag == 'img' else attrs.get('href') if tag == 'a' else None
            if url:
                data = None
                if url.startswith('data:'):
                    head, sep, body = url.partition(',')
                    if not sep or ';base64' not in head:
                        raise ClipboardInputError('clipboard_invalid_attachment')
                    try:
                        data = base64.b64decode(body, validate=True)
                    except ValueError:
                        raise ClipboardInputError('clipboard_invalid_attachment') from None
                self.current.assets.append(Asset(url,data))
            if tag == 'br': self.current.text += '\n'

    def handle_endtag(self, tag):
        if tag in ('td','th'): self.current = None

    def handle_data(self, data):
        if self.current is not None: self.current.text += data


def read_html(raw, start_row=2, start_col=20):
    parser = TableParser(start_row,start_col)
    try:
        parser.feed(fragment(raw))
    except AssertionError:
        raise ClipboardInputError('invalid_cf_html') from None
    if not parser.sheet.cells:
        raise ClipboardInputError('clipboard_no_table')
    return [parser.sheet]
