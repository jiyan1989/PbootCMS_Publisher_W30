# PbootCMS Publisher

[English](README.en.md) | [简体中文](README.md)

PbootCMS Publisher is a Windows desktop client for PbootCMS content publishing and administration. Operations are based on the target site's actual backend forms and upload configuration. The application supports HTML manuscript parsing, field mapping, media uploads, batch publishing, and common administration workflows.

| Item | Details |
| --- | --- |
| Current version | v3.4.10 |
| Supported systems | Windows 10 / Windows 11 |
| Runtime options | Standalone EXE or Python source |
| Source environment | Python 3.12 (recommended) |

## Features

- Site tabs with independent cookie sessions.
- SEO HTML, general HTML, and batch HTML import.
- Mapping for titles, subtitles, URL names, descriptions, tags, keywords, and body content.
- Local body images, video, audio, attachments, thumbnails, and article galleries.
- Article publishing, editing, querying, internal-link checks, and product-model link updates.
- Category, site carousel, single-page, message, and discoverable administration modules.
- Preflight checks, form-version validation, post-write readback, and unknown-result protection.
- Windows DPAPI credential encryption, site-isolated drafts, and redacted operation logs.

## Download

The standalone Windows build is available from [GitHub Releases](https://github.com/jiyan1989/PbootCMS_Publisher_W30/releases/tag/v3.4.10). Python is not required for the EXE build.

| File | Size | SHA-256 |
| --- | ---: | --- |
| `PbootCMS_Publisher_W_v3.4.10.exe` | 21,803,824 bytes | `206CF7E8E596EEC020D6342F2D2773FE96130A305011CFA9B1F777E3BA12E675` |

PowerShell verification:

```powershell
Get-FileHash .\PbootCMS_Publisher_W_v3.4.10.exe -Algorithm SHA256
```

> The EXE does not currently have an Authenticode signature. Windows may display a SmartScreen notice. The SHA-256 value above is the release integrity reference.

## Quick Start

1. Launch `PbootCMS_Publisher_W_v3.4.10.exe` and create a site tab.
2. Enter the target PbootCMS backend entry point and login credentials. Native Web Login supports CAPTCHA, SSO, and JavaScript-dependent login flows.
3. Import an `.html` or `.htm` manuscript in **Content Publishing**.
4. Select the publishing category and review media settings and field mappings.
5. Run **Preflight Check**, review the result, and start publishing.

## Network and Login

| Function | Behavior |
| --- | --- |
| Backend entry point | Full HTTP(S) addresses are supported. Missing schemes default to HTTPS. |
| Direct connection | Direct access to the target backend; the default mode. |
| System proxy | Uses the current Windows system proxy configuration. |
| Custom proxy | Accepts HTTP(S) proxy addresses without embedded credentials. |
| Standard login | Authenticates through the PbootCMS login form, CAPTCHA flow, and session cookies. |
| Native Web Login | Handles SSO, dynamic CAPTCHA, or JavaScript-dependent login flows. |
| Web Login synchronization | Copies same-origin cookies from the native web view into the active site session. |

## Core Workflows

### Content Publishing

1. Import one HTML file, multiple HTML files, or a manuscript folder.
2. Read the target category's actual content-creation form.
3. Parse HTML fields, body images, and other local media.
4. Configure field mappings, thumbnails, galleries, and publishing attributes.
5. Run preflight checks, submit the form, and determine the result from backend readback.

### Article Editing

1. Select a category and read the article list.
2. Select an article and load its current backend form and field values.
3. Import replacement HTML when needed, or edit fields, media, and status directly.
4. Preserve fields that were not explicitly changed and validate the form revision before writing.
5. Read the article again after writing and compare the submitted fields with the final values.

### Batch Publishing

- Reuse categories, media policies, and publishing attributes across one queue.
- Parse, validate, upload, and read back each manuscript independently.
- Pause requests take effect after the active item completes; failed items remain available for separate handling.

### Administration

The workspace provides article-list operations, category management, site carousels, single pages, message replies, and discoverable backend modules. Write operations are bound to routes, forms, and submit controls discovered on the target backend.

## HTML and Local Media

HTML files and relative-path images can use the following layout:

```text
article-folder/
├─ article.html
└─ images/
   ├─ cover.jpg
   └─ detail-01.png
```

```html
<article>
  <h2>Section heading</h2>
  <p>Article content.</p>
  <img src="images/detail-01.png" alt="Image description">
</article>
```

Relative paths are resolved from the directory containing the HTML file. Local media is checked against the target backend's upload configuration, and body paths are replaced with the resulting upload locations.

See the [HTML Manuscript Guide](docs/HTML_GUIDE.md) for the field contract, compatible formats, media rules, and security boundaries. The editable template is available at [`examples/example_article.html`](examples/example_article.html).

## Run from Source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## Build the Windows EXE

```powershell
.\build_windows_exe.bat
```

The build script creates an isolated Python environment, installs the bounded dependency set, and generates the current-version EXE. Development workspaces run regression tests first when those suites are present; the release source tree excludes test files, so the test step is skipped there.

## Documentation

- [HTML Manuscript Guide](docs/HTML_GUIDE.md): HTML structure, SEO fields, media directories, and batch import.
- [Project Structure](docs/PROJECT_STRUCTURE.md): responsibilities of every file in the release source tree.
- [HTML Example](examples/example_article.html): neutral template for copying and adaptation.
- [v3.4.10 Release](https://github.com/jiyan1989/PbootCMS_Publisher_W30/releases/tag/v3.4.10): Windows executable, release notes, and integrity verification.

## Data Safety

The release repository contains runtime source, static interface assets, build configuration, documentation, and neutral examples only. The following runtime data is excluded from version control:

- `publisher_config.json`
- `site_sessions/`
- `drafts/`
- `backend_diagnostics/`
- `*.db`, `*.sqlite*`, and `*.log*`
- `.env*`, certificates, private keys, and token files

The release tree contains no real site addresses, usernames, passwords, cookies, sessions, drafts, logs, or business databases.

### Runtime Data

| Path | Contents | Protection |
| --- | --- | --- |
| `publisher_config.json` | Application settings, site history, and local login data | Passwords are encrypted with Windows DPAPI. |
| `site_sessions/` | Site-isolated session data | Bound to the current Windows login account. |
| `drafts/` | Recoverable workflow drafts | Does not store the full parsed body or plaintext credentials. |
| `products_cache.db` | Site-isolated product cache | Local SQLite data; excluded from version control. |
| `operation_audit.db` | Redacted operation summaries | Excludes passwords, tokens, and full body content. |
| `operation_journal.db` | State for writes with unknown results | Retains hashes and short summaries only. |
| `backend_diagnostics/` | Read-only backend structure diagnostics | Redacted when generated and excluded from version control. |
| `*.log*` | Local runtime records | Sensitive fields are redacted by the logging layer. |

The default data directory is the program directory. `PBOOT_PUBLISHER_DATA_DIR` can point runtime data to a separate directory.

## Troubleshooting

### Backend Login Fails

- The backend entry point should reference the actual login page, including custom entry-file names.
- Reverse proxies, WAF rules, or system proxy settings can change login routes; the selected network mode should match the normal browser environment.
- JavaScript-dependent authentication should use Native Web Login and Web Login Synchronization.

### HTML Title or Body Not Detected

- `#seo-metadata` and `<article>` are the recommended structure.
- General HTML should include `<title>` or `<h1>`, with body content under `<article>` or `<body>`.
- The field mapping view is authoritative; title and body are required sources for content publishing.

### Local Images Are Missing

- Relative paths are resolved from the HTML file's directory, such as `images/detail-01.png`.
- `/upload/a.jpg` is a site-root path and is not read as a local file.
- File type, size, and backend `accept` restrictions are checked before upload.

### Backend Form Requires JavaScript

When dynamic fields or upload policies cannot be safely reproduced from static inspection, the application stops guess-based writes and provides the authenticated native web entry point.

### Operation Result Is “Unknown”

“Unknown” means that a write request may have reached the server but backend readback did not prove the final state. The state is recorded in the operation and persistence journals. Verify the backend state before retrying to avoid duplicate writes.

## Compatibility and Boundaries

- Intended only for PbootCMS sites with appropriate administrative authorization.
- Backend forms and upload behavior may differ across PbootCMS versions, themes, and customizations.
- A single test article is recommended when connecting a new backend for the first time.
- Site backups are recommended before publishing, editing, or batch operations.

## License

This project is distributed under the [MIT License](LICENSE).

## Feedback

Feature requests and reproducible defects can be recorded in the repository [Issues](https://github.com/jiyan1989/PbootCMS_Publisher_W30/issues). Issue reports should remove site addresses, usernames, passwords, cookies, session files, and other sensitive data.
