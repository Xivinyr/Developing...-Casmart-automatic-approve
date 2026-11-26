"""仅用 Windows 当前用户的 DPAPI 保存登录信息，不增加第三方依赖。"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


FILE_HEADER = b"CASMART-DPAPI\x01"
SERVICES = ("casmart", "smu")


def credentials_path():
    """凭证放在用户应用数据目录，独立于源码与浏览器配置。"""
    app_data = os.environ.get("LOCALAPPDATA")
    if not app_data:
        raise OSError("无法找到 Windows 本地应用数据目录。")
    return Path(app_data) / "CasmartAuto" / "credentials.bin"


def _crypt(data, *, decrypt=False):
    if os.name != "nt":
        raise OSError("保存密码功能需要 Windows DPAPI。")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    # 解密时不请求数据描述；加密也不附带任何账号或密码描述。
    operation.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    ]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    try:
        # 不使用 LOCAL_MACHINE 标志：绑定当前 Windows 用户，禁止弹出系统交互框。
        if not operation(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)):
            raise OSError("Windows 无法加密或解密保存的登录信息。")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(data))
        if output.pbData:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel32.LocalFree(output.pbData)


def _validate(accounts):
    if not isinstance(accounts, dict) or any(name not in SERVICES for name in accounts):
        raise ValueError("登录信息格式不正确。")
    for account in accounts.values():
        if not isinstance(account, dict) or set(account) != {"username", "password"}:
            raise ValueError("登录信息格式不正确。")
        if not all(isinstance(value, str) and value for value in account.values()):
            raise ValueError("账号密码不能为空。")
    return accounts


def load_credentials(path=None):
    path = Path(path) if path is not None else credentials_path()
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {}
    if not content.startswith(FILE_HEADER):
        raise ValueError("保存的登录信息格式不正确。")
    try:
        accounts = json.loads(_crypt(content[len(FILE_HEADER):], decrypt=True).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("保存的登录信息无法读取。") from exc
    return _validate(accounts)


def save_credentials(accounts, path=None):
    """先加密，再原子替换文件；不将明文写入临时文件。"""
    accounts = _validate(accounts)
    encrypted = FILE_HEADER + _crypt(json.dumps(accounts, ensure_ascii=False).encode("utf-8"))
    path = Path(path) if path is not None else credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="credentials-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encrypted)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def delete_credentials(path=None):
    path = Path(path) if path is not None else credentials_path()
    path.unlink(missing_ok=True)
