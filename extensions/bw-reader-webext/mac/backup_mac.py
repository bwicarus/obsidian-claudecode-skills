#!/usr/bin/env python3
"""Mac 服务器数据每日备份 → 外接盘 BWDev（2026-09-25）。

用户原话：外接盘「当作备份保存……尽量不把东西放在这台 mac 里，但会影响运行和服务速度的
就不需要妥协」。所以**在用数据留内置盘**，这里只把它们每天快照一份到外接盘。

规则：
- 外接盘没挂载 → 跳过并记一笔，**绝不退回写内置盘**（那等于悄悄吃掉内置盘空间）。
- SQLite 用在线备份 API 复制：服务一边写一边备也拿得到一致的库；直接拷文件会拷到
  写了一半的页（2026-09-24 Windows 断电清零 7 个文件的教训：备份本身不能再是半截的）。
- 其余文件 rsync，与上一份快照 --link-dest 硬链接：没变的文件不占新空间。
  数据库自上次快照后没被写过（主库与 -wal 的 mtime 都早于上次开始时间）也直接硬链接。
- 保留最近 7 天 + 最近 4 周每周一份；先写 <名字>.partial，成功才改名，中途崩了不会被当成好快照。

用法：backup_mac.py [--dry-run]    （launchd 每天 03:30 跑，见 deploy_mac.py 的 backup）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
VOLUME = Path("/Volumes/BWDev")
DEST_ROOT = VOLUME / "backups"
SOURCES = {"data": HOME / "BW" / "data", "config": HOME / "BW" / "config"}
# 已整体迁到外接盘 archive/ 的旧副本、临时目录：不备
SKIP_DIRS = {"legacy", "win-bridge-install", "temp"}
SKIP_SUFFIX = ".moved-to-BWDev"
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
KEEP_DAILY, KEEP_WEEKLY = 7, 4
STATUS = HOME / "BW" / "logs" / "backup.status.json"


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def write_status(payload: dict) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    payload["atLocal"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS or part.endswith(SKIP_SUFFIX) for part in path.parts)


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def snapshots() -> list[Path]:
    if not DEST_ROOT.is_dir():
        return []
    return sorted(p for p in DEST_ROOT.iterdir() if p.is_dir() and not p.name.endswith(".partial"))


def rsync_files(source: Path, target: Path, previous: Path | None) -> None:
    args = ["rsync", "-a", "--delete"]
    args += [f"--exclude={name}/" for name in SKIP_DIRS] + [f"--exclude=*{SKIP_SUFFIX}"]
    args += [f"--exclude=*{suffix}" for suffix in SQLITE_SUFFIXES + SIDE_SUFFIXES]
    if previous is not None and previous.is_dir():
        args.append(f"--link-dest={previous}")
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(args + [f"{source}/", f"{target}/"], check=True)


def backup_databases(source: Path, target: Path, previous: Path | None,
                     previous_started: float | None) -> dict:
    counts = {"copied": 0, "linked": 0, "bytes": 0}
    for root, dirs, files in os.walk(source):
        here = Path(root)
        dirs[:] = [d for d in dirs if not skipped(here / d)]
        for name in files:
            if not name.endswith(SQLITE_SUFFIXES):
                continue
            src = here / name
            rel = src.relative_to(source)
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not is_sqlite(src):          # 叫 .db 但不是 SQLite：当普通文件拷
                shutil.copy2(src, dst)
                counts["copied"] += 1
                continue
            last_write = max((p.stat().st_mtime for p in (src, Path(str(src) + "-wal")) if p.exists()),
                             default=0)
            old = previous / rel if previous else None
            if old is not None and old.is_file() and previous_started and last_write < previous_started:
                os.link(old, dst)
                counts["linked"] += 1
                continue
            partial = dst.with_name(dst.name + ".partial")
            partial.unlink(missing_ok=True)
            origin = sqlite3.connect(src, timeout=60)
            copy = sqlite3.connect(partial)
            try:
                origin.backup(copy, pages=4096)
            finally:
                copy.close()
                origin.close()
            os.replace(partial, dst)
            counts["copied"] += 1
            counts["bytes"] += dst.stat().st_size
    return counts


def prune(keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[str]:
    ordered = snapshots()[::-1]           # 新 → 旧
    keep = set(ordered[:keep_daily])
    weeks: dict[str, Path] = {}
    for snap in ordered:
        try:
            day = dt.datetime.strptime(snap.name[:8], "%Y%m%d")
        except ValueError:
            keep.add(snap)                # 认不出的目录不动
            continue
        weeks.setdefault(day.strftime("%G-W%V"), snap)
    keep.update(list(weeks.values())[:keep_weekly])
    removed = []
    for snap in ordered:
        if snap not in keep:
            shutil.rmtree(snap)
            removed.append(snap.name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="只报告会做什么")
    args = parser.parse_args()
    if not os.path.ismount(VOLUME):
        log(f"跳过：外接盘 {VOLUME} 没挂载（不退回写内置盘）")
        write_status({"ok": False, "skipped": "volume-not-mounted"})
        return 0
    previous_list = snapshots()
    previous = previous_list[-1] if previous_list else None
    previous_started = None
    if previous is not None:
        marker = previous / ".started"
        previous_started = marker.stat().st_mtime if marker.exists() else None
    name = time.strftime("%Y%m%d-%H%M%S")
    if args.dry_run:
        log(f"dry-run：将写 {DEST_ROOT / name}，上一份 {previous.name if previous else '无'}")
        return 0
    started = time.time()
    # 上次中途崩溃留下的半成品：不是快照，也不会被 prune 看到，这里清掉
    for stale in DEST_ROOT.glob("*.partial") if DEST_ROOT.is_dir() else []:
        shutil.rmtree(stale, ignore_errors=True)
    partial = DEST_ROOT / (name + ".partial")
    partial.mkdir(parents=True, exist_ok=False)
    (partial / ".started").touch()
    report: dict = {"snapshot": name, "sources": {}}
    try:
        for label, source in SOURCES.items():
            target = partial / label
            prior = previous / label if previous else None
            rsync_files(source, target, prior)
            report["sources"][label] = backup_databases(source, target, prior, previous_started)
            log(f"{label}: {report['sources'][label]}")
    except Exception as error:  # noqa: BLE001 —— 失败必须出声，且留下的 .partial 不会被当成快照
        log(f"失败：{error}")
        write_status({"ok": False, "error": str(error)[:400], **report})
        return 1
    partial.rename(DEST_ROOT / name)
    report["removed"] = prune()
    report["seconds"] = round(time.time() - started, 1)
    report["free"] = shutil.disk_usage(VOLUME).free
    log(f"完成 {name}，{report['seconds']}s，清理 {report['removed']}")
    write_status({"ok": True, **report})
    return 0


if __name__ == "__main__":
    sys.exit(main())
