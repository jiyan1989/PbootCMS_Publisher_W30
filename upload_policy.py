"""Read-only, non-executing adapters for native Layui and UEditor uploads.

Only literal configuration and a small set of DOM reads/string expressions
are interpreted. Never eval site JavaScript. Unsupported dynamic policies
raise before an upload rather than silently using a guessed endpoint.
"""
import ast
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from client_utils import _is_login_page
from asset_types import sniff_extension, sniff_mime
from file_metadata import declared_mime
from http_transport import request_with_redirects


class UploadPolicyError(RuntimeError):
    pass


class NativeUploadRequired(UploadPolicyError):
    """The browser must perform this upload through the authenticated page.

    ``UploadPolicyError`` is also used for ordinary, not-yet-sent validation
    failures.  Upload paths that discover a browser-only transform (for
    example a Canvas encoder) need to preserve the page URL and the reason so
    synchronous module writers can open the exact native form instead of
    flattening the condition into a generic error.  Keeping this as a
    subclass preserves all existing fail-closed catches.
    """

    def __init__(self, message, *, native_url="", reason=""):
        super().__init__(message)
        self.native_url = str(native_url or "")
        self.reason = str(reason or message)


def flatten_upload_data(value, prefix=""):
    """Encode literal Layui data objects like browser/jQuery form data.

    Dynamic functions are rejected by the evaluator before reaching here;
    this helper only expands static dictionaries/lists into bracketed keys so
    custom upload controls do not lose declared tokens or repeated values.
    """
    pairs = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key) if not prefix else f"{prefix}[{key}]"
            pairs.extend(flatten_upload_data(item, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            pairs.extend(flatten_upload_data(item, prefix + "[]" if prefix else ""))
    else:
        if not prefix or not isinstance(prefix, str):
            raise UploadPolicyError('上传data字段名无效')
        if value is None:
            value = ""
        if isinstance(value, bool):
            value = "true" if value else "false"
        pairs.append((prefix, str(value)))
    return pairs


def encoded_upload_data(value):
    """Return requests-compatible data while preserving repeated keys."""
    pairs = flatten_upload_data(value)
    keys = [key for key, _item in pairs]
    return dict(pairs) if len(keys) == len(set(keys)) else pairs


def same_origin(url, reference):
    def origin(value):
        p = urlsplit(value)
        scheme = p.scheme.lower()
        return scheme, (p.hostname or '').lower(), p.port or (443 if scheme == 'https' else 80)
    try:
        p = urlsplit(url)
        if p.scheme.lower() not in ('http', 'https') or p.username or p.password:
            return False
        target, source = origin(url), origin(reference)
        if target == source:
            return True
        # Browsers commonly canonicalize an HTTP admin entry to HTTPS.  Keep
        # the upgrade same-host and one-way; never accept a downgrade or a
        # different port/host as an upload endpoint.
        return (source[0] == 'http' and target[0] == 'https' and
                source[1] == target[1] and source[2] == 80 and
                target[2] == 443)
    except ValueError:
        return False


def safe_url(value, base):
    if not isinstance(value, str) or not value.strip() or re.search(r'[\x00-\x20\\]', value):
        raise UploadPolicyError('上传配置含无效地址')
    result = urljoin(base, value)
    if not same_origin(result, base):
        raise UploadPolicyError('上传配置跨站，尚未授权该上传服务')
    # URL fragments are resolved by the browser but are never sent in an
    # HTTP request.  Strip them before using the value as an upload endpoint
    # so the desktop request target matches ``fetch``/XHR rather than keeping
    # a fragment that only exists in the DOM URL.
    parts = urlsplit(result)
    return urlunsplit(parts._replace(fragment=''))


def tokens(source):
    """Retain strings as atomic tokens; ignore comments, never execute code."""
    pattern = re.compile(r'''\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]''')
    result = []
    for match in pattern.finditer(source):
        value = match.group()
        if value.isspace() or value.startswith(('//', '/*')):
            continue
        result.append(value)
    return result


def _is_safe_noop_callback(value):
    """Recognize callbacks whose only effect is returning their argument.

    A few Layui wrappers use ``choose:function(file){return file;}`` instead
    of an empty callback.  Treating that exact form as equivalent to a no-op
    is safe because it cannot mutate bytes, fields, the queue or the request;
    every other callback remains fail-closed and is never evaluated.
    """
    values = list(value or [])
    if values in (tokens('function(){}'), tokens('function(obj){}'),
                  tokens('function(file){}')):
        return True
    if len(values) != 9 or values[0] != 'function' or values[1] != '(':
        return False
    parameter = values[2]
    if parameter not in ('obj', 'file') or values[3:5] != [')', '{']:
        return False
    return values[5:] == ['return', parameter, ';', '}']


def quoted(value):
    if len(value) < 2 or value[0] not in ('"', "'"):
        raise UploadPolicyError('上传脚本使用了非字面量配置')
    try:
        # literal_eval only decodes one string; it cannot call functions.
        return ast.literal_eval(value.replace('\\/', '/'))
    except (ValueError, SyntaxError):
        raise UploadPolicyError('无法解析上传配置字符串') from None


def split_top(values, separator):
    out, start, depth = [], 0, []
    pairs = {')': '(', ']': '[', '}': '{'}
    for i, value in enumerate(values):
        if value in ('(', '[', '{'):
            depth.append(value)
        elif value in pairs:
            if not depth or depth.pop() != pairs[value]:
                raise UploadPolicyError('上传脚本结构无法解析')
        elif value == separator and not depth:
            out.append(values[start:i]); start = i + 1
    out.append(values[start:])
    return out


def balanced(values, start):
    opener = values[start]
    closer = {'(': ')', '[': ']', '{': '}'}[opener]
    depth = 0
    for i in range(start, len(values)):
        if values[i] == opener:
            depth += 1
        elif values[i] == closer:
            depth -= 1
            if not depth:
                return values[start:i+1], i+1
    raise UploadPolicyError('上传配置未闭合')


def properties(values):
    if not values or values[0] != '{' or values[-1] != '}':
        raise UploadPolicyError('上传配置不是可解析对象')
    result = {}
    for part in split_top(values[1:-1], ','):
        if not part:
            continue
        if len(part) < 3 or part[1] != ':':
            raise UploadPolicyError('上传配置使用了动态属性或展开语法')
        key = quoted(part[0]) if part[0][0] in ('"', "'") else part[0]
        result[key] = part[2:]
    return result


_EDITOR_RUNTIME_KEYS = (
    'serverUrl', 'maximumWords', 'catchRemoteImageEnable',
    'image', 'video', 'audio', 'file', 'catcher',
)


def _is_editor_runtime_key(name):
    """Return whether a literal UEditor runtime key affects upload semantics."""
    text = str(name or '')
    return text.startswith(_EDITOR_RUNTIME_KEYS)


def _static_editor_setopt(script_tokens, identity, variables, soup):
    """Read safe literal ``setOpt`` overrides for one editor instance.

    UEditor pages sometimes load the public config first and then call
    ``editor.setOpt({...})`` after ``UE.getEditor``.  Treating every setOpt as
    dynamic made the desktop path needlessly diverge from a browser.  This
    parser accepts only a literal object (or literal key/value pair), proves
    that the call targets the requested editor, and evaluates values through
    the existing non-executing literal parser.  Any upload-relevant dynamic
    call still fails closed.
    """
    tokens_list = list(script_tokens or [])
    if 'setOpt' not in tokens_list:
        return {}
    identity = str(identity or '')
    target_vars = set()
    for index in range(len(tokens_list) - 6):
        if tokens_list[index:index + 4] != ['UE', '.', 'getEditor', '(']:
            continue
        try:
            call, _call_end = balanced(tokens_list, index + 3)
            call_parts = split_top(call[1:-1], ',')
            target = quoted(call_parts[0][0]) if call_parts and len(call_parts[0]) == 1 else ''
        except (UploadPolicyError, IndexError):
            continue
        if target != identity:
            continue
        # ``var editor = UE.getEditor('content')`` or
        # ``editor = UE.getEditor('content')``.
        if index >= 2 and tokens_list[index - 1] == '=':
            variable = tokens_list[index - 2]
            if re.fullmatch(r'[A-Za-z_$][\w$]*', variable):
                target_vars.add(variable)

    overrides = {}
    for index, token in enumerate(tokens_list):
        if token != 'setOpt' or index + 1 >= len(tokens_list) or tokens_list[index + 1] != '(':
            continue
        args, _end = balanced(tokens_list, index + 1)
        parts = split_top(args[1:-1], ',')
        raw_items = {}
        if len(parts) == 1 and parts[0] and parts[0][0] == '{':
            raw_items = properties(parts[0])
        elif len(parts) == 2:
            try:
                key = quoted(parts[0])
            except UploadPolicyError:
                raise UploadPolicyError('UEditor setOpt 使用了动态键') from None
            raw_items = {key: parts[1]}
        else:
            raise UploadPolicyError('UEditor setOpt 调用结构尚未适配')
        relevant = {key: value for key, value in raw_items.items()
                    if _is_editor_runtime_key(key)}
        if not relevant:
            continue
        # Prove the call belongs to this target editor. Look back to the
        # current statement boundary for either a direct getEditor chain or
        # the variable assigned from the target instance.
        boundary = max((pos for pos in range(index)
                        if tokens_list[pos] in (';', '{', '}')), default=-1)
        context = tokens_list[boundary + 1:index]
        direct = False
        for pos in range(len(context) - 6):
            if context[pos:pos + 4] == ['UE', '.', 'getEditor', '(']:
                try:
                    call, _call_end = balanced(context, pos + 3)
                    call_parts = split_top(call[1:-1], ',')
                    direct = (bool(call_parts) and len(call_parts[0]) == 1 and
                              quoted(call_parts[0][0]) == identity)
                except (UploadPolicyError, IndexError):
                    direct = False
                if direct:
                    break
        via_variable = bool(target_vars.intersection(context))
        if not direct and not via_variable:
            raise UploadPolicyError('无法确认UEditor setOpt属于当前编辑器')
        for key, value in relevant.items():
            # Keep the token expression until the normal effective-config
            # merge below evaluates it. This preserves arrays/booleans and
            # avoids passing an already-evaluated scalar back into evaluate().
            evaluate(value, variables, soup)  # validate now, without executing JS
            overrides[key] = value
    return overrides


def _editor_serverparam_scalar(value):
    """Return the JavaScript string form used by ``encodeURIComponent``.

    UEditor's ``utils.serializeParam`` deliberately serializes only primitive
    values (and arrays of primitives); objects, functions and ``null`` are
    omitted.  Keeping that distinction here prevents the desktop adapter from
    inventing a JSON encoding that the browser never sends.
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _editor_serverparam_pairs(serverparam):
    """Serialize a static UEditor serverparam object as ordered query pairs."""
    pairs = []
    for key, value in (serverparam or {}).items():
        if not isinstance(key, str) or not key or re.search(r'[\r\n]', key):
            raise UploadPolicyError('UEditor serverparam键无效')
        if isinstance(value, list):
            for item in value:
                scalar = _editor_serverparam_scalar(item)
                if scalar is not None:
                    pairs.append((key + '[]', scalar))
            continue
        scalar = _editor_serverparam_scalar(value)
        if scalar is not None:
            pairs.append((key, scalar))
    return pairs


def _static_editor_serverparam(script_tokens, identity, variables, soup,
                               known_vars=None):
    """Parse literal ``editor.execCommand('serverparam', ...)`` calls.

    This is intentionally narrower than a JavaScript interpreter.  It accepts
    the documented UEditor forms (clear, key/value, delete-key and literal
    object), proves the call belongs to the requested editor instance, and
    rejects callbacks/expressions so the caller can fall back to the native
    webpage instead of uploading with a guessed token.
    """
    values = list(script_tokens or [])
    known_map = (dict(known_vars) if isinstance(known_vars, dict) else
                 {name: identity for name in (known_vars or ())})
    target_vars = {name for name, target in known_map.items()
                   if target == identity}
    # Capture ``var editor = UE.getEditor('target')`` assignments.  The
    # variable may be used by a later inline script, so the caller carries the
    # resulting set across scripts.
    for index in range(len(values) - 6):
        if values[index:index + 4] != ['UE', '.', 'getEditor', '(']:
            continue
        try:
            call, _ = balanced(values, index + 3)
            parts = split_top(call[1:-1], ',')
            target = quoted(parts[0][0]) if parts and len(parts[0]) == 1 else ''
        except (UploadPolicyError, IndexError):
            continue
        if index < 2 or values[index - 1] != '=':
            continue
        variable = values[index - 2]
        if re.fullmatch(r'[A-Za-z_$][\w$]*', variable):
            known_map[variable] = target
            if target == identity:
                target_vars.add(variable)

    operations = []
    for index, token in enumerate(values):
        if token != 'execCommand' or index + 1 >= len(values) or values[index + 1] != '(':
            continue
        try:
            args, _ = balanced(values, index + 1)
            parts = split_top(args[1:-1], ',')
        except UploadPolicyError:
            raise UploadPolicyError('UEditor serverparam调用结构无法解析') from None
        if not parts or not parts[0]:
            continue
        command = parts[0]
        # Prove the receiver.  ``editor.execCommand`` is accepted only for a
        # variable created from the requested UE.getEditor instance.  The
        # direct ``UE.getEditor('id').execCommand`` spelling is also common.
        receiver_ok = False
        receiver_other = False
        if index >= 2 and values[index - 1] == '.':
            receiver = values[index - 2]
            receiver_ok = receiver in target_vars
            receiver_other = receiver in known_map and not receiver_ok
        if not receiver_ok and not receiver_other:
            for pos in range(max(0, index - 16), index - 3):
                if values[pos:pos + 4] != ['UE', '.', 'getEditor', '(']:
                    continue
                try:
                    call, call_end = balanced(values, pos + 3)
                    call_parts = split_top(call[1:-1], ',')
                    direct_target = (quoted(call_parts[0][0])
                                     if call_parts and len(call_parts[0]) == 1
                                     else '')
                    direct = (call_parts and len(call_parts[0]) == 1 and
                              direct_target == identity and
                              call_end + 1 == index and
                              values[call_end] == '.')
                    receiver_ok = bool(direct)
                    receiver_other = bool(call_parts and len(call_parts[0]) == 1 and
                                          direct_target and direct_target != identity and
                                          call_end + 1 == index and values[call_end] == '.')
                except (UploadPolicyError, IndexError):
                    receiver_ok = False
                if receiver_ok:
                    break
        is_serverparam = (len(command) == 1 and
                          command[0] in ('"serverparam"', "'serverparam'"))
        if not is_serverparam:
            # Other commands do not affect the upload query.  A dynamic
            # command is therefore irrelevant unless it is proven to be this
            # editor's serverparam operation below.
            continue
        if not receiver_ok and receiver_other:
            continue
        if not receiver_ok:
            raise UploadPolicyError('无法确认UEditor serverparam属于当前编辑器')
        if len(parts) == 1:
            operations.append(('clear', None))
            continue
        key_expr = parts[1]
        if key_expr and key_expr[0] == '{':
            if len(parts) != 2:
                raise UploadPolicyError('UEditor serverparam参数过多')
            obj = evaluate(key_expr, variables, soup)
            if not isinstance(obj, dict):
                raise UploadPolicyError('UEditor serverparam对象无效')
            operations.append(('merge', obj))
            continue
        if len(key_expr) != 1 or key_expr[0][0] not in ('"', "'"):
            raise UploadPolicyError('UEditor serverparam键含动态JavaScript')
        key = quoted(key_expr[0])
        if len(parts) == 2:
            operations.append(('delete', key))
            continue
        value = evaluate(parts[2], variables, soup)
        if len(parts) > 3:
            raise UploadPolicyError('UEditor serverparam参数过多')
        # UEditor's command treats an explicit null/undefined value as a
        # delete, matching the documented ``execCommand(cmd, key)`` form.
        operations.append(('delete', key) if value is None else ('set', (key, value)))
    return operations, known_map


def _literal_dom_element(soup, selector):
    """Resolve a deliberately small, static CSS selector.

    Upload configuration commonly reads a token from ``#formcheck`` or
    ``input[name="formcheck"]``.  Supporting these selectors keeps the
    adapter close to browser behaviour without evaluating arbitrary JS or
    allowing a selector to escape into a script expression.
    """
    selector = str(selector or "").strip()
    if not re.fullmatch(
            r'(?:#[\w-]+|\.[\w-]+|[A-Za-z][\w:-]*(?:\[[^\]<>]+\])?|\[[^\]<>]+\])',
            selector):
        raise UploadPolicyError('上传配置选择器尚未支持')
    try:
        elements = soup.select(selector)
    except Exception:
        raise UploadPolicyError('上传配置选择器无效') from None
    if len(elements) != 1:
        raise UploadPolicyError('上传配置引用的页面控件缺失或重名')
    return elements[0]


def _literal_dom_property(element, prop):
    """Read the small, side-effect-free DOM property subset used by uploads.

    ``document.querySelector(...).content`` is common for meta CSRF tokens,
    while ``dataset.foo`` is common in custom Pboot/Layui wrappers.  These
    reads are equivalent to the browser DOM and do not execute page code.  A
    property outside this allow-list remains unsupported and therefore keeps
    the native-web fallback.
    """
    prop = str(prop or '')
    if prop in ('value', 'textContent', 'innerText'):
        return str(element.get('value', '') if prop == 'value'
                   else element.get_text())
    if prop in ('content', 'href', 'src', 'id', 'name', 'type'):
        if not element.has_attr(prop):
            raise UploadPolicyError('上传配置引用的控件属性缺失')
        return str(element.get(prop, ''))
    if prop.startswith('dataset.'):
        key = prop.split('.', 1)[1]
        if not re.fullmatch(r'[A-Za-z_$][\w$]*', key):
            raise UploadPolicyError('上传配置引用的dataset键无效')
        attr = 'data-' + re.sub(r'([A-Z])', lambda m: '-' + m.group(1).lower(), key)
        if not element.has_attr(attr):
            raise UploadPolicyError('上传配置引用的dataset属性缺失')
        return str(element.get(attr, ''))
    raise UploadPolicyError('上传配置引用了尚未适配的DOM属性')


def evaluate(values, variables, soup):
    # Safe scalar wrappers frequently appear around a literal DOM read in
    # upload configs (``String($('#token').val()).trim()``).  They do not
    # execute code, so unwrap only the exact call shape and evaluate the inner
    # expression through this same restricted parser.
    if values and values[-4:] == ['.', 'trim', '(', ')']:
        return str(evaluate(values[:-4], variables, soup)).strip()
    if (len(values) >= 4 and values[0] in ('String', 'Number', 'Boolean', 'encodeURIComponent')
            and values[1] == '(' and values[-1] == ')'):
        inner = evaluate(values[2:-1], variables, soup)
        name = values[0]
        if name == 'String':
            return '' if inner is None else str(inner)
        if name == 'Boolean':
            return bool(inner)
        if name == 'Number':
            try:
                number = float(inner)
                return int(number) if number.is_integer() else number
            except (TypeError, ValueError):
                raise UploadPolicyError('上传配置数字表达式无效') from None
        # encodeURIComponent is deterministic and has no side effects.  Keep
        # the browser's unescaped URI component punctuation.
        return quote(str(inner), safe="-_.!~*'()")
    # A number of otherwise-static upload configs wrap data/headers in a
    # function or arrow callback.  Safely unwrap only the exact
    # ``function(...) { return <literal>; }`` / ``(...) => (<literal>)``
    # shapes.  Any statement, side effect or conditional remains rejected;
    # this is still a parser, never a JavaScript evaluator.
    if values and values[0] == 'function':
        try:
            open_brace = values.index('{')
        except ValueError:
            raise UploadPolicyError('上传配置函数未声明静态返回值')
        if len(values) < open_brace + 4 or values[-1] != '}':
            raise UploadPolicyError('上传配置函数结构尚未适配')
        body = values[open_brace + 1:-1]
        if not body or body[0] != 'return':
            raise UploadPolicyError('上传配置函数含动态语句')
        if body[-1] == ';':
            body = body[:-1]
        if not body or any(token in body for token in ('if', 'for', 'while', '=>')):
            raise UploadPolicyError('上传配置函数含动态语句')
        return evaluate(body[1:], variables, soup)
    if '=>' in values:
        arrow = values.index('=>')
        body = values[arrow + 1:]
        if not body:
            raise UploadPolicyError('上传配置箭头函数未声明返回值')
        if body[0] == '{':
            if body[-1] != '}' or len(body) < 3 or body[1] != 'return':
                raise UploadPolicyError('上传配置箭头函数含动态语句')
            body = body[2:-1]
            if body and body[-1] == ';':
                body = body[:-1]
        elif body[0] == '(' and body[-1] == ')':
            body = body[1:-1]
        return evaluate(body, variables, soup)
    parts = split_top(values, '+')
    if len(parts) > 1:
        items = [evaluate(p, variables, soup) for p in parts]
        if not all(isinstance(v, str) for v in items):
            raise UploadPolicyError('上传地址表达式不是字符串连接')
        return ''.join(items)
    if len(values) == 1:
        value = values[0]
        if value[0] in ('"', "'"):
            return quoted(value)
        if value in ('true', 'false', 'null'):
            return {'true': True, 'false': False, 'null': None}[value]
        if re.fullmatch(r'\d+(?:\.\d+)?', value):
            return float(value) if '.' in value else int(value)
    name = ''.join(values)
    if name in variables:
        return variables[name]
    if values and values[0] == '{' and values[-1] == '}':
        return {k: evaluate(v, variables, soup) for k, v in properties(values).items()}
    if values and values[0] == '[' and values[-1] == ']':
        return [evaluate(v, variables, soup) for v in split_top(values[1:-1], ',') if v]
    # $("#preurl").data("preurl") and $("#token").val(), fresh page only.
    # Keep this deliberately structural: no JavaScript is executed.
    if (len(values) >= 8 and values[0:2] in (['$', '('], ['jQuery', '('])
            and values[3:5] == [')', '.'] and values[6] == '(' and values[-1] == ')'):
        selector = quoted(values[2])
        element = _literal_dom_element(soup, selector)
        if values[5] == 'val' and len(values) == 8:
            return str(element.get('value', ''))
        # data()/attr() has one quoted key in addition to the selector/method
        # wrapper (nine tokens total).
        if values[5] in ('data', 'attr', 'prop') and len(values) == 9:
            key = quoted(values[7])
            if values[5] == 'data':
                key = 'data-' + key
                if not element.has_attr(key):
                    raise UploadPolicyError('上传配置引用的控件属性缺失')
                return str(element[key])
            if values[5] == 'attr':
                if not element.has_attr(key):
                    raise UploadPolicyError('上传配置引用的控件属性缺失')
                return str(element[key])
            return _literal_dom_property(element, key)
    # Common vanilla-DOM equivalents emitted by custom upload wrappers.  Only
    # literal IDs/attributes are accepted; the page is read fresh and no
    # script is evaluated.
    if (len(values) >= 8 and values[:4] == ['document', '.', 'getElementById', '(']
            and values[5:7] == [')', '.'] and values[-1] not in (')',)):
        element_id = quoted(values[4]) if len(values) in (8, 10) else None
        if not element_id:
            raise UploadPolicyError('上传配置引用了动态DOM控件')
        element = _literal_dom_element(soup, '#' + element_id)
        prop = values[7]
        if len(values) == 8:
            return _literal_dom_property(element, prop)
        if (len(values) == 10 and values[7:9] == ['dataset', '.']):
            return _literal_dom_property(element, 'dataset.' + values[9])
    if (len(values) >= 11 and values[:4] == ['document', '.', 'getElementById', '(']
            and values[5:7] == [')', '.'] and values[7] == 'getAttribute'
            and values[8] == '(' and values[-1] == ')'):
        element_id = quoted(values[4]) if len(values) == 11 else None
        attr = quoted(values[9]) if len(values) == 11 else None
        if not element_id or not attr:
            raise UploadPolicyError('上传配置引用了动态DOM属性')
        element = _literal_dom_element(soup, '#' + element_id)
        if not element.has_attr(attr):
            raise UploadPolicyError('上传配置引用的控件属性缺失')
        return str(element.get(attr, ''))
    if (len(values) >= 8 and values[:4] == ['document', '.', 'querySelector', '(']
            and values[5:7] == [')', '.'] and values[-1] not in (')',)):
        selector = quoted(values[4]) if len(values) in (8, 10) else None
        if not selector:
            raise UploadPolicyError('上传配置选择器尚未支持')
        element = _literal_dom_element(soup, selector)
        prop = values[7]
        if len(values) == 8:
            return _literal_dom_property(element, prop)
        if (len(values) == 10 and values[7:9] == ['dataset', '.']):
            return _literal_dom_property(element, 'dataset.' + values[9])
    if (len(values) >= 11 and values[:4] == ['document', '.', 'querySelector', '(']
            and values[5:7] == [')', '.'] and values[7] == 'getAttribute'
            and values[8] == '(' and values[-1] == ')'):
        selector = quoted(values[4]) if len(values) == 11 else None
        attr = quoted(values[9]) if len(values) == 11 else None
        if not selector or not attr:
            raise UploadPolicyError('上传配置引用了动态DOM属性')
        element = _literal_dom_element(soup, selector)
        if not element.has_attr(attr):
            raise UploadPolicyError('上传配置引用的控件属性缺失')
        return str(element.get(attr, ''))
    raise UploadPolicyError('上传配置含尚未适配的动态JavaScript；请使用网页上传并反馈配置')


@dataclass(frozen=True)
class UploadPolicy:
    endpoint: str
    file_field: str
    page_url: str
    surface: str
    target: str
    extensions: tuple = ()
    max_bytes: int = 0
    data: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    url_prefix: str = ''
    metadata: dict = field(default_factory=dict)

    def validate(self, filename, data, *, check_size=True,
                 declared_mime_value=""):
        """Validate bytes against the discovered browser upload policy.

        Chromium's ``File.type`` is meaningful even for a dropped file with
        no extension and no short signature (fonts and some legacy media are
        common examples).  Preserve that hint as a *fallback* only for an
        extensionless name: recognizable bytes still win, and a named file
        can never bypass its filename/extension rule with a forged MIME.
        """
        declared = str(declared_mime_value or "").split(";", 1)[0].strip().lower()
        if declared and ("/" not in declared or any(
                ord(char) < 0x20 or char in "\r\n;" for char in declared)):
            declared = ""
        if self.extensions:
            suffix = Path(filename).suffix.lower()
            if suffix not in self.extensions:
                # A browser File can have an empty extension while still
                # carrying a recognised MIME/signature. Keep named-file
                # extension checks strict; only use bounded bytes for the
                # extensionless case so a mismatched ``photo.jpg`` is not
                # silently accepted as a different format.
                detected = sniff_extension(data, filename) if not suffix else ''
                if (not detected and not suffix and declared):
                    # Match the declared browser MIME only against the
                    # already discovered extension allow-list.  This keeps
                    # the policy narrow and prevents arbitrary File.type
                    # values from widening a server control.
                    from asset_types import mime_for_extension
                    if any(mime_for_extension(item).lower() == declared
                           for item in self.extensions):
                        detected = next(
                            (item for item in self.extensions
                             if mime_for_extension(item).lower() == declared),
                            "")
                if not detected or detected.lower() not in self.extensions:
                    raise UploadPolicyError('文件格式不符合当前网页上传配置：' + ', '.join(self.extensions))
        accept_mime = str((self.metadata or {}).get('accept_mime', '') or '').strip().lower()
        if accept_mime:
            actual_mime = str(sniff_mime(data, filename) or '').strip().lower()
            if not actual_mime and not Path(filename).suffix and declared:
                actual_mime = declared
            patterns = [item.strip() for item in accept_mime.split(',') if item.strip()]
            if not actual_mime or not any(
                    pattern == '*/*' or
                    (pattern.endswith('/*') and actual_mime.startswith(pattern[:-1])) or
                    pattern == actual_mime
                    for pattern in patterns):
                raise UploadPolicyError(
                    f'文件 MIME 不符合当前网页上传配置：{accept_mime}')
        if check_size and self.max_bytes and len(data) > self.max_bytes:
            raise UploadPolicyError(f'文件超过当前网页上传上限（{self.max_bytes}字节）')
        if not same_origin(self.endpoint, self.page_url):
            raise UploadPolicyError('上传策略已失效：端点与当前页面不同源')
        if not self.file_field or re.search(r'[\x00-\x1f]', self.file_field):
            raise UploadPolicyError('上传文件字段无效')
        try:
            flatten_upload_data(self.data)
        except UploadPolicyError:
            raise
        except (TypeError, ValueError):
            raise UploadPolicyError('上传data编码无效') from None
        if any(not isinstance(k, str) or not isinstance(v, str) or
               re.search(r'[\r\n]', k+v) for k, v in self.headers.items()):
            raise UploadPolicyError('上传请求头配置无效')

    def validate_path(self, filename, path, *, signature_bytes=128 * 1024,
                      check_size=True, declared_mime_value=""):
        """Validate a selected file before any queue request is started.

        Layui checks the whole selected queue's names and sizes before its
        first XHR.  Reading only a bounded signature prefix lets the desktop
        perform the same MIME/extension check without loading every large
        asset into memory twice; the full-byte validation still runs in the
        actual upload method immediately before the multipart request.
        """
        try:
            size = os.path.getsize(path)
        except (OSError, TypeError, ValueError) as exc:
            raise UploadPolicyError('文件不存在或无法读取') from exc
        if check_size and self.max_bytes and size > self.max_bytes:
            raise UploadPolicyError(f'文件超过当前网页上传上限（{self.max_bytes}字节）')
        try:
            with open(path, 'rb') as handle:
                prefix = handle.read(max(1024, int(signature_bytes)))
        except (OSError, TypeError, ValueError) as exc:
            raise UploadPolicyError('文件不存在或无法读取') from exc
        # ``validate`` is also responsible for origin, field, data and header
        # invariants.  Its size check sees only the prefix here, so the exact
        # on-disk size check above remains authoritative for this preflight.
        self.validate(filename, prefix, check_size=check_size,
                      declared_mime_value=(declared_mime_value or
                                           declared_mime(path)))

    def fingerprint(self):
        import hashlib
        # Include fresh policy data as a hash, never expose token values in logs.
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PolicyDiscovery:
    def __init__(self, session, page_url):
        self.session = session
        self.page_url = page_url
        self.resources = {}
        response = self.get(page_url)
        # The browser's document base and Referer become the final URL after
        # a safe canonical redirect.  Keep the original caller URL only as a
        # lookup alias; relative scripts, ``<base>`` and upload Origin must
        # use the resolved same-origin page.
        resolved_page = str(getattr(response, 'url', '') or page_url)
        if same_origin(resolved_page, page_url):
            self.page_url = resolved_page
        self.soup = BeautifulSoup(response.text, 'html.parser')
        if _is_login_page(response.text) or not self.soup.find('form'):
            raise UploadPolicyError('未取得已登录的真实上传表单，请重新载入栏目/文章')
        self.base = self.page_url
        base = self.soup.find('base', href=True)
        if base:
            self.base = safe_url(base['href'], page_url)
        self.inline = [tokens(s.get_text()) for s in self.soup.find_all('script', src=False)
                       if str(s.get('type', '')).lower() != 'text/plain']

    def get(self, url):
        url = safe_url(url, self.page_url)
        if url not in self.resources:
            # Browser subresource fetches follow a bounded same-origin
            # canonical redirect (including HTTP:80→HTTPS:443).  Use the
            # shared guard so credentials never cross hosts or downgrade to
            # HTTP; a missing/unsafe Location remains a hard failure and the
            # resolved URL is retained as the resource identity.
            try:
                r = request_with_redirects(
                    self.session, 'GET', url, max_redirects=5, timeout=15)
            except Exception as exc:
                raise UploadPolicyError(
                    '读取上传配置失败或发生不安全重定向，请重新载入页面') from exc
            resolved_url = getattr(r, 'url', '') or url
            if not 200 <= r.status_code < 300 or not same_origin(resolved_url, self.page_url):
                raise UploadPolicyError('读取上传配置失败或发生重定向，请重新载入页面')
            if _is_login_page(r.text):
                raise UploadPolicyError('上传配置会话失效，请重新登录')
            self.resources[url] = r
            self.resources[str(resolved_url)] = r
        return self.resources[url]

    def script(self, filename):
        candidates = [s['src'] for s in self.soup.find_all('script', src=True)
                      if urlsplit(s['src']).path.rsplit('/', 1)[-1].lower() == filename]
        if len(candidates) != 1:
            raise UploadPolicyError(f'无法唯一定位网页的{filename}配置')
        url = safe_url(candidates[0], self.base)
        return url, tokens(self.get(url).text)

    def native(self, target):
        controls = [e for e in self.soup.find_all(['input', 'textarea']) if e.get('name') == target]
        if len(controls) != 1 or not controls[0].get('id'):
            raise UploadPolicyError(f'无法唯一定位{target}上传目标控件')
        buttons = [e for e in self.soup.find_all(attrs={'data-des': controls[0]['id']})
                   if set(e.get('class', [])) & {'upload', 'uploads', 'file'}]
        if len(buttons) != 1:
            raise UploadPolicyError(f'无法唯一定位{target}原生上传按钮')
        button = buttons[0]
        kind = next(k for k in ('upload', 'uploads', 'file') if k in button.get('class', []))
        _, code = self.script('mylayui.js')
        variables = {}
        # Native Pboot scripts declare uploadurl; evaluate actual source, not a
        # guessed /Index/upload endpoint. A custom expression must be adapted.
        for i in range(len(code)-2):
            if code[i:i+2] == ['uploadurl', '=']:
                end = code.index(';', i+2)
                variables['uploadurl'] = evaluate(code[i+2:end], variables, self.soup)
                break
        configs = []
        for script in [code] + self.inline:
            for i in range(len(script)-4):
                if script[i:i+5] == ['upload', '.', 'render', '(', '{']:
                    obj, _ = balanced(script, i+4)
                    options = properties(obj)
                    if 'elem' not in options:
                        continue
                    selector = evaluate(options['elem'], variables, self.soup)
                    if selector in ('.'+kind, '#'+str(button.get('id', ''))):
                        configs.append(options)
        if len(configs) != 1:
            raise UploadPolicyError('未能唯一匹配上传按钮的实际脚本配置')
        options = dict(configs[0])
        if button.get('lay-data'):
            options.update(properties(tokens(button['lay-data'])))
        # ``auto:false`` and ``bindAction`` are common Layui presentation
        # options: they only defer the same upload request until a separate
        # button is clicked.  The desktop workflow already defers the actual
        # request until the publish/save task starts, so the request semantics
        # can be preserved without executing page JavaScript.  Validate the
        # selector instead of silently ignoring a typo.  A custom ``choose``
        # callback is different: it may mutate bytes, cancel files, or add
        # fields, therefore only an exact no-op callback is accepted.
        auto_value = True
        if 'auto' in options:
            auto_value = evaluate(options['auto'], variables, self.soup)
            if not isinstance(auto_value, bool):
                raise UploadPolicyError('上传控件auto配置无效')
        bind_action = ''
        if 'bindAction' in options:
            bind_action = evaluate(options['bindAction'], variables, self.soup)
            if not isinstance(bind_action, str) or not bind_action.strip():
                raise UploadPolicyError('上传控件bindAction配置无效')
            try:
                bound = _literal_dom_element(self.soup, bind_action.strip())
            except UploadPolicyError:
                raise UploadPolicyError('上传控件bindAction按钮不存在或选择器尚未支持') from None
            if str(bound.name or '').lower() not in ('button', 'a', 'input'):
                raise UploadPolicyError('上传控件bindAction不是可点击按钮')
        if 'choose' in options:
            choose = options['choose']
            no_op = _is_safe_noop_callback(choose)
            if not no_op:
                raise UploadPolicyError('该上传控件choose脚本可能改变文件，尚未适配')
        if str(evaluate(options.get('method', ['"post"']), variables, self.soup)).lower() != 'post':
            raise UploadPolicyError('该控件的自定义上传方法尚未适配')
        endpoint = evaluate(options.get('url', []), variables, self.soup)
        before = options.get('before', [])
        watermark = False
        if before:
            # Recognize the native watermark-only before callback. Any other
            # before handler may mutate bytes, data or the request destination.
            expected = tokens("function(obj){if($(this.item).hasClass('watermark')){INST.config.url=uploadurl+'/watermark/1';}}")
            normalized = list(before)
            # Standard Pboot displays a spinner after selecting the endpoint.
            # Remove only this exact UI-only statement, not arbitrary calls.
            spinner = tokens('layer.load();')
            for i in range(len(normalized)-len(spinner), -1, -1):
                if normalized[i:i+len(spinner)] == spinner:
                    del normalized[i:i+len(spinner)]
            if normalized == tokens('function(obj){}'):
                normalized = expected
                before = []
            if len(normalized) == len(expected):
                for i, t in enumerate(expected):
                    if t == 'INST':
                        normalized[i] = 'INST'
            if normalized != expected:
                # Single and double quoted spellings have the same semantics.
                norm = lambda ts: [quoted(t) if t[0] in ('"', "'") else t for t in ts]
                if norm(normalized) != norm(expected):
                    raise UploadPolicyError('该上传控件before脚本尚未适配')
            if before and 'watermark' in button.get('class', []):
                endpoint = variables['uploadurl'] + '/watermark/1'
                watermark = True
        field_name = evaluate(options.get('field', []), variables, self.soup)
        if not isinstance(field_name, str) or not field_name:
            raise UploadPolicyError('上传文件字段未声明或无效')
        data = evaluate(options.get('data', ['{', '}']), variables, self.soup)
        headers = evaluate(options.get('headers', ['{', '}']), variables, self.soup)
        if not isinstance(data, dict) or not isinstance(headers, dict):
            raise UploadPolicyError('上传data/headers不是对象')
        if any(k.lower() in ('host', 'cookie', 'authorization', 'content-length', 'content-type') for k in headers):
            raise UploadPolicyError('自定义上传认证/传输头尚未适配')
        accept_value = evaluate(options.get('accept', ['"images"']), variables, self.soup)
        accept_mime = evaluate(options.get('acceptMime', ['""']), variables, self.soup)
        if not isinstance(accept_mime, str):
            raise UploadPolicyError('上传控件acceptMime配置无效')
        accept_mime = ','.join(item.strip().lower()
                              for item in accept_mime.split(',') if item.strip())
        if accept_mime and any(not re.fullmatch(
                r'(?:\*/\*|[A-Za-z0-9!#$&^_.+-]+/\*|'
                r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+)', item)
                               for item in accept_mime.split(',')):
            raise UploadPolicyError('上传控件acceptMime格式尚未适配')
        multiple_value = evaluate(options.get('multiple', ['false']), variables, self.soup)
        if not isinstance(multiple_value, bool):
            raise UploadPolicyError('上传控件multiple配置无效')
        number_value = evaluate(options.get('number', ['0']), variables, self.soup)
        if (isinstance(number_value, bool) or
                not isinstance(number_value, (int, float)) or
                int(number_value) < 0):
            raise UploadPolicyError('上传控件number配置无效')
        extensions = evaluate(options.get('exts', ['""']), variables, self.soup)
        maximum = evaluate(options.get('size', ['0']), variables, self.soup)
        if not isinstance(extensions, str) or isinstance(maximum, bool) or not isinstance(maximum, (float, int)) or maximum < 0:
            raise UploadPolicyError('上传格式/大小配置无效')
        # Layui's implicit ``accept: images`` default is itself a format
        # policy even when ``exts`` is omitted.  Mirror the browser default
        # instead of treating an empty literal as unrestricted.
        if not extensions and isinstance(accept_value, str) and accept_value.lower() in ('image', 'images'):
            extensions = 'jpg|png|gif|bmp|jpeg'
        if extensions and not re.fullmatch(r'\.?[A-Za-z0-9]+(?:\|\.?[A-Za-z0-9]+)*', extensions):
            raise UploadPolicyError('该上传控件使用正则格式规则，尚未适配')
        return UploadPolicy(safe_url(endpoint, self.base), str(field_name), self.page_url,
            'field', target, tuple('.'+e.lower().lstrip('.') for e in extensions.split('|') if e),
            int(maximum * 1024), data, headers, metadata={
                'button_class': kind, 'watermark': watermark, 'accept': accept_value,
                'accept_mime': accept_mime,
                # Stock ``.uploads`` controls often target a plain text input,
                # so the DOM descriptor alone does not reveal their callback
                # queue shape. Preserve the native Layui flag in the policy
                # evidence and make it available to later workers.
                'multiple': multiple_value,
                # Layui uses ``number`` as the maximum number of files in a
                # selection.  Preserve the literal queue limit so the
                # desktop picker can reject an over-sized selection before
                # any upload request is sent.
                'number': int(number_value),
                'auto': auto_value, 'bind_action': bind_action,
                'deferred_upload': not auto_value,
            })

    def editor(self, target, media_kind='image'):
        elements = [e for e in self.soup.find_all(['textarea', 'script']) if e.get('name') == target]
        if len(elements) != 1 or not elements[0].get('id'):
            raise UploadPolicyError('无法唯一定位目标正文编辑器')
        identity = elements[0]['id']
        config_url, code = self.script('ueditor.config.js')
        variables = {}
        runtime_overrides = {}
        serverparam = {}
        serverparam_seen = False
        editor_vars = {}
        for script in self.inline:
            if any(word in script for word in ('setUploadData', 'getActionUrl', 'UEDITOR_CONFIG')):
                raise UploadPolicyError('编辑器运行时上传选项覆盖尚未适配')
            for name in ('window.UEDITOR_HOME_URL', 'window.__msCDN', 'window.__msRoot'):
                lhs = tokens(name) + ['=']
                for i in range(len(script)-len(lhs)):
                    if script[i:i+len(lhs)] == lhs:
                        if i and script[i-1] != ';':
                            raise UploadPolicyError('编辑器根目录含条件或动态赋值，尚未适配')
                        end = script.index(';', i+len(lhs))
                        variables[name] = evaluate(script[i+len(lhs):end], variables, self.soup)
            if 'setOpt' in script:
                runtime_overrides.update(
                    _static_editor_setopt(script, identity, variables, self.soup))
            if 'execCommand' in script or 'getEditor' in script:
                operations, editor_vars = _static_editor_serverparam(
                    script, identity, variables, self.soup, editor_vars)
                for operation, value in operations:
                    serverparam_seen = True
                    if operation == 'clear':
                        serverparam.clear()
                    elif operation == 'delete':
                        serverparam.pop(value, None)
                    elif operation == 'merge':
                        serverparam.update(value)
                    else:
                        key, item = value
                        serverparam[key] = item
        # Recognize the actual standard root resolver; an unrelated/custom
        # assignment to URL must not silently inherit the script directory.
        root_expressions = [tokens(value) for value in (
            'window.UEDITOR_HOME_URL', "window.__msCDN+'asset/vendor/ueditor/'",
            "window.__msRoot+'asset/vendor/ueditor/'", 'getUEBasePath()',
            'window.UEDITOR_HOME_URL || getUEBasePath()')]
        normalize = lambda ts: [quoted(t) if t[0] in ('"', "'") else t for t in ts]
        root_expressions = [normalize(x) for x in root_expressions]
        assignments = []
        for i in range(len(code)-2):
            if code[i:i+2] == ['URL', '=']:
                end = code.index(';', i+2)
                expression = normalize(code[i+2:end])
                if expression not in root_expressions:
                    raise UploadPolicyError('UEditor使用了自定义根目录计算，尚未适配')
                assignments.append(expression)
        root = None
        if assignments == root_expressions[:4]:
            root = variables.get('window.UEDITOR_HOME_URL')
            if not root:
                root = next((variables[k]+'asset/vendor/ueditor/' for k in ('window.__msCDN', 'window.__msRoot') if variables.get(k)), None)
        elif assignments == [root_expressions[4]]:
            root = variables.get('window.UEDITOR_HOME_URL')
        elif assignments and assignments != [root_expressions[3]]:
            raise UploadPolicyError('UEditor根目录解析顺序尚未适配')
        if assignments:
            variables['URL'] = urljoin(self.base, root) if root else urljoin(config_url, './')
        public = None
        for i in range(len(code)-4):
            if code[i:i+4] == ['window', '.', 'UEDITOR_CONFIG', '='] and code[i+4] == '{':
                obj, _ = balanced(code, i+4); public = properties(obj)
        if public is None:
            raise UploadPolicyError('无法解析当前UEditor公共配置')
        instance = None
        retrieval_seen = False
        for script in self.inline:
            for i in range(len(script)-5):
                if script[i:i+4] != ['UE', '.', 'getEditor', '(']:
                    continue
                args, _ = balanced(script, i+3)
                parts = split_top(args[1:-1], ',')
                if evaluate(parts[0], variables, self.soup) != identity:
                    continue
                # UEditor pages commonly call ``UE.getEditor('editor')``
                # again from submit/click handlers to retrieve the existing
                # instance.  That is not a second initialization and carries
                # no upload configuration.  Only calls with an options object
                # can override the effective policy; multiple such calls are
                # still ambiguous and remain fail-closed.
                if len(parts) == 1:
                    retrieval_seen = True
                    continue
                if instance is not None:
                    raise UploadPolicyError('同一编辑器有多个初始化，尚未适配')
                instance = properties(parts[1]) if len(parts) == 2 else {}
        if instance is None and retrieval_seen:
            # A page may rely entirely on the public UEditor configuration
            # and call getEditor without an inline options object.
            instance = {}
        if instance is None:
            raise UploadPolicyError('没有取得目标编辑器的实例初始化配置')
        instance.update(runtime_overrides)
        merged = dict(public); merged.update(instance)
        endpoint = safe_url(evaluate(merged.get('serverUrl', []), variables, self.soup), self.base)
        p = urlsplit(endpoint)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != 'action']
        config_request = urlunsplit(p._replace(query=urlencode(query+[('action', 'config')]), fragment=''))
        try:
            server = self.get(config_request).json()
        except (ValueError, TypeError):
            raise UploadPolicyError('编辑器服务端未返回有效配置') from None
        if not isinstance(server, dict):
            raise UploadPolicyError('编辑器服务端配置不是对象')
        # UEditor loads server options, then page instance options take priority.
        options = {k: evaluate(v, variables, self.soup) for k, v in merged.items()
                   if (k.startswith(('image', 'video', 'audio', 'file', 'catcher')) or
                       k in ('serverUrl', 'maximumWords', 'catchRemoteImageEnable'))}
        options.update(server)
        options.update({k: evaluate(v, variables, self.soup) for k, v in instance.items()
                        if (k.startswith(('image', 'video', 'audio', 'file', 'catcher')) or
                        k in ('maximumWords', 'catchRemoteImageEnable'))})
        media_kind = str(media_kind or 'image').strip().lower()
        if media_kind not in ('image', 'video', 'audio', 'file'):
            raise UploadPolicyError('编辑器媒体类型未适配')
        prefix = media_kind
        action = options.get(f'{prefix}ActionName')
        image_field_name = options.get(f'{prefix}FieldName')
        allowed, maximum = options.get(f'{prefix}AllowFiles'), options.get(f'{prefix}MaxSize')
        # UEditor installations commonly expose audio through the generic
        # file uploader instead of declaring a second audio action.  Treat
        # that as an explicit compatibility fallback only when the audio
        # keys are entirely absent; never guess an endpoint or field.
        fallback = ''
        if media_kind == 'audio' and not any(
                options.get(key) for key in ('audioActionName', 'audioFieldName',
                                             'audioAllowFiles', 'audioMaxSize')):
            prefix = 'file'
            action = options.get('fileActionName')
            image_field_name = options.get('fileFieldName')
            allowed, maximum = options.get('fileAllowFiles'), options.get('fileMaxSize')
            fallback = 'file'
        if not isinstance(action, str) or not action or not isinstance(image_field_name, str) or not image_field_name:
            raise UploadPolicyError(f'编辑器未声明{media_kind}上传动作/字段')
        if not isinstance(allowed, list) or not allowed or not all(isinstance(x, str) and x.startswith('.') for x in allowed):
            raise UploadPolicyError('编辑器允许图片格式配置无效')
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum <= 0:
            raise UploadPolicyError('编辑器图片大小限制无效')
        compression = options.get('imageCompressEnable') if media_kind == 'image' else False
        if not isinstance(compression, bool):
            raise UploadPolicyError('编辑器未明确声明图片压缩策略，尚未适配默认规则')
        compression_options = {}
        if compression:
            # UEditor performs this transform in the browser before the
            # multipart request.  Keep the values from the effective
            # instance/server configuration so the desktop path can perform
            # the same deterministic transform instead of silently refusing
            # an otherwise valid native upload.
            border = options.get('imageCompressBorder', 1600)
            quality = options.get('imageCompressQuality', 80)
            if (isinstance(border, bool) or not isinstance(border, (int, float))
                    or border <= 0 or border > 20000):
                raise UploadPolicyError('编辑器图片压缩边界无效')
            if (isinstance(quality, bool) or not isinstance(quality, (int, float))
                    or quality <= 0 or quality > 100):
                raise UploadPolicyError('编辑器图片压缩质量无效')
            compression_options = {
                'enabled': True,
                'border': int(border),
                'quality': int(quality),
            }
        url_prefix = options.get(f'{prefix}UrlPrefix', '')
        if not url_prefix and prefix != 'image':
            # Older UEditor server configs only expose imageUrlPrefix for all
            # editor upload actions.  Reuse it only as an explicit fallback;
            # an absent prefix remains the browser's relative response.
            url_prefix = options.get('imageUrlPrefix', '')
        if not isinstance(url_prefix, str):
            raise UploadPolicyError(f'编辑器{media_kind}URL前缀无效')
        if url_prefix:
            safe_url(url_prefix, self.base)
        upload_query = query + [('action', action)]
        if serverparam_seen:
            # UEditor's autoupload/simpleupload code appends serverparam to
            # the action URL query, not to multipart form data.  Use the same
            # encodeURIComponent spelling (not form-urlencoded '+') and keep
            # array values as repeated ``key[]`` pairs.
            upload_query.extend(_editor_serverparam_pairs(serverparam))
        upload_query_text = '&'.join(
            quote(str(key), safe="-_.!~*'()") + '=' +
            quote(str(value), safe="-_.!~*'()")
            for key, value in upload_query)
        upload_url = urlunsplit(p._replace(query=upload_query_text, fragment=''))
        metadata = {k: options[k] for k in (
            'imageCompressEnable', 'imageCompressBorder',
            'imageCompressQuality', 'imageInsertAlign', 'maximumWords',
            'videoActionName', 'videoFieldName', 'videoMaxSize', 'videoAllowFiles',
            'audioActionName', 'audioFieldName', 'audioMaxSize', 'audioAllowFiles',
            'fileActionName', 'fileFieldName', 'fileMaxSize', 'fileAllowFiles',
            'catchRemoteImageEnable', 'catcherActionName',
            'catcherFieldName', 'catcherMaxSize', 'catcherAllowFiles')
            if k in options}
        metadata.update({'media_kind': media_kind, 'action_name': action,
                         'field_name': image_field_name})
        if serverparam_seen:
            metadata['serverparam_keys'] = tuple(serverparam.keys())
        if fallback:
            metadata['media_kind_fallback'] = fallback
        # UEditor's remote-image catcher is a separate action on the same
        # controller.  Keep its effective server values in policy metadata so
        # workers can opt in only when the current page explicitly enables it;
        # do not infer this behavior from a hard-coded site default.
        if media_kind == 'image' and options.get('catchRemoteImageEnable') is True:
            action_name = options.get('catcherActionName')
            catcher_field_name = options.get('catcherFieldName')
            max_size = options.get('catcherMaxSize')
            allow_files = options.get('catcherAllowFiles')
            if not isinstance(action_name, str) or not action_name.strip():
                raise UploadPolicyError('编辑器远程图片抓取动作未声明')
            if not isinstance(catcher_field_name, str) or not catcher_field_name.strip():
                raise UploadPolicyError('编辑器远程图片抓取字段未声明')
            if isinstance(max_size, bool) or not isinstance(max_size, (int, float)) or max_size <= 0:
                raise UploadPolicyError('编辑器远程图片抓取大小限制无效')
            if not isinstance(allow_files, list) or not allow_files or not all(
                    isinstance(item, str) and item.startswith('.') for item in allow_files):
                raise UploadPolicyError('编辑器远程图片格式配置无效')
            metadata['remote_catcher'] = {
                'enabled': True,
                'action': action_name.strip(),
                'field': catcher_field_name.strip(),
                'max_bytes': int(max_size),
                'extensions': tuple(item.lower() for item in allow_files),
            }
        if compression_options:
            metadata['client_compress'] = compression_options
        # The normal image upload field and the remote-catcher field are
        # independent UEditor options.  Keep the normal field on the policy;
        # overwriting it while parsing catcher options makes a later ordinary
        # image upload send its multipart file under ``source[]`` (or another
        # catcher-only name), which browsers never do.
        return UploadPolicy(upload_url, image_field_name, self.page_url, 'editor', target,
            tuple(x.lower() for x in allowed), int(maximum), url_prefix=url_prefix,
            metadata=metadata)


def discover_policies(session, page_url, targets):
    discovery = PolicyDiscovery(session, page_url)
    result = {}
    normalized = []
    for item in targets:
        values = tuple(item) if isinstance(item, (list, tuple)) else (item,)
        if len(values) not in (2, 3):
            raise UploadPolicyError('上传目标描述无效')
        surface, target = values[:2]
        kind = str(values[2] if len(values) == 3 else 'image').strip().lower()
        normalized.append((str(surface), str(target), kind))
    for surface, target, kind in sorted(set(normalized)):
        if surface not in ('field', 'editor'):
            raise UploadPolicyError('未知上传控件类型')
        if surface == 'field':
            if kind != 'image':
                raise UploadPolicyError('原生字段上传不接受编辑器媒体类型标记')
            result[(surface, target)] = discovery.native(target)
        else:
            policy = discovery.editor(target, kind)
            # Preserve the historical two-part key for image callers while
            # exposing explicit media variants to attachment/video uploads.
            result[(surface, target, kind)] = policy
            if kind == 'image':
                result[(surface, target)] = policy
    return result
