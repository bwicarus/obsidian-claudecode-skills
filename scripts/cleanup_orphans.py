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
import note_state

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


# ── B0. 重命名检测：用内容哈希识别"删+加"实际是 rename ────────────────────────

def detect_renames(orphans: list[tuple[Path, str, list[int]]]) -> list[dict]:
    """对每个 orphan record，看 vault 里是否有相同内容哈希的新笔记。

    返回 [{record_path, old_source_note, new_source_note}]。
    匹配规则：用 note_state 的旧 hash（current 或 legacy 算法），跟 vault 里
    所有"已存在但不在已有 record 中"的笔记的实时 hash 比对。
    """
    if not orphans:
        return []
    # 1. 收集每个 orphan 的旧 hash
    states = note_state._load()
    old_hashes = {}   # source_note → hash
    for rf, sn, _ in orphans:
        # note_state key 是绝对路径
        full = (config.VAULT_ROOT / sn).as_posix() \
               if not Path(sn).is_absolute() else sn
        h = states.get(full, {}).get("summarize", {}).get("hash")
        if h:
            old_hashes[sn] = h

    if not old_hashes:
        return []
    # 2. 收集 vault 里"现存但无对应 record"的笔记 + 实时 hash
    NOTE_PAT = re.compile(r"^[0-9A-Fa-f]{3}-")
    existing_records = set()
    for rf in config.RECORDS_DIR.glob("*.json"):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sn = rec.get("source_note", "")
        if sn:
            existing_records.add(sn)
    new_files = {}   # hash → relative source_note path
    for md in config.VAULT_ROOT.rglob("*.md"):
        if not NOTE_PAT.match(md.name):
            continue
        try:
            rel = md.resolve().relative_to(config.VAULT_ROOT.resolve()).as_posix()
        except ValueError:
            continue
        if rel in existing_records:
            continue
        try:
            h = note_state._file_hash(md)
            h2 = note_state._file_hash_legacy(md)
        except Exception:
            continue
        # 一个 hash 可能对应多个文件（极少），保留第一个
        new_files.setdefault(h, rel)
        new_files.setdefault(h2, rel)
    # 3. 匹配
    renames = []
    for rf, old_sn, _ in orphans:
        old_h = old_hashes.get(old_sn)
        if not old_h:
            continue
        new_rel = new_files.get(old_h)
        if new_rel and new_rel != old_sn:
            renames.append({
                "record_path": rf,
                "old_source_note": old_sn,
                "new_source_note": new_rel,
            })
    return renames


def apply_renames(renames: list[dict]) -> int:
    """应用重命名：改 record source_note + 移 record 文件 +
    更新 note-states key + 更新 KG _note_to_covered_l2 + 改索引条目 +
    改别的笔记里的 [[old]] → [[new]]。Anki 卡片**不动**（仍指向同 nid）。
    返回成功 rename 的笔记数。"""
    if not renames:
        return 0
    states = note_state._load()
    states_changed = False
    done = 0
    for rn in renames:
        rf: Path = rn["record_path"]
        old_sn = rn["old_source_note"]
        new_sn = rn["new_source_note"]
        # 1. 改 record 内 source_note
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  rename 读 record 失败 {rf.name}: {e}", file=sys.stderr)
            continue
        rec["source_note"] = new_sn
        rec["renamed_from"] = rec.get("renamed_from", []) + [{
            "from": old_sn,
            "at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        }]
        # 2. 物理 rename record 文件
        try:
            # 复用 anki_status.safe_record_stem 逻辑（简化版）
            new_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "__",
                              new_sn[:-3] if new_sn.lower().endswith(".md") else new_sn)
            new_stem = re.sub(r"__+", "__", new_stem).strip(" ._") or "note"
            new_rf = rf.parent / f"{new_stem}.json"
            if new_rf != rf and new_rf.exists():
                print(f"  rename 目标 record 已存在跳过：{new_rf.name}", file=sys.stderr)
                continue
            rf.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
            if new_rf != rf:
                rf.rename(new_rf)
        except OSError as e:
            print(f"  rename record 失败 {rf.name}: {e}", file=sys.stderr); continue
        # 3. 更新 note-states key（绝对路径）
        old_full = (config.VAULT_ROOT / old_sn).as_posix()
        new_full = (config.VAULT_ROOT / new_sn).as_posix()
        if old_full in states:
            states[new_full] = states.pop(old_full)
            states_changed = True
        # 4. 更新 KG _note_to_covered_l2 key（所有 KG 文件扫一遍）
        kg_dir = config.PROJECT_DIR / "knowledge_graph"
        for kg_f in kg_dir.glob("*.json"):
            if kg_f.name.endswith(".bak.json"): continue
            try:
                kg = json.loads(kg_f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            persistent = kg.get("_note_to_covered_l2") or {}
            if old_sn in persistent:
                persistent[new_sn] = persistent.pop(old_sn)
                kg["_note_to_covered_l2"] = persistent
                # 重建节点 containing_notes 简单做法：替换 path
                for n in kg.get("nodes", []):
                    cn = n.get("containing_notes") or []
                    if old_sn in cn:
                        n["containing_notes"] = sorted(set(
                            new_sn if x == old_sn else x for x in cn))
                        n["note_ref"] = (n["containing_notes"] or [""])[0]
                tmp = kg_f.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(kg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(kg_f)
        # 5. 改索引条目 [[old]] → [[new]]（仅文件名 stem）
        old_stem = Path(old_sn).stem
        new_stem_link = Path(new_sn).stem
        for idx in sorted(config.INDEX_DIR.rglob("*.md")):
            try: txt = idx.read_text(encoding="utf-8")
            except OSError: continue
            new_txt = txt.replace(f"[[{old_stem}]]", f"[[{new_stem_link}]]")
            if new_txt != txt:
                idx.write_text(new_txt, encoding="utf-8")
        # 6. 改别的笔记里的 [[old]] → [[new]]
        for md in config.VAULT_ROOT.rglob("*.md"):
            if md.name == Path(new_sn).name: continue
            try: txt = md.read_text(encoding="utf-8")
            except OSError: continue
            if f"[[{old_stem}]]" in txt:
                md.write_text(txt.replace(
                    f"[[{old_stem}]]", f"[[{new_stem_link}]]"), encoding="utf-8")
        done += 1
        print(f"  ✓ rename {old_sn} → {new_sn}")
    if states_changed:
        note_state._save(states)
    return done


# ── B. Anki record 孤儿 ──────────────────────────────────────────────────────

def scan_record_orphans() -> list[tuple[Path, str, list[int]]]:
    """返回 [(record_file, source_note, anki_note_ids)]。"""
    orphans = []
    for rf in sorted(config.RECORDS_DIR.glob("*.json")):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # 书源 record 不参与孤儿判定。它的"出处"是一本书而不是 vault 笔记，
        # 尤其本机导入书是 localbook:<sha> —— 在 vault 里必然找不到，
        # 于是会被判成孤儿，suspend_orphan_cards 会把**真卡**挂起。
        # 要给书源做孤儿回收得另立规则（书文件不存在才算），不能复用笔记这套。
        if str(rec.get("source_kind") or "") == "book":
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
    # B0. 在 suspend 之前先做重命名识别：内容 hash 匹配 → rename 而非 orphan
    renames = detect_renames(b)
    if renames:
        print(f"[B0] 重命名识别：{len(renames)} 篇")
        for rn in renames:
            print(f"  {rn['old_source_note']} → {rn['new_source_note']}")
    if apply and renames:
        n_renamed = apply_renames(renames)
        print(f"  → 已重命名 {n_renamed} 个 record（卡片保留不 suspend）")
        # 已重命名的从 orphans 列表里移除，不再 suspend
        renamed_paths = {rn["record_path"] for rn in renames}
        b = [(rf, sn, nids) for (rf, sn, nids) in b if rf not in renamed_paths]
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
