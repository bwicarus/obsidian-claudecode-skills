"""
Anki 掌握情况查询脚本。

从 anki/records/*.json 读取 anki_note_id，调用 AnkiConnect 查询每张卡的调度状态，
汇总后可写回 Obsidian 笔记 frontmatter 和 record JSON。

用法：
  # 查询指定笔记
  python scripts/anki_status.py --note <笔记路径>

  # 查询目录下所有有 record 的笔记
  python scripts/anki_status.py --dir <目录> [--recursive]

  # 查询所有有 record 的笔记
  python scripts/anki_status.py --all

  # 写回目标（可组合）
  --write-frontmatter   写入笔记 frontmatter（anki_total / anki_new 等）
  --write-record        在 record JSON 中追加 status_snapshot
  --dry-run             只预览，不写入

卡片状态分类（来自 AnkiConnect cardsInfo）：
  new        未开始（type=0）
  learning   学习中（type=1）
  review     已进入长期复习（type=2）
  relearning 遗忘后重学（type=3）
  suspended  已暂停（queue=-1）
  buried     已埋藏（queue=-2 / -3）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS_DIR = PROJECT_DIR / "anki" / "records"
DEFAULT_VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))
DEFAULT_ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")

EMPTY_COUNTS: dict[str, Any] = {
    "total": 0, "new": 0, "learning": 0, "review": 0,
    "relearning": 0, "suspended": 0, "buried": 0,
    "retention_avg": 0.0, "retention_min": 0.0,
    # 掌握度：在 retention 基础上纳入 interval 成熟度 / lapses 遗忘惩罚 / ease 难度
    "mastery_avg": 0.0, "mastery_min": 0.0,
    # 累计复习投入信号
    "reps_total": 0, "lapses_total": 0,
}


# ── Retrievability (FSRS / Ebbinghaus 风格) ─────────────────────────────────
# 单卡的"现在还记得的概率"：R = 0.9^(t/I)，其中 I=interval，t=距上次复习天数。
# 0.9 来自 Anki/FSRS 默认目标留存率（每张卡刚到期时 R≈0.9）。
# new=0, learning=0.4（初次学习中），relearning=0.3（刚遗忘重学）。

def card_retrievability(card: dict, now_ms: int) -> float | None:
    queue = card.get("queue", 0)
    if queue == -1:  # suspended — 不参与平均
        return None
    type_ = card.get("type", 0)
    if type_ == 0:  # new
        return 0.0
    if type_ == 1:  # learning
        return 0.4
    if type_ == 3:  # relearning
        return 0.3
    # type_ == 2: review
    interval = max(card.get("interval", 1), 1)
    mod_s = card.get("mod", now_ms // 1000)
    days_since = max(0.0, (now_ms / 1000 - mod_s) / 86_400)
    return 0.9 ** (days_since / interval)


# ── 掌握度 (mastery) ─────────────────────────────────────────────────────────
# retention 只反映"现在还记不记得"，不区分「复习 1 次就记住」和「复习 20 次还
# 在反复遗忘」。mastery 在 retention 上再乘一个长期稳定度。
#
# FSRS 启用后（collection.anki2 的 cards.data 含 {"s","d","decay"}）：
#   maturity  = min(S / 180, 1)    S=FSRS stability(天)，真实长期记忆强度
#   d_norm    = (10 - D) / 9       D=FSRS difficulty(1-10)，Anki 官方难度
# 拿不到 FSRS（SM-2 / Windows 旧 collection / 读取失败）时退回估算：
#   maturity  = min(interval/90, 1)
#   lapse_factor = 0.7 ** lapses
#   ease_norm = ease factor 归一
# new=0 / learning=0.25 / relearning=0.15（刚遗忘，掌握度最低）。

def card_mastery(
    card: dict, now_ms: int, fsrs_mem: dict[int, dict] | None = None
) -> float | None:
    queue = card.get("queue", 0)
    if queue == -1:  # suspended — 不参与平均
        return None
    type_ = card.get("type", 0)
    if type_ == 0:  # new
        return 0.0
    if type_ == 1:  # learning
        return 0.25
    if type_ == 3:  # relearning
        return 0.15
    # type_ == 2: review
    interval = max(card.get("interval", 1), 1)
    mod_s = card.get("mod", now_ms // 1000)
    days_since = max(0.0, (now_ms / 1000 - mod_s) / 86_400)
    retention = 0.9 ** (days_since / interval)

    fm = (fsrs_mem or {}).get(card.get("cardId") or card.get("id"))
    if fm and fm.get("s") and fm.get("d"):
        # FSRS 真实 stability / difficulty
        S = max(float(fm["s"]), 0.01)
        D = min(max(float(fm["d"]), 1.0), 10.0)
        maturity = min(S / 180.0, 1.0)
        d_norm = (10.0 - D) / 9.0          # D=1→1.0(易) D=10→0.0(难)
        stability = maturity * (0.3 + 0.7 * d_norm)
    else:
        # 退回 SM-2 估算
        maturity = min(interval / 90.0, 1.0)
        lapses = max(card.get("lapses", 0), 0)
        lapse_factor = 0.7 ** lapses
        factor = card.get("factor") or 2500
        ease_norm = 0.5 + 0.5 * max(0.0, min(1.0, (factor - 1300) / 1200.0))
        stability = maturity * lapse_factor * ease_norm

    mastery = retention * (0.4 + 0.6 * stability)
    return max(0.0, min(1.0, mastery))


# ── AnkiConnect ──────────────────────────────────────────────────────────────

ANKI_EXE_CANDIDATES = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Anki\anki.exe"),
    r"C:\Program Files\Anki\anki.exe",
)


def _open_anki() -> None:
    for path in ANKI_EXE_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            subprocess.Popen(
                [path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            pass
        return


def wait_for_anki(url: str, wait_seconds: int) -> None:
    """轮询 AnkiConnect 直到响应，超时则报错。"""
    if wait_seconds <= 0:
        return
    deadline = time.time() + wait_seconds
    launched = False
    while True:
        try:
            anki_request(url, "version", timeout=5)
            print("AnkiConnect OK")
            return
        except RuntimeError:
            if time.time() >= deadline:
                raise RuntimeError(f"等待 AnkiConnect 超时（{wait_seconds}s），请确认 Anki 已启动且 AnkiConnect 插件已启用。")
            if not launched:
                print("AnkiConnect 未响应，尝试启动 Anki...")
                _open_anki()
                launched = True
            else:
                print("等待 AnkiConnect 上线...")
            time.sleep(5)


def anki_request(url: str, action: str, params: dict | None = None, timeout: int = 15) -> Any:
    payload: dict[str, Any] = {"action": action, "version": 6}
    if params:
        payload["params"] = params
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from e
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result.get("result")


# ── Record 工具 ───────────────────────────────────────────────────────────────

def safe_record_stem(source_note: str) -> str:
    if source_note.lower().endswith(".md"):
        source_note = source_note[:-3]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "__", source_note)
    value = re.sub(r"__+", "__", value).strip(" ._")
    return value or "note"


def source_note_rel(note_path: Path, vault_root: Path) -> str:
    try:
        return note_path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return str(note_path.resolve())


def record_path_for(note_path: Path, vault_root: Path, records_dir: Path) -> Path:
    return records_dir / f"{safe_record_stem(source_note_rel(note_path, vault_root))}.json"


def load_record(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def collect_anki_note_ids(record: dict) -> list[int]:
    return [
        int(c["anki_note_id"])
        for c in record.get("cards", [])
        if c.get("status") == "synced" and c.get("anki_note_id")
    ]


# ── FSRS memory state（直读 collection.anki2 cards.data）──────────────────────
# AnkiConnect cardsInfo 不暴露 FSRS 字段，但 collection 的 cards.data 列存
# {"s":stability,"d":difficulty,"decay":...}。用 immutable 模式读（Anki 在跑
# 也安全）。拿不到（SM-2 / 路径未知 / 读失败）返回 {}，card_mastery 自动退回估算。

def _anki_collection_path(anki_url: str) -> Path | None:
    env = os.environ.get("ANKI_COLLECTION")
    if env and Path(env).exists():
        return Path(env)
    try:
        media = anki_request(anki_url, "getMediaDirPath", timeout=5)
        if media:
            cand = Path(media).parent / "collection.anki2"
            if cand.exists():
                return cand
    except RuntimeError:
        pass
    return None


def fsrs_memory_map(anki_url: str, card_ids: list[int]) -> dict[int, dict]:
    if not card_ids:
        return {}
    col = _anki_collection_path(anki_url)
    if col is None:
        return {}
    out: dict[int, dict] = {}
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{col}?immutable=1", uri=True)
        # 分批 IN 查询，避免 SQL 变量上限
        for i in range(0, len(card_ids), 900):
            batch = card_ids[i:i + 900]
            ph = ",".join("?" * len(batch))
            for cid, data in conn.execute(
                f"SELECT id, data FROM cards WHERE id IN ({ph})", batch
            ):
                if not data or data in ("{}", ""):
                    continue
                try:
                    d = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if "s" in d and "d" in d:
                    out[int(cid)] = d
        conn.close()
    except Exception:
        return {}
    return out


# ── 状态查询 ─────────────────────────────────────────────────────────────────

def classify_card(info: dict) -> str:
    queue = info.get("queue", 0)
    if queue == -1:
        return "suspended"
    if queue in (-2, -3):
        return "buried"
    t = info.get("type", 0)
    return {0: "new", 1: "learning", 2: "review", 3: "relearning"}.get(t, "unknown")


def query_counts(anki_url: str, note_ids: list[int]) -> dict[str, Any]:
    notes_info = anki_request(anki_url, "notesInfo", {"notes": note_ids})
    card_ids = [cid for note in notes_info for cid in note.get("cards", [])]
    if not card_ids:
        return dict(EMPTY_COUNTS)

    cards_info = anki_request(anki_url, "cardsInfo", {"cards": card_ids})
    counts: dict[str, Any] = dict(EMPTY_COUNTS)
    counts["total"] = len(cards_info)

    fsrs_mem = fsrs_memory_map(anki_url, card_ids)

    now_ms = int(time.time() * 1000)
    retentions: list[float] = []
    masteries: list[float] = []
    for card in cards_info:
        status = classify_card(card)
        if status in counts:
            counts[status] += 1
        r = card_retrievability(card, now_ms)
        if r is not None:
            retentions.append(r)
        m = card_mastery(card, now_ms, fsrs_mem)
        if m is not None:
            masteries.append(m)
        counts["reps_total"] += max(card.get("reps", 0), 0)
        counts["lapses_total"] += max(card.get("lapses", 0), 0)

    if retentions:
        counts["retention_avg"] = sum(retentions) / len(retentions)
        counts["retention_min"] = min(retentions)
    if masteries:
        counts["mastery_avg"] = sum(masteries) / len(masteries)
        counts["mastery_min"] = min(masteries)
    return counts


# ── Frontmatter 写入 ──────────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_ANKI_FM_KEYS = (
    "anki_total", "anki_new", "anki_learning",
    "anki_review", "anki_relearning", "anki_suspended",
    "anki_retention", "anki_mastery", "anki_checked",
)


def _build_fm_kv(counts: dict[str, Any]) -> dict[str, Any]:
    return {
        "anki_total":     counts["total"],
        "anki_new":       counts["new"],
        "anki_learning":  counts["learning"],
        "anki_review":    counts["review"],
        "anki_relearning": counts["relearning"],
        "anki_suspended": counts["suspended"],
        "anki_retention": round(counts.get("retention_avg", 0.0), 3),
        "anki_mastery":   round(counts.get("mastery_avg", 0.0), 3),
        "anki_checked":   dt.date.today().isoformat(),
    }


def set_frontmatter_keys(content: str, kv: dict[str, Any]) -> str:
    m = _FM_RE.match(content)
    if m:
        fm = m.group(1)
        for key, value in kv.items():
            pat = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
            line = f"{key}: {value}"
            if pat.search(fm):
                fm = pat.sub(line, fm)
            else:
                fm = fm.rstrip("\n") + f"\n{line}"
        return f"---\n{fm}\n---\n" + content[m.end():]
    else:
        block = "\n".join(f"{k}: {v}" for k, v in kv.items())
        return f"---\n{block}\n---\n\n" + content


def write_frontmatter(note_path: Path, counts: dict[str, Any], dry_run: bool) -> None:
    content = note_path.read_text(encoding="utf-8")
    new_content = set_frontmatter_keys(content, _build_fm_kv(counts))
    if dry_run:
        m = _FM_RE.match(new_content)
        preview = f"---\n{m.group(1)}\n---" if m else new_content[:300]
        print(f"  [dry-run] frontmatter:\n{preview}")
    else:
        note_path.write_text(new_content, encoding="utf-8")
        print(f"  frontmatter 已写入：{note_path.name}")


# ── Record snapshot 写入 ──────────────────────────────────────────────────────

def write_record_snapshot(record_path: Path, counts: dict[str, Any], dry_run: bool) -> None:
    record = load_record(record_path) or {}
    snapshot = {
        "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        **counts,
    }
    record["status_snapshot"] = snapshot
    if dry_run:
        print(f"  [dry-run] status_snapshot: {json.dumps(snapshot, ensure_ascii=False)}")
    else:
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  record snapshot 已写入：{record_path.name}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def process_note(note_path: Path, args: argparse.Namespace) -> None:
    record_path = record_path_for(note_path, args.vault_root, args.records_dir)
    record = load_record(record_path)

    if record is None:
        print(f"跳过（无 record）：{note_path.name}")
        return
    if record.get("status") == "no_cards":
        print(f"跳过（no_cards）：{note_path.name}")
        return

    note_ids = collect_anki_note_ids(record)
    if not note_ids:
        print(f"跳过（无已同步卡片）：{note_path.name}")
        return

    try:
        counts = query_counts(args.anki_url, note_ids)
    except RuntimeError as e:
        print(f"ERROR [{note_path.name}]: {e}", file=sys.stderr)
        return

    print(
        f"【{note_path.stem}】"
        f"总 {counts['total']} | 新 {counts['new']} | "
        f"学习 {counts['learning']} | 复习 {counts['review']} | "
        f"重学 {counts['relearning']} | 暂停 {counts['suspended']} | "
        f"留存 {counts.get('retention_avg', 0.0):.2f}"
    )

    if args.write_frontmatter and note_path.exists():
        write_frontmatter(note_path, counts, args.dry_run)
    if args.write_record:
        write_record_snapshot(record_path, counts, args.dry_run)


def process_book_records(args: argparse.Namespace) -> int:
    """给**书源** record 也算一次 status_snapshot。

    笔记那条路（collect_notes → record_path_for）按 source_note 反算文件名，
    对 reader-<book>.json 永远对不上 —— 所以书里建的卡从来拿不到 status_snapshot，
    于是 KG 那侧的 `card_mastery()` 恒为 None、`weakness_score()` 恒 0.5。
    「按掌握度低的知识找对应知识点」的**掌握度**那一半就是空的。

    这里直接遍历 records_dir，只认 source_kind == "book"：书没有 frontmatter
    可写，所以只做 write_record_snapshot 这一半。
    """

    if not args.write_record:
        return 0
    done = 0
    for record_path in sorted(args.records_dir.glob("*.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(record.get("source_kind") or "") != "book":
            continue
        note_ids = collect_anki_note_ids(record)
        if not note_ids:
            continue
        try:
            counts = query_counts(args.anki_url, note_ids)
        except RuntimeError as e:
            print(f"ERROR [{record_path.name}]: {e}", file=sys.stderr)
            continue
        book = str(record.get("source_book") or record.get("source_ref") or record_path.stem)
        print(
            f"【书 {book}】"
            f"总 {counts['total']} | 新 {counts['new']} | "
            f"学习 {counts['learning']} | 复习 {counts['review']} | "
            f"重学 {counts['relearning']} | 暂停 {counts['suspended']} | "
            f"留存 {counts.get('retention_avg', 0.0):.2f}"
        )
        write_record_snapshot(record_path, counts, args.dry_run)
        done += 1
    return done


def collect_notes(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []

    for value in (args.note or []):
        p = Path(value).expanduser().resolve()
        if p.is_file() and p.suffix.lower() == ".md":
            paths.append(p)
        else:
            print(f"WARN: 文件不存在或不是 md：{value}", file=sys.stderr)

    for dir_value in (args.dir or []):
        d = Path(dir_value).expanduser()
        if not d.is_dir():
            print(f"WARN: 目录不存在：{dir_value}", file=sys.stderr)
            continue
        it = d.rglob("*.md") if args.recursive else d.glob("*.md")
        paths.extend(p.resolve() for p in it if p.is_file())

    if args.all:
        for record_file in sorted(args.records_dir.glob("*.json")):
            try:
                rec = json.loads(record_file.read_text(encoding="utf-8"))
                sn = rec.get("source_note", "")
                if sn:
                    p = (args.vault_root / sn).resolve()
                    if p.is_file():
                        paths.append(p)
            except (json.JSONDecodeError, OSError):
                pass

    seen: set[str] = set()
    result: list[Path] = []
    for p in paths:
        key = str(p).casefold()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return sorted(result, key=lambda p: str(p).casefold())


def main() -> None:
    parser = argparse.ArgumentParser(description="查询 Anki 卡片掌握情况并写回笔记 / record")
    parser.add_argument("--note", action="append", default=[], metavar="NOTE", help="笔记路径（可重复）")
    parser.add_argument("--dir",  action="append", default=[], metavar="DIR",  help="目录（可重复）")
    parser.add_argument("--all",  action="store_true", help="处理所有有 record 的笔记")
    parser.add_argument("--recursive", action="store_true", help="--dir 时递归子目录")
    parser.add_argument("--write-frontmatter", action="store_true", help="将统计写入笔记 frontmatter")
    parser.add_argument("--write-record",       action="store_true", help="将统计追加到 record JSON")
    parser.add_argument("--dry-run",            action="store_true", help="只预览，不写入")
    parser.add_argument("--records-dir",  type=Path, default=DEFAULT_RECORDS_DIR)
    parser.add_argument("--vault-root",   type=Path, default=DEFAULT_VAULT_ROOT)
    parser.add_argument("--anki-url",     default=DEFAULT_ANKI_URL)
    parser.add_argument("--wait-seconds", type=int, default=0,
                        help="启动时轮询等待 AnkiConnect 的最长秒数（0=不等待）")
    args = parser.parse_args()

    if not args.note and not args.dir and not args.all:
        parser.error("需要 --note、--dir 或 --all 之一")

    args.records_dir = args.records_dir.expanduser().resolve()
    args.vault_root  = args.vault_root.expanduser().resolve()

    if args.wait_seconds > 0:
        try:
            wait_for_anki(args.anki_url, args.wait_seconds)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    notes = collect_notes(args)
    # 书源 record 走单独一条路（没有笔记文件可遍历），--all 时一并处理。
    books = process_book_records(args) if args.all else 0
    if not notes:
        if books:
            print(f"没有笔记，但处理了 {books} 份书源 record。")
            return
        print("未找到目标笔记。", file=sys.stderr)
        sys.exit(1)

    for note_path in notes:
        process_note(note_path, args)


if __name__ == "__main__":
    main()
