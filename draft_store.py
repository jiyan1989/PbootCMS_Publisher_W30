# -*- coding: utf-8 -*-
"""Versioned, non-secret persistence for recoverable UI drafts.

Only the small amount of state needed to rebuild a workflow is stored here.
Parsed HTML fields are deliberately not accepted: they can be recreated from
``html_path``.  ``overrides`` are retained because a user edit cannot be
recreated; callers may pass ``source_fields`` so unchanged values are removed
before writing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from gallery_plan import normalize_gallery_plan
import math
import ntpath
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from file_metadata import remember_declared_mime

from client_utils import get_base_dir


DRAFT_FORMAT = "pboot-publisher-draft"
DRAFT_SCHEMA_VERSION = 1
SUPPORTED_WORKFLOWS = frozenset(("publish", "edit", "batch"))

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_PART_RE = re.compile(
    r"(?:^|[_-])(password|passwd|pass|pwd|cookie|cookies|session|csrf|"
    r"formcheck|token|secret|credential|credentials|authorization|auth)"
    r"(?:$|[_-])", re.I)
_WRITE_LOCK = threading.RLock()

MAX_MAPPING_ITEMS = 512
MAX_OVERRIDE_ITEMS = 512
MAX_OVERRIDE_BYTES = 8 * 1024 * 1024
# Kept as compatibility names for callers that imported the former limits;
# they are no longer applied because a browser-selected queue must not be
# silently truncated by a desktop-only count.
MAX_PATHS = None
MAX_BATCH_ITEMS = None
MAX_JSON_BYTES = 10 * 1024 * 1024
# Drafts are local recovery artifacts, not an unbounded media archive.  A
# single file and the complete snapshot set are bounded independently so an
# accidental folder selection cannot exhaust the workstation disk.
MAX_ASSET_FILE_BYTES = 256 * 1024 * 1024
MAX_ASSET_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ASSET_FILES = 2000


class DraftStoreError(ValueError):
    """The requested draft is invalid, corrupt, or from a newer schema."""


def _clean_token(value, label, max_length=80):
    value = str(value or "").strip()
    if not value or len(value) > max_length or not _TOKEN_RE.fullmatch(value):
        raise DraftStoreError(f"{label}无效")
    return value


def _clean_workflow(value):
    value = str(value or "").strip().lower()
    if value not in SUPPORTED_WORKFLOWS:
        raise DraftStoreError("草稿流程必须是 publish、edit 或 batch")
    return value


def _clean_text(value, max_length, label="文本"):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        raise DraftStoreError(f"{label}类型无效")
    value = _CONTROL_RE.sub("", str(value))
    if len(value) > max_length:
        raise DraftStoreError(f"{label}过长")
    return value


def _safe_key(value, max_length=128):
    key = _clean_text(value, max_length, "字段名").strip()
    return key if key and not _is_sensitive_key(key) else ""


def _is_sensitive_key(value):
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value or ""))
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    return bool(_SENSITIVE_PART_RE.search(normalized))


def _looks_absolute(path):
    # ntpath keeps Windows drive/UNC paths valid even when unit tests run on a
    # non-Windows host; os.path covers the native platform.
    return os.path.isabs(path) or ntpath.isabs(path)


def _clean_path(value):
    path = _clean_text(value, 4096, "文件路径").strip()
    if not path or not _looks_absolute(path):
        return ""
    # Paths are data only: never resolve them or touch their target.  This
    # preserves a temporarily disconnected drive while removing redundant
    # separators and dot segments.
    return ntpath.normpath(path) if ntpath.isabs(path) else os.path.normpath(path)


def _clean_path_list(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise DraftStoreError("文件路径列表类型无效")
    result = []
    seen = set()
    # Do not silently truncate a browser-selected queue at a desktop-only
    # count.  The draft envelope's byte limit and explicit asset snapshot
    # warnings remain the bounded safety mechanisms.
    for raw in values:
        path = _clean_path(raw)
        marker = os.path.normcase(path)
        if path and marker not in seen:
            seen.add(marker)
            result.append(path)
    return result


def _first(mapping, *keys, default=None):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _clean_mapping(values):
    if not isinstance(values, dict):
        return {}
    result = {}
    # Do not silently drop fields selected by a browser form.  The draft
    # envelope byte limit is the bounded safety mechanism; a desktop-only
    # count would change the requested mapping after a restart.
    for raw_key, raw_value in values.items():
        key = _clean_text(raw_key, 128, "映射字段名").strip()
        if not key:
            continue
        result[key] = _clean_text(raw_value, 256, "映射目标字段").strip()
    return result


def _clean_overrides(values, source_fields=None):
    if not isinstance(values, dict):
        return {}
    source_fields = source_fields if isinstance(source_fields, dict) else {}
    result = {}
    total = 0
    for raw_key, raw_value in values.items():
        key = _safe_key(raw_key)
        if not key:
            continue
        # Field overrides are textual in both publish and edit workflows.
        # Keeping this constraint also prevents a nested object from smuggling
        # cookie/password structures into the otherwise non-secret store.
        value = _clean_text(raw_value, 4 * 1024 * 1024, "手动修改值")
        if key in source_fields and str(source_fields.get(key, "") or "") == value:
            continue
        total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
        if total > MAX_OVERRIDE_BYTES:
            raise DraftStoreError("手动修改内容过大，无法保存草稿")
        result[key] = value
    return result


def _clean_backend_fields(values):
    """Keep editable CMS field choices without allowing nested or secret data."""
    if not isinstance(values, dict):
        return {}
    result = {}
    total = 0
    for raw_key, raw_value in values.items():
        key = _safe_key(raw_key)
        if not key:
            continue
        if isinstance(raw_value, (list, tuple)):
            value = [_clean_text(item, 65536, "后台字段值")
                     for item in raw_value
                     if not isinstance(item, (dict, list, tuple, set))]
            size = sum(len(item.encode("utf-8")) for item in value)
        elif isinstance(raw_value, (dict, set)):
            continue
        else:
            value = _clean_text(raw_value, 65536, "后台字段值")
            size = len(value.encode("utf-8"))
        total += len(key.encode("utf-8")) + size
        if total > 1024 * 1024:
            raise DraftStoreError("后台字段设置过大，无法保存草稿")
        result[key] = value
    return result


def _clean_json_scalar(value, max_text=4096):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(-(2 ** 53), min(2 ** 53, value))
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _clean_text(value, max_text, "偏好值")
    if isinstance(value, (list, tuple)):
        cleaned = []
        for item in value:
            if isinstance(item, (dict, list, tuple, set)):
                continue
            cleaned.append(_clean_json_scalar(item, max_text=max_text))
        return cleaned
    return None


def _clean_preferences(values, aliases):
    result = {}
    if isinstance(values, dict):
        for raw_key, raw_value in values.items():
            key = _safe_key(raw_key, 80)
            if key:
                result[key] = _clean_json_scalar(raw_value)
    # Direct snapshot aliases are normalized to stable names so a future UI
    # can migrate independently of today's camelCase property names.
    alias_table = {
        "width_mode": ("width_mode", "width"),
        "thumbnail_source": ("thumbnail_source", "ico"),
        "cover_mode": ("cover_mode", "cover"),
        "carousel_size": ("carousel_size", "carouselSize"),
        "carousel_width": ("carousel_width", "carouselW"),
        "carousel_height": ("carousel_height", "carouselH"),
        "carousel_mode": ("carousel_mode", "carouselMode"),
    }
    for target, keys in alias_table.items():
        value = _first(aliases, *keys)
        if value is not None:
            result[target] = _clean_json_scalar(value)
    return result


def _clean_flags(values, aliases):
    result = {}
    if isinstance(values, dict):
        for raw_key, raw_value in values.items():
            key = _safe_key(raw_key, 80)
            if key:
                result[key] = bool(raw_value)
    for target, keys in {
            "istop": ("istop", "top"),
            "isrecommend": ("isrecommend", "rec"),
            "isheadline": ("isheadline", "head"),
            "cover_all": ("cover_all",),
            }.items():
        value = _first(aliases, *keys)
        if value is not None:
            result[target] = bool(value)
    return result


def _clean_batch_items(values):
    """Keep queue identity/progress, never parsed fields or article bodies."""
    if not isinstance(values, (list, tuple)):
        return []
    allowed_text = {
        "id": 128, "category": 256, "article_id": 128, "status": 64,
        "title": 512, "error": 2000,
    }
    result = []
    # A native browser queue has no arbitrary 1000-item boundary. Preserve
    # every item here; oversized/corrupt envelopes are rejected by the JSON
    # byte limit rather than being silently rewritten to a different queue.
    for raw in values:
        if isinstance(raw, str):
            path = _clean_path(raw)
            if path:
                result.append({"html_path": path})
            continue
        if not isinstance(raw, dict):
            continue
        item = {}
        path = _clean_path(_first(raw, "html_path", "html", "path", default=""))
        if path:
            item["html_path"] = path
        for key, limit in allowed_text.items():
            if key in raw and raw[key] is not None:
                item[key] = _clean_text(raw[key], limit, f"批量项{key}")
        if "attempts" in raw:
            try:
                item["attempts"] = max(0, min(100, int(raw["attempts"])))
            except (TypeError, ValueError):
                pass
        # Per-item mapping/overrides/settings are part of the browser queue
        # contract. They must not be collapsed into queue-wide defaults when
        # a draft is restored after restart.
        if "mapping" in raw:
            item["mapping"] = _clean_mapping(raw.get("mapping"))
        if "overrides" in raw:
            item["overrides"] = _clean_overrides(raw.get("overrides"))
        if "settings" in raw:
            settings = _clean_batch_settings(raw.get("settings"))
            if settings is not None:
                item["settings"] = settings
        if item:
            result.append(item)
    return result


def _clean_asset_map(values):
    """Keep only absolute original -> snapshot path pairs.

    The map is metadata for recovery; it is never used as a command or
    followed outside ``DraftStore``'s validated root.  Paths are normalized
    here so old drafts can be loaded even when they contain Windows slashes.
    """
    if not isinstance(values, dict):
        return {}
    result = {}
    for raw_source, raw_target in values.items():
        source = _clean_path(raw_source)
        target = _clean_path(raw_target)
        if source and target:
            result[source] = target
    return result


def _clean_asset_mimes(values):
    """Keep browser ``File.type`` hints for selected local assets only.

    A dropped file can have no useful extension or short magic signature while
    Chromium still exposes a trustworthy MIME.  The hint is non-secret draft
    metadata; constrain both its key and value so a hand-edited draft cannot
    turn it into a path or header injection channel.
    """
    if not isinstance(values, dict):
        return {}
    result = {}
    mime_re = re.compile(
        r"[a-z0-9][a-z0-9!#$&^_.+\-]*/"
        r"[a-z0-9][a-z0-9!#$&^_.+\-]*$", re.I)
    for raw_path, raw_mime in values.items():
        path = _clean_path(raw_path)
        mime = str(raw_mime or "").split(";", 1)[0].strip().lower()
        if path and mime_re.fullmatch(mime) and len(mime) <= 128:
            result[path] = mime
    return result


def _clean_submitter(value):
    """Keep the inert browser submit-button descriptor used by a batch item.

    A submitter is page-owned transport metadata, not an action supplied by
    the local draft. Preserve only the attributes that the client validates
    against the freshly-read DOM (plus image-click coordinates when present).
    """
    if not isinstance(value, dict):
        return None
    result = {}
    text_fields = {
        "name": 256, "type": 32, "value": 4096, "formaction": 65536,
        "formmethod": 32, "formenctype": 128, "formtarget": 256,
    }
    for key, limit in text_fields.items():
        if key in value and value[key] is not None:
            result[key] = _clean_text(value[key], limit, "提交按钮属性")
    if "formnovalidate" in value:
        result["formnovalidate"] = bool(value["formnovalidate"])
    for key in ("x", "y", "click_x", "click_y", "index"):
        if key not in value:
            continue
        try:
            number = int(value[key])
        except (TypeError, ValueError):
            continue
        if -100000 <= number <= 100000:
            result[key] = number
    return result or None


def _clean_gallery_plan(value):
    """Sanitize a gallery plan while retaining an explicit empty plan."""
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    try:
        plan = normalize_gallery_plan(value, check_files=False)
    except (TypeError, ValueError):
        return None
    cleaned = []
    for item in plan:
        kind = item.get("kind")
        raw_value = item.get("value", "")
        if kind == "file":
            item_value = _clean_path(raw_value)
        else:
            item_value = _clean_text(raw_value, 65536, "图集图片地址").strip()
        if not item_value:
            continue
        cleaned.append({
            "kind": kind,
            "value": item_value,
            "title": _clean_text(item.get("title", ""), 4096, "图集标题"),
        })
    return cleaned


def _clean_batch_settings(values):
    """Keep one batch record's independent publish settings.

    Batch records are submitted as independent browser forms. Sharing the
    queue-wide state after a restart silently changes the requested column,
    mapping, media, flags, or clicked submitter, so these settings are stored
    with the same bounded/safe rules as a normal publish draft.
    """
    if not isinstance(values, dict):
        return None
    result = {
        "scode": _clean_text(_first(values, "scode", "category", "cat", default=""),
                             256, "批量栏目").strip(),
        "mapping": _clean_mapping(_first(values, "mapping", default={})),
        "backendFields": _clean_backend_fields(_first(
            values, "backendFields", "backend_fields", default={})),
        "width": _clean_text(_first(values, "width", default=""), 64, "批量宽度").strip(),
        "manualImages": _clean_path_list(_first(
            values, "manualImages", "manual_images", default=[])),
        "ico": _clean_text(_first(values, "ico", default="none"), 32, "批量缩略图模式").strip(),
        "thumbPath": _clean_path(_first(
            values, "thumbPath", "thumbnail_path", default="")),
        "thumbUrl": _clean_text(_first(
            values, "thumbUrl", "thumbnail_url", default=""), 65536, "批量缩略图地址"),
        "carouselImages": _clean_path_list(_first(
            values, "carouselImages", "carousel_images", default=[])),
        "attachmentPaths": _clean_path_list(_first(
            values, "attachmentPaths", "attachment_paths", default=[])),
        "carouselSize": _clean_text(_first(
            values, "carouselSize", "carousel_size", default="original"), 64, "批量图集尺寸").strip(),
        "insertStrategy": _clean_text(_first(
            values, "insertStrategy", "insert_strategy", default="top"), 64, "批量插入策略").strip(),
        "top": bool(values.get("top")),
        "rec": bool(values.get("rec")),
        "head": bool(values.get("head")),
        "flagChanges": _clean_flags(values.get("flagChanges", values.get("flag_changes")), values),
        "checkLinks": values.get("checkLinks", values.get("link_check_enabled")) is True,
        "submitter": _clean_submitter(values.get("submitter")),
    }
    for target, aliases, default in (
            ("carouselW", ("carouselW", "carousel_width"), 800),
            ("carouselH", ("carouselH", "carousel_height"), 800)):
        raw = _first(values, *aliases, default=default)
        try:
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError
            number = max(1, min(100000, number))
            result[target] = int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            result[target] = default
    plan = _clean_gallery_plan(_first(values, "galleryPlan", "gallery_plan", default=None))
    if plan is not None:
        result["galleryPlan"] = plan
    link_policy = values.get("linkPolicy", values.get("link_policy"))
    if isinstance(link_policy, dict):
        result["linkPolicy"] = {
            "applyVerified": link_policy.get("applyVerified") is True,
            "removeUnmatched": link_policy.get("removeUnmatched") is True,
        }
    # Keep only modes understood by the UI. Unknown values would be rejected
    # at publish time; dropping them is safer than replaying arbitrary text.
    if result["ico"] not in {"none", "file", "url", "clear", "first"}:
        result["ico"] = "none"
    return result


def _clean_image_replacements(values):
    """Keep recoverable正文图片替换 intents without storing image bytes.

    A replacement may be a local file upload or a property-only edit (alt /
    width / height).  The actual file is snapshotted by ``_asset_paths``;
    these records only carry the occurrence guard and explicit user intent.
    """
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    # A browser can edit every image in a long article.  Preserve the full
    # intent list; JSON size and per-value bounds still prevent unbounded
    # drafts, while a fixed count would silently omit later replacements.
    for raw in values:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0:
            continue
        expected = _clean_text(raw.get("expected_src", ""), 65536, "原图片地址")
        fingerprint = _clean_text(raw.get("tag_fingerprint", ""), 128, "图片指纹")
        if not expected or not fingerprint:
            continue
        local_path = _clean_path(raw.get("local_path", ""))
        item = {"index": index, "expected_src": expected,
                "tag_fingerprint": fingerprint, "local_path": local_path}
        if "new_alt" in raw:
            item["new_alt"] = _clean_text(raw.get("new_alt", ""), 4096, "图片 alt")
        for key, label in (("new_width", "图片宽度"), ("new_height", "图片高度")):
            if key in raw:
                value = _clean_text(raw.get(key, ""), 32, label).strip()
                if value and not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:px|%|em|rem|vw|vh)?|auto)", value, re.I):
                    raise DraftStoreError(f"{label}格式无效")
                item[key] = value
        if not local_path and not any(key in item for key in ("new_alt", "new_width", "new_height")):
            continue
        result.append(item)
    return result


def sanitize_draft(workflow, draft, source_fields=None):
    """Return the canonical, deliberately small draft payload."""
    workflow = _clean_workflow(workflow)
    if not isinstance(draft, dict):
        raise DraftStoreError("草稿数据必须是对象")

    media = draft.get("media") if isinstance(draft.get("media"), dict) else {}
    html_path = _clean_path(_first(draft, "html_path", "html", "file_path", default=""))
    html_paths = _clean_path_list(_first(
        draft, "html_paths", "file_paths", "files", default=[]))
    if workflow == "batch" and html_path and html_path not in html_paths:
        html_paths.insert(0, html_path)

    manual_images = _clean_path_list(_first(
        media, "manual_images", "image_paths",
        default=_first(draft, "manual_images", "manualImages", "image_paths", default=[])))
    carousel_images = _clean_path_list(_first(
        media, "carousel_images", "carousel_paths",
        default=_first(draft, "carousel_images", "carouselImages", "carousel_paths", default=[])))
    attachment_paths = _clean_path_list(_first(
        media, "attachment_paths",
        default=_first(draft, "attachment_paths", default=[])))
    thumbnail_path = _clean_path(_first(
        media, "thumbnail_path", "thumb_path",
        default=_first(draft, "thumbnail_path", "thumbPath", default="")))
    image_replacements = _clean_image_replacements(media.get("image_replacements"))
    image_content_hash = _clean_text(
        _first(media, "image_content_hash", default=""), 128, "正文图片快照哈希")

    payload = {
        "category": _clean_text(
            _first(draft, "category", "scode", "cat", default=""), 256, "栏目").strip(),
        "article_id": _clean_text(
            _first(draft, "article_id", "articleId", default=""), 128, "文章 ID").strip(),
        "html_path": html_path,
        "html_paths": html_paths,
        "mapping": _clean_mapping(draft.get("mapping")),
        "overrides": _clean_overrides(draft.get("overrides"), source_fields),
        "backend_fields": _clean_backend_fields(draft.get("backend_fields")),
        "media": {
            "manual_images": manual_images,
            "carousel_images": carousel_images,
            "attachment_paths": attachment_paths,
            "thumbnail_path": thumbnail_path,
            "thumbnail_url": _clean_text(_first(media, 'thumbnail_url',
                default=_first(draft, 'thumbnail_url', 'thumbUrl', default='')), 65536, '缩略图地址'),
            "image_replacements": image_replacements,
            "image_content_hash": image_content_hash,
        },
        "flags": _clean_flags(draft.get("flags"), draft),
        "preferences": _clean_preferences(draft.get("preferences"), draft),
        "submitter": _clean_submitter(draft.get("submitter")),
        "batch_items": _clean_batch_items(
            _first(draft, "batch_items", "items", "queue", default=[])),
        "asset_map": _clean_asset_map(draft.get("asset_map")),
        "asset_mimes": _clean_asset_mimes(
            _first(draft, "asset_mimes", "file_mimes", default={})),
        "asset_warnings": [
            _clean_text(item, 1000, "草稿素材提示")
            for item in (draft.get("asset_warnings") or [])[:100]
            if not isinstance(item, (dict, list, tuple, set))
        ],
    }
    if 'gallery_plan' in media:
        payload['media']['gallery_plan'] = normalize_gallery_plan(media['gallery_plan'], check_files=False)
    return payload


class DraftStore:
    """One latest draft per site and workflow, isolated on disk."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else get_base_dir() / "drafts"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._root_resolved = self.root.resolve()

    def _path(self, site_key, workflow, create=False):
        site_key = _clean_token(site_key, "site_key")
        workflow = _clean_workflow(workflow)
        site_dir = self.root / site_key
        if site_dir.exists() and site_dir.is_symlink():
            raise DraftStoreError("草稿站点目录不安全")
        candidate = (site_dir / f"{workflow}.json").resolve(strict=False)
        if self._root_resolved != candidate and self._root_resolved not in candidate.parents:
            raise DraftStoreError("草稿路径越界")
        if create:
            site_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(site_dir, 0o700)
            except OSError:
                pass
        return candidate

    @staticmethod
    def _envelope(site_key, workflow, payload):
        return {
            "format": DRAFT_FORMAT,
            "schema_version": DRAFT_SCHEMA_VERSION,
            "site_key": site_key,
            "workflow": workflow,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "draft": payload,
        }

    @staticmethod
    def _asset_paths(payload):
        """Yield every local file path that can be needed to resume a draft."""
        result = []
        def add(value):
            if isinstance(value, str) and value and _looks_absolute(value):
                result.append(value)
        add(payload.get("html_path"))
        for value in payload.get("html_paths", []):
            add(value)
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        for key in ("manual_images", "carousel_images", "attachment_paths"):
            for value in media.get(key, []) or []:
                add(value)
        add(media.get("thumbnail_path", ""))
        plan = media.get("gallery_plan")
        if isinstance(plan, list):
            for item in plan:
                if isinstance(item, dict) and item.get("kind") == "file":
                    add(item.get("value", ""))
        for item in media.get("image_replacements", []) or []:
            if isinstance(item, dict):
                add(item.get("local_path", ""))
        for item in payload.get("batch_items", []) or []:
            if isinstance(item, dict):
                add(item.get("html_path", ""))
                settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
                for key in ("manualImages", "carouselImages", "attachmentPaths",
                            "manual_images", "carousel_images", "attachment_paths"):
                    if key in settings:
                        for value in settings.get(key, []) or []:
                            add(value)
                add(settings.get("thumbPath", settings.get("thumbnail_path", "")))
                plan = settings.get("galleryPlan", settings.get("gallery_plan"))
                if isinstance(plan, list):
                    for entry in plan:
                        if isinstance(entry, dict) and entry.get("kind") == "file":
                            add(entry.get("value", ""))
                for replacement in settings.get("imageReplacements",
                                                settings.get("image_replacements", [])) or []:
                    if isinstance(replacement, dict):
                        add(replacement.get("localPath", replacement.get("local_path", "")))
        # Stable order makes the JSON and tests deterministic.
        seen = set()
        for value in result:
            normalized = _clean_path(value)
            marker = os.path.normcase(normalized)
            if normalized and marker not in seen:
                seen.add(marker)
                yield normalized

    @staticmethod
    def _replace_asset_paths(payload, mapping):
        """Replace source paths in the canonical payload with local snapshots."""
        def replace(value):
            if isinstance(value, str):
                normalized = _clean_path(value)
                return mapping.get(normalized, value) if normalized else value
            return value
        if payload.get("html_path"):
            payload["html_path"] = replace(payload["html_path"])
        payload["html_paths"] = [replace(value) for value in payload.get("html_paths", [])]
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        for key in ("manual_images", "carousel_images", "attachment_paths"):
            media[key] = [replace(value) for value in media.get(key, []) or []]
        if media.get("thumbnail_path"):
            media["thumbnail_path"] = replace(media["thumbnail_path"])
        plan = media.get("gallery_plan")
        if isinstance(plan, list):
            for item in plan:
                if isinstance(item, dict) and item.get("kind") == "file":
                    item["value"] = replace(item.get("value", ""))
        for item in media.get("image_replacements", []) or []:
            if isinstance(item, dict) and item.get("local_path"):
                item["local_path"] = replace(item.get("local_path", ""))
        for item in payload.get("batch_items", []) or []:
            if isinstance(item, dict) and item.get("html_path"):
                item["html_path"] = replace(item["html_path"])
            if not isinstance(item, dict):
                continue
            settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
            for key in ("manualImages", "carouselImages", "attachmentPaths",
                        "manual_images", "carousel_images", "attachment_paths"):
                if key in settings:
                    settings[key] = [replace(value) for value in settings.get(key, []) or []]
            for key in ("thumbPath", "thumbnail_path"):
                if settings.get(key):
                    settings[key] = replace(settings[key])
            plan = settings.get("galleryPlan", settings.get("gallery_plan"))
            if isinstance(plan, list):
                for entry in plan:
                    if isinstance(entry, dict) and entry.get("kind") == "file":
                        entry["value"] = replace(entry.get("value", ""))
            for replacement in settings.get("imageReplacements",
                                            settings.get("image_replacements", [])) or []:
                if isinstance(replacement, dict):
                    for key in ("localPath", "local_path"):
                        if replacement.get(key):
                            replacement[key] = replace(replacement[key])

    def _snapshot_assets(self, site_key, workflow, payload):
        """Copy selected local assets into a content-addressed draft folder.

        The copy is best-effort per file: an unavailable source remains in the
        draft as its original path and is listed in ``asset_warnings``.  This
        mirrors browser draft behavior without silently claiming a missing
        file was backed up.
        """
        site_dir = self.root / _clean_token(site_key, "site_key")
        asset_dir = site_dir / "assets" / _clean_workflow(workflow)
        root_resolved = self._root_resolved
        asset_dir_resolved = asset_dir.resolve(strict=False)
        if root_resolved != asset_dir_resolved and root_resolved not in asset_dir_resolved.parents:
            raise DraftStoreError("草稿素材路径越界")
        mapping = {}
        source_mimes = dict(payload.get("asset_mimes") or {})
        selected_sources = set(self._asset_paths(payload))
        warnings = []
        total = 0
        created = []
        limit_warned = False
        for source in self._asset_paths(payload):
            if len(mapping) >= MAX_ASSET_FILES:
                if not limit_warned:
                    warnings.append(f"草稿素材快照达到文件数上限（{MAX_ASSET_FILES}），后续素材保留原路径")
                    limit_warned = True
                break
            try:
                source_resolved = Path(source).resolve(strict=True)
                if asset_dir_resolved == source_resolved or asset_dir_resolved in source_resolved.parents:
                    mapping[source] = source
                    continue
                if not source_resolved.is_file():
                    warnings.append(f"素材不存在，保留原路径：{source}")
                    continue
                size = source_resolved.stat().st_size
                if size > MAX_ASSET_FILE_BYTES:
                    warnings.append(f"素材超过 {MAX_ASSET_FILE_BYTES // (1024 * 1024)} MiB，未快照：{source}")
                    continue
                if total + size > MAX_ASSET_TOTAL_BYTES:
                    warnings.append("草稿素材快照达到总大小上限，后续素材保留原路径")
                    break
                digest = hashlib.sha256()
                with source_resolved.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                suffix = re.sub(r"[^A-Za-z0-9._-]", "_", source_resolved.suffix.lower())[:12]
                target = asset_dir / f"{digest.hexdigest()}{suffix}"
                if not target.exists():
                    asset_dir.mkdir(parents=True, exist_ok=True)
                    temp_name = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="wb", prefix=".asset-",
                                                         suffix=".tmp", dir=str(asset_dir),
                                                         delete=False) as output:
                            temp_name = output.name
                            with source_resolved.open("rb") as handle:
                                shutil.copyfileobj(handle, output, length=1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                        try:
                            os.chmod(temp_name, 0o600)
                        except OSError:
                            pass
                        os.replace(temp_name, target)
                        created.append(target)
                    finally:
                        if temp_name:
                            try:
                                os.unlink(temp_name)
                            except OSError:
                                pass
                mapping[source] = str(target)
                total += size
            except (OSError, ValueError) as exc:
                warnings.append(f"素材快照失败，保留原路径：{source}（{exc}）")
        self._replace_asset_paths(payload, mapping)
        payload["asset_map"] = mapping
        # Move MIME hints alongside content-addressed snapshots.  Hints for
        # paths that are not part of this draft are discarded, keeping the
        # persisted metadata bounded and tied to an actual browser File.
        asset_mimes = {}
        for source, mime in source_mimes.items():
            source = _clean_path(source)
            if not source or source not in selected_sources:
                continue
            target = mapping.get(source, source)
            asset_mimes[target] = mime
            remember_declared_mime(target, mime)
        payload["asset_mimes"] = asset_mimes
        payload["asset_warnings"] = warnings
        return created

    def _restore_validated_assets(self, payload, site_key, workflow):
        """Keep recovered snapshot paths inside this draft's asset directory.

        Draft JSON is local state, not a trusted command channel.  A user can
        move or edit the file by hand, and older versions may contain stale
        mappings.  Never let a loaded mapping make the UI read an arbitrary
        absolute path (or another site's snapshot); fall back to the original
        source path and retain an explicit warning instead.
        """
        mapping = payload.get("asset_map") if isinstance(payload, dict) else {}
        if not isinstance(mapping, dict):
            return payload
        asset_dir = (self.root / _clean_token(site_key, "site_key") /
                     "assets" / _clean_workflow(workflow))
        asset_root = asset_dir.resolve(strict=False)
        valid = {}
        fallback = {}
        warnings = list(payload.get("asset_warnings") or [])
        if (asset_root != self._root_resolved and
                self._root_resolved not in asset_root.parents):
            # A manually-created symlink in the site/workflow tree must not
            # make the recovery directory escape the DraftStore root.
            fallback.update(mapping)
            mapping = {}
            warnings.append("草稿素材目录不安全，已忽略全部素材快照")
        for source, target in mapping.items():
            source_path = _clean_path(source)
            target_path = _clean_path(target)
            if not source_path or not target_path:
                continue
            try:
                resolved = Path(target_path).resolve(strict=True)
                allowed = (resolved.is_file() and not resolved.is_symlink() and
                           (resolved == asset_root or asset_root in resolved.parents))
            except (OSError, ValueError):
                resolved = None
                allowed = False
            if allowed:
                valid[source_path] = str(resolved)
                continue
            fallback[target_path] = source_path
            warnings.append(f"草稿素材快照不在当前流程目录，已回退原路径：{target_path}")
        # A payload may already contain the snapshot path while its map was
        # tampered with or the file was deleted.  Replace only known path
        # fields; the warning and cleaned map remain visible to callers.
        if fallback:
            self._replace_asset_paths(payload, fallback)
        payload["asset_map"] = valid
        # Restore browser-declared MIME hints after validating the snapshot
        # paths.  Valid mappings point source->snapshot; invalid/deleted
        # snapshots use the fallback target->source map populated above.
        raw_mimes = payload.get("asset_mimes") if isinstance(payload, dict) else {}
        restored_mimes = {}
        allowed_paths = set(self._asset_paths(payload))
        if isinstance(raw_mimes, dict):
            for raw_path, raw_mime in raw_mimes.items():
                path = _clean_path(raw_path)
                mime = str(raw_mime or "").split(";", 1)[0].strip().lower()
                if not path or not re.fullmatch(
                        r"[a-z0-9][a-z0-9!#$&^_.+\-]*/"
                        r"[a-z0-9][a-z0-9!#$&^_.+\-]*$", mime, re.I):
                    continue
                candidate = valid.get(path, fallback.get(path, path))
                if candidate not in allowed_paths:
                    continue
                restored_mimes[candidate] = mime
                remember_declared_mime(candidate, mime)
        payload["asset_mimes"] = restored_mimes
        payload["asset_warnings"] = warnings[:100]
        return payload

    def _validate_envelope(self, data, expected_site=None, expected_workflow=None):
        if not isinstance(data, dict) or data.get("format") != DRAFT_FORMAT:
            raise DraftStoreError("草稿文件格式无效")
        try:
            version = int(data.get("schema_version", 0))
        except (TypeError, ValueError):
            raise DraftStoreError("草稿版本无效")
        if version != DRAFT_SCHEMA_VERSION:
            if version > DRAFT_SCHEMA_VERSION:
                raise DraftStoreError("草稿由更新版本创建，请先升级软件")
            raise DraftStoreError("草稿版本过旧，无法恢复")
        site_key = _clean_token(data.get("site_key"), "site_key")
        workflow = _clean_workflow(data.get("workflow"))
        if expected_site and site_key != expected_site:
            raise DraftStoreError("草稿站点不匹配")
        if expected_workflow and workflow != expected_workflow:
            raise DraftStoreError("草稿流程不匹配")
        saved_at = _clean_text(data.get("saved_at", ""), 64, "保存时间")
        draft = sanitize_draft(workflow, data.get("draft") or {})
        # Validate the snapshot directory after sanitization so a malformed
        # or manually edited draft cannot escape the current site/workflow.
        draft = self._restore_validated_assets(draft, site_key, workflow)
        return {
            "format": DRAFT_FORMAT,
            "schema_version": version,
            "site_key": site_key,
            "workflow": workflow,
            "saved_at": saved_at,
            "draft": draft,
        }

    def save(self, site_key, workflow, draft, source_fields=None):
        site_key = _clean_token(site_key, "site_key")
        workflow = _clean_workflow(workflow)
        payload = sanitize_draft(workflow, draft, source_fields=source_fields)
        self._snapshot_assets(site_key, workflow, payload)
        envelope = self._envelope(site_key, workflow, payload)
        raw = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
        if len(raw) > MAX_JSON_BYTES:
            raise DraftStoreError("草稿过大，无法保存")

        with _WRITE_LOCK:
            path = self._path(site_key, workflow, create=True)
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(
                        mode="wb", prefix=f".{workflow}.", suffix=".tmp",
                        dir=str(path.parent), delete=False) as handle:
                    temp_name = handle.name
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temp_name, 0o600)
                except OSError:
                    pass
                os.replace(temp_name, path)
                temp_name = None
            finally:
                if temp_name:
                    try:
                        os.unlink(temp_name)
                    except OSError:
                        pass
        return envelope

    def load(self, site_key, workflow):
        site_key = _clean_token(site_key, "site_key")
        workflow = _clean_workflow(workflow)
        with _WRITE_LOCK:
            path = self._path(site_key, workflow)
            if not path.is_file() or path.is_symlink():
                return None
            try:
                if path.stat().st_size > MAX_JSON_BYTES:
                    raise DraftStoreError("草稿文件超过允许大小")
                data = json.loads(path.read_text(encoding="utf-8"))
            except DraftStoreError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise DraftStoreError(f"草稿文件损坏: {exc}")
        return self._validate_envelope(data, site_key, workflow)

    def list(self, site_key=None, workflow=None):
        wanted_site = _clean_token(site_key, "site_key") if site_key else None
        wanted_workflow = _clean_workflow(workflow) if workflow else None
        sites = [self.root / wanted_site] if wanted_site else sorted(self.root.iterdir())
        result = []
        for site_dir in sites:
            if not site_dir.is_dir() or site_dir.is_symlink() or not _TOKEN_RE.fullmatch(site_dir.name):
                continue
            workflows = [wanted_workflow] if wanted_workflow else sorted(SUPPORTED_WORKFLOWS)
            for name in workflows:
                try:
                    envelope = self.load(site_dir.name, name)
                except DraftStoreError:
                    continue
                if not envelope:
                    continue
                draft = envelope["draft"]
                media = draft.get("media") or {}
                result.append({
                    "schema_version": envelope["schema_version"],
                    "site_key": envelope["site_key"],
                    "workflow": envelope["workflow"],
                    "saved_at": envelope["saved_at"],
                    "category": draft.get("category", ""),
                    "article_id": draft.get("article_id", ""),
                    "html_path": draft.get("html_path", ""),
                    "html_count": len(draft.get("html_paths") or []),
                    "override_count": len(draft.get("overrides") or {}),
                    "media_count": (len(media.get("manual_images") or []) +
                                    len(media.get("carousel_images") or []) +
                                    len(media.get("attachment_paths") or []) +
                                    len(media.get("image_replacements") or []) +
                                    bool(media.get("thumbnail_path"))),
                    "batch_count": len(draft.get("batch_items") or []),
                })
        result.sort(key=lambda item: item.get("saved_at", ""), reverse=True)
        return result

    def delete(self, site_key, workflow):
        site_key = _clean_token(site_key, "site_key")
        workflow = _clean_workflow(workflow)
        with _WRITE_LOCK:
            path = self._path(site_key, workflow)
            if not path.is_file() or path.is_symlink():
                return False
            path.unlink()
            # Remove only this workflow's exact snapshot directory.  Never
            # follow symlinks or recursively target the site/root directory.
            asset_dir = path.parent / "assets" / workflow
            if asset_dir.is_dir() and not asset_dir.is_symlink():
                try:
                    shutil.rmtree(asset_dir)
                except OSError:
                    pass
            try:
                path.parent.rmdir()  # only removes an empty per-site directory
            except OSError:
                pass
            return True
