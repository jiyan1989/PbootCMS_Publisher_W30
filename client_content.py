"""Focused PbootCMS ContentMixin service."""
import io
import hashlib
import inspect
import mimetypes
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from datetime import datetime
from pathlib import Path
from urllib.parse import (parse_qs, parse_qsl, quote, unquote, urljoin,
                          urlparse, urlsplit, urlunsplit, urlencode)
import requests
from bs4 import BeautifulSoup
try:
    from PIL import Image
except ImportError:
    Image = None
from constants import TIMEOUT_NORMAL, TIMEOUT_UPLOAD, MCODE_ORDER, FIELD_XINGHAO, FIELD_JIAGE
from exceptions import Cancelled, NetworkError
from logger import debug_log, debug_log_v
from request import http_request
from http_transport import permitted_transition, request_with_redirects
from client_utils import get_base_dir, _is_login_page
from html_images import replace_image_occurrences
from form_controls import (form_elements, describe_form, serialize_form,
                           successful_pairs, BrowserFormData, merge_form_updates,
                           text_value, submission_attributes, is_disabled)
from upload_protocol import parse_upload_result, value_for
from upload_policy import (discover_policies, UploadPolicyError, safe_url,
                           encoded_upload_data, flatten_upload_data)
from save_verification import compare_saved_fields, submission_expectations
from asset_types import sniff_mime, sniff_extension, image_dimensions
from file_metadata import declared_mime


_RESPONSE_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|权限不足|未授权|被拒绝|"
    r"请先登录|登录[^\n]{0,12}失效|校验[^\n]{0,12}失败|"
    r"\b(?:error|failed|failure|invalid|forbidden|unauthorized|denied)\b",
    re.I,
)
_RESPONSE_SUCCESS_RE = re.compile(
    r"(?:添加|发布|提交|保存|修改|操作)?成功|"
    r"\bsuccess(?:ful|fully)?\b",
    re.I,
)


def _close_response(response):
    """Release a requests/adapter response without changing its result.

    Upload and mutation responses are fully consumed by the time their
    metadata/status has been parsed.  Explicitly closing them returns the
    connection to the pool just like a browser XHR completion callback.  The
    helper intentionally tolerates lightweight test/plugin response doubles
    which do not expose ``close``.
    """
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _ueditor_dialog_endpoint(endpoint):
    """Add the WebUploader dialog's ``encode=utf-8`` query parameter.

    UEditor's image/video/attachment dialogs set the uploader URL at
    ``startUpload`` to ``actionUrl + encode=utf-8 + serverparam``.  Insert the
    marker immediately after the action parameter while preserving any static
    server URL query values and the already-encoded serverparam values.
    """
    parts = urlsplit(str(endpoint or ""))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "encode" for key, _value in pairs):
        return str(endpoint)
    output = []
    inserted = False
    for key, value in pairs:
        output.append((key, value))
        if key == "action" and not inserted:
            output.append(("encode", "utf-8"))
            inserted = True
    if not inserted:
        output.insert(0, ("encode", "utf-8"))
    query = "&".join(
        quote(str(key), safe="-_.!~*'()") + "=" +
        quote(str(value), safe="-_.!~*'()")
        for key, value in output)
    return urlunsplit(parts._replace(query=query, fragment=""))


def mark_write_attempt(client):
    """Mark a mutation immediately before its first network write.

    Dedicated admin mixins use the shared form transport but otherwise do not
    have :meth:`ContentMixin.edit_content`'s classifier.  Setting this marker
    before dispatch lets the bridge preserve an ``unknown``/review outcome if
    the connection fails after the server may already have received the body.
    Callers replace it with ``verified`` only after their own post-write
    readback succeeds; validation failures occur before this helper is called.
    """
    if client is None:
        return
    client.last_write_result = {
        "outcome": "unknown", "write_attempted": True,
        "requires_review": True, "retryable": False,
    }


def mark_write_http_result(client, response):
    """Classify a completed mutation response without implying success.

    A 4xx response (except 408, whose request may have reached the server)
    is an explicit server-side rejection.  5xx and 408 remain unknown because
    the server may have processed the body before returning/losing the result.
    The helper preserves any operation metadata already attached by
    :func:`mark_write_attempt`, so direct mixin callers and the app bridge see
    the same durable result.
    """
    status = int(getattr(response, "status_code", 0) or 0)
    outcome = "rejected" if 400 <= status < 500 and status != 408 else "unknown"
    current = dict(getattr(client, "last_write_result", {}) or {})
    current.update({
        "outcome": outcome,
        "write_attempted": True,
        "requires_review": outcome != "rejected",
        "retryable": False,
        "status_code": status,
    })
    client.last_write_result = current
    return outcome


def mark_write_rejected(client, response=None, message=""):
    """Record a mutation explicitly refused by a valid server response."""
    current = dict(getattr(client, "last_write_result", {}) or {})
    current.update({
        "outcome": "rejected",
        "write_attempted": True,
        "requires_review": False,
        "retryable": False,
    })
    if response is not None:
        current["status_code"] = int(getattr(response, "status_code", 0) or 0)
    if message:
        current["message"] = str(message)
    client.last_write_result = current
    return "rejected"


def mark_write_verified(client):
    """Record that a mutation and its required readback both succeeded."""
    current = dict(getattr(client, "last_write_result", {}) or {})
    current.update({
        "outcome": "verified",
        "write_attempted": True,
        "requires_review": False,
        "retryable": False,
    })
    client.last_write_result = current
    return "verified"


def _image_asset_metadata(data, prefix):
    """Extract bounded, comparable image properties from already-read bytes.

    These values are observations only: they never decide whether an upload is
    accepted.  Keeping them on both sides of the upload lets the completion
    result identify format/alpha/animation/EXIF/ICC processing that a plain
    width/height comparison would miss.
    """
    if not data:
        return {}
    key = str(prefix or "image").strip() or "image"
    result = {}
    if Image is not None:
        try:
            with Image.open(io.BytesIO(bytes(data))) as image:
                result.update({
                    f"{key}_width": int(image.width),
                    f"{key}_height": int(image.height),
                    f"{key}_format": str(image.format or "").upper(),
                    f"{key}_alpha": bool(
                        "A" in image.getbands() or "transparency" in image.info),
                    f"{key}_animated": bool(
                        getattr(image, "is_animated", False) or
                        int(getattr(image, "n_frames", 1) or 1) > 1),
                    f"{key}_frames": int(getattr(image, "n_frames", 1) or 1),
                })
                try:
                    orientation = image.getexif().get(274)
                except Exception:
                    orientation = None
                if orientation not in (None, ""):
                    result[f"{key}_exif_orientation"] = int(orientation)
                icc = image.info.get("icc_profile")
                if icc:
                    result[f"{key}_icc_sha256"] = hashlib.sha256(bytes(icc)).hexdigest()
        except Exception:
            # The bundled Pillow build does not decode every image type that
            # browsers accept.  The bounded container parser below still
            # provides intrinsic dimensions without transcoding or executing
            # the file.
            pass
    if (f"{key}_width" not in result or f"{key}_height" not in result):
        dimensions = image_dimensions(data)
        if dimensions:
            result[f"{key}_width"], result[f"{key}_height"] = dimensions
    return result


def upload_result_entry(result, label="", filename=""):
    """Return a bounded UI/audit record for any completed media upload.

    Dedicated admin surfaces (category, Slide and Single) perform their
    uploads synchronously rather than through the article worker.  They still
    need to expose the exact server/read-back evidence instead of silently
    discarding last_upload_result after the form POST.
    """
    value = result if isinstance(result, dict) else {}
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    entry = {
        "label": str(label or "上传文件"),
        "filename": str(filename or ""),
        "url": str(value.get("path", "") or ""),
        "metadata": dict(metadata),
        "policy": dict(value.get("policy") or {})
            if isinstance(value.get("policy"), dict) else {},
        "outcome": str(value.get("outcome", "") or ""),
    }
    transform = value.get("client_transform")
    if isinstance(transform, dict) and transform:
        entry["client_transform"] = dict(transform)
    mode = str(value.get("ueditor_upload_mode", "") or "").strip()
    if mode:
        entry["ueditor_upload_mode"] = mode
    return entry


def _normalise_asset_mime(value):
    value = str(value or "").split(";", 1)[0].strip().lower()
    return {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(value, value)


def _apply_upload_url_prefix(path, prefix):
    """Apply a UEditor URL prefix without corrupting absolute callbacks.

    UEditor normally returns a relative ``url`` and the browser concatenates
    ``imageUrlPrefix``/``fileUrlPrefix`` with that value.  Custom handlers
    sometimes return an already absolute HTTP(S) or protocol-relative URL;
    browsers use that URL as-is.  Blind string concatenation (the old desktop
    behavior) produced values such as ``/upload/https://site/a.jpg`` and made
    the thumbnail differ from the web result.  Keep the native concatenation
    for relative paths while preserving explicit absolute callbacks.
    """
    value = str(path or "")
    parsed = urlparse(value)
    if parsed.scheme.lower() in ("http", "https") or parsed.netloc:
        return value
    return str(prefix or "") + value


def _server_response_metadata(headers, resolved_url=""):
    """Return safe cache/object headers useful for final-media comparison.

    These are observations only.  ETag and Last-Modified are not proof of
    physical storage identity, but they are valuable clues when two returned
    URLs appear to share a CDN/object-store representation.  Never expose the
    full response header map because it may contain site-specific secrets.
    """
    headers = headers or {}

    def header(name):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value in (None, ""):
            wanted = str(name).lower()
            try:
                for key, candidate in headers.items():
                    if str(key).lower() == wanted:
                        value = candidate
                        break
            except (AttributeError, TypeError):
                value = None
        return str(value or "").strip()

    result = {}
    for source, target in (("ETag", "server_etag"),
                           ("Last-Modified", "server_last_modified")):
        value = header(source)
        if value:
            result[target] = value[:512]
    # These cache/representation headers explain a common browser-vs-
    # requests discrepancy: a CDN can serve an old object even after the CMS
    # callback returns a new URL.  They are evidence only; never treat a
    # cache header as proof that two URLs share physical storage.
    for source, target, limit in (
            ("Content-Type", "server_content_type", 256),
            ("Content-Encoding", "server_content_encoding", 128),
            ("Cache-Control", "server_cache_control", 512),
            ("Age", "server_age", 64),
            ("Vary", "server_vary", 512),
            ("Content-Length", "server_content_length", 64)):
        value = header(source)
        if value:
            result[target] = value[:limit]
    disposition = header("Content-Disposition")
    if disposition:
        match = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)",
                          disposition, re.I)
        if not match:
            match = re.search(r"filename\s*=\s*\"([^\"]+)\"",
                              disposition, re.I)
        if not match:
            match = re.search(r"filename\s*=\s*([^;]+)", disposition, re.I)
        if match:
            filename = unquote(str(match.group(1) or "").strip().strip('"'))
            if filename:
                result["server_filename"] = filename[:512]
    if not result and resolved_url:
        # URL basename is a fallback clue only; do not treat it as an actual
        # Content-Disposition filename or use it for a success decision.
        basename = os.path.basename(urlparse(str(resolved_url)).path or "")
        if basename:
            result["server_url_basename"] = unquote(basename)[:512]
    return result


class ArticleSnapshot(list):
    """文章列表及其分页完整性证明。

    发布前/后用文章 ID 差集确认新增；若列表只拉到部分页或解析中途失败，
    把普通 list 当成完整快照会将旧同标题文章误判为本次新增。因此完整性必须
    随返回值一起传递，而不能靠“列表非空”猜测。
    """

    def __init__(self, values=(), complete=False):
        super().__init__(values)
        self.complete = bool(complete)


def _article_row_scode(row):
    """从标准/常见二开内容列表行提取栏目 scode；未知返回空串。"""
    for attr in ("data-scode", "data-category-id", "data-catid"):
        value = str(row.get(attr, "") or "").strip()
        if value.isdigit():
            return value
    for field_name in ("scode", "catid", "category_id"):
        field = row.find(attrs={"name": field_name})
        value = str(field.get("value", "") or "").strip() if field else ""
        if value.isdigit():
            return value
    # 标准 PbootCMS 内容列表以 <td title="栏目ID">栏目名</td> 表示栏目。
    for cell in row.find_all("td"):
        value = str(cell.get("title", "") or "").strip()
        if value.isdigit():
            return value
    for anchor in row.find_all("a", href=True):
        href = str(anchor.get("href", "") or "")
        match = re.search(r"(?:^|[/&?])scode(?:/|=)(\d+)(?:[/&#?]|$)", href, re.I)
        if match:
            return match.group(1)
    return ""


def _mcode_from_route(value):
    """兼容路径路由 ``mcode/7`` 与查询路由 ``mcode=7``。"""
    match = re.search(
        r"(?:^|[/&?])mcode(?:/|=)(\d+)(?:[/&#?]|$)",
        str(value or ""), re.I)
    return match.group(1) if match else ""


def _route_from_href(value):
    """Extract a Pboot route from either ``?p=/...`` or a plain path."""
    try:
        parsed = urlparse(str(value or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        route = (query.get("p") or [""])[0] or parsed.path
    except (TypeError, ValueError):
        route = str(value or "")
    return unquote(str(route or "")).strip().lstrip("/")


def _url_origin(value):
    """Return a normalized HTTP(S) origin or ``None`` for an unsafe URL."""
    try:
        raw = str(value or "")
        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if (scheme not in ("http", "https") or not host or
                parsed.username or parsed.password or
                any(ord(char) < 33 for char in raw) or "\\" in raw):
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port
    except (TypeError, ValueError):
        return None


def _same_origin(candidate, reference):
    return bool(_url_origin(candidate) and _url_origin(candidate) == _url_origin(reference))


def _same_host_http_upgrade(candidate, reference):
    """Allow only the harmless GET-side HTTP -> HTTPS canonical redirect."""
    target, source = _url_origin(candidate), _url_origin(reference)
    return bool(target and source and source[0] == "http" and target[0] == "https"
                and source[1] == target[1] and source[2] == 80
                and target[2] == 443)


def _same_origin_or_http_upgrade(candidate, reference):
    """Allow an exact origin or the single safe HTTP→HTTPS upgrade."""
    return _same_origin(candidate, reference) or _same_host_http_upgrade(
        candidate, reference)


def _browser_upload_headers(page_url, *, mode="xhr", policy_headers=None):
    """Build the browser fetch context common to every discovered uploader.

    Requests does not synthesize Chromium's Fetch Metadata headers.  A few
    Pboot extensions use them to distinguish the same-origin XHR used by
    Layui/UEditor from the hidden-iframe form used by ``simpleupload``.
    Keep page-declared headers authoritative while supplying the stable
    browser defaults for the selected upload surface.
    """
    page = urlparse(str(page_url or ""))
    headers = {
        "Referer": str(page_url or ""),
        "Origin": f"{page.scheme}://{page.netloc}",
    }
    if str(mode or "xhr").lower() == "simpleupload":
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "iframe",
        })
    else:
        headers.update({
            "Accept": "*/*",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        })
    headers.update(policy_headers or {})
    return headers


def _editable_form_elements(form):
    """Yield every supported control owned by this form, including UEditor."""
    yield from form_elements(form)

def _choice_text(element):
    """Return the visible caption for one radio/checkbox choice."""
    title = str(element.get("title", "") or "").strip()
    if title:
        return title
    element_id = str(element.get("id", "") or "").strip()
    form = element.find_parent("form")
    if element_id and form:
        label = form.find("label", {"for": element_id})
        if label and label.get_text(" ", strip=True):
            return label.get_text(" ", strip=True)
    parent = element.find_parent("label")
    if parent and parent.get_text(" ", strip=True):
        return parent.get_text(" ", strip=True)
    sibling = element.find_next_sibling()
    if sibling and getattr(sibling, "get_text", None):
        text = sibling.get_text(" ", strip=True)
        if text:
            return text
    return str(element.get("value", "") or "")


def _control_help(element):
    """Extract nearby Pboot/LayUI help text without swallowing the field label."""
    item = element.find_parent(class_=re.compile(
        r"(^|\s)(?:layui-form-item|form-item)(\s|$)"))
    if not item:
        return ""
    for selector in (
            ".layui-form-mid.layui-word-aux", ".layui-word-aux",
            ".help-block", ".form-text", "small"):
        helper = item.select_one(selector)
        if helper:
            text = helper.get_text(" ", strip=True)
            if text:
                return text[:300]
    return ""


def _static_editor_maximum_words(html, target):
    """Read a literal UEditor ``maximumWords`` override without running JS.

    The page-level instance is often embedded as
    ``UE.getEditor('content', {maximumWords: 30000})``.  This is useful for
    the desktop preflight, but it must remain conservative: dynamic
    expressions, runtime setters and server-only defaults are left unknown
    and are still enforced later by the discovered upload policy.
    """
    target = str(target or "").strip()
    if not target:
        return None
    soup = BeautifulSoup(str(html or ""), "html.parser")
    scripts = [script.get_text(" ", strip=False)
               for script in soup.find_all("script")]
    escaped = re.escape(target)
    patterns = (
        rf"getEditor\s*\(\s*['\"]{escaped}['\"][\s\S]{{0,12000}}?"
        rf"maximumWords\s*[:=]\s*['\"]?([0-9]+)",
        rf"(?:^|[{{,;\s])maximumWords\s*[:=]\s*['\"]?([0-9]+)",
    )
    candidates = []
    instance_candidates = []
    for script in scripts:
        for pattern_index, pattern in enumerate(patterns):
            for match in re.finditer(pattern, script, re.I):
                try:
                    value = int(match.group(1))
                except (TypeError, ValueError):
                    continue
                if 0 < value <= 10_000_000:
                    if pattern_index == 0:
                        instance_candidates.append(value)
                    else:
                        candidates.append(value)
            # A target-specific instance match is more authoritative than a
            # page-level public default; do not let a later generic match win.
            if instance_candidates and pattern_index == 0:
                break
    return (instance_candidates[0] if instance_candidates
            else candidates[0] if candidates else None)


def _describe_form_controls(form, label_resolver, submitter=None):
    excluded = {"formcheck", "listall[]", "list[]"}
    excluded.update(e.get("name") for e in form_elements(form)
                    if str(e.get("name", "")).startswith("urls["))
    fields, values = describe_form(form, label_resolver, exclude=excluded,
                                   submitter=submitter)
    # Stock PbootCMS content/category pages commonly use a text input plus a
    # button.upload[data-des] instead of a native file input.  Expose that
    # upload intent to the same app bridge so article edit/publish does not
    # treat a local Windows path as a literal CMS value.
    names = {str(field.get("name", "")) for field in fields
             if field.get("name")}
    upload_targets = {}
    for button in form.find_all(["button", "a", "input"]):
        target = str(button.get("data-des", "") or "").strip()
        classes = " ".join(button.get("class") or [])
        if target and "upload" in classes.lower() and target in names:
            upload_targets[target] = button
    for field in fields:
        name = str(field.get("name", "") or "")
        target = upload_targets.get(name)
        kind = str(field.get("type", field.get("kind", "")) or "").lower()
        if target is not None or kind == "file":
            field["upload_target"] = name
            # The stock ``.uploads`` control is Layui's multiple-file
            # surface even when its paired hidden/text input has no
            # ``multiple`` attribute.  Preserve that browser queue shape in
            # the desktop descriptor; the policy is revalidated before send.
            target_classes = set(target.get("class", [])) if target is not None else set()
            if "uploads" in target_classes:
                field["multiple"] = True
            if target is not None and target.get("accept") and not field.get("accept"):
                field["accept"] = str(target.get("accept"))
    return fields, values


def _form_method_enctype(form, submitter=None):
    """Read explicit form transport settings, retaining CMS POST fallback."""
    attrs = submission_attributes(form, submitter)
    # Existing PbootCMS templates often omit method but submit by POST via JS.
    # Keep that compatibility behavior while honoring any explicit method.
    if not str(form.get('method', '') or '').strip():
        attrs['method'] = 'post'
    return attrs


_SAVE_SUBMITTER_RE = re.compile(
    r"保存|提交|新增|添加|修改|更新|确定|publish|save|submit|add|update|confirm",
    re.I,
)


def _get_write_submitter_allowed(submitter):
    """Whether an explicit GET form has a save-like clicked submitter.

    GET is a valid HTML form method, but CMS list pages use it predominantly
    for search/filter forms.  Dedicated adapters may preserve GET only when
    the page's actual submitter clearly represents a write action; otherwise
    they must hand the page back to the authenticated browser instead of
    guessing which query is a mutation.
    """
    if not isinstance(submitter, dict):
        return False
    text = " ".join(str(submitter.get(key, "") or "")
                    for key in ("name", "value", "formaction"))
    return bool(_SAVE_SUBMITTER_RE.search(text))


def _submitter_values(submitter):
    """Return the successful controls contributed by a clicked submitter."""
    if not isinstance(submitter, dict):
        return {}
    name = str(submitter.get('name', '') or '').strip()
    kind = str(submitter.get('type', 'submit') or 'submit').lower()
    if kind in ('reset', 'button', 'file'):
        return {}
    if kind == 'image':
        prefix = name or ''
        try:
            click_x = int(submitter.get('x', submitter.get('click_x', 0)))
        except (TypeError, ValueError):
            click_x = 0
        try:
            click_y = int(submitter.get('y', submitter.get('click_y', 0)))
        except (TypeError, ValueError):
            click_y = 0
        return {f'{prefix}.x' if prefix else 'x': str(click_x),
                f'{prefix}.y' if prefix else 'y': str(click_y)}
    return {name: submitter.get('value', '')} if name else {}


def _discover_default_submitter(form):
    """Choose the submit control corresponding to the normal web action.

    A browser includes only the button that was actually clicked.  The
    desktop workflow has one explicit publish/edit action, so it cannot pass
    an arbitrary DOM button click.  Prefer a clearly destructive/save action
    (publish/save/submit/add/update) over preview/cancel/delete, and refuse to
    guess when the remaining candidates are tied.  Returning the button's
    formaction/method/enctype is important even when it has no name/value.
    """
    candidates = []
    positive = re.compile(r'发布|保存|提交|新增|添加|修改|更新|确定|publish|save|submit|add|update|confirm', re.I)
    negative = re.compile(r'预览|取消|返回|删除|清空|preview|cancel|back|delete|remove|reset', re.I)
    for index, element in enumerate(form_elements(form)):
        kind = str(element.get('type', '') or '').strip().lower()
        if element.name == 'button':
            kind = kind or 'submit'
        if kind not in ('submit', 'image') or is_disabled(element):
            continue
        text = ' '.join(filter(None, (
            str(element.get('name', '') or ''),
            str(element.get('value', '') or ''),
            element.get_text(' ', strip=True),
            str(element.get('id', '') or ''),
            ' '.join(element.get('class', []) or []),
        )))
        score = 0
        if positive.search(text):
            score += 20
        if negative.search(text):
            score -= 30
        if element.has_attr('formaction'):
            score += 2
        candidates.append((score, -index, element))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda item: (item[0], item[1]))
    best_score, _order, element = candidates[0]
    if len(candidates) > 1 and best_score == candidates[1][0]:
        # A tie means the page has multiple equally plausible write actions;
        # preserving the form's own action is safer than inventing one.
        return None
    return {
        'name': str(element.get('name', '') or ''),
        'type': str(element.get('type', '') or ('submit' if element.name == 'button' else 'submit')).lower(),
        'value': str(element.get('value', '') or ''),
        'formaction': str(element.get('formaction', '') or ''),
        'formmethod': str(element.get('formmethod', '') or ''),
        'formenctype': str(element.get('formenctype', '') or ''),
        'formtarget': str(element.get('formtarget', '') or ''),
        'formnovalidate': bool(element.has_attr('formnovalidate')),
    }


def _discover_submitter_options(form):
    """Expose every browser submitter which a user could click.

    The generic module UI cannot reproduce a real click when a form contains
    equally plausible save/preview buttons.  Returning the safe, inert
    submitter attributes lets the caller choose one explicitly while keeping
    the actual POST construction in the shared browser serializer.
    """
    options = []
    for index, element in enumerate(form_elements(form)):
        kind = str(element.get('type', '') or '').strip().lower()
        if element.name == 'button':
            kind = kind or 'submit'
        if kind not in ('submit', 'image') or is_disabled(element):
            continue
        text = element.get_text(' ', strip=True) or str(element.get('value', '') or '')
        options.append({
            'index': index,
            'label': str(text or element.get('name', '') or '提交').strip()[:160],
            'name': str(element.get('name', '') or ''),
            'type': kind,
            'requires_native_click': kind == 'image',
            'value': str(element.get('value', '') or ''),
            'formaction': str(element.get('formaction', '') or ''),
            'formmethod': str(element.get('formmethod', '') or ''),
            'formenctype': str(element.get('formenctype', '') or ''),
            'formtarget': str(element.get('formtarget', '') or ''),
            'formnovalidate': bool(element.has_attr('formnovalidate')),
        })
    return options


def _submitter_matches(chosen, actual):
    """Compare a caller-selected submitter with a freshly-read DOM button.

    Submitter attributes are page-owned transport semantics.  The UI may only
    send back the inert descriptor returned by the current form; it must not
    be able to smuggle an arbitrary action/method/enctype into a later POST.
    ``formnovalidate`` is included because it changes whether the browser runs
    native constraints before the click is submitted.
    """
    if not isinstance(chosen, dict) or not isinstance(actual, dict):
        return False
    keys = ("name", "type", "value", "formaction", "formmethod",
            "formenctype", "formtarget", "formnovalidate")
    for key in keys:
        left = chosen.get(key, "")
        right = actual.get(key, "")
        if key == "formnovalidate":
            if bool(left) != bool(right):
                return False
        elif str(left or "") != str(right or ""):
            return False
    return True


def _resolve_form_submitter(form, requested=None, context="表单"):
    """Resolve a safe submitter against the freshly-read form DOM.

    A real browser gets this value from the button the user clicked.  The
    desktop bridge can use its deterministic save-button heuristic only when
    it is unique; tied candidates must be selected explicitly by the caller.
    """
    options = _discover_submitter_options(form)
    if requested is not None:
        actual = next((item for item in options
                       if _submitter_matches(requested, item)), None)
        if actual is None:
            return None, options, f"{context}提交按钮已变化，请重新打开并选择实际按钮"
        if str(actual.get('type', '') or '').lower() == 'image':
            # A desktop select box does not produce the physical click point
            # of an image submitter.  Accept coordinates only when a native
            # WebView interaction supplied them explicitly; otherwise hand
            # the operation back to the real page instead of guessing 0,0.
            if not any(key in requested for key in ('x', 'y', 'click_x', 'click_y')):
                return None, options, (
                    f"{context}使用图片提交按钮，桌面端无法取得真实点击坐标；"
                    "请使用原生网页完成提交")
            actual = dict(actual)
            for key in ('x', 'y', 'click_x', 'click_y'):
                if key in requested:
                    actual[key] = requested[key]
        return actual, options, ""
    chosen = _discover_default_submitter(form)
    if chosen is None and len(options) == 1:
        chosen = options[0]
    if chosen is None and len(options) > 1:
        return None, options, f"{context}有多个提交按钮，请先选择实际保存按钮"
    if chosen is not None and str(chosen.get('type', '') or '').lower() == 'image':
        return None, options, (
            f"{context}使用图片提交按钮，桌面端无法取得真实点击坐标；"
            "请使用原生网页完成提交")
    return chosen, options, ""


def _discover_article_form(soup, *, require_business_fields=False):
    """Choose the real content form when a page contains search forms too.

    Pboot pages and custom themes often place a GET filter form before the
    article edit form.  Falling back to ``soup.find('form')`` makes the
    desktop flow serialize the wrong controls and can even POST to a search
    action.  A form is considered only when its id/action/controls identify
    it as the content route; ``formcheck`` and title/content are useful
    signals but are optional because custom models may omit them.  A tied
    candidate is rejected so the caller never guesses a write target.
    """
    forms = soup.find_all('form') if soup is not None else []
    candidates = []
    for index, form in enumerate(forms):
        names = {str(element.get('name', '') or '')
                 for element in form_elements(form)}
        action_lower = action = str(form.get('action', '') or '').strip().lower()
        form_id = str(form.get('id', '') or '').strip().lower()
        method = str(form.get('method', '') or '').strip().lower()
        route_hint = bool(
            form_id == 'edit' or
            re.search(r'(?:^|[/=?])content(?:/|%2f)(?:mod|edit|add)', action_lower, re.I) or
            re.search(r'content(?:sort|/)(?:mod|edit|add)', action_lower, re.I))
        business_hint = bool({'title', 'content', 'scode'} & names)
        # An explicit GET/search form is never a write candidate unless it has
        # the canonical edit id.  This prevents a front-of-page filter from
        # winning merely because it happens to contain a ``title`` input.
        if method == 'get' and form_id != 'edit':
            continue
        if require_business_fields and not (business_hint and route_hint):
            continue
        if not (route_hint or business_hint):
            continue
        submitter = _discover_default_submitter(form)
        score = 0
        if form_id == 'edit':
            score += 100
        if re.search(r'(?:^|[/=?])content(?:/|%2f)(?:mod|edit|add)', action_lower, re.I):
            score += 80
        if re.search(r'/Content/(?:mod|edit|add)(?:/|\b)', action, re.I):
            score += 25
        if 'formcheck' in names:
            score += 20
        if route_hint:
            score += 15
        if business_hint:
            score += 10
        if 'content' in names:
            score += 10
        if 'title' in names:
            score += 5
        if submitter:
            text = ' '.join(str(submitter.get(key, '') or '')
                            for key in ('name', 'value', 'formaction'))
            if re.search(r'发布|保存|提交|新增|添加|修改|更新|确定|publish|save|submit|add|update|confirm',
                         text, re.I):
                score += 20
            if re.search(r'预览|取消|返回|删除|清空|preview|cancel|back|delete|remove|reset',
                         text, re.I):
                score -= 30
        # Forms with no write-route identity are allowed only when they carry
        # business controls; this is needed for minimalist custom templates
        # while still refusing arbitrary search/filter forms.
        candidates.append((score, -index, form))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][2]


def _multipart_file_parts(data, fields):
    """Turn already-uploaded file-control values into multipart text parts.

    The local file bytes have already gone through the page's upload endpoint;
    the final CMS form must still honor an explicit multipart enctype.  Sending
    these values as non-file parts preserves the browser's submitted URL/value
    without leaking local paths or inventing a second upload.
    """
    names = {str(field.get('name', '')) for field in (fields or [])
             if str(field.get('type', field.get('kind', ''))).lower() == 'file'
             and field.get('name') in data}
    if not names:
        return data, []
    clean = dict(data)
    parts = []
    for name in names:
        value = clean.pop(name, '')
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            parts.append((name, (None, text_value(item))))
    return clean, parts


def _submit_content_form(client, method, url, data, fields, enctype, headers,
                         timeout=30):
    """Submit using the discovered method/enctype while keeping old POST path."""
    cancel_callback = getattr(client, "_active_cancel_callback", None)
    if callable(cancel_callback):
        # A synchronous bridge can be cancelled while it is parsing the
        # fresh form.  Check immediately before constructing/sending the
        # actual form request so cancellation cannot turn into a late POST.
        cancel_callback()
    method = str(method or 'post').upper()
    enctype = str(enctype or 'application/x-www-form-urlencoded').lower()
    if method == 'POST' and enctype == 'application/x-www-form-urlencoded':
        sender = getattr(client, '_post_preserving_transport_redirect', None)
        if callable(sender):
            return sender(url, data=data, timeout=timeout, headers=headers)
        request_sender = getattr(client, '_request', None)
        if callable(request_sender):
            return request_sender('POST', url, data=data, timeout=timeout,
                                  headers=headers)
        return request_with_redirects(client.session, 'POST', url,
                                      data=data, timeout=timeout, headers=headers)
    if method == 'GET':
        return request_with_redirects(client.session, 'GET', url,
                                      params=data, timeout=timeout, headers=headers)
    if enctype == 'text/plain':
        # Native HTML text/plain forms submit one ``name=value`` line per
        # successful control.  Preserve browser order and repeated names
        # instead of allowing requests to choose URL encoding implicitly.
        plain_pairs = data if isinstance(data, (list, tuple)) else [
            (name, item)
            for name, value in (data or {}).items()
            for item in (value if isinstance(value, (list, tuple)) else [value])]
        payload = ''.join(f'{text_value(name)}={text_value(value)}\r\n'
                          for name, value in plain_pairs).encode('utf-8')
        plain_headers = dict(headers or {})
        plain_headers['Content-Type'] = 'text/plain;charset=UTF-8'
        sender = getattr(client, '_request', None)
        if callable(sender):
            return sender(method, url, data=payload, timeout=timeout,
                          headers=plain_headers)
        return request_with_redirects(client.session, method, url, data=payload,
                                      timeout=timeout, headers=plain_headers)
    # Keep repeated successful controls when the caller supplies browser-order
    # pairs (the reply workflow and dynamic forms both do this).  A plain
    # ``dict(pairs)`` silently kept only the last value and made multipart
    # submissions differ from a browser's FormData ordering.
    if isinstance(data, BrowserFormData):
        ordered_pairs = data.browser_pairs()
        body = data
    elif isinstance(data, dict):
        ordered_pairs = []
        for name, value in data.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            ordered_pairs.extend((str(name), item) for item in values)
        body = dict(data)
    else:
        ordered_pairs = [(str(name), value) for name, value in (data or [])]
        body = {}
        for name, value in ordered_pairs:
            if name in body:
                if not isinstance(body[name], list):
                    body[name] = [body[name]]
                body[name].append(value)
            else:
                body[name] = value
    files = None
    empty_multipart_payload = None
    empty_multipart_headers = None
    if enctype == 'multipart/form-data':
        # requests appends ``files`` after ``data``.  Put every successful
        # control into the multipart list as a non-file part so the emitted
        # parts stay in the exact browser DOM order, including duplicate names
        # interleaved with other controls.  Values for native file controls
        # are already server-side URLs from the page upload endpoint; they
        # must remain text values and must never expose a local path.
        field_orders = {}
        file_descriptors = {}
        disabled_file_names = set()
        for field_index, field in enumerate(fields or []):
            name = str(field.get('name', '') or '').strip()
            kind = str(field.get('type', field.get('kind', '')) or '').lower()
            if not name:
                continue
            order = int(field.get('_dom_order', field_index) or 0)
            field_orders.setdefault(name, order)
            if kind == 'file' and field.get('disabled'):
                disabled_file_names.add(name)
            if kind == 'file' and not field.get('disabled') and name not in file_descriptors:
                # Older callers provide descriptors without the private DOM
                # position.  Their descriptor order is still a safer hint
                # than appending a missing file part after every text field.
                raw_orders = field.get('_file_dom_orders') or [order]
                if not isinstance(raw_orders, (list, tuple)):
                    raw_orders = [raw_orders]
                orders = []
                for raw_order in raw_orders:
                    try:
                        orders.append(int(raw_order))
                    except (TypeError, ValueError):
                        orders.append(order)
                file_descriptors[name] = (orders or [order], field)

        # A native empty file input is a successful browser control.  It is
        # represented by an empty File (filename="", octet-stream), not by a
        # missing field and not by a local path.  Values returned by a page's
        # upload endpoint are deliberately kept as ordinary text URL parts;
        # only an empty value is converted to the native empty File shape.
        def _empty_file_part(name):
            return (str(name), ('', b'', 'application/octet-stream'))

        # BrowserFormData appends values for controls that were deliberately
        # absent from the original successful-pair list (notably native file
        # controls).  Pull those values aside and merge them back at their
        # field's DOM position so a selected URL occupies the earliest slot
        # and untouched repeated controls receive empty File entries after it.
        file_values = {}
        non_file_pairs = []
        for name, value in ordered_pairs:
            key = str(name)
            if key in disabled_file_names:
                continue
            if key in file_descriptors:
                file_values.setdefault(key, []).append(value)
            else:
                non_file_pairs.append((key, value))

        file_entries = []
        for name, (orders, _field) in file_descriptors.items():
            values = file_values.get(name, [])
            for index, value in enumerate(values):
                order = orders[min(index, len(orders) - 1)]
                file_entries.append((order, index, name, value))
            # Existing values occupy the earliest same-name controls, just as
            # FormData does.  Any later untouched controls need empty Files.
            for index, order in enumerate(orders[len(values):], start=len(values)):
                file_entries.append((order, index, name, None))
        file_entries.sort(key=lambda item: (item[0], item[1]))

        parts = []
        file_index = 0
        for name, value in non_file_pairs:
            part_order = field_orders.get(str(name), float('inf'))
            while file_index < len(file_entries):
                file_order, _slot, file_name, file_value = file_entries[file_index]
                before = (file_order < part_order or
                          (file_order == part_order and file_name != str(name)))
                if not before:
                    break
                parts.append(_empty_file_part(file_name) if
                             file_value is None or text_value(file_value) == ''
                             else (file_name, (None, text_value(file_value))))
                file_index += 1
            parts.append((str(name), (None, text_value(value))))
        while file_index < len(file_entries):
            _order, _slot, file_name, file_value = file_entries[file_index]
            parts.append(_empty_file_part(file_name) if
                         file_value is None or text_value(file_value) == ''
                         else (file_name, (None, text_value(file_value))))
            file_index += 1
        files = parts
        body = {}
        # ``requests`` only selects multipart encoding when ``files`` is
        # truthy.  A real browser still sends multipart/form-data for an
        # explicit enctype with no successful controls; retain a harmless
        # empty file-control part when the form declares one.
        if not files:
            # ``requests`` only selects multipart encoding when ``files`` is
            # truthy.  A browser still emits a valid empty multipart body for
            # a form with no successful controls; create that body directly
            # instead of inventing a synthetic field name.
            files = None
            boundary = '----PbootPublisher' + uuid.uuid4().hex
            empty_multipart_payload = f'--{boundary}--\r\n'.encode('ascii')
            empty_multipart_headers = dict(headers or {})
            empty_multipart_headers['Content-Type'] = (
                f'multipart/form-data; boundary={boundary}')
    # Reuse the client's request wrapper when available so multipart and
    # non-default form methods receive the same same-origin ``Origin`` and
    # network policy as ordinary CMS writes.  Small test/dedicated clients
    # without that wrapper retain the bounded redirect transport fallback.
    sender = getattr(client, '_request', None)
    if callable(sender):
        if empty_multipart_payload is not None:
            return sender(method, url, data=empty_multipart_payload, files=None,
                          timeout=timeout, headers=empty_multipart_headers)
        return sender(method, url, data=body, files=files,
                      timeout=timeout, headers=headers)
    if empty_multipart_payload is not None:
        return request_with_redirects(
            client.session, method, url, data=empty_multipart_payload,
            files=None, timeout=timeout, headers=empty_multipart_headers)
    return request_with_redirects(client.session, method, url, data=body,
                                  files=files, timeout=timeout, headers=headers)

def _response_error_message(text):
    """Extract a real server-side error before considering success markers.

    Full list pages can legitimately contain words such as "error" in article
    titles, so unstructured whole-page matching is limited to short responses.
    """
    text = str(text or "")
    if _is_login_page(text):
        return "登录会话已失效，请重新登录"
    soup = BeautifulSoup(text, "html.parser")
    for element in soup.find_all(class_=re.compile(
            r"alert-danger|text-danger|layui-layer-content|(?:^|[-_])error(?:$|[-_])",
            re.I)):
        message = element.get_text(" ", strip=True)
        classes = " ".join(element.get("class") or [])
        if message and (_RESPONSE_ERROR_RE.search(message)
                        or re.search(r"danger|error", classes, re.I)):
            return message[:300]
    plain = soup.get_text(" ", strip=True)
    if len(plain) <= 500:
        match = _RESPONSE_ERROR_RE.search(plain)
        if match:
            return plain[:300]
    return ""


def _response_success_message(text):
    """Return a structured/short positive message, never a random page token."""
    text = str(text or "")
    soup = BeautifulSoup(text, "html.parser")
    for element in soup.find_all(class_=re.compile(
            r"alert-success|layui-layer-content|(?:^|[-_])success(?:$|[-_])",
            re.I)):
        message = element.get_text(" ", strip=True)
        if message and _RESPONSE_SUCCESS_RE.search(message):
            return message[:300]
    plain = soup.get_text(" ", strip=True)
    if len(plain) <= 500 and _RESPONSE_SUCCESS_RE.search(plain):
        return plain[:300]
    return ""


class ContentMixin:
    def _read_request(self, method, url, **kwargs):
        """Issue a read with the shared read-only flag when supported.

        A few lightweight compatibility clients used by integrations and
        older plugins expose a narrower ``_request`` signature.  Keep those
        clients working while the production client still receives the
        explicit read-only retry policy.
        """
        requester = getattr(self, "_request", None)
        if not callable(requester):
            # A small legacy/plugin client may mix in ContentMixin without
            # inheriting PbootCMSClient.  Still route its reads through the
            # bounded redirect transport instead of silently using
            # ``Session.get`` with arbitrary redirects.
            return request_with_redirects(
                self.session, method, url, **kwargs)
        try:
            return requester(method, url, read_only=True, **kwargs)
        except TypeError as exc:
            message = str(exc).lower()
            if 'unexpected keyword' not in message:
                raise
            # Older integrations supplied a narrow compatibility `_request`
            # shim (often only ``method, url, timeout``).  Production's
            # PbootCMSClient accepts **kwargs and never takes this branch;
            # retain the adapter contract without dropping query parameters
            # or falling back to an unguarded request in the real client.
            try:
                signature = inspect.signature(self._request)
                parameters = signature.parameters
                accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD
                                     for item in parameters.values())
            except (TypeError, ValueError):
                accepts_kwargs = True
                parameters = {}
            if accepts_kwargs:
                return requester(method, url, **kwargs)
            filtered = {key: value for key, value in kwargs.items()
                        if key in parameters}
            missing = set(kwargs) - set(filtered)
            if missing and set(missing) != {"params"}:
                # ``read_only`` was the only unsupported option in the usual
                # shim.  Any other unsupported transport option is unsafe to
                # silently discard.
                raise
            if not missing:
                return requester(method, url, **filtered)
            sender = getattr(self, "session", None)
            sender = getattr(sender, str(method or "GET").lower(), None)
            if not callable(sender):
                raise
            # This path exists solely for legacy test/plugin sessions whose
            # `_request` cannot express query parameters.  They own their
            # session adapter and receive the exact params the browser would
            # have sent; the production client never uses it.
            return sender(url, **kwargs)

    def _content_mcode_candidates(self):
        """从当前后台菜单发现内容模型；仅在发现失败时使用兼容候选。

        模型编号是后台数据，不应假定永远只有 2—7。两个已诊断站点均在
        Index/home 菜单暴露真实 mcode；二开站使用 ``mcode=`` 查询写法也支持。
        """
        site = str(getattr(self, "admin_url", "") or "").rstrip("/").lower()
        cached = getattr(self, "_content_mcode_context", None)
        if (isinstance(cached, tuple) and len(cached) == 2
                and cached[0] == site):
            return list(cached[1])

        discovered = []
        try:
            response = self._read_request("GET", self._url("Index/home"),
                                          timeout=15)
            if response.ok and not _is_login_page(response.text):
                soup = BeautifulSoup(response.text, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href", "") or "")
                    route = _route_from_href(href)
                    # ``Index/home`` also contains Single/Slide/other links
                    # whose mcode is not a content model.  Feeding those
                    # numbers into the content resolver made a stock Pboot
                    # install pick mcode=1 for a product/category that was
                    # actually linked as mcode=3.  Only content list/add
                    # routes are valid model candidates; the category-link
                    # resolver below supplies the exact scode mapping.
                    if route and not re.match(r"^Content/(?:index|add)(?:/|$)",
                                              route, re.I):
                        continue
                    mcode = _mcode_from_route(href)
                    if mcode and mcode not in discovered:
                        discovered.append(mcode)
        except Exception as exc:
            debug_log(f"[mcode] 后台菜单模型发现失败: {exc}")

        if not discovered:
            # 兼容菜单被权限/二开脚本隐藏的旧站。每个候选仍会通过目标 scode
            # 是否真实出现在表单中来验证，因此不会仅凭编号直接提交。
            discovered = list(dict.fromkeys((*MCODE_ORDER, "1")))
            debug_log(f"[mcode] 使用兼容候选（仍需表单验证）: {discovered}")
        self._content_mcode_context = (site, tuple(discovered))
        return discovered

    def _category_mcode_from_routes(self, scode):
        """Read the CMS' explicit ``scode -> mcode`` links when available.

        Pboot's add/list forms commonly expose every category in a shared
        ``<select name=scode>`` regardless of the active model.  Choosing the
        first model whose select contains the value therefore is not enough:
        it can post a product into the article model.  The category tree
        itself links each row to ``Content/index/mcode/X&scode=Y`` (or the
        equivalent slash/query route), which is the browser's authoritative
        mapping.  This is a read-only hint; if a custom installation hides
        the tree, the existing candidate probing remains the conservative
        fallback.
        """
        key = str(scode or "").strip()
        if not key:
            return None
        cache = getattr(self, "_category_mcode_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._category_mcode_cache = cache
        if key in cache:
            value = str(cache.get(key) or "").strip()
            return value or None
        resolved = ""
        try:
            url = self._url("ContentSort/index")
            response = self._read_request("GET", url, timeout=45)
            if response.ok and not _is_login_page(getattr(response, "text", "")):
                reference = getattr(response, "url", "") or url
                soup = BeautifulSoup(response.text, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    raw = str(anchor.get("href", "") or "").strip()
                    if not raw or raw.lower().startswith("javascript:"):
                        continue
                    candidate = urljoin(reference, raw)
                    if not (_same_origin(candidate, self.base_url or self.admin_url)
                            or _same_host_http_upgrade(candidate,
                                                       self.base_url or self.admin_url)):
                        continue
                    route = _route_from_href(candidate)
                    if not re.match(r"^Content/index(?:/|$)", route, re.I):
                        continue
                    parsed = urlparse(candidate)
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    route_value = str((query.get("p") or [""])[0] or "")
                    route_query = parse_qs(urlparse("https://route.invalid/?" +
                                                     route_value.split("?", 1)[-1]).query,
                                           keep_blank_values=True)
                    values = [str(item) for item in query.get("scode", [])]
                    values.extend(str(item) for item in route_query.get("scode", []))
                    # Some themes put /scode/12 in the p route instead of a
                    # separate query parameter.  The helper accepts both.
                    match = re.search(r"(?:^|[/&?])scode(?:/|=)(\d+)(?:[/&#?]|$)",
                                      route_value, re.I)
                    if match:
                        values.append(match.group(1))
                    if key not in values:
                        continue
                    mcode = _mcode_from_route(candidate)
                    if mcode:
                        resolved = str(mcode)
                        break
        except Exception as exc:
            debug_log(f"[_category_mcode_from_routes] scode={key} 读取失败: {exc}")
        cache[key] = resolved
        return resolved or None

    def get_categories(self):
        """获取栏目列表，返回 [{"id": ..., "name": ...}, ...]
        从侧边栏提取mcode，然后从添加内容页的<select name='scode'>提取栏目
        """
        categories = []
        mcodes = set(self._content_mcode_candidates())

        # 方法2：从添加内容页的<select name='scode'>提取栏目
        for mcode in sorted(mcodes):
            try:
                add_url = self._url(f"Content/add/mcode/{mcode}")
                resp = self._read_request("GET", add_url, timeout=15)
                if not resp.ok:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                # 查找所有 name="scode" 的 select
                for sel in soup.find_all("select", {"name": "scode"}):
                    for opt in sel.find_all("option"):
                        val = opt.get("value", "").strip()
                        name = opt.get_text(strip=True)
                        if val and val.isdigit() and name:
                            # 去重
                            if not any(c["id"] == val for c in categories):
                                categories.append({"id": val, "name": name})
                    # 只需要第一个scode select
                    break
            except Exception as _e:
                debug_log("[error] " + str(_e))

        # 方法3：如果还是没找到，从内容列表页解析
        if not categories:
            for mcode in sorted(mcodes):
                try:
                    list_url = self._url(f"Content/index/mcode/{mcode}")
                    resp = self._read_request("GET", list_url, timeout=15)
                    if not resp.ok:
                        continue
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for sel in soup.find_all("select", {"name": "scode"}):
                        for opt in sel.find_all("option"):
                            val = opt.get("value", "").strip()
                            name = opt.get_text(strip=True)
                            if val and val.isdigit() and name:
                                if not any(c["id"] == val for c in categories):
                                    categories.append({"id": val, "name": name})
                except Exception as _e:
                    debug_log("[error] " + str(_e))

        return categories

    def get_category_tree(self):
        """从栏目管理页获取栏目树结构
        返回: [{"id": ..., "name": ..., "children": [...]}, ...]
        使用 data-tt-id / data-tt-parent-id 属性构建真实层级
        """
        tree = []
        try:
            url = self._url("ContentSort/index")
            debug_log(f"[get_category_tree] url={url} admin_url={self.admin_url}")
            # 栏目数 300+ 时服务器生成页面需较长时间，使用 90s 超时
            resp = self._request("GET", url, timeout=90, retries=0)
            if not resp.ok:
                debug_log(f"[get_category_tree] HTTP {resp.status_code}")
                return tree
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.find_all("tr")
            debug_log(f"[get_category_tree] parsed {len(rows)} table rows")
            all_nodes = {}
            for row in rows:
                cb = row.find("input", {"name": "list[]"})
                if not cb:
                    continue
                cid = cb.get("value", "").strip()
                if not cid or not cid.isdigit():
                    continue
                tt_id = row.get("data-tt-id", "").strip() or cid
                parent_tt_id = row.get("data-tt-parent-id", "").strip()
                parent_tt_id = parent_tt_id if parent_tt_id and parent_tt_id != "0" else None
                td_name = row.find_all("td")
                if len(td_name) < 2:
                    continue
                from bs4 import NavigableString as _NS
                name_td = td_name[1]
                name = " ".join(
                    t.strip() for t in name_td.children
                    if isinstance(t, _NS) and t.strip()
                ).strip()
                if not name:
                    continue
                all_nodes[tt_id] = {
                    "id": cid,
                    "name": name,
                    "parent_tt_id": parent_tt_id,
                    "children": []
                }
            for node_id, node in all_nodes.items():
                p = node["parent_tt_id"]
                if p and p in all_nodes:
                    all_nodes[p]["children"].append(node)
                else:
                    tree.append(node)
            debug_log(f"[get_category_tree] built tree: {len(tree)} root nodes, {len(all_nodes)} total")
        except Exception as e:
            debug_log(f"[get_category_tree] error: {e}")
        return tree

    def get_category_url_paths(self, scodes, cancel_callback=None,
                               listing_timeout=90, form_timeout=15,
                               resolve_mcode=True):
        """读取栏目 URL 名称，返回 ``{scode: path}``。

        产品前台地址受栏目 URL 名称影响（例如 ``products/145.html``）。栏目名称
        不能用于猜 URL，因此只读取栏目编辑表单里的 filename/urlname；读取失败时
        留空，让上层保留已解析到的真实链接或降级处理。
        """
        result = {}
        requested = list(dict.fromkeys(
            str(x or "").strip() for x in (scodes or [])))
        # 栏目修改路由在不同 PbootCMS 版本中可能带 mcode。优先消费栏目
        # 管理页真实链接，避免固定猜 ContentSort/mod/id/{id}（两套实站之一
        # 对该旧路由返回 404）。
        edit_urls = {}
        try:
            if cancel_callback:
                cancel_callback()
            listing = self._read_request(
                "GET", self._url("ContentSort/index"), timeout=listing_timeout)
            if cancel_callback:
                cancel_callback()
            if listing.ok and not _is_login_page(listing.text):
                soup = BeautifulSoup(listing.text, "html.parser")
                reference = self.base_url or self.admin_url
                for row in soup.find_all("tr"):
                    checkbox = row.find("input", {"name": "list[]"})
                    cid = str(checkbox.get("value", "") or "").strip() \
                        if checkbox else ""
                    if not cid.isdigit() or cid not in requested:
                        continue
                    best_candidate = None
                    best_rank = None
                    for anchor in row.find_all("a", href=True):
                        href = str(anchor.get("href", "") or "").strip()
                        low = href.lower()
                        if "contentsort/mod" not in low:
                            continue
                        # A ContentSort ``field/.../value/...`` link is a
                        # state-toggle action, not an edit form.  Never GET
                        # it while merely resolving a category URL: aside
                        # from returning a redirect instead of the form, it
                        # can change the category's status.
                        if (re.search(r"(?:^|[/&?])field(?:/|=)", low)
                                or re.search(r"(?:^|[/&?])value(?:/|=)", low)):
                            continue
                        id_match = re.search(
                            rf"(?:^|[/&?])id(?:/|=){re.escape(cid)}(?:[/&#?]|$)",
                            href, re.I)
                        scode_match = re.search(
                            rf"(?:^|[/&?])scode(?:/|=){re.escape(cid)}(?:[/&#?]|$)",
                            href, re.I)
                        if not (id_match or scode_match):
                            continue
                        candidate = urljoin(getattr(listing, "url", "")
                                            or self.admin_url, href)
                        if not (_same_origin(candidate, reference)
                                or _same_host_http_upgrade(candidate, reference)):
                            continue
                        # Prefer the conventional /id/{id} edit endpoint;
                        # retain a safe /scode/{id} endpoint only for CMS
                        # variants that use that route for the form.
                        rank = 0 if id_match else 1
                        if best_rank is None or rank < best_rank:
                            best_candidate, best_rank = candidate, rank
                    if best_candidate:
                        edit_urls[cid] = best_candidate
        except Cancelled:
            raise
        except Exception as exc:
            debug_log(f"[category_url] 栏目管理页真实编辑链接读取失败: {exc}")

        for scode in requested:
            if not scode or not scode.isdigit():
                continue
            try:
                if cancel_callback:
                    cancel_callback()
                value = ""
                candidates = [edit_urls.get(scode, "")]
                if resolve_mcode:
                    mcode = self._resolve_mcode(scode)
                    if mcode:
                        candidates.append(self._url(
                            f"ContentSort/mod/mcode/{mcode}/id/{scode}"))
                candidates.append(self._url(f"ContentSort/mod/id/{scode}"))
                for candidate in dict.fromkeys(x for x in candidates if x):
                    if cancel_callback:
                        cancel_callback()
                    resp = self._read_request("GET", candidate,
                                               timeout=form_timeout)
                    if cancel_callback:
                        cancel_callback()
                    if not resp.ok or _is_login_page(resp.text):
                        continue
                    form = BeautifulSoup(resp.text, "html.parser")
                    for name in ("filename", "urlname"):
                        field = form.find(attrs={"name": name})
                        if field:
                            value = (field.get("value") or
                                     field.get_text() or "").strip()
                        if value:
                            break
                    if value:
                        break
                # 只允许相对 URL 路径；外链栏目不能作为产品详情页前缀。
                value = value.replace("\\", "/").strip("/")
                if value and "://" not in value and "?" not in value and "#" not in value:
                    result[scode] = value
            except Cancelled:
                raise
            except Exception as exc:
                debug_log(f"[category_url] scode={scode} 读取失败: {exc}")
        return result

    def get_content_form(self, scode):
        """获取指定栏目的添加内容表单字段，返回 (formcheck, fields)
        列表页取完整表单字段，再访问新增页补充UEditor字段
        """
        # 这些是当前真实新增表单的浏览器 submitter 快照；清掉旧页面
        # 的值，避免切换栏目后把上一页的 action/method 带到新页面。
        self._content_add_submitter_options = []
        self._content_add_submitter = None
        self._content_add_page_url = ""
        # A runtime-owned form must continue in the real browser.  Keep the
        # exact server-resolved page URL so the API does not have to invent a
        # route when a custom model renders controls through JavaScript.
        self._content_add_native_url = ""
        self._content_add_native_reason = ""
        # 先确定 mcode（统一解析 + 缓存，避免重复探测）
        mcode = self._resolve_mcode(scode)
        if mcode is None:
            raise Exception(f"无法确定栏目 {scode} 所属的内容模型(mcode)")
        # 解析该 mcode 列表页的表单（mcode 已缓存，仅 1 次请求）
        try:
            resp = self._read_request(
                "GET", self._url(f"Content/index/mcode/{mcode}"), timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as _e:
            raise Exception(f"获取栏目 {scode} 表单失败: {_e}")

        # 只解析新增/编辑表单（id="edit" 或 action 包含 /Content/add/）
        edit_form = _discover_article_form(soup)
        if not edit_form:
            raise Exception("未找到新增内容表单（id=edit）")
        from admin_modules import _dynamic_form_reason
        native_add_url = self._url(f"Content/add/mcode/{mcode}")
        native_add_url += ("&" if "?" in native_add_url else "?") + urlencode({"scode": scode})
        dynamic_reason = _dynamic_form_reason(soup, edit_form,
                                              getattr(resp, "url", "") or self.admin_url)
        if dynamic_reason:
            self._content_add_native_url = native_add_url
            self._content_add_native_reason = dynamic_reason
            raise Exception(dynamic_reason + "，请使用原生网页发布")

        # 记住这个模型真实的新增表单 action。二开 PbootCMS
        # 不一定仍使用默认 Content/add 路由，发布时应消费
        # 已读到的表单结构，而不是再猜一个写入端点。
        action = str(edit_form.get("action", "") or "").strip()
        if action:
            requested_url = getattr(resp, "url", "") or self._url(
                f"Content/index/mcode/{mcode}")
            candidate_action = urljoin(requested_url, action)
            reference = self.base_url or self.admin_url
            if (_same_origin(candidate_action, reference)
                    or _same_host_http_upgrade(candidate_action, reference)):
                if not hasattr(self, "_content_add_actions"):
                    self._content_add_actions = {}
                self._content_add_actions[str(mcode)] = candidate_action
            else:
                debug_log(f"[content_form] 拒绝跨站新增表单action: {candidate_action}")

        # CSRF token
        formcheck = ""
        fc = edit_form.find("input", {"name": "formcheck"})
        if fc:
            formcheck = fc.get("value", "")

        default_submitter = _discover_default_submitter(edit_form)
        self._content_add_submitter = default_submitter
        self._content_add_submitter_options = _discover_submitter_options(edit_form)
        fields, _defaults = _describe_form_controls(
            edit_form, self._find_label, submitter=default_submitter)

        # 额外请求新增页面：它通常比列表页内嵌表单更完整，合并真实
        # select/radio/必填/帮助信息，并补充 UEditor 扩展字段（如 ext_xqt）。
        editor_source_html = resp.text
        try:
            add_resp = self._read_request(
                "GET", self._url(f"Content/add/mcode/{mcode}"),
                params={"scode": scode}, timeout=15)
            if add_resp.ok:
                editor_source_html = add_resp.text
                add_soup = BeautifulSoup(add_resp.text, "html.parser")
                add_form = _discover_article_form(add_soup)
                if add_form:
                    add_dynamic_reason = _dynamic_form_reason(
                        add_soup, add_form,
                        getattr(add_resp, "url", "") or self.admin_url)
                    if add_dynamic_reason:
                        self._content_add_native_url = native_add_url
                        self._content_add_native_reason = add_dynamic_reason
                    if add_dynamic_reason:
                        # Do not merge a partial static snapshot.  The real
                        # page owns the editor/upload lifecycle.
                        add_form = None
                    if add_form:
                        resolved_add_url = str(
                            getattr(add_resp, "url", "") or
                            self._url(f"Content/add/mcode/{mcode}")
                        ).strip()
                        if (_same_origin(resolved_add_url,
                                         self.base_url or self.admin_url) or
                                _same_host_http_upgrade(
                                    resolved_add_url,
                                    self.base_url or self.admin_url)):
                            self._content_add_page_url = resolved_add_url
                        add_default_submitter = _discover_default_submitter(add_form)
                        self._content_add_submitter = add_default_submitter
                        self._content_add_submitter_options = _discover_submitter_options(add_form)
                        add_fields, _add_defaults = _describe_form_controls(
                            add_form, self._find_label,
                            submitter=add_default_submitter)
                        merged = {field["name"]: field for field in fields}
                        order = [field["name"] for field in fields]
                        for field in add_fields:
                            if field["name"] not in merged:
                                order.append(field["name"])
                            # The dedicated add page is authoritative when both
                            # surfaces expose the same control.
                            merged[field["name"]] = field
                        fields = [merged[name] for name in order]
                        add_action = str(add_form.get("action", "") or "").strip()
                        if add_action:
                            candidate_action = urljoin(
                                getattr(add_resp, "url", "") or self.admin_url,
                                add_action)
                            reference = self.base_url or self.admin_url
                            if (_same_origin(candidate_action, reference) or
                                    _same_host_http_upgrade(candidate_action, reference)):
                                if not hasattr(self, "_content_add_actions"):
                                    self._content_add_actions = {}
                                self._content_add_actions[str(mcode)] = candidate_action
        except Exception as _e:
            if not self._content_add_native_url:
                debug_log("[error] " + str(_e))
        if self._content_add_native_url:
            raise Exception(self._content_add_native_reason or
                            "新增内容表单依赖动态网页脚本，请使用原生网页发布")
        # Expose only literal page-level editor limits to the UI.  The full
        # upload-policy discovery still reads the effective server config in
        # the worker, so a dynamic/runtime override can never be mistaken for
        # a proven limit here.
        for field in fields:
            name = str(field.get("name", "") or "").strip()
            if not name or name.lower() not in {
                    "content", "body", "detail", "details", "article"}:
                continue
            limit = _static_editor_maximum_words(editor_source_html, name)
            if limit is not None:
                field["maximum_words"] = limit
        return formcheck, fields

    def _find_label(self, soup, el, name):
        """尝试查找字段的label文本"""
        # 方法1: for属性匹配
        el_id = el.get("id", "")
        if el_id:
            lbl = soup.find("label", {"for": el_id})
            if lbl:
                return lbl.get_text(strip=True)
        # PbootCMS/LayUI forms usually put the caption in a sibling
        # .layui-form-label rather than a HTML <label for="...">.
        form_item = el.find_parent(class_=re.compile(r"(^|\s)layui-form-item(\s|$)"))
        if form_item:
            lbl = form_item.find(class_=re.compile(r"(^|\s)layui-form-label(\s|$)"))
            if lbl and lbl.get_text(strip=True):
                return lbl.get_text(" ", strip=True)
        # Table-based custom-field layouts commonly place the caption in the
        # preceding cell of the same row.
        row = el.find_parent("tr")
        if row:
            cell = el.find_parent(["td", "th"])
            cells = row.find_all(["th", "td"], recursive=False)
            if cell in cells:
                index = cells.index(cell)
                if index > 0:
                    text = cells[index - 1].get_text(" ", strip=True)
                    if text:
                        return text
        # 方法2: 父级dt/th
        parent = el.parent
        if parent:
            prev = parent.find_previous("dt") or parent.find_previous("th")
            if prev:
                return prev.get_text(strip=True)
        # 方法3: 通用字段名映射
        name_map = {
            "title": "标题", "subtitle": "副标题", "filename": "URL名称",
            "ico": "缩略图", "content": "内容", "description": "描述",
            "tags": "标签", "author": "作者", "source": "来源",
            "date": "发布日期", "scode": "栏目", "subscode": "子栏目",
        }
        if name in name_map:
            return name_map[name]
        # ext_ 开头显示原始名
        if name.startswith("ext_"):
            return name.replace("ext_", "")
        return name

    def prepare_uploads(self, page_url, targets):
        """Refresh all needed control policies before the first file is sent."""
        self._upload_policies = {}
        page_url = safe_url(page_url, self.admin_url)
        self._upload_policies = discover_policies(self.session, page_url, targets)

    def apply_upload_policy_metadata(self, fields):
        """Apply only static shape hints discovered from native upload widgets.

        Stock Layui pages can bind a ``.uploads`` (multiple-file) button to a
        plain text input, so the DOM control itself has no ``multiple``
        attribute.  The browser still appends every callback result.  The
        freshly discovered policy is authoritative for this shape; dynamic
        callbacks are never executed here.
        """
        policies = getattr(self, "_upload_policies", {}) or {}
        for field in fields or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name", "") or "").strip()
            if not name:
                continue
            policy = policies.get(("field", name))
            if policy is None:
                continue
            metadata = getattr(policy, "metadata", {}) or {}
            if metadata.get("multiple") is True:
                field["multiple"] = True
            try:
                queue_limit = int(metadata.get("number") or 0)
            except (TypeError, ValueError):
                queue_limit = 0
            if queue_limit > 0:
                field["max_files"] = queue_limit
            if not str(field.get("accept", "") or "").strip():
                # Layui's ``acceptMime`` is the browser-side MIME filter and
                # is more precise than the presentation value ``images``/
                # ``file``.  Preserve it when declared, otherwise retain the
                # native accept mode for compatibility with stock controls.
                field["accept"] = str(metadata.get("accept_mime") or
                                      metadata.get("accept") or "")
        return fields

    @staticmethod
    def _remote_catcher_pending(value):
        """Return True only for an explicit asynchronous catcher response."""
        if not isinstance(value, dict):
            return False
        scopes = [value]
        if isinstance(value.get("data"), dict):
            scopes.append(value["data"])
        words = re.compile(r"处理中|排队|异步|processing|pending|queued|queue|async|running", re.I)
        for item in scopes:
            state = str(item.get("state") or item.get("status") or
                        item.get("job_status") or item.get("process_status") or "").strip()
            if state.lower() in {"pending", "processing", "queued", "running", "async"}:
                return True
            for key in ("processing", "pending", "async", "asynchronous"):
                raw = item.get(key)
                if raw is True or str(raw).strip().lower() in {
                        "1", "true", "yes", "pending", "processing", "queued"}:
                    return True
            for key in ("message", "msg", "notice", "warning"):
                raw = item.get(key)
                if isinstance(raw, str) and words.search(raw):
                    return True
        return False

    def _poll_remote_catcher(self, response, value, page_url, *, attempts=12,
                             interval=0.5):
        """Poll a catcher job using GET only; never replay the source POST."""
        # ``catch_remote_images`` snapshots response headers before closing
        # the upload response.  Accepting either that mapping or a response
        # object keeps compatibility with older callers/tests.
        headers = (response if isinstance(response, dict) else
                   getattr(response, "headers", {}) or {})
        poll = ""
        for name in ("Location", "X-Status-URL", "X-Status-Url",
                     "X-Processing-URL", "X-Processing-Url"):
            candidate = headers.get(name)
            if isinstance(candidate, str) and candidate.strip():
                poll = candidate.strip()
                break
        if not poll and isinstance(value, dict):
            for key in ("status_url", "poll_url", "processing_url",
                        "process_url", "job_url"):
                candidate = value_for(value, key)
                if isinstance(candidate, str) and candidate.strip():
                    poll = candidate.strip()
                    break
        if not poll:
            return None
        try:
            target = urljoin(str(page_url or self.base_url or self.admin_url), poll)
            if not _same_origin_or_http_upgrade(target, self.base_url or self.admin_url):
                raise UploadPolicyError("远程图片抓取状态地址跨站，已停止轮询")
            parsed = urlparse(target)
            poll_headers = {
                "Referer": str(page_url or ""),
                "Cache-Control": "no-cache", "Pragma": "no-cache",
                "Accept": "application/json, */*",
            }
            if parsed.scheme and parsed.netloc:
                poll_headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        except (TypeError, ValueError):
            raise UploadPolicyError("远程图片抓取状态地址无效，已停止轮询") from None
        last = value if isinstance(value, dict) else {}
        for index in range(max(1, int(attempts or 1))):
            observed = None
            try:
                # Status polling is a browser-style read-only GET.  Follow a
                # bounded same-origin redirect chain (including the explicit
                # default-port HTTP→HTTPS upgrade), but never replay the
                # source-image POST or send credentials cross-origin.
                observed = request_with_redirects(
                    self.session, "GET", target, max_redirects=3,
                    headers=poll_headers, timeout=8)
                status = int(getattr(observed, "status_code", 0) or 0)
                if 300 <= status < 400 or not 200 <= status < 300:
                    # A completed 4xx (other than 408) is an explicit
                    # rejection, not an unknown transport result.  Return a
                    # terminal failure envelope so the caller preserves the
                    # rejected outcome and never retries the source POST.
                    if 400 <= status < 500 and status != 408:
                        return {
                            "state": "FAILED",
                            "message": f"远程图片抓取状态接口 HTTP {status}，后台拒绝任务",
                            "_http_status": status,
                        }
                    raise UploadPolicyError(
                        f"远程图片抓取状态接口 HTTP {status}，结果待核对")
                current = observed.json()
            except UploadPolicyError:
                raise
            except (ValueError, TypeError, requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError, NetworkError) as exc:
                debug_log(f"[远程图片抓取状态轮询] 结果未知：{exc}")
                return None
            finally:
                close = getattr(observed, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            if not isinstance(current, dict):
                return None
            last = current
            rows = current.get("list", current.get("data"))
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list) and rows:
                return current
            state = str(current.get("state") or current.get("status") or "").upper()
            if state in {"FAILED", "ERROR", "FAIL", "DENIED"}:
                return current
            if not self._remote_catcher_pending(current):
                return current
            if index < max(1, int(attempts or 1)) - 1:
                time.sleep(max(0.0, min(float(interval), 5.0)))
        return last if isinstance(last, dict) else None

    def catch_remote_images(self, urls, *, upload_surface='editor', upload_target='content',
                            base_url=''):
        """Use the discovered UEditor remote-image catcher for explicit URLs.

        This mirrors the browser's ``catchRemoteImageEnable`` path.  It is
        intentionally opt-in: when the current editor does not advertise a
        catcher, callers receive a clear ``UploadPolicyError`` rather than
        downloading or rewriting external resources behind the user's back.
        The CMS response is validated item-by-item; a URL-only or partial
        response is unknown and must not be followed by an article POST.
        """
        policy = getattr(self, '_upload_policies', {}).get(
            (str(upload_surface or 'editor').strip().lower(), upload_target or 'content'))
        if policy is None:
            raise UploadPolicyError('尚未读取当前编辑器的远程图片上传配置')
        catcher = (policy.metadata or {}).get('remote_catcher') or {}
        if not catcher.get('enabled'):
            raise UploadPolicyError('当前网页编辑器未启用远程图片抓取')
        reference = base_url or getattr(self, 'base_url', '') or getattr(self, 'admin_url', '')
        normalized, original_by_normalized = [], {}
        for raw in urls or []:
            text = str(raw or '').strip()
            if not text:
                continue
            resolved = urljoin(str(reference).rstrip('/') + '/', text)
            parsed = urlparse(resolved)
            if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
                raise UploadPolicyError('远程图片地址必须是 HTTP(S) URL')
            if parsed.username or parsed.password or any(ord(ch) < 32 for ch in resolved):
                raise UploadPolicyError('远程图片地址含不安全凭据或控制字符')
            if resolved not in original_by_normalized:
                normalized.append(resolved)
                original_by_normalized[resolved] = text
        if not normalized:
            return {}
        endpoint = urlparse(policy.endpoint)
        from urllib.parse import parse_qsl, urlencode, urlunparse
        query = [(key, value) for key, value in parse_qsl(endpoint.query, keep_blank_values=True)
                 if key != 'action']
        endpoint_url = urlunparse(endpoint._replace(
            query=urlencode(query + [('action', catcher['action'])], doseq=True), fragment=''))
        pairs = flatten_upload_data(policy.data)
        pairs.extend((catcher['field'], item) for item in normalized)
        page = urlparse(policy.page_url)
        headers = _browser_upload_headers(
            policy.page_url, mode="xhr", policy_headers=policy.headers or {})
        self.last_upload_result = {'outcome': 'not_sent', 'path': '', 'message': '', 'metadata': {}}
        try:
            response = self._post_upload_once(
                endpoint_url, data=pairs, headers=headers, timeout=TIMEOUT_UPLOAD)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, NetworkError) as exc:
            self.last_upload_result = {
                'outcome': 'unknown', 'path': '',
                'message': '远程图片抓取结果未知，未自动重试，请先核对后台', 'metadata': {}}
            debug_log(f'[远程图片抓取结果未知] {exc}')
            raise NetworkError(self.last_upload_result['message']) from exc
        status = int(getattr(response, 'status_code', 0) or 0)
        response_headers = dict(getattr(response, "headers", {}) or {})
        if not 200 <= status < 300:
            outcome = 'rejected' if 400 <= status < 500 and status != 408 else 'unknown'
            message = f'远程图片抓取接口 HTTP {status}，未确认结果'
            self.last_upload_result = {'outcome': outcome, 'path': '', 'message': message,
                                       'metadata': {}}
            raise UploadPolicyError(message)
        try:
            try:
                value = response.json()
            except (ValueError, TypeError) as exc:
                if status == 202:
                    value = {}
                else:
                    message = '远程图片抓取响应不是有效JSON，无法确认结果'
                    self.last_upload_result = {'outcome': 'unknown', 'path': '', 'message': message,
                                               'metadata': {}}
                    raise UploadPolicyError(message) from exc
        finally:
            _close_response(response)
        if not isinstance(value, dict):
            raise UploadPolicyError('远程图片抓取响应结构无效')
        # A catcher may acknowledge the source list before object storage
        # finishes.  If (and only if) it advertises a status URL or an
        # explicit pending state, observe that URL with bounded GET requests;
        # never resend the original remote-image POST.
        if status == 202 or self._remote_catcher_pending(value):
            polled = self._poll_remote_catcher(response_headers, value, policy.page_url)
            if polled is None:
                message = '远程图片抓取已接受但结果未知，请先核对后台'
                self.last_upload_result = {'outcome': 'unknown', 'path': '',
                                           'message': message, 'metadata': value}
                raise UploadPolicyError(message)
            value = polled
        state = str(value.get('state', '') or '').upper()
        if state and state not in ('SUCCESS', 'OK'):
            message = str(value.get('message') or value.get('state') or '远程图片抓取失败')
            self.last_upload_result = {'outcome': 'rejected', 'path': '', 'message': message,
                                       'metadata': value}
            raise UploadPolicyError(message)
        rows = value.get('list')
        if rows is None:
            rows = value.get('data')
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            message = '远程图片抓取响应缺少逐项结果，无法确认保存地址'
            self.last_upload_result = {'outcome': 'unknown', 'path': '', 'message': message,
                                       'metadata': value}
            raise UploadPolicyError(message)
        mapping = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_state = str(row.get('state', '') or '').upper()
            source = str(row.get('source') or row.get('original') or '').strip()
            target = str(row.get('url') or row.get('path') or '').strip()
            if row_state and row_state not in ('SUCCESS', 'OK'):
                raise UploadPolicyError(str(row.get('message') or '远程图片抓取失败'))
            if not source or not target:
                continue
            prefix_base = urljoin(policy.page_url, policy.url_prefix or '')
            target_url = urljoin(prefix_base, target)
            parsed_target = urlparse(target_url)
            if parsed_target.scheme.lower() not in ('http', 'https') or not parsed_target.netloc:
                raise UploadPolicyError('远程图片抓取返回了无效地址')
            if not _same_origin_or_http_upgrade(target_url, policy.page_url):
                raise UploadPolicyError('远程图片抓取返回了跨站地址，已停止改写正文')
            original = original_by_normalized.get(source, source)
            mapping[original] = target_url
            mapping[source] = target_url
        missing = [item for item in normalized
                   if not (item in mapping or original_by_normalized[item] in mapping)]
        if missing:
            message = '远程图片抓取只返回了部分结果，请先到后台核对'
            self.last_upload_result = {'outcome': 'unknown', 'path': '', 'message': message,
                                       'metadata': value}
            raise UploadPolicyError(message)
        self.last_upload_result = {'outcome': 'confirmed', 'path': '', 'message': '',
                                   'metadata': value, 'policy': dict(policy.metadata)}
        result = {}
        for item in normalized:
            target = mapping.get(item) or mapping[original_by_normalized[item]]
            result[item] = target
            result[original_by_normalized[item]] = target
        return result

    def _post_upload_once(self, endpoint, **kwargs):
        """Send one upload POST and safely observe browser-style redirects.

        The multipart POST itself is never replayed.  Browsers commonly turn
        a same-origin 301/302/303 response into a follow-up GET, so observe
        that read-only leg to match the native uploader.  A 307/308 would
        require resending the body and can duplicate a file that the first
        request already committed; leave that response untouched so the
        normal parser reports an unknown result.  Every redirect remains
        same-origin, with only the default-port HTTP→HTTPS upgrade allowed.
        """
        request_kwargs = dict(kwargs)
        response = self.session.post(endpoint, allow_redirects=False,
                                     **request_kwargs)
        current = str(getattr(response, "url", "") or endpoint)
        # Keep this deliberately short.  Native upload handlers should not
        # create an unbounded redirect chain, and no additional POST is ever
        # allowed here.
        for _ in range(3):
            status = int(getattr(response, "status_code", 0) or 0)
            if status not in (301, 302, 303):
                return response
            location = (getattr(response, "headers", {}) or {}).get("Location", "")
            if not location:
                return response
            target = urljoin(current, str(location))
            reference = self.base_url or self.admin_url
            if not (_same_origin(target, reference) or
                    _same_host_http_upgrade(target, reference)):
                return response
            close = getattr(response, "close", None)
            if callable(close):
                close()
            headers = dict(request_kwargs.get("headers") or {})
            headers = {
                key: value for key, value in headers.items()
                if str(key).lower() not in {
                    "content-type", "content-length", "content-encoding",
                    "content-language", "content-location", "transfer-encoding",
                }
            }
            headers["Referer"] = current
            # A 301/302/303 turns the multipart upload into the browser's
            # follow-up GET.  Hidden-iframe ``simpleupload`` is a navigation
            # request, not an XHR: remove the XHR/Origin markers and restore
            # navigation Fetch Metadata.  XHR redirects retain their
            # same-origin Origin/cors context but never carry the old body.
            fetch_dest = str(headers.get("Sec-Fetch-Dest", "") or "").lower()
            if fetch_dest == "iframe":
                headers = {
                    key: value for key, value in headers.items()
                    if str(key).lower() not in {
                        "origin", "x-requested-with", "x_requested_with",
                    }
                }
                headers.update({
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "iframe",
                })
            else:
                headers.update({
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                })
            response = self.session.get(
                target, allow_redirects=False,
                headers=headers, timeout=request_kwargs.get("timeout"))
            current = str(getattr(response, "url", "") or target)
        return response

    def _poll_pending_upload(self, result, page_url, *, attempts=12,
                             interval=0.5):
        """Resolve an explicitly queued upload through a read-only status URL.

        A few UEditor/Layui-compatible handlers return HTTP 202 or a job id
        without an asset URL.  The browser polls the advertised status
        endpoint; treating that response as an ordinary upload failure would
        force users to re-submit the multipart body and could create a
        duplicate object.  Only a URL explicitly supplied by the server is
        followed, it must remain same-origin, and the bounded loop never
        sends another POST.  The default window is bounded at roughly six
        seconds before any server-supplied Retry-After delay, long enough for
        the short object-store delay commonly hidden by the browser callback
        without turning an uncertain result into an unbounded wait.
        """
        if not isinstance(result, dict):
            return result
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            return result
        poll = str(metadata.get("_poll_url") or "").strip()
        if not poll:
            return result
        try:
            target = urljoin(str(page_url or self.base_url or self.admin_url), poll)
            if not _same_origin_or_http_upgrade(target, self.base_url or self.admin_url):
                result.update(outcome="unknown", message="上传状态地址跨站，已停止轮询")
                return result
            parsed = urlparse(target)
            headers = {"Referer": str(page_url or "")}
            if parsed.scheme and parsed.netloc:
                headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        except (TypeError, ValueError):
            result.update(outcome="unknown", message="上传状态地址无效，已停止轮询")
            return result
        last = result
        poll_headers = {
            "Referer": str(page_url or ""),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept": "application/json, */*",
        }
        if parsed.scheme and parsed.netloc:
            poll_headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        for index in range(max(1, int(attempts or 1))):
            try:
                # A queued upload's status URL may be canonicalized by the
                # server.  Observe only safe same-origin redirects; the
                # original multipart request is never resent.
                response = request_with_redirects(
                    self.session, "GET", target, max_redirects=3,
                    headers={**headers, **poll_headers}, timeout=8)
                try:
                    observed = parse_upload_result(response)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    NetworkError) as exc:
                debug_log(f"[上传状态轮询] 结果未知：{exc}")
                break
            if observed.get("outcome") == "rejected":
                # A status endpoint can explicitly refuse the queued job
                # (for example HTTP 403/413).  Preserve that terminal result
                # instead of converting it to an eventual timeout/unknown.
                combined = dict(last)
                combined.update(observed)
                merged = dict(metadata)
                if isinstance(observed.get("metadata"), dict):
                    merged.update(observed["metadata"])
                combined["metadata"] = merged
                combined["path"] = ""
                return combined
            if observed.get("path"):
                # Keep the original acknowledgement metadata (job id/notice)
                # while adopting the status response's final path and fields.
                combined = dict(last)
                combined.update(observed)
                merged = dict(metadata)
                if isinstance(observed.get("metadata"), dict):
                    merged.update(observed["metadata"])
                combined["metadata"] = merged
                if not self._upload_result_pending(observed):
                    return combined
                last = combined
            elif observed.get("metadata"):
                merged = dict(metadata)
                merged.update(observed.get("metadata") or {})
                last = dict(last)
                last["metadata"] = merged
                if observed.get("message"):
                    last["message"] = observed["message"]
            if index < max(1, int(attempts or 1)) - 1:
                retry_after = ""
                try:
                    retry_after = str((getattr(response, "headers", {}) or {}).get(
                        "Retry-After", "") or "").strip()
                except Exception:
                    retry_after = ""
                delay = None
                if retry_after:
                    try:
                        delay = max(0.0, min(float(retry_after), 5.0))
                    except (TypeError, ValueError):
                        # Do not turn an HTTP-date or malformed value into an
                        # unbounded sleep; use the bounded client interval.
                        delay = None
                time.sleep(max(0.0, min(
                    float(interval) if delay is None else delay, 5.0)))
        last = dict(last)
        last["outcome"] = "unknown"
        last["path"] = ""
        last["message"] = (last.get("message") or
                            "后台已接受上传任务但未返回最终文件地址，请先核对后台")
        return last

    def _probe_uploaded_asset(self, asset_url, *, max_bytes=64 * 1024 * 1024):
        """Read back any same-origin uploaded asset without mutating it.

        Native browser uploads expose the selected ``File`` before the POST,
        while the CMS may rename, transcode, compress, watermark or otherwise
        replace the stored object.  The old read-back path only inspected
        image-looking URLs, which left PDF/video/audio/attachment fields with
        no comparable server evidence at all.  Use the same bounded HEAD/GET
        and no-redirect rules for every media type.  This is observation only:
        an unavailable probe never changes a confirmed upload into failure.
        """
        candidate = str(asset_url or "").strip()
        if not candidate:
            return {}
        try:
            resolved = urljoin((self.base_url or self.admin_url).rstrip("/") + "/", candidate)
            if not _same_origin_or_http_upgrade(resolved, self.base_url or self.admin_url):
                return {"server_inspection": "skipped_cross_origin"}
            probe_headers = {
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Accept": "*/*",
                "Referer": str(self.admin_url or self.base_url or ""),
            }
            # The production requests session exposes HEAD, but a few
            # browser/adapter shims intentionally implement only GET.  A
            # missing HEAD method is not evidence that the object is absent;
            # continue with the same bounded browser-style GET instead of
            # silently losing the server-side evidence.
            head_method = getattr(self.session, "head", None)
            if callable(head_method):
                head = head_method(resolved, allow_redirects=False,
                                   headers=probe_headers, timeout=5)
                try:
                    status = int(getattr(head, "status_code", 0) or 0)
                    if 300 <= status < 400:
                        location = str((getattr(head, "headers", {}) or {}).get(
                            "Location", "") or "")
                        try:
                            redirect_target = urljoin(resolved, location)
                            if (not location or
                                    not permitted_transition(resolved, redirect_target)):
                                return {"server_inspection": "skipped_redirect"}
                        except Exception:
                            return {"server_inspection": "skipped_redirect"}
                    # Some object stores/CDNs deliberately disable HEAD while
                    # still serving the resource to a browser GET.  Treat only
                    # the explicit method-not-allowed responses as a signal to
                    # fall back to the bounded GET below; auth/errors remain
                    # visible and are not guessed away.
                    if status not in (405, 501) and not 200 <= status < 300:
                        return {"server_inspection": f"head_http_{status}"}
                    if 200 <= status < 300:
                        content_length = str((getattr(head, "headers", {}) or {}).get(
                            "Content-Length", "") or "").strip()
                        try:
                            if content_length and int(content_length) > max_bytes:
                                return {"server_inspection": "skipped_too_large"}
                        except (TypeError, ValueError):
                            pass
                finally:
                    close = getattr(head, "close", None)
                    if callable(close):
                        close()
            response = request_with_redirects(
                self.session, "GET", resolved, stream=True,
                allow_redirects=True, headers=probe_headers, timeout=8)
            try:
                status = int(getattr(response, "status_code", 0) or 0)
                if 300 <= status < 400 or not 200 <= status < 300:
                    return {"server_inspection": f"get_http_{status}"}
                chunks, total = [], 0
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return {"server_inspection": "skipped_too_large"}
                    chunks.append(chunk)
                data = b"".join(chunks)
                response_headers = getattr(response, "headers", {}) or {}
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if not data:
                return {"server_inspection": "empty"}
            response_content_type = str(
                response_headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
            # A session expiry often returns a 200 login page for an asset
            # URL.  Hashing that HTML as the uploaded object would falsely
            # report a server transform and could feed a non-image URL into
            # the thumbnail preview.  Keep the upload outcome untouched, but
            # mark the observation unavailable so the UI asks for session
            # re-authentication instead of presenting bogus dimensions/hash.
            if response_content_type in {"text/html", "application/xhtml+xml"}:
                try:
                    login_text = data[:256 * 1024].decode("utf-8", "ignore")
                except Exception:
                    login_text = ""
                if _is_login_page(login_text):
                    return {"server_inspection": "login_page"}
            result = {
                "server_bytes": len(data),
                "server_sha256": hashlib.sha256(data).hexdigest(),
                # A number of object stores omit Content-Type.  The browser
                # still knows the returned File/response bytes, so use a
                # bounded signature fallback while retaining an explicit
                # header whenever the server supplies one.
                "server_mime": (
                    str(response_headers.get("Content-Type", "") or "")
                    .split(";", 1)[0].strip() or sniff_mime(data, resolved)),
                "server_inspection": "verified",
            }
            result.update(_server_response_metadata(
                response_headers, getattr(response, "url", "") or resolved))
            return result
        except (OSError, IOError, requests.RequestException, ValueError,
                AttributeError, TypeError, AssertionError) as exc:
            debug_log(f"[上传回读] 忽略只读媒体探测失败：{exc}")
            return {"server_inspection": "unavailable"}

    @staticmethod
    def _upload_result_pending(result):
        """Whether the upload response explicitly says processing is async.

        A successful URL is normally final, so polling every upload would add
        requests and still guess at server behavior.  Only an explicit
        ``processing/pending/async`` marker (including its common nested
        ``data`` form) enables the bounded observation loop below.
        """
        if not isinstance(result, dict):
            return False
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            return False
        scopes = [metadata]
        if isinstance(metadata.get("data"), dict):
            scopes.append(metadata["data"])
        # Besides booleans, real upload handlers commonly expose a job id or
        # a polling URL without an explicit ``processing: true`` flag.  Those
        # fields are still an explicit server contract: the browser must wait
        # for the representation to settle instead of treating the first
        # object-store response as final.  Do not infer this from an arbitrary
        # filename or from a generic success message.
        job_keys = {"job_id", "task_id", "process_id", "queue_id",
                    "status_url", "poll_url", "process_url", "job_url",
                    "processing_url"}
        pending_words = re.compile(
            r"处理中|排队|异步|processing|pending|queued|queue|async|running",
            re.I)
        for item in scopes:
            for key in ("processing", "pending", "async", "asynchronous"):
                value = item.get(key)
                if value is True or str(value).strip().lower() in {
                        "1", "true", "yes", "pending", "processing", "queued"}:
                    return True
            status = str(item.get("upload_status") or item.get("process_status") or
                         item.get("job_status") or "").strip().lower()
            if status in {"pending", "processing", "queued", "running", "async"}:
                return True
            if any(str(item.get(key, "") or "").strip() for key in job_keys):
                return True
            for key in ("message", "msg", "notice", "warning", "status"):
                value = item.get(key)
                if isinstance(value, str) and pending_words.search(value):
                    return True
        return False

    def _probe_uploaded_until_stable(self, asset_url, *, image=False,
                                     max_bytes=None, attempts=3,
                                     interval=0.25):
        """Observe an explicitly asynchronous asset without replaying upload.

        The loop is intentionally short and only activated by a server
        processing marker.  Returning the last observation preserves the old
        best-effort semantics when a CDN/object store is eventually
        unavailable; it never changes a confirmed upload into a failure.
        """
        probe = self._probe_uploaded_image if image else self._probe_uploaded_asset
        kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
        count = max(1, int(attempts or 1))
        last = {}
        previous_fingerprint = None
        for index in range(count):
            last = probe(asset_url, **kwargs) or {}
            if last.get("server_inspection") == "verified":
                fingerprint = (last.get("server_sha256"), last.get("server_bytes"),
                               last.get("server_mime"))
                if previous_fingerprint == fingerprint or index == count - 1:
                    return last
                previous_fingerprint = fingerprint
            if index < count - 1:
                time.sleep(max(0.0, min(float(interval), 2.0)))
        return last

    def _annotate_uploaded_asset(self, result, path, *, wait_for_processing=False):
        """Attach generic server metadata and compare it with client bytes."""
        if not isinstance(result, dict) or not result.get("path"):
            return result
        metadata = result.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            return result
        if metadata.get("server_bytes") in (None, ""):
            observed = (self._probe_uploaded_until_stable(path)
                        if wait_for_processing else self._probe_uploaded_asset(path))
            if observed:
                metadata.update(observed)
        changes = []
        for label, client_key, server_key in (
                ("bytes", "client_bytes", "server_bytes"),
                ("sha256", "client_sha256", "server_sha256"),
                ("mime", "client_mime", "server_mime")):
            if metadata.get(client_key) in (None, "") or metadata.get(server_key) in (None, ""):
                continue
            if label == "mime":
                equal = (_normalise_asset_mime(metadata[client_key]) ==
                         _normalise_asset_mime(metadata[server_key]))
            else:
                equal = str(metadata[client_key]) == str(metadata[server_key])
            if not equal:
                changes.append(label)
        if (metadata.get("client_filename") and metadata.get("server_filename") and
                os.path.basename(str(metadata["client_filename"])).lower() !=
                os.path.basename(str(metadata["server_filename"])).lower()):
            changes.append("filename")
        if changes:
            metadata["server_changed"] = True
            existing = list(metadata.get("server_change_fields") or [])
            metadata["server_change_fields"] = list(dict.fromkeys(existing + changes))
        elif all(metadata.get(key) not in (None, "")
                 for key in ("client_bytes", "server_bytes")):
            metadata.setdefault("server_changed", False)
        return result

    def _probe_uploaded_image(self, asset_url, *, max_bytes=16 * 1024 * 1024):
        """Read back a same-origin uploaded image without mutating the site.

        Pboot upload responses are not consistent: some return a URL only,
        while the browser can immediately observe the processed resource and
        its rendered dimensions.  A bounded HEAD/GET probe fills that gap for
        ordinary raster assets.  Redirects, cross-origin URLs, oversized
        resources and non-images are deliberately reported as unavailable;
        callers must not turn a failed probe into a failed upload.
        """
        candidate = str(asset_url or "").strip()
        if not candidate:
            return {}
        try:
            resolved = urljoin((self.base_url or self.admin_url).rstrip("/") + "/", candidate)
            if not _same_origin_or_http_upgrade(resolved, self.base_url or self.admin_url):
                return {"server_inspection": "skipped_cross_origin"}
            probe_headers = {
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Accept": "image/*,*/*;q=0.8",
                "Referer": str(self.admin_url or self.base_url or ""),
            }
            head_method = getattr(self.session, "head", None)
            if callable(head_method):
                head = head_method(resolved, allow_redirects=False,
                                   headers=probe_headers, timeout=5)
                try:
                    head_status = int(getattr(head, "status_code", 0) or 0)
                    if 300 <= head_status < 400:
                        location = str((getattr(head, "headers", {}) or {}).get(
                            "Location", "") or "")
                        try:
                            redirect_target = urljoin(resolved, location)
                            if (not location or
                                    not permitted_transition(resolved, redirect_target)):
                                return {"server_inspection": "skipped_redirect"}
                        except Exception:
                            return {"server_inspection": "skipped_redirect"}
                    if head_status not in (405, 501) and not (200 <= head_status < 300):
                        return {"server_inspection": f"head_http_{getattr(head, 'status_code', 0)}"}
                    if 200 <= head_status < 300:
                        content_length = str((getattr(head, "headers", {}) or {}).get(
                            "Content-Length", "") or "").strip()
                        try:
                            if content_length and int(content_length) > max_bytes:
                                return {"server_inspection": "skipped_too_large"}
                        except (TypeError, ValueError):
                            pass
                finally:
                    close = getattr(head, "close", None)
                    if callable(close):
                        close()
            response = request_with_redirects(
                self.session, "GET", resolved, stream=True,
                allow_redirects=True, headers=probe_headers, timeout=8)
            try:
                status = int(getattr(response, "status_code", 0) or 0)
                if 300 <= status < 400 or not 200 <= status < 300:
                    return {"server_inspection": f"get_http_{status}"}
                chunks, total = [], 0
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return {"server_inspection": "skipped_too_large"}
                    chunks.append(chunk)
                data = b"".join(chunks)
                response_headers = getattr(response, "headers", {}) or {}
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if not data:
                return {"server_inspection": "empty"}
            response_content_type = str(
                response_headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
            if response_content_type in {"text/html", "application/xhtml+xml"}:
                try:
                    login_text = data[:256 * 1024].decode("utf-8", "ignore")
                except Exception:
                    login_text = ""
                if _is_login_page(login_text):
                    return {"server_inspection": "login_page"}
            result = {
                "server_bytes": len(data),
                "server_sha256": hashlib.sha256(data).hexdigest(),
                "server_mime": (
                    str(response_headers.get("Content-Type", "") or "")
                    .split(";", 1)[0].strip() or sniff_mime(data, resolved)),
                "server_inspection": "verified",
            }
            result.update(_server_response_metadata(
                response_headers, getattr(response, "url", "") or resolved))
            image_meta = _image_asset_metadata(data, "server")
            if image_meta:
                result.update(image_meta)
            elif Image is not None:
                result["server_inspection"] = "bytes_only"
            return result
        except (OSError, IOError, requests.RequestException, ValueError,
                AttributeError, TypeError, AssertionError) as exc:
            debug_log(f"[上传回读] 忽略只读图片探测失败：{exc}")
            return {"server_inspection": "unavailable"}

    @staticmethod
    def _upload_has_dimensions(result):
        metadata = result.get("metadata") if isinstance(result, dict) else {}
        if not isinstance(metadata, dict):
            return False
        sources = [metadata]
        if isinstance(metadata.get("data"), dict):
            sources.append(metadata["data"])
        return any(source.get("width") not in (None, "") and
                   source.get("height") not in (None, "") for source in sources)

    @staticmethod
    def _client_asset_metadata(file_data, filename, *, image=False,
                               declared_mime_value=""):
        """Record the exact bytes sent by the desktop upload control.

        The browser can expose the selected File's size/type before the
        server responds.  Keeping this separate from server_* metadata lets
        the completion UI show whether the backend changed dimensions,
        format, bytes or hash, including when the request result is unknown.
        It never changes the upload decision and is bounded to the already
        loaded request bytes.
        """
        data = bytes(file_data or b"")
        declared = str(declared_mime_value or "").split(";", 1)[0].strip().lower()
        guessed = (mimetypes.guess_type(str(filename or ""))[0] or
                   sniff_mime(file_data, filename))
        if not Path(str(filename or "")).suffix and declared:
            guessed = declared
        result = {
            "client_bytes": len(data),
            "client_sha256": hashlib.sha256(data).hexdigest(),
            "client_mime": guessed or "application/octet-stream",
            "client_filename": os.path.basename(str(filename or "")),
            "client_extension": Path(str(filename or "")).suffix.lower(),
        }
        if image:
            result.update(_image_asset_metadata(data, "client"))
        return result

    @staticmethod
    def _merge_client_metadata(result, metadata):
        if not isinstance(result, dict) or not isinstance(metadata, dict):
            return result
        target = result.setdefault("metadata", {})
        if isinstance(target, dict):
            for key, value in metadata.items():
                target.setdefault(key, value)
        return result

    def _annotate_uploaded_image(self, result, path, *, wait_for_processing=False):
        """Best-effort final-resource metadata; never changes upload outcome."""
        if not isinstance(result, dict) or not result.get("path"):
            observed = {}
        else:
            # Upload handlers sometimes return width/height while the actual
            # object is still being resized, watermarked, renamed or replaced
            # by storage.  Those response fields are useful evidence, but do
            # not substitute for reading the returned URL.  Probe whenever a
            # complete server byte/hash/MIME observation is not already
            # present so a response with dimensions cannot hide processing.
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            has_observation = all(metadata.get(key) not in (None, "")
                                  for key in ("server_bytes", "server_sha256",
                                              "server_mime", "server_inspection"))
            observed = {} if has_observation else (
                self._probe_uploaded_until_stable(
                    result.get("path"), image=True)
                if wait_for_processing else
                self._probe_uploaded_image(result.get("path")))
        if observed:
            result.setdefault("metadata", {}).update(observed)
        metadata = result.get("metadata") if isinstance(result, dict) else {}
        if not isinstance(metadata, dict):
            return result
        changes = []
        comparisons = (
            ("bytes", "client_bytes", "server_bytes"),
            ("sha256", "client_sha256", "server_sha256"),
            ("width", "client_width", "server_width"),
            ("height", "client_height", "server_height"),
            ("format", "client_format", "server_format"),
            ("alpha", "client_alpha", "server_alpha"),
            ("animated", "client_animated", "server_animated"),
            ("frames", "client_frames", "server_frames"),
            ("exif_orientation", "client_exif_orientation", "server_exif_orientation"),
            ("icc", "client_icc_sha256", "server_icc_sha256"),
            ("mime", "client_mime", "server_mime"),
        )
        for label, client_key, server_key in comparisons:
            if metadata.get(client_key) not in (None, "") and metadata.get(server_key) not in (None, ""):
                left, right = metadata[client_key], metadata[server_key]
                if label == "mime":
                    equal = (_normalise_asset_mime(left) ==
                             _normalise_asset_mime(right))
                elif label in ("alpha", "animated"):
                    equal = bool(left) == bool(right)
                else:
                    equal = str(left) == str(right)
                if not equal:
                    changes.append(label)
        if (metadata.get("client_filename") and metadata.get("server_filename") and
                os.path.basename(str(metadata["client_filename"])).lower() !=
                os.path.basename(str(metadata["server_filename"])).lower()):
            changes.append("filename")
        if changes:
            metadata["server_changed"] = True
            # Preserve differences discovered by a generic byte/MIME probe
            # when an image-specific follow-up adds dimensions/format.  The
            # browser sees one final resource; the desktop result should not
            # lose an earlier filename/hash change merely because the richer
            # decoder ran afterwards.
            existing = list(metadata.get("server_change_fields") or [])
            metadata["server_change_fields"] = list(dict.fromkeys(
                existing + changes))
        elif all(metadata.get(key) not in (None, "") for key in ("client_bytes", "server_bytes")):
            metadata.setdefault("server_changed", False)
        return result

    def upload_image(self, filepath, formcheck="", render_size=None,
                     upload_surface="editor", upload_target=None,
                     ueditor_upload_mode=None):
        """按 PbootCMS 后台实际控件上传图片。

        必须先prepare_uploads读取当前表单。目标字段决定实际上传入口、
        文件字段、水印和限制，不能退回猜测的站点根路径。
        默认保留原文件；桌面端不再提供任何额外的本地裁切/转码。
        ``render_size`` 仅为旧调用保留的兼容参数。若旧草稿或外部调用
        仍传入该参数，必须显式失败，不能悄悄改变 multipart 字节。

        ``ueditor_upload_mode='autoupload'`` mirrors UEditor's
        ``sendAndInsertFile`` XHR path, which appends a multipart ``type=ajax``
        field and the XHR marker.  ``ueditor_upload_mode='dialog'`` mirrors
        the image dialog's WebUploader request: it appends ``encode=utf-8``
        to the action URL, uses the historical ``X_Requested_With`` header,
        and does not add the XHR-only multipart marker.  The dialog's image
        canvas uses quality 90.  ``ueditor_upload_mode='simpleupload'``
        mirrors the toolbar's hidden-iframe form path: it omits both the
        multipart marker and the XHR-only header.  Callers that need the
        iframe's visual/editor event lifecycle still use the authenticated
        native webpage; this mode only makes the request protocol explicit.

        返回 ``(服务器路径, 错误信息)``。
        """
        filename = os.path.basename(filepath)
        declared = declared_mime(filepath)
        self.last_upload_result = {'outcome':'not_sent', 'path':'', 'message':'', 'metadata':{}}
        try:
            if render_size:
                raise UploadPolicyError(
                    '为保持与后台直接上传一致，软件不再在客户端裁切或转码；'
                    '请使用原文件上传或打开原生网页设置尺寸')
            surface = str(upload_surface or 'editor').strip().lower()
            target = upload_target or ('content' if surface == 'editor' else 'ico')
            policy = getattr(self, '_upload_policies', {}).get((surface, target))
            if policy is None:
                raise UploadPolicyError(f'尚未读取{target}的真实上传配置，请重新载入表单')
            safe_url(policy.page_url, self.admin_url)
            upload_mode = str(ueditor_upload_mode or '').strip().lower()
            if upload_mode not in ('', 'autoupload', 'dialog', 'simpleupload'):
                raise UploadPolicyError('UEditor上传入口未适配')
            # UEditor's native image uploader checks the selected File's
            # original size before entering the optional canvas-compression
            # branch. Checking only transformed bytes would let an oversized
            # source slip through after a local resize.
            if policy.max_bytes and upload_mode != 'simpleupload':
                try:
                    original_size = os.path.getsize(filepath)
                except (OSError, TypeError, ValueError):
                    raise UploadPolicyError('文件不存在或无法读取') from None
                if original_size > policy.max_bytes:
                    raise UploadPolicyError(
                        f'文件超过当前网页上传上限（{policy.max_bytes}字节）')
            # The simpleupload toolbar submits the selected File directly in
            # its hidden form; unlike autoupload it never runs the canvas
            # compression hook before submission.
            policy_compress = ((policy.metadata or {}).get('client_compress') or {}
                               if upload_mode != 'simpleupload' else {})
            compression_suffixes = ('.jpg', '.jpeg') if upload_mode == 'dialog' \
                else ('.jpg', '.jpeg', '.png', '.gif')
            browser_would_transform = (
                upload_mode in ('dialog', 'autoupload') and
                policy_compress.get('enabled') and
                Path(filename).suffix.lower() in compression_suffixes)
            if (getattr(self, '_strict_browser_upload_parity', False) and
                    browser_would_transform):
                raise UploadPolicyError(
                    '当前网页启用了浏览器 Canvas 压缩；为保证上传字节与后台网页完全一致，'
                    '请使用认证原生网页完成正文图片上传')
            client_transform = None
            if (policy_compress.get('enabled') and
                    Path(filename).suffix.lower() in compression_suffixes):
                # UEditor's automatic upload path uses a canvas for the four
                # stock raster suffixes and a fixed quality of 0.8.  The
                # WebUploader image dialog is different: its beforeSendFile
                # hook compresses JPEG only (PNG/GIF are sent byte-for-byte)
                # and hard-codes quality=90.
                browser_quality = 90 if upload_mode == 'dialog' else 80
                file_data, upload_name, transformed = self._compress_image_for_browser(
                    filepath, int(policy_compress.get('border', 1600)),
                    browser_quality)
                if transformed:
                    client_transform = {
                        'kind': 'ueditor-image-compress',
                        'border': int(policy_compress.get('border', 1600)),
                        'quality': browser_quality,
                        'preserve_headers': bool(upload_mode == 'dialog'),
                        'filename': upload_name,
                    }
            else:
                # 后台直接上传不会先在客户端把大图缩到 1200px、
                # 转成 JPEG 或重命名。保留原文件才能让后台配置成为唯一规则。
                with open(filepath, "rb") as handle:
                    file_data = handle.read()
                upload_name = filename
            _ext, mime = self._detect_image_type(file_data, upload_name)
            if not Path(str(upload_name or "")).suffix and declared.startswith("image/"):
                mime = declared
            if upload_mode == 'simpleupload':
                # The hidden form path performs only the browser file-input
                # extension check; it does not run UEditor's XHR max-size
                # guard.  Keep the server's own limit authoritative.
                suffix = Path(upload_name).suffix.lower()
                if not suffix or (policy.extensions and suffix not in policy.extensions):
                    raise UploadPolicyError(
                        '文件格式不符合当前网页上传配置：' + ', '.join(policy.extensions))
            policy.validate(upload_name, file_data,
                            check_size=upload_mode != 'simpleupload',
                            declared_mime_value=declared)
            files = {policy.file_field: (upload_name, file_data, mime)}
            page = urlparse(policy.page_url)
            headers = _browser_upload_headers(
                policy.page_url,
                mode=(upload_mode or "xhr"),
                policy_headers=policy.headers)
            # Both stock upload paths use an XMLHttpRequest at the point where
            # the desktop workflow sends a selected asset: Layui's jQuery
            # adapter adds this header automatically, and UEditor's
            # sendAndInsertFile / canvas-compression paths set it explicitly.
            # Keep a site-declared value authoritative, but supply the browser
            # default when the page did not override it.
            if upload_mode == 'dialog':
                # The stock WebUploader dialog uses this legacy spelling.
                # It is deliberately not interchangeable with jQuery's
                # X-Requested-With marker used by sendAndInsertFile.
                headers.pop('X-Requested-With', None)
                headers.setdefault('X_Requested_With', 'XMLHttpRequest')
            elif upload_mode != 'simpleupload':
                headers.setdefault('X-Requested-With', 'XMLHttpRequest')
            debug_log(
                f"[上传准备] {filename}: 后台控件={surface}, "
                f"大小={len(file_data)/1024:.1f}KB")

            # A timed-out POST may already have saved a file. Send once and
            # preserve an unknown outcome rather than creating duplicate files.
            upload_data = encoded_upload_data(policy.data)
            is_ueditor_autoupload = upload_mode == 'autoupload'
            upload_endpoint = (policy.endpoint if upload_mode != 'dialog'
                               else _ueditor_dialog_endpoint(policy.endpoint))
            if is_ueditor_autoupload:
                if isinstance(upload_data, dict) and 'type' not in upload_data:
                    upload_data = dict(upload_data)
                    upload_data['type'] = 'ajax'
                else:
                    upload_data = list(upload_data.items()) if isinstance(upload_data, dict) else list(upload_data or [])
                    upload_data.append(('type', 'ajax'))
            try:
                resp = self._post_upload_once(
                    upload_endpoint, files=files,
                    data=upload_data,
                    headers=headers, timeout=TIMEOUT_UPLOAD)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, NetworkError) as exc:
                self.last_upload_result = dict(outcome="unknown", path="",
                    message="上传结果未知：连接中断，文件可能已保存；未自动重试，请先到后台核对",
                    metadata=self._client_asset_metadata(
                        file_data, upload_name, image=True,
                        declared_mime_value=declared),
                    ueditor_upload_mode=upload_mode)
                debug_log(f"[上传结果未知] {filename}: {exc}")
                return None, self.last_upload_result["message"]
            try:
                self.last_upload_result = parse_upload_result(resp)
            finally:
                _close_response(resp)
            if upload_mode:
                self.last_upload_result['ueditor_upload_mode'] = upload_mode
            if (self.last_upload_result.get("outcome") == "pending" or
                    (self._upload_result_pending(self.last_upload_result) and
                     isinstance(self.last_upload_result.get("metadata"), dict) and
                     self.last_upload_result["metadata"].get("_poll_url"))):
                self.last_upload_result = self._poll_pending_upload(
                    self.last_upload_result, policy.page_url)
            self._merge_client_metadata(
                self.last_upload_result,
                self._client_asset_metadata(
                    file_data, upload_name, image=True,
                    declared_mime_value=declared))
            if client_transform:
                self.last_upload_result['client_transform'] = client_transform
            if self.last_upload_result["path"]:
                self.last_upload_result['path'] = _apply_upload_url_prefix(
                    self.last_upload_result['path'], policy.url_prefix)
                self.last_upload_result['policy'] = dict(policy.metadata)
                self._annotate_uploaded_image(
                    self.last_upload_result, self.last_upload_result['path'],
                    wait_for_processing=self._upload_result_pending(
                        self.last_upload_result))
                return self.last_upload_result["path"], ""
            return None, self.last_upload_result["message"]
        except UploadPolicyError as e:
            strict_fallback = bool(
                getattr(self, '_strict_browser_upload_parity', False) and
                'Canvas 压缩' in str(e))
            self.last_upload_result.update(
                outcome='native_only' if strict_fallback else 'not_sent',
                native_only=strict_fallback,
                native_url=(str(getattr(policy, 'page_url', '') or '')
                            if strict_fallback else ''),
                native_reason=str(e) if strict_fallback else '',
                message=str(e))
            return None, str(e)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                NetworkError) as e:
            return None, f"上传网络失败: {e}"
        except (OSError, IOError) as e:
            return None, f"读取图片文件失败: {e}"
        except Exception as e:
            return None, f"图片上传处理失败: {e}"

    def upload_file(self, filepath, formcheck="", upload_surface="editor",
                    upload_target=None, media_kind="file",
                    ueditor_upload_mode=None):
        """Upload a non-image media/attachment through the discovered policy.

        The endpoint and field are still taken from the active webpage.  This
        is intentionally separate from :meth:`upload_image`: image-specific
        magic-byte detection and local rendering must never reject a PDF,
        video or audio file which the editor's native policy accepts.
        """
        filename = os.path.basename(str(filepath or ""))
        declared = declared_mime(filepath)
        self.last_upload_result = {'outcome': 'not_sent', 'path': '',
                                   'message': '', 'metadata': {}}
        try:
            surface = str(upload_surface or 'editor').strip().lower()
            target = upload_target or ('content' if surface == 'editor' else '')
            kind = str(media_kind or 'file').strip().lower()
            policies = getattr(self, '_upload_policies', {})
            policy = policies.get((surface, target, kind))
            if policy is None:
                policy = policies.get((surface, target))
            if policy is None:
                raise UploadPolicyError(f'尚未读取{target or "媒体"}的真实上传配置，请重新载入表单')
            safe_url(policy.page_url, self.admin_url)
            upload_mode = str(ueditor_upload_mode or '').strip().lower()
            if upload_mode not in ('', 'autoupload', 'dialog'):
                raise UploadPolicyError('非图片UEditor媒体不支持simpleupload入口')
            with open(filepath, 'rb') as handle:
                file_data = handle.read()
            policy.validate(filename, file_data,
                            declared_mime_value=declared)
            mime = ((mimetypes.guess_type(filename)[0] or sniff_mime(file_data, filename))
                    if Path(filename).suffix else (declared or sniff_mime(file_data, filename))) \
                   or 'application/octet-stream'
            files = {policy.file_field: (filename, file_data, mime)}
            page = urlparse(policy.page_url)
            headers = _browser_upload_headers(
                policy.page_url,
                mode=(upload_mode or "xhr"),
                policy_headers=policy.headers)
            if upload_mode == 'dialog':
                # Video and attachment dialogs share WebUploader's
                # uploadBeforeSend hook with the image dialog.
                headers.pop('X-Requested-With', None)
                headers.setdefault('X_Requested_With', 'XMLHttpRequest')
            else:
                # Layui and UEditor's non-image autouploader both submit
                # through XHR, so the browser exposes this marker.
                headers.setdefault('X-Requested-With', 'XMLHttpRequest')
            upload_data = encoded_upload_data(policy.data)
            is_ueditor_autoupload = upload_mode == 'autoupload'
            upload_endpoint = (policy.endpoint if upload_mode != 'dialog'
                               else _ueditor_dialog_endpoint(policy.endpoint))
            if is_ueditor_autoupload:
                if isinstance(upload_data, dict) and 'type' not in upload_data:
                    upload_data = dict(upload_data)
                    upload_data['type'] = 'ajax'
                else:
                    upload_data = list(upload_data.items()) if isinstance(upload_data, dict) else list(upload_data or [])
                    upload_data.append(('type', 'ajax'))
            try:
                resp = self._post_upload_once(upload_endpoint, files=files,
                                              data=upload_data, headers=headers,
                                              timeout=TIMEOUT_UPLOAD)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, NetworkError) as exc:
                self.last_upload_result = dict(outcome='unknown', path='',
                    message='上传结果未知：连接中断，文件可能已保存；未自动重试，请先到后台核对',
                    metadata=self._client_asset_metadata(
                        file_data, filename, declared_mime_value=declared),
                    ueditor_upload_mode=upload_mode)
                debug_log(f'[媒体上传结果未知] {filename}: {exc}')
                return None, self.last_upload_result['message']
            try:
                self.last_upload_result = parse_upload_result(resp)
            finally:
                _close_response(resp)
            if upload_mode:
                self.last_upload_result['ueditor_upload_mode'] = upload_mode
            if (self.last_upload_result.get("outcome") == "pending" or
                    (self._upload_result_pending(self.last_upload_result) and
                     isinstance(self.last_upload_result.get("metadata"), dict) and
                     self.last_upload_result["metadata"].get("_poll_url"))):
                self.last_upload_result = self._poll_pending_upload(
                    self.last_upload_result, policy.page_url)
            self._merge_client_metadata(
                self.last_upload_result,
                self._client_asset_metadata(
                    file_data, filename, declared_mime_value=declared))
            if self.last_upload_result.get('path'):
                self.last_upload_result['path'] = _apply_upload_url_prefix(
                    self.last_upload_result['path'], policy.url_prefix)
                self.last_upload_result['policy'] = dict(policy.metadata)
                # Image-looking assets retain the richer dimension/format
                # probe.  Every other native file now receives the generic
                # bounded byte/hash/MIME read-back as well, so attachments,
                # audio and video are not the only upload surfaces without
                # server-side evidence.
                metadata = self.last_upload_result.get("metadata")
                client_mime = (str(metadata.get("client_mime", "") or "").lower()
                               if isinstance(metadata, dict) else "")
                image_url = re.search(
                    r"\.(?:jpe?g|jpe|png|gif|webp|bmp|avif|tiff?|ico|heic|heif|jxl|jp2|j2k|jpf|jpx|jpm|psd)(?:[?#]|$)",
                    self.last_upload_result['path'], re.I)
                # Object stores may return an extensionless URL while keeping
                # an image MIME.  A browser can still render and size that
                # object, so use the client-selected MIME as a safe hint for
                # the richer image probe.
                if image_url or client_mime.startswith("image/"):
                    self._annotate_uploaded_image(
                        self.last_upload_result, self.last_upload_result['path'],
                        wait_for_processing=self._upload_result_pending(
                            self.last_upload_result))
                else:
                    self._annotate_uploaded_asset(
                        self.last_upload_result, self.last_upload_result['path'],
                        wait_for_processing=self._upload_result_pending(
                            self.last_upload_result))
                return self.last_upload_result['path'], ''
            return None, self.last_upload_result.get('message', '')
        except UploadPolicyError as exc:
            self.last_upload_result.update(outcome='not_sent', message=str(exc))
            return None, str(exc)
        except (OSError, IOError) as exc:
            return None, f'读取媒体文件失败: {exc}'
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, NetworkError) as exc:
            return None, f'媒体上传网络失败: {exc}'
        except Exception as exc:
            return None, f'媒体上传处理失败: {exc}'

    def _render_image(self, filepath, width, height, quality=88):
        """Removed legacy local carousel rendering.

        Keeping a loud compatibility stub is safer than leaving an apparently
        usable helper that can produce bytes different from the browser's
        native multipart upload.  The real upload path never calls this
        method anymore.
        """
        raise ValueError(
            "客户端轮播图裁切/转码已停用；请上传原文件或使用原生网页设置尺寸")

    def _compress_image_for_browser(self, filepath, border=1600, quality=80):
        """Mirror UEditor's optional browser-side image compression.

        When enabled, UEditor's browser path enters this branch for
        ``.jpg``, ``.jpeg``, ``.png`` and ``.gif`` files. Images larger than
        ``imageCompressBorder`` are resized first; smaller images keep their
        dimensions but still pass through ``canvas.toBlob`` and are therefore
        re-encoded. The browser keeps the selected MIME type and original
        filename; it does not rename a PNG/GIF to ``.jpg``. The current
        UEditor code passes a fixed quality of ``0.8`` (the caller supplies
        this as ``80``); PNG/GIF encoders may ignore it. Other extensions do
        not enter this conversion branch and are sent byte-for-byte
        unchanged.
        """
        import io
        from PIL import Image, ImageOps

        border = int(border)
        quality = int(quality)
        if border <= 0 or quality <= 0 or quality > 100:
            raise ValueError('网页图片压缩参数无效')
        suffix = Path(str(filepath)).suffix.lower()
        if suffix not in ('.jpg', '.jpeg', '.png', '.gif'):
            with open(filepath, "rb") as handle:
                return handle.read(), os.path.basename(filepath), False
        with Image.open(filepath) as source:
            width, height = source.size
            image = ImageOps.exif_transpose(source)
            scale = min(1.0, border / float(max(width, height)))
            target = (max(1, round(width * scale)), max(1, round(height * scale)))
            if target != image.size:
                image = image.resize(target, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if suffix in ('.jpg', '.jpeg'):
                # Stock WebUploader's dialog compressor is configured with
                # ``preserveHeaders:true``.  Preserve the browser-visible
                # EXIF/ICC payload after orientation normalization instead of
                # silently stripping it in the Pillow equivalent.  Some
                # malformed profiles cannot be serialized; in that case the
                # pixel upload remains usable and we fall back to the same
                # JPEG encode without optional headers.
                save_kwargs = {}
                try:
                    exif = image.getexif().tobytes()
                except Exception:
                    exif = b''
                icc_profile = source.info.get('icc_profile')
                if exif:
                    save_kwargs['exif'] = exif
                if icc_profile:
                    save_kwargs['icc_profile'] = icc_profile
                converted = image.convert('RGB')
                try:
                    converted.save(buf, format='JPEG', quality=quality,
                                   **save_kwargs)
                except (OSError, ValueError, TypeError):
                    buf.seek(0)
                    buf.truncate(0)
                    converted.save(buf, format='JPEG', quality=quality)
            elif suffix == '.png':
                image.save(buf, format='PNG')
            else:  # .gif: canvas conversion produces one resized frame
                image.convert('RGBA').save(buf, format='GIF')
            return buf.getvalue(), os.path.basename(filepath), True

    def _guess_mime(self, filename, file_data=b''):
        import mimetypes
        if not Path(str(filename or '')).suffix:
            detected = sniff_mime(file_data, filename)
            if detected:
                return detected
        ext = Path(filename).suffix.lower()
        return {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp",
            ".svg": "image/svg+xml", ".avif": "image/avif",
            ".heic": "image/heic", ".heif": "image/heif",
            ".ico": "image/x-icon", ".jxl": "image/jxl",
            ".jp2": "image/jp2", ".j2k": "image/jp2",
            ".jpf": "image/jp2", ".jpx": "image/jp2",
            ".jpm": "image/jp2", ".psd": "image/vnd.adobe.photoshop",
            ".mkv": "video/x-matroska", ".m4v": "video/x-m4v",
            ".3gp": "video/3gpp", ".3g2": "video/3gpp2",
            ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
            ".asf": "video/x-ms-asf", ".rm": "video/x-pn-realvideo",
            ".rmvb": "video/vnd.rn-realvideo", ".ts": "video/mp2t",
            ".mts": "video/mp2t", ".m2ts": "video/mp2t",
            ".oga": "audio/ogg", ".aac": "audio/aac",
            ".flac": "audio/flac", ".opus": "audio/opus",
            ".amr": "audio/amr", ".ape": "audio/ape",
            ".mid": "audio/midi", ".midi": "audio/midi",
            ".mka": "audio/x-matroska", ".wma": "audio/x-ms-wma",
            ".caf": "audio/x-caf", ".ac3": "audio/vnd.dolby.dd-raw",
            ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff",
            ".woff2": "font/woff2", ".eot": "application/vnd.ms-fontobject",
        }.get(ext, mimetypes.guess_type(filename)[0] or "application/octet-stream")

    def _detect_image_type(self, file_data, filename):
        """Return extension/MIME matching actual bytes after compression."""
        raw = bytes(file_data or b'')
        if raw.startswith(b"\xff\xd8\xff"):
            return ".jpg", "image/jpeg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png", "image/png"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return ".gif", "image/gif"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return ".webp", "image/webp"
        # SVG is text/XML rather than a Pillow-readable raster.  Keep the
        # signature check here lightweight; the active upload policy still
        # decides whether the current CMS control accepts the extension.
        if re.match(rb"^\s*(?:\xef\xbb\xbf)?<svg(?:\s|>)", raw[:4096], re.I):
            return ".svg", "image/svg+xml"
        # AVIF/HEIF are ISO-BMFF containers and JPEG XL may be either a
        # codestream or an ISO-BMFF container.  Detect their signatures even
        # when an object-storage URL has no extension so the multipart part
        # still receives the same MIME/filename semantics as a browser File.
        if len(raw) >= 12 and raw[4:8] == b"ftyp":
            brands = []
            for offset in range(8, min(len(raw) - 3, 64), 4):
                brand = raw[offset:offset + 4].lower()
                if all(32 <= byte < 127 for byte in brand):
                    brands.append(brand)
            if any(brand in {b"avif", b"avis"} for brand in brands):
                return ".avif", "image/avif"
            if any(brand in {b"heic", b"heix", b"hevc", b"hevx",
                            b"mif1", b"msf1"} for brand in brands):
                return ".heic", "image/heic"
        if len(raw) >= 12 and raw[4:12] == b"JXL \x0d\x0a\x87\x0a":
            return ".jxl", "image/jxl"
        if raw.startswith(b"\xff\x0a"):
            return ".jxl", "image/jxl"
        ext = Path(filename).suffix.lower()
        detected = sniff_mime(raw, filename)
        if detected.startswith('image/'):
            return sniff_extension(raw, filename), detected
        return ext, self._guess_mime(filename, raw)

    def _parse_upload_response(self, resp):
        """Compatibility helper, rejecting contradictory failures and bare URLs."""
        return parse_upload_result(resp)["path"] or None

    @staticmethod
    def _json_write_outcome(result, success_default, failure_default):
        """Return ``(True/False/None, message)`` for a JSON write reply."""
        if not isinstance(result, dict):
            return None, ""
        raw_message = (result.get("msg") or result.get("message")
                       or result.get("error") or result.get("data") or "")
        message = raw_message if isinstance(raw_message, str) else str(raw_message)
        # A positive code never overrides an explicit failure marker.
        if result.get('error') not in (None, '', False, 0, [], {}):
            return False, str(result['error'])[:300] or failure_default
        if 'success' in result and not (result['success'] is True or
                str(result['success']).lower() in ('1', 'true', 'success')):
            return False, failure_default + '（后台包含明确失败标志）'
        if 'state' in result and str(result['state']).upper() != 'SUCCESS':
            return False, failure_default + '（后台包含明确失败状态）'
        if _RESPONSE_ERROR_RE.search(message):
            return False, message[:300] or failure_default
        if "code" in result:
            code = result.get("code")
            if code in (1, "1", True, "success", "SUCCESS"):
                return True, message[:300] or success_default
            return False, message[:300] or failure_default
        if "success" in result:
            success = result.get("success")
            if success is True or str(success).lower() in ("1", "true", "success"):
                return True, message[:300] or success_default
            return False, message[:300] or failure_default
        return None, message[:300]

    def _verify_new_article(self, scode, title, known_article_ids):
        """Locate a unique new candidate and check its full edit-form identity.

        A list's only new ID or truncated display title is not sufficient.
        This remains readback evidence, not a server-issued idempotency key.
        """
        if known_article_ids is None or not str(title or ""):
            return None, ""
        before = {str(item) for item in known_article_ids}
        try:
            after = self.get_article_list(scode)
            if not getattr(after, "complete", False):
                return None, ""
            candidates = [item for item in after if str(item.get("id", "")) not in before]
            if not candidates:
                return False, ""
            if len(candidates) > 1:
                candidates = [item for item in candidates if str(item.get("title", "")) == str(title)]
            if len(candidates) != 1:
                return None, ""
            candidate = candidates[0]
            article_id = str(candidate.get("id", ""))
            if not article_id.isdigit():
                return None, ""
            _fc, _fields, current = self.get_edit_form(
                article_id, getattr(self, "_write_mcode", None),
                edit_url=candidate.get("edit_url") or None)
            if str(current.get("title", "")) != str(title):
                return None, ""
            if "scode" in current and str(current["scode"]) != str(scode):
                return None, ""
            if "id" in current and str(current["id"]) != article_id:
                return None, ""
            return True, article_id
        except Cancelled:
            raise
        except Exception as exc:
            debug_log(f"[发布验证] 无法确认新增记录身份: {type(exc).__name__}")
            return None, ""

    def _verify_edited_fields(self, article_id, mcode, fields_dict, edit_page_url):
        """Verify all business fields and absent controls, including rich media."""
        self._last_field_verification = {
            "status": "unverified", "matched": [], "different": [],
            "unverified": list(fields_dict), "normalization_possible": []}
        try:
            _fc, fields, current = self.get_edit_form(
                article_id, mcode, edit_url=edit_page_url)
        except Cancelled:
            raise
        except Exception as exc:
            debug_log(f"[保存验证] 回读不可用: {type(exc).__name__}")
            return None, []
        if not fields and not current:
            return None, []
        if "id" in current and str(current["id"]) != str(article_id):
            return None, []
        if "mcode" in current and str(current["mcode"]) != str(mcode):
            return None, []
        report = compare_saved_fields(
            fields_dict, current, fields,
            getattr(self, "_write_expected_absent", ()))
        self._last_field_verification = report
        if report["status"] == "verified":
            return True, []
        if report["status"] == "different":
            return False, report["different"]
        return None, report["unverified"]

    def _save_outcome(self, outcome, message, *, article_id="", reported=False):
        attempted = bool(getattr(self, "last_write_result", {}).get("write_attempted"))
        self.last_write_result = {
            "outcome": outcome, "write_attempted": attempted,
            "backend_reported_success": bool(reported),
            "article_id": str(article_id or ""), "retryable": False,
            "requires_review": attempted and outcome not in ("verified", "rejected"),
            "verification": dict(getattr(self, "_last_field_verification", {}) or {})}
        return outcome == "verified", message

    def _complete_content_write(self, response, expected, *, article_id=None,
                                mcode=None, edit_page_url=None, scode=None,
                                known_article_ids=None):
        """One post-write classifier. Never resubmit to resolve uncertainty."""
        status = getattr(response, "status_code", 0)
        text = response.text
        if not 200 <= status < 300:
            outcome = "rejected" if 400 <= status < 500 and status != 408 else "unknown"
            try:
                return self._save_outcome(outcome,
                    f"后台返回HTTP {status}；本次请求已发送，请核对后台，勿直接重复提交",
                    article_id=article_id)
            finally:
                _close_response(response)
        error = _response_error_message(text)
        if error:
            try:
                return self._save_outcome("rejected", error, article_id=article_id)
            finally:
                _close_response(response)
        reported, message = False, ""
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        finally:
            # The body and headers needed for classification are now
            # consumed.  Return the connection to the pool before issuing
            # the mandatory post-write readback GET.
            _close_response(response)
        if payload is not None:
            result, message = self._json_write_outcome(payload, "后台报告保存成功", "后台拒绝保存")
            if result is False:
                return self._save_outcome("rejected", message, article_id=article_id)
            reported = result is True
        if not reported:
            message = _response_success_message(text)
            reported = bool(message)
        # A redirect/list URL alone is not a positive acknowledgement.
        if article_id is None:
            found, article_id = self._verify_new_article(
                scode, expected.get("title", ""), known_article_ids)
            if not found:
                detail = "回读未发现本次新文章" if found is False else "无法唯一确认本次新增记录"
                outcome = "reported_unverified" if reported else "unknown"
                return self._save_outcome(outcome,
                    ("后台报告成功，但" if reported else "请求已发送，但") + detail +
                    "；结果待核对，请勿直接重复发布", reported=reported)
        verified, differences = self._verify_edited_fields(
            article_id, mcode, expected, edit_page_url)
        if verified is True:
            return self._save_outcome("verified",
                "已回读核验全部提交业务字段（不含前台渲染及媒体文件内容）",
                article_id=article_id, reported=reported)
        if verified is False:
            missing = getattr(self, '_last_field_verification', {}).get('unverified', [])
            return self._save_outcome("different",
                "回读字段存在差异：" + "、".join(differences) +
                ("；另有字段未能核验：" + "、".join(missing) if missing else "") +
                "；可能涉及后台规范化、并发修改或未按预期保存，不等同于整次保存失败。请先核对，不要直接重复提交",
                article_id=article_id, reported=reported)
        return self._save_outcome("reported_unverified" if reported else "unknown",
            ("后台报告成功，但" if reported else "请求已发送，但") +
            "未能核验全部业务字段" + ("（" + "、".join(differences) + "）" if differences else "") +
            "；请先核对后台，不要直接重复提交",
            article_id=article_id, reported=reported)

    def publish_content(self, scode, fields_dict, formcheck="", mcode=None,
                        known_article_ids=None, add_url_hint=None,
                        cancel_callback=None, submitter=None):
        """提交内容到PbootCMS，返回 (success, message)
        发布接口为 POST /Content/add/mcode/{mcode}
        mcode: 调用方已知内容模型时直接传入，跳过重复探测

        显式 date（包括打开新增页时取得的默认值）原样保留。
        未提供 date 时沿用实际新增表单默认值，不为无此字段的模型虚构日期。
        兼容旧 __auto_date__ 标记：仅显式带此标记时改用刷新表单的日期。
        """
        self.last_write_result = {'outcome':'not_sent', 'write_attempted':False}
        self._last_field_verification = {}
        self._write_expected_absent = []
        self._content_add_native_url = ""
        self._content_add_native_reason = ""
        fields_dict = dict(fields_dict or {})
        auto_date = bool(fields_dict.pop("__auto_date__", False))
        auto_date = auto_date or "date" not in fields_dict
        if cancel_callback:
            cancel_callback()

        # 先确定 mcode（统一解析 + 缓存；若调用方已知可直接传入）
        debug_log(f"[发布开始] scode={scode}")
        if mcode is None:
            mcode = self._resolve_mcode(scode)
        if mcode is None:
            return False, f"无法确定栏目 {scode} 所属的内容模型(mcode)"
        self._write_mcode = mcode

        # Never write with a stale token or a login/error page masquerading as
        # HTTP 200. Read-only refresh must establish an actual add form first.
        fresh_defaults = {}
        fresh_fields = []
        fresh_add_action = ""
        fresh_method = "post"
        fresh_enctype = "application/x-www-form-urlencoded"
        fresh_submission_pairs = []
        try:
            refresh_resp = self._read_request(
                "GET", self._url(f"Content/index/mcode/{mcode}"), timeout=15)
            reference = self.base_url or self.admin_url
            resolved = getattr(refresh_resp, "url", "") or self._url(f"Content/index/mcode/{mcode}")
            if not (_same_origin(resolved, reference) or _same_host_http_upgrade(resolved, reference)):
                return False, "新增表单跳转到其他站点，已停止提交"
            if not refresh_resp.ok:
                return False, f"无法刷新新增表单（HTTP {refresh_resp.status_code}），未提交内容"
            refresh_soup = BeautifulSoup(refresh_resp.text, "html.parser")
            edit_form = _discover_article_form(refresh_soup)
            if edit_form is None:
                return False, "未读取到有效新增表单，登录可能已失效；未提交内容"
            from admin_modules import _dynamic_form_reason
            dynamic_reason = _dynamic_form_reason(refresh_soup, edit_form, resolved)
            if dynamic_reason:
                native_url = self._url(f"Content/add/mcode/{mcode}")
                native_url += ("&" if "?" in native_url else "?") + urlencode({"scode": scode})
                self._content_add_native_url = native_url
                self._content_add_native_reason = dynamic_reason
                self.last_write_result.update({
                    "outcome": "native_only", "write_attempted": False,
                    "native_url": native_url, "native_reason": dynamic_reason,
                })
                return False, (dynamic_reason + "；内容未提交，请使用原生网页发布")
            self.adopt_resolved_url(resolved)
            submitter, submitter_options, submitter_error = _resolve_form_submitter(
                edit_form, submitter, "新增内容表单")
            if submitter_error:
                return False, submitter_error
            if submitter:
                debug_log(f"[发布] 按网页语义选择提交按钮: {submitter.get('name') or submitter.get('value') or '(无名按钮)'}")
            fresh_fields, fresh_defaults = _describe_form_controls(
                edit_form, self._find_label, submitter=submitter)
            excluded = {"listall[]", "list[]"}
            excluded.update(e.get("name") for e in form_elements(edit_form)
                            if str(e.get("name", "")).startswith("urls["))
            fresh_submission_pairs = successful_pairs(
                edit_form, exclude=excluded, submitter=submitter)
            form_transport = _form_method_enctype(edit_form, submitter)
            fresh_method = form_transport['method']
            fresh_enctype = form_transport['enctype']
            if isinstance(submitter, dict) and submitter.get('formaction'):
                candidate_submitter = urljoin(resolved, form_transport['action'])
                if not (_same_origin(candidate_submitter, self.base_url or self.admin_url)
                        or _same_host_http_upgrade(candidate_submitter, self.base_url or self.admin_url)):
                    return False, "提交按钮覆盖地址指向了其他站点，已安全中止"
                fresh_add_action = candidate_submitter
            token_values = serialize_form(edit_form)
            if 'formcheck' in token_values:
                fresh_defaults['formcheck'] = token_values['formcheck']
            action = str(edit_form.get("action", "") or "").strip()
            if action:
                candidate_action = urljoin(resolved, action)
                if not (_same_origin(candidate_action, self.base_url or self.admin_url)
                        or _same_host_http_upgrade(candidate_action, self.base_url or self.admin_url)):
                    return False, "新增内容表单提交地址指向了其他站点，已安全中止"
                fresh_add_action = candidate_action
        except Cancelled:
            raise
        except Exception as e:
            debug_log(f"[发布调试] 刷新新增表单失败，已停止提交: {e}")
            return False, "刷新新增表单失败，未提交内容；请重新加载栏目后重试"

        # Legacy auto-date requests explicitly opt into the fresh form default.
        # Never invent a local date for a model which has no date control.
        if auto_date:
            fields_dict.pop("date", None)
        data = merge_form_updates(fresh_defaults, fresh_fields, fields_dict)
        data["scode"] = scode
        data.update(_submitter_values(submitter))
        if "formcheck" in fresh_defaults:
            data["formcheck"] = fresh_defaults["formcheck"]
        else:
            data.pop("formcheck", None)
        transport_data = BrowserFormData.from_data(fresh_submission_pairs, data)

        # 调试：打印提交数据摘要（字段名/键常开；完整值仅详细模式落盘，避免内容原文长期留痕）
        debug_log(f"[发布请求] mcode={mcode}, scode={scode}, fields={list(data.keys())}")
        debug_log_v(
            f"[发布数据] {{{', '.join(f'{k}: {str(v)[:100]}' for k, v in data.items())}}}")

        submit_url = self._url(f"Content/add/mcode/{mcode}")
        hinted = str(fresh_add_action or add_url_hint or "").strip()
        if hinted:
            reference = self.base_url or self.admin_url
            if (_same_origin(hinted, reference)
                    or _same_host_http_upgrade(hinted, reference)):
                submit_url = hinted
            else:
                return False, "新增内容表单提交地址指向了其他站点，已安全中止"

        headers = {
            "Referer": self._url(f"Content/index/mcode/{mcode}"),
        }

        # 刷新表单的 GET 可能较慢；用户若在它进行期间取消，必须在 POST
        # 之前再检查一次，不能让“当前网络请求结束后停止”变成仍然写入。
        if cancel_callback:
            cancel_callback()
        expected, self._write_expected_absent = submission_expectations(data, fresh_fields)
        self.last_write_result['write_attempted'] = True
        try:
            resp = _submit_content_form(
                self, fresh_method, submit_url, transport_data, fresh_fields,
                fresh_enctype, headers)
        except Exception as exc:
            debug_log(f'[保存结果未知] {type(exc).__name__}')
            return self._save_outcome('unknown', '发布请求已发送但未取得结果；请核对后台，不要直接重复发布')

        try:
            return self._complete_content_write(
                resp, expected, mcode=mcode, scode=scode,
                known_article_ids=known_article_ids)
        except Exception as exc:
            debug_log(f'[发布核验未完成] {type(exc).__name__}')
            return self._save_outcome('unknown', '发布请求已发送，后续核验中断；取消不能撤销已发请求，请先核对后台')

    def get_article_list(self, scode):
        """获取指定栏目的文章列表，返回 [{"id":..., "title":...}, ...]"""
        articles = ArticleSnapshot()
        seen_ids = set()
        mcode = self._get_mcode_for_scode(scode)
        debug_log(f"[get_article_list] scode={scode}, mcode={mcode}")
        if not mcode:
            return articles
        try:
            page = 1
            next_page_url = ""
            while True:
                scoped_url = self._url(
                    f"Content/index/mcode/{mcode}/scode/{scode}")
                model_url = self._url(f"Content/index/mcode/{mcode}")
                if page == 1:
                    url_candidates = [
                        (scoped_url, {}),
                        (model_url, {"scode": scode}),
                    ]
                else:
                    # 标准 PbootCMS 分页使用 page。少数旧版/二开使用 p，
                    # 因此以页面真实“下一页”链接为最高优先级，
                    # 再按 page -> p 降级。候选返回旧 ID 时继续尝试，
                    # 避免参数被忽略后把第一页重复追加 50 次。
                    url_candidates = []
                    if next_page_url:
                        url_candidates.append((next_page_url, {}))
                    url_candidates.extend([
                        (scoped_url, {"page": page}),
                        (scoped_url, {"p": page}),
                        (model_url, {"scode": scode, "page": page}),
                        (model_url, {"scode": scode, "p": page}),
                    ])
                # 直接下一页链接与生成候选可能重复。
                unique_candidates = []
                candidate_keys = set()
                for candidate_url, candidate_params in url_candidates:
                    key = (candidate_url, tuple(sorted(candidate_params.items())))
                    if key not in candidate_keys:
                        candidate_keys.add(key)
                        unique_candidates.append((candidate_url, candidate_params))
                resp = None
                soup = None
                page_empty_confirmed = False
                for url_try, params_try in unique_candidates:
                    try:
                        r = self._read_request(
                            "GET", url_try, params=params_try, timeout=15)
                    except Exception as exc:
                        debug_log(
                            f"[get_article_list] try {url_try} params={params_try} error={exc}")
                        continue
                    debug_log(f"[get_article_list] try {url_try} params={params_try} status={r.status_code}")
                    if not r.ok or _is_login_page(r.text):
                        continue
                    candidate_soup = BeautifulSoup(r.text, "html.parser")
                    row_items = []
                    for candidate_row in candidate_soup.find_all("tr"):
                        candidate_cb = (
                            candidate_row.find("input", {"name": "list[]"}) or
                            candidate_row.find("input", {"name": "ids[]"}) or
                            candidate_row.find("input", {"type": "checkbox", "name": True})
                        )
                        candidate_id = ((candidate_cb.get("value") or "").strip()
                                        if candidate_cb else "")
                        if candidate_id.isdigit():
                            row_items.append(
                                (candidate_id, _article_row_scode(candidate_row)))
                    raw_ids = [item[0] for item in row_items]
                    category_evidence = any(item[1] for item in row_items)
                    if raw_ids and not category_evidence:
                        debug_log(
                            f"[get_article_list] 候选页无法核验栏目，已拒绝: {url_try}")
                        continue
                    candidate_ids = [
                        aid for aid, row_scode in row_items
                        if row_scode == str(scode)]
                    if raw_ids and not candidate_ids:
                        debug_log(
                            f"[get_article_list] 候选页没有目标栏目 scode={scode}，"
                            f"可能过滤参数被忽略: {params_try}")
                        continue
                    if page > 1 and not any(aid not in seen_ids for aid in candidate_ids):
                        debug_log(
                            f"[get_article_list] page={page} 候选未返回新ID，"
                            f"继续尝试其他分页参数: {params_try}")
                        continue
                    resp = r
                    soup = candidate_soup
                    page_empty_confirmed = (not raw_ids and bool(re.search(
                        r"暂无(?:相关)?数据|没有(?:相关)?数据|无数据|\bno\s+data\b",
                        candidate_soup.get_text(" ", strip=True), re.I)))
                    break
                if resp is None:
                    debug_log(f"[get_article_list] 所有URL尝试均失败")
                    break
                rows = soup.find_all("tr")
                debug_log(f"[get_article_list] page={page}, tr_count={len(rows)}")
                found = 0
                # 尝试多种checkbox名称
                for row in rows:
                    cb = (
                        row.find("input", {"name": "list[]"}) or
                        row.find("input", {"name": "ids[]"}) or
                        row.find("input", {"type": "checkbox", "name": True})
                    )
                    if not cb:
                        continue
                    aid = cb.get("value", "").strip()
                    if not aid or not aid.isdigit():
                        continue
                    row_scode = _article_row_scode(row)
                    if row_scode != str(scode):
                        debug_log(
                            f"[get_article_list] 跳过非目标栏目文章 ID={aid}, "
                            f"row_scode={row_scode or 'unknown'}, target={scode}")
                        continue
                    if aid in seen_ids:
                        continue
                    seen_ids.add(aid)

                    # 打印每行 HTML 片段供调试（详细模式才落盘）
                    debug_log_v(f"[get_article_list] article {aid} row_html: {str(row)[:500]}")

                    # 提取编辑链接和站点明确提供的前台/预览链接。
                    # 只消费当前列表行真实呈现的同源链接；不能用文章 ID
                    # 猜前台路由，因为不少二开站点使用 urlname、栏目重写
                    # 或完全不同的前台入口。
                    edit_url = ""
                    view_url = ""
                    all_links = row.find_all("a")
                    # 打印所有链接供调试（详细模式才落盘）
                    for a in all_links:
                        href = a.get("href", "")
                        text = a.get_text(strip=True)[:30]
                        debug_log_v(f"[get_article_list] article {aid} link: href={href}, text={text}")
                    for a in all_links:
                        href = a.get("href", "")
                        if ("mod" in href.lower() and "id" in href.lower() and
                                "field" not in href.lower()):  # /Content/mod/mcode/3/id/113, 排除 /Content/mod/id/113/field/xxx
                            edit_url = href
                            break
                    debug_log_v(f"[get_article_list] article {aid} edit_url={edit_url}")

                    # 浏览器列表通常会给出“查看/预览/前台/访问”链接；
                    # 这是真实前台渲染验收的唯一可靠入口。要求链接文本、
                    # title 或 aria-label 明确表达查看语义，并排除管理写入
                    #/跳转动作，避免把编辑/删除/状态切换 URL 当成前台页。
                    positive_view = re.compile(
                        r"查看|预览|前台|访问|详情|view|preview|front|visit|detail",
                        re.I)
                    negative_view = re.compile(
                        r"编辑|修改|删除|移[动除]|复制|排序|状态|field[=/]|value[=/]|"
                        r"\b(?:mod|edit|delete|remove|toggle|status)\b",
                        re.I)
                    reference = self.base_url or self.admin_url
                    for anchor in all_links:
                        raw_href = str(anchor.get("href", "") or "").strip()
                        if (not raw_href or raw_href.lower().startswith((
                                "javascript:", "mailto:", "#"))):
                            continue
                        label = " ".join(filter(None, (
                            anchor.get_text(" ", strip=True),
                            str(anchor.get("title", "") or "").strip(),
                            str(anchor.get("aria-label", "") or "").strip(),
                        )))
                        if not positive_view.search(label) or negative_view.search(label):
                            # href 也参与排除，但不单独作为正向证据；
                            # 自定义 slug 的链接往往没有可推断的路由名称。
                            continue
                        candidate = urljoin(
                            getattr(resp, "url", "") or self.admin_url,
                            raw_href)
                        low_candidate = candidate.lower()
                        if negative_view.search(low_candidate):
                            continue
                        if not (_same_origin(candidate, reference) or
                                _same_host_http_upgrade(candidate, reference)):
                            debug_log_v(
                                f"[get_article_list] 拒绝跨站前台链接 ID={aid}: {candidate}")
                            continue
                        view_url = candidate
                        break
                    debug_log_v(f"[get_article_list] article {aid} view_url={view_url}")

                    # 提取标题：找第一个非操作链接（不含 edit/delete/move 等）
                    title = ""
                    tds = row.find_all("td")
                    # 优先从 td 直接文本提取标题（不在链接里），跳过栏目列
                    title = ""
                    for td in tds:
                        # 跳过栏目列：title 属性是纯数字的 td 是栏目 ID
                        td_title = td.get("title", "").strip()
                        if td_title and td_title.isdigit():
                            continue
                        # 获取 td 内直接子文本
                        direct_text = "".join(
                            t.strip() for t in td.find_all(string=True, recursive=False)
                            if t.strip()
                        ).strip()
                        all_text = direct_text or td.get_text(strip=True)
                        if all_text and len(all_text) > 3 and all_text != aid and not all_text.isdigit():
                            title = all_text[:80]
                            break
                    # 备用：从链接文本提取
                    if not title:
                        for td in tds:
                            td_title = td.get("title", "").strip()
                            if td_title and td_title.isdigit():
                                continue
                            for a in td.find_all("a"):
                                href = a.get("href", "")
                                text = a.get_text(strip=True)
                                if not text or len(text) <= 2:
                                    continue
                                if any(kw in href.lower() for kw in ["edit", "delete", "move", "copy", "mod", "del"]):
                                    continue
                                if any(kw in text for kw in ["编辑", "删除", "移动", "复制", "查看", "修改"]):
                                    continue
                                title = text[:80]
                                break
                            if title:
                                break
                    if not title:
                        title = f"文章#{aid}"
                    title = re.sub(r'\s+', ' ', title).strip()

                    articles.append({"id": aid, "title": title,
                                     "edit_url": edit_url,
                                     "view_url": view_url})
                    found += 1
                debug_log(f"[get_article_list] found={found}, total_so_far={len(articles)}")
                if not found:
                    # 打印前500字符帮助诊断（详细模式才落盘，避免重复记录整页内容）
                    debug_log_v(f"[get_article_list] 页面内容片段: {resp.text[:500]}")
                    if page == 1 and page_empty_confirmed:
                        articles.complete = True
                    break
                # 优先跟随后台生成的真实 href，它会准确反映
                # 本站使用 page、p 还是路径分页。
                has_next = False
                next_page_url = ""
                for anchor in soup.find_all("a", href=True):
                    label = anchor.get_text(" ", strip=True)
                    if not re.search(r"^(?:下一页|下页|next)$", label, re.I):
                        continue
                    has_next = True
                    href = (anchor.get("href") or "").strip()
                    if href and href not in ("#", "javascript:;") \
                            and not href.lower().startswith("javascript:"):
                        next_page_url = urljoin(resp.url, href)
                    break
                if not has_next:
                    articles.complete = True
                    break
                page += 1
        except Exception as e:
            debug_log(f"[get_article_list] 错误: {e}")
        return articles

    def _resolve_mcode(self, scode):
        """确定栏目 scode 所属的内容模型(mcode)，带实例级缓存，避免重复请求。
        返回 mcode 字符串或 None。
        """
        if not hasattr(self, '_mcode_cache'):
            self._mcode_cache = {}
        scode_str = str(scode)
        if scode_str in self._mcode_cache:
            return self._mcode_cache[scode_str]

        # Prefer the explicit category-tree link over a model page's broad
        # scode selector.  The latter is shared by several stock Pboot models
        # and cannot by itself identify the active mcode.
        mapped = self._category_mcode_from_routes(scode_str)
        if mapped:
            self._mcode_cache[scode_str] = mapped
            debug_log(f"[_resolve_mcode] scode={scode_str} -> mcode={mapped} (category route)")
            return mapped

        for try_mcode in self._content_mcode_candidates():
            for attempt in range(2):
                try:
                    resp = self._read_request(
                        "GET", self._url(f"Content/index/mcode/{try_mcode}"),
                        timeout=20)
                    if not resp.ok:
                        break
                    soup = BeautifulSoup(resp.text, "html.parser")
                    if soup.find("option", {"value": scode_str}):
                        debug_log(f"[_resolve_mcode] scode={scode} -> mcode={try_mcode}")
                        self._mcode_cache[scode_str] = try_mcode
                        return try_mcode
                    break
                except Exception as e:
                    debug_log(f"[_resolve_mcode] error mcode={try_mcode} (attempt {attempt+1}): {e}")
                    if attempt == 0:
                        time.sleep(2)
        debug_log(f"[_resolve_mcode] scode={scode} NOT FOUND")
        return None

    def _get_mcode_for_scode(self, scode):
        """（兼容别名）获取栏目对应的 mcode（带缓存）"""
        return self._resolve_mcode(scode)

    def get_edit_form(self, article_id, mcode, edit_url=None, session=None, light=False,
                      xinghao_field=FIELD_XINGHAO, jiage_field=FIELD_JIAGE,
                      request_timeout=15, max_candidates=None):
        """获取文章编辑页表单，返回 (formcheck, fields, current_values)
        current_values: 文章当前内容字典
        edit_url: 从列表页抓取的实际编辑链接（优先使用）
        session: 可选，指定用于请求的 requests.Session（并发场景下传独立 session，
                 避免多线程共享主 client.session 引发崩溃）；默认用 self.session
        """
        self._content_edit_native_url = ""
        self._content_edit_native_reason = ""
        self._content_edit_page_url = ""
        try:
            sess = session or self.session
            # 优先使用从列表页抓取的编辑链接（去掉 &backurl 参数）
            url_candidates = []
            if edit_url:
                # edit_url 可能是相对路径或绝对路径，去掉 &backurl=... 避免问题
                clean_url = edit_url.split("&backurl=")[0] if "&backurl=" in edit_url else edit_url
                # 安全护栏：PbootCMS 的「状态/字段切换」链接形如
                # ?p=/Content/mod/id/58/field/status/value/0 —— GET 它【即执行写入】，
                # 会把该产品 status 改成 0（隐藏）。此类写操作 URL 绝不能作为编辑页去 GET，
                # 否则「拉取最新数据」也会误改后台。命中则丢弃，改走下方默认编辑页候选。
                _is_write_url = bool(re.search(r"field/[^/]+/value", clean_url)) or "field=" in clean_url
                if _is_write_url:
                    debug_log(f"[get_edit_form] ⚠ 拒绝疑似写操作URL(改走默认编辑页): {clean_url}")
                elif clean_url.startswith("http"):
                    if _same_origin_or_http_upgrade(clean_url, self.base_url or self.admin_url):
                        url_candidates.append(clean_url)
                    else:
                        debug_log(
                            f"[get_edit_form] 拒绝跨站编辑页URL: {clean_url}")
                else:
                    # 相对链接：以 admin_url（后台入口）为基准，适配 "?p=..." 形式；
                    # 路径形式（/admin.php/...）才拼 base_url
                    if clean_url.startswith("?"):
                        url_candidates.append(f"{self.admin_url}{clean_url}")
                    else:
                        url_candidates.append(f"{self.base_url}{clean_url}")
            # 添加默认 URL 作为备选：get_edit_form �����【纯读】表单，只用 Content/mod（显示表单
            # 端点）打开，不把 Content/edit（保存端点）放进 GET 候选，避免以 GET 触碰保存动作。
            url_candidates.extend([
                self._url(f"Content/mod/mcode/{mcode}/id/{article_id}"),
                self._url(f"Content/mod/id/{article_id}/mcode/{mcode}"),
            ])
            # A targeted read-only lookup during an internal-link check needs
            # a strict latency budget.  The ordinary product sync retains its
            # existing fallback behavior because it leaves this unset.
            url_candidates = list(dict.fromkeys(url_candidates))
            if max_candidates is not None:
                try:
                    url_candidates = url_candidates[:max(1, int(max_candidates))]
                except (TypeError, ValueError):
                    url_candidates = url_candidates[:1]
            resp = None
            for url_try in url_candidates:
                debug_log(f"[get_edit_form] try url={url_try}")
                try:
                    r = sess.get(url_try, timeout=request_timeout)
                    debug_log(f"[get_edit_form] status={r.status_code}")
                    if r.ok:
                        # ``requests.Response`` always exposes ``url``, but
                        # lightweight adapters/tests may only implement the
                        # response body/status surface.  Falling back to the
                        # requested URL keeps form discovery compatible
                        # without weakening the same-origin check.
                        resolved_url = getattr(r, "url", "") or url_try
                        reference = self.base_url or self.admin_url
                        if not (_same_origin(resolved_url, reference)
                                or _same_host_http_upgrade(resolved_url, reference)):
                            debug_log(
                                f"[get_edit_form] 拒绝跨站重定向页: {resolved_url}")
                            continue
                        # 确认该页确为编辑页（含目标字段名），避免登录页/列表页被误当编辑页
                        # Product-only extension fields cannot identify a
                        # generic edit page: news/case/video models may not
                        # contain ext_xinghao or ext_jiage at all.
                        page = BeautifulSoup(r.text, "html.parser")
                        candidate = _discover_article_form(page)
                        if candidate:
                            from admin_modules import _dynamic_form_reason
                            dynamic_reason = _dynamic_form_reason(page, candidate,
                                                                  resolved_url)
                            if dynamic_reason:
                                self._content_edit_native_url = resolved_url
                                self._content_edit_native_reason = dynamic_reason
                                return "", [], {}
                            # Product/special models may have no title or
                            # rich-text control, and custom installations may
                            # omit formcheck.  The route-aware selector has
                            # already excluded search forms; accepting the
                            # selected form here keeps the edit flow aligned
                            # with the actual backend model.
                            resp = r
                            self._content_edit_page_url = str(
                                resolved_url or url_try).strip()
                            break
                        debug_log(f"[get_edit_form] 页面不含目标字段名，尝试下一候选URL")
                except Exception as req_err:
                    debug_log(f"[get_edit_form] 请求失败: {req_err}")
            if resp is None:
                debug_log(f"[get_edit_form] 所有URL尝试均失败或无编辑表单字段")
                return "", [], {}
            # 完整解析编辑表单，按真实字段名取当前值（型号/价格多为普通 input，
            # 直接取 current_values[field] 即可；连接复用已保证整体加载速度）
            soup = BeautifulSoup(resp.text, "html.parser")
            # 文章页可能同时含搜索/筛选表单；只接受路由/业务字段可
            # 识别的唯一候选，无法唯一确认时宁可不读写。
            edit_form = _discover_article_form(soup)
            if not edit_form:
                debug_log(f"[get_edit_form] 没找到唯一内容form, forms_count={len(soup.find_all('form'))}")
                return "", [], {}

            formcheck = ""
            fc = edit_form.find("input", {"name": "formcheck"})
            if fc:
                formcheck = fc.get("value", "")

            default_submitter = _discover_default_submitter(edit_form)
            self._content_edit_submitter = default_submitter
            self._content_edit_submitter_options = _discover_submitter_options(edit_form)
            fields, current_values = _describe_form_controls(
                edit_form, self._find_label, submitter=default_submitter)
            # Some PbootCMS installations expose an editable category
            # selector on the article edit page.  ``scode`` is deliberately a
            # non-mappable system field on *publish* forms, but hiding it on
            # an edit form makes the desktop operation differ from clicking
            # the same selector in the browser.  Only opt in when the actual
            # DOM exposes a non-readonly select with at least two choices;
            # otherwise keep the historical fail-closed behavior.
            for field in fields:
                options = [item for item in (field.get("options") or [])
                           if not item.get("disabled")]
                if (str(field.get("name", "") or "") == "scode" and
                        str(field.get("type", field.get("kind", "")) or "").lower() == "select" and
                        not field.get("readonly") and not field.get("disabled") and
                        len(options) > 1):
                    field["mappable"] = True
                    field["edit_category"] = True
            return formcheck, fields, current_values
        except Exception as e:
            debug_log(f"[get_edit_form] 错误: {e}")
            return "", [], {}

    def _extract_form_fields(self, html):
        """Read successful controls using the same serializer as the UI."""
        soup = BeautifulSoup(html, "html.parser")
        form = _discover_article_form(soup)
        if form is None:
            return "", {}
        excluded = {"listall[]", "list[]"}
        excluded.update(e.get("name") for e in form_elements(form)
                        if str(e.get("name", "")).startswith("urls["))
        values = serialize_form(form, exclude=excluded)
        token = values.pop("formcheck", "")
        return token, values

    def edit_content(self, article_id, mcode, fields_dict, formcheck="",
                      edit_url_hint=None, cancel_callback=None,
                      expected_fields=None, image_replacements=None,
                      expected_content_hash="", refresh_publish_date=False,
                      target_field="content", expected_absent_fields=None,
                      submitter=None):
        """提交内容修改，返回 (success, message)
        edit_url_hint: 从列表页抓取的实际编辑链接（优先使用）
        """
        self.last_write_result = {'outcome':'not_sent', 'write_attempted':False}
        self._last_field_verification = {}
        self._write_expected_absent = []
        self._write_mcode = mcode
        self._content_edit_native_url = ""
        self._content_edit_native_reason = ""
        self._content_edit_submitter = None
        self._content_edit_submitter_options = []
        try:
            fields_dict = dict(fields_dict or {})
            if cancel_callback:
                cancel_callback()
            # 确定可用的 URL
            edit_url = None
            edit_page_url = None
            current_values = {}   # 编辑表单全部字段的当前值（保住 status 等未被修改的字段）
            current_fields = []
            current_submission_pairs = []
            edit_method = "post"
            edit_enctype = "application/x-www-form-urlencoded"
            # 优先使用从列表页抓取的链接（去掉 &backurl 参数）
            url_candidates = []
            if edit_url_hint:
                clean_url = edit_url_hint.split("&backurl=")[0] if "&backurl=" in edit_url_hint else edit_url_hint
                # 安全护栏（与 get_edit_form 一致）：拒绝 PbootCMS 字段切换写操作 URL
                # （?p=/Content/mod/id/58/field/status/value/0），避免 GET 即写入误改状态。
                _is_write_url = bool(re.search(r"field/[^/]+/value", clean_url)) or "field=" in clean_url
                if _is_write_url:
                    debug_log(f"[edit_content] ⚠ 拒绝疑似写操作URL(改走默认编辑页): {clean_url}")
                elif clean_url.startswith("http"):
                    if _same_origin_or_http_upgrade(clean_url, self.base_url or self.admin_url):
                        url_candidates.append(clean_url)
                    else:
                        debug_log(f"[edit_content] 拒绝跨站编辑页URL: {clean_url}")
                else:
                    if clean_url.startswith("?"):
                        url_candidates.append(f"{self.admin_url}{clean_url}")
                    else:
                        url_candidates.append(urljoin(
                            (self.base_url or "").rstrip("/") + "/", clean_url))
            # 打开编辑表单只用 Content/mod（显示表单端点）；不 GET Content/edit（保存端点），
            # 避免以 GET 触碰保存动作。真正提交时下方用表单 action POST。
            url_candidates.extend([
                self._url(f"Content/mod/mcode/{mcode}/id/{article_id}"),
                self._url(f"Content/mod/id/{article_id}/mcode/{mcode}"),
            ])
            for url_try in url_candidates:
                try:
                    r = self._read_request("GET", url_try, timeout=15)
                    if r.ok:
                        reference = self.base_url or self.admin_url
                        if not (_same_origin(r.url, reference)
                                or _same_host_http_upgrade(r.url, reference)):
                            debug_log(f"[edit_content] 拒绝跨站重定向页: {r.url}")
                            continue
                        # The edit page may resolve a saved HTTP backend to
                        # HTTPS.  Adopt it before resolving a relative form
                        # action so the subsequent POST cannot lose its body
                        # in a 301 redirect.
                        self.adopt_resolved_url(r.url)
                        # 从表单 action 提取正确的 POST URL，并解析全部字段当前值
                        s = BeautifulSoup(r.text, "html.parser")
                        f = _discover_article_form(s)
                        if f:
                            from admin_modules import _dynamic_form_reason
                            dynamic_reason = _dynamic_form_reason(
                                s, f, getattr(r, "url", "") or url_try)
                            if dynamic_reason:
                                self._content_edit_native_url = getattr(r, "url", "") or url_try
                                self._content_edit_native_reason = dynamic_reason
                                self.last_write_result.update({
                                    "outcome": "native_only",
                                    "write_attempted": False,
                                    "native_url": self._content_edit_native_url,
                                    "native_reason": dynamic_reason,
                                })
                                return False, (dynamic_reason +
                                               "；文章未提交修改，请使用原生网页编辑")
                            submitter, submitter_options, submitter_error = _resolve_form_submitter(
                                f, submitter, "编辑表单")
                            if submitter_error:
                                return False, submitter_error
                            if submitter:
                                debug_log(f"[编辑] 按网页语义选择提交按钮: {submitter.get('name') or submitter.get('value') or '(无名按钮)'}")
                            current_fields, _ = _describe_form_controls(
                                f, self._find_label, submitter=submitter)
                            excluded = {"listall[]", "list[]"}
                            excluded.update(e.get("name") for e in form_elements(f)
                                            if str(e.get("name", "")).startswith("urls["))
                            current_submission_pairs = successful_pairs(
                                f, exclude=excluded, submitter=submitter)
                            form_transport = _form_method_enctype(f, submitter)
                            edit_method = form_transport['method']
                            edit_enctype = form_transport['enctype']
                            fc, current_values = self._extract_form_fields(r.text)
                            if fc:
                                formcheck = fc
                            # 使用表单 action，不是页面 URL
                            form_action = f.get("action", "")
                            if form_action:
                                candidate_action = urljoin(r.url, form_action)
                                if not _same_origin_or_http_upgrade(
                                        candidate_action,
                                        self.base_url or self.admin_url):
                                    debug_log(
                                        f"[edit_content] 拒绝跨站表单action: "
                                        f"{candidate_action}")
                                    return False, "编辑表单提交地址指向了其他站点，已安全中止"
                                edit_url = candidate_action
                            else:
                                edit_url = url_try
                            if isinstance(submitter, dict) and submitter.get('formaction'):
                                candidate_submitter = urljoin(r.url, form_transport['action'])
                                if not (_same_origin(candidate_submitter, self.base_url or self.admin_url)
                                        or _same_host_http_upgrade(candidate_submitter, self.base_url or self.admin_url)):
                                    return False, "提交按钮覆盖地址指向了其他站点，已安全中止"
                                edit_url = candidate_submitter
                        else:
                            # A login/error/list page with HTTP 200 is not an
                            # editable form. Never POST to this fallback page.
                            continue
                        edit_page_url = r.url
                        break
                except Cancelled:
                    raise
                except Exception as req_err:
                    debug_log(f"[edit_content] 请求失败: {req_err}")
            if not edit_url:
                return False, "无法访问编辑页面"
            if not _same_origin_or_http_upgrade(edit_url, self.base_url or self.admin_url):
                debug_log(f"[edit_content] 拒绝跨站POST目标: {edit_url}")
                return False, "内容提交地址与当前后台不同源，已安全中止"

            # A category move is safe only when the freshly-read edit DOM
            # itself exposes an enabled selector containing the requested
            # value.  Never trust a stale UI value or let a hidden/text
            # ``scode`` control turn an ordinary edit into an unverified
            # route change.
            if "scode" in fields_dict:
                scode_field = next((field for field in current_fields
                                    if str(field.get("name", "") or "") == "scode"), None)
                if not scode_field or scode_field.get("readonly") or scode_field.get("disabled"):
                    return False, "当前编辑表单不允许修改栏目，已停止提交"
                options = list(scode_field.get("options") or [])
                requested_scode = str(fields_dict.get("scode", "") or "")
                if options:
                    allowed = {str(item.get("value", "") or "")
                               for item in options if not item.get("disabled")}
                    if requested_scode not in allowed:
                        return False, "目标栏目不在当前编辑表单允许范围内，已停止提交"

            # Bulk maintenance may be based on a grid loaded minutes ago.
            # Compare its expected model/price with the form values fetched
            # immediately before POST, so a newer backend edit is never
            # silently overwritten.  Validation happens before any write.
            mismatched_expected = []
            mismatched_expected.extend(str(name) for name in expected_absent_fields or []
                                       if name in current_values)
            for name, expected in dict(expected_fields or {}).items():
                actual = current_values.get(name)
                def normalized(value):
                    if isinstance(value, (list, tuple)):
                        return [text_value(v) for v in value]
                    return text_value(value)
                if name not in current_values or normalized(actual) != normalized(expected):
                    mismatched_expected.append(str(name))
            if mismatched_expected:
                return False, (
                    "后台字段已被其他操作修改（" +
                    "、".join(mismatched_expected) +
                    "），已停止提交；请同步最新数据后重新确认")

            # Detail-image replacements are applied only to the newly fetched
            # body.  Uploading can be slow, therefore modifying the snapshot
            # held by the UI would otherwise overwrite a concurrent CMS edit.
            replacements = list(image_replacements or [])
            if replacements:
                current_content = str(current_values.get(target_field, "") or "")
                current_hash = hashlib.sha256(
                    current_content.encode("utf-8")).hexdigest()
                if not expected_content_hash or current_hash != str(expected_content_hash):
                    return False, "文章正文已被其他操作修改，请重新加载文章后再提交"
                try:
                    updated_content, replaced = replace_image_occurrences(
                        current_content, replacements)
                except ValueError as exc:
                    return False, str(exc)
                if replaced != len(replacements):
                    return False, "详情图片替换不完整，已停止提交"
                fields_dict[target_field] = updated_content

            # Refresh is opt-in. Otherwise the existing form date or explicit
            # mapped date is preserved, exactly like an ordinary web edit.
            if refresh_publish_date:
                if "date" not in current_values:
                    return False, "当前编辑表单没有 date 字段，无法保证更新时间，已停止提交"
                fields_dict["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            debug_log(f"[edit_content] POST URL={edit_url}")

            # 构建提交数据：先放入编辑表单【全部字段的当前值】，再用调用方要改的字段覆盖。
            # 关键修复：PbootCMS 的 Content/edit 在收到“局部 POST”时会把未提交的字段
            # （status/istop/isrecommend/isheadline 等）写成默认值（0/隐藏），导致每次改
            # 型号/价格都把文章/产品的“状态”开关误关。现在携带全部当前值，只覆盖目标字段，
            # 未改动的字段（含 status）保持原样，后台不再清零。
            data = merge_form_updates(current_values, current_fields, fields_dict)
            data["formcheck"] = formcheck
            data.update(_submitter_values(submitter))
            # Preserve real hidden controls, not caller-invented system keys.
            for name in ("ac", "id", "mcode"):
                if name not in current_values:
                    data.pop(name, None)
            transport_data = BrowserFormData.from_data(current_submission_pairs, data)

            debug_log(f"[edit_content] article_id={article_id}, mcode={mcode}, fields={list(data.keys())}")
            debug_log_v(
                f"[edit_content] CRITICAL: ac={data.get('ac')}, id={data.get('id')}, "
                f"scode={data.get('scode')}, status={data.get('status')}, mcode_in_data={data.get('mcode')}")
            debug_log_v(f"[edit_content] FULL DATA DICT: {data}")

            if cancel_callback:
                cancel_callback()
            expected, self._write_expected_absent = submission_expectations(data, current_fields)
            self.last_write_result['write_attempted'] = True
            resp = _submit_content_form(
                self, edit_method, edit_url, transport_data, current_fields,
                edit_enctype, {"Referer": edit_url})
            return self._complete_content_write(
                resp, expected, article_id=article_id, mcode=mcode,
                edit_page_url=edit_page_url)
        except Cancelled:
            if self.last_write_result.get('write_attempted'):
                return self._save_outcome('unknown', '修改请求已发送，取消不能撤销；请先核对后台', article_id=article_id)
            raise
        except Exception as e:
            if self.last_write_result.get('write_attempted'):
                return self._save_outcome('unknown', '修改请求已发送但未完成结果核验；请核对后台，不要直接重复提交', article_id=article_id)
            return False, f"修改异常: {e}"
