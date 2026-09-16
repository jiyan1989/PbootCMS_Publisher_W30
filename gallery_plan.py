"""Ordered image/title pairs, with explicit empty gallery distinct from no edit."""
import os
from form_controls import validate_control_value


def normalize_gallery_plan(value, *, check_files=True):
    if not isinstance(value, list):
        raise ValueError('图集编辑必须是有序图片列表')
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'kind', 'value', 'title'}:
            raise ValueError('图集图片结构无效')
        kind, source, title = item['kind'], item['value'], item['title']
        if kind not in ('url', 'file') or not isinstance(source, str) or not source.strip() or not isinstance(title, str):
            raise ValueError('图集图片地址/文件或标题无效')
        if kind == 'file' and check_files and not os.path.isfile(source):
            raise ValueError('图集文件不存在或无法读取：' + source)
        result.append({'kind': kind, 'value': source, 'title': title})
    return result


def gallery_options(options, descriptors):
    if 'gallery_plan' not in options:
        return {}
    plan = normalize_gallery_plan(options['gallery_plan'])
    controls = [field for field in descriptors if field.get('name') == 'pics']
    if not controls or all(field.get('disabled') for field in controls):
        raise ValueError('当前后台表单没有可用的图集字段 pics')
    if not plan:
        for field in controls:
            error = validate_control_value('', field)
            if error:
                raise ValueError('图集：' + error)
    title_controls = [field for field in descriptors if field.get('name') == 'picstitle[]']
    for item in plan:
        for field in title_controls:
            error = validate_control_value(item['title'], field)
            if error:
                raise ValueError('图集标题：' + error)
    paths = [item['value'] for item in plan if item['kind'] == 'file']
    if options.get('carousel_paths') and list(options['carousel_paths']) != paths:
        raise ValueError('图集顺序计划与上传文件列表冲突')
    return {'gallery_plan': plan, 'carousel_paths': paths}
