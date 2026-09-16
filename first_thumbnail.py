"""Read a static same-origin image without executing backend routes.

Only the server's currently served bytes can be recovered for existing images;
this cannot recover an original that the server previously transformed.
"""
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from PIL import Image
from http_transport import origin, permitted_transition
from asset_types import sniff_mime


def image_identity(value, base):
    url = urljoin(base.rstrip('/') + '/', value)
    parsed = urlsplit(url)
    target = origin(url)
    reference = origin(base)
    # When the configured backend starts on HTTP, its canonical HTTPS object
    # URL is the same browser-visible resource. Normalize both default-port
    # spellings to the HTTPS key, while keeping HTTPS→HTTP and non-default
    # ports distinct/unsafe.
    if (reference[0] == 'http' and reference[2] == 80 and
            target[1] == reference[1] and target[2] in (80, 443) and
            (target == reference or permitted_transition(base, url))):
        target = ('https', reference[1], 443)
    return target, unquote(parsed.path)


def static_image_url(value, base, admin_url=''):
    original = urlsplit(value)
    if not original.scheme and not original.netloc and not value.startswith('/'):
        raise RuntimeError('已有首图的相对地址缺少确定的网页基准，请选择独立文件')
    url = urljoin(base.rstrip('/') + '/', value)
    if not permitted_transition(base, url):
        raise RuntimeError('首图来源跨来源或协议不同，请选择独立本地文件')
    parsed = urlsplit(url)
    if re.search(r'%(?![0-9a-f]{2})|%(?:2f|5c|2e|25|3f|23|3b)', parsed.path, re.I):
        raise RuntimeError('首图路径存在编码歧义')
    path = unquote(parsed.path, errors='strict')
    admin_path = unquote(urlsplit(admin_url).path).rstrip('/')
    if ((admin_path and (path.lower() == admin_path.lower() or path.lower().startswith(admin_path.lower() + '/'))) or
            re.search(r'/(?:Message|Content|ContentSort)/(?:del|mod|edit|add|index)(?:/|$)', path, re.I)):
        raise RuntimeError('首图地址不能指向后台业务路由')
    if (any(ord(c) < 32 for c in path) or any(c in path for c in '\\;?%') or
            '//' in path or any(part in ('.', '..') for part in path.split('/')) or
            re.search(r'\.(?:php\d*|phtml|aspx?|jsp)(?:/|$)', path, re.I)):
        raise RuntimeError('首图不是可确认的静态图片路径')
    name = path.rsplit('/', 1)[-1]
    has_known_suffix = bool(re.search(
        r'\.(?:jpe?g|jpe|png|gif|webp|bmp|ico|tiff?|avif|heic|heif|jxl|jp2|j2k|jpf|jpx|jpm|psd|svg)$',
        path, re.I))
    if not has_known_suffix:
        # Some CMS/object-storage URLs deliberately omit an extension.  They
        # are safe to consider only when the path segment itself is a plain
        # opaque filename; the response must still prove an image MIME and a
        # matching signature before it can be uploaded as a thumbnail.
        if not name or '.' in name:
            raise RuntimeError('首图不是可确认的静态图片文件，请选择独立本地文件')
    pairs = parse_qsl(parsed.query, keep_blank_values=True, errors='strict')
    if (len(dict(pairs)) != len(pairs) or ';' in parsed.query or
            any(key not in ('v', 'ver', 'version', 't', '_', 'timestamp') or
                not re.fullmatch(r'[A-Za-z0-9_.:-]*', val) for key, val in pairs)):
        raise RuntimeError('首图含动态或未知参数，尚不能安全读取；请选独立文件')
    if (any(c in name for c in '<>:"|?*') or name.endswith((' ', '.')) or
            re.match(r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', name, re.I)):
        raise RuntimeError('首图文件名无法原样保存到本机，请选择独立文件')
    return parsed._replace(fragment='').geturl(), name


@contextmanager
def downloaded_source(client, value, ctx):
    url, filename = static_image_url(value, client.base_url, client.admin_url)
    policy = getattr(client, '_upload_policies', {}).get(('field', 'ico'))
    if policy is None:
        raise RuntimeError('尚未读取缩略图实际上传配置')
    limit = policy.max_bytes or 100 * 1024 * 1024
    visited = set()
    with tempfile.TemporaryDirectory(prefix='pboot-first-thumbnail-') as folder:
        path = os.path.join(folder, filename)
        for _ in range(6):
            ctx.check_cancelled()
            url, _ = static_image_url(url, client.base_url, client.admin_url)
            if url in visited:
                raise RuntimeError('首图读取重定向循环')
            visited.add(url)
            response = client.session.get(url, stream=True, allow_redirects=False, timeout=(10, 30))
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise RuntimeError('首图跳转缺少地址')
                    url, _ = static_image_url(urljoin(url, location), client.base_url, client.admin_url)
                    continue
                if response.status_code != 200:
                    raise RuntimeError(f'首图读取失败（HTTP {response.status_code}）')
                static_image_url(getattr(response, 'url', '') or url, client.base_url, client.admin_url)
                declared = response.headers.get('Content-Length')
                if declared and (not declared.isdigit() or int(declared) > limit):
                    raise RuntimeError('首图长度无效或超过当前读取/上传上限')
                content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if content_type in ('text/html', 'application/json', 'text/plain'):
                    raise RuntimeError('首图返回的不是图片文件')
                size = 0
                with open(path, 'xb') as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        ctx.check_cancelled()
                        size += len(chunk)
                        if size > limit:
                            raise RuntimeError('首图超过当前读取/上传上限')
                        handle.write(chunk)
                if not size or (declared and not response.headers.get('Content-Encoding') and size != int(declared)):
                    raise RuntimeError('首图响应不完整')
                if not re.search(r'\.(?:jpe?g|jpe|png|gif|webp|bmp|ico|tiff?|avif|heic|heif|jxl|jp2|j2k|jpf|jpx|jpm|psd|svg)$', path, re.I):
                    with open(path, 'rb') as probe:
                        detected_type = sniff_mime(probe.read(128 * 1024), path)
                    if not content_type.startswith('image/') and not detected_type.startswith('image/'):
                        raise RuntimeError('无扩展名首图缺少明确图片 MIME')
                    if not content_type.startswith('image/'):
                        content_type = detected_type
                # Verify only. Do not resize, normalize EXIF, flatten GIF or
                # transcode the bytes that will be passed to the upload control.
                try:
                    _verify_image_file(path, content_type=content_type)
                except Exception as exc:
                    raise RuntimeError('首图文件无效或当前读取器不支持该格式') from exc
                break
            finally:
                response.close()
        else:
            raise RuntimeError('首图重定向过多')
        ctx.check_cancelled()
        ctx.log('已有首图将按服务器当前文件独立上传；无法恢复服务器此前已裁切/加水印的原始文件')
        yield path


def _verify_svg(path):
    """Validate SVG syntax without executing browser content."""
    with open(path, 'rb') as handle:
        raw = handle.read(100 * 1024 * 1024 + 1)
    if not raw or len(raw) > 100 * 1024 * 1024:
        raise ValueError('SVG为空或过大')
    text = raw.decode('utf-8-sig')
    if (re.search(r'<!DOCTYPE|<!ENTITY|<\s*(?:script|foreignObject)\b', text, re.I)
            or re.search(r'javascript:|@import|url\s*\(\s*(?:https?:|data:|//)', text, re.I)):
        raise ValueError('SVG含脚本或外部实体')
    root = ET.fromstring(text)
    if str(root.tag or '').split('}', 1)[-1].lower() != 'svg':
        raise ValueError('SVG根节点无效')
    for element in root.iter():
        for key, value in element.attrib.items():
            lowered = str(key).lower()
            candidate = str(value or '').strip().lower()
            if lowered.endswith('href') and (candidate.startswith(
                    ('http:', 'https:', 'javascript:', 'data:', '//'))):
                raise ValueError('SVG含外部或可执行资源')
            if lowered.split('}', 1)[-1].startswith('on'):
                raise ValueError('SVG含事件脚本')


def _verify_image_file(path, *, content_type=''):
    """Verify an image without transcoding it.

    Pillow deliberately has no built-in decoder for every format that a
    modern browser/CMS upload control may accept.  In particular, AVIF,
    HEIF/HEIC, JPEG XL, JPEG 2000 and Photoshop may be valid CMS assets while
    ``Image.verify`` raises ``UnidentifiedImageError``.  Keep the old strict
    Pillow check for formats it understands, and use bounded magic-byte checks
    for these container formats.  An unknown extension or unknown signature
    still fails closed; this is validation only and never makes a file
    executable.
    """
    suffix = os.path.splitext(str(path or ""))[1].lower()
    content_type = str(content_type or '').split(';', 1)[0].strip().lower()
    if suffix == '.svg' or content_type == 'image/svg+xml':
        _verify_svg(path)
        return
    try:
        with Image.open(path) as image:
            image.verify()
        return
    except Exception as pillow_error:
        if suffix not in {'.avif', '.heic', '.heif', '.jxl', '.jp2', '.j2k',
                          '.jpf', '.jpx', '.jpm', '.psd'} and not content_type in {
                'image/avif', 'image/heic', 'image/heif', 'image/jxl',
                'image/jp2', 'image/vnd.adobe.photoshop'}:
            raise
        with open(path, 'rb') as handle:
            raw = handle.read(64 * 1024)
        effective = suffix
        if not effective:
            effective = {
                'image/avif': '.avif', 'image/heic': '.heic',
                'image/heif': '.heif', 'image/jxl': '.jxl',
                'image/jp2': '.jp2', 'image/vnd.adobe.photoshop': '.psd',
            }.get(content_type, '')
        if effective in {'.jp2', '.j2k', '.jpf', '.jpx', '.jpm'}:
            # JPEG 2000 boxed files carry the JP2 signature box; raw codestream
            # variants use the SOC/SIZ marker pair.  The check is bounded and
            # never attempts to parse attacker-controlled box lengths.
            if (raw.startswith(b'\x00\x00\x00\x0cjP  \r\n\x87\x0a') or
                    raw.startswith(b'\xff\x4f\xff\x51')):
                return
        elif effective == '.psd':
            # PSD/PSB both begin with 8BPS; only the documented version bytes
            # are accepted so a random file with a coincidental prefix fails.
            if len(raw) >= 6 and raw[:4] == b'8BPS' and raw[4:6] in (b'\x00\x01', b'\x00\x02'):
                return
        elif effective == '.jxl':
            # JPEG XL codestream or ISO-BMFF container signature.
            if raw.startswith(b'\xff\x0a') or (
                    len(raw) >= 12 and raw[4:12] == b'JXL \x0d\x0a\x87\x0a'):
                return
        elif len(raw) >= 12 and raw[4:8] == b'ftyp':
            # AVIF/HEIF brands are stored in the first compatible-brand slot
            # and may be followed by additional compatible brands.
            brands = []
            for offset in range(8, min(len(raw) - 3, 64), 4):
                brand = raw[offset:offset + 4].lower()
                if all(32 <= byte < 127 for byte in brand):
                    brands.append(brand)
            avif_brands = {b'avif', b'avis'}
            heif_brands = {b'heic', b'heix', b'hevc', b'hevx',
                           b'mif1', b'msf1'}
            if effective == '.avif' and any(brand in avif_brands for brand in brands):
                return
            if effective in {'.heic', '.heif'} and any(brand in heif_brands for brand in brands):
                return
        raise pillow_error
