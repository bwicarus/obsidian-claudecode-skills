"""EPUB 阅读器侧边栏助手 —— section 级 agentic agent + 对话历史(Phase H 后端)。

自包含 Flask 扩展:**不修改 assistant.py / pdf_reader.py**,只通过 register_epub_assistant(bp)
把 4 个路由挂到 pdf_reader 的蓝图上(/pdf/api/epub-assistant、/pdf/api/epub-convo[/append|/clear])。

设计理念(照搬 PDF 侧边栏助手 assistant.py 的可迁移架构,只换语境):
- **复用 assistant.py 的 AI / SSE / 工具循环骨架**(import 见 _A()):
  Claude 剥壳进程 `_spawn`/`_send_stream`/`_kill`、Gemini 流式 `_gemini_stream`/`_compact_gemini_contents`、
  顽强 JSON 工具解析 `_parse_tool`、按动作选后端 `_resolve`、额度护栏 `_quota_warning`、
  本轮 token 计数 `_tok_*`、文本清洗 `_clean_tag`、轨迹明细 `_step_detail`。
  **通用工具**(search_all_books/recall_notes/make_anki/make_note/add_vocab/lookup_word/translate/open_book/undo_last)
  直接复用 assistant.py 的实现(`_t_*` / `_bg_task`),不重写。
- 本文件只新增:**section 级工具表**(read_section/list_sections/goto_section/search_book/epub_highlight/read_highlights)、
  **电子书语境的系统提示**(去掉 PDF 的页/印刷页/see_page 那套,换成 reflow 章节 idx)、
  **独立的对话历史命名空间**(state/epub-convo/<uid>/<file-hash>.json,跟 PDF 助手分键不串)、
  **生成与请求解耦**(detached worker + rid 重连,切后台不丢)。

EPUB 是 reflow:**无页码、有 spine 章节 idx**;高亮是 DOM 偏移锚 {section,start,end}(前端算偏移、存 sidecar)。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from flask import Response, jsonify, request, session

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))


# ── 懒加载(避免 import 期循环依赖:pdf_reader 在自己 import 末尾 register 本模块)──
def _A():
    import assistant  # PDF 侧边栏助手:复用其 AI / SSE / 工具循环骨架 + 通用工具
    return assistant


def _pdf():
    import pdf_reader  # 复用 epub 解包/OPF/高亮 sidecar/路径校验
    return pdf_reader


def _logged_in() -> bool:
    return bool(session.get("user_id"))


def _uid() -> str:
    """跟 PDF 阅读器 _reader_uid / 助手 action-pref 同口径(user_id 优先,回落 username)。"""
    try:
        return session.get("user_id") or session.get("username") or ""
    except Exception:
        return ""


# ──────────────────────── 收藏集识别(只对收藏夹物化 EPUB 加分支,普通书零影响)────────────────────────
# 收藏夹物化成一本真 EPUB(state/reader-fav-epub/<fid>.epub,合成 rel「资源/收藏夹/<fid>.epub」);它是**收藏集**:
# 用户从不同书/位置精选的页面/章节拼成,条目间通常不连续、各有原书出处。AI 在收藏集里问答时上下文语义跟普通书不同
# (不能照搬前后 section 当连续),故加:识别 + system prompt 声明 + 目录概览 + 相邻性(adj)+ read_source_page 翻原书。
# 数据源 = build 时写的 state/reader-fav-meta/<fid>.json(_pdf()._fav_meta_load)。非收藏集一律走原逻辑不变。
def _fav_fid(file_rel: str):
    """收藏夹物化 EPUB 的 fid(资源/收藏夹/<fid>.epub → f_xxx);非收藏夹 → None。"""
    import re
    m = re.match(r"^资源/收藏夹/(f_[0-9a-zA-Z]+)\.epub$", (file_rel or "").lstrip("/"))
    return m.group(1) if m else None


def _fav_meta_for(ctx) -> dict:
    """当前 ctx 若是收藏集 → 返回其 AI 元数据 dict(含 items:每条目出处/首句/相邻/missing);否则 {}。"""
    fid = _fav_fid((ctx or {}).get("file_rel") or "")
    if not fid:
        return {}
    try:
        return _pdf()._fav_meta_load(fid) or {}
    except Exception:
        return {}


def _fav_meta_rec(meta: dict, section_idx: int):
    """meta 里 section idx == section_idx 的那条(=收藏集第 N 条收藏的出处记录);无 → None。"""
    for r in (meta.get("items") or []):
        if r.get("section") == section_idx:
            return r
    return None


# ──────────────────────── EPUB 取数辅助 ────────────────────────
def _eroot(file_rel: str):
    """(root_path, opf_info) 或 None。复用 pdf_reader 的解包 + OPF 解析(带缓存)。"""
    pdf = _pdf()
    ap = pdf._resolve_epub_book(file_rel or "")   # 收藏夹物化 EPUB(state 里那本)侧栏助手也能读章节
    if not ap or ap.suffix.lower() != ".epub":
        return None
    root = pdf._ensure_epub_extracted(ap, file_rel)
    if not root:
        return None
    try:
        return root, pdf._epub_opf_info(root)
    except Exception:
        return None


def _section_plain_text(info: dict, idx: int, cap: int = 6000):
    """某 spine 章节 → 纯文本(剥标签)。越界返回 None。"""
    secs = info.get("sections") or []
    if idx < 0 or idx >= len(secs):
        return None
    try:
        import re

        from bs4 import BeautifulSoup
        raw = secs[idx].read_text("utf-8", "ignore")
        soup = BeautifulSoup(raw, "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()
        body = soup.find("body") or soup
        txt = body.get_text("\n", strip=True)
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        return txt[:cap]
    except Exception:
        return None


def _section_label(info: dict, idx: int) -> str:
    for e in (info.get("toc") or []):
        if isinstance(e, dict) and e.get("idx") == idx:
            return e.get("label") or ""
    return ""


def _cur_idx(ctx, args=None, key="idx"):
    """取章节 idx:args 显式给的优先,否则当前章(ctx.current_section_idx),默认 0。"""
    if args and args.get(key) is not None:
        try:
            return int(args[key])
        except (TypeError, ValueError):
            pass
    try:
        return int(ctx.get("current_section_idx") or 0)
    except Exception:
        return 0


# ──────────────────────── 章 → spine section 区间(整章覆盖)────────────────────────
# EPUB 里「一章」常对应**多个 spine section**(费曼 第1章=idx12..15,§1-1~§1-4 各一 section;第2章 idx16 起)。
# 扁平的 info["toc"](label→idx)丢了「章含哪些小节」的层级 → auto_highlight 只拿到章首 idx 就只标第一节。
# 这里从 ncx/nav 的**嵌套层级**重建每章的 [起 section, 止 section] 区间(章 = 子节点全是叶子的 navPoint/li;
# 区间 = 自身 + 子节点 idx 的 [min,max])。带缓存(解析一次永久复用)。失败/无层级 → 返回 [](调用方回退单 section)。
_EPUB_CHAP_CACHE: dict = {}
_echap_lock = threading.Lock()


def _epub_chapter_spans_from_nav(root, idx_by_path):
    """epub3 nav(<ol>/<li> 嵌套)兜底:无 ncx 时用。返回 [(start,end,label), ...]。"""
    from bs4 import BeautifulSoup
    spans = []
    cand = []
    try:
        for pat in ("*nav*.xhtml", "*toc*.xhtml", "*nav*.html", "*toc*.html"):
            cand += list(root.rglob(pat))
    except Exception:
        cand = []
    for navp in cand[:4]:
        try:
            soup = BeautifulSoup(navp.read_text("utf-8", "ignore"), "html.parser")
            navel = soup.find("nav") or soup
            top_ol = navel.find("ol")
            if not top_ol:
                continue
            nav_dir = navp.parent

            def _li_idx(li):
                a = li.find("a")
                if a and a.get("href"):
                    href = a["href"].split("#")[0]
                    try:
                        return idx_by_path.get(str((nav_dir / href).resolve()))
                    except Exception:
                        return None
                return None

            def _walk_li(li):
                sub = li.find("ol", recursive=False)
                if not sub:
                    return
                kids = sub.find_all("li", recursive=False)
                if kids and all(not k.find("ol", recursive=False) for k in kids):
                    idxs = [_li_idx(li)] + [_li_idx(k) for k in kids]
                    idxs = [i for i in idxs if i is not None]
                    if idxs:
                        a = li.find("a")
                        spans.append((min(idxs), max(idxs),
                                      (a.get_text(" ", strip=True)[:80] if a else "")))
                else:
                    for k in kids:
                        _walk_li(k)

            for li in top_ol.find_all("li", recursive=False):
                _walk_li(li)
            if spans:
                break
        except Exception:
            continue
    return spans


def _epub_chapter_spans_build(root, info):
    from bs4 import BeautifulSoup
    secs = info.get("sections") or []
    if not secs:
        return []
    idx_by_path = {}
    for i, p in enumerate(secs):
        try:
            idx_by_path[str(Path(p).resolve())] = i
        except Exception:
            idx_by_path[str(p)] = i
    spans = []
    # ① epub2 ncx:navPoint 嵌套(章 = 子节点全是叶子小节的 navPoint)
    try:
        ncx = next(iter(sorted(root.rglob("*.ncx"))), None)
    except Exception:
        ncx = None
    if ncx:
        try:
            xs = BeautifulSoup(ncx.read_text("utf-8", "ignore"), "xml")
            nm = xs.find("navMap")
            ncx_dir = ncx.parent

            def _np_idx(np):
                src = np.find("content")
                if src and src.get("src"):
                    href = src["src"].split("#")[0]
                    try:
                        return idx_by_path.get(str((ncx_dir / href).resolve()))
                    except Exception:
                        return None
                return None

            def _walk(np):
                kids = np.find_all("navPoint", recursive=False)
                if kids and all(not k.find_all("navPoint", recursive=False) for k in kids):
                    idxs = [_np_idx(np)] + [_np_idx(k) for k in kids]
                    idxs = [i for i in idxs if i is not None]
                    if idxs:
                        lab = np.find("text")
                        spans.append((min(idxs), max(idxs),
                                      (lab.get_text(" ", strip=True)[:80] if lab else "")))
                else:
                    for k in kids:
                        _walk(k)

            if nm:
                for tp in nm.find_all("navPoint", recursive=False):
                    _walk(tp)
        except Exception:
            spans = []
    # ② epub3 nav 兜底
    if not spans:
        try:
            spans = _epub_chapter_spans_from_nav(root, idx_by_path)
        except Exception:
            spans = []
    spans.sort(key=lambda s: s[0])
    return spans


def _epub_chapter_spans(root, info):
    """[(start_idx, end_idx, label), ...](按 start 升序);带缓存。"""
    key = str(root)
    try:
        mt = int((root / ".extracted").stat().st_mtime)
    except Exception:
        mt = 0
    c = _EPUB_CHAP_CACHE.get(key)
    if c and c[0] == mt:
        return c[1]
    with _echap_lock:
        c = _EPUB_CHAP_CACHE.get(key)
        if c and c[0] == mt:
            return c[1]
        try:
            spans = _epub_chapter_spans_build(root, info)
        except Exception:
            spans = []
        _EPUB_CHAP_CACHE[key] = (mt, spans)
        return spans


def _chapter_span(root, info, idx):
    """含 idx 的整章 section 区间 (start,end);idx 不在任何章内(前言等)→ None。"""
    try:
        for (a, b, _label) in _epub_chapter_spans(root, info):
            if a <= idx <= b:
                return (a, b)
    except Exception:
        pass
    return None


# ──────────────────────── EPUB 专属工具 ────────────────────────
def _t_read_section(args, ctx):
    """读某章节正文(不传 idx=当前章)。顺带下一章开头预览(跨章内容不漏)。"""
    r = _eroot(ctx.get("file_rel") or "")
    if not r:
        return {"error": "当前不在 EPUB 电子书里 / 解包失败"}
    root, info = r
    total = len(info.get("sections") or [])
    idx = _cur_idx(ctx, args)
    txt = _section_plain_text(info, idx)
    if txt is None:
        return {"error": f"章节 idx={idx} 越界(本书共 {total} 章)"}
    if not txt.strip():
        return {"idx": idx, "total": total, "text": "(本章无文字内容,可能是封面/纯图章节)"}
    out = txt
    nxt = idx + 1
    if nxt < total:
        fid = _fav_fid(ctx.get("file_rel") or "")
        adj_ok = True
        if fid:   # 收藏集:下一条常是另一本无关书的页 → 只在「同书连续(adj_prev)」时才带预览(否则拼进来=污染上下文)
            nrec = _fav_meta_rec(_fav_meta_for(ctx), nxt)
            adj_ok = bool(nrec and nrec.get("adj_prev"))
        if adj_ok:
            nt = _section_plain_text(info, nxt, cap=800)
            if nt and nt.strip():
                out += f"\n\n【下一章·idx={nxt}(开头预览,要看全文再 read_section idx={nxt})】\n{nt}"
        elif fid:
            out += (f"\n\n(下一条收藏 idx={nxt} 来自**另一本书**、与本条不连续,已不带它的预览;"
                    f"要看**本条**在原书里的前后文,用 read_source_page item={idx})")
    return {"idx": idx, "total": total, "section_label": _section_label(info, idx), "text": out}


def _t_list_sections(args, ctx):
    """看本书结构:章节总数 + 目录(label→章节 idx)。把『第N章/某节』映射成 idx 用。"""
    r = _eroot(ctx.get("file_rel") or "")
    if not r:
        return {"error": "当前不在 EPUB 电子书里 / 解包失败"}
    root, info = r
    return {"total": len(info.get("sections") or []), "toc": info.get("toc") or [],
            "note": "toc 每项 {label, idx};idx=spine 章节序号(从0起)。再配合 goto_section(idx)/read_section(idx)。"}


def _t_goto_section(args, ctx):
    """翻到指定章节(前端动作)。"""
    try:
        idx = int(args.get("idx"))
    except (TypeError, ValueError):
        return {"error": "idx 不是数字"}
    return {"ok": True, "note": f"已跳到第 {idx} 个章节",
            "client_action": {"fn": "jumpTo", "args": [idx]}}


def _t_search_book(args, ctx):
    """在当前这本电子书全文搜关键词,返回命中章节 idx + 片段(服务端 grep 解包 XHTML,快)。"""
    q = (args.get("query") or "").strip()
    r = _eroot(ctx.get("file_rel") or "")
    if not q:
        return {"error": "缺 query"}
    if not r:
        return {"error": "当前不在 EPUB 电子书里 / 解包失败"}
    root, info = r
    import re
    tag_re = re.compile(r"<[^>]+>")
    ql = q.lower()
    hits = []
    for idx, fp in enumerate(info.get("sections") or []):
        if len(hits) >= 12:
            break
        try:
            raw = fp.read_text("utf-8", "ignore")
        except Exception:
            continue
        body = raw.split("<body", 1)[-1]
        text = re.sub(r"\s+", " ", re.sub(r"&[a-zA-Z#0-9]+;", " ", tag_re.sub(" ", body))).strip()
        low = text.lower()
        i = low.find(ql)
        if i < 0:
            continue
        a = max(0, i - 40); b = min(len(text), i + len(q) + 50)
        hits.append({"idx": idx, "label": _section_label(info, idx),
                     "excerpt": ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")})
    return {"total": len(hits), "hits": hits,
            "note": "hits 每项 {idx, excerpt};idx=章节序号,跳过去用 goto_section(idx)。"}


def _t_epub_highlight(args, ctx):
    """在 EPUB 上画高亮(前端动作)。agent 给**原文逐字**(texts,从 read_section 结果照抄,别改写/翻译)
    + 可选 section(章节 idx,不传=当前章);前端在该章渲染 DOM 里按文本定位→算偏移锚{section,start,end}→存 EPUB 高亮。
    (服务端拿不到渲染 DOM 偏移,故定位+落库交前端;这是 reflow 电子书最稳的高亮方式。)"""
    if not (ctx.get("file_rel") or ""):
        return {"error": "当前不在 EPUB 电子书里"}
    texts = args.get("texts")
    if not texts:
        t = args.get("text")
        texts = [t] if t else []
    texts = [x.strip() for x in texts if isinstance(x, str) and x.strip()][:15]
    if not texts:
        return {"error": "没给要高亮的原文(texts:[\"原句1\",\"原句2\"],逐字照抄正文)"}
    section = _cur_idx(ctx, args, key="section")
    color = args.get("color") or "#ffd54a"
    return {"ok": True, "requested": len(texts),
            "note": "已请求前端在该章定位并高亮这些原句。简短告诉用户标了哪些即可,别再逐章 read_section。",
            "client_action": {"fn": "epubHighlight", "args": [{"section": section, "texts": texts, "color": color}]}}


def _t_read_highlights(args, ctx):
    """读已有的 EPUB 高亮(标了哪些内容、颜色、备注)。不传 section=全书;section=数字=该章。
    答『我在这章/这本书高亮了什么』、批量标注前避免重复标都用它。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "当前不在 EPUB 电子书里"}
    try:
        hls = _pdf()._epub_hl_load(file_rel) or []
    except Exception as e:
        return {"error": str(e)[:120]}
    want = None
    sv = args.get("section")
    if sv is not None and str(sv).lower() != "all":
        try:
            want = int(sv)
        except (TypeError, ValueError):
            want = None
    out = []
    for h in hls:
        sec = (h.get("anchor") or {}).get("section")
        if want is not None and sec != want:
            continue
        out.append({"section": sec, "text": (h.get("text") or "")[:200],
                    "color": h.get("color"), "note": (h.get("note") or "")[:160]})
    return {"count": len(out), "highlights": out}


def _t_make_anki(args, ctx):
    """把内容做成 Anki 卡(后台,完成发通知)。复用 assistant 的后台任务框架,**不**写 PDF 高亮回链(epub 锚不同)。"""
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip()
    if not text:
        return {"error": "缺要做卡的内容(给 text 或先选中)"}
    params = {"text": text}
    img = (args.get("image_url") or "").strip()
    if img:
        params["image_url"] = img   # 刚 search_image 过、这张图也该进卡片 → 透传到 _run_snippets_to 真下载存进 Anki 媒体库
    return _A()._bg_task("anki", params, ctx)


def _t_make_note(args, ctx):
    """把内容整理成 Obsidian 笔记(后台)。复用 assistant 后台任务框架,不写 PDF 高亮回链。"""
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip()
    if not text:
        return {"error": "缺要整理的内容(给 text 或先选中)"}
    return _A()._bg_task("note", {"text": text}, ctx)


def _vocab_mastery_map() -> dict:
    """跨库『未掌握生词』查找表 {word_lower: {label, mastery, lemma}}(英→vocab_index;日→jp-vocab)。
    已掌握(mastered)的不收 —— 跟 /pdf/api/vocab-mastery-map(EPUB 前端下划线)同源同口径。"""
    pdf = _pdf()
    out = {}
    try:
        for w, info in (pdf._vocab_idx() or {}).items():
            slug = info.get("label_slug") or ""
            if not slug or slug == "mastered":
                continue
            out[w] = {"label": slug, "mastery": round(float(info.get("mastery", 0) or 0), 3),
                      "lemma": info.get("lemma") or w}
    except Exception:
        pass
    try:
        for w, e in (pdf._jp_vocab_load() or {}).items():
            if not pdf._jp_vocab_is_trackable(w):
                continue
            slug = pdf._jp_vocab_slug(e)
            if not slug:
                continue
            wl = w.lower()
            if wl not in out:
                out[wl] = {"label": slug, "mastery": round(pdf._jp_mastery(e), 3), "lemma": w}
    except Exception:
        pass
    return out


def _t_section_vocab(args, ctx):
    """查掌握度数据库(权威,别靠猜)= PDF page_vocab 的章节版:
    不传 words → 当前/指定章节里『还没掌握』的生词(=本章下划线词);传 words → 逐词查掌握度(英+日)。"""
    pdf = _pdf()
    words = args.get("words")
    if isinstance(words, list) and words:
        return {"lookups": pdf.vocab_mastery_for(words),
                "note": "mastered=true=已掌握;tracked=false=生词库里没有(=从没查过)。以此为准回答,别自己猜。"}
    r = _eroot(ctx.get("file_rel") or "")
    if not r:
        return {"error": "当前不在 EPUB 电子书里 / 解包失败"}
    root, info = r
    total = len(info.get("sections") or [])
    idx = _cur_idx(ctx, args)
    txt = _section_plain_text(info, idx, cap=20000)
    if txt is None:
        return {"error": f"章节 idx={idx} 越界(本书共 {total} 章)"}
    mp = _vocab_mastery_map()
    if not mp:
        return {"idx": idx, "count": 0, "unmastered_in_section": [],
                "note": "生词库为空或读取失败,没有可报的未掌握生词。"}
    import re
    # 英文:正文按词切→命中未掌握库的收。日文:遍历库里 CJK 词,本章正文里出现就收(无分词器时的稳妥兜底)。
    toks = set(t for t in re.findall(r"[A-Za-z][A-Za-z'\-]*", txt.lower()) if len(t) >= 2)
    seen = {}
    for t in toks:
        iw = mp.get(t)
        if iw:
            lem = iw.get("lemma") or t
            if lem not in seen:
                seen[lem] = {"word": t, "lemma": lem, "mastery": iw.get("mastery"), "level": iw.get("label")}
    for w, iw in mp.items():
        if w.isascii():
            continue
        if w in txt:
            lem = iw.get("lemma") or w
            if lem not in seen:
                seen[lem] = {"word": w, "lemma": lem, "mastery": iw.get("mastery"), "level": iw.get("label")}
    items = list(seen.values())[:80]
    return {"idx": idx, "section_label": _section_label(info, idx),
            "unmastered_in_section": items, "count": len(items),
            "note": "这是本章你**还没掌握**的生词(来自掌握度数据库)。不在此列表的词:要么已掌握、要么从没查过(系统不视为生词)。"
                    "回答『我没掌握哪些词/这章生词』就用这个列表,别拿正文里的词自己猜掌握与否。"}


def _t_auto_highlight(args, ctx):
    """【整章自动标重点·专家外包】= PDF auto_highlight 的章节版:主循环只下一个指令,本工具内部把目标章正文
    外包给挑句专家(leaf 子调用,只给它这章正文 + 『挑重点原句』任务)→ 选出原句 → 交前端在该章定位画 CFI 高亮 →
    **只把简报回主循环**(正文压根不进主编排上下文,整章高亮 token 从 O(正文) 降到 O(一句报告))。
    范围:不传=当前章;section=指定章;from+to(章节 idx 区间)/ sections=[..](逐章)。args {section?|from?,to?|sections?, color?}"""
    r = _eroot(ctx.get("file_rel") or "")
    if not r:
        return {"error": "当前不在 EPUB 电子书里 / 解包失败"}
    root, info = r
    total = len(info.get("sections") or [])
    secs = []
    rf, rt = args.get("from"), args.get("to")
    if rf is not None and rt is not None:
        try:
            a, b = int(rf), int(rt)
            if a > b:
                a, b = b, a
            secs = list(range(a, b + 1))
        except (TypeError, ValueError):
            pass
    elif isinstance(args.get("sections"), list):
        for p in args["sections"]:
            try:
                secs.append(int(p))
            except (TypeError, ValueError):
                pass
    elif args.get("section") is not None:
        try:
            secs = [int(args["section"])]
        except (TypeError, ValueError):
            pass
    else:
        secs = [_cur_idx(ctx, args)]
    secs = sorted(set(s for s in secs if 0 <= s < total))
    # 单 section 请求(section= / 当前章 / sections=[一个] / from==to)→ 展开成**整章**区间。
    # EPUB「一章」常=多 spine section(章首 idx 只是第一节);不展开就只标第一节(老 bug A)。
    # 显式多 section 的 from/to 或 sections=[多个] 视为用户给定范围,不再展开。
    if len(secs) == 1:
        span = _chapter_span(root, info, secs[0])
        if span:
            a, b = span
            secs = [s for s in range(a, b + 1) if 0 <= s < total]
    secs = secs[:24]   # 上限 24 个 section(够覆盖最长一章 ~8 节;也兜住误传的大 from/to)
    if not secs:
        return {"error": "没定位到要标的章节(给 section / from,to / sections;不确定章号先 list_sections)"}
    A = _A()
    rr = A._resolve("summarize", ctx.get("_uid"))   # 用「总结」那档当挑句专家(默认 Gemini flash,便宜)
    if A._paid_recover_check(ctx.get("_uid"), "summarize"):   # @paid 且免费恢复 → 摘除后重读(静默)
        rr = A._resolve("summarize", ctx.get("_uid"))
    color = args.get("color") or "#ffd54a"
    import re as _re
    reports, sec_payload, total_n = [], [], 0
    for idx in secs:
        txt = _section_plain_text(info, idx, cap=9000)
        if not txt or len(txt.strip()) < 12:
            reports.append({"idx": idx, "n": 0, "skip": "无文字/空章"})
            continue
        ck = A._ai_cache_key("pick_epub", rr["variant"], rr["depth"], A._ai_cache_key(txt))
        sents = None if (ctx or {}).get("_no_cache") else A._ai_cache_get(ck)   # 感叹号「更强重答」跳过缓存重挑
        if sents is None:
            prompt = ("下面是电子书一章里**一节**的正文。请挑出这一节 **3~8 句最重要**的(定义/核心结论/关键公式/易错点),"
                      "**逐字照抄原文**(不改写/不翻译/不合并/不跨段)。返回一个 JSON 数组 [\"原句1\",\"原句2\", ...];只输出 JSON,别加别的。\n\n"
                      + txt[:8000])
            out = A._deep_ask(prompt, backend=rr["backend"], variant=rr["variant"], depth=rr["depth"])
            parsed = []
            if out:
                mm = _re.search(r"\[.*\]", out, _re.S)
                if mm:
                    try:
                        parsed = json.loads(mm.group(0))
                    except Exception:
                        parsed = []
            sents = [str(s).strip() for s in parsed if isinstance(s, str) and str(s).strip()][:8] if isinstance(parsed, list) else []
            if sents:
                A._ai_cache_set(ck, sents)   # 挑句对同一章正文确定 → 下次重标 0 token
        sents = [s for s in (sents or [])][:8]
        if not sents:
            reports.append({"idx": idx, "n": 0, "skip": "没挑出重点"})
            continue
        # 服务端拿不到渲染 DOM 偏移 → 定位+画高亮交前端;**合并成一个 client_action 携带 per-section 列表**,
        # 前端 epubHighlight 串行处理(逐 section: display→定位→标→下一个),避免多 section 抢 R.display 竞态(老 bug A2)。
        sec_payload.append({"section": idx, "texts": sents})
        total_n += len(sents)
        reports.append({"idx": idx, "label": _section_label(info, idx), "n": len(sents),
                        "sentences": [s[:24] for s in sents]})
    if not sec_payload:
        return {"done": True, "total_requested": 0, "sections": reports,
                "note": "这一章没挑出可标的重点(可能以公式/图为主或正文太短)。"}
    return {"done": True, "total_requested": total_n, "sections": reports,
            # 一个 action 带整章各 section 的挑句;前端串行定位画高亮,完成后逐条渲「跳转/删除」列表(showHlPicker)
            "client_action": {"fn": "epubHighlight",
                              "args": [{"sections": sec_payload, "color": color, "picker": True}]},
            "note": f"已挑出共 {total_n} 句重点,前端在各章串行定位画高亮(分布见 sections)。**别再逐章 read_section**;"
                    "把『标了哪些章、共多少句』简洁告诉用户即可。完成后会逐条列出可跳转/删除。"}


def _t_find_highlights(args, ctx):
    """用户想**删除/取消/清理**某些高亮时用 = PDF find_highlights 的章节版:把匹配的高亮逐条列给用户去删,
    **不替用户删**。不传 section=全书;section=数字=该章;section=\"all\"=全书。
    (EPUB 删高亮的入口在前端:跳到该章后**点那条高亮**会弹含『删除』的编辑框,或开侧栏「高亮」面板逐条删。)"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "当前不在 EPUB 电子书里"}
    try:
        hls = _pdf()._epub_hl_load(file_rel) or []
    except Exception as e:
        return {"error": str(e)[:120]}
    want = None
    sv = args.get("section")
    if sv is not None and str(sv).lower() != "all":
        try:
            want = int(sv)
        except (TypeError, ValueError):
            want = None
    items = []
    for h in hls:
        if not h.get("id"):
            continue
        sec = (h.get("anchor") or {}).get("section")
        # section 已知且不等 → 排除;section 未知(纯 CFI 手动高亮)→ 保留(后端没法从 CFI 反推章号,留给前端 cfi 跳转)
        if want is not None and sec is not None and sec != want:
            continue
        items.append({"id": h.get("id"), "section": sec, "cfi": h.get("cfi") or "",
                      "text": (h.get("text") or "")[:120], "color": h.get("color")})
    if not items:
        return {"count": 0, "note": "这个范围没有高亮,没什么可删的。"}
    return {"count": len(items),
            # 逐条渲进对话:色块 + 原文 + 「↗跳转」+「🗑删除」(跟 PDF find_highlights 的 _showHlPicker 同形)
            "client_action": {"fn": "showHlPicker", "args": [{"items": items}]},
            "note": (f"已在对话里逐条列出 {len(items)} 处高亮,每条都带「跳转」+「删除」按钮,用户自己点删即可。"
                     "你**别再说没有删除工具**、也**别替用户删**;只需简短说一句『下面是这些高亮,可逐个跳转或删除』。")}


def _t_summarize_section(args, ctx):
    """取整章正文交给『总结档』模型(默认 Gemini Flash·think,省 Claude 额度 + 同章命中缓存 0 token)做**深度结构化总结**,
    返回现成总结。『总结这一章/这一节』用它(read_section 给原文要你自己总结;这个直接给好总结)。args {idx?}"""
    r = _eroot(ctx.get("file_rel") or "")
    if not r:
        return {"error": "当前不在 EPUB 电子书里 / 解包失败"}
    root, info = r
    total = len(info.get("sections") or [])
    idx = _cur_idx(ctx, args)
    txt = _section_plain_text(info, idx, cap=9000)
    if txt is None:
        return {"error": f"章节 idx={idx} 越界(本书共 {total} 章)"}
    if not txt.strip():
        return {"error": "本章无文字内容(可能封面/纯图章节),没法总结"}
    A = _A()
    title = _section_label(info, idx)
    rr = A._resolve("summarize", ctx.get("_uid"))
    if A._paid_recover_check(ctx.get("_uid"), "summarize"):   # @paid 且免费恢复 → 摘除后重读(静默)
        rr = A._resolve("summarize", ctx.get("_uid"))
    ck = A._ai_cache_key("summ_epub", rr["backend"], rr["variant"], rr["depth"], A._ai_cache_key(txt))
    gen = None if (ctx or {}).get("_no_cache") else A._ai_cache_get(ck)   # 感叹号「更强重答」跳过缓存重做
    if not gen:
        # 来源标注:有目录章名 → 照抄章名(人类语义,前端按 label 换算跳转);没有 → 「(第idx章)」(spine idx 语义,
        # 前端 _chapTarget 兜底解析 + 显示层替换)。别再教它写「第N章」泛式——N 会被模型当人类章号,跟 idx 语义打架。
        cite = f"「({title})」(照抄这个章名)" if title else f"「(第{idx}章)」"
        gen = A._deep_ask(
            f"下面是《{ctx.get('book_name', '')}》{('「' + title + '」') if title else ''}(section idx={idx})的正文。"
            "请用中文给出**结构化总结**:① 核心要点(分条)② 关键定义 ③ 重要公式(用 $...$)④ 易错点。"
            f"引用具体内容时句末标来源章 {cite}。简洁但完整,别遗漏主线。\n\n正文:\n" + txt,
            backend=rr["backend"], variant=rr["variant"], depth=rr["depth"])
        if gen and len(gen.strip()) > 80:   # 质量闸:太短(疑似截断/出错)不入缓存
            A._ai_cache_set(ck, gen.strip())
    if gen and gen.strip():
        return {"idx": idx, "section_label": title, "summary": gen.strip(),
                "_gen_model": f"{A._variant_short(rr['variant'])}·{rr['depth']}", "_gen_action": "summarize",
                "note": "这是深度总结好的章节内容,**直接原样转达给用户**(可微调排版,别再大改/精简),保留来源章标注。给最终回答按规则附追问。"}
    # 总结档失败 → 退回把原文交给编排器自己总结
    return {"idx": idx, "section_label": title, "text": txt,
            "note": "(深度总结暂不可用)这是本章正文,请你总结成:核心要点/关键定义/重要公式/易错点,标来源章。"}


def _fig_src_to_path(src: str):
    """把前端带入图的 src(/pdf/epub/file/<sha>/<subpath>,可能含 host)解析成解包目录里的真实文件路径。
    防越界(resolve 后必须仍在 <sha> 目录内)。返回 Path 或 None。"""
    if not src or not isinstance(src, str):
        return None
    s = src.strip()
    # 去掉 host / query / hash
    try:
        if "://" in s:
            from urllib.parse import urlsplit
            s = urlsplit(s).path
    except Exception:
        pass
    s = s.split("?")[0].split("#")[0]
    i = s.find("/epub/file/")
    if i < 0:
        return None
    rest = s[i + len("/epub/file/"):]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        return None
    sha, subpath = parts[0], parts[1]
    if not sha.isalnum():
        return None
    pdf = _pdf()
    try:
        from urllib.parse import unquote
        subpath = unquote(subpath)
    except Exception:
        pass
    root = (pdf._EPUB_EXTRACT_DIR / sha).resolve()
    target = (root / subpath).resolve()
    try:
        target.relative_to(root)   # 防 path 越界
    except ValueError:
        return None
    return target if target.is_file() else None


def _t_see_figure(args, ctx):
    """看用户**带入的图**(EPUB 章节里的插图;前端 __figAttached 随请求带 src)。已给的图文字说明不够、
    要核对图像里的具体细节时用。读取图片文件 → 一次性喂给视觉模型出文字描述(主循环不背图,省 token)。
    多张时 args{index} 指定第几张(从1起),不传=全部(≤3)。返回 _vision 描述。"""
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    figs = [f for f in figs if isinstance(f, dict) and (f.get("src") or f.get("url") or f.get("href") or f.get("b64")
                                                        or (f.get("kind") == "note" and f.get("note_id")))]
    if not figs:
        return {"error": "当前没有带入的图(让用户先在书里点一张图/图描述徽标)"}
    idx = args.get("index") if isinstance(args, dict) else None
    if idx:
        try:
            figs = [figs[int(idx) - 1]]
        except Exception:
            pass
    figs = figs[:3]
    try:
        import base64
        import mimetypes
        A = _A()
        vis = []
        ink_any = False
        for fg in figs:
            if fg.get("kind") == "note" and fg.get("note_id"):   # 双击带入的手写便签:按 note_id 现场重合成(文字+笔画整体一张图,永远最新 sidecar)
                try:
                    png_n = _pdf()._note_composite_png(fg.get("file_rel") or (ctx.get("file_rel") or ""), fg.get("note_id"))
                except Exception:
                    png_n = None
                if png_n:
                    vis.append({"media_type": "image/png", "b64": base64.b64encode(png_n).decode()})
                continue
            b64 = fg.get("b64")
            mt = fg.get("media_type") or fg.get("mime")
            if b64:   # 前端直接带 base64(data: URI 也走这)
                if isinstance(b64, str) and b64.startswith("data:"):
                    try:
                        head, b64 = b64.split(",", 1)
                        mt = head.split(";")[0][5:] or mt
                    except Exception:
                        continue
                vis.append({"media_type": mt or "image/png", "b64": b64})
                continue
            _ref = fg.get("ref") or {}   # 统一 opaque ref 优先(设计 §8);旧字段 imgbox/src 兜底
            imgbox = _ref.get("imgbox") or fg.get("imgbox"); ink = fg.get("ink")
            if imgbox and ink:   # 图上有用户手写墨迹(红圈等)→ 服务端把墨迹叠到该图上合成(照搬 PDF _figure_crop_png with_ink)
                src2 = _ref.get("src") or fg.get("src") or fg.get("url") or fg.get("href") or ""
                target2 = _fig_src_to_path(src2)
                if target2:
                    try:
                        raw2 = _pdf().resolve_figure_image({"kind": "epub", "path": target2, "imgbox": imgbox, "imgsw": _ref.get("imgsw") or fg.get("imgsw")}, ink)   # 统一中间层入口(设计 §8 步骤4);behavior 等价于原 _epub_figure_ink_png
                        vis.append({"media_type": "image/png", "b64": base64.b64encode(raw2).decode()})
                        ink_any = True
                        continue
                    except Exception:
                        pass   # 合成失败 → 落到下面读原图字节(至少还能看图,只是没墨迹)
            src = fg.get("src") or fg.get("url") or fg.get("href") or ""
            target = _fig_src_to_path(src)
            if not target:
                continue
            raw = target.read_bytes()
            if len(raw) > 4_500_000:   # 超大图先用 PIL 降一档,防喂回过大 / Pi 8GB OOM
                try:
                    import io
                    from PIL import Image
                    im = Image.open(io.BytesIO(raw)); im.thumbnail((1600, 1600))
                    buf = io.BytesIO(); im.convert("RGB").save(buf, format="JPEG", quality=82)
                    raw = buf.getvalue(); mt = "image/jpeg"
                except Exception:
                    pass
            if not mt:
                mt = mimetypes.guess_type(str(target))[0] or "image/png"
            vis.append({"media_type": mt, "b64": base64.b64encode(raw).decode()})
        if not vis:
            return {"error": "图取不到(src 无效或不是解包目录里的文件)"}
        note = "下面是用户在电子书里带入的插图,描述图里的内容(图表/示意图/曲线/公式排版等文字读不到的视觉信息)。"
        if ink_any:
            note += "（图上叠加了用户的**手写笔迹/圈点**(红圈/下划线/箭头等)——重点认清并回答他圈/画/标注的是哪一部分、那部分是什么。）"
        if any(f.get("kind") == "note" for f in figs):
            note += "（其中含用户的**手写便签**合成图:便签文字+手写笔画整体一张图,认清他写了/画了什么）"
        return {"图像描述": A._vision_for(ctx, vis, note) or "(看图失败,可重试)"}
    except Exception as e:
        return {"error": str(e)[:140]}


def _t_read_source_page(args, ctx):
    """【收藏集专用】翻回**原书**读任意页/节拿更多上下文(收藏集里某条收藏信息不够、要看它在原书的前后文/查证时用)。
    定位两种:① item=收藏条目的 section idx(读它对应的**原书**页/节;offset 相对偏移读前后文,+1 下一页/节、-1 上一页/节);
    ② 直接 book(书名/rel)+ page(PDF)或 section(EPUB)。返回原书那页/节正文(不是收藏集里那一条)。"""
    fid = _fav_fid((ctx or {}).get("file_rel") or "")
    if not fid:
        return {"error": "read_source_page 只在收藏集里可用(当前不是收藏集,直接读本书用 read_section)"}
    meta = _fav_meta_for(ctx)
    try:
        off = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        off = 0
    src_file = (args.get("book") or args.get("file") or "").strip()
    src_kind = None
    base_page = None
    base_section = None
    src_name = ""
    # ① item / index = 收藏条目 section idx → 解析其原书出处
    ii = args.get("item")
    if ii is None:
        ii = args.get("index")
    if not src_file and ii is not None:
        try:
            ii = int(ii)
        except (TypeError, ValueError):
            return {"error": "item 不是数字(应为收藏条目的 section idx;不确定先 list_sections)"}
        rec = _fav_meta_rec(meta, ii)
        if not rec:
            return {"error": f"收藏集里没有第 {ii} 条(用 list_sections 看目录)"}
        if rec.get("kind") == "userpage":
            return {"error": f"第 {ii} 条是你自己创建的插入页,没有『原书』可翻(用 read_section 读它本身)"}
        src_file = rec.get("src_file") or ""
        src_kind = rec.get("kind")
        base_page = rec.get("src_page")
        base_section = rec.get("src_section")
        src_name = rec.get("src_name") or ""
        if rec.get("missing"):
            return {"error": f"第 {ii} 条的原书《{src_name}》已移动/删除,翻不了"}
    if not src_file:
        return {"error": "缺定位:给 item(收藏条目 section idx)或 book+page/section"}
    if src_file.startswith("资源/收藏夹/"):
        return {"error": "不能把收藏集自己当原书翻(那会递归)"}
    low = src_file.lower()
    if src_kind is None:
        src_kind = "pdf" if low.endswith(".pdf") else ("epub" if low.endswith(".epub") else None)
    if not src_name:
        src_name = src_file.split("/")[-1]
    # PDF 原书 → _page_text(assistant.py,吃任意 vault rel + 1-based 页)
    if src_kind == "pdf":
        pv = args.get("page")
        try:
            page = int(pv) if pv is not None else int(base_page or 1)
        except (TypeError, ValueError):
            page = int(base_page or 1)
        page = max(1, page + off)
        txt = _A()._page_text(src_file, page)
        if not txt or not txt.strip():
            return {"error": f"《{src_name}》第 {page} 页读不到文字(越界/纯图页/原书不可用)"}
        return {"book": src_name, "file": src_file, "page": page, "text": txt[:5000],
                "note": "这是**原书**该页正文(收藏集这条收藏的出处)。据它补前后文/查证;引用标来源(《书名》第N页)。"}
    # EPUB 原书 → 复用本模块 _eroot/_section_plain_text 读原书某节
    if src_kind == "epub":
        r = _eroot(src_file)
        if not r:
            return {"error": f"《{src_name}》原书打不开(已删/移动/解包失败)"}
        root, info = r
        total = len(info.get("sections") or [])
        sv = args.get("section")
        try:
            sec = int(sv) if sv is not None else int(base_section or 0)
        except (TypeError, ValueError):
            sec = int(base_section or 0)
        sec = max(0, min(sec + off, total - 1))
        txt = _section_plain_text(info, sec, cap=6000)
        if txt is None:
            return {"error": f"《{src_name}》第 {sec} 节越界(原书共 {total} 节)"}
        return {"book": src_name, "file": src_file, "section": sec, "total": total,
                "section_label": _section_label(info, sec), "text": txt or "(本节无文字)",
                "note": "这是**原书**该节正文(收藏集这条收藏的出处)。据它补前后文/查证;引用标来源(《书名》该节)。"}
    return {"error": f"无法识别《{src_name}》的原书类型(既不是 PDF 也不是 EPUB)"}


# 工具表:{name: (描述, fn)}。fn(args, ctx) -> dict。section 级专属 + 直接复用 assistant 的通用工具。
_etools = {
    "read_section": ("读某章节正文(不传 idx=当前章,顺带给下一章开头预览)。要读/总结某章内容时用。args {idx?}", _t_read_section),
    "summarize_section": ("取当前(或指定)章节正文做**深度结构化总结**(read_section 给原文要你自己总结;『总结这一章/这一节』用它,省额度+同章缓存)。args {idx?}", _t_summarize_section),
    "list_sections": ("看本书结构:章节总数 + 目录(label→章节 idx)。把『第N章/某节/前言』映射成 idx 用。args {}", _t_list_sections),
    "goto_section": ("翻到指定章节(前端跳转)。args {idx}", _t_goto_section),
    "search_book": ("在当前这本电子书全文搜关键词,返回命中章节 idx + 片段。args {query}", _t_search_book),
    "epub_highlight": ("在 EPUB 上把**你已经选定的**重点原句画高亮(可撤销;前端按文本定位)。"
                       "texts 必须是章节里的**原文逐字**(从 read_section 结果照抄,别改写/翻译),否则定位不到。"
                       "**适合标当前章你已读到的几句**;要标整章/多章重点用 auto_highlight(别自己逐章 read+highlight)。"
                       "args {texts:[\"原句1\",\"原句2\"], section?, color?}(section 不传=当前章)", _t_epub_highlight),
    "auto_highlight": ("**整章/多章『自动标重点』专用**(『把这一章/第X章的重点都高亮』就用它)。它内部把章正文外包给挑句专家、"
                       "挑好原句交前端**串行**画 CFI 高亮,只回简报——**正文不进你的上下文,省大量 token**。"
                       "★EPUB 里『一章』通常跨**多个 spine section**(每个小节 §一个 section);传 section=该章在目录里的 idx 即可,"
                       "本工具会**自动展开成整章所有 section**(不用你算区间、也别只传第一个小节);也可显式 from+to(section idx 区间)/ sections=[..]。"
                       "不传=当前章。调它一次就够,**别再自己逐章 read_section+epub_highlight**。color 可选(默认黄)。"
                       "args {section?|from?,to?|sections?, color?}", _t_auto_highlight),
    "read_highlights": ("读已有的 EPUB 高亮(标了哪些内容/颜色/备注)。批量标注前先看可避免重复标;也答『这章/这本书我高亮了啥』。"
                        "不传 section=全书,section=数字=该章。args {section?}", _t_read_highlights),
    "find_highlights": ("用户要**删除/取消/清理/去掉**某些高亮时用:把匹配的高亮**逐条列在对话里**,每条带「↗跳转」+「🗑删除」按钮,"
                        "用户自己点(**别替删**)。**这就是删除高亮的入口——别说没有**。不传=全书;section=数字=某章;section=\"all\"=全书。args {section?}", _t_find_highlights),
    "section_vocab": ("查掌握度数据库:不传 words=当前/指定章『还没掌握』的生词(权威,跟本章下划线一致);"
                      "传 words(数组)=逐词查掌握度(英+日)。问『我没掌握哪些词/这章生词/某词我会不会』时用,别自己猜。args {idx?, words?:[...]}", _t_section_vocab),
    "see_figure": ("看用户**带入的那张图**(他点选/拖进来的章节插图;前端随请求带 src)。"
                   "已给的图文字说明不够、要核对图里的具体细节(图表/曲线/示意图/公式排版)时用。"
                   "**图上若有用户的手写笔迹/圈点(红圈/下划线/箭头),会自动叠进合成图给你看**——"
                   "用户问『这个/这一列/这一行/我圈的/我标的/这是什么/看一下』时,优先 see_figure 看合成图,别只凭文字描述猜。"
                   "也可传 note_id(notes_query/notes_read 拿到的便签 id)看**某条便签**的文字+手写合成图。"
                   "args {index?:第几张,从1起;不传=全部;note_id?}", _t_see_figure),
    # ── 便签四工具(实现共用 assistant.py 的 _t_notes_*,kind='epub':位置按 section idx 口径)──
    "notes_query": ("查用户贴在书页上的**便签**(sticky notes)列表。用户问『我记了什么便签/哪章有便签/我便签里写没写过X/找我那张黄色便签』时用;"
                    "回答『这章讲什么/总结』**不需要**查便签。args {color?, keyword?, section?} 三个过滤可组合,全不传=列全部"
                    "(color 可给色名 白/黄/蓝/绿/粉/石墨/墨绿 或 hex;section=章节 idx)",
                    lambda a, c: _A()._t_notes_query(a, c, kind="epub")),
    "notes_read": ("读某条便签的**全文**+位置(notes_query 的 text 只是摘要)。args {id}(id 从 notes_query 拿)",
                   lambda a, c: _A()._t_notes_read(a, c, kind="epub")),
    "notes_create": ("在书页上**新建一张便签**(有副作用的写操作,只有用户明确要求才调,如『帮我在这章记个便签/贴张便签写上…』)。"
                     "args {text, section?, x?, y?, color?}:text=便签内容(必填);section=章节 idx(不传=当前章);"
                     "x/y=章内位置比例 0~1(不传默认右上区);color 可给色名(白/黄/蓝/绿/粉/石墨/墨绿)或 hex,缺省白。"
                     "建完系统会自动给撤销卡,你不用解释怎么撤销",
                     lambda a, c: _A()._t_notes_create(a, c, kind="epub")),
    "notes_edit": ("**修改已有便签**的文字/颜色(写操作,只有用户明确要求才调,如『把那张便签改成…/便签换个颜色』)。"
                   "args {id, text?, color?}(id 从 notes_query 拿;text/color 至少给一个)。"
                   "**只能改文字和颜色**——手写笔画/位置/尺寸动不了(工具层面就不接收),别答应用户改这些",
                   lambda a, c: _A()._t_notes_edit(a, c, kind="epub")),
    # ── 以下直接复用 assistant.py 的实现(跨书/召回/写操作/查词/翻译,跟 PDF 助手一致)──
    "search_all_books": ("跨『我所有的书』全文搜索(用户问『哪本书讲过X/别的书有没有X/之前在哪见过』时用)。args {query}",
                         lambda a, c: _A()._t_search_all_books(a, c)),
    "recall_notes": ("**召回用户自己学过/记过的**相关内容(知识索引+笔记+图谱已学节点+Anki 卡,本地查不耗时)。"
                     "想把当前内容跟他已学的串起来、或问『我之前记过吗/笔记里有没有X』时用。"
                     "**只有召回到的才算他学过**,没召回到别假设。args {query}(不传用选中)",
                     lambda a, c: _A()._t_recall_notes(a, c)),
    "make_anki": ("把内容做成 Anki 卡(后台,完成通知)。args {text?, image_url?}"
                 "(不传 text 用选中;image_url 若刚 search_image 过、这张图也该进卡片,把同一个 image_url 传进来——"
                 "会真下载存进 Anki 媒体库、只贴进本次生成的第一张卡,不是外链)", _t_make_anki),
    "make_note": ("把内容整理成 Obsidian 笔记(后台)。args {text?}(不传用选中)", _t_make_note),
    "add_vocab": ("把单词加生词本并制卡(后台)。args {word?}(不传用选中)", lambda a, c: _A()._t_add_vocab(a, c)),
    "search_image": ("★配图专用(Wikipedia 免费搜真实图片,非 AI 生成)。**只在概念具体/生僻、视觉信息真有帮助时才调**"
                     "(如某种矿物/历史文物/生物物种/机械结构/天体/建筑/仪器等有明确实物形象的东西);"
                     "**别对**『力/能量/速度』这类基础常见词、**也别对**抽象理论/数学推导配图——大多数回答根本不需要图,别每个词都调。"
                     "拿到图后在回答里用标准 markdown ![简短说明](image_url) 插入;没搜到就 ok:false,**别自己编图片链接**。"
                     "刚好这次还要制卡、这张图也想放进卡片,就把 image_url 一并传给 make_anki。args {query:要搜的词/概念}",
                     lambda a, c: _A()._t_search_image(a, c)),
    "lookup_word": ("查词典:英→ECDICT(音标+中文释义+原形)、日→unidic **权威读音+声调**。"
                    "**读音/释义以它为准,别自己编**;你只结合上下文挑义项+讲解。args {word?}(不传用选中)",
                    lambda a, c: _A()._t_lookup_word(a, c)),
    "translate": ("翻译文字成中文(或 target 语言)。不传 text 则译选中。args {text?, target?}",
                  lambda a, c: _A()._t_translate(a, c)),
    "open_book": ("打开另一本书(跨书跳转)。args {file_rel | book(书名), page?}", lambda a, c: _A()._t_open_book(a, c)),
    "undo_last": ("撤销最近一次写操作(删掉刚建的卡/笔记/生词)。用户说『撤销/取消刚才那个』时用。args {}",
                  lambda a, c: _A()._t_undo_last(a, c)),
}

_ELABELS = {
    "read_section": "读取章节", "summarize_section": "总结本章", "list_sections": "看目录",
    "goto_section": "翻到章节", "search_book": "搜索全书", "epub_highlight": "高亮",
    "auto_highlight": "自动标重点", "read_highlights": "看高亮", "find_highlights": "列出可删高亮",
    "section_vocab": "查掌握度", "see_figure": "看这张图",
    "notes_query": "查便签", "notes_read": "读便签", "notes_create": "新建便签", "notes_edit": "修改便签",
    "search_all_books": "跨书搜索", "recall_notes": "召回我的笔记", "make_anki": "制卡",
    "make_note": "整理笔记", "add_vocab": "加生词本", "lookup_word": "查词典",
    "translate": "翻译", "open_book": "打开书", "undo_last": "撤销", "search_image": "配图搜索",
    "read_source_page": "翻原书",
}


def _elabel(name):
    return _ELABELS.get(name, name)


# ── 收藏集专用工具(只在收藏夹物化 EPUB 里对 AI 暴露 + 可执行;普通书的工具集不含它 = 零影响)──
# read_source_page 不进 _etools(那样静态工具目录会对普通书也列出它);只进这里,由 _tool_fn 在**收藏集**时才并入
# 执行注册表,并在 _esys_prompt 的收藏集动态块里单独广告。普通书:模型看不到、注册表里也没有 → 行为完全不变。
_efav_tools = {
    "read_source_page": ("【收藏集专用】翻回**原书**读任意页/节拿更多上下文(某条收藏信息不够、要看它在原书的前后文/查证时用)。"
                         "args {item:收藏条目的 section idx(读它对应的原书页/节),或 book+page/section 直接指定;"
                         "offset:相对偏移(+1 读下一页/节、-1 读上一页/节)}", _t_read_source_page),
}


def _tool_fn(name, ctx):
    """按名取工具执行函数:通用/section 级工具人人可用;收藏集专用工具仅当**当前是收藏集**时并入。普通书零影响。"""
    ent = _etools.get(name)
    if ent:
        return ent[1]
    if name in _efav_tools and _fav_fid((ctx or {}).get("file_rel") or ""):
        return _efav_tools[name][1]
    return None


# ──────────────────────── 系统提示(电子书/章节语境)────────────────────────
# 静态规则(恒定)。动态【当前章节】块在 _esys_prompt 末尾拼。按唯一锚 "【当前章节】" 切静态/动态。
_ESYS_RULES = (
    "你是网页**电子书(EPUB)阅读器**的侧边栏助手,像 Copilot 一样陪用户读书。用简洁中文口语聊天。\n"
    "这是 **reflow 电子书**:**没有页码**,内容按 **spine 章节(section idx,从0起)** 组织;你看到/说的位置一律用**章节 idx**。\n"
    "你能调用下面的工具来读章节、看目录、搜索、翻译、查词、查掌握度、制卡、整理笔记、跳章、画/删高亮、总结整章等,"
    "可以连续调用多个工具来完成复合请求(例如『总结这章再做成卡』= 先 read_section,再据此回答,再 make_anki)。\n"
    "★【工具一定可用·别幻觉】这些工具**随时都能调用**,书也开着(上面给了当前书/章)。"
    "**严禁**回复『我读不到内容/工具暂时没法调用/无法访问书本/没法确认这章』之类——那是错的,你只是**还没去调**。"
    "要章节正文就 read_section、要整章总结就 summarize_section、要看目录就 list_sections、要搜就 search_book、要画/删高亮就 epub_highlight/find_highlights"
    "——**先调工具拿到真实内容再回答**,别凭空说做不到、别让用户自己把内容贴给你。"
    "只有当某次工具**真的返回了 error 字段**时,才如实把那个具体错误告诉用户(并说你试了什么),不许笼统甩锅『工具不可用』。\n"
    "★【写操作守卫·最高优先级】make_anki(制卡)/ make_note(整理笔记)/ add_vocab(加生词)/ notes_create·notes_edit(建/改便签)是**有副作用**的写操作,"
    "**只有用户在这条消息里明确要求**才能调:出现『做成卡/制卡/做张卡/加到 Anki』才 make_anki;『整理成笔记/记成笔记/存成笔记』才 make_note;『加生词/加到生词本/收藏这个词』才 add_vocab;"
    "『帮我记个便签/在这章贴张便签/把便签改成…』才 notes_create/notes_edit(问『我记了什么便签』只是**读**,用 notes_query/notes_read,不算写)。"
    "用户只说『总结/讲解/读一下/这章讲了啥/翻译/解释』——这些都只要**文字回答**,**绝不许**顺手 make_anki / make_note / add_vocab / notes_create。"
    "拿不准用户到底要不要卡时:先给文字总结,再在回答里问一句『要我做成 Anki 卡吗?』,**别擅自写**。\n"
    "★配图:讲到具体/生僻、视觉信息真有帮助的概念时(某种矿物/历史文物/生物物种/机械结构/天体/建筑/仪器等有明确实物形象的东西),"
    "可用 search_image(Wikipedia 真实图片,免费无 key,非 AI 生成)拿到图后在回答里用 ![简短说明](image_url) 插入;"
    "**别对每个词都调**,基础常见词(力/能量/速度等)和抽象理论/数学推导**不需要**配图,大多数回答根本用不上这个工具。\n"
    "调用工具时:**整条消息只输出一行 JSON**,格式 {\"tool\":\"工具名\",\"args\":{...}},别加任何别的字。"
    "我执行后会把【工具结果】返回给你,你再决定继续调工具还是回答。\n"
    "能回答用户时:直接输出给用户看的中文回答(纯文本,不要 JSON、不要工具)。回答简洁自然,别太长。\n"
    "★数学一律用 LaTeX 写进 $...$(行内)或 $$...$$(独立成行):如 $x^2$、$\\frac{a}{b}$、$\\lambda$。"
    "**严禁**用反引号包数学(`x^2` 会被当代码块、公式不渲染)、**严禁**用纯文本或 Unicode 上下标(要写 $x^2$ 不是 x²、$a_i$ 不是 aᵢ);"
    "凡变量、希腊字母、下标、分式、根号、求和/积分一律进 $...$。\n"
    "★【章节范围·跨章】read_section(不带 idx,读当前章)会顺带给**下一章开头预览**——内容常跨章,默认据这些回答。"
    "本章+下章预览仍不足以答全时,继续 read_section(idx=再下一章)往下读,够答即止,别无限翻。\n"
    "★复合请求(含多个动作,如『总结再做成卡』『翻译并制卡』『找到X并跳过去』)必须把每个动作都执行完——"
    "逐个调工具,做完一个再做下一个,全部完成后才给最终回答,**别只做第一步就停**。\n"
    "例:用户**明确说**「总结这章**并做成卡**」(带了『做成卡』三个字)。正确顺序:\n"
    "  第1步 → {\"tool\":\"read_section\",\"args\":{}}(或 summarize_section)\n"
    "  第2步(拿到正文后)→ {\"tool\":\"make_anki\",\"args\":{\"text\":\"<你总结出的要点>\"}}\n"
    "  第3步(制卡已提交后)→ 才给最终回答:「总结好了:…;卡也在做了,完成会通知你」。\n"
    "  ——第2步不能省,**是因为用户带了『做成卡』**;若用户只说「总结这章」(没提卡),就**只总结 + 给文字回答**,绝不 make_anki。\n"
    "★用户说『跳过去/翻到/去第X章』且目标明确(或 search_book 只有一个最相关命中)时,直接 goto_section,别反问;只有真有多个差不多的选项才反问。"
    "把『第N章/某节/前言』换算成章节 idx:先 list_sections 看目录(label→idx),再 goto_section/read_section。\n"
    "★高亮重点:先 read_section 拿到正文,再把要强调的几句**原句逐字**(从正文照抄,不改写/翻译)"
    "**一次性**放进 epub_highlight 的 texts 数组(一次调用搞定,别一句一调),否则前端在章节里定位不到。\n"
    "★**批量标整章/多章重点**(如『把这一章/第X章的重点都高亮』)→ **直接用 auto_highlight**"
    "(它内部把章正文外包给挑句专家、挑好原句交前端串行画高亮,正文不进你的上下文,省大量 token):"
    "EPUB『一章』通常跨**多个 section**(每个小节一个 section);先 list_sections 找到该章的 idx,再 auto_highlight(section=该章idx)即可——"
    "它会**自动展开整章所有 section**,你不用自己算区间(也别只传第一个小节);要自定义范围才用 from,to / sections=[..]。"
    "**别自己逐章 read_section+epub_highlight**(那会把每章正文反复灌进上下文,又慢又贵)。"
    "auto_highlight 回来后,把『标了哪些章、共多少句』简洁告诉用户即可。\n"
    "★**删除/取消/清理高亮**(如『把这章/这本书的高亮都取消』)→ 拿 **find_highlights** 把匹配高亮**逐条列在对话里**"
    "(每条带「↗跳转」+「🗑删除」按钮,用户点哪条删哪条)。"
    "**这就是删除入口,绝不要说没有**;你**别替用户删**,调完只简短说一句『下面是这些高亮,可逐个跳转或删除』。"
    "**严禁为了删高亮去 summarize_section / 总结 / read_section**(用户只想删,没要总结/正文);find_highlights 调完就停,别再做别的。\n"
    "★高亮重点逐字照抄:无论 epub_highlight 还是据 auto_highlight 结果转述,引用原句都不改写/不翻译。\n"
    "★凡涉及『我(没)掌握哪些词/这章生词/某词我会不会』——**必须调 section_vocab 查掌握度数据库**,"
    "**严禁**拿正文里的词自己猜谁掌握没掌握(数据库才准:已掌握的不算生词、从没查过的不视为生词)。"
    "不传 words 拿本章未掌握生词;问具体某些词会不会就传 words:[...]。\n"
    "★查词/读音(尤其日语读音、英语音标释义)**一律先用 lookup_word**——走 ECDICT/unidic 离线权威词典,"
    "**读音和释义以它为准,严禁自己编读音**;你只结合上下文挑义项、讲解。\n"
    "★『总结这一章/这一节/这部分』用 summarize_section(深度总结,省额度)或 read_section(拿整章原文自己总结);只读某章内容也用 read_section。\n"
    "★『我哪本书讲过X/别的书有没有X/之前在哪见过』用 search_all_books;要跳到搜到的别的书用 open_book(file_rel,page)。\n"
    "★想把当前内容跟用户**已学过/已记过的笔记**串起来(用户问『我之前记过吗/笔记里有没有X/跟我学的Y有关』,或要结合他知识体系深入讲)→ "
    "用 recall_notes(query=主题)召回他自己的笔记;召回到就点出『你在《X》笔记里记过…』帮他连点成线,没召回到就按通用知识答、别硬扯。\n"
    "★可溯源:凡复述/引用书里的具体内容,在句末标来源章。**优先照抄目录章名**——从下方目录映射表(或 list_sections 的 label)"
    "里找**覆盖该 section idx 的条目**,章名原文照抄放进括号,如「(第1章 原子の運動)」「(第三章 力学)」,别自己起名/改写/翻译;"
    "该位置在目录里没有条目(或本书无目录)才退回「(第N章)」,N=**工具实际返回的 section idx**,别编。"
    "⚠ spine idx≠人类章号(封面/目次等前付也各占 idx),所以**绝不要**自己把 idx 改写成『第N章』以外的人类章号——要么照抄目录章名,要么原样写 idx。"
    "两种写法前端都会变成可点跳转。\n"
    "★【最近对话】里每条用户消息都带括号标注了当时所在的书/章/选中。用户说『刚才那章/上一章/回到那段/前面那段』时,"
    "**从标注里取出确切章节 idx**,直接 goto_section / read_section(idx),别反问。\n"
    "★【追问建议】每次给最终回答时,在正文最后**另起一行**写 2-3 个贴合当前内容、能推进理解的下一步问题,"
    "格式就一行:[[FOLLOWUP]]问题1|问题2|问题3(用 | 分隔,放整条回答末尾,前端会渲成可点按钮;问题要短、具体)。"
    "**每条最终回答都要带**;只有在调工具(输出 JSON)那几条里不要带。"
)


def _fav_sys_block(ctx, cur) -> str:
    """收藏集专用系统提示块(① 收藏集声明 ② 目录概览:每条出处+首句+相邻标记 ③ read_source_page 广告)。
    非收藏集 → 空串。进**动态**块(拼在 __当前章节__ 之后 → 不入 _esys_static 缓存)→ 普通书零影响。"""
    fid = _fav_fid((ctx or {}).get("file_rel") or "")
    if not fid:
        return ""
    decl = ("\n★【这是一个「收藏集」,不是普通书】它由用户从**不同的书/位置**精选的页面/章节拼成——"
            "**条目之间通常不连续**、常来自不同原书。这本书里每个「章节(section idx)」就是**一条收藏**,并标注了它的**原书出处**。"
            "所以:① 别把 section idx 相邻的两条默认当成同一段连续上下文——只有下面目录标了『↳接上条(同书连续)』的才真连续;"
            "② 要某条在原书里的前后文/上下文时,用 **read_source_page** 翻回**原书**那几页(而不是读收藏集里相邻的另一条);"
            "③ 引用某条内容时按它的**原书出处**标来源(如「(《线性代数》第12页)」)。")
    meta = _fav_meta_for(ctx)
    items = meta.get("items") or []
    dir_line = ""
    if items:
        shown = items[:60]
        rows = []
        for r in shown:
            i = r.get("section")
            src = r.get("src_name") or "?"
            k = r.get("kind")
            if k == "pdf" and isinstance(r.get("src_page"), int):
                loc = "第%d页" % r["src_page"]
            elif k == "epub" and isinstance(r.get("src_section"), int):
                loc = "第%d节" % (r["src_section"] + 1)
            elif k == "userpage":
                loc = "我的页"
            else:
                loc = ""
            miss = "(原书缺失)" if r.get("missing") else ""
            adj = " ↳接上条(同书连续)" if r.get("adj_prev") else ""
            here = " ←当前" if (cur is not None and i == cur) else ""
            snip = (r.get("snippet") or "").strip()
            row = f"[{i}]《{src}》{loc}{miss}{adj}{here}"
            if snip:
                row += " · " + snip[:50]
            rows.append(row)
        more = ("\n(共 %d 条,此处列前 %d 条,其余用 list_sections 看)" % (len(items), len(shown))) \
               if len(items) > len(shown) else ""
        dir_line = ("\n【收藏集目录】(每行=一条收藏:[section idx]《原书》位置 + 首句;标『↳接上条』的才与上一条连续):\n"
                    + "\n".join(rows) + more)
    tool_line = ("\n【收藏集专用工具】read_source_page:翻回**原书**读任意页/节拿更多上下文——"
                 "args {item:收藏条目的 section idx(读它对应的原书页/节),或 book+page/section 直接指定;"
                 "offset:相对偏移(+1 下一页/节、-1 上一页/节)}。某条收藏本身不够、要看它在原书的前后文/查证时调它。")
    return decl + dir_line + tool_line


def _esys_prompt(ctx):
    A = _A()
    cat = "\n".join(f"- {n}: {d}" for n, (d, _f) in _etools.items())
    try:
        cur = int(ctx.get("current_section_idx")) if ctx.get("current_section_idx") is not None else None
    except Exception:
        cur = None
    meta = {"book": ctx.get("book") or ctx.get("book_name"),
            "当前章节idx": cur, "共章节数": ctx.get("total_sections")}
    meta = {k: v for k, v in meta.items() if v not in (None, "")}
    sel = A._clean_tag(ctx.get("selection"))
    sent = A._clean_tag(ctx.get("selection_sentence"))
    sel_line = ""
    if sel:
        sel_line = f"\n用户当前选中:「{sel[:200]}」"
        if sent and sent.replace(" ", "") != sel.replace(" ", ""):
            sel_line += f"\n选中所在句(已给好的上下文,可直接据此判读/查词/解释,**不必**再 read_section):「{sent[:300]}」"
        sel_line += ("\n★有选中=默认他在问这段选中内容,优先针对**选中**回答/查词/翻译/解释/制卡。"
                     "查词/读音先 lookup_word 拿权威读音+释义再挑义项(日语严禁自己编读音)。")
    toc = ctx.get("toc")
    toc_line = ""
    if isinstance(toc, list) and toc and not _fav_fid(ctx.get("file_rel") or ""):   # 收藏集改用下面更丰富的【收藏集目录】,不重复注 toc_line 省 token
        items = "；".join(f"{(e.get('label') or '')[:30]}→idx{e.get('idx')}"
                          for e in toc[:40] if isinstance(e, dict) and e.get("idx") is not None)
        if items:
            toc_line = (f"\n本书目录(章名label↔章节idx 映射表;把『第N章/某节』换算成工具用的 idx、"
                        f"以及给回答标来源时照抄章名,都用它):{items}")
    # 带入的图(用户点/拖进来的章节插图):各自带 AI 文字描述当上下文;要核对图像细节才 see_figure
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    figs = [f for f in figs if isinstance(f, dict)]
    fig_line = ""
    if figs:
        desc_lines = []
        for k, f in enumerate(figs[:3], 1):
            if f.get("kind") == "note":   # 双击带入的手写便签(kind:'note';文字/位置正文先给足,笔画内容 see_figure 看合成图)
                line = f"图{k}:用户的**手写便签**(便签文字如下,手写笔画的内容要 see_figure 看合成图):「{A._clean_tag(f.get('desc'))[:300]}」"
                near = A._clean_tag(f.get("near"))
                if near:
                    line += f";便签位置附近正文:「{near[:400]}」"
                desc_lines.append(line)
                continue
            d = A._clean_tag(f.get("desc") or f.get("caption") or f.get("alt") or "")
            tag = "  ★图上有用户的**手写圈点/标注**" if f.get("has_ink") else ""
            desc_lines.append((f"图{k}:{d[:300]}" if d else f"图{k}:(暂无文字描述)") + tag)
        any_fig_ink = any(f.get("has_ink") for f in figs[:3])
        fig_line = ("\n用户带入了 " + str(len(figs)) + " 张图(他点/拖进来的章节插图):\n" + "\n".join(desc_lines)
                    + "\n先据这些说明回答;说明不够或需核对图里具体细节/排版时,才用 see_figure(args {index:第几张,从1起;不传=全部})。")
        if any_fig_ink:
            fig_line += ("\n★上面有图带了**用户的手写圈点/标注**——他问『这个/这一列/这一行/我圈的/我标的/这是什么/看一下』,"
                         "多半就是在问他圈的那一块。这类问题**必须先 see_figure 看合成图**(会把他的手写笔迹叠在图上),别只看文字描述猜、更别答成别的图或别的内容。")
    # 双击带入的便签(无笔画:文字+锚点附近正文走文本通道;有笔画的以 kind:'note' 并在上面 figures 里走视觉)
    notes_att = [n for n in (ctx.get("notes") or []) if isinstance(n, dict) and (n.get("text") or n.get("near"))]
    note_line = ""
    if notes_att:
        nitems = []
        for i, nb in enumerate(notes_att[:4], 1):
            sec = nb.get("section")
            loc = f"(贴在章节 idx{sec})" if isinstance(sec, int) and sec >= 0 else ""
            it = f"[{i}]{loc} 便签文字:「{A._clean_tag(nb.get('text'))[:400]}」"
            near = A._clean_tag(nb.get("near"))
            if near:
                it += f";便签位置附近正文:「{near[:600]}」"
            nitems.append(it)
        note_line = ("\n用户双击带入了自己写的便签(默认在问它/要你结合它答;便签位置附近正文已给好,**别为此再 read_section**):\n"
                     + "\n".join(nitems))
    return (_ESYS_RULES + f"\n\n【可用工具】\n{cat}\n\n【当前章节】"
            + json.dumps(meta, ensure_ascii=False) + sel_line + toc_line + fig_line + note_line
            + _fav_sys_block(ctx, cur))


_ESYS_STATIC_CACHE = None


def _esys_static():
    """静态系统提示(规则+工具目录),恒定 → 缓存。给 Claude --system-prompt / Gemini systemInstruction。"""
    global _ESYS_STATIC_CACHE
    if _ESYS_STATIC_CACHE is None:
        full = _esys_prompt({})
        i = full.rfind("【当前章节】")
        _ESYS_STATIC_CACHE = (full[:i].rstrip() if i >= 0 else full)
    return _ESYS_STATIC_CACHE


def _ectx_block(ctx):
    """动态部分(【当前章节】+ 选中 + 目录),每轮随 ctx 变 → 拼进 user message。"""
    full = _esys_prompt(ctx)
    i = full.rfind("【当前章节】")
    return full[i:] if i >= 0 else ""


def _efmt_history(history):
    """近几轮对话,用户那轮标上当时所在的书/章/选中(供『刚才那章』定位)。"""
    out = []
    for h in (history or [])[-6:]:
        if h.get("role") == "user":
            bits = []
            book = _A()._clean_tag(h.get("book"))
            if book:
                bits.append(book)
            if h.get("section") is not None:
                bits.append(f"第{h.get('section')}章")
            selh = _A()._clean_tag(h.get("selection"))
            if selh:
                bits.append("选中「" + selh[:40] + "」")
            tag = ("(" + "，".join(bits) + ")") if bits else ""
            role = "用户"
        else:
            tag, role = "", "助手"
        c = (h.get("content") or "").strip()
        if c:
            out.append(f"{role}{tag}:{c[:600]}")
    return ("【最近对话】\n" + "\n".join(out) + "\n") if out else ""


# ──────────────────────── agent 工具循环(复用 assistant 的 AI/SSE 骨架)────────────────────────
def _emit_tool_side(res, name):
    """工具结果里的副作用事件(client_action[s] / 后台任务 / 撤销 / 撤销重做卡)→ SSE 事件列表。"""
    evs = []
    if not isinstance(res, dict):
        return evs
    if res.get("action"):                              # 同步写工具直接给出的可撤销/重做记录(系统自动,不依赖 AI 文本)
        evs.append({"event": "action", "data": res.pop("action")})
    if res.get("client_action"):                       # 单个动作(跳章 / 整章 epubHighlight 串行批 / showHlPicker)
        evs.append({"event": "actions", "data": [res.pop("client_action")]})
    acts = res.get("client_actions")                   # 多个动作(向后兼容;现 auto_highlight 改走单 client_action)
    if isinstance(acts, list) and acts:
        res.pop("client_actions", None)
        evs.append({"event": "actions", "data": acts})
    if res.get("task_id"):
        evs.append({"event": "task", "data": {"task_id": res["task_id"], "label": _elabel(name)}})
    if res.get("undo_id"):
        evs.append({"event": "undo", "data": {"undo_id": res["undo_id"], "label": _elabel(name),
                                              "section": res.pop("_jump_section", None)}})
    return evs


def _eagent_run(message, ctx, history, force_effort=None, force_model=None):
    """生成 SSE 事件 dict {event,data}。按用户 orchestrator 预设选 Claude / Gemini(跟 PDF 助手统一)。"""
    A = _A()
    A._tok_reset()
    uid = (ctx or {}).get("_uid")
    fe = force_effort if force_effort in A._EFFORTS else None
    fm = force_model if force_model in A._CLAUDE_VARIANTS else None
    if fm or fe:                       # 感叹号「更强重答」:一次性强制 Claude 升档
        if isinstance(ctx, dict):
            ctx["_no_cache"] = True
        yield from _eagent_claude(message, ctx, history, (fm or "opus"), (fe or "high"), uid)
        return
    # @paid 预设:节流探测免费额度是否恢复 → 恢复则摘除;绿条交 A._recover_gate 按**真实请求**裁决
    # (同 PDF 助手,修"刚宣布恢复又受限"矛盾双提示——探测 1-token 能过 ≠ 真实请求能过)
    _pn_rec = A._paid_recover_check(uid, "orchestrator")

    def _erest():
        rr = A._resolve("orchestrator", uid)
        if rr["backend"] == "gemini":
            yield from _eagent_gemini(message, ctx, history, rr["variant"], rr["depth"], uid)
            return
        if A._is_quick(message):
            eff, mdl = "low", (rr["variant"] or A._AGENT_MODEL)
        else:
            eff = (rr["depth"] if rr["depth"] in A._EFFORTS else None) or A._effort_for(message, ctx, uid)
            mdl = rr["variant"] or A._AGENT_MODEL
        yield from _eagent_claude(message, ctx, history, mdl, eff, uid)

    yield from (A._recover_gate(_erest(), _pn_rec, uid, "orchestrator") if _pn_rec else _erest())


def _eagent_claude(message, ctx, history, mdl, eff, uid, fallback_from=None):
    """orchestrator 跑在 Claude(复用 assistant._spawn/_send_stream/_parse_tool)。"""
    A = _A()
    if fallback_from:
        trace = [{"label": "编排+回答(Gemini 不可用→Claude)",
                  "model": f"{A._variant_short(fallback_from)}→{mdl}·{eff}", "action": "orchestrator"}]
    else:
        trace = [{"label": "编排+回答", "model": f"{mdl}·{eff}", "action": "orchestrator"}]
    p = A._spawn(effort=(eff if eff in A._EFFORTS else "low"), model=mdl, system=_esys_static())
    if not p:
        yield {"event": "error", "data": "助手起不来(claude 起不来)"}
        return
    qw = A._quota_warning()
    if qw:
        yield {"event": "notice", "data": qw}
    try:
        content = (f"{_ectx_block(ctx)}\n\n{_efmt_history(history)}【用户】{message}\n\n"
                   "现在开始(调工具就只输出 JSON,能答就直接答):")
        t_start = time.time()
        repair = 0
        heavy = eff in ("xhigh", "max")
        round_to = 180.0 if heavy else 90.0
        total_to = 1200.0 if heavy else 900.0
        for _step in range(400):
            if time.time() - t_start > total_to:
                yield {"event": "answer", "data": "(这个任务很大,先做到这——已完成的部分都已保存;想接着干就再说一句『继续处理后面的』)"}
                break
            raw = None
            last_emit = 0.0
            for kind, val in A._send_stream(p, content, timeout=round_to):
                if kind == "delta":
                    if val and not val.lstrip().startswith("{"):
                        now = time.time()
                        if now - last_emit > 0.1:
                            last_emit = now
                            yield {"event": "answer", "data": val}
                else:
                    raw = val
            if not raw:
                yield {"event": "error", "data": "助手没响应(超时)。再问我一次试试。"}
                return
            tool = A._parse_tool(raw)
            rs = (raw or "").strip()
            if tool is None and rs.startswith("{") and '"tool"' in rs[:400] and repair < 2:
                repair += 1
                yield {"event": "tool", "data": "整理指令"}
                yield {"event": "tool-done", "data": "整理指令"}
                content = ("你上一条像是工具调用,但 JSON 没解析成功(很可能字符串里有没转义的双引号或换行)。"
                           "请重新只输出**一条合法的 JSON**工具调用:字符串里引号换成中文引号「」、不要带换行、整条只输出 JSON 别加别的字。")
                continue
            _fn = _tool_fn(tool.get("tool"), ctx) if tool else None
            if _fn:
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _elabel(name)}
                t0 = time.time()
                try:
                    res = _fn(targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                sec = round(time.time() - t0, 1)
                gm = res.pop("_gen_model", None) if isinstance(res, dict) else None
                ga = res.pop("_gen_action", None) if isinstance(res, dict) else None
                trace.append({"label": _elabel(name), "model": gm or "—", "sec": sec, "action": ga,
                              "detail": A._step_detail(res)})
                for ev in _emit_tool_side(res, name):
                    yield ev
                yield {"event": "tool-done", "data": _elabel(name)}
                content = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                continue
            trace[0]["detail"] = (raw or "")[:6000]
            yield {"event": "answer", "data": raw}
            break
        else:
            yield {"event": "answer", "data": "(步骤已经非常多了,先到这——已完成的部分都已保存,要继续就再说一句)"}
        tool_total = sum(t.get("sec", 0) for t in trace[1:])
        trace[0]["sec"] = round(max(0.0, (time.time() - t_start) - tool_total), 1)
        tt = A._tok_get()
        if tt:
            trace[0]["tok"] = tt
        yield {"event": "trace", "data": trace}
    finally:
        A._kill(p)


def _eagent_gemini(message, ctx, history, variant, depth, uid):
    """orchestrator 跑在 Gemini(复用 assistant._gemini_stream/_compact_gemini_contents)。
    Gemini 首轮失败(还没调过工具)→ 自动回退 Claude,保证不挂。"""
    A = _A()
    model = variant if A._is_gemini(variant) else "gemini-3.5-flash"
    think = (depth != "none") and not A._is_quick(message)
    if "pro" in model:
        think = True
    trace = [{"label": "编排+回答", "model": f"{A._variant_short(model)}·{'think' if think else 'fast'}",
              "action": "orchestrator"}]
    system = _esys_static()
    content_txt = (f"{_ectx_block(ctx)}\n\n{_efmt_history(history)}【用户】{message}\n\n"
                   "现在开始(调工具就只输出 JSON,能答就直接答):")
    contents = [{"role": "user", "parts": [{"text": content_txt}]}]
    t_start = time.time()
    tools_ran = False
    repair = 0
    paid_noted = False   # 「免费受限→已用付费」提示每轮请求最多一次
    # 注:这里**不**注入 A._quota_warning()——那是 Claude Code 的额度,本路径编排跑在 Gemini,
    # 提 Claude 额度纯属噪音(修:用 Gemini 时下方仍提示 Claude 额度)。回退进 _eagent_claude 时那边会提。
    try:
        for _step in range(400):
            if time.time() - t_start > 900.0:
                yield {"event": "answer", "data": "(这个任务很大,先做到这——已完成的部分都已保存;想接着干就再说一句『继续处理后面的』)"}
                break
            raw_parts = []
            last_emit = 0.0
            err = None
            for kind, val in A._gemini_stream(system, contents, model=model, think=think):
                if kind == "delta":
                    raw_parts.append(val)
                    acc = "".join(raw_parts).lstrip()
                    if acc and not acc.startswith("{"):
                        now = time.time()
                        if now - last_emit > 0.1:
                            last_emit = now
                            # 发**累计**文本(replace 语义,跟 Claude 路径 _send_stream 一致)→ 前端 answer 事件统一「整段替换」
                            yield {"event": "answer", "data": "".join(raw_parts)}
                elif kind == "err":
                    err = val
                elif kind == "tier":
                    trace[0]["tier"] = val
                    if val == "paid" and not paid_noted:   # 免费本是首选却落到付费 → 轻量提示(带一键转直连付费)
                        paid_noted = True
                        pn = A._paid_fallback_note(model, "orchestrator", depth)
                        if pn:
                            yield {"event": "gemini-paid", "data": pn}
            raw = "".join(raw_parts).strip()
            if not raw:
                if not tools_ran:
                    why = f"Gemini({A._variant_short(model)})不可用,转 Claude" + (f":{err}" if err else "")
                    yield {"event": "tool", "data": why}
                    yield {"event": "tool-done", "data": why}
                    yield from _eagent_claude(message, ctx, history, A._AGENT_MODEL,
                                              A._effort_for(message, ctx, uid), uid, fallback_from=model)
                    return
                yield {"event": "error", "data": f"助手没响应({err or '超时'})。再问我一次试试。"}
                return
            tool = A._parse_tool(raw)
            if tool is None and raw.startswith("{") and '"tool"' in raw[:400] and repair < 2:
                repair += 1
                yield {"event": "tool", "data": "整理指令"}
                yield {"event": "tool-done", "data": "整理指令"}
                contents.append({"role": "model", "parts": [{"text": raw}]})
                contents.append({"role": "user", "parts": [{"text":
                    "你上一条像是工具调用,但 JSON 没解析成功。请只重新输出**一条合法 JSON**工具调用:"
                    "字符串里反斜杠写成 \\\\、引号用「」、整条别带换行,别加别的字。"}]})
                continue
            _fn = _tool_fn(tool.get("tool"), ctx) if tool else None
            if _fn:
                tools_ran = True
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _elabel(name)}
                t0 = time.time()
                try:
                    res = _fn(targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                sec = round(time.time() - t0, 1)
                gm = res.pop("_gen_model", None) if isinstance(res, dict) else None
                ga = res.pop("_gen_action", None) if isinstance(res, dict) else None
                trace.append({"label": _elabel(name), "model": gm or "—", "sec": sec, "action": ga,
                              "detail": A._step_detail(res)})
                for ev in _emit_tool_side(res, name):
                    yield ev
                yield {"event": "tool-done", "data": _elabel(name)}
                feed = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                contents.append({"role": "model", "parts": [{"text": raw}]})
                contents.append({"role": "user", "parts": [{"text": feed}]})
                A._compact_gemini_contents(contents)
                continue
            trace[0]["detail"] = (raw or "")[:6000]
            yield {"event": "answer", "data": raw}
            break
        else:
            yield {"event": "answer", "data": "(步骤已经非常多了,先到这——已完成的部分都已保存,要继续就再说一句)"}
        tool_total = sum(t.get("sec", 0) for t in trace[1:])
        trace[0]["sec"] = round(max(0.0, (time.time() - t_start) - tool_total), 1)
        tt = A._tok_get()
        if tt:
            trace[0]["tok"] = tt
        yield {"event": "trace", "data": trace}
    except Exception as e:
        yield {"event": "error", "data": f"Gemini 编排出错:{str(e)[:120]}"}


# ──────────────────────── 对话历史(EPUB,按 uid+file 分键,独立命名空间)────────────────────────
_ECONVO_DIR = CLAUDE_DIR / "state" / "epub-convo"
_econvo_lock = threading.Lock()


def _file_key(file_rel: str) -> str:
    return hashlib.sha1((file_rel or "").encode("utf-8")).hexdigest()[:16] or "default"


def _econvo_path(uid, file_rel) -> Path:
    return _ECONVO_DIR / (str(uid or "anon")) / (_file_key(file_rel) + ".json")


def _econvo_load(uid, file_rel):
    p = _econvo_path(uid, file_rel)
    try:
        return json.loads(p.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        try:
            if p.exists() and p.stat().st_size > 2:
                p.rename(p.with_name(p.name + f".corrupt.{int(time.time())}"))
        except Exception:
            pass
        return []


def _econvo_append(uid, file_rel, role, content, meta=None):
    with _econvo_lock:
        msgs = _econvo_load(uid, file_rel)
        rec = {"role": role, "content": content, "ts": int(time.time())}
        if meta:
            for k in ("section", "book", "selection", "sel_anchor", "trace", "actions"):
                v = meta.get(k)
                if v is not None and v != "":
                    rec[k] = v
        msgs.append(rec)
        try:
            p = _econvo_path(uid, file_rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(msgs[-200:], ensure_ascii=False), "utf-8")
            os.replace(tmp, p)
        except Exception:
            pass


def _econvo_clear(uid, file_rel):
    with _econvo_lock:
        try:
            p = _econvo_path(uid, file_rel)
            if p.exists():
                p.unlink()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# 写操作「撤销/重做」持久化系统
#   每个写工具(制卡/笔记/生词/高亮)落地后,系统自动生成一条结构化 action 记录:
#     {id, kind(=工具名), title, detail, undo(自包含逆操作 payload), redo(自包含正操作 payload), state}
#   action 塞进对应 assistant 消息的 meta["actions"](随对话持久化,刷新后还在);前端据 state 重渲卡。
#   实际写入分两类:
#     · 制卡/笔记/生词 = 后台任务(voice._run_task),完成后由前端拿 undo_id 调 /epub-action {op:from_task}
#       → 这里读 voice 撤销日志 + 快照(notesInfo / 读笔记原文)建 action,附到最近 assistant 消息。
#     · 高亮 = 前端在已渲染章里定位画 CFI(服务端拿不到 DOM 偏移)→ 前端建 action 调 {op:attach} 附库。
#   undo/redo 走 /epub-action {op:undo|redo}:按 payload 执行删/建(纯写,不调 AI、不耗额度),
#   并把新状态(+ 重做后变化的 note_ids/highlight ids)写回存储 → 刷新后状态正确、还能再 undo/redo。
# ════════════════════════════════════════════════════════════════════════════
_eaction_seq = 0
_eaction_lock = threading.Lock()


def _new_action_id() -> str:
    global _eaction_seq
    with _eaction_lock:
        _eaction_seq += 1
        return f"act_{int(time.time())}_{_eaction_seq}"


def _vmod():
    import voice   # 复用其 AnkiConnect 调用 + 撤销日志(同进程已加载)
    return voice


def _safe_note_path(path):
    """vault 内笔记路径校验(防越界);返回 Path 或 None。"""
    p = (path or "").strip()
    if not p or ".." in p:
        return None
    try:
        ap = (VAULT_ROOT / p).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
    except Exception:
        return None
    return ap


def _strip_tags(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def _anki_first_field(note_spec) -> str:
    for v in (note_spec.get("fields") or {}).values():
        if v:
            return _strip_tags(v)
    return ""


def _anki_snapshot(note_ids):
    """note_ids → addNotes 可直接重建的 specs(deck 固定 QA,跟 _run_snippets_to 一致)。纯快照,不调 AI。"""
    if not note_ids:
        return []
    try:
        info = _vmod()._anki_req("notesInfo", {"notes": note_ids}).get("result") or []
    except Exception:
        info = []
    notes = []
    for it in info:
        if not it:
            continue
        flds = {k: (fv.get("value") if isinstance(fv, dict) else fv)
                for k, fv in (it.get("fields") or {}).items()}
        if not flds:
            continue
        notes.append({"deckName": "QA", "modelName": it.get("modelName") or "Basic",
                      "fields": flds, "tags": it.get("tags") or ["pdf-snippets"]})
    return notes


def _anki_sync_bg():
    """制卡/删卡后 AnkiWeb sync,fire-and-forget(跟现有写操作一致;失败由 15min anki-sync-refresh 兜底)。"""
    def _go():
        try:
            _vmod()._anki_req("sync")
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


# ── 逆/正操作执行(就地改 action 的 payload + state;返回 (ok, err))。纯写,不调 AI。 ──
def _action_undo(action):
    u = action.get("undo") or {}
    op = u.get("op")
    if op == "anki_delete":
        ids = [i for i in (u.get("note_ids") or []) if i]
        if ids:
            try:
                _vmod()._anki_req("deleteNotes", {"notes": ids})
            except Exception as e:
                return False, str(e)[:120]
            _anki_sync_bg()
        action["undo"] = {"op": "anki_delete", "note_ids": []}   # 卡已删,redo 会重建出新 ids
        return True, None
    if op == "note_delete":
        ap = _safe_note_path(u.get("path"))
        if ap and ap.exists():
            try:
                ap.unlink()
            except Exception as e:
                return False, str(e)[:120]
        return True, None
    if op == "hl_delete":
        file = (u.get("file") or "").strip()
        ids = set(u.get("ids") or [])
        if file and ids:
            try:
                pdf = _pdf()
                items = [h for h in pdf._epub_hl_load(file) if h.get("id") not in ids]
                pdf._epub_hl_save(file, items)
            except Exception as e:
                return False, str(e)[:120]
        action["undo"] = {"op": "hl_delete", "file": u.get("file") or "", "ids": []}
        return True, None
    if op == "sticky_delete":   # 撤销 AI 建的便签 = 删掉(redo 用快照原 id 重建,payload 不用改写)
        file = (u.get("file") or "").strip()
        ids = set(u.get("ids") or [])
        if file and ids:
            try:
                pdf = _pdf()
                pdf._notes_save(file, [n for n in pdf._notes_load(file) if n.get("id") not in ids])
            except Exception as e:
                return False, str(e)[:120]
        return True, None
    if op == "sticky_set":   # 撤销 AI 的便签修改 = 恢复旧 text/color 快照(只这两个字段,绝不动 strokes/anchor/尺寸)
        return _sticky_set(u)
    return False, "未知的撤销操作"


def _sticky_set(p):
    """按 payload {file,id,fields:{text?,color?}} 设便签字段(undo=旧值快照 / redo=新值,同一执行器)。"""
    file = (p.get("file") or "").strip()
    nid = (p.get("id") or "").strip()
    fields = p.get("fields") or {}
    if not (file and nid):
        return False, "缺 file / id"
    try:
        pdf = _pdf()
        items = pdf._notes_load(file)
        n = next((x for x in items if x.get("id") == nid), None)
        if not n:
            return False, "便签已不存在(可能被删了)"
        for k in ("text", "color"):   # 白名单:只允许这两个字段
            if k in fields:
                n[k] = fields[k]
        n["updated"] = int(time.time())
        pdf._notes_save(file, items)
        return True, None
    except Exception as e:
        return False, str(e)[:120]


def _action_redo(action):
    r = action.get("redo") or {}
    op = r.get("op")
    if op == "anki_add":
        notes = r.get("notes") or []
        if not notes:
            return False, "没有可重建的卡片快照"
        try:
            res = _vmod()._anki_req("addNotes", {"notes": notes})
            new_ids = [i for i in (res.get("result") or []) if i]
        except Exception as e:
            return False, str(e)[:120]
        if not new_ids:
            return False, "重新建卡失败"
        action["undo"] = {"op": "anki_delete", "note_ids": new_ids}   # 新 ids 接管撤销
        _anki_sync_bg()
        return True, None
    if op == "note_create":
        ap = _safe_note_path(r.get("path"))
        if not ap:
            return False, "笔记路径非法"
        if ap.exists():   # 撤销时删过;若被别的东西占了,另起名
            stem, parent = ap.stem, ap.parent
            for i in range(1, 200):
                cand = parent / f"{stem}-{i}.md"
                if not cand.exists():
                    ap = cand
                    break
        try:
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(r.get("content") or "", "utf-8")
        except Exception as e:
            return False, str(e)[:120]
        rel = ap.relative_to(VAULT_ROOT.resolve()).as_posix()
        action["undo"] = {"op": "note_delete", "path": rel}
        action["redo"]["path"] = rel
        return True, None
    if op == "hl_create":
        file = (r.get("file") or "").strip()
        items = r.get("items") or []
        if not (file and items):
            return False, "没有可重建的高亮"
        try:
            import uuid as _u
            pdf = _pdf()
            cur = pdf._epub_hl_load(file)
            new_ids = []
            for it in items:
                h = {"id": "e" + _u.uuid4().hex[:11], "cfi": (it.get("cfi") or ""),
                     "anchor": {"section": it.get("section")},
                     "text": (it.get("text") or "")[:2000], "color": (it.get("color") or "#ffd54a"),
                     "note": "", "sentence": "", "body": "", "kind": "", "time": int(time.time())}
                cur.append(h)
                new_ids.append(h["id"])
            pdf._epub_hl_save(file, cur)
        except Exception as e:
            return False, str(e)[:120]
        action["undo"] = {"op": "hl_delete", "file": file, "ids": new_ids}
        return True, None
    if op == "sticky_create":   # 重做建便签:按完整快照重插(保留原 id → undo payload 始终有效;已存在则跳过,幂等)
        file = (r.get("file") or "").strip()
        notes = [n for n in (r.get("notes") or []) if isinstance(n, dict) and n.get("id")]
        if not (file and notes):
            return False, "没有可重建的便签快照"
        try:
            pdf = _pdf()
            items = pdf._notes_load(file)
            have = {x.get("id") for x in items}
            for n in notes:
                if n["id"] not in have:
                    items.append(n)
            pdf._notes_save(file, items)
        except Exception as e:
            return False, str(e)[:120]
        return True, None
    if op == "sticky_set":   # 重做便签修改 = 应用新值快照(同 undo 一个执行器)
        return _sticky_set(r)
    return False, "未知的重做操作"


def _build_action_from_undo(undo_id, file_rel):
    """后台任务(制卡/笔记/生词)完成后,据 voice 撤销日志的 undo_id 建完整 action(含自包含 undo/redo 快照)。"""
    if not undo_id:
        return None
    v = _vmod()
    entry = None
    try:
        with v._undo_lock:
            for e in reversed(v._undo_log):
                if e.get("id") == undo_id:
                    entry = dict(e)
                    break
    except Exception:
        entry = None
    if not entry:
        return None
    kind, handle, label = entry.get("kind"), (entry.get("handle") or {}), (entry.get("label") or "")
    aid = "act_" + str(undo_id)
    if kind in ("anki", "vocab"):
        note_ids = [i for i in (handle.get("note_ids")
                                or ([handle.get("card_id")] if handle.get("card_id") else [])) if i]
        notes = _anki_snapshot(note_ids)
        if kind == "vocab":
            tk = "add_vocab"
            title = "加生词本:" + label.replace("生词卡", "").strip()
        else:
            tk = "make_anki"
            title = "制卡:" + label
        if notes:
            detail = "新建卡片(共 %d 张):\n" % len(notes) + "\n".join(
                "· " + _anki_first_field(n)[:90] for n in notes)
        else:
            detail = label or "(卡片信息已不可读)"
        return {"id": aid, "kind": tk, "title": title, "detail": detail,
                "undo": {"op": "anki_delete", "note_ids": note_ids},
                "redo": {"op": "anki_add", "notes": notes},
                "state": "done", "ts": int(time.time())}
    if kind == "note":
        path = (handle.get("path") or "").strip()
        ap = _safe_note_path(path)
        content = ""
        if ap and ap.exists():
            try:
                content = ap.read_text("utf-8")[:100000]
            except Exception:
                content = ""
        title = "整理笔记:" + label.replace("笔记《", "").replace("》", "").strip()
        detail = (content[:600] + ("…" if len(content) > 600 else "")) if content else (label or path)
        return {"id": aid, "kind": "make_note", "title": title, "detail": detail,
                "undo": {"op": "note_delete", "path": path},
                "redo": {"op": "note_create", "path": path, "content": content},
                "state": "done", "ts": int(time.time())}
    return None


# ── action 的持久化(塞进 assistant 消息 meta["actions"];upsert by id 幂等)──
def _econvo_save_all(uid, file_rel, msgs):
    try:
        p = _econvo_path(uid, file_rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(msgs[-200:], ensure_ascii=False), "utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def _econvo_attach_actions(uid, file_rel, actions):
    """把 action(列表)upsert 进最近一条 assistant 消息的 actions。找不到 assistant 消息 → False(前端卡仍在内存可用)。"""
    if not actions:
        return False
    with _econvo_lock:
        msgs = _econvo_load(uid, file_rel)
        target = None
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                target = m
                break
        if target is None:
            return False
        acts = target.get("actions") or []
        by = {a.get("id"): i for i, a in enumerate(acts) if a.get("id")}
        for na in actions:
            aid = na.get("id")
            if aid in by:
                acts[by[aid]] = na
            else:
                by[aid] = len(acts)
                acts.append(na)
        target["actions"] = acts
        _econvo_save_all(uid, file_rel, msgs)
        return True


def _econvo_update_action(uid, file_rel, action):
    """按 id 在所有 assistant 消息里找到这条 action,整条替换(state + 变化后的 undo/redo payload 一起存回)。"""
    aid = action.get("id")
    if not aid:
        return False
    with _econvo_lock:
        msgs = _econvo_load(uid, file_rel)
        for m in msgs:
            if m.get("role") != "assistant":
                continue
            acts = m.get("actions")
            if not acts:
                continue
            for i, a in enumerate(acts):
                if a.get("id") == aid:
                    acts[i] = action
                    _econvo_save_all(uid, file_rel, msgs)
                    return True
    return False


def _econvo_set_action_state(uid, file_rel, action_id, state):
    with _econvo_lock:
        msgs = _econvo_load(uid, file_rel)
        for m in msgs:
            if m.get("role") != "assistant":
                continue
            for a in (m.get("actions") or []):
                if a.get("id") == action_id:
                    a["state"] = state
                    _econvo_save_all(uid, file_rel, msgs)
                    return True
    return False


# ── 生成与请求解耦:detached worker(跑到完,客户端断了也不杀)+ 按 rid 缓冲事件供重连续读 ──
_echat_jobs = {}
_echat_jobs_lock = threading.Lock()


def _echat_worker(rid, message, ctx, history, force_effort, force_model, uid, file_rel):
    job = _echat_jobs[rid]
    try:
        for ev in _eagent_run(message, ctx, history, force_effort=force_effort, force_model=force_model):
            with job["lock"]:
                job["events"].append(ev)
                if ev["event"] == "answer":
                    job["answer"] = ev["data"]
                elif ev["event"] == "trace":
                    job["trace"] = ev["data"]
                elif ev["event"] == "action":   # 同步写工具的撤销/重做记录 → 随 assistant 回合落库
                    job.setdefault("actions", []).append(ev["data"])
    except Exception as e:
        with job["lock"]:
            job["events"].append({"event": "error", "data": str(e)[:160]})
    finally:
        # 先落库再发 done:保证前端收到 done(或随后异步建的高亮/任务卡)去 attach 时,assistant 消息已在存储里
        if job.get("answer"):   # 跑完就落库(断连也不丢;历史/感叹号/撤销重做卡都用得上)
            _econvo_append(uid, file_rel, "assistant", str(job["answer"])[:1500],
                           {"section": ctx.get("current_section_idx"), "trace": job.get("trace"),
                            "actions": job.get("actions")})
        with job["lock"]:
            job["events"].append({"event": "done", "data": {}})
            job["done"] = True

        def _cleanup():
            with _echat_jobs_lock:
                _echat_jobs.pop(rid, None)
        t = threading.Timer(180, _cleanup)
        t.daemon = True
        t.start()


# ──────────────────────── 路由 ────────────────────────
def _eassistant_chat():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    A = _A()
    body = request.get_json(silent=True) or {}
    uid = _uid()
    rid = (str(body.get("rid") or "").strip())[:64] or f"e{int(time.time() * 1000)}-{len(_echat_jobs)}"
    try:
        frm = max(0, int(body.get("from") or 0))
    except Exception:
        frm = 0
    ctx_in = body.get("context") or {}
    file_rel = (ctx_in.get("file") or ctx_in.get("file_rel") or "").strip()
    with _echat_jobs_lock:
        job = _echat_jobs.get(rid)
        if job is None:
            message = (body.get("message") or "").strip()
            if not message:
                if body.get("rid"):   # rid 给了但任务已被清(>3min)→ 走历史恢复(答案早落库了)
                    return jsonify({"ok": False, "error": "gone"}), 410
                return jsonify({"ok": False, "error": "empty"}), 400
            force_effort = body.get("force_effort") if body.get("force_effort") in A._EFFORTS else None
            force_model = body.get("force_model") if body.get("force_model") in A._CLAUDE_VARIANTS else None
            ctx = dict(ctx_in)
            ctx["file_rel"] = file_rel
            ctx["book_name"] = ctx.get("book") or ctx.get("book_name") or (file_rel.split("/")[-1] if file_rel else "")
            ctx["_base"] = request.host_url.rstrip("/")
            ctx["_uid"] = uid   # 写操作记 owner=本用户 → 撤销只能撤自己的
            history = [{k: m.get(k) for k in ("role", "content", "section", "book", "selection")}
                       for m in _econvo_load(uid, file_rel)[-6:]]
            _econvo_append(uid, file_rel, "user", message,   # 进 agent 前就落库 → 断连也不丢这轮
                           {"section": ctx.get("current_section_idx"), "book": ctx.get("book_name"),
                            "selection": ctx.get("selection"),
                            # 选区偏移锚 {section,start,end}(前端发送时记录)→ 历史回放里「选中」行可点跳转+临时高亮
                            "sel_anchor": (ctx.get("selection_anchor")
                                           if isinstance(ctx.get("selection_anchor"), dict) else None)})
            job = _echat_jobs[rid] = {"events": [], "answer": "", "trace": None, "done": False,
                                      "lock": threading.Lock(), "uid": uid}
            threading.Thread(target=_echat_worker, daemon=True,
                             args=(rid, message, ctx, history, force_effort, force_model, uid, file_rel)).start()
        elif job.get("uid") != uid:
            return jsonify({"ok": False, "error": "forbidden"}), 403

    def gen():
        yield f"event: meta\ndata: {json.dumps({'rid': rid})}\n\n"   # 回 rid(断线重连用);不进缓冲计数
        i = frm
        idle = 0
        while True:
            with job["lock"]:
                n = len(job["events"])
                evs = list(job["events"][i:n])
                done = job["done"]
            for ev in evs:
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
            i = n
            if done and i >= len(job["events"]):
                return
            idle = 0 if evs else idle + 1
            if idle > 2400:   # ~6min 无动静兜底收
                return
            time.sleep(0.1)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _eassistant_convo_get():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    file_rel = (request.args.get("file") or "").strip()
    return jsonify({"ok": True, "messages": _econvo_load(_uid(), file_rel)[-100:]})


def _eassistant_convo_append():
    """手动追加一条历史(可选;/api/epub-assistant 已自动持久化每轮 user+assistant,通常前端只需 GET + clear)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    file_rel = (b.get("file") or "").strip()
    content = (b.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "empty"}), 400
    role = b.get("role") or "user"
    _econvo_append(_uid(), file_rel, role, content[:4000],
                   {"section": b.get("section"), "book": b.get("book"), "selection": b.get("selection")})
    return jsonify({"ok": True})


def _eassistant_convo_clear():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    file_rel = (b.get("file") or "").strip()
    _econvo_clear(_uid(), file_rel)
    return jsonify({"ok": True})


def _eassistant_action():
    """写操作撤销/重做统一入口。body {op, file, ...}:
       op=from_task {undo_id}     → 据后台任务 undo_id 建 action(含快照)+ 附到最近 assistant 消息 → {ok, action}
       op=attach    {actions:[..]}→ 前端建好的 action(高亮)upsert 进最近 assistant 消息(幂等)→ {ok, actions}
       op=undo      {action}      → 执行逆操作(删卡/删笔记/删高亮)+ 存回新状态 → {ok, state, action}
       op=redo      {action}      → 执行正操作(重建卡/笔记/高亮)+ 存回新状态(含新 ids)→ {ok, state, action}
    纯写,不调 AI、不耗额度。"""
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    b = request.get_json(silent=True) or {}
    op = (b.get("op") or "").strip()
    uid = _uid()
    file_rel = (b.get("file") or b.get("file_rel") or "").strip()
    if op == "from_task":
        action = _build_action_from_undo((b.get("undo_id") or "").strip(), file_rel)
        if not action:
            return jsonify({"ok": False, "error": "no_task"}), 404
        _econvo_attach_actions(uid, file_rel, [action])
        return jsonify({"ok": True, "action": action})
    if op == "attach":
        acts = [a for a in (b.get("actions") or []) if isinstance(a, dict)]
        for a in acts:
            if not a.get("id"):
                a["id"] = _new_action_id()
            a.setdefault("state", "done")
            a.setdefault("ts", int(time.time()))
        stored = _econvo_attach_actions(uid, file_rel, acts)
        return jsonify({"ok": True, "stored": stored, "actions": acts})
    if op in ("undo", "redo"):
        action = b.get("action") if isinstance(b.get("action"), dict) else {}
        if not action.get("id"):
            return jsonify({"ok": False, "error": "缺 action"}), 400
        ok, err = (_action_undo(action) if op == "undo" else _action_redo(action))
        if not ok:
            return jsonify({"ok": False, "error": err or "执行失败"})   # 200:前端按 d.ok=false 提示,不当网络错
        action["state"] = "undone" if op == "undo" else "done"
        _econvo_update_action(uid, file_rel, action)   # 存回新状态 + 变化后的 payload(刷新后仍正确、可再 undo/redo)
        return jsonify({"ok": True, "state": action["state"], "action": action})
    return jsonify({"ok": False, "error": "未知 op"}), 400


def _eassistant_update_action():
    """轻量:只改某 action 的 state(撤销/重做端点已自动存状态,这是给前端的兜底/显式入口)。body {file, action_id|id, state}。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    file_rel = (b.get("file") or "").strip()
    aid = (b.get("action_id") or b.get("id") or "").strip()
    state = (b.get("state") or "").strip()
    if not aid or state not in ("done", "undone"):
        return jsonify({"ok": False, "error": "缺 action_id / state"}), 400
    ok = _econvo_set_action_state(_uid(), file_rel, aid, state)
    return jsonify({"ok": ok})


def register_epub_assistant(bp):
    """把 EPUB 助手路由挂到 pdf_reader 的蓝图上(沿用其 /pdf 前缀 + 登录守卫)。
    必须在 app.register_blueprint(bp) **之前**调(蓝图注册后不能再加 url 规则)。"""
    bp.add_url_rule("/api/epub-assistant", "epub_assistant_chat", _eassistant_chat, methods=["POST"])
    bp.add_url_rule("/api/epub-convo", "epub_assistant_convo_get", _eassistant_convo_get, methods=["GET"])
    bp.add_url_rule("/api/epub-convo/append", "epub_assistant_convo_append", _eassistant_convo_append, methods=["POST"])
    bp.add_url_rule("/api/epub-convo/clear", "epub_assistant_convo_clear", _eassistant_convo_clear, methods=["POST"])
    bp.add_url_rule("/api/epub-action", "epub_assistant_action", _eassistant_action, methods=["POST"])
    bp.add_url_rule("/api/epub-convo/update-action", "epub_assistant_update_action", _eassistant_update_action, methods=["POST"])
    return bp
