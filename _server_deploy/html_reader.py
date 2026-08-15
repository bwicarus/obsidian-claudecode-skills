"""html_reader.py — 统一 HTML 阅读器(架构验收域)。

HTML/Markdown 内容渲进主文档 + HtmlAdapter + 共享 rc-*.js,证明「给一个新阅读器写个
adapter,共享功能(选区/查词/翻译/解释/对话/笔记/制卡/高亮)就全有」。AI 端点
(translate/explain/dict)内容无关,仍在 pdf_reader.py,直接复用。

2026-07-06 结构拆分第 2 刀(体检 structure:域自包含,块外零引用)。外部依赖经
register_html_reader 注入(safe_vault_path/obsidian_root/claude_dir),避免循环 import。
用法(pdf_reader.py):
    from html_reader import register_html_reader
    register_html_reader(bp, safe_vault_path=_safe_vault_path,
                         obsidian_root=OBSIDIAN_ROOT, claude_dir=CLAUDE_DIR,
                         asset_cache_version=_static_asset_version,
                         pdf_reader_js_v=_reader_js_v,
                         pdf_shared_js_v=_pdf_shared_js_v,
                         reader_sidecar_path=_reader_sidecar_path,
                         reader_sidecar_lock=_reader_sidecar_edit_lock)
路由:GET /pdf/html/view(?file=<vault-rel .html/.md>,无 file → 内置 sample)
     GET/POST/PATCH/DELETE /pdf/api/html-highlights(字符偏移锚 sidecar CRUD)
部署:cp 本文件到 /home/bwicarus/webapp/(跟 pdf_reader.py 同目录)+ restart webapp。
"""
import hashlib
import importlib
import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import re
import threading
from flask import abort, current_app, g, jsonify, make_response, redirect, render_template, request, session, Response
from urllib.parse import quote as _q, quote, urljoin, urlparse, parse_qs, parse_qsl, urlencode
from html import escape as _hesc
from web_proxy_cap import (
    MAX_RESOURCE_BYTES_PER_RESPONSE,
    WebProxyCapBudgetRegistry,
    canonical_web_proxy_origin,
    issue_web_proxy_cap,
    normalize_web_proxy_origin,
    normalize_web_proxy_scope,
    verify_web_proxy_cap_details,
    web_proxy_cap_matches_url,
)
from rbi_access import (
    issue_rbi_ticket,
    load_rbi_ticket_secret,
    public_network_url_error,
    verify_rbi_ticket,
)
from web_cookie_store import (
    cookie_hosts,
    cookie_values_for_url,
    load_cookie_store,
    put_cookie_header,
    remove_cookie_host,
    save_cookie_store,
    update_cookie_values_for_url,
)
from web_cache_store import (
    normalize_user_id as normalize_cache_user_id,
    read_web_cache,
    web_cache_path,
    write_web_cache,
)

# register_html_reader 注入(模块 import 时为 None,注册后可用)
_safe_vault_path = None   # callable: vault 相对路径 → 安全绝对 Path 或 None
_OBSIDIAN_ROOT = None     # Path: vault 根
_CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
_HTML_HL_DIR = None       # Path: state/html-highlights/(高亮 sidecar 目录)
_HTML_HL_USER_DIR = None  # Path: state/html-highlights-by-user/<uid>/
_HTML_HL_ACCOUNT_PATH = None  # callable(*parts): 经验证账户的统一 sidecar 路径
_HTML_HL_ACCOUNT_LOCK = None  # callable(dataset,key): owner 分区的跨进程 RMW 锁
_HTML_HL_FALLBACK_LOCK = threading.RLock()  # 仅 standalone/单测兼容模式
_WEB_JP_TAGGER = None     # 网页句子总词数(日语):fugashi 常驻，镜像 PDF 的句级判定
_WEB_JP_TAGGER_TRIED = False
_WEB_CJK_MATCHER_KEY = None
_WEB_CJK_MATCHER = None
_WEB_CJK_MATCHER_LOCK = threading.Lock()
_ASSET_CACHE_VERSION = None  # callable:静态逻辑路径列表 → 可靠 cache-bust 指纹
_PDF_READER_JS_V = None      # callable:PDF 壳 reader.js 指纹(/web/live 复用)
_PDF_SHARED_JS_V = None      # callable:PDF 壳共享资源指纹(/web/live 复用)
_RBI_TICKET_SECRET = None    # standalone RBI WS 与 Flask 共享的持久短期票密钥
_WEB_PROXY_BUDGETS = WebProxyCapBudgetRegistry()
_PWA_WEB_READER_RETIRED = "pwa_web_reader_retired"


class _WebCjkMatcher:
    """Single-pass matcher preserving the former longest-first semantics.

    The old route ran ``re.finditer`` once per vocabulary form and sentence.
    The input order is already longest-first; retaining each form's rank and
    resolving candidates in rank order therefore produces the exact same
    overlap decisions without rescanning the sentence hundreds of times.
    """

    __slots__ = ("_root",)

    def __init__(self, words):
        root = {}
        for rank, raw_word in enumerate(words):
            word = str(raw_word or "")
            if not word:
                continue
            node = root
            for char in word:
                node = node.setdefault(char, {})
            node.setdefault(None, []).append((rank, word))
        self._root = root

    def find_nonoverlapping(self, text):
        text = str(text or "")
        candidates = []
        for start in range(len(text)):
            node = self._root
            for end in range(start, len(text)):
                node = node.get(text[end])
                if node is None:
                    break
                for rank, word in node.get(None, ()):
                    candidates.append((rank, start, end + 1, word))

        # rank is the former outer-loop order: longest forms first, then the
        # stable vocabulary-index order.  start mirrors re.finditer's order
        # within one form.  This deliberately is not merely leftmost-first.
        candidates.sort(key=lambda item: (item[0], item[1]))
        occupied = bytearray(len(text))
        accepted = []
        next_start_by_rank = {}
        for _rank, start, end, word in candidates:
            # ``re.finditer`` never reports overlapping occurrences of the
            # same form, even when an earlier occurrence is later rejected
            # because a longer form already owns part of its range.
            if start < next_start_by_rank.get(_rank, 0):
                continue
            next_start_by_rank[_rank] = end
            if any(occupied[start:end]):
                continue
            occupied[start:end] = b"\x01" * (end - start)
            accepted.append((start, end, word))
        return accepted


def _web_cjk_matcher(words):
    """Return an immutable matcher cached by the current unmastered-form set."""
    global _WEB_CJK_MATCHER_KEY, _WEB_CJK_MATCHER
    key = tuple(words or ())
    if _WEB_CJK_MATCHER_KEY == key and _WEB_CJK_MATCHER is not None:
        return _WEB_CJK_MATCHER
    with _WEB_CJK_MATCHER_LOCK:
        if _WEB_CJK_MATCHER_KEY != key or _WEB_CJK_MATCHER is None:
            _WEB_CJK_MATCHER = _WebCjkMatcher(key)
            _WEB_CJK_MATCHER_KEY = key
        return _WEB_CJK_MATCHER


def _retired_web_response():
    """PWA 不再代浏览器抓取、代理或远程渲染第三方网页。"""
    response = jsonify({
        "ok": False,
        "error": _PWA_WEB_READER_RETIRED,
        "replacement": "browser_extension",
    })
    response.status_code = 410
    response.headers["Cache-Control"] = "no-store"
    return response


def _retired_web_redirect_target(value: str) -> str:
    """Validate a legacy /web/live target without dereferencing it server-side."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 8192:
        return ""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return ""
    # Backslashes are interpreted inconsistently by browsers and URL parsers;
    # credentials are unnecessary for a browser hand-off and may leak in logs.
    if "\\" in raw:
        return ""
    try:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        if not parsed.netloc or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        parsed.port  # force validation of malformed/out-of-range ports
    except (TypeError, ValueError):
        return ""
    return raw

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


_HTML_CACHE_ASSETS = (
    "pdf/rc-core.js",
    "reader-runtime/book-host.js",
    "reader-runtime/account-context.js",
    "reader-runtime/interaction-policy.js",
    "reader-runtime/document-host.js",
    "reader-runtime/context-selection-registry.js",
    "reader-runtime/data-store.js",
    "reader-runtime/indexeddb-store.js",
    "reader-runtime/data-registry.js",
    "reader-runtime/card-repository.js",
    "reader-runtime/anki-mobile-export.js",
    "reader-runtime/sync-owner-lease.js",
    "reader-runtime/sync-gateway.js",
    "reader-runtime/server-sync-transport.js",
    "reader-runtime/direct-sync-protocol.js",
    "reader-runtime/direct-sync-signal-transport.js",
    "reader-runtime/sync-coordinator.js",
    "reader-runtime/sync-runtime.js",
    "reader-runtime/sync-conflict-control.js",
    "reader-runtime/direct-sync-host.js",
    "reader-runtime/direct-sync-leader.js",
    "reader-runtime/vocabulary-state.js",
    "reader-runtime/computer-voice-webrtc.js",
    "reader-runtime/preference-store.js",
    "reader-runtime/storage-router.js",
    "reader-runtime/runtime-selector.js",
    "reader-runtime/legacy-rc-bridge.js",
    "reader-runtime/pwa-service-bridge.js",
    "reader-runtime/pwa-runtime.js",
    "pdf/rc-ui.js",
    "pdf/rc-flashcard.js",
    "pdf/rc-review.js",
    "pdf/rc-md.js",
    "pdf/rc-highlight.js",
    "pdf/rc-snippets.js",
    "pdf/rc-result.js",
    "pdf/rc-offline-dictionary.js",
    "pdf/rc-wordpop.js",
    "pdf/rc-assistant.js",
    "pdf/rc-settings.js",
    "pdf/rc-sidedrawer.js",
    "pdf/rc-toolchip.js",
    "pdf/rc-turncard.js",
    "pdf/rc-voicectx.js",
    "pdf/rc-computer-voice.js",
    "pdf/rc-voicecall.js",
    "pdf/rc-phrasepop.js",
    "pdf/html-reader.js",
    "pdf/pwa-extension-bridge.js",
)
_WEB_ADAPTER_CACHE_ASSETS = ("pdf/web-adapter.js",)
_RBI_LIVE_CACHE_ASSETS = ("pdf/rrweb-record.umd.js",)


def _cache_version(asset_names) -> str:
    """调用 PDF 域注入的唯一静态指纹实现；注册前仅供导入期安全兜底。"""
    if _ASSET_CACHE_VERSION is None:
        return "1"
    return _ASSET_CACHE_VERSION(asset_names)


def _html_js_v():
    """统一 HTML 阅读器模板所加载的全部版本化资源指纹。"""
    return _cache_version(_HTML_CACHE_ASSETS)


def _web_adapter_js_v():
    """PDF 壳内实况网页 adapter 的独立指纹。"""
    return _cache_version(_WEB_ADAPTER_CACHE_ASSETS)


def _rbi_live_js_v():
    """RBI 实况页脚本的独立指纹。"""
    return _cache_version(_RBI_LIVE_CACHE_ASSETS)


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

def _html_hl_user_id(user_id: str | int | None = None) -> str:
    return normalize_cache_user_id(_px_uid() if user_id is None else user_id)


def _html_hl_path(
    rel: str,
    *,
    user_id: str | int | None = None,
    legacy: bool = False,
) -> Path | None:
    key = rel or "__html_sample__"
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json"
    if legacy:
        return (_HTML_HL_DIR / name) if _HTML_HL_DIR else None
    if _HTML_HL_ACCOUNT_PATH is not None:
        if user_id is not None:
            raise PermissionError(
                "生产 HTML 高亮路径只接受已认证账户上下文，不能按显式 uid 推断"
            )
        # 注入回调由统一 reader_sidecar_store 完成身份校验、旧数据认领，
        # 并保证结果位于 by-user/<uid>/html-highlights/。解析失败必须向上
        # 抛出，不能退回旧共享目录。
        return Path(_HTML_HL_ACCOUNT_PATH("html-highlights", name))
    uid = _html_hl_user_id(user_id)
    if not uid or not _HTML_HL_USER_DIR:
        return None
    root = Path(_HTML_HL_USER_DIR).resolve()
    account = (root / uid).resolve()
    if account.parent != root:
        return None
    return account / name


def _html_hl_load(
    rel: str,
    *,
    user_id: str | int | None = None,
) -> list:
    """Load account data.

    注册统一账户路径后只读取该路径；旧共享数据由 reader_sidecar_store
    一次性复制认领。环境变量 owner 回退仅保留给未注册的独立模块模式。
    """
    if _HTML_HL_ACCOUNT_PATH is not None:
        current = _html_hl_path(rel, user_id=user_id)
        try:
            data = json.loads(current.read_text("utf-8"))
        except FileNotFoundError:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("highlights"), list):
            return data["highlights"]
        raise ValueError(
            f"invalid HTML highlight sidecar JSON shape: {current.name}"
        )
    uid = _html_hl_user_id(user_id)
    current = _html_hl_path(rel, user_id=user_id)
    legacy = (
        _html_hl_path(rel, legacy=True)
        if uid and os.environ.get("READER_LEGACY_HTML_HIGHLIGHT_OWNER_UID") == uid
        else None
    )
    paths = (current, legacy)
    for path in paths:
        if path is None:
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("highlights"), list):
            return data["highlights"]
    return []


def _html_hl_save(
    rel: str,
    items: list,
    *,
    user_id: str | int | None = None,
):
    path = _html_hl_path(rel, user_id=user_id)
    if path is None:
        raise ValueError("HTML 高亮缺少已认证用户上下文")
    if _HTML_HL_ACCOUNT_PATH is not None:
        from reader_sidecar_store import atomic_write_json

        atomic_write_json(path, items, indent=None)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(items, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _html_hl_edit(rel: str):
    """Yield the latest list while holding the document's mutation lease."""
    if _HTML_HL_ACCOUNT_PATH is not None:
        if not callable(_HTML_HL_ACCOUNT_LOCK):
            raise RuntimeError("HTML 高亮账户存储缺少统一写锁")
        lease = _HTML_HL_ACCOUNT_LOCK("html-highlights", rel or "__html_sample__")
    else:
        lease = _HTML_HL_FALLBACK_LOCK
    with lease:
        items = _html_hl_load(rel)
        yield items
        _html_hl_save(rel, items)


# ── 网页抓取 → 阅读器(2026-07-19,用户方向:浏览器 Copilot 的初版验证)────────────
# 路线:抓 URL → **Firefox 阅读模式同款算法**(readability-lxml)抽正文 → 走本文件既有的
# 白名单消毒 → 存 vault `资源/web/` → /pdf/html/view 打开 → 高亮/查词/AI 侧栏/翻译/
# 注意力埋点**全部白拿**(统一控制层红利)。存 vault=学习材料一等公民:Obsidian 全设备
# 同步、进 FTS 全局搜索、进概念网向前搜索。

_PRIVATE_NETS = ("127.", "10.", "192.168.", "169.254.", "0.")


def _url_safe(url: str) -> str:
    """SSRF 防护:只放行公网 http(s)。返回错误串,空=安全。"""
    return public_network_url_error(url)


def _public_http_open(
    url: str,
    *,
    headers: dict | None = None,
    timeout: int = 20,
):
    """Anonymous requests transport with a public-network check on every hop."""
    import requests

    sess = requests.Session()
    current = str(url or "")
    try:
        for hop in range(6):
            error = _url_safe(current)
            if error:
                raise ValueError(f"blocked URL: {error}")
            response = sess.get(
                current,
                timeout=timeout,
                stream=True,
                headers=headers or {},
                allow_redirects=False,
            )
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                return sess, response
            if hop >= 5:
                response.close()
                raise ValueError("too many redirects")
            next_url = urljoin(str(getattr(response, "url", "") or current), location)
            error = _url_safe(next_url)
            response.close()
            if error:
                raise ValueError(f"blocked redirect: {error}")
            current = next_url
    except Exception:
        sess.close()
        raise


def _public_http_text(
    url: str,
    *,
    headers: dict | None = None,
    timeout: int = 20,
    max_bytes: int = 8 * 1024 * 1024,
) -> tuple[str, str]:
    """Fetch anonymous public HTML with bounded bytes and safe redirects."""
    sess, response = _public_http_open(url, headers=headers, timeout=timeout)
    try:
        response.raise_for_status()
        raw = b""
        for chunk in response.iter_content(65536):
            raw += chunk
            if len(raw) > max_bytes:
                raise ValueError(f"页面超过 {max_bytes // (1024 * 1024)}MB")
        encoding = str(getattr(response, "encoding", "") or "")
        if not encoding or encoding.lower() == "iso-8859-1":
            match = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
            encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return text, str(getattr(response, "url", "") or url)
    finally:
        try:
            response.close()
        finally:
            sess.close()


def _fetch_web_page(url: str) -> dict:
    """抓取 + Readability 抽正文 + 消毒 + 存 vault。返回 {ok, file, title} 或 {ok:False, error}。"""
    err = _url_safe(url)
    if err:
        return {"ok": False, "error": err}
    try:
        html_raw, _final_url = _public_http_text(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        }, max_bytes=8 * 1024 * 1024)
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


# 浏览器主页/搜索引擎。选型一路被实测推翻:
#   Google ✗异常流量+reCAPTCHA 域名无解 / Bing ✗Cloudflare / Startpage ✗出不来结果
#   Brave 曾✓,但**用户实测高频后触发它自己的机器人验证**(在 iframe 里无法完成)→ 弃
#   → 定 **DuckDuckGo lite**(lite.duckduckgo.com):专为无脚本/低带宽设计,极轻(~22K)、
#     有真实结果、**不弹验证**(实测那个 "robot" 是 <meta name=robots> 误报,非验证页)。
# 备选 ecosia(curl_cffi 救回,155K,较重)。Google/Brave 仍可手动访问,搜索被拦时自动兜底转 ddg-lite。
WEB_HOME = "https://lite.duckduckgo.com/lite/"
WEB_SEARCH_URL = "https://lite.duckduckgo.com/lite/?q={q}"
_SEARCH_FALLBACK = "https://lite.duckduckgo.com/lite/?q={q}"
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

# ⚠ B站不再强转 embed(用户拍板 2026-07-19:要像 Obsidian 那样打开**完整 B站网页**,
#   不是光秃秃的播放器)。完整页走拦截式代理,页内的 player.bilibili.com iframe 由 _EMBED_HOSTS
#   直连、视频流靠 iPad Safari 自身编解码器播 —— 我这台无头 Chromium 缺 H.264/AAC 测不出真播,
#   实测已证:直连 embed / 经代理 / 完整代理三条路输出逐字一致,"播不了"来自测试环境不是代理。
#   YouTube 仍转 embed(完整 YT 页极重且强依赖登录,embed 体验更好,用户未要求改)。
_VIDEO_PAT = (
    (re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([\w-]{6,})", re.I),
     "https://www.youtube-nocookie.com/embed/{0}?rel=0"),
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
def _mirror(prefix: str, u: str, cap: str = "") -> str:
    try:
        p = urlparse(u)
        path = p.path or "/"
        out = f"/pdf/web/{prefix}/{p.scheme}/{p.netloc}{quote(path, safe='/@:+~!$&*,;=()')}"
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k != "__bwcap"]
        if cap:
            query.append(("__bwcap", cap))
        if query:
            out += "?" + urlencode(query, doseq=True)
        return out
    except Exception:
        return u


def unmirror(rest: str, query: str) -> str:
    """路径镜像 → 真实地址(/pdf/web/p/https/例.com/a/b?q=1 → https://例.com/a/b?q=1)。"""
    bits = (rest or "").split("/", 2)
    if len(bits) < 2 or bits[0] not in ("http", "https"):
        return ""
    u = f"{bits[0]}://{bits[1]}/" + (bits[2] if len(bits) > 2 else "")
    clean_query = [(k, v) for k, v in parse_qsl(query or "", keep_blank_values=True)
                   if k != "__bwcap"]
    if clean_query:
        u += "?" + urlencode(clean_query, doseq=True)
    return u


def _pxr(u: str, cap: str = "") -> str:
    """资源 → 子资源代理(路径镜像:JS 模块的 ./相对导入、CSS 的 ../ 也才解析得对)。"""
    return _mirror("r", u, cap)


def _pxp(u: str, cap: str = "") -> str:
    """页面 → 主文档代理。"""
    return _mirror("p", u, cap)


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


def _rewrite_css(css: str, base: str, cap: str = "") -> str:
    def _u(m):
        a = _absu(m.group(2), base)
        return f"url({m.group(1)}{_pxr(a, cap)}{m.group(1)})" if a else m.group(0)

    def _i(m):
        a = _absu(m.group(2), base)
        return f"@import {m.group(1)}{_pxr(a, cap)}{m.group(1)}" if a else m.group(0)

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


def rewrite_html(html: str, base: str, cap: str = "") -> str:
    """把页面里所有资源/导航 URL 重写到我们的代理(取代 <base href>——那个正是跨源被挡的根因)。"""
    def _res(m):
        a = _absu(m.group(3), base)
        return f"{m.group(1)}{m.group(2)}{_pxr(a, cap)}{m.group(2)}" if a else m.group(0)

    def _nav(m):
        # <a>/<form> 只绝对化、**不**代理:点击由注入脚本拦截后 postMessage 给外壳(它按真实地址走);
        # 万一没拦住(中键/target=_blank),回落到原站也是合理行为,总好过打到我们域名的死链。
        a = _absu(m.group(3), base)
        return f"{m.group(1)}{m.group(2)}{a}{m.group(2)}" if a else m.group(0)

    def _ifr(m):
        a = _absu(m.group(3), base)
        if not a:
            return m.group(0)
        u = a if any(h in a for h in _EMBED_HOSTS) else _pxp(a, cap)   # 官方 embed 直连
        return f"{m.group(1)}{m.group(2)}{u}{m.group(2)}"

    def _ss(m):
        out = []
        for part in m.group(3).split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split(None, 1)
            a = _absu(bits[0], base)
            out.append((_pxr(a, cap) if a else bits[0]) + ((" " + bits[1]) if len(bits) > 1 else ""))
        return f"{m.group(1)}{m.group(2)}{', '.join(out)}{m.group(2)}"

    html = _INTEGRITY.sub(" ", html)          # SRI 会因我们改写 CSS 而失败,必须剥
    html = _IFRAME.sub(_ifr, html)
    html = _RES_ATTR.sub(_res, html)
    html = _NAV_ATTR.sub(_nav, html)
    html = _SRCSET.sub(_ss, html)
    html = _STYLE_ATTR.sub(lambda m: f"{m.group(1)}{m.group(2)}{_rewrite_css(m.group(3), base, cap)}{m.group(2)}", html)
    html = _STYLE_TAG.sub(lambda m: m.group(1) + _rewrite_css(m.group(2), base, cap) + m.group(3), html)
    return html


# ⚠ 舱壁:一个重站单页要拉 100+ 子资源,每条都占一个 gunicorn 线程等上游网络 I/O。
# 服务只有 --threads 32,不设限 = 浏一个 github 就能把线程池吃光,SSE / 阅读器 / 语音全饿死
# (与 [[sse-thread-starvation]] 同一类事故,只是放大了一个量级)。代理流量最多占 10 条,
# 满了就 503——掉一张图,远好过全站宕机。
_RES_GATE = threading.Semaphore(10)

# 子资源磁盘缓存:现代站的 js/字体/图片多为内容哈希命名(不可变),重复代理纯属浪费,
# 还会招来对方限流(实测 stackoverflow 一页 100+ 请求直接 429)。命中即免舱壁免上游。
# CSS 会嵌入短期 capability 的递归资源地址，因此绝不能写入这份跨请求持久缓存。
RESCACHE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "web-rescache"
_RESCACHE_MAX = 3 * 1024 * 1024
_RESCACHE_TTL = 7 * 86400
_RESCACHE_CT = ("javascript", "image/", "font/", "application/font",
                "application/x-font", "text/javascript")


def _rescache_ok(ct: str) -> bool:
    c = (ct or "").lower()
    return any(x in c for x in _RESCACHE_CT)


def _rescache_path(
    url: str,
    *,
    user_id: str | None = None,
    scope_origin: str | None = None,
) -> Path:
    """Partition proxy cache by account and the capability's full origin."""
    uid = str(_px_uid() if user_id is None else user_id)
    scope = (
        _active_proxy_origin()
        if scope_origin is None
        else normalize_web_proxy_origin(scope_origin)
    )
    partitioned = "\0".join((uid, scope, str(url or "")))
    return RESCACHE_DIR / (hashlib.sha1(partitioned.encode("utf-8")).hexdigest() + ".bin")


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


# state/web-trcache 历史文件原样保留；运行时代码不再定位、读取或写入。
# 正式页面级缓存完全归扩展的账户分区 IndexedDB。
_WEB_TRANSLATE_GOOGLE_NS = "web-google-v1-gtranslate-v2"
_WEB_TRANSLATE_DOCUMENT_HEADER = "X-BW-Translate-Document"
_WEB_TRANSLATE_ALLOWED_KEYS = frozenset(("texts", "backend", "glossary", "mode"))
_WEB_TRANSLATE_MAX_TEXTS = 120
_WEB_TRANSLATE_MAX_TEXT_CHARS = 4000
_WEB_TRANSLATE_MAX_TOTAL_CHARS = 48000
_WEB_TRANSLATE_MAX_GLOSSARY = 40
_WEB_TRANSLATE_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _parse_web_translate_request(body) -> dict:
    """严格解析网页句子批翻请求；auto 必须在可信扩展层先解析。"""
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    unknown = sorted(set(body) - _WEB_TRANSLATE_ALLOWED_KEYS)
    if unknown:
        raise ValueError("不支持的字段:" + ",".join(unknown))
    raw_texts = body.get("texts")
    if not isinstance(raw_texts, list):
        raise ValueError("texts 必须是数组")
    if len(raw_texts) > _WEB_TRANSLATE_MAX_TEXTS:
        raise ValueError(f"texts 最多 {_WEB_TRANSLATE_MAX_TEXTS} 项")
    texts = []
    total_chars = 0
    for raw in raw_texts:
        if not isinstance(raw, str):
            raise ValueError("texts 每一项都必须是字符串")
        text = raw.strip()
        if len(text) > _WEB_TRANSLATE_MAX_TEXT_CHARS:
            raise ValueError(f"单句最多 {_WEB_TRANSLATE_MAX_TEXT_CHARS} 字符")
        total_chars += len(text)
        if total_chars > _WEB_TRANSLATE_MAX_TOTAL_CHARS:
            raise ValueError(f"文本总量最多 {_WEB_TRANSLATE_MAX_TOTAL_CHARS} 字符")
        texts.append(text)
    backend = body.get("backend", "google")
    if not isinstance(backend, str) or backend not in ("google", "ai"):
        raise ValueError("backend 只能是 google 或 ai")
    mode = body.get("mode", "stateless")
    if not isinstance(mode, str) or mode not in ("stateless", "session"):
        raise ValueError("mode 只能是 stateless 或 session")
    if backend == "google" and mode != "stateless":
        raise ValueError("google 只允许默认 stateless mode")
    raw_glossary = body.get("glossary", {})
    if not isinstance(raw_glossary, dict):
        raise ValueError("glossary 必须是对象")
    if len(raw_glossary) > _WEB_TRANSLATE_MAX_GLOSSARY:
        raise ValueError(f"glossary 最多 {_WEB_TRANSLATE_MAX_GLOSSARY} 项")
    glossary = {}
    for raw_key, raw_value in raw_glossary.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ValueError("glossary 的键和值都必须是字符串")
        key, value = raw_key.strip(), raw_value.strip()
        if not key or not value or len(key) > 120 or len(value) > 240:
            raise ValueError("glossary 键值为空或过长")
        glossary[key] = value
    if backend != "ai" and glossary:
        raise ValueError("glossary 只允许用于 ai")
    return {
        "texts": texts,
        "backend": backend,
        "glossary": glossary,
        "mode": mode,
    }


def _parse_web_translate_document(value, *, required=False):
    """只接受 canonical lowercase RFC 4122 UUIDv4；不宽松修复或回显敌对值。"""
    if value is None:
        if required:
            raise ValueError(f"{_WEB_TRANSLATE_DOCUMENT_HEADER} 必填")
        return None
    if not isinstance(value, str) or not _WEB_TRANSLATE_UUID4_RE.fullmatch(value):
        raise ValueError(f"{_WEB_TRANSLATE_DOCUMENT_HEADER} 必须是规范 UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError(f"{_WEB_TRANSLATE_DOCUMENT_HEADER} 必须是规范 UUIDv4")
    if (
        str(parsed) != value
        or parsed.version != 4
        or parsed.variant != uuid.RFC_4122
    ):
        raise ValueError(f"{_WEB_TRANSLATE_DOCUMENT_HEADER} 必须是规范 UUIDv4")
    return value


def _merge_web_translate_reasons(*values):
    reasons = []
    for value in values:
        for item in str(value or "").split(";"):
            item = item.strip()
            if item and item not in reasons:
                reasons.append(item)
    return ";".join(reasons)


def _cache_user_id(user_id: str | int | None = None) -> str:
    return normalize_cache_user_id(_px_uid() if user_id is None else user_id)


_JARS: dict = {}     # per-user cookie jar:很多站没 cookie 直接 403/跳登录


# TLS/HTTP2 指纹伪装:requests 的指纹一眼是脚本,被一批站(实测 chatgpt/vimeo)拦;curl_cffi
# 伪装成 Chrome 后救回(claude.ai 那种主动 JS 挑战仍过不了,少数)。配合 Pi 的 SoftBank 住宅 IP。
IMPERSONATE = "chrome"
# per-user 导入的登录 cookie(state/web-cookies/<uid>.json,0600)。解决"代理=服务端身份、没有你的
# 登录态"这一类:用户在真浏览器登录后把 cookie 粘进来,代理带上它 → B站图片防盗链/登录态即通。
WEBCOOKIE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "web-cookies"


def _px_uid() -> str:
    try:
        return str(session.get("user_id") or getattr(g, "web_proxy_user_id", "") or "anon")
    except Exception:
        return "anon"


def _proxy_cap_secret():
    return os.environ.get("READER_WEB_PROXY_SECRET") or current_app.secret_key


def _rbi_ticket_secret() -> bytes:
    global _RBI_TICKET_SECRET
    if _RBI_TICKET_SECRET is None:
        project = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
        _RBI_TICKET_SECRET = load_rbi_ticket_secret(project)
    return _RBI_TICKET_SECRET


def _new_rbi_ticket() -> tuple[str, int]:
    uid = _px_uid()
    if not uid.isdigit() or int(uid) <= 0:
        abort(403)
    active_user_check = current_app.config.get("READER_ACTIVE_USER_CHECK")
    if not callable(active_user_check) or not active_user_check(int(uid)):
        # A stale signed session must not keep minting fresh RBI credentials
        # after its account was removed or disabled.
        abort(403)
    ticket = issue_rbi_ticket(_rbi_ticket_secret(), int(uid))
    claims = verify_rbi_ticket(_rbi_ticket_secret(), ticket)
    if claims is None:
        abort(403)
    return ticket, claims.expires_at


def _url_scope_host(url: str) -> str:
    try:
        return normalize_web_proxy_scope(urlparse(str(url or "")).hostname or "")
    except ValueError:
        return ""


def _url_scope_origin(url: str) -> str:
    try:
        return normalize_web_proxy_origin(url)
    except ValueError:
        return ""


def _active_proxy_scope() -> str:
    try:
        return str(getattr(g, "web_proxy_scope_host", "") or "")
    except Exception:
        return ""


def _active_proxy_origin() -> str:
    try:
        scheme = str(getattr(g, "web_proxy_scope_scheme", "") or "")
        host = str(getattr(g, "web_proxy_scope_host", "") or "")
        port = int(getattr(g, "web_proxy_scope_port", 0) or 0)
        if not (scheme and host and port):
            return ""
        return canonical_web_proxy_origin(scheme, host, port)
    except (RuntimeError, TypeError, ValueError):
        return ""


def _current_proxy_cap(
    scope_url: str,
    *,
    fresh: bool = False,
    allow_rescope: bool = False,
    require_scope_match: bool = False,
) -> str:
    """Reuse a scoped cap; mint/re-scope only after an explicit outer-shell grant."""
    uid = _px_uid()
    if not uid.isdigit() or int(uid) <= 0:
        abort(403)
    scope_origin = _url_scope_origin(scope_url)
    if not scope_origin:
        abort(403)
    incoming = str(request.args.get("__bwcap") or "")
    details = verify_web_proxy_cap_details(_proxy_cap_secret(), incoming)
    if details and details.user_id == int(uid):
        g.web_proxy_scope_host = details.scope_host
        g.web_proxy_scope_scheme = details.scope_scheme
        g.web_proxy_scope_port = int(details.scope_port)
        g.web_proxy_cap_expires_at = int(details.expires_at)
        # A bearer capability must never be able to renew itself or pivot into a
        # fresh scope.  A session cookie may ride sandbox requests, so it is not
        # sufficient by itself: re-scoping also needs the outer-only nav ticket.
        scope_matches = web_proxy_cap_matches_url(details, scope_url)
        if require_scope_match and not scope_matches and not allow_rescope:
            abort(403)
        if (not fresh and (scope_matches or not require_scope_match)) or not allow_rescope:
            return incoming
    elif not session.get("user_id") or not allow_rescope:
        abort(403)

    token = issue_web_proxy_cap(
        _proxy_cap_secret(),
        int(uid),
        scope_url=scope_url,
    )
    minted = verify_web_proxy_cap_details(_proxy_cap_secret(), token)
    if not minted:  # defensive: issued tokens must always verify
        abort(403)
    g.web_proxy_scope_host = minted.scope_host
    g.web_proxy_scope_scheme = minted.scope_scheme
    g.web_proxy_scope_port = int(minted.scope_port)
    g.web_proxy_cap_expires_at = int(minted.expires_at)
    return token


_WEB_NAV_TICKET_SESSION_KEY = "reader_web_navigation_ticket"


def _web_navigation_ticket() -> str:
    value = str(session.get(_WEB_NAV_TICKET_SESSION_KEY) or "")
    if len(value) < 32:
        value = secrets.token_urlsafe(32)
        session[_WEB_NAV_TICKET_SESSION_KEY] = value
    return value


def _valid_web_navigation_ticket() -> bool:
    expected = str(session.get(_WEB_NAV_TICKET_SESSION_KEY) or "")
    supplied = str(request.args.get("__bwnav") or "")
    return bool(
        session.get("user_id")
        and len(expected) >= 32
        and len(supplied) == len(expected)
        and secrets.compare_digest(supplied, expected)
    )


def _cookie_store(uid: str) -> dict:
    return load_cookie_store(WEBCOOKIE_DIR, uid)


def _cookie_store_save(uid: str, data: dict):
    try:
        save_cookie_store(WEBCOOKIE_DIR, uid, data)
    except Exception:
        pass


def _cookies_for(url: str) -> dict:
    """Return Secure imported cookies only for the exact top-level HTTPS host."""
    return cookie_values_for_url(
        WEBCOOKIE_DIR,
        _px_uid(),
        url,
        expected_host=_active_proxy_scope(),
    )


def _save_resp_cookies(url: str, r):
    """Refresh an explicitly imported exact-host HTTPS jar with safe defaults."""
    try:
        update_cookie_values_for_url(
            WEBCOOKIE_DIR,
            _px_uid(),
            url,
            dict(getattr(r, "cookies", {}) or {}),
            expected_host=_active_proxy_scope(),
        )
    except Exception:
        pass


def _px_open(
    url: str,
    headers: dict,
    timeout: int = 25,
    *,
    redirect_scope_origin: str = "",
):
    """新建 curl_cffi 会话(伪装 Chrome 指纹)+ 注入用户导入的 cookie。返回 (session, response)。
    ⚠ 调用方**必须** session.close();流式则交给 _gated(it, sess)。不复用会话——curl_cffi 同会话
    的流没读完会卡死 handle(实测),每请求独立最稳。

    Redirects are followed manually so every hop receives the same SSRF check.
    Top-level documents may additionally pin redirects to their signed origin.
    """
    from curl_cffi import requests as _cr
    s = _cr.Session(impersonate=IMPERSONATE)
    current = str(url or "")
    top_scope = ""
    if redirect_scope_origin:
        try:
            top_scope = normalize_web_proxy_origin(redirect_scope_origin)
        except ValueError:
            s.close()
            raise ValueError("invalid proxy redirect scope") from None
    try:
        for hop in range(6):
            error = _url_safe(current)
            if error:
                raise ValueError(f"blocked proxy URL: {error}")
            if top_scope and _url_scope_origin(current) != top_scope:
                raise ValueError("blocked cross-scope document redirect")
            r = s.get(
                current,
                headers=headers,
                cookies=_cookies_for(current),
                stream=True,
                timeout=timeout,
                allow_redirects=False,
            )
            location = str(r.headers.get("Location") or "").strip()
            if r.status_code not in (301, 302, 303, 307, 308) or not location:
                return s, r
            if hop >= 5:
                try:
                    r.close()
                except Exception:
                    pass
                raise ValueError("too many proxy redirects")
            next_url = urljoin(str(getattr(r, "url", "") or current), location)
            # Validate before closing/following so a public open redirect can
            # never become a request to loopback, LAN, Tailscale, or metadata.
            error = _url_safe(next_url)
            if error:
                try:
                    r.close()
                except Exception:
                    pass
                raise ValueError(f"blocked proxy redirect: {error}")
            if top_scope and _url_scope_origin(next_url) != top_scope:
                try:
                    r.close()
                except Exception:
                    pass
                raise ValueError("blocked cross-scope document redirect")
            try:
                r.close()
            except Exception:
                pass
            current = next_url
    except Exception:
        s.close()
        raise


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
_WEB_PROXY_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox "
    "allow-downloads allow-modals allow-presentation; frame-ancestors 'self'"
)

_PROXY_INJECT = """
<style>
/* iOS 长按链接/图片会弹 Safari 自己的预览菜单(「在新标签页中打开」),把长按这个手势整个抢走
   —— 而长按在我们的阅读器里是有语义的(选中/便签/词组)。用与 rc-stickynote 相同的组合:
   禁 callout 菜单,但保留文字可选。桌面右键菜单不动(那是合理预期)。 */
a, img, video, button { -webkit-touch-callout: none !important; }
a { -webkit-user-drag: none; }
body { -webkit-tap-highlight-color: rgba(0,0,0,0); }
</style>
<script>
(function(){
  if(window.__rcShim) return;   // 幂等:注入位置调整过,别让同一份 shim 装两遍
  window.__rcShim = 1;
  var _rcName = /^bw-web-bridge:([A-Za-z0-9_-]{32,128})$/.exec(window.name || '');
  var _rcNonce = _rcName ? _rcName[1] : '';
  function _rcPost(payload){
    if(!_rcNonce || !payload) return;
    payload.__rcwebNonce = _rcNonce;
    try{ parent.postMessage(payload, '*'); }catch(_){}
  }
  // 站内导航拦截:链接/表单跳转改走代理(留在我们的壳里);新窗口链接也接管
  document.addEventListener('click', function(e){
    var a = e.target && e.target.closest && e.target.closest('a[href]');
    if(!a) return;
    var href = a.getAttribute('href') || '';
    if(!href || href.charAt(0)==='#' || /^(javascript|mailto|tel):/i.test(href)) return;
    e.preventDefault();
    try{ _goProxy(new URL(href, location.__realBase || document.baseURI).href); }catch(_){}
  }, true);
  // 选区上报:父壳据此弹工具条(同源本可直接读,postMessage 更稳且面向未来跨源)
  function report(){
    try{
      var s = getSelection(); var t = s && !s.isCollapsed ? String(s).trim() : '';
      var r = t && s.rangeCount ? s.getRangeAt(0).getBoundingClientRect() : null;
      _rcPost({__rcweb:'sel', text: t,
        rect: r ? {left:r.left, top:r.top, right:r.right, bottom:r.bottom} : null,
        ctx: t ? (function(){ var n = s.anchorNode; n = n && (n.nodeType===3?n.parentElement:n);
                   var b = n && n.closest ? n.closest('p,li,td,blockquote,h1,h2,h3,div') : null;
                   return b ? (b.innerText||'').slice(0,1200) : ''; })() : ''});
    }catch(_){}
  }
  // 长按开始就把链接的原生拖拽/预览掐掉(iOS 的 callout 已由上面的 CSS 关掉,这里管住残余路径:
  //   拖拽启动 + 长按后误触发的 click)。⚠ 用**冒泡**阶段,捕获阶段 stopPropagation 会吞掉
  //   页面内部按钮的事件(项目里踩过:见 overlay-gate-use-bubble-not-capture)。
  document.addEventListener('dragstart', function(e){
    if(e.target && e.target.closest && e.target.closest('a,img')) e.preventDefault();
  });
  document.addEventListener('mouseup', function(){ setTimeout(report, 10); });
  document.addEventListener('touchend', function(){ setTimeout(report, 10); });
  // 供父壳取整页正文(AI 上下文/存档)
  window.addEventListener('message', function(e){
    if(e.source !== parent) return;
    var d = e.data || {};
    if(d.__rcweb === 'getText'){
      _rcPost({__rcweb:'text', text:(document.body.innerText||'').slice(0,120000),
               title: document.title, url: location.__realBase || ''});
    }
  });
  // ── 运行时重写 shim(服务端只能改静态 HTML;页面 JS 跑起来发的请求得在这儿接)──
  var B = location.__realBase || location.href;
  var CAP = String(location.__rcCap || '');
  function WITHCAP(u){
    if(!CAP) return u;
    try{
      var x = new URL(u, location.href);
      x.searchParams.set('__bwcap', CAP);
      return x.pathname + x.search;
    }catch(e){ return u; }
  }
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
      return WITHCAP('/pdf/web/r/' + p.protocol.replace(':','') + '/' + p.host + p.pathname + p.search);
    }catch(e){ return u; }
  }
  // fetch
  var _f = window.fetch;
  // sandbox 去掉 allow-same-origin 后，页内不能携带 App cookie。沉浸翻译只通过父壳的
  // 三端点白名单代理；第三方脚本不能再直接调用同源账户 API。
  var _apiPending = {}, _apiSeq = 0;
  window.addEventListener('message', function(e){
    if(e.source !== parent) return;
    var d = e.data || {};
    if(d.__rcweb !== 'api-result' || !d.id || !_apiPending[d.id]) return;
    var pending = _apiPending[d.id];
    delete _apiPending[d.id];
    clearTimeout(pending.timer);
    var p = d.payload || {};
    var response = {
      ok: p.ok === true,
      status: Number(p.status) || 0,
      headers: { get: function(name){ return String(name || '').toLowerCase() === 'content-type' ? String(p.contentType || '') : null; } },
      text: function(){ return Promise.resolve(String(p.body || '')); },
      json: function(){
        try{ return Promise.resolve(JSON.parse(String(p.body || '{}'))); }
        catch(_){ return Promise.reject(new Error(p.error || 'invalid sandbox api response')); }
      }
    };
    pending.resolve(response);
  });
  function _rcApiFetch(input, init){
    init = init || {};
    var path = typeof input === 'string' ? input : (input && input.url) || '';
    var parsed;
    var appOrigin;
    try{
      appOrigin = new URL(location.href).origin;
      parsed = new URL(path, location.href);
    }catch(_){ return Promise.reject(new Error('invalid sandbox api url')); }
    if(parsed.origin !== appOrigin ||
       ['/pdf/api/web-translate','/pdf/api/web-vocab'].indexOf(parsed.pathname) < 0){
      return Promise.reject(new Error('sandbox api not allowed'));
    }
    var id = 'a' + Date.now().toString(36) + (++_apiSeq).toString(36);
    return new Promise(function(resolve, reject){
      var timer = setTimeout(function(){
        delete _apiPending[id];
        reject(new Error('sandbox api timeout'));
      }, 35000);
      _apiPending[id] = { resolve: resolve, reject: reject, timer: timer };
      _rcPost({
        __rcweb:'api',
        id:id,
        path:parsed.pathname + parsed.search,
        method:String(init.method || 'GET').toUpperCase(),
        body:init.body == null ? '' : String(init.body)
      });
    });
  }
  try{
    Object.defineProperty(window, '__rcRawFetch', {
      value:_rcApiFetch, writable:false, configurable:false
    });
  }catch(_){ window.__rcRawFetch = _rcApiFetch; }
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
  // ⚠ setter 陷阱(审计实锤,MutationObserver **追不上**这一类):
  //   reddit 这种站用 `script.src = "https://…绝对地址"` 运行时拼装加载,浏览器在**赋值那一刻**
  //   就发出请求,而 observer 要等节点插入后的微任务才跑 —— 于是请求已经以跨源身份发出去、被 CORS 挡死。
  //   实测证据:5 条 redditstatic concat chunk 全 net::ERR_FAILED + "blocked by CORS policy";
  //   装上本陷阱后 CORS 报错 5→0、chunk 全部 200 走 /pdf/web/r/、自定义元素正常 hydrate。
  try{
    [[HTMLScriptElement,'src'],[HTMLImageElement,'src'],[HTMLLinkElement,'href'],
     [HTMLIFrameElement,'src'],[HTMLSourceElement,'src'],[HTMLMediaElement,'src']].forEach(function(pair){
      var C = pair[0], k = pair[1];
      if(!C) return;
      var d = Object.getOwnPropertyDescriptor(C.prototype, k);
      if(!d || !d.set || !d.configurable) return;
      Object.defineProperty(C.prototype, k, {
        configurable: true, enumerable: d.enumerable,
        get: function(){ return d.get ? d.get.call(this) : ''; },
        set: function(v){
          try{
            if(this.getAttribute && this.getAttribute('data-rc-own')) return d.set.call(this, v);
            if(C === HTMLIFrameElement){        // iframe:官方 embed 直连,其余走主文档镜像
              var a0 = ABS(v);
              if(a0 && a0.indexOf(location.origin) !== 0){
                var emb = ['youtube.com/embed/','youtube-nocookie.com/embed/','player.bilibili.com',
                           'player.vimeo.com','open.spotify.com/embed'].some(function(h){ return a0.indexOf(h)>=0; });
                return d.set.call(this, emb ? a0 : WITHCAP('/pdf/web/p/' + a0.replace('://','/')));
              }
              return d.set.call(this, v);
            }
            return d.set.call(this, RES(v));
          }catch(_){ return d.set.call(this, v); }
        }
      });
    });
  }catch(_){}
  // setAttribute 也是同一条路(有些库不用 property 赋值)
  try{
    var _sa = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(k, v){
      try{
        if((k === 'src' || k === 'href') && !(this.getAttribute && this.getAttribute('data-rc-own'))
           && this.tagName !== 'A' && this.tagName !== 'FORM' && this.tagName !== 'IFRAME'){
          return _sa.call(this, k, RES(v));
        }
      }catch(_){}
      return _sa.call(this, k, v);
    };
  }catch(_){}
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
  //   所以必须把这几个程序化入口全接管。location.href= 的 setter 无法 patch；
  //   漏出的请求按普通 App 路由/404 处理，绝不靠 Referer 猜来源后代签代理请求。
  // ⚠ 剥壳(实测 B站根因):页面 JS 常读 location.href(此刻=**我们的代理地址**)再跳转,
  //   若不识别就会把代理地址当外部地址**再套一层代理** → host 变成我们自己的 .ts.net →
  //   被 SSRF 判"内网" → 整页崩。任何读 location.href 跳转的站(canonical / spm 追踪)都中招。
  function _unmirror(u){
    try{
      var m = new URL(u, B);
      if(m.origin === location.origin){
        var pp = m.pathname;                       // 无反斜杠实现,免字符串转义告警
        if(pp.indexOf('/pdf/web/p/') === 0 || pp.indexOf('/pdf/web/r/') === 0){
          var bits = pp.slice(11).split('/');      // '/pdf/web/p/'.length === 11
          if(bits.length >= 2 && (bits[0] === 'https' || bits[0] === 'http')){
            var clean = new URLSearchParams(m.search);
            clean.delete('__bwcap');
            return bits[0] + '://' + bits[1] + '/' + bits.slice(2).join('/') +
                   (clean.toString() ? ('?' + clean.toString()) : '');
          }
          return '';
        }
        return '';   // 我们自己的其它页面(/pdf/web/live 等)——**不是**外部导航,忽略
      }
      return m.href;
    }catch(e){ return ''; }
  }
  // ⚠ 导航一律**在 iframe 内直接完成**(实测 iPad 点不动的架构疑点):原设计是
  //   iframe→postMessage→外壳→回设 frame.src 的跨框架链,桌面可靠但 iOS Safari 上跨框架
  //   回设 src 时机常失效 = 点了没反应。现在 iframe 自己 location.href 导航(location.href 的
  //   setter 我们没 patch,不递归),外壳只被动同步地址栏。
  function _goProxy(u){
    var real = _unmirror(u);
    if(real === '') return false;           // 自引用(已是我们自己的页面)→ 忽略
    real = real || (ABS(u) || u);
    try{
      // 跨站导航交回可信父壳：它持有不会传播进代理页的 nav ticket，
      // 可为新顶层 host 签发新 scope。当前 bearer cap 本身绝不换域续签。
      if(new URL(real).origin.toLowerCase() !== new URL(B).origin.toLowerCase()){
        _rcPost({__rcweb:'nav', url: real});
        return true;
      }
    }catch(_){}
    _rcPost({__rcweb:'located', url: real});
    location.href = WITHCAP('/pdf/web/frame?url=' + encodeURIComponent(real));
    return true;
  }
  function _navOut(u){ return _goProxy(u); }   // form/程序化导航共用同一条可靠路径
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
      _rcPost({__rcweb:'nav', url: u.href});
    }catch(_){}
  }, true);
  function _ready(){ _rcPost({__rcweb:'ready', title: document.title}); }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _ready);
  else _ready();
})();
</script>
<script src="/static/pdf/web-immersive.js" data-rc-own="1"></script>
"""


def _proxy_page(url: str, cap: str):
    """代理网页在 opaque sandbox 中执行；cap 仅授权代理文档/子资源传输。"""
    err = _url_safe(url)
    if err:
        return None, err
    cap_details = verify_web_proxy_cap_details(_proxy_cap_secret(), cap)
    if not cap_details or not web_proxy_cap_matches_url(cap_details, url):
        return None, "代理页面 capability scope 不匹配"
    budget = _WEB_PROXY_BUDGETS.begin(cap, cap_details)
    if budget is None:
        return None, "网页代理短期访问额度已用完，请从阅读器重新打开页面"
    _pxs = None
    try:
        _h = _px_headers(url)
        _h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        _h.pop("Origin", None)                     # 顶层导航不带 Origin(带了反而像 CSRF 被拒)
        _pxs, r = _px_open(
            url,
            _h,
            timeout=20,
            redirect_scope_origin=cap_details.scope_origin,
        )
    except Exception as ex:
        return None, f"抓取失败:{str(ex)[:120]}"
    try:
        ct = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ct:
            return None, f"不是网页({ct.split(';')[0] or '未知类型'});图片/PDF 等请直接在原站打开"
        _save_resp_cookies(str(getattr(r, "url", "") or url), r)
        raw = b""
        for chunk in r.iter_content(65536):
            if not budget.consume(len(chunk)):
                return None, "网页正文超过单次或短期访问流量上限"
            raw += chunk
            if len(raw) > 12 * 1024 * 1024:
                return None, "网页正文超过 12MB 安全上限"
    finally:
        try:
            _pxs.close()
        except Exception:
            pass
    # ⚠ 流式读完后**不能**再碰 r.apparent_encoding(它要 r.content,已消费 → RuntimeError,
    #   实测 mhlw.go.jp 500 的根因)。自己按 raw 检测:响应头 → <meta charset> → chardet → utf-8。
    _renc = getattr(r, "encoding", "") or ""
    enc = _renc if (_renc and _renc.lower() != "iso-8859-1") else ""
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
    html = rewrite_html(html, final, cap)  # ★ 根因修复:资源/CSS/srcset 全部改走我们的代理
    # ⚠ shim 必须在**页面自己的脚本之前**跑(审计实锤):原来 `html += _PROXY_INJECT` 挂在文档末尾,
    #   而 reddit 的模块加载器在第 ~34000 字节就执行 `script.src = 绝对地址` —— 浏览器在赋值那刻
    #   就发请求,setter 陷阱那时还没装上,于是照样跨源被 CORS 挡死(实测 CORS 报错 6 条不减)。
    #   现在整块注入提到 <head> 最前,__realBase 也一并前置(RES() 依赖它解析相对地址)。
    base_tag = (
        f'<script>location.__realBase={json.dumps(final)};'
        f'location.__rcCap={json.dumps(cap)};</script>'
        + _PROXY_INJECT
    )
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
    # ⚠ 挑战页的**正文往往是空的**(内容由 JS 渲染),光看话术判不到(实测 claude.ai 正文长度 0)。
    #   所以先认 HTML 里的硬标记,再退回话术。
    _CF_MARKS = ("cdn-cgi/challenge-platform", "<title>Just a moment", "challenge-error-text",
                 "__cf_chl", "cf-turnstile", "/recaptcha/api.js")
    blocked = ("/sorry/" in final or "/recaptcha/" in final
               or any(m in html for m in _CF_MARKS)
               or (len(_bare) < 6000 and any(x in _bare for x in _BLOCK_SIGNS)))
    if blocked:
        try:
            _bh = urlparse(final).netloc or ""
            _bn = ("<div style=\"font:14px/1.7 system-ui;padding:14px 16px;background:#fff1f0;"
                   "border-bottom:1px solid #f3c9c5;color:#8a3b34\">"
                   f"🔒 <b>{_hesc(_bh)}</b> 用了反机器人验证(Cloudflare / reCAPTCHA 之类)。"
                   "这类验证的密钥与 TLS 指纹**绑死原站域名**,经本站转发<b>永远无法通过</b>——"
                   "点验证码也没用,不是可以修的问题。<br>"
                   f"→ <a href=\"{_hesc(final)}\" target=\"_blank\" rel=\"noopener\" "
                   "style=\"color:#0a58ca\">在系统浏览器打开这一页</a>"
                   "(需要登录的站也走这里;在本阅读器窗口内直接登录则是可以的,登录态服务端会保持)。"
                   "</div>")
        except Exception:
            _bn = ""
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
                r2, e2 = _proxy_page(alt, cap)
                if r2 is not None and not e2:
                    r2.set_data(_re.sub(r"(<body[^>]*>)", r"\1" + note, r2.get_data(as_text=True),
                                        count=1, flags=_re.I) or r2.get_data(as_text=True))
                    return r2, ""
        except Exception:
            pass
        if _bn:      # 提不出搜索词(纯登录页/普通页被拦)→ 至少把"为什么打不开 + 怎么办"讲清楚
            html = _re.sub(r"(<body[^>]*>)", r"\1" + _bn.replace("\\", "\\\\"), html,
                           count=1, flags=_re.I) or html
    try:   # ★中间转换层(内容侧):浏览过的页面立刻可被 AI 读到。
        #   `web:<url>` 是**视图引用**(同 vbook:),后端必须有 resolver —— 这里在代理时
        #   顺手抽正文写缓存,assistant._page_text 见 web: 前缀即命中(用户实锤:不做这层,
        #   AI read_page 只会说"没能读到这页的文字内容")。
        _web_cache_put(final, _cache_raw)
    except Exception:
        pass
    resp = make_response(html)   # 注入已前置到 <head>,这里不再追加
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    for h in list(resp.headers.keys()):
        if h.lower() in _PROXY_STRIP_HEADERS:
            del resp.headers[h]
    resp.headers["Content-Security-Policy"] = _WEB_PROXY_CSP
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp, ""


# ⚠ 不依赖 register 注入:任何 import 本模块的进程(assistant 工具、脚本、cron)都要能直接用
#   resolver。首版写成 register 时才赋值 → 裸 import 场景 WEB_CACHE_DIR=None → **静默返回空正文**
#   (实测:read_page 有内容而 summarize 说"没抓到正文",同一 resolver 结果不一致的根因)。
WEB_CACHE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "state" / "web-cache"


def _web_key(url: str, *, user_id: str | int | None = None) -> str:
    uid = _cache_user_id(user_id)
    if not uid:
        return ""
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:32]


def _web_cache_path(
    url: str,
    *,
    user_id: str | int | None = None,
) -> Path | None:
    uid = _cache_user_id(user_id)
    if not WEB_CACHE_DIR or not uid:
        return None
    return web_cache_path(WEB_CACHE_DIR, uid, url)


def _web_cache_put(
    url: str,
    raw_html: str,
    *,
    user_id: str | int | None = None,
):
    """把代理过的页面抽成正文存缓存(AI 读页/搜索的数据源)。"""
    uid = _cache_user_id(user_id)
    if not uid:
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
    write_web_cache(
        WEB_CACHE_DIR,
        uid,
        url,
        title=title,
        text=txt,
    )


def web_material(
    ref: str,
    *,
    user_id: str | int | None = None,
) -> dict:
    """`web:<url>` 旧资料 resolver；只读已有账户缓存，不再联网补抓。

    身份形式继续保留，便于扩展/迁移工具引用历史资料；新网页正文应由扩展
    本地读取并显式同步，而不是由 PWA 隐式抓取第三方站点。
    """
    if not isinstance(ref, str) or not ref.startswith("web:"):
        return None
    url = ref[4:]
    uid = _cache_user_id(user_id)
    if not uid:
        return {
            "url": url,
            "title": "",
            "text": "",
            "error": "网页材料缺少已认证用户上下文",
        }
    cached, _source_path = read_web_cache(WEB_CACHE_DIR, uid, url)
    if cached and cached.get("text"):
        return cached
    return {
        "url": url,
        "title": "",
        "text": "",
        "error": "网页资料不在旧缓存中；PWA 已停止联网抓取，请由浏览器扩展读取",
    }


def _web_last_path(user_id: str | int | None = None) -> Path | None:
    uid = _cache_user_id(user_id)
    if not _WEB_LAST or not uid:
        return None
    return _WEB_LAST.parent / "web-last-by-user" / (uid + ".json")


def _web_last_get(*, user_id: str | int | None = None) -> str:
    path = _web_last_path(user_id)
    if path is None:
        return ""
    try:
        d = json.loads(path.read_text("utf-8"))
        rel = str(d.get("file") or "")
        return rel if rel and (_OBSIDIAN_ROOT / rel).exists() else ""
    except Exception:
        return ""


def _web_last_set(rel: str, *, user_id: str | int | None = None):
    path = _web_last_path(user_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"file": rel, "ts": int(time.time())}, ensure_ascii=False),
            "utf-8",
        )
        tmp.replace(path)
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


def register_html_reader(
    bp,
    *,
    safe_vault_path,
    obsidian_root,
    claude_dir,
    asset_cache_version,
    pdf_reader_js_v,
    pdf_shared_js_v,
    reader_sidecar_path=None,
    reader_sidecar_lock=None,
    web_translate_protocol_module=None,
):
    global _WEB_LAST, WEB_CACHE_DIR
    _WEB_LAST = Path(claude_dir) / "state" / "web-last.json"
    WEB_CACHE_DIR = Path(claude_dir) / "state" / "web-cache"   # 与模块级默认同值;显式对齐注入的 claude_dir
    """挂 HTML 阅读器路由到 bp(url_prefix /pdf),并注入路径与唯一静态指纹实现。"""
    global _safe_vault_path, _OBSIDIAN_ROOT, _CLAUDE_DIR
    global _HTML_HL_DIR, _HTML_HL_USER_DIR
    global _HTML_HL_ACCOUNT_PATH, _HTML_HL_ACCOUNT_LOCK
    global _ASSET_CACHE_VERSION, _PDF_READER_JS_V, _PDF_SHARED_JS_V
    _safe_vault_path = safe_vault_path
    _OBSIDIAN_ROOT = obsidian_root
    _CLAUDE_DIR = Path(claude_dir)
    _HTML_HL_DIR = claude_dir / "state" / "html-highlights"
    _HTML_HL_USER_DIR = claude_dir / "state" / "html-highlights-by-user"
    _HTML_HL_ACCOUNT_PATH = reader_sidecar_path
    _HTML_HL_ACCOUNT_LOCK = reader_sidecar_lock
    _ASSET_CACHE_VERSION = asset_cache_version
    _PDF_READER_JS_V = pdf_reader_js_v
    _PDF_SHARED_JS_V = pdf_shared_js_v

    @bp.route("/html/view")
    def html_view():
        """统一 HTML 阅读器主页(架构验收)。?file=<vault-rel .html/.md>;无 file → 内置 sample。"""
        rel = (request.args.get("file") or "").strip()
        if rel == "__web__":
            # 旧收藏/历史入口。原本送回书架,但书架自己也随 PWA 一起退役了
            # (reader_pwa_retirement),再往那儿转等于让用户连撞两次墙 ——
            # 先跳一次,再收到一个 410。直接给退役说明。
            import reader_pwa_retirement as _retired_pwa
            return _retired_pwa.retired_response()
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
            file_name=title, reader_js_v=_html_js_v(),
            reader_app="html", reader_route="html"))
        return resp

    @bp.route("/web/proxy")
    def pdf_web_proxy():
        """Retired: third-party transport belongs to the browser extension."""
        return _retired_web_response()

    @bp.route("/web/p/<path:rest>")
    def pdf_web_page_mirror(rest):
        """Retired path mirror; kept only as a deterministic Gone response."""
        return _retired_web_response()

    @bp.route("/web/r/<path:rest>")
    def pdf_web_res_mirror(rest):
        """Retired resource mirror; never read the old cache or upstream."""
        return _retired_web_response()

    @bp.route("/api/web-trcache")
    def pdf_api_web_trcache():
        """Retired: page URL caches live only in the browser extension."""
        return _retired_web_response()

    @bp.route("/api/web-translate", methods=["POST"])
    def pdf_api_web_translate():
        """网页句子批翻。默认 Google；AI 支持无状态批翻或文档内短时会话。

        普通网页 URL 只供扩展本地页面缓存使用且不会进入请求体；服务端严格拒绝
        URL、model、cache namespace 和 body session key。文档 UUID 只从可信扩展
        写入的请求头读取，并与当前 uid 共同隔离会话。
        """
        try:
            parsed = _parse_web_translate_request(request.get_json(silent=True))
            document_header = request.headers.get(_WEB_TRANSLATE_DOCUMENT_HEADER)
            if parsed["mode"] == "session":
                document_id = _parse_web_translate_document(
                    document_header, required=True
                )
            else:
                if document_header is not None:
                    raise ValueError(
                        f"{_WEB_TRANSLATE_DOCUMENT_HEADER} 只允许用于 session mode"
                    )
                document_id = None
        except ValueError as ex:
            return jsonify({
                "ok": False,
                "code": "invalid_web_translate_request",
                "error": str(ex),
            }), 400
        texts = parsed["texts"]
        backend = parsed["backend"]
        mode_requested = parsed["mode"]
        mode_resolved = mode_requested
        uid = _px_uid()
        profile = None
        session_supported = False
        cache_namespaces = {
            "stateless": _WEB_TRANSLATE_GOOGLE_NS,
            "session": _WEB_TRANSLATE_GOOGLE_NS,
        }
        degraded = False
        reason = ""
        if backend == "ai":
            if not uid or uid == "anon":
                return jsonify({"ok": False, "error": "authentication required"}), 401
            try:
                import assistant as _assistant
                profile = _assistant.web_translate_profile(uid)
            except Exception as ex:
                return jsonify({"ok": False, "error": f"AI profile load fail: {ex}"}), 500
            raw_namespaces = profile.get("cache_namespaces")
            if not isinstance(raw_namespaces, dict):
                raw_namespaces = {}
            stateless_ns = str(
                raw_namespaces.get("stateless")
                or profile.get("cache_namespace")
                or ""
            )
            session_ns = str(
                raw_namespaces.get("session")
                or (stateless_ns + "-session")
            )
            if not stateless_ns or not session_ns:
                return jsonify({"ok": False, "error": "AI cache identity unavailable"}), 500
            cache_namespaces = {
                "stateless": stateless_ns,
                "session": session_ns,
            }
            session_supported = bool(
                profile.get(
                    "session_supported",
                    profile.get("backend") == "claude",
                )
            )
            degraded = bool(profile.get("degraded"))
            reason = str(profile.get("reason") or "")
            if mode_requested == "session" and not session_supported:
                mode_resolved = "stateless"
                degraded = True
                reason = _merge_web_translate_reasons(
                    reason, "session_backend_unsupported"
                )
        if not texts:
            return jsonify({
                "ok": True,
                "zh": [],
                "sources": [],
                "glossary": {},
                "backendRequested": backend,
                "backendUsed": "google" if backend == "google" else "none",
                "modeRequested": mode_requested,
                "modeResolved": mode_resolved,
                "sessionSupported": session_supported,
                "cacheNamespace": cache_namespaces[mode_resolved],
                "cacheNamespaces": cache_namespaces,
                "googleCacheNamespace": _WEB_TRANSLATE_GOOGLE_NS,
                "degraded": degraded,
                "reason": reason,
                "translated": 0,
                "total": 0,
            })
        try:
            # 生产只认部署到 webapp 的模块名；不得再把开发仓库 scripts/vocab
            # 动态塞进 sys.path。测试可通过 register 的显式 module 注入唯一源码。
            _protocol = (
                web_translate_protocol_module
                if web_translate_protocol_module is not None
                else importlib.import_module("web_translate_protocol")
            )
            _ab = _protocol.ai_translate_batch
            _gb = _protocol.gtranslate_batch
            _tr = _protocol.translate
            _cg = _protocol._cache_get
            _cp = _protocol._cache_put
        except Exception as ex:
            return jsonify({"ok": False, "error": f"translate load fail: {ex}"}), 500
        # session 译文依赖文档上下文，绝不能命中跨文档的服务端文本缓存。
        read_ai_cache = backend == "ai" and mode_resolved == "stateless"
        read_cache_ns = (
            cache_namespaces["stateless"]
            if backend == "ai"
            else _WEB_TRANSLATE_GOOGLE_NS
        )
        zhs = [
            (
                (_cg(t, "zh-CN", ns=read_cache_ns) or "")
                if t and (backend != "ai" or read_ai_cache)
                else ""
            )
            for t in texts
        ]
        sources = [("ai" if backend == "ai" else "google") if zhs[i] else "" for i in range(len(texts))]
        miss = [i for i, z in enumerate(zhs) if not z and texts[i]]
        glossary = dict(parsed["glossary"])
        batch_meta = {"ai": 0, "google": 0, "blank": 0, "sources": []}
        runtime = {
            "mode": mode_resolved,
            "reason": "",
        }
        session_attempted = backend == "ai" and mode_resolved == "session"
        if miss and backend == "ai":
            mt = [texts[i] for i in miss]

            def _safe_generate(system_text, user_text):
                if runtime["mode"] == "session":
                    session_output = _assistant.web_translate_session_text(
                        system_text,
                        user_text,
                        uid=uid,
                        document_id=document_id,
                        profile=profile,
                        timeout=60,
                    )
                    if session_output:
                        return session_output
                    # 同一请求余下批次也固定退无状态，避免交替创建/失败会话。
                    runtime["mode"] = "stateless"
                    runtime["reason"] = _merge_web_translate_reasons(
                        runtime["reason"],
                        "session_unavailable_stateless_fallback",
                    )
                return _assistant.web_translate_text(
                    system_text,
                    user_text,
                    uid=uid,
                    profile=profile,
                    timeout=60,
                )

            translated, glossary, batch_meta = _ab(
                mt,
                model=str(profile.get("variant") or "default"),
                effort=str(profile.get("depth") or "none"),
                glossary=glossary,
                generator=_safe_generate,
                cache_ns=(
                    cache_namespaces["session"]
                    if session_attempted
                    else cache_namespaces["stateless"]
                ),
                fallback_cache_ns=_WEB_TRANSLATE_GOOGLE_NS,
                with_meta=True,
                cache_ai=not session_attempted,
            )
            for offset, original_i in enumerate(miss):
                zhs[original_i] = translated[offset]
                sources[original_i] = batch_meta["sources"][offset]
        elif miss:
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
                        sources[i] = "google"
                        try:
                            _cp(
                                texts[i], "zh-CN", batch[k], "gtranslate",
                                ns=_WEB_TRANSLATE_GOOGLE_NS,
                            )
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
                    z = _tr(
                        texts[i],
                        backend="no_ai",
                        cache_ns=_WEB_TRANSLATE_GOOGLE_NS,
                    )
                    if z:
                        zhs[i] = z
                        sources[i] = "google"
                        _cp(
                            texts[i], "zh-CN", z, "no_ai",
                            ns=_WEB_TRANSLATE_GOOGLE_NS,
                        )
                except Exception:
                    pass
        mode_resolved = runtime["mode"]
        reason = _merge_web_translate_reasons(reason, runtime["reason"])
        if mode_requested != mode_resolved:
            degraded = True
        ai_count = sum(1 for source in sources if source == "ai")
        google_count = sum(1 for source in sources if source == "google")
        blank_count = sum(1 for i, source in enumerate(sources) if texts[i] and not source)
        if backend == "ai" and (google_count or blank_count):
            degraded = True
            if google_count and ai_count:
                reason = _merge_web_translate_reasons(
                    reason, "ai_partial_google_fallback"
                )
            elif google_count:
                reason = _merge_web_translate_reasons(
                    reason, "ai_unavailable_google_fallback"
                )
            else:
                reason = _merge_web_translate_reasons(reason, "ai_unavailable")
        if backend == "ai":
            if ai_count and google_count:
                backend_used = str(profile.get("backend")) + "+google"
            elif ai_count:
                backend_used = str(profile.get("backend"))
            elif google_count:
                backend_used = "google"
            else:
                backend_used = "none"
        else:
            backend_used = "google"
        return jsonify({
            "ok": True,
            "zh": zhs,
            "sources": sources,
            "glossary": glossary if backend == "ai" else {},
            "backendRequested": backend,
            "backendUsed": backend_used,
            "modeRequested": mode_requested,
            "modeResolved": mode_resolved,
            "sessionSupported": session_supported,
            # 单值必须跟实际解析模式一致；完整映射供切换/预加载时使用。
            "cacheNamespace": cache_namespaces[mode_resolved],
            "cacheNamespaces": cache_namespaces,
            "googleCacheNamespace": _WEB_TRANSLATE_GOOGLE_NS,
            "degraded": degraded,
            "reason": reason,
            "model": ({
                "backend": profile.get("backend"),
                "variant": profile.get("variant"),
                "depth": profile.get("depth"),
            } if profile else None),
            "translated": sum(1 for z in zhs if z),
            "total": len(texts),
        })

    @bp.route("/api/web-translate-config")
    def pdf_api_web_translate_config():
        """返回当前账户的网页 AI action；缓存身份只能由服务端生成。"""
        uid = _px_uid()
        if not uid or uid == "anon":
            return jsonify({"ok": False, "error": "authentication required"}), 401
        try:
            import assistant as _assistant
            profile = _assistant.web_translate_profile(uid)
        except Exception as ex:
            return jsonify({"ok": False, "error": f"AI profile load fail: {ex}"}), 500
        return jsonify({
            "ok": True,
            "backend": profile["backend"],
            "variant": profile["variant"],
            "depth": profile["depth"],
            "cacheNamespace": profile["cache_namespace"],
            "cacheNamespaces": profile["cache_namespaces"],
            "sessionSupported": bool(profile.get("session_supported")),
            "googleCacheNamespace": _WEB_TRANSLATE_GOOGLE_NS,
            "degraded": bool(profile.get("degraded")),
            "reason": str(profile.get("reason") or ""),
            "requestedBackend": str(profile.get("requested_backend") or ""),
            "requestedVariant": str(profile.get("requested_variant") or ""),
        })

    # Do not add a Referer-based "leaked path rescue" here.  Proxy documents
    # deliberately emit Referrer-Policy:no-referrer, and transport endpoints
    # require a signed capability.  Guessing an origin from an ambient Referer
    # would both be dead code in the intended path and an authorization bypass
    # if a browser/proxy ever supplied one.

    @bp.route("/api/web-vocab", methods=["POST"])
    def pdf_api_web_vocab():
        """网页版句级生词判定。POST {texts:[句子...],threshold?,min_words?}。

        返回每句所有未掌握词的字符区间(用于真实词下划线)，并严格复用 PDF 的双条件：
        未掌握 lemma >= 3 且句子总词数 >= 10 时 hot=true、后台预翻译、显示「译 N」。
        """
        body = request.get_json(silent=True) or {}
        texts = [str(t or "").strip()[:2000] for t in (body.get("texts") or [])][:160]
        thr = max(2, min(8, int(body.get("threshold") or 3)))
        min_words = max(4, min(80, int(body.get("min_words") or 10)))
        if not texts:
            return jsonify({"ok": True, "items": [], "marks": []})
        import sys as _sys
        vp = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")) / "scripts" / "vocab"
        if str(vp) not in _sys.path:
            _sys.path.insert(0, str(vp))
        try:
            import vocab_index          # type: ignore
            idx = vocab_index.index() or {}
        except Exception:
            return jsonify({"ok": True, "items": [], "marks": []})     # 词库读不到 → 静默不标
        unmastered = {f: i for f, i in idx.items()
                      if i.get("label_slug") and i["label_slug"] != "mastered"}
        if not unmastered:
            return jsonify({"ok": True, "items": [], "marks": []})
        # ⚠ 日语不能"切出词再查":日文没有词间空格,连续假名+汉字是一整串
        #   (实测 `没落する没落していく貿易ボタン` 被当成一个 token,一个也匹配不上)。
        #   反过来做——拿词库里的未掌握词去**子串搜**正文,这才是无分词语言的正确方向。
        cjk_un = sorted((w for w in unmastered if not w.isascii() and len(w) >= 2), key=len, reverse=True)
        cjk_matcher = _web_cjk_matcher(cjk_un)
        global _WEB_JP_TAGGER, _WEB_JP_TAGGER_TRIED
        if not _WEB_JP_TAGGER_TRIED:
            _WEB_JP_TAGGER_TRIED = True
            try:
                from fugashi import Tagger
                _WEB_JP_TAGGER = Tagger()
            except Exception:
                _WEB_JP_TAGGER = None
        items = []
        for i, t in enumerate(texts):
            hit = set()
            occ = []
            en_words = list(re.finditer(r"[A-Za-z][A-Za-z'’\-]*", t))
            total_words = len(en_words)
            for m in en_words:
                surf = m.group(0)
                info = unmastered.get(surf.lower()) or unmastered.get(surf)
                if info:
                    lem = info.get("lemma") or surf.lower(); hit.add(lem)
                    occ.append({"start": m.start(), "end": m.end(), "surface": surf, "lemma": lem,
                                "label": info.get("label_slug") or "new", "mastery": info.get("mastery", 0)})
            if re.search(r"[぀-ヿ一-鿿]", t):
                # 总词数用与 PDF 相同的 fugashi；不可用时仅以 CJK 字符数/2 保守近似。
                cjk_runs = list(re.finditer(r"[぀-ヿ一-鿿]+", t))
                if _WEB_JP_TAGGER:
                    try:
                        total_words += sum(sum(1 for _ in _WEB_JP_TAGGER(r.group(0))) for r in cjk_runs)
                    except Exception:
                        total_words += sum(max(1, len(r.group(0)) // 2) for r in cjk_runs)
                else:
                    total_words += sum(max(1, len(r.group(0)) // 2) for r in cjk_runs)
                for start, end, w in cjk_matcher.find_nonoverlapping(t):
                    info = unmastered[w]
                    lem = info.get("lemma") or w; hit.add(lem)
                    occ.append({"start": start, "end": end, "surface": w, "lemma": lem,
                                "label": info.get("label_slug") or "new", "mastery": info.get("mastery", 0)})
            occ.sort(key=lambda x: (x["start"], x["end"]))
            hot = len(t) >= 12 and len(hit) >= thr and total_words >= min_words
            items.append({"i": i, "count": len(hit), "words": sorted(hit)[:12], "total_words": total_words,
                          "hot": hot, "occurrences": occ})
        marks = [{k: x[k] for k in ("i", "count", "words", "total_words")} for x in items if x["hot"]]
        return jsonify({"ok": True, "items": items, "marks": marks, "threshold": thr, "min_words": min_words})

    @bp.route("/api/web-cookie", methods=["GET", "POST"])
    def pdf_api_web_cookie():
        """Retired with the server-side proxy; saved cookie files stay untouched."""
        return _retired_web_response()

    @bp.route("/api/rbi-ticket", methods=["POST"])
    def pdf_api_rbi_ticket():
        """Retired: no new RBI browser session may be authorized."""
        return _retired_web_response()

    @bp.route("/web/rbi-live")
    def pdf_web_rbi_live():
        """Retired: RBI UI remains on disk only for offline recovery."""
        return _retired_web_response()

    @bp.route("/web/rbi")
    def pdf_web_rbi():
        """Retired: the server no longer launches a browser for third-party pages."""
        return _retired_web_response()

    @bp.route("/web/frame")
    def pdf_web_frame():
        """Retired: no iframe/proxy fallback is allowed."""
        return _retired_web_response()

    @bp.route("/web/res")
    def pdf_web_res():
        """Retired: do not serve old resource-cache entries through the proxy."""
        return _retired_web_response()

    def _serve_res(url: str):
        if not url or _url_safe(url):
            abort(403)
        cap = _current_proxy_cap(url)
        cap_details = verify_web_proxy_cap_details(_proxy_cap_secret(), cap)
        if not cap_details:
            abort(403)
        budget = _WEB_PROXY_BUDGETS.begin(cap, cap_details)
        if budget is None:
            return Response("proxy capability budget exhausted", status=429)
        hit = _rescache_get(url)      # 静态资源(hash 命名的 js/css/字体/图)命中即走,不占舱壁也不打上游
        if hit is not None:
            body, ct = hit
            if _rescache_ok(ct):
                if not budget.consume(len(body)):
                    return Response("proxy capability byte limit", status=429)
                return Response(body, status=200, headers={
                    "Content-Type": ct, "Cache-Control": "public, max-age=604800",
                    "Access-Control-Allow-Origin": "*"})
        if not _RES_GATE.acquire(timeout=6):
            return Response("proxy busy", status=503)
        ok = False
        try:
            resp = _serve_res_inner(url, cap, budget)
            ok = True
            return resp
        finally:
            # ⚠ 流式正文是在视图**返回之后**才被消费的 → 不能在这里直接 release,
            #   否则舱壁只罩住"连上游"那一小段,大文件下载照样吃满线程。
            #   走 _gated() 把 release 交给生成器的 finally;非流式路径(CSS/异常)才就地放。
            if not ok:
                _RES_GATE.release()

    def _gated(it, sess=None, budget=None):
        """把流量上限、释放信号量和会话关闭绑到流的生命周期。"""
        try:
            for chunk in it:
                if budget is not None and not budget.consume(len(chunk)):
                    break
                yield chunk
        finally:
            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass
            _RES_GATE.release()

    def _serve_res_inner(url: str, cap: str, budget):
        # Never forward the browser's internal /pdf/web/... Referer upstream:
        # it may contain the bearer capability in its query string.
        h = _px_headers(url)
        h["Accept"] = request.headers.get("Accept", "*/*")
        rng = request.headers.get("Range")
        if rng:
            h["Range"] = rng
        try:
            sess, r = _px_open(url, h, timeout=25)
        except Exception:
            abort(502)
        ct = (r.headers.get("Content-Type") or "application/octet-stream")
        keep = {"content-type", "content-length", "content-range", "accept-ranges",
                "cache-control", "etag", "last-modified", "expires", "content-encoding"}
        hd = {k: v for k, v in r.headers.items()
              if k.lower() in keep and k.lower() not in ("content-length", "content-encoding")}
        hd["Access-Control-Allow-Origin"] = "*"
        hd["Content-Type"] = ct
        _save_resp_cookies(str(getattr(r, "url", "") or url), r)
        try:
            declared_length = int(r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > MAX_RESOURCE_BYTES_PER_RESPONSE:
            try:
                sess.close()
            finally:
                _RES_GATE.release()
            return Response("proxy response too large", status=413)
        # 可缓存的小静态资源:iter_content 整读(curl_cffi stream 后不用 r.content)→ 落盘 → 就地返回
        if _rescache_ok(ct) and r.status_code == 200 and int(r.headers.get("Content-Length") or 0) <= _RESCACHE_MAX:
            try:
                raw = b""
                too_big = False
                for chunk in r.iter_content(65536):
                    raw += chunk
                    if len(raw) > _RESCACHE_MAX:
                        too_big = True
                        break
                if not too_big:
                    if "text/css" in ct.lower():
                        raw = _rewrite_css(raw.decode("utf-8", "replace"), r.url or url, cap).encode("utf-8")
                    if not budget.consume(len(raw)):
                        sess.close()
                        _RES_GATE.release()
                        return Response("proxy capability byte limit", status=429)
                    _rescache_put(url, raw, ct)
                    sess.close()
                    resp = Response(raw, status=200, headers=hd)
                    _RES_GATE.release()
                    return resp
                # 超出缓存上限:已读部分 + 续流,交给 _gated 收尾(release + close)
                def _cont(pre, it):
                    yield pre
                    for c in it:
                        yield c
                return Response(
                    _gated(_cont(raw, r.iter_content(65536)), sess, budget),
                    status=200,
                    headers=hd,
                )
            except Exception:
                pass
        # 大 CSS(未缓存):读全 → 改写 url()/@import
        if "text/css" in ct.lower():
            try:
                body = b""
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_RESOURCE_BYTES_PER_RESPONSE:
                        sess.close()
                        _RES_GATE.release()
                        return Response("proxy response too large", status=413)
                rewritten = _rewrite_css(
                    body.decode("utf-8", "replace"),
                    r.url or url,
                    cap,
                ).encode("utf-8")
                if not budget.consume(len(rewritten)):
                    sess.close()
                    _RES_GATE.release()
                    return Response("proxy capability byte limit", status=429)
                resp = Response(rewritten, status=r.status_code, headers=hd)
                sess.close()
                _RES_GATE.release()
                return resp
            except Exception:
                pass
        # 其它:流式透传,_gated 负责 close + release
        return Response(
            _gated(r.iter_content(65536), sess, budget),
            status=r.status_code,
            headers=hd,
        )

    @bp.route("/web/live")
    def pdf_web_live():
        """Short compatibility hand-off: open the original page in the browser."""
        target = _retired_web_redirect_target(request.args.get("url"))
        if not target:
            abort(400)
        response = redirect(target, code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @bp.route("/web")
    def pdf_web_portal():
        """已退役的 PWA 网页阅读器入口。

        原先转回书架,但书架现在也退役了(reader_pwa_retirement),那个转向
        会落在一个 410 上。直接说明,不让用户经过一次无意义的跳转。
        """
        import reader_pwa_retirement as _retired_pwa
        return _retired_pwa.retired_response()

    @bp.route("/api/web-fetch", methods=["POST"])
    def pdf_api_web_fetch():
        """Retired: the PWA never parses a third-party webpage into a book."""
        return _retired_web_response()

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
            with _html_hl_edit(rel) as items:
                items[:] = [h for h in items if h.get("id") != hid]
            return jsonify({"ok": True})
        body = request.get_json(silent=True) or {}
        rel = (body.get("file") or "").strip()
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
            with _html_hl_edit(rel) as items:
                items.append(h)
            return jsonify({"ok": True, "id": h["id"], "highlight": h})
        # PATCH
        hid = (body.get("id") or "").strip()
        with _html_hl_edit(rel) as items:
            h = next((x for x in items if x.get("id") == hid), None)
            if not h:
                return jsonify({"ok": False, "error": "未找到"}), 404
            if "color" in body:
                h["color"] = body.get("color") or h["color"]
            if "note" in body:
                h["note"] = (body.get("note") or "")[:2000]
        return jsonify({"ok": True, "highlight": h})
