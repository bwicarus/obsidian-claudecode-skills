"""
skilltree.py — /skilltree/<book>/ 知识图谱可视化路由 + 编辑 API。

模式跟 control.py 一致：register_skilltree(app) 注册路由。
KG 数据 = CLAUDE_PROJECT/knowledge_graph/<book>.json
"""
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import abort, jsonify, render_template, request, send_file

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
KG_DIR = CLAUDE_DIR / "knowledge_graph"


def list_books():
    out = []
    if not KG_DIR.exists():
        return out
    for f in sorted(KG_DIR.glob("*.json")):
        if f.name.endswith(".bak.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": d.get("book") or f.stem,
            "file": f.name,
            "nodes": len(d.get("nodes", [])),
            "edges": len(d.get("edges", [])),
        })
    return out


def _kg_path(book):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "", book) or "book"
    p = KG_DIR / f"{safe}.json"
    return p if p.exists() else None


def _load_kg(book):
    p = _kg_path(book)
    if not p:
        return None, None
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return p, None


def _save_kg(p, kg):
    """先写临时文件再 rename，避免半写状态。"""
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


_edit_lock = threading.Lock()


# ─── PDF→笔记 辅助函数 ────────────────────────────────────────────────────
TYPE_ZH = {"definition":"定义", "theorem":"定理", "proposition":"命题",
           "corollary":"推论", "lemma":"引理", "example":"例子"}

def _safe_filename(s):
    return re.sub(r'[\\/:*?"<>|]', '', s or "新节点")[:50]

def _rand_hex3():
    import random
    return f"{random.randint(0, 0xfff):03X}"

def _find_node_blocks_in_page(page, numeric_label, max_blocks=8):
    """在 PDF page 里找含 numeric_label 的 text blocks + 后续相关 block。
    返回 [{text, bbox}, ...]。pymupdf 坐标系跟 obsidian 一致。"""
    blocks = page.get_text("dict").get("blocks", [])
    text_blocks = [b for b in blocks if b.get("type") == 0]   # type=0 文本块
    # 拼出每块的纯文本 + bbox
    items = []
    for b in text_blocks:
        text = ""
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                text += span.get("text", "") + " "
            text += "\n"
        items.append({"text": text.strip(), "bbox": b.get("bbox")})
    if not numeric_label:
        return items[:max_blocks]
    # 找包含 numeric_label 的块
    start = -1
    for i, it in enumerate(items):
        if numeric_label in it["text"]:
            start = i
            break
    if start < 0:
        return []
    # 从 start 开始取后续 N 块，直到遇到下一个明显的 numeric_label 或上限
    # 简化判别：下一个块开头出现 X.YY (两段数字+点) 即新节点
    import re as _re
    out = [items[start]]
    next_label_pat = _re.compile(r"^\s*\d+\.\d+")
    for j in range(start + 1, min(start + max_blocks + 1, len(items))):
        t = items[j]["text"]
        if next_label_pat.match(t) and not t.startswith(numeric_label):
            break
        out.append(items[j])
    return out

def _union_bbox(boxes):
    """合并多个 (x0, y0, x1, y1) → 外包矩形。坐标取整。"""
    if not boxes: return None
    xs0 = min(b[0] for b in boxes); ys0 = min(b[1] for b in boxes)
    xs1 = max(b[2] for b in boxes); ys1 = max(b[3] for b in boxes)
    return [int(xs0), int(ys0), int(xs1), int(ys1)]

def _build_note_for_node(kg, node, pdf_abs_path: Path):
    """生成笔记文件并返回 (note_rel_path, covered_node_ids)。"""
    import fitz
    vault_root = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
    pdf_vault_path = pdf_abs_path.resolve().relative_to(vault_root.resolve()).as_posix()
    pdf_name = pdf_abs_path.name
    book = kg.get("book", "?")
    id2 = {n["id"]: n for n in kg["nodes"]}
    chap_of = lambda nid: id2.get(id2.get(nid, {}).get("parent_id", ""), {})

    # 决定要处理哪些节点：L1 → 该节所有 L2；L2 → 仅自己
    if node["level"] == 1:
        targets = [n for n in kg["nodes"] if n["level"]==2 and n.get("parent_id")==node["id"]]
        targets.sort(key=lambda n: (n.get("pages") or [99999])[0])
        title_node = node
    elif node["level"] == 2:
        targets = [node]
        title_node = node
    else:
        raise RuntimeError("仅支持 L1/L2 节点建笔记")
    if not targets:
        raise RuntimeError("无 L2 节点可建笔记")

    # 打开 PDF
    doc = fitz.open(str(pdf_abs_path))
    sections_md = []
    covered_ids = []
    for t in targets:
        covered_ids.append(t["id"])
        pages = t.get("pages") or []
        numeric_label = t.get("numeric_label", "")
        tyz = TYPE_ZH.get(t.get("type"), t.get("type") or "")
        sec_parts = [f"## {numeric_label} {t['name']}"]
        if tyz:
            sec_parts.append(f"*[{tyz}]*")
        sec_parts.append("")
        # 对每个 page 提取并嵌入
        embedded = False
        for pg in pages:
            if pg < 1 or pg > len(doc): continue
            page = doc[pg - 1]
            blocks = _find_node_blocks_in_page(page, numeric_label)
            if blocks:
                bbox = _union_bbox([b["bbox"] for b in blocks if b.get("bbox")])
                if bbox:
                    rect_str = ",".join(map(str, bbox))
                    sec_parts.append(f"![[{pdf_name}#page={pg}&rect={rect_str}&color=yellow]]")
                    embedded = True
                    # 文本层节录（前 400 字）作 fallback 描述
                    raw = "\n".join(b["text"] for b in blocks)
                    if raw.strip():
                        sec_parts.append("")
                        sec_parts.append(f"> {raw[:400].replace(chr(10), chr(10)+'> ')}")
                    continue
            # fallback：无 block 匹配，嵌整页
            sec_parts.append(f"![[{pdf_name}#page={pg}]]")
            embedded = True
        if not embedded:
            sec_parts.append("*（PDF 找不到对应内容，请手动补充）*")
        # 节点 summary 也写上作快速参考
        if t.get("summary"):
            sec_parts.append("")
            sec_parts.append(f"**摘要**：{t['summary']}")
        sec_parts.append("")
        sec_parts.append("")
        sections_md.append("\n".join(sec_parts))
    doc.close()

    # 拼装最终笔记
    chap = chap_of(title_node["id"]) if title_node["level"]==2 else id2.get(title_node.get("parent_id",""), {})
    title = title_node["name"] if title_node["level"]==1 else (title_node.get("numeric_label","") + " " + title_node["name"])
    all_pages = sorted(set(p for t in targets for p in (t.get("pages") or [])))
    fm_lines = [
        "---",
        "subject: 数学",
        f"source: {book}",
        f"pages: {', '.join(map(str, all_pages))}" if all_pages else "pages: ",
        f"chapter: {chap.get('name', '') if chap else ''}",
        f"section: {title_node['name'] if title_node['level']==1 else ''}",
        f"kg_node_ids: [{', '.join(covered_ids)}]",
        "---",
        "",
        f"# {title}",
        "",
        f"_含 {len(covered_ids)} 个知识点 · 页码 {', '.join(map(str, all_pages))}_",
        "",
    ]
    content = "\n".join(fm_lines) + "\n".join(sections_md) + "\n## 我的笔记\n\n（待补充）\n"

    # 写文件
    safe = _safe_filename(title_node["name"])
    fname = f"{_rand_hex3()}-{safe}.md"
    out_path = vault_root / fname
    n_try = 0
    while out_path.exists() and n_try < 10:
        fname = f"{_rand_hex3()}-{safe}.md"
        out_path = vault_root / fname
        n_try += 1
    out_path.write_text(content, encoding="utf-8")
    return fname, covered_ids
# ───────────────────────────────────────────────────────────────────────


def _trigger_mastery_recompute(kg_path: Path):
    """后台跑 link_and_mastery 重新计算掌握度/解锁态。"""
    script = CLAUDE_DIR / "scripts" / "kg" / "link_and_mastery.py"
    if not script.exists():
        return
    env = os.environ.copy()
    env.setdefault("CLAUDE_PROJECT", str(CLAUDE_DIR))
    env.setdefault("OBSIDIAN_VAULT", os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
    env.setdefault("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
    log_dir = CLAUDE_DIR / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "kg_edit_recompute.log").open("ab") as logf:
        logf.write(f"\n=== {datetime.now().isoformat()} {kg_path.name} ===\n".encode())
        subprocess.Popen(
            ["/usr/bin/python3", str(script), "--kg", str(kg_path), "--in-place"],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
        )


def _apply_edit(kg, op, payload):
    """对 kg dict 做编辑操作；返回 (ok, info_str)。"""
    nodes = kg.setdefault("nodes", [])
    edges = kg.setdefault("edges", [])
    id2 = {n["id"]: n for n in nodes}
    if op == "delete_node":
        nid = payload.get("id")
        if nid not in id2:
            return False, "node not found"
        kg["nodes"] = [n for n in nodes if n["id"] != nid]
        kg["edges"] = [e for e in edges
                       if e.get("from") != nid and e.get("to") != nid]
        return True, f"deleted node {nid}"
    if op == "delete_edge":
        a = payload.get("from"); b = payload.get("to")
        before = len(edges)
        kg["edges"] = [e for e in edges
                       if not (e.get("from") == a and e.get("to") == b)]
        return True, f"removed {before - len(kg['edges'])} edge(s)"
    if op == "add_edge":
        a = payload.get("from"); b = payload.get("to")
        if a not in id2 or b not in id2 or a == b:
            return False, "invalid endpoints"
        # 防重复
        for e in edges:
            if e.get("from") == a and e.get("to") == b and e.get("kind") == "prereq":
                return False, "edge already exists"
        # 防环（简单 DFS：从 b 能否到 a）
        adj = {}
        for e in edges:
            adj.setdefault(e.get("from"), []).append(e.get("to"))
        stack = [b]; seen = {b}
        while stack:
            cur = stack.pop()
            if cur == a:
                return False, "would create cycle"
            for nx in adj.get(cur, []):
                if nx not in seen:
                    seen.add(nx); stack.append(nx)
        edges.append({
            "from": a, "to": b, "kind": "prereq", "level": id2[a].get("level", 2),
            "evidence": payload.get("evidence", "手动添加"),
        })
        return True, f"added {a} → {b}"
    if op == "merge":
        canonical = payload.get("canonical")
        drop_ids = payload.get("drop") or []
        if canonical not in id2:
            return False, "canonical not found"
        canon = id2[canonical]
        actual_drops = []
        for d in drop_ids:
            if d not in id2 or d == canonical:
                continue
            n = id2[d]
            # 合并 pages
            pgs = set(canon.get("pages", [])); pgs.update(n.get("pages", []))
            canon["pages"] = sorted(pgs)
            canon.setdefault("_merged_from", []).append(
                {"id": d, "name": n["name"], "label": n.get("numeric_label", "")})
            actual_drops.append(d)
        # 重定向边
        for e in edges:
            if e.get("from") in actual_drops: e["from"] = canonical
            if e.get("to") in actual_drops: e["to"] = canonical
        # 删自环 + 去重
        seen = set(); new_edges = []
        for e in edges:
            if e.get("from") == e.get("to"): continue
            k = (e.get("from"), e.get("to"), e.get("kind"))
            if k in seen: continue
            seen.add(k); new_edges.append(e)
        kg["edges"] = new_edges
        kg["nodes"] = [n for n in nodes if n["id"] not in actual_drops]
        return True, f"merged {len(actual_drops)} into {canonical}"
    if op == "update_summary":
        nid = payload.get("id")
        if nid not in id2:
            return False, "node not found"
        id2[nid]["summary"] = (payload.get("summary") or "").strip()
        return True, "summary updated"
    return False, f"unknown op: {op}"


def register_skilltree(app):

    @app.route("/skilltree/")
    def skilltree_index():
        return render_template("skilltree_index.html", books=list_books())

    @app.route("/skilltree/<book>/")
    def skilltree_view(book):
        p = _kg_path(book)
        if not p:
            abort(404)
        return render_template("skilltree.html", book=book)

    @app.route("/skilltree/<book>/data.json")
    def skilltree_data(book):
        p, kg = _load_kg(book)
        if not kg:
            abort(404)
        # 派生 PDF 的 vault 相对路径（给前端构 obsidian:// 链接）
        pdf_abs = kg.get("pdf")
        if pdf_abs:
            vault_root = os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian")
            try:
                rel = Path(pdf_abs).resolve().relative_to(Path(vault_root).resolve())
                kg["pdf_vault_path"] = str(rel).replace("\\", "/")
            except (ValueError, OSError):
                pass  # PDF 不在 vault 内则忽略
        return jsonify(kg)

    @app.route("/skilltree/<book>/pdf")
    def skilltree_pdf(book):
        """流式返回 KG 关联的 PDF；浏览器原生 PDF viewer 支持 #page=N 锚点。"""
        p, kg = _load_kg(book)
        if not kg:
            abort(404)
        pdf_path = kg.get("pdf")
        if not pdf_path:
            abort(404)
        pdf = Path(pdf_path)
        if not pdf.exists() or not pdf.is_file():
            abort(404)
        # 防 path traversal：只允许 KG 显式声明的那个 PDF
        try:
            return send_file(str(pdf), mimetype="application/pdf",
                             as_attachment=False,
                             download_name=pdf.name,
                             max_age=86400)
        except Exception:
            abort(500)

    @app.route("/skilltree/<book>/api/build-note", methods=["POST"])
    def skilltree_build_note(book):
        """根据 KG 节点的 pages 从 PDF 提取相关内容，生成笔记。
        - L2 节点：单节点笔记，含该节点 numeric_label 起的文本块
        - L1 节点：整节笔记，含该节所有 L2 节点各一段
        生成的笔记用 ![[X.pdf#page=N&rect=...]] obsidian 嵌入语法。
        写入 vault 根 + 更新 KG containing_notes + 触发 link_and_mastery 重算。
        """
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        node_id = body.get("node_id") or ""
        node = next((n for n in kg["nodes"] if n["id"] == node_id), None)
        if not node:
            return jsonify({"ok": False, "error": "node not found"}), 404
        pdf_path = kg.get("pdf")
        if not pdf_path or not Path(pdf_path).exists():
            return jsonify({"ok": False, "error": "pdf 缺失"}), 400

        try:
            new_path, covered_ids = _build_note_for_node(kg, node, Path(pdf_path))
        except Exception as ex:
            return jsonify({"ok": False, "error": f"生成失败: {ex}"}), 500

        # 更新 KG 持久化字典 + 节点
        with _edit_lock:
            _, kg2 = _load_kg(book)
            if not kg2:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            persistent = kg2.setdefault("_note_to_covered_l2", {})
            persistent[new_path] = covered_ids
            # 重建节点 containing_notes
            id2 = {n["id"]: n for n in kg2["nodes"]}
            for cid in covered_ids:
                if cid in id2:
                    n2 = id2[cid]
                    notes = list(set((n2.get("containing_notes") or []) + [new_path]))
                    n2["containing_notes"] = sorted(notes)
                    n2["note_ref"] = sorted(notes)[0]
                    n2["note_ref_ai_verified"] = True
            _save_kg(p, kg2)
            _trigger_mastery_recompute(p)

        return jsonify({
            "ok": True, "note_path": new_path,
            "covered_node_ids": covered_ids,
            "obsidian_url": f"obsidian://open?vault={os.environ.get('OBSIDIAN_VAULT_NAME','Obsidian Vault')}&file={new_path.replace('.md','')}",
        })

    @app.route("/skilltree/<book>/api/edit", methods=["POST"])
    def skilltree_edit(book):
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        op = body.get("op") or ""
        with _edit_lock:
            # 重新加载一次（防止并发覆盖）
            _, kg = _load_kg(book)
            if not kg:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            ok, info = _apply_edit(kg, op, body)
            if ok:
                # 备份（最近一份）
                bak = p.with_suffix(".bak.json")
                try: bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception: pass
                _save_kg(p, kg)
                _trigger_mastery_recompute(p)
        return jsonify({"ok": ok, "info": info,
                        "nodes": len(kg.get("nodes", [])),
                        "edges": len(kg.get("edges", []))})
