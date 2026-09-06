"""KJ 命令行：Skill 执行者（Claude / Codex）用的固定程序入口。所有输出都是一行 JSON。

    python scripts/kj/cli.py search "向量空间" [--online]
    python scripts/kj/cli.py node kj:XXXXXXXXXX
    python scripts/kj/cli.py register --json '{"type":"record", ...}'     # 或 --json - 从 stdin 读
    python scripts/kj/cli.py relation FROM TO prereq --evidence "…"
    python scripts/kj/cli.py quiz --json '{"items":[...]}'  /  quiz-result QUIZ --json '[{"item_id":"q1","result":"correct"}]'
    python scripts/kj/cli.py self-assess NODE 0.9 --reason "…"
    python scripts/kj/cli.py anki-sync | rebuild | rebuild-md | stats
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kj.service import KJService  # type: ignore
    from kj import anki_sync as AK  # type: ignore
else:
    from .service import KJService
    from . import anki_sync as AK


def _json_arg(v: str | None):
    if v is None:
        return None
    if v == "-":
        v = sys.stdin.read()
    v = v.strip()
    if not v:
        return None
    return json.loads(v)


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kj", description="KJ 知识节点系统")
    ap.add_argument("--db", help="账本路径（默认 $CLAUDE_PROJECT/state/kj/kj.db）")
    ap.add_argument("--vault", help="Markdown 输出目录（默认 $OBSIDIAN_VAULT/KJ）")
    ap.add_argument("--no-render", action="store_true", help="不写 Markdown")
    ap.add_argument("--actor", default="cli", help="登记者标识（写进事件）")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("search"); s.add_argument("q"); s.add_argument("--limit", type=int, default=8)
    s.add_argument("--online", action="store_true"); s.add_argument("--no-public", action="store_true")
    s = sp.add_parser("node"); s.add_argument("node_id"); s.add_argument("--records", type=int, default=8)
    s = sp.add_parser("browse"); s.add_argument("parent", nargs="?")
    s = sp.add_parser("neighbors"); s.add_argument("node_id"); s.add_argument("--depth", type=int, default=1)
    sp.add_parser("stats")
    s = sp.add_parser("register", help="通用登记：--json 传 {type, ...}"); s.add_argument("--json", required=True)
    s = sp.add_parser("node-create"); s.add_argument("--name", required=True); s.add_argument("--kind", default="concept")
    s.add_argument("--alias", action="append", default=[]); s.add_argument("--qid"); s.add_argument("--summary", default="")
    s.add_argument("--source"); s.add_argument("--fetch", action="store_true", help="绑了 qid 时在线补公共目录")
    s = sp.add_parser("definition"); s.add_argument("node_id"); s.add_argument("--text", required=True); s.add_argument("--source", required=True)
    s.add_argument("--context", default=""); s.add_argument("--decision", default=""); s.add_argument("--supersedes", default="")
    s.add_argument("--uses", default="", help="看懂这条定义必须先会的节点：编号或名称，逗号分隔；程序以定义原句为依据登 prereq")
    s = sp.add_parser("page-status"); s.add_argument("book", help="书的 sha(16 hex) 或绝对路径"); s.add_argument("page", type=int)
    s = sp.add_parser("page-brief"); s.add_argument("book"); s.add_argument("page", type=int)
    s = sp.add_parser("page-block"); s.add_argument("book"); s.add_argument("page", type=int)
    s = sp.add_parser("page-submit"); s.add_argument("--json", required=True, help="一页分析的 JSON（结构见 kj/pages.py），或 @文件")
    s = sp.add_parser("book-pages"); s.add_argument("book"); s.add_argument("--total", type=int, help="给了就列未分析页（整本手动批处理用）")
    s = sp.add_parser("record"); s.add_argument("node_id"); s.add_argument("--text", required=True); s.add_argument("--kind", default="note")
    s.add_argument("--source"); s.add_argument("--at", help="发生时间 ISO/epoch；不知道就别填")
    s = sp.add_parser("merge-records"); s.add_argument("node_id"); s.add_argument("--ids", required=True, help="逗号分隔")
    s.add_argument("--text", required=True); s.add_argument("--occurrences", type=int); s.add_argument("--earliest")
    s = sp.add_parser("relation"); s.add_argument("from_id"); s.add_argument("to_id"); s.add_argument("rtype")
    s.add_argument("--evidence", default=""); s.add_argument("--source")
    s = sp.add_parser("relation-retract"); s.add_argument("relation_id"); s.add_argument("--reason", default="")
    s = sp.add_parser("relation-change"); s.add_argument("relation_id"); s.add_argument("--type"); s.add_argument("--reverse", action="store_true")
    s.add_argument("--evidence", default=""); s.add_argument("--reason", default="")
    s = sp.add_parser("bind-qid"); s.add_argument("node_id"); s.add_argument("qid"); s.add_argument("--no-fetch", action="store_true")
    s = sp.add_parser("unbind-qid"); s.add_argument("node_id")
    s = sp.add_parser("merge-node"); s.add_argument("node_id"); s.add_argument("into"); s.add_argument("--reason", default="")
    s = sp.add_parser("card-bind"); s.add_argument("--nodes", required=True); s.add_argument("--anki-note-id", type=int)
    s.add_argument("--front", default=""); s.add_argument("--back", default=""); s.add_argument("--deck", default="")
    s = sp.add_parser("card-make"); s.add_argument("--nodes", required=True); s.add_argument("--front", required=True); s.add_argument("--back", required=True)
    s.add_argument("--deck", default=AK.DEFAULT_DECK); s.add_argument("--tag", action="append", default=[])
    sp.add_parser("anki-sync", help="吸收桥的卡↔节点绑定账本 + 拉 Anki 复习快照进掌握度")
    s = sp.add_parser("inbox", help="只吸收桥的卡↔节点绑定账本（确认入库后的卡自动绑节点、补深链）"); s.add_argument("--path")
    s = sp.add_parser("quiz"); s.add_argument("--json", required=True, help='{"items":[{"item_id","question","answer","node_ids":[...]}], "target_node", "title"}')
    s = sp.add_parser("quiz-result"); s.add_argument("quiz_id"); s.add_argument("--json", required=True, help='[{"item_id","result"}]')
    s = sp.add_parser("self-assess"); s.add_argument("node_id"); s.add_argument("value", type=float); s.add_argument("--reason", default="")
    sp.add_parser("rebuild"); sp.add_parser("rebuild-md")
    s = sp.add_parser("wikidata-import"); s.add_argument("path"); s.add_argument("--only-qids", help="文件，每行一个 Q 编号")
    s.add_argument("--require-lang"); s.add_argument("--limit", type=int)
    s = sp.add_parser("wikidata-fetch"); s.add_argument("qid"); s.add_argument("--refresh", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    svc = KJService(a.db, a.vault, render=not a.no_render, actor=a.actor)
    try:
        c = a.cmd
        if c == "search":
            r = svc.search(a.q, limit=a.limit, online=a.online, include_public=not a.no_public)
        elif c == "node":
            r = svc.node(a.node_id, records_limit=a.records)
        elif c == "browse":
            r = svc.browse(a.parent)
        elif c == "neighbors":
            r = svc.neighbors(a.node_id, depth=a.depth)
        elif c == "stats":
            r = svc.stats()
        elif c == "register":
            r = svc.register(_json_arg(a.json) or {})
        elif c == "node-create":
            r = svc.create_node(name=a.name, kind=a.kind, aliases=a.alias, qid=a.qid, summary=a.summary, source=_json_arg(a.source), fetch_public=a.fetch)
        elif c == "definition":
            r = svc.add_definition(a.node_id, text=a.text, source=_json_arg(a.source), context_key=a.context, decision=a.decision, supersedes=a.supersedes,
                                   uses=[x.strip() for x in (a.uses or "").split(",") if x.strip()])
        elif c == "page-status":
            r = svc.page_status(a.book, a.page)
        elif c == "page-brief":
            r = svc.page_brief(a.book, a.page)
        elif c == "page-block":
            r = svc.page_block(a.book, a.page)
        elif c == "page-submit":
            r = svc.page_submit(_json_arg(a.json))
        elif c == "book-pages":
            r = svc.book_pages(a.book, a.total)
        elif c == "record":
            r = svc.add_record(a.node_id, text=a.text, kind=a.kind, source=_json_arg(a.source), occurred_at=a.at)
        elif c == "merge-records":
            r = svc.merge_records(a.node_id, record_ids=[x.strip() for x in a.ids.split(",") if x.strip()], text=a.text,
                                  occurrences=a.occurrences, earliest=a.earliest)
        elif c == "relation":
            r = svc.add_relation(from_id=a.from_id, to_id=a.to_id, type=a.rtype, evidence=a.evidence, source=_json_arg(a.source))
        elif c == "relation-retract":
            r = svc.retract_relation(a.relation_id, reason=a.reason)
        elif c == "relation-change":
            r = svc.change_relation(a.relation_id, type=a.type, reverse=a.reverse, evidence=a.evidence, reason=a.reason)
        elif c == "bind-qid":
            r = svc.bind_qid(a.node_id, a.qid, fetch_public=not a.no_fetch)
        elif c == "unbind-qid":
            r = svc.unbind_qid(a.node_id)
        elif c == "merge-node":
            r = svc.merge_node(a.node_id, a.into, reason=a.reason)
        elif c == "card-bind":
            r = svc.bind_card(node_ids=[x.strip() for x in a.nodes.split(",") if x.strip()], anki_note_id=a.anki_note_id, front=a.front, back=a.back, deck=a.deck)
        elif c == "card-make":
            r = svc.make_card(node_ids=[x.strip() for x in a.nodes.split(",") if x.strip()], front=a.front, back=a.back, deck=a.deck, tags=a.tag)
        elif c == "anki-sync":
            r = svc.anki_sync()
        elif c == "inbox":
            r = svc.ingest_bindings(a.path)
        elif c == "quiz":
            p = _json_arg(a.json) or {}
            r = svc.register_quiz(items=p.get("items") or [], target_node=p.get("target_node"), title=p.get("title", ""), source=p.get("source"))
        elif c == "quiz-result":
            p = _json_arg(a.json) or []
            r = svc.submit_results(quiz_id=a.quiz_id, results=p if isinstance(p, list) else (p.get("results") or []))
        elif c == "self-assess":
            r = svc.self_assess(a.node_id, value=a.value, reason=a.reason)
        elif c == "rebuild":
            r = svc.rebuild()
        elif c == "rebuild-md":
            r = svc.rebuild_markdown()
        elif c == "wikidata-import":
            only = None
            if a.only_qids:
                only = {ln.strip() for ln in Path(a.only_qids).read_text("utf-8").splitlines() if ln.strip()}
            r = svc.wikidata_import(a.path, only_qids=only, require_lang=a.require_lang, limit=a.limit)
        elif c == "wikidata-fetch":
            r = svc.wikidata_fetch(a.qid, refresh=a.refresh)
        else:
            r = {"ok": False, "error": f"unknown cmd {c}"}
    finally:
        svc.close()
    _out(r)
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
