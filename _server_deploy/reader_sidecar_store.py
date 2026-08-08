"""Account-partitioned storage and one-time legacy reader-sidecar claiming.

This module deliberately has no Flask or application imports.  The application
must supply a verified :class:`ReaderStorageIdentity` and an ``authorize_claim``
callback.  Returning ``True`` from that callback means that the identity is
allowed to claim the fixed legacy dataset set.  Every other identity receives
an isolated account directory and can never read the shared legacy paths.

The legacy claim is copy-only:

1. inventory and hash the fixed legacy paths;
2. create and verify a read-only backup snapshot;
3. copy the snapshot into a staging account directory and verify it;
4. atomically activate ``by-user/<uid>``;
5. write ``legacy-claim.json`` last.

The original legacy files are never moved, rewritten, or deleted.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import uuid
from typing import Any, Callable, Iterator, Sequence

try:  # Linux production path; the thread lock remains the portable fallback.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows deployments.
    fcntl = None


CLAIM_SCHEMA = "reader-sidecar-claim/1"
CLAIM_EXTENSION_SCHEMA = "reader-sidecar-claim-extension/1"
BACKUP_SCHEMA = "reader-sidecar-backup/1"
ACCOUNT_SCHEMA = "reader-sidecar-account/1"
NAMESPACE_RE = re.compile(r"^acct-v1-[a-f0-9]{64}$")

# These are the only shared paths this migration is allowed to inspect/copy.
# Keeping the registry fixed prevents a future unrelated state file from being
# silently assigned to an account merely because it was placed in STATE.
LEGACY_DATASETS: tuple[tuple[str, str], ...] = (
    ("reader-positions.json", "file"),
    ("pdf-phrases.json", "file"),
    ("pdf-phrase-mark.json", "file"),
    ("epub-highlights", "dir"),
    ("reader-notes", "dir"),
    ("assets", "dir"),
    ("pdf-highlights", "dir"),
    ("html-highlights", "dir"),
    ("pdf-ink", "dir"),
    ("epub-ink", "dir"),
    ("reader-userpages", "dir"),
)

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_MISSING = object()


class SidecarStoreError(RuntimeError):
    """Base class for sidecar storage contract failures."""


class InvalidIdentityError(ValueError, SidecarStoreError):
    """The caller did not provide a valid verified account identity."""


class UnsafePathError(ValueError, SidecarStoreError):
    """A requested path escaped its account root or traversed a symlink."""


class LegacySnapshotError(SidecarStoreError):
    """A legacy source or backup could not be snapshotted safely."""


class ClaimConflictError(SidecarStoreError):
    """Existing state disagrees with a requested or recoverable legacy claim."""


class IdentityMismatchError(SidecarStoreError):
    """A uid's recorded namespace differs from the verified identity."""


@dataclass(frozen=True, slots=True)
class ReaderStorageIdentity:
    """Verified immutable identity used by all private reader sidecars."""

    user_id: int
    storage_namespace: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
        ):
            raise InvalidIdentityError("verified positive integer user_id required")
        if (
            not isinstance(self.storage_namespace, str)
            or not NAMESPACE_RE.fullmatch(self.storage_namespace)
        ):
            raise InvalidIdentityError(
                "storage_namespace must match acct-v1- plus 64 lowercase hex digits"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "storage_namespace": self.storage_namespace,
        }


def default_sidecar_root(project_root: str | Path) -> Path:
    """Resolve the shared sidecar root consistently for HTTP and CLI readers."""

    explicit = str(os.environ.get("READER_SIDECAR_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    webapp_data = str(os.environ.get("WEBAPP_DATA") or "").strip()
    if webapp_data:
        return (Path(webapp_data).expanduser() / "reader-sidecars").resolve()
    return (Path(project_root).expanduser() / "state" / "reader-sidecars").resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _reject_symlink(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise UnsafePathError(f"{label} must not be a symlink: {path}")


def _ensure_dir(path: Path, *, label: str) -> None:
    _reject_symlink(path, label=label)
    path.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path, label=label)
    if not path.is_dir():
        raise UnsafePathError(f"{label} is not a directory: {path}")


@contextmanager
def exclusive_lock(path: str | Path) -> Iterator[None]:
    """Hold a process-local and (where supported) OS advisory exclusive lock."""

    lock_path = Path(path).expanduser()
    _ensure_dir(lock_path.parent, label="lock directory")
    _reject_symlink(lock_path, label="lock file")
    local_lock = _thread_lock(lock_path)
    with local_lock:
        handle = lock_path.open("a+b")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # Opening a directory this way is not portable there.
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, payload: bytes, *, mode: int = 0o600) -> Path:
    """Write bytes through a unique same-directory temp file and fsync/replace."""

    target = Path(path)
    _ensure_dir(target.parent, label="atomic-write parent")
    _reject_symlink(target.parent, label="atomic-write parent")
    temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        _fsync_dir(target.parent)
        return target
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> Path:
    return atomic_write_bytes(path, str(text).encode(encoding), mode=mode)


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    indent: int | None = 2,
    mode: int = 0o600,
) -> Path:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    )
    return atomic_write_text(path, payload + "\n", mode=mode)


def read_json(path: str | Path, default: Any = _MISSING) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default


def _hash_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LegacySnapshotError(f"cannot open legacy file safely: {path}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise LegacySnapshotError(f"legacy entry is not a regular file: {path}")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        before_key = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_key = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_key != after_key:
            raise LegacySnapshotError(f"legacy file changed while hashing: {path}")
        return after.st_size, digest.hexdigest()
    finally:
        os.close(fd)


def _inventory_dir(base: Path, current: Path, entries: list[dict[str, Any]]) -> None:
    try:
        children = sorted(os.scandir(current), key=lambda item: item.name)
    except OSError as exc:
        raise LegacySnapshotError(f"cannot enumerate legacy directory: {current}") from exc
    for child in children:
        child_path = Path(child.path)
        rel = child_path.relative_to(base).as_posix()
        try:
            info = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise LegacySnapshotError(f"cannot inspect legacy entry: {child_path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise LegacySnapshotError(f"legacy symlink rejected: {child_path}")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": rel, "type": "dir"})
            _inventory_dir(base, child_path, entries)
        elif stat.S_ISREG(info.st_mode):
            size, digest = _hash_file(child_path)
            entries.append(
                {"path": rel, "type": "file", "size": size, "sha256": digest}
            )
        else:
            raise LegacySnapshotError(f"legacy special file rejected: {child_path}")


def _inventory_declared_datasets(
    root: str | Path,
    datasets: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Inventory an exact subset of the fixed legacy registry."""

    declared = dict(LEGACY_DATASETS)
    base = Path(root).expanduser().resolve()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative, expected_type in datasets:
        if (
            relative in seen
            or declared.get(relative) != expected_type
            or expected_type not in ("file", "dir")
        ):
            raise LegacySnapshotError(
                f"legacy dataset is not in the fixed registry: {relative}"
            )
        seen.add(relative)
        path = base / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise LegacySnapshotError(f"legacy symlink rejected: {path}")
        if expected_type == "file":
            if not stat.S_ISREG(info.st_mode):
                raise LegacySnapshotError(f"legacy file path has wrong type: {path}")
            size, digest = _hash_file(path)
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": size,
                    "sha256": digest,
                }
            )
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise LegacySnapshotError(
                    f"legacy directory path has wrong type: {path}"
                )
            entries.append({"path": relative, "type": "dir"})
            _inventory_dir(base, path, entries)
    return sorted(entries, key=lambda item: (item["path"], item["type"]))


def inventory_legacy(root: str | Path) -> list[dict[str, Any]]:
    """Return a deterministic hash inventory for only ``LEGACY_DATASETS``."""

    return _inventory_declared_datasets(root, LEGACY_DATASETS)


def inventory_digest(inventory: Sequence[dict[str, Any]]) -> str:
    canonical = json.dumps(
        list(inventory),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _copy_inventory(
    source_root: Path,
    destination_root: Path,
    inventory: Sequence[dict[str, Any]],
) -> None:
    _ensure_dir(destination_root, label="snapshot destination")
    for entry in inventory:
        relative = PurePosixPath(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise LegacySnapshotError(f"invalid inventory path: {relative}")
        destination = destination_root.joinpath(*relative.parts)
        if entry["type"] == "dir":
            _ensure_dir(destination, label="snapshot directory")
            continue
        _ensure_dir(destination.parent, label="snapshot file parent")
        source = source_root.joinpath(*relative.parts)
        expected_size = int(entry["size"])
        expected_digest = str(entry["sha256"])
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        source_fd = os.open(source, flags)
        temp = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        destination_fd: int | None = None
        digest = hashlib.sha256()
        copied = 0
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode):
                raise LegacySnapshotError(f"source stopped being regular: {source}")
            destination_fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                copied += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = None
            if copied != expected_size or digest.hexdigest() != expected_digest:
                raise LegacySnapshotError(f"source changed while copying: {source}")
            os.replace(temp, destination)
            _fsync_dir(destination.parent)
        finally:
            os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def _make_tree_read_only(path: Path) -> None:
    for current, directories, files in os.walk(path, topdown=False):
        for filename in files:
            os.chmod(Path(current) / filename, 0o400)
        for dirname in directories:
            os.chmod(Path(current) / dirname, 0o500)
    os.chmod(path, 0o500)


def _make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for current, directories, files in os.walk(path):
        os.chmod(current, 0o700)
        for dirname in directories:
            os.chmod(Path(current) / dirname, 0o700)
        for filename in files:
            os.chmod(Path(current) / filename, 0o600)


def _remove_internal_tree(path: Path) -> None:
    if not path.exists():
        return
    _make_tree_writable(path)
    shutil.rmtree(path)


class SidecarStore:
    """Private account roots plus an authorized, recoverable legacy claim."""

    def __init__(
        self,
        root: str | Path,
        legacy_root: str | Path,
        authorize_claim: Callable[[ReaderStorageIdentity], bool],
    ) -> None:
        if not callable(authorize_claim):
            raise TypeError("authorize_claim must be callable")
        self.root = Path(root).expanduser().resolve()
        self.legacy_root = Path(legacy_root).expanduser().resolve()
        self.authorize_claim = authorize_claim
        self.by_user_root = self.root / "by-user"
        self.backups_root = self.root / "backups"
        self.locks_root = self.root / ".locks"
        self.claim_path = self.root / "legacy-claim.json"
        _ensure_dir(self.root, label="sidecar root")
        _ensure_dir(self.by_user_root, label="sidecar by-user root")
        _ensure_dir(self.backups_root, label="sidecar backup root")
        _ensure_dir(self.locks_root, label="sidecar locks root")

    @staticmethod
    def _validate_identity(identity: ReaderStorageIdentity) -> None:
        if not isinstance(identity, ReaderStorageIdentity):
            raise InvalidIdentityError("ReaderStorageIdentity required")

    def _account_root(self, identity: ReaderStorageIdentity) -> Path:
        self._validate_identity(identity)
        _reject_symlink(self.by_user_root, label="sidecar by-user root")
        candidate = self.by_user_root / str(identity.user_id)
        _reject_symlink(candidate, label="account root")
        resolved_parent = candidate.parent.resolve()
        if resolved_parent != self.by_user_root.resolve():
            raise UnsafePathError("account root escaped by-user root")
        return candidate

    def _safe_account_child(
        self,
        account_root: Path,
        parts: Sequence[str | Path],
    ) -> Path:
        relative_parts: list[str] = []
        for raw_part in parts:
            text = str(raw_part)
            if not text or "\x00" in text or "\\" in text:
                raise UnsafePathError("account path contains an unsafe component")
            pure = PurePosixPath(text)
            if pure.is_absolute():
                raise UnsafePathError("account path must be relative")
            for component in pure.parts:
                if component in ("", ".", ".."):
                    raise UnsafePathError("account path traversal rejected")
                relative_parts.append(component)
        candidate = account_root.joinpath(*relative_parts)
        current = account_root
        _reject_symlink(current, label="account root")
        for component in relative_parts:
            current = current / component
            _reject_symlink(current, label="account child")
        try:
            candidate.resolve().relative_to(account_root.resolve())
        except ValueError as exc:
            raise UnsafePathError("account path escaped account root") from exc
        return candidate

    def _identity_metadata_path(self, account_root: Path) -> Path:
        return account_root / ".reader-account.json"

    def _read_account_metadata(self, account_root: Path) -> dict[str, Any] | None:
        value = read_json(self._identity_metadata_path(account_root), default=None)
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("schema") != ACCOUNT_SCHEMA:
            raise ClaimConflictError(f"invalid account metadata: {account_root}")
        return value

    @staticmethod
    def _metadata_matches(
        metadata: dict[str, Any],
        identity: ReaderStorageIdentity,
    ) -> bool:
        owner = metadata.get("identity")
        return (
            isinstance(owner, dict)
            and owner.get("user_id") == identity.user_id
            and owner.get("storage_namespace") == identity.storage_namespace
        )

    def _ensure_empty_account(
        self,
        identity: ReaderStorageIdentity,
        account_root: Path,
    ) -> None:
        if account_root.exists():
            metadata = self._read_account_metadata(account_root)
            if metadata is None:
                raise ClaimConflictError(
                    f"unidentified pre-existing account directory: {account_root}"
                )
            if not self._metadata_matches(metadata, identity):
                raise IdentityMismatchError(
                    f"uid {identity.user_id} is bound to another storage namespace"
                )
            if metadata.get("legacy_claim") is not None:
                raise ClaimConflictError(
                    "legacy-bearing account cannot be treated as an empty account"
                )
            return
        staging = self.by_user_root / (
            f".account-{identity.user_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            _ensure_dir(staging, label="empty account staging")
            atomic_write_json(
                self._identity_metadata_path(staging),
                {
                    "schema": ACCOUNT_SCHEMA,
                    "identity": identity.as_dict(),
                    "created_at": _utc_now(),
                    "legacy_claim": None,
                },
            )
            _fsync_dir(staging)
            os.replace(staging, account_root)
            _fsync_dir(self.by_user_root)
        finally:
            _remove_internal_tree(staging)

    @staticmethod
    def _validate_claim_datasets(value: Any, *, label: str) -> tuple[str, ...]:
        declared = dict(LEGACY_DATASETS)
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or item not in declared for item in value)
            or len(value) != len(set(value))
        ):
            raise ClaimConflictError(f"invalid legacy claim {label} datasets")
        return tuple(value)

    @staticmethod
    def _inventory_uses_only(
        inventory: Sequence[dict[str, Any]],
        datasets: Sequence[str],
    ) -> bool:
        allowed = set(datasets)
        return all(
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and entry["path"].split("/", 1)[0] in allowed
            for entry in inventory
        )

    def _validate_claim_extension(
        self,
        value: Any,
        *,
        claimed: set[str],
    ) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("schema") != CLAIM_EXTENSION_SCHEMA
            or not isinstance(value.get("claim_id"), str)
            or not value.get("claim_id")
            or not isinstance(value.get("claimed_at"), str)
        ):
            raise ClaimConflictError("invalid legacy claim extension")
        datasets = self._validate_claim_datasets(
            value.get("datasets"),
            label="extension",
        )
        if not datasets or claimed.intersection(datasets):
            raise ClaimConflictError("legacy claim extension datasets overlap")
        inventories: list[list[dict[str, Any]]] = []
        for section in ("source", "backup", "account"):
            detail = value.get(section)
            if (
                not isinstance(detail, dict)
                or not isinstance(detail.get("inventory"), list)
                or detail.get("digest")
                != inventory_digest(detail.get("inventory", []))
                or not self._inventory_uses_only(detail["inventory"], datasets)
            ):
                raise ClaimConflictError(
                    f"invalid legacy claim extension {section} inventory"
                )
            inventories.append(detail["inventory"])
        if (
            inventories[1] != inventories[0]
            or inventories[2] != inventories[0]
        ):
            raise ClaimConflictError("legacy claim extension copies disagree")
        claimed.update(datasets)
        return value

    def _validate_claim(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("schema") != CLAIM_SCHEMA:
            raise ClaimConflictError("invalid legacy claim manifest")
        owner = value.get("owner")
        if (
            not isinstance(owner, dict)
            or isinstance(owner.get("user_id"), bool)
            or not isinstance(owner.get("user_id"), int)
            or owner["user_id"] <= 0
            or not isinstance(owner.get("storage_namespace"), str)
            or not NAMESPACE_RE.fullmatch(owner["storage_namespace"])
        ):
            raise ClaimConflictError("invalid legacy claim owner")
        for section in ("source", "backup", "account"):
            detail = value.get(section)
            if (
                not isinstance(detail, dict)
                or not isinstance(detail.get("inventory"), list)
                or detail.get("digest")
                != inventory_digest(detail.get("inventory", []))
            ):
                raise ClaimConflictError(f"invalid legacy claim {section} inventory")
        if not isinstance(value.get("claim_id"), str):
            raise ClaimConflictError("invalid legacy claim id")
        claimed = set(
            self._validate_claim_datasets(
                value.get("datasets"),
                label="base",
            )
        )
        if not self._inventory_uses_only(value["source"]["inventory"], claimed):
            raise ClaimConflictError("legacy claim source escaped declared datasets")
        extensions = value.get("extensions", [])
        if not isinstance(extensions, list):
            raise ClaimConflictError("invalid legacy claim extensions")
        for extension in extensions:
            self._validate_claim_extension(extension, claimed=claimed)
        return value

    def read_claim(self) -> dict[str, Any] | None:
        value = read_json(self.claim_path, default=None)
        return None if value is None else self._validate_claim(value)

    def _claim_id(
        self,
        identity: ReaderStorageIdentity,
        source_digest: str,
    ) -> str:
        namespace_tag = hashlib.sha256(
            identity.storage_namespace.encode("ascii")
        ).hexdigest()[:12]
        return f"u{identity.user_id}-{namespace_tag}-{source_digest[:20]}"

    def _extension_claim_id(
        self,
        identity: ReaderStorageIdentity,
        datasets: Sequence[tuple[str, str]],
        source_digest: str,
    ) -> str:
        namespace_tag = hashlib.sha256(
            identity.storage_namespace.encode("ascii")
        ).hexdigest()[:12]
        dataset_tag = hashlib.sha256(
            json.dumps(
                [name for name, _kind in datasets],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        return (
            f"u{identity.user_id}-{namespace_tag}-ext-{dataset_tag}-"
            f"{source_digest[:20]}"
        )

    def _backup_paths(self, claim_id: str) -> tuple[Path, Path, Path]:
        root = self.backups_root / claim_id
        return root, root / "data", root / "snapshot.json"

    def _validate_backup(
        self,
        claim_id: str,
        identity: ReaderStorageIdentity,
        source_inventory: list[dict[str, Any]],
        source_digest: str,
    ) -> tuple[Path, list[dict[str, Any]]]:
        backup_root, data_root, snapshot_path = self._backup_paths(claim_id)
        snapshot = read_json(snapshot_path)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("schema") != BACKUP_SCHEMA
            or snapshot.get("claim_id") != claim_id
            or snapshot.get("owner") != identity.as_dict()
        ):
            raise ClaimConflictError("existing legacy backup identity mismatch")
        recorded = snapshot.get("source")
        if (
            not isinstance(recorded, dict)
            or recorded.get("inventory") != source_inventory
            or recorded.get("digest") != source_digest
        ):
            raise ClaimConflictError("existing legacy backup source mismatch")
        backup_inventory = inventory_legacy(data_root)
        if (
            backup_inventory != source_inventory
            or inventory_digest(backup_inventory) != source_digest
        ):
            raise ClaimConflictError("existing legacy backup content mismatch")
        return backup_root, backup_inventory

    def _create_or_reuse_backup(
        self,
        claim_id: str,
        identity: ReaderStorageIdentity,
        source_inventory: list[dict[str, Any]],
        source_digest: str,
    ) -> tuple[Path, list[dict[str, Any]], str]:
        backup_root, data_root, snapshot_path = self._backup_paths(claim_id)
        if backup_root.exists():
            existing_root, existing_inventory = self._validate_backup(
                claim_id,
                identity,
                source_inventory,
                source_digest,
            )
            return existing_root, existing_inventory, inventory_digest(
                existing_inventory
            )

        staging = self.backups_root / (
            f".{claim_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            staging_data = staging / "data"
            _ensure_dir(staging_data, label="legacy backup staging data")
            _copy_inventory(self.legacy_root, staging_data, source_inventory)
            backup_inventory = inventory_legacy(staging_data)
            backup_digest = inventory_digest(backup_inventory)
            if (
                backup_inventory != source_inventory
                or backup_digest != source_digest
            ):
                raise LegacySnapshotError("legacy backup verification failed")
            atomic_write_json(
                staging / "snapshot.json",
                {
                    "schema": BACKUP_SCHEMA,
                    "claim_id": claim_id,
                    "owner": identity.as_dict(),
                    "created_at": _utc_now(),
                    "source": {
                        "inventory": source_inventory,
                        "digest": source_digest,
                    },
                },
                mode=0o400,
            )
            _fsync_dir(staging_data)
            _fsync_dir(staging)
            _make_tree_read_only(staging)
            os.replace(staging, backup_root)
            _fsync_dir(self.backups_root)
            return backup_root, backup_inventory, backup_digest
        finally:
            _remove_internal_tree(staging)

    def _account_claim_metadata(
        self,
        identity: ReaderStorageIdentity,
        claim_id: str,
        source_inventory: list[dict[str, Any]],
        source_digest: str,
        *,
        activated_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": ACCOUNT_SCHEMA,
            "identity": identity.as_dict(),
            "created_at": activated_at or _utc_now(),
            "legacy_claim": {
                "claim_id": claim_id,
                "source_inventory": source_inventory,
                "source_digest": source_digest,
                "extensions": [],
            },
        }

    def _validate_activated_account(
        self,
        account_root: Path,
        identity: ReaderStorageIdentity,
        claim_id: str,
        source_inventory: list[dict[str, Any]],
        source_digest: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        metadata = self._read_account_metadata(account_root)
        if metadata is None or not self._metadata_matches(metadata, identity):
            raise ClaimConflictError("activated account identity mismatch")
        claim = metadata.get("legacy_claim")
        if (
            not isinstance(claim, dict)
            or claim.get("claim_id") != claim_id
            or claim.get("source_inventory") != source_inventory
            or claim.get("source_digest") != source_digest
        ):
            raise ClaimConflictError("activated account claim metadata mismatch")
        account_inventory = inventory_legacy(account_root)
        if (
            account_inventory != source_inventory
            or inventory_digest(account_inventory) != source_digest
        ):
            raise ClaimConflictError("activated account snapshot mismatch")
        return account_inventory, metadata

    def _activate_or_recover_account(
        self,
        identity: ReaderStorageIdentity,
        claim_id: str,
        backup_data_root: Path,
        source_inventory: list[dict[str, Any]],
        source_digest: str,
    ) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
        account_root = self._account_root(identity)
        if account_root.exists():
            inventory, metadata = self._validate_activated_account(
                account_root,
                identity,
                claim_id,
                source_inventory,
                source_digest,
            )
            return account_root, inventory, metadata

        staging = self.by_user_root / (
            f".claim-{identity.user_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            _ensure_dir(staging, label="legacy account staging")
            _copy_inventory(backup_data_root, staging, source_inventory)
            metadata = self._account_claim_metadata(
                identity,
                claim_id,
                source_inventory,
                source_digest,
            )
            atomic_write_json(self._identity_metadata_path(staging), metadata)
            account_inventory = inventory_legacy(staging)
            if (
                account_inventory != source_inventory
                or inventory_digest(account_inventory) != source_digest
            ):
                raise LegacySnapshotError("account staging verification failed")
            _fsync_dir(staging)
            os.replace(staging, account_root)
            _fsync_dir(self.by_user_root)
            return account_root, account_inventory, metadata
        finally:
            _remove_internal_tree(staging)

    def _build_manifest(
        self,
        identity: ReaderStorageIdentity,
        claim_id: str,
        source_inventory: list[dict[str, Any]],
        source_digest: str,
        backup_root: Path,
        backup_inventory: list[dict[str, Any]],
        backup_digest: str,
        account_root: Path,
        account_inventory: list[dict[str, Any]],
        activated_at: str,
    ) -> dict[str, Any]:
        return {
            "schema": CLAIM_SCHEMA,
            "claim_id": claim_id,
            "owner": identity.as_dict(),
            "claimed_at": activated_at,
            "datasets": [name for name, _kind in LEGACY_DATASETS],
            "extensions": [],
            "source": {
                "root": str(self.legacy_root),
                "inventory": source_inventory,
                "digest": source_digest,
            },
            "backup": {
                "relative_path": backup_root.relative_to(self.root).as_posix(),
                "inventory": backup_inventory,
                "digest": backup_digest,
            },
            "account": {
                "relative_path": account_root.relative_to(self.root).as_posix(),
                "inventory": account_inventory,
                "digest": inventory_digest(account_inventory),
            },
        }

    def _claim_legacy(self, identity: ReaderStorageIdentity) -> dict[str, Any]:
        source_inventory = inventory_legacy(self.legacy_root)
        source_digest = inventory_digest(source_inventory)
        claim_id = self._claim_id(identity, source_digest)
        backup_root, backup_inventory, backup_digest = (
            self._create_or_reuse_backup(
                claim_id,
                identity,
                source_inventory,
                source_digest,
            )
        )
        # Detect an old writer racing the copy before any account activation.
        if inventory_legacy(self.legacy_root) != source_inventory:
            raise LegacySnapshotError("legacy source changed during backup")
        account_root, account_inventory, metadata = (
            self._activate_or_recover_account(
                identity,
                claim_id,
                backup_root / "data",
                source_inventory,
                source_digest,
            )
        )
        # The manifest is the cut-over marker, so verify source once more before
        # writing it.  A mismatch leaves the recovered copy and backup intact
        # but fails closed for an explicit operator decision.
        if inventory_legacy(self.legacy_root) != source_inventory:
            raise ClaimConflictError(
                "legacy source changed after account activation; claim not finalized"
            )
        activated_at = str(metadata.get("created_at") or _utc_now())
        manifest = self._build_manifest(
            identity,
            claim_id,
            source_inventory,
            source_digest,
            backup_root,
            backup_inventory,
            backup_digest,
            account_root,
            account_inventory,
            activated_at,
        )
        atomic_write_json(self.claim_path, manifest)
        return self._validate_claim(manifest)

    @staticmethod
    def _claimed_dataset_names(manifest: dict[str, Any]) -> set[str]:
        names = set(manifest.get("datasets") or [])
        for extension in manifest.get("extensions") or []:
            names.update(extension.get("datasets") or [])
        return names

    def _activate_or_recover_extension(
        self,
        account_root: Path,
        backup_data_root: Path,
        datasets: Sequence[tuple[str, str]],
        source_inventory: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Atomically add each fixed top-level dataset, recovering exact copies.

        An extension cannot replace an account path.  A destination left by a
        crashed prior attempt is accepted only when its complete inventory is
        byte-for-byte identical to the read-only backup snapshot.
        """

        staging = self.by_user_root / (
            f".extend-{account_root.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            _ensure_dir(staging, label="legacy extension staging")
            _copy_inventory(backup_data_root, staging, source_inventory)
            for name, kind in datasets:
                expected = [
                    entry
                    for entry in source_inventory
                    if entry["path"] == name
                    or entry["path"].startswith(name + "/")
                ]
                destination = account_root / name
                _reject_symlink(destination, label="legacy extension destination")
                if destination.exists():
                    actual = _inventory_declared_datasets(
                        account_root,
                        ((name, kind),),
                    )
                    if actual != expected:
                        raise ClaimConflictError(
                            f"account already has different {name} state"
                        )
                    continue
                if not expected:
                    continue
                source = staging / name
                if not source.exists():
                    raise LegacySnapshotError(
                        f"legacy extension staging is incomplete: {name}"
                    )
                os.replace(source, destination)
                _fsync_dir(account_root)

            account_inventory = _inventory_declared_datasets(
                account_root,
                datasets,
            )
            if account_inventory != source_inventory:
                raise ClaimConflictError(
                    "activated legacy extension differs from its backup"
                )
            return account_inventory
        finally:
            _remove_internal_tree(staging)

    def _record_claim_extension(
        self,
        manifest: dict[str, Any],
        identity: ReaderStorageIdentity,
        account_root: Path,
        extension: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = self._read_account_metadata(account_root)
        if metadata is None or not self._metadata_matches(metadata, identity):
            raise ClaimConflictError("legacy owner account metadata missing")
        account_claim = metadata.get("legacy_claim")
        if (
            not isinstance(account_claim, dict)
            or account_claim.get("claim_id") != manifest.get("claim_id")
            or account_claim.get("source_inventory")
            != manifest.get("source", {}).get("inventory")
            or account_claim.get("source_digest")
            != manifest.get("source", {}).get("digest")
        ):
            raise ClaimConflictError(
                "legacy owner account does not match base claim"
            )

        metadata_copy = json.loads(json.dumps(metadata, ensure_ascii=False))
        account_claim_copy = metadata_copy["legacy_claim"]
        account_extensions = account_claim_copy.get("extensions", [])
        if not isinstance(account_extensions, list):
            raise ClaimConflictError("invalid account legacy extensions")
        prior = next(
            (
                item
                for item in account_extensions
                if isinstance(item, dict)
                and item.get("claim_id") == extension["claim_id"]
            ),
            None,
        )
        if prior is not None:
            prior_content = dict(prior)
            extension_content = dict(extension)
            prior_content.pop("claimed_at", None)
            extension_content.pop("claimed_at", None)
            if prior_content != extension_content:
                raise ClaimConflictError(
                    "account legacy extension marker disagrees"
                )
            # Crash recovery keeps the timestamp already committed to account
            # metadata instead of manufacturing a different manifest record.
            extension = prior
        else:
            account_extensions.append(extension)
        account_claim_copy["extensions"] = account_extensions
        atomic_write_json(self._identity_metadata_path(account_root), metadata_copy)

        manifest_copy = json.loads(json.dumps(manifest, ensure_ascii=False))
        manifest_extensions = manifest_copy.get("extensions", [])
        if not isinstance(manifest_extensions, list):
            raise ClaimConflictError("invalid legacy claim extensions")
        manifest_extensions.append(extension)
        manifest_copy["extensions"] = manifest_extensions
        atomic_write_json(self.claim_path, manifest_copy)
        return self._validate_claim(manifest_copy)

    def _extend_legacy_if_needed(
        self,
        manifest: dict[str, Any],
        identity: ReaderStorageIdentity,
        account_root: Path,
    ) -> dict[str, Any]:
        claimed = self._claimed_dataset_names(manifest)
        pending = tuple(
            item for item in LEGACY_DATASETS if item[0] not in claimed
        )
        if not pending:
            return manifest

        source_inventory = _inventory_declared_datasets(
            self.legacy_root,
            pending,
        )
        source_digest = inventory_digest(source_inventory)
        claim_id = self._extension_claim_id(
            identity,
            pending,
            source_digest,
        )
        backup_root, backup_inventory, backup_digest = (
            self._create_or_reuse_backup(
                claim_id,
                identity,
                source_inventory,
                source_digest,
            )
        )
        if (
            _inventory_declared_datasets(self.legacy_root, pending)
            != source_inventory
        ):
            raise LegacySnapshotError(
                "legacy extension source changed during backup"
            )
        account_inventory = self._activate_or_recover_extension(
            account_root,
            backup_root / "data",
            pending,
            source_inventory,
        )
        if (
            _inventory_declared_datasets(self.legacy_root, pending)
            != source_inventory
        ):
            raise ClaimConflictError(
                "legacy extension source changed after account activation"
            )
        extension = {
            "schema": CLAIM_EXTENSION_SCHEMA,
            "claim_id": claim_id,
            "claimed_at": _utc_now(),
            "datasets": [name for name, _kind in pending],
            "source": {
                "inventory": source_inventory,
                "digest": source_digest,
            },
            "backup": {
                "relative_path": backup_root.relative_to(self.root).as_posix(),
                "inventory": backup_inventory,
                "digest": backup_digest,
            },
            "account": {
                "relative_path": account_root.relative_to(self.root).as_posix(),
                "inventory": account_inventory,
                "digest": inventory_digest(account_inventory),
            },
        }
        return self._record_claim_extension(
            manifest,
            identity,
            account_root,
            extension,
        )

    def _claim_or_isolate(
        self,
        identity: ReaderStorageIdentity,
    ) -> Path:
        account_root = self._account_root(identity)
        with exclusive_lock(self.locks_root / "legacy-claim.lock"):
            manifest = self.read_claim()
            if manifest is None:
                try:
                    authorized = self.authorize_claim(identity)
                except Exception as exc:
                    raise ClaimConflictError(
                        "legacy claim authorization failed"
                    ) from exc
                if authorized is True:
                    manifest = self._claim_legacy(identity)
                else:
                    self._ensure_empty_account(identity, account_root)
                    return account_root

            owner = manifest["owner"]
            if (
                owner["user_id"] == identity.user_id
                and owner["storage_namespace"] != identity.storage_namespace
            ):
                raise IdentityMismatchError(
                    f"uid {identity.user_id} is bound to another storage namespace"
                )
            if owner == identity.as_dict():
                metadata = self._read_account_metadata(account_root)
                if metadata is None or not self._metadata_matches(metadata, identity):
                    raise ClaimConflictError("legacy owner account metadata missing")
                account_claim = metadata.get("legacy_claim")
                if (
                    not isinstance(account_claim, dict)
                    or account_claim.get("claim_id") != manifest["claim_id"]
                    or account_claim.get("source_inventory")
                    != manifest["source"]["inventory"]
                    or account_claim.get("source_digest")
                    != manifest["source"]["digest"]
                ):
                    raise ClaimConflictError(
                        "legacy owner account does not match claim manifest"
                    )
                manifest = self._extend_legacy_if_needed(
                    manifest,
                    identity,
                    account_root,
                )
                metadata = self._read_account_metadata(account_root)
                account_claim = (
                    metadata.get("legacy_claim")
                    if isinstance(metadata, dict)
                    else None
                )
                if (
                    not isinstance(account_claim, dict)
                    or account_claim.get("extensions", [])
                    != manifest.get("extensions", [])
                ):
                    raise ClaimConflictError(
                        "account legacy extensions do not match claim manifest"
                    )
            else:
                self._ensure_empty_account(identity, account_root)
            return account_root

    def account_path(
        self,
        identity: ReaderStorageIdentity,
        *parts: str | Path,
    ) -> Path:
        """Return a safe private path after claim reconciliation/isolation."""

        self._validate_identity(identity)
        account_root = self._claim_or_isolate(identity)
        return self._safe_account_child(account_root, parts)

    @contextmanager
    def lock(
        self,
        identity: ReaderStorageIdentity,
        dataset: str,
        key: str = "",
    ) -> Iterator[None]:
        """Exclusive owner/dataset/key lease for route read-modify-write cycles."""

        self.account_path(identity)
        dataset_text = str(dataset)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", dataset_text):
            raise UnsafePathError("lock dataset must be a short safe token")
        lock_key = json.dumps(
            [
                identity.user_id,
                identity.storage_namespace,
                dataset_text,
                str(key),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        filename = "data-" + hashlib.sha256(lock_key).hexdigest() + ".lock"
        with exclusive_lock(self.locks_root / filename):
            yield


__all__ = [
    "ACCOUNT_SCHEMA",
    "BACKUP_SCHEMA",
    "CLAIM_SCHEMA",
    "LEGACY_DATASETS",
    "ClaimConflictError",
    "IdentityMismatchError",
    "InvalidIdentityError",
    "LegacySnapshotError",
    "ReaderStorageIdentity",
    "SidecarStore",
    "SidecarStoreError",
    "UnsafePathError",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "default_sidecar_root",
    "exclusive_lock",
    "inventory_digest",
    "inventory_legacy",
    "read_json",
]
