# -*- coding: utf-8 -*-
"""PbootCMS 发布工具 — 全局常量"""

# ── HTTP 请求超时（秒）──
TIMEOUT_SHORT  = 15   # 简单页面请求（验证码、快速检查）
TIMEOUT_NORMAL = 25   # 一般 GET/POST 请求
TIMEOUT_LONG   = 30   # 慢响应服务器
TIMEOUT_UPLOAD = 60   # 图片上传

# ── mcode 内容模型探测顺序 ──
MCODE_ORDER = ("3", "2", "4", "5", "6", "7")

# ── PbootCMS 表单字段名 ──
FIELD_TITLE       = "title"
FIELD_CONTENT     = "content"
FIELD_SCODE       = "scode"
FIELD_SUBSCODE    = "subscode"
FIELD_FORMCHECK   = "formcheck"
FIELD_XINGHAO     = "ext_xinghao"
FIELD_JIAGE       = "ext_jiage"
FIELD_PICS        = "pics"
FIELD_ICO         = "ico"
FIELD_TAGS        = "tags"
FIELD_DATE        = "date"
FIELD_SORTING     = "sorting"
FIELD_AUTHOR      = "author"
FIELD_KEYWORDS    = "keywords"
FIELD_DESCRIPTION = "description"

# ── HTTP 常见 User-Agent ──
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── 默认请求头 ──
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ── 请求重试 ──
RETRY_COUNT = 2          # 默认重试次数
RETRY_DELAY = 1.5        # 重试间隔（秒）

# ── 图片压缩参数 ──
IMAGE_MAX_SIZE   = 1200  # 最大边长（像素）
IMAGE_QUALITY    = 85    # JPEG 质量
IMAGE_MAX_FILE   = 1 * 1024 * 1024  # 超过 1MB 才压缩
