#!/usr/bin/env python3
"""把文档审计结论应用到文件 —— 整行匹配，绝不半途替换。

## 为什么单独写这个

2026-08-19 第一轮应用时用的是"拿引文去找，找不到就把引文截短一点再找，
直到唯一命中"。听起来稳妥，实际后果是：**只替换了前缀，旧句子的尾巴留在原地**。
同一行里于是并排放着两个版本，而且互相矛盾 —— 比修之前更糟，因为它看起来像
是有人特意写的。8 处，全是手工挑出来重写的。

病根不是正则写得不好，是那个 fallback 本身：找不到就悄悄降级成"改一半"，
不出声。这跟 `references/silent-failure-lessons.md` 记的十处是同一个选择。

所以这版只有两种结局：**整行换掉**，或者**报出来不动**。没有中间态。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _apply_one(lines: list[str], anchor: str, new: str) -> tuple[bool, str]:
    """→ (是否改了, 说明)。anchor 必须在文件里**唯一整行命中**。"""

    stripped = anchor.rstrip("\n")
    hits = [i for i, line in enumerate(lines) if line == stripped]
    if not hits:
        # 退一步：允许尾部空白差异，但仍要求整行等价 —— 不做子串匹配
        hits = [i for i, line in enumerate(lines) if line.rstrip() == stripped.rstrip()]
    if not hits:
        return False, "锚定行不存在（agent 多半是凭记忆重写了原文）"
    if len(hits) > 1:
        return False, f"锚定行出现 {len(hits)} 次，不唯一"
    lines[hits[0]] = new.rstrip("\n")
    return True, f"第 {hits[0] + 1} 行"


def _sanity(before: str, after: str, new: str) -> str | None:
    """替换后的自检。返回问题描述，没问题返回 None。"""

    if before.strip().startswith("|") and not after.strip().endswith("|"):
        return "原行是表格行，新行没有以 | 收尾"
    if before.lstrip().startswith("- ") and not after.lstrip().startswith(("- ", "* ", ">")):
        return "原行是列表项，新行丢了列表记号"
    # 新行里若把同一个长片段写了两遍,基本可以断定是拼接时把旧文本带进来了
    for width in (40, 30):
        for i in range(0, max(0, len(new) - width), 7):
            frag = new[i : i + width]
            if frag.count(" ") < width // 2 and new.count(frag) > 1:
                return f"新行内部出现重复片段：{frag[:30]!r}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("findings", help="findings JSON（含 anchorLine / suggestedLine）")
    parser.add_argument("--roots", nargs="*", default=[str(ROOT)],
                        help="要改的仓库根，可给多个（主仓 + worktree）")
    parser.add_argument("--severity", nargs="*", default=None,
                        help="只应用这些等级，默认全部")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = json.loads(Path(args.findings).read_text("utf-8"))
    items = data["findings"] if isinstance(data, dict) else data
    if args.severity:
        items = [f for f in items if f.get("severity") in args.severity]

    by_file: dict[str, list[dict]] = {}
    for f in items:
        by_file.setdefault(f["file"], []).append(f)

    ok = skipped = 0
    problems: list[str] = []
    for root_str in args.roots:
        root = Path(root_str)
        first = root_str == args.roots[0]
        for rel, group in sorted(by_file.items()):
            path = root / rel
            if not path.exists():
                continue
            lines = path.read_text("utf-8").splitlines()
            changed = 0
            for f in group:
                anchor, new = f["anchorLine"], f["suggestedLine"]
                if anchor.rstrip() == new.rstrip():
                    continue
                done, note = _apply_one(lines, anchor, new)
                if not done:
                    if first:
                        skipped += 1
                        problems.append(f"  ✗ {rel}  {note}\n      {anchor[:70]}")
                    continue
                bad = _sanity(anchor, new, new)
                if bad:
                    # 自检不过就**退回去**：宁可这条不改,也不要留下一行坏文本
                    idx = next(i for i, l in enumerate(lines) if l == new.rstrip("\n"))
                    lines[idx] = anchor.rstrip("\n")
                    if first:
                        skipped += 1
                        problems.append(f"  ✗ {rel}  自检不过：{bad}")
                    continue
                changed += 1
                if first:
                    ok += 1
            if changed and not args.dry_run:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            if first and changed:
                print(f"  ✓ {rel}: {changed} 行")

    if problems:
        print("\n需要手工处理的：")
        print("\n".join(problems))
    print(f"\n应用 {ok} 条，跳过 {skipped} 条" + ("（dry-run，未写盘）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
