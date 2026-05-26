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


def _ai_call(prompt: str) -> str:
    """复用 qa_server 的 AI 后端（read server-config，跑当前激活的 backend）。"""
    sys.path.insert(0, str(CLAUDE_DIR / "_client" / "core"))
    from ai_backends import make_backend  # type: ignore
    sys.path.insert(0, str(CLAUDE_DIR / "scripts"))

    # 复用 qa_server.get_cfg（读 server-config.json）
    sys.path.insert(0, str(CLAUDE_DIR / "_server_deploy"))
    try:
        from qa_server import get_cfg
        cfg = get_cfg()
    except Exception:
        cfg = {"ai_backend": "claude_cli", "ai": {"claude_cli": {"command": "/usr/bin/claude"}}}
    backend_name = cfg.get("ai_backend", "claude_cli")
    settings = (cfg.get("ai") or {}).get(backend_name, {})
    ad = make_backend(backend_name, settings)
    return ad.chat([
        {"role": "system", "content": "你是一个学习辅助助手。回答简洁、准确。数学公式用 $...$ 或 $$...$$，不要用反引号包数学。"},
        {"role": "user", "content": prompt},
    ])


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
        return jsonify({
            "ok": True,
            "chars": chars,
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

@bp.route("/api/dict")
def pdf_api_dict():
    """ECDICT 离线英汉字典查询。GET ?word=X → {ok, word, phonetic, translation, definition}"""
    word = (request.args.get("word") or "").strip().lower()
    if not word or len(word) > 50:
        return jsonify({"ok": False, "error": "invalid word"}), 400
    if not _DICT_DB_PATH.exists():
        return jsonify({"ok": False, "error": "dict db missing"}), 500
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{_DICT_DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT word, phonetic, translation, definition, exchange FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1", (word,))
        row = cur.fetchone()
        # 没命中：查 exchange 表（屈折形态 → 原型）
        if not row:
            cur.execute("SELECT word, phonetic, translation, definition, exchange FROM stardict WHERE exchange LIKE ? LIMIT 1",
                        (f"%0:{word}%",))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error": "not found"})
        return jsonify({
            "ok": True,
            "word": row[0], "phonetic": row[1] or "",
            "translation": row[2] or "", "definition": row[3] or "",
            "exchange": row[4] or "",
        })
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


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
    try:
        out = _ai_call(prompt).strip()
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
    prompt = (
        "解释下面这段教材内容（这段文字来自 PDF 教材，可能含数学公式和符号）。\n"
        "要求：\n"
        "1. 用通俗语言重述，但保留专业术语\n"
        "2. 涉及定义/定理时点出关键概念之间的关系\n"
        "3. 必要时给一个直观例子或类比（如果是数学/物理）\n"
        "4. 用 Markdown 输出，公式用 $...$ 或 $$...$$\n"
        "5. 不要复述原文逐字，要提炼\n\n"
    )
    if ctx:
        prompt += f"=== 上下文（前后段落）===\n{ctx[:2000]}\n\n"
    prompt += f"=== 待解释 ===\n{text}"
    try:
        out = _ai_call(prompt).strip()
        return jsonify({"ok": True, "explanation": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI 解释失败：{ex}"}), 500


def register_pdf_reader(app):
    app.register_blueprint(bp)
