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
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = load_kg(kg_path)
    l2_nodes = [n for n in kg["nodes"] if n["level"] == 2]
    if not l2_nodes:
        print("KG 无 L2 节点，退出")
        return 1
    book = kg.get("book", "?")
    print(f"KG: {kg_path.name}  book={book}  L2 节点={len(l2_nodes)}")

    notes = collect_relevant_notes()
    if args.limit:
        notes = notes[:args.limit]
    print(f"待判定笔记数: {len(notes)}")
    if not notes:
        print("无笔记可处理"); return 0

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

    # 写 KG: 每个 L2 节点维护 containing_notes 列表（可能多篇笔记包含同一知识点）
    # 反向映射 node_id -> [note_rel_path, ...]
    node_to_notes: dict[str, list[str]] = defaultdict(list)
    for note_path, (covered, _) in results.items():
        try:
            rel = note_path.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            rel = note_path.name
        for nid in covered:
            if rel not in node_to_notes[nid]:
                node_to_notes[nid].append(rel)

    id2 = {n["id"]: n for n in kg["nodes"]}
    # L2 关联：containing_notes 数组 + note_ref 兼容字段（首项）
    n_linked = 0
    for n in kg["nodes"]:
        if n["level"] != 2: continue
        notes = node_to_notes.get(n["id"], [])
        if notes:
            n["containing_notes"] = sorted(notes)
            n["note_ref"] = notes[0]
            n["note_ref_ai_verified"] = True
            n_linked += 1
        else:
            n["containing_notes"] = []
            n["note_ref"] = ""
            n["note_ref_ai_verified"] = False

    # L1 关联：所有子 L2 的 containing_notes 取 union
    l1_notes: dict[str, set[str]] = defaultdict(set)
    for n in kg["nodes"]:
        if n["level"] == 2 and n.get("containing_notes") and n.get("parent_id"):
            for nt in n["containing_notes"]:
                l1_notes[n["parent_id"]].add(nt)
    n_l1_linked = 0
    for n in kg["nodes"]:
        if n["level"] != 1: continue
        nts = sorted(l1_notes.get(n["id"], set()))
        if nts:
            n["containing_notes"] = nts
            n["note_ref"] = nts[0]
            n["note_ref_ai_verified"] = True
            n_l1_linked += 1
        else:
            n["containing_notes"] = []
            n["note_ref"] = ""
            n["note_ref_ai_verified"] = False

    print(f"\nAI 关联结果:")
    n2 = sum(1 for n in kg['nodes'] if n['level']==2)
    n1 = sum(1 for n in kg['nodes'] if n['level']==1)
    multi = sum(1 for n in kg['nodes'] if n['level']==2 and len(n.get('containing_notes', []))>1)
    print(f"  L2: {n_linked}/{n2} 关联（其中 {multi} 个节点同时在多篇笔记）")
    print(f"  L1: {n_l1_linked}/{n1} 关联（union 子节点的笔记集合）")

    if not args.in_place:
        print("\n（dry-run，未写回；加 --in-place 应用）")
        return 0
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写回 {kg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
