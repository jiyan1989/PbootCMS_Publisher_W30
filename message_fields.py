"""Lossless source evidence and a readable, non-executing message projection."""
from bs4 import Comment, NavigableString
from html_fragments import HTMLFragments


BLOCKS = frozenset('p div section article header footer ul ol li table tr pre blockquote h1 h2 h3 h4 h5 h6'.split())
ALIASES = {
    'name': ('姓名', '联系人', '您的姓名', '客户姓名'),
    'email': ('邮箱', '电子邮箱', '联系邮箱'),
    'phone': ('电话', '联系电话', '手机', '手机号码'),
    'industry': ('行业', '所在行业'), 'city': ('城市', '所在城市'),
    'product': ('需要的产品', '需求产品'),
    'time': ('时间', '留言时间', '提交时间', '发布时间'),
    'content': ('内容', '留言', '留言内容'),
    'visitor': ('访客', '访客信息'), 'status': ('状态', '留言状态'),
}
FIELD_KEYS = {label: key for key, labels in ALIASES.items() for label in labels}


def readable_text(element):
    """Retain inline spacing and explicit breaks, without running/rendering HTML."""
    parts = []
    def boundary():
        if parts and not parts[-1].endswith('\n'):
            parts.append('\n')
    def visit(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if node.name in ('script', 'style', 'template'):
            return
        if node.name == 'br':
            parts.append('\n')
            return
        if node.name in BLOCKS:
            boundary()
        for child in node.children:
            visit(child)
        if node.name in BLOCKS:
            boundary()
    for child in element.children:
        visit(child)
    return ''.join(parts)


class MessageFieldSource:
    def __init__(self, html):
        self.html = html
        self.fragments = HTMLFragments(html)
        self.nodes = {node['start']: node for node in self.fragments.nodes}

    def field(self, label, cell):
        line, column = cell.sourceline, cell.sourcepos
        start = self.fragments.lines[line - 1] + column if line is not None and column is not None else None
        node = self.nodes.get(start)
        closed = node and self.html[node['close']:node['end']].lower().startswith('</td')
        source = self.html[node['open']:node['close']] if node and node['tag'] == 'td' and closed else None
        label_text = readable_text(label)
        return {'label': label_text, 'value': readable_text(cell),
                'key': FIELD_KEYS.get(label_text.strip().rstrip('：:'), ''),
                'html': source, 'source_exact': source is not None}


def csv_safe_cell(value):
    """Neutralize spreadsheet formula interpretation; JSON evidence stays raw."""
    text = '' if value is None else str(value)
    first = text.lstrip(' \t\r\n\ufeff')[:1]
    if first in ('=', '+', '-', '@') or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text
