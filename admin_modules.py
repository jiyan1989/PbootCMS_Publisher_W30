"""Safe discovery and editing of CMS modules not covered by a dedicated mixin.

The web backend exposes different menus and permissions per installation.  A
generic module adapter therefore never invents a route: it discovers a
same-origin menu link, reads its real form, preserves successful controls and
requires a revision before a safe form submission.  Destructive/list mutation links are not
offered by this adapter; those remain in their dedicated, route-aware mixes.
"""

import hashlib
import os
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from client_utils import _is_login_page
from form_controls import (describe_form, form_elements, successful_pairs,
                           merge_form_updates,
                           normalize_control_value, upload_field_is_image,
                           validate_control_value, browser_values_equal)
from http_transport import permitted_transition
from upload_policy import UploadPolicyError


_MODULE_PREFIXES = {
    "config", "system", "template", "user", "member", "admin", "file",
    "media", "attachment", "recycle", "label", "extlabel", "database",
    "content", "contentsort", "slide", "single", "message",
}
_DANGEROUS_ROUTE = re.compile(
    r"(?:^|/)(?:del|delete|remove|logout|loginout|field)(?:/|$)|"
    r"(?:^|/)clearcache(?:/|$)", re.I)
# ``Index/home``/logout/session-clear are shell actions rather than editable
# modules.  ``Index/ucenter`` is different: stock Pboot installations expose
# the account/profile/password form there, and hiding every ``Index/*`` route
# made the desktop silently lose that web function.  Keep the shell routes
# blocked while allowing the explicitly discovered account page to go through
# the same dynamic-form/revision safeguards as other modules.
_NON_MODULE_ROUTE = re.compile(
    r"^(?:home|login|logout|loginout|error)(?:/|$)|"
    r"^index/(?:home|loginout|clearsession)(?:/|$)|^index$", re.I)
_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|权限不足|未授权|被拒绝|登录.*失效|校验.*失败|"
    r"\b(?:error|failed|failure|forbidden|unauthorized|denied|invalid)\b", re.I)


class NativeModuleFallback(RuntimeError):
    """The page cannot be safely reproduced by the static form adapter."""

    def __init__(self, message, native_url="", reason=""):
        super().__init__(message)
        self.native_url = str(native_url or "")
        self.reason = str(reason or message or "")


_DYNAMIC_FORM_CODE = re.compile(
    r"(?:\b(?:addEventListener|attachEvent|preventDefault|stopPropagation|"
    r"requestSubmit|submit|setCustomValidity|formData|fetch|ajax|post|get)\b|"
    r"\.\s*(?:appendChild|insertAdjacent(?:HTML|Element)|replaceChildren|"
    r"remove(?:Child|Attribute)|setAttribute|innerHTML|outerHTML|value)\b|"
    r"\b(?:createElement|layui\.form|layui\.upload|upload\.render|"
    r"UE\.getEditor|baidu\.editor|setOpt)\b)", re.I)
_DYNAMIC_FORM_EVENT_ATTR = re.compile(
    r"^on(?:submit|click|change|input|beforeinput|keydown|keyup|drop|paste)$",
    re.I,
)

_KNOWN_STOCK_SCRIPT = re.compile(
    r"(?:^|/)(?:jquery(?:[-.][\w-]+)*|jquery\.treetable|"
    r"jquery\.dragsort(?:[-.][\w-]+)*|jscolor|"
    r"ueditor\.(?:config|all)(?:\.[\w-]+)*|"
    r"ueditor/lang/zh-cn/zh-cn|"
    r"layui(?:\.all)?|mylayui|comm)\.js(?:[?#].*)?$|"
    r"(?:^|/)res/v\d+/js/update\.js(?:[?#].*)?$",
    re.I,
)


def _known_stock_script(src, reference=None):
    """Recognize only the stock Pboot UI libraries we model explicitly.

    This is intentionally a filename/path allow-list, not a JavaScript
    execution shortcut.  A custom external script still keeps the safe native
    webpage fallback.  The allow-list covers the standard jQuery/Layui/
    UEditor/mylayui bundle whose upload and model-change semantics are parsed
    separately by ``upload_policy`` and the dedicated category adapter.
    """
    value = str(src or "").strip()
    if reference:
        try:
            resolved = urljoin(str(reference), value)
            parsed = urlparse(resolved)
            # A filename that happens to match a stock bundle is not enough
            # when it came from another origin.  The browser would execute
            # that remote script, so static replay must fail closed unless it
            # is actually same-origin with the fresh form page.
            if parsed.scheme and parsed.netloc and not _same_origin(resolved, reference):
                # Stock Pboot installations may load the vendor's update
                # notifier from its canonical HTTPS host.  It does not own
                # form/upload state; keep this one explicitly identified
                # inert bundle compatible, while every other cross-origin
                # script remains a native-page fallback.
                official_update = (
                    (parsed.hostname or '').lower() in
                    {'pbootcms.com', 'www.pbootcms.com'} and
                    bool(re.search(r'/(?:res/)?v\d+/js/update\.js(?:$|[?#])',
                                   parsed.path + (('?' + parsed.query) if parsed.query else ''))))
                if not official_update:
                    return False
        except (TypeError, ValueError):
            return False
    return bool(_KNOWN_STOCK_SCRIPT.search(value))


def _safe_stock_editor_script(code):
    """Return True for the narrow stock UEditor synchronization snippets.

    Stock Pboot pages commonly register the editor, switch source mode back to
    visual mode on submit, and insert an image from the add/edit picker.  None
    of those snippets changes form fields or sends a request; the desktop
    already receives the resulting ``content`` value from the form snapshot.
    Any network call, validation override, action mutation, DOM replacement or
    unrecognized editor command remains dynamic and therefore fails closed.
    """
    text = str(code or "")
    if not re.search(r"(?:UE\.getEditor|queryCommandState|execCommand)",
                     text, re.I):
        return False
    if re.search(
            r"(?:fetch|ajax|\$\s*\.\s*(?:post|get)|requestSubmit|preventDefault|"
            r"setCustomValidity|FormData|(?:^|[.\s])(?:action|method|enctype)\s*=|"
            r"appendChild|insertAdjacent|replaceChildren|removeChild|"
            r"setAttribute|outerHTML|location\s*=)", text, re.I):
        return False
    for command in re.findall(r"execCommand\s*\(\s*['\"]([^'\"]+)",
                              text, re.I):
        if command.lower() not in {"source", "inserthtml"}:
            return False
    for match in re.finditer(r"UE\.getEditor\s*\(\s*([^,\)]+)", text, re.I):
        argument = match.group(1).strip()
        if not ((len(argument) >= 2 and argument[0] in "'\"" and
                 argument[-1] == argument[0]) or argument == "'editor'"):
            return False
    return True


def _safe_stock_page_script(code):
    """Recognize stock list/gallery helpers handled by dedicated adapters."""
    text = str(code or "")
    if re.search(r"(?:fetch|ajax|\$\s*\.\s*(?:post|get)|requestSubmit|"
                 r"preventDefault|FormData|setCustomValidity)", text, re.I):
        return False
    if re.search(r"setDelAction", text, re.I):
        return bool(re.search(
            r"document\.contentForm\.action\s*=\s*['\"][^'\"]*/Content/del"
            r"['\"]", text, re.I) and re.search(r"return\s+confirm\s*\(",
                                                  text, re.I))
    if re.search(r"dragsort|#pics_box|function\s+saveOrder", text, re.I):
        return bool(re.search(r"input\s*\[\s*name\s*=\s*(?:['\"])?pics(?:['\"])?\s*\]",
                              text, re.I) and
                    re.search(r"data\s*\.join\s*\(", text, re.I))
    return False


def _dynamic_form_reason(soup, form, reference=None):
    """Detect scripts which can change this form's browser semantics.

    A static successful-control snapshot cannot reproduce JavaScript that
    adds/removes controls, changes the target, intercepts validation, or owns
    an UEditor/Layui upload callback.  Those pages must continue in the real
    authenticated browser instead of receiving a guessed request.
    """
    if not form:
        return ""
    form_id = str(form.get("id", "") or "").strip().lower()
    form_name = str(form.get("name", "") or "").strip().lower()
    action = str(form.get("action", "") or "").strip().lower()
    action_path = urlparse(action).path.rsplit("/", 1)[-1] if action else ""
    control_names = {
        str(element.get("name", "") or "").strip().lower()
        for element in form_elements(form)
        if str(element.get("name", "") or "").strip()
    }
    identifiers = {item for item in (form_id, form_name, action_path) if len(item) >= 3}
    identifiers.update(item for item in control_names if len(item) >= 3)
    form_classes = " ".join(form.get("class") or []).lower()
    editor_marker = bool(re.search(r"(?:ueditor|edui|layui-form|layui-upload|editor)",
                                   form_classes))
    widget_marker = editor_marker or any(
        re.search(r"(?:layui|ueditor|edui|upload|editor)",
                  " ".join(element.get("class") or []).lower())
        or any(str(key).lower() in ("data-des", "data-url", "lay-filter",
                                    "lay-submit", "lay-verify")
               for key in element.attrs)
        for element in form_elements(form))
    external_scripts = [str(script.get("src", "") or "").strip()
                        for script in soup.find_all("script")
                        if str(script.get("src", "") or "").strip()]
    unknown_external = [src for src in external_scripts
                        if not _known_stock_script(src, reference)]
    # Stock Pboot bundles are covered by the non-executing upload/category
    # adapters below.  Keep the old fail-closed behavior for any custom or
    # cross-site script instead of assuming a filename we do not recognize.
    if widget_marker and unknown_external:
        return "表单依赖外部组件脚本，无法在静态请求中复现"

    for element in [form] + list(form_elements(form)):
        for attr in element.attrs:
            if _DYNAMIC_FORM_EVENT_ATTR.match(str(attr)):
                return "表单包含浏览器事件处理脚本"

    for script in soup.find_all("script"):
        code = script.get_text(" ", strip=False) or ""
        if not code.strip():
            continue
        low = code.lower()
        # A service worker or Workbox handler can rewrite fetch/XHR requests
        # without mentioning this form at all.  A requests-based replay has
        # no equivalent browser worker scope, so fail closed and keep the
        # authenticated native page as the only byte/response-faithful path.
        if re.search(r"(?:navigator\s*\.\s*serviceworker|"
                     r"serviceworker\s*\.\s*register|workbox|"
                     r"clients\s*\.\s*(?:claim|matchall))", low, re.I):
            return "页面由 Service Worker/Workbox 接管网络，无法在静态请求中复现"
        if _safe_stock_page_script(code):
            continue
        if _safe_stock_editor_script(code):
            continue
        if editor_marker and re.search(
                r"(?:ueditor|edui|layui\.form|layui\.upload|upload\.render|"
                r"form\.on|setopt|ue\.geteditor|execcommand|"
                r"querycommandstate)", low, re.I):
            if _safe_stock_editor_script(code):
                continue
            return "表单由 UEditor/Layui 或动态编辑器脚本接管"
        if not _DYNAMIC_FORM_CODE.search(code):
            continue
        references_form = bool(identifiers and any(token in low for token in identifiers))
        references_form = references_form or bool(re.search(
            r"(?:querySelector(?:All)?\s*\(\s*['\"]form|"
            r"getElementById\s*\(|getElementsByName\s*\(|"
            r"closest\s*\(\s*['\"]form|form\s*\.)", low, re.I))
        if references_form:
            return "页面脚本可能在提交前改变字段、校验或上传回调"
    return ""


def _same_origin(left, right):
    try:
        # ``right`` is the page/request origin and ``left`` is the discovered
        # target.  This preserves a safe default-port HTTP→HTTPS upgrade,
        # while still rejecting HTTPS→HTTP, cross-host and non-default-port
        # transitions before credentials or form data are used.
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _route(url):
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query, keep_blank_values=True)
    value = (query.get("p") or [""])[0] or parsed.path
    return unquote(str(value or "")).strip().lstrip("/")


def _module_key(route):
    parts = [part for part in str(route or "").split("/") if part]
    return (parts[0].lower() if parts else "")


def _label(form, element, name):
    element_id = str(element.get("id", "") or "")
    if element_id:
        label = form.find("label", {"for": element_id})
        if label and label.get_text(" ", strip=True):
            return label.get_text(" ", strip=True)[:120]
    parent = element.find_parent(class_=re.compile(r"layui-form-item|form-item|form-group", re.I))
    if parent:
        label = parent.find(class_=re.compile(r"layui-form-label", re.I)) or parent.find("label")
        if label and label.get_text(" ", strip=True):
            return label.get_text(" ", strip=True)[:120]
    return str(name or "")


def _public_form(fields):
    allowed = {"name", "label", "type", "kind", "value", "required", "readonly",
               "dom_readonly", "disabled", "multiple", "max_files", "mappable", "widget", "help",
               "options", "min", "max", "step", "maxlength", "minlength", "pattern",
               "accept", "placeholder", "lay-verify", "hidden_fallback", "upload_target",
               "dirname", "dirname_direction", "dirname_auto", "autocomplete",
               "inputmode", "list", "size", "form_novalidate"}
    return [{key: value for key, value in field.items() if key in allowed}
            for field in fields or [] if not field.get("_upload_internal")]


class AdminModuleMixin:
    def _module_home(self):
        url = self._url("Index/home")
        response = self._request("GET", url, timeout=45, read_only=True)
        if not getattr(response, "ok", False):
            raise RuntimeError(f"后台模块首页读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        reference = getattr(response, "url", "") or url
        if not _same_origin(reference, self.admin_url):
            raise RuntimeError("后台模块首页发生跨站跳转，已停止")
        return response

    def _module_snapshot(self):
        response = self._module_home()
        soup = BeautifulSoup(response.text, "html.parser")
        modules, seen = [], set()
        reference = getattr(response, "url", "") or self.admin_url
        for anchor in soup.find_all("a", href=True):
            href = urljoin(reference, str(anchor.get("href", "") or ""))
            route = _route(href)
            prefix = _module_key(route)
            # Custom CMS extensions do not share a predictable first route
            # segment.  Same-origin menu discovery is safe here because the
            # form parser below still requires a real save form, rejects
            # destructive routes, and verifies the page again before writing.
            # Keep only shell/login links out of the generic list.
            if (not _same_origin(href, self.admin_url) or
                    _NON_MODULE_ROUTE.search(route) or
                    _DANGEROUS_ROUTE.search(route)):
                continue
            if not route or _DANGEROUS_ROUTE.search(route):
                continue
            # A menu item may repeat in desktop/mobile navigation; expose it once.
            key = route.lower()
            if key in seen:
                continue
            seen.add(key)
            label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or route
            modules.append({"key": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
                            "label": label[:120], "route": route[:240], "url": href})
        return {"modules": modules, "page_url": reference}

    def list_admin_modules(self):
        snapshot = self._module_snapshot()
        source = "|".join(f"{item['route']}:{item['label']}" for item in snapshot["modules"])
        return {"modules": snapshot["modules"],
                "revision": hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]}

    def inspect_admin_module(self, module_url, *, max_rows=200, max_text=20000):
        """Read a discovered module even when it has no editable form.

        Recycle-bin, material, model and user pages are often read-only for a
        given account.  The old generic adapter treated those pages as
        unsupported because it only exposed save forms.  This method keeps
        the route/menu safety checks, performs a read-only GET, and returns a
        bounded text/table snapshot for the UI.  It deliberately strips
        scripts, styles and executable action links; write operations remain
        in dedicated route-aware managers.
        """
        module = self._discover_module(module_url)
        response = self._request("GET", module["url"], timeout=45, read_only=True)
        if not getattr(response, "ok", False):
            raise RuntimeError(f"后台模块读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        reference = getattr(response, "url", "") or module["url"]
        if not _same_origin(reference, self.admin_url):
            raise RuntimeError("后台模块读取发生跨站跳转，已停止")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else module["label"]
        for node in soup.find_all(["script", "style", "noscript"]):
            node.decompose()
        tables = []
        try:
            row_limit = max(1, min(int(max_rows), 500))
        except (TypeError, ValueError):
            row_limit = 200
        for table in soup.find_all("table")[:30]:
            headers = [cell.get_text(" ", strip=True)[:160]
                       for cell in table.find_all("th")]
            rows = []
            for row in table.find_all("tr")[:row_limit]:
                cells = row.find_all(["th", "td"])
                if not cells:
                    continue
                rows.append([cell.get_text(" ", strip=True)[:300] for cell in cells])
            if rows:
                tables.append({"headers": headers, "rows": rows})
        links = []
        seen = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(reference, str(anchor.get("href", "") or ""))
            route = _route(href)
            if (not _same_origin(href, self.admin_url) or
                    _DANGEROUS_ROUTE.search(route)):
                continue
            key = href.lower()
            if key in seen:
                continue
            seen.add(key)
            label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or route
            # Keep the server-resolved same-origin URL so the UI can offer a
            # real browser continuation for pagination/detail pages.  The
            # route and link have already passed the same-origin and
            # destructive-route filters above; the native bridge performs
            # the origin check again at click time.
            links.append({"label": label[:160], "route": route[:240],
                          "url": href})
            if len(links) >= 200:
                break
        text = soup.get_text(" ", strip=True)
        try:
            text_limit = max(1000, min(int(max_text), 100000))
        except (TypeError, ValueError):
            text_limit = 20000
        text = re.sub(r"\s+", " ", text)[:text_limit]
        return {"key": module["key"], "route": module["route"],
                "label": module["label"], "page_url": reference,
                "title": title[:240], "text": text,
                "tables": tables, "links": links,
                "has_form": bool(soup.find("form"))}

    def _discover_module(self, module_url):
        candidate = urljoin(self.admin_url, str(module_url or "").strip())
        if not _same_origin(candidate, self.admin_url):
            raise ValueError("后台模块地址必须与当前站点同源")
        route = _route(candidate)
        if (not route or _NON_MODULE_ROUTE.search(route) or
                _DANGEROUS_ROUTE.search(route)):
            raise ValueError("后台模块地址未通过安全路由校验")
        snapshot = self._module_snapshot()
        exact = next((item for item in snapshot["modules"]
                      if item["url"] == candidate or item["route"].lower() == route.lower()), None)
        if not exact:
            raise RuntimeError("该后台模块未在当前菜单中发现，请刷新后重试")
        return exact

    def _parse_module_form(self, response, module):
        if not getattr(response, "ok", False):
            raise RuntimeError(f"后台模块表单读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        reference = getattr(response, "url", "") or module["url"]
        if not _same_origin(reference, self.admin_url):
            raise RuntimeError("后台模块表单发生跨站跳转，已停止")
        soup = BeautifulSoup(response.text, "html.parser")
        forms = soup.find_all("form")
        if not forms:
            raise RuntimeError("当前后台模块没有可编辑表单")
        # Reuse the same submitter choice as the article workflow.  Generic
        # modules frequently expose preview/cancel beside the real save
        # button, and the clicked submitter may override action/method/
        # enctype even when the form itself points elsewhere.
        from client_content import (_discover_default_submitter,
                                    _discover_submitter_options,
                                    _form_method_enctype)
        candidates = []
        for index, candidate in enumerate(forms):
            candidate_submitter = _discover_default_submitter(candidate)
            candidate_transport = _form_method_enctype(candidate, candidate_submitter)
            candidate_action = urljoin(
                reference, str(candidate_transport.get("action") or
                               candidate.get("action", "") or ""))
            candidate_method = str(candidate_transport.get("method", "post") or "post").lower()
            if (candidate_method not in ("post", "get") or
                    not _same_origin(candidate_action, self.admin_url) or
                    _DANGEROUS_ROUTE.search(_route(candidate_action))):
                continue
            text = candidate.get_text(" ", strip=True)
            # GET is a valid HTML submission method, but it is also the
            # dominant search/filter method in CMS pages.  Only expose it as
            # a write candidate when the actual submitter clearly says save;
            # the UI adds an explicit confirmation before sending it.
            if candidate_method == "get":
                submitter_text = " ".join(str(candidate_submitter.get(key, "") or "")
                                           for key in ("name", "value", "formaction")) \
                    if candidate_submitter else ""
                if not re.search(r"保存|提交|新增|添加|修改|更新|确定|save|submit|add|update|confirm",
                                 submitter_text, re.I):
                    continue
            score = 0
            if candidate_submitter:
                button_text = " ".join(str(candidate_submitter.get(key, "") or "")
                                       for key in ("name", "value", "formaction"))
                if re.search(r"保存|提交|新增|添加|修改|更新|确定|save|submit|add|update|confirm",
                             button_text, re.I):
                    score += 20
                if re.search(r"预览|取消|返回|删除|清空|preview|cancel|back|delete|remove|reset",
                             button_text, re.I):
                    score -= 30
            if re.search(r"搜索|筛选|查询|search|filter", text, re.I):
                score -= 10
            # A real editable form normally has at least one named field in
            # addition to its anti-CSRF token.  This keeps a bare routing
            # form from winning over a complete save form.
            named_controls = [element for element in form_elements(candidate)
                              if element.get("name") and
                              str(element.get("name")) not in ("formcheck", "csrf")]
            score += min(len(named_controls), 10)
            candidates.append((score, -index, candidate, candidate_submitter,
                               candidate_transport, candidate_action))
        if not candidates:
            raise RuntimeError("当前后台模块没有可安全提交的保存表单")
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise RuntimeError("当前后台模块存在多个无法唯一确认的保存表单")
        (_score, _order, form, submitter, transport, action) = candidates[0]
        dynamic_reason = _dynamic_form_reason(soup, form, reference)
        if dynamic_reason:
            raise NativeModuleFallback(
                dynamic_reason + "，已切换原生网页完成操作",
                native_url=reference,
                reason=dynamic_reason,
            )
        submitter_options = _discover_submitter_options(form)
        fields, defaults = describe_form(
            form, _label, submitter=submitter)
        pairs = successful_pairs(form, submitter=submitter)
        # Generic modules often use the same stock PbootCMS text input plus
        # ``button.upload[data-des]`` pattern as category/Slide forms.  Keep
        # that relationship in the descriptor so a local picker is an upload
        # intent, never a literal Windows path in the final POST.  Native
        # ``input[type=file]`` controls are handled through the same policy.
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
                if target is not None and "uploads" in set(target.get("class", [])):
                    field["multiple"] = True
                if target is not None and target.get("accept") and not field.get("accept"):
                    field["accept"] = str(target.get("accept"))
        revision_source = "|".join([action, module["route"]] +
                                    [f"{key}={defaults[key]}" for key in sorted(defaults)
                                     if not re.search(r"csrf|token|formcheck", key, re.I)])
        revision = hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:24]
        return {"module": module, "action": action, "page_url": reference,
                "fields": fields, "defaults": defaults, "revision": revision,
                "submitter": submitter,
                "submitter_options": submitter_options,
                "method": str(transport.get("method", "post") or "post"),
                "enctype": transport.get("enctype", ""),
                "pairs": pairs}

    def prepare_admin_module(self, module_url):
        module = self._discover_module(module_url)
        response = self._request("GET", module["url"], timeout=45, read_only=True)
        try:
            info = self._parse_module_form(response, module)
        except NativeModuleFallback as exc:
            return {"key": module["key"], "route": module["route"],
                    "label": module["label"], "revision": "", "fields": [],
                    "method": "", "enctype": "", "get_write": False,
                    "submitter": None, "submitter_options": [],
                    "native_url": exc.native_url or module["url"],
                    "native_only": True, "native_reason": exc.reason}
        return {"key": module["key"], "route": module["route"], "label": module["label"],
                "revision": info["revision"], "fields": _public_form(info["fields"]),
                "method": info.get("method", "post"),
                "enctype": info.get("enctype", ""),
                "get_write": str(info.get("method", "post")).lower() == "get",
                "submitter": info.get("submitter"),
                "submitter_options": info.get("submitter_options") or []}

    def update_admin_module(self, module_url, values, expected_revision="", submitter=None):
        module = self._discover_module(module_url)
        response = self._request("GET", module["url"], timeout=45, read_only=True)
        info = self._parse_module_form(response, module)
        if expected_revision and str(expected_revision) != str(info["revision"]):
            raise RuntimeError("后台模块表单已变化，请重新打开后再保存")
        if not isinstance(values, dict):
            raise ValueError("后台模块字段数据无效")
        if submitter is not None:
            # Match the caller's explicit choice against the freshly-read DOM;
            # never trust a stale formaction/method supplied by the UI.
            options = info.get("submitter_options") or []
            chosen = None
            for option in options:
                if all(str(option.get(key, "") or "") ==
                       str(submitter.get(key, "") or "")
                       for key in ("name", "type", "value", "formaction",
                                    "formmethod", "formenctype", "formtarget",
                                    "formnovalidate")):
                    chosen = option
                    break
            if chosen is None:
                raise RuntimeError("后台提交按钮已变化，请重新打开表单")
            info["submitter"] = chosen
        elif info.get("submitter") is None:
            options = info.get("submitter_options") or []
            if len(options) == 1:
                info["submitter"] = options[0]
            elif len(options) > 1:
                raise RuntimeError("当前后台模块有多个提交按钮，请先选择实际保存按钮")
        fields = {field["name"]: field for field in info["fields"] if field.get("name")}
        unknown = set(values) - set(fields)
        if unknown:
            raise ValueError("包含后台未允许的模块字段：" + "、".join(sorted(unknown)))
        # Resolve native file controls and stock text+upload controls before
        # merging the ordinary successful controls.  The browser sends a
        # multipart upload first (through the page-owned endpoint), then the
        # form POST contains the returned server URL.  A local path must never
        # leak into a CMS field value.
        resolved_values = dict(values)
        upload_metadata = []
        upload_fields = {name: field for name, field in fields.items()
                         if field.get("upload_target") or
                         str(field.get("type", field.get("kind", "")) or "").lower() == "file"}
        upload_jobs = []
        for name, field in upload_fields.items():
            if name not in resolved_values:
                continue
            raw = resolved_values.get(name)
            if raw in (None, "", []):
                continue
            items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            local_paths, server_values = [], []
            native_file = str(field.get("type", field.get("kind", "")) or "").lower() == "file"
            for item in items:
                text = str(item or "").strip()
                if not text:
                    continue
                if os.path.isfile(os.path.abspath(text)):
                    local_paths.append(os.path.abspath(text))
                    continue
                # A text+upload control may retain an existing server URL or
                # root-relative path. Native file inputs cannot submit either.
                if not native_file:
                    relative_server = bool(
                        text and not text.startswith((".", "~")) and
                        ":" not in text and "\\" not in text and
                        "?" not in text and "#" not in text and
                        all(part not in ("", ".", "..")
                            for part in text.replace("\\", "/").split("/")))
                    if (text.startswith(("http://", "https://", "/")) or
                            text.startswith(("./", "../")) or relative_server):
                        server_values.append(item)
                        continue
                raise ValueError(f"字段 {name} 选择的文件不存在，或不是可保留的服务器地址")
            if local_paths:
                try:
                    queue_limit = int(field.get("max_files") or 0)
                except (TypeError, ValueError):
                    queue_limit = 0
                if queue_limit > 0 and len(local_paths) > queue_limit:
                    raise ValueError(f"{field.get('label') or name}最多选择 {queue_limit} 个文件")
                upload_jobs.append({"key": name, "name": name, "field": field,
                                    "paths": local_paths, "upload_target": name,
                                    "label": field.get("label") or name,
                                    "media_kind": str(field.get("media_kind") or "file")})
            if native_file and not server_values:
                raise ValueError(f"字段 {name} 未选择有效文件")
            # Keep values until the shared upload queue below has filled in
            # any local-file results.  Existing server values are already in
            # the browser control and must remain in order.
            resolved_values[name] = (server_values if field.get("multiple")
                                     else (server_values[-1] if server_values else ""))

        if upload_jobs:
            # Discover every page-owned policy in one pass.  ``prepare_uploads``
            # replaces the client's policy map; calling it once per control
            # silently discarded all but the last field and made a form with
            # two independent upload controls diverge from the browser queue.
            targets = list(dict.fromkeys(
                ("field", job["name"]) for job in upload_jobs))
            try:
                self.prepare_uploads(info["page_url"], targets)
            except UploadPolicyError as exc:
                names = "、".join(str(job["name"]) for job in upload_jobs)
                reason = (f"字段 {names} 的网页上传策略无法安全复刻：{exc}")
                raise NativeModuleFallback(
                    reason + "，已切换原生网页完成上传和保存",
                    native_url=info.get("page_url") or module.get("url", ""),
                    reason=reason) from exc
            apply_policy = getattr(self, "apply_upload_policy_metadata", None)
            if callable(apply_policy):
                apply_policy(info.get("fields", []))
            from webtasks import upload_field_queues
            from exceptions import UploadOutcomeUnknown
            from upload_policy import NativeUploadRequired
            try:
                uploaded = upload_field_queues(
                    self, upload_jobs,
                    formcheck=str(info["defaults"].get("formcheck", "") or ""))
            except NativeUploadRequired as exc:
                reason = str(exc.reason or exc)
                raise NativeModuleFallback(
                    reason + "，已切换原生网页完成上传和保存",
                    native_url=exc.native_url or info.get("page_url") or module.get("url", ""),
                    reason=reason) from exc
            except UploadOutcomeUnknown as exc:
                raise NativeModuleFallback(
                    "后台模块文件上传结果未知，文件可能已保存；已切换原生网页核对",
                    native_url=info.get("page_url") or module.get("url", ""),
                    reason=str(exc)) from exc
            except Exception as exc:
                raise RuntimeError(f"后台模块文件上传失败：{exc}") from exc
            for name, result in uploaded.items():
                urls = list(result.get("urls") or [])
                upload_metadata.extend(result.get("metadata") or [])
                field = upload_fields[name]
                old = resolved_values.get(name)
                old_values = old if isinstance(old, list) else ([old] if old else [])
                merged = old_values + urls if field.get("multiple") else urls[-1]
                resolved_values[name] = merged

        updates = {}
        for name, value in resolved_values.items():
            field = fields[name]
            if field.get("readonly") or field.get("disabled"):
                continue
            normalized = normalize_control_value(value, field)
            issue = validate_control_value(normalized, field)
            if issue:
                raise ValueError(f"{field.get('label') or name}{issue}")
            updates[name] = normalized
        data = merge_form_updates(info["defaults"], info["fields"], updates)
        if info.get("submitter"):
            from client_content import _submitter_values
            data.update(_submitter_values(info["submitter"]))
        # Route every discovered form through the shared browser transport,
        # not only multipart forms.  Dynamic admin pages may use GET or
        # text/plain and may override action/method/enctype on the clicked
        # submitter; bypassing the adapter silently changed those semantics.
        from client_content import (_submit_content_form, BrowserFormData,
                                    mark_write_attempt, mark_write_http_result,
                                    mark_write_rejected)
        transport_data = BrowserFormData.from_data(info.get("pairs", []), data)
        mark_write_attempt(self)
        post = _submit_content_form(
            self, info.get("method", "post"), info["action"], transport_data,
            info["fields"], info.get("enctype", ""),
            {"Referer": info.get("page_url", "")}, timeout=60)
        text = BeautifulSoup(getattr(post, "text", ""), "html.parser").get_text(" ", strip=True)
        if not getattr(post, "ok", False):
            mark_write_http_result(self, post)
            raise RuntimeError(f"后台模块保存失败（HTTP {getattr(post, 'status_code', '?')}）")
        if _is_login_page(getattr(post, "text", "")) or (_ERROR_RE.search(text) and not re.search(r"成功|success", text, re.I)):
            message = text[:300] or "后台未确认模块保存"
            mark_write_rejected(self, post, message)
            raise RuntimeError(message)
        fresh = self._parse_module_form(
            self._request("GET", module["url"], timeout=45, read_only=True), module)
        mismatches = []
        for name, value in updates.items():
            if name not in fresh["defaults"]:
                mismatches.append(name)
                continue
            expected = value
            if not browser_values_equal(fresh["defaults"].get(name, ""), expected):
                mismatches.append(name)
        if mismatches:
            raise RuntimeError("后台模块已提交但回读字段不一致：" + "、".join(mismatches))
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "后台模块保存成功", "key": module["key"],
                "route": module["route"], "revision": fresh["revision"],
                "fields": _public_form(fresh["fields"]),
                "method": fresh.get("method", "post"),
                "enctype": fresh.get("enctype", ""),
                "get_write": str(fresh.get("method", "post")).lower() == "get",
                "submitter": fresh.get("submitter"),
                "submitter_options": fresh.get("submitter_options") or [],
                "upload_metadata": upload_metadata}
