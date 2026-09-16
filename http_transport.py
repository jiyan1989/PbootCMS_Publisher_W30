"""Bounded same-origin HTTP redirects without application-level retries."""
from collections.abc import Mapping
import inspect
import re
from urllib.parse import urljoin, urlsplit

import requests
from exceptions import NetworkError


REDIRECTS = frozenset((301, 302, 303, 307, 308))
BODY_HEADERS = frozenset(('content-type', 'content-length', 'content-encoding',
                          'content-language', 'content-location', 'transfer-encoding'))


def origin(url):
    try:
        p = urlsplit(url)
        if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
                or any(ord(c) < 33 for c in url) or '\\' in url):
            raise ValueError('invalid HTTP URL')
        return p.scheme, p.hostname.lower(), p.port or (443 if p.scheme == 'https' else 80)
    except ValueError:
        raise NetworkError('请求或重定向地址无效，已停止继续访问') from None


def permitted_transition(source, target):
    a, b = origin(source), origin(target)
    return a == b or (a[0] == 'http' and a[2] == 80 and b[0] == 'https' and b[2] == 443 and a[1] == b[1])


def redirect_method(method, status):
    if status in (301, 302) and method == 'POST':
        return 'GET'
    if status == 303 and method not in ('GET', 'HEAD'):
        return 'GET'
    return method


def _body_positions(kwargs):
    streams = []
    body = kwargs.get('data')
    if body is not None and not isinstance(body, (str, bytes, bytearray, list, tuple, Mapping)):
        streams.append(body)
    files = kwargs.get('files') or {}
    entries = files.items() if isinstance(files, Mapping) else files
    for _name, value in entries:
        value = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else value
        if hasattr(value, 'read'):
            streams.append(value)
    positions = []
    for stream in streams:
        try:
            positions.append((stream, stream.tell()))
        except (AttributeError, OSError, ValueError):
            positions.append((stream, None))
    return positions


def request_with_redirects(session, method, url, *, max_redirects=20,
                           allow_redirects=True, **kwargs):
    """Follow HTTP redirects; never resend a request due to network failure.

    Cross-origin/downgrade requests are rejected before sending credentials or
    body. A default-port same-host HTTP→HTTPS upgrade is permitted. Uploads use
    their separate non-following path because their result may be uncertain.
    """
    method = str(method).upper()
    if method not in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
        raise ValueError('unsupported HTTP method')
    origin(url)
    current = url
    kwargs = dict(kwargs)
    kwargs['headers'] = dict(kwargs.get('headers') or {})
    positions = _body_positions(kwargs)
    history = []

    def send_without_following(sender, target, request_kwargs):
        """Call a requests-like sender with redirects disabled.

        The production ``requests.Session`` methods expose
        ``allow_redirects``.  A few integrations (and our intentionally tiny
        in-process test routers) implement only ``get(url, params, timeout)``
        and reject that keyword.  For those adapters we preserve compatibility
        by inspecting the callable before invocation.  We do *not* catch an
        arbitrary ``TypeError`` from inside the sender, since retrying a
        request after such an error could duplicate a write.
        """
        try:
            signature = inspect.signature(sender)
            parameters = signature.parameters.values()
            accepts_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD
                             for p in parameters)
            has_kw = "allow_redirects" in signature.parameters
            if accepts_kw:
                call_kwargs = dict(request_kwargs)
            else:
                # Keep compatibility with small adapters that expose only
                # the subset they actually consume (for example
                # ``get(url, params=None, timeout=None)``).  Requests' real
                # methods all accept ``**kwargs`` and therefore retain every
                # transport option above.
                call_kwargs = {
                    key: value for key, value in request_kwargs.items()
                    if key in signature.parameters
                }
        except (TypeError, ValueError):
            # Unknown callables are treated as strict requests-like senders;
            # passing the explicit safety flag is the least surprising path.
            accepts_kw = has_kw = True
            call_kwargs = dict(request_kwargs)
        if accepts_kw or has_kw:
            return sender(target, allow_redirects=False, **call_kwargs)
        return sender(target, **call_kwargs)

    for hop in range(max_redirects + 1):
        sender = getattr(session, method.lower())
        try:
            response = send_without_following(sender, current, kwargs)
        except TypeError as exc:
            # Some legacy/test adapters expose a ``**kwargs`` wrapper which
            # forwards to a narrower ``get(url, timeout, params)`` method.
            # The wrapper advertises transport keywords even though the
            # underlying sender rejects them.  A read-only GET/HEAD has not
            # been sent in that case, so remove only the named unexpected
            # keyword and retry the same call.  Never do this for a write
            # request: retrying a POST after an opaque TypeError could
            # duplicate a mutation.
            compatible = dict(kwargs)
            message = str(exc)
            for _ in range(8):
                match = re.search(
                    r"unexpected keyword argument ['\"]([^'\"]+)['\"]",
                    message, re.I)
                if (method not in ('GET', 'HEAD') or not match):
                    raise
                compatible.pop(match.group(1), None)
                try:
                    response = sender(current, **compatible)
                    break
                except TypeError as retry_error:
                    message = str(retry_error)
            else:
                raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if history:
                # Even a read-only entry URL may redirect to a write action.
                # Never retry the whole chain after reaching another endpoint.
                raise NetworkError('重定向链中连接中断，结果未知；未自动重发，请先核对后台') from exc
            raise
        resolved = getattr(response, 'url', '') or current
        if not permitted_transition(current, resolved):
            raise NetworkError('请求返回了非预期来源，结果待核对')
        location = (getattr(response, 'headers', {}) or {}).get('Location', '')
        # Tiny in-process compatibility responses sometimes expose only
        # ``ok/text/url``.  Treat an omitted status as an ordinary 200 read;
        # production ``requests.Response`` objects always provide it.
        status_code = getattr(response, 'status_code', 200)
        if not allow_redirects or status_code not in REDIRECTS or not location:
            response.history = history
            return response
        if hop >= max_redirects:
            raise NetworkError('重定向次数超过限制，结果未知；请先核对后台')
        target = urljoin(resolved, location)
        if not permitted_transition(resolved, target):
            raise NetworkError('重定向跨来源或降级到HTTP，已拒绝继续；原请求结果请核对后台')
        next_method = redirect_method(method, status_code)
        if next_method != method:
            for key in ('data', 'json', 'files'):
                kwargs.pop(key, None)
            kwargs['headers'] = {k:v for k,v in kwargs['headers'].items() if k.lower() not in BODY_HEADERS}
            # Suppress body headers inherited from session defaults as well.
            for key in getattr(session, 'headers', {}):
                if key.lower() in BODY_HEADERS:
                    kwargs['headers'][key] = None
            positions = []
        elif method not in ('GET', 'HEAD'):
            prepared = getattr(response, 'request', None)
            body = getattr(prepared, 'body', None)
            if isinstance(body, (str, bytes, bytearray)):
                # Preserve the already encoded bytes, including a multipart
                # boundary, instead of rebuilding a different MIME body.
                kwargs.pop('files', None); kwargs.pop('json', None)
                kwargs['data'] = body
                for key, value in getattr(prepared, 'headers', {}).items():
                    if key.lower() in BODY_HEADERS:
                        kwargs['headers'][key] = value
                positions = []
            else:
                for stream, position in positions:
                    if position is None:
                        raise NetworkError('重定向要求保留请求体，但数据流不可回放；结果未知，未继续发送')
                    try:
                        stream.seek(position)
                    except (AttributeError, OSError, ValueError) as exc:
                        raise NetworkError('无法恢复重定向请求体，结果未知，未继续发送') from exc
        kwargs.pop('params', None)
        history.append(response)
        close = getattr(response, 'close', None)
        if close:
            close()
        current, method = target, next_method
    raise NetworkError('重定向未完成')
