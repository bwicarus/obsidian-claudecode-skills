"""
skilltree.py — /skilltree/<book>/ 知识图谱可视化路由。

模式跟 control.py 一致：register_skilltree(app) 注册路由。
KG 数据 = CLAUDE_PROJECT/knowledge_graph/<book>.json（由 scripts/kg/* 生成）。
"""
import json
import os
from pathlib import Path

from flask import abort, jsonify, render_template

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
KG_DIR = CLAUDE_DIR / "knowledge_graph"


def list_books() -> list[dict]:
    out = []
    if not KG_DIR.exists():
        return out
    for f in sorted(KG_DIR.glob("*.json")):
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


def register_skilltree(app):

    @app.route("/skilltree/")
    def skilltree_index():
        return render_template("skilltree_index.html", books=list_books())

    @app.route("/skilltree/<book>/")
    def skilltree_view(book):
        kg = KG_DIR / f"{book}.json"
        if not kg.exists():
            abort(404)
        return render_template("skilltree.html", book=book)

    @app.route("/skilltree/<book>/data.json")
    def skilltree_data(book):
        kg = KG_DIR / f"{book}.json"
        if not kg.exists():
            abort(404)
        try:
            return jsonify(json.loads(kg.read_text(encoding="utf-8")))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
