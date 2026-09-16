"""Preview and restore model links, preserving all unrelated source bytes."""
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime
from html import escape
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
from html_fragments import HTMLFragments, rewrite_attributes
from save_verification import TRANSPORT_FIELDS


def date_value(value):
    value = str(value or '').strip().replace('T', ' ')
    return datetime.fromisoformat(value)


def local_url(url, base):
    a, b = urlsplit(str(url)), urlsplit(str(base))
    return a.scheme in ('http', 'https') and a.hostname == b.hostname and a.port == b.port and not a.username and not a.password and not (b.scheme == 'https' and a.scheme != 'https') and not a.query and not a.fragment and not a.path.lower().endswith('.php')


def model_index(products):
    index = {}
    for p in products:
        model = str(p.get('xinghao') or '').strip()
        if model:
            index.setdefault(model.casefold(), []).append(p)
    return index


class TextSpans(HTMLFragments):
    def __init__(self, source):
        self.texts = []
        super().__init__(source)

    def handle_data(self, data):
        if data:
            self.texts.append((self.position(), data, list(self.stack)))


def rewrite_models(source, index, article_id, base, add=False):
    doc = TextSpans(source)
    changes, skipped, edits = [], [], []
    pattern = re.compile(r'(?<![\w-])(' + '|'.join(re.escape(k) for k in sorted(index, key=len, reverse=True)) + r')(?![\w-])', re.I) if index else None
    def target(model):
        matches = index.get(model.casefold(), [])
        if len(matches) != 1:
            skipped.append({'model': model, 'reason': '型号对应多个产品或没有唯一匹配'})
            return None
        p = matches[0]
        if str(p.get('id')) == str(article_id):
            skipped.append({'model': model, 'reason': '跳过自身链接'})
            return None
        url = p.get('front_url', '')
        if not local_url(url, base):
            skipped.append({'model': model, 'reason': '没有已验证的站内地址'})
            return None
        return url
    for node in doc.find('a'):
        label = BeautifulSoup(doc.inner(node), 'html.parser').get_text().strip()
        labels = list(dict.fromkeys(m.group().casefold() for m in pattern.finditer(label))) if pattern else []
        if not labels:
            continue
        if len(labels) != 1:
            skipped.append({'model':label, 'reason':'链接文字包含多个型号，未自动修改'})
            continue
        model = labels[0]
        old = node['attrs'].get('href', '')
        if old and urlsplit(urljoin(base, old)).hostname != urlsplit(base).hostname:
            continue
        url = target(model)
        if url and old != url:
            edits.append((node['start'], node['open'], rewrite_attributes(source[node['start']:node['open']], {'href': url})))
            changes.append({'model': model, 'old': old, 'new': url, 'kind': '更新'})
    if add and index:
        for offset, data, parents in doc.texts:
            if any(doc.nodes[i]['tag'] in ('a','script','style','textarea','title','code','pre','button','h1','h2','h3','h4','h5','h6') for i in parents):
                continue
            for match in pattern.finditer(data):
                model = match.group()
                url = target(model)
                if url:
                    edits.append((offset+match.start(),offset+match.end(),'<a href="'+escape(url, quote=True)+'">'+model+'</a>'))
                    changes.append({'model': model, 'old': '', 'new': url, 'kind': '新增'})
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source, changes, skipped


def save_backup(root, record):
    root.mkdir(parents=True, exist_ok=True)
    path = root / (record['backup_id'] + '.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def execute(client, plan, root, ctx, restore=False):
    results = []
    for row_number, row in enumerate(plan['rows']):
        try:
            ctx.check_cancelled()
        except Exception:
            results.extend({'id':r['id'], 'status':'未执行', 'msg':'任务已停止'} for r in plan['rows'][row_number:])
            break
        expected = row['after'] if restore else row['before']
        replacement = row['before'] if restore else row['after']
        try:
            token, descriptors, current = client.get_edit_form(row['id'], row['mcode'], edit_url=row['edit_url'])
        except Exception as exc:
            results.append({'id':row['id'], 'status':'失败', 'msg':'读取失败，未提交：'+str(exc)})
            continue
        if not descriptors or current.get('content') != expected:
            results.append({'id': row['id'], 'status': '跳过', 'msg': '正文已变化或无法读取，未覆盖'})
            continue
        if not restore and (current.get('date') != row.get('date') or str(current.get('scode')) != str(row.get('scode'))):
            results.append({'id':row['id'], 'status':'跳过', 'msg':'发布时间或栏目已变化，请重新预览'})
            continue
        # Preserve all current fields, and reject a concurrent edit before POST.
        baseline = {k:deepcopy(v) for k,v in current.items() if k not in TRANSPORT_FIELDS}
        if not restore:
            record = dict(row, backup_id=uuid.uuid4().hex, state='prepared')
            save_backup(root, record)  # Must succeed before any write.
        else:
            record = row
        try:
            ok, msg = client.edit_content(row['id'], row['mcode'], {'content':replacement},
                edit_url_hint=row['edit_url'], expected_fields=baseline, refresh_publish_date=False)
        except Exception as exc:
            record['state'] = 'review'
            save_backup(root, record)
            results.append({'id':row['id'], 'status':'待核对', 'msg':str(exc)})
            results.extend({'id':r['id'], 'status':'未执行', 'msg':'前一条结果待核对'} for r in plan['rows'][row_number+1:])
            break
        outcome = getattr(client, 'last_write_result', {})
        if ok:
            record['state'] = 'restored' if restore else 'applied'
        elif outcome.get('write_attempted'):
            record['state'] = 'review'
        else:
            record['state'] = 'not_sent'
        save_backup(root, record)
        results.append({'id':row['id'], 'status':'已恢复' if ok and restore else '成功' if ok else '待核对' if record['state']=='review' else '失败', 'msg':msg})
        if record['state'] == 'review':
            results.extend({'id':r['id'], 'status':'未执行', 'msg':'前一条结果待核对'} for r in plan['rows'][row_number+1:])
            break  # Do not continue after an uncertain write.
    return results
