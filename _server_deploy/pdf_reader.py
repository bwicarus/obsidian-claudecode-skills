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
import sys
import urllib.parse
from pathlib import Path

from flask import (
    Blueprint, abort, jsonify, render_template, request,
    send_file,
)

# AI 后端复用 _client/core 的 ai_backends + scripts/ai_client
# 同 skilltree.py 已经在 app.py 启动时把 sys.path 加好了

CLAUDE_DIR    = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
OBSIDIAN_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))

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


def _list_vault_pdfs() -> list[dict]:
    """扫 vault 下所有 PDF。返回 [{rel, name, size_kb, mtime}, ...]，按修改时间倒序。"""
    out = []
    for p in OBSIDIAN_ROOT.rglob("*.pdf"):
        try:
            rel = p.relative_to(OBSIDIAN_ROOT).as_posix()
            st = p.stat()
            out.append({
                "rel": rel,
                "name": p.name,
                "dir": str(Path(rel).parent),
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": int(st.st_mtime),
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

        # 2. Free Dictionary（在线 ~500ms，有缓存秒命中）
        try:
            fd_raw = ds.lookup_free_dict(lemma)
            fd = ds._free_dict_unpack(fd_raw)
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

        # 3. MW Learner（在线 ~500ms）
        try:
            mw_raw = ds.lookup_mw_learner(lemma)
            mw = ds._mw_unpack(mw_raw, lemma=lemma)
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

        # 4. 例句翻译（每翻 1 句 yield 1 次，让用户看到逐步进度）
        try:
            from translate import translate as _tr
            # 收集所有可翻译例句：MW + Free Dict + ECDICT 例句池（前 8 条）
            all_examples = []
            seen = set()
            for d_list in [
                mw_payload.get("definitions_en", []) if 'mw_payload' in locals() else [],
                fd_payload.get("definitions_en", []) if 'fd_payload' in locals() else [],
            ]:
                for d in d_list:
                    for ex in (d.get("examples") or [])[:2]:
                        k = ex.lower()[:60]
                        if k and k not in seen:
                            seen.add(k); all_examples.append(ex)
            for ex in (fd_payload.get("examples", []) if 'fd_payload' in locals() else [])[:5]:
                k = ex.lower()[:60]
                if k and k not in seen:
                    seen.add(k); all_examples.append(ex)
            for ex in all_examples[:8]:
                zh = _tr(ex)
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


@bp.route("/view")
def pdf_view():
    rel = request.args.get("file", "")
    page = int(request.args.get("page", "1") or "1")
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        abort(404)
    from flask import make_response
    resp = make_response(render_template(
        "pdf_reader.html",
        file_rel=rel,
        file_name=Path(rel).name,
        page=page,
        pdf_url=f"/pdf/file/{urllib.parse.quote(rel, safe='/')}",
    ))
    # HTML 还在快速迭代，强制每次拿新版（避免浏览器缓存旧 JS 报语法错）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@bp.route("/file/<path:rel>")
def pdf_file(rel):
    abs_path = _safe_vault_path(rel)
    if not abs_path:
        abort(404)
    return send_file(str(abs_path), mimetype="application/pdf")


@bp.route("/api/list-pdfs")
def pdf_api_list_pdfs():
    """vault 里所有 PDF 的列表（控制面板新建书本下拉用）。"""
    return jsonify({"ok": True, "pdfs": _list_vault_pdfs()})


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
        import fitz
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF not installed"}), 500
    doc = None
    try:
        doc = fitz.open(str(abs_path))
        if page > len(doc):
            return jsonify({"ok": False, "error": "page out of range"}), 400
        p = doc[page - 1]
        raw = p.get_text("rawdict")
        chars = []
        for block in raw.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        bbox = ch.get("bbox")
                        if not bbox or len(bbox) != 4:
                            continue
                        c = ch.get("c", "")
                        if not c:
                            continue
                        # 保留空格（拼接选中文本时需要），但用 sp 标记免得占点击命中
                        chars.append({
                            "c": c,
                            "x0": round(bbox[0], 2), "y0": round(bbox[1], 2),
                            "x1": round(bbox[2], 2), "y1": round(bbox[3], 2),
                            "sp": 1 if c.isspace() else 0,
                        })
        # 生成 vocab_marks + 含 ≥2 未掌握词的句子框
        vocab_marks = _build_vocab_marks(chars)
        sentences = _build_unmastered_sentences(chars)
        return jsonify({
            "ok": True,
            "chars": chars,
            "page_w": p.rect.width,
            "page_h": p.rect.height,
            "vocab_marks": vocab_marks,
            "vocab_sentences": sentences,
        })
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    finally:
        if doc:
            try: doc.close()
            except Exception: pass


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


def _build_unmastered_sentences(chars: list[dict], threshold: int = 3, min_words: int = 10) -> list[dict]:
    """识别需要标注的句子。判定条件：
      - 至少 threshold 个未掌握 lemma（默认 3）
      - 句子总词数 > min_words - 1（默认 10，即 ≥ 10 词）
    返回 [{text, rects, lemmas, count, total_words, last_char}]
    句子边界 = . ! ? 。！？ / 列表标记 • / 段落分界（行间距 > 1.5× 行高）
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
    # 未掌握的 forms 映射到 lemma
    form_to_lemma_unmastered = {
        form: info["lemma"]
        for form, info in idx.items()
        if info.get("label_slug") and info["label_slug"] != "mastered"
    }

    sentences: list[dict] = []
    cur_chars: list[dict] = []
    cur_lemmas: set[str] = set()
    cur_word_letters: list[str] = []
    cur_total_words: int = 0

    def _flush_word():
        nonlocal cur_word_letters, cur_total_words
        if cur_word_letters:
            w = "".join(cur_word_letters).lower()
            if w in form_to_lemma_unmastered:
                cur_lemmas.add(form_to_lemma_unmastered[w])
            # 计数实际单词（不限英文）
            if len(w) >= 2 or w.isalpha():
                cur_total_words += 1
        cur_word_letters = []

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
            if nsp:
                bw = max(c["x1"] for c in nsp) - min(c["x0"] for c in nsp)
                bh = max(c["y1"] for c in nsp) - min(c["y0"] for c in nsp)
                vertical = bh > bw * 1.6
            if not vertical:
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

    prev = None
    pending_period = False   # 上一字符是 .，需要看下一字符决定切不切
    for ch in chars:
        c = ch.get("c", "")
        # 处理 pending period：根据当前字符决定上一个 . 是否真切句
        if pending_period:
            is_continuation = (
                (not ch.get("sp"))
                and len(c) == 1
                and (c.isdigit() or (c.isalpha() and c.islower()))
            )
            if not is_continuation:
                _flush_word()
                _flush_sentence()
            pending_period = False
        # 跨行检测：行间距 > 1.5× 行高 → 段落分界（新句）
        if prev and not prev.get("sp") and not ch.get("sp"):
            prev_h = max(0.1, prev["y1"] - prev["y0"])
            line_gap = ch["y0"] - prev["y0"]
            if line_gap > prev_h * 1.5:
                # 大段落间距 → 切句
                _flush_word()
                _flush_sentence()
            elif abs(line_gap) > prev_h * 0.5:
                # 普通跨行：拼词处理
                if cur_word_letters and cur_word_letters[-1] == "-":
                    cur_word_letters.pop()
                else:
                    _flush_word()
        # 列表标记 → 切句（每个列表项独立句）
        if c in "•▪▶◆●○◇":
            _flush_word()
            _flush_sentence()
            cur_chars.append(ch); prev = ch; continue
        if ch.get("sp"):
            _flush_word()
            cur_chars.append(ch); prev = ch; continue
        # 句末标点
        if c in "!?。！？":
            _flush_word()
            cur_chars.append(ch)
            _flush_sentence()
            prev = ch; continue
        if c == ".":
            _flush_word()
            cur_chars.append(ch)
            pending_period = True   # 推迟切句决策到下一字符
            prev = ch; continue
        if c.isalpha() or c in "'-":
            cur_word_letters.append(c)
        else:
            _flush_word()
        cur_chars.append(ch)
        prev = ch
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
        for b in raw.get("blocks", []):
            if b.get("type") != 0: continue
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        bb = ch.get("bbox")
                        if not bb: continue
                        c = ch.get("c", "")
                        if not c: continue
                        chars.append({
                            "c": c, "x0": round(bb[0],2), "y0": round(bb[1],2),
                            "x1": round(bb[2],2), "y1": round(bb[3],2),
                            "sp": 1 if c.isspace() else 0,
                        })
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
        marks = _build_vocab_marks(chars)
        sentences = _build_unmastered_sentences(chars)
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
        from flask import Response, stream_with_context
        return Response(stream_with_context(_sse_stream(prompt, model, effort)),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
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
        from flask import Response, stream_with_context
        return Response(stream_with_context(_sse_stream(prompt, model, effort)),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    try:
        out = _ai_call(prompt, model, effort).strip()
        return jsonify({"ok": True, "explanation": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI 解释失败：{ex}"}), 500


@bp.route("/api/snippets-to", methods=["POST"])
def pdf_api_snippets_to():
    """从用户在 AI 回答里勾选的段落 → 创建笔记 / Anki 卡 / 两者。

    body: {
      snippets: [{text, source}],
      make_note: bool, make_anki: bool,
      note_name: str（make_note 时必填）,
      model, effort
    }
    """
    body = request.get_json(silent=True) or {}
    snippets = body.get("snippets") or []
    make_note = bool(body.get("make_note"))
    make_anki = bool(body.get("make_anki"))
    note_name = (body.get("note_name") or "").strip()
    if not snippets:
        return jsonify({"ok": False, "error": "无选中段落"}), 400
    if not (make_note or make_anki):
        return jsonify({"ok": False, "error": "至少选一个动作"}), 400
    if make_note and not note_name:
        return jsonify({"ok": False, "error": "笔记名不能为空"}), 400
    out = {"ok": True}
    # ── 创建笔记 ──
    if make_note:
        if not OBSIDIAN_ROOT:
            return jsonify({"ok": False, "error": "VAULT 未配置"}), 500
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
            content = _ai_call(prompt, body.get("model") or "", body.get("effort") or "").strip()
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
            return jsonify({"ok": False, "error": f"笔记创建失败：{ex}"}), 500
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
            if body.get("model"):  settings["model"] = body["model"]
            if body.get("effort"): settings["effort"] = body["effort"]
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
                    model = "Cloze"
                else:
                    fields = {"Front": c.get("front", ""), "Back": c.get("back", "")}
                    model = "Basic"
                req = json.dumps({
                    "action": "addNote", "version": 6,
                    "params": {"note": {
                        "deckName": "QA",
                        "modelName": model,
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
        except Exception as ex:
            out["anki_error"] = str(ex)
    return jsonify(out)


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

@bp.route("/api/translate-sentence", methods=["POST"])
def pdf_api_translate_sentence():
    """body: {text, backend?, model?, effort?} → {ok, zh}
    backend 留空时按 server-config.dict.translate_backend 默认（auto = deepl → mymemory）。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text or len(text) > 2000:
        return jsonify({"ok": False, "error": "no text / too long"}), 400
    backend = (data.get("backend") or "").strip()
    model = (data.get("model") or "").strip()
    effort = (data.get("effort") or "").strip()
    import sys
    vp = CLAUDE_DIR / "scripts" / "vocab"
    if str(vp) not in sys.path:
        sys.path.insert(0, str(vp))
    try:
        from translate import translate as _tr  # type: ignore
        zh = _tr(text, backend=backend, model=model, effort=effort)
        if zh:
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
            "model": d.get("translate_model", "haiku"),
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
    except Exception:
        pass
    # 整句翻译 + 语法点讲解交给 AI 流式（/api/grammar-stream，翻译标志先出）
    # 这里只出词性 + 依存 + 子句切分，秒级零 AI
    return {"tokens": tokens, "deps": deps, "clauses": clauses, "sentence_zh": ""}


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
    model = (data.get("model") or "haiku").strip()
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
    model = (data.get("model") or "haiku").strip()
    effort = (data.get("effort") or "low").strip()
    return Response(
        stream_with_context(_sse_stream(prompt, model, effort)),
        mimetype="text/event-stream",
    )


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
    # book scope：本 PDF 查过的 lemma
    target = set(notes.keys())
    if scope == "book" and file:
        book = set()
        log = CLAUDE_DIR / "state" / "vocab-lookups.jsonl"
        if log.exists():
            for line in log.read_text("utf-8").splitlines():
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("pdf") == file and j.get("lemma"):
                    book.add(j["lemma"].strip().lower())
        # 也并入「反向索引里在本书出现 + 有笔记」的词
        for lem, ex in exposure.items():
            ll = lem.strip().lower()
            if ll in notes:
                for pg in (ex.get("pages") or []):
                    if pg.get("pdf") == file:
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


@bp.route("/api/vocab-add-anki", methods=["POST"])
def pdf_api_vocab_add_anki():
    """对一个 lemma 生成 Anki 单词卡（调 scripts/vocab/anki_from_word.py）。"""
    import subprocess
    data = request.get_json(silent=True) or {}
    lemma = (data.get("lemma") or "").strip()
    if not lemma:
        return jsonify({"ok": False, "error": "no lemma"}), 400
    script = CLAUDE_DIR / "scripts" / "vocab" / "anki_from_word.py"
    try:
        r = subprocess.run(
            [sys.executable, str(script), lemma],
            capture_output=True, text=True, timeout=90,
        )
        out = {}
        if r.stdout.strip():
            try: out = json.loads(r.stdout)
            except Exception: out = {"raw": r.stdout[-400:]}
        if not out.get("ok") and r.returncode != 0:
            return jsonify({"ok": False, "error": (r.stderr or r.stdout or "anki 失败")[-300:]}), 500
        return jsonify({"ok": out.get("ok", r.returncode == 0), **out})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


def register_pdf_reader(app):
    app.register_blueprint(bp)
