"""kg/rescan_rolling.py — 滚动重扫 PDF，对比主 KG 更新节点。

每晚跑一次，选下一批 N 页让 build_nodes 子集模式重扫，把结果跟主 KG 比对：
  - safe diff（summary 微调）→ 自动 apply
  - risky diff（节点 name 改 / 节点消失 / 新增节点）→ 写 audit JSON 等 review

进度持久化 state/rescan_progress.json，按 [start, end] 滚动覆盖全书。
集成到 daily：跟 audit_kg --deep 并列消耗夜间 token，时间感知 cutoff。

用法：
  python3 scripts/kg/rescan_rolling.py --kg knowledge_graph/LADR.json
    [--pages-per-night 30]    每晚扫多少页（默认 30）
    [--workers 4] [--model sonnet] [--effort medium]
    [--target-hour 9] [--buffer-min 30]   时间感知 cutoff，过点不跑
    [--auto-apply-safe]       summary 差异自动 apply
"""
from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa
from lib.claude_quota import time_to_safe_cutoff, can_run_aggressive  # noqa


PROJECT_DIR = config.PROJECT_DIR
STATE_DIR = config.STATE_DIR
PROGRESS_FILE = STATE_DIR / "rescan_progress.json"


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_progress(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def select_next_pages(kg: dict, prog: dict, n: int,
                       stable_threshold: int = 3,
                       max_stable_days: int = 90) -> list[int]:
    """智能选页：稳定度判定 + 学习前沿优先。返回离散页列表（≤ n）。

    优先级（高→低）：
      1. unlockable / mastered 节点但**无 containing_notes** 的 pages
      2. mastered 节点的直接 successors（一跳后续，没笔记的）
      3. mastered 节点的两跳 successors
      4. 当前最深 mastered 章节的下一章节里的节点

    跳过：
      - 节点已有 containing_notes（笔记已存在 = 内容已验证）
      - stable=true 的页（连续 stable_threshold 次扫描无变化）
      - **例外**：stable 但超 max_stable_days 没扫过 → 强制扫一次防 KG 漂移
    """
    from collections import defaultdict
    id2 = {n["id"]: n for n in kg["nodes"]}
    edges = kg.get("edges", [])
    succ = defaultdict(set)
    for e in edges:
        if e.get("kind") == "prereq":
            succ[e["from"]].add(e["to"])

    def has_notes(n): return bool(n.get("containing_notes"))
    now = datetime.now()
    history: dict = prog.get("page_history") or {}

    def is_stable(page: int) -> bool:
        h = history.get(str(page)) or {}
        if not h.get("stable"): return False
        # 稳定但超 max_stable_days 没扫 → 强制重扫一次
        scans = h.get("scans") or []
        if not scans: return False
        try:
            last_at = datetime.fromisoformat(scans[-1]["at"])
            if (now - last_at).total_seconds() > max_stable_days * 86400:
                return False
        except Exception:
            return False
        return True

    candidates: list[tuple[int, int]] = []   # [(priority, page)]
    # === 1. unlockable / mastered 无笔记 ===
    for nd in kg["nodes"]:
        if nd["level"] != 2: continue
        if nd.get("state") in ("unlockable", "mastered") and not has_notes(nd):
            for p in (nd.get("pages") or []):
                candidates.append((1, p))
    # === 2/3. mastered 节点的一跳 / 两跳后继 ===
    mastered_ids = {nd["id"] for nd in kg["nodes"]
                    if nd["level"] == 2 and nd.get("state") == "mastered"}
    one_hop = set()
    for mid in mastered_ids:
        one_hop.update(succ.get(mid, ()))
    one_hop -= mastered_ids
    two_hop = set()
    for did in one_hop:
        two_hop.update(succ.get(did, ()))
    two_hop -= mastered_ids; two_hop -= one_hop
    for nid in one_hop:
        nd = id2.get(nid)
        if not nd or has_notes(nd): continue
        for p in (nd.get("pages") or []):
            candidates.append((2, p))
    for nid in two_hop:
        nd = id2.get(nid)
        if not nd or has_notes(nd): continue
        for p in (nd.get("pages") or []):
            candidates.append((3, p))
    # === 4. 最深 mastered 章节的下一章节 ===
    def chap_of(node):
        cur = node
        while cur and cur.get("parent_id"):
            par = id2.get(cur["parent_id"])
            if par and par.get("level") == 0:
                return par["id"]
            cur = par
        return None
    chap_order = [c["id"] for c in kg["nodes"] if c["level"] == 0]
    mastered_chap = {chap_of(id2[nid]) for nid in mastered_ids if id2.get(nid)}
    mastered_chap.discard(None)
    deepest_idx = -1
    for i, c in enumerate(chap_order):
        if c in mastered_chap:
            deepest_idx = max(deepest_idx, i)
    if 0 <= deepest_idx < len(chap_order) - 1:
        next_chap_id = chap_order[deepest_idx + 1]
        for nd in kg["nodes"]:
            if nd["level"] != 2: continue
            if chap_of(nd) != next_chap_id: continue
            if has_notes(nd): continue
            for p in (nd.get("pages") or []):
                candidates.append((4, p))

    # 排序 + 去重 + 跳过稳定页 + 取 N 页
    candidates.sort(key=lambda x: (x[0], x[1]))
    seen, out, skipped_stable = set(), [], 0
    for prio, pg in candidates:
        if pg in seen: continue
        if is_stable(pg):
            skipped_stable += 1; continue
        seen.add(pg); out.append(pg)
        if len(out) >= n: break
    if skipped_stable:
        print(f"  跳过稳定页 {skipped_stable} 个（连续 ≥{stable_threshold} 次无变化）")
    print(f"  候选页（按学习前沿排序前 {len(out)} 页）: {out}")
    return sorted(out)


def record_scan_outcome(prog: dict, page: int, had_changes: bool,
                         stable_threshold: int = 3, max_history: int = 5,
                         change_summary: str = "") -> dict:
    """记录单页扫描结果。
      - 追加 scans 历史
      - 有变化 → stable_streak 重置为 0
      - 无变化 → stable_streak++；达 stable_threshold → stable=true
    """
    history = prog.setdefault("page_history", {})
    key = str(page)
    h = history.setdefault(key, {"scans": [], "stable_streak": 0, "stable": False})
    h["scans"].append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "had_changes": bool(had_changes),
        "summary": change_summary[:120] if change_summary else "",
    })
    # 限制历史长度
    if len(h["scans"]) > max_history:
        h["scans"] = h["scans"][-max_history:]
    # 更新 streak
    if had_changes:
        h["stable_streak"] = 0
        h["stable"] = False
    else:
        h["stable_streak"] = h.get("stable_streak", 0) + 1
        if h["stable_streak"] >= stable_threshold:
            h["stable"] = True
    return h


def pages_to_ranges(pages: list[int], gap: int = 2) -> list[tuple[int, int]]:
    """把离散页聚合成连续段（相邻间距 ≤ gap 合并）。"""
    if not pages: return []
    pages = sorted(set(pages))
    ranges = [(pages[0], pages[0])]
    for p in pages[1:]:
        last_s, last_e = ranges[-1]
        if p - last_e <= gap:
            ranges[-1] = (last_s, p)
        else:
            ranges.append((p, p))
    return ranges


def run_build_nodes_shadow_ranges(kg: dict, kg_path: Path, ranges: list[tuple[int, int]],
                                   workers: int, model: str, effort: str) -> Path | None:
    """对多个连续 page range 各调一次 build_nodes，合并 shadow 结果。返回最终 shadow 路径。"""
    pdf = kg.get("pdf")
    if not pdf:
        return None
    book = kg.get("book", kg_path.stem)
    book_full = kg.get("book_full", book)
    tmpdir = Path(tempfile.mkdtemp(prefix="rescan-shadow-"))
    shadow_nodes: list[dict] = []
    for i, (s, e) in enumerate(ranges):
        out = tmpdir / f"{book}.shadow.{i}.json"
        cmd = [
            sys.executable, str(PROJECT_DIR / "scripts" / "kg" / "build_nodes.py"),
            "--pdf", pdf, "--book", book, "--book-full", book_full,
            "--out", str(out),
            "--pages", f"{s}-{e}",
            "--workers", str(workers), "--model", model, "--effort", effort,
        ]
        print(f"  调 build_nodes range {i+1}/{len(ranges)}: pages {s}-{e}")
        r = subprocess.run(cmd, capture_output=False)
        if r.returncode != 0 or not out.exists():
            print(f"    range {s}-{e} 失败，跳过")
            continue
        try:
            sub = json.loads(out.read_text(encoding="utf-8"))
            shadow_nodes.extend(sub.get("nodes") or [])
        except Exception as ex:
            print(f"    读 shadow 失败: {ex}")
    if not shadow_nodes:
        return None
    # 拼装合并 shadow
    merged = tmpdir / f"{book}.shadow.merged.json"
    merged.write_text(json.dumps({
        "book": book, "nodes": shadow_nodes, "edges": []
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def diff_nodes(main_kg: dict, shadow_kg: dict, scan_pages: set[int]) -> dict:
    """对比主 KG 跟 shadow 上 scan_pages 内的 L2 节点。
    返回 {safe_changes: [...], risky_changes: [...]}"""
    in_range = lambda n: any(p in scan_pages for p in (n.get("pages") or []))
    main_l2 = [n for n in main_kg["nodes"] if n["level"] == 2 and in_range(n)]
    shadow_l2 = [n for n in shadow_kg["nodes"] if n["level"] == 2 and in_range(n)]

    # 用 (parent_id, numeric_label) 做匹配键
    def key(n):
        return (n.get("parent_id", ""), (n.get("numeric_label", "") or "").strip())

    main_by_key = {key(n): n for n in main_l2}
    shadow_by_key = {key(n): n for n in shadow_l2}

    safe, risky = [], []
    # 1. 双方都有 → 比较 summary / name
    for k, m in main_by_key.items():
        s = shadow_by_key.get(k)
        if not s: continue
        # name 差异：risky
        if (m.get("name") or "").strip() != (s.get("name") or "").strip():
            risky.append({
                "kind": "name_changed", "node_id": m["id"],
                "numeric_label": m.get("numeric_label",""),
                "old_name": m["name"], "new_name": s["name"],
                "reason": "重扫发现节点名变化",
            })
            continue
        # summary 差异：safe（自动可 apply）
        old_sum = (m.get("summary") or "").strip()
        new_sum = (s.get("summary") or "").strip()
        if old_sum != new_sum and new_sum:
            ratio = difflib.SequenceMatcher(None, old_sum, new_sum).ratio()
            if ratio < 0.95:
                safe.append({
                    "kind": "summary_changed", "node_id": m["id"],
                    "numeric_label": m.get("numeric_label",""),
                    "name": m["name"],
                    "old_summary": old_sum, "new_summary": new_sum,
                    "similarity": round(ratio, 3),
                })
    # 2. 主有 shadow 无 → 节点消失（risky）
    for k, m in main_by_key.items():
        if k not in shadow_by_key:
            risky.append({
                "kind": "node_disappeared", "node_id": m["id"],
                "numeric_label": m.get("numeric_label",""),
                "name": m["name"],
                "reason": "重扫未在 PDF 该 page 找到此节点（可能 AI 误识别或 PDF 变化）",
            })
    # 3. shadow 有主无 → 新节点（risky，需要人工 review）
    for k, s in shadow_by_key.items():
        if k not in main_by_key:
            risky.append({
                "kind": "node_added", "node_id": "(待分配)",
                "numeric_label": s.get("numeric_label",""),
                "name": s["name"],
                "summary": s.get("summary",""),
                "parent_id": s.get("parent_id", ""),
                "pages": s.get("pages", []),
                "reason": "重扫发现 PDF 该 page 有新节点（未在主 KG 中）",
            })
    return {"safe_changes": safe, "risky_changes": risky}


def apply_safe(kg: dict, safe_changes: list[dict]) -> int:
    """自动 apply safe changes：node.summary = new_summary。返回 apply 数。"""
    id2 = {n["id"]: n for n in kg["nodes"]}
    n_applied = 0
    for ch in safe_changes:
        if ch["kind"] == "summary_changed":
            n = id2.get(ch["node_id"])
            if n:
                n["summary"] = ch["new_summary"]
                n_applied += 1
    return n_applied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", required=True)
    ap.add_argument("--pages-per-night", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--target-hour", type=int, default=9)
    ap.add_argument("--target-min", type=int, default=0)
    ap.add_argument("--buffer-min", type=int, default=30)
    ap.add_argument("--auto-apply-safe", action="store_true")
    ap.add_argument("--stable-threshold", type=int, default=3,
                    help="连续 N 次扫描无变化判定稳定（默认 3）")
    ap.add_argument("--max-stable-days", type=int, default=90,
                    help="稳定页超 N 天没扫则强制重扫一次（默认 90）")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    if not kg.get("pdf"):
        print("KG 无 pdf 字段，跳过"); return 0

    # 时间感知：过 cutoff 直接跳
    ok, reason, _ = time_to_safe_cutoff(args.target_hour, args.target_min, args.buffer_min)
    if not ok:
        print(f"已过 cutoff，跳过滚动重扫：{reason}")
        return 0

    prog = load_progress()
    pages = select_next_pages(kg, prog, args.pages_per_night,
                               args.stable_threshold, args.max_stable_days)
    if not pages:
        print("无可扫页面（前沿全部已建笔记或已扫过）")
        return 0
    ranges = pages_to_ranges(pages)
    print(f"=== 学习前沿重扫 {len(pages)} 页 → {len(ranges)} 段 ===")
    for s, e in ranges:
        print(f"  range {s}-{e}")

    shadow_path = run_build_nodes_shadow_ranges(kg, kg_path, ranges,
                                                 args.workers, args.model, args.effort)
    if not shadow_path:
        print("build_nodes 子集扫描全部失败"); return 1
    try:
        shadow_kg = json.loads(shadow_path.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"读 shadow 失败: {ex}"); return 1

    # 比对
    scan_pages_set = set(pages)
    diffs = diff_nodes(kg, shadow_kg, scan_pages_set)
    n_safe = len(diffs["safe_changes"]); n_risky = len(diffs["risky_changes"])
    print(f"\n比对结果：safe {n_safe} / risky {n_risky}")
    for ch in diffs["safe_changes"][:5]:
        print(f"  safe {ch['kind']:<18} {ch['numeric_label']:8} {ch['name'][:25]}")
    for ch in diffs["risky_changes"][:5]:
        print(f"  risky {ch['kind']:<18} {ch.get('numeric_label',''):8} {ch.get('name','')[:25]} - {ch.get('reason','')[:40]}")

    # auto apply safe
    if args.auto_apply_safe and diffs["safe_changes"]:
        n_applied = apply_safe(kg, diffs["safe_changes"])
        if n_applied:
            kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n✓ 自动 apply {n_applied} 个 safe 变更到主 KG")

    # 写到 audit
    audit_file = STATE_DIR / "kg_audit.json"
    audit = {}
    if audit_file.exists():
        try: audit = json.loads(audit_file.read_text(encoding="utf-8"))
        except Exception: audit = {}
    audit.setdefault("rescan_diffs", {})
    audit["rescan_diffs"][f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"] = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "scan_pages": sorted(pages),
        "safe_count": n_safe, "risky_count": n_risky,
        "safe_changes": diffs["safe_changes"],
        "risky_changes": diffs["risky_changes"],
    }
    rd = audit["rescan_diffs"]
    if len(rd) > 5:
        keys = sorted(rd.keys(), key=lambda k: rd[k].get("scanned_at", ""))
        for old_k in keys[:-5]:
            del rd[old_k]
    audit_file.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 写 audit: {audit_file}")

    # 更新每页的扫描历史 + 稳定状态
    # 哪些 page 有变化：safe_changes 或 risky_changes 涉及的 node.pages
    id2 = {n["id"]: n for n in kg["nodes"]}
    changed_pages = set()
    for ch in diffs["safe_changes"] + diffs["risky_changes"]:
        nid = ch.get("node_id", "")
        if nid in id2:
            for p in id2[nid].get("pages") or []:
                changed_pages.add(p)
        # 新增节点也算变化（用 ch.pages）
        for p in ch.get("pages") or []:
            changed_pages.add(p)
    for p in pages:
        had_changes = p in changed_pages
        summary = ""
        if had_changes:
            related = [ch for ch in diffs["safe_changes"] + diffs["risky_changes"]
                       if (ch.get("node_id") in id2 and
                           p in (id2[ch["node_id"]].get("pages") or []))]
            summary = f"{len(related)} 个变化：" + ", ".join(c.get("kind","") for c in related[:3])
        record_scan_outcome(prog, p, had_changes,
                             args.stable_threshold, change_summary=summary)
    prog["last_run"] = datetime.now().isoformat(timespec="seconds")
    # 清掉旧 legacy 字段
    prog.pop("recently_scanned_pages", None)
    prog.pop("last_scanned_end", None)
    prog.pop("page_cooldown", None)
    save_progress(prog)
    stable_count = sum(1 for h in prog.get("page_history", {}).values() if h.get("stable"))
    print(f"✓ 进度更新：{len(prog.get('page_history',{}))} 页有扫描历史，{stable_count} 页已稳定")
    return 0


if __name__ == "__main__":
    sys.exit(main())
