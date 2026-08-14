#!/usr/bin/env python3
"""Build, verify, install, or roll back a Windows direct-voice candidate.

Build and verification remain side-effect free. Installation is transactional:
it validates both sides, backs up only the fixed payload, stops only the exact
owned Direct service, and restores the old payload/service after any failure.
Configuration, runtime state, bundled .NET, and all other install files are
outside the payload whitelist and are never replaced.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
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
import time
from typing import Any, Mapping, Protocol, Sequence
import uuid
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
DEFAULT_INSTALL_ROOT = Path.home() / "bw-computer-voice-bridge"
DEFAULT_BACKUP_ROOT = Path.home() / "bw-computer-voice-bridge-backups"

PACKAGE_CONTRACT = "reader-computer-voice-direct-package/1"
MANIFEST_SCHEMA = 1
READER_CONTEXT_MCP_SERVER_VERSION = "1.5.0"
ARCHIVE_STAMP = (1980, 1, 1, 0, 0, 0)
RID = "win-x64"
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,3}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_EXCLUDED_PARTS = frozenset({"bin", "obj", "tests", "__pycache__"})
# Every file capable of changing a packaged executable must be content-
# addressed. The C# project embeds ReaderCapabilities/*.md, so those guides
# are build inputs just as surely as the .cs files which read them.
SOURCE_INPUT_SUFFIXES = frozenset({".cs", ".csproj", ".md", ".py"})
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


class StdioCommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
    ) -> CommandResult: ...


class InstallServiceController(Protocol):
    def is_running(self, install_root: Path) -> bool: ...

    def stop(self, install_root: Path) -> None: ...

    def start(self, install_root: Path) -> None: ...


class InstallMcpController(Protocol):
    def quiesce(self, install_root: Path) -> int: ...


@dataclass(frozen=True)
class InstalledProcess:
    pid: int
    executable: Path
    command_line: str
    creation_date: str


class InstalledProcessBackend(Protocol):
    def list_exact_executable(self, executable: Path) -> Sequence[InstalledProcess]: ...

    def terminate_exact(self, process: InstalledProcess) -> bool: ...


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


class SubprocessStdioRunner:
    def __init__(self, *, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                cwd=str(cwd),
                input=stdin,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                124,
                stdout,
                (
                    stderr
                    + "\nstdio MCP smoke test timed out after "
                    + f"{self._timeout_seconds}s"
                ).strip(),
            )
        return CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


class BridgeCoreInstallServiceController:
    """Use bridge_core's exact PID + executable ownership checks."""

    def __init__(self) -> None:
        source = DESKTOP_SOURCE / "bridge_core.py"
        spec = importlib.util.spec_from_file_location(
            "bw_direct_install_bridge_core",
            source,
        )
        if spec is None or spec.loader is None:
            _fail("无法加载 Direct 服务控制模块")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self._module = module
        self._runner = module.WindowsProcessRunner()

    def _paths(self, install_root: Path) -> Any:
        return self._module.BridgePaths.for_root(install_root)

    def is_running(self, install_root: Path) -> bool:
        paths = self._paths(install_root)
        record = self._module._load_service_record(paths)
        if record is None:
            if paths.service_record.exists():
                _fail("Direct 服务记录合同无效；拒绝安装时猜测进程身份")
            return False
        executable = self._runner.executable_for_pid(record["pid"])
        if executable is None:
            return False
        if not self._module._same_path(executable, paths.native_host):
            _fail("Direct 服务记录 PID 属于陌生进程；拒绝停止")
        return True

    def stop(self, install_root: Path) -> None:
        if not self._module.stop_direct_service(
            self._paths(install_root),
            self._runner,
        ):
            _fail("Direct 服务在安装前未确认停止")

    def start(self, install_root: Path) -> None:
        paths = self._paths(install_root)
        self._module.start_direct_service(paths, self._runner)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._module.read_direct_status(paths, self._runner).service_online:
                return
            time.sleep(0.1)
        _fail("Direct 服务重启后 10 秒内未进入在线状态")


def _same_windows_path(left: Path | str, right: Path | str) -> bool:
    def normalized(value: Path | str) -> str:
        return str(Path(value).resolve(strict=False)).replace("/", "\\").rstrip("\\").casefold()

    return normalized(left) == normalized(right)


def _strict_windows_command_tokens(command_line: str) -> tuple[str, ...] | None:
    text = command_line.strip()
    if not text:
        return None
    tokens: list[str] = []
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset >= len(text):
            break
        if text[offset] == '"':
            closing = text.find('"', offset + 1)
            if closing < 0:
                return None
            tokens.append(text[offset + 1:closing])
            offset = closing + 1
            if offset < len(text) and not text[offset].isspace():
                return None
        else:
            end = offset
            while end < len(text) and not text[end].isspace():
                if text[end] == '"':
                    return None
                end += 1
            tokens.append(text[offset:end])
            offset = end
    return tuple(tokens)


def _exact_reader_context_mcp_command(
    command_line: str,
    executable: Path,
) -> bool:
    tokens = _strict_windows_command_tokens(command_line)
    expected_state = (
        executable.parent.parent / "runtime" / "reader-context-snapshot.json"
    ).resolve(strict=False)
    return (
        tokens is not None
        and len(tokens) == 4
        and _same_windows_path(tokens[0], executable)
        and tokens[1:3] == ("--reader-context-mcp", "--state")
        and _same_windows_path(tokens[3], expected_state)
    )


class PowerShellInstalledProcessBackend:
    """Inventory and terminate only an unchanged Win32 process identity."""

    _LIST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('BW_DIRECT_INSTALL_PROCESS_TARGET')
$items = @(
  Get-CimInstance Win32_Process |
    Where-Object {
      $_.ExecutablePath -and
      [StringComparer]::OrdinalIgnoreCase.Equals([string]$_.ExecutablePath, $target)
    } |
    ForEach-Object {
      [pscustomobject]@{
        processId = [int]$_.ProcessId
        executablePath = [string]$_.ExecutablePath
        commandLine = if ($null -eq $_.CommandLine) { '' } else { [string]$_.CommandLine }
        creationDate = if ($null -eq $_.CreationDate) { '' } else { [string]$_.CreationDate }
      }
    }
)
ConvertTo-Json -Compress -InputObject $items
"""
    _TERMINATE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('BW_DIRECT_INSTALL_PROCESS_TARGET')
$expectedCommand = [Environment]::GetEnvironmentVariable('BW_DIRECT_INSTALL_PROCESS_COMMAND')
$expectedCreated = [Environment]::GetEnvironmentVariable('BW_DIRECT_INSTALL_PROCESS_CREATED')
$pidValue = [int][Environment]::GetEnvironmentVariable('BW_DIRECT_INSTALL_PROCESS_PID')
$current = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $pidValue)
if ($null -eq $current) { exit 0 }
$actualCommand = if ($null -eq $current.CommandLine) { '' } else { [string]$current.CommandLine }
$actualCreated = if ($null -eq $current.CreationDate) { '' } else { [string]$current.CreationDate }
if (-not [StringComparer]::OrdinalIgnoreCase.Equals([string]$current.ExecutablePath, $target) -or
    $actualCommand -cne $expectedCommand -or
    $actualCreated -cne $expectedCreated) {
  exit 3
}
$result = Invoke-CimMethod -InputObject $current -MethodName Terminate
if ($null -eq $result -or [int]$result.ReturnValue -ne 0) { exit 4 }
$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
  Start-Sleep -Milliseconds 50
  $remaining = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $pidValue)
} while ($null -ne $remaining -and [DateTime]::UtcNow -lt $deadline)
if ($null -ne $remaining) { exit 5 }
"""

    def __init__(self) -> None:
        if os.name != "nt":
            _fail("Direct MCP 进程静默只支持 Windows")
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        self._powershell = (
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        ).resolve(strict=False)
        if not self._powershell.is_file():
            _fail("找不到固定 Windows PowerShell，无法安全识别 Direct MCP 进程")

    def _run(self, script: str, environment: Mapping[str, str]) -> CommandResult:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        process_environment = os.environ.copy()
        process_environment.update(environment)
        try:
            completed = subprocess.run(
                (
                    str(self._powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded,
                ),
                cwd=str(HERE),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=15,
                env=process_environment,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, stderr="process query timed out")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def list_exact_executable(self, executable: Path) -> Sequence[InstalledProcess]:
        result = self._run(
            self._LIST_SCRIPT,
            {"BW_DIRECT_INSTALL_PROCESS_TARGET": str(executable)},
        )
        if result.returncode != 0:
            _fail("无法安全枚举已安装 Direct 进程")
        try:
            raw = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            _fail("Direct 进程枚举返回了无效 JSON")
        if not isinstance(raw, list):
            _fail("Direct 进程枚举结果不是数组")
        processes: list[InstalledProcess] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {
                "processId", "executablePath", "commandLine", "creationDate"
            }:
                _fail("Direct 进程枚举记录合同无效")
            pid = item["processId"]
            executable_path = item["executablePath"]
            command_line = item["commandLine"]
            creation_date = item["creationDate"]
            if (
                not isinstance(pid, int)
                or pid <= 0
                or not isinstance(executable_path, str)
                or not isinstance(command_line, str)
                or not isinstance(creation_date, str)
                or not creation_date
            ):
                _fail("Direct 进程枚举记录字段无效")
            process = InstalledProcess(
                pid=pid,
                executable=Path(executable_path),
                command_line=command_line,
                creation_date=creation_date,
            )
            if not _same_windows_path(process.executable, executable):
                _fail("Direct 进程枚举越出固定可执行文件")
            processes.append(process)
        return tuple(processes)

    def terminate_exact(self, process: InstalledProcess) -> bool:
        result = self._run(
            self._TERMINATE_SCRIPT,
            {
                "BW_DIRECT_INSTALL_PROCESS_TARGET": str(process.executable),
                "BW_DIRECT_INSTALL_PROCESS_COMMAND": process.command_line,
                "BW_DIRECT_INSTALL_PROCESS_CREATED": process.creation_date,
                "BW_DIRECT_INSTALL_PROCESS_PID": str(process.pid),
            },
        )
        return result.returncode == 0


class ExactReaderContextMcpController:
    """Stop only installed native-host processes in the exact MCP mode."""

    def __init__(self, backend: InstalledProcessBackend | None = None) -> None:
        self._backend = backend or PowerShellInstalledProcessBackend()

    def quiesce(self, install_root: Path) -> int:
        executable = (install_root / NATIVE_REL).resolve(strict=True)
        processes = tuple(self._backend.list_exact_executable(executable))
        unfamiliar = tuple(
            process
            for process in processes
            if not _same_windows_path(process.executable, executable)
            or not _exact_reader_context_mcp_command(
                process.command_line,
                executable,
            )
        )
        if unfamiliar:
            pids = ",".join(str(process.pid) for process in unfamiliar)
            _fail(
                "已安装 native-host 正被非 MCP 模式占用；拒绝猜测终止 "
                f"(pid={pids})"
            )
        for process in processes:
            if not self._backend.terminate_exact(process):
                _fail(f"未能确认停止旧 Reader MCP 进程 (pid={process.pid})")
        remaining = tuple(self._backend.list_exact_executable(executable))
        if remaining:
            _fail("停止旧 Reader MCP 后仍有已安装 native-host 进程；拒绝替换")
        return len(processes)


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


def _mcp_smoke_snapshot() -> dict[str, Any]:
    observed_at = int(time.time() * 1000)
    return {
        "schema": "reader-context-snapshot/1",
        "producerInstanceId": "00112233445566778899aabbccddeeff",
        "revision": 1,
        "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "activeReading": {
            "kind": "pdf",
            "file": "package-self-test.pdf",
            "title": "Package MCP self-test",
            "page": 3,
            "sourceInstanceId": "package-self-test-source",
            "receivedAtEpochMs": observed_at,
            "fresh": True,
        },
        "contextStatus": "ready",
        "currentPage": {
            "kind": "pdf",
            "file": "package-self-test.pdf",
            "title": "Package MCP self-test",
            "page": 3,
            "sourceInstanceId": "package-self-test-source",
            "stable": True,
            "text": "packaged stdio MCP snapshot",
            "textAvailable": True,
        },
        "selection": {
            "state": "unknown",
            "text": None,
            "ref": None,
            "reason": "none",
        },
    }


def _mcp_smoke_input() -> str:
    requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "package-self-test",
                    "version": "1",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "reader_context_snapshot",
                "arguments": {},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "reader_capability_guide",
                "arguments": {"topic": "index"},
            },
        },
    )
    return "\n".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in requests
    ) + "\n"


def _mcp_response_text(response: Mapping[str, Any], *, label: str) -> str:
    result = response.get("result")
    if not isinstance(result, dict):
        _fail(f"包内 stdio MCP {label} 未返回 result")
    content = result.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
    ):
        _fail(f"包内 stdio MCP {label} 文本合同无效")
    return content[0]["text"]


def _validate_mcp_smoke_output(result: CommandResult) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip()[:1000]
        _fail(
            "包内 stdio MCP 前向测试退出失败"
            + (f": {detail}" if detail else "")
        )
    try:
        responses = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError as exc:
        _fail(f"包内 stdio MCP 返回无效 JSONL: {exc}")
    if len(responses) != 4 or any(not isinstance(item, dict) for item in responses):
        _fail("包内 stdio MCP 响应数量或类型无效")
    by_id: dict[int, dict[str, Any]] = {}
    for response in responses:
        if response.get("jsonrpc") != "2.0":
            _fail("包内 stdio MCP 响应协议无效")
        response_id = response.get("id")
        if not isinstance(response_id, int) or response_id in by_id:
            _fail("包内 stdio MCP 响应 id 无效")
        by_id[response_id] = response
    if set(by_id) != {1, 2, 3, 4}:
        _fail("包内 stdio MCP 响应 id 不完整")

    initialize = by_id[1].get("result")
    server_info = initialize.get("serverInfo") if isinstance(initialize, dict) else None
    if (
        not isinstance(server_info, dict)
        or server_info.get("name") != "bw-reader-context-snapshot"
        or server_info.get("version") != READER_CONTEXT_MCP_SERVER_VERSION
    ):
        _fail(
            "包内 stdio MCP serverInfo 不是 "
            + READER_CONTEXT_MCP_SERVER_VERSION
            + " 合同"
        )

    list_result = by_id[2].get("result")
    tools = list_result.get("tools") if isinstance(list_result, dict) else None
    expected_tools = (
        "reader_context_snapshot",
        "reader_capability_guide",
        "reader_visual_image",
        "reader_browser_control",
        "reader_highlight_text",
        "reader_undo_last",
        "reader_anki_draft",
        "reader_card",
        "reader_command",
    )
    if (
        not isinstance(tools, list)
        or tuple(
            item.get("name") if isinstance(item, dict) else None
            for item in tools
        ) != expected_tools
    ):
        _fail("包内 stdio MCP 未暴露精确 9 工具合同")
    command_schema = tools[-1].get("inputSchema")
    command_variants = (
        command_schema.get("oneOf")
        if isinstance(command_schema, dict)
        else None
    )
    if (
        not isinstance(command_variants, list)
        or len(command_variants) != 2
        or command_variants[0].get("required") != ["command"]
        or command_variants[1].get("required") != ["card"]
        or not isinstance(command_variants[1].get("properties"), dict)
        or not isinstance(
            command_variants[1]["properties"].get("card"),
            dict,
        )
        or command_variants[1]["properties"]["card"].get("required")
        != ["kind", "title", "data"]
    ):
        _fail("包内 reader_command 未保留白名单可见的 typed card 回退")

    snapshot_text = _mcp_response_text(by_id[3], label="快照工具")
    try:
        snapshot = json.loads(snapshot_text)
    except json.JSONDecodeError as exc:
        _fail(f"包内 stdio MCP 快照文本不是 JSON: {exc}")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema") != "reader-context-snapshot/1"
        or snapshot.get("revision") != 1
        or not isinstance(snapshot.get("mcp"), dict)
    ):
        _fail("包内 stdio MCP 快照工具未读回临时快照")

    guide_text = _mcp_response_text(by_id[4], label="能力指南工具")
    if len(guide_text.strip()) < 32 or "Reader" not in guide_text:
        _fail("包内 stdio MCP 能力指南为空或不完整")


def run_packaged_self_tests(
    path: Path,
    *,
    runner: CommandRunner | None = None,
    stdio_runner: StdioCommandRunner | None = None,
) -> None:
    """Exercise packaged binaries, including the real stdio MCP boundary."""

    runner = runner or SubprocessRunner(
        timeout_seconds=SELF_TEST_TIMEOUT_SECONDS,
    )
    stdio_runner = stdio_runner or SubprocessStdioRunner(
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
        state_path = root / prefix / "runtime" / "reader-context-snapshot.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                _mcp_smoke_snapshot(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        native_executable = root / prefix / NATIVE_REL
        mcp_result = stdio_runner.run(
            (
                str(native_executable),
                "--reader-context-mcp",
                "--state",
                str(state_path.resolve()),
            ),
            cwd=root,
            stdin=_mcp_smoke_input(),
        )
        _validate_mcp_smoke_output(mcp_result)
        for relative in PYTHON_RUNTIME_RELATIVE_PATHS:
            source = payload[f"{prefix}/{relative}"]
            try:
                compile(source.decode("utf-8"), relative, "exec")
            except (UnicodeDecodeError, SyntaxError) as exc:
                _fail(f"包内 Python runtime 语法无效: {relative}: {exc}")


def _verified_install_directory(
    root: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    root = _require_plain_directory(root, label=label)

    def checked(relative: str) -> Path:
        target = root.joinpath(*PurePosixPath(relative).parts)
        current = target.parent
        while current != root:
            try:
                status = current.lstat()
            except OSError as exc:
                _fail(f"{label} payload 父目录不可读取: {relative}: {exc}")
            if _is_reparse_path(current, status) or not stat.S_ISDIR(status.st_mode):
                _fail(f"{label} payload 父目录不是普通目录: {relative}")
            current = current.parent
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            _fail(f"{label} payload 不存在或不可读取: {relative}: {exc}")
        if not resolved.is_relative_to(root):
            _fail(f"{label} payload 越出安装根: {relative}")
        return target

    manifest_bytes = _read_regular(checked(MANIFEST_REL))
    manifest = _strict_json(manifest_bytes, label=f"{label} manifest")
    if manifest_bytes != _manifest_bytes(manifest):
        _fail(f"{label} manifest 不是规范化 JSON")
    version = manifest.get("version")
    if not isinstance(version, str):
        _fail(f"{label} manifest 缺少版本")
    payload = {
        relative: _read_regular(checked(relative))
        for relative in PAYLOAD_RELATIVE_PATHS
    }
    _validate_manifest(manifest, version=version, payload=payload)
    return manifest, payload


def _prepare_backup_root(path: Path) -> Path:
    lexical = _lexical_absolute(path)
    parent = _require_plain_directory(lexical.parent, label="安装备份根父目录")
    if lexical.parent != parent:
        _fail("安装备份根父目录解析后偏离固定路径")
    if not lexical.exists():
        lexical.mkdir()
    return _require_plain_directory(lexical, label="安装备份根")


def _write_payload_tree(
    root: Path,
    manifest: dict[str, Any],
    payload: Mapping[str, bytes],
) -> None:
    for relative in PAYLOAD_RELATIVE_PATHS:
        target = root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload[relative])
    (root / MANIFEST_REL).write_bytes(_manifest_bytes(manifest))


def _replace_install_payload(
    install_root: Path,
    manifest: dict[str, Any],
    payload: Mapping[str, bytes],
    *,
    replace_file: Any = os.replace,
) -> None:
    transaction_id = uuid.uuid4().hex
    for relative in (*PAYLOAD_RELATIVE_PATHS, MANIFEST_REL):
        target = install_root.joinpath(*PurePosixPath(relative).parts)
        current = target.parent
        while current != install_root:
            try:
                status = current.lstat()
            except OSError as exc:
                _fail(f"安装目标父目录不存在: {relative}: {exc}")
            if _is_reparse_path(current, status) or not stat.S_ISDIR(status.st_mode):
                _fail(f"安装目标父目录不是普通目录: {relative}")
            current = current.parent
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            _fail(f"安装目标不存在或不可读取: {relative}: {exc}")
        if not resolved.is_relative_to(install_root):
            _fail(f"安装目标越出安装根: {relative}")
        temporary = target.with_name(f".{target.name}.bw-install-{transaction_id}.tmp")
        content = (
            _manifest_bytes(manifest)
            if relative == MANIFEST_REL
            else payload[relative]
        )
        try:
            temporary.write_bytes(content)
            replace_file(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _run_installed_self_tests(
    install_root: Path,
    payload: Mapping[str, bytes],
    *,
    runner: CommandRunner,
) -> None:
    for relative in EXECUTABLE_RELATIVE_PATHS:
        executable = install_root.joinpath(*PurePosixPath(relative).parts)
        _require_success(
            runner.run((str(executable), "--self-test"), cwd=install_root),
            label=f"安装后自检 {relative}",
        )
    for relative in PYTHON_RUNTIME_RELATIVE_PATHS:
        try:
            compile(payload[relative].decode("utf-8"), relative, "exec")
        except (UnicodeDecodeError, SyntaxError) as exc:
            _fail(f"安装后 Python runtime 语法无效: {relative}: {exc}")


def _install_verified_payload(
    manifest: dict[str, Any],
    payload: dict[str, bytes],
    *,
    install_root: Path,
    backup_root: Path,
    service_controller: InstallServiceController,
    mcp_controller: InstallMcpController,
    runner: CommandRunner,
    replace_file: Any = os.replace,
) -> dict[str, Any]:
    install_root = _require_plain_directory(install_root, label="Direct 安装根")
    current_manifest, current_payload = _verified_install_directory(
        install_root,
        label="当前 Direct 安装",
    )
    backup_root = _prepare_backup_root(backup_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / (
        f"install-{manifest['version']}-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    backup.mkdir()
    _write_payload_tree(backup, current_manifest, current_payload)
    _verified_install_directory(backup, label="新建安装备份")

    was_running = service_controller.is_running(install_root)
    stopped = False
    payload_mutation_started = False
    mcp_processes_stopped = 0
    try:
        if was_running:
            service_controller.stop(install_root)
            stopped = True
        mcp_processes_stopped = mcp_controller.quiesce(install_root)
        payload_mutation_started = True
        _replace_install_payload(
            install_root,
            manifest,
            payload,
            replace_file=replace_file,
        )
        installed_manifest, installed_payload = _verified_install_directory(
            install_root,
            label="安装后 Direct",
        )
        _run_installed_self_tests(
            install_root,
            installed_payload,
            runner=runner,
        )
        if was_running:
            service_controller.start(install_root)
        receipt = {
            "backup": str(backup),
            "installedVersion": installed_manifest["version"],
            "previousVersion": current_manifest["version"],
            "serviceRestored": was_running,
            "mcpProcessesStopped": mcp_processes_stopped,
        }
        (backup / "install-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return receipt
    except Exception as install_error:
        recovery_errors: list[str] = []
        if stopped:
            try:
                if service_controller.is_running(install_root):
                    service_controller.stop(install_root)
            except Exception as exc:
                recovery_errors.append(f"停止失败候选服务失败: {exc}")
        if payload_mutation_started:
            try:
                mcp_controller.quiesce(install_root)
                _replace_install_payload(
                    install_root,
                    current_manifest,
                    current_payload,
                )
                _verified_install_directory(install_root, label="自动回滚后的 Direct")
            except Exception as exc:
                recovery_errors.append(f"恢复旧 payload 失败: {exc}")
        if was_running:
            try:
                service_controller.start(install_root)
            except Exception as exc:
                recovery_errors.append(f"恢复旧 Direct 服务失败: {exc}")
        detail = f"安装失败，已自动回滚: {install_error}"
        if recovery_errors:
            detail += "; " + "; ".join(recovery_errors)
        raise PackageError(detail) from install_error


def install_archive(
    archive: Path,
    *,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    service_controller: InstallServiceController | None = None,
    mcp_controller: InstallMcpController | None = None,
    runner: CommandRunner | None = None,
    replace_file: Any = os.replace,
) -> dict[str, Any]:
    """Atomically install a validated candidate and preserve non-payload data."""

    manifest, archive_payload = _verified_archive_contents(archive)
    prefix = bundle_name(str(manifest["version"]))
    payload = {
        relative: archive_payload[f"{prefix}/{relative}"]
        for relative in PAYLOAD_RELATIVE_PATHS
    }
    return _install_verified_payload(
        manifest,
        payload,
        install_root=install_root,
        backup_root=backup_root,
        service_controller=(
            service_controller or BridgeCoreInstallServiceController()
        ),
        mcp_controller=(mcp_controller or ExactReaderContextMcpController()),
        runner=runner or SubprocessRunner(timeout_seconds=SELF_TEST_TIMEOUT_SECONDS),
        replace_file=replace_file,
    )


def rollback_install(
    backup: Path,
    *,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    service_controller: InstallServiceController | None = None,
    mcp_controller: InstallMcpController | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Install a previously validated backup using the same transaction."""

    manifest, payload = _verified_install_directory(
        backup,
        label="指定回滚备份",
    )
    return _install_verified_payload(
        manifest,
        payload,
        install_root=install_root,
        backup_root=backup_root,
        service_controller=(
            service_controller or BridgeCoreInstallServiceController()
        ),
        mcp_controller=(mcp_controller or ExactReaderContextMcpController()),
        runner=runner or SubprocessRunner(timeout_seconds=SELF_TEST_TIMEOUT_SECONDS),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", metavar="VERSION", help="构建新的版本化候选 ZIP")
    parser.add_argument("--verify", type=Path, metavar="ZIP", help="只读校验现有候选 ZIP")
    parser.add_argument(
        "--self-test",
        type=Path,
        metavar="ZIP",
        help="校验后运行两个 EXE 自检、真实 stdio MCP 前向调用，并静态编译 Python runtime",
    )
    parser.add_argument("--install", type=Path, metavar="ZIP", help="原子安装候选 ZIP")
    parser.add_argument(
        "--rollback",
        type=Path,
        metavar="BACKUP_DIR",
        help="以同一原子流程恢复指定备份",
    )
    parser.add_argument(
        "--install-root",
        type=Path,
        default=DEFAULT_INSTALL_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    actions = [
        args.build is not None,
        args.verify is not None,
        args.self_test is not None,
        args.install is not None,
        args.rollback is not None,
    ]
    if sum(actions) != 1:
        parser.error("必须且只能指定一个 build/verify/self-test/install/rollback 动作")
    try:
        if args.build:
            print(build_candidate(args.build))
        elif args.verify:
            print(json.dumps(verify_archive(args.verify), ensure_ascii=False, sort_keys=True))
        elif args.install:
            print(json.dumps(install_archive(
                args.install,
                install_root=args.install_root,
                backup_root=args.backup_root,
            ), ensure_ascii=False, sort_keys=True))
        elif args.rollback:
            print(json.dumps(rollback_install(
                args.rollback,
                install_root=args.install_root,
                backup_root=args.backup_root,
            ), ensure_ascii=False, sort_keys=True))
        else:
            run_packaged_self_tests(args.self_test)
            print("OK: packaged self-tests passed")
    except PackageError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
