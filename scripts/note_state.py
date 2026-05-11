"""
Shared note state tracker: records per-note content hash for each skill.
Stored in state/note-states.json — script-only, never passed to AI.
"""
import datetime
import hashlib
import json
import re
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "note-states.json"

# 自动管理的内容（不应影响哈希）：
#   1. 整段 frontmatter（Obsidian / 插件 / anki_status / review_priority 都可能改）
#   2. 「相关笔记」节由 connect_note 维护
_FRONTMATTER_RE   = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)
_RELATED_SPLIT_RE = re.compile(r"(?:\n+---\s*)?\n##\s+相关笔记\s*\n", re.UNICODE)
# 旧算法的字段级剥离，仅用于 hash 兼容比对，不再用于写入
_AUTO_FIELDS_RE_LEGACY = re.compile(r"^(?:anki_\w+|review_\w+):.*\n?", re.MULTILINE)


def _file_hash(path: Path) -> str:
    """计算笔记主体内容的哈希，排除自动管理的内容。

    排除项：
      - 整段 frontmatter（含未来可能新增的自动字段，避免打开/插件触发误识别）
      - 「相关笔记」节（含其前的 --- 分割线）
      - 尾部空白
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()[:16]
    # 剥整段 frontmatter
    text = _FRONTMATTER_RE.sub("", text, count=1)
    # 剥「相关笔记」节
    m = _RELATED_SPLIT_RE.search(text)
    if m:
        text = text[:m.start()]
    return hashlib.sha256(text.rstrip().encode("utf-8")).hexdigest()[:16]


def _file_hash_legacy(path: Path) -> str:
    """旧 hash 算法（未剥 frontmatter，仅剥 anki_/review_ 字段）。
    仅用于已存量笔记的兼容比对，避免迁移期把所有笔记当成"已修改"。"""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()[:16]
    m = _RELATED_SPLIT_RE.search(text)
    if m:
        text = text[:m.start()]
    text = _AUTO_FIELDS_RE_LEGACY.sub("", text)
    return hashlib.sha256(text.rstrip().encode("utf-8")).hexdigest()[:16]


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(states: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(states, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def is_unchanged(note_path: Path, skill: str) -> bool:
    """Returns True if note content matches the hash recorded for this skill."""
    states = _load()
    key = str(note_path.resolve())
    stored_hash = states.get(key, {}).get(skill, {}).get("hash")
    if not stored_hash:
        return False
    try:
        if _file_hash(note_path) == stored_hash:
            return True
        # 兼容存量数据：旧算法计算的 hash 也算匹配
        return _file_hash_legacy(note_path) == stored_hash
    except OSError:
        return False


def mark_processed(note_path: Path, skill: str) -> None:
    """Record current content hash for this note+skill combination.
    Resets failure counter on success."""
    states = _load()
    key = str(note_path.resolve())
    if key not in states:
        states[key] = {}
    states[key][skill] = {
        "hash": _file_hash(note_path),
        "processed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _save(states)


def mark_failed(note_path: Path, skill: str, error: str) -> None:
    """Record a failure for this note+skill, incrementing the failure counter."""
    states = _load()
    key = str(note_path.resolve())
    if key not in states:
        states[key] = {}
    rec = states[key].setdefault(skill, {})
    rec["failure_count"] = int(rec.get("failure_count", 0)) + 1
    rec["last_error"] = (error or "")[:200]
    rec["last_error_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    _save(states)


def should_skip_failed(note_path: Path, skill: str, threshold: int = 3) -> bool:
    """Returns True if this note has failed the given skill ≥threshold times."""
    states = _load()
    key = str(note_path.resolve())
    rec = states.get(key, {}).get(skill, {})
    return int(rec.get("failure_count", 0)) >= threshold


def has_record(note_path: Path, skill: str) -> bool:
    """Returns True if this note has ever been successfully processed by this skill."""
    states = _load()
    key = str(note_path.resolve())
    rec = states.get(key, {}).get(skill, {})
    # 区分"成功"与"仅记录失败"：要求有 hash 字段
    return bool(rec.get("hash"))


def get_last_scan(scan_name: str) -> "datetime.datetime | None":
    """Returns the last recorded scan time for scan_name, or None."""
    states = _load()
    ts = states.get("__last_scan__", {}).get(scan_name)
    if ts:
        try:
            return datetime.datetime.fromisoformat(ts)
        except ValueError:
            return None
    return None


def update_last_scan(scan_name: str) -> None:
    """Record the current time as the last scan time for scan_name."""
    states = _load()
    if "__last_scan__" not in states:
        states["__last_scan__"] = {}
    states["__last_scan__"][scan_name] = (
        datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    )
    _save(states)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="note_state CLI")
    parser.add_argument("--mark-processed", metavar="PATH", help="笔记路径")
    parser.add_argument("--skill", default="summarize", help="skill 名称（配合 --mark-processed）")
    parser.add_argument("--update-last-scan", metavar="SCAN_NAME", help="更新扫描时间戳")
    args = parser.parse_args()

    if args.mark_processed:
        mark_processed(Path(args.mark_processed), args.skill)
        print(f"已记录：{args.mark_processed} [{args.skill}]")
    if args.update_last_scan:
        update_last_scan(args.update_last_scan)
        print(f"已更新扫描时间：{args.update_last_scan}")
