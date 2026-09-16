"""Local, site-scoped operation audit records with privacy-safe summaries."""
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path


_LOCK = threading.RLock()
_SECRET_PARTS = ("password", "passwd", "cookie", "token", "formcheck", "captcha")
_LARGE_PARTS = ("content", "body", "html", "description")
_MESSAGE_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|cookie|token|formcheck|authorization)\b\s*[:=]\s*)"
    r"([^\s,;&]+)")


def get_base_dir():
    override = os.environ.get("PBOOT_PUBLISHER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _db_path():
    return get_base_dir() / "operation_audit.db"


def _connection():
    conn = sqlite3.connect(str(_db_path()), timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        site TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT,
        status TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        message TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS audit_site_time ON audit_records(site, created_at DESC)")
    conn.commit()
    return conn


def _text_summary(value, key=""):
    text = str(value or "")
    low = str(key or "").lower()
    if any(part in low for part in _SECRET_PARTS):
        return "[已隐藏]"
    if any(part in low for part in _LARGE_PARTS) or len(text) > 300:
        return {"length": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                "preview": text[:80]}
    return text


def _safe(value, key=""):
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item, key) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text_summary(value, key)


def _safe_message(value):
    text = str(value or "")[:500]
    return _MESSAGE_SECRET_RE.sub(r"\1[已隐藏]", text)


def record_audit(site, action, target_type, target_id="", *, status="success",
                 before=None, after=None, message=""):
    site = str(site or "").strip()
    if not site:
        return 0
    payload = (time.time(), site, str(action or ""), str(target_type or ""),
               str(target_id or ""), str(status or ""),
               json.dumps(_safe(before or {}), ensure_ascii=False, sort_keys=True),
               json.dumps(_safe(after or {}), ensure_ascii=False, sort_keys=True),
               _safe_message(message))
    try:
        with _LOCK, closing(_connection()) as conn:
            cursor = conn.execute(
                "INSERT INTO audit_records(created_at,site,action,target_type,target_id,status,"
                "before_json,after_json,message) VALUES (?,?,?,?,?,?,?,?,?)", payload)
            conn.commit()
            return int(cursor.lastrowid)
    except (OSError, sqlite3.Error):
        # Auditing is secondary: a locked/read-only local database must never
        # turn a verified CMS write into a false failure or leave a task busy.
        return 0


def list_audit(site, limit=200):
    limit = max(1, min(2000, int(limit or 200)))
    with _LOCK, closing(_connection()) as conn:
        rows = conn.execute(
            "SELECT id,created_at,action,target_type,target_id,status,before_json,after_json,message "
            "FROM audit_records WHERE site=? ORDER BY id DESC LIMIT ?",
            (str(site or ""), limit)).fetchall()
    result = []
    for row in rows:
        result.append({
            "id": row[0], "created_at": row[1], "action": row[2],
            "target_type": row[3], "target_id": row[4], "status": row[5],
            "before": json.loads(row[6] or "{}"),
            "after": json.loads(row[7] or "{}"), "message": row[8],
        })
    return result


def export_audit_csv(site):
    rows = list_audit(site, 2000)
    folder = get_base_dir() / "audit_exports"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = folder / f"operation_audit_{str(site or 'site')[:20]}_{stamp}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["时间", "操作", "对象", "对象ID", "结果", "修改前摘要", "修改后摘要", "后台信息"])
        for row in rows:
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"])),
                row["action"], row["target_type"], row["target_id"], row["status"],
                json.dumps(row["before"], ensure_ascii=False),
                json.dumps(row["after"], ensure_ascii=False), row["message"],
            ])
    return str(path), len(rows)
