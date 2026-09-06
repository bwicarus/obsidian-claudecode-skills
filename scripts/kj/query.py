"""渐进式查询：搜索候选 → 浏览一层 → 读一个节点。不把全部节点数据交给 AI。"""
from __future__ import annotations

from .store import Ledger, loads
from . import wikidata as WD
from .markdown import obsidian_url

_DEF_PREVIEW = 160


def _fts_query(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def node_brief(ledger: Ledger, node_id: str) -> dict | None:
    n = ledger.node(node_id)
    if n is None:
        return None
    m = ledger.mastery_row(node_id) or {}
    return {"id": n["id"], "name": n["name"], "kind": n["kind"], "qid": n["qid"], "status": n["status"],
            "mastery": m.get("value"), "level": m.get("level", 0), "progress": m.get("progress", "unseen"),
            "readiness": m.get("readiness", "no_prereq_info"), "state": m.get("state", "unlockable"),
            "obsidian_url": obsidian_url(n["id"], n["name"])}


def search(ledger: Ledger, q: str, *, limit: int = 8, include_public: bool = True, public_limit: int | None = None) -> dict:
    """本地节点优先（名称/别名/定义 FTS，短词退 LIKE），再附公共目录候选（标 local_node 有无）。"""
    q = (q or "").strip()
    if not q:
        return {"query": q, "local": [], "public": []}
    seen: list[str] = []
    if len(q) >= 3:
        try:
            for r in ledger.db.execute("SELECT node_id FROM node_fts WHERE node_fts MATCH ? ORDER BY rank LIMIT ?", (_fts_query(q), limit)):
                if r[0] not in seen:
                    seen.append(r[0])
        except Exception:
            pass
    if len(seen) < limit:
        like = f"%{q}%"
        for r in ledger.db.execute(
                "SELECT DISTINCT n.id FROM nodes n LEFT JOIN node_aliases a ON a.node_id=n.id"
                " WHERE n.status='active' AND (n.name LIKE ? OR a.alias LIKE ?) ORDER BY n.name LIMIT ?", (like, like, limit)):
            if r[0] not in seen:
                seen.append(r[0])
    exact = [nid for nid in seen if (ledger.node(nid) or {"name": ""})["name"].lower() == q.lower()]
    ordered = exact + [n for n in seen if n not in exact]
    local = []
    for nid in ordered[:limit]:
        b = node_brief(ledger, nid)
        if not b:
            continue
        defs = ledger.definitions(nid)
        b["definition_preview"] = defs[0]["text"][:_DEF_PREVIEW] if defs else ""
        b["path"] = classification_path(ledger, nid, depth=2)
        local.append(b)
    # 公共候选默认最多 5 个（排序修好后 4~5 个够定位；每个约 60 token），要更多显式给 public_limit
    public = WD.search_public(ledger, q, limit=public_limit or min(limit, 5)) if include_public else []
    local_qids = {b["qid"] for b in local if b["qid"]}
    public = [p for p in public if p["qid"] not in local_qids]
    return {"query": q, "local": local, "public": public}


def classification_path(ledger: Ledger, node_id: str, depth: int = 3) -> list[dict]:
    """本地分类路径：沿 subclass_of / instance_of / part_of 的本地关系向上；没有本地上级时借公共路径。"""
    out: list[dict] = []
    cur = node_id
    seen = {node_id}
    for _ in range(depth):
        row = ledger.db.execute(
            "SELECT to_id, type FROM relations WHERE from_id=? AND status='active' AND type IN ('subclass_of','instance_of','part_of')"
            " ORDER BY CASE type WHEN 'subclass_of' THEN 0 WHEN 'instance_of' THEN 1 ELSE 2 END LIMIT 1", (cur,)).fetchone()
        if row is None or row["to_id"] in seen:
            break
        seen.add(row["to_id"])
        parent = ledger.node(row["to_id"])
        out.append({"node": row["to_id"], "name": parent["name"] if parent else row["to_id"], "via": row["type"]})
        cur = row["to_id"]
    if not out:
        n = ledger.node(node_id)
        if n is not None and n["qid"]:
            out = [{"qid": p["qid"], "name": p["label"], "via": p["via"], "node": p["local_node"]} for p in WD.path_up(ledger, n["qid"], depth)]
    return out


def browse(ledger: Ledger, parent: str | None = None, *, limit: int = 40) -> dict:
    """一层一层看。parent=None → 根：没有本地上级的节点按 kind 分组；parent=节点 id → 它的下级（谁 subclass_of/instance_of/part_of 它）。"""
    if parent:
        p = ledger.resolve(parent)
        if p is None:
            return {"parent": parent, "error": "node_not_found", "children": []}
        rows = ledger.db.execute(
            "SELECT from_id, type FROM relations WHERE to_id=? AND status='active' AND type IN ('subclass_of','instance_of','part_of') ORDER BY type LIMIT ?",
            (p["id"], limit)).fetchall()
        children = []
        for r in rows:
            b = node_brief(ledger, r["from_id"])
            if b:
                b["via"] = r["type"]
                children.append(b)
        return {"parent": node_brief(ledger, p["id"]), "children": children}
    roots = ledger.db.execute(
        "SELECT n.id FROM nodes n WHERE n.status='active' AND NOT EXISTS ("
        " SELECT 1 FROM relations r WHERE r.from_id=n.id AND r.status='active' AND r.type IN ('subclass_of','instance_of','part_of'))"
        " ORDER BY n.kind, n.name LIMIT ?", (limit,)).fetchall()
    groups: dict[str, list[dict]] = {}
    for r in roots:
        b = node_brief(ledger, r[0])
        if b:
            groups.setdefault(b["kind"], []).append(b)
    total = ledger.db.execute("SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0]
    return {"parent": None, "total_nodes": int(total), "groups": groups}


def node_detail(ledger: Ledger, node_id: str, *, records_limit: int = 8) -> dict | None:
    """一次拿全 AI 交流所需：位置、关系、定义、记录摘要、卡、掌握数据、准备状态。"""
    row = ledger.resolve(node_id)
    if row is None:
        return None
    nid = row["id"]
    m = ledger.mastery_row(nid) or {}
    detail = m.get("detail") or {}
    rels = ledger.relations(nid)
    prereqs, successors, others = [], [], []
    for r in rels:
        other = r["to_id"] if r["from_id"] == nid else r["from_id"]
        entry = {"relation_id": r["id"], "type": r["type"], "origin": r["origin"], "evidence": r["evidence"],
                 "node": node_brief(ledger, other) or {"id": other}}
        if r["type"] == "prereq" and r["to_id"] == nid:
            prereqs.append(entry)
        elif r["type"] == "prereq" and r["from_id"] == nid:
            successors.append(entry)
        else:
            entry["direction"] = "out" if r["from_id"] == nid else "in"
            others.append(entry)
    recs = ledger.records(nid, limit=records_limit)
    rec_total = ledger.db.execute("SELECT COUNT(*) FROM records WHERE node_id=? AND merged_into IS NULL", (nid,)).fetchone()[0]
    cards = ledger.cards_of(nid)
    public = None
    if row["qid"]:
        e = WD.entity(ledger, row["qid"])
        if e:
            public = {"qid": e["qid"], "label": e["label"], "label_en": e["label_en"], "description": e["description"],
                      "description_en": e["desc_en"], "aliases": WD.alias_sample(e, 6),
                      "neighbors": public_neighbors(ledger, row["qid"])}
        else:
            public = {"qid": row["qid"], "label": None, "description": None, "neighbors": [], "note": "公共目录里还没有这个编号（可 fetch）"}
    weak = detail.get("prereqs", {}).get("weak", [])
    unknown = detail.get("prereqs", {}).get("unknown", [])
    return {
        "id": nid, "name": row["name"], "kind": row["kind"], "qid": row["qid"], "summary": row["summary"],
        "aliases": ledger.aliases(nid), "status": row["status"],
        "obsidian_url": obsidian_url(nid, row["name"]),
        "path": classification_path(ledger, nid),
        "definitions": [{"id": d["id"], "text": d["text"], "context_key": d["context_key"], "source": d["source"]} for d in ledger.definitions(nid)],
        "prereqs": prereqs, "successors": successors, "relations": others,
        "records": {"total": int(rec_total), "latest": [{"id": r["id"], "kind": r["kind"], "text": r["text"], "occurred_at": r["occurred_at"],
                                                          "registered_at": r["registered_at"], "source": r["source"], "occurrences": r["occurrences"]} for r in recs]},
        "cards": [{"card_key": c["card_key"], "anki_note_id": c["anki_note_id"], "front": c["front"][:120]} for c in cards],
        "mastery": {"value": m.get("value"), "level": m.get("level", 0), "progress": m.get("progress", "unseen"),
                    "evidence_count": m.get("evidence_count", 0), "signals": detail.get("signals", []), "updated_at": m.get("updated_at")},
        "readiness": {"availability": m.get("availability", "open"), "readiness": m.get("readiness", "no_prereq_info"), "state": m.get("state", "unlockable"),
                      "weak_prereqs": [node_brief(ledger, w) for w in weak], "unknown_prereqs": [node_brief(ledger, u) for u in unknown]},
        "public": public,
        "next_hint": next_hint(m, prereqs, weak, unknown),
    }


def next_hint(m: dict, prereqs: list, weak: list, unknown: list) -> str:
    """给 AI 的一句话建议动作（代码 + 短说明，不是话术）。"""
    if not m or m.get("value") is None:
        if prereqs and unknown:
            return "unknown_basics: 前置有关系但无个人记录，掌握未知；先了解基础或直接讲解，勿断言未掌握"
        return "no_evidence: 还没有掌握证据；讨论后可出 2-3 题速测或让用户自评"
    if weak:
        return "needs_basics: 有前置显示薄弱，结合上下文提出先补这些基础"
    if m.get("progress") == "mastered":
        return "mastered: 已掌握；可作前置解锁后续，隔期再抽查"
    if unknown:
        return "unknown_basics: 部分前置掌握未知；若用户受阻可提议检查这些前置"
    return "in_progress: 进行中；继续学习/练习，答错会立刻反映到掌握度"


def public_neighbors(ledger: Ledger, qid: str, limit: int = 12) -> list[dict]:
    out = []
    for prop, target, _ in WD.claims_of(ledger, qid):
        if prop not in WD.PROP_MAP:
            continue
        local = ledger.find_by_qid(target)
        out.append({"qid": target, "label": WD.label_of(ledger, target), "via": WD.PROP_MAP[prop][0], "direction": "out",
                    "local_node": local["id"] if local else None})
        if len(out) >= limit:
            return out
    for src, prop, _ in WD.claims_to(ledger, qid):
        if prop not in WD.PROP_MAP:
            continue
        local = ledger.find_by_qid(src)
        out.append({"qid": src, "label": WD.label_of(ledger, src), "via": WD.PROP_MAP[prop][0], "direction": "in",
                    "local_node": local["id"] if local else None})
        if len(out) >= limit:
            break
    return out


def neighbors(ledger: Ledger, node_id: str, depth: int = 1) -> dict:
    row = ledger.resolve(node_id)
    if row is None:
        return {"error": "node_not_found"}
    frontier = {row["id"]}
    seen = {row["id"]}
    edges = []
    for _ in range(max(1, depth)):
        nxt = set()
        for nid in frontier:
            for r in ledger.relations(nid):
                edges.append({"id": r["id"], "from": r["from_id"], "to": r["to_id"], "type": r["type"], "origin": r["origin"]})
                other = r["to_id"] if r["from_id"] == nid else r["from_id"]
                if other not in seen:
                    seen.add(other)
                    nxt.add(other)
        frontier = nxt
    uniq = {e["id"]: e for e in edges}
    return {"center": row["id"], "nodes": [b for b in (node_brief(ledger, n) for n in seen) if b], "edges": list(uniq.values())}


def stats(ledger: Ledger) -> dict:
    db = ledger.db
    def c(sql: str) -> int:
        return int(db.execute(sql).fetchone()[0])
    return {
        "events": c("SELECT COUNT(*) FROM events"), "nodes": c("SELECT COUNT(*) FROM nodes WHERE status='active'"),
        "definitions": c("SELECT COUNT(*) FROM definitions WHERE superseded_by IS NULL"),
        "records": c("SELECT COUNT(*) FROM records WHERE merged_into IS NULL"),
        "relations": c("SELECT COUNT(*) FROM relations WHERE status='active'"),
        "prereq_relations": c("SELECT COUNT(*) FROM relations WHERE status='active' AND type='prereq'"),
        "cards": c("SELECT COUNT(*) FROM cards WHERE status='active'"),
        "quizzes": c("SELECT COUNT(*) FROM quizzes"), "public_entities": c("SELECT COUNT(*) FROM public_entities"),
        "mastered": c("SELECT COUNT(*) FROM mastery WHERE progress='mastered'"),
        "in_progress": c("SELECT COUNT(*) FROM mastery WHERE progress='in_progress'"),
    }
