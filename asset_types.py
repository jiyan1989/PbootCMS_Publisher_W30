"""Small, bounded file-signature helpers shared by browser-like upload paths.

The native browser picker exposes a MIME type for many files even when the
name has no useful extension.  Python's :mod:`mimetypes` only consults the
filename, which made the desktop path reject otherwise valid no-extension
PNG/PDF/video files when a form used ``accept=image/*`` (or a concrete MIME).
These helpers inspect only a short prefix, never execute a file, and fall back
to the filename mapping when the bytes are not recognisable.
"""

from pathlib import Path
import re
import struct


_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".svg": "image/svg+xml", ".avif": "image/avif", ".heic": "image/heic",
    ".heif": "image/heif", ".jxl": "image/jxl", ".ico": "image/x-icon",
    ".jp2": "image/jp2", ".j2k": "image/jp2", ".jpf": "image/jp2",
    ".jpx": "image/jp2", ".jpm": "image/jp2",
    ".pdf": "application/pdf", ".zip": "application/zip",
    ".gz": "application/gzip", ".bz2": "application/x-bzip2",
    ".xz": "application/x-xz", ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed", ".tar": "application/x-tar",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".opus": "audio/opus", ".amr": "audio/amr",
    ".ape": "audio/ape", ".mid": "audio/midi", ".midi": "audio/midi",
    ".mka": "audio/x-matroska", ".wma": "audio/x-ms-wma",
    ".caf": "audio/x-caf", ".ac3": "audio/vnd.dolby.dd-raw",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v", ".webm": "video/webm", ".mkv": "video/x-matroska", ".ogv": "video/ogg",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
    ".asf": "video/x-ms-asf", ".rm": "video/x-pn-realvideo",
    ".rmvb": "video/vnd.rn-realvideo",
    ".ogv": "video/ogg", ".m3u8": "application/vnd.apple.mpegurl",
    ".m3u": "audio/x-mpegurl", ".ts": "video/mp2t", ".mts": "video/mp2t",
    ".m2ts": "video/mp2t", ".vtt": "text/vtt", ".txt": "text/plain",
    ".csv": "text/csv", ".json": "application/json", ".xml": "application/xml",
    ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff",
    ".woff2": "font/woff2", ".eot": "application/vnd.ms-fontobject",
}


def mime_for_extension(extension):
    """Return the normalized browser MIME for a known extension.

    This small public lookup is used when a WebView supplies a useful
    ``File.type`` for an extensionless drop.  It deliberately does not guess
    unknown/custom MIME values and therefore cannot widen an ``accept`` rule.
    """
    value = str(extension or "").strip().lower()
    if value and not value.startswith("."):
        value = "." + value
    return _EXT_MIME.get(value, "")


def _iso_bmff_brands(raw):
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        return set()
    brands = set()
    # major_brand followed by compatible_brands.  Four-byte ASCII brands are
    # enough to distinguish the browser-visible image/video families.
    for offset in range(8, min(len(raw) - 3, 128), 4):
        brand = raw[offset:offset + 4].lower()
        if all(32 <= byte < 127 for byte in brand):
            brands.add(brand)
    return brands


def sniff_mime(data=b"", filename=""):
    """Return a conservative MIME guess from bounded bytes and filename.

    Bytes take precedence only when a known signature is present.  Otherwise
    the extension mapping is retained so existing browser-style behaviour for
    ordinary named files is unchanged.
    """
    raw = bytes(data or b"")[:128 * 1024]
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if raw.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if raw.startswith((b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")):
        return "image/x-icon"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") or raw.startswith(b"\xffO\xffQ"):
        return "image/jp2"
    # PSD/PSB both use the 8BPS signature; the following two bytes are the
    # documented version (1 for PSD, 2 for PSB).  Require the version too so
    # a random file with only a four-byte coincidence is not accepted as an
    # extensionless image.
    if len(raw) >= 6 and raw.startswith(b"8BPS") and raw[4:6] in (b"\x00\x01", b"\x00\x02"):
        return "image/vnd.adobe.photoshop"
    if raw.startswith(b"%PDF-"):
        return "application/pdf"
    if raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06"):
        return "application/zip"
    if raw.startswith(b"Rar!\x1a\x07"):
        return "application/vnd.rar"
    if raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if raw.startswith(b"\x1f\x8b\x08"):
        return "application/gzip"
    if raw.startswith(b"BZh"):
        return "application/x-bzip2"
    if raw.startswith(b"\xfd7zXZ\x00"):
        return "application/x-xz"
    if raw.startswith(b"ID3") or (len(raw) >= 2 and
                                   raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if raw.startswith(b"RIFF"):
        if raw[8:12] == b"WAVE":
            return "audio/wav"
        if raw[8:12] == b"AVI ":
            return "video/x-msvideo"
    if raw.startswith(b"OggS"):
        # Ogg containers can carry audio or video.  audio/ogg is the safe
        # browser-compatible default; an explicit .ogv name identifies the
        # video variant without requiring a full container parser.
        return "video/ogg" if Path(str(filename or "")).suffix.lower() == ".ogv" else "audio/ogg"
    if raw.startswith(b"fLaC"):
        return "audio/flac"
    if raw.startswith(b"\x1aE\xdf\xa3"):
        suffix = Path(str(filename or "")).suffix.lower()
        if suffix == ".mkv":
            return "video/x-matroska"
        if suffix == ".mka":
            return "audio/x-matroska"
        return "video/webm"
    brands = _iso_bmff_brands(raw)
    if brands:
        if brands & {b"avif", b"avis"}:
            return "image/avif"
        if brands & {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        if brands & {b"qt  "}:
            return "video/quicktime"
        if brands & {b"m4a ", b"M4A ".lower()}:
            return "audio/mp4"
        if brands & {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"mp71",
                     b"dash", b"3gp4", b"3g2a"}:
            return "video/mp4"
    if raw.startswith(b"\xff\x0a") or (len(raw) >= 12 and
                                       raw[4:12] == b"JXL \x0d\x0a\x87\x0a"):
        return "image/jxl"
    # Keep SVG detection bounded and syntax-light; full XML safety validation
    # remains the responsibility of the image upload path.
    text = raw.decode("utf-8-sig", errors="ignore").lstrip()[:4096].lower()
    if text.startswith("<svg") or (text.startswith("<?xml") and "<svg" in text):
        return "image/svg+xml"
    suffix = Path(str(filename or "")).suffix.lower()
    return _EXT_MIME.get(suffix, "")


def sniff_extension(data=b"", filename=""):
    """Return a browser-compatible extension for a recognised MIME/signature."""
    mime = sniff_mime(data, filename)
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tiff",
        "image/svg+xml": ".svg", "image/avif": ".avif", "image/heic": ".heic",
        "image/heif": ".heif", "image/jxl": ".jxl", "image/x-icon": ".ico",
        "image/jp2": ".jp2", "image/vnd.adobe.photoshop": ".psd",
        "application/pdf": ".pdf", "application/zip": ".zip",
        "application/gzip": ".gz", "application/x-bzip2": ".bz2",
        "application/x-xz": ".xz", "application/vnd.rar": ".rar",
        "application/x-7z-compressed": ".7z", "audio/mpeg": ".mp3",
        "audio/wav": ".wav", "audio/ogg": ".ogg", "audio/flac": ".flac",
        "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/opus": ".opus",
        "audio/amr": ".amr", "audio/ape": ".ape", "audio/midi": ".mid",
        "audio/x-matroska": ".mka", "audio/x-ms-wma": ".wma",
        "audio/x-caf": ".caf", "audio/vnd.dolby.dd-raw": ".ac3",
        "video/mp4": ".mp4", "video/x-m4v": ".m4v",
        "video/quicktime": ".mov", "video/webm": ".webm",
        "video/x-matroska": ".mkv", "video/3gpp": ".3gp",
        "video/3gpp2": ".3g2", "video/x-flv": ".flv",
        "video/x-ms-wmv": ".wmv", "video/x-ms-asf": ".asf",
        "video/x-pn-realvideo": ".rm", "video/vnd.rn-realvideo": ".rmvb",
        "video/ogg": ".ogv", "video/mp2t": ".ts",
        "application/vnd.apple.mpegurl": ".m3u8", "audio/x-mpegurl": ".m3u",
        "text/vtt": ".vtt", "text/plain": ".txt", "text/csv": ".csv",
        "application/json": ".json", "application/xml": ".xml",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "font/otf": ".otf", "font/ttf": ".ttf", "font/woff": ".woff",
        "font/woff2": ".woff2", "application/vnd.ms-fontobject": ".eot",
    }.get(mime, Path(str(filename or "")).suffix.lower())


def _positive_dimensions(width, height):
    """Return sane positive integer dimensions, or ``None``.

    This helper is deliberately conservative.  It is used for display and
    read-back evidence only; upload acceptance remains the responsibility of
    the field policy and the format validators.  Capping both axes avoids
    turning malformed headers into an unbounded UI value.
    """
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (1 <= width <= 100_000 and 1 <= height <= 100_000):
        return None
    return width, height


def _tiff_value(raw, base, offset, field_type, count, endian):
    """Read a small TIFF IFD value without following unbounded offsets."""
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2,
             9: 4, 10: 8, 11: 4, 12: 8}
    size = sizes.get(field_type)
    if size is None or count <= 0 or count > 16:
        return None
    total = size * count
    if total <= base:
        data = raw[offset:offset + base]
    else:
        if offset < 0 or offset + total > len(raw):
            return None
        data = raw[offset:offset + total]
    if len(data) < total:
        return None
    try:
        if field_type == 3:
            values = struct.unpack(endian + ("H" * count), data[:2 * count])
        elif field_type == 4:
            values = struct.unpack(endian + ("I" * count), data[:4 * count])
        elif field_type == 8:
            values = struct.unpack(endian + ("h" * count), data[:2 * count])
        elif field_type == 9:
            values = struct.unpack(endian + ("i" * count), data[:4 * count])
        else:
            return None
        return int(values[0]) if values else None
    except (struct.error, ValueError, TypeError):
        return None


def _tiff_dimensions(raw):
    """Read TIFF/BigTIFF width and height from the first IFD."""
    if len(raw) < 8 or raw[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if raw[:2] == b"II" else ">"
    try:
        magic = struct.unpack(endian + "H", raw[2:4])[0]
    except struct.error:
        return None
    if magic == 42:
        if len(raw) < 8:
            return None
        try:
            ifd = struct.unpack(endian + "I", raw[4:8])[0]
            count = struct.unpack(endian + "H", raw[ifd:ifd + 2])[0]
        except (struct.error, IndexError):
            return None
        entry_size, value_base = 12, 4
        cursor = ifd + 2
    elif magic == 43:  # BigTIFF: offset size 8, zero marker, 8-byte count.
        if len(raw) < 16:
            return None
        try:
            offset_size, zero = struct.unpack(endian + "HH", raw[4:8])
            if offset_size != 8 or zero != 0:
                return None
            ifd = struct.unpack(endian + "Q", raw[8:16])[0]
            count = struct.unpack(endian + "Q", raw[ifd:ifd + 8])[0]
        except (struct.error, IndexError):
            return None
        entry_size, value_base = 20, 8
        cursor = ifd + 8
    else:
        return None
    if count > 128 or cursor < 0 or cursor + count * entry_size > len(raw):
        return None
    values = {}
    for index in range(int(count)):
        entry = raw[cursor + index * entry_size:cursor + (index + 1) * entry_size]
        try:
            if magic == 42:
                tag, field_type, number = struct.unpack(endian + "HHI", entry[:8])
                value_offset = cursor + index * entry_size + 8
                inline = entry[8:12]
            else:
                tag, field_type = struct.unpack(endian + "HH", entry[:4])
                number = struct.unpack(endian + "Q", entry[4:12])[0]
                value_offset = cursor + index * entry_size + 12
                inline = entry[12:20]
            if tag not in (256, 257):
                continue
            size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1,
                    7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}.get(field_type)
            if size is None or number <= 0 or number > 16:
                continue
            total = size * int(number)
            if total <= value_base:
                data = inline[:total]
                parsed = _tiff_value(data, value_base, 0, field_type,
                                     int(number), endian)
            else:
                try:
                    offset = struct.unpack(endian + ("I" if magic == 42 else "Q"),
                                           inline[:4 if magic == 42 else 8])[0]
                except struct.error:
                    continue
                parsed = _tiff_value(raw, value_base, int(offset), field_type,
                                     int(number), endian)
            if parsed is not None:
                values[tag] = parsed
        except (struct.error, ValueError, TypeError):
            continue
    return _positive_dimensions(values.get(256), values.get(257))


def _jp2_dimensions(raw):
    """Read dimensions from JP2 ``ihdr`` or a bounded JPEG-2000 SIZ marker."""
    if raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\x0a"):
        cursor = 0
        while cursor + 8 <= len(raw):
            try:
                length = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            except struct.error:
                return None
            box_type = raw[cursor + 4:cursor + 8]
            header = 8
            if length == 1:
                if cursor + 16 > len(raw):
                    return None
                length = struct.unpack(">Q", raw[cursor + 8:cursor + 16])[0]
                header = 16
            elif length == 0:
                length = len(raw) - cursor
            if length < header or length > len(raw) - cursor:
                return None
            if box_type == b"ihdr" and length >= header + 8:
                height, width = struct.unpack(">II", raw[cursor + header:cursor + header + 8])
                return _positive_dimensions(width, height)
            cursor += int(length)
            if cursor > 256 * 1024:
                break
    # Raw codestream: SOC followed by SIZ.  Skip only marker segments with
    # validated lengths; never trust an attacker-controlled segment length.
    if raw.startswith(b"\xff\x4f"):
        cursor = 2
        while cursor + 4 <= len(raw):
            if raw[cursor] != 0xFF:
                cursor += 1
                continue
            marker = raw[cursor + 1]
            cursor += 2
            if marker == 0x51 and cursor + 2 <= len(raw):
                length = struct.unpack(">H", raw[cursor:cursor + 2])[0]
                if length >= 38 and cursor + length <= len(raw):
                    start = cursor + 2 + 2  # Lsiz, Rsiz
                    xsiz, ysiz, xosiz, yosiz = struct.unpack(
                        ">IIII", raw[start:start + 16])
                    return _positive_dimensions(xsiz - xosiz, ysiz - yosiz)
                return None
            if marker in (0x4F, 0xD9):
                continue
            if cursor + 2 > len(raw):
                break
            length = struct.unpack(">H", raw[cursor:cursor + 2])[0]
            if length < 2 or cursor + length > len(raw):
                break
            cursor += length
    return None


def _svg_dimensions(raw):
    try:
        text = raw[:256 * 1024].decode("utf-8-sig", "strict")
    except (UnicodeDecodeError, AttributeError):
        return None
    match = re.search(r"<svg\b[^>]*>", text, re.I | re.S)
    if not match:
        return None
    tag = match.group(0)

    def number(name):
        found = re.search(r"\b" + name + r"\s*=\s*([\"'])([^\"']+)\1",
                          tag, re.I)
        if not found:
            return None
        value = found.group(2).strip()
        # Percentages and viewport-relative units do not represent a fixed
        # intrinsic pixel size; leave those to the browser preview.
        parsed = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:px|pt|pc|mm|cm|in)?",
                              value, re.I)
        if not parsed:
            return None
        try:
            return float(parsed.group(1))
        except ValueError:
            return None

    width, height = number("width"), number("height")
    if width is not None and height is not None:
        return _positive_dimensions(round(width), round(height))
    viewbox = re.search(r"\bviewBox\s*=\s*([\"'])([^\"']+)\1", tag, re.I)
    if viewbox:
        parts = re.split(r"[\s,]+", viewbox.group(2).strip())
        if len(parts) == 4:
            try:
                return _positive_dimensions(round(float(parts[2])), round(float(parts[3])))
            except (ValueError, TypeError):
                pass
    return None


def image_dimensions(data=b"", filename="", mime=""):
    """Return intrinsic ``(width, height)`` from common image containers.

    The parser consumes at most 256 KiB and never decodes pixels or executes
    markup.  It supplements Pillow for formats that the browser/CMS may
    accept but the bundled Pillow build cannot decode (SVG, AVIF/HEIC, JP2,
    PSD and some TIFF variants).  ``None`` means that no trustworthy fixed
    size was available; callers should then let the WebView render it.
    """
    raw = bytes(data or b"")[:256 * 1024]
    if len(raw) < 4:
        return None
    extension = Path(str(filename or "")).suffix.lower()
    content_type = str(mime or "").split(";", 1)[0].strip().lower()
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        return _positive_dimensions(*struct.unpack(">II", raw[16:24]))
    if raw.startswith((b"GIF87a", b"GIF89a")) and len(raw) >= 10:
        return _positive_dimensions(*struct.unpack("<HH", raw[6:10]))
    if raw.startswith(b"BM") and len(raw) >= 26:
        width, height = struct.unpack("<ii", raw[18:26])
        return _positive_dimensions(abs(width), abs(height))
    if raw.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return _tiff_dimensions(raw)
    if raw.startswith((b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")) and len(raw) >= 10:
        width, height = raw[6] or 256, raw[7] or 256
        return _positive_dimensions(width, height)
    if raw.startswith(b"\xff\xd8\xff"):
        cursor = 2
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while cursor + 4 <= len(raw):
            if raw[cursor] != 0xFF:
                cursor += 1
                continue
            while cursor < len(raw) and raw[cursor] == 0xFF:
                cursor += 1
            if cursor >= len(raw):
                break
            marker = raw[cursor]
            cursor += 1
            if marker in {0xD8, 0xD9}:
                continue
            if cursor + 2 > len(raw):
                break
            length = struct.unpack(">H", raw[cursor:cursor + 2])[0]
            if length < 2 or cursor + length > len(raw):
                break
            if marker in sof and length >= 7:
                height, width = struct.unpack(">HH", raw[cursor + 3:cursor + 7])
                return _positive_dimensions(width, height)
            cursor += length
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        if raw[12:16] == b"VP8X" and len(raw) >= 30:
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
            return _positive_dimensions(width, height)
    if raw.startswith(b"8BPS") and len(raw) >= 26:
        height = int.from_bytes(raw[14:18], "big")
        width = int.from_bytes(raw[18:22], "big")
        return _positive_dimensions(width, height)
    if raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\x0a") or raw.startswith(b"\xff\x4f\xff\x51"):
        return _jp2_dimensions(raw)
    if (raw[:4] == b"\x00\x00\x00\x18" and raw[4:8] == b"ftyp" or
            len(raw) >= 12 and raw[4:8] == b"ftyp" and
            content_type.startswith("image/") or extension in {".avif", ".heic", ".heif"}):
        marker = raw.find(b"ispe")
        if marker >= 0 and marker + 16 <= len(raw):
            width, height = struct.unpack(">II", raw[marker + 8:marker + 16])
            return _positive_dimensions(width, height)
    if (content_type == "image/svg+xml" or extension == ".svg" or
            raw.lstrip().startswith((b"<svg", b"<?xml"))):
        return _svg_dimensions(raw)
    return None
