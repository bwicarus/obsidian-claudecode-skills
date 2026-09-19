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
REVIEW_PAIRING_TTL_SECONDS = 7 * 24 * 60 * 60
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
                    used_at REAL,
                    access TEXT NOT NULL DEFAULT 'full'
                );
                CREATE TABLE IF NOT EXISTS device_tokens (
                    token_hash TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    access TEXT NOT NULL DEFAULT 'full'
                );
                CREATE TABLE IF NOT EXISTS apple_accounts (
                    subject_hash TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    last_login_at REAL NOT NULL,
                    access TEXT NOT NULL DEFAULT 'full'
                );
                CREATE INDEX IF NOT EXISTS idx_device_tokens_device
                    ON device_tokens(device_id);
            """)
            self._ensure_column(connection, "pairing_codes", "access", "TEXT NOT NULL DEFAULT 'full'")
            self._ensure_column(connection, "device_tokens", "access", "TEXT NOT NULL DEFAULT 'full'")
            self._ensure_column(connection, "device_tokens", "owner_id", "TEXT")
            # Earlier Apple logins wrote both records with the same timestamp but
            # omitted their relation. Recover only a unique, exact recorded match;
            # never assign all devices to the first/only account.
            connection.execute("""
                UPDATE device_tokens SET owner_id = (
                    SELECT 'apple:' || MIN(subject_hash) FROM apple_accounts
                    WHERE device_tokens.created_at IN (created_at, last_login_at)
                ) WHERE owner_id IS NULL AND (
                    SELECT COUNT(*) FROM apple_accounts
                    WHERE device_tokens.created_at IN (created_at, last_login_at)
                ) = 1
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
    def _ensure_column(connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _device_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
            raise AuthError("Invalid device identifier")
        return value

    def create_pairing_code(self, ttl_seconds: int = PAIRING_TTL_SECONDS) -> dict[str, object]:
        ttl = int(ttl_seconds)
        if not 1 <= ttl <= PAIRING_TTL_SECONDS:
            raise ValueError("Pairing code lifetime must be between 1 and 600 seconds")
        return self._create_pairing_code(ttl, "full")

    def create_review_pairing_code(self) -> dict[str, object]:
        """Create an administrator-only, single-use code with AI disabled."""
        result = self._create_pairing_code(REVIEW_PAIRING_TTL_SECONDS, "review")
        result["purpose"] = "app-review"
        result["aiEnabled"] = False
        return result

    def _create_pairing_code(self, ttl: int, access: str) -> dict[str, object]:
        now = self._clock()
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with self._connect() as connection:
            connection.execute("DELETE FROM pairing_codes WHERE expires_at <= ? OR used_at IS NOT NULL", (now,))
            connection.execute(
                "INSERT INTO pairing_codes(code_hash,created_at,expires_at,access) VALUES(?,?,?,?)",
                (_digest(raw), now, now + ttl, access),
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
                "SELECT expires_at,used_at,access FROM pairing_codes WHERE code_hash=?", (_digest(normalized),)
            ).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] <= now:
                raise AuthError("Invalid or expired pairing code")
            connection.execute("UPDATE pairing_codes SET used_at=? WHERE code_hash=?", (now, _digest(normalized)))
            connection.execute(
                "INSERT INTO device_tokens(token_hash,device_id,name,created_at,expires_at,access,owner_id) VALUES(?,?,?,?,?,?,?)",
                (_digest(token), device_id, name.strip(), now, now + self._token_ttl, row["access"],
                 "device:" + _digest(device_id)),
            )
        return {"token": token, "deviceId": device_id, "aiEnabled": row["access"] == "full"}

    def apple_login(self, subject: str, device_id: str, name: str, previous_token: str | None = None) -> dict[str, object]:
        """Issue an app token after a caller has cryptographically verified Apple identity."""
        device_id = self._device_id(device_id)
        if not isinstance(subject, str) or not 6 <= len(subject) <= 255:
            raise AuthError("Invalid Apple subject")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise AuthError("Device name must contain between 1 and 80 characters")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        subject_hash = _digest("apple:" + subject)
        previous_owner = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if previous_token:
                previous = connection.execute(
                    "SELECT device_id,owner_id,expires_at,revoked_at FROM device_tokens WHERE token_hash=?",
                    (_digest(previous_token),),
                ).fetchone()
                if previous and previous["device_id"] == device_id and previous["revoked_at"] is None \
                        and previous["expires_at"] > now:
                    candidate = previous["owner_id"] or "device:" + _digest(device_id)
                    if candidate.startswith("device:"):
                        previous_owner = candidate
            account = connection.execute(
                "SELECT access FROM apple_accounts WHERE subject_hash=?", (subject_hash,)
            ).fetchone()
            access = account["access"] if account else "full"
            connection.execute(
                "INSERT INTO apple_accounts(subject_hash,created_at,last_login_at,access) VALUES(?,?,?,?) "
                "ON CONFLICT(subject_hash) DO UPDATE SET last_login_at=excluded.last_login_at",
                (subject_hash, now, now, access),
            )
            connection.execute(
                "INSERT INTO device_tokens(token_hash,device_id,name,created_at,expires_at,access,owner_id) VALUES(?,?,?,?,?,?,?)",
                (_digest(token), device_id, name.strip(), now, now + self._token_ttl, access, "apple:" + subject_hash),
            )
        result = {"token": token, "deviceId": device_id, "aiEnabled": access == "full",
                  "ownerId": "apple:" + subject_hash}
        if previous_owner:
            result["previousOwnerId"] = previous_owner
        return result

    def authenticate(self, token: str, device_id: str | None = None) -> dict[str, object]:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AuthError("Invalid or expired device token")
        if device_id is not None:
            device_id = self._device_id(device_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT device_id,name,created_at,expires_at,revoked_at,access,owner_id FROM device_tokens WHERE token_hash=?",
                (_digest(token),),
            ).fetchone()
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= self._clock() \
                or (device_id is not None and not secrets.compare_digest(device_id, row["device_id"])):
            raise AuthError("Invalid or expired device token")
        return {"deviceId": row["device_id"], "name": row["name"],
                "ownerId": row["owner_id"] or "device:" + _digest(row["device_id"]),
                "createdAt": _iso(row["created_at"]), "expiresAt": _iso(row["expires_at"]),
                "aiEnabled": row["access"] == "full"}

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
    subparsers.add_parser("create-review-code", help="Print a single-use seven-day App Review code with AI disabled")
    revoke = subparsers.add_parser("revoke-device", help="Revoke every token for a device")
    revoke.add_argument("device_id")
    arguments = parser.parse_args()
    store = AuthStore(arguments.state_dir)
    if arguments.command == "create-code":
        print(json.dumps(store.create_pairing_code(), ensure_ascii=False))
    elif arguments.command == "create-review-code":
        print(json.dumps(store.create_review_pairing_code(), ensure_ascii=False))
    else:
        print(json.dumps({"revoked": store.revoke_device(arguments.device_id)}))


if __name__ == "__main__":
    main()
