"""Windows CF_HTML 与 rowspan/colspan 解析；不读取其他剪贴板格式。"""
import base64
import re
from html.parser import HTMLParser
from .core import Asset, Cell, Sheet


def read_windows_html():
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = wintypes.HANDLE
    k.GlobalLock.argtypes = [wintypes.HANDLE]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalSize.argtypes = [wintypes.HANDLE]
    k.GlobalSize.restype = ctypes.c_size_t
    k.GlobalUnlock.argtypes = [wintypes.HANDLE]
    fmt = u.RegisterClipboardFormatW('HTML Format')
    if not u.OpenClipboard(None): raise RuntimeError('clipboard_busy')
    try:
        handle = u.GetClipboardData(fmt)
        if not handle: raise ValueError('clipboard_has_no_html')
        pointer = k.GlobalLock(handle)
        if not pointer: raise RuntimeError('clipboard_lock_failed')
        try: return ctypes.string_at(pointer, k.GlobalSize(handle))
        finally: k.GlobalUnlock(handle)
    finally: u.CloseClipboard()


def fragment(raw):
    if isinstance(raw, str): raw = raw.encode('utf-8')
    # CF_HTML offsets 是字节偏移，不能先按 Unicode 字符切片。
    a = re.search(rb'StartFragment:(\d+)', raw[:4096])
    b = re.search(rb'EndFragment:(\d+)', raw[:4096])
    if a and b and 0 <= int(a[1]) < int(b[1]) <= len(raw): raw = raw[int(a[1]):int(b[1])]
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
            rs, cs = int(attrs.get('rowspan',1)), int(attrs.get('colspan',1))
            if not 1 <= rs <= 10000 or not 1 <= cs <= 16384: raise ValueError('Invalid span')
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
                    head, body = url.split(',',1)
                    if ';base64' not in head: raise ValueError('Only base64 data URLs supported')
                    data = base64.b64decode(body, validate=True)
                self.current.assets.append(Asset(url,data))
            if tag == 'br': self.current.text += '\n'

    def handle_endtag(self, tag):
        if tag in ('td','th'): self.current = None

    def handle_data(self, data):
        if self.current is not None: self.current.text += data


def read_html(raw, start_row=2, start_col=20):
    parser = TableParser(start_row,start_col)
    parser.feed(fragment(raw))
    if not parser.sheet.cells: raise ValueError('No HTML table cells; canvas/private clipboard format is not supported')
    return [parser.sheet]
