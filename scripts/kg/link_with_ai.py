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

这是一个**技能解锁系统**——当笔记**实质涉及/讨论**了某个知识点，
读者就被视为"已接触过该技能"。判定要**宽松而准确**：宁可多关联一个相关的，
也不要漏掉笔记里**明显在讲**的节点。

【判定标准 — 宽松但准确】

一个候选要算「关联」，**满足下面任一条件即可**：

(A) 笔记里有该知识点的**定义** 或 **形式陈述**（"X 是 ..."、"称 ... 为 X"、数学 :=、∀∃ 等）
(B) 笔记里有该知识点的**性质/定理/例子/推论/练习**（哪怕只展开一两条性质，也算）
(C) 笔记里**反复操作/计算/应用**该知识点（不只是引用一句）

也就是说，**散布式覆盖**（笔记用多个小节分别讲该知识点的不同性质）**也算关联**——
不要求集中在一个标题下、也不要求"完整"。

【还是不要列入的情况】
- 笔记里只是**一句话提到名字**，没有任何展开
- 笔记里**仅作为引用**（"由 X 可知 ..."，X 是这次要联系的节点）
- 候选 name 与笔记主题字面相近但**内涵完全不同**
- 笔记里以"以后会学到"形式提到

【KG 节点信息可能不准】
- 候选的 `name` 可能跟 PDF 实际标题不一致（OCR / 早期识别错误）
- **以 summary 描述的实际内容为准**，再对照笔记内容判定
- 例：候选 name 是"复数的加法与乘法"但 summary 写"实际标题为「复数的算术性质」六条性质"
  → 用 summary 的描述判定，不被错误 name 误导

【数量原则】
- 一篇笔记通常关联 **0~6 个**节点
- 不确定时**倾向关联**而非排除——后续会有反向验证兜底

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


# 反向验证：对未被任何笔记关联的节点，问 AI "哪些笔记可能覆盖你"，取并集
_REVERSE_PROMPT = """你看到教材《{book}》的一个原子知识点，以及该书相关的多篇 Obsidian 笔记摘要列表。

**任务**：找出**明显在讲该知识点**的笔记。判定标准与正向一致（满足定义/性质/例子/操作任一即可）。

【特别注意】
- 节点 `name` 可能跟 PDF 实际标题有出入，请以 `summary` 描述的实际内容为准
- 笔记标题可能用不同措辞（如节点叫"复数的算术性质"，笔记叫"复数的基本性质"）—— 看内容判定
- 散布式覆盖（笔记多个小节分别讲该节点的性质）**也算关联**

节点：
  id: {node_id}
  name: {node_name}
  numeric_label: {node_label}
  summary: {node_summary}

候选笔记摘要列表（rel_path | 笔记前 300 字摘要）：
{notes_listing}

输出严格 JSON，无任何额外文字：
{{"covering_notes": ["<rel_path1>", ...], "reason": "<20 字内判定依据>"}}
没有任何笔记覆盖时返回 {{"covering_notes": [], "reason": "..."}}
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


# ── 反向验证：节点维度问 AI "哪些笔记覆盖了你"，捕捉正向漏判 ────────────────

# 中文停用词 + 单字符 token 过滤
_STOP = set("的是在了和与或一个对从以为而但所就也都把及就行得对于")

def _keywords_from(text: str) -> set[str]:
    """从字符串提取 2+ 字的中文 token + 字母数字 token，作为关键词集合。"""
    if not text:
        return set()
    out = set()
    # 中文 token：2-6 字连续中文片段
    for m in re.findall(r"[一-鿿]{2,6}", text):
        if m not in _STOP:
            out.add(m)
    # 字母数字 token（≥2 字符）
    for m in re.findall(r"[A-Za-z][A-Za-z0-9_^]{1,}", text):
        out.add(m.lower())
    return out


def _filter_candidate_notes_for_node(node: dict, all_notes_text: dict[str, str]) -> list[str]:
    """对给定 L2 节点，用关键词重叠启发式筛出可能覆盖它的笔记 rel_path 列表（最多 10 篇）。"""
    node_kw = _keywords_from(node.get("name","")) | _keywords_from(node.get("summary",""))
    if not node_kw:
        return []
    scored = []
    for rel, text in all_notes_text.items():
        note_kw = _keywords_from(rel) | _keywords_from(text[:1500])
        overlap = len(node_kw & note_kw)
        if overlap >= 1:
            scored.append((overlap, rel))
    scored.sort(reverse=True)
    return [r for _, r in scored[:10]]


def judge_reverse_one_node(backend, book: str, node: dict, all_notes_text: dict[str, str]
                            ) -> tuple[str, list[str], str]:
    """反向：节点维度问 AI 哪些笔记覆盖它。返回 (node_id, [rel_paths], reason)。"""
    candidates = _filter_candidate_notes_for_node(node, all_notes_text)
    if not candidates:
        return node["id"], [], "无关键词候选"
    listing_lines = []
    for rel in candidates:
        snippet = (all_notes_text.get(rel, "") or "").replace("\n", " ").strip()[:300]
        listing_lines.append(f"{rel} | {snippet}")
    prompt = _REVERSE_PROMPT.format(
        book=book,
        node_id=node["id"],
        node_name=node.get("name",""),
        node_label=node.get("numeric_label",""),
        node_summary=(node.get("summary") or "")[:200],
        notes_listing="\n".join(listing_lines),
    )
    try:
        raw = backend.chat([{"role": "user", "content": prompt}]).strip()
    except Exception as ex:
        return node["id"], [], f"AI 失败: {ex}"
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return node["id"], [], f"无 JSON: {raw[:80]}"
    try:
        data = json.loads(raw[s:e+1])
    except Exception as ex:
        return node["id"], [], f"JSON 解析失败: {ex}"
    covering = list(data.get("covering_notes") or [])
    # 过滤掉不在候选列表里的（AI 偶尔会编造路径）
    covering = [c for c in covering if c in candidates]
    return node["id"], covering, data.get("reason", "")


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
    ap.add_argument("--skip-reverse", action="store_true",
                    help="跳过反向验证（默认开启反向兜底：未关联节点 + 本次笔记关键词重叠 → 跑反向 AI）")
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

    # ===== 增量合并：更新 KG 顶层持久化字典 _note_to_covered_l2 =====
    persistent: dict[str, list[str]] = kg.get("_note_to_covered_l2", {}) or {}
    for note_path, (covered, _) in results.items():
        try:
            rel = note_path.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            rel = note_path.name
        persistent[rel] = sorted(set(covered))
    kg["_note_to_covered_l2"] = persistent

    # ===== 反向验证：对仍未关联的节点跑反向 AI 抽问 "哪些笔记覆盖了你" =====
    if not args.skip_reverse:
        # 计算当前未关联的 L2（containing_notes 空），且关键词在最近改过的笔记里有重叠
        covered_node_ids = set()
        for cids in persistent.values():
            covered_node_ids.update(cids)
        # 准备 all_notes_text（最近 since-days 或本次处理的笔记的清洗内容）
        all_notes_text: dict[str, str] = {}
        for p in notes:   # 仅扫本次处理的笔记，避免给 AI 太多噪声
            try:
                rel = p.relative_to(VAULT_ROOT).as_posix()
            except ValueError:
                rel = p.name
            try:
                all_notes_text[rel] = read_note_clean(p)
            except Exception:
                pass
        unlinked_nodes = [n for n in l2_nodes if n["id"] not in covered_node_ids]
        # 进一步筛：只对"跟最近笔记有关键词重叠"的未关联节点跑反向
        reverse_candidates = []
        for n in unlinked_nodes:
            if _filter_candidate_notes_for_node(n, all_notes_text):
                reverse_candidates.append(n)
        print(f"\n反向验证：{len(reverse_candidates)} 个未关联节点跟本次笔记有关键词重叠，跑反向 AI", flush=True)
        if reverse_candidates:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                rfuts = {pool.submit(judge_reverse_one_node, backend, book, n, all_notes_text): n
                         for n in reverse_candidates}
                done = 0
                added = 0
                for fut in as_completed(rfuts):
                    nid, covering, reason = fut.result()
                    done += 1
                    if covering:
                        for rel in covering:
                            if rel not in persistent:
                                persistent[rel] = []
                            if nid not in persistent[rel]:
                                persistent[rel] = sorted(set(persistent[rel] + [nid]))
                                added += 1
                    nname = next((n['name'] for n in reverse_candidates if n['id']==nid), '?')
                    print(f"  [{done}/{len(reverse_candidates)}] {nname[:30]:<30} ← {len(covering)} 篇笔记  ({reason[:30]})",
                          flush=True)
            print(f"反向验证补关联：{added} 条 (node, note) 对", flush=True)
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
