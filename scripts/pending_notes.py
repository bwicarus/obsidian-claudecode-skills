"""
pending_notes.py — 找出需要被 /登记新笔记 处理的笔记

扫描范围：vault 根目录下符合 [0-9A-Fa-f]{3}-*.md 命名规则的文件
输出：
  新笔记   — 从未登记过，且 mtime 晚于上次运行时间
  已修改   — 登记过但内容哈希已变化
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))
import note_state
from config import VAULT_ROOT

SKILL = "summarize"
NOTE_PATTERN = re.compile(r"^[0-9A-Fa-f]{3}-.+\.md$")


def main():
    last_scan = note_state.get_last_scan("登记新笔记")

    new_notes = []
    modified_notes = []

    for md_file in sorted(VAULT_ROOT.glob("**/*.md")):
        if not NOTE_PATTERN.match(md_file.name):
            continue
        # v3-C:自动概念笔记(frontmatter type: concept-auto)不进登记管线——
        # 机器写的不算"你的笔记",不该被自动摘要/关联/Anki 制卡(污染环);用户亲手编辑后
        # (status→user-edited 且移除该标记)才升格。只读前 200 字节,开销可忽略。
        try:
            head = md_file.read_text("utf-8", errors="ignore")[:200]
            if "type: concept-auto" in head:
                continue
        except OSError:
            pass

        if note_state.has_record(md_file, SKILL):
            if not note_state.is_unchanged(md_file, SKILL):
                modified_notes.append(md_file)
        else:
            if last_scan is None or md_file.stat().st_mtime > last_scan.timestamp():
                new_notes.append(md_file)

    _print_section("新笔记", new_notes)
    _print_section("已修改", modified_notes)

    total = len(new_notes) + len(modified_notes)
    print(f"\n共需处理 {total} 篇")
    if last_scan:
        print(f"（上次登记：{last_scan.strftime('%Y-%m-%d %H:%M:%S')}）")
    else:
        print("（尚无登记记录，显示所有符合命名规则的笔记）")


def _print_section(title: str, files: list) -> None:
    print(f"\n{title}（{len(files)} 篇）")
    for f in files:
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
