"""Local video/audio/attachment discovery and source-preserving rewrites.

The browser/editor treats these as ordinary media URLs, not article image
galleries.  This module only selects local files; remote, data and root-site
URLs remain untouched and are reported so the caller never silently downloads
third-party content.
"""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from html_fragments import HTMLFragments, attribute_spans, rewrite_attributes
from html_images import (classify_src, resolve_local_src, resolve_document_src,
                         document_base_url, KIND_DATA, KIND_REMOTE, KIND_SITE)
from asset_types import sniff_mime


_MEDIA_ATTRS = {
    # poster is an image and is handled by html_images for local files.  It is
    # retained here only so remote/site posters are reported as preserved
    # resources instead of silently disappearing from diagnostics.
    "video": ("src", "poster"),
    "audio": ("src",),
    "source": ("src",),
    "track": ("src",),
    "embed": ("src",),
    "iframe": ("src",),
    "object": ("data",),
    "param": ("value",),
    "a": ("href",),
}
_MEDIA_EXTS = {
    ".mp4", ".webm", ".ogv", ".ogg", ".mp3", ".wav", ".m4a", ".aac",
    ".flac", ".opus", ".amr", ".ape", ".mid", ".midi", ".mka", ".wma",
    ".caf", ".ac3", ".m3u", ".m3u8",
    ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".3g2", ".flv", ".wmv",
    ".asf", ".rm", ".rmvb", ".ts", ".mts", ".m2ts", ".pdf", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".json", ".xml", ".zip",
    ".rar", ".7z", ".gz", ".bz2", ".xz", ".tar", ".apk", ".otf", ".ttf",
    ".woff", ".woff2", ".eot",
}

_VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".avi", ".mkv", ".m4v",
               ".3gp", ".3g2", ".flv", ".wmv", ".asf", ".rm", ".rmvb",
               ".ts", ".mts", ".m2ts", ".m3u8"}
_AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus",
               ".amr", ".ape", ".mid", ".midi", ".mka", ".wma", ".caf",
               ".ac3", ".m3u"}


def _media_upload_kind(tag, attr, value, parent_tag="", declared_type=""):
    """Map a local HTML media node to the UEditor upload family.

    UEditor exposes separate video/audio/file actions.  The old scanner only
    returned the tag, which caused every local video/audio to use the image
    editor endpoint.  Keep the mapping conservative: unknown attachments use
    the generic file action, while source nodes are classified by extension.
    """
    tag = str(tag or "").lower()
    parent_tag = str(parent_tag or "").lower()
    declared_type = str(declared_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(str(value or "").split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    if (declared_type.startswith("video/") or tag == "video" or
            parent_tag == "video" or suffix in _VIDEO_EXTS):
        return "video"
    if (declared_type.startswith("audio/") or tag == "audio" or
            parent_tag == "audio" or suffix in _AUDIO_EXTS):
        return "audio"
    return "file"


def _looks_like_attachment(tag, attr, value, declared_type=""):
    """Return whether a DOM resource should enter the binary-media path.

    Named files have historically been selected by their extension.  Native
    browser file controls, however, can still expose a useful MIME for an
    extensionless object.  The caller performs the bounded signature probe
    after resolving a local path; this function only handles declarations that
    are already safe to classify without opening a file.
    """
    declared = str(declared_type or "").split(";", 1)[0].strip().lower()
    if declared.startswith(("audio/", "video/", "application/")):
        return True
    if attr == "poster":
        return classify_src(value) in (KIND_REMOTE, KIND_DATA, KIND_SITE)
    if tag in ("video", "audio", "source", "track", "embed", "object"):
        return True
    if tag == "param":
        # Flash/media object fallbacks conventionally use one of these names;
        # arbitrary <param> values are often configuration strings and must
        # not become uploads merely because they contain a dot.
        return Path(str(value or "").split("?", 1)[0].split("#", 1)[0]).suffix.lower() in _MEDIA_EXTS
    return attr in ("href", "src") and Path(
        str(value or "").split("?", 1)[0].split("#", 1)[0]).suffix.lower() in _MEDIA_EXTS


def scan_html_media(html, base_dir, document_url=""):
    """Return local media assets and non-local diagnostic entries."""
    source_html = str(html or "")
    fragments = HTMLFragments(source_html)
    document_base = document_base_url(source_html, document_url)
    assets, problems, seen = [], [], set()
    for node in fragments.nodes:
        attrs = _MEDIA_ATTRS.get(node.get("tag"))
        if not attrs:
            continue
        # <picture><source> is an image candidate, not a binary video/audio
        # asset.  html_images owns its src/srcset handling; scanning it here
        # would upload the same file a second time through the generic file
        # endpoint.  A source under <video>/<audio> remains a media asset.
        parent_tag = ""
        parent = node.get("parent")
        while parent is not None:
            ancestor = fragments.nodes[parent]
            parent_tag = str(ancestor.get("tag", "") or "").lower()
            if parent_tag == "picture":
                break
            if parent_tag in ("video", "audio"):
                break
            parent = ancestor.get("parent")
        if node.get("tag") == "source" and parent_tag == "picture":
            continue
        if node.get("tag") == "param":
            param_name = str(node.get("attrs", {}).get("name", "") or "").strip().lower()
            if param_name not in {"movie", "src", "file", "url"}:
                continue
        for attr in attrs:
            value = str(node.get("attrs", {}).get(attr, "") or "").strip()
            declared = str(node.get("attrs", {}).get("type", "") or "").strip()
            explicit_kind = _looks_like_attachment(node["tag"], attr, value, declared)
            # A bare local download link has no reliable extension clue.  Let
            # it reach the bounded signature probe below; all other unknown
            # attributes retain the old conservative skip behaviour.
            pathless_download = (
                node["tag"] == "a" and attr == "href" and
                not Path(value.split("?", 1)[0].split("#", 1)[0]).suffix)
            if not value or (not explicit_kind and not pathless_download):
                continue
            # The same local resource is often referenced by both
            # ``object data`` and a legacy ``param value`` fallback.  A
            # browser may issue one request for that URL; upload it once and
            # use the shared mapping to rewrite every occurrence.
            key = value
            if key in seen:
                continue
            seen.add(key)
            kind, resolved_value = resolve_document_src(value, base_dir, document_base)
            if kind in (KIND_REMOTE, KIND_DATA, KIND_SITE):
                problems.append({"src": value[:60] + ("…" if len(value) > 60 else ""),
                                 "resolved_src": resolved_value if resolved_value != value else "",
                                 "kind": kind, "reason": "已托管或内联媒体，保留原样"})
                continue
            if kind == "local" and resolved_value != value:
                path = (resolved_value if os.path.isabs(str(resolved_value))
                        and os.path.isfile(str(resolved_value))
                        else resolve_local_src(resolved_value, base_dir))
            else:
                path = resolve_local_src(value, base_dir)
            if not path:
                problems.append({"src": value, "kind": kind,
                                 "reason": "本地媒体文件不存在或无法读取"})
                continue
            # A local extensionless link may still be a real PDF/archive/audio
            # or video object.  Probe only a bounded prefix, never execute the
            # resource, and keep ordinary HTML/text links out of the upload
            # queue.  Image links remain owned by html_images when they are
            # represented as image elements; an anchor to an image is kept as
            # a normal link unless it carries an explicit application/media
            # type, matching the browser's conservative download semantics.
            if not explicit_kind:
                try:
                    with open(path, "rb") as handle:
                        detected = sniff_mime(handle.read(128 * 1024), path)
                except (OSError, IOError):
                    detected = ""
                if not detected or (node["tag"] == "a" and detected.startswith("image/")):
                    continue
            assets.append({"src": value, "path": path, "tag": node["tag"], "attr": attr,
                           "media_kind": _media_upload_kind(node["tag"], attr, value,
                                                              parent_tag,
                                                              declared)})
    return assets, problems


def rewrite_html_media(html, url_by_src):
    """Replace only selected media attributes, preserving all other bytes."""
    source = str(html or "")
    if not url_by_src:
        return source, 0
    fragments = HTMLFragments(source)
    changes, replaced = [], 0
    for node in fragments.nodes:
        attrs = _MEDIA_ATTRS.get(node.get("tag"))
        if not attrs:
            continue
        tag = source[node["start"]:node["open"]]
        updates = {}
        for attr in attrs:
            old = node.get("attrs", {}).get(attr, "")
            if old in url_by_src:
                updates[attr] = str(url_by_src[old])
        if updates:
            changes.append((node["start"], node["open"], rewrite_attributes(tag, updates)))
            replaced += len(updates)
    for start, end, tag in sorted(changes, reverse=True):
        source = source[:start] + tag + source[end:]
    return source, replaced
