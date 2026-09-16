"""Threading-based background tasks (WebUI edition, zero Qt dependency).

R20 桌面版把这些逻辑放在 QThread 里；WebUI 版没有 Qt，改用 threading +
进度回调。**安全约定与 R20 完全一致**，逐条对应：

  1. 登录态快照（_snapshot_login）：主线程复制全部 cookie，后台线程只用副本，
     绝不触碰主 client.session（requests 的 cookiejar 非线程安全）；
  2. 后台线程用独立临时 client，并调 load_session() 从持久化会话文件兜底，
     防止内存快照为空的时序问题导致误报未登录；
  3. 「缺图不提交」：任一正文图 / 缩略图上传失败立即中止，绝不提交缺图内容；
  4. HTML内置图和手选图均处理，默认保留布局；图集默认上传原文件；
  5. 正常新任务不复用历史上传缓存；明确重试使用本任务已确认的路径；
     结果未知时停止，不盲目重传。
"""
import hashlib
import json
import os
import re
import threading
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from copy import copy
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from content_draft import insert_images_into_fields
from html_images import (rewrite_html_images, srcset_url_spans,
                         responsive_srcset_candidates, normalize_responsive_context,
                         media_type_supported, _media_matches, document_base_url)
from html_media import rewrite_html_media
from logger import debug_log
from pboot_client import PbootCMSClient, _is_login_page
from client_products import ProductSnapshot
from backend_diagnostic import diagnose_backend, save_report
from exceptions import Cancelled, UploadOutcomeUnknown
from upload_policy import UploadPolicyError, NativeUploadRequired
from file_metadata import remember_declared_mime
from gallery_plan import normalize_gallery_plan
from first_thumbnail import downloaded_source, image_identity
from http_transport import permitted_transition
from link_check import probe_links
from urllib.parse import urljoin
from html_images import replace_image_occurrences


# Layui 2.5 starts one jQuery XHR for every selected file in a
# ``multiple:true`` queue immediately (see the stock ``upload`` module's
# ``layui.each`` loop).  Browsers still limit the actual socket fan-out, so a
# small bounded pool is a closer match than either an unbounded thread pool or
# the old strictly serial desktop queue.
_BROWSER_UPLOAD_WORKERS = 6


def _payload_declared_mime_paths(payload):
    """Return the local files actually referenced by one upload task.

    Browser ``File.type`` hints are carried in a draft/task snapshot only for
    these paths.  Keeping this allow-list explicit prevents an untrusted
    ``asset_mimes`` mapping from registering arbitrary files in the process
    registry (or influencing a later task).
    """
    paths = []

    def add(value):
        if isinstance(value, (str, os.PathLike)):
            text = str(value or "").strip()
            if text:
                paths.append(text)

    for key in ("image_paths", "carousel_paths"):
        for value in payload.get(key) or ():
            add(value)
    add(payload.get("thumbnail_path"))
    for key in ("inline_images", "media_assets", "media_uploads"):
        for item in payload.get(key) or ():
            if isinstance(item, dict):
                add(item.get("path"))
    for item in payload.get("image_replacements") or ():
        if isinstance(item, dict):
            add(item.get("local_path"))
    plan = payload.get("gallery_plan")
    if isinstance(plan, (list, tuple)):
        for item in plan:
            if isinstance(item, dict) and str(item.get("kind", "") or "").lower() == "file":
                add(item.get("value"))
    return paths


def _restore_payload_declared_mimes(payload):
    """Restore only browser MIME hints for files in this task snapshot."""
    hints = payload.get("asset_mimes")
    if not isinstance(hints, dict) or not hints:
        return 0
    allowed = {}
    for path in _payload_declared_mime_paths(payload):
        try:
            absolute = os.path.normcase(os.path.realpath(os.path.abspath(path)))
            if os.path.isfile(absolute):
                allowed[absolute] = absolute
        except (OSError, TypeError, ValueError):
            continue
    restored = 0
    for raw_path, raw_mime in hints.items():
        if not isinstance(raw_path, (str, os.PathLike)):
            continue
        try:
            absolute = os.path.normcase(os.path.realpath(os.path.abspath(str(raw_path))))
        except (OSError, TypeError, ValueError):
            continue
        if absolute not in allowed:
            continue
        mime = str(raw_mime or "").split(";", 1)[0].strip().lower()
        if (not mime or len(mime) > 127 or "/" not in mime or
                any(ord(char) < 0x21 or char in "\r\n;" for char in mime)):
            continue
        if remember_declared_mime(allowed[absolute], mime):
            restored += 1
    return restored


def snapshot_login(client, include_headers=False):
    """【必须在调用方线程（非后台线程）执行】快照 admin_url + 全部 cookie。

    复制【全部】cookie 而不按域名过滤：requests 只会发送 domain 匹配的 cookie，
    多带其它站点的不会误发；而按域名过滤反可能因 cookie 的 domain 写法
    （带/不带前导点、host-only）漏掉登录态，导致后台 client 未登录。
    """
    admin_url = getattr(client, "admin_url", "")
    cookies = []
    try:
        for cookie in client.session.cookies:
            cookies.append(copy(cookie))
    except Exception as exc:
        debug_log(f"[snapshot] cookie 快照失败: {exc}")
    debug_log(f"[snapshot] 已快照 {len(cookies)} 个 cookie（来自 {admin_url}）")
    if include_headers:
        headers = dict(getattr(client.session, "headers", {}) or {})
        # Cookie values are carried separately; never duplicate Cookie into a
        # worker header snapshot where requests could send stale credentials.
        headers = {str(k): str(v) for k, v in headers.items()
                   if str(k).lower() != "cookie"}
        return admin_url, cookies, headers
    return admin_url, cookies


def network_snapshot(client):
    """Capture only the explicit network routing policy for worker clients."""
    return {
        "mode": str(getattr(client, "network_mode", "direct") or "direct"),
        "proxy_url": str(getattr(client, "proxy_url", "") or ""),
    }


def worker_snapshot(client):
    """Capture cookies and browser-like request headers for any worker.

    Content publishing already used the extended snapshot; keeping the helper
    shared by category/product/message/diagnostic workers prevents those
    read/write paths from silently dropping a site's custom User-Agent,
    Referer token or Accept headers.
    """
    snapshot = snapshot_login(client, include_headers=True)
    network = network_snapshot(client)
    if isinstance(snapshot, (tuple, list)) and len(snapshot) >= 3:
        return snapshot[0], snapshot[1], snapshot[2], network
    return snapshot[0], snapshot[1], dict(getattr(client.session, 'headers', {}) or {}), network


def _session_cookie_records(client):
    """Serialize worker cookie rotation for the owning UI session.

    Browser tabs share a cookie jar; the desktop worker intentionally uses a
    copy to avoid requests' thread-safety problem.  Returning the rotated
    records lets the bridge merge Set-Cookie results back after a task without
    exposing Cookie objects through the JavaScript event payload.
    """
    records = []
    try:
        for cookie in client.session.cookies:
            records.append({
                "name": cookie.name, "value": cookie.value,
                "domain": cookie.domain, "path": cookie.path,
                "secure": bool(getattr(cookie, "secure", False)),
                "expires": getattr(cookie, "expires", None),
                "discard": bool(getattr(cookie, "discard", False)),
                "version": int(getattr(cookie, "version", 0) or 0),
                "domain_initial_dot": bool(getattr(cookie, "domain_initial_dot", False)),
                "rest": dict(getattr(cookie, "_rest", {}) or {}),
            })
    except Exception as exc:
        debug_log(f"[session] worker cookie snapshot failed: {exc}")
    return records


def _with_worker_session(client, result):
    result = dict(result or {})
    result["_session_cookie_records"] = _session_cookie_records(client)
    # The worker starts with a private copy of the parent's cookie jar.  The
    # owning UI session needs this baseline so a Set-Cookie deletion (an
    # expired cookie is removed from the worker jar) is propagated back too.
    # Keep the records private to the bridge; they are popped before the
    # result reaches WebUI JavaScript.
    result["_session_cookie_initial_records"] = list(
        getattr(client, "_worker_initial_cookie_records", []) or [])
    return result


def _worker_cookie_identity(record):
    if isinstance(record, dict):
        return (str(record.get("name", "") or ""),
                str(record.get("domain", "") or ""),
                str(record.get("path", "/") or "/"))
    return (str(getattr(record, "name", "") or ""),
            str(getattr(record, "domain", "") or ""),
            str(getattr(record, "path", "/") or "/"))


def _worker_cookie_is_expired(record):
    if not isinstance(record, dict):
        return False
    value = record.get("expires")
    if value in (None, "", 0, "0"):
        return False
    try:
        return float(value) <= time.time()
    except (TypeError, ValueError):
        return False


def _merge_parallel_worker_session(target, worker):
    """Merge one independent upload-XHR CookieJar into its parent client.

    Layui starts independent requests for a multi-file queue.  Each clone
    therefore needs its own CookieJar, but Set-Cookie rotations and deletions
    must still reach the parent before the final form POST.  Completion is
    serialized by the caller, so this reconciliation does not race the
    individual requests.
    """
    target_session = getattr(target, "session", target)
    worker_session = getattr(worker, "session", worker)
    if not hasattr(target_session, "cookies") or not hasattr(worker_session, "cookies"):
        return
    initial = list(getattr(worker, "_worker_initial_cookie_records", []) or [])
    final = _session_cookie_records(worker)
    final_ids = {_worker_cookie_identity(item) for item in final
                 if not _worker_cookie_is_expired(item)}
    for before in initial:
        identity = _worker_cookie_identity(before)
        if not identity[0] or identity in final_ids:
            continue
        for existing in list(target_session.cookies):
            if (_worker_cookie_identity(existing) != identity or
                    str(getattr(existing, "value", "") or "") !=
                    str(before.get("value", "") or "")):
                continue
            try:
                target_session.cookies.clear(
                    domain=str(getattr(existing, "domain", "") or ""),
                    path=str(getattr(existing, "path", "/") or "/"),
                    name=str(getattr(existing, "name", "") or ""))
            except Exception:
                pass
            break
    try:
        from requests.cookies import create_cookie
        for record in final:
            if _worker_cookie_is_expired(record):
                continue
            # The parent here is the task's private client, and all sibling
            # XHR clones share the same browser queue.  Applying records in
            # completion order preserves the browser's last-completed cookie
            # rotation; the owning UI session is reconciled separately by
            # ``_merge_worker_session_result`` after the task settles.
            cookie = create_cookie(
                name=str(record.get("name", "")),
                value=str(record.get("value", "")),
                domain=str(record.get("domain", "") or ""),
                path=str(record.get("path", "/") or "/"),
                secure=bool(record.get("secure", False)),
                expires=record.get("expires"),
                discard=bool(record.get("discard", False)),
                version=int(record.get("version", 0) or 0),
                rest=dict(record.get("rest", {}) or {}),
            )
            cookie.domain_initial_dot = bool(record.get("domain_initial_dot", False))
            target_session.cookies.set_cookie(cookie)
    except Exception as exc:
        debug_log(f"[upload] 并发 XHR Cookie 合并失败: {exc}")


def build_worker_client(admin_url, cookies, verify=True, headers=None, network=None):
    """用快照重建一个独立的后台 client（零共享）。"""
    tmp = PbootCMSClient()
    tmp.set_admin_url(admin_url)
    tmp.session.verify = verify
    if isinstance(network, dict):
        try:
            tmp.configure_network(network.get("mode", "direct"),
                                  network.get("proxy_url", ""))
        except (TypeError, ValueError) as exc:
            debug_log(f"[worker] 网络策略无效，回退直连: {exc}")
            tmp.configure_network("direct", "")
    if headers:
        tmp.session.headers.update(dict(headers))
    # 内存快照是调用时的最新登录态。只有快照为空才从磁盘兜底，避免
    # 旧磁盘 Cookie 覆盖刚登录后取得的新 Cookie。
    if not cookies:
        try:
            tmp.load_session()
        except Exception as exc:
            debug_log(f"[worker] load_session 兜底失败(可忽略): {exc}")
    for cookie in cookies:
        tmp.session.cookies.set_cookie(copy(cookie))
    tmp.logged_in = True
    # Capture the post-fallback baseline.  When the in-memory snapshot is
    # empty, ``load_session`` may have populated the jar from disk; those
    # cookies are part of the worker's initial state and must also be
    # reconciled on completion.
    tmp._worker_initial_cookie_records = _session_cookie_records(tmp)
    return tmp


def file_key(site_id, path, variant=""):
    """站点 + 文件内容 SHA-256，作为上传缓存键。

    The caller's variant may include a stable occurrence slot.  Without a
    slot, identical content is intentionally reusable; with one, separate
    browser upload rows remain separate while retries still hit the same key.
    """
    digest = hashlib.sha256(str(site_id or "").encode("utf-8"))
    digest.update(str(variant or "").encode("utf-8"))
    digest.update(b'\x00filename\x00' + os.path.basename(path).encode('utf-8'))
    # Keep the per-read buffer modest.  Hashing is performed for every asset
    # (including tiny HTML fixtures and retry probes); a 1 MiB temporary
    # allocation is unnecessary and can fail under the desktop WebView's
    # already constrained heap when a large batch has just completed.
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _call_upload_with_slot(upload_fn, args, kwargs, cache_slot=None):
    """Call an upload helper while keeping old test/integration shims valid.

    A few embedders replace ``_upload_one``/``_upload_media_one`` with a
    legacy six-argument callback.  The slot is an optional keyword, so a
    callback that explicitly does not accept it should retain the old
    content-addressed behaviour rather than failing before the upload.  Real
    helpers accept the keyword and never take the fallback path.
    """
    if cache_slot is None:
        return upload_fn(*args, **kwargs)
    try:
        return upload_fn(*args, **dict(kwargs, cache_slot=cache_slot))
    except TypeError as exc:
        message = str(exc)
        if "cache_slot" not in message or "unexpected keyword" not in message:
            raise
        return upload_fn(*args, **kwargs)


def _native_upload_required(message, client=None, surface="", target="", kind=""):
    """Build a native-page exception while retaining the discovered owner.

    Upload workers are deliberately isolated, so the caller must not guess a
    POST action when a worker reports ``native_only``.  The policy page is the
    safest fallback URL and is already constrained to the current site by
    policy discovery.
    """
    page_url = ""
    policies = getattr(client, "_upload_policies", {}) or {}
    policy = policies.get((surface, target, str(kind or "").strip().lower())) \
        if kind else None
    if policy is None:
        policy = policies.get((surface, target))
    if policy is None and surface == "editor":
        policy = policies.get((surface, target, "image"))
    page_url = str(getattr(policy, "page_url", "") or "")
    return NativeUploadRequired(str(message or "该上传必须由认证原生网页处理"),
                                native_url=page_url,
                                reason=str(message or ""))
def _cached_upload_is_missing(client, url):
    """Return whether a retry-cache URL is conclusively gone.

    A retry should reuse a confirmed upload, but a server-side cleanup or
    short-lived object store can remove that URL before the user retries.  A
    bounded same-origin read-only probe may prove 404/410; all other outcomes
    (auth denial, redirects, timeout, oversized object, unsupported probe)
    remain inconclusive and therefore keep the cache entry rather than causing
    a duplicate upload.
    """
    probe = getattr(client, '_probe_uploaded_asset', None)
    if not callable(probe):
        return False
    try:
        observed = probe(url)
    except Exception:
        return False
    status = str((observed or {}).get('server_inspection', '') or '').lower()
    return status in {'head_http_404', 'get_http_404',
                      'head_http_410', 'get_http_410'}


def _parallel_upload_client(parent):
    """Build an isolated client for one browser-style upload XHR.

    ``requests.Session`` and ``last_upload_result`` are mutable.  Sharing the
    task client between parallel callbacks would make one response overwrite
    another response's metadata and would also race its cookie jar.  Reusing
    the already-discovered immutable upload policies keeps the endpoint/field
    decision identical while giving every XHR its own session/result state.
    """
    if not isinstance(parent, PbootCMSClient):
        return parent
    admin_url, cookies, headers, network = worker_snapshot(parent)
    clone = build_worker_client(
        admin_url, cookies, getattr(parent.session, "verify", True),
        headers=headers, network=network)
    clone._upload_policies = dict(getattr(parent, "_upload_policies", {}) or {})
    clone._strict_browser_upload_parity = bool(
        getattr(parent, "_strict_browser_upload_parity", False))
    return clone


def _parallel_uploads(client, ctx, payload, cache, specs, total, done):
    """Run a native multi-file queue with browser-like callback semantics.

    ``specs`` is an ordered list of callables receiving an isolated client and
    returning the server URL.  Requests may complete in any order, so progress
    and upload metadata are emitted as futures finish, while the returned URL
    list always follows the original DOM/selection order.  All in-flight
    requests are allowed to settle before an error is raised; this mirrors
    Layui's independent ``success/error`` callbacks and avoids starting a
    second retry after an uncertain POST.

    Test doubles and single-item queues intentionally remain serial.  This
    keeps legacy integrations deterministic while real Pboot clients follow
    the stock Layui XHR fan-out.
    """
    if len(specs) < 2 or not isinstance(client, PbootCMSClient):
        urls = []
        for spec in specs:
            ctx.check_cancelled()
            urls.append(spec(client))
            done += 1
            ctx.progress(done, total, "上传完成")
        return urls, done

    results = [None] * len(specs)
    failures = []
    completed = 0
    max_workers = min(_BROWSER_UPLOAD_WORKERS, len(specs))

    def run_one(index, spec):
        worker = None
        try:
            ctx.check_cancelled()
            worker = _parallel_upload_client(client)
            return index, spec(worker), None, worker
        except Exception as exc:  # collect all callbacks before raising
            return index, None, exc, worker

    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="pboot-upload") as pool:
        futures = [pool.submit(run_one, index, spec)
                   for index, spec in enumerate(specs)]
        for future in as_completed(futures):
            index, url, error, worker = future.result()
            if worker is not None:
                _merge_parallel_worker_session(client, worker)
            completed += 1
            if error is not None:
                failures.append((index, error))
                ctx.progress(done + completed, total,
                             f"上传失败（队列第 {index + 1} 项）")
                continue
            results[index] = url
            ctx.progress(done + completed, total,
                         f"上传完成（队列第 {index + 1} 项）")

    if failures:
        # Unknown outcomes take precedence over ordinary rejection: the user
        # must inspect the backend before retrying any queue item.
        failures.sort(key=lambda item: item[0])
        unknown = next((exc for _index, exc in failures
                        if isinstance(exc, UploadOutcomeUnknown)), None)
        if unknown is not None:
            raise unknown
        cancelled = next((exc for _index, exc in failures
                          if isinstance(exc, Cancelled)), None)
        if cancelled is not None:
            raise cancelled
        raise failures[0][1]
    return results, done + completed


def upload_field_queue(client, paths, field, *, formcheck="",
                       upload_target="", label="字段", media_kind="file",
                       progress_callback=None, cancel_callback=None):
    """Upload one dynamic form field with the browser's multi-file semantics.

    Category/Slide/Single/generic-admin forms are executed outside the
    publish worker, but their ``button.upload``/``input[type=file]`` controls
    still use the same Layui independent-XHR contract.  Keeping this adapter
    here avoids four subtly different serial loops.  URL order is always the
    original selection order; metadata is returned in callback completion
    order, matching the page UI.

    The caller is responsible for discovering and preflighting the page-owned
    upload policy before invoking this helper.  A local path is never placed
    directly in the final form value.  ``UploadOutcomeUnknown`` is raised for
    a worker whose POST may have reached the server, so callers can switch to
    the authenticated native page rather than retrying blindly.
    """
    if cancel_callback is None:
        cancel_callback = getattr(client, "_active_cancel_callback", None)
    ordered = [str(path or "").strip() for path in (paths or ())
               if str(path or "").strip()]
    if not ordered:
        return [], []
    field = dict(field or {})
    target = str(upload_target or field.get("name", "") or "").strip()
    title = str(label or target or "字段")

    def check_cancelled():
        if callable(cancel_callback):
            try:
                if cancel_callback():
                    raise Cancelled()
            except Cancelled:
                raise
            except Exception as exc:
                debug_log(f"[upload] 取消回调异常（忽略）: {exc}")

    check_cancelled()

    # Do the same whole-queue check Layui performs before creating its first
    # XHR.  Dynamic forms can expose different policies for images and other
    # media under the same field, so resolve the policy per selected path.
    # ``validate_path`` reads only a bounded signature prefix and therefore
    # does not duplicate a large upload in memory.
    try:
        from form_controls import upload_field_is_image
        # Production PbootCMSClient always owns ``_upload_policies`` and must
        # pass the whole-queue preflight above before any XHR is created.  A
        # few embedders and the long-standing unit-test doubles intentionally
        # expose only ``prepare_uploads`` plus ``upload_image``/``upload_file``
        # and have no policy store at all; retain their historical adapter
        # contract instead of rejecting the upload before their uploader gets
        # a chance to validate it.  An explicitly present (even empty) store
        # remains authoritative and therefore still fails closed.
        has_policy_store = hasattr(client, "_upload_policies")
        policies = getattr(client, "_upload_policies", {}) or {}
        if has_policy_store:
            for path in ordered:
                check_cancelled()
                image = bool(upload_field_is_image(field, path))
                policy = (policies.get(("field", target)) if image else
                          policies.get(("field", target, media_kind)) or
                          policies.get(("field", target)))
                if policy is None:
                    raise UploadPolicyError(
                        f"尚未读取{target or '字段'}的真实上传配置，请重新载入表单")
                policy.validate_path(os.path.basename(path), path)
    except UploadPolicyError:
        raise
    except (OSError, IOError) as exc:
        raise UploadPolicyError(f"{title}文件无法读取：{exc}") from exc
    entries = []
    entry_lock = threading.Lock()

    class _ForegroundContext:
        cancelled = False

        def check_cancelled(self):
            if callable(cancel_callback):
                try:
                    if cancel_callback():
                        self.cancelled = True
                except Exception as exc:
                    debug_log(f"[upload] 取消回调异常（忽略）: {exc}")
            if self.cancelled:
                raise Cancelled()

        def progress(self, done, total, text=""):
            if callable(progress_callback):
                try:
                    progress_callback(done, total, text)
                except Exception:
                    pass

    ctx = _ForegroundContext()

    def spec(path):
        def run(worker):
            image = False
            try:
                from form_controls import upload_field_is_image
                image = bool(upload_field_is_image(field, path))
            except Exception:
                image = False
            if image:
                url, error = worker.upload_image(
                    path, formcheck=formcheck, upload_surface="field",
                    upload_target=target)
            else:
                url, error = worker.upload_file(
                    path, formcheck=formcheck, upload_surface="field",
                    upload_target=target, media_kind=media_kind)
            result = dict(getattr(worker, "last_upload_result", {}) or {})
            if not url:
                if str(result.get("outcome", "") or "").lower() == "native_only":
                    reason = str(error or result.get("native_reason") or
                                  f"{title}必须由认证原生网页处理")
                    raise _native_upload_required(
                        reason, worker, "field", target)
                if str(result.get("outcome", "") or "").lower() == "unknown":
                    raise UploadOutcomeUnknown(
                        f"{title}上传结果未知，文件可能已保存；请先核对后台")
                raise RuntimeError(str(error or f"{title}上传失败"))
            try:
                from client_content import upload_result_entry
                entry = upload_result_entry(result, title, os.path.basename(path))
            except Exception:
                entry = {"label": title, "filename": os.path.basename(path),
                         "outcome": result.get("outcome", "")}
            with entry_lock:
                entries.append(entry)
            return url
        return run

    specs = [spec(path) for path in ordered]
    urls, _done = _parallel_uploads(
        client, ctx, {"upload_metadata": []}, {}, specs,
        len(ordered), 0)
    return urls, entries


def upload_field_queues(client, queues, *, formcheck="", progress_callback=None,
                        cancel_callback=None):
    """Upload several independent dynamic-form controls as one browser queue.

    A page can expose more than one ``button.upload``/``input[type=file]``
    control.  The old adapters called :func:`upload_field_queue` once per
    field, which made otherwise independent controls strictly serial.  The
    browser starts an XHR for each selected file as the controls are handled;
    this helper performs one complete preflight for *all* supplied controls,
    then fans the files out through the same bounded pool.  Returned URLs are
    grouped by ``key`` (or field name) and retain each control's original
    selection order, while metadata remains in completion order.

    ``queues`` is an iterable of dictionaries with ``field`` (descriptor),
    ``paths``, optional ``name``/``key``, ``upload_target``, ``label`` and
    ``media_kind``.  An explicit ``_upload_policies`` mapping is fail-closed;
    test doubles without one retain the historical permissive contract.
    """
    if cancel_callback is None:
        cancel_callback = getattr(client, "_active_cancel_callback", None)
    jobs = []
    seen_keys = set()
    policies = getattr(client, "_upload_policies", {}) or {}
    has_policy_store = hasattr(client, "_upload_policies")
    try:
        from form_controls import upload_field_is_image
    except Exception:  # pragma: no cover - production import is always present
        upload_field_is_image = lambda _field, _path: False

    def check_cancelled():
        if callable(cancel_callback):
            try:
                if cancel_callback():
                    raise Cancelled()
            except Cancelled:
                raise
            except Exception as exc:
                debug_log(f"[upload] 取消回调异常（忽略）: {exc}")

    # First normalize and validate every queue.  No upload worker is created
    # before this loop completes, matching Layui's number/accept preflight.
    for queue in queues or ():
        check_cancelled()
        if not isinstance(queue, dict):
            raise ValueError("动态上传队列项格式无效")
        field = dict(queue.get("field") or {})
        name = str(queue.get("name") or field.get("name") or "").strip()
        target = str(queue.get("upload_target") or name).strip()
        label = str(queue.get("label") or field.get("label") or name or "字段")
        kind = str(queue.get("media_kind") or field.get("media_kind") or "file").strip().lower()
        if kind not in ("video", "audio", "file"):
            kind = "file"
        ordered = [str(path or "").strip() for path in (queue.get("paths") or ())
                   if str(path or "").strip()]
        if not ordered:
            continue
        try:
            max_files = int(field.get("max_files") or 0)
        except (TypeError, ValueError):
            max_files = 0
        if max_files > 0 and len(ordered) > max_files:
            raise UploadPolicyError(f"{label}最多选择 {max_files} 个文件")
        # Use the per-path image decision exactly as the single-field helper
        # does.  This is intentionally performed before any XHR is submitted.
        if has_policy_store:
            for path in ordered:
                check_cancelled()
                image = bool(upload_field_is_image(field, path))
                policy = (policies.get(("field", target)) if image else
                          policies.get(("field", target, kind)) or
                          policies.get(("field", target)))
                if policy is None:
                    raise UploadPolicyError(
                        f"尚未读取{target or '字段'}的真实上传配置，请重新载入表单")
                try:
                    policy.validate_path(os.path.basename(path), path)
                except (OSError, IOError) as exc:
                    raise UploadPolicyError(f"{label}文件无法读取：{exc}") from exc
        key = str(queue.get("key") or name or len(jobs))
        if key in seen_keys:
            # A dict result cannot represent two independent controls with
            # the same name without losing DOM order or metadata ownership.
            # Refuse the ambiguous desktop path; the native page can submit
            # both controls exactly as its own JavaScript intends.
            raise UploadPolicyError(
                f"动态上传字段 {key} 重复，无法安全合并，请使用原生网页")
        seen_keys.add(key)
        jobs.append({"key": key,
                     "name": name, "target": target, "label": label,
                     "kind": kind, "field": field, "paths": ordered})
    if not jobs:
        return {}

    entries = []
    entry_lock = threading.Lock()

    class _ForegroundContext:
        cancelled = False

        def check_cancelled(self):
            if callable(cancel_callback):
                try:
                    if cancel_callback():
                        self.cancelled = True
                except Exception as exc:
                    debug_log(f"[upload] 取消回调异常（忽略）: {exc}")
            if self.cancelled:
                raise Cancelled()

        def progress(self, done, total, text=""):
            if callable(progress_callback):
                try:
                    progress_callback(done, total, text)
                except Exception:
                    pass

    ctx = _ForegroundContext()
    specs = []
    spec_meta = []
    for job in jobs:
        for index, path in enumerate(job["paths"]):
            # Capture all values by default: closures run later on worker
            # threads and must not observe the next loop iteration.
            def spec(worker, job=job, index=index, path=path):
                image = bool(upload_field_is_image(job["field"], path))
                if image:
                    url, error = worker.upload_image(
                        path, formcheck=formcheck, upload_surface="field",
                        upload_target=job["target"])
                else:
                    url, error = worker.upload_file(
                        path, formcheck=formcheck, upload_surface="field",
                        upload_target=job["target"], media_kind=job["kind"])
                result = dict(getattr(worker, "last_upload_result", {}) or {})
                if not url:
                    if str(result.get("outcome", "") or "").lower() == "native_only":
                        reason = str(error or result.get("native_reason") or
                                      f"{job['label']}必须由认证原生网页处理")
                        raise _native_upload_required(
                            reason, worker, "field", job["target"])
                    if str(result.get("outcome", "") or "").lower() == "unknown":
                        raise UploadOutcomeUnknown(
                            f"{job['label']}上传结果未知，文件可能已保存；请先核对后台")
                    raise RuntimeError(str(error or f"{job['label']}上传失败"))
                try:
                    from client_content import upload_result_entry
                    entry = upload_result_entry(
                        result, job["label"], os.path.basename(path))
                except Exception:
                    entry = {"label": job["label"],
                             "filename": os.path.basename(path),
                             "outcome": result.get("outcome", "")}
                entry["_queue_key"] = job["key"]
                entry["_queue_index"] = index
                with entry_lock:
                    entries.append(entry)
                return url
            specs.append(spec)
            spec_meta.append((job["key"], index))

    urls, _done = _parallel_uploads(
        client, ctx, {"upload_metadata": []}, {}, specs,
        len(specs), 0)
    grouped = {}
    for (key, index), url in zip(spec_meta, urls):
        grouped.setdefault(key, []).append((index, url))
    for key, values in grouped.items():
        values.sort(key=lambda pair: pair[0])
        grouped[key] = {"urls": [url for _index, url in values],
                        "metadata": []}
    # Metadata is deliberately reported in completion order, but each queue
    # receives the entries belonging to its own occurrence slot for UI/audit
    # use.  Filename alone is not sufficient because two controls can select
    # different files with the same basename.
    for job in jobs:
        bucket = grouped.get(job["key"])
        if bucket is None:
            continue
        bucket["metadata"] = [entry for entry in entries
                              if entry.get("_queue_key") == job["key"]]
        for entry in bucket["metadata"]:
            entry.pop("_queue_key", None)
            entry.pop("_queue_index", None)
    return grouped


def _same_site_http_url(candidate, reference):
    """Return whether two HTTP(S) URLs have the exact same origin.

    Remote-image mappings can survive in a retry cache for a long time.  The
    original page policy may have changed in the meantime, so a cached value
    must pass the same origin check as a fresh catcher response before it is
    allowed back into the HTML.  origin() also rejects credentials, control
    characters and unsupported schemes.
    """
    try:
        candidate_text = str(candidate or "").strip()
        reference_text = str(reference or "").strip()
        # Upload callbacks are allowed to return a root-relative path (the
        # common UEditor/Layui response shape).  Resolve that spelling against
        # the active site before comparing origins; treating it as an external
        # URL would incorrectly invalidate the retry cache and upload twice.
        if candidate_text.startswith('/') and reference_text:
            candidate_text = urljoin(reference_text.rstrip('/') + '/', candidate_text)
        return permitted_transition(reference_text, candidate_text)
    except Exception:
        return False


def _record_remote_upload(client, ctx, payload, source, target_url,
                          policy_metadata=None):
    """Record and stream metadata for one remote-image catcher result.

    A remote-image catcher is still an upload surface: the browser receives a
    callback for the resulting server object even though there was no local
    File to hash.  Probe that object with the same bounded, read-only image
    inspection used by direct uploads and expose the evidence immediately.
    """
    target_url = str(target_url or "").strip()
    if not target_url:
        return
    observed_result = {"path": target_url, "metadata": {}}
    try:
        annotate = getattr(client, "_annotate_uploaded_image", None)
        if callable(annotate):
            pending_detector = getattr(client, "_upload_result_pending", None)
            pending = bool(callable(pending_detector) and
                           pending_detector(getattr(client, "last_upload_result", {})))
            annotate(observed_result, target_url, wait_for_processing=pending)
        else:
            probe = getattr(client, "_probe_uploaded_asset", None)
            if callable(probe):
                observed_result["metadata"].update(probe(target_url) or {})
    except Exception as exc:
        debug_log(f"[远程图片回读] 忽略只读探测失败：{exc}")
        observed_result["metadata"]["server_inspection"] = "unavailable"
    metadata = observed_result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    parsed = urlparse(target_url)
    filename = os.path.basename(parsed.path or "") or os.path.basename(
        urlparse(str(source or "")).path or "") or "远程图片"
    entry = {
        "label": "远程图片抓取",
        "filename": filename,
        "source": str(source or ""),
        "url": target_url,
        "metadata": metadata,
        "policy": dict(policy_metadata or {}),
        "outcome": "confirmed",
    }
    payload.setdefault("upload_metadata", []).append(entry)
    _mark_possible_shared_object(payload, entry)
    ctx.upload_metadata(entry)


class TaskContext:
    """后台任务上下文：进度/日志/上传回调 + 取消标志。"""

    def __init__(self, on_progress=None, on_log=None, on_upload=None):
        self._on_progress = on_progress
        self._on_log = on_log
        self._on_upload = on_upload
        self._cancel = threading.Event()
        # Browser uploaders expose byte progress in addition to the number of
        # completed callbacks.  Keep this optional so non-media jobs retain
        # their old step-only contract and legacy callbacks remain valid.
        self._bytes_total = 0
        self._bytes_done = 0
        # Multiple Layui uploads can finish on different XHR callbacks.  A
        # single task still owns one payload/cache, so protect those small
        # mutations while allowing the network calls themselves to overlap.
        self._lock = threading.RLock()
        self._emit_lock = threading.RLock()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def check_cancelled(self):
        if self._cancel.is_set():
            raise Cancelled()

    @property
    def bytes_total(self):
        with self._lock:
            return self._bytes_total

    @property
    def bytes_done(self):
        with self._lock:
            return self._bytes_done

    def set_byte_budget(self, total):
        try:
            value = max(0, int(total or 0))
        except (TypeError, ValueError):
            value = 0
        with self._lock:
            self._bytes_total = value
            self._bytes_done = 0

    def add_uploaded_bytes(self, value):
        try:
            amount = max(0, int(value or 0))
        except (TypeError, ValueError):
            amount = 0
        with self._lock:
            self._bytes_done += amount

    def progress(self, done, total, current="", *, bytes_done=None,
                 bytes_total=None):
        if self._on_progress:
            with self._emit_lock:
                try:
                    if bytes_done is None:
                        bytes_done = self.bytes_done
                    if bytes_total is None:
                        bytes_total = self.bytes_total
                    if bytes_total:
                        self._on_progress(done, total, current,
                                          bytes_done, bytes_total)
                    else:
                        self._on_progress(done, total, current)
                except TypeError:
                    # Third-party task adapters written before byte progress
                    # was introduced still receive the original three args.
                    try:
                        self._on_progress(done, total, current)
                    except Exception:
                        pass
                except Exception:
                    pass

    def log(self, message):
        debug_log(f"[task] {message}")
        if self._on_log:
            with self._emit_lock:
                try:
                    self._on_log(message)
                except Exception:
                    pass

    def upload_metadata(self, entry):
        """Publish one bounded upload result as soon as the server replies.

        The browser shows a selected/uploaded thumbnail immediately, while a
        desktop publish task historically exposed all server dimensions only
        after the final article POST.  Keep the task result as the durable
        source of truth, but also stream the same metadata to the UI so a
        thumbnail's server width/height/MIME/processing state is visible while
        the remaining queue is still running.  Callers may use old TaskContext
        adapters; failures here never affect the upload transaction.
        """
        if not self._on_upload or not isinstance(entry, dict):
            return
        with self._emit_lock:
            try:
                self._on_upload(dict(entry))
            except Exception:
                pass


def _validate_editor_word_limit(client, payload, fields):
    """Enforce the active UEditor ``maximumWords`` before any upload/POST.

    UEditor counts the textual content rather than the HTML tag bytes.  A
    desktop publish that silently uploads assets and only then receives a
    server-side word-limit rejection is not browser-equivalent, so reject a
    definitely over-limit body while the operation is still side-effect free.
    Missing/ambiguous policy metadata remains fail-closed at policy discovery;
    an absent maximumWords simply means that this editor has no declared cap.
    """
    target = str(payload.get('target_field') or 'content')
    policy = getattr(client, '_upload_policies', {}).get(('editor', target))
    if policy is None:
        return
    raw_limit = (getattr(policy, 'metadata', {}) or {}).get('maximumWords')
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return
    if limit <= 0 or target not in fields:
        return
    from bs4 import BeautifulSoup
    text = BeautifulSoup(str(fields.get(target, '') or ''), 'html.parser').get_text('', strip=False)
    # UEditor runs in JavaScript and maximumWords is based on UTF-16 string
    # length, not Python's Unicode code-point count (emoji/non-BMP characters
    # therefore consume two units in the browser).
    units = len(text.encode('utf-16-le')) // 2
    if units > limit:
        raise RuntimeError(f'正文超过当前网页编辑器字数上限（{limit} 字符），未上传或提交')


def _mark_possible_shared_object(payload, entry):
    """Flag equal server hashes/ETags returned under different URL paths.

    A browser cannot prove whether two URLs point to one physical object.  We
    can still expose the observable signal without treating equal bytes as a
    failure: identical server hashes on different returned URLs are a
    *possible* deduplication/shared-object condition that needs site review.
    """
    metadata = entry.get("metadata") if isinstance(entry, dict) else None
    digest = metadata.get("server_sha256") if isinstance(metadata, dict) else ""
    etag = metadata.get("server_etag") if isinstance(metadata, dict) else ""
    url = str(entry.get("url", "") or "") if isinstance(entry, dict) else ""
    if (not digest and not etag) or not url:
        return
    def _url_key(value):
        try:
            parsed = urlparse(str(value or ""))
            if parsed.scheme and parsed.netloc:
                return (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/")
        except Exception:
            pass
        return str(value or "")

    current_url_key = _url_key(url)
    previous = []
    for other in payload.get("upload_metadata") or []:
        if other is entry or not isinstance(other, dict):
            continue
        other_meta = other.get("metadata")
        other_digest = other_meta.get("server_sha256") if isinstance(other_meta, dict) else ""
        other_etag = other_meta.get("server_etag") if isinstance(other_meta, dict) else ""
        other_url = str(other.get("url", "") or "")
        same_hash = bool(digest and other_digest and other_digest == digest)
        same_etag = bool(etag and other_etag and other_etag == etag)
        if ((same_hash or same_etag) and other_url and
                _url_key(other_url) != current_url_key):
            previous.append(other)
    if not previous:
        return
    entry["possible_shared_object"] = True
    evidence = []
    if digest:
        evidence.append("server_sha256")
    if etag:
        evidence.append("server_etag")
    entry["shared_object_evidence"] = evidence
    entry["shared_with"] = [str(item.get("filename", "") or "") for item in previous]
    for other in previous:
        other["possible_shared_object"] = True
        other["shared_object_evidence"] = list(dict.fromkeys(
            list(other.get("shared_object_evidence") or []) + evidence))
        names = list(other.get("shared_with") or [])
        if entry.get("filename") and entry["filename"] not in names:
            names.append(entry["filename"])
        other["shared_with"] = names


def _preflight_policy_paths(policy, paths, label, *, check_size=True):
    """Validate a complete selected queue before its first upload request."""
    validator = getattr(policy, "validate_path", None)
    if not callable(validator):
        return
    for path in paths or ():
        path = str(path or "").strip()
        if not path:
            raise RuntimeError(f"{label}存在空文件路径")
        try:
            try:
                validator(os.path.basename(path), path, check_size=check_size)
            except TypeError:
                # Keep compatibility with narrow test/third-party policy
                # adapters that predate the explicit simpleupload size flag.
                validator(os.path.basename(path), path)
        except UploadPolicyError as exc:
            raise RuntimeError(
                f"{label}整组选中文件未通过网页预检：{exc}") from exc


def _upload_one(client, ctx, payload, cache, path, label, render_size=None,
                upload_surface="editor", upload_target=None, cache_slot=None):
    """上传一个文件，命中缓存则复用。返回 url；失败抛 RuntimeError。"""
    ctx.check_cancelled()
    if render_size:
        raise UploadPolicyError(
            "为保持与后台直接上传一致，软件不在客户端裁切或转码；"
            "请使用原文件上传或打开原生网页设置尺寸")
    target = upload_target or (payload.get('target_field') or 'content')
    if upload_surface == 'field' and upload_target is None:
        target = 'pics' if label == '轮播图' else 'ico'
    try:
        variant = str(upload_surface or "editor") + ':' + target + ':' + label
        policy = getattr(client, '_upload_policies', {}).get((upload_surface, target))
        if policy is not None:
            variant += ':' + policy.fingerprint()
        if upload_surface == "editor":
            # The same file/field can be sent through UEditor's dialog,
            # autoupload or simpleupload entry points.  Their query/header,
            # multipart and compression semantics differ, so a retry cache
            # from one mode must never satisfy another mode.
            variant += ':ueditor:' + str(
                payload.get("ueditor_upload_mode") or "dialog").strip().lower()
        # A browser upload control is occurrence-based: two equal files in
        # two gallery rows (or two repeated file controls) normally cause two
        # upload callbacks.  Keep the retry cache, but bind it to the stable
        # row/control slot when the caller supplies one.  Legacy direct calls
        # without a slot retain the old content-addressed behaviour.
        if cache_slot is not None:
            variant += ':slot:' + str(cache_slot)
        key = file_key(payload.get("site_id", ""), path, variant)
    except OSError as exc:
        raise RuntimeError(
            f"{label}无法读取（可能已被移动或网络盘断开）: {path}（{exc}）") from exc
    cached = cache.get(key)
    if cached:
        if _cached_upload_is_missing(client, cached):
            cache.pop(key, None)
            ctx.log(f"已上传缓存路径不存在，按网页行为重新上传: {os.path.basename(path)}")
        else:
            ctx.log(f"使用已上传缓存: {os.path.basename(path)} → {cached}")
            try:
                ctx.add_uploaded_bytes(os.path.getsize(path))
            except (OSError, TypeError, ValueError):
                pass
            return cached
    ctx.log(f"上传{label}: {os.path.basename(path)}")
    upload_kwargs = {
        "formcheck": payload.get("formcheck", ""),
        "render_size": render_size,
        "upload_surface": upload_surface,
        "upload_target": target,
    }
    if upload_surface == "editor":
        # A selected image in UEditor's image dialog uses WebUploader.  The
        # automatic paste/drop path can opt into autoupload explicitly; keep
        # the dialog as the normal desktop equivalent of a manual selection.
        # Avoid passing the optional keyword to legacy/native-field adapters,
        # which intentionally expose the narrower old signature.
        upload_kwargs["ueditor_upload_mode"] = str(
            payload.get("ueditor_upload_mode") or "dialog").strip().lower()
    url, error = client.upload_image(path, **upload_kwargs)
    if not url:
        upload_result = getattr(client, 'last_upload_result', {}) or {}
        if upload_result.get('outcome') == 'native_only':
            reason = str(error or upload_result.get(
                'native_reason') or '该上传必须由认证原生网页处理')
            raise _native_upload_required(reason, client, upload_surface, target,
                                          kind="image")
        if upload_result.get('outcome') == 'unknown':
            raise UploadOutcomeUnknown(f"{label}上传结果未知: {os.path.basename(path)} - {error}")
        raise RuntimeError(f"{label}上传失败: {os.path.basename(path)} - {error}")
    upload_result = getattr(client, 'last_upload_result', {}) or {}
    metadata = upload_result.get('metadata') or {}
    policy_meta = upload_result.get('policy') or {}
    client_transform = upload_result.get('client_transform') or {}
    try:
        # Count the actual client bytes after any browser-like compression or
        # explicit crop; source size is only the fallback estimate.
        ctx.add_uploaded_bytes(int(metadata.get('client_bytes') or
                                   os.path.getsize(path)))
    except (OSError, TypeError, ValueError):
        pass
    # Always emit one entry for a confirmed upload, even when the endpoint
    # returns only a bare URL and no metadata envelope.  The browser's upload
    # callback still fires in that case and the desktop UI must be able to
    # replace its local thumbnail immediately; metadata is an enhancement,
    # not the condition for acknowledging the callback.
    entry = {
        'label': label, 'filename': os.path.basename(path),
        'url': url,
        'metadata': metadata, 'policy': policy_meta,
        'client_transform': client_transform,
        'outcome': upload_result.get('outcome', '')}
    if cache_slot is not None:
        entry['cache_slot'] = str(cache_slot)
    lock = getattr(ctx, "_lock", None)
    with lock if lock is not None else nullcontext():
        payload.setdefault('upload_metadata', []).append(entry)
        _mark_possible_shared_object(payload, entry)
    # Stream the same evidence to the UI immediately.  The final result
    # still carries the complete list; this event is only an early,
    # non-authoritative rendering aid while later uploads continue.
    ctx.upload_metadata(entry)
    notices = [metadata.get(k) for k in ('notice', 'warning') if metadata.get(k)]
    if isinstance(metadata.get('data'), dict):
        notices.extend(metadata['data'].get(k) for k in ('notice', 'warning') if metadata['data'].get(k))
    for notice in notices:
        ctx.log(f"后台上传提示（{os.path.basename(path)}）：{notice}")
    if client_transform:
        ctx.log(f"按网页编辑器压缩图片（边界 {client_transform.get('border')}，质量 {client_transform.get('quality')}）")
    with lock if lock is not None else nullcontext():
        cache[key] = url
    return url


def _upload_media_one(client, ctx, payload, cache, path, label,
                      upload_surface="editor", upload_target=None,
                      media_kind="file", cache_slot=None):
    """Upload a non-image media asset through the discovered editor policy."""
    ctx.check_cancelled()
    target = upload_target or (payload.get("target_field") or "content")
    try:
        kind = str(media_kind or "file").strip().lower()
        variant = f"media:{upload_surface}:{target}:{kind}:{label}"
        policies = getattr(client, '_upload_policies', {})
        policy = policies.get((upload_surface, target, kind))
        if policy is None:
            policy = policies.get((upload_surface, target))
        if policy is not None:
            variant += ':' + policy.fingerprint()
        if upload_surface == "editor":
            variant += ':ueditor:' + str(
                payload.get("ueditor_upload_mode") or "dialog").strip().lower()
        if cache_slot is not None:
            variant += ':slot:' + str(cache_slot)
        key = file_key(payload.get("site_id", ""), path, variant)
    except OSError as exc:
        raise RuntimeError(f"{label}无法读取：{path}（{exc}）") from exc
    cached = cache.get(key)
    if cached:
        if _cached_upload_is_missing(client, cached):
            cache.pop(key, None)
            ctx.log(f"已上传缓存路径不存在，按网页行为重新上传: {os.path.basename(path)}")
        else:
            ctx.log(f"使用已上传缓存: {os.path.basename(path)} → {cached}")
            try:
                ctx.add_uploaded_bytes(os.path.getsize(path))
            except (OSError, TypeError, ValueError):
                pass
            return cached
    ctx.log(f"上传{label}: {os.path.basename(path)}")
    upload_kwargs = {
        "formcheck": payload.get("formcheck", ""),
        "upload_surface": upload_surface,
        "upload_target": target,
        "media_kind": kind,
    }
    if upload_surface == "editor":
        # Video and attachment dialogs use the same WebUploader wire format
        # as the image dialog (encode=utf-8 and X_Requested_With).  A caller
        # handling a true automatic XHR may still opt into autoupload through
        # the payload.
        upload_kwargs["ueditor_upload_mode"] = str(
            payload.get("ueditor_upload_mode") or "dialog").strip().lower()
    url, error = client.upload_file(path, **upload_kwargs)
    if not url:
        upload_result = getattr(client, 'last_upload_result', {}) or {}
        if upload_result.get('outcome') == 'native_only':
            reason = str(error or upload_result.get(
                'native_reason') or '该媒体必须由认证原生网页处理')
            raise _native_upload_required(reason, client, upload_surface, target,
                                          kind=kind)
        if upload_result.get('outcome') == 'unknown':
            raise UploadOutcomeUnknown(f"{label}上传结果未知: {os.path.basename(path)} - {error}")
        raise RuntimeError(f"{label}上传失败: {os.path.basename(path)} - {error}")
    upload_result = getattr(client, 'last_upload_result', {}) or {}
    metadata = upload_result.get('metadata') or {}
    policy_meta = upload_result.get('policy') or {}
    try:
        ctx.add_uploaded_bytes(int(metadata.get('client_bytes') or
                                   os.path.getsize(path)))
    except (OSError, TypeError, ValueError):
        pass
    # Generic attachments, audio and video can likewise return only a URL.
    # Keep their callback observable instead of making the UI wait for task
    # completion merely because no optional metadata was supplied.
    entry = {
        'label': label, 'filename': os.path.basename(path),
        'url': url,
        'metadata': metadata, 'policy': policy_meta,
        'outcome': upload_result.get('outcome', '')}
    if cache_slot is not None:
        entry['cache_slot'] = str(cache_slot)
    lock = getattr(ctx, "_lock", None)
    with lock if lock is not None else nullcontext():
        payload.setdefault('upload_metadata', []).append(entry)
        _mark_possible_shared_object(payload, entry)
    ctx.upload_metadata(entry)
    notices = [metadata.get(k) for k in ('notice', 'warning') if metadata.get(k)]
    if isinstance(metadata.get('data'), dict):
        notices.extend(metadata['data'].get(k) for k in ('notice', 'warning') if metadata['data'].get(k))
    for notice in notices:
        ctx.log(f"后台上传提示（{os.path.basename(path)}）：{notice}")
    with lock if lock is not None else nullcontext():
        cache[key] = url
    return url


def _apply_media_assets(client, ctx, payload, fields, cache, total, done):
    assets = list(payload.get("media_assets") or [])
    if not assets:
        return done
    target = payload.get("target_field") or "content"
    mapping = {}
    grouped_paths = {}
    for item in assets:
        path = str(item.get("path", "") or "")
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"媒体文件不存在：{path}")
        kind = str(item.get('media_kind', 'file') or 'file').strip().lower()
        if kind not in ('video', 'audio', 'file'):
            kind = 'file'
        grouped_paths.setdefault(kind, []).append(path)
    policies = getattr(client, "_upload_policies", {}) or {}
    for kind, paths in grouped_paths.items():
        policy = policies.get(("editor", target, kind))
        if policy is None:
            policy = policies.get(("editor", target))
        _preflight_policy_paths(policy, paths, f"{kind}媒体")
    # UEditor/media controls create one independent XHR per selected
    # resource.  Keep the returned mapping in DOM order, but let real
    # Pboot clients fan out the requests so completion callbacks can arrive
    # in the same order as the browser.  Test doubles remain deterministic
    # through _parallel_uploads' serial fallback.
    specs = []
    for index, item in enumerate(assets):
        path = str(item.get("path", "") or "")
        kind = str(item.get('media_kind', 'file') or 'file').strip().lower()
        if kind not in ('video', 'audio', 'file'):
            kind = 'file'

        def upload(worker, index=index, item=item, path=path, kind=kind):
            return _upload_media_one(
                worker, ctx, payload, cache, path,
                f"{item.get('tag', '媒体')}资源",
                media_kind=kind, cache_slot=f"media:{index}")

        specs.append(upload)
    urls, done = _parallel_uploads(
        client, ctx, payload, cache, specs, total, done)
    for item, url in zip(assets, urls):
        mapping[str(item.get("src", "") or "")] = url
    value, replaced = rewrite_html_media(str(fields.get(target, "") or ""), mapping)
    fields[target] = value
    ctx.log(f"已将 {replaced} 个视频/音频/附件地址改写为后台地址")
    return done


def _upload_body_image(client, ctx, payload, cache, path, label, cache_slot=None):
    snapshot_dir = payload.get('_first_source_dir')
    if snapshot_dir:
        original_path = os.path.abspath(path)
        # Several browser-style XHR workers can enter this helper at once.
        # Snapshot creation must therefore be serialized; otherwise two
        # workers can both copy the same source and race when resolving the
        # stable first-image fingerprint.  The lock is task-local and never
        # crosses the worker client boundary.
        snapshot_lock = payload.setdefault('_snapshot_lock', threading.RLock())
        with snapshot_lock:
            snapshots = payload.setdefault('_first_local_snapshots', {})
            if original_path not in snapshots:
                folder = os.path.join(snapshot_dir, uuid.uuid4().hex)
                os.mkdir(folder)
                destination = os.path.join(folder, os.path.basename(path))
                with open(path, 'rb') as source, open(destination, 'xb') as copied:
                    while True:
                        ctx.check_cancelled()
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        copied.write(chunk)
                snapshots[original_path] = destination
            path = snapshots[original_path]
    fingerprint = file_key('', path) if payload.get('thumbnail_from_first') else None
    url = _call_upload_with_slot(
        _upload_one, (client, ctx, payload, cache, path, label), {}, cache_slot)
    if fingerprint is not None:
        if file_key('', path) != fingerprint:
            raise RuntimeError('正文图片文件在上传期间变化，请重新选择文件')
        lock = getattr(ctx, "_lock", None)
        with lock if lock is not None else nullcontext():
            payload.setdefault('_body_upload_sources', []).append(
                {'url': url, 'path': path, 'fingerprint': fingerprint})
    return url


def _apply_images(client, ctx, payload, fields, cache, total, done):
    """Upload inline resources and explicit additional images without dropping either."""
    target = payload.get("target_field") or "content"
    inline = list(payload.get("inline_images") or [])
    manual = list(payload.get("image_paths") or [])
    first_url = ""
    url_by_src = {}
    policy = (getattr(client, "_upload_policies", {}) or {}).get(
        ("editor", target))
    # UEditor image selection is a queue.  Reject every invalid local item
    # before the first upload callback, matching browser-side preflight.
    _preflight_policy_paths(
        policy,
        [item.get("path", "") for item in inline] + manual,
        "正文图片",
        check_size=str(payload.get("ueditor_upload_mode") or "dialog").strip().lower()
        != "simpleupload")
    inline_specs = []
    for index, item in enumerate(inline):
        # Inline sources are scanned in DOM order.  The slot keeps retry
        # semantics stable while preserving separate uploads for distinct
        # local occurrences; repeated identical HTML src values still map to
        # one rewritten source as the browser/editor does.  Each item gets
        # its own XHR worker when the real client supports the browser-style
        # queue, while _parallel_uploads returns URLs in this DOM order.
        def upload(worker, index=index, item=item):
            return _upload_body_image(
                worker, ctx, payload, cache, item.get("path", ""),
                "正文内置图", cache_slot=f"inline:{index}")
        inline_specs.append(upload)
    inline_urls, done = _parallel_uploads(
        client, ctx, payload, cache, inline_specs, total, done)
    for item, url in zip(inline, inline_urls):
        url_by_src[item.get("src", "")] = url
        first_url = first_url or url
    if inline:
        fields[target], replaced = rewrite_html_images(
            fields.get(target, ""), url_by_src,
            width_mode=payload.get("width_mode", "preserve"))
        ctx.log(f"已将 {replaced} 张内置图片改写为后台地址（保留位置和原属性）")
    manual_specs = []
    for index, path in enumerate(manual):
        def upload(worker, index=index, path=path):
            return _upload_body_image(
                worker, ctx, payload, cache, path, "图片",
                cache_slot=f"manual:{index}")
        manual_specs.append(upload)
    manual_urls, done = _parallel_uploads(
        client, ctx, payload, cache, manual_specs, total, done)
    uploaded = []
    for path, url in zip(manual, manual_urls):
        uploaded.append((os.path.basename(path), url))
        first_url = first_url or url
    if uploaded:
        current_fields = dict(fields)
        for alt_name in ("image-alt", "image_alt"):
            if alt_name not in current_fields:
                current_fields[alt_name] = (payload.get("parsed_fields") or {}).get(alt_name, "")
        value, location, count = insert_images_into_fields(
            current_fields, uploaded, target, payload.get("strategy", "top"),
            payload.get("width_mode", "preserve"))
        fields[target] = value
        ctx.log(f"插入 {count} 张图片到 [{target}]，位置: {location}")
    return done, first_url


def _apply_remote_images(client, ctx, payload, fields, cache, total, done):
    """Catch externally hosted images only when the active editor enables it."""
    remote = []
    seen = set()
    original_by_resolved = {}
    for item in payload.get('remote_images') or []:
        value = str(item.get('src', '') if isinstance(item, dict) else item or '').strip()
        original = str(item.get('original_src', '') if isinstance(item, dict) else '').strip()
        if value and value not in seen:
            seen.add(value)
            remote.append(value)
        if value and original and original != value:
            original_by_resolved[value] = original
    if not remote:
        return done
    target = payload.get('target_field') or 'content'
    policy = getattr(client, '_upload_policies', {}).get(('editor', target))
    catcher = ((getattr(policy, 'metadata', {}) or {}).get('remote_catcher')
               if policy is not None else None) or {}
    if not catcher.get('enabled'):
        ctx.log(f'正文含 {len(remote)} 个外部图片，当前网页编辑器未启用远程抓取，保留原地址')
        done += 1
        ctx.progress(done, total, '外部图片保留原地址')
        return done
    if policy is not None and callable(getattr(policy, 'fingerprint', None)):
        policy_key = policy.fingerprint()
    else:
        policy_key = repr(getattr(policy, 'metadata', {}) if policy is not None else {})
    cached_mapping, pending = {}, []
    for source in remote:
        cache_key = '__remote__:' + hashlib.sha256(
            (source + ':' + policy_key).encode('utf-8')).hexdigest()
        cached = str(cache.get(cache_key, '') or '').strip()
        reference = getattr(client, "base_url", "") or getattr(client, "admin_url", "")
        if (cached and _same_site_http_url(cached, reference) and
                not _cached_upload_is_missing(client, cached)):
            cached_mapping[source] = cached
        else:
            if cached:
                cache.pop(cache_key, None)
            pending.append(source)
    ctx.check_cancelled()
    mapping = dict(cached_mapping)
    if cached_mapping:
        ctx.log(f'复用已确认的远程图片抓取结果：{len(cached_mapping)} 个')
    if pending:
        ctx.log(f'按网页编辑器远程抓取配置处理 {len(pending)} 个外部图片')
    try:
        fresh = client.catch_remote_images(
            pending, upload_surface='editor', upload_target=target,
            base_url=getattr(client, 'base_url', '')) if pending else {}
        mapping.update(fresh)
        for source in pending:
            target_url = mapping.get(source)
            if target_url:
                cache_key = '__remote__:' + hashlib.sha256(
                    (source + ':' + policy_key).encode('utf-8')).hexdigest()
                cache[cache_key] = target_url
    except UploadOutcomeUnknown:
        raise
    except Exception as exc:
        if getattr(client, 'last_upload_result', {}).get('outcome') == 'unknown':
            raise UploadOutcomeUnknown(f'远程图片抓取结果未知：{exc}') from exc
        raise RuntimeError(f'远程图片抓取失败：{exc}') from exc
    # A document <base href> can make the browser submit an absolute URL
    # while the source HTML still contains its relative spelling.  Keep both
    # keys so cached and fresh catcher results are rewritten at the original
    # byte location instead of leaving the relative URL untouched.
    for resolved, original in original_by_resolved.items():
        if resolved in mapping:
            mapping[original] = mapping[resolved]
    # Every resulting remote object gets the same immediate callback and
    # server-side evidence as a local upload.  Cached mappings are included so
    # the current task's thumbnail/metadata area is populated after restart.
    for source in remote:
        target_url = mapping.get(source)
        if target_url and _same_site_http_url(
                target_url,
                getattr(client, "base_url", "") or getattr(client, "admin_url", "")):
            _record_remote_upload(
                client, ctx, payload, source, target_url,
                getattr(policy, "metadata", {}) if policy is not None else {})
    value, replaced = rewrite_html_images(str(fields.get(target, '') or ''), mapping)
    fields[target] = value
    ctx.log(f'已将 {replaced} 个外部图片地址改写为后台地址')
    done += 1
    ctx.progress(done, total, '外部图片抓取完成')
    return done


def _apply_media_uploads(client, ctx, payload, fields, cache, total, done):
    """Upload native non-image ``type=file`` controls through their real policy.

    The browser never submits a local path from a file input.  Each selected
    file is therefore uploaded first, then its returned server value is put
    into the corresponding hidden/text field before the normal content POST.
    """
    items = list(payload.get("media_uploads") or [])
    # Layui checks ``number`` against the complete selected queue before it
    # starts any XHR.  Do the same preflight so an invalid oversized
    # selection cannot leave the first few files on the server before the
    # desktop task discovers the overflow.
    queue_counts = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("附件上传项格式无效")
        field = str(item.get("field", "") or "").strip()
        path = str(item.get("path", "") or "").strip()
        if not field or not path or not os.path.isfile(path):
            raise RuntimeError(f"附件字段 {field or '(未知)'} 的本地文件不存在")
        try:
            queue_limit = int(item.get("max_files") or 0)
        except (TypeError, ValueError):
            queue_limit = 0
        if queue_limit > 0:
            queue_counts[field] = queue_counts.get(field, 0) + 1
            if queue_counts[field] > queue_limit:
                raise RuntimeError(f"附件字段 {field} 最多选择 {queue_limit} 个文件")

    grouped_items = {}
    for index, item in enumerate(items):
        field = str(item.get("field", "") or "").strip()
        grouped_items.setdefault(field, []).append((index, item))

    # A submit can contain several independent file controls.  Once each
    # control's complete queue has passed its own browser ``number`` check,
    # the browser may have multiple XHRs in flight across those controls too.
    # Build one global queue so attachments selected in different fields do
    # not become an accidental serial bottleneck; grouping is restored below
    # strictly by field and original DOM order.
    specs = []
    spec_items = []
    for field, field_items in grouped_items.items():
        policy = getattr(client, "_upload_policies", {}).get(("field", field))
        validate_path = getattr(policy, "validate_path", None)
        if callable(validate_path):
            for _index, item in field_items:
                try:
                    validate_path(
                        os.path.basename(str(item.get("path", "") or "")),
                        str(item.get("path", "") or ""))
                except UploadPolicyError as exc:
                    raise RuntimeError(
                        f"附件字段 {field} 的整组选中文件未通过网页预检：{exc}") from exc
        for index, item in field_items:
            media_kind = str(item.get('media_kind', 'file') or 'file').strip().lower()
            if media_kind not in ('video', 'audio', 'file'):
                media_kind = 'file'
            path = str(item.get("path", "") or "").strip()

            def upload(worker, field=field, index=index, item=item,
                       path=path, media_kind=media_kind):
                if item.get("image_upload", True):
                    return _upload_one(
                        worker, ctx, payload, cache, path, f"附件 {field}",
                        upload_surface="field", upload_target=field,
                        cache_slot=f"field:{field}:{index}")
                return _upload_media_one(
                    worker, ctx, payload, cache, path, f"附件 {field}",
                    upload_surface="field", upload_target=field,
                    media_kind=media_kind, cache_slot=f"field:{field}:{index}")

            specs.append(upload)
            spec_items.append((field, index, item))
    urls, done = _parallel_uploads(
        client, ctx, payload, cache, specs, total, done)
    grouped = {}
    for (field, index, _item), url in zip(spec_items, urls):
        grouped.setdefault(field, []).append((index, url))
    for field in grouped:
        grouped[field].sort(key=lambda pair: pair[0])
        grouped[field] = [url for _index, url in grouped[field]]
    for field, urls in grouped.items():
        descriptor_multiple = any(
            bool(item.get("multiple")) for item in (payload.get("media_uploads") or [])
            if str(item.get("field", "") or "").strip() == field)
        if descriptor_multiple:
            old = fields.get(field, [])
            old = old if isinstance(old, list) else ([old] if old else [])
            fields[field] = list(old) + urls
        else:
            fields[field] = urls[-1]
    return done


def _resolve_thumbnail(client, ctx, payload, fields, cache, total, done, first_url):
    """确定缩略图：首图 / 手动URL / 独立文件上传。"""
    if payload.get('thumbnail_mode') == 'clear':
        fields['ico'] = ''
        ctx.log('缩略图明确提交空值；后台仍可能按自身规则自动取图，保存后将回读核对')
        return done
    if payload.get('thumbnail_mode') == 'none':
        return done
    ico_url = payload.get("thumbnail_url", "")
    thumbnail_path = payload.get("thumbnail_path")
    if thumbnail_path:
        # 独立图片是用户的明确选择，拥有最高优先级。
        ico_url = _upload_one(
            client, ctx, payload, cache, thumbnail_path, "缩略图",
            upload_surface="field", cache_slot="thumbnail:independent")
        done += 1
        ctx.progress(done, total, os.path.basename(thumbnail_path))
        ctx.log(f"缩略图使用独立文件：{os.path.basename(thumbnail_path)}")
    elif payload.get("thumbnail_from_first"):
        if not first_url:
            raise RuntimeError('最终正文没有可用的同站首图，请选择独立图片或其他缩略图模式')
        identity = image_identity(first_url, client.base_url)
        sources = [item for item in payload.get('_body_upload_sources', [])
                   if image_identity(item['url'], client.base_url) == identity]
        if len({item['fingerprint'] for item in sources}) > 1:
            raise RuntimeError('首图地址对应多个不同原文件，无法确定独立缩略图来源')
        if sources:
            source = sources[0]
            if file_key('', source['path']) != source['fingerprint']:
                raise RuntimeError('首图原文件在正文上传后变化，请重新选择')
            ico_url = _upload_one(client, ctx, payload, cache, source['path'],
                                  '缩略图', upload_surface='field',
                                  cache_slot='thumbnail:first')
            ctx.log('首图缩略图使用本地原文件，独立经过当前ico上传控件')
        else:
            with downloaded_source(client, first_url, ctx) as path:
                ico_url = _upload_one(client, ctx, payload, cache, path,
                                      '缩略图', upload_surface='field',
                                      cache_slot='thumbnail:first')
        if image_identity(ico_url, client.base_url) == identity:
            raise UploadOutcomeUnknown('缩略图上传返回正文同一文件路径，不能确认独立保存，请核对后台文件处理')
        done += 1
        ctx.progress(done, total, '首图独立缩略图')
    if ico_url or payload.get('thumbnail_mode') == 'url':
        fields["ico"] = ico_url
    return done


def _srcset_candidates(value, responsive_context=None, sizes=""):
    """Return srcset URLs in browser-like quality order.

    ``srcset_url_spans`` already understands commas inside data URLs.  The
    small score below is intentionally deterministic: desktop WebView uses
    the largest declared width/density candidate, while a plain candidate
    remains valid with score zero.  This is not a replacement for a browser's
    viewport selection, but it avoids silently choosing a 1x placeholder when
    the markup explicitly provides a larger source.
    """
    context = normalize_responsive_context(responsive_context)
    return responsive_srcset_candidates(value, context.get("viewport_width"),
                                        context.get("device_pixel_ratio", 1.0),
                                        sizes, context.get("viewport_height"), context)


def _image_source_candidates(image, responsive_context=None):
    """Yield likely final image sources for one ``img`` element.

    Lazy-loading attributes are preferred only when ``src`` is absent or is a
    known placeholder.  Otherwise the normal ``src``/``srcset`` path remains
    authoritative, matching how most CMS pages progressively enhance the
    image without unexpectedly replacing an already real URL.
    """
    attrs = image.attrs or {}
    src = str(attrs.get("src", "") or "").strip()
    lowered = src.lower()
    placeholder = (not src or lowered.startswith(("data:", "blob:")) or
                   any(token in lowered for token in
                       ("placeholder", "transparent", "spacer", "blank", "loading")))
    candidates = []
    if placeholder:
        for name in ("data-src", "data-original", "data-lazy-src", "data-url"):
            value = str(attrs.get(name, "") or "").strip()
            if value:
                candidates.append(value)
        for name in ("data-srcset", "srcset"):
            candidates.extend(_srcset_candidates(attrs.get(name, ""), responsive_context,
                                                 attrs.get("sizes", "")))
    else:
        candidates.extend(_srcset_candidates(attrs.get("srcset", ""), responsive_context,
                                             attrs.get("sizes", "")))
        candidates.append(src)
        # A data-src is often populated by a lazy loader after the initial
        # src. Keep it as a fallback, never as an unconditional replacement.
        for name in ("data-src", "data-original", "data-lazy-src"):
            value = str(attrs.get(name, "") or "").strip()
            if value:
                candidates.append(value)
    # Preserve order while avoiding duplicate requests/identity checks.
    seen = set()
    return [value for value in candidates if value and not (value in seen or seen.add(value))]


def _browser_first_image_url(browser_first_image, base_url):
    """Return a browser-selected first-image URL only when it is safe.

    The WebView is deliberately only a candidate selector.  Treat its result
    as untrusted input at the worker boundary: it must be an absolute HTTP(S)
    URL on the same origin as the authenticated backend, without credentials
    or a fragment.  The final HTML membership check below also prevents a
    candidate captured before local/remote image rewriting from becoming
    stale.
    """
    value = browser_first_image
    if isinstance(value, dict):
        value = value.get("url") or value.get("current_src") or ""
    value = str(value or "").strip()
    if not value or any(ord(char) < 32 for char in value):
        return ""
    parsed = urlparse(value)
    if (parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or
            parsed.username is not None or parsed.password is not None or
            not str(base_url or "").strip() or
            not permitted_transition(str(base_url or ""), value)):
        return ""
    return parsed._replace(fragment="").geturl()


def _first_content_image(fields, target_field, base_url, responsive_context=None,
                         browser_first_image=None):
    """从最终正文取第一个可信图片地址。

    本地图在此之前已被 rewrite_html_images 换成上传地址；
    对原本就是 HTTP(S) 的图片只接受当前站点，避免把
    外站热链静默写入 PbootCMS 缩略图字段。常见 lazy/srcset/picture
    标记会按静态候选顺序读取，但不执行页面 JavaScript。
    """
    responsive_context = normalize_responsive_context(responsive_context)
    html = str((fields or {}).get(target_field or "content", "") or "")
    if not html:
        return ""
    declared_base = document_base_url(html, base_url)
    effective_base = declared_base or base_url
    soup = BeautifulSoup(html, "html.parser")
    browser_url = _browser_first_image_url(browser_first_image, base_url)
    # The browser candidate is authoritative only if it is still represented
    # by the final body after local/remote uploads and detail replacements.
    # Build a normalized set while retaining the same static candidate order
    # used by the fallback parser.
    final_candidates = set()
    for image in soup.find_all("img"):
        candidates = _image_source_candidates(image, responsive_context)
        picture = image.find_parent("picture")
        if picture:
            source_candidates = []
            for source in picture.find_all("source"):
                if not media_type_supported(source.get("type", "")):
                    continue
                if not _media_matches(source.get("media", ""),
                                      (responsive_context or {}).get("viewport_width"),
                                      (responsive_context or {}).get("viewport_height"),
                                      (responsive_context or {}).get("device_pixel_ratio"),
                                      responsive_context):
                    continue
                source_candidates.extend(_srcset_candidates(
                    source.get("srcset", ""), responsive_context,
                    source.get("sizes", "")))
            candidates = source_candidates + candidates
        for src in candidates:
            parsed = urlparse(src)
            if parsed.scheme.lower() in ("data", "blob", "javascript", "file"):
                continue
            try:
                resolved = urljoin(str(effective_base).rstrip('/') + '/', src)
            except Exception:
                continue
            if (urlparse(resolved).scheme.lower() in ("http", "https") and
                    permitted_transition(str(base_url or ""), resolved)):
                final_candidates.add(urlparse(resolved)._replace(fragment="").geturl())
    if browser_url and browser_url in final_candidates:
        return browser_url if declared_base else browser_url
    for image in soup.find_all("img"):
        candidates = _image_source_candidates(image, responsive_context)
        picture = image.find_parent("picture")
        if picture:
            # ``source`` precedes ``img`` in the native picture algorithm.
            # Keep all candidates static (no media-query execution) and let
            # same-origin validation fall back to the img candidate when a
            # source is external or otherwise unusable.
            source_candidates = []
            for source in picture.find_all("source"):
                if not media_type_supported(source.get("type", "")):
                    continue
                if not _media_matches(source.get("media", ""),
                                      (responsive_context or {}).get("viewport_width"),
                                      (responsive_context or {}).get("viewport_height"),
                                      (responsive_context or {}).get("device_pixel_ratio"),
                                      responsive_context):
                    continue
                source_candidates.extend(_srcset_candidates(
                    source.get("srcset", ""), responsive_context,
                    source.get("sizes", "")))
            candidates = source_candidates + candidates
        for src in candidates:
            parsed = urlparse(src)
            scheme = parsed.scheme.lower()
            if scheme in ("data", "blob", "javascript", "file"):
                continue
            try:
                resolved = urljoin(str(effective_base).rstrip('/') + '/', src)
                if not permitted_transition(str(base_url or ""), resolved):
                    continue
            except Exception:
                continue
            # Returning the resolved URL is important when a document
            # declares <base href>; downloaded_source must fetch the same
            # resource a browser would display, not reinterpret the relative
            # spelling against the admin origin.
            return resolved if declared_base else src
    return ""


def _apply_carousel(client, ctx, payload, fields, cache, total, done):
    """渲染并上传轮播图，按编辑表单现有格式写入 ``pics``。"""
    paths = list(payload.get("carousel_paths") or [])
    plan = normalize_gallery_plan(payload['gallery_plan']) if 'gallery_plan' in payload else None
    if plan is not None and paths != [item['value'] for item in plan if item['kind'] == 'file']:
        raise RuntimeError('图集计划与上传文件顺序不一致')
    if not paths and plan is None:
        return done
    size = payload.get("carousel_size")
    if size not in (None, "", "original"):
        # A legacy caller may still send the removed local-crop option.  Do
        # not silently ignore it or produce a different file; the worker's
        # UploadPolicyError path hands the exact authenticated page to the
        # native browser instead.
        raise UploadPolicyError(
            "客户端裁切/转码设置会改变客户端上传字节；为保持与后台直接上传一致，"
            "请使用原文件上传或打开原生网页设置尺寸")
    policy = getattr(client, "_upload_policies", {}).get(("field", "pics"))
    policy_meta = getattr(policy, "metadata", {}) if policy is not None else {}
    browser_multiple = bool((policy_meta or {}).get("multiple"))
    try:
        queue_limit = int((policy_meta or {}).get("number") or 0)
    except (TypeError, ValueError):
        queue_limit = 0
    if queue_limit > 0 and len(paths) > queue_limit:
        # Layui rejects the entire selection before its first XHR when the
        # literal ``number`` limit is exceeded.
        raise RuntimeError(f"轮播图最多选择 {queue_limit} 个文件")
    _preflight_policy_paths(policy, paths, "轮播图")
    if browser_multiple and len(paths) > 1 and isinstance(client, PbootCMSClient):
        specs = []
        for index, path in enumerate(paths):
            def upload(worker, index=index, path=path):
                return _upload_one(
                    worker, ctx, payload, cache, path, "轮播图",
                    upload_surface="field",
                    cache_slot=f"carousel:{index}")
            specs.append(upload)
        urls, done = _parallel_uploads(
            client, ctx, payload, cache, specs, total, done)
    else:
        urls = []
        for index, path in enumerate(paths):
            url = _upload_one(
                client, ctx, payload, cache, path, "轮播图",
                upload_surface="field",
                cache_slot=f"carousel:{index}")
            urls.append(url)
            done += 1
            ctx.progress(done, total, f"轮播图 {os.path.basename(path)}")
    if plan is not None:
        uploaded = iter(urls)
        output = [next(uploaded) if item['kind'] == 'file' else item['value'] for item in plan]
        raw = fields.get('pics', payload.get('carousel_existing_pics', ''))
        _, storage = _decode_carousel_values(raw)
        # Keep pairs inseparable; an empty plan explicitly clears the field.
        fields['pics'] = _encode_carousel_values(output, storage)
        fields['picstitle[]'] = [item['title'] for item in plan]
        ctx.log(f'图集明确保存 {len(plan)} 张，逐图标题及顺序一起提交；删除条目不删除服务器文件')
        return done
    raw_existing = (fields.get("pics", "") or
                    payload.get("carousel_existing_pics", "") or "")
    existing_values, storage = _decode_carousel_values(raw_existing)
    append_mode = payload.get("carousel_mode", "append") == "append"
    output_values = (existing_values + urls) if append_mode else urls
    fields["pics"] = _encode_carousel_values(output_values, storage)

    # Some PbootCMS templates expose one picstitle[] input per existing
    # carousel image. Preserve the full array when appending and reset it to
    # the correct length when replacing. Sites without this field receive no
    # extra POST key.
    title_field = str(payload.get("carousel_title_field", "") or "")
    if title_field:
        old_titles = payload.get("carousel_existing_titles") or []
        if not isinstance(old_titles, list):
            old_titles = [str(old_titles)]
        existing_count = len(existing_values)
        if append_mode and existing_count:
            old_titles = list(old_titles[:existing_count])
            old_titles.extend([""] * (existing_count - len(old_titles)))
            fields[title_field] = old_titles + ([""] * len(urls))
        else:
            fields[title_field] = [""] * len(urls)
    ctx.log(f"图集已处理 {len(urls)} 张（原文件上传，未本地裁切或转码）")
    return done


def _decode_carousel_values(raw):
    """返回 ``(图片列表, 存储格式)``，未知/空值按 Pboot 默认逗号。"""
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()], "list"
    text = str(raw or "").strip()
    if not text:
        return [], "comma"
    if text.startswith("["):
        try:
            values = json.loads(text)
            if isinstance(values, list):
                return [str(item).strip() for item in values if str(item).strip()], "json"
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    for separator, storage in (("\r\n", "newline"), ("\n", "newline"),
                               ("|", "pipe"), (";", "semicolon"),
                               (",", "comma")):
        if separator in text:
            return [item.strip() for item in text.split(separator) if item.strip()], storage
    return [text], "comma"


def _encode_carousel_values(values, storage):
    values = [str(item).strip() for item in (values or []) if str(item).strip()]
    if storage == "list":
        return values
    if storage == "json":
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if storage == "newline":
        return "\n".join(values)
    if storage == "pipe":
        return "|".join(values)
    if storage == "semicolon":
        return ";".join(values)
    return ",".join(values)


def _total_steps(payload):
    inline = list(payload.get("inline_images") or [])
    manual = list(payload.get("image_paths") or [])
    replacements = list(payload.get("image_replacements") or [])
    replacement_uploads = sum(
        1 for item in replacements
        if isinstance(item, dict) and str(item.get("local_path", "") or "").strip()
    )
    return (len(inline) + len(payload.get("media_assets") or []) + len(manual) + replacement_uploads
            + (1 if payload.get("remote_images") else 0)
            + len(payload.get("carousel_paths") or [])
            + len(payload.get("media_uploads") or [])
            + (1 if payload.get("thumbnail_path") or payload.get('thumbnail_from_first') else 0) + 1)


def _payload_byte_budget(payload):
    """Return the bytes expected to cross upload controls for this task.

    This is deliberately a best-effort budget: UEditor browser compression or
    an explicit crop can change the actual multipart bytes.  The upload
    result's ``client_bytes`` remains authoritative and replaces the estimate
    when each asset completes.  Missing/moved files are ignored here and are
    still rejected by the normal task path before sending a request.
    """
    paths = []
    for item in payload.get("inline_images") or []:
        if isinstance(item, dict):
            paths.append(item.get("path", ""))
    paths.extend(payload.get("image_paths") or [])
    for item in payload.get("image_replacements") or []:
        if isinstance(item, dict):
            paths.append(item.get("local_path", ""))
    for item in payload.get("media_assets") or []:
        if isinstance(item, dict):
            paths.append(item.get("path", ""))
    for item in payload.get("media_uploads") or []:
        if isinstance(item, dict):
            paths.append(item.get("path", ""))
    paths.extend(payload.get("carousel_paths") or [])
    if payload.get("thumbnail_path"):
        paths.append(payload.get("thumbnail_path"))
    total = 0
    for path in paths:
        try:
            total += max(0, int(os.path.getsize(str(path))))
        except (OSError, TypeError, ValueError):
            continue
    return total


def _report_upload_bytes(ctx, done, total, current, *, completed=0):
    """Emit a byte-aware progress event without changing step semantics."""
    if completed:
        ctx.add_uploaded_bytes(completed)
    ctx.progress(done, total, current,
                 bytes_done=ctx.bytes_done, bytes_total=ctx.bytes_total)


def _upload_detail_replacements(client, ctx, payload, cache, total, done):
    """Upload selected original-body image replacements, without editing HTML.

    Actual HTML replacement happens in ``edit_content`` only after that method
    has re-read the latest form.  This avoids an upload that takes minutes
    overwriting somebody else's newer article body.
    """
    replacement_items = list(payload.get("image_replacements") or [])
    policy = (getattr(client, "_upload_policies", {}) or {}).get(
        ("editor", payload.get("target_field") or "content"))
    _preflight_policy_paths(
        policy,
        [item.get("local_path", "") for item in replacement_items
         if isinstance(item, dict) and str(item.get("local_path", "") or "").strip()],
        "正文替图")
    # Replacing several images is another browser upload queue.  Upload all
    # local replacements through independent XHR workers, then rebuild the
    # result in the original DOM order so the later optimistic-concurrency
    # check still targets the exact image occurrence the user edited.
    specs = []
    spec_items = []
    for index, item in enumerate(replacement_items):
        path = str(item.get("local_path", "") or "")
        if not path:
            continue
        def upload(worker, index=index, item=item, path=path):
            return _upload_body_image(
                worker, ctx, payload, cache, path, "详情替换图",
                cache_slot=f"replacement:{index}")

        specs.append(upload)
        spec_items.append((index, item))
    urls, done = _parallel_uploads(
        client, ctx, payload, cache, specs, total, done)
    url_by_index = {index: url for (index, _item), url in zip(spec_items, urls)}
    uploaded = []
    for index, item in enumerate(replacement_items):
        url = url_by_index.get(index, "")
        entry = {"index": item.get("index"),
                 "expected_src": item.get("expected_src", ""),
                 "tag_fingerprint": item.get("tag_fingerprint", ""),
                 "url": url}
        for key in ("new_alt", "new_width", "new_height"):
            if key in item:
                entry[key] = str(item.get(key, ""))
        uploaded.append(entry)
    return uploaded, done


def _prepare_upload_policies(client, payload, ctx, *, editing):
    targets = []
    if any(payload.get(key) for key in ('inline_images', 'remote_images', 'image_paths', 'image_replacements')):
        targets.append(('editor', payload.get('target_field') or 'content'))
    media_target = payload.get('target_field') or 'content'
    for item in payload.get('media_assets') or []:
        kind = str(item.get('media_kind', 'file') or 'file').strip().lower()
        if kind not in ('video', 'audio', 'file'):
            kind = 'file'
        spec = ('editor', media_target, kind)
        if spec not in targets:
            targets.append(spec)
    if payload.get('thumbnail_path') or payload.get('thumbnail_from_first'):
        targets.append(('field', 'ico'))
    if payload.get('carousel_paths'):
        targets.append(('field', 'pics'))
    for item in payload.get('media_uploads') or []:
        field = str(item.get('field', '') or '').strip()
        if field and ('field', field) not in targets:
            targets.append(('field', field))
    if not targets:
        return
    ctx.check_cancelled()
    mcode = str(payload.get('mcode') or '')
    if not mcode:
        mcode = str(client._resolve_mcode(payload.get('scode')) or '')
    if not mcode:
        raise RuntimeError('无法确定实际上传表单模型')
    if editing:
        page = (payload.get('upload_page_url') or
                payload.get('edit_url_hint') or client._url(
                    f"Content/mod/id/{payload.get('article_id')}/mcode/{mcode}"))
    else:
        page = (payload.get('upload_page_url') or
                client._url(f'Content/index/mcode/{mcode}'))
    ctx.log('正在读取当前网页上传控件和编辑器配置')
    client.prepare_uploads(page, targets)
    # Do this check after discovering the real editor policy but before any
    # queue worker is created.  Otherwise one image in a multi-file queue
    # could start uploading while another worker discovers that exact browser
    # Canvas bytes require the native page.
    editor_target = payload.get('target_field') or 'content'
    editor_policy = (getattr(client, '_upload_policies', {}) or {}).get(
        ('editor', editor_target))
    editor_mode = str(payload.get('ueditor_upload_mode') or 'dialog').strip().lower()
    # UEditor's dialog path only enters Canvas for JPEG/JPG; PNG/GIF are
    # submitted byte-for-byte.  The automatic paste/drop path transforms all
    # four stock raster suffixes.  Inspect the actual selected local files so
    # a strict task does not needlessly leave the desktop merely because the
    # policy advertises compression for a different file type.
    editor_paths = []
    for item in payload.get('inline_images') or []:
        if isinstance(item, dict):
            value = str(item.get('local_path') or item.get('path') or '').strip()
            if value:
                editor_paths.append(value)
    for value in payload.get('image_paths') or []:
        value = str(value or '').strip()
        if value:
            editor_paths.append(value)
    for item in payload.get('image_replacements') or []:
        if isinstance(item, dict):
            value = str(item.get('local_path') or '').strip()
            if value:
                editor_paths.append(value)
    if editor_mode == 'dialog':
        compressible_suffixes = {'.jpg', '.jpeg'}
    elif editor_mode == 'autoupload':
        compressible_suffixes = {'.jpg', '.jpeg', '.png', '.gif'}
    else:
        compressible_suffixes = set()
    needs_native_canvas = any(
        os.path.splitext(path)[1].lower() in compressible_suffixes
        for path in editor_paths)
    if (payload.get('strict_browser_upload_parity') and editor_policy is not None and
            editor_mode in ('dialog', 'autoupload') and
            bool((getattr(editor_policy, 'metadata', {}) or {}).get('client_compress')) and
            needs_native_canvas):
        raise UploadPolicyError(
            '当前网页启用了浏览器 Canvas 压缩；为保证上传字节与后台网页完全一致，'
            '请使用认证原生网页完成正文图片上传')
    # The native Layui queue limit is runtime policy metadata, not always a
    # DOM attribute on the paired text/file field.  Carry the discovered
    # literal ``number`` value into the worker items before any bytes are
    # sent, so an over-sized desktop selection fails like the browser queue.
    for item in payload.get('media_uploads') or []:
        field = str(item.get('field', '') or '').strip()
        policy = getattr(client, '_upload_policies', {}).get(('field', field))
        metadata = getattr(policy, 'metadata', {}) if policy is not None else {}
        try:
            queue_limit = int((metadata or {}).get('number') or 0)
        except (TypeError, ValueError):
            queue_limit = 0
        if queue_limit > 0:
            item['max_files'] = queue_limit
    ctx.check_cancelled()


def _native_upload_page_hint(client, payload, *, editing=False):
    """Return the authenticated GET page for a native upload fallback.

    ``add_url_hint`` is retained for compatibility with older callers, but it
    historically contained the form's POST action.  A native-browser fallback
    must open the page that owns the uploader; otherwise a POST-only action can
    show an error or lose the current form state.
    """
    candidates = [payload.get("upload_page_url")]
    if editing:
        candidates.extend((payload.get("edit_url_hint"),
                           getattr(client, "_content_edit_native_url", "")))
    else:
        candidates.append(getattr(client, "_content_add_native_url", ""))
    candidates.extend((payload.get("add_url_hint"), payload.get("edit_url_hint")))
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def run_publish(client, payload, ctx):
    """One site-bound publish transaction. Returns a result dict.

    结构与 R20 的 PublishTransactionLoader 一致：图片全部就绪后才提交，
    任一环节失败即中止且不提交，upload_cache 一并回传供「重试失败图片」复用。
    """
    payload = dict(payload, _body_upload_sources=[])
    snapshot_dir = None
    cache = dict(payload.get("upload_cache") or {})
    login_snapshot = payload["_login"]
    admin_url, cookies = login_snapshot[:2]
    headers = login_snapshot[2] if len(login_snapshot) > 2 else payload.get("_headers")
    tmp = build_worker_client(admin_url, cookies, payload.get("_verify", True),
                              headers=headers, network=payload.get("_network"))
    # A browser canvas encoder is not byte-identical to Pillow.  Production
    # publish/edit payloads opt into strict parity and therefore hand pages
    # with UEditor client compression back to the authenticated WebView
    # instead of silently sending a merely semantic approximation.  Legacy
    # direct client/test adapters keep their historical deterministic path.
    tmp._strict_browser_upload_parity = bool(payload.get("strict_browser_upload_parity"))
    ctx.set_byte_budget(_payload_byte_budget(payload))
    total = _total_steps(payload)
    done = 0
    try:
        _restore_payload_declared_mimes(payload)
        if payload.get('thumbnail_from_first'):
            snapshot_dir = tempfile.TemporaryDirectory(prefix='pboot-body-source-')
            payload['_first_source_dir'] = snapshot_dir.name
            payload['_first_local_snapshots'] = {}
        _prepare_upload_policies(tmp, payload, ctx, editing=False)
        fields = dict(payload.get("fields") or {})
        _validate_editor_word_limit(tmp, payload, fields)
        done = _apply_remote_images(tmp, ctx, payload, fields, cache, total, done)
        done, first_url = _apply_images(tmp, ctx, payload, fields, cache, total, done)
        done = _apply_media_assets(tmp, ctx, payload, fields, cache, total, done)
        done = _apply_media_uploads(tmp, ctx, payload, fields, cache, total, done)
        done = _apply_carousel(tmp, ctx, payload, fields, cache, total, done)
        if payload.get("thumbnail_from_first"):
            first_url = _first_content_image(
                fields, payload.get("target_field", "content"), tmp.base_url,
                payload.get("responsive_context"),
                payload.get("browser_first_image"))
        done = _resolve_thumbnail(tmp, ctx, payload, fields, cache,
                                  total, done, first_url)
        ctx.check_cancelled()
        ctx.log(f"提交内容到栏目 {payload.get('scode')}")
        ok, message = tmp.publish_content(
            payload.get("scode"), fields, payload.get("formcheck", ""),
            mcode=payload.get("mcode"),
            known_article_ids=payload.get("known_article_ids"),
            add_url_hint=payload.get("add_url_hint"),
            cancel_callback=ctx.check_cancelled,
            submitter=payload.get("submitter"))
        done += 1
        ctx.progress(done, total, "提交完成")
        native_url = str(getattr(tmp, "_content_add_native_url", "") or "")
        native_reason = str(getattr(tmp, "_content_add_native_reason", "") or "")
        return _with_worker_session(tmp, {"ok": bool(ok), "msg": message,
                "upload_cache": cache, "upload_metadata": list(payload.get('upload_metadata') or []), "fields": fields,
                "native_only": bool(native_url), "native_url": native_url,
                "native_reason": native_reason,
                **dict(getattr(tmp, 'last_write_result', {}) or {})})
    except Cancelled:
        return _with_worker_session(tmp, {"ok": False, "cancelled": True,
                "msg": "任务已取消，内容尚未提交", "upload_cache": cache,
                "upload_metadata": list(payload.get('upload_metadata') or [])})
    except UploadOutcomeUnknown as exc:
        native_url = _native_upload_page_hint(tmp, payload, editing=False)
        return _with_worker_session(tmp, {"ok": False,
                "msg": f"{exc}；内容未提交，文件可能已保存，请先核对后台",
                "upload_cache": cache, "upload_metadata": list(payload.get('upload_metadata') or []),
                "retryable": False, "outcome": "unknown",
                "native_only": bool(native_url), "native_url": native_url,
                "native_reason": "上传结果未知，请在原生网页核对服务器对象"})
    except UploadPolicyError as exc:
        # A page whose UEditor/Layui upload policy is generated by runtime
        # JavaScript cannot be safely mirrored by the non-executing adapter.
        # Keep the exact same-origin add page so the UI can hand the operation
        # to the real browser instead of offering a blind retry or guessing a
        # legacy endpoint.
        native_url = _native_upload_page_hint(tmp, payload, editing=False)
        return _with_worker_session(tmp, {"ok": False,
                "msg": f"{exc}；该页面上传逻辑需要原生网页处理，内容未提交",
                "upload_cache": cache,
                "upload_metadata": list(payload.get('upload_metadata') or []),
                "retryable": False, "native_only": bool(native_url),
                "native_url": native_url})
    except RuntimeError as exc:
        # 图片类失败：明确告知「内容未提交」，并回传缓存供重试
        return _with_worker_session(tmp, {"ok": False,
                "msg": f"{exc}，已中止（内容未提交）", "upload_cache": cache,
                "upload_metadata": list(payload.get('upload_metadata') or []),
                "retryable": True})
    except Exception as exc:
        debug_log(f"[run_publish] 异常: {exc}")
        return _with_worker_session(tmp, {"ok": False, "msg": str(exc),
                "upload_cache": cache, "upload_metadata": list(payload.get('upload_metadata') or [])})
    finally:
        if snapshot_dir:
            snapshot_dir.cleanup()


def run_edit(client, payload, ctx):
    """One site-bound edit transaction. Returns a result dict."""
    payload = dict(payload, _body_upload_sources=[])
    snapshot_dir = None
    cache = dict(payload.get("upload_cache") or {})
    login_snapshot = payload["_login"]
    admin_url, cookies = login_snapshot[:2]
    headers = login_snapshot[2] if len(login_snapshot) > 2 else payload.get("_headers")
    tmp = build_worker_client(admin_url, cookies, payload.get("_verify", True),
                              headers=headers, network=payload.get("_network"))
    tmp._strict_browser_upload_parity = bool(payload.get("strict_browser_upload_parity"))
    ctx.set_byte_budget(_payload_byte_budget(payload))
    total = _total_steps(payload)
    done = 0
    try:
        _restore_payload_declared_mimes(payload)
        if payload.get('thumbnail_from_first'):
            snapshot_dir = tempfile.TemporaryDirectory(prefix='pboot-body-source-')
            payload['_first_source_dir'] = snapshot_dir.name
            payload['_first_local_snapshots'] = {}
        _prepare_upload_policies(tmp, payload, ctx, editing=True)
        fields = dict(payload.get("fields") or {})
        _validate_editor_word_limit(tmp, payload, fields)
        done = _apply_remote_images(tmp, ctx, payload, fields, cache, total, done)
        done, first_url = _apply_images(tmp, ctx, payload, fields, cache, total, done)
        done = _apply_media_assets(tmp, ctx, payload, fields, cache, total, done)
        done = _apply_media_uploads(tmp, ctx, payload, fields, cache, total, done)
        done = _apply_carousel(tmp, ctx, payload, fields, cache, total, done)
        replacements, done = _upload_detail_replacements(
            tmp, ctx, payload, cache, total, done)
        if payload.get('thumbnail_from_first'):
            target = payload.get('target_field') or 'content'
            content = fields.get(target, payload.get('thumbnail_source_content', ''))
            if replacements:
                content, replaced = replace_image_occurrences(content, replacements)
                if replaced != len(replacements):
                    raise RuntimeError('首图来源无法按全部替图计划确认')
            first_url = _first_content_image(
                {target: content}, target, tmp.base_url,
                payload.get("responsive_context"),
                payload.get("browser_first_image"))
        done = _resolve_thumbnail(tmp, ctx, payload, fields, cache, total, done, first_url)
        ctx.check_cancelled()
        ctx.log(f"提交修改 ID={payload.get('article_id')}")
        ok, message = tmp.edit_content(
            payload.get("article_id"), payload.get("mcode"), fields,
            payload.get("formcheck", ""),
            edit_url_hint=payload.get("edit_url_hint"),
            cancel_callback=ctx.check_cancelled,
            expected_fields=payload.get("expected_fields"),
            expected_absent_fields=payload.get("expected_absent_fields"),
            image_replacements=replacements,
            expected_content_hash=payload.get("expected_content_hash", ""),
            refresh_publish_date=bool(payload.get("refresh_publish_date")),
            target_field=payload.get("target_field", "content"),
            submitter=payload.get("submitter"))
        done += 1
        ctx.progress(done, total, "提交完成")
        native_url = str(getattr(tmp, "_content_edit_native_url", "") or "")
        native_reason = str(getattr(tmp, "_content_edit_native_reason", "") or "")
        return _with_worker_session(tmp, {"ok": bool(ok), "msg": message,
                "upload_cache": cache, "upload_metadata": list(payload.get('upload_metadata') or []),
                "native_only": bool(native_url), "native_url": native_url,
                "native_reason": native_reason,
                **dict(getattr(tmp, 'last_write_result', {}) or {})})
    except Cancelled:
        return _with_worker_session(tmp, {"ok": False, "cancelled": True,
                "msg": "任务已取消，文章未改动", "upload_cache": cache,
                "upload_metadata": list(payload.get('upload_metadata') or [])})
    except UploadOutcomeUnknown as exc:
        native_url = _native_upload_page_hint(tmp, payload, editing=True)
        return _with_worker_session(tmp, {"ok": False,
                "msg": f"{exc}；文章未提交修改，文件可能已保存，请先核对后台",
                "upload_cache": cache, "upload_metadata": list(payload.get('upload_metadata') or []),
                "retryable": False, "outcome": "unknown",
                "native_only": bool(native_url), "native_url": native_url,
                "native_reason": "上传结果未知，请在原生网页核对服务器对象"})
    except UploadPolicyError as exc:
        native_url = _native_upload_page_hint(tmp, payload, editing=True)
        return _with_worker_session(tmp, {"ok": False,
                "msg": f"{exc}；该页面上传逻辑需要原生网页处理，文章未提交修改",
                "upload_cache": cache,
                "upload_metadata": list(payload.get('upload_metadata') or []),
                "retryable": False, "native_only": bool(native_url),
                "native_url": native_url})
    except RuntimeError as exc:
        return _with_worker_session(tmp, {"ok": False,
                "msg": f"{exc}，已中止（文章未改动）", "upload_cache": cache,
                "upload_metadata": list(payload.get('upload_metadata') or [])})
    except Exception as exc:
        debug_log(f"[run_edit] 异常: {exc}")
        return _with_worker_session(tmp, {"ok": False, "msg": str(exc),
                "upload_cache": cache, "upload_metadata": list(payload.get('upload_metadata') or [])})
    finally:
        if snapshot_dir:
            snapshot_dir.cleanup()


def run_fetch_categories(client, ctx, retries=3, return_session=False):
    """Load the category tree with retries; returns ``(tree, error)``.

    ``return_session`` is an internal bridge option used by the synchronous
    category API to reconcile worker Cookie rotations/deletions without
    changing the historical two-value return contract for callers/tests.
    """
    admin_url, cookies, headers, network = worker_snapshot(client)
    verify = client.session.verify
    last_error = ""
    last_worker = None

    def finish(tree, error):
        if not return_session:
            return tree, error
        records = _session_cookie_records(last_worker) if last_worker else []
        initial = (list(getattr(last_worker, "_worker_initial_cookie_records", []) or [])
                   if last_worker else [])
        return tree, error, records, initial

    for attempt in range(retries):
        ctx.check_cancelled()
        try:
            tmp = build_worker_client(admin_url, cookies, verify, headers=headers, network=network)
            last_worker = tmp
            tree = tmp.get_category_tree()
            if tree:
                return finish(tree, "")
            # 二开后台可能没有 treetable 的 data-tt-* 结构。
            # 管理树解析失败时，从新增/列表表单的 scode select
            # 降级读取扁平栏目；至少保证可搜索、可发布。
            flat = tmp.get_categories() or []
            if flat:
                ctx.log("栏目管理树结构不兼容，已从内容表单降级读取栏目")
                tree = [{"id": str(item.get("id", "")),
                         "scode": str(item.get("id", "")),
                         "name": str(item.get("name", "")),
                         "children": []}
                        for item in flat if item.get("id")]
                if tree:
                    return finish(tree, "")
            last_error = "栏目管理页与内容表单都未解析到栏目"
            try:
                if hasattr(tmp, "session"):
                    cookies = [copy(cookie) for cookie in tmp.session.cookies]
            except Exception as cookie_exc:
                debug_log(f"[categories] 重试 Cookie 继承失败(可忽略): {cookie_exc}")
        except Exception as exc:
            last_error = str(exc)
            debug_log(f"[categories] 第 {attempt + 1} 次失败: {exc}")
            # A failed read may still have rotated a CSRF/session cookie.
            # Browser retries continue with that live CookieStore; carry the
            # worker's private jar into the next bounded attempt instead of
            # repeatedly replaying the original snapshot.
            try:
                if last_worker is not None and hasattr(last_worker, "session"):
                    cookies = [copy(cookie)
                               for cookie in last_worker.session.cookies]
            except Exception as cookie_exc:
                debug_log(f"[categories] 重试 Cookie 继承失败(可忽略): {cookie_exc}")
    return finish([], last_error)


_PRODUCT_REPAIR_FIELDS = {"front_url", "xinghao", "jiage"}


def _normalize_product_ids(values):
    result = []
    seen = set()
    for value in values or ():
        pid = str(value or "").strip()
        if pid and pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


def _normalize_repair_fields(values):
    aliases = {"model": "xinghao", "price": "jiage",
               "url": "front_url", "link": "front_url"}
    fields = []
    for value in values or ("front_url",):
        key = aliases.get(str(value or "").strip().lower(),
                          str(value or "").strip().lower())
        if key and key not in fields:
            fields.append(key)
    unknown = set(fields) - _PRODUCT_REPAIR_FIELDS
    if unknown:
        raise ValueError("未知产品修复字段: " + ", ".join(sorted(unknown)))
    if not fields:
        fields = ["front_url"]
    return fields


def _copy_cached_detail(remote, cached):
    """Merge a cheap list row with its last successfully read detail values."""
    merged = dict(remote)
    if not cached:
        return merged
    for key in ("xinghao", "jiage", "xinghao_field", "jiage_field"):
        merged[key] = cached.get(key, "")
    if not str(merged.get("front_url", "") or "").strip():
        merged["front_url"] = cached.get("front_url", "")
    if not str(merged.get("mcode", "") or "").strip():
        merged["mcode"] = cached.get("mcode", "")
    return merged


def _list_row_changed(remote, cached):
    """Return whether fields visible on the cheap catalogue list changed."""
    if not cached:
        return True
    for key in ("title", "cat_name", "edit_url", "mcode"):
        if str(remote.get(key, "") or "") != str(cached.get(key, "") or ""):
            return True
    return False


def _product_is_missing(product, fields):
    for field in fields:
        if field == "jiage" and not str(
                product.get("jiage_field", "") or "").strip():
            # This model has no price field, so repeatedly opening the detail
            # form can never repair a price and only wastes minutes.
            continue
        if not str(product.get(field, "") or "").strip():
            return True
    return False


def _fetch_product_detail_groups(tmp, products, target_ids, ctx, max_workers):
    """Fetch selected details grouped by their proven mcode."""
    by_id = {str(item.get("id", "")): item for item in products}
    groups = {}
    for pid in target_ids:
        product = by_id.get(pid)
        if not product:
            continue
        mcode = str(product.get("mcode", "") or "").strip()
        if not mcode:
            raise RuntimeError(f"产品 ID {pid} 缺少 mcode，无法安全读取详情")
        groups.setdefault(mcode, []).append(pid)
    results = {}
    total = sum(len(ids) for ids in groups.values())
    done_offset = 0
    ctx.progress(0, max(total, 1),
                 f"读取产品详情 0/{total}" if total else "无需读取产品详情")
    for mcode, ids in groups.items():
        edit_urls = {pid: by_id[pid].get("edit_url", "") for pid in ids}

        def on_progress(done, _total, pid, offset=done_offset):
            ctx.progress(offset + done, max(total, 1),
                         f"读取详情 {offset + done}/{total}（ID {pid}）")

        details = tmp.fetch_product_details(
            ids, progress_callback=on_progress, max_workers=max_workers,
            edit_urls=edit_urls, mcode=mcode,
            cancel_callback=ctx.check_cancelled)
        if (not isinstance(details, dict)
                or set(map(str, details)) != set(ids)
                or any(not isinstance(value, dict) or not {
                    "xinghao", "jiage", "xinghao_field", "jiage_field"
                }.issubset(value) for value in details.values())):
            raise RuntimeError(
                f"产品详情不完整或身份不匹配（mcode {mcode}）")
        results.update({str(pid): value for pid, value in details.items()})
        done_offset += len(ids)
    return results


def run_product_sync(client, ctx, *, cached_products=None,
                     mode="incremental", product_ids=None, fields=None,
                     max_workers=10, mcode=None, prefer_current_filename=False):
    """Run a cancellable full, incremental, or targeted product sync.

    The return value is a dict suitable for ``db_commit_product_sync``:
    ``products`` + ``complete`` + ``mode`` + ``remote_total`` +
    ``requested`` + ``refreshed``.  No local database is touched here.
    """
    mode = str(mode or "incremental").strip().lower()
    if mode not in ("full", "incremental", "repair"):
        return {"ok": False, "msg": f"未知产品同步模式: {mode}"}
    if fields is None:
        fields = (["front_url"] if mode == "repair"
                  else ["front_url", "xinghao", "jiage"])
    try:
        repair_fields = _normalize_repair_fields(fields)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "mode": mode}
    cached_products = [dict(item) for item in (cached_products or [])]
    cached_by_id = {str(item.get("id", "") or "").strip(): item
                    for item in cached_products}
    explicit_ids = _normalize_product_ids(product_ids)
    explicit_set = set(explicit_ids)

    admin_url, cookies, headers, network = worker_snapshot(client)
    tmp = build_worker_client(admin_url, cookies, client.session.verify,
                              headers=headers, network=network)
    finish = lambda value: _with_worker_session(tmp, value)
    ctx.check_cancelled()
    reasons = {}
    if mode == "repair":
        if not cached_products:
            return finish({"ok": False, "mode": mode,
                           "msg": "本地产品库为空，请先执行增量同步"})
        unknown_ids = [pid for pid in explicit_ids if pid not in cached_by_id]
        if unknown_ids:
            return finish({"ok": False, "mode": mode,
                           "msg": "当前缓存中不存在产品: " + ", ".join(unknown_ids)})
        products = [dict(item) for item in cached_products
                    if (not explicit_set or str(item.get("id")) in explicit_set)
                    and _product_is_missing(item, repair_fields)]
        target_ids = [str(item.get("id")) for item in products]
        for pid in target_ids:
            reasons[pid] = [field for field in repair_fields
                            if not str(cached_by_id[pid].get(field, "") or "").strip()]
        complete = False
        remote_total = None
        ctx.log(f"定向修复 {len(target_ids)} 条产品：{', '.join(repair_fields)}")
    else:
        ctx.progress(0, 1, "读取产品列表…")
        try:
            fetch_kwargs = {"cancel_callback": ctx.check_cancelled}
            if str(mcode or "").strip():
                fetch_kwargs["mcode"] = str(mcode).strip()
            snapshot = tmp.fetch_all_products(**fetch_kwargs)
        except Cancelled:
            raise
        except Exception as exc:
            return finish({"ok": False, "mode": mode,
                           "msg": f"拉取产品列表失败: {exc}"})
        if not snapshot and not (getattr(snapshot, "complete", False)
                                 and getattr(snapshot, "empty_confirmed", False)):
            html = getattr(tmp, "_last_list_html", "")
            if _is_login_page(html):
                return finish({"ok": False, "mode": mode,
                               "msg": "登录已失效或未授权，请重新登录后再拉取"})
            return finish({"ok": False, "mode": mode,
                           "msg": "已加载后台列表页，但未解析到任何产品（可能页面结构变化）"})
        if not getattr(snapshot, "complete", False):
            return finish({"ok": False, "mode": mode,
                           "msg": "产品列表未确认完整，已保留原有缓存不覆盖"})
        products = []
        target_ids = []
        remote_ids = set()
        for remote in snapshot:
            pid = str(remote.get("id", "") or "").strip()
            if pid:
                remote_ids.add(pid)
            cached = cached_by_id.get(pid)
            products.append(_copy_cached_detail(remote, cached))
            row_reasons = []
            if mode == "full":
                row_reasons.append("full")
            else:
                # An explicit ID list is the desktop equivalent of selecting
                # rows in the native page.  It must narrow the detail reads;
                # otherwise a caller asking to refresh one row silently
                # refreshes every row in the catalogue.  The ordinary
                # no-selection incremental path intentionally remains a
                # complete detail refresh so list-invisible model/price
                # changes are not missed.
                if explicit_set and pid not in explicit_set:
                    continue
                # List rows expose no dependable detail revision.  A changed
                # price/model can leave every list field untouched.  Incremental
                # refers to cache writes, never permission to reuse stale detail.
                row_reasons.append("detail_refresh")
                if not cached:
                    row_reasons.append("new")
                elif _list_row_changed(remote, cached):
                    row_reasons.append("list_changed")
                if _product_is_missing(products[-1], repair_fields):
                    row_reasons.append("missing")
                if pid in explicit_set:
                    row_reasons.append("selected")
            if row_reasons:
                target_ids.append(pid)
                reasons[pid] = row_reasons
        if explicit_set and mode != "full":
            missing_requested = [pid for pid in explicit_ids
                                 if pid not in remote_ids]
            if missing_requested:
                return finish({
                    "ok": False, "mode": mode,
                    "msg": "后台列表中不存在所选产品: " +
                           ", ".join(missing_requested),
                    "requested_ids": explicit_ids,
                    "missing_ids": missing_requested,
                })
        complete = True
        remote_total = len(products)
        ctx.log(
            f"列表已获取 {len(products)} 条；{mode} 模式需读取详情 "
            f"{len(target_ids)} 条，复用缓存 {len(products) - len(target_ids)} 条")

    ctx.check_cancelled()
    try:
        details = _fetch_product_detail_groups(
            tmp, products, target_ids, ctx, max_workers)
    except Cancelled:
        raise
    except Exception as exc:
        return finish({"ok": False, "mode": mode,
                       "msg": f"读取产品详情失败: {exc}"})

    by_id = {str(item.get("id", "")): item for item in products}
    missing_scodes = []
    scode_mcodes = {}
    for product in products:
        pid = str(product.get("id", ""))
        detail = details.get(pid)
        if detail:
            product["xinghao"] = detail.get("xinghao", "")
            product["jiage"] = detail.get("jiage", "")
            # Empty means this backend model does not provide the field.
            # Never invent ext_jiage for a site whose product form has no
            # price extension at all.
            product["xinghao_field"] = detail.get("xinghao_field", "")
            product["jiage_field"] = detail.get("jiage_field", "")
            scode = str(detail.get("scode", "") or "").strip()
            if prefer_current_filename and str(detail.get("filename", "") or "").strip():
                # Bulk link repair must prefer the current custom filename over an old numeric list URL.
                product["front_url"] = ""
            if not str(product.get("front_url", "") or "").strip() and scode:
                if scode not in missing_scodes:
                    missing_scodes.append(scode)
                scode_mcodes[scode] = str(detail.get("mcode", "") or
                                          product.get("mcode", "") or "")

    category_paths = {}
    if missing_scodes:
        ctx.progress(len(target_ids), max(len(target_ids) + 1, 1),
                     f"补全前台链接（{len(missing_scodes)} 个栏目）")
        if not hasattr(tmp, "_mcode_cache"):
            tmp._mcode_cache = {}
        for scode, mcode in scode_mcodes.items():
            if mcode:
                tmp._mcode_cache[scode] = mcode
        try:
            ctx.check_cancelled()
            category_kwargs = {"cancel_callback": ctx.check_cancelled}
            if mode == "repair":
                # A health-panel repair should stay responsive.  Cancellation
                # is checked between requests and these shorter per-request
                # budgets bound the one in-flight request it cannot interrupt.
                category_kwargs.update(
                    listing_timeout=15, form_timeout=10,
                    resolve_mcode=False)
            category_paths = tmp.get_category_url_paths(
                missing_scodes, **category_kwargs)
            ctx.check_cancelled()
        except Cancelled:
            raise
        except Exception as exc:
            ctx.log(f"栏目 URL 路径读取失败，将保留现有前台链接: {exc}")

    # A filename/category-derived URL is only a candidate until the same
    # authenticated worker session can read it.  Build all candidates first
    # so verification can use the bounded link checker in one batch instead
    # of persisting a guessed route one row at a time.
    candidate_urls = {}
    for pid, detail in details.items():
        product = by_id.get(pid)
        if not product or str(product.get("front_url", "") or "").strip():
            continue
        candidate = _build_front_url(
            getattr(tmp, "base_url", ""), detail.get("filename", ""), pid,
            category_paths.get(str(detail.get("scode", "") or ""), ""))
        if candidate:
            candidate_urls[pid] = candidate
    verified_urls = {}
    if candidate_urls:
        session = getattr(tmp, "session", None)
        if session is None:
            # Small embedders/test doubles may only expose the parser API;
            # production PbootCMSClient always has a requests session.  Keep
            # their historical behavior without weakening the real worker.
            verified_urls = dict(candidate_urls)
        else:
            ctx.check_cancelled()
            outcomes = probe_links(
                session,
                [{"id": pid, "href": url, "abs": url, "index": pid}
                 for pid, url in candidate_urls.items()],
                timeout=4, max_workers=min(5, len(candidate_urls)),
                total_timeout=max(8, min(30, 4 * len(candidate_urls))),
                cancel_callback=ctx.check_cancelled,
                request_mode="get")
            for item in outcomes:
                if item.get("state") == "ok":
                    verified_urls[str(item.get("id", ""))] = item.get("abs", "")
    repaired_urls = 0
    unresolved = []
    for pid, product in by_id.items():
        candidate = verified_urls.get(str(pid), "")
        if candidate:
            product["front_url"] = candidate
            repaired_urls += 1
        elif pid in candidate_urls:
            unresolved.append(pid)
    final_total = max(len(target_ids) + (1 if missing_scodes else 0), 1)
    ctx.progress(final_total, final_total,
                 f"产品同步完成：读取 {len(details)} 条，补全链接 {repaired_urls} 条")
    return finish({
        "ok": True, "products": products, "complete": complete,
        "mode": mode, "remote_total": remote_total,
        "empty_confirmed": bool(mode != "repair" and
                                getattr(snapshot, "empty_confirmed", False)),
        "requested": len(target_ids), "refreshed": len(details),
        "target_ids": target_ids, "reasons": reasons,
        "repaired_front_url": repaired_urls,
        "unresolved_ids": unresolved,
    })


def run_pull_products(client, ctx, max_workers=10):
    """Backward-compatible full refresh returning ``(products, error)``."""
    result = run_product_sync(
        client, ctx, cached_products=[], mode="full",
        max_workers=max_workers)
    if not result.get("ok"):
        return [], result.get("msg", "产品同步失败")
    return ProductSnapshot(result["products"], complete=True,
                           empty_confirmed=result.get("empty_confirmed", False)), ""


def run_bulk_product_modifications(client, payload, ctx):
    """Apply a prevalidated product edit batch with progress and cancellation.

    Remote writes cannot be rolled back as one transaction, so the result
    always identifies ``succeeded``, ``failed`` and ``not_attempted`` rows.
    The default is fail-fast; callers may explicitly opt into continuing after
    independent row failures.
    """
    items = list((payload or {}).get("items") or [])
    continue_on_error = bool((payload or {}).get("continue_on_error"))
    if not items:
        return {"ok": False, "msg": "没有待修改的产品", "succeeded": [],
                "failed": [], "not_attempted": []}
    admin_url, cookies, headers, network = worker_snapshot(client)
    tmp = build_worker_client(admin_url, cookies, client.session.verify,
                              headers=headers, network=network)
    finish = lambda value: _with_worker_session(tmp, value)
    succeeded = []
    failed = []
    requires_review = False
    total = len(items)
    ctx.progress(0, total, f"批量修改 0/{total}")
    for index, item in enumerate(items):
        try:
            ctx.check_cancelled()
        except Cancelled:
            return finish({
                "ok": False, "cancelled": True,
                "msg": "批量修改已取消；已完成的产品不会重复提交",
                "succeeded": succeeded, "failed": failed,
                "not_attempted": [row.get("id", "") for row in items[index:]],
            })
        pid = str(item.get("id", "") or "").strip()
        try:
            ok, message = tmp.modify_product_fields(
                pid, dict(item.get("changes") or {}),
                xinghao_field=str(item.get("xinghao_field", "") or ""),
                jiage_field=str(item.get("jiage_field", "") or ""),
                mcode=str(item.get("mcode", "") or ""),
                edit_url=str(item.get("edit_url", "") or ""),
                cancel_callback=ctx.check_cancelled,
                expected=dict(item.get("expected") or {}))
        except Cancelled:
            return finish({
                "ok": False, "cancelled": True,
                "msg": "批量修改已取消；已完成的产品不会重复提交",
                "succeeded": succeeded, "failed": failed,
                "not_attempted": [row.get("id", "") for row in items[index:]],
            })
        metadata = dict(getattr(tmp, "last_write_result", {}) or {})
        row = {"id": pid, "msg": message}
        # A product edit can have reached the server while its final response
        # or full-field read-back was lost.  Do not flatten that state into a
        # normal failed row: the desktop queue must block blind retry and show
        # the same durable-review semantics as the single-item editor.
        if metadata.get("requires_review") or str(metadata.get("outcome", "")).lower() in {
                "unknown", "different", "reported_unverified"}:
            requires_review = True
            row.update(metadata)
        if ok:
            succeeded.append({"id": pid,
                              "changes": dict(item.get("changes") or {}),
                              "msg": message, **metadata})
        else:
            failed.append(row)
        ctx.progress(index + 1, total,
                     f"批量修改 {index + 1}/{total}（ID {pid}）")
        if not ok and not continue_on_error:
            return finish({
                "ok": False,
                "msg": f"产品 {pid} 修改失败，已停止后续提交：{message}",
                "succeeded": succeeded, "failed": failed,
                "not_attempted": [row.get("id", "")
                                  for row in items[index + 1:]],
                "requires_review": requires_review,
            })
    return finish({
        "ok": not failed,
        "msg": (f"已修改 {len(succeeded)} 条产品" if not failed else
                f"已修改 {len(succeeded)} 条，失败 {len(failed)} 条"),
        "succeeded": succeeded, "failed": failed, "not_attempted": [],
        "requires_review": requires_review,
    })


def run_backend_diagnostic(client, _payload, ctx):
    """Run a read-only backend scan in an isolated authenticated session."""
    login = _payload["_login"]
    admin_url, cookies = login[:2]
    headers = login[2] if isinstance(login, (tuple, list)) and len(login) > 2 else _payload.get('_headers')
    network = _payload.get('_network')
    tmp = build_worker_client(admin_url, cookies, _payload.get("_verify", True),
                              headers=headers, network=network)
    finish = lambda value: _with_worker_session(tmp, value)
    try:
        report = diagnose_backend(tmp, ctx)
        path = save_report(report)
        return finish({"ok": True, "path": path,
                       "family": report.get("structure_family", ""),
                       "model_count": len(report.get("models") or []),
                       "category_count": (report.get("categories") or {}).get("count", 0),
                       "errors": report.get("errors") or []})
    except Cancelled:
        return finish({"ok": False, "cancelled": True, "msg": "结构诊断已取消"})
    except PermissionError:
        return finish({"ok": False, "msg": "登录会话已失效，请重新登录后再运行结构诊断"})
    except Exception as exc:
        debug_log(f"[backend_diagnostic] {type(exc).__name__}: {exc}")
        return finish({"ok": False, "msg": f"结构诊断失败：{type(exc).__name__}: {exc}"})


def _build_front_url(base_url, filename, pid, category_path=""):
    """拼产品前台 URL。

    PbootCMS 产品页前台地址优先用后台 filename（URL 名），
    形如 https://站/{栏目URL名}/{filename或id}.html。
    【关键】base_url 可能是完整后台入口（如 https://站/admin.php），
    必须只取站点根 scheme+host，否则会拼成 admin.php/xxx.html。
    只用于“提交给用户审核的建议链接”，不保证 100% 命中；
    用户可在面板里改，发布前还会对它做存活性探测。栏目路径未知时
    返回空字符串，绝不猜成站点根目录 URL。
    """
    from urllib.parse import quote, urlparse
    p = urlparse((base_url or "").strip())
    if (p.scheme.lower() not in ("http", "https") or not p.netloc or
            p.username is not None or p.password is not None):
        return ""
    root = f"{p.scheme}://{p.netloc}"
    def safe_path(value):
        value = str(value or "").replace("\\", "/").strip().strip("/")
        if (not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or
                any(token in value for token in ("?", "#", ";", "%", "//"))):
            return ""
        parts = value.split("/")
        if any(part in ("", ".", "..") or ":" in part for part in parts):
            return ""
        # Keep the path structure but quote each segment.  This avoids turning
        # spaces/Unicode into a different route while rejecting traversal and
        # query injection; the server still decides whether the route exists.
        return "/".join(quote(part, safe="-._~!$&'()*+,=@") for part in parts)

    slug = safe_path(filename)
    cat = safe_path(category_path)
    leaf = slug or safe_path(pid)
    if not leaf:
        return ""
    # 栏目路径读取失败时不能猜成站点根目录 /145.html。只有后台明确给出
    # 栏目路径，或 filename 自身已经包含完整相对路径时才生成候选 URL。
    if not cat and "/" not in slug:
        return ""
    if not slug and not str(pid or "").strip().isdigit():
        return ""
    leaf_path = leaf if leaf.lower().endswith(".html") else leaf + ".html"
    path = "/".join(x for x in (cat, leaf_path) if x)
    return f"{root}/{path}"
