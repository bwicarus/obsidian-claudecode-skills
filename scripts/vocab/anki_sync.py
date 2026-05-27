"""Anki 复习 → vocab mastery 双向同步。

正向（Anki → vocab）：
  扫所有 Vocab deck cards 的 reviews（近 N 天），按 ease 映射 mastery delta：
    1=again -0.15 / 2=hard -0.05 / 3=good +0.05 / 4=easy +0.15
  累加到 vocab .md frontmatter.mastery，最后写回。

反向（vocab → Anki）：
  mastery ≥ 0.95 → 把卡 suspend（不再加复习负担）
  mastery < 0.85 + 处于 suspend 状态 → unsuspend
  （0.85~0.95 维持现状，防抖）

CLI：
  python3 scripts/vocab/anki_sync.py [--days 7] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
ANKI_URL     = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")

sys.path.insert(0, str(Path(__file__).parent))
import vocab_index  # noqa: E402
import paragraph_exposure as pe  # noqa: E402 - 复用 _bump_mastery / _label_for

EASE_TO_DELTA = {1: -0.15, 2: -0.05, 3: 0.05, 4: 0.15}


def anki(action, params=None, timeout=20):
    data = json.dumps({"action": action, "version": 6, "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(ANKI_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if body.get("error"):
        return None
    return body.get("result")


def collect_recent_reviews(days: int = 7) -> dict[int, list[int]]:
    """返回 {note_id: [ease_1, ease_2, ...]} 近 N 天 review 历史。"""
    nids = anki("findNotes", {"query": "deck:Vocab"}) or []
    if not nids:
        return {}
    notes_info = anki("notesInfo", {"notes": nids}) or []
    note_to_cards = {n.get("noteId"): n.get("cards", []) for n in notes_info}
    all_cards = [c for cs in note_to_cards.values() for c in cs]
    if not all_cards:
        return {}
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    reviews_by_card = anki("getReviewsOfCards", {"cards": all_cards}) or {}
    # reviews format: {"<cardId>": [{"id": ms_ts, "ease": 1..4, ...}, ...]}
    out: dict[int, list[int]] = {}
    for nid, cids in note_to_cards.items():
        eases = []
        for cid in cids:
            for rv in (reviews_by_card.get(str(cid)) or reviews_by_card.get(cid) or []):
                if rv.get("id", 0) >= cutoff_ms and rv.get("ease", 0) in EASE_TO_DELTA:
                    eases.append(rv["ease"])
        if eases:
            out[nid] = eases
    return out


def sync_from_anki(days: int = 7, dry_run: bool = False) -> dict:
    """正向：Anki review → vocab mastery."""
    reviews = collect_recent_reviews(days)
    if not reviews:
        return {"updated": 0, "msg": "no recent reviews"}
    # 反查 note_id → lemma：遍历 vocab_index 找匹配 anki_card_id
    idx = vocab_index.index(force_reload=True)
    nid_to_lemma: dict[int, str] = {}
    for word, info in idx.items():
        if info.get("anki_card_id"):
            try:
                nid_to_lemma[int(info["anki_card_id"])] = info["lemma"]
            except (ValueError, TypeError):
                pass
    updated = []
    for nid, eases in reviews.items():
        lemma = nid_to_lemma.get(nid)
        if not lemma: continue
        total_delta = sum(EASE_TO_DELTA[e] for e in eases)
        if abs(total_delta) < 0.01: continue
        if dry_run:
            updated.append({"lemma": lemma, "eases": eases, "delta": round(total_delta, 3)})
        else:
            result = pe._bump_mastery(lemma, total_delta)
            if result:
                old, new = result
                updated.append({"lemma": lemma, "eases": eases,
                                "delta": round(total_delta, 3),
                                "old": round(old, 3), "new": round(new, 3)})
    return {"updated_count": len(updated), "updated": updated}


def sync_to_anki(dry_run: bool = False) -> dict:
    """反向：mastery ≥ 0.95 → suspend；< 0.85 + suspended → unsuspend."""
    idx = vocab_index.index(force_reload=True)
    nids_high, nids_low = [], []
    for word, info in idx.items():
        if not info.get("anki_card_id"): continue
        try:
            nid = int(info["anki_card_id"])
        except (ValueError, TypeError):
            continue
        if info["mastery"] >= 0.95:
            nids_high.append(nid)
        elif info["mastery"] < 0.85:
            nids_low.append(nid)
    # note id → card ids
    def _cards_of(nids):
        if not nids: return []
        info_list = anki("notesInfo", {"notes": list(set(nids))}) or []
        cs = []
        for n in info_list:
            cs.extend(n.get("cards", []))
        return cs

    high_cards = _cards_of(nids_high)
    low_cards = _cards_of(nids_low)
    # 当前 suspended cards
    susp_high = anki("areSuspended", {"cards": high_cards}) or []
    susp_low = anki("areSuspended", {"cards": low_cards}) or []
    to_suspend = [c for c, s in zip(high_cards, susp_high) if s is False]
    to_unsuspend = [c for c, s in zip(low_cards, susp_low) if s is True]
    if not dry_run:
        if to_suspend: anki("suspend", {"cards": to_suspend})
        if to_unsuspend: anki("unsuspend", {"cards": to_unsuspend})
    return {"suspended": len(to_suspend), "unsuspended": len(to_unsuspend)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("[1/2] Anki review → mastery…")
    r1 = sync_from_anki(args.days, args.dry_run)
    print(json.dumps(r1, ensure_ascii=False, indent=2))
    print("[2/2] mastery → Anki suspend…")
    r2 = sync_to_anki(args.dry_run)
    print(json.dumps(r2, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
