#!/usr/bin/env python3
"""Build and atomically publish the locked Windows extension test channel."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterator
import zipfile

try:
    import fcntl
except ImportError:  # pragma: no cover - deployment runs on Linux
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows-only release tooling
    msvcrt = None

import release_preflight as contract


HERE = pathlib.Path(__file__).resolve().parent
EXTENSIONS = HERE.parent
DEPLOY = contract.DEPLOY_ROOT
DEPLOY_BACKUP_ROOT = pathlib.Path("/home/bwicarus/deploy-backups/reader")
LAUNCHER_VERSION = contract.LAUNCHER_VERSION
WEB_TEST_URL = contract.WEB_TEST_URL
CHANNEL_BACKUP_SCHEMA = 1
WINDOWS_LOCK_RETRY_SECONDS = 0.05
WINDOWS_LOCK_BUSY_ERRNOS = frozenset({
    errno.EACCES,
    errno.EAGAIN,
    errno.EDEADLK,
})


@dataclass(frozen=True)
class ChannelBackup:
    directory: pathlib.Path
    record_path: pathlib.Path
    payload_path: pathlib.Path | None
    original_state: str
    original_sha256: str | None
    original_mode: int | None
    candidate_sha256: str


def fail(message: str) -> None:
    raise SystemExit(f"BLOCKED: {message}")


def report_status(message: str) -> None:
    """Never let a closed diagnostic stream change publication semantics."""

    try:
        print(message, file=sys.stderr)
    except OSError:
        pass


@contextmanager
def windows_process_lock(
    handle: object,
    *,
    sleeper: object = time.sleep,
) -> Iterator[None]:
    """Lock byte zero with Windows kernel ownership and retry contention."""

    if msvcrt is None:
        fail("Windows 平台缺少 msvcrt 发布进程锁")
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError as error:
            if error.errno not in WINDOWS_LOCK_BUSY_ERRNOS:
                raise
            sleeper(WINDOWS_LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def process_lock(path: pathlib.Path) -> Iterator[None]:
    """Serialize publishers without leaving lock artifacts in the workspace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            with windows_process_lock(handle):
                yield
            return
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        fail("当前平台不支持发布所需的进程锁")


def lock_path(kind: str, target: pathlib.Path) -> pathlib.Path:
    identity = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:16]
    return pathlib.Path(tempfile.gettempdir()) / f"bw-webext-{kind}-{identity}.lock"


def write_deterministic_zip(
    path: pathlib.Path,
    payload: dict[str, bytes],
) -> None:
    with zipfile.ZipFile(
        path,
        "w",
        zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name, content in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100644 << 16)
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)


def reuse_or_build_zip(
    *,
    existing: pathlib.Path,
    staging: pathlib.Path,
    payload: dict[str, bytes],
    label: str,
) -> None:
    if existing.exists() or existing.is_symlink():
        try:
            contract.audit_zip_exact(existing, payload, label=label)
        except SystemExit:
            fail(
                f"{label} {existing.name} 已存在但内容不同；"
                "请提升对应版本，禁止覆盖旧 ZIP"
            )
        write_regular(staging, contract.read_regular_file(existing))
        return
    write_deterministic_zip(staging, payload)


def write_regular(path: pathlib.Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: pathlib.Path) -> None:
    if os.name == "nt":
        # Windows does not allow os.open() on a directory.  File payloads are
        # still flushed before ReplaceFile/rename; directory-handle durability
        # is a POSIX-only strengthening used by the Pi publisher.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: pathlib.Path,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    """Durably replace a private audit record in one directory."""

    descriptor, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=path.name + ".",
    )
    temp_path = pathlib.Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
        path.chmod(mode)
        fsync_directory(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_backup_record(
    backup: ChannelBackup,
    record: dict[str, object],
) -> None:
    atomic_write_bytes(
        backup.record_path,
        (
            json.dumps(
                record,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def prepare_channel_backup(
    *,
    target: pathlib.Path,
    candidate: pathlib.Path,
    version: str,
    backup_root: pathlib.Path,
) -> tuple[ChannelBackup, dict[str, object]]:
    """Save a durable exact pre-write snapshot of the mutable channel."""

    candidate_bytes = contract.read_regular_file(candidate)
    candidate_sha256 = contract.sha256_bytes(candidate_bytes)

    backup_root.mkdir(parents=True, exist_ok=True)
    root_mode = os.lstat(backup_root).st_mode
    if not stat.S_ISDIR(root_mode) or stat.S_ISLNK(root_mode):
        fail(f"部署备份根必须是实体目录: {backup_root}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f"webext-channel-{stamp}-",
            dir=backup_root,
        )
    )
    directory.chmod(0o700)
    fsync_directory(backup_root)

    payload_path: pathlib.Path | None = None
    original_sha256: str | None = None
    original_mode: int | None = None
    if target.is_symlink():
        fail(f"生产 channel 不允许是符号链接: {target}")
    if target.exists():
        original_bytes = contract.read_regular_file(target)
        target_stat = os.stat(target, follow_symlinks=False)
        original_mode = stat.S_IMODE(target_stat.st_mode)
        original_sha256 = contract.sha256_bytes(original_bytes)
        payload_path = directory / "channel.before"
        write_regular(payload_path, original_bytes)
        payload_path.chmod(0o400)
        fsync_directory(directory)
        original: dict[str, object] = {
            "state": "present",
            "sha256": original_sha256,
            "bytes": len(original_bytes),
            "mode": f"{original_mode:04o}",
            "backupFile": payload_path.name,
        }
        original_state = "present"
    else:
        original = {
            "state": "missing",
            "sha256": None,
            "bytes": 0,
            "mode": None,
            "backupFile": None,
        }
        original_state = "missing"

    backup = ChannelBackup(
        directory=directory,
        record_path=directory / "channel-deploy.json",
        payload_path=payload_path,
        original_state=original_state,
        original_sha256=original_sha256,
        original_mode=original_mode,
        candidate_sha256=candidate_sha256,
    )
    record: dict[str, object] = {
        "schema": CHANNEL_BACKUP_SCHEMA,
        "kind": "bw-reader-webext-test-channel",
        "status": "prepared",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "target": str(target),
        "candidate": {
            "version": version,
            "filename": candidate.name,
            "source": str(candidate),
            "sha256": candidate_sha256,
            "bytes": len(candidate_bytes),
        },
        "original": original,
        "rollback": {
            "attempted": False,
            "verified": False,
            "result": "not-needed",
        },
    }
    write_backup_record(backup, record)
    report_status(
        f"channel-backup={backup.directory} "
        f"original={original_state} "
        f"sha256={original_sha256 or 'missing'}"
    )
    return backup, record


def restore_channel_backup(
    *,
    backup: ChannelBackup,
    target: pathlib.Path,
) -> str:
    """Restore the exact pre-write channel state and verify the result."""

    if backup.original_state == "present":
        if backup.payload_path is None or backup.original_sha256 is None:
            fail("channel 回滚记录缺少原始字节证明")
        atomic_copy(backup.payload_path, target)
        if backup.original_mode is not None:
            target.chmod(backup.original_mode)
        restored = contract.read_regular_file(target)
        if contract.sha256_bytes(restored) != backup.original_sha256:
            fail("channel 回滚后 SHA-256 与备份不一致")
        return "restored"

    if backup.original_state != "missing":
        fail(f"未知 channel 原始状态: {backup.original_state}")
    if target.is_symlink():
        fail("拒绝删除回滚期间出现的生产 channel 符号链接")
    if target.exists():
        contract.read_regular_file(target)
        target.unlink()
        fsync_directory(target.parent)
    if target.exists() or target.is_symlink():
        fail("原始 channel 缺失，但回滚后目标仍存在")
    return "removed"


def build_candidate(staging_root: pathlib.Path) -> dict[str, pathlib.Path | str]:
    manifest = contract.read_json(HERE / "manifest.json")
    version = str(manifest.get("version", ""))
    contract.version_tuple(version)
    launcher_version = contract.source_launcher_version(HERE)

    package_filename = contract.package_name(version)
    launcher_filename = contract.launcher_archive_name(launcher_version)
    launcher_script_filename = contract.launcher_script_name(launcher_version)
    package_path = staging_root / package_filename
    launcher_path = staging_root / launcher_filename
    launcher_script_path = staging_root / launcher_script_filename
    channel_path = staging_root / contract.CHANNEL_FILENAME

    package_payload = contract.package_source_snapshot(HERE)
    launcher_payload = contract.launcher_source_snapshot(HERE)
    reuse_or_build_zip(
        existing=EXTENSIONS / package_filename,
        staging=package_path,
        payload=package_payload,
        label="Windows 测试包",
    )
    reuse_or_build_zip(
        existing=EXTENSIONS / launcher_filename,
        staging=launcher_path,
        payload=launcher_payload,
        label="launcher ZIP",
    )
    write_regular(launcher_script_path, launcher_payload[contract.LAUNCHER_PS1])

    channel = contract.make_channel(
        version=version,
        package_sha256=contract.sha256_file(package_path),
        launcher_version=launcher_version,
        launcher_sha256=contract.sha256_file(launcher_script_path),
    )
    write_regular(
        channel_path,
        (json.dumps(channel, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    contract.audit_artifact(
        package_path=package_path,
        channel_path=channel_path,
        launcher_script_path=launcher_script_path,
        launcher_archive_path=launcher_path,
        version=version,
        source_root=HERE,
    )
    candidate: dict[str, pathlib.Path | str] = {
        "version": version,
        "package": package_path,
        "launcher_archive": launcher_path,
        "launcher_script": launcher_script_path,
        "channel": channel_path,
    }
    # Detect all local immutable-name conflicts before a deployment can start.
    for key in ("package", "launcher_archive", "launcher_script"):
        source = pathlib.Path(candidate[key])
        assert_immutable_compatible(
            source,
            EXTENSIONS / source.name,
            label="本地版本化生成物",
        )
    return candidate


def assert_immutable_compatible(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    label: str,
) -> None:
    """Reject any existing immutable path whose bytes are not identical."""

    if target.exists() or target.is_symlink():
        try:
            target_bytes = contract.read_regular_file(target)
        except SystemExit as exc:
            fail(f"{label} 不是可安全复用的普通文件: {target}: {exc}")
        if target_bytes != contract.read_regular_file(source):
            fail(
                f"{label} 已存在且内容不同: {target.name}；"
                "请提升 manifest/launcher 版本"
            )


def immutable_copy(source: pathlib.Path, target: pathlib.Path) -> None:
    """Create a versioned artifact without ever replacing an existing path."""

    source_bytes = contract.read_regular_file(source)
    assert_immutable_compatible(source, target, label="版本化生成物")
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        dir=target.parent,
        prefix=target.name + ".",
    )
    temp_path = pathlib.Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o644)
        try:
            os.link(temp_path, target)
        except FileExistsError:
            assert_immutable_compatible(source, target, label="版本化生成物")
        fsync_directory(target.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_copy(source: pathlib.Path, target: pathlib.Path) -> None:
    """Durably replace one file; callers publish the channel pointer last."""

    source_bytes = contract.read_regular_file(source)
    if target.exists() and not target.is_symlink():
        try:
            if contract.read_regular_file(target) == source_bytes:
                return
        except SystemExit:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        dir=target.parent,
        prefix=target.name + ".",
    )
    temp_path = pathlib.Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o644)
        os.replace(temp_path, target)
        target.chmod(0o644)
        fsync_directory(target.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def run_preflight(candidate: dict[str, pathlib.Path | str]) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "release_preflight.py"),
            "--artifact",
            str(candidate["package"]),
            "--channel",
            str(candidate["channel"]),
            "--launcher-script",
            str(candidate["launcher_script"]),
            "--launcher-archive",
            str(candidate["launcher_archive"]),
        ],
        cwd=HERE.parent.parent,
        check=False,
    )
    if result.returncode:
        fail(
            "release_preflight.py 未通过；生产 channel 和生产生成物均未激活"
        )


def publish_candidate(
    candidate: dict[str, pathlib.Path | str],
    *,
    deploy_root: pathlib.Path,
    backup_root: pathlib.Path = DEPLOY_BACKUP_ROOT,
) -> ChannelBackup:
    """Publish immutable assets first and the single mutable pointer last."""

    immutable = [
        pathlib.Path(candidate[key])
        for key in ("package", "launcher_archive", "launcher_script")
    ]
    # Check every target before creating any of them, preventing partial
    # publication when an orphaned versioned filename already conflicts.
    for source in immutable:
        assert_immutable_compatible(
            source,
            deploy_root / source.name,
            label="已部署版本化生成物",
        )
    channel = pathlib.Path(candidate["channel"])
    channel_target = deploy_root / contract.CHANNEL_FILENAME
    backup, record = prepare_channel_backup(
        target=channel_target,
        candidate=channel,
        version=str(candidate["version"]),
        backup_root=backup_root,
    )
    try:
        for source in immutable:
            immutable_copy(source, deploy_root / source.name)
        atomic_copy(channel, channel_target)
        activated = contract.read_regular_file(channel_target)
        activated_sha256 = contract.sha256_bytes(activated)
        if activated_sha256 != backup.candidate_sha256:
            fail("生产 channel 原子切换后的 SHA-256 与候选不一致")
        record["status"] = "committed"
        record["completedAt"] = datetime.now(timezone.utc).isoformat()
        record["activated"] = {
            "sha256": activated_sha256,
            "bytes": len(activated),
        }
        write_backup_record(backup, record)
    except BaseException as publication_error:
        rollback = record["rollback"]
        assert isinstance(rollback, dict)
        rollback["attempted"] = True
        record["failure"] = {
            "type": type(publication_error).__name__,
            "message": str(publication_error),
        }
        try:
            result = restore_channel_backup(
                backup=backup,
                target=channel_target,
            )
            rollback["verified"] = True
            rollback["result"] = result
            record["status"] = "rolled-back"
            record["completedAt"] = datetime.now(timezone.utc).isoformat()
            write_backup_record(backup, record)
            report_status(
                f"channel-rollback=verified result={result} "
                f"backup={backup.directory}"
            )
        except BaseException as rollback_error:
            rollback["verified"] = False
            rollback["result"] = "failed"
            record["status"] = "rollback-failed"
            record["rollbackFailure"] = {
                "type": type(rollback_error).__name__,
                "message": str(rollback_error),
            }
            try:
                write_backup_record(backup, record)
            except BaseException:
                pass
            raise RuntimeError(
                "生产 channel 发布失败且无法证明逐字节回滚；"
                f"备份保留在 {backup.directory}"
            ) from rollback_error
        raise
    return backup


def promote_local(candidate: dict[str, pathlib.Path | str]) -> None:
    for key in ("package", "launcher_archive", "launcher_script"):
        source = pathlib.Path(candidate[key])
        immutable_copy(source, EXTENSIONS / source.name)
    atomic_copy(
        pathlib.Path(candidate["channel"]),
        EXTENSIONS / contract.CHANNEL_FILENAME,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="publish immutable assets and activate the production channel",
    )
    args = parser.parse_args()

    local_lock = lock_path("build", EXTENSIONS)
    deploy_lock = lock_path("deploy", DEPLOY)
    deployed_backup: ChannelBackup | None = None
    with ExitStack() as stack:
        stack.enter_context(process_lock(local_lock))
        if args.deploy:
            stack.enter_context(process_lock(deploy_lock))
        with tempfile.TemporaryDirectory(
            prefix="bw-webext-candidate-",
        ) as raw_staging:
            candidate = build_candidate(pathlib.Path(raw_staging))
            if args.deploy:
                # This subprocess re-reads the deployed channel while the
                # deploy lock is held, closing the monotonic-version race.
                run_preflight(candidate)
                deployed_backup = publish_candidate(
                    candidate,
                    deploy_root=DEPLOY,
                )
            promote_local(candidate)

    package_path = EXTENSIONS / pathlib.Path(candidate["package"]).name
    channel_path = EXTENSIONS / contract.CHANNEL_FILENAME
    print(
        f"{package_path.name} "
        f"sha256={contract.sha256_file(package_path)}"
    )
    print(f"channel={channel_path}")
    if args.deploy:
        print(f"deployed={DEPLOY}")
        assert deployed_backup is not None
        print(f"channel-backup={deployed_backup.directory}")
        print(f"channel-backup-record={deployed_backup.record_path}")
        print("channel-rollback=not-needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
