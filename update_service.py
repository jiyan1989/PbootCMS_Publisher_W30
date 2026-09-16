# -*- coding: utf-8 -*-
"""Read-only release checks and local pre-update backups.

This module deliberately has no download/install operation.  A release check
performs one bounded HTTPS GET for a small JSON manifest.  Creating a backup is
a separate, explicit local action initiated by the user.
"""

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from app_meta import (
    APP_CHANNEL,
    APP_DISPLAY_VERSION,
    APP_NAME,
    APP_VERSION,
    BUILD_DATE,
    CHANGELOG,
    DEFAULT_UPDATE_MANIFEST_URL,
    UPDATE_MANIFEST_ENV,
    UPDATE_SIGNING_KEY_ID,
    UPDATE_SIGNING_RSA_EXPONENT,
    UPDATE_SIGNING_RSA_MODULUS_HEX,
    WINDOW_TITLE,
)


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_MAX_BYTES = 256 * 1024
UPDATE_CONNECT_TIMEOUT = 3.05
UPDATE_READ_TIMEOUT = 8.0
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(
    r"^[vV]?(\d+(?:\.\d+){1,3})(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_RSA_SHA256_DIGEST_INFO = bytes.fromhex(
    "3031300d060960864801650304020105000420")


class UpdateManifestError(ValueError):
    """The remote document is reachable but is not a safe update manifest."""


class BackupError(RuntimeError):
    """A complete, verified local backup could not be created."""


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat() \
        .replace("+00:00", "Z")


def _json_without_duplicates(raw):
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise UpdateManifestError(f"manifest 包含重复字段: {key}")
            result[key] = value
        return result

    try:
        def reject_constant(value):
            raise UpdateManifestError(f"manifest 包含非标准数值: {value}")

        return json.loads(raw, object_pairs_hook=pairs_hook,
                          parse_constant=reject_constant)
    except UpdateManifestError:
        raise
    except (TypeError, ValueError) as exc:
        raise UpdateManifestError(f"manifest 不是有效 JSON: {exc}") from exc


def _version_key(value):
    """Return a deterministic SemVer-like comparison key for app releases."""
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise UpdateManifestError(f"版本号格式无效: {value}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    numbers += (0,) * (4 - len(numbers))
    prerelease = match.group(2)
    if prerelease is None:
        pre_key = (1,)
    else:
        tokens = []
        for token in prerelease.split("."):
            # SemVer: numeric identifiers sort before non-numeric identifiers.
            tokens.append((0, int(token)) if token.isdigit()
                          else (1, token.lower()))
        pre_key = (0, tuple(tokens))
    return numbers, pre_key


def _safe_https_url(value, label):
    text = str(value or "").strip()
    parsed = urlparse(text)
    if (parsed.scheme.lower() != "https" or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.fragment):
        raise UpdateManifestError(f"{label}必须是不含账号、密码和片段的 HTTPS 地址")
    return text


def _canonical_manifest(document):
    signed = dict(document)
    signed.pop("signature", None)
    return json.dumps(
        signed, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def _verify_rsa_sha256(signature_b64, payload, modulus_hex, exponent=65537):
    """Verify an RSASSA-PKCS1-v1_5 SHA-256 signature using a raw public key.

    Keeping verification in the standard library avoids adding a large crypto
    runtime to the desktop executable.  Only modern (>=2048-bit) RSA keys and
    the exact ``rsa-sha256`` scheme are accepted.
    """
    try:
        modulus = int(str(modulus_hex or "").strip(), 16)
        exponent = int(exponent)
        signature = base64.b64decode(str(signature_b64 or ""), validate=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise UpdateManifestError("manifest 签名或 RSA 公钥格式无效") from exc
    if modulus.bit_length() < 2048 or exponent < 3 or not exponent % 2:
        raise UpdateManifestError("manifest RSA 公钥强度不足或指数无效")
    width = (modulus.bit_length() + 7) // 8
    if len(signature) != width or int.from_bytes(signature, "big") >= modulus:
        return False
    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus) \
        .to_bytes(width, "big")
    digest_info = _RSA_SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    padding_size = width - len(digest_info) - 3
    if padding_size < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def _signature_metadata(document, public_key):
    signature = document.get("signature")
    if not signature:
        return {
            "present": False, "verified": False, "status": "missing",
            "algorithm": "", "key_id": "",
            "message": "更新清单未携带数字签名",
        }
    if not isinstance(signature, dict):
        raise UpdateManifestError("manifest signature 必须是对象")
    algorithm = str(signature.get("algorithm", "") or "").strip().lower()
    key_id = str(signature.get("key_id", "") or "").strip()
    value = str(signature.get("value", "") or "").strip()
    if algorithm != "rsa-sha256" or not value:
        raise UpdateManifestError("不支持的 manifest 签名格式")
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise UpdateManifestError("manifest 签名不是有效 Base64") from exc
    if not public_key or not public_key.get("modulus_hex"):
        return {
            "present": True, "verified": False, "status": "unverified",
            "algorithm": algorithm, "key_id": key_id,
            "message": "当前版本未配置发布公钥，已保留签名元数据但无法验证",
        }
    expected_key_id = str(public_key.get("key_id", "") or "").strip()
    if expected_key_id and key_id != expected_key_id:
        raise UpdateManifestError("manifest 签名 key_id 与发布公钥不匹配")
    verified = _verify_rsa_sha256(
        value, _canonical_manifest(document), public_key.get("modulus_hex"),
        public_key.get("exponent", 65537))
    if not verified:
        raise UpdateManifestError("manifest 数字签名验证失败")
    return {
        "present": True, "verified": True, "status": "verified",
        "algorithm": algorithm, "key_id": key_id,
        "message": "更新清单数字签名已验证",
    }


def _artifact_signature_metadata(download):
    signature = download.get("signature")
    if not signature:
        return {
            "present": False, "status": "missing", "algorithm": "",
            "key_id": "", "message": "安装包未声明独立签名元数据",
        }
    if not isinstance(signature, dict):
        raise UpdateManifestError("download.signature 必须是对象")
    algorithm = str(signature.get("algorithm", "") or "").strip().lower()
    key_id = str(signature.get("key_id", "") or "").strip()
    if not algorithm:
        raise UpdateManifestError("download.signature 缺少 algorithm")
    # No artifact bytes are downloaded here, so claiming verification would be
    # misleading.  The declared metadata is returned for a future manual
    # downloader/verifier to consume.
    return {
        "present": True, "status": "declared", "algorithm": algorithm,
        "key_id": key_id, "message": "已读取安装包签名元数据，尚未下载验证",
    }


def _parse_manifest(document, current_version, channel, public_key=None):
    if not isinstance(document, dict):
        raise UpdateManifestError("manifest 根节点必须是对象")
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise UpdateManifestError("manifest schema_version 不受支持")
    latest_version = str(document.get("version", "") or "").strip()
    latest_key = _version_key(latest_version)
    current_key = _version_key(current_version)
    manifest_channel = str(document.get("channel", "") or "").strip()
    if manifest_channel != channel:
        raise UpdateManifestError(
            f"manifest 渠道不匹配（期望 {channel}，实际 {manifest_channel or '空'}）")

    download = document.get("download")
    if not isinstance(download, dict):
        raise UpdateManifestError("manifest 缺少 download 对象")
    download_url = _safe_https_url(download.get("url"), "安装包地址")
    sha256 = str(download.get("sha256", "") or "").strip().lower()
    if not _SHA256_RE.fullmatch(sha256):
        raise UpdateManifestError("安装包 sha256 必须是 64 位十六进制值")
    raw_size = download.get("size")
    if raw_size is None:
        size = None
    elif isinstance(raw_size, bool):
        raise UpdateManifestError("安装包 size 无效")
    else:
        try:
            size = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise UpdateManifestError("安装包 size 无效") from exc
        if size < 0:
            raise UpdateManifestError("安装包 size 无效")

    notes = document.get("notes", [])
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
        raise UpdateManifestError("manifest notes 必须是文本列表")
    notes = [item.strip() for item in notes if item.strip()][:50]
    manifest_signature = _signature_metadata(document, public_key or {})
    artifact_signature = _artifact_signature_metadata(download)
    return {
        "latest_version": latest_version,
        "update_available": latest_key > current_key,
        "release": {
            "version": latest_version,
            "channel": manifest_channel,
            "published_at": str(document.get("published_at", "") or ""),
            "notes": notes,
            "download": {
                "url": download_url,
                "size": size,
                "integrity": {
                    "sha256": sha256,
                    "sha256_status": "declared",
                    "signature": artifact_signature,
                },
            },
            "manifest_signature": manifest_signature,
            "trusted_manifest": bool(manifest_signature.get("verified")),
        },
    }


def _read_bounded_response(response, limit=MANIFEST_MAX_BYTES):
    raw_length = response.headers.get("Content-Length", "") if response.headers else ""
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise UpdateManifestError("manifest Content-Length 无效")
        if content_length > limit:
            raise UpdateManifestError("manifest 超过允许的大小")
    chunks = []
    total = 0
    iterator = response.iter_content(chunk_size=16384)
    for chunk in iterator:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise UpdateManifestError("manifest 超过允许的大小")
        chunks.append(chunk)
    return b"".join(chunks)


class UpdateService:
    """Stateful in-memory facade for update status and local backups."""

    def __init__(self, manifest_url=None, data_dir=None, http=None,
                 current_version=APP_VERSION, channel=APP_CHANNEL,
                 public_key=None):
        configured = manifest_url
        if configured is None:
            build_update = _read_build_metadata().get("update", {})
            embedded_url = (build_update.get("manifest_url", "")
                            if isinstance(build_update, dict) else "")
            # A runtime environment override is useful for managed/offline
            # deployments; release builds can embed the public URL in their
            # non-secret build metadata.  Neither source mode nor app startup
            # performs a request merely because a URL exists.
            configured = os.environ.get(UPDATE_MANIFEST_ENV, "").strip() or \
                str(embedded_url or "").strip() or DEFAULT_UPDATE_MANIFEST_URL
        self.manifest_url = str(configured or "").strip()
        self.data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self.http = http or requests.Session()
        self.current_version = current_version
        self.channel = channel
        self.public_key = public_key if public_key is not None else {
            "key_id": UPDATE_SIGNING_KEY_ID,
            "modulus_hex": UPDATE_SIGNING_RSA_MODULUS_HEX,
            "exponent": UPDATE_SIGNING_RSA_EXPONENT,
        }
        self._last_check = None
        self._lock = threading.RLock()
        self._signing = None

    def update_summary(self):
        with self._lock:
            last = dict(self._last_check or {})
        return {
            "configured": bool(self.manifest_url),
            "manifest_host": _display_host(self.manifest_url),
            "last_status": last.get("status", "never_checked"),
            "last_checked_at": last.get("checked_at", ""),
            "latest_version": last.get("latest_version", ""),
            "update_available": last.get("update_available"),
            "automatic_download": False,
            "automatic_install": False,
        }

    def check(self):
        checked_at = _utc_now()
        base = {
            "configured": bool(self.manifest_url),
            "checked_at": checked_at,
            "current_version": self.current_version,
            "latest_version": "",
            "update_available": None,
            "release": None,
            "automatic_download": False,
            "automatic_install": False,
        }
        if not self.manifest_url:
            result = {
                **base, "status": "not_configured",
                "message": "当前版本未配置更新清单地址",
            }
            return self._remember(result)
        try:
            url = _safe_https_url(self.manifest_url, "manifest 地址")
            response = self.http.get(
                url,
                timeout=(UPDATE_CONNECT_TIMEOUT, UPDATE_READ_TIMEOUT),
                stream=True,
                allow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"PbootCMS-Publisher/{self.current_version} ({self.channel})",
                },
            )
            try:
                if 300 <= int(response.status_code) < 400:
                    raise UpdateManifestError("manifest 地址不允许重定向")
                if int(response.status_code) != 200:
                    raise UpdateManifestError(
                        f"manifest 服务返回 HTTP {response.status_code}")
                raw = _read_bounded_response(response)
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                text = raw.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                raise UpdateManifestError("manifest 必须使用 UTF-8 编码") from exc
            parsed = _parse_manifest(
                _json_without_duplicates(text), self.current_version,
                self.channel, self.public_key)
            result = {
                **base, **parsed, "status": "ok",
                "message": ("发现新版本" if parsed["update_available"]
                            else "当前已是最新版本"),
            }
        except (requests.Timeout, requests.ConnectionError) as exc:
            result = {
                **base, "status": "offline",
                "message": f"暂时无法连接更新服务: {_safe_error(exc)}",
            }
        except requests.RequestException as exc:
            result = {
                **base, "status": "offline",
                "message": f"更新检查请求失败: {_safe_error(exc)}",
            }
        except UpdateManifestError as exc:
            result = {
                **base, "status": "invalid",
                "message": str(exc),
            }
        except OSError as exc:
            result = {
                **base, "status": "offline",
                "message": f"更新检查暂不可用: {_safe_error(exc)}",
            }
        return self._remember(result)

    def _remember(self, result):
        with self._lock:
            self._last_check = dict(result)
        return result

    def signing_info(self, refresh=False):
        with self._lock:
            if self._signing is None or refresh:
                self._signing = get_runtime_signing_info()
            return dict(self._signing)

    def about(self):
        return {
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "display_version": APP_DISPLAY_VERSION,
                "channel": APP_CHANNEL,
                "build_date": BUILD_DATE,
                "title": WINDOW_TITLE,
            },
            "signing": self.signing_info(),
            "update": self.update_summary(),
            "update_policy": {
                "check_is_manual": True,
                "downloads_automatically": False,
                "installs_automatically": False,
                "backup_required_before_install": True,
            },
            "changelog": [
                {**entry, "items": list(entry.get("items", ()))}
                for entry in CHANGELOG
            ],
        }

    def create_backup(self):
        """Create an atomic ZIP with config and a consistent SQLite snapshot."""
        return create_pre_update_backup(self.data_dir)


def _display_host(url):
    try:
        return urlparse(url).hostname or ""
    except (TypeError, ValueError):
        return ""


def _safe_error(exc):
    text = str(exc or exc.__class__.__name__).replace("\r", " ").replace("\n", " ")
    return text[:180]


def _default_data_dir():
    override = os.environ.get("PBOOT_PUBLISHER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sqlite(source, destination):
    # URI read-only mode prevents a typo from creating an empty source DB.
    source_uri = source.resolve().as_uri() + "?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True, timeout=5)
    destination_conn = sqlite3.connect(str(destination), timeout=5)
    try:
        source_conn.backup(destination_conn, pages=256, sleep=0.02)
        destination_conn.commit()
        check = destination_conn.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise BackupError("产品库备份完整性检查失败")
    finally:
        destination_conn.close()
        source_conn.close()


def create_pre_update_backup(data_dir):
    data_dir = Path(data_dir).expanduser().resolve()
    if not data_dir.is_dir():
        raise BackupError(f"数据目录不存在: {data_dir}")
    backup_dir = data_dir / "update_backups"
    backup_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"{os.getpid()}-{time.time_ns() % 1000000:06d}"
    backup_id = f"pre-update-{APP_VERSION}-{stamp}-{suffix}"
    target = backup_dir / f"{backup_id}.zip"
    temp_target = target.with_suffix(".zip.tmp")
    sources = {
        "publisher_config.json": data_dir / "publisher_config.json",
        "products_cache.db": data_dir / "products_cache.db",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="backup-", dir=str(backup_dir)) as work:
            work_dir = Path(work)
            files = []
            config_source = sources["publisher_config.json"]
            if config_source.is_file():
                config_snapshot = work_dir / config_source.name
                shutil.copy2(config_source, config_snapshot)
                # Detect a truncated/partially-written config before calling it safe.
                with open(config_snapshot, "r", encoding="utf-8") as handle:
                    if not isinstance(json.load(handle), dict):
                        raise BackupError("配置备份不是有效 JSON 对象")
                files.append(config_snapshot)
            db_source = sources["products_cache.db"]
            if db_source.is_file():
                db_snapshot = work_dir / db_source.name
                _snapshot_sqlite(db_source, db_snapshot)
                files.append(db_snapshot)
            if not files:
                raise BackupError("当前没有可备份的配置或产品库")
            manifest = {
                "schema_version": 1,
                "backup_id": backup_id,
                "created_at": _utc_now(),
                "app_version": APP_VERSION,
                "files": [
                    {
                        "name": item.name,
                        "size": item.stat().st_size,
                        "sha256": _sha256_file(item),
                    }
                    for item in files
                ],
            }
            with zipfile.ZipFile(temp_target, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "backup_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
                for item in files:
                    archive.write(item, item.name)
            with zipfile.ZipFile(temp_target, "r") as archive:
                bad_name = archive.testzip()
                if bad_name:
                    raise BackupError(f"备份 ZIP 校验失败: {bad_name}")
            # Windows does not permit FlushFileBuffers through a read-only
            # descriptor; r+b keeps this durability check cross-platform.
            with open(temp_target, "r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_target, target)
        return {
            "created": True,
            "backup_id": backup_id,
            "path": str(target),
            "size": target.stat().st_size,
            "sha256": _sha256_file(target),
            "files": manifest["files"],
            "message": f"已备份 {len(manifest['files'])} 个数据文件",
        }
    except (BackupError, json.JSONDecodeError, OSError, sqlite3.Error) as exc:
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"创建更新前备份失败: {_safe_error(exc)}") from exc


def _read_build_metadata():
    if not getattr(sys, "frozen", False):
        return {}
    root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    path = root / "build_metadata.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _win_verify_authenticode(path):
    """Offline Authenticode verification via WinVerifyTrust.

    ``WTD_CACHE_ONLY_URL_RETRIEVAL`` prevents certificate verification from
    silently contacting revocation servers while merely opening About.
    """
    if sys.platform != "win32":
        return "unsupported"
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
        ]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.POINTER(GUID)),
        ]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", wintypes.LPVOID),
            ("pSIPClientData", wintypes.LPVOID),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", wintypes.LPVOID),
        ]

    action = GUID(
        0x00AAC56B, 0xCD44, 0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = WINTRUST_FILE_INFO(
        ctypes.sizeof(WINTRUST_FILE_INFO), str(path), None, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = 2                 # WTD_UI_NONE
    data.fdwRevocationChecks = 0        # WTD_REVOKE_NONE
    data.dwUnionChoice = 1              # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 1              # WTD_STATEACTION_VERIFY
    data.dwProvFlags = 0x1000           # WTD_CACHE_ONLY_URL_RETRIEVAL
    wintrust = ctypes.windll.wintrust
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID),
                                        ctypes.POINTER(WINTRUST_DATA)]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    try:
        status = int(wintrust.WinVerifyTrust(None, ctypes.byref(action),
                                             ctypes.byref(data)))
    except (AttributeError, OSError):
        return "unknown"
    finally:
        if data.hWVTStateData:
            data.dwStateAction = 2      # WTD_STATEACTION_CLOSE
            try:
                wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
            except Exception:
                pass
    unsigned_codes = {0x800B0100, 0x800B0003, 0x800B0001}
    unsigned_signed = status & 0xFFFFFFFF
    if status == 0:
        return "valid"
    if unsigned_signed in unsigned_codes:
        return "unsigned"
    return "invalid"


def get_runtime_signing_info():
    metadata = _read_build_metadata()
    declared = metadata.get("signing", {}) if isinstance(metadata, dict) else {}
    method = str(declared.get("method", "") or "")
    if not getattr(sys, "frozen", False):
        return {
            "signed": False,
            "status": "development_unsigned",
            "verification": "not_applicable",
            "method": "",
            "message": "当前为源码运行模式，未应用 Windows 代码签名",
        }
    verification = _win_verify_authenticode(sys.executable)
    if verification == "valid":
        status, message, signed = (
            "signed", "Windows Authenticode 签名验证通过", True)
    elif verification == "unsigned":
        status, message, signed = (
            "unsigned", "当前 EXE 未配置数字签名，Windows 可能显示安全提示", False)
    elif verification == "invalid":
        status, message, signed = (
            "invalid", "当前 EXE 的数字签名无效或不可信", False)
    else:
        status, message, signed = (
            "unknown", "当前环境无法完成 Authenticode 离线验证", False)
    return {
        "signed": signed,
        "status": status,
        "verification": verification,
        "method": method,
        "message": message,
    }


__all__ = [
    "BackupError", "UpdateManifestError", "UpdateService",
    "create_pre_update_backup", "get_runtime_signing_info",
]
