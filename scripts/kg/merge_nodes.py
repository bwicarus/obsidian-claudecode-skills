"""
kg/merge_nodes.py — 跨节合并 L2 中重复的概念。

build_nodes.py 已经按 numeric_label 在节内去重了，但跨节、跨章经常出现：
- 同概念不同编号（教材在两处给出近似定义）
- 同概念在两节都被列出（前置回顾、后续扩展）
- AI 错读编号

策略：把所有 L2 节点（name + summary + numeric_label + parent_id）丢给 AI，
让它输出"合并组"——同一组的节点是同概念，选一个 canonical id 作代表。
脚本据此重写 nodes：保留 canonical，删除其它，并把 pages 合并。

用法：
  python3 scripts/kg/merge_nodes.py --kg knowledge_graph/LADR.json
    [--model opus|sonnet]   默认 opus（合并判断需精度）
    [--effort low|medium|high|max]
    [--dry-run]             只输出建议，不改文件
    [--in-place]            直接改 kg 文件
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402

_PROMPT = """下面是教材《{book}》一些**原子知识点**的列表（每行格式：id|编号|父节点|name|summary）。
其中可能存在**同一概念被列了多次**（不同编号/不同父节点/相近 name）。

请识别出真正"是同一概念"的合并组。判定标准（严格）：
- 名称几乎一致，**或**指代的数学对象完全相同（同一定义/同一定理本体）
- 不要把"加法"与"标量乘法"合并；不要把"定义 A"与"定理 A 的某条性质"合并
- 不同 chapter 但讲同一概念的早期/后期版本，算合并

输出严格 JSON，无任何额外文字：
{{
  "groups": [
    {{"canonical_id": "ladr.l2.xxx", "merge_ids": ["ladr.l2.yyy", "ladr.l2.zzz"], "reason": "同为'XXX定义'"}}
  ]
}}
- canonical_id 选编号最早（数值最小）或父节点层级最高（L1 编号小）的那个
- merge_ids 是要合并到 canonical 的其它 id（不含 canonical 自身）
- 没找到任何合并组则 groups: []
- 一个 id 不可同时出现在多个组

知识点列表（共 {n} 条）：
{listing}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--in-place", action="store_true")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    l2 = [n for n in kg["nodes"] if n["level"] == 2]
    if not l2:
        print("无 L2 节点，跳过")
        return 0
    print(f"L2 候选: {len(l2)} 个")

    lines = []
    for n in l2:
        lines.append(f"{n['id']}|{n.get('numeric_label','')}|{n['parent_id']}|"
                     f"{n['name']}|{n.get('summary','')}")
    listing = "\n".join(lines)

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude", "model": args.model, "effort": args.effort,
        "timeout": 900,   # 全量合并对 opus 是大 prompt（~30KB），需 10+ 分钟
    })
    prompt = _PROMPT.format(book=kg.get("book", "?"), n=len(l2), listing=listing)
    print(f"调 AI 找合并组（{args.model}/{args.effort}）…")
    raw = backend.chat([{"role": "user", "content": prompt}]).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[s:e + 1]) if (s != -1 and e > s) else {}
    except Exception as ex:
        print(f"AI 返回非合法 JSON：{ex}")
        print(raw[:400])
        return 1
    groups = data.get("groups") or []
    print(f"AI 建议合并 {len(groups)} 组")

    by_id = {n["id"]: n for n in kg["nodes"]}
    plan: list[tuple[str, list[str], str]] = []
    seen_ids: set[str] = set()
    for g in groups:
        cid = g.get("canonical_id"); mids = g.get("merge_ids") or []
        if cid not in by_id or any(m not in by_id for m in mids):
            continue
        if cid in seen_ids or any(m in seen_ids for m in mids):
            continue
        plan.append((cid, mids, g.get("reason", "")))
        seen_ids.add(cid); seen_ids.update(mids)

    for cid, mids, reason in plan:
        print(f"  {by_id[cid]['name']}({cid}) ← {[by_id[m]['name'] for m in mids]}  // {reason}")

    if not plan:
        print("无可合并项")
        return 0
    if args.dry_run or not args.in_place:
        print(f"\n（dry-run，未写文件；加 --in-place 应用合并）")
        return 0

    # 应用：把 merge_ids 的 pages 合并到 canonical，删除 merge_ids 节点
    drop_ids: set[str] = set()
    for cid, mids, _ in plan:
        canon = by_id[cid]
        for m in mids:
            n = by_id[m]
            canon.setdefault("pages", []).extend([p for p in n.get("pages", []) if p not in canon.get("pages", [])])
            canon.setdefault("_merged_from", []).append({"id": m, "name": n["name"], "label": n.get("numeric_label", "")})
            drop_ids.add(m)
    kg["nodes"] = [n for n in kg["nodes"] if n["id"] not in drop_ids]
    kg["edges"] = [e for e in kg.get("edges", []) if e.get("from") not in drop_ids and e.get("to") not in drop_ids]

    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已合并 {len(plan)} 组（删除 {len(drop_ids)} 重复节点）→ {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
