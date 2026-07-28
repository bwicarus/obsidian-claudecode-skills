"""Fail-closed Windows control core for the BW computer-voice bridge.

This module is deliberately not a daemon and opens no listening socket.  A
future paired outbound transport may pass one authenticated command to
``CombinedVoiceStartCoordinator``.  Server data can never choose a local path,
PowerShell action, process, or shortcut.

The existing voice-typist remains an independent component.  This supervisor
only uses its public ``Status`` and ``Start`` launcher actions.  It never calls
Stop/Pause/Resume/EStop/ClearStop and never edits the typist cursor or config.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


CONTRACT = "reader-computer-voice-supervisor/1"
LOCAL_CONFIG_CONTRACT = "reader-computer-voice-local-config/1"
BRIDGE_COMMAND_CONTRACT = "reader-computer-voice-bridge/1"
START_ACTION = "start-computer-voice"
DEFAULT_TYPING_LAUNCHER = Path(
    r"C:\Users\bwica\bw-reader-context\reader-bridge\voice-typist-launcher.ps1"
)
DEFAULT_TYPING_SCRIPT = Path(
    r"C:\Users\bwica\bw-reader-context\reader-bridge\voice_typist.py"
)
_ALLOWED_LAUNCHER_ACTIONS = frozenset({"Status", "Start"})
_SAFE_COMMAND_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_SHORTCUT_KEY = re.compile(r"^(?:[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4]))$")
_SHORTCUT_MODIFIERS = frozenset({"ctrl", "shift", "alt", "win"})


class SupervisorError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TypistProcess:
    pid: int
    session_id: int
    command_line: str


@dataclass(frozen=True)
class TypistState:
    running: bool
    pid: int | None
    paused: bool
    emergency_stop: bool


class CaptureAdapter(Protocol):
    def ensure_started(self, root_process_id: int) -> Mapping[str, Any]: ...

    def rollback_if_owned(self) -> None: ...


@dataclass(frozen=True)
class LocalBridgeConfig:
    """Strict local-only configuration; it is never accepted from Pi."""

    local_opt_in: bool
    voice_start_shortcut: tuple[str, ...]
    app_kind: str
    companion_launcher: Path

    @classmethod
    def load(cls, path: Path) -> "LocalBridgeConfig":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
                "电脑客户端本地配置无法读取",
            ) from exc
        if not isinstance(value, dict) or set(value) != {
            "contract",
            "localOptIn",
            "voiceStartShortcut",
            "appKind",
            "outputScope",
            "companionLauncher",
        }:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
                "电脑客户端本地配置字段不匹配",
            )
        if value.get("contract") != LOCAL_CONFIG_CONTRACT:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
                "电脑客户端本地配置合同无效",
            )
        if not isinstance(value.get("localOptIn"), bool):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
                "localOptIn 必须是布尔值",
            )
        if value.get("outputScope") != "process-only":
            raise SupervisorError(
                "BW_COMPUTER_VOICE_PROCESS_OUTPUT_REQUIRED",
                "本地配置禁止全系统输出捕获",
            )
        app_kind = value.get("appKind")
        if app_kind not in {"chatgpt-desktop", "codex-desktop"}:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
                "本地目标应用无效",
            )
        launcher = Path(str(value.get("companionLauncher") or ""))
        if str(PureWindowsPath(launcher)).casefold() != str(
            PureWindowsPath(DEFAULT_TYPING_LAUNCHER)
        ).casefold():
            raise SupervisorError(
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
                "voice-typist launcher 必须使用固定本地路径",
            )
        return cls(
            local_opt_in=value["localOptIn"],
            voice_start_shortcut=parse_shortcut(value.get("voiceStartShortcut")),
            app_kind=app_kind,
            companion_launcher=launcher,
        )


def parse_shortcut(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise SupervisorError(
            "BW_COMPUTER_VOICE_CONFIG_INVALID",
            "语音启动快捷键必须是字符串",
        )
    parts = tuple(part.strip().casefold() for part in value.split("+") if part.strip())
    if len(parts) < 2 or len(set(parts)) != len(parts):
        raise SupervisorError(
            "BW_COMPUTER_VOICE_CONFIG_INVALID",
            "语音启动快捷键必须包含修饰键且不能重复",
        )
    modifiers = parts[:-1]
    key = parts[-1]
    if (
        not modifiers
        or any(part not in _SHORTCUT_MODIFIERS for part in modifiers)
        or not _SHORTCUT_KEY.fullmatch(key)
    ):
        raise SupervisorError(
            "BW_COMPUTER_VOICE_CONFIG_INVALID",
            "语音启动快捷键格式无效",
        )
    return parts


def _default_runner(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _last_json_object(text: str, *, label: str) -> dict[str, Any]:
    for line in reversed(str(text or "").splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise SupervisorError("BW_COMPUTER_VOICE_BAD_LOCAL_OUTPUT", f"{label} 未返回 JSON 对象")


def _windows_path_token_present(command_line: str, expected: Path) -> bool:
    normalized = str(PureWindowsPath(expected)).casefold()
    value = str(command_line or "").casefold()
    pattern = r'(?:"|^|\s)' + re.escape(normalized) + r'(?:"|$|\s)'
    return re.search(pattern, value) is not None


def _current_windows_session_id() -> int:
    if os.name != "nt":
        raise SupervisorError(
            "BW_COMPUTER_VOICE_WRONG_PLATFORM",
            "电脑客户端监督器只能在 Windows 交互会话运行",
        )
    import ctypes
    from ctypes import wintypes

    session = wintypes.DWORD()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(
        wintypes.DWORD(os.getpid()),
        ctypes.byref(session),
    )
    if not ok:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_SESSION_UNKNOWN",
            "无法确认当前 Windows 会话",
        )
    return int(session.value)


def _default_typist_process_probe() -> list[TypistProcess]:
    """Read process metadata without executing or controlling the typist."""
    script = (
        "$rows=@(Get-CimInstance Win32_Process|"
        "Where-Object{$_.CommandLine -and $_.CommandLine -like '*voice_typist.py*'}|"
        "ForEach-Object{[pscustomobject]@{"
        "pid=[int]$_.ProcessId;sessionId=[int]$_.SessionId;"
        "commandLine=[string]$_.CommandLine}});"
        "$rows|ConvertTo-Json -Depth 3 -Compress"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = _default_runner(
        (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ),
        10.0,
    )
    if completed.returncode != 0:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_PROCESS_PROBE_FAILED",
            "无法核对 voice-typist 进程",
        )
    raw = str(completed.stdout or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_PROCESS_PROBE_FAILED",
            "voice-typist 进程探针返回无效 JSON",
        ) from exc
    rows = value if isinstance(value, list) else [value]
    result: list[TypistProcess] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_PROCESS_PROBE_FAILED",
                "voice-typist 进程探针结构无效",
            )
        result.append(
            TypistProcess(
                pid=int(row.get("pid")),
                session_id=int(row.get("sessionId")),
                command_line=str(row.get("commandLine") or ""),
            )
        )
    return result


class VoiceTypistLauncher:
    """Idempotently ensure the fixed local voice-typist is running."""

    def __init__(
        self,
        *,
        launcher_path: Path = DEFAULT_TYPING_LAUNCHER,
        typist_script: Path = DEFAULT_TYPING_SCRIPT,
        powershell: str = "powershell.exe",
        runner: Callable[
            [Sequence[str], float], subprocess.CompletedProcess[str]
        ] = _default_runner,
        process_probe: Callable[[], Iterable[TypistProcess]] = _default_typist_process_probe,
        session_id_provider: Callable[[], int] = _current_windows_session_id,
    ) -> None:
        # Paths are constructor-time local policy.  No command or server payload
        # is allowed to override them.
        self.launcher_path = Path(launcher_path)
        self.typist_script = Path(typist_script)
        self.powershell = powershell
        self._runner = runner
        self._process_probe = process_probe
        self._session_id_provider = session_id_provider

    def ensure_running(self) -> dict[str, Any]:
        before = self.verified_status()
        if before.emergency_stop:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_ESTOP",
                "voice-typist 处于紧急停止状态，拒绝自动解除",
            )
        if before.paused:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_PAUSED",
                "voice-typist 已暂停，拒绝自动恢复",
            )
        if before.running:
            return {
                "contract": CONTRACT,
                "running": True,
                "pid": before.pid,
                "result": "already-running",
            }

        started = self._invoke("Start", timeout=20.0)
        # Even an "already running" launcher error is not trusted by text.  A
        # fresh Status + exact process postcondition is the only success proof.
        after = self.verified_status()
        if after.emergency_stop or after.paused or not after.running:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_START_FAILED",
                "voice-typist 启动后置条件未成立",
            )
        return {
            "contract": CONTRACT,
            "running": True,
            "pid": after.pid,
            "result": "started" if started.returncode == 0 else "raced-running",
        }

    def verified_status(self) -> TypistState:
        completed = self._invoke("Status", timeout=10.0)
        if completed.returncode != 0:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_STATUS_FAILED",
                "无法读取 voice-typist 状态",
            )
        status = _last_json_object(completed.stdout, label="voice-typist Status")
        required = {"running", "pid", "paused", "emergencyStop"}
        if not required.issubset(status):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_STATUS_INVALID",
                "voice-typist 状态缺少必要字段",
            )
        if not all(
            isinstance(status[name], bool)
            for name in ("running", "paused", "emergencyStop")
        ):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_STATUS_INVALID",
                "voice-typist 状态类型无效",
            )
        status_pid = status["pid"]
        if status_pid is not None and (
            isinstance(status_pid, bool) or not isinstance(status_pid, int)
        ):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_STATUS_INVALID",
                "voice-typist PID 无效",
            )

        session_id = int(self._session_id_provider())
        if session_id == 0:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_NON_INTERACTIVE",
                "监督器不在 Windows 交互会话，拒绝桌面控制",
            )
        matches = [
            process
            for process in self._process_probe()
            if process.session_id == session_id
            and _windows_path_token_present(
                process.command_line,
                self.typist_script,
            )
        ]
        if len(matches) > 1:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_AMBIGUOUS",
                "发现多个 voice-typist 进程，拒绝自动修理",
            )
        process_pid = matches[0].pid if matches else None
        running = bool(status["running"])
        if running and (status_pid is None or process_pid != status_pid):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_PID_MISMATCH",
                "voice-typist PID 文件与真实进程不一致",
            )
        if not running and process_pid is not None:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_ORPHAN",
                "发现未被 launcher 管理的 voice-typist 进程",
            )
        if not running and status_pid is not None:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_PID_MISMATCH",
                "voice-typist 停止状态仍携带 PID",
            )
        return TypistState(
            running=running,
            pid=status_pid if running else None,
            paused=bool(status["paused"]),
            emergency_stop=bool(status["emergencyStop"]),
        )

    def _invoke(
        self,
        action: str,
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if action not in _ALLOWED_LAUNCHER_ACTIONS:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_ACTION_DENIED",
                "监督器只允许读取或启动 voice-typist",
            )
        return self._runner(
            (
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.launcher_path),
                "-Action",
                action,
            ),
            timeout,
        )


class CommandReceiptStore:
    """Small durable ledger that prevents shortcut replay after a crash."""

    def __init__(self, path: Path, *, limit: int = 128) -> None:
        self.path = Path(path)
        self.limit = max(16, int(limit))

    def get(self, command_id: str) -> dict[str, Any] | None:
        rows = self._read()
        value = rows.get(command_id)
        return dict(value) if isinstance(value, dict) else None

    def record(self, command_id: str, state: str, *, at: float) -> None:
        normalized = _require_command_id(command_id)
        rows = self._read()
        rows[normalized] = {"state": str(state), "at": float(at)}
        ordered = sorted(
            rows.items(),
            key=lambda item: float(item[1].get("at", 0.0)),
        )[-self.limit :]
        self._write(dict(ordered))

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_RECEIPT_INVALID",
                "本地启动回执损坏，拒绝发送快捷键",
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("contract") != CONTRACT
            or not isinstance(value.get("commands"), dict)
        ):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_RECEIPT_INVALID",
                "本地启动回执合同无效",
            )
        result: dict[str, dict[str, Any]] = {}
        for key, row in value["commands"].items():
            _require_command_id(key)
            if not isinstance(row, dict) or set(row) != {"state", "at"}:
                raise SupervisorError(
                    "BW_COMPUTER_VOICE_RECEIPT_INVALID",
                    "本地启动回执记录无效",
                )
            result[key] = dict(row)
        return result

    def _write(self, commands: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"contract": CONTRACT, "commands": commands},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def _require_command_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _SAFE_COMMAND_ID.fullmatch(text):
        raise SupervisorError(
            "BW_COMPUTER_VOICE_COMMAND_INVALID",
            "启动命令 ID 无效",
        )
    return text


class CombinedVoiceStartCoordinator:
    """Execute one authenticated combined-start command at most once."""

    def __init__(
        self,
        *,
        app_probe: Callable[[], Mapping[str, Any]],
        capture: CaptureAdapter,
        typist: VoiceTypistLauncher,
        shortcut_sender: Callable[[], bool],
        receipts: CommandReceiptStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_probe = app_probe
        self._capture = capture
        self._typist = typist
        self._shortcut_sender = shortcut_sender
        self._receipts = receipts
        self._clock = clock

    def execute(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(command, Mapping):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_INVALID",
                "启动命令必须是对象",
            )
        if set(command) != {
            "contract",
            "commandId",
            "nonce",
            "action",
            "expiresAt",
        }:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_INVALID",
                "启动命令字段不匹配",
            )
        if (
            command.get("contract") != BRIDGE_COMMAND_CONTRACT
            or command.get("action") != START_ACTION
        ):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_INVALID",
                "启动命令合同或动作无效",
            )
        command_id = _require_command_id(command.get("commandId"))
        nonce = command.get("nonce")
        if not isinstance(nonce, str) or not (16 <= len(nonce) <= 512):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_INVALID",
                "启动命令 nonce 无效",
            )
        expires_at = command.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_INVALID",
                "启动命令过期时间无效",
            )
        now = float(self._clock())
        remaining = float(expires_at) - now
        if remaining <= 0:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_EXPIRED",
                "启动命令已过期",
            )
        if remaining > 15:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_COMMAND_INVALID",
                "启动命令有效期超出本地安全上限",
            )
        prior = self._receipts.get(command_id)
        if prior is not None:
            return {
                "contract": CONTRACT,
                "commandId": command_id,
                "result": "duplicate-suppressed",
                "shortcutSent": False,
                "receiptState": prior.get("state"),
            }

        app = dict(self._app_probe())
        if app.get("ready") is not True:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_APP_NOT_READY",
                str(app.get("reason") or "Codex/ChatGPT 桌面程序未就绪"),
            )
        root_pid = app.get("rootProcessId")
        app_kind = app.get("appKind")
        if isinstance(root_pid, bool) or not isinstance(root_pid, int) or root_pid <= 0:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_APP_NOT_READY",
                "目标应用根进程无效",
            )
        if app_kind not in {"chatgpt-desktop", "codex-desktop"}:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_APP_NOT_READY",
                "目标应用类型无效",
            )

        capture_owned = False
        try:
            capture = dict(self._capture.ensure_started(root_pid))
            capture_owned = bool(capture.get("owned"))
            if (
                capture.get("active") is not True
                or capture.get("microphoneActive") is not True
                or capture.get("scope") != "process-only"
                or capture.get("rootProcessId") != root_pid
                or capture.get("outputTarget") != app_kind
                or capture.get("nativeHostReady") is not True
                or capture.get("mediaHostReady") is not True
                or capture.get("rtcConnected") is not True
            ):
                raise SupervisorError(
                    "BW_COMPUTER_VOICE_CAPTURE_NOT_READY",
                    "目标进程音频、麦克风或直连媒体尚未就绪",
                )
            typist = self._typist.ensure_running()
            if typist.get("running") is not True:
                raise SupervisorError(
                    "BW_COMPUTER_VOICE_TYPIST_START_FAILED",
                    "voice-typist 尚未就绪",
                )

            # Persist before SendInput.  A crash between these two lines may
            # suppress one legitimate retry, but it can never duplicate a
            # shortcut.  This is the intended fail-closed trade-off.
            self._receipts.record(command_id, "shortcut-attempted", at=now)
            sent = self._shortcut_sender()
            if sent is not True:
                self._receipts.record(command_id, "shortcut-failed", at=self._clock())
                raise SupervisorError(
                    "BW_COMPUTER_VOICE_SHORTCUT_FAILED",
                    "语音启动快捷键未被目标应用确认接收",
                )
            self._receipts.record(command_id, "started", at=self._clock())
            return {
                "contract": CONTRACT,
                "commandId": command_id,
                "result": "started",
                "shortcutSent": True,
                "captureActive": True,
                "rtcConnected": True,
                "typist": {
                    "running": True,
                    "result": typist.get("result"),
                },
            }
        except Exception:
            if capture_owned:
                self._capture.rollback_if_owned()
            raise
