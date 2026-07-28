"""Durable pairing and ephemeral WebRTC signalling for computer voice.

This module is transport-neutral.  It registers no route and never carries
audio.  A future Flask adapter must authenticate the Reader account before
calling Reader-facing methods; Windows-facing methods authenticate with the
device token that Windows generated locally during one-time pairing.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable


PAIRING_CONTRACT = "reader-computer-voice-pairing/1"
SIGNAL_CONTRACT = "reader-computer-voice-signal/1"
PAIRING_TTL_SECONDS = 300
PAIRING_MAX_ATTEMPTS = 5
PAIRING_REPLAY_TTL_SECONDS = 24 * 60 * 60
SIGNAL_TTL_SECONDS = 120
MAX_SIGNALS_PER_EXCHANGE = 32
MAX_SIGNAL_PAYLOAD_BYTES = 32 * 1024
MAX_SIGNAL_SESSIONS = 128
MAX_SIGNAL_SESSIONS_PER_DEVICE = 4
MAX_SIGNAL_MESSAGES_PER_SESSION = 256
MAX_SIGNAL_BYTES_PER_SESSION = 512 * 1024
MAX_PENDING_PAIRINGS_PER_ACCOUNT = 4
MAX_PENDING_PAIRINGS = 256
MAX_DEVICES_PER_ACCOUNT = 16
MAX_ACTIVE_DEVICES_PER_ACCOUNT = 8
MAX_SDP_LINES = 256
MAX_SDP_LINE_CHARS = 512
MAX_ICE_CANDIDATE_CHARS = 1_024
MAX_SDP_MID_CHARS = 64
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_PAIRING_CODE = re.compile(r"^[A-HJ-NP-Z2-9]{8,20}$")
_DEVICE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{32,512}$")
_SIGNAL_KINDS = frozenset({"offer", "answer", "ice", "bye"})
_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SDP_ATTRIBUTE_LIMITS = {
    "candidate": 64,
    "end-of-candidates": 4,
    "extmap": 32,
    "extmap-allow-mixed": 1,
    "fingerprint": 4,
    "fmtp": 32,
    "group": 4,
    "ice-options": 4,
    "ice-pwd": 4,
    "ice-ufrag": 4,
    "maxptime": 4,
    "mid": 4,
    "msid": 8,
    "msid-semantic": 4,
    "ptime": 4,
    "rtcp": 4,
    "rtcp-fb": 64,
    "rtcp-mux": 4,
    "rtcp-mux-only": 4,
    "rtcp-rsize": 4,
    "rtpmap": 32,
    "setup": 4,
    "ssrc": 32,
    "ssrc-group": 8,
}
_SDP_DIRECTION_ATTRIBUTES = frozenset({
    "inactive",
    "recvonly",
    "sendonly",
    "sendrecv",
})


class ComputerVoicePairingError(ValueError):
    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ComputerVoicePairingError(
            f"{label} 无效",
            "BW_COMPUTER_VOICE_PAIRING_INVALID",
        )
    text = value.strip()
    if not _SAFE_ID.fullmatch(text):
        raise ComputerVoicePairingError(
            f"{label} 无效",
            "BW_COMPUTER_VOICE_PAIRING_INVALID",
        )
    return text


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ComputerVoicePairingError(
            "信令包含非 JSON 值",
            "BW_COMPUTER_VOICE_SIGNAL_INVALID",
        ) from exc


def _normalize_code(value: Any) -> str:
    if not isinstance(value, str):
        raise ComputerVoicePairingError(
            "配对码无效",
            "BW_COMPUTER_VOICE_PAIRING_AUTH",
            403,
        )
    text = re.sub(r"[\s-]+", "", value.upper())
    if not _PAIRING_CODE.fullmatch(text):
        raise ComputerVoicePairingError(
            "配对码无效",
            "BW_COMPUTER_VOICE_PAIRING_AUTH",
            403,
        )
    return text


def _normalize_token(value: Any) -> str:
    if not isinstance(value, str):
        raise ComputerVoicePairingError(
            "设备凭据无效",
            "BW_COMPUTER_VOICE_DEVICE_AUTH",
            403,
        )
    text = value.strip()
    if not _DEVICE_TOKEN.fullmatch(text):
        raise ComputerVoicePairingError(
            "设备凭据无效",
            "BW_COMPUTER_VOICE_DEVICE_AUTH",
            403,
        )
    return text


class ComputerVoicePairingStore:
    """SQLite-backed, hash-only device pairing store.

    Windows supplies and retains its own random device token.  The server stores
    only a keyed digest, so a lost HTTP response can be retried idempotently
    without retaining or re-issuing plaintext credentials.
    """

    def __init__(
        self,
        path: Path,
        *,
        pepper: bytes,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
        code_factory: Callable[[], str] | None = None,
        generation_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(pepper, bytes) or len(pepper) < 32:
            raise ValueError("pairing pepper must contain at least 32 bytes")
        self.path = Path(path)
        self._pepper = bytes(pepper)
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._code_factory = code_factory or (
            lambda: "".join(
                secrets.choice(_PAIRING_ALPHABET)
                for _ in range(12)
            )
        )
        self._generation_factory = generation_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        self._lock = threading.RLock()
        self._ensure_schema()

    def begin_pairing(self, account: Any) -> dict[str, Any]:
        account_id = _safe_id(account, "account")
        now = int(self._clock())
        pair_id = "pair-" + _safe_id(self._id_factory(), "pairId token")
        code = _normalize_code(self._code_factory())
        with self._lock, contextlib.closing(self._connection()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE computer_voice_pairings SET state='expired' "
                    "WHERE state='pending' AND expires_at<=?",
                    (now,),
                )
                connection.execute(
                    "DELETE FROM computer_voice_pairings "
                    "WHERE (state IN ('expired','locked') AND expires_at<?) "
                    "OR (state='consumed' AND consumed_at<?)",
                    (
                        now - PAIRING_REPLAY_TTL_SECONDS,
                        now - PAIRING_REPLAY_TTL_SECONDS,
                    ),
                )
                pending_total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM computer_voice_pairings "
                        "WHERE state='pending'"
                    ).fetchone()[0]
                )
                pending_account = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM computer_voice_pairings "
                        "WHERE state='pending' AND account_id=?",
                        (account_id,),
                    ).fetchone()[0]
                )
                if (
                    pending_total >= MAX_PENDING_PAIRINGS
                    or pending_account >= MAX_PENDING_PAIRINGS_PER_ACCOUNT
                ):
                    connection.rollback()
                    raise ComputerVoicePairingError(
                        "当前待确认配对请求过多",
                        "BW_COMPUTER_VOICE_PAIRING_RATE_LIMIT",
                        429,
                    )
                connection.execute(
                    "INSERT INTO computer_voice_pairings("
                    "pair_id,account_id,code_hmac,created_at,expires_at,"
                    "attempts,state,paired_device_id,consumed_at"
                    ") VALUES(?,?,?,?,?,0,'pending',NULL,NULL)",
                    (
                        pair_id,
                        account_id,
                        self._digest("pair-code", f"{pair_id}:{code}"),
                        now,
                        now + PAIRING_TTL_SECONDS,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("pairing id factory produced a duplicate") from exc
        return {
            "contract": PAIRING_CONTRACT,
            "pairId": pair_id,
            "pairingCode": code,
            "expiresAt": now + PAIRING_TTL_SECONDS,
            "state": "pending",
        }

    def consume_pairing(
        self,
        pair_id: Any,
        pairing_code: Any,
        device_id: Any,
        device_token: Any,
    ) -> dict[str, Any]:
        normalized_pair = _safe_id(pair_id, "pairId")
        normalized_code = _normalize_code(pairing_code)
        normalized_device = _safe_id(device_id, "deviceId")
        normalized_token = _normalize_token(device_token)
        now = int(self._clock())
        token_digest = self._digest(
            "device-token",
            f"{normalized_device}:{normalized_token}",
        )

        with self._lock, contextlib.closing(self._connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM computer_voice_pairings WHERE pair_id=?",
                (normalized_pair,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ComputerVoicePairingError(
                    "配对请求不存在或已失效",
                    "BW_COMPUTER_VOICE_PAIRING_UNAVAILABLE",
                    404,
                )
            if row["state"] == "locked":
                connection.rollback()
                raise ComputerVoicePairingError(
                    "配对请求已锁定",
                    "BW_COMPUTER_VOICE_PAIRING_LOCKED",
                    409,
                )
            if now >= int(row["expires_at"]) and row["state"] == "pending":
                connection.execute(
                    "UPDATE computer_voice_pairings SET state='expired' "
                    "WHERE pair_id=?",
                    (normalized_pair,),
                )
                connection.commit()
                raise ComputerVoicePairingError(
                    "配对请求已过期",
                    "BW_COMPUTER_VOICE_PAIRING_EXPIRED",
                    409,
                )

            code_digest = self._digest(
                "pair-code",
                f"{normalized_pair}:{normalized_code}",
            )
            code_matches = hmac.compare_digest(
                row["code_hmac"],
                code_digest,
            )
            account_id = row["account_id"]
            current_device = connection.execute(
                "SELECT account_id,token_hmac,revoked_at "
                "FROM computer_voice_devices WHERE device_id=?",
                (normalized_device,),
            ).fetchone()
            if row["state"] == "consumed":
                same = bool(
                    code_matches
                    and row["paired_device_id"] == normalized_device
                    and current_device is not None
                    and current_device["account_id"] == account_id
                    and current_device["revoked_at"] is None
                    and hmac.compare_digest(
                        current_device["token_hmac"],
                        token_digest,
                    )
                )
                connection.rollback()
                if same:
                    return self._pairing_receipt(
                        normalized_pair,
                        account_id,
                        normalized_device,
                        int(row["consumed_at"]),
                        replayed=True,
                    )
                raise ComputerVoicePairingError(
                    "配对请求已被消费",
                    "BW_COMPUTER_VOICE_PAIRING_REUSED",
                    409,
                )
            if not code_matches:
                attempts = int(row["attempts"]) + 1
                state = (
                    "locked"
                    if attempts >= PAIRING_MAX_ATTEMPTS
                    else row["state"]
                )
                connection.execute(
                    "UPDATE computer_voice_pairings SET attempts=?,state=? "
                    "WHERE pair_id=?",
                    (attempts, state, normalized_pair),
                )
                connection.commit()
                raise ComputerVoicePairingError(
                    "配对码无效",
                    (
                        "BW_COMPUTER_VOICE_PAIRING_LOCKED"
                        if state == "locked"
                        else "BW_COMPUTER_VOICE_PAIRING_AUTH"
                    ),
                    409 if state == "locked" else 403,
                )

            if row["state"] != "pending":
                connection.rollback()
                raise ComputerVoicePairingError(
                    "配对请求不可用",
                    "BW_COMPUTER_VOICE_PAIRING_UNAVAILABLE",
                    409,
                )
            if current_device is not None:
                connection.rollback()
                raise ComputerVoicePairingError(
                    "设备 ID 已被使用，拒绝隐式换绑或换钥",
                    "BW_COMPUTER_VOICE_DEVICE_OWNERSHIP",
                    409,
                )
            device_counts = connection.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS active "
                "FROM computer_voice_devices WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if (
                int(device_counts["total"] or 0) >= MAX_DEVICES_PER_ACCOUNT
                or int(device_counts["active"] or 0)
                >= MAX_ACTIVE_DEVICES_PER_ACCOUNT
            ):
                connection.rollback()
                raise ComputerVoicePairingError(
                    "当前账户已达到电脑客户端设备上限",
                    "BW_COMPUTER_VOICE_DEVICE_CAPACITY",
                    409,
                )
            credential_generation = _safe_id(
                self._generation_factory(),
                "credential generation",
            )

            connection.execute(
                "INSERT INTO computer_voice_devices("
                "device_id,account_id,token_hmac,credential_generation,"
                "paired_at,revoked_at"
                ") VALUES(?,?,?,?,?,NULL)",
                (
                    normalized_device,
                    account_id,
                    token_digest,
                    credential_generation,
                    now,
                ),
            )
            connection.execute(
                "UPDATE computer_voice_pairings "
                "SET state='consumed',paired_device_id=?,consumed_at=? "
                "WHERE pair_id=? AND state='pending'",
                (normalized_device, now, normalized_pair),
            )
            if connection.total_changes < 2:
                connection.rollback()
                raise ComputerVoicePairingError(
                    "配对结果未知，拒绝继续",
                    "BW_COMPUTER_VOICE_PAIRING_UNKNOWN",
                    503,
                )
            connection.commit()
            return self._pairing_receipt(
                normalized_pair,
                account_id,
                normalized_device,
                now,
                replayed=False,
            )

    def authenticate_device(
        self,
        device_id: Any,
        device_token: Any,
    ) -> dict[str, str]:
        normalized_device = _safe_id(device_id, "deviceId")
        normalized_token = _normalize_token(device_token)
        expected = self._digest(
            "device-token",
            f"{normalized_device}:{normalized_token}",
        )
        with contextlib.closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT account_id,token_hmac,credential_generation,"
                "revoked_at "
                "FROM computer_voice_devices WHERE device_id=?",
                (normalized_device,),
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or not hmac.compare_digest(row["token_hmac"], expected)
        ):
            raise ComputerVoicePairingError(
                "设备认证失败",
                "BW_COMPUTER_VOICE_DEVICE_AUTH",
                403,
            )
        return {
            "account": row["account_id"],
            "deviceId": normalized_device,
            "binding": str(row["credential_generation"]),
        }

    def require_account_device(self, account: Any, device_id: Any) -> str:
        account_id = _safe_id(account, "account")
        normalized_device = _safe_id(device_id, "deviceId")
        with contextlib.closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT account_id,credential_generation,revoked_at "
                "FROM computer_voice_devices "
                "WHERE device_id=?",
                (normalized_device,),
            ).fetchone()
        if (
            row is None
            or row["account_id"] != account_id
            or row["revoked_at"] is not None
        ):
            raise ComputerVoicePairingError(
                "设备未配对到当前账户",
                "BW_COMPUTER_VOICE_DEVICE_UNAVAILABLE",
                404,
            )
        return str(row["credential_generation"])

    def list_devices(self, account: Any) -> list[dict[str, Any]]:
        account_id = _safe_id(account, "account")
        with contextlib.closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT device_id,paired_at,revoked_at "
                "FROM computer_voice_devices WHERE account_id=? "
                "ORDER BY paired_at DESC,device_id ASC",
                (account_id,),
            ).fetchall()
        result = []
        for row in rows:
            revoked_at = row["revoked_at"]
            device = {
                "deviceId": str(row["device_id"]),
                "pairedAt": int(row["paired_at"]),
                "state": (
                    "revoked" if revoked_at is not None else "active"
                ),
            }
            if revoked_at is not None:
                device["revokedAt"] = int(revoked_at)
            result.append(device)
        return result

    def revoke_device(self, account: Any, device_id: Any) -> dict[str, Any]:
        account_id = _safe_id(account, "account")
        normalized_device = _safe_id(device_id, "deviceId")
        now = int(self._clock())
        with self._lock, contextlib.closing(self._connection()) as connection:
            cursor = connection.execute(
                "UPDATE computer_voice_devices SET revoked_at=? "
                "WHERE device_id=? AND account_id=? AND revoked_at IS NULL",
                (now, normalized_device, account_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise ComputerVoicePairingError(
                "设备不存在或已撤销",
                "BW_COMPUTER_VOICE_DEVICE_UNAVAILABLE",
                404,
            )
        return {
            "contract": PAIRING_CONTRACT,
            "deviceId": normalized_device,
            "state": "revoked",
            "revokedAt": now,
        }

    def forget_revoked_device(
        self,
        account: Any,
        device_id: Any,
    ) -> dict[str, Any]:
        """Explicitly remove a revoked tombstone to free account capacity.

        Active devices can never be forgotten directly: callers must revoke
        first, making the destructive transition visible and authenticated.
        """
        account_id = _safe_id(account, "account")
        normalized_device = _safe_id(device_id, "deviceId")
        with self._lock, contextlib.closing(self._connection()) as connection:
            cursor = connection.execute(
                "DELETE FROM computer_voice_devices "
                "WHERE device_id=? AND account_id=? AND revoked_at IS NOT NULL",
                (normalized_device, account_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise ComputerVoicePairingError(
                "只能忘记已撤销的设备",
                "BW_COMPUTER_VOICE_DEVICE_UNAVAILABLE",
                409,
            )
        return {
            "contract": PAIRING_CONTRACT,
            "deviceId": normalized_device,
            "state": "forgotten",
        }

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connection()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS computer_voice_pairings (
                  pair_id TEXT PRIMARY KEY,
                  account_id TEXT NOT NULL,
                  code_hmac TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  attempts INTEGER NOT NULL,
                  state TEXT NOT NULL,
                  paired_device_id TEXT,
                  consumed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS computer_voice_devices (
                  device_id TEXT PRIMARY KEY,
                  account_id TEXT NOT NULL,
                  token_hmac TEXT NOT NULL,
                  credential_generation TEXT NOT NULL,
                  paired_at INTEGER NOT NULL,
                  revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS computer_voice_devices_account
                  ON computer_voice_devices(account_id, revoked_at);
                CREATE INDEX IF NOT EXISTS computer_voice_pairings_state_expiry
                  ON computer_voice_pairings(state, expires_at);
                CREATE INDEX IF NOT EXISTS computer_voice_pairings_account_state
                  ON computer_voice_pairings(account_id, state);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(computer_voice_devices)"
                    ).fetchall()
                }
                if "credential_generation" not in columns:
                    connection.execute(
                        "ALTER TABLE computer_voice_devices "
                        "ADD COLUMN credential_generation TEXT"
                    )
                rows = connection.execute(
                    "SELECT device_id FROM computer_voice_devices "
                    "WHERE credential_generation IS NULL "
                    "OR credential_generation=''"
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE computer_voice_devices "
                        "SET credential_generation=? WHERE device_id=? "
                        "AND (credential_generation IS NULL "
                        "OR credential_generation='')",
                        (
                            _safe_id(
                                self._generation_factory(),
                                "credential generation",
                            ),
                            row["device_id"],
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _digest(self, label: str, value: str) -> str:
        return hmac.new(
            self._pepper,
            f"{label}\0{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _pairing_receipt(
        pair_id: str,
        account: str,
        device_id: str,
        paired_at: int,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        return {
            "contract": PAIRING_CONTRACT,
            "pairId": pair_id,
            "account": account,
            "deviceId": device_id,
            "state": "paired",
            "pairedAt": paired_at,
            "replayed": replayed,
        }


@dataclass
class _SignalMessage:
    cursor: int
    signal_id: str
    sender: str
    kind: str
    payload: dict[str, Any]
    digest: str
    byte_size: int


@dataclass
class _SignalSession:
    account: str
    device_id: str
    credential_generation: str
    session_id: str
    created_at: float
    expires_at: float
    next_cursor: int = 0
    byte_size: int = 0
    messages: list[_SignalMessage] = field(default_factory=list)
    dedupe: dict[tuple[str, str], str] = field(default_factory=dict)


class ComputerVoiceSignalBroker:
    """Short-lived, in-memory audio-WebRTC metadata signalling.

    The broker accepts a bounded, whitelisted SDP/ICE subset and never accepts
    a PCM/audio field.  SDP remains protocol metadata rather than media bytes;
    DTLS-SRTP audio flows directly between the browser and Windows.
    """

    def __init__(
        self,
        pairing: ComputerVoicePairingStore,
        *,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._pairing = pairing
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._sessions: dict[str, _SignalSession] = {}
        self._lock = threading.RLock()

    def open_session(self, account: Any, device_id: Any) -> dict[str, Any]:
        account_id = _safe_id(account, "account")
        normalized_device = _safe_id(device_id, "deviceId")
        credential_generation = self._pairing.require_account_device(
            account_id,
            normalized_device,
        )
        now = self._clock()
        session_id = "voice-" + _safe_id(self._id_factory(), "session token")
        with self._lock:
            self._expire_locked(now)
            if session_id in self._sessions:
                raise RuntimeError("signal id factory produced a duplicate")
            if len(self._sessions) >= MAX_SIGNAL_SESSIONS:
                raise ComputerVoicePairingError(
                    "当前语音信令会话过多",
                    "BW_COMPUTER_VOICE_SIGNAL_CAPACITY",
                    429,
                )
            device_sessions = sum(
                1
                for session in self._sessions.values()
                if session.account == account_id
                and session.device_id == normalized_device
            )
            if device_sessions >= MAX_SIGNAL_SESSIONS_PER_DEVICE:
                raise ComputerVoicePairingError(
                    "该设备已有过多语音信令会话",
                    "BW_COMPUTER_VOICE_SIGNAL_CAPACITY",
                    429,
                )
            self._sessions[session_id] = _SignalSession(
                account=account_id,
                device_id=normalized_device,
                credential_generation=credential_generation,
                session_id=session_id,
                created_at=now,
                expires_at=now + SIGNAL_TTL_SECONDS,
            )
        return {
            "contract": SIGNAL_CONTRACT,
            "sessionId": session_id,
            "deviceId": normalized_device,
            "expiresAt": int(now + SIGNAL_TTL_SECONDS),
            "state": "waiting",
        }

    def exchange_reader(
        self,
        account: Any,
        session_id: Any,
        *,
        signals: Any,
        cursor: Any,
    ) -> dict[str, Any]:
        account_id = _safe_id(account, "account")
        return self._exchange(
            "reader",
            session_id,
            signals=signals,
            cursor=cursor,
            expected_account=account_id,
        )

    def exchange_device(
        self,
        device_id: Any,
        device_token: Any,
        session_id: Any,
        *,
        signals: Any,
        cursor: Any,
    ) -> dict[str, Any]:
        identity = self._pairing.authenticate_device(device_id, device_token)
        return self._exchange(
            "device",
            session_id,
            signals=signals,
            cursor=cursor,
            expected_account=identity["account"],
            expected_device=identity["deviceId"],
            expected_generation=identity["binding"],
        )

    def _exchange(
        self,
        actor: str,
        session_id: Any,
        *,
        signals: Any,
        cursor: Any,
        expected_account: str,
        expected_device: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        normalized_session = _safe_id(session_id, "sessionId")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ComputerVoicePairingError(
                "signal cursor 无效",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        normalized = self._normalize_signals(signals)
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            session = self._sessions.get(normalized_session)
            invalid_identity = (
                session is None
                or session.account != expected_account
                or (
                    expected_device is not None
                    and session.device_id != expected_device
                )
                or (
                    expected_generation is not None
                    and session.credential_generation
                    != expected_generation
                )
            )
            if invalid_identity:
                if (
                    session is not None
                    and expected_generation is not None
                    and session.account == expected_account
                    and session.device_id == expected_device
                    and session.credential_generation
                    != expected_generation
                ):
                    self._sessions.pop(normalized_session, None)
                raise ComputerVoicePairingError(
                    "信令会话不存在或不属于当前身份",
                    "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
                    404,
                )
            try:
                current_generation = (
                    self._pairing.require_account_device(
                        session.account,
                        session.device_id,
                    )
                )
            except ComputerVoicePairingError:
                self._sessions.pop(normalized_session, None)
                raise ComputerVoicePairingError(
                    "信令会话绑定的设备已失效",
                    "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
                    404,
                )
            if current_generation != session.credential_generation:
                self._sessions.pop(normalized_session, None)
                raise ComputerVoicePairingError(
                    "信令会话绑定的设备代际已失效",
                    "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
                    404,
                )
            if cursor > session.next_cursor:
                raise ComputerVoicePairingError(
                    "signal cursor 超过会话 head",
                    "BW_COMPUTER_VOICE_SIGNAL_CURSOR",
                    409,
                )
            # Two-phase validation: no mutation is allowed until the whole
            # request has passed idempotency and capacity checks.  Otherwise a
            # late signalId conflict could make the request return 409 after an
            # earlier signal had already become visible to the peer.
            acknowledged: list[str] = []
            pending: list[
                tuple[dict[str, Any], tuple[str, str], str, int]
            ] = []
            for signal in normalized:
                dedupe_key = (actor, signal["signalId"])
                encoded = _canonical_json(signal).encode("utf-8")
                digest = hashlib.sha256(encoded).hexdigest()
                previous = session.dedupe.get(dedupe_key)
                if previous is not None:
                    if not hmac.compare_digest(previous, digest):
                        raise ComputerVoicePairingError(
                            "signalId 已用于不同内容",
                            "BW_COMPUTER_VOICE_SIGNAL_ID_REUSE",
                            409,
                        )
                    acknowledged.append(signal["signalId"])
                    continue
                pending.append((signal, dedupe_key, digest, len(encoded)))
                acknowledged.append(signal["signalId"])
            if (
                len(session.messages) + len(pending)
                > MAX_SIGNAL_MESSAGES_PER_SESSION
                or session.byte_size + sum(item[3] for item in pending)
                > MAX_SIGNAL_BYTES_PER_SESSION
            ):
                raise ComputerVoicePairingError(
                    "信令会话容量已满",
                    "BW_COMPUTER_VOICE_SIGNAL_CAPACITY",
                    413,
                )
            for signal, dedupe_key, digest, byte_size in pending:
                session.next_cursor += 1
                payload = dict(signal["payload"])
                session.messages.append(
                    _SignalMessage(
                        cursor=session.next_cursor,
                        signal_id=signal["signalId"],
                        sender=actor,
                        kind=signal["kind"],
                        payload=payload,
                        digest=digest,
                        byte_size=byte_size,
                    )
                )
                session.dedupe[dedupe_key] = digest
                session.byte_size += byte_size
            incoming = [
                {
                    "cursor": message.cursor,
                    "signalId": message.signal_id,
                    "kind": message.kind,
                    # Never expose the mutable object held by the broker.
                    "payload": dict(message.payload),
                }
                for message in session.messages
                if message.cursor > cursor and message.sender != actor
            ]
            return {
                "contract": SIGNAL_CONTRACT,
                "sessionId": session.session_id,
                "deviceId": session.device_id,
                "ackedSignalIds": acknowledged,
                "signals": incoming,
                "cursor": session.next_cursor,
                "expiresAt": int(session.expires_at),
            }

    def _expire_locked(self, now: float) -> None:
        for session_id in [
            session_id
            for session_id, session in self._sessions.items()
            if now >= session.expires_at
        ]:
            self._sessions.pop(session_id, None)

    @staticmethod
    def _normalize_signals(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ComputerVoicePairingError(
                "signals 必须是数组",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        if len(value) > MAX_SIGNALS_PER_EXCHANGE:
            raise ComputerVoicePairingError(
                "signals 超过单批上限",
                "BW_COMPUTER_VOICE_SIGNAL_TOO_LARGE",
                413,
            )
        result = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, dict) or set(raw) != {
                "signalId",
                "kind",
                "payload",
            }:
                raise ComputerVoicePairingError(
                    "signal 字段不匹配",
                    "BW_COMPUTER_VOICE_SIGNAL_INVALID",
                )
            signal_id = _safe_id(raw.get("signalId"), "signalId")
            if signal_id in seen:
                raise ComputerVoicePairingError(
                    "同一批含重复 signalId",
                    "BW_COMPUTER_VOICE_SIGNAL_ID_REUSE",
                    409,
                )
            seen.add(signal_id)
            kind = str(raw.get("kind") or "")
            if kind not in _SIGNAL_KINDS:
                raise ComputerVoicePairingError(
                    "signal kind 无效",
                    "BW_COMPUTER_VOICE_SIGNAL_INVALID",
                )
            payload = ComputerVoiceSignalBroker._normalize_payload(
                kind,
                raw.get("payload"),
            )
            normalized = {
                "signalId": signal_id,
                "kind": kind,
                "payload": payload,
            }
            if len(_canonical_json(normalized).encode("utf-8")) > (
                MAX_SIGNAL_PAYLOAD_BYTES
            ):
                raise ComputerVoicePairingError(
                    "单条信令过大",
                    "BW_COMPUTER_VOICE_SIGNAL_TOO_LARGE",
                    413,
                )
            result.append(normalized)
        return result

    @staticmethod
    def _normalize_payload(kind: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ComputerVoicePairingError(
                "signal payload 必须是对象",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        if kind in {"offer", "answer"}:
            if set(value) != {"type", "sdp"} or value.get("type") != kind:
                raise ComputerVoicePairingError(
                    "SDP 信令字段不匹配",
                    "BW_COMPUTER_VOICE_SIGNAL_INVALID",
                )
            sdp = value.get("sdp")
            if not isinstance(sdp, str):
                raise ComputerVoicePairingError(
                    "SDP 无效",
                    "BW_COMPUTER_VOICE_SIGNAL_INVALID",
                )
            ComputerVoiceSignalBroker._validate_audio_sdp(sdp)
            return {"type": kind, "sdp": sdp}
        if kind == "ice":
            if set(value) != {
                "candidate",
                "sdpMid",
                "sdpMLineIndex",
            }:
                raise ComputerVoicePairingError(
                    "ICE 信令字段不匹配",
                    "BW_COMPUTER_VOICE_SIGNAL_INVALID",
                )
            candidate = value.get("candidate")
            sdp_mid = value.get("sdpMid")
            line = value.get("sdpMLineIndex")
            if (
                not isinstance(candidate, str)
                or len(candidate) > MAX_ICE_CANDIDATE_CHARS
                or not ComputerVoiceSignalBroker._valid_ice_candidate(
                    candidate
                )
                or (sdp_mid is not None and not isinstance(sdp_mid, str))
                or (
                    isinstance(sdp_mid, str)
                    and (
                        len(sdp_mid) > MAX_SDP_MID_CHARS
                        or not re.fullmatch(r"[A-Za-z0-9._:-]*", sdp_mid)
                    )
                )
                or (
                    line is not None
                    and (
                        isinstance(line, bool)
                        or not isinstance(line, int)
                        or not 0 <= line <= 65_535
                    )
                )
            ):
                raise ComputerVoicePairingError(
                    "ICE candidate 无效",
                    "BW_COMPUTER_VOICE_SIGNAL_INVALID",
                )
            return {
                "candidate": candidate,
                "sdpMid": sdp_mid,
                "sdpMLineIndex": line,
            }
        if set(value) != {"reason"}:
            raise ComputerVoicePairingError(
                "bye 信令字段不匹配",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        reason = value.get("reason")
        if not isinstance(reason, str) or len(reason) > 160:
            raise ComputerVoicePairingError(
                "bye reason 无效",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        return {"reason": reason}

    @staticmethod
    def _validate_audio_sdp(sdp: str) -> None:
        if (
            not sdp
            or len(sdp.encode("utf-8")) > MAX_SIGNAL_PAYLOAD_BYTES
            or "\x00" in sdp
        ):
            raise ComputerVoicePairingError(
                "SDP 无效",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        lines = sdp.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if (
            not lines
            or lines[0] != "v=0"
            or len(lines) > MAX_SDP_LINES
            or any(
                not line
                or len(line) > MAX_SDP_LINE_CHARS
                or any(ord(char) < 0x20 and char != "\t" for char in line)
                for line in lines
            )
        ):
            raise ComputerVoicePairingError(
                "SDP 无效",
                "BW_COMPUTER_VOICE_SIGNAL_INVALID",
            )
        media = [line[2:] for line in lines if line.startswith("m=")]
        if not media or any(
            not ComputerVoiceSignalBroker._valid_audio_media_line(entry)
            for entry in media
        ):
            raise ComputerVoicePairingError(
                "只允许音频 WebRTC SDP",
                "BW_COMPUTER_VOICE_SIGNAL_MEDIA_SCOPE",
            )
        if len(media) > 4:
            raise ComputerVoicePairingError(
                "音频媒体段过多",
                "BW_COMPUTER_VOICE_SIGNAL_MEDIA_SCOPE",
            )
        attribute_counts: dict[str, int] = {}
        fixed_counts = {"v": 0, "o": 0, "s": 0, "t": 0, "c": 0, "b": 0}
        for line in lines:
            prefix = line[:2]
            if prefix in {"v=", "o=", "s=", "t=", "c=", "b=", "m="}:
                key = prefix[0]
                if key in fixed_counts:
                    fixed_counts[key] += 1
                if prefix == "m=":
                    continue
                if prefix == "v=" and line != "v=0":
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                elif prefix == "o=" and not re.fullmatch(
                    r"o=- [0-9]{1,20} [0-9]{1,20} "
                    r"IN IP(?:4|6) [A-Za-z0-9:.%-]{1,64}",
                    line,
                ):
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                elif prefix == "s=" and line != "s=-":
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                elif prefix == "t=" and line != "t=0 0":
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                elif prefix == "c=" and not re.fullmatch(
                    r"c=IN IP(?:4|6) [A-Za-z0-9:.%-]{1,255}",
                    line,
                ):
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                elif prefix == "b=" and not re.fullmatch(
                    r"b=(?:AS|TIAS):[0-9]{1,10}",
                    line,
                ):
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                continue
            if prefix != "a=":
                ComputerVoiceSignalBroker._invalid_sdp_line()
            attribute = line[2:].split(":", 1)[0]
            if attribute in _SDP_DIRECTION_ATTRIBUTES:
                if line != f"a={attribute}":
                    ComputerVoiceSignalBroker._invalid_sdp_line()
                limit = 4
            else:
                limit = _SDP_ATTRIBUTE_LIMITS.get(attribute, 0)
            if not limit:
                ComputerVoiceSignalBroker._invalid_sdp_line()
            attribute_counts[attribute] = (
                attribute_counts.get(attribute, 0) + 1
            )
            if attribute_counts[attribute] > limit:
                ComputerVoiceSignalBroker._invalid_sdp_line()
            if not ComputerVoiceSignalBroker._valid_sdp_attribute(
                attribute,
                line[2:],
            ):
                ComputerVoiceSignalBroker._invalid_sdp_line()
        if (
            fixed_counts["v"] != 1
            or fixed_counts["o"] != 1
            or fixed_counts["s"] != 1
            or fixed_counts["t"] != 1
            or fixed_counts["c"] > 4
            or fixed_counts["b"] > 4
        ):
            ComputerVoiceSignalBroker._invalid_sdp_line()

    @staticmethod
    def _valid_audio_media_line(value: str) -> bool:
        tokens = value.split()
        allowed_protocols = {
            "UDP/TLS/RTP/SAVPF",
            "UDP/TLS/RTP/SAVP",
            "RTP/SAVPF",
            "RTP/SAVP",
        }
        return bool(
            4 <= len(tokens) <= 36
            and tokens[0] == "audio"
            and re.fullmatch(r"[0-9]{1,5}", tokens[1])
            and tokens[2].upper() in allowed_protocols
            and all(
                re.fullmatch(r"[0-9]{1,3}", payload)
                and 0 <= int(payload) <= 127
                for payload in tokens[3:]
            )
        )

    @staticmethod
    def _valid_sdp_attribute(attribute: str, value: str) -> bool:
        if attribute in _SDP_DIRECTION_ATTRIBUTES:
            return value == attribute
        if attribute in {
            "end-of-candidates",
            "extmap-allow-mixed",
            "rtcp-mux",
            "rtcp-mux-only",
            "rtcp-rsize",
        }:
            return value == attribute
        raw = value[len(attribute) + 1:] if value.startswith(
            attribute + ":"
        ) else None
        if raw is None:
            return False
        if attribute == "candidate":
            return ComputerVoiceSignalBroker._valid_ice_candidate(value)
        if attribute == "group":
            return bool(re.fullmatch(
                r"BUNDLE(?: [A-Za-z0-9._-]{1,64}){1,4}",
                raw,
            ))
        if attribute == "msid-semantic":
            return bool(re.fullmatch(
                r" ?WMS(?: [A-Za-z0-9*._-]{1,128}){0,8}",
                raw,
            ))
        if attribute == "ice-options":
            return bool(re.fullmatch(
                r"(?:trickle|renomination)(?: (?:trickle|renomination)){0,1}",
                raw,
            ))
        if attribute == "ice-ufrag":
            return bool(re.fullmatch(r"[A-Za-z0-9+/]{4,64}", raw))
        if attribute == "ice-pwd":
            return bool(re.fullmatch(r"[A-Za-z0-9+/]{16,128}", raw))
        if attribute == "fingerprint":
            return bool(re.fullmatch(
                r"sha-256(?: [0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){31})",
                raw,
            ))
        if attribute == "setup":
            return raw in {"actpass", "active", "passive"}
        if attribute == "mid":
            return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", raw))
        if attribute in {"ptime", "maxptime"}:
            return bool(re.fullmatch(r"[0-9]{1,5}", raw))
        if attribute == "rtcp":
            return bool(re.fullmatch(
                r"[0-9]{1,5}(?: IN IP(?:4|6) [A-Za-z0-9:.%-]{1,64})?",
                raw,
            ))
        if attribute == "rtpmap":
            return bool(re.fullmatch(
                r"(?:[0-9]{1,3}) [A-Za-z0-9._+-]{1,32}/"
                r"[0-9]{1,6}(?:/[1-9][0-9]{0,2})?",
                raw,
            ))
        if attribute == "fmtp":
            return bool(re.fullmatch(
                r"[0-9]{1,3} [A-Za-z0-9._+/%=;,:~-]{1,256}",
                raw,
            ))
        if attribute == "rtcp-fb":
            return bool(re.fullmatch(
                r"(?:[0-9]{1,3}|\*) "
                r"[A-Za-z0-9._+/%=;,:~-]{1,128}(?: "
                r"[A-Za-z0-9._+/%=;,:~-]{1,64}){0,3}",
                raw,
            ))
        if attribute == "extmap":
            return bool(re.fullmatch(
                r"[1-9][0-9]{0,2}(?:/(?:sendonly|recvonly|sendrecv|inactive))? "
                r"\S{1,256}(?: \S{1,64})?",
                raw,
            ))
        if attribute == "msid":
            return bool(re.fullmatch(
                r"[A-Za-z0-9._*-]{1,128}(?: [A-Za-z0-9._*-]{1,128})?",
                raw,
            ))
        if attribute == "ssrc":
            return bool(re.fullmatch(
                r"[0-9]{1,10} (?:cname|msid|mslabel|label):"
                r"[A-Za-z0-9._+/%~-]{1,128}(?: [A-Za-z0-9._+/%~-]{1,128})?",
                raw,
            ))
        if attribute == "ssrc-group":
            return bool(re.fullmatch(
                r"(?:FID|FEC|FEC-FR|SIM)(?: [0-9]{1,10}){2,8}",
                raw,
            ))
        return False

    @staticmethod
    def _valid_ice_candidate(candidate: str) -> bool:
        if candidate == "":
            return True
        if (
            "\r" in candidate
            or "\n" in candidate
            or any(ord(character) < 0x20 for character in candidate)
        ):
            return False
        # RFC 5245/8445 candidate grammar is token based.  Keep the transport
        # opaque but bounded, and require the mandatory foundation/component/
        # transport/priority/address/port/type fields.
        tokens = candidate.split()
        if (
            len(tokens) < 8
            or len(tokens) > 32
            or not re.fullmatch(r"candidate:[A-Za-z0-9+/]{1,64}", tokens[0])
            or not re.fullmatch(r"[1-9][0-9]{0,2}", tokens[1])
            or tokens[2].casefold() not in {"udp", "tcp"}
            or not re.fullmatch(r"[0-9]{1,10}", tokens[3])
            or not re.fullmatch(r"[A-Za-z0-9:.%-]{1,255}", tokens[4])
            or not re.fullmatch(r"[0-9]{1,5}", tokens[5])
            or tokens[6] != "typ"
            or tokens[7] not in {"host", "srflx", "prflx", "relay"}
        ):
            return False
        return all(
            re.fullmatch(r"[A-Za-z0-9:._+/%~-]{1,255}", token)
            for token in tokens[8:]
        )

    @staticmethod
    def _invalid_sdp_line() -> None:
        raise ComputerVoicePairingError(
            "SDP 含未允许或超量的属性",
            "BW_COMPUTER_VOICE_SIGNAL_MEDIA_SCOPE",
        )


__all__ = [
    "ComputerVoicePairingError",
    "ComputerVoicePairingStore",
    "ComputerVoiceSignalBroker",
    "MAX_SIGNAL_PAYLOAD_BYTES",
    "MAX_SIGNAL_SESSIONS",
    "MAX_SIGNAL_SESSIONS_PER_DEVICE",
    "MAX_SIGNAL_MESSAGES_PER_SESSION",
    "MAX_SIGNAL_BYTES_PER_SESSION",
    "MAX_PENDING_PAIRINGS_PER_ACCOUNT",
    "MAX_PENDING_PAIRINGS",
    "MAX_DEVICES_PER_ACCOUNT",
    "MAX_ACTIVE_DEVICES_PER_ACCOUNT",
    "PAIRING_REPLAY_TTL_SECONDS",
    "PAIRING_CONTRACT",
    "PAIRING_MAX_ATTEMPTS",
    "PAIRING_TTL_SECONDS",
    "SIGNAL_CONTRACT",
    "SIGNAL_TTL_SECONDS",
]
