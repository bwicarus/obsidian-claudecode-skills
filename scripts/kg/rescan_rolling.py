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


def select_next_pages(kg: dict, prog: dict, n: int) -> tuple[int, int]:
    """选下一批 page 范围 [start, end]。轮转覆盖全 PDF。"""
    pdf_path = Path(kg.get("pdf", ""))
    if not pdf_path.exists():
        return (0, 0)
    import fitz
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    doc.close()
    last_end = prog.get("last_scanned_end") or 0
    # 跳过封面/版权页（前 10 页）
    skip_front = prog.get("skip_front", 10)
    start = max(skip_front + 1, last_end + 1)
    if start > total:
        # 一轮完成，重置
        start = skip_front + 1
    end = min(start + n - 1, total)
    return (start, end)


def run_build_nodes_shadow(kg: dict, kg_path: Path, start: int, end: int,
                           workers: int, model: str, effort: str) -> Path | None:
    """调 build_nodes.py 子集模式（--pages X-Y）写入临时 shadow KG，返回路径。"""
    pdf = kg.get("pdf")
    if not pdf:
        return None
    book = kg.get("book", kg_path.stem)
    book_full = kg.get("book_full", book)
    out = Path(tempfile.mkdtemp(prefix="rescan-shadow-")) / f"{book}.shadow.json"
    cmd = [
        sys.executable, str(PROJECT_DIR / "scripts" / "kg" / "build_nodes.py"),
        "--pdf", pdf, "--book", book, "--book-full", book_full,
        "--out", str(out),
        "--pages", f"{start}-{end}",
        "--workers", str(workers), "--model", model, "--effort", effort,
    ]
    print(f"  调 build_nodes：pages {start}-{end}")
    r = subprocess.run(cmd, capture_output=False)
    if r.returncode != 0 or not out.exists():
        return None
    return out


def diff_nodes(main_kg: dict, shadow_kg: dict, page_range: tuple[int, int]) -> dict:
    """对比主 KG 跟 shadow 上 page_range 内的 L2 节点。
    返回 {safe_changes: [...], risky_changes: [...]}"""
    start, end = page_range
    in_range = lambda n: any(start <= p <= end for p in (n.get("pages") or []))
    # 主 KG 中本次扫描页范围内的节点
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
    start, end = select_next_pages(kg, prog, args.pages_per_night)
    if end == 0 or end < start:
        print("无可扫页面"); return 0
    print(f"=== 滚动重扫 pages {start}-{end} ===")

    shadow_path = run_build_nodes_shadow(kg, kg_path, start, end,
                                          args.workers, args.model, args.effort)
    if not shadow_path:
        print("build_nodes 子集扫描失败"); return 1
    try:
        shadow_kg = json.loads(shadow_path.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"读 shadow 失败: {ex}"); return 1

    # 比对
    diffs = diff_nodes(kg, shadow_kg, (start, end))
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
    audit["rescan_diffs"][f"pages_{start}_{end}"] = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "page_range": [start, end],
        "safe_count": n_safe, "risky_count": n_risky,
        "safe_changes": diffs["safe_changes"],
        "risky_changes": diffs["risky_changes"],
    }
    # 只保留最近 5 批 rescan 数据
    rd = audit["rescan_diffs"]
    if len(rd) > 5:
        keys = sorted(rd.keys(), key=lambda k: rd[k].get("scanned_at", ""))
        for old_k in keys[:-5]:
            del rd[old_k]
    audit_file.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 写 audit: {audit_file}")

    # 更新进度
    prog["last_scanned_end"] = end
    prog["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_progress(prog)
    print(f"✓ 进度更新：下次从 page {end + 1} 开始")
    return 0


if __name__ == "__main__":
    sys.exit(main())
