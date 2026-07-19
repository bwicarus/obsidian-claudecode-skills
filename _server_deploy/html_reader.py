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

from flask import abort, jsonify, make_response, render_template, request

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
function isUrl(v){return v.indexOf('http://')===0 || v.indexOf('https://')===0 || /^[\w-]+(\.[\w-]+)+([/]|$)/.test(v);}
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
    """挂 HTML 阅读器路由到 bp(url_prefix /pdf),并注入 pdf_reader 的三个依赖。"""
    global _safe_vault_path, _OBSIDIAN_ROOT, _HTML_HL_DIR
    _safe_vault_path = safe_vault_path
    _OBSIDIAN_ROOT = obsidian_root
    _HTML_HL_DIR = claude_dir / "state" / "html-highlights"

    @bp.route("/html/view")
    def html_view():
        """统一 HTML 阅读器主页(架构验收)。?file=<vault-rel .html/.md>;无 file → 内置 sample。"""
        rel = (request.args.get("file") or "").strip()
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
        else:
            rel_clean = ""
            html_content = _HTML_SAMPLE
            title = "HTML 阅读器(示例)"
        resp = make_response(render_template(
            "html_reader.html", html_content=html_content, file_rel=rel_clean,
            file_name=title, reader_js_v=_html_js_v()))
        return resp

    @bp.route("/web")
    def pdf_web_portal():
        """网页阅读门户(独立地址,浏览器 Copilot 入口):默认搜索页;?q= 出结果列表。
        输 URL 直接抓;点结果 → web-fetch 抓正文 → HTML 阅读器打开(全套阅读能力)。"""
        import html as _h
        q = (request.args.get("q") or "").strip()
        rows = ""
        engine = ""
        if q:
            results, engine = _web_search(q)
            if results:
                rows = "".join(
                    f'<a class="r" href="{_h.escape(r["url"])}" onclick="openResult(event,{json.dumps(r["url"])})">'
                    f'<div class="t">{_h.escape(r["title"])}</div>'
                    f'<div class="u">{_h.escape(r["url"][:90])}</div>'
                    f'<div class="s">{_h.escape(r["snippet"])}</div></a>'
                    for r in results)
            else:
                rows = '<p style="color:#8a9bb4;text-align:center">没搜到结果</p>'
        recent = _recent_web_pages()
        rec_html = ""
        if recent and not q:
            rec_html = ('<div class="sec">最近抓取</div><div class="recent">'
                        + "".join(f'<a href="/pdf/html/view?file={_h.escape(r["rel"])}">{_h.escape(r["name"][:24])}</a>'
                                  for r in recent) + "</div>")
        eng_note = {"google": "Google(官方 CSE API)", "duckduckgo": "DuckDuckGo(要用 Google 结果:在 server-config 的 web_portal.cse_cx 填搜索引擎 ID)"}.get(engine, "")
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>🌐 网页阅读</title>
<link rel="apple-touch-icon" href="/static/icons/web.png">
<link rel="icon" type="image/png" href="/static/icons/web-32.png">
<meta name="apple-mobile-web-app-title" content="网页">
<meta name="apple-mobile-web-app-capable" content="yes">
<style>{_WEB_PORTAL_CSS}</style></head><body>
<div class="wrap">
<h1>🌐 网页阅读</h1>
<div class="sbox"><input id="q" value="{_h.escape(q)}" placeholder="搜索,或直接输入网址…" autocomplete="off">
<button onclick="go(document.getElementById('q').value)">{'搜索' if not q else '再搜'}</button></div>
<div class="hint">回车即搜;输入网址直接进阅读器(高亮/查词/AI 侧栏全套可用)</div>
<div id="st"></div>
<div class="res">{rows}</div>
{rec_html}
{f'<div class="eng">搜索引擎:{_h.escape(eng_note)}</div>' if eng_note else ''}
</div><script>{_WEB_PORTAL_JS}</script></body></html>"""
        resp = make_response(page)
        resp.headers["Cache-Control"] = "no-store"
        return resp

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
