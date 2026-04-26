"""
笔记摘要辅助脚本（分层索引版）

索引结构：
  index/knowledge-index.md    主索引（科目列表 + 统计，始终保持轻量）
  index/{科目}.md             科目索引（各分支 + 条目）
  index/{科目}/{分支}.md      分支索引（条目超过阈值时自动拆分）

模式一：读取笔记，输出去噪后的纯文本供 AI 分析
  python summarize_note.py --read <笔记路径>

模式二：写入摘要到知识索引（自动路由到正确层级）
  python summarize_note.py --write <笔记路径> \
    --keywords "kw1, kw2" --summary "摘要" --category "数学/线性代数"
"""

import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

INDEX_DIR   = r"C:\obsidian\claude\index"
MASTER_FILE = os.path.join(INDEX_DIR, "knowledge-index.md")

# 科目文件中某个分支的条目数超过此值时，自动拆分为独立文件
BRANCH_SPLIT_THRESHOLD = 30

SUBJECTS = ["数学", "物理", "计算机科学", "英语", "日语"]


# ── 路径工具 ────────────────────────��─────────────────────────────────────────

def subject_file(subject):
    return os.path.join(INDEX_DIR, f"{subject}.md")

def branch_file(subject, branch):
    return os.path.join(INDEX_DIR, subject, f"{branch}.md")

def branch_is_split(subject, branch):
    return os.path.exists(branch_file(subject, branch))


# ── 读取模式 ─────────────────────────────────────────────────��────────────────

def clean_note(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, flags=re.DOTALL)
    text = re.sub(r'> \[!tip\].*?```\s*\n', '', text, flags=re.DOTALL)
    text = re.sub(r'!\[\[.+?\.pdf[^\]]*\]\]\n?', '', text)
    text = re.sub(r'^> \[!.*?\].*\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'^> ', '', text, flags=re.MULTILINE)
    text = re.sub(r'- \[.\] ', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


# ── 索引文件解析 ──────────────────────────────────────────────────────────────

def count_entries_in_file(path):
    """计算文件中条目（以 '- [[' 开头的行）的数量。"""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for l in f if l.startswith("- [["))

def count_branch_entries_in_subject(subject, branch):
    """统计科目文件中某个分支的条目数。"""
    sf = subject_file(subject)
    if not os.path.exists(sf):
        return 0
    with open(sf, encoding="utf-8") as f:
        lines = f.readlines()
    in_branch, count = False, 0
    for line in lines:
        if line.strip() == f"### {branch}":
            in_branch = True
            continue
        if in_branch:
            if re.match(r'^##', line):
                break
            if line.startswith("- [["):
                count += 1
    return count


# ── 写入工具 ────────────────────────────────────────────────���─────────────────

def _upsert_entry(lines, section_header, entry_line, header_level="###"):
    """
    在 lines 中找到 section_header 对应的节，插入或更新 entry_line。
    返回 (new_lines, action)，action 为 '新增' 或 '更新'。
    """
    note_name = re.search(r'\[\[([^\]]+)\]\]', entry_line).group(1)
    sec_start = sec_end = None

    for i, line in enumerate(lines):
        if line.strip() == f"{header_level} {section_header}":
            sec_start = i
        if sec_start is not None and i > sec_start and re.match(r'^##', line):
            sec_end = i
            break
    if sec_start is not None and sec_end is None:
        sec_end = len(lines)

    result = list(lines)
    if sec_start is not None:
        existing = next(
            (i for i in range(sec_start + 1, sec_end) if f"[[{note_name}]]" in result[i]),
            None
        )
        if existing is not None:
            result[existing] = entry_line
            return result, "更新"
        else:
            insert_at = sec_end
            while insert_at > sec_start + 1 and result[insert_at - 1].strip() == "":
                insert_at -= 1
            result.insert(insert_at, entry_line)
            return result, "新增"
    else:
        # 节不存在，追加到文件末尾
        result.append(f"\n{header_level} {section_header}\n")
        result.append(entry_line)
        return result, "新增（新建节）"


def write_to_branch_file(subject, branch, entry_line):
    """直接写入已拆分的分支文件。"""
    bf = branch_file(subject, branch)
    os.makedirs(os.path.dirname(bf), exist_ok=True)
    if os.path.exists(bf):
        with open(bf, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = [f"# {subject} / {branch}\n\n"]

    note_name = re.search(r'\[\[([^\]]+)\]\]', entry_line).group(1)
    existing = next((i for i, l in enumerate(lines) if f"[[{note_name}]]" in l), None)
    if existing is not None:
        lines[existing] = entry_line
        action = "更新"
    else:
        lines.append(entry_line)
        action = "新增"

    with open(bf, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return action


def write_to_subject_file(subject, branch, entry_line):
    """将条目写入科目文件的对应分支节。"""
    sf = subject_file(subject)
    if not os.path.exists(sf):
        _init_subject_file(subject)
    with open(sf, encoding="utf-8") as f:
        lines = f.readlines()
    new_lines, action = _upsert_entry(lines, branch, entry_line)
    with open(sf, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return action


def _init_subject_file(subject):
    """创建空的科目索引文件。"""
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(subject_file(subject), "w", encoding="utf-8") as f:
        f.write(f"# {subject}索引\n\n")


# ── 自动拆分 ──────────────────────────────────────────────────────────────────

def _split_branch(subject, branch):
    """
    将科目文件中某个分支的所有条目迁移到独立的分支文件，
    并在科目文件中用引用行替换原有条目。
    """
    sf = subject_file(subject)
    with open(sf, encoding="utf-8") as f:
        lines = f.readlines()

    # 找到分支节的范围
    sec_start = sec_end = None
    for i, line in enumerate(lines):
        if line.strip() == f"### {branch}":
            sec_start = i
        if sec_start is not None and i > sec_start and re.match(r'^##', line):
            sec_end = i
            break
    if sec_start is None:
        return
    if sec_end is None:
        sec_end = len(lines)

    # 提取该节条目
    entries = [l for l in lines[sec_start + 1:sec_end] if l.strip()]

    # 写入分支文件
    bf = branch_file(subject, branch)
    os.makedirs(os.path.dirname(bf), exist_ok=True)
    with open(bf, "w", encoding="utf-8") as f:
        f.write(f"# {subject} / {branch}\n\n")
        f.writelines(entries)

    # 科目文件中用引用行替换
    count = sum(1 for e in entries if e.startswith("- [["))
    ref_line = f"> 已拆分 → [{branch}]({subject}/{branch}.md)（{count} 条）\n"
    new_lines = lines[:sec_start + 1] + [ref_line] + lines[sec_end:]
    with open(sf, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print(f"  → 分支「{branch}」已达 {count} 条，自动拆分为 {subject}/{branch}.md")


def check_and_split(subject, branch):
    """检查分支条目数，超过阈值则自动拆分。"""
    if branch_is_split(subject, branch):
        return
    count = count_branch_entries_in_subject(subject, branch)
    if count >= BRANCH_SPLIT_THRESHOLD:
        _split_branch(subject, branch)


# ── 主索引更新 ───────────────────────────��───────────────────────────────────��

def update_master_index():
    """重新统计各科目条目数，更新主索引文件。"""
    rows = []
    for subj in SUBJECTS:
        sf = subject_file(subj)
        total = 0
        if os.path.exists(sf):
            with open(sf, encoding="utf-8") as f:
                lines = f.readlines()
            # 统计科目文件内条目 + 已拆分分支文件的条目
            total = sum(1 for l in lines if l.startswith("- [["))
            # 找出已拆分的分支
            subj_dir = os.path.join(INDEX_DIR, subj)
            if os.path.isdir(subj_dir):
                for fn in os.listdir(subj_dir):
                    if fn.endswith(".md"):
                        total += count_entries_in_file(os.path.join(subj_dir, fn))
        suffix = f"（{total} 条）" if total else "（空）"
        rows.append(f"- **{subj}** {suffix} → `{subj}.md`\n")

    content = (
        "# 知识索引\n\n"
        "> 各科目的详细条目见子文件，使用时只加载相关科目以节省 token。\n"
        f"> 分支条目超过 {BRANCH_SPLIT_THRESHOLD} 条时自动拆分为独立文件。\n\n"
        + "".join(rows)
    )
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        f.write(content)


# ── 主写入流程 ──────────────────��────────────────────────���────────────────────

def write_to_index(note_path, keywords, summary, category):
    parts = category.strip().split("/")
    if len(parts) != 2:
        print("ERROR: --category 格式应为 '一级/二级'，如 '数学/线性代数'", file=sys.stderr)
        sys.exit(1)
    subject, branch = parts[0].strip(), parts[1].strip()

    note_name = os.path.splitext(os.path.basename(note_path))[0]
    entry_line = f"- [[{note_name}]] `{keywords}` — {summary}\n"

    if branch_is_split(subject, branch):
        action = write_to_branch_file(subject, branch, entry_line)
    else:
        action = write_to_subject_file(subject, branch, entry_line)
        check_and_split(subject, branch)

    update_master_index()
    print(f"OK: {action} [[{note_name}]] → {subject}/{branch}")


# ── 主程序 ─────────────────────────────────────────────────���──────────────────

def main():
    parser = argparse.ArgumentParser(description="笔记摘要辅助工具（分层索引）")
    parser.add_argument("--read",     metavar="NOTE")
    parser.add_argument("--write",    metavar="NOTE")
    parser.add_argument("--keywords", metavar="KW")
    parser.add_argument("--summary",  metavar="TEXT")
    parser.add_argument("--category", metavar="一级/二级")
    args = parser.parse_args()

    if args.read:
        if not os.path.exists(args.read):
            print(f"ERROR: 文件不存在：{args.read}", file=sys.stderr)
            sys.exit(1)
        print(clean_note(args.read))

    elif args.write:
        missing = [f for f in ["keywords", "summary", "category"] if not getattr(args, f)]
        if missing:
            print(f"ERROR: 缺少参数：{', '.join('--' + m for m in missing)}", file=sys.stderr)
            sys.exit(1)
        write_to_index(args.write, args.keywords, args.summary, args.category)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
