"""
cleanup_orphans.py — 维护脚本：扫描并清理系统里的孤儿数据

四类孤儿：
  A. 索引条目     → 笔记文件不存在
  B. Anki record  → source_note 不存在（卡片在 Anki 里成孤儿）
  C. 相关笔记链接 → 目标笔记不存在（dead [[link]]）
  D. note-states  → 状态文件里的 key 笔记不存在

默认 dry-run，加 --apply 才真的修改。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

ANKI_URL = config.ANKI_CONNECT_URL


def anki_request(action: str, params: dict | None = None, timeout: int = 30):
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ANKI_URL, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    result = json.loads(body)
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result.get("result")


# ── A. 索引条目孤儿 ──────────────────────────────────────────────────────────

def scan_index_orphans() -> list[tuple[Path, int, str, str]]:
    """返回 [(index_file, line_number, line_text, note_name)]。"""
    orphans = []
    entry_re = re.compile(r"^-\s*\[\[([^\]]+)\]\]")
    for idx in sorted(config.INDEX_DIR.rglob("*.md")):
        if idx.name == "knowledge-index.md":
            continue
        try:
            lines = idx.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = entry_re.match(line.strip())
            if not m:
                continue
            name = m.group(1).strip()
            # 只检查 .md 笔记（不带扩展名的 wiki 链接）
            note_path = config.VAULT_ROOT / f"{name}.md"
            # 也找 vault 子目录（笔记可能在 vault 子目录里）
            if not note_path.exists():
                # 全 vault 找
                hits = list(config.VAULT_ROOT.rglob(f"{name}.md"))
                if not hits:
                    orphans.append((idx, i, line.rstrip(), name))
    return orphans


def remove_index_orphans(orphans: list[tuple[Path, int, str, str]]) -> int:
    """按 (index_file, line_number) 反向删除。"""
    by_file: dict[Path, list[int]] = {}
    for idx, ln, _, _ in orphans:
        by_file.setdefault(idx, []).append(ln)
    removed = 0
    for idx, lns in by_file.items():
        try:
            lines = idx.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            continue
        for ln in sorted(lns, reverse=True):
            del lines[ln]
            removed += 1
        idx.write_text("".join(lines), encoding="utf-8")
    return removed


# ── B. Anki record 孤儿 ──────────────────────────────────────────────────────

def scan_record_orphans() -> list[tuple[Path, str, list[int]]]:
    """返回 [(record_file, source_note, anki_note_ids)]。"""
    orphans = []
    for rf in sorted(config.RECORDS_DIR.glob("*.json")):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sn = rec.get("source_note", "")
        if not sn:
            continue
        # source_note 可能是相对路径
        candidates = [config.VAULT_ROOT / sn]
        # 也扫子目录
        bare = Path(sn).name
        if not candidates[0].exists():
            extra = list(config.VAULT_ROOT.rglob(bare))
            if extra:
                continue  # 找到了
            nids = [c.get("anki_note_id") for c in rec.get("cards", []) if c.get("anki_note_id")]
            orphans.append((rf, sn, [int(n) for n in nids if n]))
    return orphans


def suspend_orphan_cards(orphans: list[tuple[Path, str, list[int]]]) -> tuple[int, int]:
    """对孤儿 record 的卡片调用 AnkiConnect suspend；标记 record status='orphan'。
    返回 (suspended_card_count, marked_record_count)。"""
    if not orphans:
        return 0, 0
    # AnkiConnect suspend 接受 cardIds，我们有的是 noteIds，需转换
    suspended_total = 0
    for rf, sn, nids in orphans:
        if not nids:
            continue
        try:
            cids_per_note = anki_request("findCards", {
                "query": " OR ".join(f"nid:{n}" for n in nids),
            })
            if cids_per_note:
                anki_request("suspend", {"cards": cids_per_note})
                suspended_total += len(cids_per_note)
        except RuntimeError as e:
            print(f"  suspend 失败 {rf.name}: {e}", file=sys.stderr)
            continue
        # 标记 record
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
            rec["status"] = "orphan"
            rec["orphan_at"] = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
            rf.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  标记 record 失败 {rf.name}: {e}", file=sys.stderr)
    return suspended_total, len(orphans)


# ── C. 相关笔记里的 dead 链接 ────────────────────────────────────────────────

_RELATED_HEADING_RE = re.compile(r"\n##\s+相关笔记\s*\n", re.UNICODE)
_LINK_LINE_RE       = re.compile(r"-\s+\[\[([^\]]+)\]\]")


def scan_dead_links() -> list[tuple[Path, str, list[str]]]:
    """返回 [(note_path, section_text, dead_targets)]。"""
    orphans = []
    pattern = re.compile(r"^[0-9A-Fa-f]{3}-.+\.md$")
    for md in sorted(config.VAULT_ROOT.rglob("*.md")):
        if not pattern.match(md.name):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _RELATED_HEADING_RE.search(text)
        if not m:
            continue
        section = text[m.end():]
        nh = re.search(r"\n#{1,2}\s+", section)
        if nh:
            section = section[:nh.start()]
        dead = []
        for line in section.splitlines():
            lm = _LINK_LINE_RE.match(line.strip())
            if not lm:
                continue
            target = lm.group(1).strip()
            if target.lower().endswith(".md"):
                target = target[:-3]
            tp = config.VAULT_ROOT / f"{target}.md"
            if not tp.exists() and not list(config.VAULT_ROOT.rglob(f"{target}.md")):
                dead.append(target)
        if dead:
            orphans.append((md, section, dead))
    return orphans


def remove_dead_links(orphans: list[tuple[Path, str, list[str]]]) -> int:
    """从笔记的相关笔记节里删掉 dead link 行。"""
    removed = 0
    for note_path, _, dead in orphans:
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        new_lines = []
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            lm = _LINK_LINE_RE.match(stripped)
            if lm:
                target = lm.group(1).strip()
                if target.lower().endswith(".md"):
                    target = target[:-3]
                if target in dead:
                    removed += 1
                    continue
            new_lines.append(line)
        note_path.write_text("".join(new_lines), encoding="utf-8")
    return removed


# ── D. note-states 孤儿 ─────────────────────────────────────────────────────

def scan_state_orphans() -> list[str]:
    """返回 note-states.json 里 key 不存在的笔记路径列表。"""
    states_file = config.NOTE_STATES_FILE
    if not states_file.exists():
        return []
    try:
        states = json.loads(states_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    orphans = []
    for key in states.keys():
        if key == "__last_scan__":
            continue
        if not Path(key).exists():
            orphans.append(key)
    return orphans


def remove_state_orphans(orphans: list[str]) -> int:
    if not orphans:
        return 0
    states_file = config.NOTE_STATES_FILE
    states = json.loads(states_file.read_text(encoding="utf-8"))
    for key in orphans:
        states.pop(key, None)
    states_file.write_text(json.dumps(states, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(orphans)


# ── 主入口 ──────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="孤儿数据扫描/清理")
    p.add_argument("--apply", action="store_true",
                   help="实际执行清理（默认 dry-run，只报告）")
    p.add_argument("--no-anki", action="store_true",
                   help="跳过 Anki 操作（B 类只报告不挂起）")
    args = p.parse_args()

    apply = args.apply
    print(f"模式：{'APPLY' if apply else 'DRY-RUN'}")
    print()

    # A. 索引孤儿
    a = scan_index_orphans()
    print(f"[A] 索引孤儿：{len(a)} 条")
    for idx, ln, line, name in a:
        print(f"  {idx.relative_to(config.PROJECT_DIR)}:{ln+1}  [[{name}]]")
    if apply and a:
        n = remove_index_orphans(a)
        print(f"  → 已删除 {n} 行")
    print()

    # B. record 孤儿
    b = scan_record_orphans()
    print(f"[B] 卡片记录孤儿：{len(b)} 条")
    for rf, sn, nids in b:
        print(f"  {rf.name}  (source_note={sn}, {len(nids)} 张卡)")
    if apply and b and not args.no_anki:
        try:
            anki_request("version", timeout=5)
            sc, rc = suspend_orphan_cards(b)
            print(f"  → 已挂起 {sc} 张卡，标记 {rc} 个 record 为 orphan")
        except (RuntimeError, OSError) as e:
            print(f"  AnkiConnect 不可用，跳过 B 类挂起：{e}", file=sys.stderr)
    print()

    # C. 死链接
    c = scan_dead_links()
    total_dead = sum(len(d) for _, _, d in c)
    print(f"[C] 相关笔记 dead links：{len(c)} 篇笔记 / {total_dead} 条链接")
    for note_path, _, dead in c:
        print(f"  {note_path.name}: → {', '.join(dead)}")
    if apply and c:
        n = remove_dead_links(c)
        print(f"  → 已删除 {n} 行")
    print()

    # D. note-states 孤儿
    d = scan_state_orphans()
    print(f"[D] note-states 孤儿：{len(d)} 条")
    for k in d[:10]:
        print(f"  {k}")
    if len(d) > 10:
        print(f"  ... 还有 {len(d) - 10} 条")
    if apply and d:
        n = remove_state_orphans(d)
        print(f"  → 已删除 {n} 条")
    print()

    total = len(a) + len(b) + total_dead + len(d)
    print(f"合计：{total} 项孤儿数据" + ("（已清理）" if apply else "（运行 --apply 清理）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
