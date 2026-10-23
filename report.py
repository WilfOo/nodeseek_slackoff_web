#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: WTFNMFPL
"""
报表中心 - 本地只读数据服务

把论坛内容整理成"对账明细"结构，供 index.html 渲染。
仅本机使用，默认只监听 127.0.0.1。

用法:
    python3 report.py                 # http://127.0.0.1:8765
    python3 report.py --port 9000
    python3 report.py --host 0.0.0.0 --token abc123   # 局域网/隧道访问
"""

import argparse
import base64
import html as htmllib
import ipaddress
import json
import os
import random
import re
import sys
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE = "https://www.nodeseek.com"
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
CFG_DIR = os.path.join(os.path.expanduser("~"), ".config", "report-center")
SESSION_FILE = os.path.join(CFG_DIR, "session.json")

CATEGORIES = [
    ("daily", "日常"), ("tech", "技术"), ("info", "情报"), ("review", "测评"),
    ("trade", "交易"), ("carpool", "拼车"), ("promotion", "推广"), ("life", "生活"),
    ("dev", "Dev"), ("photo-share", "贴图"), ("expose", "曝光"),
    ("sandbox", "沙盒"), ("meaningless", "无意义"),
]
CAT_NAME = dict(CATEGORIES)


# --------------------------------------------------------------------------
# 极简 HTML 树解析（标准库，无第三方依赖）
# --------------------------------------------------------------------------
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.kids = []
        self.parent = parent

    def cls(self):
        return self.attrs.get("class", "") or ""


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].kids.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].kids.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def parse_html(text):
    t = Tree()
    t.feed(text)
    t.close()
    return t.root


def walk(node):
    for k in node.kids:
        if isinstance(k, Node):
            yield k
            yield from walk(k)


def has_cls(node, name):
    return name in node.cls().split()


def find_all(node, tag=None, cls=None):
    return [n for n in walk(node)
            if (tag is None or n.tag == tag) and (cls is None or has_cls(n, cls))]


def find(node, tag=None, cls=None):
    for n in walk(node):
        if (tag is None or n.tag == tag) and (cls is None or has_cls(n, cls)):
            return n
    return None


def text_of(node, skip_cls=("avatar-wrapper",)):
    if node is None:
        return ""
    out = []

    def rec(n):
        for k in n.kids:
            if isinstance(k, str):
                out.append(k)
            else:
                if k.tag in ("script", "style", "svg"):
                    continue
                if any(has_cls(k, c) for c in skip_cls):
                    continue
                rec(k)

    rec(node)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def num(s):
    m = re.search(r"[\d,]+", s or "")
    return int(m.group(0).replace(",", "")) if m else 0


# --------------------------------------------------------------------------
# 正文清洗：只保留"报表批注"风格的排版
# --------------------------------------------------------------------------
KEEP = {"p", "br", "strong", "b", "em", "i", "del", "s", "code", "pre",
        "ul", "ol", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "thead", "tbody", "tr", "th", "td", "hr", "div"}
DROP = {"script", "style", "svg", "iframe", "video", "audio", "noscript",
        "button", "form", "input", "select", "textarea", "use", "path"}


def sanitize(node):
    """把 article.post-content 变成一份朴素、无外链、无图的 HTML 片段"""
    out = []
    imgs = [0]

    def attr(name, val):
        return f' {name}="{htmllib.escape(str(val), quote=True)}"'

    def rec(n):
        if isinstance(n, str):
            out.append(htmllib.escape(n, quote=False))
            return
        tag = n.tag
        if tag in DROP:
            return
        if tag == "img":
            src = (n.attrs.get("src") or n.attrs.get("data-src") or "").strip()
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE + src
            if not src.startswith("http"):
                return
            imgs[0] += 1
            alt = htmllib.escape(n.attrs.get("alt") or "", quote=True)
            out.append(f'<span class="imgph" data-src="/api/img?u={quote(src, safe="")}" '
                       f'data-alt="{alt}"><i></i>图{imgs[0]}</span>')
            return
        if tag == "a":
            t = text_of(n)
            href = (n.attrs.get("href") or "").strip()
            jumped = False
            m = re.search(r"[?&]to=([^&]+)", href)
            if m:
                href = unquote(m.group(1)).strip()
                jumped = True
            # 图片链接只保留图片本身，不额外套一层文字链接
            if not t and any(isinstance(k, Node) and k.tag == "img" for k in n.kids):
                for k in n.kids:
                    rec(k)
                return
            pm = re.match(r"^(?:https?://(?:www\.)?nodeseek\.com)?(/post-(\d+)-(\d+))", href)
            if pm:
                fm = re.search(r"#(\d+)$", href)
                out.append(
                    f'<a class="lnk inpost" href="{BASE}{pm.group(1)}" data-post="{pm.group(2)}" '
                    f'data-page="{pm.group(3)}" data-floor="{fm.group(1) if fm else 0}" '
                    f'target="_blank" rel="noopener noreferrer nofollow">'
                    f'{htmllib.escape(t) or "帖子"}</a>')
                return
            if href.startswith("/") and not jumped:
                if t:
                    out.append(htmllib.escape(t))
                return
            if not re.match(r"^https?://", href):
                if t:
                    out.append(htmllib.escape(t))
                return
            label = "[链接]" if re.match(r"^(https?://|www\.)", t) else (t or "链接")
            out.append(
                f'<a class="lnk" href="{htmllib.escape(href, quote=True)}" target="_blank" '
                f'rel="noopener noreferrer nofollow">{htmllib.escape(label)}</a>')
            return
        if tag in ("span", "font", "u", "small", "big", "figure", "figcaption"):
            for k in n.kids:
                rec(k)
            return
        if tag in ("br", "hr"):
            out.append(f"<{tag}>")
            return
        if tag in KEEP:
            out.append(f"<{tag}>")
            for k in n.kids:
                rec(k)
            out.append(f"</{tag}>")
            return
        for k in n.kids:
            rec(k)

    for k in node.kids:
        rec(k)
    s = "".join(out)
    s = re.sub(r"(?:<p>\s*</p>)+", "", s)
    s = re.sub(r"<div>\s*</div>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# --------------------------------------------------------------------------
# 会话（把论坛的登录 Cookie 存在服务端）
# --------------------------------------------------------------------------
class AuthError(Exception):
    pass


class RestrictedPost(Exception):
    """上游返回 404，但页面里写着权限说明（需要登录 / 需要等级）"""

    def __init__(self, body):
        super().__init__("restricted")
        self.body = body


class SessionStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.cookie = ""
        self.user = None
        self.ts = 0
        self.load()

    def load(self):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.cookie = d.get("cookie", "") or ""
            self.user = d.get("user")
            self.ts = d.get("ts", 0)
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        os.makedirs(CFG_DIR, exist_ok=True)
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cookie": self.cookie, "user": self.user, "ts": self.ts},
                      f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSION_FILE)

    def set(self, cookie, user):
        with self.lock:
            self.cookie, self.user, self.ts = cookie, user, int(time.time())
            self.save()

    def clear(self):
        with self.lock:
            self.cookie, self.user, self.ts = "", None, 0
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass

    @property
    def active(self):
        return bool(self.cookie)


STORE = SessionStore()
_auth_lock = threading.Lock()
_auth_sess = {"cookie": "", "s": None}


def auth_session():
    with _auth_lock:
        if _auth_sess["s"] is not None and _auth_sess["cookie"] == STORE.cookie:
            return _auth_sess["s"]
        s = creq.Session(impersonate=_active_imp[0],
                         headers={**HEADERS, "Cookie": STORE.cookie,
                                  "Origin": BASE, "Referer": BASE + "/"},
                         timeout=25)
        _auth_sess.update(cookie=STORE.cookie, s=s)
        return s


def whoami(cookie):
    """用给定 Cookie 取一次首页，解出 temp-script 里的 user"""
    s = creq.Session(impersonate=_active_imp[0],
                     headers={**HEADERS, "Cookie": cookie}, timeout=25)
    r = s.get(BASE + "/")
    if r.status_code != 200 or "Just a moment" in r.text[:2000]:
        raise RuntimeError("上游拒绝访问，稍后再试")
    m = re.search(r'<script[^>]*id="temp-script"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("页面结构变化，取不到会话信息")
    raw = re.sub(r"\s+", "", m.group(1))
    raw += "=" * (-len(raw) % 4)
    data = json.loads(base64.b64decode(raw).decode("utf-8"))
    u = data.get("user") or {}
    if not u:
        return None
    return {
        "name": (u.get("member_name") or u.get("name") or u.get("username")
                 or u.get("nickname") or "已认证"),
        "id": u.get("member_id") or u.get("memberId") or u.get("uid") or u.get("id") or "",
        "rank": u.get("rank", 0),
        "coin": u.get("coin", 0),
        "posts": u.get("nPost", 0),
        "comments": u.get("nComment", 0),
    }


def api_get(path):
    if not STORE.active:
        raise AuthError()
    r = auth_session().get(BASE + path, headers={
        "Referer": BASE + "/notification",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"上游返回 {r.status_code}")
    msg = str(data.get("message", "")) if isinstance(data, dict) else ""
    if msg.upper() == "USER NOT FOUND":
        STORE.clear()
        raise AuthError()
    return data


_unread_cache = {"t": 0, "v": None}


def _md_plain(text):
    """把站内系统消息里的 markdown 链接压成纯文本"""
    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", text or "")
    text = re.sub(r"https?://\S+", "[链接]", text)
    return re.sub(r"\s+", " ", text).strip()


def notify_items(tab, page=1):
    if tab == "msg":
        data = api_get(f"/api/notification/message/list?page={page}")
        rows = data.get("msgArray") or []
        out = [{
            "type": "sys",
            "id": r.get("max_id"),
            "from": r.get("sender_name") or "",
            "time": r.get("created_at") or "",
            "text": _md_plain(r.get("content")),
            "viewed": r.get("viewed", 1),
        } for r in rows]
        return out, len(rows)
    key = "at-me" if tab == "at" else "reply-to-me"
    data = api_get(f"/api/notification/{key}/list?page={page}")
    rows = data.get("data") or []
    out = [{
        "type": "post",
        "id": r.get("post_id"),
        "title": r.get("title") or "",
        "from": r.get("commenter_name") or "",
        "floor": r.get("floor_id") or 0,
        "time": r.get("created_at") or "",
        "viewed": r.get("viewed", 1),
    } for r in rows]
    return out, len(rows)


def api_post(path, payload, referer=None):
    if not STORE.active:
        raise AuthError()
    r = auth_session().post(BASE + path, json=payload, headers={
        "x-csrf-challenge": "simple-token",
        "Csrf-Token": "".join(random.choices(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=16)),
        "Referer": referer or (BASE + "/"),
        "Content-Type": "application/json",
    })
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {"success": False, "message": r.text[:200]}
    msg = str(data.get("message", "")) if isinstance(data, dict) else ""
    if msg.upper() == "USER NOT FOUND":
        STORE.clear()
        raise AuthError()
    if msg.lower() == "high risk action":
        raise RuntimeError("请求被风控拦截，请稍后重试或重新认证")
    return data


# 图片走本地代理，页面不直接连外站
_img_cache = {}
_img_bytes = [0]
_img_lock = threading.Lock()


def image_url_ok(u):
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    h = p.hostname.lower()
    if h in ("localhost", "localhost.localdomain") or h.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(h)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return True


def fetch_image(u):
    with _img_lock:
        hit = _img_cache.get(u)
        if hit and time.time() - hit[2] < 3600:
            return hit[0], hit[1]
    s = creq.Session(impersonate=_active_imp[0], timeout=30)
    r = s.get(u, headers={"Referer": BASE + "/",
                          "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
    if r.status_code != 200:
        raise RuntimeError(f"图片上游返回 {r.status_code}")
    body = r.content[:12 * 1024 * 1024]
    ct = (r.headers.get("content-type") or "image/png").split(";")[0].strip()
    if not ct.startswith("image/"):
        ct = "image/png"
    with _img_lock:
        if len(_img_cache) > 120 or _img_bytes[0] > 80 * 1024 * 1024:
            _img_cache.clear()
            _img_bytes[0] = 0
        _img_cache[u] = (body, ct, time.time())
        _img_bytes[0] += len(body)
    return body, ct


# --------------------------------------------------------------------------
# 抓取（带缓存）
# --------------------------------------------------------------------------
try:
    from curl_cffi import requests as creq
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install curl_cffi")

_local = threading.local()
_cache = {}
_cache_lock = threading.Lock()

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}


IMPERSONATIONS = ["chrome131", "chrome136", "chrome124"]
try:  # 只保留当前 curl_cffi 支持的指纹，避免运行期才炸
    from curl_cffi.requests.impersonate import BrowserType as _BT

    _VALID = {t.value for t in _BT}
    IMPERSONATIONS = [i for i in IMPERSONATIONS if i in _VALID] or ["chrome131"]
except Exception:  # noqa: BLE001
    pass
_active_imp = [IMPERSONATIONS[0]]


def session(imp=None):
    imp = imp or _active_imp[0]
    key = "_s_" + imp
    s = getattr(_local, key, None)
    if s is None:
        s = creq.Session(impersonate=imp, headers=HEADERS, timeout=25)
        setattr(_local, key, s)
    return s


def fetch(path, ttl=90, auth=False):
    """取页面文本；有会话时优先用会话取（能看到登录可见的帖子）"""
    use_auth = bool(auth and STORE.active)
    key = ("u|" if use_auth else "") + path
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]

    body, errs, missing_body = None, [], None
    order = (["auth"] if use_auth else []) + IMPERSONATIONS
    for tag in order:
        try:
            sess = auth_session() if tag == "auth" else session(tag)
            r = sess.get(BASE + path)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{tag}: 连接失败 {e}")
            continue
        if r.status_code in (404, 410):
            if missing_body is None:
                missing_body = r.text
            continue
        txt = r.text
        if r.status_code == 200 and "Just a moment" not in txt[:2000]:
            body = txt
            if tag != "auth":
                _active_imp[0] = tag
            break
        errs.append(f"{tag}: HTTP {r.status_code}")

    if body is None:
        if missing_body is not None:
            raise RestrictedPost(missing_body)
        raise RuntimeError("；".join(errs) or "上游不可用")

    with _cache_lock:
        _cache[key] = (now, body)
        if len(_cache) > 200:
            for k in sorted(_cache, key=lambda x: _cache[x][0])[:80]:
                _cache.pop(k, None)
    return body


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def list_path(page, cat, sort):
    q = f"?sortBy={sort}" if sort else ""
    if cat:
        p = f"/categories/{cat}" + (f"/page-{page}" if page > 1 else "")
    else:
        p = f"/page-{page}" if page > 1 else "/"
    return p + q


def parse_list(body):
    root = parse_html(body)
    items = []
    for li in find_all(root, "li", "post-list-item"):
        a = None
        for cand in find_all(li, "a"):
            if cand.attrs.get("href", "").startswith("/post-"):
                a = cand
                break
        if not a:
            continue
        m = re.match(r"/post-(\d+)-", a.attrs["href"])
        if not m:
            continue
        pid = int(m.group(1))
        author = ""
        ai = find(li, None, "info-author")
        if ai:
            la = find(ai, "a")
            author = text_of(la) or li.attrs.get("data-author", "")
        cat_el = None
        for cand in find_all(li, "a"):
            if cand.attrs.get("href", "").startswith("/categories/"):
                cat_el = cand
                break
        t = find(li, "time")
        need_level = None
        title_div = find(li, "div", "post-title")
        if title_div is not None:
            for u in find_all(title_div, "use"):
                if (u.attrs.get("href") or u.attrs.get("xlink:href")) == "#lock":
                    span = u.parent
                    while span is not None and span.tag != "span":
                        span = span.parent
                    need_level = num(text_of(span)) if span is not None else 0
                    break
        items.append({
            "id": pid,
            "title": text_of(a),
            "author": author,
            "views": num(text_of(find(li, None, "info-views"))),
            "comments": num(text_of(find(li, None, "info-comments-count"))),
            "cat": text_of(cat_el) if cat_el else "",
            "cat_slug": (cat_el.attrs["href"].split("/categories/")[1].split("?")[0]
                         if cat_el else ""),
            "time": (t.attrs.get("title") or t.attrs.get("datetime") or "") if t else "",
            "pinned": any(has_cls(n, "pined") for n in walk(li)),
            "need_level": need_level,
            "last": text_of(find(li, None, "info-last-commenter")),
        })
    pages = 0
    for a in find_all(root, "a"):
        m = re.search(r"page-(\d+)", a.attrs.get("href", ""))
        if m:
            pages = max(pages, int(m.group(1)))
    return items, pages


def parse_locked(body):
    """识别 404 页面里的权限文案"""
    text = text_of(parse_html(body))
    m = re.search(r"需要\s*Lv\s*(\d+)", text)
    if "权限不足" in text and m:
        return {"type": "level", "level": int(m.group(1))}
    if "需要注册用户才能查看" in text:
        return {"type": "login"}
    if "权限不足" in text or "需要登录" in text:
        return {"type": "level", "level": int(m.group(1)) if m else 0}
    return {"type": "missing"}


def parse_post(body, pid):
    root = parse_html(body)
    post = find(root, "div", "nsk-post") or root

    h1 = find(post, "h1")
    title = text_of(h1)

    # 楼主楼层：<div id="0" class="content-item">
    op = None
    for n in walk(post):
        if has_cls(n, "content-item") and n.attrs.get("id") == "0":
            op = n
            break

    def block_of(node):
        author = text_of(find(node, None, "author-name"))
        t = find(node, "time")
        when = (t.attrs.get("title") or "") if t else ""
        art = find(node, "article", "post-content") or find(node, "article")
        return author, when, sanitize(art) if art else ""

    op_author, op_time, op_html = block_of(op) if op is not None else ("", "", "")

    cat = ""
    for a in find_all(post, "a"):
        if a.attrs.get("href", "").startswith("/categories/"):
            cat = text_of(a)
            break

    comments = []
    for li in find_all(root, "li", "content-item"):
        floor_el = find(li, None, "floor-link")
        author, when, htm = block_of(li)
        comments.append({
            "floor": text_of(floor_el).lstrip("#"),
            "author": author,
            "time": when,
            "html": htm,
        })

    pages = 1
    for a in find_all(root, "a"):
        m = re.search(rf"/post-{pid}-(\d+)", a.attrs.get("href", ""))
        if m:
            pages = max(pages, int(m.group(1)))

    # 正文开头若重复了标题，去掉，免得「摘要」与「说明」两处一样
    if op_html:
        m = re.match(r"\s*<h([1-3])>(.*?)</h\1>\s*", op_html, re.S)
        if m:
            def norm(x):
                x = re.sub(r"<[^>]+>", "", x)
                x = re.sub(r"^[#\s]+", "", x)
                return re.sub(r"\s+", "", x)
            if norm(m.group(2)) == norm(title):
                op_html = op_html[m.end():]

    return {
        "id": pid,
        "title": title,
        "author": op_author,
        "time": op_time,
        "cat": cat,
        "html": op_html,
        "comments": comments,
        "pages": pages,
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
TOKEN = ""
BOUND_HOST = "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "ReportCenter/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not (extra and "Cache-Control" in extra):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", {"Access-Control-Allow-Origin": "*"})

    def _ok(self):
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("k", [""])[0] == TOKEN:
            return True
        if self.headers.get("X-Key") == TOKEN:
            return True
        self._json({"ok": False, "error": "E-4011 会话令牌无效"}, 403)
        return False

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._index()
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if not self._ok():
                return
            if u.path == "/api/list":
                return self._list(q)
            if u.path == "/api/post":
                return self._post(q)
            if u.path == "/api/img":
                return self._img(q)
            if u.path == "/api/me":
                return self._json({"ok": True, "user": STORE.user, "active": STORE.active})
            if u.path == "/api/notify":
                return self._notify(q)
            if u.path == "/api/notify/unread":
                return self._notify_unread()
            if u.path == "/api/ping":
                return self._json({"ok": True, "t": int(time.time())})
            self._json({"ok": False, "error": "E-4040 未知资源"}, 404)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            sys.stderr.write(f"  !! {msg}\n")
            if "不存在" in msg or "登录后" in msg:
                return self._json({"ok": False, "error": "E-4041 " + msg}, 404)
            self._json({"ok": False, "error": f"E-5001 数据源异常：{msg}"}, 502)

    # -- 写操作 ----------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        if not self._ok():
            return
        if not self._same_origin():
            return self._json({"ok": False, "error": "E-4030 来源校验失败"}, 403)
        try:
            if u.path == "/api/login":
                return self._login()
            if u.path == "/api/logout":
                STORE.clear()
                return self._json({"ok": True})
            if u.path == "/api/checkin":
                return self._checkin()
            if u.path == "/api/reply":
                return self._reply()
            if u.path == "/api/newpost":
                return self._newpost()
            if u.path == "/api/notify/read":
                return self._notify_read()
            self._json({"ok": False, "error": "E-4040 未知资源"}, 404)
        except AuthError:
            self._json({"ok": False, "error": "E-4010 会话已失效，请重新认证", "auth": False}, 401)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"  !! POST {u.path} {e}\n")
            self._json({"ok": False, "error": f"E-5002 操作失败：{e}"}, 502)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _same_origin(self):
        o = self.headers.get("Origin")
        if not o:
            return True
        host = (urlparse(o).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", BOUND_HOST.lower())

    def _login(self):
        d = self._body()
        cookie = (d.get("cookie") or "").strip()
        if "=" not in cookie:
            return self._json({"ok": False, "error": "E-4002 凭据格式不对"}, 400)
        if len(cookie) > 4000:
            return self._json({"ok": False, "error": "E-4003 凭据过长"}, 400)
        try:
            user = whoami(cookie)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"E-5012 校验失败：{e}"}, 502)
        if not user:
            return self._json({"ok": False, "error": "E-4012 凭据无效或已过期"}, 401)
        STORE.set(cookie, user)
        return self._json({"ok": True, "user": user})

    def _checkin(self):
        if not STORE.active:
            raise AuthError()
        data = api_post("/api/attendance?random=true", {}, referer=BASE + "/board")
        msg = str(data.get("message") or "")
        got = re.search(r"(\d+)\s*(?:个)?(?:鸡腿|腿)", msg)
        return self._json({"ok": bool(data.get("success")), "raw": msg,
                           "got": int(got.group(1)) if got else 0})

    def _reply(self):
        d = self._body()
        pid = int(d.get("postId") or 0)
        content = (d.get("content") or "").strip()
        if not pid or not content:
            return self._json({"ok": False, "error": "E-4004 内容为空"}, 400)
        if len(content) > 20000:
            return self._json({"ok": False, "error": "E-4005 内容过长"}, 400)
        if not STORE.active:
            raise AuthError()
        data = api_post("/api/content/new-comment",
                        {"content": content, "mode": "new-comment", "postId": pid},
                        referer=f"{BASE}/post-{pid}-1")
        return self._json({"ok": bool(data.get("success")),
                           "message": data.get("message") or ""})

    def _newpost(self):
        d = self._body()
        title = (d.get("title") or "").strip()
        content = (d.get("content") or "").strip()
        cat = (d.get("category") or "").strip()
        if not title or not content:
            return self._json({"ok": False, "error": "E-4006 标题或内容为空"}, 400)
        if cat not in CAT_NAME:
            return self._json({"ok": False, "error": "E-4007 业务类型无效"}, 400)
        # 阅读限制：0=公开，255=私有，1..自己等级=对应 Lv
        urank = int((STORE.user or {}).get("rank") or 0)
        try:
            rank = int(d.get("rank") or 0)
        except (TypeError, ValueError):
            rank = 0
        if rank not in {0, 255} and not (1 <= rank <= urank):
            return self._json({"ok": False,
                               "error": "E-4008 当前账号不支持该阅读限制"}, 400)
        if not STORE.active:
            raise AuthError()
        data = api_post("/api/content/new-discussion",
                        {"content": content, "mode": "new-discussion",
                         "title": title, "category": cat, "rank": rank},
                        referer=f"{BASE}/new-discussion")
        return self._json({"ok": bool(data.get("success")),
                           "message": data.get("message") or "",
                           "id": data.get("data") or data.get("postId") or ""})

    def _notify(self, q):
        if not STORE.active:
            raise AuthError()
        tab = q.get("tab") or "at"
        if tab not in ("at", "reply", "msg"):
            tab = "at"
        page = max(1, int(q.get("page") or 1))
        items, n = notify_items(tab, page)
        return self._json({"ok": True, "tab": tab, "page": page,
                           "items": items, "has_more": n > 0})

    def _notify_unread(self):
        if not STORE.active:
            return self._json({"ok": True, "active": False,
                               "counts": {"atMe": 0, "reply": 0, "message": 0, "all": 0}})
        if _unread_cache["v"] and time.time() - _unread_cache["t"] < 20:
            c = _unread_cache["v"]
        else:
            data = api_get("/api/notification/unread-count")
            c = data.get("unreadCount") or {}
            _unread_cache.update(t=time.time(), v=c)
        return self._json({"ok": True, "active": True, "counts": {
            "atMe": c.get("atMe", 0), "reply": c.get("reply", 0),
            "message": c.get("message", 0), "all": c.get("all", 0)}})

    def _notify_read(self):
        _unread_cache["v"] = None
        d = self._body()
        tab = d.get("tab") or ""
        paths = {"at": "/api/notification/at-me/markViewedComments",
                 "reply": "/api/notification/reply-to-me/markViewedComments",
                 "msg": "/api/notification/message/markViewed"}
        if tab not in paths:
            return self._json({"ok": False, "error": "E-4009 参数不对"}, 400)
        if not STORE.active:
            raise AuthError()
        try:
            api_post(paths[tab], {}, referer=BASE + "/notification")
        except RuntimeError:
            pass
        return self._json({"ok": True})

    def _img(self, q):
        u = unquote(q.get("u") or "")
        if not u or not image_url_ok(u):
            return self._json({"ok": False, "error": "E-4008 地址不接受"}, 400)
        try:
            body, ct = fetch_image(u)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"E-5013 取图失败：{e}"}, 502)
        self._send(200, body, ct, {"Cache-Control": "public, max-age=3600"})

    def _index(self):
        try:
            with open(INDEX, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._send(500, "index.html 缺失", "text/plain; charset=utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _list(self, q):
        page = max(1, int(q.get("page") or 1))
        cat = q.get("cat") or ""
        sort = q.get("sort") or "replyTime"
        if cat and cat not in CAT_NAME:
            cat = ""
        if sort not in ("replyTime", "createTime"):
            sort = "replyTime"
        body = fetch(list_path(page, cat, sort), ttl=90, auth=True)
        items, pages = parse_list(body)
        total_pages = pages or page
        self._json({
            "ok": True,
            "page": page,
            "pages": max(total_pages, page),
            "has_next": bool(pages and page < pages) or (not pages and len(items) > 0),
            "items": items,
            "ts": int(time.time()),
        })

    def _post(self, q):
        pid = int(q.get("id") or 0)
        page = max(1, int(q.get("page") or 1))
        if not pid:
            return self._json({"ok": False, "error": "E-4001 缺少单据号"}, 400)
        try:
            body = fetch(f"/post-{pid}-{page}", ttl=180, auth=True)
        except RestrictedPost as e:
            info = parse_locked(e.body)
            if info["type"] == "missing":
                return self._json({"ok": False, "error": "E-4041 单据不存在或已删除"}, 404)
            return self._json({"ok": True, "locked": True, "lock": info,
                               "id": pid, "page": page})
        data = parse_post(body, pid)
        data["ok"] = True
        data["page"] = page
        self._json(data)


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="报表中心 本地只读数据服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="", help="局域网/隧道访问时的共享令牌")
    a = ap.parse_args()
    TOKEN = a.token
    global BOUND_HOST
    BOUND_HOST = a.host

    if not os.path.exists(INDEX):
        sys.exit(f"找不到 {INDEX}")

    # 预热，顺便验证出口可用
    try:
        t0 = time.time()
        fetch("/", ttl=60)
        sys.stdout.write(f"数据源连通，耗时 {time.time() - t0:.2f}s\n")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"警告：数据源暂时不可用（{e}），仍继续启动\n")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/"
    if TOKEN:
        url += f"?k={TOKEN}"
    if STORE.active:
        try:
            fresh = whoami(STORE.cookie)
            if fresh:
                STORE.set(STORE.cookie, fresh)
            else:
                STORE.clear()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"会话校验未完成：{e}（沿用本地凭据）\n")

    if STORE.active and STORE.user:
        sys.stdout.write(f"会话：已认证 {STORE.user.get('name')}（成员 {STORE.user.get('id')}）\n")
    else:
        sys.stdout.write("会话：未认证（发帖/回帖/签到需要先在页面里认证）\n")
    sys.stdout.write(f"报表中心已就绪 -> {url}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\n已停止\n")


if __name__ == "__main__":
    main()
