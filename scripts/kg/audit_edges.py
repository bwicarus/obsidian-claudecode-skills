#!/usr/bin/env python3
"""audit_edges.py — v3-D 边审计(代替人工逐条确认;规格 references/emergent-edge-algorithm.md §4)。

抽查 emergent-graph 里的边(优先:status=auto 从未审过 / tier2 散文边 / rel_detail=unconfirmed),
重读证据句 ±上下文,AI 批量判 keep|demote|remove:
  keep   → status=audited
  demote → kind=related + status=audited(不再参与解锁门控)
  remove → 移入 removed_edges 墓碑(不物理删,可查可恢复)
用户改过的边(emergent-confirmations.json edges 里有记录)审计**不动**。
每轮上限 MAX_PER_RUN 条、一次批量调用;全程写 state/attention/edge-audit-log.jsonl。
CLI:默认 dry-run;--run 应用。
"""
import json
import re
import sys
import time
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import attention_profile as AP  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import promote_concepts as PC  # noqa: E402

STATE = config.PROJECT_DIR / "state"
EMERGENT = STATE / "attention" / "emergent-graph.json"
CONF = STATE / "attention" / "emergent-confirmations.json"
LOG = STATE / "attention" / "edge-audit-log.jsonl"
SEARCH_DB = STATE / "pdf-search.db"
MAX_PER_RUN = 20


def _ek(e):
    return "%s|%s|%s" % (e["from"], e["to"], e.get("kind", "prereq"))


def _context_for(e):
    """取证据句的容器上下文(±~200字):note → 笔记可扫描文本;book → 该页 body。"""
    src = e.get("quote_src", "")
    quote = e.get("quote", "")
    text = ""
    if src.startswith("note:"):
        f = PC._find_note_file(src[5:])
        if f:
            try:
                text = "\n".join(seg for seg, _t in PC._note_scannable_text(f.read_text("utf-8")))
            except Exception:
                pass
    elif src.startswith("book:") and SEARCH_DB.exists():
        m = re.match(r"book:(.+)#p(\d+)", src)
        if m:
            con = sqlite3.connect("file:%s?mode=ro" % SEARCH_DB, uri=True)
            r = con.execute("SELECT body FROM pages_data WHERE file=? AND page=?",
                            (m.group(1), int(m.group(2)))).fetchone()
            con.close()
            text = (r[0] if r else "") or ""
    if not text or not quote:
        return quote
    norm = lambda x: re.sub(r"\s+", "", x)
    i = norm(text).find(norm(quote)[:40])
    if i < 0:
        return quote
    # 粗略按压缩后位置换算原文位置(足够取邻域)
    ratio = len(text) / max(1, len(norm(text)))
    j = int(i * ratio)
    return text[max(0, j - 200): j + len(quote) + 200]


def pick_audit_set(g):
    """审计集:未审过的 auto 边 + 散文边 + unconfirmed,跳过用户改过的。"""
    try:
        conf_edges = (json.loads(CONF.read_text("utf-8")) or {}).get("edges", {})
    except Exception:
        conf_edges = {}
    out = []
    for e in g.get("edges", []):
        if _ek(e) in conf_edges:
            continue                      # 用户签过字的不动
        st = e.get("status", "auto")
        if st == "audited" and e.get("src_tier") != "prose" and e.get("rel_detail") != "unconfirmed":
            continue
        if st == "auto" or e.get("src_tier") == "prose" or e.get("rel_detail") == "unconfirmed":
            out.append(e)
    return out[:MAX_PER_RUN]


def audit(run=False, model="sonnet", effort="low"):
    g = json.loads(EMERGENT.read_text("utf-8"))
    todo = pick_audit_set(g)
    if not todo:
        print("无待审计边")
        return {"audited": 0}
    lines = []
    for i, e in enumerate(todo):
        ctx = _context_for(e)
        lines.append("%d. %s %s %s(%s)\n   证据:「%s」\n   上下文:「%s」"
                     % (i, e["from"], "→" if e["kind"] == "prereq" else "—", e["to"],
                        e.get("rel_detail", ""), e.get("quote", "")[:150], ctx[:300]))
    prompt = ("审计下列自动生成的概念关系边。对每条,依据证据句+上下文判断:\n"
              "- keep: 关系与类型(→=前置,—=相关)都站得住\n"
              "- demote: 存在关系但**不是前置**(例子/推广/顺带提及)→ 应降为相关\n"
              "- remove: 证据撑不住这条关系\n\n" + "\n\n".join(lines)
              + '\n\n只输出严格 JSON 数组:[{"i":0,"verdict":"keep|demote|remove","reason":"≤15字"},...]')
    sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
    from ai_backends import make_backend
    be = make_backend("claude_cli", {"command": "/usr/bin/claude",
                                     "model": model, "effort": effort, "timeout": 240})
    try:
        raw = be.chat([{"role": "user", "content": prompt}]).strip()
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```\s*$", "", raw)
        a, b = raw.find("["), raw.rfind("]")
        arr = json.loads(raw[a:b + 1]) if a != -1 and b > a else []
    except Exception as e2:
        print("⚠ AI 审计失败:%s(边保持原状,下轮再试)" % str(e2)[:60], file=sys.stderr)
        return {"audited": 0, "error": True}
    got = {int(x["i"]): x for x in arr if isinstance(x, dict) and "i" in x}
    stats = {"keep": 0, "demote": 0, "remove": 0, "noans": 0}
    logs = []
    removed = g.setdefault("removed_edges", [])
    for i, e in enumerate(todo):
        x = got.get(i) or {}
        v = x.get("verdict")
        if v not in ("keep", "demote", "remove"):
            stats["noans"] += 1
            continue
        stats[v] += 1
        logs.append({"ts": int(time.time()), "edge": _ek(e), "verdict": v,
                     "reason": (x.get("reason") or "")[:40], "was": {"kind": e["kind"], "status": e.get("status")}})
        if not run:
            continue
        if v == "keep":
            e["status"] = "audited"
        elif v == "demote":
            e["kind"] = "related"
            e["status"] = "audited"
            e["rel_detail"] = "audit_demoted"
        else:
            e["status"] = "removed"
            removed.append(dict(e, removed_ts=int(time.time())))
    if run:
        g["edges"] = [e for e in g["edges"] if e.get("status") != "removed"]
        EMERGENT.write_text(json.dumps(g, ensure_ascii=False, indent=1), "utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            for L in logs:
                f.write(json.dumps(L, ensure_ascii=False) + "\n")
    for L in logs:
        print("  %s %s — %s" % ({"keep": "✓", "demote": "↓", "remove": "✗"}[L["verdict"]], L["edge"], L["reason"]))
    print("审计 %d 条:keep %d / demote %d / remove %d%s"
          % (len(todo), stats["keep"], stats["demote"], stats["remove"], " [已应用]" if run else " [dry-run]"))
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    audit(run=a.run, model=a.model, effort=a.effort)
