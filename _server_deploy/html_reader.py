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

from flask import abort, jsonify, make_response, redirect, render_template, request
from urllib.parse import quote as _q

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


_WEB_LAST = None   # register 时指向 state/web-last.json(网页阅读独立状态,绝不进书的 reading-pos)


# ══════════ 实况网页(用户拍板 2026-07-19:要**原本的网页**,不是正文抓取)══════════
# 形态 = 浏览器 Copilot(Edge/Arc/Sider):真实页面 + 侧栏悬浮。
# 关键:iframe 直嵌真站会被 X-Frame-Options/CSP frame-ancestors 拦(实测 mhlw.go.jp、
# google.com 都是 SAMEORIGIN)→ 必须**服务端代理**:由我们的域名吐 HTML、剥掉这两个头。
# 副作用正是我们要的:页面变成**同源文档** → 外壳 JS 能直接读 iframe 内的选区/DOM,
# 于是查词/高亮/AI 侧栏这套控制层原样可用(不像扩展要跨进程通信)。
# 资源(图片/CSS/JS)不代理,靠注入的 <base href> 直接走原站(省带宽,也少一层出错)。

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
  try{ parent.postMessage({__rcweb:'ready', title: document.title}, '*'); }catch(_){}
})();
</script>
"""


def _proxy_page(url: str):
    """代理一张网页:抓 → 剥框架限制头 → 注 <base> + 桥接脚本 → 当我们自己的文档吐出去。"""
    err = _url_safe(url)
    if err:
        return None, err
    import requests
    try:
        r = requests.get(url, timeout=20, allow_redirects=True, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"})
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
    # <base>:让相对资源/链接指向原站(资源直连原站,不经我们)
    base_tag = f'<base href="{final}">\n<script>location.__realBase={json.dumps(final)};</script>'
    if _re.search(r"<head[^>]*>", html, _re.I):
        html = _re.sub(r"(<head[^>]*>)", r"\1" + base_tag, html, count=1, flags=_re.I)
    else:
        html = base_tag + html
    # 页面自带的 CSP <meta> 也要剥(否则注入脚本被拦)
    html = _re.sub(r'<meta[^>]+http-equiv=["\']?content-security-policy["\']?[^>]*>', "", html, flags=_re.I)
    html += _PROXY_INJECT
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    for h in list(resp.headers.keys()):
        if h.lower() in _PROXY_STRIP_HEADERS:
            del resp.headers[h]
    return resp, ""


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
    global _WEB_LAST
    _WEB_LAST = Path(claude_dir) / "state" / "web-last.json"
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
            return redirect("/pdf/html/view?file=__web__&q=" + _q(request.args["q"]))
        if not request.args.get("home"):
            last = _web_last_get()
            if last:
                return redirect("/pdf/html/view?file=" + _q(last))
        return redirect("/pdf/html/view?file=__web__")

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
