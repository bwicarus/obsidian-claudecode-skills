"""kg/auto_archive.py — 机械判定 trivial 节点，自动收入回收站。

无 AI，纯规则：
  1. type == "method"（教材的"记号/方法"类）
  2. summary 长度 ≤ 15 字
  3. name 含数学符号 / "记号" 关键字 且 ≤ 6 字
  4. name 极短（≤ 2 字，如 "V"/"0"）

跑这个脚本会调用服务端 archive-node 端点（或直接复用辅助函数）。

用法：
  python3 scripts/kg/auto_archive.py --kg knowledge_graph/LADR.json [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa


def is_trivial_contextual(node: dict, kg: dict) -> tuple[bool, str]:
    """基于「学习进度地形」判定：通用规则不依赖书籍词汇。

    候选条件（全部满足才标记）：
      1. 节点本身没笔记
      2. 同章 L2 兄弟里：附近 ±2 页有 mastered 比例 ≥ 50%
      3. 同章 L2 兄弟里：后续页（>本节 page）有 ≥ 1 个 mastered
      4. 节点自身 summary 短（≤ 35 字，作弱信号防核心概念被误判）

    解释：你已经学完了附近和后面的内容，但**没碰**这个节点——说明这是顺带的
    术语/记号 / 已被你的笔记隐含覆盖。
    """
    notes = node.get("containing_notes") or []
    if notes:
        return False, "already_has_notes"
    label = (node.get("numeric_label") or "").strip()
    pages = node.get("pages") or []
    if not label or not pages:
        return False, ""
    main_page = pages[0]

    id2 = {n["id"]: n for n in kg["nodes"]}
    def chap_of_id(nid):
        cur = id2.get(nid)
        while cur and cur.get("parent_id"):
            par = id2.get(cur["parent_id"])
            if par and par.get("level") == 0:
                return par["id"]
            cur = par
        return None

    my_chap = chap_of_id(node["id"])
    if not my_chap:
        return False, ""
    siblings = [n for n in kg["nodes"]
                if n["level"] == 2 and chap_of_id(n["id"]) == my_chap and n["id"] != node["id"]]
    if not siblings:
        return False, ""

    # 附近 ±2 页（按 page）
    nearby = [s for s in siblings
              if (s.get("pages") or [-99])[0] in range(main_page - 2, main_page + 3)]
    nearby_mastered = sum(1 for s in nearby if s.get("state") == "mastered")
    cond_nearby_ok = len(nearby) >= 2 and nearby_mastered / max(len(nearby), 1) >= 0.5

    # 后续（page > main_page）
    later = [s for s in siblings if (s.get("pages") or [-99])[0] > main_page]
    later_mastered = sum(1 for s in later if s.get("state") == "mastered")
    cond_later_ok = later_mastered >= 1

    # 自身内容短（弱信号）
    summary = (node.get("summary") or "").strip()
    name = (node.get("name") or "").strip()
    cond_self_short = len(summary) <= 30
    # 进一步：name 短（≤ 6 字）或含数学符号/"记号"——避免误判核心长名概念
    has_math_sym = any(c in name for c in ["−", "→", "·", "⊕", "⊗", "⟨", "⟩", "‖"])
    cond_name_trivial = len(name) <= 6 or "记号" in name or has_math_sym

    # 排除节内首节点（通常是核心定义）
    sec_id = node.get("parent_id", "")
    sec_siblings = [n for n in kg["nodes"]
                    if n["level"] == 2 and n.get("parent_id") == sec_id]
    sec_siblings.sort(key=lambda x: x.get("numeric_label", ""))
    is_first_in_section = sec_siblings and sec_siblings[0]["id"] == node["id"]
    if is_first_in_section:
        return False, "first_node_in_section_likely_core"

    if cond_nearby_ok and cond_later_ok and cond_self_short and cond_name_trivial:
        return True, (f"附近 mastered {nearby_mastered}/{len(nearby)} + 后续 mastered "
                       f"{later_mastered} + summary {len(summary)} 字 + name 短/含符号")
    return False, ""


# 旧字面规则保留作 backward 兼容（auto_archive --legacy 调用）
def is_trivial_mechanical_legacy(node: dict) -> tuple[bool, str]:
    """LADR 字面规则——只对中文教材的"记号/术语"敏感，不通用。"""
    name = (node.get("name") or "").strip()
    summary = (node.get("summary") or "").strip()
    type_ = (node.get("type") or "").strip()
    if type_ == "method":
        return True, "legacy_type_method"
    if "记号" in name:
        return True, "legacy_name_jihao"
    has_math_sym = any(c in name for c in ["−", "→", "·", "⊕", "⊗", "⟨", "⟩", "‖"])
    if has_math_sym and len(name) <= 6:
        return True, "legacy_math_short"
    return False, ""


# 默认判定 = contextual
def is_trivial_mechanical(node: dict, kg: dict = None) -> tuple[bool, str]:
    if kg is not None:
        return is_trivial_contextual(node, kg)
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="（不推荐）直接归档；默认只标 _archive_suggestions 让用户手动决定")
    ap.add_argument("--limit", type=int, default=0,
                    help="单次最多归档 N 个（防意外大批操作）")
    ap.add_argument("--legacy", action="store_true",
                    help="用旧字面规则（type=method/记号/数学符号），仅 LADR 等中文教材")
    ap.add_argument("--suggest", action="store_true", default=True,
                    help="（默认）把候选写到 KG._archive_suggestions，前端按钮高亮提示用户")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))

    candidates = []
    for n in kg["nodes"]:
        if n["level"] != 2: continue
        if any("回收站" in nt for nt in (n.get("containing_notes") or [])):
            continue
        if (n.get("containing_notes") or []):
            continue
        if args.legacy:
            ok, rule = is_trivial_mechanical_legacy(n)
        else:
            ok, rule = is_trivial_contextual(n, kg)
        if ok:
            candidates.append((n, rule))

    print(f"候选 trivial 节点：{len(candidates)} 个")
    for n, rule in candidates[:30]:
        print(f"  [{n.get('numeric_label','?'):<7}] {n['name']:<20} ({rule})  summary={n.get('summary','')[:30]}")
    if len(candidates) > 30:
        print(f"  ... 共 {len(candidates)} 个")

    # 默认行为：写 KG._archive_suggestions（让前端按钮高亮提示），不实际归档
    if not args.apply:
        if candidates:
            kg["_archive_suggestions"] = sorted(set([n["id"] for n, _ in candidates]))
        else:
            kg["_archive_suggestions"] = []
        kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ 已写 {len(kg.get('_archive_suggestions', []))} 个建议到 KG._archive_suggestions")
        print("   前端节点的 🗑 按钮会高亮提示，用户手动按下才执行")
        print("   （如真要批量执行，加 --apply 但建议手动）")
        return 0

    # 实际归档：调服务端端点
    import urllib.request
    book = kg.get("book", kg_path.stem)
    base = f"http://127.0.0.1:5000/skilltree/{book}/api/archive-node"
    # 但本地 webapp 监听端口未知；改用直接 import 服务端函数
    sys.path.insert(0, str(config.PROJECT_DIR / "_server_deploy"))
    from skilltree import _archive_node_to_trash
    pdf_path = Path(kg.get("pdf",""))
    if not pdf_path.exists():
        print(f"PDF 缺失：{pdf_path}"); return 1
    persistent = kg.setdefault("_note_to_covered_l2", {})
    n_done = 0
    limit = args.limit or len(candidates)
    for n, rule in candidates[:limit]:
        try:
            trash_rel = _archive_node_to_trash(kg, n, pdf_path)
            persistent.setdefault(trash_rel, [])
            if n["id"] not in persistent[trash_rel]:
                persistent[trash_rel].append(n["id"])
            cn = sorted(set((n.get("containing_notes") or []) + [trash_rel]))
            n["containing_notes"] = cn
            n["note_ref"] = cn[0]
            n["note_ref_ai_verified"] = True
            n_done += 1
            print(f"  ✓ {n.get('numeric_label','?'):<7} {n['name']:<20} → {trash_rel}")
        except Exception as ex:
            print(f"  ✗ {n.get('numeric_label','?'):<7} {n['name']}: {ex}")
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 归档 {n_done} 个节点，KG 已写回 {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
