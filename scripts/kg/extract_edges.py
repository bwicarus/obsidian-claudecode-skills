"""
kg/extract_edges.py — 抽取 L2 节点之间的严格前置边。

策略：分章批处理，按章序从前往后。对当前 L0 章 X 的每个 L2 节点 K：
- 候选前置 = 所有「在 K 之前出现」的 L2 节点（按 (page, numeric_label) 排序）
- 给 AI 看：K 的 name+summary+编号 + 候选前置列表（精简）
- AI 选出严格意义的前置（"不懂 P 就不可能懂 K"），每节点 1-3 条

事后检查：
- 不允许指向自己/同节点同 numeric_label
- 不允许指向比自己后出现的节点（DAG 约束 by 顺序）
- 后置环检测（用拓扑排序）—— 顺序约束已基本排除环，但 AI 错误时仍可能引入

用法：
  python3 scripts/kg/extract_edges.py --kg knowledge_graph/LADR.json
    [--model sonnet|opus]   默认 sonnet（边判断量大，sonnet 足够）
    [--effort low|medium|high|max]
    [--max-prereqs 3]       每节点最多保留几条前置
    [--in-place]            写回 kg 文件
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402


_PROMPT = """下面是教材《{book}》一个原子知识点 K，以及它**之前**出现过的所有原子知识点候选。
请从候选里选出 K 的**严格前置**（"不先懂 P 就根本不可能懂 K"），最多 {maxp} 条。

**严格**：只列必备前置；"用过/相关/类似"不算。多数节点只有 0-2 条真正的前置。

K：
  id: {k_id}
  编号: {k_label}
  名称: {k_name}
  描述: {k_summary}
  所在节: {k_section}

候选前置列表（id | 编号 | 父节点 | name | summary）：
{candidates}

输出严格 JSON，无任何额外文字：
{{
  "prereqs": [
    {{"from": "<候选id>", "reason": "<不超过 25 字>"}}
  ]
}}
如确实没有严格前置，prereqs 为空数组。
"""


def page_of(n: dict) -> int:
    pgs = n.get("pages") or []
    return pgs[0] if pgs else 10**9


def label_key(s: str) -> tuple:
    """编号排序键：'1.41' → (1, 41)，'2.A.13' → (2, 0, 13) 等。"""
    parts = re.split(r"[\.A-Z]", s)
    nums = []
    for p in parts:
        if p.isdigit(): nums.append(int(p))
    return tuple(nums) if nums else (10**9,)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--max-prereqs", type=int, default=3)
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个 L2（试水）")
    ap.add_argument("--workers", type=int, default=4, help="并行 AI 调用数")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    nodes = kg["nodes"]
    id2 = {n["id"]: n for n in nodes}
    l2 = [n for n in nodes if n["level"] == 2]
    # 按教材出现顺序：(page, label)
    l2.sort(key=lambda n: (page_of(n), label_key(n.get("numeric_label", ""))))
    if args.limit:
        l2_target = l2[:args.limit]
    else:
        l2_target = l2

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude", "model": args.model, "effort": args.effort,
        "timeout": 300,
    })

    book = kg.get("book", "?")

    def _make_prompt(k):
        candidates = [n for n in l2 if (page_of(n), label_key(n.get("numeric_label",""))) <
                      (page_of(k), label_key(k.get("numeric_label","")))]
        if not candidates:
            return None, []
        cand_lines = [f"{c['id']}|{c.get('numeric_label','')}|{c['parent_id']}|"
                      f"{c['name']}|{c.get('summary','')[:40]}" for c in candidates]
        if len(cand_lines) > 80:
            same_chap = [c for c in candidates
                         if id2.get(c["parent_id"], {}).get("parent_id") ==
                            id2.get(k["parent_id"], {}).get("parent_id")]
            recent = candidates[-50:]
            keep_ids = set(c["id"] for c in same_chap) | set(c["id"] for c in recent)
            cand_lines = [line for c, line in zip(candidates, cand_lines)
                          if c["id"] in keep_ids]
        prompt = _PROMPT.format(
            book=book, maxp=args.max_prereqs,
            k_id=k["id"], k_label=k.get("numeric_label", ""), k_name=k["name"],
            k_summary=k.get("summary", ""),
            k_section=id2.get(k["parent_id"], {}).get("name", ""),
            candidates="\n".join(cand_lines),
        )
        return prompt, candidates

    def _process(k):
        prompt, candidates = _make_prompt(k)
        if not prompt:
            return k, [], None
        try:
            raw = backend.chat([{"role": "user", "content": prompt}]).strip()
        except Exception as ex:
            return k, [], ex
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
            raw = re.sub(r"\n```\s*$", "", raw)
        s, e = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[s:e + 1]) if (s != -1 and e > s) else {}
        except Exception:
            data = {}
        return k, (data.get("prereqs") or [])[:args.max_prereqs], None

    new_edges: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_process, k): k for k in l2_target}
        for fut in as_completed(futs):
            done += 1
            k, prereqs, err = fut.result()
            if err is not None:
                print(f"  [{done}/{len(l2_target)}] {k['name']}: AI 失败 {err}", flush=True)
                continue
            kept = 0
            for p in prereqs:
                fid = (p.get("from") or "").strip()
                if fid not in id2 or fid == k["id"]:
                    continue
                from_n = id2[fid]
                if from_n["level"] != 2:
                    continue
                if (page_of(from_n), label_key(from_n.get("numeric_label",""))) >= \
                   (page_of(k), label_key(k.get("numeric_label",""))):
                    continue
                new_edges.append({"from": fid, "to": k["id"], "kind": "prereq",
                                  "level": 2, "evidence": p.get("reason", "")})
                kept += 1
            if done % 20 == 0 or done == len(l2_target):
                print(f"  [{done}/{len(l2_target)}] {k.get('numeric_label','?'):8s} "
                      f"{k['name'][:18]:18s} → {kept} 前置（累计边 {len(new_edges)}）", flush=True)

    # 环检测：拓扑排序，环边记号
    indeg = defaultdict(int); g = defaultdict(list)
    for e in new_edges:
        g[e["from"]].append(e["to"]); indeg[e["to"]] += 1
    targets = set(e["to"] for e in new_edges) | set(e["from"] for e in new_edges)
    queue = [n for n in targets if indeg[n] == 0]
    seen = set()
    while queue:
        n = queue.pop(); seen.add(n)
        for v in g[n]:
            indeg[v] -= 1
            if indeg[v] == 0: queue.append(v)
    cyc_nodes = targets - seen
    if cyc_nodes:
        bad = [e for e in new_edges if e["from"] in cyc_nodes and e["to"] in cyc_nodes]
        new_edges = [e for e in new_edges if e not in bad]
        print(f"⚠ 检测到环，剔除 {len(bad)} 条边")

    print(f"前置边总数：{len(new_edges)}")
    if not args.in_place:
        print("（未写回，加 --in-place 应用）")
        return 0
    kg["edges"] = new_edges
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
