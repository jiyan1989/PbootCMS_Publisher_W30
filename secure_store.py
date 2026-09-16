# -*- coding: utf-8 -*-
"""Windows 安全存储：使用当前用户 DPAPI 加密密码与会话数据。"""

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

_PREFIX = "dpapi:"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅适用于 Windows")
    in_blob, in_buf = _blob(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    if decrypt:
        ok = fn(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    else:
        ok = fn(ctypes.byref(in_blob), "PbootCMS Publisher", None, None, None, 0,
                ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect_text(value: str) -> str:
    """加密文本。非 Windows 不持久化敏感值。"""
    if not value or sys.platform != "win32":
        return ""
    encrypted = _dpapi(value.encode("utf-8"))
    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_text(value: str) -> str:
    if not value or not value.startswith(_PREFIX):
        return ""
    raw = base64.b64decode(value[len(_PREFIX):])
    return _dpapi(raw, decrypt=True).decode("utf-8")


def save_private_json(path, payload: dict) -> None:
    """Windows 下加密整个 JSON；其他平台仅写入 0600 权限文件。"""
    path = Path(path)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if sys.platform == "win32":
        wrapper = {"format": "dpapi-v1", "payload": protect_text(raw.decode("utf-8"))}
    else:
        wrapper = payload
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_private_json(path) -> dict:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("format") == "dpapi-v1":
        return json.loads(unprotect_text(data.get("payload", "")))
    return data
