"""Pure content draft transformations shared by publish and edit workflows."""
from dataclasses import dataclass, field
from html import escape
import re


def image_sort_number(filename):
    match=re.search(r"(\d+)\.\w+$",str(filename or ""))
    return int(match.group(1)) if match else 999


def image_style_attrs(width_mode="preserve", width=790):
    """Return (width_attr, style) for one content image.

    宽度策略的单一来源：手动选图（build_image_html）与 HTML 内置图重写
    （html_images.rewrite_html_images）共用，避免两条路径前台表现不一致。
    width HTML 属性仅为展示提示，常被主题 `img { width:auto; }` 覆盖，
    因此同时写内联规则。
    """
    if width_mode == "preserve":
        return "", ""
    if width_mode == "fixed":
        return (
            f' width="{width}"',
            f"display:block;width:{width}px !important;min-width:{width}px !important;"
            f"max-width:{width}px !important;height:auto !important;margin:0 auto;")
    if width_mode == "original":
        return (
            "",
            "display:block;max-width:100% !important;height:auto !important;margin:0 auto;")
    return (
        f' width="{width}"',
        f"display:block;width:{width}px !important;max-width:100% !important;"
        "height:auto !important;margin:0 auto;")


def build_image_html(uploaded, alt_value="", width=790, width_mode="preserve"):
    raw_alt = str(alt_value or "").strip()
    # One ALT per line; commas belong to the description, not a separator.
    if "\n" in raw_alt or "\r" in raw_alt:
        alts = [item.strip() for item in raw_alt.splitlines() if item.strip()]
    else:
        alts = [raw_alt] if raw_alt else []
    parts=[]
    for index,(name,url) in enumerate(uploaded or []):
        number=image_sort_number(name)
        if 0<number<=len(alts): alt=alts[number-1]
        elif index<len(alts): alt=alts[index]
        else: alt=""
        # Escape both URL and ALT before placing them in HTML attributes.  The
        # previous malformed legacy parse could leave quote characters inside
        # ALT, causing the browser/CMS sanitizer to drop the attribute.
        safe_url = escape(str(url), quote=True)
        safe_alt = escape(str(alt), quote=True)
        # The width HTML attribute is only a presentational hint and is often
        # overridden by front-end theme CSS such as `img { width:auto; }`.
        # Keep the attribute for CMS compatibility and add an inline responsive
        # rule: 790px on a wide content column, 100% when the column is narrower.
        width_attr, style = image_style_attrs(width_mode, width)
        if width_mode == "preserve":
            parts.append(f'<img src="{safe_url}" alt="{safe_alt}" />')
        else:
            parts.append(
                f'<p style="text-align:center;">'
                f'<img src="{safe_url}" alt="{safe_alt}"{width_attr} style="{style}" />'
                f'</p>')
    return "\n".join(parts)


def insert_images_into_fields(fields, uploaded, target_field="content", strategy="top",
                              width_mode="preserve"):
    content=str(fields.get(target_field,"") or "")
    alt=fields.get("image-alt","") or fields.get("image_alt","")
    images=build_image_html(uploaded,alt,width_mode=width_mode)
    if not images: return content,"none",0
    if not content: return images,"empty",len(uploaded)
    if strategy=="before_h2":
        # 新规范的英文子标题使用 H3，旧稿仍可能使用 H2；两种都应视为
        # 正文分节标题，避免找不到 H2 时把图片错误堆到正文最前面。
        match=re.search(r"<h[23][\s>]",content,re.I)
        if match:
            at=match.start(); return content[:at]+images+"\n"+content[at:],"before_h2",len(uploaded)
    if strategy=="after_first_paragraph":
        match=re.search(r"</p\s*>",content,re.I)
        if match:
            at=match.end(); return content[:at]+"\n"+images+"\n"+content[at:],"after_first_paragraph",len(uploaded)
    return images+"\n"+content,"top",len(uploaded)


def restore_target(fields, original_fields, target_field):
    fields[target_field]=original_fields.get(target_field,"")
    return fields[target_field]


@dataclass
class ContentDraft:
    fields: dict=field(default_factory=dict)
    original_fields: dict=field(default_factory=dict)
    def load(self, values):
        self.fields=dict(values or {}); self.original_fields=dict(self.fields); return self.fields
    def clear(self):
        self.fields.clear(); self.original_fields.clear()
    def restore(self,target_field): return restore_target(self.fields,self.original_fields,target_field)
    def insert_images(self,uploaded,target_field="content",strategy="top"):
        value,location,count=insert_images_into_fields(self.fields,uploaded,target_field,strategy)
        self.fields[target_field]=value
        return location,count
