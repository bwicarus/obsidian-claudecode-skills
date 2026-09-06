"""Anki 接线：制卡（必须带节点）+ 已绑卡复习快照 → anki.snapshot 事件。

复用 scripts/anki_status.py 的 card_mastery / fsrs_memory_map / anki_request，不另造公式。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

from .register import RegisterError, anki_snapshot, bind_card
from .store import Ledger, loads

_SCRIPTS = Path(__file__).resolve().parent.parent

DEFAULT_DECK = "KJ"
DEFAULT_MODEL = "Basic"


def _anki():
    """延迟导入 scripts/anki_status.py（只在真要碰 Anki 时才把 scripts 目录放进 sys.path）。"""
    try:
        import anki_status as AS
    except ModuleNotFoundError:
        if str(_SCRIPTS) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS))
        import anki_status as AS
    return AS


def make_card(ledger: Ledger, *, node_ids: list[str], front: str, back: str, deck: str = DEFAULT_DECK,
              model: str = DEFAULT_MODEL, tags: list[str] | None = None, anki_url: str | None = None,
              request: Callable[..., Any] | None = None, actor: str = "") -> dict:
    """AnkiConnect addNote（+ changeDeck 归位，见 CLAUDE.md 的 addNote deckName 不生效坑）→ 绑定节点。"""
    if not (front or "").strip() or not (back or "").strip():
        raise RegisterError("missing_field", "卡片正反面都要有内容")
    if not node_ids:
        raise RegisterError("missing_node", "制卡必须关联至少一个有效节点")
    AS = _anki()
    url = anki_url or AS.DEFAULT_ANKI_URL
    req = request or AS.anki_request
    tags = list(dict.fromkeys(["kj", *(tags or [])] + [f"kj::{n.replace(':', '_')}" for n in node_ids]))
    note = {"deckName": deck, "modelName": model, "fields": {"Front": front, "Back": back}, "tags": tags,
            "options": {"allowDuplicate": False, "duplicateScope": "deck"}}
    try:
        req(url, "createDeck", {"deck": deck}, timeout=15)
    except Exception:
        pass
    note_id = req(url, "addNote", {"note": note}, timeout=30)
    if not note_id:
        raise RegisterError("anki_failed", "AnkiConnect addNote 没返回 note id")
    card_ids = []
    try:
        card_ids = req(url, "findCards", {"query": f"nid:{int(note_id)}"}, timeout=15) or []
        if card_ids:
            req(url, "changeDeck", {"cards": card_ids, "deck": deck}, timeout=15)
    except Exception:
        pass
    ev = bind_card(ledger, node_ids=node_ids, anki_note_id=int(note_id), anki_card_ids=[int(c) for c in card_ids],
                   front=front, back=back, deck=deck, actor=actor)
    return {"anki_note_id": int(note_id), "anki_card_ids": [int(c) for c in card_ids], "card_key": ev.payload["card_key"],
            "node_ids": ev.payload["node_ids"], "event": ev.id}


def sync_snapshots(ledger: Ledger, *, anki_url: str | None = None, request: Callable[..., Any] | None = None,
                   fsrs: Callable[..., dict] | None = None, now: float | None = None) -> dict:
    """所有活跃绑卡：notesInfo → cardsInfo → card_mastery → anki.snapshot（按卡+日幂等）。返回统计与受影响节点。"""
    AS = _anki()
    url = anki_url or AS.DEFAULT_ANKI_URL
    req = request or AS.anki_request
    fsrs_fn = fsrs or AS.fsrs_memory_map
    cards = ledger.db.execute("SELECT card_key, anki_note_id, anki_card_ids_json FROM cards WHERE status='active' AND anki_note_id IS NOT NULL").fetchall()
    if not cards:
        return {"cards": 0, "snapshots": 0, "nodes": []}
    key_by_note = {int(c["anki_note_id"]): c["card_key"] for c in cards}
    note_ids = list(key_by_note)
    infos = req(url, "notesInfo", {"notes": note_ids}) or []
    card_note: dict[int, int] = {}
    for info in infos:
        nid = int(info.get("noteId") or 0)
        for cid in info.get("cards") or []:
            card_note[int(cid)] = nid
    if not card_note:
        return {"cards": len(cards), "snapshots": 0, "nodes": [], "note": "Anki 里找不到这些笔记"}
    cards_info = req(url, "cardsInfo", {"cards": list(card_note)}) or []
    try:
        fsrs_mem = fsrs_fn(url, list(card_note))
    except Exception:
        fsrs_mem = {}
    now_ms = int((now or time.time()) * 1000)
    touched: set[str] = set()
    n = 0
    for card in cards_info:
        cid = int(card.get("cardId") or card.get("id") or 0)
        note_id = card_note.get(cid)
        if not note_id:
            continue
        key = key_by_note.get(note_id)
        node_ids = [r[0] for r in ledger.db.execute("SELECT node_id FROM card_nodes WHERE card_key=?", (key,))]
        if not node_ids:
            continue
        m = AS.card_mastery(card, now_ms, fsrs_mem)
        ev = anki_snapshot(ledger, card_id=cid, mastery=m, node_ids=node_ids, card_key=key, ts=now_ms // 1000)
        if not ev.duplicate:
            n += 1
            touched.update(node_ids)
    return {"cards": len(cards), "snapshots": n, "nodes": sorted(touched)}


def card_ids_of(ledger: Ledger, card_key: str) -> list[int]:
    row = ledger.db.execute("SELECT anki_card_ids_json FROM cards WHERE card_key=?", (card_key,)).fetchone()
    return [int(x) for x in loads(row["anki_card_ids_json"], [])] if row else []
