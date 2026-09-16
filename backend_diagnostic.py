"""Read-only, redacted PbootCMS backend structure diagnostics."""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from client_utils import get_base_dir, _is_login_page
from constants import MCODE_ORDER
from http_transport import request_with_redirects
from exceptions import NetworkError


_SENSITIVE = re.compile(r"pass|pwd|token|formcheck|captcha|checkcode|secret", re.I)


def _safe_action_route(action):
    """保留 PbootCMS 的 ?p=/Content/... 路由，同时丢弃其他查询参数。"""
    parsed = urlparse(str(action or ""))
    p_route = (parse_qs(parsed.query).get("p") or [""])[0]
    path = parsed.path[-80:]
    if p_route:
        return f"{path}?p={p_route[:120]}"
    return path[-120:]


def _label_for(form, el, name):
    el_id = el.get("id", "")
    if el_id:
        label = form.find("label", {"for": el_id})
        if label:
            return label.get_text(" ", strip=True)[:100]
    item = el.find_parent(class_=re.compile(r"layui-form-item"))
    if item:
        label = item.find(class_=re.compile(r"layui-form-label"))
        if label:
            return label.get_text(" ", strip=True)[:100]
    row = el.find_parent("tr")
    if row:
        cell = el.find_parent(["td", "th"])
        cells = row.find_all(["td", "th"], recursive=False)
        if cell in cells and cells.index(cell) > 0:
            return cells[cells.index(cell) - 1].get_text(" ", strip=True)[:100]
    return name


def _field_value(el):
    if el.name == "textarea":
        return el.get_text() or ""
    if el.name == "select":
        selected = el.find("option", selected=True) or el.find("option")
        return selected.get("value", "") if selected else ""
    return el.get("value", "") or ""


def _pics_shape(raw):
    value = str(raw or "").strip()
    if not value:
        return {"storage": "empty", "item_count": 0}
    if value[:1] in "[{":
        try:
            parsed = json.loads(value)
            return {"storage": "json", "item_count": len(parsed) if hasattr(parsed, "__len__") else 1}
        except Exception:
            pass
    if "\n" in value or "\r" in value:
        parts = [x for x in value.splitlines() if x.strip()]
        return {"storage": "newline", "item_count": len(parts)}
    for sep, name in ((",", "comma"), ("|", "pipe"), (";", "semicolon")):
        if sep in value:
            return {"storage": name, "item_count": len([x for x in value.split(sep) if x.strip()])}
    return {"storage": "single", "item_count": 1}


def parse_form_structure(html):
    """Return field metadata and redacted media storage observations."""
    soup = BeautifulSoup(html or "", "html.parser")
    forms = soup.find_all("form")
    form = (soup.find("form", {"id": re.compile(r"add|edit", re.I)})
            or next((f for f in forms if "Content/" in (f.get("action") or "")), None)
            or (forms[0] if forms else None))
    if not form:
        return {"found": False, "fields": [], "form_count": len(forms)}
    fields, seen = [], set()
    media = {}
    for el in form.find_all(["input", "textarea", "select"]):
        name = (el.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        field_type = (el.get("type") or el.name).lower()
        verify = str(el.get("lay-verify", "") or "")
        required = bool(el.has_attr("required") or el.get("aria-required") == "true"
                        or "required" in verify.lower())
        item = {
            "name": name,
            "tag": el.name,
            "type": field_type,
            "label": _label_for(form, el, name),
            "required": required,
            "multiple": bool(el.has_attr("multiple")),
            "readonly": bool(el.has_attr("readonly")),
            "disabled": bool(el.has_attr("disabled")),
        }
        if el.name == "select":
            options = el.find_all("option")
            item["option_count"] = len(options)
        for attr in ("accept", "maxlength", "min", "max", "step"):
            if el.get(attr) is not None:
                item[attr] = str(el.get(attr))[:100]
        raw = _field_value(el)
        item["has_default"] = bool(raw) and not _SENSITIVE.search(name)
        if name == "pics":
            media["pics"] = _pics_shape(raw)
        elif name == "ico":
            media["ico"] = {"has_existing_value": bool(raw)}
        fields.append(item)
    signature_data = [(x["name"], x["tag"], x["type"], x["required"]) for x in fields]
    signature = hashlib.sha256(
        json.dumps(signature_data, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return {
        "found": True,
        "action_route": _safe_action_route(form.get("action") or ""),
        "method": (form.get("method") or "get").lower(),
        "field_count": len(fields),
        "fields": fields,
        "media_observations": media,
        "structure_signature": signature,
        "form_count": len(forms),
    }


def _get(client, route, timeout=30, params=None):
    url = client._url(route)
    last = None
    for _attempt in range(2):
        try:
            # Diagnostics are read-only, but they still carry authenticated
            # cookies. Follow only the same-origin/browser-safe redirect
            # chain so a WAF/SSO/error host cannot receive the session cookie.
            response = request_with_redirects(
                client.session, "GET", url, params=params,
                timeout=timeout, max_redirects=20)
            break
        except NetworkError:
            # A rejected source/redirect is a deterministic safety decision,
            # not a transient network failure; do not issue the same
            # authenticated probe a second time.
            raise
        except Exception as exc:
            last = exc
    else:
        raise last
    response.raise_for_status()
    if _is_login_page(response.text):
        raise PermissionError("登录会话已失效")
    return response


def _discover_models(home_html):
    soup = BeautifulSoup(home_html or "", "html.parser")
    models = {}
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"mcode/(\d+)", anchor.get("href", ""))
        if match:
            models.setdefault(match.group(1), anchor.get_text(" ", strip=True)[:80])
    return models


def _category_summary(html):
    soup = BeautifulSoup(html or "", "html.parser")
    nodes = []
    for row in soup.find_all("tr"):
        checkbox = row.find("input", {"name": "list[]"})
        if not checkbox:
            continue
        cid = (checkbox.get("value") or "").strip()
        if not cid.isdigit():
            continue
        nodes.append({
            "id": cid,
            "parent": (row.get("data-tt-parent-id") or "").strip(),
        })
    return {
        "count": len(nodes),
        "root_count": sum(1 for n in nodes if not n["parent"] or n["parent"] == "0"),
        "max_depth_estimate": _estimate_depth(nodes),
    }


def _estimate_depth(nodes):
    parent = {x["id"]: x["parent"] for x in nodes}
    best = 0
    for node in nodes:
        depth, cur, seen = 1, node["id"], set()
        while parent.get(cur) and parent[cur] != "0" and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
            depth += 1
        best = max(best, depth)
    return best


def _selected_scodes(html):
    soup = BeautifulSoup(html or "", "html.parser")
    select = soup.find("select", {"name": "scode"})
    if not select:
        return []
    return [(opt.get("value") or "").strip() for opt in select.find_all("option")
            if (opt.get("value") or "").strip().isdigit()]


def _first_content_row(html, base_url):
    soup = BeautifulSoup(html or "", "html.parser")
    for row in soup.find_all("tr"):
        aid = ""
        for field in row.find_all("input"):
            value = (field.get("value") or "").strip()
            if value.isdigit():
                aid = value
                break
        if not aid:
            continue
        edit_url, preview = "", ""
        for anchor in row.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            low = href.lower()
            if ("content/mod" in low and f"id/{aid}" in low
                    and "field/" not in low and "field=" not in low):
                edit_url = href
            if ("?p=" not in low and "content/" not in low and "static/" not in low
                    and (href.endswith(".html") or re.search(rf"/{aid}/?$", href))):
                preview = urljoin(base_url.rstrip("/") + "/", href)
        return {"id": aid, "edit_url": edit_url, "preview_url": preview}
    return {}


def _url_pattern(url):
    if not url:
        return "unknown"
    path = urlparse(url).path
    path = re.sub(r"/\d+(?=\.html?$|/$)", "/{id}", path)
    return re.sub(r"[^/]+(?=\.html?$)", "{slug_or_id}", path)


def diagnose_backend(client, ctx=None):
    """Scan authenticated backend pages using GET requests only."""
    ctx = ctx or type("Ctx", (), {"log": lambda *_: None, "progress": lambda *_: None,
                                  "check_cancelled": lambda *_: None})()
    report = {
        "diagnostic_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "site": {
            "origin": client.base_url,
            "admin_path": urlparse(client.admin_url).path,
            "site_key": client.site_key,
        },
        "models": [],
        "errors": [],
    }
    ctx.log("结构诊断：验证登录态")
    home = _get(client, "Index/home", 30)
    discovered = _discover_models(home.text)
    report["backend"] = {
        "resolved_admin_path": urlparse(home.url).path,
        "model_links_found": len(discovered),
    }
    try:
        ctx.log("结构诊断：读取栏目树")
        categories = _get(client, "ContentSort/index", 90)
        report["categories"] = _category_summary(categories.text)
    except Exception as exc:
        report["errors"].append({"scope": "categories", "error": type(exc).__name__})

    model_codes = list(discovered)
    if not model_codes:
        model_codes = list(MCODE_ORDER)
    total = max(1, len(model_codes))
    for index, mcode in enumerate(model_codes, 1):
        ctx.check_cancelled()
        ctx.log(f"结构诊断：扫描内容模型 mcode={mcode}")
        item = {"mcode": mcode, "menu_label": discovered.get(mcode, "")}
        add_html = ""
        scodes = []
        try:
            add_response = _get(client, f"Content/add/mcode/{mcode}", 30)
            add_html = add_response.text
            item["standalone_add_form"] = parse_form_structure(add_html)
            scodes = _selected_scodes(add_html)
        except Exception as exc:
            item["standalone_add_error"] = type(exc).__name__
        try:
            listing = _get(client, f"Content/index/mcode/{mcode}", 30)
            item["embedded_add_form"] = parse_form_structure(listing.text)
            if not scodes:
                scodes = _selected_scodes(listing.text)
            sample = _first_content_row(listing.text, client.base_url)
            item["list_page"] = {
                "has_sample": bool(sample),
                "preview_url_pattern": _url_pattern(sample.get("preview_url", "")),
            }
            if sample:
                edit_url = sample.get("edit_url")
                if edit_url:
                    if edit_url.startswith("?"):
                        edit_abs = client.admin_url + edit_url
                    else:
                        edit_abs = urljoin(client.base_url + "/", edit_url)
                else:
                    edit_abs = client._url(
                        f"Content/mod/mcode/{mcode}/id/{sample['id']}")
                edit_response = request_with_redirects(
                    client.session, "GET", edit_abs, timeout=30,
                    max_redirects=20)
                edit_response.raise_for_status()
                if _is_login_page(edit_response.text):
                    raise PermissionError("登录会话已失效")
                item["edit_form"] = parse_form_structure(edit_response.text)
        except Exception as exc:
            item["list_or_edit_error"] = type(exc).__name__
        item["category_option_count"] = len(scodes)
        item["representative_scode"] = scodes[0] if scodes else ""
        if scodes:
            try:
                category_add = _get(client, f"Content/add/mcode/{mcode}", 30,
                                    params={"scode": scodes[0]})
                item["category_add_form"] = parse_form_structure(category_add.text)
            except Exception as exc:
                item["category_add_error"] = type(exc).__name__
        candidates = [item.get("category_add_form"), item.get("standalone_add_form"),
                      item.get("embedded_add_form")]
        candidates = [x for x in candidates if x and x.get("found")]
        if candidates:
            item["add_form"] = max(candidates, key=lambda x: x.get("field_count", 0))
        if scodes:
            try:
                try:
                    sort_form = _get(
                        client,
                        f"ContentSort/mod/mcode/{mcode}/id/{scodes[0]}", 30)
                except Exception:
                    # 兼容不要求 mcode 的旧路由；两者都只读。
                    sort_form = _get(client, f"ContentSort/mod/id/{scodes[0]}", 30)
                parsed = parse_form_structure(sort_form.text)
                names = {x["name"] for x in parsed.get("fields", [])}
                item["category_url_fields"] = sorted(
                    names & {"filename", "outlink", "urlname", "type", "outtype"})
            except Exception as exc:
                item["category_form_error"] = type(exc).__name__
        report["models"].append(item)
        ctx.progress(index, total, f"模型 {mcode}")
    report["structure_family"] = hashlib.sha256(
        json.dumps([(x["mcode"], x.get("add_form", {}).get("structure_signature", ""),
                    x.get("edit_form", {}).get("structure_signature", ""))
                   for x in report["models"]], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return report


def save_report(report):
    root = Path(get_base_dir()) / "backend_diagnostics"
    root.mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^0-9A-Za-z.-]+", "_",
                  urlparse(report.get("site", {}).get("origin", "")).netloc or "site")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = root / f"backend_structure_{host}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
