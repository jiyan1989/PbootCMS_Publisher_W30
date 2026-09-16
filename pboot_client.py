"""PbootCMS HTTP client and session isolation."""
import hashlib
import io
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
try:
    from PIL import Image
except ImportError:
    Image = None

from constants import TIMEOUT_NORMAL, TIMEOUT_UPLOAD, MCODE_ORDER, FIELD_XINGHAO, FIELD_JIAGE
from exceptions import NetworkError
from logger import debug_log, debug_log_v, redact_text
from request import http_request
from http_transport import request_with_redirects, permitted_transition
from secure_store import save_private_json, load_private_json
from client_utils import get_base_dir, _is_login_page
from client_auth import AuthMixin
from client_categories import CategoryAdminMixin
from client_content import ContentMixin
from content_admin import ContentAdminMixin
from single_admin import SingleAdminMixin
from client_products import ProductMixin
from client_messages import MessageMixin
from client_slides import SlideMixin
from admin_modules import AdminModuleMixin




class PbootCMSClient(AuthMixin, CategoryAdminMixin, ContentAdminMixin, SingleAdminMixin, ContentMixin, ProductMixin, MessageMixin, SlideMixin, AdminModuleMixin):
    def __init__(self):
        self.session = requests.Session()
        # ── 请求审计钩子（排查“拉取/修改是否改后台状态”）──
        # 把所有 GET/POST 请求（方法、路径、POST 是否含 status 类字段）写入
        # request_audit.log，独立于 debug_log 开关，始终开启。复现问题后把该日志发回即可定责。
        self.session.hooks["response"].append(PbootCMSClient._audit_response)
        # 关键稳定性：关闭系统代理/环境探测。requests 在 Windows 上默认会读取系统代理
        # （走 WinHTTP/注册表 COM 调用），在后台线程或主线程消息派发期间触发该 COM 出站
        # 调用会导致 RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010D) 原生崩溃。站点为直连，
        # 关闭不影响正常访问。
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.session.verify = True
        # Network routing is explicit per site.  Direct mode remains the
        # default for desktop stability; the UI can opt into the browser-like
        # system proxy or a validated HTTP(S) proxy and workers inherit it.
        self.network_mode = "direct"
        self.proxy_url = ""
        self.admin_url = ""
        self.base_url = ""
        self.logged_in = False
        self.last_response = ""
        self._last_list_html = ""
        # Resolved read-only form pages used to discover upload policies.
        # These are kept separate from the eventual POST action because a
        # custom CMS may expose a POST-only save endpoint.
        self._content_add_page_url = ""
        self._content_edit_page_url = ""
        self._session_dir = get_base_dir() / "site_sessions"
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._legacy_cookie_file = get_base_dir() / "session_cookies.json"
        self._cookie_file = self._legacy_cookie_file
        self.site_key = ""
        # 每个客户端实例只服务一个站点；保留字典以兼容旧数据格式
        self._logged_sites = {}

    def configure_network(self, mode="direct", proxy_url=""):
        """Apply a site-scoped proxy policy without changing credentials.

        ``system`` lets requests consume the process environment proxy (the
        closest safe approximation to a browser's proxy setting). ``custom``
        accepts only HTTP(S) proxy URLs without embedded credentials; callers
        must never smuggle a proxy into an arbitrary backend URL.
        """
        mode = str(mode or "direct").strip().lower()
        if mode not in {"direct", "system", "custom"}:
            raise ValueError("网络模式必须是 direct、system 或 custom")
        proxy = str(proxy_url or "").strip()
        if mode == "custom":
            parsed = urlparse(proxy)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                raise ValueError("自定义代理必须是 http:// 或 https:// 地址")
            if parsed.username or parsed.password:
                raise ValueError("自定义代理地址不允许内嵌用户名或密码")
            proxy = parsed.geturl().rstrip("/")
        else:
            proxy = ""
        self.network_mode = mode
        self.proxy_url = proxy
        if mode == "direct":
            self.session.trust_env = False
            self.session.proxies.clear()
        elif mode == "system":
            self.session.trust_env = True
            self.session.proxies.clear()
        else:
            self.session.trust_env = False
            self.session.proxies.update({"http": proxy, "https": proxy})
        debug_log(f"[network] mode={mode} proxy={'configured' if proxy else 'none'}")

    # ── 站点切换（极简：只改 URL，cookies 自动跟 domain 走）──
    def set_admin_url(self, url):
        old_admin = str(self.admin_url or "").rstrip("/").lower()
        url = url.rstrip("/")
        if "?p=" in url:
            url = url.split("?p=")[0]
        if "://" not in url:
            url = "https://" + url
        p = urlparse(url)
        new_base = f"{p.scheme}://{p.netloc}"
        old_base = self.base_url
        self.admin_url = url
        self.base_url = new_base
        # 后台入口（不仅是域名）生成稳定站点 ID。同域名的多个后台路径也完全隔离。
        import hashlib
        normalized = url.lower().rstrip("/")
        if old_admin and old_admin != normalized:
            # 同域名不同后台入口也按不同站点隔离。mcode/表单 action 若沿用
            # 上一入口缓存，可能把新站栏目映射到旧模型或旧写入端点。
            self._mcode_cache = {}
            self._content_add_actions = {}
            self._upload_policies = {}
            self._content_mcode_context = None
            self._product_mcode_context = None
            self._last_list_html = ""
            self.logged_in = False
        self.site_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
        self._cookie_file = self._session_dir / f"{self.site_key}.json"
        if old_base != new_base:
            self.logged_in = False
            debug_log(f"[session] 激活独立站点 {normalized} key={self.site_key}")

    # ── 会话持久化（保存/恢复所有 domain 的 cookies）──
    def save_session(self):
        """保存所有 cookies + 登录标记到文件"""
        try:
            import json
            # 更新登录标记
            if self.base_url and self.logged_in:
                self._logged_sites[self.base_url] = True
            # 导出所有 cookies
            all_cookies = {}
            for cookie in self.session.cookies:
                domain = cookie.domain.lstrip(".")
                if domain not in all_cookies:
                    all_cookies[domain] = []
                # Preserve the attributes browsers use when deciding whether
                # a cookie is sent.  The old format kept only name/value/
                # domain/path, which silently changed Secure, expiry,
                # host-only and SameSite-adjacent extension semantics after a
                # restart.  ``_rest`` is already a plain mapping for
                # requests' Cookie objects; stringify only non-JSON values.
                rest = {}
                for key, value in dict(getattr(cookie, '_rest', {}) or {}).items():
                    try:
                        json.dumps(value)
                        rest[str(key)] = value
                    except (TypeError, ValueError):
                        rest[str(key)] = str(value)
                all_cookies[domain].append({
                    "name": cookie.name, "value": cookie.value,
                    "domain": cookie.domain, "path": cookie.path,
                    "secure": bool(getattr(cookie, 'secure', False)),
                    "expires": getattr(cookie, 'expires', None),
                    "discard": bool(getattr(cookie, 'discard', False)),
                    "version": int(getattr(cookie, 'version', 0) or 0),
                    "domain_initial_dot": bool(getattr(cookie, 'domain_initial_dot', False)),
                    "rest": rest,
                })
            data = {
                "logged_sites": {k: v for k, v in self._logged_sites.items() if v},
                "cookies": all_cookies,
                "last_admin_url": self.admin_url,
            }
            save_private_json(self._cookie_file, data)
            debug_log(f"[session] 已保存: {len(all_cookies)} 个domain的cookies, logged_sites={list(self._logged_sites.keys())}")
        except Exception as e:
            debug_log(f"[session] 保存失败: {e}")

    def _claim_session_file_by_url(self):
        """按 site_key 找不到会话文件时，改用 admin_url 在会话目录里认领。

        site_key 是按**首次输入的地址**算的，而 adopt_resolved_url() 会在
        301/302 后把 admin_url 改成服务器的真实地址（且有意保留旧 key）。
        于是“输 http 被跳转到 https”的站，文件名是 http 算的 key，
        而下次拿 https 地址去算就对不上，会话白白丢掉。
        这里用文件里记的 last_admin_url 反向匹配，命中则认领该文件。
        """
        want = (self.admin_url or "").rstrip("/").lower()
        if not want or not self._session_dir.is_dir():
            return False
        for path in self._session_dir.glob("*.json"):
            if path == self._cookie_file:
                continue
            try:
                data = load_private_json(path)
            except Exception:
                continue
            saved_url = str(data.get("last_admin_url", "") or "").rstrip("/").lower()
            if saved_url and saved_url == want:
                self.site_key = path.stem
                self._cookie_file = path
                debug_log(f"[session] 按地址认领历史会话文件 {path.name}"
                          f"（原因：首次登录地址与当前地址不同）")
                return True
        return False

    def load_session(self):
        """从文件恢复 cookies + 登录标记到当前 session
        返回 (last_admin_url, 是否有已登录站点)
        """
        try:
            import json
            if not self._cookie_file.exists() and not self._claim_session_file_by_url():
                return "", False
            data = load_private_json(self._cookie_file)
            # 兼容旧版格式（_site_sessions 字典格式）
            if "cookies" not in data:
                debug_log(f"[session] 检测到旧版格式，将自动迁移")
                return self._migrate_old_format(data)
            # 新版格式
            self._logged_sites = data.get("logged_sites", {})
            all_cookies = data.get("cookies", {})
            for domain, cookies in all_cookies.items():
                for c in cookies:
                    # create_cookie retains the full requests Cookie shape;
                    # old files simply omit the optional attributes.
                    from requests.cookies import create_cookie
                    cookie_domain = c.get("domain", domain or "")
                    cookie = create_cookie(
                        name=c.get("name", ""), value=c.get("value", ""),
                        domain=cookie_domain, path=c.get("path", "/"),
                        secure=bool(c.get("secure", False)),
                        expires=c.get("expires"),
                        discard=bool(c.get("discard", False)),
                        version=int(c.get("version", 0) or 0),
                        rest=dict(c.get("rest") or {}),
                    )
                    # requests derives this from the domain string in normal
                    # cases.  Restore the explicit flag when supplied by a
                    # browser-exported/legacy cookie record.
                    if "domain_initial_dot" in c:
                        cookie.domain_initial_dot = bool(c.get("domain_initial_dot"))
                    self.session.cookies.set_cookie(cookie)
            last_url = data.get("last_admin_url", "")
            debug_log(f"[session] 已恢复: {len(all_cookies)} 个domain的cookies, logged_sites={list(self._logged_sites.keys())}")
            return last_url, bool(self._logged_sites)
        except Exception as e:
            debug_log(f"[session] 恢复失败: {e}")
            return "", False

    def _migrate_old_format(self, data):
        """迁移旧版 {base_url: {cookies, logged_in, admin_url}} 格式"""
        first_url = ""
        has_logged = False
        for base_url, site_data in data.items():
            if not isinstance(site_data, dict):
                continue
            for c in site_data.get("cookies", []):
                from requests.cookies import create_cookie
                cookie = create_cookie(
                    name=c.get("name", ""), value=c.get("value", ""),
                    domain=c.get("domain", ""), path=c.get("path", "/"),
                    secure=bool(c.get("secure", False)),
                    expires=c.get("expires"), discard=bool(c.get("discard", False)),
                    version=int(c.get("version", 0) or 0),
                    rest=dict(c.get("rest") or {}),
                )
                if "domain_initial_dot" in c:
                    cookie.domain_initial_dot = bool(c.get("domain_initial_dot"))
                self.session.cookies.set_cookie(cookie)
            if site_data.get("logged_in"):
                self._logged_sites[base_url] = True
                has_logged = True
            if not first_url:
                first_url = site_data.get("admin_url", "")
        if has_logged:
            self.save_session()  # 迁移后立即保存为新格式
            debug_log(f"[session] 旧格式已迁移为新格式")
        return first_url, has_logged

    def _url(self, path):
        return f"{self.admin_url}?p=/{path}"

    def adopt_resolved_url(self, url):
        """Adopt the server's resolved origin without changing site identity.

        A saved backend may use ``http://`` while the server permanently
        redirects to ``https://``.  The SiteContext and cookie file must keep
        their original stable key, but all subsequent network operations must
        use the resolved origin; otherwise requests turns a 301/302 POST into
        a GET and silently drops form/file data.
        """
        if not url:
            return
        resolved = urlparse(url)
        current = urlparse(self.admin_url)
        if not resolved.scheme or not resolved.netloc:
            return
        # Never let an unexpected cross-domain response rebind a site client.
        if current.netloc and not permitted_transition(self.admin_url, url):
            debug_log(
                f"[redirect] 忽略跨域地址切换: {current.netloc} -> {resolved.netloc}")
            return
        old_admin = self.admin_url
        admin_path = current.path or resolved.path
        self.base_url = f"{resolved.scheme}://{resolved.netloc}"
        self.admin_url = f"{self.base_url}{admin_path}".rstrip("/")
        if self.admin_url != old_admin:
            debug_log(f"[redirect] 运行地址已更新: {old_admin} -> {self.admin_url}")

    @staticmethod
    def _same_resource(left, right):
        """Whether two URLs differ only by transport scheme/default port."""
        a, b = urlparse(left), urlparse(right)
        host_a = (a.hostname or "").lower()
        host_b = (b.hostname or "").lower()
        return (host_a == host_b and a.path == b.path and a.query == b.query)

    def _post_preserving_transport_redirect(self, url, **kwargs):
        """Compatibility name: apply status-code semantics, never path-based replay."""
        # Content add/edit submits bypass ``_request`` because their form
        # enctype and redirect semantics are discovered from the live DOM.
        # Keep their same-origin POST context aligned with ordinary browser
        # form navigation as well.
        supplied = dict(kwargs.get("headers") or {})
        if not any(str(key).lower() == "origin" for key in supplied):
            try:
                current = urlparse(self.base_url or self.admin_url)
                target = urlparse(str(url or ""))
                if (current.scheme in ("http", "https") and current.netloc and
                        target.scheme in ("http", "https") and target.netloc and
                        permitted_transition(self.base_url or self.admin_url,
                                              str(url or ""))):
                    supplied["Origin"] = f"{current.scheme}://{current.netloc}"
            except (TypeError, ValueError):
                pass
        kwargs["headers"] = supplied
        response = request_with_redirects(self.session, 'POST', url, **kwargs)
        self.adopt_resolved_url(response.url)
        return response

    @staticmethod
    def _audit_response(resp, *args, **kwargs):
        """记录所有 HTTP 响应到 request_audit.log（独立于 debug_log 开关，始终开启）。
        用于排查“拉取/修改是否改后台状态”：
          - GET 列表页/编辑页不会改后台；
          - POST 且该 POST 含 status/istop/isrecommend/isheadline 字段，才是改动来源。
        """
        try:
            import datetime
            from urllib.parse import urlparse
            req = resp.request
            method = req.method
            # 记录【完整请求 URL（含 query string）】：PbootCMS 的字段切换链接形如
            # ?p=/Content/mod/id/58/field/status/value/0，仅记录 path 会丢失 ?p=...，
            # 导致「GET 即写入」的隐藏写操作在审计里被掩盖成普通 GET /xxx.php。
            full_url = redact_text(req.url)
            line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {method} {full_url} -> {resp.status_code}"
            if method == "POST":
                body = req.body
                keys = []
                try:
                    if isinstance(body, (bytes, bytearray)):
                        b = body.decode("utf-8", "ignore")
                    else:
                        b = str(body or "")
                    for k in ("status", "istop", "isrecommend", "isheadline", "formcheck"):
                        if k in b:
                            keys.append(k)
                except Exception:
                    pass
                if keys:
                    line += f"  含字段={keys}"
            try:
                audit_path = get_base_dir() / "request_audit.log"
                with open(audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        except Exception:
            pass


    def _request(self, method, url, **kwargs):
        """统一HTTP请求，默认只发送一次；纯读取可显式read_only=True。

        所有重试逻辑集中在一处（request.py），避免各调用点重复实现。
        重试耗尽时抛出 NetworkError（caller 捕获后转为用户可见的错误信息）。
        """
        # Modern browsers include an Origin header on same-origin unsafe
        # submissions (POST/PUT/PATCH/DELETE), including ordinary form
        # navigations.  Most callers already provide a page-specific
        # Referer; add only the missing Origin here so category/slide/single/
        # message writes use the same request context as the web page.  Do
        # not invent an Origin for GET/HEAD or cross-origin targets, and let
        # an explicit caller header win.
        method_name = str(method or "").upper()
        if method_name not in ("GET", "HEAD"):
            supplied = dict(kwargs.get("headers") or {})
            has_origin = any(str(key).lower() == "origin" for key in supplied)
            if not has_origin:
                try:
                    current = urlparse(self.base_url or self.admin_url)
                    target = urlparse(str(url or ""))
                    if (current.scheme in ("http", "https") and current.netloc and
                            target.scheme in ("http", "https") and target.netloc and
                            permitted_transition(self.base_url or self.admin_url,
                                                  str(url or ""))):
                        supplied["Origin"] = f"{current.scheme}://{current.netloc}"
                except (TypeError, ValueError):
                    pass
            kwargs["headers"] = supplied
        return http_request(self.session, method, url, **kwargs)

    # ─── 登录 ───



    # ─── 栏目获取 ───


    # ─── 获取内容表单字段 ───


    # ─── 图片上传 ───




    # ─── 发布内容 ───

    # ─── 获取文章列表 ───





    # ─── 查询修改 ───
