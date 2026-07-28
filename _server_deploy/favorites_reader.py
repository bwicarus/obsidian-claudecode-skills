"""favorites_reader.py — 收藏夹域(全局 sidecar + EPUB 物化 v5 + 后台预建 + PWA 入口)。

收藏夹 = 物化成一本真 EPUB(state/reader-fav-epub/<fid>.epub)用完整 EPUB 阅读器打开;
CRUD(/api/favorites)+ 结构增量(/api/fav-meta)+ 打开(/fav/open 脏→等待页/不脏→秒开)
+ 独立 PWA(/fav/manifest,/fav/icon)+ 空闲预建线程(_fav_prebuild_loop)。
设计文档:references/reader-userpages-favorites.md「二、收藏夹设计」+「统一化 v5」。

2026-07-06 结构拆分第 5 刀。依赖经 register_favorites 显式注入(同名占位,函数体从
pdf_reader.py 机械逐行搬);_reader_publish 直接 import 自 reader_events(第 1 刀产物)。
⚠ register 必须在 pdf_reader 的 _job_set/_JOBS 定义之后调用;块外仍在用的符号
(_FAV_FILE/_fav_cascade_userpage_delete/_fav_epub_raw_section/_fav_prebuild_loop)
在 register 调用后由 pdf_reader 回导入。
部署:cp 本文件到 /home/bwicarus/webapp/(跟 pdf_reader.py 同目录)+ restart webapp。
"""
import json
import os
import re
import threading as _upthr
import time as _time
import urllib.parse
import uuid as _uuid
from pathlib import Path

from flask import Response, abort, jsonify, redirect, render_template, request

from reader_events import publish as _reader_publish

# ── register_favorites 显式注入(模块 import 时为 None,注册后可用)──
CLAUDE_DIR = None            # Path: 项目根
OBSIDIAN_ROOT = None         # Path: vault 根
_FAV_BOOK_PREFIX = None      # str: "资源/收藏夹/"(合成 rel 前缀)
_FAV_EPUB_DIR = None         # Path: state/reader-fav-epub/(物化产物;定义住 EPUB 域)
_EPUB_EXTRACT_DIR = None     # Path: EPUB 解包缓存根
_EPUB_OPF_CACHE = None       # dict: OPF 解析缓存(跨域共享,直接引用同一对象)
_safe_vault_path = None      # callable
_epub_sha = None             # callable: rel → sha
_epub_opf_info = None        # callable: 解包根 → OPF 信息
_epub_section_cached = None  # callable: 消毒缓存版 section html
_epub_rewrite_url = None     # callable: section 内资源 URL 重写
_ensure_epub_extracted = None  # callable: rel → 解包根
_epub_js_v = None            # callable: EPUB 静态 cache-bust
_upages_load = None          # callable: 书 rel → 用户页 sidecar dict
_upages_path = None          # callable: 书 rel → 用户页 sidecar Path
_up_md_html = None           # callable: 用户页 md → 显示 HTML
_INSPAGE_MUTEX = None        # threading.Lock: PDF 真插页互斥(跨域共享对象)
_INSPAGE_ACTIVE = None       # dict: 进行中的 PDF 插页
_page_chars_cached = None    # callable: PDF 页字符层(收藏 PDF 页自定义选区用)
_ink_load = None             # callable: PDF 墨迹 sidecar 读
_job_set = None              # callable: 后台 job 状态
# register 里从 claude_dir 派生:
_FAV_FILE = None             # Path: state/reader-favorites.json
_FAV_HTML_DIR = None         # Path: state/reader-fav-html(【退役】v4 产物,仅清理残留用)
_FAV_META_DIR = None         # Path: state/reader-fav-meta(AI 认收藏集元数据)

# ── 收藏夹(全局 sidecar:state/reader-favorites.json;设计:references/reader-userpages-favorites.md「二、收藏夹设计」)──
# 收藏夹 = 一本"虚拟书":夹内条目指向各原书的某页(PDF)/某章(EPUB);查看页(/pdf/fav/view)只读原书资源,
# **零进度状态**(不 _lastopen_touch、不写 LS.pos)。阶段A:数据模型 + CRUD + ⭐picker + 书架 tab + 查看页(即时类功能)。


def _fav_load() -> dict:
    try:
        d = json.loads(_FAV_FILE.read_text("utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("folders"), list):
            return {"folders": []}
        return d
    except Exception:
        return {"folders": []}


def _fav_save(d: dict):
    _FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FAV_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    tmp.replace(_FAV_FILE)


def _fav_folder(d: dict, fid: str):
    return next((f for f in (d.get("folders") or []) if f.get("id") == fid), None)


def _fav_norm_item(raw):
    """规整条目 {file, kind:'pdf'|'epub'|'userpage', page|section|id};非法 → None。
    page=PDF 页(1-based)/section=spine idx(0-based)/id=用户页(插入页)记录 id(u_<hex>,file=该插入页所属书 rel)。"""
    if not isinstance(raw, dict):
        return None
    kind = (raw.get("kind") or "").strip()
    if kind == "video":   # 阶段D:收藏视频(YouTube 11 位 id / Bilibili bvid=BV+10=12 位);无 file → 用 vid 作合成 key
        vid = (raw.get("vid") or "").strip()
        is_bv = bool(re.match(r"^BV[0-9A-Za-z]{10}$", vid))   # B站 bvid(12 位)
        if not (re.match(r"^[A-Za-z0-9_-]{11}$", vid) or is_bv):   # 两种都放行(原来只认 11 位 → B站 12 位被判非法)
            return None
        _src = (raw.get("src") or "").strip()
        src = "bili" if (_src == "bili" or (not _src and is_bv)) else "yt"   # 显式 src 优先,无则按 bvid 兜底
        return {"file": "video:" + vid, "kind": "video", "vid": vid, "src": src,
                "title": (raw.get("title") or "")[:200], "thumb": (raw.get("thumb") or "")[:300]}
    rel = (raw.get("file") or "").strip()
    if not rel or ".." in rel or kind not in ("pdf", "epub", "userpage"):
        return None
    if rel.startswith(_FAV_BOOK_PREFIX):
        return None   # 禁止收藏「收藏夹物化书」自身/其内插入页(防收藏集套收藏集递归;规格 D 后端双保险)
    it = {"file": rel, "kind": kind}
    try:
        if kind == "pdf":
            it["page"] = max(1, int(raw.get("page")))
        elif kind == "epub":
            it["section"] = max(0, int(raw.get("section")))
        else:   # userpage:自己创建的插入页(userpages sidecar 该 id 的正文 md 会被物化成一个 section)
            uid = (raw.get("id") or "").strip()
            if not re.match(r"^u_[0-9a-zA-Z]+$", uid):
                return None
            it["id"] = uid
    except (TypeError, ValueError):
        return None
    return it


def _fav_same_item(a: dict, b: dict) -> bool:
    return (a.get("file") == b.get("file") and a.get("kind") == b.get("kind")
            and a.get("page") == b.get("page") and a.get("section") == b.get("section")
            and a.get("id") == b.get("id"))   # userpage 用 id 判重(pdf/epub 的 id 恒 None → 不影响原判重)


# ── 收藏夹统一化 规格 v5:物化成「一本真 EPUB」(state/reader-fav-epub/<fid>.epub),用**完整 EPUB 阅读器**打开 ──
#   【重大调整 2026-07-04·用户拍板「要全功能」】v4 曾物化成流式 HTML 片段(state/reader-fav-html/<fid>.html)+
#   轻量 html-reader.js —— 功能少(无手写/侧栏AI助手/图徽标/语法/生词)。改为**收藏夹=一本真 EPUB**:标准 zip
#   (mimetype+container.xml+OPF(manifest/spine)+各条目一个 XHTML section+图片打包),epub-html.js 当普通 EPUB 打开
#   → 选词/查词/AI/侧栏助手/高亮/手写/生词/振假名/语法/插入页 **全功能天然可用**(对它就是一本 EPUB)。
#   · EPUB 条目 = 原 section 消毒 HTML(图打包进 zip,img src 指 zip 内相对路径);
#   · PDF  条目 = 原分辨率页图(打包)+ 透明可选文字层(复用 _fav_pdf_overlay_spans %定位/cqh → EPUB reflow 里也能选词);
#   · 条目间分隔条(《书名》·页/章 + 打开原书深链)。
#   产物放 state(非 vault → 无 Obsidian Sync churn、天然不进书架/搜索);epub 端点用 _resolve_epub_book 把合成 rel
#   「资源/收藏夹/<fid>.epub」解析回 state 那本;section 端点对收藏夹前缀走 raw(不消毒,保住透明词层行内 style + 站内链接)。
#   真源仍是 favorites.json 的 item 列表;加/删/改 item → content_sig 变 → 脏 → CRUD 后台自动重建 +(打开时兜底)。
#   命名按 fid:改名只改 name(高亮/墨迹 sidecar 键=合成 rel 资源/收藏夹/<fid>.epub 的 sha,与 fid 绑死,零孤儿)。
_FAV_LOCK = _upthr.Lock()               # favorites.json 的 RMW 串行化(CRUD ⇄ build job 写 built_sig 防丢更新)
_FAV_BUILD_ACTIVE: dict = {}            # fid -> jid(同夹重建进行中复用同一 job,去重)
_FAV_BUILD_VER = 13                      # v13(2026-07-05):来源条加「☆ 取消收藏」按钮(data-fitem → PATCH remove_item)+ 页间距紧凑(.fav-sep margin 22/14→8/5、.fav-item 8→2)。v12(2026-07-05):EPUB 收藏的插入页正文也双向——md 显示层包 .fav-up-disp,前端「✏️ 编辑」拉原书 md → textarea → PATCH /api/userpages 写回原书 → 就地重渲(原书→fav 经 content_sig 折 md 触发重建)。v11:墨迹实时绑定原书(data-uid+data-ink-file 双向读写)。v9:PDF 页图 per-figure 徽标(.fav-pdf-page 带 data-favpdf-*,前端 fetch page-figures)。v5=真 EPUB + 完整 EPUB 阅读器;bump → 存量夹视为脏,下次打开/变更自动重建
                                         # v6(2026-07-04):PDF 条目透明词层从「散布 absolute span」改「按视觉行分组 .fav-pdf-line 行盒 + 行内 inline-block 词」→ 修收藏夹长按选中乱跨上下多行(存量夹重建;文字节点顺序不变 → 高亮/便签 offset 锚不受影响)
                                         # v7(2026-07-04):PDF 条目透明词层从「原生 DOM Selection」改「char-layer 式自定义选择」——逐字照搬 PDF 阅读器 reader.src/13-selection.js::_bindCharLayer:词层 user-select:none 彻底关原生选区(iOS 长按拖选引擎压根不启动 → 根治乱跨行),epub-html.js 自建"拖选=按词 bbox 圈范围 → 自绘高亮 .fav-pdf-sel → 手动组 cur + showSel"(与 char-layer 手动开 toolbar 同构;⚠ user-select:none 下 addRange 后 getSelection 恒空,故手动 cur,text/anchor 用既有 offsetOf/_countableText 算=高亮/便签同口径;document capture 相拦 content 的 mouseup/touchend→captureSel 防误清 toolbar,零改 captureSel)。CSS 加 .fav-pdf-sel 自绘高亮层 + 词层 user-select:none(存量夹重建;HTML 结构不变 → offset 锚不受影响)
                                         # v8(2026-07-04):build 顺带写 state/reader-fav-meta/<fid>.json(每条目=一 section:出处 src_file/src_name/src_page|src_section + 首句 snippet + 相邻 adj_prev + missing)。给 EPUB 侧栏助手「认收藏集(system prompt 声明)+ 目录概览 + 相邻性(不把无关两条当连续上下文)+ read_source_page 翻原书」用(见 epub_assistant.py + references/reader-userpages-favorites.md「C. AI 集成」)。EPUB 产物字节不变、offset 锚不受影响,bump 仅为存量夹重建补 meta。


def _fav_html_path(fid: str) -> Path:
    """【退役】v4 流式 HTML 产物路径(仅用于重建/删夹时清理残留)。"""
    return _FAV_HTML_DIR / (fid + ".html")


def _fav_epub_path(fid: str) -> Path:
    """v5 物化产物:一本真 EPUB(state,非 vault)。"""
    return _FAV_EPUB_DIR / (fid + ".epub")


# ── 收藏集 AI 元数据(v8):build 顺带写,给 EPUB 侧栏助手认收藏集 + 目录概览 + 相邻性 + 翻原书 ──


def _fav_meta_path(fid: str) -> Path:
    return _FAV_META_DIR / (fid + ".json")


def _fav_meta_load(fid: str) -> dict:
    """读收藏集 AI 元数据(每条目=一 section:出处/首句/相邻 adj_prev/missing)。缺失/损坏 → {}。
    epub_assistant.py 经 _pdf()._fav_meta_load(fid) 取用:识别收藏集、组目录概览、判相邻、read_source_page 翻原书。"""
    try:
        m = json.loads(_fav_meta_path(fid).read_text("utf-8"))
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _fav_epub_rel(fid: str) -> str:
    """EPUB 阅读器/高亮/墨迹 sidecar 用的**合成 rel 键**(资源/收藏夹/<fid>.epub;不是 vault 文件)。
    _resolve_epub_book 认这个前缀 → 解析回 state 里的真 .epub;与 fid 绑死(改名零孤儿)。"""
    return _FAV_BOOK_PREFIX + fid + ".epub"


def _fav_view_rel(fid: str) -> str:
    """收藏夹书打开时传给 epub_html_reader.html 的 file_rel = 合成 EPUB rel。"""
    return _fav_epub_rel(fid)


def _fav_book_rel(fid: str) -> str:
    """【退役】旧 v3 固定页 PDF 产物路径(仅用于重建时删除残留 + 删夹清理)。"""
    return _FAV_BOOK_PREFIX + fid + ".pdf"


def _fav_book_abs(fid: str) -> Path:
    return OBSIDIAN_ROOT / _fav_book_rel(fid)


def _fav_content_sig(items) -> str:
    """items 列表的稳定哈希(收藏内容指纹)。sort_keys 保证字段序无关,内容不变则 sig 不变。
    userpage 条目**额外**把该插入页当前 md/title 版本折进指纹 → 用户编辑了收藏的插入页,sig 变 → 下次打开脏重建
    (userpage 编辑不经收藏夹 CRUD,靠 /fav/open 的 _fav_is_dirty 兜底重建)。**非 userpage 折算前后字节完全一致**
    (enriched==items)→ 存量夹 built_sig 不受影响,零无谓重建。"""
    import hashlib
    enriched = []
    _cache: dict = {}
    for it in (items or []):
        if isinstance(it, dict) and it.get("kind") == "userpage":
            rel = it.get("file") or ""
            uid = it.get("id") or ""
            recs = _cache.get(rel)
            if recs is None:
                recs = {r.get("id"): r for r in _upages_load(rel) if isinstance(r, dict)}
                _cache[rel] = recs
            r = recs.get(uid) or {}
            enriched.append({"file": rel, "kind": "userpage", "id": uid,
                             "v": [r.get("md_ver"), r.get("updated"), r.get("title"), r.get("md")]})
        else:
            enriched.append(it)
    try:
        s = json.dumps(enriched, sort_keys=True, ensure_ascii=False)
    except Exception:
        s = repr(enriched)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _fav_mark_dirty(folder: dict):
    """items 变更后调:刷新 content_sig(!= built_sig 即脏)。不立即 build(打开时兜底重建,零无谓 churn)。"""
    folder["content_sig"] = _fav_content_sig(folder.get("items") or [])


def _fav_is_dirty(folder: dict, fid: str) -> bool:
    """脏 = 内容指纹 != 上次 build 用的指纹,或物化逻辑版本升级(v4 流式 HTML → v5 真 EPUB),
    或 EPUB 产物不存在(首次打开)。"""
    if _fav_content_sig(folder.get("items") or []) != folder.get("built_sig"):
        return True
    if folder.get("built_ver") != _FAV_BUILD_VER:   # 物化逻辑升级 → 旧产物视为脏(下次打开/变更时重建成 EPUB)
        return True
    return not _fav_epub_path(fid).exists()


def _fav_epub_label(toc: list, section: int) -> str:
    """EPUB 章名:toc 里 idx<=section 的最近 label(照阶段A fav-reader 逻辑)。"""
    best = ""
    for e in (toc or []):
        try:
            if int(e.get("idx")) <= section:
                best = e.get("label") or best
        except (TypeError, ValueError):
            continue
    return best


# ── 收藏夹 EPUB 装配(v5)──────────────────────────────────────────────────────────────
# 产物 = 一本真 EPUB(zip:mimetype+container.xml+OPF+nav+各条目一 XHTML section+图片打包)。分隔条 + 内容原大小流式。
# EPUB 阅读器(epub-html.js)当普通 EPUB 打开;section 端点对收藏夹前缀走 raw(不消毒,保住透明词层行内 style + 站内链接)。
_FAV_PDF_IMG_W = 1520   # PDF 页图渲染宽度(px,~2× 阅读列宽,清晰;显示按列宽等比缩,高度自然不压)


def _fav_esc(s) -> str:
    import html as _h
    return _h.escape("" if s is None else str(s))


def _fav_sep_html(label: str, href: str, item=None) -> str:
    """条目分隔条:来源标签 + 「打开原书 ↗」深链 + (item 非 None 时)「☆ 取消收藏」按钮
    (data-fitem=条目 JSON,前端 PATCH remove_item → 乐观隐藏本节 + 后台重建 reconcile 收尾)。
    fav.css 给 .fav-sep 上 user-select:none;「打开原书」是站内 <a>(tap 处理器对 closest('a') 让位)。
    (2026-07-06 清理:原 _fav_sep_html3 更名回来,两行包装删除)"""
    btn = ""
    if isinstance(item, dict):
        try:
            btn = '<button type="button" class="fav-unfav" data-fitem="%s" title="取消收藏这一页(不影响原书)">☆ 取消收藏</button>' \
                  % _fav_esc(json.dumps({k: item.get(k) for k in ("file", "kind", "page", "section", "id") if item.get(k) is not None},
                                        ensure_ascii=False))
        except Exception:
            btn = ""
    return ('<div class="fav-sep"><span class="fav-sep-t">%s</span>'
            '<a class="fav-open-src" href="%s">打开原书 ↗</a>%s</div>'
            % (_fav_esc(label), _fav_esc(href), btn))


def _fav_pdf_overlay_spans(chars, page_w, page_h) -> str:
    """把 page-chars(PDF 点坐标)转成透明可选词层。**按视觉行分组**(2026-07-04 修「长按选中乱跨上下多行」):
    先把相邻同 w 字合成词,再把同一视觉行(同 bk 块/列 + 竖直中心相近)的词包进一个 `.fav-pdf-line` 行容器
    (absolute 定位在行 bbox);行内各词用 `display:inline-block` + margin-left(词间距)/ width(词宽)按**行宽百分比
    在正常行内流里**排布 → 浏览器建出真实行盒,原生选区(尤其 iOS Safari 长按/拖选)按行自然收敛,
    不再像**散布 absolute span**(旧实现:每词一个 position:absolute 直挂 .fav-pdf-txt,无行结构)那样整块乱选
    上下多行(iOS 对嵌套 absolute span 的选区引擎尤其脆弱,同 pdf.js #14243/#20017;`-webkit-text-size-adjust:none` 亦是其官方缓解)。
    行容器 left/top/width/height 用 %(随页图等比缩放),font-size 用 cqh(容器高度 %)→ 免 JS 响应式;
    词 margin-left/width 用 %(相对行宽,即行容器宽)→ 与页图逐词对齐(选中高亮贴合原字)。
    英/数字词加尾随空格(多词选中拼出空格,copy/翻译干净);CJK 无空格。
    v7:词层 user-select:none,拖选走 epub-html.js 自建 char-layer 式自定义选择(按 span bbox 圈范围→自绘 .fav-pdf-sel 高亮
    →手动 cur+showSel,照 reader.src/13-selection.js;根治 iOS 长按乱跨行);单击查词仍走 caretRangeFromPoint(none 下照常)。
    文字节点顺序仍是 reading order(行序×词序)→ EPUB 侧 offset 锚/分词/高亮口径与旧实现一致(存量夹重建后偏移不变)。"""
    if not chars or not page_w or not page_h or page_w <= 0 or page_h <= 0:
        return ""
    # 1) 相邻同 w 合并为词(英文单词/日语 fugashi token;CJK 单字各自 w → 单字词可独立选)——保留 bk(块/列)+ bbox
    words = []
    i, n = 0, len(chars)
    while i < n:
        c = chars[i]
        if c.get("sp"):
            i += 1
            continue
        w = c.get("w"); bk = c.get("bk")
        text = c.get("c") or ""
        x0 = c.get("x0"); y0 = c.get("y0"); x1 = c.get("x1"); y1 = c.get("y1")
        j = i + 1
        while j < n and (not chars[j].get("sp")) and chars[j].get("w") == w:
            cj = chars[j]
            # 行感知合并:同 w 但下一字竖直中心明显错开当前词的 y 带 → 断开(不让词跨行)。
            # 防 w 退化成 -1(word-id 查不中,如某些符号/退化提取)时把上一行末词与下一行首词粘成一个跨行 token
            # → 撑高行盒、行盒相互重叠 → 选区又跨行(旧散布 span 版同 bug,一并根治)。真实词各字同行,此 guard 从不误伤。
            try:
                cyc = (float(cj.get("y0")) + float(cj.get("y1"))) / 2.0
                if y0 is not None and y1 is not None and (cyc < float(y0) - 0.5 or cyc > float(y1) + 0.5):
                    break
            except (TypeError, ValueError):
                pass
            text += cj.get("c") or ""
            try:
                x0 = min(x0, cj.get("x0")); y0 = min(y0, cj.get("y0"))
                x1 = max(x1, cj.get("x1")); y1 = max(y1, cj.get("y1"))
            except (TypeError, ValueError):
                pass
            j += 1
        i = j
        text = (text or "").strip()
        if not text or x0 is None or y0 is None or x1 is None or y1 is None:
            continue
        try:
            words.append({"t": text, "x0": float(x0), "y0": float(y0),
                          "x1": float(x1), "y1": float(y1), "bk": bk})
        except (TypeError, ValueError):
            continue
    if not words:
        return ""
    # 2) 按视觉行分组(reading order):同 bk 且竖直中心落在当前行带内(±0.6×行高)→ 同一行;否则起新行
    #    (bk 隔开并排气泡/多列;竖直中心避免上下行合并)
    lines = []
    cur = None
    for wd in words:
        cy = (wd["y0"] + wd["y1"]) / 2.0
        h = wd["y1"] - wd["y0"]
        if cur is not None and wd["bk"] == cur["bk"] and abs(cy - cur["cy"]) <= max(h, cur["h"]) * 0.6:
            cur["words"].append(wd)
            cur["cy"] = (cur["cy"] * cur["n"] + cy) / (cur["n"] + 1)
            cur["n"] += 1
            cur["h"] = max(cur["h"], h)
        else:
            cur = {"bk": wd["bk"], "cy": cy, "h": h, "n": 1, "words": [wd]}
            lines.append(cur)
    # 3) 每行一个 .fav-pdf-line 行盒(absolute 定位在行 bbox);行内词 inline-block 在正常行内流里排(真实行盒 → 选区按行收敛)
    out = []
    for ln in lines:
        ws = ln["words"]
        Lx = min(wd["x0"] for wd in ws); Rx = max(wd["x1"] for wd in ws)
        Ty = min(wd["y0"] for wd in ws); By = max(wd["y1"] for wd in ws)
        lw = Rx - Lx; lh = By - Ty
        if lw <= 0 or lh <= 0:
            continue
        Lp = Lx / page_w * 100.0; Tp = Ty / page_h * 100.0
        Wp = lw / page_w * 100.0; Hp = lh / page_h * 100.0
        fs = max(Hp, 0.4)
        inner = []
        prev_r = Lx
        for wd in ws:
            gap = (wd["x0"] - prev_r) / lw * 100.0   # 与前一词的间距(相对行宽)= inline-block margin-left
            if gap < 0:
                gap = 0.0
            ww = (wd["x1"] - wd["x0"]) / lw * 100.0   # 词宽(相对行宽)= inline-block width → 逐词与页图对齐
            prev_r = wd["x1"]
            t = wd["t"]
            # 拉丁词/数字加**span 内尾随空格**(overflow:hidden 下不撑宽 → 视觉不动,但多词选中/copy 拼出空格,翻译干净);CJK 不加
            if re.search(r"[A-Za-z0-9]", t):
                t = t + " "
            inner.append('<span style="margin-left:%.3f%%;width:%.3f%%">%s</span>'
                         % (gap, ww, _fav_esc(t)))
        out.append('<div class="fav-pdf-line" style="left:%.3f%%;top:%.3f%%;width:%.3f%%;height:%.3f%%;font-size:%.3fcqh">%s</div>'
                   % (Lp, Tp, Wp, Hp, fs, "".join(inner)))
    return "".join(out)


# ── v5 真 EPUB 装配原语 ──────────────────────────────────────────────────────────────
_FAV_CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
    '</container>\n')

# 收藏夹 EPUB 自带 CSS(打包进 zip,/api/epub-css scope 到 #ep-col 后注入 #ep-book-css)。分隔条 + PDF 透明词层。
_FAV_EPUB_CSS = (
    ".fav-sep{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:8px 0 5px;padding:4px 10px;"
    "border-radius:8px;background:rgba(120,150,200,.10);border:1px solid rgba(120,150,200,.22);"
    "font-size:.82em;color:#5a6a86;user-select:none;-webkit-user-select:none}\n"
    ".fav-sep-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}\n"
    ".fav-open-src{flex:none;text-decoration:none;white-space:nowrap;font-weight:600}\n"
    ".fav-item{margin:0 0 2px}\n"
    ".fav-unfav{flex:none;background:transparent;border:1px solid rgba(120,140,190,.45);border-radius:999px;padding:2px 10px;font-size:11px;color:inherit;opacity:.75;cursor:pointer;white-space:nowrap;-webkit-user-select:none;user-select:none;touch-action:manipulation}\n"
    ".fav-unfav:active{opacity:1;transform:scale(.95)}\n"
    ".fav-pdf-page{position:relative;container-type:size;width:100%;margin:0 auto;background:#fff;"
    "border:1px solid rgba(0,0,0,.10);border-radius:4px;overflow:hidden}\n"
    ".fav-pdf-img{display:block;width:100%;height:auto}\n"
    ".fav-pdf-txt{position:absolute;left:0;top:0;right:0;bottom:0}\n"
    # 行盒:absolute 定位在视觉行 bbox;white-space:nowrap 让行内词单行排布(词层与页图逐词对齐);
    # v7:词层 user-select:none 关掉原生选区(iOS 长按拖选引擎对绝对/嵌套词层乱跨行的根因)→ epub-html.js
    #   自建 char-layer 式自定义拖选(自绘高亮 + 手动组 cur+showSel,照 reader.src/13-selection.js)。单击查词仍走 caretRangeFromPoint(none 下照常)
    ".fav-pdf-txt .fav-pdf-line{position:absolute;white-space:nowrap;color:transparent;line-height:1;"
    "cursor:text;user-select:none;-webkit-user-select:none;-webkit-text-size-adjust:none}\n"
    # 词:inline-block 在行内正常流里,width=词宽 / margin-left=词间距(均相对行宽)→ 逐词与页图对齐;overflow:hidden 裁掉尾随空格视觉
    ".fav-pdf-txt .fav-pdf-line span{display:inline-block;overflow:hidden;vertical-align:top;white-space:pre;"
    "cursor:text;user-select:none;-webkit-user-select:none}\n"
    # v7 自绘选中高亮层(char-layer 的 .sel-overlay 等价物):pointer-events:none 让单击 caretRangeFromPoint 穿透到词层;
    #   epub-html.js 拖选时按选中词 bbox 画 .hl,起新选/点别处清空
    ".fav-pdf-page .fav-pdf-sel{position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:4}\n"
    ".fav-pdf-page .fav-pdf-sel .hl{position:absolute;background:rgba(90,150,255,.32);border-radius:2px}\n"
    ".fav-fig-badge{position:absolute;transform:translate(-50%,-50%);width:26px;height:26px;border-radius:50%;z-index:6;cursor:pointer;display:flex;align-items:center;justify-content:center;background:rgba(28,28,38,.5);color:#fff;box-shadow:0 1px 5px rgba(0,0,0,.35);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}\n"
    ".fav-fig-badge:active{transform:translate(-50%,-50%) scale(.88)}\n"
    ".fav-fig-badge svg{width:15px;height:15px;display:block}\n"
    ".fav-item-userpage.fav-up-hasink{position:relative;min-height:86vh}\n"   # 与阅读器 .ep-usec 同高 → 手写不竖向失真
    ".fav-up-ink{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2;overflow:visible}\n"
    ".fav-up-content{position:relative;z-index:1}\n"
    ".fav-item-userpage[data-uid]{position:relative;min-height:86vh}\n"   # EPUB 实时绑定原书墨迹的自建页:页体 = 原书 .ep-usec 同 86vh 几何(墨迹坐标对齐)+ position:relative 给墨迹 canvas 定位
    # 收藏夹自建页正文双向:✏️ 编辑按钮 + 全屏 textarea 覆盖层(iOS:整条链 user-select:text)
    ".fav-up-editbtn{position:absolute;left:10px;top:10px;z-index:13;height:28px;padding:0 12px;display:inline-flex;align-items:center;justify-content:center;background:rgba(255,255,255,.8);-webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px);border:.5px solid rgba(0,0,0,.16);border-radius:9px;font:600 13px/1 -apple-system,system-ui,sans-serif;color:#1d1d1f;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);-webkit-user-select:none;user-select:none;touch-action:manipulation}\n"
    ".fav-up-editbtn:active{transform:scale(.95)}\n"
    ".fav-item-userpage.fav-up-editing .fav-up-disp{display:none}\n"
    ".fav-up-edit{position:absolute;inset:0;z-index:20;display:flex;flex-direction:column;background:var(--pa,#f6f3ea);border-radius:9px;-webkit-user-select:text;user-select:text}\n"
    ".fav-up-edit .fav-up-ta{flex:1 1 auto;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;padding:16px 8%;font:15px/1.7 ui-monospace,Menlo,Consolas,monospace;background:var(--pa,#f6f3ea);color:var(--ink,#1b1b1b);-webkit-text-fill-color:var(--ink,#1b1b1b);caret-color:var(--lnk,#2a5db0);-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}\n"
    ".fav-up-editbar{flex:0 0 auto;display:flex;justify-content:flex-end;gap:10px;padding:8px 8% 12px;-webkit-user-select:none;user-select:none}\n"
    ".fav-up-editbar button{background:var(--lnk,#2a5db0);color:#fff;border:none;border-radius:8px;padding:6px 16px;font-size:14px;cursor:pointer;touch-action:manipulation}\n"

    ".fav-missing{padding:14px 16px;border-radius:8px;background:rgba(200,120,120,.10);"
    "border:1px dashed rgba(200,120,120,.35);color:#8a5a5a;font-size:.9em}\n"
    ".fav-item-userpage{padding:2px 0 6px}\n"                       # 「我的页」正文 = 用户 markdown 渲染(公式/列表/标题)
    ".fav-item-userpage img{max-width:100%;height:auto}\n"
    ".fav-up-empty{opacity:.5;font-style:italic}\n")


def _fav_img_mt(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "svg": "image/svg+xml"}.get(e, "application/octet-stream")


def _fav_img_wh(data: bytes):
    """图片真实像素尺寸(补进 <img> width/height 防 reflow 抽搐)。无 PIL / 失败 → None。"""
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(data)) as im:
            return (int(im.width), int(im.height)) if (im.width and im.height) else None
    except Exception:
        return None


def _fav_xhtml(title: str, body: str) -> str:
    """一条目 → 一个 XHTML section(body 含分隔条 + 内容;body 里有 PDF 透明词层的行内 % 定位 → 必须用
    字符串拼接,**不能** %-format,否则 % 被当格式符)。link 引 fav.css 供其它阅读器用(本站 CSS 走 /api/epub-css)。"""
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/><title>'
            + _fav_esc(title) + '</title><link rel="stylesheet" type="text/css" href="style/fav.css"/></head><body>'
            + (body or "") + '</body></html>\n')


def _fav_epub_raw_section(sp: Path, root: Path, sha: str) -> str:
    """收藏夹物化 EPUB 的章节读取:服务端亲手生成的可信 HTML,**不消毒**——保住 PDF 透明词层的行内 style
    与「打开原书」站内链接(_sanitize_epub_section 会剥掉);只把 zip 内相对 img src 改写成 /pdf/epub/file/<sha>/… 代理 URL。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(sp.read_text("utf-8", "ignore"), "html.parser")
    body = soup.find("body") or soup
    sec_dir = sp.parent
    for im in body.find_all("img"):
        s = im.get("src")
        if s:
            im["src"] = _epub_rewrite_url(s, sec_dir, root, sha)
    return body.decode_contents()


def _fav_pdf_page_jpg(ap: Path, page: int, w: int):
    """渲原书某页为 JPEG 字节(打包进收藏夹 EPUB)。只读原书。失败/越界 → None。"""
    import fitz
    d = None
    try:
        d = fitz.open(str(ap))
        if page < 1 or page > d.page_count:
            return None
        p = d[page - 1]
        zoom = w / max(1.0, p.rect.width)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("jpg", jpg_quality=82)
    except Exception:
        return None
    finally:
        try:
            if d:
                d.close()
        except Exception:
            pass


def _fav_epub_pack_item(i: int, html: str, source_root: Path, source_sha: str):
    """EPUB 条目消毒 HTML 里的 <img>(代理 URL 指向 source 解包目录)→ 读字节打包,src 改成 zip 内相对路径 img/…。
    返回 (rewritten_html, [(arc_under_OEBPS, bytes, media_type), …])。读不到的图 decompose 跳过(不中断)。"""
    from bs4 import BeautifulSoup
    from urllib.parse import unquote
    soup = BeautifulSoup(html or "", "html.parser")
    imgs_out = []
    n = 0
    prefix = "/pdf/epub/file/%s/" % source_sha
    src_root_res = source_root.resolve()
    for im in list(soup.find_all("img")):
        src = im.get("src") or ""
        data = None
        ext = ".img"
        if src.startswith(prefix):
            relp = unquote(src[len(prefix):].split("?")[0].split("#")[0])
            try:
                ip = (source_root / relp).resolve()
                ip.relative_to(src_root_res)   # 防越界
                if ip.is_file() and ip.stat().st_size <= 12 * 1024 * 1024:
                    data = ip.read_bytes()
                    ext = ip.suffix.lower() or ".img"
            except Exception:
                data = None
        if data is None:
            im.decompose()   # data: URI / http / 读不到 → 跳过(收藏夹 EPUB 自包含,只打包能读到的本地图)
            continue
        n += 1
        arc = "img/ep%03d_%02d%s" % (i, n, ext if ext.startswith(".") else "." + ext)
        imgs_out.append((arc, data, _fav_img_mt(ext)))
        im["src"] = arc                 # zip 内相对路径(相对 OEBPS/sec_N.xhtml)
        wh = _fav_img_wh(data)
        if wh:
            im["width"] = str(wh[0])
            im["height"] = str(wh[1])
    return soup.decode_contents(), imgs_out


def _fav_pdf_item(i: int, it: dict, src_name: str, warns: list):
    """PDF 条目 → (label, section_body_html, [(img_arc, bytes, mt)])。原书只读:字符层 + 页图。"""
    frel = it.get("file") or ""
    pno = int(it.get("page") or 1)
    label = "《%s》 · 第 %d 页" % (src_name, pno)
    href = "/pdf/view?file=" + urllib.parse.quote(frel) + "&page=%d" % pno
    sep = _fav_sep_html(label, href, it)
    ap = _safe_vault_path(frel)
    imgs = []
    body_inner = None
    if ap and ap.suffix.lower() == ".pdf":
        try:
            res = _page_chars_cached(ap, frel, pno)   # 只读原书:字符层 + 页尺寸(pt)
            jpg = _fav_pdf_page_jpg(ap, pno, _FAV_PDF_IMG_W)   # 只读原书:渲页图字节 → 打包
            if res is not None and jpg:
                chars, pw, ph, _fg = res
                if pw and ph and pw > 0 and ph > 0:
                    arc = "img/pdf_%03d.jpg" % i
                    imgs.append((arc, jpg, "image/jpeg"))
                    spans = _fav_pdf_overlay_spans(chars, pw, ph)
                    # aspect-ratio 用 page-chars 页尺寸(与页图同坐标系)→ 透明词层与图对齐;reflow 里随列宽等比缩
                    body_inner = ('<div class="fav-item fav-item-pdf"><div class="fav-pdf-page tex2jax_ignore" data-favpdf-file="%s" data-favpdf-page="%d" style="aspect-ratio:%g/%g">'
                                  '<img class="fav-pdf-img" alt="第%d页" src="%s"/><div class="fav-pdf-txt">%s</div></div></div>'
                                  % (_fav_esc(frel), pno, pw, ph, pno, arc, spans))   # data-favpdf-*:前端据此 fetch 原书 page-figures 渲 per-figure 徽标(复用原本判定+内容)
                    if not spans:
                        warns.append("第 %d 条(%s 第%d页)无字符层,选词不可用(可点『打开原书』)" % (i + 1, src_name, pno))
        except Exception:
            body_inner = None
    if body_inner is None:
        body_inner = ('<div class="fav-item fav-missing">《%s》第 %d 页:原书已移动/删除或页码越界。'
                      '<br>点上方「打开原书 ↗」可查看。</div>' % (_fav_esc(src_name), pno))
        warns.append("第 %d 条(%s 第%d页)原书不可用" % (i + 1, src_name, pno))
    return label, sep + body_inner, imgs


def _fav_epub_item(i: int, it: dict, src_name: str, warns: list):
    """EPUB 条目 → (label, section_body_html, [(img_arc, bytes, mt)])。原书只读:消毒 HTML + 打包图。"""
    frel = it.get("file") or ""
    idx = int(it.get("section") or 0)
    ap = _safe_vault_path(frel)
    html = None
    label = ""
    imgs = []
    if ap and ap.suffix.lower() == ".epub":
        try:
            root = _ensure_epub_extracted(ap, frel)
            if root:
                info = _epub_opf_info(root)
                secs = info["sections"]
                label = _fav_epub_label(info.get("toc") or [], idx)
                if 0 <= idx < len(secs):
                    src_sha = _epub_sha(frel)
                    src_html = _epub_section_cached(secs, idx, root, src_sha)   # 只读原书:消毒章节
                    html, imgs = _fav_epub_pack_item(i, src_html, root, src_sha)
        except Exception:
            html = None
            imgs = []
    if not label:
        label = "《%s》 · 第 %d 节" % (src_name, idx + 1)
    else:
        label = "《%s》 · %s" % (src_name, label)
    href = "/pdf/epub/view?file=" + urllib.parse.quote(frel) + "&sec=%d" % idx
    sep = _fav_sep_html(label, href, it)
    if html is not None:
        body_inner = '<div class="fav-item fav-item-epub">' + html + "</div>"
    else:
        body_inner = ('<div class="fav-item fav-missing">《%s》第 %d 节:原书已移动/删除或章节越界。</div>'
                      % (_fav_esc(src_name), idx + 1))
        warns.append("第 %d 条(%s 第%d节)不可用" % (i + 1, src_name, idx + 1))
    return label, sep + body_inner, imgs


# ── 用户页(自己创建的插入页)条目物化 ─────────────────────────────────────────────────
#   收藏条目 {file, kind:'userpage', id} → 读 file 所属书的 userpages sidecar 该 id 的**正文 md**(只读,不改插入页)
#   → RC.md 式 markdown→HTML(公式 $..$ 留给 MathJax、列表/标题/图/链接)→ 一个 section。手写墨迹本批不收
#   (canvas 运行时数据嵌静态 EPUB 困难);只物化正文 md(文字/公式/图)。记录不存在(被删)→ 占位 section 不中断。
def _fav_userpage_href(rel: str, rec: dict) -> str:
    """「打开原书」深链:插入页所属书 + 插入位置(PDF=page / EPUB=章序 after)。"""
    low = (rel or "").lower()
    if low.endswith(".pdf"):
        pg = rec.get("page")
        if isinstance(pg, int) and pg > 0:
            return "/pdf/view?file=" + urllib.parse.quote(rel) + "&page=%d" % pg
        return "/pdf/view?file=" + urllib.parse.quote(rel)
    if low.endswith(".epub"):
        af = rec.get("after")
        sec = max(0, int(af) - 1) if isinstance(af, int) and af > 0 else 0
        return "/pdf/epub/view?file=" + urllib.parse.quote(rel) + "&sec=%d" % sec
    return "/pdf/"


def _fav_render_userpage_md(md: str) -> str:
    """用户页 markdown → EPUB section HTML(RC.md 式:公式/列表/标题/图/链接;复用 PDF 插页同款行内渲染 _up_md_html)。
    数学/图片/链接**先抠成占位符**(用原文,零重复转义)再渲染;数学还原时 HTML 转义(html.parser 安全,MathJax 读
    textContent 仍是原式);标题空 → 不加 h2(标题走分隔条)。"""
    import html as _h
    ph = []   # 占位符 → 还原用的 raw HTML 片段

    def _hold(frag):
        ph.append(frag)
        return "@@FAVPH%d@@" % (len(ph) - 1)

    t = md or ""
    # ① 数学(整段抠出防被 markdown/escape 拆坏;还原时转义:$a<b$ → $a&lt;b$,html.parser 安全,MathJax 读回 $a<b$)
    t = re.sub(r"\$\$[\s\S]+?\$\$", lambda m: _hold(_h.escape(m.group(0), quote=False)), t)
    t = re.sub(r"\\\[[\s\S]+?\\\]", lambda m: _hold(_h.escape(m.group(0), quote=False)), t)
    t = re.sub(r"\$(?!\s)(?:\\\$|[^$\n])+?\$", lambda m: _hold(_h.escape(m.group(0), quote=False)), t)
    t = re.sub(r"\\\([\s\S]+?\\\)", lambda m: _hold(_h.escape(m.group(0), quote=False)), t)
    # ② 图片 ![alt](src)(src/alt 取原文,转义一次)③ 链接 [text](url)(图片先于链接,避免 ![..](..) 被链接规则截半)
    t = re.sub(r"!\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)",
               lambda m: _hold('<img alt="%s" src="%s"/>' % (_fav_esc(m.group(1)), _fav_esc(m.group(2)))), t)
    t = re.sub(r"\[([^\]]+)\]\(\s*([^)\s]+)[^)]*\)",
               lambda m: _hold('<a href="%s">%s</a>' % (_fav_esc(m.group(2)), _fav_esc(m.group(1)))), t)
    html = _up_md_html("", t)   # 复用 PDF 插页同款(标题/列表/加粗/斜体/行内 code);占位符纯字母数字,穿过 escape+inline 不变
    return re.sub(r"@@FAVPH(\d+)@@", lambda m: ph[int(m.group(1))] if int(m.group(1)) < len(ph) else "", html)


def _fav_pack_userpage_imgs(i: int, html: str):
    """用户页正文里的 <img>:vault 本地文件 → 读字节打包进 zip(src 改相对路径);远程/内联(http/data)原样留
    (在线阅读器能直接加载);解析不到本地文件也原样留(可能是站内代理 URL)。返回 (rewritten_html, imgs_out)。"""
    from bs4 import BeautifulSoup
    from urllib.parse import unquote
    soup = BeautifulSoup(html or "", "html.parser")
    imgs_out = []
    n = 0
    root_res = OBSIDIAN_ROOT.resolve()
    for im in list(soup.find_all("img")):
        src = (im.get("src") or "").strip()
        if not src or src.startswith(("http://", "https://", "data:", "//")):
            continue   # 远程/内联图:原样保留(不打包、不丢)
        data = None
        ext = ".img"
        try:
            rp = unquote(src.split("?")[0].split("#")[0]).lstrip("/")
            if ".." not in rp:
                ip = (OBSIDIAN_ROOT / rp).resolve()
                ip.relative_to(root_res)   # 防越界
                if ip.is_file() and ip.stat().st_size <= 12 * 1024 * 1024:
                    data = ip.read_bytes()
                    ext = ip.suffix.lower() or ".img"
        except Exception:
            data = None
        if data is None:
            continue   # 解析不到本地文件:保留原 src
        n += 1
        arc = "img/up%03d_%02d%s" % (i, n, ext if ext.startswith(".") else "." + ext)
        imgs_out.append((arc, data, _fav_img_mt(ext)))
        im["src"] = arc                 # zip 内相对路径(相对 OEBPS/sec_N.xhtml)
        wh = _fav_img_wh(data)
        if wh:
            im["width"] = str(wh[0])
            im["height"] = str(wh[1])
    return soup.decode_contents(), imgs_out


def _ink_strokes_to_svg(strokes, vb: int = 1000) -> str:
    """墨迹笔画 [{t,c,w,p:[[x,y]...]}](x/宽、y/高 各自归一化到所在页)→ SVG 元素串。
    收藏夹里插入页无 canvas,用 <svg viewBox=0 0 vb vb preserveAspectRatio=none> 拉满 block 复现相对位置;
    stroke-width 用 vector-effect=non-scaling-stroke 保持 w px 常宽(不随拉伸变粗)。形状照搬前端 _inkDrawStroke。"""
    import html as _html
    import math
    out = []
    for s in (strokes or []):
        if not isinstance(s, dict):
            continue
        pts = s.get("p") or []
        if not pts:
            continue
        col = _html.escape(str(s.get("c") or "#e74c3c"), quote=True)
        w = float(s.get("w") or 2.5)
        t = s.get("t") or "pen"
        common = ('stroke="%s" stroke-width="%g" fill="none" stroke-linecap="round" '
                  'stroke-linejoin="round" vector-effect="non-scaling-stroke"' % (col, w))
        def X(i, _p=pts): return round(_p[i][0] * vb, 1)
        def Y(i, _p=pts): return round(_p[i][1] * vb, 1)
        n = len(pts)
        if t == "line":
            out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" %s/>' % (X(0), Y(0), X(n - 1), Y(n - 1), common))
        elif t == "rect":
            x0, y0, x1, y1 = X(0), Y(0), X(n - 1), Y(n - 1)
            out.append('<rect x="%g" y="%g" width="%g" height="%g" %s/>' % (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), common))
        elif t == "arrow":
            ex, ey = X(n - 1), Y(n - 1)
            px, py = (X(n - 2), Y(n - 2)) if n > 1 else (X(0), Y(0))
            out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" %s/>' % (X(0), Y(0), ex, ey, common))
            ang = math.atan2(ey - py, ex - px); ah = vb * 0.022
            out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" %s/>' % (ex, ey, ex - ah * math.cos(ang - 0.42), ey - ah * math.sin(ang - 0.42), common))
            out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" %s/>' % (ex, ey, ex - ah * math.cos(ang + 0.42), ey - ah * math.sin(ang + 0.42), common))
        else:  # pen:折线(点够密,直连即平滑)
            d = "M" + " L".join("%g,%g" % (X(i), Y(i)) for i in range(n))
            out.append('<path d="%s" %s/>' % (d, common))
    return "".join(out)


def _fav_video_item(i: int, it: dict):
    """视频条目(阶段D)→ (label, body, [])。收藏夹 section 走 raw 不消毒 → 缩略图 + data-yt,前端点击升级成 iframe 播放。"""
    vid = it.get("vid") or ""
    title = (it.get("title") or "").strip() or "视频"
    # 来源:显式 src 优先,否则按 bvid(BV 开头 12 位)兜底 → B站/YouTube 各自的原链接 + 缩略图兜底
    is_bili = (it.get("src") == "bili") if it.get("src") else bool(re.match(r"^BV[0-9A-Za-z]{10}", vid))
    thumb = it.get("thumb") or ("" if is_bili else ("https://i.ytimg.com/vi/%s/mqdefault.jpg" % vid))
    label = "🎬 视频 · " + title
    href = ("https://www.bilibili.com/video/" + vid) if is_bili else ("https://www.youtube.com/watch?v=" + vid)
    body = (_fav_sep_html(label, href, it)
            + ('<div class="fav-item fav-video" data-yt="%s">' % _fav_esc(vid))
            + ('<div class="fav-video-thumb"><img src="%s" alt="" referrerpolicy="no-referrer"/><span class="fav-video-play">&#9654;</span></div>' % _fav_esc(thumb))
            + ('<div class="fav-video-title">%s</div></div>' % _fav_esc(title)))
    return label, body, []


def _fav_userpage_item(i: int, it: dict, warns: list):
    """用户页(插入页)条目 → (label, section_body_html, [(img_arc, bytes, mt)])。userpages sidecar 全程只读。"""
    rel = it.get("file") or ""
    uid = it.get("id") or ""
    src_name = rel.split("/")[-1] or "?"
    rec = next((x for x in _upages_load(rel) if isinstance(x, dict) and x.get("id") == uid), None)
    if rec is None:
        low = rel.lower()
        href = ("/pdf/view?file=" + urllib.parse.quote(rel)) if low.endswith(".pdf") else \
               (("/pdf/epub/view?file=" + urllib.parse.quote(rel)) if low.endswith(".epub") else "/pdf/")
        label = "📝 我的页 · 出自《%s》" % src_name
        warns.append("第 %d 条(我的页 %s)记录不存在(可能已删除)" % (i + 1, uid))
        return label, (_fav_sep_html(label, href, it)
                       + '<div class="fav-item fav-missing">这条「我的页」已被删除或找不到了。</div>'), []
    title = (rec.get("title") or "").strip()
    label = "📝 我的页" + ((" · " + title) if title else "") + " · 出自《%s》" % src_name
    href = _fav_userpage_href(rel, rec)
    md = rec.get("md") or ""
    imgs = []
    if md.strip():
        inner = _fav_render_userpage_md(md)
        inner, imgs = _fav_pack_userpage_imgs(i, inner)
    else:
        inner = '<p class="fav-up-empty">（这一页还没有文字内容）</p>'
    # 手写墨迹双向(2026-07-05):把收藏的自建页做成**原书那页的实时编辑器**。
    #   EPUB:.fav-item-userpage 带 data-uid(原书 userpage id)+ data-ink-file(原书文件)→ 前端把墨迹画在页体(与原书
    #     .ep-usec 同 86vh 几何 → 坐标对齐),并按「原书文件+uid」读写 /api/epub-ink(fav 里画=写进原书,原书画了 fav 也同步)。
    #   PDF:墨迹按页号存 /api/ink,epub-ink 端点管不到 → 暂以只读 SVG 快照显示原墨迹(不双向)。
    if rel.lower().endswith(".epub") and uid:
        # .fav-up-disp 包住 md 显示层:前端「✏️ 编辑」保存后就地替换它(正文双向 → PATCH /api/userpages 写回原书);
        # data-uid/data-ink-file 供墨迹+正文都定址原书。
        body_inner = ('<div class="fav-item fav-item-userpage" data-uid="%s" data-ink-file="%s"><div class="fav-up-disp">%s</div></div>'
                      % (_fav_esc(uid), _fav_esc(rel), inner))
    else:
        ink = []
        try:
            pg = rec.get("page"); pgs = _ink_load(rel).get("pages") or {}
            ink = (pgs.get(str(pg)) if pg is not None else None) or pgs.get(uid) or []
        except Exception:
            ink = []
        ink_svg = _ink_strokes_to_svg(ink) if isinstance(ink, list) and ink else ""
        if ink_svg:
            body_inner = ('<div class="fav-item fav-item-userpage fav-up-hasink">'
                          '<svg class="fav-up-ink" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">'
                          + ink_svg + '</svg><div class="fav-up-content">' + inner + '</div></div>')
        else:
            body_inner = '<div class="fav-item fav-item-userpage">' + inner + "</div>"
    return label, _fav_sep_html(label, href, it) + body_inner, imgs


def _fav_item_snippet(body: str, limit: int = 80) -> str:
    """从条目 section body 抠出**内容首句**(≤limit 字,供 AI 目录概览)。只取 .fav-item 内容块(排除分隔条
    《书名》·页/『打开原书 ↗』),PDF 透明词层/EPUB 正文/用户页 md 皆命中;失败/缺失 → 空串。"""
    try:
        from bs4 import BeautifulSoup
        el = BeautifulSoup(body or "", "html.parser").find(class_="fav-item")
        if not el:
            return ""
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        return t[:limit]
    except Exception:
        return ""


def _fav_write_epub(out_path: Path, fid: str, name: str, items: list, warns: list) -> list:
    """按 items 顺序装配一本**真 EPUB**(标准 zip:mimetype+container.xml+OPF+nav+各条目一个 XHTML+图片打包)写到 out_path。
    严格 1 item=1 section(=1 spine)。原书全程只读;只往 out_path 写。
    返回 **AI 元数据 meta_items**(每条目一条,section idx 对齐:出处 src_file/src_name/src_page|src_section + label
    + 首句 snippet + 相邻 adj_prev + missing),供 _fav_build_job 落 state/reader-fav-meta/<fid>.json。"""
    import zipfile
    sections = []   # (sec_href, xhtml_str)
    images = []     # (arc_under_OEBPS, bytes, media_type)
    nav = []        # (label, sec_href)
    meta_items = []  # 每条目 AI 元数据(出处/首句/相邻/missing),section idx 对齐 spine
    for i, it in enumerate(items):
        sec_href = "sec_%04d.xhtml" % i
        kind = it.get("kind")
        frel = it.get("file") or ""
        src_name = frel.split("/")[-1] or "?"
        if kind == "pdf":
            label, body, imgs = _fav_pdf_item(i, it, src_name, warns)
        elif kind == "epub":
            label, body, imgs = _fav_epub_item(i, it, src_name, warns)
        elif kind == "userpage":
            label, body, imgs = _fav_userpage_item(i, it, warns)   # 自己创建的插入页:物化正文 md 成一节
        elif kind == "video":
            label, body, imgs = _fav_video_item(i, it)   # 阶段D:YouTube 视频条目 → 可播放 section
        else:
            label = "未知条目"
            body = _fav_sep_html("未知条目", "/pdf/") + '<div class="fav-item fav-missing">无法识别的收藏条目。</div>'
            imgs = []
        images.extend(imgs)
        sections.append((sec_href, _fav_xhtml(label or name, body)))
        nav.append((label or ("第 %d 条" % (i + 1)), sec_href))
        # ── AI 元数据:出处 + 首句 + 相邻性(同书连续)+ missing ──
        missing = "fav-missing" in (body or "")
        rec = {"section": i, "kind": kind, "src_file": frel, "src_name": src_name,
               "label": label or "", "snippet": ("" if missing else _fav_item_snippet(body)),
               "missing": missing}
        if kind == "pdf":
            rec["src_page"] = it.get("page")
        elif kind == "epub":
            rec["src_section"] = it.get("section")
        elif kind == "userpage":
            rec["id"] = it.get("id")
        # adj_prev:与上一条**同书且页/章连续**(PDF page==prev+1 / EPUB section==prev+1;跨书/逆序/重复/跳页/userpage 一律 False)
        adj = False
        if i > 0 and kind in ("pdf", "epub"):
            pv = items[i - 1]
            if pv.get("kind") == kind and (pv.get("file") or "") == frel:
                if kind == "pdf":
                    cp, pp = it.get("page"), pv.get("page")
                    adj = isinstance(cp, int) and isinstance(pp, int) and cp == pp + 1
                else:
                    cs, ps = it.get("section"), pv.get("section")
                    adj = isinstance(cs, int) and isinstance(ps, int) and cs == ps + 1
        rec["adj_prev"] = adj
        meta_items.append(rec)
    if not items:                     # 空收藏夹:一张说明页(空 spine 的 EPUB 不合法)
        sec_href = "sec_0000.xhtml"
        body = ('<div class="fav-item fav-missing">这个收藏夹还没有内容。<br>在阅读器里点 ⭐ 收藏页面/章节即可。</div>')
        sections.append((sec_href, _fav_xhtml(name, body)))
        nav.append(("(空)", sec_href))
    # OPF manifest + spine(拼接,不用 %-format:label/name 可能含 %)
    man = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
           '<item id="css" href="style/fav.css" media-type="text/css"/>']
    spine = []
    for i, (sec_href, _b) in enumerate(sections):
        man.append('<item id="sec%d" href="%s" media-type="application/xhtml+xml"/>' % (i, sec_href))
        spine.append('<itemref idref="sec%d"/>' % i)
    for j, (arc, _d, mt) in enumerate(images):
        man.append('<item id="img%d" href="%s" media-type="%s"/>' % (j, arc, mt))
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           '    <dc:identifier id="bookid">fav-' + _fav_esc(fid) + '</dc:identifier>\n'
           '    <dc:title>' + _fav_esc(name) + '</dc:title>\n'
           '    <dc:language>zh</dc:language>\n'
           '    <dc:creator>bwicarus 收藏夹</dc:creator>\n'
           '  </metadata>\n  <manifest>\n    ' + "\n    ".join(man) + '\n  </manifest>\n'
           '  <spine>\n    ' + "\n    ".join(spine) + '\n  </spine>\n</package>\n')
    nav_lis = "\n      ".join('<li><a href="' + h + '">' + _fav_esc(l) + '</a></li>' for (l, h) in nav)
    nav_xhtml = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head>'
                 '<meta charset="utf-8"/><title>目录</title></head><body>'
                 '<nav epub:type="toc" id="toc"><ol>\n      ' + nav_lis + '\n    </ol></nav></body></html>\n')
    with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as z:
        zi = zipfile.ZipInfo("mimetype")   # mimetype 必须**第一个 + 不压缩**(EPUB 规范)
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, b"application/epub+zip")
        z.writestr("META-INF/container.xml", _FAV_CONTAINER_XML)
        z.writestr("OEBPS/content.opf", opf.encode("utf-8"))
        z.writestr("OEBPS/nav.xhtml", nav_xhtml.encode("utf-8"))
        z.writestr("OEBPS/style/fav.css", _FAV_EPUB_CSS.encode("utf-8"))
        for sec_href, data in sections:
            z.writestr("OEBPS/" + sec_href, data.encode("utf-8"))
        for arc, data, _mt in images:
            z.writestr("OEBPS/" + arc, data)
    return meta_items


def _fav_build_job(jid: str, fid: str):
    """收藏夹物化 job(v5=真 EPUB):按 items 顺序装配一本真 EPUB(state/reader-fav-epub/<fid>.epub),用完整 EPUB
    阅读器(epub-html.js)打开 → 全功能天然可用。原书全程只读(page-chars/page-image/epub-section 只读);
    只往 state/reader-fav-epub/<fid>.epub 写(tmp+原子替换)。并退役旧 v4 流式 HTML(.html)/ v3 固定页 PDF(.pdf)产物。"""
    tmp = None
    built_ok = False                      # 成功且未被删 → finally re-check 合并(期间又变脏则再起一次)
    fav_rel = _fav_book_rel(fid)          # 旧 .pdf rel(占 _INSPAGE_ACTIVE / 退役删除用)
    epub_path = _fav_epub_path(fid)
    html_path = _fav_html_path(fid)       # 旧 v4 流式 HTML(退役清理)
    try:
        _job_set(jid, status="running", kind="fav-build", step="读取收藏清单…", ts=_time.time())
        folder = _fav_folder(_fav_load(), fid)
        if not folder:
            raise RuntimeError("收藏夹不存在")
        items = folder.get("items") or []
        name = folder.get("name") or "收藏夹"
        content_sig = _fav_content_sig(items)
        warns = []
        _job_set(jid, step="装配收藏 EPUB…(共 %d 条)" % len(items))
        _FAV_EPUB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = epub_path.with_name(".favtmp-" + _uuid.uuid4().hex[:8] + ".epub")   # 同目录原子替换
        meta_items = _fav_write_epub(tmp, fid, name, items, warns)
        _job_set(jid, step="写入收藏夹产物…")
        os.replace(str(tmp), str(epub_path))
        tmp = None
        # 换 .epub → mtime 变 → _ensure_epub_extracted 下次自动重解包(旧解包目录会被清并重建);为保险主动失效 OPF 缓存
        try:
            _EPUB_OPF_CACHE.pop(str(_EPUB_EXTRACT_DIR / _epub_sha(_fav_epub_rel(fid))), None)
        except Exception:
            pass
        # AI 元数据(出处/首句/相邻/missing)→ state/reader-fav-meta/<fid>.json(tmp+原子替换);夹被删则下面 deleted 分支清
        try:
            _FAV_META_DIR.mkdir(parents=True, exist_ok=True)
            mtmp = _fav_meta_path(fid).with_name(".favmeta-" + _uuid.uuid4().hex[:8] + ".json")
            mtmp.write_text(json.dumps({"fid": fid, "name": name, "built_ver": _FAV_BUILD_VER,
                                        "content_sig": content_sig, "built_ts": int(_time.time()),
                                        "items": meta_items}, ensure_ascii=False), "utf-8")
            os.replace(str(mtmp), str(_fav_meta_path(fid)))
        except Exception:
            pass
        # 退役旧 v4 流式 HTML + v3 固定页 PDF 产物(派生物,删了不影响任何原书;别删原书)
        for _old in (html_path, _fav_book_abs(fid)):
            try:
                _old.unlink(missing_ok=True)
            except Exception:
                pass
        # built_sig 落定(标记已构建到当前 content_sig)。持 _FAV_LOCK 与 CRUD 串行,防丢更新。
        deleted = False
        with _FAV_LOCK:
            d3 = _fav_load()
            f3 = _fav_folder(d3, fid)
            if f3:
                f3["built_sig"] = content_sig
                f3["built_ver"] = _FAV_BUILD_VER
                f3.setdefault("content_sig", content_sig)
                f3["built_ts"] = int(_time.time())
                _fav_save(d3)
            else:
                deleted = True               # 夹在 build 期间被删 → 别留孤儿产物
        if deleted:
            try:
                epub_path.unlink(missing_ok=True)   # 夹在 build 期间被删 → 清掉刚落盘的孤儿 EPUB
            except Exception:
                pass
            try:
                _fav_meta_path(fid).unlink(missing_ok=True)   # 同步清孤儿 AI 元数据
            except Exception:
                pass
            _job_set(jid, status="done", ts=_time.time(),
                     result={"ok": True, "deleted": True, "fid": fid})
        else:
            built_ok = True
            _job_set(jid, status="done", ts=_time.time(),
                     result={"ok": True, "fid": fid, "items": len(items), "warnings": warns[:20],
                             "view": "/pdf/fav/open?id=" + urllib.parse.quote(fid)})
            try:
                _reader_publish("fav-built", "fav:" + fid, None)   # 新产物就绪 → 已打开的收藏夹阅读器增量重排(结构真·增量)
            except Exception:
                pass
    except Exception as ex:
        try:
            if tmp and Path(tmp).exists():
                Path(tmp).unlink()
        except Exception:
            pass
        _job_set(jid, status="error", error=str(ex), ts=_time.time())
    finally:
        with _INSPAGE_MUTEX:
            _FAV_BUILD_ACTIVE.pop(fid, None)
            _INSPAGE_ACTIVE.discard(fav_rel)
        # 合并(last-write-wins):build 成功且期间 CRUD 又改了条目(content_sig != built_sig)→ 再起一次后台 build。
        # 失败/夹被删不自动重试(防死循环 / 别复活已删夹)。_fav_trigger_build 自带脏检查+去重。
        if built_ok:
            try:
                _fav_trigger_build(fid)
            except Exception:
                pass


def _fav_trigger_build(fid: str, folder=None):
    """收藏夹内容变更 → 后台 fire-and-forget 起 build(daemon 线程,不阻塞 CRUD 响应、不等 job)。
    去重:同夹已有 build 在跑 → 复用其 jid,不重复起(短时多次改条目自动合并——那个 job 收尾会 re-check dirty 再起)。
    脏才起(不脏 / 夹不存在 → 不起,返回 jid=None)。返回 (jid or None, started:bool)。"""
    if folder is None:
        folder = _fav_folder(_fav_load(), fid)
    if not folder:
        return None, False
    with _INSPAGE_MUTEX:                          # 与 job finally 的 pop / /fav/open 去重共用同一把锁,原子判定
        jid = _FAV_BUILD_ACTIVE.get(fid)
        if jid:
            return jid, False                    # 已在建 → 复用(合并靠该 job 收尾 re-check)
        if not _fav_is_dirty(folder, fid):
            return None, False                   # 不脏,免建
        jid = _uuid.uuid4().hex[:12]
        _FAV_BUILD_ACTIVE[fid] = jid
        _INSPAGE_ACTIVE.add(_fav_book_rel(fid))   # 重建期间占住派生书 rel(拦对它的插页 job)
    _job_set(jid, status="running", kind="fav-build", step="排队中…", ts=_time.time())
    try:
        _upthr.Thread(target=_fav_build_job, args=(jid, fid), daemon=True).start()
    except Exception:
        with _INSPAGE_MUTEX:   # 起线程失败(线程/内存耗尽)→ 回滚 _FAV_BUILD_ACTIVE/_INSPAGE_ACTIVE 占位,否则该夹永久卡在"building"打不开(审查确认)
            _FAV_BUILD_ACTIVE.pop(fid, None)
            _INSPAGE_ACTIVE.discard(_fav_book_rel(fid))
        return None, False
    return jid, True


def _fav_cascade_userpage_delete(rel: str, uid: str):
    """自建页被删(任一阅读器的 🗑,同一张纸)→ 级联移除各收藏夹里指向它的条目(kind:userpage 同 file+id),
    标脏 + 后台重建 + 推 SSE 事件。双向同步:收藏夹不留「已删除」墓碑。"""
    changed = []
    with _FAV_LOCK:
        d = _fav_load()
        for f in d.get("folders") or []:
            items = f.get("items") or []
            keep = [it for it in items
                    if not (isinstance(it, dict) and it.get("kind") == "userpage"
                            and it.get("file") == rel and it.get("id") == uid)]
            if len(keep) != len(items):
                f["items"] = keep
                _fav_mark_dirty(f)
                changed.append(f.get("id"))
        if changed:
            _fav_save(d)
    for fid in changed:
        try:
            _fav_trigger_build(fid)
        except Exception:
            pass
    if changed:
        try:
            _reader_publish("fav", rel, uid)   # 已打开的收藏夹/原书据此感知结构变化
        except Exception:
            pass


def _fav_prune_dead_userpages(fid: str) -> bool:
    """清掉指向**已删除自建页**的收藏条目(同一张纸:页没了条目就该没;修级联上线前删的存量墓碑)。
    谨慎:仅当原书 userpages sidecar 文件确实存在、且查无此 id 才删;sidecar 缺失(改名/迁移)→ 保留不动。
    ⚠ 调用方不得持 _FAV_LOCK(普通 Lock 非重入;CRUD 在锁内调 trigger 的路径不要来这)。返回是否有清理。"""
    changed = False
    with _FAV_LOCK:
        d = _fav_load()
        f = _fav_folder(d, fid)
        if not f:
            return False
        keep = []
        for it in (f.get("items") or []):
            if isinstance(it, dict) and it.get("kind") == "userpage":
                rel2, uid2 = (it.get("file") or ""), (it.get("id") or "")
                try:
                    if uid2 and _upages_path(rel2).exists() \
                            and not any(x.get("id") == uid2 for x in _upages_load(rel2) if isinstance(x, dict)):
                        changed = True
                        continue   # 死条目 → 丢弃
                except Exception:
                    pass
            keep.append(it)
        if changed:
            f["items"] = keep
            _fav_mark_dirty(f)
            _fav_save(d)
    return changed


def _fav_prebuild_all():
    """服务器空闲时把所有『脏』收藏夹提前重建好(版本 bump / userpage 正文编辑后自动变脏)→ 用户打开即秒开、不再前台等重建。
    串行(一次一本,复用 _fav_trigger_build 的脏检查+去重),避免同时建多本大 EPUB 压垮 Pi;每本最多等 5min。"""
    try:
        folders = list(_fav_load().get("folders") or [])
    except Exception:
        return
    for f in folders:
        fid = f.get("id")
        if not fid:
            continue
        try:
            if _fav_prune_dead_userpages(fid):   # 顺带清存量墓碑条目(指向已删自建页)→ 变脏本轮就重建掉
                f = None                          # items 变了 → 让 trigger 重新加载
            jid, started = _fav_trigger_build(fid, f)   # 脏才建;不脏/在建 → 跳
        except Exception:
            continue
        if not jid:
            continue                # 不脏 / 触发失败 → 跳
        for _ in range(600):        # 串行等这本(含用户 /fav/open 触发的同一本,jid 已在建也等它)建完 ≤300s 再下一本,别并发建多本大 EPUB 压垮 Pi(审查确认)
            with _INSPAGE_MUTEX:
                if _FAV_BUILD_ACTIVE.get(fid) != jid:
                    break
            _time.sleep(0.5)


def _fav_prebuild_loop():
    """启动后台预建 + 定期复扫(catch userpage 正文编辑等运行期新脏)。daemon;只在 webapp(register_pdf_reader)里起,裸 import 不跑。"""
    _time.sleep(45)     # 让 app 启动稳定 + 避开启动高峰
    while True:
        try:
            _fav_prebuild_all()
        except Exception:
            pass
        _time.sleep(900)   # 每 15min 复扫一次


def pdf_api_favorites():
    """收藏夹 CRUD(全局 sidecar,照 notes 模式)。
    GET → {ok, folders:[{id,name,items:[{file,kind,page|section}],created}]}(⭐picker 端自行判断当前页在哪些夹);
    POST {name, item?} → 建夹(可顺带收一条,picker「新建即勾选」一步到位);POST {folder, item} → 加条目(同夹同页去重,幂等);
    PATCH {folder, name?} → 改名;PATCH {folder, remove_item:{...}} → 移出条目;
    DELETE ?id=f_xxx → 删整夹。"""
    if request.method == "GET":
        r = jsonify({"ok": True, "folders": _fav_load().get("folders") or []})
        r.headers["Cache-Control"] = "no-store"
        return r
    # 变更(建/加/改名/移出/删)全串行化(_FAV_LOCK:防与 build job 写 built_sig 丢更新)。
    # 加/删/改条目 → _fav_mark_dirty 刷 content_sig(!= built_sig 即脏 → 下次 /fav/open 兜底重建)。
    if request.method == "DELETE":
        fid = (request.args.get("id") or "").strip()
        with _FAV_LOCK:
            d = _fav_load()
            n0 = len(d["folders"])
            d["folders"] = [f for f in d["folders"] if f.get("id") != fid]
            if len(d["folders"]) != n0:
                _fav_save(d)
        if fid and re.match(r"^f_[0-9a-zA-Z]+$", fid):   # 删夹后顺手删派生产物(EPUB + 解包目录 + 退役 .html/.pdf;派生物,失败无碍)
            try:
                _fav_epub_path(fid).unlink(missing_ok=True)
            except Exception:
                pass
            try:
                import shutil as _sh
                _sh.rmtree(_EPUB_EXTRACT_DIR / _epub_sha(_fav_epub_rel(fid)), ignore_errors=True)
            except Exception:
                pass
            for _old in (_fav_html_path(fid), _fav_book_abs(fid), _fav_meta_path(fid)):
                try:
                    _old.unlink(missing_ok=True)
                except Exception:
                    pass
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    with _FAV_LOCK:
        d = _fav_load()
        if request.method == "POST":
            fid = (body.get("folder") or "").strip()
            if not fid:                                        # 建夹
                name = (body.get("name") or "").strip()[:80]
                if not name:
                    return jsonify({"ok": False, "error": "缺少 name"}), 400
                import uuid as _u
                f = {"id": "f_" + _u.uuid4().hex[:8], "name": name, "items": [],
                     "created": int(__import__("time").time())}
                it = _fav_norm_item(body.get("item"))
                if it:
                    f["items"].append(it)
                _fav_mark_dirty(f)
                d["folders"].append(f)
                _fav_save(d)
                if f["items"]:
                    _fav_trigger_build(f["id"], f)   # 建夹带条目 → 立即后台物化(打开时通常已好、秒开)
                return jsonify({"ok": True, "folder": f})
            f = _fav_folder(d, fid)
            if not f:
                return jsonify({"ok": False, "error": "未找到收藏夹"}), 404
            it = _fav_norm_item(body.get("item"))
            if not it:
                return jsonify({"ok": False, "error": "缺少/非法 item"}), 400
            if not isinstance(f.get("items"), list):
                f["items"] = []
            if not any(_fav_same_item(x, it) for x in f["items"]):   # 同夹同页不重复
                f["items"].append(it)
                _fav_mark_dirty(f)
                _fav_save(d)
                _fav_trigger_build(fid, f)          # 加条目 → 立即后台重建(不阻塞本次响应)
                try:
                    _reader_publish("fav-changed", "fav:" + fid, None)   # 结构变了(已打开的收藏夹等 fav-built 再增量重排)
                except Exception:
                    pass
            return jsonify({"ok": True, "folder": f})
        # PATCH:改名 / 移出条目
        fid = (body.get("folder") or "").strip()
        f = _fav_folder(d, fid)
        if not f:
            return jsonify({"ok": False, "error": "未找到收藏夹"}), 404
        changed = False
        if (body.get("name") or "").strip():
            f["name"] = str(body["name"]).strip()[:80]   # 改名只改 name(+PDF title,重建时刷),不动 items/sig,不触发重建
            changed = True
        items_changed = False
        ri = _fav_norm_item(body.get("remove_item"))
        if ri:
            n0 = len(f.get("items") or [])
            f["items"] = [x for x in (f.get("items") or []) if not _fav_same_item(x, ri)]
            items_changed = len(f["items"]) != n0
            if items_changed:
                _fav_mark_dirty(f)                 # 条目变了才标脏(改名不算)
            changed = changed or items_changed
        if changed:
            _fav_save(d)
        if items_changed:
            _fav_trigger_build(fid, f)             # 移出条目 → 立即后台重建(改名不触发)
            try:
                _reader_publish("fav-changed", "fav:" + fid, None)
            except Exception:
                pass
        return jsonify({"ok": True, "folder": f})


def pdf_api_fav_meta():
    """收藏夹 AI 元数据(section↔条目映射,build 时落 state/reader-fav-meta/<fid>.json)。
    已打开的收藏夹阅读器用它做**结构真·增量重排**(fav-built 事件后 diff 新旧 items,新增长出来/移除当场消失,不刷新)。"""
    fid = (request.args.get("id") or "").strip()
    if not re.match(r"^f_[0-9a-zA-Z]+$", fid):
        abort(404)
    r = jsonify({"ok": True, "meta": _fav_meta_load(fid)})
    r.headers["Cache-Control"] = "no-store"
    return r



def _fav_serve_reader(fid: str, folder: dict):
    """把已就绪的收藏夹 **真 EPUB** 用**完整 EPUB 阅读器**(epub_html_reader.html + epub-html.js)打开渲染。
    file_rel=合成 EPUB rel(资源/收藏夹/<fid>.epub,_resolve_epub_book 解析回 state 那本;高亮/墨迹 sidecar 键与 fid 绑死)。
    · 先确保解包(预热 OPF 缓存)——浏览器随后并发发 manifest/section 请求直接命中,不各自 490ms 堵 worker(见 epub_view 先例)。
    · server_pos=None + 不调 _lastopen_touch → **零进度**(不记停留位置、不进「最近打开」;规格 D)。读/解包失败返回 None。"""
    ep = _fav_epub_path(fid)
    if not ep.is_file():
        return None
    rel = _fav_epub_rel(fid)
    try:
        root = _ensure_epub_extracted(ep, rel)
        if not root:
            return None
        _epub_opf_info(root)   # 预热 OPF 缓存(照 epub_view)
    except Exception:
        return None
    from flask import make_response
    resp = make_response(render_template(
        "epub_html_reader.html", file_rel=rel, file_name=(folder.get("name") or "收藏夹"),
        sha=_epub_sha(rel), reader_js_v=_epub_js_v(), server_pos=None,
        reader_app="epub", reader_route="favorite",
        is_fav=True, fav_id=fid))   # is_fav → 模板注入收藏夹专属 PWA manifest/apple 标签(独立 app 入口)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp



# 等待页:脏收藏夹首访触发 build,轮询 job-status,done 后 reload /fav/open(届时不脏 → 渲染流式阅读器)。
_FAV_WAIT_HTML = (
    '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>正在生成收藏夹…</title>'
    '<style>body{margin:0;background:#0b0f1a;color:#dce6ff;font-family:sans-serif;display:flex;'
    'align-items:center;justify-content:center;min-height:100vh}.card{text-align:center;padding:28px 34px;max-width:82vw}'
    '.sp{width:34px;height:34px;border:3px solid #24406e;border-top-color:#7dd3fc;border-radius:50%;'
    'animation:s 1s linear infinite;margin:0 auto 16px}@keyframes s{to{transform:rotate(360deg)}}'
    '.nm{font-size:15px;color:#9fcbff;font-weight:600;margin-bottom:6px}'
    '.st{font-size:13px;color:#8fa4cc;min-height:18px}.er{color:#ff9a9a;font-size:13px}a{color:#7dd3fc}</style></head>'
    '<body><div class="card"><div class="sp" id="sp"></div>'
    '<div class="nm">正在整理「__NAME__」…</div>'
    '<div class="st" id="st">首次打开或有新收藏,需要生成一次(之后秒开)。</div></div>'
    '<script>var JID="__JID__",VIEW="__VIEW__";function poll(){'
    "fetch('/pdf/api/job-status?id='+encodeURIComponent(JID),{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){"
    "var st=document.getElementById('st');"
    "if(j.status==='done'){st.textContent='完成,正在打开…';location.replace(VIEW);return;}"
    "if(j.status==='error'){document.getElementById('sp').style.display='none';st.className='er';"
    "st.textContent='生成失败:'+(j.error||'未知错误')+'(原书未受影响)';return;}"
    "if(j.step)st.textContent=j.step;setTimeout(poll,900);"
    "}).catch(function(){setTimeout(poll,1500);});}poll();</script></body></html>"
)


def pdf_fav_open():
    """收藏夹统一化(规格 v5:真 EPUB):收藏夹 = 物化成的一本真 EPUB,用**完整 EPUB 阅读器**打开(全功能)。
    不脏(产物就绪)→ 直接渲染 EPUB 阅读器(秒开);脏(items 变过 / 产物不存在 / 版本升级)→ 触发后台
    _fav_build_job + 返回轮询等待页,done 后 reload /fav/open(届时不脏 → 渲染阅读器)。"""
    fid = (request.args.get("id") or "").strip()
    if not re.match(r"^f_[0-9a-zA-Z]+$", fid):
        abort(404)
    folder = _fav_folder(_fav_load(), fid)
    if not folder:
        abort(404)
    try:
        if _fav_prune_dead_userpages(fid):    # 清存量墓碑(指向已删自建页的条目)→ 变脏走等待页重建,打开即干净
            folder = _fav_folder(_fav_load(), fid) or folder
    except Exception:
        pass
    # 常态:内容一变 CRUD 已自动后台 build,这里通常不脏。_fav_trigger_build 脏才起(不脏返回 None)。
    jid, _started = _fav_trigger_build(fid, folder)
    if jid is None:                      # 不脏 = 产物就绪 → 秒开渲染
        resp = _fav_serve_reader(fid, folder)
        if resp is not None:
            return resp
        # 产物读/解包失败(极罕见)→ 删掉标脏 + 重新触发,走等待页重建
        try:
            _fav_epub_path(fid).unlink(missing_ok=True)
        except Exception:
            pass
        jid, _started = _fav_trigger_build(fid, folder)
    if jid is None:
        abort(503)
    import html as _htmlmod
    reload_url = "/pdf/fav/open?id=" + urllib.parse.quote(fid)
    page_html = (_FAV_WAIT_HTML.replace("__JID__", jid).replace("__VIEW__", reload_url)
                 .replace("__NAME__", _htmlmod.escape(folder.get("name") or "收藏夹")))
    resp = Response(page_html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def pdf_fav_view():
    """【已退役·规格 v3/v4】旧精简查看页 → 302 到 /fav/open(现=流式 HTML 阅读器)。
    路由保留只为兼容旧书签/外部链接;书架已改指 /fav/open。"""
    fid = (request.args.get("id") or "").strip()
    return redirect("/pdf/fav/open?id=" + urllib.parse.quote(fid))


def pdf_fav_manifest():
    """收藏夹 PWA manifest(每个夹一份:start_url 固定打开该夹、独立 name/scope/图标)→ iOS/安卓把收藏夹装成
    跟通用阅读器**独立**的 app;start_url 走 /fav/open(零状态、不进「最近打开」),不受「最后打开的书」影响。"""
    fid = (request.args.get("id") or "").strip()
    folder = _fav_folder(_fav_load(), fid) if re.match(r"^f_[0-9a-zA-Z]+$", fid) else None
    name = (((folder or {}).get("name")) or "收藏夹").strip() or "收藏夹"
    start = "/pdf/fav/open?id=" + urllib.parse.quote(fid)
    m = {
        "id": "fav-" + fid, "name": name, "short_name": name[:12],
        "start_url": start, "scope": "/pdf/fav/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#0a0e1a", "theme_color": "#10162a",
        "icons": [
            {"src": "/pdf/fav/icon?sz=192", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/pdf/fav/icon?sz=512", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    resp = jsonify(m)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def pdf_fav_icon():
    """收藏夹 PWA 图标:深底 + 金色五角星(⭐ 语义),即时生成 PNG(apple-touch-icon / manifest icons 共用)。"""
    try:
        sz = max(48, min(512, int(request.args.get("sz", "180") or "180")))
    except Exception:
        sz = 180
    import io
    import math
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (sz, sz), "#10162a")
    dr = ImageDraw.Draw(im)
    cx = cy = sz / 2.0
    R = sz * 0.36
    r = R * 0.42
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = R if (i % 2 == 0) else r
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    dr.polygon(pts, fill="#f2c14e")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    resp = Response(buf.getvalue(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

def register_favorites(bp, *, claude_dir, obsidian_root, fav_book_prefix, fav_epub_dir,
                       epub_extract_dir, epub_opf_cache, safe_vault_path, epub_sha,
                       epub_opf_info, epub_section_cached, epub_rewrite_url,
                       ensure_epub_extracted, epub_js_v, upages_load, upages_path,
                       up_md_html, inspage_mutex, inspage_active, page_chars_cached,
                       ink_load, job_set):
    """挂收藏夹 6 条路由到 bp(url_prefix /pdf),并注入 pdf_reader 依赖(见模块头)。"""
    global CLAUDE_DIR, OBSIDIAN_ROOT, _FAV_BOOK_PREFIX, _FAV_EPUB_DIR, _EPUB_EXTRACT_DIR
    global _EPUB_OPF_CACHE, _safe_vault_path, _epub_sha, _epub_opf_info, _epub_section_cached
    global _epub_rewrite_url, _ensure_epub_extracted, _epub_js_v, _upages_load, _upages_path
    global _up_md_html, _INSPAGE_MUTEX, _INSPAGE_ACTIVE, _page_chars_cached, _ink_load, _job_set
    global _FAV_FILE, _FAV_HTML_DIR, _FAV_META_DIR
    CLAUDE_DIR = claude_dir
    OBSIDIAN_ROOT = obsidian_root
    _FAV_BOOK_PREFIX = fav_book_prefix
    _FAV_EPUB_DIR = fav_epub_dir
    _EPUB_EXTRACT_DIR = epub_extract_dir
    _EPUB_OPF_CACHE = epub_opf_cache
    _safe_vault_path = safe_vault_path
    _epub_sha = epub_sha
    _epub_opf_info = epub_opf_info
    _epub_section_cached = epub_section_cached
    _epub_rewrite_url = epub_rewrite_url
    _ensure_epub_extracted = ensure_epub_extracted
    _epub_js_v = epub_js_v
    _upages_load = upages_load
    _upages_path = upages_path
    _up_md_html = up_md_html
    _INSPAGE_MUTEX = inspage_mutex
    _INSPAGE_ACTIVE = inspage_active
    _page_chars_cached = page_chars_cached
    _ink_load = ink_load
    _job_set = job_set
    _FAV_FILE = claude_dir / "state" / "reader-favorites.json"
    _FAV_HTML_DIR = claude_dir / "state" / "reader-fav-html"
    _FAV_META_DIR = claude_dir / "state" / "reader-fav-meta"
    for rule, func, methods in (
        ("/api/favorites", pdf_api_favorites, ['GET', 'POST', 'PATCH', 'DELETE']),
        ("/api/fav-meta", pdf_api_fav_meta, ['GET']),
        ("/fav/open", pdf_fav_open, ['GET']),
        ("/fav/view", pdf_fav_view, ['GET']),
        ("/fav/manifest", pdf_fav_manifest, ['GET']),
        ("/fav/icon", pdf_fav_icon, ['GET']),
    ):
        bp.add_url_rule(rule, view_func=func, methods=methods)
