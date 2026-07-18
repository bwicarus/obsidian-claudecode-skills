#!/usr/bin/env python3
"""node_evidence.py — 把注意力/Anki/诊断数据按【页→节点】映射成每个 KG 节点的**证据候选**(join)
+ 跨书**覆盖体检**(骨架 vs 活动错配可见化)+ 数据健康。

守铁律:读/查词/高亮/QA = engagement 证据(只支撑"接触过/进度",**不当掌握**);
        只有 诊断卷判分 / Anki 卡 = mastery 证据。据此给每个节点一个**诚实的建议动作**。
CLI: --book LADR [--json] | --coverage | --health
"""
import json
import sqlite3
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

DB = config.PROJECT_DIR / "state" / "attention" / "events.db"
CHECK_DIR = config.PROJECT_DIR / "state" / "reader-check-reports"
KG_DIR = config.PROJECT_DIR / "knowledge_graph"
RECORDS = config.PROJECT_DIR / "anki" / "records"


def _kg_rel(kg):
    pdf = kg.get("pdf") or ""
    return pdf.split("/obsidian/", 1)[1] if "/obsidian/" in pdf else ""


def _real_kgs():
    """真 KG 文件名(排除 .bak / pre / scan 快照)。"""
    out = []
    for p in KG_DIR.glob("*.json"):
        s = p.stem
        if ".bak." in p.name or "pre" in s or "scan" in s:
            continue
        out.append(s)
    return sorted(out)


def _diag_by_node(book):
    """本书每节点的诊断累计 {bare_nid: (correct, total)}(读检查报告 node_results)。"""
    out = {}
    for f in CHECK_DIR.glob("*.json"):
        try:
            reps = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        for r in reps if isinstance(reps, list) else []:
            if not isinstance(r, dict) or r.get("sandbox"):
                continue
            for nid, e in (r.get("node_results") or {}).items():
                if not isinstance(nid, str) or not nid.startswith("kg:"):
                    continue
                b, _, bare = nid[3:].partition("#")
                if b != book or not bare:
                    continue
                c, t = out.get(bare, (0, 0))
                out[bare] = (c + int(e.get("correct") or 0), t + int(e.get("total") or 0))
    return {k: v for k, v in out.items() if v[1]}


def _suggest_action(n, engaged, diag, card_m):
    """诚实建议(守铁律):诊断有=可提议掌握;接触过但没测=该出速测;可学没碰=前沿。"""
    prog = n.get("progress")
    avail = n.get("availability")
    if diag:
        return "propose_mastered" if diag[0] / diag[1] >= 0.8 else "propose_relearn"
    if engaged and card_m is None:
        return "suggest_diagnostic"     # engagement≠mastery:接触过就出 3 题速测确认
    if prog == "unseen" and avail == "open":
        return "frontier_unstudied"     # 可学但没碰=前沿
    return "none"


def evidence_for_book(book):
    kgp = KG_DIR / (book + ".json")
    kg = json.loads(kgp.read_text("utf-8"))
    rel = _kg_rel(kg)
    l2 = [n for n in kg["nodes"] if n.get("level") == 2]
    page_ch = {}   # page -> {channel: count}
    if DB.exists() and rel:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        for pg, ch, cnt in con.execute(
                "SELECT page, channel, COUNT(*) FROM events WHERE file=? AND page>0 GROUP BY page, channel",
                (rel,)):
            page_ch.setdefault(pg, {})[ch] = cnt
        con.close()
    diag = _diag_by_node(book)
    nodes = {}
    for n in l2:
        pgs = n.get("pages") or []
        ev = {"reading": 0, "lookup": 0, "highlight": 0, "qa": 0, "check": 0}
        if pgs:
            lo, hi = pgs[0], pgs[-1]
            for pg, chs in page_ch.items():
                if lo <= pg <= hi:
                    ev["reading"] += chs.get("read", 0)
                    ev["lookup"] += chs.get("lookup", 0)
                    ev["highlight"] += chs.get("highlight", 0)
                    ev["qa"] += chs.get("qa", 0)
                    ev["check"] += chs.get("check", 0)
        engaged = (ev["reading"] + ev["lookup"] + ev["highlight"] + ev["qa"]) > 0
        d = diag.get(n["id"])
        card_m = n.get("mastery") if n.get("card_refs") else None
        nodes[n["id"]] = {
            "name": n.get("name"), "pages": pgs, "engagement": ev,
            "diagnostic": ({"correct": d[0], "total": d[1]} if d else None),
            "card_mastery": card_m,
            "mastery_evidence": bool(d) or (card_m is not None),
            "progress": n.get("progress"), "availability": n.get("availability"),
            "action": _suggest_action(n, engaged, d, card_m),
        }
    return {"book": book, "rel": rel, "n_l2": len(l2), "nodes": nodes}


def coverage_matrix():
    """跨书覆盖矩阵:哪些书有 KG、哪些书有活动,把"骨架 vs 活动"错配暴露出来。"""
    kgs = _real_kgs()
    kg_rel = {}
    kg_nodes = {}
    for b in kgs:
        try:
            kg = json.loads((KG_DIR / (b + ".json")).read_text("utf-8"))
            kg_rel[_kg_rel(kg)] = b
            kg_nodes[b] = sum(1 for n in kg["nodes"] if n.get("level") == 2)
        except Exception:
            pass
    rows = []
    if DB.exists():
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        agg = {}
        for f, ch, n in con.execute(
                "SELECT file, channel, COUNT(*) FROM events WHERE page>0 GROUP BY file, channel"):
            agg.setdefault(f, {})[ch] = n
        con.close()
        for f, chs in agg.items():
            if "/.sandbox/" in f:
                continue
            rows.append({"file": f, "kg": kg_rel.get(f, ""), "events": sum(chs.values()),
                         "read": chs.get("read", 0), "lookup": chs.get("lookup", 0),
                         "highlight": chs.get("highlight", 0), "check": chs.get("check", 0)})
        rows.sort(key=lambda x: -x["events"])
    active_with_kg = [r for r in rows if r["kg"]]
    active_no_kg = [r for r in rows if not r["kg"] and r["events"] >= 20]
    kg_no_activity = [b for b in kgs if b not in {r["kg"] for r in active_with_kg}]
    return {"kgs": [{"book": b, "l2": kg_nodes.get(b, 0)} for b in kgs],
            "active_with_kg": active_with_kg, "active_no_kg": active_no_kg,
            "kg_no_activity": kg_no_activity, "all_active": rows}


def health():
    """数据健康:events 新鲜度 / Anki records 是否加载 / 每本 KG 的节点数据覆盖率。"""
    h = {"ts": int(time.time())}
    if DB.exists():
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        try:
            li = con.execute("SELECT v FROM meta WHERE k='last_import'").fetchone()
            h["last_import_age_min"] = round((time.time() - int(li[0])) / 60, 1) if li else None
        except Exception:
            h["last_import_age_min"] = None
        h["events_total"] = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        con.close()
    else:
        h["events_db"] = "MISSING"
    h["anki_records"] = len(list(RECORDS.glob("*.json"))) if RECORDS.exists() else 0
    warns = []
    if h.get("anki_records", 0) == 0:
        warns.append("anki/records 为空 → 掌握度数据源缺失(config 可能退回 Windows 路径?检查 CLAUDE_PROJECT env)")
    if h.get("last_import_age_min") is not None and h["last_import_age_min"] > 60:
        warns.append("events.db 超过 60min 没导入 → 注意力数据可能陈旧(quick-sync timer 挂了?)")
    cov = []
    for b in _real_kgs():
        try:
            kg = json.loads((KG_DIR / (b + ".json")).read_text("utf-8"))
        except Exception:
            continue
        l2 = [n for n in kg["nodes"] if n.get("level") == 2]
        if not l2:
            continue
        has_m = sum(1 for n in l2 if n.get("mastery") is not None)
        has_card = sum(1 for n in l2 if n.get("card_refs"))
        unseen = sum(1 for n in l2 if n.get("progress") == "unseen")
        cov.append({"book": b, "l2": len(l2),
                    "mastery_pct": round(100 * has_m / len(l2)),
                    "card_pct": round(100 * has_card / len(l2)),
                    "unseen_pct": round(100 * unseen / len(l2))})
    h["coverage"] = cov
    h["warnings"] = warns
    return h


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--book")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.coverage:
        print(json.dumps(coverage_matrix(), ensure_ascii=False, indent=1))
    elif a.health:
        print(json.dumps(health(), ensure_ascii=False, indent=1))
    elif a.book:
        r = evidence_for_book(a.book)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            from collections import Counter
            acts = Counter(v["action"] for v in r["nodes"].values())
            print("《%s》%d 个 L2 节点,证据聚合:" % (a.book, r["n_l2"]))
            print("  建议动作分布:", dict(acts))
            print("  有 mastery 证据(诊断/卡):", sum(1 for v in r["nodes"].values() if v["mastery_evidence"]))
            print("  有 engagement(读/查/高亮/QA):", sum(1 for v in r["nodes"].values()
                  if sum(v["engagement"].values()) > 0))
    else:
        ap.print_help()
