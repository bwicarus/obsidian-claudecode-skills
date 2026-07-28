"""Resolve the immutable KG runtime selected by the reader deployment.

Production code must never fall back to ``CLAUDE_PROJECT/scripts/kg``.  The
project root remains the data/configuration root, while executable KG modules
come only from one atomically selected and manifest-verified release.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from types import MappingProxyType
from typing import Mapping


RUNTIME_CONTRACT = "bw-reader-kg-runtime/1"
SUPPORTED_DATA_SCHEMA = "kg-node-history/1"
DEFAULT_RUNTIME_ROOT = Path("/home/bwicarus/reader-runtime/kg")
_MARKER_NAME = "runtime-manifest.json"
_VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,3}"
)
_DEPLOY_ID_RE = re.compile(
    rf"kg-{_VERSION_PATTERN}-[0-9a-f]{{20}}"
)
_SCHEMA_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,79}){0,3}"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MANIFEST_KEYS = frozenset(
    {
        "contract",
        "deployId",
        "readerVersion",
        "dataSchemaMin",
        "dataSchemaMax",
        "files",
        "manifestDigest",
    }
)
_IMPORT_RELATIVE_DIRS = (
    "scripts/kg",
    "scripts",
    "scripts/lib",
    "_client/core",
    "_server_deploy",
)
_ACTIVATED_IMPORT_PATHS: set[str] = set()


class KgRuntimeError(RuntimeError):
    pass


def _runtime_root() -> Path:
    return Path(
        os.environ.get(
            "BW_READER_KG_RUNTIME_ROOT",
            str(DEFAULT_RUNTIME_ROOT),
        )
    )


def _safe_relative(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in ("\x00", "\t", "\r", "\n", "\\"))
    ):
        raise KgRuntimeError("KG runtime 相对路径无效")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != value
    ):
        raise KgRuntimeError("KG runtime 相对路径越界")
    return value


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_reader_version(value: object) -> str:
    if not isinstance(value, str):
        raise KgRuntimeError("KG runtime readerVersion 无效")
    parts = value.split(".")
    if (
        not 1 <= len(parts) <= 4
        or any(not re.fullmatch(r"(?:0|[1-9][0-9]*)", part) for part in parts)
        or any(len(part) > 5 or int(part) > 65535 for part in parts)
        or all(part == "0" for part in parts)
    ):
        raise KgRuntimeError("KG runtime readerVersion 无效")
    return value


def _validate_schema_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SCHEMA_ID_RE.fullmatch(value):
        raise KgRuntimeError(f"KG runtime {field} 无效")
    return value


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise KgRuntimeError(f"KG runtime {label} 缺失或不可读") from exc


def _require_real_directory(path: Path, label: str) -> None:
    mode = _lstat(path, label).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise KgRuntimeError(f"KG runtime {label} 不是实体目录")


def _require_real_path(root: Path, relative: str, *, directory: bool) -> Path:
    """Return a path whose complete release-internal chain has no symlinks."""
    relative = _safe_relative(relative)
    _require_real_directory(root, "release")
    candidate = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        mode = _lstat(candidate, relative).st_mode
        if stat.S_ISLNK(mode):
            raise KgRuntimeError(
                "KG runtime release 内部禁止符号链接: " + relative
            )
        final = index == len(parts) - 1
        if not final and not stat.S_ISDIR(mode):
            raise KgRuntimeError(
                "KG runtime 路径中间项不是目录: " + relative
            )
        if final:
            expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
            if not expected:
                kind = "目录" if directory else "普通文件"
                raise KgRuntimeError(
                    f"KG runtime 路径不是{kind}: {relative}"
                )
    return candidate


def _read_regular_file(root: Path, relative: str) -> bytes:
    path = _require_real_path(root, relative, directory=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KgRuntimeError(
            "KG runtime 普通文件无法安全打开: " + relative
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise KgRuntimeError(
                "KG runtime 路径不是普通文件: " + relative
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _no_duplicate_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise KgRuntimeError("KG runtime manifest 含重复字段")
        value[key] = item
    return value


def _parse_manifest(release: Path) -> dict:
    raw = _read_regular_file(release, _MARKER_NAME)
    try:
        marker = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
        )
    except KgRuntimeError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise KgRuntimeError("KG runtime manifest 缺失或损坏") from exc
    if not isinstance(marker, dict) or set(marker) != _MANIFEST_KEYS:
        raise KgRuntimeError("KG runtime manifest 字段集合无效")

    marker_body = {
        key: value
        for key, value in marker.items()
        if key != "manifestDigest"
    }
    files = marker.get("files")
    deploy_id = marker.get("deployId")
    manifest_digest = marker.get("manifestDigest")
    schema_min = _validate_schema_id(
        marker.get("dataSchemaMin"),
        "dataSchemaMin",
    )
    schema_max = _validate_schema_id(
        marker.get("dataSchemaMax"),
        "dataSchemaMax",
    )
    if (
        marker.get("contract") != RUNTIME_CONTRACT
        or not isinstance(deploy_id, str)
        or not _DEPLOY_ID_RE.fullmatch(deploy_id)
        or not isinstance(files, dict)
        or not files
        or not isinstance(manifest_digest, str)
        or not _DIGEST_RE.fullmatch(manifest_digest)
        or manifest_digest
        != hashlib.sha256(_canonical_json(marker_body)).hexdigest()
    ):
        raise KgRuntimeError("KG runtime manifest 身份或摘要无效")
    _validate_reader_version(marker.get("readerVersion"))
    if (
        schema_min != SUPPORTED_DATA_SCHEMA
        or schema_max != SUPPORTED_DATA_SCHEMA
    ):
        raise KgRuntimeError("KG runtime 数据 schema 与本进程不兼容")

    verified_files: dict[str, str] = {}
    for raw_relative, expected_digest in files.items():
        if not isinstance(raw_relative, str):
            raise KgRuntimeError("KG runtime manifest 文件路径无效")
        relative = _safe_relative(raw_relative)
        if (
            not isinstance(expected_digest, str)
            or not _DIGEST_RE.fullmatch(expected_digest)
        ):
            raise KgRuntimeError(
                "KG runtime 文件摘要无效: " + relative
            )
        actual_digest = hashlib.sha256(
            _read_regular_file(release, relative)
        ).hexdigest()
        if actual_digest != expected_digest:
            raise KgRuntimeError(
                "KG runtime 文件缺失或摘要不一致: " + relative
            )
        verified_files[relative] = expected_digest
    marker["files"] = verified_files
    release_identity = {
        "contract": marker["contract"],
        "readerVersion": marker["readerVersion"],
        "dataSchemaMin": marker["dataSchemaMin"],
        "dataSchemaMax": marker["dataSchemaMax"],
        "files": verified_files,
    }
    expected_deploy_id = (
        f"kg-{marker['readerVersion']}-"
        f"{hashlib.sha256(_canonical_json(release_identity)).hexdigest()[:20]}"
    )
    if deploy_id != expected_deploy_id:
        raise KgRuntimeError("KG runtime deployId 不是内容寻址身份")
    return marker


def _core_module_files(files: Mapping[str, str]) -> dict[str, str]:
    modules: dict[str, str] = {}
    for relative in files:
        path = PurePosixPath(relative)
        if (
            len(path.parts) == 3
            and path.parts[:2] == ("scripts", "kg")
            and path.suffix == ".py"
            and path.stem != "__init__"
        ):
            modules[path.stem] = relative
    return modules


@dataclass(frozen=True)
class PinnedRelease:
    """One verified release identity, stable across a ``current`` switch."""

    release: Path
    marker: Mapping[str, object]

    @property
    def deploy_id(self) -> str:
        return str(self.marker["deployId"])

    @property
    def reader_version(self) -> str:
        return str(self.marker["readerVersion"])

    @property
    def files(self) -> Mapping[str, str]:
        files = self.marker["files"]
        if not isinstance(files, Mapping):  # Defensive; constructor is private.
            raise KgRuntimeError("KG runtime manifest files 无效")
        return files

    def runtime_file(self, relative: str) -> Path:
        relative = _safe_relative(relative)
        expected_digest = self.files.get(relative)
        if not isinstance(expected_digest, str):
            raise KgRuntimeError(
                "文件不在 KG runtime manifest: " + relative
            )
        actual_digest = hashlib.sha256(
            _read_regular_file(self.release, relative)
        ).hexdigest()
        if actual_digest != expected_digest:
            raise KgRuntimeError(
                "KG runtime 文件缺失或摘要不一致: " + relative
            )
        return _require_real_path(
            self.release,
            relative,
            directory=False,
        )

    def _import_paths(self) -> tuple[Path, ...]:
        # concept_node_service is the required anchor of every KG release.
        self.runtime_file("scripts/kg/concept_node_service.py")
        return tuple(
            _require_real_path(self.release, relative, directory=True)
            for relative in _IMPORT_RELATIVE_DIRS
        )

    def _reject_core_cache_pollution(self) -> None:
        kg_dir = _require_real_path(
            self.release,
            "scripts/kg",
            directory=True,
        )
        for name in _core_module_files(self.files):
            module = sys.modules.get(name)
            if module is None:
                continue
            module_file_raw = getattr(module, "__file__", None)
            try:
                module_file = Path(module_file_raw).resolve(strict=True)
            except (OSError, TypeError, ValueError) as exc:
                raise KgRuntimeError(
                    f"KG module {name} 缓存身份无法验证"
                ) from exc
            try:
                module_file.relative_to(kg_dir)
            except ValueError as exc:
                raise KgRuntimeError(
                    f"KG module {name} 从非 pinned release 缓存: "
                    f"{module_file}"
                ) from exc
            expected = self.runtime_file(
                _core_module_files(self.files)[name]
            )
            if module_file != expected:
                raise KgRuntimeError(
                    f"KG module {name} 从非 pinned release 缓存: "
                    f"{module_file}"
                )

    def activate_imports(self) -> Path:
        """Activate all release code roots after rejecting mixed KG caches."""
        self._reject_core_cache_pollution()
        import_paths = self._import_paths()
        values = tuple(str(path) for path in import_paths)
        global _ACTIVATED_IMPORT_PATHS
        sys.path[:] = [
            entry
            for entry in sys.path
            if entry not in _ACTIVATED_IMPORT_PATHS and entry not in values
        ]
        sys.path[:0] = list(values)
        _ACTIVATED_IMPORT_PATHS = set(values)
        importlib.invalidate_caches()
        return import_paths[0]

    def import_module(self, name: str):
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        ):
            raise KgRuntimeError("KG module 名称无效")
        core_modules = _core_module_files(self.files)
        relative = core_modules.get(name)
        if relative is None:
            raise KgRuntimeError("KG module 不在 runtime manifest: " + name)
        self.activate_imports()
        expected_file = self.runtime_file(relative)
        module = importlib.import_module(name)
        module_file_raw = getattr(module, "__file__", None)
        try:
            module_file = Path(module_file_raw).resolve(strict=True)
        except (OSError, TypeError, ValueError) as exc:
            raise KgRuntimeError(
                f"KG module {name} 加载身份无法验证"
            ) from exc
        if module_file != expected_file:
            raise KgRuntimeError(
                f"KG module {name} 从非 pinned release 加载: "
                f"{module_file}"
            )
        # Re-hash after import so a concurrent mutation cannot be accepted.
        self.runtime_file(relative)
        return module


def pin_release() -> PinnedRelease:
    """Resolve and verify ``current`` once for a multi-step operation."""
    root = _runtime_root()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise KgRuntimeError("KG runtime 根目录无法解析") from exc
    _require_real_directory(root, "根目录")
    releases = root / "releases"
    _require_real_directory(releases, "releases")
    current = root / "current"
    current_mode = _lstat(current, "current").st_mode
    if not stat.S_ISLNK(current_mode):
        raise KgRuntimeError("KG runtime current 不是受管符号链接")
    try:
        raw_target = os.readlink(current)
    except OSError as exc:
        raise KgRuntimeError("KG runtime current 无法读取") from exc
    target = PurePosixPath(raw_target)
    if (
        target.is_absolute()
        or target.as_posix() != raw_target
        or len(target.parts) != 2
        or target.parts[0] != "releases"
        or not _DEPLOY_ID_RE.fullmatch(target.parts[1])
    ):
        raise KgRuntimeError(
            "KG runtime current 必须是 releases 直属相对符号链接"
        )
    release = releases / target.parts[1]
    _require_real_directory(release, "release")
    marker = _parse_manifest(release)
    if marker["deployId"] != release.name:
        raise KgRuntimeError("KG runtime manifest deployId 与目录不一致")
    frozen_files = MappingProxyType(dict(marker["files"]))
    frozen_marker = MappingProxyType(
        {
            **marker,
            "files": frozen_files,
        }
    )
    return PinnedRelease(release=release, marker=frozen_marker)


def current_release() -> Path:
    return pin_release().release


def runtime_file(
    relative: str,
    *,
    pinned: PinnedRelease | None = None,
) -> Path:
    return (pinned or pin_release()).runtime_file(relative)


def activate_imports(*, pinned: PinnedRelease | None = None) -> Path:
    """Activate all selected release import roots; return ``scripts/kg``."""
    return (pinned or pin_release()).activate_imports()


def import_module(
    name: str,
    *,
    pinned: PinnedRelease | None = None,
):
    return (pinned or pin_release()).import_module(name)


__all__ = [
    "DEFAULT_RUNTIME_ROOT",
    "KgRuntimeError",
    "PinnedRelease",
    "RUNTIME_CONTRACT",
    "SUPPORTED_DATA_SCHEMA",
    "activate_imports",
    "current_release",
    "import_module",
    "pin_release",
    "runtime_file",
]
