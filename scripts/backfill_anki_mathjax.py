"""一次性脚本：把 anki/records 里的 $...$ / $$...$$ 替换为 \\(...\\) / \\[...\\]。

为什么需要：Anki 默认 MathJax 只识别 \\(...\\)、\\[...\\]、[$]...[/$]、[$$]...[/$$]。
$...$ 在 AnkiMobile / AnkiDroid 上不会渲染，公式会以原始文本形式显示。

脚本动作：
  1. 扫描 anki/records/*.json
  2. 把每张卡的 front / back / text 中的 $$...$$ → \\[...\\]、$...$ → \\(...\\)
  3. 对已同步的卡（含 anki_note_id），调用 AnkiConnect updateNoteFields 同步更新
  4. 写回 records JSON
  5. 触发一次 AnkiWeb 同步

用法：
  python scripts/backfill_anki_mathjax.py --dry-run   # 预览
  python scripts/backfill_anki_mathjax.py             # 真改
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from anki_from_note import (  # noqa: E402
    DEFAULT_ANKI_URL,
    DEFAULT_VAULT_NAME,
    anki_request,
    html_text,
    obsidian_url,
    source_footer,
    wait_for_anki,
)

PROJECT_DIR = Path(__file__).resolve().parents[1]
RECORDS_DIR = PROJECT_DIR / "anki" / "records"

BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
INLINE_RE = re.compile(r"\$([^$\n]+?)\$")


def transform_text(text: str) -> str:
    if not text or "$" not in text:
        return text
    text = BLOCK_RE.sub(lambda m: "\\[" + m.group(1) + "\\]", text)
    text = INLINE_RE.sub(lambda m: "\\(" + m.group(1) + "\\)", text)
    return text


def card_diff(card: dict[str, Any]) -> dict[str, tuple[str, str]] | None:
    diff: dict[str, tuple[str, str]] = {}
    for key in ("front", "back", "text"):
        before = card.get(key) or ""
        after = transform_text(before)
        if before != after:
            diff[key] = (before, after)
    return diff or None


def apply_diff(card: dict[str, Any], diff: dict[str, tuple[str, str]]) -> dict[str, Any]:
    updated = dict(card)
    for key, (_before, after) in diff.items():
        updated[key] = after
    return updated


def build_fields(card: dict[str, Any], source_link: str, source_url: str) -> dict[str, str]:
    footer = source_footer(source_link, source_url, card.get("reason", ""), card["local_id"])
    if card["type"] == "cloze":
        extra = html_text(card.get("back", "") or "")
        return {
            "Text": html_text(card.get("text", "") or ""),
            "Extra": (extra + footer) if extra else footer,
        }
    return {
        "Front": html_text(card.get("front", "") or ""),
        "Back": html_text(card.get("back", "") or "") + footer,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="把 Anki 卡片的 $...$ 分隔符回填为 \\(...\\)")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入也不动 Anki")
    parser.add_argument("--anki-url", default=DEFAULT_ANKI_URL)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--no-sync", action="store_true", help="不触发 AnkiWeb 同步")
    parser.add_argument(
        "--fix-missing-url",
        action="store_true",
        help="对缺 source_url 的 record 现算 URL，并强制重发所有已同步卡（修正空 href）",
    )
    args = parser.parse_args()

    records = sorted(RECORDS_DIR.glob("*.json"))
    if not records:
        print("没有找到任何 records JSON。")
        return

    plan: list[tuple[Path, dict[str, Any], list[tuple[int, dict[str, tuple[str, str]]]]]] = []

    for record_path in records:
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"跳过（无法解析）：{record_path}")
            continue

        cards = data.get("cards") or []
        mods: list[tuple[int, dict[str, tuple[str, str]]]] = []
        url_was_missing = args.fix_missing_url and not data.get("source_url")
        if url_was_missing:
            source_note = data.get("source_note") or ""
            data["source_url"] = obsidian_url(DEFAULT_VAULT_NAME, source_note)

        for idx, card in enumerate(cards):
            if not isinstance(card, dict):
                continue
            diff = card_diff(card)
            if diff:
                mods.append((idx, diff))
            elif url_was_missing and card.get("anki_note_id") and any(
                ("\\(" in (card.get(k) or "")) or ("\\[" in (card.get(k) or ""))
                for k in ("front", "back", "text")
            ):
                # 这张卡之前已经被改成 \(...\) 形式，但当时 record 缺 source_url，
                # 导致 footer 的 href 写成了空字符串。需要现算 URL 后重发字段。
                mods.append((idx, {}))

        if mods:
            plan.append((record_path, data, mods))

    if not plan:
        print("没有需要回填的卡片。")
        return

    total_cards = 0
    print("=== 待更新明细 ===")
    for record_path, _data, mods in plan:
        print(f"\n[{record_path.name}] {len(mods)} 张卡")
        for idx, diff in mods:
            card = _data["cards"][idx]
            note_id = card.get("anki_note_id")
            tag = "（仅刷新 footer URL）" if not diff else ""
            print(f"  card[{idx}] note_id={note_id} {tag}")
            for field, (before, after) in diff.items():
                print(f"    {field}:")
                print(f"      - {before}")
                print(f"      + {after}")
            total_cards += 1
    print(f"\n=== 汇总：{len(plan)} 个 record，{total_cards} 张卡 ===")

    if args.dry_run:
        print("\n[dry-run] 退出，未做任何写入。")
        return

    wait_for_anki(args.anki_url, args.wait_seconds)

    success = 0
    skipped_no_id = 0
    failed: list[str] = []

    for record_path, data, mods in plan:
        source_link = data.get("source_link") or ""
        source_url = data.get("source_url") or ""
        cards = data["cards"]

        for idx, diff in mods:
            updated = apply_diff(cards[idx], diff)
            note_id = updated.get("anki_note_id")
            if note_id is None:
                cards[idx] = updated
                skipped_no_id += 1
                print(f"  SKIP（无 note_id）{record_path.name} card[{idx}]")
                continue

            try:
                fields = build_fields(updated, source_link, source_url)
                anki_request(
                    args.anki_url,
                    "updateNoteFields",
                    {"note": {"id": note_id, "fields": fields}},
                    timeout=30,
                )
                cards[idx] = updated
                success += 1
                print(f"  OK note {note_id} ({record_path.name})")
            except RuntimeError as e:
                failed.append(f"{record_path.name} note {note_id}: {e}")
                print(f"  FAIL note {note_id} ({record_path.name}): {e}")

        record_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  写回 {record_path.name}")

    print()
    print(f"完成：成功 {success}，跳过 {skipped_no_id}，失败 {len(failed)}。")
    for line in failed:
        print(f"  FAIL {line}")

    if not args.no_sync and success > 0:
        try:
            anki_request(args.anki_url, "sync", timeout=120)
            print("已触发 AnkiWeb 同步")
        except RuntimeError as e:
            print(f"AnkiWeb 同步失败（可手动同步）：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()
