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
import urllib.parse
from pathlib import Path

from flask import (
    Blueprint, Response, abort, jsonify, render_template, request,
    send_file,
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
    """扫 vault 下所有 PDF。返回 [{rel, name, size_kb, mtime, comp_*}, ...]，按修改时间倒序。"""
    out = []
    for p in OBSIDIAN_ROOT.rglob("*.pdf"):
        if p.name.endswith((".orig.pdf", ".compressed.pdf")):
            continue   # 备份/旧式压缩版残留,不当独立书列出
        try:
            rel = p.relative_to(OBSIDIAN_ROOT).as_posix()
            st = p.stat()
            ci = _compressed_info(rel)
            out.append({
                "rel": rel,
                "name": p.name,
                "dir": str(Path(rel).parent),
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": int(st.st_mtime),
                "comp_exists": ci["exists"],
                "comp_compressing": ci["compressing"],
                "comp_percent": ci["percent"],
            })
        except OSError:
            continue
    out.sort(key=lambda x: -x["mtime"])
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


def _ai_backend(override_model: str = "", override_effort: str = ""):
    """初始化 AI backend + 可选 model/effort 覆盖。返回 (backend, msgs_head)"""
    sys.path.insert(0, str(CLAUDE_DIR / "_client" / "core"))
    from ai_backends import make_backend  # type: ignore
    sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
    sys.path.insert(0, str(CLAUDE_DIR / "_server_deploy"))
    try:
        from qa_server import get_cfg
        cfg = get_cfg()
    except Exception:
        cfg = {"ai_backend": "claude_cli", "ai": {"claude_cli": {"command": "/usr/bin/claude"}}}
    backend_name = cfg.get("ai_backend", "claude_cli")
    settings = dict((cfg.get("ai") or {}).get(backend_name, {}))
    if override_model:  settings["model"] = override_model
    if override_effort: settings["effort"] = override_effort
    ad = make_backend(backend_name, settings)
    sys_msg = {"role": "system", "content": "你是一个学习辅助助手。回答简洁、准确。数学公式用 $...$ 或 $$...$$，不要用反引号包数学。"}
    return ad, sys_msg


def _ai_call(prompt: str, override_model: str = "", override_effort: str = "") -> str:
    """同步调用 AI，返回完整字符串。"""
    ad, sys_msg = _ai_backend(override_model, override_effort)
    return ad.chat([sys_msg, {"role": "user", "content": prompt}])


def _ai_call_stream(prompt: str, override_model: str = "", override_effort: str = ""):
    """流式调用 AI，yield text chunks。"""
    ad, sys_msg = _ai_backend(override_model, override_effort)
    msgs = [sys_msg, {"role": "user", "content": prompt}]
    if hasattr(ad, "chat_stream"):
        gen = ad.chat_stream(msgs)
        try:
            for chunk in gen:
                if chunk:
                    yield chunk
        finally:
            try: gen.close()
            except Exception: pass
    else:
        # 后端不支持流式 → 一次性 yield
        yield ad.chat(msgs)


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
                    for _chunk in _ai_call_stream(_prompt, _tmodel, _teffort):
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


def _sse_stream(prompt, model, effort):
    """SSE generator：把 AI chunks 包成 SSE event 流。"""
    import json as _json
    try:
        yield "event: start\ndata: {}\n\n"
        for chunk in _ai_call_stream(prompt, model, effort):
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
    """reader.js 的 cache-bust 版本 = 已部署静态文件 mtime（每次部署自动变，免手动 bump）。"""
    try:
        return str(int(os.path.getmtime("/var/www/html/static/pdf/reader.js")))
    except Exception:
        return "1"


# ── 图片模式(大型文档网站成熟方案:服务端按页出图,客户端只按需取看到的页,不下载整本 PDF)──
_PAGE_IMG_DIR = CLAUDE_DIR / "state" / "pdf-page-img"

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


@bp.route("/api/page-image")
def pdf_api_page_image():
    """渲染某页为 JPEG(PyMuPDF,宽 w px),磁盘缓存(键含 mtime)。客户端逐页按需取 → 只下看到的页。"""
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
        import fitz
        try:
            d = fitz.open(str(ap))
            if page < 1 or page > d.page_count:
                d.close(); abort(404)
            p = d[page - 1]; zoom = w / max(1.0, p.rect.width)
            pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            tmp = cf.with_suffix(".jpg.tmp")
            tmp.write_bytes(pix.tobytes("jpg", jpg_quality=78))
            tmp.replace(cf); d.close()
        except Exception:
            try: d.close()
            except Exception: pass
            abort(500)
    resp = send_file(str(cf), mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@bp.route("/view")
def pdf_view():
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "1") or "1")
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
    # 共享 json:langs + crop 改 key
    for path in (_BOOK_LANGS_PATH, _BOOK_CROP_PATH):
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


def _compute_page_chars(abs_path, page: int):
    """提取该页所有字符 bbox + 词 id(rawdict + PyMuPDF words + fugashi 分词)。
    只依赖 (文件内容, 页码) → 可缓存。返回 (chars, page_w, page_h, furigana) 或 None(越界)。
    furigana = 振假名/音标叠加条目(日语 unidic 读音 + 英文 ECDICT 音标)。"""
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
                _apply_jp_tokenize(_col, _block_i, cur_bk % 1000, furigana)
        # 英文词音标叠加（单连接直查 ECDICT；日语为主的书几乎无开销）
        try:
            furigana.extend(_build_en_furigana(chars))
        except Exception as ex:
            sys.stderr.write(f"[furigana en] fail: {ex}\n")
        return chars, p.rect.width, p.rect.height, furigana
    finally:
        doc.close()


_CHAR_CACHE_VER = 8   # chars 缓存 schema 版本。改抽取/分词逻辑就 +1 → 旧缓存全部失效重算
                      # (v2:坏缓存; v3:完整读音; v4:动词链+日计数器; v5:サ变+wd; v6:量词 furigana 加 ctx;
                      #  v7:按块分词[跨行词如 間食 读对音] + 跨行 token 振假名只放首行段;
                      #  v8:块内 gutter 切列[漫画并排气泡分开 bk]→ 选中/分词不串气泡)


def _page_chars_cached(abs_path, rel: str, page: int):
    """带磁盘缓存的 chars 提取。缓存键含 mtime → PDF 改了自动失效。
    缓存的是「只依赖文件+页」的不变部分(chars/page_w/page_h);vocab_marks/句子框
    依赖可变的掌握度/译文/删除态,不缓存,每次从 chars 现算(便宜,无 fitz/rawdict)。
    返回 (chars, page_w, page_h) 或 None。"""
    import hashlib
    try:
        mtime = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mtime = 0
    cdir = CLAUDE_DIR / "state" / "pdf-char-cache"
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    cpath = cdir / f"{sha}-p{page}-{mtime}.json"
    if cpath.exists():
        try:
            d = json.loads(cpath.read_text("utf-8"))
            if d.get("cver") == _CHAR_CACHE_VER:   # 版本不符(或老缓存) → 落到重算
                return d["chars"], d["page_w"], d["page_h"], d.get("furigana", [])
        except Exception:
            pass   # 缓存损坏 → 重算
    res = _compute_page_chars(abs_path, page)
    if res is None:
        return None
    chars, pw, ph, furigana = res
    # 安全阀:别缓存「分词失败」的结果。fugashi 不可用(tagger None) → 该页 CJK 全 w=-1;
    # 或有汉字却没产出振假名 = 分词没跑成。否则 fugashi 临时挂掉时写的坏缓存(全 w=-1)会被
    # 之后一直命中 → 整页单字选中(本次 bug 根因)。纯假名页(无汉字)无振假名属正常,不拦。
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
        _merge_favorite_phrases(chars)   # 收藏词组合并 w（单击选中整词组）；live 应用不进缓存
        # 生成 vocab_marks + 含 ≥2 未掌握词的句子框(依赖可变状态,每次现算)
        _aj = _page_allows_ja(chars, rel)   # 该页是否日语(中文书纯汉字 → 否,免撞日语词库)
        vocab_marks = _build_vocab_marks(chars)
        if _aj:
            vocab_marks += _build_jp_vocab_marks(chars)   # 日语生词下划线(查过的 JP 词)
        sentences = _build_unmastered_sentences(chars, page_h=page_h, allow_ja=_aj)
        for ts in _tr_load(rel):   # 合并该页手动翻译过的句子(持久 sidecar，带译文)
            if ts.get("rects") and ts.get("page", page) == page:
                sentences.append(ts)
        _dis = _dismiss_load(rel)   # 过滤用户长按 L 框删除的句子标记
        if _dis:
            sentences = [s for s in sentences if (s.get("text") or "").strip() not in _dis]
        resp = jsonify({
            "ok": True,
            "chars": chars,
            "page_w": page_w,
            "page_h": page_h,
            "vocab_marks": vocab_marks,
            "vocab_sentences": sentences,
            "furigana": furigana,
        })
        # 动态数据绝不缓存:之前无 Cache-Control,iOS Safari 启发式缓存了旧版(分词上线前
        # 每字 w 各自独立)→ 整页单字。no-store 杜绝;前端再加 ?v=mtime 换 cache key 立即生效。
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
        while j < n and chars[j].get("w") == wid and not chars[j].get("sp"):
            toks.append(chars[j]); j += 1
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


def _merge_favorite_phrases(chars: list[dict]) -> None:
    """把收藏词组在该页出现处的 chars 合并成同一个 w（含内部空格）→ 单击选词时整词组一起选。
    空白不敏感 + ASCII 大小写不敏感匹配；按收藏长度降序优先，used 标记防重叠。"""
    favs = _phrases_load()
    if not favs or not chars:
        return
    nonsp = [(i, ch.get("c", "")) for i, ch in enumerate(chars) if not ch.get("sp")]
    if not nonsp:
        return
    compact = "".join(c for _i, c in nonsp).lower()
    cidx = [i for i, _c in nonsp]
    used = [False] * len(chars)
    wn = 0
    for ph in sorted(set(favs), key=len, reverse=True):
        p = re.sub(r"\s+", "", ph).lower()
        if len(p) < 2:
            continue
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
            start = k + 1


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
    # 预翻译：cache 命中秒返回；未命中调 MyMemory ~300ms/句（同步在这里阻塞）
    # 第一次访问该页慢一点，但后续命中 cache 一次就秒了
    try:
        import sys as _sys
        vp = CLAUDE_DIR / "scripts" / "vocab"
        if str(vp) not in _sys.path:
            _sys.path.insert(0, str(vp))
        from translate import translate as _tr   # type: ignore
        for s in sentences:
            t = s.get("text") or ""
            if t:
                zh = _tr(t)
                if zh:
                    s["zh"] = zh
    except Exception:
        pass
    return sentences


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
        # 3) Google 没 key / 整体失败 → 逐句兜底（auto 链：deepl/mymemory）
        still = [i for i in miss_idx if not zhs[i]]
        for i in still[:60]:   # 限量兜底，避免极端长页阻塞
            try:
                z = _tr(texts[i])
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
    """轻量路由：仅返回该页 vocab_marks（不返回 chars）。
    用户查词后用来立刻刷新下划线，不需要重传整页 chars。"""
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    abs_path = _safe_vault_path(rel)
    if not abs_path or page < 1:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        import fitz
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF missing"}), 500
    doc = None
    try:
        doc = fitz.open(str(abs_path))
        if page > len(doc):
            return jsonify({"ok": False, "error": "page out of range"}), 400
        p = doc[page - 1]
        raw = p.get_text("rawdict")
        chars = []
        _col_seq = 0   # 跟 _compute_page_chars 一致:块内 gutter 切列,每列唯一 bk(并排气泡分开)
        for _bi, b in enumerate(raw.get("blocks", [])):
            if b.get("type") != 0: continue
            _bs = len(chars)
            for _li, ln in enumerate(b.get("lines", [])):
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        bb = ch.get("bbox")
                        if not bb: continue
                        c = ch.get("c", "")
                        if not c: continue
                        c = _LIGATURES.get(c, c)   # 还原连字 ﬁ→fi，免得词在此被断
                        chars.append({
                            "c": c, "x0": round(bb[0],2), "y0": round(bb[1],2),
                            "x1": round(bb[2],2), "y1": round(bb[3],2),
                            "sp": 1 if c.isspace() else 0,
                            "bk": _bi,   # 块号:下面按列覆盖。句子检测/选中靠它(并排气泡分列才不串)
                        })
            for _col in _split_block_columns(chars[_bs:]):
                cur_bk = _col_seq; _col_seq += 1
                for c in _col: c["bk"] = cur_bk
                _apply_jp_tokenize(_col, _bi, cur_bk % 1000)   # CJK 列补 w（JP 生词下划线要按词）
        _merge_favorite_phrases(chars)   # 收藏词组合并 w
        # 强制刷新 vocab_index（防 vocab note 刚写完缓存还旧）
        try:
            import sys as _sys
            vp = CLAUDE_DIR / "scripts" / "vocab"
            if str(vp) not in _sys.path:
                _sys.path.insert(0, str(vp))
            import vocab_index   # type: ignore
            vocab_index.index(force_reload=True)
        except Exception:
            pass
        _aj = _page_allows_ja(chars, rel)   # 中文书纯汉字 → 不当日语,免撞日语词库
        marks = _build_vocab_marks(chars)
        if _aj:
            marks += _build_jp_vocab_marks(chars)   # 日语生词下划线
        sentences = _build_unmastered_sentences(chars, page_h=p.rect.height, allow_ja=_aj)
        # **必须跟 page-chars 路由一致**：合并手动翻译句 sidecar + 过滤已删除句。
        # 否则 refreshVocabUnderlinesForAllPages(查词后调) 会用「只有自动生词句」覆盖前端
        # __vocabSentences → 手动翻译形成的 L 框/边框被刷没（本次 bug 根因）。
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
            "page_w": p.rect.width,
            "page_h": p.rect.height,
        })
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    finally:
        if doc:
            try: doc.close()
            except Exception: pass


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


@bp.route("/api/page-nodes")
def pdf_api_page_nodes():
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "0") or "0")
    if not rel or page < 1:
        return jsonify({"nodes": []})
    return jsonify({"nodes": _find_kg_nodes_for_page(rel, page)})


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
        jp = ds.lookup_jp(_lookup, context=context)
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
        raw = _ai_call(prompt, "haiku", "low")
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
    jp = ds.lookup_jp(_base, context=request.args.get("context", ""))
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


@bp.route("/api/dict-jp-ai")
def pdf_api_dict_jp_ai():
    """日语词「✨AI 深入讲解」SSE 流式:用法/语感/近义辨析/汉字记忆法。按需触发,耗 AI。"""
    word = (request.args.get("word", "") or "").strip()
    context = request.args.get("context", "")
    if not word:
        return jsonify({"ok": False, "error": "no word"})
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
    return _start_ai_stream(prompt, "", "", (request.args.get("rid") or "").strip())


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
    model = (body.get("model") or "").strip()
    effort = (body.get("effort") or "").strip()
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        return _start_ai_stream(prompt, model, effort, (body.get("rid") or "").strip())
    try:
        out = _ai_call(prompt, model, effort).strip()
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


@bp.route("/api/upload", methods=["POST"])
def pdf_api_upload():
    """上传 PDF 到 vault 子目录。multipart form：file + target_dir（默认 资源/uploads/）。"""
    f = request.files.get("file")
    target_dir = (request.form.get("target_dir") or "资源/uploads").strip().strip("/")
    if not f:
        return jsonify({"ok": False, "error": "未选择文件"}), 400
    fname = f.filename or ""
    if not fname.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "仅支持 PDF 文件"}), 400
    # 防 path traversal
    if ".." in target_dir.split("/") or target_dir.startswith("/"):
        return jsonify({"ok": False, "error": "目标目录非法"}), 400
    # 清理文件名
    safe_name = _sanitize_filename(Path(fname).stem) + ".pdf"
    dest_dir = (OBSIDIAN_ROOT / target_dir).resolve()
    try:
        dest_dir.relative_to(OBSIDIAN_ROOT.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "目标目录超出 vault"}), 400
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    if dest.exists():
        stem = safe_name[:-4]
        for i in range(1, 200):
            cand = dest_dir / f"{stem}-{i}.pdf"
            if not cand.exists():
                dest = cand; break
    try:
        f.save(str(dest))
    except Exception as ex:
        return jsonify({"ok": False, "error": f"保存失败：{ex}"}), 500
    rel = dest.relative_to(OBSIDIAN_ROOT).as_posix()
    return jsonify({"ok": True, "rel": rel, "view_url": f"/pdf/view?file={urllib.parse.quote(rel, safe='/')}"})


def _sanitize_filename(s: str) -> str:
    """去非法字符 + 长度限制（同 qa_browser._sanitize_note_name）。"""
    import re as _re
    s = (s or "").strip().strip(".")
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s)
    return s[:120] or "untitled"


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
    model = (body.get("model") or "").strip()
    effort = (body.get("effort") or "").strip()
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        return _start_ai_stream(prompt, model, effort, (body.get("rid") or "").strip())
    try:
        out = _ai_call(prompt, model, effort).strip()
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

def _ai_stream_worker(rid: str, prompt: str, model: str, effort: str):
    acc = []
    try:
        for chunk in _ai_call_stream(prompt, model, effort):
            if chunk:
                acc.append(chunk)
                _aistream_update(rid, "".join(acc))
        _aistream_update(rid, "".join(acc), status="done")
    except Exception as e:
        _aistream_update(rid, "".join(acc), status="error", error=str(e))

def _start_ai_stream(prompt: str, model: str = "", effort: str = "", rid: str = ""):
    """启动后台 AI 生成线程 + 返回「tail」SSE Response（边生成边推，且断连后线程继续跑完）。
    rid 为空 → 退化成原行内 _sse_stream（不抗断，兼容老前端）。"""
    from flask import Response, stream_with_context
    if not rid:
        return Response(stream_with_context(_sse_stream(prompt, model, effort)),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    import json as _json
    _aistream_init(rid)
    _threading.Thread(target=_ai_stream_worker, args=(rid, prompt, model, effort), daemon=True).start()

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
        "note_name": note_name, "model": body.get("model") or "", "effort": body.get("effort") or "",
    }, None


def _run_snippets_to(snippets, make_note, make_anki, note_name, model, effort) -> dict:
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
            content = _ai_call(prompt, model or "", effort or "").strip()
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
            sys.path.insert(0, str(CLAUDE_DIR / "_client" / "core"))
            from ai_backends import make_backend  # type: ignore
            sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
            sys.path.insert(0, str(CLAUDE_DIR / "_server_deploy"))
            from qa_server import get_cfg
            cfg = get_cfg()
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
            backend_name = cfg.get("ai_backend", "claude_cli")
            settings = dict((cfg.get("ai") or {}).get(backend_name, {}))
            if model:  settings["model"] = model
            if effort: settings["effort"] = effort
            ad = make_backend(backend_name, settings)
            raw = ad.chat([
                {"role": "system", "content": "你是 Anki 卡片生成器，严格只输出 JSON。"},
                {"role": "user", "content": prompt},
            ])
            # 提取 JSON
            s_idx = raw.find("{"); e_idx = raw.rfind("}")
            cards_data = json.loads(raw[s_idx:e_idx+1]) if s_idx >= 0 else {"cards": []}
            cards = cards_data.get("cards") or []
            # 通过 AnkiConnect 加入 Anki（deck 用 "QA"）
            import urllib.request
            ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
            added = 0
            for c in cards:
                ctype = (c.get("type") or "basic").lower()
                if ctype == "cloze":
                    fields = {"Text": c.get("text", ""), "Back Extra": ""}
                    model_name = "Cloze"
                else:
                    fields = {"Front": c.get("front", ""), "Back": c.get("back", "")}
                    model_name = "Basic"
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
                except Exception:
                    pass
            out["anki_added"] = added
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
    return jsonify({"ok": True, **_ink_load(rel)})


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
    model = (data.get("model") or "").strip()
    effort = (data.get("effort") or "").strip()
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


def _spacy_grammar(sentence: str) -> dict | None:
    """用 spaCy venv 子进程做词性 + 依存分析；ECDICT 补每词中文义、MyMemory 译整句。
    返回 {tokens, deps, sentence_zh}；失败返回 None（调用方回退 AI）。"""
    import subprocess
    try:
        r = subprocess.run(
            [str(SPACY_PY), str(SPACY_SCRIPT), sentence],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        parsed = json.loads(r.stdout)
    except Exception:
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
        _zh_cache: dict[str, str] = {}
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
            return jsonify({"ok": True, "from_cache": True, **json.loads(cache_p.read_text("utf-8"))})
        except Exception:
            pass

    # ── 优先 spaCy 本地分析（词性 + 依存，零 AI、秒级）──
    if _spacy_available():
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
                cache_p.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
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
    model = (data.get("model") or "sonnet").strip()
    effort = (data.get("effort") or "low").strip()
    try:
        zh = _ai_call(prompt, override_model=model, override_effort=effort)
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
    cache_p.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
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
    model = (data.get("model") or "sonnet").strip()
    effort = (data.get("effort") or "low").strip()
    return _start_ai_stream(prompt, model, effort, (data.get("rid") or "").strip())


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


def _vocab_ecdict_zh(lemma: str) -> str:
    try:
        vp = str(CLAUDE_DIR / "scripts" / "vocab")
        if vp not in sys.path:
            sys.path.insert(0, vp)
        import dict_sources  # type: ignore
        ec = dict_sources.lookup_ecdict(lemma)
        if ec:
            for d in dict_sources._ec_definitions(ec):
                if d.get("zh"):
                    return d["zh"][:40]
    except Exception:
        pass
    return ""


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
    # 全部笔记 frontmatter
    notes = {}
    for p in vroot.rglob("*.md"):
        if p.parent.name == "_audio":
            continue
        fm = _vocab_read_fm(p)
        lemma = (fm.get("lemma") or fm.get("word") or p.stem).strip().lower()
        if lemma:
            notes[lemma] = fm
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
    app.register_blueprint(bp)
