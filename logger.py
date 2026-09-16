# -*- coding: utf-8 -*-
"""PbootCMS 发布工具 — 统一日志模块"""

import os
import time
import re
import json
import platform
import sys
import zipfile
from pathlib import Path
from app_meta import APP_DISPLAY_VERSION, BUILD_DATE


def _get_base_dir():
    override = os.environ.get("PBOOT_PUBLISHER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    """exe 运行时取 exe 所在目录，开发时取本文件所在目录"""
    import sys
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


_LOG_FILE = _get_base_dir() / "debug.log"
_MAX_LOG_BYTES = 2 * 1024 * 1024


def redact_text(value):
    """Redact credentials/tokens while retaining routes useful for diagnosis.

    覆盖四类格式：key=value / URL 参数 / JSON及Python字典的引号键值对 /
    Bearer与Basic 凭据。注意：这是尽力而为的脱敏，无法保证覆盖所有自定义格式。
    """
    text = str(value or "")
    sensitive = r"password|passwd|pwd|pass|token|secret|cookie|authorization|formcheck|captcha|checkcode"
    # Bearer/Basic 凭据必须先于通用 key:value 规则：否则 "Authorization: Basic xxx"
    # 会被通用规则先把 "Basic" 替换成 ***，留下 base64 凭据本体
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", text)
    text = re.sub(r"(?i)(Basic\s+)[A-Za-z0-9+/=]{4,}", r"\1***", text)
    text = re.sub(
        rf"(?i)(\b(?:{sensitive})\b\s*[=:]\s*)([^\s&,;\]\}}]+)",
        r"\1***", text)
    text = re.sub(
        rf"(?i)([?&](?:{sensitive})=)[^&#\s]+", r"\1***", text)
    # JSON / Python 字典格式："password":"xxx" / 'password': 'xxx'
    text = re.sub(
        rf"(?i)([\"'](?:{sensitive})[\"']\s*:\s*[\"'])[^\"']*([\"'])",
        r"\1***\2", text)
    return text


def _rotate(path):
    try:
        if not path.exists() or path.stat().st_size < _MAX_LOG_BYTES:
            return
        for index in range(2, 0, -1):
            src = path.with_name(path.name + f".{index}")
            dst = path.with_name(path.name + f".{index + 1}")
            if src.exists():
                os.replace(src, dst)
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def debug_log(msg: str):
    """写入调试日志到文件（--noconsole 模式下也能查看）"""
    try:
        _rotate(_LOG_FILE)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {redact_text(msg)}\n")
    except Exception:
        pass


# ── 详细日志开关 ──
# 默认关闭。开启方式（二选一）：
#   1) 环境变量：PBOOTCMS_VERBOSE=1
#   2) 代码调用：from logger import set_verbose; set_verbose(True)
# 细粒度日志（逐行 HTML、逐链接、完整响应体等）只在开启时落盘，
# 避免大列表/大响应时产生海量磁盘写入，也避免把内容原文长期留在 debug.log。
_VERBOSE = os.environ.get("PBOOTCMS_VERBOSE", "0").lower() in ("1", "true", "yes", "on")


def set_verbose(on: bool):
    """运行时开关详细日志"""
    global _VERBOSE
    _VERBOSE = bool(on)


def is_verbose() -> bool:
    return _VERBOSE


def debug_log_v(msg: str):
    """详细日志：仅在 set_verbose(True) / 环境变量开启时写入"""
    if _VERBOSE:
        debug_log(msg)


def log_event(event, **fields):
    """Write a compact structured event with automatic redaction."""
    payload = {"event": event, **fields}
    debug_log("EVENT " + json.dumps(payload, ensure_ascii=False, default=str))


def export_diagnostics(output_path, config_data=None):
    """Create a redacted support ZIP; excludes cookies, DB and HTML bodies.
    脱敏为尽力而为（见 redact_text），导出前仍建议人工抽查。"""
    output_path = Path(output_path)
    safe_config = dict(config_data or {})
    safe_config.pop("last_pass", None)
    credentials = safe_config.get("credentials_per_site", {})
    if isinstance(credentials, dict):
        safe_config["credentials_per_site"] = {
            url: {"user": str(values.get("user", "")), "pass": "***"}
            for url, values in credentials.items() if isinstance(values, dict)}
    environment = {
        "version": APP_DISPLAY_VERSION, "build_date": BUILD_DATE,
        "python": sys.version, "platform": platform.platform(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("environment.json", json.dumps(environment, ensure_ascii=False, indent=2))
        archive.writestr("publisher_config.redacted.json",
                         json.dumps(safe_config, ensure_ascii=False, indent=2))
        for name in ("debug.log", "request_audit.log", "error.log", "crash.log"):
            path = _get_base_dir() / name
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="replace")
                archive.writestr(name, redact_text(content))
    return output_path


# ── 便捷别名 ──
info    = debug_log
warning = debug_log
error   = debug_log
