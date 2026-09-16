"""Compare every submitted business field without claiming backend semantics.

HTML, arrays, order, zero and whitespace are data. Differences are reported,
not silently normalized into success. No field contents/tokens enter reports.
"""
from collections.abc import Mapping


TRANSPORT_FIELDS = frozenset({'formcheck', 'ac', 'id', 'mcode', '_token',
                              '_csrf', 'csrf_token', 'csrfmiddlewaretoken'})


def wire_values(value):
    """requests form encoding omits None and expands ordered list values."""
    values = value if isinstance(value, (list, tuple)) else [value]
    if any(isinstance(v, (Mapping, set, list, tuple)) for v in values):
        raise ValueError('unsupported nested form value')
    return [str(v) for v in values if v is not None]


def submission_expectations(data, descriptors=()):
    expected, absent = {}, set()
    for name, value in data.items():
        if name in TRANSPORT_FIELDS:
            continue
        try:
            values = wire_values(value)
        except ValueError:
            # Keep unsupported values: comparison must explicitly report them,
            # never turn an unrecognized target into a passing omission.
            expected[name] = value
            continue
        if values:
            expected[name] = value
        else:
            absent.add(name)
    for descriptor in descriptors:
        name = descriptor.get('name')
        if not name or name in TRANSPORT_FIELDS or descriptor.get('disabled'):
            continue
        if name not in data and descriptor.get('type') in ('checkbox', 'radio', 'select'):
            absent.add(name)
    return expected, sorted(absent)


def compare_saved_fields(expected, current, descriptors=(), absent=()):
    report = {'status':'unverified', 'matched':[], 'different':[],
              'unverified':[], 'normalization_possible':[]}
    known = {d.get('name') for d in descriptors}
    textarea_names = {d.get('name') for d in descriptors
                      if str(d.get('type', d.get('kind', ''))).lower() == 'textarea'}
    def transport_normalize(name, value):
        values = wire_values(value)
        if name in textarea_names:
            return [str(item).replace('\r\n', '\n').replace('\r', '\n')
                    for item in values]
        return values
    for name, value in expected.items():
        if name in TRANSPORT_FIELDS:
            continue
        if name not in current:
            report['unverified'].append(name)
            continue
        try:
            wanted, actual = wire_values(value), wire_values(current[name])
        except ValueError:
            report['unverified'].append(name)
            continue
        if wanted == actual:
            report['matched'].append(name)
        elif transport_normalize(name, wanted) == transport_normalize(name, actual):
            # Browsers submit textarea line endings as CRLF; many CMS
            # backends immediately store/read them back as LF.  This is a
            # transport normalization, not a lost reply, so it remains
            # verified while being visible to the audit consumer.
            report['matched'].append(name)
            report['normalization_possible'].append(name)
        else:
            report['different'].append(name)
            if (name in ('filename', 'description', 'ico') or
                    [v.strip() for v in wanted] == [v.strip() for v in actual]):
                report['normalization_possible'].append(name)
    for name in absent:
        if name in TRANSPORT_FIELDS:
            continue
        if name in current:
            report['different'].append(name)
        elif name in known:
            report['matched'].append(name)
        elif (name == 'picstitle[]' and 'pics' in known and
              expected.get('pics') == '' and current.get('pics') == ''):
            # Native gallery title controls are generated per image; clearing
            # pics removes every title input. Require the real empty pics
            # control on readback, not arbitrary absence on an incomplete form.
            report['matched'].append(name)
        else:
            report['unverified'].append(name)
    if report['different']:
        report['status'] = 'different'
    elif report['matched'] and not report['unverified']:
        report['status'] = 'verified'
    return report
