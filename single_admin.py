"""Safe management of PbootCMS single-page records.

Single pages use a separate ``Single/index``/``Single/mod`` surface even
though their fields resemble article forms.  This mixin deliberately does
not reuse article IDs or Content routes: it discovers the active site's own
links/forms and verifies the saved values with a fresh GET.
"""

import hashlib
import os
import re
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from client_utils import _is_login_page
from form_controls import (describe_form, successful_pairs, merge_form_updates,
                           normalize_control_value, upload_field_is_image,
                           validate_control_value, browser_values_equal)
from http_transport import permitted_transition


_ROUTE_RE = re.compile(r"(?:^|/)Single/(index|mod)(?:/|$)", re.I)
_TOGGLE_RE = re.compile(
    r"(?:^|/)Single/mod/id/(\d+)/field/(status)/value/([01])(?:/|$)", re.I)
_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|权限不足|未授权|被拒绝|登录.*失效|校验.*失败|"
    r"\b(?:error|failed|failure|forbidden|unauthorized|denied|invalid)\b", re.I)


def _same_origin(left, right):
    try:
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _route(value):
    parsed = urlparse(str(value or ""))
    query = parsed.query
    match = re.search(r"(?:^|&)p=([^&]*)", query, re.I)
    return str(match.group(1) if match else parsed.path).lstrip("/")


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


class SingleAdminMixin:
    def _single_page_snapshot(self, keyword=""):
        url = self._url("Single/index")
        params = {"keyword": str(keyword)} if str(keyword or "").strip() else None
        response = self._request("GET", url, params=params, timeout=45, read_only=True)
        if not getattr(response, "ok", False):
            raise RuntimeError(f"单页列表读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        reference = getattr(response, "url", "") or url
        if not _same_origin(reference, self.admin_url):
            raise RuntimeError("单页列表发生跨站跳转，已停止")
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            rid = ""
            for element in list(row.find_all("input")) + list(row.find_all("a", href=True)):
                value = str(element.get("value", "") or "").strip()
                match = re.search(r"(?:^|/)id/(\d+)(?:/|$)", _route(element.get("href", "")), re.I)
                rid = value if value.isdigit() else (match.group(1) if match else rid)
                if rid:
                    break
            if not rid.isdigit() and cells:
                text = cells[0].get_text(" ", strip=True)
                rid = text if text.isdigit() else ""
            if not rid.isdigit():
                continue
            values = [cell.get_text(" ", strip=True) for cell in cells]
            edit_url = ""
            toggles = {}
            for anchor in row.find_all("a", href=True):
                href = urljoin(reference, str(anchor.get("href", "") or ""))
                if not _same_origin(href, self.admin_url):
                    continue
                match = _TOGGLE_RE.search(_route(href))
                if match and match.group(1) == rid:
                    toggles[match.group(2).lower()] = {"url": href, "target": int(match.group(3))}
                elif re.search(rf"(?:^|/)Single/mod/(?:mcode/\d+/)?id/{re.escape(rid)}(?:/|$)", _route(href), re.I):
                    edit_url = href
            records.append({"id": rid, "scode": cells[1].get("title", "") if len(cells) > 1 else "",
                            "title": (cells[2].get("title", "") if len(cells) > 2 else "") or
                                     (values[2] if len(values) > 2 else f"单页#{rid}"),
                            "date": values[3] if len(values) > 3 else "",
                            "edit_url": edit_url, "toggles": toggles})
        source = "|".join([reference, *(f"{row['id']}:{row['title']}:{row['date']}" for row in records)])
        revision = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
        return {"records": records, "revision": revision, "page_url": reference}

    def list_single_pages(self, keyword=""):
        snap = self._single_page_snapshot(keyword)
        return {"records": snap["records"], "revision": snap["revision"]}

    def _parse_single_form(self, response, single_id):
        if not getattr(response, "ok", False):
            raise RuntimeError(f"单页表单读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        reference = getattr(response, "url", "") or self.admin_url
        if not _same_origin(reference, self.admin_url):
            raise RuntimeError("单页表单发生跨站跳转，已停止")
        soup = BeautifulSoup(response.text, "html.parser")
        form = None
        for candidate in soup.find_all("form"):
            action = urljoin(reference, str(candidate.get("action", "") or ""))
            if _same_origin(action, self.admin_url) and re.search(
                    rf"(?:^|/)Single/mod/(?:id/)?{re.escape(str(single_id))}(?:/|$)", _route(action), re.I):
                form = candidate
                break
        if form is None:
            raise RuntimeError("未发现安全的 Single/mod 表单")
        from admin_modules import NativeModuleFallback, _dynamic_form_reason
        dynamic_reason = _dynamic_form_reason(soup, form, reference)
        if dynamic_reason:
            raise NativeModuleFallback(dynamic_reason + "，已切换原生网页完成单页操作",
                                       native_url=reference, reason=dynamic_reason)
        from client_content import (_discover_default_submitter,
                                    _form_method_enctype,
                                    _get_write_submitter_allowed)
        submitter = _discover_default_submitter(form)
        transport = _form_method_enctype(form, submitter)
        action = urljoin(reference, str(transport.get("action") or
                                        form.get("action", "") or ""))
        method = str(transport.get("method", "post") or "post").lower()
        if method not in ("post", "get"):
            raise RuntimeError("单页表单使用了未适配的提交方法")
        if method == "get" and not _get_write_submitter_allowed(submitter):
            raise NativeModuleFallback(
                "单页表单使用 GET 但未能确认保存按钮，已切换原生网页完成单页操作",
                native_url=reference,
                reason="单页 GET 表单缺少明确保存提交按钮")
        fields, defaults = describe_form(
            form, _label, submitter=submitter)
        pairs = successful_pairs(form, submitter=submitter)
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
        revision_source = "|".join([action, str(single_id)] +
                                    [f"{key}={defaults[key]}" for key in sorted(defaults)
                                     if not re.search(r"csrf|token|formcheck", key, re.I)])
        revision = hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:24]
        return {"id": str(single_id), "action": action, "page_url": reference,
                "fields": fields, "defaults": defaults, "revision": revision,
                "method": method,
                "enctype": str(transport.get("enctype", "") or ""),
                "submitter": submitter, "pairs": pairs}

    def prepare_single_edit(self, single_id):
        single_id = str(single_id or "").strip()
        if not single_id.isdigit():
            raise ValueError("单页编号无效")
        snap = self._single_page_snapshot()
        row = next((item for item in snap["records"] if str(item["id"]) == single_id), None)
        if not row or not row.get("edit_url"):
            raise RuntimeError("目标单页不存在或缺少安全修改入口")
        response = self._request("GET", row["edit_url"], timeout=45, read_only=True)
        info = self._parse_single_form(response, single_id)
        return {"id": single_id, "revision": info["revision"],
                "fields": info["fields"], "method": info.get("method", "post"),
                "enctype": info.get("enctype", ""),
                "submitter": info.get("submitter")}

    def _validate_single_values(self, info, values):
        if not isinstance(values, dict):
            raise ValueError("单页字段数据无效")
        fields = {field.get("name"): field for field in info["fields"] if field.get("name")}
        unknown = set(values) - set(fields)
        if unknown:
            raise ValueError("包含后台未允许的单页字段：" + "、".join(sorted(unknown)))
        result = dict(info["defaults"])
        for name, value in values.items():
            field = fields[name]
            if field.get("readonly") or field.get("disabled"):
                continue
            normalized = normalize_control_value(value, field)
            issue = validate_control_value(normalized, field)
            if issue:
                raise ValueError(f"{field.get('label') or name}{issue}")
            result = merge_form_updates(result, info["fields"], {name: normalized})
        return result

    def update_single(self, single_id, values, expected_revision=""):
        single_id = str(single_id or "").strip()
        if not single_id.isdigit():
            raise ValueError("单页编号无效")
        snap = self._single_page_snapshot()
        row = next((item for item in snap["records"] if str(item["id"]) == single_id), None)
        if not row or not row.get("edit_url"):
            raise RuntimeError("目标单页不存在或缺少安全修改入口")
        info = self._parse_single_form(
            self._request("GET", row["edit_url"], timeout=45, read_only=True), single_id)
        if expected_revision and str(expected_revision) != str(info["revision"]):
            raise RuntimeError("单页表单已变化，请重新打开后再保存")
        data = self._validate_single_values(info, values or {})
        # Text + upload buttons and native file fields use the same page-owned
        # upload policy as articles/categories; local paths never leak into POST.
        fields = {field["name"]: field for field in info["fields"] if field.get("name")}
        upload_metadata = []
        upload_jobs = []
        for name, field in fields.items():
            kind = str(field.get("type", field.get("kind", ""))).lower()
            if kind != "file" and not field.get("upload_target"):
                continue
            raw = data.get(name)
            paths = raw if isinstance(raw, (list, tuple)) else [raw]
            local = [str(item or "") for item in paths if str(item or "") and os.path.isfile(str(item))]
            # A native browser file control cannot be pre-populated with a
            # server URL.  Text+upload controls may retain an existing URL,
            # but passing one through a real ``type=file`` field would make
            # the desktop submit a value the webpage could never submit.
            if kind == "file" and any(str(item or "").strip() and
                                       not os.path.isfile(str(item))
                                       for item in paths):
                raise ValueError(f"字段 {name} 的本地文件不存在；原生文件控件不能填写服务器地址")
            if not local:
                continue
            try:
                queue_limit = int(field.get("max_files") or 0)
            except (TypeError, ValueError):
                queue_limit = 0
            if queue_limit > 0 and len(local) > queue_limit:
                raise ValueError(f"字段 {name} 最多选择 {queue_limit} 个文件")
            upload_jobs.append({"key": name, "name": name, "field": field,
                                "paths": local, "upload_target": name,
                                "label": field.get("label") or name,
                                "media_kind": str(field.get("media_kind") or "file")})
        if upload_jobs:
            # Discover every field's page-owned policy before starting any
            # request.  The browser can interleave independent controls, so
            # the shared helper below then submits one global XHR queue.
            # Policy discovery is map-replacing.  A single discovery pass is
            # required when the page owns multiple independent file controls;
            # otherwise the first control loses its endpoint before the shared
            # browser-style queue starts.
            targets = list(dict.fromkeys(
                ("field", job["name"]) for job in upload_jobs))
            try:
                self.prepare_uploads(info["page_url"], targets)
            except Exception as exc:
                from upload_policy import UploadPolicyError
                if not isinstance(exc, UploadPolicyError):
                    raise
                from admin_modules import NativeModuleFallback
                names = "、".join(str(job["name"]) for job in upload_jobs)
                reason = f"单页字段 {names} 的网页上传策略无法安全复刻：{exc}"
                raise NativeModuleFallback(
                    reason + "，已切换原生网页完成上传和保存",
                    native_url=info.get("page_url") or self.admin_url,
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
                from admin_modules import NativeModuleFallback
                reason = str(exc.reason or exc)
                raise NativeModuleFallback(
                    reason + "，已切换原生网页完成上传和保存",
                    native_url=exc.native_url or info.get("page_url") or self.admin_url,
                    reason=reason) from exc
            except UploadOutcomeUnknown as exc:
                from admin_modules import NativeModuleFallback
                raise NativeModuleFallback(
                    "单页文件上传结果未知，文件可能已保存；已切换原生网页核对",
                    native_url=info.get("page_url") or self.admin_url,
                    reason=str(exc)) from exc
            except Exception as exc:
                raise RuntimeError(f"单页文件上传失败：{exc}") from exc
            for name, result in uploaded.items():
                urls = list(result.get("urls") or [])
                upload_metadata.extend(result.get("metadata") or [])
                field = fields[name]
                data[name] = urls if field.get("multiple") else urls[-1]
        from client_content import (_submit_content_form, _submitter_values,
                                    BrowserFormData, mark_write_attempt,
                                    mark_write_http_result, mark_write_rejected)
        data.update(_submitter_values(info.get("submitter")))
        transport_data = BrowserFormData.from_data(info.get("pairs", []), data)
        mark_write_attempt(self)
        response = _submit_content_form(
            self, info.get("method", "post"), info["action"], transport_data,
            info.get("fields", []), info.get("enctype", ""), {})
        if not getattr(response, "ok", False):
            mark_write_http_result(self, response)
            raise RuntimeError(f"保存单页失败（HTTP {getattr(response, 'status_code', '?')}）")
        text = BeautifulSoup(getattr(response, "text", ""), "html.parser").get_text(" ", strip=True)
        if _is_login_page(getattr(response, "text", "")) or (_ERROR_RE.search(text) and not re.search(r"成功|success", text, re.I)):
            message = text[:300] or "后台未确认单页保存"
            mark_write_rejected(self, response, message)
            raise RuntimeError(message)
        fresh = self._parse_single_form(
            self._request("GET", row["edit_url"], timeout=45, read_only=True), single_id)
        for name, value in data.items():
            if name in fresh["defaults"] and not browser_values_equal(
                    fresh["defaults"].get(name, ""), value):
                raise RuntimeError(f"单页已提交，但字段“{name}”回读不一致，请核对后台")
        listing = self._single_page_snapshot()
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "单页保存成功", "records": listing["records"],
                "revision": listing["revision"],
                "upload_metadata": upload_metadata}

    def toggle_single_status(self, single_id, value, expected_url="",
                             verify_readback=False):
        single_id = str(single_id or "").strip(); value = str(value or "").strip()
        if not single_id.isdigit() or value not in {"0", "1"}:
            raise ValueError("单页状态参数无效")
        if not expected_url:
            raise ValueError("缺少后台发现的状态操作链接")
        match = _TOGGLE_RE.search(_route(expected_url))
        if not match or match.group(1) != single_id or match.group(3) != value:
            raise RuntimeError("单页状态链接已变化，请刷新后重试")
        before = None
        if verify_readback:
            before = self._single_page_snapshot()
            row = next((item for item in before.get("records", [])
                        if str(item.get("id")) == single_id), None)
            discovered = (row or {}).get("toggles", {}).get("status")
            if not discovered or str(discovered.get("target")) != value:
                raise RuntimeError("单页状态列表已变化，请刷新后重试")
        # A successful HTTP response is not proof that the GET toggle was
        # applied.  Mark the write as uncertain before dispatch so a timeout
        # or readback failure is durable-review rather than a retryable error.
        self.last_write_result = {"outcome": "unknown", "write_attempted": True,
                                  "requires_review": True, "retryable": False}
        response = self._request("GET", expected_url, timeout=35, read_only=False)
        if not getattr(response, "ok", False):
            from client_content import mark_write_http_result
            mark_write_http_result(self, response)
            raise RuntimeError(f"切换单页状态失败（HTTP {getattr(response, 'status_code', '?')}）")
        text = BeautifulSoup(getattr(response, "text", ""), "html.parser").get_text(" ", strip=True)
        if _is_login_page(getattr(response, "text", "")) or (_ERROR_RE.search(text) and not re.search(r"成功|success", text, re.I)):
            from client_content import mark_write_rejected
            message = text[:300] or "后台未确认单页状态切换"
            mark_write_rejected(self, response, message)
            raise RuntimeError(message)
        result = {"msg": "单页状态已切换", "id": single_id, "value": value}
        if verify_readback:
            after = self._single_page_snapshot()
            row = next((item for item in after.get("records", [])
                        if str(item.get("id")) == single_id), None)
            discovered = (row or {}).get("toggles", {}).get("status")
            # The native list exposes the link for the *next* state.  After a
            # request to value N, that link must therefore point to 1-N.
            if not discovered or str(discovered.get("target")) == value:
                raise RuntimeError("单页状态请求已发送，但回读未证明目标状态已生效，请核对后台")
            result.update({"records": after.get("records", []),
                           "revision": after.get("revision", "")})
        self.last_write_result = {"outcome": "verified", "write_attempted": True,
                                  "requires_review": False, "retryable": False}
        return result
