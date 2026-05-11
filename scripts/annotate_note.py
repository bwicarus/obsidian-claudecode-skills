"""
笔记 PDF 标注脚本

扫描指定笔记中所有 Obsidian PDF 链接（![[file.pdf#page=N&rect=...]]），
提取每个区域的文本内容，并将其插入到对应链接的正下方。

标注格式为 HTML 注释（<!-- 原文 ... -->），阅读模式不显示，源码模式可见。
已提取的链接会被跳过（检测到后方紧邻的 <!-- 或 > [!quote] 块时，可隔空行）。

用法：
  python annotate_note.py --note <笔记路径>
  python annotate_note.py --note <笔记路径> --dry-run    # 只预览，不写入
  python annotate_note.py --note <笔记路径> --migrate    # 将旧版 > [!quote] 块转为 HTML 注释
"""

import argparse
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_SCRIPT = os.path.join(SCRIPT_DIR, "pdf_extract.py")
PYTHON = sys.executable

PDF_LINK_RE = re.compile(r'(!\[\[[^\]]+\.pdf#[^\]]+\]\])')
ALREADY_ANNOTATED_RE = re.compile(r'^\s*(<!--\s*原文|>\s*\[!quote\])')


def extract_region(link):
    """调用 pdf_extract.py 提取链接对应区域，返回文本（不含前缀）。"""
    result = subprocess.run(
        [PYTHON, EXTRACT_SCRIPT, "--link", link],
        capture_output=True, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = result.stdout.strip()
    if not output or result.returncode != 0:
        err = result.stderr.strip()
        return None, err
    # 去掉 TEXT: / OCR: / VISION: 前缀
    for prefix in ("TEXT:", "OCR:", "VISION:"):
        if output.startswith(prefix):
            return output[len(prefix):].strip(), None
    return output, None


def build_callout(text):
    """将提取的文本包装为 HTML 注释块（阅读模式隐藏，源码模式可见）。"""
    return f"<!-- 原文\n{text}\n-->"


def next_nonblank_line(lines, start):
    for i in range(start, len(lines)):
        if lines[i].strip():
            return lines[i]
    return ""


def process_note(note_path, dry_run=False):
    with open(note_path, encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    changed = 0

    while i < len(lines):
        line = lines[i]
        match = PDF_LINK_RE.search(line)

        if match:
            # 检查后方是否已经是 callout（允许中间有空行，避免重复插入）
            next_line = next_nonblank_line(lines, i + 1)
            if ALREADY_ANNOTATED_RE.match(next_line):
                new_lines.append(line)
                i += 1
                continue

            link = match.group(1)
            print(f"  提取: {link[:60]}...", end=" ", flush=True)
            text, err = extract_region(link)

            if err or not text:
                print(f"失败（{err or '空内容'}）")
                new_lines.append(line)
            else:
                print(f"OK（{len(text)} 字符）")
                callout = build_callout(text)
                new_lines.append(line)
                new_lines.append("\n")
                new_lines.append(callout + "\n\n")
                changed += 1
        else:
            new_lines.append(line)

        i += 1

    print(f"\n共处理 {changed} 个链接。")

    if changed == 0:
        print("无变更。")
        return

    if dry_run:
        print("\n--- 预览（dry-run，不写入）---")
        print("".join(new_lines))
    else:
        with open(note_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"已写入：{note_path}")


def migrate_callouts(note_path, dry_run=False):
    """将笔记中旧版 > [!quote] 原文 块转换为 HTML 注释格式。"""
    with open(note_path, encoding="utf-8") as f:
        content = f.read()

    def replace_callout(m):
        block = m.group(1)
        lines = block.splitlines()
        # 第一行是 "> [!quote] 原文"，去掉；其余行去掉 "> " 前缀
        stripped = []
        for line in lines[1:]:
            stripped.append(re.sub(r'^>\s?', '', line))
        return "\n<!-- 原文\n" + "\n".join(stripped) + "\n-->\n\n"

    # 匹配 > [!quote] 原文 开头、到下一个非 > 行（或文件末尾）之间的块
    new_content, count = re.subn(
        r'(> \[!quote\] 原文\n(?:>.*\n?)*)',
        replace_callout,
        content
    )

    if count == 0:
        print("未找到旧版 callout 块。")
        return

    print(f"找到 {count} 个旧版块。")
    if dry_run:
        print("\n--- 预览（dry-run，不写入）---")
        print(new_content)
    else:
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"已转换并写入：{note_path}")


def main():
    parser = argparse.ArgumentParser(description="批量提取笔记中的 PDF 标注内容")
    parser.add_argument("--note", required=True, help="笔记文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入文件")
    parser.add_argument("--migrate", action="store_true", help="将旧版 > [!quote] 块转为 HTML 注释")
    args = parser.parse_args()

    if not os.path.exists(args.note):
        print(f"ERROR: 文件不存在：{args.note}", file=sys.stderr)
        sys.exit(1)

    print(f"处理笔记：{args.note}\n")
    if args.migrate:
        migrate_callouts(args.note, dry_run=args.dry_run)
    else:
        process_note(args.note, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
