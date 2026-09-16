"""Focused PbootCMS AuthMixin service."""
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
from logger import debug_log, debug_log_v
from request import http_request
from http_transport import request_with_redirects, permitted_transition
from client_utils import get_base_dir, _is_login_page
from form_controls import submission_attributes, successful_pairs

class AuthMixin:
    @staticmethod
    def _same_admin_host(left, right):
        """登录相关 URL 只能保持同源（允许同主机 HTTP→HTTPS 升级）。"""
        try:
            return permitted_transition(str(left or ""), str(right or ""))
        except (NetworkError, ValueError, TypeError):
            return False

    def logout(self):
        """Log out this client only and remove its persisted site session.

        Every SiteContext owns a separate client/session file, therefore this
        operation cannot log out or delete cookies belonging to another site.
        Saved usernames/passwords are intentionally retained for the next
        login; only authenticated session material is removed.
        """
        remote_error = ""
        try:
            # Logout is a browser navigation, but it still must not forward
            # the authenticated session across an unexpected host.  Follow
            # only same-origin/default-port HTTPS redirects and never replay
            # the request after a transport failure; local cleanup below is
            # unconditional even when the remote result is unknown.
            logout_url = self._url("Index/loginOut")
            response = request_with_redirects(
                self.session, "GET", logout_url, timeout=TIMEOUT_NORMAL,
                headers={"Referer": self.admin_url})
            if not self._same_admin_host(logout_url, getattr(response, "url", "")):
                remote_error = "服务器登出跳转到了其他主机"
            elif not response.ok:
                remote_error = f"服务器返回 HTTP {response.status_code}"
        except Exception as exc:
            remote_error = str(exc)
        finally:
            try:
                self.session.cookies.clear()
            except Exception as exc:
                debug_log(f"[logout] 清除会话 cookie 失败: {exc}")
            self.logged_in = False
            self._logged_sites = {}
            try:
                if self._cookie_file.exists():
                    self._cookie_file.unlink()
            except Exception as exc:
                debug_log(f"[logout] 删除站点会话文件失败: {exc}")

        if remote_error:
            return False, f"本地登录状态已清除；服务器登出请求失败：{remote_error}"
        return True, "已退出当前账户"

    def clear_all_cache(self):
        """Clear all PbootCMS runtime caches for the authenticated site.

        This deliberately uses PbootCMS' own same-origin admin action instead
        of touching server paths directly.  A positive backend message is
        required before reporting success to the UI.
        """
        if not self.logged_in:
            raise RuntimeError("请先登录当前网站后台")
        url = self._url("Index/clearCache/delall/1")
        response = self._request(
            "GET", url, timeout=TIMEOUT_NORMAL,
            headers={"Referer": self._url("Index/home")})
        text = str(getattr(response, "text", "") or "")
        if _is_login_page(text):
            self.logged_in = False
            raise RuntimeError("登录会话已失效，请重新登录")
        if not getattr(response, "ok", False):
            raise RuntimeError(
                f"清理缓存失败（HTTP {getattr(response, 'status_code', '?')}）")
        if re.search(r"清理缓存失败|缓存清理失败|无权限|权限不足", text, re.I):
            raise RuntimeError("网站后台返回清理缓存失败或权限不足")
        if not re.search(r"清理缓存成功|缓存清理成功", text, re.I):
            raise RuntimeError("网站后台未明确确认缓存已清理，请登录后台核对")

        # Invalidate only disposable routing/form discovery data.  Login
        # cookies, saved credentials, drafts and the local product repository
        # are intentionally preserved.
        self._mcode_cache = {}
        self._content_add_actions = {}
        self._content_mcode_context = None
        self._product_mcode_context = None
        self._last_list_html = ""
        return {"msg": "当前网站后台的所有缓存已清理"}

    def check_auth(self):
        """验证当前 cookie 是否有效（访问后台首页检测是否在登录页）
        注意：此方法会发起网络请求，调用方应考虑异步执行
        """
        try:
            # Use the shared browser-like redirect guard instead of letting
            # requests follow an arbitrary Location.  A login page can
            # legitimately redirect HTTP -> HTTPS on the same host, but an
            # unexpected SSO/error host must never receive the authenticated
            # session cookie during an auth probe.
            resp = request_with_redirects(
                self.session, "GET", self.admin_url, timeout=TIMEOUT_NORMAL)
            if not resp.ok:
                debug_log(f"[check_auth] HTTP {resp.status_code} from {self.admin_url}")
                return False
            # Browser navigation may follow redirects, but an authenticated
            # backend session must never be considered valid after it lands on
            # another host (SSO/logout/error pages can otherwise look like a
            # normal 200 response).  Keep this read-only probe same-origin.
            if not self._same_admin_host(self.admin_url, getattr(resp, "url", "")):
                debug_log(f"[check_auth] 跨主机跳转: {getattr(resp, 'url', '')}")
                return False
            # Keep the requests client on the browser's canonical final
            # origin after a safe default-port HTTP→HTTPS upgrade.  Without
            # this, a successful read could be followed by a second request
            # to the old HTTP entry and differ from the page that the user
            # just authenticated in the browser.
            resolved = str(getattr(resp, "url", "") or "")
            adopter = getattr(self, "adopt_resolved_url", None)
            if callable(adopter) and resolved:
                adopter(resolved)
            text = resp.text
            # formcheck is a CSRF token and appears on both login and authenticated
            # admin pages.  Treating it as a login marker caused valid sessions to
            # be reported as expired whenever a category request had a transient
            # failure.  Use only the shared, login-page-specific detector.
            if _is_login_page(text):
                debug_log("[check_auth] 检测到登录页")
                return False
            # A few custom themes return a short login/error fragment without
            # the standard password form.  Do not promote that fragment to a
            # valid session solely because the transport status is 200.
            if re.search(r"登录(?:失效|过期)|请先登录|未授权|权限不足|unauthorized|forbidden",
                         str(text or ""), re.I):
                debug_log("[check_auth] 响应包含登录/权限失效提示")
                return False
            return True
        except Exception as e:
            debug_log(f"[check_auth] 验证异常({self.admin_url}): {e}")
        return False

    def fetch_login_page(self, page_timeout=12, captcha_budget=10):
        """获取登录页面，自动提取表单信息
        返回 dict: {formcheck, captcha_bytes, has_captcha,
                    form_action, captcha_field, captcha_img_src}
        """
        # A login-page fetch is initiated from the UI.  Keep it bounded: the
        # old 30 + 10 + 8 + 8 + 8 second chain looked like a permanent hang.
        requested_admin_url = self.admin_url
        resp = request_with_redirects(
            self.session, "GET", self.admin_url, timeout=page_timeout)
        resp.raise_for_status()
        # 跟踪重定向：如果服务器从http跳到https，更新admin_url和base_url
        final_url = resp.url
        if not self._same_admin_host(requested_admin_url, final_url):
            raise NetworkError(
                "后台地址发生跨域跳转，已拒绝继续登录："
                f"{urlparse(requested_admin_url).hostname} -> "
                f"{urlparse(final_url).hostname}")
        if final_url != self.admin_url:
            # 只更新基础URL，剥离 ?p= 参数避免污染
            p = urlparse(final_url)
            clean_final = f"{p.scheme}://{p.netloc}{p.path}"
            if clean_final != self.admin_url:
                self.admin_url = clean_final
                self.base_url = f"{p.scheme}://{p.netloc}"
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        info = {
            "formcheck": "",
            "captcha_bytes": None,
            "has_captcha": False,
            "form_action": "",       # 表单提交地址
            "captcha_field": "code",  # 验证码字段名（默认code，PbootCMS常用checkcode）
            "captcha_img_src": "",    # 验证码图片地址
            "captcha_error": "",      # 登录页成功但验证码图片失败的可读原因
            "is_login_page": True,   # 默认视为登录页；拉到后台页则为 False
            "resolved_admin_url": self.admin_url,  # 服务器最终 HTTP/HTTPS 后台地址
            "login_fields": {},             # 二开表单的额外成功控件
            "login_pairs": [],               # browser-order successful controls
            "login_pairs_include_core": False,
            "form_method": "post",
            "form_enctype": "application/x-www-form-urlencoded",
            "submitter": {},
        }

        # ── 提取form action ──
        form = soup.find("form")
        if form:
            action = form.get("action", "")
            if action:
                resolved_action = urljoin(self.admin_url, action)
                if not self._same_admin_host(self.admin_url, resolved_action):
                    raise NetworkError("登录表单提交地址跨域，已拒绝提交账号密码")
                info["form_action"] = resolved_action

        # ── 提取 CSRF token ──
        fc = soup.find("input", {"name": "formcheck"})
        if fc:
            info["formcheck"] = fc.get("value", "")

        # ── 自动检测验证码字段名 ──
        for name in ["checkcode", "code", "captcha", "verifycode", "vcode"]:
            el = soup.find("input", {"name": name})
            if el:
                info["captcha_field"] = name
                info["has_captcha"] = True
                break
        # 正则兜底
        if not info["has_captcha"]:
            el = soup.find("input", {"name": re.compile(r"check|captcha|verify|code", re.I)})
            if el and el.get("name") != "formcheck" and el.get("name") != "username" and el.get("name") != "password":
                info["captcha_field"] = el["name"]
                info["has_captcha"] = True

        # Preserve successful hidden/select/checkbox controls from the actual
        # login form.  Standard PbootCMS only needs username/password/
        # formcheck/code, but SSO/WAF/custom themes commonly add tenant,
        # remember, nonce or submit-mode fields.  Do not execute JS or carry
        # password/captcha values from the page; credentials supplied by the
        # user overwrite those fields during login().
        if form:
            attrs = submission_attributes(form)
            # A login page normally omits method and relies on its AJAX
            # handler; retain the historical POST fallback while honoring an
            # explicit native form method/enctype.
            info["form_method"] = (str(attrs.get("method") or "post").lower()
                                   if str(form.get("method") or "").strip() else "post")
            info["form_enctype"] = str(attrs.get("enctype") or
                                       "application/x-www-form-urlencoded").lower()
            submit = form.find(["button", "input"], attrs={"type": re.compile(r"submit|image", re.I)})
            if submit:
                info["submitter"] = {
                    key: str(submit.get(key, "") or "")
                    for key in ("name", "value", "type", "formaction", "formmethod", "formenctype")
                    if submit.has_attr(key)
                }
            extras = {}
            for name, value in successful_pairs(form):
                if name in {"username", "password", "formcheck", info["captcha_field"]}:
                    continue
                if name in extras:
                    extras[name] = extras[name] if isinstance(extras[name], list) else [extras[name]]
                    extras[name].append(value)
                else:
                    extras[name] = value
            info["login_fields"] = extras
            # Keep the complete original successful-control order for GET,
            # multipart and text/plain transports.  Credential and captcha
            # values are replaced at send time, rather than moved to the end
            # of the list; this matters to custom parsers that inspect order.
            info["login_pairs"] = list(successful_pairs(form, submitter=info["submitter"]))
            info["login_pairs_include_core"] = True

        # ── 多策略检测验证码图片 ──
        captcha_el = None
        for pattern in [
            lambda: soup.find("img", {"id": "captcha"}),
            lambda: soup.find("img", {"id": "checkcode"}),
            lambda: soup.find("img", {"id": "codeimg"}),
            lambda: soup.find("img", {"id": "verify_img"}),
            lambda: soup.find("img", {"id": re.compile(r"captcha|check|verify|code", re.I)}),
            lambda: soup.find("img", src=re.compile(r"captcha|checkcode|verify|vcode|code\.php", re.I)),
            lambda: soup.find("img", class_=re.compile(r"captcha|checkcode|verify|code", re.I)),
            lambda: soup.find("img", onclick=re.compile(r"captcha|checkcode|verify|code", re.I)),
            lambda: self._find_captcha_near_code_input(soup),
        ]:
            try:
                captcha_el = pattern()
            except Exception as _e:
                debug_log("[error] " + str(_e))
            if captcha_el:
                break

        captcha_candidates = []
        if captcha_el:
            info["has_captcha"] = True
            src = captcha_el.get("src", "")
            if src:
                info["captcha_img_src"] = src
                captcha_url = urljoin(self.admin_url, src)
                if not self._same_admin_host(self.admin_url, captcha_url):
                    info["captcha_img_src"] = ""
                elif captcha_url:
                    captcha_candidates.append(captcha_url)

        # If the detected image is unavailable, try compatible endpoints, but
        # make every candidate share one small total budget.
        if info["has_captcha"] and not info["captcha_bytes"]:
            for endpoint in [
                "/core/code.php",  # PbootCMS默认
                "index/captcha",
                "index/checkcode",
            ]:
                try:
                    if endpoint.startswith("/"):
                        url = urljoin(self.admin_url, endpoint)
                    else:
                        url = self._url(endpoint)
                    if self._same_admin_host(self.admin_url, url):
                        captcha_candidates.append(url)
                except Exception as exc:
                    debug_log(f"[captcha] 构造备用地址失败: {exc}")
            deadline = time.monotonic() + max(1, float(captcha_budget or 0))
            errors = []
            for url in dict.fromkeys(captcha_candidates):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    errors.append("验证码图片请求超时")
                    break
                try:
                    cr = request_with_redirects(
                        self.session, "GET", url,
                        timeout=max(1, min(6, remaining)))
                    if cr.ok and len(cr.content) > 100:
                        ct = cr.headers.get("Content-Type", "")
                        if "image" in ct or cr.content[:4] in [b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF"]:
                            info["captcha_bytes"] = cr.content
                            break
                    errors.append(f"{url} 未返回图片")
                except Exception as exc:
                    errors.append(f"{url}: {exc}")
                    debug_log(f"[captcha] 获取失败 {url}: {exc}")
            if not info["captcha_bytes"]:
                info["captcha_error"] = ("；".join(errors) or
                                         "验证码图片接口未返回图片")[:300]

        # 真实登录态判定：用拉到的页面是否为登录页，而非盲目信任内存标记
        # 登录页含密码输入框（后台页无），用 BS 解析更稳；再叠加字符串启发式兜底
        has_pwd = bool(soup.find("input", {"type": "password"}))
        info["is_login_page"] = has_pwd or _is_login_page(html)
        return info

    def _find_captcha_near_code_input(self, soup):
        """查找验证码输入框附近的img标签"""
        code_input = soup.find("input", {"name": "code"}) or soup.find(
            "input", {"name": re.compile(r"captcha|checkcode|verify|vcode", re.I)}
        )
        if not code_input:
            return None
        # 向上查找父容器中的img
        parent = code_input.parent
        for _ in range(5):  # 最多向上5层
            if parent is None:
                break
            img = parent.find("img")
            if img:
                return img
            parent = parent.parent
        return None

    def login(self, username, password, captcha="", login_info=None):
        """登录后台，返回 (success, message)
        login_info: fetch_login_page()返回的dict，包含form_action、captcha_field等
        """
        if login_info is None:
            login_info = {}

        # 确定提交URL：优先用从页面提取的form action
        action = login_info.get("form_action", "")
        submitter = dict(login_info.get("submitter") or {})
        if submitter.get("formaction"):
            action = urljoin(self.admin_url, str(submitter.get("formaction")))
        if action:
            submit_url = action
        else:
            submit_url = self._url("index/login")
        if not permitted_transition(self.admin_url, submit_url):
            return False, "登录表单提交地址跨域，已拒绝提交账号密码"

        # 确定验证码字段名
        captcha_field = login_info.get("captcha_field", "code")

        # 构建提交数据（密码明文，PbootCMS标准行为）
        data = dict(login_info.get("login_fields") or {})
        data.update({
            "username": username,
            "password": password,
            "formcheck": login_info.get("formcheck", ""),
        })
        if captcha:
            data[captcha_field] = captcha
        submit_name = str(submitter.get("name", "") or "").strip()
        submit_type = str(submitter.get("type", "submit") or "submit").lower()
        # An image submitter contributes the physical click coordinates that
        # the browser recorded.  A requests-only login has no such click
        # point; sending the historical ``0,0`` pair changes the server-side
        # branch for pages that inspect it.  Fail closed and let the native
        # authenticated WebView perform the real click instead.
        if submit_type == "image" and not any(
                key in submitter for key in ("x", "y", "click_x", "click_y")):
            self.last_login_outcome = "not_sent"
            return False, "登录按钮是图片提交按钮，无法在桌面端取得真实点击坐标；请使用原生网页登录"
        if submit_name and submit_type not in ("button", "reset", "file", "image"):
            data[submit_name] = submitter.get("value", "")

        # 调试日志（脱敏，避免明文密码泄露到 stdout / 日志文件）
        safe_data = {k: ("***" if (k == "password" or re.search(
            r"token|secret|csrf|formcheck|nonce", str(k), re.I)) else v)
                     for k, v in data.items()}
        debug_log(f"[login] 提交URL: {submit_url}")
        debug_log(f"[login] 验证码字段: {captcha_field}")
        debug_log(f"[login] 发送数据(已脱敏): {safe_data}")

        method = str(submitter.get("formmethod") or
                     login_info.get("form_method") or "post").lower()
        enctype = str(submitter.get("formenctype") or
                      login_info.get("form_enctype") or
                      "application/x-www-form-urlencoded").lower()
        if method not in ("get", "post"):
            return False, "登录表单使用了不支持的提交方法，已停止发送"
        if enctype not in ("application/x-www-form-urlencoded", "multipart/form-data",
                           "text/plain"):
            return False, "登录表单编码方式无法安全复现，已停止发送"

        # Use browser-order pairs for transports where a mapping would
        # collapse repeated names.  ``login_fields`` is still kept in
        # ``data`` for the legacy URL-encoded path and API compatibility.
        wire_pairs = []
        supplied_pairs = login_info.get("login_pairs")
        supplied_pairs = (list(supplied_pairs)
                          if isinstance(supplied_pairs, (list, tuple)) else [])
        include_core = bool(login_info.get("login_pairs_include_core"))
        core_values = {
            "username": username,
            "password": password,
            "formcheck": login_info.get("formcheck", ""),
        }
        if captcha:
            core_values[captcha_field] = captcha
        seen_core = set()
        if supplied_pairs and include_core:
            for item in supplied_pairs:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                key, value = str(item[0]), item[1]
                if key in core_values:
                    value = core_values[key]
                    seen_core.add(key)
                wire_pairs.append((key, value))
        elif supplied_pairs:
            # Backwards-compatible callers historically supplied only extra
            # fields in login_pairs; retain that interpretation unless the
            # fetcher explicitly marks a complete browser-order template.
            wire_pairs = [
                (str(item[0]), item[1]) for item in supplied_pairs
                if isinstance(item, (list, tuple)) and len(item) == 2]
        else:
            for key, value in data.items():
                # ``data`` already contains the credential/token fields that
                # are appended below.  Keep only the extra successful
                # controls here so ordered transports do not send those
                # values twice when a caller did not provide ``login_pairs``.
                if str(key) in {"username", "password", "formcheck", captcha_field}:
                    continue
                values = value if isinstance(value, (list, tuple)) else [value]
                wire_pairs.extend((str(key), item) for item in values)
        for key, value in (("username", username), ("password", password),
                           ("formcheck", login_info.get("formcheck", ""))):
            if key not in seen_core:
                wire_pairs.append((key, value))
        if captcha and captcha_field not in seen_core:
            wire_pairs.append((captcha_field, captcha))
        submitter_pairs = []
        if submit_name and submit_type == "image":
            click_x = submitter.get("x", submitter.get("click_x"))
            click_y = submitter.get("y", submitter.get("click_y"))
            submitter_pairs = [(f"{submit_name}.x", str(click_x)),
                               (f"{submit_name}.y", str(click_y))]
        elif submit_name and submit_type not in ("button", "reset", "file"):
            submitter_pairs = [(submit_name, submitter.get("value", ""))]
        if not include_core:
            for pair in submitter_pairs:
                if pair not in wire_pairs:
                    wire_pairs.append(pair)
        elif submitter_pairs and not any(pair in wire_pairs for pair in submitter_pairs):
            # A hand-authored login_info can mark a complete template but omit
            # the clicked submitter; preserve a usable fallback in that case.
            wire_pairs.extend(submitter_pairs)

        # 登录POST用AJAX头（模拟jQuery $.ajax行为）；显式原生 GET/
        # multipart 仍遵循表单自身的传输属性。
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": self.admin_url,
        }
        # The login page's AJAX POST carries the page origin in modern
        # browsers.  Keep it same-origin only; the form/action guards above
        # still reject an unexpected host before credentials are sent.
        try:
            page_origin = urlparse(self.admin_url)
            target_origin = urlparse(submit_url)
            if (page_origin.scheme in ("http", "https") and page_origin.netloc and
                    target_origin.scheme in ("http", "https") and target_origin.netloc and
                    permitted_transition(self.admin_url, submit_url)):
                ajax_headers["Origin"] = f"{page_origin.scheme}://{page_origin.netloc}"
        except (TypeError, ValueError):
            pass
        if method == "post" and enctype == "application/x-www-form-urlencoded":
            ajax_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        elif method == "post" and enctype == "text/plain":
            ajax_headers["Content-Type"] = "text/plain;charset=UTF-8"

        # A timeout may already have consumed a captcha or a login attempt.
        # Redirects follow HTTP semantics, but network failure never replays it.
        self.last_login_outcome = 'not_sent'
        try:
            if method == "get":
                resp = request_with_redirects(self.session, 'GET', submit_url,
                    params=wire_pairs, timeout=30, headers=ajax_headers)
            elif enctype == "multipart/form-data":
                parts = [(str(key), (None, str(value)))
                         for key, value in wire_pairs]
                resp = request_with_redirects(self.session, 'POST', submit_url,
                    data={}, files=parts, timeout=30, headers=ajax_headers)
            elif enctype == "text/plain":
                body = ''.join(f"{key}={value}\r\n" for key, value in wire_pairs)
                resp = request_with_redirects(self.session, 'POST', submit_url,
                    data=body.encode("utf-8"), timeout=30, headers=ajax_headers)
            else:
                # ``requests`` accepts an ordered pair list for URL-encoded
                # bodies.  Passing the legacy ``data`` mapping here collapsed
                # duplicate names and moved credential fields, while a real
                # browser preserves every successful control in DOM order.
                resp = request_with_redirects(self.session, 'POST', submit_url,
                    data=wire_pairs, timeout=30, headers=ajax_headers)
        except (requests.Timeout, requests.ConnectionError, NetworkError):
            self.last_login_outcome = 'unknown'
            return False, '登录请求结果未知，未自动重发；请检查登录状态或重新获取验证码后再试'
        if not 200 <= resp.status_code < 300:
            self.last_login_outcome = 'unknown' if resp.status_code >= 500 else 'rejected'
            return False, f'登录请求返回HTTP {resp.status_code}，未自动重发，请先核对登录状态'
        text = resp.text
        url = resp.url
        if not self._same_admin_host(self.admin_url, url):
            return False, "登录响应跳转到其他域名，已按安全策略拒绝"
        self.last_response = text  # 保存响应供调试

        # 尝试从JSON响应判断（PbootCMS layui登录返回JSON）
        try:
            result = resp.json()
            message_text = str(result.get('msg') or result.get('message') or result.get('data') or '')
            explicit_failure = (('success' in result and str(result['success']).lower() not in ('true', '1', 'success'))
                                or ('state' in result and str(result['state']).upper() != 'SUCCESS'))
            if (result.get('error') or explicit_failure or
                    re.search(r'失败|错误|不正确|权限不足|\b(?:error|failed|invalid|denied)\b',message_text,re.I)):
                self.last_login_outcome = 'rejected'
                return False, str(result.get('error') or result.get('msg') or '后台拒绝登录')
            if result.get("code") in (1, '1'):
                self.last_login_outcome = 'reported_success'
                self.logged_in = True
                self.save_session()
                return True, "登录成功"
            # 错误信息在data或msg字段
            err_msg = result.get("data", "") or result.get("msg", "") or "登录失败"
            self.last_login_outcome = 'rejected'
            return False, err_msg
        except Exception as _e:
            debug_log("[error] " + str(_e))

        # 尝试从页面提取错误
        soup = BeautifulSoup(text, "html.parser")
        for cls in ["alert-danger", "alert", "error", "layui-layer-content"]:
            el = soup.find(class_=cls)
            if el and (cls in ('alert-danger', 'error') or re.search(
                    r'失败|错误|不正确|权限不足|\b(?:error|failed|invalid|denied)\b',el.get_text(),re.I)):
                self.last_login_outcome = 'rejected'
                return False, el.get_text(strip=True)[:200]

        # 检查是否仍在登录页：响应 HTML 仍为登录页（含密码框）→ 登录失败
        # 用 _is_login_page 判定，避免“后台页含 username 字样”等误判
        if _is_login_page(text):
            self.last_login_outcome = 'rejected'
            return False, "用户名或密码错误"

        links = []
        for link in soup.find_all('a', href=True):
            try:
                target = urljoin(url, link['href'])
                if permitted_transition(url, target):
                    links.append(target.lower())
            except NetworkError:
                continue
        if (any('index/loginout' in href for href in links) and
                any('content/index' in href or 'index/home' in href for href in links)):
            self.last_login_outcome = 'verified_page'
            self.logged_in = True
            self.save_session()
            return True, "已读取到后台登录导航"
        self.last_login_outcome = 'unknown'
        return False, '后台未明确确认登录成功；错误页或非登录页不能证明已登录，请刷新核对'
