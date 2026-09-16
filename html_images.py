"""Extract, upload-map and rewrite <img> sources embedded in generated SEO HTML.

内容生成端在 content 里直接写 `<img src="images/01.jpg" alt="...">` 时，
本模块负责：
  1) 扫描出可上传的【本地图片】（相对/绝对路径，按 HTML 所在目录解析）；
  2) 上传后原位替换 img/picture 的 src、srcset 和常见懒加载地址。
     默认不改变尺寸与样式；用户明确选择尺寸策略时才调整布局属性。

设计取舍：
  - 只自动上传本地文件。`data:` 内联与 `http(s)` 外链**不改动**，仅作为问题项
    报告给调用方（外链常常本来就是已托管的正式图，擅自搬运会有副作用）。
  - 重写只改图片节点的目标属性值，不整体重新序列化 HTML，
    避免破坏正文其它结构与已有内联样式。
"""
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from content_draft import image_style_attrs
from html_fragments import HTMLFragments, attribute_spans, rewrite_attributes
from asset_types import sniff_mime

KIND_LOCAL = "local"
KIND_DATA = "data"
KIND_REMOTE = "remote"
KIND_SITE = "site"

_BROWSER_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
    "image/bmp", "image/tiff", "image/svg+xml", "image/x-icon",
    "image/vnd.microsoft.icon",
}


def media_type_supported(value):
    """Return whether a common picture ``source[type]`` is browser-safe.

    Unknown types are not selected implicitly: a real browser checks decoder
    support before choosing a source, while the desktop worker cannot probe
    every server-provided codec without fetching it.
    """
    text = str(value or "").split(";", 1)[0].strip().lower()
    return not text or text in _BROWSER_IMAGE_TYPES



def classify_src(src):
    """Return KIND_LOCAL / KIND_DATA / KIND_REMOTE, or None for an empty src."""
    value = str(src or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("data:"):
        return KIND_DATA
    if lowered.startswith(("http://", "https://", "//")):
        return KIND_REMOTE
    # A root-relative path is a website URL in a browser, not a path rooted
    # at the Windows drive/current process.  Treating /static/a.jpg as a
    # local file could upload an unrelated file when a matching path happens
    # to exist on the machine.  Explicit file:/// and drive paths remain
    # local and are handled by resolve_local_src.
    if value.startswith("/") and not re.match(r"^/[A-Za-z]:[\\/]", value):
        return KIND_SITE
    return KIND_LOCAL


def resolve_local_src(src, base_dir):
    """Resolve a local img src to an existing absolute file path, else None.

    相对路径以 HTML 文件所在目录为基准；同时处理 URL 转义（%20）、
    file:/// 前缀、查询串/锚点与反斜杠写法。
    """
    raw = str(src or "").strip()
    if not raw:
        return None
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    if raw.lower().startswith("file:"):
        parsed = urlparse(raw)
        raw = unquote(parsed.path or "")
        # file:///C:/x -> /C:/x，需去掉前导斜杠才是合法 Windows 路径
        if re.match(r"^/[A-Za-z]:", raw):
            raw = raw[1:]
    else:
        raw = unquote(raw)
    raw = raw.replace("\\", os.sep)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(base_dir or ".") / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return str(resolved) if resolved.is_file() else None


def document_base_url(html, base_url=""):
    """Resolve the document's first HTML ``<base href>`` safely.

    Imported HTML is normally rooted at its local file directory, but a
    browser gives a document ``<base>`` element precedence for every relative
    resource.  We only return an absolute HTTP(S)/file URL; unknown schemes or
    malformed values are deliberately ignored so a local file is never
    uploaded merely because a page contains an exotic base tag.
    """
    source = str(html or "")
    try:
        node = next(iter(HTMLFragments(source).find("base")), None)
        href = str((node or {}).get("attrs", {}).get("href", "") or "").strip()
    except Exception:
        return ""
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.scheme.lower() in ("http", "https", "file"):
        return href
    reference = str(base_url or "").strip()
    if not reference:
        return ""
    reference_parsed = urlparse(reference)
    if reference_parsed.scheme.lower() not in ("http", "https", "file"):
        return ""
    try:
        resolved = urljoin(reference, href)
    except ValueError:
        return ""
    return resolved if urlparse(resolved).scheme.lower() in ("http", "https", "file") else ""


def resolve_document_src(src, base_dir, document_base=""):
    """Return ``(kind, resolved)`` using browser ``<base>`` semantics.

    ``resolved`` is absolute only when a base URL is available.  Without a
    base, behaviour remains the established local-file/root-site split.
    """
    raw = str(src or "").strip()
    base = str(document_base or "").strip()
    if base and raw and not urlparse(raw).scheme and not raw.lower().startswith(("data:", "blob:", "javascript:")):
        try:
            resolved = urljoin(base, raw)
        except ValueError:
            resolved = raw
        parsed = urlparse(resolved)
        if parsed.scheme.lower() in ("http", "https"):
            return KIND_REMOTE, resolved
        if parsed.scheme.lower() == "file":
            local = resolve_local_src(resolved, base_dir)
            return KIND_LOCAL, local or resolved
    return classify_src(raw), raw




def iter_img_tags(html):
    """Yield real img nodes, ignoring comments/script strings and quoted >."""
    source = str(html or "")
    for node in HTMLFragments(source).find('img'):
        tag = source[node['start']:node['open']]
        attrs = {a['name']: a['value'] for a in attribute_spans(tag)}
        yield tag, attrs.get('src') or '', attrs.get('alt') or ''


def srcset_url_spans(value):
    """Locate candidate URLs using the HTML srcset tokenization boundaries.

    Commas inside a URL (notably data:) aren't blindly split. Descriptors and
    their original whitespace are left untouched.
    """
    at, length = 0, len(value)
    whitespace = ' \t\r\n\f'
    while at < length:
        while at < length and (value[at] in whitespace or value[at] == ','):
            at += 1
        start = at
        while at < length and value[at] not in whitespace:
            at += 1
        end = at
        while end > start and value[end-1] == ',':
            end -= 1
        if end > start:
            yield start, end
        if end < at:
            continue
        depth = 0
        while at < length:
            char = value[at]
            at += 1
            if char == '(':
                depth += 1
            elif char == ')':
                depth = max(0, depth-1)
            elif char == ',' and not depth:
                break


def _parse_srcset(value):
    """Parse srcset into URL/descriptor records without losing source order."""
    text = str(value or "")
    spans = list(srcset_url_spans(text))
    result = []
    for index, (start, end) in enumerate(spans):
        candidate = text[start:end].strip()
        if not candidate:
            continue
        next_start = spans[index + 1][0] if index + 1 < len(spans) else len(text)
        descriptor = text[end:next_start]
        # ``descriptor`` includes the comma separating the next candidate;
        # accept that delimiter while rejecting digits embedded in a URL.
        match = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)([wx])(?=\s*(?:,|$))",
                          descriptor)
        kind = match.group(2).lower() if match else ""
        number = float(match.group(1)) if match else None
        result.append({"url": candidate, "kind": kind, "value": number,
                       "index": index})
    return result


def _css_length_px(value, viewport_width, viewport_height=None,
                   reference_axis="width"):
    """Resolve a bounded CSS length used by ``sizes``/media queries.

    ``vh``/viewport variants previously fell through to a font-size estimate,
    which could select the wrong responsive source whenever a page used a
    height-based slot.  Keep the parser deterministic and only use a height
    unit when the WebView supplied a valid viewport height.
    """
    text = str(value or "").strip().lower()
    if not text or text == "auto":
        return None
    viewport = float(viewport_width)
    height = None if viewport_height is None else float(viewport_height)

    def unit_value(amount, unit):
        unit = str(unit or "").lower()
        if unit == "px":
            return amount
        if unit in ("vw", "svw", "lvw", "dvw"):
            return viewport * amount / 100.0
        if unit in ("vh", "svh", "lvh", "dvh"):
            return None if height is None else height * amount / 100.0
        if unit in ("vmin", "svmin", "lvmin", "dvmin"):
            return None if height is None else min(viewport, height) * amount / 100.0
        if unit in ("vmax", "svmax", "lvmax", "dvmax"):
            return None if height is None else max(viewport, height) * amount / 100.0
        if unit == "%":
            base = height if reference_axis == "height" else viewport
            return base * amount / 100.0
        if unit in ("rem", "em"):
            return amount * 16.0
        if unit == "ch":
            return amount * 8.0
        if unit == "ex":
            return amount * 8.0
        return None

    if text.startswith("calc(") and text.endswith(")"):
        inner = text[5:-1].strip()
        total = 0.0
        matched = False
        invalid = False
        for sign, number, unit in re.findall(
                r"([+-]?)\s*(\d+(?:\.\d+)?)\s*(px|vw|vh|svw|lvw|dvw|"
                r"svh|lvh|dvh|vmin|vmax|svmin|lvmin|dvmin|svmax|lvmax|dvmax|"
                r"rem|em|ch|ex|%)", inner):
            matched = True
            amount = float(number)
            px = unit_value(amount, unit)
            if px is None:
                invalid = True
                continue
            total += -px if sign == "-" else px
        return max(0.0, total) if matched and not invalid else None
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(px|vw|vh|svw|lvw|dvw|svh|lvh|dvh|"
        r"vmin|vmax|svmin|lvmin|dvmin|svmax|lvmax|dvmax|rem|em|ch|ex|%)", text)
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2)
    return unit_value(amount, unit)


def _media_matches(media, viewport_width, viewport_height=None,
                   device_pixel_ratio=None, responsive_features=None):
    """Match the deterministic subset of browser media-query selection.

    The old adapter rejected every ``not`` query and comma-separated media
    list, which could select the wrong ``picture`` source even though both
    forms are static and safe to evaluate.  Keep unknown features fail-closed,
    but support the common screen/all, width/height, orientation and
    resolution conditions used by CMS themes.  A missing viewport remains
    intentionally permissive for the historical static-preview mode.
    """
    text = str(media or "").strip().lower()
    if not text or viewport_width is None:
        return True
    try:
        viewport = float(viewport_width)
        height = None if viewport_height is None else float(viewport_height)
        dpr = None if device_pixel_ratio is None else float(device_pixel_ratio)
    except (TypeError, ValueError):
        return False

    # Comma-separated media queries are an OR list. Split only outside
    # parentheses so future calc() expressions are not broken.
    queries, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif char == ',' and depth == 0:
            queries.append(text[start:index].strip()); start = index + 1
    queries.append(text[start:].strip())

    features = dict(responsive_features or {}) if isinstance(
        responsive_features, dict) else {}

    def feature_value(name):
        aliases = {
            "prefers-color-scheme": "prefers_color_scheme",
            "prefers-reduced-motion": "prefers_reduced_motion",
            "prefers-contrast": "prefers_contrast",
            "forced-colors": "forced_colors",
            "inverted-colors": "inverted_colors",
            "dynamic-range": "dynamic_range",
            "video-dynamic-range": "video_dynamic_range",
            "hover": "hover",
            "any-hover": "any_hover",
            "pointer": "pointer",
            "any-pointer": "any_pointer",
            "update": "update",
            "scripting": "scripting",
            "color": "color_depth",
            "color-index": "color_index",
            "monochrome": "monochrome_depth",
        }
        key = aliases.get(str(name or "").strip().lower(),
                          str(name or "").strip().lower().replace("-", "_"))
        return features.get(key)

    def ratio_value(value):
        text_value = str(value or "").strip()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
                             text_value)
        if match:
            numerator, denominator = float(match.group(1)), float(match.group(2))
            return numerator / denominator if denominator else None
        try:
            number = float(text_value)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    def resolution(value):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(dppx|dpi|dpcm)", value)
        if not match:
            return None
        number, unit = float(match.group(1)), match.group(2)
        if unit == "dppx":
            return number
        if unit == "dpi":
            return number / 96.0
        return number * 2.54 / 96.0

    def one(query):
        query = query.strip()
        if not query:
            return False
        # Media Queries Level 4 permits ``or`` as an explicit alternative to
        # a comma-separated list. Split only outside parentheses so an
        # ``or`` appearing inside a future function/value is not misread.
        parts, start, depth = [], 0, 0
        for index, char in enumerate(query):
            if char == '(':
                depth += 1
            elif char == ')':
                depth = max(0, depth - 1)
            elif depth == 0 and query[index:index + 4].lower() == " or ":
                parts.append(query[start:index].strip())
                start = index + 4
        if parts:
            parts.append(query[start:].strip())
            return any(one(part) for part in parts)
        negated = False
        if re.match(r"^not\b", query):
            negated = True
            query = re.sub(r"^not\s+", "", query, count=1)
        query = re.sub(r"^only\s+", "", query, count=1)
        type_match = re.match(r"^(all|screen|print|speech)\b", query)
        media_type = type_match.group(1) if type_match else "all"
        if type_match:
            query = query[type_match.end():].strip()
        if query and not query.startswith("("):
            # ``and`` is consumed below; anything else is a dynamic or
            # unsupported media expression.
            if not re.match(r"^and\b", query):
                return False
        conditions = re.findall(r"\(([^()]*)\)", query)
        remainder = re.sub(r"\([^()]*\)", "", query)
        if remainder.strip().replace("and", "").strip():
            return False
        matched = media_type in ("all", "screen")
        if media_type in ("print", "speech"):
            matched = False

        def _measure(feature, value):
            """Resolve a media-feature value to the feature's unit space."""
            feature = str(feature or "").strip().lower()
            text = str(value or "").strip()
            if feature in ("width", "height"):
                return _css_length_px(text, viewport, height,
                                      "height" if feature == "height" else "width")
            if feature in ("resolution", "device-pixel-ratio", "-webkit-device-pixel-ratio"):
                if feature == "resolution":
                    return resolution(text)
                try:
                    return float(text)
                except (TypeError, ValueError):
                    return None
            return None

        def _compare(actual, operator, expected):
            if actual is None or expected is None:
                return False
            if operator == ">=":
                return actual >= expected
            if operator == "<=":
                return actual <= expected
            if operator == ">":
                return actual > expected
            if operator == "<":
                return actual < expected
            return abs(actual - expected) <= 0.5

        for condition in conditions:
            condition = condition.strip()
            orientation = re.fullmatch(r"orientation\s*:\s*(portrait|landscape)", condition)
            if orientation:
                if height is None:
                    return False
                actual_orientation = "portrait" if height >= viewport else "landscape"
                matched = matched and actual_orientation == orientation.group(1)
                continue
            # Deterministic interaction/preferences features are supplied by
            # the WebView through ``responsiveContext``.  If the context does
            # not expose one, fail closed for a supplied viewport instead of
            # guessing which <source> a browser would choose.
            preference = re.fullmatch(r"([a-z-]+)\s*:\s*([^\s].*)", condition,
                                      re.I)
            if preference and preference.group(1).lower() in {
                    "prefers-color-scheme", "prefers-reduced-motion",
                    "prefers-contrast", "forced-colors", "inverted-colors",
                    "dynamic-range", "video-dynamic-range", "hover", "any-hover",
                    "pointer", "any-pointer", "update", "scripting", "color",
                    "min-color", "max-color", "color-index", "min-color-index",
                    "max-color-index", "monochrome", "min-monochrome",
                    "max-monochrome", "aspect-ratio", "min-aspect-ratio",
                    "max-aspect-ratio"}:
                feature, expected = preference.groups()
                feature = feature.lower()
                actual = feature_value(feature)
                if feature in {"color", "color-index", "monochrome"}:
                    try:
                        expected_number = float(expected.strip())
                        actual_number = float(actual)
                    except (TypeError, ValueError):
                        return False
                    matched = matched and actual_number >= expected_number
                    continue
                if feature in {"min-color", "max-color", "min-color-index",
                               "max-color-index", "min-monochrome",
                               "max-monochrome"}:
                    base = feature.replace("min-", "").replace("max-", "")
                    actual = feature_value(base)
                    try:
                        expected_number = float(expected.strip())
                        actual_number = float(actual)
                    except (TypeError, ValueError):
                        return False
                    matched = matched and (
                        actual_number >= expected_number if feature.startswith("min-")
                        else actual_number <= expected_number)
                    continue
                if feature in {"aspect-ratio", "min-aspect-ratio",
                               "max-aspect-ratio"}:
                    if height is None or height <= 0:
                        return False
                    actual_ratio = viewport / height
                    expected_ratio = ratio_value(expected)
                    if expected_ratio is None:
                        return False
                    if feature == "min-aspect-ratio":
                        matched = matched and actual_ratio >= expected_ratio
                    elif feature == "max-aspect-ratio":
                        matched = matched and actual_ratio <= expected_ratio
                    else:
                        matched = matched and abs(actual_ratio - expected_ratio) <= 0.01
                    continue
                if actual is None:
                    return False
                matched = matched and str(actual).strip().lower() == expected.strip().lower()
                continue
            ratio = re.fullmatch(
                r"(-webkit-)?(min|max)?-?device-pixel-ratio\s*:\s*(\d+(?:\.\d+)?)",
                condition)
            if ratio:
                if dpr is None:
                    return False
                bound = float(ratio.group(3))
                kind = ratio.group(2) or "exact"
                matched = matched and ((dpr >= bound) if kind == "min" else
                                      (dpr <= bound) if kind == "max" else
                                      abs(dpr - bound) < 0.01)
                continue
            # Media Queries Level 4 also permits mathematical range syntax,
            # for example ``(width >= 768px)`` or
            # ``(600px <= width <= 1200px)``.  These expressions are fully
            # static and safe to evaluate; support only the bounded features
            # for which this module already has viewport/DPR values.
            chain = re.fullmatch(
                r"(.+?)\s*(<=|>=|<|>|=)\s*"
                r"(width|height|resolution|device-pixel-ratio|"
                r"-webkit-device-pixel-ratio)\s*(<=|>=|<|>|=)\s*(.+)",
                condition, re.I)
            if chain:
                left_value, left_op, feature, right_op, right_value = chain.groups()
                actual = _measure(feature, "")
                if feature.lower() == "width":
                    actual = viewport
                elif feature.lower() == "height":
                    actual = height
                elif feature.lower() in ("resolution", "device-pixel-ratio",
                                         "-webkit-device-pixel-ratio"):
                    actual = dpr
                left_expected = _measure(feature, left_value)
                right_expected = _measure(feature, right_value)
                matched = matched and _compare(left_expected, left_op, actual)
                matched = matched and _compare(actual, right_op, right_expected)
                continue
            simple_range = re.fullmatch(
                r"(width|height|resolution|device-pixel-ratio|"
                r"-webkit-device-pixel-ratio)\s*(<=|>=|<|>|=)\s*(.+)",
                condition, re.I)
            if simple_range:
                feature, operator, value = simple_range.groups()
                feature_lower = feature.lower()
                if feature_lower == "width":
                    actual = viewport
                elif feature_lower == "height":
                    actual = height
                else:
                    actual = dpr
                matched = matched and _compare(actual, operator,
                                               _measure(feature, value))
                continue
            res_match = re.fullmatch(r"(min|max)?-resolution\s*:\s*(.+)", condition)
            if res_match:
                if dpr is None:
                    return False
                bound = resolution(res_match.group(2).strip())
                if bound is None:
                    return False
                kind = res_match.group(1) or "exact"
                matched = matched and ((dpr >= bound) if kind == "min" else
                                      (dpr <= bound) if kind == "max" else
                                      abs(dpr - bound) < 0.01)
                continue
            match = re.fullmatch(
                r"(min-width|max-width|width|min-height|max-height|height)\s*:\s*([^ ]+)",
                condition)
            if not match:
                return False
            length = _css_length_px(
                match.group(2), viewport, height,
                "height" if "height" in match.group(1) else "width")
            if length is None:
                return False
            is_height = "height" in match.group(1)
            if is_height and height is None:
                return False
            actual = height if is_height else viewport
            if match.group(1) in ("min-width", "min-height"):
                matched = matched and actual >= length
            elif match.group(1) in ("max-width", "max-height"):
                matched = matched and actual <= length
            else:
                matched = matched and abs(actual - length) <= 0.5
        return not matched if negated else matched

    return any(one(query) for query in queries)


def _sizes_slot_width(sizes, viewport_width, viewport_height=None,
                      device_pixel_ratio=None, responsive_features=None):
    """Resolve the first matching ``sizes`` clause, defaulting to 100vw."""
    if viewport_width is None:
        return None
    text = str(sizes or "").strip()
    if not text:
        return float(viewport_width)
    # Split commas outside parentheses (calc() may contain commas in future
    # CSS functions); the common sizes grammar is otherwise straightforward.
    clauses, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char == "(": depth += 1
        elif char == ")": depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            clauses.append(text[start:index].strip()); start = index + 1
    clauses.append(text[start:].strip())
    for clause in clauses:
        match = re.match(r"^(?P<media>\([^)]*\)(?:\s+and\s+[^)]*)*)\s+(?P<len>.+)$", clause, re.I)
        if match:
            if not _media_matches(match.group("media"), viewport_width,
                                  viewport_height, device_pixel_ratio,
                                  responsive_features):
                continue
            length = match.group("len")
        else:
            length = clause
        resolved = _css_length_px(length, viewport_width, viewport_height)
        if resolved is not None:
            return resolved
    return float(viewport_width)


def responsive_srcset_candidates(value, viewport_width=None, device_pixel_ratio=1.0,
                                 sizes="", viewport_height=None,
                                 responsive_features=None):
    """Return candidates with the browser-like choice first.

    When no viewport is supplied this intentionally retains the old static
    desktop order.  Callers that know the WebView viewport can provide width
    and DPR; ``w`` candidates then use ``sizes`` and density candidates use
    DPR.  All remaining candidates are retained as safe fallbacks.
    """
    records = _parse_srcset(value)
    if not records:
        return []
    if viewport_width is None:
        ranked = []
        for item in records:
            score = float(item["value"] or 0.0)
            if item["kind"] == "x":
                score *= 1000.0
            ranked.append((score, item["index"], item["url"]))
        return [url for _score, _index, url in sorted(ranked, key=lambda item: (-item[0], item[1]))]
    try:
        viewport = max(1.0, float(viewport_width))
        dpr = max(0.1, float(device_pixel_ratio or 1.0))
    except (TypeError, ValueError):
        return responsive_srcset_candidates(value)
    widths = [item for item in records if item["kind"] == "w"]
    densities = [item for item in records if item["kind"] == "x"]
    plain = [item for item in records if not item["kind"]]
    selected = None
    if widths:
        target = (_sizes_slot_width(
            sizes, viewport, viewport_height, dpr, responsive_features) or viewport) * dpr
        selected = min((item for item in widths if item["value"] >= target),
                       key=lambda item: (item["value"], item["index"]), default=None)
        if selected is None:
            selected = max(widths, key=lambda item: (item["value"], -item["index"]))
    elif densities:
        selected = min((item for item in densities if item["value"] >= dpr),
                       key=lambda item: (item["value"], item["index"]), default=None)
        if selected is None:
            selected = max(densities, key=lambda item: (item["value"], -item["index"]))
    elif plain:
        selected = plain[0]
    ordered = [selected] if selected else []
    ordered.extend(item for item in records if item is not selected)
    return [item["url"] for item in ordered]


def normalize_responsive_context(value):
    """Return a bounded viewport/DPR context supplied by the WebView.

    A missing context deliberately preserves the historical static preview;
    malformed or absurd values are ignored rather than changing upload data.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    try:
        width = float(value.get("viewport_width"))
        if 1 <= width <= 100000:
            result["viewport_width"] = width
    except (TypeError, ValueError):
        pass
    try:
        dpr = float(value.get("device_pixel_ratio", 1.0))
        if 0.1 <= dpr <= 16:
            result["device_pixel_ratio"] = dpr
    except (TypeError, ValueError):
        pass
    try:
        height = float(value.get("viewport_height"))
        if 1 <= height <= 100000:
            result["viewport_height"] = height
    except (TypeError, ValueError):
        pass
    # These values are intentionally allow-listed.  They come from
    # matchMedia/screen in the embedded browser and are only used to select a
    # static candidate; arbitrary strings cannot alter upload URLs.
    enum_fields = {
        "prefers_color_scheme": {"dark", "light", "no-preference"},
        "prefers_reduced_motion": {"reduce", "no-preference"},
        "prefers_contrast": {"more", "less", "custom", "no-preference"},
        "forced_colors": {"active", "none"},
        "inverted_colors": {"inverted", "none"},
        "dynamic_range": {"standard", "high"},
        "video_dynamic_range": {"standard", "high"},
        "hover": {"hover", "none"},
        "any_hover": {"hover", "none"},
        "pointer": {"fine", "coarse", "none"},
        "any_pointer": {"fine", "coarse", "none"},
        "update": {"fast", "slow", "none"},
        "scripting": {"enabled", "initial-only", "none"},
    }
    for key, allowed in enum_fields.items():
        raw = str(value.get(key, "") or "").strip().lower()
        if raw in allowed:
            result[key] = raw
    numeric_fields = {
        "color_depth": (1, 128), "color_index": (1, 128),
        "monochrome_depth": (0, 128),
    }
    for key, (low, high) in numeric_fields.items():
        try:
            number = int(value.get(key))
            if low <= number <= high:
                result[key] = number
        except (TypeError, ValueError):
            pass
    return result


def _srcset_candidates(value, responsive_context=None, sizes=""):
    """Return static candidates, optionally selecting by viewport and DPR."""
    context = normalize_responsive_context(responsive_context)
    return responsive_srcset_candidates(
        value, context.get("viewport_width"), context.get("device_pixel_ratio", 1.0), sizes,
        context.get("viewport_height"), context)


def _is_placeholder(value):
    lowered = str(value or "").strip().lower()
    return (not lowered or lowered.startswith(("data:", "blob:")) or
            any(token in lowered for token in
                ("placeholder", "transparent", "spacer", "blank", "loading")))


def _preview_candidates(fragments, node, responsive_context=None):
    """Collect likely static preview sources without executing page JS."""
    responsive_context = normalize_responsive_context(responsive_context)
    attrs = node.get("attrs", {})
    src = str(attrs.get("src", "") or "").strip()
    candidates = []
    parent = node.get("parent")
    while parent is not None:
        ancestor = fragments.nodes[parent]
        if ancestor.get("tag") == "picture":
            for source in fragments.nodes:
                if source.get("parent") == parent and source.get("tag") == "source":
                    source_attrs = source.get("attrs", {})
                    context = responsive_context or {}
                    if not media_type_supported(source_attrs.get("type", "")):
                        continue
                    if not _media_matches(source_attrs.get("media", ""),
                                          context.get("viewport_width"),
                                          context.get("viewport_height"),
                                          context.get("device_pixel_ratio"), context):
                        continue
                    candidates.extend(_srcset_candidates(
                        source_attrs.get("srcset", ""), responsive_context,
                        source_attrs.get("sizes", "")))
                    candidates.extend(_srcset_candidates(
                        source_attrs.get("data-srcset", ""), responsive_context,
                        source_attrs.get("sizes", "")))
            break
        parent = ancestor.get("parent")
    if _is_placeholder(src):
        for name in ("data-src", "data-original", "data-lazy-src", "data-url"):
            if attrs.get(name):
                candidates.append(str(attrs[name]).strip())
        candidates.extend(_srcset_candidates(attrs.get("data-srcset", ""),
                                             responsive_context, attrs.get("sizes", "")))
        candidates.extend(_srcset_candidates(attrs.get("srcset", ""),
                                             responsive_context, attrs.get("sizes", "")))
    else:
        candidates.extend(_srcset_candidates(attrs.get("srcset", ""),
                                             responsive_context, attrs.get("sizes", "")))
        candidates.append(src)
        for name in ("data-src", "data-original", "data-lazy-src"):
            if attrs.get(name):
                candidates.append(str(attrs[name]).strip())
    seen = set()
    return [value for value in candidates
            if value and not (value in seen or seen.add(value))]


_SOURCE_ATTRIBUTES = ('src', 'data-src', 'data-original', 'data-lazy-src')
_SOURCESETS = ('srcset', 'data-srcset')
_IMAGE_EXTRA_ATTRIBUTES = {'video': ('poster',)}
_SVG_SOURCE_ATTRIBUTES = ('href', 'xlink:href')
_STYLE_URL_RE = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<url>[^'\")]+)(?P=quote)\s*\)", re.I)
_STYLE_IMAGE_EXTS = {
    '.jpg', '.jpeg', '.jpe', '.png', '.gif', '.webp', '.avif', '.bmp', '.tif', '.tiff',
    '.svg', '.ico', '.heic', '.heif', '.jxl',
    '.jp2', '.j2k', '.jpf', '.jpx', '.jpm', '.psd'
}


def _style_url_values(value):
    for match in _STYLE_URL_RE.finditer(str(value or '')):
        yield match.group('url').strip()


def _style_image_url(value, base_dir=""):
    """Yield only likely image URLs from CSS backgrounds/masks.

    CSS may also contain font/video URLs.  Restricting this automatic upload
    path to image extensions prevents the article body scanner from stealing
    unrelated local assets while still covering the common background-image
    and mask-image forms used by page templates.
    """
    for url in _style_url_values(value):
        path = url.split('?', 1)[0].split('#', 1)[0]
        if Path(unquote(path)).suffix.lower() in _STYLE_IMAGE_EXTS:
            yield url
            continue
        # Local generated HTML occasionally references a raster object by an
        # extensionless name.  The browser can still identify it by bytes;
        # use the same bounded signature fallback as native file controls.
        if base_dir and classify_src(url) == KIND_LOCAL:
            local = resolve_local_src(url, base_dir)
            if local:
                try:
                    with open(local, "rb") as handle:
                        if sniff_mime(handle.read(128 * 1024), local).startswith("image/"):
                            yield url
                except (OSError, IOError):
                    pass


def _style_block_image_urls(source, fragments, base_dir="", url_by_src=None):
    """Yield ``(node, match, url)`` for image URLs in inline ``<style>`` blocks.

    Inline CSS is part of the document's resource graph just like a style
    attribute.  ``HTMLFragments`` keeps the original source spans, so callers
    can replace only the URL token and preserve selectors, comments, quoting
    and whitespace.  External stylesheets are intentionally not fetched here:
    doing so would introduce network side effects and a second base URL.
    """
    for node in fragments.find('style'):
        css = source[node['open']:node['close']]
        for match in _STYLE_URL_RE.finditer(css):
            value = match.group('url').strip()
            path = value.split('?', 1)[0].split('#', 1)[0]
            if (Path(unquote(path)).suffix.lower() in _STYLE_IMAGE_EXTS or
                    value in (url_by_src or {})):
                yield node, match, value
                continue
            if base_dir and classify_src(value) == KIND_LOCAL:
                local = resolve_local_src(value, base_dir)
                if local:
                    try:
                        with open(local, "rb") as handle:
                            is_image = sniff_mime(
                                handle.read(128 * 1024), local).startswith("image/")
                    except (OSError, IOError):
                        is_image = False
                    if is_image:
                        yield node, match, value


def _rewrite_source_attributes(tag, url_by_src, *, force_url=None):
    updates = {}
    for attr in attribute_spans(tag):
        name, value = attr['name'], attr['value']
        if value is None:
            continue
        if name in _SOURCE_ATTRIBUTES or name in ('poster',):
            replacement = force_url or url_by_src.get(value)
            if replacement:
                updates[name] = replacement
        elif name in _SOURCESETS:
            rewritten = value
            for start, end in reversed(list(srcset_url_spans(value))):
                replacement = force_url or url_by_src.get(value[start:end])
                if replacement:
                    rewritten = rewritten[:start] + str(replacement) + rewritten[end:]
            if rewritten != value:
                updates[name] = rewritten
        elif name in _SVG_SOURCE_ATTRIBUTES:
            replacement = force_url or url_by_src.get(value)
            if replacement:
                updates[name] = replacement
        elif name == 'style':
            rewritten = value
            for match in reversed(list(_STYLE_URL_RE.finditer(value))):
                old = match.group('url').strip()
                replacement = force_url or url_by_src.get(old)
                if replacement:
                    start, end = match.span('url')
                    rewritten = rewritten[:start] + str(replacement) + rewritten[end:]
            if rewritten != value:
                updates[name] = rewritten
    return rewrite_attributes(tag, updates)


def _picture_parent(fragments, node):
    parent = node['parent']
    while parent is not None:
        candidate = fragments.nodes[parent]
        if candidate['tag'] == 'picture':
            return parent
        parent = candidate['parent']
    return None


def _tag_fingerprint(tag):
    """Stable guard for an individual source ``<img>`` tag."""
    return hashlib.sha256(str(tag or "").encode("utf-8")).hexdigest()


_IMAGE_DIMENSION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:px|%|em|rem|vw|vh)?|auto)$", re.I)


def validate_image_dimension(value, label="图片尺寸"):
    """Validate an explicitly edited image width/height value.

    The browser accepts a non-negative dimension in the HTML attribute and
    sites commonly use a CSS unit in a custom editor.  Keep the portable
    editor contract deliberately narrow (non-negative number plus a common
    unit, or ``auto``) instead of allowing arbitrary markup/CSS tokens.  An
    empty string is meaningful: it removes the attribute and restores the
    page/theme's natural sizing.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 32 or not _IMAGE_DIMENSION_RE.fullmatch(text):
        raise ValueError(f"{label}格式无效：请输入非负数值或常见 CSS 单位")
    return text


def describe_html_images(html, responsive_context=None):
    """Return safe, occurrence-based metadata for images in a HTML fragment.

    ``index`` deliberately identifies an occurrence rather than a URL: the same
    URL can appear in more than one image and an editor must be able to replace
    only one of them.  The fingerprint protects a pending replacement from
    silently targeting a changed tag.
    """
    responsive_context = normalize_responsive_context(responsive_context)
    result = []
    source = str(html or "")
    fragments = HTMLFragments(source)
    for index, node in enumerate(fragments.find('img')):
        tag = source[node['start']:node['open']]
        attrs = node.get('attrs', {})
        src = str(attrs.get('src', '') or '')
        if not src:
            continue
        candidates = _preview_candidates(fragments, node, responsive_context)
        result.append({"index": index, "src": src,
                       "preview_src": candidates[0] if candidates else src,
                       "preview_candidates": candidates,
                       "alt": attrs.get('alt', '') or '',
                       "width": attrs.get('width', '') or '',
                       "height": attrs.get('height', '') or '',
                       "tag_fingerprint": _tag_fingerprint(tag)})
    return result


def replace_image_occurrences(html, replacements):
    """Replace exactly selected images, retaining layout and checking fingerprints.

    An explicitly replaced picture uses the new image for every responsive
    candidate, including its picture/source nodes. Otherwise a 2x/media/lazy
    candidate could continue showing the old image despite a successful save.
    """
    source = str(html or "")
    by_index = {}
    for item in replacements or ():
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            raise ValueError("详情图片替换项缺少有效序号")
        if index < 0 or index in by_index:
            raise ValueError("详情图片替换项序号重复或无效")
        url = str(item.get("url", "") or "").strip()
        expected_src = str(item.get("expected_src", "") or "")
        fingerprint = str(item.get("tag_fingerprint", "") or "")
        if not expected_src or not fingerprint:
            raise ValueError("详情图片替换项不完整")
        has_property_update = any(key in item for key in
                                  ("new_alt", "new_width", "new_height"))
        if not url and not has_property_update:
            raise ValueError("详情图片替换项缺少图片地址或图片属性")
        if "new_alt" in item:
            new_alt = str(item.get("new_alt", "") or "")
            if len(new_alt) > 4096 or any(ord(char) < 32 and char not in "\r\n\t" for char in new_alt):
                raise ValueError("详情图片 alt 文本无效或过长")
            item["new_alt"] = new_alt
        for key, label in (("new_width", "图片宽度"), ("new_height", "图片高度")):
            if key in item:
                item[key] = validate_image_dimension(item.get(key), label)
        by_index[index] = dict(url=url, expected_src=expected_src, tag_fingerprint=fingerprint)
        if "new_alt" in item:
            by_index[index]["new_alt"] = item["new_alt"]
        for key in ("new_width", "new_height"):
            if key in item:
                by_index[index][key] = item[key]
    if not by_index:
        return source, 0
    fragments = HTMLFragments(source)
    images = fragments.find('img')
    if any(index >= len(images) for index in by_index):
        raise ValueError("文章正文图片已变化，请重新加载文章后再提交")
    changes, pictures = [], {}
    for index, item in by_index.items():
        node = images[index]
        tag = source[node['start']:node['open']]
        if (node['attrs'].get('src', '') != item['expected_src'] or
                _tag_fingerprint(tag) != item['tag_fingerprint']):
            raise ValueError("文章正文图片已变化，请重新加载文章后再提交")
        picture = _picture_parent(fragments, node)
        if picture is not None:
            if sum(_picture_parent(fragments, n) == picture for n in images) != 1:
                raise ValueError("picture 包含多张图片，无法唯一确定响应式替换范围")
            pictures[picture] = item['url']
        changed = _rewrite_source_attributes(tag, {}, force_url=item['url'] or None)
        updates = {}
        remove = []
        if "new_alt" in item:
            updates["alt"] = item["new_alt"]
        for key, attr in (("new_width", "width"), ("new_height", "height")):
            if key in item:
                if item[key]:
                    updates[attr] = item[key]
                else:
                    remove.append(attr)
        if updates or remove:
            changed = rewrite_attributes(changed, updates, remove=remove)
        changes.append((node['start'], node['open'], changed))
    for node in fragments.find('source'):
        picture = _picture_parent(fragments, node)
        if picture in pictures:
            tag = source[node['start']:node['open']]
            changes.append((node['start'], node['open'],
                            _rewrite_source_attributes(tag, {}, force_url=pictures[picture])))
    for start, end, tag in sorted(changes, reverse=True):
        source = source[:start] + tag + source[end:]
    return source, len(by_index)


def scan_html_images(html, base_dir, document_url=""):
    """Scan img/lazy/srcset and picture sources, deduplicating actual URLs.

    When the imported document declares ``<base href>``, relative resources
    follow browser URL resolution instead of being mistaken for local files.
    ``document_url`` is optional so existing local-file callers retain their
    previous behaviour.
    """
    images, problems, seen = [], [], set()
    source_html = str(html or "")
    fragments = HTMLFragments(source_html)
    document_base = document_base_url(source_html, document_url)
    for node in fragments.nodes:
        attrs = node['attrs']
        image_node = node['tag'] in ('img', 'image') or (
            node['tag'] == 'video' and
            (attrs.get('poster') or any(_style_image_url(attrs.get('style', ''), base_dir))))
        if not image_node and not (
                node['tag'] == 'source' and _picture_parent(fragments, node) is not None) \
                and not any(_style_image_url(attrs.get('style', ''), base_dir)):
            continue
        candidates = []
        if node['tag'] != 'video':
            for name in _SOURCE_ATTRIBUTES:
                if attrs.get(name):
                    candidates.append(attrs[name])
        for name in _SOURCESETS:
            value = attrs.get(name) or ''
            candidates.extend(value[start:end] for start, end in srcset_url_spans(value))
        if node['tag'] == 'image':
            candidates.extend(attrs.get(name) for name in _SVG_SOURCE_ATTRIBUTES
                              if attrs.get(name))
        candidates.extend(attrs.get(name) for name in _IMAGE_EXTRA_ATTRIBUTES.get(node['tag'], ())
                          if attrs.get(name))
        candidates.extend(_style_image_url(attrs.get('style', ''), base_dir))
        if not candidates:
            problems.append(dict(src="", kind="empty", reason="图片缺少可用地址"))
        for src in candidates:
            if src in seen:
                continue
            seen.add(src)
            kind, resolved_src = resolve_document_src(src, base_dir, document_base)
            if kind in (KIND_REMOTE, KIND_DATA, KIND_SITE):
                problems.append(dict(src=src[:40]+"..." if kind == KIND_DATA else src,
                                     resolved_src=resolved_src if resolved_src != src else "",
                                     kind=kind, reason="已托管或内联图片，保留原样"))
                continue
            if kind == KIND_LOCAL and resolved_src != src:
                # A declared file:// base is authoritative; do not silently
                # fall back to the imported HTML directory when that resource
                # is missing, which would differ from browser resolution.
                path = (resolved_src if os.path.isabs(str(resolved_src))
                        and os.path.isfile(str(resolved_src))
                        else resolve_local_src(resolved_src, base_dir))
            else:
                path = resolve_local_src(src, base_dir)
            if not path:
                problems.append(dict(src=src, kind=kind, reason="本地图片文件不存在或无法读取"))
                continue
            images.append(dict(src=src, path=path, alt=attrs.get('alt') or ''))
    # Inline stylesheet blocks are commonly used by generated article HTML
    # for hero/background images.  They are real browser resources even
    # though they are not represented by an element attribute.  Scan them
    # after element order so existing occurrence ordering remains stable and
    # deduplication still covers a URL used by both CSS and markup.
    for _node, _match, src in _style_block_image_urls(
            source_html, fragments, base_dir=base_dir):
        if src in seen:
            continue
        seen.add(src)
        kind, resolved_src = resolve_document_src(src, base_dir, document_base)
        if kind in (KIND_REMOTE, KIND_DATA, KIND_SITE):
            problems.append(dict(src=src, resolved_src=resolved_src if resolved_src != src else "",
                                 kind=kind, reason="已托管或内联图片，保留原样"))
            continue
        if kind == KIND_LOCAL and resolved_src != src:
            path = (resolved_src if os.path.isabs(str(resolved_src))
                    and os.path.isfile(str(resolved_src))
                    else resolve_local_src(resolved_src, base_dir))
        else:
            path = resolve_local_src(src, base_dir)
        if not path:
            problems.append(dict(src=src, kind=kind, reason="本地图片文件不存在或无法读取"))
            continue
        images.append(dict(src=src, path=path, alt="", source="style"))
    return images, problems


def scan_html_remote_images(html, document_url="", base_dir=""):
    """Return externally hosted image candidates in document order.

    UEditor's ``catchRemoteImageEnable`` mode sends these URLs to the CMS
    catcher before the article is saved.  The normal local-image scanner
    deliberately reports remote resources as problems because silently
    downloading them is surprising; callers that have explicitly discovered
    the catcher policy can use this separate, opt-in list instead.
    """
    result, seen = [], set()
    source_html = str(html or "")
    fragments = HTMLFragments(source_html)
    document_base = document_base_url(source_html, document_url)
    explicit_document_base = bool(document_base)
    # The browser resolves a relative URL against the document URL even when
    # no explicit <base> element exists.  The local-file scanner deliberately
    # keeps its historical file-directory semantics; this remote-only scanner
    # is used for an editor's optional catcher and should follow the live page.
    if not document_base:
        candidate = str(document_url or "").strip()
        parsed_document = urlparse(candidate)
        if parsed_document.scheme.lower() in ("http", "https") and parsed_document.netloc:
            document_base = candidate
    for node in fragments.nodes:
        attrs = node.get('attrs', {})
        image_node = node.get('tag') in ('img', 'image') or (
            node.get('tag') == 'video' and
            (attrs.get('poster') or any(_style_image_url(attrs.get('style', '')))))
        if not image_node and not (
                node.get('tag') == 'source' and _picture_parent(fragments, node) is not None) \
                and not any(_style_image_url(attrs.get('style', ''))):
            continue
        candidates = []
        if node.get('tag') != 'video':
            for name in _SOURCE_ATTRIBUTES:
                if attrs.get(name):
                    candidates.append((name, attrs[name]))
        for name in _SOURCESETS:
            value = attrs.get(name) or ''
            candidates.extend((name, value[start:end])
                              for start, end in srcset_url_spans(value))
        if node.get('tag') == 'image':
            candidates.extend((name, attrs.get(name))
                              for name in _SVG_SOURCE_ATTRIBUTES
                              if attrs.get(name))
        candidates.extend((value, attrs.get(value))
                          for value in _IMAGE_EXTRA_ATTRIBUTES.get(node.get('tag'), ())
                          if attrs.get(value))
        candidates.extend(("style", value) for value in _style_image_url(attrs.get('style', '')))
        for attr, value in candidates:
            value = str(value or '').strip()
            # Imported local HTML commonly contains relative files that also
            # happen to be resolvable against the eventual article URL.  If
            # there is no explicit <base href>, prefer the existing local
            # file and leave it to scan_html_images; otherwise remote-catcher
            # mode would upload the same asset a second time as a URL.
            if (base_dir and not explicit_document_base and
                    classify_src(value) == KIND_LOCAL and
                    resolve_local_src(value, base_dir)):
                continue
            kind, resolved_value = resolve_document_src(value, "", document_base)
            if not value or kind != KIND_REMOTE or resolved_value in seen:
                continue
            seen.add(resolved_value)
            item = {"src": resolved_value, "tag": node.get('tag', ''),
                    "attr": attr}
            if resolved_value != value:
                item["original_src"] = value
            result.append(item)
    # Include image URLs in inline <style> blocks.  Resolve them with the
    # document's <base href> exactly as element attributes are resolved; CSS
    # from an external stylesheet is deliberately left to the browser/editor.
    for _node, _match, value in _style_block_image_urls(source_html, fragments):
        if (base_dir and not explicit_document_base and
                classify_src(value) == KIND_LOCAL and
                resolve_local_src(value, base_dir)):
            continue
        kind, resolved_value = resolve_document_src(value, "", document_base)
        if kind != KIND_REMOTE or resolved_value in seen:
            continue
        seen.add(resolved_value)
        item = {"src": resolved_value, "tag": "style", "attr": "style"}
        if resolved_value != value:
            item["original_src"] = value
        result.append(item)
    return result


def rewrite_html_images(html, url_by_src, width_mode="preserve", width=790):
    """Patch uploaded URLs without altering presentation unless explicitly asked."""
    source = str(html or "")
    if not url_by_src:
        return source, 0
    fragments = HTMLFragments(source)
    changes = []
    replaced = 0
    for node in fragments.nodes:
        style_values = list(_style_url_values(node['attrs'].get('style', '')))
        mapped_style = any(value in url_by_src for value in style_values)
        image_node = node['tag'] in ('img', 'image') or (
            node['tag'] == 'video' and
            (node['attrs'].get('poster') or any(_style_image_url(node['attrs'].get('style', '')))))
        if not image_node and not (
                node['tag'] == 'source' and _picture_parent(fragments, node) is not None) \
                and not any(_style_image_url(node['attrs'].get('style', ''))) \
                and not mapped_style:
            continue
        tag = source[node['start']:node['open']]
        changed = _rewrite_source_attributes(tag, url_by_src)
        if changed == tag:
            continue
        if node['tag'] in ('img', 'image', 'video'):
            replaced += 1
            if node['tag'] == 'img' and width_mode != "preserve":
                width_attr, style = image_style_attrs(width_mode, width)
                previous = node['attrs'].get('style') or ''
                style = previous.rstrip('; \t\r\n') + ';' + style if previous else style
                updates = {'style': style}
                if width_attr:
                    updates['width'] = str(width)
                changed = rewrite_attributes(changed, updates,
                                             remove=('height',) if width_attr else ('height', 'width'))
        changes.append((node['start'], node['open'], changed))
    # Rewrite only URL tokens in inline style blocks.  Keep the existing
    # element-only replacement count for API compatibility; callers still get
    # the changed HTML while historical progress messages remain stable.
    for node in fragments.find('style'):
        css = source[node['open']:node['close']]
        rewritten = css
        for match in reversed(list(_STYLE_URL_RE.finditer(css))):
            old = match.group('url').strip()
            replacement = url_by_src.get(old)
            if not replacement:
                continue
            start, end = match.span('url')
            rewritten = rewritten[:start] + str(replacement) + rewritten[end:]
        if rewritten != css:
            changes.append((node['open'], node['close'], rewritten))
    for start, end, tag in sorted(changes, reverse=True):
        source = source[:start] + tag + source[end:]
    return source, replaced
