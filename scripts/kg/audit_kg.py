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
from lib.claude_quota import can_run_more, can_run_aggressive, util_5h  # noqa: E402
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

_DEEP_AUDIT_PROMPT = """你是教材《{book}》知识图谱的资深审稿人，要做最严格的节点审查。

【节点元数据】
id: {nid}
编号: {label}
名称: {name}
类型: {type}
摘要: {summary}
所在节: {parent}
页码: {pages}

【PDF 该 page(s) 实际内容】
————
{pdf_text}
————

【该节兄弟节点】（同 L1 父下其它 L2）
{siblings}

请严格判断（一律返回 JSON，不要别的文字）：
1) PDF 内容里是否真的有「{name}」这个**特定对象**的明确定义/陈述/性质？
2) 节点 name/summary/numeric_label 是否准确？
3) 若 PDF 里 "{label}" 是例子/图注/无关内容（AI 误识别）→ action=delete
4) 若跟某兄弟节点是同一概念 → action=merge + target_id
5) 若 summary 不准但节点真实存在 → action=fix_summary + new_summary
6) 若 pages 错（PDF 这页没有此节点，应该在别页）→ action=fix_pages + new_pages（int 数组）
7) 都没问题 → action=ok

输出 JSON：
{{
  "action": "ok" | "merge" | "delete" | "fix_summary" | "fix_pages",
  "target_id": "<merge 时给>",
  "new_summary": "<fix_summary 时给>",
  "new_pages": [<int>, ...],
  "reason": "<30 字内判定依据>"
}}
"""


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


def audit_deep_one_node(backend, kg: dict, node: dict, pdf_cache: dict) -> dict:
    """深度审计：含 PDF 实际内容；判定 ok/merge/delete/fix_summary/fix_pages。"""
    id2 = {n["id"]: n for n in kg["nodes"]}
    parent = id2.get(node.get("parent_id"))
    siblings = [n for n in kg["nodes"]
                if n["level"] == 2 and n.get("parent_id") == node.get("parent_id")
                and n["id"] != node["id"]]
    sib_text = "\n".join(
        f"  {s['id']} | {s.get('numeric_label','')} | {s['name']} | {(s.get('summary','') or '')[:50]}"
        for s in siblings[:30]
    ) or "  （无）"
    # PDF 内容（带缓存避免重复 open）
    pages = node.get("pages") or []
    pdf_text = ""
    if pages and pdf_cache.get("doc"):
        doc = pdf_cache["doc"]
        parts = []
        for pg in pages[:3]:   # 最多 3 页
            if 1 <= pg <= len(doc):
                parts.append(f"=== PDF page {pg} ===\n" + doc[pg-1].get_text()[:3000])
        pdf_text = "\n\n".join(parts) or "（无 PDF 内容）"
    else:
        pdf_text = "（KG 无 PDF 路径或页码缺失）"

    prompt = _DEEP_AUDIT_PROMPT.format(
        book=kg.get("book", "?"), nid=node["id"],
        label=node.get("numeric_label", ""), name=node["name"],
        type=node.get("type", ""), summary=node.get("summary", ""),
        parent=parent["name"] if parent else "?",
        pages=", ".join(map(str, pages)) or "?",
        pdf_text=pdf_text, siblings=sib_text,
    )
    try:
        raw = backend.chat([{"role": "user", "content": prompt}]).strip()
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
        return {"node_id": node["id"], "name": node["name"], "_error": f"JSON: {ex}"}
    data["node_id"] = node["id"]; data["name"] = node["name"]
    data["numeric_label"] = node.get("numeric_label", "")
    return data


def node_audit_hash(n: dict) -> str:
    """节点内容指纹:编号/名称/摘要/类型/页码任一变动 → 哈希变 → 增量模式会重审它。
    审查读的就是这些字段 + 该页 PDF 文本;这些没变、重审基本只会返回 ok,所以可跳过。"""
    import hashlib
    pages = n.get("pages") or []
    key = "\x1f".join([
        str(n.get("numeric_label") or n.get("label") or ""),
        str(n.get("name") or ""),
        str(n.get("summary") or ""),
        str(n.get("type") or ""),
        ",".join(str(p) for p in pages),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


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
    ap.add_argument("--ai-sample-size", type=int, default=20,
                    help="每批 AI 抽样大小（默认 20）")
    ap.add_argument("--ai-skip", action="store_true", help="跳过 AI 审稿（开发调试）")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(STATE_DIR / "kg_audit.json"))
    # Budget-gated 激进模式：实时查 quota + 时间感知，保证 target 时刻满血
    ap.add_argument("--budget-loop", action="store_true",
                    help="循环跑直到 quota 触限或安全 cutoff（凌晨用，激进消耗夜间空闲额度）")
    ap.add_argument("--target-hour", type=int, default=9,
                    help="目标满血时刻小时（默认 9，即早 9:00）")
    ap.add_argument("--target-min", type=int, default=0)
    ap.add_argument("--buffer-min", type=int, default=30,
                    help="安全 cutoff 前的 buffer 分钟（默认 30）")
    ap.add_argument("--budget-target-7d", type=float, default=88.0,
                    help="7d 窗口 utilization 上限（默认 88，保护周窗口不爆）")
    ap.add_argument("--budget-max-batches", type=int, default=30,
                    help="budget-loop 硬上限批数（防意外，默认 30 批 × 20 节点 = 600）")
    ap.add_argument("--budget-5h-cap", type=float, default=100.0,
                    help="5h 窗口利用率天花板（默认 100=不限）。can_run_aggressive 故意只看 7d+时间、不看 5h 窗口，"
                         "导致 7d 处于周低点时单夜能把 5h 烧满、锁死当天用量。设低于 100（如 70）"
                         "让 budget-loop 在 5h 达此值时也停，防单夜独占整个 5h 窗口。")
    # 深度审计：含 PDF 内容验证 + 自动 apply safe ops（fix_summary）
    ap.add_argument("--deep", action="store_true",
                    help="深度模式：每节点跑 PDF 内容验证，能输出 fix_summary/fix_pages/merge/delete 建议")
    ap.add_argument("--auto-apply-safe", action="store_true",
                    help="深度模式下自动 apply safe ops（fix_summary）；risky ops 仍写入 audit 待 review")
    ap.add_argument("--incremental", action="store_true",
                    help="增量审查:只审『新增/内容变动过』的节点(按 node_audit_hash 比对 audit_progress.audited_hashes),"
                         "不再全图轮转重审。首跑某书时用当前哈希建基线、本轮跳过(信任刚被全量审过的现状)。"
                         "根治『每晚把全图几百节点重审一遍』的浪费。")
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
    # 增量审查用的全局节点哈希表 {node_id: 内容哈希}。node id 自带书前缀(egiu.* / ladr.*)不会撞,
    # 故多本书共用这一张表、写回时合并保留其它书的条目(顺带绕开 kg_audit.json 被各书互相覆盖的坑)。
    audited_hashes: dict = dict(((prev.get("audit_progress") or {}).get("audited_hashes") or {}))

    # C
    edge_gaps = heuristic_edge_gaps(kg)
    print(f"[C] 启发式: {len(edge_gaps)} 条")
    # D
    mono = monotonicity_violations(kg)
    print(f"[D] 单调性: {len(mono)} 条")
    # B
    ai_flagged: list[dict] = []
    audited_ids_now = set(prev_audited_ids)
    auto_applied: list[dict] = []     # 深度模式 safe ops 自动应用记录
    if not args.ai_skip:
        l2_total = sum(1 for n in kg["nodes"] if n["level"]==2)
        baseline_seeded = False
        if args.incremental:
            this_l2 = [n for n in kg["nodes"] if n["level"] == 2]
            covered = sum(1 for n in this_l2 if n["id"] in audited_hashes)
            if covered == 0 and this_l2:
                # 本书还没哈希基线 → 用当前节点哈希建基线、本轮跳过审查
                # (信任刚被全量审过的现状;以后只审 hash 变了的=新增/改过的节点)
                for n in this_l2:
                    audited_hashes[n["id"]] = node_audit_hash(n)
                baseline_seeded = True
                print(f"[B] 增量首跑:建立 {len(this_l2)} 节点哈希基线,本轮跳过审查(以后只审变动/新增)")
        # 已完成全图 → 清空进度重审(仅全量模式;增量模式靠哈希判变动,不轮转)
        elif len(prev_audited_ids) >= l2_total:
            print(f"[B] 已完成全图轮转（{l2_total} 节点），清空 ai_audited_node_ids 重新开始")
            audited_ids_now = set()
        backend = make_backend("claude_cli", {
            "command": "/usr/bin/claude", "model": args.model,
            "effort": args.effort, "timeout": 300,
        })

        # 深度模式：加载 PDF
        pdf_cache = {}
        if args.deep:
            pdf_path = kg.get("pdf")
            if pdf_path and Path(pdf_path).exists():
                try:
                    import fitz
                    pdf_cache["doc"] = fitz.open(pdf_path)
                    print(f"[DEEP] 加载 PDF: {pdf_path}（{len(pdf_cache['doc'])} 页）")
                except Exception as ex:
                    print(f"[DEEP] PDF 加载失败: {ex}")

        def _pick_sample():
            if not args.incremental:
                return select_audit_sample(kg, audited_ids_now, args.ai_sample_size)
            # 增量:只挑哈希变了的(新增节点缺哈希 → 也算变动)且本轮还没审过的
            l2 = [x for x in kg["nodes"] if x["level"] == 2]
            todo = [x for x in l2 if x["id"] not in audited_ids_now
                    and node_audit_hash(x) != audited_hashes.get(x["id"])]
            return random.sample(todo, min(args.ai_sample_size, len(todo))) if todo else []

        def run_one_batch():
            sample = _pick_sample()
            if not sample:
                return False
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                if args.deep:
                    futs = {pool.submit(audit_deep_one_node, backend, kg, n, pdf_cache): n for n in sample}
                else:
                    futs = {pool.submit(audit_one_node, backend, kg, n): n for n in sample}
                for fut in as_completed(futs):
                    res = fut.result()
                    audited_ids_now.add(res["node_id"])
                    if res.get("_error"):
                        print(f"  ✗ {res['name'][:30]}: {res['_error']}")
                        continue
                    if args.deep:
                        action = res.get("action", "ok")
                        if action == "ok":
                            continue
                        # safe op: fix_summary - 可自动 apply
                        if action == "fix_summary" and args.auto_apply_safe and res.get("new_summary"):
                            for n in kg["nodes"]:
                                if n["id"] == res["node_id"]:
                                    n["summary"] = res["new_summary"]
                                    break
                            auto_applied.append({"action": "fix_summary", **res})
                            print(f"  ✓ fix_summary {res['numeric_label']:8} {res['name'][:25]}: 已 apply")
                        else:
                            # risky ops (merge/delete/fix_pages) 仅记录待 review
                            ai_flagged.append(res)
                            print(f"  ⚠ {action:14} {res['numeric_label']:8} {res['name'][:25]}: "
                                  f"{res.get('reason','')[:40]}")
                    else:
                        if not res.get("ok", True):
                            ai_flagged.append(res)
                            print(f"  ⚠ {res['numeric_label']:8} {res['name'][:30]}: "
                                  f"{(res.get('issues') or ['(无具体说明)'])[0][:50]}")
            # 增量:记录本批节点(若 fix_summary 已就地修正则记修正后)的最新哈希,下次不再重审
            if args.incremental:
                for nd in sample:
                    audited_hashes[nd["id"]] = node_audit_hash(nd)
            return True

        if baseline_seeded:
            pass   # 增量首跑只建哈希基线,本轮不发任何 AI 调用
        elif args.budget_loop:
            # 时间感知激进模式：保证 target_hour:target_min 时 5h 窗口自然清零
            print(f"[B] BUDGET-LOOP 时间感知模式")
            print(f"    目标满血时刻：{args.target_hour:02d}:{args.target_min:02d}")
            print(f"    安全 cutoff：5h - {args.buffer_min}min buffer = "
                  f"{args.target_hour - 5}:{args.target_min:02d}（左右）后停跑")
            print(f"    7d 触限阈值：{args.budget_target_7d:.0f}%")
            print(f"    5h 天花板：{args.budget_5h_cap:.0f}%")
            print(f"    硬上限：{args.budget_max_batches} 批 × {args.ai_sample_size} 节点")
            for batch_i in range(args.budget_max_batches):
                ok, reason = can_run_aggressive(
                    target_hour=args.target_hour, target_min=args.target_min,
                    buffer_min=args.buffer_min, target_7d_util=args.budget_target_7d)
                if not ok:
                    print(f"  [批 {batch_i+1}] STOP - {reason}")
                    break
                _u5 = util_5h()   # can_run_aggressive 不看 5h 窗口 → 这里补一道天花板,防单夜把 5h 烧满锁死当天用量
                if _u5 >= args.budget_5h_cap:
                    print(f"  [批 {batch_i+1}] STOP - 5h 窗口已达 {_u5:.0f}% >= 天花板 {args.budget_5h_cap:.0f}%")
                    break
                print(f"  [批 {batch_i+1}/{args.budget_max_batches}] {reason}, 5h={_u5:.0f}%, 累计 {len(audited_ids_now)}/{l2_total}")
                if not run_one_batch():
                    print("  无更多节点可审，停"); break
        else:
            # 单批模式：跑一批就停
            print(f"[B] AI 抽样 {args.ai_sample_size} 节点（累计已审 {len(audited_ids_now)}/{l2_total}）")
            run_one_batch()

    # 深度模式 auto_apply 有改动 → 写回 KG
    if args.deep and auto_applied:
        kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[DEEP] 已 auto-apply {len(auto_applied)} 个 safe ops 到 KG")

    # 写 audit JSON
    audit_data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "kg_file": kg_path.name, "kg_book": book,
        "summary": {
            "heuristic_edge_gaps": len(edge_gaps),
            "monotonicity_violations": len(mono),
            "ai_audit_flagged": len(ai_flagged),
            "auto_applied": len(auto_applied),
        },
        "issues": {
            "heuristic_edge_gaps": edge_gaps,
            "monotonicity_violations": mono,
            "ai_audit_flagged": ai_flagged,
        },
        "auto_applied": auto_applied,
        "audit_progress": {
            "ai_audited_node_ids": sorted(audited_ids_now),
            "audited_hashes": audited_hashes,   # 增量审查基线(全局,跨书合并)
            "last_run": datetime.now().isoformat(timespec="seconds"),
        },
    }
    out_path.write_text(json.dumps(audit_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 写 audit: {out_path}")
    print(f"  总问题数: C={len(edge_gaps)} D={len(mono)} B={len(ai_flagged)} auto-applied={len(auto_applied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
