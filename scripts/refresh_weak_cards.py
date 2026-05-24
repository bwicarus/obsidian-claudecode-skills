#!/usr/bin/env python3
"""refresh_weak_cards.py — 卡片 AI 维护，多 task 共用一套管道。

--task weak       薄弱卡(leech/高lapses/低mastery)：L1 AI 重写问法；
                  L2 改写后仍 lapse → 拆/删(破坏性,--apply-escalation)
--task antimodel  已掌握卡(高 stability/长 ivl/reps 多/lapses 低)久未换：
                  AI 换问法防"记住问法而非懂"(语义/答案不变,原地)
--task quality    全量低质卡(答案过长/多知识点/指代不清)：AI 评分→
                  原地优化 or 建议拆(拆破坏性,--apply-escalation)

共用：updateNoteFields 原地改(note id 不变, FSRS 调度全保留)、
record 每卡 _refresh 状态 + 冷却(任一 task 改过都进冷却防互相打架)、
改前备份原 fields 可回滚、AI 输出裸文本 LaTeX 校验 gate、限量、
dry-run 默认。

用法：
  python scripts/refresh_weak_cards.py --task weak --limit 5            # dry-run
  python scripts/refresh_weak_cards.py --task weak --apply
  python scripts/refresh_weak_cards.py --task weak --apply --apply-escalation
  python scripts/refresh_weak_cards.py --task antimodel --apply
  python scripts/refresh_weak_cards.py --task quality --apply [--apply-escalation]
  python scripts/refresh_weak_cards.py --task weak --no-ai             # 只筛选
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import random
import re
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from anki_from_note import (  # noqa: E402
    anki_request, clean_note, load_references, html_text, source_footer,
)
from anki_status import fsrs_memory_map, card_mastery  # noqa: E402
import ai_client  # noqa: E402

RECORDS_DIR = PROJECT_DIR / "anki" / "records"
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))
ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
QUALITY_REPORT = PROJECT_DIR / "state" / "quality_report.json"

_BARE_MATH = [
    re.compile(r"[A-Za-z]_[A-Za-z0-9{]"),
    re.compile(r"[A-Za-z0-9}]\^[A-Za-z0-9{]"),
    re.compile(r"(?<![\\\w])(span|dim|det|ker|rank|trace)\s*\("),
    re.compile(r",\s*\.\.\.\s*,|\.\.\.\s*\+|\+\s*\.\.\."),
    re.compile(r"[∈∉∑∏∫≤≥≠⊆⊇∪∩∅→↦√∞±×·∀∃≈≅⟨⟩₀₁₂₃₄₅₆₇₈₉ₙₖᵢⱼ⊕⟺]"),
]


def has_bare_math(s: str) -> bool:
    if not s:
        return False
    if "\\(" in s or "\\[" in s:
        return False
    return any(p.search(s) for p in _BARE_MATH)


def latex_ok(fields: list[str]) -> bool:
    return not any(has_bare_math(f or "") for f in fields)


def strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def parse_json(raw: str) -> dict | None:
    try:
        return json.loads(strip_fence(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_records() -> list[tuple[Path, dict]]:
    out = []
    for fn in sorted(glob.glob(str(RECORDS_DIR / "*.json"))):
        try:
            rec = json.loads(Path(fn).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # 跳过孤儿 record（source_note 已删，卡片已 suspend）
        if rec.get("status") in ("no_cards", "orphan"):
            continue
        out.append((Path(fn), rec))
    return out


def index_cards(records: list[tuple[Path, dict]]) -> dict[int, dict]:
    idx = {}
    for fn, rec in records:
        for c in rec.get("cards") or []:
            nid = c.get("anki_note_id")
            if nid:
                idx[nid] = {
                    "recfile": fn, "rec": rec, "card": c,
                    "source_note": rec.get("source_note", ""),
                    "source_link": rec.get("source_link", ""),
                    "source_url": rec.get("source_url", ""),
                }
    return idx


def gather_card_stats(idx: dict[int, dict]) -> list[dict]:
    """每 note 一条：idx 信息 + tags/lapses/reps/ivl/mastery/stability。
    三个 collect_* 共用，只取一次 Anki 数据。"""
    note_ids = list(idx.keys())
    if not note_ids:
        return []
    notes = anki_request(ANKI_URL, "notesInfo", {"notes": note_ids}) or []
    all_cids: list[int] = []
    note_cids: dict[int, list[int]] = {}
    note_tags: dict[int, list] = {}
    for nid, n in zip(note_ids, notes):
        if not n:
            continue
        cids = n.get("cards", []) or []
        note_cids[nid] = cids
        note_tags[nid] = n.get("tags", [])
        all_cids += cids
    if not all_cids:
        return []
    cinfo = {c["cardId"]: c for c in
             (anki_request(ANKI_URL, "cardsInfo", {"cards": all_cids}) or [])}
    fmem = fsrs_memory_map(ANKI_URL, all_cids)
    # 复习行为：ease 1=again 2=hard 3=good 4=easy；time=毫秒答题耗时。
    # type 3=cram(临时演练)不计入。中位数抗挂机异常。
    rev_raw = anki_request(ANKI_URL, "getReviewsOfCards",
                           {"cards": all_cids}) or {}
    revmap = {int(k): v for k, v in rev_raw.items()}
    now_ms = int(time.time() * 1000)

    stats = []
    for nid, cids in note_cids.items():
        lapses = max((cinfo.get(ci, {}).get("lapses", 0) for ci in cids), default=0)
        reps = max((cinfo.get(ci, {}).get("reps", 0) for ci in cids), default=0)
        ivl = max((cinfo.get(ci, {}).get("interval", 0) for ci in cids), default=0)
        masts, stabs = [], []
        for ci in cids:
            if ci in cinfo:
                m = card_mastery(cinfo[ci], now_ms, fmem)
                if m is not None:
                    masts.append(m)
                fm = fmem.get(ci) or {}
                if fm.get("s"):
                    stabs.append(fm["s"])
        revs = [r for ci in cids for r in revmap.get(ci, [])
                if r.get("type") != 3]
        rc = len(revs)
        ha = sum(1 for r in revs if r.get("ease") in (1, 2))
        times = sorted(r["time"] for r in revs if r.get("time", 0) > 0)
        # P30 而非中位：发呆只会拉高 time 不会拉低，取低分位更接近
        # "没走神时的真实耗时"，比中位更抗高频发呆。
        p30_t = times[int(0.30 * len(times))] if times else 0
        stats.append({
            **idx[nid], "note_id": nid, "tags": note_tags.get(nid, []),
            "lapses": lapses, "reps": reps, "ivl": ivl,
            "mastery": min(masts) if masts else 1.0,
            "stability": max(stabs) if stabs else 0.0,
            "review_count": rc,
            "hard_again_ratio": (ha / rc) if rc else 0.0,
            "p30_time_ms": p30_t,
        })
    return stats


def collect_weak(stats: list[dict], min_lapses: int) -> list[dict]:
    out = []
    for s in stats:
        why = []
        if "leech" in s["tags"]:
            why.append("leech")
        if s["lapses"] >= min_lapses:
            why.append(f"lapses={s['lapses']}")
        if s["mastery"] < 0.35 and s["reps"] >= 4:
            why.append(f"mastery={s['mastery']:.2f}")
        if why:
            out.append({**s, "why": why})
    out.sort(key=lambda w: (-w["lapses"], w["mastery"]))
    return out


def collect_antimodel(stats: list[dict], min_stab: int, min_reps: int) -> list[dict]:
    out = []
    for s in stats:
        if s["lapses"] > 1 or s["reps"] < min_reps:
            continue
        stable = (s["stability"] >= min_stab) or (s["stability"] == 0 and s["ivl"] >= 21)
        if not stable:
            continue
        out.append({**s, "why": [f"stab={s['stability']:.0f}d/ivl={s['ivl']}d/reps={s['reps']}"]})
    out.sort(key=lambda w: -w["stability"])
    return out


def _pct(lst: list, p: float):
    if not lst:
        return None
    x = sorted(lst)
    return x[min(len(x) - 1, int(p * len(x)))]


def collect_quality(stats: list[dict], max_back_len: int, hard_ratio: float,
                    min_rev: int, relative: bool, sample_n: int) -> list[dict]:
    """启发式 + 行为信号预筛 → AI 终裁。相对阈值按同 type P85；
    另随机采样 sample_n 张绕过启发式作盲区安全网。"""
    def body_of(s):
        c = s["card"]
        return c.get("back") or c.get("text") or ""

    # 按 type 基线：body 长度 P85（相对阈值）、med_time 中位（耗时基线）
    blens: dict = {}
    btimes: dict = {}
    for s in stats:
        t = s["card"].get("type", "basic")
        blens.setdefault(t, []).append(len(body_of(s)))
        if s.get("p30_time_ms", 0) > 0:
            btimes.setdefault(t, []).append(s["p30_time_ms"])
    p85 = {t: _pct(v, 0.85) for t, v in blens.items()}
    tmed = {t: _pct(v, 0.5) for t, v in btimes.items()}

    cand, pool = [], []
    for s in stats:
        c = s["card"]
        t = c.get("type", "basic")
        body, front = body_of(s), (c.get("front") or "")
        if relative and p85.get(t):
            long_thr = max(p85[t], int(max_back_len * 0.6))
        else:
            long_thr = max_back_len
        flags = []
        if len(body) > long_thr:
            flags.append(f"过长{len(body)}>{long_thr}")
        if t != "cloze":
            seg = (body.count("\n") + body.count("；")
                   + len(re.findall(r"(?m)^\s*\d+[\.、]", body)))
            if seg >= 3:
                flags.append("多知识点")
        if 0 < len(front) < 14 and re.match(r"^(它|这|该|其|此)", front):
            flags.append("指代不清")
        if (s.get("review_count", 0) >= min_rev
                and s.get("hard_again_ratio", 0) > hard_ratio):
            flags.append(f"难用{s['hard_again_ratio']*100:.0f}%")
        if (tmed.get(t) and s.get("review_count", 0) >= min_rev
                and s.get("p30_time_ms", 0) > 2 * tmed[t]):
            flags.append(f"耗时{s['p30_time_ms']//1000}s")
        (cand if flags else pool).append((s, flags))

    out = [{**s, "why": f} for s, f in cand]
    if sample_n > 0 and pool:
        for s, _ in random.sample(pool, min(sample_n, len(pool))):
            out.append({**s, "why": ["采样体检"]})
    return out


def in_cooldown(card: dict, days: int) -> bool:
    rf = card.get("_refresh") or {}
    ts = rf.get("last_ts")
    if not ts:
        return False
    try:
        age = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(ts)).days
    except ValueError:
        return False
    return age < days


def stage_weak(card: dict, lapses: int, cooldown: int, esc: int) -> str:
    """weak 专用 L1/L2 分级。"""
    if in_cooldown(card, cooldown):
        return "skip"
    rf = card.get("_refresh") or {}
    if rf.get("count", 0) >= 1:
        if lapses - rf.get("lapses_at", 0) >= esc:
            return "L2"
        return "skip"
    return "L1"


REFS = None


def fmt_ref() -> str:
    global REFS
    if REFS is None:
        REFS = load_references()
    return REFS.get("format", "")


def build_note_ctx(source_note: str, max_chars: int = 3500) -> str:
    try:
        txt = (VAULT_ROOT / source_note).read_text(encoding="utf-8")
    except OSError:
        return "(来源笔记不可读)"
    cleaned, _ = clean_note(txt, max_chars)
    return cleaned


def _fld_spec(ct: str) -> str:
    return ('仅 "text"（含 {{c1::...}} 挖空），"front"/"back" 留空'
            if ct == "cloze" else '"front" 和 "back"，"text" 留空')


def _orig(card: dict) -> str:
    return (f"原卡：\nfront: {card.get('front','')}\n"
            f"back: {card.get('back','')}\ntext: {card.get('text','')}")


def prompt_rewrite(card, lapses, ctx) -> str:
    return f"""这张 Anki 卡用户反复记不住（已失败 {lapses} 次 / leech）。请在**绝对不改变所测知识点和答案正确性**的前提下，重写问法：让问题更聚焦、换一个角度或表述，降低对固定字面的机械记忆。

硬性要求：
- 知识点与答案语义严格不变，只改表达方式，不得改对错。
- 所有数学符号必须用 \\(...\\) 行内或 \\[...\\] 块级包裹，禁止任何裸写。
- 只输出 JSON：{{"diagnosis":"为何难记的一句话","front":"...","back":"...","text":"..."}}
- 本卡类型 {card['type']}，须填 {_fld_spec(card['type'])}。

{_orig(card)}

来源笔记（对照知识点，勿照搬）：
{ctx}

LaTeX / 卡片规范：
{fmt_ref()}"""


def prompt_antimodel(card, ctx) -> str:
    return f"""这张卡用户已经掌握得很好且复习多次——风险是只记住了「这个问法→这个答案」的模式，而非真正理解。请换一个角度/场景重新提问来检验真理解，**所测知识点与答案正确性绝对不变**，只换问法切入点（如改成应用情境、反向追问、给条件反求等）。

硬性要求：
- 知识点与正确答案语义严格不变，难度相当，不得改对错、不得加新知识点。
- 数学一律 \\(...\\) / \\[...\\] 包裹，禁止裸写。
- 只输出 JSON：{{"angle":"换了什么角度的一句话","front":"...","back":"...","text":"..."}}
- 本卡类型 {card['type']}，须填 {_fld_spec(card['type'])}。

{_orig(card)}

来源笔记：
{ctx}

规范：
{fmt_ref()}"""


def prompt_quality(card, flags, ctx) -> str:
    return f"""审查这张 Anki 卡的质量。预筛疑点：{flags}。判断并三选一：
- "ok"：质量没问题，无需改（cards/fields 留空）
- "rewrite"：原地优化（答案太长就精简到要点、问题指代不清就补明确主语、表述歧义就改清楚），**知识点与答案正确性不变**
- "split"：一张塞了多个知识点，应拆成 2-4 张各测一个最小点

只输出 JSON：
- ok:      {{"action":"ok","reason":"..."}}
- rewrite: {{"action":"rewrite","reason":"...","front":"...","back":"...","text":"..."}}（类型 {card['type']}，填 {_fld_spec(card['type'])}）
- split:   {{"action":"split","reason":"...","cards":[{{"type":"basic","front":"...","back":"...","text":""}}]}}

数学一律 \\(...\\) 包裹，禁止裸写。不要为了改而改，没问题就 ok。

{_orig(card)}

来源笔记：
{ctx}

规范：
{fmt_ref()}"""


def build_anki_fields(card: dict, info: dict) -> dict:
    footer = source_footer(info["source_link"], info["source_url"],
                           card.get("reason", ""), card["local_id"])
    if card["type"] == "cloze":
        extra = html_text(card.get("back", "") or "")
        return {"Text": html_text(card.get("text", "") or ""),
                "Extra": (extra + footer) if extra else footer}
    return {"Front": html_text(card.get("front", "") or ""),
            "Back": html_text(card.get("back", "") or "") + footer}


def write_rec(fn: Path, rec: dict) -> None:
    fn.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8")


def write_quality_report(qstat: dict, processed: int, mode: str) -> None:
    try:
        rep = json.loads(QUALITY_REPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rep = {"history": []}
    rep.setdefault("history", []).append({
        "date": dt.date.today().isoformat(), "ts": now_iso(),
        "mode": mode, "processed": processed,
        "ok": qstat["ok"], "rewrite": qstat["rewrite"],
        "split": qstat["split"], "by_flag": qstat["by_flag"],
    })
    rep["history"] = rep["history"][-60:]
    QUALITY_REPORT.parent.mkdir(parents=True, exist_ok=True)
    QUALITY_REPORT.write_text(
        json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✎ 质量报告 → {QUALITY_REPORT.name}")


def apply_rewrite(w: dict, new: dict, stage: str) -> None:
    """共用：备份原 fields → 写回 card → updateNoteFields → record。"""
    card = w["card"]
    rf = card.setdefault("_refresh", {"count": 0, "history": []})
    rf["history"].append({"ts": now_iso(), "stage": stage,
                          "front": card.get("front", ""),
                          "back": card.get("back", ""),
                          "text": card.get("text", "")})
    for k in ("front", "back", "text"):
        card[k] = new.get(k, card.get(k, ""))
    anki_request(ANKI_URL, "updateNoteFields",
                 {"note": {"id": w["note_id"],
                           "fields": build_anki_fields(card, w)}}, timeout=20)
    rf["count"] = rf.get("count", 0) + 1
    rf["last_ts"] = now_iso()
    rf["lapses_at"] = w["lapses"]
    rf["stage"] = stage
    write_rec(w["recfile"], w["rec"])
    print(f"  ✓ {stage} note {w['note_id']}（_refresh#{rf['count']}），record 回写")


def do_split(w: dict, subs: list[dict], origin: str) -> None:
    card = w["card"]
    if not subs or not latex_ok([f for sc in subs for f in
                                 (sc.get("front", ""), sc.get("back", ""),
                                  sc.get("text", ""))]):
        print("  ✗ 拆分子卡为空或含裸文本，放弃")
        return
    base = card["local_id"].rsplit("-", 1)[0]
    for i, sc in enumerate(subs, 1):
        nc = {"local_id": f"{base}-s{i:02d}", "type": sc.get("type", "basic"),
              "deck": card.get("deck", ""), "front": sc.get("front", ""),
              "back": sc.get("back", ""), "text": sc.get("text", ""),
              "reason": f"{origin}拆分自 {card['local_id']}",
              "tags": card.get("tags", []), "status": "synced"}
        note = {"deckName": nc["deck"],
                "modelName": "Obsidian-cloze" if nc["type"] == "cloze"
                else "Obsidian-basic",
                "fields": build_anki_fields(nc, w),
                "tags": ["obsidian", "ai_generated", *nc["tags"]]}
        try:
            nc["anki_note_id"] = anki_request(ANKI_URL, "addNote",
                                              {"note": note}, timeout=20)
            w["rec"]["cards"].append(nc)
            print(f"  ✓ 新子卡 {nc['local_id']} → {nc['anki_note_id']}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 子卡 {nc['local_id']} 失败: {e}")
    anki_request(ANKI_URL, "deleteNotes", {"notes": [w["note_id"]]})
    w["rec"]["cards"] = [c for c in w["rec"]["cards"]
                         if c.get("local_id") != card["local_id"]]
    write_rec(w["recfile"], w["rec"])
    print(f"  ✓ 拆分完成，原 note {w['note_id']} 已删")


def do_delete(w: dict) -> None:
    card = w["card"]
    anki_request(ANKI_URL, "deleteNotes", {"notes": [w["note_id"]]})
    w["rec"]["cards"] = [c for c in w["rec"]["cards"]
                         if c.get("local_id") != card["local_id"]]
    write_rec(w["recfile"], w["rec"])
    print(f"  ✓ 已删除 note {w['note_id']} + record 移除")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["weak", "antimodel", "quality"],
                    default="weak")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--apply-escalation", action="store_true",
                    help="执行破坏性 L2/拆卡")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--cooldown-days", type=int, default=30)
    # weak
    ap.add_argument("--min-lapses", type=int, default=3)
    ap.add_argument("--escalate-lapses", type=int, default=2)
    # antimodel
    ap.add_argument("--min-stability-days", type=int, default=60)
    ap.add_argument("--min-reps", type=int, default=5)
    # quality
    ap.add_argument("--max-back-len", type=int, default=280)
    ap.add_argument("--hard-again-ratio", type=float, default=0.4,
                    help="again+hard 占比超此值算难用")
    ap.add_argument("--min-reviews", type=int, default=4,
                    help="行为信号至少需多少次复习才可信")
    ap.add_argument("--abs-threshold", action="store_true",
                    help="用绝对 max_back_len（默认按同 type P85 相对）")
    ap.add_argument("--sample-per-run", type=int, default=3,
                    help="每次额外随机抽几张绕过启发式（盲区安全网）")
    args = ap.parse_args()

    stats = gather_card_stats(index_cards(load_records()))
    if args.task == "weak":
        items = collect_weak(stats, args.min_lapses)
    elif args.task == "antimodel":
        items = collect_antimodel(stats, args.min_stability_days, args.min_reps)
    else:
        items = collect_quality(stats, args.max_back_len,
                                args.hard_again_ratio, args.min_reviews,
                                not args.abs_threshold, args.sample_per_run)
    print(f"[{args.task}] 候选 {len(items)} 张")
    if not items:
        return 0

    picked = 0
    qstat = {"ok": 0, "rewrite": 0, "split": 0, "by_flag": {}}
    for w in items:
        if picked >= args.limit:
            break
        card = w["card"]
        why = "/".join(w["why"])

        if args.task == "weak":
            st = stage_weak(card, w["lapses"], args.cooldown_days,
                            args.escalate_lapses)
        else:
            st = "skip" if in_cooldown(card, args.cooldown_days) else "DO"
        if st == "skip":
            continue
        picked += 1
        print(f"\n━━ [{w['source_note']}] {card['local_id']} ({why}) "
              f"{args.task}:{st} ━━")
        if args.no_ai:
            continue
        ctx = build_note_ctx(w["source_note"])

        # ── weak L1 / antimodel / quality-rewrite：原地改写 ──
        if args.task == "weak" and st == "L1":
            j = parse_json(ai_client.ask(prompt_rewrite(card, w["lapses"], ctx)))
            if not j:
                print("  ✗ AI 非 JSON"); continue
            new = {k: (j.get(k) or "").strip() for k in ("front", "back", "text")}
            if not latex_ok(list(new.values())):
                print("  ✗ 含裸文本数学，拒绝"); continue
            print(f"  诊断: {j.get('diagnosis','')}")
            for k in ("front", "back", "text"):
                if card.get(k) or new[k]:
                    print(f"  {k}: {card.get(k,'')!r}\n      → {new[k]!r}")
            if args.apply:
                apply_rewrite(w, new, "L1")

        elif args.task == "antimodel":
            j = parse_json(ai_client.ask(prompt_antimodel(card, ctx)))
            if not j:
                print("  ✗ AI 非 JSON"); continue
            new = {k: (j.get(k) or "").strip() for k in ("front", "back", "text")}
            if not latex_ok(list(new.values())):
                print("  ✗ 含裸文本数学，拒绝"); continue
            print(f"  换角度: {j.get('angle','')}")
            for k in ("front", "back", "text"):
                if card.get(k) or new[k]:
                    print(f"  {k}: {card.get(k,'')!r}\n      → {new[k]!r}")
            if args.apply:
                apply_rewrite(w, new, "antimodel")

        elif args.task == "quality":
            j = parse_json(ai_client.ask(prompt_quality(card, w["why"], ctx)))
            if not j:
                print("  ✗ AI 非 JSON"); continue
            act = j.get("action")
            for fl in w["why"]:
                m = re.match(r"[^\d%>]+", fl)
                cat = m.group().strip() if m else fl
                qstat["by_flag"][cat] = qstat["by_flag"].get(cat, 0) + 1
            if act in ("ok", "rewrite", "split"):
                qstat[act] += 1
            print(f"  质量判定: {act} — {j.get('reason','')}")
            if act == "ok":
                continue
            if act == "rewrite":
                new = {k: (j.get(k) or "").strip()
                       for k in ("front", "back", "text")}
                if not latex_ok(list(new.values())):
                    print("  ✗ 含裸文本数学，拒绝"); continue
                for k in ("front", "back", "text"):
                    if card.get(k) or new[k]:
                        print(f"  {k}: {card.get(k,'')!r}\n      → {new[k]!r}")
                if args.apply:
                    apply_rewrite(w, new, "quality")
            elif act == "split":
                for sc in j.get("cards", []):
                    print(f"    子卡: {sc.get('front','')!r} / {sc.get('back','')!r}")
                if not args.apply_escalation:
                    print("  （拆卡破坏性，未加 --apply-escalation，仅建议）")
                elif args.apply:
                    do_split(w, j.get("cards", []), "质量")

        # ── weak L2：拆/删（破坏性）──
        elif args.task == "weak" and st == "L2":
            delta = w["lapses"] - (card.get("_refresh", {}).get("lapses_at", 0))
            j = parse_json(ai_client.ask(prompt_escalate(card, w["lapses"],
                                                         delta, ctx)))
            if not j:
                print("  ✗ AI 非 JSON"); continue
            act = j.get("action")
            print(f"  升级判定: {act} — {j.get('reason','')}")
            if act == "split":
                for sc in j.get("cards", []):
                    print(f"    子卡: {sc.get('front','')!r} / {sc.get('back','')!r}")
            if not args.apply_escalation:
                print("  （L2 破坏性，未加 --apply-escalation，仅建议）")
                continue
            if args.apply and act == "delete":
                do_delete(w)
            elif args.apply and act == "split":
                do_split(w, j.get("cards", []), "L2")

    mode = ("APPLY" + ("+ESC" if args.apply_escalation else "")
            if args.apply else "DRY-RUN")
    print(f"\n[{args.task}] 处理 {picked} 张（{mode}）"
          + ("" if args.apply else "  —— 加 --apply 执行"))
    if args.task == "quality":
        write_quality_report(qstat, picked, mode)
    return 0


def prompt_escalate(card, lapses, delta, ctx) -> str:
    return f"""这张卡已被改写 {card.get('_refresh',{}).get('count',0)} 次，用户仍持续记不住（改写后又失败 {delta} 次）。二选一：
- "split"：拆成 2-4 张更小的卡，每张只测一个最小知识点
- "delete"：不值得保留（过碎/过宽/不适合卡片化）

只输出 JSON：{{"action":"split"|"delete","reason":"理由","cards":[{{"type":"basic","front":"...","back":"...","text":""}}]}}
delete 时 cards 为 []。数学一律 \\(...\\) 包裹。

{_orig(card)}

来源笔记：
{ctx}

规范：
{fmt_ref()}"""


if __name__ == "__main__":
    sys.exit(main())
