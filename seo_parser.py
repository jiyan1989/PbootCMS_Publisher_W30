"""SEO HTML parser."""
import re
from bs4 import BeautifulSoup

class SEOHTMLParser:
    SAFE_STYLE_PROPERTIES = {
        "color", "background", "background-color", "font-size", "font-weight",
        "font-style", "font-family", "line-height", "text-align", "text-decoration",
        "display", "width", "min-width", "max-width", "height", "min-height",
        "max-height", "margin", "margin-top", "margin-right", "margin-bottom",
        "margin-left", "padding", "padding-top", "padding-right", "padding-bottom",
        "padding-left", "border", "border-top", "border-right", "border-bottom",
        "border-left", "border-color", "border-width", "border-style",
        "border-collapse", "border-radius", "list-style", "list-style-type",
        "vertical-align", "white-space", "word-break", "overflow-wrap",
    }
    STYLE_MAP = {
        # No h1 entry on purpose: the PbootCMS detail template already renders
        # the title field as the page H1, so _process_content* strips any <h1>
        # from the submitted body instead of styling it.
        "h2": "font-size:21px;color:#2D56A8;font-weight:700;margin-top:32px;",
        "h3": "font-size:18px;color:#2D56A8;font-weight:600;",
        "p": "font-size:15px;color:#333;line-height:1.8;",
        "li": "font-size:15px;color:#333;line-height:1.8;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;",
        "th": "background-color:#E8EEF7;font-weight:600;text-align:left;padding:10px;border:1px solid #ccc;font-size:14px;",
        "td": "padding:10px;border:1px solid #ccc;font-size:14px;",
        "ul": "padding-left:24px;",
        "ol": "padding-left:24px;",
        "a": "color:#2D56A8;",
    }


    def parse_file(self, filepath):
        """解析SEO HTML文件，返回字段字典。
        支持新格式（#seo-metadata 表格 + <article>）和旧格式（div.section）。
        """
        with open(filepath, "rb") as f:
            raw = f.read()
        declared = re.search(br'charset\s*=\s*["\x27]?([\w-]+)', raw[:4096], re.I)
        encodings = ([declared.group(1).decode('ascii')] if declared else [])
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            encodings.insert(0, 'utf-16')
        encodings.extend(['utf-8-sig', 'gb18030'])
        for encoding in encodings:
            try:
                html = raw.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            raise ValueError('无法识别 HTML 编码，请指定 charset 或另存为 UTF-8')
        return self.parse_file_html(html)

    def parse_file_html(self, html):
        """解析SEO HTML字符串，返回字段字典。供测试与内嵌解析复用。"""
        soup = BeautifulSoup(html, "html.parser")
        stylesheet = "\n".join(tag.get_text(" ", strip=False) for tag in soup.find_all("style"))
        fields = {}

        # ══ #seo-metadata 表格 + <article>（与 Qoder 的 HTML 产出契约）══
        seo_div = soup.find(id="seo-metadata")
        article_el = soup.find("article")
        if seo_div and article_el:
            # 字段映射：表格首列文字 -> 内部字段名
            # 兼容两种写法：① <th> 表头 ② 首列 <td> 作为标签（内容生成工具当前产出）
            field_map = {
                "title": "title",
                "subtitle": "subtitle",
                "urlslug": "filename",
                "url": "filename",
                "metadescription": "description",
                "seodescription": "description",
                "tags": "tags",
                "targetkeyword": "keywords",
                "imagealt": "image_alt",
            }
            for row in seo_div.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True)
                    val = cells[1].get_text(strip=True)
                    nk = re.sub(r"\s+", "", key.lower())
                    if nk in field_map:
                        fields[field_map[nk]] = val
            # 正文内容作为 content
            fields["content"] = self._process_content_el(article_el, stylesheet)
            return fields

        # ══ 旧格式：div.section ══
        for sec in soup.find_all("div", class_="section"):
            h2 = sec.find("h2")
            if not h2:
                continue
            raw = h2.get_text(strip=True)
            field_name = raw.split("（")[0].split("(")[0].strip()
            # Keep one canonical key across the new metadata-table format and
            # the legacy div.section format.  Previously the same field became
            # image_alt in one HTML shape and image-alt in the other, which
            # made saved mappings and automatic mapping appear incomplete.
            normalized_name = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff-]+", "", field_name.lower())
            legacy_aliases = {
                "image-alt": "image_alt",
                "imagealt": "image_alt",
                "urlslug": "filename",
                "meta-description": "description",
                "metadescription": "description",
                "seo-description": "description",
                "seodescription": "description",
                "target-keyword": "keywords",
                "targetkeyword": "keywords",
            }
            field_name = legacy_aliases.get(normalized_name, field_name)
            section_el = sec.find("section")
            if not section_el:
                continue
            if field_name == "content":
                fields[field_name] = self._process_content(section_el, stylesheet)
            elif field_name in ("image-alt", "image_alt"):
                fields[field_name] = self._extract_alt_texts(section_el)
            else:
                fields[field_name] = section_el.get_text(strip=True)

        if not fields:
            # Generic HTML follows the same CMS-safe layout as SEO documents.
            source = BeautifulSoup(html, "html.parser")
            root = source.find('article') or source.find('body') or source
            fragment = BeautifulSoup('<article></article>', 'html.parser')
            for child in list(root.contents):
                fragment.article.append(child.extract())
            fields['content'] = self._process_content_el(fragment.article, stylesheet)
            title = soup.find('title') or soup.find('h1')
            fields['title'] = title.get_text(' ', strip=True) if title else ''
            for name in ('description','keywords'):
                meta = soup.find('meta', attrs={'name':name})
                if meta and meta.has_attr('content'):
                    fields[name] = meta['content']
        return fields


    @classmethod
    def _safe_style(cls, raw):
        result = {}
        for declaration in str(raw or "").split(";"):
            if ":" not in declaration:
                continue
            name, value = declaration.split(":", 1)
            name, value = name.strip().lower(), value.strip()
            unsafe = any(token in value.lower() for token in ("javascript:", "expression(", "url("))
            if name in cls.SAFE_STYLE_PROPERTIES and value and not unsafe:
                result[name] = value
        return result

    @staticmethod
    def _style_text(values):
        return ";".join(f"{name}:{value}" for name, value in values.items()) + (";" if values else "")

    def _inline_stylesheet(self, root, stylesheet):
        css = re.sub(r"/\*[\s\S]*?\*/", "", stylesheet or "")
        for selector_text, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            style_values = self._safe_style(declarations)
            if not style_values:
                continue
            for selector in selector_text.split(","):
                selector = selector.strip()
                if not selector or selector.startswith("@") or any(x in selector for x in (":hover", "::", ":before", ":after")):
                    continue
                try:
                    matches = root.select(selector)
                except Exception:
                    continue
                for tag in matches:
                    merged = self._safe_style(tag.get("style", ""))
                    # Existing inline style has the highest priority.
                    merged = {**style_values, **merged}
                    tag["style"] = self._style_text(merged)

    def _apply_default_styles(self, root):
        for tag_name, declarations in self.STYLE_MAP.items():
            defaults = self._safe_style(declarations)
            for tag in root.find_all(tag_name):
                existing = self._safe_style(tag.get("style", ""))
                tag["style"] = self._style_text({**defaults, **existing})

    def _process_content_el(self, el, stylesheet=""):
        """Inline article styles without allowing source CSS to restyle the site."""
        clone = BeautifulSoup(str(el), "html.parser")
        root = clone.find(el.name) or clone
        self._inline_stylesheet(root, stylesheet)
        for tag in clone.find_all("style"):
            tag.decompose()
        for tag in clone.find_all("link", rel="stylesheet"):
            tag.decompose()
        for tag in clone.find_all(id="seo-metadata"):
            tag.decompose()
        self._apply_default_styles(root)
        anchor_ids = {a.get("href", "")[1:] for a in root.find_all("a", href=True)
                      if a.get("href", "").startswith("#")}
        # Classes from the standalone document can collide with the CMS theme.
        for tag in root.find_all(True):
            tag.attrs.pop("class", None)
            if tag.get("id") not in anchor_ids:
                tag.attrs.pop("id", None)
        return "".join(str(child) for child in root.contents).strip()

    def _process_content(self, section_el, stylesheet=""):
        return self._process_content_el(section_el, stylesheet)

    def _extract_alt_texts(self, section_el):
        """提取image-alt字段的alt文本列表"""
        alts = []
        for item in section_el.find_all("p", class_="alt-item"):
            m = re.search(r'alt="([^"]*)"', item.get_text())
            if m:
                alts.append(m.group(1))
        # Use one ALT per line.  Commas are valid inside an ALT description,
        # so comma-joining makes it impossible for the image builder to know
        # whether a comma separates two images or is part of one description.
        return "\n".join(alts)
