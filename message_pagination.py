"""Read-only Message/index routing and explicit pagination evidence."""
import re
from urllib.parse import parse_qsl, urljoin, urlsplit
from message_routes import message_route


def list_url(url, reference, filter_params=()):
    try:
        return message_route(
            url, reference, r'/Message/index(?:/page/[1-9][0-9]*)?/?',
            pagination=True, filter_params=filter_params)[0]
    except ValueError as exc:
        raise RuntimeError(f'不是可确认的只读留言列表地址：{exc}') from None


def get_list_page(session, url, reference, cancel_callback=None,
                  filter_params=()):
    visited = set()
    expected_page = page_number(url)
    for _ in range(21):
        if cancel_callback:
            cancel_callback()
        url = list_url(url, reference, filter_params=filter_params)
        if page_number(url) != expected_page:
            raise RuntimeError('留言列表重定向改变页码，未确认完整')
        if url in visited:
            raise RuntimeError('留言列表重定向循环')
        visited.add(url)
        try:
            response = session.get(url, timeout=25, allow_redirects=False)
        except Exception as exc:
            raise RuntimeError('留言列表网络请求失败，请重新读取') from exc
        status = getattr(response, 'status_code', 0)
        if status not in (301, 302, 303, 307, 308):
            resolved = list_url(getattr(response, 'url', '') or url, reference,
                                filter_params=filter_params)
            if page_number(resolved) != expected_page:
                raise RuntimeError('留言列表响应页码不匹配，未确认完整')
            return response
        location = (getattr(response, 'headers', {}) or {}).get('Location', '')
        if not location:
            raise RuntimeError('留言列表重定向缺少地址')
        next_url = urljoin(url, location)
        close = getattr(response, 'close', None)
        if callable(close):
            close()
        url = list_url(next_url, reference, filter_params=filter_params)
    raise RuntimeError('留言列表重定向过多')


def page_number(url):
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = {str(key).lower(): value for key, value in pairs}
    match = re.search(r'/page/(\d+)/?$', query.get('p', parsed.path), re.I)
    for key in ('page', 'pageno', 'page_no', 'pageindex', 'page_index',
                'pagenum', 'page_num', 'current', 'currentpage',
                'current_page'):
        value = query.get(key)
        if value and value.isdigit():
            return int(value)
    if match:
        return int(match.group(1))
    # Custom themes sometimes expose only offset+limit.  Infer a page only
    # when both values prove the cursor; an offset without a size remains
    # unconfirmed rather than being guessed as page two.
    offset = query.get('offset')
    size = next((query.get(key) for key in
                 ('pagesize', 'page_size', 'perpage', 'per_page', 'limit')
                 if query.get(key)), None)
    if offset is not None and size is not None:
        try:
            offset_value, size_value = int(offset), int(size)
            if offset_value >= 0 and size_value > 0 and offset_value % size_value == 0:
                return offset_value // size_value + 1
        except (TypeError, ValueError):
            pass
    return 1


def next_page(soup, page_number=1):
    """Return an enabled next anchor, excluding links inside message bodies."""
    numeric = []
    for anchor in soup.find_all('a', href=True):
        if any(parent.find('input', attrs={'name': 'checkbox'}) or
               parent.select_one('thead th[colspan]') for parent in anchor.find_parents('table')):
            continue
        label = anchor.get_text(' ', strip=True)
        classes = ' '.join(anchor.get('class') or [])
        if any('disabled' in ' '.join(node.get('class') or []).lower()
               or str(node.get('aria-disabled', '')).lower() == 'true'
               or node.has_attr('disabled') for node in (anchor, *anchor.parents) if getattr(node, 'attrs', None) is not None):
            continue
        pager = anchor.find_parent(attrs={'class': re.compile(r'pagination|laypage|pager|(?:^|\s)page(?:\s|$)', re.I)}) or anchor.find_parent(id=re.compile(r'pagebar|pagination|pager', re.I))
        if pager and label.isdigit() and int(label) > page_number:
            numeric.append((int(label), anchor))
        if not (re.fullmatch(r'下一页|下页|next|[>›»]+', label, re.I)
                or re.search(r'(?:^|[-_])next(?:$|[-_])', classes, re.I)
                or 'next' in (anchor.get('rel') or [])):
            continue
        return anchor
    if numeric:
        number, anchor = min(numeric, key=lambda item: item[0])
        if number != page_number + 1:
            raise RuntimeError('留言分页页码不连续，未确认完整')
        return anchor
    return None


def explicit_empty(soup):
    if any(re.fullmatch(r'(?:暂无(?:相关)?数据|没有(?:相关)?数据|无数据|暂无留言|no\s+data)[。.!！]?',
                           node.get_text(' ', strip=True), re.I)
               for node in soup.select('table td')):
        return True
    # Upstream template has an empty foreach and a pagebar, not a "no data" row.
    for tab in soup.select('.layui-tab'):
        title = tab.select_one('.layui-tab-title')
        content = tab.select_one('.layui-tab-item.layui-show')
        if not title or title.get_text(strip=True) != '留言列表' or content is None:
            continue
        pager = content.select_one('.page')
        if (pager is not None and not pager.get_text(strip=True) and not pager.find('a')
                and not content.find(['table', 'input', 'form'])
                and not content.get_text(strip=True)):
            return True
    return False
