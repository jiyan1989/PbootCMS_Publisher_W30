"""Bounded metadata for browser ``File`` objects materialized by the bridge.

The WebView can provide a useful ``File.type`` even when a dropped file has
no extension and no short magic signature (fonts and some legacy media are
common examples).  Once the bytes are copied to a temporary path that MIME
would otherwise be lost.  This small process-local registry carries only the
validated MIME hint; it never stores file contents or a path in a request.
"""
from collections import OrderedDict
import os
import threading


_MAX_RECORDS = 4096
_LOCK = threading.RLock()
_VALUES = OrderedDict()


def _key(path):
    value = os.path.abspath(str(path or "").strip()) if path else ""
    return os.path.normcase(value) if value else ""


def remember_declared_mime(path, mime):
    """Remember a browser-declared MIME for one materialized file path."""
    key = _key(path)
    value = str(mime or "").split(";", 1)[0].strip().lower()
    if (not key or not value or "/" not in value or
            any(ord(char) < 0x20 or char in "\r\n;" for char in value)):
        return False
    with _LOCK:
        _VALUES.pop(key, None)
        _VALUES[key] = value
        while len(_VALUES) > _MAX_RECORDS:
            _VALUES.popitem(last=False)
    return True


def declared_mime(path):
    """Return the remembered browser MIME, if the path is still registered."""
    key = _key(path)
    if not key:
        return ""
    with _LOCK:
        value = _VALUES.get(key, "")
        if value:
            _VALUES.move_to_end(key)
        return value


def forget_declared_mime(path):
    key = _key(path)
    if not key:
        return
    with _LOCK:
        _VALUES.pop(key, None)
