# -*- coding: utf-8 -*-
"""PbootCMS 发布工具 — 配置管理"""

import json
import os
import threading
import time
import shutil
from pathlib import Path
import sys

from logger import debug_log
from secure_store import protect_text, unprotect_text


def _get_base_dir():
    override = os.environ.get("PBOOT_PUBLISHER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# 全局配置路径（与主程序共享同一个配置文件）
CONFIG_FILE = _get_base_dir() / "publisher_config.json"
CONFIG_SCHEMA_VERSION = 3
# 后台地址下拉历史保留条数
URL_HISTORY_MAX = 30
# 多标签并行启动时会多线程写配置，写入需串行以免丢改动
_WRITE_LOCK = threading.RLock()


def _defaults():
    return {
        "config_version": CONFIG_SCHEMA_VERSION,
        "urls": [], "last_url": "", "last_user": "", "last_pass": "",
        "mappings": {}, "tabs": {}, "credentials_per_site": {}, "network_per_site": {}, "workspaces": {},
        "file_dialog_dirs": {},
    }


class ConfigManager:
    """配置管理：URL 历史、密码、字段映射"""

    def __init__(self):
        self.data = _defaults()
        self.load()

    def load(self):
        try:
            if CONFIG_FILE.exists():
                raw = None
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("配置根节点必须是对象")
                old_version = int(raw.get("config_version", 0) or 0)
                if old_version < CONFIG_SCHEMA_VERSION:
                    backup = CONFIG_FILE.with_name(
                        f"{CONFIG_FILE.name}.bak-v{old_version}-{time.strftime('%Y%m%d%H%M%S')}")
                    shutil.copy2(CONFIG_FILE, backup)
                    debug_log(f"[config] 迁移前备份: {backup.name}")
                self.data.update(raw)
                for key, expected in (("urls", list), ("mappings", dict), ("tabs", dict),
                                      ("credentials_per_site", dict), ("network_per_site", dict),
                                      ("workspaces", dict),
                                      ("file_dialog_dirs", dict)):
                    if not isinstance(self.data.get(key), expected):
                        self.data[key] = expected()
                self.data["urls"] = list(dict.fromkeys(
                    str(url).strip().rstrip("/") for url in self.data["urls"]
                    if str(url).strip()))[:URL_HISTORY_MAX]
                self.data["config_version"] = CONFIG_SCHEMA_VERSION
                if old_version < CONFIG_SCHEMA_VERSION:
                    self.save()
        except Exception as e:
            debug_log(f"[config] load error: {e}")
            if CONFIG_FILE.exists():
                try:
                    os.replace(CONFIG_FILE, CONFIG_FILE.with_name(
                        f"{CONFIG_FILE.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"))
                except OSError:
                    pass
            self.data = _defaults()

    def save(self):
        with _WRITE_LOCK:
            try:
                self.data["config_version"] = CONFIG_SCHEMA_VERSION
                tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, CONFIG_FILE)
            except Exception as e:
                debug_log(f"[config] save error: {e}")
                raise

    def locked(self):
        """供“修改多个配置项后一次保存”的调用方使用同一把可重入锁。"""
        return _WRITE_LOCK

    def add_url(self, url: str):
        """记一条后台地址历史（最近用过的排前面）。

        下拉历史保留 URL_HISTORY_MAX 条，多站维护时 10 条不够用。
        """
        with _WRITE_LOCK:
            url = url.rstrip("/")
            if url in self.data["urls"]:
                self.data["urls"].remove(url)
            self.data["urls"].insert(0, url)
            evicted = self.data["urls"][URL_HISTORY_MAX:]
            self.data["urls"] = self.data["urls"][:URL_HISTORY_MAX]
            credentials = self.data.setdefault("credentials_per_site", {})
            for old_url in evicted:
                credentials.pop(str(old_url).rstrip("/"), None)
            self.data["last_url"] = url
            self.save()

    # ── 按站点记住账密 ──
    def set_credentials(self, admin_url: str, user: str, password: str):
        """记住某站点的账号密码。

        密码经 DPAPI 加密后才入盘（绑当前 Windows 用户），配置文件里
        不会出现明文密码；拷到其他电脑/其他账户下也解不开。
        """
        admin_url = (admin_url or "").rstrip("/")
        if not admin_url or not user:
            return
        with _WRITE_LOCK:
            table = self.data.setdefault("credentials_per_site", {})
            table[admin_url] = {
                "user": user,
                "pass": protect_text(password or ""),
            }
            self.save()

    def get_credentials(self, admin_url: str) -> dict:
        """取某站点已记住的账密（密码已解密）。解不开则密码为空。"""
        admin_url = (admin_url or "").rstrip("/")
        info = (self.data.get("credentials_per_site", {}) or {}).get(admin_url, {})
        if not isinstance(info, dict):
            return {}
        pwd = ""
        try:
            pwd = unprotect_text(info.get("pass", "") or "")
        except Exception as exc:
            debug_log(f"[config] 密码解密失败（将要求重输）: {exc}")
        return {"user": str(info.get("user", "")), "pass": pwd}

    def forget_credentials(self, admin_url: str):
        """忘记某站点的账密。"""
        admin_url = (admin_url or "").rstrip("/")
        with _WRITE_LOCK:
            table = self.data.setdefault("credentials_per_site", {})
            if table.pop(admin_url, None) is not None:
                self.save()

    def forget_site(self, admin_url: str):
        """同时删除历史地址和对应账密，避免地址淘汰后留下孤立密码。"""
        admin_url = (admin_url or "").rstrip("/")
        if not admin_url:
            return
        with _WRITE_LOCK:
            self.data["urls"] = [
                u for u in self.data.get("urls", []) if u.rstrip("/") != admin_url]
            self.data.setdefault("credentials_per_site", {}).pop(admin_url, None)
            if str(self.data.get("last_url", "")).rstrip("/") == admin_url:
                self.data["last_url"] = (self.data["urls"][0]
                                         if self.data["urls"] else "")
            self.save()
