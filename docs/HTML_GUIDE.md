# HTML 稿件指南

本文档定义 PbootCMS Publisher 支持的 HTML 输入格式、字段映射、本地媒体目录和安全边界。

## 文件与编码

- 文件扩展名：`.html` 或 `.htm`。
- 推荐编码：UTF-8。
- 兼容编码：文档已声明的编码、UTF-8 BOM、UTF-16 和 GB18030。
- 无法识别的字节编码会在解析阶段停止，不会进入发布流程。

## 推荐目录结构

```text
article-folder/
├─ article.html
└─ images/
   ├─ cover.jpg
   ├─ detail-01.png
   └─ detail-02.png
```

HTML 中的本地图片使用相对路径：

```html
<img src="images/detail-01.png" alt="图片说明">
```

相对路径以 HTML 文件所在目录为基准解析。以 `/` 开头的路径属于站点根路径，不作为本地文件读取。

## 推荐 SEO 格式

推荐格式由 `#seo-metadata` 表格和 `<article>` 正文组成：

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>示例文章</title>
</head>
<body>
  <div id="seo-metadata">
    <table>
      <tr><td>Title</td><td>示例文章</td></tr>
      <tr><td>Subtitle</td><td>示例副标题</td></tr>
      <tr><td>URL Slug</td><td>example-article</td></tr>
      <tr><td>Meta Description</td><td>示例摘要。</td></tr>
      <tr><td>Tags</td><td>示例,教程</td></tr>
      <tr><td>Target Keyword</td><td>示例关键词</td></tr>
      <tr><td>Image ALT</td><td>图片说明</td></tr>
    </table>
  </div>

  <article>
    <h2>正文小标题</h2>
    <p>正文内容。</p>
    <img src="images/detail-01.png" alt="图片说明">
  </article>
</body>
</html>
```

### SEO 表格字段

| HTML 字段 | 内部字段 | 用途 |
| --- | --- | --- |
| `Title` | `title` | 文章标题 |
| `Subtitle` | `subtitle` | 副标题 |
| `URL Slug` 或 `URL` | `filename` | URL 名称 |
| `Meta Description` 或 `SEO Description` | `description` | 页面描述 |
| `Tags` | `tags` | 文章标签 |
| `Target Keyword` | `keywords` | 关键词 |
| `Image ALT` | `image_alt` | 图片 ALT 文本 |
| `<article>` | `content` | 正文 HTML |

CMS 字段以目标后台实际表单为准。解析后的字段会先进入映射界面，发布前可核对目标字段和最终值。

## 通用 HTML 格式

未包含 `#seo-metadata` 表格时，解析顺序如下：

1. `<title>` 或首个 `<h1>` 作为标题。
2. `<article>`、`<body>` 或整份文档作为正文。
3. `<meta name="description">` 和 `<meta name="keywords">` 分别作为描述和关键词。

兼容的旧格式使用 `div.section > h2 + section` 表示各字段，新稿件应优先使用 SEO 表格与 `<article>` 格式。

## 样式处理

- 安全 CSS 属性会转换为正文内联样式。
- `javascript:`、`expression()` 和 CSS `url()` 不会进入样式映射。
- `<style>` 和外部样式链接不会作为正文节点提交。
- 非锚点用途的 `class` 和 `id` 会被移除，避免与站点主题冲突。
- 正文 `<h1>` 会被移除，PbootCMS 详情页通常由文章标题输出页面 H1。

## 媒体处理

### 本地资源

- 相对路径和本地绝对路径在发布前扫描。
- 文件存在性、类型、大小和目标表单 `accept` 规则在上传前校验。
- 上传成功后，`src`、`srcset`、常见懒加载属性和 CSS 图片引用会按原位置替换。
- 视频、音频和附件使用后台对应字段的真实上传配置。

### 远程资源

外链图片的处理方式由目标后台编辑器配置决定。编辑器提供安全的远程图片抓取能力时可执行抓取；未提供时保留原地址并记录处理结果。

## 批量导入

“选择多个 HTML”和“导入文件夹”可将多份稿件加入发布队列。栏目和发布设置可复用，每篇稿件仍会独立执行解析、媒体扫描和发布前校验。

## 安全边界

- HTML 文件不得包含后台地址、账号密码、Cookie、会话标识或 API 密钥。
- HTML 预览使用受限 iframe，禁止脚本、表单提交和跨站写入。
- 不支持的动态上传策略会在发送文件前停止，不猜测上传地址。
- 目标站点主题 CSS 和服务端再处理可能改变最终展示，发布后结果以站点前台为准。
