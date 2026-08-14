from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid


PRODUCT_NAME = "ReaderPC 服务器"
PC_OCR_STATUS_CONTRACT = "reader-pc-ocr-status/1"
READERPC_STATUS_CONTRACT = "readerpc-server-status/1"
READER_CONTEXT_SNAPSHOT_CONTRACT = "reader-context-snapshot/1"
CODEX_VOICE_REGISTRY_PATH = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone\OpenAI.CodexBeta_2p2nqsd0c76g0"
)
DEFAULT_PI_ORIGIN = "https://bwicarus.taile44d0c.ts.net"
STATUS_LIMIT_BYTES = 128 * 1024

PHASE_LABELS = {
    "preparing": "准备中",
    "downloading": "下载书籍",
    "text-ocr": "文字识别",
    "tokenizing": "分词",
    "formula-detect": "公式定位",
    "formula-latex": "公式识别",
    "uploading": "上传结果",
    "finalizing": "收尾",
}


class ReaderPCServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_file_time_utc: int


class ProcessProbe(Protocol):
    def start_file_time_utc(self, pid: int) -> int | None: ...


class WindowsProcessProbe:
    """Read a process generation without WMI or a long shell query."""

    def start_file_time_utc(self, pid: int) -> int | None:
        if os.name != "nt" or isinstance(pid, bool) or pid <= 0:
            return None
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return (int(creation.dwHighDateTime) << 32) | int(
                creation.dwLowDateTime
            )
        finally:
            kernel32.CloseHandle(handle)


def current_process_identity() -> ProcessIdentity:
    pid = os.getpid()
    start = WindowsProcessProbe().start_file_time_utc(pid)
    if start is None:
        # The PC worker is Windows-only in production.  This deterministic
        # fallback keeps source tests useful on other platforms.
        start = int(time.time_ns() // 100)
    return ProcessIdentity(pid=pid, start_file_time_utc=start)


def _default_local_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _source_project_root() -> Path | None:
    configured = str(os.environ.get("BW_READER_PC_PROJECT_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            (
                executable_dir / "readerpc-runtime",
                executable_dir.parent / "readerpc-runtime",
            )
        )
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.append(Path(bundled) / "readerpc-runtime")
    resolved = Path(__file__).resolve()
    candidates.extend(resolved.parents)
    for candidate in candidates:
        if (
            (candidate / "scripts" / "reader_pc_preprocess_worker.py").is_file()
            and (candidate / "_server_deploy" / "reader_book_ocr_worker.py").is_file()
        ):
            return candidate.resolve()
    return None


@dataclass(frozen=True)
class ReaderPCPaths:
    local_root: Path
    status_file: Path
    preferences_file: Path

    @classmethod
    def discover(cls) -> "ReaderPCPaths":
        local_root = _default_local_root()
        return cls(
            local_root=local_root,
            status_file=local_root / "readerpc-server.status.json",
            preferences_file=local_root / "readerpc-server.config.json",
        )


@dataclass(frozen=True)
class ReaderContextStatus:
    available: bool
    fresh: bool
    title: str
    kind: str
    updated_at_epoch_ms: int | None

    @property
    def state_label(self) -> str:
        if self.available and self.fresh:
            return "已连接 · 快照正在更新"
        if self.available:
            return "等待 Reader 页面更新"
        return "暂无快照"


@dataclass(frozen=True)
class CodexVoiceActivityStatus:
    status: str
    active: bool | None
    generation: int | None = None


def _nonnegative_filetime(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, bytes) and len(value) == 8:
        number = int.from_bytes(value, byteorder="little", signed=True)
        return number if number >= 0 else None
    return None


def read_codex_voice_activity(
    value_reader: Callable[[], tuple[object, object] | None] | None = None,
) -> CodexVoiceActivityStatus:
    """Read only the Windows microphone-use ledger used by Direct Voice.

    The ledger contains two timestamps, never audio or transcript content.
    Keeping this probe in-process avoids repeatedly launching the large native
    host merely to render ReaderPC status.
    """

    if value_reader is None:
        if os.name != "nt":
            return CodexVoiceActivityStatus("unavailable", None, None)

        def value_reader() -> tuple[object, object] | None:
            import winreg

            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    CODEX_VOICE_REGISTRY_PATH,
                    0,
                    winreg.KEY_READ,
                ) as key:
                    start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
                    stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
                    return start, stop
            except FileNotFoundError:
                return None

    try:
        values = value_reader()
    except (OSError, PermissionError, ValueError):
        return CodexVoiceActivityStatus("error", None, None)
    if values is None:
        return CodexVoiceActivityStatus("unavailable", None, None)
    start = _nonnegative_filetime(values[0])
    stop = _nonnegative_filetime(values[1])
    if start is None or stop is None:
        return CodexVoiceActivityStatus("error", None, None)
    active = start > 0 and (stop == 0 or start > stop)
    return CodexVoiceActivityStatus(
        "available",
        active,
        start if active else None,
    )

def read_reader_context_status(
    snapshot_path: Path,
    *,
    now_epoch_ms: int | None = None,
    fresh_for_ms: int = 35_000,
) -> ReaderContextStatus:
    value = _read_json(snapshot_path) or {}
    if value.get("schema") != READER_CONTEXT_SNAPSHOT_CONTRACT:
        return ReaderContextStatus(False, False, "", "", None)
    if value.get("contextStatus") == "disabled":
        return ReaderContextStatus(False, False, "", "", None)
    active = value.get("activeReading")
    active = active if isinstance(active, dict) else {}
    page = value.get("currentPage")
    page = page if isinstance(page, dict) else {}
    updated = active.get("receivedAtEpochMs")
    if not isinstance(updated, int) or isinstance(updated, bool):
        updated = page.get("receivedAtEpochMs")
    if not isinstance(updated, int) or isinstance(updated, bool):
        updated = _parse_epoch_ms(value.get("updatedAtUtc"))
    current = int(time.time() * 1000) if now_epoch_ms is None else now_epoch_ms
    fresh = bool(
        isinstance(updated, int)
        and updated > 0
        and 0 <= current - updated <= fresh_for_ms
    )
    return ReaderContextStatus(
        available=True,
        fresh=fresh,
        title=str(active.get("title") or page.get("title") or "")[:160],
        kind=str(active.get("kind") or page.get("kind") or "")[:40],
        updated_at_epoch_ms=updated if isinstance(updated, int) else None,
    )


def write_disabled_reader_context_snapshot(
    bridge_root: Path,
    *,
    now: datetime | None = None,
    producer_instance_id: str | None = None,
) -> None:
    """Atomically revoke every Reader action target while ReaderPC is offline."""

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    producer = producer_instance_id or uuid.uuid4().hex
    try:
        uuid.UUID(hex=producer)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReaderPCServiceError("停用快照 producer 标识无效。") from exc
    if len(producer) != 32 or producer.casefold() != producer:
        raise ReaderPCServiceError("停用快照 producer 标识无效。")
    epoch_ms = int(instant.timestamp() * 1000)
    _atomic_json(
        bridge_root / "runtime" / "reader-context-snapshot.json",
        {
            "schema": READER_CONTEXT_SNAPSHOT_CONTRACT,
            "producerInstanceId": producer,
            "revision": 0,
            "updatedAtUtc": instant.isoformat().replace("+00:00", "Z"),
            "latestEvent": {
                "source": "readerpc-service",
                "seq": None,
                "id": f"readerpc-disabled-{producer}",
                "type": "readerpc.disabled",
                "ts": epoch_ms,
            },
            "activeReading": None,
            "contextStatus": "disabled",
            "currentPage": None,
            "selection": {
                "state": "unknown",
                "text": None,
                "ref": None,
                "reason": "readerpc-service-disabled",
            },
            "focus": {
                "state": "unknown",
                "kind": None,
                "ref": None,
                "reason": "readerpc-service-disabled",
            },
        },
    )


def _parse_epoch_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def write_readerpc_status(
    path: Path,
    *,
    voice: Mapping[str, Any],
    context: ReaderContextStatus,
    pc_ocr: "PcOcrStatus",
) -> None:
    """Publish one credential-free local status entry for App/extension tools."""

    _atomic_json(
        path,
        {
            "contract": READERPC_STATUS_CONTRACT,
            "updatedAtEpochMs": int(time.time() * 1000),
            "voice": dict(voice),
            "readerContext": {
                "available": context.available,
                "fresh": context.fresh,
                "kind": context.kind,
                "title": context.title,
                "updatedAtEpochMs": context.updated_at_epoch_ms,
            },
            "pcPreprocessing": {
                "online": pc_ocr.running,
                "state": pc_ocr.state,
                "phase": pc_ocr.phase,
                "workerId": pc_ocr.worker_id,
                "currentPage": pc_ocr.current_page,
                "progress": dict(pc_ocr.progress),
                "gpu": pc_ocr.gpu_name,
                "error": pc_ocr.error,
                "sourceReady": pc_ocr.source_ready,
            },
        },
    )


@dataclass(frozen=True)
class PcOcrPaths:
    local_root: Path
    cache_root: Path
    status_file: Path
    stdout_log: Path
    stderr_log: Path
    python_exe: Path
    project_root: Path | None
    worker_script: Path | None
    doclayout_model: Path
    unimernet_model_dir: Path

    @classmethod
    def discover(cls, project_root: Path | None = None) -> "PcOcrPaths":
        local_root = _default_local_root()
        root = project_root.resolve() if project_root else _source_project_root()
        python_value = str(os.environ.get("BW_READER_PC_OCR_PYTHON") or "").strip()
        python_exe = (
            Path(python_value).expanduser()
            if python_value
            else local_root
            / "reader-pc-ocr-venv"
            / "Scripts"
            / "python.exe"
        )
        cache_root = local_root / "pc-ocr-cache"
        return cls(
            local_root=local_root,
            cache_root=cache_root,
            status_file=cache_root / "worker-status.json",
            stdout_log=local_root / "logs" / "pc-ocr-worker.out.log",
            stderr_log=local_root / "logs" / "pc-ocr-worker.err.log",
            python_exe=python_exe,
            project_root=root,
            worker_script=(
                root / "scripts" / "reader_pc_preprocess_worker.py"
                if root is not None
                else None
            ),
            doclayout_model=(
                local_root
                / "models"
                / "doclayout_yolo"
                / "doclayout_yolo_docstructbench_imgsz1024.pt"
            ),
            unimernet_model_dir=local_root / "models" / "unimernet_base",
        )


@dataclass(frozen=True)
class PcOcrStatus:
    running: bool
    state: str
    phase: str
    pid: int | None
    start_file_time_utc: int | None
    worker_id: str | None
    gpu_name: str | None
    current_page: int | None
    progress: Mapping[str, Any]
    updated_at_epoch_ms: int | None
    error: str | None
    controllable: bool
    source_ready: bool

    @property
    def state_label(self) -> str:
        if self.running and self.state == "idle":
            return "在线 · 空闲"
        if self.running:
            return "在线 · " + PHASE_LABELS.get(self.phase, self.phase or "工作中")
        if self.state == "starting":
            return "正在启动"
        if self.state == "stale":
            return "已停止（状态已过期）"
        if self.state == "unavailable":
            return "尚未安装 PC 预处理运行环境"
        if self.state == "failed":
            return "启动失败"
        return "已停止"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > STATUS_LIMIT_BYTES:
            return None
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2),
        "utf-8",
    )
    os.replace(temporary, path)


class PcOcrServiceController:
    def __init__(
        self,
        paths: PcOcrPaths | None = None,
        *,
        process_probe: ProcessProbe | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        command_runner: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.paths = paths or PcOcrPaths.discover()
        self.process_probe = process_probe or WindowsProcessProbe()
        self._popen = popen
        self._command_runner = command_runner
        self._sleep = sleep
        self._clock = clock

    def status(self) -> PcOcrStatus:
        value = _read_json(self.paths.status_file) or {}
        valid = value.get("contract") == PC_OCR_STATUS_CONTRACT
        pid_value = value.get("processId") if valid else None
        start_value = value.get("processStartFileTimeUtc") if valid else None
        pid = (
            int(pid_value)
            if isinstance(pid_value, int) and not isinstance(pid_value, bool) and pid_value > 0
            else None
        )
        start = (
            int(start_value)
            if isinstance(start_value, int)
            and not isinstance(start_value, bool)
            and start_value > 0
            else None
        )
        actual_start = self.process_probe.start_file_time_utc(pid) if pid else None
        running = bool(pid and start and actual_start == start)
        state = str(value.get("state") or "stopped") if valid else "stopped"
        if pid and not running:
            state = "stale"
        source_ready = bool(
            self.paths.python_exe.is_file()
            and self.paths.worker_script is not None
            and self.paths.worker_script.is_file()
        )
        if not source_ready and not running:
            state = "unavailable"
        gpu = value.get("gpu") if isinstance(value.get("gpu"), dict) else {}
        progress = (
            dict(value.get("progress"))
            if isinstance(value.get("progress"), dict)
            else {}
        )
        page_value = value.get("currentPage")
        current_page = (
            int(page_value)
            if isinstance(page_value, int)
            and not isinstance(page_value, bool)
            and page_value > 0
            else None
        )
        error = value.get("error")
        return PcOcrStatus(
            running=running,
            state=state,
            phase=str(value.get("phase") or ""),
            pid=pid,
            start_file_time_utc=start,
            worker_id=(
                str(value.get("workerId"))
                if isinstance(value.get("workerId"), str)
                else None
            ),
            gpu_name=(
                str(gpu.get("deviceName"))
                if isinstance(gpu.get("deviceName"), str)
                else None
            ),
            current_page=current_page,
            progress=progress,
            updated_at_epoch_ms=(
                int(value.get("updatedAtEpochMs"))
                if isinstance(value.get("updatedAtEpochMs"), int)
                and not isinstance(value.get("updatedAtEpochMs"), bool)
                else None
            ),
            error=str(error)[:300] if isinstance(error, str) and error else None,
            controllable=running,
            source_ready=source_ready,
        )

    def _require_start_paths(self) -> tuple[Path, Path]:
        worker = self.paths.worker_script
        if not self.paths.python_exe.is_file():
            raise ReaderPCServiceError(
                "PC 预处理 Python 环境不存在，请先完成运行环境安装。"
            )
        if worker is None or not worker.is_file() or self.paths.project_root is None:
            raise ReaderPCServiceError(
                "ReaderPC 找不到已签发的 PC 预处理运行文件。"
            )
        return worker, self.paths.project_root

    def _environment(self, project_root: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.setdefault("BW_READER_PC_OCR_BASE_URL", DEFAULT_PI_ORIGIN)
        environment.setdefault("BW_READER_PC_FORMULA_BACKEND", "unimernet-base")
        environment.setdefault(
            "BW_READER_PC_UNIMERNET_ADAPTER",
            "reader_unimernet_adapter:create_model",
        )
        environment.setdefault(
            "BW_READER_PC_UNIMERNET_CONFIG",
            str(project_root / "scripts" / "reader_unimernet_base.yaml"),
        )
        environment.setdefault(
            "BW_READER_PC_UNIMERNET_MODEL_DIR",
            str(self.paths.unimernet_model_dir),
        )
        environment.setdefault(
            "BW_READER_PC_DOCLAYOUT_MODEL",
            str(self.paths.doclayout_model),
        )
        environment.setdefault(
            "HF_HOME",
            str(self.paths.local_root / "models" / "hf-cache"),
        )
        environment.setdefault(
            "XDG_CACHE_HOME",
            str(self.paths.local_root / "models" / "cache"),
        )
        environment["PYTHONUTF8"] = "1"
        return environment

    def start(self, *, timeout_seconds: float = 20.0) -> PcOcrStatus:
        before = self.status()
        if before.running:
            return before
        worker, project_root = self._require_start_paths()
        self.paths.stdout_log.parent.mkdir(parents=True, exist_ok=True)
        self.paths.cache_root.mkdir(parents=True, exist_ok=True)
        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        stdout_handle = self.paths.stdout_log.open("a", encoding="utf-8")
        stderr_handle = self.paths.stderr_log.open("a", encoding="utf-8")
        try:
            process = self._popen(
                [
                    str(self.paths.python_exe),
                    str(worker),
                    "--project-root",
                    str(project_root),
                    "--recycle-after-job",
                ],
                cwd=str(project_root),
                env=self._environment(project_root),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=creation_flags,
                close_fds=True,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        deadline = self._clock() + max(1.0, timeout_seconds)
        while self._clock() < deadline:
            if process.poll() is not None:
                detail = self._error_tail()
                raise ReaderPCServiceError(
                    "PC 预处理进程启动失败" + (f"：{detail}" if detail else "。")
                )
            current = self.status()
            if current.running:
                return current
            self._sleep(0.2)
        raise ReaderPCServiceError(
            "PC 预处理进程已拉起，但未在限定时间内写出可验证状态。"
        )

    def stop(self, *, timeout_seconds: float = 8.0) -> PcOcrStatus:
        current = self.status()
        if not current.running or current.pid is None or current.start_file_time_utc is None:
            return current
        if (
            self.process_probe.start_file_time_utc(current.pid)
            != current.start_file_time_utc
        ):
            raise ReaderPCServiceError(
                "PC 预处理进程代次已变化，拒绝停止未知进程。"
            )
        command = ["taskkill", "/PID", str(current.pid), "/T", "/F"]
        completed = self._command_runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(2.0, timeout_seconds),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        deadline = self._clock() + max(1.0, timeout_seconds)
        while self._clock() < deadline:
            if self.process_probe.start_file_time_utc(current.pid) is None:
                self._mark_stopped()
                return self.status()
            self._sleep(0.1)
        detail = str(getattr(completed, "stderr", "") or "").strip()[:200]
        raise ReaderPCServiceError(
            "PC 预处理进程未停止" + (f"：{detail}" if detail else "。")
        )

    def _mark_stopped(self) -> None:
        value = _read_json(self.paths.status_file) or {
            "contract": PC_OCR_STATUS_CONTRACT,
        }
        value.update(
            {
                "state": "stopped",
                "phase": "",
                "processId": None,
                "processStartFileTimeUtc": None,
                "updatedAtEpochMs": int(time.time() * 1000),
            }
        )
        _atomic_json(self.paths.status_file, value)

    def _error_tail(self) -> str:
        try:
            content = self.paths.stderr_log.read_text("utf-8", errors="replace")
        except OSError:
            return ""
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return lines[-1][:240] if lines else ""


def format_pc_progress(status: PcOcrStatus) -> str:
    details: list[str] = []
    if status.current_page:
        details.append(f"第 {status.current_page} 页")
    text = status.progress.get("text")
    if isinstance(text, dict):
        completed = text.get("completed")
        total = text.get("total")
        if isinstance(completed, int) and isinstance(total, int) and total > 0:
            details.append(f"文字 {completed}/{total}")
    if status.gpu_name:
        details.append(status.gpu_name)
    return " · ".join(details)
