"""
kg/link_and_mastery.py — 把 KG 节点关联到 Obsidian 笔记/Anki 卡，并计算掌握度+解锁态。

关联策略（零 AI）：
- L1（节）→ 按 section_label / name 模糊匹配 anki/records/*.json 的 source_note
- L2（原子）→ 优先匹配 records 里 anki 卡的 name（含数学概念名/编号）；
  没匹配上的 L2，回退继承父 L1 的笔记关联（节点本身没卡片就算"无独立掌握度"）

掌握度：
- 叶子 L2 的 mastery = 其 card_refs 对应卡的平均"mastery"
  （从 records 的 status_snapshot.mastery_avg / priority_snapshot.weakness 反算）
- L1/L0 mastery = 子节点 mastery 加权平均（按子节点数加权）
- 解锁态：节点 unlocked = 它所有 prereq 边的 from 节点都 mastered（mastery ≥ 0.8）
- 节点 mastered = mastery ≥ 0.8 且 unlocked

用法：
  python3 scripts/kg/link_and_mastery.py --kg knowledge_graph/LADR.json [--in-place]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

RECORDS_DIR = config.RECORDS_DIR
MASTERED_THRESHOLD = 0.8


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def load_records() -> tuple[list[dict], dict[str, dict]]:
    """records: 所有 record 文件内容；by_source_stem: stem → record。"""
    recs = []
    by_stem: dict[str, dict] = {}
    for f in sorted(RECORDS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        recs.append(d)
        sn = d.get("source_note", "")
        if sn.lower().endswith(".md"):
            sn = sn[:-3]
        by_stem[_norm(sn.rsplit("/", 1)[-1])] = d
    return recs, by_stem


def link_l1(node: dict, by_stem: dict[str, dict]) -> dict | None:
    """L1 → record 笔记（按 name 模糊匹配 stem）。"""
    keys = [_norm(node["name"]), _norm(node.get("section_label", "") + node["name"])]
    for k in keys:
        if k in by_stem:
            return by_stem[k]
    # 包含匹配：record stem 含节点 name
    for stem, rec in by_stem.items():
        if _norm(node["name"]) and _norm(node["name"]) in stem:
            return rec
    return None


def link_l2(node: dict, parent_rec: dict | None, recs: list[dict]) -> list[str]:
    """L2 → 卡片 local_ids（按 name / numeric_label 在父 L1 关联的 record 内匹配）。"""
    if not parent_rec:
        return []
    name_n = _norm(node["name"])
    lbl = (node.get("numeric_label") or "").strip()
    matched: list[str] = []
    for c in parent_rec.get("cards", []):
        front = _norm(c.get("front", "")[:80])
        back  = _norm(c.get("back", "")[:80])
        if name_n and (name_n in front or name_n in back):
            matched.append(c["local_id"]); continue
        if lbl and (lbl in c.get("front", "") or lbl in c.get("back", "")):
            matched.append(c["local_id"])
    return matched


def card_mastery(card: dict, rec: dict) -> float | None:
    """从 anki record 的 status_snapshot/priority_snapshot 推断卡片掌握度。
    粗略：用 record 整体 mastery_avg（每张卡精度有限，用 record 均值近似）。"""
    snap = rec.get("status_snapshot") or {}
    m = snap.get("mastery_avg")
    if isinstance(m, (int, float)):
        return float(m)
    # 退一步：1 - weakness
    pr = rec.get("priority_snapshot") or {}
    w = pr.get("weakness")
    if isinstance(w, (int, float)):
        return float(1.0 - w)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--in-place", action="store_true")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    nodes = kg["nodes"]
    id2 = {n["id"]: n for n in nodes}

    recs, by_stem = load_records()
    rec_by_sn = {r.get("source_note", ""): r for r in recs}
    print(f"加载 records: {len(recs)} 篇")

    # 1) 关联：L1 → 笔记，L2 → 卡（继承父 L1 的 record）
    linked_l1 = linked_l2 = 0
    for n in nodes:
        if n["level"] == 1:
            r = link_l1(n, by_stem)
            if r:
                n["note_ref"] = r.get("source_note", "")
                n["_record"] = id(r)   # 临时
                linked_l1 += 1
    for n in nodes:
        if n["level"] == 2:
            parent = id2.get(n["parent_id"])
            parent_rec = None
            if parent and parent.get("note_ref"):
                parent_rec = rec_by_sn.get(parent["note_ref"])
            n["note_ref"] = parent.get("note_ref", "") if parent else ""
            n["card_refs"] = link_l2(n, parent_rec, recs) if parent_rec else []
            if n["card_refs"]:
                linked_l2 += 1
    # 清理临时字段
    for n in nodes:
        n.pop("_record", None)
    print(f"关联 L1→笔记: {linked_l1}/{sum(1 for n in nodes if n['level']==1)}，"
          f"L2→卡片: {linked_l2}/{sum(1 for n in nodes if n['level']==2)}")

    # 2) 掌握度：L2 叶子先算，再向上聚合
    # 用 L2 父 record 的 mastery_avg 给所有挂在该 record 上的 L2 同一个值（粗近似）
    for n in nodes:
        if n["level"] == 2:
            rec = rec_by_sn.get(n.get("note_ref", ""))
            m = card_mastery({}, rec) if rec else None
            n["mastery"] = m if m is not None else 0.0
            n["has_cards"] = bool(n.get("card_refs"))
    # 聚合 L1/L0：子节点 mastery 均值（只算有 has_cards 的 L2）
    children = defaultdict(list)
    for n in nodes:
        if n.get("parent_id"):
            children[n["parent_id"]].append(n)
    def agg(node):
        kids = children.get(node["id"], [])
        if not kids:
            return None
        ms = []
        for k in kids:
            if k["level"] == 2:
                if k.get("has_cards"): ms.append(k["mastery"])
            else:
                v = agg(k)
                if v is not None: ms.append(v)
        return sum(ms) / len(ms) if ms else None
    for n in nodes:
        if n["level"] in (0, 1):
            v = agg(n)
            n["mastery"] = v if v is not None else 0.0

    # 3) 解锁态：所有 prereq.from 已 mastered 才 unlocked；mastered ⇔ mastery ≥ 阈值 且 unlocked
    edges = kg.get("edges", [])
    prereqs_to = defaultdict(list)
    for e in edges:
        if e.get("kind") == "prereq":
            prereqs_to[e["to"]].append(e["from"])
    # 拓扑顺序计算
    indeg = defaultdict(int); g = defaultdict(list)
    for e in edges:
        if e.get("kind") == "prereq":
            g[e["from"]].append(e["to"]); indeg[e["to"]] += 1
    # 起点：indeg 0 的 L2
    queue = [n["id"] for n in nodes if n["level"] == 2 and indeg[n["id"]] == 0]
    state: dict[str, dict] = {}
    for nid in queue:
        n = id2[nid]
        state[nid] = {"unlocked": True,
                      "mastered": n["mastery"] >= MASTERED_THRESHOLD}
    while queue:
        cur = queue.pop(0)
        for v in g[cur]:
            indeg[v] -= 1
            if indeg[v] == 0:
                prs = prereqs_to[v]
                all_mast = all(state.get(p, {}).get("mastered") for p in prs)
                n = id2[v]
                state[v] = {"unlocked": all_mast,
                            "mastered": all_mast and n["mastery"] >= MASTERED_THRESHOLD}
                queue.append(v)
    for n in nodes:
        if n["level"] == 2:
            st = state.get(n["id"], {"unlocked": True, "mastered": n["mastery"] >= MASTERED_THRESHOLD})
            n["unlocked"] = st["unlocked"]; n["mastered"] = st["mastered"]
    # L1/L0 聚合解锁/掌握：所有 L2 子孙 unlocked → 该节点 unlocked；mastered 同理
    def agg_state(node):
        kids = children.get(node["id"], [])
        if not kids:
            return True, node.get("mastered", False)
        u_all, m_all = True, True
        for k in kids:
            if k["level"] == 2:
                u_all &= k.get("unlocked", True)
                m_all &= k.get("mastered", False)
            else:
                u, m = agg_state(k)
                u_all &= u; m_all &= m
        return u_all, m_all
    for n in nodes:
        if n["level"] in (0, 1):
            u, m = agg_state(n)
            n["unlocked"] = u; n["mastered"] = m

    if not args.in_place:
        print("（dry-run，未写回；加 --in-place 应用）")
        return 0
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
