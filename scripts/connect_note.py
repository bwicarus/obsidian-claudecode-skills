"""
Obsidian related-notes writer.

The agent decides which notes are related. This script only performs the
mechanical update of the target note's "相关笔记" section.

Usage:
  python connect_note.py --note <笔记路径> --link "文件名|关联原因"
  python connect_note.py --note <笔记路径> --link "[[文件名]]|关联原因" --dry-run
"""

import argparse
import os
import re
import sys
from pathlib import Path

import note_state

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SECTION_TITLE = "相关笔记"
MAX_REASON_LEN = 20

_LINK_LINE_RE = re.compile(r"-\s+\[\[([^\]]+)\]\]\s*[—–-]?\s*(.*)")


def normalize_note_name(value):
    value = value.strip()
    match = re.fullmatch(r"\[\[([^\]]+)\]\](?:\.md)?", value)
    if match:
        value = match.group(1).strip()
    if value.lower().endswith(".md"):
        value = value[:-3]
    return value


def parse_link(value):
    if "|" not in value:
        raise ValueError("关联项格式应为 '文件名|关联原因'")
    note_name, reason = value.split("|", 1)
    note_name = normalize_note_name(note_name)
    reason = reason.strip()
    if not note_name:
        raise ValueError("关联项缺少文件名")
    if not reason:
        raise ValueError(f"[[{note_name}]] 缺少关联原因")
    if len(reason) > MAX_REASON_LEN:
        raise ValueError(f"[[{note_name}]] 的关联原因超过 {MAX_REASON_LEN} 字")
    return note_name, reason


def dedupe_links(items):
    seen = set()
    result = []
    for note_name, reason in items:
        key = note_name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append((note_name, reason))
    return result


def build_section(items):
    lines = ["---\n", f"## {SECTION_TITLE}\n"]
    lines.extend(f"- [[{note_name}]] — {reason}\n" for note_name, reason in items)
    return lines


def find_related_section(lines):
    heading_re = re.compile(rf"^##\s+{re.escape(SECTION_TITLE)}\s*$")
    start = next((i for i, line in enumerate(lines) if heading_re.match(line.strip())), None)
    if start is None:
        return None, None

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^#{1,2}\s+", lines[i]):
            end = i
            break

    block_start = start
    prev = start - 1
    while prev >= 0 and lines[prev].strip() == "":
        prev -= 1
    if prev >= 0 and lines[prev].strip() == "---":
        block_start = prev
        while block_start > 0 and lines[block_start - 1].strip() == "":
            block_start -= 1

    return block_start, end


def parse_existing_items(text):
    """从文本中解析「相关笔记」节的现有条目，返回 [(name, reason)]。"""
    heading_re = re.compile(rf"\n##\s+{re.escape(SECTION_TITLE)}\s*\n")
    m = heading_re.search(text)
    if not m:
        return []
    section = text[m.end():]
    next_h = re.search(r"\n#{1,2}\s+", section)
    if next_h:
        section = section[:next_h.start()]
    entries = []
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        em = _LINK_LINE_RE.match(s)
        if em:
            name = em.group(1).strip()
            if name.lower().endswith(".md"):
                name = name[:-3]
            entries.append((name, em.group(2).strip()))
    return entries


def update_note(content, items, mode="merge"):
    """将 items 写入「相关笔记」节。

    mode='merge'   ：与已有条目合并去重（保留旧链接，新条目排在前）
    mode='replace' ：完全替换已有条目
    """
    if mode == "merge":
        existing = parse_existing_items(content)
        seen = set()
        merged = []
        for name, reason in list(items) + existing:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append((name, reason))
        items = merged

    lines = content.splitlines(keepends=True)
    section = build_section(items)
    start, end = find_related_section(lines)

    if start is None:
        new_lines = list(lines)
        while new_lines and new_lines[-1].strip() == "":
            new_lines.pop()
        if new_lines:
            new_lines.append("\n\n")
        new_lines.extend(section)
        return "".join(new_lines), "新增"

    new_lines = lines[:start]
    while new_lines and new_lines[-1].strip() == "":
        new_lines.pop()
    if new_lines:
        new_lines.append("\n\n")
    new_lines.extend(section)
    tail = lines[end:]
    if tail:
        new_lines.append("\n")
        new_lines.extend(tail)
    return "".join(new_lines), "更新"


def main():
    parser = argparse.ArgumentParser(description="更新笔记末尾的相关笔记链接")
    parser.add_argument("--note", required=True, help="笔记文件路径")
    parser.add_argument(
        "--link",
        action="append",
        default=[],
        metavar="文件名|原因",
        help="关联笔记项，可重复传入；文件名可写 [[文件名]]，原因不超过20字",
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入文件")
    args = parser.parse_args()

    if not os.path.exists(args.note):
        print(f"ERROR: 文件不存在：{args.note}", file=sys.stderr)
        sys.exit(1)
    if not args.link:
        print("ERROR: 至少需要一个 --link '文件名|关联原因'", file=sys.stderr)
        sys.exit(1)

    try:
        items = dedupe_links(parse_link(value) for value in args.link)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    with open(args.note, encoding="utf-8") as f:
        content = f.read()

    new_content, action = update_note(content, items)

    if args.dry_run:
        print("--- 预览（dry-run，不写入）---")
        print(new_content)
        return

    with open(args.note, "w", encoding="utf-8") as f:
        f.write(new_content)

    note_state.mark_processed(Path(args.note), "connect")
    print(f"OK: {action} {len(items)} 条相关笔记 → {args.note}")


if __name__ == "__main__":
    main()
