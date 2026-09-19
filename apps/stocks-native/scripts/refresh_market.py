#!/usr/bin/env python3
"""Publish a bounded, read-only SQLite backup for the StocksNative service.

Production inputs are never imported, opened writable, chmod'ed, or copied as a
raw SQLite file. The destination is managed generations plus a single atomic
``current`` symlink. Stable stocks.json/stocks.db/manifest.json aliases point into
current. Existing ordinary files at those output names are left untouched and
cause a failure, so first installation should use a new cache directory.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

GENERATED_NAME = re.compile(r"(?:generation|partial)-[0-9a-f]{32}\Z")
OUTPUT_FILES = ("stocks.json", "stocks.db", "manifest.json")


def source_signature(source: Path):
    signature = {}
    for name in ("stocks.json", "stocks.db", "stocks.db-wal"):
        try:
            stat = (source / name).stat()
            signature[name] = {"mtimeNs": stat.st_mtime_ns, "size": stat.st_size}
        except FileNotFoundError:
            if name != "stocks.db-wal":
                raise
            signature[name] = None
    return signature


def remove_generated(path: Path, root: Path):
    """Only delete this helper's direct, non-symlink generation directories."""
    if not GENERATED_NAME.fullmatch(path.name) or path.is_symlink():
        raise ValueError("Refusing to remove an unmanaged path")
    if path.resolve().parent != root.resolve():
        raise ValueError("Generation path escaped the cache directory")
    if path.exists():
        shutil.rmtree(path)


@contextlib.contextmanager
def exclusive_lock(root: Path):
    with (root / ".refresh.lock").open("a+b") as lock:
        if os.name == "posix":
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        else:
            import msvcrt
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def read_json_snapshot(path: Path):
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise RuntimeError("Source JSON changed during read")
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        raise ValueError("Source JSON must contain a rows array")
    if value.get("ok") is False or not value.get("generated_at"):
        raise ValueError("Source JSON is unsuccessful or missing its original data timestamp")
    return raw, value


def backup_worker(source: Path, stage: Path, budget: float):
    deadline = time.monotonic() + budget
    signature = source_signature(source)
    raw, snapshot = read_json_snapshot(source / "stocks.json")

    def check_time(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError("SQLite backup exceeded its time budget")

    # mode=ro still sees live WAL contents. immutable=1 would silently omit them.
    with contextlib.closing(sqlite3.connect(
        (source / "stocks.db").as_uri() + "?mode=ro", uri=True, timeout=0.1,
    )) as origin:
        origin.execute("PRAGMA query_only = ON")
        origin.execute("PRAGMA busy_timeout = 100")
        with contextlib.closing(sqlite3.connect(stage / "stocks.db", timeout=0.1)) as target:
            origin.backup(target, pages=1024, progress=check_time, sleep=0.01)
            check_time()
            # Change only the new copy so a reader needs no writable -wal/-shm.
            mode = target.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise RuntimeError("Backup did not become a standalone SQLite database")
            target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            tables = {row[0] for row in target.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if not {"daily_quotes", "stock_industries"}.issubset(tables):
                raise RuntimeError("Backup is missing the stock data tables")
            latest_quote = target.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0]
            target.commit()
    check_time()
    after_raw, _ = read_json_snapshot(source / "stocks.json")
    if after_raw != raw:
        raise RuntimeError("Source JSON changed while the database was being backed up")
    (stage / "stocks.json").write_bytes(raw)
    # Keep the source's actual data dates. refreshedAt is only a copy timestamp.
    manifest = {
        "schemaVersion": 1,
        "sourceSignature": signature,
        "refreshedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "json": {"asOf": snapshot["generated_at"], "sha256": hashlib.sha256(raw).hexdigest(),
                 "rows": len(snapshot["rows"]), "source": snapshot.get("source")},
        "database": {"asOf": latest_quote, "asOfMeaning": "latest daily_quotes.trade_date",
                     "bytes": (stage / "stocks.db").stat().st_size, "journalMode": "delete"},
        "consistency": "SQLite online backup; source JSON unchanged during backup; original source dates retained",
    }
    (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    for name in OUTPUT_FILES:
        with (stage / name).open("r+b") as stream:
            os.fsync(stream.fileno())
    # There must be no required WAL content left next to the published DB.
    if any((stage / ("stocks.db" + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RuntimeError("Backup still has SQLite sidecar files")


def reader_identity(owner: str, group: str):
    if owner:
        if os.name != "posix":
            raise ValueError("--owner is supported only on POSIX hosts")
        import pwd
        account = pwd.getpwnam(owner)
        return account.pw_uid, account.pw_gid
    name = group
    if not name:
        return None, None
    if os.name != "posix":
        raise ValueError("Use --reader-group '' for a local Windows fixture")
    import grp
    return None, grp.getgrnam(name).gr_gid


def apply_permissions(path: Path, identity, directory=False):
    path.chmod(0o750 if directory else 0o640)
    uid, gid = identity
    if gid is not None:
        os.chown(path, uid if uid is not None else -1, gid)


def validate_output(root: Path):
    for name in OUTPUT_FILES:
        path = root / name
        if path.is_symlink():
            if os.readlink(path) != "current/" + name:
                raise RuntimeError("Cache contains an unmanaged output symlink")
        elif path.exists():
            raise RuntimeError("Use a new cache directory; ordinary output files are preserved")
    current = root / "current"
    if current.is_symlink():
        target = os.readlink(current)
        if not re.fullmatch(r"generation-[0-9a-f]{32}", target):
            raise RuntimeError("Cache current points outside a managed generation")
    elif current.exists():
        raise RuntimeError("Cache current is not a managed symlink")


def publish(args):
    source = Path(args.source).resolve(strict=True)
    root = Path(args.destination).resolve()
    if root == source or source in root.parents or root in source.parents:
        raise ValueError("Source and destination must be separate directories")
    identity = reader_identity(args.owner, args.reader_group)
    root.mkdir(parents=True, exist_ok=True)
    apply_permissions(root, identity, directory=True)
    with exclusive_lock(root):
        validate_output(root)
        with contextlib.suppress(OSError, ValueError, KeyError):
            previous = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            if previous.get("sourceSignature") == source_signature(source):
                print(json.dumps({"ok": True, "skipped": True, "asOf": previous["json"]["asOf"],
                                  "candlesAsOf": previous["database"]["asOf"]}, ensure_ascii=False))
                return
        generation_id = uuid.uuid4().hex
        stage = root / ("partial-" + generation_id)
        generation = root / ("generation-" + generation_id)
        pointer = root / (".current-" + generation_id)
        stage.mkdir(mode=0o700)
        published = False
        try:
            command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                       "--source", str(source), "--destination", str(stage),
                       "--timeout", str(max(0.01, args.timeout - 1))]
            # Hard process deadline also covers unexpected SQLite busy loops or I/O.
            result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
            if result.returncode:
                raise RuntimeError((result.stdout.strip() or "Snapshot worker failed")[:240])
            manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            for name in OUTPUT_FILES:
                apply_permissions(stage / name, identity)
            apply_permissions(stage, identity, directory=True)
            stage.rename(generation)
            # Aliases are installed only on first use and resolve through the same
            # atomic pointer. They never replace any existing ordinary files.
            for name in OUTPUT_FILES:
                alias = root / name
                if not alias.is_symlink():
                    os.symlink("current/" + name, alias)
            os.symlink(generation.name, pointer, target_is_directory=True)
            os.replace(pointer, root / "current")
            published = True
            print(json.dumps({"ok": True, "asOf": manifest["json"]["asOf"],
                              "candlesAsOf": manifest["database"]["asOf"],
                              "rows": manifest["json"]["rows"]}, ensure_ascii=False))
            # Keep two older generations for in-flight readers. Cleanup failure
            # cannot undo publication and must not report the refreshed cache bad.
            with contextlib.suppress(OSError):
                complete = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                                   and re.fullmatch(r"generation-[0-9a-f]{32}", p.name)),
                                  key=lambda p: p.stat().st_mtime_ns, reverse=True)
                for old in complete[3:]:
                    if old != generation:
                        remove_generated(old, root)
        finally:
            if pointer.is_symlink():
                pointer.unlink()
            if stage.exists():
                remove_generated(stage, root)
            if not published and generation.exists():
                remove_generated(generation, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="/root/webapp/data/stocks")
    parser.add_argument("--destination", default="/var/lib/stocks-native/market")
    parser.add_argument("--reader-group", default="stocks-native")
    parser.add_argument("--owner", default="", help="Own only the new cache files as this POSIX account")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and no more than 120 seconds")
    try:
        if args.worker:
            backup_worker(Path(args.source).resolve(strict=True), Path(args.destination), args.timeout)
        else:
            publish(args)
    except Exception as error:
        # No source paths, credentials or record data appear in operational logs.
        message = "snapshot deadline exceeded" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        print(json.dumps({"ok": False, "error": message[:240]}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
