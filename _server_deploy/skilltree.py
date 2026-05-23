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
