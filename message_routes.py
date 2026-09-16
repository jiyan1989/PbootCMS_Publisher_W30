"""Unambiguous Message routes bound to the configured admin entry point."""
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from http_transport import permitted_transition


def _path(value):
    # Decode ordinary URL-encoded names (including Unicode directories), but
    # never let decoding introduce routing separators or normalization steps.
    if re.search(r'%(?![0-9a-f]{2})|%(?:2f|5c|2e|3f|23|3b|25|26|3d)', value, re.I):
        raise ValueError('留言地址路径编码含歧义')
    decoded = unquote(value, errors='strict')
    if (any(ord(char) < 33 or ord(char) == 127 for char in decoded)
            or any(char in decoded for char in '\\;?#%')
            or '//' in decoded or any(part in ('.', '..') for part in decoded.split('/'))):
        raise ValueError('留言地址路径无法唯一解析')
    return decoded


def message_route(url, reference, pattern, *, pagination=False, token_params=False,
                  filter_params=()):
    """Return URL/route/parameters only when both routing channels agree.

    reference is the configured admin entry, not an untrusted discovered URL.
    Supports query-p and PATH_INFO routes, subdirectories and renamed entries.
    Unknown routing/encoding conventions require an explicit future adapter.
    """
    # Match the browser/transport redirect boundary: an authenticated HTTP
    # entry may canonicalize to HTTPS on the same host's default ports, while
    # HTTPS→HTTP, non-default ports and cross-host links remain forbidden.
    if not permitted_transition(reference, url):
        raise ValueError('留言地址跨来源')
    parsed, base = urlsplit(url), urlsplit(reference)
    path, entry = _path(parsed.path), _path(base.path).rstrip('/')
    if ';' in parsed.query:
        raise ValueError('留言地址含歧义参数分隔符')
    pairs = parse_qsl(parsed.query, keep_blank_values=True, errors='strict')
    params = dict(pairs)
    if len(params) != len(pairs) or len({str(key).lower() for key, _ in pairs}) != len(pairs):
        raise ValueError('留言地址参数重复')
    allowed = {'p'}
    # Search controls are not globally trusted.  A caller may pass names only
    # after discovering them on the current same-origin GET form.  Keeping
    # this as an explicit opt-in prevents a forged ``?field=...`` URL from
    # being accepted as complete list evidence.
    for name in filter_params or ():
        name = str(name or '').strip()
        if name and re.fullmatch(r'[^\s&=;#%]+', name):
            allowed.add(name)
    if pagination:
        # Stock routes use ``page`` or ``/page/N``. Custom themes commonly
        # rename the cursor to pageNo/pageindex or add page-size/offset
        # controls. Keep this allow-list narrow so arbitrary search filters
        # are still rejected as non-complete list evidence.
        allowed |= {
            'page', 'pageno', 'page_no', 'pageindex', 'page_index',
            'pagenum', 'page_num', 'current', 'currentpage', 'current_page',
            'pagesize', 'page_size', 'perpage', 'per_page', 'limit', 'offset',
        }
    if token_params:
        allowed |= {'formcheck', '_token', '_csrf', 'csrf_token', 'csrfmiddlewaretoken'}
    allowed_lower = {str(key).lower() for key in allowed}
    unknown = [key for key in params if str(key).lower() not in allowed_lower]
    if unknown:
        raise ValueError('留言地址含过滤或未知参数')
    page_keys = {
        'page', 'pageno', 'page_no', 'pageindex', 'page_index',
        'pagenum', 'page_num', 'current', 'currentpage', 'current_page',
    }
    size_keys = {'pagesize', 'page_size', 'perpage', 'per_page', 'limit'}
    for key, value in params.items():
        low_key = str(key).lower()
        if low_key in page_keys and not re.fullmatch(r'[1-9][0-9]*', value):
            raise ValueError('留言地址页码无效')
        if low_key in size_keys and not re.fullmatch(r'[1-9][0-9]{0,4}', value):
            raise ValueError('留言地址分页大小无效')
        if low_key == 'offset' and not re.fullmatch(r'[0-9]+', value):
            raise ValueError('留言地址分页偏移无效')
    if path.rstrip('/') == entry:
        path_route = ''
    elif path.startswith(entry + '/'):
        path_route = path[len(entry):]
    else:
        raise ValueError('留言地址不属于当前后台入口')
    route = params.get('p', path_route)
    if not re.fullmatch(pattern, route, re.I):
        raise ValueError('留言操作路由不匹配')
    if path_route and ('p' in params and path_route.rstrip('/').lower() != route.rstrip('/').lower()):
        raise ValueError('留言地址路径与查询路由冲突')
    if pagination:
        match = re.search(r'/page/([1-9][0-9]*)/?$', route, re.I)
        for key, value in params.items():
            if str(key).lower() in page_keys and match and value != match.group(1):
                raise ValueError('留言地址页码冲突')
    return parsed._replace(fragment='').geturl(), route, params


def message_record_route(url, reference):
    pattern = r'/Message/(?P<op>mod|del)/id/(?P<id>[0-9]+)(?P<status>/field/status/value/[01])?/?'
    clean, route, _ = message_route(url, reference, pattern, token_params=True)
    match = re.fullmatch(pattern, route, re.I)
    operation = match.group('op').lower()
    if operation == 'del' and match.group('status'):
        raise ValueError('删除入口不能附带状态修改')
    action = 'delete' if operation == 'del' else 'status' if match.group('status') else 'reply'
    return clean, match.group('id'), action


def message_action_url(url, reference, message_id, action):
    clean, actual_id, actual_action = message_record_route(url, reference)
    if actual_id != str(message_id) or actual_action != action:
        raise ValueError('留言操作或目标编号不匹配')
    return clean
