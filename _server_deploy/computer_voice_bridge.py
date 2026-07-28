"""Fail-closed state machine for the optional Windows computer-voice bridge.

This module deliberately contains no HTTP/WebSocket listener and no audio path.
The future Windows bridge dials *out* to an authenticated Pi endpoint, reports
its local readiness, and obtains a one-shot ``start-computer-voice`` command
covering process-scoped audio, the existing voice-typist companion, and the
configured local voice shortcut.  A route adapter may call this state machine
only after it has authenticated both the Reader account and the paired device.

The server never receives microphone/application audio.  WebRTC signalling and
media ownership will be added in a later, separately authenticated layer.  In
particular, ``system-wide`` output capture is rejected here so it cannot become
an accidental compatibility fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import secrets
import threading
import time
from typing import Any, Callable


CONTRACT = "reader-computer-voice-bridge/1"
READY_LEASE_SECONDS = 15
START_COMMAND_TTL_SECONDS = 10
START_ACTION = "start-computer-voice"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_APP_KINDS = frozenset({"chatgpt-desktop", "codex-desktop"})
_COMPANION_KINDS = frozenset({"voice-typist"})
_START_RESULTS = frozenset({"started", "not-ready", "rejected", "failed"})


class ComputerVoiceBridgeError(ValueError):
    """Stable, transport-neutral error for a future route adapter."""

    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


def _require_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise ComputerVoiceBridgeError(
            f"{label} 无效",
            "BW_COMPUTER_VOICE_INVALID",
        )
    return text


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ComputerVoiceBridgeError(
            f"{label} 必须是布尔值",
            "BW_COMPUTER_VOICE_INVALID",
        )
    return value


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComputerVoiceBridgeError(
            f"{label} 必须是对象",
            "BW_COMPUTER_VOICE_INVALID",
        )
    return value


@dataclass
class _Command:
    command_id: str
    nonce: str
    created_at: float
    expires_at: float
    state: str = "pending"
    result: str | None = None
    acknowledged_at: float | None = None


@dataclass
class _Device:
    account: str
    device_id: str
    provisioned_at: float
    last_seen_at: float | None = None
    app_kind: str | None = None
    app_ready: bool = False
    local_opt_in: bool = False
    shortcut_configured: bool = False
    microphone_available: bool = False
    output_scope: str | None = None
    output_target: str | None = None
    capture_active: bool = False
    native_host_ready: bool = False
    media_host_ready: bool = False
    rtc_connected: bool = False
    companion_kind: str | None = None
    companion_launcher_available: bool = False
    companion_running: bool = False
    bridge_version: str | None = None
    command: _Command | None = None


class ComputerVoiceBridgeRegistry:
    """In-memory command state with explicit fail-closed readiness checks.

    Pairing/persistence/authentication intentionally live outside this class.
    ``provision_device`` is a privileged pairing-layer operation, not a browser
    request.  The future route adapter must authenticate the paired Windows
    bridge before invoking bridge-facing methods.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._devices: dict[str, _Device] = {}
        self._lock = threading.RLock()

    def provision_device(self, account: Any, device_id: Any) -> None:
        """Register a device after an external one-time pairing proof succeeds."""
        account_id = _require_id(account, "account")
        normalized_device_id = _require_id(device_id, "deviceId")
        with self._lock:
            current = self._devices.get(normalized_device_id)
            if current and current.account != account_id:
                raise ComputerVoiceBridgeError(
                    "设备已绑定到其他账户",
                    "BW_COMPUTER_VOICE_DEVICE_OWNERSHIP",
                    409,
                )
            if not current:
                self._devices[normalized_device_id] = _Device(
                    account=account_id,
                    device_id=normalized_device_id,
                    provisioned_at=self._clock(),
                )

    def unprovision_device(self, account: Any, device_id: Any) -> None:
        """Drop volatile readiness and pending commands after a durable revoke."""
        account_id = _require_id(account, "account")
        normalized_device_id = _require_id(device_id, "deviceId")
        with self._lock:
            current = self._devices.get(normalized_device_id)
            if current is None:
                return
            if current.account != account_id:
                raise ComputerVoiceBridgeError(
                    "设备不属于当前账户",
                    "BW_COMPUTER_VOICE_DEVICE_OWNERSHIP",
                    409,
                )
            self._devices.pop(normalized_device_id, None)

    def report_heartbeat(
        self,
        account: Any,
        device_id: Any,
        report: Any,
    ) -> dict[str, Any]:
        """Accept only a process-scoped capture readiness report.

        A heartbeat is a capability report, never proof that audio may be sent
        to the server.  ``outputScope`` is intentionally an exact enum: using a
        default-device/system loopback must be rejected rather than silently
        making the device available.
        """
        with self._lock:
            device = self._owned_device(account, device_id)
            value = _require_dict(report, "heartbeat")
            if set(value) != {
                "app",
                "voiceStart",
                "capture",
                "media",
                "companion",
                "bridgeVersion",
            }:
                raise ComputerVoiceBridgeError(
                    "heartbeat 字段不匹配",
                    "BW_COMPUTER_VOICE_INVALID",
                )
            app = _require_dict(value.get("app"), "app")
            start = _require_dict(value.get("voiceStart"), "voiceStart")
            capture = _require_dict(value.get("capture"), "capture")
            media = _require_dict(value.get("media"), "media")
            companion = _require_dict(value.get("companion"), "companion")
            if set(app) != {"kind", "ready"} or set(start) != {
                "localOptIn",
                "shortcutConfigured",
            } or set(capture) != {
                "microphoneAvailable",
                "outputScope",
                "outputTarget",
                "active",
            } or set(media) != {
                "nativeHostReady",
                "mediaHostReady",
                "rtcConnected",
            } or set(companion) != {
                "kind",
                "launcherAvailable",
                "running",
            }:
                raise ComputerVoiceBridgeError(
                    "heartbeat 子字段不匹配",
                    "BW_COMPUTER_VOICE_INVALID",
                )
            app_kind = app.get("kind")
            if app_kind not in _APP_KINDS:
                raise ComputerVoiceBridgeError(
                    "app.kind 不受支持",
                    "BW_COMPUTER_VOICE_INVALID",
                )
            output_scope = capture.get("outputScope")
            if output_scope != "process-only":
                raise ComputerVoiceBridgeError(
                    "只允许配置目标应用的进程级输出采集",
                    "BW_COMPUTER_VOICE_PROCESS_OUTPUT_REQUIRED",
                    409,
                )
            if capture.get("outputTarget") != app_kind:
                raise ComputerVoiceBridgeError(
                    "输出采集目标必须与就绪应用一致",
                    "BW_COMPUTER_VOICE_PROCESS_OUTPUT_REQUIRED",
                    409,
                )
            companion_kind = companion.get("kind")
            if companion_kind not in _COMPANION_KINDS:
                raise ComputerVoiceBridgeError(
                    "companion.kind 不受支持",
                    "BW_COMPUTER_VOICE_INVALID",
                )
            bridge_version = value.get("bridgeVersion")
            if (
                not isinstance(bridge_version, str)
                or not bridge_version.strip()
                or len(bridge_version) > 64
            ):
                raise ComputerVoiceBridgeError(
                    "bridgeVersion 无效",
                    "BW_COMPUTER_VOICE_INVALID",
                )

            device.last_seen_at = self._clock()
            device.app_kind = app_kind
            device.app_ready = _require_bool(app.get("ready"), "app.ready")
            device.local_opt_in = _require_bool(
                start.get("localOptIn"),
                "voiceStart.localOptIn",
            )
            device.shortcut_configured = _require_bool(
                start.get("shortcutConfigured"),
                "voiceStart.shortcutConfigured",
            )
            device.microphone_available = _require_bool(
                capture.get("microphoneAvailable"),
                "capture.microphoneAvailable",
            )
            device.output_scope = output_scope
            device.output_target = app_kind
            device.capture_active = _require_bool(
                capture.get("active"),
                "capture.active",
            )
            device.native_host_ready = _require_bool(
                media.get("nativeHostReady"),
                "media.nativeHostReady",
            )
            device.media_host_ready = _require_bool(
                media.get("mediaHostReady"),
                "media.mediaHostReady",
            )
            device.rtc_connected = _require_bool(
                media.get("rtcConnected"),
                "media.rtcConnected",
            )
            device.companion_kind = companion_kind
            device.companion_launcher_available = _require_bool(
                companion.get("launcherAvailable"),
                "companion.launcherAvailable",
            )
            device.companion_running = _require_bool(
                companion.get("running"),
                "companion.running",
            )
            device.bridge_version = bridge_version.strip()
            self._expire_command(device)
            return self._browser_status(device)

    def browser_status(self, account: Any, device_id: Any) -> dict[str, Any]:
        """Return a deliberately non-sensitive status projection for Reader UI."""
        with self._lock:
            device = self._owned_device(account, device_id)
            self._expire_command(device)
            return self._browser_status(device)

    def request_voice_start(self, account: Any, device_id: Any) -> dict[str, Any]:
        """Create one short-lived command after the Reader telephone button click.

        This method must not be called just because a selector changes.  The
        caller is expected to require an explicit telephone-button user gesture.
        The browser projection never receives the nonce or the configured
        Windows shortcut.
        """
        with self._lock:
            device = self._owned_device(account, device_id)
            self._expire_command(device)
            state, _ = self._availability(device)
            if state != "ready":
                raise ComputerVoiceBridgeError(
                    "电脑客户端未就绪，未发送任何快捷键请求",
                    "BW_COMPUTER_VOICE_NOT_READY",
                    409,
                )
            command = device.command
            if command and command.state in {"pending", "delivered"}:
                return self._browser_command(command, result="already-pending")
            now = self._clock()
            command = _Command(
                command_id="start-" + self._new_token("commandId"),
                nonce=self._new_token("nonce"),
                created_at=now,
                expires_at=now + START_COMMAND_TTL_SECONDS,
            )
            device.command = command
            return self._browser_command(command, result="queued")

    def claim_voice_start(self, account: Any, device_id: Any) -> dict[str, Any] | None:
        """Return the one command to the *authenticated outbound* bridge.

        The same unacknowledged command may be re-delivered after a transient
        network failure.  The Windows bridge must persist command IDs locally
        and execute a particular ID at most once before acknowledging it.
        """
        with self._lock:
            device = self._owned_device(account, device_id)
            self._expire_command(device)
            command = device.command
            state, _ = self._availability(device)
            if (
                not command
                or command.state not in {"pending", "delivered"}
                or state != "ready"
            ):
                return None
            command.state = "delivered"
            return {
                "contract": CONTRACT,
                "commandId": command.command_id,
                "nonce": command.nonce,
                "action": START_ACTION,
                "expiresAt": int(command.expires_at),
            }

    def acknowledge_voice_start(
        self,
        account: Any,
        device_id: Any,
        command_id: Any,
        nonce: Any,
        result: Any,
    ) -> dict[str, Any]:
        """Accept an idempotent bridge acknowledgement without retaining nonce."""
        with self._lock:
            device = self._owned_device(account, device_id)
            self._expire_command(device)
            command = device.command
            normalized_id = _require_id(command_id, "commandId")
            if not command or command.command_id != normalized_id:
                raise ComputerVoiceBridgeError(
                    "启动命令不存在",
                    "BW_COMPUTER_VOICE_COMMAND_UNKNOWN",
                    404,
                )
            if not isinstance(nonce, str) or not secrets.compare_digest(
                hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                hashlib.sha256(command.nonce.encode("utf-8")).hexdigest(),
            ):
                raise ComputerVoiceBridgeError(
                    "启动命令 nonce 不匹配",
                    "BW_COMPUTER_VOICE_COMMAND_AUTH",
                    403,
                )
            if result not in _START_RESULTS:
                raise ComputerVoiceBridgeError(
                    "启动结果无效",
                    "BW_COMPUTER_VOICE_INVALID",
                )
            if command.state == "expired":
                raise ComputerVoiceBridgeError(
                    "启动命令已过期",
                    "BW_COMPUTER_VOICE_COMMAND_EXPIRED",
                    409,
                )
            if command.state == "acknowledged":
                if command.result == result:
                    return self._browser_command(command, result="idempotent-replay")
                raise ComputerVoiceBridgeError(
                    "启动命令不能以不同结果重复确认",
                    "BW_COMPUTER_VOICE_COMMAND_REUSE",
                    409,
                )
            # The bridge must re-check its own local state immediately before
            # invoking the shortcut.  Pi also rejects a stale positive ack when
            # its latest heartbeat says the bridge is no longer ready.
            if result == "started" and self._availability(device)[0] != "ready":
                raise ComputerVoiceBridgeError(
                    "电脑客户端已不就绪，拒绝确认已启动",
                    "BW_COMPUTER_VOICE_NOT_READY",
                    409,
                )
            if result == "started" and not self._started_components_ready(device):
                raise ComputerVoiceBridgeError(
                    "电脑音频桥或 voice-typist 尚未确认启动",
                    "BW_COMPUTER_VOICE_START_INCOMPLETE",
                    409,
                )
            command.state = "acknowledged"
            command.result = result
            command.acknowledged_at = self._clock()
            return self._browser_command(command, result="acknowledged")

    def _owned_device(self, account: Any, device_id: Any) -> _Device:
        account_id = _require_id(account, "account")
        normalized_device_id = _require_id(device_id, "deviceId")
        device = self._devices.get(normalized_device_id)
        if not device or device.account != account_id:
            raise ComputerVoiceBridgeError(
                "电脑客户端未配对或不属于当前账户",
                "BW_COMPUTER_VOICE_DEVICE_UNAVAILABLE",
                404,
            )
        return device

    def _availability(self, device: _Device) -> tuple[str, str]:
        now = self._clock()
        if device.last_seen_at is None or now - device.last_seen_at > READY_LEASE_SECONDS:
            return "offline", "bridge-offline"
        if not device.app_ready:
            return "online-not-ready", "app-not-ready"
        if not device.local_opt_in:
            return "online-not-ready", "local-opt-in-required"
        if not device.shortcut_configured:
            return "online-not-ready", "shortcut-not-configured"
        if not device.microphone_available:
            return "online-not-ready", "microphone-unavailable"
        if (
            device.output_scope != "process-only"
            or device.output_target != device.app_kind
        ):
            return "online-not-ready", "process-output-unavailable"
        if not device.native_host_ready:
            return "online-not-ready", "native-host-unavailable"
        if not device.media_host_ready:
            return "online-not-ready", "media-host-unavailable"
        if not device.companion_launcher_available:
            return "online-not-ready", "voice-typist-launcher-unavailable"
        return "ready", ""

    @staticmethod
    def _started_components_ready(device: _Device) -> bool:
        return bool(
            device.capture_active
            and device.rtc_connected
            and device.companion_kind == "voice-typist"
            and device.companion_running
        )

    def _expire_command(self, device: _Device) -> None:
        command = device.command
        if (
            command
            and command.state in {"pending", "delivered"}
            and self._clock() >= command.expires_at
        ):
            command.state = "expired"

    def _browser_status(self, device: _Device) -> dict[str, Any]:
        state, reason = self._availability(device)
        command = device.command
        return {
            "contract": CONTRACT,
            "deviceId": device.device_id,
            "state": state,
            "reason": reason or None,
            "appKind": device.app_kind,
            "lastSeenAt": int(device.last_seen_at) if device.last_seen_at else None,
            "bridgeVersion": device.bridge_version,
            "captureActive": device.capture_active,
            "media": {
                "hostReady": bool(
                    device.native_host_ready and device.media_host_ready
                ),
                "rtcConnected": device.rtc_connected,
            },
            "companion": {
                "kind": device.companion_kind,
                "launcherAvailable": device.companion_launcher_available,
                "running": device.companion_running,
            },
            "start": self._browser_command(command) if command else None,
        }

    @staticmethod
    def _browser_command(command: _Command, *, result: str | None = None) -> dict[str, Any]:
        return {
            "commandId": command.command_id,
            "state": command.state,
            "result": result or command.result,
            "expiresAt": int(command.expires_at),
        }

    def _new_token(self, label: str) -> str:
        value = self._token_factory()
        if not isinstance(value, str) or not value or len(value) < 16:
            raise RuntimeError(f"{label} token factory returned an unsafe value")
        return value
