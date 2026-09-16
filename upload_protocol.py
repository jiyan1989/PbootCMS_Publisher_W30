"""Strict upload outcome parsing; a URL is not by itself a success signal."""
import html as _html
import json
from urllib.parse import urlparse
import re


# Common upload adapters use a small family of names for the final object
# address.  Keep this allow-list explicit: arbitrary strings such as a source
# URL, message text or a redirect target must never become an upload success.
_PATH_KEYS = (
    'url', 'path', 'file_url', 'fileUrl', 'download_url', 'downloadUrl',
    'location', 'src', 'uri',
)
_CONTAINER_KEYS = (
    'data', 'paths', 'result', 'file', 'asset', 'output', 'response', 'payload',
    'job', 'task', 'process',
)


def _candidate_values(value, depth=0):
    """Yield explicitly named file-address candidates from common wrappers.

    The depth and key allow-list are deliberately bounded.  This handles
    nested JSON envelopes emitted by custom UEditor/Layui endpoints while
    refusing to treat arbitrary nested text as a saved file URL.
    """
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _candidate_values(item, depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key in _PATH_KEYS:
        # A path field itself must be a scalar string.  Do not recurse through
        # a malicious ``{"url": {"path": ...}}`` wrapper; nested traversal
        # is reserved for explicit response-container keys below.
        candidate = value.get(key)
        if isinstance(candidate, str):
            yield candidate
    for key in _CONTAINER_KEYS:
        if key not in value:
            continue
        nested = value.get(key)
        # Numeric object keys are used by several Pboot/Layui handlers in
        # place of an array.  Preserve numeric order before other wrappers.
        if key == 'data' and isinstance(nested, dict):
            numeric = sorted((name for name in nested if str(name).isdigit()),
                             key=lambda name: int(str(name)))
            for name in numeric:
                yield from _candidate_values(nested.get(name), depth + 1)
        yield from _candidate_values(nested, depth + 1)


def _iframe_json_payload(response):
    """Read the non-executable JSON envelope used by iframe upload forms.

    UEditor/Layui ``simpleupload`` submits a hidden ``iframe`` form instead
    of XHR.  Depending on the server's content type, the browser callback may
    receive plain JSON or JSON escaped inside a ``textarea`` element.  The
    desktop adapter must accept that *transport wrapper* without executing
    arbitrary iframe HTML/JavaScript.  Keep this parser deliberately strict:
    only a bounded whole-body JSON object/array or one ``textarea``/``pre``
    body is considered; login pages and script callbacks remain unknown.
    """
    raw = getattr(response, "text", "")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    if len(raw) > 4 * 1024 * 1024:
        return None
    text = raw.lstrip("\ufeff \t\r\n")

    def decode(candidate):
        candidate = _html.unescape(str(candidate or "")).strip()
        if not candidate or candidate[0] not in "[{":
            return None
        try:
            return json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    direct = decode(text)
    if direct is not None:
        return direct
    # Do not use a general HTML parser here: accepting arbitrary ``pre`` or
    # script text can mistake an error page/source URL for a file result.
    for tag in ("textarea", "pre"):
        match = re.search(
            rf"<\s*{tag}\b[^>]*>(.*?)<\s*/\s*{tag}\s*>",
            text, re.I | re.S)
        if match:
            payload = decode(match.group(1))
            if payload is not None:
                return payload
    return None


def parse_upload_result(response):
    status = getattr(response, 'status_code', 0)
    headers = getattr(response, 'headers', {}) or {}
    header_poll = ''
    if status == 202:
        for name in ('Location', 'X-Status-URL', 'X-Status-Url',
                     'X-Processing-URL', 'X-Processing-Url'):
            value = headers.get(name)
            if isinstance(value, str) and value.strip():
                header_poll = value.strip()
                break
    if not 200 <= status < 300:
        outcome = 'rejected' if 400 <= status < 500 and status != 408 else 'unknown'
        return {'outcome':outcome, 'path':'', 'message':f'上传接口 HTTP {status}，未确认成功', 'metadata':{}}
    try:
        value = response.json()
    except (ValueError, TypeError):
        value = _iframe_json_payload(response)
        if value is not None:
            # Continue through the same strict envelope/path validation as a
            # native JSON response.  No iframe script is executed.
            pass
        else:
            if status == 202 and header_poll:
                return {'outcome': 'pending', 'path': '',
                        'message': '后台已接受上传任务，等待状态地址返回结果',
                        'metadata': {'processing': True, '_http_status': 202,
                                     '_poll_url': header_poll}}
            return {'outcome':'unknown', 'path':'', 'message':'上传响应不是有效JSON，无法确认是否已保存文件', 'metadata':{}}
    if not isinstance(value, dict):
        return {'outcome':'unknown', 'path':'', 'message':'上传响应结构无法确认结果', 'metadata':{}}
    # HTTP 202 is itself an explicit browser/server contract: the request was
    # accepted for processing but the representation may not be final yet.
    # Preserve the original JSON for ordinary responses; only add private
    # transport metadata for 202 so callers can perform bounded read-only
    # observation without replaying the upload POST.
    metadata = value
    if status == 202:
        metadata = dict(value)
        metadata.setdefault('processing', True)
        metadata['_http_status'] = 202
    # Preserve an explicitly advertised status endpoint even when the same
    # acknowledgement also contains a provisional file URL.  The browser
    # still waits for that endpoint before treating the object as final; the
    # old parser only retained it for URL-less responses and could render the
    # first, pre-processing object prematurely.
    poll_url = header_poll
    if not poll_url:
        for key in ('status_url', 'poll_url', 'processing_url',
                    'process_url', 'job_url'):
            candidate = value_for(metadata, key)
            if isinstance(candidate, str) and candidate.strip():
                poll_url = candidate.strip()
                break
    if poll_url:
        metadata = dict(metadata)
        metadata.setdefault('_poll_url', poll_url)
    message = next((str(value[k]) for k in ('message','msg','error') if value.get(k)), '')
    positive = []
    if 'code' in value:
        positive.append(value['code'] in (1,'1',True))
    if 'state' in value:
        positive.append(str(value['state']).upper() == 'SUCCESS')
    if 'success' in value:
        positive.append(value['success'] is True or str(value['success']).lower() in ('1','true','success'))
    if (positive and not all(positive)) or value.get('error'):
        return {'outcome':'rejected', 'path':'', 'message':message or str(value.get('state') or '后台明确返回上传失败'), 'metadata':metadata}
    if positive and re.search(r'\b(?:error|failed|failure|denied)\b|失败|错误|未成功|无权限', message, re.I):
        return {'outcome':'unknown', 'path':'', 'message':'上传成功标志与响应文案矛盾，请先到后台核对：'+message, 'metadata':metadata}
    if not positive:
        # Some asynchronous handlers acknowledge a job with no file URL and
        # expose only a same-origin status/poll endpoint.  Keep that explicit
        # contract distinct from an arbitrary JSON response so the caller can
        # perform bounded read-only polling instead of replaying the upload.
        status_url = next((value for key in (
            'status_url', 'poll_url', 'processing_url', 'process_url',
            'job_url') if isinstance(value := value_for(metadata, key), str)
            and value.strip()), '')
        if not status_url:
            status_url = header_poll
        if status_url:
            metadata = dict(metadata)
            metadata['_poll_url'] = status_url.strip()
            return {'outcome':'pending', 'path':'',
                    'message':message or '后台已接受上传任务，等待处理结果',
                    'metadata':metadata}
        return {'outcome':'unknown', 'path':'', 'message':message or '上传响应缺少明确成功标志', 'metadata':metadata}
    for candidate in _candidate_values(value):
        if not isinstance(candidate,str) or not candidate.strip():
            continue
        candidate = candidate.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in candidate):
            continue
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        if parsed.scheme and parsed.scheme.lower() not in ('http','https'):
            continue
        # A scalar from an explicitly named field may be a relative asset
        # path, but a bare word from a nested array is usually a notice,
        # status label, or source text.  Keep common extensionless download
        # routes (``/download?id=...``) while refusing that ambiguous form.
        if not parsed.scheme and not candidate.startswith(('/', './', '../', '//')):
            path = parsed.path
            if '/' not in path and not re.search(
                    r'\.[A-Za-z0-9]{1,12}(?:$|[?#])', path):
                continue
        return {'outcome':'confirmed', 'path':candidate, 'message':message, 'metadata':metadata}
    status_url = next((value for key in (
        'status_url', 'poll_url', 'processing_url', 'process_url', 'job_url')
        if isinstance(value := value_for(metadata, key), str) and value.strip()), '')
    if not status_url:
        status_url = header_poll
    if status_url:
        metadata = dict(metadata)
        metadata['_poll_url'] = status_url.strip()
        return {'outcome': 'pending', 'path': '',
                'message': message or '后台已接受上传任务，等待处理结果',
                'metadata': metadata}
    return {'outcome':'unknown', 'path':'', 'message':'后台返回成功但没有可用文件地址，请核对后再操作', 'metadata':metadata}


def value_for(value, key):
    """Read a status URL from common bounded response envelopes."""
    if not isinstance(value, dict):
        return ''
    direct = value.get(key)
    if direct not in (None, ''):
        return direct
    if key not in ('status_url', 'poll_url', 'processing_url', 'process_url',
                   'job_url'):
        return ''
    def search(item, depth=0):
        if depth > 4 or not isinstance(item, dict):
            return ''
        nested = item.get(key)
        if nested not in (None, ''):
            return nested
        for name in _CONTAINER_KEYS:
            found = search(item.get(name), depth + 1)
            if found not in (None, ''):
                return found
        return ''
    found = search(value)
    if found not in (None, ''):
        return found
    return ''
