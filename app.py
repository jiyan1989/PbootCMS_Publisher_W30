# -*- coding: utf-8 -*-
"""PbootCMS 发文助手 W 版 —— pywebview 入口 + Api 桥接层（多站点标签架构）。

界面在 webui/（纯静态 HTML/CSS/JS，无框架无 CDN，离线可用）。

多标签架构（v3.1 起）：
  - 每个后台地址是一个独立标签（Chrome 风），拥有独立的 SiteSession：
    独立 PbootCMSClient（独立 requests.Session → cookie 天然隔离）、独立登录态、
    独立解析/映射/产品缓存、独立任务锁。因此可**同时登录多个后台并并发操作**，
    互不影响。
  - Api 是薄路由层，持有 {tab_id: SiteSession}，每个方法首参 tab_id 定位会话。
  - 进度/日志/完成事件都带 tab_id，前端据此更新对应标签。

与 R20 的区分见 app_meta.py：渠道 W、版本 3.x、独立目录 → 配置/会话/
产品库完全隔离，两版可同时运行互不干扰。
"""
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import tempfile
import traceback
import time
import webbrowser
from copy import deepcopy
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, unquote_plus

import requests

# Keep the data/transport layer importable in a plain Python environment.
# The packaged desktop build always includes pywebview, but source-level
# parity tests and read-only diagnostics do not need to create a native
# window.  A lazy failure at the actual window/file-dialog boundary is safer
# than making every non-UI test depend on the GUI runtime being installed.
try:
    import webview
except ModuleNotFoundError as _webview_import_error:  # pragma: no cover - environment dependent
    _WEBVIEW_IMPORT_ERROR = _webview_import_error

    class _WebViewUnavailable:
        OPEN_DIALOG = 0
        FOLDER_DIALOG = 1
        SAVE_DIALOG = 2

        def __getattr__(self, name):
            raise RuntimeError(
                "当前 Python 环境未安装 pywebview；请使用打包版或安装 requirements.txt"
            ) from _WEBVIEW_IMPORT_ERROR

    webview = _WebViewUnavailable()

import webtasks as T
from audit_log import export_audit_csv, list_audit, record_audit
import operation_journal
from app_meta import APP_DISPLAY_VERSION, BUILD_DATE, WINDOW_TITLE
from config import ConfigManager
from draft_store import DraftStore, DraftStoreError
from field_mapping import (choose_best_field, mapping_scope, sanitize_publish_fields,
                           save_mapping_scope, validate_mapping)
from html_images import (describe_html_images, scan_html_images,
                         scan_html_remote_images, validate_image_dimension,
                         normalize_responsive_context)
from html_media import scan_html_media
from http_transport import permitted_transition, request_with_redirects
from link_check import (scan_content_links, probe_links, strip_links,
                        apply_link_actions, match_products_by_model)
from logger import debug_log, export_diagnostics
from pboot_client import PbootCMSClient
from admin_modules import NativeModuleFallback
from upload_policy import NativeUploadRequired
from secure_store import load_private_json
from product_repository import (db_clear_site, db_commit_product_sync, db_count,
                                db_filter_products, db_load_products,
                                db_patch_products, db_product_health,
                                db_upsert_products)
from seo_parser import SEOHTMLParser
from form_controls import (normalize_control_value, text_value,
                           upload_field_is_image, validate_control_value)
from asset_types import sniff_mime, sniff_extension, mime_for_extension, image_dimensions
from file_metadata import remember_declared_mime, declared_mime
from update_service import UpdateService
from thumbnail_intent import thumbnail_options
from gallery_plan import gallery_options
from exceptions import Cancelled

_window = None
_native_windows = {}
_native_windows_lock = threading.RLock()


def _close_native_windows_for_site(site_key):
    """Close authenticated child WebViews before a site session is cleared.

    A native browser child can otherwise keep rendering (and sometimes keep
    submitting) with cookies that the requests client has already deleted.
    Closing only children tagged with this site leaves other site tabs alone.
    """
    key = str(site_key or "")
    if not key:
        return 0
    with _native_windows_lock:
        entries = [(uid, item) for uid, item in list(_native_windows.items())
                   if isinstance(item, dict) and str(item.get("site_key", "")) == key]
        for uid, _item in entries:
            _native_windows.pop(uid, None)
    closed = 0
    for _uid, item in entries:
        stop = item.get("cookie_poll_stop") if isinstance(item, dict) else None
        if hasattr(stop, "set"):
            stop.set()
        window = item.get("window") if isinstance(item, dict) else None
        try:
            close = getattr(window, "destroy", None) or getattr(window, "close", None)
            if callable(close):
                close()
                closed += 1
        except Exception as exc:
            debug_log(f"[原生网页会话] 关闭登出站点窗口失败: {exc}")
    return closed


def _browser_cookie_pairs(session, target_url):
    """Return safe, non-HttpOnly cookies that can be handed to WebView JS.

    pywebview does not expose a portable cookie-import API.  Copy only cookies
    whose domain/path/secure attributes match the target URL and whose values
    can be represented by document.cookie. HttpOnly cookies are deliberately
    omitted: they cannot be set from page JavaScript and must remain protected
    in the requests session.
    """
    parsed = urlparse(str(target_url or ""))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return []
    host = parsed.hostname.lower().rstrip(".")
    path = parsed.path or "/"
    now = time.time()
    result = []
    for cookie in session.cookies:
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        if not name or any(ch in name + value for ch in ("\r", "\n", ";")):
            continue
        expires = getattr(cookie, "expires", None)
        if expires not in (None, ""):
            try:
                if float(expires) <= now:
                    continue
            except (TypeError, ValueError):
                continue
        raw_domain = str(getattr(cookie, "domain", "") or "").strip().lower()
        domain = raw_domain.lstrip(".").rstrip(".")
        if domain and host != domain and not host.endswith("." + domain):
            continue
        cookie_path = str(getattr(cookie, "path", "") or "/")
        if not cookie_path.startswith("/"):
            continue
        if (cookie_path != "/" and path != cookie_path and
                not path.startswith(cookie_path.rstrip("/") + "/")):
            continue
        if bool(getattr(cookie, "secure", False)) and parsed.scheme.lower() != "https":
            continue
        rest = getattr(cookie, "_rest", {}) or {}
        if any(str(key).lower() == "httponly" and
               value not in (None, "", False, 0)
               for key, value in rest.items()):
            continue
        same_site = ""
        for key, item in rest.items():
            if str(key).lower() == "samesite" and item not in (None, "", False):
                same_site = str(item)
                break
        # Preserve the browser's Domain/host-only distinction.  Omitting a
        # domain attribute creates a host-only cookie; copying a requests
        # cookie that was explicitly scoped to ``.example.test`` without it
        # silently loses SSO/tenant cookies needed by sibling subdomains.
        domain_attr = ""
        if raw_domain:
            domain_attr = ("." if bool(getattr(cookie, "domain_initial_dot", False))
                           else "") + domain
        result.append({"name": name, "value": value, "path": cookie_path,
                       "domain": domain_attr,
                       "secure": bool(getattr(cookie, "secure", False)),
                       # A JavaScript cookie setter can retain expiry and
                       # SameSite.  Keeping these attributes avoids turning a
                       # persistent browser cookie into a session cookie when
                       # the authenticated child WebView is first opened.
                       "expires": (float(expires) if expires not in (None, "")
                                   else None),
                       "sameSite": same_site})
    return result


def _browser_cookie_script(pairs):
    """Build a one-shot script that imports cookies and reloads the page."""
    payload = json.dumps(list(pairs or []), ensure_ascii=False,
                         separators=(",", ":"))
    return (
        "(function(){const p=" + payload + ";"
        "for(const c of p){let s=c.name+'='+c.value+'; path='+(c.path||'/');"
        "if(c.domain)s+='; domain='+c.domain;"
        "if(c.expires)s+='; expires='+new Date(c.expires*1000).toUTCString();"
        "if(c.sameSite)s+='; SameSite='+c.sameSite;"
        "if(c.secure)s+='; secure';document.cookie=s;}"
        "if(p.length)location.reload();})();"
    )


_NATIVE_HANDOFF_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\[\]-]{0,159}$")
_NATIVE_HANDOFF_SECRET_RE = re.compile(
    r"(?:pass(?:word)?|pwd|token|secret|cookie|csrf|captcha|checkcode|formcheck|nonce|auth|"
    r"api[_-]?key|signature|sign(?:ature)?|session(?:id)?|credential|private|bearer|jwt|salt)",
    re.I,
)


def _sanitize_native_handoff(value):
    """Keep a small, non-secret draft snapshot for an authenticated WebView.

    Native fallback pages are the byte/JavaScript-faithful path, but opening a
    blank form after a desktop preflight forces users to retype every ordinary
    field.  This adapter deliberately accepts only scalar/list form values and
    display-only notes.  It drops credentials, tokens, file paths and unknown
    objects, applies bounded sizes, and never represents a submit action.
    """
    if not isinstance(value, dict):
        return {}
    fields = []
    seen = set()
    for item in value.get("fields", []) if isinstance(value.get("fields"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if (not name or not _NATIVE_HANDOFF_NAME_RE.fullmatch(name) or
                _NATIVE_HANDOFF_SECRET_RE.search(name) or name.lower() in {"password", "passwd"}):
            continue
        if name in seen:
            continue
        raw = item.get("value")
        if isinstance(raw, (str, int, float, bool)):
            out_value = str(raw) if not isinstance(raw, bool) else bool(raw)
        elif isinstance(raw, list):
            values = []
            for part in raw[:50]:
                if isinstance(part, (str, int, float, bool)):
                    values.append(str(part) if not isinstance(part, bool) else bool(part))
            out_value = values
        else:
            continue
        if isinstance(out_value, str) and len(out_value) > 65536:
            out_value = out_value[:65536]
        if isinstance(out_value, list):
            out_value = [part[:65536] if isinstance(part, str) else part
                         for part in out_value]
        fields.append({"name": name, "value": out_value})
        seen.add(name)
        if len(fields) >= 300:
            break
    notes = []
    raw_notes = value.get("notes", [])
    if isinstance(raw_notes, (str, int, float)):
        raw_notes = [raw_notes]
    if isinstance(raw_notes, list):
        for note in raw_notes[:30]:
            if isinstance(note, (str, int, float)):
                text = str(note).strip()
                if text:
                    notes.append(text[:500])
    result = {"version": 1, "workflow": str(value.get("workflow", "") or "")[:40],
              "fields": fields, "notes": notes}
    if value.get("message"):
        result["message"] = str(value.get("message"))[:500]
    return result if fields or notes or result.get("message") else {}


def _native_handoff_script(value):
    """Build a bounded script that fills ordinary controls without submitting.

    It intentionally ignores file/password/hidden/submit controls.  A few
    delayed attempts cover forms rendered after the initial page load, while
    the small banner makes the remaining file/editor work explicit to users.
    """
    handoff = _sanitize_native_handoff(value)
    if not handoff:
        return ""
    payload = json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
    return (
        "(function(p){if(!p||window.__pbootNativeHandoff)return;"
        "window.__pbootNativeHandoff=true;"
        "const apply=()=>{try{const root=document.querySelector('form')||document;"
        "const all=Array.from(root.querySelectorAll('input,textarea,select'));"
        "const by=new Map();all.forEach(e=>{[e.name,e.id].filter(Boolean).forEach(k=>{"
        "if(!by.has(k))by.set(k,[]);by.get(k).push(e);});});"
        "const fire=e=>{try{e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));}catch(_){}};"
        "(p.fields||[]).forEach(item=>{const xs=by.get(item.name)||[];"
        "if(!xs.length)return;const vals=Array.isArray(item.value)?item.value:[item.value];"
        "xs.forEach((e,i)=>{const t=String(e.type||'').toLowerCase();"
        "if(['file','password','hidden','submit','button','reset','image'].includes(t))return;"
        "const v=vals.length>1?(vals[i]??''):vals[0];"
        "if(t==='checkbox'||t==='radio'){e.checked=vals.map(String).includes(String(e.value))||"
        "(vals.length===1&&(v===true||String(v)==='true'));fire(e);return;}"
        "if(e.tagName==='SELECT'){const o=Array.from(e.options||[]);"
        "o.forEach(opt=>{opt.selected=vals.map(String).includes(String(opt.value));});}"
        "else e.value=(v==null?'':String(v));fire(e);});});"
        "if(!document.querySelector('[data-pboot-native-handoff]')){const box=document.createElement('div');"
        "box.setAttribute('data-pboot-native-handoff','1');box.style.cssText='position:fixed;z-index:2147483647;"
        "top:8px;right:8px;max-width:420px;padding:10px 14px;background:#fff7d6;color:#4b3b00;"
        "border:1px solid #d8b44c;border-radius:6px;font:14px/1.45 sans-serif;box-shadow:0 2px 10px #0003';"
        "const title=document.createElement('div');title.textContent='软件已带入当前草稿字段（未自动提交）';"
        "title.style.fontWeight='600';box.appendChild(title);"
        "(p.message?[p.message]:[]).concat(p.notes||[]).forEach(n=>{const d=document.createElement('div');"
        "d.textContent=String(n);box.appendChild(d);});document.body.appendChild(box);}"
        "}catch(_){}};apply();[250,800,1800,3500].forEach(ms=>setTimeout(apply,ms));"
        "})(" + payload + ");"
    )


def _browser_cookie_default_path(target_url):
    """Return the browser default-path approximation for a page URL.

    ``document.cookie`` exposes only name/value pairs, so a cookie changed by
    page JavaScript no longer carries its original Path attribute.  The HTML
    cookie algorithm defaults a path to the current URL's directory; using the
    same narrow path here prevents a page under ``/admin/sub/`` from silently
    broadening a refreshed token to the whole host.
    """
    try:
        path = str(urlparse(str(target_url or "")).path or "/")
    except Exception:
        path = "/"
    if not path.startswith("/"):
        return "/"
    if path.count("/") <= 1:
        return "/"
    directory = path.rsplit("/", 1)[0]
    return directory or "/"


def _merge_browser_cookie_string(session, target_url, cookie_string):
    """Merge non-HttpOnly ``document.cookie`` values into requests safely.

    The embedded browser can update CSRF/session cookies while running page
    JavaScript.  Only cookies visible to the exact target origin are accepted;
    malformed names/values, control characters and cross-origin targets are
    rejected.  Existing HttpOnly cookies are never replaced from JavaScript.
    A visible non-HttpOnly cookie that is absent from the returned
    ``document.cookie`` string is removed from the matching domain/path scope;
    this mirrors an expired cookie without touching HttpOnly cookies or
    cookies hidden by a different path.
    """
    parsed = urlparse(str(target_url or ""))
    if (parsed.scheme.lower() not in ("http", "https") or
            not parsed.hostname or not isinstance(cookie_string, str)):
        return 0
    host = parsed.hostname.lower().rstrip(".")
    path = _browser_cookie_default_path(target_url)
    updated = 0
    try:
        from requests.cookies import create_cookie
        visible_names = set()
        parsed_items = []
        for item in cookie_string.split(";"):
            item = str(item or "").strip()
            if not item or "=" not in item:
                continue
            name, value = item.split("=", 1)
            name, value = name.strip(), value.strip()
            if (not name or any(ch in name + value for ch in ("\r", "\n", ";")) or
                    "=" in name or any(ord(ch) < 0x20 for ch in name + value)):
                continue
            visible_names.add(name)
            parsed_items.append((name, value))

        def _matches_visible_scope(existing):
            domain = str(getattr(existing, "domain", "") or "").lower().lstrip(".").rstrip(".")
            cookie_path = str(getattr(existing, "path", "") or "/")
            rest = getattr(existing, "_rest", {}) or {}
            if any(str(key).lower() == "httponly" and value not in (None, "", False, 0)
                   for key, value in rest.items()):
                return False
            if domain and host != domain and not host.endswith("." + domain):
                return False
            if not cookie_path.startswith("/"):
                return False
            if (cookie_path != "/" and path != cookie_path and
                    not path.startswith(cookie_path.rstrip("/") + "/")):
                return False
            return True

        # ``document.cookie`` only exposes cookies visible to this page.  Do
        # not clear a same-name cookie from another path or any HttpOnly entry.
        stale = []
        for existing in list(session.cookies):
            name = str(getattr(existing, "name", "") or "")
            if name and _matches_visible_scope(existing) and name not in visible_names:
                stale.append((str(getattr(existing, "domain", "") or "") or host,
                              str(getattr(existing, "path", "") or "/"), name))
        for domain, cookie_path, name in stale:
            try:
                session.cookies.clear(domain=domain, path=cookie_path, name=name)
                updated += 1
            except (KeyError, ValueError):
                # CookieJar implementations differ on host-only domains;
                # removing by identity keeps the operation bounded and safe.
                for existing in list(session.cookies):
                    if (str(getattr(existing, "name", "") or "") == name and
                            str(getattr(existing, "path", "") or "/") == cookie_path and
                            str(getattr(existing, "domain", "") or "") == domain):
                        try:
                            session.cookies.clear(
                                domain=domain, path=cookie_path, name=name)
                        except Exception:
                            pass
                        break

        for name, value in parsed_items:
            # Preserve the most specific existing non-HttpOnly path for this
            # name when possible.  Setting a replacement with a broader path
            # could leave stale duplicate cookies in the worker session.
            selected_path = path
            selected_domain = host
            selected_secure = False
            selected_expires = None
            selected_rest = {}
            for existing in session.cookies:
                if str(getattr(existing, "name", "")) != name:
                    continue
                domain = str(getattr(existing, "domain", "") or "").lower().lstrip(".").rstrip(".")
                cookie_path = str(getattr(existing, "path", "") or "/")
                rest = getattr(existing, "_rest", {}) or {}
                if any(str(key).lower() == "httponly" and value not in (None, "", False, 0)
                       for key, value in rest.items()):
                    continue
                if domain and host != domain and not host.endswith("." + domain):
                    continue
                if cookie_path != "/" and path != cookie_path and not path.startswith(cookie_path.rstrip("/") + "/"):
                    continue
                selected_path = cookie_path
                selected_domain = domain or host
                selected_secure = bool(getattr(existing, "secure", False))
                selected_expires = getattr(existing, "expires", None)
                selected_rest = dict(rest)
                # HttpOnly entries were excluded above; remove a malformed
                # case-insensitive key anyway before creating a JS-visible
                # replacement.
                selected_rest = {
                    key: item for key, item in selected_rest.items()
                    if str(key).lower() != "httponly"
                }
                break
            cookie = create_cookie(name=name, value=value,
                                   domain=selected_domain, path=selected_path,
                                   secure=selected_secure,
                                   expires=selected_expires,
                                   rest=selected_rest)
            # JavaScript cannot set HttpOnly; explicitly carry an empty rest
            # mapping so a stale matching cookie cannot be promoted.
            session.cookies.set_cookie(cookie)
            updated += 1
    except Exception as exc:
        debug_log(f"[原生网页会话] Cookie回写失败: {exc}")
    return updated


def _merge_native_cookie_objects(session, target_url, cookies):
    """Merge cookies returned by pywebview's native CookieStore.

    ``document.cookie`` intentionally cannot expose HttpOnly values.  Native
    pywebview backends, however, return their own trusted cookie objects after
    a page has completed an SSO/CSRF flow.  Copy only records that belong to
    the exact authenticated host, preserve domain/path/Secure/expiry and
    replace the matching requests-cookie identity.  A page cannot use this
    helper directly; it is called only from the native window lifecycle.
    """
    parsed = urlparse(str(target_url or ""))
    if (parsed.scheme.lower() not in ("http", "https") or
            not parsed.hostname or not isinstance(cookies, (list, tuple))):
        return 0
    host = parsed.hostname.lower().rstrip(".")
    now = time.time()
    updated = 0

    def attr(item, *names, default=None):
        if isinstance(item, dict):
            lowered = {str(key).lower(): value for key, value in item.items()}
            for name in names:
                if name in item:
                    return item[name]
                value = lowered.get(str(name).lower())
                if value is not None:
                    return value
            return default
        for name in names:
            value = getattr(item, name, None)
            if value is not None:
                return value
        return default

    def epoch(value):
        if value in (None, "", 0, False):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            from email.utils import parsedate_to_datetime
            parsed_date = parsedate_to_datetime(str(value))
            return parsed_date.timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    try:
        from requests.cookies import create_cookie
        for item in cookies:
            name = str(attr(item, "name", "Name", default="") or "")
            value = str(attr(item, "value", "Value", default="") or "")
            if (not name or any(ord(ch) < 0x20 or ch in "\r\n;" for ch in name + value)
                    or "=" in name):
                continue
            domain = str(attr(item, "domain", "Domain", default="") or "").strip().lower()
            domain = domain.rstrip(".")
            domain_host = domain.lstrip(".")
            if domain_host and host != domain_host and not host.endswith("." + domain_host):
                continue
            cookie_path = str(attr(item, "path", "Path", default="/") or "/")
            if not cookie_path.startswith("/") or any(ord(ch) < 0x20 for ch in cookie_path):
                continue
            secure = bool(attr(item, "secure", "Secure", "is_secure", default=False))
            if secure and parsed.scheme.lower() != "https":
                continue
            expires = epoch(attr(item, "expires", "Expires", default=None))
            rest = attr(item, "_rest", "rest", "Rest", default={}) or {}
            if not isinstance(rest, dict):
                rest = {}
            # Some backends expose these attributes at the top level instead
            # of inside Cookie._rest.  Normalize them for requests.
            http_only = attr(item, "httponly", "httpOnly", "HttpOnly", default=None)
            same_site = attr(item, "samesite", "sameSite", "SameSite", default=None)
            normalized_rest = dict(rest)
            if http_only not in (None, "", False, 0):
                normalized_rest["HttpOnly"] = True
            if same_site not in (None, "", False, 0):
                normalized_rest["SameSite"] = str(same_site)
            effective_domain = domain or host
            domain_flag = attr(item, "domain_initial_dot", "domainInitialDot",
                               default=None)
            domain_initial_dot = (domain.startswith(".") if domain_flag is None
                                  else bool(domain_flag))
            # A CookieStore may expose ``hostOnly`` instead of the requests
            # ``domain_initial_dot`` flag.  ``hostOnly=true`` means the
            # Domain attribute must be omitted, so keep the explicit domain
            # only when the record is actually shareable with subdomains.
            if str(attr(item, "hostOnly", "host_only", default="")).lower() in {
                    "true", "1", "yes"}:
                domain_initial_dot = False
                effective_domain = host
            # An expired native cookie means the browser has removed this
            # identity; mirror that removal rather than resurrecting it.
            try:
                session.cookies.clear(domain=effective_domain,
                                      path=cookie_path, name=name)
            except (KeyError, ValueError):
                for existing in list(session.cookies):
                    if (str(getattr(existing, "name", "") or "") == name and
                            str(getattr(existing, "domain", "") or "") == effective_domain and
                            str(getattr(existing, "path", "") or "/") == cookie_path):
                        try:
                            session.cookies.clear(domain=effective_domain,
                                                  path=cookie_path, name=name)
                        except Exception:
                            pass
                        break
            if expires is not None and expires <= now:
                updated += 1
                continue
            cookie = create_cookie(name=name, value=value,
                                   domain=effective_domain, path=cookie_path,
                                   secure=secure, expires=expires,
                                   rest=normalized_rest)
            cookie.domain_initial_dot = domain_initial_dot
            session.cookies.set_cookie(cookie)
            updated += 1
    except Exception as exc:
        debug_log(f"[原生网页会话] CookieStore回写失败: {exc}")
    return updated


def _worker_cookie_identity(record):
    """Normalize a cookie object/record to ``(name, domain, path)``."""
    if isinstance(record, dict):
        return (str(record.get("name", "") or ""),
                str(record.get("domain", "") or ""),
                str(record.get("path", "/") or "/"))
    return (str(getattr(record, "name", "") or ""),
            str(getattr(record, "domain", "") or ""),
            str(getattr(record, "path", "/") or "/"))


def _worker_cookie_is_expired(record):
    """Whether a serialized worker cookie is already past its expiry."""
    if not isinstance(record, dict):
        return False
    value = record.get("expires")
    if value in (None, "", 0, "0"):
        return False
    try:
        return float(value) <= time.time()
    except (TypeError, ValueError):
        return False


def _merge_worker_cookie_records(session, worker_records,
                                  initial_records=None):
    """Merge a background worker CookieJar and propagate deletions safely.

    Worker requests use a private cookie-jar copy.  Set-Cookie additions and
    rotations were already merged by the task bridge, but an expired
    Set-Cookie removes a record from the worker jar rather than returning a
    record with an empty value.  Compare the worker's initial and final
    ``name/domain/path`` identities and remove only a matching *unchanged*
    cookie from the owning session.  If the UI/WebView rotated that identity
    while the worker was running, its newer value is preserved instead of
    being deleted by a stale worker completion.
    """
    jar = getattr(session, "cookies", session)
    if not hasattr(jar, "__iter__") or not hasattr(jar, "clear"):
        return 0
    final_records = [item for item in (worker_records or [])
                     if isinstance(item, dict)]
    initial = [item for item in (initial_records or [])
               if isinstance(item, dict)]
    if not initial:
        return 0
    final_ids = {_worker_cookie_identity(item) for item in final_records
                 if not _worker_cookie_is_expired(item)}
    removed = 0
    for before in initial:
        identity = _worker_cookie_identity(before)
        if not identity[0] or identity in final_ids:
            continue
        for existing in list(jar):
            if _worker_cookie_identity(existing) != identity:
                continue
            # Do not erase a cookie that was independently rotated in the
            # visible session while the worker was running.
            if str(getattr(existing, "value", "") or "") != str(
                    before.get("value", "") or ""):
                continue
            domain = str(getattr(existing, "domain", "") or "")
            path = str(getattr(existing, "path", "/") or "/")
            name = str(getattr(existing, "name", "") or "")
            try:
                jar.clear(domain=domain, path=path, name=name)
                removed += 1
            except (KeyError, ValueError):
                # Host-only CookieJar implementations can reject an explicit
                # clear even though iteration exposes the record.  Retry by
                # the exact same identity; never fall back to name-only
                # deletion, which could remove another path/domain cookie.
                for candidate in list(jar):
                    if (_worker_cookie_identity(candidate) == identity and
                            str(getattr(candidate, "value", "") or "") ==
                            str(before.get("value", "") or "")):
                        try:
                            jar.clear(
                                domain=str(getattr(candidate, "domain", "") or ""),
                                path=str(getattr(candidate, "path", "/") or "/"),
                                name=str(getattr(candidate, "name", "") or ""))
                            removed += 1
                        except Exception:
                            pass
                        break
            break
    return removed


def _merge_worker_session_result(session, result):
    """Consume private worker-cookie metadata and update the visible session.

    This helper is shared by content, product, diagnostic and other worker
    paths.  The metadata is deliberately removed before a result is exposed
    to WebUI so cookie values never become part of an event payload or draft.
    """
    result = dict(result or {})
    worker_cookies = result.pop("_session_cookie_records", None)
    worker_initial = result.pop("_session_cookie_initial_records", None)
    if worker_cookies is None:
        return result
    try:
        from requests.cookies import create_cookie
        _merge_worker_cookie_records(session, worker_cookies, worker_initial)
        initial_by_id = {
            _worker_cookie_identity(item): item
            for item in (worker_initial or []) if isinstance(item, dict)
        }
        for record in worker_cookies:
            if _worker_cookie_is_expired(record):
                continue
            identity = _worker_cookie_identity(record)
            baseline = initial_by_id.get(identity)
            if baseline is not None:
                current = next((item for item in session.cookies
                                if _worker_cookie_identity(item) == identity), None)
                if (current is not None and
                        str(getattr(current, "value", "") or "") not in {
                            str(baseline.get("value", "") or ""),
                            str(record.get("value", "") or ""),
                        }):
                    # The visible WebView/session rotated this identity while
                    # the worker was running; do not overwrite that newer
                    # value with a stale worker completion.
                    continue
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
            cookie.domain_initial_dot = bool(
                record.get("domain_initial_dot", False))
            session.cookies.set_cookie(cookie)
    except Exception as exc:
        debug_log(f"[session] worker cookie merge failed: {exc}")
    return result


class _NativeCookieBridge:
    """Small pywebview API exposed only to an authenticated child window.

    The child may navigate through several same-origin admin paths.  The
    browser's ``document.cookie`` visibility (and therefore the default path
    used when a script updates or expires a cookie) is tied to the *current*
    page, not the URL used to create the window.  Keep the original origin as
    the trust boundary, but accept a same-origin current URL for every sync.
    """

    def __init__(self, session, target_url):
        self._session = session
        self._target_url = str(target_url or "")
        self._origin_url = self._target_url

    def sync_cookies(self, cookie_string="", target_url=""):
        candidate = str(target_url or self._target_url or "")
        if target_url and not _same_http_origin(candidate, self._origin_url):
            # A compromised page must not use the JS bridge to write cookies
            # for another host (even if it can call the exposed API object).
            return {"ok": False, "updated": 0,
                    "msg": "Cookie同步地址必须与原生网页同源"}
        if candidate:
            self._target_url = candidate
        return {"ok": True,
                "updated": _merge_browser_cookie_string(
                    self._session, self._target_url, cookie_string)}

    def sync_native_cookies(self, cookies=None, target_url=""):
        """Accept trusted CookieStore records from the native WebView host."""
        candidate = str(target_url or self._target_url or "")
        if target_url and not _same_http_origin(candidate, self._origin_url):
            return {"ok": False, "updated": 0,
                    "msg": "CookieStore同步地址必须与原生网页同源"}
        if candidate:
            self._target_url = candidate
        return {"ok": True,
                "updated": _merge_native_cookie_objects(
                    self._session, self._target_url, cookies or [])}


def _browser_cookie_sync_script():
    """Install a best-effort page-lifecycle CookieStore bridge.

    A page can rotate a non-HttpOnly CSRF/session cookie from an XHR while it
    remains visible.  A loaded/pagehide-only hook misses that rotation, which
    makes the requests worker stale after the user has completed a native
    webpage operation.  Keep a small, idempotent poll in the child WebView;
    it only exposes ``document.cookie`` for the current same-origin page and
    is stopped before navigation.  The bridge itself still enforces the
    origin/path/HttpOnly checks, so this does not widen the trust boundary.
    """
    return (
        "(function(){if(window.__pbootCookieSyncInstalled)return;"
        "window.__pbootCookieSyncInstalled=true;"
        "const f=()=>{try{if(window.pywebview&&"
        "window.pywebview.api&&window.pywebview.api.sync_cookies)"
        "window.pywebview.api.sync_cookies(document.cookie,location.href);"
        "}catch(e){}};"
        "if(window.pywebview)f();"
        "window.addEventListener('pywebviewready',f);"
        "window.addEventListener('pagehide',f);"
        "document.addEventListener('visibilitychange',()=>{"
        "if(document.visibilityState==='hidden')f();});"
        "const timer=window.setInterval(f,2000);"
        "window.addEventListener('beforeunload',()=>window.clearInterval(timer),"
        "{once:true});})();"
    )


def _ok(**kwargs):
    result = {"ok": True}
    result.update(kwargs)
    return result


def _err(msg, **kwargs):
    result = {"ok": False, "msg": str(msg)}
    result.update(kwargs)
    return result


def _same_http_origin(left, right):
    """Compare trusted HTTP origins, allowing only a default-port upgrade.

    The browser and the transport layer both treat a same-host ``http`` to
    ``https`` upgrade on ports 80/443 as the safe canonical redirect.  Native
    WebView fallbacks must use the same rule or a backend opened over HTTP can
    be rejected merely because its discovered page URL is HTTPS.  Downgrades,
    non-default ports, cross-host URLs and malformed URLs remain disallowed.
    """
    try:
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _resolve_native_page_url(client, candidate, fallback_route=""):
    """Resolve a server-discovered native page before handing it to WebView.

    List pages commonly expose relative ``?p=/...`` or ``/admin.php/...``
    links.  The authenticated WebView bridge accepts absolute same-origin
    URLs only, so normalize those spellings here while retaining the exact
    server link whenever it is already absolute.  This helper never turns a
    missing URL into a guessed route unless the caller explicitly supplies a
    fallback route, and it does not authorize a cross-origin destination.
    """
    value = str(candidate or "").strip()
    base = str(getattr(client, "base_url", "") or "").strip()
    admin = str(getattr(client, "admin_url", "") or "").strip()
    if not value:
        fallback = str(fallback_route or "").strip()
        if fallback:
            # PbootCMS's backend route is carried by the admin entry's ``p``
            # query parameter; joining it to ``base_url`` would incorrectly
            # produce ``https://host/Content/mod/...``.  Reuse the client's
            # own route builder when no server-discovered URL is available.
            builder = getattr(client, "_url", None)
            if callable(builder):
                try:
                    value = str(builder(fallback) or "").strip()
                except Exception:
                    value = fallback
            else:
                value = fallback
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return value
    if value.startswith("?") and admin:
        return urljoin(admin, value)
    if base:
        return urljoin(base.rstrip("/") + "/", value)
    return value


def _journal_state(result=None, error=None):
    """Map a bridge/worker result to the durable pending-operation state."""
    result = result if isinstance(result, dict) else {}
    outcome = str(result.get("outcome", "") or "").lower()
    if result.get("requires_review") or outcome in {"unknown", "different", "reported_unverified"}:
        return "review"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("ok"):
        return "success"
    # A thrown exception can mean a request was sent but the response was lost.
    # Callers that have transport metadata pass it through result; without it,
    # keep the journal failed rather than inventing a successful write.
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return "review"
    if error and re.search(r"超时|timeout|连接|connection|断开|disconnect|未收到|no response|lost response|reset", str(error), re.I):
        return "review"
    return "failed"


def _write_exception_result(sess, exc):
    """Preserve client transport metadata when a bridge write raises.

    Category/Slide/Single writes are synchronous bridge calls.  If an upload
    or form request times out after the server may have received it, returning
    only the exception text loses the durable-review signal.  Carry the
    client's structured last result into both the journal and the UI.
    """
    result = {"ok": False, "msg": str(exc)}
    for attr in ("last_write_result", "last_message_result", "last_upload_result"):
        value = getattr(getattr(sess, "client", None), attr, None)
        if isinstance(value, dict):
            result.update(value)
    result["ok"] = False
    result["msg"] = str(exc) or str(result.get("message", "") or "操作未完成")
    if isinstance(exc, Cancelled):
        # A dynamic upload queue may have completed one or more independent
        # XHRs before the user cancelled.  Stop the form write and expose that
        # partial state explicitly; do not label it a clean failure or retry
        # it automatically, because the server-side objects cannot be rolled
        # back by the desktop bridge.
        prior_upload = str(result.get("outcome", "") or "").lower()
        partial = prior_upload in {"confirmed", "success", "verified", "unknown"}
        result.update({"cancelled": True, "outcome": "cancelled",
                       "retryable": False,
                       "requires_review": bool(partial or
                                                result.get("write_attempted"))})
        if partial:
            result["msg"] = ("已取消表单提交；部分上传请求可能已完成，请先核对后台，"
                             "软件不会自动重试")
        else:
            result["msg"] = "已取消，未继续提交表单"
    native_url = str(getattr(exc, "native_url", "") or "")
    if native_url:
        result.update({"native_only": True, "native_url": native_url,
                       "native_reason": str(getattr(exc, "reason", "") or str(exc))})
    state = _journal_state(result, exc)
    if state == "review":
        result["requires_review"] = True
        result.setdefault("outcome", "unknown")
    return result, state


def _public_form_fields(fields, *, truncate_values=False):
    """Expose complete values and HTML constraints; never silently truncate."""
    allowed = {"name","label","type","kind","value","required","readonly",
               "dom_readonly","disabled","multiple","max_files","mappable","widget","help",
               "options","min","max","step","maxlength","minlength","pattern",
               "accept","placeholder","lay-verify","dirname","autocomplete",
               "inputmode","list","size","dirname_direction","dirname_auto",
               "hidden_fallback","form_novalidate",
               "upload_target","maximum_words","edit_category"}
    return [{key: value for key, value in field.items() if key in allowed}
            for field in fields or [] if not field.get('_upload_internal')]


def _public_messages(messages):
    """Do not expose executable backend action URLs to JavaScript."""
    from message_state import message_revision
    allowed = ("id", "name", "email", "phone", "contact", "industry",
               "city", "product", "content", "time", "visitor", "status",
               "extras")
    result = []
    for message in messages or []:
        item = {key: message.get(key, "") for key in allowed}
        item['fields'] = [{key: field.get(key, '') for key in ('label', 'value', 'key')}
                          for field in message.get('fields', [])]
        item['revision'] = message_revision(message)
        item['status_info'] = dict(message.get('status_info', {}) or {})
        item["can_status"] = bool(message.get("status_url"))
        item["can_delete"] = bool(message.get("delete_url"))
        item['can_reply'] = bool(message.get('reply_url'))
        result.append(item)
    return result


_PRODUCT_ADVANCED_EXCLUDES = {
    # Body/gallery/thumbnail remain on the dedicated content editor, but a
    # native file field such as ``enclosure`` is safe to expose here: the
    # same discovered field-upload policy used by publish/edit can upload it
    # before the product form POST.  Do not blanket-exclude every attachment
    # field merely because its conventional name is enclosure.
    "content", "pics", "ico", "picstitle[]", "formcheck",
    "id", "mcode", "scode", "ac",
}


def _advanced_product_fields(fields, current):
    result = []
    for field in fields or []:
        name = str(field.get("name", "") or "")
        if (not name or name in _PRODUCT_ADVANCED_EXCLUDES or
                field.get("_upload_internal") or
                field.get("mappable") is False or field.get("readonly")):
            continue
        item = dict(field)
        item["value"] = current.get(name, field.get("value", ""))
        result.append(item)
    return result


def _product_form_revision(product_id, mcode, fields):
    payload = {
        "id": str(product_id or ""), "mcode": str(mcode or ""),
        "fields": [{"name": str(field.get("name", "")),
                    "type": str(field.get("type", "text")),
                    "value": field.get("value", ""),
                    "required": bool(field.get("required")),
                    "multiple": bool(field.get("multiple")),
                    "max_files": int(field.get("max_files") or 0),
                    "options": field.get("options") or []}
                   for field in fields or []],
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:24]


def _validate_form_overrides(values, form_fields):
    descriptors = {field.get("name"): field for field in form_fields or []}
    errors, result = [], {}
    for name, value in dict(values or {}).items():
        field = descriptors.get(name)
        if not field or field.get("readonly"):
            errors.append(f"后台字段 {name} 不存在或不可修改")
            continue
        # Native file inputs are never successful controls themselves.  Keep
        # the selected local path as an upload intent; the worker will upload
        # it through the page's actual file policy and submit the returned
        # server URL instead of leaking a Windows path to the CMS.
        if str(field.get("type", field.get("kind", ""))).lower() == "file":
            normalized = normalize_control_value(value, field)
            if field.get("multiple") and not isinstance(normalized, list):
                normalized = [normalized] if normalized else []
            if field.get("required") and not normalized and not field.get("form_novalidate"):
                errors.append(f"{field.get('label') or name}不能为空")
            else:
                result[name] = normalized
            continue
        if field.get("mappable") is False:
            errors.append(f"后台字段 {name} 不存在或不可修改")
            continue
        normalized = normalize_control_value(value, field)
        issue = validate_control_value(normalized, field)
        if issue:
            errors.append(f"{field.get('label') or name}{issue}")
        else:
            result[name] = normalized
    return result, errors


def _is_jpeg_file(path):
    """缩略图必须是实际 JPEG；不能只信可伪造的文件扩展名。"""
    try:
        if os.path.splitext(str(path or ""))[1].lower() not in (".jpg", ".jpeg", ".jpe"):
            return False
        with open(path, "rb") as handle:
            return handle.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


def _accepted_file(path, accept):
    """Apply the browser file-input ``accept`` filter before uploading."""
    tokens = [str(item or '').strip().lower().split(';', 1)[0] for item in
              re.split(r'[,;\s]+', str(accept or '')) if str(item or '').strip()]
    if not tokens:
        return True
    if '*/*' in tokens:
        return True
    suffix = os.path.splitext(str(path or ''))[1].lower()
    mime = (mimetypes.guess_type(str(path or ''))[0] or '').lower()
    # ``mimetypes`` is filename-only.  Native browser pickers can still expose
    # a useful File.type for a no-extension object, so inspect a bounded prefix
    # as a safe fallback.  A known filename MIME remains authoritative to keep
    # the browser's extension-based accept behaviour unchanged.
    if not mime:
        try:
            with open(path, 'rb') as handle:
                mime = sniff_mime(handle.read(128 * 1024), path).lower()
        except (OSError, IOError):
            mime = ''
    for token in tokens:
        if token.startswith('.') and suffix == token:
            return True
        if token.endswith('/*') and mime.startswith(token[:-1]):
            return True
        if '/' in token and mime == token:
            return True
    return False


def _path_mime_type(path):
    """Return the browser-equivalent MIME hint for a picked local file.

    Native Windows pickers do not expose ``File.type`` directly.  Prefer a
    MIME captured from a WebView drop, then use the filename and a bounded
    signature probe; never read the whole asset merely to label a picker row.
    """
    value = str(declared_mime(path) or "").split(";", 1)[0].strip().lower()
    if not value:
        value = str(mimetypes.guess_type(str(path or ""))[0] or "").strip().lower()
    if not value:
        try:
            with open(path, "rb") as handle:
                value = str(sniff_mime(handle.read(128 * 1024), path) or "").strip().lower()
        except (OSError, IOError):
            value = ""
    return value


def _looks_like_server_media_value(value):
    """Recognize an existing text+upload value using browser URL semantics.

    Native file inputs still require a real local file.  Stock CMS image
    controls, however, keep an existing relative value such as
    ``static/upload/a.jpg`` in a text input; rejecting that value merely
    because it is not an absolute Windows path makes the desktop path differ
    from the webpage.  Picker results are absolute paths, so accepting safe
    relative URL syntax here does not turn a missing local picker selection
    into a silent upload.
    """
    text = str(value or '').strip()
    if not text or any(ord(ch) < 32 for ch in text):
        return False
    if text.startswith(('http://', 'https://', '//', '/', './', '../')):
        return True
    if re.match(r'^[A-Za-z]:[\\/]', text) or text.startswith('\\\\'):
        return False
    return bool(re.fullmatch(r'[A-Za-z0-9._~%+\-]+(?:/[A-Za-z0-9._~%+\-]+)*(?:\?[A-Za-z0-9._~%+\-=&%]*)?(?:#[A-Za-z0-9._~%+\-]*)?', text))


def _extract_media_uploads(values, form_fields):
    """Validate native file and stock text+upload control intents.

    Browser file controls never submit a local path.  Stock PbootCMS image
    controls often use a text value with a separate upload button; existing
    server URLs remain ordinary values while newly selected local files are
    uploaded before the final content POST.
    """
    descriptors = {str(field.get("name", "")): field
                   for field in (form_fields or []) if field.get("name")}
    uploads, cleaned = [], dict(values or {})
    for name, field in descriptors.items():
        kind = str(field.get("type", field.get("kind", ""))).lower()
        is_native = kind == "file"
        if not is_native and not field.get("upload_target"):
            continue
        raw = cleaned.get(name)
        if raw in (None, "", []):
            continue
        paths = raw if isinstance(raw, (list, tuple)) else [raw]
        server_values, local_paths = [], []
        for value in paths:
            text = str(value or "").strip()
            path = os.path.abspath(text)
            if text and os.path.isfile(path):
                if not _accepted_file(path, field.get('accept', '')):
                    raise ValueError(
                        f"{field.get('label') or name}文件格式不符合网页 accept 限制："
                        f"{field.get('accept')}")
                local_paths.append(path)
                uploads.append({"field": name, "path": path,
                                "multiple": bool(field.get("multiple")),
                                "max_files": int(field.get("max_files") or 0),
                                "accept": str(field.get("accept", "") or ""),
                                "image_upload": upload_field_is_image(field, path)})
            elif not is_native and _looks_like_server_media_value(text):
                server_values.append(value)
            else:
                raise ValueError(f"{field.get('label') or name}选择的文件不存在")
        if is_native:
            cleaned.pop(name, None)
        elif local_paths:
            # A newly selected local file replaces a single text field; for a
            # repeated field retain existing server values and append later.
            if server_values:
                cleaned[name] = server_values if field.get("multiple") else server_values[-1]
            else:
                cleaned.pop(name, None)
    return cleaned, uploads


def _opened_form_values(fields):
    """Visible/default form values at open time, excluding volatile hidden data."""
    return {f['name']: f['value'] for f in fields or []
            if f.get('name') and 'value' in f and not f.get('disabled')
            and f.get('type', f.get('kind')) not in ('hidden', 'file')
            and f['name'] not in ('formcheck', 'scode', 'id', 'mcode', 'ac')}


def _carousel_size(options):
    raw = (options or {}).get("carousel_size")
    if raw is None or raw == 'original':
        return None
    # Client-side crop/resize is intentionally not supported: it changes the
    # multipart bytes compared with the browser's direct uploader.  Keep this
    # validator as a compatibility guard for old integrations and drafts.
    raise ValueError(
        "为保持与后台直接上传一致，软件不在客户端裁切或转码；"
        "请使用原文件上传或打开原生网页设置尺寸")


def _native_carousel_fallback(sess, *, editing=False):
    """Return the exact authenticated add/edit page for legacy crop intents."""
    client = getattr(sess, "client", None)
    if editing:
        candidate = (str(getattr(sess, "edit_url_hint", "") or "").strip() or
                     str(getattr(client, "_content_edit_native_url", "") or "").strip())
        fallback_route = ""
        if not candidate:
            article_id = str(getattr(sess, "edit_loaded_article_id", "") or "").strip()
            mcode = str(getattr(sess, "edit_mcode", "") or "").strip()
            if article_id and mcode and client is not None:
                try:
                    fallback_route = client._url(
                        f"Content/mod/mcode/{mcode}/id/{article_id}")
                except Exception:
                    fallback_route = ""
    else:
        candidate = (str(getattr(sess, "publish_page_url", "") or "").strip() or
                     str(getattr(client, "_content_add_native_url", "") or "").strip())
        fallback_route = ""
        if not candidate and client is not None:
            mcode = str(getattr(sess, "publish_mcode", "") or "").strip()
            scode = str(getattr(sess, "publish_scode", "") or "").strip()
            if mcode:
                try:
                    candidate = client._url(f"Content/add/mcode/{mcode}")
                    if scode:
                        candidate += ("&" if "?" in candidate else "?") + urlencode({"scode": scode})
                except Exception:
                    candidate = ""
    try:
        return _resolve_native_page_url(client, candidate, fallback_route) if client else candidate
    except Exception:
        return candidate


def _same_local_path(left, right):
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(str(left))) == \
            os.path.normcase(os.path.abspath(str(right)))
    except (OSError, ValueError):
        return False


def _guard(func):
    """统一异常包装：任何未捕获异常都转成 {ok:false}，避免前端卡死。"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Cancelled:
            # Synchronous bridge calls use ``_journal_call`` and may be
            # cancelled from a second WebView invocation.  Preserve the
            # explicit cancellation outcome instead of exposing a generic
            # "<method> 失败" error to the browser UI.
            owner = args[0] if args else None
            sess = None
            try:
                tab_id = args[1] if len(args) > 1 else kwargs.get("tab_id", "")
                sess = owner._session(tab_id) if hasattr(owner, "_session") else None
            except Exception:
                sess = None
            metadata = {}
            client = getattr(sess, "client", None)
            for attr in ("last_write_result", "last_message_result", "last_upload_result"):
                value = getattr(client, attr, None)
                if isinstance(value, dict):
                    metadata.update(value)
            payload = dict(metadata)
            payload.update({
                "cancelled": True, "outcome": "cancelled", "retryable": False,
                "requires_review": bool(metadata.get("write_attempted")),
            })
            return _err("操作已取消；如已有请求发出，请先核对后台，勿直接重试",
                        **payload)
        except Exception as exc:
            debug_log(f"[api] {func.__name__} 异常: {exc}\n{traceback.format_exc()[:600]}")
            return _err(f"{func.__name__} 失败: {exc}")
    wrapper.__name__ = func.__name__
    return wrapper


def _push(event, payload):
    """向前端推送事件（进度/日志/完成）。payload 内应带 tab_id 以便前端路由。"""
    if _window is None:
        return
    try:
        data = json.dumps({"event": event, "data": payload}, ensure_ascii=False)
        _window.evaluate_js(f"window.onPyEvent && window.onPyEvent({data})")
    except Exception:
        pass


def _open_clipboard(u32, tries=6, delay=0.05):
    """打开剪贴板（带重试）。

    剪贴板是全局独占资源，经常被其他程序（输入法、截图工具、Office）
    瞬时占用，单次 OpenClipboard 失败很常见，重试几次即可。
    """
    import time
    for _ in range(max(1, tries)):
        if u32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def _now_stamp():
    """PbootCMS 内容表 date 字段的时间格式。"""
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _content_hash(value):
    """Hash the exact stored article body for optimistic edit protection."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sanitize_admin_url(url):
    """清洗手输/粘贴的后台地址。

    粘贴出错时常见“同一地址被拼两遍”：
        https://a.com/x.phphttps://a.com/x.php
    直接请求就是 404。这里只取第一个完整 URL，并清掉首尾空白/引号。
    """
    s = str(url or "").strip().strip('"\'')
    # Do not normalize control characters away before validation.  A pasted
    # newline/tab can otherwise be silently joined into a different trusted
    # host/path, unlike the browser URL parser used by the native page.
    if "\\" in s or any(ord(char) < 33 for char in s):
        return ""
    s = "".join(s.split())          # 去掉内部空白（含换行/制表符）
    if not s:
        return ""
    # 第二个 scheme 出现位置之后全部丢弃
    low = s.lower()
    second = -1
    for scheme in ("http://", "https://"):
        pos = low.find(scheme, 1)
        if pos > 0 and (second < 0 or pos < second):
            second = pos
    if second > 0:
        s = s[:second]
        debug_log(f"[url] 检测到重复粘贴的地址，已截取为: {s}")
    from urllib.parse import urlparse
    raw_parsed = urlparse(s)
    # Do not turn an arbitrary scheme such as ``javascript:`` or
    # ``file:`` into an apparently valid HTTPS hostname by blindly prefixing
    # ``https://`` below.  Only schemeless host input or explicit HTTP(S) is
    # accepted at this boundary.
    if raw_parsed.scheme and raw_parsed.scheme.lower() not in ("http", "https"):
        # ``example.test:8443/admin.php`` is common schemeless input; its
        # colon is a port separator rather than a URL scheme.  Permit only
        # that strict host/decimal-port shape, never arbitrary ``scheme:``.
        if not re.fullmatch(r"[A-Za-z0-9.-]+:\d+(?:[/?#].*)?", s):
            return ""
    parsed = urlparse(s if "://" in s else "https://" + s)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    if (parsed.username is not None or parsed.password is not None or
            "\\" in s or any(ord(char) < 33 for char in s)):
        return ""
    return s


def _win_clip_api():
    """声明剪贴板相关 Win32 函数原型。

    句柄在 64 位下是 8 字节指针，**必须**显式声明 argtypes，
    否则 ctypes 默认按 32 位 int 转换，抛 OverflowError。
    """
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.restype = wintypes.BOOL
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, u32, k32


def _clipboard_get_text():
    """读系统剪贴板文本（Windows，ctypes，无额外依赖）。

    WebView 里 navigator.clipboard.readText() 可能因权限/非安全上下文被拒，
    此时前端回退到本函数，保证 Ctrl+V 一定可用。
    """
    if sys.platform != "win32":
        return ""
    CF_UNICODETEXT = 13
    ctypes, u32, k32 = _win_clip_api()
    if not _open_clipboard(u32):
        debug_log("[clip] 打开剪贴板失败（读）")
        return ""
    try:
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


def _clipboard_set_text(text):
    """写系统剪贴板文本（Windows，ctypes）。"""
    if sys.platform != "win32":
        return False
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    text = str(text or "")
    ctypes, u32, k32 = _win_clip_api()
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    if not _open_clipboard(u32):
        debug_log("[clip] 打开剪贴板失败（写）")
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            k32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            k32.GlobalUnlock(handle)
        if u32.SetClipboardData(CF_UNICODETEXT, handle):
            return True                 # 成功后内存归系统所有，不能再 free
        k32.GlobalFree(handle)          # 失败才由我们释放，否则泄漏
        return False
    finally:
        u32.CloseClipboard()


def _image_mime(raw):
    """按字节魔数识别图片类型；非图片返回空串（验证码可能被站点返回成 HTML 错误页，
    直接塞进 data:image/* 会裂图）。"""
    if not raw or len(raw) < 8:
        return ""
    if raw[:4] == b"\x89PNG":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] in (b"GIF8",):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return ""


class SiteSession(object):
    """单个后台标签的全部状态。每个 SiteSession 有独立 client / 锁 / 缓存，
    彼此零共享，因此多个标签可同时登录并并发操作，互不影响。"""

    def __init__(self, tab_id):
        self.tab_id = tab_id
        self.client = PbootCMSClient()
        # Every real desktop workflow must use the browser's bytes whenever a
        # discovered editor policy enables a client-side Canvas transform.
        # Publish/edit workers set this explicitly on their private client;
        # keeping the owning session strict as well covers synchronous product,
        # category, Slide, single-page and generic-module upload paths.
        self.client._strict_browser_upload_parity = True
        self.login_info = {}
        self._prepared_raw_url = ""   # 上次 prepare_login 时用户输入的原始地址
        self.categories = []
        # Independent website Slide/轮播 records.  These are deliberately
        # separate from article ``pics`` galleries.
        self.slides = []
        self.parsed = {}          # 当前解析出的 HTML 字段
        self.orig_parsed = {}     # 原始快照（重复发布时还原，避免图片累积）
        self.html_path = ""
        self.form_fields = []     # 发布栏目的表单字段
        self.publish_scode = ""
        self.publish_mcode = None
        self.publish_formcheck = ""
        self.publish_action = ""
        # Exact GET page discovered while reading the current add form.  The
        # UI can hand this URL to the authenticated native WebView so sites
        # with runtime UEditor/Layui scripts have a true browser-equivalent
        # publish path instead of a guessed route.
        self.publish_page_url = ""
        self.publish_open_values = None

        # 编辑流程必须与发布流程完全隔离。旧版共用 parsed/form_fields/mcode，
        # 在两个页面交替操作时会让界面显示 A、后台实际提交 B。
        self.edit_parsed = {}
        self.edit_orig_parsed = {}
        self.edit_html_path = ""
        self.edit_form_fields = []
        self.edit_scode = ""
        self.edit_mcode = None
        self.edit_loaded_article_id = ""
        self.edit_context_initialized = False
        self.edit_load_seq = 0
        self.edit_baseline_key = None
        self.edit_baseline_values = {}
        # 以下三个旧名称保留给历史调用/测试；正式编辑流程只读 edit_* 状态。
        self.current_mcode = None
        self.articles = []
        self.products = []
        self.upload_cache = {}
        self.failed_paths = []
        self.retryable = False
        self.last_publish_payload = None
        self.last_publish_title = ""
        self.edit_formcheck = ""
        self.edit_url_hint = ""
        self.current_values = {}
        self._task_ctx = None
        self._journal_op_id = ""
        self._busy = False        # 本标签事务防重入（各标签独立）
        # 标签关闭是永久状态。长请求可能在 close_tab 之后才返回；该标志
        # 保证它不能再启动写任务，也不能把完成事件投递给同 ID 的新标签。
        self._closed = threading.Event()
        self.insecure = False     # 是否已经用户确认忽略 HTTPS 证书错误
        self._lock = threading.Lock()


class Api(object):
    """前端唯一可调用的桥接层；所有方法返回可 JSON 序列化的 dict。

    多标签路由：除少数全局方法（app_info / pick_* / export_diagnostics）外，
    每个方法首参为 tab_id，用于定位对应的 SiteSession。
    """

    def _journal_call(self, sess, action, target, summary, callback):
        """Run one side-effecting client call behind a durable journal marker.

        Synchronous bridge writes (product edits, dynamic module writes and
        message operations) still need the same cooperative cancellation
        contract as background tasks.  Install a short-lived context here so
        ``cancel_task`` can stop the operation before its first request and so
        upload helpers can observe cancellation while a form is being built.
        Existing task contexts are preserved when a caller already owns one.
        """
        own_context = sess._task_ctx is None
        previous_cancel = getattr(sess.client, "_active_cancel_callback", None)
        context = sess._task_ctx
        if own_context:
            context = T.TaskContext()
            sess._task_ctx = context
            sess.client._active_cancel_callback = context.check_cancelled
        def cleanup_context():
            if not own_context:
                return
            if previous_cancel is None:
                try:
                    delattr(sess.client, "_active_cancel_callback")
                except AttributeError:
                    pass
            else:
                sess.client._active_cancel_callback = previous_cancel
            if sess._task_ctx is context:
                sess._task_ctx = None
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            action, target, summary or {})
        try:
            context.check_cancelled()
            result = callback()
        except Exception as exc:
            metadata = {}
            for attr in ("last_write_result", "last_message_result", "last_upload_result"):
                value = getattr(sess.client, attr, None)
                if isinstance(value, dict):
                    metadata.update(value)
            journal_result = dict(metadata)
            journal_result.update({"ok": False, "msg": str(exc)})
            if isinstance(exc, Cancelled):
                attempted = bool(journal_result.get("write_attempted"))
                journal_result.update({
                    "cancelled": True, "outcome": "cancelled",
                    "retryable": False,
                    "requires_review": attempted,
                    "msg": ("操作已取消；请求可能已发送，请先核对后台，勿直接重试"
                             if attempted else "操作已取消，未发送请求"),
                })
            operation_journal.finish(
                journal_id, _journal_state(journal_result, exc), journal_result,
                str(exc))
            cleanup_context()
            raise
        metadata = {}
        for attr in ("last_write_result", "last_message_result", "last_upload_result"):
            value = getattr(sess.client, attr, None)
            if isinstance(value, dict):
                metadata.update(value)
        if isinstance(result, dict):
            journal_result = dict(result)
            journal_result.update(metadata)
        elif isinstance(result, tuple) and result:
            journal_result = {"ok": bool(result[0]),
                              "msg": str(result[1] if len(result) > 1 else "")}
            journal_result.update(metadata)
        else:
            journal_result = dict(metadata)
            journal_result["ok"] = bool(result)
        operation_journal.finish(
            journal_id, _journal_state(journal_result), journal_result,
            str(journal_result.get("msg", "") or ""))
        cleanup_context()
        return result

    def __init__(self):
        self.config = ConfigManager()
        self.draft_store = DraftStore()
        # 初始化和 app_info 均不触网；只有用户显式调用
        # check_for_updates 时才会只读获取一次发布 manifest。
        self._updates = UpdateService()
        self._sessions = {}          # tab_id -> SiteSession
        self._sessions_lock = threading.Lock()

    # ── 会话路由 ──
    def _session(self, tab_id):
        """获取或创建指定标签的会话（首次访问自动建）。"""
        tab_id = str(tab_id or "default")
        with self._sessions_lock:
            sess = self._sessions.get(tab_id)
            if sess is None:
                sess = SiteSession(tab_id)
                self._sessions[tab_id] = sess
                debug_log(f"[tab] 新建标签会话 {tab_id}")
            return sess

    @staticmethod
    def _clear_site_workflow_state(sess):
        """清除只属于当前站点的草稿、表单和重试快照。"""
        sess.categories = []
        sess.slides = []
        sess.form_fields = []
        sess.publish_scode = ""
        sess.publish_mcode = None
        sess.publish_formcheck = ""
        sess.publish_action = ""
        sess.publish_page_url = ""
        sess.publish_open_values = None
        sess.edit_baseline_key = None
        sess.edit_baseline_values = {}
        sess.parsed = {}
        sess.orig_parsed = {}
        sess.html_path = ""
        sess.edit_parsed = {}
        sess.edit_orig_parsed = {}
        sess.edit_html_path = ""
        sess.edit_form_fields = []
        sess.edit_scode = ""
        sess.edit_mcode = None
        sess.edit_loaded_article_id = ""
        sess.edit_context_initialized = False
        sess.edit_load_seq += 1
        sess.current_mcode = None
        sess.articles = []
        sess.current_values = {}
        sess.edit_formcheck = ""
        sess.edit_url_hint = ""
        sess.products = []
        sess.upload_cache = {}
        sess.failed_paths = []
        sess.retryable = False
        sess.last_publish_payload = None
        sess.last_publish_title = ""
        sess.login_info = {}

    def _dialog_directory(self, picker_key):
        """每类选择按钮使用各自上一次成功选择的目录。"""
        dirs = self.config.data.get("file_dialog_dirs", {})
        path = str(dirs.get(str(picker_key or "default"), "") or "")
        return path if os.path.isdir(path) else ""

    def _remember_dialog_path(self, picker_key, selected_path):
        path = os.path.abspath(str(selected_path or ""))
        directory = path if os.path.isdir(path) else os.path.dirname(path)
        if not directory or not os.path.isdir(directory):
            return
        with self.config.locked():
            dirs = self.config.data.setdefault("file_dialog_dirs", {})
            dirs[str(picker_key or "default")] = directory
            self.config.save()

    # ══════════════════════════════════════════════════════════
    #  全局信息（不绑定标签）
    # ══════════════════════════════════════════════════════════
    @_guard
    def app_info(self):
        return _ok(version=APP_DISPLAY_VERSION, build=BUILD_DATE,
                   title=WINDOW_TITLE,
                   urls=self.config.data.get("urls", []),
                   last_url=self.config.data.get("last_url", ""),
                   update=self._updates.update_summary(),
                   signing=self._updates.signing_info())

    @_guard
    def about_info(self):
        """返回版本、更新日志、更新策略与实际签名状态。"""
        return _ok(**self._updates.about())

    @_guard
    def check_for_updates(self):
        """手动只读检查：仅读取 manifest，不下载、不安装。"""
        return _ok(**self._updates.check())

    @_guard
    def create_update_backup(self):
        """显式创建更新前备份：配置 JSON + SQLite 一致性快照。"""
        # 与 ConfigManager.save 共用锁，避免备份到半个 JSON。
        with self.config.locked():
            result = self._updates.create_backup()
        return _ok(**result)

    @_guard
    def clear_all_cache(self, tab_id):
        """Explicitly clear all runtime caches of the current PbootCMS site."""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录当前网站后台")
        with sess._lock:
            if sess._busy:
                return _err("当前站点有任务正在执行，请稍后再清理缓存")
            sess._busy = True
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            "cache_clear", "site", {"route": "Index/clearCache"})
        try:
            try:
                result = sess.client.clear_all_cache()
            except Exception as exc:
                state = _journal_state({}, exc)
                if state == "review":
                    # The request may have reached the server even though the
                    # response was lost.  Leave the durable marker pending so
                    # the next login shows it and never silently repeats the
                    # destructive operation.
                    record_audit(
                        getattr(sess.client, "site_key", ""), "cache_clear",
                        "site", status="review", message=str(exc))
                    return _err(
                        "清理缓存请求结果未知，请先到后台核对；软件不会自动重发",
                        outcome="unknown", requires_review=True,
                        operation_id=journal_id)
                operation_journal.finish(journal_id, "failed", {}, str(exc))
                record_audit(
                    getattr(sess.client, "site_key", ""), "cache_clear",
                    "site", status="failed", message=str(exc))
                raise
            operation_journal.finish(journal_id, "success", result,
                                     str(result.get("msg", "")))
            record_audit(
                getattr(sess.client, "site_key", ""), "cache_clear",
                "site", status="success", message=result.get("msg", ""))
            return _ok(**result)
        finally:
            with sess._lock:
                sess._busy = False

    # ── 可恢复草稿（按站点 + 流程隔离）──
    @staticmethod
    def _draft_site_key(sess):
        return str(getattr(sess.client, "site_key", "") or "").strip()

    @_guard
    def save_draft(self, tab_id, workflow, draft):
        """保存 publish/edit/batch 的最新草稿，不保存密码或 Cookie。"""
        sess = self._session(tab_id)
        site_key = self._draft_site_key(sess)
        if not site_key:
            return _err("请先选择站点，再保存草稿")
        workflow = str(workflow or "").strip().lower()
        if workflow == "publish":
            source_fields = sess.parsed
        elif workflow == "edit":
            source_fields = sess.edit_parsed
        else:
            source_fields = None
        try:
            record = self.draft_store.save(
                site_key, workflow, draft, source_fields=source_fields)
        except DraftStoreError as exc:
            return _err(str(exc))
        warnings = list((record.get("draft") or {}).get("asset_warnings") or [])
        return _ok(record=record, asset_warnings=warnings,
                   msg=("草稿已保存；部分素材未能快照，请核对恢复提示" if warnings
                        else "草稿已保存"))

    @_guard
    def load_draft(self, tab_id, workflow):
        """读取当前站点指定流程草稿；不存在不视为错误。"""
        sess = self._session(tab_id)
        site_key = self._draft_site_key(sess)
        if not site_key:
            return _err("请先选择站点，再恢复草稿")
        try:
            record = self.draft_store.load(site_key, workflow)
        except DraftStoreError as exc:
            return _err(str(exc))
        return _ok(found=record is not None, record=record)

    @_guard
    def list_drafts(self, tab_id, workflow=""):
        """列出当前站点草稿摘要，workflow 留空时列出全部流程。"""
        sess = self._session(tab_id)
        site_key = self._draft_site_key(sess)
        if not site_key:
            return _err("请先选择站点，再查看草稿")
        try:
            drafts = self.draft_store.list(site_key, workflow or None)
        except DraftStoreError as exc:
            return _err(str(exc))
        return _ok(drafts=drafts, count=len(drafts))

    @_guard
    def delete_draft(self, tab_id, workflow):
        """删除当前站点指定流程草稿。"""
        sess = self._session(tab_id)
        site_key = self._draft_site_key(sess)
        if not site_key:
            return _err("请先选择站点，再删除草稿")
        try:
            deleted = self.draft_store.delete(site_key, workflow)
        except DraftStoreError as exc:
            return _err(str(exc))
        return _ok(deleted=deleted)

    @_guard
    def saved_credentials(self, admin_url=""):
        """只读返回指定历史地址的本机加密账密，不发网络请求。"""
        url = _sanitize_admin_url(admin_url)
        saved = self.config.get_credentials(url) if url else {}
        return _ok(user=str(saved.get("user", "")),
                   password=str(saved.get("pass", "")))

    @_guard
    def restorable_sites(self):
        """列出磁盘上“声称已登录”的站点，供启动时自动恢复。

        只读会话文件，**不发网络请求**（真实有效性由前端逐站 prepare_login 验证）。
        返回按地址历史先后排序，最近用过的在前。
        """
        sess_dir = getattr(PbootCMSClient(), "_session_dir", None)
        found = {}
        if sess_dir and os.path.isdir(str(sess_dir)):
            for name in os.listdir(str(sess_dir)):
                if not name.endswith(".json"):
                    continue
                try:
                    data = load_private_json(os.path.join(str(sess_dir), name))
                except Exception as exc:
                    debug_log(f"[restore] 读会话文件失败 {name}: {exc}")
                    continue
                last_url = str(data.get("last_admin_url", "") or "").strip()
                logged = data.get("logged_sites") or {}
                if last_url and logged:
                    found[last_url.rstrip("/")] = name[:-5]
        # 只恢复仍在地址历史中的会话。用户明确删除过的站点即使遗留了
        # 损坏/占用中的会话文件，也不能在下次启动时重新出现。
        history = [u.rstrip("/") for u in self.config.data.get("urls", [])]
        ordered = [u for u in history if u in found]
        sites = [{"admin_url": u,
                  "site_key": found[u],
                  "user": self.config.get_credentials(u).get("user", "")}
                 for u in ordered]
        debug_log(f"[restore] 可尝试恢复的站点 {len(sites)} 个")
        return _ok(sites=sites)

    @_guard
    def forget_site_credentials(self, admin_url=""):
        """忘记指定站点已记住的账密。"""
        self.config.forget_credentials(admin_url)
        return _ok(msg="已忘记该站点的账密")

    @_guard
    def forget_saved_site(self, admin_url=""):
        """从历史中删除地址、账密和对应的持久化登录会话。"""
        url = _sanitize_admin_url(admin_url)
        if not url:
            return _err("请选择要删除的历史地址")
        self.config.forget_site(url)
        removed_sessions = 0
        probe = PbootCMSClient()
        probe.set_admin_url(url)
        session_dir = probe._session_dir
        candidates = {probe._cookie_file}
        normalized = url.rstrip("/").lower()
        if session_dir.is_dir():
            for path in session_dir.glob("*.json"):
                try:
                    saved = load_private_json(path)
                    saved_url = str(saved.get("last_admin_url", "") or "") \
                        .rstrip("/").lower()
                    if saved_url == normalized:
                        candidates.add(path)
                except Exception:
                    continue
        for path in candidates:
            try:
                if path.is_file():
                    path.unlink()
                    removed_sessions += 1
            except OSError as exc:
                debug_log(f"[history] 删除会话文件失败 {path.name}: {exc}")
        return _ok(msg="已删除历史地址及其保存的账号密码",
                   removed_sessions=removed_sessions,
                   urls=list(self.config.data.get("urls", [])),
                   last_url=str(self.config.data.get("last_url", "")))

    @_guard
    def close_tab(self, tab_id):
        """关闭标签：取消其正在进行的任务并释放会话。"""
        tab_id = str(tab_id or "")
        with self._sessions_lock:
            sess = self._sessions.pop(tab_id, None)
            if sess:
                # 必须在仍持有会话表锁时先标记关闭，避免另一个调用拿着旧 sess
                # 在 pop 与 cancel 之间启动新的后台写任务。
                sess._closed.set()
        if sess and sess._task_ctx:
            try:
                sess._task_ctx.cancel()
            except Exception:
                pass
        debug_log(f"[tab] 关闭标签会话 {tab_id}")
        return _ok()

    @_guard
    def get_site_state(self, tab_id):
        sess = self._session(tab_id)
        return _ok(admin_url=sess.client.admin_url,
                   logged_in=bool(sess.client.logged_in),
                   site_key=getattr(sess.client, "site_key", ""),
                   login_ready=bool(sess.login_info),
                   prepared_url=sess._prepared_raw_url,
                   busy=sess._busy)

    # ══════════════════════════════════════════════════════════
    #  站点与登录
    # ══════════════════════════════════════════════════════════
    @_guard
    def prepare_login(self, tab_id, url, force=False, insecure=False,
                      record_history=True, network_mode=None, proxy_url=None):
        """设置后台地址并拉取登录页（返回是否需验证码 + 验证码图 base64）。

        【关键】仅在**真正切换站点**或 force 时才清 cookie。
        每次清 cookie 会换一个全新 PHP 会话，在有 WAF/限流的站点上极易造成
        “图片与会话里的答案错位”，表现为验证码永远错。

        insecure=True 时关闭该标签的 HTTPS 证书校验（仅在用户明确确认后使用，
        用于自有站点证书过期的场景）。
        """
        sess = self._session(tab_id)
        url = _sanitize_admin_url(url)
        if not url:
            return _err("请填写后台地址")
        if sess._busy:
            return _err("有任务正在执行，请稍候")
        old_key = getattr(sess.client, "site_key", "")
        sess.client.set_admin_url(url)
        site_changed = getattr(sess.client, "site_key", "") != old_key

        # Browser sessions may use a system or configured proxy.  Keep the
        # routing policy per backend URL and apply it before the first probe;
        # workers receive the same snapshot later.  Legacy callers which do
        # not pass the new arguments load the saved policy, preserving direct
        # mode for old configurations.
        network_table = self.config.data.setdefault("network_per_site", {})
        network_key = str(sess.client.admin_url or url).rstrip("/")
        saved_network = network_table.get(network_key, {})
        if not isinstance(saved_network, dict):
            saved_network = {}
        explicit_network = network_mode is not None or proxy_url is not None
        selected_mode = (network_mode if network_mode is not None
                         else saved_network.get("mode", "direct"))
        selected_proxy = (proxy_url if proxy_url is not None
                          else saved_network.get("proxy_url", ""))
        try:
            sess.client.configure_network(selected_mode, selected_proxy)
        except (TypeError, ValueError) as exc:
            return _err(f"网络代理设置无效：{exc}")
        if explicit_network:
            network_table[network_key] = {
                "mode": sess.client.network_mode,
                "proxy_url": sess.client.proxy_url,
            }
            try:
                self.config.save()
            except Exception as exc:
                debug_log(f"[network] 保存站点代理设置失败（不影响本次连接）: {exc}")

        if site_changed:
            # 新站点绝不能继承旧站的草稿、重试任务或 SSL 降级状态。
            self._clear_site_workflow_state(sess)
            sess.insecure = False
            sess.client.session.verify = True

        # 证书校验策略：默认校验；用户确认过或配置里记过的站点才降级
        remembered = self.config.data.get("insecure_sites", {}) or {}
        if insecure or remembered.get(getattr(sess.client, "site_key", "")):
            sess.insecure = True
        if sess.insecure:
            sess.client.session.verify = False
            debug_log(f"[ssl] tab={tab_id} 已按用户确认关闭证书校验: {url}")

        if force:
            sess.client.logged_in = False
            try:
                sess.client.session.cookies.clear()
                debug_log(f"[prepare] tab={tab_id} 已清 cookie（force）")
            except Exception as exc:
                debug_log(f"[prepare] 清 cookie 失败: {exc}")
        elif site_changed:
            # 切到新站点：先尝试恢复该站已保存的登录会话（免重登）
            sess.client.logged_in = False
            try:
                sess.client.session.cookies.clear()
                sess.client.load_session()
                debug_log(f"[prepare] tab={tab_id} 站点切换，已尝试恢复持久化会话")
            except Exception as exc:
                debug_log(f"[prepare] 恢复会话失败（将走登录）: {exc}")

        try:
            self.login_info_probe(sess, url)
        except requests.exceptions.SSLError as exc:
            # 证书问题：不静默降级，交由前端向用户确认后重试
            debug_log(f"[ssl] tab={tab_id} 证书校验失败: {exc}")
            return _err(
                "该站点 HTTPS 证书校验失败（常见原因：证书已过期）。",
                ssl_error=True, detail=str(exc)[:300])
        resolved = sess.login_info.get("resolved_admin_url", "")
        if resolved and resolved != sess.client.admin_url:
            sess.client.adopt_resolved_url(resolved)

        # 拉到的不是登录页（无密码框）= 恢复的 cookie 仍有效，本就已登录。
        # load_session() 只恢复 cookie 不置 logged_in，这里必须补上，
        # 否则后续所有操作都会报“请先登录”。
        if not sess.login_info.get("is_login_page", True):
            if not sess.client.logged_in:
                sess.client.logged_in = True
                try:
                    sess.client.save_session()
                except Exception as exc:
                    debug_log(f"[prepare] 保存会话失败(可忽略): {exc}")
                debug_log(f"[prepare] tab={tab_id} 非登录页 → 判定会话仍有效，已置 logged_in")

        import base64
        captcha_b64 = ""
        captcha_mime = ""
        # 能拉到真实后台页/登录页，说明地址是有效的 → 计入下拉历史。
        # （只在登录成功时才记，会漏掉“试过但当时未登成”的站；
        #   而拉页失败的错别字/404 地址不会跑到这里，不会污染历史。）
        if record_history:
            try:
                self.config.add_url(sess.client.admin_url)
            except Exception as exc:
                debug_log(f"[prepare] 记地址历史失败(可忽略): {exc}")
        raw = sess.login_info.get("captcha_bytes")
        if raw:
            captcha_mime = _image_mime(raw)
            if captcha_mime:
                captcha_b64 = base64.b64encode(raw).decode("ascii")
            else:
                debug_log(f"[captcha] 拿到 {len(raw)}B 但非图片内容，不下发前端（避免裂图）")
        saved = self.config.get_credentials(sess.client.admin_url)
        return _ok(has_captcha=bool(sess.login_info.get("has_captcha")),
                   captcha=captcha_b64,
                   captcha_mime=captcha_mime or "image/png",
                   captcha_error=str(sess.login_info.get("captcha_error", "") or ""),
                   is_login_page=bool(sess.login_info.get("is_login_page", True)),
                   admin_url=sess.client.admin_url,
                   site_key=getattr(sess.client, "site_key", ""),
                   network_mode=getattr(sess.client, "network_mode", "direct"),
                   proxy_url=getattr(sess.client, "proxy_url", ""),
                   insecure=bool(sess.insecure),
                   urls=list(self.config.data.get("urls", [])),
                   saved_user=str(saved.get("user", "")),
                   saved_pass=str(saved.get("pass", "")))

    @_guard
    def remember_insecure_site(self, tab_id, remember=True):
        """记住“该站点忽略证书错误”的选择（仅限本机配置，可随时取消）。"""
        sess = self._session(tab_id)
        key = getattr(sess.client, "site_key", "")
        if not key:
            return _err("当前标签尚未确定站点")
        with self.config.locked():
            table = self.config.data.setdefault("insecure_sites", {})
            if remember:
                table[key] = True
            else:
                table.pop(key, None)
            self.config.save()
        if not remember:
            sess.insecure = False
            sess.client.session.verify = True
        return _ok(remembered=bool(remember))

    def login_info_probe(self, sess, url):
        """拉取登录页并写诊断日志（供 prepare_login 复用）。"""
        sess.login_info = sess.client.fetch_login_page()
        sess._prepared_raw_url = url
        try:
            cookie_names = [c.name for c in sess.client.session.cookies]
        except Exception:
            cookie_names = []
        raw_probe = sess.login_info.get("captcha_bytes") or b""
        debug_log(
            f"[prepare] tab={sess.tab_id} admin_url={sess.client.admin_url} "
            f"is_login_page={sess.login_info.get('is_login_page')} "
            f"has_captcha={sess.login_info.get('has_captcha')} "
            f"captcha_field={sess.login_info.get('captcha_field')} "
            f"captcha_bytes={len(raw_probe)} cookies={cookie_names}")

    @_guard
    def reload_captcha(self, tab_id):
        """只重载验证码图片，**不**重拉登录页、**不**清 cookie。

        这才是浏览器点验证码时的真实行为：同一个会话内换一张图，
        formcheck 与 session 保持不变，从根上避开“会话反复重建”带来的错位。
        """
        sess = self._session(tab_id)
        if not sess.login_info:
            return _err("请先填写后台地址并连接站点")
        src = sess.login_info.get("captcha_img_src") or "/core/code.php"
        from urllib.parse import urljoin
        captcha_url = urljoin(sess.client.admin_url, src)
        try:
            # Captcha images are same-site browser subresources.  Follow only
            # the shared same-origin/HTTP-to-HTTPS redirect policy so a
            # malformed login page cannot send the site's cookies to an
            # unrelated image host.
            resp = request_with_redirects(
                sess.client.session, "GET", captcha_url, timeout=10)
        except Exception as exc:
            debug_log(f"[captcha] 重载失败 {captcha_url}: {exc}")
            return _err(f"验证码获取失败：{exc}")
        raw = resp.content if resp.ok else b""
        mime = _image_mime(raw)
        if not mime:
            debug_log(f"[captcha] 重载得到 {len(raw)}B 非图片 status={resp.status_code}")
            return _err("验证码接口未返回图片，请稍后重试")
        sess.login_info["captcha_bytes"] = raw
        import base64
        debug_log(f"[captcha] tab={tab_id} 已重载图片 {len(raw)}B（会话未变）")
        return _ok(has_captcha=True,
                   captcha=base64.b64encode(raw).decode("ascii"),
                   captcha_mime=mime)

    @_guard
    def login(self, tab_id, username, password, captcha="", url=""):
        """提交登录。

        【关键】验证码绑定当前 session，因此本方法**绝不静默重拉登录页**（重拉会
        清 cookie 并换新验证码，使用户刚填的码失效，表现为“密码或验证码错误”）。
        """
        sess = self._session(tab_id)
        username = str(username or "").strip()
        password = str(password or "")
        captcha = str(captcha or "").strip()
        url = _sanitize_admin_url(url)
        if not username or not password:
            return _err("请填写用户名与密码")

        # 地址变更 → 旧会话作废（不能拿旧站的 formcheck 去登新站）
        if url and url != sess._prepared_raw_url:
            sess.login_info = {}
        if not sess.login_info:
            if not url:
                return _err("请先填写后台地址并获取验证码")
            probe = self.prepare_login(tab_id, url)
            if not probe.get("ok"):
                return probe
            if probe.get("has_captcha"):
                return _err("会话已刷新，请输入新的验证码后再登录",
                            need_captcha=True, captcha=probe.get("captcha", ""),
                            captcha_mime=probe.get("captcha_mime", "image/png"))

        # 【关键护栏】拿到的不是登录页 → 会话已有效，直接返回成功。
        # 不能继续提交：此时没解析到登录表单，formcheck 为空，
        # 提交只会换来“表单提交校验失败”，且刷验证码永远无法自愈。
        if not sess.login_info.get("is_login_page", True):
            sess.client.logged_in = True
            try:
                sess.client.save_session()
            except Exception as exc:
                debug_log(f"[login] 保存会话失败(可忽略): {exc}")
            self.config.add_url(sess.client.admin_url)
            debug_log(f"[login] tab={tab_id} 非登录页，会话已有效，跳过表单提交")
            return _ok(msg="会话仍有效，已直接进入后台", admin_url=sess.client.admin_url,
                       site_key=getattr(sess.client, "site_key", ""), restored=True)

        if sess.login_info.get("has_captcha") and not captcha:
            return _err("该站点需要验证码，请先填写", need_captcha=True)

        debug_log(f"[login] tab={tab_id} 提交 admin_url={sess.client.admin_url} "
                  f"has_captcha={sess.login_info.get('has_captcha')} "
                  f"captcha_len={len(captcha)} "
                  f"formcheck_len={len(sess.login_info.get('formcheck') or '')}")
        # 不记内容，只记长度与首尾空白：账密被服务端拒时能直接判定
        # “是真的密码错”还是“复制时带了空格/换行”。
        pwd_dirty = password != password.strip()
        debug_log(f"[login] tab={tab_id} 凭据体检 user_len={len(username)} "
                  f"pwd_len={len(password)} pwd_首尾空白={pwd_dirty} "
                  f"user_is_admin={username == 'admin'}")
        ok, msg = sess.client.login(username, password, captcha, sess.login_info)
        if not ok:
            raw = str(getattr(sess.client, "last_response", "") or "")[:400]
            debug_log(f"[login] tab={tab_id} 失败 msg={msg}")
            debug_log(f"[login] 服务端原始应答: {raw}")
            if "图片提交按钮" in str(msg or "") and "原生网页" in str(msg or ""):
                # A browser supplies the image submitter's physical click
                # coordinates.  Do not replay a guessed (0,0) pair through
                # requests; hand the verified login URL to the same native
                # WebView bridge used for SSO and dynamic login pages.
                return _err(msg, native_only=True,
                            native_url=sess.client.admin_url,
                            native_reason=msg,
                            requires_review=False, retryable=False)
            # 服务端明确报账密错：此时 formcheck/验证码已通过（PbootCMS 按
            # formcheck → 验证码 → 账密 逐层校验），不要重建会话，
            # 而是把可疑点直接告诉用户。
            if "用户名或密码" in msg:
                hints = []
                if pwd_dirty:
                    hints.append("密码首尾有空格/换行（多半是复制时带入的）")
                if username == "admin":
                    hints.append("用户名是默认的 admin，请确认该站确实用 admin")
                tip = ("（验证码已通过，服务端只拒账密）"
                       + ("；可疑点：" + "；".join(hints) if hints else ""))
                return _err(msg + tip,
                            need_captcha=bool(sess.login_info.get("has_captcha")),
                            credential_rejected=True, pwd_dirty=pwd_dirty)
            # 【自愈】formcheck 失效/为空属于会话层面的问题，光刷验证码永远好不了。
            # 这里强制重拉一份干净的登录页，拿到新 formcheck + 新验证码再让用户重试。
            if "表单提交校验" in msg or not (sess.login_info.get("formcheck") or ""):
                debug_log(f"[login] tab={tab_id} formcheck 异常，强制重建登录页")
                fresh = self.prepare_login(tab_id, url or sess._prepared_raw_url, force=True)
                if fresh.get("ok"):
                    return _err("登录表单已过期，已重新获取。请重新输入验证码后登录",
                                need_captcha=bool(fresh.get("has_captcha")),
                                captcha=fresh.get("captcha", ""),
                                captcha_mime=fresh.get("captcha_mime", "image/png"))
            # 保留会话与 formcheck，只需换新验证码图（reload_captcha）
            return _err(msg, need_captcha=bool(sess.login_info.get("has_captcha")))
        self.config.add_url(sess.client.admin_url)
        # 登录成功才记住账密（密码 DPAPI 加密），错的账密不会被存下来
        try:
            self.config.set_credentials(sess.client.admin_url, username, password)
        except Exception as exc:
            debug_log(f"[login] 保存账密失败(可忽略): {exc}")
        return _ok(msg=msg, admin_url=sess.client.admin_url,
                   urls=list(self.config.data.get("urls", [])),
                   site_key=getattr(sess.client, "site_key", ""))

    @_guard
    def logout(self, tab_id):
        sess = self._session(tab_id)
        if sess._busy:
            return _err("任务正在执行，请先取消或等待任务结束后再退出")
        site_key = getattr(sess.client, "site_key", "")
        ok, msg = sess.client.logout()
        native_windows_closed = _close_native_windows_for_site(site_key)
        self._clear_site_workflow_state(sess)
        # AuthMixin.logout 无论远端请求是否成功都会在 finally 中清除本地
        # Cookie/会话文件；远端失败不能让前端继续冒充“已登录”。
        return _ok(msg=msg, logged_out=True, remote_ok=bool(ok),
                   native_windows_closed=native_windows_closed)

    # ══════════════════════════════════════════════════════════
    #  栏目
    # ══════════════════════════════════════════════════════════
    @_guard
    def load_categories(self, tab_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        ctx = T.TaskContext(on_log=lambda m: _push("log", {"tab_id": tab_id, "msg": m}))
        fetched = T.run_fetch_categories(
            sess.client, ctx, return_session=True)
        if isinstance(fetched, (tuple, list)) and len(fetched) >= 4:
            tree, error, worker_cookies, worker_initial = fetched[:4]
            _merge_worker_session_result(
                sess.client.session,
                {"_session_cookie_records": worker_cookies,
                 "_session_cookie_initial_records": worker_initial})
        else:
            tree, error = fetched
        if error and not tree:
            return _err(error)
        sess.categories = tree
        return _ok(tree=tree, count=self._count_nodes(tree))

    def _count_nodes(self, nodes):
        total = 0
        for node in nodes or []:
            total += 1 + self._count_nodes(node.get("children"))
        return total

    @staticmethod
    def _invalidate_category_cache(sess):
        """A category mutation can change model routing and front URL paths."""
        sess.client._mcode_cache = {}
        sess.client._content_add_actions = {}
        sess.client._content_mcode_context = None

    @staticmethod
    def _clear_category_selection(sess, scode):
        """Never keep a form bound to a category that was changed/deleted."""
        scode = str(scode or "")
        if sess.publish_scode == scode:
            sess.publish_scode = ""
            sess.publish_mcode = None
            sess.publish_formcheck = ""
            sess.publish_action = ""
            sess.publish_open_values = None
            sess.form_fields = []
        if sess.edit_scode == scode:
            sess.edit_scode = ""
            sess.edit_mcode = None
            sess.edit_loaded_article_id = ""
            sess.edit_context_initialized = False
            sess.edit_form_fields = []
            sess.edit_baseline_key = None
            sess.edit_baseline_values = {}
            sess.articles = []
            sess.current_values = {}
            sess.edit_formcheck = ""
            sess.edit_url_hint = ""
            sess.edit_load_seq += 1

    def _category_write(self, sess, action, audit_action="", target_id="",
                        before=None, after=None):
        """Serialize short destructive calls with publish/edit work in this tab."""
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍后再操作栏目")
            sess._busy = True
        # Synchronous bridge writes still need a cancellable context: the UI
        # can call cancel_task while an independent upload XHR is in flight.
        write_ctx = T.TaskContext()
        previous_cancel = getattr(sess.client, "_active_cancel_callback", None)
        sess._task_ctx = write_ctx
        sess.client._active_cancel_callback = write_ctx.check_cancelled
        # Reset stale metadata from a previous operation.  The dedicated
        # client marks the exact form/GET dispatch below.
        sess.client.last_write_result = {
            "outcome": "not_sent", "write_attempted": False,
            "requires_review": False, "retryable": False,
        }
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            audit_action or "category_write", target_id, before or {})
        try:
            try:
                result = action()
            except Exception as exc:
                failure, journal_state = _write_exception_result(sess, exc)
                operation_journal.finish(journal_id, journal_state, failure,
                                         str(failure.get("msg", "") or ""))
                if audit_action:
                    record_audit(
                        getattr(sess.client, "site_key", ""), audit_action,
                        "category", target_id,
                        status="review" if journal_state == "review" else "failed",
                        before=before, after=after,
                        message=str(failure.get("msg", "") or ""))
                return failure
            tree = result.get("tree", []) if isinstance(result, dict) else []
            if isinstance(result, dict) and "tree" in result:
                sess.categories = tree
            self._invalidate_category_cache(sess)
            if audit_action:
                record_audit(
                    getattr(sess.client, "site_key", ""), audit_action,
                    "category", target_id or result.get("scode", ""),
                    before=before, after=after or result,
                    message=result.get("msg", ""))
            sess.client.last_write_result = {
                "outcome": "verified", "write_attempted": True,
                "requires_review": False, "retryable": False,
            }
            operation_journal.finish(journal_id, "success", result,
                                     str((result or {}).get("msg", "")))
            return _ok(**(result or {}))
        finally:
            if previous_cancel is None:
                try:
                    delattr(sess.client, "_active_cancel_callback")
                except AttributeError:
                    pass
            else:
                sess.client._active_cancel_callback = previous_cancel
            if sess._task_ctx is write_ctx:
                sess._task_ctx = None
            with sess._lock:
                sess._busy = False

    # ─── 栏目管理（增删改查）───
    @_guard
    def prepare_category_create(self, tab_id, parent_scode=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作栏目")
        parent_scode = str(parent_scode or "").strip()
        if parent_scode and not parent_scode.isdigit():
            return _err("父栏目编号无效")
        try:
            return _ok(**sess.client.prepare_category_create(parent_scode))
        except NativeModuleFallback as exc:
            return _err(str(exc), native_only=True, native_url=exc.native_url,
                        native_reason=exc.reason)

    @_guard
    def prepare_category_batch_create(self, tab_id, parent_scode=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作栏目")
        try:
            return _ok(**sess.client.prepare_category_batch_create(parent_scode))
        except NativeModuleFallback as exc:
            return _err(str(exc), native_only=True, native_url=exc.native_url,
                        native_reason=exc.reason)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def prepare_category_edit(self, tab_id, scode):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作栏目")
        try:
            return _ok(**sess.client.prepare_category_edit(scode))
        except NativeModuleFallback as exc:
            return _err(str(exc), native_only=True, native_url=exc.native_url,
                        native_reason=exc.reason)

    @_guard
    def create_category(self, tab_id, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._category_write(
            sess, lambda: sess.client.create_category(values or {}, str(revision or "")),
            "category_create", after=dict(values or {}))

    @_guard
    def create_categories_batch(self, tab_id, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._category_write(
            sess, lambda: sess.client.create_categories_batch(
                values or {}, str(revision or "")),
            "category_batch_create", after=dict(values or {}))

    @_guard
    def update_category(self, tab_id, scode, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        result = self._category_write(
            sess, lambda: sess.client.update_category(
                scode, values or {}, str(revision or "")),
            "category_update", str(scode or ""), after=dict(values or {}))
        if result.get("ok"):
            self._clear_category_selection(sess, scode)
        return result

    @_guard
    def delete_category(self, tab_id, scode, expected_name="", allow_children=False):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        result = self._category_write(
            sess, lambda: sess.client.delete_category(
                scode, str(expected_name or ""), bool(allow_children)),
            "category_delete", str(scode or ""),
            before={"name": str(expected_name or "")})
        if result.get("ok"):
            self._clear_category_selection(sess, scode)
        return result

    # ─── 网站 Slide/轮播管理（独立于文章图集）───
    def _slide_write(self, sess, action, audit_action="", target_id="",
                     before=None):
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍后再操作 Slide")
            sess._busy = True
        write_ctx = T.TaskContext()
        previous_cancel = getattr(sess.client, "_active_cancel_callback", None)
        sess._task_ctx = write_ctx
        sess.client._active_cancel_callback = write_ctx.check_cancelled
        sess.client.last_write_result = {
            "outcome": "not_sent", "write_attempted": False,
            "requires_review": False, "retryable": False,
        }
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            audit_action or "slide_write", target_id, before or {})
        try:
            try:
                result = action()
            except Exception as exc:
                failure, journal_state = _write_exception_result(sess, exc)
                operation_journal.finish(journal_id, journal_state, failure,
                                         str(failure.get("msg", "") or ""))
                if audit_action:
                    record_audit(getattr(sess.client, "site_key", ""),
                                 audit_action, "slide", target_id,
                                 status="review" if journal_state == "review" else "failed",
                                 before=before,
                                 message=str(failure.get("msg", "") or ""))
                return failure
            if isinstance(result, dict) and "slides" in result:
                sess.slides = list(result.get("slides") or [])
            if audit_action:
                record_audit(getattr(sess.client, "site_key", ""),
                             audit_action, "slide", target_id or
                             str((result or {}).get("slide", {}).get("id", "")),
                             before=before, after=result,
                             message=str((result or {}).get("msg", "")))
            sess.client.last_write_result = {
                "outcome": "verified", "write_attempted": True,
                "requires_review": False, "retryable": False,
            }
            operation_journal.finish(journal_id, "success", result,
                                     str((result or {}).get("msg", "")))
            return _ok(**(result or {}))
        finally:
            if previous_cancel is None:
                try:
                    delattr(sess.client, "_active_cancel_callback")
                except AttributeError:
                    pass
            else:
                sess.client._active_cancel_callback = previous_cancel
            if sess._task_ctx is write_ctx:
                sess._task_ctx = None
            with sess._lock:
                sess._busy = False

    @_guard
    def load_slides(self, tab_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        try:
            result = sess.client.list_slides()
            sess.slides = list(result.get("slides") or [])
            return _ok(slides=sess.slides, count=len(sess.slides))
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def prepare_slide_create(self, tab_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作 Slide")
        try:
            return _ok(**sess.client.prepare_slide_create())
        except NativeModuleFallback as exc:
            return _err(str(exc), native_only=True, native_url=exc.native_url,
                        native_reason=exc.reason)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def prepare_slide_edit(self, tab_id, slide_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作 Slide")
        try:
            return _ok(**sess.client.prepare_slide_edit(slide_id))
        except NativeModuleFallback as exc:
            return _err(str(exc), native_only=True, native_url=exc.native_url,
                        native_reason=exc.reason)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def create_slide(self, tab_id, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._slide_write(
            sess, lambda: sess.client.create_slide(values or {}, str(revision or "")),
            "slide_create", before=dict(values or {}))

    @_guard
    def update_slide(self, tab_id, slide_id, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._slide_write(
            sess, lambda: sess.client.update_slide(
                slide_id, values or {}, str(revision or "")),
            "slide_update", str(slide_id or ""))

    @_guard
    def delete_slide(self, tab_id, slide_id, expected_title=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._slide_write(
            sess, lambda: sess.client.delete_slide(
                slide_id, str(expected_title or "")),
            "slide_delete", str(slide_id or ""),
            before={"title": str(expected_title or "")})

    @_guard
    def select_category(self, tab_id, scode, preserve_defaults=False):
        """选定栏目后加载其发布表单字段（字段映射的依据）。"""
        sess = self._session(tab_id)
        if sess._busy:
            return _err("任务正在执行，暂不能切换发布栏目")
        scode = str(scode or "").strip()
        if not scode:
            return _err("请选择栏目")
        if not sess.client.logged_in:
            return _err("请先登录")
        # Invalidate the previous native add page before probing the new
        # category.  A failed/partial probe must never leave a button that
        # opens the previous model's form.
        sess.publish_page_url = ""
        try:
            formcheck, fields = sess.client.get_content_form(scode)
        except Exception as exc:
            # A fully dynamic custom model may create its form/editor only in
            # page JavaScript.  Do not guess a desktop POST shape, but keep
            # the exact same-origin add page available for the native WebView
            # fallback so the user can complete the operation with the real
            # browser runtime.
            page_url = str(getattr(sess.client, "_content_add_native_url", "") or "")
            native_reason = str(getattr(sess.client, "_content_add_native_reason", "") or exc)
            mcode = ""
            try:
                mcode = str(sess.client._get_mcode_for_scode(scode) or "")
                if mcode and not page_url:
                    page_url = sess.client._url(
                        f"Content/add/mcode/{mcode}") + "&" + urlencode({"scode": scode})
            except Exception as page_exc:
                debug_log(f"[select_category] 动态表单原生回退地址解析失败: {page_exc}")
            sess.publish_page_url = page_url
            return _err(
                f"读取栏目表单失败：{native_reason}；该栏目可能依赖动态网页脚本，"
                f"请使用原生网页发布" if page_url else f"读取栏目表单失败：{exc}",
                page_url=page_url, native_url=page_url, mcode=mcode,
                native_reason=native_reason, native_only=bool(page_url))
        if not preserve_defaults or sess.publish_scode != scode or sess.publish_open_values is None:
            sess.publish_open_values = deepcopy(_opened_form_values(fields))
        sess.form_fields = fields or []
        sess.publish_scode = scode
        sess.publish_mcode = sess.client._get_mcode_for_scode(scode)
        sess.publish_formcheck = str(formcheck or "")
        sess.publish_action = str(
            getattr(sess.client, "_content_add_actions", {}).get(
                str(sess.publish_mcode or ""), "") or "")
        if sess.publish_mcode:
            # This is the same add-page family already fetched by
            # ``get_content_form``.  Keep the resolved route same-origin and
            # carry the selected category exactly as the webpage does.
            page = str(getattr(sess.client, "_content_add_page_url", "") or "").strip()
            if not page:
                page = sess.client._url(
                    f"Content/add/mcode/{sess.publish_mcode}")
            # ``_url`` already owns the admin entry's ``?p=`` query
            # parameter; append the category as a sibling query parameter,
            # rather than nesting a second ``?`` inside the route value.
            try:
                parsed_page = urlparse(page)
                # Preserve the server's original query spelling (notably the
                # unescaped slash in ``p=/Content/add/...``).  Rebuilding the
                # whole query with ``urlencode`` would turn it into ``%2F``
                # and make the native URL differ from the page's own link.
                raw_parts = []
                for part in str(parsed_page.query or "").split("&"):
                    key = part.split("=", 1)[0]
                    if unquote_plus(key).lower() == "scode":
                        continue
                    if part:
                        raw_parts.append(part)
                raw_parts.append(urlencode({"scode": scode}))
                sess.publish_page_url = parsed_page._replace(
                    query="&".join(raw_parts), fragment="").geturl()
            except (TypeError, ValueError):
                sess.publish_page_url = page + "&" + urlencode({"scode": scode})
        return _ok(scode=scode, mcode=sess.publish_mcode,
                   page_url=sess.publish_page_url,
                   fields=_public_form_fields(sess.form_fields),
                   submitter=deepcopy(getattr(sess.client, "_content_add_submitter", None)),
                   submitter_options=deepcopy(
                       getattr(sess.client, "_content_add_submitter_options", []) or []))

    # ══════════════════════════════════════════════════════════
    #  HTML 解析与字段映射
    # ══════════════════════════════════════════════════════════
    @_guard
    def pick_html(self, picker_key="html"):
        """打开系统文件对话框选择 HTML（全局对话框，返回路径由前端再调 parse_html）。"""
        paths = _window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            directory=self._dialog_directory(picker_key),
            file_types=("HTML 文件 (*.html;*.htm)", "所有文件 (*.*)"))
        if not paths:
            return _ok(cancelled=True)
        self._remember_dialog_path(picker_key, paths[0])
        return _ok(path=paths[0])

    @_guard
    def pick_html_files(self, picker_key="batch_html", from_folder=False):
        """选择多个 HTML，或从一个文件夹递归建立完整批量队列。"""
        if from_folder:
            selected = _window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=self._dialog_directory(picker_key))
            if not selected:
                return _ok(cancelled=True, paths=[])
            folder = selected if isinstance(selected, str) else selected[0]
            folder = os.path.abspath(str(folder or ""))
            if not os.path.isdir(folder):
                return _err("所选文件夹不存在")
            paths = []
            for root, dirs, names in os.walk(folder):
                # 不跟随目录链接，避免一个导入动作意外跨出所选文件夹。
                dirs[:] = [name for name in dirs
                           if not os.path.islink(os.path.join(root, name))]
                for name in sorted(names):
                    if os.path.splitext(name)[1].lower() not in (".html", ".htm"):
                        continue
                    path = os.path.join(root, name)
                    if os.path.isfile(path):
                        paths.append(path)
                    # Keep every file selected by the native folder picker.
                    # The browser does not impose a desktop-only 1000-item
                    # cap; downstream task/JSON/resource guards provide the
                    # explicit safety boundary instead of silently dropping
                    # later files.
            self._remember_dialog_path(picker_key, folder)
            return _ok(paths=paths, count=len(paths), folder=folder,
                       limited=False)
        selected = _window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            directory=self._dialog_directory(picker_key),
            file_types=("HTML 文件 (*.html;*.htm)", "所有文件 (*.*)"))
        paths = [os.path.abspath(str(path)) for path in (selected or [])
                 if os.path.isfile(str(path)) and
                 os.path.splitext(str(path))[1].lower() in (".html", ".htm")]
        if not paths:
            return _ok(cancelled=True, paths=[])
        self._remember_dialog_path(picker_key, paths[0])
        unique_paths = list(dict.fromkeys(paths))
        return _ok(paths=unique_paths, count=len(unique_paths), limited=False)

    @_guard
    def parse_html(self, tab_id, path, workflow="publish"):
        sess = self._session(tab_id)
        if sess._busy:
            return _err("任务正在执行，暂不能更换 HTML 文件")
        workflow = "edit" if str(workflow or "").lower() == "edit" else "publish"
        path = str(path or "").strip()
        # 先清空对应流程的旧稿。即使新文件不存在或解析抛错，也不能继续提交旧稿。
        if workflow == "edit":
            sess.edit_context_initialized = True
            sess.edit_parsed = {}
            sess.edit_orig_parsed = {}
            sess.edit_html_path = ""
        else:
            sess.parsed = {}
            sess.orig_parsed = {}
            sess.html_path = ""
        if not path or not os.path.isfile(path):
            return _err("HTML 文件不存在")
        parsed = SEOHTMLParser().parse_file(path)
        if not isinstance(parsed, dict) or not parsed:
            return _err("HTML 文件未解析出可发布字段")
        if workflow == "edit":
            sess.edit_parsed = parsed
            sess.edit_orig_parsed = dict(parsed)
            sess.edit_html_path = path
        else:
            sess.parsed = parsed
            sess.orig_parsed = dict(parsed)
            sess.html_path = path
        content = str(parsed.get("content", "") or "")
        html_dir = os.path.dirname(os.path.abspath(path))
        document_url = getattr(sess.client, "base_url", "") or getattr(sess.client, "admin_url", "")
        inline, problems = scan_html_images(content, html_dir, document_url)
        remote_images = scan_html_remote_images(content, document_url, html_dir)
        media_assets, media_problems = scan_html_media(content, html_dir, document_url)
        expected = ["title", "subtitle", "filename", "description",
                    "tags", "keywords", "image_alt", "content"]
        missing = [k for k in expected
                   if k not in parsed or not str(parsed[k]).strip()]
        return _ok(path=path,
                   workflow=workflow,
                   fields=[{"key": k, "value": str(v)} for k, v in parsed.items()],
                   missing=missing,
                   inline_images=[{"src": x["src"], "alt": x["alt"]} for x in inline],
                   remote_images=remote_images,
                   image_problems=problems,
                   media_assets=[{"src": x["src"], "tag": x["tag"], "attr": x["attr"],
                                  "media_kind": x.get("media_kind", "file")}
                                 for x in media_assets],
                   media_problems=media_problems,
                   suggest=self._suggest_mapping(sess, workflow))

    def _suggest_mapping(self, sess, workflow="publish"):
        """自动映射建议：HTML 字段 → CMS 字段（沿用 R20 的匹配算法）。"""
        is_edit = str(workflow or "").lower() == "edit"
        parsed = sess.edit_parsed if is_edit else sess.parsed
        form_fields = sess.edit_form_fields if is_edit else sess.form_fields
        mcode = sess.edit_mcode if is_edit else sess.publish_mcode
        saved = mapping_scope(self.config.data, "product",
                              getattr(sess.client, "site_key", ""), mcode)
        suggestion = {}
        for key in parsed:
            if key in saved:
                suggestion[key] = saved[key]
                continue
            suggestion[key] = choose_best_field(key, form_fields) or ""
        return suggestion

    @_guard
    def save_mapping(self, tab_id, mapping, workflow="publish"):
        sess = self._session(tab_id)
        is_edit = str(workflow or "").lower() == "edit"
        mcode = sess.edit_mcode if is_edit else sess.publish_mcode
        with self.config.locked():
            save_mapping_scope(self.config.data, "product",
                               getattr(sess.client, "site_key", ""),
                               dict(mapping or {}), mcode)
            self.config.save()
        return _ok()

    @staticmethod
    def _editor_word_limit_issue(value, limit, label="正文"):
        """Return a browser/UEditor-style maximumWords error, if any.

        UEditor counts the textual content (not HTML tags) with JavaScript
        string semantics.  UTF-16 code units are therefore used so astral
        characters such as emoji do not pass the desktop preflight while the
        browser would reject them.
        """
        try:
            maximum = int(limit)
        except (TypeError, ValueError):
            return ""
        if maximum <= 0:
            return ""
        from bs4 import BeautifulSoup
        text = BeautifulSoup(str(value or ""), "html.parser").get_text(
            "", strip=False)
        units = len(text.encode("utf-16-le")) // 2
        if units > maximum:
            return f"{label}超过当前网页编辑器字数上限（{maximum} 字符）"
        return ""

    @_guard
    def preflight(self, tab_id, mapping, overrides=None, backend_fields=None):
        """发布前校验：字段映射 + 必填 + 内置图完整性。不发起任何写请求。"""
        sess = self._session(tab_id)
        mapping = dict(mapping or {})
        overrides = dict(overrides or {})
        rows = []
        fields, backend_errors = _validate_form_overrides(
            backend_fields or {}, sess.form_fields)
        for html_key, cms_field in mapping.items():
            value = overrides.get(html_key, sess.parsed.get(html_key, ""))
            rows.append((html_key, cms_field, value))
            if cms_field:
                fields[cms_field] = value
        # validate_mapping checks required CMS controls through each
        # descriptor's current/default value.  Reflect explicit backend-field
        # choices there as well, otherwise a required field filled in the new
        # settings panel would be falsely reported as empty.
        effective_form_fields = []
        for descriptor in sess.form_fields or []:
            item = dict(descriptor)
            name = str(item.get("name", "") or "")
            if name in fields:
                item["value"] = fields[name]
            effective_form_fields.append(item)
        errors, warnings = validate_mapping(rows, effective_form_fields)
        errors.extend(backend_errors)
        fields = sanitize_publish_fields(fields)
        # Only validate core values that the opened form actually exposes or
        # that the user explicitly mapped.  Some legitimate Pboot models are
        # link-only or use a custom body field; inventing a synthetic
        # ``content`` requirement makes the desktop stricter than the web.
        form_names = {str(item.get("name", "") or "")
                      for item in (sess.form_fields or [])}
        mapped_title = str(mapping.get("title") or "")
        mapped_content = str(mapping.get("content") or "")
        title_supported = bool(mapped_title or "title" in form_names or
                               any(str(name).lower() in ("name", "subject", "标题")
                                   for name in form_names))
        content_supported = bool(mapped_content or "content" in form_names or
                                 any(str(name).lower() in ("body", "details", "detail", "article")
                                     for name in form_names))
        title = str(fields.get(mapped_title or "title", "") or "").strip()
        content_target = mapped_content or ("content" if "content" in fields else "")
        content = str(fields.get(content_target, "") or "").strip()
        if title_supported and not title:
            errors.append("标题为空")
        if content_supported and not content:
            errors.append("正文 content 为空")
        # A literal instance maximumWords is safe to check before any upload.
        # If the page limit is dynamic or server-only, the worker performs the
        # same check after policy discovery and still fails before POST/upload.
        for descriptor in sess.form_fields or []:
            name = str(descriptor.get("name", "") or "")
            issue = self._editor_word_limit_issue(
                fields.get(name, ""), descriptor.get("maximum_words"),
                descriptor.get("label") or name or "正文")
            if issue:
                errors.append(issue)
        html_dir = os.path.dirname(os.path.abspath(sess.html_path)) \
            if sess.html_path else ""
        document_url = getattr(sess.client, "base_url", "") or getattr(sess.client, "admin_url", "")
        inline, problems = (scan_html_images(content, html_dir, document_url)
                            if content else ([], []))
        missing = [x for x in problems if x.get("kind") == "local"]
        if missing:
            errors.append("正文引用的本地图片找不到：" +
                          "、".join(x["src"] for x in missing[:5]))
        snapshot_token = self._preflight_snapshot_token(
            sess, mapping, overrides, backend_fields)
        return _ok(errors=errors, warnings=warnings,
                    field_count=len(fields),
                    inline_count=len(inline),
                    problems=problems,
                    snapshot_token=snapshot_token,
                    preview={k: str(v)[:200] for k, v in fields.items()})

    @staticmethod
    def _preflight_snapshot_token(sess, mapping, overrides,
                                  backend_fields=None):
        """Hash the exact publish source and choices without trusting UI state.

        The file bytes are included, so an editor overwriting the same path
        invalidates a previously successful check even when every visible
        control still has the same value.
        """
        path = str(getattr(sess, "html_path", "") or "")
        if not path or not os.path.isfile(path):
            return ""
        digest = hashlib.sha256()
        payload = {
            "scode": str(getattr(sess, "publish_scode", "") or ""),
            "mcode": str(getattr(sess, "publish_mcode", "") or ""),
            "html_path": os.path.normcase(os.path.abspath(path)),
            "mapping": dict(mapping or {}),
            "overrides": dict(overrides or {}),
            "backend_fields": dict(backend_fields or {}),
            "parsed": dict(getattr(sess, "parsed", {}) or {}),
            "form_fields": [
                {"name": str(item.get("name", "") or ""),
                 "label": str(item.get("label", "") or "")}
                for item in (getattr(sess, "form_fields", []) or [])],
        }
        digest.update(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()

    @_guard
    def validate_preflight_snapshot(self, tab_id, snapshot_token,
                                    mapping, overrides=None,
                                    backend_fields=None):
        """Lightweight local validation for reuse; performs no network I/O."""
        sess = self._session(tab_id)
        supplied = str(snapshot_token or "").strip().lower()
        current = self._preflight_snapshot_token(
            sess, dict(mapping or {}), dict(overrides or {}),
            dict(backend_fields or {}))
        valid = bool(supplied and current and supplied == current)
        return _ok(valid=valid,
                   reason="" if valid else "栏目、HTML 文件或字段值已变化",
                   snapshot_token=current if valid else "")

    def _resolve_missing_product_front_urls(self, sess, products, product_ids):
        """Read-only recovery for cached products whose model is known but URL is not.

        Product sync normally obtains the real front URL from the list page or
        the category edit form.  A timeout during that secondary lookup used
        to leave a valid model indistinguishable from a missing one.  During
        an internal-link check, retry only the small set actually referenced
        by the article, then persist any recovered URLs for future checks.

        ``get_edit_form`` only opens the Content/mod form endpoint and
        ``get_category_url_paths`` filters state-toggle links, so this method
        deliberately performs no CMS write request.
        """
        requested = []
        seen = set()
        for product_id in product_ids or ():
            product_id = str(product_id or "").strip()
            if product_id and product_id not in seen:
                seen.add(product_id)
                requested.append(product_id)
        # Keep a malformed article with many unresolved links from turning a
        # pre-publish check into an unbounded sequence of backend reads.
        # Three is enough for common product series such as DK1/DK2/DK3 and
        # bounds UI-thread recovery even when an article contains many bad
        # placeholders.
        requested = requested[:3]
        if not requested:
            return []

        by_id = {str(product.get("id", "") or "").strip(): product
                 for product in (products or [])}
        details = {}
        for product_id in requested:
            product = by_id.get(product_id)
            if not product or str(product.get("front_url", "") or "").strip():
                continue
            mcode = str(product.get("mcode", "") or "").strip()
            if not mcode:
                debug_log(f"[link_url_recovery] id={product_id} 缺少 mcode，跳过")
                continue
            try:
                _formcheck, _fields, values = sess.client.get_edit_form(
                    product_id, mcode,
                    edit_url=str(product.get("edit_url", "") or ""),
                    request_timeout=4, max_candidates=1)
                values = values or {}
                scode = str(values.get("scode", "") or "").strip()
                filename = str(values.get("filename", "") or
                               values.get("urlname", "") or "").strip()
                if not scode:
                    debug_log(f"[link_url_recovery] id={product_id} 编辑表单缺少 scode")
                    continue
                details[product_id] = {"scode": scode, "filename": filename}
            except Exception as exc:
                # The check must remain usable if one old product is no
                # longer readable.  The response will accurately say that
                # the model exists but its front URL is unavailable.
                debug_log(f"[link_url_recovery] id={product_id} 读取失败: {exc}")

        if not details:
            return []
        scodes = list(dict.fromkeys(
            item["scode"] for item in details.values() if item.get("scode")))
        try:
            # Do not run the normal 90-second category-list request as part
            # of a synchronous pre-publish check.  The safe listing link and
            # one category form are normally enough to recover a series URL;
            # failing fast leaves a clear "model exists, URL unavailable"
            # message instead of blocking for minutes.
            category_paths = sess.client.get_category_url_paths(
                scodes, listing_timeout=4, form_timeout=4,
                resolve_mcode=False)
        except Exception as exc:
            debug_log(f"[link_url_recovery] 读取栏目 URL 名称失败: {exc}")
            return []

        updated = []
        base_url = getattr(sess.client, "base_url", "") or sess.client.admin_url
        for product_id, detail in details.items():
            front_url = T._build_front_url(
                base_url, detail.get("filename", ""), product_id,
                category_paths.get(detail.get("scode", ""), ""))
            if not front_url:
                continue
            # A filename/category-derived route is only a candidate.  Do not
            # write it into the product cache until the same authenticated
            # session has proved that the page exists.  This keeps a guessed
            # ``/category/id.html`` from becoming indistinguishable from a
            # real preview link in later software operations.
            try:
                verified = probe_links(
                    sess.client.session,
                    [{"href": front_url, "abs": front_url, "index": 0}],
                    timeout=4, max_workers=1, total_timeout=6,
                    request_mode="get")
                live = bool(verified and verified[0].get("state") == "ok")
            except Exception as exc:
                live = False
                debug_log(f"[link_url_recovery] id={product_id} 候选前台链接验证失败: {exc}")
            if not live:
                debug_log(f"[link_url_recovery] id={product_id} 候选前台链接未验证，保留缺失状态")
                continue
            product = by_id[product_id]
            product["front_url"] = front_url
            updated.append(product)
        if not updated:
            return []

        site = getattr(sess.client, "site_key", "")
        db_upsert_products(site, updated)
        # ``products`` comes from the database, while the query screen keeps
        # its own list.  Reflect the recovered URL there too, so a later
        # lookup in this tab sees the same cache without a reload.
        updated_urls = {str(item.get("id", "")): item.get("front_url", "")
                        for item in updated}
        for item in getattr(sess, "products", []) or []:
            product_id = str(item.get("id", "") or "")
            if product_id in updated_urls:
                item["front_url"] = updated_urls[product_id]
        debug_log(f"[link_url_recovery] 已恢复 {len(updated)} 条产品前台链接")
        return updated

    @_guard
    def check_links(self, tab_id, mapping, overrides=None, use_current=False,
                    workflow="publish", expected_article_id="",
                    _task_ctx=None):
        """正文内链体检：找出指向本站但实际不存在的死链。

        HTML 由 AI 提前生成，正文里的本站链接 slug 常是编的，
        发布后即 404。这里用**已登录会话**逐个探测存活性，
        只报告不自作主张；是否移除由前端让用户确认。
        """
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录再体检内链")
        def stage(done, current):
            if _task_ctx is not None:
                _task_ctx.progress(done, 5, current)

        def cancelled():
            return bool(_task_ctx is not None and _task_ctx.cancelled)

        if cancelled():
            return _err("内链检查已取消", cancelled=True)
        stage(1, "提取并分类正文链接")
        mapping = dict(mapping or {})
        overrides = dict(overrides or {})
        is_edit = str(workflow or "").lower() == "edit"
        expected_article_id = str(expected_article_id or "").strip()
        if (is_edit and expected_article_id and
                sess.edit_loaded_article_id != expected_article_id):
            return _err("当前文章已变化，内链检测结果已作废，请重新载入文章")
        parsed = sess.edit_parsed if is_edit else sess.parsed
        content = ""
        if use_current:
            content = str(sess.current_values.get("content", "") or "")
        else:
            for html_key, cms_field in mapping.items():
                if cms_field == "content":
                    content = str(overrides.get(html_key,
                                                parsed.get(html_key, "")) or "")
                    break
        base = getattr(sess.client, "base_url", "") or sess.client.admin_url
        buckets = scan_content_links(content, base)
        internal = buckets["internal"]
        if not internal:
            stage(5, "正文中没有需要检查的站内链接")
            return _ok(dead=[], correct=[], links=[], internal_count=0,
                       external_count=len(buckets["external"]))
        if cancelled():
            return _err("内链检查已取消", cancelled=True)
        stage(2, f"匹配产品库（站内链接 {len(internal)} 条）")
        # 按型号匹配产品库的真实前台 URL（库里拉取时已存 front_url）
        products = db_load_products(getattr(sess.client, "site_key", ""))
        matched = match_products_by_model(internal, products)
        # A cache may know that MODEL-200 exists while a previous category-URL
        # lookup timed out and left front_url blank.  Retry only those linked
        # products through safe read-only form endpoints before calling it
        # "unresolved" in the UI.
        unresolved_ids = []
        for item in matched:
            if item.get("missing_front_url"):
                unresolved_ids.extend(item.get("unresolved_product_ids", []))
        if unresolved_ids:
            stage(3, f"补查 {min(len(set(unresolved_ids)), 3)} 个缺失产品链接")
        if self._resolve_missing_product_front_urls(sess, products, unresolved_ids):
            matched = match_products_by_model(internal, products)
        if cancelled():
            return _err("内链检查已取消", cancelled=True)
        # 只有 href=# / 空 href 才是待填占位链接。普通链接即使从锚文字推断出
        # 型号，也必须实际探测；旧逻辑把“能识别型号”等同于“占位链接”，会漏报 404。
        placeholder = [m for m in matched if not m.get("abs")]
        real_links = [m for m in matched if m.get("abs")]
        probe_queue = [dict(item, probe_kind="current") for item in real_links]
        # 产品缓存只提供候选，不能未经探测就自动填充。唯一候选也要实际
        # 访问成功后才进入 suggest；重复型号则保持空白让用户选择。
        for item in matched:
            candidate = str(item.get("suggest", "") or "").strip()
            if (candidate and not item.get("ambiguous") and
                    candidate != str(item.get("abs", "") or "").strip()):
                probe_queue.append({
                    "href": candidate, "abs": candidate,
                    "text": item.get("text", ""), "index": item.get("index"),
                    "probe_kind": "suggestion"})
        # 内链检测仍处于提交前阶段，不能让数十个超时链接
        # 把 UI 阻塞十几分钟。有界并发 + 45 秒批次上限；
        # 未完成项会标记 unchecked，由用户明确决定是否继续。
        stage(4, f"实际访问验证 {len(probe_queue)} 个链接（可取消）")
        probed = probe_links(
            sess.client.session, probe_queue,
            timeout=8, max_workers=5, total_timeout=45,
            cancel_callback=cancelled if _task_ctx is not None else None,
            # A user clicking an internal link in the native page performs a
            # browser navigation (GET), not a speculative HEAD request.  Use
            # the same mode for the actual content check; the lower-level
            # helper still keeps ``head`` as an explicit compatibility mode
            # for callers that need a cheap probe.
            request_mode="browser")
        if cancelled():
            return _err("内链检查已取消", cancelled=True)
        current_by_index = {
            item.get("index"): item for item in probed
            if item.get("probe_kind") == "current"}
        suggestion_by_index = {
            item.get("index"): item for item in probed
            if item.get("probe_kind") == "suggestion"}
        # 合并全部内链状态；发布流程仍只处理待填占位和真死链。
        all_links = []
        for m in placeholder:
            candidate = str(m.get("suggest", "") or "")
            candidate_probe = suggestion_by_index.get(m.get("index"), {})
            verified_suggest = (candidate
                                if candidate_probe.get("state") == "ok" else "")
            if m.get("ambiguous"):
                fill_status = "同型号对应多个产品，请手动选择"
            elif candidate and not verified_suggest:
                fill_status = "候选链接未验证通过，请手动核对"
            elif verified_suggest:
                fill_status = "待填充（候选已验证）"
            elif m.get("missing_front_url"):
                fill_status = "产品库已找到型号，但前台链接尚未获取"
            else:
                fill_status = "未匹配到唯一产品链接"
            all_links.append({"href": m["href"], "abs": "",
                              "index": m.get("index"),
                              "text": m.get("text", ""),
                              "model": m.get("model", ""),
                              "suggest": verified_suggest,
                              "candidate": candidate,
                              "suggest_state": candidate_probe.get("state", ""),
                              "suggest_status": candidate_probe.get("status", ""),
                              "matched": m.get("matched", False),
                              "missing_front_url": m.get("missing_front_url", False),
                              "ambiguous": m.get("ambiguous", False),
                              "candidates": m.get("candidates", []),
                              "state": "pending", "status": fill_status})
        for original in real_links:
            checked = current_by_index.get(original.get("index"), {})
            mm = original
            candidate = str(mm.get("suggest", "") or "")
            candidate_probe = suggestion_by_index.get(mm.get("index"), {})
            verified_suggest = (candidate
                                if candidate_probe.get("state") == "ok" else "")
            all_links.append({"href": checked.get("href", mm.get("href", "")),
                              "abs": checked.get("abs", mm.get("abs", "")),
                              "index": checked.get("index", mm.get("index")),
                              "text": checked.get("text", mm.get("text", "")),
                              "model": mm.get("model", ""),
                              "suggest": verified_suggest,
                              "candidate": candidate,
                              "suggest_state": candidate_probe.get("state", ""),
                              "suggest_status": candidate_probe.get("status", ""),
                              "matched": mm.get("matched", False),
                              "missing_front_url": mm.get("missing_front_url", False),
                              "ambiguous": mm.get("ambiguous", False),
                              "candidates": mm.get("candidates", []),
                              "state": checked.get("state", "unknown"),
                              "status": checked.get("status", "")})
        all_links.sort(key=lambda item: (item.get("index") is None,
                                         item.get("index") or 0))
        pending = [item for item in all_links
                   if item.get("state") in ("pending", "dead")]
        correct = [item for item in all_links if item.get("state") == "ok"]
        stage(5, f"检查完成：正确 {len(correct)}，待处理 {len(pending)}")
        return _ok(dead=pending, correct=correct, links=all_links,
                   internal_count=len(internal),
                   external_count=len(buckets["external"]),
                   have_products=bool(products))

    @_guard
    def start_link_check(self, tab_id, mapping, overrides=None,
                         use_current=False, workflow="publish",
                         expected_article_id="", request_id=""):
        """Run the complete link check in a cancellable background task."""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录再体检内链")
        request_id = str(request_id or "")[:160]
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        ctx = self._new_ctx(sess, "link_check")
        if sess._closed.is_set():
            ctx.cancel()

        def worker():
            result = {"ok": False, "msg": "内链检查未完成"}
            try:
                if ctx.cancelled:
                    result = {"ok": False, "cancelled": True,
                              "msg": "内链检查已取消"}
                else:
                    result = self.check_links(
                        sess.tab_id, dict(mapping or {}),
                        dict(overrides or {}), bool(use_current), workflow,
                        expected_article_id, ctx)
                    if ctx.cancelled or result.get("cancelled"):
                        result = {"ok": False, "cancelled": True,
                                  "msg": "内链检查已取消"}
            except Exception as exc:
                debug_log(
                    f"[start_link_check] tab={sess.tab_id} 异常: {exc}")
                result = {"ok": False, "msg": str(exc)}
            finally:
                sess._busy = False
                if sess._task_ctx is ctx:
                    sess._task_ctx = None
            if not sess._closed.is_set():
                payload = dict(result)
                payload.update({"tab_id": sess.tab_id,
                                "request_id": request_id})
                _push("link_check_done", payload)

        threading.Thread(target=worker, daemon=True).start()
        return _ok(started=True, request_id=request_id)

    # ═══════════════════════════════════════════════════════════
    #  发布
    # ═══════════════════════════════════════════════════════════
    @_guard
    def publish(self, tab_id, options):
        """启动后台发布事务；进度经事件推送，完成后推 publish_done。"""
        sess = self._session(tab_id)
        options = dict(options or {})
        if not sess.client.logged_in:
            return _err("请先登录")
        scode = str(options.get("scode", "") or "").strip()
        if not scode:
            return _err("请选择发布栏目")
        if sess.publish_scode and sess.publish_scode != scode:
            return _err("发布栏目状态已变化，请重新选择栏目后再发布")
        expected_html = str(options.get("html_path", "") or "").strip()
        if expected_html and not _same_local_path(expected_html, sess.html_path):
            return _err("HTML 文件状态已变化，请重新选择文件后再发布")
        expected_preflight = str(options.get("preflight_token", "") or "").strip()
        if expected_preflight:
            current_preflight = self._preflight_snapshot_token(
                sess, dict(options.get("mapping") or {}),
                dict(options.get("overrides") or {}),
                dict(options.get("backend_fields") or {}))
            if not current_preflight or current_preflight != expected_preflight:
                return _err("发布快照已变化，已停止提交；请重新运行发布前检查")
        try:
            thumbnail = thumbnail_options(options, sess.form_fields)
            options.update(gallery_options(options, sess.form_fields))
        except ValueError as exc:
            return _err(str(exc))
        thumbnail_path = thumbnail['thumbnail_path']
        carousel_paths = list(options.get("carousel_paths") or [])
        publish_field_names = {
            str(item.get("name", "")) for item in (sess.form_fields or [])}
        if carousel_paths and "pics" not in publish_field_names:
            return _err("当前栏目表单没有轮播图字段 pics，已停止提交以避免写错字段")
        carousel_size = None
        if carousel_paths:
            if options.get("carousel_size") not in (None, "", "original"):
                native_url = _native_carousel_fallback(sess, editing=False)
                return _err(
                    "已停止：客户端裁切/转码会改变上传字节。请使用原文件上传，"
                    "或在认证原生网页中设置图集尺寸。软件未发送上传请求。",
                    native_only=bool(native_url), native_url=native_url,
                    native_reason="客户端图集尺寸处理已停用；需由后台原生网页处理",
                    retryable=False)
            try:
                carousel_size = _carousel_size(options)
            except ValueError as exc:
                return _err(str(exc))

        mapping = dict(options.get("mapping") or {})
        overrides = dict(options.get("overrides") or {})
        explicit_fields, backend_errors = _validate_form_overrides(
            options.get("backend_fields") or {}, sess.form_fields)
        if backend_errors:
            return _err("；".join(backend_errors))
        try:
            explicit_fields, media_uploads = _extract_media_uploads(
                explicit_fields, sess.form_fields)
        except ValueError as exc:
            return _err(str(exc))
        # Like the opened web form, retain its actual defaults (including date
        # and status). A fresh form/token is still required immediately at POST.
        fields = deepcopy(sess.publish_open_values if sess.publish_open_values is not None
                          else _opened_form_values(sess.form_fields))
        fields.update(explicit_fields)
        for html_key, cms_field in mapping.items():
            if cms_field:
                fields[cms_field] = overrides.get(html_key,
                                                  sess.parsed.get(html_key, ""))
        fields = sanitize_publish_fields(fields)
        for flag in ("istop", "isrecommend", "isheadline"):
            if flag in options:
                fields[flag] = "1" if options[flag] else "0"
        if "status" not in explicit_fields and "status" not in mapping.values() and "offline" in options:
            fields["status"] = "0" if options.get("offline") else "1"

        target_field = str(options.get("target_field") or
                           mapping.get("content") or "content")
        # A model without a body field must not receive an invented content
        # key.  For custom-body models the mapped field is the actual editor
        # target, matching the web form's submitted control.
        if (target_field == "content" and "content" not in publish_field_names
                and mapping.get("content") != "content"):
            target_field = ""
        content = str(fields.get(target_field, "") or "")
        # 按用户对每条死链的选择改写正文：
        #   link_actions=[{href, new_href}] —— new_href 非空则替换，为空则移除壳。
        # 兼容旧的 dead_links（一键移除）。
        link_actions = options.get("link_actions") or []
        if link_actions and content:
            content, _rep, _rm = apply_link_actions(content, link_actions)
            fields[target_field] = content
        elif options.get("dead_links") and content:
            content = strip_links(content, options.get("dead_links"))
            fields[target_field] = content
        html_dir = os.path.dirname(os.path.abspath(sess.html_path)) \
            if sess.html_path else ""
        document_url = getattr(sess.client, "base_url", "") or getattr(sess.client, "admin_url", "")
        inline, _problems = scan_html_images(content, html_dir, document_url)
        remote_images = scan_html_remote_images(content, document_url, html_dir)
        media_assets, _media_problems = scan_html_media(content, html_dir, document_url)

        # 发布前抓同标题 ID 快照；查询失败必须传 None（无法验证），
        # 绝不能传空集合，否则空响应时会把旧同标题文章误判为「本次新增」。
        known_ids = None
        title = str(fields.get("title", "") or "").strip()
        try:
            articles = sess.client.get_article_list(scode)
            if getattr(articles, "complete", False):
                known_ids = {str(a.get("id")) for a in articles if a.get("id")}
            else:
                _push("log", {"tab_id": tab_id,
                              "msg": "发布前文章列表未确认完整，将不使用 ID 差集判定"})
        except Exception as exc:
            _push("log", {"tab_id": tab_id,
                          "msg": f"发布前查询文章列表失败，空响应将按失败处理: {exc}"})

        payload = {
            "_login": T.snapshot_login(sess.client, include_headers=True),
            "_verify": sess.client.session.verify,
            "_network": T.network_snapshot(sess.client),
            "site_id": getattr(sess.client, "site_key", ""),
            "scode": scode,
            "mcode": sess.publish_mcode,
            "fields": fields,
            "parsed_fields": dict(sess.parsed),
            "image_paths": list(options.get("image_paths") or []),
            "inline_images": inline,
            "remote_images": remote_images,
            "media_assets": media_assets,
            "target_field": target_field,
            "strategy": options.get("strategy", "top"),
            "width_mode": options.get("width_mode", "preserve"),
            "formcheck": sess.publish_formcheck,
            "media_uploads": media_uploads,
            # Upload-policy discovery must GET the actual add page, not the
            # form's eventual POST action (custom backends may make the latter
            # POST-only or route it through a different controller).
            "upload_page_url": sess.publish_page_url,
            # The WebView may know File.type for extensionless drops.  Keep
            # only the snapshot mapping; webtasks narrows it to referenced
            # existing files before it can affect policy validation.
            "asset_mimes": dict(options.get("asset_mimes") or {}),
            # Pillow cannot guarantee byte-identical output to the active
            # browser's Canvas encoder.  Strict mode therefore hands pages
            # with UEditor client compression to the authenticated WebView.
            "strict_browser_upload_parity": True,
            "known_article_ids": known_ids,
            "upload_cache": {},
            **thumbnail,
            **({'gallery_plan': deepcopy(options['gallery_plan'])} if 'gallery_plan' in options else {}),
            "carousel_paths": carousel_paths,
            "carousel_size": carousel_size,
            "carousel_mode": options.get("carousel_mode", "append"),
            "carousel_title_field": ("picstitle[]"
                                     if "pics" in publish_field_names else ""),
            "responsive_context": normalize_responsive_context(
                options.get("responsive_context")),
            # The WebView may supply the actual currentSrc selected by its
            # native picture/srcset algorithm.  The worker re-validates this
            # untrusted candidate as same-origin before using it.
            "browser_first_image": deepcopy(options.get("browser_first_image"))
                if isinstance(options.get("browser_first_image"), dict) else
                str(options.get("browser_first_image") or ""),
            "add_url_hint": sess.publish_action,
            "submitter": deepcopy(options.get("submitter")) if isinstance(options.get("submitter"), dict) else None,
            "_audit_before": {},
        }
        # 保存已完成校验和内链改写的完整任务快照。重试时即使用户随后选择了
        # 另一份 HTML，也只能继续这一次失败的任务，避免内容与图片串单。
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            if expected_preflight:
                current_preflight = self._preflight_snapshot_token(
                    sess, mapping, overrides,
                    dict(options.get("backend_fields") or {}))
                if current_preflight != expected_preflight:
                    return _err("发布快照已变化，已停止提交；请重新运行发布前检查")
            sess._busy = True
            sess.last_publish_payload = dict(payload)
            sess.last_publish_title = title
            self._start_task(sess, T.run_publish, payload, "publish_done", title=title)
        return _ok(started=True, inline_count=len(inline))

    @_guard
    def retry_failed_images(self, tab_id, options):
        """用原始发布参数重试；已经成功上传的图片直接复用缓存。"""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录原站点后再重试")
        if not sess.retryable or not sess.last_publish_payload:
            return _err("没有可重试的图片上传任务")
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        payload = dict(sess.last_publish_payload)
        current_site = getattr(sess.client, "site_key", "")
        if payload.get("site_id") and payload.get("site_id") != current_site:
            sess.retryable = False
            sess.last_publish_payload = None
            sess.last_publish_title = ""
            sess._busy = False
            return _err("该重试任务属于另一个站点，已为安全起见清除")
        payload["fields"] = dict(payload.get("fields") or {})
        payload["upload_cache"] = dict(sess.upload_cache)
        payload["_login"] = T.snapshot_login(sess.client, include_headers=True)
        payload["_verify"] = sess.client.session.verify
        payload["_network"] = T.network_snapshot(sess.client)
        self._start_task(sess, T.run_publish, payload, "publish_done",
                         title=sess.last_publish_title)
        return _ok(started=True, retry=True)

    # ═══════════════════════════════════════════════════════════
    #  编辑修改
    # ═══════════════════════════════════════════════════════════
    @_guard
    def load_articles(self, tab_id, scode):
        sess = self._session(tab_id)
        if sess._busy:
            return _err("任务正在执行，暂不能切换编辑栏目")
        if not sess.client.logged_in:
            return _err("请先登录")
        scode = str(scode or "").strip()
        if not scode:
            return _err("请选择栏目")
        previous_scode = str(sess.edit_scode or "")
        if previous_scode and previous_scode != scode:
            # 新栏目属于新编辑上下文，不能继续携带上一栏目的稿件。
            sess.edit_parsed = {}
            sess.edit_orig_parsed = {}
            sess.edit_html_path = ""
        # 一旦开始新的编辑上下文，先使旧文章表单失效。即使网络请求失败，
        # 后续提交也不能继续使用上一文章的 action/formcheck/current_values。
        sess.edit_context_initialized = True
        sess.edit_load_seq += 1
        sess.edit_scode = scode
        sess.edit_mcode = sess.client._get_mcode_for_scode(scode)
        sess.current_mcode = sess.edit_mcode  # 兼容旧扩展调用
        sess.edit_loaded_article_id = ""
        sess.edit_form_fields = []
        sess.edit_formcheck = ""
        sess.edit_url_hint = ""
        sess.current_values = {}
        sess.articles = []
        sess.articles = sess.client.get_article_list(scode)
        if not getattr(sess.articles, "complete", False):
            sess.articles = []
            return _err("文章列表未能按目标栏目完整加载，已停止以避免选错文章")
        return _ok(articles=[{"id": str(a.get("id", "")),
                              "title": str(a.get("title", "")),
                              "edit_url": str(a.get("edit_url", ""))}
                             for a in sess.articles],
                   mcode=sess.edit_mcode)

    # ─── 原生内容列表管理（复制/移动/删除/排序/状态）───
    def _content_admin_write(self, sess, action, audit_action="", target_id="",
                             before=None):
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请先等待当前任务完成")
            sess._busy = True
        write_ctx = T.TaskContext()
        previous_cancel = getattr(sess.client, "_active_cancel_callback", None)
        sess._task_ctx = write_ctx
        sess.client._active_cancel_callback = write_ctx.check_cancelled
        sess.client.last_write_result = {
            "outcome": "not_sent", "write_attempted": False,
            "requires_review": False, "retryable": False,
        }
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            audit_action or "content_admin", target_id, before or {})
        try:
            try:
                result = action()
            except Exception as exc:
                failure, journal_state = _write_exception_result(sess, exc)
                operation_journal.finish(journal_id, journal_state, failure,
                                         str(failure.get("msg", "") or ""))
                if audit_action:
                    record_audit(getattr(sess.client, "site_key", ""), audit_action,
                                 "content_admin", target_id,
                                 status="review" if journal_state == "review" else "failed",
                                 before=before,
                                 message=str(failure.get("msg", "") or ""))
                return failure
            if audit_action:
                record_audit(getattr(sess.client, "site_key", ""), audit_action,
                             "content_admin", target_id, before=before,
                             after=result, message=str((result or {}).get("msg", "")))
            sess.client.last_write_result = {
                "outcome": "verified", "write_attempted": True,
                "requires_review": False, "retryable": False,
            }
            operation_journal.finish(journal_id, "success", result,
                                     str((result or {}).get("msg", "")))
            return _ok(**(result or {}))
        finally:
            if previous_cancel is None:
                try:
                    delattr(sess.client, "_active_cancel_callback")
                except AttributeError:
                    pass
            else:
                sess.client._active_cancel_callback = previous_cancel
            if sess._task_ctx is write_ctx:
                sess._task_ctx = None
            with sess._lock:
                sess._busy = False

    def _bulk_link_root(self, sess):
        from pathlib import Path
        from client_utils import get_base_dir
        return Path(get_base_dir()) / 'link_backups' / sess.client.site_key

    @_guard
    def bulk_link_status(self, tab_id):
        sess = self._session(tab_id)
        return _ok(**deepcopy(getattr(sess, '_bulk_link_state', {'running':False, 'msg':'尚未预览'})))

    @_guard
    def bulk_link_start(self, tab_id, action, options=None):
        import bulk_links as B
        from datetime import datetime
        import uuid
        sess = self._session(tab_id)
        options = options or {}
        if not sess.client.logged_in:
            return _err('请先登录')
        if action not in ('preview', 'apply', 'restore_preview', 'restore'):
            return _err('未知操作')
        if action == 'preview':
            if not str(options.get('scode', '')).isdigit():
                return _err('请先选择需要更新内链的栏目')
            try:
                cutoff = B.date_value(options.get('after'))
                if cutoff.tzinfo:
                    return _err('请使用本地日期时间')
            except (ValueError, TypeError):
                return _err('请选择有效的起始发布时间')
        with sess._lock:
            if sess._busy:
                return _err('本站点有任务正在执行')
            if action in ('apply', 'restore'):
                plan = getattr(sess, '_bulk_link_plan', None)
                if not plan or plan['token'] != options.get('token') or plan['restore'] != (action == 'restore'):
                    return _err('预览已失效，请重新预览')
                sess._bulk_link_plan = None  # Consume once, including uncertain outcomes.
            else:
                sess._bulk_link_plan = None
            sess._busy = True
        ctx = T.TaskContext()
        sess._task_ctx = ctx
        sess._bulk_link_state = {'running': True, 'msg': '正在读取并核对，请稍候…', 'rows':[]}
        def worker():
            previous = getattr(sess.client, '_active_cancel_callback', None)
            sess.client._active_cancel_callback = ctx.check_cancelled
            try:
                if action in ('apply', 'restore'):
                    results = B.execute(sess.client, plan, self._bulk_link_root(sess), ctx, action == 'restore')
                    sess._bulk_link_state = {'running':False, 'msg':'处理结束，请查看逐条结果', 'results':results,
                        'backup_path':str(self._bulk_link_root(sess))}
                    return
                rows, skipped = [], []
                if action == 'restore_preview':
                    root = self._bulk_link_root(sess)
                    restored_ids = set()
                    for path in sorted(root.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True) if root.exists() else []:
                        record = json.loads(path.read_text(encoding='utf-8'))
                        if record.get('state') in ('prepared','applied','review') and record['id'] not in restored_ids:
                            ctx.check_cancelled()
                            _, fields, current = sess.client.get_edit_form(record['id'], record['mcode'], edit_url=record['edit_url'])
                            if fields and current.get('content') == record['after']:
                                rows.append(record)
                                restored_ids.add(record['id'])
                            else:
                                skipped.append({'id':record['id'], 'reason':'正文与备份结果不一致，不覆盖后续修改'})
                else:
                    sync = T.run_product_sync(sess.client, ctx, cached_products=[], mode='full',
                                              mcode=options.get('mcode') or None, max_workers=4, prefer_current_filename=True)
                    if not sync.get('ok') or not sync.get('complete'):
                        raise ValueError(sync.get('msg') or '产品库同步不完整，已停止')
                    if sync.get('refreshed', 0) < sync.get('requested', 0):
                        raise ValueError('部分产品型号详情未读全，不能安全判断重复型号，请重新同步')
                    products = sync['products']
                    # Verify every unique destination using a read-only frontend GET.
                    verified = {}
                    for product in products:
                        ctx.check_cancelled()
                        url = product.get('front_url', '')
                        if not B.local_url(url, sess.client.base_url):
                            product['front_url'] = ''
                            continue
                        if url not in verified:
                            try:
                                response = sess.client._read_request('GET', url, timeout=15)
                                resolved = response.url
                                verified[url] = resolved if response.status_code == 200 and B.local_url(resolved, sess.client.base_url) else ''
                                response.close()
                            except Exception:
                                verified[url] = ''
                        product['front_url'] = verified[url]
                    index = B.model_index(products)
                    scode = str(options['scode'])
                    mcode = sess.client._resolve_mcode(scode)
                    if not mcode:
                        raise ValueError('无法确定当前栏目模型')
                    articles = sess.client.get_article_list(scode)
                    if not getattr(articles, 'complete', False):
                        raise ValueError('文章列表读取不完整，已停止')
                    for article in articles:
                        ctx.check_cancelled()
                        _, descriptors, values = sess.client.get_edit_form(article['id'], mcode, edit_url=article.get('edit_url'))
                        if not descriptors or 'content' not in values or 'date' not in values:
                            skipped.append({'id':article['id'], 'reason':'无法读取正文或发布时间'})
                            continue
                        try:
                            if B.date_value(values['date']) <= cutoff:
                                continue
                        except (ValueError, TypeError):
                            skipped.append({'id':article['id'], 'reason':'发布时间无法解析'})
                            continue
                        before = values['content']
                        after, changes, warnings = B.rewrite_models(before, index, article['id'], sess.client.base_url, bool(options.get('add')))
                        skipped.extend(dict(item, id=article['id']) for item in warnings)
                        if changes:
                            rows.append({'id':article['id'], 'mcode':mcode, 'title':values.get('title',article['title']),
                                         'date':values['date'], 'scode':scode, 'edit_url':article.get('edit_url',''),
                                         'before':before, 'after':after, 'changes':changes})
                token = uuid.uuid4().hex
                sess._bulk_link_plan = {'token':token, 'rows':rows, 'restore':action=='restore_preview'}
                visible = [{k:v for k,v in row.items() if k not in ('before','after')} for row in rows]
                sess._bulk_link_state = {'running':False, 'msg':f'预览完成：{len(rows)} 篇可处理，尚未修改网站',
                    'rows':visible, 'skipped':skipped, 'token':token, 'restore':action=='restore_preview'}
            except Exception as exc:
                sess._bulk_link_state = {'running':False, 'msg':'任务停止：'+str(exc), 'error':True,
                                         'backup_path':str(self._bulk_link_root(sess))}
            finally:
                sess.client._active_cancel_callback = previous
                sess._task_ctx = None
                with sess._lock:
                    sess._busy = False
        threading.Thread(target=worker, daemon=True).start()
        return _ok()

    @_guard
    def prepare_content_admin(self, tab_id, scode, mcode=None, page=1, keyword=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作")
        try:
            result = sess.client.prepare_content_admin(
                scode, mcode=mcode, page=page, keyword=keyword)
            return _ok(**result)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def content_bulk_action(self, tab_id, scode, ids, operation,
                            revision="", target_scode="", sorting=None, mcode=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        values = [str(item or "") for item in (ids or [])]
        return self._content_admin_write(
            sess,
            lambda: sess.client.content_bulk_action(
                scode, values, operation, str(revision or ""),
                target_scode=str(target_scode or ""), sorting=sorting or {},
                mcode=mcode),
            "content_" + str(operation or "").lower(),
            ",".join(values), before={"scode": str(scode or ""), "ids": values})

    @_guard
    def delete_content(self, tab_id, scode, ids, revision="", mcode=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        values = [str(item or "") for item in (ids or [])]
        return self._content_admin_write(
            sess,
            lambda: sess.client.delete_content(
                scode, values, str(revision or ""), mcode=mcode),
            "content_delete", ",".join(values),
            before={"scode": str(scode or ""), "ids": values})

    @_guard
    def toggle_content_field(self, tab_id, article_id, field, value, expected_url="",
                             scode="", mcode=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._content_admin_write(
            sess,
            lambda: sess.client.toggle_content_field(
                article_id, field, value, expected_url=str(expected_url or ""),
                scode=str(scode or ""), mcode=mcode, verify_readback=True),
            "content_" + str(field or "").lower(), str(article_id or ""),
            before={"field": str(field or ""), "value": str(value or "")})

    # ─── 单页管理（独立于文章 Content/index）───
    @_guard
    def load_single_pages(self, tab_id, keyword=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作")
        try:
            result = sess.client.list_single_pages(str(keyword or ""))
            return _ok(**result)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def prepare_single_edit(self, tab_id, single_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作")
        try:
            return _ok(**sess.client.prepare_single_edit(single_id))
        except NativeModuleFallback as exc:
            return _err(str(exc), native_only=True, native_url=exc.native_url,
                        native_reason=exc.reason)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def update_single(self, tab_id, single_id, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._content_admin_write(
            sess,
            lambda: sess.client.update_single(single_id, values or {}, str(revision or "")),
            "single_update", str(single_id or ""), before=dict(values or {}))

    @_guard
    def toggle_single_status(self, tab_id, single_id, value, expected_url=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._content_admin_write(
            sess,
            lambda: sess.client.toggle_single_status(
                single_id, value, expected_url=str(expected_url or ""),
                verify_readback=True),
            "single_status", str(single_id or ""),
            before={"value": str(value or "")})

    # ─── 其余后台模块（菜单发现 + 安全动态表单）───
    @_guard
    def load_admin_modules(self, tab_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作")
        try:
            return _ok(**sess.client.list_admin_modules())
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def inspect_admin_module(self, tab_id, module_url):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作")
        try:
            return _ok(**sess.client.inspect_admin_module(str(module_url or "")))
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def prepare_admin_module(self, tab_id, module_url):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍后再操作")
        try:
            result = sess.client.prepare_admin_module(str(module_url or ""))
            result["fields"] = _public_form_fields(result.get("fields", []))
            return _ok(**result)
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def update_admin_module(self, tab_id, module_url, values, revision="", submitter=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        try:
            result = self._journal_call(
                sess, "admin_module_update", str(module_url or ""),
                {"field_names": list(values) if isinstance(values, dict) else []},
                lambda: sess.client.update_admin_module(
                    str(module_url or ""), values or {}, str(revision or ""),
                    submitter if isinstance(submitter, dict) else None))
        except NativeModuleFallback as exc:
            # The page changed after it was opened, or its runtime form is
            # owned by JavaScript.  Return the server-resolved URL so the UI
            # can continue in the authenticated browser without replaying a
            # guessed desktop request.
            return _err(str(exc), native_url=exc.native_url,
                        native_only=True, native_reason=exc.reason)
        result["fields"] = _public_form_fields(result.get("fields", []))
        return _ok(**result)

    @_guard
    def load_edit_form(self, tab_id, article_id, preserve_baseline=False,
                       responsive_context=None):
        sess = self._session(tab_id)
        if sess._busy:
            return _err("任务正在执行，暂不能切换文章")
        if not sess.client.logged_in:
            return _err("请先登录")
        article_id = str(article_id or "").strip()
        if not article_id:
            return _err("请选择要修改的文章")
        if not sess.edit_mcode:
            return _err("请先选择栏目并加载文章")
        previous_article_id = str(sess.edit_loaded_article_id or "")
        if previous_article_id and previous_article_id != article_id:
            sess.edit_parsed = {}
            sess.edit_orig_parsed = {}
            sess.edit_html_path = ""
        # 先清旧状态，保证读取新文章失败后不能提交旧文章。请求序号确保
        # 快速切换 A/B 文章时，较慢返回的 A 不能覆盖已经载入的 B。
        sess.edit_load_seq += 1
        request_seq = sess.edit_load_seq
        sess.edit_loaded_article_id = ""
        sess.edit_form_fields = []
        sess.edit_formcheck = ""
        sess.edit_url_hint = ""
        sess.current_values = {}
        edit_url = ""
        for item in sess.articles:
            if str(item.get("id")) == str(article_id):
                edit_url = item.get("edit_url", "")
                break
        if sess.articles and not edit_url and not any(
                str(item.get("id")) == article_id for item in sess.articles):
            return _err("所选文章已不在当前栏目列表中，请重新加载文章")
        requested_mcode = sess.edit_mcode
        native_edit_url = str(edit_url or "").strip()
        if not native_edit_url:
            try:
                native_edit_url = sess.client._url(
                    f"Content/mod/mcode/{requested_mcode}/id/{article_id}")
            except Exception:
                native_edit_url = ""
        formcheck, fields, current = sess.client.get_edit_form(
            article_id, requested_mcode, edit_url=edit_url)
        client_native_edit_url = str(
            getattr(sess.client, "_content_edit_native_url", "") or "")
        if client_native_edit_url:
            native_edit_url = client_native_edit_url
        native_edit_reason = str(
            getattr(sess.client, "_content_edit_native_reason", "") or "")
        if request_seq != sess.edit_load_seq or requested_mcode != sess.edit_mcode:
            return _err("文章选择已变化，已丢弃较旧的表单响应", stale=True)
        if not fields and not current:
            if native_edit_url:
                message = ((native_edit_reason + "；该页面由网页脚本控制，")
                           if native_edit_reason
                           else "未能读取该文章的编辑表单；该页面可能依赖动态网页脚本，")
                message += "请使用原生网页编辑"
            else:
                message = "未能读取该文章的编辑表单"
            return _err(message, native_url=native_edit_url,
                        native_only=bool(native_edit_url),
                        native_reason=native_edit_reason)
        baseline_key = (sess.edit_scode, str(requested_mcode), article_id)
        if not preserve_baseline or sess.edit_baseline_key != baseline_key:
            sess.edit_baseline_key = baseline_key
            sess.edit_baseline_values = deepcopy(current)
        sess.edit_form_fields = fields
        sess.edit_formcheck = formcheck
        sess.edit_url_hint = edit_url
        sess.current_values = current
        sess.edit_loaded_article_id = article_id
        content = str(current.get("content", "") or "")
        image_items = []
        base_url = sess.client.base_url or sess.client.admin_url
        responsive_context = normalize_responsive_context(responsive_context)
        for item in describe_html_images(content, responsive_context):
            # Prefer the largest statically declared responsive/lazy candidate
            # for the read-only preview, while keeping ``src`` as the exact
            # replacement target.  No media query or page JavaScript runs in
            # this path, so the candidate list remains visible to the user.
            preview_source = item.get("preview_src") or item["src"]
            preview = urljoin(base_url.rstrip("/") + "/", preview_source)
            parsed_preview = urlparse(preview)
            # The UI is allowed to preview only this site's own image URLs;
            # never turn an arbitrary remote URL embedded in content into a
            # browser request from the desktop app.
            if not _same_http_origin(preview, base_url):
                preview = ""
            image_items.append({**item, "preview_url": preview,
                                "preview_source": preview_source})
        return _ok(formcheck=bool(formcheck),
                   fields=_public_form_fields(fields, truncate_values=True),
                   submitter=deepcopy(getattr(sess.client, "_content_edit_submitter", None)),
                   submitter_options=deepcopy(
                       getattr(sess.client, "_content_edit_submitter_options", []) or []),
                    status={k: str(current.get(k, ""))
                            for k in ("status", "istop", "isrecommend", "isheadline")},
                   article_id=article_id,
                   native_url=native_edit_url,
                   content_hash=_content_hash(content),
                   content_images=image_items,
                   responsive_context=responsive_context,
                    suggest=self._suggest_mapping(sess, "edit"))

    @_guard
    def submit_edit(self, tab_id, options):
        """启动后台编辑事务（与发布同一套安全约定）。"""
        sess = self._session(tab_id)
        options = dict(options or {})
        if not sess.client.logged_in:
            return _err("请先登录")
        article_id = str(options.get("article_id", "") or "").strip()
        if not article_id:
            return _err("请先选择要修改的文章")
        # 正式 UI 流程只允许提交刚刚成功加载的那一篇。兼容未初始化编辑
        # 上下文的旧 API 调用，但一旦调用过 load_articles/load_edit_form 就严格绑定。
        if (sess.edit_context_initialized and
                sess.edit_loaded_article_id != article_id):
            return _err("当前文章表单未成功加载或选择已变化，请重新加载后再修改")
        expected_html = str(options.get("html_path", "") or "").strip()
        active_edit_html = (sess.edit_html_path if sess.edit_context_initialized
                            else sess.html_path)
        if expected_html and not _same_local_path(expected_html, active_edit_html):
            return _err("编辑 HTML 文件状态已变化，请重新选择文件后再修改")
        image_replacements = list(options.get("image_replacements") or [])
        expected_content_hash = str(options.get("expected_content_hash", "") or "").strip()
        if expected_html and image_replacements:
            return _err("新 HTML 与原正文图片直接替换不能同时提交")
        if image_replacements:
            if not expected_content_hash:
                return _err("详情图片状态已失效，请重新加载文章")
            current_content = str(sess.current_values.get("content", "") or "")
            if _content_hash(current_content) != expected_content_hash:
                return _err("文章正文已变化，请重新加载文章后再提交")
            seen_indexes = set()
            for item in image_replacements:
                if not isinstance(item, dict):
                    return _err("详情图片替换项格式无效")
                try:
                    index = int(item.get("index"))
                except (TypeError, ValueError):
                    return _err("详情图片替换项缺少有效序号")
                raw_path = str(item.get("local_path", "") or "").strip()
                path = os.path.abspath(raw_path) if raw_path else ""
                property_update = any(key in item for key in
                                      ("new_alt", "new_width", "new_height"))
                if index < 0 or index in seen_indexes:
                    return _err("详情图片替换项无效或序号重复")
                if not raw_path and not property_update:
                    return _err("详情图片替换项缺少本地图片或图片属性")
                if raw_path and not os.path.isfile(path):
                    return _err("详情图片替换项无效或本地文件不存在")
                if not str(item.get("expected_src", "") or "") or not str(item.get("tag_fingerprint", "") or ""):
                    return _err("详情图片替换项校验信息缺失，请重新加载文章")
                if "new_alt" in item:
                    new_alt = str(item.get("new_alt", "") or "")
                    if len(new_alt) > 4096 or any(ord(char) < 32 and char not in "\r\n\t" for char in new_alt):
                        return _err("详情图片 alt 文本无效或过长")
                    item["new_alt"] = new_alt
                for key, label in (("new_width", "图片宽度"), ("new_height", "图片高度")):
                    if key in item:
                        try:
                            item[key] = validate_image_dimension(item.get(key), label)
                        except ValueError as exc:
                            return _err(str(exc))
                seen_indexes.add(index)
                item["index"] = index
                item["local_path"] = path
        edit_mcode = sess.edit_mcode or (
            sess.current_mcode if not sess.edit_context_initialized else None)
        if not edit_mcode:
            return _err("请先选择栏目并加载文章")
        try:
            thumbnail = thumbnail_options(options, sess.edit_form_fields if sess.edit_context_initialized else sess.form_fields)
            options.update(gallery_options(options, sess.edit_form_fields if sess.edit_context_initialized else sess.form_fields))
        except ValueError as exc:
            return _err(str(exc))
        thumbnail_path = thumbnail['thumbnail_path']
        carousel_paths = list(options.get("carousel_paths") or [])
        edit_field_names = {
            str(item.get("name", "")) for item in (sess.edit_form_fields if sess.edit_context_initialized else sess.form_fields)
        }
        if carousel_paths and "pics" not in edit_field_names:
            return _err("当前文章表单没有轮播图字段 pics，已停止提交以避免写错字段")
        carousel_size = None
        if carousel_paths:
            if options.get("carousel_size") not in (None, "", "original"):
                native_url = _native_carousel_fallback(sess, editing=True)
                sess._busy = False
                return _err(
                    "已停止：客户端裁切/转码会改变上传字节。请使用原文件上传，"
                    "或在认证原生网页中设置图集尺寸。软件未发送上传请求。",
                    native_only=bool(native_url), native_url=native_url,
                    native_reason="客户端图集尺寸处理已停用；需由后台原生网页处理",
                    retryable=False)
            try:
                carousel_size = _carousel_size(options)
            except ValueError as exc:
                sess._busy = False
                return _err(str(exc))
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True

        mapping = dict(options.get("mapping") or {})
        overrides = dict(options.get("overrides") or {})
        edit_parsed = (sess.edit_parsed if sess.edit_context_initialized
                       else sess.parsed)
        edit_form_fields = (sess.edit_form_fields if sess.edit_context_initialized
                            else sess.form_fields)
        mapped = {}
        rows = []
        for html_key, cms_field in mapping.items():
            value = overrides.get(html_key, edit_parsed.get(html_key, ""))
            rows.append((html_key, cms_field, value))
            if cms_field:
                mapped[cms_field] = value
        errors, _warnings = validate_mapping(rows, edit_form_fields,
                                             require_core=False)
        if errors:
            sess._busy = False
            return _err("字段映射检查未通过：" + "；".join(errors))

        explicit_backend, backend_errors = _validate_form_overrides(
            options.get("backend_fields") or {}, edit_form_fields)
        if backend_errors:
            sess._busy = False
            return _err("后台字段检查未通过：" + "；".join(backend_errors))
        try:
            explicit_backend, media_uploads = _extract_media_uploads(
                explicit_backend, edit_form_fields)
        except ValueError as exc:
            sess._busy = False
            return _err(str(exc))

        # Both legacy cover modes mean preserving unmapped values. The client
        # merges these explicit updates into the LATEST form, not this snapshot.
        fields = dict(mapped)
        fields.update(explicit_backend)
        for flag in ("istop", "isrecommend", "isheadline"):
            if flag in options:
                fields[flag] = "1" if options[flag] else "0"

        target_field = str(options.get("target_field") or
                           mapping.get("content") or "content")
        if target_field == "content" and "content" not in {
                str(item.get("name", "")) for item in (edit_form_fields or [])}:
            target_field = ""
        link_actions = options.get("link_actions") or []
        if link_actions:
            content = str(fields.get(target_field, "") or
                          sess.current_values.get(target_field, "") or "")
            if content:
                content, _rep, _rm = apply_link_actions(content, link_actions)
                fields[target_field] = content
        edit_html_path = (sess.edit_html_path if sess.edit_context_initialized
                          else sess.html_path)
        html_dir = os.path.dirname(os.path.abspath(edit_html_path)) \
            if edit_html_path else ""
        current_content = str(fields.get(target_field, "") or "")
        document_url = getattr(sess.client, "base_url", "") or getattr(sess.client, "admin_url", "")
        inline, _problems = scan_html_images(current_content, html_dir, document_url)
        remote_images = scan_html_remote_images(current_content, document_url, html_dir)
        media_assets, _media_problems = scan_html_media(current_content, html_dir, document_url)

        edit_field_names = {
            str(item.get("name", "")) for item in (edit_form_fields or [])
        }
        if carousel_paths and "pics" not in edit_field_names:
            sess._busy = False
            return _err("当前文章表单没有轮播图字段 pics，已停止提交以避免写错字段")
        carousel_title_field = ("picstitle[]"
                                if "pics" in edit_field_names else "")
        changed_names = set(fields)
        changed_names.update(item.get("field", "") for item in media_uploads
                             if item.get("field"))
        if inline or options.get('image_paths') or image_replacements:
            changed_names.add(target_field)
        if thumbnail['thumbnail_mode'] != 'none':
            changed_names.add('ico')
        if thumbnail['thumbnail_from_first']:
            changed_names.add(target_field)
        if carousel_paths or 'gallery_plan' in options:
            changed_names.update(('pics', 'picstitle[]'))
        if options.get('refresh_publish_date'):
            changed_names.add('date')
        baseline_key = (sess.edit_scode, str(edit_mcode), article_id)
        baseline = (sess.edit_baseline_values if sess.edit_baseline_key == baseline_key
                    else sess.current_values)
        expected_fields = {name: deepcopy(baseline[name]) for name in changed_names
                           if name in baseline}
        expected_absent_fields = sorted(name for name in changed_names
                                        if name not in baseline and
                                        (name in edit_field_names or name == 'picstitle[]' and (carousel_paths or 'gallery_plan' in options)))
        payload = {
            "_login": T.snapshot_login(sess.client, include_headers=True),
            "_verify": sess.client.session.verify,
            "_network": T.network_snapshot(sess.client),
            "site_id": getattr(sess.client, "site_key", ""),
            "article_id": article_id,
            "scode": sess.edit_scode,
            "mcode": edit_mcode,
            "fields": fields,
            "media_uploads": media_uploads,
            "parsed_fields": dict(edit_parsed),
            "image_paths": list(options.get("image_paths") or []),
            "inline_images": inline,
            "remote_images": remote_images,
            "media_assets": media_assets,
            "target_field": target_field,
            "strategy": options.get("strategy", "before_h2"),
            "width_mode": options.get("width_mode", "preserve"),
            "formcheck": sess.edit_formcheck,
            "edit_url_hint": sess.edit_url_hint,
            "upload_page_url": str(
                getattr(sess.client, "_content_edit_page_url", "") or
                sess.edit_url_hint or "").strip(),
            "asset_mimes": dict(options.get("asset_mimes") or {}),
            "strict_browser_upload_parity": True,
            "submitter": deepcopy(options.get("submitter")) if isinstance(options.get("submitter"), dict) else None,
            **thumbnail,
            **({'gallery_plan': deepcopy(options['gallery_plan'])} if 'gallery_plan' in options else {}),
            "carousel_paths": carousel_paths,
            "carousel_size": carousel_size,
            "carousel_mode": ("replace" if options.get("carousel_mode") == "replace"
                              else "append"),
            "carousel_existing_pics": deepcopy(sess.current_values.get("pics", "")),
            "carousel_title_field": carousel_title_field,
            "responsive_context": normalize_responsive_context(
                options.get("responsive_context")),
            "browser_first_image": deepcopy(options.get("browser_first_image"))
                if isinstance(options.get("browser_first_image"), dict) else
                str(options.get("browser_first_image") or ""),
            "carousel_existing_titles": sess.current_values.get(
                carousel_title_field, []) if carousel_title_field else [],
            "image_replacements": image_replacements,
            "expected_content_hash": expected_content_hash,
            "refresh_publish_date": bool(options.get("refresh_publish_date", False)),
            "expected_fields": expected_fields,
            "thumbnail_source_content": str(fields.get(target_field, sess.current_values.get(target_field, '')) or '')
                if thumbnail['thumbnail_from_first'] else '',
            "expected_absent_fields": expected_absent_fields,
            "upload_cache": {},
        }
        self._start_task(sess, T.run_edit, payload, "edit_done")
        return _ok(started=True, inline_count=len(inline))

    # ═══════════════════════════════════════════════════════════
    #  查询修改（产品型号/价格）
    # ═══════════════════════════════════════════════════════════
    @_guard
    def load_products_cache(self, tab_id):
        sess = self._session(tab_id)
        site = getattr(sess.client, "site_key", "")
        products = db_load_products(site)
        sess.products = products
        return _ok(products=products, count=len(products),
                   cached=db_count(site), health=db_product_health(site))

    @_guard
    def query_products_remote(self, tab_id, keyword="", page=1, mcode=""):
        """Read the product list using the backend's own search controls.

        This is intentionally separate from the local SQLite cache and from
        the synchronisation writers: a user asking the web list a question
        must not silently replace or reconcile the cached catalogue.
        """
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        try:
            value = str(mcode or "").strip()
            if value and not value.isdigit():
                return _err("产品模型 mcode 无效")
            result = sess.client.query_products_remote(
                keyword=keyword, page=page, mcode=value or None)
            return _ok(**(result or {}))
        except Exception as exc:
            # Keep the exact list URL available when a site builds its search
            # or pagination entirely in JavaScript.  The desktop adapter must
            # not guess those callbacks; the UI can open this same-origin page
            # in the authenticated native browser instead of presenting a
            # misleading empty/failed result.
            fallback_url = ""
            try:
                resolved = value or sess.client._discover_product_mcode()
                if resolved:
                    fallback_url = sess.client._url(
                        f"Content/index/mcode/{resolved}")
            except Exception:
                fallback_url = ""
            return _err(str(exc), native_url=fallback_url,
                        native_only=bool(fallback_url))

    @_guard
    def product_health(self, tab_id):
        """Return product-cache health without starting a network request."""
        sess = self._session(tab_id)
        site = getattr(sess.client, "site_key", "")
        return _ok(health=db_product_health(site))

    @_guard
    def list_product_models(self, tab_id):
        """Read product-model choices so ambiguous sites can be selected explicitly."""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        try:
            return _ok(models=sess.client.list_product_models())
        except Exception as exc:
            return _err(str(exc))

    @_guard
    def filter_products_cache(self, tab_id, missing_fields=None,
                              product_ids=None):
        """Select cache rows by missing health fields and/or explicit IDs."""
        sess = self._session(tab_id)
        site = getattr(sess.client, "site_key", "")
        products = db_filter_products(
            site, ids=product_ids, missing_fields=missing_fields)
        return _ok(products=products, count=len(products),
                   health=db_product_health(site))

    def _begin_product_sync(self, sess, options, *, done_event,
                            task_kind="product_sync"):
        """Start a read-only remote sync and atomically commit its result."""
        mode = str((options or {}).get("mode", "incremental") or
                   "incremental").strip().lower()
        if mode not in ("full", "incremental", "repair"):
            return _err(f"未知产品同步模式: {mode}")
        raw_ids = (options or {}).get("product_ids")
        if raw_ids is not None and not isinstance(raw_ids, (list, tuple)):
            return _err("product_ids 必须是数组")
        product_ids = []
        seen = set()
        for value in raw_ids or ():
            pid = str(value or "").strip()
            if pid and pid not in seen:
                seen.add(pid)
                product_ids.append(pid)
        # Do not impose a desktop-only count cap here.  The web backend lets
        # the caller choose any number of rows (subject to its own paging and
        # rate limits); the worker already streams detail reads through its
        # bounded executor.  Rejecting the 1001st ID in the desktop bridge
        # created a behavior that could never occur in the native page.
        raw_fields = (options or {}).get("fields")
        if raw_fields is not None and not isinstance(raw_fields, (list, tuple)):
            return _err("fields 必须是数组")
        fields = list(raw_fields) if raw_fields is not None else None
        mcode = str((options or {}).get("mcode", "") or "").strip()
        if mcode and not mcode.isdigit():
            return _err("产品模型 mcode 无效")

        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        cached_products = db_load_products(
            getattr(sess.client, "site_key", ""))
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            "product_sync", mode,
            {"mode": mode, "product_ids": product_ids,
             "fields": fields, "mcode": mcode})
        ctx = self._new_ctx(sess, task_kind)
        if sess._closed.is_set():
            ctx.cancel()

        def worker():
            result = {"ok": False, "mode": mode}
            try:
                ctx.check_cancelled()
                result = T.run_product_sync(
                    sess.client, ctx, cached_products=cached_products,
                    mode=mode, product_ids=product_ids, fields=fields,
                    mcode=mcode or None)
                result = _merge_worker_session_result(
                    sess.client.session, result)
                if sess._closed.is_set():
                    ctx.cancel()
                ctx.check_cancelled()
                if result.get("ok"):
                    site = getattr(sess.client, "site_key", "")
                    stats = db_commit_product_sync(
                        site, result.get("products") or [],
                        complete=bool(result.get("complete")), mode=mode,
                        remote_total=result.get("remote_total"),
                        requested=result.get("requested", 0),
                        refreshed=result.get("refreshed", 0),
                        empty_confirmed=bool(result.get("empty_confirmed")))
                    stored_products = db_load_products(site)
                    sess.products = stored_products
                    result.update({
                        "products": stored_products,
                        "count": len(stored_products),
                        "stats": stats,
                        "health": db_product_health(site),
                    })
            except T.Cancelled:
                result = {"ok": False, "mode": mode, "cancelled": True,
                          "msg": "同步已取消，本地缓存未改动"}
            except Exception as exc:
                debug_log(
                    f"[product_sync] tab={sess.tab_id} mode={mode} 异常: {exc}")
                result = {"ok": False, "mode": mode, "msg": str(exc)}
            finally:
                operation_journal.finish(
                    journal_id, _journal_state(result), result,
                    str(result.get("msg", "") or ""))
                sess._busy = False
                if sess._task_ctx is ctx:
                    sess._task_ctx = None
            if not sess._closed.is_set():
                payload = dict(result)
                payload["tab_id"] = sess.tab_id
                _push(done_event, payload)

        threading.Thread(target=worker, daemon=True).start()
        return _ok(started=True, mode=mode)

    @_guard
    def product_sync(self, tab_id, options=None):
        """Start incremental/full/repair sync; completes via product_sync_done."""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            return _err("同步选项必须是对象")
        return self._begin_product_sync(
            sess, options, done_event="product_sync_done",
            task_kind="product_sync")

    @_guard
    def pull_products(self, tab_id):
        """后台全量拉取产品（列表 + 型号价格），完整成功才落库。"""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._begin_product_sync(
            sess, {"mode": "full"}, done_event="pull_done",
            task_kind="query")

    def _cached_product(self, sess, product_id):
        product_id = str(product_id or "").strip()
        products = sess.products or db_load_products(
            getattr(sess.client, "site_key", ""))
        return next((item for item in products
                     if str(item.get("id", "")) == product_id), None)

    @_guard
    def prepare_product_advanced_edit(self, tab_id, product_id):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if sess._busy:
            return _err("本站点有任务正在执行，请稍候")
        product = self._cached_product(sess, product_id)
        if not product:
            return _err("当前缓存中不存在该产品，请先同步最新数据")
        mcode = str(product.get("mcode", "") or "").strip()
        if not mcode:
            return _err("该产品缺少 mcode，请先重新同步")
        edit_url = str(product.get("edit_url", "") or "").strip()
        try:
            _formcheck, fields, current = sess.client.get_edit_form(
                str(product_id), mcode, edit_url=edit_url)
        except Exception as exc:
            # A product form generated entirely by page JavaScript cannot be
            # safely reconstructed from a static GET.  Keep the server-
            # discovered edit URL so the exact browser page remains an
            # immediate, authenticated fallback instead of a guessed route.
            native_url = _resolve_native_page_url(
                sess.client,
                getattr(sess.client, "_content_edit_native_url", "") or edit_url,
                f"Content/mod/mcode/{mcode}/id/{product_id}")
            return _err(
                f"读取产品后台动态编辑表单失败：{exc}；请使用原生网页完成该操作",
                native_url=native_url, native_only=bool(native_url),
                native_reason=str(
                    getattr(sess.client, "_content_edit_native_reason", "") or
                    exc))
        # ``get_edit_form`` deliberately returns an empty field set when its
        # static safety check detects runtime-generated controls, while
        # retaining the exact resolved page in the client.  Do not let the
        # product bridge collapse that useful native fallback into the generic
        # "no editable fields" error.
        native_url = _resolve_native_page_url(
            sess.client,
            getattr(sess.client, "_content_edit_native_url", "") or edit_url,
            f"Content/mod/mcode/{mcode}/id/{product_id}")
        native_reason = str(
            getattr(sess.client, "_content_edit_native_reason", "") or "").strip()
        if native_url and not fields and not current:
            return _err(
                (native_reason or "产品编辑表单由网页脚本动态生成") +
                "；请使用原生网页完成该操作",
                native_url=native_url, native_only=True,
                native_reason=native_reason)
        advanced = _advanced_product_fields(fields, current)
        if not advanced:
            return _err("后台编辑表单没有可安全开放的产品字段；请使用原生网页完成该操作",
                        native_url=native_url, native_only=bool(native_url),
                        native_reason=native_reason)
        return _ok(
            product_id=str(product_id), title=str(product.get("title", "") or ""),
            fields=_public_form_fields(advanced),
            revision=_product_form_revision(product_id, mcode, advanced),
            warnings=["正文、图集、缩略图和栏目字段请使用“编辑修改”功能处理；当前表单中的普通 file 字段会先按网页策略上传再保存。"])

    @_guard
    def update_product_advanced(self, tab_id, product_id, values, revision=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if not isinstance(values, dict):
            return _err("产品字段数据无效")
        product = self._cached_product(sess, product_id)
        if not product:
            return _err("当前缓存中不存在该产品，请先同步最新数据")
        mcode = str(product.get("mcode", "") or "").strip()
        if not mcode:
            return _err("该产品缺少 mcode，请先重新同步")
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        site = getattr(sess.client, "site_key", "")
        write_ctx = T.TaskContext()
        previous_cancel = getattr(sess.client, "_active_cancel_callback", None)
        sess._task_ctx = write_ctx
        sess.client._active_cancel_callback = write_ctx.check_cancelled
        upload_metadata = []
        upload_journal_id = ""
        try:
            write_ctx.check_cancelled()
            try:
                _formcheck, fields, current = sess.client.get_edit_form(
                    str(product_id), mcode,
                    edit_url=str(product.get("edit_url", "") or ""))
            except Exception as exc:
                # A dynamic page may raise instead of returning the empty
                # ``(formcheck, fields, current)`` sentinel.  Preserve the
                # exact URL discovered by the client so the UI can open the
                # authenticated native editor rather than exposing a generic
                # error or retrying a guessed POST.
                native_url = _resolve_native_page_url(
                    sess.client,
                    getattr(sess.client, "_content_edit_native_url", "") or
                    product.get("edit_url", ""),
                    f"Content/mod/mcode/{mcode}/id/{product_id}")
                native_reason = str(
                    getattr(sess.client, "_content_edit_native_reason", "") or
                    exc).strip()
                return _err(
                    native_reason + "；产品未提交修改，请使用原生网页完成该操作",
                    native_url=native_url, native_only=bool(native_url),
                    native_reason=native_reason)
            native_url = _resolve_native_page_url(
                sess.client,
                getattr(sess.client, "_content_edit_native_url", "") or
                product.get("edit_url", ""),
                f"Content/mod/mcode/{mcode}/id/{product_id}")
            native_reason = str(
                getattr(sess.client, "_content_edit_native_reason", "") or
                "").strip()
            if native_url and not fields and not current:
                reason = (native_reason or "产品编辑表单由网页脚本动态生成") + \
                    "；请使用原生网页完成该操作"
                if upload_journal_id:
                    operation_journal.finish(
                        upload_journal_id, "failed",
                        {"ok": False, "msg": reason,
                         "native_url": native_url, "native_only": True,
                         "native_reason": native_reason}, reason)
                    upload_journal_id = ""
                return _err(reason, native_url=native_url, native_only=True,
                            native_reason=native_reason)
            advanced = _advanced_product_fields(fields, current)
            current_revision = _product_form_revision(product_id, mcode, advanced)
            if not revision or str(revision) != current_revision:
                return _err("产品后台字段已变化，请重新打开高级编辑器")
            descriptors = {field["name"]: field for field in advanced}
            unknown = set(values) - set(descriptors)
            if unknown:
                return _err("包含后台未允许的产品字段：" + "、".join(sorted(unknown)))
            normalized, errors = _validate_form_overrides(values, advanced)
            if errors:
                return _err("；".join(errors))
            # Native file inputs are not successful controls until the page's
            # upload script has returned a server URL.  Product advanced
            # editing used to reject/exclude these fields entirely, unlike
            # the normal publish/edit flow.  Discover each field's real
            # policy from the current edit page and upload selected files
            # before constructing the content POST; never leak local paths.
            file_fields = {
                name: field for name, field in descriptors.items()
                if str(field.get("type", field.get("kind", ""))).lower() == "file"
                and name in normalized
            }
            if file_fields:
                upload_journal_id = operation_journal.begin(
                    site, sess.tab_id, "product_advanced_upload",
                    str(product_id),
                    {"field_names": sorted(file_fields)})
                edit_page = (str(product.get("edit_url", "") or "").strip()
                             or sess.client._url(
                                 f"Content/mod/id/{product_id}/mcode/{mcode}"))
                try:
                    sess.client.prepare_uploads(
                        edit_page, [("field", name) for name in file_fields])
                except Exception as exc:
                    from upload_policy import UploadPolicyError
                    if not isinstance(exc, UploadPolicyError):
                        raise
                    reason = f"产品字段上传策略无法安全复刻：{exc}"
                    operation_journal.finish(
                        upload_journal_id, "failed",
                        {"ok": False, "msg": reason, "native_url": edit_page,
                         "native_only": True, "native_reason": reason}, reason)
                    return _err(reason + "，已切换原生网页完成上传和保存",
                                native_url=edit_page, native_only=True,
                                native_reason=reason)
                apply_policy = getattr(sess.client, "apply_upload_policy_metadata", None)
                if callable(apply_policy):
                    apply_policy(fields)
                # The native product editor can expose several independent
                # file controls (for example a cover image and an attachment)
                # on the same page.  A browser starts each selected file's
                # XHR as its control is handled, so these controls may be in
                # flight together.  Reuse the shared queue adapter whenever
                # the complete policy snapshot and all local paths are
                # available; retain the legacy single-file loop for tiny
                # test/integration shims that intentionally do not expose a
                # policy store.  This removes a real desktop-only serial
                # bottleneck without weakening fail-closed policy discovery.
                all_product_paths = []
                for _name, _field in file_fields.items():
                    _raw = normalized.get(_name)
                    _paths = _raw if isinstance(_raw, (list, tuple)) else [_raw]
                    all_product_paths.extend(
                        os.path.abspath(str(_path or "").strip())
                        for _path in _paths if str(_path or "").strip())
                product_policies = getattr(sess.client, "_upload_policies", {}) or {}
                can_parallel_product_upload = (
                    len(all_product_paths) > 1 and
                    all(os.path.isfile(path) for path in all_product_paths) and
                    all(("field", name) in product_policies
                        for name in file_fields))
                if can_parallel_product_upload:
                    queues = []
                    for name, field in file_fields.items():
                        raw = normalized.get(name)
                        paths = raw if isinstance(raw, (list, tuple)) else [raw]
                        paths = [os.path.abspath(str(path or "").strip())
                                 for path in paths if str(path or "").strip()]
                        if not paths:
                            continue
                        queues.append({
                            "key": name, "name": name, "field": field,
                            "paths": paths, "upload_target": name,
                            "label": field.get("label") or name,
                            "media_kind": str(field.get("media_kind") or "file")})
                    try:
                        uploaded = T.upload_field_queues(
                            sess.client, queues,
                            formcheck=str(_formcheck or ""),
                            cancel_callback=write_ctx.check_cancelled)
                    except NativeUploadRequired as exc:
                        reason = str(exc.reason or exc)
                        fallback_url = (str(exc.native_url or "").strip()
                                        or edit_page)
                        message = reason + "，已切换原生网页完成上传和保存"
                        operation_journal.finish(
                            upload_journal_id, "failed",
                            {"ok": False, "msg": message,
                             "native_url": fallback_url,
                             "native_only": True,
                             "native_reason": reason}, message)
                        upload_journal_id = ""
                        return _err(message, native_url=fallback_url,
                                    native_only=True,
                                    native_reason=reason)
                    except T.UploadOutcomeUnknown as exc:
                        message = (f"产品字段上传结果未知，文件可能已保存：{exc}")
                        operation_journal.finish(
                            upload_journal_id, "review",
                            {"ok": False, "msg": message,
                             "requires_review": True, "retryable": False},
                            message)
                        return _err(message, requires_review=True,
                                    retryable=False, outcome="unknown",
                                    operation_id=upload_journal_id)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        message = f"产品字段上传失败：{exc}"
                        operation_journal.finish(
                            upload_journal_id, "failed",
                            {"ok": False, "msg": message}, message)
                        return _err(message, operation_id=upload_journal_id)
                    for name, result in uploaded.items():
                        urls = list(result.get("urls") or [])
                        upload_metadata.extend(result.get("metadata") or [])
                        field = file_fields[name]
                        normalized[name] = urls if field.get("multiple") else urls[-1]
                    # Skip the serial compatibility loop below; the journal
                    # completion and ID cleanup remain shared with it.
                    file_fields = {}
                for name, field in file_fields.items():
                    raw = normalized.get(name)
                    paths = raw if isinstance(raw, (list, tuple)) else [raw]
                    try:
                        queue_limit = int(field.get("max_files") or 0)
                    except (TypeError, ValueError):
                        queue_limit = 0
                    if queue_limit > 0 and len(paths) > queue_limit:
                        message = f"{field.get('label') or name}最多选择 {queue_limit} 个文件"
                        operation_journal.finish(upload_journal_id, "failed",
                                                 {"ok": False, "msg": message}, message)
                        return _err(message)
                    urls = []
                    for path in paths:
                        local_path = os.path.abspath(str(path or "").strip())
                        if not local_path or not os.path.isfile(local_path):
                            message = f"{field.get('label') or name}选择的文件不存在"
                            operation_journal.finish(upload_journal_id, "failed",
                                                     {"ok": False, "msg": message}, message)
                            return _err(message)
                        # A product model may expose ordinary attachments,
                        # PDFs, videos or audio through a native ``type=file``
                        # field.  The browser sends those through the field's
                        # generic uploader; routing every product file through
                        # ``upload_image`` silently changed MIME detection and
                        # could reject valid non-image files.  Reuse the same
                        # accept-aware classifier as dynamic modules,
                        # categories, slides and single pages.
                        if upload_field_is_image(field, local_path):
                            url, error = sess.client.upload_image(
                                local_path, _formcheck,
                                upload_surface="field", upload_target=name)
                        else:
                            url, error = sess.client.upload_file(
                                local_path, _formcheck,
                                upload_surface="field", upload_target=name,
                                media_kind="file")
                        if not url:
                            metadata = dict(getattr(
                                sess.client, "last_upload_result", {}) or {})
                            if metadata.get("outcome") == "native_only":
                                reason = str(error or metadata.get(
                                    "native_reason") or
                                    f"{field.get('label') or name}必须由认证原生网页处理")
                                policy = (getattr(sess.client, "_upload_policies", {})
                                          or {}).get(("field", name))
                                fallback_url = str(
                                    getattr(policy, "page_url", "") or edit_page)
                                message = reason + "，已切换原生网页完成上传和保存"
                                operation_journal.finish(
                                    upload_journal_id, "failed",
                                    {"ok": False, "msg": message,
                                     "native_url": fallback_url,
                                     "native_only": True,
                                     "native_reason": reason}, message)
                                upload_journal_id = ""
                                return _err(message, native_url=fallback_url,
                                            native_only=True,
                                            native_reason=reason)
                            message = (f"{field.get('label') or name}上传失败："
                                       f"{error or '未知错误'}")
                            if metadata.get("outcome") == "unknown":
                                message += "；上传结果未知，请先核对后台，未自动重试"
                            state = _journal_state(metadata)
                            operation_journal.finish(
                                upload_journal_id, state,
                                {"ok": False, "msg": message,
                                 "upload_result": metadata},
                                message)
                            return _err(message, upload_result=metadata,
                                        requires_review=(state == "review"),
                                        outcome=metadata.get("outcome", ""),
                                        operation_id=upload_journal_id)
                        from client_content import upload_result_entry
                        upload_metadata.append(upload_result_entry(
                            getattr(sess.client, "last_upload_result", {}),
                            field.get("label") or name,
                            os.path.basename(local_path)))
                        urls.append(url)
                    normalized[name] = urls if field.get("multiple") else urls[-1]
                operation_journal.finish(
                    upload_journal_id, "success",
                    {"ok": True, "upload_metadata": upload_metadata},
                    "文件上传完成")
                upload_journal_id = ""
            changes, expected = {}, {}
            for name, value in normalized.items():
                field = descriptors[name]
                old_value = current.get(name, [])
                if value != normalize_control_value(old_value, field):
                    changes[name] = value
                    expected[name] = current.get(name, "")
            if not changes:
                return _ok(changed=False, msg="产品字段没有变化",
                           upload_metadata=upload_metadata)
            ok, message = self._journal_call(
                sess, "product_advanced_update", str(product_id),
                {"before": expected, "after": changes},
                lambda: sess.client.edit_content(
                    str(product_id), mcode, changes,
                    edit_url_hint=str(product.get("edit_url", "") or ""),
                    expected_fields=expected))
            write_metadata = dict(getattr(sess.client, "last_write_result", {}) or {})
            write_review = bool(
                write_metadata.get("requires_review") or
                str(write_metadata.get("outcome", "")).lower() in
                ("unknown", "different", "reported_unverified"))
            record_audit(
                site, "product_advanced_update", "product", product_id,
                status=("success" if ok else "review" if write_review else "failed"),
                before=expected,
                after=changes, message=message)
            if not ok:
                return _err(message, **write_metadata,
                            requires_review=write_review)
            if "title" in changes:
                product["title"] = changes["title"]
            if product.get("xinghao_field") in changes:
                product["xinghao"] = changes[product.get("xinghao_field")]
            if product.get("jiage_field") in changes:
                product["jiage"] = changes[product.get("jiage_field")]
            # A filename/URL-name or category change can alter the site's
            # rewrite route.  The cached front URL is no longer evidence of
            # the new address; clear it until the next read-only URL recovery
            # or full product sync rather than displaying a stale link.
            front_url_invalidated = bool(
                {"filename", "urlname", "scode"}.intersection(changes))
            if front_url_invalidated:
                product["front_url"] = ""
                db_patch_products(site, [{"id": str(product_id),
                                          "front_url": ""}])
                message = (str(message or "产品已保存") +
                           "；URL名称或栏目已变化，旧前台链接已清除，请重新同步确认")
            db_upsert_products(site, [product])
            sess.products = db_load_products(site)
            return _ok(changed=True, msg=message, products=sess.products,
                       health=db_product_health(site),
                       front_url_invalidated=front_url_invalidated,
                       upload_metadata=upload_metadata)
        except Cancelled:
            # Keep product advanced-edit cancellation consistent with article
            # and dynamic-module writes.  A completed upload cannot be
            # rolled back by the desktop bridge, so expose a durable review
            # warning instead of presenting a clean retryable failure.
            if upload_journal_id:
                operation_journal.finish(
                    upload_journal_id, "review" if upload_metadata else "cancelled",
                    {"ok": False, "cancelled": True,
                     "requires_review": bool(upload_metadata),
                     "upload_metadata": upload_metadata},
                    "产品高级编辑已取消")
            if upload_metadata:
                return _err(
                    "产品高级编辑已取消；部分上传可能已完成，请先核对后台，软件不会自动重试",
                    cancelled=True, outcome="cancelled", requires_review=True,
                    retryable=False, upload_metadata=upload_metadata)
            return _err("产品高级编辑已取消，未继续提交", cancelled=True,
                        outcome="cancelled", requires_review=False,
                        retryable=False)
        finally:
            if previous_cancel is None:
                try:
                    delattr(sess.client, "_active_cancel_callback")
                except AttributeError:
                    pass
            else:
                sess.client._active_cancel_callback = previous_cancel
            if sess._task_ctx is write_ctx:
                sess._task_ctx = None
            with sess._lock:
                sess._busy = False

    @_guard
    def modify_product(self, tab_id, pid, new_model=None, new_price=None):
        """None leaves a value alone; an explicit empty string clears it."""
        sess = self._session(tab_id)
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        try:
            return self._modify_product(tab_id, pid, new_model, new_price)
        finally:
            with sess._lock:
                sess._busy = False

    def _modify_product(self, tab_id, pid, new_model=None, new_price=None):
        """改单个产品型号/价格（提交前抓全字段当前值，保住 status 等未改字段）。"""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        pid = str(pid or "").strip()
        if not pid:
            return _err("缺少产品 ID")
        if any(value is not None and not isinstance(value, (str, int, float))
               for value in (new_model, new_price)):
            return _err("型号和价格必须是文本或数字")
        new_model = str(new_model) if new_model is not None else None
        new_price = str(new_price) if new_price is not None else None
        if new_model is None and new_price is None:
            return _err("型号与价格都未填写，无需修改")
        model_field, price_field = "ext_xinghao", "ext_jiage"
        product_mcode, product_edit_url = "", ""
        matched_product = None
        for product in sess.products:
            if str(product.get("id")) == pid:
                matched_product = product
                model_field = product.get("xinghao_field", "")
                price_field = product.get("jiage_field", "")
                product_mcode = str(product.get("mcode", "") or "").strip()
                product_edit_url = str(product.get("edit_url", "") or "").strip()
                break
        if matched_product is None:
            return _err("当前缓存中不存在该产品，请先同步最新数据")
        if not product_mcode:
            return _err("该产品缺少 mcode，请先重新同步后再修改")
        if new_model is not None and not model_field:
            return _err("当前网站产品模型没有型号字段，无法修改型号")
        if new_price is not None and not price_field:
            return _err("当前网站产品模型没有价格字段，无法修改价格")
        changes, expected = {}, {}
        if new_model is not None:
            changes["xinghao"] = new_model
            expected["xinghao"] = str(matched_product.get("xinghao") if matched_product.get("xinghao") is not None else "")
        if new_price is not None:
            changes["jiage"] = new_price
            expected["jiage"] = str(matched_product.get("jiage") if matched_product.get("jiage") is not None else "")
        site = getattr(sess.client, "site_key", "")
        # 单行修改也必须和批量修改一样，在真正 POST 前对比后台最新值；
        # 否则查询表停留几分钟后可能覆盖另一位管理员刚保存的型号或价格。
        sess.client.last_write_result = {}
        ok, msg = self._journal_call(
            sess, "product_update", pid,
            {"before": expected, "after": changes},
            lambda: sess.client.modify_product_fields(
                pid, changes,
                xinghao_field=model_field, jiage_field=price_field,
                mcode=product_mcode or None, edit_url=product_edit_url or None,
                expected=expected))
        metadata = dict(getattr(sess.client, "last_write_result", {}) or {})
        if not ok:
            record_audit(site, "product_update", "product", pid,
                         status="review" if metadata.get("requires_review") else "failed", before=expected, after=changes,
                         message=msg)
            return _err(msg, **metadata)
        for product in sess.products:
            if str(product.get("id")) == pid:
                if new_model is not None:
                    product["xinghao"] = new_model
                if new_price is not None:
                    product["jiage"] = new_price
                db_upsert_products(site, [product])
                break
        record_audit(site, "product_update", "product", pid,
                     before=expected, after=changes, message=msg)
        return _ok(msg=msg, health=db_product_health(site), **metadata)

    @_guard
    def bulk_modify_products(self, tab_id, changes, continue_on_error=False):
        """Validate an entire product batch before starting any remote write.

        Each item is ``{id, model?, price?, expected_model?, expected_price?}``.
        ``xinghao``/``jiage`` aliases are accepted.  Presence of a key means
        intent, so an empty value deliberately clears that field.  Optional
        expected values provide optimistic concurrency protection against a
        stale grid editing newer remote/cache data by accident.
        """
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if not isinstance(changes, (list, tuple)):
            return _err("批量修改内容必须是数组")
        changes = list(changes)
        if not changes:
            return _err("没有待修改的产品")
        products = sess.products or db_load_products(
            getattr(sess.client, "site_key", ""))
        by_id = {str(item.get("id", "") or "").strip(): item
                 for item in products}
        allowed = {"id", "model", "xinghao", "price", "jiage",
                   "expected_model", "expected_xinghao",
                   "expected_price", "expected_jiage"}
        prepared = []
        unchanged_ids = []
        seen = set()
        for index, item in enumerate(changes, 1):
            if not isinstance(item, dict):
                return _err(f"第 {index} 条修改不是对象")
            unknown = set(item) - allowed
            if unknown:
                return _err(
                    f"第 {index} 条包含不允许的字段：" +
                    "、".join(sorted(unknown)))
            pid = str(item.get("id", "") or "").strip()
            if not pid:
                return _err(f"第 {index} 条缺少产品 ID")
            if pid in seen:
                return _err(f"批量修改包含重复产品 ID：{pid}")
            seen.add(pid)
            product = by_id.get(pid)
            if product is None:
                return _err(f"当前缓存中不存在产品 {pid}，请先同步最新数据")
            if "model" in item and "xinghao" in item:
                return _err(f"产品 {pid} 同时提供 model 与 xinghao，请只保留一个")
            if "price" in item and "jiage" in item:
                return _err(f"产品 {pid} 同时提供 price 与 jiage，请只保留一个")
            if "expected_model" in item and "expected_xinghao" in item:
                return _err(
                    f"产品 {pid} 同时提供 expected_model 与 expected_xinghao")
            if "expected_price" in item and "expected_jiage" in item:
                return _err(
                    f"产品 {pid} 同时提供 expected_price 与 expected_jiage")
            logical = {}
            if "model" in item or "xinghao" in item:
                key = "model" if "model" in item else "xinghao"
                logical["xinghao"] = str(item.get(key, "") or "")
            if "price" in item or "jiage" in item:
                key = "price" if "price" in item else "jiage"
                logical["jiage"] = str(item.get(key, "") or "")
            if not logical:
                return _err(f"产品 {pid} 没有待修改字段")

            expected_model_key = ("expected_model" if "expected_model" in item
                                  else "expected_xinghao"
                                  if "expected_xinghao" in item else "")
            expected_price_key = ("expected_price" if "expected_price" in item
                                  else "expected_jiage"
                                  if "expected_jiage" in item else "")
            if (expected_model_key and
                    str(item.get(expected_model_key, "") or "") !=
                    str(product.get("xinghao", "") or "")):
                return _err(
                    f"产品 {pid} 的型号已变化，请刷新后重新确认批量修改")
            if (expected_price_key and
                    str(item.get(expected_price_key, "") or "") !=
                    str(product.get("jiage", "") or "")):
                return _err(
                    f"产品 {pid} 的价格已变化，请刷新后重新确认批量修改")
            if "xinghao" in logical and not str(
                    product.get("xinghao_field", "") or "").strip():
                return _err(f"产品 {pid} 所在模型没有型号字段")
            if "jiage" in logical and not str(
                    product.get("jiage_field", "") or "").strip():
                return _err(f"产品 {pid} 所在模型没有价格字段")
            mcode = str(product.get("mcode", "") or "").strip()
            if not mcode:
                return _err(f"产品 {pid} 缺少 mcode，请先重新同步")
            effective = {
                key: value for key, value in logical.items()
                if value != str(product.get(key, "") or "")
            }
            if not effective:
                unchanged_ids.append(pid)
                continue
            prepared.append({
                "id": pid, "changes": effective,
                "expected": {key: str(product.get(key, "") or "")
                             for key in effective},
                "xinghao_field": str(product.get("xinghao_field", "") or ""),
                "jiage_field": str(product.get("jiage_field", "") or ""),
                "mcode": mcode,
                "edit_url": str(product.get("edit_url", "") or ""),
            })
        if not prepared:
            return _ok(started=False, unchanged_ids=unchanged_ids,
                       msg="所选产品的值没有变化",
                       health=db_product_health(
                           getattr(sess.client, "site_key", "")))

        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            "product_bulk_update", "batch",
            {"count": len(prepared), "continue_on_error": bool(continue_on_error),
             "ids": [item.get("id") for item in prepared]})
        ctx = self._new_ctx(sess, "product_bulk_modify")
        if sess._closed.is_set():
            ctx.cancel()

        def worker():
            result = {"ok": False, "msg": "批量修改未完成",
                      "succeeded": [], "failed": [],
                      "not_attempted": []}
            try:
                result = T.run_bulk_product_modifications(
                    sess.client, {"items": prepared,
                                  "continue_on_error": bool(continue_on_error)},
                    ctx)
                result = _merge_worker_session_result(
                    sess.client.session, result)
                succeeded = list(result.get("succeeded") or [])
                if succeeded:
                    patches = [dict(row.get("changes") or {}, id=row.get("id"))
                               for row in succeeded]
                    try:
                        result["cache_stats"] = db_patch_products(
                            getattr(sess.client, "site_key", ""), patches)
                    except Exception as cache_exc:
                        # Remote writes already succeeded and must never be
                        # reported as failures (which could prompt a dangerous
                        # duplicate retry).  Surface an explicit reload warning.
                        result["cache_warning"] = (
                            "后台修改已成功，但本地缓存更新失败，请重新同步：" +
                            str(cache_exc))
                prepared_by_id = {str(item.get("id", "")): item
                                  for item in prepared}
                audit_site = getattr(sess.client, "site_key", "")
                for row in succeeded:
                    item = prepared_by_id.get(str(row.get("id", "")), {})
                    record_audit(
                        audit_site, "product_bulk_update", "product",
                        row.get("id", ""), before=item.get("expected", {}),
                        after=row.get("changes", {}), message=row.get("msg", ""))
                for row in result.get("failed", []) or []:
                    item = prepared_by_id.get(str(row.get("id", "")), {})
                    record_audit(
                        audit_site, "product_bulk_update", "product",
                        row.get("id", ""),
                        status=("review" if row.get("requires_review") or
                                str(row.get("outcome", "")).lower() in
                                ("unknown", "different", "reported_unverified")
                                else "failed"),
                        before=item.get("expected", {}),
                        after=item.get("changes", {}), message=row.get("msg", ""))
                site = getattr(sess.client, "site_key", "")
                sess.products = db_load_products(site)
                result.update({"products": sess.products,
                               "count": len(sess.products),
                               "health": db_product_health(site),
                               "unchanged_ids": unchanged_ids})
            except T.Cancelled:
                result.update({"cancelled": True,
                               "msg": "批量修改已取消"})
            except Exception as exc:
                debug_log(
                    f"[bulk_modify_products] tab={sess.tab_id} 异常: {exc}")
                result.update({"ok": False, "msg": str(exc)})
            finally:
                operation_journal.finish(
                    journal_id, _journal_state(result), result,
                    str(result.get("msg", "") or ""))
                sess._busy = False
                if sess._task_ctx is ctx:
                    sess._task_ctx = None
            if not sess._closed.is_set():
                payload = dict(result)
                payload["tab_id"] = sess.tab_id
                _push("product_bulk_modify_done", payload)

        threading.Thread(target=worker, daemon=True).start()
        return _ok(started=True, count=len(prepared),
                   unchanged_ids=unchanged_ids)

    @_guard
    def clear_product_cache(self, tab_id):
        sess = self._session(tab_id)
        db_clear_site(getattr(sess.client, "site_key", ""))
        sess.products = []
        return _ok()

    @_guard
    def diagnose_backend_structure(self, tab_id):
        """只读扫描当前站点后台结构并生成脱敏 JSON 报告。"""
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录再运行后台结构诊断")
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        payload = {"_login": T.snapshot_login(sess.client, include_headers=True),
                   "_verify": sess.client.session.verify,
                   "_network": T.network_snapshot(sess.client)}
        self._start_task(sess, T.run_backend_diagnostic, payload,
                         "diagnostic_done")
        return _ok(started=True)

    # ═══════════════════════════════════════════════════════════
    #  留言
    # ═══════════════════════════════════════════════════════════
    @_guard
    def load_messages(self, tab_id, limit=50, keyword="", status=""):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        try:
            limit = None if limit is None or int(limit) == 0 else max(1, int(limit))
        except (TypeError, ValueError):
            limit = 50
        messages = sess.client.fetch_messages(
            limit=limit, keyword=str(keyword or ""), status=str(status or ""))
        return _ok(messages=_public_messages(messages), count=len(messages),
                   complete=bool(getattr(messages, "complete", False)),
                   pages=int(getattr(messages, "pages", 0) or 0),
                   warning=str(getattr(messages, "warning", "") or ""),
                   server_filter=getattr(messages, "server_filter", None),
                   server_filter_query=dict(
                       getattr(messages, "server_filter_query", {}) or {}),
                   filter_warning=str(
                       getattr(messages, "filter_warning", "") or ""),
                   native_url=str(getattr(messages, "native_url", "") or ""))

    def _message_write(self, sess, message_id, action, expected=None):
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍后再操作留言")
            sess._busy = True
        try:
            audit_action = "message_status" if action == "status" else "message_delete"
            try:
                sess.client.last_message_result = {}
                result = self._journal_call(
                    sess, audit_action, str(message_id),
                    {"before": expected or {}, "action": action},
                    lambda: sess.client.message_action(
                        message_id, action, expected=expected or {}))
            except Exception as exc:
                metadata = dict(getattr(sess.client, 'last_message_result', {}) or {})
                record_audit(
                    getattr(sess.client, "site_key", ""), audit_action,
                    "message", message_id, status="review" if metadata.get('requires_review') else "failed",
                    before=expected or {}, message=str(exc))
                if isinstance(exc, Cancelled):
                    attempted = bool(metadata.get("write_attempted"))
                    cancel_result = dict(metadata)
                    cancel_result.update({
                        "cancelled": True, "outcome": "cancelled",
                        "retryable": False, "requires_review": attempted,
                    })
                    return _err(
                        "留言操作已取消；" +
                        ("请求可能已发送，请先核对后台，勿直接重试"
                         if attempted else "未发送请求"),
                        **cancel_result)
                return _err(str(exc), **metadata)
            record_audit(
                getattr(sess.client, "site_key", ""), audit_action,
                "message", message_id, before=expected or {},
                message=result.get("msg", ""))
            result["messages"] = _public_messages(result.get("messages", []))
            return _ok(**result)
        finally:
            with sess._lock:
                sess._busy = False

    @_guard
    def toggle_message_status(self, tab_id, message_id, expected=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._message_write(
            sess, message_id, "status", expected=expected)

    @_guard
    def delete_message(self, tab_id, message_id, expected=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        return self._message_write(
            sess, message_id, "delete", expected=expected)

    @_guard
    def export_messages(self, tab_id, message_ids=None):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        ids = list(message_ids or [])
        path, count, warning = sess.client.export_messages_csv(ids, limit=None)
        return _ok(path=path, count=count, warning=warning)

    @_guard
    def prepare_message_reply(self, tab_id, message_id, revision=''):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err('请先登录')
        with sess._lock:
            if sess._busy:
                return _err('本站点有任务正在执行，请稍候')
            sess._busy = True
        try:
            info = sess.client.prepare_message_reply(message_id, revision)
            info['fields'] = _public_form_fields(info['fields'])
            return _ok(**info)
        finally:
            with sess._lock:
                sess._busy = False

    @_guard
    def save_message_reply(self, tab_id, message_id, values, form_revision='', message_revision=''):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err('请先登录')
        with sess._lock:
            if sess._busy:
                return _err('本站点有任务正在执行，请稍候')
            sess._busy = True
        try:
            result = self._journal_call(
                sess, "message_reply", str(message_id),
                {"field_names": list(values) if isinstance(values, dict) else [],
                 "form_revision": form_revision, "message_revision": message_revision},
                lambda: sess.client.save_message_reply(
                    message_id, values, form_revision, message_revision))
            record_audit(getattr(sess.client, 'site_key', ''), 'message_reply', 'message', message_id,
                         status='success' if result.get('ok') else
                         'cancelled' if result.get('cancelled') else
                         'review' if result.get('requires_review') else 'failed',
                         after={'field_names': list(values) if isinstance(values, dict) else []}, message=result.get('msg', ''))
            if 'messages' in result:
                result['messages'] = _public_messages(result['messages'])
            return result
        finally:
            with sess._lock:
                sess._busy = False

    @_guard
    def bulk_toggle_message_status(self, tab_id, items):
        sess = self._session(tab_id)
        if not sess.client.logged_in:
            return _err("请先登录")
        if not isinstance(items, list) or not items:
            return _err("请选择要切换状态的留言")
        prepared, seen = [], set()
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                return _err(f"第 {index} 条留言参数无效")
            message_id = str(item.get("id", "") or "").strip()
            if not message_id.isdigit() or message_id in seen:
                return _err(f"第 {index} 条留言编号无效或重复")
            seen.add(message_id)
            prepared.append({"id": message_id,
                             "expected": {"name": str(item.get("name", "") or ""),
                                          "time": str(item.get("time", "") or ""),
                                          **({'revision': item['revision']} if 'revision' in item else {})}})
        with sess._lock:
            if sess._busy:
                return _err("本站点有任务正在执行，请稍候")
            sess._busy = True
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), sess.tab_id,
            "message_bulk_status", "batch",
            {"count": len(prepared), "ids": [item.get("id") for item in prepared]})
        ctx = self._new_ctx(sess, "message_bulk")
        login = T.snapshot_login(sess.client, include_headers=True)
        verify = sess.client.session.verify

        def worker():
            succeeded, failed = [], []
            tmp = None
            result = {"ok": False, "succeeded": [], "failed": [],
                      "msg": "批量留言状态操作未完成"}
            try:
                headers = login[2] if len(login) > 2 else None
                tmp = T.build_worker_client(
                    login[0], login[1], verify, headers=headers,
                    network=T.network_snapshot(sess.client))
                # The worker owns the same cooperative cancellation contract
                # as the foreground bridge.  Message actions check this
                # callback before their write and while paging/read-back.
                try:
                    tmp._active_cancel_callback = ctx.check_cancelled
                except Exception:
                    # Test doubles/third-party clients may use slots; the
                    # explicit ``ctx.check_cancelled`` call before each item
                    # still prevents any subsequent request from starting.
                    pass
                total = len(prepared)
                ctx.progress(0, total, f"批量切换留言状态 0/{total}")
                for index, item in enumerate(prepared, 1):
                    ctx.check_cancelled()
                    try:
                        result = tmp.message_action(
                            item["id"], "status", expected=item["expected"])
                        succeeded.append(item["id"])
                        record_audit(
                            getattr(sess.client, "site_key", ""),
                            "message_bulk_status", "message", item["id"],
                            before=item["expected"], message=result.get("msg", ""))
                    except Exception as exc:
                        metadata = dict(getattr(tmp, 'last_message_result', {}) or {})
                        failed.append({"id": item["id"], "msg": str(exc), **metadata})
                        record_audit(
                            getattr(sess.client, "site_key", ""),
                            "message_bulk_status", "message", item["id"],
                            status="review" if metadata.get('requires_review') else "failed", before=item["expected"],
                            message=str(exc))
                        if metadata.get('requires_review'):
                            break
                    ctx.progress(index, total,
                                 f"批量切换留言状态 {index}/{total}")
                snapshot = tmp.fetch_messages(limit=None)
                result = {"ok": not failed, "succeeded": succeeded,
                          "failed": failed, "messages": _public_messages(snapshot),
                          "requires_review": any(row.get('requires_review') for row in failed),
                          "not_attempted": [row['id'] for row in prepared[len(succeeded) + len(failed):]],
                          "complete": bool(getattr(snapshot, "complete", False)),
                          "pages": int(getattr(snapshot, "pages", 0) or 0),
                          "warning": str(getattr(snapshot, "warning", "") or ""),
                          "msg": (f"已完成 {len(succeeded)} 条" +
                                  (f"，失败 {len(failed)} 条" if failed else ""))}
            except T.Cancelled:
                result = {"ok": False, "cancelled": True,
                          "succeeded": succeeded, "failed": failed,
                          "msg": "批量留言状态操作已取消"}
            except Exception as exc:
                result = {"ok": False, "succeeded": succeeded,
                          "failed": failed, "msg": str(exc)}
            finally:
                if tmp is not None:
                    result = _merge_worker_session_result(
                        sess.client.session,
                        T._with_worker_session(tmp, result))
                operation_journal.finish(
                    journal_id, _journal_state(result), result,
                    str(result.get("msg", "") or ""))
                sess._busy = False
                if sess._task_ctx is ctx:
                    sess._task_ctx = None
            if not sess._closed.is_set():
                result['requires_review'] = any(row.get('requires_review') for row in failed)
                result['not_attempted'] = [row['id'] for row in prepared[len(succeeded) + len(failed):]]
                _push("message_bulk_done", dict(result, tab_id=sess.tab_id))

        threading.Thread(target=worker, daemon=True).start()
        return _ok(started=True, count=len(prepared))

    # ═══════════════════════════════════════════════════════════
    #  任务与诊断
    # ═══════════════════════════════════════════════════════════
    def _new_ctx(self, sess, task_kind=""):
        tab_id = sess.tab_id
        def progress(done, total, current, bytes_done=0, bytes_total=0):
            if not sess._closed.is_set():
                payload = {"tab_id": tab_id, "done": done,
                           "total": total, "current": current,
                           "task": task_kind}
                if bytes_total:
                    payload.update({"bytes_done": max(0, int(bytes_done or 0)),
                                    "bytes_total": max(0, int(bytes_total or 0))})
                _push("progress", payload)

        def log(message):
            if not sess._closed.is_set():
                _push("log", {"tab_id": tab_id, "msg": message})

        def upload(entry):
            if not sess._closed.is_set() and isinstance(entry, dict):
                # Keep this event intentionally separate from the final
                # publish/edit result: the UI can render server dimensions and
                # processing metadata as soon as each upload callback returns,
                # matching the browser's immediate upload preview.  The worker
                # completion payload remains authoritative for retries/drafts.
                _push("upload_metadata", {"tab_id": tab_id,
                                           "task": task_kind,
                                           "entry": entry})

        ctx = T.TaskContext(
            on_progress=progress, on_log=log, on_upload=upload)
        sess._task_ctx = ctx
        return ctx

    def _start_task(self, sess, runner, payload, done_event, **extra):
        tab_id = sess.tab_id
        task_kind = {"publish_done": "publish", "edit_done": "edit",
                     "diagnostic_done": "diagnostic"}.get(done_event, "")
        journal_id = operation_journal.begin(
            getattr(sess.client, "site_key", ""), tab_id,
            task_kind or done_event,
            str(payload.get("article_id", payload.get("scode", "")) or ""),
            {"workflow": task_kind,
             "payload_keys": sorted(str(key) for key in payload.keys())})
        sess._journal_op_id = journal_id
        # 同步创建并挂载 ctx，close_tab/cancel_task 从线程启动前起就能取消；
        # 旧版在线程内部创建，存在“标签已关但真实发布仍启动”的竞态窗口。
        ctx = self._new_ctx(sess, task_kind)
        if sess._closed.is_set():
            ctx.cancel()

        def worker():
            result = {}
            try:
                ctx.check_cancelled()
                result = runner(sess.client, payload, ctx)
            except T.Cancelled:
                result = {"ok": False, "cancelled": True, "msg": "任务已取消"}
            except Exception as exc:
                debug_log(f"[task] tab={tab_id} {done_event} 异常: {exc}")
                result = {"ok": False, "msg": str(exc)}
            finally:
                # The worker owns a cookie-jar copy so requests remains
                # thread-safe.  Merge Set-Cookie rotations back into the
                # visible site session before emitting completion; otherwise
                # the next browser-like operation would keep stale auth/CSRF
                # state even though the page request succeeded.
                result = _merge_worker_session_result(sess.client.session, result)
                # 无论成败都要解锁，且先保存缓存再推事件
                sess.upload_cache = dict(result.get("upload_cache") or {})
                sess.failed_paths = list(result.get("failed_paths") or [])
                # 将后台上传接口返回的尺寸/压缩/重命名等提示带回软件完成提示，
                # 让软件操作的可见结果与后台直接上传一致，而不是静默丢弃。
                upload_notes = []
                upload_details = []
                for entry in list(result.get("upload_metadata") or []):
                    meta = entry.get("metadata") if isinstance(entry, dict) else {}
                    if not isinstance(meta, dict):
                        continue
                    data = meta.get("data")
                    data = data if isinstance(data, dict) else {}
                    notes = [source.get(k) for source in (meta, data)
                             for k in ("notice", "warning") if source.get(k)]
                    if entry.get("possible_shared_object"):
                        names = ", ".join(str(item) for item in (entry.get("shared_with") or []) if item)
                        evidence = ",".join(str(item) for item in
                                             (entry.get("shared_object_evidence") or []))
                        notes.append("不同上传地址返回相同服务器哈希/ETag，可能存在服务器去重/物理对象共享" +
                                     (f"（证据：{evidence}）" if evidence else "") +
                                     (f"（关联：{names}）" if names else ""))
                    if meta.get("server_changed") or data.get("server_changed"):
                        notes.append("服务器已对上传文件执行处理（客户端与服务器字节、尺寸、格式或元数据存在差异）")
                    upload_notes.extend(str(note) for note in notes if note)
                    detail_keys = ("width", "height", "size", "filesize", "fileSize",
                                   "filename", "fileName", "name", "mime", "mimeType",
                                   "compress", "watermark", "imageCompressEnable",
                                   "imageCompressBorder", "server_width", "server_height",
                                   "server_bytes", "server_mime", "server_format",
                                   "server_sha256", "server_inspection", "server_changed",
                                   "server_etag", "server_last_modified", "server_filename",
                                   "server_url_basename", "server_content_type",
                                   "server_content_encoding", "server_cache_control",
                                   "server_age", "server_vary", "server_content_length",
                                   "client_filename", "client_extension",
                                   "server_change_fields", "client_width", "client_height",
                                   "client_bytes", "client_mime", "client_format", "client_sha256",
                                   "server_alpha", "server_animated", "server_frames",
                                   "server_exif_orientation", "server_icc_sha256",
                                   "client_alpha", "client_animated", "client_frames",
                                   "client_exif_orientation", "client_icc_sha256")
                    detail_values = []
                    sources = [meta, entry.get("policy") if isinstance(entry, dict) else {}]
                    if isinstance(data, dict):
                        sources.append(data)
                    for source in sources:
                        if not isinstance(source, dict):
                            continue
                        for key in detail_keys:
                            value = source.get(key)
                            if value not in (None, "", False) and f"{key}={value}" not in detail_values:
                                detail_values.append(f"{key}={value}")
                    if detail_values:
                        upload_details.append(
                            f"{entry.get('filename', '文件')}：" + ", ".join(detail_values))
                result["upload_warnings"] = list(dict.fromkeys(
                    str(source.get("warning"))
                    for entry in list(result.get("upload_metadata") or []) if isinstance(entry, dict)
                    for source in [entry.get("metadata") or {}]
                    if isinstance(source, dict) and source.get("warning")))
                suffixes = []
                if upload_notes:
                    suffixes.append("后台上传提示：" + "；".join(dict.fromkeys(upload_notes)))
                if upload_details:
                    suffixes.append("后台上传结果：" + "；".join(dict.fromkeys(upload_details)))
                if suffixes:
                    suffix = "；" + "；".join(suffixes)
                    result["technical_details"] = suffix.lstrip("；")
                    debug_log("[上传技术详情] " + result["technical_details"])
                sess.retryable = bool(result.get("retryable")) \
                    if done_event == "publish_done" else False
                if done_event == "publish_done" and not sess.retryable:
                    sess.last_publish_payload = None
                    sess.last_publish_title = ""
                if done_event == "publish_done" and result.get("ok"):
                    # 成功后使旧稿失效，防止按钮重入或前端状态异常导致重复发布。
                    sess.parsed = {}
                    sess.orig_parsed = {}
                    sess.html_path = ""
                if done_event == "edit_done" and result.get("ok"):
                    # 修改后必须重新载入文章，不能把上一份 HTML 自动应用到下一篇。
                    sess.edit_parsed = {}
                    sess.edit_orig_parsed = {}
                    sess.edit_html_path = ""
                    sess.edit_loaded_article_id = ""
                    sess.edit_form_fields = []
                    sess.edit_formcheck = ""
                    sess.edit_url_hint = ""
                    sess.current_values = {}
                if done_event in ("publish_done", "edit_done"):
                    audit_action = "content_publish" if done_event == "publish_done" else "content_edit"
                    target_id = (payload.get("scode", "") if done_event == "publish_done"
                                 else payload.get("article_id", ""))
                    record_audit(
                        getattr(sess.client, "site_key", ""), audit_action,
                        "content", target_id,
                        status="review" if result.get("requires_review") else
                        ("success" if result.get("ok") else
                        ("cancelled" if result.get("cancelled") else "failed")),
                        before=payload.get("_audit_before", {}),
                        after=result.get("fields", payload.get("fields", {})),
                        message=result.get("msg", ""))
                journal_state = ("review" if result.get("requires_review") or
                                 str(result.get("outcome", "")).lower() in ("unknown", "different")
                                 else ("cancelled" if result.get("cancelled") else
                                       ("success" if result.get("ok") else "failed")))
                operation_journal.finish(journal_id, journal_state, result,
                                         str(result.get("msg", "") or ""))
                if sess._journal_op_id == journal_id:
                    sess._journal_op_id = ""
                sess._busy = False
                sess._task_ctx = None
            payload_out = {k: v for k, v in result.items() if k != "upload_cache"}
            payload_out.update(extra)
            payload_out["tab_id"] = tab_id
            payload_out["has_failed"] = bool(sess.retryable)
            # 已关闭标签不再接收旧任务事件；否则同一 tab_id 若被复用，旧事件
            # 可能清掉新标签的 busy 状态或显示错误的发布结果。
            if not sess._closed.is_set():
                _push(done_event, payload_out)
        threading.Thread(target=worker, daemon=True).start()

    @_guard
    def cancel_task(self, tab_id):
        sess = self._session(tab_id)
        ctx = sess._task_ctx
        if ctx is None:
            return _err("当前没有正在执行的任务")
        ctx.cancel()
        return _ok(msg="已请求取消，当前网络请求结束后停止")

    @_guard
    def clipboard_get(self):
        """供前端 Ctrl+V 回退使用（WebView 剪贴板 API 被拒时）。"""
        return _ok(text=_clipboard_get_text())

    @_guard
    def clipboard_set(self, text=""):
        """供前端 Ctrl+C / Ctrl+X 回退使用。"""
        return _ok(done=bool(_clipboard_set_text(text)))

    @_guard
    def open_external_url(self, url=""):
        """仅在用户点击后打开明确的 HTTP(S) 地址。"""
        url = str(url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return _err("只能打开有效的 HTTP/HTTPS 地址")
        opened = bool(webbrowser.open(url, new=2))
        return _ok(opened=opened)

    @_guard
    def open_authenticated_url(self, tab_id, url="", title="原生网页", handoff=None):
        """Open a same-origin backend page in a real embedded WebView.

        The requests session remains the source of truth.  Safe, script-visible
        cookies are copied after the first page load and the page is reloaded;
        HttpOnly cookies are never exposed to JavaScript.  If the GUI cannot
        create a child window, the caller can fall back to the system browser.
        """
        sess = self._session(tab_id)
        target = str(url or "").strip()
        parsed = urlparse(target)
        base = getattr(sess.client, "base_url", "") or getattr(sess.client, "admin_url", "")
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or
                not _same_http_origin(target, base)):
            return _err("原生网页地址必须与当前登录站点同源")
        if _window is None:
            return _err("当前窗口尚未就绪")
        pairs = _browser_cookie_pairs(sess.client.session, target)
        script = _browser_cookie_script(pairs)
        handoff = _sanitize_native_handoff(handoff)
        handoff_script = _native_handoff_script(handoff)
        cookie_bridge = _NativeCookieBridge(sess.client.session, target)
        sync_script = _browser_cookie_sync_script()
        try:
            child = webview.create_window(
                str(title or "原生网页")[:120], target,
                width=1280, height=860, min_size=(900, 640),
                text_select=True, zoomable=True, js_api=cookie_bridge)
            cookie_poll_stop = threading.Event()
            state = {"injected": False, "loads": 0, "poll_started": False,
                     "handoff_applied": False}

            def poll_native_cookie_store():
                # Native get_cookies is host-side and may block briefly while
                # the GUI backend answers.  Keep it bounded by the window
                # lifecycle and run it on a daemon timer so the desktop app
                # never waits for CookieStore during shutdown.
                if cookie_poll_stop.is_set():
                    return
                try:
                    get_cookies = getattr(child, "get_cookies", None)
                    if callable(get_cookies):
                        current_url = child.evaluate_js("location.href")
                        cookie_bridge.sync_native_cookies(
                            get_cookies(), current_url)
                except Exception as exc:
                    debug_log(f"[原生网页会话] CookieStore轮询失败: {exc}")
                if not cookie_poll_stop.is_set():
                    timer = threading.Timer(2.0, poll_native_cookie_store)
                    timer.daemon = True
                    timer.start()

            def on_loaded(window):
                try:
                    state["loads"] += 1
                    # First load imports the requests cookies and reloads once;
                    # subsequent loads reflect cookies produced by page JS.
                    if not state["injected"]:
                        state["injected"] = True
                        if pairs:
                            window.evaluate_js(script)
                    window.evaluate_js(sync_script)
                    raw = window.evaluate_js("document.cookie")
                    current_url = window.evaluate_js("location.href")
                    # A native browser may have completed the canonical
                    # HTTP:80 -> HTTPS:443 upgrade before the first loaded
                    # callback.  Keep the requests client on that final
                    # origin too, otherwise a later desktop operation would
                    # silently start from the stale HTTP entry while the
                    # visible webpage is already on HTTPS.  adopt_resolved_url
                    # preserves the configured admin route and rejects every
                    # cross-host/downgrade transition itself.
                    adopt = getattr(sess.client, "adopt_resolved_url", None)
                    if callable(adopt) and current_url:
                        try:
                            adopt(str(current_url))
                        except Exception as exc:
                            debug_log(f"[原生网页会话] 最终地址同步失败: {exc}")
                    if isinstance(raw, str):
                        cookie_bridge.sync_cookies(raw, current_url)
                    # Cookie import may trigger one reload.  Apply the draft
                    # only after that canonical page is visible, and only
                    # once; the script itself retries a few times for forms
                    # created by ordinary page JavaScript.
                    if (handoff_script and not state["handoff_applied"] and
                            (not pairs or state["loads"] > 1)):
                        window.evaluate_js(handoff_script)
                        state["handoff_applied"] = True
                    # pywebview's native CookieStore includes HttpOnly and
                    # SSO cookies which JavaScript cannot expose.  Use it
                    # only as a trusted host-side read and keep the same
                    # origin/path validation in the bridge.
                    get_cookies = getattr(window, "get_cookies", None)
                    if callable(get_cookies):
                        cookie_bridge.sync_native_cookies(
                            get_cookies(), current_url)
                    if not state["poll_started"]:
                        state["poll_started"] = True
                        poll_native_cookie_store()
                except Exception as exc:
                    debug_log(f"[原生网页会话] Cookie同步失败: {exc}")

            def on_closing(window):
                # Capture a final non-HttpOnly token rotation before the child
                # disappears.  HttpOnly cookies remain in the requests jar and
                # are never exposed to JavaScript.
                try:
                    raw = window.evaluate_js("document.cookie")
                    current_url = window.evaluate_js("location.href")
                    adopt = getattr(sess.client, "adopt_resolved_url", None)
                    if callable(adopt) and current_url:
                        try:
                            adopt(str(current_url))
                        except Exception as exc:
                            debug_log(f"[原生网页会话] 关闭前地址同步失败: {exc}")
                    if isinstance(raw, str):
                        cookie_bridge.sync_cookies(raw, current_url)
                    get_cookies = getattr(window, "get_cookies", None)
                    if callable(get_cookies):
                        cookie_bridge.sync_native_cookies(
                            get_cookies(), current_url)
                except Exception as exc:
                    debug_log(f"[原生网页会话] 关闭前Cookie同步失败: {exc}")

            def on_closed(window):
                cookie_poll_stop.set()
                with _native_windows_lock:
                    _native_windows.pop(getattr(window, "uid", ""), None)

            child.events.loaded += on_loaded
            if hasattr(child.events, "closing"):
                child.events.closing += on_closing
            child.events.closed += on_closed
            with _native_windows_lock:
                _native_windows[child.uid] = {
                    "window": child,
                    "site_key": str(getattr(sess.client, "site_key", "") or ""),
                    "cookie_bridge": cookie_bridge,
                    "cookie_poll_stop": cookie_poll_stop,
                }
            return _ok(opened=True, mode="embedded", cookie_count=len(pairs),
                       session_handoff=bool(pairs), handoff=bool(handoff))
        except Exception as exc:
            debug_log(f"[原生网页窗口] 创建失败: {exc}")
            return _err(f"无法创建原生网页窗口: {exc}")

    @_guard
    def open_native_login(self, tab_id, url="", title="原生网页登录"):
        """Open a login URL even when the requests probe cannot parse it.

        SSO pages and challenge screens may be inaccessible to the static
        login parser.  Set the site identity only after sanitizing the user
        supplied URL, clear a previous site's cookies on an actual switch,
        then reuse the authenticated WebView bridge.  The user must still
        explicitly press the follow-up sync button before desktop operations
        are unlocked.
        """
        sess = self._session(tab_id)
        if sess._busy:
            return _err("有任务正在执行，请稍候")
        target = _sanitize_admin_url(url)
        if not target:
            return _err("请填写有效的后台地址")
        old_key = str(getattr(sess.client, "site_key", "") or "")
        sess.client.set_admin_url(target)
        if old_key and old_key != str(getattr(sess.client, "site_key", "") or ""):
            sess.client.session.cookies.clear()
            sess.client.logged_in = False
            sess.login_info = {}
            self._clear_site_workflow_state(sess)
        sess._prepared_raw_url = target
        return self.open_authenticated_url(tab_id, sess.client.admin_url, title)

    @_guard
    def sync_native_session(self, tab_id):
        """Pull native WebView CookieStore records before a requests probe.

        A login or token refresh performed by page JavaScript can set an
        HttpOnly cookie without navigating.  The regular loaded/closing hooks
        eventually capture it, but an explicit UI sync should be immediate so
        the user can return from native login and continue in the desktop
        workflow without waiting for the child window to close.
        """
        sess = self._session(tab_id)
        site_key = str(getattr(sess.client, "site_key", "") or "")
        if not site_key:
            return _err("当前标签尚未设置站点")
        with _native_windows_lock:
            entries = [item for item in _native_windows.values()
                       if isinstance(item, dict) and
                       str(item.get("site_key", "")) == site_key]
        updated = 0
        windows = 0
        for item in entries:
            window = item.get("window")
            bridge = item.get("cookie_bridge")
            get_cookies = getattr(window, "get_cookies", None)
            if not callable(get_cookies) or not isinstance(bridge, _NativeCookieBridge):
                continue
            try:
                current_url = window.evaluate_js("location.href")
                result = bridge.sync_native_cookies(get_cookies(), current_url)
                if result.get("ok"):
                    updated += int(result.get("updated", 0) or 0)
                    windows += 1
            except Exception as exc:
                debug_log(f"[原生网页会话] 主动同步CookieStore失败: {exc}")
        return _ok(updated=updated, windows=windows)

    @_guard
    def open_local_path(self, path=""):
        """打开用户刚导出/备份的现有本地文件，不执行任意命令。"""
        path = os.path.abspath(str(path or ""))
        if not path or not os.path.exists(path):
            return _err("文件不存在或已被移动")
        if os.name != "nt" or not hasattr(os, "startfile"):
            return _err("当前系统不支持从应用内打开本地文件")
        os.startfile(path)  # type: ignore[attr-defined]
        return _ok(opened=True, path=path)

    @_guard
    def load_audit_records(self, tab_id, limit=200):
        sess = self._session(tab_id)
        site = getattr(sess.client, "site_key", "")
        records = list_audit(site, limit)
        return _ok(records=records, count=len(records))

    @_guard
    def export_audit_records(self, tab_id):
        sess = self._session(tab_id)
        path, count = export_audit_csv(getattr(sess.client, "site_key", ""))
        return _ok(path=path, count=count)

    @_guard
    def load_pending_operations(self, tab_id):
        """Return durable requests left unresolved by an earlier process."""
        sess = self._session(tab_id)
        site = getattr(sess.client, "site_key", "")
        operations = operation_journal.list_pending(site)
        return _ok(operations=operations, count=len(operations))

    @_guard
    def resolve_pending_operation(self, operation_id, note=""):
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            return _err("缺少待核对操作编号")
        if not operation_journal.resolve(operation_id, str(note or "")):
            return _err("待核对操作不存在或已处理")
        return _ok(msg="已确认后台结果并关闭该待核对记录；软件不会自动重发")

    @_guard
    def pick_images(self, picker_key="images", extensions=None):
        extensions = self._normalize_image_extensions(extensions)
        patterns = ";".join("*" + item for item in extensions)
        if not patterns:
            patterns = "*.jpg;*.jpeg;*.jpe;*.png;*.gif;*.webp;*.bmp;*.tif;*.tiff;*.svg;*.avif;*.heic;*.heif;*.ico;*.jxl;*.jp2;*.j2k;*.jpf;*.jpx;*.jpm;*.psd"
        paths = _window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            directory=self._dialog_directory(picker_key),
            file_types=(f"图片文件 ({patterns})",
                        "所有文件 (*.*)"))
        if paths:
            self._remember_dialog_path(picker_key, paths[0])
        selected = list(paths or [])
        return _ok(paths=selected,
                   file_types=[{"path": path, "type": _path_mime_type(path)}
                               for path in selected])

    @_guard
    def pick_files(self, picker_key="files", allow_multiple=False, extensions=None):
        """Pick files for native CMS ``type=file`` controls.

        The browser's file input does not expose a local path to the server;
        this bridge only returns the user's explicit selection so the normal
        upload-policy adapter can send it once with the correct field.
        """
        patterns = ";".join("*" + item for item in self._normalize_file_extensions(extensions))
        file_types = (f"允许文件 ({patterns})", "所有文件 (*.*)") if patterns \
            else ("所有文件 (*.*)",)
        paths = _window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=bool(allow_multiple),
            directory=self._dialog_directory(picker_key),
            file_types=file_types)
        if paths:
            self._remember_dialog_path(picker_key, paths[0])
        selected = list(paths or [])
        return _ok(paths=selected,
                   file_types=[{"path": path, "type": _path_mime_type(path)}
                               for path in selected])

    @_guard
    def save_dropped_files(self, picker_key="dropped", items=None,
                           allow_multiple=False, extensions=None):
        """Materialize files dropped onto the WebView into safe temp paths.

        Chromium/Qt WebEngine intentionally does not expose a stable local
        path for a ``File`` object.  The browser picker bridge therefore also
        accepts a bounded data URL fallback, while preserving a native
        ``File.path`` when the host exposes one.  URLs, HTML and text drops are
        never accepted, so dropping a link cannot navigate the application.
        """
        rows = items if isinstance(items, (list, tuple)) else []
        if not rows:
            return _ok(cancelled=True, paths=[])
        if not allow_multiple:
            rows = rows[:1]
        allowed = set(self._normalize_file_extensions(extensions))
        # Do not impose a desktop-only 64/128 MiB ceiling here.  The native
        # webpage lets its accept/size policy decide whether a File is valid;
        # this bridge must hand the complete drop to the same policy instead
        # of rejecting a perfectly valid large asset before upload discovery.
        # The caller still receives bounded policy validation immediately
        # before the first XHR, and the normal task byte budget remains the
        # resource-exhaustion guard for a publish transaction.
        total_limit = None
        item_limit = None
        total = 0
        folder = tempfile.mkdtemp(prefix="pboot-dropped-")
        paths = []
        try:
            for index, item in enumerate(rows):
                if not isinstance(item, dict):
                    raise ValueError("拖放数据格式无效")
                original_name = os.path.basename(str(item.get("name", "") or "").strip())
                original_suffix = os.path.splitext(original_name)[1].lower()
                name = original_name
                name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name).strip(" .")
                if not name:
                    name = f"dropped-{index + 1}.bin"
                suffix = os.path.splitext(name)[1].lower()
                source_path = str(item.get("path", "") or "").strip()
                raw = None
                if source_path:
                    source_path = os.path.abspath(source_path)
                    if not os.path.isfile(source_path):
                        raise ValueError(f"拖放文件不存在：{name}")
                    size = os.path.getsize(source_path)
                    with open(source_path, "rb") as handle:
                        raw = handle.read() if item_limit is None else handle.read(item_limit + 1)
                else:
                    data_url = str(item.get("data_url", "") or "")
                    match = re.fullmatch(
                        r"data:([^;,\s]*)(?:;[^,]*)?;base64,([A-Za-z0-9+/=\r\n]+)",
                        data_url, re.I)
                    if not match:
                        raise ValueError(f"拖放对象不是受支持的本地文件：{name}")
                    try:
                        raw = base64.b64decode(match.group(2), validate=True)
                    except (ValueError, base64.binascii.Error) as exc:
                        raise ValueError(f"拖放文件内容无效：{name}") from exc
                if raw is None or len(raw) == 0:
                    raise ValueError(f"拖放文件为空：{name}")
                if item_limit is not None and len(raw) > item_limit:
                    raise ValueError(f"拖放文件超过当前上传策略允许的单文件大小：{name}")
                if allowed:
                    # A browser File with a real extension is filtered by its
                    # filename/OS MIME mapping.  For a genuinely extensionless
                    # drop, Chromium can still provide a useful File.type; the
                    # bridge has no stable path, so use the bounded signature
                    # detector as the equivalent safe fallback.  Do not use
                    # bytes to override a non-empty (but wrong) extension: a
                    # renamed PDF/PNG must remain rejected just like a native
                    # file picker would reject its File.type.
                    accepted = original_suffix in allowed
                    if not accepted and not original_suffix:
                        detected_ext = sniff_extension(raw, original_name)
                        accepted = detected_ext.lower() in allowed
                    # A native browser File can carry a trustworthy MIME even
                    # when its name has no extension and its bytes have no
                    # short signature (fonts and some legacy media are common
                    # examples).  Match that MIME only against the already
                    # narrowed extension allow-list; never use it to override
                    # a non-empty, mismatching extension.
                    if not accepted and not original_suffix:
                        declared_mime = str(item.get("type", "") or "").split(
                            ";", 1)[0].strip().lower()
                        if declared_mime:
                            accepted = any(
                                mime_for_extension(ext).lower() == declared_mime
                                for ext in allowed)
                    if not accepted:
                        raise ValueError(f"文件格式不符合当前网页 accept 限制：{name}")
                total += len(raw)
                if total_limit is not None and total > total_limit:
                    raise ValueError("本次拖放文件总大小超过当前上传策略限制")
                target = os.path.join(folder, name)
                if os.path.exists(target):
                    stem, ext = os.path.splitext(name)
                    target = os.path.join(folder, f"{stem}-{index + 1}{ext}")
                with open(target, "xb") as handle:
                    handle.write(raw)
                # Preserve the WebView File.type for extensionless drops so
                # the eventual multipart part carries the same browser MIME
                # even when the bytes have no recognizable short signature.
                remember_declared_mime(target, item.get("type", ""))
                paths.append(target)
            return _ok(paths=paths, dropped=True, bytes=total,
                       file_types=[{"path": path,
                                    "type": str(item.get("type", "") or "").split(";", 1)[0].strip().lower()}
                                   for path, item in zip(paths, rows)])
        except Exception as exc:
            # Do not leave a half-selected batch that the caller might upload.
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
            return _err(str(exc), paths=[])

    @staticmethod
    def _normalize_file_extensions(value):
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        if not isinstance(value, (list, tuple)):
            return []
        allowed = []
        for item in value:
            token = str(item or "").strip().lower()
            if token.startswith(".") and re.fullmatch(r"\.[a-z0-9_-]{1,30}", token):
                if token not in allowed:
                    allowed.append(token)
        return allowed

    @staticmethod
    def _normalize_image_extensions(value):
        """Normalize a browser accept list for the native picker only."""
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        if not isinstance(value, (list, tuple)):
            return []
        allowed = []
        for item in value:
            token = str(item or "").strip().lower()
            if token.startswith("image/"):
                continue
            if not token.startswith("."):
                token = "." + token
            if re.fullmatch(r"\.[a-z0-9_-]{1,30}", token) and token not in allowed:
                allowed.append(token)
        return allowed

    @_guard
    def pick_image(self, picker_key="thumbnail", extensions=None):
        extensions = self._normalize_image_extensions(extensions)
        patterns = ";".join("*" + item for item in extensions)
        if not patterns:
            patterns = "*.jpg;*.jpeg;*.jpe;*.png;*.gif;*.webp;*.bmp;*.tif;*.tiff;*.svg;*.avif;*.heic;*.heif;*.ico;*.jxl;*.jp2;*.j2k;*.jpf;*.jpx;*.jpm;*.psd"
        paths = _window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            directory=self._dialog_directory(picker_key),
            file_types=(f"图片文件 ({patterns})",
                        "所有文件 (*.*)"))
        path = paths[0] if paths else ""
        if not path:
            return _ok(cancelled=True, path="")
        self._remember_dialog_path(picker_key, path)
        return _ok(path=path,
                   file_types=[{"path": path, "type": _path_mime_type(path)}])

    @_guard
    def preview_local_image(self, path, max_bytes=8 * 1024 * 1024):
        """Return a bounded, validated local image preview for the WebView.

        Native file pickers return Windows paths, which a local WebView must
        not load through an unrestricted ``file://`` URL.  The browser still
        needs the same immediate thumbnail feedback as the web editor, so the
        bridge validates a selected image and exposes only a bounded data URL.
        Formats without a Pillow decoder use the same strict signature/SVG
        checks as the upload path; arbitrary files and active SVG content are
        never interpreted.
        """
        candidate = os.path.abspath(str(path or "").strip())
        try:
            limit = max(1, min(int(max_bytes), 16 * 1024 * 1024))
        except (TypeError, ValueError):
            limit = 8 * 1024 * 1024
        if not candidate or not os.path.isfile(candidate):
            return _err("本地预览文件不存在")
        try:
            size = os.path.getsize(candidate)
            if size <= 0 or size > limit:
                return _err("本地预览文件过大或为空")
            with open(candidate, "rb") as handle:
                raw = handle.read(limit + 1)
            if len(raw) > limit:
                return _err("本地预览文件过大")
            from PIL import Image
            import io
            from asset_types import sniff_mime
            suffix = os.path.splitext(candidate)[1].lower()
            mime_map = {
                "JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif",
                "WEBP": "image/webp", "BMP": "image/bmp", "TIFF": "image/tiff",
                "AVIF": "image/avif", "ICO": "image/x-icon", "HEIC": "image/heic",
                "HEIF": "image/heif", "JXL": "image/jxl", "JPEG XL": "image/jxl",
                "JPEG2000": "image/jp2", "JPEG 2000": "image/jp2",
                "PSD": "image/vnd.adobe.photoshop",
            }
            width = height = None
            fmt = ""
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    image.verify()
                    width, height = int(image.width), int(image.height)
                    fmt = str(image.format or "").upper()
            except Exception:
                # ``first_thumbnail`` owns the fail-closed signature rules;
                # reuse them instead of maintaining a second, looser parser.
                from first_thumbnail import _verify_image_file
                ext_mime = {
                    ".svg": "image/svg+xml", ".avif": "image/avif",
                    ".heic": "image/heic", ".heif": "image/heif",
                    ".jxl": "image/jxl", ".jp2": "image/jp2",
                    ".j2k": "image/jp2", ".jpf": "image/jp2",
                    ".jpx": "image/jp2", ".jpm": "image/jp2",
                    ".psd": "image/vnd.adobe.photoshop",
                }.get(suffix, "")
                if not ext_mime:
                    detected = sniff_mime(raw, candidate)
                    if detected.startswith("image/"):
                        ext_mime = detected
                _verify_image_file(candidate, content_type=ext_mime)
                mime = ext_mime
                dimensions = image_dimensions(raw, candidate, mime)
                if not mime:
                    return _err("该文件不是受支持的安全栅格图片")
                return _ok(data_url="data:%s;base64,%s" %
                           (mime, base64.b64encode(raw).decode("ascii")),
                           name=os.path.basename(candidate), bytes=len(raw),
                           mime=mime,
                           **({"width": dimensions[0], "height": dimensions[1]}
                              if dimensions else {}))
            mime = mime_map.get(fmt)
            if not mime:
                return _err("该文件不是受支持的安全栅格图片")
            return _ok(data_url="data:%s;base64,%s" %
                       (mime, base64.b64encode(raw).decode("ascii")),
                       width=width, height=height, mime=mime,
                       name=os.path.basename(candidate), bytes=len(raw))
        except Exception as exc:
            debug_log(f"[本地图片预览] 忽略无效图片 {candidate}: {exc}")
            return _err("本地图片无法预览")

    def pick_jpg_image(self, picker_key="thumbnail"):
        """Legacy bridge alias; allowed formats are now decided by the backend."""
        return self.pick_image(picker_key)

    @_guard
    def export_diagnostics(self):
        target = _window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=self._dialog_directory("diagnostics_export"),
            save_filename="diagnostics.zip")
        if not target:
            return _ok(cancelled=True)
        path = target if isinstance(target, str) else target[0]
        self._remember_dialog_path("diagnostics_export", path)
        export_diagnostics(path, self.config.data)
        return _ok(path=str(path))


def main():
    global _window
    api = Api()
    here = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    index = os.path.join(here, "webui", "index.html")
    if getattr(sys, "frozen", False):
        index = os.path.join(sys._MEIPASS, "webui", "index.html")
    _window = webview.create_window(
        WINDOW_TITLE, index, js_api=api,
        width=1320, height=880, min_size=(1100, 760))
    webview.start(debug=bool(os.environ.get("PBOOT_WEBUI_DEBUG")))


if __name__ == "__main__":
    main()
