# -*- coding: utf-8 -*-
"""PbootCMS 发布工具 — 异常类"""


class PbootCMSError(Exception):
    """PbootCMS 所有异常的基类"""
    pass


class NetworkError(PbootCMSError):
    """网络请求错误（超时、连接拒绝、SSL 错误）"""
    pass


class Cancelled(PbootCMSError):
    """用户在后台任务真正写入前请求取消。"""
    pass


class UploadOutcomeUnknown(RuntimeError):
    """Upload may already be committed; do not blindly retry the write."""
