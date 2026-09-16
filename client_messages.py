"""Focused PbootCMS MessageMixin service."""
import csv
import io
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from datetime import datetime
from pathlib import Path
from urllib.parse import (parse_qsl, unquote, urlencode, urljoin,
                          urlparse, urlsplit, urlunsplit)
import requests
from bs4 import BeautifulSoup
try:
    from PIL import Image
except ImportError:
    Image = None
from constants import TIMEOUT_NORMAL, TIMEOUT_UPLOAD, MCODE_ORDER, FIELD_XINGHAO, FIELD_JIAGE
from exceptions import NetworkError, Cancelled
from logger import debug_log, debug_log_v
from request import http_request
from client_utils import get_base_dir, _is_login_page
from message_fields import MessageFieldSource, csv_safe_cell
from message_pagination import get_list_page, list_url, next_page, explicit_empty, page_number
from message_state import native_status, finalize_status, message_revision, target_value
from message_reply import MessageReplyMixin, reply_url
from message_routes import message_record_route, message_action_url
from http_transport import permitted_transition
from form_controls import control_type, form_elements, is_disabled, successful_pairs


_MESSAGE_ERROR_RE = re.compile(
    r"失败|错误|异常|无权限|权限不足|未授权|被拒绝|请先登录|登录.*失效|"
    r"\b(?:error|failed|failure|invalid|forbidden|unauthorized|denied)\b",
    re.I,
)


class MessageSnapshot(list):
    """Parsed messages plus pagination/error evidence for the UI."""

    def __init__(self, values=(), *, complete=False, pages=0, warning="",
                 server_filter=None, server_filter_query=None,
                 filter_warning="", native_url=""):
        super().__init__(values)
        self.complete = bool(complete)
        self.pages = int(pages or 0)
        self.warning = str(warning or "")
        # ``None`` means no server-side filter was requested.  A boolean value
        # is used when a caller explicitly requested one, so the UI can tell
        # a real backend search from its local-snapshot fallback.
        self.server_filter = server_filter
        self.server_filter_query = dict(server_filter_query or {})
        self.filter_warning = str(filter_warning or "")
        # When the page owns a POST/JavaScript search form, a requests-only
        # adapter must not invent a GET equivalent.  Preserve the exact
        # same-origin list URL so the UI can hand the operation to the real
        # authenticated browser instead of silently presenting a local-only
        # result as if it were server-filtered.
        self.native_url = str(native_url or "")


_MESSAGE_RESERVED_FILTER_NAMES = {
    'p', 'page', 'pageno', 'page_no', 'pageindex', 'page_index',
    'pagenum', 'page_num', 'current', 'currentpage', 'current_page',
    'pagesize', 'page_size', 'perpage', 'per_page', 'limit', 'offset',
    'formcheck', '_token', '_csrf', 'csrf_token', 'csrfmiddlewaretoken',
}


def _message_form_route(url, reference, hidden_route=""):
    """Return the route portion of a candidate GET search form.

    A number of Pboot themes use an empty ``action`` plus a hidden ``p``
    control.  Resolve that form without accepting arbitrary paths or a
    cross-origin action.  The route is checked again by ``list_url`` after
    the discovered filter names are known.
    """
    if not _same_http_origin(url, reference):
        return ""
    parsed, base = urlsplit(str(url)), urlsplit(str(reference))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_route = next((value for key, value in pairs
                        if str(key).lower() == 'p'), '')
    route = unquote(str(query_route or hidden_route or ""), errors='strict')
    if route:
        return route if route.startswith('/') else '/' + route
    path = parsed.path or '/'
    entry = (base.path or '/').rstrip('/')
    if entry and path.startswith(entry + '/'):
        return path[len(entry):] or '/'
    if path.rstrip('/') == entry.rstrip('/'):
        return '/'
    return ""


def _control_label(form, element):
    eid = str(element.get('id', '') or '')
    if eid:
        label = form.find('label', {'for': eid})
        if label:
            return label.get_text(' ', strip=True)
    parent = element.find_parent(class_=re.compile(
        r'layui-form-item|form-item|form-group|search|filter', re.I))
    if parent:
        label = parent.find(['label', 'th', 'dt', 'span'],
                            class_=re.compile(r'label|title|name', re.I))
        if label:
            return label.get_text(' ', strip=True)
        # Keep the nearby item text short; it is only used as search-field
        # evidence and never sent to the server.
        return parent.get_text(' ', strip=True)[:160]
    return str(element.get('placeholder', '') or '')


def _discover_message_search_form(soup, response_url, reference):
    """Discover one explicit same-origin GET search form on Message/index.

    The returned control names become an allow-list for pagination URLs.  We
    intentionally do not guess POST/JavaScript searches: those remain local
    filtering (or can be opened in the native webpage).
    """
    for form in soup.find_all('form'):
        method = str(form.get('method', 'get') or 'get').strip().lower()
        if method != 'get':
            continue
        controls = []
        hidden_route = ''
        for element in form_elements(form):
            name = str(element.get('name', '') or '').strip()
            kind = control_type(element)
            if name.lower() == 'p' and kind == 'hidden':
                hidden_route = str(element.get('value', '') or '').strip()
            if (not name or is_disabled(element) or element.name == 'button' or
                    kind in ('submit', 'reset', 'image', 'file', 'button')):
                continue
            if name.lower() in _MESSAGE_RESERVED_FILTER_NAMES:
                continue
            controls.append((element, name, kind, _control_label(form, element)))
        action = urljoin(response_url, str(form.get('action', '') or ''))
        if not form.get('action'):
            action = response_url
        try:
            route = _message_form_route(action, reference, hidden_route)
        except (TypeError, ValueError):
            continue
        if not re.fullmatch(r'/Message/index(?:/page/[1-9][0-9]*)?/?', route, re.I):
            continue
        if not controls:
            continue
        evidence = ' '.join([str(form.get('id', '') or ''),
                             str(form.get('class', '') or ''),
                             form.get_text(' ', strip=True)[:400]])
        # A GET form on Message/index with a text/select control is generally
        # a search form.  Require either an explicit search/filter cue or a
        # field name that is conventional enough to avoid treating a theme's
        # unrelated language selector as a filter.
        cue = re.search(r'搜索|筛选|过滤|关键词|关键字|search|filter|keyword|query',
                        evidence, re.I)
        likely = [item for item in controls if (
            item[2] in ('text', 'search', 'email', 'tel', 'number', 'select',
                        'checkbox', 'radio') or
            re.search(r'search|filter|keyword|query|status|state|name|email|phone|content|message|q$',
                      item[1], re.I))]
        if not likely or (not cue and not any(re.search(
                r'search|filter|keyword|query|status|state', item[1], re.I)
                for item in likely)):
            continue
        names = []
        for _element, name, _kind, _label in likely:
            if name.lower() not in {str(item).lower() for item in names}:
                names.append(name)
        try:
            # Validate the action itself before returning it.  The names are
            # explicitly discovered above, therefore they are safe allow-list
            # additions but still cannot alter the Message route.
            list_url(action + (('&' if '?' in action else '?') +
                               urlencode({'p': route}) if 'p=' not in action.lower() and
                               not route.startswith('/Message/index/page/') else ''),
                     reference, filter_params=names)
        except Exception:
            # A malformed query/action is not a usable search form.  Even a
            # path-style action is revalidated with a synthetic ``p`` above,
            # so silently accepting an unknown query here would weaken the
            # list-route guard.
            continue
        by_lower = {name.lower(): (element, name, kind, label)
                    for element, name, kind, label in likely}
        keyword = next((item for key, item in by_lower.items() if re.search(
            r'keyword|search|query|q$|name|email|phone|content|message', key, re.I)), None)
        status = next((item for key, item in by_lower.items() if re.search(
            r'status|state|处理|审核|显示', key, re.I)), None)
        return {'action': action, 'route': route, 'names': names,
                'keyword': keyword, 'status': status}
    return None


def _status_option_value(element, wanted):
    """Map the UI's normalized status kind to a native option value."""
    wanted = str(wanted or '').strip().lower()
    if not wanted or wanted == 'all' or element is None:
        return ''
    options = element.find_all('option') if getattr(element, 'name', '') == 'select' else []
    patterns = {
        'visible': r'前端显示|^显示$|visible|开启|^1$',
        'hidden': r'前端隐藏|^隐藏$|hidden|关闭|^0$',
        'processed': r'已处理|处理完成|done|processed',
        'unprocessed': r'未处理|待处理|unprocessed',
        'approved': r'已审核|审核通过|approved',
        'unapproved': r'未审核|待审核|unapproved',
        'enabled': r'开启|启用|enabled|^1$',
        'disabled': r'关闭|停用|disabled|^0$',
        'value0': r'^0$|状态值\s*0',
        'value1': r'^1$|状态值\s*1',
    }
    pattern = patterns.get(wanted, '')
    if not pattern:
        return ''
    matches = []
    for option in options:
        value = str(option.get('value', '') or '')
        label = option.get_text(' ', strip=True)
        if re.search(pattern, label, re.I) or re.search(pattern, value, re.I):
            matches.append(value)
    return matches[0] if len(matches) == 1 else ''


def _build_message_filter_url(form_info, keyword='', status=''):
    if not form_info:
        return '', {}, ''
    pairs = parse_qsl(urlsplit(form_info['action']).query,
                      keep_blank_values=True)
    existing = {str(key).lower(): value for key, value in pairs}
    chosen = {}
    keyword = str(keyword or '')
    if keyword and form_info.get('keyword'):
        name = form_info['keyword'][1]
        chosen[name] = keyword
    if status and str(status).lower() != 'all' and form_info.get('status'):
        element = form_info['status'][0]
        value = _status_option_value(element, status)
        if value:
            chosen[form_info['status'][1]] = value
    if not chosen:
        return '', {}, '当前后台没有可安全映射的关键词/状态筛选字段'
    for name, value in chosen.items():
        pairs = [(key, item) for key, item in pairs
                 if str(key).lower() != str(name).lower()]
        pairs.append((name, value))
    query = urlencode(pairs, doseq=True)
    parsed = urlsplit(form_info['action'])
    result = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ''))
    return result, chosen, ''


def _carry_message_filter(url, expected):
    """Keep a server search bound while following a bare next-page link."""
    if not expected:
        return url
    parsed = urlsplit(str(url or ''))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    existing = {}
    for key, value in pairs:
        low = str(key).lower()
        if low in existing and existing[low] != value:
            raise RuntimeError('留言分页筛选参数重复或发生变化')
        existing[low] = value
    for key, value in expected.items():
        low = str(key).lower()
        if low in existing and existing[low] != str(value):
            raise RuntimeError('留言分页筛选条件发生变化，未确认完整')
        if low not in existing:
            pairs.append((str(key), str(value)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(pairs, doseq=True), ''))


def _same_http_origin(left, right):
    try:
        return permitted_transition(str(right or ""), str(left or ""))
    except Exception:
        return False


def _message_error(response):
    text = str(getattr(response, "text", "") or "")
    if _is_login_page(text):
        return "登录会话已失效，请重新登录"
    soup = BeautifulSoup(text, "html.parser")
    for element in soup.find_all(class_=re.compile(
            r"danger|error|layui-layer-content", re.I)):
        message = element.get_text(" ", strip=True)
        if message and _MESSAGE_ERROR_RE.search(message):
            return message[:300]
    plain = soup.get_text(" ", strip=True)
    if len(plain) <= 500 and _MESSAGE_ERROR_RE.search(plain):
        return plain[:300]
    return ""


class MessageMixin(MessageReplyMixin):
    def fetch_messages(self, limit=50, cancel_callback=None, *, keyword="",
                       status=""):
        """获取留言列表"""
        if limit is not None:
            limit = int(limit)
            if limit < 1:
                raise ValueError('留言读取条数必须为正数，全部读取请传None')
        messages = []
        seen = set()
        pages = 0
        warning = ""
        filter_warning = ""
        server_filter = None
        server_filter_query = {}
        native_url = ""
        complete = False
        current_url = self._url("Message/index")
        filter_names = ()
        visited_urls = set()
        try:
            # Search controls are discovered from the live Message/index page,
            # never guessed from a desktop field name.  Only an explicit
            # same-origin GET form can be promoted to server-side filtering;
            # POST/JS filters remain ordinary local snapshot filters.
            if str(keyword or '').strip() or (str(status or '').strip() and
                                               str(status).lower() != 'all'):
                probe_url = list_url(current_url, self.admin_url)
                native_url = probe_url
                try:
                    probe = get_list_page(self.session, probe_url,
                                          self.admin_url, cancel_callback)
                    if probe is not None and getattr(probe, 'ok', False):
                        info = _discover_message_search_form(
                            BeautifulSoup(probe.text, 'html.parser'),
                            getattr(probe, 'url', '') or probe_url,
                            self.admin_url)
                        if info:
                            filtered_url, server_filter_query, reason = (
                                _build_message_filter_url(info, keyword, status))
                            if filtered_url:
                                # Validate all discovered filter names on the
                                # exact target before making the filtered GET.
                                list_url(filtered_url, self.admin_url,
                                         filter_params=info['names'])
                                current_url = filtered_url
                                native_url = filtered_url
                                filter_names = tuple(info['names'])
                                server_filter = True
                            else:
                                filter_warning = reason
                                server_filter = False
                        else:
                            filter_warning = '未发现明确的只读留言筛选表单，已使用本地筛选'
                            server_filter = False
                    else:
                        filter_warning = '留言筛选预检失败，已使用本地筛选'
                        server_filter = False
                except Exception as exc:
                    debug_log(f"[fetch_messages] server filter unavailable: {exc}")
                    filter_warning = '留言筛选无法安全交给后台，已使用本地筛选'
                    server_filter = False
            while True:
                current_url = list_url(current_url, self.admin_url,
                                       filter_params=filter_names)
                if page_number(current_url) != pages + 1:
                    raise RuntimeError('留言分页跳过页码，未确认完整')
                if current_url in visited_urls:
                    raise RuntimeError('留言分页地址重复，列表未确认完整')
                visited_urls.add(current_url)
                resp = get_list_page(self.session, current_url, self.admin_url,
                                     cancel_callback, filter_params=filter_names)
                if resp is None or not resp.ok:
                    raise RuntimeError(
                        f"留言列表请求失败（HTTP {getattr(resp, 'status_code', '?')}）")
                if _is_login_page(resp.text):
                    raise RuntimeError("登录会话已失效，请重新登录")
                pages += 1
                soup = BeautifulSoup(resp.text, "html.parser")
                field_source = MessageFieldSource(resp.text)
                parsed_cells = set()
                page_count = 0
                page_rows = 0
                overlapping = False
                truncated = False
                debug_log(f"[fetch_messages] page={pages}, status={resp.status_code}")
                # 必须用 table tr 才能找到嵌套表格内的行
                rows = soup.select("table tr")
                current_msg = None

                def append_current():
                    nonlocal page_count, current_msg, page_rows, overlapping, truncated
                    if not current_msg:
                        return
                    finalize_status(current_msg)
                    identity = str(current_msg.get("id", "") or "").strip()
                    if not identity.isdigit() or not current_msg.get('fields'):
                        raise RuntimeError('留言记录ID或字段结构无法确认，列表未确认完整')
                    page_rows += 1
                    if identity in seen:
                        overlapping = True
                    elif limit is not None and len(messages) >= limit:
                        truncated = True
                    else:
                        seen.add(identity)
                        messages.append(current_msg)
                        page_count += 1
                    current_msg = None

                for row in rows:
                    if cancel_callback:
                        cancel_callback()
                    if any(id(parent) in parsed_cells for parent in row.parents):
                        continue
                    cb = row.find("input", {"name": "checkbox"})
                    if cb and cb.find_parent('tr') is not row:
                        cb = None
                    native_ids = set()
                    if not cb and row.find('th', recursive=False) and not row.find('td', recursive=False):
                        for anchor in row.find_all('a', href=True):
                            if not _same_http_origin(urljoin(current_url, anchor['href']), self.admin_url):
                                continue
                            try:
                                _, record_id, _ = message_record_route(urljoin(current_url, anchor['href']), self.admin_url)
                                native_ids.add(record_id)
                            except (ValueError, RuntimeError, NetworkError):
                                continue
                    if len(native_ids) > 1:
                        raise RuntimeError('留言表头包含多个记录ID，无法确认完整列表')
                    if cb or len(native_ids) == 1:
                        append_current()
                        message_id = str(cb.get("value", "") or "").strip() if cb else next(iter(native_ids))
                        current_msg = {"id": message_id, "name": "", "email": "", "phone": "",
                                       "contact": "", "industry": "", "city": "", "product": "",
                                       "content": "", "time": "", "visitor": "", "status": "",
                                       "extras": {}, "fields": [], "status_url": "", "delete_url": "", "reply_url": ""}
                        reference = getattr(resp, "url", "") or current_url
                        status_urls = set()
                        delete_urls = set()
                        reply_urls = set()
                        for anchor in row.find_all("a", href=True):
                            href = str(anchor.get("href", "") or "")
                            candidate = urljoin(reference, href)
                            if not _same_http_origin(candidate, reference):
                                continue
                            try:
                                reply_urls.add(reply_url(candidate, self.admin_url, message_id))
                            except (ValueError, RuntimeError, NetworkError):
                                pass
                            try:
                                clean, record_id, route_action = message_record_route(candidate, self.admin_url)
                            except (ValueError, RuntimeError, NetworkError):
                                continue
                            if record_id != message_id:
                                continue
                            if route_action == 'delete':
                                delete_urls.add(clean)
                            elif route_action == 'status':
                                status_urls.add(clean)
                                current_msg["status_url"] = clean
                                current_msg['_native_status'] = native_status(anchor)
                        if len(delete_urls) == 1:
                            current_msg['delete_url'] = next(iter(delete_urls))
                        if len(status_urls) > 1:
                            current_msg['status_url'] = ''
                            current_msg['_native_status'] = {
                                'kind': 'unknown', 'label': '状态操作不唯一', 'confirmed': False,
                                'value': '', 'source': 'ambiguous_actions'}
                        if len(reply_urls) == 1:
                            current_msg['reply_url'] = next(iter(reply_urls))
                        continue
                    if not current_msg:
                        continue
                    th = row.find("th", recursive=False)
                    td = row.find("td", recursive=False)
                    if not th or not td:
                        continue
                    parsed_cells.add(id(td))
                    field = field_source.field(th, td)
                    previous_keys = {item['key'] for item in current_msg['fields']}
                    current_msg['fields'].append(field)
                    key, value = field['key'], field['value']
                    if key and key not in previous_keys:
                        current_msg[key] = value
                    elif not key:
                        current_msg['extras'].setdefault(field['label'], value)
                    current_msg['contact'] = current_msg['email'] or current_msg['phone']
                append_current()
                debug_log(f"[fetch_messages] page={pages}, parsed={page_count}, total={len(messages)}")
                next_anchor = next_page(soup, pages)
                if overlapping:
                    raise RuntimeError('留言分页出现重复ID，可能列表变动或分页失效，未确认完整')
                if not page_rows and (pages != 1 or next_anchor is not None or not explicit_empty(soup)):
                    raise RuntimeError('未取得明确空列表证据，不能将空白或异常页当作加载完毕')
                if truncated or (next_anchor is not None and limit is not None and len(messages) >= limit):
                    warning = f'已达到本次 {limit} 条读取范围，尚未加载全部留言'
                    break
                if next_anchor is None:
                    complete = True
                    break
                next_url = urljoin(getattr(resp, "url", "") or current_url,
                                   str(next_anchor.get("href", "") or ""))
                if server_filter:
                    next_url = _carry_message_filter(next_url,
                                                     server_filter_query)
                current_url = list_url(next_url, self.admin_url,
                                       filter_params=filter_names)
                time.sleep(0.5)  # 翻页间隔，避免触发防火墙
            debug_log(f"[fetch_messages] done: {len(messages)}")
        except Cancelled:
            raise
        except Exception as e:
            debug_log(f"[fetch_messages] error: {e}")
            if not messages:
                raise
            warning = f"仅加载到部分留言：{e}"
        if filter_warning:
            warning = '；'.join(filter(None, [warning, filter_warning]))
        return MessageSnapshot(messages, complete=complete, pages=pages,
                               warning=warning, server_filter=server_filter,
                               server_filter_query=server_filter_query,
                               filter_warning=filter_warning,
                               native_url=native_url)

    def message_action(self, message_id, action, expected=None,
                       cancel_callback=None):
        """Execute only a freshly discovered real status/delete action."""
        self.last_message_result = {'outcome': 'not_sent', 'write_attempted': False,
                                    'requires_review': False, 'retryable': False}
        cancel_callback = cancel_callback or getattr(
            self, "_active_cancel_callback", None)
        if callable(cancel_callback):
            cancel_callback()
        message_id = str(message_id or "").strip()
        action = str(action or "").strip().lower()
        if not message_id.isdigit():
            raise ValueError("留言编号无效")
        if action not in ("status", "delete"):
            raise ValueError("不支持的留言操作")
        before = self.fetch_messages(limit=None,
                                     cancel_callback=cancel_callback)
        if callable(cancel_callback):
            cancel_callback()
        target = next((item for item in before
                       if str(item.get("id", "")) == message_id), None)
        if not target:
            raise RuntimeError("目标留言已不存在或不在可读取范围内，请刷新后重试")
        expected = dict(expected or {})
        if 'revision' in expected and str(expected['revision']) != message_revision(target):
            raise RuntimeError('留言内容、回复或状态已变化，请刷新后重新确认')
        for key in ("name", "time"):
            supplied = str(expected.get(key, "") or "")
            if key in expected and supplied != str(target.get(key, "") or ""):
                raise RuntimeError("留言内容已变化，请刷新后重新确认")
        url_key = "status_url" if action == "status" else "delete_url"
        url = str(target.get(url_key, "") or "")
        try:
            url = message_action_url(url, self.admin_url, message_id, action)
        except (ValueError, RuntimeError, NetworkError):
            label = "状态切换" if action == "status" else "删除"
            raise RuntimeError(f"后台未提供该留言的安全{label}入口") from None
        requested_status = target_value(url) if action == 'status' else ''
        if action == 'status' and not requested_status:
            raise RuntimeError('无法确认后台状态目标值，未发送操作')
        self.last_message_result = {'outcome': 'unknown', 'write_attempted': True,
                                    'requires_review': True, 'retryable': False}
        if callable(cancel_callback):
            cancel_callback()
        response = self._request(
            "GET", url, timeout=30,
            headers={"Referer": self._url("Message/index")})
        if not getattr(response, "ok", False):
            status = int(getattr(response, "status_code", 0) or 0)
            outcome = "rejected" if 400 <= status < 500 and status != 408 else "unknown"
            self.last_message_result.update(
                outcome=outcome, requires_review=(outcome != "rejected"),
                retryable=False, status_code=status)
            raise RuntimeError(
                f"留言操作失败（HTTP {getattr(response, 'status_code', '?')}）")
        error = _message_error(response)
        if error:
            self.last_message_result.update(
                outcome="rejected", requires_review=False, retryable=False,
                status_code=int(getattr(response, "status_code", 0) or 0),
                message=error)
            raise RuntimeError(error)
        if callable(cancel_callback):
            cancel_callback()
        after = self.fetch_messages(limit=None,
                                    cancel_callback=cancel_callback)
        updated = next((item for item in after
                        if str(item.get("id", "")) == message_id), None)
        if action == "delete":
            if not getattr(after, 'complete', False):
                raise RuntimeError('删除结果待核对：回读列表不完整，目标缺席不能证明删除成功，请勿重复点击')
            if updated:
                raise RuntimeError("后台响应后留言仍然存在，请核对后再操作，不要重复点击")
            message = "留言删除成功"
        else:
            if not updated:
                raise RuntimeError("状态操作后无法回读目标留言，请到后台核对，不要重复点击")
            state = updated.get('status_info', {})
            if not state.get('confirmed') or state.get('value') != requested_status:
                raise RuntimeError('状态结果待核对：回读未证明已达到本次目标值，不以链接变化当作成功')
            if message_revision(updated, False) != message_revision(target, False):
                raise RuntimeError('状态结果待核对：留言其他内容同时变化，请检查后台')
            message = "留言状态修改成功"
        self.last_message_result.update(outcome='verified', requires_review=False)
        return {
            **self.last_message_result,
            "msg": message,
            "messages": list(after),
            "count": len(after),
            "complete": bool(getattr(after, "complete", False)),
            "pages": int(getattr(after, "pages", 0) or 0),
            "warning": str(getattr(after, "warning", "") or ""),
        }

    def export_messages_csv(self, message_ids=None, limit=None):
        """Read a fresh snapshot and export selected/all messages to UTF-8 CSV."""
        snapshot = self.fetch_messages(limit=limit)
        wanted = {str(item or "") for item in (message_ids or []) if str(item or "")}
        rows = [item for item in snapshot
                if not wanted or str(item.get("id", "")) in wanted]
        folder = Path(get_base_dir()) / "message_exports"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        site_label = re.sub(r'[^A-Za-z0-9_-]', '_', str(getattr(self, 'site_key', '') or 'site'))[:20]
        path = folder / f"messages_{site_label}_{stamp}_{uuid.uuid4().hex}.csv"
        with path.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ID", "时间", "姓名", "邮箱", "电话", "行业", "城市",
                             "需求产品", "状态", "访客", "留言内容", "其他字段", "全部字段（有序原值JSON）"])
            for item in rows:
                writer.writerow([csv_safe_cell(value) for value in [
                    item.get("id", ""), item.get("time", ""), item.get("name", ""),
                    item.get("email", ""), item.get("phone", ""),
                    item.get("industry", ""), item.get("city", ""),
                    item.get("product", ""), item.get("status", ""),
                    item.get("visitor", ""), item.get("content", ""),
                    json.dumps(item.get("extras", {}), ensure_ascii=False),
                    json.dumps(item.get("fields", []), ensure_ascii=False),
                ]])
        warnings = [str(getattr(snapshot, "warning", "") or "")]
        if not getattr(snapshot, 'complete', False):
            warnings.append('本次列表未确认完整，导出仅包含已读取的留言')
        missing = wanted - {str(item.get('id', '')) for item in rows}
        if missing:
            warnings.append(f'有 {len(missing)} 条选中留言未读取到，未包含在文件中')
        warnings.append('公式风险文本列已加单引号；JSON列保留已读取字段的原值、重复项和源码证据（如有）')
        return str(path), len(rows), '；'.join(filter(None, warnings))
