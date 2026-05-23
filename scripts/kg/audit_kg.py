"""
kg/audit_kg.py — KG 准确性夜间审计：启发式 + 单调性 + AI 抽样审稿。

晚上定时跑，把可疑节点 / 边 / 关联写到 state/kg_audit.json，
仪表盘读它显示"KG 待 review"面板。

三类 issue：

C. 启发式检测（无 AI）
   - heuristic_edge_gaps
     · "章内孤儿"：节点 mastery>0 且非章首但 prereqs 空
     · "章间断层"：节点跨章前置全 mastery=None / locked
     · "高度数无前置"：descendants_count>10 但 prereqs=[]

D. 单调性检测（无 AI）
   - monotonicity_violations
     · prereq 边的 from.mastery < to.mastery（前置反而比后续弱）
     · 已经被反向传递修过的边大多不会触发，仍可能在 0.4 区间出问题

B. AI 抽样审稿（每晚 N 个 AI 调用）
   - ai_audit_flagged
     · 抽 N 个还没审过的节点（轮转 18 天扫完 353 个）
     · AI 判断 name+summary 准确性、containing_notes 是否真覆盖、
       prereqs 是否合理
   - 进度持久化在 audit JSON 顶层 audit_progress.ai_audited_node_ids

用法：
  python3 scripts/kg/audit_kg.py --kg knowledge_graph/LADR.json
    [--ai-sample-size N]   默认 20
    [--ai-skip]            跳过 AI 审稿（只跑 C/D，开发调试用）
    [--model sonnet] [--effort medium] [--workers 4]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402

VAULT_ROOT = config.VAULT_ROOT
STATE_DIR = config.STATE_DIR


# ---------------------------------------------------------------------------
# C: 启发式
# ---------------------------------------------------------------------------

def heuristic_edge_gaps(kg: dict) -> list[dict]:
    """章内孤儿 / 章间断层 / 高度数无前置。"""
    nodes = kg["nodes"]
    edges = [e for e in kg.get("edges", []) if e.get("kind") == "prereq"]
    id2 = {n["id"]: n for n in nodes}
    prereqs_to = defaultdict(list)
    descendants = defaultdict(int)
    successors = defaultdict(list)
    for e in edges:
        prereqs_to[e["to"]].append(e["from"])
        successors[e["from"]].append(e["to"])
    # 算 descendants 数（传递）
    def count_desc(nid):
        seen = set(); stack = [nid]
        while stack:
            cur = stack.pop()
            for v in successors.get(cur, []):
                if v not in seen:
                    seen.add(v); stack.append(v)
        return len(seen)

    def root_chap(nid):
        cur = id2.get(nid)
        while cur and cur.get("parent_id"):
            cur = id2.get(cur["parent_id"])
        return cur["id"] if cur else None

    issues = []
    for n in nodes:
        if n["level"] != 2: continue
        nid = n["id"]
        m = n.get("mastery")
        prs = prereqs_to.get(nid, [])
        dcnt = count_desc(nid)
        my_chap = root_chap(nid)
        # (1) 章内孤儿：mastery>0 且 prereqs 空 且不是章首页节点
        if m is not None and m > 0 and not prs:
            # 是否是该章最早出现的节点？(pages 最小)
            same_chap = [x for x in nodes if x["level"]==2 and root_chap(x["id"])==my_chap]
            same_chap.sort(key=lambda x: (x.get("pages") or [10**9])[0])
            is_first = bool(same_chap) and same_chap[0]["id"] == nid
            if not is_first:
                issues.append({
                    "kind": "orphan_in_chapter", "node_id": nid, "name": n["name"],
                    "chapter": id2[my_chap]["name"] if my_chap else "?",
                    "reason": f"mastery={m:.2f} 但无任何 prereqs（非章首节点）",
                })
        # (2) 高度数无前置
        if dcnt > 10 and not prs:
            issues.append({
                "kind": "high_degree_no_prereq", "node_id": nid, "name": n["name"],
                "descendants_count": dcnt,
                "reason": f"有 {dcnt} 个传递后继但没设任何 prereq",
            })
        # (3) 章间断层：本节点 mastery>0.3 但所有跨章前置 mastery 都是 None/0
        if m is not None and m > 0.3 and prs:
            cross_chap_prs = [p for p in prs if root_chap(p) != my_chap]
            if cross_chap_prs:
                cross_ms = [id2[p].get("mastery") for p in cross_chap_prs if p in id2]
                if cross_ms and all(x is None or x == 0 for x in cross_ms):
                    issues.append({
                        "kind": "cross_chap_break", "node_id": nid, "name": n["name"],
                        "reason": f"mastery={m:.2f} 但 {len(cross_chap_prs)} 个跨章前置全无数据",
                    })
    return issues


# ---------------------------------------------------------------------------
# D: 单调性
# ---------------------------------------------------------------------------

def monotonicity_violations(kg: dict, tol: float = 0.05) -> list[dict]:
    """prereq 边的 from.mastery < to.mastery 视为可疑（前置弱于后续）。
    tol：容差，防止浮点抖动；超过 tol 才报。"""
    nodes = kg["nodes"]
    id2 = {n["id"]: n for n in nodes}
    issues = []
    for e in kg.get("edges", []):
        if e.get("kind") != "prereq": continue
        a, b = id2.get(e["from"]), id2.get(e["to"])
        if not a or not b: continue
        ma, mb = a.get("mastery"), b.get("mastery")
        if ma is None or mb is None: continue
        if mb - ma > tol:
            issues.append({
                "kind": "mastery_inversion", "from_id": a["id"], "from_name": a["name"],
                "to_id": b["id"], "to_name": b["name"],
                "from_mastery": round(ma, 3), "to_mastery": round(mb, 3),
                "reason": f"前置 mastery={ma:.2f} < 后续 mastery={mb:.2f}（差 {mb-ma:+.2f}）",
            })
    return issues


# ---------------------------------------------------------------------------
# B: AI 抽样审稿
# ---------------------------------------------------------------------------

_AUDIT_PROMPT = """你是教材《{book}》知识图谱的审稿人。这是一个原子知识点节点的元数据，
请审查它的描述、关联笔记和前置链是否准确/合理。

【节点】
id: {nid}
编号: {label}
名称: {name}
摘要: {summary}
父节: {parent}

【关联笔记】（AI 之前判定的"严格包含本节点定义的笔记"）：
{notes}

【前置节点】（"不懂这些就不可能懂本节点"）：
{prereqs}

【后续节点】（依赖本节点的概念，仅前 5）：
{successors}

请按以下维度判断（每项一行 yes/no/uncertain + 理由 < 20 字）：
1. 名称是否准确反映节点指代的对象？
2. 关联笔记是否真的包含本节点的完整定义？
3. 前置节点列表是否合理（既不缺关键前置，也不冗余无关）？

如果总体没问题返回 ok：true；任一维度有问题返回 ok：false + 具体说明。

输出严格 JSON，无任何其它文字：
{{
  "ok": true|false,
  "name_ok": "yes|no|uncertain",
  "notes_ok": "yes|no|uncertain",
  "prereqs_ok": "yes|no|uncertain",
  "issues": ["<问题描述 1>", ...],
  "suggestion": "<不超过 40 字的修改建议（如有）>"
}}
"""


def audit_one_node(backend, kg: dict, node: dict) -> dict:
    id2 = {n["id"]: n for n in kg["nodes"]}
    prereqs_to = defaultdict(list)
    successors = defaultdict(list)
    for e in kg.get("edges", []):
        if e.get("kind") == "prereq":
            prereqs_to[e["to"]].append(e["from"])
            successors[e["from"]].append(e["to"])
    # 加载笔记内容（如果有）
    notes_text = ""
    notes_list = node.get("containing_notes") or []
    if notes_list:
        parts = []
        for nt in notes_list[:3]:   # 最多读 3 篇
            p = VAULT_ROOT / nt
            if p.exists():
                try:
                    content = p.read_text(encoding="utf-8")
                    content = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)
                    parts.append(f"=== {nt} ===\n{content[:3000]}")
                except Exception:
                    pass
        notes_text = "\n\n".join(parts) if parts else "（笔记文件不可读）"
    else:
        notes_text = "（无关联笔记）"
    prs = prereqs_to.get(node["id"], [])
    prs_text = "\n".join(
        f"  {pid} | {id2[pid].get('numeric_label','')} | {id2[pid]['name']} | {(id2[pid].get('summary') or '')[:50]}"
        for pid in prs if pid in id2
    ) or "  （无前置）"
    sucs = successors.get(node["id"], [])[:5]
    sucs_text = "\n".join(
        f"  {sid} | {id2[sid].get('numeric_label','')} | {id2[sid]['name']}"
        for sid in sucs if sid in id2
    ) or "  （无后续）"

    parent = id2.get(node.get("parent_id"))
    prompt = _AUDIT_PROMPT.format(
        book=kg.get("book","?"),
        nid=node["id"], label=node.get("numeric_label",""),
        name=node["name"], summary=node.get("summary",""),
        parent=parent["name"] if parent else "?",
        notes=notes_text,
        prereqs=prs_text,
        successors=sucs_text,
    )
    try:
        raw = backend.chat([{"role":"user","content":prompt}]).strip()
    except Exception as ex:
        return {"node_id": node["id"], "name": node["name"], "_error": str(ex)}
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return {"node_id": node["id"], "name": node["name"], "_error": "无 JSON"}
    try:
        data = json.loads(raw[s:e+1])
    except Exception as ex:
        return {"node_id": node["id"], "name": node["name"], "_error": f"JSON 解析失败: {ex}"}
    data["node_id"] = node["id"]
    data["name"] = node["name"]
    data["numeric_label"] = node.get("numeric_label","")
    return data


def select_audit_sample(kg: dict, audited_ids: set[str], n: int) -> list[dict]:
    """从未审过的 L2 节点里抽 n 个，全审完后清空进度循环重审。"""
    l2 = [n for n in kg["nodes"] if n["level"] == 2]
    not_yet = [x for x in l2 if x["id"] not in audited_ids]
    if not not_yet:
        # 全部审过 → 清空重审
        not_yet = l2
        return random.sample(not_yet, min(n, len(not_yet)))
    return random.sample(not_yet, min(n, len(not_yet)))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--ai-sample-size", type=int, default=20)
    ap.add_argument("--ai-skip", action="store_true", help="跳过 AI 审稿（开发调试）")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(STATE_DIR / "kg_audit.json"))
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    book = kg.get("book", kg_path.stem)
    print(f"KG: {kg_path.name}  book={book}  节点 {len(kg['nodes'])}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 读历史 audit 进度（保留 ai_audited_node_ids 累积进度）
    prev = {}
    if out_path.exists():
        try: prev = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception: prev = {}
    prev_audited_ids = set(((prev.get("audit_progress") or {}).get("ai_audited_node_ids") or []))

    # C
    edge_gaps = heuristic_edge_gaps(kg)
    print(f"[C] 启发式: {len(edge_gaps)} 条")
    # D
    mono = monotonicity_violations(kg)
    print(f"[D] 单调性: {len(mono)} 条")
    # B
    ai_flagged: list[dict] = []
    audited_ids_now = set(prev_audited_ids)
    if not args.ai_skip:
        sample = select_audit_sample(kg, prev_audited_ids, args.ai_sample_size)
        # 如果 prev 已经审过全部，重新开始时清空
        l2_total = sum(1 for n in kg["nodes"] if n["level"]==2)
        if len(prev_audited_ids) >= l2_total:
            print(f"[B] 已完成全图轮转（{l2_total} 节点），清空 ai_audited_node_ids 重新开始")
            audited_ids_now = set()
        print(f"[B] AI 抽样 {len(sample)} 节点（累计已审 {len(audited_ids_now)}/{l2_total}）")
        backend = make_backend("claude_cli", {
            "command": "/usr/bin/claude", "model": args.model,
            "effort": args.effort, "timeout": 300,
        })
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(audit_one_node, backend, kg, n): n for n in sample}
            for fut in as_completed(futs):
                res = fut.result()
                audited_ids_now.add(res["node_id"])
                if res.get("_error"):
                    print(f"  ✗ {res['name'][:30]}: {res['_error']}")
                    continue
                if not res.get("ok", True):
                    ai_flagged.append(res)
                    print(f"  ⚠ {res['numeric_label']:8} {res['name'][:30]}: "
                          f"{(res.get('issues') or ['(无具体说明)'])[0][:50]}")

    # 写 audit JSON
    audit_data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "kg_file": kg_path.name, "kg_book": book,
        "summary": {
            "heuristic_edge_gaps": len(edge_gaps),
            "monotonicity_violations": len(mono),
            "ai_audit_flagged": len(ai_flagged),
        },
        "issues": {
            "heuristic_edge_gaps": edge_gaps,
            "monotonicity_violations": mono,
            "ai_audit_flagged": ai_flagged,
        },
        "audit_progress": {
            "ai_audited_node_ids": sorted(audited_ids_now),
            "last_run": datetime.now().isoformat(timespec="seconds"),
        },
    }
    out_path.write_text(json.dumps(audit_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 写 audit: {out_path}")
    print(f"  总问题数: C={len(edge_gaps)} D={len(mono)} B={len(ai_flagged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
