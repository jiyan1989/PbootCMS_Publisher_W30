"""Safe PbootCMS category-management client.

The content publishing workflow only needs a compact category tree.  This
module deliberately keeps the more dangerous admin operations separate: it
discovers links and forms from the active site's own ``ContentSort`` pages
instead of assuming one PbootCMS version's URL layout.
"""
import hashlib
import os
import re
from types import SimpleNamespace
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from client_utils import _is_login_page
from logger import debug_log
from form_controls import (describe_form, successful_pairs, merge_form_updates,
                           normalize_control_value, upload_field_is_image,
                           validate_control_value)
from http_transport import permitted_transition


_WRITE_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|权限不足|未授权|被拒绝|请先登录|登录.*失效|"
    r"校验.*失败|\b(?:error|failed|failure|invalid|forbidden|unauthorized|denied)\b",
    re.I,
)


# Template names used by the active site's existing category list.  These are
# suggestions for a *new* category only; the UI still accepts a preset only
# when that exact template is offered by the site's own backend form.
_CATEGORY_TEMPLATE_PRESETS = (
    (re.compile(r"(?:^|[\s_-])(product)(?:$|[\s_-])|产品", re.I),
     "pro.html", "proshow.html"),
    (re.compile(r"(?:^|[\s_-])(case)(?:$|[\s_-])|案例", re.I),
     "case.html", "newsshow.html"),
    (re.compile(r"(?:^|[\s_-])(video)(?:$|[\s_-])|视频", re.I),
     "news.html", "vdshow.html"),
    (re.compile(r"(?:^|[\s_-])(news)(?:$|[\s_-])|新闻", re.I),
     "news.html", "newsshow.html"),
    (re.compile(r"(?:^|[\s_-])(about)(?:$|[\s_-])|关于我们", re.I),
     "", "about.html"),
    (re.compile(r"(?:^|[\s_-])(contact)(?:$|[\s_-])|联系我们", re.I),
     "", "contact.html"),
)


def _same_origin(left, right):
    try:
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _route_value(value, key):
    match = re.search(
        rf"(?:^|[/&?]){re.escape(key)}(?:/|=)([^/&?#]+)",
        str(value or ""), re.I)
    return match.group(1).strip() if match else ""


def _category_route(value, operation):
    """True only for a same-controller operation, never a status toggle."""
    raw = str(value or "")
    low = raw.lower()
    if not re.search(rf"(?:^|[/=])contentsort/{operation}(?:[/&?#]|$)", low):
        return False
    if operation == "mod" and (
            re.search(r"(?:^|[/&?])field(?:/|=)", low) or
            re.search(r"(?:^|[/&?])value(?:/|=)", low)):
        return False
    return True


def _node_name(row):
    """Extract the name cell without accidentally using an operation link."""
    cells = row.find_all("td")
    if len(cells) > 1:
        text = cells[1].get_text(" ", strip=True)
        if text:
            return text[:200]
    for anchor in row.find_all("a", href=True):
        href = anchor.get("href", "")
        if "contentsort/" not in str(href).lower():
            text = anchor.get_text(" ", strip=True)
            if text:
                return text[:200]
    return ""


class CategoryAdminMixin:
    """Discover and submit category forms for the current PbootCMS session."""

    def _category_absolute_url(self, href, reference):
        candidate = urljoin(reference or self.admin_url, str(href or "").strip())
        return candidate if _same_origin(candidate, self.base_url or self.admin_url) else ""

    def _read_category_index(self):
        """Read one real category listing and preserve only safe row actions."""
        url = self._url("ContentSort/index")
        response = self._request("GET", url, timeout=45, read_only=True)
        if not getattr(response, "ok", False):
            raise RuntimeError(f"读取栏目列表失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        soup = BeautifulSoup(response.text, "html.parser")
        reference = getattr(response, "url", "") or url
        nodes, all_nodes = [], {}
        add_url = ""
        add_form_html = ""

        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "") or "")
            if _category_route(href, "add"):
                candidate = self._category_absolute_url(href, reference)
                if candidate:
                    add_url = candidate
                    break

        # Some PbootCMS themes keep one or more add dialogs directly on the
        # listing page and therefore expose no add link at all.  Prefer the
        # single-category form (``name``); the other common inline form uses
        # ``multiplename`` for batch creation.
        inline_add_forms = [form for form in soup.find_all("form")
                            if _category_route(form.get("action", ""), "add")]
        if inline_add_forms:
            selected = next((form for form in inline_add_forms
                             if form.find(attrs={"name": "name"})), inline_add_forms[0])
            action = self._category_absolute_url(selected.get("action", ""), reference)
            if action:
                add_form_html = str(selected)

        for row in soup.find_all("tr"):
            checkbox = row.find("input", {"name": "list[]"})
            if not checkbox:
                continue
            scode = str(checkbox.get("value", "") or "").strip()
            if not scode.isdigit():
                continue
            node_id = str(row.get("data-tt-id", "") or "").strip() or scode
            parent_id = str(row.get("data-tt-parent-id", "") or "").strip()
            if not parent_id or parent_id == "0":
                parent_id = ""
            node = {
                "id": scode,
                "scode": scode,
                "name": _node_name(row) or f"栏目 {scode}",
                "parent_tt_id": parent_id,
                "children": [],
                "mcode": "",
                "edit_url": "",
                "delete_url": "",
            }
            for anchor in row.find_all("a", href=True):
                href = str(anchor.get("href", "") or "")
                candidate = self._category_absolute_url(href, reference)
                if not candidate:
                    continue
                route_scode = _route_value(href, "scode") or _route_value(href, "id")
                if route_scode and route_scode != scode:
                    continue
                if _category_route(href, "mod") and not node["edit_url"]:
                    node["edit_url"] = candidate
                elif _category_route(href, "del") and not node["delete_url"]:
                    node["delete_url"] = candidate
                mcode = _route_value(href, "mcode")
                if mcode.isdigit() and not node["mcode"]:
                    node["mcode"] = mcode
            all_nodes[node_id] = node

        for node_id, node in all_nodes.items():
            parent_id = node["parent_tt_id"]
            if parent_id and parent_id in all_nodes:
                all_nodes[parent_id]["children"].append(node)
            else:
                nodes.append(node)
        return {"tree": nodes, "nodes": all_nodes, "add_url": add_url,
                "add_form_html": add_form_html,
                "reference": reference}

    @staticmethod
    def _public_tree(nodes):
        result = []
        for item in nodes or []:
            result.append({
                "id": str(item.get("id", "")),
                "scode": str(item.get("scode", item.get("id", ""))),
                "name": str(item.get("name", "")),
                "mcode": str(item.get("mcode", "")),
                "children": CategoryAdminMixin._public_tree(item.get("children", [])),
            })
        return result

    def get_category_admin_tree(self):
        snapshot = self._read_category_index()
        return self._public_tree(snapshot["tree"])

    @staticmethod
    def _form_label(element):
        element_id = str(element.get("id", "") or "")
        if element_id:
            label = element.find_parent().find("label", {"for": element_id}) \
                if element.find_parent() else None
            if label:
                text = label.get_text(" ", strip=True)
                if text:
                    return text[:100]
        container = element.find_parent(class_=re.compile(r"form-item|form-group", re.I))
        if container:
            label = container.find("label")
            if label:
                text = label.get_text(" ", strip=True)
                if text:
                    return text[:100]
        return str(element.get("placeholder", "") or element.get("name", "") or "字段")[:100]

    @staticmethod
    def _choice_label(element):
        """Return a radio/checkbox option caption, not its group title."""
        title = str(element.get("title", "") or "").strip()
        if title:
            return title[:100]
        return CategoryAdminMixin._form_label(element)

    @staticmethod
    def _control_value(element):
        if element.name == "textarea":
            return element.get_text() or ""
        if element.name == "select":
            selected = element.find("option", selected=True)
            if selected is None:
                selected = element.find("option")
            return str(selected.get("value", "") if selected else "")
        if str(element.get("type", "") or "").lower() in ("checkbox", "radio"):
            return str(element.get("value", "1") or "1") if element.has_attr("checked") else ""
        return str(element.get("value", "") or "")

    @staticmethod
    def _category_template_presets(fields):
        """Describe safe create-form template defaults for the WebUI.

        The labels and option values are always discovered from the active
        PbootCMS form.  A preset never invents a model option; it only tells
        the client which of the site's existing template options to select.
        """
        model_field = next((field for field in fields
                            if field.get("kind") == "select" and
                            str(field.get("name", "")).lower() in
                            ("mcode", "model", "model_id", "modelid")), None)
        list_field = next((field for field in fields
                           if str(field.get("name", "")).lower() in
                           ("listtpl", "list_tpl", "listtemplate")), None)
        detail_field = next((field for field in fields
                             if str(field.get("name", "")).lower() in
                             ("contenttpl", "content_tpl", "detailtpl",
                              "detail_tpl", "showtpl", "show_tpl")), None)
        if not model_field or not list_field or not detail_field:
            return []

        presets = []
        for option in model_field.get("options", []):
            label = str(option.get("label", "") or "")
            value = str(option.get("value", "") or "")
            backend_type = str(option.get("data_type", "") or "").strip()
            backend_list = str(option.get("data_listtpl", "") or "").strip()
            backend_detail = str(option.get("data_contenttpl", "") or "").strip()
            if backend_type in ("1", "2"):
                presets.append({
                    "model_field": model_field["name"],
                    "model_value": value,
                    "model_label": label,
                    "category_type": backend_type,
                    "list_field": list_field["name"],
                    "list_value": backend_list,
                    "detail_field": detail_field["name"],
                    "detail_value": backend_detail,
                })
                continue
            for matcher, list_value, detail_value in _CATEGORY_TEMPLATE_PRESETS:
                if matcher.search(label):
                    presets.append({
                        "model_field": model_field["name"],
                        "model_value": value,
                        "model_label": label,
                        "category_type": "1" if not list_value else "2",
                        "list_field": list_field["name"],
                        "list_value": list_value,
                        "detail_field": detail_field["name"],
                        "detail_value": detail_value,
                    })
                    break
        return presets

    def _parse_category_form(self, response, expected_operation, scode="", parent_scode=""):
        if not getattr(response, "ok", False):
            raise RuntimeError(f"读取栏目表单失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        soup = BeautifulSoup(response.text, "html.parser")
        reference = getattr(response, "url", "") or self.admin_url
        form = None
        for candidate in soup.find_all("form"):
            action = str(candidate.get("action", "") or "")
            if _category_route(action, expected_operation):
                form = candidate
                break
        if form is None:
            raise RuntimeError("未发现可安全提交的栏目表单")
        from admin_modules import NativeModuleFallback, _dynamic_form_reason
        dynamic_reason = _dynamic_form_reason(soup, form, reference)
        if dynamic_reason:
            raise NativeModuleFallback(dynamic_reason + "，已切换原生网页完成栏目操作",
                                       native_url=reference, reason=dynamic_reason)
        from client_content import (_discover_default_submitter,
                                    _form_method_enctype,
                                    _get_write_submitter_allowed)
        submitter = _discover_default_submitter(form)
        transport = _form_method_enctype(form, submitter)
        action = self._category_absolute_url(transport.get("action", ""), reference)
        if not action or not _category_route(action, expected_operation):
            raise RuntimeError("栏目表单提交地址不安全")
        method = str(transport.get("method", "post") or "post").lower()
        if method not in ("post", "get"):
            raise RuntimeError("栏目新增/修改表单使用了未适配的提交方法")
        # A GET form is valid HTML, but on CMS pages it is usually a search
        # form.  Preserve it only when the page's actual save submitter is
        # explicit; otherwise use the authenticated native page so no query
        # request is mistaken for a mutation.
        if method == "get" and not _get_write_submitter_allowed(submitter):
            raise NativeModuleFallback(
                "栏目表单使用 GET 但未能确认保存按钮，已切换原生网页完成栏目操作",
                native_url=reference,
                reason="栏目 GET 表单缺少明确保存提交按钮")
        all_fields, defaults = describe_form(
            form, lambda _form, element, _name: self._form_label(element),
            submitter=submitter)
        pairs = successful_pairs(form, submitter=submitter)
        # Keep native file controls in the public form.  They are not
        # successful controls by themselves, but the desktop can now choose
        # a local file and send it through the same discovered upload policy
        # before submitting the category form.
        fields = [dict(field) for field in all_fields
                  if field["type"] != "hidden"]
        names = {field["name"] for field in fields}

        # PbootCMS's stock category form exposes ``ico``/``pic`` as ordinary
        # text inputs plus a ``button.upload[data-des="..."]`` rather than a
        # native input[type=file].  Preserve that relationship so the
        # desktop can offer the same picker while still allowing an existing
        # URL/path to be typed, and so submission can route the local file
        # through the button's discovered upload policy.
        upload_targets = {}
        for button in form.find_all(["button", "a", "input"]):
            target = str(button.get("data-des", "") or "").strip()
            classes = " ".join(button.get("class") or [])
            if not target or "upload" not in classes.lower():
                continue
            if target in names:
                upload_targets[target] = button
        for field in fields:
            target = upload_targets.get(str(field.get("name", "")))
            if target is None:
                continue
            field["upload_target"] = str(field["name"])
            # Layui's stock ``.uploads`` button controls a multi-file queue
            # even when the paired text input has no native ``multiple``
            # attribute.  Preserve that DOM-level contract in the field
            # descriptor; the discovered upload policy is applied again just
            # before submission and remains authoritative for custom themes.
            if "uploads" in set(target.get("class", [])):
                field["multiple"] = True
            accept = str(target.get("accept", "") or "").strip()
            if accept and not field.get("accept"):
                field["accept"] = accept

        if parent_scode and "pcode" in names:
            defaults["pcode"] = str(parent_scode)
            for field in fields:
                if field["name"] == "pcode":
                    field["value"] = str(parent_scode)
        revision_source = "\n".join(
            [action, expected_operation, str(scode)] +
            [f"{key}={defaults[key]}" for key in sorted(defaults)
             if not re.search(r"formcheck|csrf|token", key, re.I)])
        revision = hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:24]
        return {
            "action": action,
            "page_url": reference,
            "defaults": defaults,
            "fields": fields,
            "allowed": names,
            "revision": revision,
            "template_presets": self._category_template_presets(fields),
            "method": method,
            "enctype": str(transport.get("enctype", "") or ""),
            "submitter": submitter,
            "pairs": pairs,
            "warnings": [],
        }

    def _open_category_form(self, operation, scode="", parent_scode="", snapshot=None):
        snapshot = snapshot or self._read_category_index()
        if operation == "add":
            url = snapshot.get("add_url", "")
            inline_form = snapshot.get("add_form_html", "")
            if not url and not inline_form:
                raise RuntimeError("未在栏目列表中发现新增栏目入口")
            if inline_form:
                response = SimpleNamespace(
                    ok=True, status_code=200, text=inline_form,
                    url=snapshot.get("reference", "") or self._url("ContentSort/index"))
            else:
                response = self._request("GET", url, timeout=30, read_only=True)
        else:
            node = next((item for item in snapshot["nodes"].values()
                         if item.get("scode") == str(scode)), None)
            if not node:
                raise RuntimeError("目标栏目已不存在，请刷新后重试")
            url = node.get("edit_url", "")
            if not url:
                raise RuntimeError("未发现该栏目的安全修改入口")
            response = self._request("GET", url, timeout=30, read_only=True)
        info = self._parse_category_form(response, operation, scode, parent_scode)
        info["mode"] = "create" if operation == "add" else "edit"
        info["scode"] = str(scode or "")
        # Return the same fresh hierarchy used to discover and validate this
        # form.  The WebUI uses it to render a searchable, indented parent
        # selector instead of flattening the backend's native <select>.
        info["category_tree"] = self._public_tree(snapshot.get("tree", []))
        return info

    def prepare_category_create(self, parent_scode=""):
        info = self._open_category_form("add", parent_scode=str(parent_scode or ""))
        return self._public_form(info)

    def prepare_category_batch_create(self, parent_scode=""):
        """Expose the site's native ``multiplename`` add form when present.

        A normal add form is intentionally not treated as a batch form: the
        backend's JavaScript uses a separate ``multiplename`` control and a
        different success path.  If a theme has no such control this method
        refuses the operation instead of silently looping single creates.
        """
        info = self._open_category_form("add", parent_scode=str(parent_scode or ""))
        field = next((item for item in info["fields"]
                      if str(item.get("name", "")).lower() in
                      ("multiplename", "multiplename[]")), None)
        if field is None:
            raise RuntimeError("当前后台没有发现 multiplename 批量新增栏目控件")
        field["multiple"] = True
        info["batch_field"] = field["name"]
        public = self._public_form(info)
        public["batch_field"] = field["name"]
        public["batch_capable"] = True
        return public

    def prepare_category_edit(self, scode):
        scode = str(scode or "").strip()
        if not scode.isdigit():
            raise ValueError("栏目编号无效")
        info = self._open_category_form("mod", scode=scode)
        return self._public_form(info)

    @staticmethod
    def _public_form(info):
        return {key: info[key] for key in (
            "mode", "scode", "revision", "fields", "warnings", "category_tree",
            "template_presets", "method", "enctype", "submitter")}

    @staticmethod
    def _all_descendants(node):
        result = set()
        for child in node.get("children", []):
            result.add(str(child.get("scode", child.get("id", ""))))
            result.update(CategoryAdminMixin._all_descendants(child))
        return result

    def _validate_category_values(self, info, values, scode="", snapshot=None,
                                  batch=False):
        if not isinstance(values, dict):
            raise ValueError("栏目表单数据无效")
        result = dict(info["defaults"])
        field_by_name = {field["name"]: field for field in info["fields"]}
        for name in info["allowed"]:
            if (name not in values or field_by_name.get(name, {}).get("readonly") or
                    (scode and name in ("scode", "id"))):
                continue
            value = values.get(name)
            field = field_by_name.get(name, {})
            if (str(field.get("type", field.get("kind", ""))).lower() == "file"
                    or field.get("upload_target")):
                # Local paths are upload intents, not values to POST.  The
                # submit phase validates existence and replaces them with the
                # returned server URL.  A stock category ``ico``/``pic``
                # control is text+button, so an existing URL remains a plain
                # value and only an actual local file becomes an upload.
                if value in (None, "", []):
                    continue
                paths = value if isinstance(value, (list, tuple)) else [value]
                for path in paths:
                    candidate = os.path.abspath(str(path or ""))
                    server_ref = (str(path).strip().lower().startswith(
                        ("http://", "https://", "/")))
                    if field.get("upload_target") and server_ref:
                        continue
                    if not os.path.isfile(candidate):
                        raise ValueError(f"{field.get('label') or name}选择的文件不存在")
                result = merge_form_updates(result, info['fields'], {name: value})
                continue
            value = normalize_control_value(value, field_by_name.get(name, {}))
            issue = validate_control_value(value, field_by_name.get(name, {}))
            if issue:
                raise ValueError(f"{field_by_name.get(name, {}).get('label') or name}{issue}")
            result = merge_form_updates(result, info['fields'], {name: value})
        for field in info["fields"]:
            raw_value = result.get(field["name"], "")
            if isinstance(raw_value, (list, tuple)):
                # A required multiple control is empty when every successful
                # value is empty.  ``str([])`` is non-empty and used to let a
                # blank multi-select/duplicate field pass category validation.
                has_value = any(str(item if item is not None else "").strip()
                                 for item in raw_value)
                value = (str(raw_value[0]).strip() if len(raw_value) == 1 else
                         ("" if not raw_value else ",".join(
                             str(item if item is not None else "").strip()
                             for item in raw_value)))
            else:
                value = str(raw_value if raw_value is not None else "").strip()
                has_value = bool(value)
            if field.get("required") and not has_value and not (batch and field.get("name") == "name"):
                raise ValueError(f"{field.get('label') or field['name']}不能为空")
            if field.get("name") in ("pcode", "mcode") and field.get("kind") == "select":
                valid = {str(item.get("value", "") or "") for item in field.get("options", [])}
                if value not in valid:
                    raise ValueError(f"{field.get('label') or field['name']}不是后台允许的选项")
        if "name" in result and not str(result["name"]).strip() and not batch:
            raise ValueError("栏目名称不能为空")
        if "filename" in result:
            filename = str(result["filename"] or "").strip()
            if ("\\" in filename or ".." in filename or "://" in filename or
                    "?" in filename or "#" in filename):
                raise ValueError("URL 名称只能填写相对路径名称")
        if scode and "pcode" in result:
            target = str(scode)
            parent = str(result.get("pcode", "") or "")
            if parent == target:
                raise ValueError("父栏目不能选择自身")
            if parent and snapshot:
                node = next((item for item in snapshot["nodes"].values()
                             if item.get("scode") == target), None)
                if node and parent in self._all_descendants(node):
                    raise ValueError("父栏目不能选择当前栏目的子栏目")
        return result

    @staticmethod
    def _response_error(response):
        text = str(getattr(response, "text", "") or "")
        if _is_login_page(text):
            return "登录会话已失效，请重新登录"
        soup = BeautifulSoup(text, "html.parser")
        for item in soup.find_all(class_=re.compile(r"danger|error|layer-content", re.I)):
            message = item.get_text(" ", strip=True)
            if message and _WRITE_ERROR_RE.search(message):
                return message[:300]
        plain = soup.get_text(" ", strip=True)
        if len(plain) <= 500 and _WRITE_ERROR_RE.search(plain):
            return plain[:300]
        return ""

    def _submit_category_form(self, info, values, expected_revision, scode="", snapshot=None,
                              batch=False):
        if expected_revision and expected_revision != info["revision"]:
            raise RuntimeError("栏目表单已被后台更新，请重新打开后再保存")
        data = self._validate_category_values(info, values, scode, snapshot, batch=batch)
        file_fields = {field["name"]: field for field in info.get("fields", [])
                       if (str(field.get("type", field.get("kind", ""))).lower() == "file"
                           or field.get("upload_target"))}
        file_items = []
        server_values = {}
        for name, field in file_fields.items():
            raw = data.pop(name, None)
            if raw in (None, "", []):
                continue
            paths = raw if isinstance(raw, (list, tuple)) else [raw]
            for path in paths:
                text = str(path or "").strip()
                candidate = os.path.abspath(text)
                # Text+upload controls may retain an existing server URL.
                # Only local files are sent to the upload endpoint; URLs stay
                # in the normal form POST unchanged.
                if field.get("upload_target") and not os.path.isfile(candidate):
                    relative_server = bool(
                        text and not text.startswith((".", "~")) and
                        ":" not in text and "\\" not in text and
                        "?" not in text and "#" not in text and
                        all(part not in ("", ".", "..")
                            for part in text.replace("\\", "/").split("/")))
                    if (text.startswith(("http://", "https://", "/")) or
                            text.startswith(("./", "../")) or relative_server):
                        server_values.setdefault(name, []).append(path)
                        continue
                    if str(field.get("type", field.get("kind", ""))).lower() != "file":
                        raise ValueError(f"字段 {name} 的值不是可保留的服务器地址：{text}")
                if (str(field.get("type", field.get("kind", ""))).lower() == "file"
                        and not os.path.isfile(candidate)):
                    raise ValueError(f"字段 {name} 的本地文件不存在")
                if not text:
                    continue
                file_items.append((name, candidate, bool(field.get("multiple"))))
        upload_metadata = []
        if file_items:
            targets = list(dict.fromkeys(("field", name) for name, _path, _multiple in file_items))
            try:
                self.prepare_uploads(info.get("page_url") or info["action"], targets)
            except Exception as exc:
                from upload_policy import UploadPolicyError
                if not isinstance(exc, UploadPolicyError):
                    raise
                from admin_modules import NativeModuleFallback
                reason = f"栏目文件上传策略无法安全复刻：{exc}"
                raise NativeModuleFallback(
                    reason + "，已切换原生网页完成上传和保存",
                    native_url=info.get("page_url") or info.get("action", ""),
                    reason=reason) from exc
            apply_policy = getattr(self, "apply_upload_policy_metadata", None)
            if callable(apply_policy):
                apply_policy(info.get("fields", []))
            for field_name, descriptor in file_fields.items():
                try:
                    queue_limit = int(descriptor.get("max_files") or 0)
                except (TypeError, ValueError):
                    queue_limit = 0
                count = sum(1 for item_name, _path, _multiple in file_items
                            if item_name == field_name)
                if queue_limit > 0 and count > queue_limit:
                    raise ValueError(f"{descriptor.get('label') or field_name}最多选择 {queue_limit} 个文件")
            # All independent fields share one browser-style XHR queue.  The
            # helper performs whole-form preflight before creating its first
            # request and returns each field's URLs in original order.
            from webtasks import upload_field_queues
            from exceptions import UploadOutcomeUnknown
            from upload_policy import NativeUploadRequired
            queues = []
            for name, descriptor in file_fields.items():
                field_paths = [path for item_name, path, _multiple in file_items
                               if item_name == name]
                if field_paths:
                    queues.append({"key": name, "name": name,
                                   "field": descriptor, "paths": field_paths,
                                   "upload_target": name,
                                   "label": descriptor.get("label") or name,
                                   "media_kind": str(descriptor.get("media_kind") or "file")})
            try:
                uploaded = upload_field_queues(
                    self, queues,
                    formcheck=str(info["defaults"].get("formcheck", "") or ""))
            except NativeUploadRequired as exc:
                from admin_modules import NativeModuleFallback
                reason = str(exc.reason or exc)
                raise NativeModuleFallback(
                    reason + "，已切换原生网页完成上传和保存",
                    native_url=exc.native_url or info.get("page_url") or info.get("action", ""),
                    reason=reason) from exc
            except UploadOutcomeUnknown as exc:
                from admin_modules import NativeModuleFallback
                raise NativeModuleFallback(
                    f"栏目文件上传结果未知，文件可能已保存；已切换原生网页核对",
                    native_url=info.get("page_url") or info.get("action", ""),
                    reason=str(exc)) from exc
            except Exception as exc:
                raise RuntimeError(f"栏目文件上传失败：{exc}") from exc
            for name, result in uploaded.items():
                urls = list(result.get("urls") or [])
                upload_metadata.extend(result.get("metadata") or [])
                server_values.setdefault(name, []).extend(urls)
        # Preserve existing server values and merge freshly uploaded URLs in
        # the same control order.  A scalar control receives the last value,
        # exactly as its native text input would.
        for name, values in server_values.items():
            descriptor = file_fields[name]
            data[name] = list(values) if descriptor.get("multiple") else values[-1]
        # PbootCMS keeps category type in a hidden input whose static add-form
        # default is 1 (single page).  Its official JavaScript replaces that
        # value from the selected model's data-type attribute.  Since this
        # client submits the discovered form without executing backend JS, do
        # the same model binding explicitly.  Otherwise a product/list model
        # is submitted as type=1 and ContentSort/add calls addSingle(), which
        # creates the same-name blank content record reported by users.
        model_value = str(data.get("mcode", "") or "")
        preset = next((item for item in info.get("template_presets", [])
                       if str(item.get("model_value", "") or "") == model_value), None)
        category_type = str((preset or {}).get("category_type", "") or "")
        if "type" in info["defaults"] and category_type in ("1", "2"):
            data["type"] = category_type
        # The stock page's model-change JavaScript also updates the list/detail
        # template selects.  A desktop submission must not leave those fields
        # at the static form default when the user picked a different model.
        # For an existing category apply this only when mcode actually changed;
        # otherwise preserve a site's deliberate custom template choice.
        model_field_names = {"mcode", "model", "model_id", "modelid"}
        original_model = next((str(field.get("value", "") or "")
                               for field in info.get("fields", [])
                               if str(field.get("name", "")).lower() in model_field_names), "")
        model_changed = not scode or model_value != original_model
        if preset and model_changed:
            for field_key, value_key in (("list_field", "list_value"),
                                         ("detail_field", "detail_value")):
                field_name = str(preset.get(field_key, "") or "")
                value = str(preset.get(value_key, "") or "")
                field_info = next((field for field in info.get("fields", [])
                                   if str(field.get("name", "")) == field_name), None)
                allowed = {str(option.get("value", "") or "")
                           for option in (field_info or {}).get("options", [])
                           if not option.get("disabled")}
                if field_info and value in allowed:
                    data[field_name] = value
        # Match the discovered form's actual encoding and clicked submitter;
        # multipart category forms must stay multipart even when no file is
        # selected, and a submitter may carry a routing flag.
        from client_content import (_submit_content_form, _submitter_values,
                                    BrowserFormData, mark_write_attempt,
                                    mark_write_http_result, mark_write_rejected)
        data.update(_submitter_values(info.get("submitter")))
        transport_data = BrowserFormData.from_data(info.get("pairs", []), data)
        mark_write_attempt(self)
        response = _submit_content_form(
            self, info.get("method", "post"), info["action"], transport_data,
            info.get("fields", []), info.get("enctype", ""),
            {"Referer": self._url("ContentSort/index")})
        if not getattr(response, "ok", False):
            mark_write_http_result(self, response)
            raise RuntimeError(f"保存栏目失败（HTTP {getattr(response, 'status_code', '?')}）")
        error = self._response_error(response)
        if error:
            mark_write_rejected(self, response, error)
            raise RuntimeError(error)
        self._last_form_upload_metadata = upload_metadata
        return data

    def _result_tree(self):
        snapshot = self._read_category_index()
        return snapshot, self._public_tree(snapshot["tree"])

    def create_category(self, values, expected_revision=""):
        before = self._read_category_index()
        info = self._open_category_form("add", snapshot=before)
        data = self._submit_category_form(info, values, expected_revision, snapshot=before)
        after, tree = self._result_tree()
        old_scodes = {item.get("scode") for item in before["nodes"].values()}
        created = [item for item in after["nodes"].values()
                   if item.get("scode") not in old_scodes]
        name = str(data.get("name", "") or "").strip()
        parent = str(data.get("pcode", "") or "")
        matches = [item for item in created if item.get("name") == name]
        if parent:
            matches = [item for item in matches
                       if item.get("parent_tt_id", "") == parent] or matches
        if len(matches) != 1:
            raise RuntimeError("栏目已提交，但无法从后台列表唯一确认新增结果，请刷新后核对")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "栏目新增成功", "scode": matches[0]["scode"], "tree": tree,
                "upload_metadata": list(getattr(self, "_last_form_upload_metadata", []) or [])}

    def create_categories_batch(self, values, expected_revision=""):
        """Submit the backend's native multiplename batch form once."""
        before = self._read_category_index()
        info = self._open_category_form("add", snapshot=before)
        field = next((item for item in info["fields"]
                      if str(item.get("name", "")).lower() in
                      ("multiplename", "multiplename[]")), None)
        if field is None:
            raise RuntimeError("当前后台没有发现 multiplename 批量新增栏目控件")
        field["multiple"] = True
        raw = (values or {}).get(field["name"], (values or {}).get("multiplename", []))
        names = raw if isinstance(raw, (list, tuple)) else [raw]
        names = [str(name or "") for name in names if str(name or "").strip()]
        if not names:
            raise ValueError("至少填写一个栏目名称")
        # The native field may be named ``multiplename`` while its descriptor
        # is not marked multiple in a custom theme.  Passing a list preserves
        # the browser's repeated successful controls regardless.
        prepared = dict(values or {})
        prepared[field["name"]] = names
        data = self._submit_category_form(info, prepared, expected_revision,
                                          snapshot=before, batch=True)
        after, tree = self._result_tree()
        old_ids = {str(item.get("scode")) for item in before["nodes"].values()}
        created = [item for item in after["nodes"].values()
                   if str(item.get("scode")) not in old_ids]
        created_by_name = {str(item.get("name", "")): item for item in created}
        matched = [created_by_name[name] for name in names if name in created_by_name]
        if len(matched) != len(set(names)):
            raise RuntimeError("栏目批量新增已提交，但无法从后台列表完整确认结果，请刷新后核对")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": f"已新增 {len(matched)} 个栏目", "scodes": [item["scode"] for item in matched],
                "created": matched, "tree": tree,
                "upload_metadata": list(getattr(self, "_last_form_upload_metadata", []) or [])}

    def update_category(self, scode, values, expected_revision=""):
        scode = str(scode or "").strip()
        if not scode.isdigit():
            raise ValueError("栏目编号无效")
        snapshot = self._read_category_index()
        info = self._open_category_form("mod", scode=scode, snapshot=snapshot)
        original_filename = str(info["defaults"].get("filename", "") or "")
        data = self._submit_category_form(info, values, expected_revision, scode, snapshot)
        verify = self._open_category_form("mod", scode=scode)
        for name in info["allowed"]:
            if name in data and name in verify["defaults"] and not self._category_values_equal(
                    data[name], verify["defaults"][name]):
                raise RuntimeError(f"栏目已提交，但字段“{name}”未按预期保存，请刷新后核对")
        _, tree = self._result_tree()
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "栏目修改成功", "scode": scode, "tree": tree,
                "front_url_changed": str(data.get("filename", "") or "") != original_filename,
                "upload_metadata": list(getattr(self, "_last_form_upload_metadata", []) or [])}

    @staticmethod
    def _category_values_equal(left, right):
        """Compare browser form values without collapsing arrays or numeric zero."""
        if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
            left_values = left if isinstance(left, (list, tuple)) else [left]
            right_values = right if isinstance(right, (list, tuple)) else [right]
            return [str(value) if value is not None else "" for value in left_values] == \
                [str(value) if value is not None else "" for value in right_values]
        return (str(left) if left is not None else "") == (str(right) if right is not None else "")

    def delete_category(self, scode, expected_name="", allow_children=False):
        scode = str(scode or "").strip()
        if not scode.isdigit():
            raise ValueError("栏目编号无效")
        snapshot = self._read_category_index()
        node = next((item for item in snapshot["nodes"].values()
                     if item.get("scode") == scode), None)
        if not node:
            raise RuntimeError("目标栏目已不存在，请刷新后重试")
        if expected_name and str(expected_name).strip() != str(node.get("name", "")).strip():
            raise RuntimeError("栏目名称已变化，请刷新后重新确认删除")
        # The web page decides whether a parent with children can be deleted:
        # some installations reject it, while others cascade or re-parent
        # children.  Keep the programmatic default fail-closed, but allow the
        # UI to opt in only after it has shown the explicit destructive warning
        # and the user has confirmed the native delete action.
        if node.get("children") and not bool(allow_children):
            raise RuntimeError("该栏目含有子栏目，不能删除")
        delete_url = node.get("delete_url", "")
        if not delete_url:
            raise RuntimeError("未发现该栏目的安全删除入口")
        from client_content import (mark_write_attempt, mark_write_http_result,
                                    mark_write_rejected)
        mark_write_attempt(self)
        response = self._request("GET", delete_url, timeout=35,
                                 headers={"Referer": self._url("ContentSort/index")})
        if not getattr(response, "ok", False):
            mark_write_http_result(self, response)
            raise RuntimeError(f"删除栏目失败（HTTP {getattr(response, 'status_code', '?')}）")
        error = self._response_error(response)
        if error:
            mark_write_rejected(self, response, error)
            raise RuntimeError(error)
        after, tree = self._result_tree()
        if any(item.get("scode") == scode for item in after["nodes"].values()):
            raise RuntimeError("后台未确认删除栏目，可能仍含内容或权限不足")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "栏目删除成功", "scode": scode, "tree": tree,
                "children_before": [str(item.get("scode", ""))
                                    for item in (node.get("children") or [])]}
