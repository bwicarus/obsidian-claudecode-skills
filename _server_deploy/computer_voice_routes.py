"""Authenticated HTTP adapter for computer voice pairing and WebRTC signalling.

The adapter deliberately carries signalling metadata only.  It never accepts
PCM/audio blobs and does not expose a Windows inbound control endpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from computer_voice_bridge import (
    CONTRACT as BRIDGE_CONTRACT,
    ComputerVoiceBridgeError,
    ComputerVoiceBridgeRegistry,
)
from computer_voice_pairing import (
    ComputerVoicePairingError,
    ComputerVoicePairingStore,
    ComputerVoiceSignalBroker,
    PAIRING_CONTRACT,
    SIGNAL_CONTRACT,
)
from reader_sidecar_store import NAMESPACE_RE


bp = Blueprint(
    "computer_voice",
    __name__,
    url_prefix="/api/reader/computer-voice",
)

MAX_REQUEST_BYTES = 80 * 1024
DEVICE_AUTH_SCHEME = "BWComputerVoice"
_DEVICE_AUTH = re.compile(r"^BWComputerVoice ([A-Za-z0-9._~-]{32,512})$")


def _json_response(value: dict[str, Any], status: int = 200):
    response = jsonify(value)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization, Cookie"
    return response


def _error(
    error: ComputerVoicePairingError | ComputerVoiceBridgeError,
    contract: str,
):
    return _json_response(
        {
            "ok": False,
            "contract": contract,
            "code": error.code,
            "error": str(error),
        },
        error.status,
    )


def _request_error_code(contract: str, suffix: str) -> str:
    if contract == SIGNAL_CONTRACT:
        return f"BW_COMPUTER_VOICE_SIGNAL_{suffix}"
    if contract == BRIDGE_CONTRACT:
        return f"BW_COMPUTER_VOICE_{suffix}"
    return f"BW_COMPUTER_VOICE_PAIRING_{suffix}"


def _services() -> tuple[
    ComputerVoicePairingStore,
    ComputerVoiceSignalBroker,
    ComputerVoiceBridgeRegistry,
]:
    pairing = current_app.extensions.get("computer_voice_pairing")
    broker = current_app.extensions.get("computer_voice_signal_broker")
    bridge = current_app.extensions.get("computer_voice_bridge_registry")
    if not isinstance(pairing, ComputerVoicePairingStore) or not isinstance(
        broker,
        ComputerVoiceSignalBroker,
    ) or not isinstance(bridge, ComputerVoiceBridgeRegistry):
        raise ComputerVoicePairingError(
            "电脑客户端桥接服务未就绪",
            "BW_COMPUTER_VOICE_PAIRING_UNAVAILABLE",
            503,
        )
    return pairing, broker, bridge


def _reader_account() -> str:
    resolver = current_app.extensions.get("reader_storage_identity_resolver")
    identity = resolver() if callable(resolver) else None
    namespace = (
        str(identity.get("storage_namespace") or "")
        if isinstance(identity, dict)
        else ""
    )
    if not NAMESPACE_RE.fullmatch(namespace):
        raise ComputerVoicePairingError(
            "需要登录或有效 Bearer token",
            "BW_COMPUTER_VOICE_PAIRING_AUTH",
            401,
        )
    return namespace


def _request_json(contract: str, fields: set[str]) -> dict[str, Any]:
    content_length = request.content_length
    if content_length is not None and content_length > MAX_REQUEST_BYTES:
        raise ComputerVoicePairingError(
            "请求过大",
            _request_error_code(contract, "TOO_LARGE"),
            413,
        )
    if not request.is_json:
        raise ComputerVoicePairingError(
            "请求必须使用 JSON",
            _request_error_code(contract, "INVALID"),
        )
    raw = request.stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ComputerVoicePairingError(
            "请求过大",
            _request_error_code(contract, "TOO_LARGE"),
            413,
        )
    try:
        text = raw.decode("utf-8")

        def reject_duplicate_keys(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = item
            return result

        value = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ComputerVoicePairingError(
            "请求 JSON 无效",
            _request_error_code(contract, "INVALID"),
        )
    expected = set(fields) | {"contract"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ComputerVoicePairingError(
            "请求字段不匹配",
            _request_error_code(contract, "INVALID"),
        )
    if value.get("contract") != contract:
        raise ComputerVoicePairingError(
            "请求合同不匹配",
            _request_error_code(contract, "INVALID"),
        )
    return value


def _device_credentials() -> tuple[str, str]:
    device_id = str(
        request.headers.get("X-BW-Computer-Voice-Device-Id") or ""
    ).strip()
    authorization = str(request.headers.get("Authorization") or "")
    matched = _DEVICE_AUTH.fullmatch(authorization)
    if not matched:
        raise ComputerVoicePairingError(
            "设备认证失败",
            "BW_COMPUTER_VOICE_DEVICE_AUTH",
            403,
        )
    return device_id, matched.group(1)


class _CommandSessionBindings:
    """Short-lived command→WebRTC session association, never media storage."""

    def __init__(self, *, clock=time.time, limit: int = 128) -> None:
        self._clock = clock
        self._limit = max(16, int(limit))
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.start_lock = threading.RLock()

    def bind(
        self,
        command_id: str,
        session_id: str,
        expires_at: int,
    ) -> None:
        with self._lock:
            self._expire_locked()
            self._rows[command_id] = {
                "sessionId": session_id,
                "expiresAt": int(expires_at),
            }
            if len(self._rows) > self._limit:
                ordered = sorted(
                    self._rows.items(),
                    key=lambda item: int(item[1]["expiresAt"]),
                )
                for key, _ in ordered[: len(self._rows) - self._limit]:
                    self._rows.pop(key, None)

    def get(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire_locked()
            row = self._rows.get(command_id)
            return dict(row) if row else None

    def pop(self, command_id: str) -> None:
        with self._lock:
            self._rows.pop(command_id, None)

    def _expire_locked(self) -> None:
        now = int(self._clock())
        for key in [
            command_id
            for command_id, row in self._rows.items()
            if now >= int(row["expiresAt"])
        ]:
            self._rows.pop(key, None)


def _bindings() -> _CommandSessionBindings:
    value = current_app.extensions.get("computer_voice_command_sessions")
    if not isinstance(value, _CommandSessionBindings):
        raise ComputerVoicePairingError(
            "电脑客户端命令服务未就绪",
            "BW_COMPUTER_VOICE_PAIRING_UNAVAILABLE",
            503,
        )
    return value


def _authenticated_bridge_device() -> tuple[
    str,
    str,
    ComputerVoiceBridgeRegistry,
]:
    device_id, token = _device_credentials()
    pairing, _, bridge = _services()
    identity = pairing.authenticate_device(device_id, token)
    account = identity["account"]
    # Readiness is intentionally volatile.  A valid durable device credential
    # may re-provision it after a process restart, then reports offline until a
    # fresh heartbeat arrives.
    bridge.provision_device(account, device_id)
    return account, device_id, bridge


@bp.before_request
def require_secure_transport():
    if (
        current_app.config.get("COMPUTER_VOICE_REQUIRE_TLS", True)
        and not request.is_secure
    ):
        contract = (
            SIGNAL_CONTRACT
            if "/sessions" in request.path
            else PAIRING_CONTRACT
        )
        return _error(
            ComputerVoicePairingError(
                "电脑客户端桥接只允许 HTTPS",
                "BW_COMPUTER_VOICE_TRANSPORT_SECURITY",
                426,
            ),
            contract,
        )
    return None


@bp.post("/pairings")
def begin_pairing():
    try:
        _request_json(PAIRING_CONTRACT, set())
        pairing, _, _ = _services()
        result = pairing.begin_pairing(_reader_account())
        return _json_response({"ok": True, **result}, 201)
    except ComputerVoicePairingError as error:
        return _error(error, PAIRING_CONTRACT)


@bp.post("/pairings/consume")
def consume_pairing():
    try:
        body = _request_json(
            PAIRING_CONTRACT,
            {"pairId", "pairingCode", "deviceId", "deviceToken"},
        )
        pairing, _, bridge = _services()
        result = pairing.consume_pairing(
            body["pairId"],
            body["pairingCode"],
            body["deviceId"],
            body["deviceToken"],
        )
        bridge.provision_device(result["account"], result["deviceId"])
        return _json_response({"ok": True, **result})
    except ComputerVoicePairingError as error:
        return _error(error, PAIRING_CONTRACT)


@bp.get("/devices")
def list_devices():
    try:
        pairing, _, _ = _services()
        devices = pairing.list_devices(_reader_account())
        return _json_response({
            "ok": True,
            "contract": PAIRING_CONTRACT,
            "devices": devices,
        })
    except ComputerVoicePairingError as error:
        return _error(error, PAIRING_CONTRACT)


@bp.delete("/devices/<device_id>")
def revoke_device(device_id: str):
    try:
        _request_json(PAIRING_CONTRACT, set())
        pairing, _, bridge = _services()
        account = _reader_account()
        result = pairing.revoke_device(account, device_id)
        bridge.unprovision_device(account, device_id)
        return _json_response({"ok": True, **result})
    except ComputerVoicePairingError as error:
        return _error(error, PAIRING_CONTRACT)


@bp.delete("/devices/<device_id>/record")
def forget_revoked_device(device_id: str):
    try:
        _request_json(PAIRING_CONTRACT, set())
        pairing, _, bridge = _services()
        account = _reader_account()
        result = pairing.forget_revoked_device(
            account,
            device_id,
        )
        bridge.unprovision_device(account, device_id)
        return _json_response({"ok": True, **result})
    except ComputerVoicePairingError as error:
        return _error(error, PAIRING_CONTRACT)


@bp.get("/devices/<device_id>/status")
def browser_device_status(device_id: str):
    try:
        account = _reader_account()
        pairing, _, bridge = _services()
        pairing.require_account_device(account, device_id)
        bridge.provision_device(account, device_id)
        return _json_response({
            "ok": True,
            **bridge.browser_status(account, device_id),
        })
    except (ComputerVoicePairingError, ComputerVoiceBridgeError) as error:
        return _error(error, BRIDGE_CONTRACT)


@bp.post("/devices/<device_id>/start")
def browser_start_device(device_id: str):
    """Queue one explicit telephone-button start and bind one RTC session."""
    try:
        _request_json(BRIDGE_CONTRACT, set())
        account = _reader_account()
        pairing, broker, bridge = _services()
        pairing.require_account_device(account, device_id)
        bridge.provision_device(account, device_id)
        bindings = _bindings()
        with bindings.start_lock:
            current = bridge.browser_status(account, device_id)
            existing = current.get("start")
            if (
                isinstance(existing, dict)
                and existing.get("state") in {"pending", "delivered"}
            ):
                bound = bindings.get(str(existing.get("commandId") or ""))
                if bound is None:
                    raise ComputerVoiceBridgeError(
                        "启动命令缺少媒体会话，拒绝猜测恢复",
                        "BW_COMPUTER_VOICE_COMMAND_UNKNOWN",
                        409,
                    )
                return _json_response({
                    "ok": True,
                    "contract": BRIDGE_CONTRACT,
                    "command": existing,
                    "session": {
                        "contract": SIGNAL_CONTRACT,
                        "sessionId": bound["sessionId"],
                        "deviceId": device_id,
                        "expiresAt": bound["expiresAt"],
                        "state": "waiting",
                    },
                    "device": current,
                })

            # request_voice_start is the readiness gate.  Calling it first
            # prevents creating an RTC session when the PC/app/opt-in is not
            # positively ready.
            command = bridge.request_voice_start(account, device_id)
            try:
                session = broker.open_session(account, device_id)
            except Exception:
                # The command will expire after ten seconds and cannot execute
                # because no command→session binding is created.
                raise
            expires_at = min(
                int(command["expiresAt"]),
                int(session["expiresAt"]),
            )
            bindings.bind(
                command["commandId"],
                session["sessionId"],
                expires_at,
            )
            return _json_response({
                "ok": True,
                "contract": BRIDGE_CONTRACT,
                "command": command,
                "session": session,
                "device": bridge.browser_status(account, device_id),
            }, 202)
    except (ComputerVoicePairingError, ComputerVoiceBridgeError) as error:
        return _error(error, BRIDGE_CONTRACT)


@bp.post("/device/heartbeat")
def device_heartbeat():
    try:
        body = _request_json(BRIDGE_CONTRACT, {"heartbeat"})
        account, device_id, bridge = _authenticated_bridge_device()
        status = bridge.report_heartbeat(
            account,
            device_id,
            body["heartbeat"],
        )
        return _json_response({"ok": True, **status})
    except (ComputerVoicePairingError, ComputerVoiceBridgeError) as error:
        return _error(error, BRIDGE_CONTRACT)


@bp.post("/device/commands/claim")
def device_claim_command():
    try:
        _request_json(BRIDGE_CONTRACT, set())
        account, device_id, bridge = _authenticated_bridge_device()
        command = bridge.claim_voice_start(account, device_id)
        if command is None:
            return _json_response({
                "ok": True,
                "contract": BRIDGE_CONTRACT,
                "command": None,
                "sessionId": None,
            })
        bound = _bindings().get(command["commandId"])
        if bound is None:
            raise ComputerVoiceBridgeError(
                "启动命令媒体会话已失效",
                "BW_COMPUTER_VOICE_COMMAND_EXPIRED",
                409,
            )
        return _json_response({
            "ok": True,
            "contract": BRIDGE_CONTRACT,
            "command": command,
            "sessionId": bound["sessionId"],
        })
    except (ComputerVoicePairingError, ComputerVoiceBridgeError) as error:
        return _error(error, BRIDGE_CONTRACT)


@bp.post("/device/commands/<command_id>/ack")
def device_ack_command(command_id: str):
    try:
        body = _request_json(BRIDGE_CONTRACT, {"nonce", "result"})
        account, device_id, bridge = _authenticated_bridge_device()
        result = bridge.acknowledge_voice_start(
            account,
            device_id,
            command_id,
            body["nonce"],
            body["result"],
        )
        _bindings().pop(command_id)
        return _json_response({"ok": True, **result})
    except (ComputerVoicePairingError, ComputerVoiceBridgeError) as error:
        return _error(error, BRIDGE_CONTRACT)


@bp.post("/sessions")
def open_session():
    try:
        body = _request_json(SIGNAL_CONTRACT, {"deviceId"})
        _, broker, _ = _services()
        result = broker.open_session(
            _reader_account(),
            body["deviceId"],
        )
        return _json_response({"ok": True, **result}, 201)
    except ComputerVoicePairingError as error:
        return _error(error, SIGNAL_CONTRACT)


@bp.post("/sessions/<session_id>/signals")
def exchange_reader_signals(session_id: str):
    try:
        body = _request_json(SIGNAL_CONTRACT, {"signals", "cursor"})
        _, broker, _ = _services()
        result = broker.exchange_reader(
            _reader_account(),
            session_id,
            signals=body["signals"],
            cursor=body["cursor"],
        )
        return _json_response({"ok": True, **result})
    except ComputerVoicePairingError as error:
        return _error(error, SIGNAL_CONTRACT)


@bp.post("/device/sessions/<session_id>/signals")
def exchange_device_signals(session_id: str):
    try:
        body = _request_json(SIGNAL_CONTRACT, {"signals", "cursor"})
        device_id, token = _device_credentials()
        _, broker, _ = _services()
        result = broker.exchange_device(
            device_id,
            token,
            session_id,
            signals=body["signals"],
            cursor=body["cursor"],
        )
        return _json_response({"ok": True, **result})
    except ComputerVoicePairingError as error:
        return _error(error, SIGNAL_CONTRACT)


def register_computer_voice(
    app,
    *,
    root: str | Path | None = None,
    pepper: bytes | None = None,
) -> None:
    state_root = Path(
        root
        or app.extensions.get("computer_voice_root")
        or os.environ.get("WEBAPP_DATA", "/root/webapp/data")
    ).resolve()
    secret = app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if pepper is None:
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise RuntimeError("computer voice pairing secret unavailable")
        pepper = hmac.new(
            secret,
            b"bw-computer-voice-pairing:v1",
            hashlib.sha256,
        ).digest()
    pairing = ComputerVoicePairingStore(
        state_root / "computer-voice.sqlite3",
        pepper=pepper,
    )
    app.extensions["computer_voice_root"] = state_root
    app.extensions["computer_voice_pairing"] = pairing
    app.extensions["computer_voice_signal_broker"] = (
        ComputerVoiceSignalBroker(pairing)
    )
    app.extensions["computer_voice_bridge_registry"] = (
        ComputerVoiceBridgeRegistry()
    )
    app.extensions["computer_voice_command_sessions"] = (
        _CommandSessionBindings()
    )
    app.config.setdefault("COMPUTER_VOICE_REQUIRE_TLS", True)
    app.register_blueprint(bp)


__all__ = [
    "DEVICE_AUTH_SCHEME",
    "MAX_REQUEST_BYTES",
    "bp",
    "register_computer_voice",
]
