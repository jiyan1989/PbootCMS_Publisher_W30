"""SQLite product repository isolated by site ID.

The repository deliberately keeps *catalogue sync metadata* separate from a
product row's ``updated_at`` value.  A local model/price edit changes one row,
but it must not make the UI claim that the whole remote catalogue was synced.
"""
import os, re, sqlite3, sys, time
from contextlib import closing
from pathlib import Path
from logger import debug_log

def get_base_dir():
    override = os.environ.get("PBOOT_PUBLISHER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys,"frozen",False): return Path(sys.executable).parent
    return Path(__file__).parent

def _db_path():
    return str(get_base_dir() / "products_cache.db")

def _db_conn():
    # A short busy timeout prevents an occasional "database is locked" error when
    # a previous UI callback has not yet released its connection.
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS products (
        site TEXT,
        id TEXT,
        cat_name TEXT,
        title TEXT,
        xinghao TEXT,
        jiage TEXT,
        edit_url TEXT,
        front_url TEXT,
        xinghao_field TEXT,
        jiage_field TEXT,
        mcode TEXT,
        updated_at REAL,
        PRIMARY KEY(site, id))""")
    # 旧库迁移：缺 front_url 列则补上（不丢历史数据）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
    if "front_url" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN front_url TEXT")
    if "mcode" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN mcode TEXT")
        # 旧缓存中的真实编辑链接通常已携带 mcode，只从该
        # 明确数据源回填，绝不对缺失值猜测为 3。
        for rowid, edit_url in conn.execute(
                "SELECT rowid,edit_url FROM products WHERE mcode IS NULL OR mcode=''"
                ).fetchall():
            match = re.search(
                r"(?:^|[/&?])mcode(?:/|=)(\d+)(?:[/&#?]|$)",
                str(edit_url or ""), re.I)
            if match:
                conn.execute("UPDATE products SET mcode=? WHERE rowid=?",
                             (match.group(1), rowid))
    conn.execute("""CREATE TABLE IF NOT EXISTS product_sync_meta (
        site TEXT PRIMARY KEY,
        last_sync_at REAL,
        last_sync_mode TEXT,
        last_catalog_sync_at REAL,
        last_full_refresh_at REAL,
        remote_total INTEGER,
        requested INTEGER,
        refreshed INTEGER,
        added INTEGER,
        updated INTEGER,
        unchanged INTEGER,
        deleted INTEGER)""")
    # DDL/旧数据回填立即落盘；即使调用方只读后关闭连接，
    # 也不会在下次启动重复迁移或丢失新列。
    conn.commit()
    return conn


_PRODUCT_COLUMNS = (
    "cat_name", "title", "xinghao", "jiage", "edit_url", "front_url",
    "xinghao_field", "jiage_field", "mcode",
)


def _clean_id(value):
    return str(value or "").strip()


def _product_values(item, existing=None):
    """Return the stored product tuple, preserving proven URL/mcode evidence."""
    front_url = str(item.get("front_url", "") or "").strip()
    mcode = str(item.get("mcode", "") or "").strip()
    if existing:
        # A temporary category lookup failure must not erase a previously
        # verified front URL.  The same applies to mcode, which is needed for
        # every later safe edit or targeted repair.
        if not front_url:
            front_url = str(existing[5] or "")
        if not mcode:
            mcode = str(existing[8] or "").strip()
    return (
        item.get("cat_name", ""), item.get("title", ""),
        item.get("xinghao", ""), item.get("jiage", ""),
        item.get("edit_url", ""), front_url,
        item.get("xinghao_field", "ext_xinghao"),
        item.get("jiage_field", "ext_jiage"), mcode,
    )


def _validate_product_batch(products, *, allow_empty=False):
    products = list(products or [])
    if not products and not allow_empty:
        raise ValueError("拒绝用空结果覆盖现有产品缓存")
    ids = [_clean_id(item.get("id")) for item in products]
    if any(not pid for pid in ids) or len(ids) != len(set(ids)):
        raise ValueError("同步结果包含空ID或重复ID")
    return products, ids


def _sync_stats_payload(mode, complete, selected, total, added, updated,
                        unchanged, deleted, requested, refreshed, synced_at):
    return {
        "mode": mode,
        "complete": bool(complete),
        "selected": int(selected),
        "total": int(total),
        "added": int(added),
        "updated": int(updated),
        "unchanged": int(unchanged),
        "deleted": int(deleted),
        "requested": int(requested),
        "refreshed": int(refreshed),
        "synced_at": float(synced_at),
    }


def db_commit_product_sync(site, products, *, complete, mode="incremental",
                           remote_total=None, requested=None, refreshed=None,
                           empty_confirmed=False):
    """Atomically store one successful product-sync result.

    ``complete=True`` means the remote paginator proved a complete catalogue
    snapshot, so missing IDs may be deleted.  ``complete=False`` is used by
    targeted repair and can only upsert the explicitly returned rows.

    Returns reconciliation counts plus sync metadata.  Any validation or
    SQLite failure is raised; callers can therefore avoid publishing a false
    "sync succeeded" state.
    """
    site = str(site or "").strip()
    if not site:
        raise ValueError("site is required")
    mode = str(mode or "").strip().lower()
    if mode not in ("full", "incremental", "repair"):
        raise ValueError(f"未知产品同步模式: {mode or '(empty)'}")
    products, ids = _validate_product_batch(
        products, allow_empty=(not complete and mode == "repair") or
        (complete and empty_confirmed and remote_total == 0))
    if complete and remote_total is not None and int(remote_total) != len(products):
        raise ValueError("完整产品快照数量与 remote_total 不一致")
    if requested is None:
        requested = len(products)
    if refreshed is None:
        refreshed = len(products)
    try:
        requested = max(0, int(requested))
        refreshed = max(0, int(refreshed))
    except (TypeError, ValueError) as exc:
        raise ValueError("requested/refreshed 必须是非负整数") from exc
    synced_at = time.time()

    with closing(_db_conn()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_rows = conn.execute(
                "SELECT id,cat_name,title,xinghao,jiage,edit_url,front_url,"
                "xinghao_field,jiage_field,mcode FROM products WHERE site=?",
                (site,)).fetchall()
            existing = {row[0]: tuple(row[1:]) for row in existing_rows}
            incoming = {
                pid: _product_values(item, existing.get(pid))
                for pid, item in zip(ids, products)
            }
            added = sum(1 for pid in incoming if pid not in existing)
            updated = sum(1 for pid, values in incoming.items()
                          if pid in existing and existing[pid] != values)
            unchanged = len(incoming) - added - updated
            changed_ids = [
                pid for pid, values in incoming.items()
                if pid not in existing or existing[pid] != values
            ]
            rows = [(site, pid, *incoming[pid], synced_at)
                    for pid in changed_ids]
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO products "
                    "(site,id,cat_name,title,xinghao,jiage,edit_url,front_url,"
                    "xinghao_field,jiage_field,mcode,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

            removed = list(set(existing) - set(incoming)) if complete else []
            if removed:
                conn.executemany(
                    "DELETE FROM products WHERE site=? AND id=?",
                    [(site, pid) for pid in removed])
            total = len(incoming) if complete else len(existing) + added
            if remote_total is None:
                remote_total = len(incoming) if complete else None
            elif int(remote_total) < 0:
                raise ValueError("remote_total 必须是非负整数")
            remote_total_value = (int(remote_total)
                                  if remote_total is not None else None)

            previous = conn.execute(
                "SELECT last_catalog_sync_at,last_full_refresh_at,remote_total "
                "FROM product_sync_meta WHERE site=?", (site,)).fetchone()
            previous_catalog = previous[0] if previous else None
            previous_full = previous[1] if previous else None
            previous_remote_total = previous[2] if previous else None
            catalog_at = synced_at if complete else previous_catalog
            full_at = synced_at if mode == "full" and complete else previous_full
            if remote_total_value is None:
                remote_total_value = previous_remote_total
            conn.execute(
                "INSERT OR REPLACE INTO product_sync_meta "
                "(site,last_sync_at,last_sync_mode,last_catalog_sync_at,"
                "last_full_refresh_at,remote_total,requested,refreshed,added,"
                "updated,unchanged,deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (site, synced_at, mode, catalog_at, full_at,
                 remote_total_value, requested, refreshed, added, updated,
                 unchanged, len(removed)))
            conn.commit()
            return _sync_stats_payload(
                mode, complete, len(incoming), total, added, updated,
                unchanged, len(removed), requested, refreshed, synced_at)
        except Exception:
            conn.rollback()
            raise

def db_upsert_products(site, products):
    """写入/更新一批产品到本地数据库（按 site 隔离多站点）。"""
    if not site:
        return
    if not products:
        return
    try:
        now = time.time()
        products, ids = _validate_product_batch(products)
        with closing(_db_conn()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_rows = conn.execute(
                "SELECT id,cat_name,title,xinghao,jiage,edit_url,front_url,"
                "xinghao_field,jiage_field,mcode FROM products WHERE site=?",
                (site,)).fetchall()
            existing = {row[0]: tuple(row[1:]) for row in existing_rows}
            rows = [
                (site, pid, *_product_values(product, existing.get(pid)), now)
                for pid, product in zip(ids, products)
            ]
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO products "
                    "(site,id,cat_name,title,xinghao,jiage,edit_url,front_url,"
                    "xinghao_field,jiage_field,mcode,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
    except Exception as e:
        debug_log(f"[db] upsert error: {e}")


def db_reconcile_products(site, products, *, complete=None):
    """Atomically reconcile one complete remote snapshot with the site cache.

    Empty snapshots require separate parser evidence, not just completeness.
    Returns added/updated/unchanged/deleted.
    """
    if not site:
        raise ValueError("site is required")
    if complete is None:
        complete = getattr(products, "complete", False)
    if not complete:
        raise ValueError("拒绝使用未确认完整的产品快照进行删除对账")
    empty_confirmed = bool(getattr(products, "empty_confirmed", False))
    products = list(products or [])
    return db_commit_product_sync(
        site, products, complete=True, mode="full",
        remote_total=len(products), requested=len(products),
        refreshed=len(products), empty_confirmed=empty_confirmed)

def db_load_products(site):
    """从本地数据库读取某站点全部产品（秒开，无需联网）。"""
    if not site:
        return []
    try:
        with closing(_db_conn()) as conn:
            rows = conn.execute(
                "SELECT id,cat_name,title,xinghao,jiage,edit_url,front_url,xinghao_field,jiage_field,mcode "
                "FROM products WHERE site=? ORDER BY CAST(id AS INTEGER)", (site,)).fetchall()
        return [{"id": r[0], "cat_name": r[1], "title": r[2], "xinghao": r[3],
                "jiage": r[4], "edit_url": r[5], "front_url": r[6] or "",
                "xinghao_field": r[7] if r[7] is not None else "ext_xinghao",
                "jiage_field": r[8] if r[8] is not None else "ext_jiage",
                "mcode": r[9] or ""} for r in rows]
    except Exception as e:
        debug_log(f"[db] load error: {e}")
        return []

def db_count(site):
    if not site:
        return 0
    try:
        with closing(_db_conn()) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM products WHERE site=?",
                (site,)).fetchone()[0]
    except Exception:
        return 0


def db_product_health(site):
    """Return authoritative cache health and successful-sync timestamps."""
    empty = {
        "available": False, "error": "",
        "total": 0, "missing_front_url": 0, "missing_model": 0,
        "missing_price": 0, "missing_supported_price": 0,
        "missing_mcode": 0, "cache_updated_at": None,
        "last_sync_at": None, "last_sync": None, "last_sync_mode": "",
        "last_catalog_sync_at": None, "last_full_refresh_at": None,
        "remote_total": None, "requested": 0, "refreshed": 0,
        "last_stats": {"added": 0, "updated": 0, "unchanged": 0,
                       "deleted": 0},
    }
    site = str(site or "").strip()
    if not site:
        return empty
    try:
        with closing(_db_conn()) as conn:
            counts = conn.execute(
                "SELECT COUNT(*),"
                "COALESCE(SUM(CASE WHEN TRIM(COALESCE(front_url,''))='' "
                "THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN TRIM(COALESCE(xinghao,''))='' "
                "THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN TRIM(COALESCE(jiage,''))='' "
                "THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN TRIM(COALESCE(jiage_field,''))<>'' "
                "AND TRIM(COALESCE(jiage,''))='' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN TRIM(COALESCE(mcode,''))='' "
                "THEN 1 ELSE 0 END),0), MAX(updated_at) "
                "FROM products WHERE site=?", (site,)).fetchone()
            meta = conn.execute(
                "SELECT last_sync_at,last_sync_mode,last_catalog_sync_at,"
                "last_full_refresh_at,remote_total,requested,refreshed,added,"
                "updated,unchanged,deleted FROM product_sync_meta WHERE site=?",
                (site,)).fetchone()
        health = dict(empty)
        health["available"] = True
        (health["total"], health["missing_front_url"],
         health["missing_model"], health["missing_price"],
         health["missing_supported_price"], health["missing_mcode"],
         health["cache_updated_at"]) = counts
        if meta:
            health.update({
                "last_sync_at": meta[0], "last_sync": meta[0],
                "last_sync_mode": meta[1] or "",
                "last_catalog_sync_at": meta[2],
                "last_full_refresh_at": meta[3],
                "remote_total": meta[4], "requested": meta[5] or 0,
                "refreshed": meta[6] or 0,
                "last_stats": {"added": meta[7] or 0,
                               "updated": meta[8] or 0,
                               "unchanged": meta[9] or 0,
                               "deleted": meta[10] or 0},
            })
        return health
    except Exception as exc:
        debug_log(f"[db] health error: {exc}")
        empty["error"] = str(exc)
        return empty


def db_filter_products(site, *, ids=None, missing_fields=None):
    """Return cached products selected by ID and/or blank health fields.

    This intentionally builds no caller-provided SQL.  It is the shared,
    deterministic selector for the health panel and targeted repair jobs.
    ``missing_fields`` accepts ``front_url``, ``xinghao`` and ``jiage``.
    """
    products = db_load_products(site)
    wanted_ids = None
    if ids is not None:
        wanted_ids = {_clean_id(value) for value in ids if _clean_id(value)}
    fields = set(missing_fields or ())
    unknown = fields - {"front_url", "xinghao", "jiage"}
    if unknown:
        raise ValueError("未知缺失字段: " + ", ".join(sorted(unknown)))
    selected = []
    for product in products:
        if wanted_ids is not None and _clean_id(product.get("id")) not in wanted_ids:
            continue
        if fields and not any(
                not str(product.get(field, "") or "").strip()
                for field in fields):
            continue
        selected.append(product)
    return selected


def db_patch_products(site, patches):
    """Transactionally patch verified local model/price/URL values.

    Every patch must target an existing unique ID.  Validation happens before
    the first UPDATE, so an invalid bulk request cannot partially mutate the
    local cache.  Empty strings are intentional values when a key is present.
    """
    site = str(site or "").strip()
    if not site:
        raise ValueError("site is required")
    patches = list(patches or [])
    if not patches:
        return {"updated": 0, "unchanged": 0, "total": 0}
    allowed = {"xinghao", "jiage", "front_url"}
    normalized = []
    seen = set()
    for patch in patches:
        if not isinstance(patch, dict):
            raise ValueError("产品补丁必须是对象")
        pid = _clean_id(patch.get("id"))
        if not pid:
            raise ValueError("产品补丁缺少 ID")
        if pid in seen:
            raise ValueError(f"产品补丁包含重复 ID: {pid}")
        seen.add(pid)
        keys = set(patch) - {"id"}
        unknown = keys - allowed
        if unknown:
            raise ValueError("不允许修改缓存字段: " + ", ".join(sorted(unknown)))
        if not keys:
            raise ValueError(f"产品 {pid} 没有待修改字段")
        normalized.append((pid, {key: str(patch.get(key, "") or "")
                                 for key in keys}))
    with closing(_db_conn()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in normalized)
            rows = conn.execute(
                f"SELECT id,xinghao,jiage,front_url FROM products "
                f"WHERE site=? AND id IN ({placeholders})",
                (site, *[item[0] for item in normalized])).fetchall()
            existing = {row[0]: {"xinghao": row[1] or "",
                                 "jiage": row[2] or "",
                                 "front_url": row[3] or ""}
                        for row in rows}
            missing = [pid for pid, _values in normalized if pid not in existing]
            if missing:
                raise ValueError("当前缓存中不存在产品: " + ", ".join(missing))
            updated = 0
            unchanged = 0
            now = time.time()
            for pid, values in normalized:
                if all(existing[pid][key] == value
                       for key, value in values.items()):
                    unchanged += 1
                    continue
                assignments = ",".join(f"{key}=?" for key in values)
                conn.execute(
                    f"UPDATE products SET {assignments},updated_at=? "
                    "WHERE site=? AND id=?",
                    (*values.values(), now, site, pid))
                updated += 1
            conn.commit()
            return {"updated": updated, "unchanged": unchanged,
                    "total": len(normalized)}
        except Exception:
            conn.rollback()
            raise
def db_clear_site(site):
    if not site:
        return
    try:
        with closing(_db_conn()) as conn:
            conn.execute("DELETE FROM products WHERE site=?", (site,))
            conn.execute("DELETE FROM product_sync_meta WHERE site=?", (site,))
            conn.commit()
    except Exception as e:
        debug_log(f"[db] clear error: {e}")
