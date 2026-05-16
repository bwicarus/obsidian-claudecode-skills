#!/usr/bin/env python3
"""refresh_weak_cards.py — 薄弱卡分级处理（leech / 高 lapses / 低 mastery）。

分级（状态存 record 每卡 _refresh）：
  L1  未处理或改写后暂稳：AI 重写问法（语义/答案不变），原地
      updateNoteFields —— note id 不变，FSRS 调度数据(S/D/ivl/due/
      reps/lapses)完全不动，不破坏 FSRS、复习进度全保留。
  L2  已改写 ≥1 次仍持续 lapse：AI 判定「拆成多张」或「删除」。
      破坏性(删旧建新会丢调度)，默认只出建议，--apply-escalation 才执行。

防护：改写前备份原 fields 进 _refresh.history(可回滚)；改写后跑
裸文本 LaTeX 校验，不合格拒绝写入；每卡冷却期；每次限量。

用法：
  dry-run（默认，调 AI 出预览但不写）：
    python scripts/refresh_weak_cards.py --limit 5
  执行 L1 改写：
    python scripts/refresh_weak_cards.py --limit 5 --apply
  额外执行 L2 拆/删：
    python scripts/refresh_weak_cards.py --apply --apply-escalation
  只看筛选不调 AI：
    python scripts/refresh_weak_cards.py --no-ai
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
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

# 裸文本数学特征（命中且无 \( \[ → AI 又生成了裸文本，拒绝写入）
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


def strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_records() -> list[tuple[Path, dict]]:
    out = []
    for fn in sorted(glob.glob(str(RECORDS_DIR / "*.json"))):
        try:
            out.append((Path(fn), json.loads(Path(fn).read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            pass
    return out


def index_cards(records: list[tuple[Path, dict]]) -> dict[int, dict]:
    """anki_note_id -> {recfile, rec, card, source_note, source_link, source_url}"""
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


def collect_weak(idx: dict[int, dict], min_lapses: int) -> list[dict]:
    note_ids = list(idx.keys())
    if not note_ids:
        return []
    notes = anki_request(ANKI_URL, "notesInfo", {"notes": note_ids}) or []
    # 该版本 notesInfo 不返回 noteId，按请求顺序对应
    all_cids: list[int] = []
    note_cids: dict[int, list[int]] = {}
    note_meta: dict[int, dict] = {}
    for nid, n in zip(note_ids, notes):
        if not n:
            continue
        cids = n.get("cards", []) or []
        note_cids[nid] = cids
        note_meta[nid] = {"tags": n.get("tags", []), "fields": n.get("fields", {})}
        all_cids += cids
    if not all_cids:
        return []
    cinfo = {c["cardId"]: c for c in (anki_request(ANKI_URL, "cardsInfo", {"cards": all_cids}) or [])}
    fmem = fsrs_memory_map(ANKI_URL, all_cids)
    now_ms = int(time.time() * 1000)

    weak = []
    for nid, cids in note_cids.items():
        meta = note_meta[nid]
        tags = meta["tags"]
        lapses = max((cinfo.get(ci, {}).get("lapses", 0) for ci in cids), default=0)
        reps = max((cinfo.get(ci, {}).get("reps", 0) for ci in cids), default=0)
        masts = []
        for ci in cids:
            if ci in cinfo:
                m = card_mastery(cinfo[ci], now_ms, fmem)
                if m is not None:
                    masts.append(m)
        mastery = min(masts) if masts else 1.0
        is_leech = "leech" in tags
        weak_by = []
        if is_leech:
            weak_by.append("leech")
        if lapses >= min_lapses:
            weak_by.append(f"lapses={lapses}")
        if mastery < 0.35 and reps >= 4:
            weak_by.append(f"mastery={mastery:.2f}")
        if not weak_by:
            continue
        info = idx[nid]
        weak.append({
            **info, "note_id": nid, "lapses": lapses, "reps": reps,
            "mastery": mastery, "weak_by": weak_by,
        })
    # 越弱越优先
    weak.sort(key=lambda w: (-w["lapses"], w["mastery"]))
    return weak


def stage_of(card: dict, lapses: int, cooldown_days: int, escalate_lapses: int) -> str:
    """返回 'skip' | 'L1' | 'L2'。"""
    rf = card.get("_refresh") or {}
    last_ts = rf.get("last_ts")
    if last_ts:
        try:
            age = (dt.datetime.now().astimezone()
                   - dt.datetime.fromisoformat(last_ts)).days
        except ValueError:
            age = 999
        if age < cooldown_days:
            return "skip"  # 冷却中，给它时间按新问法重新稳定
    count = rf.get("count", 0)
    lapses_at = rf.get("lapses_at", 0)
    if count >= 1:
        # 改写后又新增 lapse 达阈值 → 升级 L2；否则还在恢复中，跳过
        if lapses - lapses_at >= escalate_lapses:
            return "L2"
        return "skip"
    return "L1"


def build_note_ctx(source_note: str, max_chars: int = 3500) -> str:
    p = (VAULT_ROOT / source_note)
    try:
        txt = p.read_text(encoding="utf-8")
    except OSError:
        return "(来源笔记不可读)"
    cleaned, _ = clean_note(txt, max_chars)
    return cleaned


REFS = None


def fmt_ref() -> str:
    global REFS
    if REFS is None:
        REFS = load_references()
    return REFS.get("format", "")


def prompt_rewrite(card: dict, lapses: int, note_ctx: str) -> str:
    ct = card["type"]
    if ct == "cloze":
        fld = '仅 "text"（含 {{c1::...}} 挖空），"front"/"back" 留空'
    else:
        fld = '"front" 和 "back"，"text" 留空'
    return f"""这张 Anki 卡用户反复记不住（已失败 {lapses} 次 / leech）。请在**绝对不改变所测知识点和答案正确性**的前提下，重写问法：让问题更聚焦、换一个角度或表述，降低对固定字面的机械记忆。

硬性要求：
- 知识点与答案语义严格不变，只改表达方式，不得改对错。
- 所有数学符号必须用 \\(...\\) 行内或 \\[...\\] 块级包裹，禁止任何裸写（下标 b_1、上标 e^x、函数名 span、省略号、集合记号都要包）。
- 只输出 JSON，无其它文字：{{"diagnosis":"为何难记的一句话判断","front":"...","back":"...","text":"..."}}
- 本卡类型 {ct}，须填 {fld}。

原卡：
front: {card.get('front','')}
back: {card.get('back','')}
text: {card.get('text','')}

来源笔记（供你对照知识点，勿照搬）：
{note_ctx}

LaTeX / 卡片规范：
{fmt_ref()}"""


def prompt_escalate(card: dict, lapses: int, delta: int, note_ctx: str) -> str:
    return f"""这张卡已被改写 {card['_refresh']['count']} 次，用户仍持续记不住（改写后又失败 {delta} 次）。说明单卡承载过重或知识点本身需要处理。二选一给出方案：
- "split"：拆成 2-4 张更小的卡，每张只测一个最小知识点
- "delete"：这张卡不值得保留（过碎/过宽/不适合卡片化）

只输出 JSON：{{"action":"split"|"delete","reason":"理由","cards":[{{"type":"basic","front":"...","back":"...","text":""}}]}}
delete 时 cards 为 []。数学一律 \\(...\\) 包裹，禁止裸写。

原卡：
front: {card.get('front','')}
back: {card.get('back','')}
text: {card.get('text','')}

来源笔记：
{note_ctx}

规范：
{fmt_ref()}"""


def parse_json(raw: str) -> dict | None:
    try:
        return json.loads(strip_fence(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def latex_ok(fields: list[str]) -> bool:
    return not any(has_bare_math(f or "") for f in fields)


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行 L1 改写")
    ap.add_argument("--apply-escalation", action="store_true",
                    help="额外执行 L2 拆/删（破坏性）")
    ap.add_argument("--no-ai", action="store_true", help="只筛选不调 AI")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-lapses", type=int, default=3)
    ap.add_argument("--cooldown-days", type=int, default=30)
    ap.add_argument("--escalate-lapses", type=int, default=2,
                    help="改写后再 lapse 多少次升级 L2")
    args = ap.parse_args()

    records = load_records()
    idx = index_cards(records)
    weak = collect_weak(idx, args.min_lapses)
    print(f"薄弱卡 {len(weak)} 张（min_lapses={args.min_lapses}）")
    if not weak:
        return 0

    picked = 0
    for w in weak:
        if picked >= args.limit:
            break
        card = w["card"]
        st = stage_of(card, w["lapses"], args.cooldown_days, args.escalate_lapses)
        tag = "/".join(w["weak_by"])
        head = f"[{w['source_note']}] {card['local_id']} ({tag}) → {st}"
        if st == "skip":
            continue
        picked += 1
        print(f"\n━━ {head} ━━")
        if args.no_ai:
            continue
        note_ctx = build_note_ctx(w["source_note"])

        if st == "L1":
            resp = ai_client.ask(prompt_rewrite(card, w["lapses"], note_ctx))
            j = parse_json(resp)
            if not j:
                print("  ✗ AI 返回非 JSON，跳过")
                continue
            new = {k: (j.get(k) or "").strip() for k in ("front", "back", "text")}
            if not latex_ok([new["front"], new["back"], new["text"]]):
                print("  ✗ 新内容含裸文本数学，拒绝写入")
                continue
            print(f"  诊断: {j.get('diagnosis','')}")
            for k in ("front", "back", "text"):
                if card.get(k) or new[k]:
                    print(f"  {k}: {card.get(k,'')!r}\n      → {new[k]!r}")
            if args.apply:
                rf = card.setdefault("_refresh", {"count": 0, "history": []})
                rf["history"].append({"ts": now_iso(), "stage": "L1",
                                       "front": card.get("front", ""),
                                       "back": card.get("back", ""),
                                       "text": card.get("text", "")})
                for k in ("front", "back", "text"):
                    card[k] = new[k]
                anki_request(ANKI_URL, "updateNoteFields",
                             {"note": {"id": w["note_id"],
                                       "fields": build_anki_fields(card, w)}},
                             timeout=20)
                rf["count"] = rf.get("count", 0) + 1
                rf["last_ts"] = now_iso()
                rf["lapses_at"] = w["lapses"]
                rf["stage"] = "rewritten"
                write_rec(w["recfile"], w["rec"])
                print(f"  ✓ 已改写 note {w['note_id']}（第 {rf['count']} 次），record 回写")

        elif st == "L2":
            delta = w["lapses"] - (card.get("_refresh", {}).get("lapses_at", 0))
            resp = ai_client.ask(prompt_escalate(card, w["lapses"], delta, note_ctx))
            j = parse_json(resp)
            if not j:
                print("  ✗ AI 返回非 JSON，跳过")
                continue
            act = j.get("action")
            print(f"  升级判定: {act} — {j.get('reason','')}")
            if act == "split":
                for sc in j.get("cards", []):
                    print(f"    子卡: {sc.get('front','')!r} / {sc.get('back','')!r}")
            if not args.apply_escalation:
                print("  （L2 破坏性，未加 --apply-escalation，仅建议不执行）")
                continue
            # 执行升级（破坏性）
            if act == "delete":
                anki_request(ANKI_URL, "deleteNotes", {"notes": [w["note_id"]]})
                w["rec"]["cards"] = [c for c in w["rec"]["cards"]
                                     if c.get("local_id") != card["local_id"]]
                write_rec(w["recfile"], w["rec"])
                print(f"  ✓ 已删除 note {w['note_id']} + record 移除")
            elif act == "split":
                subs = j.get("cards", [])
                if not subs or not latex_ok(
                        [f for sc in subs for f in (sc.get("front", ""),
                         sc.get("back", ""), sc.get("text", ""))]):
                    print("  ✗ 拆分子卡为空或含裸文本，放弃")
                    continue
                base = card["local_id"].rsplit("-", 1)[0]
                for i, sc in enumerate(subs, 1):
                    nc = {"local_id": f"{base}-s{i:02d}",
                          "type": sc.get("type", "basic"),
                          "deck": card.get("deck", ""),
                          "front": sc.get("front", ""), "back": sc.get("back", ""),
                          "text": sc.get("text", ""),
                          "reason": f"L2拆分自 {card['local_id']}",
                          "tags": card.get("tags", []), "status": "synced"}
                    note = {"deckName": nc["deck"],
                            "modelName": "Obsidian-cloze" if nc["type"] == "cloze"
                            else "Obsidian-basic",
                            "fields": build_anki_fields(nc, w),
                            "tags": ["obsidian", "ai_generated", *nc["tags"]]}
                    try:
                        nc["anki_note_id"] = anki_request(
                            ANKI_URL, "addNote", {"note": note}, timeout=20)
                        w["rec"]["cards"].append(nc)
                        print(f"  ✓ 新子卡 {nc['local_id']} → {nc['anki_note_id']}")
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ 子卡 {nc['local_id']} 失败: {e}")
                anki_request(ANKI_URL, "deleteNotes", {"notes": [w["note_id"]]})
                w["rec"]["cards"] = [c for c in w["rec"]["cards"]
                                     if c.get("local_id") != card["local_id"]]
                write_rec(w["recfile"], w["rec"])
                print(f"  ✓ 拆分完成，原 note {w['note_id']} 已删")

    mode = ("APPLY" + ("+ESCALATION" if args.apply_escalation else "")
            if args.apply else "DRY-RUN")
    print(f"\n处理 {picked} 张（{mode}）"
          + ("" if args.apply else "  —— 加 --apply 执行 L1 改写"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
