# -*- coding: utf-8 -*-
"""PbootCMS 发布工具 — 统一网络请求封装"""

import time
import requests

from logger import debug_log
from constants import (
    TIMEOUT_NORMAL,
    RETRY_COUNT,
    RETRY_DELAY,
)
from exceptions import NetworkError
from http_transport import request_with_redirects


def http_request(session, method: str, url: str,
                 retries: int = None,
                 timeout: int = None,
                 delay: float = None,
                 read_only: bool = False,
                 **kwargs):
    """
    统一HTTP请求：默认不重发；仅明确声明纯读取的GET/HEAD可重试。

    参数
    ----
    session : requests.Session
    method : "GET" | "POST"
    url : str
    retries : 默认重试次数
    timeout : 超时秒数
    delay : 重试间隔（秒）
    **kwargs : 传递给 session.get/post 的额外参数

    返回
    ----
    requests.Response

    异常
    ----
    NetworkError : 所有重试耗尽后抛出
    """
    method = method.upper()
    _retries = max(0, int(retries if retries is not None else RETRY_COUNT)) \
        if read_only and method in ('GET', 'HEAD') else 0
    _timeout = timeout if timeout is not None else TIMEOUT_NORMAL
    _delay   = delay   if delay   is not None else RETRY_DELAY

    last_err = None
    for attempt in range(_retries + 1):
        try:
            resp = request_with_redirects(session, method, url, timeout=_timeout, **kwargs)
            return resp
        except requests.exceptions.SSLError as e:
            raise NetworkError('TLS证书校验失败，未自动重试') from e
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            debug_log(f"[request] {method} attempt={attempt+1}/{_retries+1}: {type(e).__name__}")
            if attempt < _retries:
                time.sleep(_delay * (attempt + 1))

    raise NetworkError(f"{method}请求未取得响应（共{_retries+1}次）；"
                       + ("读取未完成" if read_only else "结果未知，未自动重发，请先核对后台")) from last_err


def quick_get(session, url: str, **kwargs):
    """快速 GET（timeout=15，不重试）"""
    return http_request(session, "GET", url, retries=0, timeout=15, **kwargs)
