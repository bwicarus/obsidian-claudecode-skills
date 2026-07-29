#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把无 AI 直接命令服务接到现有阅读器路由(任务书 A4 接线)。**纯增量**。

不改写任何既有路由:只在同一个 blueprint 上 `add_url_rule` 两条新路径。
旧网页 / 旧 MCP / 旧 AI / 旧卡片路径一律不动。

设计要点:
- **handler 在接线时解析,不在运行时猜**。解析不到的动作直接从活白名单移除,
  调用时明确报"未接线",而不是留一个假 handler 让调用方以为成功了。
- 白名单里**不允许出现会调 AI 的能力**(见 `reader-agent-capability-audit.md` §1.1)。
  这里再做一次机器校验:动作名尾段命中审计里的 AI 工具名就拒绝注册。
- 视觉类只提供**元数据**(页图是否存在/尺寸/墨迹版本),判断仍归上游助手。
"""
from __future__ import annotations

import time

import reader_direct_commands as DC

# 审计 §1.1 里会再次调用 AI 的能力名。接线时用它做机器校验,防止有人日后往白名单里加。
_AI_TOOL_NAMES = {
    "web_search", "search_image", "search_video", "make_paper", "summarize_section",
    "do_task", "run_saved_task", "see_page", "see_figure", "see_ink", "correct_dict",
    "material_graph", "read_material", "relate_material", "learning_focus",
    "situation_feedback", "make_diagnostic", "mastery_proposal", "apply_mastery",
    "error_patterns", "read_check_report", "add_vocab", "auto_highlight",
}
# ⚠ 2026-07-29:此表拦的是**旧助手工具名**,不是新动作名 —— `_assert_no_ai` 比的是
# `action.split(".",1)[-1]`,所以 `vocab.add` 的 tail 是 `add`,本就不会被 `add_vocab`
# 拦。曾因误以为会被拦而把 add_vocab / search_image 移出本表,那既不必要、判断也错:
#   · search_image:常规搜索落空时会调 Gemini 规范化检索词(`_gemini_text`),
#     确实会调 AI。(初次核对漏搜 gemini 关键词才判成零 AI。)
#   · add_vocab:旧助手链会经在线例句翻译落到 AI 后端。
# **安全的只是新拆出的 `vocab.add` 底座**(build_vocab_note + online=False),不是旧工具。
# 结论:旧工具名一律留在表内;要用某项能力就像 vocab.add 这样**另拆一条确定性路径**,
# 而不是给旧工具开口子。


class WiringError(RuntimeError):
    pass


def _assert_no_ai(actions) -> None:
    for a in actions:
        tail = a.split(".", 1)[-1]
        if tail in _AI_TOOL_NAMES or a in _AI_TOOL_NAMES:
            raise WiringError(f"拒绝注册:{a} 指向会调用 AI 的能力,不得进入直接命令白名单")


def build_handlers(pdf, *, toc_get=None, search_book=None, search_all=None,
                   dict_lookup=None, nav_publish=None,
                   vocab_add=None) -> tuple[dict, list[str]]:
    """从既有确定性底座组装 handler。返回 (handlers, 未接线的动作列表)。

    `pdf` = pdf_reader 模块。可选参数用于注入跨模块能力(目录/检索/词典/前端动作),
    没注入的就不接线——**宁可少一个动作,也不给假成功**。
    """
    H: dict = {}

    def _rel(anchor):
        rel = str(anchor.get("file") or "").strip()
        ap = pdf._safe_vault_path(rel)
        if not ap:
            raise ValueError(f"anchor.file 不是 vault 内的有效文件:{rel}")
        return rel, ap

    # ── 读取 ──────────────────────────────────────────────────────────────
    def read_page(anchor, params, prev):
        rel, ap = _rel(anchor)
        page = int(anchor.get("page"))
        if rel.lower().endswith(".epub"):
            paras = pdf._epub_section_paragraphs(rel, max(0, page)) or []
            text = "\n\n".join(str(x) for x in paras if str(x).strip())
            src = "epub:章节段落"
        else:
            text = pdf._page_text_clean(str(ap), rel, page, limit=8001) or ""
            src = "pdf:字符层(已剔噪)"
        return {"text": text[:8000], "text_available": bool(text.strip()),
                "text_source": src if text.strip() else None,
                "truncated": len(text) > 8000, "anchor": {"file": rel, "page": page}}
    H["read.page"] = read_page

    def read_selection(anchor, params, prev):
        # 选区三态照现状语义:空串=明确无选区,字段缺失=未上报。不沿用旧值。
        rec = pdf._reader_active_load() if hasattr(pdf, "_reader_active_load") else {}
        if "selection" not in (rec or {}):
            return {"reported": False, "selection": None}
        return {"reported": True, "selection": rec.get("selection") or "",
                "has_selection": bool(rec.get("selection")),
                "anchor": {"file": rec.get("member") or rec.get("file"),
                           "page": rec.get("member_pos", rec.get("pos"))}}
    H["read.selection"] = read_selection

    def read_pageimage(anchor, params, prev):
        # 只给元数据:是否有页图、墨迹版本。**不做视觉判断**(那是 AI 的事)。
        rel, ap = _rel(anchor)
        page = int(anchor.get("page"))
        ink = {}
        try:
            ink = (pdf._ink_load(rel) or {}).get("pages", {}) if hasattr(pdf, "_ink_load") else {}
        except Exception:
            ink = {}
        return {"anchor": {"file": rel, "page": page},
                "has_ink": str(page) in (ink or {}),
                "note": "仅元数据;需要看图请由上游助手走既有视觉能力"}
    H["read.pageimage"] = read_pageimage

    # ── 标注 ──────────────────────────────────────────────────────────────
    def hl_list(anchor, params, prev):
        rel, _ = _rel(anchor)
        items = (pdf._hl_load(rel) or {}).get("highlights") or []
        page = anchor.get("page")
        if page not in (None, ""):
            items = [h for h in items if str(h.get("page")) == str(page)]
        return {"count": len(items), "highlights": items[:200]}
    H["highlight.list"] = hl_list

    def hl_create(anchor, params, prev):
        rel, _ = _rel(anchor)
        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("params.text 必填(要高亮的正文片段)")
        doc = pdf._hl_load(rel) or {}
        items = doc.get("highlights") or []
        idem = params.get("_idem")
        if idem and any(h.get("idem") == idem for h in items):
            return {"created": False, "reason": "幂等键已存在", "id": idem}
        hid = f"dc_{int(time.time()*1000):x}"
        one = {"id": hid, "page": anchor.get("page"), "text": text[:2000],
               "color": pdf.hl_norm_color(params.get("color") or ""),
               "note": str(params.get("note") or "")[:1000] or None}
        if idem:
            one["idem"] = idem
        items.append(one)
        doc["highlights"] = items
        pdf._hl_save(rel, doc)
        return {"created": True, "id": hid}
    H["highlight.create"] = hl_create

    # ── 便签 ──────────────────────────────────────────────────────────────
    def note_list(anchor, params, prev):
        rel, _ = _rel(anchor)
        items = pdf._notes_load(rel) or []
        return {"count": len(items), "notes": items[:200]}
    H["note.list"] = note_list

    def note_create(anchor, params, prev):
        rel, _ = _rel(anchor)
        body = str(params.get("text") or "").strip()
        if not body:
            raise ValueError("params.text 必填(便签正文)")
        items = pdf._notes_load(rel) or []
        nid = f"dc_{int(time.time()*1000):x}"
        items.append({"id": nid, "page": anchor.get("page"), "text": body[:4000]})
        pdf._notes_save(rel, items)
        return {"created": True, "id": nid}
    H["note.create"] = note_create

    # ── 插入页 ────────────────────────────────────────────────────────────
    def page_new(anchor, params, prev):
        rel, _ = _rel(anchor)
        pages = pdf._upages_load(rel) or []
        after = params.get("after")
        pid = f"dc_{int(time.time()*1000):x}"
        pages.append({"id": pid, "after": after, "els": []})
        pdf._upages_save(rel, pages)
        return {"created": True, "anchor": {"file": rel, "userpage": pid}}
    H["page.new"] = page_new

    def page_add(anchor, params, prev):
        rel, _ = _rel(anchor)
        pid = (params.get("userpage")
               or ((prev or {}).get("anchor") or {}).get("userpage"))
        if not pid:
            raise ValueError("缺 params.userpage(或依赖模式下上一步的 anchor.userpage)")
        pages = pdf._upages_load(rel) or []
        for pg in pages:
            if pg.get("id") == pid:
                pg.setdefault("els", []).append(
                    {"kind": str(params.get("kind") or "text"),
                     "text": str(params.get("text") or "")[:4000]})
                pdf._upages_save(rel, pages)
                return {"added": True, "userpage": pid, "n": len(pg["els"])}
        raise ValueError(f"找不到插入页 {pid}")
    H["page.add"] = page_add

    # ── 制卡草稿(仅落编号,不调 AI 生成内容)────────────────────────────
    def anki_draft(anchor, params, prev):
        cards = params.get("cards")
        if not isinstance(cards, list) or not cards:
            raise ValueError("params.cards 必填(上游助手已想好的卡片数组)")
        gid = pdf._entity_reg_cards(cards[:24], {})
        return {"gid": gid, "n": len(cards[:24]), "draft": True}
    H["anki.draft"] = anki_draft

    # ── 自动解析剩余确定性底座(解析不到就不接线,不给假 handler)────────────
    if toc_get is None:
        # ⚠ 惰性解析:`_effective_toc` 是 pdf_reader **更后面**才 from book_toc 导入的,
        # 接线发生在文件前半段 → 那时 hasattr 还是 False。所以在调用时才取,而不是接线时。
        def toc_get(rel):                                       # noqa: F811
            fn = getattr(pdf, "_effective_toc", None)
            if not callable(fn):
                raise ValueError("目录能力未就绪(pdf._effective_toc 不可用)")
            return fn(rel)
    if dict_lookup is None:
        def dict_lookup(word):                                  # noqa: F811
            """离线 ECDICT,纯本地查表,不耗 AI。同样惰性导入:它在 scripts/vocab/ 下,
            接线时 sys.path 未必已包含该目录。"""
            import sys as _sys
            from pathlib import Path as _P
            vroot = str(_P(__file__).resolve().parents[1] / "scripts" / "vocab")
            if vroot not in _sys.path:
                _sys.path.append(vroot)
            from dict_sources import lookup_ecdict
            w = (word or "").strip()
            if not w:
                raise ValueError("params.word 必填")
            return lookup_ecdict(w)
    if search_book is None or search_all is None:
        import sqlite3
        _idx = getattr(pdf, "CLAUDE_DIR", None)
        _db = (_idx / "state" / "pdf-search.db") if _idx else None

        def _fts(q: str, rel: str | None = None, limit: int = 20):
            """FTS5 子串检索。纯 SQL,确定性,不调 AI。"""
            q = (q or "").strip()
            if len(q) < 2:
                raise ValueError("params.q 至少 2 个字符")
            # ⚠ 用户输入不能直接当 FTS5 查询语法:`AND/OR/NOT/NEAR/*/"/:`(以及连字符里的
            # not 这种)会被当作操作符,轻则 OperationalError(实测 `no such column: not`),
            # 重则成为查询注入面。整串包成**单个短语**,只做子串匹配 —— 与 trigram 分词器
            # 的设计意图一致(见 build_search_index.py)。
            phrase = chr(34) + q.replace(chr(34), chr(34) * 2) + chr(34)
            if not (_db and _db.exists()):
                raise ValueError("全文索引尚未构建(state/pdf-search.db 不存在)")
            con = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
            try:
                sql = ("SELECT file, page, snippet(pages_fts, -1, '[', ']', '…', 12) "
                       "FROM pages_fts JOIN pages_data ON pages_data.rowid = pages_fts.rowid "
                       "WHERE pages_fts MATCH ?")
                args: list = [phrase]
                if rel:
                    sql += " AND pages_data.file = ?"
                    args.append(rel)
                rows = con.execute(sql + " LIMIT ?", (*args, limit)).fetchall()
            finally:
                con.close()
            return [{"file": r[0], "page": r[1], "snippet": r[2]} for r in rows]

        if search_book is None:
            search_book = lambda rel, q: _fts(q, rel)          # noqa: E731
        if search_all is None:
            search_all = lambda q: _fts(q)                     # noqa: E731
    if nav_publish is None:
        try:
            import reader_events as _RE

            def nav_publish(kind, rel, page):
                """前端动作经既有 SSE 总线下发。确定性:只发事件,不做判断。"""
                fn = "goToPage" if kind == "goto" else "openBookAt"
                args = [page] if kind == "goto" else [rel, page]
                return _RE.publish("client-action", rel, None,
                                   {"action": {"fn": fn, "args": args}})
        except Exception:
            nav_publish = None

    # ── 可选注入项:注入了才接线 ──────────────────────────────────────────
    if callable(toc_get):
        H["toc.get"] = lambda a, p, prev: {"toc": toc_get(_rel(a)[0])}
    if callable(search_book):
        H["search.book"] = lambda a, p, prev: {"hits": search_book(_rel(a)[0], str(p.get("q") or ""))}
    if callable(search_all):
        H["search.all"] = lambda a, p, prev: {"hits": search_all(str(p.get("q") or ""))}
    if callable(dict_lookup):
        H["dict.lookup"] = lambda a, p, prev: {"entry": dict_lookup(str(p.get("word") or ""))}
    if callable(nav_publish):
        H["nav.goto"] = lambda a, p, prev: {"sent": nav_publish("goto", _rel(a)[0], a.get("page"))}
        H["nav.open"] = lambda a, p, prev: {"sent": nav_publish("open", _rel(a)[0], a.get("page"))}

    # ── 整章正文(第八节:拆开 summarize_section)────────────────────────────
    # 取正文是确定性的,总结归上游。PDF 按 TOC 切章;EPUB 的 section 本身即章。
    def section_read(anchor, params, prev):
        rel, ap = _rel(anchor)
        page = anchor.get("page")
        limit = min(int(params.get("limit") or 9000), 20000)
        if str(rel).lower().endswith(".epub"):
            paras = pdf._epub_section_paragraphs(rel, int(page or 0)) or []
            text = "\n\n".join(str(x) for x in paras if str(x).strip())
            return {"section_title": "", "page_range": str(page),
                    "text": text[:limit], "truncated": len(text) > limit}
        if not callable(toc_get):
            raise ValueError("PDF 取整章需要 toc_get 底座,本机未注入")
        page = int(page or 1)
        toc = toc_get(rel) or []
        # 找 page 落在哪个条目区间:条目按页升序,取最后一个 <= page 的作为章首,
        # 下一个条目的页作为章尾(开区间)。TOC 缺失就退回单页,并在标题里说明。
        starts = sorted({int(t.get("page") or 0) for t in toc if int(t.get("page") or 0) > 0})
        if not starts:
            body = pdf._page_text_clean(str(ap), rel, page, limit=limit) or ""
            return {"section_title": "(无目录,退回单页)", "page_range": str(page),
                    "text": body[:limit], "truncated": False}
        lo = max([s for s in starts if s <= page], default=starts[0])
        after = [s for s in starts if s > lo]
        hi = (after[0] - 1) if after else lo + 60      # 末章兜底,不无限读下去
        title = next((str(t.get("title") or "") for t in toc
                      if int(t.get("page") or 0) == lo), "")
        parts, total = [], 0
        for p in range(lo, hi + 1):
            if total >= limit:
                break
            t = pdf._page_text_clean(str(ap), rel, p, limit=limit - total) or ""
            if t.strip():
                parts.append(t)
                total += len(t)
        return {"section_title": title, "page_range": f"{lo}-{hi}",
                "text": "\n\n".join(parts)[:limit], "truncated": total >= limit}
    H["section.read"] = section_read

    # ── 生词(确定性词典 + 写 vault;不调 AI)────────────────────────────────
    # ⚠ 不要图省事整包用 scripts/vocab/dict_sources —— 它里面的 translate_sentences()
    #    是**调 AI** 的(默认 model="sonnet")。这里只走 build_vocab_note 的两个写入口,
    #    已逐行确认它们链上不碰 translate_sentences / ai_client。
    if vocab_add is None:
        try:
            import sys as _sys
            _vp = str(pdf.CLAUDE_DIR / "scripts" / "vocab")
            if _vp not in _sys.path:
                _sys.path.insert(0, _vp)
            import build_vocab_note as _BVN

            def vocab_add(word):                                # noqa: F811
                w = str(word or "").strip()[:60]
                if not w:
                    raise ValueError("params.word 不能为空")
                # 日文(含假名/汉字且非纯 ASCII)走日语笔记,其余走英语笔记。
                is_ja = (not w.isascii()) and any(
                    ("぀" <= c <= "ヿ") or ("一" <= c <= "鿿") for c in w)
                if is_ja:
                    p = _BVN.update_jp_word_note(w)
                    return {"word": w, "lang": "ja", "note": str(p[0] if isinstance(p, tuple) else p)}
                # online=False:直接命令要可预期、不吊在外部词典站上;ECDICT 是离线的。
                path, fm = _BVN.update_word_note(w, online=False, download_audio=False)
                return {"word": w, "lang": "en", "note": str(path),
                        "lemma": (fm or {}).get("lemma") or w}
        except Exception:
            vocab_add = None            # 底座缺失就不接线,不给假成功

    if callable(vocab_add):
        H["vocab.add"] = lambda a, p, prev: {
            "vocab": vocab_add(str(p.get("word") or "").strip()[:60])}

    # ── 召回创造物(本地注册表;上游联网也拿不到)──────────────────────────
    # 直读 state/assistant-creations/<uid>.json,不 import assistant —— 那个模块带
    # 整套 AI 编排,为读一个 JSON 把它拉进来既重又容易越界。
    # 引用型条目(纸/报告,ref 指向别的 sidecar)只回 ref 本身**不解引用**:本体要用
    # page/note 相关命令去取,让上游明确知道自己在读哪一份,而不是这里悄悄替它拼。
    def recall_creation(anchor, params, prev):
        import json as _json
        try:
            uid = pdf._reader_storage_identity_current().user_id
        except Exception as ex:
            raise ValueError(f"取不到当前身份:{ex}")
        p = pdf.CLAUDE_DIR / "state" / "assistant-creations" / f"{uid}.json"
        try:
            lst = _json.loads(p.read_text("utf-8")) if p.exists() else []
        except (OSError, ValueError):
            lst = []                       # 注册表损坏当空,不阻断上游
        if not isinstance(lst, list):
            lst = []
        cid = str(params.get("id") or "").strip()
        kind = str(params.get("kind") or "").strip()
        q = str(params.get("query") or "").strip()
        hits = [c for c in lst if isinstance(c, dict)]
        if cid:
            hits = [c for c in hits if str(c.get("id") or "") == cid]
        if kind:
            hits = [c for c in hits if str(c.get("kind") or "") == kind]
        if q:
            hits = [c for c in hits if q in str(c.get("brief") or "")]
        hits.sort(key=lambda c: -(c.get("ts") or 0))
        limit = max(1, min(int(params.get("limit") or 1), 10))
        return {"creations": hits[:limit], "total": len(lst)}
    H["recall.creation"] = recall_creation

    # ── 召回已学内容(合同由 Codex 2026-07-29 12:20 定)──────────────────────
    # 硬约束:query 必填(≤80)、limit ≤8、**不隐式继承 selection/focus**(上游必须自己
    # 说要查什么,否则召回结果与它的意图无从对齐);只查知识索引 / 已学 KG 节点 / Anki,
    # 不扫 raw vault、不联网、不调 AI。空命中是**成功**,单源异常回 partial。
    def recall_notes(anchor, params, prev):
        q = str(params.get("query") or "").strip()
        if not q:
            raise ValueError("params.query 必填(不隐式继承 selection/focus)")
        if len(q) > 80:
            raise ValueError(f"params.query 最多 80 字,当前 {len(q)}")
        limit = max(1, min(int(params.get("limit") or 8), 8))
        # 先全量收集再排序截断:total 必须是**命中总数**而非返回数,否则上游无从判断
        # "是不是还有更多"。截断只发生在最后一步。
        found: list = []
        status: dict = {}

        def _src_ready(path, label):
            """源目录可用性。**不存在=正常无数据(ok)**;存在但不是目录=坏(partial)。

            不能只靠 try/except:Path.glob() 对非目录不抛异常、直接返回空,
            于是"源坏了"会被静默当成"没命中",sourceStatus 仍报 ok —— 上游据此
            以为用户没学过,而真相是根本没查成。
            """
            if not path.exists():
                status[label] = "ok"
                return False
            if not path.is_dir():
                status[label] = "error: not_a_directory"
                return False
            return True

        def _kg_files(root):
            """只取正式图,排除备份/中间产物。

            生产上 knowledge_graph/ 里确实有 *.bak.json / *.pre.json / *.scan.json,
            裸 glob("*.json") 会把陈旧快照当现状召回,并与正式图重复。
            """
            skip = (".bak", ".pre", ".scan", ".tmp", ".old")
            for f in sorted(root.glob("*.json")):
                if any(f.stem.endswith(s) for s in skip) or f.stem.startswith("_"):
                    continue
                yield f

        # ① 知识索引:index/*.md 的条目行(纯文件读)。
        try:
            _d = pdf.CLAUDE_DIR / "index"
            for f in (sorted(_d.glob("*.md")) if _src_ready(_d, "index") else []):
                for ln in f.read_text("utf-8", errors="replace").splitlines():
                    if q in ln and ln.strip().startswith(("-", "*", "|")):
                        found.append({"source": "index", "rank": 2, "file": f.name,
                                      "text": ln.strip()[:300]})
            status.setdefault("index", "ok")
        except Exception as ex:
            status["index"] = f"error: {type(ex).__name__}"

        # ② KG 节点:**只回有真实学习证据的**。
        #    证据是**并集**,不是单一字段:
        #      · progress ∈ {mastered, in_progress}(link_and_mastery.py:240 的
        #        PROG_ORDER 才是学习进度权威;state 是 UI 状态,其中 unlockable 混了
        #        "已开始学"与"前置通了可以开始学"两种来源)
        #      · containing_notes 非空 —— 用户为它记过笔记,是硬证据
        #      · mastery / mastery_level 有正值
        #    Pi 上真实存在 progress=unseen 但 containing_notes 非空、mastery>0 的节点;
        #    只看 progress 会把这些真学过的漏掉。四者任一成立即算学过。
        try:
            kg = pdf.CLAUDE_DIR / "knowledge_graph"
            if _src_ready(kg, "kg"):
                import json as _j
                for f in _kg_files(kg):
                    try:
                        data = _j.loads(f.read_text("utf-8"))
                    except (OSError, ValueError):
                        continue
                    for node in (data.get("nodes") or []) if isinstance(data, dict) else []:
                        prog = str(node.get("progress") or "")
                        notes = node.get("containing_notes")
                        lvl, mst = node.get("mastery_level"), node.get("mastery")
                        has_notes = isinstance(notes, (list, tuple)) and len(notes) > 0
                        ok_l = isinstance(lvl, (int, float)) and not isinstance(lvl, bool) and lvl >= 1
                        ok_m = isinstance(mst, (int, float)) and not isinstance(mst, bool) and mst > 0
                        if prog == "mastered":
                            evidence, rank = "mastered", 0
                        elif prog == "in_progress" or has_notes or ok_l or ok_m:
                            evidence, rank = "started", 1
                        else:
                            continue          # unseen 且无笔记无掌握度 = 真没学过
                        title = str(node.get("title") or node.get("name") or "")
                        if q in title:
                            found.append({"source": "kg", "rank": rank, "book": f.stem,
                                          "title": title[:200],
                                          "state": str(node.get("state") or ""),
                                          "progress": prog, "evidence": evidence,
                                          "note_count": (len(notes) if has_notes else 0)})
            status.setdefault("kg", "ok")
        except Exception as ex:
            status["kg"] = f"error: {type(ex).__name__}"

        # ③ Anki:本地 record,不连 AnkiConnect(直接命令要可预期,不吊在外部进程上)。
        #    ⚠ 必须搜 `text` —— cloze 卡的正文在 text 而非 front/back;只搜 front/back
        #    会让"卡片明明在本地却返回空结果"(实测漏了 25 张 cloze)。
        try:
            rec = pdf.CLAUDE_DIR / "anki" / "records"
            if _src_ready(rec, "anki"):
                import json as _j
                for f in sorted(rec.glob("*.json")):
                    try:
                        data = _j.loads(f.read_text("utf-8"))
                    except (OSError, ValueError):
                        continue
                    for c in (data.get("cards") or []) if isinstance(data, dict) else []:
                        if not isinstance(c, dict):
                            continue
                        blob = " ".join(str(c.get(k) or "") for k in
                                        ("front", "back", "text", "cloze", "extra"))
                        if q in blob:
                            shown = (str(c.get("front") or "") or str(c.get("text") or ""))
                            found.append({"source": "anki", "rank": 1, "note": f.stem,
                                          "type": ("cloze" if c.get("text") or c.get("cloze") else "basic"),
                                          "front": shown[:200]})
            status.setdefault("anki", "ok")
        except Exception as ex:
            status["anki"] = f"error: {type(ex).__name__}"

        # 跨源确定性排序:证据强度(mastered→started→索引条目)优先,同级按源名+文本,
        # 保证同一 query 每次返回同一批,不受文件系统枚举顺序影响。
        _SRC = {"kg": 0, "anki": 1, "index": 2}
        found.sort(key=lambda x: (x.get("rank", 9), _SRC.get(x["source"], 9),
                                  str(x.get("title") or x.get("front") or x.get("text") or "")))
        results = [{k: v for k, v in x.items() if k != "rank"} for x in found[:limit]]
        complete = all(v == "ok" for v in status.values()) and bool(status)
        return {"query": q, "results": results, "count": len(results),
                "total": len(found), "truncated": len(found) > len(results),
                "complete": complete,
                "sourceStatus": ("ok" if complete else "partial")}

    H["recall.notes"] = recall_notes

    _assert_no_ai(H.keys())
    missing = sorted(set(DC.ACTIONS) - set(H))
    return H, missing


def register_direct_commands(bp, *, pdf, jsonify, request, session, **inject):
    """在既有 blueprint 上挂两条新路由。不触碰任何已注册的 endpoint。"""
    handlers, missing = build_handlers(pdf, **inject)
    svc = DC.DirectCommandService(handlers)

    def submit():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        body = request.get_json(silent=True) or {}
        act = [s.get("action") for s in (body.get("steps") or [{"action": body.get("action")}])]
        blocked = [a for a in act if a in missing]
        if blocked:
            return jsonify({"contract": DC.CONTRACT, "ok": False,
                            "error": f"动作未接线(本机未注入其确定性底座):{blocked}",
                            "retryable": False, "unwired": missing}), 400
        try:
            _r = svc.submit(body)
            _mirror_failures()          # 失败已入 bus → 镜像进出向日志(Windows 单一订阅源)
            return jsonify(_r)
        except DC.CommandError as e:
            return jsonify({"contract": DC.CONTRACT, "ok": False, "error": str(e),
                            "retryable": False}), 400

    def _mirror_failures():
        """把命令失败事件镜像进出向日志 —— Windows 只需订阅一个源。"""
        try:
            import reader_outgoing_context as _OC  # noqa: F401
            og = getattr(pdf, "_OUTGOING", None) or {}
            jr = og.get("journal")
            if not jr:
                return
            last = getattr(svc, "_mirrored", 0)
            for ev in svc.bus.since(last):
                jr.append("command-failed", {
                    "correlation": ev.get("correlation"), "commandId": ev.get("commandId"),
                    "taskId": ev.get("voiceTask"), "step": ev.get("step"),
                    "retryable": ev.get("retryable"), "error": ev.get("error")})
                svc._mirrored = ev["seq"]
        except Exception:
            pass

    def events():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        try:
            since = int(request.args.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        vt = str(request.args.get("voiceTask") or "")
        return jsonify({"contract": DC.CONTRACT, "ok": True,
                        "cursor": svc.bus.cursor(),
                        "events": svc.bus.since(since, vt)})

    bp.add_url_rule("/api/direct-command", "pdf_api_direct_command", submit, methods=["POST"])
    bp.add_url_rule("/api/direct-events", "pdf_api_direct_events", events, methods=["GET"])
    return {"wired": sorted(handlers), "unwired": missing, "service": svc}
