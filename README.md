# PbootCMS Publisher

PbootCMS Publisher 是一款 Windows 桌面端内容发布与后台管理工具。应用通过目标站点的实际后台表单和上传配置执行操作，支持 HTML 稿件解析、字段映射、媒体上传、批量发布和常用后台管理。

| 项目 | 信息 |
| --- | --- |
| 当前版本 | v3.4.10 |
| 支持系统 | Windows 10 / Windows 11 |
| 运行方式 | 独立 EXE 或 Python 源码 |
| 源码环境 | Python 3.12（推荐） |

## 主要功能

- 多站点标签与独立 Cookie 会话。
- SEO HTML 、通用 HTML 和批量 HTML 文件导入。
- 标题、副标题、URL 名称、描述、标签、关键词和正文字段映射。
- 本地正文图片、视频、音频、附件、缩略图和文章图集处理。
- 文章发布、编辑、查询、站内链检查和产品型号内链更新。
- 栏目、网站轮播、单页、留言和可发现后台模块管理。
- 发布前检查、表单版本校验、写入后回读与结果未知保护。
- Windows DPAPI 本地凭据加密、站点隔离草稿和脱敏操作日志。

## 下载

Windows 独立版位于 [GitHub Releases](https://github.com/jiyan1989/PbootCMS_Publisher_W30/releases/tag/v3.4.10)，无需安装 Python。

| 文件 | 大小 | SHA-256 |
| --- | ---: | --- |
| `PbootCMS_Publisher_W_v3.4.10.exe` | 21,803,824 字节 | `206CF7E8E596EEC020D6342F2D2773FE96130A305011CFA9B1F777E3BA12E675` |

PowerShell 校验命令：

```powershell
Get-FileHash .\PbootCMS_Publisher_W_v3.4.10.exe -Algorithm SHA256
```

> 当前 EXE 未配置 Authenticode 代码签名，Windows 可能显示 SmartScreen 提示。文件完整性以上述 SHA-256 为准。

## 快速开始

1. 运行 `PbootCMS_Publisher_W_v3.4.10.exe`，创建站点标签。
2. 填写目标 PbootCMS 后台入口和登录凭据。验证码、SSO 或动态脚本登录可通过“原生网页登录”完成。
3. 在“内容发布”中导入 `.html` 或 `.htm` 稿件。
4. 选择发布栏目，核对媒体设置和字段映射。
5. 执行“发布前检查”，确认结果后执行发布。

## 网络与登录

| 功能 | 说明 |
| --- | --- |
| 后台入口 | 支持完整 HTTP(S) 地址，未填写协议时默认按 HTTPS 处理。 |
| 直连 | 应用直接连接目标后台，为默认模式。 |
| 系统代理 | 使用 Windows 当前系统代理配置。 |
| 自定义代理 | 支持不带账号信息的 HTTP(S) 代理地址。 |
| 普通登录 | 通过 PbootCMS 登录表单、验证码和会话 Cookie 完成认证。 |
| 原生网页登录 | 用于 SSO、动态验证码或依赖网页脚本的登录流程。 |
| 同步网页登录 | 将原生网页的同源 CookieStore 同步到当前站点会话。 |

## 核心工作流程

### 内容发布

1. 导入单个 HTML、多个 HTML 或稿件文件夹。
2. 读取目标栏目的实际新增内容表单。
3. 解析 HTML 字段、正文图片和其他本地媒体。
4. 完成字段映射、缩略图、图集和发布属性设置。
5. 通过发布前检查后提交，并根据后台回读判定结果。

### 文章编辑

1. 选择栏目并读取文章列表。
2. 选择目标文章，加载当前后台表单和字段值。
3. 选择性导入新 HTML，或仅修改现有字段、图片和状态。
4. 保留未显式修改的后台字段，并在写入前检查表单修订版本。
5. 写入后重新读取目标文章，对比提交字段与最终值。

### 批量发布

- 同一队列可复用栏目、媒体策略和发布属性。
- 每篇稿件独立执行解析、校验、上传和结果回读。
- 暂停在当前任务完成后生效，失败项可根据运行结果单独处理。

### 后台管理

工作区同时提供文章列表操作、栏目管理、网站轮播、单页管理、留言回复和可发现后台模块。写操作以目标站点实际页面中发现的路由、表单和提交控件为准。

## HTML 与本地图片

HTML 文件与相对路径图片可按以下结构组织：

```text
article-folder/
├─ article.html
└─ images/
   ├─ cover.jpg
   └─ detail-01.png
```

```html
<article>
  <h2>正文小标题</h2>
  <p>正文内容。</p>
  <img src="images/detail-01.png" alt="图片说明">
</article>
```

相对路径以 HTML 文件所在目录为基准解析。发布时，本地媒体按目标后台的实际上传配置处理，正文路径随上传结果替换。

完整的字段契约、兼容格式、图片规则和安全边界见 [HTML 稿件指南](docs/HTML_GUIDE.md)。可编辑模板见 [`examples/example_article.html`](examples/example_article.html)。

## 从源码运行

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## 构建 Windows EXE

```powershell
.\build_windows_exe.bat
```

构建脚本会创建独立 Python 环境、安装固定范围依赖并生成当前版本 EXE。开发工作区存在回归测试时会先执行测试；发行源码包不包含测试文件，构建流程会自动跳过该步骤。

## 文档

- [HTML 稿件指南](docs/HTML_GUIDE.md)：HTML 结构、SEO 字段、媒体目录和批量导入。
- [项目结构](docs/PROJECT_STRUCTURE.md)：发布仓库中每个文件的职责。
- [HTML 示例](examples/example_article.html)：可直接复制和修改的中性模板。
- [v3.4.10 Release](https://github.com/jiyan1989/PbootCMS_Publisher_W30/releases/tag/v3.4.10)：Windows 可执行文件、版本说明和完整性校验。

## 数据安全

发布仓库仅包含运行源码、静态界面、构建配置、文档和中性示例。以下运行时数据已从版本控制中排除：

- `publisher_config.json`
- `site_sessions/`
- `drafts/`
- `backend_diagnostics/`
- `*.db`、`*.sqlite*`、`*.log*`
- `.env*`、证书、私钥和令牌文件

发布内容不包含真实站点地址、账号密码、Cookie、会话、草稿、日志或业务数据库。

### 运行时数据

| 路径 | 内容 | 保护方式 |
| --- | --- | --- |
| `publisher_config.json` | 应用设置、站点历史和本地登录信息 | 密码使用 Windows DPAPI 加密 |
| `site_sessions/` | 按站点隔离的会话数据 | 绑定当前 Windows 登录账户 |
| `drafts/` | 可恢复的工作流草稿 | 不保存解析后全量正文和明文凭据 |
| `products_cache.db` | 按站点隔离的产品缓存 | 本地 SQLite，不进入版本控制 |
| `operation_audit.db` | 脱敏操作摘要 | 不记录密码、令牌和完整正文 |
| `operation_journal.db` | 结果未知的写请求状态 | 仅保留哈希和短摘要 |
| `backend_diagnostics/` | 只读后台结构诊断 | 生成时脱敏，不进入版本控制 |
| `*.log*` | 本地运行记录 | 日志层执行敏感字段脱敏 |

默认数据目录为程序运行目录。环境变量 `PBOOT_PUBLISHER_DATA_DIR` 可将运行时数据指向独立目录。

## 常见问题

### 后台地址无法登录

- 后台入口应指向实际登录页，包括二次开发后的入口文件名。
- 反向代理、WAF 或系统代理可能改变登录路由，网络模式应与正常浏览器访问环境保持一致。
- 依赖网页脚本的登录应使用“原生网页登录”和“同步网页登录”。

### HTML 未识别标题或正文

- 推荐使用 `#seo-metadata` 与 `<article>` 结构。
- 通用 HTML 应包含 `<title>` 或 `<h1>`，正文应位于 `<article>` 或 `<body>` 中。
- 解析结果以字段映射表为准，标题和正文是内容发布的必要来源字段。

### 本地图片显示为缺失

- 相对路径以 HTML 文件所在目录为基准，例如 `images/detail-01.png`。
- `/upload/a.jpg` 属于站点根路径，不代表本地文件。
- 文件类型、大小和后台 `accept` 限制会在上传前校验。

### 后台表单需要网页脚本

动态生成字段或上传配置无法安全静态复现时，应用会停止猜测式写入，并提供认证后的原生网页入口。

### 操作结果显示“未知”

“未知”表示写请求可能已到达服务器，但回读未能证明最终结果。该状态会进入操作日志和持久化日志，后续处理应先核对后台实际状态，避免盲目重试。

## 兼容性与使用边界

- 仅适用于已获管理授权的 PbootCMS 站点。
- 不同 PbootCMS 版本、主题和二次开发后台可能存在表单或上传差异。
- 首次连接新后台时，建议仅发布一篇测试内容并核对前台结果。
- 发布、修改或批量操作前，建议完成站点数据备份。

## 问题反馈

功能问题和可复现的缺陷可通过仓库 [Issues](https://github.com/jiyan1989/PbootCMS_Publisher_W30/issues) 记录。问题描述中应移除站点地址、账号密码、Cookie、会话文件和其他敏感数据。

## 许可证

本项目采用 [MIT License](LICENSE)。
