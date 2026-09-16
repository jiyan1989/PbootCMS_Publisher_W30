"""Locate source spans without rewriting HTML attributes, entities or CSS."""
from html.parser import HTMLParser
from html import escape, unescape
import re

VOID_TAGS = frozenset('area base br col embed hr img input link meta param source track wbr'.split())

_ATTRIBUTE = re.compile(r'''\s+([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''')


def attribute_spans(tag):
    """Read actual attributes, never text resembling attributes inside a value."""
    opening = re.match(r'<[^\s/>]+', tag)
    at = opening.end() if opening else len(tag)
    while at < len(tag):
        match = _ATTRIBUTE.match(tag, at)
        if not match:
            break
        group = next((i for i in (2, 3, 4) if match.group(i) is not None), None)
        yield dict(name=match.group(1).lower(), start=match.start(), end=match.end(),
                   value=unescape(match.group(group)) if group else None,
                   value_start=match.start(group) if group else None,
                   value_end=match.end(group) if group else None,
                   quote='"' if group == 2 else "'" if group == 3 else '')
        at = match.end()


def rewrite_attributes(tag, updates, *, remove=()):
    """Patch values only, preserving every unrelated source byte and quote style."""
    changes, found = [], set()
    for attr in attribute_spans(tag):
        name = attr['name']
        found.add(name)
        if name in remove:
            changes.append((attr['start'], attr['end'], ''))
        elif name in updates and str(updates[name]) != attr['value']:
            value = escape(str(updates[name]), quote=True)
            if attr['value_start'] is None:
                changes.append((attr['end'], attr['end'], '="' + value + '"'))
            else:
                if not attr['quote']:
                    value = '"' + value + '"'
                changes.append((attr['value_start'], attr['value_end'], value))
    missing = ''.join(' ' + name + '="' + escape(str(value), quote=True) + '"'
                      for name, value in updates.items() if name not in found and name not in remove)
    if missing:
        closing = re.search(r'/?>\s*$', tag)
        if closing:
            changes.append((closing.start(), closing.start(), missing))
    for start, end, replacement in sorted(changes, reverse=True):
        tag = tag[:start] + replacement + tag[end:]
    return tag


class HTMLFragments(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0]
        for index, char in enumerate(source):
            if char == '\n':
                self.lines.append(index+1)
        self.nodes, self.stack = [], []
        self.feed(source)
        self.close()
        for index in self.stack:
            self.nodes[index]['close'] = len(source)
            self.nodes[index]['end'] = len(source)

    def position(self):
        line, column = self.getpos()
        return self.lines[line-1] + column

    def handle_starttag(self, tag, attrs):
        if self.stack and self.nodes[self.stack[-1]]['tag'] in ('textarea', 'title'):
            return
        start = self.position()
        end = start + len(self.get_starttag_text())
        self.nodes.append(dict(tag=tag, attrs=dict(attrs), start=start, open=end,
                               close=end, end=end, parent=self.stack[-1] if self.stack else None))
        if tag not in VOID_TAGS:
            self.stack.append(len(self.nodes)-1)

    def handle_startendtag(self, tag, attrs):
        if self.stack and self.nodes[self.stack[-1]]['tag'] in ('textarea', 'title'):
            return
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag):
        if (self.stack and self.nodes[self.stack[-1]]['tag'] in ('textarea', 'title')
                and self.nodes[self.stack[-1]]['tag'] != tag):
            return
        for at in range(len(self.stack)-1,-1,-1):
            index = self.stack[at]
            if self.nodes[index]['tag'] != tag:
                continue
            start = self.position()
            end = self.source.find('>',start)
            end = len(self.source) if end < 0 else end+1
            for unclosed in self.stack[at+1:]:
                self.nodes[unclosed].update(close=start,end=start)
            self.nodes[index].update(close=start,end=end)
            del self.stack[at:]
            break

    def find(self, tag=None, **attrs):
        return [node for node in self.nodes if (tag is None or node['tag']==tag)
                and all(node['attrs'].get(k)==v for k,v in attrs.items())]

    def outer(self, node):
        return self.source[node['start']:node['end']]

    def inner(self, node):
        return self.source[node['open']:node['close']]

    def with_styles(self, node, *, inner=False, exclude=()):
        # Keep original stylesheets as stylesheets. This lets the browser use
        # correct specificity/media queries, rather than an incomplete inliner.
        styles = [n for n in self.nodes if (n['tag']=='style' or
                  n['tag']=='link' and 'stylesheet' in (n['attrs'].get('rel') or '').split())
                  and not (node['start'] <= n['start'] < node['end'])
                  and not any(e['start'] <= n['start'] < e['end'] for e in exclude)]
        start, end = (node['open'], node['close']) if inner else (node['start'], node['end'])
        fragments = [(start, end)] + [(n['start'], n['end']) for n in styles]
        output = []
        # Keep styles that appeared AFTER the content after it, too. Moving all
        # external styles to the front would invert their CSS cascade order.
        for left, right in sorted(fragments):
            cursor = left
            for removed in sorted(exclude, key=lambda n: n['start']):
                if left <= removed['start'] and removed['end'] <= right:
                    if removed['start'] >= cursor:
                        output.append(self.source[cursor:removed['start']])
                    cursor = max(cursor, removed['end'])
            output.append(self.source[cursor:right])
        return ''.join(output)
