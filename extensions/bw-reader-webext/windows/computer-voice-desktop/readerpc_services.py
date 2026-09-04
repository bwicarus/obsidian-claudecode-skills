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
DEFAULT_PI_ORIGIN = "https://bwicarus-2.taile44d0c.ts.net"   # 2026-09-02 Pi 整体退出:协调/发布在 Windows 的 Flask
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

    @property
    def exit_request_file(self) -> Path:
        return self.local_root / EXIT_REQUEST_FILE

    @classmethod
    def discover(cls) -> "ReaderPCPaths":
        local_root = _default_local_root()
        return cls(
            local_root=local_root,
            status_file=local_root / "readerpc-server.status.json",
            preferences_file=local_root / "readerpc-server.config.json",
        )


# 带外退出请求(2026-09-05)。换代接管原本只有 `taskkill /PID`(WM_CLOSE)一条路,
# 而 ReaderPC 收进托盘后**没有顶层窗口**(实测 MainWindowHandle = 0),WM_CLOSE 无处可去,
# taskkill 却照样返回 0 —— 于是等 60s 超时、拒绝接管,新旧两代同时在跑。
# 这条通道让请求方写文件,运行中的实例在自己的刷新循环里看到就走正常退出路径。
EXIT_REQUEST_CONTRACT = "readerpc-exit-request/1"
EXIT_REQUEST_FILE = "readerpc-exit-request.json"
# ⚠ 必须过期:一个赖在原地的请求会让**新**实例一启动就自杀,表现是"双击没反应"。
EXIT_REQUEST_MAX_SECONDS = 30.0


def write_readerpc_exit_request(
    paths: "ReaderPCPaths",
    reason: str,
    *,
    ttl_seconds: float = EXIT_REQUEST_MAX_SECONDS,
    pid: int | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """请正在运行的 ReaderPC 走正常退出路径。"""

    if not reason or len(reason) > 200:
        raise ReaderPCServiceError("退出请求必须带一句 200 字以内的原因。")
    if not 0 < ttl_seconds <= EXIT_REQUEST_MAX_SECONDS:
        raise ReaderPCServiceError("退出请求有效期无效。")
    path = paths.exit_request_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "contract": EXIT_REQUEST_CONTRACT,
                "reason": reason,
                "pid": int(os.getpid() if pid is None else pid),
                "expiresAtEpoch": float(clock() + ttl_seconds),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def clear_readerpc_exit_request(paths: "ReaderPCPaths") -> None:
    try:
        paths.exit_request_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def read_readerpc_exit_request(
    paths: "ReaderPCPaths",
    *,
    pid_alive: Callable[[int], bool] | None = None,
    clock: Callable[[], float] = time.time,
) -> str | None:
    """有活跃退出请求就回原因，否则回 None。

    过期、契约不对、请求方进程已不在 —— 都当作没有请求。
    ⚠ 这个函数**绝不能抛异常**：它跑在刷新循环里，抛出会把状态刷新一起搭上。
    """

    try:
        value = json.loads(paths.exit_request_file.read_text("utf-8"))
    except Exception:
        return None
    if (
        not isinstance(value, dict)
        or value.get("contract") != EXIT_REQUEST_CONTRACT
        or not isinstance(value.get("reason"), str)
        or not value["reason"]
        or not isinstance(value.get("pid"), int)
        or isinstance(value.get("pid"), bool)
        or value["pid"] <= 0
        or not isinstance(value.get("expiresAtEpoch"), (int, float))
        or isinstance(value.get("expiresAtEpoch"), bool)
        or float(value["expiresAtEpoch"]) <= clock()
    ):
        return None
    if pid_alive is not None and not pid_alive(int(value["pid"])):
        return None
    return value["reason"][:200]


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
    context_status = value.get("contextStatus")
    if context_status == "disabled":
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
        context_status not in {"pending", "stale"}
        and
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


def _write_reader_context_lifecycle_snapshot(
    bridge_root: Path,
    *,
    context_status: str,
    event_name: str,
    reason: str,
    error_label: str,
    now: datetime | None = None,
    producer_instance_id: str | None = None,
) -> None:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    producer = producer_instance_id or uuid.uuid4().hex
    try:
        uuid.UUID(hex=producer)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReaderPCServiceError(
            f"{error_label} producer 标识无效。"
        ) from exc
    if len(producer) != 32 or producer.casefold() != producer:
        raise ReaderPCServiceError(f"{error_label} producer 标识无效。")
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
                "id": f"readerpc-{event_name}-{producer}",
                "type": f"readerpc.{event_name}",
                "ts": epoch_ms,
            },
            "activeReading": None,
            "contextStatus": context_status,
            "currentPage": None,
            "selection": {
                "state": "unknown",
                "text": None,
                "ref": None,
                "reason": reason,
            },
            "focus": {
                "state": "unknown",
                "kind": None,
                "ref": None,
                "reason": reason,
            },
        },
    )


def write_disabled_reader_context_snapshot(
    bridge_root: Path,
    *,
    now: datetime | None = None,
    producer_instance_id: str | None = None,
) -> None:
    """Atomically revoke every Reader action target after an explicit stop."""

    _write_reader_context_lifecycle_snapshot(
        bridge_root,
        context_status="disabled",
        event_name="disabled",
        reason="readerpc-service-disabled",
        error_label="停用快照",
        now=now,
        producer_instance_id=producer_instance_id,
    )


def write_recovering_reader_context_snapshot(
    bridge_root: Path,
    *,
    now: datetime | None = None,
    producer_instance_id: str | None = None,
) -> None:
    """Revoke action targets without claiming the enabled service was disabled."""

    _write_reader_context_lifecycle_snapshot(
        bridge_root,
        context_status="pending",
        event_name="recovering",
        reason="readerpc-service-recovering",
        error_label="恢复中快照",
        now=now,
        producer_instance_id=producer_instance_id,
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
    services: Sequence["ManagedServiceStatus"] | None = None,
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
            # 三守护合一(2026-09-03):Flask 与 sidecar 的可达/托管状态;影子模式下 owned 全 False
            "services": [item.to_public() for item in (services or [])],
        },
    )




# ── 通用受管进程(三守护合一第一步,2026-09-03)──────────────────────────────
# 目标形态见 references/windows-server-consolidation-plan.md:Flask(5000)与四个 sidecar
# 由 ReaderPC 用同一套控制器托管,不再各起守护。本步为影子模式:控制器只观测(端口探针)与
# 显示;偏好 manageServerServices=True 时才真正拉起/保活。health 探针只用 TCP 连通,
# 既不依赖 HTTP 状态码也不需要凭据。

MANAGED_SERVICE_STATUS_CONTRACT = "readerpc-managed-service/1"
DEFAULT_SERVER_PROJECT_ROOTS = (
    Path(r"C:\tmp\reader-card-anchor-release"),
    Path(r"C:\claude"),
)


def discover_server_project_root() -> Path | None:
    """服务器代码(app.py 与 .env.local)所在的**工作树**,不是打进包的 readerpc-runtime。
    第一阶段按方案先托管工作树里的 app.py(今天实际就是这么跑的),原子发布留第二阶段。"""
    configured = str(os.environ.get("BW_READER_SERVER_PROJECT_ROOT") or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(DEFAULT_SERVER_PROJECT_ROOTS)
    for candidate in candidates:
        if (candidate / "_server_deploy" / "app.py").is_file() and (candidate / ".env.local").is_file():
            return candidate.resolve()
    return None


def load_server_env(project_root: Path) -> dict[str, str]:
    """与 scripts/windows_sidecar_services.load_env 同规则:.env.local 的键只在环境里缺席时补上。"""
    env = dict(os.environ)
    env_file = project_root / ".env.local"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    except OSError:
        pass
    env.setdefault("CLAUDE_PROJECT", str(project_root))
    env.setdefault("WEBAPP_DATA", str(project_root / "webapp-data"))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


@dataclass(frozen=True)
class ManagedServiceSpec:
    name: str
    label: str
    command: tuple[str, ...]
    cwd: Path
    port: int
    log_file: Path
    env: Mapping[str, str] | None = None
    # 相对 cwd 的 glob;任一文件 mtime 变了 → restart_if_code_changed() 重启(只对本控制器拉起的实例)。
    # 搬自 local_supervisor.pyw 的「代码变更自动重启」(治「改端点要手动重启」)。
    watch_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedServiceStatus:
    name: str
    label: str
    port: int
    reachable: bool          # 端口可连(不管是谁起的)
    owned: bool              # 由本控制器拉起且进程仍在
    pid: int | None
    restarts: int
    error: str | None
    halted: bool = False     # 连续快失败熔断:暂停自动重拉,等人看日志后 reset()

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "port": self.port,
            "reachable": self.reachable,
            "owned": self.owned,
            "restarts": self.restarts,
            "error": self.error,
            "halted": self.halted,
        }


def _tcp_reachable(port: int, timeout: float = 0.6) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


MANAGED_FAST_FAIL_SECONDS = 20.0   # 起来不到这么久就退出 = 快失败
MANAGED_FAST_FAIL_LIMIT = 4        # 连续这么多次 → 熔断


class ManagedProcessController:
    def __init__(
        self,
        spec: ManagedServiceSpec,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        reachable: Callable[[int], bool] = _tcp_reachable,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.spec = spec
        self._popen = popen
        self._reachable = reachable
        self._clock = clock
        self._sleep = sleep
        self._process: Any = None
        self._restarts = 0
        self._error: str | None = None
        self._last_start = 0.0
        self._watched: dict[str, float] = {}
        self._fast_fails = 0
        self._halted = False

    def status(self) -> ManagedServiceStatus:
        owned = self._process is not None and self._process.poll() is None
        pid = int(getattr(self._process, "pid", 0) or 0) if owned else None
        return ManagedServiceStatus(
            name=self.spec.name,
            label=self.spec.label,
            port=self.spec.port,
            reachable=bool(self._reachable(self.spec.port)),
            owned=owned,
            pid=pid,
            restarts=self._restarts,
            error=self._error,
            halted=self._halted,
        )

    # —— 代码变更自动重启(搬自 local_supervisor.pyw)——
    def _watched_files(self):
        for pattern in self.spec.watch_globs:
            try:
                yield from self.spec.cwd.glob(pattern)
            except Exception:
                continue

    def _snapshot_watched(self) -> None:
        snapshot: dict[str, float] = {}
        for path in self._watched_files():
            try:
                snapshot[str(path)] = path.stat().st_mtime
            except OSError:
                continue
        self._watched = snapshot

    def code_changed(self) -> str | None:
        """返回第一个 mtime 与上次快照不同(或新增)的文件名;没变返回 None。"""
        for path in self._watched_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if self._watched.get(str(path)) != mtime:
                return path.name
        return None

    def restart_if_code_changed(self, *, debounce_seconds: float = 1.5) -> str | None:
        """只对**本控制器拉起**的实例生效:别人的进程改了代码不归我们重启。"""
        if not self.spec.watch_globs or not self.status().owned:
            return None
        changed = self.code_changed()
        if not changed:
            return None
        self._sleep(debounce_seconds)   # 连写多文件只重启一次
        self.stop()
        self.start()
        return changed

    def reset(self) -> None:
        """人看过日志后解除熔断。"""
        self._halted = False
        self._fast_fails = 0
        self._error = None

    def start(self, *, timeout_seconds: float = 20.0) -> ManagedServiceStatus:
        before = self.status()
        if before.owned:
            return before
        if before.reachable:
            # 别人(旧守护)已经占着端口:不抢,只报告 —— 影子模式下这就是常态
            self._error = None
            return before
        self.spec.log_file.parent.mkdir(parents=True, exist_ok=True)
        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        handle = self.spec.log_file.open("a", encoding="utf-8")
        try:
            handle.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ReaderPC 启动 {self.spec.name} =====\n")
            handle.flush()
            self._process = self._popen(
                list(self.spec.command),
                cwd=str(self.spec.cwd),
                env=dict(self.spec.env) if self.spec.env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                close_fds=True,
            )
        finally:
            handle.close()
        self._last_start = self._clock()
        self._restarts += 1
        self._snapshot_watched()
        deadline = self._clock() + max(1.0, timeout_seconds)
        while self._clock() < deadline:
            if self._process.poll() is not None:
                self._error = f"{self.spec.label} 启动后立即退出(code={self._process.returncode}),看 {self.spec.log_file.name}"
                raise ReaderPCServiceError(self._error)
            if self._reachable(self.spec.port):
                self._error = None
                return self.status()
            self._sleep(0.2)
        self._error = f"{self.spec.label} 已拉起但 {timeout_seconds:.0f}s 内端口 {self.spec.port} 未就绪"
        raise ReaderPCServiceError(self._error)

    def stop(self, *, timeout_seconds: float = 8.0) -> ManagedServiceStatus:
        process = self._process
        if process is None or process.poll() is not None:
            self._process = None
            return self.status()
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=timeout_seconds,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=timeout_seconds)
            except Exception:
                process.kill()
        finally:
            self._process = None
        return self.status()

    def ensure(self, *, backoff_seconds: float = 15.0) -> ManagedServiceStatus:
        """保活:不可达且未由本控制器持有 → 拉起(带退避);由本控制器持有但进程死了 → 记错并重拉。"""
        current = self.status()
        if current.reachable or self._halted:
            return current
        if self._process is not None and self._process.poll() is not None:
            # 连续快失败熔断(搬自 local_supervisor.pyw):坏代码/端口被占时别每 15s 拉一次
            uptime = self._clock() - self._last_start
            exit_code = self._process.returncode
            self._fast_fails = self._fast_fails + 1 if uptime < MANAGED_FAST_FAIL_SECONDS else 0
            self._process = None
            if self._fast_fails >= MANAGED_FAST_FAIL_LIMIT:
                self._halted = True
                self._error = (
                    f"{self.spec.label} 连续 {self._fast_fails} 次启动后立即退出,已熔断;"
                    f"看 {self.spec.log_file.name} 修好后点「重启」"
                )
                return self.status()
            self._error = f"{self.spec.label} 退出(code={exit_code}),重拉"
        if self._clock() - self._last_start < backoff_seconds:
            return current
        try:
            return self.start()
        except ReaderPCServiceError:
            # start() 已把 _error 写好;熔断计数在下一轮 ensure 里按"进程已退出"累加
            return self.status()


def default_server_services(project_root: Path | None = None) -> list[ManagedServiceSpec]:
    """Flask(5000)+ 四个 sidecar 的规格。project_root 为 None 时返回空表(影子模式也显示不出来,
    UI 会提示"未找到服务器工作树")。"""
    root = project_root or discover_server_project_root()
    if root is None or not (root / "_server_deploy" / "app.py").is_file() or not (root / ".env.local").is_file():
        return []
    env = load_server_env(root)
    python = env.get("APP_PYTHON") or sys.executable
    deploy = root / "_server_deploy"
    logs = root / "webapp-data"
    return [
        ManagedServiceSpec("webapp", "Flask 服务器(5000)", (python, "app.py"), deploy, 5000, logs / "local_flask.log", env, watch_globs=("*.py",)),
        ManagedServiceSpec("voice-rt", "实时语音中继(8767)", (python, str(deploy / "voice_realtime_relay.py")), deploy, 8767, logs / "sidecar-voice-rt.log", env),
        ManagedServiceSpec("watch-voice", "手表语音中继(8768)", (python, str(deploy / "watch_voice_relay.py")), deploy, 8768, logs / "sidecar-watch-voice.log", env),
        ManagedServiceSpec("rbi", "远程浏览器(8769)", (python, str(deploy / "rbi_server.py")), deploy, 8769, logs / "sidecar-rbi.log", env),
        ManagedServiceSpec("mcp", "MCP 门面(8766)", (python, str(deploy / "mcp_server.py"), "--http", "8766"), deploy, 8766, logs / "sidecar-mcp.log", env),
    ]


OBSIDIAN_SYNC_TASK_NAME = "Obsidian Headless Sync"


def ensure_scheduled_task_running(
    task_name: str = OBSIDIAN_SYNC_TASK_NAME,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """看护一个 Windows 计划任务:被禁→启用,没跑→启动。搬自 local_supervisor.pyw 的
    ensure_obsidian_sync(2026-05-15 迁服务器时该任务被禁,PC vault 停更 3 周没人发现)。
    用 Get-ScheduledTask 的 State 枚举(语言中立)。返回做了什么:absent / running / started / enabled+started / error:…"""
    if os.name != "nt":
        return "absent"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        probe = runner(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue).State"],
            capture_output=True, text=True, creationflags=flags, timeout=20,
        )
        state = str(getattr(probe, "stdout", "") or "").strip()
        if not state:
            return "absent"
        if state == "Running":
            return "running"
        command = ""
        action = "started"
        if state == "Disabled":
            command += f"Enable-ScheduledTask -TaskName '{task_name}' | Out-Null; "
            action = "enabled+started"
        command += f"Start-ScheduledTask -TaskName '{task_name}'"
        runner(["powershell", "-NoProfile", "-Command", command],
               capture_output=True, text=True, creationflags=flags, timeout=20)
        return action
    except Exception as exc:
        return f"error:{exc}"


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
