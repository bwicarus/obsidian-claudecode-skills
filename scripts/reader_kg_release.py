#!/usr/bin/env python3
"""Build, publish, select and verify immutable reader KG runtimes.

The repository is a data/configuration root in production, not a code release.
This helper copies the explicit ``kg_runtime`` deploy-manifest group into a
content-addressed, read-only release and atomically selects it with ``current``.
All mutating operations are parameterized so the same code can be exercised in
temporary directories before it is allowed near the production root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import uuid


RUNTIME_CONTRACT = "bw-reader-kg-runtime/1"
DATA_SCHEMA_MIN = "kg-node-history/1"
DATA_SCHEMA_MAX = "kg-node-history/1"
MISSING_CURRENT = "-"
_DEPLOY_ID_RE = re.compile(r"kg-[0-9]+(?:\.[0-9]+){2,3}-[0-9a-f]{20}")
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2,3}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MANIFEST_KEYS = {
    "contract",
    "deployId",
    "readerVersion",
    "dataSchemaMin",
    "dataSchemaMax",
    "files",
    "manifestDigest",
}


class ReleaseError(RuntimeError):
    """The release or current pointer cannot be proven safe."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_relative(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in ("\x00", "\t", "\r", "\n", "\\"))
    ):
        raise ReleaseError(f"unsafe runtime path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ReleaseError(f"unsafe runtime path: {value!r}")
    return value


def _safe_deploy_id(value: str) -> str:
    if not _DEPLOY_ID_RE.fullmatch(str(value or "")):
        raise ReleaseError(f"unsafe KG release id: {value!r}")
    return value


def _regular_file_bytes(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ReleaseError(f"runtime file cannot be inspected: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ReleaseError(f"runtime entry is not a regular file: {path}")
    return path.read_bytes()


def _tree_files(root: Path, *, include_marker: bool = False) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError(f"runtime stage is not a real directory: {root}")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ReleaseError(f"runtime contains symlink: {relative}")
        if path.is_dir():
            continue
        if relative == "runtime-manifest.json" and not include_marker:
            continue
        value = _regular_file_bytes(path)
        result[_safe_relative(relative)] = hashlib.sha256(value).hexdigest()
    if not result:
        raise ReleaseError("runtime stage contains no files")
    return result


def make_manifest(stage: Path, reader_version: str) -> dict:
    """Return a deterministic manifest for an unsealed staging tree."""

    if not _VERSION_RE.fullmatch(str(reader_version or "")):
        raise ReleaseError(f"invalid reader version: {reader_version!r}")
    files = _tree_files(stage)
    identity = {
        "contract": RUNTIME_CONTRACT,
        "readerVersion": reader_version,
        "dataSchemaMin": DATA_SCHEMA_MIN,
        "dataSchemaMax": DATA_SCHEMA_MAX,
        "files": files,
    }
    content_digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    deploy_id = f"kg-{reader_version}-{content_digest[:20]}"
    manifest = {**identity, "deployId": deploy_id}
    manifest["manifestDigest"] = hashlib.sha256(
        _canonical_json(manifest)
    ).hexdigest()
    return manifest


def write_manifest(stage: Path, reader_version: str) -> dict:
    marker = stage / "runtime-manifest.json"
    if marker.exists() or marker.is_symlink():
        raise ReleaseError("runtime stage already contains a manifest")
    manifest = make_manifest(stage, reader_version)
    marker.write_bytes(_canonical_json(manifest) + b"\n")
    _fsync_file(marker)
    _fsync_dir(stage)
    return manifest


def _read_manifest(release: Path) -> dict:
    marker = release / "runtime-manifest.json"
    def no_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ReleaseError("runtime manifest has duplicate fields")
            result[key] = item
        return result
    try:
        value = json.loads(
            _regular_file_bytes(marker).decode("utf-8"),
            object_pairs_hook=no_duplicates,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ReleaseError(f"invalid runtime manifest: {marker}") from exc
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise ReleaseError("runtime manifest field set is invalid")
    body = {key: item for key, item in value.items() if key != "manifestDigest"}
    if (
        value.get("contract") != RUNTIME_CONTRACT
        or not _VERSION_RE.fullmatch(str(value.get("readerVersion") or ""))
        or value.get("dataSchemaMin") != DATA_SCHEMA_MIN
        or value.get("dataSchemaMax") != DATA_SCHEMA_MAX
        or not _DEPLOY_ID_RE.fullmatch(str(value.get("deployId") or ""))
        or not isinstance(value.get("files"), dict)
        or value.get("manifestDigest")
        != hashlib.sha256(_canonical_json(body)).hexdigest()
    ):
        raise ReleaseError("runtime manifest identity or digest is invalid")
    identity = {
        "contract": value["contract"],
        "readerVersion": value["readerVersion"],
        "dataSchemaMin": value["dataSchemaMin"],
        "dataSchemaMax": value["dataSchemaMax"],
        "files": value["files"],
    }
    expected_id = (
        f"kg-{value['readerVersion']}-"
        f"{hashlib.sha256(_canonical_json(identity)).hexdigest()[:20]}"
    )
    if value["deployId"] != expected_id:
        raise ReleaseError("runtime deployId is not content-addressed")
    return value


def verify_release(release: Path, *, require_sealed: bool = False) -> dict:
    """Verify an exact, non-symlink release tree and return its marker."""

    if release.is_symlink() or not release.is_dir():
        raise ReleaseError(f"release is not a real directory: {release}")
    manifest = _read_manifest(release)
    if release.name != manifest["deployId"]:
        raise ReleaseError("release directory name does not match deployId")
    actual = _tree_files(release)
    expected = manifest["files"]
    if set(actual) != set(expected):
        raise ReleaseError("runtime release file set differs from manifest")
    for relative, digest in expected.items():
        _safe_relative(str(relative))
        if not _DIGEST_RE.fullmatch(str(digest or "")):
            raise ReleaseError(f"invalid runtime digest: {relative}")
        if actual[relative] != digest:
            raise ReleaseError(f"runtime digest mismatch: {relative}")
    if require_sealed:
        for path in (release, *release.rglob("*")):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ReleaseError(f"sealed runtime contains symlink: {path}")
            if mode & 0o222:
                raise ReleaseError(f"sealed runtime remains writable: {path}")
    return manifest


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReleaseError(f"runtime contains symlink: {path}")
        if path.is_file():
            _fsync_file(path)
        elif path.is_dir():
            directories.append(path)
        else:
            raise ReleaseError(f"runtime contains unsupported entry: {path}")
    for directory in reversed(directories):
        _fsync_dir(directory)


def _seal_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
        else:
            path.chmod(0o444)
    root.chmod(0o555)


def _require_real_directory(path: Path, *, label: str) -> None:
    """Reject managed runtime directories that are redirected by symlinks."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ReleaseError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise ReleaseError(f"{label} cannot be inspected: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReleaseError(f"{label} is not a real directory: {path}")


def publish(stage: Path, runtime_root: Path) -> dict:
    """Copy a prepared stage into releases and publish it without overwrite."""

    manifest = _read_manifest(stage)
    deploy_id = _safe_deploy_id(manifest["deployId"])
    if stage.name == deploy_id:
        verify_release(stage)
    else:
        # A deploy stage is allowed to have an arbitrary temporary basename.
        files = _tree_files(stage)
        if files != manifest["files"]:
            raise ReleaseError("runtime stage differs from its manifest")

    if runtime_root.exists() or runtime_root.is_symlink():
        _require_real_directory(runtime_root, label="runtime root")
    else:
        runtime_root.mkdir(parents=True)
    releases = runtime_root / "releases"
    releases.mkdir(exist_ok=True)
    _require_real_directory(runtime_root, label="runtime root")
    _require_real_directory(releases, label="runtime releases")
    _fsync_dir(runtime_root)
    _fsync_dir(releases)
    final = releases / deploy_id
    if final.exists() or final.is_symlink():
        existing = verify_release(final, require_sealed=True)
        if existing != manifest:
            raise ReleaseError("existing immutable release has different bytes")
        return existing

    temporary = releases / f".stage-{deploy_id}-{uuid.uuid4().hex}"
    try:
        shutil.copytree(stage, temporary, symlinks=False)
        _fsync_tree(temporary)
        _seal_tree(temporary)
        # Validate after sealing and before the only publishing rename.
        staged_manifest = _read_manifest(temporary)
        if staged_manifest != manifest:
            raise ReleaseError("copied runtime manifest changed")
        if _tree_files(temporary) != manifest["files"]:
            raise ReleaseError("copied runtime files changed")
        os.rename(temporary, final)
        _fsync_dir(releases)
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            for path in temporary.rglob("*"):
                try:
                    path.chmod(0o755 if path.is_dir() else 0o644)
                except OSError:
                    pass
            temporary.chmod(0o755)
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    verify_release(final, require_sealed=True)
    return manifest


def current_id(runtime_root: Path) -> str | None:
    if runtime_root.exists() or runtime_root.is_symlink():
        _require_real_directory(runtime_root, label="runtime root")
    current = runtime_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise ReleaseError("KG current is not a symlink")
    releases = runtime_root / "releases"
    _require_real_directory(releases, label="runtime releases")
    raw = os.readlink(current)
    raw_path = PurePosixPath(raw)
    expected = PurePosixPath("releases") / raw_path.name
    if raw_path.as_posix() != raw or raw_path != expected:
        raise ReleaseError("KG current is not a direct relative release link")
    deploy_id = _safe_deploy_id(PurePosixPath(raw).name)
    release = runtime_root / "releases" / deploy_id
    verify_release(release, require_sealed=True)
    return deploy_id


def switch_current(
    runtime_root: Path,
    deploy_id: str | None,
    *,
    expected: str | None,
) -> None:
    """CAS-switch current, including the explicit no-current bootstrap state."""

    actual = current_id(runtime_root)
    if actual != expected:
        raise ReleaseError(
            f"KG current changed concurrently: expected {expected!r}, "
            f"found {actual!r}"
        )
    if runtime_root.exists() or runtime_root.is_symlink():
        _require_real_directory(runtime_root, label="runtime root")
    else:
        runtime_root.mkdir(parents=True)
    _require_real_directory(runtime_root, label="runtime root")
    current = runtime_root / "current"
    if deploy_id is None:
        if current.is_symlink():
            current.unlink()
            _fsync_dir(runtime_root)
        return
    deploy_id = _safe_deploy_id(deploy_id)
    verify_release(
        runtime_root / "releases" / deploy_id,
        require_sealed=True,
    )
    temporary = runtime_root / f".current-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(PurePosixPath("releases") / deploy_id)
        os.replace(temporary, current)
        _fsync_dir(runtime_root)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    if current_id(runtime_root) != deploy_id:
        raise ReleaseError("KG current verification failed after switch")


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--stage", type=Path, required=True)
    prepare.add_argument("--reader-version", required=True)
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--stage", type=Path, required=True)
    publish_parser.add_argument("--runtime-root", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--runtime-root", type=Path, required=True)
    verify.add_argument("--release-id")
    current = sub.add_parser("current")
    current.add_argument("--runtime-root", type=Path, required=True)
    switch = sub.add_parser("switch")
    switch.add_argument("--runtime-root", type=Path, required=True)
    switch.add_argument("--release-id", required=True)
    switch.add_argument("--expected", required=True)
    args = parser.parse_args(argv)

    if args.command == "prepare":
        _print(write_manifest(args.stage, args.reader_version))
    elif args.command == "publish":
        _print(publish(args.stage, args.runtime_root))
    elif args.command == "verify":
        release_id = args.release_id or current_id(args.runtime_root)
        if release_id is None:
            raise ReleaseError("KG current is missing")
        _print(
            verify_release(
                args.runtime_root / "releases" / release_id,
                require_sealed=True,
            )
        )
    elif args.command == "current":
        _print({"deployId": current_id(args.runtime_root)})
    elif args.command == "switch":
        release_id = (
            None if args.release_id == MISSING_CURRENT else args.release_id
        )
        expected = None if args.expected == MISSING_CURRENT else args.expected
        switch_current(args.runtime_root, release_id, expected=expected)
        _print({"deployId": current_id(args.runtime_root)})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
