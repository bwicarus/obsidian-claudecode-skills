"""
annotate_images.py — 图片标注的预处理和写回工具（配合 Claude Code CLI 使用）。

两阶段工作流：
    1. scan:  解析笔记 → 找图片链接 → 按 ## 分段 → 解析文件路径 → 输出 JSON
    2. apply: 接收 Claude Code 生成的分组描述 JSON → 写回笔记

用法:
    python annotate_images.py scan --note <笔记路径>
    python annotate_images.py apply --note <笔记路径> --result <JSON文件路径>
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# subprocess 调用时 stdout 是 pipe，Windows 默认会用 cp936，
# 输出含中文 heading 的 JSON 会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))
# 两种图片语法都识别：
#   1. Obsidian wikilink:  ![[file.png]]
#   2. 标准 markdown:      ![alt](path/to/file.png)   或 ![](file.png)
_IMG_EXTS = r"(?:png|jpg|jpeg|gif|bmp|webp|svg)"
WIKILINK_IMAGE_RE = re.compile(
    rf"!\[\[([^\]]+\.{_IMG_EXTS})\]\]", re.IGNORECASE
)
MARKDOWN_IMAGE_RE = re.compile(
    rf"!\[[^\]]*\]\(([^)\s]+\.{_IMG_EXTS})(?:\s+\"[^\"]*\")?\)", re.IGNORECASE
)
HEADING_RE = re.compile(r"^(#{1,6}\s+.+)", re.MULTILINE)
ANNOTATED_RE = re.compile(r"<!--\s*(图片描述|已标注)")


def find_image_file(name: str, note_path: Path | None = None) -> Path | None:
    """支持三种来源：
       1. 绝对路径（含盘符）→ 直接用
       2. 相对路径（含 / 或 \\）→ 相对笔记 / vault 根解析
       3. 仅文件名 → 全 vault 搜（兼容 Obsidian wikilink 行为）
    """
    if not name:
        return None
    p = Path(name)
    if p.is_absolute() and p.exists() and p.is_file():
        return p
    # 相对路径
    if "/" in name or "\\" in name:
        candidates = []
        if note_path is not None:
            candidates.append(note_path.parent / name)
        candidates.append(VAULT_ROOT / name)
        for c in candidates:
            if c.exists() and c.is_file():
                return c.resolve()
        # 路径不对就退回到只用文件名匹配
        name = p.name
    # 仅文件名：全 vault 扫
    name_lower = name.lower()
    for q in VAULT_ROOT.rglob("*"):
        if q.is_file() and q.name.lower() == name_lower:
            return q
    return None


def split_sections(text: str) -> list[dict]:
    """按 ## 标题分段，返回 [{"heading": str|null, "start": int, "end": int}]。"""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [{"heading": None, "start": 0, "end": len(text)}]
    sections = []
    if matches[0].start() > 0:
        sections.append({"heading": None, "start": 0, "end": matches[0].start()})
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append({"heading": m.group(1).strip(), "start": m.start(), "end": end})
    return sections


def find_images_in_section(text: str, sec_start: int, sec_end: int) -> list[dict]:
    section_text = text[sec_start:sec_end]
    images: list[dict] = []
    seen_pos: set[int] = set()
    for pattern in (WIKILINK_IMAGE_RE, MARKDOWN_IMAGE_RE):
        for m in pattern.finditer(section_text):
            abs_pos = sec_start + m.start()
            if abs_pos in seen_pos:
                continue
            seen_pos.add(abs_pos)
            link_end = sec_start + m.end()
            # 「已标注」检测：吞噬紧跟的多种空白（含 0 个换行：用户没留空行也能识别）
            after_link = text[link_end:link_end + 200]
            stripped = after_link.lstrip("\r\n \t")
            if stripped.startswith("<!--") and ANNOTATED_RE.match(stripped):
                continue
            images.append({
                "filename": m.group(1),
                "abs_pos": abs_pos,
                "abs_end": link_end,
            })
    images.sort(key=lambda x: x["abs_pos"])
    return images


def cmd_scan(args):
    note_path = Path(args.note)
    if not note_path.exists():
        print(f"错误：笔记文件不存在: {note_path}", file=sys.stderr)
        sys.exit(1)

    text = note_path.read_text(encoding="utf-8")
    sections = split_sections(text)

    output_sections = []
    for sec in sections:
        images = find_images_in_section(text, sec["start"], sec["end"])
        if not images:
            continue

        resolved_images = []
        for img in images:
            path = find_image_file(img["filename"], note_path=note_path)
            if path is None:
                print(f"警告：找不到图片文件 {img['filename']}，跳过", file=sys.stderr)
                continue
            resolved_images.append({
                "index": len(resolved_images),
                "filename": img["filename"],
                "path": str(path),
                "abs_end": img["abs_end"],
            })

        if resolved_images:
            output_sections.append({
                "heading": sec["heading"],
                "images": resolved_images,
            })

    if not output_sections:
        print("没有需要标注的图片")
        sys.exit(0)

    result = {"note": str(note_path), "sections": output_sections}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_apply(args):
    note_path = Path(args.note)
    if not note_path.exists():
        print(f"错误：笔记文件不存在: {note_path}", file=sys.stderr)
        sys.exit(1)

    result_path = Path(args.result)
    if not result_path.exists():
        print(f"错误：结果文件不存在: {result_path}", file=sys.stderr)
        sys.exit(1)

    text = note_path.read_text(encoding="utf-8")
    result_data = json.loads(result_path.read_text(encoding="utf-8"))

    # result_data 格式:
    # {"sections": [{"images": [...], "groups": [{"indices": [0,1], "description": "..."}]}]}

    insertions = []

    for section in result_data["sections"]:
        images = section["images"]
        groups = section["groups"]

        for group in groups:
            indices = sorted(group["indices"])
            description = group["description"]

            if len(indices) == 1:
                img = images[indices[0]]
                annotation = f"\n\n<!-- 图片描述\n{description}\n-->"
                insertions.append((img["abs_end"], annotation))
            else:
                filenames = [images[i]["filename"] for i in indices]
                header = "[" + ", ".join(filenames) + "]"
                full_annotation = f"\n\n<!-- 图片描述\n{header}\n{description}\n-->"
                for idx in indices[:-1]:
                    img = images[idx]
                    insertions.append((img["abs_end"], "\n\n<!-- 已标注 -->"))
                last_img = images[indices[-1]]
                insertions.append((last_img["abs_end"], full_annotation))

    insertions.sort(key=lambda x: x[0], reverse=True)
    for pos, annotation in insertions:
        text = text[:pos] + annotation + text[pos:]

    note_path.write_text(text, encoding="utf-8")
    total = sum(len(g["indices"]) for s in result_data["sections"] for g in s["groups"])
    print(f"完成：标注了 {total} 张图片")


def main():
    parser = argparse.ArgumentParser(description="图片标注预处理与写回工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="扫描笔记中待标注的图片")
    scan_parser.add_argument("--note", required=True, help="笔记文件路径")

    apply_parser = subparsers.add_parser("apply", help="将分组描述写回笔记")
    apply_parser.add_argument("--note", required=True, help="笔记文件路径")
    apply_parser.add_argument("--result", required=True, help="包含分组描述的 JSON 文件路径")

    args = parser.parse_args()
    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "apply":
        cmd_apply(args)


if __name__ == "__main__":
    main()
