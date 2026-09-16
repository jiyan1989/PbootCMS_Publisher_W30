"""Focused PbootCMS ProductMixin service."""
import io
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import copy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
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
from http_transport import request_with_redirects
from client_utils import get_base_dir, _is_login_page
from form_controls import successful_pairs


_PRODUCT_MENU_RE = re.compile(r"产品|商品|\bproducts?\b", re.I)
_NEXT_PAGE_RE = re.compile(r"^(?:后一页|下一页|下页|next|[>›»]+)$", re.I)


def _normalized_field_key(value):
    """将字段名/标签归一化为可精确比较的键。"""
    value = str(value or "").strip().lower()
    value = re.sub(r"^(?:ext|custom)[_-]", "", value)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def _url_origin(value):
    """返回 HTTP(S) origin；不可用 URL 返回 None。"""
    try:
        raw = str(value or "")
        parsed = urlparse(raw)
        if (parsed.scheme.lower() not in ("http", "https") or
                not parsed.hostname or parsed.username or parsed.password or
                any(ord(char) < 33 for char in raw) or "\\" in raw):
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower(), port
    except (TypeError, ValueError):
        return None


def _same_origin_or_http_upgrade(candidate, reference):
    target, source = _url_origin(candidate), _url_origin(reference)
    if not target or not source:
        return False
    if target == source:
        return True
    return (source[0] == "http" and target[0] == "https"
            and source[1] == target[1] and source[2] == 80
            and target[2] == 443)


class ProductSnapshot(list):
    """A product list whose completeness was proved by the paginator.

    Reconciliation is destructive because IDs missing from the incoming list
    are deleted from the local cache.  A plain list therefore must not be
    treated as a complete remote snapshot by accident.
    """

    def __init__(self, values=(), complete=False, empty_confirmed=False):
        super().__init__(values)
        self.complete = bool(complete)
        self.empty_confirmed = bool(empty_confirmed)


class ProductMixin:
    @staticmethod
    def _check_product_cancelled(cancel_callback):
        """兼容“返回 True”与“直接抛出业务取消异常”的回调。"""
        if cancel_callback is not None and cancel_callback():
            raise RuntimeError("产品同步已取消")

    @staticmethod
    def _mcode_from_url(value):
        """仅从 PbootCMS 路由中提取数字 mcode。"""
        text = str(value or "")
        match = re.search(r"(?:^|[/&?])mcode(?:/|=)(\d+)(?:[/&#?]|$)", text, re.I)
        return match.group(1) if match else ""

    @staticmethod
    def _has_route_query_p(url):
        """URL 的 ``p`` 是否已经是 PbootCMS 后台路由参数。"""
        try:
            pairs = parse_qsl(urlparse(str(url or "")).query,
                                keep_blank_values=True)
        except (TypeError, ValueError):
            return False
        return any(key.lower() == "p" and value.startswith("/")
                   for key, value in pairs)

    @classmethod
    def _normalize_duplicate_route_page_p(cls, url):
        """修正二开后台错误生成的 ``?p=/路由&p=页码``。

        PbootCMS 的 ``p`` 本来承载后台路由。部分二开主题却把分页页码
        也写成 ``p``，PHP 最终只会拿到后一个值，导致 ``HTTP 404``。
        只有能明确识别到“前一个 p 是 /Content 路由、后一个 p 是纯数字”
        时才把后者改成 ``page``，其余查询参数一律原样保留。
        """
        try:
            parsed = urlparse(str(url or ""))
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
        except (TypeError, ValueError):
            return str(url or "")
        route_p_seen = False
        changed = False
        normalized = []
        for key, value in pairs:
            if key.lower() == "p" and value.startswith("/"):
                route_p_seen = True
                normalized.append((key, value))
            elif route_p_seen and key.lower() == "p" and value.isdigit():
                normalized.append(("page", value))
                changed = True
            else:
                normalized.append((key, value))
        if not changed:
            return parsed._replace(fragment="").geturl()
        fixed = parsed._replace(
            query=urlencode(normalized, doseq=True), fragment="")
        debug_log(f"[fetch_all] 修正重复路由分页参数: {url} -> {fixed.geturl()}")
        return fixed.geturl()

    @staticmethod
    def _validate_product_mcode(value):
        text = str(value or "").strip()
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(f"无效的产品模型 mcode: {value!r}")
        return text

    def _product_cache_key(self):
        return str(getattr(self, "admin_url", "") or
                   getattr(self, "base_url", "")).rstrip("/").lower()

    def _remember_product_mcode(self, mcode):
        mcode = self._validate_product_mcode(mcode)
        self._product_mcode_context = (self._product_cache_key(), mcode)
        return mcode

    def _cached_product_mcode(self):
        context = getattr(self, "_product_mcode_context", None)
        if (isinstance(context, tuple) and len(context) == 2
                and context[0] == self._product_cache_key()):
            return context[1]
        return ""

    @staticmethod
    def _label_for_element(form, element):
        """在常见 PbootCMS/LayUI/表格布局中找到字段真实标签。"""
        element_id = (element.get("id") or "").strip()
        if element_id:
            label = form.find("label", {"for": element_id})
            if label and label.get_text(" ", strip=True):
                return label.get_text(" ", strip=True)
        item = element.find_parent(class_=re.compile(
            r"(^|\s)layui-form-item(\s|$)"))
        if item:
            label = item.find(class_=re.compile(
                r"(^|\s)layui-form-label(\s|$)"))
            if label and label.get_text(" ", strip=True):
                return label.get_text(" ", strip=True)
        row = element.find_parent("tr")
        cell = element.find_parent(["td", "th"])
        if row and cell:
            cells = row.find_all(["td", "th"], recursive=False)
            if cell in cells and cells.index(cell) > 0:
                text = cells[cells.index(cell) - 1].get_text(" ", strip=True)
                if text:
                    return text
        return ((element.get("aria-label") or element.get("placeholder") or
                 element.get("title") or "").strip())

    def _form_field_descriptors(self, html):
        """从新增/编辑表单中取出 name + label，不猜测提交字段。"""
        soup = BeautifulSoup(html or "", "html.parser")
        form = soup.find("form", {"id": "edit"})
        if not form:
            form = soup.find("form", action=re.compile(
                r"/Content/(?:add|mod|edit)(?:/|\b)", re.I))
        if not form:
            return []
        fields = []
        seen = set()
        for element in form.find_all(["input", "textarea", "select"]):
            name = (element.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            fields.append({
                "name": name,
                "label": self._label_for_element(form, element),
            })
        return fields

    @staticmethod
    def _detect_product_fields(fields, require_model=True):
        """
        依据编辑表单的真实 ``name`` 与 ``label`` 识别型号/价格。

        只接受唯一的高置信候选；价格字段可以不存在，但多个
        高置信候选不会被静默地选中其一。
        """
        model_names = {
            "xinghao", "model", "modelno", "modelnumber", "productmodel",
            "equipmentmodel", "instrumentmodel", "sku",
        }
        model_labels = {
            "型号", "产品型号", "商品型号", "设备型号", "仪器型号",
            "规格型号", "型号规格", "model", "modelno", "modelnumber",
            "productmodel", "equipmentmodel", "instrumentmodel", "sku",
        }
        price_names = {
            "jiage", "price", "productprice", "saleprice", "sellingprice",
            "unitprice",
        }
        price_labels = {
            "价格", "产品价格", "商品价格", "售价", "销售价格", "单价",
            "price", "productprice", "saleprice", "sellingprice", "unitprice",
        }
        model_candidates = []
        price_candidates = []
        for field in fields or []:
            name = str(field.get("name", "") or "").strip()
            if not name:
                continue
            name_key = _normalized_field_key(name)
            label_key = _normalized_field_key(field.get("label", ""))
            is_model = name_key in model_names or label_key in model_labels
            is_price = name_key in price_names or label_key in price_labels
            # 允许常见的币种后缀，但不对“价格说明”等子串模糊匹配。
            if re.fullmatch(r"(?:product)?price(?:rmb|cny|usd|yuan)", label_key):
                is_price = True
            if re.fullmatch(r"(?:产品|商品)?价格(?:元|人民币|美元)", label_key):
                is_price = True
            if is_model:
                model_candidates.append(name)
            if is_price:
                price_candidates.append(name)

        model_candidates = list(dict.fromkeys(model_candidates))
        price_candidates = list(dict.fromkeys(price_candidates))
        if len(model_candidates) > 1:
            raise RuntimeError(
                "型号字段无法唯一确认：" + ", ".join(model_candidates))
        if require_model and not model_candidates:
            raise RuntimeError("未能从编辑表单唯一识别型号字段")
        if len(price_candidates) > 1:
            raise RuntimeError(
                "价格字段无法唯一确认：" + ", ".join(price_candidates))
        return (model_candidates[0] if model_candidates else "",
                price_candidates[0] if price_candidates else "")

    def _checked_get(self, url, *, params=None, timeout=30):
        """只读 GET，并拒绝跨域重定向与登录页伪响应。"""
        reference = getattr(self, "admin_url", "") or getattr(self, "base_url", "")
        # Use the shared browser-like redirect guard.  Calling
        # ``Session.get`` directly followed cross-origin redirects before the
        # final-origin check, which differed from the protected read paths and
        # could leak a query token to an unexpected host.
        response = request_with_redirects(
            self.session, "GET", url, params=params, timeout=timeout)
        resolved = getattr(response, "url", "") or url
        if not _same_origin_or_http_upgrade(resolved, reference):
            raise RuntimeError(
                f"后台请求发生跨域跳转，已拒绝：{resolved}")
        if not getattr(response, "ok", False):
            raise RuntimeError(
                f"HTTP {getattr(response, 'status_code', '?')} ({resolved})")
        if _is_login_page(getattr(response, "text", "")):
            raise PermissionError("后台返回了登录页，会话已失效")
        return response

    def _checked_query_request(self, method, url, *, params=None, data=None,
                               timeout=30):
        """Read a server-side list form using its declared transport.

        Most Pboot list filters are GET forms, but custom themes sometimes
        use a POST form for the same read-only search.  Replaying that form is
        safe only when its action remains same-origin and the response is
        validated exactly like ``_checked_get``; it is never reused for a
        write workflow.
        """
        method = str(method or "get").upper()
        if method == "GET":
            return self._checked_get(url, params=params, timeout=timeout)
        if method != "POST":
            raise RuntimeError(f"后台产品搜索使用了不支持的只读方法：{method}")
        reference = getattr(self, "admin_url", "") or getattr(self, "base_url", "")
        # A custom product list may use a read-only POST filter.  It is still
        # a browser form submission and therefore carries the current page
        # Origin, even though it is not a mutation.
        headers = {}
        try:
            page = urlparse(getattr(self, "base_url", "") or reference)
            target = urlparse(str(url or ""))
            if (page.scheme in ("http", "https") and page.netloc and
                    target.scheme in ("http", "https") and target.netloc and
                    page.hostname and target.hostname and
                    page.hostname.lower() == target.hostname.lower() and
                    (page.port or (443 if page.scheme == "https" else 80)) ==
                    (target.port or (443 if target.scheme == "https" else 80))):
                headers["Origin"] = f"{page.scheme}://{page.netloc}"
        except (TypeError, ValueError):
            pass
        # Preserve ordered/repeated query controls when a custom read-only
        # filter uses POST, and reject cross-origin redirects before the next
        # request is sent.
        response = request_with_redirects(
            self.session, "POST", url, data=data or {}, timeout=timeout,
            headers=headers)
        resolved = getattr(response, "url", "") or url
        if not _same_origin_or_http_upgrade(resolved, reference):
            raise RuntimeError(f"后台产品搜索发生跨域跳转，已拒绝：{resolved}")
        if not getattr(response, "ok", False):
            raise RuntimeError(f"HTTP {getattr(response, 'status_code', '?')} ({resolved})")
        if _is_login_page(getattr(response, "text", "")):
            raise PermissionError("后台返回了登录页，会话已失效")
        return response

    def _discover_product_mcode(self, cancel_callback=None):
        """从已登录后台菜单/模型表单唯一确认产品 mcode。"""
        self._check_product_cancelled(cancel_callback)
        cached = self._cached_product_mcode()
        if cached:
            return cached

        home_url = self._url("Index/home")
        try:
            home = self._checked_get(home_url, timeout=20)
        except Exception as exc:
            raise RuntimeError(f"无法从后台首页识别产品模型：{exc}") from exc
        soup = BeautifulSoup(home.text or "", "html.parser")
        model_labels = {}
        for anchor in soup.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            mcode = self._mcode_from_url(href)
            if not mcode:
                continue
            labels = model_labels.setdefault(mcode, [])
            label = " ".join(filter(None, [
                anchor.get_text(" ", strip=True),
                str(anchor.get("title") or "").strip(),
            ])).strip()
            if label:
                labels.append(label)

        if not model_labels:
            raise RuntimeError(
                "后台首页未发现任何内容模型入口，无法安全确认产品 mcode")
        menu_matches = {
            code for code, labels in model_labels.items()
            if any(_PRODUCT_MENU_RE.search(label) for label in labels)
        }
        if len(menu_matches) == 1:
            return self._remember_product_mcode(next(iter(menu_matches)))

        # 菜单文字被定制或多个模型都叫“产品”时，只读新增表单，
        # 以唯一的真实型号字段作为第二证据。
        form_matches = set()
        form_errors = []
        probe_codes = sorted(menu_matches or set(model_labels), key=lambda x: int(x))
        for code in probe_codes:
            self._check_product_cancelled(cancel_callback)
            try:
                response = self._checked_get(
                    self._url(f"Content/add/mcode/{code}"), timeout=20)
                fields = self._form_field_descriptors(response.text)
                model_field, _price_field = self._detect_product_fields(fields)
                if model_field:
                    form_matches.add(code)
            except RuntimeError as exc:
                form_errors.append(f"mcode={code}: {exc}")
            except Exception as exc:
                form_errors.append(f"mcode={code}: {type(exc).__name__}")
        if len(form_matches) == 1:
            return self._remember_product_mcode(next(iter(form_matches)))

        candidates = sorted(menu_matches or form_matches, key=lambda x: int(x))
        if len(candidates) > 1 or len(form_matches) > 1:
            values = sorted(menu_matches | form_matches, key=lambda x: int(x))
            raise RuntimeError(
                "产品模型 mcode 存在歧义（候选：" +
                ", ".join(values) + "），已停止同步")
        detail = f"；{' | '.join(form_errors[:3])}" if form_errors else ""
        raise RuntimeError(
            "无法从后台菜单或模型表单唯一确认产品 mcode" + detail)

    def list_product_models(self, cancel_callback=None):
        """Read all visible product-model candidates without choosing one.

        The web backend lets an operator select a model when several menu
        entries are labelled as products.  The old desktop path rejected that
        ambiguity outright; this read-only method exposes the same choices so
        the caller can pass an explicit ``mcode`` to synchronization.
        """
        self._check_product_cancelled(cancel_callback)
        home = self._checked_get(self._url("Index/home"), timeout=20)
        soup = BeautifulSoup(home.text or "", "html.parser")
        labels = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "") or "")
            code = self._mcode_from_url(href)
            if not code:
                continue
            text = " ".join(filter(None, [anchor.get_text(" ", strip=True),
                                            str(anchor.get("title", "") or "").strip()]))
            labels.setdefault(code, set()).update([text] if text else [])
        candidates = []
        for code in sorted(labels, key=lambda value: int(value)):
            self._check_product_cancelled(cancel_callback)
            model_field = price_field = ""
            try:
                response = self._checked_get(self._url(f"Content/add/mcode/{code}"), timeout=20)
                fields = self._form_field_descriptors(response.text)
                model_field, price_field = self._detect_product_fields(fields, require_model=False)
            except Exception as exc:
                # The menu itself remains a valid candidate; retain the error
                # for the UI instead of silently dropping a custom model.
                candidates.append({"mcode": code, "labels": sorted(labels[code]),
                                   "model_field": "", "price_field": "",
                                   "probe_error": str(exc)[:240]})
                continue
            candidates.append({"mcode": code, "labels": sorted(labels[code]),
                               "model_field": model_field or "",
                               "price_field": price_field or ""})
        if not candidates:
            raise RuntimeError("后台首页未发现可选内容模型")
        return candidates

    def _resolve_operation_mcode(self, mcode=None, edit_urls=None,
                                 edit_url=None):
        """操作级 mcode：显式值 > 编辑链接 > 本站同步缓存。"""
        if mcode not in (None, ""):
            return self._remember_product_mcode(mcode)
        discovered = set()
        if edit_url:
            value = self._mcode_from_url(edit_url)
            if value:
                discovered.add(value)
        for value in (edit_urls or {}).values():
            parsed = self._mcode_from_url(value)
            if parsed:
                discovered.add(parsed)
        if len(discovered) > 1:
            raise RuntimeError(
                "产品编辑链接包含多个 mcode：" +
                ", ".join(sorted(discovered, key=int)))
        if discovered:
            return self._remember_product_mcode(next(iter(discovered)))
        cached = self._cached_product_mcode()
        if cached:
            return cached
        raise RuntimeError("未确认产品模型 mcode")

    def parse_product_row(self, row, mcode=None):
        """从产品列表的一行 <tr> 解析出产品 dict；非产品行返回 None。
        标题只取指向本产品编辑页的「标题链接」自身文本，避免同单元格内
        「缩图 / 查看缩略图」等兄弟链接/文字被一起拼进标题（如“真实标题缩图”）。
        """
        # ── 产品 ID 识别（尽量宽松，兼容不同后台的复选框/链接命名）──
        pid = ""
        cb = row.find("input", {"type": "checkbox"})
        if cb:
            v = (cb.get("value") or "").strip()
            if v.isdigit():
                pid = v
        if not pid:
            # 备选：行内任意带数字 value 的 input（部分后台用其他 name，如 list[]/ids[]/check[]）
            for inp in row.find_all("input"):
                v = (inp.get("value") or "").strip()
                if v.isdigit():
                    pid = v
                    break
        if not pid:
            # 备选：从指向本产品编辑页的链接中提取 id/{数字}
            for a in row.find_all("a"):
                href = a.get("href", "")
                # 排除 PbootCMS「字段切换」写操作链接：?p=/Content/mod/id/58/field/status/value/0
                # （GET 它即把 status 改成 0，会误隐藏产品）
                if ("Content/mod" in href or "Content/edit" in href) and "id/" in href and "field/" not in href.lower():
                    m = re.search(r"id/(\d+)", href)
                    if m:
                        pid = m.group(1)
                        break
        if not pid:
            return None
        tds = row.find_all("td")
        title = ""
        cat_name = ""
        edit_url = ""
        front_url = ""
        # 栏目：<td title="数字"> 为其栏目 id，文本为栏目名
        for td in tds:
            td_title = td.get("title", "").strip()
            if td_title and td_title.isdigit():
                cat_name = td.get_text(strip=True)
                break
        # 标题：取指向本���品编辑页（Content/mod 或 Content/edit 且含 id/{pid}）的链接文本，
        # 排除「修改/删除/缩图」等操作类链接，确保拿到的是标题本身。
        op_labels = {"修改", "编辑", "删除", "查看", "置顶", "推荐",
                     "缩图", "缩略图", "预览", "复制", "移动", "详情"}
        for a in row.find_all("a"):
            href = a.get("href", "")
            # 排除 PbootCMS「字段切换」写操作链接（?p=/Content/mod/id/58/field/status/value/0）：
            # 它同样含 Content/mod 与 id/{pid}，但 GET 即执行写入（把 status 改成 0 隐藏产品）。
            # 若把它误当编辑链接传给 get_edit_form 并发起 GET，拉取也会误改后台。
            if ("Content/mod" in href or "Content/edit" in href) and f"id/{pid}" in href and "field/" not in href.lower():
                t = a.get_text(strip=True)
                if t and t not in op_labels:
                    title = t
                    edit_url = href
                    break
                elif not edit_url:
                    edit_url = href
        # Only a link explicitly labelled as a front-end/view action is a
        # trustworthy source.  A neighbouring ``/145.html`` (or any other
        # unlabeled URL) is not enough evidence: lists often contain banners,
        # thumbnail links, or custom actions whose URL shape happens to end in
        # the product ID.  Do not invent a route from the ID or filename here;
        # callers may still use the separate, explicitly verified link-check
        # suggestion flow when the site exposes no preview anchor.
        preview_labels = {"查看", "预览", "前台", "访问", "打开"}
        for a in row.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            text = a.get_text(" ", strip=True)
            absolute = urljoin((self.base_url or "").rstrip("/") + "/", href)
            parsed = urlparse(absolute)
            low = href.lower()
            if (not href or text not in preview_labels or
                    low.startswith(("#", "javascript:", "mailto:", "tel:")) or
                    "?p=" in low or "field/" in low or
                    "/static/" in parsed.path.lower() or
                    "/upload/" in parsed.path.lower() or
                    not _same_origin_or_http_upgrade(
                        absolute, getattr(self, "base_url", ""))):
                continue
            front_url = absolute
            break
        # 兜底：无标题链接时，用 td 扫描（排除操作标签/缩图字样）
        if not title:
            for td in tds:
                text = td.get_text(strip=True)
                if (text and len(text) > 3 and text != pid
                        and not text.isdigit() and text not in op_labels
                        and "缩图" not in text and "缩略图" not in text):
                    title = text[:120]
                    break
        row_mcode = (str(mcode) if mcode not in (None, "") else
                     self._mcode_from_url(edit_url))
        return {"id": pid, "title": title, "xinghao": "", "jiage": "",
                "cat_name": cat_name, "edit_url": edit_url, "front_url": front_url,
                "mcode": row_mcode,
                "xinghao_field": FIELD_XINGHAO, "jiage_field": FIELD_JIAGE}

    @staticmethod
    def _next_page_marker(soup, page_number):
        """返回 (has_next, href)，只接受可证实的分页链接。

        产品列表最后一页常有“下一页”禁用按钮；而列表中的产品 ID、
        左侧菜单也可能恰好是下一个数字。旧逻辑遍历全页的所有 ``<a>``，
        会把这种普通链接误判为分页，继而请求不存在的一页并拿到最后页
        的重复数据。这里要求数字链接位于分页容器，或 href 明确含页码。
        """
        anchors = soup.find_all("a")

        def is_disabled(anchor):
            classes = " ".join(anchor.get("class") or [])
            parent_classes = " ".join(
                (anchor.parent.get("class") or []) if anchor.parent else [])
            attr_disabled = (anchor.has_attr("disabled") or
                             str(anchor.get("aria-disabled", "")).lower() == "true")
            return attr_disabled or bool(re.search(
                r"(?:^|[\s_-])disabled(?:$|[\s_-])|layui-disabled",
                classes + " " + parent_classes, re.I))

        def usable_href(anchor):
            href = (anchor.get("href") or "").strip()
            if not href or href in ("#", "javascript:;") \
                    or href.lower().startswith("javascript:"):
                return ""
            return href

        def pager_context(anchor):
            node = anchor
            for _ in range(5):
                if node is None:
                    break
                marker = " ".join([
                    str(node.get("id", "") or ""),
                    " ".join(node.get("class") or []),
                ])
                if re.search(r"(?:laypage|pagination|pager|pagebar|page-nav)",
                             marker, re.I):
                    return True
                node = node.parent
            return False

        def href_has_page_number(href):
            if not href:
                return False
            try:
                pairs = parse_qsl(urlparse(href).query,
                                    keep_blank_values=True)
            except (TypeError, ValueError):
                pairs = []
            for key, value in pairs:
                name = key.lower()
                if (name in ("page", "pageno", "page_no", "pageindex",
                             "page_index", "p") and value.isdigit()):
                    return True
            return bool(re.search(r"(?:^|/)page/(?:\d+)(?:/|$)", href, re.I))

        for anchor in anchors:
            if is_disabled(anchor):
                continue
            label = anchor.get_text(" ", strip=True)
            rel = " ".join(anchor.get("rel") or [])
            # 有些二开后台把可用“下一页”写成 href="#"，实际分页
            # 参数由前端脚本补上。只要按钮未标记为禁用，仍应进入
            # ``page``/旧式 ``p`` 的受限兼容候选；此时返回空 href，
            # 不会跟随这个无效地址。末页的禁用状态会在上面被过滤。
            if "next" in rel.lower() or _NEXT_PAGE_RE.fullmatch(label or ""):
                return True, usable_href(anchor)
        wanted = str(page_number + 1)
        for anchor in anchors:
            if is_disabled(anchor) or anchor.get_text(" ", strip=True) != wanted:
                continue
            href = usable_href(anchor)
            if href and (pager_context(anchor) or href_has_page_number(href)):
                return True, href
        return False, ""

    def _safe_next_url(self, href, response_url, mcode):
        if not href or href in ("#", "javascript:;") \
                or href.lower().startswith("javascript:"):
            return ""
        candidate = urljoin(response_url, href)
        reference = getattr(self, "admin_url", "") or getattr(self, "base_url", "")
        if not _same_origin_or_http_upgrade(candidate, reference):
            raise RuntimeError(
                f"产品分页链接指向其他站点，已拒绝：{candidate}")
        linked_mcode = self._mcode_from_url(candidate)
        if linked_mcode and linked_mcode != str(mcode):
            raise RuntimeError(
                f"产品分页链接 mcode 发生变化：{mcode} -> {linked_mcode}")
        candidate = self._normalize_duplicate_route_page_p(candidate)
        parsed = urlparse(candidate)
        return parsed._replace(fragment="").geturl()

    def _get_product_page(self, url, *, params=None, page_number=1,
                          cancel_callback=None):
        response = None
        last_error = None
        for attempt in range(3):
            self._check_product_cancelled(cancel_callback)
            try:
                response = self._checked_get(url, params=params, timeout=30)
                self._last_list_html = response.text
                return response
            except PermissionError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self._check_product_cancelled(cancel_callback)
                    time.sleep(2)
        message = f"产品列表第 {page_number} 页加载失败（已重试3次）"
        if last_error:
            message += f": {last_error}"
        raise RuntimeError(message)

    def _parse_product_page(self, response, mcode, seen_ids):
        soup = BeautifulSoup(response.text, "html.parser")
        parsed_products = []
        for row in soup.find_all("tr"):
            product = self.parse_product_row(row, mcode=mcode)
            if product is not None:
                parsed_products.append(product)
        fresh = [item for item in parsed_products if item["id"] not in seen_ids]
        return soup, parsed_products, fresh

    def _product_empty_confirmed(self, soup, response, mcode):
        """Require an unfiltered content-list route and a real empty table row.

        An arbitrary blank/error page (or a notice elsewhere on the page) is
        not evidence that every cached product has been deleted.
        """
        resolved = str(getattr(response, "url", "") or "")
        parsed = urlparse(resolved)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        route = next((value for key, value in query if key == "p"), parsed.path)
        if not re.search(r"/Content/index/mcode/" + re.escape(str(mcode)) +
                         r"/?$", route, re.I):
            return False
        if any(value and key != "p" for key, value in query):
            return False
        if _is_login_page(str(soup)) or self._next_page_marker(soup, 1)[0]:
            return False
        for cell in soup.select("table td"):
            if not re.fullmatch(r"(?:暂无(?:相关)?数据|没有(?:相关)?数据|无数据|no\s+data)[。.!！]?",
                                cell.get_text(" ", strip=True), re.I):
                continue
            table = cell.find_parent("table")
            if table.find("input", attrs={"type": "checkbox", "value": re.compile(r"\d+")}) or table.find(
                    "a", href=re.compile(r"/Content/(?:mod|edit)/.*?id/", re.I)):
                continue
            headers = " ".join(node.get_text(" ", strip=True)
                               for node in table.find_all("th"))
            if re.search(r"标题|名称|title|name", headers, re.I):
                return True
        return False

    def fetch_all_products(self, mcode=None, cancel_callback=None):
        """翻页抓取所有产品ID和标题。

        只有明确走到最后一页才返回 ``complete=True`` 的快照。
        任意中间页网络/HTTP 失败都抛出异常，不把前几页伪装成
        可供删除对账的“全量结果”。
        """
        # 正常同步不使用默认 mcode=3：显式参数或后台唯一发现。
        self._check_product_cancelled(cancel_callback)
        resolved_mcode = (self._remember_product_mcode(mcode)
                          if mcode not in (None, "")
                          else self._discover_product_mcode(cancel_callback))
        products = ProductSnapshot()
        seen_ids = set()
        try:
            page = 1
            list_url = self._url(f"Content/index/mcode/{resolved_mcode}")
            response = self._get_product_page(
                list_url, page_number=page,
                cancel_callback=cancel_callback)
            request_url = list_url
            while True:
                self._check_product_cancelled(cancel_callback)
                soup, parsed_products, fresh = self._parse_product_page(
                    response, resolved_mcode, seen_ids)
                for product in fresh:
                    seen_ids.add(product["id"])
                    products.append(product)
                debug_log(
                    f"[fetch_all] page={page}, found={len(fresh)}, total={len(products)}")
                if page == 1 and not parsed_products:
                    if not self._product_empty_confirmed(soup, response, resolved_mcode):
                        raise RuntimeError("未确认后台产品列表为空，已保留原有缓存")
                    products.empty_confirmed = True
                    break
                has_next, href = self._next_page_marker(soup, page)
                if not has_next:
                    break
                next_page = page + 1
                response_url = getattr(response, "url", "") or request_url
                real_next = self._safe_next_url(
                    href, response_url, resolved_mcode)

                # 优先跟随后台给出的真实 href。只有它返回重复首页/空页时，
                # 才使用 page/p 兼容参数进行有限兜底，且永不接受重复 ID。
                candidates = []
                if real_next:
                    candidates.append((real_next, None, "href"))
                candidates.append((list_url, {"page": next_page}, "page"))
                # ``p`` 已经被 ``?p=/Content/...`` 占用时，再传 p=页码会
                # 生成重复查询键并覆盖路由（截图中的 HTTP 404 根因）。
                # 只有路径式后台路由才允许尝试这个旧分页参数。
                if not self._has_route_query_p(list_url):
                    candidates.append((list_url, {"p": next_page}, "p"))
                candidate_keys = set()
                accepted = None
                last_duplicate = ""
                last_fetch_error = ""
                nonempty_candidate_count = 0
                repeated_current_count = 0
                empty_candidate_count = 0
                current_page_ids = [
                    item["id"] for item in parsed_products
                ]
                for candidate_url, params, source in candidates:
                    self._check_product_cancelled(cancel_callback)
                    key = (candidate_url, tuple(sorted((params or {}).items())))
                    if key in candidate_keys:
                        continue
                    candidate_keys.add(key)
                    try:
                        candidate_response = self._get_product_page(
                            candidate_url, params=params, page_number=next_page,
                            cancel_callback=cancel_callback)
                    except PermissionError:
                        raise
                    except Exception as exc:
                        # 一个二开后台给出的分页 href 失效时，仍允许已验证的
                        # page/旧式参数候选继续尝试；任何候选都失败才中止同步。
                        last_fetch_error = str(exc)
                        debug_log(
                            f"[fetch_all] page={next_page} candidate={source} "
                            f"加载失败，将尝试其他分页方式: {exc}")
                        continue
                    candidate_soup, candidate_parsed, candidate_fresh = \
                        self._parse_product_page(
                            candidate_response, resolved_mcode, seen_ids)
                    if candidate_fresh:
                        accepted = (candidate_response, candidate_url,
                                    candidate_soup, source)
                        break
                    if candidate_parsed:
                        nonempty_candidate_count += 1
                        candidate_ids = [
                            item["id"] for item in candidate_parsed
                        ]
                        if candidate_ids == current_page_ids:
                            repeated_current_count += 1
                        last_duplicate = f"{source} 返回了重复的上一页"
                    else:
                        empty_candidate_count += 1
                        last_duplicate = f"{source} 未解析到产品"
                if accepted is None:
                    # 部分 PbootCMS 二开后台会一直显示“下一页”，并把超出
                    # 末页的 page=N 请求强制回退到最后一页。只有本轮同步
                    # 已经成功翻过至少一页，且所有成功解析的候选都完整重复
                    # 当前页时，才把它视为可证明的末页。首次翻页即重复、
                    # 返回更早页面、空页面或夹杂请求失败仍保持报错，防止把
                    # 真正的分页参数失效误判成完整快照。
                    clamped_to_last_page = (
                        page > 1
                        and nonempty_candidate_count > 0
                        and repeated_current_count == nonempty_candidate_count
                        and empty_candidate_count == 0
                        and not last_fetch_error
                    )
                    if clamped_to_last_page:
                        debug_log(
                            f"[fetch_all] page={next_page} 完整重复第 {page} 页，"
                            "确认后台将越界页回退到末页，停止翻页")
                        break
                    if last_fetch_error:
                        raise RuntimeError(
                            f"产品列表第 {next_page} 页没有可用分页地址："
                            f"{last_fetch_error}")
                    raise RuntimeError(
                        f"产品列表第 {next_page} 页没有新产品ID，"
                        f"分页参数可能未生效（{last_duplicate}）")
                response, request_url, _accepted_soup, source = accepted
                debug_log(f"[fetch_all] next page source={source} url={request_url}")
                page = next_page
            self._check_product_cancelled(cancel_callback)
            products.complete = True
            debug_log(f"[fetch_all] done: {len(products)} products")
        except Exception as e:
            debug_log(f"[fetch_all] error: {e}")
            raise
        return products

    def query_products_remote(self, keyword="", *, page=1, mcode=None,
                             cancel_callback=None):
        """Query the backend's own product list form without mutating it.

        The desktop cache remains the fast default, but the web list can have
        server-side filtering rules (custom fields, Chinese collation and
        permission-aware rows) that a local substring search cannot reproduce.
        Discover the site's literal search control first, then issue a single
        read-only GET with its name and retain the real pagination marker.
        """
        self._check_product_cancelled(cancel_callback)
        resolved_mcode = (self._remember_product_mcode(mcode)
                          if mcode not in (None, "")
                          else self._discover_product_mcode(cancel_callback))
        list_url = self._url(f"Content/index/mcode/{resolved_mcode}")
        keyword = str(keyword or "").strip()
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1

        # Read the form once to discover the actual keyword field.  Never send
        # a guessed collection of aliases: a custom field with the same name
        # could otherwise narrow the result unexpectedly.
        response = self._get_product_page(list_url, page_number=1,
                                           cancel_callback=cancel_callback)
        soup = BeautifulSoup(response.text, "html.parser")
        query_name = ""
        query_form = None
        candidates = []
        for form in soup.find_all("form"):
            # Do not score the entire form text: a normal product table can
            # contain the word “搜索” in a help paragraph or a row title.
            # Only the form's own action/submit controls are search evidence.
            form_context = " ".join(filter(None, [
                str(form.get("id", "") or ""),
                str(form.get("class", "") or ""),
                str(form.get("action", "") or ""),
                " ".join(btn.get_text(" ", strip=True)
                          for btn in form.find_all(["button", "input"])
                          if str(btn.get("type", "")).lower() in {"submit", "button"}
                          or btn.name == "button"),
            ]))
            for control in form.find_all(["input", "select"]):
                name = str(control.get("name", "") or "").strip()
                if not name or str(control.get("type", "text")).lower() in {
                        "hidden", "submit", "button", "checkbox", "radio"}:
                    continue
                label = " ".join(str(control.get(key, "") or "")
                                 for key in ("placeholder", "title", "aria-label"))
                parent = control.find_parent(class_=re.compile(r"form-item|layui-form-item"))
                if parent:
                    label += " " + parent.get_text(" ", strip=True)
                score = 0
                if re.search(r"搜索|关键字|关键词|查询|search|keyword|query", label, re.I):
                    score += 5
                if re.search(r"搜索|关键字|关键词|查询|search|keyword|query", form_context, re.I):
                    score += 1
                if re.search(r"title|name|model|xinghao|product", name, re.I):
                    score += 3
                if score:
                    candidates.append((score, name, form))
        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            query_name, query_form = candidates[0][1], candidates[0][2]
        elif keyword:
            # Common PbootCMS list forms use keyword; this fallback is only
            # used when the page exposes no discoverable search control.
            query_name = "keyword"

        params = {}
        form_action = list_url
        form_method = "get"
        if query_form is not None:
            raw_action = str(query_form.get("action", "") or "").strip()
            form_action = urljoin(getattr(response, "url", "") or list_url,
                                  raw_action or list_url)
            if not _same_origin_or_http_upgrade(
                    form_action, getattr(self, "admin_url", "") or self.base_url):
                raise RuntimeError(f"后台产品搜索地址跨域，已拒绝：{form_action}")
            form_method = str(query_form.get("method", "get") or "get").strip().lower()
            # A browser submits every successful non-file control in the
            # search form, not just the keyword and hidden token.  Preserve
            # selected model/category filters, sort/order and page-size
            # controls as well; this is what makes custom server-side
            # sorting/pagination behave like the web form.  Submit buttons
            # are omitted because no physical click was supplied.
            for key, value in successful_pairs(query_form, exclude=(), submitter=None):
                if key not in params:
                    params[key] = value
                elif isinstance(params[key], list):
                    params[key].append(value)
                else:
                    params[key] = [params[key], value]
        if query_name:
            params[query_name] = keyword
        if params:
            # The first request was only for discovering the controls.  The
            # actual result must come from the form-submitted filtered page.
            response = self._get_product_page(
                form_action, params=params if form_method == "get" else None,
                page_number=1, cancel_callback=cancel_callback) \
                if form_method == "get" else self._checked_query_request(
                    form_method, form_action, data=params, timeout=30)

        # Follow the same real pagination links the browser would follow.
        # Sending a guessed ``page=2`` works on stock PbootCMS but silently
        # fails on installations using ``p``, ``pageno`` or a JS-generated
        # query string.  For a requested later page, walk from page one and
        # retain each server-provided search/pagination parameter.  The
        # bounded walk also makes a repeated/invalid page fail visibly rather
        # than returning an earlier page as if it were the requested result.
        current_page = 1
        request_url = list_url
        request_params = params or None
        while current_page < page:
            self._check_product_cancelled(cancel_callback)
            soup, _parsed, _fresh = self._parse_product_page(
                response, resolved_mcode, set())
            has_next, next_href = self._next_page_marker(soup, current_page)
            if not has_next:
                return {
                    "products": [], "page": current_page,
                    "has_next": False, "next_url": "",
                    "complete": True, "keyword_field": query_name,
                    "mcode": str(resolved_mcode),
                    "native_url": str(getattr(response, "url", "") or list_url),
                }
            response_url = getattr(response, "url", "") or request_url
            real_next = self._safe_next_url(
                next_href, response_url, resolved_mcode)
            if real_next:
                request_url, request_params = real_next, None
            else:
                request_url, request_params = form_action, dict(params)
                request_params["page"] = current_page + 1
            if request_params is None:
                response = self._get_product_page(
                    request_url, params=None, page_number=current_page + 1,
                    cancel_callback=cancel_callback)
            elif form_method == "post" and request_url == form_action:
                response = self._checked_query_request(
                    "post", request_url, data=request_params, timeout=30)
            else:
                response = self._get_product_page(
                    request_url, params=request_params,
                    page_number=current_page + 1,
                    cancel_callback=cancel_callback)
            current_page += 1

        soup, parsed, _fresh = self._parse_product_page(
            response, resolved_mcode, set())
        has_next, next_href = self._next_page_marker(soup, current_page)
        return {
            "products": parsed,
            "page": current_page,
            "has_next": bool(has_next),
            "next_url": str(next_href or ""),
            "complete": not bool(has_next),
            "keyword_field": query_name,
            "mcode": str(resolved_mcode),
            # If a custom search/pagination script cannot be mirrored by the
            # requests adapter, the caller can hand this exact same-origin
            # list page to the native browser.  This is evidence from the
            # discovered form, never a guessed frontend route.
            "native_url": str(getattr(response, "url", "") or list_url),
        }

    def fetch_product_details(self, product_ids, progress_callback=None, max_workers=10,
                              edit_urls=None, item_callback=None, mcode=None,
                              cancel_callback=None):
        """批量读取产品型号/价格，字段名以编辑表单为准。"""
        product_ids = list(product_ids)
        results = {}
        total = len(product_ids)
        if total == 0:
            return results
        self._check_product_cancelled(cancel_callback)
        edit_urls = edit_urls or {}
        resolved_mcode = self._resolve_operation_mcode(
            mcode=mcode, edit_urls=edit_urls)

        domain = (urlparse(getattr(self, "admin_url", "") or
                           getattr(self, "base_url", "")).hostname or "").lower()
        src_cookies = []
        for cookie in getattr(self.session, "cookies", ()):
            cookie_domain = cookie.domain.lstrip(".").lower()
            # A host-only cookie is represented by an empty domain in
            # requests.  Browsers still send it to the current host, so do
            # not drop it merely because it has no Domain attribute.
            if (not cookie_domain or cookie_domain == domain or
                    domain.endswith("." + cookie_domain)):
                src_cookies.append(copy(cookie))
        headers = dict(getattr(self.session, "headers", {}) or {})
        tls = threading.local()
        session_registry = []
        session_registry_lock = threading.Lock()

        def get_session():
            session = getattr(tls, "session", None)
            if session is None:
                session = requests.Session()
                session.headers.update(headers)
                session.verify = getattr(self.session, "verify", True)
                # Product detail reads run in their own thread-local
                # sessions.  They must use the same site-scoped routing as
                # the owning browser-like client; otherwise a custom/system
                # proxy works for the list but silently disappears for detail
                # fields and front-url repair.
                network_mode = str(getattr(self, "network_mode", "direct") or "direct").lower()
                proxy_url = str(getattr(self, "proxy_url", "") or "")
                if network_mode == "system":
                    session.trust_env = True
                    session.proxies.clear()
                elif network_mode == "custom" and proxy_url:
                    session.trust_env = False
                    session.proxies.update({"http": proxy_url, "https": proxy_url})
                else:
                    session.trust_env = False
                    session.proxies.clear()
                for cookie in src_cookies:
                    session.cookies.set_cookie(copy(cookie))
                audit_hook = getattr(type(self), "_audit_response", None)
                if callable(audit_hook):
                    session.hooks["response"].append(audit_hook)
                baseline = {
                    (str(cookie.name or ""),
                     str(getattr(cookie, "domain", "") or ""),
                     str(getattr(cookie, "path", "/") or "/")): str(cookie.value or "")
                    for cookie in session.cookies
                }
                with session_registry_lock:
                    session_registry.append((session, baseline))
                tls.session = session
            return session

        def worker(pid):
            try:
                edit_url = edit_urls.get(pid, edit_urls.get(str(pid), ""))
                _formcheck, fields, values = self.get_edit_form(
                    pid, resolved_mcode, edit_url=edit_url,
                    session=get_session())
                if not fields and not values:
                    raise RuntimeError("编辑表单为空")
                model_field, price_field = self._detect_product_fields(fields)
                return pid, {
                    "xinghao": values.get(model_field, ""),
                    "jiage": values.get(price_field, "") if price_field else "",
                    "xinghao_field": model_field,
                    "jiage_field": price_field,
                    "mcode": resolved_mcode,
                    "filename": (values.get("filename", "") or
                                 values.get("urlname", "")),
                    "scode": values.get("scode", ""),
                }, ""
            except Exception as exc:
                debug_log(f"[fetch_detail] id={pid} error: {exc}")
                return pid, None, str(exc)

        errors = {}
        done = 0
        worker_limit = max(1, int(max_workers or 1))
        id_iterator = iter(product_ids)
        executor = ThreadPoolExecutor(max_workers=worker_limit)
        pending = {}
        stop_submitting = False

        def submit_one():
            self._check_product_cancelled(cancel_callback)
            try:
                next_pid = next(id_iterator)
            except StopIteration:
                return False
            pending[executor.submit(worker, next_pid)] = next_pid
            return True

        try:
            for _ in range(worker_limit):
                if not submit_one():
                    break
            while pending:
                self._check_product_cancelled(cancel_callback)
                completed, _remaining = wait(
                    pending, timeout=0.05 if cancel_callback else None,
                    return_when=FIRST_COMPLETED)
                if not completed:
                    continue
                completed_count = 0
                for future in completed:
                    pid = pending.pop(future)
                    if future.cancelled():
                        continue
                    completed_count += 1
                    pid, result, error = future.result()
                    if result:
                        results[pid] = result
                    elif error:
                        errors[pid] = error
                        stop_submitting = True
                    done += 1
                    if progress_callback:
                        try:
                            progress_callback(done, total, pid)
                        except Exception:
                            pass
                if stop_submitting:
                    # 已发现表单/字段错误，取消还未开始的 future，
                    # 不再把余下几百个详情任务一次性提交到队列。
                    for future in list(pending):
                        if future.cancel():
                            pending.pop(future, None)
                else:
                    for _ in range(completed_count):
                        if not submit_one():
                            break
        except BaseException:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        # Detail reads use thread-local sessions to avoid sharing a mutable
        # requests CookieJar.  Reconcile their Set-Cookie rotations after
        # the batch settles so a subsequent write or browser-like operation
        # sees the same session state as the parallel GETs.
        try:
            from requests.cookies import create_cookie
            def _cookie_live(cookie):
                expires = getattr(cookie, "expires", None)
                if expires in (None, "", 0):
                    return True
                try:
                    return float(expires) > time.time()
                except (TypeError, ValueError):
                    return False

            parent_jar = self.session.cookies
            for detail_session, baseline in session_registry:
                final_cookies = list(detail_session.cookies)
                final_ids = {
                    (str(cookie.name or ""),
                     str(getattr(cookie, "domain", "") or ""),
                     str(getattr(cookie, "path", "/") or "/"))
                    for cookie in final_cookies
                    if _cookie_live(cookie)
                }
                for identity, old_value in baseline.items():
                    if identity in final_ids:
                        continue
                    for current in list(parent_jar):
                        current_id = (
                            str(current.name or ""),
                            str(getattr(current, "domain", "") or ""),
                            str(getattr(current, "path", "/") or "/"))
                        if (current_id == identity and
                                str(current.value or "") == old_value):
                            try:
                                parent_jar.clear(
                                    domain=str(getattr(current, "domain", "") or ""),
                                    path=str(getattr(current, "path", "/") or "/"),
                                    name=str(current.name or ""))
                            except Exception:
                                pass
                            break
                for cookie in final_cookies:
                    expires = getattr(cookie, "expires", None)
                    if expires not in (None, "", 0):
                        try:
                            if float(expires) <= time.time():
                                continue
                        except (TypeError, ValueError):
                            continue
                    identity = (
                        str(cookie.name or ""),
                        str(getattr(cookie, "domain", "") or ""),
                        str(getattr(cookie, "path", "/") or "/"))
                    old_value = baseline.get(identity)
                    current = next((item for item in parent_jar
                                    if (str(item.name or ""),
                                        str(getattr(item, "domain", "") or ""),
                                        str(getattr(item, "path", "/") or "/")) == identity), None)
                    if (old_value is not None and current is not None and
                            str(current.value or "") not in {
                                old_value, str(cookie.value or "")
                            }):
                        continue
                    record = create_cookie(
                        name=cookie.name, value=cookie.value,
                        domain=cookie.domain, path=cookie.path,
                        secure=bool(getattr(cookie, "secure", False)),
                        expires=getattr(cookie, "expires", None),
                        discard=bool(getattr(cookie, "discard", False)),
                        version=int(getattr(cookie, "version", 0) or 0),
                        rest=dict(getattr(cookie, "_rest", {}) or {}),
                    )
                    record.domain_initial_dot = bool(
                        getattr(cookie, "domain_initial_dot", False))
                    self.session.cookies.set_cookie(record)
        except Exception as exc:
            debug_log(f"[fetch_detail] Cookie轮换回写失败(不影响已读取详情): {exc}")

        if errors:
            samples = "; ".join(
                f"ID {pid}: {message}"
                for pid, message in list(errors.items())[:3])
            raise RuntimeError(
                f"产品详情读取失败（{len(errors)}/{total}）：{samples}")
        # 整批完整后才通知单项回调，避免失败批次留下部分写入。
        if item_callback:
            for pid in product_ids:
                if pid not in results:
                    continue
                try:
                    item_callback(pid, results[pid])
                except Exception:
                    pass
        return results

    def modify_product_fields(self, pid, changes, *,
                              xinghao_field=FIELD_XINGHAO,
                              jiage_field=FIELD_JIAGE, mcode=None,
                              edit_url=None, cancel_callback=None,
                              expected=None):
        """Safely modify explicitly supplied product model/price fields.

        ``changes`` uses logical keys ``xinghao`` and ``jiage``.  Key
        presence, rather than truthiness, expresses intent, so an operator can
        deliberately clear a bad value.  The underlying ``edit_content``
        still reads and submits the complete current form and verifies the
        changed fields, preserving status and other unrelated values.
        """
        self.last_write_result = {}
        cancel_callback = cancel_callback or getattr(
            self, "_active_cancel_callback", None)
        try:
            if callable(cancel_callback):
                cancel_callback()
            if not isinstance(changes, dict):
                return False, "产品修改内容必须是对象"
            unknown = set(changes) - {"xinghao", "jiage"}
            if unknown:
                return False, "不允许修改产品字段: " + ", ".join(sorted(unknown))
            if expected is not None and not isinstance(expected, dict):
                return False, "产品预期值必须是对象"
            expected = dict(expected or {})
            unknown_expected = set(expected) - {"xinghao", "jiage"}
            if unknown_expected:
                return False, ("不允许校验产品字段: " +
                               ", ".join(sorted(unknown_expected)))
            resolved_mcode = self._resolve_operation_mcode(
                mcode=mcode, edit_url=edit_url)
            fields = {}
            if "xinghao" in changes:
                if not xinghao_field:
                    return False, "当前网站产品模型没有型号字段，无法修改型号"
                fields[xinghao_field] = str(changes["xinghao"] if changes["xinghao"] is not None else "")
            if "jiage" in changes:
                if not jiage_field:
                    return False, "当前网站产品模型没有价格字段，无法修改价格"
                fields[jiage_field] = str(changes["jiage"] if changes["jiage"] is not None else "")
            if not fields:
                return False, "无可修改字段"
            kwargs = {"edit_url_hint": edit_url or ""}
            if cancel_callback is not None:
                kwargs["cancel_callback"] = cancel_callback
            expected_fields = {}
            if "xinghao" in expected and xinghao_field:
                expected_fields[xinghao_field] = str(
                    expected["xinghao"] if expected["xinghao"] is not None else "")
            if "jiage" in expected and jiage_field:
                expected_fields[jiage_field] = str(
                    expected["jiage"] if expected["jiage"] is not None else "")
            if expected_fields:
                kwargs["expected_fields"] = expected_fields
            result = self.edit_content(pid, resolved_mcode, fields, **kwargs)
            if callable(cancel_callback):
                cancel_callback()
            return result
        except Cancelled:
            raise
        except Exception as exc:
            return False, str(exc)

    def modify_product_price(self, pid, new_xinghao="", new_jiage="",
                              xinghao_field=FIELD_XINGHAO, jiage_field=FIELD_JIAGE,
                              mcode=None, edit_url=None):
        """Backward-compatible non-empty model/price update helper."""
        changes = {}
        if new_xinghao:
            changes["xinghao"] = new_xinghao
        if new_jiage:
            changes["jiage"] = new_jiage
        return self.modify_product_fields(
            pid, changes, xinghao_field=xinghao_field,
            jiage_field=jiage_field, mcode=mcode, edit_url=edit_url)
