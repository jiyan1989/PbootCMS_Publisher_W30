"""Shared client helpers with no network-service dependencies."""
import os
from pathlib import Path
import re
import sys

def get_base_dir():
    override = os.environ.get("PBOOT_PUBLISHER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

def _is_login_page(html):
    """判断后台返回的 HTML 是否为“未登录/登录页”。

    ⚠️ 关键：只用【登录页独有】的特征，绝不能把已登录的后台列表页误判为登录页。
    后台列表/编辑页普遍包含 `formcheck` 等 CSRF 令牌，以及“登录”二字，
    若用它们当判定依据，会把已认证的产品列表页误判成登录页 → 误报“登录已失效”。

    登录页独有强特征（任一成立即判定为登录页）：
      1) 含【密码输入框】(type="password") —— 后台列表/编辑/内容页均不含密码框，
         这是最可靠、与站点字段命名无关的特征（example 的登录页用户名框不叫 username、
         验证码字段叫 code 而非 checkcode��靠密码框兜底识别）；
      2) 含经典验证码字段 name="checkcode"（部分站点为 code，故不作为唯一依据）；
      3) 表单 action / 链接指向登录接口（/Index/login，排除 /Index/loginOut）。
    """
    if not html:
        return False
    # 1) 密码框（最可靠）：兼容大小写与单/双引号写法
    if re.search(r'type\s*=\s*["\']password["\']', html, re.I):
        return True
    # 2) 经典验证码字段名
    if "name=\"checkcode\"" in html or "name='checkcode'" in html:
        return True
    # 3) 登录接口链接/表单 action（排除 /Index/loginOut 退出登录）
    if re.search(r"/Index/login(?!Out)", html):
        return True
    return False
