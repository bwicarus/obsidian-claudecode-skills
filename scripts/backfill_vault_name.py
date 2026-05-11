"""一次性回填：把 Anki 卡片 footer 和 records 中 vault=obsidian 替换为 vault=Obsidian%20Vault。

records JSON 里 source_url 是原始 URL（& 未 escape），Anki 字段里是 HTML（& → &amp;），都要处理。

用法：
  python scripts/backfill_vault_name.py --dry-run
  python scripts/backfill_vault_name.py                # 先 records 后 Anki
  python scripts/backfill_vault_name.py --records-only # 只改本地 records JSON
  python scripts/backfill_vault_name.py --anki-only    # 只改 Anki 字段
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[1]
RECORDS_DIR = PROJECT_DIR / "anki" / "records"
ANKI_URL = "http://127.0.0.1:8765"

OLD_URL_FRAG = "vault=obsidian&"
NEW_URL_FRAG = "vault=Obsidian%20Vault&"
OLD_HTML_FRAG = "vault=obsidian&amp;"
NEW_HTML_FRAG = "vault=Obsidian%20Vault&amp;"


def anki(action: str, params: dict | None = None, timeout: int = 30):
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        ANKI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    result = json.loads(body)
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result.get("result")


def update_records(dry_run: bool) -> list[int]:
    record_paths = sorted(RECORDS_DIR.glob("*.json"))
    print(f"扫到 {len(record_paths)} 个 record 文件")

    note_ids: list[int] = []
    record_changed = 0

    for rp in record_paths:
        data = json.loads(rp.read_text(encoding="utf-8"))
        before = json.dumps(data, ensure_ascii=False)
        if isinstance(data.get("source_url"), str) and OLD_URL_FRAG in data["source_url"]:
            data["source_url"] = data["source_url"].replace(OLD_URL_FRAG, NEW_URL_FRAG)
        for card in data.get("cards", []):
            nid = card.get("anki_note_id")
            if nid:
                note_ids.append(int(nid))
        after = json.dumps(data, ensure_ascii=False)
        if before != after:
            record_changed += 1
            print(f"  record 待更新: {rp.name}")
            if not dry_run:
                rp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

    print(f"records JSON 改动: {record_changed} 个，收集 anki_note_id: {len(note_ids)} 个")
    return note_ids


def collect_note_ids() -> list[int]:
    note_ids: list[int] = []
    for rp in sorted(RECORDS_DIR.glob("*.json")):
        data = json.loads(rp.read_text(encoding="utf-8"))
        for card in data.get("cards", []):
            nid = card.get("anki_note_id")
            if nid:
                note_ids.append(int(nid))
    return note_ids


def update_anki(note_ids: list[int], dry_run: bool) -> int:
    if not note_ids:
        print("没有 note_id 需要处理")
        return 0
    notes_info = anki("notesInfo", {"notes": note_ids})
    print(f"AnkiConnect notesInfo 返回: {len(notes_info)} 条")

    updated = 0
    missing = 0
    for info in notes_info:
        if not info or not info.get("noteId"):
            missing += 1
            continue
        nid = info["noteId"]
        fields = info.get("fields") or {}
        new_fields: dict[str, str] = {}
        for fname, fdata in fields.items():
            v = fdata.get("value", "")
            new_v = v
            if OLD_HTML_FRAG in new_v:
                new_v = new_v.replace(OLD_HTML_FRAG, NEW_HTML_FRAG)
            if OLD_URL_FRAG in new_v:
                new_v = new_v.replace(OLD_URL_FRAG, NEW_URL_FRAG)
            if new_v != v:
                new_fields[fname] = new_v
        if not new_fields:
            continue
        print(f"  note {nid}: 更新字段 {list(new_fields.keys())}")
        if not dry_run:
            anki("updateNoteFields", {"note": {"id": nid, "fields": new_fields}})
        updated += 1

    print(f"Anki note 改动: {updated} 条；missing/已删除: {missing}")
    return updated


def main(args: argparse.Namespace) -> int:
    if not args.anki_only:
        note_ids = update_records(args.dry_run)
    else:
        note_ids = collect_note_ids()

    if args.records_only:
        if args.dry_run:
            print("(dry-run，未写入)")
        return 0

    try:
        updated = update_anki(note_ids, args.dry_run)
    except (urllib.error.URLError, RuntimeError) as e:
        print(f"\nAnkiConnect 调用失败: {e}", file=sys.stderr)
        print("提示：请打开 Anki 主窗口，确保 AnkiConnect 插件启用（端口 8765），然后重跑：", file=sys.stderr)
        print("  python scripts/backfill_vault_name.py --anki-only", file=sys.stderr)
        return 1

    if not args.dry_run and not args.no_sync and updated > 0:
        try:
            anki("sync", timeout=120)
            print("已触发 AnkiWeb 同步")
        except (urllib.error.URLError, RuntimeError) as e:
            print(f"同步失败（可手动同步）：{e}", file=sys.stderr)

    if args.dry_run:
        print("(dry-run，未写入)")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--records-only", action="store_true", help="只改本地 records JSON")
    p.add_argument("--anki-only", action="store_true", help="只改 Anki 字段")
    p.add_argument("--no-sync", action="store_true", help="改完 Anki 后不触发 AnkiWeb 同步")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(main(parse_args()))
