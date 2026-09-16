"""Durable, privacy-safe journal for requests whose final result is unknown.

The normal audit table records completed operations.  This separate journal
keeps a pending marker before a potentially long POST starts, so a process
restart cannot silently turn "request may have reached the server" into a
fresh retry.  It intentionally stores only hashes/short summaries.
"""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from audit_log import get_base_dir


_LOCK = threading.RLock()


def _db_path():
    return get_base_dir() / "operation_journal.db"


def _safe(value, key=""):
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item, key) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    low = str(key or "").lower()
    if any(token in low for token in ("password", "passwd", "cookie", "token", "captcha", "formcheck")):
        return "[已隐藏]"
    if any(token in low for token in ("content", "html", "body", "description")) or len(text) > 300:
        return {"length": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}
    return text[:500]


def _connection():
    conn = sqlite3.connect(str(_db_path()), timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS operation_journal (
        operation_id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        site TEXT NOT NULL,
        tab_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        state TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        note TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS journal_site_state ON operation_journal(site,state,updated_at DESC)")
    conn.commit()
    return conn


def begin(site, tab_id, action, target="", summary=None):
    operation_id = uuid.uuid4().hex
    now = time.time()
    payload = json.dumps(_safe(summary or {}), ensure_ascii=False, sort_keys=True)
    try:
        with _LOCK, closing(_connection()) as conn:
            conn.execute("INSERT INTO operation_journal VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (operation_id, now, now, str(site or ""), str(tab_id or ""),
                          str(action or ""), str(target or ""), "pending", payload, "{}", ""))
            conn.commit()
        return operation_id
    except (OSError, sqlite3.Error):
        return ""


def finish(operation_id, state, result=None, note=""):
    if not operation_id:
        return False
    allowed = {"success", "failed", "cancelled", "unknown", "review", "resolved"}
    state = str(state or "unknown").lower()
    if state not in allowed:
        state = "unknown"
    try:
        with _LOCK, closing(_connection()) as conn:
            cur = conn.execute("UPDATE operation_journal SET updated_at=?, state=?, result_json=?, note=? WHERE operation_id=?",
                               (time.time(), state,
                                json.dumps(_safe(result or {}), ensure_ascii=False, sort_keys=True),
                                str(note or "")[:500], operation_id))
            conn.commit()
            return bool(cur.rowcount)
    except (OSError, sqlite3.Error):
        return False


def list_pending(site=""):
    try:
        with _LOCK, closing(_connection()) as conn:
            if site:
                rows = conn.execute("SELECT operation_id,created_at,updated_at,site,tab_id,action,target,state,summary_json,result_json,note FROM operation_journal WHERE site=? AND state IN ('pending','review') ORDER BY created_at DESC",
                                    (str(site),)).fetchall()
            else:
                rows = conn.execute("SELECT operation_id,created_at,updated_at,site,tab_id,action,target,state,summary_json,result_json,note FROM operation_journal WHERE state IN ('pending','review') ORDER BY created_at DESC").fetchall()
    except (OSError, sqlite3.Error):
        return []
    result = []
    for row in rows:
        result.append({"operation_id": row[0], "created_at": row[1], "updated_at": row[2],
                       "site": row[3], "tab_id": row[4], "action": row[5],
                       "target": row[6], "state": row[7],
                       "summary": json.loads(row[8] or "{}"),
                       "result": json.loads(row[9] or "{}"), "note": row[10]})
    return result


def resolve(operation_id, note=""):
    # "review" means the operation still needs attention and must survive a
    # restart. Once the user explicitly confirms the backend state, use a
    # distinct terminal state so it disappears from the pending/review panel.
    return finish(operation_id, "resolved", {}, note or "用户已确认需核对后台")
