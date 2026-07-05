"""PDF 阅读器：vault 里的 PDF 用 PDF.js 网页渲染 + 选中文本→翻译/解释/进 QA browser/加笔记。

路由（注册到 app.py 的 register_pdf_reader）：
  GET  /pdf/                                  PDF 列表（vault 下所有 *.pdf）
  GET  /pdf/view?file=<rel>&page=N           阅读器页面
  GET  /pdf/file/<vault_rel_path>            返回 PDF 二进制（content-type application/pdf）
  POST /pdf/api/translate     body: {text, target_lang}     翻译选中文本
  POST /pdf/api/explain       body: {text, context?}        AI 解释选中文本
  POST /pdf/api/to-qa         body: {text, file, page}      跳到 QA browser 带选中内容
  POST /pdf/api/to-note       body: {text, name}            从选中文本创建笔记
  GET  /pdf/api/page-nodes?file=<rel>&page=N                当前页对应的 KG 节点列表
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

from flask import (
    Blueprint, Response, abort, jsonify, redirect, render_template, request,
    send_file, session,
)

# AI 后端复用 _client/core 的 ai_backends + scripts/ai_client
# 同 skilltree.py 已经在 app.py 启动时把 sys.path 加好了

CLAUDE_DIR    = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
OBSIDIAN_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
# PDF_XACCEL=1:用 nginx X-Accel-Redirect 发大 PDF(原生 sendfile+Range,绕开 Werkzeug dev server
# 服务几百 MB 时的卡顿)。需 nginx internal location `/_vault_pdf/`(alias 到 vault)+ www-data 有读权限。
_PDF_XACCEL = os.environ.get("PDF_XACCEL", "").strip() == "1"

# spaCy 本地句法分析（独立 venv，subprocess 调用；装上则语法分析走它、零 AI）
SPACY_PY     = Path(os.environ.get("SPACY_PYTHON", "/home/bwicarus/spacy-venv/bin/python"))
SPACY_SCRIPT = CLAUDE_DIR / "scripts" / "spacy_parse.py"
def _spacy_available() -> bool:
    return SPACY_PY.exists() and SPACY_SCRIPT.exists()

bp = Blueprint("pdf_reader", __name__, url_prefix="/pdf")

# 收藏夹统一化(规格 v5):收藏夹物化成**一本真 EPUB**(state/reader-fav-epub/<fid>.epub),用**完整 EPUB 阅读器**
# (/pdf/fav/open → epub_html_reader.html + epub-html.js)打开 → 手写/侧栏AI助手/高亮/生词/振假名/语法/查词/翻译/插入页
# 全功能天然可用。EPUB 条目=原 section 消毒 HTML(图打包)、PDF 条目=原分辨率页图(打包)+透明可选词层。
# _FAV_BOOK_PREFIX 前缀语义:合成 EPUB rel「资源/收藏夹/<fid>.epub」(_resolve_epub_book 解析回 state 那本)+ 各扫 vault
# 系统对残留 .pdf/.epub 的排除/特判(书架列表/全文搜索排除;pdf_view/reading-pos 零进度;禁自我收藏)。见 references/reader-userpages-favorites.md。
_FAV_BOOK_PREFIX = "资源/收藏夹/"


def _safe_vault_path(rel: str) -> Path | None:
    """防 path traversal：把 rel 解析到 vault 内的绝对路径，超出 vault 返 None。"""
    rel = (rel or "").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    abs_path = (OBSIDIAN_ROOT / rel).resolve()
    try:
        abs_path.relative_to(OBSIDIAN_ROOT.resolve())
    except ValueError:
        return None
    if not abs_path.exists() or not abs_path.is_file():
        return None
    return abs_path


# ── 压缩版（原书不动,压缩版单独存服务器,不进 Obsidian 同步;开关打开才传压缩版）──
_COMPRESSED_DIR = CLAUDE_DIR / "state" / "pdf-compressed"
def _compressed_paths(rel: str):
    """压缩版文件 + 其独立状态文件(键=sha1(vault-rel)[:16];状态独立,不和预处理状态冲突)。"""
    import hashlib
    sha = hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16]
    return _COMPRESSED_DIR / f"{sha}.pdf", _COMPRESSED_DIR / f"{sha}.status.json"
def _compressed_info(rel: str) -> dict:
    """压缩版状态:{exists, compressing, percent, comp_kb, phase, msg, error}。"""
    cf, sf = _compressed_paths(rel)
    info = {"exists": cf.exists(), "compressing": False, "percent": 0, "comp_kb": 0}
    if cf.exists():
        try: info["comp_kb"] = round(cf.stat().st_size / 1024, 1)
        except OSError: pass
    try:
        st = json.loads(sf.read_text("utf-8"))
        info["phase"] = st.get("phase", ""); info["msg"] = st.get("msg", "")
        if st.get("phase") == "error": info["error"] = st.get("error", "")
        if st.get("phase") not in ("done", "error", "", None):
            pid = st.get("pid")
            alive = _pid_alive(pid) if pid else (_time.time() - st.get("updated_at", 0) < 120)
            if alive:
                info["compressing"] = True; info["percent"] = st.get("percent", 0)
    except Exception:
        pass
    return info


def _list_vault_pdfs() -> list[dict]:
    """扫 vault 下所有书:PDF + EPUB。返回 [{rel, name, kind, size_kb, mtime, lastopen, comp_*}, ...]。
    kind: "pdf"(分页阅读器) | "epub"(原生 reflow 阅读器,不转换)。
    排序:最近打开过的在最上(按打开时间倒序);没打开过的退回按文件修改时间倒序。"""
    lo = _lastopen_load()
    out = []
    for p in OBSIDIAN_ROOT.rglob("*.pdf"):
        if p.name.endswith((".orig.pdf", ".compressed.pdf")):
            continue   # 备份/旧式压缩版残留,不当独立书列出
        try:
            rel = p.relative_to(OBSIDIAN_ROOT).as_posix()
            if rel.startswith(_FAV_BOOK_PREFIX):
                continue   # 收藏夹物化书只该出现在「⭐收藏夹」tab,不进「📖书架」普通列表(规格 E)
            st = p.stat()
            ci = _compressed_info(rel)
            out.append({
                "rel": rel,
                "name": p.name,
                "kind": "pdf",
                "dir": str(Path(rel).parent),
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": int(st.st_mtime),
                "lastopen": int(lo.get(rel, 0)),   # 该用户最近打开时间(0=没打开过)
                "comp_exists": ci["exists"],
                "comp_compressing": ci["compressing"],
                "comp_percent": ci["percent"],
            })
        except OSError:
            continue
    for p in OBSIDIAN_ROOT.rglob("*.epub"):
        # EPUB 原生 reflow 阅读:不转 PDF,直接列出,开到 /pdf/epub/view。压缩/预处理等 PDF 专属功能不适用。
        try:
            rel = p.relative_to(OBSIDIAN_ROOT).as_posix()
            st = p.stat()
            out.append({
                "rel": rel,
                "name": p.name,
                "kind": "epub",
                "dir": str(Path(rel).parent),
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": int(st.st_mtime),
                "lastopen": int(lo.get(rel, 0)),
                "comp_exists": False, "comp_compressing": False, "comp_percent": 0,
            })
        except OSError:
            continue
    out.sort(key=lambda x: (-x["lastopen"], -x["mtime"]))   # 用过的置顶(近→远),其余按文件时间
    return out


def _find_kg_nodes_for_page(file_rel: str, page: int) -> list[dict]:
    """查 knowledge_graph/*.json 里 pdf 路径匹配且 pages 含 page 的节点。"""
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    if not kg_dir.exists():
        return []
    out = []
    for kg_f in kg_dir.glob("*.json"):
        if kg_f.name.endswith(".bak.json"):
            continue
        try:
            kg = json.loads(kg_f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # KG 里的 pdf 字段可能是 vault 相对路径或绝对路径
        kg_pdf = (kg.get("pdf") or "").strip()
        if not kg_pdf:
            continue
        # 标准化：相对 vault
        kg_pdf_rel = kg_pdf
        try:
            kp = Path(kg_pdf)
            if kp.is_absolute():
                kg_pdf_rel = kp.relative_to(OBSIDIAN_ROOT).as_posix()
        except (ValueError, OSError):
            pass
        if kg_pdf_rel != file_rel:
            continue
        # 匹配的 KG，找含该 page 的节点
        for n in kg.get("nodes", []):
            if n.get("level") != 2:
                continue
            if page in (n.get("pages") or []):
                out.append({
                    "id": n["id"],
                    "name": n.get("name", ""),
                    "numeric_label": n.get("numeric_label", ""),
                    "state": n.get("state", "locked"),
                    "mastery_level": n.get("mastery_level", 0),
                    "summary": (n.get("summary") or "")[:120],
                    "book": kg.get("book", kg_f.stem),
                    "kg_file": kg_f.name,
                    "kind": kg.get("kind", ""),          # grammar KG 才允许跟踪
                    "tracked": bool(n.get("tracked", False)),
                })
    out.sort(key=lambda x: x["numeric_label"] or "z")
    return out


def _norm_book_key(s: str) -> str:
    """书名/文件名归一化(给 EPUB↔KG 模糊匹配用):小写 + 去登记前缀(000-/十六进制-)+ 去空白标点。"""
    import re as _re
    s = (s or "").strip().lower()
    s = _re.sub(r"^[0-9a-f]{2,4}[-_ ]+", "", s)   # 去 000- / 1a- 之类登记前缀
    s = _re.sub(r"[\s\-_.]+", "", s)              # 去空白/连字符/下划线/点
    return s


def _find_kg_nodes_for_book(file_rel: str) -> list[dict]:
    """按「书归属」匹配 KG(给 EPUB 用)。

    _find_kg_nodes_for_page 是按 obsidian 路径精确匹配 kg.pdf —— EPUB 路径不会进 kg.pdf 字段,
    永远匹配不到。这里改成模糊匹配:用 EPUB 文件名 stem / 所在目录名,跟 kg.get("book")、kg 文件 stem、
    以及 KG 里 pdf 的 stem/目录名 比对(同名书的 PDF/EPUB 通常同目录、book 名一致)。
    匹配到 → 返回该书**整本** level-2 KG 节点(不按页/章);匹配不到 → []。
    节点结构同 _find_kg_nodes_for_page,额外带 is_grammar 布尔。"""
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    if not kg_dir.exists():
        return []
    from pathlib import PurePosixPath
    ep = PurePosixPath(file_rel or "")
    ep_stem = _norm_book_key(ep.stem)
    ep_dir = _norm_book_key(ep.parent.name)
    if not ep_stem and not ep_dir:
        return []
    for kg_f in sorted(kg_dir.glob("*.json")):
        if kg_f.name.endswith(".bak.json"):
            continue
        try:
            kg = json.loads(kg_f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        book = (kg.get("book") or kg_f.stem or "").strip()
        keys = {_norm_book_key(book), _norm_book_key(kg_f.stem)}
        kg_pdf = (kg.get("pdf") or "").strip()
        if kg_pdf:
            pp = PurePosixPath(kg_pdf)
            keys.add(_norm_book_key(pp.stem))         # 同书 PDF 文件名
            keys.add(_norm_book_key(pp.parent.name))  # 同书 PDF 所在目录
        keys.discard("")
        # 命中:EPUB 文件名或所在目录 == 任一 key;或 book(>=3 字)与 EPUB stem 互为子串
        hit = (ep_stem in keys) or (ep_dir in keys)
        if not hit:
            bk = _norm_book_key(book)
            if len(bk) >= 3 and ep_stem and (bk in ep_stem or ep_stem in bk):
                hit = True
        if not hit:
            continue
        is_grammar = (kg.get("kind") == "grammar")
        out = []
        for n in kg.get("nodes", []):
            if n.get("level") != 2:
                continue
            out.append({
                "id": n["id"],
                "name": n.get("name", ""),
                "numeric_label": n.get("numeric_label", ""),
                "state": n.get("state", "locked"),
                "mastery_level": n.get("mastery_level", 0),
                "summary": (n.get("summary") or "")[:120],
                "book": book or kg_f.stem,
                "kg_file": kg_f.name,
                "kind": kg.get("kind", ""),       # grammar KG 才允许跟踪
                "is_grammar": is_grammar,
                "tracked": bool(n.get("tracked", False)),
            })
        out.sort(key=lambda x: x["numeric_label"] or "z")
        return out   # 一本 EPUB 对一本 KG,第一本命中即返回
    return []


_ASSIST_MOD = None
def _assistant():
    """懒加载侧边栏助手模块(脱壳 claude CLI + Gemini 双后端 + 按动作预设)。所有阅读器 AI 调用复用它,
    口径跟助手一致:claude/Gemini 互为兜底 + 用户在 PDF 设置里按功能选后端/型号/深度。"""
    global _ASSIST_MOD
    if _ASSIST_MOD is None:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        sys.path.insert(0, str(CLAUDE_DIR / "_server_deploy"))
        import assistant as _A  # type: ignore
        _ASSIST_MOD = _A
    return _ASSIST_MOD


def _reader_uid() -> str:
    """当前用户 id(跟助手 action-pref 同口径:user_id 优先,回落 username)。非请求上下文 → ''(用出厂默认)。"""
    try:
        from flask import session
        return session.get("user_id") or session.get("username") or ""
    except Exception:
        return ""


def _ai_call(prompt: str, action: str = "explain", uid=None) -> str:
    """同步调用 AI(脱壳 claude / Gemini,按 action 预设 + 互为兜底),返回完整字符串。
    action ∈ {explain, translate, dict}(在 PDF 设置面板可分别配后端/型号/深度)。"""
    A = _assistant()
    return A.reader_ask(prompt, action=action, uid=(_reader_uid() if uid is None else uid))


def _ai_call_stream(prompt: str, action: str = "explain", uid=None):
    """流式版:按 action 预设选后端 yield 文本块(主后端失败→兜底另一边)。
    注意:后台线程(无请求上下文)必须显式传 uid,否则取不到用户预设只能用默认。"""
    A = _assistant()
    yield from A.reader_stream(prompt, action=action, uid=(_reader_uid() if uid is None else uid))


def _dict_sse_stream(word: str, pdf_rel: str = "", page: int = 0, context: str = ""):
    """字典查询 SSE：分阶段输出，让前端能立刻看到 ECDICT 结果，慢源后续追加。

    event 序列：
      ecdict   → {phonetic, translation_zh, definition_en, lemma, forms, freq, pos}
      free     → {phon_us, phon_uk, audio_us, audio_uk, definitions_en[], examples[], synonyms, antonyms, etymology}
      mw       → {phon_us, audio_us, definitions_en[], examples[], pos[]}
      translate → {examples_zh: {en: zh, ...}}  (例句中文，可能多次 yield 增量)
      done     → {vocab_note}
    """
    import json as _json
    import time as _t

    ds, bvn = _vocab_modules()
    if ds is None:
        yield f"event: error\ndata: {_json.dumps({'error': 'vocab modules not loaded'})}\n\n"
        return

    word = (word or "").strip().lower()
    if not word:
        yield f"event: error\ndata: {_json.dumps({'error': 'invalid word'})}\n\n"
        return

    try:
        yield f"event: start\ndata: {_json.dumps({'word': word})}\n\n"

        # 1. ECDICT（本地，~10ms）
        ec = ds.lookup_ecdict(word)
        if not ec:
            yield f"event: error\ndata: {_json.dumps({'error': 'not found in any source'})}\n\n"
            return
        lemma = ec["lemma"]
        forms = ec["forms"]
        # 解析 ec 的中文 / 英文释义
        zh_defs = []
        en_defs = []
        for d in ds._ec_definitions(ec):
            if d.get("zh"):
                pos = d.get("pos") or ""
                zh_defs.append((f"{pos} " if pos else "") + d["zh"])
            elif d.get("en"):
                pos = d.get("pos") or ""
                en_defs.append((f"{pos} " if pos else "") + d["en"])
        ec_payload = {
            "word": word, "lemma": lemma, "forms": forms,
            "phonetic": "/" + ec.get("phonetic", "") + "/" if ec.get("phonetic") else "",
            "translation": "\n".join(zh_defs[:8]),
            "definition": "\n".join(en_defs[:6]),
            "freq_bnc": ec.get("bnc", 0),
            "freq_coc": ec.get("frq", 0),
        }
        yield f"event: ecdict\ndata: {_json.dumps(ec_payload, ensure_ascii=False)}\n\n"

        # 同步追加 lookup-log + 异步触发笔记生成 + 段落扫描（不阻塞响应）
        try:
            _append_lookup_log(word, lemma, pdf_rel, page, context)
            if pdf_rel and page > 0:
                _trigger_vocab_note_async(word, pdf_rel, page, context)
                _trigger_paragraph_exposure_async(pdf_rel, page, lemma)
            else:
                _trigger_vocab_note_async(word, "", 0, "")
        except Exception:
            pass

        # 2 + 3. Free Dictionary + MW Learner 并行（两个网络源同时请求，不再串行等待）
        #         之前 mw 排在 free 后面 → 要等 free 整个跑完(最多 8s)才开始，MW 内容出现极晚。
        #         现在两个请求同时发，谁先回先 yield，总耗时从 free+mw 降到 max(free, mw)。
        import concurrent.futures as _cf
        fd_payload = None
        mw_payload = None
        with _cf.ThreadPoolExecutor(max_workers=2) as _pool:
            _futs = {
                _pool.submit(ds.lookup_free_dict, lemma): "free",
                _pool.submit(ds.lookup_mw_learner, lemma): "mw",
            }
            for _fut in _cf.as_completed(_futs):
                _which = _futs[_fut]
                if _which == "free":
                    try:
                        fd = ds._free_dict_unpack(_fut.result())
                        fd_payload = {
                            "phon_us": fd.get("phon_us", ""), "phon_uk": fd.get("phon_uk", ""),
                            "audio_us": fd.get("audio_us", ""), "audio_uk": fd.get("audio_uk", ""),
                            "definitions_en": [
                                {"pos": d.get("pos") or "", "en": d["en"], "examples": d.get("examples", [])}
                                for d in (fd.get("definitions") or [])[:6]
                            ],
                            "examples": fd.get("examples", [])[:10],
                            "synonyms": fd.get("synonyms", [])[:10],
                            "antonyms": fd.get("antonyms", [])[:10],
                            "etymology": fd.get("etymology", ""),
                        }
                        yield f"event: free\ndata: {_json.dumps(fd_payload, ensure_ascii=False)}\n\n"
                    except Exception as ex:
                        yield f"event: warn\ndata: {_json.dumps({'source': 'free_dict', 'error': str(ex)})}\n\n"
                else:
                    try:
                        mw = ds._mw_unpack(_fut.result(), lemma=lemma)
                        mw_payload = {
                            "phon_us": mw.get("phon_us", ""),
                            "audio_us": ds._mw_audio_url(mw.get("audio_path", "")) if mw.get("audio_path") else "",
                            "definitions_en": [
                                {"pos": d.get("pos") or "", "en": d["en"], "examples": d.get("examples", [])}
                                for d in (mw.get("definitions") or [])
                            ],
                            "examples": mw.get("examples", [])[:15],
                            "pos": mw.get("pos", []),
                        }
                        yield f"event: mw\ndata: {_json.dumps(mw_payload, ensure_ascii=False)}\n\n"
                    except Exception as ex:
                        yield f"event: warn\ndata: {_json.dumps({'source': 'mw', 'error': str(ex)})}\n\n"

        # 4. 例句翻译
        try:
            import re as _re
            from translate import (translate as _tr, _cache_get as _tr_cache_get,
                                   _cache_put as _tr_cache_put, _cfg as _tr_cfg)
            # 收集所有可翻译例句：MW + Free Dict 例句池（前 8 条）
            all_examples = []
            seen = set()
            for d_list in [
                (mw_payload or {}).get("definitions_en", []),
                (fd_payload or {}).get("definitions_en", []),
            ]:
                for d in d_list:
                    for ex in (d.get("examples") or [])[:2]:
                        k = ex.lower()[:60]
                        if k and k not in seen:
                            seen.add(k); all_examples.append(ex)
            for ex in (fd_payload or {}).get("examples", [])[:5]:
                k = ex.lower()[:60]
                if k and k not in seen:
                    seen.add(k); all_examples.append(ex)
            todo = all_examples[:8]

            # 缓存命中的先秒出，未命中的留给后端翻译
            pending = []
            for ex in todo:
                c = _tr_cache_get(ex, "zh-CN")
                if c:
                    yield f"event: translate\ndata: {_json.dumps({'en': ex, 'zh': c}, ensure_ascii=False)}\n\n"
                else:
                    pending.append(ex)

            if pending:
                _tcfg = _tr_cfg()
                _tb = (_tcfg.get("translate_backend") or "auto").strip().lower()
                if _tb == "ai":
                    # 一次 AI 调用翻译所有句子，marker 分隔流式输出 → 先翻好的先显示
                    # （N 次 AI 调用 → 1 次；逐行解析，凑齐一句立刻 yield）
                    _tmodel = (_tcfg.get("translate_model") or "sonnet").strip()
                    _teffort = (_tcfg.get("translate_effort") or "low").strip()
                    _numbered = "\n".join(f"{i+1}‖ {ex}" for i, ex in enumerate(pending))
                    _prompt = (
                        "把下面每个英文例句翻译成简洁自然的中文。严格逐行输出，每行格式为"
                        "「序号‖中文译文」，序号与输入一一对应。只输出译文，不要重复英文原文，"
                        "不要任何解释。\n\n" + _numbered
                    )
                    _emitted = set()
                    def _emit(idx, zh):
                        if idx in _emitted or not (1 <= idx <= len(pending)):
                            return None
                        zh = (zh or "").strip().strip('"“”')
                        if not zh:
                            return None
                        _emitted.add(idx)
                        try: _tr_cache_put(pending[idx-1], "zh-CN", zh, f"ai-{_tmodel}")
                        except Exception: pass
                        return f"event: translate\ndata: {_json.dumps({'en': pending[idx-1], 'zh': zh}, ensure_ascii=False)}\n\n"
                    _buf = ""
                    for _chunk in _ai_call_stream(_prompt, "translate"):
                        _buf += _chunk
                        while "\n" in _buf:
                            _line, _buf = _buf.split("\n", 1)
                            _m = _re.match(r"\s*(\d+)\s*[‖|｜:：.]\s*(.+)", _line)
                            if _m:
                                _ev = _emit(int(_m.group(1)), _m.group(2))
                                if _ev: yield _ev
                    _m = _re.match(r"\s*(\d+)\s*[‖|｜:：.]\s*(.+)", _buf.strip())   # flush 末行
                    if _m:
                        _ev = _emit(int(_m.group(1)), _m.group(2))
                        if _ev: yield _ev
                else:
                    # DeepL / MyMemory：HTTP 源不支持 marker 批量，并发逐句（先回先 yield）
                    with _cf.ThreadPoolExecutor(max_workers=4) as _tpool:
                        _tfuts = {_tpool.submit(_tr, ex): ex for ex in pending}
                        for _tf in _cf.as_completed(_tfuts):
                            ex = _tfuts[_tf]
                            try: zh = _tf.result()
                            except Exception: zh = ""
                            if zh:
                                yield f"event: translate\ndata: {_json.dumps({'en': ex, 'zh': zh}, ensure_ascii=False)}\n\n"
        except Exception as ex:
            yield f"event: warn\ndata: {_json.dumps({'source': 'translate', 'error': str(ex)})}\n\n"

        # 5. done
        vocab_note = f"资源/vocab/{lemma[0]}/{lemma}.md" if lemma else ""
        yield f"event: done\ndata: {_json.dumps({'vocab_note': vocab_note}, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield f"event: error\ndata: {_json.dumps({'error': str(e)})}\n\n"


def _sse_stream(prompt, action="explain", uid=""):
    """SSE generator：把 AI chunks 包成 SSE event 流。"""
    import json as _json
    try:
        yield "event: start\ndata: {}\n\n"
        for chunk in _ai_call_stream(prompt, action, uid):
            yield f"data: {_json.dumps({'text': chunk})}\n\n"
        yield "event: done\ndata: {}\n\n"
    except Exception as e:
        yield f"event: error\ndata: {_json.dumps({'error': str(e)})}\n\n"


# ─── 路由 ─────────────────────────────────────────────────────────────────

@bp.route("/")
def pdf_index():
    pdfs = _list_vault_pdfs()
    return render_template("pdf_index.html", pdfs=pdfs)


def _reader_js_v():
    """reader.js 的 cache-bust 版本 = 已部署静态文件 mtime（每次部署自动变，免手动 bump）。
    Pi:nginx 服务 /var/www/...;本地实例(Windows,无 nginx):Flask 从 _server_deploy/static 服务 → 读那份。"""
    for _p in ("/var/www/html/static/pdf/reader.js",
               str(Path(__file__).resolve().parent / "static" / "pdf" / "reader.js")):
        try:
            return str(int(os.path.getmtime(_p)))
        except Exception:
            continue
    return "1"


def _epub_js_v():
    """EPUB 阅读器静态(epub-reader.js + epub-ai.js)的 cache-bust 版本 = 两者 mtime 最大值。
    跟 reader.js 解耦:改了 epub 的 JS 也能 bust(否则 reader.js 没动 → ?v 不变 → 浏览器用旧缓存)。"""
    mt = 0
    for name in ("epub-reader.js", "epub-ai.js", "epub2.js", "epub2-highlight.js", "epub2-deco.js", "epub2-sentences.js", "epub2-assist.js", "epub2-extra.js", "epub2-grammar.js", "epub-html.js", "rc-core.js", "rc-md.js",
                 "rc-figures.js", "rc-highlight.js", "rc-snippets.js", "rc-result.js", "rc-wordpop.js", "rc-phrasepop.js", "rc-settings.js", "rc-knowledge.js", "rc-assistant.js", "rc-sidedrawer.js", "rc-grammar.js", "rc-stickynote.js", "rc-favorites.js", "rc-userpages.js"):
        for base in ("/var/www/html/static/pdf",
                     str(Path(__file__).resolve().parent / "static" / "pdf")):
            try:
                mt = max(mt, int(os.path.getmtime(os.path.join(base, name)))); break
            except Exception:
                continue
    return str(mt or 1)


def _html_js_v():
    """统一 HTML 阅读器静态(html-reader.js + 全套 rc-*)的 cache-bust 版本 = 各文件 mtime 最大值。
    跟 _epub_js_v 同构,只是把 epub 驱动换成 html-reader.js;rc-* 共享层任一改动也会 bust。"""
    mt = 0
    for name in ("html-reader.js", "rc-core.js", "rc-md.js", "rc-highlight.js",
                 "rc-snippets.js", "rc-result.js", "rc-wordpop.js", "rc-settings.js",
                 "rc-sidedrawer.js", "rc-phrasepop.js"):
        for base in ("/var/www/html/static/pdf",
                     str(Path(__file__).resolve().parent / "static" / "pdf")):
            try:
                mt = max(mt, int(os.path.getmtime(os.path.join(base, name)))); break
            except Exception:
                continue
    return str(mt or 1)


def _pdf_shared_js_v():
    """ui=shared 下条件加载的共享层 cache-bust = 各文件 mtime 最大值。
    不能用 _reader_js_v()(只看 reader.js mtime,改 rc-*/adapter 不 bust)。
    阶段2:加 rc-md/rc-result/rc-wordpop/rc-phrasepop(解释/翻译/对话→rc-result、全词典→rc-wordpop、词组→rc-phrasepop)。
    阶段3:加 rc-figures/rc-highlight(图描述浮层→rc-figures、高亮编辑→rc-highlight)。
    阶段4:加 rc-knowledge(知识点面板→rc-knowledge.renderInto,渲进 PDF 自己的 #kg-nodes,抽屉本体不迁)。
    阶段5:加 rc-assistant(助手 openModelSettings + splitFollowups 分流;renderFollowups/contextCard/抽屉 chrome 推迟,
           故不加 rc-sidedrawer——无 live 消费方,避免死模块)。
    阶段6:加 rc-grammar(语法分析 onGrammarAnalyze/renderGrammarTrackList/loadGrammarHistory/setGrammarView
           四个入口分流到 RC.grammar,跟 EPUB 共用同一套渲染核心,见 18-grammar.js 里的 __uiShared 分支)。
    阶段7:加 rc-settings(⚙ 总设置面板→统一面板,跟 EPUB 同一份内容/行为;openSettings 按 __uiShared 分流,
           原生回填/保存函数经 PdfAdapter.openSettings 的 onFill/onSave 复用,见 21-misc-ai.js)。"""
    mt = 0
    for name in ("rc-core.js", "rc-md.js", "rc-result.js", "rc-wordpop.js", "rc-phrasepop.js",
                 "rc-figures.js", "rc-highlight.js", "rc-knowledge.js", "rc-assistant.js", "rc-grammar.js",
                 "rc-settings.js", "rc-stickynote.js", "rc-favorites.js", "rc-userpages.js", "pdf-adapter.js",
                 "pdf-uishared.js", "pdf-tail.js"):   # 2026-07-06 架构优化:pdf_reader.html 抽出的内联 JS(改它们 → ?v 跳变)
        for base in ("/var/www/html/static/pdf",
                     str(Path(__file__).resolve().parent / "static" / "pdf")):
            try:
                mt = max(mt, int(os.path.getmtime(os.path.join(base, name)))); break
            except Exception:
                continue
    return str(mt or 1)


# ── 图片模式(大型文档网站成熟方案:服务端按页出图,客户端只按需取看到的页,不下载整本 PDF)──
_PAGE_IMG_DIR = CLAUDE_DIR / "state" / "pdf-page-img"

# ── 缓存命中统计(2026-06-10):进程内计数器(gunicorn 单 worker,重启清零)。
# 目的:链路变慢时区分「没命中缓存(该预热/该修键)」vs「网络/渲染本身慢」,不再靠猜。
# 看法:curl https://.../pdf/api/cache-stats(或浏览器直开)。
_CACHE_STATS: dict[str, int] = {}
_CACHE_STATS_SINCE = time.time()

def _cstat(key: str, n: int = 1) -> None:
    _CACHE_STATS[key] = _CACHE_STATS.get(key, 0) + n

@bp.route("/api/cache-stats")
def pdf_api_cache_stats():
    """各层缓存命中/重算计数。键约定 <层>.<结果>:page_img.hit/fallback_ge/fallback_lt/render_sync,
    page_chars.override/hit/compute,sent_tr.hit/miss,grammar.full_hit/sp_hit/spacy_run/ai_run,
    dict_jp.cache/ai。比值异常(hit 远低于预期)= 缓存键漂移/预热缺口,先查键再怪网络。"""
    r = jsonify({"ok": True, "since": int(_CACHE_STATS_SINCE),
                 "uptime_s": int(time.time() - _CACHE_STATS_SINCE),
                 "stats": dict(sorted(_CACHE_STATS.items()))})
    r.headers["Cache-Control"] = "no-store"
    return r

@bp.route("/api/ping")
def pdf_api_ping():
    """连接质量探针:前端定期量 RTT 显示 🟢直连/🟡中继/🔴断(Tailscale 掉中继时用户能一眼归因网络)。"""
    r = jsonify({"ok": True})
    r.headers["Cache-Control"] = "no-store"
    return r


@bp.route("/api/book-meta")
def pdf_api_book_meta():
    """书元数据(图片模式用,不下载 PDF):页数 + 首页尺寸(pt)。"""
    ap = _safe_vault_path(request.args.get("file", ""))
    if not ap:
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    import fitz
    try:
        d = fitz.open(str(ap)); n = d.page_count; r = d[0].rect
        pw, ph = round(r.width, 1), round(r.height, 1); d.close()
        return jsonify({"ok": True, "page_count": n, "page_w": pw, "page_h": ph, "mtime": int(ap.stat().st_mtime)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_SW_JS = r"""// PDF 阅读器 Service Worker(作用域 /pdf/,只拦 /pdf/*):
//   页图 + 文字层 → cache-first(命中零网络,秒开/离线、抗 iOS 清缓存);
//   徽标 page-figures → stale-while-revalidate(秒回缓存 + 后台刷,解决每次重开都要等网络拉徽标)。
//   静态 JS(reader.js / pdf.mjs 在 /static/,超出本 SW 作用域)→ 靠浏览器 HTTP 缓存(nginx immutable)。
const CACHE = 'pdf-cache-v2';
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => (k.startsWith('pdf-pages-') || k.startsWith('pdf-cache-')) && k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});
async function _cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) cache.put(req, resp.clone());
    return resp;
  } catch (err) {
    const any = await cache.match(req);
    if (any) return any;
    throw err;
  }
}
async function _swr(req) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(req);
  const net = fetch(req).then(resp => { if (resp && resp.ok) cache.put(req, resp.clone()); return resp; }).catch(() => null);
  return hit || (await net) || Response.error();
}
self.addEventListener('fetch', (e) => {
  let url;
  try { url = new URL(e.request.url); } catch (_) { return; }
  if (e.request.method !== 'GET') return;
  const p = url.pathname;
  if (p === '/pdf/api/page-image' || p === '/pdf/api/page-chars') { e.respondWith(_cacheFirst(e.request)); return; }
  if (p === '/pdf/api/page-figures') { e.respondWith(_swr(e.request)); return; }   // 徽标:秒回缓存 + 后台更新
});
"""

# ── 跨设备/跨上下文同步阅读器设置+进度+排版(Kindle/Google Books 做法)──
# iOS 把"装到主屏的 PWA"和 Safari 当成独立存储沙箱 → localStorage 不互通。把这些 pdf-* 偏好
# 存服务端(按登录用户),前端进页面先灌入 localStorage、改动防抖回传 → 设置/进度/旋转排版跨端同步。
_PDF_PREFS_DIR = CLAUDE_DIR / "state" / "pdf-prefs"
def _prefs_path():
    import re as _re
    user = (session.get("username") or "anon")
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(user))[:64] or "anon"
    return _PDF_PREFS_DIR / f"{safe}.json"

# ── 「最近打开」按用户记录（书架置顶用过的书）──
_PDF_LASTOPEN_DIR = CLAUDE_DIR / "state" / "pdf-lastopen"
def _lastopen_path():
    import re as _re
    user = (session.get("username") or "anon")
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(user))[:64] or "anon"
    return _PDF_LASTOPEN_DIR / f"{safe}.json"
def _lastopen_load() -> dict:
    try:
        p = _lastopen_path()
        return json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}
def _lastopen_touch(rel: str):
    """打开一本书时戳一下时间（原子写）。rel 用规范化相对路径，跟 _list_vault_pdfs 的 key 一致。"""
    try:
        m = _lastopen_load()
        m[rel] = int(time.time())
        _PDF_LASTOPEN_DIR.mkdir(parents=True, exist_ok=True)
        p = _lastopen_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
        tmp.replace(p)
    except Exception:
        pass

# ── 续读位置(服务端记录,2026-07-03):前端"随时上传现在正在看的页/章"→ 开书跨设备接续 ──
# 全局单文件 sidecar(键=vault 相对路径,值={kind:'pdf'|'epub', pos:int, ts};跟 highlights/favorites
# 等阅读器 sidecar 一样不分用户)。读取不设 GET 轮询:/pdf/view 把记录折进模板 page(__PDF_CFG.page)、
# /pdf/epub/view 注入 EPUB_CFG.serverPos,前端零异步等待。fav 查看页(/pdf/fav/view)零进度状态是既定设计,不接此系统。
_READER_POS_FILE = CLAUDE_DIR / "state" / "reader-positions.json"
_READER_POS_LOCK = __import__("threading").Lock()

def _reading_pos_load() -> dict:
    try:
        d = json.loads(_READER_POS_FILE.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _reading_pos_get(rel: str):
    """rel(vault 相对路径)的服务端续读位置(int:PDF=页码 1-based / EPUB=节 idx 0-based);无记录/不合法 → None。"""
    rec = _reading_pos_load().get(rel)
    try:
        pos = int(rec.get("pos"))
        return pos if pos >= 0 else None
    except (AttributeError, TypeError, ValueError):
        return None

@bp.route("/api/reading-pos", methods=["POST"])
def pdf_api_reading_pos():
    """前端节流上报当前阅读位置 {file, kind:'pdf'|'epub', pos:int}。原子写(tmp+os.replace)+ 进程内锁。
    写入端已各自过校验(EPUB=_jumping+loaded 校验后的 topIdx / PDF=视口交叠最多页),服务端只做基本合法性。"""
    body = request.get_json(silent=True) or {}
    _rel_in = (body.get("file") or "").strip()
    if _rel_in.lstrip("/").startswith(_FAV_BOOK_PREFIX):
        return jsonify({"ok": True, "skipped": "fav"})   # 收藏夹物化书(合成 rel,非 vault 文件):零进度,提前拒收(免 404 刷屏)
    ap = _safe_vault_path(_rel_in)
    if not ap:
        return jsonify({"ok": False, "error": "bad file"}), 404
    kind = "epub" if body.get("kind") == "epub" else "pdf"
    try:
        pos = int(body.get("pos"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad pos"}), 400
    if pos < 0 or pos > 10_000_000:
        return jsonify({"ok": False, "error": "bad pos"}), 400
    rel_clean = ap.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
    if rel_clean.startswith(_FAV_BOOK_PREFIX):
        return jsonify({"ok": True, "skipped": "fav"})   # 收藏夹物化书不记续读位置(规格 D:零进度,服务端拒收更稳)
    try:
        with _READER_POS_LOCK:
            m = _reading_pos_load()
            m[rel_clean] = {"kind": kind, "pos": pos, "ts": int(time.time())}
            _READER_POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _READER_POS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
            tmp.replace(_READER_POS_FILE)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True, "pos": pos})


@bp.route("/api/prefs", methods=["GET", "POST"])
def pdf_api_prefs():
    """GET → {ok, prefs:{key:value}}; POST {patch:{k:v|null}} 合并(null=删) → {ok}。键为前端 localStorage 的 pdf-*。"""
    p = _prefs_path()
    try:
        cur = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        cur = {}
    if request.method == "GET":
        return jsonify({"ok": True, "prefs": cur})
    body = request.get_json(silent=True) or {}
    patch = body.get("patch") or {}
    if not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "bad patch"}), 400
    for k, v in patch.items():
        if not isinstance(k, str):
            continue
        if v is None:
            cur.pop(k, None)
        else:
            cur[str(k)] = v
    try:
        _PDF_PREFS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), "utf-8")
        tmp.replace(p)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True})


@bp.route("/sw.js")
def pdf_sw_js():
    """Service Worker 脚本。**必须从 /pdf/ 下提供**(SW 作用域=其所在目录 → 覆盖 /pdf/api/page-image)。
    no-cache:SW 更新能被及时拉取(改了版本号即生效)。"""
    resp = Response(_SW_JS, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/pdf/"
    return resp


def _render_page_jpg(ap, page: int, w: int, cf: Path) -> bool:
    """渲一页→JPEG 写到 cf(原子)。True=成功。同步路径与后台补渲共用。"""
    import fitz
    d = None
    try:
        d = fitz.open(str(ap))
        if page < 1 or page > d.page_count:
            return False
        p = d[page - 1]; zoom = w / max(1.0, p.rect.width)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        tmp = cf.with_suffix(".jpg.tmp")
        tmp.write_bytes(pix.tobytes("jpg", jpg_quality=78))
        tmp.replace(cf)
        return True
    except Exception:
        return False
    finally:
        try:
            if d: d.close()
        except Exception:
            pass


_BG_RENDERS: set = set()      # 去重在途的后台补渲(单 worker 进程内)


def _spawn_exact_render(ap, page: int, w: int, cf: Path) -> None:
    """后台补渲精确宽度(轻度放大回退时用):本次先回近似图即时显示,下次请求命中精确图。"""
    import threading
    key = str(cf)
    if key in _BG_RENDERS:
        return
    _BG_RENDERS.add(key)

    def _run():
        try:
            _render_page_jpg(ap, page, w, cf)
        finally:
            _BG_RENDERS.discard(key)

    threading.Thread(target=_run, daemon=True).start()


@bp.route("/api/page-image")
def pdf_api_page_image():
    """渲染某页为 JPEG(PyMuPDF,宽 w px),磁盘缓存(键含 mtime)。客户端逐页按需取 → 只下看到的页。

    ⚡ 宽度容差回退(2026-06-10):缓存键含精确宽度,而每台设备/窗口/缩放请求的 w 都差一点 →
    服务器明明有这页(预热过/别的设备渲过)却 miss、现场重渲(实测同一本书被存下 ~85 种宽度,
    只有 SW 看过的页才秒开)。现在精确 miss 时找同页其它宽度:≥请求宽 → 回最小那张(缩小显示零损失);
    ≥70% 请求宽 → 先回它(即时显示)+ 后台补渲精确宽(下次命中);都没有才同步渲。
    客户端 <img> 是显式 CSS 定宽,固有宽度不同显示也正确。"""
    ap = _safe_vault_path(request.args.get("file", ""))
    if not ap:
        abort(404)
    try:
        page = int(request.args.get("page", "1")); w = int(request.args.get("w", "1400"))
    except ValueError:
        abort(400)
    w = max(400, min(w, 3000))   # 限幅(防超大渲染)
    sha = _book_sha(ap); mt = int(ap.stat().st_mtime)
    _PAGE_IMG_DIR.mkdir(parents=True, exist_ok=True)
    cf = _PAGE_IMG_DIR / f"{sha}-p{page}-w{w}-{mt}.jpg"
    if not cf.exists():
        # 宽度容差回退:同页已有的其它宽度
        alts = []
        for f in _PAGE_IMG_DIR.glob(f"{sha}-p{page}-w*-{mt}.jpg"):
            m = re.search(r"-w(\d+)-", f.name)
            if m:
                alts.append((int(m.group(1)), f))
        ge = sorted(a for a in alts if a[0] >= w)            # ≥请求宽:缩小显示,零质量损失
        lt = sorted(a for a in alts if a[0] < w)             # <请求宽:轻度放大可接受
        best = None
        if ge:
            best = ge[0]
        elif lt and lt[-1][0] >= int(w * 0.7):
            best = lt[-1]
            _spawn_exact_render(ap, page, w, cf)             # 后台补精确宽,本次先即时回近似图
        if best is not None:
            _cstat("page_img.fallback_ge" if best[0] >= w else "page_img.fallback_lt")
            resp = send_file(str(best[1]), mimetype="image/jpeg", conditional=True)
            # ≥请求宽=终态可长缓存;放大回退=临时**模糊**图 → no-store(别让浏览器缓存它,否则精确图渲好后
            # 再请求仍被浏览器缓存的模糊图顶掉;前端也会主动补拉清晰图,见 _renderPageImg 的 _scheduleSharpen)
            resp.headers["Cache-Control"] = ("private, max-age=31536000, immutable"
                                             if best[0] >= w else "no-store")
            resp.headers["X-PageImg-Fallback"] = str(best[0])
            return resp
        _cstat("page_img.render_sync")
        if not _render_page_jpg(ap, page, w, cf):
            abort(500)
    else:
        _cstat("page_img.hit")
    resp = send_file(str(cf), mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@bp.route("/view")
def pdf_view():
    rel = request.args.get("file", "")
    try:
        page = int(request.args.get("page") or 0)   # 无 ?page= 深链 → 0,下面折入服务端续读记录
    except ValueError:
        page = 0
    ui_shared = 0 if request.args.get("ui", "") == "legacy" else 1   # 上线:默认走共享 rc-* 层;?ui=legacy 一键回落老 reader.src(逃生口)
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        abort(404)
    # mtime 做 cache-busting 参数:每次 PDF 改了,pdf_url 变 → 浏览器(尤其 iOS Safari PDF.js)必重 fetch
    try:
        mtime = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mtime = 0
    # 压缩版:?compressed=1 且压缩版存在 → 传压缩版(pdf_url 带 &compressed=1 + pdf_size 用压缩版)
    rel_clean = abs_path.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
    _is_fav_book = rel_clean.startswith(_FAV_BOOK_PREFIX)   # 收藏夹物化书:零进度(规格 D)
    if not _is_fav_book:
        _lastopen_touch(rel_clean)   # 戳「最近打开」→ 书架把这本置顶(收藏夹书不进「最近打开」)
    if page < 1 and not _is_fav_book:
        page = _reading_pos_get(rel_clean) or 1   # 无 ?page= → 服务端续读位置作初始页(有 ?page= 的深链优先,不受影响)
    elif page < 1:
        page = 1   # 收藏夹书:不注入 serverPos(不记录停留位置)
    comp_file, _ = _compressed_paths(rel_clean)
    comp_avail = comp_file.exists()
    use_comp = (request.args.get("compressed", "") == "1") and comp_avail
    if use_comp:
        src_mt = int(comp_file.stat().st_mtime); pdf_size_val = comp_file.stat().st_size
        pdf_url = f"/pdf/file/{urllib.parse.quote(rel, safe='/')}?v={src_mt}&compressed=1"
    else:
        pdf_url = f"/pdf/file/{urllib.parse.quote(rel, safe='/')}?v={mtime}"
        pdf_size_val = abs_path.stat().st_size if abs_path.exists() else 0
    from flask import make_response
    resp = make_response(render_template(
        "pdf_reader.html",
        file_rel=rel,
        file_name=Path(rel).name,
        page=page,
        pdf_url=pdf_url,
        pdf_size=pdf_size_val,   # 字节数:前端据此决定小文件整本取/大文件 range
        compressed=(1 if use_comp else 0),     # 当前是否在用压缩版
        comp_avail=(1 if comp_avail else 0),   # 是否存在压缩版(供"加载慢→切压缩版"提示)
        reader_js_v=_reader_js_v(),
        chars_ver=_CHAR_CACHE_VER,   # 并进前端 page-chars 缓存键:改分词逻辑(bump 它)→ 客户端也重取
        ui_shared=ui_shared,         # 默认 1(共享 rc-* 层,已上线);?ui=legacy → 回落老 reader.src(逃生口)
        shared_js_v=_pdf_shared_js_v(),
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@bp.route("/file/<path:rel>")
def pdf_file(rel):
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        abort(404)
    rel_clean = abs_path.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
    # ?compressed=1 且压缩版存在 → 发压缩版(单独存 state/pdf-compressed/,不动原书)
    comp_file, _ = _compressed_paths(rel_clean)
    serve_comp = (request.args.get("compressed", "") == "1") and comp_file.exists()
    # 大文件优先走 X-Accel-Redirect:Flask 只鉴权,文件交给 nginx 原生 sendfile + 原生 Range 发
    # (Werkzeug dev server 服务几百 MB 时慢/卡 → "加载中 1% 卡死";nginx 原生快且并发好)。
    if _PDF_XACCEL:
        try:
            if serve_comp:
                xaccel = "/_compressed_pdf/" + urllib.parse.quote(comp_file.name)   # nginx internal:alias state/pdf-compressed/
            else:
                xaccel = "/_vault_pdf/" + urllib.parse.quote(rel_clean)
            resp = Response()
            resp.headers["X-Accel-Redirect"] = xaccel
            resp.headers["Content-Type"] = "application/pdf"
            # ⚠ 不要在此设 Accept-Ranges:nginx 服务该静态文件时会自己加 → 两份会被合成
            # "bytes, bytes" ≠ "bytes" → PDF.js 判定不支持 range → 回退整本下载大文件 → Load failed/极慢。
            resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
            return resp
        except Exception:
            pass   # 兜底:任何意外 → 回落 send_file
    # conditional=True → 支持 HTTP Range(206),PDF.js 才能逐页流式只取所需字节,
    # 大文件(几百 MB)不必整本下载到浏览器,iPad Safari 不再 OOM。
    resp = send_file(str(comp_file if serve_comp else abs_path), mimetype="application/pdf", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    # 缓存:**immutable + 长 max-age**。URL 带 ?v=<mtime>(文件一变 URL 就变),所以
    # 同一 URL 的字节永不变 → 可让浏览器长期缓存已取的 Range 分块、**重复打开直接命中
    # 本地缓存、零网络往返**(之前 max-age=0/must-revalidate 每块都要回服务器校验,
    # 大图书每次打开反复读)。文件改了 mtime 变 → 新 URL 自然 miss 取新版,不会串味。
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@bp.route("/api/list-pdfs")
def pdf_api_list_pdfs():
    """vault 里所有 PDF 的列表（控制面板新建书本下拉用）。"""
    return jsonify({"ok": True, "pdfs": _list_vault_pdfs()})


# ── 书本预处理（扫描 PDF 补文字层）：检测文字层 → 无则 Google Vision OCR + 嵌入 → 原地替换 ──
# 只编排现有脚本(scripts/{preprocess_book,google_vision_ocr,embed_google_ocr_to_pdf}.py)。
_BOOK_PREPROCESS_DIR = CLAUDE_DIR / "state" / "book-preprocess"

def _book_sha(abs_path) -> str:
    import hashlib
    return hashlib.sha1(str(Path(abs_path).resolve()).encode("utf-8")).hexdigest()[:16]


def _pid_alive(pid) -> bool:
    """进程是否还活着（用于判断预处理后台进程是否崩了/被 OOM 杀）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 存在但无权发信号 → 仍算活着
    except Exception:
        return False
    # **僵尸(Z)算死**:os.kill(pid,0) 对僵尸仍成功(pid 还在表里)→ 否则被杀/崩溃后没被父进程
    # 回收的子进程会被误判"还活着",进度条永远卡在最后一步、还挡住重新启动(2026-06 踩)。
    try:
        st = open(f"/proc/{pid}/stat", "rb").read().rsplit(b")", 1)[1].split()[0]
        if st in (b"Z", b"X", b"x"):   # Z=僵尸 X/x=已死
            return False
    except Exception:
        pass
    return True


@bp.route("/api/preprocess-status")
def pdf_api_preprocess_status():
    """预处理状态：读 state/book-preprocess/<sha>.json（文件驱动 → 关网页/webapp 重启都不丢）。
    {phase: idle|detecting|ocr|embedding|done|error, percent, msg, completed, total, has_text, error}"""
    ap = _safe_vault_path(request.args.get("file", ""))
    if not ap:
        return jsonify({"phase": "idle"})
    try:
        st = json.loads((_BOOK_PREPROCESS_DIR / f"{_book_sha(ap)}.json").read_text("utf-8"))
    except Exception:
        return jsonify({"phase": "idle"})
    # 存活检测：进行中的相位但后台进程已退出（崩溃/OOM/被杀）→ 别让进度条永远卡着，报错
    if st.get("phase") in ("detecting", "normalizing", "ocr", "embedding", "compressing"):
        pid = st.get("pid")
        stale = (_time.time() - st.get("updated_at", 0)) > 30
        if pid is not None and stale and not _pid_alive(pid):
            st = {**st, "phase": "error",
                  "error": "预处理进程已中断（可能内存不足或被终止），请重试"}
    return jsonify(st)


@bp.route("/api/preprocess-async", methods=["POST"])
def pdf_api_preprocess_async():
    """启动预处理：detached 子进程跑 scripts/preprocess_book.py（关网页/重启不中断；在跑则不重复启）。"""
    import subprocess
    body = request.get_json(silent=True) or {}
    ap = _safe_vault_path((body.get("file") or "").strip())
    if not ap or not ap.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    sha = _book_sha(ap)
    sp = _BOOK_PREPROCESS_DIR / f"{sha}.json"
    try:
        st = json.loads(sp.read_text("utf-8"))
        # 真正在跑（进程活着 / 或刚更新过且没记 pid 的老状态）才拦重复启动；
        # 死进程留下的陈旧 in-progress 状态允许直接重跑。
        if st.get("phase") in ("detecting", "normalizing", "ocr", "embedding", "compressing"):
            pid = st.get("pid")
            fresh = (_time.time() - st.get("updated_at", 0)) < 120
            alive = _pid_alive(pid) if pid is not None else fresh
            if alive:
                return jsonify({"ok": True, "already": True, "phase": st.get("phase")})
    except Exception:
        pass
    engine = (body.get("engine") or "vision").strip()
    if engine not in ("vision", "manga"):
        engine = "vision"
    enhance = bool(body.get("enhance"))
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = [py, str(CLAUDE_DIR / "scripts" / "preprocess_book.py"),
           "--pdf", str(ap), "--engine", engine]
    if enhance:
        cmd.append("--enhance")
    try:
        subprocess.Popen(cmd, cwd=str(CLAUDE_DIR), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动失败：{ex}"}), 500
    return jsonify({"ok": True, "sha": sha, "engine": engine, "enhance": enhance})


# ── 整本预热(页图 + 字符层/振假名)：本地实例后台一次渲好全书 → 之后任意翻页秒开。──
# 按当前显示宽度渲(width-specific),detached 子进程(关网页/翻页不中断),状态文件去重防重复启。
_PREWARM_DIR = CLAUDE_DIR / "state" / "pdf-prewarm"
_EREADER_DIR = CLAUDE_DIR / "state" / "pdf-ereader"   # 电子版生成进度文件


def _prewarm_status_path(sha: str, w: int):
    return _PREWARM_DIR / f"{sha}-w{w}.json"


@bp.route("/api/prewarm-async", methods=["POST"])
def pdf_api_prewarm_async():
    """启动整本预热:scripts/prewarm_pdf.py(先渲全部页图@width,再算全部字符层)。在跑则不重复启。"""
    import subprocess, time
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    ap = _safe_vault_path(rel)
    if not ap:
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    try:
        w = max(400, min(int(body.get("width") or 1260), 3000))
    except (TypeError, ValueError):
        w = 1260
    sha = _book_sha(ap)
    _PREWARM_DIR.mkdir(parents=True, exist_ok=True)
    sp = _prewarm_status_path(sha, w)
    try:                                  # 已在跑 → 不重复启
        st = json.loads(sp.read_text("utf-8"))
        if st.get("pid") and _pid_alive(st["pid"]):
            return jsonify({"ok": True, "already_running": True, "width": w})
    except Exception:
        pass
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = [py, str(CLAUDE_DIR / "scripts" / "prewarm_pdf.py"),
           "--pdf", str(ap), "--rel", rel, "--width", str(w)]
    # 后台预热降到低优先级:别让整本渲染抢满 CPU 拖慢交互请求(翻页/查词/返回选书页)。
    popen_kw = dict(cwd=str(CLAUDE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        popen_kw["creationflags"] = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
    else:
        popen_kw["start_new_session"] = True
        cmd = ["nice", "-n", "19"] + cmd
    try:
        p = subprocess.Popen(cmd, **popen_kw)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动失败：{ex}"}), 500
    try:
        sp.write_text(json.dumps({"pid": p.pid, "width": w, "ts": int(time.time())}), "utf-8")
    except Exception:
        pass
    return jsonify({"ok": True, "started": True, "width": w, "pid": p.pid})


@bp.route("/api/prewarm-status")
def pdf_api_prewarm_status():
    """整本预热进度:按已缓存页图张数(@该宽度)/总页数算 percent;附后台进程是否在跑。"""
    rel = (request.args.get("file") or "").strip()
    ap = _safe_vault_path(rel)
    if not ap:
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    try:
        w = max(400, min(int(request.args.get("width") or 1260), 3000))
    except (TypeError, ValueError):
        w = 1260
    sha = _book_sha(ap); mt = int(ap.stat().st_mtime)
    try:
        import fitz
        total = fitz.open(str(ap)).page_count
    except Exception:
        total = 0
    done = len(list(_PAGE_IMG_DIR.glob(f"{sha}-p*-w{w}-{mt}.jpg"))) if _PAGE_IMG_DIR.exists() else 0
    running = False
    try:
        st = json.loads(_prewarm_status_path(sha, w).read_text("utf-8"))
        running = bool(st.get("pid") and _pid_alive(st["pid"]))
    except Exception:
        pass
    pct = round(100.0 * done / total, 1) if total else 0
    return jsonify({"ok": True, "total": total, "done": done, "percent": pct, "running": running, "width": w})


def _ereader_out_path(ap: Path) -> Path:
    """电子版输出路径:跟原书**同目录**、名字加 `.电子版`(不自动优先打开,用户手动选)。"""
    return ap.parent / (ap.stem + ".电子版.pdf")


@bp.route("/api/ereader-async", methods=["POST"])
def pdf_api_ereader_async():
    """生成「电子版」PDF:后台 detached 低优先级跑 scripts/make_ereader_pdf.py(整本)。
    产物落原书同目录 `<名>.电子版.pdf`(纯文字重排 + 图/公式裁图,打开快;不自动优先打开)。在跑则不重复启。"""
    import subprocess, time
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    ap = _safe_vault_path(rel)
    if not ap:
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    if ap.stem.endswith(".电子版"):
        return jsonify({"ok": False, "error": "这已经是电子版了"}), 400
    out = _ereader_out_path(ap)
    sha = _book_sha(ap)
    _EREADER_DIR.mkdir(parents=True, exist_ok=True)
    sp = _EREADER_DIR / f"{sha}.json"
    try:                                  # 已在跑 → 不重复启
        st = json.loads(sp.read_text("utf-8"))
        if st.get("pid") and _pid_alive(st["pid"]) and st.get("status") == "running":
            return jsonify({"ok": True, "already_running": True})
    except Exception:
        pass
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = [py, str(CLAUDE_DIR / "scripts" / "make_ereader_pdf.py"),
           str(ap), str(out), "--progress", str(sp)]
    popen_kw = dict(cwd=str(CLAUDE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        popen_kw["creationflags"] = 0x00004000 | 0x08000000   # BELOW_NORMAL | NO_WINDOW
    else:
        popen_kw["start_new_session"] = True
        cmd = ["nice", "-n", "19"] + cmd
    try:
        p = subprocess.Popen(cmd, **popen_kw)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动失败：{ex}"}), 500
    try:
        sp.write_text(json.dumps({"pid": p.pid, "status": "running", "done": 0, "total": 0,
                                  "ts": int(time.time())}), "utf-8")
    except Exception:
        pass
    return jsonify({"ok": True, "started": True, "pid": p.pid})


@bp.route("/api/ereader-status")
def pdf_api_ereader_status():
    """电子版生成进度:读进度文件(done/total/status) + 产物是否已生成。"""
    rel = (request.args.get("file") or "").strip()
    ap = _safe_vault_path(rel)
    if not ap:
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    out = _ereader_out_path(ap)
    sha = _book_sha(ap)
    st = {}
    try:
        st = json.loads((_EREADER_DIR / f"{sha}.json").read_text("utf-8"))
    except Exception:
        pass
    done, total = int(st.get("done") or 0), int(st.get("total") or 0)
    status = st.get("status") or ""
    running = bool(st.get("pid") and _pid_alive(st["pid"]) and status == "running")
    pct = st.get("percent")                          # 脚本算好的综合百分比(抽取/排版/收尾三段)
    if pct is None:
        pct = round(100.0 * done / total, 1) if total else 0
    exists = out.exists()
    out_rel = ""
    if exists:
        try:
            out_rel = out.relative_to(OBSIDIAN_ROOT).as_posix()
        except Exception:
            out_rel = out.name
    return jsonify({"ok": True, "running": running, "status": status, "percent": pct,
                    "done": done, "total": total, "phase": st.get("phase", ""), "error": st.get("error", ""),
                    "exists": exists, "out_name": out.name, "out_rel": out_rel})


@bp.route("/api/compress-async", methods=["POST"])
def pdf_api_compress_async():
    """智能压缩：detached 子进程跑 scripts/compress_pdf.py(gs 降采样图像+保留文字层+重线性化)。
    复用 book-preprocess 状态文件 → 进度条/刷新恢复/重复启动守卫跟预处理共用一套。"""
    import subprocess
    body = request.get_json(silent=True) or {}
    ap = _safe_vault_path((body.get("file") or "").strip())
    if not ap or not ap.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    sha = _book_sha(ap)
    sp = _BOOK_PREPROCESS_DIR / f"{sha}.json"
    try:                                  # 已在跑(预处理/压缩任一)且进程活着 → 不重复启
        st = json.loads(sp.read_text("utf-8"))
        if st.get("phase") in ("detecting", "normalizing", "ocr", "embedding", "compressing"):
            pid = st.get("pid")
            fresh = (_time.time() - st.get("updated_at", 0)) < 120
            if (_pid_alive(pid) if pid is not None else fresh):
                return jsonify({"ok": True, "already": True, "phase": st.get("phase")})
    except Exception:
        pass
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = [py, str(CLAUDE_DIR / "scripts" / "compress_pdf.py"), "--pdf", str(ap)]
    try:
        subprocess.Popen(cmd, cwd=str(CLAUDE_DIR), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动失败：{ex}"}), 500
    return jsonify({"ok": True, "sha": sha})


@bp.route("/api/compressed-status")
def pdf_api_compressed_status():
    """压缩版状态:{ok, exists, compressing, percent, comp_kb, orig_kb, phase, msg, error}。"""
    ap = _safe_vault_path(request.args.get("file", ""))
    if not ap:
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    rel_clean = ap.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
    info = _compressed_info(rel_clean); info["ok"] = True
    try: info["orig_kb"] = round(ap.stat().st_size / 1024, 1)
    except OSError: pass
    return jsonify(info)


@bp.route("/api/compress-make", methods=["POST"])
def pdf_api_compress_make():
    """生成**压缩版**到单独文件(不动原书,原书仍是 OCR + 默认/好网传输源);后台 detached 跑
    compress_pdf.py --out --status(独立状态文件,不和预处理冲突)。"""
    import subprocess
    body = request.get_json(silent=True) or {}
    ap = _safe_vault_path((body.get("file") or "").strip())
    if not ap or not ap.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    rel_clean = ap.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
    comp_file, status_file = _compressed_paths(rel_clean)
    if comp_file.exists():
        return jsonify({"ok": True, "already": True, "exists": True})
    if _compressed_info(rel_clean).get("compressing"):
        return jsonify({"ok": True, "already": True, "compressing": True})
    comp_file.parent.mkdir(parents=True, exist_ok=True)
    py = os.environ.get("APP_PYTHON") or sys.executable
    mp = str(int(body.get("max_px") or 1150)); q = str(int(body.get("quality") or 55))   # 1920px 源→~1150px q55 ≈ 省 55%
    cmd = [py, str(CLAUDE_DIR / "scripts" / "compress_pdf.py"),
           "--pdf", str(ap), "--out", str(comp_file), "--status", str(status_file),
           "--max-px", mp, "--quality", q]
    try:
        subprocess.Popen(cmd, cwd=str(CLAUDE_DIR), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动失败：{ex}"}), 500
    return jsonify({"ok": True, "compressing": True})


@bp.route("/api/delete-pdf", methods=["POST"])
def pdf_api_delete_pdf():
    """删除一本书：删 PDF + 清 sidecar(OCR/预处理/备份)。路径必须在 vault 内。"""
    import shutil
    body = request.get_json(silent=True) or {}
    ap = _safe_vault_path((body.get("file") or "").strip())
    if not ap or not ap.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    sha = _book_sha(ap)
    try:
        ap.unlink()
    except Exception as ex:
        return jsonify({"ok": False, "error": f"删除失败：{ex}"}), 500
    for path in [_BOOK_PREPROCESS_DIR / f"{sha}.json",
                 _BOOK_PREPROCESS_DIR / f"{sha}.orig.pdf",
                 _BOOK_PREPROCESS_DIR / f"{sha}.embedded.pdf",
                 CLAUDE_DIR / "state" / "google-vision-ocr" / sha,
                 CLAUDE_DIR / "state" / "mokuro-ocr" / sha]:
        try:
            if path.is_dir(): shutil.rmtree(path, ignore_errors=True)
            elif path.exists(): path.unlink()
        except Exception:
            pass
    return jsonify({"ok": True})


def _migrate_book_sidecars(old_rel: str, new_rel: str, old_abs: Path, new_abs: Path):
    """重命名 PDF 时迁移所有按 rel/绝对路径哈希命名的 sidecar 到新 key。
    用户数据(高亮/墨迹/翻译/隐藏句/语法跟踪)+贵产物(预处理 OCR)迁移;字符/振假名缓存按 mtime
    命名(rename 保留 mtime)也顺手迁;langs/crop 共享 json 改 key。漏迁的纯缓存会自动重建。"""
    import hashlib, shutil
    def _h(s):   return hashlib.sha1(s.encode("utf-8")).hexdigest()
    def _h16(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
    o_full, n_full = _h(old_rel), _h(new_rel)
    o16, n16 = _h16(old_rel), _h16(new_rel)
    o_bsha, n_bsha = _book_sha(old_abs), _book_sha(new_abs)

    def _mv(src: Path, dst: Path):
        try:
            if src.exists() and src.resolve() != dst.resolve():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                src.replace(dst)
        except Exception:
            pass

    # 用户数据(整 sha1(rel) 命名)
    for d in (_HL_DIR, _INK_DIR, _TR_DIR, _DISMISS_DIR):
        _mv(d / f"{o_full}.json", d / f"{n_full}.json")
    # 语法跟踪(sha1(rel)[:16])
    _mv(_GRAMMAR_TRACKED_DIR / f"{o16}.json", _GRAMMAR_TRACKED_DIR / f"{n16}.json")
    # 贵产物:预处理(绝对路径 sha)+ 备份/嵌入 pdf + OCR 目录
    for suffix in (".json", ".orig.pdf", ".embedded.pdf"):
        _mv(_BOOK_PREPROCESS_DIR / f"{o_bsha}{suffix}", _BOOK_PREPROCESS_DIR / f"{n_bsha}{suffix}")
    for base in (CLAUDE_DIR / "state" / "google-vision-ocr", CLAUDE_DIR / "state" / "mokuro-ocr"):
        _mv(base / o_bsha, base / n_bsha)
    # 按页缓存(sha16-p{page}-{mtime}.json):字符层 + 振假名验证(rename 保留 mtime → key 仍有效)
    for cdir in (CLAUDE_DIR / "state" / "pdf-char-cache", _FURIFIX_DIR):
        try:
            for f in cdir.glob(f"{o16}-p*"):
                _mv(f, cdir / (n16 + f.name[len(o16):]))
        except Exception:
            pass
    # 共享 json:langs + crop + 插图开关 改 key
    for path in (_BOOK_LANGS_PATH, _BOOK_CROP_PATH, _BOOK_FIG_PATH):
        try:
            if path.exists():
                m = json.loads(path.read_text("utf-8"))
                if old_rel in m:
                    m[new_rel] = m.pop(old_rel)
                    path.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass


@bp.route("/api/rename-pdf", methods=["POST"])
def pdf_api_rename_pdf():
    """重命名一本书：改文件名(保留所在目录)+ 迁移所有 sidecar。路径必须在 vault 内。"""
    body = request.get_json(silent=True) or {}
    old_rel = (body.get("file") or "").strip()
    old_ap = _safe_vault_path(old_rel)
    if not old_ap or not old_ap.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 400
    new_name = (body.get("new_name") or "").strip().replace("/", "").replace("\\", "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "新名称非法"}), 400
    if not new_name.lower().endswith(".pdf"):
        new_name += ".pdf"
    new_ap = old_ap.parent / new_name
    new_rel = new_ap.relative_to(OBSIDIAN_ROOT).as_posix()
    if new_ap.resolve() == old_ap.resolve():
        return jsonify({"ok": True, "rel": old_rel, "name": new_name})
    if new_ap.exists():
        return jsonify({"ok": False, "error": "同名文件已存在"}), 400
    try:
        old_ap.rename(new_ap)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"重命名失败：{ex}"}), 500
    _migrate_book_sidecars(old_rel, new_rel, old_ap, new_ap)
    return jsonify({"ok": True, "rel": new_rel, "name": new_name})


@bp.route("/api/preprocess-active")
def pdf_api_preprocess_active():
    """列出当前进行中的预处理任务(列表页刷新后据此恢复进度条+轮询)。
    扫 book-preprocess/*.json,phase∈(detecting/ocr/embedding) 且进程没死的。"""
    out = []
    try:
        for sp in _BOOK_PREPROCESS_DIR.glob("*.json"):
            try:
                st = json.loads(sp.read_text("utf-8"))
            except Exception:
                continue
            if st.get("phase") not in ("detecting", "normalizing", "ocr", "embedding", "compressing"):
                continue
            pid = st.get("pid")
            stale = (_time.time() - st.get("updated_at", 0)) > 30
            if pid is not None and stale and not _pid_alive(pid):
                continue   # 死进程,不算进行中
            pdf = st.get("pdf")
            if not pdf:
                continue
            try:
                rel = Path(pdf).resolve().relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
            except Exception:
                continue
            out.append({"rel": rel, "phase": st.get("phase"),
                        "percent": st.get("percent", 0), "msg": st.get("msg", "")})
    except Exception:
        pass
    return jsonify({"ok": True, "active": out})


# 连字(ligature)展开：PyMuPDF 把 ﬁ/ﬂ 等当单个非 ASCII 字形输出，前端词边界正则 [A-Za-z]
# 不认它 → 单词在连字处被断(如 infinitely 只能选到 nitely)。提取时还原成 ASCII。
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st",
}


# 日语分词(fugashi/MeCab + unidic-lite)：CJK 行 override char.w，前端单击选词逻辑零改动复用。
# Tagger 全局缓存(thread-safe,Flask threaded 模式 OK),import 失败 fallback 跳过分词。
_JP_TAGGER = None
_JP_TAGGER_TRIED = False


def _get_jp_tagger():
    global _JP_TAGGER, _JP_TAGGER_TRIED
    if not _JP_TAGGER_TRIED:
        _JP_TAGGER_TRIED = True
        try:
            from fugashi import Tagger
            _JP_TAGGER = Tagger()
        except Exception as ex:
            sys.stderr.write(f"[jp tokenize] fugashi 不可用,跳过日语分词: {ex}\n")
            _JP_TAGGER = None
    return _JP_TAGGER


def _is_cjk_char(c: str) -> bool:
    if not c:
        return False
    o = ord(c[0])
    # 汉字 / 假名 / CJK 标点 / 兼容汉字
    return (0x3000 <= o <= 0x303F) or (0x3040 <= o <= 0x30FF) or \
           (0x3400 <= o <= 0x4DBF) or (0x4E00 <= o <= 0x9FFF) or \
           (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFFEF)


# 日「X日」读 か(訓読み)的数字：2-10、14、20、24（ふつか…とおか/じゅうよっか/はつか/にじゅうよっか）；其余読 にち
_JP_DAY_KA = {2, 3, 4, 5, 6, 7, 8, 9, 10, 14, 20, 24}


def _kata_to_hira(s: str) -> str:
    """片假名 → 平假名（长音符 ー 等非假名原样保留）。"""
    out = []
    for ch in s or "":
        o = ord(ch)
        out.append(chr(o - 0x60) if 0x30A1 <= o <= 0x30F6 else ch)
    return "".join(out)


def _is_kanji_ch(c: str) -> bool:
    if not c:
        return False
    o = ord(c[0])
    return (0x3400 <= o <= 0x4DBF) or (0x4E00 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF)


def _is_kana_ch(c: str) -> bool:
    if not c:
        return False
    o = ord(c[0])
    return (0x3040 <= o <= 0x309F) or (0x30A0 <= o <= 0x30FF)


def _furigana_item(surface: str, reading: str, tchars: list) -> dict | None:
    """为含汉字的 token 生成振假名条目：**完整读音**放在整个词上方(不剥送り仮名)。
    学习者要看全部读音(役立つ→やくだつ,つ 也要;之前剥成 やくだ 缺了 つ)。
    surface=token 表层, reading=平假名读音, tchars=该 token 的非空格 char dict。
    返回 {x0,y0,x1,y1,rt} (PDF pt 坐标) 或 None(无汉字/读音无效)。"""
    if not reading or "*" in reading or not tchars:
        return None
    if not any(_is_kanji_ch(c) for c in surface):
        return None   # 纯假名/纯符号 → 不需要振假名
    # token 可能跨行(如 間食:間 在行尾、食 在行首,按块分词才读对 かんしょく)。振假名只放在
    # 首字所在那一行的连续片段上方,否则 bbox 纵向跨两行、读音飘在行间。单行 token 不受影响。
    try:
        ref = tchars[0]
        rh = max(0.1, ref["y1"] - ref["y0"])
        row = []
        for c in tchars:
            if abs(c["y0"] - ref["y0"]) <= rh * 0.6:
                row.append(c)
            else:
                break
        rc = row or tchars
        x0 = min(c["x0"] for c in rc); y0 = min(c["y0"] for c in rc)
        x1 = max(c["x1"] for c in rc); y1 = max(c["y1"] for c in rc)
    except (ValueError, KeyError, IndexError):
        return None
    return {"x0": round(x0, 2), "y0": round(y0, 2),
            "x1": round(x1, 2), "y1": round(y1, 2), "rt": reading, "wd": surface}


# 日语助动词/助词 → 中文语法标签（变形分析用）。lemma 或 surface 命中即取。
_JP_AUX_ZH = {
    "た": "过去/完成", "だ": "过去/完成",
    "ない": "否定", "ぬ": "否定(书)", "ん": "否定(口语)", "ず": "否定(文语)",
    "ます": "敬体(礼貌)", "です": "敬体(礼貌)",
    "たい": "愿望(想…)", "たがる": "(他人)想…",
    "れる": "被动/可能/自发/尊敬", "られる": "被动/可能/自发/尊敬",
    "せる": "使役(让…)", "させる": "使役(让…)",
    "う": "意志/推量(吧)", "よう": "意志/推量(吧)", "まい": "否定意志",
    "らしい": "推测(似乎)", "そう": "样态/传闻", "よう": "比况/推测",
    "て": "て形(接续/请求/进行)", "で": "て形(接续)",
    "ば": "假定(如果…就)", "たら": "假定(…的话)", "なら": "假定(要是)",
    "ください": "请求(请…)", "下さる": "请求(请…)", "くださる": "请求(请…)",
    "ちゃう": "完了/不经意(口语)", "じゃう": "完了/不经意(口语)",
    "って": "引用/口语(=と/と言って)", "という": "称为/叫做",
    "ながら": "一边…一边", "つつ": "一边…一边(文)",
}
# 动词/形容词自身活用形（无助动词时兜底；只标有意义的，基本形/连体形不啰嗦）
_JP_CFORM_ZH = {
    "未然形": "未然形(否定/意志接续)", "連用形": "连用形(中止/接续)",
    "仮定形": "假定形(ば)", "命令形": "命令形(命令)",
    "意志推量形": "意志/推量形",
}


def _jp_inflection(text: str) -> dict | None:
    """用 fugashi 分析选中日语文本的变形：还原原形 + 列出变形语法标签(中文)。
    返回 {base, marks:[中文标签]} 或 None(无动词/形容词=不是变形,如纯名词)。"""
    tagger = _get_jp_tagger()
    s = (text or "").strip()
    if tagger is None or not s:
        return None
    try:
        toks = list(tagger(s))
    except Exception:
        return None
    base = None
    cform = ""
    marks: list = []
    for i, w in enumerate(toks):
        f = w.feature
        p1 = (getattr(f, "pos1", "") or "")
        surf = w.surface or ""
        lemma = (getattr(f, "lemma", "") or surf)
        if base is None and p1 in ("動詞", "形容詞", "形容動詞"):
            # サ变(名詞+する):原形 = 名詞+する(稼働した→稼働する),不要显示成 為る
            if lemma in ("為る", "する") and i > 0 and (getattr(toks[i - 1].feature, "pos1", "") == "名詞"):
                base = (toks[i - 1].surface or "") + "する"
            else:
                base = lemma
            cform = (getattr(f, "cForm", "") or "")
        elif p1 in ("助動詞", "助詞"):
            z = _JP_AUX_ZH.get(lemma) or _JP_AUX_ZH.get(surf)
            if z and z not in marks:
                marks.append(z)
    if base is None:
        return None   # 没动词/形容词 → 不是变形（纯名词等不显示）
    # 没助动词标记时，用动词/形容词自身活用形兜底（基本形/终止形/连体形不啰嗦）
    if not marks and cform:
        head = cform.split("-")[0]
        z = _JP_CFORM_ZH.get(head)
        if z:
            marks.append(z)
    if base == s and not marks:
        return None   # 就是原形、无变形 → 不显示
    return {"base": base, "marks": marks}


def _stitch_latin_words(chars: list[dict]) -> None:
    """删掉西文词内的合成空格(字距拉开 tracking 时 PyMuPDF 会在词内插一个空格 glyph)。
    判据:某空格的「同行前一个非空格 char」与「后一个非空格 char」都是单个 ASCII 字母,
    且二者 w 相同且 ≥0(PyMuPDF 把它们归为同一个词)→ 这是词内空格 → 删。
    保留真正的词间空格(前后 w 不同)。否则像「Web」会被切出 surface「W eb」:
    含空格 → 前端单词正则 /^[A-Za-z]+$/ 不通过 → 落到词组工具条、查不出单词、无「已掌握」。"""
    n = len(chars)
    _SP = (" ", " ", " ", " ", " ", " ", " ")
    drop = set()
    for i, c in enumerate(chars):
        if not c.get("sp") or (c.get("c") or "") not in _SP:
            continue
        p = i - 1
        while p >= 0 and chars[p].get("sp"):
            p -= 1
        q = i + 1
        while q < n and chars[q].get("sp"):
            q += 1
        if p < 0 or q >= n:
            continue
        a, b = chars[p], chars[q]
        bk = c.get("bk")
        if a.get("bk") != bk or b.get("bk") != bk:
            continue
        ca, cb = a.get("c", ""), b.get("c", "")
        if not (len(ca) == 1 and ca.isascii() and ca.isalpha()):
            continue
        if not (len(cb) == 1 and cb.isascii() and cb.isalpha()):
            continue
        wa, wb = a.get("w", -1), b.get("w", -1)
        if wa >= 0 and wa == wb:
            drop.add(i)
    if drop:
        chars[:] = [c for i, c in enumerate(chars) if i not in drop]


def _apply_cjk_singleton(seg: list, block_i: int, col_i: int) -> None:
    """非日语 CJK(中文 / 未设语言)书:给每个 CJK 字一个**独立** word id → 单击选一字、拖选任意范围,
    不被日语分词器(fugashi/unidic)按日语词典把两个汉字强行粘成一个词。非 CJK(英文词)保持原 _word_id 成组。"""
    if not seg:
        return
    for n, c in enumerate(seg):
        if not c.get("sp") and _is_cjk_char(c.get("c", "")):
            c["w"] = block_i * 1000000 + col_i * 1000 + n


def _apply_jp_tokenize(seg: list, block_i: int, col_i: int,
                       furigana_out: list | None = None) -> None:
    """对一列(块内 gutter 切出的列,如漫画一个气泡)的 chars 用 fugashi 分词,覆盖其 w 字段。
    seg = 该列的 char dict 列表(已按 reading order)。word_id = block*1e6 + col*1e3 + word_no。
    furigana_out 非空时顺带把含汉字 token 的振假名条目 append 进去(读音来自 unidic kana)。
    分词失败/未装 fugashi 静默跳过(前端 w 还是英语 lookup 结果或 -1)。"""
    line_i = col_i   # word_id 用列号占原 line 槽位(块内列唯一即可)
    if not seg:
        return
    # 该列至少含 1 个 CJK 字符才跑分词(纯英语列走原 _word_id 逻辑)
    if not any(_is_cjk_char(c.get("c", "")) for c in seg if not c.get("sp")):
        return
    tagger = _get_jp_tagger()
    if tagger is None:
        return
    # fugashi 输出 token 跳空格,但 PDF chars 含空格(PyMuPDF reflow 自动加,或用户嵌入)→
    # 用 sp-filtered chars 跟 fugashi 字数对齐(seg 中的 dict ref 不变,改 ['w'] 仍 propagate)
    text_chars = [c for c in seg if not c.get("sp")]
    line_text = "".join(c.get("c", "") for c in text_chars)
    if not line_text.strip():
        return
    try:
        char_ptr = 0
        chain_wid = None      # 当前动词链的 w（助动词/接续助词て/补助动词 并入它 → 单击选整个变形词）
        chain_active = False
        prev_surf = ""        # 上一 token 表层（日计数器读音判定用）
        prev_pos1 = ""        # 上一 token 词性（サ变 名詞+する 合并判定）
        prev_wid = None
        prev_is_num = False   # 上一 token 是数字（量词读音随数字变，只对 数字+量词 标 ctx）
        for wn, w in enumerate(tagger(line_text)):
            surf = w.surface or ""
            wlen = len(surf)
            if wlen == 0:
                continue
            f = w.feature
            p1 = (getattr(f, "pos1", "") or "")
            p2 = (getattr(f, "pos2", "") or "")
            lemma = (getattr(f, "lemma", "") or "")
            my_wid = block_i * 1000000 + line_i * 1000 + wn
            # 动词链合并：助动词(た/ない/ます/られ…) / 接续助词(て/で) / 补助动词(ている的いる) 并入前面动词
            attachable = (p1 == "助動詞") or (p1 == "助詞" and p2 == "接続助詞") or (p1 == "動詞" and "非自立" in p2)
            is_suru = (p1 == "動詞" and lemma in ("為る", "する"))
            if is_suru and prev_pos1 == "名詞":
                wid = prev_wid; chain_wid = prev_wid; chain_active = True   # サ变:名詞+する 合并成一个词(稼働する)
            elif chain_active and attachable:
                wid = chain_wid
            elif p1 in ("動詞", "形容詞", "形容動詞"):
                wid = my_wid; chain_wid = my_wid; chain_active = True
            else:
                wid = my_wid; chain_active = False; chain_wid = None
            tok_chars = []
            for j in range(wlen):
                idx = char_ptr + j
                if 0 <= idx < len(text_chars):
                    text_chars[idx]["w"] = wid
                    tok_chars.append(text_chars[idx])
            if furigana_out is not None and tok_chars:
                reading = ""
                try:
                    reading = _kata_to_hira(getattr(f, "kana", "") or "")
                except Exception:
                    reading = ""
                # 日计数器读音修正:unidic 接尾辞「日」默认 カ(三日=みっか对),但阿拉伯数字+日
                # 多数读 にち(365日)。数字不在 native-か 集合(2-10,14,20,24)→ にち。
                if surf == "日" and reading == "か" and p1 == "接尾辞" and prev_surf.isdigit():
                    n = int(prev_surf)
                    reading = "か" if n in _JP_DAY_KA else ("ついたち" if n == 1 else "にち")
                item = _furigana_item(surf, reading, tok_chars)
                if item:
                    # 量词(接尾辞,如 日/人/本/月/回)读音随前面数字变(365日=にち vs 三日=みっか)，
                    # unidic 常给错 → 标 ctx=数字+量词，留给 AI 按上下文校正(通用,不硬编码每个量词)。
                    # 只对【数字+量词】标(排除 正規化/正規形 这类名词后缀,它们读音本就对)
                    if p1 == "接尾辞" and prev_is_num and prev_surf:
                        item["ctx"] = prev_surf + surf
                    furigana_out.append(item)
            _is_num = (p2 == "数詞") or surf.isdigit() or bool(surf) and all(ch in "〇零一二三四五六七八九十百千万億兆" for ch in surf)
            prev_surf = surf; prev_pos1 = p1; prev_wid = wid; prev_is_num = _is_num
            char_ptr += wlen
    except Exception as ex:
        sys.stderr.write(f"[jp tokenize] fail col {col_i}: {ex}\n")


def _split_block_columns(block_chars: list) -> list:
    """把一个 PyMuPDF block 的 chars 按竖直空隙(gutter)分成左右并排列。
    漫画并排气泡常被 Vision/PyMuPDF 并成一个块、按视觉行左右交错读 → 选中/分词分不开;
    检测块内贯穿的竖直空白条(gutter,宽 > 1.5×字宽)在那里切列。
    返回 [列chars, ...](左→右;列内保持原 reading order)。无明显空隙 → 原样一列。"""
    ns = [c for c in block_chars if not c.get("sp") and c.get("c", "").strip()]
    if len(ns) < 4:
        return [block_chars]
    chw = statistics.median([max(1.0, c["x1"] - c["x0"]) for c in ns]) or 1.0
    # CJK 列内字紧挨(字间 gap≈0),并排气泡间隙只有 ~1 字宽 → 小阈值才切得开,且不会误切;
    # Latin 有词间空格(~0.5 字宽)→ 阈值要大,免得在词缝处误切(真双栏间隙很大仍能切)。
    cjk_ratio = sum(1 for c in ns if _is_cjk_char(c.get("c", ""))) / len(ns)
    cjk = cjk_ratio > 0.5
    tol = chw * (0.45 if cjk else 1.2)         # 同列内允许的字/词间小缝(合并成一簇)
    gutter_min = chw * (0.8 if cjk else 3.0)   # 列间空隙阈值(≥它才分列)
    merged: list = []
    for a, b in sorted((c["x0"], c["x1"]) for c in ns):
        if merged and a <= merged[-1][1] + tol:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    cols = [list(merged[0])]
    for a, b in merged[1:]:
        if a - cols[-1][1] >= gutter_min:    # 空隙够宽 → 新列
            cols.append([a, b])
        elif b > cols[-1][1]:
            cols[-1][1] = b
    if len(cols) <= 1:
        return [block_chars]
    bounds = [(cols[i][1] + cols[i + 1][0]) / 2 for i in range(len(cols) - 1)]
    groups: list = [[] for _ in cols]
    for c in block_chars:
        cx = (c["x0"] + c["x1"]) / 2
        gi = len(cols) - 1
        for i, bd in enumerate(bounds):
            if cx < bd:
                gi = i
                break
        groups[gi].append(c)
    return [g for g in groups if g]


def _build_en_furigana(chars: list) -> list:
    """英文词音标叠加：把连续字母 char 拼成词，单连接直查 ECDICT phonetic（不做 lemma/LIKE，
    避免逐词全表扫描）。返回 [{x0,y0,x1,y1,rt}]（rt = IPA，无斜杠）。"""
    import sqlite3
    db = _DICT_DB_PATH
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception:
        return []
    cur = conn.cursor()
    phon_cache: dict = {}

    def _phon(word: str) -> str:
        key = word.lower()
        if key in phon_cache:
            return phon_cache[key]
        p = ""
        try:
            cur.execute("SELECT phonetic FROM stardict WHERE word=? COLLATE NOCASE LIMIT 1", (key,))
            r = cur.fetchone()
            if r and r[0]:
                p = r[0].strip()
        except Exception:
            p = ""
        phon_cache[key] = p
        return p

    out: list = []
    cur_chars: list = []

    def _flush():
        if len(cur_chars) >= 2:
            word = "".join(c["c"] for c in cur_chars)
            if word.isascii() and any(ch.isalpha() for ch in word):
                p = _phon(word)
                if p:
                    try:
                        x0 = min(c["x0"] for c in cur_chars); y0 = min(c["y0"] for c in cur_chars)
                        x1 = max(c["x1"] for c in cur_chars); y1 = max(c["y1"] for c in cur_chars)
                        out.append({"x0": round(x0, 2), "y0": round(y0, 2),
                                    "x1": round(x1, 2), "y1": round(y1, 2), "rt": p, "wd": word})
                    except (ValueError, KeyError):
                        pass
        cur_chars.clear()

    prev = None
    for ch in chars:
        c = ch.get("c", "")
        # 跨行/跨大间距断词（同 _build_vocab_marks 逻辑的简化版）
        if prev and not prev.get("sp") and not ch.get("sp") and cur_chars:
            ph = max(0.1, prev["y1"] - prev["y0"])
            if abs(ch["y0"] - prev["y0"]) > ph * 0.5:
                _flush()
        if ch.get("sp"):
            _flush()
        elif (c.isascii() and c.isalpha()) or c in "'-":
            cur_chars.append(ch)
        else:
            _flush()
        prev = ch
    _flush()
    try:
        conn.close()
    except Exception:
        pass
    return out


def _compute_page_chars(abs_path, page: int, is_ja: bool = True):
    """提取该页所有字符 bbox + 词 id(rawdict + PyMuPDF words + 分词)。
    只依赖 (文件内容, 页码, is_ja) → 可缓存。返回 (chars, page_w, page_h, furigana) 或 None(越界)。
    is_ja=True(日语书):fugashi 分词 + 振假名;is_ja=False(中文/无语言书):每个汉字独立词 id(自由选,
    不被日语词典强行把两个汉字粘成一个词)。furigana = 振假名/音标叠加(日语 unidic 读音 + 英文 ECDICT 音标)。"""
    import fitz
    doc = fitz.open(str(abs_path))
    try:
        if page > len(doc):
            return None
        p = doc[page - 1]
        raw = p.get_text("rawdict")
        words_raw = p.get_text("words")   # (x0,y0,x1,y1, text, block_no, line_no, word_no)
        _wbuckets: dict = {}
        for _wi, _w in enumerate(words_raw):
            _wid = _w[5] * 1000000 + _w[6] * 1000 + _w[7]
            _yk = int(_w[1] // 5)
            for _k in (_yk - 1, _yk, _yk + 1):
                _wbuckets.setdefault(_k, []).append((_w[0], _w[1], _w[2], _w[3], _wid))
        def _word_id(cx, cy):
            for (wx0, wy0, wx1, wy1, wid) in _wbuckets.get(int(cy // 5), ()):  # noqa
                if wy0 - 0.5 <= cy <= wy1 + 0.5 and wx0 - 0.5 <= cx <= wx1 + 0.5:
                    return wid
            return -1
        chars = []
        furigana: list = []
        _col_seq = 0   # 全局列 id：每个 gutter 切出的列一个唯一 bk(并排气泡分开)
        for _block_i, block in enumerate(raw.get("blocks", [])):
            if block.get("type", 0) != 0:
                continue
            _block_start = len(chars)
            for _line_i, line in enumerate(block.get("lines", [])):
                for span in line.get("spans", []):
                    _bold = bool(span.get("flags", 0) & 16) or "bold" in (span.get("font", "") or "").lower()
                    for ch in span.get("chars", []):
                        bbox = ch.get("bbox")
                        if not bbox or len(bbox) != 4:
                            continue
                        c = ch.get("c", "")
                        if not c:
                            continue
                        c = _LIGATURES.get(c, c)   # 还原连字 ﬁ→fi，免得词在此被断
                        chars.append({
                            "c": c,
                            "x0": round(bbox[0], 2), "y0": round(bbox[1], 2),
                            "x1": round(bbox[2], 2), "y1": round(bbox[3], 2),
                            "sp": 1 if c.isspace() else 0,
                            "w": _word_id((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2),
                            "b": 1 if _bold else 0,
                            "bk": _block_i,
                        })
            # 块内按竖直空隙切列(漫画并排气泡分开)。每列:① 给唯一 bk → 选中/句子框不串气泡;
            # ② 整列一起 fugashi 分词(列内各行连续 reading order,跨行词如 間食 读对 かんしょく)。
            block_chars = chars[_block_start:]
            for _col in _split_block_columns(block_chars):
                cur_bk = _col_seq
                _col_seq += 1
                for c in _col:
                    c["bk"] = cur_bk
                if is_ja:
                    _apply_jp_tokenize(_col, _block_i, cur_bk % 1000, furigana)
                else:
                    _apply_cjk_singleton(_col, _block_i, cur_bk % 1000)   # 中文书:每汉字独立词 → 自由选
        # 西文词内合成空格清理(字距拉开 → "Web" 被切成 surface "W eb")。须在分词后、furigana 前。
        _stitch_latin_words(chars)
        # 英文词音标叠加（单连接直查 ECDICT；日语为主的书几乎无开销）
        try:
            furigana.extend(_build_en_furigana(chars))
        except Exception as ex:
            sys.stderr.write(f"[furigana en] fail: {ex}\n")
        return chars, p.rect.width, p.rect.height, furigana
    finally:
        doc.close()


_CHAR_CACHE_VER = 10  # chars 缓存 schema 版本。改抽取/分词逻辑就 +1 → 旧缓存全部失效重算
                      # (v2:坏缓存; v3:完整读音; v4:动词链+日计数器; v5:サ变+wd; v6:量词 furigana 加 ctx;
                      #  v7:按块分词[跨行词如 間食 读对音] + 跨行 token 振假名只放首行段;
                      #  v8:块内 gutter 切列[漫画并排气泡分开 bk]→ 选中/分词不串气泡;
                      #  v10:中文/无语言书每汉字独立词 id[不再套日语分词把两字粘一起],日语书才走 fugashi)


def _page_chars_cached(abs_path, rel: str, page: int):
    """带磁盘缓存的 chars 提取。缓存键含 mtime → PDF 改了自动失效。
    缓存的是「只依赖文件+页」的不变部分(chars/page_w/page_h);vocab_marks/句子框
    依赖可变的掌握度/译文/删除态,不缓存,每次从 chars 现算(便宜,无 fitz/rawdict)。
    返回 (chars, page_w, page_h) 或 None。"""
    ovr = _page_ocr_override_load(rel, page)   # 单页重扫覆盖:有则直接用(绕过 PDF 嵌入文字层)
    if ovr is not None:
        _cstat("page_chars.override")
        return ovr["chars"], ovr["page_w"], ovr["page_h"], ovr.get("furigana", [])
    import hashlib
    try:
        mtime = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mtime = 0
    is_ja = "ja" in (_book_langs_for(rel) or [])   # 日语书才走 fugashi 分词;中文/无语言书每汉字独立选
    cdir = CLAUDE_DIR / "state" / "pdf-char-cache"
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    cpath = cdir / f"{sha}-p{page}-{mtime}-{'ja' if is_ja else 'zh'}.json"   # 语言进缓存键:改书语言→换 key 重算
    if cpath.exists():
        try:
            d = json.loads(cpath.read_text("utf-8"))
            if d.get("cver") == _CHAR_CACHE_VER:   # 版本不符(或老缓存) → 落到重算
                _cstat("page_chars.hit")
                return d["chars"], d["page_w"], d["page_h"], d.get("furigana", [])
        except Exception:
            pass   # 缓存损坏 → 重算
    _cstat("page_chars.compute")
    res = _compute_page_chars(abs_path, page, is_ja=is_ja)
    if res is None:
        return None
    chars, pw, ph, furigana = res
    # 安全阀:别缓存「分词失败」的结果。fugashi 不可用(tagger None) → 该页 CJK 全 w=-1;
    # 或有汉字却没产出振假名 = 分词没跑成。否则 fugashi 临时挂掉时写的坏缓存(全 w=-1)会被
    # 之后一直命中 → 整页单字选中(本次 bug 根因)。纯假名页(无汉字)无振假名属正常,不拦。
    # 安全阀只对日语书有意义(非日语书不跑 fugashi,tagger 挂不挂都不影响选中)
    if is_ja:
        tagger_down = _get_jp_tagger() is None
        has_kanji = any(_is_kanji_ch(c.get("c", "")) for c in chars if not c.get("sp"))
        has_cjk = any(_is_cjk_char(c.get("c", "")) for c in chars if not c.get("sp"))
        if (tagger_down and has_cjk) or (has_kanji and not furigana):
            sys.stderr.write(f"[page-chars] p{page} 分词未成(tagger_down={tagger_down}),跳过缓存等 fugashi 恢复\n")
            return chars, pw, ph, furigana
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps({"chars": chars, "page_w": pw, "page_h": ph,
                                     "furigana": furigana, "cver": _CHAR_CACHE_VER},
                                    ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return chars, pw, ph, furigana


_FORMULA_INJECT_VER = 1   # 公式字符层注入逻辑版本(改注入规则就 +1 → cv 变 → 旧缓存失效)


def _apply_formula_chars(chars, furigana, rel, page, page_w, page_h):
    """把公式 OCR 的 LaTeX 注入字符层(用户要的"公式文字层直接替换为 OCR 结果")。
    字符层是隐藏选择层(视觉靠页图),所以这一步只改"选中公式时拿到什么":
      ① 删掉公式 bbox 内的原扫描乱码字符 + 落在框内的振假名;
      ② 塞入 LaTeX 串(切片平铺满整框、同一词 id w、标 fml=1),单击选公式 → 直接得 $...$。
    随 sidecar latex 改而变 → cv 已含 fig sidecar mtime,前端缓存自动失效。"""
    try:
        abs_path = _safe_vault_path(rel)
        if not abs_path:
            return
        data = _fig_load_abs(abs_path)
    except Exception:
        return
    fmls = [f for f in (data.get("formulas") or [])
            if f.get("page") == page and (f.get("latex") or "").strip()
            and f.get("bbox") and len(f.get("bbox")) == 4]
    if not fmls:
        return
    WID, BK = 950000000, 950000
    for fi, f in enumerate(fmls):
        x0n, y0n, x1n, y1n = f["bbox"]
        fx0, fy0, fx1, fy1 = x0n * page_w, y0n * page_h, x1n * page_w, y1n * page_h

        def _inside(bx0, by0, bx1, by1):
            cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
            return fx0 <= cx <= fx1 and fy0 <= cy <= fy1
        # 删原乱码字符 + 框内振假名
        chars[:] = [c for c in chars if not _inside(c["x0"], c["y0"], c["x1"], c["y1"])]
        if isinstance(furigana, list) and furigana:
            furigana[:] = [r for r in furigana
                           if not (all(k in r for k in ("x0", "y0", "x1", "y1"))
                                   and _inside(r["x0"], r["y0"], r["x1"], r["y1"]))]
        lx = f["latex"].strip()
        wrapped = ("$$" + lx + "$$") if f.get("multiline") else ("$" + lx + "$")
        n = len(wrapped)
        if not n:
            continue
        slice_w = max(0.5, (fx1 - fx0) / n)
        wid, bk = WID + fi, BK + fi
        for i, cc in enumerate(wrapped):
            sx0 = fx0 + i * slice_w
            chars.append({
                "c": cc, "x0": round(sx0, 2), "y0": round(fy0, 2),
                "x1": round(sx0 + slice_w, 2), "y1": round(fy1, 2),
                "sp": 0, "w": wid, "b": 0, "bk": bk,
                "fml": 1, "flx": (lx if i == 0 else ""),
            })


@bp.route("/api/page-chars")
def pdf_api_page_chars():
    """提取该页所有字符的精确 bbox（PDF 坐标）。
    前端按这个画 char-level overlay 接管选中事件，绕开 PDF.js textLayer 字符位置不准。

    返回：{chars: [{c, x0, y0, x1, y1}], page_w, page_h}
    """
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        import fitz  # noqa: F401
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF not installed"}), 500
    try:
        res = _page_chars_cached(abs_path, rel, page)
        if res is None:
            return jsonify({"ok": False, "error": "page out of range"}), 400
        chars, page_w, page_h, furigana = res
        _apply_char_offset(chars, _char_offset_for(rel, page))   # 文字层校准:偏移 live 应用(不进磁盘缓存)
        _merge_favorite_phrases(chars)   # 收藏词组合并 w（单击选中整词组）
        furigana = _merge_favorite_phrases_furigana(furigana)   # 收藏词组按整体读音合并振假名(一条 ruby)
        _apply_formula_chars(chars, furigana, rel, page, page_w, page_h)   # 公式区域文字层替换为 OCR LaTeX(选中公式直接得 $...$)
        _apply_ocr_corrections(chars, furigana, rel, page, page_w, page_h)   # 选区 OCR 校正:坏文字层永久修正(注入正确文字)
        # **只回不变部分**(字 bbox/分词/振假名);可变的生词/句子框走 /page-overlay。
        # 可缓存:前端带 &cv=<内容版本>(偏移/重扫/PDF 改 → cv 变 → 换 key → 不会陈旧);
        # /pdf/sw.js 缓存命中即本地(读过的书秒开/离线),没命中才回 Pi。
        resp = jsonify({
            "ok": True,
            "chars": chars,
            "page_w": page_w,
            "page_h": page_h,
            "furigana": furigana,
        })
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@bp.route("/api/ocr-selection", methods=["POST"])
def pdf_api_ocr_selection():
    """选区重新识别:坏文字层时,把选区裁成图发 Claude 视觉精确转写,回正确文字。
    body {file, page(PDF页), bbox:[x0,y0,x1,y1](PDF pt), model?, effort?}。只读图回字,不落库。"""
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    page = int(body.get("page") or 0)
    bbox = body.get("bbox") or []
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1 or len(bbox) != 4:
        return jsonify({"ok": False, "error": "参数缺失"}), 400
    try:
        import fitz
        doc = fitz.open(str(abs_path))
        try:
            if page > len(doc):
                return jsonify({"ok": False, "error": "页码越界"}), 400
            pr = doc[page - 1].rect; W = float(pr.width); H = float(pr.height)
        finally:
            doc.close()
        x0, y0, x1, y1 = [float(v) for v in bbox]
        if x1 - x0 < 0.5 or y1 - y0 < 0.5:
            return jsonify({"ok": False, "error": "选区太小"}), 400
        # padding:选区并集已含上标/下标字符的 bbox,只需极小留白防边缘字形被切;
        # 横向留太多会把相邻字的一半圈进来 → Claude 把半个字也当完整字转写(如"径"尾→"金");
        # 纵向留太多会把上下相邻行也圈进图。
        padx = 1.0
        pady = (y1 - y0) * 0.12 + 2.0
        nb = [max(0.0, (x0 - padx) / W), max(0.0, (y0 - pady) / H),
              min(1.0, (x1 + padx) / W), min(1.0, (y1 + pady) / H)]
        # 小区域高 DPI、大选区降档:让裁图长边 ~1500px(视觉甜区,又不至于过大)
        longpt = max((nb[2] - nb[0]) * W, (nb[3] - nb[1]) * H) or 1.0
        scale = max(2.0, min(5.0, 1500.0 / longpt))
        png = _figure_crop_png(abs_path, page, nb, scale=scale)
        # 2026-07 收口:不再收 request 的 model/effort 覆盖(以前也只是死参数)。模型走「看图」action 预设。
        text = _claude_ocr_crop(png)
        if not text:
            return jsonify({"ok": False, "error": "OCR 没结果,再试一次"}), 502
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
        text = re.sub(r"\n?```$", "", text).strip()
        # 持久化:用**原始选区 bbox**(不含 padding)归一化存校正 → 注入字符层,重选/复制/翻译永久生效
        cv = None
        try:
            _ocrfix_add(abs_path, page, [x0 / W, y0 / H, x1 / W, y1 / H], text)
            cv = _page_content_version(abs_path, rel, page)   # 新 cv → 前端据此立即重载本页字符层
        except Exception as ex:
            sys.stderr.write(f"[ocr-fix] save fail p{page}: {ex}\n")
        return jsonify({"ok": True, "text": text, "cv": cv})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:160]}), 500


def _page_content_version(abs_path, rel: str, page: int) -> str:
    """该页内容版本签名:PDF mtime + 分词版本 + 偏移 + 单页重扫覆盖。任一变 → cv 变 → 前端换缓存 key。"""
    import hashlib
    try:
        mt = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mt = 0
    ofs = _char_offset_for(rel, page)
    ovr = _page_ocr_override_sig(rel, page)   # 单页重扫覆盖签名(无则空)
    try:
        ph = int(os.path.getmtime(str(_PHRASES_PATH)))   # 收藏词组改 → 合并 w 变 → cv 必须变
    except Exception:
        ph = 0
    try:
        pm = int(os.path.getmtime(str(_PHRASE_MARK_PATH)))   # 词组掌握态改 → chars 的 favm 变 → cv 也得变
    except Exception:
        pm = 0
    try:
        fm = int(os.path.getmtime(str(_fig_path_abs(abs_path))))   # 公式 sidecar 改(latex 填好/改) → 公式字符层变 → cv 必须变
    except Exception:
        fm = 0
    try:
        om = int(os.path.getmtime(str(_ocrfix_path_abs(abs_path))))   # 选区 OCR 校正 sidecar 改 → 注入字符变 → cv 必须变
    except Exception:
        om = 0
    try:
        lm = int(os.path.getmtime(str(_BOOK_LANGS_PATH)))   # 书语言改(中文↔日语)→ 分词粒度变 → cv 必须变
    except Exception:
        lm = 0
    sig = f"{_CHAR_CACHE_VER}|{mt}|{ofs['dx']},{ofs['dy']},{ofs['scale']}|{ovr}|{ph}|{pm}|f{_FORMULA_INJECT_VER}:{fm}|o{_OCRFIX_INJECT_VER}:{om}|l{lm}"
    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]


@bp.route("/api/page-overlay")
def pdf_api_page_overlay():
    """该页**可变**叠层数据(生词下划线/句子框)+ 偏移 + 内容版本 cv。no-store(实时,随掌握度/译文变)。
    前端先拉它拿 cv,再用 cv 拉可缓存的 /page-chars。"""
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        import fitz  # noqa: F401
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF not installed"}), 500
    try:
        res = _page_chars_cached(abs_path, rel, page)
        if res is None:
            return jsonify({"ok": False, "error": "page out of range"}), 400
        chars, page_w, page_h, furigana = res
        _apply_char_offset(chars, _char_offset_for(rel, page))
        _merge_favorite_phrases(chars)
        _aj = _page_allows_ja(chars, rel)
        vocab_marks = _build_vocab_marks(chars)
        if _aj:
            vocab_marks += _build_jp_vocab_marks(chars)
        sentences = _build_unmastered_sentences(chars, page_h=page_h, allow_ja=_aj)
        for ts in _tr_load(rel):
            if ts.get("rects") and ts.get("page", page) == page:
                sentences.append(ts)
        _dis = _dismiss_load(rel)
        if _dis:
            sentences = [s for s in sentences if (s.get("text") or "").strip() not in _dis]
        resp = jsonify({
            "ok": True,
            "vocab_marks": vocab_marks,
            "vocab_sentences": sentences,
            "offset": _char_offset_for(rel, page),
            "cv": _page_content_version(abs_path, rel, page),
        })
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


def _page_allows_ja(chars: list[dict], rel: str) -> bool:
    """是否对该页做日语分词/匹配。声明了 ja → 是；否则按**假名比例**判：中文书纯汉字(无假名)→ 否
    (免中文/日语共用汉字时，中文书的汉字撞上日语词库 → 误下划线/误框)。日语正文假名占比高。"""
    if "ja" in (_book_langs_for(rel) or []):
        return True
    kana = cjk = 0
    for c in chars:
        ch = c.get("c", "")
        if not ch:
            continue
        o = ord(ch[0])
        if 0x3040 <= o <= 0x30FF:   # 平/片假名
            kana += 1; cjk += 1
        elif 0x4E00 <= o <= 0x9FFF:   # 汉字
            cjk += 1
    return cjk > 0 and (kana / cjk) > 0.06   # 有相当比例假名 = 日语；纯汉字(中文) → 不当日语


def _build_vocab_marks(chars: list[dict]) -> list[dict]:
    """扫 chars 识别英文词；命中 vocab index 的标记下划线。
    返回 marks 用 **PDF pt 坐标 rects**（跟 hl-saved 一样），不依赖 char idx，
    跟前端 chars sort 与否无关。掌握的词 (label_slug='mastered') 跳过。"""
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        import vocab_index  # type: ignore
    except Exception:
        return []
    idx = vocab_index.index()
    if not idx:
        return []
    marks: list[dict] = []
    cur_letters: list[str] = []
    cur_chars: list[dict] = []

    def _flush():
        if not cur_letters or not cur_chars:
            cur_letters.clear(); cur_chars.clear(); return
        word = "".join(cur_letters).lower()
        info = idx.get(word)
        cur_letters.clear()
        chars_snap = cur_chars[:]
        cur_chars.clear()
        if any(c.get("favm") for c in chars_snap):
            return   # 属于已掌握收藏词组的字 → 整条不画(即便单词本身在生词库)
        if not info or not info.get("label_slug"):
            return
        if info["label_slug"] == "mastered":
            return   # 掌握的不画
        # 合并同行 chars 成 rect 列表（pt 坐标）
        rects: list[list[float]] = []
        cur_rect = None
        for c in chars_snap:
            lineH = c["y1"] - c["y0"]
            if cur_rect and abs(c["y0"] - cur_rect[1]) <= lineH * 0.5:
                cur_rect[2] = max(cur_rect[2], c["x1"])
                cur_rect[1] = min(cur_rect[1], c["y0"])
                cur_rect[3] = max(cur_rect[3], c["y1"])
            else:
                if cur_rect: rects.append([round(x,2) for x in cur_rect])
                cur_rect = [c["x0"], c["y0"], c["x1"], c["y1"]]
        if cur_rect: rects.append([round(x,2) for x in cur_rect])
        marks.append({
            "word": word, "lemma": info["lemma"],
            "mastery": round(info["mastery"], 3),
            "label_slug": info["label_slug"],
            "rects": rects,
        })

    prev: dict | None = None
    for ch in chars:
        c = ch.get("c", "")
        # 跨行检测：PyMuPDF rawdict 在行尾不插空格，要手动断词（避免 "been\npresented" → "beenpresented"）
        if prev and not prev.get("sp") and not ch.get("sp"):
            prev_h = max(0.1, prev["y1"] - prev["y0"])
            if abs(ch["y0"] - prev["y0"]) > prev_h * 0.5:
                # 跨行：如果上一个字符是 hyphen → 行尾连字符 "pre-\nsented"，去 hyphen 继续拼
                if cur_letters and cur_letters[-1] == "-":
                    cur_letters.pop()
                    if cur_chars: cur_chars.pop()
                else:
                    _flush()
        if ch.get("sp"):
            _flush(); prev = ch; continue
        if c.isalpha() or c in "'-":
            cur_letters.append(c); cur_chars.append(ch)
        else:
            _flush()
        prev = ch
    _flush()
    return marks


# ── 日语生词 store（state/jp-vocab.json）：查过的 JP 词按熟悉度高亮，镜像英语生词系统 ──
import threading as _jp_threading
_JP_VOCAB_PATH = CLAUDE_DIR / "state" / "jp-vocab.json"
_JP_VOCAB_LOCK = _jp_threading.Lock()


def _jp_vocab_load() -> dict:
    try:
        return json.loads(_JP_VOCAB_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _jp_vocab_save(d: dict):
    try:
        _JP_VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _JP_VOCAB_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        tmp.replace(_JP_VOCAB_PATH)
    except Exception:
        pass


def _jp_vocab_is_trackable(word: str) -> bool:
    """是否值得记入生词库(画下划线)。过滤垃圾:跨行残片(含空白)、单假名助词(を/た/の)。
    规则:无空白 + (含汉字 或 ≥2 个假名)。单个假名/单符号不记。"""
    w = (word or "").strip()
    if not w or any(ch.isspace() for ch in w):
        return False
    has_kanji = any(_is_kanji_ch(ch) for ch in w)
    return has_kanji or len(w) >= 2


def _jp_vocab_bump(word: str):
    """JP 词被查 → looks+1、刷新 last_ts。已 mastered 的查了也不复活（除非显式取消）。
    垃圾词(单假名/跨行残片)不记,避免下划线噪声。"""
    import time as _t
    word = (word or "").strip()
    if not word or not _jp_vocab_is_trackable(word):
        return
    with _JP_VOCAB_LOCK:
        d = _jp_vocab_load()
        e = d.get(word) or {}
        e["looks"] = int(e.get("looks", 0)) + 1
        e["last_ts"] = int(_t.time())
        e.setdefault("first_ts", e["last_ts"])
        d[word] = e
        _jp_vocab_save(d)


def _mastery_slug(m: float) -> str:
    """mastery 0-1 → 颜色 slug。跟英语 compute_mastery.LABELS 完全相同的阈值(统一两语言着色)。"""
    if m < 0.25: return "new"
    if m < 0.55: return "seen"
    if m < 0.85: return "known"
    return "mastered"


def _jp_mastery(e: dict) -> float:
    """日语词 mastery（0-1），跟英语 compute_score 统一模型，用日语侧可用信号：
    用户手动锁(user_mark/legacy mastered) > 查询次数(查得多=没掌握) > 时间衰减(久未查=大概率学会、缓慢回升)。
    冷却:刚查过 last_ts≈now → days=0 → 无回升加成 → 24h 内只会因再次查询而降、不会自己涨(=「查后只跌不涨」)。
    注:日语暂无 Anki 卡链接 + 段落暴露信号(那是英语 vault 笔记管线),故不含这两项；其余跟英语一致。"""
    um = (e.get("user_mark") or "").strip().lower()
    if um == "known" or e.get("mastered"):   # mastered 兼容旧数据
        return 1.0
    if um == "unknown":
        return 0.0
    score = 0.50
    looks = int(e.get("looks", 0))
    if looks >= 5: score -= 0.25
    elif looks >= 3: score -= 0.15
    elif looks >= 2: score -= 0.05
    last = int(e.get("last_ts", 0) or 0)
    if last:
        days = (_time.time() - last) / 86400.0
        if days > 90: score += 0.10
        elif days > 30: score += 0.05
    return max(0.0, min(1.0, score))


def _jp_vocab_slug(e: dict) -> str | None:
    """熟悉度 → 颜色 slug（跟英语统一：new/seen/known/mastered）。mastered 返回 None（不画下划线）。"""
    slug = _mastery_slug(_jp_mastery(e))
    return None if slug == "mastered" else slug


def _vocab_idx() -> dict:
    """vocab_index.index()（英日**同一库**，2026-06 日语并入后）。失败返回 {}。"""
    try:
        import sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in sys.path:
            sys.path.insert(0, str(vp))
        import vocab_index  # type: ignore
        return vocab_index.index() or {}
    except Exception:
        return {}


def _build_jp_vocab_marks(chars: list[dict]) -> list[dict]:
    """按 fugashi 分词的 w 把 chars 分组成 JP token → 解析原形 → 查 **vocab_index（英日统一库）** →
    未掌握(label_slug!=mastered)的按 mastery 上色画下划线。rects 用 PDF pt 坐标，前端复用 renderVocabUnderlines。"""
    idx = _vocab_idx()
    if not idx:
        return []
    marks: list[dict] = []
    i = 0
    n = len(chars)
    while i < n:
        c = chars[i]
        wid = c.get("w", -1)
        if c.get("sp") or wid is None or wid < 0:
            i += 1
            continue
        j = i
        toks = []
        # 同 w 继续合并;中间的 sp char(收藏词组跨行/词内空格,_merge_favorite_phrases 给它也设了同 w)
        # 跳过不计入 surface、但**不终止**分组 → 跨行词组「公表する」不会被换行空格切成两段各自查词。
        while j < n and chars[j].get("w") == wid:
            if not chars[j].get("sp"):
                toks.append(chars[j])
            j += 1
        if any(t.get("favm") for t in toks):
            i = j   # 已掌握收藏词组 → 不画下划线
            continue
        surf = "".join(t.get("c", "") for t in toks)
        # 先按表层查（forms 映射已含活用形）；查不到再解析原形(辞書形)查
        info = idx.get(surf.lower())
        if not info:
            base = (_jp_inflection(surf) or {}).get("base")
            if base:
                info = idx.get(base.lower())
        if info and info.get("label_slug") and info["label_slug"] != "mastered":
            rects = []
            cur = None
            for t in toks:
                lh = t["y1"] - t["y0"]
                if cur and abs(t["y0"] - cur[1]) <= lh * 0.5:
                    cur[2] = max(cur[2], t["x1"]); cur[1] = min(cur[1], t["y0"]); cur[3] = max(cur[3], t["y1"])
                else:
                    if cur: rects.append([round(x, 2) for x in cur])
                    cur = [t["x0"], t["y0"], t["x1"], t["y1"]]
            if cur: rects.append([round(x, 2) for x in cur])
            marks.append({"word": surf, "lemma": info["lemma"],
                          "mastery": round(float(info.get("mastery", 0.0)), 3),
                          "label_slug": info["label_slug"], "rects": rects, "jp": True})
        i = j
    return marks


def page_unmastered_vocab(rel: str, page: int) -> list[dict]:
    """供侧边栏 agent 查掌握度数据库:某页**还没掌握**的生词(跟页面下划线一模一样,英+日)。
    复用 /page-overlay 的管线(_page_chars_cached→marks),返回 [{word,lemma,mastery,level}]。
    已掌握的词(label_slug=mastered)和**从没查过的词**(不在生词库)都不会出现——这才是真实的『没掌握』,别靠猜。"""
    try:
        abs_path = _safe_vault_path(rel)
        if not abs_path or page < 1:
            return []
        res = _page_chars_cached(abs_path, rel, page)
        if res is None:
            return []
        chars, page_w, page_h, furigana = res
        _apply_char_offset(chars, _char_offset_for(rel, page))
        _merge_favorite_phrases(chars)
        marks = _build_vocab_marks(chars)
        if _page_allows_ja(chars, rel):
            marks += _build_jp_vocab_marks(chars)
        out = []
        for m in marks:
            out.append({"word": m.get("word"), "lemma": m.get("lemma"),
                        "mastery": m.get("mastery"), "level": m.get("label_slug")})
        return out
    except Exception:
        return []


def vocab_mastery_for(words) -> list[dict]:
    """供侧边栏 agent 查指定词的掌握度(英+日,日语自动解析活用→原形再查)。
    返回每词 {word,lemma,mastery,level,mastered} 或 {word,tracked:False}(生词库没有=没查过)。"""
    idx = _vocab_idx()
    out = []
    for w in (words or [])[:50]:
        w = str(w or "").strip()
        if not w:
            continue
        info = idx.get(w.lower())
        if not info:
            try:
                base = (_jp_inflection(w) or {}).get("base")
                if base:
                    info = idx.get(base.lower())
            except Exception:
                pass
        if info:
            out.append({"word": w, "lemma": info.get("lemma"),
                        "mastery": round(float(info.get("mastery", 0) or 0), 3),
                        "level": info.get("label_slug"),
                        "mastered": info.get("label_slug") == "mastered"})
        else:
            out.append({"word": w, "tracked": False})
    return out


# ── 收藏词组（state/pdf-phrases.json）：作为之后分词依据，合并成单个 w（单击选中整词组）──
_PHRASES_PATH = CLAUDE_DIR / "state" / "pdf-phrases.json"


def _phrases_load() -> list:
    try:
        d = json.loads(_PHRASES_PATH.read_text("utf-8"))
        return [p for p in (d.get("phrases") or []) if isinstance(p, str) and p.strip()]
    except Exception:
        return []


def _phrases_save(lst: list):
    try:
        _PHRASES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PHRASES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"phrases": lst}, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(_PHRASES_PATH)
    except Exception:
        pass


# ── 词组「已掌握」store（state/pdf-phrase-mark.json）：标记掌握的词组不再画生词下划线 ──
# 跟单词掌握分开:单词走 vocab 笔记 frontmatter,词组没有笔记(强行建会生成 "web browser.md" 幽灵笔记)。
_PHRASE_MARK_PATH = CLAUDE_DIR / "state" / "pdf-phrase-mark.json"


def _phrase_norm(t: str) -> str:
    """词组归一化键:折叠空白 + 转小写(空白/大小写不敏感匹配,跟 _merge_favorite_phrases 一致)。"""
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _phrase_marks_load() -> set:
    try:
        d = json.loads(_PHRASE_MARK_PATH.read_text("utf-8"))
        return {_phrase_norm(p) for p in (d.get("mastered") or []) if isinstance(p, str) and p.strip()}
    except Exception:
        return set()


def _phrase_marks_save(s: set):
    try:
        _PHRASE_MARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PHRASE_MARK_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"mastered": sorted(s)}, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(_PHRASE_MARK_PATH)
    except Exception:
        pass


def _merge_favorite_phrases(chars: list[dict]) -> None:
    """把收藏词组在该页出现处的 chars 合并成同一个 w（含内部空格）→ 单击选词时整词组一起选。
    空白不敏感 + ASCII 大小写不敏感匹配；按收藏长度降序优先，used 标记防重叠。
    已掌握的词组(_phrase_marks_load)也参与合并,并给 chars 打 favm=1 → 两个 vocab builder 据此跳过下划线。"""
    favs = _phrases_load()
    marked = _phrase_marks_load()
    targets = set(favs) | marked
    if not targets or not chars:
        return
    nonsp = [(i, ch.get("c", "")) for i, ch in enumerate(chars) if not ch.get("sp")]
    if not nonsp:
        return
    compact = "".join(c for _i, c in nonsp).lower()
    cidx = [i for i, _c in nonsp]
    used = [False] * len(chars)
    wn = 0
    for ph in sorted(targets, key=len, reverse=True):
        p = re.sub(r"\s+", "", ph).lower()
        if len(p) < 2:
            continue
        is_mastered = _phrase_norm(ph) in marked
        start = 0
        while True:
            k = compact.find(p, start)
            if k < 0:
                break
            i0 = cidx[k]; i1 = cidx[k + len(p) - 1]
            # 校验是连续文本流（防跨段/跨栏把 reading-order 相邻但视觉分离的字误并）：
            # 相邻非空格字符竖直跳变 > 2.2 行高 → 不是同一词组，跳过
            ok = not any(used[i0:i1 + 1])
            prevc = None
            if ok:
                for j in range(i0, i1 + 1):
                    cj = chars[j]
                    if cj.get("sp"):
                        continue
                    if prevc is not None:
                        h = max(1.0, prevc["y1"] - prevc["y0"])
                        if abs(cj["y0"] - prevc["y0"]) > h * 2.2:
                            ok = False; break
                    prevc = cj
            if ok:
                wid = 900000000 + wn; wn += 1
                for j in range(i0, i1 + 1):
                    chars[j]["w"] = wid
                    used[j] = True
                    if is_mastered:
                        chars[j]["favm"] = 1   # 已掌握词组 → builder 跳过下划线
            start = k + 1


def _merge_favorite_phrases_furigana(furigana: list[dict]) -> list[dict]:
    """收藏/已掌握词组覆盖处:把连续的多个振假名条目合并成单条(bbox 并集 + 读音拼接),
    让注音按词组整体读音显示一个 ruby(如 当試験 → とうしけん 一条),而非分词的 当(とう)/試験(しけん) 两条。
    匹配同 _merge_favorite_phrases(空白不敏感 + 小写);同行 y 容差防跨行误并。"""
    targets = set(_phrases_load()) | _phrase_marks_load()
    norm_targets = {re.sub(r"\s+", "", t).lower() for t in targets if len(re.sub(r"\s+", "", t)) >= 2}
    if not norm_targets or not furigana:
        return furigana
    out: list[dict] = []
    n = len(furigana)
    i = 0
    while i < n:
        matched = False
        hi = min(n, i + 8)   # 词组最多并 8 个 token
        for j in range(hi, i + 1, -1):   # 优先最长匹配
            grp = furigana[i:j]
            if max(g["y0"] for g in grp) - min(g["y0"] for g in grp) > 3:   # 跨行 → 不并
                continue
            surf = "".join((g.get("wd") or "") for g in grp)
            if re.sub(r"\s+", "", surf).lower() in norm_targets:
                out.append({
                    "x0": min(g["x0"] for g in grp), "y0": min(g["y0"] for g in grp),
                    "x1": max(g["x1"] for g in grp), "y1": max(g["y1"] for g in grp),
                    "rt": "".join((g.get("rt") or "") for g in grp), "wd": surf,
                })
                i = j; matched = True; break
        if not matched:
            out.append(furigana[i]); i += 1
    return out


def _build_unmastered_sentences(chars: list[dict], threshold: int = 3, min_words: int = 10, page_h: float = 0, allow_ja: bool = True) -> list[dict]:
    """识别需要标注的句子。判定条件：
      - 至少 threshold 个未掌握 lemma（默认 3）
      - 句子总词数 > min_words - 1（默认 10，即 ≥ 10 词）
    返回 [{text, rects, lemmas, count, total_words, last_char}]
    句子边界 = . ! ? 。！？ / 列表标记 • / 段落分界（行间距 > 1.5× 行高）
    allow_ja=False（中文书等非日语 CJK 页）→ 不对 CJK 段做日语分词/匹配（免中文汉字撞日语词库）。
    """
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        import vocab_index   # type: ignore
    except Exception:
        return []
    idx = vocab_index.index()
    if not idx:
        return []
    # 未掌握的 forms 映射到 lemma。**计数集 = 下划线集**：凡是查过且未掌握(label_slug!=mastered)的词都算，
    # 跟 _build_vocab_marks 下划线完全一致 → 句中被下划线的词数 ≥ threshold 就框（不再按词频排除，
    # 否则会出现「下划线 5 个词、框只数到 1 个 → 不框」的不一致）。不想要某词计数 → 把它标记掌握即可。
    form_to_lemma_unmastered = {
        form: info["lemma"]
        for form, info in idx.items()
        if info.get("label_slug") and info["label_slug"] != "mastered"
    }

    # 正文字号基准：非空格 char 高度中位数。明显大于它的句子（章节标题/单元名）不当学习句子
    _hs = sorted((c["y1"] - c["y0"]) for c in chars if not c.get("sp") and c["y1"] > c["y0"])
    median_h = _hs[len(_hs) // 2] if _hs else 0
    # 页面文本右边界 / 宽度:用于「短行=独立行」判定(标题/版权/ISBN/居中行等没顶到右边界 → 不该并入下一行)
    _x1s = [c["x1"] for c in chars if not c.get("sp") and c["x1"] > c["x0"]]
    _x0s = [c["x0"] for c in chars if not c.get("sp") and c["x1"] > c["x0"]]
    right_edge = max(_x1s) if _x1s else 0
    text_width = max(1.0, right_edge - (min(_x0s) if _x0s else 0))

    sentences: list[dict] = []
    cur_chars: list[dict] = []
    cur_lemmas: set[str] = set()
    cur_word_letters: list[str] = []
    cur_word_chars: list[dict] = []   # 当前词的 char dict(带 w),JP 按 w 分组计数用
    cur_total_words: int = 0

    def _flush_word():
        nonlocal cur_word_letters, cur_word_chars, cur_total_words
        if not cur_word_chars:
            cur_word_letters = []
            return
        w = "".join(x.get("c", "") for x in cur_word_chars)
        chs = cur_word_chars
        cur_word_letters = []
        cur_word_chars = []
        # 含日语(平/片假名/汉字) → **按 page-chars 的 w 分组**(fugashi token / 收藏词组合并),
        # 跟下划线 _build_jp_vocab_marks 同一套 → 计数严格 = 下划线:收藏词组(一个 w)算 1 个,
        # 不会被拆成内部词素跟「词组本身」重复计数(用户报的 词组 aabb + aa + bb = 3 的 bug)。
        if re.search(r"[぀-ゟ゠-ヿ㐀-鿿]", w):
            if not allow_ja:
                return   # 中文书等非日语:CJK 段不分词不匹配,免中文汉字撞日语词库(误框)
            m = len(chs)
            k = 0
            while k < m:
                wid = chs[k].get("w", -1)
                if wid is None or wid < 0:
                    k += 1
                    continue
                g = k
                while g < m and chs[g].get("w") == wid:
                    g += 1
                surf = "".join(x.get("c", "") for x in chs[k:g])
                cur_total_words += 1
                lk = form_to_lemma_unmastered.get(surf.lower())
                if not lk:
                    base = (_jp_inflection(surf) or {}).get("base")
                    if base:
                        lk = form_to_lemma_unmastered.get(base.lower())
                if lk:
                    cur_lemmas.add(lk)
                k = g
            return
        wl = w.lower()
        # ≤2 字母的词（am/is/he/we/it/of/to… 功能词）默认掌握，不算生词
        if len(wl) > 2 and wl in form_to_lemma_unmastered:
            cur_lemmas.add(form_to_lemma_unmastered[wl])
        if len(wl) >= 2 or wl.isalpha():
            cur_total_words += 1

    def _sentence_rects(sent_chars: list[dict]) -> list[list[float]]:
        rects = []
        cur_rect = None
        for c in sent_chars:
            if c.get("sp") and (not cur_rect):
                continue
            x0, y0, x1, y1 = c["x0"], c["y0"], c["x1"], c["y1"]
            lineH = y1 - y0
            if cur_rect and abs(y0 - cur_rect[1]) <= lineH * 0.5:
                cur_rect[2] = max(cur_rect[2], x1)
                cur_rect[1] = min(cur_rect[1], y0)
                cur_rect[3] = max(cur_rect[3], y1)
            else:
                if cur_rect:
                    rects.append([round(x, 2) for x in cur_rect])
                if c.get("sp"):
                    continue
                cur_rect = [x0, y0, x1, y1]
        if cur_rect:
            rects.append([round(x, 2) for x in cur_rect])
        return rects

    def _flush_sentence():
        nonlocal cur_chars, cur_lemmas, cur_total_words
        # 双条件：未掌握词 ≥ threshold 且 总词数 ≥ min_words
        if cur_chars and len(cur_lemmas) >= threshold and cur_total_words >= min_words:
            text = "".join(c["c"] for c in cur_chars).strip()
            text = re.sub(r"\s+", " ", text)[:500] if text else ""
            rects = _sentence_rects(cur_chars)
            first_char = None
            last_char = None
            for c in cur_chars:
                if not c.get("sp"):
                    first_char = [round(c["x0"], 2), round(c["y0"], 2),
                                  round(c["x1"], 2), round(c["y1"], 2)]
                    break
            for c in reversed(cur_chars):
                if not c.get("sp"):
                    last_char = [round(c["x0"], 2), round(c["y0"], 2),
                                 round(c["x1"], 2), round(c["y1"], 2)]
                    break
            # 竖排文字（旋转排版的书页边注释）：整句 bbox 高 > 宽*1.6 → 跳过
            # （横排句子哪怕 2-3 行，行宽通常仍 > 行高叠加；竖排是一长列，高远大于宽）
            nsp = [c for c in cur_chars if not c.get("sp")]
            vertical = False
            footer = False
            if nsp:
                bw = max(c["x1"] for c in nsp) - min(c["x0"] for c in nsp)
                bh = max(c["y1"] for c in nsp) - min(c["y0"] for c in nsp)
                vertical = bh > bw * 1.6
                # 页脚/页眉（如底部"→ Unit X"导航条）：整句靠页底(>90%)或页顶(<6%) → 不当学习句子
                if page_h:
                    sy0 = min(c["y0"] for c in nsp); sy1 = max(c["y1"] for c in nsp)
                    footer = (sy0 > page_h * 0.90 or sy1 < page_h * 0.06)
                # 大字号 = 章节标题/单元名（如 "Present continuous and present simple 1"）→ 不当学习句子
                if not footer and median_h:
                    avg_h = sum(c["y1"] - c["y0"] for c in nsp) / len(nsp)
                    if avg_h > median_h * 1.4:
                        footer = True
                # 整句加粗(>90%) = 练习指令/加粗演示句/小标题（如 "Put the verb into..."）→ 不当学习句子
                # （正文叙述句不加粗；演示句只部分加粗、比例低于此阈值，不误伤普通正文）
                if not footer:
                    nb = sum(1 for c in nsp if c.get("b"))
                    if nb / len(nsp) > 0.9:
                        footer = True
            if not vertical and not footer:
                sentences.append({
                    "text": text, "rects": rects,
                    "lemmas": sorted(cur_lemmas), "count": len(cur_lemmas),
                    "total_words": cur_total_words,
                    "first_char": first_char,
                    "last_char": last_char,
                })
        cur_chars = []
        cur_lemmas = set()
        cur_total_words = 0

    # 列表项序号:新行以 10.1 / 10. / 1) / a. / iv) 等开头 → 该行是独立列表项,要在它前面断句
    # (编号列表行尾常无句号、行距又不大 → 否则 10.1/10.2/… 会被并成一个跨多行的大句子框)。
    _list_head_re = re.compile(r"^\s*(\d{1,3}([.)]|\.\d)|[A-Za-z][.)]|[ivxIVX]{1,4}[.)])")

    def _is_list_head(idx: int) -> bool:
        s = "".join(chars[j].get("c", "") for j in range(idx, min(idx + 12, len(chars))))
        return bool(_list_head_re.match(s))

    prev = None
    prev_ns = None   # 上一个非空格字符(换行/短行判定用,避开行尾空格)
    pending_period = False   # 上一字符是 .，需要看下一字符决定切不切
    for i, ch in enumerate(chars):
        c = ch.get("c", "")
        # 处理 pending period：根据当前字符决定上一个 . 是否真切句
        if pending_period:
            # 延续(小数 3.14 / 缩写 e.g.)必须同一行：换行后的字符(如练习题下一行的编号 2/3)不算延续
            _same_line = bool(prev) and not prev.get("sp") and \
                abs(ch.get("y0", 0) - prev.get("y0", 0)) < max(1.0, (prev.get("y1", 0) - prev.get("y0", 0)) * 0.5)
            is_continuation = (
                _same_line
                and (not ch.get("sp"))
                and len(c) == 1
                and (c.isdigit() or (c.isalpha() and c.islower()))
            )
            if not is_continuation:
                _flush_word()
                _flush_sentence()
            pending_period = False
        # 跨 block 切句：PyMuPDF 不同排版块（如对话气泡 vs 页脚导航条）的字符在 reading order
        # 相邻时绝不混进同一句（否则气泡句的翻译会串成页脚的）。w = block*1e6+line*1e3+word_no
        if prev is not None:   # 不加 sp 守卫：块之间常隔一个空格 char，若要求 prev 非空格就漏切了
            pbk, cbk = prev.get("bk"), ch.get("bk")
            if pbk is not None and cbk is not None and pbk != cbk:   # rawdict 块变即切(斜体 w=-1 也不漏)
                _flush_word(); _flush_sentence()
        # 跨行检测：用「上一个非空格字符」prev_ns(行尾常有空格 char,用 prev 会漏判换行 → 整块并成大框)
        if prev_ns is not None and not ch.get("sp"):
            prev_h = max(0.1, prev_ns["y1"] - prev_ns["y0"])
            line_gap = ch["y0"] - prev_ns["y0"]
            if line_gap > prev_h * 1.5:
                # 大段落间距 → 切句
                _flush_word()
                _flush_sentence()
            elif abs(line_gap) > prev_h * 0.5:
                # 普通跨行
                if _is_list_head(i):
                    # 新行是列表项(10.1 / 10. / a) …) → 独立成句,不并进上一项(否则整列表框成一大块)
                    _flush_word(); _flush_sentence()
                elif (right_edge - prev_ns.get("x1", 0)) > text_width * 0.30:
                    # 上一行明显没顶到右边界(>30% 短) = 独立短行(标题/版权/ISBN/居中行/段末) → 断句,
                    # 不并入下一行(否则标题块那几行被并成一个跨行大框)。正文顶格换行的行≈到右边界,不受影响。
                    _flush_word(); _flush_sentence()
                elif cur_word_letters and cur_word_letters[-1] == "-":
                    cur_word_letters.pop()   # 行尾连字符 → 拼回
                    if cur_word_chars: cur_word_chars.pop()
                else:
                    _flush_word()
        # 列表标记 → 切句（每个列表项独立句）
        if c in "•▪▶◆●○◇":
            _flush_word()
            _flush_sentence()
            cur_chars.append(ch); prev = ch; prev_ns = ch; continue
        if ch.get("sp"):
            _flush_word()
            cur_chars.append(ch); prev = ch; continue   # 空格不更新 prev_ns
        # 句末标点
        if c in "!?。！？":
            _flush_word()
            cur_chars.append(ch)
            _flush_sentence()
            prev = ch; prev_ns = ch; continue
        if c == ".":
            _flush_word()
            cur_chars.append(ch)
            pending_period = True   # 推迟切句决策到下一字符
            prev = ch; prev_ns = ch; continue
        if c.isalpha() or c in "'-":
            cur_word_letters.append(c)
            cur_word_chars.append(ch)   # 带 w,JP 按 w 分组计数(词组算一个)
        else:
            _flush_word()
        cur_chars.append(ch)
        prev = ch; prev_ns = ch
    _flush_word()
    # 文件末尾：如果有 pending period 也切
    if pending_period:
        _flush_sentence()
        pending_period = False
    _flush_sentence()
    # 加 NBSP（避免极短句子无法被 hit）：按文本长度过滤
    sentences = [s for s in sentences if len(s.get("text", "")) >= 12]
    # 预翻译(2026-06-10 改 SWR):**只取已缓存的译文**即回——此前 miss 句逐句同步翻
    # (Google ~300ms/句,抖动时退化为每句数秒 AI CLI),首访页被阻塞 1-3s+ 且可耗尽线程池。
    # miss 句丢后台批量翻译(去重、绝不落 AI CLI),下次 overlay/vocab-marks 重取自然带上;
    # 前端句子 L 按钮本就有按需翻译路径,首访功能不丢。
    try:
        import sys as _sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in _sys.path:
            _sys.path.insert(0, str(vp))
        from translate import _cache_get   # type: ignore
        misses = []
        for s in sentences:
            t = s.get("text") or ""
            if not t:
                continue
            zh = _cache_get(t, "zh-CN")
            if zh:
                _cstat("sent_tr.hit")
                s["zh"] = zh
            else:
                _cstat("sent_tr.miss")
                misses.append(t)
        if misses:
            _bg_translate_sentences(misses)
    except Exception:
        pass
    return sentences


_BG_TR_INFLIGHT: set = set()    # 后台补翻去重(多页并发不重复提交)


def _bg_translate_sentences(texts: list[str]) -> None:
    """后台批量补翻句子进 tr-cache(gtranslate_batch 一次过;失败静默,下次再试)。不调 AI CLI。"""
    import threading
    todo = [t for t in texts if t not in _BG_TR_INFLIGHT]
    if not todo:
        return
    _BG_TR_INFLIGHT.update(todo)

    def _run():
        try:
            import sys as _sys
            vp = CLAUDE_DIR / "scripts" / "vocab"
            if str(vp) not in _sys.path:
                _sys.path.insert(0, str(vp))
            from translate import gtranslate_batch, _cache_put   # type: ignore
            res = gtranslate_batch(todo, "zh-CN")   # 等长 list;无 key → None
            for t, zh in zip(todo, res or []):
                if zh:
                    try:
                        _cache_put(t, "zh-CN", zh, "gtranslate")
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            _BG_TR_INFLIGHT.difference_update(todo)

    threading.Thread(target=_run, daemon=True).start()


def _split_page_sentences(chars: list[dict], page_h: float = 0) -> list[dict]:
    """整页翻译用：把整页 chars 切成**所有**句子（不限生词数，不排除标题/页脚——整页都译）。
    边界规则同 _build_unmastered_sentences：. ! ? 。！？ / 列表标记 / 跨 block / 大行间距。
    返回 [{text, rects, first_char, last_char}]；跳过竖排文字（几何会让 overlay 乱）。"""
    sentences: list[dict] = []
    cur_chars: list[dict] = []

    def _rects(sent_chars):
        rects = []
        cur_rect = None
        for c in sent_chars:
            if c.get("sp") and (not cur_rect):
                continue
            x0, y0, x1, y1 = c["x0"], c["y0"], c["x1"], c["y1"]
            lineH = y1 - y0
            if cur_rect and abs(y0 - cur_rect[1]) <= lineH * 0.5:
                cur_rect[2] = max(cur_rect[2], x1)
                cur_rect[1] = min(cur_rect[1], y0)
                cur_rect[3] = max(cur_rect[3], y1)
            else:
                if cur_rect:
                    rects.append([round(x, 2) for x in cur_rect])
                if c.get("sp"):
                    continue
                cur_rect = [x0, y0, x1, y1]
        if cur_rect:
            rects.append([round(x, 2) for x in cur_rect])
        return rects

    def _flush():
        nonlocal cur_chars
        nsp = [c for c in cur_chars if not c.get("sp")]
        if nsp:
            text = re.sub(r"\s+", " ", "".join(c["c"] for c in cur_chars).strip())[:500]
            if len(text) >= 4:
                bw = max(c["x1"] for c in nsp) - min(c["x0"] for c in nsp)
                bh = max(c["y1"] for c in nsp) - min(c["y0"] for c in nsp)
                if not (bh > bw * 1.6):   # 竖排跳过
                    fc = lc = None
                    for c in cur_chars:
                        if not c.get("sp"):
                            fc = [round(c["x0"],2), round(c["y0"],2), round(c["x1"],2), round(c["y1"],2)]; break
                    for c in reversed(cur_chars):
                        if not c.get("sp"):
                            lc = [round(c["x0"],2), round(c["y0"],2), round(c["x1"],2), round(c["y1"],2)]; break
                    sentences.append({"text": text, "rects": _rects(cur_chars),
                                      "first_char": fc, "last_char": lc})
        cur_chars = []

    prev = None
    pending_period = False
    for ch in chars:
        c = ch.get("c", "")
        if pending_period:
            _same_line = bool(prev) and not prev.get("sp") and \
                abs(ch.get("y0", 0) - prev.get("y0", 0)) < max(1.0, (prev.get("y1", 0) - prev.get("y0", 0)) * 0.5)
            is_cont = (_same_line and (not ch.get("sp")) and len(c) == 1
                       and (c.isdigit() or (c.isalpha() and c.islower())))
            if not is_cont:
                _flush()
            pending_period = False
        if prev is not None:
            pbk, cbk = prev.get("bk"), ch.get("bk")
            if pbk is not None and cbk is not None and pbk != cbk:
                _flush()
        if prev and not prev.get("sp") and not ch.get("sp"):
            ph = max(0.1, prev["y1"] - prev["y0"])
            gap = ch["y0"] - prev["y0"]
            if gap > ph * 1.5:
                _flush()
        if c in "•▪▶◆●○◇":
            _flush(); cur_chars.append(ch); prev = ch; continue
        if ch.get("sp"):
            cur_chars.append(ch); prev = ch; continue
        if c in "!?。！？":
            cur_chars.append(ch); _flush(); prev = ch; continue
        if c == ".":
            cur_chars.append(ch); pending_period = True; prev = ch; continue
        cur_chars.append(ch)
        prev = ch
    _flush()
    return sentences


@bp.route("/api/page-translate")
def pdf_api_page_translate():
    """整页翻译：切出该页所有句子 → 批量翻译（Google batch 优先，带 sidecar 缓存）→ 返回。
    GET ?file=&page= → {ok, sentences:[{text,rects,zh,first_char,last_char}], page_w, page_h}"""
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        import fitz  # noqa: F401
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF not installed"}), 500
    res = _page_chars_cached(abs_path, rel, page)
    if res is None:
        return jsonify({"ok": False, "error": "page out of range"}), 400
    chars, page_w, page_h, _furi = res
    sentences = _split_page_sentences(chars, page_h)
    if not sentences:
        return jsonify({"ok": True, "sentences": [], "page_w": page_w, "page_h": page_h})
    import sys as _sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in _sys.path:
        _sys.path.insert(0, str(vp))
    try:
        from translate import (gtranslate_batch as _gb, translate as _tr,
                               _cache_get as _cg, _cache_put as _cp)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"translate load fail: {ex}"}), 500
    texts = [s["text"] for s in sentences]
    # 1) 先吃缓存（重开同页秒出）
    zhs = [(_cg(t, "zh-CN") or "") for t in texts]
    miss_idx = [i for i, z in enumerate(zhs) if not z]
    # 2) 未命中的批量 Google 翻译
    if miss_idx:
        miss_texts = [texts[i] for i in miss_idx]
        batch = None
        try:
            batch = _gb(miss_texts)
        except Exception:
            batch = None
        if batch and len(batch) == len(miss_texts):
            for k, i in enumerate(miss_idx):
                if batch[k]:
                    zhs[i] = batch[k]
                    try: _cp(texts[i], "zh-CN", batch[k], "gtranslate")
                    except Exception: pass
        # 3) Google 没 key / 整体失败 → 逐句兜底。2026-06-10 改:① backend="no_ai"
        #    (gtranslate→deepl→mymemory,**绝不落 AI CLI**——此前走 auto 链,Google 故障窗
        #    60 句 × [8s 超时 + AI 数秒] 单请求挂 4-7 分钟还烧几十次额度);② 10s 墙钟预算,
        #    超时即止,未译句 zh 留空原样返回(前端对空 zh 优雅跳过,响应带 translated/total)。
        still = [i for i in miss_idx if not zhs[i]]
        _deadline = _time.monotonic() + 10
        for i in still[:60]:   # 限量兜底，避免极端长页阻塞
            if _time.monotonic() > _deadline:
                break
            try:
                z = _tr(texts[i], backend="no_ai")
                if z:
                    zhs[i] = z
            except Exception:
                pass
    for s, z in zip(sentences, zhs):
        s["zh"] = z
    done = sum(1 for z in zhs if z)
    return jsonify({"ok": True, "sentences": sentences, "page_w": page_w,
                    "page_h": page_h, "translated": done, "total": len(sentences)})


@bp.route("/api/page-vocab-marks")
def pdf_api_page_vocab_marks():
    """轻量路由：仅返回该页 vocab_marks（不返回 chars）。用户查词后用来立刻刷新下划线。

    2026-06-10 统一到 /api/page-overlay 同路径:此前这里内联 fitz.open+rawdict+分词全重算
    (查词后 3-4 轮 × 多页,每页几百 ms 纯浪费)+ vocab_index force_reload 全库重读(~150ms);
    改用 _page_chars_cached(磁盘缓存秒回)+ _apply_char_offset(顺带修校准页错位)+
    OCR override 一致 + 普通 mtime 扫描足以感知刚写盘的笔记(前端刷新延迟 ≥1.5s)。"""
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        import fitz  # noqa: F401
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF missing"}), 500
    try:
        res = _page_chars_cached(abs_path, rel, page)
        if res is None:
            return jsonify({"ok": False, "error": "page out of range"}), 400
        chars, page_w, page_h, _furi = res
        _apply_char_offset(chars, _char_offset_for(rel, page))
        _merge_favorite_phrases(chars)
        _aj = _page_allows_ja(chars, rel)   # 中文书纯汉字 → 不当日语,免撞日语词库
        marks = _build_vocab_marks(chars)
        if _aj:
            marks += _build_jp_vocab_marks(chars)   # 日语生词下划线
        sentences = _build_unmastered_sentences(chars, page_h=page_h, allow_ja=_aj)
        # **必须跟 page-overlay 一致**：合并手动翻译句 sidecar + 过滤已删除句。
        for ts in _tr_load(rel):
            if ts.get("rects") and ts.get("page", page) == page:
                sentences.append(ts)
        _dis = _dismiss_load(rel)
        if _dis:
            sentences = [s for s in sentences if (s.get("text") or "").strip() not in _dis]
        return jsonify({
            "ok": True,
            "vocab_marks": marks,
            "vocab_sentences": sentences,
            "page_w": page_w,
            "page_h": page_h,
        })
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


def _book_text_index(abs_path, rel: str) -> dict:
    """全书逐页纯文本，磁盘缓存(键含 mtime)。{page_str: text}。首次约 3s(679 页)，之后秒读。"""
    import hashlib
    try:
        mtime = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mtime = 0
    cdir = CLAUDE_DIR / "state" / "pdf-text-index"
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    cpath = cdir / f"{sha}-{mtime}.json"
    if cpath.exists():
        try:
            return json.loads(cpath.read_text("utf-8"))
        except Exception:
            pass
    import fitz
    out = {}
    doc = fitz.open(str(abs_path))
    try:
        for i in range(len(doc)):
            try:
                out[str(i + 1)] = doc[i].get_text("text")
            except Exception:
                out[str(i + 1)] = ""
    finally:
        doc.close()
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        # 顺手清理同书旧 mtime 的索引
        for old in cdir.glob(f"{sha}-*.json"):
            if old != cpath:
                try: old.unlink()
                except Exception: pass
        cpath.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return out


@bp.route("/api/search")
def pdf_api_search():
    """全文搜索：在全书文本索引里找 q（大小写不敏感，子串匹配，适配中日无词边界）。
    GET ?file=&q=&limit= → {ok, total, matches:[{page, count, snippet, pos}]}"""
    rel = request.args.get("file", "")
    q = (request.args.get("q", "") or "").strip()
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    if len(q) < 1:
        return jsonify({"ok": False, "error": "empty query"}), 400
    try:
        limit = max(1, min(400, int(request.args.get("limit", "200") or "200")))
    except ValueError:
        limit = 200
    try:
        import fitz  # noqa: F401
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF not installed"}), 500
    try:
        idx = _book_text_index(abs_path, rel)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"index build fail: {ex}"}), 500
    ql = q.lower()
    matches = []
    total = 0
    for pg in sorted(idx.keys(), key=lambda x: int(x)):
        text = idx[pg] or ""
        low = text.lower()
        cnt = low.count(ql)
        if not cnt:
            continue
        total += cnt
        if len(matches) < limit:
            # 取第一处命中做 snippet（折叠空白，上下文 ±28 字）
            pos = low.find(ql)
            flat = re.sub(r"\s+", " ", text)
            flow = flat.lower()
            fpos = flow.find(ql)
            if fpos < 0:
                fpos = 0
            a = max(0, fpos - 28)
            b = min(len(flat), fpos + len(q) + 28)
            snippet = ("…" if a > 0 else "") + flat[a:b].strip() + ("…" if b < len(flat) else "")
            matches.append({"page": int(pg), "count": cnt, "snippet": snippet,
                            "pos": fpos - a + (1 if a > 0 else 0)})
    return jsonify({"ok": True, "q": q, "total": total,
                    "pages": len(matches), "matches": matches})


# ── 全局搜索:跨 vault 所有 PDF 书的全文(SQLite FTS5 trigram,索引由 scripts/build_search_index.py 建)──
_SEARCH_DB = CLAUDE_DIR / "state" / "pdf-search.db"


def _clean_snippet(body: str, q: str, ctx: int = 40):
    """从整页文本取 q 首处命中、折叠空白、上下文 ±ctx 字的片段 + 命中相对位置(供前端加粗)。"""
    flat = re.sub(r"\s+", " ", body)
    flow = flat.lower()
    fpos = flow.find(q.lower())
    if fpos < 0:
        fpos = 0
    a = max(0, fpos - ctx)
    b = min(len(flat), fpos + len(q) + ctx)
    snippet = ("…" if a > 0 else "") + flat[a:b].strip() + ("…" if b < len(flat) else "")
    return snippet, max(0, fpos - a + (1 if a > 0 else 0))


@bp.route("/search")
def pdf_search_page():
    """全局搜索页(跨所有 PDF 书,点结果跳阅读器对应页)。"""
    return render_template("pdf_search.html")


@bp.route("/api/global-search")
def pdf_api_global_search():
    """跨全书全文搜索。GET ?q=&limit= → {ok, q, books:[{file,name,dir,hits,pages:[{page,snippet,pos,qlen}]}], total_books, total_hits, truncated}。
    q≥3 字走 FTS5 trigram + bm25 排序;q<3 字(如 2 字 CJK 词)trigram 无法匹配 → LIKE 子串兜底。"""
    import sqlite3
    q = (request.args.get("q", "") or "").strip()
    if len(q) < 1:
        return jsonify({"ok": True, "q": q, "books": [], "total_books": 0, "total_hits": 0})
    if not _SEARCH_DB.exists():
        return jsonify({"ok": False, "error": "搜索索引未建,请先运行 build_search_index.py"}), 503
    try:
        cap = max(20, min(500, int(request.args.get("limit", "300") or "300")))
    except ValueError:
        cap = 300
    con = sqlite3.connect(f"file:{_SEARCH_DB}?mode=ro", uri=True)
    try:
        names = dict(con.execute("SELECT file, name FROM meta").fetchall())
        dirs = dict(con.execute("SELECT file, dir FROM meta").fetchall())
        if len(q) >= 3:
            fts = '"' + q.replace('"', '""') + '"'   # 当字符串/短语,避开 FTS5 查询语法
            rows = con.execute(
                "SELECT d.file, d.page, d.body FROM pages_fts "
                "JOIN pages_data d ON d.id = pages_fts.rowid "
                "WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ?",
                (fts, cap + 1),
            ).fetchall()
        else:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = con.execute(
                "SELECT file, page, body FROM pages_data WHERE body LIKE ? ESCAPE '\\' LIMIT ?",
                (like, cap + 1),
            ).fetchall()
    finally:
        con.close()
    truncated = len(rows) > cap
    rows = rows[:cap]
    books = {}
    for file, page, body in rows:
        snippet, pos = _clean_snippet(body, q)
        bk = books.get(file)
        if bk is None:
            bk = books[file] = {"file": file, "name": names.get(file, file),
                                "dir": dirs.get(file, ""), "pages": []}
        bk["pages"].append({"page": page, "snippet": snippet, "pos": pos, "qlen": len(q)})
    book_list = list(books.values())   # 保持插入序 = 最佳命中书在前(rows 已按 bm25 排)
    for bk in book_list:
        bk["pages"].sort(key=lambda x: x["page"])   # 书内按页序(阅读顺序)
        bk["hits"] = len(bk["pages"])
    return jsonify({"ok": True, "q": q, "books": book_list,
                    "total_books": len(book_list),
                    "total_hits": sum(b["hits"] for b in book_list),
                    "truncated": truncated})


@bp.route("/api/page-nodes")
def pdf_api_page_nodes():
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    if not rel or page < 1:
        return jsonify({"nodes": []})
    return jsonify({"nodes": _find_kg_nodes_for_page(rel, page)})


@bp.route("/api/epub-nodes")
def pdf_api_epub_nodes():
    """EPUB「本书知识点」:按书归属匹配 KG(EPUB 路径不进 kg.pdf 精确匹配),返回整本 level-2 节点。
    结构同 /api/page-nodes(id/name/numeric_label/state/book/kind/is_grammar/tracked…)。匹配不到 → 优雅空。"""
    rel = (request.args.get("file", "") or "").strip()
    if not rel:
        return jsonify({"ok": True, "nodes": []})
    return jsonify({"ok": True, "nodes": _find_kg_nodes_for_book(rel)})


_DICT_DB_PATH = CLAUDE_DIR / "data" / "ecdict.db"

# vocab 系统脚本（按需懒加载）
def _vocab_modules():
    try:
        import sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in sys.path:
            sys.path.insert(0, str(vp))
        import dict_sources, build_vocab_note   # type: ignore
        return dict_sources, build_vocab_note
    except Exception as ex:
        sys.stderr.write(f"vocab modules load fail: {ex}\n")
        return None, None


def _append_lookup_log(word: str, lemma: str, pdf_rel: str, page: int, context: str = ""):
    """追加查词日志到 state/vocab-lookups.jsonl（每次 dict API 调用都写一行）。"""
    import time as _t
    log_path = CLAUDE_DIR / "state" / "vocab-lookups.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "word": word, "lemma": lemma,
        "pdf": pdf_rel, "page": page,
        "context": context[:200] if context else "",
        "ts": int(_t.time()),
    }, ensure_ascii=False)
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 例句 Anki 卡:自动收割(2026-06-14)。免 AI。边界 = 你最近查过词的页 ──
# 思路:查词=你在这页学不动了/在学;那些页上「含多个未掌握词」的句子做成卡。
# 读音(fugashi)+译文(translate no_ai 缓存)都离线;上限防泛滥;词全学会→suspend。
_SENT_CARDS_STATE = CLAUDE_DIR / "state" / "sentence-cards.json"

def _sent_cards_load() -> dict:
    try:
        return json.loads(_SENT_CARDS_STATE.read_text("utf-8"))
    except Exception:
        return {}

def _sent_cards_save(d: dict) -> None:
    try:
        _SENT_CARDS_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SENT_CARDS_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(_SENT_CARDS_STATE)
    except Exception:
        pass

def _recent_active_pages(since_days: int, rel_filter: str | None = None) -> dict:
    """读 vocab-lookups.jsonl → {(rel,page): 最新ts}，限最近 since_days 天(rel_filter 则只此书)。"""
    import time as _t
    cutoff = int(_t.time()) - since_days * 86400
    out: dict = {}
    p = CLAUDE_DIR / "state" / "vocab-lookups.jsonl"
    if not p.exists():
        return out
    try:
        for line in p.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = int(d.get("ts", 0))
            if ts < cutoff:
                continue
            rel = d.get("pdf") or ""
            pg = int(d.get("page") or 0)
            if not rel or pg < 1:
                continue
            if rel_filter and rel != rel_filter:
                continue
            k = (rel, pg)
            if ts > out.get(k, 0):
                out[k] = ts
    except Exception:
        pass
    return out

def _all_lemmas_mastered(lemmas: list[str]) -> bool:
    """该句的未掌握词是否已全部学会(label_slug=mastered)→ 卡片可退役。"""
    if not lemmas:
        return False
    idx = _vocab_idx()
    for lm in lemmas:
        info = idx.get(str(lm).lower()) or idx.get(str(lm)) or {}
        if info.get("label_slug") != "mastered":
            return False
    return True

def _sent_zh_offline(text: str) -> str:
    """取整句中文:先 translate 缓存(零网络),miss 走 no_ai 链(绝不 AI),都没有回空。"""
    try:
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in sys.path:
            sys.path.insert(0, str(vp))
        from translate import _cache_get, translate as _tr   # type: ignore
        zh = _cache_get(text, "zh-CN")
        if zh:
            return zh
        zh = _tr(text, "zh-CN", backend="no_ai") or ""
        return zh
    except Exception:
        return ""

def _harvest_sentence_cards(rel: str | None = None, since_days: int = 14,
                            per_page_cap: int = 2, run_cap: int = 15,
                            max_len: int = 120) -> dict:
    """核心收割:最近活跃页 → 未掌握句 → 离线译文+读音 → 建卡。返回统计 dict。"""
    import sentence_card as sc
    state = _sent_cards_load()
    active = _recent_active_pages(since_days, rel)
    # 最近查词的页排前(优先给「刚学的地方」出卡)
    pages = sorted(active.items(), key=lambda kv: kv[1], reverse=True)
    added = 0; scanned = 0; skipped_dup = 0; retired = 0
    seen_prefix: set = set()   # 跨页近重复(同书同前缀)只取一条
    for (prel, page), _ts in pages:
        if added >= run_cap:
            break
        ap = _safe_vault_path(prel)
        if not ap:
            continue
        scanned += 1
        try:
            res = _page_chars_cached(ap, prel, page)
        except Exception:
            res = None
        if not res:
            continue
        chars, _pw, ph, _fg = res
        langs = _book_langs_for(prel) if "_book_langs_for" in globals() else []
        allow_ja = (not langs) or ("ja" in langs)
        try:
            sents = _build_unmastered_sentences(chars, page_h=ph, allow_ja=allow_ja)
        except Exception:
            sents = []
        # 甜区:不太长、未掌握词多的优先
        cand = [s for s in sents if len(s.get("text", "")) <= max_len and s.get("count", 0) >= 3]
        cand.sort(key=lambda s: s.get("count", 0), reverse=True)
        per = 0
        for s in cand:
            if added >= run_cap or per >= per_page_cap:
                break
            text = s["text"]
            key = sc.sentence_key(prel, page, text)
            pref = (prel.split("/")[-1], text[:18])
            if key in state and state[key].get("anki_note_id"):
                continue   # 已建过
            if pref in seen_prefix:
                skipped_dup += 1
                continue
            zh = _sent_zh_offline(text)
            if not zh:
                continue   # 没译文不出卡(半张卡无意义);下次缓存补上再收
            bk = prel.split("/")[-1].replace(".pdf", "")
            r = sc.make_sentence_card(file_rel=prel, page=page, text=text, zh=zh,
                                      lemmas=s.get("lemmas") or [],
                                      source=f"{bk} · p{page}")
            if r.get("ok"):
                state[key] = {"file": prel, "page": page, "text": text,
                              "lemmas": s.get("lemmas") or [], "zh": zh,
                              "anki_note_id": r.get("note_id"),
                              "created_ts": int(_time.time()), "retired": False}
                seen_prefix.add(pref)
                added += 1; per += 1
    # 退役:已建卡中未掌握词全学会的 → suspend
    for key, rec in state.items():
        if rec.get("retired") or not rec.get("anki_note_id"):
            continue
        if _all_lemmas_mastered(rec.get("lemmas") or []):
            try:
                sc.suspend_sentence_card(key)
                rec["retired"] = True
                retired += 1
            except Exception:
                pass
    _sent_cards_save(state)
    return {"ok": True, "added": added, "scanned_pages": scanned,
            "skipped_dup": skipped_dup, "retired": retired,
            "total_cards": sum(1 for v in state.values() if v.get("anki_note_id")),
            "active_pages": len(active)}

@bp.route("/api/sentence-cards/harvest", methods=["POST"])
def pdf_api_sentence_harvest():
    """后台收割例句卡。body 可选 {file, since_days, per_page_cap, run_cap}。返回 {job}。"""
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip() or None
    since = int(data.get("since_days") or 14)
    ppc = int(data.get("per_page_cap") or 2)
    rcap = int(data.get("run_cap") or 15)
    jid = _uuid.uuid4().hex[:12]
    _job_set(jid, status="running", kind="sent_harvest", ts=_time.time())
    def _run():
        try:
            out = _harvest_sentence_cards(rel, since, ppc, rcap)
            _job_set(jid, status="done", result=out, ts=_time.time())
        except Exception as e:
            _job_set(jid, status="error", error=str(e), ts=_time.time())
    _threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job": jid})

@bp.route("/api/sentence-cards/status")
def pdf_api_sentence_status():
    """轮询收割 job;另回 total_cards 供按钮显示。"""
    jid = request.args.get("job", "")
    with _JOBS_LOCK:
        j = dict(_JOBS.get(jid) or {})
    st = _sent_cards_load()
    j["total_cards"] = sum(1 for v in st.values() if v.get("anki_note_id"))
    return jsonify(j or {"status": "unknown"})


def _auto_anki_cfg() -> tuple[int, int]:
    """(阈值, 冷却小时)。server-config dict.auto_anki_lookups(默认3,0=关) / auto_anki_cooldown_h(默认24)。"""
    try:
        cfg = json.loads((CLAUDE_DIR / "state" / "server-config.json").read_text("utf-8"))
        d = cfg.get("dict") or {}
        return int(d.get("auto_anki_lookups", 3)), int(d.get("auto_anki_cooldown_h", 24))
    except Exception:
        return 3, 24


def _effective_lookup_count(lemma: str, cooldown_h: int = 24) -> int:
    """「有效遇到次数」：从查词日志算去重后的 (pdf, page, 冷却窗口) 组合数。
    - 同一位置(pdf+page) + 同一冷却窗口内反复查 → 只算 1 次
    - 换页/换书，或隔了冷却时间再遇到 → +1
    真实含义 = 在多少个「不同场合」遇到这词还要查。"""
    log = CLAUDE_DIR / "state" / "vocab-lookups.jsonl"
    if not log.exists():
        return 0
    cd = max(1, cooldown_h * 3600)
    seen = set()
    try:
        for line in log.read_text("utf-8").splitlines():
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("lemma") != lemma:
                continue
            seen.add((j.get("pdf", ""), j.get("page", 0), int(j.get("ts", 0)) // cd))
    except Exception:
        return 0
    return len(seen)


def _maybe_auto_anki(word: str):
    """多场合遇到同一词后自动加卡：有效遇到次数 ≥ 阈值 且 笔记无 anki_card_id → make_card。"""
    try:
        th, cooldown_h = _auto_anki_cfg()
        if th <= 0:
            return
        vp = str(CLAUDE_DIR / "scripts" / "vocab")
        if vp not in sys.path:
            sys.path.insert(0, vp)
        import dict_sources, anki_from_word  # type: ignore
        ec = dict_sources.lookup_ecdict(word)
        lemma = (ec or {}).get("lemma") or word
        path = anki_from_word._word_path(lemma)
        if not path.exists():
            return
        fm = _vocab_read_fm(path)
        if fm.get("anki_card_id"):   # 已有卡，跳过
            return
        eff = _effective_lookup_count(lemma, cooldown_h)
        if eff < th:
            return
        anki_from_word.make_card(lemma)
        sys.stderr.write(f"auto-anki: created card for {lemma!r} (有效遇到 {eff}≥{th})\n")
    except Exception as ex:
        sys.stderr.write(f"auto-anki fail {word!r}: {ex}\n")


def _trigger_vocab_note_async(word: str, pdf_rel: str, page: int, context: str = ""):
    """后台线程跑 build_vocab_note，不阻塞 dict API 响应。"""
    import threading
    def _run():
        try:
            _, bvn = _vocab_modules()
            if bvn is None:
                return
            bvn.update_word_note(
                word,
                add_source={"pdf": pdf_rel, "page": page, "context": context} if pdf_rel else None,
                online=True,
                download_audio=True,
            )
            # 笔记写完(lookup_count 已 +1) → 检查是否达阈值自动加卡
            _maybe_auto_anki(word)
        except Exception as ex:
            sys.stderr.write(f"vocab note bg fail for {word!r}: {ex}\n")
    threading.Thread(target=_run, daemon=True).start()


def _trigger_paragraph_exposure_async(pdf_rel: str, page: int, lemma: str):
    """段落扫描：用户查某词时，扫该词所在段落 + 当前句子之前的所有词，
    凡是已查过的词（在 vocab_index）+ 在阅读路径上 → mastery 加点。"""
    import threading
    def _run():
        try:
            import sys
            vp = CLAUDE_DIR / "scripts" / "vocab"
            if str(vp) not in sys.path:
                sys.path.insert(0, str(vp))
            # 让 vocab note 先写完才扫（避免新创建的词没在 vocab_index 里）
            import time as _t
            _t.sleep(1.5)
            import paragraph_exposure  # type: ignore
            paragraph_exposure.process_lookup(pdf_rel, page, lemma)
        except Exception as ex:
            sys.stderr.write(f"paragraph exposure fail: {ex}\n")
    threading.Thread(target=_run, daemon=True).start()


def _trigger_jp_note_async(lemma, reading, meaning, examples, forms, pdf_rel, page, context):
    """日语查词 → 后台建/更新 vault vocab 笔记(英日同库)+ 句子暴露。镜像英语 _trigger_vocab_note_async。"""
    import threading
    def _run():
        try:
            _, bvn = _vocab_modules()
            if bvn is None or not hasattr(bvn, "update_jp_word_note"):
                return
            bvn.update_jp_word_note(
                lemma, reading=reading, meaning=meaning, examples=examples, forms=forms,
                add_source={"pdf": pdf_rel, "page": page, "context": context} if pdf_rel else None)
            try: _maybe_auto_anki(lemma)
            except Exception: pass
            _jp_exposure(context or "", lemma)   # 同句其他已入库日语词 +mastery(被动暴露=大概率认识)
        except Exception as ex:
            sys.stderr.write(f"jp note bg fail for {lemma!r}: {ex}\n")
    threading.Thread(target=_run, daemon=True).start()


def _jp_exposure(context: str, looked_lemma: str):
    """日语句子暴露:context(整句)里**其他**已入 vocab 库的日语词 → +mastery(看到没查=大概率认识)。
    复用英语 paragraph_exposure._bump_mastery(语言无关:按 lemma 给笔记加分)。"""
    if not (context or "").strip():
        return
    try:
        tagger = _get_jp_tagger()
        idx = _vocab_idx()
        if not tagger or not idx:
            return
        import sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in sys.path:
            sys.path.insert(0, str(vp))
        import paragraph_exposure  # type: ignore
        looked = (looked_lemma or "").lower()
        seen = set()
        for tok in tagger(context):
            surf = tok.surface
            base = (getattr(tok.feature, "lemma", "") or surf)
            info = idx.get(surf.lower()) or idx.get(base.lower())
            if not info:
                continue
            lm = info["lemma"].lower()
            if lm == looked or lm in seen:
                continue
            seen.add(lm)
            try: paragraph_exposure._bump_mastery(lm, 0.05)
            except Exception: pass
    except Exception as ex:
        sys.stderr.write(f"jp exposure fail: {ex}\n")


# ── 每本书的文本语言声明(影响查词路由:汉字词按 ja 走中日 / 按 zh 当中文)──
_BOOK_LANGS_PATH = CLAUDE_DIR / "state" / "pdf-book-langs.json"
_VALID_LANGS = {"en", "ja", "zh", "ko", "fr", "de"}


def _load_book_langs() -> dict:
    try:
        return json.loads(_BOOK_LANGS_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _book_langs_for(rel: str) -> list:
    return _load_book_langs().get(rel, [])


@bp.route("/api/book-langs")
def pdf_api_book_langs_get():
    return jsonify({"ok": True, "langs": _book_langs_for(request.args.get("file", ""))})


@bp.route("/api/book-langs", methods=["POST"])
def pdf_api_book_langs_set():
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    if not rel:
        return jsonify({"ok": False, "error": "no file"}), 400
    langs = [l for l in (data.get("langs") or []) if l in _VALID_LANGS]
    allm = _load_book_langs()
    allm[rel] = langs
    try:
        _BOOK_LANGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BOOK_LANGS_PATH.write_text(json.dumps(allm, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "langs": langs})


# ──────── 每本书:插图 AI 描述 + 徽标 开关(默认关) ────────
# 不是每本书都需要这功能,且懒描述会逐页烧 AI 配额 → 默认关闭,在 PDF 设置「阅读」里逐本开。
_BOOK_FIG_PATH = CLAUDE_DIR / "state" / "pdf-book-figures.json"


def _load_book_fig() -> dict:
    try:
        return json.loads(_BOOK_FIG_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _book_fig_enabled(rel: str) -> bool:
    return bool(_load_book_fig().get(rel, False))   # 默认 False


@bp.route("/api/book-figures")
def pdf_api_book_figures_get():
    return jsonify({"ok": True, "enabled": _book_fig_enabled(request.args.get("file", ""))})


@bp.route("/api/book-figures", methods=["POST"])
def pdf_api_book_figures_set():
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    if not rel:
        return jsonify({"ok": False, "error": "no file"}), 400
    enabled = bool(data.get("enabled"))
    was = _load_book_fig().get(rel, False)
    allm = _load_book_fig()
    allm[rel] = enabled
    try:
        _BOOK_FIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BOOK_FIG_PATH.write_text(json.dumps(allm, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    # 刚开启 → 后台立刻跑一轮 YOLO 框图 + 裁图描述(不必等夜间)。仅 PDF:EPUB 图是嵌入资源,
    # 按需走 /api/epub-img-describe 描述,不需要(也不能)跑 PDF 页面的 YOLO/裁图管线。
    if enabled and not was and rel.lower().endswith(".pdf"):
        ap = _safe_vault_path(rel)
        if ap:
            _threading.Thread(target=_run_figures_pipeline, args=(str(ap),), daemon=True).start()
    return jsonify({"ok": True, "enabled": enabled})


def _is_born_digital(abs_path: str) -> bool:
    """原生数字 PDF(转换自 epub / 出版社数字版):图和文字本就分开,图描述走嵌入图提取而非 YOLO。"""
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        import extract_pdf_figures as EF  # type: ignore
        import fitz
        d = fitz.open(abs_path)
        try:
            return EF.is_born_digital(d)
        finally:
            d.close()
    except Exception:
        return False


def _run_figures_pipeline(abs_path: str):
    """后台:框图 → 裁图描述(主 python)。开书启用/夜间 timer 都走它。低优先级(nice)别影响 webapp。
    **原生数字书(epub 转来的/数字版)**:直接拿嵌入图位置(`extract_pdf_figures`,秒级),**跳过 YOLO**;
    **扫描书**:跑 DocLayout-YOLO 框图(Pi CPU 慢 ~6.7s/页,整本可能几十分钟)。"""
    import subprocess
    sp = str(CLAUDE_DIR / "scripts")
    env = dict(os.environ)
    py = os.environ.get("APP_PYTHON") or sys.executable
    if _is_born_digital(abs_path):
        try:                                    # 原生数字:嵌入图提取(主 python,无需 YOLO)
            subprocess.run([py, f"{sp}/extract_pdf_figures.py", "--book", abs_path],
                           timeout=600, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    else:
        try:                                    # 扫描书:DocLayout-YOLO 框图
            subprocess.run(["nice", "-n", "19", "/home/bwicarus/doclayout-venv/bin/python",
                            f"{sp}/yolo_figures.py", "--book", abs_path],
                           timeout=7200, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        subprocess.run(["nice", "-n", "19", py, f"{sp}/describe_figures_batch.py", "--book", abs_path],
                       timeout=7200, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


_BOOK_CROP_PATH = CLAUDE_DIR / "state" / "pdf-book-crop.json"


def _load_book_crop() -> dict:
    try:
        return json.loads(_BOOK_CROP_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _book_crop_for(rel: str) -> dict:
    """每本书的去边百分比 {l,r,t,b}(左/右/上/下各隐藏 %)。无配置 → 全 0。"""
    d = _load_book_crop().get(rel) or {}
    out = {}
    for k in ("l", "r", "t", "b"):
        try:
            out[k] = max(0.0, min(45.0, float(d.get(k, 0) or 0)))
        except Exception:
            out[k] = 0.0
    return out


@bp.route("/api/book-crop")
def pdf_api_book_crop_get():
    return jsonify({"ok": True, "crop": _book_crop_for(request.args.get("file", ""))})


@bp.route("/api/book-crop", methods=["POST"])
def pdf_api_book_crop_set():
    """设置某书去边百分比。每边 0–45%,且左+右、上+下各 < 90(防裁没)。"""
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    if not rel:
        return jsonify({"ok": False, "error": "no file"}), 400
    c = data.get("crop") or {}
    crop = {}
    for k in ("l", "r", "t", "b"):
        try:
            crop[k] = max(0.0, min(45.0, float(c.get(k, 0) or 0)))
        except Exception:
            crop[k] = 0.0
    if crop["l"] + crop["r"] > 90:
        crop["l"] = crop["r"] = min(crop["l"], crop["r"], 45)
    if crop["t"] + crop["b"] > 90:
        crop["t"] = crop["b"] = min(crop["t"], crop["b"], 45)
    allm = _load_book_crop()
    allm[rel] = crop
    try:
        _BOOK_CROP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BOOK_CROP_PATH.write_text(json.dumps(allm, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "crop": crop})


# ──────── 书籍目录(TOC):有原生目录默认用原生;「建立目录」AI 整页识图抽出可覆盖 ────────
# 目录是 provenance(图描述/助手都要知道「书的哪一章节」)的权威来源,也补「书没标准化分段」缺口。
# sidecar: state/pdf-toc/<book-sha>.json = {range:{start,end}, entries:[{title,page,level}], built_at}
_BOOK_TOC_DIR = CLAUDE_DIR / "state" / "pdf-toc"

def _toc_path_abs(abs_path) -> Path:
    return _BOOK_TOC_DIR / f"{_book_sha(abs_path)}.json"

def _toc_load_abs(abs_path) -> dict:
    try:
        p = _toc_path_abs(abs_path)
        return json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}

def _toc_save_abs(abs_path, data: dict):
    _BOOK_TOC_DIR.mkdir(parents=True, exist_ok=True)
    p = _toc_path_abs(abs_path)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)

def _native_toc_entries(abs_path) -> list:
    """PDF 自带目录 → [{title,page(印刷不可知→PDF页),level}]。get_toc 的 page 是 PDF 页(1基)。"""
    try:
        import fitz
        doc = fitz.open(str(abs_path))
        try:
            toc = doc.get_toc() or []
        finally:
            doc.close()
        return [{"title": str(t[1]).strip(), "page": int(t[2]), "level": int(t[0])} for t in toc if t[2]]
    except Exception:
        return []

# 页偏移 PDF页-印刷页(前端 localStorage 的服务端镜像,供后台 describe/provenance 把目录印刷页对齐)
_BOOK_OFFSET_PATH = CLAUDE_DIR / "state" / "pdf-book-offset.json"
def _page_offset_for(rel: str) -> int:
    try:
        return int(json.loads(_BOOK_OFFSET_PATH.read_text("utf-8")).get(rel, 0) or 0)
    except Exception:
        return 0

def _effective_toc(abs_path, rel: str = "") -> tuple[list, str]:
    """有效目录(page 归一到**印刷页**):自定义(建过的,page 本就是印刷页)优先覆盖原生(get_toc 是 PDF 页→减 offset)。"""
    cust = _toc_load_abs(abs_path).get("entries")
    if cust:
        return cust, "custom"
    nat = _native_toc_entries(abs_path)
    if nat:
        off = _page_offset_for(rel) if rel else 0
        return [{**e, "page": e["page"] - off} for e in nat], "native"
    return [], "none"

def _book_location(abs_path, printed_page: int, rel: str = "") -> str:
    """provenance:该**印刷页**所属章节标题(取 page ≤ 当前的最深一条)。无目录 → 空串。"""
    entries, _ = _effective_toc(abs_path, rel)
    cands = [e for e in entries if e.get("page") and e["page"] <= printed_page]
    if not cands:
        return ""
    return (max(cands, key=lambda e: e["page"]).get("title") or "").strip()


def _claude_vision_pages(pngs: list, prompt: str, timeout=240):
    """把多张页面图 + 一段 prompt 发给视觉模型(脱壳 claude + Gemini 双后端,按「看图」预设 + 互为兜底),
    返回结果文本。建目录用。prompt 自包含 → 不另加系统提示(system="")。
    (2026-07:删掉从未生效的 model/effort 死参数,模型统一由「看图」action 预设决定。)"""
    import base64
    A = _assistant()
    images = [{"media_type": "image/png", "b64": base64.b64encode(png).decode("ascii")} for png in pngs]
    return A.reader_vision(images, prompt, action="vision", uid=_reader_uid(), system="", timeout=timeout)


def _build_toc_job(jid: str, abs_path, rel: str, start: int, end: int):
    """后台:渲染目录页(start..end,PDF 页)→ 整页图 + 文字层 → AI 抽目录 → 存 sidecar。"""
    import fitz
    try:
        doc = fitz.open(str(abs_path))
        npages = doc.page_count
        s, e = max(1, start), min(npages, end)
        pngs, texts = [], []
        for pg in range(s, e + 1):
            p = doc[pg - 1]
            z = min(2.0, 1600.0 / (max(p.rect.width, p.rect.height) or 1.0))
            pngs.append(p.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png"))
            texts.append(f"[PDF第{pg}页文字层] " + " ".join((p.get_text("text") or "").split())[:1200])
        doc.close()
        _job_set(jid, status="running", step="AI 识别目录中…")
        prompt = (
            "下面是一本书**目录页**的扫描图(可能多张),附带各页 OCR 文字层(可能有错,以图为准)。\n"
            + "\n".join(texts) + "\n\n"
            "请把整个**目录(章节/小节标题 + 对应页码)**完整抽取出来。注意:\n"
            "- page 用目录里印的**书页码(印刷页码)**,原样填数字;\n"
            "- level 用层级(章=1,节=2,小节=3…,看缩进/编号判断);\n"
            "- 标题保留原文(含编号,如「1-1-2 共通フレーム」);别漏条目、别合并、别翻译。\n"
            "**只输出一个 JSON 数组**,每条 {\"title\":\"...\",\"page\":数字,\"level\":数字};"
            "不要 ``` 围栏、不要数组以外任何文字。"
        )
        raw = _claude_vision_pages(pngs, prompt)
        entries = _parse_toc(raw)
        if not entries:
            _job_set(jid, status="error", error="AI 没抽出目录(可识别页范围/换强模型重试)")
            return
        data = _toc_load_abs(abs_path)
        data.update({"entries": entries, "range": {"start": s, "end": e},
                     "built_at": int(time.time()), "source": "custom"})
        _toc_save_abs(abs_path, data)
        _job_set(jid, status="done", count=len(entries))
    except Exception as ex:
        _job_set(jid, status="error", error=str(ex))

def _parse_toc(raw):
    """解析 AI 回的目录 JSON 数组 → [{title,page,level}]。失败/空 → []。
    **顽强解析**:整体 json.loads 一旦因某一条目坏掉(最常见:title 里有未转义引号,如「§34-2 求"表观"运动」)就会全军覆没,
    所以坏了就退化到**逐对象**解析——单条坏的再用正则硬抠 title/page/level,一条坏数据绝不毁掉整本目录(实测 344 条只 1 条坏,旧逻辑 0 存活)。"""
    import re
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"):
            s = s[:-3]
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < 0:
        return []
    body = s[i:j + 1]
    arr = None
    try:
        arr = json.loads(body)
    except Exception:
        arr = None
    if not isinstance(arr, list):   # 整体坏 → 逐对象救
        arr = []
        for m in re.finditer(r'\{[^{}]*\}', body):
            o = m.group(0)
            try:
                arr.append(json.loads(o))
            except Exception:
                pm = re.search(r'"page"\s*:\s*(\d+)', o)
                lm = re.search(r'"level"\s*:\s*(\d+)', o)
                tm = re.search(r'"title"\s*:\s*"(.*)"\s*,\s*"page"', o, re.S)   # 贪婪到最后一个引号→容忍 title 内裸引号
                if pm and tm:
                    arr.append({"title": tm.group(1), "page": int(pm.group(1)),
                                "level": int(lm.group(1)) if lm else 1})
    out = []
    for e in (arr if isinstance(arr, list) else []):
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").strip()
        try:
            page = int(e.get("page"))
        except Exception:
            continue
        if not title or page < 1:
            continue
        try:
            level = max(1, int(e.get("level") or 1))
        except Exception:
            level = 1
        out.append({"title": title, "page": page, "level": level})
    return out


@bp.route("/api/toc")
def pdf_api_toc_get():
    """GET ?file= → {ok, exists, source(custom|native|none), count, range}。前端据此显示『已存在目录』。"""
    rel = request.args.get("file", "")
    ap = _safe_vault_path(rel)
    if not ap:
        return jsonify({"ok": False, "error": "bad file"}), 400
    entries, source = _effective_toc(ap, rel)
    rng = (_toc_load_abs(ap).get("range")) or {}
    return jsonify({"ok": True, "exists": bool(entries), "source": source,
                    "count": len(entries), "range": rng})


@bp.route("/api/page-offset", methods=["POST"])
def pdf_api_page_offset_set():
    """前端页码对齐时把 PDF页-印刷页 偏移镜像到服务端(供后台 describe/provenance 用)。{file, offset}。"""
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    if not rel:
        return jsonify({"ok": False, "error": "no file"}), 400
    try:
        off = int(data.get("offset") or 0)
    except Exception:
        off = 0
    try:
        m = json.loads(_BOOK_OFFSET_PATH.read_text("utf-8")) if _BOOK_OFFSET_PATH.exists() else {}
    except Exception:
        m = {}
    m[rel] = off
    try:
        _BOOK_OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BOOK_OFFSET_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
        tmp.replace(_BOOK_OFFSET_PATH)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@bp.route("/api/build-toc", methods=["POST"])
def pdf_api_build_toc():
    """POST {file, start, end} → 后台 AI 整页识图建目录(覆盖原生)。返回 {ok, jid} 供轮询。"""
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    ap = _safe_vault_path(rel)
    if not ap:
        return jsonify({"ok": False, "error": "bad file"}), 400
    try:
        start = int(data.get("start")); end = int(data.get("end"))
    except Exception:
        return jsonify({"ok": False, "error": "目录起止页要填数字"}), 400
    if start < 1 or end < start or (end - start) > 30:
        return jsonify({"ok": False, "error": "起止页不合法(end≥start,且范围≤30页)"}), 400
    import time as _t
    jid = "toc" + str(int(_t.time() * 1000))
    _job_set(jid, status="running", step="渲染目录页…", ts=int(_t.time()))
    _threading.Thread(target=_build_toc_job, args=(jid, ap, rel, start, end), daemon=True).start()
    return jsonify({"ok": True, "jid": jid})


@bp.route("/api/build-toc-status")
def pdf_api_build_toc_status():
    """GET ?jid= → {status:running|done|error, step?, count?, error?}。"""
    j = dict(_JOBS.get(request.args.get("jid", "")) or {})
    if not j:
        return jsonify({"ok": False, "error": "no job"}), 404
    return jsonify({"ok": True, **{k: j.get(k) for k in ("status", "step", "count", "error")}})


# ──────── 文字层校准:per-book per-page 偏移(扫描/OCR 书文字层没对齐时手动微调) ────────
_CHAR_OFFSET_PATH = CLAUDE_DIR / "state" / "pdf-char-offset.json"


def _load_char_offsets() -> dict:
    try:
        return json.loads(_CHAR_OFFSET_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _char_offset_for(rel: str, page: int) -> dict:
    """该书该页文字层偏移 {dx,dy,scale}(pt;dx/dy 平移、scale 缩放,绕页原点)。无 → 0/0/1。"""
    d = (_load_char_offsets().get(rel) or {}).get(str(page)) or {}
    out = {"dx": 0.0, "dy": 0.0, "scale": 1.0}
    for k in ("dx", "dy"):
        try:
            out[k] = float(d.get(k, 0) or 0)
        except Exception:
            out[k] = 0.0
    try:
        out["scale"] = max(0.5, min(2.0, float(d.get("scale", 1) or 1)))
    except Exception:
        out["scale"] = 1.0
    return out


def _apply_char_offset(chars: list, ofs: dict) -> None:
    """就地把偏移应用到 char bbox(只动 x0/x1/y0/y1;w 是词 id 不能动)。"""
    dx, dy, sc = ofs["dx"], ofs["dy"], ofs["scale"]
    if dx == 0 and dy == 0 and sc == 1.0:
        return
    for c in chars:
        if "x0" in c:
            c["x0"] = c["x0"] * sc + dx
        if "x1" in c:
            c["x1"] = c["x1"] * sc + dx
        if "y0" in c:
            c["y0"] = c["y0"] * sc + dy
        if "y1" in c:
            c["y1"] = c["y1"] * sc + dy


# ──────── 单页重扫(重新 OCR)覆盖:per-book per-page 存重新 OCR 的 chars,优先于 PDF 嵌入文字层 ────────
_PAGE_OCR_DIR = CLAUDE_DIR / "state" / "pdf-page-ocr"


def _page_ocr_path(rel: str, page: int) -> Path:
    import hashlib
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    return _PAGE_OCR_DIR / f"{sha}-p{page}.json"


def _page_ocr_override_sig(rel: str, page: int) -> str:
    p = _page_ocr_path(rel, page)
    try:
        return str(int(p.stat().st_mtime))
    except Exception:
        return ""


def _page_ocr_override_load(rel: str, page: int):
    p = _page_ocr_path(rel, page)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text("utf-8"))
        if isinstance(d, dict) and isinstance(d.get("chars"), list):
            return d
    except Exception:
        pass
    return None


def _page_ocr_override_save(rel: str, page: int, chars, page_w, page_h, furigana=None) -> None:
    p = _page_ocr_path(rel, page)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"chars": chars, "page_w": page_w, "page_h": page_h,
                             "furigana": furigana or []}, ensure_ascii=False), "utf-8")


def _page_ocr_override_clear(rel: str, page: int) -> bool:
    p = _page_ocr_path(rel, page)
    try:
        if p.exists():
            p.unlink()
            return True
    except Exception:
        pass
    return False


def _reocr_page_vision(rel: str, abs_path, page: int, dpi: int = 300) -> int:
    """单页重新 OCR(Google Vision DOCUMENT_TEXT_DETECTION)→ 转 char 格式存覆盖 sidecar。返回字符数。
    坐标:Vision 给视觉(渲染后)图像像素 → ×(pt/px) 转 PDF 点,跟 page-chars(rawdict)/页图同一坐标系。"""
    import fitz
    doc = fitz.open(str(abs_path))
    try:
        if page < 1 or page > doc.page_count:
            raise ValueError("page out of range")
        p = doc[page - 1]
        if p.rotation:
            raise RuntimeError(f"该页有旋转({p.rotation}°),单页重扫暂不支持")
        # 长边封顶 4000px(同 build 流程):巨幅扫描页 300dpi 会撑爆 Vision 40MB 请求上限
        long_pt = max(p.rect.width, p.rect.height) or 1.0
        zoom = min(dpi / 72.0, 4000.0 / long_pt)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img_bytes = pix.tobytes("jpg", jpg_quality=90)   # JPEG 比 PNG 小一个量级,OCR 无可感损失
        img_w, img_h = pix.width, pix.height
        page_w, page_h = p.rect.width, p.rect.height
    finally:
        doc.close()
    sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
    from google_vision_ocr import ocr_one_page, _load_key
    res = ocr_one_page(_load_key(), img_bytes)
    sx = (page_w / img_w) if img_w else 1.0
    sy = (page_h / img_h) if img_h else 1.0
    out = []
    for ch in res.get("chars", []):
        bb = ch.get("bbox")
        if not bb or len(bb) != 4:
            continue
        x0, y0, x1, y1 = bb
        c = {"c": ch.get("c", ""), "x0": x0 * sx, "y0": y0 * sy, "x1": x1 * sx, "y1": y1 * sy,
             "w": ch.get("w", -1), "bk": ch.get("bk", -1)}
        if ch.get("sp"):
            c["sp"] = 1
        out.append(c)
    _page_ocr_override_save(rel, page, out, page_w, page_h)
    return len(out)


@bp.route("/api/reocr-page", methods=["POST"])
def pdf_api_reocr_page():
    """单页重扫:对当前页重跑 Google Vision OCR → 覆盖该页文字层(修识别错/漏/对不齐)。
    body {file, page}。同步(单页 ~2-5s,nginx 300s 内)。完成后 cv 变 → 前端重渲拿新文字层。"""
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    page = int(data.get("page", 0) or 0)
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        n = _reocr_page_vision(rel, abs_path, page)
        return jsonify({"ok": True, "chars": n, "cv": _page_content_version(abs_path, rel, page)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@bp.route("/api/reocr-page/clear", methods=["POST"])
def pdf_api_reocr_clear():
    """撤销单页重扫:删覆盖 sidecar → 回到 PDF 原嵌入文字层。"""
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    page = int(data.get("page", 0) or 0)
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    cleared = _page_ocr_override_clear(rel, page)
    return jsonify({"ok": True, "cleared": cleared, "cv": _page_content_version(abs_path, rel, page)})


@bp.route("/api/char-offset")
def pdf_api_char_offset_get():
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    return jsonify({"ok": True, "offset": _char_offset_for(rel, page)})


@bp.route("/api/char-offset", methods=["POST"])
def pdf_api_char_offset_set():
    """设置某书某页文字层偏移。dx/dy(pt) 平移、scale(0.5–2)缩放。归零=删除该页记录。"""
    data = request.get_json(silent=True) or {}
    rel = data.get("file", "")
    page = int(data.get("page", 0) or 0)
    if not rel or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        dx = float(data.get("dx", 0) or 0)
        dy = float(data.get("dy", 0) or 0)
        sc = max(0.5, min(2.0, float(data.get("scale", 1) or 1)))
    except Exception:
        return jsonify({"ok": False, "error": "bad numbers"}), 400
    allofs = _load_char_offsets()
    book = allofs.setdefault(rel, {})
    if dx == 0 and dy == 0 and sc == 1.0:
        book.pop(str(page), None)            # 归零 → 清掉该页记录
    else:
        book[str(page)] = {"dx": round(dx, 2), "dy": round(dy, 2), "scale": round(sc, 4)}
    if not book:
        allofs.pop(rel, None)
    try:
        _CHAR_OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CHAR_OFFSET_PATH.write_text(json.dumps(allofs, ensure_ascii=False), "utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    abs_path = _safe_vault_path(rel)
    cv = _page_content_version(abs_path, rel, page) if abs_path else None
    return jsonify({"ok": True, "offset": _char_offset_for(rel, page), "cv": cv})


@bp.route("/api/phrases", methods=["GET", "POST", "DELETE"])
def pdf_api_phrases():
    """收藏词组（全局，作为分词依据）。
    GET → {ok, phrases:[...]}
    POST {text} → 添加；DELETE {text} → 删除。"""
    if request.method == "GET":
        return jsonify({"ok": True, "phrases": _phrases_load()})
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text or len(text) > 60:
        return jsonify({"ok": False, "error": "invalid text"}), 400
    lst = _phrases_load()
    if request.method == "POST":
        if text not in lst:
            lst.append(text)
            _phrases_save(lst)
        return jsonify({"ok": True, "phrases": lst, "added": text})
    # DELETE
    lst = [p for p in lst if p != text]
    _phrases_save(lst)
    return jsonify({"ok": True, "phrases": lst, "removed": text})


@bp.route("/api/phrase-mark", methods=["GET", "POST"])
def pdf_api_phrase_mark():
    """词组「已掌握」(跟单词掌握分开,不建幽灵 vocab 笔记)。
    GET → {ok, mastered:[归一化键...]}
    POST {text, mark} → mark∈{mastered,known,1,true} 标掌握;否则取消。
    标掌握的词组也会作为分词单元参与合并(单击整词组),且不再画生词下划线。"""
    if request.method == "GET":
        return jsonify({"ok": True, "mastered": sorted(_phrase_marks_load())})
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text or len(text) > 60:
        return jsonify({"ok": False, "error": "invalid text"}), 400
    mark = str(data.get("mark") or "").strip().lower()
    on = mark in ("mastered", "known", "1", "true", "yes")
    key = _phrase_norm(text)
    s = _phrase_marks_load()
    if on:
        s.add(key)
    else:
        s.discard(key)
    _phrase_marks_save(s)
    return jsonify({"ok": True, "mastered": sorted(s), "marked": on, "key": key})


def _en_word_mastered(lemma: str) -> bool:
    """英语词当前是否「已掌握」(vocab_index label_slug=='mastered')。给单词小框掌握按钮初始态。"""
    try:
        import sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in sys.path:
            sys.path.insert(0, str(vp))
        import vocab_index  # type: ignore
        info = (vocab_index.index() or {}).get((lemma or "").lower())
        return bool(info and info.get("label_slug") == "mastered")
    except Exception:
        return False


@bp.route("/api/dict-quick")
def pdf_api_dict_quick():
    """单词小框用：只查 ECDICT 核心（音标 + 中英释义 + lemma/forms），本地秒回；
    同时后台触发 vocab note 生成（制卡数据补全）。完整 mw/free 走 /api/dict SSE「展开」。"""
    word_raw = (request.args.get("word", "") or "").strip()
    word = word_raw.lower()
    pdf_rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    context = request.args.get("context", "")
    if not word:
        return jsonify({"ok": False, "error": "no word"})
    ds, _ = _vocab_modules()
    if not ds:
        return jsonify({"ok": False, "error": "dict unavailable"})
    # 本书声明的语言(前端传 ?langs=en,ja;没传则按 rel 读存储)。决定汉字词走哪。
    langs = [l for l in (request.args.get("langs", "") or "").split(",") if l]
    if not langs and pdf_rel:
        langs = _book_langs_for(pdf_rel)
    word_is_cjk = bool(getattr(ds, "is_japanese", None)) and ds.is_japanese(word_raw)
    # 声明了 ja → 汉字/假名词走中日;声明了但没 ja → 不当日语;没声明 → 自动判断(is_japanese)
    want_ja = word_is_cjk and ((not langs) or ("ja" in langs))
    # 日语词 → 走 AI 中日词典(无免费离线中日库;Claude Haiku + 永久缓存)
    if want_ja:
        # 活用形先还原成原形再查释义:確認します/確認した/確認して 都→確認する 共用一份缓存
        # (否则每个变形各调一次 AI、各存一份,既慢又浪费。变形说明仍按选中原文单独显示)
        _inf = _jp_inflection(word_raw)
        _lookup = (_inf or {}).get("base") or word_raw
        jp = ds.lookup_jp(_lookup, context=context, langs=langs)   # 传本书语言 → 防中日同形异义
        _cstat("dict_jp.cache" if (jp or {}).get("from_cache") else "dict_jp.ai")
        if not jp or (jp.get("zh") in ("(无)", "", None)):
            return jsonify({"ok": False, "word": word_raw, "jp": True})
        ex = [e for e in (jp.get("examples") or []) if isinstance(e, dict)][:2]
        # zh 未翻译则回退英文;Tanaka 母语例句即使没中文也先给原句+英译
        ex_txt = "\n".join(
            f"· {e.get('ja','')} — {e.get('zh') or e.get('en') or ''}" for e in ex)
        try:
            _append_lookup_log(word_raw, word_raw, pdf_rel, page, context)
            _jp_vocab_bump(word_raw)   # 日语生词高亮：查过即记，按熟悉度上色
        except Exception:
            pass
        # unidic 权威读音 + 声调(ピッチアクセント),离线毫秒级,覆盖 AI 读音
        ra = {}
        try:
            ra = ds._jp_reading_accent(word_raw) or {}
        except Exception:
            pass
        reading = ra.get("reading") or jp.get("reading", "")
        # 入统一 vocab 库:查词→建/更新 vault 笔记(原形为键,表层入 forms)+ 句子暴露(同英语)
        _trigger_jp_note_async(_lookup, reading, jp.get("zh", ""), jp.get("examples") or [],
                               [word_raw, _lookup], pdf_rel, page, context)
        return jsonify({
            "ok": True, "jp": True, "word": word_raw, "lemma": word_raw, "forms": [],
            "phonetic": reading + (f" [{jp['romaji']}]" if jp.get("romaji") else ""),
            "reading": reading,
            "accent": ra.get("accent"),       # 重音核:0=平板,N=第 N 拍后下降
            "mora": ra.get("mora"),
            "mastered": (_vocab_idx().get((_lookup or word_raw).lower()) or {}).get("label_slug") == "mastered",   # 掌握按钮初始态:读统一 vocab 库
            "pos": jp.get("pos", ""),          # 词性单独给前端(小框里做暗色标签,跟含义区分)
            "inflect": _inf,                       # 变形分析:原形 + 中文语法标签(过去た/否定ない/て形…)
            "translation": (jp.get("zh") or ""),
            "definition": ex_txt,
            "examples": ex,                    # [{ja, zh, en}] 结构化,前端可富渲染
            "examples_src": jp.get("examples_src", ""),
            "from_cache": jp.get("from_cache", False),
        })
    ec = ds.lookup_ecdict(word)
    if not ec:
        return jsonify({"ok": False, "word": word})
    lemma = ec["lemma"]; forms = ec["forms"]
    zh_defs = []; en_defs = []
    for d in ds._ec_definitions(ec):
        pos = d.get("pos") or ""
        if d.get("zh"):
            zh_defs.append((f"{pos} " if pos else "") + d["zh"])
        elif d.get("en"):
            en_defs.append((f"{pos} " if pos else "") + d["en"])
    try:
        _append_lookup_log(word, lemma, pdf_rel, page, context)
        if pdf_rel and page > 0:
            _trigger_vocab_note_async(word, pdf_rel, page, context)
            _trigger_paragraph_exposure_async(pdf_rel, page, lemma)
        else:
            _trigger_vocab_note_async(word, "", 0, "")
    except Exception:
        pass
    # 真人音频：若该词已有 vocab 笔记（之前查过），带上 audio_us 供小框喇叭播放（否则前端退化 TTS）
    audio = ""
    try:
        import anki_from_word  # type: ignore
        _p = anki_from_word._word_path(lemma)
        if _p.exists():
            _fm = _vocab_read_fm(_p)
            audio = _fm.get("audio_us") or _fm.get("audio_uk") or ""
    except Exception:
        pass
    return jsonify({
        "ok": True, "word": word, "lemma": lemma, "forms": forms,
        "phonetic": ("/" + ec.get("phonetic", "") + "/") if ec.get("phonetic") else "",
        "translation": "\n".join(zh_defs[:8]),
        "definition": "\n".join(en_defs[:6]),
        "freq_bnc": ec.get("bnc", 0),
        "audio": audio,
        "mastered": _en_word_mastered(lemma),   # 掌握按钮初始态(跟日语对称)
    })


def _kanji_fill_zh(kanji: list, do_translate: bool) -> None:
    """给汉字拆解填 meanings_zh(KANJIDIC 英文字义→中文)。do_translate=False 只读缓存;
    True 则把未缓存的用 Google batch 翻译并落缓存。"""
    if not kanji:
        return
    try:
        import sys as _sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in _sys.path:
            _sys.path.insert(0, str(vp))
        from translate import gtranslate_batch as _gb, _cache_get as _cg, _cache_put as _cp
    except Exception:
        return
    for k in kanji:
        ms = k.get("meanings") or []
        k["_mstr"] = "; ".join(ms) if isinstance(ms, list) else str(ms)
        if k["_mstr"]:
            c = _cg(k["_mstr"], "zh-CN")
            if c:
                k["meanings_zh"] = c
    if do_translate:
        miss = [k for k in kanji if k.get("_mstr") and not k.get("meanings_zh")]
        if miss:
            try:
                res = _gb([k["_mstr"] for k in miss]) or []
            except Exception:
                res = []
            if len(res) == len(miss):
                for k, z in zip(miss, res):
                    z = (z or "").strip()
                    if z:
                        k["meanings_zh"] = z
                        try: _cp(k["_mstr"], "zh-CN", z, "gtranslate")
                        except Exception: pass
    for k in kanji:
        k.pop("_mstr", None)


def _jp_dict_bg_translate(word: str, kanji: list) -> None:
    """后台线程:翻例句(jp_examples_zh translate=True 落缓存) + 汉字字义(_kanji_fill_zh translate=True)。
    不阻塞 dict-jp 响应;前端随后轮询 /api/dict-jp-zh 拿翻好的结果替换英文。"""
    import threading
    def _run():
        try:
            ds, _ = _vocab_modules()
            if ds:
                ds.jp_examples_zh(word, limit=5, translate=True)
            _kanji_fill_zh([dict(k) for k in (kanji or [])], do_translate=True)
        except Exception as ex:
            sys.stderr.write(f"[dict-jp bg] {word!r}: {ex}\n")
    threading.Thread(target=_run, daemon=True).start()


@bp.route("/api/dict-jp-zh")
def pdf_api_dict_jp_zh():
    """轮询用:只读缓存返回该词的例句中译 + 汉字字义中译(后台翻完即有)。前端拿来原地替换英文。
    GET ?word= → {ok, examples:[{ja,zh}], kanji:[{kanji,meanings_zh}]}"""
    word = (request.args.get("word", "") or "").strip()
    if not word:
        return jsonify({"ok": False})
    ds, _ = _vocab_modules()
    if not ds:
        return jsonify({"ok": False})
    # 跟 dict-jp 一致用原形(否则例句/汉字集不一致 → 前端按 index 替换错位)
    _base = (_jp_inflection(word) or {}).get("base") or word
    try:
        exs = ds.jp_examples_zh(_base, limit=5, translate=False)
    except Exception:
        exs = []
    kanji = ds.word_kanji_breakdown(_base)
    _kanji_fill_zh(kanji, do_translate=False)
    return jsonify({
        "ok": True,
        "examples": [{"ja": e.get("ja", ""), "zh": e.get("zh", "")} for e in exs],
        "kanji": [{"kanji": k.get("kanji", ""), "meanings_zh": k.get("meanings_zh", "")} for k in kanji],
    })


_FURIFIX_DIR = CLAUDE_DIR / "state" / "pdf-furigana-fix"


def _furifix_path(rel: str, page: int, mtime: int) -> Path:
    import hashlib
    _FURIFIX_DIR.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    return _FURIFIX_DIR / f"{sha}-p{page}-{mtime}.json"


@bp.route("/api/furigana-verify")
def pdf_api_furigana_verify():
    """振假名读音**按整页上下文 AI 校正**（通用解决计数器/熟字训/多音字读错，不硬编码）。
    GET ?file=&page= → {ok, fixes:[{i, r}]}（i=furigana 下标, r=纠正后平假名；只列改了的）。
    每页缓存(mtime 键)永久；前端 ruby 渲染后台调一次，拿 fixes 原地替换。"""
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        import fitz  # noqa: F401
        mtime = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mtime = 0
    fpath = _furifix_path(rel, page, mtime)
    if fpath.exists():
        try:
            return jsonify({"ok": True, "cached": True, "fixes": json.loads(fpath.read_text("utf-8"))})
        except Exception:
            pass
    res = _page_chars_cached(abs_path, rel, page)
    if res is None:
        return jsonify({"ok": False, "error": "page out of range"}), 400
    chars, _pw, _ph, furigana = res
    def _is_kana_rt(s):
        return bool(s) and all(("぀" <= ch <= "ヿ") or ch == "ー" for ch in s)
    # 只校「量词读音」(带 ctx 的接尾辞项):这是读音高发错误类，且少而集中 → prompt 极小、AI 快。
    cnt = [(i, it) for i, it in enumerate(furigana) if it.get("ctx") and _is_kana_rt(it.get("rt", ""))]
    if not cnt:
        try: fpath.write_text("[]", "utf-8")
        except Exception: pass
        return jsonify({"ok": True, "fixes": []})
    listing = "\n".join(f"{i}. {it['ctx']}（末尾「{it['wd']}」当前注 {it['rt']}）" for i, it in cnt[:30])
    prompt = (
        "下面每行是日语「数字/词 + 量词」组合，末尾量词的假名注音可能有误"
        "(如 365日 的「日」应读 にち、14日 读 か、3人 读 にん)。请给出**末尾量词**在该组合里的"
        "正确平假名读音。\n" + listing + "\n\n"
        '严格只输出 JSON 数组，每项 {"i":行号,"r":"末尾量词的正确平假名"}；'
        "每行都要给(即使本来就对)。不要解释。"
    )
    fixes = []
    try:
        raw = _ai_call(prompt, "dict")
        m = re.search(r"\[.*\]", raw or "", re.DOTALL)
        if m:
            arr = json.loads(m.group(0))
            valid_idx = {i for i, _ in cnt}
            for it in arr:
                if not isinstance(it, dict):
                    continue
                try: i = int(it.get("i"))
                except (TypeError, ValueError): continue
                r = (it.get("r") or "").strip()
                if i in valid_idx and r and _is_kana_rt(r) and len(r) <= 12:
                    if r != furigana[i].get("rt", ""):
                        fixes.append({"i": i, "r": r})
    except Exception as ex:
        sys.stderr.write(f"[furigana-verify] {rel} p{page}: {ex}\n")
    try:
        fpath.write_text(json.dumps(fixes, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return jsonify({"ok": True, "fixes": fixes})


@bp.route("/api/dict-jp")
def pdf_api_dict_jp():
    """日语词「完整字典」离线富内容(JSON,秒回):读音+音调+罗马字+词性+完整中文释义
    + 5 句母语例句(Tanaka,缓存中译/未译先回退英文+后台补译) + 汉字拆解(KANJIDIC 音读/训读/字义)。
    例句/汉字字义的中译走后台(_jp_dict_bg_translate)+ 前端轮询 /api/dict-jp-zh 原地替换,不增加首屏等待。
    深入讲解(用法/语感/近义辨析)走 /api/dict-jp-ai SSE,按需触发。"""
    word = (request.args.get("word", "") or "").strip()
    if not word:
        return jsonify({"ok": False, "error": "no word"})
    ds, _ = _vocab_modules()
    if not ds:
        return jsonify({"ok": False, "error": "dict unavailable"})
    # 活用形→原形再查释义/例句:確認します/確認した 都→確認する 共用缓存(快、省)
    _inf = _jp_inflection(word)
    _base = (_inf or {}).get("base") or word
    # 本书声明语言(?langs= 优先,否则按 file 读存储;都没有 → None=默认纯日语)。dict-jp 本就是日语路径。
    _langs = [l for l in (request.args.get("langs", "") or "").split(",") if l] \
        or _book_langs_for(request.args.get("file", "")) or None
    jp = ds.lookup_jp(_base, context=request.args.get("context", ""), langs=_langs)
    if not jp or (jp.get("zh") in ("(无)", "", None)):
        return jsonify({"ok": False, "word": word, "jp": True})
    ra = {}
    try:
        ra = ds._jp_reading_accent(word) or {}
    except Exception:
        pass
    reading = ra.get("reading") or jp.get("reading", "")
    try:
        # 完整字典也算一次「查过」→ 入统一 vocab 库(建/更新笔记 + 暴露)
        _trigger_jp_note_async(_base, reading, jp.get("zh", ""), jp.get("examples") or [],
                               [word, _base], request.args.get("file", ""),
                               int(request.args.get("page", "0") or "0"), request.args.get("context", ""))
    except Exception:
        pass
    # 跟英文单词一致:**秒回**——例句/汉字字义只取已缓存的中文,没翻的先回退英文;
    # 同时后台翻译(下一次轮询/重开就有中文),不增加首屏等待。前端轮询 /api/dict-jp-zh 自动替换。
    exs = ds.jp_examples_zh(_base, limit=5, translate=False)   # 例句也用原形(Tanaka 有 確認 没 確認します)
    kanji = ds.word_kanji_breakdown(_base)
    _kanji_fill_zh(kanji, do_translate=False)   # 只读缓存
    _jp_dict_bg_translate(_base, kanji)          # 后台翻例句 + 汉字字义(落缓存,按原形)
    return jsonify({
        "ok": True, "jp": True, "word": word,
        "reading": reading, "reading_kata": ra.get("reading_kata", ""),
        "accent": ra.get("accent"), "mora": ra.get("mora"),
        "romaji": jp.get("romaji", ""), "pos": jp.get("pos", ""),
        "zh": jp.get("zh", ""),
        "inflect": _inf,                   # 变形分析:原形 + 中文语法标签
        "examples": exs,
        "kanji": kanji,
    })


@bp.route("/api/jp-vocab-mark", methods=["POST"])
def pdf_api_jp_vocab_mark():
    """日语词掌握标记 → 跟英语**完全同一条路径** compute_mastery.apply_user_mark
    (写 vault vocab 笔记 user_mark + 锁 mastery)。笔记按**原形**为键(活用形先还原)。
    body: {word, mark: "known"|"unknown"|""|"forget"}（兼容旧 "mastered"=known）"""
    data = request.get_json(silent=True) or {}
    word = (data.get("word") or "").strip()
    mark = (data.get("mark") or "known").strip().lower()
    if not word:
        return jsonify({"ok": False, "error": "no word"}), 400
    # ⚠ 关键:**标记下划线实际命中的那个笔记**,而不是盲目还原原形再标。
    # 下划线 _build_jp_vocab_marks 是 idx.get(表层) → 否则 idx.get(原形)。迁移来的旧笔记可能按
    # **表层活用形**为键(jp-vocab.json 里有 削った/使わ/加え 这种),若标记只认原形会标到另一个笔记
    # → 下划线那个没变 → 标了也不消失。故按同一套解析找到 info,标 info['lemma']。
    base_inf = (_jp_inflection(word) or {}).get("base")
    idx = _vocab_idx()
    info = idx.get(word.lower()) or (idx.get(base_inf.lower()) if base_inf else None)
    target = (info or {}).get("lemma") or base_inf or word
    _, bvn = _vocab_modules()
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        import compute_mastery  # type: ignore
        import vocab_index      # type: ignore
        if mark == "forget":
            try:
                p = bvn._word_path(target)
                if p.exists(): p.unlink()
            except Exception:
                pass
            vocab_index.index(force_reload=True)
            return jsonify({"ok": True, "word": word, "mark": mark})
        # 确保笔记存在(没查过的词也能直接标)
        if bvn is not None and hasattr(bvn, "update_jp_word_note"):
            if not bvn._word_path(target).exists():
                bvn.update_jp_word_note(target, forms=[word, target], _new_source=False)
        m = mark if mark in ("known", "unknown", "") else ("known" if mark == "mastered" else "")
        result = compute_mastery.apply_user_mark(target, m)
        vocab_index.index(force_reload=True)   # 让下划线立即反映
        return jsonify(result if result.get("ok") else {"ok": True, "word": word, "mark": mark, "warn": result.get("error")})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


_JP_EXPLAIN_VER = 1   # 深入讲解 prompt 版本(改 prompt 就 +1 → 旧缓存自动重生成)


@bp.route("/api/dict-jp-ai")
def pdf_api_dict_jp_ai():
    """日语词「✨AI 深入讲解」SSE 流式:用法/语感/近义辨析/汉字记忆法。按需触发,耗 AI。

    服务端永久缓存(2026-06-10):同一个词(按原形)讲解过一次就存 dict-cache,再点
    **任何设备**都按同 SSE 格式秒回全文(此前每次都现场跑 AI 等数秒)。前端零改动。"""
    word = (request.args.get("word", "") or "").strip()
    context = request.args.get("context", "")
    if not word:
        return jsonify({"ok": False, "error": "no word"})
    ds, _ = _vocab_modules()
    # 活用形→原形共用一份缓存(確認します/確認した → 確認する)
    _base = ((_jp_inflection(word) or {}).get("base") or word)
    if ds:
        try:
            c = ds._cache_load("jp-explain", _base, ttl_days=3650)
        except Exception:
            c = None
        if c and c.get("v") == _JP_EXPLAIN_VER and c.get("md"):
            from flask import Response, stream_with_context

            def _replay(md=c["md"]):
                yield "event: start\ndata: {}\n\n"
                yield f"data: {json.dumps({'text': md}, ensure_ascii=False)}\n\n"
                yield "event: done\ndata: {}\n\n"
            return Response(stream_with_context(_replay()), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _save_explain(full, _b=_base, _ds=ds):
        if _ds and full and len(full) > 40:   # 太短多半是报错文本,不缓存
            try:
                _ds._cache_save("jp-explain", _b, {"v": _JP_EXPLAIN_VER, "md": full, "word": _b})
            except Exception:
                pass
    prompt = (
        f"你是资深日语老师,面向中文母语学习者讲解日语词「{word}」"
        + (f"(出现在句子:{context[:120]})" if context else "")
        + "。用简体中文,Markdown,简洁有条理,讲这几块(有才写):\n"
        "1. **核心义** — 多义项分点,每项给典型搭配\n"
        "2. **用法/语感** — 常见搭配、助词、敬体/口语差异、易错点\n"
        "3. **近义辨析** — 跟意思相近的日语词的区别(若有)\n"
        "4. **汉字记忆** — 各汉字音读/训读怎么记、构词规律\n"
        "不要寒暄,不要重复词本身的读音表格(前面已显示)。"
    )
    return _start_ai_stream(prompt, "dict", _reader_uid(), (request.args.get("rid") or "").strip(),
                            on_done=_save_explain)


@bp.route("/api/dict")
def pdf_api_dict():
    """字典查询（三源融合：ECDICT + Free Dict + MW Learner）。

    GET ?word=X [&file=&page=&context=]
    file/page/context 用来记录"在哪本 PDF 的哪页查的"，写入 lookup 日志 + vocab 笔记。

    Accept: text/event-stream → SSE 分段（ECDICT 先到 → free → mw → translate → done）
    否则 → 一次性返回完整 JSON（保留兼容）
    """
    # SSE 分支：让前端能立刻看到 ECDICT 中文释义，慢源后续追加
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        from flask import Response, stream_with_context
        word_arg = (request.args.get("word") or "").strip()
        pdf_rel  = (request.args.get("file") or "").strip()
        try: page = int(request.args.get("page") or 0)
        except (TypeError, ValueError): page = 0
        context = (request.args.get("context") or "").strip()[:500]
        return Response(
            stream_with_context(_dict_sse_stream(word_arg, pdf_rel, page, context)),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    word_raw = (request.args.get("word") or "").strip()
    word = word_raw.lower()
    if not word or len(word) > 50:
        return jsonify({"ok": False, "error": "invalid word"}), 400
    pdf_rel = (request.args.get("file") or "").strip()
    try:
        page = int(request.args.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    context = (request.args.get("context") or "").strip()[:500]

    ds, _ = _vocab_modules()
    if ds is None:
        # fallback：只 ECDICT
        if not _DICT_DB_PATH.exists():
            return jsonify({"ok": False, "error": "dict db missing"}), 500
        import sqlite3 as _sq
        try:
            conn = _sq.connect(f"file:{_DICT_DB_PATH}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT word, phonetic, translation, definition, exchange FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1", (word,))
            row = cur.fetchone()
            conn.close()
            if not row:
                return jsonify({"ok": False, "error": "not found"})
            return jsonify({"ok": True, "word": row[0], "phonetic": row[1] or "",
                            "translation": row[2] or "", "definition": row[3] or "", "exchange": row[4] or ""})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500

    # 主路径：三源融合
    try:
        entry = ds.compose_entry(word, online=True)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    if not entry:
        return jsonify({"ok": False, "error": "not found"})

    lemma = entry.get("lemma") or word
    # 副作用：日志 + 异步生成 vocab 笔记 + 段落扫描（用户读过但没查的词加 mastery）
    _append_lookup_log(word, lemma, pdf_rel, page, context)
    if pdf_rel and page > 0:
        _trigger_vocab_note_async(word, pdf_rel, page, context)
        _trigger_paragraph_exposure_async(pdf_rel, page, lemma)
    else:
        _trigger_vocab_note_async(word, "", 0, "")

    # 前端精简结构（保留旧字段兼容）
    zh_lines = []
    for d in entry["definitions"]:
        if d.get("zh"):
            pos = d.get("pos") or ""
            zh_lines.append((f"{pos} " if pos else "") + d["zh"])
    en_lines = []
    for d in entry["definitions"]:
        if d.get("en") and d.get("source") in ("ecdict_en", "wiktionary", "mw"):
            pos = d.get("pos") or ""
            en_lines.append((f"{pos} " if pos else "") + d["en"])
    return jsonify({
        "ok": True,
        "word": lemma,
        "lemma": lemma,
        "forms": entry.get("forms", []),
        "phonetic": entry["phonetics"]["us"] or entry["phonetics"]["uk"],
        "phonetic_us": entry["phonetics"]["us"],
        "phonetic_uk": entry["phonetics"]["uk"],
        "audio_us": entry["audio"]["us"],
        "audio_uk": entry["audio"]["uk"],
        "translation": "\n".join(zh_lines[:8]),
        "definition":  "\n".join(en_lines[:6]),
        "examples": entry.get("examples", [])[:6],
        "examples_zh": entry.get("examples_zh", {}),
        "synonyms": entry.get("synonyms", [])[:8],
        "antonyms": entry.get("antonyms", [])[:8],
        "freq_bnc": entry["freq"]["bnc"],
        "sources_hit": entry["sources_hit"],
        "vocab_note": f"资源/vocab/{lemma[0]}/{lemma}.md" if lemma else "",
    })


@bp.route("/api/translate", methods=["POST"])
def pdf_api_translate():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    target = (body.get("target_lang") or "中文").strip()
    if not text:
        return jsonify({"ok": False, "error": "无选中文本"}), 400
    if len(text) > 5000:
        return jsonify({"ok": False, "error": "文本过长（>5000 字），请缩小选区"}), 400
    prompt = (
        f"把以下内容翻译成{target}。如果原文已经是{target}，则翻译成英文。\n"
        f"严格只输出译文本身，不要解释、不要加引号、不要前后缀。\n"
        f"数学公式（$...$）保留原样不翻译。\n\n"
        f"原文：\n{text}"
    )
    uid = _reader_uid()
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        return _start_ai_stream(prompt, "translate", uid, (body.get("rid") or "").strip())
    try:
        out = _ai_call(prompt, "translate", uid).strip()
        return jsonify({"ok": True, "translation": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI 翻译失败：{ex}"}), 500


@bp.route("/api/to-note", methods=["POST"])
def pdf_api_to_note():
    """从选中文本创建笔记。AI 不整理（用户已选），但会加 PDF 来源行（文件名 + 页码 + 嵌入式 PDF rect）。"""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    name = (body.get("name") or "").strip()
    file_rel = (body.get("file") or "").strip()
    page = int(body.get("page") or 0)
    if not text or not name:
        return jsonify({"ok": False, "error": "缺少 text 或 name"}), 400
    safe = _sanitize_filename(name)
    if not safe.endswith(".md"):
        safe += ".md"
    note_path = OBSIDIAN_ROOT / safe
    if note_path.exists():
        stem = safe[:-3]
        for i in range(1, 200):
            cand = OBSIDIAN_ROOT / f"{stem}-{i}.md"
            if not cand.exists():
                note_path = cand; break
    src_line = ""
    if file_rel:
        # vault 内 PDF 直接 obsidian embed 引用
        if page > 0:
            src_line = f"\n\n> 来源：[[{file_rel}#page={page}]]"
        else:
            src_line = f"\n\n> 来源：[[{file_rel}]]"
    content = f"# {name}\n\n{text}{src_line}\n"
    try:
        note_path.write_text(content, encoding="utf-8")
    except Exception as ex:
        return jsonify({"ok": False, "error": f"写文件失败：{ex}"}), 500
    rel = note_path.relative_to(OBSIDIAN_ROOT).as_posix()
    vault_name = os.environ.get("OBSIDIAN_VAULT_NAME", "Obsidian Vault")
    obsidian_url = (
        f"obsidian://open?vault={urllib.parse.quote(vault_name, safe='')}"
        f"&file={urllib.parse.quote(rel[:-3] if rel.endswith('.md') else rel, safe='/')}"
    )
    return jsonify({"ok": True, "note_path": rel, "obsidian_url": obsidian_url})


# 非 PDF 但 MuPDF 能读的电子书格式 → 上传时**转成 PDF**(阅读器整条管线都基于 PDF:字符层/页图/公式裁图)
_EBOOK_EXTS = {".epub", ".mobi", ".fb2", ".xps", ".cbz"}


def _ebook_convert_bin():
    import shutil
    return shutil.which("ebook-convert")


def _spawn_survivable(cmd, cwd):
    """长后台任务放进**用户级 systemd transient scope**(独立 cgroup)→ 不随 webapp.service 重启/部署被 SIGKILL
    (默认 KillMode=control-group 会连同子进程一起杀,大书转换/电子版生成动辄几分钟,正好撞上部署就废)。
    需 linger(已 enable)+ XDG_RUNTIME_DIR;systemd-run 不可用则回退普通 detached(会被重启杀,但至少能跑)。"""
    import subprocess, shutil
    env = dict(os.environ); env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    kw = dict(cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sr = shutil.which("systemd-run")
    if sr and os.path.isdir(env["XDG_RUNTIME_DIR"]):
        try:
            return subprocess.Popen([sr, "--user", "--scope", "--quiet", "--collect"] + list(cmd), **kw)
        except Exception:
            pass
    return subprocess.Popen(list(cmd), start_new_session=True, **kw)


def _convert_ebook_to_pdf(src_file: Path, out_pdf: Path):
    """电子书(epub/mobi/fb2/xps/cbz) → 带文字层的分页 PDF。
    **首选 Calibre `ebook-convert`(业界标准)**:HTML/CSS 完整渲染 + 智能分页(图不被拦腰切两页)+ 字体子集化;
    没装/失败 → 回退 PyMuPDF `convert_to_pdf`(轻量但分页较糙)。"""
    import subprocess
    eb = _ebook_convert_bin()
    if eb:
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"   # headless(Pi 无显示)
        cmd = [eb, str(src_file), str(out_pdf),
               "--paper-size", "a4",
               "--pdf-default-font-size", "16",
               "--margin-top", "40", "--margin-bottom", "40",
               "--margin-left", "48", "--margin-right", "48",
               "--pdf-page-margin-top", "40"]
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and out_pdf.exists() and out_pdf.stat().st_size > 1000:
                return
        except Exception:
            pass
        # ebook-convert 失败 → 落到 PyMuPDF 兜底
    import fitz
    d = fitz.open(str(src_file))
    try:
        if getattr(d, "is_reflowable", False):
            d.layout(rect=fitz.paper_rect("a4"), fontsize=11)
        pdfbytes = d.convert_to_pdf()
    finally:
        d.close()
    out_pdf.write_bytes(pdfbytes)


# ════════════════════ EPUB 原生 reflow 阅读器 ════════════════════
# EPUB 本就是可重排文本,转固定页 PDF 是逆其本性(慢/图跨页/大书 OOM)。这里直接渲染:
# 服务端把 .epub 解包到 state/epub-extract/<sha>/(一次,按 mtime 缓存),epub.js 指向解包目录
# **按章/按图懒加载**(不把 155MB 整包塞进浏览器,iPad 不会崩)。AI 端点全复用(只要 text+context,
# 不需要 page)。坐标锚用 EPUB CFI 而非 page+rect。
_EPUB_EXTRACT_DIR = CLAUDE_DIR / "state" / "epub-extract"


def _epub_sha(rel: str) -> str:
    import hashlib
    return hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16]


def _ensure_epub_extracted(abs_path: Path, rel: str) -> Path | None:
    """把 epub(zip)解包到 state/epub-extract/<sha>/。按源文件 mtime 缓存:没变就复用。
    返回解包根目录(含 META-INF/container.xml),失败返回 None。防 zip-slip。"""
    import zipfile
    sha = _epub_sha(rel)
    root = _EPUB_EXTRACT_DIR / sha
    marker = root / ".extracted"
    try:
        src_mt = int(abs_path.stat().st_mtime)
    except OSError:
        return None
    # 已解包且源文件没更新 → 直接用
    if marker.exists():
        try:
            if int(marker.read_text("utf-8").strip() or "0") == src_mt and (root / "META-INF" / "container.xml").exists():
                return root
        except Exception:
            pass
        # 源变了 → 清旧解包
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(abs_path)) as z:
            root_resolved = root.resolve()
            for member in z.namelist():
                if member.endswith("/"):
                    continue
                # zip-slip 防护:解析后必须仍在 root 内
                dest = (root / member).resolve()
                try:
                    dest.relative_to(root_resolved)
                except ValueError:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as srcf, open(dest, "wb") as outf:
                    outf.write(srcf.read())
    except Exception:
        return None
    if not (root / "META-INF" / "container.xml").exists():
        return None   # 不是合法 epub
    try:
        marker.write_text(str(src_mt), "utf-8")
    except Exception:
        pass
    return root


# ── 收藏夹 EPUB 物化(规格 v5):收藏夹 = 一本真 EPUB(state/reader-fav-epub/<fid>.epub),用**完整 EPUB 阅读器**
#   (epub-html.js)打开 → 手写/侧栏AI助手/高亮/生词/振假名/语法/查词/翻译/插入页 全功能天然可用。
#   产物放 state(非 vault → 无 Obsidian Sync churn、天然不进书架/搜索);EPUB 阅读器各端点用 _resolve_epub_book
#   把「资源/收藏夹/<fid>.epub」这个合成 rel 解析回 state 里的真 .epub。epub-html.js / epub_html_reader.html 零改动。
_FAV_EPUB_DIR = CLAUDE_DIR / "state" / "reader-fav-epub"


def _resolve_epub_book(rel: str) -> Path | None:
    """把 epub rel 解析成绝对路径。**收藏夹物化 EPUB**(资源/收藏夹/<fid>.epub)住在 state/reader-fav-epub/
    (不在 vault);其余一律走 vault 的 _safe_vault_path。让 EPUB 阅读器各端点(manifest/section/css/search/
    section-paragraphs/助手 _eroot)无需分别改判定,只需把 _safe_vault_path 换成本函数即可支持收藏夹书。"""
    rel = (rel or "").lstrip("/")
    m = re.match(r"^资源/收藏夹/(f_[0-9a-zA-Z]+)\.epub$", rel)
    if m:
        p = _FAV_EPUB_DIR / (m.group(1) + ".epub")
        try:
            return p if p.is_file() else None
        except OSError:
            return None
    return _safe_vault_path(rel)


@bp.route("/epub/view")
def epub_view():
    """EPUB 原生 reflow 阅读器主页。?file=<vault-rel-of-.epub>。"""
    rel = request.args.get("file", "")
    abs_path = _safe_vault_path(rel)
    if not abs_path or abs_path.suffix.lower() != ".epub":
        abort(404)
    rel_clean = abs_path.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
    root = _ensure_epub_extracted(abs_path, rel_clean)
    if not root:
        abort(500)
    try:
        _epub_opf_info(root)   # 预热 OPF 缓存:浏览器随后并发发 section 请求时直接命中,不再每个 490ms 堵 worker
    except Exception:
        pass
    _lastopen_touch(rel_clean)
    sha = _epub_sha(rel_clean)
    from flask import make_response
    # 默认仍走功能齐全的「统一 HTML 阅读器」(生词/振假名/词组/语法/单词本/AI 全在);
    # ?engine=epubjs 走成熟 epub.js 引擎(渲染稳,功能正逐步移植到这条上,完成前不当默认)
    if (request.args.get("engine") or "") == "epubjs":
        resp = make_response(render_template(
            "epub_reader.html", file_rel=rel_clean, file_name=Path(rel_clean).name,
            epub_base=f"/pdf/epub/file/{sha}/", reader_js_v=_epub_js_v()))
    else:
        resp = make_response(render_template(
            "epub_html_reader.html", file_rel=rel_clean, file_name=Path(rel_clean).name,
            sha=sha, reader_js_v=_epub_js_v(),
            server_pos=_reading_pos_get(rel_clean)))   # 服务端续读位置(节 idx;无记录=None→null),epub-html.js onBuilt 消费
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ── 统一 HTML 阅读器(第一步:EPUB → 服务端消毒 HTML → 渲进主文档,不用 iframe)──
# 控制层得以像 PDF 字符层一样在同一文档里直接够到内容(选中/高亮/工具栏无 iframe 摩擦)。
_EPUB_OPF_CACHE = {}
_EPUB_OPF_LOCK = __import__("threading").Lock()


def _epub_opf_info(root: Path) -> dict:
    """带缓存:解析 OPF(677KB)+nav 很贵(~490ms,BeautifulSoup,CPU 密集 GIL 下不并行),
    而 section/search/manifest 每次都要它 → 开书并发涌入会把单 worker 堵死(图片/高亮 POST 超时失败)。
    按 sha+解包 mtime 缓存,解析一次永久复用。"""
    key = str(root)
    try:
        mt = int((root / ".extracted").stat().st_mtime)
    except Exception:
        mt = 0
    c = _EPUB_OPF_CACHE.get(key)
    if c and c[0] == mt:
        return c[1]
    with _EPUB_OPF_LOCK:
        c = _EPUB_OPF_CACHE.get(key)   # 双检:等锁时别人可能已解析好
        if c and c[0] == mt:
            return c[1]
        info = _epub_opf_info_uncached(root)
        _EPUB_OPF_CACHE[key] = (mt, info)
        return info


def _epub_opf_info_uncached(root: Path) -> dict:
    """解析 epub:返回 {title, opf_dir, sections:[Path](spine 顺序), toc:[{label, idx}]}。"""
    from bs4 import BeautifulSoup
    info = {"title": "", "opf_dir": root, "sections": [], "toc": []}
    try:
        cont = (root / "META-INF" / "container.xml").read_text("utf-8", "ignore")
        opf_rel = BeautifulSoup(cont, "xml").find("rootfile")["full-path"]
    except Exception:
        return info
    opf_path = (root / opf_rel)
    opf_dir = opf_path.parent
    info["opf_dir"] = opf_dir
    try:
        soup = BeautifulSoup(opf_path.read_text("utf-8", "ignore"), "xml")
    except Exception:
        return info
    t = soup.find("title")
    if t:
        info["title"] = t.get_text(" ", strip=True)
    manifest = {}
    for it in soup.find_all("item"):
        if it.get("id") and it.get("href"):
            manifest[it["id"]] = {"href": it["href"], "mt": (it.get("media-type") or "")}
    sections = []
    spine = soup.find("spine")
    if spine:
        for ir in spine.find_all("itemref"):
            it = manifest.get(ir.get("idref"))
            if it and it["href"].lower().endswith((".xhtml", ".html", ".htm")):
                sections.append((opf_dir / it["href"]).resolve())
    info["sections"] = sections
    idx_by_path = {str(p): i for i, p in enumerate(sections)}
    # TOC:nav(epub3)优先,其次 ncx(epub2);href 相对 nav 文件目录
    toc = []
    nav = next((manifest[m] for m in manifest if "nav" in (manifest[m]["mt"] or "")
                or manifest[m]["href"].lower().endswith("nav.xhtml")), None)
    if nav:
        try:
            navp = (opf_dir / nav["href"])
            nsoup = BeautifulSoup(navp.read_text("utf-8", "ignore"), "html.parser")
            navel = nsoup.find("nav") or nsoup
            for a in navel.find_all("a"):
                href = (a.get("href") or "").split("#")[0]
                label = a.get_text(" ", strip=True)
                if not href or not label:
                    continue
                tgt = str((navp.parent / href).resolve())
                if tgt in idx_by_path:
                    toc.append({"label": label[:80], "idx": idx_by_path[tgt]})
        except Exception:
            pass
    if not toc:
        ncx = next((manifest[m] for m in manifest if "ncx" in (manifest[m]["mt"] or "")
                    or manifest[m]["href"].lower().endswith(".ncx")), None)
        if ncx:
            try:
                ncxp = (opf_dir / ncx["href"])
                xs = BeautifulSoup(ncxp.read_text("utf-8", "ignore"), "xml")
                for np in xs.find_all("navPoint"):
                    lab = np.find("text"); src = np.find("content")
                    if not lab or not src or not src.get("src"):
                        continue
                    href = src["src"].split("#")[0]
                    tgt = str((ncxp.parent / href).resolve())
                    if tgt in idx_by_path:
                        toc.append({"label": lab.get_text(" ", strip=True)[:80], "idx": idx_by_path[tgt]})
            except Exception:
                pass
    info["toc"] = toc
    return info


_EPUB_ALLOWED = {"p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "img", "a", "br", "hr",
                 "ul", "ol", "li", "blockquote", "table", "thead", "tbody", "tr", "td", "th",
                 "caption", "em", "strong", "b", "i", "u", "s", "sup", "sub", "small", "figure",
                 "figcaption", "ruby", "rt", "rp", "section", "article", "dl", "dt", "dd",
                 "pre", "code", "mark", "cite", "q"}
_EPUB_DROP = {"script", "style", "link", "iframe", "object", "embed", "title", "head", "meta", "base", "form", "input", "button"}


def _epub_rewrite_url(url, sec_dir, root, sha):
    if not url or url.startswith(("http://", "https://", "data:", "/", "#", "mailto:")):
        return url
    try:
        target = (sec_dir / url.split("#")[0]).resolve()
        relp = target.relative_to(root.resolve()).as_posix()
        return f"/pdf/epub/file/{sha}/{relp}"
    except Exception:
        return url


def _sanitize_epub_section(section_path: Path, root: Path, sha: str) -> str:
    """章节 XHTML → 安全 HTML(剥脚本/书 CSS/所有 class,套我们自己的主题);图 src 改服务 URL。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(section_path.read_text("utf-8", "ignore"), "html.parser")
    body = soup.find("body") or soup
    sec_dir = section_path.parent
    # svg>image(封面/整页图常用)→ 转普通 img(svg 随后 unwrap,img 留下);避开 svg 命名空间/大小写麻烦
    for im in body.find_all("image"):
        src = im.get("xlink:href") or im.get("href") or ""
        ni = soup.new_tag("img")
        ni["src"] = _epub_rewrite_url(src, sec_dir, root, sha)
        im.replace_with(ni)
    # 先删危险/无用标签(含内容)
    for tag in body.find_all(_EPUB_DROP):
        tag.decompose()
    # 再处理剩余:剥属性、改 url、未知标签拆壳保留文字
    for tag in list(body.find_all(True)):
        if not tag.name or tag.parent is None:
            continue   # 已被上一步祖先 decompose/unwrap 带走
        name = tag.name.lower()
        if name not in _EPUB_ALLOWED:
            tag.unwrap(); continue
        # 保留 class(书靠 class 定排版:标题/加粗/行内 vs 块级图/居中)→ 配合 scoped 书 CSS 还原忠实排版
        keep = {"class"}
        if name == "img":
            keep |= {"src", "alt"}
        elif name == "a":
            keep |= {"href"}
        elif name in ("td", "th"):
            keep |= {"colspan", "rowspan"}
        for attr in list(tag.attrs):
            if attr not in keep:
                del tag[attr]
        if name == "img" and tag.get("src"):
            tag["src"] = _epub_rewrite_url(tag["src"], sec_dir, root, sha)
            tag["loading"] = "lazy"
        if name == "a":
            href = tag.get("href") or ""
            if not (href.startswith("#") or href.startswith(("http://", "https://"))):
                del tag["href"]   # 站内跨章链接暂时拆掉(防跳出阅读器),保留文字
    return body.decode_contents()


_EPUB_SEC_CACHE_DIR = CLAUDE_DIR / "state" / "epub-section-cache"   # 章节消毒 HTML 磁盘缓存:避免每次现 BeautifulSoup 解析吃满 GIL → 堵住用户操作请求


def _epub_section_cached(secs, idx: int, root: Path, sha: str) -> str:
    """消毒章节 HTML,落磁盘缓存(按 sha/idx/源文件 mtime)。命中则秒回不吃 GIL。"""
    sp = secs[idx]
    cache = _EPUB_SEC_CACHE_DIR / sha / (str(idx) + ".html")
    try:
        if cache.is_file() and cache.stat().st_mtime >= sp.stat().st_mtime:
            return cache.read_text("utf-8")
    except Exception:
        pass
    html = _sanitize_epub_section(sp, root, sha)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(html, "utf-8")
    except Exception:
        pass
    return html


def _scope_css(css: str) -> str:
    """把书的 CSS 每条规则 scope 到 #ep-col(不污染 app UI);丢 @page/@font-face/@import 等 @ 规则。
    calibre 生成的 CSS 选择器简单(.calibre_xx / p.calibre),足够用这个轻量解析。"""
    import re
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    i, n = 0, len(css)
    while i < n:
        brace = css.find("{", i)
        if brace < 0:
            break
        sel = css[i:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        block = css[brace + 1:j - 1].strip()
        i = j
        if not sel or sel.startswith("@"):
            continue   # @page(屏幕无用)/@font-face(用系统字体)/@media 等一律丢
        parts = []
        for s in (x.strip() for x in sel.split(",")):
            if not s:
                continue
            if s in ("body", "html", "*"):
                parts.append("#ep-col")
            elif s.startswith(("body ", "html ")):
                parts.append("#ep-col " + s.split(" ", 1)[1])
            else:
                parts.append("#ep-col " + s)
        if parts and block:
            out.append(", ".join(parts) + "{" + block + "}")
    return "\n".join(out)


@bp.route("/api/epub-css")
def pdf_api_epub_css():
    """书自带 CSS,scope 到 #ep-col 后返回(还原忠实排版,又不污染 app UI)。"""
    from flask import Response
    rel = (request.args.get("file") or "").strip()
    abs_path = _resolve_epub_book(rel)   # 收藏夹物化 EPUB 也可查(state 里那本;fav.css 由此 scope 出去)
    if not abs_path or abs_path.suffix.lower() != ".epub":
        return Response("", mimetype="text/css")
    root = _ensure_epub_extracted(abs_path, rel)
    if not root:
        return Response("", mimetype="text/css")
    chunks = []
    for cssf in sorted(root.rglob("*.css")):
        try:
            chunks.append(_scope_css(cssf.read_text("utf-8", "ignore")))
        except Exception:
            continue
    resp = Response("\n".join(chunks), mimetype="text/css")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


_EPUB_IMGDESC_DIR = CLAUDE_DIR / "state" / "epub-imgdesc"


@bp.route("/api/epub-img-describe", methods=["POST"])
def pdf_api_epub_img_describe():
    """点图 → AI 看图描述(物理示意图/公式排版等)。按图内容 sha1 缓存(同图只描述一次,跨书复用)。"""
    import hashlib, base64, mimetypes
    body = request.get_json(silent=True) or {}
    sha = (body.get("sha") or "").strip()
    sub = (body.get("path") or "").strip().lstrip("/")
    if not sha.isalnum() or not sub or ".." in sub.split("/"):
        return jsonify({"ok": False, "error": "bad"}), 400
    root = (_EPUB_EXTRACT_DIR / sha).resolve()
    target = (root / sub).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return jsonify({"ok": False, "error": "越界"}), 404
    if not target.is_file():
        return jsonify({"ok": False, "error": "无此图"}), 404
    raw = target.read_bytes()
    ih = hashlib.sha1(raw).hexdigest()[:16]
    cache = _EPUB_IMGDESC_DIR / f"{ih}.txt"
    if cache.exists():
        try:
            return jsonify({"ok": True, "desc": cache.read_text("utf-8"), "cached": True})
        except Exception:
            pass
    caption = (body.get("caption") or "").strip()[:200]
    context = (body.get("context") or "").strip()[:2000]
    mt = mimetypes.guess_type(str(target))[0] or "image/jpeg"
    A = _assistant()
    prompt = (
        (f"【图注】{caption}\n\n" if caption else "")
        + (f"【这段正文(帮你理解上下文)】{context}\n\n" if context else "")
        + "**结合上下文**用中文描述这张图:它是什么图、关键要素/结构/数值、在讲什么概念,2~4 句,抓重点别冗长。数学用 $...$。"
    )
    desc = A.reader_vision(
        [{"media_type": mt, "b64": base64.b64encode(raw).decode("ascii")}],
        prompt, action="vision", uid=_reader_uid(), system="")
    desc = (desc or "").strip()
    if desc:
        try:
            _EPUB_IMGDESC_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(desc, "utf-8")
        except Exception:
            pass
    return jsonify({"ok": True, "desc": desc})


@bp.route("/api/epub-manifest")
def pdf_api_epub_manifest():
    """EPUB 结构清单(给统一 HTML 阅读器)。?file= → {ok, title, count, toc:[{label,idx}]}。"""
    rel = (request.args.get("file") or "").strip()
    abs_path = _resolve_epub_book(rel)   # 收藏夹物化 EPUB 也可列清单(state 里那本)
    if not abs_path or abs_path.suffix.lower() != ".epub":
        return jsonify({"ok": False, "error": "bad file"}), 400
    root = _ensure_epub_extracted(abs_path, rel)
    if not root:
        return jsonify({"ok": False, "error": "解包失败"}), 500
    info = _epub_opf_info(root)
    return jsonify({"ok": True, "title": info["title"], "count": len(info["sections"]),
                    "toc": info["toc"], "sha": _epub_sha(rel)})


@bp.route("/api/epub-section")
def pdf_api_epub_section():
    """某个 spine 章节的消毒 HTML。?file=&idx=N → {ok, html, idx}。"""
    rel = (request.args.get("file") or "").strip()
    try:
        idx = int(request.args.get("idx") or "0")
    except Exception:
        idx = 0
    abs_path = _resolve_epub_book(rel)   # 收藏夹物化 EPUB 也可取章节(state 里那本)
    if not abs_path or abs_path.suffix.lower() != ".epub":
        return jsonify({"ok": False, "error": "bad file"}), 400
    root = _ensure_epub_extracted(abs_path, rel)
    if not root:
        return jsonify({"ok": False, "error": "解包失败"}), 500
    info = _epub_opf_info(root)
    secs = info["sections"]
    if idx < 0 or idx >= len(secs):
        return jsonify({"ok": False, "error": "idx 越界"}), 400
    _fav_sha = _epub_sha(rel)
    try:
        # 收藏夹物化 EPUB 的章节由服务端亲手生成(可信),**不再消毒**:保留 PDF 页透明词层的行内定位
        # style 与「打开原书」站内链接(_sanitize_epub_section 会剥掉两者)→ 选词/跳原书都可用。
        if rel.lstrip("/").startswith(_FAV_BOOK_PREFIX):
            html = _fav_epub_raw_section(secs[idx], root, _fav_sha)
        else:
            html = _epub_section_cached(secs, idx, root, _fav_sha)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"消毒失败：{ex}"}), 500
    return jsonify({"ok": True, "html": html, "idx": idx})


_EPUB_HTML_DIMS_CACHE: dict = {}   # {str(path): (mtime, processed_html)} 给章节 HTML 的 <img> 注入真实尺寸(防图片加载回流→连续模式抽搐)


def _epub_inject_img_dims(html: str, html_file: Path, root: Path) -> str:
    """给 HTML 里缺 width/height 的 <img> 补上图片真实像素尺寸(从解包目录读图)。
    浏览器据 width/height 算 aspect-ratio 预留空间(即便 CSS height:auto),图片加载前就占好位,
    连续滚动模式下不再每张图加载完就重排重渲(根治『左侧文本抽搐』)。"""
    try:
        from PIL import Image
    except Exception:
        return html
    base = html_file.parent

    def _repl(m):
        tag = m.group(0)
        if re.search(r"\bwidth\s*=", tag, re.I) and re.search(r"\bheight\s*=", tag, re.I):
            return tag
        sm = re.search(r"""src\s*=\s*["']([^"']+)["']""", tag, re.I)
        if not sm:
            return tag
        src = sm.group(1).split("#")[0].split("?")[0]
        if src.startswith("data:") or src.lower().endswith((".svg",)):
            return tag
        try:
            from urllib.parse import unquote
            ip = (base / unquote(src)).resolve()
            ip.relative_to(root)
            with Image.open(ip) as im:
                w, h = im.size
            if w and h:
                return tag[:4] + ' width="%d" height="%d"' % (int(w), int(h)) + tag[4:]
        except Exception:
            pass
        return tag

    try:
        return re.sub(r"<img\b[^>]*>", _repl, html, flags=re.I)
    except Exception:
        return html


_EPUB_IMG_CACHE_DIR = CLAUDE_DIR / "state" / "epub-img-cache"   # 大图降采样缓存(减 iOS 解码内存,防大图书内存压力→Safari 重载标签页)


def _epub_downscale_img(target: Path, sha: str, subpath: str, max_w: int = 1100):
    """超 max_w 宽的图降采样到 max_w(保持格式与宽高比),缓存到磁盘。返回缓存路径或 None(不用缩/失败)。"""
    try:
        from PIL import Image
        ext = target.suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            return None
        cache = _EPUB_IMG_CACHE_DIR / sha / (subpath + ".w%d%s" % (max_w, ext))
        if cache.is_file() and cache.stat().st_mtime >= target.stat().st_mtime:
            return cache
        with Image.open(target) as im:
            if im.width <= max_w:
                return None
            r = max_w / float(im.width)
            im2 = im.resize((max_w, max(1, int(im.height * r))), Image.LANCZOS)
            cache.parent.mkdir(parents=True, exist_ok=True)
            if ext in (".jpg", ".jpeg"):
                im2.convert("RGB").save(cache, "JPEG", quality=84)
            elif ext == ".png":
                im2.save(cache, "PNG", optimize=True)
            else:
                im2.save(cache, "WEBP", quality=84)
        return cache
    except Exception:
        return None


@bp.route("/epub/file/<sha>/<path:subpath>")
def epub_file(sha, subpath):
    """服务 epub 解包目录里的文件(章节 XHTML / 图片 / CSS / 字体),供 epub.js 懒加载。"""
    if not sha.isalnum():
        abort(404)
    root = (_EPUB_EXTRACT_DIR / sha).resolve()
    if not root.is_dir():
        abort(404)
    target = (root / subpath).resolve()
    try:
        target.relative_to(root)   # 防越界
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    from flask import send_file
    import mimetypes
    mt = mimetypes.guess_type(str(target))[0]
    # epub 章节常见后缀
    if target.suffix.lower() in (".xhtml", ".html", ".htm"):
        mt = "application/xhtml+xml"
        # 给章节 HTML 的 <img> 注入真实尺寸(缓存按 mtime)→ 防图片加载回流抽搐
        try:
            mtime = target.stat().st_mtime
            ck = str(target)
            cached = _EPUB_HTML_DIMS_CACHE.get(ck)
            if cached and cached[0] == mtime:
                processed = cached[1]
            else:
                raw = target.read_text("utf-8", errors="replace")
                processed = _epub_inject_img_dims(raw, target, root)
                if len(_EPUB_HTML_DIMS_CACHE) > 4000:
                    _EPUB_HTML_DIMS_CACHE.clear()
                _EPUB_HTML_DIMS_CACHE[ck] = (mtime, processed)
            resp = Response(processed, mimetype=mt)
            resp.headers["Cache-Control"] = "no-cache"   # 章节 HTML 每次重新取(注入的 img 尺寸要即时生效;服务端已缓存处理结果,快)
            return resp
        except Exception:
            pass   # 出错回退原始 send_file
    elif target.suffix.lower() == ".opf":
        mt = "application/oebps-package+xml"
    elif target.suffix.lower() == ".ncx":
        mt = "application/x-dtbncx+xml"
    elif target.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        _ds = _epub_downscale_img(target, sha, subpath)   # 大图降采样省内存
        if _ds is not None:
            resp = send_file(str(_ds), mimetype=mt or "image/jpeg", conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    resp = send_file(str(target), mimetype=mt or "application/octet-stream", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=86400"   # 解包文件内容稳定,可缓存
    return resp


_EPUB_HL_DIR = CLAUDE_DIR / "state" / "epub-highlights"


def _epub_hl_path(rel: str) -> Path:
    import hashlib
    return _EPUB_HL_DIR / (hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16] + ".json")


def _epub_hl_load(rel: str) -> list:
    try:
        return json.loads(_epub_hl_path(rel).read_text("utf-8"))
    except Exception:
        return []


def _epub_hl_save(rel: str, items: list):
    _EPUB_HL_DIR.mkdir(parents=True, exist_ok=True)
    _epub_hl_path(rel).write_text(json.dumps(items, ensure_ascii=False), "utf-8")


@bp.route("/api/epub-highlights", methods=["GET", "POST", "PATCH", "DELETE"])
def pdf_api_epub_highlights():
    """EPUB 高亮 CRUD(CFI 文本锚,独立 sidecar:state/epub-highlights/<sha>.json)。
    GET ?file= → 列;POST {file,cfi,text,color,note,body?,kind?} → 建;PATCH {file,id,color?,note?,body?,kind?} → 改;DELETE ?file=&id= → 删。
    body=AI 译文/解释正文(高亮带的整段内容),kind=类型标签(译文/解释/笔记);跟 PDF 高亮的 sentence/body/note 四字段对齐。"""
    if request.method == "GET":
        rel = (request.args.get("file") or "").strip()
        return jsonify({"ok": True, "highlights": _epub_hl_load(rel)})
    if request.method == "DELETE":
        rel = (request.args.get("file") or "").strip()
        hid = (request.args.get("id") or "").strip()
        items = [h for h in _epub_hl_load(rel) if h.get("id") != hid]
        _epub_hl_save(rel, items)
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    if not rel:
        return jsonify({"ok": False, "error": "缺少 file"}), 400
    items = _epub_hl_load(rel)
    if request.method == "POST":
        cfi = (body.get("cfi") or "").strip()
        anchor = body.get("anchor")   # 新 HTML 阅读器用偏移锚 {section,start,end};老 epub.js 版用 cfi
        if not cfi and not anchor:
            return jsonify({"ok": False, "error": "缺少 cfi/anchor"}), 400
        import uuid as _u
        h = {"id": "e" + _u.uuid4().hex[:11], "cfi": cfi, "anchor": anchor,
             "text": (body.get("text") or "")[:2000], "color": (body.get("color") or "#ffd54a"),
             "note": (body.get("note") or "")[:2000],
             "sentence": (body.get("sentence") or "")[:2000],   # epub.js 版高亮存所在句(编辑浮层只读预览;HTML 版不传则空)
             "body": (body.get("body") or "")[:8000],   # AI 译文/解释正文(高亮带的整段内容;preview 渲)
             "kind": (body.get("kind") or "")[:32],     # 类型标签:译文/解释/笔记
             "time": int(__import__("time").time())}
        items.append(h); _epub_hl_save(rel, items)
        return jsonify({"ok": True, "id": h["id"], "highlight": h})
    # PATCH
    hid = (body.get("id") or "").strip()
    h = next((x for x in items if x.get("id") == hid), None)
    if not h:
        return jsonify({"ok": False, "error": "未找到"}), 404
    if "color" in body:
        # 允许置空("" → no-color 虚框模式:保留备注/原文但不上色,前端渲成虚线框)
        h["color"] = (body.get("color") or "")
    if "note" in body:
        h["note"] = (body.get("note") or "")[:2000]
    if "sentence" in body:
        h["sentence"] = (body.get("sentence") or "")[:2000]
    if "body" in body:
        h["body"] = (body.get("body") or "")[:8000]
    if "kind" in body:
        h["kind"] = (body.get("kind") or "")[:32]
    _epub_hl_save(rel, items)
    return jsonify({"ok": True, "highlight": h})


# ── 便签(sticky notes)sidecar:PDF/EPUB 共用一套路由,anchor 不透明存储(设计见 references/sticky-notes-design.md)。
# anchor = {"kind":"pdf","page":N,"x":0-1,"y":0-1} | {"kind":"epub","section":N,"x":0-1,"y":0-1}(挂进内容容器随滚动)。
# strokes = [{c,w,pts:[[x,y],...]}](归一化到便签 body 宽高;只能前端笔擦改,AI 工具不许动)。
_NOTES_DIR = CLAUDE_DIR / "state" / "reader-notes"


def _notes_path(rel: str) -> Path:
    import hashlib
    return _NOTES_DIR / (hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16] + ".json")


def _notes_load(rel: str) -> list:
    try:
        return json.loads(_notes_path(rel).read_text("utf-8"))
    except Exception:
        return []


def _notes_save(rel: str, items: list):
    _NOTES_DIR.mkdir(parents=True, exist_ok=True)
    _notes_path(rel).write_text(json.dumps(items, ensure_ascii=False), "utf-8")


@bp.route("/api/notes", methods=["GET", "POST", "PATCH", "DELETE"])
def pdf_api_notes():
    """便签 CRUD(sidecar:state/reader-notes/<sha>.json,PDF/EPUB 同一套)。
    GET ?file= → 列;POST {file,anchor,text?,color?,w?,h?,collapsed?,strokes?} → 建;
    PATCH {file,id,anchor?/text?/color?/w?/h?/collapsed?/strokes?} → 改(字段级合并);DELETE ?file=&id= → 删。"""
    if request.method == "GET":
        rel = (request.args.get("file") or "").strip()
        return jsonify({"ok": True, "notes": _notes_load(rel)})
    if request.method == "DELETE":
        rel = (request.args.get("file") or "").strip()
        nid = (request.args.get("id") or "").strip()
        items = [n for n in _notes_load(rel) if n.get("id") != nid]
        _notes_save(rel, items)
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    if not rel:
        return jsonify({"ok": False, "error": "缺少 file"}), 400
    items = _notes_load(rel)
    now = int(__import__("time").time())
    if request.method == "POST":
        anchor = body.get("anchor")
        if not isinstance(anchor, dict) or anchor.get("kind") not in ("pdf", "epub"):
            return jsonify({"ok": False, "error": "缺少/非法 anchor"}), 400
        import uuid as _u
        n = {"id": "n" + _u.uuid4().hex[:11], "anchor": anchor,
             "text": (body.get("text") or "")[:8000],
             "color": (body.get("color") or "#fff8c5"),
             "w": int(body.get("w") or 260), "h": int(body.get("h") or 180),
             "collapsed": bool(body.get("collapsed", False)),
             "strokes": body.get("strokes") if isinstance(body.get("strokes"), list) else [],
             "created": now, "updated": now}
        items.append(n); _notes_save(rel, items)
        return jsonify({"ok": True, "id": n["id"], "note": n})
    # PATCH:字段级合并(anchor 移动/text 编辑/颜色/尺寸/折叠态/笔画 各自独立更新)
    nid = (body.get("id") or "").strip()
    n = next((x for x in items if x.get("id") == nid), None)
    if not n:
        return jsonify({"ok": False, "error": "未找到"}), 404
    if isinstance(body.get("anchor"), dict) and body["anchor"].get("kind") in ("pdf", "epub"):
        n["anchor"] = body["anchor"]
    if "text" in body:
        n["text"] = (body.get("text") or "")[:8000]
    if "color" in body:
        n["color"] = (body.get("color") or "#fff8c5")
    if "w" in body:
        n["w"] = int(body.get("w") or 260)
    if "h" in body:
        n["h"] = int(body.get("h") or 180)
    if "collapsed" in body:
        n["collapsed"] = bool(body.get("collapsed"))
    if isinstance(body.get("strokes"), list):
        n["strokes"] = body["strokes"]
    n["updated"] = now
    _notes_save(rel, items)
    return jsonify({"ok": True, "note": n})


def _note_composite_png(rel: str, nid: str):
    """便签合成图 PNG bytes(有手写笔画时把 文字+笔画 整体画成一张图;PIL 重绘:便签色底+文字排版+笔画)。
    /api/note-composite 路由与 assistant/epub_assistant 的 see_figure(kind:'note')共用。找不到便签 → None。"""
    n = next((x for x in _notes_load(rel) if x.get("id") == nid), None)
    if not n:
        return None
    from PIL import Image, ImageDraw, ImageFont
    import io
    scale = 2   # 2x 渲染,AI 看得清小字
    w, h = max(120, int(n.get("w") or 260)) * scale, max(80, int(n.get("h") or 180)) * scale
    img = Image.new("RGB", (w, h), n.get("color") or "#fff8c5")
    dr = ImageDraw.Draw(img)
    font = None
    for fp in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(fp, 15 * scale); break
        except Exception:
            continue
    # 文字:简单折行排版(便签内容短,逐字宽度累加折行足够)
    text = n.get("text") or ""
    if text:
        x0, y0, line, yy = 10 * scale, 8 * scale, "", 8 * scale
        maxw = w - 20 * scale
        lh = int(15 * scale * 1.5)
        for ch in text:
            if ch == "\n" or (font and dr.textlength(line + ch, font=font) > maxw):
                dr.text((x0, yy), line, fill="#1b1b1b", font=font)
                yy += lh; line = "" if ch == "\n" else ch
                if yy > h - lh:
                    break
            else:
                line += ch
        if line and yy <= h - lh:
            dr.text((x0, yy), line, fill="#1b1b1b", font=font)
    # 笔画:归一化坐标 × 便签尺寸
    for s in (n.get("strokes") or []):
        pts = [(float(p[0]) * w, float(p[1]) * h) for p in (s.get("pts") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(pts) >= 2:
            dr.line(pts, fill=s.get("c") or "#e33", width=max(1, int(float(s.get("w") or 2) * scale)), joint="curve")
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


@bp.route("/api/note-composite", methods=["POST"])
def pdf_api_note_composite():
    """便签合成图(双击注入 AI 用)。body: {file, id} → {ok, data_url}。
    服务端 PIL 重绘(_note_composite_png),避免前端 html2canvas 依赖。"""
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    nid = (body.get("id") or "").strip()
    try:
        png = _note_composite_png(rel, nid)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"合成失败: {ex}"}), 500
    if png is None:
        return jsonify({"ok": False, "error": "未找到便签"}), 404
    import base64
    return jsonify({"ok": True, "data_url": "data:image/png;base64," + base64.b64encode(png).decode()})


# ── 插入页(用户页)sidecar:state/reader-userpages/<sha>.json(按书;设计:references/reader-userpages-favorites.md「一、插入页设计」阶段A)──
# 锚定铁律:用户页 id=u_<8hex>(独立编号空间),插入位置只存 {after:N}——N=原书 PDF 页(1-based)/EPUB 章序(1-based,=idx+1);
# 0=书首。**绝不挤占原书 page/section 编号**:前端把用户页渲成原书页/章元素的兄弟节点,原书已有高亮/便签/墨迹锚零影响。
_UPAGES_DIR = CLAUDE_DIR / "state" / "reader-userpages"


def _upages_path(rel: str) -> Path:
    import hashlib
    return _UPAGES_DIR / (hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16] + ".json")


def _upages_load(rel: str) -> list:
    try:
        items = json.loads(_upages_path(rel).read_text("utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _upages_save(rel: str, items: list):
    _UPAGES_DIR.mkdir(parents=True, exist_ok=True)
    p = _upages_path(rel)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), "utf-8")
    tmp.replace(p)


def _upages_sorted(items: list) -> list:
    return sorted(items, key=lambda x: (int(x.get("after") or 0), int(x.get("created") or 0), str(x.get("id") or "")))


@bp.route("/api/userpages", methods=["GET", "POST", "PATCH", "DELETE"])
def pdf_api_userpages():
    """用户页 CRUD(sidecar:state/reader-userpages/<sha>.json,PDF/EPUB 同一套,照 notes 模式)。
    GET ?file= → {ok, pages}(按 after,created 排序);POST {file, after, title?, md?} → 建(id=u_<8hex>);
    PATCH {file, id, title?/md?/after?} → 字段级合并;DELETE ?file=&id= → 删。"""
    if request.method == "GET":
        rel = (request.args.get("file") or "").strip()
        r = jsonify({"ok": True, "pages": _upages_sorted(_upages_load(rel))})
        r.headers["Cache-Control"] = "no-store"
        return r
    if request.method == "DELETE":
        rel = (request.args.get("file") or "").strip()
        uid = (request.args.get("id") or "").strip()
        with _upages_lock(rel):   # 与后台同步 job 的迁移事务互斥(blocker③)
            items = _upages_load(rel)
            hit = next((x for x in items if x.get("id") == uid), None)
            if hit and isinstance(hit.get("page"), int):   # 真插入页:必须走 /api/pdf-insert-page(要改 PDF+迁锚)
                return jsonify({"ok": False, "error": "真实插入页请用 /api/pdf-insert-page 删除"}), 400
            _upages_save(rel, [x for x in items if x.get("id") != uid])
        try:
            _fav_cascade_userpage_delete(rel, uid)   # 同一张纸:页没了 → 各收藏夹里指向它的条目一并移除(不留墓碑)+ 后台重建 + 推事件
        except Exception:
            pass
        try:
            _reader_publish("userpage-del", rel, uid)   # 推给所有打开着的视图(原书/收藏夹):当场移除该页元素
        except Exception:
            pass
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    if not rel:
        return jsonify({"ok": False, "error": "缺少 file"}), 400
    now = int(__import__("time").time())

    def _norm_after(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    _up_lk = _upages_lock(rel)   # POST/PATCH 全程持锁 RMW(与后台同步 job 迁移事务互斥,blocker③)
    _up_lk.acquire()
    try:
        items = _upages_load(rel)
        if request.method == "POST":
            import uuid as _u
            p = {"id": "u_" + _u.uuid4().hex[:8], "after": _norm_after(body.get("after")),
                 "title": (body.get("title") or "")[:120],
                 "md": (body.get("md") or "")[:100000],
                 "created": now, "updated": now}
            items.append(p)
            _upages_save(rel, items)
            return jsonify({"ok": True, "id": p["id"], "page": p})
        # PATCH:字段级合并(title/md/after 各自独立更新)
        uid = (body.get("id") or "").strip()
        p = next((x for x in items if x.get("id") == uid), None)
        if not p:
            return jsonify({"ok": False, "error": "未找到"}), 404
        if isinstance(p.get("page"), int):   # 真插入页
            if p.get("mode") == "overlay":   # v4 overlay:文字即时存 sidecar,不碰 PDF(设计 v4 §C;后台同步另走 edit job=批次2)
                changed = False
                if "title" in body:
                    p["title"] = (body.get("title") or "")[:120]; changed = True
                if "md" in body:
                    p["md"] = (body.get("md") or "")[:100000]; changed = True
                if changed:
                    p["md_ver"] = int(p.get("md_ver", 0)) + 1   # 脏标记版本戳:md_ver > synced_ver = 待写回 PDF
                p["updated"] = now
                _upages_save(rel, items)
                return jsonify({"ok": True, "page": p, "md_ver": int(p.get("md_ver", 0))})
            # baked 真实页(内容已烧进 PDF):改动仍须重排,走 /api/pdf-insert-page
            return jsonify({"ok": False, "error": "真实插入页请用 /api/pdf-insert-page 编辑"}), 400
        if "title" in body:
            p["title"] = (body.get("title") or "")[:120]
        if "md" in body:
            p["md"] = (body.get("md") or "")[:100000]
        if "after" in body:
            p["after"] = _norm_after(body.get("after"))
        if "h" in body:   # EPUB 插入页手动高度(px;下边缘拖动手柄,设计见 references「插入页高度」)
            try:
                hv = float(body.get("h"))
            except (TypeError, ValueError):
                hv = 0.0
            if hv > 0:
                p["h"] = int(max(60, min(30000, hv)))   # 服务端宽松 clamp(前端另有 120..300vh 交互 clamp)
            else:
                p.pop("h", None)   # h<=0 → 复位为默认(keepRatio 等比)
        p["updated"] = now
        _upages_save(rel, items)
        try:
            _reader_publish("text", rel, p.get("id"))   # 推「正文变了」给其它客户端(实时同步)
        except Exception:
            pass
        return jsonify({"ok": True, "page": p})
    finally:
        _up_lk.release()


# ═══════════ PDF 真插入页(规格 v2):PyMuPDF 修改 PDF 文件本身 + 锚迁移注册表 ═══════════
# 设计:references/reader-userpages-favorites.md「⚠️ 规格 v2」。用户明确要求:插入的页真正写进 PDF
# (页数改变,任何阅读器可见),错位由系统全部解决。EPUB 虚拟章节(.ep-usec)不受影响。
#
# 异步 job(复用 _JOBS/_job_set + /api/job-status 轮询,单 worker gunicorn 内存表够用):
#   磁盘守卫 → 备份(state/pdf-page-backups/<sha>/<ts>.pdf,留最近2份)→ PyMuPDF 改页 → tmp 同目录全量
#   save(小书 garbage=3 / 大书 garbage=1,照 embed_google_ocr_to_pdf 取舍)→ 重开断言页数 → journal →
#   os.replace 原子替换 → 锚迁移注册表(两阶段事务,见下)→ 用户页表记录 → done → 前端整页 reload。
#
# 安全红线:
#   · 原书永不损坏:替换前任何异常 = 删 tmp,原书分毫不动;迁移阶段任何异常 = 恢复 PDF 备份 + 回滚已写
#     sidecar(全成或全不成)。
#   · 事务实现:阶段1把所有 sidecar 读进内存、算好新内容(纯内存,零写盘);阶段2统一写回
#     (每个文件 tmp+replace 原子),任何一步失败 → 用留存的原始 bytes 逆序还原已写文件 + copy 备份恢复 PDF。
#   · journal(pdf-page-backups/<sha>/journal.json):写在 os.replace 之前、迁移全部完成后删。进程在
#     替换与迁移之间死掉 → journal 残留 → 后续操作一律 409 拒绝并提示从备份恢复(防带着错位继续写)。
#
# ⚠ 未来任何新增「按 PDF 页号锚定」的存储,必须在 PAGE_ANCHOR_MIGRATIONS 登记迁移器(设计文档铁律)。
import threading as _upthr

_PAGE_BACKUP_DIR = CLAUDE_DIR / "state" / "pdf-page-backups"
_INSPAGE_ACTIVE: set = set()          # 正在改页的 rel(单 worker,进程内互斥即可)
_INSPAGE_MUTEX = _upthr.Lock()

# 🔴 v4 批次2 BLOCKER 修法③:userpages sidecar 的所有「读改写」必须经一把 per-rel 锁串行化。
#   单 worker gunicorn 里,前端即时编辑 PATCH(/api/userpages)与后台同步 job 的锚迁移事务(_pam_userpages
#   phase1 读 → phase2 写)是**独立线程并发**;不加锁 → job 用旧内存快照 phase2 落盘会覆盖 PATCH 刚写的新编辑
#   = 静默丢字。锁范围:PATCH/POST/DELETE 的 RMW + job 的 collect_plans→apply_plans。per-rel(不同书不互斥)。
_UPAGES_LOCKS: dict = {}
_UPAGES_LOCKS_GUARD = _upthr.Lock()


def _upages_lock(rel: str):
    with _UPAGES_LOCKS_GUARD:
        lk = _UPAGES_LOCKS.get(rel)
        if lk is None:
            lk = _upthr.Lock()
            _UPAGES_LOCKS[rel] = lk
        return lk


def _up_journal_path(sha: str) -> Path:
    return _PAGE_BACKUP_DIR / sha / "journal.json"


# ── markdown → 简单 HTML(insert_htmlbox 用):标题/段落/列表/粗斜体/行内代码。
#    $..$ 公式不渲染、原文保留(阶段1降级,如实告知);全文先 HTML 转义再放行有限标记。
def _up_md_html(title: str, md: str) -> str:
    import html as _html
    def _inline(s: str) -> str:
        s = _html.escape(s, quote=False)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
        return s
    out, para, lmode = [], [], None   # lmode: None|'ul'|'ol'
    def _flush():
        nonlocal para
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para = []
    def _close():
        nonlocal lmode
        if lmode:
            out.append("</" + lmode + ">"); lmode = None
    if (title or "").strip():
        out.append("<h2>" + _inline(title.strip()) + "</h2>")
    for ln in (md or "").replace("\r\n", "\n").split("\n"):
        t = ln.strip()
        if not t:
            _flush(); _close(); continue
        m = re.match(r"^(#{1,4})\s+(.*)$", t)
        if m:
            _flush(); _close()
            lv = min(4, len(m.group(1)) + 1)   # '#'→h2(h1 留给页标题层级感)
            out.append("<h%d>%s</h%d>" % (lv, _inline(m.group(2)), lv)); continue
        m = re.match(r"^[-*+]\s+(.*)$", t)
        if m:
            _flush()
            if lmode != "ul": _close(); out.append("<ul>"); lmode = "ul"
            out.append("<li>" + _inline(m.group(1)) + "</li>"); continue
        m = re.match(r"^\d+[.)]\s+(.*)$", t)
        if m:
            _flush()
            if lmode != "ol": _close(); out.append("<ol>"); lmode = "ol"
            out.append("<li>" + _inline(m.group(1)) + "</li>"); continue
        para.append(t)
    _flush(); _close()
    return "".join(out) or "<p></p>"


_UP_PAGE_CSS = ("h2{font-size:16pt;margin:0 0 10pt}h3{font-size:13pt}h4{font-size:11.5pt}"
                "p,li{font-size:10.5pt;line-height:1.55}code{background:#eeeeee}"
                "*{font-family:sans-serif}")


def _up_render_page(page, title: str, md: str) -> list:
    """标题+markdown 排进新页(insert_htmlbox;CJK 走 MuPDF 内置 fallback 字体,文字可选中=字符层可用)。
    返回 warnings(内容放不下被整体缩小/截断时告知)。"""
    import fitz
    warns = []
    r = page.rect
    margin = max(24.0, min(r.width, r.height) * 0.06)
    box = fitz.Rect(r.x0 + margin, r.y0 + margin, r.x1 - margin, r.y1 - margin * 1.8)
    try:
        spare, scale = page.insert_htmlbox(box, _up_md_html(title, md), css=_UP_PAGE_CSS, scale_low=0.25)
        if scale < 0.999:
            warns.append("内容较多,排版整体缩小到 %d%% 以放进一页" % int(scale * 100))
        if spare < 0:
            warns.append("内容超长,已按最小缩放排版,末尾可能被截断")
    except Exception as ex:
        # 兜底:纯文本(china-s 内置 CJK 字体),不至于替换出一张空页
        try:
            page.insert_textbox(box, ((title or "") + "\n\n" + (md or ""))[:4000],
                                fontsize=10.5, fontname="china-s")
        except Exception:
            pass
        warns.append("富文本排版失败已降级纯文本:%s" % ex)
    try:
        fr = fitz.Rect(r.x0 + margin, r.y1 - margin * 1.4, r.x1 - margin, r.y1 - margin * 0.2)
        page.insert_htmlbox(fr, '<p style="text-align:center;color:#999999;font-size:8pt">— 用户插入页 —</p>')
    except Exception:
        pass
    return warns


# ── 页号映射(mv):插入=+1 / 删除=-1(被删页上的锚 → None=丢弃)/ 编辑=不动,同一套走全部迁移器 ──
def _up_mv_insert(after: int):
    return lambda p: (p + 1) if p > after else p


def _up_mv_delete(pno: int):
    return lambda p: None if p == pno else ((p - 1) if p > pno else p)


def _up_mv_keep():
    return lambda p: p


def _up_mv_droponly(pno: int):
    """编辑模式给「机器派生」存储用:被重写那页的派生数据(图框/公式/OCR校正)已过期 → 丢,其余不动。"""
    return lambda p: None if p == pno else p


def _up_json_plan(path: Path, mutate):
    """读 sidecar 原始 JSON(⚠ 不经业务 loader——_fig_load_abs/_ocrfix_load_abs 有 mtime 清空守卫,迁移必须
    绕过它拿到原始数据)→ mutate(data)->bool → 有改动出 write plan。不存在→无事;损坏→跳过+警告
    (业务 loader 对损坏文件本就按空处理,不因此中止整个事务)。返回 (plan|None, warn|None)。"""
    if not path.exists():
        return None, None
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "sidecar %s 损坏,跳过迁移(业务上视为空)" % path.name
    if not mutate(data):
        return None, None
    return ("write", path, json.dumps(data, ensure_ascii=False).encode("utf-8"), raw), None


def _up_shift_pagelist(arr, mv, key="page") -> tuple:
    """列表元素按 key 迁页号(int 才动;u_* 等字符串跳过);mv→None 的条目丢弃。返回 (新列表, changed)。"""
    out, changed = [], False
    for it in (arr or []):
        if not isinstance(it, dict):
            out.append(it); continue
        p = it.get(key)
        if isinstance(p, bool) or not isinstance(p, int):
            out.append(it); continue
        np = mv(p)
        if np is None:
            changed = True; continue
        if np != p:
            it[key] = np; changed = True
        out.append(it)
    return out, changed


# ── 迁移器们:fn(ctx) -> (plans, warns)。ctx = {rel, ap, sha, mv, mvd, new_mtime, mode, pivot, record_op}
#    mv=用户数据映射;mvd=机器派生数据映射(编辑模式丢被重写页);plans 见 _up_apply_plans。
def _pam_highlights(ctx):
    def mut(d):
        arr, ch = _up_shift_pagelist(d.get("highlights"), ctx["mv"])
        if ch: d["highlights"] = arr
        return ch
    plan, warn = _up_json_plan(_hl_path(ctx["rel"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_notes(ctx):
    def mut(d):
        if not isinstance(d, list):
            return False
        changed = False
        keep = []
        for n in d:
            a = (n or {}).get("anchor") or {}
            p = a.get("page")
            if a.get("kind") == "pdf" and isinstance(p, int) and not isinstance(p, bool):
                np = ctx["mv"](p)
                if np is None:
                    changed = True; continue
                if np != p:
                    a["page"] = np; changed = True
            keep.append(n)
        if changed:
            d[:] = keep
        return changed
    plan, warn = _up_json_plan(_notes_path(ctx["rel"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_ink(ctx):
    def mut(d):
        pages = d.get("pages")
        if not isinstance(pages, dict):
            return False
        out, changed = {}, False
        for k, v in pages.items():
            try:
                p = int(k)
            except (TypeError, ValueError):
                out[k] = v; continue
            np = ctx["mv"](p)
            if np is None:
                changed = True; continue
            if np != p:
                changed = True
            out[str(np)] = v
        if changed:
            d["pages"] = out
        return changed
    plan, warn = _up_json_plan(_ink_path(ctx["rel"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_favorites(ctx):
    def mut(d):
        changed = False
        for f in (d.get("folders") or []):
            items, keep = f.get("items") or [], []
            for it in items:
                if (it or {}).get("file") == ctx["rel"] and it.get("kind") == "pdf" \
                        and isinstance(it.get("page"), int) and not isinstance(it.get("page"), bool):
                    np = ctx["mv"](it["page"])
                    if np is None:
                        changed = True; continue
                    if np != it["page"]:
                        it["page"] = np; changed = True
                keep.append(it)
            if len(keep) != len(items):
                f["items"] = keep
        return changed
    plan, warn = _up_json_plan(_FAV_FILE, mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_userpages(ctx):
    """用户页表自身:真实页记录(page)按 mv 迁;旧虚拟页(after=边界语义)插入>pivot→+1、删除>=pivot→-1;
    最后应用本次 record_op(add/update/remove)。此表必写(record_op 总有事做)。"""
    path = _upages_path(ctx["rel"])
    raw, items = None, []
    if path.exists():
        try:
            raw = path.read_bytes()
            items = json.loads(raw.decode("utf-8"))
            if not isinstance(items, list):
                items = []
        except Exception:
            items, raw = [], None
    mode, pivot = ctx["mode"], ctx["pivot"]
    out = []
    for p in items:
        if isinstance(p.get("page"), int):          # 真实插入页记录
            np = ctx["mv"](p["page"])
            if np is None:                          # 只可能是 delete 目标页自身;它由 record_op remove 处理,防御性跳过
                out.append(p); continue
            p["page"] = np
        else:                                        # 旧虚拟页:after 是"页边界"(0=书首)
            a = int(p.get("after") or 0)
            if mode == "insert" and a > pivot:
                p["after"] = a + 1
            elif mode == "delete" and a >= pivot:
                p["after"] = max(0, a - 1)
        out.append(p)
    op = ctx["record_op"]
    now = int(_time.time())
    if op["op"] == "add":
        rec = {"id": op["id"], "page": op["page"], "title": op.get("title") or "",
               "md": op.get("md") or "", "real": True, "created": now, "updated": now}
        if op.get("mode") == "overlay":   # v4:空白真页 + sidecar 文字(md 即时编辑,后台同步回 PDF;设计 v4 §A/§B)
            rec["mode"] = "overlay"; rec["md_ver"] = 0; rec["synced_ver"] = 0
        out.append(rec)
    elif op["op"] == "update":
        for p in out:
            if p.get("id") == op["id"]:
                if p.get("mode") == "overlay":
                    # 🔴 BLOCKER①:overlay 的 md 真源在 sidecar,后台同步 job 绝不回写 md/title
                    #   (否则用触发同步时的旧快照覆盖掉同步期间的新编辑 = 静默丢字)。这里 out 是**本次
                    #   刚从磁盘读回的最新 sidecar**(含同步期间用户新写的 md),原样保留。只把 synced_ver
                    #   抬到本次写进 PDF 的 base_ver(单调 max);base_ver < 当前 md_ver 则仍脏、下次再同步。
                    bv = op.get("base_ver")
                    if bv is not None:
                        p["synced_ver"] = max(int(p.get("synced_ver", 0)), int(bv))
                    p["updated"] = now
                else:
                    p["title"] = op.get("title") or ""
                    p["md"] = op.get("md") or ""
                    p["updated"] = now
    elif op["op"] == "remove":
        out = [p for p in out if p.get("id") != op["id"]]
    return [("write", path, json.dumps(out, ensure_ascii=False).encode("utf-8"), raw)], []


def _pam_tr_sentences(ctx):
    def mut(d):
        arr, ch = _up_shift_pagelist(d.get("sentences"), ctx["mv"])
        if ch: d["sentences"] = arr
        return ch
    plan, warn = _up_json_plan(_tr_path(ctx["rel"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_char_offset(ctx):
    def mut(d):
        m = d.get(ctx["rel"])
        if not isinstance(m, dict):
            return False
        out, changed = {}, False
        for k, v in m.items():
            try:
                p = int(k)
            except (TypeError, ValueError):
                out[k] = v; continue
            np = ctx["mv"](p)
            if np is None:
                changed = True; continue
            if np != p:
                changed = True
            out[str(np)] = v
        if changed:
            d[ctx["rel"]] = out
        return changed
    plan, warn = _up_json_plan(_CHAR_OFFSET_PATH, mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_toc(ctx):
    """自定义目录:entries[].page 是**印刷页**(物理插页不动它);range{start,end} 是 PDF 页 → 迁。
    原生书签(PDF outline)指向页对象引用,PyMuPDF 插/删页自动跟随,无需处理。"""
    def mut(d):
        rng = d.get("range")
        if not isinstance(rng, dict):
            return False
        changed = False
        for k in ("start", "end"):
            v = rng.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                nv = ctx["mv"](v)
                if nv is None:
                    nv = max(1, v - 1)   # 目录页本身被删(不太可能):范围端点退一格
                if nv != v:
                    rng[k] = nv; changed = True
        return changed
    plan, warn = _up_json_plan(_toc_path_abs(ctx["ap"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_sentence_cards(ctx):
    """自动例句卡登记表(全局):key=sha1(rel|page|text)[:16] 含页号 → 重键,值里的 page 同步迁
    (不迁的话页移位后同句生成新 key → 重复建 Anki 卡)。"""
    import hashlib
    path = CLAUDE_DIR / "state" / "sentence-cards.json"
    def mut(d):
        if not isinstance(d, dict):
            return False
        changed = False
        out = {}
        for k, v in d.items():
            if isinstance(v, dict) and v.get("file") == ctx["rel"] \
                    and isinstance(v.get("page"), int) and not isinstance(v.get("page"), bool):
                np = ctx["mv"](v["page"])
                if np is None:
                    changed = True; continue
                if np != v["page"]:
                    v["page"] = np
                    k = hashlib.sha1(("%s|%d|%s" % (ctx["rel"], np, v.get("text") or "")).encode("utf-8")).hexdigest()[:16]
                    changed = True
            out[k] = v
        if changed:
            d.clear(); d.update(out)
        return changed
    plan, warn = _up_json_plan(path, mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_vocab_exposure(ctx):
    """生词暴露反向索引(全局,scripts/vocab/build_exposure.py 产物,可重建;迁移保正确性)。"""
    path = CLAUDE_DIR / "state" / "vocab-exposure.json"
    def mut(d):
        if not isinstance(d, dict):
            return False
        changed = False
        for lemma, info in d.items():
            if not isinstance(info, dict):
                continue
            pages = info.get("pages")
            if not isinstance(pages, list):
                continue
            keep, ch = [], False
            for e in pages:
                if isinstance(e, dict) and e.get("pdf") == ctx["rel"] \
                        and isinstance(e.get("page"), int) and not isinstance(e.get("page"), bool):
                    np = ctx["mv"](e["page"])
                    if np is None:
                        ch = True; continue
                    if np != e["page"]:
                        e["page"] = np; ch = True
                keep.append(e)
            if ch:
                info["pages"] = keep
                if isinstance(info.get("total_pages"), int):
                    info["total_pages"] = len(keep)
                changed = True
        return changed
    plan, warn = _up_json_plan(path, mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_figures(ctx):
    """pdf-figures/<book_sha>.json(figures/figures_geom/formulas 各带 page + _none_pages)——机器派生,
    按 mvd 迁 + **book_mtime 刷成新值**:_fig_load_abs 的 mtime 守卫本会在书 mtime 变化时清空 figures,
    但我们精确知道这次只是页号移位 → 迁移页号 + 刷 mtime,贵的 AI 图描述/YOLO 框/公式 latex 全保留。"""
    def mut(d):
        changed = False
        for key in ("figures", "figures_geom", "formulas"):
            arr, ch = _up_shift_pagelist(d.get(key), ctx["mvd"])
            if ch:
                d[key] = arr; changed = True
        np_list, ch2 = [], False
        for p in (d.get("_none_pages") or []):
            if isinstance(p, int) and not isinstance(p, bool):
                np = ctx["mvd"](p)
                if np is None:
                    ch2 = True; continue
                if np != p:
                    ch2 = True
                np_list.append(np)
            else:
                np_list.append(p)
        if ch2:
            d["_none_pages"] = np_list; changed = True
        if d.get("book_mtime") != ctx["new_mtime"]:
            d["book_mtime"] = ctx["new_mtime"]; changed = True
        return changed
    plan, warn = _up_json_plan(_fig_path_abs(ctx["ap"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _pam_ocrfix(ctx):
    """pdf-ocr-fix/<book_sha>.json(选区重新识别校正,fixes[].page)——同 _pam_figures:迁页号+刷 book_mtime
    保住用户手动重扫的成果(否则 loader 的 mtime 守卫会整册清空)。"""
    def mut(d):
        arr, ch = _up_shift_pagelist(d.get("fixes"), ctx["mvd"])
        if ch:
            d["fixes"] = arr
        changed = ch
        if d.get("book_mtime") != ctx["new_mtime"]:
            d["book_mtime"] = ctx["new_mtime"]; changed = True
        return changed
    plan, warn = _up_json_plan(_ocrfix_path_abs(ctx["ap"]), mut)
    return ([plan] if plan else []), ([warn] if warn else [])


def _up_rename_plans(dirpath: Path, pat, namer, mv, one_based: bool) -> list:
    """按页命名的文件簇改名迁移(pdf-page-ocr/<sha16>-p{N}.json、mokuro/vision p%04d.*)。
    pat(name)->页标识 int|None;namer(n)->新文件名。+1 方向按页号降序改名(不撞名)、-1 升序;删除的页先出 unlink plan。"""
    if not dirpath.exists():
        return []
    moves, drops = [], []
    for f in dirpath.iterdir():
        n = pat(f.name)
        if n is None:
            continue
        page1 = n + 1 if not one_based else n     # 统一成 1-based 页号喂 mv
        np1 = mv(page1)
        if np1 is None:
            drops.append(f)
        elif np1 != page1:
            nn = np1 - 1 if not one_based else np1
            moves.append((page1, f, dirpath / namer(nn, f.name)))
    plans = []
    for f in drops:
        try:
            plans.append(("unlink", f, f.read_bytes()))
        except Exception:
            pass
    up = all(mv(p) >= p for p, _, _ in moves) if moves else True   # 全 +1 → 降序;全 -1 → 升序
    for _, src, dst in sorted(moves, key=lambda x: x[0], reverse=up):
        plans.append(("rename", src, dst))
    return plans


def _pam_page_ocr(ctx):
    """单页重扫覆盖 sidecar:页号在文件名(<sha16(rel)>-p{N}.json,1-based)→ 改名迁移。内容跟页绑定 → mvd。"""
    import hashlib
    sha = hashlib.sha1(ctx["rel"].encode("utf-8")).hexdigest()[:16]
    rx = re.compile(r"^" + re.escape(sha) + r"-p(\d+)\.json$")
    def pat(name):
        m = rx.match(name)
        return int(m.group(1)) if m else None
    def namer(n, _old):
        return "%s-p%d.json" % (sha, n)
    return _up_rename_plans(_PAGE_OCR_DIR, pat, namer, ctx["mvd"], one_based=True), []


def _pam_ocr_checkpoints(ctx):
    """mokuro/google-vision OCR 按页 checkpoint(p%04d.json/.png,0-based idx)→ 改名迁移,
    防未来重跑 OCR 时旧 checkpoint 按页复用串位(embed 脚本按文件名 glob 消费,json 内 _page 字段仅 provenance)。"""
    plans = []
    rx = re.compile(r"^p(\d{4})\.(json|png)$")
    def pat(name):
        m = rx.match(name)
        return int(m.group(1)) if m else None
    for base in (CLAUDE_DIR / "state" / "mokuro-ocr", CLAUDE_DIR / "state" / "google-vision-ocr"):
        d = base / ctx["sha"]
        def namer(n, old, _d=d):
            return "p%04d.%s" % (n, old.rsplit(".", 1)[1])
        plans += _up_rename_plans(d, pat, namer, ctx["mvd"], one_based=False)
    return plans, []


# ⚠ 注册表:所有「按 PDF 页号锚定」的存储在此穷尽登记(盘点结论 2026-07-03,详见设计文档规格 v2)。
#   免迁(天然失效)不进表:页图/字符层/振假名/整本文本缓存(文件名含 mtime)、FTS 搜索库(meta.mtime,
#   quick_sync ≤15min 自动整书重建)、pdf-sent-dismissed(按句文本)、lastopen(无页号)、
#   grammar-tracked/history + spacy 缓存(按节点/句文本)、assistant 会话(历史上下文,印刷页语义)、
#   vocab-lookups.jsonl(追加日志,只做近期活跃页启发)、pdf-prefs 阅读位置(客户端 LS 为真源,±1 接受)、
#   book-preprocess 状态(一次性流水线)、pdf-book-{langs,crop,figures,offset}(无页号/印刷页偏移标量,
#   偏移在插入点之后差 1 属已知妥协,可在设置里重新对齐)。
PAGE_ANCHOR_MIGRATIONS = [
    ("pdf-highlights", _pam_highlights),
    ("reader-notes", _pam_notes),
    ("pdf-ink", _pam_ink),
    ("reader-favorites", _pam_favorites),
    ("reader-userpages", _pam_userpages),
    ("pdf-tr-sentences", _pam_tr_sentences),
    ("pdf-char-offset", _pam_char_offset),
    ("pdf-toc-range", _pam_toc),
    ("sentence-cards", _pam_sentence_cards),
    ("vocab-exposure", _pam_vocab_exposure),
    ("pdf-figures", _pam_figures),
    ("pdf-ocr-fix", _pam_ocrfix),
    ("pdf-page-ocr", _pam_page_ocr),
    ("ocr-checkpoints", _pam_ocr_checkpoints),
]


def _up_collect_plans(ctx) -> tuple:
    """阶段1:纯内存跑全部迁移器,收集写盘/改名/删除计划。任何迁移器抛异常 → 整体中止(调用方恢复 PDF 备份)。"""
    plans, warns = [], []
    for name, fn in PAGE_ANCHOR_MIGRATIONS:
        try:
            p, w = fn(ctx)
        except Exception as ex:
            raise RuntimeError("迁移器 %s 失败:%s" % (name, ex))
        plans += p or []
        warns += w or []
    return plans, warns


def _up_apply_plans(plans):
    """阶段2:统一落盘。write=tmp+原子替换;rename/unlink 直接执行。任何失败 → 逆序回滚已完成项后抛出。"""
    done = []
    try:
        for pl in plans:
            if pl[0] == "write":
                _, path, nb, _ob = pl
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".migtmp")
                tmp.write_bytes(nb)
                tmp.replace(path)
            elif pl[0] == "rename":
                _, src, dst = pl
                src.replace(dst)
            elif pl[0] == "unlink":
                _, path, _ob = pl
                path.unlink()
            done.append(pl)
    except Exception:
        for pl in reversed(done):
            try:
                if pl[0] == "write":
                    _, path, _nb, ob = pl
                    if ob is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(ob)
                elif pl[0] == "rename":
                    _, src, dst = pl
                    dst.replace(src)
                elif pl[0] == "unlink":
                    _, path, ob = pl
                    path.write_bytes(ob)
            except Exception:
                pass
        raise


def _up_backup_book(ap: Path, sha: str) -> Path:
    import shutil
    bdir = _PAGE_BACKUP_DIR / sha
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / (_time.strftime("%Y%m%d-%H%M%S") + ".pdf")
    shutil.copy2(str(ap), str(dst))
    if dst.stat().st_size != ap.stat().st_size:
        raise RuntimeError("备份文件大小与原书不一致")
    return dst


def _up_prune_backups(sha: str, keep: int = 2):
    try:
        fs = sorted((_PAGE_BACKUP_DIR / sha).glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in fs[keep:]:
            f.unlink()
    except Exception:
        pass


def _inspage_job(jid: str, mode: str, rel: str, ap: Path, payload: dict):
    """后台 job:insert/edit/delete 三种操作同一套安全流程。"""
    import fitz, shutil
    sha = _book_sha(ap)
    tmp = None
    try:
        _job_set(jid, status="running", kind="pdf-inspage", step="备份原书…", ts=_time.time())
        size = ap.stat().st_size
        if hasattr(os, "statvfs"):   # Windows 本地实例没有 statvfs,跳过守卫
            st = os.statvfs(str(ap.parent))
            if st.f_bavail * st.f_frsize < size * 2 + (100 << 20):
                raise RuntimeError("磁盘空间不足(备份+临时文件需要约 2×书大小)")
        backup = _up_backup_book(ap, sha)
        _job_set(jid, step="改写 PDF…")
        doc = fitz.open(str(ap))
        n0 = doc.page_count
        warns = []
        if mode == "insert":
            after = int(payload["after"])
            if not (0 <= after <= n0):
                raise RuntimeError("插入位置越界(书共 %d 页)" % n0)
            ref = doc[min(max(after - 1, 0), n0 - 1)].rect   # 尺寸=邻页(after=0 取第1页)
            pg = doc.new_page(pno=(after if after < n0 else -1), width=ref.width, height=ref.height)
            warns += _up_render_page(pg, payload.get("title") or "", payload.get("md") or "")
            expect, pivot = n0 + 1, after
            mv = mvd = _up_mv_insert(after)
        elif mode == "edit":
            pno = int(payload["page"])
            if not (1 <= pno <= n0):
                raise RuntimeError("页号越界")
            ref = doc[pno - 1].rect
            doc.delete_page(pno - 1)
            pg = doc.new_page(pno=(pno - 1 if pno - 1 < doc.page_count else -1), width=ref.width, height=ref.height)
            warns += _up_render_page(pg, payload.get("title") or "", payload.get("md") or "")
            expect, pivot = n0, pno
            mv, mvd = _up_mv_keep(), _up_mv_droponly(pno)
        else:   # delete
            pno = int(payload["page"])
            if not (1 <= pno <= n0):
                raise RuntimeError("页号越界")
            if n0 <= 1:
                raise RuntimeError("整本书只剩这一页,不能删")
            doc.delete_page(pno - 1)
            expect, pivot = n0 - 1, pno
            mv = mvd = _up_mv_delete(pno)
        _job_set(jid, step="保存新文件…(大书要一会)")
        tmp = ap.with_name(".instmp-" + _uuid.uuid4().hex[:8] + ".pdf")   # 同目录(同文件系统才能原子替换);点开头避开 Obsidian Sync
        doc.save(str(tmp), garbage=(3 if size < (40 << 20) else 1), deflate=True)
        doc.close()
        _job_set(jid, step="校验新文件…")
        d2 = fitz.open(str(tmp))
        got = d2.page_count
        d2.close()
        if got != expect:
            raise RuntimeError("页数断言失败:期望 %d 得到 %d,原书未动" % (expect, got))
        # journal:写在替换前、迁移全部完成后删。中间进程死掉 → 残留 → 后续操作 409(防错位继续写)
        jp = _up_journal_path(sha)
        jp.write_text(json.dumps({"mode": mode, "rel": rel, "backup": str(backup),
                                  "ts": int(_time.time())}, ensure_ascii=False), "utf-8")
        os.replace(str(tmp), str(ap))
        tmp = None
        _job_set(jid, step="迁移页锚(高亮/便签/墨迹/图注等)…")
        new_mtime = int(os.path.getmtime(str(ap)))
        try:
            ctx = {"rel": rel, "ap": ap, "sha": sha, "mv": mv, "mvd": mvd, "mode": mode, "pivot": pivot,
                   "new_mtime": new_mtime, "record_op": payload["record_op"]}
            # 🔴 BLOCKER③:持 per-rel userpages 锁跨「phase1 读所有 sidecar → phase2 落盘」整个事务,
            #   期间前端 PATCH(/api/userpages)阻塞 → 迁移读到的 userpages 快照与落盘之间不会被 PATCH 插入 →
            #   phase2 不会覆盖 PATCH 刚写的新编辑。doc.save(慢,几秒)在此之前,锁只压这段几毫秒的迁移。
            with _upages_lock(rel):
                plans, w2 = _up_collect_plans(ctx)
                warns += w2
                _up_apply_plans(plans)
        except Exception as ex:
            shutil.copy2(str(backup), str(ap))   # 全不成:恢复原书,不写任何 sidecar
            jp.unlink(missing_ok=True)
            raise RuntimeError("锚迁移失败,已从备份恢复原书、未改任何数据:%s" % ex)
        jp.unlink(missing_ok=True)
        _up_prune_backups(sha)
        result = {"ok": True, "mode": mode, "warnings": warns, "mtime": new_mtime}
        if mode == "insert":
            result["page"] = pivot + 1
        _job_set(jid, status="done", result=result, ts=_time.time())
    except Exception as ex:
        try:
            if tmp and tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        _job_set(jid, status="error", error=str(ex), ts=_time.time())
    finally:
        with _INSPAGE_MUTEX:
            _INSPAGE_ACTIVE.discard(rel)


@bp.route("/api/pdf-insert-page", methods=["POST", "PATCH", "DELETE"])
def pdf_api_pdf_insert_page():
    """真插入页(异步 job,轮询 /pdf/api/job-status?id=):
    POST {file, after, title?, md?} → 在 after(1-based;0=书首)后插入真实页;
    PATCH {file, id, title?, md?} → 重排该用户页内容(delete_page+同位重插,页号不变);
    DELETE ?file=&id= → 删除该用户页(后续页锚 -1)。均返回 {ok, job_id}。"""
    if request.method == "DELETE":
        body = {"file": request.args.get("file", ""), "id": request.args.get("id", "")}
    else:
        body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    ap = _safe_vault_path(rel)
    if not ap or not ap.exists() or ap.suffix.lower() != ".pdf":
        return jsonify({"ok": False, "error": "文件不存在或不是 PDF"}), 400
    sha = _book_sha(ap)
    if _up_journal_path(sha).exists():
        return jsonify({"ok": False, "error": "检测到上次改页中断(journal 残留),数据可能不一致。"
                                              "请先人工核对 state/pdf-page-backups/%s/ 下的备份后删除 journal.json 再操作" % sha}), 409
    # 预处理(OCR/嵌入)进行中不许改页(流水线按页写 checkpoint,并发改页必串位)
    try:
        pst = json.loads((_BOOK_PREPROCESS_DIR / (sha + ".json")).read_text("utf-8"))
        if pst.get("phase") in ("detecting", "normalizing", "ocr", "embedding", "compressing") and _pid_alive(pst.get("pid")):
            return jsonify({"ok": False, "error": "本书正在预处理(OCR/嵌入),完成后再插页"}), 409
    except FileNotFoundError:
        pass
    except Exception:
        pass
    import fitz
    if request.method == "POST":
        try:
            after = int(body.get("after"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "缺少 after"}), 400
        try:
            n = fitz.open(str(ap)).page_count
        except Exception as ex:
            return jsonify({"ok": False, "error": "打不开 PDF:%s" % ex}), 500
        if not (0 <= after <= n):
            return jsonify({"ok": False, "error": "after 越界(书共 %d 页)" % n}), 400
        rid = "u_" + _uuid.uuid4().hex[:8]
        # v4:新建一律 overlay 模式(空白真页 + sidecar 文字即时编辑)。md 通常空(前端先建空白再即时编辑);
        #     即便带初始 md,job 渲进 PDF + record_op md_ver=0/synced_ver=0=已同步态,之后编辑 PATCH 才转脏。
        payload = {"after": after, "title": (body.get("title") or "")[:120], "md": (body.get("md") or "")[:100000],
                   "record_op": {"op": "add", "id": rid, "page": after + 1, "mode": "overlay",
                                 "title": (body.get("title") or "")[:120], "md": (body.get("md") or "")[:100000]}}
        mode = "insert"
    else:
        uid = (body.get("id") or "").strip()
        rec = next((x for x in _upages_load(rel) if x.get("id") == uid), None)
        if not rec:
            return jsonify({"ok": False, "error": "未找到该用户页记录"}), 404
        if not isinstance(rec.get("page"), int):
            return jsonify({"ok": False, "error": "这是旧版虚拟页,请用页面上的 ✏️/🗑 直接编辑(不改 PDF)"}), 400
        if request.method == "PATCH":
            if rec.get("mode") == "overlay":
                # v4 批次2:overlay 后台同步 = 把 sidecar 当前 md 写进那张(空白/旧)真页(edit job:delete_page+同位
                #   重插,页号不变 → 零页号锚迁移)。在 per-rel 锁下**原子快照** md+md_ver(与前端 PATCH 串行,防读到半写);
                #   不脏(md_ver<=synced_ver)直接免同步(省一次昂贵 doc.save);record_op **只带 base_ver,绝不带 md/title**
                #   (blocker②:job 不回写 sidecar)。base_ver = 服务端权威快照的 md_ver(不信客户端)。
                with _upages_lock(rel):
                    rec2 = next((x for x in _upages_load(rel) if x.get("id") == uid), None) or rec
                    md_ver = int(rec2.get("md_ver", 0)); synced_ver = int(rec2.get("synced_ver", 0))
                    snap_md = (rec2.get("md") or "")[:100000]; snap_title = (rec2.get("title") or "")[:120]
                    page_no = rec2.get("page")
                if md_ver <= synced_ver:
                    return jsonify({"ok": True, "clean": True, "md_ver": md_ver, "synced_ver": synced_ver})
                payload = {"page": page_no, "title": snap_title, "md": snap_md,
                           "record_op": {"op": "update", "id": uid, "base_ver": md_ver}}
                mode = "edit"
            else:
                title = (body.get("title") if "title" in body else rec.get("title") or "")[:120]
                md = (body.get("md") if "md" in body else rec.get("md") or "")[:100000]
                payload = {"page": rec["page"], "title": title, "md": md,
                           "record_op": {"op": "update", "id": uid, "title": title, "md": md}}
                mode = "edit"
        else:
            payload = {"page": rec["page"], "record_op": {"op": "remove", "id": uid}}
            mode = "delete"
    with _INSPAGE_MUTEX:
        if rel in _INSPAGE_ACTIVE:
            return jsonify({"ok": False, "error": "本书已有改页任务进行中"}), 409
        _INSPAGE_ACTIVE.add(rel)
    jid = _uuid.uuid4().hex[:12]
    _job_set(jid, status="running", kind="pdf-inspage", step="排队中…", ts=_time.time())
    _upthr.Thread(target=_inspage_job, args=(jid, mode, rel, ap, payload), daemon=True).start()
    return jsonify({"ok": True, "job_id": jid})


# ── 收藏夹(全局 sidecar:state/reader-favorites.json;设计:references/reader-userpages-favorites.md「二、收藏夹设计」)──
# 收藏夹 = 一本"虚拟书":夹内条目指向各原书的某页(PDF)/某章(EPUB);查看页(/pdf/fav/view)只读原书资源,
# **零进度状态**(不 _lastopen_touch、不写 LS.pos)。阶段A:数据模型 + CRUD + ⭐picker + 书架 tab + 查看页(即时类功能)。
_FAV_FILE = CLAUDE_DIR / "state" / "reader-favorites.json"


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
    rel = (raw.get("file") or "").strip()
    kind = (raw.get("kind") or "").strip()
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
_FAV_HTML_DIR = CLAUDE_DIR / "state" / "reader-fav-html"   # 【退役】v4 流式 HTML 产物(仅用于重建/删夹时清理残留)


def _fav_html_path(fid: str) -> Path:
    """【退役】v4 流式 HTML 产物路径(仅用于重建/删夹时清理残留)。"""
    return _FAV_HTML_DIR / (fid + ".html")


def _fav_epub_path(fid: str) -> Path:
    """v5 物化产物:一本真 EPUB(state,非 vault)。"""
    return _FAV_EPUB_DIR / (fid + ".epub")


# ── 收藏集 AI 元数据(v8):build 顺带写,给 EPUB 侧栏助手认收藏集 + 目录概览 + 相邻性 + 翻原书 ──
_FAV_META_DIR = CLAUDE_DIR / "state" / "reader-fav-meta"


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


def _fav_sep_html(label: str, href: str) -> str:
    """条目分隔条:来源标签 + 「打开原书 ↗」深链。fav.css 给 .fav-sep 上 user-select:none(不误建高亮);
    「打开原书」是站内 <a>(epub-html tap 处理器对 closest('a') 让位 → 正常导航,不触发单击查词)。"""
    return _fav_sep_html3(label, href, None)


def _fav_sep_html3(label: str, href: str, item) -> str:
    """同 _fav_sep_html,多传原始收藏条目 → 来源条尾部加「☆ 取消收藏」按钮(data-fitem=条目 JSON,
    前端 PATCH remove_item → 乐观隐藏本节 + 后台重建 reconcile 收尾)。item=None 则不加按钮。"""
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
    sep = _fav_sep_html3(label, href, it)
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
    sep = _fav_sep_html3(label, href, it)
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
        return label, (_fav_sep_html3(label, href, it)
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
    return label, _fav_sep_html3(label, href, it) + body_inner, imgs


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


@bp.route("/api/favorites", methods=["GET", "POST", "PATCH", "DELETE"])
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


@bp.route("/api/fav-meta")
def pdf_api_fav_meta():
    """收藏夹 AI 元数据(section↔条目映射,build 时落 state/reader-fav-meta/<fid>.json)。
    已打开的收藏夹阅读器用它做**结构真·增量重排**(fav-built 事件后 diff 新旧 items,新增长出来/移除当场消失,不刷新)。"""
    fid = (request.args.get("id") or "").strip()
    if not re.match(r"^f_[0-9a-zA-Z]+$", fid):
        abort(404)
    r = jsonify({"ok": True, "meta": _fav_meta_load(fid)})
    r.headers["Cache-Control"] = "no-store"
    return r


def _fav_js_v():
    """【已退役 2026-07-04·规格 v3】收藏夹精简查看页(fav_reader.html + fav-reader.js)的 cache-bust。
    收藏夹统一化后改为「物化成真 PDF + 标准阅读器打开」(/fav/open),精简页退役,本函数不再被调用
    (fav_reader.html / fav-reader.js 保留在盘上防外部书签,书架不再链接)。"""
    mt = 0
    for name in ("fav-reader.js", "rc-core.js", "rc-md.js", "rc-snippets.js",
                 "rc-result.js", "rc-wordpop.js", "rc-favorites.js"):
        for base in ("/var/www/html/static/pdf",
                     str(Path(__file__).resolve().parent / "static" / "pdf")):
            try:
                mt = max(mt, int(os.path.getmtime(os.path.join(base, name)))); break
            except Exception:
                continue
    return str(mt or 1)


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
        is_fav=True, fav_id=fid))   # is_fav → 模板注入收藏夹专属 PWA manifest/apple 标签(独立 app 入口)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@bp.route("/fav/open")
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


@bp.route("/fav/view")
def pdf_fav_view():
    """【已退役·规格 v3/v4】旧精简查看页 → 302 到 /fav/open(现=流式 HTML 阅读器)。
    路由保留只为兼容旧书签/外部链接;书架已改指 /fav/open。"""
    fid = (request.args.get("id") or "").strip()
    return redirect("/pdf/fav/open?id=" + urllib.parse.quote(fid))


@bp.route("/fav/manifest")
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


@bp.route("/fav/icon")
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


# ── EPUB 手写墨迹 sidecar(按 section idx 存归一化笔画;独立 state/epub-ink/<sha>.json)──
# 照搬 PDF /api/ink(state/pdf-ink),把锚从「页码」换成「EPUB section 索引」(reflow 无固定页)。
# stroke = {t:'pen'|'line'|'arrow'|'rect', c, w, p:[[x,y],...]},坐标归一化 0-1(相对 section 内容盒)。
_EPUB_INK_DIR = CLAUDE_DIR / "state" / "epub-ink"


def _epub_ink_path(rel: str) -> Path:
    import hashlib
    return _EPUB_INK_DIR / (hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16] + ".json")


def _epub_ink_load(rel: str) -> dict:
    try:
        data = json.loads(_epub_ink_path(rel).read_text("utf-8"))
        if not isinstance(data.get("sections"), dict):
            data["sections"] = {}
        data["file_rel"] = rel
        return data
    except Exception:
        return {"file_rel": rel, "sections": {}}


def _epub_ink_save(rel: str, data: dict):
    _EPUB_INK_DIR.mkdir(parents=True, exist_ok=True)
    p = _epub_ink_path(rel)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(p)


@bp.route("/api/epub-ink", methods=["GET", "POST"])
def pdf_api_epub_ink():
    """EPUB 手写墨迹(按 section idx 存,独立 sidecar:state/epub-ink/<sha>.json)。
    GET ?file= → {ok, sections:{"<idx>":[stroke,...]}};
    POST {file, idx, strokes:[...]} → 整段替换该 section 墨迹(strokes 空则删该段)。"""
    if request.method == "GET":
        rel = (request.args.get("file") or "").strip()
        if not rel:
            return jsonify({"ok": False, "error": "缺少 file"}), 400
        r = jsonify({"ok": True, **_epub_ink_load(rel)})
        r.headers["Cache-Control"] = "no-store"   # 同 /api/ink:实时同步读源禁缓存
        return r
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    if not rel:
        return jsonify({"ok": False, "error": "缺少 file"}), 400
    # idx = 正文章节序(非负整数) 或 插入页(.ep-usec)id(字符串 u_<8hex>,独立编号空间,不是章序)。
    # sections dict 本就以字符串为键,两类共存互不干扰;用户页墨迹随插入页一起持久化(设计见 reader-userpages-favorites.md「EPUB 插入页·手写」)。
    raw_idx = data.get("idx")
    if isinstance(raw_idx, str) and re.fullmatch(r"u_[0-9a-fA-F]{4,16}", raw_idx):
        key = raw_idx
    else:
        try:
            n = int(raw_idx)
        except (TypeError, ValueError):
            n = -1
        key = str(n) if n >= 0 else None
    if key is None:
        return jsonify({"ok": False, "error": "invalid idx"}), 400
    strokes = data.get("strokes")
    if not isinstance(strokes, list):
        return jsonify({"ok": False, "error": "invalid strokes"}), 400
    if len(strokes) > 5000:
        return jsonify({"ok": False, "error": "too many strokes"}), 400
    doc = _epub_ink_load(rel)
    if strokes:
        doc["sections"][key] = strokes
    else:
        doc["sections"].pop(key, None)
    _epub_ink_save(rel, doc)
    try:
        _reader_publish("ink", rel, key)   # 推「墨迹变了」给其它打开着的客户端(实时同步)
    except Exception:
        pass
    return jsonify({"ok": True, "count": len(strokes)})


# ── 阅读器实时事件推送(SSE pub/sub;2026-07-05):自建页墨迹/正文一存 → 推「变了」给其它打开着的客户端 →
#   活跃侧收到即重拉+就地重渲(~1s,取代 7s 轮询);不活跃侧忽略(后端已更新,回来 visibility 再同步)。
#   webapp = 单 worker gthread(见 systemd)→ 进程内 pub/sub 直接通,无需跨进程总线;长连接占 1 线程、余 7 个照常服务。
import queue as _rq
_READER_SUBS = set()          # set[queue.Queue]:每个订阅的 SSE 连接一个队列
_READER_SUBS_LOCK = _upthr.Lock()


def _reader_publish(kind: str, file: str, uid):
    ev = {"kind": kind, "file": file, "uid": (str(uid) if uid is not None else None), "t": int(_time.time())}
    with _READER_SUBS_LOCK:
        subs = list(_READER_SUBS)
    for q in subs:
        try:
            q.put_nowait(ev)
        except Exception:
            pass


@bp.route("/api/reader-events")
def pdf_reader_events():
    """阅读器变更事件流(SSE):客户端订阅,服务端在墨迹/正文保存后推 {kind,file,uid}。EventSource 自带断线重连。"""
    q = _rq.Queue(maxsize=128)
    with _READER_SUBS_LOCK:
        _READER_SUBS.add(q)

    def gen():
        try:
            yield "retry: 3000\n\n"          # EventSource 重连间隔 3s
            yield ": connected\n\n"
            t0 = _time.time()
            while _time.time() - t0 < 300:    # 5min 回收连接(gunicorn timeout 600 前),EventSource 自动重连、连接常新
                try:
                    ev = q.get(timeout=15)
                    yield "event: change\ndata: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except _rq.Empty:
                    yield ": ka\n\n"          # 心跳(保活 + 检测死连接)
        except GeneratorExit:
            pass
        finally:
            with _READER_SUBS_LOCK:
                _READER_SUBS.discard(q)
    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ════════════════════════════════════════════════════════════════════════════
# 统一 HTML 阅读器(架构验收)—— HTML 内容渲进主文档 + HtmlAdapter + 共享 rc-*.js。
# 证明「给一个新阅读器写个 adapter,共享功能(选区/查词/翻译/解释/对话/笔记/制卡/高亮)就全有」。
# 纯新增,零碰 PDF/EPUB 现有路由。AI 端点(translate/explain/dict)内容无关,直接复用。
# ════════════════════════════════════════════════════════════════════════════

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


@bp.route("/html/view")
def html_view():
    """统一 HTML 阅读器主页(架构验收)。?file=<vault-rel .html/.md>;无 file → 内置 sample。"""
    from flask import make_response
    rel = (request.args.get("file") or "").strip()
    if rel:
        abs_path = _safe_vault_path(rel)
        if not abs_path or abs_path.suffix.lower() not in (".html", ".htm", ".md", ".markdown"):
            abort(404)
        rel_clean = abs_path.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
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


# ── HTML 阅读器高亮 sidecar(字符偏移锚,独立 state/html-highlights/<sha>.json;照搬 epub-highlights 形态)──
_HTML_HL_DIR = CLAUDE_DIR / "state" / "html-highlights"


def _html_hl_path(rel: str) -> Path:
    import hashlib
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
        import uuid as _u
        h = {"id": "h" + _u.uuid4().hex[:11], "start": start, "end": end,
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


@bp.route("/api/epub-to-full", methods=["POST"])
def pdf_api_epub_to_full():
    """把 vault 里某本 .epub 后台转成同名 .pdf(Calibre 防跨页),用现有 PDF 阅读器打开 = 全套控制层。
    产物已存在 → 直接回 view_url;否则起后台转换(survivable),前端轮询 /api/ebook-convert-status。"""
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    abs_path = _safe_vault_path(rel)
    if not abs_path or abs_path.suffix.lower() != ".epub":
        return jsonify({"ok": False, "error": "bad file"}), 400
    out_pdf = abs_path.with_suffix(".pdf")
    out_rel = out_pdf.relative_to(OBSIDIAN_ROOT).as_posix()
    out_url = f"/pdf/view?file={urllib.parse.quote(out_rel, safe='/')}"
    if out_pdf.exists() and out_pdf.stat().st_size > 1000:
        return jsonify({"ok": True, "ready": True, "rel": out_rel, "view_url": out_url})
    import uuid
    job = uuid.uuid4().hex[:12]
    _EBOOK_CONV_DIR.mkdir(parents=True, exist_ok=True)
    prog = _EBOOK_CONV_DIR / f"{job}.json"
    try:
        prog.write_text(json.dumps({"status": "converting", "rel": out_rel, "ts": int(__import__("time").time())}), "utf-8")
    except Exception:
        pass
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = ["nice", "-n", "15", py, str(CLAUDE_DIR / "scripts" / "convert_ebook.py"),
           str(abs_path), str(out_pdf), "--progress", str(prog)]
    try:
        _spawn_survivable(cmd, str(CLAUDE_DIR))
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动转换失败：{ex}"}), 500
    return jsonify({"ok": True, "converting": True, "job": job, "rel": out_rel, "view_url": out_url})


@bp.route("/api/epub-search")
def pdf_api_epub_search():
    """EPUB 全文搜索:服务端 grep 解包出的 XHTML(快,不用浏览器加载几千章)。
    GET ?file=<rel>&q=<词> → {ok, results:[{href, loc, excerpt}]}。href 相对 OPF 目录 → epub.js display(href) 可跳。"""
    rel = (request.args.get("file") or "").strip()
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": True, "results": []})
    abs_path = _resolve_epub_book(rel)   # 收藏夹物化 EPUB 也可全文搜索
    if not abs_path or abs_path.suffix.lower() != ".epub":
        return jsonify({"ok": False, "error": "bad file"}), 400
    root = _ensure_epub_extracted(abs_path, rel)
    if not root:
        return jsonify({"ok": False, "error": "解包失败"}), 500
    import re
    # OPF 目录(算 href 相对路径用)
    try:
        cont = (root / "META-INF" / "container.xml").read_text("utf-8", "ignore")
        opf_rel = re.search(r'full-path="([^"]+)"', cont).group(1)
        opf_dir = (root / opf_rel).parent
    except Exception:
        opf_dir = root
    ql = q.lower()
    tag_re = re.compile(r"<[^>]+>")
    results = []
    sections = _epub_opf_info(root)["sections"]   # spine 顺序 → idx 直接给前端精确跳转
    for idx, fp in enumerate(sections):
        if len(results) >= 80:
            break
        try:
            raw = fp.read_text("utf-8", "ignore")
        except Exception:
            continue
        # 去标签 → 纯文本
        body = raw.split("<body", 1)[-1]
        text = tag_re.sub(" ", body)
        text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        low = text.lower()
        if ql not in low:
            continue
        start = 0
        cnt = 0
        while cnt < 3:
            i = low.find(ql, start)
            if i < 0:
                break
            a = max(0, i - 40); b = min(len(text), i + len(q) + 50)
            excerpt = ("…" if a > 0 else "") + text[a:i] + "" + text[i:i + len(q)] + "" + text[i + len(q):b] + ("…" if b < len(text) else "")
            results.append({"idx": idx, "loc": fp.stem, "excerpt": excerpt})
            start = i + len(q); cnt += 1
            if len(results) >= 80:
                break
    return jsonify({"ok": True, "results": results, "truncated": len(results) >= 80})


# ── Phase G EPUB 数据源：振假名/音标、整段翻译、生词掌握度查找表 ──────────────────
# 纯新增；复用 PDF 阅读器现成实现（unidic 分词 _apply_jp_tokenize / ECDICT 音标
# _build_en_furigana / Google 批量翻译 gtranslate_batch + sidecar 缓存 / vocab_index）。
# EPUB 只有纯文本（无 char bbox）→ 用「合成 char dict」把字符索引当 x 坐标喂给那两个按
# bbox 出读音的函数，返回 furigana item 的 x0/x1 即字符 start/end 偏移（code point 计）。

_EPUB_FURI_CACHE_DIR = CLAUDE_DIR / "state" / "epub-furigana-cache"


def _epub_text_chars(text: str) -> list:
    """把纯文本转成「合成 char dict」（x0=字符在 text 中的偏移、宽 1、单行 y0=0/y1=1）。
    好让 _apply_jp_tokenize / _build_en_furigana 直接复用——它们按 char bbox 出读音/音标，
    这里「索引当 x 坐标」→ 返回 furigana item 的 x0/x1 即字符 start/end 偏移（end 不含）。
    whitespace 标 sp=True（跟 PDF chars 一致，分词/拼词跳过且不破坏分组对齐）。"""
    out: list = []
    for k, ch in enumerate(text or ""):
        out.append({"c": ch, "x0": float(k), "x1": float(k + 1),
                    "y0": 0.0, "y1": 1.0, "sp": ch.isspace()})
    return out


def _epub_furigana_tokens(text: str) -> list:
    """对一段文本算振假名/音标 token。
    日语含汉字 token → 平假名读音（unidic）；英文连续字母词 → IPA 音标（ECDICT）。
    返回 [{start,end,reading,kind}]，start/end=字符偏移（end 不含），kind='jp'|'en'。
    纯假名/无音标词不产 token（前端无需注音）。"""
    text = text or ""
    if not text.strip():
        return []
    chars = _epub_text_chars(text)
    toks: list = []
    # 日语：仅含汉字的 token 出读音（_apply_jp_tokenize 内部还会再判一次 CJK，纯英文段直接 return）
    try:
        furi: list = []
        _apply_jp_tokenize(chars, 0, 0, furigana_out=furi)
        for it in furi:
            try:
                toks.append({"start": int(round(it["x0"])), "end": int(round(it["x1"])),
                             "reading": it.get("rt", ""), "kind": "jp"})
            except Exception:
                continue
    except Exception:
        pass
    # 英文：连续字母词查 ECDICT phonetic（x0/x1 即偏移）
    try:
        for it in _build_en_furigana(chars):
            try:
                toks.append({"start": int(round(it["x0"])), "end": int(round(it["x1"])),
                             "reading": it.get("rt", ""), "kind": "en"})
            except Exception:
                continue
    except Exception:
        pass
    toks.sort(key=lambda t: t["start"])
    return toks


def _epub_furi_cache_path(text: str) -> Path:
    import hashlib
    sha = hashlib.sha1(("furi::" + (text or "")).encode("utf-8")).hexdigest()[:16]
    return _EPUB_FURI_CACHE_DIR / f"{sha}.json"


def _epub_furi_cache_get(text: str):
    p = _epub_furi_cache_path(text)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _epub_furi_cache_put(text: str, tokens: list):
    try:
        _EPUB_FURI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _epub_furi_cache_path(text).write_text(
            json.dumps(tokens, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def _epub_section_paragraphs(rel: str, idx: int) -> list:
    """取 EPUB 某 section 的纯文本段落（给 {file,idx} 模式）。
    复用 _sanitize_epub_section 出的 HTML，块级标签换行 → 去标签 → 按非空行切段。"""
    abs_path = _resolve_epub_book(rel)   # 收藏夹物化 EPUB 也可取段落(振假名 by-idx / 助手 read_section)
    if not abs_path or abs_path.suffix.lower() != ".epub":
        return []
    root = _ensure_epub_extracted(abs_path, rel)
    if not root:
        return []
    secs = _epub_opf_info(root)["sections"]
    if idx < 0 or idx >= len(secs):
        return []
    try:
        html = _epub_section_cached(secs, idx, root, _epub_sha(rel))
    except Exception:
        return []
    import re as _re
    h = _re.sub(r"(?is)<(?:p|div|h[1-6]|li|br|section|article|blockquote|tr)[^>]*>", "\n", html)
    h = _re.sub(r"(?is)</(?:p|div|h[1-6]|li|section|article|blockquote|tr)>", "\n", h)
    h = _re.sub(r"(?is)<[^>]+>", "", h)
    h = (h.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
    out: list = []
    for line in h.split("\n"):
        s = line.strip()
        if s:
            out.append(s)
    return out


@bp.route("/api/epub-furigana", methods=["POST"])
def pdf_api_epub_furigana():
    """EPUB 段落振假名/音标。
    body: {file, texts:[...]}（一组段落纯文本）或 {file, idx}（按 section 取章节段落）。
    每段:含汉字 → unidic 分词出 token offset + 平假名读音;英文词 → ECDICT 音标。
    返回 {ok, items:[{text, tokens:[{start,end,reading,kind}]}]}。按文本哈希 sidecar 缓存
    （避免每次重算 unidic；fugashi tagger 进程内常驻，只首段加载一次）。"""
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    texts = body.get("texts")
    if not isinstance(texts, list):
        try:
            idx = int(body.get("idx") or 0)
        except Exception:
            idx = 0
        texts = _epub_section_paragraphs(rel, idx)
    items: list = []
    for t in texts[:4000]:
        t = "" if t is None else str(t)
        toks = _epub_furi_cache_get(t)
        if toks is None:
            toks = _epub_furigana_tokens(t)
            _epub_furi_cache_put(t, toks)
        items.append({"text": t, "tokens": toks})
    return jsonify({"ok": True, "items": items})


_EPUB_FURIFIX_DIR = CLAUDE_DIR / "state" / "epub-furigana-fix"


def _epub_furifix_path(text: str, readings: list) -> Path:
    import hashlib
    key = "epubfurifix::" + (text or "") + "||" + json.dumps(readings or [], ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    _EPUB_FURIFIX_DIR.mkdir(parents=True, exist_ok=True)
    return _EPUB_FURIFIX_DIR / f"{sha}.json"


@bp.route("/api/epub-furigana-verify", methods=["POST"])
def pdf_api_epub_furigana_verify():
    """EPUB 振假名读音**按整段上下文 AI 校正**(复用 PDF /api/furigana-verify 同款做法,
    通用解决日语量词/熟字訓/多音字读错,不硬编码)。
    POST {text, readings:[{wd,rt}|{word,reading}|{start,end,reading}]} → {ok, fixes:[{i,r}], readings:[...纠正后...]}。
    i=readings 下标、r=纠正后平假名(只列改了的)。按 (text+readings) 哈希永久缓存;
    前端 epub2-deco rubyApply 后台调一次,拿 fixes 原地替换 rt。"""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "")
    readings = body.get("readings")
    if not isinstance(readings, list) or not readings:
        return jsonify({"ok": False, "error": "缺少 readings"}), 400

    def _is_kana_rt(s):
        return bool(s) and all(("぀" <= ch <= "ヿ") or ch == "ー" for ch in s)

    def _word_of(it):
        if not isinstance(it, dict):
            return ""
        w = it.get("wd") or it.get("word") or it.get("text") or ""
        if not w and ("start" in it and "end" in it):
            try:
                w = text[int(it["start"]):int(it["end"])]
            except Exception:
                w = ""
        return w or ""

    def _rt_of(it):
        if not isinstance(it, dict):
            return ""
        return it.get("rt") or it.get("reading") or ""

    # 只校「日语假名读音」项(rt 全是假名)且词里含汉字(纯假名词无校正空间)→ prompt 小而集中
    cand = []
    for i, it in enumerate(readings[:60]):
        rt = _rt_of(it)
        wd = _word_of(it)
        if not _is_kana_rt(rt) or not wd:
            continue
        if not any("一" <= ch <= "鿿" for ch in wd):   # 含汉字才校(熟字訓/多音字/量词)
            continue
        cand.append((i, wd, rt))

    # 输出 readings:在原 readings 上把 rt/reading 替换为纠正后值
    def _build_out(fixes):
        fix_map = {f["i"]: f["r"] for f in fixes}
        out = []
        for i, it in enumerate(readings):
            if isinstance(it, dict) and i in fix_map:
                nit = dict(it)
                if "reading" in nit:
                    nit["reading"] = fix_map[i]
                else:
                    nit["rt"] = fix_map[i]
                out.append(nit)
            else:
                out.append(it)
        return out

    fpath = _epub_furifix_path(text, readings)
    if fpath.exists():
        try:
            fixes = json.loads(fpath.read_text("utf-8"))
            return jsonify({"ok": True, "cached": True, "fixes": fixes, "readings": _build_out(fixes)})
        except Exception:
            pass

    if not cand:
        try: fpath.write_text("[]", "utf-8")
        except Exception: pass
        return jsonify({"ok": True, "fixes": [], "readings": readings})

    ctx_text = (text or "").strip()[:600]
    listing = "\n".join(f"{i}. 「{wd}」(当前注 {rt})" for i, wd, rt in cand)
    prompt = (
        "下面是日语句子里的若干词及其假名注音,注音可能有误"
        "(常见错因:量词读音如 365日→にち、14日→か、3人→にん;熟字訓如 大人→おとな、今日→きょう;"
        "多音字按上下文不同读音如 行く→いく vs 行う→おこなう)。"
        "请结合句子上下文,给出每个词在该处的**正确平假名读音**。\n"
        + (f"句子上下文:「{ctx_text}」\n" if ctx_text else "")
        + listing + "\n\n"
        '严格只输出 JSON 数组,每项 {"i":序号,"r":"正确平假名"};每行都要给(即使本来就对)。不要解释。'
    )
    fixes = []
    try:
        raw = _ai_call(prompt, "dict")
        m = re.search(r"\[.*\]", raw or "", re.DOTALL)
        if m:
            arr = json.loads(m.group(0))
            cand_rt = {i: rt for i, _wd, rt in cand}
            for it in arr:
                if not isinstance(it, dict):
                    continue
                try:
                    i = int(it.get("i"))
                except (TypeError, ValueError):
                    continue
                r = (it.get("r") or "").strip()
                if i in cand_rt and r and _is_kana_rt(r) and len(r) <= 16 and r != cand_rt[i]:
                    fixes.append({"i": i, "r": r})
    except Exception as ex:
        sys.stderr.write(f"[epub-furigana-verify] {ex}\n")
    try:
        fpath.write_text(json.dumps(fixes, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return jsonify({"ok": True, "fixes": fixes, "readings": _build_out(fixes)})


# ── EPUB 跨设备 prefs(独立命名空间,跟 PDF 的 /api/prefs 同款 patch 存,按登录用户)──
# 存:续读 cfi / 字号 / 主题 / 行距 / ruby 开关 等 epub-* 键。iOS PWA↔Safari localStorage 不互通 → 存服务端同步。
_EPUB_PREFS_DIR = CLAUDE_DIR / "state" / "epub-prefs"


def _epub_prefs_path():
    import re as _re
    user = (session.get("username") or "anon")
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(user))[:64] or "anon"
    return _EPUB_PREFS_DIR / f"{safe}.json"


@bp.route("/api/epub-prefs", methods=["GET", "POST"])
def pdf_api_epub_prefs():
    """GET → {ok, prefs:{key:value}}; POST {patch:{k:v|null}} 合并(null=删) → {ok}。
    键为前端 localStorage 的 epub-*(续读 cfi / 字号 / 主题 / 行距 / ruby 开关)。"""
    p = _epub_prefs_path()
    try:
        cur = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        cur = {}
    if request.method == "GET":
        return jsonify({"ok": True, "prefs": cur})
    body = request.get_json(silent=True) or {}
    patch = body.get("patch") or {}
    if not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "bad patch"}), 400
    for k, v in patch.items():
        if not isinstance(k, str):
            continue
        if v is None:
            cur.pop(k, None)
        else:
            cur[str(k)] = v
    try:
        _EPUB_PREFS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), "utf-8")
        tmp.replace(p)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True})


def _epub_ja_tokens(text: str) -> list:
    """全分词:fugashi 把日语文本切成**全部**词 token(含纯假名/助词,不像 furigana 只给汉字词),
    返回 [{start,end}](字符偏移,end 不含)。供 EPUB 分词浮层给每个日语词盖可点按钮。
    含 CJK 才分;纯非 CJK 段返回 [](英文由前端按拉丁词自行包)。"""
    text = text or ""
    if not text.strip() or not any(_is_cjk_char(c) for c in text):
        return []
    tagger = _get_jp_tagger()
    if not tagger:
        return []
    toks: list = []
    pos = 0
    try:
        for w in tagger(text):
            surf = getattr(w, "surface", "") or ""
            if not surf:
                continue
            i = text.find(surf, pos)
            if i < 0:
                i = text.find(surf)
            if i < 0:
                continue
            toks.append({"start": i, "end": i + len(surf)})
            pos = i + len(surf)
    except Exception:
        return []
    return toks


def _epub_jatok_cache_path(text: str) -> Path:
    import hashlib
    sha = hashlib.sha1(("jatok::" + (text or "")).encode("utf-8")).hexdigest()[:16]
    return _EPUB_FURI_CACHE_DIR / f"jt-{sha}.json"


def _epub_jatok_cache_get(text: str):
    p = _epub_jatok_cache_path(text)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _epub_jatok_cache_put(text: str, tokens: list):
    try:
        _EPUB_FURI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _epub_jatok_cache_path(text).write_text(json.dumps(tokens, ensure_ascii=False), "utf-8")
    except Exception:
        pass


@bp.route("/api/epub-tokenize", methods=["POST"])
def pdf_api_epub_tokenize():
    """EPUB 日语全分词(给分词浮层用)。body: {file?, texts:[...]} → {ok, items:[{text, tokens:[{start,end}]}]}。
    按文本哈希缓存;纯非 CJK 段 tokens=[]。"""
    body = request.get_json(silent=True) or {}
    texts = body.get("texts")
    if not isinstance(texts, list):
        return jsonify({"ok": False, "error": "no texts"}), 400
    items: list = []
    for t in texts[:4000]:
        t = "" if t is None else str(t)
        toks = _epub_jatok_cache_get(t)
        if toks is None:
            toks = _epub_ja_tokens(t)
            _epub_jatok_cache_put(t, toks)
        items.append({"text": t, "tokens": toks})
    return jsonify({"ok": True, "items": items})


@bp.route("/api/epub-translate-section", methods=["POST"])
def pdf_api_epub_translate_section():
    """EPUB 段落批量翻译（中文）。body: {file, texts:[...]}。
    Google 批量翻译（gtranslate_batch）+ sidecar 永久缓存,未命中逐段 no_ai 兜底
    （gtranslate→deepl→mymemory，**绝不调 AI CLI**；10s 墙钟预算）。
    返回 {ok, translations:[zh,...]}（顺序对应 texts，未译留空串）。"""
    body = request.get_json(silent=True) or {}
    texts_in = body.get("texts")
    if not isinstance(texts_in, list):
        return jsonify({"ok": False, "error": "texts 必须是数组"}), 400
    texts = ["" if t is None else str(t) for t in texts_in][:4000]
    import sys as _sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in _sys.path:
        _sys.path.insert(0, str(vp))
    try:
        from translate import (gtranslate_batch as _gb, translate as _tr,
                               _cache_get as _cg, _cache_put as _cp)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"translate load fail: {ex}"}), 500
    # 1) 先吃缓存（空文本视为已译空串）
    zhs = [((_cg(t, "zh-CN") or "") if t.strip() else "") for t in texts]
    miss_idx = [i for i, t in enumerate(texts) if t.strip() and not zhs[i]]
    # 2) 未命中批量 Google
    if miss_idx:
        miss_texts = [texts[i] for i in miss_idx]
        batch = None
        try:
            batch = _gb(miss_texts)
        except Exception:
            batch = None
        if batch and len(batch) == len(miss_texts):
            for k, i in enumerate(miss_idx):
                if batch[k]:
                    zhs[i] = batch[k]
                    try: _cp(texts[i], "zh-CN", batch[k], "gtranslate")
                    except Exception: pass
        # 3) 仍缺 → 逐段 no_ai 兜底（translate() 内部自带缓存读写），10s 墙钟预算
        still = [i for i in miss_idx if not zhs[i]]
        _deadline = time.monotonic() + 10
        for i in still[:60]:
            if time.monotonic() > _deadline:
                break
            try:
                z = _tr(texts[i], backend="no_ai")
                if z:
                    zhs[i] = z
            except Exception:
                pass
    done = sum(1 for z in zhs if z)
    return jsonify({"ok": True, "translations": zhs, "translated": done, "total": len(texts)})


@bp.route("/api/vocab-mastery-map")
def pdf_api_vocab_mastery_map():
    """整本一次返回生词掌握度查找表（给 EPUB 前端画下划线着色，客户端缓存）。
    英文取 vocab_index（lemma + 所有 forms → mastery/label_slug）；日语取 jp-vocab trackable
    词（按熟悉度算 slug）。已掌握(mastered)的不返回（=不画下划线，跟 PDF 一致）。
    ?file= 保留参数（当前不分书，前端只对文本里出现的词查表，多余条目无害）。
    返回 {ok, map:{word_lower:{label, mastery}}, count}。"""
    out: dict = {}
    # 英文（及统一库里已并入的词）
    try:
        for w, info in (_vocab_idx() or {}).items():
            slug = info.get("label_slug") or ""
            if not slug or slug == "mastered":
                continue
            out[w] = {"label": slug, "mastery": round(float(info.get("mastery", 0) or 0), 3)}
    except Exception:
        pass
    # 日语 jp-vocab（查过的词，按熟悉度算 slug；mastered → _jp_vocab_slug 返回 None 跳过）
    try:
        for w, e in (_jp_vocab_load() or {}).items():
            if not _jp_vocab_is_trackable(w):
                continue
            slug = _jp_vocab_slug(e)
            if not slug:
                continue
            wl = w.lower()
            if wl not in out:   # vocab_index 已有则不覆盖（统一库优先）
                out[wl] = {"label": slug, "mastery": round(_jp_mastery(e), 3)}
    except Exception:
        pass
    return jsonify({"ok": True, "map": out, "count": len(out)})


_EPUB_DBG_LOG = CLAUDE_DIR / "state" / "epub-dbg.log"


@bp.route("/api/epub-dbg", methods=["POST"])
def pdf_api_epub_dbg():
    """EPUB 阅读器前端调试日志回传(临时诊断用):前端 dbg() 把每行 POST 来,服务端追加到 state/epub-dbg.log。
    这样不靠截图也能远程定位「选中不弹工具栏」卡在哪一步。"""
    body = request.get_json(silent=True) or {}
    msg = str(body.get("msg") or "")[:500]
    try:
        _EPUB_DBG_LOG.parent.mkdir(parents=True, exist_ok=True)
        # 控制大小:超过 64KB 截断重来
        if _EPUB_DBG_LOG.exists() and _EPUB_DBG_LOG.stat().st_size > 64 * 1024:
            _EPUB_DBG_LOG.write_text("", "utf-8")
        with _EPUB_DBG_LOG.open("a", encoding="utf-8") as f:
            f.write(__import__("time").strftime("%H:%M:%S") + "  " + msg + "\n")
    except Exception:
        pass
    return jsonify({"ok": True})


@bp.route("/api/upload", methods=["POST"])
def pdf_api_upload():
    """上传书到 vault 子目录。multipart form：file + target_dir（默认 资源/uploads/）。
    PDF 直存;**EPUB 直存(原生 reflow 阅读,不转换)**;MOBI/FB2/XPS/CBZ 仍服务端转 PDF。"""
    f = request.files.get("file")
    target_dir = (request.form.get("target_dir") or "资源/uploads").strip().strip("/")
    if not f:
        return jsonify({"ok": False, "error": "未选择文件"}), 400
    fname = f.filename or ""
    ext = Path(fname).suffix.lower()
    if ext != ".pdf" and ext not in _EBOOK_EXTS:
        return jsonify({"ok": False, "error": "支持的格式：PDF / EPUB / MOBI / FB2 / XPS / CBZ"}), 400
    # 防 path traversal
    if ".." in target_dir.split("/") or target_dir.startswith("/"):
        return jsonify({"ok": False, "error": "目标目录非法"}), 400
    dest_dir = (OBSIDIAN_ROOT / target_dir).resolve()
    try:
        dest_dir.relative_to(OBSIDIAN_ROOT.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "目标目录超出 vault"}), 400
    dest_dir.mkdir(parents=True, exist_ok=True)
    # PDF 存 .pdf;EPUB 原生存 .epub;其余电子书转换后产物叫 .pdf
    out_ext = ".epub" if ext == ".epub" else ".pdf"
    safe_name = _sanitize_filename(Path(fname).stem) + out_ext
    dest = dest_dir / safe_name
    if dest.exists():
        stem = safe_name[: -len(out_ext)]
        for i in range(1, 200):
            cand = dest_dir / f"{stem}-{i}{out_ext}"
            if not cand.exists():
                dest = cand; break
    if ext == ".pdf":
        try:
            f.save(str(dest))
        except Exception as ex:
            return jsonify({"ok": False, "error": f"保存失败：{ex}"}), 500
        rel = dest.relative_to(OBSIDIAN_ROOT).as_posix()
        return jsonify({"ok": True, "rel": rel, "converted": False, "kind": "pdf",
                        "view_url": f"/pdf/view?file={urllib.parse.quote(rel, safe='/')}"})
    if ext == ".epub":
        # 原生 reflow:直接存 .epub,开到 epub 阅读器(不转换、不阻塞)
        try:
            f.save(str(dest))
        except Exception as ex:
            return jsonify({"ok": False, "error": f"保存失败：{ex}"}), 500
        rel = dest.relative_to(OBSIDIAN_ROOT).as_posix()
        return jsonify({"ok": True, "rel": rel, "converted": False, "kind": "epub",
                        "view_url": f"/pdf/epub/view?file={urllib.parse.quote(rel, safe='/')}"})
    # ── 电子书 → **后台转 PDF**(多卷集/大书 Calibre 要几分钟,同步会让手机端 fetch 超时报错)──
    import uuid, subprocess
    job = uuid.uuid4().hex[:12]
    _EBOOK_CONV_DIR.mkdir(parents=True, exist_ok=True)
    staging = _EBOOK_CONV_DIR / f"{job}{ext}"
    try:
        f.save(str(staging))
    except Exception as ex:
        return jsonify({"ok": False, "error": f"保存失败：{ex}"}), 500
    rel = dest.relative_to(OBSIDIAN_ROOT).as_posix()
    prog = _EBOOK_CONV_DIR / f"{job}.json"
    try:
        prog.write_text(json.dumps({"status": "converting", "rel": rel, "ts": int(__import__("time").time())}), "utf-8")
    except Exception:
        pass
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = ["nice", "-n", "15", py, str(CLAUDE_DIR / "scripts" / "convert_ebook.py"),
           str(staging), str(dest), "--progress", str(prog)]
    try:
        _spawn_survivable(cmd, str(CLAUDE_DIR))   # 放进用户级 scope:webapp 重启/部署不会杀掉长转换
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动转换失败：{ex}"}), 500
    return jsonify({"ok": True, "converting": True, "job": job, "rel": rel, "name": dest.name,
                    "view_url": f"/pdf/view?file={urllib.parse.quote(rel, safe='/')}"})


_EBOOK_CONV_DIR = CLAUDE_DIR / "state" / "pdf-ebook-convert"


@bp.route("/api/ebook-convert-status")
def pdf_api_ebook_convert_status():
    """电子书后台转换进度。GET ?job=<id> → {status: converting|done|error, rel, view_url, error}。"""
    job = (request.args.get("job") or "").strip()
    if not job or not job.isalnum():
        return jsonify({"ok": False, "error": "bad job"}), 400
    prog = _EBOOK_CONV_DIR / f"{job}.json"
    st = {}
    try:
        st = json.loads(prog.read_text("utf-8"))
    except Exception:
        return jsonify({"ok": True, "status": "converting"})   # 文件还没写出 → 当转换中
    rel = st.get("rel", "")
    # 产物真生成了才算 done(防进度文件先写但文件没落)
    if st.get("status") == "done" and rel:
        ap = OBSIDIAN_ROOT / rel
        if ap.exists():
            # 清掉 staging 临时电子书
            for f in _EBOOK_CONV_DIR.glob(f"{job}.*"):
                if not f.name.endswith(".json"):
                    try: f.unlink()
                    except Exception: pass
            return jsonify({"ok": True, "status": "done", "rel": rel,
                            "view_url": f"/pdf/view?file={urllib.parse.quote(rel, safe='/')}"})
    if st.get("status") == "error":
        return jsonify({"ok": True, "status": "error", "error": st.get("error", "转换失败")})
    return jsonify({"ok": True, "status": "converting"})


def _sanitize_filename(s: str) -> str:
    """去非法字符 + 长度限制（同 qa_browser._sanitize_note_name）。"""
    import re as _re
    s = (s or "").strip().strip(".")
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s)
    return s[:120] or "untitled"


@bp.route("/api/epub-chat", methods=["POST"])
def pdf_api_epub_chat():
    """EPUB 阅读器侧栏对话(解耦,不走 PDF agentic 工具循环):带书名 + 选中文 + 所在章 + 近几轮历史
    拼 prompt → reader_ask/stream(explain 档:claude/Gemini 互兜底)。SSE 优先,JSON 兜底。"""
    body = request.get_json(silent=True) or {}
    msg = (body.get("message") or "").strip()
    if not msg:
        return jsonify({"ok": False, "error": "空消息"}), 400
    sel = (body.get("selection") or "").strip()
    chap = (body.get("chapter") or "").strip()      # 当前章节纯文本(可截断)
    book = (body.get("book") or "").strip()
    history = body.get("history") or []             # [{role, content}, ...] 近几轮
    parts = ["你是阅读助手,正在帮用户读一本电子书。用简洁中文回答。"
             "用 Markdown;数学公式严格用 $...$ 或 $$...$$,禁止用反引号包数学。"
             "回答完后另起一行,用 [[FOLLOWUP]]问题1|问题2|问题3[[/FOLLOWUP]] 给最多 3 条用户可能想继续追问的简短问题(没有就不给)。"]
    if book:
        parts.append(f"【书】{book}")
    if chap:
        parts.append(f"【当前章节节选】\n{chap[:3000]}")
    if sel:
        parts.append(f"【用户选中的文字】\n{sel[:2000]}")
    if history:
        h = "\n".join(f"{'用户' if m.get('role')=='user' else '助手'}：{(m.get('content') or '')[:600]}"
                      for m in history[-6:] if m.get('content'))
        if h:
            parts.append(f"【最近对话】\n{h}")
    parts.append(f"【用户问】\n{msg}")
    prompt = "\n\n".join(parts)
    uid = _reader_uid()
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        return _start_ai_stream(prompt, "explain", uid, (body.get("rid") or "").strip())
    try:
        return jsonify({"ok": True, "answer": _ai_call(prompt, "explain", uid).strip()})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI 失败：{ex}"}), 500


@bp.route("/api/explain", methods=["POST"])
def pdf_api_explain():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    ctx = (body.get("context") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "无选中文本"}), 400
    if len(text) > 5000:
        return jsonify({"ok": False, "error": "文本过长（>5000 字）"}), 400
    is_short = len(text) < 30 and "\n" not in text
    if is_short:
        # 短选区（单词/短语）：在句子上下文中解释
        prompt = (
            "下面的【待解释】是用户在阅读教材时选中的一个词或短语。\n"
            "**结合【上下文】**（包含它的整句）解释这个词/短语在此处的含义：\n"
            "1. 给出该词/短语在此语境下的中文意思（不只是字典翻译）\n"
            "2. 解释它在这句话里**起什么作用**、跟相邻概念的关系\n"
            "3. 数学符号要说明记号含义；专有名词要点明指代\n"
            "4. 用 Markdown，公式用 $...$ 或 $$...$$，不要反引号包数学\n"
            "5. 简洁，不要复述整句\n\n"
        )
    else:
        # 长选区（句子/段落）：在更大上下文中解释整段
        prompt = (
            "解释下面【待解释】这段教材内容（来自 PDF 教材，可能含数学公式）。\n"
            "**如果提供了【上下文】**（包含的段落），用它帮助理解但不要重复其中已经清楚的部分。\n"
            "要求：\n"
            "1. 用通俗语言重述，保留专业术语\n"
            "2. 涉及定义/定理时点出关键概念间的关系\n"
            "3. 必要时给一个直观例子或类比\n"
            "4. 用 Markdown，公式用 $...$ 或 $$...$$，不要反引号包数学\n"
            "5. 不要逐字复述，要提炼\n\n"
        )
    if ctx:
        prompt += f"=== 上下文 ===\n{ctx[:3000]}\n\n"
    prompt += f"=== 待解释 ===\n{text}"
    uid = _reader_uid()
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        return _start_ai_stream(prompt, "explain", uid, (body.get("rid") or "").strip())
    try:
        out = _ai_call(prompt, "explain", uid).strip()
        return jsonify({"ok": True, "explanation": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI 解释失败：{ex}"}), 500


# ─── 通用后台 AI job：任务在服务器跑，网页只轮询拉状态 ───
# 防 iPad 切后台/锁屏时浏览器挂起 JS、掐断 fetch/SSE 长连接导致 AI 任务失败。
import threading as _threading
import uuid as _uuid
import time as _time
_JOBS: dict = {}
_JOBS_LOCK = _threading.Lock()

def _job_set(jid: str, **kw):
    with _JOBS_LOCK:
        _JOBS.setdefault(jid, {}).update(kw)
        # 顺手清理 10 分钟前的旧 job（防内存堆积）
        cutoff = _time.time() - 600
        for k in [k for k, v in list(_JOBS.items()) if v.get("ts", 0) < cutoff and k != jid]:
            _JOBS.pop(k, None)


# ─── 抗断连流式 AI：生成跑在后台线程（脱离客户端连接），SSE 只「tail」读取已生成文本 ───
# iPad 切后台/锁屏/网络抖 → SSE 长连接断 → 但后台线程仍把 AI 答案跑完写进 _JOBS[rid]，
# 前端回前台后轮询 /api/ai-stream-result?id=rid 拿完整结果，断点也不丢。rid 为空则退化成原行内流式。
def _aistream_init(rid: str):
    if not rid:
        return
    with _JOBS_LOCK:
        _JOBS[rid] = {"status": "running", "kind": "aistream", "ts": _time.time(), "full": ""}
        cutoff = _time.time() - 600
        for k in [k for k, v in list(_JOBS.items()) if v.get("ts", 0) < cutoff and k != rid]:
            _JOBS.pop(k, None)

def _aistream_update(rid: str, full: str, status: str = "", error: str = ""):
    if not rid:
        return
    with _JOBS_LOCK:
        j = _JOBS.get(rid)
        if j is None:
            j = _JOBS[rid] = {"kind": "aistream"}
        j["full"] = full
        j["ts"] = _time.time()
        if status:
            j["status"] = status
        if error:
            j["error"] = error

def _ai_stream_worker(rid: str, prompt: str, action: str = "explain", uid: str = ""):
    acc = []
    try:
        for chunk in _ai_call_stream(prompt, action, uid):
            if chunk:
                acc.append(chunk)
                _aistream_update(rid, "".join(acc))
        _aistream_update(rid, "".join(acc), status="done")
    except Exception as e:
        _aistream_update(rid, "".join(acc), status="error", error=str(e))

def _start_ai_stream(prompt: str, action: str = "explain", uid: str = "", rid: str = "", on_done=None):
    """启动后台 AI 生成线程 + 返回「tail」SSE Response（边生成边推，且断连后线程继续跑完）。
    rid 为空 → 退化成原行内 _sse_stream（不抗断，兼容老前端）。
    on_done(full_text):生成成功后回调(写服务端缓存等);仅 rid 路径生效。
    ⚠ uid 须在请求上下文里取好传进来(后台线程没有 session,否则只能用出厂默认预设)。"""
    from flask import Response, stream_with_context
    if not rid:
        return Response(stream_with_context(_sse_stream(prompt, action, uid)),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    import json as _json
    _aistream_init(rid)

    def _worker():
        _ai_stream_worker(rid, prompt, action, uid)
        if on_done:
            with _JOBS_LOCK:
                j = _JOBS.get(rid) or {}
            if j.get("status") == "done" and j.get("full"):
                try:
                    on_done(j["full"])
                except Exception:
                    pass

    _threading.Thread(target=_worker, daemon=True).start()

    def tail():
        yield "event: start\ndata: {}\n\n"
        sent = 0
        while True:
            with _JOBS_LOCK:
                j = _JOBS.get(rid) or {}
                full = j.get("full", ""); st = j.get("status"); err = j.get("error", "")
            if len(full) > sent:
                yield f"data: {_json.dumps({'text': full[sent:]}, ensure_ascii=False)}\n\n"
                sent = len(full)
            if st == "done":
                yield "event: done\ndata: {}\n\n"; return
            if st == "error":
                yield f"event: error\ndata: {_json.dumps({'error': err or 'AI 失败'}, ensure_ascii=False)}\n\n"; return
            _time.sleep(0.08)
    return Response(stream_with_context(tail()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _validate_snippets_body(body):
    """校验 snippets-to 入参。返回 (params_dict, None) 或 (None, (errmsg, code))。"""
    snippets = body.get("snippets") or []
    make_note = bool(body.get("make_note"))
    make_anki = bool(body.get("make_anki"))
    note_name = (body.get("note_name") or "").strip()
    if not snippets:
        return None, ("无选中段落", 400)
    if not (make_note or make_anki):
        return None, ("至少选一个动作", 400)
    if make_note and not note_name:
        return None, ("笔记名不能为空", 400)
    return {
        "snippets": snippets, "make_note": make_note, "make_anki": make_anki,
        "note_name": note_name, "action": "explain", "uid": _reader_uid(),   # uid 在请求里取好(异步线程没 session)
    }, None


_ANKI_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
def _anki_md_links(text):
    """Anki 卡按 HTML 渲染:AI 生成的 markdown 链接 [文本](url) 转成可点的 <a href>(否则整串显示成字面文本)。"""
    if not text:
        return text
    return _ANKI_MD_LINK_RE.sub(r'<a href="\2">\1</a>', text)


def _download_image_for_anki(url, timeout=5, max_bytes=10 * 1024 * 1024):
    """下载一张图片准备存进 Anki 媒体库(限时限型限大小,失败静默返回 None,不拖垮制卡流程)。
    返回 (filename, b64data) 或 None。filename 用 url 的 md5 短哈希 + 猜出的扩展名,避免跟已有媒体重名/路径穿越。"""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None
    try:
        import urllib.request as _ureq
        req = _ureq.Request(url, headers={"User-Agent": "bwicarus-claude-assistant/1.0"})
        with _ureq.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return None
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
        ext_map = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                   "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}
        ext = ext_map.get(ctype, "")
        if not ext:
            url_ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
            ext = url_ext if url_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") else ".jpg"
        import hashlib, base64
        fname = "asst-img-" + hashlib.md5(url.encode("utf-8")).hexdigest()[:16] + ext
        return fname, base64.b64encode(data).decode("ascii")
    except Exception:
        return None


def _run_snippets_to(snippets, make_note, make_anki, note_name, action="explain", uid="", image_url=None) -> dict:
    """核心执行（同步/后台线程共用）：AI 整理勾选段落 → 创建笔记 / Anki 卡。返回 out dict。"""
    out = {"ok": True}
    # ── 创建笔记 ──
    if make_note:
        if not OBSIDIAN_ROOT:
            return {"ok": False, "error": "VAULT 未配置"}
        safe = _sanitize_filename(note_name)
        if not safe.endswith(".md"):
            safe += ".md"
        note_path = OBSIDIAN_ROOT / safe
        if note_path.exists():
            stem = safe[:-3]
            for i in range(1, 200):
                cand = OBSIDIAN_ROOT / f"{stem}-{i}.md"
                if not cand.exists():
                    note_path = cand; break
        # AI 整理多段 snippets → 结构化 Markdown
        snippets_text = "\n\n".join([
            f"### 段 {i+1}（来自：{s.get('source','?')}）\n{s.get('text','')}"
            for i, s in enumerate(snippets)
        ])
        prompt = (
            f"请把以下从 AI 回答中收集到的多段内容整理成一篇结构化的 Obsidian Markdown 学习笔记。\n"
            f"笔记主题：{note_name}\n\n"
            "整理要求：\n"
            "1. 各段按主题归类（同主题合并，不同主题用 ## 分节）\n"
            "2. 保留所有实质内容，可改写让连贯但不丢信息\n"
            "3. 数学公式用 $...$ 或 $$...$$，不要反引号包数学\n"
            "4. 直接输出 Markdown 正文，不要前言/代码围栏\n\n"
            f"=== 收集内容 ===\n{snippets_text}"
        )
        try:
            content = _ai_call(prompt, action, uid).strip()
            if content.startswith("```"):
                import re as _re
                content = _re.sub(r'^```[a-zA-Z]*\n', '', content)
                content = _re.sub(r'\n```\s*$', '', content)
            note_path.write_text(content, encoding="utf-8")
            rel = note_path.relative_to(OBSIDIAN_ROOT).as_posix()
            import urllib.parse as _up
            vault_name = os.environ.get("OBSIDIAN_VAULT_NAME", "Obsidian Vault")
            out["note_path"] = rel
            out["obsidian_url"] = (
                f"obsidian://open?vault={_up.quote(vault_name, safe='')}"
                f"&file={_up.quote(rel[:-3] if rel.endswith('.md') else rel, safe='/')}"
            )
        except Exception as ex:
            return {"ok": False, "error": f"笔记创建失败：{ex}"}
    # ── 创建 Anki 卡 ──
    if make_anki:
        try:
            # AI 把 snippets 转 Anki 卡片 JSON
            snippets_text = "\n\n".join([
                f"段 {i+1}：{s.get('text','')}"
                for i, s in enumerate(snippets)
            ])
            prompt = (
                "请把以下学习内容转成 Anki 卡片（问答型 basic 或挖空型 cloze）。\n"
                "输出严格 JSON，无任何额外文字：\n"
                '{"cards": [{"type": "basic", "front": "...", "back": "..."}, '
                '{"type": "cloze", "text": "...{{c1::挖空内容}}..."}, ...]}\n'
                "要求：\n"
                "1. 每个独立知识点 1 张卡，不要堆叠\n"
                "2. front/back 简洁；cloze 一句一空（用 {{c1::xxx}} 不要 {{c1::xxx::hint}}）\n"
                "3. 数学公式 $...$ 或 $$...$$\n\n"
                f"=== 学习内容 ===\n{snippets_text}"
            )
            raw = _ai_call(prompt, action, uid)
            # 提取 JSON
            s_idx = raw.find("{"); e_idx = raw.rfind("}")
            cards_data = json.loads(raw[s_idx:e_idx+1]) if s_idx >= 0 else {"cards": []}
            cards = cards_data.get("cards") or []
            # 通过 AnkiConnect 加入 Anki（deck 用 "QA"）
            import urllib.request
            ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
            def _ank(action, params=None):
                rq = json.dumps({"action": action, "version": 6, "params": params or {}}).encode()
                with urllib.request.urlopen(urllib.request.Request(
                        ANKI_URL, data=rq, headers={"Content-Type": "application/json"}), timeout=10) as rr:
                    return json.loads(rr.read())
            # 本地化 Anki 模型名/字段名可能是中文（「基础的」正面/背面、「填空题」文字/背面额外），动态解析,
            # 否则硬编码 Basic/Cloze + Front/Back 在中文 Anki 上 addNote 全失败（model was not found）
            try:
                _mn = _ank("modelNames").get("result") or []
            except Exception:
                _mn = []
            def _pickm(cands, dflt):
                for cc in cands:
                    if cc in _mn:
                        return cc
                return dflt
            basic_m = _pickm(["Basic", "基础的", "基本"], "Basic")
            cloze_m = _pickm(["Cloze", "填空题", "挖空题"], "Cloze")
            def _mf(m):
                try:
                    return _ank("modelFieldNames", {"modelName": m}).get("result") or []
                except Exception:
                    return []
            _bf, _cf = _mf(basic_m), _mf(cloze_m)
            b_front = _bf[0] if _bf else "Front"
            b_back = _bf[1] if len(_bf) > 1 else (_bf[0] if _bf else "Back")
            c_text = _cf[0] if _cf else "Text"
            try:
                _ank("createDeck", {"deck": "QA"})   # 幂等,确保牌组在(headless addNote 偶尔落系统默认)
            except Exception:
                pass
            # 助手 search_image 找到的图,若一并要求做卡 → 真下载 + 存进 Anki 媒体库(不是外链,以后链接失效卡片也不烂)。
            # 下载/存媒体任一步失败都静默跳过,不影响卡片本身正常生成。
            img_tag = ""
            if image_url:
                try:
                    dl = _download_image_for_anki(image_url)
                    if dl:
                        fname, b64data = dl
                        mres = _ank("storeMediaFile", {"filename": fname, "data": b64data})
                        if not mres.get("error"):
                            img_tag = f'<br><img src="{fname}">'
                except Exception:
                    pass
            added = 0
            note_ids = []
            for idx, c in enumerate(cards):
                ctype = (c.get("type") or "basic").lower()
                if ctype == "cloze":
                    text_val = _anki_md_links(c.get("text", ""))
                    if idx == 0 and img_tag:   # 只贴进本次生成的第一张卡,避免多卡重复贴同一张图
                        text_val += img_tag
                    fields = {c_text: text_val}
                    model_name = cloze_m
                else:
                    back_val = _anki_md_links(c.get("back", ""))
                    if idx == 0 and img_tag:
                        back_val += img_tag
                    fields = {b_front: _anki_md_links(c.get("front", "")), b_back: back_val}
                    model_name = basic_m
                req = json.dumps({
                    "action": "addNote", "version": 6,
                    "params": {"note": {
                        "deckName": "QA",
                        "modelName": model_name,
                        "fields": fields,
                        "tags": ["pdf-snippets"],
                    }}
                }).encode()
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(ANKI_URL, data=req,
                                                headers={"Content-Type":"application/json"}),
                        timeout=10) as r:
                        resp = json.loads(r.read())
                        if not resp.get("error"):
                            added += 1
                            if resp.get("result"):
                                note_ids.append(resp["result"])
                except Exception:
                    pass
            out["anki_added"] = added
            out["anki_note_ids"] = note_ids   # 供撤销:deleteNotes
            # 制完触发 AnkiWeb sync（fire-and-forget，~50ms 返回，后台推送）
            if added > 0:
                try:
                    sreq = json.dumps({"action": "sync", "version": 6}).encode()
                    urllib.request.urlopen(
                        urllib.request.Request(ANKI_URL, data=sreq,
                                                headers={"Content-Type": "application/json"}),
                        timeout=5).read()
                except Exception:
                    pass
        except Exception as ex:
            out["anki_error"] = str(ex)
    return out


@bp.route("/api/snippets-to", methods=["POST"])
def pdf_api_snippets_to():
    """同步版（兼容）：勾选段落 → 笔记/Anki。"""
    body = request.get_json(silent=True) or {}
    params, err = _validate_snippets_body(body)
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]
    out = _run_snippets_to(**params)
    return jsonify(out), (200 if out.get("ok") else 500)


@bp.route("/api/snippets-to-async", methods=["POST"])
def pdf_api_snippets_to_async():
    """异步版：服务器后台线程跑，立即返回 job_id；网页轮询 /api/job-status。
    防 iPad 切后台/锁屏时浏览器掐断长请求导致 AI 任务失败。"""
    body = request.get_json(silent=True) or {}
    params, err = _validate_snippets_body(body)
    if err:
        return jsonify({"ok": False, "error": err[0]}), err[1]
    jid = _uuid.uuid4().hex[:12]
    _job_set(jid, status="running", ts=_time.time(), kind="snippets-to")
    def _work():
        try:
            out = _run_snippets_to(**params)
            _job_set(jid, status="done", result=out, ts=_time.time())
        except Exception as ex:
            _job_set(jid, status="error", error=str(ex), ts=_time.time())
    _threading.Thread(target=_work, daemon=True).start()
    return jsonify({"ok": True, "job_id": jid})


@bp.route("/api/job-status")
def pdf_api_job_status():
    """轮询后台 job：{status: running|done|error|unknown, result?, error?}。"""
    jid = request.args.get("id", "")
    with _JOBS_LOCK:
        j = dict(_JOBS.get(jid) or {})
    return jsonify(j if j else {"status": "unknown"})


@bp.route("/api/ai-stream-result")
def pdf_api_ai_stream_result():
    """抗断连流式 AI 的结果轮询：SSE 断了（切后台/网抖）前端轮询这里，拿后台线程已生成的完整文本。
    {status: running|done|error|unknown, full, error}。"""
    rid = request.args.get("id", "")
    with _JOBS_LOCK:
        j = dict(_JOBS.get(rid) or {})
    if not j:
        return jsonify({"status": "unknown"})
    return jsonify({"status": j.get("status", "running"),
                    "full": j.get("full", ""), "error": j.get("error", "")})


# ─── 手写墨迹（sidecar JSON 存 state/pdf-ink/<sha1>.json，按页存归一化笔画）──────

_INK_DIR = CLAUDE_DIR / "state" / "pdf-ink"

def _ink_path(rel: str) -> Path:
    import hashlib
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    _INK_DIR.mkdir(parents=True, exist_ok=True)
    return _INK_DIR / f"{sha}.json"

def _ink_load(rel: str) -> dict:
    p = _ink_path(rel)
    if not p.exists():
        return {"pdf_rel": rel, "pages": {}}
    try:
        data = json.loads(p.read_text("utf-8"))
        if not isinstance(data.get("pages"), dict):
            data["pages"] = {}
        data["pdf_rel"] = rel
        return data
    except Exception:
        return {"pdf_rel": rel, "pages": {}}

def _ink_save(rel: str, data: dict):
    p = _ink_path(rel)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(p)


@bp.route("/api/ink", methods=["GET"])
def pdf_api_ink_list():
    """GET ?file=<rel> → {ok, pages:{"<page>":[stroke,...]}}。
    stroke = {t:'pen'|'line'|'arrow'|'rect', c, w, p:[[x,y],...]}，坐标归一化 0-1。"""
    rel = request.args.get("file", "")
    if not rel or _safe_vault_path(rel) is None:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    r = jsonify({"ok": True, **_ink_load(rel)})
    r.headers["Cache-Control"] = "no-store"   # 实时同步的读源(SSE 触发重拉):iOS Safari 缓存旧响应会把画面回退到陈旧墨迹
    return r


@bp.route("/api/ink", methods=["POST"])
def pdf_api_ink_save():
    """POST {file, page, strokes:[...]} → 整页替换该页墨迹（strokes 空则删该页）。"""
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    if not rel or _safe_vault_path(rel) is None:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    try:
        page = int(data.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    if page <= 0:
        return jsonify({"ok": False, "error": "invalid page"}), 400
    strokes = data.get("strokes")
    if not isinstance(strokes, list):
        return jsonify({"ok": False, "error": "invalid strokes"}), 400
    if len(strokes) > 5000:
        return jsonify({"ok": False, "error": "too many strokes"}), 400
    doc = _ink_load(rel)
    if strokes:
        doc["pages"][str(page)] = strokes
    else:
        doc["pages"].pop(str(page), None)
    _ink_save(rel, doc)
    try:
        _reader_publish("ink", rel, str(page))   # 推给其它打开着的视图(PDF 阅读器/收藏夹):同一张纸,~1s 同步
    except Exception:
        pass
    return jsonify({"ok": True, "count": len(strokes)})


# ─── 高亮（sidecar JSON 存 state/pdf-highlights/<sha1>.json）───────────────────

_HL_DIR = CLAUDE_DIR / "state" / "pdf-highlights"
_HL_LOCK_TIMEOUT = 5  # 秒，简单文件锁

def _hl_path(rel: str) -> Path:
    import hashlib
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    _HL_DIR.mkdir(parents=True, exist_ok=True)
    return _HL_DIR / f"{sha}.json"

def _hl_load(rel: str) -> dict:
    p = _hl_path(rel)
    if not p.exists():
        return {"pdf_rel": rel, "highlights": []}
    try:
        data = json.loads(p.read_text("utf-8"))
        if not isinstance(data.get("highlights"), list):
            data["highlights"] = []
        data["pdf_rel"] = rel
        return data
    except Exception:
        return {"pdf_rel": rel, "highlights": []}

def _hl_save(rel: str, data: dict):
    p = _hl_path(rel)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)


# ── 页级图注(state/pdf-figures/<book-sha>.json):视觉模型(Claude)描述扫描书插图 ──
# 懒加载+预取:阅读器打开某页 → 描述该页(+后几页)插图,缓存;供阅读器图区徽标/全文搜索/助手用。
# ⚠ sidecar key 用 _book_sha(abspath)(= describe_figures.pdf_sha),跟 describe_figures.py 批量脚本互通。
_FIG_DIR = CLAUDE_DIR / "state" / "pdf-figures"
_fig_lock = _threading.Lock()
_fig_inflight = set()   # 正在后台描述的 (sha,page),去重

def _fig_path_abs(abs_path) -> Path:
    _FIG_DIR.mkdir(parents=True, exist_ok=True)
    return _FIG_DIR / f"{_book_sha(abs_path)}.json"

def _fig_load_abs(abs_path) -> dict:
    p = _fig_path_abs(abs_path)
    try:
        cur_mt = int(os.path.getmtime(str(abs_path)))
    except Exception:
        cur_mt = 0
    if not p.exists():
        return {"pdf": str(abs_path), "book_mtime": cur_mt, "figures": [], "_none_pages": []}
    try:
        d = json.loads(p.read_text("utf-8"))
        d.setdefault("figures", []); d.setdefault("_none_pages", [])
        # 书变了(重 OCR/重嵌/替换 → mtime 变)→ 旧图注可能过期 → 清空,懒重描
        if cur_mt and d.get("book_mtime") and d["book_mtime"] != cur_mt:
            d["figures"] = []; d["_none_pages"] = []
        d["book_mtime"] = cur_mt
        return d
    except Exception:
        return {"pdf": str(abs_path), "book_mtime": cur_mt, "figures": [], "_none_pages": []}

def _fig_save_abs(abs_path, data: dict):
    p = _fig_path_abs(abs_path)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)

def _fig_done_page(data: dict, page: int) -> bool:
    return any(f.get("page") == page for f in data.get("figures", [])) or page in set(data.get("_none_pages", []))


# ─── 选区 OCR 校正 sidecar：坏文字层的『选区重新识别』结果持久化，注入字符层永久生效 ───
_OCRFIX_DIR = CLAUDE_DIR / "state" / "pdf-ocr-fix"
_OCRFIX_INJECT_VER = 7   # 注入逻辑版本(改注入规则就 +1 → cv 变 → 旧缓存失效)


def _ocrfix_path_abs(abs_path) -> Path:
    _OCRFIX_DIR.mkdir(parents=True, exist_ok=True)
    return _OCRFIX_DIR / f"{_book_sha(abs_path)}.json"


def _ocrfix_load_abs(abs_path) -> dict:
    p = _ocrfix_path_abs(abs_path)
    try:
        cur_mt = int(os.path.getmtime(str(abs_path)))
    except Exception:
        cur_mt = 0
    if not p.exists():
        return {"pdf": str(abs_path), "book_mtime": cur_mt, "fixes": []}
    try:
        d = json.loads(p.read_text("utf-8"))
        d.setdefault("fixes", [])
        if cur_mt and d.get("book_mtime") and d["book_mtime"] != cur_mt:
            d["fixes"] = []   # 书重建(mtime 变)→ 旧校正坐标可能失效 → 清空
        d["book_mtime"] = cur_mt
        return d
    except Exception:
        return {"pdf": str(abs_path), "book_mtime": cur_mt, "fixes": []}


def _ocrfix_save_abs(abs_path, data: dict):
    p = _ocrfix_path_abs(abs_path)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def _ocrfix_add(abs_path, page: int, bbox_norm, text: str):
    """加一条校正(归一化 bbox)。同页『重叠过半』的旧校正替换掉(重 OCR 同一处=覆盖,不堆叠)。"""
    data = _ocrfix_load_abs(abs_path)
    nx0, ny0, nx1, ny1 = bbox_norm

    def _overlap(f):
        if f.get("page") != page:
            return False
        b = f.get("bbox") or [0, 0, 0, 0]
        ix0, iy0, ix1, iy1 = max(nx0, b[0]), max(ny0, b[1]), min(nx1, b[2]), min(ny1, b[3])
        if ix1 <= ix0 or iy1 <= iy0:
            return False
        inter = (ix1 - ix0) * (iy1 - iy0)
        a1 = (nx1 - nx0) * (ny1 - ny0); a2 = (b[2] - b[0]) * (b[3] - b[1])
        return inter > 0.5 * max(1e-9, min(a1, a2))

    data["fixes"] = [f for f in data["fixes"] if not _overlap(f)]
    data["fixes"].append({"page": int(page), "bbox": [round(float(v), 4) for v in bbox_norm],
                          "text": text, "ts": int(time.time())})
    if len(data["fixes"]) > 2000:
        data["fixes"] = data["fixes"][-2000:]
    _ocrfix_save_abs(abs_path, data)


def _ocr_token_ids(txt):
    """给注入文字按 token 切分 → 每 token 一个相对编号(同 token 同号),让校正文字能**分开点/选**:
    $...$/$$...$$ 数学整块算一个 token、连续 ASCII 字母数字(如 cm/Å 旁的词)算一个、其余每个字符(中日文/标点)各自一个。
    返回 len==len(txt) 的编号列表。"""
    ids = [0] * len(txt); t = 0; i = 0; n = len(txt)
    while i < n:
        ch = txt[i]
        if ch == "$":                                   # 数学跨度整块一个 token
            two = i + 1 < n and txt[i + 1] == "$"
            close = "$$" if two else "$"
            k = txt.find(close, i + (2 if two else 1))
            end = (k + len(close)) if k >= 0 else n
            for p in range(i, end):
                ids[p] = t
            t += 1; i = end; continue
        if ch.isascii() and ch.isalnum():               # 连续 ASCII 词(cm/10 等)一个 token
            j = i
            while j < n and txt[j].isascii() and txt[j].isalnum():
                j += 1
            for p in range(i, j):
                ids[p] = t
            t += 1; i = j; continue
        ids[i] = t; t += 1; i += 1                       # 中日文字/标点/符号 → 各自一个 token
    return ids


def _ocr_align_positions(txt, orig, fx0, fy0, fx1, fy1):
    """把校正文字 txt 对齐到原字形真实位置:**LCS 找 txt 跟原字形串的公共子序列当锚点**
    (相同字 = 原字形坏文字层里也对、位置也对),锚点用原字形精确 x/y;不同的字(数学/Å/LaTeX 标记)
    在相邻锚点间按 index 线性插值。返回每个 txt 字符的 (x0,y0,x1,y1)。
    这是用户的思路:相同字符作对照,不同字符的 OCR 结果放中间。"""
    N = len(txt); M = len(orig)
    if M == 0 or N == 0:
        sw = (fx1 - fx0) / max(1, N)
        return [(fx0 + i * sw, fy0, fx0 + (i + 1) * sw, fy1) for i in range(N)]
    o = [c["c"] for c in orig]
    # LCS DP(自底向上)
    dp = [[0] * (M + 1) for _ in range(N + 1)]
    for i in range(N - 1, -1, -1):
        di, di1 = dp[i], dp[i + 1]
        ti = txt[i]
        for j in range(M - 1, -1, -1):
            di[j] = di1[j + 1] + 1 if ti == o[j] else (di1[j] if di1[j] >= di[j + 1] else di[j + 1])
    # 回溯出锚点 (txt 下标, x 中心, y0, y1)
    anc = []
    i = j = 0
    while i < N and j < M:
        if txt[i] == o[j]:
            anc.append((i, (orig[j]["x0"] + orig[j]["x1"]) / 2.0, orig[j]["y0"], orig[j]["y1"]))
            i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    if not anc:                                  # 无公共字 → 退回 bbox 内均匀
        sw = (fx1 - fx0) / N
        return [(fx0 + k * sw, fy0, fx0 + (k + 1) * sw, fy1) for k in range(N)]
    # 控制点 = 两端(bbox 边,y 借最近锚点)+ 各锚点;每个 txt 字符的中心 x 按 index 在控制点间线性插值
    ctrl = [(-1.0, fx0, anc[0][2], anc[0][3])] + [(float(a[0]), a[1], a[2], a[3]) for a in anc] \
        + [(float(N), fx1, anc[-1][2], anc[-1][3])]
    cen = []
    p = 0
    for k in range(N):
        while p + 1 < len(ctrl) - 1 and ctrl[p + 1][0] <= k:
            p += 1
        a, b = ctrl[p], ctrl[p + 1]
        fr = 0.0 if b[0] == a[0] else (k - a[0]) / (b[0] - a[0])
        fr = min(1.0, max(0.0, fr))
        cx = a[1] * (1 - fr) + b[1] * fr
        gy0, gy1 = (a[2], a[3]) if fr < 0.5 else (b[2], b[3])
        cen.append((cx, gy0, gy1))
    # 中心 x → [x0,x1]:取到左右邻居中心的中点,得连续不重叠的格子
    out = []
    for k in range(N):
        cx, gy0, gy1 = cen[k]
        lx = cen[k - 1][0] if k > 0 else fx0
        rx = cen[k + 1][0] if k < N - 1 else fx1
        x0 = (lx + cx) / 2.0; x1 = (cx + rx) / 2.0
        if x1 - x0 < 0.2:
            x1 = x0 + 0.2
        out.append((round(x0, 2), round(gy0, 2), round(x1, 2), round(gy1, 2)))
    return out


def _apply_ocr_corrections(chars, furigana, rel, page, page_w, page_h):
    """把『选区 OCR 校正』的正确文字注入字符层(坏文字层永久修正)。
    同 _apply_formula_chars:删掉校正 bbox 内的原坏字符 + 框内振假名,塞入校正文字(平铺满框宽、标 ocrfix=1)。
    词 id **按 token 分**(不是整块一个)→ 校正文字可分开点/选;cv 已含本 sidecar mtime,改了前端缓存自动失效。"""
    try:
        abs_path = _safe_vault_path(rel)
        if not abs_path:
            return
        data = _ocrfix_load_abs(abs_path)
    except Exception:
        return
    fixes = [f for f in (data.get("fixes") or [])
             if f.get("page") == page and (f.get("text") or "").strip()
             and f.get("bbox") and len(f.get("bbox")) == 4]
    if not fixes:
        return
    WID, BK = 960000000, 960000
    for fi, f in enumerate(fixes):
        x0n, y0n, x1n, y1n = f["bbox"]
        fx0, fy0, fx1, fy1 = x0n * page_w, y0n * page_h, x1n * page_w, y1n * page_h

        def _inside(bx0, by0, bx1, by1):
            cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
            return fx0 <= cx <= fx1 and fy0 <= cy <= fy1
        # ★关键:删原字符前先**捕获它们的真实坐标**。坏文字层只是 ToUnicode 把字符『值』映射错了,
        #   bbox『位置』是对的(PDF 渲染就靠它)。把校正文字按真实字形位置注入 → 选中框/点选跟图对齐。
        # 排序按**行聚类**(y 中心差 >7pt 才换行;上标 `⁻⁸` y 抬高但 <7 仍算同行,否则会被分到别桶把 x 序打乱)
        # + 行内按 x;得到正确阅读序(单行=单调 x)。
        ins = [c for c in chars if _inside(c["x0"], c["y0"], c["x1"], c["y1"])]
        ins.sort(key=lambda c: ((c["y0"] + c["y1"]) / 2.0, c["x0"]))
        orig, _line, _lasty = [], [], None
        for c in ins:
            yc = (c["y0"] + c["y1"]) / 2.0
            if _lasty is not None and yc - _lasty > 7.0:
                _line.sort(key=lambda c: c["x0"]); orig.extend(_line); _line = []
            _line.append(c); _lasty = yc
        if _line:
            _line.sort(key=lambda c: c["x0"]); orig.extend(_line)
        chars[:] = [c for c in chars if not _inside(c["x0"], c["y0"], c["x1"], c["y1"])]
        if isinstance(furigana, list) and furigana:
            furigana[:] = [r for r in furigana
                           if not (all(k in r for k in ("x0", "y0", "x1", "y1"))
                                   and _inside(r["x0"], r["y0"], r["x1"], r["y1"]))]
        txt = f["text"].strip()
        N = len(txt)
        if not N:
            continue
        tok = _ocr_token_ids(txt)            # 每字符所属 token 号 → 词 id 按 token 分,可分开点/选
        wbase, bk = WID + fi * 100000, BK + fi
        pos = _ocr_align_positions(txt, orig, fx0, fy0, fx1, fy1)   # LCS 锚点对齐:相同字用原字形精确位置,不同字插值
        for i, cc in enumerate(txt):
            px0, py0, px1, py1 = pos[i]
            chars.append({
                "c": cc, "x0": px0, "y0": py0, "x1": px1, "y1": py1,
                "sp": 1 if cc.isspace() else 0, "w": wbase + tok[i], "b": 0, "bk": bk, "ocrfix": 1,
            })

def _fig_describe_bg(abs_path, page: int, model: str = "sonnet", prefetch: int = 2):
    """后台:描述本页(+预取后 prefetch 页)插图,写 sidecar。in-flight 去重,失败不记(下次重试)。"""
    import sys as _sys
    sp = str(CLAUDE_DIR / "scripts")
    if sp not in _sys.path:
        _sys.path.insert(0, sp)
    try:
        import describe_figures as DF
        import fitz
        npages = fitz.open(str(abs_path)).page_count
    except Exception:
        return
    sha = _book_sha(abs_path)
    for pg in range(page, min(npages, page + prefetch) + 1):
        if pg < 1:
            continue
        key = (sha, pg)
        with _fig_lock:
            data = _fig_load_abs(abs_path)
            if _fig_done_page(data, pg) or key in _fig_inflight:
                continue
            _fig_inflight.add(key)
        try:
            # provenance:这页(印刷页=PDF页-offset)所属章节,喂给描述让 AI 把图放进语境
            _rel = ""
            try:
                _rel = abs_path.relative_to(OBSIDIAN_ROOT.resolve()).as_posix()
            except Exception:
                pass
            _printed = pg - _page_offset_for(_rel)
            _sec = _book_location(abs_path, _printed, _rel)
            _bn = Path(abs_path).stem
            _loc = (f"《{_bn}》" + (f"「{_sec}」" if _sec else "")) if _bn else _sec
            figs = DF.describe_page_figures(str(abs_path), pg - 1, model, location=_loc)
        except Exception:
            figs = None
        with _fig_lock:
            _fig_inflight.discard(key)
            if figs is None:
                continue
            data = _fig_load_abs(abs_path)
            data["figures"] = [f for f in data["figures"] if f.get("page") != pg]
            if figs:
                for f in figs:
                    data["figures"].append({"page": pg, "caption": f.get("caption", ""),
                                            "bbox": f.get("bbox"), "desc": f.get("desc", "")})
            elif pg not in data["_none_pages"]:
                data["_none_pages"].append(pg)
            _fig_save_abs(abs_path, data)

def _fig_refine_bbox(page, bbox, scale=2.0, pad=1.5, dark=205):
    """把 AI 给的(常偏大、上含正文/下含图题)插图 bbox 收紧到**真实墨迹范围**(= 图本身)。
    做法:渲染该区域灰度图 → 二值化取墨迹 → 用**精确文字框** page.get_text('words') 抹掉文字层
    → 中值滤波去椒盐噪(保留点簇/分子小圆) → getbbox 求剩余墨迹外接框。归一化并夹在原 bbox 内。
    这样原本溢到正文/图题里的边会被拉回到图的真实边界,徽标几何(A/B)也随之落到正确位置。
    失败 / 退化(区域几乎全是文字或空白)返回 None,调用方回退到原 bbox。不依赖 AI。"""
    try:
        import fitz
        from PIL import Image, ImageDraw, ImageFilter
        pr = page.rect; W = float(pr.width); H = float(pr.height)
        if W <= 0 or H <= 0:
            return None
        fx0, fy0, fx1, fy1 = [max(0.0, min(1.0, float(v))) for v in bbox[:4]]
        if fx1 - fx0 < 1e-3 or fy1 - fy0 < 1e-3:
            return None
        px0, py0, px1, py1 = fx0 * W, fy0 * H, fx1 * W, fy1 * H
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                              clip=fitz.Rect(px0, py0, px1, py1),
                              colorspace=fitz.csGRAY, alpha=False)
        if pix.width < 3 or pix.height < 3:
            return None
        ink = Image.frombytes("L", (pix.width, pix.height), pix.samples).point(
            lambda p: 255 if p < dark else 0)            # 白=墨迹,黑=底
        d = ImageDraw.Draw(ink)
        for wd in page.get_text("words"):                # 抹掉文字层(精确框,略外扩 pad)
            x0 = (wd[0] - pad - px0) * scale; y0 = (wd[1] - pad - py0) * scale
            x1 = (wd[2] + pad - px0) * scale; y1 = (wd[3] + pad - py0) * scale
            d.rectangle([x0, y0, x1, y1], fill=0)
        ink = ink.filter(ImageFilter.MedianFilter(3))    # 去椒盐,保留分子小圆/点簇
        bb = ink.getbbox()
        if not bb:
            return None
        cx0, cy0, cx1, cy1 = bb
        nx0 = (px0 + cx0 / scale) / W; ny0 = (py0 + cy0 / scale) / H
        nx1 = (px0 + cx1 / scale) / W; ny1 = (py0 + cy1 / scale) / H
        nx0 = max(fx0, min(fx1, nx0)); nx1 = max(fx0, min(fx1, nx1))   # 夹在原 bbox 内
        ny0 = max(fy0, min(fy1, ny0)); ny1 = max(fy0, min(fy1, ny1))
        if nx1 - nx0 < 0.01 or ny1 - ny0 < 0.01:         # 退化(几乎全文字/空白)→ 回退原 bbox
            return None
        return [round(nx0, 4), round(ny0, 4), round(nx1, 4), round(ny1, 4)]
    except Exception:
        return None


def _fig_badge_topright(fbox):
    """徽标中心 = 图框右上角顶点(中心与右上角重叠)。归一。与 yolo_figures.badge_topright 同口径。"""
    try:
        x0, y0, x1, y1 = [float(v) for v in fbox[:4]]
        return [round(max(0.005, min(0.995, x1)), 4), round(max(0.005, min(0.995, y0)), 4)]
    except Exception:
        return None


def _draw_ink(im, strokes, mp, scale):
    """共享画笔循环:把 strokes 逐笔画到 PIL 图 im 上,每点经 mp(point)->(px,py) 映射到图像素;scale=线宽放大(w*scale)。
    三处合成(figure 裁图 _figure_crop_png / epub 图 _epub_figure_ink_png / 整页 _overlay_ink_on_page_png)共用同一套 pen/line/arrow/rect 绘制。
    坐标差异全在 mp 里,本函数只管画。任何单笔异常都跳过,绝不让一笔画崩整图。"""
    import math
    from PIL import ImageDraw
    d = ImageDraw.Draw(im); cw, ch = im.width, im.height
    for s in (strokes or []):
        try:
            pts = s.get("p") or []
            if not pts: continue
            col = s.get("c") or "#e74c3c"
            if isinstance(col, str) and col.startswith("rgb"): col = "#e74c3c"
            w = max(1, int(round((s.get("w") or 2.5) * scale)))
            t = s.get("t") or "pen"
            cp = [mp(p) for p in pts]
            if all(px < -5 or px > cw + 5 or py < -5 or py > ch + 5 for px, py in cp):
                continue
            if t == "rect" and len(cp) >= 2:
                d.rectangle([min(cp[0][0], cp[1][0]), min(cp[0][1], cp[1][1]),
                             max(cp[0][0], cp[1][0]), max(cp[0][1], cp[1][1])], outline=col, width=w)
            elif t == "arrow" and len(cp) >= 2:
                d.line([cp[0], cp[1]], fill=col, width=w)
                ang = math.atan2(cp[1][1] - cp[0][1], cp[1][0] - cp[0][0]); ah = max(8, w * 3)
                for sgn in (-0.42, 0.42):
                    d.line([cp[1], (cp[1][0] - ah * math.cos(ang + sgn), cp[1][1] - ah * math.sin(ang + sgn))], fill=col, width=w)
            elif len(cp) >= 2:
                d.line(cp, fill=col, width=w, joint="curve")
            else:
                px, py = cp[0]; d.ellipse([px - w, py - w, px + w, py + w], fill=col)
        except Exception:
            continue
    return im


def resolve_figure_image(ref, ink=None):
    """统一 Figure 图像解析入口(中间层设计 §8 步骤4):按 ref.kind 分发到各格式的合成函数。
    加一种阅读格式 = 加一个 kind 分支,助手/see_figure 只透传 opaque ref、不 branch。
    ref 至少含 kind + path(调用方已解析好的图/PDF 绝对路径)+ 该格式定位字段;ink=手写墨迹(坐标口径由各分支既有函数处理)。"""
    kind = (ref or {}).get("kind")
    try:
        if kind == "pdf":
            has_ink = bool(ink) or bool(ref.get("has_ink"))   # ink 空但标了 has_ink → _figure_crop_png 回退读服务端 ink sidecar(保留原行为)
            return _figure_crop_png(ref.get("path"), ref.get("page"), ref.get("box"),
                                    with_ink=has_ink, rel=ref.get("rel"), strokes=ink)
        if kind == "epub":
            return _epub_figure_ink_png(ref.get("path"), ref.get("imgbox"), ink, ref.get("imgsw"))
    except Exception:
        return None
    return None


def _figure_crop_png(abs_path, page, box, scale=2.4, with_ink=False, rel=None, strokes=None):
    """裁出图框(归一 box)区域的 PNG。with_ink → 叠加手写笔迹合成(给助手看/拖拽 ghost)。
    笔迹来源:优先用**传入的 strokes**(客户端随图带来的当前笔迹,不依赖服务端保存时机);
    没传则回退读服务端 ink sidecar(_ink_load)。"""
    import io
    import fitz
    from PIL import Image, ImageDraw
    doc = fitz.open(str(abs_path))
    try:
        pg = doc[int(page) - 1]; pr = pg.rect; W = float(pr.width); H = float(pr.height)
        x0, y0, x1, y1 = [max(0.0, min(1.0, float(v))) for v in box[:4]]
        if x1 - x0 < 1e-3 or y1 - y0 < 1e-3:
            x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale),
                            clip=fitz.Rect(x0 * W, y0 * H, x1 * W, y1 * H), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()
    if with_ink:
        try:
            if strokes is None:
                strokes = (_ink_load(rel).get("pages") or {}).get(str(int(page))) or [] if rel else []
            if strokes:
                cw, ch = im.width, im.height
                bw = (x1 - x0) or 1e-6; bh = (y1 - y0) or 1e-6
                def mp(p): return ((p[0] - x0) / bw * cw, (p[1] - y0) / bh * ch)   # 页归一 → 裁图内像素
                _draw_ink(im, strokes, mp, scale)
        except Exception:
            pass
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def _epub_figure_ink_png(img_path, imgbox, strokes, sw=None):
    """EPUB 版 see_figure「图 + 手写墨迹」合成(照搬 _figure_crop_png with_ink 的绘制循环)。
    EPUB 图是完整图片文件、无需裁 PDF 页;墨迹是**相对 .ep-sec 章内容盒**的归一化 0-1,
    imgbox=[ix0,iy0,ix1,iy1] 是该 <img> 在章内的归一化矩形(前端 getBoundingClientRect 量)。
    换算:章坐标先减 imgbox 原点、除 imgbox 宽高 → 图内 0-1 → ×图像素。
    sw=该图在屏幕上的 CSS 宽度 px(前端传),线宽放大 = 图自然宽/屏幕宽;不传回退 2.0。异常回退原图字节。"""
    import io
    import math
    from PIL import Image, ImageDraw
    im = Image.open(str(img_path)).convert("RGB")
    cw, ch = im.width, im.height
    try:
        ix0, iy0, ix1, iy1 = [float(v) for v in imgbox[:4]]
    except Exception:
        ix0, iy0, ix1, iy1 = 0.0, 0.0, 1.0, 1.0
    bw = (ix1 - ix0) or 1e-6; bh = (iy1 - iy0) or 1e-6
    try:
        wscale = (cw / float(sw)) if (sw and float(sw) > 0) else 2.0
    except Exception:
        wscale = 2.0
    def mp(p): return ((p[0] - ix0) / bw * cw, (p[1] - iy0) / bh * ch)   # 章归一 → 图内像素
    _draw_ink(im, strokes, mp, wscale)
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def _overlay_ink_on_page_png(png_bytes, strokes, scale=2.0):
    """把整页归一化笔画(0-1)叠加到**已渲染的整页 PNG** 上,返回新 PNG bytes。
    给助手 see_page 用:页面有手写批注时发『页面+手写』合成图。画线逻辑与 _figure_crop_png 一致,
    只是按全页映射(box=(0,0,1,1),mp(p)=(x*cw,y*ch));scale=整页渲染倍率(决定线宽,跟 w*matrix_scale 同口径)。
    任何异常都回退原图,绝不让『叠墨迹失败』把『看页面』整个搞挂。"""
    if not strokes:
        return png_bytes
    try:
        import io, math
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        cw, ch = im.width, im.height
        d = ImageDraw.Draw(im)
        def mp(p): return (p[0] * cw, p[1] * ch)
        for s in strokes:
            try:
                pts = s.get("p") or []
                if not pts:
                    continue
                col = s.get("c") or "#e74c3c"
                if isinstance(col, str) and col.startswith("rgb"):
                    col = "#e74c3c"
                w = max(1, int(round((s.get("w") or 2.5) * scale)))
                t = s.get("t") or "pen"
                cp = [mp(p) for p in pts]
                if t == "rect" and len(cp) >= 2:
                    d.rectangle([min(cp[0][0], cp[1][0]), min(cp[0][1], cp[1][1]),
                                 max(cp[0][0], cp[1][0]), max(cp[0][1], cp[1][1])], outline=col, width=w)
                elif t == "arrow" and len(cp) >= 2:
                    d.line([cp[0], cp[1]], fill=col, width=w)
                    ang = math.atan2(cp[1][1] - cp[0][1], cp[1][0] - cp[0][0]); ah = max(8, w * 3)
                    for sgn in (-0.42, 0.42):
                        d.line([cp[1], (cp[1][0] - ah * math.cos(ang + sgn), cp[1][1] - ah * math.sin(ang + sgn))], fill=col, width=w)
                elif len(cp) >= 2:
                    d.line(cp, fill=col, width=w, joint="curve")
                else:
                    px, py = cp[0]; d.ellipse([px - w, py - w, px + w, py + w], fill=col)
            except Exception:
                continue
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()
    except Exception:
        return png_bytes


def _page_ink_strokes(rel, page):
    """读服务端 ink sidecar 里某 PDF 页(1-based)的笔画列表;无则空 list。给 see_page / 系统 prompt 探测用。"""
    try:
        return (_ink_load(rel).get("pages") or {}).get(str(int(page))) or []
    except Exception:
        return []


def _text_under_ink(rel, page, strokes=None):
    """检测某页**墨迹圈住/划下的文字**,按阅读序返回(给助手当焦点:用户用笔圈了啥就问啥)。
    圈/方框 → 框内的字;下划线/横线(扁笔画)→ 线正上方一行内的字。墨迹归一化坐标 → ×页宽高转 PDF pt 跟字符层同系。
    strokes 传了用传入的(前端内存实时墨迹,不依赖服务端保存时机);没传则读 sidecar。"""
    try:
        if strokes is None:
            strokes = _page_ink_strokes(rel, page)
        if not strokes:
            return ""
        abs_path = _safe_vault_path(rel)
        if not abs_path:
            return ""
        res = _page_chars_cached(abs_path, rel, page)
        if not res:
            return ""
        chars, pw, ph = res[0], res[1], res[2]
    except Exception:
        return ""
    picked = {}
    for s in strokes:
        pts = s.get("p") or []
        if not pts:
            continue
        xs = [p[0] * pw for p in pts]; ys = [p[1] * ph for p in pts]
        bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
        h = by1 - by0; w = bx1 - bx0
        flat = h < max(6.0, w * 0.18)          # 又扁又宽 → 当下划线/横线(文字在它上方)
        for i, c in enumerate(chars):
            if c.get("sp"):
                continue
            cx = (c["x0"] + c["x1"]) / 2; cy = (c["y0"] + c["y1"]) / 2
            chh = (c["y1"] - c["y0"]) or 10.0
            inside = (bx0 - 2 <= cx <= bx1 + 2) and (by0 - 2 <= cy <= by1 + 2)
            under = flat and (bx0 - 2 <= cx <= bx1 + 2) and (by0 - chh * 1.3 <= cy <= by0 + chh * 0.3)
            if inside or under:
                picked[i] = c["c"]
    if not picked:
        return ""
    return "".join(picked[i] for i in sorted(picked))[:200]   # i = reading order(chars 已按阅读序)


def _ink_focus_image(rel, page, strokes, scale=2.6):
    """裁出**笔迹附近区域**(笔迹外接框 + 留白带上下文)并把笔迹叠加上去 → PNG。
    给助手:用户笔迹不一定是圈/线(箭头/勾/波浪/随手涂都行),直接看『原文+笔迹』合成图最稳。返回 PNG bytes 或 None。"""
    try:
        abs_path = _safe_vault_path(rel)
        if not abs_path or not strokes:
            return None
        xs, ys = [], []
        for s in strokes:
            for p in (s.get("p") or []):
                if len(p) >= 2:
                    xs.append(float(p[0])); ys.append(float(p[1]))
        if not xs:
            return None
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        bw = x1 - x0; bh = y1 - y0
        padx = max(0.07, bw * 0.45)            # 横向多留点(把整行/邻词带进来)
        pady = max(0.05, bh * 0.7)             # 纵向带上下文行
        box = [max(0.0, x0 - padx), max(0.0, y0 - pady), min(1.0, x1 + padx), min(1.0, y1 + pady)]
        png = _figure_crop_png(abs_path, page, box, scale=scale, with_ink=True, rel=rel, strokes=strokes)
        if png and len(png) > 3_000_000:       # 太大降一档(防喂回 stdin 过大)
            png = _figure_crop_png(abs_path, page, box, scale=scale * 0.6, with_ink=True, rel=rel, strokes=strokes)
        return png
    except Exception:
        return None


def _claude_bin():
    """稳健解析 claude CLI 路径:env APP_CLAUDE > which > 常见安装位。"""
    import shutil
    c = (os.environ.get("APP_CLAUDE") or "").strip()
    if c and os.path.exists(c):
        return c
    return shutil.which("claude") or "/home/bwicarus/.local/bin/claude"


def _claude_ocr_crop(png_bytes, timeout=140):
    """把一小块裁图发给视觉模型,精确转写其中文字(修坏文字层用)。返回纯转写文本或 None。
    走脱壳 claude + Gemini 双后端(按「看图」预设 + 互为兜底);prompt 自包含 → system=""。
    (2026-07:删掉从未生效的 model/effort 死参数,模型统一由「看图」action 预设决定。)"""
    import base64
    A = _assistant()
    prompt = (
        "这是从 PDF 裁出的一小块文字图。请**精确逐字转写**图中文字,原样输出,"
        "**不要翻译、不要解释、不要补全、不要加引号或代码围栏**。规则:"
        "① 数学/上标/下标/分式/根号/求和积分一律用 LaTeX 写进 $...$(例:$10^{-8}$、$x_i$、$\\frac{a}{b}$);"
        "② 特殊符号照原样保留(如 Å、±、≈、×、希腊字母 α β λ);"
        "③ 多行的话用空格连接成一段;④ 图里没有的内容绝不臆造;"
        "⑤ 图像**左右边缘**若有被裁掉一半、不完整的字符(只露出偏旁/半个字),**忽略它**,只转写完整的字。"
        "只输出转写出来的文字本身,别的什么都别说。")
    images = [{"media_type": "image/png", "b64": base64.b64encode(png_bytes).decode("ascii")}]
    return A.reader_vision(images, prompt, action="vision", uid=_reader_uid(), system="", timeout=timeout)


def _fig_badge_anchor(page, bbox, others=None, debug=False):
    """徽标锚点:先把 AI bbox 收紧成真实图框(_fig_refine_bbox),邻图同样收紧,再交几何函数。
    供独立脚本(重算徽标)直接用 AI bbox 调用;路由侧已缓存 fbox 时走 _fig_badge_from_block。"""
    fb = _fig_refine_bbox(page, bbox) or [max(0.0, min(1.0, float(v))) for v in bbox[:4]]
    ofbs = []
    for o in (others or []):
        ofbs.append(_fig_refine_bbox(page, o) or [max(0.0, min(1.0, float(v))) for v in o[:4]])
    return _fig_badge_from_block(page, fb, ofbs, debug)


def _fig_badge_from_block(page, block, others=None, debug=False):
    """徽标锚点(归一 [bx,by]),**按用户算法,纯几何**;block 已是收紧后的真实图框:
    A = 以**图中心**为中心,向四向各自撞到**最近的文字框 / 页边 / 邻图**围出的「无文字矩形」(图在 A 内);
    B = 从 A 的**右上角顶点**沿 A 的**右上→左下对角线**(共线)往图方向缩放出的、与图不重叠的最大矩形;
    徽标 = B 的**左下角**(= 该对角线撞到图边界 x=fx1 或 y=fy0 的那一点),再朝右上微退,使徽标落在图外。
    文字框来自 page.get_text('words')(精确),不碰像素。失败回 None。"""
    try:
        pr = page.rect
        W = float(pr.width); H = float(pr.height)
        if W <= 0 or H <= 0:
            return None
        fx0, fy0, fx1, fy1 = [max(0.0, min(1.0, float(v))) for v in block[:4]]
        if fx1 <= fx0 or fy1 <= fy0:
            return None
        fcx = (fx0 + fx1) / 2.0; fcy = (fy0 + fy1) / 2.0      # 图中心
        # 障碍(归一):文字 word ∪ 邻图
        obs = [(w[0] / W, w[1] / H, w[2] / W, w[3] / H) for w in page.get_text("words")]
        for og in (others or []):
            try:
                obs.append(tuple(max(0.0, min(1.0, float(v))) for v in og[:4]))
            except Exception:
                pass

        def ovy(o): return o[3] > fy0 and o[1] < fy1         # 障碍 y 与图重叠(同高 → 算左右边界)
        def ovx(o): return o[2] > fx0 and o[0] < fx1         # 障碍 x 与图重叠(同宽 → 算上下边界)
        # A 四边:撞到图**外**的最近障碍/页边。**只看图外**的障碍(用图四条边过滤)——
        # 否则 OCR 在图内识别出的"文字"(标签/噪声)会被当边界,把 A 挤进图里。
        A_left  = max([0.0] + [o[2] for o in obs if o[2] <= fx0 and ovy(o)])
        A_right = min([1.0] + [o[0] for o in obs if o[0] >= fx1 and ovy(o)])
        A_top   = max([0.0] + [o[3] for o in obs if o[3] <= fy0 and ovx(o)])
        A_bot   = min([1.0] + [o[1] for o in obs if o[1] >= fy1 and ovx(o)])
        if A_right <= A_left or A_bot <= A_top:
            return None
        # 对角线 TR(A_right,A_top) → BL(A_left,A_bot)。B 从 TR 沿对角线扩,撞图(x=fx1 或 y=fy0)即停。
        dx = A_left - A_right    # < 0
        dy = A_bot - A_top       # > 0
        t1 = (fx1 - A_right) / dx if dx < 0 else 1.0     # 对角线 x 到达图右边 fx1 的参数
        t2 = (fy0 - A_top) / dy if dy > 0 else 1.0       # 对角线 y 到达图上边 fy0 的参数
        t = max(0.0, min(1.0, max(t1, t2)))              # B 最大 = 后到的那条边界(撞图前最后一刻)
        px = A_right + t * dx                            # B 左下角(在对角线上、图边界上)
        py = A_top + t * dy
        # 朝右上(TR 方向)微退一点,让徽标整体落到图外(否则正好压在图边界上)
        off = 0.022
        ux, uy = (A_right - px), (A_top - py)
        un = (ux * ux + uy * uy) ** 0.5
        if un > 1e-6:
            k = min(off, un * 0.5) / un
            px += ux * k; py += uy * k
        bx = max(0.0, min(1.0, px)); by = max(0.0, min(1.0, py))
        if debug:
            return {
                "badge": [round(bx, 4), round(by, 4)],
                "A": [round(A_left, 4), round(A_top, 4), round(A_right, 4), round(A_bot, 4)],
                "fig_block": [round(fx0, 4), round(fy0, 4), round(fx1, 4), round(fy1, 4)],
                "B": [round(A_right + t * dx, 4), round(A_top, 4), round(A_right, 4), round(A_top + t * dy, 4)],
            }
        return [round(bx, 4), round(by, 4)]
    except Exception:
        return None


@bp.route("/api/page-figures")
def pdf_api_page_figures():
    """GET ?file=&page= → {ok, figures:[{caption,bbox,desc,badge}…本页], pending}。
    没描述过的页 → 后台触发描述(本页 + 预取后 2 页),返回 pending=true,前端稍后重取。
    badge=[bx,by] 是服务端按像素算的徽标锚点(归一,徽标中心),缺则懒算 + 持久化(下次同位置)。"""
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    if not _book_fig_enabled(rel):              # 本书未开插图描述 → 不画徽标、不触发任何 AI 描述
        resp = jsonify({"ok": True, "figures": [], "pending": False, "disabled": True})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    data = _fig_load_abs(abs_path)
    # 几何/图组层 = DocLayout-YOLO 离线写的 figures_geom(scripts/yolo_figures.py:嵌套去重 + 图组合并 + fbox)。
    # 优先用 DocLayout-YOLO 的 figures_geom(精确 fbox + 去重 + 图组);但 **per-page 回退**:
    # YOLO 召回率比 AI describe 低,有些页 AI 已描述了插图、YOLO 却漏检 → 这些页回退到 AI 原始 figures,
    # 否则「开了插图描述却整页不出徽标」(临时用 AI bbox 当 fbox、徽标放右上角,夜间 YOLO 再细化)。
    geom = data.get("figures_geom")
    raw = data.get("figures", [])
    if geom is None:                                    # 整本没跑过 YOLO → 全用 AI figures
        page_figs = [f for f in raw if f.get("page") == page]
    else:
        page_figs = [f for f in geom if f.get("page") == page]
        if not page_figs:                               # 这页 YOLO 没覆盖 → 回退 AI describe(漏检页也出图)
            page_figs = [f for f in raw if f.get("page") == page]
    need_badge = [f for f in page_figs if f.get("bbox") and not f.get("badge")]
    if need_badge:
        try:
            for f in need_badge:
                if not f.get("fbox"):
                    f["fbox"] = f["bbox"]
                f["badge"] = _fig_badge_topright(f["fbox"])
            _fig_save_abs(abs_path, data)
        except Exception:
            pass
    # 只回**已描述**的图(desc 非空):YOLO 框好但夜间还没描述的 standalone 框先不显示(避免空徽标),
    #   等夜间裁图描述批处理(describe_figures_batch.py)填了 desc 下次自然出现。
    figs = [{"caption": f.get("caption", ""), "bbox": f.get("bbox"), "fbox": f.get("fbox"),
             "desc": f.get("desc", ""), "badge": f.get("badge"),
             "group": bool(f.get("group")), "members": f.get("members") or []}
            for f in page_figs if (f.get("desc") or "").strip() or (f.get("caption") or "").strip()]
    # 只读:描述由 YOLO 闲时框图(yolo-figures.timer)+ 夜间裁图描述(figures-describe.timer)离线生成,
    #   阅读时不再即时触发 AI(_fig_describe_bg 已退役)。pending 恒 False → 前端不再轮询。
    resp = jsonify({"ok": True, "figures": figs, "pending": False})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _formula_ocr_url():
    """PC 上 pix2tex 服务地址:env FORMULA_OCR_URL > server-config.formula_ocr_url > 默认 PC Tailscale。"""
    u = (os.environ.get("FORMULA_OCR_URL") or "").strip()
    if u:
        return u.rstrip("/")
    try:
        cfg = json.loads((CLAUDE_DIR / "state" / "server-config.json").read_text("utf-8"))
        u = (cfg.get("formula_ocr_url") or "").strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return "http://100.99.9.124:8765"


_FML_OCR_DIR = CLAUDE_DIR / "state" / "formula-ocr"   # 公式 OCR 后台任务 pid 文件(防重复启 + 查在跑)


@bp.route("/api/formula-ocr", methods=["POST"])
def pdf_api_formula_ocr():
    """公式 OCR(**Claude 视觉,服务端,无需 PC**):后台跑 scripts/formula_ocr_claude.py 给"还没 latex"的
    公式框补 LaTeX(中文混排出 \\text{})。detached 子进程(关网页/重启不中断;在跑则不重复启)。
    幂等(只补缺 latex 的,不覆盖已校正的)。进度查 /api/formula-ocr-status。"""
    import subprocess, time
    body = request.get_json(silent=True) or {}
    rel = (body.get("file") or "").strip()
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        return jsonify({"ok": False, "error": "invalid"}), 400
    data = _fig_load_abs(abs_path)
    formulas = data.get("formulas") or []
    if not formulas:
        return jsonify({"ok": False, "error": "no_boxes", "msg": "本书还没检测到公式框(先跑 YOLO 公式检测)"}), 200
    todo = [f for f in formulas if not (f.get("latex") or "").strip() and f.get("bbox") and len(f.get("bbox")) == 4]
    total = len(formulas); have = total - len(todo)
    if not todo:
        return jsonify({"ok": True, "started": False, "done": True, "total": total, "have": have, "msg": "所有公式都已识别"}), 200
    sha = _book_sha(abs_path)
    _FML_OCR_DIR.mkdir(parents=True, exist_ok=True)
    sp = _FML_OCR_DIR / f"{sha}.json"
    try:                                  # 已在跑 → 不重复启
        st = json.loads(sp.read_text("utf-8"))
        if st.get("pid") and _pid_alive(st["pid"]):
            return jsonify({"ok": True, "already_running": True, "total": total, "have": have, "remaining": len(todo)})
    except Exception:
        pass
    py = os.environ.get("APP_PYTHON") or sys.executable
    cmd = [py, str(CLAUDE_DIR / "scripts" / "formula_ocr_claude.py"),
           "--book", str(abs_path), "--model", "sonnet", "--effort", "low", "--batch", "8"]
    popen_kw = dict(cwd=str(CLAUDE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        popen_kw["creationflags"] = 0x00004000 | 0x08000000   # BELOW_NORMAL + CREATE_NO_WINDOW
    else:
        popen_kw["start_new_session"] = True
        cmd = ["nice", "-n", "10"] + cmd
    try:
        p = subprocess.Popen(cmd, **popen_kw)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"启动失败:{ex}"}), 500
    try:
        sp.write_text(json.dumps({"pid": p.pid, "ts": int(time.time())}), "utf-8")
    except Exception:
        pass
    return jsonify({"ok": True, "started": True, "pid": p.pid, "total": total, "have": have, "remaining": len(todo)})


@bp.route("/api/formula-ocr-status")
def pdf_api_formula_ocr_status():
    """公式 OCR 进度:从 sidecar 实时统计 有latex/总框 + 后台进程是否在跑。"""
    rel = (request.args.get("file") or "").strip()
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        return jsonify({"ok": False, "error": "invalid"}), 400
    formulas = (_fig_load_abs(abs_path).get("formulas") or [])
    total = len(formulas)
    have = sum(1 for f in formulas if (f.get("latex") or "").strip())
    running = False
    try:
        st = json.loads((_FML_OCR_DIR / f"{_book_sha(abs_path)}.json").read_text("utf-8"))
        running = bool(st.get("pid") and _pid_alive(st["pid"]))
    except Exception:
        pass
    return jsonify({"ok": True, "total": total, "have": have, "remaining": total - have, "running": running})


@bp.route("/api/figure-crop", methods=["GET", "POST"])
def pdf_api_figure_crop():
    """图框区域 PNG(拖拽 ghost / 缩略图 / 大图)。
    GET  ?file=&page=&box=x0,y0,x1,y1[&ink=1] → ink=1 叠服务端保存的笔迹。
    POST {file,page,box:[..],strokes:[..]} → 用**传入的当前笔迹**合成(缩略图/大图用,不依赖墨迹保存时机)。"""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        rel = (data.get("file") or "").strip()
        abs_path = _safe_vault_path(rel)
        try:
            page = int(data.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        box = data.get("box"); strokes = data.get("strokes")
        if not abs_path or page < 1 or not box:
            return jsonify({"ok": False, "error": "invalid"}), 400
        try:
            png = _figure_crop_png(abs_path, page, box, with_ink=bool(strokes), rel=rel, strokes=strokes)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:120]}), 500
        return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})
    rel = request.args.get("file", "")
    abs_path = _safe_vault_path(rel)
    page = int(request.args.get("page", "0") or "0")
    boxs = request.args.get("box", "")
    if not abs_path or page < 1 or not boxs:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        box = [float(v) for v in boxs.split(",")][:4]
    except Exception:
        return jsonify({"ok": False, "error": "bad box"}), 400
    try:
        png = _figure_crop_png(abs_path, page, box,
                               with_ink=(request.args.get("ink") in ("1", "true")), rel=rel)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]}), 500
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/api/highlights", methods=["GET"])
def pdf_api_highlights_list():
    """GET ?file=<rel> → {ok, highlights:[{id,page,rects,color,text,note,time}, ...]}"""
    rel = request.args.get("file", "")
    if not rel or _safe_vault_path(rel) is None:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    return jsonify({"ok": True, **_hl_load(rel)})


@bp.route("/api/highlights", methods=["POST"])
def pdf_api_highlights_create():
    """POST {file, page, rects:[[x0,y0,x1,y1],...], color, text, note?, page_w?, page_h?}
    rects 用 PDF 坐标（pt，跟 page-chars 同坐标系）。返回 {ok, id}。"""
    import time as _t
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    if not rel or _safe_vault_path(rel) is None:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    page = int(data.get("page") or 0)
    rects = data.get("rects") or []
    color = (data.get("color") or "#fff59d").strip()
    text = (data.get("text") or "").strip()
    note = (data.get("note") or "").strip()
    kind = (data.get("kind") or "note").strip()
    sentence = (data.get("sentence") or "").strip()
    body = (data.get("body") or "").strip()
    if page < 1 or not rects:
        return jsonify({"ok": False, "error": "missing page/rects"}), 400
    # 规范化 rects
    norm = []
    for r in rects:
        if not isinstance(r, list) or len(r) != 4:
            continue
        try:
            x0, y0, x1, y1 = float(r[0]), float(r[1]), float(r[2]), float(r[3])
        except (TypeError, ValueError):
            continue
        if x1 < x0: x0, x1 = x1, x0
        if y1 < y0: y0, y1 = y1, y0
        norm.append([round(x0,2), round(y0,2), round(x1,2), round(y1,2)])
    if not norm:
        return jsonify({"ok": False, "error": "no valid rects"}), 400
    obj = {
        "id": "h_" + os.urandom(6).hex(),
        "page": page,
        "rects": norm,
        "color": color,
        "text": text[:2000],
        "note": note[:2000],
        "kind": kind if kind in ("note","translate","explain") else "note",
        "sentence": sentence[:2000],
        "body": body[:8000],
        "time": int(_t.time()),
    }
    pw, ph = data.get("page_w"), data.get("page_h")
    if pw and ph:
        try:
            obj["page_w"] = float(pw); obj["page_h"] = float(ph)
        except (TypeError, ValueError): pass
    db = _hl_load(rel)
    db["highlights"].append(obj)
    _hl_save(rel, db)
    return jsonify({"ok": True, "id": obj["id"], "highlight": obj})


@bp.route("/api/highlight-text", methods=["POST"])
def pdf_api_highlight_text():
    """给 agent(Claude Code skill)用的高层高亮:只传文字,服务端 PyMuPDF search_for 自己找坐标,无需 agent 处理 bbox。
    POST {file, page, text, color?, note?} → {ok, id, found}。text 在该页找不到 → {ok:False, found:0}。"""
    import time as _t, os
    import fitz
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    ap = _safe_vault_path(rel)
    if not rel or ap is None:
        return jsonify({"ok": False, "error": "invalid file"}), 400
    page = int(data.get("page") or 0)
    text = (data.get("text") or "").strip()
    color = (data.get("color") or "#fff59d").strip()
    note = (data.get("note") or "").strip()
    if page < 1 or not text:
        return jsonify({"ok": False, "error": "missing page/text"}), 400
    try:
        doc = fitz.open(str(ap))
        pg = doc[page - 1]
        found = pg.search_for(text)   # Rect 列表(pt,topleft 原点,跟 page-chars/highlights 同坐标系)
        pw, ph = pg.rect.width, pg.rect.height
        doc.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:140]}), 500
    if not found:
        return jsonify({"ok": False, "error": "text not found on page", "found": 0})
    rects = [[round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)] for r in found]
    obj = {"id": "h_" + os.urandom(6).hex(), "page": page, "rects": rects, "color": color,
           "text": text[:2000], "note": note[:2000], "kind": "note", "sentence": "", "body": "",
           "time": int(_t.time()), "page_w": pw, "page_h": ph}
    db = _hl_load(rel)
    db["highlights"].append(obj)
    _hl_save(rel, db)
    return jsonify({"ok": True, "id": obj["id"], "found": len(rects)})


@bp.route("/api/highlights", methods=["PATCH"])
def pdf_api_highlights_update():
    """PATCH {file, id, color?, note?} → {ok}"""
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    hid = (data.get("id") or "").strip()
    if not rel or _safe_vault_path(rel) is None or not hid:
        return jsonify({"ok": False, "error": "invalid"}), 400
    db = _hl_load(rel)
    found = None
    for h in db["highlights"]:
        if h.get("id") == hid:
            found = h
            break
    if not found:
        return jsonify({"ok": False, "error": "not found"}), 404
    if "color" in data:
        # 允许空字符串：表示"取消颜色但保留备注"
        v = data.get("color")
        found["color"] = (v.strip() if isinstance(v, str) else "")
    if "note" in data:
        found["note"] = (data.get("note") or "").strip()[:2000]
    if "sentence" in data:
        found["sentence"] = (data.get("sentence") or "").strip()[:2000]
    if "body" in data:
        found["body"] = (data.get("body") or "").strip()[:8000]
    _hl_save(rel, db)
    return jsonify({"ok": True, "highlight": found})


@bp.route("/api/highlights", methods=["DELETE"])
def pdf_api_highlights_delete():
    """DELETE {file, id} → {ok}"""
    data = request.get_json(silent=True) or {}
    if not data:
        # 也支持 ?file=&id=
        data = {"file": request.args.get("file",""), "id": request.args.get("id","")}
    rel = (data.get("file") or "").strip()
    hid = (data.get("id") or "").strip()
    if not rel or _safe_vault_path(rel) is None or not hid:
        return jsonify({"ok": False, "error": "invalid"}), 400
    db = _hl_load(rel)
    before = len(db["highlights"])
    db["highlights"] = [h for h in db["highlights"] if h.get("id") != hid]
    if len(db["highlights"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    _hl_save(rel, db)
    return jsonify({"ok": True})


# ─── 整句翻译（DeepL / Haiku / MyMemory 可选）────────────────────────────

# 句子翻译 sidecar：手动翻译过的句子(几何 + 译文)持久化，下次渲染句子标记 + 点 L 框看译文
_TR_DIR = CLAUDE_DIR / "state" / "pdf-tr-sentences"
def _tr_path(rel: str) -> Path:
    import hashlib
    _TR_DIR.mkdir(parents=True, exist_ok=True)
    return _TR_DIR / (hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".json")
def _tr_load(rel: str) -> list:
    p = _tr_path(rel)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text("utf-8")).get("sentences", [])
    except Exception:
        return []
def _tr_save_one(rel: str, sent: dict):
    arr = [s for s in _tr_load(rel) if s.get("text") != sent.get("text")]   # 同句重译覆盖
    arr.append(sent)
    try:
        _tr_path(rel).write_text(json.dumps({"pdf_rel": rel, "sentences": arr[-500:]}, ensure_ascii=False), "utf-8")
    except Exception:
        pass
def _tr_delete(rel: str, text: str):
    arr = [s for s in _tr_load(rel) if (s.get("text") or "") != text]
    try:
        _tr_path(rel).write_text(json.dumps({"pdf_rel": rel, "sentences": arr}, ensure_ascii=False), "utf-8")
    except Exception:
        pass

# 用户长按 L 框删除的句子（持久隐藏，page-chars 过滤）
_DISMISS_DIR = CLAUDE_DIR / "state" / "pdf-sent-dismissed"
def _dismiss_path(rel: str) -> Path:
    import hashlib
    _DISMISS_DIR.mkdir(parents=True, exist_ok=True)
    return _DISMISS_DIR / (hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".json")
def _dismiss_load(rel: str) -> set:
    p = _dismiss_path(rel)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text("utf-8")).get("texts", []))
    except Exception:
        return set()
def _dismiss_add(rel: str, text: str):
    s = _dismiss_load(rel); s.add(text)
    try:
        _dismiss_path(rel).write_text(json.dumps({"pdf_rel": rel, "texts": sorted(s)[-2000:]}, ensure_ascii=False), "utf-8")
    except Exception:
        pass


@bp.route("/api/sentence-dismiss", methods=["POST"])
def pdf_api_sentence_dismiss():
    """长按 L 框删除句子标记：记入 dismissed(page-chars 过滤) + 若是翻译句则从译文 sidecar 删。
    body: {file, text}"""
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    text = (data.get("text") or "").strip()
    if not rel or _safe_vault_path(rel) is None or not text:
        return jsonify({"ok": False, "error": "invalid"}), 400
    _dismiss_add(rel, text)
    try: _tr_delete(rel, text)
    except Exception: pass
    return jsonify({"ok": True})


@bp.route("/api/translate-sentence", methods=["POST"])
def pdf_api_translate_sentence():
    """body: {text, backend?, model?, effort?, file?, sentence?} → {ok, zh}
    backend 留空时按 server-config.dict.translate_backend 默认（auto = deepl → mymemory）。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text or len(text) > 2000:
        return jsonify({"ok": False, "error": "no text / too long"}), 400
    backend = (data.get("backend") or "").strip()
    # 2026-07 收口:不再收 request 的 model/effort 覆盖(以前来自已废弃的 pdf-ai-overrides localStorage)。
    # 传空 → translate.py 自动回退 server-config dict.translate_model/effort(设置面板「句子翻译源」)。
    model = ""
    effort = ""
    no_cache = bool(data.get("fresh"))   # 「重新翻译」绕缓存,必出新结果(覆盖旧/坏译文如 AI 拒绝)
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        from translate import translate as _tr  # type: ignore
        zh = _tr(text, backend=backend, model=model, effort=effort, no_cache=no_cache)
        if zh:
            # 前端带 file + sentence 几何时 → 存 sidecar(持久句子标记 + 译文浮层)
            sent = data.get("sentence")
            file_rel = data.get("file") or ""
            if file_rel and isinstance(sent, dict) and sent.get("rects"):
                sent = dict(sent); sent["text"] = text; sent["zh"] = zh; sent["manual"] = True
                _tr_save_one(file_rel, sent)
            return jsonify({"ok": True, "zh": zh})
        return jsonify({"ok": False, "error": "translation failed (no result)"})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@bp.route("/api/translate-config", methods=["GET", "POST"])
def pdf_api_translate_config():
    """读写句子翻译配置 (server-config.dict.translate_*)。
    GET → {ok, backend, model, effort}
    POST {backend, model, effort} → {ok}
    """
    cfg_path = CLAUDE_DIR / "state" / "server-config.json"
    try:
        cfg = json.loads(cfg_path.read_text("utf-8"))
    except Exception:
        cfg = {}
    d = cfg.setdefault("dict", {})
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "backend": d.get("translate_backend", "auto"),
            "model": d.get("translate_model", "sonnet"),
            "effort": d.get("translate_effort", "low"),
        })
    data = request.get_json(silent=True) or {}
    bk = (data.get("backend") or "").strip().lower()
    if bk and bk in ("auto", "deepl", "mymemory", "ai"):
        d["translate_backend"] = bk
    if "model" in data:
        d["translate_model"] = (data.get("model") or "").strip()
    if "effort" in data:
        d["translate_effort"] = (data.get("effort") or "").strip()
    try:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True})


# ─── vocab Anki 一键加卡 ────────────────────────────────────────────────────

@bp.route("/api/vocab-anki", methods=["POST"])
def pdf_api_vocab_anki():
    """根据 vocab 笔记 + 三源字典数据生成 Anki 卡（不调 AI），加到 deck 'Vocab'。
    body: {word: <lemma 或 inflected>}
    返回: {ok, action: created|updated, note_id}
    """
    data = request.get_json(silent=True) or {}
    word = (data.get("word") or "").strip().lower()
    if not word or len(word) > 50:
        return jsonify({"ok": False, "error": "invalid word"}), 400
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        import anki_from_word    # type: ignore
    except Exception as ex:
        return jsonify({"ok": False, "error": f"load anki_from_word failed: {ex}"}), 500
    try:
        result = anki_from_word.make_card(word)
        return jsonify(result)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@bp.route("/api/vocab-mark", methods=["POST"])
def pdf_api_vocab_mark():
    """用户在完整字典框手动标「已掌握 / 没掌握 / 清除」。
    body: {word: <lemma>, mark: "known"|"unknown"|""}
    写 vocab 笔记 frontmatter.user_mark + 立即锁定 mastery（known→1.0），刷新 vocab_index。
    """
    data = request.get_json(silent=True) or {}
    word = (data.get("word") or "").strip().lower()
    mark = (data.get("mark") or "known").strip().lower()
    if not word or len(word) > 50:
        return jsonify({"ok": False, "error": "invalid word"}), 400
    if any(ch.isspace() for ch in word):
        # 含空格的是词组,不是单词。单词掌握会建 vocab 笔记,词组建会生成幽灵笔记(web browser.md)。
        # 词组掌握走 /api/phrase-mark。这里直接拒,避免脏数据。
        return jsonify({"ok": False, "error": "phrase not allowed; use phrase-mark"}), 400
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        import compute_mastery   # type: ignore
        result = compute_mastery.apply_user_mark(word, mark)
        if not result.get("ok") and "not found" in (result.get("error") or "").lower():
            # 笔记还没建好(查词后台建笔记是异步+在线,慢)就点了掌握 → 先离线建一个再标,避免标记失败
            try:
                _, bvn = _vocab_modules()
                if bvn is not None:
                    bvn.update_word_note(word, online=False, download_audio=False)
                    result = compute_mastery.apply_user_mark(word, mark)
            except Exception:
                pass
        if result.get("ok"):
            try:
                import vocab_index   # type: ignore
                vocab_index.index(force_reload=True)   # 让下划线/句子标记立即反映新 mastery
            except Exception:
                pass
        return jsonify(result)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


# ─── 英语语法分析 ─────────────────────────────────────────────────────────

_GRAMMAR_NODES_PATH = CLAUDE_DIR / "_server_deploy" / "grammar-nodes.json"
_GRAMMAR_TRACKED_DIR = CLAUDE_DIR / "state" / "grammar-tracked"
_GRAMMAR_CACHE_DIR   = CLAUDE_DIR / "state" / "grammar-cache"


def _grammar_nodes() -> list[dict]:
    try:
        data = json.loads(_GRAMMAR_NODES_PATH.read_text("utf-8"))
        return data.get("nodes") or []
    except Exception:
        return []


def _tracked_path(file_rel: str) -> Path:
    import hashlib
    sha = hashlib.sha1(file_rel.encode("utf-8")).hexdigest()[:16]
    _GRAMMAR_TRACKED_DIR.mkdir(parents=True, exist_ok=True)
    return _GRAMMAR_TRACKED_DIR / f"{sha}.json"


def _tracked_get(file_rel: str) -> list[str]:
    p = _tracked_path(file_rel)
    if not p.exists(): return []
    try:
        return list(json.loads(p.read_text("utf-8")).get("tracked", []))
    except Exception:
        return []


def _tracked_set(file_rel: str, tracked: list[str]):
    p = _tracked_path(file_rel)
    p.write_text(json.dumps({"pdf_rel": file_rel, "tracked": tracked},
                            ensure_ascii=False, indent=2), "utf-8")


@bp.route("/api/grammar-nodes", methods=["GET"])
def pdf_api_grammar_nodes():
    """所有可跟踪的语法节点 list（旧 demo 数据；保留兼容）"""
    return jsonify({"ok": True, "nodes": _grammar_nodes()})


@bp.route("/api/grammar-books", methods=["GET"])
def pdf_api_grammar_books():
    """列出所有 kind=grammar 的 KG，含每本书 tracked 节点数。
    返回 {ok, books: [{book, title, total_l2, tracked_count}]}"""
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    out = []
    for kg_f in kg_dir.glob("*.json"):
        if kg_f.name.endswith(".bak.json"): continue
        try:
            kg = json.loads(kg_f.read_text("utf-8"))
        except Exception:
            continue
        if kg.get("kind") != "grammar":
            continue
        nodes = kg.get("nodes") or []
        l2 = [n for n in nodes if n.get("level") == 2]
        tracked = [n for n in l2 if n.get("tracked")]
        out.append({
            "book": kg.get("book") or kg_f.stem,
            "title": kg.get("title") or kg.get("book") or kg_f.stem,
            "total_l2": len(l2),
            "tracked_count": len(tracked),
        })
    out.sort(key=lambda x: x["book"])
    return jsonify({"ok": True, "books": out})


def _collect_grammar_tracked_nodes(enabled_books: list[str]) -> list[dict]:
    """汇总指定 KG 中所有 tracked level-2 节点（合并视图给 AI 用）。"""
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    out = []
    for b in enabled_books:
        kg_f = kg_dir / f"{b}.json"
        if not kg_f.exists(): continue
        try:
            kg = json.loads(kg_f.read_text("utf-8"))
        except Exception:
            continue
        if kg.get("kind") != "grammar": continue
        for n in kg.get("nodes") or []:
            if n.get("level") == 2 and n.get("tracked"):
                out.append({
                    "id": n["id"], "name": n.get("name", ""),
                    "summary": n.get("summary", ""),
                    "book": b,
                })
    return out


@bp.route("/api/grammar-tracked", methods=["GET", "POST"])
def pdf_api_grammar_tracked():
    """新语义：per-PDF 启用的 grammar KG 书列表。
    用户在技能树页面 toggle 节点 tracked；PDF reader 这里勾选哪些书启用。
    分析时合并所有启用书的 tracked 节点。
    GET ?file=<rel> → {ok, enabled_books: [...]}
    POST {file, enabled_books: [...]} → {ok}"""
    if request.method == "GET":
        rel = (request.args.get("file") or "").strip()
        if not rel: return jsonify({"ok": False, "error": "no file"}), 400
        data = _load_grammar_enabled(rel)
        return jsonify({"ok": True, "enabled_books": data})
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    books = data.get("enabled_books") or []
    if not rel or not isinstance(books, list):
        return jsonify({"ok": False, "error": "invalid"}), 400
    _save_grammar_enabled(rel, [str(b) for b in books])
    return jsonify({"ok": True})


def _load_grammar_enabled(file_rel: str) -> list[str]:
    p = _tracked_path(file_rel)
    if not p.exists(): return []
    try:
        d = json.loads(p.read_text("utf-8"))
        # 兼容老格式（tracked 是 node ids）+ 新格式（enabled_books）
        if "enabled_books" in d:
            return list(d["enabled_books"])
        return []
    except Exception:
        return []


def _save_grammar_enabled(file_rel: str, books: list[str]):
    p = _tracked_path(file_rel)
    p.write_text(json.dumps({"pdf_rel": file_rel, "enabled_books": books},
                            ensure_ascii=False, indent=2), "utf-8")


_SPACY_LOCK = _threading.Lock()
_SPACY_PROC: list = [None]    # 常驻 spacy worker 进程盒(2026-06-10:原每句 spawn 子进程,固定付 ~3.3s 进程+模型加载税)


def _spacy_worker_kill():
    p = _SPACY_PROC[0]
    try:
        if p:
            p.kill()
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_spacy_worker_kill)   # gunicorn 重启不留孤儿


def _spacy_worker_request(sentence: str, timeout: float = 10.0) -> str | None:
    """向常驻 spacy worker(spacy_parse.py --server)发一行 JSON、读一行响应。
    锁覆盖完整收发(8 gthread 下防响应错配);进程没起/死了就地拉起(首次含模型加载 ~3s);
    超时/异常 → kill+置空下次重拉,返回 None(调用方走既有 AI 兜底,语义不变)。"""
    import subprocess
    with _SPACY_LOCK:
        first = _SPACY_PROC[0] is None
        out: list = []

        def _io():
            try:
                p = _SPACY_PROC[0]
                if p is None or p.poll() is not None:
                    p = subprocess.Popen(
                        [str(SPACY_PY), str(SPACY_SCRIPT), "--server"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
                    _SPACY_PROC[0] = p          # 先存(超时路径才能 kill 到),再等 ready
                    if not p.stdout.readline():  # {"ready":true}
                        raise RuntimeError("worker no ready")
                p.stdin.write(json.dumps({"text": sentence}, ensure_ascii=False) + "\n")
                p.stdin.flush()
                out.append(p.stdout.readline())
            except Exception:
                pass

        t = _threading.Thread(target=_io, daemon=True)
        t.start()
        t.join(timeout + (8.0 if first else 0.0))   # 首次多给模型加载时间
        if t.is_alive() or not out or not (out[0] or "").strip():
            _spacy_worker_kill()
            _SPACY_PROC[0] = None
            return None
        return out[0]


def _spacy_grammar(sentence: str) -> dict | None:
    """用 spaCy 常驻 worker 做词性 + 依存分析；ECDICT 补每词中文义、MyMemory 译整句。
    返回 {tokens, deps, sentence_zh}；失败返回 None（调用方回退 AI）。"""
    line = _spacy_worker_request(sentence)
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except Exception:
        return None
    if parsed.get("error"):
        return None
    tokens = parsed.get("tokens") or []
    deps = parsed.get("deps") or []
    clauses = parsed.get("clauses") or []
    components = parsed.get("components") or []
    clause_tree = parsed.get("clause_tree")
    if not tokens:
        return None
    # ECDICT 补每个词的简明中文义（离线、毫秒级）；主 tokens + 各子句 tokens 共用一份缓存
    try:
        vp = str(CLAUDE_DIR / "scripts" / "vocab")
        if vp not in sys.path:
            sys.path.insert(0, vp)
        import dict_sources  # type: ignore
        _zh_cache = _GRAMMAR_ZH_CACHE   # 模块级 memo:ECDICT 是静态库,跨请求复用(含 miss 空串负缓存)
        if len(_zh_cache) > 20000:
            _zh_cache.clear()
        def _zh(w: str) -> str:
            w = (w or "").strip()
            if not w or not w[0].isalpha():
                return ""
            key = w.lower()
            if key in _zh_cache:
                return _zh_cache[key]
            z = ""
            try:
                ec = dict_sources.lookup_ecdict(w)
                if ec:
                    for d in dict_sources._ec_definitions(ec):
                        if d.get("zh"):
                            z = d["zh"][:30]
                            break
            except Exception:
                pass
            _zh_cache[key] = z
            return z
        for tk in tokens:
            tk["zh"] = _zh(tk.get("text", ""))
        for c in clauses:
            for tk in c.get("tokens", []):
                tk["zh"] = _zh(tk.get("text", ""))
        # 嵌套子句树的节点也补中文（占位节点 ref 不补），供可展开弧线图点词看翻译
        def _fill_tree_zh(node):
            if not node:
                return
            for nd in node.get("nodes", []):
                if nd.get("ref") is None:
                    nd["zh"] = _zh(nd.get("text", ""))
            for ch in node.get("children", []):
                _fill_tree_zh(ch)
        _fill_tree_zh(clause_tree)
    except Exception:
        pass
    # 整句翻译 + 语法点讲解交给 AI 流式（/api/grammar-stream，翻译标志先出）
    # 这里只出词性 + 依存 + 子句切分，秒级零 AI
    return {"tokens": tokens, "deps": deps, "clauses": clauses, "components": components, "clause_tree": clause_tree, "sentence_zh": ""}


@bp.route("/api/grammar-analyze", methods=["POST"])
def pdf_api_grammar_analyze():
    """spaCy（默认，零 AI）/ AI（兜底）分析句子词性+依存，相对 tracked 语法节点。
    body: {text, sentence?, file, tracked_ids?, model?, effort?}
    返回 {ok, analyses: [{node_id, node_name, point, explanation, examples}]}
    缓存：state/grammar-cache/<sha1(text + sorted(ids))>.json
    """
    import hashlib
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    sentence = (data.get("sentence") or text).strip()
    rel = (data.get("file") or "").strip()
    if not text or len(text) > 2000:
        return jsonify({"ok": False, "error": "no text / too long"}), 400
    enabled_books = data.get("enabled_books") or _load_grammar_enabled(rel)
    if not enabled_books:
        return jsonify({"ok": False, "error": "no enabled grammar KGs; enable in settings + track nodes in skilltree"}), 400
    tracked_nodes = _collect_grammar_tracked_nodes(enabled_books)
    if not tracked_nodes:
        return jsonify({"ok": False, "error": "no tracked level-2 nodes in enabled KGs (toggle 跟踪 in skilltree page)"}), 400

    tracked_ids = [n["id"] for n in tracked_nodes]
    node_by_id = {n["id"]: n for n in tracked_nodes}
    cache_key = hashlib.sha1((sentence + "||" + text + "||" + ",".join(sorted(tracked_ids))).encode("utf-8")).hexdigest()[:20]
    _GRAMMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_p = _GRAMMAR_CACHE_DIR / f"{cache_key}.json"
    if cache_p.exists():
        try:
            _cd = json.loads(cache_p.read_text("utf-8"))
            # 同键文件可能被 grammar-stream 先建(只含 ai_stream_full)→ 必须有真分析内容才算命中
            if _cd.get("tokens") or _cd.get("analyses"):
                _cstat("grammar.full_hit")
                return jsonify({"ok": True, "from_cache": True, **_cd})
        except Exception:
            pass
    # spaCy 输出只依赖 sentence(不看 text/tracked_ids)→ 单独的 sentence-only 键:
    # 换选中焦点词/toggle 任一跟踪节点不再全量重付分析(2026-06-10)。先查全键(兼容存量
    # 含 AI 结果的条目),miss 再查 sp 键。若将来 spaCy 路径用 tracked_ids 做规则匹配,键需加回。
    sp_key = hashlib.sha1(("spacy||" + sentence).encode("utf-8")).hexdigest()[:20]
    sp_p = _GRAMMAR_CACHE_DIR / f"{sp_key}.json"
    if sp_p.exists():
        try:
            _out = json.loads(sp_p.read_text("utf-8"))
            _cstat("grammar.sp_hit")
            return jsonify({"ok": True, "from_cache": True, **_out})
        except Exception:
            pass

    # ── 优先 spaCy 本地分析（词性 + 依存，零 AI、秒级）──
    if _spacy_available():
        _cstat("grammar.spacy_run")
        sp = _spacy_grammar(sentence)
        if sp is not None:
            out = {
                "sentence_zh": sp.get("sentence_zh", ""),
                "tokens":      sp.get("tokens", []),
                "deps":        sp.get("deps", []),
                "clauses":     sp.get("clauses", []),   # 长句按从句切段
                "components":  sp.get("components", []), # 句子成分分块
                "clause_tree": sp.get("clause_tree"),    # 嵌套子句树(可展开弧线)
                "analyses":    [],   # 语法点匹配暂不在 spaCy 路径做（后续可加规则）
                "engine":      "spacy",
            }
            try:
                sp_p.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
            except Exception:
                pass
            return jsonify({"ok": True, **out})
        # spaCy 失败则继续走下面 AI 兜底

    # 构 prompt
    nodes_block = "\n".join(
        f"- [{n['id']}] **{n['name']}** ({n.get('book','')}): {n.get('summary','')}"
        for n in tracked_nodes
    )
    prompt = f"""你是英语句子结构分析助手。请对下面这句做依存句法分析，输出可用于画依存关系图的结构化数据。

【待分析句子】
{sentence}

【用户特别关注的片段（句子内的子串）】
{text}

【跟踪的语法点】
{nodes_block}

【任务】
1. sentence_zh：整句自然中文翻译。
2. tokens：把句子按词切分（标点也算一个 token），**严格保持原句顺序**。每个词给：
   - text：原文 token（跟原句一致，含大小写）
   - pos：词性，**只能用这些小写英文之一**：noun, verb, adj, adv, pron, prep, det, conj, aux, num, punct, part, intj
   - zh：该词在本句中的简明中文义（标点或虚词可留空字符串）
3. deps：依存关系弧的数组。每条 {{"head": <int>, "child": <int>, "label": "<中文关系名>"}}：
   - head / child 都是 tokens 数组下标（0-based 整数）。head 是支配词，child 是从属词。
   - label 用简短中文：主语 / 宾语 / 间接宾语 / 定语 / 状语 / 介词宾语 / 介词 / 系动词 / 并列 / 连词 / 补语 / 限定 / 同位 / 主句谓语标记 等。
   - 整句的核心（一般是主要动词）作为根，不必为它列入边。
   - 不要画自环，head ≠ child。
4. analyses：句中命中的跟踪语法点（仅限上面列表，不要凭空加）。每条 {{node_id, phrase, explanation, examples}}。没有命中则 []。

【输出 JSON（仅输出 JSON，不要任何额外文字）】
{{
  "sentence_zh": "<整句中文翻译>",
  "tokens": [{{"text": "The", "pos": "det", "zh": "（定冠词）"}}, {{"text": "cat", "pos": "noun", "zh": "猫"}}],
  "deps": [{{"head": 1, "child": 0, "label": "限定"}}],
  "analyses": [{{"node_id": "<id>", "phrase": "<语法实例>", "explanation": "<简明解释>", "examples": ["..."]}}]
}}
"""
    _cstat("grammar.ai_run")
    try:
        zh = _ai_call(prompt, "grammar")   # 2026-07:语法分析拆出独立 action(原并在 explain 里)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI call failed: {ex}"}), 500
    # 解析 JSON（AI 可能裹在 ```json 或夹解释文字里）
    import re as _re
    j = None
    # 优先匹配整段 JSON 对象
    m = _re.search(r"\{[\s\S]*\}", zh)
    if m:
        try:
            j = json.loads(m.group(0))
        except Exception:
            # AI 偶尔吐 ```json...``` 包裹，剥一层再试
            cleaned = _re.sub(r"^```(?:json)?|```$", "", m.group(0).strip(), flags=_re.M).strip()
            try:
                j = json.loads(cleaned)
            except Exception:
                pass
    if not j:
        return jsonify({"ok": True, "sentence_zh": "", "tokens": [], "deps": [], "analyses": [], "raw": zh[:1500]})
    # 补节点名
    for a in (j.get("analyses") or []):
        nid = a.get("node_id")
        if nid in node_by_id:
            a["node_name"] = node_by_id[nid]["name"]
    # 清洗 tokens / deps：保证 index 合法、无自环
    tokens = []
    for t in (j.get("tokens") or []):
        if isinstance(t, dict) and t.get("text") is not None:
            tokens.append({
                "text": str(t.get("text", "")),
                "pos":  str(t.get("pos", "")).lower(),
                "zh":   str(t.get("zh", "")),
            })
    n = len(tokens)
    deps = []
    for d in (j.get("deps") or []):
        try:
            h, c = int(d.get("head")), int(d.get("child"))
        except Exception:
            continue
        if 0 <= h < n and 0 <= c < n and h != c:
            deps.append({"head": h, "child": c, "label": str(d.get("label", ""))})
    out = {
        "sentence_zh": j.get("sentence_zh") or "",
        "tokens":      tokens,
        "deps":        deps,
        "analyses":    j.get("analyses") or [],
    }
    try:   # merge:别覆盖 grammar-stream 已存进同键文件的 ai_stream_full
        _old = json.loads(cache_p.read_text("utf-8")) if cache_p.exists() else {}
    except Exception:
        _old = {}
    cache_p.write_text(json.dumps({**_old, **out}, ensure_ascii=False, indent=2), "utf-8")
    return jsonify({"ok": True, **out})


@bp.route("/api/grammar-stream", methods=["POST"])
def pdf_api_grammar_stream():
    """AI 流式：先输出整句翻译（[[TRANS]]..[[/TRANS]] 标志先到先显示），
    再输出语法点讲解（[[POINTS]] JSON [[/POINTS]]）。配合 spaCy 出的依存图用。
    依存图本身不在这里——spaCy 已经出了，这里只补「翻译 + 语法点讲解」。"""
    from flask import Response, stream_with_context
    data = request.get_json(silent=True) or {}
    sentence = (data.get("sentence") or "").strip()
    text = (data.get("text") or "").strip()
    rel = (data.get("file") or "").strip()
    if not sentence:
        return jsonify({"ok": False, "error": "no sentence"}), 400
    enabled_books = data.get("enabled_books") or _load_grammar_enabled(rel)
    tracked_nodes = _collect_grammar_tracked_nodes(enabled_books) if enabled_books else []
    # 服务端回放缓存(2026-06-10):同 (sentence,text,tracked_ids) 讲解过一次就存进
    # grammar-cache 同键文件(grammar-forget 已 unlink 同文件,级联免费)。命中 → 按
    # dict-jp-ai 同款 SSE 一次性回放全文(前端对累计文本抠 [[TRANS]]/[[POINTS]],天然兼容)。
    # prompt 变更需 bump ai_v。
    import hashlib
    tracked_ids = [n["id"] for n in tracked_nodes]
    _gs_key = hashlib.sha1((sentence + "||" + text + "||" + ",".join(sorted(tracked_ids))).encode("utf-8")).hexdigest()[:20]
    _GRAMMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _gs_p = _GRAMMAR_CACHE_DIR / f"{_gs_key}.json"
    try:
        _gs_old = json.loads(_gs_p.read_text("utf-8")) if _gs_p.exists() else {}
    except Exception:
        _gs_old = {}
    if _gs_old.get("ai_stream_full") and _gs_old.get("ai_v") == 1:
        def _gs_replay(md=_gs_old["ai_stream_full"]):
            yield "event: start\ndata: {}\n\n"
            yield f"data: {json.dumps({'text': md}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
        return Response(stream_with_context(_gs_replay()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    nodes_block = "\n".join(
        f"- {n['name']}：{n.get('summary','')}" for n in tracked_nodes
    ) or "（无跟踪语法点，[[POINTS]] 直接输出 []）"
    prompt = f"""你是英语句子分析助手。严格按下面顺序、用标志输出两部分，标志必须原样出现、不要加代码块围栏。

第一部分——整句中文翻译（自然通顺）：
[[TRANS]]这里写整句翻译[[/TRANS]]

第二部分——语法点讲解，只讲【跟踪语法点】里命中的，JSON 数组：
[[POINTS]][{{"point":"语法点名称","phrase":"句中实例短语","explanation":"针对该句的简明讲解","examples":["1-2个相似例句"]}}][[/POINTS]]
没有命中的语法点就输出 [[POINTS]][][[/POINTS]]。

【待分析句子】
{sentence}

【用户特别关注的片段】
{text}

【跟踪语法点】
{nodes_block}

只输出上面两段（含标志），先翻译后语法点，不要任何额外说明。"""

    def _gs_save(full, _p=_gs_p):
        # 只缓存完整输出(两个闭合标志都在,防报错/截断永久缓存);merge 不覆盖同键已有字段(AI 依存分析)
        if "[[/TRANS]]" not in full or "[[POINTS]]" not in full:
            return
        try:
            old = json.loads(_p.read_text("utf-8")) if _p.exists() else {}
        except Exception:
            old = {}
        old["ai_stream_full"] = full
        old["ai_v"] = 1
        try:
            _p.write_text(json.dumps(old, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    return _start_ai_stream(prompt, "grammar", _reader_uid(), (data.get("rid") or "").strip(), on_done=_gs_save)   # 2026-07:语法分析独立 action


# ─────────────────── 单词本 tab ───────────────────

def _vocab_read_fm(path: Path) -> dict:
    """轻量解析 vocab 笔记 frontmatter 的标量字段（跳过 list 项）。"""
    try:
        txt = path.read_text("utf-8")
    except Exception:
        return {}
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    fm = {}
    for line in txt[3:end].splitlines():
        if not line or line[:1] in (" ", "\t", "-"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


_GRAMMAR_ZH_CACHE: dict = {}    # 语法分析 token→简明中文 memo(ECDICT 静态库,常驻进程跨请求复用)
_VOCAB_ZH_CACHE: dict = {}      # vocab-list lemma→中文 memo(含空串负缓存)
_VOCAB_NOTES_CACHE: dict = {"sig": None, "notes": None}   # vocab 笔记 frontmatter(mtime 签名失效)


def _vocab_notes_all(vroot) -> dict:
    """全部 vocab 笔记 frontmatter,带 (文件数,最大mtime) 签名缓存——没变不重读。"""
    files = [p for p in vroot.rglob("*.md") if p.parent.name != "_audio"]
    try:
        sig = (len(files), max((p.stat().st_mtime for p in files), default=0))
    except OSError:
        sig = None
    c = _VOCAB_NOTES_CACHE
    if sig is not None and c["sig"] == sig and c["notes"] is not None:
        return c["notes"]
    notes = {}
    for p in files:
        fm = _vocab_read_fm(p)
        lemma = (fm.get("lemma") or fm.get("word") or p.stem).strip().lower()
        if lemma:
            notes[lemma] = fm
    c["sig"], c["notes"] = sig, notes
    return notes


def _vocab_ecdict_zh(lemma: str) -> str:
    key = (lemma or "").lower()
    if key in _VOCAB_ZH_CACHE:
        return _VOCAB_ZH_CACHE[key]
    if len(_VOCAB_ZH_CACHE) > 5000:
        _VOCAB_ZH_CACHE.clear()
    z = ""
    try:
        vp = str(CLAUDE_DIR / "scripts" / "vocab")
        if vp not in sys.path:
            sys.path.insert(0, vp)
        import dict_sources  # type: ignore
        ec = dict_sources.lookup_ecdict(lemma)
        if ec:
            for d in dict_sources._ec_definitions(ec):
                if d.get("zh"):
                    z = d["zh"][:40]
                    break
    except Exception:
        pass
    _VOCAB_ZH_CACHE[key] = z
    return z


@bp.route("/api/vocab-list")
def pdf_api_vocab_list():
    """单词本列表。scope=book(本 PDF 查过/出现) / all(全部笔记)。
    返回按 mastery 升序（最该复习的在前）的词条。"""
    file = (request.args.get("file") or "").strip()
    scope = (request.args.get("scope") or "book").strip()
    page = 0
    try: page = int(request.args.get("page") or 0)
    except Exception: page = 0
    vroot = OBSIDIAN_ROOT / "资源" / "vocab"
    if not vroot.exists():
        return jsonify({"ok": True, "items": [], "scope": scope})
    notes = _vocab_notes_all(vroot)   # mtime 签名缓存:笔记没变不重读全部 frontmatter
    # 反向索引（出现页）
    exposure = {}
    try:
        exposure = json.loads((CLAUDE_DIR / "state" / "vocab-exposure.json").read_text("utf-8"))
    except Exception:
        pass
    # book(本书) / page(本页) scope：从查词日志 + 反向索引筛 lemma
    target = set(notes.keys())
    if scope in ("book", "page") and file:
        book = set()
        match_page = (scope == "page")
        log = CLAUDE_DIR / "state" / "vocab-lookups.jsonl"
        if log.exists():
            for line in log.read_text("utf-8").splitlines():
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("pdf") == file and j.get("lemma") and (not match_page or j.get("page") == page):
                    book.add(j["lemma"].strip().lower())
        # 也并入「反向索引里在本书/本页出现 + 有笔记」的词
        for lem, ex in exposure.items():
            ll = lem.strip().lower()
            if ll in notes:
                for pg in (ex.get("pages") or []):
                    if pg.get("pdf") == file and (not match_page or pg.get("page") == page):
                        book.add(ll); break
        target = book & set(notes.keys())
    items = []
    for lemma in target:
        fm = notes.get(lemma) or {}
        pages = []
        ex = exposure.get(lemma) or exposure.get(fm.get("lemma", "")) or {}
        for pg in (ex.get("pages") or []):
            if not file or pg.get("pdf") == file:
                if pg.get("page") is not None:
                    pages.append(int(pg["page"]))
        try: mastery = float(fm.get("mastery") or 0)
        except Exception: mastery = 0.0
        try: lc = int(fm.get("lookup_count") or 0)
        except Exception: lc = 0
        try: ts = int(fm.get("last_lookup_ts") or 0)
        except Exception: ts = 0
        items.append({
            "lemma": fm.get("lemma") or lemma,
            "phonetic": fm.get("phonetic_us") or fm.get("phonetic_uk") or "",
            "zh": _vocab_ecdict_zh(lemma),
            "mastery": round(mastery, 3),
            "mastery_label": fm.get("mastery_label") or "",
            "audio": fm.get("audio_us") or fm.get("audio_uk") or "",
            "pages": sorted(set(pages))[:30],
            "lookup_count": lc,
            "last_ts": ts,
            "has_card": bool(fm.get("anki_card_id") or fm.get("card_id")),
        })
    items.sort(key=lambda x: (x["mastery"], -x["lookup_count"], -x["last_ts"]))
    return jsonify({"ok": True, "items": items, "scope": scope, "total": len(items)})


@bp.route("/api/vocab-audio")
def pdf_api_vocab_audio():
    """serve vocab 真人音频（限 资源/vocab/ 下）。"""
    rel = (request.args.get("path") or "").strip()
    if not rel or "资源/vocab/" not in rel:
        abort(403)
    abs_path = _safe_vault_path(rel)
    if not abs_path or not abs_path.exists():
        abort(404)
    ext = abs_path.suffix.lower()
    mime = "audio/mpeg" if ext == ".mp3" else ("audio/ogg" if ext == ".ogg" else "audio/wav")
    return send_file(str(abs_path), mimetype=mime)


# 单词制卡统一走上面的 /api/vocab-anki（进程内 import make_card，无 subprocess 冷启动）；
# 旧 /api/vocab-add-anki（subprocess 版）已并入它，前端 _vocabAddAnki 改调 /vocab-anki。


# ── 语法分析历史：按 PDF 分文件持久保存（state/grammar-history/<sha>.json）──
_GRAMMAR_HISTORY_DIR = CLAUDE_DIR / "state" / "grammar-history"


def _grammar_hist_path(file_rel: str) -> Path:
    import hashlib
    h = hashlib.sha1((file_rel or "_").encode("utf-8")).hexdigest()[:16]
    return _GRAMMAR_HISTORY_DIR / f"{h}.json"


def _grammar_hist_load(file_rel: str) -> dict:
    p = _grammar_hist_path(file_rel)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"pdf": file_rel, "items": []}


def _grammar_hist_write(file_rel: str, data: dict):
    _GRAMMAR_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _grammar_hist_path(file_rel).write_text(json.dumps(data, ensure_ascii=False), "utf-8")


@bp.route("/api/grammar-history", methods=["GET"])
def pdf_api_grammar_history():
    """返回某 PDF 的语法分析历史（按时间倒序，新的在前）。"""
    file = (request.args.get("file") or "").strip()
    hist = _grammar_hist_load(file)
    items = sorted(hist.get("items", []), key=lambda x: x.get("ts", 0), reverse=True)
    return jsonify({"ok": True, "items": items})


@bp.route("/api/grammar-history-save", methods=["POST"])
def pdf_api_grammar_history_save():
    """保存一条语法分析结果到该 PDF 的历史（同句去重更新，限 200 条）。"""
    import time
    data = request.get_json(silent=True) or {}
    file = (data.get("file") or "").strip()
    item = data.get("item") or {}
    sentence = (item.get("sentence") or "").strip()
    if not file or not sentence:
        return jsonify({"ok": False, "error": "no file/sentence"}), 400
    text = item.get("text") or ""
    hist = _grammar_hist_load(file)
    hist["items"] = [x for x in hist.get("items", [])
                     if not (x.get("sentence") == sentence and (x.get("text") or "") == text)]
    item["ts"] = int(time.time())
    hist["items"].append(item)
    hist["items"] = hist["items"][-200:]
    _grammar_hist_write(file, hist)
    return jsonify({"ok": True, "total": len(hist["items"])})


@bp.route("/api/grammar-forget", methods=["POST"])
def pdf_api_grammar_forget():
    """删除某句的语法分析缓存 + 历史（删卡时调），下次分析同句从头生成。"""
    import hashlib
    data = request.get_json(silent=True) or {}
    sentence = (data.get("sentence") or "").strip()
    text = (data.get("text") or "").strip()
    rel = (data.get("file") or "").strip()
    if not sentence:
        return jsonify({"ok": True, "removed": 0})
    enabled_books = data.get("enabled_books") or _load_grammar_enabled(rel)
    tracked_nodes = _collect_grammar_tracked_nodes(enabled_books) if enabled_books else []
    tracked_ids = [n["id"] for n in tracked_nodes]
    # 跟 grammar-analyze 完全一致的 cache_key 算法
    cache_key = hashlib.sha1(
        (sentence + "||" + text + "||" + ",".join(sorted(tracked_ids))).encode("utf-8")
    ).hexdigest()[:20]
    removed = 0
    p = _GRAMMAR_CACHE_DIR / f"{cache_key}.json"
    if p.exists():
        try:
            p.unlink(); removed = 1
        except Exception:
            pass
    # 级联删 spaCy sentence-only 键(保住「删卡→同句从头生成」语义)
    sp_p = _GRAMMAR_CACHE_DIR / (hashlib.sha1(("spacy||" + sentence).encode("utf-8")).hexdigest()[:20] + ".json")
    if sp_p.exists():
        try:
            sp_p.unlink(); removed += 1
        except Exception:
            pass
    # 从历史删
    try:
        hist = _grammar_hist_load(rel)
        before = len(hist.get("items", []))
        hist["items"] = [x for x in hist.get("items", [])
                         if not (x.get("sentence") == sentence and (x.get("text") or "") == text)]
        if len(hist["items"]) != before:
            _grammar_hist_write(rel, hist)
    except Exception:
        pass
    return jsonify({"ok": True, "removed": removed})


def register_pdf_reader(app):
    try:
        from epub_assistant import register_epub_assistant
        register_epub_assistant(bp)   # Phase H:EPUB section 级 agentic 助手 + 对话历史(挂到 /pdf bp)
    except Exception as _ea_err:
        import logging; logging.getLogger(__name__).warning("epub_assistant 未注册: %s", _ea_err)
    app.register_blueprint(bp)
    try:
        _upthr.Thread(target=_fav_prebuild_loop, daemon=True).start()   # 空闲把脏收藏夹提前重建好 → 打开即秒开(不再前台等)
    except Exception:
        pass
    def _warm_templates():
        # 预编译大模板(实测:重启后首个开书请求要 ~1s,全是 Jinja 首次编译;预热后 ~15ms)。后台线程,不阻塞启动。
        try:
            with app.app_context():
                for t in ("pdf_reader.html", "epub_html_reader.html", "pdf_index.html"):
                    try:
                        app.jinja_env.get_template(t)
                    except Exception:
                        pass
        except Exception:
            pass
    try:
        _upthr.Thread(target=_warm_templates, daemon=True).start()
    except Exception:
        pass
