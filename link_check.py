# -*- coding: utf-8 -*-
"""正文内链体检：找出指向本站但实际不存在的死链。

场景：HTML 由 AI 提前生成，正文里常出现形如
    https://site.test/example-article
的链接——域名是对的（本站），但后面的 slug 是 AI 编的，站上并不存在，
发布后即成 404 死链，对 SEO 有害。

本模块只做两件事：
  1) scan_content_links：从正文抽出所有 <a href>，按“本站/外站/锚点”分类；
  2) probe_dead_links：用已登录会话对“本站链接”逐个探测存活性（HEAD/GET）。

不猜测、不替换真实 URL——猜错只会制造新错误链接。是否处理、如何处理
（默认移除死链保留锚文字）交由上层让用户确认。
"""

import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from logger import debug_log


_NAV_REDIRECTS = frozenset((301, 302, 303, 307, 308))


def _same_navigation_origin(left, right):
    """Whether two navigation URLs share the browser's safe navigation origin.

    A browser treats an explicit same-host ``http:80`` to ``https:443``
    upgrade as the same site navigation for cookie/referrer purposes.  The
    previous exact-scheme comparison classified that hop as cross-origin and
    replaced the authenticated session with an empty clone, which made an
    otherwise reachable internal link look like a login/unknown result.
    """
    try:
        a = urlparse(str(left or ""))
        b = urlparse(str(right or ""))
        if a.scheme.lower() not in ("http", "https") or b.scheme.lower() not in ("http", "https"):
            return False
        host_same = ((a.hostname or "").lower().rstrip(".") ==
                     (b.hostname or "").lower().rstrip("."))
        a_port = a.port or (443 if a.scheme.lower() == "https" else 80)
        b_port = b.port or (443 if b.scheme.lower() == "https" else 80)
        if not host_same:
            return False
        if a.scheme.lower() == b.scheme.lower() and a_port == b_port:
            return True
        return (a.scheme.lower() == "http" and a_port == 80 and
                b.scheme.lower() == "https" and b_port == 443)
    except (TypeError, ValueError):
        return False


def _browser_navigation(session, method, url, timeout, deadline=None,
                        cancel_callback=None, stream=False, max_redirects=20):
    """Perform bounded browser-like HEAD/GET navigation without unrestricted redirects.

    Link checking is intentionally allowed to *observe* a public cross-site
    redirect so that a WAF or migrated page is reported as ``unknown`` rather
    than as a transport failure.  The old ``allow_redirects=True`` call let a
    requests session carry explicit Authorization/Cookie headers across that
    redirect.  Follow each hop ourselves, strip sensitive explicit headers on
    a cross-origin hop, and use a cookie-free cloned session for real
    ``requests.Session`` objects.  This preserves browser navigation's
    visibility while avoiding credential leakage and an unbounded chain.
    """
    method = str(method or "GET").upper()
    current = str(url or "")
    history = []
    active_session = session
    cross_session = None
    base_kwargs = {"timeout": _request_timeout(timeout, deadline),
                   "allow_redirects": False}
    if stream:
        base_kwargs["stream"] = True
    try:
        for hop in range(max(1, int(max_redirects)) + 1):
            stopped = _probe_stop_reason(cancel_callback, deadline)
            if stopped:
                raise _ProbeDeadline()
            sender = getattr(active_session, method.lower())
            response = sender(current, **base_kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            location = (getattr(response, "headers", {}) or {}).get("Location", "")
            if status not in _NAV_REDIRECTS or not location:
                response.history = history
                return response
            if hop >= max_redirects:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                raise requests.RequestException("redirect limit exceeded")
            target = urljoin(str(getattr(response, "url", "") or current),
                             str(location))
            parsed = urlparse(target)
            if (parsed.scheme.lower() not in ("http", "https") or
                    not parsed.netloc or parsed.username or parsed.password or
                    any(ord(char) < 32 for char in target)):
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                raise requests.RequestException("invalid redirect target")
            history.append(response)
            close = getattr(response, "close", None)
            if callable(close):
                close()
            if not _same_navigation_origin(current, target):
                if isinstance(session, requests.Session):
                    # Preserve TLS/proxy/UA settings but do not send the
                    # authenticated cookie jar or explicit auth headers to a
                    # public external destination.
                    cross_session = _clone_requests_session(session)
                    cross_session.cookies.clear()
                    cross_session.auth = None
                    # Session-level query parameters and client certificates
                    # are credentials too; a browser navigation would not
                    # carry an application's backend token or mTLS identity
                    # to an unrelated public host.
                    cross_session.params = {}
                    cross_session.cert = None
                    for key in list(cross_session.headers):
                        if str(key).lower() in {"authorization", "cookie",
                                                "proxy-authorization", "origin",
                                                "referer", "x-requested-with",
                                                "x_requested_with"}:
                            cross_session.headers.pop(key, None)
                    active_session = cross_session
                else:
                    active_session = session
            current = target
        raise requests.RequestException("redirect chain did not finish")
    finally:
        if cross_session is not None:
            try:
                cross_session.close()
            except Exception:
                pass


def _same_site(host_a, host_b):
    """域名是否同站（忽略大小写与 www. 前缀）。"""
    def normalize(host):
        value = (host or "").lower().rstrip(".")
        # str.lstrip("www.") 会按“字符集合”删除，可能把 wow.com 等合法域名
        # 也截坏；这里只删除确切的 www. 前缀。
        return value[4:] if value.startswith("www.") else value
    a = normalize(host_a)
    b = normalize(host_b)
    return bool(a) and a == b


def scan_content_links(html, base_url):
    """抽取正文里的 <a href>，按本站/外站/锚点(非http)分类。

    返回 dict：
      internal: [{"href","text","abs"}]  指向本站的链接（需体检）
      external: [...]                     外站链接（不动）
      anchor:   [...]                     #锚点 / mailto / tel / javascript 等
    abs 为规范化后的绝对地址（相对路径按 base_url 补全）。
    """
    result = {"internal": [], "external": [], "anchor": []}
    if not html:
        return result
    base_host = urlparse(base_url or "").netloc
    soup = BeautifulSoup(html, "html.parser")
    for link_index, a in enumerate(soup.find_all("a", href=True)):
        href = (a.get("href") or "").strip()
        text = a.get_text(strip=True)
        item = {"href": href, "text": text, "abs": "",
                "model": (a.get("data-model") or "").strip(),
                # Stable within this exact HTML snapshot.  New callers can
                # return it with a link action so two placeholders that both
                # use href="#" are still independently addressable.
                "index": link_index}
        low = href.lower()
        if not href or low.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            # 带 data-model 的锚点链接（如 href="#"）也要当待处理目标，归为 internal
            if item["model"]:
                result["internal"].append(item)
            else:
                result["anchor"].append(item)
            continue
        parsed = urlparse(href)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            result["anchor"].append(item)
            continue
        # 相对路径 → 用当前站补成绝对地址
        abs_url = href if parsed.netloc else urljoin(_ensure_slash(base_url), href)
        item["abs"] = abs_url
        host = urlparse(abs_url).netloc
        if not parsed.netloc or _same_site(host, base_host):
            result["internal"].append(item)
        else:
            result["external"].append(item)
    return result


def _ensure_slash(base_url):
    """把后台入口地址收敛成站点根，用于相对链接补全。"""
    p = urlparse(base_url or "")
    if not p.scheme:
        return base_url or ""
    return f"{p.scheme}://{p.netloc}/"


_MODEL_RE = re.compile(r"[A-Za-z]{1,6}-?[A-Za-z]*\d{1,5}[A-Za-z0-9-]*")


def _norm_model(s):
    """型号归一化：去空白、去连字符、转小写，便于容错匹配。

    MODEL-100 / model 100 / MODEL100 归为同一键，避免因连字符/大小写差异漏匹配。
    """
    return re.sub(r"[\s\-_]+", "", str(s or "")).lower()


def guess_model_from_item(item):
    """从一个链接推断它指向的产品型号。

    优先用显式的 data-model（规范要求 AI 写）；没有则从锚文字、
    再不行从 href 里正则抽型号（容错兼容旧 HTML）。抽不到返回空串。
    """
    if item.get("model"):
        return item["model"].strip()
    for src in (item.get("text", ""), item.get("href", "")):
        m = _MODEL_RE.search(str(src or ""))
        if m:
            return m.group(0)
    return ""


def match_products_by_model(items, products):
    """为每个链接按型号匹配产品库的真实前台 URL。

    products: db_load_products() 的结果（含 xinghao / front_url）。
    返回每个 item 附上：
      model     : 推断出的型号
      suggest   : 匹配到的前台 URL（建议值，可能为空）
      matched   : 是否命中产品库
    不修改 items 本身，返回新列表（浅拷贝）。
    """
    # Keep the catalogue match separate from the usable front-end URL.  A
    # partially populated product cache is still useful evidence that the
    # model exists; treating it as absent makes the UI say "not found" even
    # though the real problem is that URL discovery did not finish.
    by_model = {}
    for p in (products or []):
        key = _norm_model(p.get("xinghao", ""))
        if key:
            by_model.setdefault(key, []).append(p)
    out = []
    for it in items:
        model = guess_model_from_item(it)
        catalogue_matches = (list(by_model.get(_norm_model(model), []))
                             if model else [])
        candidates = []
        unresolved_ids = []
        for product in catalogue_matches:
            front = str(product.get("front_url", "") or "").strip()
            if front:
                if front not in candidates:
                    candidates.append(front)
            else:
                product_id = str(product.get("id", "") or "").strip()
                if product_id and product_id not in unresolved_ids:
                    unresolved_ids.append(product_id)
        # 同一规范化型号对应多个产品时，静默取数据库第一条会把文章链接
        # 指向错误产品。只有唯一候选才允许自动建议，其余交给用户消歧。
        suggest = candidates[0] if len(candidates) == 1 else ""
        merged = dict(it)
        merged["model"] = model
        merged["suggest"] = suggest
        # ``matched`` answers "does this model exist in the product
        # catalogue?" rather than "does the cache already know its URL?".
        # The latter is exposed separately so callers can safely try a
        # read-only URL resolution or give an accurate recovery message.
        merged["matched"] = bool(catalogue_matches)
        merged["missing_front_url"] = bool(catalogue_matches) and not candidates
        merged["unresolved_product_ids"] = unresolved_ids
        merged["ambiguous"] = len(candidates) > 1
        merged["candidates"] = candidates
        out.append(merged)
    return out


def probe_links(session, links, timeout=12, limit=60, max_workers=5,
                cancel_callback=None, total_timeout=None,
                request_mode="head"):
    """探测本站链接并返回每条链接的状态。

    state 为 ok / dead / unknown / unchecked。网络超时只标 unknown，
    不会被当成死链。重复 URL 只请求一次，但结果仍按输入顺序返回。

    ``max_workers`` 是并发上限（默认 5）。真实 ``requests.Session``
    会为每个探测任务复制一份只读配置和 Cookie，避免跨线程共享 Session；
    其他会话对象会加锁串行调用，测试替身可实现 ``clone_for_probe``
    返回独立会话以启用并发。

    ``cancel_callback`` 是可选的无参回调，返回真时停止等待；
    ``total_timeout`` 是整个批次的可选秒数上限。因取消、总时限或
    ``limit`` 未执行的链接标为 unchecked，绝不会被误判为 dead。
    ``request_mode`` 默认 ``head``（先 HEAD、必要时 GET）；传入 ``get``
    / ``browser`` 时模拟用户点击的 GET 导航，不发 HEAD，适合验证真实
    前台候选 URL，避免 WAF/路由对 HEAD 和浏览器 GET 给出不同结果。
    """
    ordered = []
    unique_urls = []
    known_urls = set()
    for item in (links or []):
        abs_url = item.get("abs") or item.get("href")
        if not abs_url:
            continue
        ordered.append((item, abs_url))
        if abs_url not in known_urls:
            known_urls.add(abs_url)
            unique_urls.append(abs_url)

    if not ordered:
        return []

    try:
        limit_count = max(0, int(limit))
    except (TypeError, ValueError):
        limit_count = 0
    eligible = unique_urls[:limit_count]
    outcomes = {url: ("unchecked", "limit")
                for url in unique_urls[limit_count:]}

    mode = _normalise_request_mode(request_mode)
    started_at = time.monotonic()
    deadline = None
    if total_timeout is not None:
        try:
            deadline = started_at + max(0.0, float(total_timeout))
        except (TypeError, ValueError):
            deadline = started_at

    stop_reason = _probe_stop_reason(cancel_callback, deadline)
    if stop_reason:
        outcomes.update((url, ("unchecked", stop_reason)) for url in eligible)
    elif eligible:
        try:
            worker_count = max(1, min(int(max_workers), len(eligible)))
        except (TypeError, ValueError):
            worker_count = 1
        shared_session_lock = threading.Lock()
        executor = ThreadPoolExecutor(max_workers=worker_count,
                                      thread_name_prefix="link-probe")
        pending = {}
        next_index = 0
        stopped = ""

        def submit_available():
            nonlocal next_index
            while next_index < len(eligible) and len(pending) < worker_count:
                url = eligible[next_index]
                next_index += 1
                future = executor.submit(
                    _probe_task, session, url, timeout, deadline,
                    cancel_callback, shared_session_lock, mode)
                pending[future] = url

        try:
            submit_available()
            while pending:
                stopped = _probe_stop_reason(cancel_callback, deadline)
                if stopped:
                    break
                wait_timeout = None
                if cancel_callback is not None:
                    wait_timeout = 0.05
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    wait_timeout = (remaining if wait_timeout is None
                                    else min(wait_timeout, remaining))
                done, _ = wait(tuple(pending), timeout=wait_timeout,
                               return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    url = pending.pop(future)
                    try:
                        is_dead, code = future.result()
                    except Exception as exc:  # 防止单条异常中断整批体检
                        is_dead, code = False, type(exc).__name__
                    state = _probe_state(is_dead, code)
                    outcomes[url] = (state, code)
                    debug_log(f"[linkcheck] {url} -> {code} ({state})")
                submit_available()
        finally:
            if stopped:
                for future in pending:
                    future.cancel()
                for url in eligible:
                    outcomes.setdefault(url, ("unchecked", stopped))
                # 正在进行的 requests 调用无法被强行终止，但每次请求仍受
                # timeout/deadline 约束；这里不阻塞 UI 等待它们退出。
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)

    results = []
    for item, abs_url in ordered:
        state, code = outcomes.get(abs_url, ("unknown", "not-run"))
        merged = dict(item)
        merged["state"] = state
        merged["status"] = code
        results.append(merged)
    return results


def probe_dead_links(session, links, timeout=12, limit=60, max_workers=5,
                     cancel_callback=None, total_timeout=None,
                     request_mode="head"):
    """对本站链接逐个探测存活性，返回死链列表。

    session: 已登录的 requests.Session（复用会话，避免被 WAF 拦）。
    links:   scan_content_links()["internal"]。
    判定：只有明确的 HTTP 404/410 视为死链。网络异常、
    403/429、临时 5xx 等只表示本次无法确认，不引导用户删链。
    去重：同一 abs 只探测一次。
    """
    return [item for item in probe_links(
            session, links, timeout, limit, max_workers,
            cancel_callback, total_timeout, request_mode)
            if item.get("state") == "dead"]


def _probe_state(is_dead, code):
    if is_dead:
        return "dead"
    if isinstance(code, int):
        return "ok"
    return "unknown"


def _probe_stop_reason(cancel_callback, deadline):
    if cancel_callback is not None:
        try:
            if cancel_callback():
                return "cancelled"
        except Exception as exc:
            # 取消回调只是控制信号；回调自身异常不能把正常链接判死。
            debug_log(f"[linkcheck] 取消回调异常: {type(exc).__name__}")
    if deadline is not None and time.monotonic() >= deadline:
        return "deadline"
    return ""


def _probe_task(source_session, url, timeout, deadline, cancel_callback,
                shared_session_lock, request_mode="head"):
    """为一个 URL 准备线程安全会话，然后执行探测。"""
    owned = False
    probe_session = source_session
    try:
        clone_method = getattr(source_session, "clone_for_probe", None)
        if callable(clone_method):
            probe_session = clone_method()
            owned = probe_session is not source_session
        elif isinstance(source_session, requests.Session):
            probe_session = _clone_requests_session(source_session)
            owned = True
        else:
            # requests.Session 之外的未知对象是否线程安全不可知；加锁是
            # 唯一不改变旧调用方契约的安全方案。
            with shared_session_lock:
                stopped = _probe_stop_reason(cancel_callback, deadline)
                if stopped:
                    return False, stopped
                return _probe_one(source_session, url, timeout, deadline,
                                  cancel_callback, request_mode)
        return _probe_one(probe_session, url, timeout, deadline,
                          cancel_callback, request_mode)
    except Exception as exc:
        return False, type(exc).__name__
    finally:
        if owned:
            try:
                probe_session.close()
            except Exception:
                pass


def _clone_requests_session(source):
    """复制探测所需的 Session 状态，避免跨线程共享可变 CookieJar。"""
    cloned = requests.Session()
    cloned.headers.clear()
    cloned.headers.update(source.headers)
    cloned.cookies.update(source.cookies)
    cloned.auth = source.auth
    cloned.proxies = dict(source.proxies)
    cloned.hooks = {name: list(callbacks)
                    for name, callbacks in source.hooks.items()}
    cloned.params = dict(source.params)
    cloned.verify = source.verify
    cloned.cert = source.cert
    cloned.trust_env = source.trust_env
    cloned.max_redirects = source.max_redirects
    return cloned


class _ProbeDeadline(Exception):
    pass


def _request_timeout(timeout, deadline):
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ProbeDeadline()
    if isinstance(timeout, (int, float)):
        return max(0.05, min(float(timeout), remaining))
    return timeout


def _normalise_request_mode(value):
    return "get" if str(value or "").strip().lower() in {
        "get", "browser", "navigation", "click"} else "head"


def _probe_one(session, url, timeout, deadline=None, cancel_callback=None,
               request_mode="head"):
    """探测单个 URL。先 HEAD，必要时退回 GET。

    返回 ``(是否确认死链, 状态码/原因)``。只有 404/410
    返回 ``True``；可用页返回整数状态码；临时或权限类问题
    返回字符串原因，上层因此将其标为 ``unknown``。
    """
    head_resp = None
    resp = None
    try:
        stopped = _probe_stop_reason(cancel_callback, deadline)
        if stopped:
            return False, stopped
        if _normalise_request_mode(request_mode) == "get":
            # A real browser navigation sends GET immediately.  Keep the
            # same redirect and soft-404 checks as the safe HEAD path, but do
            # not issue a preceding HEAD which some WAFs treat differently.
            resp = _browser_navigation(
                session, "GET", url, timeout, deadline,
                cancel_callback=cancel_callback, stream=True)
            status = int(resp.status_code)
            redirect_issue = _redirect_issue(
                url, getattr(resp, "url", ""), status)
            if redirect_issue:
                return False, redirect_issue
            snippet = _response_snippet(resp)
            if status in (404, 410):
                return True, status
            if 200 <= status < 400:
                body_issue = _soft_404_body_issue(snippet)
                if body_issue:
                    return False, body_issue
                return False, status
            return False, f"HTTP {status}"
        head_resp = _browser_navigation(
            session, "HEAD", url, timeout, deadline,
            cancel_callback=cancel_callback)
        resp = head_resp
        status = int(resp.status_code)

        redirect_issue = _redirect_issue(
            url, getattr(resp, "url", ""), status)
        if redirect_issue:
            return False, redirect_issue

        # 不少站点/WAF 对 HEAD 统一返回 404/403/405，而正常 GET 可访问。
        # 所有非成功 HEAD 都必须用 GET 复核后才能确认死链。
        snippet = _response_snippet(resp)
        needs_get = status >= 400 or (
            status == 200 and not snippet and
            _should_fetch_html_body(session, resp))
        if needs_get:
            stopped = _probe_stop_reason(cancel_callback, deadline)
            if stopped:
                return False, stopped
            resp = _browser_navigation(
                session, "GET", url, timeout, deadline,
                cancel_callback=cancel_callback, stream=True)
            snippet = _response_snippet(resp)
        status = int(resp.status_code)

        redirect_issue = _redirect_issue(
            url, getattr(resp, "url", ""), status)
        if redirect_issue:
            return False, redirect_issue
        if status in (404, 410):
            return True, status
        if 200 <= status < 400:
            body_issue = _soft_404_body_issue(snippet)
            if body_issue:
                return False, body_issue
            return False, status
        # 400/401/403/408/429/5xx 等不能证明页面不存在。
        return False, f"HTTP {status}"
    except _ProbeDeadline:
        return False, "deadline"
    except Exception as exc:
        # 超时、DNS/临时断网只说明“本次无法确认”，不能据此让用户删除链接。
        # 历史日志中确有 ConnectTimeout 被当成死链的记录，存在误删正常链接风险。
        return (False, type(exc).__name__)
    finally:
        for response in (resp, head_resp):
            if response is None:
                continue
            try:
                response.close()
            except Exception:
                pass


def _should_fetch_html_body(session, response):
    """生产 Session 对 HTML 再 GET 少量正文，用于识别软 404。"""
    headers = getattr(response, "headers", None) or {}
    content_type = str(headers.get("Content-Type", "")).lower()
    if content_type and "html" not in content_type and "xhtml" not in content_type:
        return False
    # 旧测试替身通常只有 head()，不能因新增软 404 检查破坏兼容；
    # 带明确 HTML Content-Type 的替身仍可覆盖 GET 分支。
    return isinstance(session, requests.Session) or bool(content_type)


def _response_snippet(response, max_bytes=65536):
    """读取至多一小段响应正文；失败时宁可不判断，也不误判。"""
    if response is None:
        return ""
    if isinstance(response, requests.Response):
        try:
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                remaining = max_bytes - size
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
                if size >= max_bytes:
                    break
            raw = b"".join(chunks)
            return raw.decode(response.encoding or "utf-8", errors="replace")
        except Exception:
            return ""
    try:
        value = getattr(response, "text", "") or ""
        return str(value)[:max_bytes]
    except Exception:
        return ""


_ERROR_TITLE_RE = re.compile(
    r"(?:^\s*(?:error\s*)?(?:404|410)\b|"
    r"\b(?:404|410)\b.{0,40}\b(?:error|not\s+found|page)\b|"
    r"\bpage\s+not\s+found\b|页面(?:不|未)存在|找不到(?:该|此)?页面|页面未找到)",
    re.IGNORECASE)


def _soft_404_body_issue(html):
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if title and _ERROR_TITLE_RE.search(title):
            return "soft-404-title"
        for meta in soup.find_all("meta"):
            name = str(meta.get("name", "")).strip().lower()
            content = str(meta.get("content", "")).strip().lower()
            if name in ("robots", "googlebot", "bingbot") \
                    and "noindex" in content:
                return "soft-404-noindex"
    except Exception:
        return ""
    return ""


def _redirect_issue(original_url, final_url, status):
    """返回无法把重定向当作“正确内链”的保守原因。

    - 跳到另一域名：可能是 WAF/托管页，也可能是站点迁移，
      两者都不应直接判死链或正确链。
    - 非根路径跳到本站首页：这是 PbootCMS 软 404 的常见形式。
    - 跳到 /404.html、/not-found、/error/404 等错误页，或明确的
      登录路径：只标记不确定，绝不当成可用链接或确认死链。

    返空字符串表示没有发现这两类明确的不确定跳转。
    """
    if not final_url or not (200 <= int(status) < 400):
        return ""
    original = urlparse(str(original_url or ""))
    final = urlparse(str(final_url or ""))
    if original.hostname and final.hostname \
            and not _same_site(original.hostname, final.hostname):
        return "redirect-cross-site"
    original_path = (original.path or "/").rstrip("/") or "/"
    final_path = (final.path or "/").rstrip("/") or "/"
    if original_path != "/" and final_path == "/":
        return "redirect-root"
    if _looks_like_error_path(final_path):
        return "soft-404-path"
    if original_path != final_path and _looks_like_login_path(final_path):
        return "redirect-login"
    return ""


def _looks_like_error_path(path):
    segments = [unquote(part).strip().lower()
                for part in str(path or "").split("/") if part.strip()]
    for segment in segments:
        stem = re.sub(r"\.(?:s?html?|php|aspx?)$", "", segment)
        if stem in {"404", "410", "error", "not-found", "not_found",
                    "notfound", "page-not-found", "page_not_found"}:
            return True
        if re.fullmatch(r"(?:error[-_]?|http[-_]?)?(?:404|410)", stem):
            return True
    return False


def _looks_like_login_path(path):
    lowered = unquote(str(path or "")).lower()
    return bool(re.search(
        r"(?:^|/)(?:login|log-in|signin|sign-in|auth)"
        r"(?:\.(?:s?html?|php|aspx?))?(?:/|$)", lowered))


def strip_links(html, dead_items):
    """把死链的 <a> 标签替换为其锚文字（保留文本，去掉链接）。

    只处理 dead_items 里出现过的 href，其余 <a> 原样保留。
    返回改写后的 HTML 字符串。
    """
    if not html or not dead_items:
        return html
    result, _, _ = apply_link_actions(html, [dict(href=it.get('href'), new_href='')
                                           for it in dead_items if it.get('href')])
    return result


def apply_link_actions(html, actions):
    """按每条死链的处理方式改写正文。

    actions: [{"href": 原链接, "new_href": 新链接}]
      - new_href 非空  → 把该 href 替换为新链接（保留锚文字）
      - new_href 为空  → 移除 <a> 壳，只留锚文字

    新调用方可额外传 ``index``（HTML 中所有带 href 的 a 标签的
    从 0 开始索引）或 ``occurrence``（同 href 第几次出现）。
    兼容旧格式：同 href 只有一条无索引动作时仍作用于全部；
    同 href 有多条无索引动作时，按 HTML 出现顺序逐条消费。
    未在 actions 中的 <a> 一律不动。返回 (新HTML, 替换数, 移除数)。
    """
    if not html or not actions:
        return html, 0, 0
    indexed_plan = {}
    occurrence_plan = {}
    ordered_plan = {}
    for action in actions:
        if not isinstance(action, dict):
            continue
        href = (action.get("href") or "").strip()
        if not href:
            continue
        new_href = (action.get("new_href") or "").strip()
        index = _action_index(action.get("index"))
        occurrence = _action_index(action.get("occurrence"))
        if index is not None:
            indexed_plan[(href, index)] = new_href
        elif occurrence is not None:
            occurrence_plan[(href, occurrence)] = new_href
        else:
            ordered_plan.setdefault(href, []).append(new_href)

    from html_fragments import HTMLFragments, rewrite_attributes
    fragments = HTMLFragments(html)
    changes = []
    replaced = removed = 0
    occurrence_by_href = {}
    anchors = [n for n in fragments.find('a') if 'href' in n['attrs']]
    for link_index, a in enumerate(anchors):
        cur = (a['attrs'].get("href") or "").strip()
        occurrence = occurrence_by_href.get(cur, 0)
        occurrence_by_href[cur] = occurrence + 1

        marker = object()
        new_href = indexed_plan.get((cur, link_index), marker)
        if new_href is marker:
            new_href = occurrence_plan.get((cur, occurrence), marker)
        if new_href is marker:
            choices = ordered_plan.get(cur, [])
            if len(choices) == 1:
                # 旧语义：一条 href 动作处理所有同 href 标签。
                new_href = choices[0]
            elif occurrence < len(choices):
                # 现有前端每个问题链接产生一条无索引动作；
                # 按出现顺序一一对应，不再“最后一条覆盖全部”。
                new_href = choices[occurrence]
        if new_href is marker:
            continue
        if new_href:
            tag = html[a['start']:a['open']]
            changes.append((a['start'], a['open'], rewrite_attributes(tag, {'href': new_href})))
            replaced += 1
        else:
            changes.append((a['start'], a['open'], ''))
            changes.append((a['close'], a['end'], ''))
            removed += 1
    for start, end, value in sorted(changes, reverse=True):
        html = html[:start] + value + html[end:]
    debug_log(f"[linkcheck] 死链处理：替换 {replaced} 处、移除 {removed} 处")
    return html, replaced, removed


def _action_index(value):
    """把动作索引收敛为非负整数；非法/缺失值表示旧格式动作。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
