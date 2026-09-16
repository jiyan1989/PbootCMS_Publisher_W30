"""Safe article-list administration parity for the native PbootCMS page.

The stock backend exposes copy, move, delete and sorting from the content
list form, plus GET links for the three visibility flags.  This module only
uses routes and field names discovered from the active site's own page.  A
write is accepted only after a fresh snapshot and a post-write readback prove
the requested IDs/values; ambiguous or custom pages stop before sending data.
"""

import hashlib
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from client_utils import _is_login_page
from form_controls import describe_form
from http_transport import permitted_transition, request_with_redirects


_CONTENT_ROUTE_RE = re.compile(
    r"(?:^|/)Content/(index|mod|del)(?:/|$)", re.I)
_FIELD_TOGGLE_RE = re.compile(
    r"(?:^|/)Content/mod/id/(\d+)/field/"
    r"(status|istop|isrecommend|isheadline)/value/([01])(?:/|$)", re.I)
_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|权限不足|未授权|被拒绝|登录.*失效|校验.*失败|"
    r"\b(?:error|failed|failure|forbidden|unauthorized|denied|invalid)\b", re.I)


def _route_part(value):
    """Return the decoded Pboot route from a query-style admin URL."""
    raw = str(value or "")
    parsed = urlparse(raw)
    query = parse_qs(parsed.query, keep_blank_values=True)
    route = query.get("p", [""])[-1] if query else ""
    if route:
        return unquote(route).lstrip("/")
    return raw.split("?", 1)[0].lstrip("/")


def _same_origin(left, right):
    try:
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _safe_route(url, expected):
    route = _route_part(url)
    return bool(re.search(expected, route, re.I))


def _text(response):
    return BeautifulSoup(str(getattr(response, "text", "") or ""),
                         "html.parser").get_text(" ", strip=True)


def _row_title(row, article_id):
    # The stock template puts the full title in td[title].  Prefer it over
    # clipped display text, then fall back to a non-operation link/text cell.
    for cell in row.find_all("td"):
        title = str(cell.get("title", "") or "").strip()
        if title and title != str(article_id) and not title.isdigit():
            return title
    for link in row.find_all("a", href=True):
        href = str(link.get("href", "") or "")
        label = link.get_text(" ", strip=True)
        if label and not _FIELD_TOGGLE_RE.search(_route_part(href)) and not re.search(
                r"编辑|删除|复制|移动|查看|修改|toggle|delete|mod|del|copy|move",
                href + " " + label, re.I):
            return label
    return f"文章#{article_id}"


def _explicit_view_url(row, response_url, base_url):
    """Return only a clearly labelled, same-origin front-end link.

    Content-list rows often contain edit/delete/status links beside the title.
    A desktop client must not invent a front-end route from an article ID, so
    this helper requires the page itself to label the anchor as view/preview
    and rejects administrative or state-changing routes.
    """
    positive = re.compile(
        r"查看|预览|前台|访问|详情|view|preview|front|visit|detail", re.I)
    negative = re.compile(
        r"编辑|修改|删除|移[动除]|复制|排序|状态|field[=/]|value[=/]|"
        r"\b(?:mod|edit|delete|remove|toggle|status)\b", re.I)
    for anchor in row.find_all("a", href=True):
        href = str(anchor.get("href", "") or "").strip()
        if not href or href.lower().startswith(("#", "javascript:", "mailto:")):
            continue
        label = " ".join(filter(None, (
            anchor.get_text(" ", strip=True),
            str(anchor.get("title", "") or "").strip(),
            str(anchor.get("aria-label", "") or "").strip(),
        )))
        if not positive.search(label) or negative.search(label):
            continue
        candidate = urljoin(response_url or base_url, href)
        if negative.search(candidate.lower()) or not _same_origin(candidate, base_url):
            continue
        return candidate
    return ""


class ContentAdminMixin:
    """Discover and safely execute native content-list operations."""

    def _content_admin_page(self, scode, *, mcode=None, page=1, keyword=""):
        scode = str(scode or "").strip()
        if not scode.isdigit():
            raise ValueError("栏目编号无效")
        if mcode is None:
            mcode = self._get_mcode_for_scode(scode)
        mcode = str(mcode or "").strip()
        if not mcode.isdigit():
            raise RuntimeError("未能确认该栏目的内容模型")
        base = self._url(f"Content/index/mcode/{mcode}")
        candidates = [(base, {"scode": scode})]
        if int(page or 1) > 1:
            candidates = [(base, {"scode": scode, "page": int(page) }),
                          (base, {"scode": scode, "p": int(page)})]
        if keyword:
            candidates = [(url, dict(params, keyword=str(keyword)))
                          for url, params in candidates]
        response = None
        soup = None
        for url, params in candidates:
            try:
                result = self._request("GET", url, params=params, timeout=30,
                                       read_only=True)
            except TypeError:
                # A few test/dedicated clients implement only a narrow
                # `_request` signature.  Retain their compatibility while
                # keeping the production fallback behind the same bounded
                # redirect guard; never silently use Session.get with its
                # default cross-origin redirect behavior.
                result = request_with_redirects(
                    self.session, "GET", url, params=params, timeout=30)
            if not getattr(result, "ok", False) or _is_login_page(getattr(result, "text", "")):
                continue
            final_url = getattr(result, "url", "") or url
            if not _same_origin(final_url, self.admin_url):
                continue
            candidate = BeautifulSoup(result.text, "html.parser")
            # Reject a page where the requested scode was silently ignored if
            # it contains row IDs with category evidence.
            rows = candidate.find_all("tr")
            row_scodes = []
            for row in rows:
                value = str(row.get("data-scode", "") or "").strip()
                if not value:
                    cell = next((c for c in row.find_all("td")
                                 if str(c.get("title", "") or "").strip().isdigit()), None)
                    value = str(cell.get("title", "") or "").strip() if cell else ""
                if value:
                    row_scodes.append(value)
            if row_scodes and any(value != scode for value in row_scodes):
                continue
            response, soup = result, candidate
            break
        if response is None or soup is None:
            raise RuntimeError("未能安全读取内容列表")

        list_form = None
        for form in soup.find_all("form"):
            action = urljoin(getattr(response, "url", "") or base,
                             str(form.get("action", "") or ""))
            # The list's editable batch form is Content/mod.  A page may put
            # a separate Content/del form before it in the DOM; accepting that
            # form here would make the later route check fail even though a
            # valid batch editor exists further down the page.
            if _same_origin(action, self.admin_url) and _safe_route(
                    action, r"^Content/mod(?:/|$)"):
                list_form = form
                break
        if list_form is None:
            raise RuntimeError("未发现内容列表的安全批量表单")
        from client_content import (_discover_default_submitter,
                                    _discover_submitter_options,
                                    _form_method_enctype)
        from form_controls import successful_pairs
        submitter = _discover_default_submitter(list_form)
        submitter_options = _discover_submitter_options(list_form)
        transport = _form_method_enctype(list_form, submitter)
        action = urljoin(getattr(response, "url", "") or base,
                         str(transport.get("action") or
                             list_form.get("action", "") or ""))
        if not _same_origin(action, self.admin_url) or not _safe_route(
                action, r"^Content/mod(?:/|$)"):
            raise RuntimeError("内容批量表单不是安全的 Content/mod POST")
        if str(transport.get("method", "post") or "post").lower() != "post":
            raise RuntimeError("内容批量表单未使用 POST")
        fields, defaults = describe_form(
            list_form, lambda _form, _element, name: str(name or ""),
            submitter=submitter)
        # Keep the exact successful-control order for later destructive and
        # bulk submissions.  The public bridge intentionally omits this
        # internal snapshot, but the writer uses it to preserve interleaved
        # repeated controls just like a browser FormData instance.
        pairs = successful_pairs(list_form, submitter=submitter)

        records = []
        for row in soup.find_all("tr"):
            checkbox = (row.find("input", {"name": "list[]"}) or
                        row.find("input", {"name": "ids[]"}) or
                        row.find("input", {"type": "checkbox", "name": True}))
            article_id = str(checkbox.get("value", "") or "").strip() if checkbox else ""
            if not article_id.isdigit():
                continue
            row_scode = str(row.get("data-scode", "") or "").strip()
            if not row_scode:
                cell = next((c for c in row.find_all("td")
                             if str(c.get("title", "") or "").strip().isdigit()), None)
                row_scode = str(cell.get("title", "") or "").strip() if cell else scode
            if row_scode != scode:
                continue
            links = {}
            toggles = {}
            for anchor in row.find_all("a", href=True):
                href = urljoin(getattr(response, "url", "") or base,
                               str(anchor.get("href", "") or ""))
                if not _same_origin(href, self.admin_url):
                    continue
                route = _route_part(href)
                match = _FIELD_TOGGLE_RE.search(route)
                if match and match.group(1) == article_id:
                    toggles[match.group(2).lower()] = {
                        "url": href, "target": int(match.group(3))}
                    continue
                if _safe_route(href, rf"^Content/mod/mcode/\d+/id/{re.escape(article_id)}"):
                    links["edit_url"] = href
                elif _safe_route(href, rf"^Content/del(?:/id/{re.escape(article_id)})?(?:/|$)"):
                    links["delete_url"] = href
                elif re.search(r"(?:^|/)Content/(?:copy|move)(?:/|$)", route, re.I):
                    links.setdefault("bulk_hint", href)
            sorting = ""
            sort_input = row.find("input", {"name": re.compile(r"^sorting(?:\[\])?$", re.I)})
            if sort_input:
                sorting = str(sort_input.get("value", "") or "")
            records.append({"id": article_id, "scode": scode,
                            "title": _row_title(row, article_id),
                            "sorting": sorting,
                            "view_url": _explicit_view_url(
                                row, getattr(response, "url", "") or base,
                                self.base_url or self.admin_url),
                            **links, "toggles": toggles})

        select = list_form.find("select", {"name": "scode"})
        targets = []
        if select:
            for option in select.find_all("option"):
                value = str(option.get("value", "") or "").strip()
                if value.isdigit():
                    targets.append({"value": value,
                                    "label": option.get_text(" ", strip=True)})
        revision_source = "|".join([
            str(getattr(response, "url", "") or base), action, scode, mcode,
            *(f"{item['id']}:{item['title']}:{item['sorting']}" for item in records)])
        revision = hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:24]
        return {"scode": scode, "mcode": mcode, "page": int(page or 1),
                "records": records, "action": action, "defaults": defaults,
                "fields": fields, "targets": targets, "revision": revision,
                "page_url": getattr(response, "url", "") or base,
                "method": str(transport.get("method", "post") or "post"),
                "enctype": str(transport.get("enctype", "") or ""),
                "submitter": submitter, "submitter_options": submitter_options,
                "pairs": pairs}

    @staticmethod
    def _public_content_admin(snapshot):
        return {key: snapshot[key] for key in
                ("scode", "mcode", "page", "records", "targets", "revision")}

    def prepare_content_admin(self, scode, mcode=None, page=1, keyword=""):
        return self._public_content_admin(
            self._content_admin_page(scode, mcode=mcode, page=page, keyword=keyword))

    @staticmethod
    def _validate_ids(snapshot, ids):
        values = [str(item or "").strip() for item in (ids or [])]
        if not values or any(not value.isdigit() for value in values):
            raise ValueError("请选择有效的文章")
        if len(set(values)) != len(values):
            raise ValueError("文章列表中存在重复编号")
        known = {str(item["id"]) for item in snapshot.get("records", [])}
        unknown = [value for value in values if value not in known]
        if unknown:
            raise RuntimeError("所选文章已变化，请刷新后重新选择")
        return values

    @staticmethod
    def _operation_submitter(snapshot, operation):
        """Resolve the actual clicked bulk-action button when available.

        A list form often has one submitter per operation.  Reusing the
        default save button would add the wrong ``name/value`` or even route
        the request to a different action.  Match the current DOM option by
        its inert label/name/value and otherwise let the explicit operation
        field below carry the stock route semantics.
        """
        terms = {
            "copy": ("copy", "复制"),
            "move": ("move", "移动"),
            "sorting": ("sorting", "排序"),
            "delete": ("delete", "del", "删除"),
        }.get(str(operation or "").lower(), ())
        options = snapshot.get("submitter_options") or []
        matches = []
        for option in options:
            haystack = " ".join(str(option.get(key, "") or "").lower()
                                for key in ("label", "name", "value", "formaction"))
            if any(term.lower() in haystack for term in terms):
                matches.append(option)
        if len(matches) > 1:
            raise RuntimeError("当前后台批量操作按钮不唯一，请刷新后重新选择")
        return matches[0] if matches else None

    @staticmethod
    def _response_error(response):
        if _is_login_page(getattr(response, "text", "")):
            return "登录会话已失效，请重新登录"
        text = _text(response)
        if _ERROR_RE.search(text) and not re.search(r"成功|success", text, re.I):
            return text[:300]
        return ""

    def _submit_content_bulk(self, snapshot, ids, operation, *, target_scode="",
                             sorting=None):
        ids = self._validate_ids(snapshot, ids)
        operation = str(operation or "").lower().strip()
        if operation not in {"copy", "move", "sorting"}:
            raise ValueError("不支持的内容批量操作")
        if operation in {"copy", "move"}:
            target_scode = str(target_scode or "").strip()
            if not target_scode.isdigit() or target_scode == str(snapshot["scode"]):
                raise ValueError("复制/移动必须选择不同的目标栏目")
            allowed = {str(item["value"]) for item in snapshot.get("targets", [])}
            if allowed and target_scode not in allowed:
                raise ValueError("目标栏目不在后台当前允许列表中")
        data = dict(snapshot.get("defaults") or {})
        # Browser successful controls submit repeated list[]/listall[] and
        # one sorting[] value per selected row.  requests accepts list tuples.
        pairs = []
        for value in ids:
            pairs.append(("list[]", value))
            pairs.append(("listall[]", value))
        if operation in {"copy", "move"}:
            # The list page contains two scode selects (filter + destination)
            # in the stock template.  The destination control is the second
            # successful value; when serializing a native form, its selected
            # value must replace the filter default rather than be appended
            # as an ambiguous list.
            data["scode"] = target_scode
            pairs.append(("submit", operation))
        else:
            values = sorting if isinstance(sorting, dict) else {}
            for value in ids:
                if value not in values:
                    raise ValueError(f"缺少文章 {value} 的排序值")
                pairs.append(("sorting[]", str(values[value])))
            pairs.append(("submit", "sorting"))
        for key, value in pairs:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(value)
            else:
                data[key] = [data[key], value]
        from client_content import (_submit_content_form, _submitter_values,
                                    BrowserFormData, mark_write_attempt,
                                    mark_write_http_result, mark_write_rejected)
        operation_submitter = self._operation_submitter(snapshot, operation)
        data.update(_submitter_values(operation_submitter))
        transport_data = BrowserFormData.from_data(snapshot.get("pairs", []), data)
        mark_write_attempt(self)
        response = _submit_content_form(
            self, snapshot.get("method", "post"), snapshot["action"], transport_data,
            snapshot.get("fields", []), snapshot.get("enctype", ""), {})
        if not getattr(response, "ok", False):
            mark_write_http_result(self, response)
            raise RuntimeError(f"内容批量操作失败（HTTP {getattr(response, 'status_code', '?')}）")
        error = self._response_error(response)
        if error:
            mark_write_rejected(self, response, error)
            raise RuntimeError(error)
        return response

    def content_bulk_action(self, scode, ids, operation, expected_revision="",
                             target_scode="", sorting=None, mcode=None):
        before = self._content_admin_page(scode, mcode=mcode)
        if expected_revision and str(expected_revision) != str(before["revision"]):
            raise RuntimeError("内容列表已变化，请刷新后重新确认")
        selected = self._validate_ids(before, ids)
        before_map = {str(item["id"]): item for item in before["records"]}
        self._submit_content_bulk(before, selected, operation,
                                  target_scode=target_scode, sorting=sorting)
        after = self._content_admin_page(scode, mcode=before["mcode"])
        after_map = {str(item["id"]): item for item in after["records"]}
        operation = str(operation or "").lower().strip()
        if operation == "copy":
            if any(item_id not in after_map for item_id in selected):
                raise RuntimeError("复制已提交，但原文章回读缺失，请核对后台")
        elif operation == "move":
            if any(item_id in after_map for item_id in selected):
                raise RuntimeError("移动已提交，但原栏目仍存在目标文章，请核对后台")
        elif operation == "sorting":
            for item_id in selected:
                wanted = str((sorting or {}).get(item_id, ""))
                actual = str(after_map.get(item_id, {}).get("sorting", ""))
                if wanted != actual:
                    raise RuntimeError(f"排序已提交，但文章 {item_id} 回读不一致")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": {"copy": "内容复制成功", "move": "内容移动成功",
                         "sorting": "内容排序保存成功"}[operation],
                "operation": operation, "records": after["records"],
                "revision": after["revision"]}

    def delete_content(self, scode, ids, expected_revision="", mcode=None):
        before = self._content_admin_page(scode, mcode=mcode)
        if expected_revision and str(expected_revision) != str(before["revision"]):
            raise RuntimeError("内容列表已变化，请刷新后重新确认删除")
        selected = self._validate_ids(before, ids)
        delete_urls = {str(before_item["delete_url"]) for before_item in before["records"]
                       if str(before_item["id"]) in selected and before_item.get("delete_url")}
        # The stock page changes the bulk form action to /Content/del in JS.
        # Require a discovered per-row delete route as evidence of that route;
        # never guess a destructive endpoint from a hard-coded path.
        if not delete_urls:
            raise RuntimeError("后台未发现安全的内容删除入口")
        routes = {_route_part(url).split("/id/", 1)[0].rstrip("/")
                  for url in delete_urls}
        if len(routes) != 1:
            raise RuntimeError("选中文章的删除路由不唯一，已停止")
        discovered_delete = next(iter(delete_urls))
        delete_route = _route_part(discovered_delete)
        delete_route = re.split(r"/id/", delete_route, maxsplit=1, flags=re.I)[0].strip("/")
        if not _safe_route(discovered_delete, rf"^{re.escape(delete_route)}(?:/|$)"):
            raise RuntimeError("后台删除路由解析失败，已停止")
        delete_action = self._url(delete_route)
        data = dict(before.get("defaults") or {})
        pairs = [("list[]", value) for value in selected]
        pairs.extend(("listall[]", value) for value in selected)
        pairs.append(("submit", "del"))
        for key, value in pairs:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(value)
            else:
                data[key] = [data[key], value]
        from client_content import (_submit_content_form, _submitter_values,
                                    BrowserFormData, mark_write_attempt,
                                    mark_write_http_result, mark_write_rejected)
        data.update(_submitter_values(self._operation_submitter(before, "delete")))
        transport_data = BrowserFormData.from_data(before.get("pairs", []), data)
        mark_write_attempt(self)
        response = _submit_content_form(
            self, before.get("method", "post"), delete_action, transport_data,
            before.get("fields", []), before.get("enctype", ""),
            {"Referer": before.get("page_url", "")})
        if not getattr(response, "ok", False):
            mark_write_http_result(self, response)
            raise RuntimeError(f"删除内容失败（HTTP {getattr(response, 'status_code', '?')}）")
        error = self._response_error(response)
        if error:
            mark_write_rejected(self, response, error)
            raise RuntimeError(error)
        after = self._content_admin_page(scode, mcode=before["mcode"])
        remaining = {str(item["id"]) for item in after["records"]}
        if any(value in remaining for value in selected):
            raise RuntimeError("删除请求已发送，但后台仍回读到目标内容，请核对")
        from client_content import mark_write_verified
        mark_write_verified(self)
        return {"msg": f"已删除 {len(selected)} 篇内容", "records": after["records"],
                "revision": after["revision"]}

    def toggle_content_field(self, article_id, field, value, expected_url="",
                             scode="", mcode=None, verify_readback=False):
        article_id = str(article_id or "").strip()
        field = str(field or "").strip().lower()
        target = str(value or "").strip()
        if not article_id.isdigit() or field not in {"status", "istop", "isrecommend", "isheadline"} or target not in {"0", "1"}:
            raise ValueError("内容状态参数无效")
        if expected_url:
            route = _route_part(expected_url)
            match = _FIELD_TOGGLE_RE.search(route)
            if not match or match.group(1) != article_id or match.group(2).lower() != field or match.group(3) != target:
                raise RuntimeError("状态操作链接已变化，请刷新后重试")
            url = expected_url
        else:
            raise ValueError("缺少后台发现的状态操作链接")
        before = None
        if verify_readback:
            scode = str(scode or "").strip()
            if not scode.isdigit():
                raise ValueError("缺少状态回读所需的栏目编号")
            before = self._content_admin_page(scode, mcode=mcode)
            row = next((item for item in before.get("records", [])
                        if str(item.get("id")) == article_id), None)
            discovered = (row or {}).get("toggles", {}).get(field)
            if not discovered or str(discovered.get("target")) != target:
                raise RuntimeError("内容状态列表已变化，请刷新后重试")
        self.last_write_result = {"outcome": "unknown", "write_attempted": True,
                                  "requires_review": True, "retryable": False}
        response = self._request("GET", url, timeout=35, read_only=False)
        error = self._response_error(response)
        if error:
            from client_content import mark_write_rejected
            mark_write_rejected(self, response, error)
            raise RuntimeError(error)
        if not getattr(response, "ok", False):
            from client_content import mark_write_http_result
            mark_write_http_result(self, response)
            raise RuntimeError(f"切换内容状态失败（HTTP {getattr(response, 'status_code', '?')}）")
        result = {"msg": "内容状态已切换", "id": article_id,
                  "field": field, "value": target}
        if verify_readback:
            after = self._content_admin_page(scode, mcode=mcode)
            row = next((item for item in after.get("records", [])
                        if str(item.get("id")) == article_id), None)
            discovered = (row or {}).get("toggles", {}).get(field)
            if not discovered or str(discovered.get("target")) == target:
                raise RuntimeError("内容状态请求已发送，但回读未证明目标状态已生效，请核对后台")
            result.update({"records": after.get("records", []),
                           "revision": after.get("revision", "")})
        self.last_write_result = {"outcome": "verified", "write_attempted": True,
                                  "requires_review": False, "retryable": False}
        return result
