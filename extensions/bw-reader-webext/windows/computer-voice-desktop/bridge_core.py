from __future__ import annotations

import base64
from collections import OrderedDict
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlsplit
import uuid


DIRECT_CONFIG_CONTRACT = "reader-computer-voice-direct-config/5"
FIXED_AUDIO_BUS_DIRECT_CONFIG_CONTRACT = (
    "reader-computer-voice-direct-config/6"
)
LEGACY_V4_DIRECT_CONFIG_CONTRACT = "reader-computer-voice-direct-config/4"
LEGACY_DIRECT_CONFIG_CONTRACT = "reader-computer-voice-direct-config/1"
DIRECT_STATUS_CONTRACT = (
    "reader-computer-voice-direct-runtime-status/2"
)
SERVICE_RECORD_CONTRACT = "reader-computer-voice-desktop-service/1"
SELF_TEST_CONTRACT = "reader-computer-voice-desktop-self-test/1"

FIXED_LISTEN_HOST = "127.0.0.1"
FIXED_LISTEN_PORT = 43128
DIRECT_SERVE_PATH = "/reader-computer-voice/v1"
DIRECT_WSS_URL = (
    "wss://bwicarus-2.taile44d0c.ts.net"
    f"{DIRECT_SERVE_PATH}"
)
FIXED_ALLOWED_TAILSCALE_USER_LOGIN = "bwicarus@gmail.com"
FIXED_OUTPUT_SCOPE = "process-only"
FIXED_APP_KIND = "codex-desktop"
FIXED_SHORTCUT = "F24"
SHORTCUT_BROKER_CONTRACT = "bw-codex-voice-shortcut/1"
SHORTCUT_BROKER_PIPE_NAME = "bw-reader-codex-voice-shortcut-v1"
SHORTCUT_BROKER_PIPE_PATH = rf"\\.\pipe\{SHORTCUT_BROKER_PIPE_NAME}"
SHORTCUT_BROKER_MAX_REQUEST_BYTES = 1024
SHORTCUT_BROKER_RECEIPT_CACHE_SIZE = 256
SHORTCUT_BROKER_READY_TIMEOUT_SECONDS = 2.0
CONTEXT_DELIVERY_LEGACY = "legacy-inject"
CONTEXT_DELIVERY_SNAPSHOT = "snapshot-mcp"
CONTEXT_DELIVERY_MODES = frozenset(
    {CONTEXT_DELIVERY_LEGACY, CONTEXT_DELIVERY_SNAPSHOT}
)
NATIVE_APP_ORIGIN = "http://127.0.0.1:43129"
DEFAULT_ALLOWED_ORIGINS = (
    "https://bwicarus.taile44d0c.ts.net",
    NATIVE_APP_ORIGIN,
)
ONLINE_STATES = frozenset(
    {
        "starting",
        "idle",
        "reader-connected",
        "starting-app",
        "waiting-app-ready",
        "waiting-voice-ready",
        "starting-capture",
        "active",
    }
)
TASK_NAME = "BW Computer Voice Direct Bootstrap"
CREATE_NO_WINDOW = 0x08000000
SUPERVISOR_POLL_SECONDS = 5.0
SUPERVISOR_STABLE_POLLS = 3
SUPERVISOR_UNRESPONSIVE_POLLS = 3
SUPERVISOR_RESTART_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
DISABLE_STOP_RECHECK_SECONDS = (0.05, 0.1, 0.2, 0.4)
_CAPTURE_ENDPOINT_UNSET = object()


_SHORTCUT_REQUEST_ID = re.compile(r"^shortcut-([A-Za-z0-9_-]{22})$")
_UTC_ROUNDTRIP_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,7})?Z$"
)


def _valid_shortcut_request_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = _SHORTCUT_REQUEST_ID.fullmatch(value)
    if match is None:
        return False
    encoded = match.group(1)
    try:
        decoded = base64.urlsafe_b64decode(encoded + "==")
    except (ValueError, TypeError):
        return False
    return (
        len(decoded) == 16
        and base64.urlsafe_b64encode(decoded)
        .rstrip(b"=")
        .decode("ascii")
        == encoded
    )


def _valid_utc_roundtrip_time(value: object) -> bool:
    if (
        not isinstance(value, str)
        or _UTC_ROUNDTRIP_TIME.fullmatch(value) is None
    ):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _expected_f24_binding_is_configured(
    path: Path | None = None,
) -> bool:
    path = path or (Path.home() / ".codex" / "keybindings.json")
    try:
        if not path.is_file() or path.stat().st_size not in range(1, 65537):
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, list):
        return False
    command_count = 0
    shortcut_count = 0
    exact_binding = False
    for item in value:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        key = item.get("key")
        if command == "realtimeVoice":
            command_count += 1
            exact_binding = exact_binding or key == "F24"
        if key == "F24":
            shortcut_count += 1
    return command_count == 1 and shortcut_count == 1 and exact_binding


def _send_f24_keybd_event() -> None:
    if os.name != "nt":
        raise ShortcutBrokerError("F24 broker 只允许在 Windows 运行。")
    if not _expected_f24_binding_is_configured():
        raise ShortcutBrokerError("Codex 全局语音快捷键必须唯一配置为 F24。")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.keybd_event.restype = None
    # Exactly one activation pair.  There is deliberately no retry, fallback,
    # foreground switch, or second toggle in this process.
    user32.keybd_event(0x87, 0, 0, 0)
    user32.keybd_event(0x87, 0, 0x0002, 0)


class ShortcutBrokerRequestProcessor:
    def __init__(
        self,
        send_shortcut: Callable[[], None] = _send_f24_keybd_event,
        *,
        cache_size: int = SHORTCUT_BROKER_RECEIPT_CACHE_SIZE,
    ) -> None:
        if cache_size <= 0:
            raise ValueError("shortcut receipt cache size must be positive")
        self._send_shortcut = send_shortcut
        self._cache_size = cache_size
        self._receipts: OrderedDict[
            str, tuple[tuple[int, str], dict[str, Any]]
        ] = OrderedDict()

    @staticmethod
    def _failure(request_id: str, code: str) -> dict[str, Any]:
        return {
            "contract": SHORTCUT_BROKER_CONTRACT,
            "type": "receipt",
            "requestId": request_id,
            "ok": False,
            "code": code,
        }

    def process(self, payload: bytes) -> bytes:
        request_id = "invalid"
        if (
            not payload
            or len(payload) > SHORTCUT_BROKER_MAX_REQUEST_BYTES
            or not payload.endswith(b"\n")
            or b"\n" in payload[:-1]
            or b"\r" in payload
        ):
            receipt = self._failure(
                request_id,
                "BW_SHORTCUT_BROKER_INVALID_REQUEST",
            )
            return self._encode(receipt)
        try:
            value = json.loads(payload[:-1].decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            receipt = self._failure(
                request_id,
                "BW_SHORTCUT_BROKER_INVALID_REQUEST",
            )
            return self._encode(receipt)
        if isinstance(value, dict) and isinstance(
            value.get("requestId"), str
        ):
            request_id = value["requestId"]
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "contract",
                "type",
                "requestId",
                "rootProcessId",
                "rootProcessStartTimeUtc",
                "windowHandle",
            }
            or value.get("contract") != SHORTCUT_BROKER_CONTRACT
            or value.get("type") != "toggle"
            or not _valid_shortcut_request_id(request_id)
            or isinstance(value.get("rootProcessId"), bool)
            or not isinstance(value.get("rootProcessId"), int)
            or not 0 < value["rootProcessId"] <= 0xFFFFFFFF
            or not _valid_utc_roundtrip_time(
                value.get("rootProcessStartTimeUtc")
            )
            or isinstance(value.get("windowHandle"), bool)
            or not isinstance(value.get("windowHandle"), int)
            or not 0 < value["windowHandle"] <= 0x7FFFFFFFFFFFFFFF
        ):
            receipt = self._failure(
                request_id if _valid_shortcut_request_id(request_id) else "invalid",
                "BW_SHORTCUT_BROKER_INVALID_REQUEST",
            )
            return self._encode(receipt)

        signature = (
            value["rootProcessId"],
            value["rootProcessStartTimeUtc"],
            value["windowHandle"],
        )
        cached = self._receipts.get(request_id)
        if cached is not None:
            self._receipts.move_to_end(request_id)
            if cached[0] != signature:
                return self._encode(
                    self._failure(
                        request_id,
                        "BW_SHORTCUT_BROKER_REQUEST_ID_CONFLICT",
                    )
                )
            return self._encode(cached[1])

        try:
            self._send_shortcut()
        except Exception:
            receipt = self._failure(
                request_id,
                "BW_SHORTCUT_BROKER_TOGGLE_FAILED",
            )
        else:
            receipt = {
                "contract": SHORTCUT_BROKER_CONTRACT,
                "type": "receipt",
                "requestId": request_id,
                "ok": True,
            }
        self._receipts[request_id] = (signature, receipt)
        self._receipts.move_to_end(request_id)
        while len(self._receipts) > self._cache_size:
            self._receipts.popitem(last=False)
        return self._encode(receipt)

    @staticmethod
    def _encode(receipt: dict[str, Any]) -> bytes:
        return (
            json.dumps(
                receipt,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

# These identifiers are local constants.  Reader input is never accepted as an
# application path, command, or AUMID.
LOCAL_PACKAGED_APP_IDS = {
    "codex-desktop": "OpenAI.CodexBeta_2p2nqsd0c76g0!App",
    "chatgpt-classic": (
        "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT"
    ),
}

DIRECT_CONFIG_KEYS = frozenset(
    {
        "contract",
        "localOptIn",
        "experimentalSingleUserMode",
        "virtualMicrophoneRenderEndpointId",
        "virtualMicrophoneCaptureEndpointId",
        "virtualSpeakerRenderEndpointId",
        "listenHost",
        "listenPort",
        "allowedOrigins",
        "allowedTailscaleUserLogin",
        "outputScope",
        "appKind",
        "runtimeStatusPath",
        "contextDeliveryMode",
    }
)
FIXED_AUDIO_BUS_DIRECT_CONFIG_KEYS = (
    DIRECT_CONFIG_KEYS | {"virtualSpeakerCaptureEndpointId"}
)
LEGACY_V4_DIRECT_CONFIG_KEYS = (
    DIRECT_CONFIG_KEYS - {"virtualMicrophoneCaptureEndpointId"}
)
LEGACY_V1_DIRECT_CONFIG_KEYS = (
    DIRECT_CONFIG_KEYS
    - {
        "virtualMicrophoneRenderEndpointId",
        "virtualMicrophoneCaptureEndpointId",
        "virtualSpeakerRenderEndpointId",
        "contextDeliveryMode",
    }
    | {
        "microphoneEndpointId",
        "pairingCodeHash",
        "pairingExpiresAtUtc",
        "pairedClientPublicKeySpki",
        "pairedClientFingerprintSha256",
    }
)
LEGACY_V1_CONFIG_KEYS_WITHOUT_MODE = LEGACY_V1_DIRECT_CONFIG_KEYS - {
    "experimentalSingleUserMode"
}
DIRECT_STATUS_KEYS = frozenset(
    {
        "contract",
        "serviceInstanceId",
        "pid",
        "state",
        "readerConnected",
        "captureActive",
        "lastError",
        "updatedAtUtc",
    }
)
SERVICE_RECORD_KEYS = frozenset(
    {
        "contract",
        "pid",
        "executable",
        "configPath",
        "startedAtUtc",
    }
)


class BridgeError(RuntimeError):
    pass


class ShortcutBrokerError(BridgeError):
    """A fixed local shortcut-broker contract or transport failed."""


class LocalOptOutDuringStart(BridgeError):
    """The just-started owned child was stopped after a concurrent opt-out."""


@dataclass(frozen=True)
class RenderEndpoint:
    endpoint_id: str
    friendly_name: str

    @property
    def display_name(self) -> str:
        return self.friendly_name or self.endpoint_id


@dataclass(frozen=True)
class CaptureEndpoint:
    endpoint_id: str
    friendly_name: str

    @property
    def display_name(self) -> str:
        return self.friendly_name or self.endpoint_id


@dataclass(frozen=True)
class BridgePaths:
    root: Path
    native_host: Path
    desktop_launcher: Path
    direct_config: Path
    runtime_status: Path
    service_record: Path

    @classmethod
    def for_root(cls, root: Path) -> "BridgePaths":
        root = root.resolve()
        return cls(
            root=root,
            native_host=(
                root / "native-host" / "bw-computer-voice-audio.exe"
            ),
            desktop_launcher=(
                root
                / "desktop-launcher"
                / "BW-Computer-Voice-Bridge.exe"
            ),
            direct_config=(
                root
                / "native-host"
                / "computer-voice-direct.config.json"
            ),
            runtime_status=(
                root
                / "runtime"
                / "computer-voice-direct.status.json"
            ),
            service_record=(
                root
                / "runtime"
                / "computer-voice-direct.service.json"
            ),
        )

    @classmethod
    def discover(cls) -> "BridgePaths":
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent.parent)
        candidates.append(Path.home() / "bw-computer-voice-bridge")
        root = next(
            (
                candidate
                for candidate in candidates
                if (
                    candidate
                    / "native-host"
                    / "bw-computer-voice-audio.exe"
                ).is_file()
            ),
            candidates[0],
        )
        return cls.for_root(root)


@dataclass(frozen=True)
class DirectStatus:
    configuration_enabled: bool
    service_online: bool
    reader_connected: bool
    reason: str
    pid: int | None = None
    service_instance_id: str = ""
    capture_active: bool = False
    last_error: dict[str, Any] | None = None


@dataclass(frozen=True)
class TailscaleCommandPlan:
    status: tuple[str, ...]
    serve_status: tuple[str, ...]
    apply_serve: tuple[str, ...]
    rollback_serve: tuple[str, ...]


class ProcessRunner(Protocol):
    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
    ) -> int:
        ...

    def executable_for_pid(self, pid: int) -> Path | None:
        ...

    def terminate_exact(self, pid: int, executable: Path) -> bool:
        ...


class ReadOnlyCommandRunner(Protocol):
    def run_read_only(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise BridgeError("UTC 时间必须带时区。")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_valid_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value == NATIVE_APP_ORIGIN:
        return True
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and value == f"https://{parsed.netloc}"
    )


def _validate_render_endpoint_id(value: str, role: str) -> str:
    if (
        not value
        or value.isspace()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
    ):
        raise BridgeError(f"必须明确选择一个有效的{role}播放端点。")
    return value


_RENDER_ENDPOINT_ID = re.compile(
    r"^\{0\.0\.0\.00000000\}\."
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$"
)
_CAPTURE_ENDPOINT_ID = re.compile(
    r"^\{0\.0\.1\.00000000\}\."
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$"
)


def _validate_virtual_render_endpoints(
    virtual_microphone_render_endpoint_id: str,
    virtual_speaker_render_endpoint_id: str,
    *,
    strict_flow: bool = False,
) -> tuple[str, str]:
    virtual_microphone = _validate_render_endpoint_id(
        virtual_microphone_render_endpoint_id,
        "虚拟麦克风 A",
    )
    virtual_speaker = _validate_render_endpoint_id(
        virtual_speaker_render_endpoint_id,
        "虚拟扬声器 B",
    )
    if virtual_microphone == virtual_speaker:
        raise BridgeError("虚拟麦克风 A 与虚拟扬声器 B 必须选择不同端点。")
    if strict_flow and (
        _RENDER_ENDPOINT_ID.fullmatch(virtual_microphone) is None
        or _RENDER_ENDPOINT_ID.fullmatch(virtual_speaker) is None
    ):
        raise BridgeError("自动路由要求两个明确的 eRender MMDevice ID。")
    return virtual_microphone, virtual_speaker


def _validate_virtual_microphone_capture_endpoint(
    value: str,
) -> str:
    if (
        not value
        or value.isspace()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
        or _CAPTURE_ENDPOINT_ID.fullmatch(value) is None
    ):
        raise BridgeError("自动路由要求明确的虚拟麦克风 eCapture MMDevice ID。")
    return value


def _validate_virtual_speaker_capture_endpoint(
    value: str,
) -> str:
    if (
        not value
        or value.isspace()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
        or _CAPTURE_ENDPOINT_ID.fullmatch(value) is None
    ):
        raise BridgeError(
            "固定音频总线要求明确的虚拟扬声器 B eCapture MMDevice ID。"
        )
    return value


def _validate_runtime_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError("直连状态文件路径无效。")
    path = Path(value)
    if not path.is_absolute():
        raise BridgeError("直连状态文件必须使用绝对路径。")
    return str(path.resolve())


def validate_direct_config(
    value: dict[str, Any],
    *,
    expected_runtime_status: Path | None = None,
) -> dict[str, Any]:
    contract = value.get("contract")
    legacy_v4 = contract == LEGACY_V4_DIRECT_CONFIG_CONTRACT
    fixed_audio_bus = (
        contract == FIXED_AUDIO_BUS_DIRECT_CONFIG_CONTRACT
    )
    keys = set(value)
    expected_keys = (
        LEGACY_V4_DIRECT_CONFIG_KEYS
        if legacy_v4
        else (
            FIXED_AUDIO_BUS_DIRECT_CONFIG_KEYS
            if fixed_audio_bus
            else DIRECT_CONFIG_KEYS
        )
    )
    if keys != expected_keys:
        raise BridgeError("直连配置字段不完整或包含未知字段。")
    if contract not in (
        DIRECT_CONFIG_CONTRACT,
        FIXED_AUDIO_BUS_DIRECT_CONFIG_CONTRACT,
        LEGACY_V4_DIRECT_CONFIG_CONTRACT,
    ):
        raise BridgeError("直连配置合同版本不匹配。")
    if not isinstance(value.get("localOptIn"), bool):
        raise BridgeError("直连配置授权状态无效。")
    experimental_single_user_mode = value.get(
        "experimentalSingleUserMode"
    )
    if experimental_single_user_mode is not True:
        raise BridgeError("单用户实验模式状态无效。")
    virtual_microphone, virtual_speaker = (
        _validate_virtual_render_endpoints(
            str(value.get("virtualMicrophoneRenderEndpointId", "")),
            str(value.get("virtualSpeakerRenderEndpointId", "")),
            strict_flow=not legacy_v4,
        )
    )
    virtual_microphone_capture = (
        None
        if legacy_v4
        else _validate_virtual_microphone_capture_endpoint(
            str(value.get("virtualMicrophoneCaptureEndpointId", ""))
        )
    )
    virtual_speaker_capture = (
        _validate_virtual_speaker_capture_endpoint(
            str(value.get("virtualSpeakerCaptureEndpointId", ""))
        )
        if fixed_audio_bus
        else None
    )
    if (
        virtual_speaker_capture is not None
        and virtual_speaker_capture == virtual_microphone_capture
    ):
        raise BridgeError("虚拟麦克风 A 与虚拟扬声器 B 的录音端必须不同。")
    if value.get("listenHost") != FIXED_LISTEN_HOST:
        raise BridgeError("直连服务只允许绑定 127.0.0.1。")
    if value.get("listenPort") != FIXED_LISTEN_PORT:
        raise BridgeError("直连服务端口不符合固定合同。")
    origins = value.get("allowedOrigins")
    if (
        not isinstance(origins, list)
        or not origins
        or len(origins) > 8
        or len(set(origins)) != len(origins)
        or not all(_is_valid_origin(origin) for origin in origins)
    ):
        raise BridgeError("Reader 来源白名单无效。")
    if (
        value.get("allowedTailscaleUserLogin")
        != FIXED_ALLOWED_TAILSCALE_USER_LOGIN
    ):
        raise BridgeError("Tailscale 登录身份不符合固定合同。")
    if value.get("outputScope") != FIXED_OUTPUT_SCOPE:
        raise BridgeError("禁止使用全系统输出回退。")
    if value.get("appKind") != FIXED_APP_KIND:
        raise BridgeError("目标应用类型不在本机允许列表中。")
    context_delivery_mode = value.get("contextDeliveryMode")
    if context_delivery_mode not in CONTEXT_DELIVERY_MODES:
        raise BridgeError("上下文交付模式无效。")
    runtime_path = _validate_runtime_path(value.get("runtimeStatusPath"))
    if (
        expected_runtime_status is not None
        and os.path.normcase(runtime_path)
        != os.path.normcase(str(expected_runtime_status.resolve()))
    ):
        raise BridgeError("直连状态文件偏离固定安装目录。")
    validated = {
        **value,
        "experimentalSingleUserMode": experimental_single_user_mode,
        "virtualMicrophoneRenderEndpointId": virtual_microphone,
        "virtualSpeakerRenderEndpointId": virtual_speaker,
        "allowedOrigins": list(origins),
        "runtimeStatusPath": runtime_path,
        "contextDeliveryMode": context_delivery_mode,
    }
    if virtual_microphone_capture is not None:
        validated["virtualMicrophoneCaptureEndpointId"] = (
            virtual_microphone_capture
        )
    if virtual_speaker_capture is not None:
        validated["virtualSpeakerCaptureEndpointId"] = (
            virtual_speaker_capture
        )
    return validated


def load_direct_config(paths: BridgePaths) -> dict[str, Any] | None:
    value = read_json(paths.direct_config)
    if value is None:
        return None
    try:
        return validate_direct_config(
            value,
            expected_runtime_status=paths.runtime_status,
        )
    except BridgeError:
        return None


def migrate_native_app_origin(paths: BridgePaths) -> bool:
    """Atomically add the one fixed App origin to an older valid config."""

    value = read_json(paths.direct_config)
    if value is None:
        return False
    try:
        validated = validate_direct_config(
            value,
            expected_runtime_status=paths.runtime_status,
        )
    except BridgeError:
        # Migration is allowed to extend only an already-valid predecessor;
        # the normal start/load gate remains responsible for rejecting damage.
        return False
    origins = list(validated["allowedOrigins"])
    if NATIVE_APP_ORIGIN in origins:
        return False
    if len(origins) >= 8:
        raise BridgeError(
            "Reader 来源白名单已满，无法加入固定原生 App 来源。"
        )
    migrated = validate_direct_config(
        {
            **validated,
            "allowedOrigins": [*origins, NATIVE_APP_ORIGIN],
        },
        expected_runtime_status=paths.runtime_status,
    )
    _atomic_write_json(paths.direct_config, migrated)
    return True


def legacy_microphone_config_requires_migration(
    paths: BridgePaths,
) -> bool:
    value = read_json(paths.direct_config)
    return bool(
        value
        and value.get("contract") == LEGACY_DIRECT_CONFIG_CONTRACT
        and set(value) in (
            LEGACY_V1_DIRECT_CONFIG_KEYS,
            LEGACY_V1_CONFIG_KEYS_WITHOUT_MODE,
        )
        and isinstance(value.get("microphoneEndpointId"), str)
        and value.get("microphoneEndpointId")
    )


def build_direct_config(
    virtual_microphone_render_endpoint_id: str,
    virtual_speaker_render_endpoint_id: str,
    runtime_status_path: Path,
    *,
    allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS,
    local_opt_in: bool = True,
    experimental_single_user_mode: bool = True,
    context_delivery_mode: str = CONTEXT_DELIVERY_LEGACY,
    virtual_microphone_capture_endpoint_id: str | None = None,
    virtual_speaker_capture_endpoint_id: str | None = None,
) -> dict[str, Any]:
    virtual_microphone, virtual_speaker = (
        _validate_virtual_render_endpoints(
            virtual_microphone_render_endpoint_id,
            virtual_speaker_render_endpoint_id,
            strict_flow=(
                virtual_microphone_capture_endpoint_id is not None
            ),
        )
    )
    virtual_microphone_capture = (
        None
        if virtual_microphone_capture_endpoint_id is None
        else _validate_virtual_microphone_capture_endpoint(
            virtual_microphone_capture_endpoint_id
        )
    )
    virtual_speaker_capture = (
        None
        if virtual_speaker_capture_endpoint_id is None
        else _validate_virtual_speaker_capture_endpoint(
            virtual_speaker_capture_endpoint_id
        )
    )
    if (
        virtual_speaker_capture is not None
        and virtual_microphone_capture is None
    ):
        raise BridgeError("固定音频总线同时需要 A 与 B 的录音端点。")
    if virtual_speaker_capture == virtual_microphone_capture and (
        virtual_speaker_capture is not None
    ):
        raise BridgeError("虚拟麦克风 A 与虚拟扬声器 B 的录音端必须不同。")
    if experimental_single_user_mode is not True:
        raise BridgeError("单用户实验模式状态无效。")
    if context_delivery_mode not in CONTEXT_DELIVERY_MODES:
        raise BridgeError("上下文交付模式无效。")
    origins = list(allowed_origins)
    if not origins or not all(_is_valid_origin(origin) for origin in origins):
        raise BridgeError("Reader 来源白名单无效。")

    value = {
        "contract": (
            FIXED_AUDIO_BUS_DIRECT_CONFIG_CONTRACT
            if virtual_speaker_capture is not None
            else (
                DIRECT_CONFIG_CONTRACT
                if virtual_microphone_capture is not None
                else LEGACY_V4_DIRECT_CONFIG_CONTRACT
            )
        ),
        "localOptIn": bool(local_opt_in),
        "experimentalSingleUserMode": experimental_single_user_mode,
        "virtualMicrophoneRenderEndpointId": virtual_microphone,
        "virtualSpeakerRenderEndpointId": virtual_speaker,
        "listenHost": FIXED_LISTEN_HOST,
        "listenPort": FIXED_LISTEN_PORT,
        "allowedOrigins": origins,
        "allowedTailscaleUserLogin":
            FIXED_ALLOWED_TAILSCALE_USER_LOGIN,
        "outputScope": FIXED_OUTPUT_SCOPE,
        "appKind": FIXED_APP_KIND,
        "runtimeStatusPath": str(runtime_status_path.resolve()),
        "contextDeliveryMode": context_delivery_mode,
    }
    if virtual_microphone_capture is not None:
        value["virtualMicrophoneCaptureEndpointId"] = (
            virtual_microphone_capture
        )
    if virtual_speaker_capture is not None:
        value["virtualSpeakerCaptureEndpointId"] = virtual_speaker_capture
    return validate_direct_config(value)


def save_enabled_config(
    paths: BridgePaths,
    virtual_microphone: RenderEndpoint,
    virtual_speaker: RenderEndpoint,
    *,
    allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS,
    active_render_endpoints: Sequence[RenderEndpoint] | None = None,
    active_capture_endpoints: Sequence[CaptureEndpoint] | None = None,
    allow_legacy_migration: bool = False,
    context_delivery_mode: str | None = None,
    virtual_microphone_capture_endpoint_id: (
        str | None | object
    ) = _CAPTURE_ENDPOINT_UNSET,
) -> dict[str, Any]:
    if not paths.native_host.is_file():
        raise BridgeError(f"直连代理不存在：{paths.native_host}")
    _validate_virtual_render_endpoints(
        virtual_microphone.endpoint_id,
        virtual_speaker.endpoint_id,
    )
    if active_render_endpoints is not None:
        active_ids = {
            item.endpoint_id for item in active_render_endpoints
        }
        if virtual_microphone.endpoint_id not in active_ids:
            raise BridgeError(
                "虚拟麦克风 A 已不再是 Active 播放端点；拒绝保存。"
            )
        if virtual_speaker.endpoint_id not in active_ids:
            raise BridgeError(
                "虚拟扬声器 B 已不再是 Active 播放端点；拒绝保存。"
            )
    previous = load_direct_config(paths)
    if paths.direct_config.exists() and previous is None:
        if not (
            allow_legacy_migration
            and legacy_microphone_config_requires_migration(paths)
        ):
            if legacy_microphone_config_requires_migration(paths):
                raise BridgeError(
                    "检测到旧 microphoneEndpointId 配置；"
                    "必须在桌面窗口中明确确认迁移，不能静默使用。"
                )
            raise BridgeError(
                "现有直连配置无效；拒绝静默覆盖。"
            )
    selected_context_delivery_mode = (
        context_delivery_mode
        if context_delivery_mode is not None
        else (
            str(previous["contextDeliveryMode"])
            if previous is not None
            else CONTEXT_DELIVERY_LEGACY
        )
    )
    if virtual_microphone_capture_endpoint_id is _CAPTURE_ENDPOINT_UNSET:
        selected_capture_endpoint = (
            str(previous["virtualMicrophoneCaptureEndpointId"])
            if (
                previous is not None
                and previous.get("contract")
                    in (
                        DIRECT_CONFIG_CONTRACT,
                        FIXED_AUDIO_BUS_DIRECT_CONFIG_CONTRACT,
                    )
                and previous.get(
                    "virtualMicrophoneRenderEndpointId"
                ) == virtual_microphone.endpoint_id
            )
            else None
        )
    elif virtual_microphone_capture_endpoint_id is None:
        # Explicit None is the GUI's deliberate legacy /4 choice.  It must
        # not silently resurrect a capture endpoint from an older /5 file.
        selected_capture_endpoint = None
    elif isinstance(virtual_microphone_capture_endpoint_id, str):
        selected_capture_endpoint = (
            virtual_microphone_capture_endpoint_id
        )
    else:
        raise BridgeError("虚拟麦克风录音端点选择无效。")
    if (
        selected_capture_endpoint is not None
        and active_capture_endpoints is not None
        and selected_capture_endpoint not in {
            item.endpoint_id for item in active_capture_endpoints
        }
    ):
        raise BridgeError(
            "Codex 虚拟麦克风输入已不再是 Active 录音端点；拒绝保存。"
        )
    virtual_speaker_capture_endpoint: str | None = None
    if (
        selected_capture_endpoint is not None
        and active_capture_endpoints is not None
    ):
        speaker_capture_matches = [
            item
            for item in active_capture_endpoints
            if item.friendly_name == virtual_speaker.friendly_name
            and item.endpoint_id != selected_capture_endpoint
        ]
        if len(speaker_capture_matches) != 1:
            raise BridgeError(
                "无法按虚拟扬声器 B 名称唯一找到对应录音端；"
                "请确认 B 使用同名的一对虚拟音频端点。"
            )
        virtual_speaker_capture_endpoint = (
            speaker_capture_matches[0].endpoint_id
        )
    value = build_direct_config(
        virtual_microphone.endpoint_id,
        virtual_speaker.endpoint_id,
        paths.runtime_status,
        allowed_origins=allowed_origins,
        local_opt_in=True,
        context_delivery_mode=selected_context_delivery_mode,
        virtual_microphone_capture_endpoint_id=(
            selected_capture_endpoint
        ),
        virtual_speaker_capture_endpoint_id=(
            virtual_speaker_capture_endpoint
        ),
    )
    _atomic_write_json(paths.direct_config, value)
    return value


def set_direct_config_enabled(
    paths: BridgePaths,
    enabled: bool,
    *,
    context_delivery_mode: str | None = None,
) -> bool:
    """Atomically update the ReaderPC master opt-in and delivery mode."""

    if not isinstance(enabled, bool):
        raise BridgeError("直连配置启用状态无效。")
    if (
        context_delivery_mode is not None
        and context_delivery_mode not in CONTEXT_DELIVERY_MODES
    ):
        raise BridgeError("上下文交付模式无效。")
    previous = load_direct_config(paths)
    if previous is None:
        if paths.direct_config.exists():
            raise BridgeError("现有直连配置无效；拒绝静默改写。")
        return False
    selected_mode = (
        context_delivery_mode
        if context_delivery_mode is not None
        else previous.get("contextDeliveryMode")
    )
    if (
        previous.get("localOptIn") is enabled
        and previous.get("contextDeliveryMode") == selected_mode
    ):
        return False
    value = {
        **previous,
        "localOptIn": enabled,
        "contextDeliveryMode": selected_mode,
    }
    _atomic_write_json(
        paths.direct_config,
        validate_direct_config(
            value,
            expected_runtime_status=paths.runtime_status,
        ),
    )
    return True


def restore_direct_config(
    paths: BridgePaths,
    previous: dict[str, Any],
) -> bool:
    """Atomically restore one previously validated configuration snapshot."""

    validated = validate_direct_config(
        previous,
        expected_runtime_status=paths.runtime_status,
    )
    current = load_direct_config(paths)
    if current == validated:
        return False
    _atomic_write_json(paths.direct_config, validated)
    return True


def disable_config(paths: BridgePaths) -> bool:
    return set_direct_config_enabled(paths, False)


def _require_exact_native_paths(paths: BridgePaths) -> tuple[Path, Path]:
    expected_host = (
        paths.root
        / "native-host"
        / "bw-computer-voice-audio.exe"
    ).resolve()
    expected_config = (
        paths.root
        / "native-host"
        / "computer-voice-direct.config.json"
    ).resolve()
    if (
        paths.native_host.resolve() != expected_host
        or paths.direct_config.resolve() != expected_config
    ):
        raise BridgeError("直连进程路径偏离固定安装目录。")
    if not expected_host.is_file():
        raise BridgeError(f"直连代理不存在：{expected_host}")
    if not expected_config.is_file():
        raise BridgeError(f"直连配置不存在：{expected_config}")
    return expected_host, expected_config


def build_start_command(
    paths: BridgePaths,
    *,
    readerpc_owner_pid: int | None = None,
) -> tuple[str, ...]:
    host, config = _require_exact_native_paths(paths)
    command = (
        str(host),
        "--direct-serve",
        "--config",
        str(config),
    )
    if readerpc_owner_pid is None:
        return command
    if not isinstance(readerpc_owner_pid, int) or readerpc_owner_pid <= 0:
        raise BridgeError("ReaderPC owner PID 无效。")
    return command + (
        "--readerpc-owner-pid",
        str(readerpc_owner_pid),
    )


def _require_named_executable(path: Path, expected_name: str) -> Path:
    path = path.resolve()
    if not path.is_absolute() or path.name.casefold() != expected_name.casefold():
        raise BridgeError(f"{expected_name} 路径无效。")
    return path


def build_tailscale_command_plan(
    tailscale_exe: Path,
) -> TailscaleCommandPlan:
    executable = _require_named_executable(tailscale_exe, "tailscale.exe")
    target = (
        f"http://{FIXED_LISTEN_HOST}:{FIXED_LISTEN_PORT}"
        f"{DIRECT_SERVE_PATH}"
    )
    return TailscaleCommandPlan(
        status=(str(executable), "status", "--json"),
        serve_status=(str(executable), "serve", "status", "--json"),
        apply_serve=(
            str(executable),
            "serve",
            "--yes",
            "--bg",
            "--https=443",
            f"--set-path={DIRECT_SERVE_PATH}",
            target,
        ),
        rollback_serve=(
            str(executable),
            "serve",
            "--yes",
            "--bg",
            "--https=443",
            f"--set-path={DIRECT_SERVE_PATH}",
            "off",
        ),
    )


def run_tailscale_read_only_preflight(
    tailscale_exe: Path,
    runner: ReadOnlyCommandRunner,
) -> tuple[subprocess.CompletedProcess[str], ...]:
    plan = build_tailscale_command_plan(tailscale_exe)
    # Only the two read-only commands are callable here.  Mutating Serve
    # commands deliberately have no execution helper in this module.
    return (
        runner.run_read_only(plan.status, timeout_seconds=5.0),
        runner.run_read_only(plan.serve_status, timeout_seconds=5.0),
    )


def build_local_app_launch_command(
    explorer_exe: Path,
    app_kind: str,
) -> tuple[str, ...]:
    explorer = _require_named_executable(explorer_exe, "explorer.exe")
    try:
        app_id = LOCAL_PACKAGED_APP_IDS[app_kind]
    except KeyError as error:
        raise BridgeError("目标应用不在本机允许列表中。") from error
    return (str(explorer), f"shell:AppsFolder\\{app_id}")


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _load_service_record(paths: BridgePaths) -> dict[str, Any] | None:
    value = read_json(paths.service_record)
    if (
        value is None
        or set(value) != SERVICE_RECORD_KEYS
        or value.get("contract") != SERVICE_RECORD_CONTRACT
        or parse_utc(value.get("startedAtUtc")) is None
    ):
        return None
    try:
        pid = int(value.get("pid", 0))
    except (TypeError, ValueError):
        return None
    if (
        pid <= 0
        or not _same_path(value.get("executable", ""), paths.native_host)
        or not _same_path(value.get("configPath", ""), paths.direct_config)
    ):
        return None
    return {**value, "pid": pid}


def _runtime_status_is_fresh(
    value: dict[str, Any],
    *,
    now: datetime,
    maximum_age: timedelta,
) -> bool:
    updated = parse_utc(value.get("updatedAtUtc"))
    if updated is None:
        return False
    age = now.astimezone(timezone.utc) - updated
    return timedelta(seconds=-2) <= age <= maximum_age


def _validated_runtime_error(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value)
        != {"failureId", "code", "stage", "hresult", "atUtc"}
        or not isinstance(value.get("failureId"), str)
        or not re.fullmatch(
            r"failure-[A-Za-z0-9_-]{16}",
            value["failureId"],
        )
        or not isinstance(value.get("code"), str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", value["code"])
        or not isinstance(value.get("stage"), str)
        or not re.fullmatch(r"[a-z][a-z.-]{0,79}", value["stage"])
        or (
            value.get("hresult") is not None
            and (
                not isinstance(value.get("hresult"), str)
                or not re.fullmatch(r"0x[0-9A-F]{8}", value["hresult"])
            )
        )
        or parse_utc(value.get("atUtc")) is None
    ):
        raise BridgeError("runtime lastError 合同无效。")
    return dict(value)


def read_direct_status(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(seconds=15),
) -> DirectStatus:
    config = load_direct_config(paths)
    configured = bool(config and config.get("localOptIn") is True)
    runtime = read_json(paths.runtime_status)
    try:
        last_error = (
            _validated_runtime_error(runtime.get("lastError"))
            if runtime is not None
            and set(runtime) == DIRECT_STATUS_KEYS
            and runtime.get("contract") == DIRECT_STATUS_CONTRACT
            else None
        )
    except BridgeError:
        last_error = None
        runtime = None
    record = _load_service_record(paths)
    if record is None:
        return DirectStatus(
            configured,
            False,
            False,
            "service-record-missing",
            last_error=last_error,
        )
    pid = record["pid"]
    executable = runner.executable_for_pid(pid)
    if executable is None or not _same_path(executable, paths.native_host):
        return DirectStatus(
            configured,
            False,
            False,
            "service-process-offline",
            pid=pid,
            last_error=last_error,
        )
    if (
        runtime is None
        or set(runtime) != DIRECT_STATUS_KEYS
        or runtime.get("contract") != DIRECT_STATUS_CONTRACT
        or runtime.get("pid") != pid
        or not isinstance(runtime.get("serviceInstanceId"), str)
        or not re.fullmatch(
            r"[0-9a-f]{32}",
            runtime.get("serviceInstanceId", ""),
        )
        or runtime.get("state") not in ONLINE_STATES
        or not _runtime_status_is_fresh(
            runtime,
            now=now or utc_now(),
            maximum_age=maximum_age,
        )
    ):
        return DirectStatus(
            configured,
            False,
            False,
            "runtime-status-offline-or-stale",
            pid=pid,
            last_error=last_error,
        )
    reader_connected = runtime.get("readerConnected") is True
    return DirectStatus(
        configured,
        True,
        reader_connected,
        "reader-connected" if reader_connected else "reader-not-connected",
        pid=pid,
        service_instance_id=runtime["serviceInstanceId"],
        capture_active=runtime.get("captureActive") is True,
        last_error=last_error,
    )


def start_direct_service(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    now: datetime | None = None,
    readerpc_owner_pid: int | None = None,
) -> int:
    migrate_native_app_origin(paths)
    config = load_direct_config(paths)
    if config is None or config.get("localOptIn") is not True:
        raise BridgeError("请先保存并启用直连配置。")
    current = read_direct_status(paths, runner, now=now)
    if current.service_online and current.pid is not None:
        return current.pid
    existing = _load_service_record(paths)
    if paths.service_record.exists() and existing is None:
        raise BridgeError(
            "直连服务记录存在但合同无效；拒绝覆盖或启动第二实例。"
        )
    if existing is not None:
        observed = runner.executable_for_pid(existing["pid"])
        if observed is not None:
            if not _same_path(observed, paths.native_host):
                raise BridgeError(
                    "服务记录 PID 属于陌生进程；拒绝覆盖、停止或冒充。"
                )
            raise BridgeError(
                "固定直连代理进程仍存在但状态无效；"
                "请先显式停止，拒绝启动第二实例。"
            )
    command = build_start_command(
        paths,
        readerpc_owner_pid=readerpc_owner_pid,
    )
    pid = runner.start(command, cwd=paths.native_host.parent.resolve())
    if not isinstance(pid, int) or pid <= 0:
        raise BridgeError("直连代理没有返回有效 PID。")
    record = {
        "contract": SERVICE_RECORD_CONTRACT,
        "pid": pid,
        "executable": str(paths.native_host.resolve()),
        "configPath": str(paths.direct_config.resolve()),
        "startedAtUtc": format_utc(now or utc_now()),
    }
    try:
        _atomic_write_json(paths.service_record, record)
    except Exception:
        # Do not leave an untracked listener behind if the PID record cannot
        # be committed.  The runner still revalidates PID + exact EXE.
        runner.terminate_exact(pid, paths.native_host.resolve())
        raise
    # The GUI and scheduled bootstrap are different processes, so an in-memory
    # lock cannot serialize "disable" against this launch.  Publishing the
    # owned PID record before re-reading strict config closes both orderings:
    # disable can either see and stop this PID, or this post-commit check sees
    # opt-out and stops the child itself.
    latest = load_direct_config(paths)
    if latest is None or latest.get("localOptIn") is not True:
        terminated = runner.terminate_exact(
            pid,
            paths.native_host.resolve(),
        )
        if not terminated:
            raise BridgeError(
                "直连代理启动期间已停用，但无法用同一进程句柄确认"
                "刚启动的进程停止。"
            )
        _remove_service_record_for_pid(paths, pid)
        raise LocalOptOutDuringStart(
            "直连代理启动期间已停用；刚启动的进程已安全停止。"
        )
    return pid


def _remove_service_record_for_pid(paths: BridgePaths, pid: int) -> bool:
    record = _load_service_record(paths)
    if record is None or record["pid"] != pid:
        return False
    try:
        paths.service_record.unlink()
    except FileNotFoundError:
        pass
    return True


def stop_direct_service(
    paths: BridgePaths,
    runner: ProcessRunner,
) -> bool:
    record = _load_service_record(paths)
    if record is None:
        if paths.service_record.exists():
            raise BridgeError(
                "直连服务记录存在但合同无效；拒绝按未知 PID 停止。"
            )
        return False
    pid = record["pid"]
    executable = runner.executable_for_pid(pid)
    if executable is None:
        return False
    if not _same_path(executable, paths.native_host):
        raise BridgeError("PID 对应进程不是固定直连代理；拒绝停止。")
    if not runner.terminate_exact(pid, paths.native_host.resolve()):
        raise BridgeError("直连代理未确认停止。")
    try:
        paths.service_record.unlink()
    except FileNotFoundError:
        pass
    return True


def disable_and_stop_direct_service(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    after_disable: Callable[[], None] | None = None,
    after_stop: Callable[[], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    recheck_delays: Sequence[float] = DISABLE_STOP_RECHECK_SECONDS,
) -> tuple[bool, bool]:
    if any(delay < 0 or delay > 1 for delay in recheck_delays):
        raise BridgeError("停用后的服务重查间隔无效。")
    previous = load_direct_config(paths)
    if previous is None:
        raise BridgeError(
            "没有可原子停用的有效配置；拒绝先行停止服务。"
        )
    disabled = False
    if previous.get("localOptIn") is True:
        disabled = disable_config(paths)
        current = load_direct_config(paths)
        if not disabled and (
            current is None or current.get("localOptIn") is not False
        ):
            raise BridgeError(
                "直连配置未能原子停用；拒绝停止服务。"
            )
    callback_error: Exception | None = None
    if after_disable is not None:
        try:
            after_disable()
        except Exception as exc:
            # Opt-out has already committed. Still stop the exact owned
            # listener; otherwise a snapshot write failure would leave a
            # disabled but live process behind.
            callback_error = exc

    stopped = stop_direct_service(paths, runner)

    # A bootstrap may have passed its first config read but not published its
    # PID record yet.  Its own post-commit opt-out check is the final invariant;
    # these bounded rechecks let the foreground GUI also observe and stop the
    # PID as soon as that record becomes visible.
    if not stopped:
        for delay in recheck_delays:
            sleeper(float(delay))
            if stop_direct_service(paths, runner):
                stopped = True
                break
    if paths.service_record.exists():
        raise BridgeError(
            "停用后仍存在 owned 服务记录，无法确认 listener 已停止。"
        )

    final_callback_error: Exception | None = None
    if after_stop is not None:
        try:
            after_stop()
        except Exception as exc:
            final_callback_error = exc
    if final_callback_error is not None:
        detail = str(final_callback_error)
        if callback_error is not None:
            detail = f"停用前：{callback_error}；停用后：{detail}"
        raise BridgeError(
            "直连配置已停用且服务已停止，但最终停用快照写入失败："
            f"{detail}"
        ) from final_callback_error
    if callback_error is not None and after_stop is None:
        raise BridgeError(
            "直连配置已停用且服务已停止，但停用快照写入失败："
            f"{callback_error}"
        ) from callback_error
    return disabled, stopped


def enumerate_active_render_endpoints(
    native_host: Path | None = None,
) -> list[RenderEndpoint]:
    # Ask the bundled native host for exact active eRender
    # IMMDevice::GetId values.  Never infer a default endpoint here: both
    # independent virtual cables must be selected explicitly.
    native_host = (
        native_host.resolve()
        if native_host is not None
        else BridgePaths.discover().native_host
    )
    if not native_host.is_file():
        return []
    try:
        flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )
        result = subprocess.run(
            (str(native_host), "--list-direct-render-endpoints"),
            cwd=str(native_host.parent),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
            check=False,
            shell=False,
            creationflags=flags,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return []
    if (
        not isinstance(value, dict)
        or set(value) != {
            "contract",
            "ok",
            "captureStarted",
            "devices",
        }
        or value.get("contract")
            != "reader-computer-voice-render-endpoints/1"
        or value.get("ok") is not True
        or value.get("captureStarted") is not False
        or not isinstance(value.get("devices"), list)
        or len(value["devices"]) > 64
    ):
        return []
    endpoints: list[RenderEndpoint] = []
    seen: set[str] = set()
    for item in value["devices"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"endpointId", "friendlyName"}
            or not isinstance(item.get("endpointId"), str)
            or not isinstance(item.get("friendlyName"), str)
        ):
            return []
        try:
            endpoint_id = _validate_render_endpoint_id(
                item["endpointId"],
                "虚拟音频",
            )
        except BridgeError:
            return []
        friendly_name = item["friendlyName"]
        if (
            len(friendly_name) > 512
            or any(ord(character) < 32 for character in friendly_name)
            or endpoint_id in seen
        ):
            return []
        seen.add(endpoint_id)
        endpoints.append(
            RenderEndpoint(
                endpoint_id=endpoint_id,
                friendly_name=friendly_name or endpoint_id,
            )
        )
    endpoints.sort(key=lambda item: item.display_name.casefold())
    return endpoints


def enumerate_active_capture_endpoints(
    native_host: Path | None = None,
) -> list[CaptureEndpoint]:
    # Capture selection is independent from eRender selection.  Ask the
    # native host for exact active eCapture IMMDevice::GetId values and never
    # derive the recording-side ID from a render-side ID.
    native_host = (
        native_host.resolve()
        if native_host is not None
        else BridgePaths.discover().native_host
    )
    if not native_host.is_file():
        return []
    try:
        flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )
        result = subprocess.run(
            (str(native_host), "--list-direct-microphones"),
            cwd=str(native_host.parent),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
            check=False,
            shell=False,
            creationflags=flags,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return []
    if (
        not isinstance(value, dict)
        or set(value) != {
            "contract",
            "ok",
            "captureStarted",
            "devices",
        }
        or value.get("contract")
            != "reader-computer-voice-microphones/1"
        or value.get("ok") is not True
        or value.get("captureStarted") is not False
        or not isinstance(value.get("devices"), list)
        or len(value["devices"]) > 64
    ):
        return []
    endpoints: list[CaptureEndpoint] = []
    seen: set[str] = set()
    for item in value["devices"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"endpointId", "friendlyName"}
            or not isinstance(item.get("endpointId"), str)
            or not isinstance(item.get("friendlyName"), str)
        ):
            return []
        try:
            endpoint_id = _validate_virtual_microphone_capture_endpoint(
                item["endpointId"]
            )
        except BridgeError:
            return []
        friendly_name = item["friendlyName"]
        if (
            len(friendly_name) > 512
            or any(ord(character) < 32 for character in friendly_name)
            or endpoint_id in seen
        ):
            return []
        seen.add(endpoint_id)
        endpoints.append(
            CaptureEndpoint(
                endpoint_id=endpoint_id,
                friendly_name=friendly_name or endpoint_id,
            )
        )
    endpoints.sort(key=lambda item: item.display_name.casefold())
    return endpoints


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class WindowsShortcutBroker:
    """Single-user, single-request named-pipe broker for one F24 toggle."""

    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_MESSAGE = 0x00000004
    PIPE_READMODE_MESSAGE = 0x00000002
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    ERROR_MORE_DATA = 234
    ERROR_PIPE_BUSY = 231
    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1
    SDDL_REVISION_1 = 1
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(
        self,
        processor: ShortcutBrokerRequestProcessor | None = None,
    ) -> None:
        self._processor = processor or ShortcutBrokerRequestProcessor()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None

    def __enter__(self) -> WindowsShortcutBroker:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if os.name != "nt":
            raise ShortcutBrokerError("快捷键 broker 只允许在 Windows 运行。")
        if self._thread is not None:
            raise ShortcutBrokerError("快捷键 broker 不允许重复启动。")
        self._thread = threading.Thread(
            target=self._serve,
            name="bw-shortcut-broker",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(SHORTCUT_BROKER_READY_TIMEOUT_SECONDS):
            self.close()
            raise ShortcutBrokerError("快捷键 broker 未能及时就绪。")
        if self._startup_error is not None:
            error = self._startup_error
            self.close()
            raise ShortcutBrokerError("快捷键 broker 启动失败。") from error

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._wake_server()
        thread.join(timeout=SHORTCUT_BROKER_READY_TIMEOUT_SECONDS)
        self._thread = None

    @classmethod
    def probe_available(cls) -> bool:
        """Check that the local owned pipe exists without sending F24."""

        if os.name != "nt":
            return False
        try:
            kernel32 = cls._configure_kernel32()
            handle = kernel32.CreateFileW(
                SHORTCUT_BROKER_PIPE_PATH,
                cls.GENERIC_READ | cls.GENERIC_WRITE,
                0,
                None,
                cls.OPEN_EXISTING,
                0,
                None,
            )
            if handle != cls.INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() == cls.ERROR_PIPE_BUSY
        except OSError:
            return False

    @staticmethod
    def _configure_kernel32() -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
        ]
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.ConnectNamedPipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
        ]
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        return kernel32

    @classmethod
    def _current_user_security(
        cls,
        kernel32: Any,
    ) -> tuple[_SECURITY_ATTRIBUTES, wintypes.HLOCAL]:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            wintypes.BOOL
        )

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            cls.TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_string = wintypes.LPWSTR()
        try:
            required = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token,
                cls.TOKEN_USER_CLASS,
                None,
                0,
                ctypes.byref(required),
            )
            if required.value <= 0:
                raise ctypes.WinError(ctypes.get_last_error())
            token_buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token,
                cls.TOKEN_USER_CLASS,
                token_buffer,
                required,
                ctypes.byref(required),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            token_user = ctypes.cast(
                token_buffer,
                ctypes.POINTER(_TOKEN_USER),
            ).contents
            if not advapi32.ConvertSidToStringSidW(
                token_user.User.Sid,
                ctypes.byref(sid_string),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor = wintypes.LPVOID()
            sddl = f"D:P(A;;GA;;;{sid_string.value})"
            if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl,
                cls.SDDL_REVISION_1,
                ctypes.byref(descriptor),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = _SECURITY_ATTRIBUTES(
                ctypes.sizeof(_SECURITY_ATTRIBUTES),
                descriptor,
                False,
            )
            return attributes, wintypes.HLOCAL(descriptor.value)
        finally:
            if sid_string:
                kernel32.LocalFree(sid_string)
            kernel32.CloseHandle(token)

    @classmethod
    def _new_pipe(
        cls,
        kernel32: Any,
        security: _SECURITY_ATTRIBUTES,
    ) -> wintypes.HANDLE:
        handle = kernel32.CreateNamedPipeW(
            SHORTCUT_BROKER_PIPE_PATH,
            cls.PIPE_ACCESS_DUPLEX | cls.FILE_FLAG_FIRST_PIPE_INSTANCE,
            cls.PIPE_TYPE_MESSAGE
            | cls.PIPE_READMODE_MESSAGE
            | cls.PIPE_REJECT_REMOTE_CLIENTS,
            1,
            2048,
            2048,
            0,
            ctypes.byref(security),
        )
        if handle == cls.INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    @classmethod
    def _read_one_message(cls, kernel32: Any, handle: Any) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            buffer = ctypes.create_string_buffer(512)
            read = wintypes.DWORD()
            success = bool(
                kernel32.ReadFile(
                    handle,
                    buffer,
                    len(buffer),
                    ctypes.byref(read),
                    None,
                )
            )
            chunk = bytes(buffer.raw[: read.value])
            chunks.append(chunk)
            total += len(chunk)
            if total > SHORTCUT_BROKER_MAX_REQUEST_BYTES:
                return b""
            if success:
                return b"".join(chunks)
            if ctypes.get_last_error() != cls.ERROR_MORE_DATA:
                raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _write_receipt(kernel32: Any, handle: Any, receipt: bytes) -> None:
        buffer = ctypes.create_string_buffer(receipt)
        written = wintypes.DWORD()
        if (
            not kernel32.WriteFile(
                handle,
                buffer,
                len(receipt),
                ctypes.byref(written),
                None,
            )
            or written.value != len(receipt)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.FlushFileBuffers(handle)

    def _serve(self) -> None:
        kernel32: Any | None = None
        descriptor: wintypes.HLOCAL | None = None
        pipe: Any | None = None
        try:
            kernel32 = self._configure_kernel32()
            security, descriptor = self._current_user_security(kernel32)
            # Keep the sole named-pipe instance for the broker lifetime.
            # Closing and recreating a FILE_FLAG_FIRST_PIPE_INSTANCE pipe for
            # every request races with a client handle that has not finished
            # releasing yet and can terminate the broker with WinError 231.
            pipe = self._new_pipe(kernel32, security)
            self._ready.set()
            while not self._stop.is_set():
                connected = bool(kernel32.ConnectNamedPipe(pipe, None))
                if (
                    not connected
                    and ctypes.get_last_error() != self.ERROR_PIPE_CONNECTED
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if self._stop.is_set():
                    break
                try:
                    payload = self._read_one_message(kernel32, pipe)
                    receipt = self._processor.process(payload)
                    self._write_receipt(kernel32, pipe, receipt)
                except OSError:
                    # One broken current-user client cannot kill the sole
                    # broker.  The next connection gets a fresh pipe instance.
                    pass
                finally:
                    kernel32.DisconnectNamedPipe(pipe)
        except BaseException as error:
            if not self._ready.is_set():
                self._startup_error = error
            self._ready.set()
        finally:
            if pipe not in (None, self.INVALID_HANDLE_VALUE) and kernel32:
                kernel32.CloseHandle(pipe)
            if descriptor and kernel32:
                kernel32.LocalFree(descriptor)
            self._ready.set()

    def _wake_server(self) -> None:
        if os.name != "nt":
            return
        try:
            kernel32 = self._configure_kernel32()
            handle = kernel32.CreateFileW(
                SHORTCUT_BROKER_PIPE_PATH,
                self.GENERIC_READ | self.GENERIC_WRITE,
                0,
                None,
                self.OPEN_EXISTING,
                0,
                None,
            )
            if handle != self.INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(handle)
        except OSError:
            pass


class WindowsProcessRunner:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[Any]] = {}

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
    ) -> int:
        if not command:
            raise BridgeError("直连启动命令为空。")
        executable = _require_named_executable(
            Path(command[0]),
            "bw-computer-voice-audio.exe",
        )
        expected_config = (
            executable.parent / "computer-voice-direct.config.json"
        ).resolve()
        owner_args_valid = (
            len(command) == 4
            or (
                len(command) == 6
                and command[4] == "--readerpc-owner-pid"
                and command[5].isdigit()
                and int(command[5]) > 0
            )
        )
        if (
            not owner_args_valid
            or command[1] != "--direct-serve"
            or command[2] != "--config"
            or not _same_path(command[3], expected_config)
            or not _same_path(executable.parent, cwd)
        ):
            raise BridgeError("直连启动命令或目录偏离固定合同。")
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._children[process.pid] = process
        return process.pid

    def executable_for_pid(self, pid: int) -> Path | None:
        child = self._children.get(pid)
        if child is not None and child.poll() is not None:
            self._children.pop(pid, None)
            return None
        if os.name != "nt" or pid <= 0:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def terminate_exact(self, pid: int, executable: Path) -> bool:
        if os.name != "nt" or pid <= 0:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION | self.PROCESS_TERMINATE,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            # Query and terminate through the same kernel handle.  Reopening by
            # PID after a path check permits PID reuse to redirect termination
            # to an unrelated process.
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return False
            if not _same_path(Path(buffer.value), executable):
                return False
            terminated = bool(kernel32.TerminateProcess(handle, 0))
            if terminated:
                self._children.pop(pid, None)
            return terminated
        finally:
            kernel32.CloseHandle(handle)

    def wait_for_exit(self, pid: int) -> int:
        try:
            process = self._children[pid]
        except KeyError as error:
            raise BridgeError(
                "bootstrap 只能等待自己创建的直连代理。"
            ) from error
        try:
            return int(process.wait())
        finally:
            self._children.pop(pid, None)


class SubprocessReadOnlyRunner:
    def run_read_only(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


def run_idle_bootstrap(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    now_provider: Callable[[], datetime] = utc_now,
    max_cycles: int | None = None,
) -> int:
    if max_cycles is not None and max_cycles <= 0:
        raise BridgeError("bootstrap 测试循环上限无效。")
    restart_count = 0
    stable_polls = 0
    unresponsive_polls = 0
    cycles = 0

    migrate_native_app_origin(paths)

    while True:
        config = load_direct_config(paths)
        if config is None or config.get("localOptIn") is not True:
            return 0

        current = read_direct_status(
            paths,
            runner,
            now=now_provider(),
        )
        record = _load_service_record(paths)
        if paths.service_record.exists() and record is None:
            raise BridgeError(
                "bootstrap 发现无效服务记录；拒绝覆盖未知状态。"
            )

        if current.service_online:
            stable_polls += 1
            unresponsive_polls = 0
            if stable_polls >= SUPERVISOR_STABLE_POLLS:
                restart_count = 0
        else:
            stable_polls = 0
            observed: Path | None = None
            if record is not None:
                observed = runner.executable_for_pid(record["pid"])
                if (
                    observed is not None
                    and not _same_path(observed, paths.native_host)
                ):
                    raise BridgeError(
                        "bootstrap 发现服务记录 PID 属于陌生进程；"
                        "拒绝停止、覆盖或冒充。"
                    )
                if observed is not None:
                    unresponsive_polls += 1
                    if (
                        unresponsive_polls
                        >= SUPERVISOR_UNRESPONSIVE_POLLS
                    ):
                        pid = record["pid"]
                        terminated = runner.terminate_exact(
                            pid,
                            paths.native_host.resolve(),
                        )
                        if not terminated:
                            after = runner.executable_for_pid(pid)
                            if after is not None:
                                raise BridgeError(
                                    "精确服务连续无心跳且无法安全停止；"
                                    "拒绝启动第二实例。"
                                )
                        latest = _load_service_record(paths)
                        if latest is not None and latest["pid"] == pid:
                            try:
                                paths.service_record.unlink()
                            except FileNotFoundError:
                                pass
                        observed = None
                        unresponsive_polls = 0
                        restart_count = max(restart_count, 1)
            else:
                unresponsive_polls = 0

            if observed is None:
                unresponsive_polls = 0
                if restart_count:
                    delay = SUPERVISOR_RESTART_BACKOFF_SECONDS[
                        min(
                            restart_count - 1,
                            len(SUPERVISOR_RESTART_BACKOFF_SECONDS) - 1,
                        )
                    ]
                    sleeper(delay)
                    config = load_direct_config(paths)
                    if (
                        config is None
                        or config.get("localOptIn") is not True
                    ):
                        return 0
                try:
                    start_direct_service(
                        paths,
                        runner,
                        now=now_provider(),
                    )
                except LocalOptOutDuringStart:
                    return 0
                restart_count += 1

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0
        sleeper(SUPERVISOR_POLL_SECONDS)


def build_self_test_report(paths: BridgePaths) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    errors: list[str] = []

    def check(name: str, action: Callable[[], Any]) -> None:
        try:
            result = action()
            if result is False:
                raise BridgeError("check returned false")
        except Exception as error:
            checks[name] = False
            errors.append(f"{name}: {error}")
        else:
            checks[name] = True

    check("native-host-present", paths.native_host.is_file)
    check("desktop-launcher-present", paths.desktop_launcher.is_file)
    check(
        "fixed-native-host-path",
        lambda: paths.native_host.resolve()
        == (
            paths.root
            / "native-host"
            / "bw-computer-voice-audio.exe"
        ).resolve(),
    )
    check(
        "direct-config-contract",
        lambda: build_direct_config(
            "{0.0.0.00000000}.{11111111-1111-1111-1111-111111111111}",
            "{0.0.0.00000000}.{33333333-3333-3333-3333-333333333333}",
            paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                "{0.0.1.00000000}."
                "{22222222-2222-2222-2222-222222222222}"
            ),
        ),
    )
    check(
        "single-user-mode-contract",
        lambda: build_direct_config(
            "{0.0.0.00000000}.{11111111-1111-1111-1111-111111111111}",
            "{0.0.0.00000000}.{33333333-3333-3333-3333-333333333333}",
            paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                "{0.0.1.00000000}."
                "{22222222-2222-2222-2222-222222222222}"
            ),
        )["experimentalSingleUserMode"] is True,
    )
    report = {
        "contract": SELF_TEST_CONTRACT,
        "ok": all(checks.values()),
        "checks": checks,
        "errors": errors,
        "writesToBridgeConfiguration": False,
        "serviceStarted": False,
        "audioOpened": False,
        "applicationStarted": False,
        "typistStarted": False,
        "shortcutSent": False,
        "taskRegistered": False,
        "registryWritten": False,
        "tailscaleServeChanged": False,
        "browserOpened": False,
    }
    return report
