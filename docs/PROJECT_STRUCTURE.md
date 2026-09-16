# 项目结构

本文档说明 v3.4.10 发布仓库中每个文件的职责。开发测试、本地会话、配置、草稿、日志、数据库和构建产物不属于发布源码树。

## 根目录与构建

| 文件 | 职责 |
| --- | --- |
| `.gitignore` | 排除会话、配置、日志、数据库、草稿、构建产物和开发测试文件。 |
| `LICENSE` | 项目的 MIT 开源许可证。 |
| `README.md` | 项目定位、功能、下载、快速开始、构建和数据安全说明。 |
| `app.py` | pywebview 应用入口、多站点状态和 Python/JavaScript API 桥接。 |
| `app_meta.py` | 应用名称、版本号、构建日期和版本记录的唯一数据源。 |
| `config.py` | 本地配置读写、迁移、默认值和并发保护。 |
| `constants.py` | HTTP 超时、请求头等全局常量。 |
| `exceptions.py` | 应用统一异常类。 |
| `requirements.txt` | Python 运行依赖和版本范围。 |
| `build_windows_exe.bat` | Windows 一键测试、打包和可选签名流程。 |
| `build_support.py` | 构建元数据、可选 Authenticode 签名和签名校验。 |
| `PbootCMS_Publisher_W_v3.4.10.spec` | v3.4.10 的 PyInstaller 打包配置。 |
| `update_service.py` | 只读版本检查、本地升级前备份和发布元数据验证。 |

## PbootCMS 客户端与后台功能

| 文件 | 职责 |
| --- | --- |
| `pboot_client.py` | PbootCMS HTTP 客户端主体、会话隔离和各功能 Mixin 整合。 |
| `client_auth.py` | 登录、验证码、Cookie 会话和登录态处理。 |
| `client_categories.py` | 栏目树读取、栏目表单发现和安全管理。 |
| `client_content.py` | 文章列表、表单、发布、编辑、上传和回读验证。 |
| `client_messages.py` | 留言列表、详情、搜索、导出和回复。 |
| `client_products.py` | 产品列表、详情、同步、编辑和批量处理。 |
| `client_slides.py` | 网站轮播的发现、新增、修改、删除和回读验证。 |
| `client_utils.py` | 路径、登录页识别等无网络依赖的客户端工具。 |
| `admin_modules.py` | 安全发现并编辑未被专用模块覆盖的后台功能。 |
| `content_admin.py` | 文章列表复制、移动、排序、状态和删除操作。 |
| `single_admin.py` | PbootCMS 单页内容的独立查询、表单发现、保存和验证。 |
| `backend_diagnostic.py` | 只读、脱敏的后台结构诊断和导出。 |
| `form_controls.py` | 文章、产品和栏目共用的浏览器表单控件模型。 |
| `field_mapping.py` | HTML 字段与 CMS 字段的匹配、映射持久化和提交值清理。 |
| `save_verification.py` | 提交后业务字段的严格回读对比。 |
| `request.py` | 基础网络请求、超时和日志封装。 |
| `http_transport.py` | 同源、有界的 HTTP 重定向处理。 |
| `upload_policy.py` | 静态解析 Layui/UEditor 上传配置，不执行站点 JavaScript。 |
| `upload_protocol.py` | 严格解析上传响应并判定真实成功结果。 |

## 内容、媒体与任务

| 文件 | 职责 |
| --- | --- |
| `seo_parser.py` | 解析 SEO 表格、`article`、通用 HTML 和可安全保留的样式。 |
| `html_fragments.py` | 定位 HTML 源片段，尽量不改写属性、实体和 CSS。 |
| `html_images.py` | 扫描、校验、上传映射并改写 HTML 中的本地正文图片。 |
| `html_media.py` | 发现并改写 HTML 中的本地视频、音频和附件地址。 |
| `asset_types.py` | 通过受限文件头检测图片、PDF、视频等资源类型。 |
| `file_metadata.py` | 在 WebView 桥接中临时保留经验证的 MIME 提示。 |
| `first_thumbnail.py` | 只读获取并验证已有的同源静态首图。 |
| `thumbnail_intent.py` | 区分缩略图保留、替换、清空等明确操作意图。 |
| `gallery_plan.py` | 管理有序的图集图片/标题对，区分空图集和未修改。 |
| `content_draft.py` | 发布和编辑流程共用的纯内容草稿转换。 |
| `draft_store.py` | 本地可恢复草稿的版本化、脱敏存储。 |
| `bulk_links.py` | 批量预览、替换和恢复产品型号内链。 |
| `link_check.py` | 检查正文站内链接、识别死链并尝试匹配产品地址。 |
| `webtasks.py` | 基于线程的登录、发布、编辑、媒体上传和同步任务。 |
| `product_repository.py` | 按站点隔离的 SQLite 产品缓存和同步元数据。 |

## 留言、记录与安全存储

| 文件 | 职责 |
| --- | --- |
| `message_fields.py` | 无损提取留言字段，生成不执行 HTML 的可读投影。 |
| `message_pagination.py` | 留言列表的只读路由、翻页和数量证据。 |
| `message_reply.py` | 发现、编辑并验证真实留言回复表单。 |
| `message_routes.py` | 把留言路由严格绑定到当前后台入口和同源范围。 |
| `message_state.py` | 分离留言显示状态和处理状态证据。 |
| `audit_log.py` | 按站点隔离、仅保留脱敏摘要的本地操作审计。 |
| `operation_journal.py` | 记录结果未知的写请求，避免重启后意外重试。 |
| `logger.py` | 统一、脱敏的本地运行日志。 |
| `secure_store.py` | 使用 Windows DPAPI 按系统登录账户加密本地密码和会话数据。 |

## 界面、文档与示例

| 文件 | 职责 |
| --- | --- |
| `webui/index.html` | 应用主界面、功能区和对话框结构。 |
| `webui/app.js` | 前端状态管理、交互、校验和 pywebview API 调用。 |
| `webui/style.css` | 多站点内容工作台的布局、响应式和视觉样式。 |
| `examples/example_article.html` | 可复制的 SEO 字段、正文和相对图片路径示例。 |
| `docs/HTML_GUIDE.md` | HTML 输入格式、字段契约、媒体目录和安全边界。 |
| `docs/PROJECT_STRUCTURE.md` | 发布源码树中每个文件的职责索引。 |
