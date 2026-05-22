"""
reconcile_records.py — 对账：把「Anki 里有、records 没跟踪」的游离卡补回 records。

游离卡成因：卡片走 AnkiWeb 同步、records 走 git 同步，是两条独立通道。
某台机器制卡同步到 AnkiWeb 后，若 record 没提交/推到 git（如服务器迁移、
多实例切换），别的实例从 AnkiWeb 拉到卡却没记录 → 游离卡。

每张 ai_generated 卡的 footer 都带 Local ID + 来源链接，所以 records 可从
Anki 反向重建。本脚本扫描所有 ai_generated 卡，凡 anki_note_id 不在任何
record 里的，按其 footer 的来源(source_note) 归到对应 record 文件、补一条卡记录。

默认 dry-run，加 --apply 才写入。可放进 daily 流程自愈。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

ANKI_URL = config.ANKI_CONNECT_URL
RECORDS_DIR = config.RECORDS_DIR
FOOTER_MARKER = '<hr><div style="font-size:0.85em;color:#666;">'


def anki(action: str, params: dict | None = None, timeout: int = 60):
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(ANKI_URL, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode("utf-8"))
    if res.get("error"):
        raise RuntimeError(f"AnkiConnect error: {res['error']}")
    return res["result"]


def safe_stem(source_note: str) -> str:
    if source_note.lower().endswith(".md"):
        source_note = source_note[:-3]
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "__", source_note)


def strip_footer(v: str) -> str:
    i = v.find(FOOTER_MARKER)
    return (v[:i] if i != -1 else v).strip()


def tracked_note_ids() -> set[int]:
    nids: set[int] = set()
    for f in RECORDS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in d.get("cards") or []:
            if c.get("anki_note_id"):
                nids.add(c["anki_note_id"])
    return nids


def card_from_anki(info: dict) -> dict | None:
    fields = info.get("fields") or {}
    is_cloze = "Text" in fields and "Front" not in fields
    body = (fields.get("Extra" if is_cloze else "Back") or {}).get("value", "")
    m_lid = re.search(r"Local ID：([0-9][0-9\-]+)", body)
    m_file = re.search(r'file=([^&"]+)', body)
    if not m_lid or not m_file:
        return None
    lid = m_lid.group(1)
    sn = urllib.parse.unquote(m_file.group(1)) + ".md"
    m_reason = re.search(r"原因：([^<]*)", body)
    reason = html.unescape(m_reason.group(1).strip()) if m_reason else "对账补录"
    nid = info["noteId"]
    deck_map = anki("getDecks", {"cards": anki("findCards", {"query": f"nid:{nid}"})}) or {}
    deck = next(iter(deck_map), "Obsidian::未分类")
    return {
        "local_id": lid, "type": "cloze" if is_cloze else "basic", "deck": deck,
        "front": "" if is_cloze else strip_footer((fields.get("Front") or {}).get("value", "")),
        "back": strip_footer((fields.get("Extra" if is_cloze else "Back") or {}).get("value", "")),
        "text": strip_footer((fields.get("Text") or {}).get("value", "")) if is_cloze else "",
        "reason": reason,
        "tags": [t for t in info.get("tags", []) if t not in ("obsidian", "ai_generated")],
        "anki_note_id": nid, "status": "synced", "_reconciled": True,
        "_source_note": sn,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写入 records（默认 dry-run）")
    args = ap.parse_args()

    tracked = tracked_note_ids()
    nids = anki("findNotes", {"query": "tag:ai_generated"})
    orphans: list[dict] = []
    for i in range(0, len(nids), 50):
        for info in anki("notesInfo", {"notes": nids[i:i + 50]}):
            if not info or "fields" not in info:
                continue
            if info["noteId"] in tracked:
                continue
            c = card_from_anki(info)
            if c:
                orphans.append(c)

    print(f"Anki ai_generated: {len(nids)} | records 跟踪: {len(tracked)} | 游离: {len(orphans)}")
    if not orphans:
        print("无游离卡，records 与 Anki 一致。")
        return 0

    for c in orphans:
        sn = c.pop("_source_note")
        rf = RECORDS_DIR / f"{safe_stem(sn)}.json"
        print(f"  {'补录' if args.apply else 'DRY'} {c['local_id']} → {rf.name} (deck={c['deck']})")
        if not args.apply:
            continue
        if rf.exists():
            d = json.loads(rf.read_text(encoding="utf-8"))
        else:
            stem = sn.rsplit("/", 1)[-1]
            stem = stem[:-3] if stem.lower().endswith(".md") else stem
            d = {"source_note": sn, "source_link": f"[[{stem}]]", "source_url": "",
                 "generator": "reconcile", "status": "synced", "warnings": [],
                 "section_hashes": {}, "cards": []}
        if any(x.get("local_id") == c["local_id"] for x in d.get("cards", [])):
            continue
        d.setdefault("cards", []).append(c)
        rf.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"{'已补录' if args.apply else '将补录(加 --apply)'} {len(orphans)} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
