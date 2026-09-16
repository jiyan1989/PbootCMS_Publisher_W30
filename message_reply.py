"""Discover, edit and verify the actual PbootCMS message reply form."""
import hashlib
import json
import re
from copy import deepcopy
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from client_content import (ContentMixin, _response_error_message,
                            _response_success_message, _discover_default_submitter,
                            _form_method_enctype, _submit_content_form)
from client_utils import _is_login_page
from form_controls import describe_form, form_elements, successful_pairs, merge_form_updates, normalize_control_value, validate_control_value
from message_state import message_revision
from message_routes import message_route
from save_verification import TRANSPORT_FIELDS, submission_expectations, compare_saved_fields, wire_values
from exceptions import Cancelled


def reply_url(url, reference, message_id):
    if not re.fullmatch(r'[0-9]+', str(message_id)):
        raise ValueError('留言编号无效')
    return message_route(url, reference, r'/Message/mod/id/' + re.escape(str(message_id)) + r'/?')[0]


def _label(form, element, name):
    if element.get('id'):
        label = form.find('label', attrs={'for': element['id']})
        if label:
            return label.get_text(' ', strip=True)
    group = element.find_parent(class_=re.compile(r'layui-form-item'))
    label = group.find('label') if group else None
    return label.get_text(' ', strip=True) if label else name


def _form_revision(fields, values):
    data = {'values': {key: value for key, value in values.items() if key not in TRANSPORT_FIELDS},
            'fields': [field for field in fields if field['name'] not in TRANSPORT_FIELDS]}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _unchanged_message(message):
    data = deepcopy(message)
    data['fields'] = [field for field in data.get('fields', [])
                      if field.get('key') != 'status' and str(field.get('label', '')).strip().rstrip('：:') not in ('回复内容', '回复')]
    data['extras'] = {key: value for key, value in data.get('extras', {}).items()
                      if key.strip().rstrip('：:') not in ('回复内容', '回复')}
    return message_revision(data, include_status=False)


class MessageReplyMixin:
    @staticmethod
    def _check_cancel(cancel_callback):
        if callable(cancel_callback):
            cancel_callback()

    def _reply_target(self, message_id, cancel_callback=None):
        self._check_cancel(cancel_callback)
        if not str(message_id).isdigit():
            raise ValueError('留言编号无效')
        snapshot = self.fetch_messages(limit=None,
                                       cancel_callback=cancel_callback)
        self._check_cancel(cancel_callback)
        matches = [row for row in snapshot if str(row.get('id')) == str(message_id)]
        if len(matches) != 1:
            raise RuntimeError('无法唯一读取目标留言，请刷新后核对')
        target = matches[0]
        url = target.get('reply_url', '')
        if not url:
            raise RuntimeError('后台没有提供唯一的留言回复入口')
        reply_url(url, self.admin_url, message_id)
        return target

    def _read_reply_form(self, message_id, url, cancel_callback=None):
        visited = set()
        for _ in range(21):
            self._check_cancel(cancel_callback)
            url = reply_url(url, self.admin_url, message_id)
            if url in visited:
                raise RuntimeError('回复表单重定向循环')
            visited.add(url)
            response = self.session.get(url, timeout=25, allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = (getattr(response, 'headers', {}) or {}).get('Location')
            if not location:
                raise RuntimeError('回复表单重定向缺少地址')
            close = getattr(response, 'close', None)
            if callable(close):
                close()
            url = reply_url(urljoin(url, location), self.admin_url, message_id)
        else:
            raise RuntimeError('回复表单重定向过多')
        if not response.ok or _is_login_page(response.text):
            raise RuntimeError('回复表单读取失败或登录已失效')
        resolved = reply_url(getattr(response, 'url', '') or url, self.admin_url, message_id)
        soup = BeautifulSoup(response.text, 'html.parser')
        forms = []
        for form in soup.find_all('form'):
            try:
                action = reply_url(urljoin(resolved, form.get('action') or resolved), self.admin_url, message_id)
            except (ValueError, RuntimeError):
                continue
            if any(element.get('name') == 'recontent' for element in form_elements(form)):
                forms.append((form, action))
        if len(forms) != 1:
            raise RuntimeError('无法唯一确认实际回复表单')
        form, _form_action = forms[0]
        # Use the same submitter/transport discovery as content and dynamic
        # admin forms.  A reply template may override action, method or
        # enctype on its actual save button, and that button's name/value is a
        # successful control in a real browser submission.
        submitter = _discover_default_submitter(form)
        transport = _form_method_enctype(form, submitter)
        try:
            action = reply_url(urljoin(resolved, transport.get('action') or resolved),
                               self.admin_url, message_id)
        except (ValueError, RuntimeError):
            raise RuntimeError('回复表单提交地址不是当前留言的安全入口')
        method = str(transport.get('method') or 'post').lower()
        enctype = str(transport.get('enctype') or
                      'application/x-www-form-urlencoded').lower()
        # The browser honors an explicit GET save form as a query submission;
        # ``_submit_content_form`` already preserves repeated pairs and the
        # real action URL is constrained to this message id above.  Reject
        # only methods outside the HTML form transport set, rather than
        # silently making every reply POST-only.
        if method not in ('get', 'post'):
            raise RuntimeError('回复表单提交方法暂不支持，未发送操作')
        if enctype not in ('application/x-www-form-urlencoded', 'multipart/form-data',
                           'text/plain'):
            raise RuntimeError('回复表单编码暂不支持，未发送操作')
        fields, values = describe_form(form, _label, submitter=submitter)
        if 'id' in values and str(values['id']) != str(message_id):
            raise RuntimeError('回复表单记录ID不匹配')
        if 'formcheck' in values and not values['formcheck']:
            raise RuntimeError('回复表单令牌为空，请重新登录后读取')
        return {'fields': fields, 'values': values,
                'pairs': successful_pairs(form, submitter=submitter),
                'action': action, 'url': resolved, 'method': method,
                'enctype': enctype, 'submitter': submitter,
                'form_novalidate': bool(any(field.get('form_novalidate')
                                            for field in fields)),
                'revision': _form_revision(fields, values)}

    def prepare_message_reply(self, message_id, expected_revision=''):
        target = self._reply_target(message_id)
        current_revision = message_revision(target)
        if expected_revision and expected_revision != current_revision:
            raise RuntimeError('留言已变化，请刷新列表后重新打开回复')
        info = self._read_reply_form(message_id, target['reply_url'])
        fields = [field for field in info['fields'] if field['name'] not in TRANSPORT_FIELDS and field.get('mappable')]
        if not any(field['name'] == 'recontent' for field in fields):
            raise RuntimeError('后台回复内容字段不可编辑')
        return {'message_id': str(message_id), 'fields': fields, 'revision': info['revision'],
                'message_revision': current_revision,
                'method': info.get('method', 'post'),
                'enctype': info.get('enctype', 'application/x-www-form-urlencoded'),
                'form_novalidate': bool(info.get('form_novalidate')),
                'submitter': info.get('submitter'),
                'warnings': ['字段和默认状态来自后台；未修改字段保持原值。保存后核验表单，不代表前台渲染已验收。']}

    def _reply_result(self, outcome, msg, **values):
        attempted = bool(getattr(self, 'last_message_result', {}).get('write_attempted'))
        self.last_message_result = {'outcome': outcome, 'write_attempted': attempted,
            'requires_review': attempted and outcome not in ('verified', 'rejected'), 'retryable': False,
            'verification': values.pop('verification', {})}
        return {'ok': outcome == 'verified', 'msg': msg, **self.last_message_result, **values}

    def save_message_reply(self, message_id, updates, expected_form_revision,
                           expected_message_revision, cancel_callback=None):
        cancel_callback = cancel_callback or getattr(
            self, "_active_cancel_callback", None)
        self.last_message_result = {'write_attempted': False}
        try:
            self._check_cancel(cancel_callback)
            target = self._reply_target(message_id, cancel_callback)
            if not expected_message_revision or expected_message_revision != message_revision(target):
                return self._reply_result('not_sent', '留言内容/回复/状态已变化，请刷新后重新编辑')
            info = self._read_reply_form(message_id, target['reply_url'],
                                         cancel_callback)
            if not expected_form_revision or expected_form_revision != info['revision']:
                return self._reply_result('not_sent', '回复表单或字段值已变化，请重新打开回复')
            editable = {field['name']: field for field in info['fields']
                        if field.get('mappable') and field['name'] not in TRANSPORT_FIELDS}
            if not isinstance(updates, dict) or set(updates) - set(editable):
                return self._reply_result('not_sent', '包含不允许修改的回复字段')
            changes = {}
            for name, value in updates.items():
                if isinstance(value, dict) or isinstance(value, list) and any(isinstance(v, (list, dict)) for v in value):
                    return self._reply_result('not_sent', '回复字段值格式无效')
                field = editable[name]
                value = normalize_control_value(value, field)
                error = '' if info.get('form_novalidate') else validate_control_value(value, field)
                if error:
                    return self._reply_result('not_sent', f'{field.get("label", name)}：{error}')
                if value != normalize_control_value(field.get('value'), field):
                    changes[name] = value
            for name, field in editable.items():
                value = changes.get(name, normalize_control_value(field.get('value'), field))
                error = '' if info.get('form_novalidate') else validate_control_value(value, field)
                if error:
                    return self._reply_result('not_sent', f'{field.get("label", name)}：{error}')
            if not changes:
                return self._reply_result('verified', '回复字段没有变化，未发送请求', changed=False)
            data = merge_form_updates(info['values'], info['fields'], changes)
            # Keep untouched successful controls in original DOM order, including
            # interleaved repeated names. Changed groups retain their first slot.
            pairs, inserted = [], set()
            for name, value in info['pairs']:
                if name not in changes:
                    pairs.append((name, value))
                elif name not in inserted:
                    pairs.extend((name, v) for v in wire_values(data.get(name, [])))
                    inserted.add(name)
            for name in changes.keys() - inserted:
                pairs.extend((name, v) for v in wire_values(data.get(name, [])))
            expected, absent = submission_expectations(data, info['fields'])
            self._check_cancel(cancel_callback)
            self.last_message_result = {'write_attempted': True}
            response = _submit_content_form(
                self, info.get('method', 'post'), info['action'], pairs,
                info['fields'], info.get('enctype', ''),
                {'Referer': info['url'],
                 'Origin': f'{urlsplit(info["url"]).scheme}://{urlsplit(info["url"]).netloc}'})
            if not 200 <= response.status_code < 300:
                # A completed 4xx (except 408) is an explicit server-side
                # rejection.  Timeout/5xx remain unknown because the body may
                # already have reached the server and must not be replayed.
                outcome = ('rejected' if 400 <= response.status_code < 500
                           and response.status_code != 408 else 'unknown')
                return self._reply_result(
                    outcome,
                    (f'回复请求被后台拒绝，HTTP {response.status_code}'
                     if outcome == 'rejected' else
                     f'回复请求已发送，HTTP {response.status_code}，请先核对后台'),
                    status_code=response.status_code)
            error = _response_error_message(response.text)
            if error:
                return self._reply_result('rejected', error)
            try:
                payload = response.json()
            except (AttributeError, ValueError, TypeError):
                payload = None
            reported, message = ContentMixin._json_write_outcome(payload, '后台报告成功', '后台拒绝回复')
            if reported is False:
                return self._reply_result('rejected', message)
            reported = bool(reported or _response_success_message(response.text))
            self._check_cancel(cancel_callback)
            after_form = self._read_reply_form(message_id, target['reply_url'],
                                               cancel_callback)
            report = compare_saved_fields(expected, after_form['values'], after_form['fields'], absent)
            if report['status'] != 'verified':
                return self._reply_result('different' if report['status'] == 'different' else 'reported_unverified' if reported else 'unknown',
                    '回复结果待核对：回读字段存在差异或无法完整核验，请勿直接重复提交', verification=report)
            snapshot = self.fetch_messages(limit=None,
                                           cancel_callback=cancel_callback)
            after = next((row for row in snapshot if str(row.get('id')) == str(message_id)), None)
            if after is None or _unchanged_message(after) != _unchanged_message(target):
                return self._reply_result('different', '回复字段已回读，但原留言内容变化或无法读取，请核对后台', verification=report)
            return self._reply_result('verified', '留言回复已保存并回读核验全部表单业务字段', changed=True,
                verification=report, messages=list(snapshot), complete=snapshot.complete, pages=snapshot.pages, warning=snapshot.warning)
        except Cancelled:
            attempted = bool(self.last_message_result.get('write_attempted'))
            return self._reply_result(
                'cancelled',
                '回复操作已取消；' +
                ('请求可能已发送，请先核对后台，勿直接重复提交'
                 if attempted else '未发送保存请求'),
                cancelled=True)
        except Exception:
            attempted = bool(self.last_message_result.get('write_attempted'))
            return self._reply_result('unknown' if attempted else 'not_sent',
                '回复请求已发送但无法完整核验，请先核对后台，勿直接重复提交' if attempted else '回复准备失败，未发送保存请求；请重新读取表单')
