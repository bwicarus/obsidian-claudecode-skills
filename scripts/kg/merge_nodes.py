"""
kg/merge_nodes.py — 跨节合并 L2 中重复的概念。

策略（按用户要求：每次 AI 调用只分析一个候选组，多组并行）：
1. 本地预聚类（无 AI）：按归一化 name 完全相同、numeric_label 完全相同、
   或 difflib 相似度 > 0.85 把 L2 节点聚成候选组。
2. 对每个 size >= 2 的候选组，单独一次 AI 调用：判定是否真的同概念，
   并指定 canonical_id。多组并行（默认 6 worker）。
3. 应用合并：删除 merge_ids，pages 合并到 canonical。

用法：
  python3 scripts/kg/merge_nodes.py --kg knowledge_graph/LADR.json
    [--model opus|sonnet]      默认 opus（每个组判断少而精）
    [--effort low|medium|high|max]
    [--workers N]              默认 6
    [--threshold 0.85]         本地聚类相似度阈值
    [--dry-run]                只输出聚类+AI建议，不写
    [--in-place]               直接改 kg
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402


def _norm(s: str) -> str:
    return re.sub(r"[\s\.，。·\-]", "", (s or "")).lower()


def cluster_candidates(l2: list[dict], threshold: float) -> list[list[dict]]:
    """本地预聚类：返回 size>=2 的候选组列表。
    并查集：按 norm(name)/numeric_label 完全相同 → 合并；再用 difflib 相似度补充。"""
    parent = list(range(len(l2)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[rb] = ra

    # 精确等价：归一化 name 或 numeric_label 相同
    by_name = {}
    by_label = {}
    for i, n in enumerate(l2):
        nm = _norm(n["name"])
        lb = (n.get("numeric_label") or "").strip()
        if nm and nm in by_name: union(by_name[nm], i)
        if nm: by_name.setdefault(nm, i)
        if lb and lb in by_label: union(by_label[lb], i)
        if lb: by_label.setdefault(lb, i)

    # 近似（仅同章/同 L1 父附近做，O(n²) 全量太慢，但 314 节点尚可）
    norms = [_norm(n["name"]) for n in l2]
    for i in range(len(l2)):
        for j in range(i + 1, len(l2)):
            if find(i) == find(j): continue
            if not norms[i] or not norms[j]: continue
            # 快剪枝：长度差距大跳过
            if abs(len(norms[i]) - len(norms[j])) > min(len(norms[i]), len(norms[j])): continue
            r = difflib.SequenceMatcher(None, norms[i], norms[j]).ratio()
            if r >= threshold:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i, n in enumerate(l2):
        groups.setdefault(find(i), []).append(n)
    return [g for g in groups.values() if len(g) >= 2]


_PROMPT = """下面是 {n} 个候选「原子知识点」（来自教材《{book}》），它们的名字或编号相近，
可能是同一概念的不同记录、也可能是真正不同的相邻概念。请**只针对这一组**做判断。

判定标准（严格）：
- 名字几乎一致、或指代的数学对象完全相同（同一定义/同一定理本体）→ 是同一概念
- 名字看着像但实指不同（"加法"和"标量乘法"、"基"和"维数"）→ 不是同一概念
- 真的同概念时，canonical_id 选**编号最早**（数值最小）或**父节点 L1 更早**那个；
  其余作 merge_ids

输出严格 JSON，不要任何其它文字：
{{"same_concept": true|false,
  "canonical_id": "<id>",
  "merge_ids": ["<id>",...],
  "reason": "<不超过 30 字>"}}

候选组（id | 编号 | 父节点 | name | summary）：
{listing}
"""


def judge_group(backend, book: str, group: list[dict]) -> dict | None:
    lines = [f"{n['id']}|{n.get('numeric_label','')}|{n['parent_id']}|{n['name']}|{(n.get('summary') or '')[:50]}"
             for n in group]
    prompt = _PROMPT.format(book=book, n=len(group), listing="\n".join(lines))
    try:
        raw = backend.chat([{"role": "user", "content": prompt}]).strip()
    except Exception as ex:
        return {"_error": str(ex)}
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s: return {"_error": "no JSON"}
    try:
        return json.loads(raw[s:e+1])
    except Exception as ex:
        return {"_error": f"bad JSON: {ex}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--in-place", action="store_true")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    l2 = [n for n in kg["nodes"] if n["level"] == 2]
    if not l2:
        print("无 L2 节点，跳过"); return 0
    print(f"L2 候选: {len(l2)} 个")

    groups = cluster_candidates(l2, args.threshold)
    print(f"本地预聚类: {len(groups)} 个候选组（size>=2），覆盖 "
          f"{sum(len(g) for g in groups)} 个节点")

    if not groups:
        print("无可合并候选"); return 0

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude", "model": args.model, "effort": args.effort,
        "timeout": 240,
    })
    book = kg.get("book", "?")

    judgments: list[tuple[list[dict], dict]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(judge_group, backend, book, g): g for g in groups}
        for fut in as_completed(futs):
            g = futs[fut]; res = fut.result()
            done += 1
            if not res or res.get("_error"):
                print(f"  [{done}/{len(groups)}] {[x['name'] for x in g]}: AI 失败 {res.get('_error') if res else '?'}", flush=True)
                continue
            ok = bool(res.get("same_concept"))
            verdict = "✓ 同" if ok else "✗ 异"
            print(f"  [{done}/{len(groups)}] {verdict} {[x['name'] for x in g]} // {res.get('reason','')[:40]}", flush=True)
            if ok:
                judgments.append((g, res))

    # 应用
    if not judgments:
        print("AI 判定无可合并"); return 0
    if args.dry_run or not args.in_place:
        print(f"\nDRY-RUN：将合并 {len(judgments)} 组（加 --in-place 应用）")
        return 0

    by_id = {n["id"]: n for n in kg["nodes"]}
    drop_ids: set[str] = set()
    seen: set[str] = set()
    actual = 0
    for g, res in judgments:
        cid = res.get("canonical_id"); mids = res.get("merge_ids") or []
        if cid not in by_id or any(m not in by_id for m in mids): continue
        if cid in seen or any(m in seen for m in mids): continue
        canon = by_id[cid]
        for m in mids:
            n = by_id[m]
            pgs = set(canon.get("pages", [])); pgs.update(n.get("pages", []))
            canon["pages"] = sorted(pgs)
            canon.setdefault("_merged_from", []).append(
                {"id": m, "name": n["name"], "label": n.get("numeric_label","")})
            drop_ids.add(m)
        seen.add(cid); seen.update(mids); actual += 1

    kg["nodes"] = [n for n in kg["nodes"] if n["id"] not in drop_ids]
    kg["edges"] = [e for e in kg.get("edges", [])
                   if e.get("from") not in drop_ids and e.get("to") not in drop_ids]
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 合并 {actual} 组（删除 {len(drop_ids)} 重复节点）→ {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
