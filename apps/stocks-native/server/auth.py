"""Device pairing confined to the new gateway's state directory.

Create an administrator pairing code, without starting a service:
    python auth.py --state-dir /var/lib/stocks-native-mvp create-code

Pairing codes and device bearer tokens are only stored as SHA-256 digests.
The web adapter must accept tokens in headers, never in URLs, and rate-limit
the unauthenticated pairing route. Public methods are synchronous.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_TTL_SECONDS = 600
TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60


class AuthError(ValueError):
    """Invalid or expired credentials; do not reveal which part failed."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class AuthStore:
    def __init__(self, state_dir: str | os.PathLike[str] | None = None, *,
                 clock: Callable[[], float] = time.time, token_ttl_seconds: int = TOKEN_TTL_SECONDS):
        self.root = Path(state_dir or os.environ.get("STOCKS_MVP_STATE_DIR", Path(__file__).parent / "runtime")).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.root / "auth.sqlite3"
        self._clock = clock
        self._token_ttl = int(token_ttl_seconds)
        if self._token_ttl <= 0:
            raise ValueError("token_ttl_seconds must be positive")
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL
                );
                CREATE TABLE IF NOT EXISTS device_tokens (
                    token_hash TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_device_tokens_device
                    ON device_tokens(device_id);
            """)
        if os.name != "nt":
            self.db_path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _device_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
            raise AuthError("Invalid device identifier")
        return value

    def create_pairing_code(self, ttl_seconds: int = PAIRING_TTL_SECONDS) -> dict[str, object]:
        ttl = int(ttl_seconds)
        if not 1 <= ttl <= PAIRING_TTL_SECONDS:
            raise ValueError("Pairing code lifetime must be between 1 and 600 seconds")
        now = self._clock()
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with self._connect() as connection:
            connection.execute("DELETE FROM pairing_codes WHERE expires_at <= ? OR used_at IS NOT NULL", (now,))
            connection.execute(
                "INSERT INTO pairing_codes(code_hash,created_at,expires_at) VALUES(?,?,?)",
                (_digest(raw), now, now + ttl),
            )
        return {"code": raw[:4] + "-" + raw[4:], "expiresAt": _iso(now + ttl), "validForSeconds": ttl}

    def pair(self, code: str, device_id: str, name: str) -> dict[str, str]:
        device_id = self._device_id(device_id)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise AuthError("Device name must contain between 1 and 80 characters")
        if not isinstance(code, str) or len(code) > 32:
            raise AuthError("Invalid or expired pairing code")
        normalized = re.sub(r"[\s-]", "", code).upper()
        if len(normalized) != 8 or any(character not in CODE_ALPHABET for character in normalized):
            raise AuthError("Invalid or expired pairing code")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            # The lock covers validation and redemption, including across workers.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT expires_at,used_at FROM pairing_codes WHERE code_hash=?", (_digest(normalized),)
            ).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] <= now:
                raise AuthError("Invalid or expired pairing code")
            connection.execute("UPDATE pairing_codes SET used_at=? WHERE code_hash=?", (now, _digest(normalized)))
            connection.execute(
                "INSERT INTO device_tokens(token_hash,device_id,name,created_at,expires_at) VALUES(?,?,?,?,?)",
                (_digest(token), device_id, name.strip(), now, now + self._token_ttl),
            )
        return {"token": token, "deviceId": device_id}

    def authenticate(self, token: str, device_id: str | None = None) -> dict[str, object]:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AuthError("Invalid or expired device token")
        if device_id is not None:
            device_id = self._device_id(device_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT device_id,name,created_at,expires_at,revoked_at FROM device_tokens WHERE token_hash=?",
                (_digest(token),),
            ).fetchone()
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= self._clock() \
                or (device_id is not None and not secrets.compare_digest(device_id, row["device_id"])):
            raise AuthError("Invalid or expired device token")
        return {"deviceId": row["device_id"], "name": row["name"],
                "createdAt": _iso(row["created_at"]), "expiresAt": _iso(row["expires_at"])}

    def revoke_device(self, device_id: str) -> int:
        device_id = self._device_id(device_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE device_tokens SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                (self._clock(), device_id),
            )
            return cursor.rowcount


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage pairing for the native stocks gateway")
    parser.add_argument("--state-dir", default=os.environ.get("STOCKS_MVP_STATE_DIR"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-code", help="Print a new single-use code valid for ten minutes")
    revoke = subparsers.add_parser("revoke-device", help="Revoke every token for a device")
    revoke.add_argument("device_id")
    arguments = parser.parse_args()
    store = AuthStore(arguments.state_dir)
    if arguments.command == "create-code":
        print(json.dumps(store.create_pairing_code(), ensure_ascii=False))
    else:
        print(json.dumps({"revoked": store.revoke_device(arguments.device_id)}))


if __name__ == "__main__":
    main()
