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
# 四态分桶（按自身 mastery，不做 DAG 传递；前置链信息在详情面板里看）：
#   mastered     mastery >= 0.8       金色，真正掌握
#   unlockable   mastery >= 0.4       黄色，正在认真学
#   previewable  0 < mastery < 0.4    浅灰，刚起步
#   locked       mastery == None / 0  深灰，还没碰过
# 这种纯桶分类避免了"刷得多的章节因低 mastery 反而被链式 lock"的反直觉。
MASTERED_THRESHOLD = 0.8
UNLOCK_THRESHOLD = 0.4
PREVIEW_FLOOR = 0.0          # > 这个值进 previewable，否则 locked


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _stem_core(stem: str) -> str:
    """剥前缀：'000-向量空间' → '向量空间'；'资源/books/000-LADR/000-直和' → '直和'。"""
    stem = stem.rsplit("/", 1)[-1]
    return re.sub(r"^[0-9]+[\-_]", "", stem)


def load_records() -> tuple[list[dict], dict[str, dict]]:
    """records: 所有 record 文件内容；by_stem: 多键索引（norm 全stem + norm core）→ record。"""
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
        # 索引两个键：完整 stem 和 去前缀的 core
        full = sn.rsplit("/", 1)[-1]
        by_stem[_norm(full)] = d
        by_stem[_norm(_stem_core(full))] = d
    return recs, by_stem


def link_l1(node: dict, by_stem: dict[str, dict]) -> dict | None:
    """L1 → record 笔记。先精确，再双向 contains。"""
    name_n = _norm(node["name"])
    if not name_n:
        return None
    # 精确
    if name_n in by_stem:
        return by_stem[name_n]
    # 双向 contains：要求 stem core 与 name 至少 3 字重叠，防"基"→"复数的基本性质"误匹配
    best = None; best_overlap = 0
    for k, rec in by_stem.items():
        if not k or len(k) < 3 or len(name_n) < 3: continue
        ov = 0
        if k in name_n:  ov = len(k)
        elif name_n in k: ov = len(name_n)
        if ov >= 3 and ov > best_overlap:
            best, best_overlap = rec, ov
    return best


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

    # 每次重算：清掉 mastery 字段；containing_notes 完全交给 link_with_ai.py
    # 未被 AI verified 的节点 = 没数据，不做模糊匹配 fallback（避免"商空间的维数"
    # 因为父 L1 name 含"向量空间"被字符串匹配到"000-向量空间.md"的连锁误判）
    for n in nodes:
        n.pop("card_refs", None)
        n.pop("mastery", None); n.pop("unlocked", None); n.pop("mastered", None)
        n.pop("has_cards", None); n.pop("state", None)
        n.pop("mastery_self", None); n.pop("mastery_inferred", None)
        if not n.get("note_ref_ai_verified"):
            n["note_ref"] = ""
            n["containing_notes"] = []
    # 关联：只用 AI verified 的 containing_notes，没就是没（locked）
    linked_l1 = sum(1 for n in nodes if n["level"]==1 and n.get("note_ref_ai_verified"))
    linked_l2 = 0
    for n in nodes:
        if n["level"] != 2: continue
        notes = n.get("containing_notes") or []
        if not notes:
            n["card_refs"] = []
            continue
        cards_union = []
        for nt in notes:
            rec = rec_by_sn.get(nt)
            if rec:
                cards_union.extend(link_l2(n, rec, recs))
        n["card_refs"] = list(set(cards_union))
        if n["card_refs"]:
            linked_l2 += 1
    print(f"关联 L1→笔记: {linked_l1}/{sum(1 for n in nodes if n['level']==1)}，"
          f"L2→卡片: {linked_l2}/{sum(1 for n in nodes if n['level']==2)}（全部 AI verified）")

    # 2) 掌握度：L2 节点用 containing_notes 里所有 record mastery 取 max
    # （任一笔记里掌握 = 该节点已掌握；保守用 max 比 mean 更合直觉）
    # 没卡的节点 mastery 留 None（"无数据"，与 0.0 区分；状态判定时不阻塞）
    for n in nodes:
        if n["level"] != 2: continue
        notes = n.get("containing_notes")
        if not notes and n.get("note_ref"):
            notes = [n["note_ref"]]  # 兼容旧字段
        ms = []
        if notes:
            for nt in notes:
                rec = rec_by_sn.get(nt)
                if rec:
                    mv = card_mastery({}, rec)
                    if mv is not None: ms.append(mv)
        n["mastery"] = max(ms) if ms else None
        n["has_cards"] = bool(n.get("card_refs"))
    # 聚合 L1/L0：子节点 mastery 均值（只算有 has_cards 的 L2；其它跳过）
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
                # 只要 L2 mastery 不是 None 就纳入（继承自父 L1 record 也算）
                if k.get("mastery") is not None:
                    ms.append(k["mastery"])
            else:
                v = agg(k)
                if v is not None: ms.append(v)
        return sum(ms) / len(ms) if ms else None
    for n in nodes:
        if n["level"] in (0, 1):
            n["mastery"] = agg(n)  # 可能 None

    # 2.5) 反向传递：L2 节点的有效 mastery = max(自身, 所有 descendant 的 mastery)
    # 逻辑：能掌握高级知识点说明已掌握其所有前置 → 把前置 mastery 拉到至少跟最强后继齐
    # 保留 mastery_self 字段记录原始值，加 mastery_inferred 标记被推断掌握的节点
    edges_kg = kg.get("edges", [])
    fwd = defaultdict(list)  # from -> [to]
    for e in edges_kg:
        if e.get("kind") == "prereq":
            fwd[e["from"]].append(e["to"])
    # 拓扑序（自顶向下找所有 descendant 的最大 mastery）—— 反向 BFS 累计
    def all_descendants(nid):
        seen = set(); stack = [nid]
        while stack:
            cur = stack.pop()
            for v in fwd.get(cur, []):
                if v not in seen:
                    seen.add(v); stack.append(v)
        return seen
    for n in nodes:
        if n["level"] != 2: continue
        n["mastery_self"] = n.get("mastery")
        descs = all_descendants(n["id"])
        desc_ms = [id2[d].get("mastery") for d in descs
                   if d in id2 and id2[d].get("mastery") is not None]
        if not desc_ms:
            n["mastery_inferred"] = False
            continue
        max_desc = max(desc_ms)
        self_m = n.get("mastery")
        if max_desc is not None and (self_m is None or max_desc > self_m):
            n["mastery"] = max_desc
            n["mastery_inferred"] = True
        else:
            n["mastery_inferred"] = False
    # L0/L1 mastery 已经是 L2 均值，但 L2 反向传递后 L2 mastery 变了，
    # 重新跑 agg 让 L0/L1 反映新的 L2 mastery
    for n in nodes:
        if n["level"] in (0, 1):
            n["mastery"] = agg(n)

    # 3) 状态计算（DAG 传递 + 自身 mastery 综合）
    # 规则：
    #   自己有 mastery → 按 mastery 数值分桶
    #   自己无 mastery → 看 prereq 链：
    #     - 无 prereq（起点）→ unlockable
    #     - 所有 prereq state ∈ {unlockable, mastered} → unlockable（链通了，可开始学）
    #     - 至少一个 prereq state ∈ {unlockable, mastered} → previewable（部分开放，可瞟）
    #     - 全部 prereq locked → locked（链还没通）
    def bucket_by_mastery(m):
        if m is None or m <= 0:           return None    # 无数据，靠 DAG 传递
        if m >= MASTERED_THRESHOLD:        return "mastered"
        if m >= UNLOCK_THRESHOLD:          return "unlockable"
        return "previewable"

    # 构造 prereq 邻接（只对 L2）
    edges_kg = kg.get("edges", [])
    prereqs_of = defaultdict(list)
    indeg = defaultdict(int); succ = defaultdict(list)
    for e in edges_kg:
        if e.get("kind") == "prereq":
            prereqs_of[e["to"]].append(e["from"])
            succ[e["from"]].append(e["to"])
            indeg[e["to"]] += 1
    # 拓扑序处理 L2
    open_set = {"unlockable", "mastered"}
    queue_l2 = [n["id"] for n in nodes if n["level"]==2 and indeg[n["id"]]==0]
    remaining = dict(indeg)
    state_map: dict[str, str] = {}
    while queue_l2:
        cur = queue_l2.pop(0)
        n = id2[cur]
        m_state = bucket_by_mastery(n.get("mastery"))
        if m_state is not None:
            state_map[cur] = m_state
        else:
            prs = prereqs_of.get(cur, [])
            if not prs:
                state_map[cur] = "unlockable"   # 起点无前置 → 开放
            else:
                pr_states = [state_map.get(p, "locked") for p in prs]
                if all(s in open_set for s in pr_states):
                    state_map[cur] = "unlockable"
                elif any(s in open_set for s in pr_states):
                    state_map[cur] = "previewable"
                else:
                    state_map[cur] = "locked"
        for v in succ[cur]:
            remaining[v] -= 1
            if remaining[v] == 0:
                queue_l2.append(v)
    # 落到 L2 节点字段
    for n in nodes:
        if n["level"] == 2:
            st = state_map.get(n["id"], "locked")
            n["state"] = st
            n["unlocked"] = st in open_set
            n["mastered"] = st == "mastered"

    # L0/L1 聚合：取子孙 L2 的"最强"状态（mastered > unlockable > previewable > locked）
    STATE_ORDER = {"locked":0, "previewable":1, "unlockable":2, "mastered":3}
    def agg_state(node):
        kids = children.get(node["id"], [])
        if not kids: return "locked"
        best = "locked"
        for k in kids:
            s = k.get("state") if k["level"]==2 else agg_state(k)
            if STATE_ORDER.get(s,0) > STATE_ORDER.get(best,0):
                best = s
        return best
    for n in nodes:
        if n["level"] in (0, 1):
            n["state"] = agg_state(n)
            n["unlocked"] = n["state"] in open_set
            n["mastered"] = n["state"] == "mastered"

    if not args.in_place:
        print("（dry-run，未写回；加 --in-place 应用）")
        return 0
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
