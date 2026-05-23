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
# 四态阈值：解锁阈值（开始学的门槛）和掌握阈值（金色徽章）分开
# 阈值取 0.4 贴合实际：当前 mastery 公式下，刷过几遍的复习卡 mastery 在 0.40-0.50；
# 0.4 让"刚开始复习的章节"就能传递解锁性，0.8 仍保留作严格的掌握徽章。
UNLOCK_THRESHOLD = 0.4       # 前置 mastery 全 >= 0.4 → 节点 unlockable
MASTERED_THRESHOLD = 0.8     # 前置 mastery 全 >= 0.8 且自身 >= 0.8 → mastered
PREVIEW_THRESHOLD = 0.3      # ≥1 个前置 mastery >= 0.3 → previewable（可瞟）


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

    # 先清空旧关联，确保重跑时不残留上一版的匹配
    for n in nodes:
        n.pop("note_ref", None); n.pop("card_refs", None)
        n.pop("mastery", None); n.pop("unlocked", None); n.pop("mastered", None)
        n.pop("has_cards", None); n.pop("state", None)
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
    # 没卡的节点 mastery 留 None（"无数据"，与 0.0 区分；状态判定时不阻塞）
    for n in nodes:
        if n["level"] == 2:
            rec = rec_by_sn.get(n.get("note_ref", ""))
            m = card_mastery({}, rec) if rec else None
            n["mastery"] = m  # 可能是 None
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
                if k.get("has_cards") and k.get("mastery") is not None:
                    ms.append(k["mastery"])
            else:
                v = agg(k)
                if v is not None: ms.append(v)
        return sum(ms) / len(ms) if ms else None
    for n in nodes:
        if n["level"] in (0, 1):
            n["mastery"] = agg(n)  # 可能 None

    # 3) 四态：locked / previewable / unlockable / mastered
    #    - mastered      = 所有前置 mastered & 自身 mastery ≥ MASTERED_THRESHOLD
    #    - unlockable    = 所有前置 mastery ≥ UNLOCK_THRESHOLD（"可以开始学"）
    #    - previewable   = ≥1 个前置 mastery ≥ PREVIEW_THRESHOLD（可瞟简介）
    #    - locked        = 其它（暂不该碰）
    # 起点（indeg=0 的 L2）默认 unlockable。
    edges = kg.get("edges", [])
    prereqs_to = defaultdict(list)
    indeg = defaultdict(int); g = defaultdict(list)
    for e in edges:
        if e.get("kind") == "prereq":
            prereqs_to[e["to"]].append(e["from"])
            g[e["from"]].append(e["to"]); indeg[e["to"]] += 1

    # 拿前置 mastery 列表来判定，所以需要先有所有 L2 的 mastery。已在上方完成。
    # 规则：mastery=None（无卡）的前置 = "中性"（不阻塞解锁，也不传递 mastered）。
    def classify_l2(nid: str) -> str:
        n = id2[nid]
        prs = prereqs_to[nid]
        self_m = n.get("mastery")  # 可能 None
        # 自身 mastered 必须 mastery 真的 ≥ 0.8（None 不算）
        self_is_mastered = (self_m is not None and self_m >= MASTERED_THRESHOLD)
        if not prs:
            return "mastered" if self_is_mastered else "unlockable"
        # 把前置 mastery 分成"有数据"和"无数据"两组
        data_ms = [id2[p].get("mastery") for p in prs if p in id2]
        with_data = [m for m in data_ms if m is not None]
        # 无数据前置全部跳过（中性）；有数据前置走阈值
        if not with_data:
            # 所有前置都无数据 → 不阻塞，按自身定
            return "mastered" if self_is_mastered else "unlockable"
        all_mast   = all(m >= MASTERED_THRESHOLD for m in with_data)
        all_unlock = all(m >= UNLOCK_THRESHOLD for m in with_data)
        any_prev   = any(m >= PREVIEW_THRESHOLD for m in with_data)
        if all_mast and self_is_mastered:
            return "mastered"
        if all_unlock:
            return "unlockable"
        if any_prev:
            return "previewable"
        return "locked"

    # 拓扑序处理（防止环里出错；这里用 BFS 顺序保证前置已分类）
    queue = [n["id"] for n in nodes if n["level"] == 2 and indeg[n["id"]] == 0]
    indeg_copy = dict(indeg)
    state_map: dict[str, str] = {}
    while queue:
        cur = queue.pop(0)
        state_map[cur] = classify_l2(cur)
        for v in g[cur]:
            indeg_copy[v] -= 1
            if indeg_copy[v] == 0:
                queue.append(v)
    # 落到节点字段；并保留旧的 unlocked / mastered 兼容字段（unlocked = unlockable | mastered）
    for n in nodes:
        if n["level"] == 2:
            st = state_map.get(n["id"], "unlockable")
            n["state"] = st
            n["unlocked"] = st in ("unlockable", "mastered")
            n["mastered"] = st == "mastered"

    # L1/L0 聚合：state 取"子孙最弱的那个"（保守显示）
    STATE_ORDER = {"locked": 0, "previewable": 1, "unlockable": 2, "mastered": 3}
    def agg_state(node):
        kids = children.get(node["id"], [])
        if not kids:
            return node.get("state", "unlockable")
        worst = "mastered"
        for k in kids:
            s = k.get("state") if k["level"] == 2 else agg_state(k)
            if STATE_ORDER.get(s, 0) < STATE_ORDER.get(worst, 0):
                worst = s
        return worst
    for n in nodes:
        if n["level"] in (0, 1):
            s = agg_state(n)
            n["state"] = s
            n["unlocked"] = s in ("unlockable", "mastered")
            n["mastered"] = s == "mastered"

    if not args.in_place:
        print("（dry-run，未写回；加 --in-place 应用）")
        return 0
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
