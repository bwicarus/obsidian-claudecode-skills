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


def is_trivial_mechanical(node: dict) -> tuple[bool, str]:
    """返回 (是否 trivial, 触发规则名)。保守规则——只抓真正的「记号/术语」。

    单核心概念（如「基」「张成」「内积」「单射」）一律不归——名字短不代表过简单。
    """
    name  = (node.get("name") or "").strip()
    summary = (node.get("summary") or "").strip()
    type_ = (node.get("type") or "").strip()

    # 规则 1：type == "method"（build_nodes 标的"记号/方法"类）
    if type_ == "method":
        return True, "rule_1_type_method"
    # 规则 2：name 含"记号"字样（明确标识为记号定义）
    if "记号" in name:
        return True, "rule_2_name_has_jihao"
    # 规则 3：name 含数学符号且长度 ≤ 6（如 "−v、w−v"、"𝑎0"、"0V"）
    has_math_sym = any(c in name for c in ["−", "→", "·", "⊕", "⊗", "⟨", "⟩", "‖"])
    if has_math_sym and len(name) <= 6:
        return True, "rule_3_math_symbol_short"
    # 规则 4：summary 含"记号"或"约定"等元词（教材级元描述）
    if any(kw in summary for kw in ["记号约定", "本书中表示", "本节中表示", "本书中我们用"]):
        return True, "rule_4_summary_meta"
    # 规则 5：name 是单字母 + 数字（如"V"、"0"、"F^n"中的"F"）
    import re as _re
    if _re.match(r"^[A-Za-z𝑎-𝑧][\^\d]*$", name) and len(name) <= 4:
        return True, "rule_5_single_letter"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="实际收入回收站（默认 dry-run，只列候选）")
    ap.add_argument("--limit", type=int, default=0,
                    help="单次最多归档 N 个（防意外大批操作）")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))

    candidates = []
    for n in kg["nodes"]:
        if n["level"] != 2: continue
        # 已经在回收站的跳过
        if any("回收站" in nt for nt in (n.get("containing_notes") or [])):
            continue
        # 已有非回收站笔记的跳过（用户已经收为独立笔记）
        if (n.get("containing_notes") or []):
            continue
        ok, rule = is_trivial_mechanical(n)
        if ok:
            candidates.append((n, rule))

    print(f"候选 trivial 节点：{len(candidates)} 个")
    for n, rule in candidates[:30]:
        print(f"  [{n.get('numeric_label','?'):<7}] {n['name']:<20} ({rule})  summary={n.get('summary','')[:30]}")
    if len(candidates) > 30:
        print(f"  ... 共 {len(candidates)} 个")

    if not args.apply:
        print("\n（dry-run，加 --apply 实际归档）")
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
