"""Anki 接线：制卡（必须带节点）+ 已绑卡复习快照 → anki.snapshot 事件。

复用 scripts/anki_status.py 的 card_mastery / fsrs_memory_map / anki_request，不另造公式。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .register import RegisterError, anki_snapshot, bind_card
from .store import Ledger, loads

_SCRIPTS = Path(__file__).resolve().parent.parent

DEFAULT_DECK = "KJ"
DEFAULT_MODEL = "Basic"


def node_provenance_html(ledger: Ledger, node_ids: list[str]) -> str:
    """卡片背面末尾的来源栏：每个绑定节点一条 obsidian://open 深链。
    复习卡 UI 与 Anki 桌面版都从这里拿"打开节点"的链接（服务端 _anki_visible_source_urls 只认这种 obsidian://open?file= 形状）。"""
    import html as _html
    from .markdown import obsidian_url
    links = []
    for nid in node_ids:
        n = ledger.node(nid)
        if n is None:
            continue
        links.append('<a href="%s" rel="noopener noreferrer">%s</a>' % (_html.escape(obsidian_url(n["id"], n["name"]), quote=True), _html.escape(n["name"])))
    if not links:
        return ""
    return '<hr><div class="bw-reader-anki-source">节点：' + " · ".join(links) + "</div>"


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
    back_html = back + node_provenance_html(ledger, node_ids)
    note = {"deckName": deck, "modelName": model, "fields": {"Front": front, "Back": back_html}, "tags": tags,
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


DEFAULT_BINDINGS_PATH = Path(os.environ.get("BW_BRIDGE_RUNTIME") or (Path.home() / "bw-computer-voice-bridge" / "runtime")) / "kj-card-bindings.jsonl"
_BINDINGS_OFFSET_KEY = "bridge_bindings_offset"
_PROVENANCE_MARK = 'class="bw-reader-anki-source"'


def ingest_bridge_bindings(ledger: Ledger, path: str | Path | None = None, *, anki_url: str | None = None,
                           request: Callable[..., Any] | None = None, add_provenance: bool = True) -> dict:
    """读 Windows 桥写的绑定账本（追加式 JSONL：确认入库的卡 ↔ nodeIds），把卡绑到节点（幂等：card_key=anki:<note>），
    再给 Anki 卡背面补上节点深链。游标存 meta 表，只处理新增行；节点不存在的行落到 state/kj/unresolved-bindings.jsonl 等人工处理。"""
    p = Path(path or DEFAULT_BINDINGS_PATH)
    out = {"path": str(p), "lines": 0, "bound": 0, "skipped": 0, "unresolved": 0, "nodes": [], "notes": []}
    if not p.exists():
        return out
    row = ledger.db.execute("SELECT v FROM meta WHERE k=?", (_BINDINGS_OFFSET_KEY,)).fetchone()
    offset = int(row["v"]) if row else 0
    data = p.read_bytes()
    if offset > len(data):
        offset = 0  # 账本被重建/截断：从头重放（bind_card 幂等）
    touched: set[str] = set()
    new_notes: list[tuple[int, list[str]]] = []
    unresolved_path = ledger.path.parent / "unresolved-bindings.jsonl"
    for raw in data[offset:].split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        out["lines"] += 1
        try:
            rec = json.loads(raw.decode("utf-8"))
        except Exception:
            out["skipped"] += 1
            continue
        node_ids = [str(n) for n in (rec.get("nodeIds") or []) if n]
        note_ids = [int(x) for x in (rec.get("noteIds") or []) if str(x).isdigit()]
        known = [n for n in node_ids if ledger.resolve(n) is not None]
        if not node_ids or not note_ids:
            out["skipped"] += 1
            continue
        if not known:
            out["unresolved"] += 1
            with unresolved_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            continue
        for nid in note_ids:
            ev = bind_card(ledger, node_ids=known, anki_note_id=nid, anki_card_ids=[int(c) for c in (rec.get("cardIds") or []) if str(c).isdigit()],
                           front=str(rec.get("front") or rec.get("cloze") or "")[:400], back=str(rec.get("back") or "")[:400],
                           deck="QA", card_key=f"anki:{nid}", actor="bridge")
            out["bound"] += 1
            touched.update(ev.node_ids)
            new_notes.append((nid, list(ev.payload["node_ids"])))
    with ledger._lock:
        ledger.db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (_BINDINGS_OFFSET_KEY, str(len(data))))
    out["nodes"] = sorted(touched)
    out["notes"] = [n for n, _ in new_notes]
    if add_provenance and new_notes:
        out["provenance_updated"] = _append_node_provenance(ledger, new_notes, anki_url=anki_url, request=request)
    return out


def _append_node_provenance(ledger: Ledger, notes: list[tuple[int, list[str]]], *, anki_url: str | None = None,
                            request: Callable[..., Any] | None = None) -> int:
    """给刚绑定的 Anki 卡背面（最后一个字段）追加"节点：<a obsidian://…>"来源栏；已有就不重复。失败不抛。"""
    try:
        AS = _anki()
        url = anki_url or AS.DEFAULT_ANKI_URL
        req = request or AS.anki_request
        infos = req(url, "notesInfo", {"notes": [n for n, _ in notes]}) or []
    except Exception:
        return 0
    by_id = {int(i.get("noteId") or 0): i for i in infos if isinstance(i, dict)}
    updated = 0
    for note_id, node_ids in notes:
        info = by_id.get(note_id)
        if not info:
            continue
        fields = info.get("fields") or {}
        if not fields:
            continue
        name = max(fields, key=lambda k: (fields[k] or {}).get("order", 0))
        value = str((fields[name] or {}).get("value") or "")
        if _PROVENANCE_MARK in value and "obsidian://open" in value:
            continue
        try:
            req(url, "updateNoteFields", {"note": {"id": note_id, "fields": {name: value + node_provenance_html(ledger, node_ids)}}}, timeout=15)
            updated += 1
        except Exception:
            continue
    return updated


def sync_snapshots(ledger: Ledger, *, anki_url: str | None = None, request: Callable[..., Any] | None = None,
                   fsrs: Callable[..., dict] | None = None, now: float | None = None, bindings_path: str | Path | None = None) -> dict:
    """先吸收桥的绑定账本，再对所有活跃绑卡：notesInfo → cardsInfo → card_mastery → anki.snapshot（按卡+日幂等）。返回统计与受影响节点。"""
    AS = _anki()
    url = anki_url or AS.DEFAULT_ANKI_URL
    req = request or AS.anki_request
    fsrs_fn = fsrs or AS.fsrs_memory_map
    ingest = ingest_bridge_bindings(ledger, bindings_path, anki_url=url, request=req)
    cards = ledger.db.execute("SELECT card_key, anki_note_id, anki_card_ids_json FROM cards WHERE status='active' AND anki_note_id IS NOT NULL").fetchall()
    if not cards:
        return {"cards": 0, "snapshots": 0, "nodes": ingest["nodes"], "ingest": ingest}
    key_by_note = {int(c["anki_note_id"]): c["card_key"] for c in cards}
    note_ids = list(key_by_note)
    infos = req(url, "notesInfo", {"notes": note_ids}) or []
    card_note: dict[int, int] = {}
    for info in infos:
        nid = int(info.get("noteId") or 0)
        for cid in info.get("cards") or []:
            card_note[int(cid)] = nid
    if not card_note:
        return {"cards": len(cards), "snapshots": 0, "nodes": ingest["nodes"], "ingest": ingest, "note": "Anki 里找不到这些笔记"}
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
    touched.update(ingest["nodes"])
    return {"cards": len(cards), "snapshots": n, "nodes": sorted(touched), "ingest": ingest}


def card_ids_of(ledger: Ledger, card_key: str) -> list[int]:
    row = ledger.db.execute("SELECT anki_card_ids_json FROM cards WHERE card_key=?", (card_key,)).fetchone()
    return [int(x) for x in loads(row["anki_card_ids_json"], [])] if row else []
