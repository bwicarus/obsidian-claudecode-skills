"""
Anki 制卡机械脚本（agent 驱动版）。

AI 判断与卡片生成由 Claude Code / Codex 在脚本外完成；本脚本只负责：
  1. 批量收集待处理 md，并按 records JSON 判断是否跳过
  2. 输出单篇笔记 + references 的制卡上下文，供外层 agent 阅读
  3. 接收外层 agent 生成的卡片 JSON
  4. 调用 AnkiConnect 创建卡片
  5. 将 Anki 返回的 note_id 写入独立 records JSON

原始 md 文件不会被修改。

用法：
  # 列出待处理笔记
  python scripts/anki_from_note.py --list --dir <目录> --recursive

  # 输出某篇笔记的制卡上下文，给 Claude/Codex 判断
  python scripts/anki_from_note.py --read <笔记路径>

  # 同步 Claude/Codex 生成的卡片 JSON
  python scripts/anki_from_note.py --sync <笔记路径> --cards-json <cards.json>

  # 记录“无需制卡”，避免下次重复消耗 token
  python scripts/anki_from_note.py --sync <笔记路径> --no-cards-reason "主要是学习计划"
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[1]
REFERENCES_DIR = PROJECT_DIR / "references"
DEFAULT_RECORDS_DIR = PROJECT_DIR / "anki" / "records"
DEFAULT_VAULT_ROOT = Path(r"C:\obsidian")
DEFAULT_ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")

DEFAULT_DECK = "Obsidian::未分类"
DEFAULT_BASIC_MODEL = "Obsidian-basic"
DEFAULT_REVERSE_MODEL = "Obsidian-basic-reversed"
DEFAULT_CLOZE_MODEL = "Obsidian-cloze"
VALID_CARD_TYPES = {"basic", "cloze", "reverse", "problem"}

EXPECTED_JSON = {
    "should_create_cards": True,
    "reason": "为什么这篇笔记值得或不值得制卡",
    "cards": [
        {
            "type": "basic | cloze | reverse | problem",
            "deck": "数学::线性代数",
            "front": "非 cloze 卡的问题；cloze 卡留空",
            "back": "答案；cloze 卡可作为补充解释",
            "text": "仅 cloze 卡使用，必须包含 {{c1::...}}",
            "reason": "这张卡的制卡理由",
            "tags": ["数学", "线性代数"],
        }
    ],
}


# ── 通用工具 ──────────────────────────────────────────────────────────────────

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def batch_id(ts: dt.datetime) -> str:
    return ts.strftime("%Y%m%d%H%M%S")


def clean_note(text: str, max_chars: int) -> tuple[str, bool]:
    """去掉明显噪声，保留正文和 PDF 注释中的原文内容。"""
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[\[.+?\.pdf[^\]]*\]\]\n?", "", text)
    text = re.sub(r"^> \[!.*?\].*\n", "", text, flags=re.MULTILINE)
    text = re.sub(r"^> ", "", text, flags=re.MULTILINE)
    text = re.sub(r"- \[.\] ", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[内容过长，脚本已截断到 max-chars 限制。]"
        truncated = True
    return text, truncated


def resolve_existing_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise ValueError(f"文件不存在：{value}")
    if not path.is_file():
        raise ValueError(f"不是文件：{value}")
    if path.suffix.lower() != ".md":
        raise ValueError(f"不是 md 文件：{value}")
    return path.resolve()


def safe_record_stem(source_note: str) -> str:
    if source_note.lower().endswith(".md"):
        source_note = source_note[:-3]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "__", source_note)
    value = re.sub(r"__+", "__", value).strip(" ._")
    return value or "note"


def source_note_path(note_path: Path, vault_root: Path) -> str:
    note_resolved = note_path.resolve()
    vault_resolved = vault_root.resolve()
    try:
        return note_resolved.relative_to(vault_resolved).as_posix()
    except ValueError:
        return str(note_resolved)


def record_path_for(note_path: Path, vault_root: Path, records_dir: Path) -> Path:
    source_note = source_note_path(note_path, vault_root)
    return records_dir / f"{safe_record_stem(source_note)}.json"


def source_link_for(note_path: Path) -> str:
    return f"[[{note_path.stem}]]"


def obsidian_url(vault_name: str, source_note: str) -> str:
    file_path = source_note.replace("\\", "/")
    if file_path.lower().endswith(".md"):
        file_path = file_path[:-3]
    return (
        "obsidian://open?vault="
        + urllib.parse.quote(vault_name, safe="")
        + "&file="
        + urllib.parse.quote(file_path, safe="")
    )


def sanitize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#")
    tag = re.sub(r"\s+", "_", tag)
    tag = re.sub(r"[,;，；]+", "_", tag)
    return tag.strip("_")


def html_text(text: str) -> str:
    return html.escape(text.strip()).replace("\n", "<br>")


# ── 文件收集与 reference ─────────────────────────────────────────────────────

def collect_notes(args: argparse.Namespace) -> list[Path]:
    values: list[str] = []
    values.extend(args.note or [])
    values.extend(args.notes or [])

    paths: list[Path] = []
    for value in values:
        paths.append(resolve_existing_file(value))

    for dir_value in args.dir or []:
        directory = Path(dir_value).expanduser()
        if not directory.exists():
            raise ValueError(f"目录不存在：{dir_value}")
        if not directory.is_dir():
            raise ValueError(f"不是目录：{dir_value}")
        iterator = directory.rglob("*.md") if args.recursive else directory.glob("*.md")
        paths.extend(path.resolve() for path in iterator if path.is_file())

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return sorted(deduped, key=lambda p: str(p).casefold())


def load_references() -> dict[str, str]:
    return {
        "selection": read_text(REFERENCES_DIR / "anki-selection-rules.md"),
        "format": read_text(REFERENCES_DIR / "anki-card-format.md"),
        "vault": read_text(REFERENCES_DIR / "vault-structure.md"),
    }


def pending_note_rows(notes: list[Path], args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = []
    for note_path in notes:
        record_path = record_path_for(note_path, args.vault_root, args.records_dir)
        exists = record_path.exists()
        if exists and not args.force:
            status = "skipped"
        else:
            status = "pending"
        rows.append(
            {
                "status": status,
                "note": str(note_path),
                "source_note": source_note_path(note_path, args.vault_root),
                "record": str(record_path),
                "record_exists": exists,
            }
        )
    return rows


# ── AnkiConnect ──────────────────────────────────────────────────────────────

def post_json(url: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"返回内容不是 JSON：{body[:500]}") from e


def anki_request(anki_url: str, action: str, params: dict[str, Any] | None = None, timeout: int = 15) -> Any:
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    result = post_json(anki_url, payload, timeout=timeout)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result.get("result")


def open_anki() -> None:
    try:
        subprocess.Popen("start Anki", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def wait_for_anki(anki_url: str, wait_seconds: int) -> int:
    deadline = time.time() + wait_seconds
    anki_launched = False
    while True:
        try:
            version = anki_request(anki_url, "version", timeout=5)
            print(f"AnkiConnect OK: version {version}")
            return int(version)
        except RuntimeError as e:
            if time.time() >= deadline:
                raise RuntimeError(
                    f"未检测到 AnkiConnect：{e}\n"
                    "请打开 Anki，并确认 AnkiConnect 插件已启用。"
                ) from e
            if not anki_launched:
                print("AnkiConnect 未响应，尝试启动 Anki...")
                open_anki()
                anki_launched = True
            else:
                print("等待 AnkiConnect 上线...")
            time.sleep(3)


# ── 卡片 JSON 处理 ───────────────────────────────────────────────────────────

def normalize_json_quotes(text: str) -> str:
    """把中文弯引号替换为 JSON 兼容的直引号。"""
    return text.replace("“", '"').replace("”", '"')


def load_cards_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.cards_json:
        path = Path(args.cards_json).expanduser()
        return json.loads(normalize_json_quotes(read_text(path)))
    if args.cards:
        return json.loads(normalize_json_quotes(args.cards))
    if args.cards_stdin:
        return json.loads(normalize_json_quotes(sys.stdin.read()))
    if args.no_cards_reason:
        return {"should_create_cards": False, "reason": args.no_cards_reason, "cards": []}
    raise ValueError("同步时需要 --cards-json、--cards、--cards-stdin 或 --no-cards-reason")


def normalize_cards(result: dict[str, Any], max_cards: int) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    cards: list[dict[str, Any]] = []

    for index, raw_card in enumerate(result.get("cards", []), start=1):
        if not isinstance(raw_card, dict):
            warnings.append(f"第 {index} 张卡不是对象，已跳过")
            continue

        card_type = str(raw_card.get("type", "")).strip().lower()
        if card_type not in VALID_CARD_TYPES:
            warnings.append(f"第 {index} 张卡类型无效：{card_type or '空'}，已跳过")
            continue

        deck = str(raw_card.get("deck", "")).strip() or DEFAULT_DECK
        front = str(raw_card.get("front", "")).strip()
        back = str(raw_card.get("back", "")).strip()
        text = str(raw_card.get("text", "")).strip()
        reason = str(raw_card.get("reason", "")).strip() or "未说明"
        tags = raw_card.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [tag for tag in (sanitize_tag(str(tag)) for tag in tags) if tag]

        if card_type == "cloze":
            if not text or "{{c" not in text:
                warnings.append(f"第 {index} 张 cloze 卡缺少有效挖空，已跳过")
                continue
        elif not front or not back:
            warnings.append(f"第 {index} 张 {card_type} 卡缺少 front/back，已跳过")
            continue

        cards.append(
            {
                "type": card_type,
                "deck": deck,
                "front": front,
                "back": back,
                "text": text,
                "reason": reason,
                "tags": tags,
            }
        )

    if max_cards > 0 and len(cards) > max_cards:
        warnings.append(f"输入包含 {len(cards)} 张卡，已截断为前 {max_cards} 张")
        cards = cards[:max_cards]

    return cards, warnings


def no_cards_record(
    source_note: str,
    source_link: str,
    generated_at: dt.datetime,
    reason: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "source_note": source_note,
        "source_link": source_link,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "generator": "agent",
        "status": "no_cards",
        "reason": reason,
        "warnings": warnings or [],
        "cards": [],
    }


# ── Anki 同步 ─────────────────────────────────────────────────────────────────

def source_footer(source_link: str, source_url: str, reason: str, local_id: str) -> str:
    lines = [
        f'来源：<a href="{html.escape(source_url, quote=True)}">{html.escape(source_link)}</a>',
        f"原因：{html.escape(reason)}",
        f"Local ID：{html.escape(local_id)}",
    ]
    return '<hr><div style="font-size:0.85em;color:#666;">' + "<br>".join(lines) + "</div>"


def build_anki_note(
    card: dict[str, Any],
    local_id: str,
    source_link: str,
    source_url: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    card_type = card["type"]
    footer = source_footer(source_link, source_url, card["reason"], local_id)
    tags = ["obsidian", "ai_generated", *card["tags"]]

    if card_type == "cloze":
        model_name = args.cloze_model
        extra = html_text(card["back"])
        fields = {
            "Text": html_text(card["text"]),
            "Extra": (extra + footer) if extra else footer,
        }
    else:
        model_name = args.reverse_model if card_type == "reverse" else args.basic_model
        fields = {
            "Front": html_text(card["front"]),
            "Back": html_text(card["back"]) + footer,
        }

    return {
        "deckName": card["deck"],
        "modelName": model_name,
        "fields": fields,
        "options": {"allowDuplicate": False},
        "tags": tags,
    }


def sync_cards_to_anki(
    cards: list[dict[str, Any]],
    source_link: str,
    source_url: str,
    started_at: dt.datetime,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    failed = 0
    id_prefix = batch_id(started_at)

    for index, card in enumerate(cards, start=1):
        local_id = f"{id_prefix}-{index:03d}"
        card_record = {"local_id": local_id, **card}
        try:
            anki_request(args.anki_url, "createDeck", {"deck": card["deck"]})
            note = build_anki_note(card, local_id, source_link, source_url, args)
            note_id = anki_request(args.anki_url, "addNote", {"note": note}, timeout=30)
            card_record["anki_note_id"] = note_id
            card_record["status"] = "synced"
            print(f"  OK {local_id} → Anki note {note_id}")
        except RuntimeError as e:
            failed += 1
            card_record["anki_note_id"] = None
            card_record["status"] = "failed"
            card_record["error"] = str(e)
            print(f"  FAIL {local_id}: {e}")
        records.append(card_record)

    if failed == 0:
        return records, "synced"
    if failed == len(records):
        return records, "failed"
    return records, "partial_failed"


# ── 模式实现 ─────────────────────────────────────────────────────────────────

def run_list(args: argparse.Namespace) -> None:
    notes = collect_notes(args)
    rows = pending_note_rows(notes, args)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    pending = [row for row in rows if row["status"] == "pending"]
    skipped = [row for row in rows if row["status"] == "skipped"]
    print(f"待处理 {len(pending)} 个，跳过 {len(skipped)} 个。")
    for row in pending:
        print(row["note"])


def run_read(args: argparse.Namespace) -> None:
    note_path = resolve_existing_file(args.read)
    record_path = record_path_for(note_path, args.vault_root, args.records_dir)
    references = load_references()
    source_note = source_note_path(note_path, args.vault_root)
    source_link = source_link_for(note_path)
    note_text, truncated = clean_note(read_text(note_path), args.max_chars)

    package = {
        "note": str(note_path),
        "source_note": source_note,
        "source_link": source_link,
        "record": str(record_path),
        "record_exists": record_path.exists(),
        "truncated": truncated,
        "expected_json": EXPECTED_JSON,
        "references": references,
        "note_text": note_text,
    }
    if args.json:
        print(json.dumps(package, ensure_ascii=False, indent=2))
        return

    print("# Anki 制卡上下文")
    print()
    print("请根据 references 判断是否值得制卡，并只输出符合 expected_json 结构的 JSON。")
    print("records JSON 是脚本状态，不要把旧 records 传入判断；原 md 不会被修改。")
    print()
    print("## 元数据")
    print(f"- note: {note_path}")
    print(f"- source_note: {source_note}")
    print(f"- source_link: {source_link}")
    print(f"- record: {record_path}")
    print(f"- record_exists: {record_path.exists()}")
    print(f"- truncated: {truncated}")
    print()
    print("## expected_json")
    print("```json")
    print(json.dumps(EXPECTED_JSON, ensure_ascii=False, indent=2))
    print("```")
    print()
    print("## reference: anki-selection-rules.md")
    print(references["selection"])
    print()
    print("## reference: anki-card-format.md")
    print(references["format"])
    print()
    print("## reference: vault-structure.md")
    print(references["vault"])
    print()
    print("## 当前笔记内容")
    print(note_text)


def run_sync(args: argparse.Namespace) -> int:
    note_path = resolve_existing_file(args.sync)
    record_path = record_path_for(note_path, args.vault_root, args.records_dir)
    if record_path.exists() and not args.force:
        print(f"跳过（已有记录）：{record_path}")
        return 0

    started_at = now_local()
    source_note = source_note_path(note_path, args.vault_root)
    source_link = source_link_for(note_path)
    source_url = obsidian_url(args.vault_name, source_note)

    try:
        payload = load_cards_payload(args)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: 读取卡片 JSON 失败：{e}", file=sys.stderr)
        return 1

    cards, warnings = normalize_cards(payload, args.max_cards)
    reason = str(payload.get("reason", "")).strip() or "未说明"
    for warning in warnings:
        print(f"  WARN {warning}")

    if not cards:
        record = no_cards_record(source_note, source_link, started_at, reason, warnings)
        if args.dry_run:
            print(json.dumps(record, ensure_ascii=False, indent=2))
        else:
            write_json(record_path, record)
            print(f"no_cards record: {record_path}")
        return 0

    if args.dry_run:
        card_records = [
            {"local_id": f"{batch_id(started_at)}-{index:03d}", "status": "dry_run", **card}
            for index, card in enumerate(cards, start=1)
        ]
        status = "dry_run"
    else:
        try:
            wait_for_anki(args.anki_url, max(args.wait_seconds, 0))
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        card_records, status = sync_cards_to_anki(cards, source_link, source_url, started_at, args)

    record = {
        "source_note": source_note,
        "source_link": source_link,
        "source_url": source_url,
        "generated_at": started_at.isoformat(timespec="seconds"),
        "generator": "agent",
        "status": status,
        "reason": reason,
        "warnings": warnings,
        "cards": card_records,
    }

    if args.dry_run:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        write_json(record_path, record)
        print(f"record: {record_path}")

    return 2 if status in {"failed", "partial_failed"} else 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Obsidian md 笔记同步 Anki 卡片（Claude/Codex 驱动）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="列出待处理 md 文件")
    mode.add_argument("--read", metavar="NOTE", help="输出单篇笔记的制卡上下文，供外层 agent 使用")
    mode.add_argument("--sync", metavar="NOTE", help="同步外层 agent 生成的卡片 JSON 到 Anki")

    parser.add_argument("--note", action="append", default=[], help="配合 --list 使用：单个笔记路径；可重复传入")
    parser.add_argument("--notes", nargs="+", default=[], help="配合 --list 使用：多个笔记路径")
    parser.add_argument("--dir", action="append", default=[], help="配合 --list 使用：目录路径；可重复传入")
    parser.add_argument("--recursive", action="store_true", help="配合 --dir 递归处理子目录")
    parser.add_argument("--force", action="store_true", help="已有 records JSON 时仍视为待处理/允许覆盖")
    parser.add_argument("--json", action="store_true", help="--list 或 --read 输出 JSON")
    parser.add_argument("--dry-run", action="store_true", help="--sync 时预览 records，不写入也不调用 AnkiConnect")

    parser.add_argument("--cards-json", help="--sync 使用：Claude/Codex 生成的卡片 JSON 文件")
    parser.add_argument("--cards", help="--sync 使用：直接传入卡片 JSON 字符串")
    parser.add_argument("--cards-stdin", action="store_true", help="--sync 使用：从 stdin 读取卡片 JSON")
    parser.add_argument("--no-cards-reason", help="--sync 使用：记录无需制卡的原因")

    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR, help="records JSON 输出目录")
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT, help="Obsidian vault 根目录")
    parser.add_argument("--vault-name", default=None, help="obsidian:// 链接使用的 vault 名称，默认取 vault-root 文件夹名")
    parser.add_argument("--anki-url", default=DEFAULT_ANKI_URL, help="AnkiConnect URL")
    parser.add_argument("--wait-seconds", type=int, default=60, help="等待 AnkiConnect 的秒数")
    parser.add_argument("--max-chars", type=int, default=60000, help="--read 时输出的最大笔记字符数，0 表示不限制")
    parser.add_argument("--max-cards", type=int, default=15, help="--sync 时单篇笔记最多同步的卡片数，0 表示不限制")
    parser.add_argument("--basic-model", default=DEFAULT_BASIC_MODEL, help="Anki basic/problem 使用的 note type")
    parser.add_argument("--reverse-model", default=DEFAULT_REVERSE_MODEL, help="Anki reverse 使用的 note type")
    parser.add_argument("--cloze-model", default=DEFAULT_CLOZE_MODEL, help="Anki cloze 使用的 note type")
    args = parser.parse_args()

    args.records_dir = args.records_dir.expanduser().resolve()
    args.vault_root = args.vault_root.expanduser().resolve()
    if not args.vault_name:
        args.vault_name = args.vault_root.name
    return args


def main() -> None:
    args = parse_args()

    try:
        if args.list:
            run_list(args)
            return
        if args.read:
            run_read(args)
            return
        if args.sync:
            sys.exit(run_sync(args))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: 文件读写失败：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
