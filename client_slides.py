"""Safe management of PbootCMS's independent website Slide/轮播 records.

Article ``pics`` is an article gallery and is deliberately not treated as a
site-wide carousel.  This mixin discovers the active site's own Slide routes
and forms before allowing a mutation, keeps the original hidden controls, and
verifies every create/update/delete by reading the index again.
"""
import hashlib
import os
import re
from types import SimpleNamespace
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from client_utils import _is_login_page
from form_controls import (describe_form, successful_pairs, merge_form_updates,
                           normalize_control_value, upload_field_is_image,
                           validate_control_value, browser_values_equal)
from http_transport import permitted_transition


_SLIDE_ROUTE = re.compile(r"(?:^|[/=])slide/(index|add|mod|del)(?:[/&?#]|$)", re.I)
_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|未授权|被拒绝|登录.*失效|校验.*失败|"
    r"\b(?:error|failed|failure|forbidden|unauthorized|denied)\b", re.I)


def _same_origin(left, right):
    try:
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _route(value, operation):
    raw = str(value or "")
    return bool(re.search(
        rf"(?:^|[/=])slide/{re.escape(operation)}(?:[/&?#]|$)", raw, re.I))


def _param(value, key):
    match = re.search(rf"(?:^|[/&?]){re.escape(key)}(?:/|=)([^/&?#]+)",
                      str(value or ""), re.I)
    return match.group(1) if match else ""


def _label(form, element, name):
    element_id = str(element.get("id", "") or "")
    if element_id:
        label = form.find("label", {"for": element_id})
        if label:
            text = label.get_text(" ", strip=True)
            if text:
                return text[:120]
    item = element.find_parent(class_=re.compile(r"layui-form-item|form-item|form-group", re.I))
    if item:
        label = item.find(class_=re.compile(r"layui-form-label", re.I)) or item.find("label")
        if label:
            text = label.get_text(" ", strip=True)
            if text:
                return text[:120]
    return {"gid": "分组", "pic": "图片", "link": "链接",
            "title": "标题", "subtitle": "副标题", "sorting": "排序"}.get(name, name)


class SlideMixin:
    """Discover and safely edit independent ``/Slide/*`` records."""

    def _slide_absolute(self, href, reference):
        candidate = urljoin(reference or self.admin_url, str(href or "").strip())
        return candidate if _same_origin(candidate, self.base_url or self.admin_url) else ""

    def _slide_index_snapshot(self):
        url = self._url("Slide/index")
        response = self._request("GET", url, timeout=45, read_only=True)
        if not getattr(response, "ok", False):
            raise RuntimeError(f"轮播列表读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        soup = BeautifulSoup(response.text, "html.parser")
        reference = getattr(response, "url", "") or url
        add_url = ""
        for anchor in soup.find_all("a", href=True):
            candidate = self._slide_absolute(anchor.get("href", ""), reference)
            if candidate and _route(candidate, "add"):
                add_url = candidate
                break
        inline = ""
        for form in soup.find_all("form"):
            action = self._slide_absolute(form.get("action", ""), reference)
            if action and _route(action, "add"):
                inline = str(form)
                if form.find(attrs={"name": "title"}) or form.find(attrs={"name": "pic"}):
                    break
        records = []
        for row in soup.select("tr"):
            record = self._parse_slide_row(row, reference)
            if record:
                records.append(record)
        return {"records": records, "add_url": add_url, "add_form_html": inline,
                "reference": reference}

    @staticmethod
    def _parse_slide_row(row, reference):
        values = {}
        for name in ("id", "gid", "pic", "link", "title", "subtitle", "sorting"):
            element = row.find(attrs={"name": name})
            if element is not None:
                values[name] = str(element.get("value", "") or "")
        cells = row.find_all("td", recursive=False)
        # Standard PbootCMS puts the hidden id in a checkbox and then renders
        # gid/pic/link/title/subtitle/sorting as table cells.
        checkbox = row.find("input", attrs={"name": re.compile(r"(?:list|ids?)", re.I)})
        rid = str((checkbox or {}).get("value", "") or "") if checkbox else ""
        if not rid:
            rid = _param(next((a.get("href", "") for a in row.find_all("a", href=True)
                               if _route(a.get("href", ""), "mod")), ""), "id")
        if not rid:
            rid = str(row.get("data-id", "") or "")
        if not rid.isdigit():
            return None
        if cells:
            usable = list(cells)
            if usable and usable[0].find("input") is not None:
                usable = usable[1:]
            if usable and usable[-1].find("a", href=True) is not None:
                usable = usable[:-1]
            texts = [cell.get_text(" ", strip=True) for cell in usable]
            # Only fill absent values; action buttons and checkboxes vary by
            # template, while the canonical six columns remain in order.
            for name, value in zip(("gid", "pic", "link", "title", "subtitle", "sorting"), texts):
                values.setdefault(name, value)
        values["id"] = rid
        values.setdefault("title", "")
        values.setdefault("pic", "")
        values.setdefault("link", "")
        values.setdefault("subtitle", "")
        values.setdefault("gid", "")
        values.setdefault("sorting", "")
        edit_url = delete_url = ""
        for anchor in row.find_all("a", href=True):
            href = str(anchor.get("href", "") or "")
            candidate = urljoin(reference, href)
            if not _same_origin(candidate, reference):
                continue
            if _route(candidate, "mod") and not edit_url:
                edit_url = candidate
            elif _route(candidate, "del") and not delete_url:
                delete_url = candidate
        values.update(edit_url=edit_url, delete_url=delete_url)
        return values

    def list_slides(self):
        snapshot = self._slide_index_snapshot()
        return {"slides": snapshot["records"]}

    def _parse_slide_form(self, response, operation, slide_id=""):
        if not getattr(response, "ok", False):
            raise RuntimeError(f"轮播表单读取失败（HTTP {getattr(response, 'status_code', '?')}）")
        if _is_login_page(getattr(response, "text", "")):
            raise RuntimeError("登录会话已失效，请重新登录")
        soup = BeautifulSoup(response.text, "html.parser")
        reference = getattr(response, "url", "") or self.admin_url
        form = None
        for candidate in soup.find_all("form"):
            action = self._slide_absolute(candidate.get("action", ""), reference)
            if action and _route(action, operation):
                form = candidate
                break
        if form is None:
            raise RuntimeError("未发现可安全提交的轮播表单")
        from admin_modules import NativeModuleFallback, _dynamic_form_reason
        dynamic_reason = _dynamic_form_reason(soup, form, reference)
        if dynamic_reason:
            raise NativeModuleFallback(dynamic_reason + "，已切换原生网页完成轮播操作",
                                       native_url=reference, reason=dynamic_reason)
        from client_content import (_discover_default_submitter,
                                    _form_method_enctype,
                                    _get_write_submitter_allowed)
        submitter = _discover_default_submitter(form)
        transport = _form_method_enctype(form, submitter)
        action = self._slide_absolute(
            transport.get("action") or form.get("action", ""), reference)
        if not action or not _route(action, operation):
            raise RuntimeError("轮播表单提交地址不安全")
        method = str(transport.get("method", "post") or "post").lower()
        if method not in ("post", "get"):
            raise RuntimeError("轮播表单使用了未适配的提交方法")
        if method == "get" and not _get_write_submitter_allowed(submitter):
            raise NativeModuleFallback(
                "轮播表单使用 GET 但未能确认保存按钮，已切换原生网页完成轮播操作",
                native_url=reference,
                reason="轮播 GET 表单缺少明确保存提交按钮")
        fields, defaults = describe_form(
            form, _label, submitter=submitter)
        pairs = successful_pairs(form, submitter=submitter)
        visible = [dict(field) for field in fields if field.get("type") != "hidden"]
        names = {str(field.get("name", "")) for field in visible if field.get("name")}
        upload_targets = {}
        for button in form.find_all(["button", "a", "input"]):
            target = str(button.get("data-des", "") or "").strip()
            classes = " ".join(button.get("class") or [])
            if target and "upload" in classes.lower() and target in names:
                upload_targets[target] = button
        for field in visible:
            target = upload_targets.get(str(field.get("name", "")))
            if target:
                field["upload_target"] = str(field["name"])
                if "uploads" in set(target.get("class", [])):
                    field["multiple"] = True
                if target.get("accept") and not field.get("accept"):
                    field["accept"] = target.get("accept")
        revision_source = "\n".join([action, operation, str(slide_id)] +
                                      [f"{k}={defaults[k]}" for k in sorted(defaults)
                                       if not re.search(r"csrf|token|formcheck", k, re.I)])
        revision = hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:24]
        return {"action": action, "page_url": reference, "fields": visible,
                "defaults": defaults, "allowed": names, "revision": revision,
                "id": str(slide_id or ""), "mode": "create" if operation == "add" else "edit",
                "method": method,
                "enctype": str(transport.get("enctype", "") or ""),
                "submitter": submitter, "pairs": pairs}

    def _open_slide_form(self, operation, slide_id="", snapshot=None):
        snapshot = snapshot or self._slide_index_snapshot()
        if operation == "add":
            inline = snapshot.get("add_form_html", "")
            url = snapshot.get("add_url", "")
            if inline:
                response = SimpleNamespace(ok=True, status_code=200, text=inline,
                                           url=snapshot.get("reference", "") or self.admin_url)
            elif url:
                response = self._request("GET", url, timeout=30, read_only=True)
            else:
                raise RuntimeError("后台没有发现 Slide 新增入口")
        else:
            row = next((item for item in snapshot["records"] if str(item.get("id")) == str(slide_id)), None)
            if not row or not row.get("edit_url"):
                raise RuntimeError("目标 Slide 不存在或缺少安全修改入口")
            response = self._request("GET", row["edit_url"], timeout=30,
                                     read_only=True)
        return self._parse_slide_form(response, operation, slide_id)

    @staticmethod
    def _public_form(info):
        return {key: info[key] for key in (
            "mode", "id", "revision", "fields", "method", "enctype",
            "submitter")}

    def prepare_slide_create(self):
        return self._public_form(self._open_slide_form("add"))

    def prepare_slide_edit(self, slide_id):
        slide_id = str(slide_id or "").strip()
        if not slide_id.isdigit():
            raise ValueError("Slide 编号无效")
        return self._public_form(self._open_slide_form("mod", slide_id))

    def _validate_slide_values(self, info, values):
        if not isinstance(values, dict):
            raise ValueError("Slide 表单数据无效")
        descriptors = {field["name"]: field for field in info["fields"] if field.get("name")}
        unknown = set(values) - set(descriptors)
        if unknown:
            raise ValueError("包含后台未允许的 Slide 字段：" + "、".join(sorted(unknown)))
        normalized = {}
        for name, value in values.items():
            field = descriptors[name]
            value = normalize_control_value(value, field)
            issue = validate_control_value(value, field)
            if issue:
                raise ValueError(f"{field.get('label') or name}{issue}")
            normalized[name] = value
        return normalized

    def _submit_slide(self, info, values, expected_revision, *, slide_id="", snapshot=None):
        if not expected_revision or str(expected_revision) != str(info["revision"]):
            raise RuntimeError("Slide 表单已变化，请重新打开后再保存")
        values = self._validate_slide_values(info, values)
        descriptors = {field["name"]: field for field in info["fields"] if field.get("name")}
        # A stock Slide form uses a text ``pic`` field plus
        # ``button.upload[data-des="pic"]``.  If the desktop picker supplied a
        # local path, use that button's discovered upload policy first; URLs
        # already present in the form remain untouched.
        upload_metadata = []
        upload_jobs = []
        for name, field in descriptors.items():
            kind = str(field.get("type", field.get("kind", "")) or "").lower()
            if not field.get("upload_target") and kind != "file":
                continue
            raw = values.get(name)
            paths = raw if isinstance(raw, (list, tuple)) else [raw]
            local = [str(item or "").strip() for item in paths
                     if str(item or "").strip() and os.path.isfile(str(item))]
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
            # Discover every control's policy first, then submit all selected
            # files through one global browser-style XHR queue so independent
            # Slide fields can complete in the same interleaved fashion as
            # the native page.
            # ``prepare_uploads`` replaces the policy map, so discover all
            # independent Slide controls together instead of retaining only
            # the last field's endpoint/field metadata.
            targets = list(dict.fromkeys(
                ("field", job["name"]) for job in upload_jobs))
            try:
                self.prepare_uploads(info.get("page_url") or self.admin_url,
                                     targets)
            except Exception as exc:
                from upload_policy import UploadPolicyError
                if not isinstance(exc, UploadPolicyError):
                    raise
                from admin_modules import NativeModuleFallback
                names = "、".join(str(job["name"]) for job in upload_jobs)
                reason = f"Slide 字段 {names} 的网页上传策略无法安全复刻：{exc}"
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
                    "Slide 文件上传结果未知，文件可能已保存；已切换原生网页核对",
                    native_url=info.get("page_url") or self.admin_url,
                    reason=str(exc)) from exc
            except Exception as exc:
                raise RuntimeError(f"Slide 文件上传失败：{exc}") from exc
            for name, result in uploaded.items():
                urls = list(result.get("urls") or [])
                upload_metadata.extend(result.get("metadata") or [])
                field = descriptors[name]
                values[name] = urls if field.get("multiple") else urls[-1]
        current = dict(info["defaults"])
        data = merge_form_updates(current, info["fields"], values)
        # A slide form may expose id/gid as hidden controls.  Keep the hidden
        # values captured by the real form rather than manufacturing IDs.
        if slide_id and "id" in info["defaults"]:
            data["id"] = str(slide_id)
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
            raise RuntimeError(f"保存 Slide 失败（HTTP {getattr(response, 'status_code', '?')}）")
        text = BeautifulSoup(getattr(response, "text", ""), "html.parser").get_text(" ", strip=True)
        if _is_login_page(getattr(response, "text", "")):
            message = "登录会话已失效，请重新登录"
            mark_write_rejected(self, response, message)
            raise RuntimeError(message)
        if _ERROR_RE.search(text) and not re.search(r"成功|success", text, re.I):
            message = text[:300] or "后台未确认保存 Slide"
            mark_write_rejected(self, response, message)
            raise RuntimeError(message)
        refreshed = self._slide_index_snapshot()
        records = refreshed["records"]
        if slide_id:
            row = next((item for item in records if str(item.get("id")) == str(slide_id)), None)
            if row is None:
                raise RuntimeError("Slide 已提交，但回读列表未找到目标记录，请核对后台")
            for name, value in values.items():
                if name in row and not browser_values_equal(row.get(name, ""), value):
                    raise RuntimeError(f"Slide 已提交，但字段“{name}”回读不一致，请核对后台")
            from client_content import mark_write_verified
            mark_write_verified(self)
            return {"msg": "Slide 修改成功", "slide": row, "slides": records,
                    "upload_metadata": upload_metadata}
        # New ID must be unique relative to the pre-write snapshot.  If a
        # custom table hides IDs, do not claim success from a generic message.
        before_ids = {str(item.get("id")) for item in (snapshot or {}).get("records", [])}
        added = [item for item in records if str(item.get("id")) not in before_ids]
        if len(added) != 1:
            raise RuntimeError("Slide 已提交，但无法唯一确认新增记录，请刷新后台核对")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "Slide 新增成功", "slide": added[0], "slides": records,
                "upload_metadata": upload_metadata}

    def create_slide(self, values, expected_revision=""):
        snapshot = self._slide_index_snapshot()
        info = self._open_slide_form("add", snapshot=snapshot)
        return self._submit_slide(info, values, expected_revision, snapshot=snapshot)

    def update_slide(self, slide_id, values, expected_revision=""):
        slide_id = str(slide_id or "").strip()
        if not slide_id.isdigit():
            raise ValueError("Slide 编号无效")
        info = self._open_slide_form("mod", slide_id)
        return self._submit_slide(info, values, expected_revision, slide_id=slide_id)

    def delete_slide(self, slide_id, expected_title=""):
        slide_id = str(slide_id or "").strip()
        if not slide_id.isdigit():
            raise ValueError("Slide 编号无效")
        snapshot = self._slide_index_snapshot()
        row = next((item for item in snapshot["records"] if str(item.get("id")) == slide_id), None)
        if not row:
            raise RuntimeError("目标 Slide 已不存在，请刷新列表")
        if expected_title and str(row.get("title", "")) != str(expected_title):
            raise RuntimeError("Slide 标题已变化，请刷新后重新确认删除")
        if not row.get("delete_url"):
            raise RuntimeError("未发现安全的 Slide 删除入口")
        from client_content import (mark_write_attempt, mark_write_http_result,
                                    mark_write_rejected)
        mark_write_attempt(self)
        response = self._request("GET", row["delete_url"], timeout=45)
        if _is_login_page(getattr(response, "text", "")):
            message = "登录会话已失效，请重新登录"
            mark_write_rejected(self, response, message)
            raise RuntimeError(message)
        if not getattr(response, "ok", False):
            mark_write_http_result(self, response)
            raise RuntimeError(f"删除 Slide 失败（HTTP {getattr(response, 'status_code', '?')}）")
        text = BeautifulSoup(getattr(response, "text", ""), "html.parser").get_text(" ", strip=True)
        if _ERROR_RE.search(text) and not re.search(r"成功|success", text, re.I):
            message = text[:300] or "后台未确认删除 Slide"
            mark_write_rejected(self, response, message)
            raise RuntimeError(message)
        refreshed = self._slide_index_snapshot()
        if any(str(item.get("id")) == slide_id for item in refreshed["records"]):
            raise RuntimeError("后台未确认 Slide 已删除，请刷新后核对")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": "Slide 删除成功", "slides": refreshed["records"]}
