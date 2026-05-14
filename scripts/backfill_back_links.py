"""
backfill_back_links.py — 一次性脚本：扫描所有已有「相关笔记」节的笔记，
对其每条正向链接，向目标笔记追加对应的反向链接（已存在则跳过）。

适用场景：
  - 第一次启用单向连接 + 反向传播策略时，对存量笔记补全反向链接
  - 怀疑反向链接缺失时手动重跑

输出每篇笔记新增的反向链接数。运行时不修改 state/note-states.json，
不依赖 connect 记录，纯按文件「相关笔记」节做传播。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import register_notes  # noqa: E402

VAULT = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))
PATTERN = re.compile(r"^[0-9A-Fa-f]{3}-.+\.md$")


def main() -> int:
    total = touched = added_total = 0
    for md in sorted(VAULT.rglob("*.md")):
        if not PATTERN.match(md.name):
            continue
        total += 1
        forward = register_notes.parse_existing_links(md.read_text(encoding="utf-8", errors="replace"))
        if not forward:
            continue
        added = register_notes.propagate_back_links(md)
        if added:
            print(f"  {md.name}: +{added}")
            touched += 1
            added_total += added
    print()
    print(f"扫描笔记 {total} 篇，{touched} 篇有新增反向链接，共追加 {added_total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
