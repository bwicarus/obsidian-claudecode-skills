"""html_reader.py — 统一 HTML 阅读器(架构验收域)。

HTML/Markdown 内容渲进主文档 + HtmlAdapter + 共享 rc-*.js,证明「给一个新阅读器写个
adapter,共享功能(选区/查词/翻译/解释/对话/笔记/制卡/高亮)就全有」。AI 端点
(translate/explain/dict)内容无关,仍在 pdf_reader.py,直接复用。

2026-07-06 结构拆分第 2 刀(体检 structure:域自包含,块外零引用)。外部依赖经
register_html_reader 注入(safe_vault_path/obsidian_root/claude_dir),避免循环 import。
用法(pdf_reader.py):
    from html_reader import register_html_reader
    register_html_reader(bp, safe_vault_path=_safe_vault_path,
                         obsidian_root=OBSIDIAN_ROOT, claude_dir=CLAUDE_DIR)
路由:GET /pdf/html/view(?file=<vault-rel .html/.md>,无 file → 内置 sample)
     GET/POST/PATCH/DELETE /pdf/api/html-highlights(字符偏移锚 sidecar CRUD)
部署:cp 本文件到 /home/bwicarus/webapp/(跟 pdf_reader.py 同目录)+ restart webapp。
"""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import re
import threading
from flask import abort, jsonify, make_response, redirect, render_template, request, session, Response
from urllib.parse import quote as _q, quote, urljoin, urlparse, parse_qs
from html import escape as _hesc

# register_html_reader 注入(模块 import 时为 None,注册后可用)
_safe_vault_path = None   # callable: vault 相对路径 → 安全绝对 Path 或 None
_OBSIDIAN_ROOT = None     # Path: vault 根
_HTML_HL_DIR = None       # Path: state/html-highlights/(高亮 sidecar 目录)

# 内置 sample(无 file 时渲它,方便验收):中英文混排 + 若干英文词够测选区/查词/翻译/高亮。
_HTML_SAMPLE = """
<h1>统一 HTML 阅读器 · 架构验收 Demo</h1>
<p>这是一个<strong>最小 HTML 阅读器</strong>，用来证明统一控制层架构：HTML 内容直接渲在主文档里，
通过一个 <code>HtmlAdapter</code> 接入共享的 <code>rc-*.js</code> 控制层，于是
<em>选区 / 查词 / 翻译 / 解释 / 对话 / 笔记 / 制卡 / 高亮</em> 这些功能就全部可用——一行业务逻辑都不用重写。</p>
<h2>English paragraph for word lookup</h2>
<p>The fundamental theorem of calculus connects differentiation and integration. Select any
<b>sentence</b> here and tap translate or explain. Tap a single word like <i>derivative</i>,
<i>convergence</i>, or <i>eigenvalue</i> to open the offline dictionary popup.</p>
<p>You can also drag-select several words to get the multi-word toolbar (copy / translate / explain /
chat / note / anki / highlight). Highlights are persisted server-side by character offset.</p>
<h2>数学公式渲染</h2>
<p>行内公式如 $E = mc^2$ 与 $\\int_0^1 x^2\\,dx = \\tfrac{1}{3}$ 会被 MathJax 渲染。
选中一句中文也可以直接翻译或问 AI。</p>
<h2>段落选区与高亮</h2>
<p>选中这一段任意文字，底部会弹出工具栏；点「🖍 高亮」即按字符偏移存进
<code>state/html-highlights/</code> 的 sidecar，刷新后仍在。点已存在的高亮块可改色 / 加备注 / 删除。</p>
"""

# 仅最小白名单消毒所需的标签集(剥脚本/样式/事件,只留正文结构);允许的内联标签足够还原文档结构。
_HTML_ALLOWED = {
    "p", "div", "span", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "b", "i", "u",
    "a", "img", "br", "hr", "table", "thead", "tbody", "tr", "td", "th",
    "figure", "figcaption", "sup", "sub", "mark", "ruby", "rt", "rp", "small",
}
_HTML_DROP = ["script", "style", "iframe", "object", "embed", "link", "meta",
              "noscript", "head", "form", "input", "button", "svg", "video", "audio"]


def _html_js_v():
    """统一 HTML 阅读器静态(html-reader.js + 全套 rc-*)的 cache-bust 版本 = 各文件 mtime 最大值。
    跟 _epub_js_v 同构,只是把 epub 驱动换成 html-reader.js;rc-* 共享层任一改动也会 bust。"""
    mt = 0
    for name in ("html-reader.js", "rc-core.js", "rc-md.js", "rc-highlight.js",
                 "rc-snippets.js", "rc-result.js", "rc-wordpop.js", "rc-settings.js",
                 "rc-sidedrawer.js", "rc-phrasepop.js", "rc-assistant.js"):   # rc-assistant:2026-07-06 体检补(模板加载它却不在清单,immutable 下改它不 bust)
        for base in ("/var/www/html/static/pdf",
                     str(Path(__file__).resolve().parent / "static" / "pdf")):
            try:
                mt = max(mt, int(os.path.getmtime(os.path.join(base, name)))); break
            except Exception:
                continue
    return str(mt or 1)


def _md_to_html(text: str) -> str:
    """markdown → HTML(项目已装 markdown 3.x;装不上则 <pre> 包原文兜底)。"""
    try:
        import markdown
        return markdown.markdown(text, extensions=["extra", "sane_lists", "nl2br"])
    except Exception:
        import html as _h
        return "<pre style='white-space:pre-wrap;word-break:break-word'>" + _h.escape(text) + "</pre>"


def _sanitize_html_doc(raw: str) -> str:
    """最小白名单消毒(借鉴 _sanitize_epub_section 思路):剥危险标签 + 去事件/内联样式属性,
    未知标签拆壳保留文字。站内相对 href/src 去掉(最小版不解析相对路径),保留 http(s)/锚点/data:。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw or "", "html.parser")
    body = soup.find("body") or soup
    for tag in body.find_all(_HTML_DROP):
        tag.decompose()
    for tag in list(body.find_all(True)):
        if not tag.name or tag.parent is None:
            continue
        name = tag.name.lower()
        if name not in _HTML_ALLOWED:
            tag.unwrap(); continue
        for attr in list(tag.attrs):
            al = attr.lower()
            if al.startswith("on") or al == "style":
                del tag[attr]
            elif al == "href":
                href = tag.get("href") or ""
                if not (href.startswith("#") or href.startswith(("http://", "https://"))):
                    del tag["href"]
            elif al == "src":
                src = tag.get("src") or ""
                if not src.startswith(("http://", "https://", "data:")):
                    del tag["src"]
            elif al not in ("alt", "colspan", "rowspan", "class"):
                del tag[attr]
    return body.decode_contents()


# ── HTML 阅读器高亮 sidecar(字符偏移锚,独立 state/html-highlights/<sha>.json;照搬 epub-highlights 形态)──

def _html_hl_path(rel: str) -> Path:
    key = rel or "__html_sample__"
    return _HTML_HL_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def _html_hl_load(rel: str) -> list:
    try:
        return json.loads(_html_hl_path(rel).read_text("utf-8"))
    except Exception:
        return []


def _html_hl_save(rel: str, items: list):
    _HTML_HL_DIR.mkdir(parents=True, exist_ok=True)
    _html_hl_path(rel).write_text(json.dumps(items, ensure_ascii=False), "utf-8")


# ── 网页抓取 → 阅读器(2026-07-19,用户方向:浏览器 Copilot 的初版验证)────────────
# 路线:抓 URL → **Firefox 阅读模式同款算法**(readability-lxml)抽正文 → 走本文件既有的
# 白名单消毒 → 存 vault `资源/web/` → /pdf/html/view 打开 → 高亮/查词/AI 侧栏/翻译/
# 注意力埋点**全部白拿**(统一控制层红利)。存 vault=学习材料一等公民:Obsidian 全设备
# 同步、进 FTS 全局搜索、进概念网向前搜索。

_PRIVATE_NETS = ("127.", "10.", "192.168.", "169.254.", "0.")


def _url_safe(url: str) -> str:
    """SSRF 防护:只放行公网 http(s)。返回错误串,空=安全。"""
    from urllib.parse import urlparse
    import socket, ipaddress
    try:
        u = urlparse(url)
    except Exception:
        return "URL 无法解析"
    if u.scheme not in ("http", "https"):
        return "只支持 http/https"
    host = (u.hostname or "").lower()
    if not host or host in ("localhost",) or host.endswith((".local", ".ts.net", ".internal")):
        return "不允许内网地址"
    try:
        for ai in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(ai[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return "不允许内网地址"
    except Exception:
        return "域名解析失败"
    return ""


def _fetch_web_page(url: str) -> dict:
    """抓取 + Readability 抽正文 + 消毒 + 存 vault。返回 {ok, file, title} 或 {ok:False, error}。"""
    err = _url_safe(url)
    if err:
        return {"ok": False, "error": err}
    import requests
    try:
        r = requests.get(url, timeout=20, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"})
        r.raise_for_status()
        raw = b""
        for chunk in r.iter_content(65536):
            raw += chunk
            if len(raw) > 8 * 1024 * 1024:
                return {"ok": False, "error": "页面超过 8MB"}
        enc = r.encoding if (r.encoding and r.encoding.lower() != "iso-8859-1") else (r.apparent_encoding or "utf-8")
        html_raw = raw.decode(enc, errors="replace")
    except Exception as ex:
        return {"ok": False, "error": f"抓取失败:{str(ex)[:120]}"}
    try:
        from readability import Document          # Firefox 阅读模式算法的 Python 移植
        doc = Document(html_raw)
        title = (doc.short_title() or "").strip()[:80] or "网页"
        content = doc.summary(html_partial=True)
    except Exception:
        title, content = url[:80], html_raw       # 抽取失败 → 原文交给消毒(白名单会剥到只剩正文结构)
    clean = _sanitize_html_doc(content)
    if len((clean or "").strip()) < 200:
        return {"ok": False, "error": "没抽到正文(可能是纯 JS 渲染的页面,初版先不支持)"}
    import html as _h
    from urllib.parse import urlparse as _up2
    header = ("<p><small>🌐 来源:<a href=\"%s\">%s</a> · 抓取于 %s</small></p><hr>"
              % (_h.escape(url), _h.escape(_up2(url).netloc), time.strftime("%Y-%m-%d %H:%M")))
    body = "<h1>%s</h1>%s%s" % (_h.escape(title), header, clean)
    # 存 vault:资源/web/<slug>-<sha8>.html(重复抓同一 URL 覆盖同一文件=天然去重)
    import re as _re
    slug = _re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff-]+", "-", title).strip("-")[:48] or "page"
    sha8 = hashlib.sha1(url.encode()).hexdigest()[:8]
    rel = f"资源/web/{slug}-{sha8}.html"
    ap = _OBSIDIAN_ROOT / rel
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(body, "utf-8")
    return {"ok": True, "file": rel, "title": title}


def _web_search(q: str, n: int = 15) -> tuple[list, str]:
    """站外搜索 → [{title,url,snippet}]。引擎:Google CSE(官方 API,配置了 cx 才用)→
    DuckDuckGo HTML 版兜底(无 key,实测质量好;Google 网页版已全面 JS 化,服务端直抓
    两种 UA 都拿不到结果——实测实锤,别再试)。返回 (results, engine_name)。"""
    import requests
    cx = ""
    key = ""
    try:
        cfg = json.loads((_CLAUDE_DIR / "state" / "server-config.json").read_text("utf-8"))
        cx = str((cfg.get("web_portal") or {}).get("cse_cx") or "").strip()
    except Exception:
        pass
    if cx:
        try:
            key = (os.environ.get("GOOGLE_API_KEY") or "").strip()
            if not key:
                for ln in (_CLAUDE_DIR / ".env").read_text("utf-8").splitlines():
                    if ln.startswith("GOOGLE_API_KEY="):
                        key = ln.split("=", 1)[1].strip().strip('"')
        except Exception:
            key = ""
    if cx and key:
        try:
            r = requests.get("https://www.googleapis.com/customsearch/v1",
                             params={"key": key, "cx": cx, "q": q, "num": min(10, n)}, timeout=12)
            items = (r.json() or {}).get("items") or []
            if items:
                return ([{"title": i.get("title") or "", "url": i.get("link") or "",
                          "snippet": i.get("snippet") or ""} for i in items], "google")
        except Exception:
            pass
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": q}, timeout=15,
                          headers={"User-Agent": "Mozilla/5.0"})
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for res in soup.select(".result")[:n]:
            a = res.select_one("a.result__a")
            if not a or not (a.get("href") or "").startswith("http"):
                continue
            sn = res.select_one(".result__snippet")
            out.append({"title": a.get_text(" ", strip=True),
                        "url": a["href"],
                        "snippet": (sn.get_text(" ", strip=True) if sn else "")[:200]})
        return out, "duckduckgo"
    except Exception:
        return [], "none"


# 浏览器主页/搜索引擎。⚠ 用户原本要的是**真谷歌**,实测证伪:代理下 Google 判"异常流量"直接拦,
# 且它给的 reCAPTCHA **在我们的域下永远无解**(站点密钥绑死域名,报「网站密钥的网域无效」)——
# 这不是调 header 能绕的,是 Google 明确不允许被代理。七家实测(headless chromium,中日英各一次):
#   Google ✗异常流量 / Bing ✗Cloudflare Turnstile / Ecosia ✗ / Startpage ✗(GET 出不来结果)
#   Brave ✓(自有索引,中日英都出真结果,80 外链) / DuckDuckGo ✓(最轻,42 外链)
# → 默认换 Brave;Google 仍可手动访问(首页能开),只是搜索会被它自己拦,那时自动兜底转 Brave。
WEB_HOME = "https://search.brave.com/"
WEB_SEARCH_URL = "https://search.brave.com/search?q={q}"
_SEARCH_FALLBACK = "https://search.brave.com/search?q={q}"
# 判"被搜索引擎拦下了"的特征(拦截页都很短且带这些话术)
_BLOCK_SIGNS = ("异常流量", "unusual traffic", "Confirm you", "not a robot",
                "Verify you are human", "解决以下难题", "detected unusual")
_WEB_LAST = None   # register 时指向 state/web-last.json(网页阅读独立状态,绝不进书的 reading-pos)


# ══════════ 实况网页(用户拍板 2026-07-19:要**原本的网页**,不是正文抓取)══════════
# 形态 = 浏览器 Copilot(Edge/Arc/Sider):真实页面 + 侧栏悬浮。
# 关键:iframe 直嵌真站会被 X-Frame-Options/CSP frame-ancestors 拦(实测 mhlw.go.jp、
# google.com 都是 SAMEORIGIN)→ 必须**服务端代理**:由我们的域名吐 HTML、剥掉这两个头。
# 副作用正是我们要的:页面变成**同源文档** → 外壳 JS 能直接读 iframe 内的选区/DOM,
# 于是查词/高亮/AI 侧栏这套控制层原样可用(不像扩展要跨进程通信)。
# ⚠ 首版「资源不代理,靠 <base href> 直连原站」**已实测证伪**(2026-07-19 headless chromium 七站实测):
#   页面里每个 JS/CSS/图片/XHR 都从我们的域名发往原站 = **跨源请求**,被浏览器 ORB/CORS 全挡:
#     github err=267/netfail=129、stackoverflow 连 CSS 都 ERR_BLOCKED_BY_RESPONSE.NotSameOrigin、
#     youtube text=68、bilibili 自家 api.bilibili.com XHR blocked by CORS;只有维基这种纯服务端渲染站活着。
#   现改为**拦截式代理**(成熟做法:Ultraviolet / webrecorder Wombat 同思路的最小实现):
#     ① 去掉 <base>,服务端把 HTML 里所有资源 URL 重写成 /pdf/web/res?url=<绝对地址>;
#     ② CSS 里的 url()/@import 同样重写(经 /res 时改写正文);
#     ③ 注入 shim 在客户端补运行时那半:patch fetch / XHR / 动态插入节点 / pushState;
#     ④ 视频站不硬代理(签名 CDN + MSE 打不通)→ 走**官方 embed**(youtube-nocookie / player.bilibili),
#        官方 embed 本就允许被 iframe,直连最稳(与 rc-videoplayer.js 同一套 URL 构造)。
#   安全边界:代理页跑在我们**自己的源**上(这正是控制层能读选区的前提),等于在自己域内执行第三方 JS。
#   已做的收口:禁 Service Worker 注册(否则第三方 SW 会接管整个 App 的源)。彻底隔离需换独立源
#   (桥接本就全走 postMessage,天然可跨源),列为后续加固项。

# 官方 embed:允许被 iframe,直连不代理(硬代理视频=签名 CDN/MSE/DRM,打不通)
_EMBED_HOSTS = ("youtube.com/embed/", "youtube-nocookie.com/embed/", "player.bilibili.com",
                "player.vimeo.com", "open.spotify.com/embed", "w.soundcloud.com/player")

_VIDEO_PAT = (
    (re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([\w-]{6,})", re.I),
     "https://www.youtube-nocookie.com/embed/{0}?rel=0"),
    (re.compile(r"bilibili\.com/video/(BV[\w]+)", re.I),
     "https://player.bilibili.com/player.html?bvid={0}&autoplay=0&danmaku=0"),
)


def video_embed(url: str) -> str:
    """视频页 → 官方 embed 地址(打不通的硬代理换成能真播的官方播放器);非视频页返回空串。"""
    for pat, tpl in _VIDEO_PAT:
        m = pat.search(url or "")
        if m:
            return tpl.format(m.group(1))
    return ""


# ── 路径镜像式代理地址(取代首版 ?url= 查询串)──
# 实测:`?url=` 形态下文档地址是 /pdf/web/proxy,页面里任何**文档相对**地址(webpack 动态 chunk
# 最典型)都会解析成 /pdf/web/app-runtime.xxx.css 打到我们身上 → github 118 / stackoverflow 407 条
# 404。改成把真实地址镜进路径(Ultraviolet 同思路),相对解析天然落回原站结构,这一整类一次性消失。
def _mirror(prefix: str, u: str) -> str:
    try:
        p = urlparse(u)
        path = p.path or "/"
        out = f"/pdf/web/{prefix}/{p.scheme}/{p.netloc}{quote(path, safe='/@:+~!$&*,;=()')}"
        if p.query:
            out += "?" + p.query
        return out
    except Exception:
        return u


def unmirror(rest: str, query: str) -> str:
    """路径镜像 → 真实地址(/pdf/web/p/https/例.com/a/b?q=1 → https://例.com/a/b?q=1)。"""
    bits = (rest or "").split("/", 2)
    if len(bits) < 2 or bits[0] not in ("http", "https"):
        return ""
    u = f"{bits[0]}://{bits[1]}/" + (bits[2] if len(bits) > 2 else "")
    if query:
        u += "?" + query
    return u


def _pxr(u: str) -> str:
    """资源 → 子资源代理(路径镜像:JS 模块的 ./相对导入、CSS 的 ../ 也才解析得对)。"""
    return _mirror("r", u)


def _pxp(u: str) -> str:
    """页面 → 主文档代理。"""
    return _mirror("p", u)


def _absu(u: str, base: str) -> str:
    u = (u or "").strip()
    if not u or u[0] == "#" or re.match(r"^(data|blob|javascript|mailto|tel|about):", u, re.I):
        return ""
    try:
        return urljoin(base, u)
    except Exception:
        return ""


_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)
_CSS_IMP = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.I)


def _rewrite_css(css: str, base: str) -> str:
    def _u(m):
        a = _absu(m.group(2), base)
        return f"url({m.group(1)}{_pxr(a)}{m.group(1)})" if a else m.group(0)

    def _i(m):
        a = _absu(m.group(2), base)
        return f"@import {m.group(1)}{_pxr(a)}{m.group(1)}" if a else m.group(0)

    return _CSS_IMP.sub(_i, _CSS_URL.sub(_u, css))


_RES_ATTR = re.compile(
    r"""(<(?:img|script|link|source|track|video|audio|embed|input|object|use|image)\b[^>]*?\s"""
    r"""(?:src|href|poster|data-src|data-original|data-lazy-src|xlink:href)\s*=\s*)(["'])([^"']*)\2""",
    re.I)
_NAV_ATTR = re.compile(r"""(<(?:a|area|form)\b[^>]*?\s(?:href|action)\s*=\s*)(["'])([^"']*)\2""", re.I)
_IFRAME = re.compile(r"""(<iframe\b[^>]*?\ssrc\s*=\s*)(["'])([^"']*)\2""", re.I)
_SRCSET = re.compile(r"""(<[^>]+?\ssrcset\s*=\s*)(["'])([^"']*)\2""", re.I)
_STYLE_ATTR = re.compile(r"""(\sstyle\s*=\s*)(["'])([^"']*url\([^"']*)\2""", re.I)
_STYLE_TAG = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)
_INTEGRITY = re.compile(r"""\s(?:integrity|nomodule)\s*=\s*(["'])[^"']*\1""", re.I)


def rewrite_html(html: str, base: str) -> str:
    """把页面里所有资源/导航 URL 重写到我们的代理(取代 <base href>——那个正是跨源被挡的根因)。"""
    def _res(m):
        a = _absu(m.group(3), base)
        return f"{m.group(1)}{m.group(2)}{_pxr(a)}{m.group(2)}" if a else m.group(0)

    def _nav(m):
        # <a>/<form> 只绝对化、**不**代理:点击由注入脚本拦截后 postMessage 给外壳(它按真实地址走);
        # 万一没拦住(中键/target=_blank),回落到原站也是合理行为,总好过打到我们域名的死链。
        a = _absu(m.group(3), base)
        return f"{m.group(1)}{m.group(2)}{a}{m.group(2)}" if a else m.group(0)

    def _ifr(m):
        a = _absu(m.group(3), base)
        if not a:
            return m.group(0)
        u = a if any(h in a for h in _EMBED_HOSTS) else _pxp(a)   # 官方 embed 直连
        return f"{m.group(1)}{m.group(2)}{u}{m.group(2)}"

    def _ss(m):
        out = []
        for part in m.group(3).split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split(None, 1)
            a = _absu(bits[0], base)
            out.append((_pxr(a) if a else bits[0]) + ((" " + bits[1]) if len(bits) > 1 else ""))
        return f"{m.group(1)}{m.group(2)}{', '.join(out)}{m.group(2)}"

    html = _INTEGRITY.sub(" ", html)          # SRI 会因我们改写 CSS 而失败,必须剥
    html = _IFRAME.sub(_ifr, html)
    html = _RES_ATTR.sub(_res, html)
    html = _NAV_ATTR.sub(_nav, html)
    html = _SRCSET.sub(_ss, html)
    html = _STYLE_ATTR.sub(lambda m: f"{m.group(1)}{m.group(2)}{_rewrite_css(m.group(3), base)}{m.group(2)}", html)
    html = _STYLE_TAG.sub(lambda m: m.group(1) + _rewrite_css(m.group(2), base) + m.group(3), html)
    return html


# ⚠ 舱壁:一个重站单页要拉 100+ 子资源,每条都占一个 gunicorn 线程等上游网络 I/O。
# 服务只有 --threads 32,不设限 = 浏一个 github 就能把线程池吃光,SSE / 阅读器 / 语音全饿死
# (与 [[sse-thread-starvation]] 同一类事故,只是放大了一个量级)。代理流量最多占 10 条,
# 满了就 503——掉一张图,远好过全站宕机。
_RES_GATE = threading.Semaphore(10)

# 子资源磁盘缓存:现代站的 js/css/字体都是内容哈希命名(不可变),重复代理纯属浪费,
# 还会招来对方限流(实测 stackoverflow 一页 100+ 请求直接 429)。命中即免舱壁免上游。
RESCACHE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "web-rescache"
_RESCACHE_MAX = 3 * 1024 * 1024
_RESCACHE_TTL = 7 * 86400
_RESCACHE_CT = ("javascript", "text/css", "image/", "font/", "application/font",
                "application/x-font", "text/javascript")


def _rescache_ok(ct: str) -> bool:
    c = (ct or "").lower()
    return any(x in c for x in _RESCACHE_CT)


def _rescache_path(url: str) -> Path:
    return RESCACHE_DIR / (hashlib.sha1((url or "").encode("utf-8")).hexdigest() + ".bin")


def _rescache_get(url: str):
    p = _rescache_path(url)
    try:
        st = p.stat()
        if time.time() - st.st_mtime > _RESCACHE_TTL:
            return None
        blob = p.read_bytes()
        i = blob.index(b"\n")
        return blob[i + 1:], blob[:i].decode("utf-8", "replace")
    except Exception:
        return None


def _rescache_put(url: str, body: bytes, ct: str):
    try:
        RESCACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _rescache_path(url)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(ct.encode("utf-8") + b"\n" + body)
        tmp.replace(p)
    except Exception:
        pass


_JARS: dict = {}     # per-user cookie jar:很多站没 cookie 直接 403/跳登录


def _px_session():
    import requests
    uid = ""
    try:
        uid = str(session.get("user_id") or "anon")
    except Exception:
        uid = "anon"
    s = _JARS.get(uid)
    if s is None:
        if len(_JARS) > 24:
            _JARS.clear()
        s = requests.Session()
        _JARS[uid] = s
    return s


def _px_headers(url: str, extra_ref: str = "") -> dict:
    h = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36",
         "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"}
    try:
        p = urlparse(url)
        h["Referer"] = extra_ref or f"{p.scheme}://{p.netloc}/"
        h["Origin"] = f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return h


_PROXY_STRIP_HEADERS = {"x-frame-options", "content-security-policy",
                        "content-security-policy-report-only", "cross-origin-opener-policy",
                        "cross-origin-embedder-policy", "frame-options"}

_PROXY_INJECT = """
<script>
(function(){
  // 站内导航拦截:链接/表单跳转改走代理(留在我们的壳里);新窗口链接也接管
  function proxied(u){ try{ return '/pdf/web/proxy?url=' + encodeURIComponent(new URL(u, location.__realBase || document.baseURI).href); }catch(e){ return u; } }
  document.addEventListener('click', function(e){
    var a = e.target && e.target.closest && e.target.closest('a[href]');
    if(!a) return;
    var href = a.getAttribute('href') || '';
    if(!href || href.charAt(0)==='#' || /^(javascript|mailto|tel):/i.test(href)) return;
    e.preventDefault();
    try{ parent.postMessage({__rcweb:'nav', url: new URL(href, location.__realBase || document.baseURI).href}, '*'); }
    catch(_){ location.href = proxied(href); }
  }, true);
  // 选区上报:父壳据此弹工具条(同源本可直接读,postMessage 更稳且面向未来跨源)
  function report(){
    try{
      var s = getSelection(); var t = s && !s.isCollapsed ? String(s).trim() : '';
      var r = t && s.rangeCount ? s.getRangeAt(0).getBoundingClientRect() : null;
      parent.postMessage({__rcweb:'sel', text: t,
        rect: r ? {left:r.left, top:r.top, right:r.right, bottom:r.bottom} : null,
        ctx: t ? (function(){ var n = s.anchorNode; n = n && (n.nodeType===3?n.parentElement:n);
                   var b = n && n.closest ? n.closest('p,li,td,blockquote,h1,h2,h3,div') : null;
                   return b ? (b.innerText||'').slice(0,1200) : ''; })() : ''}, '*');
    }catch(_){}
  }
  document.addEventListener('mouseup', function(){ setTimeout(report, 10); });
  document.addEventListener('touchend', function(){ setTimeout(report, 10); });
  // 供父壳取整页正文(AI 上下文/存档)
  window.addEventListener('message', function(e){
    var d = e.data || {};
    if(d.__rcweb === 'getText'){
      try{ parent.postMessage({__rcweb:'text', text:(document.body.innerText||'').slice(0,120000),
                               title: document.title, url: location.__realBase || ''}, '*'); }catch(_){}
    }
  });
  // ── 运行时重写 shim(服务端只能改静态 HTML;页面 JS 跑起来发的请求得在这儿接)──
  var B = location.__realBase || location.href;
  function ABS(u){ try{ return new URL(u, B).href; }catch(e){ return null; } }
  function RES(u){
    if(u==null) return u;
    u = String(u);
    if(!u || u.charAt(0)==='#') return u;
    if(/^(data|blob|javascript|mailto|tel|about):/i.test(u)) return u;
    if(u.indexOf('/pdf/web/')===0) return u;                     // 已是代理地址
    var a = ABS(u); if(!a) return u;
    try{
      var p = new URL(a);
      if(p.origin === location.origin) return u;                 // 已在我们的域内,别再套一层
      return '/pdf/web/r/' + p.protocol.replace(':','') + '/' + p.host + p.pathname + p.search;
    }catch(e){ return u; }
  }
  // fetch
  var _f = window.fetch;
  // ⚠ 留一条**未被代理**的通道:我们自己注入的引擎(web-immersive)要调 /pdf/api/*,
  //   走 patch 过的 fetch 会被当成原站相对路径翻成 /pdf/web/r/https/<原站>/pdf/api/... → 405。
  try{ window.__rcRawFetch = _f && _f.bind(window); }catch(_){}
  if(_f) window.fetch = function(input, init){
    try{
      if(typeof input === 'string') input = RES(input);
      else if(input && input.url) input = new Request(RES(input.url), input);
    }catch(_){}
    return _f.call(this, input, init);
  };
  // XHR
  var _o = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u){
    var a = [].slice.call(arguments); try{ a[1] = RES(u); }catch(_){}
    return _o.apply(this, a);
  };
  // 动态插入的节点(懒加载图片/异步 <script>/后插 <link>)
  try{
    new MutationObserver(function(ms){
      ms.forEach(function(mu){ [].forEach.call(mu.addedNodes||[], function(n){
        if(!n || n.nodeType!==1) return;
        if(n.getAttribute && n.getAttribute('data-rc-own')) return;   // 我们自己注入的资源,别按原站解析
        ['src','href'].forEach(function(k){
          var v = n.getAttribute && n.getAttribute(k);
          if(v && !/^(data|blob|#|javascript)/i.test(v) && v.indexOf('/pdf/web/')!==0
             && n.tagName!=='A' && n.tagName!=='FORM' && n.tagName!=='IFRAME'){
            try{ n.setAttribute(k, RES(v)); }catch(_){}
          }
        });
      }); });
    }).observe(document.documentElement, {childList:true, subtree:true});
  }catch(_){}
  // SPA 路由:pushState 换成相对我们的代理地址会污染后续相对解析 → 只记不跳
  try{
    ['pushState','replaceState'].forEach(function(k){
      var _s = history[k];
      history[k] = function(a,b,u){ try{ if(u) B = ABS(u) || B; }catch(_){}
                                    return _s.call(history, a, b, location.href); };
    });
  }catch(_){}
  // ⚠ 安全硬闸:代理页跑在**我们自己的源**上,放任它注册 Service Worker = 第三方脚本接管整个 App。
  try{ if(navigator.serviceWorker){ navigator.serviceWorker.register = function(){ return Promise.reject(new Error('blocked')); }; } }catch(_){}
  // ⚠ 程序化导航拦截(用户实测:谷歌首页搜索无响应的根因)。
  //   谷歌不走原生表单提交 —— 它用 JS 直接导航,于是:
  //     ① submit 事件根本不触发(form.submit() 按规范就**不**派发 submit 事件);
  //     ② 导航到绝对地址 https://www.google.com/search?... → iframe 加载真谷歌 →
  //        对方 X-Frame-Options 一挡 → chrome-error 空白页(实测复现)。
  //   所以必须把这几个程序化入口全接管。(location.href= 的 setter 无法 patch,
  //   由服务端的 referer 兜底救回,见 _leak_rescue。)
  function _navOut(u){
    try{ var a = ABS(u); if(a){ parent.postMessage({__rcweb:'nav', url:a}, '*'); return true; } }catch(_){}
    return false;
  }
  try{
    var _fs = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function(){
      try{
        if((this.method||'get').toLowerCase()==='get'){
          var u = new URL(this.getAttribute('action') || B, B);
          new FormData(this).forEach(function(v,k){ u.searchParams.set(k, v); });
          if(_navOut(u.href)) return;
        }
      }catch(_){}
      return _fs.apply(this, arguments);
    };
  }catch(_){}
  try{
    ['assign','replace'].forEach(function(k){
      var _l = location[k].bind(location);
      location[k] = function(u){ if(_navOut(u)) return; return _l(u); };
    });
  }catch(_){}
  try{
    var _wo = window.open;
    window.open = function(u){ if(u && _navOut(u)) return null; return _wo.apply(window, arguments); };
  }catch(_){}
  // 表单(站内搜索框)→ 交给外壳按真实地址导航
  document.addEventListener('submit', function(e){
    var f = e.target; if(!f || f.tagName!=='FORM') return;
    if((f.method||'get').toLowerCase()!=='get') return;
    try{
      var u = new URL(f.getAttribute('action') || B, B);
      new FormData(f).forEach(function(v,k){ u.searchParams.set(k, v); });
      e.preventDefault();
      parent.postMessage({__rcweb:'nav', url: u.href}, '*');
    }catch(_){}
  }, true);
  try{ parent.postMessage({__rcweb:'ready', title: document.title}, '*'); }catch(_){}
})();
</script>
<script src="/static/pdf/web-immersive.js" data-rc-own="1"></script>
"""


def _proxy_page(url: str):
    """代理一张网页:抓 → 剥框架限制头 → 注 <base> + 桥接脚本 → 当我们自己的文档吐出去。"""
    err = _url_safe(url)
    if err:
        return None, err
    try:
        _h = _px_headers(url)
        _h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        _h.pop("Origin", None)                     # 顶层导航不带 Origin(带了反而像 CSRF 被拒)
        r = _px_session().get(url, timeout=20, allow_redirects=True, stream=True, headers=_h)
    except Exception as ex:
        return None, f"抓取失败:{str(ex)[:120]}"
    ct = (r.headers.get("Content-Type") or "").lower()
    if "html" not in ct:
        return None, f"不是网页({ct.split(';')[0] or '未知类型'});图片/PDF 等请直接在原站打开"
    raw = b""
    for chunk in r.iter_content(65536):
        raw += chunk
        if len(raw) > 12 * 1024 * 1024:
            break
    # ⚠ 流式读完后**不能**再碰 r.apparent_encoding(它要 r.content,已消费 → RuntimeError,
    #   实测 mhlw.go.jp 500 的根因)。自己按 raw 检测:响应头 → <meta charset> → chardet → utf-8。
    enc = r.encoding if (r.encoding and r.encoding.lower() != "iso-8859-1") else ""
    if not enc:
        import re as _re0
        m0 = _re0.search(rb'charset=["\']?([\w-]+)', raw[:4096], _re0.I)
        if m0:
            enc = m0.group(1).decode("ascii", "ignore")
    if not enc:
        try:
            import chardet
            enc = chardet.detect(raw[:20000]).get("encoding") or "utf-8"
        except Exception:
            enc = "utf-8"
    try:
        html = raw.decode(enc, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    final = r.url or url
    import re as _re
    # 页面自带的 <base> 必须先摘掉:留着它我们重写出来的 /pdf/web/res?url=… 会被再拼一次原站前缀。
    html = _re.sub(r"<base\b[^>]*>", "", html, flags=_re.I)
    _cache_raw = html                      # ★ 抽正文用**重写前**的原始 HTML(重写后 URL 全变代理链接)
    html = rewrite_html(html, final)       # ★ 根因修复:资源/CSS/srcset 全部改走我们的代理
    base_tag = f'<script>location.__realBase={json.dumps(final)};</script>'
    if _re.search(r"<head[^>]*>", html, _re.I):
        html = _re.sub(r"(<head[^>]*>)", r"\1" + base_tag, html, count=1, flags=_re.I)
    else:
        html = base_tag + html
    # 页面自带的 CSP <meta> 也要剥(否则注入脚本被拦)
    html = _re.sub(r'<meta[^>]+http-equiv=["\']?content-security-policy["\']?[^>]*>', "", html, flags=_re.I)
    # 搜索引擎把我们拦下来了 → 换一家能用的重搜,别把用户丢在一堵解不开的验证墙前。
    #   (Google 的 reCAPTCHA 在我们的域下**必然**报"网站密钥的网域无效",点了也没用。)
    #   ⚠ 判"页面很短"必须先剜掉 script/style 的**内容** —— 去标签的正则只删标签本身,
    #     而拦截页塞满验证脚本,长度轻松破万,首版据此判定直接失效(实测 Google 兜底没触发)。
    _bare = _re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    _bare = _re.sub(r"<[^>]+>", " ", _bare)
    blocked = ("/sorry/" in final or "/recaptcha/" in final
               or (len(_bare) < 6000 and any(x in _bare for x in _BLOCK_SIGNS)))
    if blocked:
        try:
            def _kw_of(u):
                q = parse_qs(urlparse(u).query)
                return (q.get("q") or q.get("query") or q.get("p") or [""])[0]
            qs = parse_qs(urlparse(final).query)
            # ⚠ Google 的 /sorry/ 页 URL 里 `q=` 是**它自己的验证 token**(实测抓成
            #   "EhAkACQQ_4OLAC7PZ…"),真正的搜索词在 `continue=` 指向的原始地址里。
            kw = _kw_of((qs.get("continue") or [""])[0]) or _kw_of(final)
            if kw and (len(kw) > 60 and " " not in kw and "+" not in kw):
                kw = ""       # 还是像 token 就宁可不兜底,别拿乱码去搜
            host = (urlparse(final).netloc or "").lower()
            if kw and "brave" not in host:
                alt = _SEARCH_FALLBACK.format(q=quote(kw, safe=""))
                note = ("<div style=\"font:14px/1.6 system-ui;padding:12px 16px;margin:0;"
                        "background:#fff5e6;border-bottom:1px solid #f0d9b0;color:#7a5520\">"
                        f"⚠ <b>{_hesc(host)}</b> 把这次搜索判成了自动程序流量(它的验证码在本站域名下无解)"
                        f"——已自动改用 Brave 搜索「{_hesc(kw)}」。</div>")
                r2, e2 = _proxy_page(alt)
                if r2 is not None and not e2:
                    r2.set_data(_re.sub(r"(<body[^>]*>)", r"\1" + note, r2.get_data(as_text=True),
                                        count=1, flags=_re.I) or r2.get_data(as_text=True))
                    return r2, ""
        except Exception:
            pass
    try:   # ★中间转换层(内容侧):浏览过的页面立刻可被 AI 读到。
        #   `web:<url>` 是**视图引用**(同 vbook:),后端必须有 resolver —— 这里在代理时
        #   顺手抽正文写缓存,assistant._page_text 见 web: 前缀即命中(用户实锤:不做这层,
        #   AI read_page 只会说"没能读到这页的文字内容")。
        _web_cache_put(final, _cache_raw)
    except Exception:
        pass
    html += _PROXY_INJECT
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    for h in list(resp.headers.keys()):
        if h.lower() in _PROXY_STRIP_HEADERS:
            del resp.headers[h]
    return resp, ""


# ⚠ 不依赖 register 注入:任何 import 本模块的进程(assistant 工具、脚本、cron)都要能直接用
#   resolver。首版写成 register 时才赋值 → 裸 import 场景 WEB_CACHE_DIR=None → **静默返回空正文**
#   (实测:read_page 有内容而 summarize 说"没抓到正文",同一 resolver 结果不一致的根因)。
WEB_CACHE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "web-cache"


def _web_key(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:20]


def _web_cache_put(url: str, raw_html: str):
    """把代理过的页面抽成正文存缓存(AI 读页/搜索的数据源)。"""
    if not WEB_CACHE_DIR:
        return
    try:
        from readability import Document
        doc = Document(raw_html)
        title = (doc.short_title() or "").strip()[:120]
        body = doc.summary(html_partial=True)
    except Exception:
        title, body = "", raw_html
    from bs4 import BeautifulSoup
    txt = BeautifulSoup(body or "", "html.parser").get_text("\n", strip=True)
    if len(txt) < 120:   # 抽取失败(纯 JS 页/首页型)→ 退回整页去标签,总比空好
        txt = BeautifulSoup(raw_html, "html.parser").get_text("\n", strip=True)
    WEB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (WEB_CACHE_DIR / (_web_key(url) + ".json")).write_text(
        json.dumps({"url": url, "title": title, "text": txt[:200000], "ts": int(time.time())},
                   ensure_ascii=False), "utf-8")


def web_material(ref: str) -> dict:
    """★`web:<url>` → {url,title,text}。**中间转换层的后端 resolver**,给 assistant/工具用。
    先查缓存(浏览时已写),miss 则实时抓一次。非 web: 引用返回 None。"""
    if not isinstance(ref, str) or not ref.startswith("web:"):
        return None
    url = ref[4:]
    if WEB_CACHE_DIR:
        p = WEB_CACHE_DIR / (_web_key(url) + ".json")
        try:
            d = json.loads(p.read_text("utf-8"))
            if d.get("text"):
                return d
        except Exception:
            pass
    try:   # 缓存 miss(如 AI 先于渲染发问)→ 现抓一次
        import requests
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Chrome/126.0"})
        _web_cache_put(url, r.text)
        p = WEB_CACHE_DIR / (_web_key(url) + ".json")
        return json.loads(p.read_text("utf-8"))
    except Exception as ex:
        return {"url": url, "title": "", "text": "", "error": f"网页内容取不到:{str(ex)[:80]}"}


def _web_last_get() -> str:
    try:
        d = json.loads(_WEB_LAST.read_text("utf-8"))
        rel = str(d.get("file") or "")
        return rel if rel and (_OBSIDIAN_ROOT / rel).exists() else ""
    except Exception:
        return ""


def _web_last_set(rel: str):
    try:
        _WEB_LAST.write_text(json.dumps({"file": rel, "ts": int(time.time())}, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def _web_home_content(q: str = "") -> str:
    """搜索主页/结果页,渲成**阅读器正文**(在 html/view 壳里 → 侧栏/助手/设置第一屏即可用)。
    Google 官网式布局:大标题+居中搜索框;?q= 时下方列结果。"""
    import html as _h
    rows = ""
    eng = ""
    if q:
        results, engine = _web_search(q)
        eng = {"google": "Google", "duckduckgo": "DuckDuckGo"}.get(engine, "")
        if results:
            rows = "".join(
                f'<div class="wh-r"><a class="wh-t" href="{_h.escape(r["url"])}" '
                f'onclick="return __webOpen(this)">{_h.escape(r["title"])}</a>'
                f'<div class="wh-u">{_h.escape(r["url"][:90])}</div>'
                f'<div class="wh-s">{_h.escape(r["snippet"])}</div></div>'
                for r in results)
        else:
            rows = '<p style="text-align:center;color:#888">没搜到结果</p>'
    recent = ""
    if not q:
        rec = _recent_web_pages()
        if rec:
            recent = ('<div class="wh-sec">最近浏览</div>'
                      + "".join(f'<a class="wh-chip" href="/pdf/html/view?file={_h.escape(r["rel"])}">'
                                f'{_h.escape(r["name"][:24])}</a>' for r in rec))
    return f"""
<div class="wh-wrap{' wh-results' if q else ''}">
  <div class="wh-logo">🌐 网页阅读</div>
  <form class="wh-box" onsubmit="return __webGo(this)">
    <input name="q" value="{_h.escape(q)}" placeholder="搜索,或输入网址…" autocomplete="off" autofocus>
    <button type="submit">搜索</button>
  </form>
  <div class="wh-hint">输网址直接进阅读器;搜索{('由 ' + eng + ' 提供') if eng else ''}</div>
  <div id="wh-st"></div>
  <div class="wh-res">{rows}</div>
  {recent}
</div>
<style>
.wh-wrap{{max-width:640px;margin:0 auto;padding-top:10vh;text-align:center}}
.wh-results{{padding-top:2vh;text-align:left}}
.wh-results .wh-logo{{font-size:18px}}
.wh-logo{{font-size:30px;font-weight:600;margin-bottom:26px;text-align:center}}
.wh-box{{display:flex;gap:8px}}
.wh-box input{{flex:1;border:1px solid #c8c2b2;border-radius:24px;padding:12px 20px;font-size:16px;outline:none;background:#fffdf6;color:#1b1b1b}}
.wh-box button{{border:1px solid #c8c2b2;background:#efe9d8;color:#333;border-radius:24px;padding:0 22px;font-size:15px;cursor:pointer}}
.wh-hint{{color:#8a8571;font-size:12px;margin-top:10px;text-align:center}}
#wh-st{{color:#666;font-size:13px;min-height:18px;margin-top:10px;text-align:center}}
.wh-r{{padding:12px 4px;border-bottom:1px solid rgba(0,0,0,.06)}}
.wh-t{{font-size:17px;color:#2255bb;text-decoration:none}}
.wh-u{{color:#3d7a4e;font-size:12px;margin:2px 0;word-break:break-all}}
.wh-s{{color:#555;font-size:13.5px;line-height:1.55}}
.wh-sec{{color:#8a8571;font-size:12px;margin:28px 0 10px;letter-spacing:.05em;text-align:left}}
.wh-chip{{display:inline-block;background:rgba(0,0,0,.05);border:1px solid rgba(0,0,0,.1);color:#444;border-radius:15px;padding:5px 13px;margin:0 6px 8px 0;font-size:13px;text-decoration:none}}
</style>
<script>
function __webGo(f){{
  var v=(f.q.value||'').trim(); if(!v) return false;
  var isU = v.indexOf('http://')===0||v.indexOf('https://')===0||/^[\\w-]+(\\.[\\w-]+)+([/]|$)/.test(v);
  if(isU){{ location.href='/pdf/web/live?url='+encodeURIComponent(v.indexOf('http')===0?v:'https://'+v); return false; }}
  location.href='/pdf/html/view?file=__web__&q='+encodeURIComponent(v); return false;
}}
// 点结果 → **实况网页**(用户拍板:要原本的网页);正文抽取降为阅读器里的 📄 按钮
function __webOpen(a){{ location.href='/pdf/web/live?url='+encodeURIComponent(a.getAttribute('href')); return false; }}
function __webFetch(url){{
  document.getElementById('wh-st').textContent='🌐 抓取正文中…';
  fetch('/pdf/api/web-fetch',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{url:url}})}})
   .then(function(r){{return r.json();}}).then(function(d){{
     if(d.ok&&d.file) location.href='/pdf/html/view?file='+encodeURIComponent(d.file);
     else document.getElementById('wh-st').textContent='✗ '+(d.error||'抓取失败');
   }}).catch(function(){{ document.getElementById('wh-st').textContent='✗ 网络错误'; }});
}}
</script>"""


def _recent_web_pages(limit: int = 12) -> list:
    """资源/web/ 里最近抓取的网页(mtime 倒序)。"""
    d = _OBSIDIAN_ROOT / "资源" / "web"
    if not d.is_dir():
        return []
    fs = sorted(d.glob("*.html"), key=lambda f: -f.stat().st_mtime)[:limit]
    return [{"rel": f"资源/web/{f.name}", "name": f.stem.rsplit("-", 1)[0].replace("-", " ")} for f in fs]


_WEB_PORTAL_CSS = """
body{margin:0;background:#0b101d;color:#e6e6f0;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;min-height:100vh}
.wrap{max-width:720px;margin:0 auto;padding:8vh 20px 40px}
h1{font-size:26px;text-align:center;margin:0 0 26px;color:#cfe6ff;font-weight:600}
.sbox{display:flex;gap:8px}
.sbox input{flex:1;background:#111a2e;border:1px solid #2a3550;color:#e6e6f0;border-radius:24px;
  padding:13px 20px;font-size:16px;outline:none}
.sbox input:focus{border-color:#3b6db5}
.sbox button{background:#1a2540;border:1px solid #3b6db5;color:#cfe6ff;border-radius:24px;
  padding:0 22px;font-size:15px;cursor:pointer}
.hint{color:#5a6b84;font-size:12px;text-align:center;margin-top:10px}
.res{margin-top:26px}
.r{display:block;padding:12px 14px;border-radius:10px;text-decoration:none;margin-bottom:4px}
.r:hover{background:#111a2e}
.r .t{color:#8ab4f8;font-size:16px;margin-bottom:2px}
.r .u{color:#5a9367;font-size:12px;margin-bottom:3px;word-break:break-all}
.r .s{color:#9aa8bd;font-size:13px;line-height:1.5}
.sec{color:#5a6b84;font-size:12px;margin:24px 0 8px;letter-spacing:.05em}
.recent a{display:inline-block;background:#111a2e;border:1px solid #22304d;color:#aebfd8;
  border-radius:16px;padding:6px 14px;margin:0 6px 8px 0;font-size:13px;text-decoration:none}
.recent a:hover{border-color:#3b6db5;color:#cfe6ff}
#st{color:#8a9bb4;font-size:13px;text-align:center;margin-top:14px;min-height:18px}
.eng{color:#44506a;font-size:11px;text-align:center;margin-top:22px}
"""

_WEB_PORTAL_JS = r"""
function isUrl(v){return v.indexOf('http://')===0 || v.indexOf('https://')===0 || /^[\\w-]+(\\.[\\w-]+)+([/]|$)/.test(v);}
async function go(v){
  v=(v||'').trim(); if(!v) return;
  if(isUrl(v)){
    if(!/^https?:/.test(v)) v='https://'+v;
    document.getElementById('st').textContent='🌐 抓取正文中…';
    try{
      const r=await fetch('/pdf/api/web-fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:v})});
      const d=await r.json();
      if(d.ok&&d.file){location.href='/pdf/html/view?file='+encodeURIComponent(d.file);}
      else{document.getElementById('st').textContent='✗ '+(d.error||'抓取失败');}
    }catch(e){document.getElementById('st').textContent='✗ 网络错误';}
  }else{
    location.href='/pdf/web?q='+encodeURIComponent(v);
  }
}
function openResult(ev,url){ev.preventDefault();
  document.getElementById('st').textContent='🌐 抓取「'+url.slice(0,50)+'」…';
  fetch('/pdf/api/web-fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url})})
   .then(r=>r.json()).then(d=>{
     if(d.ok&&d.file){location.href='/pdf/html/view?file='+encodeURIComponent(d.file);}
     else{document.getElementById('st').textContent='✗ '+(d.error||'抓取失败')+'（可长按链接在新页原样打开）';}
   }).catch(()=>{document.getElementById('st').textContent='✗ 网络错误';});
}
document.addEventListener('DOMContentLoaded',()=>{
  const i=document.getElementById('q');
  i.addEventListener('keydown',e=>{if(e.key==='Enter')go(i.value);});
  i.focus();
});
"""


def register_html_reader(bp, *, safe_vault_path, obsidian_root, claude_dir):
    global _WEB_LAST, WEB_CACHE_DIR
    _WEB_LAST = Path(claude_dir) / "state" / "web-last.json"
    WEB_CACHE_DIR = Path(claude_dir) / "state" / "web-cache"   # 与模块级默认同值;显式对齐注入的 claude_dir
    """挂 HTML 阅读器路由到 bp(url_prefix /pdf),并注入 pdf_reader 的三个依赖。"""
    global _safe_vault_path, _OBSIDIAN_ROOT, _HTML_HL_DIR
    _safe_vault_path = safe_vault_path
    _OBSIDIAN_ROOT = obsidian_root
    _HTML_HL_DIR = claude_dir / "state" / "html-highlights"

    @bp.route("/html/view")
    def html_view():
        """统一 HTML 阅读器主页(架构验收)。?file=<vault-rel .html/.md>;无 file → 内置 sample。"""
        rel = (request.args.get("file") or "").strip()
        if rel == "__web__":
            # 网页阅读主页(Google 式搜索页)在**阅读器壳内**渲 → 侧栏/助手第一屏可用(用户需求)
            resp = make_response(render_template(
                "html_reader.html", html_content=_web_home_content((request.args.get("q") or "").strip()),
                file_rel="__web__", file_name="🌐 网页阅读", reader_js_v=_html_js_v()))
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if rel:
            abs_path = _safe_vault_path(rel)
            if not abs_path or abs_path.suffix.lower() not in (".html", ".htm", ".md", ".markdown"):
                abort(404)
            rel_clean = abs_path.relative_to(_OBSIDIAN_ROOT.resolve()).as_posix()
            raw = abs_path.read_text("utf-8", "ignore")
            if abs_path.suffix.lower() in (".md", ".markdown"):
                html_content = _md_to_html(raw)
            else:
                html_content = _sanitize_html_doc(raw)
            title = Path(rel_clean).name
            if rel_clean.startswith("资源/web/"):
                _web_last_set(rel_clean)   # 网页阅读独立"上次位置"(绝不写书的 reading-pos)
        else:
            rel_clean = ""
            html_content = _HTML_SAMPLE
            title = "HTML 阅读器(示例)"
        resp = make_response(render_template(
            "html_reader.html", html_content=html_content, file_rel=rel_clean,
            file_name=title, reader_js_v=_html_js_v()))
        return resp

    @bp.route("/web/proxy")
    def pdf_web_proxy():
        """代理渲染真实网页(同源 → 外壳 JS 可直接操作它)。仅登录用户可用(/pdf 在鉴权前缀内)。"""
        url = (request.args.get("url") or "").strip()
        if not url:
            abort(400)
        resp, err = _proxy_page(url)
        if err:
            import html as _h
            return make_response(
                f'<body style="font:15px system-ui;padding:40px;color:#555">'
                f'<p>⚠ {_h.escape(err)}</p>'
                f'<p><a href="{_h.escape(url)}" target="_blank">在系统浏览器打开原页 →</a></p></body>', 200)
        return resp

    @bp.route("/web/p/<path:rest>")
    def pdf_web_page_mirror(rest):
        """路径镜像式主文档代理(见 _mirror 注释:根治文档相对地址打到我们身上)。"""
        url = unmirror(rest, request.query_string.decode("utf-8", "ignore"))
        if not url:
            abort(400)
        resp, err = _proxy_page(url)
        if err:
            import html as _h
            return make_response(
                f'<body style="font:15px system-ui;padding:40px;color:#555">'
                f'<p>⚠ {_h.escape(err)}</p>'
                f'<p><a href="{_h.escape(url)}" target="_blank">在系统浏览器打开原页 →</a></p></body>', 200)
        return resp

    @bp.route("/web/r/<path:rest>")
    def pdf_web_res_mirror(rest):
        """路径镜像式子资源代理。"""
        url = unmirror(rest, request.query_string.decode("utf-8", "ignore"))
        if not url:
            abort(400)
        return _serve_res(url)

    @bp.route("/api/web-translate", methods=["POST"])
    def pdf_api_web_translate():
        """网页沉浸式翻译的批量端点。POST {texts:[...]} → {ok, zh:[...]}。

        与 PDF 的「译页」**共用同一条管线**(scripts/vocab/translate.py:缓存 → Google 批量 →
        no_ai 兜底),所以译文缓存跨 PDF/网页互通,同一句话不会翻两遍。
        差别只在取句方式:PDF 从字符层切句,网页由前端按 DOM 段落给 —— 那才是网页的天然句段。
        """
        body = request.get_json(silent=True) or {}
        texts = [str(t or "").strip() for t in (body.get("texts") or [])][:120]
        if not texts:
            return jsonify({"ok": True, "zh": []})
        import sys as _sys
        vp = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "scripts" / "vocab"
        if str(vp) not in _sys.path:
            _sys.path.insert(0, str(vp))
        try:
            from translate import (gtranslate_batch as _gb, translate as _tr,
                                   _cache_get as _cg, _cache_put as _cp)
        except Exception as ex:
            return jsonify({"ok": False, "error": f"translate load fail: {ex}"}), 500
        zhs = [(_cg(t, "zh-CN") or "") if t else "" for t in texts]
        miss = [i for i, z in enumerate(zhs) if not z and texts[i]]
        if miss:
            mt = [texts[i] for i in miss]
            batch = None
            try:
                batch = _gb(mt)
            except Exception:
                batch = None
            if batch and len(batch) == len(mt):
                for k, i in enumerate(miss):
                    if batch[k]:
                        zhs[i] = batch[k]
                        try:
                            _cp(texts[i], "zh-CN", batch[k], "gtranslate")
                        except Exception:
                            pass
            # 同 PDF 译页的教训:兜底**绝不落 AI CLI**,且带墙钟预算——否则 Google 故障窗
            # 一个请求能挂好几分钟还烧额度。译不出的留空,前端优雅跳过。
            still = [i for i in miss if not zhs[i]]
            _dl = time.monotonic() + 10
            for i in still[:40]:
                if time.monotonic() > _dl:
                    break
                try:
                    z = _tr(texts[i], backend="no_ai")
                    if z:
                        zhs[i] = z
                        _cp(texts[i], "zh-CN", z, "no_ai")
                except Exception:
                    pass
        return jsonify({"ok": True, "zh": zhs,
                        "translated": sum(1 for z in zhs if z), "total": len(texts)})

    @bp.app_errorhandler(404)
    def _leak_rescue(e):
        """代理页"漏出来"的导航救回。

        页面里 `location.href = '/search?q=x'` 这类赋值**无法被 patch**(location 的 setter
        不可覆写),于是 iframe 会带着原站的路径打到**我们**的域名上,拿一个 404 白页。
        但请求头里的 Referer 忠实记着它是从哪个代理页发出的 —— 据此还原真实站点再重定向回代理,
        就把这一整类漏网补上了(经典代理的 referer 兜底手法)。
        """
        try:
            ref = request.headers.get("Referer") or ""
            i = ref.find("/pdf/web/")
            if i >= 0 and request.path and not request.path.startswith("/pdf/web/"):
                rest = ref[i + len("/pdf/web/"):]
                kind, _, tail = rest.partition("/")
                if kind in ("p", "r"):
                    src = unmirror(tail.split("?")[0], "")
                    if src:
                        p = urlparse(src)
                        real = f"{p.scheme}://{p.netloc}{request.path}"
                        if request.query_string:
                            real += "?" + request.query_string.decode("utf-8", "ignore")
                        return redirect(_pxp(real))
        except Exception:
            pass
        return e

    @bp.route("/api/web-vocab", methods=["POST"])
    def pdf_api_web_vocab():
        """网页版「未掌握词多的段落」判定。POST {texts:[...]} → {ok, marks:[{i, count, words}]}。

        与 PDF 阅读器的 `_build_unmastered_sentences` **同一个判定集**:凡查过且 label_slug
        不是 mastered 的词都算(与生词下划线完全一致——不想被计数就把词标记掌握)。
        差别只在粒度:PDF 按几何切句,网页按 DOM 段落 —— 段落本就是网页的天然语义块。
        """
        body = request.get_json(silent=True) or {}
        texts = [str(t or "") for t in (body.get("texts") or [])][:120]
        thr = max(2, min(8, int(body.get("threshold") or 3)))
        if not texts:
            return jsonify({"ok": True, "marks": []})
        import sys as _sys
        vp = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "scripts" / "vocab"
        if str(vp) not in _sys.path:
            _sys.path.insert(0, str(vp))
        try:
            import vocab_index          # type: ignore
            idx = vocab_index.index() or {}
        except Exception:
            return jsonify({"ok": True, "marks": []})     # 词库读不到 → 静默不标,别拦住翻译
        unmastered = {f: i.get("lemma") or f for f, i in idx.items()
                      if i.get("label_slug") and i["label_slug"] != "mastered"}
        if not unmastered:
            return jsonify({"ok": True, "marks": []})
        # ⚠ 日语不能"切出词再查":日文没有词间空格,连续假名+汉字是一整串
        #   (实测 `没落する没落していく貿易ボタン` 被当成一个 token,一个也匹配不上)。
        #   反过来做——拿词库里的未掌握词去**子串搜**正文,这才是无分词语言的正确方向。
        cjk_un = [w for w in unmastered if not w.isascii() and len(w) >= 2]
        marks = []
        for i, t in enumerate(texts):
            hit = {}
            for w in re.findall(r"[A-Za-z][A-Za-z'-]{1,}", t):
                lem = unmastered.get(w.lower()) or unmastered.get(w)
                if lem:
                    hit[lem] = hit.get(lem, 0) + 1
            if re.search(r"[぀-ヿ一-鿿]", t):
                for w in cjk_un:
                    if w in t:
                        lem = unmastered[w]
                        hit[lem] = hit.get(lem, 0) + 1
            if len(hit) >= thr:
                marks.append({"i": i, "count": len(hit), "words": sorted(hit)[:12]})
        return jsonify({"ok": True, "marks": marks, "threshold": thr})

    @bp.route("/web/frame")
    def pdf_web_frame():
        """iframe 的唯一入口:服务端裁决——视频页 → 官方 embed(能真播),其余 → 代理渲染。
        模板和前端 go() 都走这里,免得"哪些算视频"在两处各写一份。"""
        url = (request.args.get("url") or "").strip()
        if not url:
            abort(400)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        emb = video_embed(url)
        return redirect(emb or _pxp(url))

    @bp.route("/web/res")
    def pdf_web_res():
        """子资源代理(JS/CSS/图片/字体/XHR/媒体)。**这是"很多网页打不开"的根因修复**:
        原先靠 <base> 让资源直连原站 → 每个都是跨源请求 → 被 ORB/CORS 挡死。
        现在全部由我们同源吐出去。CSS 正文再递归重写 url()/@import;媒体透传 Range 支持拖动。"""
        url = (request.args.get("url") or "").strip()
        return _serve_res(url)

    def _serve_res(url: str):
        if not url or _url_safe(url):
            abort(403)
        hit = _rescache_get(url)      # 静态资源(hash 命名的 js/css/字体/图)命中即走,不占舱壁也不打上游
        if hit is not None:
            body, ct = hit
            return Response(body, status=200, headers={
                "Content-Type": ct, "Cache-Control": "public, max-age=604800",
                "Access-Control-Allow-Origin": "*"})
        if not _RES_GATE.acquire(timeout=6):
            return Response("proxy busy", status=503)
        ok = False
        try:
            resp = _serve_res_inner(url)
            ok = True
            return resp
        finally:
            # ⚠ 流式正文是在视图**返回之后**才被消费的 → 不能在这里直接 release,
            #   否则舱壁只罩住"连上游"那一小段,大文件下载照样吃满线程。
            #   走 _gated() 把 release 交给生成器的 finally;非流式路径(CSS/异常)才就地放。
            if not ok:
                _RES_GATE.release()

    def _gated(it):
        """把释放信号量绑到流的生命周期上。"""
        try:
            for chunk in it:
                yield chunk
        finally:
            _RES_GATE.release()

    def _serve_res_inner(url: str):
        h = _px_headers(url, extra_ref=request.headers.get("Referer", ""))
        h["Accept"] = request.headers.get("Accept", "*/*")
        rng = request.headers.get("Range")
        if rng:
            h["Range"] = rng
        try:
            r = _px_session().get(url, headers=h, timeout=25, stream=True, allow_redirects=True)
        except Exception:
            abort(502)
        ct = (r.headers.get("Content-Type") or "application/octet-stream")
        keep = {"content-type", "content-length", "content-range", "accept-ranges",
                "cache-control", "etag", "last-modified", "expires", "content-encoding"}
        hd = {k: v for k, v in r.headers.items()
              if k.lower() in keep and k.lower() not in ("content-length", "content-encoding")}
        hd["Access-Control-Allow-Origin"] = "*"
        hd["Content-Type"] = ct
        if _rescache_ok(ct) and r.status_code == 200:
            try:                                           # 小体积静态资源整读+落盘(不流式)
                raw = r.content if int(r.headers.get("Content-Length") or 0) <= _RESCACHE_MAX else None
                if raw is not None and len(raw) <= _RESCACHE_MAX:
                    if "text/css" in ct.lower():
                        raw = _rewrite_css(raw.decode("utf-8", "replace"), r.url or url).encode("utf-8")
                    _rescache_put(url, raw, ct)
                    resp = Response(raw, status=200, headers=hd)
                    _RES_GATE.release()
                    return resp
            except Exception:
                pass
        if "text/css" in ct.lower():                       # CSS 里的 url()/@import 要跟着改写
            try:
                resp = Response(_rewrite_css(r.text, r.url or url),
                                status=r.status_code, headers=hd)
                _RES_GATE.release()   # 构造完再放:途中抛异常就落回下面的流式分支(那条自己会放)
                return resp
            except Exception:
                pass
        return Response(_gated(r.iter_content(65536)), status=r.status_code, headers=hd)

    @bp.route("/web/live")
    def pdf_web_live():
        """实况网页阅读(浏览器 Copilot 形态,用户拍板:要**原本的网页**):
        顶栏地址栏 + 同源 iframe 真页面 + 右侧助手侧栏(选区经 postMessage 上来)。"""
        import html as _h
        url = (request.args.get("url") or "").strip()
        if not url:
            return redirect("/pdf/web?home=1")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        # ★直接用 **PDF 阅读器那张页面**(用户拍板:"就只是把书页的展示窗口换成网页"):
        #   顶栏/侧栏/全部 rc-* 与 reader.js 原样复用,零新壳;reader.js 见 web_url 即跳过 PDF 加载。
        return make_response(render_template(
            "pdf_reader.html", web_url=url, pdf_url="", file_rel="web:" + url,
            file_name=url, page=1, page_ts=0, chars_ver=0, pdf_size=0,
            compressed=0, comp_avail=0, ui_shared=1, group=None,
            reader_js_v=_html_js_v(), js_v=_html_js_v()))

    @bp.route("/web")
    def pdf_web_portal():
        """网页阅读入口(仲裁,像浏览器恢复会话):上次看过网页 → 直接恢复它;
        没有(或 ?home=1)→ 搜索主页。两者都在 html/view 阅读器壳内(侧栏第一屏可用)。
        与书的续读完全分离:状态存 state/web-last.json,html 阅读器不碰 reading-pos。"""
        if request.args.get("q"):
            return redirect("/pdf/web/live?url=" + _q(
                WEB_SEARCH_URL.format(q=_q(request.args["q"], safe="")), safe=""))
        if not request.args.get("home"):
            last = _web_last_get()
            if last and last.startswith("http"):
                return redirect("/pdf/web/live?url=" + _q(last, safe=""))
        # 主页 = **真的谷歌首页**(用户拍板:不要我们自制的搜索页,直接把原网站拉进来)
        return redirect("/pdf/web/live?url=" + _q(WEB_HOME, safe=""))

    @bp.route("/api/web-fetch", methods=["POST"])
    def pdf_api_web_fetch():
        """抓网页进阅读器(浏览器 Copilot 初版)。POST {url} → {ok, file, title};
        前端拿 file 跳 /pdf/html/view?file=... 即获全套阅读能力。"""
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "缺 url"}), 400
        out = _fetch_web_page(url)
        return jsonify(out), (200 if out.get("ok") else 422)

    @bp.route("/api/html-highlights", methods=["GET", "POST", "PATCH", "DELETE"])
    def pdf_api_html_highlights():
        """HTML 阅读器高亮 CRUD(字符偏移锚 {start,end};独立 sidecar:state/html-highlights/<sha>.json)。
        GET ?file= → 列;POST {file,start,end,text,color,note?,sentence?} → 建;
        PATCH {file,id,color?,note?} → 改;DELETE ?file=&id= → 删。file 可空(走内置 sample 键)。"""
        if request.method == "GET":
            rel = (request.args.get("file") or "").strip()
            return jsonify({"ok": True, "highlights": _html_hl_load(rel)})
        if request.method == "DELETE":
            rel = (request.args.get("file") or "").strip()
            hid = (request.args.get("id") or "").strip()
            items = [h for h in _html_hl_load(rel) if h.get("id") != hid]
            _html_hl_save(rel, items)
            return jsonify({"ok": True})
        body = request.get_json(silent=True) or {}
        rel = (body.get("file") or "").strip()
        items = _html_hl_load(rel)
        if request.method == "POST":
            try:
                start = int(body.get("start"))
                end = int(body.get("end"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "缺少 start/end"}), 400
            if end <= start:
                return jsonify({"ok": False, "error": "无效区间"}), 400
            h = {"id": "h" + uuid.uuid4().hex[:11], "start": start, "end": end,
                 "text": (body.get("text") or "")[:2000], "color": (body.get("color") or "#fff59d"),
                 "note": (body.get("note") or "")[:2000], "sentence": (body.get("sentence") or "")[:2000],
                 "time": int(time.time())}
            items.append(h); _html_hl_save(rel, items)
            return jsonify({"ok": True, "id": h["id"], "highlight": h})
        # PATCH
        hid = (body.get("id") or "").strip()
        h = next((x for x in items if x.get("id") == hid), None)
        if not h:
            return jsonify({"ok": False, "error": "未找到"}), 404
        if "color" in body:
            h["color"] = body.get("color") or h["color"]
        if "note" in body:
            h["note"] = (body.get("note") or "")[:2000]
        _html_hl_save(rel, items)
        return jsonify({"ok": True, "highlight": h})
