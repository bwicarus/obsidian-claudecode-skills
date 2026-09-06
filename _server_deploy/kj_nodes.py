"""kj_nodes.py — KJ 知识节点 HTTP 接口（``/kj/api/*``）。

薄适配层：全部业务在 ``scripts/kj``（账本 ``$CLAUDE_PROJECT/state/kj/kj.db``，
Markdown 输出 ``$OBSIDIAN_VAULT/KJ/``）。这里只做登录守卫、参数搬运、状态码。
运行位置 = Windows 本机 Flask（Windows 是 App 的服务器；Pi 只做备份）。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request, session

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
bp = Blueprint("kj_nodes", __name__, url_prefix="/kj/api")

_SVC = None
_SVC_LOCK = threading.Lock()
_NOT_FOUND = {"node_not_found", "quiz_not_found", "relation_not_found", "record_not_found", "card_not_found", "not_found"}


def _svc():
    """懒加载单例；测试可直接替换模块级 _SVC。"""
    global _SVC
    with _SVC_LOCK:
        if _SVC is None:
            sp = str(CLAUDE_DIR / "scripts")
            if sp not in sys.path:
                sys.path.insert(0, sp)
            from kj.service import KJService
            _SVC = KJService(actor="http")
        return _SVC


def _uid() -> str:
    return str(session.get("user_id") or "")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _reply(res: dict):
    if res.get("ok", True):
        return jsonify(res)
    return jsonify(res), (404 if res.get("code") in _NOT_FOUND else 400)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


@bp.before_request
def _guard():
    if not _uid():
        return jsonify({"ok": False, "code": "unauthorized", "error": "login required"}), 401
    svc = _svc()
    svc.actor = f"http:{_uid()}"
    return None


@bp.route("/search")
def kj_search():
    q = request.args.get("q", "")
    limit = max(1, min(int(request.args.get("limit", 8) or 8), 30))
    return _reply(_svc().search(q, limit=limit, online=_truthy(request.args.get("online")),
                                include_public=not _truthy(request.args.get("no_public"))))


@bp.route("/node/<node_id>")
def kj_node(node_id: str):
    return _reply(_svc().node(node_id, records_limit=max(0, min(int(request.args.get("records", 8) or 8), 50))))


@bp.route("/browse")
def kj_browse():
    return _reply(_svc().browse(request.args.get("parent") or None, limit=max(1, min(int(request.args.get("limit", 40) or 40), 200))))


@bp.route("/neighbors/<node_id>")
def kj_neighbors(node_id: str):
    return _reply(_svc().neighbors(node_id, depth=max(1, min(int(request.args.get("depth", 1) or 1), 3))))


@bp.route("/stats")
def kj_stats():
    return _reply(_svc().stats())


@bp.route("/register", methods=["POST"])
def kj_register():
    return _reply(_svc().register(_body()))


@bp.route("/quiz", methods=["POST"])
def kj_quiz():
    b = _body()
    return _reply(_svc().register_quiz(items=b.get("items") or [], target_node=b.get("target_node"), title=b.get("title", ""), source=b.get("source")))


@bp.route("/quiz/<quiz_id>/result", methods=["POST"])
def kj_quiz_result(quiz_id: str):
    b = _body()
    results = b if isinstance(b, list) else (b.get("results") or [])
    return _reply(_svc().submit_results(quiz_id=quiz_id, results=results, occurred_at=(b.get("occurred_at") if isinstance(b, dict) else None)))


@bp.route("/relation", methods=["POST"])
def kj_relation():
    b = _body()
    op = str(b.get("op") or "add").lower()
    svc = _svc()
    if op == "retract":
        return _reply(svc.retract_relation(b.get("relation_id", ""), reason=b.get("reason", "")))
    if op == "change":
        return _reply(svc.change_relation(b.get("relation_id", ""), type=b.get("relation_type"), reverse=bool(b.get("reverse")),
                                          evidence=b.get("evidence", ""), source=b.get("source"), reason=b.get("reason", "")))
    return _reply(svc.add_relation(from_id=b.get("from", b.get("from_id", "")), to_id=b.get("to", b.get("to_id", "")),
                                   type=b.get("relation_type", b.get("type", "")), evidence=b.get("evidence", ""), source=b.get("source")))


@bp.route("/self-assess", methods=["POST"])
def kj_self_assess():
    b = _body()
    return _reply(_svc().self_assess(b.get("node_id", ""), value=b.get("value"), reason=b.get("reason", "")))


@bp.route("/anki-sync", methods=["POST"])
def kj_anki_sync():
    return _reply(_svc().anki_sync())


@bp.route("/rebuild-md", methods=["POST"])
def kj_rebuild_md():
    return _reply(_svc().rebuild_markdown())


@bp.route("/wikidata/fetch", methods=["POST"])
def kj_wikidata_fetch():
    b = _body()
    return _reply(_svc().wikidata_fetch(str(b.get("qid") or ""), refresh=bool(b.get("refresh"))))


def register_kj_nodes(app) -> None:
    app.register_blueprint(bp)
