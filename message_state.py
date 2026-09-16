"""Message status evidence, without conflating visibility with processing."""
import hashlib
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit


LABELS = {
    '显示': ('visible', '前端显示'), '前端显示': ('visible', '前端显示'),
    '隐藏': ('hidden', '前端隐藏'), '前端隐藏': ('hidden', '前端隐藏'),
    '已处理': ('processed', '已处理'), '未处理': ('unprocessed', '未处理'),
    '待处理': ('unprocessed', '待处理'),
    '已审核': ('approved', '已审核'), '未审核': ('unapproved', '未审核'),
    '开启': ('enabled', '开启'), '关闭': ('disabled', '关闭'),
    '0': ('value0', '状态值 0'), '1': ('value1', '状态值 1'),
}


def target_value(url):
    # Display only; request authorization still requires message_routes.
    try:
        parsed = urlsplit(str(url or ''))
        pairs = parse_qsl(parsed.query, keep_blank_values=True, errors='strict')
        params = dict(pairs)
        if len(params) != len(pairs):
            return ''
        route = params.get('p', unquote(parsed.path, errors='strict'))
        match = re.search(r'/Message/mod/id/[0-9]+/field/status/value/([01])/?$', route, re.I)
        return match.group(1) if match else ''
    except ValueError:
        return ''


def native_status(anchor):
    """Require matching native icon, action tooltip and explicit target value."""
    target = target_value(anchor.get('href', ''))
    for icon in anchor.select('i.fa-toggle-on, i.fa-toggle-off'):
        classes = icon.get('class', [])
        current = '1' if 'fa-toggle-on' in classes else '0'
        title = str(icon.get('title') or anchor.get('title') or '').strip()
        expected_title = '点击前端隐藏' if current == '1' else '点击前端显示'
        if target == ('0' if current == '1' else '1') and title == expected_title:
            return {'kind': 'visible' if current == '1' else 'hidden',
                    'label': '前端显示' if current == '1' else '前端隐藏',
                    'value': current, 'target_value': target,
                    'target_label': '前端显示' if target == '1' else '前端隐藏',
                    'source': 'native_icon_and_tooltip', 'confirmed': True}
    return {}


def finalize_status(message):
    info = dict(message.pop('_native_status', {}) or {})
    text = str(message.get('status', '') or '').strip()
    kind, label = LABELS.get(text, ('unknown', text))
    if info and text and kind not in (info['kind'], 'value' + info['value']):
        info = {'kind': 'unknown', 'label': text, 'value': '', 'source': 'conflicting_status', 'confirmed': False}
    if not info:
        info = {'kind': kind, 'label': label or '状态未标明',
                'value': text if text in ('0', '1') else '',
                'source': 'field' if text else 'unavailable', 'confirmed': text in ('0', '1')}
    info.setdefault('target_value', target_value(message.get('status_url')))
    info.setdefault('target_label', '状态值 ' + info['target_value'] if info['target_value'] else '')
    message['status_info'] = info
    if not message.get('status') and info.get('confirmed'):
        message['status'] = info['label']
    message['revision'] = message_revision(message)


def message_revision(message, include_status=True):
    keys = ('id', 'name', 'email', 'phone', 'industry', 'city', 'product', 'content', 'time', 'visitor', 'extras')
    data = {key: message.get(key, '') for key in keys}
    data['fields'] = [{key: field.get(key) for key in ('label', 'value', 'key', 'html')}
                      for field in message.get('fields', []) if include_status or field.get('key') != 'status']
    if include_status:
        info = message.get('status_info', {})
        data['status'] = message.get('status', '')
        data['state'] = {key: info.get(key) for key in ('kind', 'value', 'source', 'target_value')}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
