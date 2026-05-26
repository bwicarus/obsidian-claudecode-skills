"""
kg/link_with_ai.py — 用 AI 判定 KG 节点真正属于哪篇笔记。

替代 link_and_mastery.py 里"模糊字符串匹配"（"基"→"复数的基本性质"那种过宽匹配）。
晚上定时跑（贵但精准），日间用 link_and_mastery 读结果（无 AI 快速更新 mastery）。

策略：
1. 拿所有 Anki record（用户建过卡的笔记 = 跟 KG 相关）
2. 对每篇笔记的 markdown 内容 + 全部 L2 节点列表，让 AI 判定该笔记真正
   覆盖了哪些 L2 节点（严格：必须有定义/定理/例子，不只是顺便提一句）
3. 写到 KG：
   - L2 节点 note_ref 设给真正覆盖它的笔记
   - L2 节点加 note_ref_ai_verified=True 标记
   - 没被任何笔记覆盖的 L2 → note_ref=None，verified=False
4. L1 note_ref 由 majority vote 决定：该 L1 下大多数 L2 verified 到哪篇笔记
   → L1 也关联到那篇

用法：
  python3 scripts/kg/link_with_ai.py --kg knowledge_graph/LADR.json
    [--model opus|sonnet]  默认 sonnet（vision 不需要，语言任务即可）
    [--effort medium|high|max]
    [--workers N]          并行（默认 4，13 篇笔记约 1-2 分钟）
    [--in-place]           写回 KG（默认 dry-run）

注：每篇笔记一次 AI 调用，prompt 含整篇笔记 + 353 节点 ≈ 45K tokens，
sonnet 200K 上下文够。失败的笔记跳过（不影响其它）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402


RECORDS_DIR = config.RECORDS_DIR
VAULT_ROOT = config.VAULT_ROOT


_PROMPT = """你将看到教材《{book}》一篇 Obsidian 笔记的内容，以及该教材所有「原子知识点」候选列表。

这是一个**技能解锁系统**——只有当笔记**完整定义并讲解**了某个知识点，
读者才被视为"已具备解锁该技能的条件"。所以判定必须极严格。

【判定标准 — 极严格，宁可全空也不要错关联】

一个候选要算「严格包括」，必须**同时满足**：

(1) 笔记里有该知识点的**完整明确定义陈述**：
    - 该候选 name / numeric_label 指代的**那个特定对象**有正式定义
    - 形如"X 是 ..."、"称 ... 为 X"、或数学定义符号 :=、∀∃ 等
    - 不是仅"提到了它的名字"
(2) 笔记里有该知识点的**展开**（证明/例子/性质/应用/练习 任一）

【一律不算 — 这些情况下绝对不要列入】
- 笔记里只是提到名字
- 笔记里仅引用（"由 X 可知 ..."）
- 笔记里讲的是相关但**本质不同**的概念
  例：候选"商空间的维数"——笔记必须明确给出"商空间 V/U 的维数 = ..."
       的公式定义+证明/例子。仅讲"维数"的一般定义、或者"向量空间"、
       或者"子空间维数"——**全部不算**。
- 笔记里以"以后会学到"形式提到
- 候选 name 跟笔记主题字面相近但内涵不同（避免字符串误判）

【数量原则】
- 一篇笔记通常只严格包括 **0~3 个**节点
- 整本教材几百个节点，单篇笔记很难覆盖 ≥4 个
- 不确定就不要列入；宁可空数组也比错关联好

【自检】
列入每个候选前自问：
"笔记里有没有这个**特定对象**的**正式定义**？没有就不要列入。"

笔记内容：
————————————
{note_content}
————————————

候选原子知识点（id | 编号 | 名称 | 摘要）：
{candidates}

输出严格 JSON，无任何额外文字：
{{"covered_ids": ["<id1>", ...], "reason": "<20 字内判定依据>"}}
不覆盖任何候选时返回 {{"covered_ids": [], "reason": "..."}}
"""


def load_kg(kg_path: Path) -> dict:
    return json.loads(kg_path.read_text(encoding="utf-8"))


def collect_relevant_notes() -> list[Path]:
    """从 anki/records/ 找用户建过卡的笔记路径。这些是跟 KG 大概率相关的笔记。"""
    notes = []
    if not RECORDS_DIR.exists():
        return notes
    for f in sorted(RECORDS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        sn = d.get("source_note", "")
        if not sn:
            continue
        p = VAULT_ROOT / sn
        if p.exists():
            notes.append(p)
    return notes


def read_note_clean(p: Path) -> str:
    """读笔记 markdown，去掉 frontmatter 和 ankicards 区块（不让 AI 看到自动生成内容）。"""
    txt = p.read_text(encoding="utf-8")
    # 去 frontmatter
    txt = re.sub(r"^---\s*\n.*?\n---\s*\n", "", txt, count=1, flags=re.DOTALL)
    # 去 anki 卡区块（如果有）
    txt = re.sub(r"<!-- ankicards-start -->.*?<!-- ankicards-end -->", "", txt, flags=re.DOTALL)
    txt = re.sub(r"\n## Anki.*?(?=\n## |\Z)", "", txt, flags=re.DOTALL)
    return txt.strip()


def build_candidates_listing(l2_nodes: list[dict]) -> str:
    """构造给 AI 看的候选节点列表。"""
    lines = []
    for n in l2_nodes:
        lines.append(
            f"{n['id']} | {n.get('numeric_label','')} | {n['name']} | {(n.get('summary') or '')[:70]}"
        )
    return "\n".join(lines)


def judge_one_note(backend, book: str, note_path: Path, candidates_listing: str) -> tuple[Path, list[str], str]:
    """对一篇笔记调一次 AI，返回 (note_path, covered_ids, reason 或 error)。"""
    content = read_note_clean(note_path)
    if len(content) < 50:
        return note_path, [], "内容太短跳过"
    prompt = _PROMPT.format(
        book=book,
        note_content=content[:8000],     # 限内容长度防 prompt 爆
        candidates=candidates_listing,
    )
    try:
        raw = backend.chat([{"role": "user", "content": prompt}]).strip()
    except Exception as ex:
        return note_path, [], f"AI 失败: {ex}"
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return note_path, [], f"无 JSON: {raw[:80]}"
    try:
        data = json.loads(raw[s:e+1])
    except Exception as ex:
        return note_path, [], f"JSON 解析失败: {ex}"
    return note_path, list(data.get("covered_ids") or []), data.get("reason", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（试水）")
    ap.add_argument("--since-days", type=int, default=0,
                    help="只对最近 N 天 mtime 改过的笔记跑（增量；默认 0=全跑）")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = load_kg(kg_path)
    l2_nodes = [n for n in kg["nodes"] if n["level"] == 2]
    if not l2_nodes:
        print("KG 无 L2 节点，退出")
        return 1
    book = kg.get("book", "?")
    print(f"KG: {kg_path.name}  book={book}  L2 节点={len(l2_nodes)}")

    # Step 0: Prune 悬空关联——删除 vault 里已经不存在的笔记的关联
    persistent = kg.get("_note_to_covered_l2", {}) or {}
    pruned = []
    for path in list(persistent.keys()):
        if not (VAULT_ROOT / path).exists():
            del persistent[path]
            pruned.append(path)
    if pruned:
        print(f"Prune: 删除 {len(pruned)} 篇已不存在的笔记关联")
        for p in pruned[:5]:
            print(f"  · {p}")
        if len(pruned) > 5:
            print(f"  ... 共 {len(pruned)} 篇")
        kg["_note_to_covered_l2"] = persistent
        # 立刻重建节点字段，确保 prune 生效
        if args.in_place:
            _rebuild_node_fields_from_persistent(kg)

    all_notes = collect_relevant_notes()
    notes = all_notes
    # 增量模式：只对最近 N 天 mtime 改过的笔记跑
    if args.since_days > 0:
        import time
        cutoff = time.time() - args.since_days * 86400
        notes = [p for p in all_notes if p.stat().st_mtime >= cutoff]
        print(f"增量模式 --since-days {args.since_days}：{len(notes)}/{len(all_notes)} 篇笔记被选中")
    if args.limit:
        notes = notes[:args.limit]
    print(f"待判定笔记数: {len(notes)}")
    if not notes:
        # 没要处理的笔记 → 仍要根据已有 _note_to_covered_l2 重建 containing_notes
        if args.in_place:
            print("无新笔记，但仍重建 containing_notes（从持久化字典）")
            _rebuild_from_persistent(kg, kg_path)
        else:
            print("无新笔记可处理")
        return 0

    candidates_listing = build_candidates_listing(l2_nodes)
    print(f"候选列表 {len(candidates_listing)} 字")

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude",
        "model": args.model,
        "effort": args.effort,
        "timeout": 300,
    })

    # 并行调用
    results: dict[Path, tuple[list[str], str]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(judge_one_note, backend, book, p, candidates_listing): p for p in notes}
        for fut in as_completed(futs):
            p, covered, reason = fut.result()
            done += 1
            results[p] = (covered, reason)
            print(f"  [{done}/{len(notes)}] {p.name:<40} → {len(covered)} 个节点  ({reason[:30]})",
                  flush=True)

    # ===== 解锁规则过滤 =====
    # 对每个新关联 (note, node)，校验：
    #   - node.state != "locked"          → 允许
    #   - node.state == "locked" 但前置 mastered ≥ 50%  → 允许
    #   - 否则 → 拒绝（记 _rejected_links 带 note_hash 防重试）
    # 笔记内容 hash 与拒绝记录里的相同 → 同笔记不允许再次尝试同节点
    id2_local = {n["id"]: n for n in kg["nodes"]}
    prereqs_of: dict[str, list[str]] = defaultdict(list)
    for e in kg.get("edges", []):
        if e.get("kind") == "prereq" and id2_local.get(e["to"], {}).get("level") == 2:
            prereqs_of[e["to"]].append(e["from"])
    rejected_links: dict = kg.get("_rejected_links", {}) or {}
    accepted_count = 0
    new_reject_count = 0
    skipped_by_hash_count = 0
    filtered_results: dict[Path, list[str]] = {}
    for note_path, (covered, _) in results.items():
        try:
            rel = note_path.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            rel = note_path.name
        # 计算笔记当前 hash（基于清洗后内容）
        try:
            note_clean = read_note_clean(note_path)
        except Exception:
            note_clean = ""
        note_hash = hashlib.sha1(note_clean.encode("utf-8")).hexdigest()[:16]
        existing_rejects = rejected_links.get(rel, {}) or {}
        kept: list[str] = []
        for nid in covered:
            n = id2_local.get(nid)
            if not n:
                continue
            rj = existing_rejects.get(nid)
            # 规则判定（每次都重新评估，因为节点的 state / 前置 mastered 可能已经变化）
            state = n.get("state", "locked")
            allow = False
            reason = ""
            if state in ("mastered", "unlockable"):
                allow = True
            else:
                prs = prereqs_of.get(nid, [])
                if not prs:
                    reason = "node locked + 无前置数据"
                else:
                    cleared = sum(1 for p in prs
                                  if (id2_local.get(p, {}).get("mastery_level", 0) or 0) >= 2)
                    total = len(prs)
                    ratio = cleared / total
                    if ratio >= 0.5:
                        allow = True
                    else:
                        reason = f"node locked + 前置解锁 {cleared}/{total}={ratio:.0%}"
            if allow:
                kept.append(nid); accepted_count += 1
                # 规则通过 → 清除旧拒绝记录
                if rj: existing_rejects.pop(nid, None)
            else:
                # 规则不通过：检查幂等（同 hash 不再消耗 AI 跑下次；但当前轮的 covered
                # 已经是 AI 出的结果——我们只是不写入。下次跑 AI 时同 hash 可跳过提示
                # AI 不要再列入）。所以这里直接拒绝并更新记录。
                if rj and rj.get("note_hash") == note_hash:
                    skipped_by_hash_count += 1   # 同 hash 之前已记，不算新拒绝
                else:
                    new_reject_count += 1
                existing_rejects[nid] = {
                    "note_hash": note_hash,
                    "reason": reason,
                    "rejected_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                }
        if existing_rejects:
            rejected_links[rel] = existing_rejects
        elif rel in rejected_links:
            del rejected_links[rel]
        filtered_results[note_path] = kept
    kg["_rejected_links"] = rejected_links
    print(f"\n解锁规则过滤：通过 {accepted_count} / 拒绝 {new_reject_count} / "
          f"按 hash 跳过 {skipped_by_hash_count}", flush=True)
    if new_reject_count and new_reject_count <= 20:
        # 列前 20 条拒绝详情
        for note_rel, ids in rejected_links.items():
            for nid, info in ids.items():
                n = id2_local.get(nid, {})
                print(f"  ✗ {note_rel} ↛ [{n.get('numeric_label','?')}] "
                      f"{n.get('name','?')}：{info['reason']}", flush=True)

    # ===== 增量合并：更新 KG 顶层持久化字典 _note_to_covered_l2 =====
    persistent: dict[str, list[str]] = kg.get("_note_to_covered_l2", {}) or {}
    for note_path, covered in filtered_results.items():
        try:
            rel = note_path.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            rel = note_path.name
        persistent[rel] = sorted(set(covered))
    kg["_note_to_covered_l2"] = persistent

    # 从持久化字典重建节点 containing_notes
    n_linked, n_l1_linked = _rebuild_node_fields_from_persistent(kg)
    n2 = sum(1 for n in kg['nodes'] if n['level']==2)
    n1 = sum(1 for n in kg['nodes'] if n['level']==1)
    multi = sum(1 for n in kg['nodes'] if n['level']==2 and len(n.get('containing_notes', []))>1)
    print(f"\nAI 关联结果（含历史持久化数据）:")
    print(f"  L2: {n_linked}/{n2} 关联（其中 {multi} 个节点同时在多篇笔记）")
    print(f"  L1: {n_l1_linked}/{n1} 关联（union 子节点的笔记集合）")
    print(f"  持久化字典含 {len(persistent)} 篇笔记的关联")

    if not args.in_place:
        print("\n（dry-run，未写回；加 --in-place 应用）")
        return 0
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")
    return 0


def _rebuild_node_fields_from_persistent(kg: dict) -> tuple[int, int]:
    """从 kg._note_to_covered_l2 字典重建每个节点的 containing_notes 字段。
    返回 (l2_linked, l1_linked) 计数。"""
    persistent = kg.get("_note_to_covered_l2", {}) or {}
    # 反向构造 node_id -> [note_path, ...]
    node_to_notes: dict[str, list[str]] = defaultdict(list)
    for note_rel, covered_ids in persistent.items():
        for nid in covered_ids:
            if note_rel not in node_to_notes[nid]:
                node_to_notes[nid].append(note_rel)
    n_l2 = 0
    for n in kg["nodes"]:
        if n["level"] != 2: continue
        notes = sorted(node_to_notes.get(n["id"], []))
        if notes:
            n["containing_notes"] = notes
            n["note_ref"] = notes[0]
            n["note_ref_ai_verified"] = True
            n_l2 += 1
        else:
            n["containing_notes"] = []
            n["note_ref"] = ""
            n["note_ref_ai_verified"] = False
    # L1 = union 子 L2 的 containing_notes
    l1_notes: dict[str, set[str]] = defaultdict(set)
    for n in kg["nodes"]:
        if n["level"] == 2 and n.get("containing_notes") and n.get("parent_id"):
            for nt in n["containing_notes"]:
                l1_notes[n["parent_id"]].add(nt)
    n_l1 = 0
    for n in kg["nodes"]:
        if n["level"] != 1: continue
        nts = sorted(l1_notes.get(n["id"], set()))
        if nts:
            n["containing_notes"] = nts; n["note_ref"] = nts[0]
            n["note_ref_ai_verified"] = True; n_l1 += 1
        else:
            n["containing_notes"] = []; n["note_ref"] = ""
            n["note_ref_ai_verified"] = False
    return n_l2, n_l1


def _rebuild_from_persistent(kg: dict, kg_path: Path) -> None:
    """无新笔记可处理时的纯 rebuild path（不调 AI）。"""
    n_l2, n_l1 = _rebuild_node_fields_from_persistent(kg)
    print(f"  从持久化字典重建：L2 {n_l2} 关联，L1 {n_l1} 关联")
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")


if __name__ == "__main__":
    sys.exit(main())
