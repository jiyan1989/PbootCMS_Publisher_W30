"""One browser-compatible form model for content, products and categories.

Values here describe successful controls, not invented CMS defaults.  Named
text/plain scripts are virtual UEditor textareas; executable scripts are never
interpreted. Duplicate names keep their submission order.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, datetime, time
import os
import re

from asset_types import sniff_mime


def control_type(element):
    if element.name in ('textarea', 'script'):
        return 'textarea'
    if element.name == 'select':
        return 'select'
    if element.name == 'button':
        # HTML buttons default to type=submit.  Treating an untyped button as
        # a text input drops the clicked submitter from browser-order pairs.
        return str(element.get('type', 'submit') or 'submit').lower()
    return str(element.get('type', 'text') or 'text').lower()


def form_elements(form):
    root = form
    while root.parent is not None:
        root = root.parent
    identity = form.get('id')
    for element in root.find_all(['input', 'textarea', 'select', 'button', 'script']):
        explicit_owner = element.get('form')
        if explicit_owner is not None:
            owned = bool(identity and explicit_owner == identity)
        else:
            owned = element.find_parent('form') is form
        if not owned:
            continue
        if element.name == 'script' and (
                not element.get('name') or
                str(element.get('type', '')).lower() != 'text/plain'):
            continue
        yield element


def is_disabled(element):
    if element.has_attr('disabled'):
        return True
    for ancestor in element.parents:
        if ancestor.name != 'fieldset' or not ancestor.has_attr('disabled'):
            continue
        legend = ancestor.find('legend', recursive=False)
        if legend is not None and any(p is legend for p in element.parents):
            continue
        return True
    return False


def option_disabled(option):
    return option.has_attr('disabled') or any(
        p.name == 'optgroup' and p.has_attr('disabled') for p in option.parents)


def option_value(option):
    # HTML option.value falls back to text with ASCII whitespace collapsed.
    return str(option['value']) if option.has_attr('value') else re.sub(
        r'[\t\n\f\r ]+', ' ', option.get_text()).strip(' \t\n\f\r')


def selected_options(element):
    options = element.find_all('option')
    selected = [o for o in options if o.has_attr('selected')]
    if element.has_attr('multiple'):
        return [o for o in selected if not option_disabled(o)]
    if selected:
        selected = selected[-1:]
    else:
        # A size>1 listbox does not automatically select its first item.
        try:
            auto_select = int(element.get('size', '1')) <= 1
        except (TypeError, ValueError):
            auto_select = True
        selected = next(([o] for o in options if not option_disabled(o)), []) if auto_select else []
    return [o for o in selected if not option_disabled(o)]


def raw_value(element):
    if element.name in ('textarea', 'script'):
        # HTML form submission normalizes textarea line endings to CRLF,
        # regardless of how the source document represented them.
        return element.get_text().replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
    kind = control_type(element)
    default = 'on' if kind in ('radio', 'checkbox') else ''
    value = str(element.get('value', default))
    # HTML color controls expose a canonical six-digit lowercase RGB value;
    # an omitted/invalid value is normalized by the browser to black rather
    # than submitted as an empty string.  Mirror that DOM normalization before
    # building the successful-control snapshot.
    if kind == 'color':
        return value.lower() if re.fullmatch(r'#[0-9a-fA-F]{6}', value) else '#000000'
    return _sanitize_browser_value(kind, value, element)


def _sanitize_browser_value(kind, value, attrs=None):
    """Apply the HTML input value-sanitization algorithm we can determine.

    Native constraint validation is intentionally separate from this helper:
    ``novalidate`` suppresses the validity check, but it does not make an
    invalid date/number remain as an arbitrary string in ``FormData``.  The
    browser sanitizes those controls to the empty value before submission.
    This helper only covers deterministic built-in input types; custom widgets
    and page JavaScript remain outside the static adapter and use the native
    webpage fallback.
    """
    text = '' if value is None else str(value)
    kind = str(kind or 'text').lower()
    if kind in ('text', 'search', 'tel', 'url', 'email', 'password', 'hidden'):
        # HTML input value sanitization strips CR/LF from single-line controls.
        return text.replace('\r', '').replace('\n', '')
    if kind == 'number':
        if not text:
            return ''
        # Valid floating-point number syntax (HTML, not Python's Decimal
        # extensions such as NaN/Infinity).  Keep a valid spelling intact;
        # FormData uses the control's sanitized string, not a re-serialized
        # Python number.
        if not re.fullmatch(
                r'[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))'
                r'(?:[eE][+-]?[0-9]+)?', text):
            return ''
        try:
            if not Decimal(text).is_finite():
                return ''
        except (InvalidOperation, ValueError, TypeError):
            return ''
        return text
    if kind == 'range':
        # A range control never submits an arbitrary invalid/out-of-bounds
        # string.  Its browser value sanitization falls back to the midpoint
        # (then the nearest step); a valid in-range spelling is preserved so
        # a separate stepMismatch check can still mirror native validation.
        try:
            minimum = Decimal(str((attrs or {}).get('min', '0') or '0'))
            maximum = Decimal(str((attrs or {}).get('max', '100') or '100'))
            if not minimum.is_finite() or not maximum.is_finite():
                raise InvalidOperation
            if maximum < minimum:
                maximum = minimum
            try:
                number = Decimal(text) if text else None
            except (InvalidOperation, ValueError, TypeError):
                number = None
            if number is None or not number.is_finite() or number < minimum or number > maximum:
                number = (minimum + maximum) / Decimal(2)
                step_raw = str((attrs or {}).get('step', '1') or '1')
                if step_raw != 'any':
                    step = Decimal(step_raw)
                    if step > 0 and step.is_finite():
                        steps = ((number - minimum) / step).to_integral_value(
                            rounding=ROUND_HALF_UP)
                        number = minimum + steps * step
                        number = min(max(number, minimum), maximum)
            return format(number, 'f').rstrip('0').rstrip('.') or '0'
        except (InvalidOperation, ValueError, TypeError):
            return '50'
    if kind in ('date', 'datetime-local', 'time', 'month', 'week'):
        if not text:
            return ''
        # _temporal_value is defined later in this module; raw_value is only
        # called after module initialization, so the lookup is safe.
        try:
            if _temporal_value(kind, text) is None:
                return ''
        except (TypeError, ValueError, OverflowError):
            return ''
        return text
    return text


def _direction_for_text(text):
    for char in str(text or ''):
        code = ord(char)
        # Unicode digits are weak bidi characters; browser ``dir=auto``
        # waits for the first strong L/R/AL character instead of treating a
        # leading number as LTR.
        if ('A' <= char <= 'Z') or ('a' <= char <= 'z'):
            return 'ltr'
        if (0x0590 <= code <= 0x08ff) or (0xfb1d <= code <= 0xfdff) or (0xfe70 <= code <= 0xfefc):
            return 'rtl'
    return 'ltr'


def _text_direction(element):
    """Resolve the browser's effective ``dir`` for a dirname control.

    ``dirname`` is a successful-control companion field for text inputs and
    textareas.  Page scripts may set ``dir`` on the control, form, or an
    ancestor; ``auto`` follows the first strong character.  This small
    deterministic resolver covers the HTML semantics without executing CSS
    or JavaScript.
    """
    current = element
    while current is not None:
        value = str(current.get('dir', '') or '').strip().lower()
        if value in ('ltr', 'rtl'):
            return value
        if value == 'auto':
            return _direction_for_text(raw_value(element))
        current = current.parent
    return 'ltr'


def _has_auto_direction(element):
    current = element
    while current is not None:
        value = str(current.get('dir', '') or '').strip().lower()
        if value in ('ltr', 'rtl'):
            return False
        if value == 'auto':
            return True
        current = current.parent
    return False


def _dirname_pair(element, name, kind):
    if kind in ('checkbox', 'radio', 'file', 'hidden', 'select') or not element.has_attr('dirname'):
        return None
    dirname = str(element.get('dirname', '') or '').strip()
    if not dirname or not re.fullmatch(r'[^\s]+', dirname):
        return None
    return dirname, _text_direction(element)


def successful_pairs(form, *, exclude=(), submitter=None):
    elements = list(form_elements(form))
    last_radio = {}
    for element in elements:
        if control_type(element) == 'radio' and element.has_attr('checked'):
            last_radio[element.get('name')] = element
    pairs = []
    for element in elements:
        name = str(element.get('name', ''))
        kind = control_type(element)
        if not name or name in exclude or is_disabled(element):
            continue
        # Most callers pass the actual BeautifulSoup element, while the
        # transport adapters pass the serializable descriptor returned by
        # ``_discover_default_submitter``.  Treat both forms identically so a
        # clicked button's name/value or image coordinates are not lost.
        clicked = element is submitter
        if isinstance(submitter, dict):
            submit_kind = str(submitter.get('type', 'submit') or 'submit').lower()
            element_kind = kind or ('submit' if element.name == 'button' else '')
            clicked = (element_kind == submit_kind and
                       str(element.get('name', '') or '') ==
                       str(submitter.get('name', '') or '') and
                       str(raw_value(element) if element_kind != 'button' else
                           element.get('value', '') or '') ==
                       str(submitter.get('value', '') or '') and
                       (not submitter.get('formaction') or
                        str(element.get('formaction', '') or '') ==
                        str(submitter.get('formaction', '') or '')))
        if element.name == 'button' or kind in ('button', 'submit', 'reset', 'image', 'file'):
            if clicked and kind not in ('reset', 'file'):
                if kind == 'image':
                    # Native image submitters send click coordinates.  The
                    # desktop bridge normally has no physical click position,
                    # so use the browser-compatible origin coordinate unless
                    # a real WebView click supplied explicit coordinates.
                    try:
                        click_x = int(submitter.get('x', submitter.get('click_x', 0)))
                    except (TypeError, ValueError):
                        click_x = 0
                    try:
                        click_y = int(submitter.get('y', submitter.get('click_y', 0)))
                    except (TypeError, ValueError):
                        click_y = 0
                    prefix = name or ''
                    pairs.extend(((prefix + '.x', str(click_x)),
                                  (prefix + '.y', str(click_y)))
                                  if prefix else (('x', str(click_x)),
                                                  ('y', str(click_y))))
                else:
                    pairs.append((name, raw_value(element)))
            continue
        if kind == 'checkbox' and not element.has_attr('checked'):
            continue
        if kind == 'radio' and last_radio.get(name) is not element:
            continue
        if element.name == 'select':
            pairs.extend((name, option_value(o)) for o in selected_options(element))
        else:
            pairs.append((name, raw_value(element)))
            direction_pair = _dirname_pair(element, name, kind)
            if direction_pair:
                pairs.append(direction_pair)
    return pairs


def serialize_form(form, *, exclude=(), submitter=None):
    elements = list(form_elements(form))
    counts = Counter(e.get('name') for e in elements if control_type(e) != 'radio')
    list_names = {e.get('name') for e in elements
                  if e.has_attr('multiple') or str(e.get('name', '')).endswith('[]') or
                  counts[e.get('name')] > 1}
    values = {}
    for name, value in successful_pairs(form, exclude=exclude, submitter=submitter):
        if name in list_names:
            values.setdefault(name, []).append(value)
        else:
            values[name] = value
    return values


def submission_attributes(form, submitter=None):
    """Return the browser form submission attributes without executing JS.

    CMS forms historically omit ``method`` and expect POST, so callers may
    choose that compatibility default.  Explicit form/submitter overrides are
    preserved exactly and can be rejected by a caller when a write would be
    unsafe.  This keeps action/method/enctype discovery in one place.
    """
    form = form or {}
    action = str(form.get('action', '') or '')
    method = str(form.get('method', '') or '').strip().lower() or 'get'
    enctype = str(form.get('enctype', '') or '').strip().lower() \
        or 'application/x-www-form-urlencoded'
    if submitter is not None:
        action = str(submitter.get('formaction', action) or action)
        method = str(submitter.get('formmethod', method) or method).strip().lower()
        enctype = str(submitter.get('formenctype', enctype) or enctype).strip().lower()
    return {'action': action, 'method': method, 'enctype': enctype}


def validation_disabled(form, submitter=None):
    """Return whether native constraint validation is bypassed for a submit.

    ``novalidate`` belongs to the form and ``formnovalidate`` belongs to the
    clicked submitter.  They do not change which controls are successful or
    which transport is used; they only suppress the browser's constraint
    validation step.  Keeping this as a separate helper avoids changing the
    long-standing transport dictionary consumed by callers and tests.
    """
    if form is not None and form.has_attr('novalidate'):
        return True
    if isinstance(submitter, dict):
        return bool(submitter.get('formnovalidate'))
    return bool(submitter is not None and submitter.has_attr('formnovalidate'))


def _choice_label(element):
    if element.get('title'):
        return str(element['title'])
    parent = element.find_parent('label')
    if parent:
        return parent.get_text(' ', strip=True)
    form = element.find_parent('form')
    label = form.find('label', attrs={'for': element.get('id')}) if form and element.get('id') else None
    return label.get_text(' ', strip=True) if label else raw_value(element)


def _help(element):
    # Layui and common Pboot themes attach field guidance directly to the
    # control instead of rendering a separate help block.  These are inert
    # text attributes, so exposing them does not execute page JavaScript.
    for attr in ('data-tips', 'lay-tips', 'data-content', 'title'):
        direct = str(element.get(attr, '') or '').strip()
        if direct:
            return direct
    item = element.find_parent(class_=re.compile(r'(^|\s)(?:layui-form-item|form-item)(\s|$)'))
    if item is None:
        return str(element.get('placeholder', '') or '').strip()
    helper = item.select_one('.layui-word-aux,.help-block,.form-text,small,.tips[data-content]')
    if helper:
        return str(helper.get('data-content') or helper.get_text(' ', strip=True))
    return str(element.get('placeholder', '') or '').strip()


def describe_form(form, label_resolver, *, exclude=(), submitter=None):
    # ``submitter`` is part of the browser's successful-control set.  The
    # previous implementation used it only for the descriptor's validation
    # metadata, while the returned defaults silently omitted a clicked
    # submit button's name/value (and image submitter coordinates).  That
    # made dynamic modules differ from a real form whenever the save button
    # carried a routing flag or an alternate action.  Serialize the same
    # submitter that the transport resolver uses so callers receive one
    # coherent browser snapshot.
    values = serialize_form(form, exclude=exclude, submitter=submitter)
    # Keep the first DOM position for each named control group.  The public
    # field descriptor does not expose this implementation detail, but the
    # multipart transport uses it to put an empty native file part back where
    # the browser would have emitted it (``successful_pairs`` intentionally
    # omits local file paths).
    elements = list(form_elements(form))
    dom_positions = {id(element): index for index, element in enumerate(elements)}
    groups = {}
    for element in elements:
        name = str(element.get('name', ''))
        if not name or name in exclude or element.name == 'button' or control_type(element) in ('submit','button','reset','image'):
            continue
        groups.setdefault(name, []).append(element)
    named_nonfile_names = {
        str(name) for name, group in groups.items()
        if any(control_type(item) != 'file' for item in group)
    }
    # Layui's stock uploader creates hidden implementation inputs named
    # ``upload`` and pairs them with visible ``button.upload[data-des]``
    # controls targeting a text field such as ``ico``/``pics``/``enclosure``.
    # They remain in the internal descriptor for multipart fidelity, but are
    # marked so public desktop editors do not render a phantom extra field.
    internal_upload_names = set()
    upload_button_exists = False
    for button in form.find_all(['button', 'a', 'input']):
        target = str(button.get('data-des', '') or '').strip()
        classes = ' '.join(button.get('class') or [])
        if (target and target in named_nonfile_names and
                'upload' in classes.lower()):
            upload_button_exists = True
            break
    if upload_button_exists:
        for element in elements:
            if control_type(element) != 'file':
                continue
            classes = ' '.join(element.get('class') or []).lower()
            if 'layui-upload-file' in classes and element.get('name'):
                internal_upload_names.add(str(element.get('name')))
    fields = []
    for name, group in groups.items():
        # Hidden fallback + checkbox represents one visible editor, but both
        # successful values remain in the ordered serialization above.
        element = next((e for e in group if control_type(e) != 'hidden'), group[0])
        kind = control_type(element)
        choices = [e for e in group if control_type(e) == kind]
        options = []
        if kind == 'select':
            options = [dict(value=option_value(o), label=o.get_text(' ',strip=True),
                            disabled=option_disabled(o),
                            data_type=o.get('data-type',''),
                            data_listtpl=o.get('data-listtpl',''),
                            data_contenttpl=o.get('data-contenttpl','')) for o in element.find_all('option')]
            value = [option_value(o) for o in selected_options(element)]
            if not element.has_attr('multiple'):
                value = value[-1] if value else ''
        elif kind in ('checkbox','radio'):
            options = [dict(value=raw_value(e),label=_choice_label(e),disabled=is_disabled(e)) for e in choices]
            checked = [raw_value(e) for e in choices if e.has_attr('checked') and not is_disabled(e)]
            value = checked if kind == 'checkbox' else (checked[-1] if checked else '')
            if kind == 'checkbox' and len(choices) == 1 and not name.endswith('[]'):
                value = checked[0] if checked else []
        else:
            value = values.get(name, raw_value(element))
        disabled = is_disabled(element)
        classes = set(element.get('class', []))
        widget = next((w for w in ('datetime','date','time') if w in classes), '')
        dom_readonly = element.has_attr('readonly')
        readonly = disabled or (dom_readonly and not widget)
        # The browser submits every successful control with the same name in
        # DOM order, even when the author forgot the conventional ``[]``
        # suffix and used two ordinary text inputs.  Mark any grouped name as
        # repeated so merge/validation paths do not stringify a list into
        # ``['a', 'b']`` and silently change the request.
        multiple = (name.endswith('[]') or element.has_attr('multiple') or
                    (len(group) > 1 and kind != 'radio') or
                    (kind == 'checkbox' and len(choices) > 1))
        descriptor = dict(name=name,type=kind,kind=kind,label=label_resolver(form,element,name),
                          value=value,required=element.has_attr('required') or 'required' in str(element.get('lay-verify','')).split('|'),
                          readonly=readonly,dom_readonly=dom_readonly,disabled=disabled,multiple=bool(multiple),
                          widget=widget,options=options,help=_help(element),
                          mappable=kind not in ('hidden','file') and not readonly and name not in ('scode','id','mcode'))
        descriptor['_dom_order'] = min(dom_positions.get(id(item), 0)
                                       for item in group)
        file_orders = [dom_positions.get(id(item), 0) for item in group
                       if control_type(item) == 'file' and not is_disabled(item)]
        if file_orders:
            # Repeated native file controls with the same name are distinct
            # browser entries even when the descriptor groups them together.
            descriptor['_file_dom_orders'] = file_orders
        if name in internal_upload_names:
            descriptor['_upload_internal'] = True
        # Preserve the form-level validation switch with every descriptor so
        # dynamic desktop forms can reproduce the browser's ``novalidate``
        # behavior without guessing at site JavaScript.
        descriptor['form_novalidate'] = validation_disabled(form, submitter)
        if element.has_attr('dirname'):
            descriptor['dirname_direction'] = _text_direction(element)
            descriptor['dirname_auto'] = _has_auto_direction(element)
        descriptor['hidden_fallback'] = [raw_value(e) for e in group if control_type(e)=='hidden']
        for key in ('min','max','step','maxlength','minlength','pattern','accept','placeholder',
                    'lay-verify','dirname','autocomplete','inputmode','list','size'):
            if element.has_attr(key):
                descriptor[key] = element.get(key)
        fields.append(descriptor)
    return fields, values


def text_value(value):
    """Unlike str(value or ''), preserve a legitimate numeric zero."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


class BrowserFormData(dict):
    """Mapping that keeps the browser's successful-control pair order.

    ``requests`` accepts mappings for URL-encoded forms but ordinary dicts
    collapse repeated names and cannot retain interleaved controls.  The
    mapping base keeps existing callers/tests able to index values, while an
    ``items()`` override exposes the exact ordered pairs to the encoder.
    """

    def __init__(self, pairs=()):
        self._browser_pairs = []
        super().__init__()
        for name, value in pairs or ():
            name = str(name)
            self._browser_pairs.append((name, value))
            if name not in self:
                super().__setitem__(name, value)
            else:
                current = super().__getitem__(name)
                if isinstance(current, list):
                    current.append(value)
                else:
                    super().__setitem__(name, [current, value])

    @classmethod
    def from_data(cls, original_pairs, data):
        """Apply final values to the original DOM order, appending new keys."""
        buckets = {}
        explicit_lists = {}
        for name, value in (data.items() if isinstance(data, dict) else data or ()):
            values = value if isinstance(value, (list, tuple)) else [value]
            key = str(name)
            buckets.setdefault(key, []).extend(values)
            if isinstance(value, (list, tuple)):
                # Keep the caller's list shape in the mapping view as well as
                # in the ordered browser pairs.  In particular, an explicit
                # [] means "clear this repeated control" and must not vanish
                # merely because it has no successful pair to emit.
                explicit_lists[key] = list(values)
        pairs = []
        for name, _old in original_pairs or ():
            key = str(name)
            values = buckets.get(key) or []
            if values:
                pairs.append((key, values.pop(0)))
        for name, values in buckets.items():
            pairs.extend((name, value) for value in values)
        result = cls(pairs)
        for name, values in explicit_lists.items():
            super(BrowserFormData, result).__setitem__(name, list(values))
        return result

    def items(self):
        return list(self._browser_pairs)

    def browser_pairs(self):
        return list(self._browser_pairs)


def browser_values_equal(left, right):
    """Compare form values without collapsing repeated controls or zero.

    Native form snapshots represent ``name[]``/multiple controls as ordered
    lists.  A scalar-vs-list comparison must therefore be explicit; taking
    only the last list item (as several older admin adapters did) can report a
    successful save when one repeated value was silently dropped.
    """
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        left_values = left if isinstance(left, (list, tuple)) else [left]
        right_values = right if isinstance(right, (list, tuple)) else [right]
        return [text_value(item) for item in left_values] == \
            [text_value(item) for item in right_values]
    return text_value(left) == text_value(right)


def normalize_control_value(value, field):
    kind = field.get('type', field.get('kind', 'text'))
    if kind == 'checkbox':
        options = {text_value(o.get('value')) for o in field.get('options', [])}
        if value is False or value is None:
            return []
        if value is True:
            return [next((text_value(o.get('value')) for o in field.get('options', [])
                          if not o.get('disabled')), 'on')]
        # Legacy UI sent "0" for unchecked value=1. Explicit lists are
        # unambiguous even when the real selectable value itself is "0".
        if not isinstance(value, (list,tuple)) and text_value(value) in ('','0') and text_value(value) not in options:
            return []
        return [text_value(v) for v in (value if isinstance(value,(list,tuple)) else [value])]
    if field.get('multiple'):
        return [_sanitize_browser_value(kind, text_value(v), field)
                for v in (value if isinstance(value,(list,tuple)) else [value])]
    if kind in ('select', 'file'):
        return text_value(value)
    return _sanitize_browser_value(kind, text_value(value), field)


def _temporal_value(kind, text):
    """Return a browser-like numeric value for date/time input kinds.

    HTML constraint validation uses different units for the temporal controls:
    days for ``date``, seconds for ``time``/``datetime-local``, months for
    ``month`` and weeks for ``week``.  Keeping the conversion here avoids
    comparing human-readable strings (which would mishandle invalid dates and
    step bases) while remaining independent of a local timezone.
    """
    value = str(text or "")
    try:
        if kind == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return None
            return Decimal((date.fromisoformat(value) - date(1970, 1, 1)).days), Decimal(1), Decimal(0)
        if kind == "datetime-local":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?", value):
                return None
            parsed = datetime.fromisoformat(value)
            epoch = datetime(1970, 1, 1)
            return Decimal(str((parsed - epoch).total_seconds())), Decimal(60), Decimal(0)
        if kind == "time":
            if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?", value):
                return None
            parsed = time.fromisoformat(value)
            seconds = (parsed.hour * 3600 + parsed.minute * 60 +
                       parsed.second + parsed.microsecond / 1_000_000)
            return Decimal(str(seconds)), Decimal(60), Decimal(0)
        if kind == "month":
            match = re.fullmatch(r"(\d{4})-(\d{2})", value)
            if not match or not 1 <= int(match.group(2)) <= 12:
                return None
            return Decimal(int(match.group(1)) * 12 + int(match.group(2)) - 1), Decimal(1), Decimal(1970 * 12)
        if kind == "week":
            match = re.fullmatch(r"(\d{4})-W(\d{2})", value)
            if not match:
                return None
            parsed = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
            epoch = date.fromisocalendar(1970, 1, 1)
            return Decimal((parsed - epoch).days // 7), Decimal(1), Decimal(0)
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def merge_form_updates(current, descriptors, updates):
    """Apply explicit changes without manufacturing unchecked POST values."""
    result = dict(current)
    by_name = {f['name']:f for f in descriptors}
    for name, supplied in updates.items():
        field = by_name.get(name)
        if field is None:
            result[name] = supplied
            continue
        if field.get('disabled'):
            result.pop(name, None)
            continue
        value = normalize_control_value(supplied, field)
        kind = field.get('type', field.get('kind', 'text'))
        if kind == 'checkbox':
            combined = list(field.get('hidden_fallback', [])) + value
            if not combined:
                result.pop(name, None)
            elif field.get('multiple') or field.get('hidden_fallback'):
                result[name] = combined
            else:
                result[name] = combined[0]
        elif kind == 'select' and field.get('multiple') and not value:
            result.pop(name, None)
        elif kind == 'radio' and value == '':
            result.pop(name, None)
        else:
            result[name] = value
    # Browsers recompute a dirname companion when a dir=auto text control is
    # edited. Fixed ltr/rtl controls retain their original direction.
    for field in descriptors or []:
        dirname = str(field.get('dirname', '') or '').strip()
        name = str(field.get('name', '') or '')
        if not dirname or name not in result:
            continue
        if field.get('dirname_auto') and name in updates:
            result[dirname] = _direction_for_text(result.get(name, ''))
        elif dirname not in result:
            result[dirname] = str(field.get('dirname_direction', 'ltr') or 'ltr')
    return result


def validate_control_value(value, field):
    """Portable constraints; the WebView also runs native checkValidity().

    Custom JavaScript validators remain backend-owned and must not be guessed.
    """
    # A browser skips native constraint validation when the owning form has
    # ``novalidate`` (or the clicked submitter has ``formnovalidate``).  The
    # server may still reject the request, but the desktop preflight must not
    # invent a client-side rejection that the web flow would not make.
    if (field or {}).get('form_novalidate'):
        return ''
    kind = field.get('type', field.get('kind', 'text'))
    supplied = value if isinstance(value, list) else [value]
    if field.get('multiple') and field.get('max_files'):
        try:
            maximum_files = int(field.get('max_files'))
        except (TypeError, ValueError):
            maximum_files = 0
        if maximum_files > 0 and len(supplied) > maximum_files:
            return f'最多选择 {maximum_files} 个文件'
    if field.get('required'):
        if kind == 'checkbox' or field.get('multiple'):
            present = bool(supplied) and value != []
        else:
            present = value != '' and value is not None
        if 'required' in str(field.get('lay-verify','')).split('|') and isinstance(value,str):
            present = bool(value.strip())
        if not present:
            return '不能为空'
    options = field.get('options') or []
    if options:
        allowed = {text_value(o.get('value')) for o in options if not o.get('disabled')}
        if any(text_value(v) not in allowed for v in supplied):
            return '不是后台允许的选项'
    if kind not in ('checkbox','radio','select'):
        for item in supplied:
            text = text_value(item)
            verify_rules = {rule.strip().lower() for rule in
                            str(field.get('lay-verify', '') or '').split('|') if rule.strip()}
            # Layui's built-in validators are deterministic and can be
            # mirrored without executing arbitrary page JavaScript. Unknown
            # rule names remain page-owned and are deliberately not guessed.
            if text and 'email' in verify_rules and not re.fullmatch(
                    r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                return '必须是有效邮箱地址'
            if text and 'url' in verify_rules and not re.match(
                    r'^[A-Za-z][A-Za-z0-9+.-]*://[^\s]+$', text):
                return '必须是有效 URL'
            if text and 'number' in verify_rules:
                try:
                    Decimal(text)
                except (InvalidOperation, ValueError, TypeError):
                    return '必须是有效数值'
            if text and 'date' in verify_rules and not re.fullmatch(
                    r'\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?', text):
                return '必须是有效日期'
            if text and 'identity' in verify_rules and not re.fullmatch(
                    r'(?:\d{15}|\d{17}[\dXx])', text):
                return '身份证号格式无效'
            if text and 'phone' in verify_rules and not re.fullmatch(r'1\d{10}', text):
                return '手机号格式无效'
            if text and 'password' in verify_rules and not re.fullmatch(r'[^\s]{6,12}', text):
                return '密码长度应为 6～12 位'
            # HTML's pattern constraint is implicitly anchored to the whole
            # value (as if ``^(?:pattern)$``).  Keep this validation local and
            # deterministic; site-specific Layui validators are still left to
            # the page and are never guessed here.
            pattern = field.get('pattern')
            if pattern and text:
                try:
                    if re.fullmatch(str(pattern), text) is None:
                        return '不符合后台格式限制 pattern'
                except re.error:
                    # A malformed pattern is a page/configuration problem;
                    # do not reject a value using a different regex dialect.
                    pass
            for limit, comparison in (('maxlength',lambda n:n>0),('minlength',lambda n:n<0)):
                if field.get(limit) is not None and text:
                    try:
                        # DOM maxlength counts UTF-16 code units.
                        size = len(text.encode('utf-16-le')) // 2
                        if comparison(size-int(field[limit])):
                            return f'不符合长度限制 {limit}={field[limit]}'
                    except (TypeError,ValueError):
                        pass
            if kind in ('date', 'datetime-local', 'time', 'month', 'week') and text:
                parsed = _temporal_value(kind, text)
                if parsed is None:
                    return f'必须是有效{kind}值'
                numeric, default_step, default_base = parsed
                minimum = maximum = None
                for key, target in (('min', 'minimum'), ('max', 'maximum')):
                    raw = field.get(key)
                    if raw in ('', None):
                        continue
                    parsed_limit = _temporal_value(kind, str(raw))
                    if parsed_limit is None:
                        # A malformed page constraint is not a reason to
                        # invent a different rule; let the native page
                        # validity layer decide it.
                        continue
                    if target == 'minimum':
                        minimum = parsed_limit[0]
                    else:
                        maximum = parsed_limit[0]
                if minimum is not None and numeric < minimum:
                    return '早于后台允许的最小时间'
                if maximum is not None and numeric > maximum:
                    return '晚于后台允许的最大时间'
                raw_step = field.get('step')
                if raw_step not in ('', None, 'any'):
                    try:
                        step = Decimal(str(raw_step))
                        if step <= 0:
                            step = default_step
                    except (InvalidOperation, ValueError, TypeError):
                        step = default_step
                    # week values are already expressed in weeks; all other
                    # conversions use their native HTML unit.
                else:
                    step = default_step
                base = minimum if minimum is not None else default_base
                if step > 0 and (numeric - base) % step:
                    return '不符合后台时间步长'
            if kind == 'email' and text:
                # This intentionally mirrors the browser's basic single-value
                # email validity check, while leaving site-specific Layui
                # validators to the page itself.
                email_values = [part.strip() for part in text.split(',')] \
                    if field.get('multiple') else [text]
                if (not email_values or
                        any(not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", part)
                            for part in email_values)):
                    return '必须是有效邮箱地址'
            if kind == 'url' and text:
                if not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://[^\s]+$', text):
                    return '必须是有效 URL'
            if kind == 'color' and text:
                # HTML ``color`` controls submit a canonical six-digit RGB
                # value.  Alpha colors and named colors are not successful
                # native values, even though a site-specific widget may use
                # a text input for those formats instead.
                if not re.fullmatch(r'#[0-9A-Fa-f]{6}', text):
                    return '必须是有效颜色值'
            if kind in ('number', 'range') and text:
                try:
                    number = Decimal(text)
                    if not number.is_finite():
                        return '必须是有限数值'
                    low = Decimal(str(field['min'])) if field.get('min') not in ('',None) else None
                    high = Decimal(str(field['max'])) if field.get('max') not in ('',None) else None
                    if low is not None and number < low or high is not None and number > high:
                        return '超出后台允许的数值范围'
                    step = field.get('step','1')
                    if step != 'any':
                        step = Decimal(str(step))
                        if step > 0 and (number-(low if low is not None else Decimal(0))) % step:
                            return '不符合后台数值步长'
                except (InvalidOperation,ValueError,TypeError):
                    return '必须是有效数值'
    return ''


def upload_field_is_image(field, path=''):
    """Whether a native/text upload control should use image semantics.

    A ``type=file`` field is not necessarily an image (PDF/video/audio
    attachments are common in custom CMS modules).  Callers must choose the
    generic upload endpoint for those fields rather than passing them through
    image magic-byte validation.  Empty ``accept`` is only treated as an
    image for the conventional Pboot image field names.
    """
    field = field or {}
    path_value = str(path or '').split('?', 1)[0].split('#', 1)[0]
    suffix = os.path.splitext(path_value)[1].lower()
    image_exts = ('.jpg', '.jpeg', '.jpe', '.png', '.gif', '.webp', '.bmp',
                  '.svg', '.avif', '.tif', '.tiff', '.ico', '.heic',
                  '.heif', '.jxl', '.jp2', '.j2k', '.jpf', '.jpx', '.jpm',
                  '.psd')
    if suffix.endswith(image_exts):
        return True
    if suffix and any(suffix.endswith(ext) for ext in
                      ('.pdf', '.mp3', '.wav', '.ogg', '.oga', '.m4a', '.aac',
                       '.flac', '.opus', '.amr', '.ape', '.mid', '.midi',
                       '.mka', '.wma', '.caf', '.ac3',
                       '.mp4', '.m4v', '.webm', '.ogv',
                       '.mov', '.avi', '.mkv', '.3gp', '.3g2', '.flv', '.wmv',
                       '.asf', '.rm', '.rmvb', '.ts', '.mts', '.m2ts',
                       '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
                       '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                       '.otf', '.ttf', '.woff', '.woff2', '.eot')):
        return False
    # A native file control may receive an extensionless asset. When the field
    # itself has no accept/name hint, use only a bounded local signature to
    # choose the image-vs-generic uploader; never infer from a remote URL.
    if not suffix and path and os.path.isfile(str(path)):
        try:
            with open(str(path), 'rb') as handle:
                detected = sniff_mime(handle.read(128 * 1024), path)
            if detected.startswith('image/'):
                return True
            if detected.startswith(('video/', 'audio/', 'application/')):
                return False
        except (OSError, IOError):
            pass
    accept = str(field.get('accept', '') or '').lower()
    if accept:
        if 'image/*' in accept:
            return True
        return any(ext in accept for ext in image_exts) and not any(
            token in accept for token in ('application/pdf', 'video/', 'audio/'))
    name = str(field.get('name', '') or '').lower()
    return name in {'ico', 'pic', 'image', 'thumb', 'thumbnail', 'cover', 'logo', 'avatar'}
