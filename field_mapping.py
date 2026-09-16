"""Pure field matching, mapping persistence and publish-value sanitization."""
import re

ALIASES = {
    "title": ("title", "name", "subject", "标题", "内容标题"),
    "subtitle": ("subtitle", "sub_title", "subname", "副标题"),
    "filename": ("filename", "urlname", "slug", "url名称", "链接名称"),
    "description": ("description", "metadescription", "seo_description", "seo描述", "描述"),
    "keywords": ("keywords", "keyword", "tags", "关键词", "标签"),
    "tags": ("tags", "keywords", "keyword", "标签", "关键词"),
    "content": ("content", "body", "details", "detail", "article", "内容", "正文", "产品详情内容"),
    "imagealt": ("image_alt", "imagealt", "alt", "alt属性", "图片alt", "图片alt属性", "图片alt文本", "图片替代文本"),
    "model": ("ext_xinghao", "xinghao", "model", "型号", "产品型号"),
    "price": ("ext_jiage", "jiage", "price", "价格", "产品价格"),
}


def _has_explicit_value(value):
    """Presence check that keeps numeric zero while rejecting False/blank."""
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(_has_explicit_value(item) for item in value)
    if isinstance(value, bool):
        return value
    return bool(str(value).strip())
CANONICAL = {
    "urlslug": "filename", "url": "filename", "slug": "filename",
    "metadescription": "description", "seodescription": "description",
    "targetkeyword": "keywords", "targetkeywords": "keywords",
    "image_alt": "imagealt", "image-alt": "imagealt",
    "alt": "imagealt", "alt属性": "imagealt", "图片alt属性": "imagealt",
    "xinghao": "model", "型号": "model", "jiage": "price", "价格": "price",
}


def normalize_field_name(value):
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(value or "").lower())


def canonical_field_name(value):
    raw=str(value or "").strip().lower()
    normalized=normalize_field_name(raw)
    return CANONICAL.get(raw, CANONICAL.get(normalized, normalized))


def field_match_score(html_key, cms_name, cms_label=""):
    source=canonical_field_name(html_key)
    name=normalize_field_name(cms_name)
    label=normalize_field_name(cms_label)
    if not source or not name:
        return 0
    if source == name:
        return 100
    aliases=ALIASES.get(source, ())
    normalized_aliases={normalize_field_name(x) for x in aliases}
    if name in normalized_aliases:
        return 95
    if label and label in normalized_aliases:
        return 90
    # CMS labels commonly include decorations such as “产品型号(扩展字段)”
    # or “图片ALT文本”.  Exact label matching therefore misses an otherwise
    # unambiguous field.  Accept containment only for meaningful aliases.
    for alias in normalized_aliases:
        if len(alias) >= 3 and (alias in name or name in alias):
            return max(84, 82 + min(len(alias), len(name)))
        if len(alias) >= 2 and label and (alias in label or label in alias):
            return max(82, 80 + min(len(alias), len(label)))
    if source in name or name in source:
        return 70 + min(len(source),len(name))
    if label and (source in label or label in source):
        return 60 + min(len(source),len(label))
    return 0


def choose_best_field(html_key, form_fields, minimum_score=60):
    best_name=""; best_score=minimum_score-1
    for field in form_fields or []:
        if field.get("mappable") is False or field.get("readonly"):
            continue
        name=field.get("name","")
        score=field_match_score(html_key,name,field.get("label",name))
        if score>best_score:
            best_name,best_score=name,score
    return best_name


def _mapping_site_key(site_id, model_key=""):
    site_id = str(site_id or "")
    model_key = str(model_key or "").strip()
    return f"{site_id}::mcode:{model_key}" if site_id and model_key else site_id


def mapping_scope(config_data, tab_key, site_id, model_key=""):
    saved=config_data.get("mappings",{}).get(tab_key,{})
    scoped_key = _mapping_site_key(site_id, model_key)
    if scoped_key and isinstance(saved.get(scoped_key),dict):
        return dict(saved[scoped_key])
    # 兼容旧版仅按站点保存的映射，首次按模型保存后会自然迁移。
    if site_id and isinstance(saved.get(site_id),dict):
        return dict(saved[site_id])
    # Backward compatibility with the old global {html: cms} format.
    if saved and all(not isinstance(v,dict) for v in saved.values()):
        return dict(saved)
    return {}


def save_mapping_scope(config_data, tab_key, site_id, mapping, model_key=""):
    tab_maps=config_data.setdefault("mappings",{}).setdefault(tab_key,{})
    if site_id:
        # Migrate legacy mapping into the active site's scope.
        if tab_maps and all(not isinstance(v,dict) for v in tab_maps.values()):
            config_data["mappings"][tab_key]={}
            tab_maps=config_data["mappings"][tab_key]
        tab_maps[_mapping_site_key(site_id, model_key)]=dict(mapping)
    else:
        config_data["mappings"][tab_key]=dict(mapping)


def validate_mapping(rows, form_fields, require_core=True):
    """Return blocking errors and non-blocking warnings for mapping rows.

    rows contains (html_key, cms_field, value) tuples.  Validation is based on
    the source semantic key, so a site's custom CMS field name remains valid.
    require_core: 发布场景要求 title/content 必填；编辑（局部覆盖）场景传 False，
    只做重复映射/失效字段/未映射告警检查。
    """
    # Import lazily: ``form_controls`` is also used by the app while loading
    # mapping helpers, and keeping this dependency local avoids a module
    # initialization cycle for lightweight mapping consumers.
    from form_controls import normalize_control_value, validate_control_value

    errors, warnings = [], []
    descriptors = {str(field.get("name", "")): field
                   for field in (form_fields or [])
                   if str(field.get("name", ""))}
    known = set(descriptors)
    used = {}
    submitted = {}
    # Do not invent title/content requirements for models whose real form
    # does not expose those controls (for example an external-link model or
    # a custom body field).  A core key is still required when the form has a
    # canonical equivalent or the user explicitly mapped that source key.
    required = {}
    if require_core:
        canonical_names = {canonical_field_name(name) for name in known}
        normalized_controls = {
            normalize_field_name(field.get("name", ""))
            for field in (form_fields or [])
        } | {
            normalize_field_name(field.get("label", ""))
            for field in (form_fields or [])
        }
        row_core = {
            canonical_field_name(row[0]) for row in (rows or [])
            if len(row) >= 2 and str(row[1] or "")
        }
        for core in ("title", "content"):
            aliases = {normalize_field_name(item) for item in ALIASES.get(core, ())}
            if (core in canonical_names or aliases.intersection(normalized_controls)
                    or core in row_core):
                required[core] = False
    for html_key, cms_field, value in rows or []:
        canonical = canonical_field_name(html_key)
        cms_field = str(cms_field or "")
        has_value = _has_explicit_value(value)
        if cms_field and cms_field not in known:
            errors.append(f"{html_key} 映射到了当前表单不存在的字段 {cms_field}")
        if cms_field:
            descriptor = descriptors.get(cms_field, {})
            if descriptor.get("mappable") is False or descriptor.get("readonly"):
                errors.append(f"{html_key} 映射到了后台只读或系统字段 {cms_field}")
            if cms_field in used and used[cms_field] != html_key:
                errors.append(f"{used[cms_field]} 和 {html_key} 同时映射到 {cms_field}")
            used[cms_field] = html_key
            submitted[cms_field] = value
            # Mapping used to validate only select options and required
            # fields, while the actual browser form also applies pattern,
            # length, range/step, email/URL and Layui deterministic rules.
            # Reuse the same normalization/constraint evaluator here so a
            # mapped publish value cannot pass the desktop preflight and then
            # be rejected by the native form.  Empty required values are left
            # to the single required-field pass below to avoid duplicate
            # messages.
            if has_value:
                normalized = normalize_control_value(value, descriptor)
                issue = validate_control_value(normalized, descriptor)
                if issue:
                    errors.append(f"{html_key}{issue}（字段 {cms_field}）")
        elif has_value:
            warnings.append(f"{html_key} 有内容但未映射，不会提交")
        if canonical in required:
            required[canonical] = bool(cms_field and has_value)
    for name, valid in required.items():
        if not valid:
            errors.append(f"必填来源字段 {name} 未映射或值为空")
    if require_core:
        for name, descriptor in descriptors.items():
            if not descriptor.get("required") or name in (
                    "formcheck", "scode", "id", "mcode", "ac"):
                continue
            value = submitted.get(name, descriptor.get("value", ""))
            present = _has_explicit_value(value)
            if not present:
                label = str(descriptor.get("label", "") or name)
                errors.append(f"后台必填字段 {label}（{name}）没有值")
    return errors, warnings


def sanitize_publish_fields(fields):
    """Compatibility entry point: do not mutate content behind the user's back.

    The real form validates values and the CMS owns server-side normalization.
    This function is not an HTML sanitizer; UI previews must remain inert.
    """
    return dict(fields or {})
