"""Explicit thumbnail control intent; blank is not synonymous with unchanged."""
import os

from form_controls import validate_control_value


def thumbnail_options(options, descriptors=None):
    explicit = 'thumbnail_mode' in options
    mode = options.get('thumbnail_mode')
    path = options.get('thumbnail_path', '')
    url = options.get('thumbnail_url', '')
    path = '' if path is None else path
    url = '' if url is None else url
    first = bool(options.get('thumbnail_from_first'))
    if not isinstance(path, str) or not isinstance(url, str):
        raise ValueError('缩略图文件或地址必须是文本')
    if not explicit:
        # Compatibility with old saved tasks: an empty URL never meant clear.
        mode = 'file' if path else 'first' if first else 'url' if url else 'none'
    if mode not in ('none', 'file', 'url', 'clear', 'first'):
        raise ValueError('缩略图操作无效，请重新选择')
    if explicit and ((path and mode != 'file') or (url and mode != 'url') or (first and mode != 'first')):
        raise ValueError('缩略图操作与文件/地址选项冲突')
    if mode == 'file' and (not path or not os.path.isfile(path)):
        raise ValueError('缩略图文件不存在或无法读取')
    if mode == 'url' and not url.strip():
        raise ValueError('请填写缩略图地址；需要清空时请选择明确清空')
    if explicit and mode != 'none' and descriptors is not None:
        controls = [field for field in descriptors if field.get('name') == 'ico']
        if not controls or all(field.get('disabled') or
                (field.get('readonly') and mode in ('url', 'clear')) for field in controls):
            raise ValueError('当前后台表单没有可编辑的缩略图字段 ico')
        if mode in ('url', 'clear'):
            for field in controls:
                error = validate_control_value(url if mode == 'url' else '', field)
                if error:
                    raise ValueError('缩略图：' + error)
    return {'thumbnail_mode': mode, 'thumbnail_path': path if mode == 'file' else '',
            'thumbnail_url': url if mode == 'url' else '', 'thumbnail_from_first': mode == 'first'}
