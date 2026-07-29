#!/usr/bin/env python3
"""Build and verify an immutable Windows direct-voice bridge candidate.

This is deliberately a *candidate* builder, not an installer.  It never
starts ``--direct-serve``, touches Scheduled Tasks or Tailscale Serve, opens
audio devices, or writes outside ``windows/candidates/<version>``.  The ZIP is
also intentionally tiny: two self-contained executables, their fixed local
bridge modules, the canonical voice-typist runtime, and a manifest.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Protocol, Sequence
import zipfile


HERE = Path(__file__).resolve().parent
PACKAGER_SOURCE = Path(__file__).resolve()
DESKTOP_SOURCE = HERE / "computer-voice-desktop"
AUDIO_SOURCE = HERE / "ComputerVoiceAudio"
TYPIST_HELPER_SOURCE = HERE / "bw_computer_voice_typist_helper.py"
SUPERVISOR_SOURCE = HERE / "bw_computer_voice_supervisor.py"
TYPIST_RUNTIME_SOURCE = HERE / "typist-runtime"
TYPIST_SCRIPT_SOURCE = TYPIST_RUNTIME_SOURCE / "voice_typist.py"
TYPIST_IPC_SOURCE = TYPIST_RUNTIME_SOURCE / "typist_ipc.py"
TYPIST_LAUNCHER_SOURCE = TYPIST_RUNTIME_SOURCE / "voice-typist-launcher.ps1"
CANDIDATES = HERE / "candidates"

PACKAGE_CONTRACT = "reader-computer-voice-direct-package/1"
MANIFEST_SCHEMA = 1
ARCHIVE_STAMP = (1980, 1, 1, 0, 0, 0)
RID = "win-x64"
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,3}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_EXCLUDED_PARTS = frozenset({"bin", "obj", "tests", "__pycache__"})
SOURCE_INPUT_SUFFIXES = frozenset({".cs", ".csproj", ".py"})
BUILD_COMMAND_TIMEOUT_SECONDS = 600
SELF_TEST_TIMEOUT_SECONDS = 30
DETERMINISTIC_BUILD_ENV = {
    "PYTHONHASHSEED": "0",
    "SOURCE_DATE_EPOCH": "315532800",
}

NATIVE_REL = "native-host/bw-computer-voice-audio.exe"
DESKTOP_REL = "desktop-launcher/BW-Computer-Voice-Bridge.exe"
TYPIST_HELPER_REL = "bw_computer_voice_typist_helper.py"
SUPERVISOR_REL = "bw_computer_voice_supervisor.py"
TYPIST_SCRIPT_REL = "typist-runtime/voice_typist.py"
TYPIST_IPC_REL = "typist-runtime/typist_ipc.py"
TYPIST_LAUNCHER_REL = "typist-runtime/voice-typist-launcher.ps1"
MANIFEST_REL = "manifest.json"
PAYLOAD_RELATIVE_PATHS = (
    NATIVE_REL,
    DESKTOP_REL,
    TYPIST_HELPER_REL,
    SUPERVISOR_REL,
    TYPIST_SCRIPT_REL,
    TYPIST_IPC_REL,
    TYPIST_LAUNCHER_REL,
)
EXECUTABLE_RELATIVE_PATHS = (NATIVE_REL, DESKTOP_REL)
PYTHON_RUNTIME_RELATIVE_PATHS = (
    TYPIST_HELPER_REL,
    SUPERVISOR_REL,
    TYPIST_SCRIPT_REL,
    TYPIST_IPC_REL,
)
DOTNET_DEFAULT = Path(
    r"C:\Users\bwica\bw-computer-voice-bridge\dotnet8\dotnet.exe"
)
PYINSTALLER_DEFAULT = Path(
    r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe"
)


class PackageError(RuntimeError):
    """A fail-closed candidate build or verification error."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], *, cwd: Path) -> CommandResult: ...


class SubprocessRunner:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._environment_overrides = dict(environment_overrides or {})

    def run(self, args: Sequence[str], *, cwd: Path) -> CommandResult:
        environment = os.environ.copy()
        environment.update(self._environment_overrides)
        try:
            completed = subprocess.run(
                list(args),
                cwd=str(cwd),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=self._timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                124,
                stdout,
                (
                    stderr
                    + f"\ncommand timed out after {self._timeout_seconds}s"
                ).strip(),
            )
        return CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


def _fail(message: str) -> None:
    raise PackageError(message)


def _validate_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version) or not any(
        int(part) for part in version.split(".")
    ):
        _fail("版本必须是 1–4 段且至少一段非零的数字，例如 0.4.1")
    return version


def bundle_name(version: str) -> str:
    return f"bw-computer-voice-direct-{_validate_version(version)}-windows-x64"


def candidate_directory(version: str, *, candidates: Path = CANDIDATES) -> Path:
    return candidates / _validate_version(version)


def archive_path(version: str, *, candidates: Path = CANDIDATES) -> Path:
    return candidate_directory(version, candidates=candidates) / f"{bundle_name(version)}.zip"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_regular(path: Path) -> bytes:
    try:
        status = path.lstat()
    except OSError as exc:
        _fail(f"无法读取 {path}: {exc}")
    if _is_reparse_path(path, status) or not stat.S_ISREG(status.st_mode):
        _fail(f"必须是普通文件，拒绝链接或目录: {path}")
    return path.read_bytes()


def _is_reparse_path(path: Path, status: os.stat_result | None = None) -> bool:
    if status is None:
        try:
            status = path.lstat()
        except OSError:
            return False
    if stat.S_ISLNK(status.st_mode):
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(status, "st_file_attributes", 0) & reparse_attribute:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_plain_directory(path: Path, *, label: str) -> Path:
    lexical = _lexical_absolute(path)
    try:
        status = lexical.lstat()
    except OSError as exc:
        _fail(f"{label} 不存在或不可读取: {exc}")
    if _is_reparse_path(lexical, status) or not stat.S_ISDIR(status.st_mode):
        _fail(f"{label} 必须是非 reparse 普通目录: {lexical}")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        _fail(f"{label} 无法解析: {exc}")
    if resolved != lexical:
        _fail(f"{label} 经过链接或越出词法路径: {lexical}")
    return resolved


def _require_plain_source_root(path: Path, *, label: str) -> Path:
    """Prove that a compiler source root and every ancestor are plain dirs."""

    lexical = _lexical_absolute(path)
    for directory in reversed((lexical, *lexical.parents)):
        try:
            status = directory.lstat()
        except OSError as exc:
            _fail(f"{label} 或其祖先不存在或不可读取: {directory}: {exc}")
        if _is_reparse_path(directory, status) or not stat.S_ISDIR(status.st_mode):
            _fail(f"{label} 及其祖先必须是非 reparse 普通目录: {directory}")
    return _require_plain_directory(lexical, label=label)


def _prepare_candidates_root(candidates: Path) -> Path:
    lexical = _lexical_absolute(candidates)
    parent = _require_plain_directory(lexical.parent, label="候选根父目录")
    if lexical.parent != parent:
        _fail("候选根父目录解析后偏离固定路径")
    try:
        status = lexical.lstat()
    except FileNotFoundError:
        lexical.mkdir(exist_ok=False)
    except OSError as exc:
        _fail(f"无法检查候选根: {exc}")
    else:
        if _is_reparse_path(lexical, status) or not stat.S_ISDIR(status.st_mode):
            _fail(f"候选根必须是非 reparse 普通目录: {lexical}")
    return _require_plain_directory(lexical, label="候选根")


def _candidate_status(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _fail(f"无法检查候选目录 {path}: {exc}")


def _require_exact_candidate_directory(
    destination: Path,
    *,
    candidates_root: Path,
    version: str,
) -> Path:
    root = _require_plain_directory(candidates_root, label="候选根")
    expected = root / _validate_version(version)
    lexical = _lexical_absolute(destination)
    if lexical != expected or lexical.parent != root:
        _fail("候选目录不是候选根下的精确版本子目录")
    status = _candidate_status(lexical)
    if status is None:
        _fail("候选目录不存在")
    if _is_reparse_path(lexical, status) or not stat.S_ISDIR(status.st_mode):
        _fail("候选目录必须是非 reparse 普通目录")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        _fail(f"候选目录无法解析: {exc}")
    if resolved != expected or resolved.parent != root:
        _fail("候选目录解析后越出候选根")
    return resolved


def _remove_failed_candidate(
    destination: Path,
    *,
    candidates_root: Path,
    version: str,
) -> None:
    if _candidate_status(destination) is None:
        return
    exact = _require_exact_candidate_directory(
        destination,
        candidates_root=candidates_root,
        version=version,
    )
    shutil.rmtree(exact)


def _relative_source_files(root: Path) -> list[Path]:
    root = _require_plain_source_root(root, label="编译源码根")
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            _fail(f"无法枚举编译源码目录 {directory}: {exc}")
        for entry in entries:
            path = directory / entry.name
            try:
                status = path.lstat()
            except OSError as exc:
                _fail(f"无法检查编译源码 {path}: {exc}")
            if _is_reparse_path(path, status):
                _fail(f"编译源码树包含 symlink/reparse，拒绝静默跳过: {path}")
            if stat.S_ISDIR(status.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(status.st_mode):
                _fail(f"编译源码树包含非普通文件: {path}")
            relative = path.relative_to(root)
            if (
                not SOURCE_EXCLUDED_PARTS.intersection(relative.parts)
                and path.suffix.casefold() in SOURCE_INPUT_SUFFIXES
            ):
                files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def source_inputs(
    *,
    audio_source: Path = AUDIO_SOURCE,
    desktop_source: Path = DESKTOP_SOURCE,
    typist_helper_source: Path = TYPIST_HELPER_SOURCE,
    supervisor_source: Path = SUPERVISOR_SOURCE,
    typist_script_source: Path = TYPIST_SCRIPT_SOURCE,
    typist_ipc_source: Path = TYPIST_IPC_SOURCE,
    typist_launcher_source: Path = TYPIST_LAUNCHER_SOURCE,
) -> list[dict[str, str]]:
    """Return content-addressed inputs that determine the signed candidate."""

    output: list[dict[str, str]] = []
    for source_root in (audio_source, desktop_source):
        for path in _relative_source_files(source_root):
            try:
                relative = path.relative_to(HERE).as_posix()
            except ValueError:
                # Tests may use a synthetic source root.  It still must not
                # leak a machine path into the manifest.
                relative = f"input/{source_root.name}/{path.relative_to(source_root).as_posix()}"
            output.append({"path": relative, "sha256": _sha256(_read_regular(path))})
    output.append({
        "path": PACKAGER_SOURCE.relative_to(HERE).as_posix(),
        "sha256": _sha256(_read_regular(PACKAGER_SOURCE)),
    })
    for source in (
        typist_helper_source,
        supervisor_source,
        typist_script_source,
        typist_ipc_source,
        typist_launcher_source,
    ):
        try:
            relative = source.relative_to(HERE).as_posix()
        except ValueError:
            relative = f"input/{source.name}"
        output.append({
            "path": relative,
            "sha256": _sha256(_read_regular(source)),
        })
    paths = [entry["path"] for entry in output]
    if (
        not output
        or len(paths) != len(set(paths))
        or len(paths) != len({path.casefold() for path in paths})
    ):
        _fail("编译输入为空或存在重复的相对路径")
    return sorted(output, key=lambda item: item["path"])


def _require_canonical_runtime_sources(
    *,
    typist_script_source: Path,
    typist_ipc_source: Path,
    typist_launcher_source: Path,
) -> None:
    parents = {
        _lexical_absolute(typist_script_source).parent,
        _lexical_absolute(typist_ipc_source).parent,
        _lexical_absolute(typist_launcher_source).parent,
    }
    if len(parents) != 1:
        _fail("canonical voice-typist runtime 必须来自同一普通目录")
    runtime_root = _require_plain_directory(
        parents.pop(),
        label="canonical voice-typist runtime 目录",
    )
    for label, source in (
        ("voice_typist.py", typist_script_source),
        ("typist_ipc.py", typist_ipc_source),
        ("voice-typist-launcher.ps1", typist_launcher_source),
    ):
        lexical = _lexical_absolute(source)
        if lexical.parent != runtime_root:
            _fail(f"canonical runtime {label} 越出固定目录")
        _read_regular(lexical)
    try:
        typist_source = _read_regular(
            _lexical_absolute(typist_script_source)
        ).decode("utf-8")
        ipc_source = _read_regular(
            _lexical_absolute(typist_ipc_source)
        ).decode("utf-8")
        launcher_source = _read_regular(
            _lexical_absolute(typist_launcher_source)
        ).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        _fail(f"canonical voice-typist runtime 不是 UTF-8: {exc}")
    if (
        "import typist_ipc as typist_ipc_runtime" not in typist_source
        or "typist_ipc_runtime.connect_pipe" not in typist_source
        or "typist_ipc_runtime.serve" not in typist_source
        or "process_generation_alive" not in typist_source
        or "queue-resolve" not in typist_source
        or "reader-context-injector" in typist_source
        or "ProductionJournal" in typist_source
        or "--journal-url" in typist_source
        or "bw-reader-context" in typist_source
    ):
        _fail("canonical voice_typist.py 未固定到包内 direct-v3 IPC")
    if (
        "ctypes.WinDLL" not in ipc_source
        or "win32file" in ipc_source
        or "pywintypes" in ipc_source
        or "BW_TYPIST_IPC_HANDOFF_FAILED" not in ipc_source
    ):
        _fail("canonical typist_ipc.py 仍有外部依赖或缺 durable ACK")
    if (
        "$install = $PSScriptRoot" not in launcher_source
        or "[int]$ExpectedPid = 0" not in launcher_source
        or "[long]$ExpectedStartFileTimeUtc = 0" not in launcher_source
        or "[int]$OwnerPid = 0" not in launcher_source
        or "[long]$OwnerStartFileTimeUtc = 0" not in launcher_source
        or "'--owner-process-id'" not in launcher_source
        or "ResolveUncertain" not in launcher_source
        or "'queue-resolve'" not in launcher_source
        or "'--launcher-confirmed-stopped'" not in launcher_source
        or "Get-TypistProcess -Strict" not in launcher_source
        or ".IndexOf($script" in launcher_source
        or "JournalUrl" in launcher_source
        or "--journal-url" in launcher_source
        or "'--clear-stop'" in launcher_source
        or "bw-reader-context" in launcher_source
    ):
        _fail("canonical voice-typist launcher 未固定到 direct-v3 生命周期")


def _tool_version(runner: CommandRunner, executable: Path, *, cwd: Path) -> str:
    result = runner.run((str(executable), "--version"), cwd=cwd)
    if result.returncode != 0:
        _fail(f"工具版本探测失败: {executable.name}: {result.stderr.strip()}")
    version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not version or "\x00" in version or len(version) > 120:
        _fail(f"工具版本输出不合法: {executable.name}")
    return version


def _require_success(result: CommandResult, *, label: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\r", " ")
        _fail(f"{label} 失败 (exit={result.returncode}): {detail[:800]}")


def _safe_zip_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(
        name
        and not name.startswith("/")
        and "\\" not in name
        and "\x00" not in name
        and path.as_posix() == name
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _strict_json(content: bytes, *, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                _fail(f"{label} JSON 有重复键: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} 不是 UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} 顶层必须是 object")
    return value


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def build_manifest(
    *,
    version: str,
    payload: dict[str, bytes],
    build_inputs: list[dict[str, str]],
    dotnet_version: str,
    pyinstaller_version: str,
) -> dict[str, Any]:
    if set(payload) != set(PAYLOAD_RELATIVE_PATHS) or len(payload) != len(PAYLOAD_RELATIVE_PATHS):
        _fail("候选 payload 不等于精确白名单")
    return {
        "buildInputs": {
            "dotnet": {
                "rid": RID,
                "selfContained": True,
                "singleFile": True,
                "version": dotnet_version,
            },
            "pyinstaller": {
                "oneFile": True,
                "noConsole": True,
                "version": pyinstaller_version,
            },
            "environment": dict(sorted(DETERMINISTIC_BUILD_ENV.items())),
            "sourceFiles": build_inputs,
        },
        "contract": PACKAGE_CONTRACT,
        "files": [
            {"path": path, "sha256": _sha256(payload[path]), "size": len(payload[path])}
            for path in PAYLOAD_RELATIVE_PATHS
        ],
        "schema": MANIFEST_SCHEMA,
        "version": _validate_version(version),
    }


def _write_zip(path: Path, contents: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(contents):
            info = zipfile.ZipInfo(name, date_time=ARCHIVE_STAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, contents[name], compress_type=zipfile.ZIP_DEFLATED)


def build_candidate(
    version: str,
    *,
    runner: CommandRunner | None = None,
    candidates: Path = CANDIDATES,
    dotnet: Path = DOTNET_DEFAULT,
    pyinstaller: Path = PYINSTALLER_DEFAULT,
    audio_source: Path = AUDIO_SOURCE,
    desktop_source: Path = DESKTOP_SOURCE,
    typist_helper_source: Path = TYPIST_HELPER_SOURCE,
    supervisor_source: Path = SUPERVISOR_SOURCE,
    typist_script_source: Path = TYPIST_SCRIPT_SOURCE,
    typist_ipc_source: Path = TYPIST_IPC_SOURCE,
    typist_launcher_source: Path = TYPIST_LAUNCHER_SOURCE,
) -> Path:
    """Build a new candidate. Existing version directories are never replaced."""

    version = _validate_version(version)
    runner = runner or SubprocessRunner(
        timeout_seconds=BUILD_COMMAND_TIMEOUT_SECONDS,
        environment_overrides=DETERMINISTIC_BUILD_ENV,
    )
    audio_source = _require_plain_source_root(
        audio_source,
        label="C# 编译源码根",
    )
    desktop_source = _require_plain_source_root(
        desktop_source,
        label="PyInstaller 编译源码根",
    )
    _read_regular(audio_source / "ComputerVoiceAudio.csproj")
    _read_regular(desktop_source / "desktop_launcher.py")
    if not typist_helper_source.is_file() or not supervisor_source.is_file():
        _fail("电脑语音桥固定本地 Python 模块不存在")
    _require_canonical_runtime_sources(
        typist_script_source=typist_script_source,
        typist_ipc_source=typist_ipc_source,
        typist_launcher_source=typist_launcher_source,
    )
    if not dotnet.is_file() or not pyinstaller.is_file():
        _fail("未找到固定的 dotnet 或 PyInstaller；拒绝改用 PATH 中的未知工具")

    build_inputs = source_inputs(
        audio_source=audio_source,
        desktop_source=desktop_source,
        typist_helper_source=typist_helper_source,
        supervisor_source=supervisor_source,
        typist_script_source=typist_script_source,
        typist_ipc_source=typist_ipc_source,
        typist_launcher_source=typist_launcher_source,
    )
    candidates_root = _prepare_candidates_root(candidates)
    destination = candidate_directory(version, candidates=candidates_root)
    if _candidate_status(destination) is not None:
        _fail(f"候选版本已存在，拒绝覆盖: {destination.name}")
    work = destination / "_work"
    publish_dir = work / "native-publish"
    desktop_dist = work / "desktop-dist"
    stage_root = destination / "staging" / bundle_name(version)
    dotnet_version = _tool_version(runner, dotnet, cwd=HERE)
    pyinstaller_version = _tool_version(runner, pyinstaller, cwd=HERE)

    destination.mkdir(exist_ok=False)
    _require_exact_candidate_directory(
        destination,
        candidates_root=candidates_root,
        version=version,
    )
    try:
        native_result = runner.run(
            (
                str(dotnet),
                "publish",
                str(audio_source / "ComputerVoiceAudio.csproj"),
                "--configuration",
                "Release",
                "--runtime",
                RID,
                "--self-contained",
                "true",
                "--artifacts-path",
                str(work / "dotnet-artifacts"),
                "--output",
                str(publish_dir),
                "-p:PublishSingleFile=true",
                "-p:IncludeNativeLibrariesForSelfExtract=true",
                "-p:DebugType=None",
                "-p:DebugSymbols=false",
                "-p:Deterministic=true",
                "-p:ContinuousIntegrationBuild=true",
            ),
            cwd=HERE,
        )
        _require_success(native_result, label="dotnet publish")
        py_result = runner.run(
            (
                str(pyinstaller),
                "--noconfirm",
                "--clean",
                "--onefile",
                "--noconsole",
                "--name",
                "BW-Computer-Voice-Bridge",
                "--distpath",
                str(desktop_dist),
                "--workpath",
                str(work / "desktop-work"),
                "--specpath",
                str(work / "desktop-spec"),
                "--paths",
                str(desktop_source),
                str(desktop_source / "desktop_launcher.py"),
            ),
            cwd=HERE,
        )
        _require_success(py_result, label="PyInstaller")
        final_build_inputs = source_inputs(
            audio_source=audio_source,
            desktop_source=desktop_source,
            typist_helper_source=typist_helper_source,
            supervisor_source=supervisor_source,
            typist_script_source=typist_script_source,
            typist_ipc_source=typist_ipc_source,
            typist_launcher_source=typist_launcher_source,
        )
        if final_build_inputs != build_inputs:
            _fail("源码在构建期间发生变化，拒绝签发候选")

        payload_paths = {
            NATIVE_REL: publish_dir / "bw-computer-voice-audio.exe",
            DESKTOP_REL: desktop_dist / "BW-Computer-Voice-Bridge.exe",
            TYPIST_HELPER_REL: typist_helper_source,
            SUPERVISOR_REL: supervisor_source,
            TYPIST_SCRIPT_REL: typist_script_source,
            TYPIST_IPC_REL: typist_ipc_source,
            TYPIST_LAUNCHER_REL: typist_launcher_source,
        }
        payload = {relative: _read_regular(path) for relative, path in payload_paths.items()}
        post_payload_build_inputs = source_inputs(
            audio_source=audio_source,
            desktop_source=desktop_source,
            typist_helper_source=typist_helper_source,
            supervisor_source=supervisor_source,
            typist_script_source=typist_script_source,
            typist_ipc_source=typist_ipc_source,
            typist_launcher_source=typist_launcher_source,
        )
        if post_payload_build_inputs != build_inputs:
            _fail("源码在候选取样期间发生变化，拒绝签发候选")
        stage_payload = {stage_root / relative: content for relative, content in payload.items()}
        for path, content in stage_payload.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = build_manifest(
            version=version,
            payload=payload,
            build_inputs=build_inputs,
            dotnet_version=dotnet_version,
            pyinstaller_version=pyinstaller_version,
        )
        manifest_bytes = _manifest_bytes(manifest)
        (stage_root / MANIFEST_REL).write_bytes(manifest_bytes)
        zip_contents = {
            f"{bundle_name(version)}/{MANIFEST_REL}": manifest_bytes,
            **{f"{bundle_name(version)}/{key}": value for key, value in payload.items()},
        }
        output = archive_path(version, candidates=candidates_root)
        _write_zip(output, zip_contents)
        verify_archive(output, expected_version=version)
        return output
    except Exception as build_error:
        # The directory was created by this invocation and contains no user
        # state. Re-prove its exact, non-reparse containment before deletion.
        try:
            _remove_failed_candidate(
                destination,
                candidates_root=candidates_root,
                version=version,
            )
        except Exception as cleanup_error:
            raise PackageError(
                "候选构建失败，且因路径证明失败而拒绝清理；"
                f"原始错误: {build_error}; 清理错误: {cleanup_error}"
            ) from build_error
        raise


def _read_archive(path: Path) -> tuple[dict[str, bytes], dict[str, zipfile.ZipInfo]]:
    archive_bytes = _read_regular(path)
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except (OSError, zipfile.BadZipFile) as exc:
        _fail(f"无法读取 ZIP: {exc}")
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
            _fail("ZIP 含重复或 Windows 大小写冲突路径")
        if names != sorted(names) or archive.comment:
            _fail("ZIP 条目顺序或 archive comment 不符合可复现候选契约")
        payload: dict[str, bytes] = {}
        metadata: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            name = info.filename
            mode = info.external_attr >> 16
            if info.is_dir() or not _safe_zip_name(name) or stat.S_IFMT(mode) == stat.S_IFLNK:
                _fail(f"ZIP 含不安全路径或链接: {name}")
            if info.date_time != ARCHIVE_STAMP or info.create_system != 3 or mode != (stat.S_IFREG | 0o755):
                _fail(f"ZIP 元数据不符合可复现候选契约: {name}")
            if (
                info.compress_type != zipfile.ZIP_DEFLATED
                or info.extra
                or info.comment
            ):
                _fail(f"ZIP 压缩或附加元数据不符合可复现候选契约: {name}")
            payload[name] = archive.read(info)
            metadata[name] = info
        return payload, metadata


def _validate_manifest(manifest: dict[str, Any], *, version: str, payload: dict[str, bytes]) -> None:
    if set(manifest) != {"buildInputs", "contract", "files", "schema", "version"}:
        _fail("manifest 顶层字段不精确")
    if manifest["contract"] != PACKAGE_CONTRACT or manifest["schema"] != MANIFEST_SCHEMA:
        _fail("manifest contract 或 schema 不匹配")
    if manifest["version"] != version:
        _fail("manifest 版本与 ZIP 目录不一致")
    if not isinstance(manifest["files"], list) or len(manifest["files"]) != len(PAYLOAD_RELATIVE_PATHS):
        _fail("manifest files 不精确")
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            _fail("manifest file 条目不精确")
        path = entry["path"]
        if not isinstance(path, str) or path in entries:
            _fail("manifest file 路径重复或非法")
        entries[path] = entry
    if tuple(entries) != PAYLOAD_RELATIVE_PATHS:
        _fail("manifest payload 白名单不匹配")
    for path, content in payload.items():
        entry = entries[path]
        if entry["sha256"] != _sha256(content) or entry["size"] != len(content):
            _fail(f"manifest 哈希或大小不匹配: {path}")
    inputs = manifest["buildInputs"]
    if (
        not isinstance(inputs, dict)
        or set(inputs)
            != {"dotnet", "pyinstaller", "environment", "sourceFiles"}
    ):
        _fail("manifest buildInputs 不精确")
    if inputs["environment"] != DETERMINISTIC_BUILD_ENV:
        _fail("manifest 确定性构建环境不精确")
    dotnet = inputs["dotnet"]
    if (
        not isinstance(dotnet, dict)
        or set(dotnet) != {"rid", "selfContained", "singleFile", "version"}
        or dotnet.get("rid") != RID
        or dotnet.get("selfContained") is not True
        or dotnet.get("singleFile") is not True
        or not _valid_tool_version(dotnet.get("version"))
    ):
        _fail("manifest dotnet 构建合同不精确")
    pyinstaller = inputs["pyinstaller"]
    if (
        not isinstance(pyinstaller, dict)
        or set(pyinstaller) != {"oneFile", "noConsole", "version"}
        or pyinstaller.get("oneFile") is not True
        or pyinstaller.get("noConsole") is not True
        or not _valid_tool_version(pyinstaller.get("version"))
    ):
        _fail("manifest PyInstaller 构建合同不精确")
    source_files = inputs["sourceFiles"]
    if not isinstance(source_files, list) or not source_files:
        _fail("manifest 未列出源输入")
    source_paths: list[str] = []
    for item in source_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            _fail("manifest sourceFiles 条目不精确")
        value = item.get("path")
        if (
            not isinstance(value, str)
            or not _safe_zip_name(value)
            or (
                not value.startswith("input/")
                and not value.startswith((
                    "ComputerVoiceAudio/",
                    "computer-voice-desktop/",
                ))
                and value not in {
                    PACKAGER_SOURCE.name,
                    TYPIST_HELPER_SOURCE.name,
                    SUPERVISOR_SOURCE.name,
                    TYPIST_SCRIPT_REL,
                    TYPIST_IPC_REL,
                    TYPIST_LAUNCHER_REL,
                    f"input/{TYPIST_HELPER_SOURCE.name}",
                    f"input/{SUPERVISOR_SOURCE.name}",
                    f"input/{TYPIST_SCRIPT_SOURCE.name}",
                    f"input/{TYPIST_IPC_SOURCE.name}",
                    f"input/{TYPIST_LAUNCHER_SOURCE.name}",
                }
            )
        ):
            _fail("manifest 泄露或包含非法源路径")
        if not isinstance(item.get("sha256"), str) or not SHA256_RE.fullmatch(item["sha256"]):
            _fail("manifest 源哈希非法")
        source_paths.append(value)
    if (
        source_paths != sorted(source_paths)
        or len(source_paths) != len(set(source_paths))
        or len(source_paths) != len({value.casefold() for value in source_paths})
    ):
        _fail("manifest sourceFiles 必须唯一且按路径排序")
    if (
        PACKAGER_SOURCE.name not in source_paths
        or not any(
            value in {
                TYPIST_HELPER_SOURCE.name,
                f"input/{TYPIST_HELPER_SOURCE.name}",
            }
            for value in source_paths
        )
        or not any(
            value in {
                SUPERVISOR_SOURCE.name,
                f"input/{SUPERVISOR_SOURCE.name}",
            }
            for value in source_paths
        )
        or not any(
            value in {
                TYPIST_SCRIPT_REL,
                f"input/{TYPIST_SCRIPT_SOURCE.name}",
            }
            for value in source_paths
        )
        or not any(
            value in {
                TYPIST_IPC_REL,
                f"input/{TYPIST_IPC_SOURCE.name}",
            }
            for value in source_paths
        )
        or not any(
            value in {
                TYPIST_LAUNCHER_REL,
                f"input/{TYPIST_LAUNCHER_SOURCE.name}",
            }
            for value in source_paths
        )
        or not any(
            value.startswith(("ComputerVoiceAudio/", "input/ComputerVoiceAudio/"))
            for value in source_paths
        )
        or not any(
            value.startswith((
                "computer-voice-desktop/",
                "input/computer-voice-desktop/",
            ))
            for value in source_paths
        )
    ):
        _fail("manifest sourceFiles 缺少打包器或任一生产源码根")
    source_hashes = {
        item["path"]: item["sha256"]
        for item in source_files
    }
    for payload_path, source_candidates in (
        (
            TYPIST_HELPER_REL,
            (TYPIST_HELPER_SOURCE.name, f"input/{TYPIST_HELPER_SOURCE.name}"),
        ),
        (
            SUPERVISOR_REL,
            (SUPERVISOR_SOURCE.name, f"input/{SUPERVISOR_SOURCE.name}"),
        ),
        (
            TYPIST_SCRIPT_REL,
            (TYPIST_SCRIPT_REL, f"input/{TYPIST_SCRIPT_SOURCE.name}"),
        ),
        (
            TYPIST_IPC_REL,
            (TYPIST_IPC_REL, f"input/{TYPIST_IPC_SOURCE.name}"),
        ),
        (
            TYPIST_LAUNCHER_REL,
            (TYPIST_LAUNCHER_REL, f"input/{TYPIST_LAUNCHER_SOURCE.name}"),
        ),
    ):
        matches = [
            source_hashes[value]
            for value in source_candidates
            if value in source_hashes
        ]
        if len(matches) != 1 or matches[0] != entries[payload_path]["sha256"]:
            _fail(f"manifest 源摘要与 payload 不一致: {payload_path}")


def _valid_tool_version(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and "\x00" not in value
        and len(value) <= 120
        and value == value.strip()
        and "\r" not in value
        and "\n" not in value
    )


def _verified_archive_contents(
    path: Path,
    *,
    expected_version: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    payload, _ = _read_archive(path)
    name = path.name
    match = re.fullmatch(r"bw-computer-voice-direct-([0-9.]+)-windows-x64\.zip", name)
    if not match:
        _fail("候选 ZIP 文件名不符合固定版本布局")
    version = _validate_version(match.group(1))
    if expected_version is not None and version != _validate_version(expected_version):
        _fail("候选 ZIP 版本不匹配")
    prefix = bundle_name(version)
    expected = {f"{prefix}/{MANIFEST_REL}", *(f"{prefix}/{item}" for item in PAYLOAD_RELATIVE_PATHS)}
    if set(payload) != expected:
        _fail("ZIP 内容不等于精确白名单")
    manifest_bytes = payload[f"{prefix}/{MANIFEST_REL}"]
    manifest = _strict_json(manifest_bytes, label="manifest")
    if manifest_bytes != _manifest_bytes(manifest):
        _fail("manifest 不是规范化的可复现 JSON")
    relative_payload = {item: payload[f"{prefix}/{item}"] for item in PAYLOAD_RELATIVE_PATHS}
    _validate_manifest(manifest, version=version, payload=relative_payload)
    return manifest, payload


def verify_archive(path: Path, *, expected_version: str | None = None) -> dict[str, Any]:
    """Read-only candidate validation; it never launches bridge executables."""

    manifest, _ = _verified_archive_contents(
        path,
        expected_version=expected_version,
    )
    return manifest


def run_packaged_self_tests(path: Path, *, runner: CommandRunner | None = None) -> None:
    """Run only each package's documented self-test after read-only validation."""

    runner = runner or SubprocessRunner(
        timeout_seconds=SELF_TEST_TIMEOUT_SECONDS,
    )
    manifest, payload = _verified_archive_contents(path)
    version = str(manifest["version"])
    prefix = bundle_name(version)
    with tempfile.TemporaryDirectory(prefix="bw-computer-voice-package-self-test-") as raw:
        root = Path(raw).resolve()
        exact_names = (
            f"{prefix}/{MANIFEST_REL}",
            *(f"{prefix}/{relative}" for relative in PAYLOAD_RELATIVE_PATHS),
        )
        for name in exact_names:
            target = root.joinpath(*PurePosixPath(name).parts)
            if not target.is_relative_to(root):
                _fail(f"包内自检解压路径越界: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload[name])
        for relative in EXECUTABLE_RELATIVE_PATHS:
            executable = root / prefix / relative
            if not executable.is_relative_to(root) or not executable.is_file():
                _fail(f"包内自检执行路径不在临时根: {relative}")
            result = runner.run((str(executable), "--self-test"), cwd=root)
            _require_success(result, label=f"包内自检 {relative}")
        for relative in PYTHON_RUNTIME_RELATIVE_PATHS:
            source = payload[f"{prefix}/{relative}"]
            try:
                compile(source.decode("utf-8"), relative, "exec")
            except (UnicodeDecodeError, SyntaxError) as exc:
                _fail(f"包内 Python runtime 语法无效: {relative}: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", metavar="VERSION", help="构建新的版本化候选 ZIP")
    parser.add_argument("--verify", type=Path, metavar="ZIP", help="只读校验现有候选 ZIP")
    parser.add_argument(
        "--self-test",
        type=Path,
        metavar="ZIP",
        help="校验后只运行两个 EXE --self-test，并静态编译 Python runtime",
    )
    args = parser.parse_args(argv)
    actions = [args.build is not None, args.verify is not None, args.self_test is not None]
    if sum(actions) != 1:
        parser.error("必须且只能指定 --build、--verify 或 --self-test")
    try:
        if args.build:
            print(build_candidate(args.build))
        elif args.verify:
            print(json.dumps(verify_archive(args.verify), ensure_ascii=False, sort_keys=True))
        else:
            run_packaged_self_tests(args.self_test)
            print("OK: packaged self-tests passed")
    except PackageError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
