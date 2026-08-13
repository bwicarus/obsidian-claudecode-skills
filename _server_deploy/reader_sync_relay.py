"""Durable, account-partitioned relay for Reader DataStore changes.

This is deliberately separate from ``/pdf/api/sync-batch`` (CommandOutbox).
The relay stores opaque DataStore records, assigns server cursors, exposes
explicit revision conflicts, and materializes consistent recovery snapshots.
Collection eligibility remains owned by the client DataRegistry.
"""
from __future__ import annotations

import contextlib
import hmac
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from reader_sidecar_store import NAMESPACE_RE


CONTRACT = "sync-gateway/2"
SYNC_CONTRACT = "sync-v3"
SYNC_CHANGE_CONTRACT = "record-parent-state/1"
REGISTRY_DIGEST_PREFIX = (
    f"{SYNC_CONTRACT}:{SYNC_CHANGE_CONTRACT}|"
)
LEGACY_REGISTRY_DIGEST = (
    REGISTRY_DIGEST_PREFIX
    + "user-settings:explicit:0:1|"
    + "vocabulary-state:explicit:0:1"
)
CARD_REGISTRY_DIGEST = (
    REGISTRY_DIGEST_PREFIX
    + "card-entities:explicit:0:1|"
    + "card-states:explicit:0:1|"
    + "user-settings:explicit:0:1|"
    + "vocabulary-state:explicit:0:1"
)
SIGNAL_CONTRACT = "direct-signal/1"
OWNER_LEASE_CONTRACT = "owner-lease/1"
NATIVE_SYNC_BOOTSTRAP_CONTRACT = "native-sync-bootstrap/1"
OWNER_LEASE_TTL_SECONDS = 30
NATIVE_SYNC_BOOTSTRAP_TTL_SECONDS = 60
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_CHANGES = 100
MAX_LIMIT = 100
SNAPSHOT_TTL_SECONDS = 3600
SIGNAL_PRESENCE_TTL_SECONDS = 45
SIGNAL_MESSAGE_TTL_SECONDS = 120
SIGNAL_DEDUPE_TTL_SECONDS = 24 * 3600
MAX_SIGNALS = 32
MAX_SIGNAL_RESULTS = 100
MAX_SIGNAL_PEERS = 64
MAX_SIGNAL_PAYLOAD_BYTES = 64 * 1024
RECORD_SCHEMA = 1
CAUSAL_CONTRACT = "record-parent-state/1"
MAX_CAUSAL_PARENT_BYTES = 512 * 1024
MAX_JS_SAFE_INTEGER = 2**53 - 1
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DEVICE_FAMILY_RE = re.compile(
    r"^(?:pwa-install|native-app)-v1-[a-f0-9]{32}$"
)

bp = Blueprint("reader_sync_relay", __name__, url_prefix="/api/reader/sync")
DEPLOYMENT_PROBE_HEADER = "X-BW-Reader-Deployment-Probe"


class RelayRequestError(ValueError):
    def __init__(self, message: str, code: str = "BW_SYNC_INVALID", status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


def default_sync_root() -> Path:
    base = Path(os.environ.get("WEBAPP_DATA", "/root/webapp/data"))
    return (base / "reader-sync-v1").resolve()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _records_semantically_equal(left: Any, right: Any) -> bool:
    """Compare DataStore records without transport-only metadata.

    ``rev``, ``updatedAt`` and ``updatedBy`` describe how a record arrived, not
    its business value.  Tombstones intentionally retain the last live value
    for local recovery, so two tombstones for the same stable record are equal
    regardless of that retained payload.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_deleted = left.get("deleted")
    right_deleted = right.get("deleted")
    if (
        left.get("collection") != right.get("collection")
        or left.get("id") != right.get("id")
        or not isinstance(left_deleted, bool)
        or not isinstance(right_deleted, bool)
        or left_deleted is not right_deleted
    ):
        return False
    if left_deleted is True:
        return True
    return _canonical_json(left.get("value")) == _canonical_json(right.get("value"))


def _inspect_causal_proof(record: dict) -> tuple[bool, str, Any]:
    if "causal" not in record:
        return False, "causal-proof-missing", None
    proof = record.get("causal")
    if (
        not isinstance(proof, dict)
        or set(proof) != {"contract", "parent"}
        or proof.get("contract") != CAUSAL_CONTRACT
    ):
        return False, "causal-proof-invalid", None
    parent = proof.get("parent")
    if parent is None:
        return True, "", None
    if not isinstance(parent, dict) or not isinstance(parent.get("deleted"), bool):
        return False, "causal-proof-invalid", None
    expected = {"deleted"} if parent["deleted"] else {"deleted", "value"}
    if set(parent) != expected:
        return False, "causal-proof-invalid", None
    try:
        encoded = _canonical_json(parent).encode("utf-8")
    except (TypeError, ValueError):
        return False, "causal-proof-invalid", None
    if len(encoded) > MAX_CAUSAL_PARENT_BYTES:
        return False, "causal-proof-too-large", None
    return True, "", parent


def _causal_parent_matches(
    current: dict | None,
    parent: Any,
) -> bool:
    if parent is None:
        return current is None
    if current is None or parent["deleted"] is not bool(current.get("deleted")):
        return False
    if parent["deleted"] is True:
        return True
    return _canonical_json(parent.get("value")) == _canonical_json(
        current.get("value")
    )


def _json_response(payload: dict, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store, private"
    return response


def _error(exc: RelayRequestError, *, contract=CONTRACT):
    return _json_response(
        {
            "ok": False,
            "contract": contract,
            "error": str(exc),
            "code": exc.code,
        },
        exc.status,
    )


@bp.before_request
def _deployment_probe_read_only_gate():
    """真实页面部署 E2E 不得取得租约或改写同步中继状态。"""
    if request.headers.get(DEPLOYMENT_PROBE_HEADER) != "1":
        return None
    if request.path.endswith("/signal"):
        contract = SIGNAL_CONTRACT
    elif "/owner/" in request.path:
        contract = OWNER_LEASE_CONTRACT
    else:
        contract = CONTRACT
    return _json_response(
        {
            "ok": False,
            "contract": contract,
            "error": "部署浏览器探测为只读模式",
            "code": "BW_SYNC_DEPLOYMENT_PROBE_READ_ONLY",
        },
        503,
    )


def _identity() -> dict:
    resolver = current_app.extensions.get("reader_storage_identity_resolver")
    identity = resolver() if callable(resolver) else None
    if not isinstance(identity, dict):
        raise RelayRequestError("需要登录或有效 Bearer token", "BW_SYNC_AUTH", 401)
    namespace = str(identity.get("storage_namespace") or "")
    user_id = identity.get("user_id")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not NAMESPACE_RE.fullmatch(namespace)
    ):
        raise RelayRequestError("认证身份无效", "BW_SYNC_AUTH", 401)
    return {"user_id": user_id, "storage_namespace": namespace}


def _request_json(*, contract=CONTRACT) -> dict:
    content_length = request.content_length
    if content_length is not None and content_length > MAX_BODY_BYTES:
        raise RelayRequestError("同步请求过大", "BW_SYNC_TOO_LARGE", 413)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise RelayRequestError("请求必须是 JSON 对象")
    try:
        encoded = _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RelayRequestError("请求包含非 JSON 值") from exc
    if len(encoded) > MAX_BODY_BYTES:
        raise RelayRequestError("同步请求过大", "BW_SYNC_TOO_LARGE", 413)
    if value.get("contract") != contract:
        raise RelayRequestError("sync contract 不匹配", "BW_SYNC_CONTRACT")
    return value


def _require_registry_digest(value: Any) -> str:
    registry_digest = value
    if (
        not isinstance(registry_digest, str)
        or len(registry_digest) > 2048
        or any(ord(character) < 32 or ord(character) == 127
               for character in registry_digest)
        or not registry_digest.startswith(REGISTRY_DIGEST_PREFIX)
        or len(registry_digest) <= len(REGISTRY_DIGEST_PREFIX)
    ):
        raise RelayRequestError(
            "同步因果合同或 registry 摘要不匹配",
            "BW_SYNC_CONTRACT",
            409,
        )
    return registry_digest


def _require_sync_fence(body: dict) -> str:
    if (
        body.get("syncContract") != SYNC_CONTRACT
        or body.get("syncChangeContract") != SYNC_CHANGE_CONTRACT
    ):
        raise RelayRequestError(
            "同步因果合同或 registry 摘要不匹配",
            "BW_SYNC_CONTRACT",
            409,
        )
    return _require_registry_digest(body.get("registryDigest"))


def _safe_integer(value: Any, label: str, *, maximum=2**63 - 1) -> int:
    if isinstance(value, bool):
        raise RelayRequestError(f"{label} 必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RelayRequestError(f"{label} 必须是非负整数") from exc
    if number < 0 or number > maximum or str(value).strip() not in {
        str(number), f"{number}.0"
    }:
        raise RelayRequestError(f"{label} 必须是非负整数")
    return number


def _safe_name(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _SAFE_NAME_RE.fullmatch(value):
        raise RelayRequestError(f"{label} 无效")
    return value


def _safe_id(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if (
        not value
        or len(value) > 300
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RelayRequestError(f"{label} 无效")
    return value


def _safe_text(value: Any, label: str, *, max_bytes: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise RelayRequestError(f"{label} 无效")
    if (
        len(value.encode("utf-8")) > max_bytes
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RelayRequestError(f"{label} 无效")
    return value


def _assert_finite_json(value: Any, path="value") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RelayRequestError(f"{path} 含非有限数字")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RelayRequestError(f"{path} 含非字符串键")
            _assert_finite_json(item, f"{path}.{key}")
        return
    raise RelayRequestError(f"{path} 不是 JSON 值")


def _owner_and_device(body: dict) -> tuple[dict, str]:
    identity = _identity()
    supplied = str(body.get("ownerNamespace") or "")
    if supplied != identity["storage_namespace"]:
        raise RelayRequestError(
            "ownerNamespace 与认证账户不一致",
            "BW_SYNC_OWNER_MISMATCH",
            403,
        )
    device_id = _safe_name(body.get("deviceId"), "deviceId")
    return identity, device_id


def _owner_and_common(body: dict) -> tuple[dict, str, int]:
    identity, device_id = _owner_and_device(body)
    limit = _safe_integer(body.get("limit", 100), "limit", maximum=MAX_LIMIT)
    if limit < 1:
        raise RelayRequestError("limit 必须大于 0")
    return identity, device_id, limit


def _owner_lease_holder(body: dict) -> dict:
    device_family_id = str(body.get("deviceFamilyId") or "").strip()
    if not _DEVICE_FAMILY_RE.fullmatch(device_family_id):
        raise RelayRequestError("deviceFamilyId 无效")
    owner_role = body.get("ownerRole")
    if owner_role not in {"pwa", "extension", "native"}:
        raise RelayRequestError("ownerRole 只允许 pwa、extension 或 native")
    if (
        owner_role == "native"
        and not device_family_id.startswith("native-app-v1-")
    ) or (
        owner_role != "native"
        and not device_family_id.startswith("pwa-install-v1-")
    ):
        raise RelayRequestError("deviceFamilyId 与 ownerRole 不匹配")
    if not isinstance(body.get("ownerInstanceId"), str):
        raise RelayRequestError("ownerInstanceId 必须是字符串")
    owner_instance_id = _safe_name(
        body.get("ownerInstanceId"),
        "ownerInstanceId",
    )
    if not isinstance(body.get("deviceId"), str):
        raise RelayRequestError("deviceId 必须是字符串")
    device_id = _safe_name(body.get("deviceId"), "deviceId")
    return {
        "deviceFamilyId": device_family_id,
        "ownerRole": owner_role,
        "ownerInstanceId": owner_instance_id,
        "deviceId": device_id,
    }


def _owner_lease_credentials(body: dict) -> dict:
    holder = _owner_lease_holder(body)
    generation = _safe_integer(
        body.get("ownerGeneration"),
        "ownerGeneration",
        maximum=MAX_JS_SAFE_INTEGER,
    )
    if generation < 1:
        raise RelayRequestError("ownerGeneration 必须大于 0")
    token = _safe_text(
        body.get("ownerToken"),
        "ownerToken",
        max_bytes=512,
    )
    return {
        **holder,
        "ownerGeneration": generation,
        "ownerToken": token,
    }


def _business_owner_lease_credentials(body: dict) -> dict:
    try:
        return _owner_lease_credentials(body)
    except RelayRequestError as exc:
        raise RelayRequestError(
            "同步 owner lease 不存在、已过期或不匹配",
            "BW_SYNC_OWNER_INACTIVE",
            409,
        ) from exc


def _owner_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_signal(raw: Any, index: int) -> dict:
    label = f"signals[{index}]"
    if not isinstance(raw, dict):
        raise RelayRequestError(f"{label} 必须是对象")
    allowed = {"signalId", "toDeviceId", "sessionId", "kind", "payload"}
    if set(raw) != allowed:
        raise RelayRequestError(f"{label} 字段无效")
    for field in ("signalId", "toDeviceId", "sessionId", "kind"):
        if not isinstance(raw.get(field), str):
            raise RelayRequestError(f"{label}.{field} 必须是字符串")
    signal_id = _safe_id(raw.get("signalId"), f"{label}.signalId")
    to_device_id = _safe_name(raw.get("toDeviceId"), f"{label}.toDeviceId")
    session_id = _safe_name(raw.get("sessionId"), f"{label}.sessionId")
    kind = str(raw.get("kind") or "").strip()
    if kind not in {"offer", "answer", "ice", "bye"}:
        raise RelayRequestError(f"{label}.kind 无效")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise RelayRequestError(f"{label}.payload 必须是对象")
    _assert_finite_json(payload, f"{label}.payload")
    payload_json = _canonical_json(payload)
    if len(payload_json.encode("utf-8")) > MAX_SIGNAL_PAYLOAD_BYTES:
        raise RelayRequestError(
            f"{label}.payload 过大",
            "BW_DIRECT_SIGNAL_TOO_LARGE",
            413,
        )
    normalized = {
        "signalId": signal_id,
        "toDeviceId": to_device_id,
        "sessionId": session_id,
        "kind": kind,
        "payload": json.loads(payload_json),
    }
    return {
        **normalized,
        "_payload_json": payload_json,
        "_canonical": _canonical_json(normalized),
    }


def _normalize_change(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise RelayRequestError(f"changes[{index}] 必须是对象")
    mutation_id = _safe_id(raw.get("mutationId"), f"changes[{index}].mutationId")
    collection = _safe_name(raw.get("collection"), f"changes[{index}].collection")
    operation = raw.get("operation")
    if not isinstance(operation, str) or operation not in {"put", "remove"}:
        raise RelayRequestError(f"changes[{index}].operation 无效")
    record = raw.get("record")
    if not isinstance(record, dict):
        raise RelayRequestError(f"changes[{index}].record 必须是对象")
    _assert_finite_json(record, f"changes[{index}].record")
    record = json.loads(_canonical_json(record))
    record_path = f"changes[{index}].record"
    schema = record.get("schema") if "schema" in record else None
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != RECORD_SCHEMA:
        raise RelayRequestError(
            f"{record_path}.schema 必须为 {RECORD_SCHEMA}",
            "BW_SYNC_SCHEMA",
        )
    if "collection" not in record or not isinstance(record["collection"], str):
        raise RelayRequestError(f"{record_path}.collection 必须是字符串")
    record_collection = _safe_name(
        record["collection"],
        f"{record_path}.collection",
    )
    if record_collection != collection:
        raise RelayRequestError(f"changes[{index}] collection 不一致")
    record["collection"] = record_collection
    if "id" not in record or not isinstance(record["id"], str):
        raise RelayRequestError(f"{record_path}.id 必须是字符串")
    record_id = _safe_id(record["id"], f"{record_path}.id")
    revision = record.get("rev") if "rev" in record else None
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or revision > MAX_JS_SAFE_INTEGER
    ):
        raise RelayRequestError(f"{record_path}.rev 必须是大于 0 的安全整数")
    updated_at = record.get("updatedAt") if "updatedAt" in record else None
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(updated_at)
        or updated_at < 0
    ):
        raise RelayRequestError(f"{record_path}.updatedAt 必须是非负有限数字")
    updated_by = record.get("updatedBy") if "updatedBy" in record else None
    if (
        not isinstance(updated_by, str)
        or updated_by != updated_by.strip()
        or not updated_by
        or len(updated_by) > 300
        or any(ord(character) < 32 or ord(character) == 127 for character in updated_by)
    ):
        raise RelayRequestError(f"{record_path}.updatedBy 必须是有效的非空字符串")
    deleted = record.get("deleted")
    if "deleted" not in record or not isinstance(deleted, bool):
        raise RelayRequestError(f"{record_path}.deleted 必须是布尔值")
    if "value" not in record:
        raise RelayRequestError(f"{record_path}.value 不能为空缺失")
    _assert_finite_json(record["value"], f"{record_path}.value")
    if operation == "remove" and deleted is not True:
        raise RelayRequestError(f"changes[{index}] remove 必须携带 tombstone")
    if operation == "put" and deleted is not False:
        raise RelayRequestError(f"changes[{index}] put 不得携带 tombstone")
    record["collection"] = collection
    causal_valid, causal_reason, causal_parent = _inspect_causal_proof(record)
    normalized = {
        "mutationId": mutation_id,
        "operation": operation,
        "collection": collection,
        "record": record,
    }
    return {
        **normalized,
        "_record_id": record_id,
        "_revision": revision,
        "_deleted": deleted,
        "_causal_valid": causal_valid,
        "_causal_reason": causal_reason,
        "_causal_parent": causal_parent,
        "_payload": _canonical_json(normalized),
    }


def _db_path(namespace: str) -> Path:
    if not NAMESPACE_RE.fullmatch(namespace):
        raise RelayRequestError("storage namespace 无效", "BW_SYNC_AUTH", 401)
    root = Path(
        current_app.extensions.get("reader_sync_root") or default_sync_root()
    ).resolve()
    account = (root / namespace).resolve()
    try:
        account.relative_to(root)
    except ValueError as exc:  # pragma: no cover - guarded by regex
        raise RelayRequestError("storage namespace 越界", "BW_SYNC_AUTH", 401) from exc
    account.mkdir(parents=True, exist_ok=True)
    return account / "relay.sqlite3"


def _connect(namespace: str) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(_db_path(namespace)),
        timeout=10,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        # Install the busy handler before journal/schema initialization: several
        # devices can be the first connection for an account at the same time.
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # SQLite can reject a simultaneous WAL pragma immediately even with
            # a busy handler. The competing connection is performing the same
            # one-way initialization; schema/transaction writes below still use
            # the busy timeout and safely wait for it.
            if "locked" not in str(exc).lower():
                raise
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
        CREATE TABLE IF NOT EXISTS relay_state (
          singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
          current_cursor INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO relay_state(singleton,current_cursor) VALUES(1,0);
        CREATE TABLE IF NOT EXISTS relay_mutations (
          mutation_id TEXT PRIMARY KEY,
          payload_sha256 TEXT NOT NULL,
          status TEXT NOT NULL,
          result_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relay_events (
          cursor INTEGER PRIMARY KEY,
          mutation_id TEXT NOT NULL UNIQUE,
          device_id TEXT NOT NULL,
          collection TEXT NOT NULL,
          record_id TEXT NOT NULL,
          change_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relay_heads (
          collection TEXT NOT NULL,
          record_id TEXT NOT NULL,
          rev INTEGER NOT NULL,
          deleted INTEGER NOT NULL,
          change_json TEXT NOT NULL,
          cursor INTEGER NOT NULL,
          PRIMARY KEY(collection,record_id)
        );
        CREATE TABLE IF NOT EXISTS relay_snapshots (
          snapshot_id TEXT PRIMARY KEY,
          snapshot_cursor INTEGER NOT NULL,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relay_snapshot_items (
          snapshot_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          change_json TEXT NOT NULL,
          PRIMARY KEY(snapshot_id,ordinal),
          FOREIGN KEY(snapshot_id) REFERENCES relay_snapshots(snapshot_id)
            ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS direct_signal_state (
          singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
          current_cursor INTEGER NOT NULL,
          expired_through INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO direct_signal_state(
          singleton,current_cursor,expired_through
        ) VALUES(1,0,0);
        CREATE TABLE IF NOT EXISTS direct_presence (
          device_id TEXT PRIMARY KEY,
          registry_digest TEXT NOT NULL,
          server_cursor INTEGER NOT NULL,
          local_cursor INTEGER NOT NULL,
          server_ready INTEGER NOT NULL,
          last_seen INTEGER NOT NULL,
          expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS direct_presence_registry_expiry
          ON direct_presence(registry_digest,expires_at);
        CREATE TABLE IF NOT EXISTS direct_signals (
          id INTEGER PRIMARY KEY,
          signal_id TEXT NOT NULL,
          from_device_id TEXT NOT NULL,
          to_device_id TEXT NOT NULL,
          registry_digest TEXT NOT NULL,
          server_cursor INTEGER NOT NULL,
          session_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          UNIQUE(from_device_id,signal_id)
        );
        CREATE INDEX IF NOT EXISTS direct_signals_target_cursor
          ON direct_signals(to_device_id,id);
        CREATE TABLE IF NOT EXISTS direct_signal_dedupe (
          from_device_id TEXT NOT NULL,
          signal_id TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL,
          signal_cursor INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          PRIMARY KEY(from_device_id,signal_id)
        );
        CREATE TABLE IF NOT EXISTS sync_owner_leases (
          device_family_id TEXT PRIMARY KEY,
          generation INTEGER NOT NULL,
          owner_role TEXT,
          owner_instance_id TEXT,
          device_id TEXT,
          token_sha256 TEXT,
          claimed_at INTEGER,
          renewed_at INTEGER,
          released_at INTEGER,
          handoff_role TEXT,
          handoff_requested_at INTEGER,
          expires_at INTEGER NOT NULL
        );
            """
        )
        state_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(relay_state)")
        }
        if "causal_contract" not in state_columns:
            try:
                connection.execute(
                    "ALTER TABLE relay_state ADD COLUMN causal_contract TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        if "causal_start_cursor" not in state_columns:
            try:
                connection.execute(
                    "ALTER TABLE relay_state "
                    "ADD COLUMN causal_start_cursor INTEGER"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        if "registry_digest" not in state_columns:
            try:
                connection.execute(
                    "ALTER TABLE relay_state ADD COLUMN registry_digest TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        lease_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(sync_owner_leases)"
            )
        }
        if "released_at" not in lease_columns:
            try:
                connection.execute(
                    "ALTER TABLE sync_owner_leases "
                    "ADD COLUMN released_at INTEGER"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        for column_name, column_type in (
            ("handoff_role", "TEXT"),
            ("handoff_requested_at", "INTEGER"),
        ):
            if column_name in lease_columns:
                continue
            try:
                connection.execute(
                    f"ALTER TABLE sync_owner_leases "
                    f"ADD COLUMN {column_name} {column_type}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    except Exception:
        connection.close()
        raise
    return connection


@contextlib.contextmanager
def _connection(namespace: str):
    connection = _connect(namespace)
    try:
        yield connection
    finally:
        connection.close()


def _state(connection: sqlite3.Connection) -> tuple[int, int]:
    current = int(connection.execute(
        "SELECT current_cursor FROM relay_state WHERE singleton=1"
    ).fetchone()["current_cursor"])
    row = connection.execute("SELECT MIN(cursor) AS oldest FROM relay_events").fetchone()
    oldest = int(row["oldest"]) if row and row["oldest"] is not None else current + 1
    return current, oldest


def _pin_registry_digest_locked(
    connection: sqlite3.Connection,
    registry_digest: str,
    *,
    allow_legacy_upgrade: bool = False,
    now: int | None = None,
) -> None:
    row = connection.execute(
        "SELECT current_cursor,registry_digest FROM relay_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise RelayRequestError(
            "同步中继状态缺失",
            "BW_SYNC_SCHEMA",
            500,
        )
    pinned = row["registry_digest"]
    if pinned is None:
        connection.execute(
            "UPDATE relay_state SET registry_digest=? WHERE singleton=1",
            (registry_digest,),
        )
        return
    if pinned == registry_digest:
        return
    if not (
        allow_legacy_upgrade
        and pinned == LEGACY_REGISTRY_DIGEST
        and registry_digest == CARD_REGISTRY_DIGEST
        and now is not None
    ):
        raise RelayRequestError(
            "当前账户的 DataRegistry 摘要与中继不一致",
            "BW_SYNC_REGISTRY_MISMATCH",
            409,
        )

    # Registry generations salt direct account proofs and define which
    # collections a checkpoint represents.  The only supported transition is
    # the exact settings/vocabulary generation to the card-enabled generation.
    # It is entered only from owner/claim while holding BEGIN IMMEDIATE, so two
    # first clients serialize and the cleanup below runs exactly once.
    current_cursor = int(row["current_cursor"])
    updated = connection.execute(
        "UPDATE relay_state SET registry_digest=?,causal_contract=?,"
        "causal_start_cursor=? WHERE singleton=1 AND registry_digest=?",
        (
            registry_digest,
            SYNC_CHANGE_CONTRACT,
            current_cursor,
            LEGACY_REGISTRY_DIGEST,
        ),
    )
    if updated.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE serializes it
        raise RelayRequestError(
            "当前账户的 DataRegistry 摘要与中继不一致",
            "BW_SYNC_REGISTRY_MISMATCH",
            409,
        )

    # Durable business heads, events and mutation receipts stay intact.  Old
    # direct/snapshot state is generation-bound and must never cross the fence.
    connection.execute("DELETE FROM relay_snapshot_items")
    connection.execute("DELETE FROM relay_snapshots")
    connection.execute("DELETE FROM direct_presence")
    connection.execute("DELETE FROM direct_signals")
    connection.execute("DELETE FROM direct_signal_dedupe")
    connection.execute(
        "UPDATE direct_signal_state SET current_cursor=0,expired_through=0 "
        "WHERE singleton=1"
    )
    # Keep per-family generation counters monotonic while making every old
    # credential unusable.  owner/claim runs after this and replaces only its
    # own row with a fresh token hash; plaintext tokens are never persisted.
    connection.execute(
        "UPDATE sync_owner_leases SET token_sha256=NULL,released_at=?,"
        "expires_at=0,handoff_role=NULL,handoff_requested_at=NULL",
        (now,),
    )


def _verify_owner_lease_locked(
    connection: sqlite3.Connection,
    credentials: dict,
    *,
    now: int,
) -> None:
    row = connection.execute(
        "SELECT generation,owner_role,owner_instance_id,device_id,"
        "token_sha256,released_at,expires_at FROM sync_owner_leases "
        "WHERE device_family_id=?",
        (credentials["deviceFamilyId"],),
    ).fetchone()
    active = _owner_lease_row_matches(row, credentials) and bool(
        row["released_at"] is None and int(row["expires_at"]) > now
    )
    if not active:
        raise RelayRequestError(
            "同步 owner lease 不存在、已过期或不匹配",
            "BW_SYNC_OWNER_INACTIVE",
            409,
        )


def _owner_lease_row_matches(
    row: sqlite3.Row | None,
    credentials: dict,
) -> bool:
    return bool(
        row
        and row["token_sha256"]
        and int(row["generation"]) == credentials["ownerGeneration"]
        and row["owner_role"] == credentials["ownerRole"]
        and row["owner_instance_id"] == credentials["ownerInstanceId"]
        and row["device_id"] == credentials["deviceId"]
        and hmac.compare_digest(
            str(row["token_sha256"]),
            _owner_token_hash(credentials["ownerToken"]),
        )
    )


def _activate_causal_epoch_locked(
    connection: sqlite3.Connection,
    registry_digest: str,
) -> int:
    _pin_registry_digest_locked(connection, registry_digest)
    row = connection.execute(
        "SELECT current_cursor,causal_contract,causal_start_cursor "
        "FROM relay_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise RelayRequestError(
            "同步中继状态缺失",
            "BW_SYNC_SCHEMA",
            500,
        )
    current = int(row["current_cursor"])
    active_contract = row["causal_contract"]
    if active_contract is None:
        start_cursor = current
        connection.execute(
            "UPDATE relay_state SET causal_contract=?,causal_start_cursor=? "
            "WHERE singleton=1",
            (SYNC_CHANGE_CONTRACT, start_cursor),
        )
    elif active_contract != SYNC_CHANGE_CONTRACT:
        raise RelayRequestError(
            "同步因果 epoch 不兼容",
            "BW_SYNC_CONTRACT",
            409,
        )
    else:
        start_cursor = int(row["causal_start_cursor"] or 0)
    return start_cursor


def _public_change(change: dict, cursor: int | None = None) -> dict:
    out = {
        "mutationId": change["mutationId"],
        "operation": change["operation"],
        "collection": change["collection"],
        "record": change["record"],
    }
    if cursor is not None:
        out["cursor"] = int(cursor)
    return out


def _push_locked(
    connection: sqlite3.Connection,
    *,
    device_id: str,
    changes: list[dict],
) -> tuple[list[str], list[dict]]:
    acknowledged: list[str] = []
    conflict_entries: list[dict] = []
    now = int(time.time())
    for change_index, change in enumerate(changes):
        mutation_id = change["mutationId"]
        payload_sha = hashlib.sha256(change["_payload"].encode("utf-8")).hexdigest()
        remembered = connection.execute(
            "SELECT payload_sha256,status,result_json FROM relay_mutations "
            "WHERE mutation_id=?",
            (mutation_id,),
        ).fetchone()
        remembered_conflict = False
        if remembered:
            if remembered["payload_sha256"] != payload_sha:
                conflict_entries.append({
                    "index": change_index,
                    "change": change,
                    "resolvable": False,
                    "result": {
                        "mutationId": mutation_id,
                        "collection": change["collection"],
                        "id": change["_record_id"],
                        "reason": "mutation-id-reuse",
                    },
                })
                continue
            else:
                result = json.loads(remembered["result_json"])
                if remembered["status"] == "acked":
                    acknowledged.append(mutation_id)
                    continue
                remembered_conflict = True

        head = connection.execute(
            "SELECT rev,deleted,change_json,cursor FROM relay_heads "
            "WHERE collection=? AND record_id=?",
            (change["collection"], change["_record_id"]),
        ).fetchone()
        if head:
            current_change = json.loads(head["change_json"])
            current_record = current_change["record"]
        else:
            current_record = None

        same_business = bool(head) and _records_semantically_equal(
            change["record"], current_record
        )
        semantic_advance = (
            same_business
            and change["_revision"] > int(head["rev"])
        )
        if same_business and not semantic_advance:
            outcome = {
                "mutationId": mutation_id,
                "status": "acked",
                "noOp": True,
                "cursor": int(head["cursor"]),
            }
            status = "acked"
        elif (
            head
            and bool(head["deleted"])
            and not change["_deleted"]
            and (
                not change["_causal_valid"]
                or not _causal_parent_matches(
                    current_record,
                    change["_causal_parent"],
                )
            )
        ):
            outcome = {
                "mutationId": mutation_id,
                "collection": change["collection"],
                "id": change["_record_id"],
                "incomingRev": change["_revision"],
                "currentRev": int(head["rev"]),
                "reason": "tombstone-dominates",
            }
            status = "conflict"
        elif not semantic_advance and not change["_causal_valid"]:
            outcome = {
                "mutationId": mutation_id,
                "collection": change["collection"],
                "id": change["_record_id"],
                "incomingRev": change["_revision"],
                "currentRev": int(head["rev"]) if head else 0,
                "reason": change["_causal_reason"],
            }
            status = "conflict"
        elif not semantic_advance and not _causal_parent_matches(
            current_record,
            change["_causal_parent"],
        ):
            outcome = {
                "mutationId": mutation_id,
                "collection": change["collection"],
                "id": change["_record_id"],
                "incomingRev": change["_revision"],
                "currentRev": int(head["rev"]) if head else 0,
                "reason": "causal-parent-mismatch",
            }
            status = "conflict"
        elif head and int(head["rev"]) >= MAX_JS_SAFE_INTEGER:
            outcome = {
                "mutationId": mutation_id,
                "collection": change["collection"],
                "id": change["_record_id"],
                "incomingRev": change["_revision"],
                "currentRev": int(head["rev"]),
                "reason": "causal-revision-overflow",
            }
            status = "conflict"
        else:
            effective_revision = max(
                change["_revision"],
                (int(head["rev"]) + 1) if head else 1,
            )
            current = int(connection.execute(
                "SELECT current_cursor FROM relay_state WHERE singleton=1"
            ).fetchone()["current_cursor"])
            cursor = current + 1
            public_change = _public_change(change, cursor)
            public_change["record"]["rev"] = effective_revision
            change_json = _canonical_json(public_change)
            connection.execute(
                "UPDATE relay_state SET current_cursor=? WHERE singleton=1",
                (cursor,),
            )
            connection.execute(
                "INSERT INTO relay_events("
                "cursor,mutation_id,device_id,collection,record_id,change_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    cursor,
                    mutation_id,
                    device_id,
                    change["collection"],
                    change["_record_id"],
                    change_json,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO relay_heads("
                "collection,record_id,rev,deleted,change_json,cursor"
                ") VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(collection,record_id) DO UPDATE SET "
                "rev=excluded.rev,deleted=excluded.deleted,"
                "change_json=excluded.change_json,cursor=excluded.cursor",
                (
                    change["collection"],
                    change["_record_id"],
                    effective_revision,
                    1 if change["_deleted"] else 0,
                    change_json,
                    cursor,
                ),
            )
            outcome = {
                "mutationId": mutation_id,
                "status": "acked",
                "noOp": False,
                "cursor": cursor,
                "effectiveRev": effective_revision,
            }
            status = "acked"

        if remembered_conflict:
            connection.execute(
                "UPDATE relay_mutations SET status=?,result_json=? "
                "WHERE mutation_id=? AND payload_sha256=?",
                (
                    status,
                    _canonical_json(outcome),
                    mutation_id,
                    payload_sha,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO relay_mutations("
                "mutation_id,payload_sha256,status,result_json,created_at"
                ") VALUES(?,?,?,?,?)",
                (
                    mutation_id,
                    payload_sha,
                    status,
                    _canonical_json(outcome),
                    now,
                ),
            )
        if status == "acked":
            acknowledged.append(mutation_id)
        else:
            conflict_entries.append({
                "index": change_index,
                "change": change,
                "resolvable": True,
                "result": outcome,
            })

    # A rejected earlier journal entry may become a semantic no-op during
    # the same ordered push.  Only exact final-head business equality can
    # resolve it.  A later higher revision is not proof: revisions are
    # device-local and two offline branches can both increase them.
    resolved_mutation_ids: set[str] = set()
    for entry in conflict_entries:
        if not entry["resolvable"]:
            continue
        change = entry["change"]
        head = connection.execute(
            "SELECT change_json,cursor FROM relay_heads "
            "WHERE collection=? AND record_id=?",
            (change["collection"], change["_record_id"]),
        ).fetchone()
        head_matches = False
        if head:
            current_change = json.loads(head["change_json"])
            head_matches = _records_semantically_equal(
                change["record"],
                current_change["record"],
            )
        if not head_matches:
            continue
        resolution = {
            "mutationId": change["mutationId"],
            "status": "acked",
            "noOp": True,
            "superseded": True,
            "supersededBy": "server-head",
            "cursor": int(head["cursor"]) if head else 0,
        }
        payload_sha = hashlib.sha256(
            change["_payload"].encode("utf-8")
        ).hexdigest()
        updated = connection.execute(
            "UPDATE relay_mutations SET status='acked',result_json=? "
            "WHERE mutation_id=? AND payload_sha256=? AND status='conflict'",
            (
                _canonical_json(resolution),
                change["mutationId"],
                payload_sha,
            ),
        )
        if updated.rowcount:
            resolved_mutation_ids.add(change["mutationId"])
            acknowledged.append(change["mutationId"])

    acknowledged_set = set(acknowledged)
    acknowledged = []
    for change in changes:
        mutation_id = change["mutationId"]
        if mutation_id in acknowledged_set and mutation_id not in acknowledged:
            acknowledged.append(mutation_id)
    conflicts = [
        entry["result"]
        for entry in conflict_entries
        if not (
            entry["resolvable"]
            and entry["change"]["mutationId"] in resolved_mutation_ids
        )
    ]
    return acknowledged, conflicts


def _account_proof(namespace: str, registry_digest: str) -> str:
    """Return an opaque account fence scoped to one sync registry generation.

    The wire shape intentionally remains ``account-proof-v1-<hex>`` because
    clients treat the value as opaque.  ``registry_digest`` is the shared
    protocol-generation salt: unlike an owner generation it is identical for
    every compatible device in the account, and unlike a clock/process salt it
    cannot split active workers or peers mid-generation.
    """
    registry_digest = _require_registry_digest(registry_digest)
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or not secret:
        raise RelayRequestError(
            "直连账户证明暂时不可用",
            "BW_SYNC_RETRYABLE",
            503,
        )
    digest = hmac.new(
        secret,
        (
            b"reader-direct-account-proof-v1\0registry-generation\0"
            + registry_digest.encode("utf-8")
            + b"\0"
            + namespace.encode("utf-8")
        ),
        hashlib.sha256,
    ).hexdigest()
    return "account-proof-v1-" + digest


def _cleanup_direct_state(connection: sqlite3.Connection, now: int) -> None:
    expired = connection.execute(
        "SELECT MAX(id) AS cursor FROM direct_signals WHERE expires_at<=?",
        (now,),
    ).fetchone()
    if expired and expired["cursor"] is not None:
        connection.execute(
            "UPDATE direct_signal_state SET expired_through="
            "MAX(expired_through,?) WHERE singleton=1",
            (int(expired["cursor"]),),
        )
    connection.execute("DELETE FROM direct_signals WHERE expires_at<=?", (now,))
    connection.execute(
        "DELETE FROM direct_signal_dedupe WHERE expires_at<=?",
        (now,),
    )
    connection.execute("DELETE FROM direct_presence WHERE expires_at<=?", (now,))


def _owner_lease_response(
    holder: dict,
    *,
    generation: int,
    token: str | None,
    expires_at: int,
    released: bool = False,
    replayed: bool = False,
):
    payload = {
        "ok": True,
        "contract": OWNER_LEASE_CONTRACT,
        **holder,
        "ownerGeneration": generation,
        "expiresAt": expires_at,
        "released": released,
        "replayed": replayed,
    }
    if token is not None:
        payload["ownerToken"] = token
    return _json_response(payload)


def _owner_lease_request(*, credentials: bool) -> tuple[dict, str, dict]:
    body = _request_json(contract=OWNER_LEASE_CONTRACT)
    registry_digest = _require_sync_fence(body)
    identity, device_id = _owner_and_device(body)
    owner = (
        _owner_lease_credentials(body)
        if credentials
        else _owner_lease_holder(body)
    )
    if not credentials and owner["ownerRole"] == "native":
        _verify_native_bootstrap_token(
            body.get("nativeBootstrapToken"),
            identity=identity,
            registry_digest=registry_digest,
            holder=owner,
        )
    if owner["deviceId"] != device_id:  # pragma: no cover - same parser/input
        raise RelayRequestError("deviceId 不一致")
    return identity, registry_digest, owner


def _native_bootstrap_secret() -> bytes:
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise RelayRequestError(
            "native sync bootstrap 暂时不可用",
            "BW_SYNC_NATIVE_BOOTSTRAP",
            503,
        )
    return bytes(secret)


def _native_bootstrap_mac(
    *,
    namespace: str,
    registry_digest: str,
    holder: dict,
    expires_at: int,
) -> str:
    payload = "\0".join((
        "reader-native-sync-bootstrap-v1",
        namespace,
        registry_digest,
        holder["deviceFamilyId"],
        holder["ownerInstanceId"],
        holder["deviceId"],
        str(expires_at),
    )).encode("utf-8")
    return hmac.new(
        _native_bootstrap_secret(),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _native_bootstrap_token(
    *,
    namespace: str,
    registry_digest: str,
    holder: dict,
    expires_at: int,
) -> str:
    return "native-bootstrap-v1-{}-{}".format(
        expires_at,
        _native_bootstrap_mac(
            namespace=namespace,
            registry_digest=registry_digest,
            holder=holder,
            expires_at=expires_at,
        ),
    )


def _verify_native_bootstrap_token(
    value: Any,
    *,
    identity: dict,
    registry_digest: str,
    holder: dict,
) -> None:
    token = str(value or "")
    match = re.fullmatch(
        r"native-bootstrap-v1-([0-9]{1,12})-([a-f0-9]{64})",
        token,
    )
    if not match:
        raise RelayRequestError(
            "native sync bootstrap 无效",
            "BW_SYNC_NATIVE_BOOTSTRAP",
            403,
        )
    expires_at = int(match.group(1))
    now = int(time.time())
    if expires_at < now or expires_at > now + NATIVE_SYNC_BOOTSTRAP_TTL_SECONDS:
        raise RelayRequestError(
            "native sync bootstrap 已失效",
            "BW_SYNC_NATIVE_BOOTSTRAP",
            403,
        )
    expected = _native_bootstrap_mac(
        namespace=identity["storage_namespace"],
        registry_digest=registry_digest,
        holder=holder,
        expires_at=expires_at,
    )
    if not hmac.compare_digest(match.group(2), expected):
        raise RelayRequestError(
            "native sync bootstrap 无效",
            "BW_SYNC_NATIVE_BOOTSTRAP",
            403,
        )


@bp.post("/native/bootstrap")
def native_bootstrap():
    """Authenticate App sync without exposing a durable credential to JS."""
    try:
        body = _request_json(contract=NATIVE_SYNC_BOOTSTRAP_CONTRACT)
        expected_keys = {
            "contract",
            "deviceFamilyId",
            "deviceId",
            "ownerInstanceId",
            "registryDigest",
            "requestId",
            "syncChangeContract",
            "syncContract",
        }
        if set(body) != expected_keys:
            raise RelayRequestError("native sync bootstrap 字段不匹配")
        registry_digest = _require_sync_fence(body)
        identity = _identity()
        request_id = _safe_name(body.get("requestId"), "requestId")
        holder = _owner_lease_holder({
            "deviceFamilyId": body.get("deviceFamilyId"),
            "ownerRole": "native",
            "ownerInstanceId": body.get("ownerInstanceId"),
            "deviceId": body.get("deviceId"),
        })
        expires_at = int(time.time()) + NATIVE_SYNC_BOOTSTRAP_TTL_SECONDS
        token = _native_bootstrap_token(
            namespace=identity["storage_namespace"],
            registry_digest=registry_digest,
            holder=holder,
            expires_at=expires_at,
        )
        return _json_response({
            "ok": True,
            "contract": NATIVE_SYNC_BOOTSTRAP_CONTRACT,
            "requestId": request_id,
            "ownerNamespace": identity["storage_namespace"],
            "nativeBootstrapToken": token,
            "expiresAt": expires_at,
        })
    except RelayRequestError as exc:
        return _error(exc, contract=NATIVE_SYNC_BOOTSTRAP_CONTRACT)


@bp.post("/owner/claim")
def owner_claim():
    try:
        identity, registry_digest, holder = _owner_lease_request(
            credentials=False,
        )
        now = int(time.time())
        expires_at = now + OWNER_LEASE_TTL_SECONDS
        token = "owner-token-v1-" + secrets.token_urlsafe(32)
        token_sha256 = _owner_token_hash(token)
        with _connection(identity["storage_namespace"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _pin_registry_digest_locked(
                    connection,
                    registry_digest,
                    allow_legacy_upgrade=True,
                    now=now,
                )
                row = connection.execute(
                    "SELECT generation,owner_role,expires_at,released_at "
                    "FROM sync_owner_leases WHERE device_family_id=?",
                    (holder["deviceFamilyId"],),
                ).fetchone()
                if (
                    row
                    and row["released_at"] is None
                    and int(row["expires_at"]) > now
                ):
                    if (
                        holder["ownerRole"] == "pwa"
                        and row["owner_role"] == "extension"
                    ):
                        connection.execute(
                            "UPDATE sync_owner_leases "
                            "SET handoff_role='pwa',handoff_requested_at=? "
                            "WHERE device_family_id=?",
                            (now, holder["deviceFamilyId"]),
                        )
                        connection.commit()
                    raise RelayRequestError(
                        "该设备 family 已由另一个同步 owner 持有",
                        "BW_SYNC_OWNER_HELD",
                        409,
                    )
                previous_generation = int(row["generation"]) if row else 0
                if previous_generation >= MAX_JS_SAFE_INTEGER:
                    raise RelayRequestError(
                        "同步 owner generation 已耗尽",
                        "BW_SYNC_OWNER_GENERATION",
                        409,
                    )
                generation = previous_generation + 1
                connection.execute(
                    "INSERT INTO sync_owner_leases("
                    "device_family_id,generation,owner_role,owner_instance_id,"
                    "device_id,token_sha256,claimed_at,renewed_at,released_at,"
                    "handoff_role,handoff_requested_at,expires_at"
                    ") VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,?) "
                    "ON CONFLICT(device_family_id) DO UPDATE SET "
                    "generation=excluded.generation,"
                    "owner_role=excluded.owner_role,"
                    "owner_instance_id=excluded.owner_instance_id,"
                    "device_id=excluded.device_id,"
                    "token_sha256=excluded.token_sha256,"
                    "claimed_at=excluded.claimed_at,"
                    "renewed_at=excluded.renewed_at,"
                    "released_at=NULL,"
                    "handoff_role=NULL,"
                    "handoff_requested_at=NULL,"
                    "expires_at=excluded.expires_at",
                    (
                        holder["deviceFamilyId"],
                        generation,
                        holder["ownerRole"],
                        holder["ownerInstanceId"],
                        holder["deviceId"],
                        token_sha256,
                        now,
                        now,
                        expires_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _owner_lease_response(
            holder,
            generation=generation,
            token=token,
            expires_at=expires_at,
        )
    except RelayRequestError as exc:
        return _error(exc, contract=OWNER_LEASE_CONTRACT)
    except (sqlite3.Error, OSError) as exc:
        current_app.logger.exception("reader sync owner claim failed")
        return _error(
            RelayRequestError(
                "同步 owner lease 暂时不可用",
                "BW_SYNC_RETRYABLE",
                503,
            ),
            contract=OWNER_LEASE_CONTRACT,
        )


@bp.post("/owner/renew")
def owner_renew():
    try:
        identity, registry_digest, credentials = _owner_lease_request(
            credentials=True,
        )
        now = int(time.time())
        expires_at = now + OWNER_LEASE_TTL_SECONDS
        with _connection(identity["storage_namespace"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_owner_lease_locked(connection, credentials, now=now)
                row = connection.execute(
                    "SELECT handoff_role FROM sync_owner_leases "
                    "WHERE device_family_id=?",
                    (credentials["deviceFamilyId"],),
                ).fetchone()
                if (
                    credentials["ownerRole"] == "extension"
                    and row
                    and row["handoff_role"] == "pwa"
                ):
                    raise RelayRequestError(
                        "PWA 已请求接管该设备 family 的同步 owner",
                        "BW_SYNC_OWNER_INACTIVE",
                        409,
                    )
                _pin_registry_digest_locked(connection, registry_digest)
                connection.execute(
                    "UPDATE sync_owner_leases SET renewed_at=?,expires_at=? "
                    "WHERE device_family_id=?",
                    (
                        now,
                        expires_at,
                        credentials["deviceFamilyId"],
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        holder = {
            key: credentials[key]
            for key in (
                "deviceFamilyId",
                "ownerRole",
                "ownerInstanceId",
                "deviceId",
            )
        }
        return _owner_lease_response(
            holder,
            generation=credentials["ownerGeneration"],
            token=credentials["ownerToken"],
            expires_at=expires_at,
        )
    except RelayRequestError as exc:
        return _error(exc, contract=OWNER_LEASE_CONTRACT)
    except (sqlite3.Error, OSError) as exc:
        current_app.logger.exception("reader sync owner renew failed")
        return _error(
            RelayRequestError(
                "同步 owner lease 暂时不可用",
                "BW_SYNC_RETRYABLE",
                503,
            ),
            contract=OWNER_LEASE_CONTRACT,
        )


@bp.post("/owner/release")
def owner_release():
    try:
        identity, registry_digest, credentials = _owner_lease_request(
            credentials=True,
        )
        now = int(time.time())
        replayed = False
        with _connection(identity["storage_namespace"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT generation,owner_role,owner_instance_id,device_id,"
                    "token_sha256,released_at,expires_at "
                    "FROM sync_owner_leases WHERE device_family_id=?",
                    (credentials["deviceFamilyId"],),
                ).fetchone()
                if (
                    _owner_lease_row_matches(row, credentials)
                    and row["released_at"] is not None
                ):
                    replayed = True
                else:
                    _verify_owner_lease_locked(
                        connection,
                        credentials,
                        now=now,
                    )
                    connection.execute(
                        "UPDATE sync_owner_leases "
                        "SET released_at=?,expires_at=0 "
                        "WHERE device_family_id=?",
                        (now, credentials["deviceFamilyId"]),
                    )
                _pin_registry_digest_locked(connection, registry_digest)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        holder = {
            key: credentials[key]
            for key in (
                "deviceFamilyId",
                "ownerRole",
                "ownerInstanceId",
                "deviceId",
            )
        }
        return _owner_lease_response(
            holder,
            generation=credentials["ownerGeneration"],
            token=None,
            expires_at=0,
            released=True,
            replayed=replayed,
        )
    except RelayRequestError as exc:
        return _error(exc, contract=OWNER_LEASE_CONTRACT)
    except (sqlite3.Error, OSError) as exc:
        current_app.logger.exception("reader sync owner release failed")
        return _error(
            RelayRequestError(
                "同步 owner lease 暂时不可用",
                "BW_SYNC_RETRYABLE",
                503,
            ),
            contract=OWNER_LEASE_CONTRACT,
        )


@bp.post("/signal")
def signal():
    try:
        body = _request_json(contract=SIGNAL_CONTRACT)
        identity, device_id = _owner_and_device(body)
        if not isinstance(body.get("deviceId"), str):
            raise RelayRequestError("deviceId 必须是字符串")
        owner_credentials = _business_owner_lease_credentials(body)
        registry_digest = _require_registry_digest(body.get("registryDigest"))
        server_cursor = _safe_integer(body.get("serverCursor"), "serverCursor")
        local_cursor = _safe_integer(body.get("localCursor"), "localCursor")
        signal_cursor = _safe_integer(body.get("signalCursor", 0), "signalCursor")
        server_ready = body.get("serverReady")
        if not isinstance(server_ready, bool):
            raise RelayRequestError("serverReady 必须是布尔值")
        raw_signals = body.get("signals")
        if not isinstance(raw_signals, list):
            raise RelayRequestError("signals 必须是数组")
        if len(raw_signals) > MAX_SIGNALS:
            raise RelayRequestError(
                "signals 超过单批上限",
                "BW_DIRECT_SIGNAL_TOO_LARGE",
                413,
            )
        signals = [
            _normalize_signal(raw_signal, index)
            for index, raw_signal in enumerate(raw_signals)
        ]
        signal_ids: set[str] = set()
        for normalized in signals:
            if normalized["signalId"] in signal_ids:
                raise RelayRequestError(
                    "同一批 signals 含重复 signalId",
                    "BW_DIRECT_SIGNAL_ID_REUSE",
                    409,
                )
            signal_ids.add(normalized["signalId"])

        account_proof = ""
        now = int(time.time())
        with _connection(identity["storage_namespace"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_owner_lease_locked(
                    connection,
                    owner_credentials,
                    now=now,
                )
                _pin_registry_digest_locked(connection, registry_digest)
                account_proof = _account_proof(
                    identity["storage_namespace"],
                    registry_digest,
                )
                _cleanup_direct_state(connection, now)
                current, _oldest = _state(connection)
                if server_cursor > current:
                    raise RelayRequestError(
                        "serverCursor 超过服务器 head",
                        "BW_DIRECT_SERVER_CURSOR",
                        409,
                    )
                baseline_ready = bool(server_ready and server_cursor == current)
                connection.execute(
                    "INSERT INTO direct_presence("
                    "device_id,registry_digest,server_cursor,local_cursor,server_ready,"
                    "last_seen,expires_at"
                    ") VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET "
                    "registry_digest=excluded.registry_digest,"
                    "server_cursor=excluded.server_cursor,"
                    "local_cursor=excluded.local_cursor,"
                    "server_ready=excluded.server_ready,"
                    "last_seen=excluded.last_seen,"
                    "expires_at=excluded.expires_at",
                    (
                        device_id,
                        registry_digest,
                        server_cursor,
                        local_cursor,
                        1 if server_ready else 0,
                        now,
                        now + SIGNAL_PRESENCE_TTL_SECONDS,
                    ),
                )

                acknowledged: list[str] = []
                new_signals: list[tuple[dict, str]] = []
                for normalized in signals:
                    dedupe_payload = _canonical_json({
                        "registryDigest": registry_digest,
                        "serverCursor": server_cursor,
                        "signal": {
                            key: normalized[key]
                            for key in (
                                "signalId",
                                "toDeviceId",
                                "sessionId",
                                "kind",
                                "payload",
                            )
                        },
                    })
                    payload_sha = hashlib.sha256(
                        dedupe_payload.encode("utf-8")
                    ).hexdigest()
                    remembered = connection.execute(
                        "SELECT payload_sha256 FROM direct_signal_dedupe "
                        "WHERE from_device_id=? AND signal_id=?",
                        (device_id, normalized["signalId"]),
                    ).fetchone()
                    if remembered:
                        if remembered["payload_sha256"] != payload_sha:
                            raise RelayRequestError(
                                "signalId 已被用于不同信令",
                                "BW_DIRECT_SIGNAL_ID_REUSE",
                                409,
                            )
                        acknowledged.append(normalized["signalId"])
                    else:
                        new_signals.append((normalized, payload_sha))

                if new_signals and not baseline_ready:
                    raise RelayRequestError(
                        "发送信令前必须完成服务器基线同步",
                        "BW_DIRECT_BASELINE_REQUIRED",
                        409,
                    )
                for normalized, _payload_sha in new_signals:
                    if normalized["toDeviceId"] == device_id:
                        raise RelayRequestError(
                            "不得向本设备发送信令",
                            "BW_DIRECT_SELF_SIGNAL",
                            400,
                        )
                    target = connection.execute(
                        "SELECT registry_digest,server_cursor,server_ready "
                        "FROM direct_presence "
                        "WHERE device_id=? AND expires_at>?",
                        (normalized["toDeviceId"], now),
                    ).fetchone()
                    if (
                        not target
                        or target["registry_digest"] != registry_digest
                        or int(target["server_ready"]) != 1
                        or int(target["server_cursor"]) != current
                    ):
                        raise RelayRequestError(
                            "目标设备不可用或尚未完成服务器基线同步",
                            "BW_DIRECT_TARGET_UNAVAILABLE",
                            409,
                        )

                state_row = connection.execute(
                    "SELECT current_cursor,expired_through "
                    "FROM direct_signal_state WHERE singleton=1"
                ).fetchone()
                signal_head = int(state_row["current_cursor"])
                for normalized, payload_sha in new_signals:
                    signal_head += 1
                    connection.execute(
                        "INSERT INTO direct_signals("
                        "id,signal_id,from_device_id,to_device_id,"
                        "registry_digest,server_cursor,session_id,kind,"
                        "payload_json,created_at,expires_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            signal_head,
                            normalized["signalId"],
                            device_id,
                            normalized["toDeviceId"],
                            registry_digest,
                            current,
                            normalized["sessionId"],
                            normalized["kind"],
                            normalized["_payload_json"],
                            now,
                            now + SIGNAL_MESSAGE_TTL_SECONDS,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO direct_signal_dedupe("
                        "from_device_id,signal_id,payload_sha256,"
                        "signal_cursor,expires_at"
                        ") VALUES(?,?,?,?,?)",
                        (
                            device_id,
                            normalized["signalId"],
                            payload_sha,
                            signal_head,
                            now + SIGNAL_DEDUPE_TTL_SECONDS,
                        ),
                    )
                    acknowledged.append(normalized["signalId"])
                if new_signals:
                    connection.execute(
                        "UPDATE direct_signal_state SET current_cursor=? "
                        "WHERE singleton=1",
                        (signal_head,),
                    )

                peer_rows = connection.execute(
                    "SELECT device_id,server_cursor,local_cursor,server_ready "
                    "FROM direct_presence "
                    "WHERE device_id<>? AND registry_digest=? AND expires_at>? "
                    "ORDER BY device_id LIMIT ?",
                    (device_id, registry_digest, now, MAX_SIGNAL_PEERS),
                ).fetchall()
                peers = []
                for row in peer_rows:
                    peer_ready = bool(
                        int(row["server_ready"]) == 1
                        and int(row["server_cursor"]) == current
                    )
                    peers.append({
                        "deviceId": row["device_id"],
                        "baselineReady": peer_ready,
                        "baselineLocalCursor": (
                            int(row["local_cursor"]) if peer_ready else None
                        ),
                    })

                signal_reset_required = False
                has_more = False
                incoming: list[dict] = []
                response_signal_cursor = signal_cursor
                expired_through = int(connection.execute(
                    "SELECT expired_through FROM direct_signal_state "
                    "WHERE singleton=1"
                ).fetchone()["expired_through"])
                if baseline_ready:
                    if signal_cursor > signal_head:
                        signal_reset_required = True
                        scan_cursor = signal_head
                    elif signal_cursor < expired_through:
                        signal_reset_required = True
                        scan_cursor = expired_through
                    else:
                        scan_cursor = signal_cursor
                    rows = connection.execute(
                        "SELECT s.id,s.from_device_id,s.session_id,s.kind,"
                        "s.payload_json,s.registry_digest AS signal_registry,"
                        "s.server_cursor AS signal_server_cursor,"
                        "p.registry_digest AS presence_registry,"
                        "p.server_cursor AS presence_server_cursor,"
                        "p.server_ready AS presence_server_ready,"
                        "p.expires_at AS presence_expires_at "
                        "FROM direct_signals AS s "
                        "LEFT JOIN direct_presence AS p "
                        "ON p.device_id=s.from_device_id "
                        "WHERE s.to_device_id=? AND s.id>? "
                        "AND s.expires_at>? "
                        "ORDER BY s.id LIMIT ?",
                        (
                            device_id,
                            scan_cursor,
                            now,
                            MAX_SIGNAL_RESULTS + 1,
                        ),
                    ).fetchall()
                    blocked_by_sender = False
                    scanned_rows = rows[:MAX_SIGNAL_RESULTS]
                    response_signal_cursor = scan_cursor
                    for row in scanned_rows:
                        row_cursor = int(row["id"])
                        if (
                            row["signal_registry"] != registry_digest
                            or int(row["signal_server_cursor"]) != current
                        ):
                            # A signal from an obsolete registry/server baseline
                            # can never become valid again.
                            response_signal_cursor = row_cursor
                            continue
                        sender_ready = bool(
                            row["presence_registry"] == registry_digest
                            and row["presence_server_ready"] is not None
                            and int(row["presence_server_ready"]) == 1
                            and int(row["presence_server_cursor"]) == current
                            and int(row["presence_expires_at"]) > now
                        )
                        if not sender_ready:
                            # Keep the cursor before this still-live message.
                            # The sender may refresh presence before signal TTL.
                            blocked_by_sender = True
                            break
                        incoming.append({
                            "id": int(row["id"]),
                            "fromDeviceId": row["from_device_id"],
                            "sessionId": row["session_id"],
                            "kind": row["kind"],
                            "payload": json.loads(row["payload_json"]),
                        })
                        response_signal_cursor = row_cursor
                    has_more = bool(
                        not blocked_by_sender
                        and len(rows) > MAX_SIGNAL_RESULTS
                    )
                    if not blocked_by_sender and not has_more:
                        # There is no remaining signal addressed to this
                        # receiver, so global cursor gaps belong to other
                        # devices and are safe to skip.
                        response_signal_cursor = signal_head
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _json_response({
            "ok": True,
            "contract": SIGNAL_CONTRACT,
            "accountProof": account_proof,
            "headCursor": current,
            "baselineReady": baseline_ready,
            "baselineLocalCursor": local_cursor if baseline_ready else None,
            "peers": peers,
            "ackedSignalIds": acknowledged,
            "signals": incoming,
            "signalCursor": response_signal_cursor,
            "signalResetRequired": signal_reset_required,
            "hasMore": has_more,
        })
    except RelayRequestError as exc:
        return _error(exc, contract=SIGNAL_CONTRACT)
    except (sqlite3.Error, OSError) as exc:
        current_app.logger.exception("reader direct signal relay failed")
        return _error(
            RelayRequestError(
                "直连信令暂时不可用",
                "BW_SYNC_RETRYABLE",
                503,
            ),
            contract=SIGNAL_CONTRACT,
        )


@bp.post("/exchange")
def exchange():
    try:
        body = _request_json()
        registry_digest = _require_sync_fence(body)
        identity, device_id, limit = _owner_and_common(body)
        owner_credentials = _business_owner_lease_credentials(body)
        direction = str(body.get("direction") or "")
        if direction not in {"push", "pull"}:
            raise RelayRequestError("direction 只允许 push 或 pull")
        cursor = _safe_integer(body.get("cursor", 0), "cursor")
        raw_changes = body.get("changes") or []
        if not isinstance(raw_changes, list):
            raise RelayRequestError("changes 必须是数组")
        if len(raw_changes) > MAX_CHANGES:
            raise RelayRequestError("changes 超过单批上限", "BW_SYNC_TOO_LARGE", 413)
        if direction == "pull" and raw_changes:
            raise RelayRequestError("pull 不接受 changes")
        changes = [
            _normalize_change(change, index)
            for index, change in enumerate(raw_changes)
        ]
        with _connection(identity["storage_namespace"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_owner_lease_locked(
                    connection,
                    owner_credentials,
                    now=int(time.time()),
                )
                causal_start_cursor = _activate_causal_epoch_locked(
                    connection,
                    registry_digest,
                )
                acknowledged: list[str] = []
                conflicts: list[dict] = []
                current, oldest = _state(connection)
                epoch_reset = cursor < causal_start_cursor
                if direction == "push" and not epoch_reset:
                    acknowledged, conflicts = _push_locked(
                        connection,
                        device_id=device_id,
                        changes=changes,
                    )
                    current, oldest = _state(connection)
                if epoch_reset or (
                    direction == "pull" and (
                        cursor > current or cursor < max(0, oldest - 1)
                    )
                ):
                    payload = {
                        "ok": True,
                        "contract": CONTRACT,
                        "cursor": cursor,
                        "headCursor": current,
                        "oldestCursor": oldest,
                        "hasMore": False,
                        "resetRequired": True,
                        "ackedMutationIds": [],
                        "changes": [],
                        "conflicts": [],
                    }
                elif direction == "pull":
                    rows = connection.execute(
                        "SELECT cursor,change_json FROM relay_events "
                        "WHERE cursor>? ORDER BY cursor LIMIT ?",
                        (cursor, limit),
                    ).fetchall()
                    outgoing = [json.loads(row["change_json"]) for row in rows]
                    page_cursor = int(rows[-1]["cursor"]) if rows else cursor
                    payload = {
                        "ok": True,
                        "contract": CONTRACT,
                        "cursor": page_cursor,
                        "headCursor": current,
                        "oldestCursor": oldest,
                        "hasMore": page_cursor < current,
                        "resetRequired": False,
                        "ackedMutationIds": [],
                        "changes": outgoing,
                        "conflicts": [],
                    }
                else:
                    payload = {
                        "ok": True,
                        "contract": CONTRACT,
                        "cursor": current,
                        "headCursor": current,
                        "oldestCursor": oldest,
                        "hasMore": False,
                        "resetRequired": False,
                        "ackedMutationIds": acknowledged,
                        "changes": [],
                        "conflicts": conflicts,
                    }
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _json_response(payload)
    except RelayRequestError as exc:
        return _error(exc)
    except (sqlite3.Error, OSError) as exc:
        current_app.logger.exception("reader sync relay exchange failed")
        return _error(RelayRequestError(
            "同步中继暂时不可用",
            "BW_SYNC_RETRYABLE",
            503,
        ))


@bp.post("/snapshot")
def snapshot():
    try:
        body = _request_json()
        registry_digest = _require_sync_fence(body)
        identity, _device_id, limit = _owner_and_common(body)
        owner_credentials = _business_owner_lease_credentials(body)
        snapshot_id = str(body.get("snapshotId") or "").strip()
        offset = _safe_integer(body.get("offset", 0), "offset")
        now = int(time.time())
        with _connection(identity["storage_namespace"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_owner_lease_locked(
                    connection,
                    owner_credentials,
                    now=now,
                )
                _activate_causal_epoch_locked(connection, registry_digest)
                connection.execute(
                    "DELETE FROM relay_snapshots WHERE expires_at<?",
                    (now,),
                )
                if not snapshot_id:
                    snapshot_id = "snap-" + secrets.token_urlsafe(24)
                    current, _oldest = _state(connection)
                    connection.execute(
                        "INSERT INTO relay_snapshots("
                        "snapshot_id,snapshot_cursor,expires_at"
                        ") VALUES(?,?,?)",
                        (snapshot_id, current, now + SNAPSHOT_TTL_SECONDS),
                    )
                    heads = connection.execute(
                        "SELECT change_json FROM relay_heads "
                        "ORDER BY collection,record_id"
                    ).fetchall()
                    connection.executemany(
                        "INSERT INTO relay_snapshot_items("
                        "snapshot_id,ordinal,change_json"
                        ") VALUES(?,?,?)",
                        [
                            (snapshot_id, ordinal, row["change_json"])
                            for ordinal, row in enumerate(heads)
                        ],
                    )
                row = connection.execute(
                    "SELECT snapshot_cursor,expires_at FROM relay_snapshots "
                    "WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                if not row or int(row["expires_at"]) < now:
                    raise RelayRequestError(
                        "snapshotId 无效或已过期",
                        "BW_SYNC_SNAPSHOT_EXPIRED",
                        409,
                    )
                total = int(connection.execute(
                    "SELECT COUNT(*) AS n FROM relay_snapshot_items "
                    "WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()["n"])
                items = connection.execute(
                    "SELECT ordinal,change_json FROM relay_snapshot_items "
                    "WHERE snapshot_id=? AND ordinal>=? "
                    "ORDER BY ordinal LIMIT ?",
                    (snapshot_id, offset, limit),
                ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        changes = [json.loads(item["change_json"]) for item in items]
        next_offset = offset + len(changes)
        return _json_response({
            "ok": True,
            "contract": CONTRACT,
            "snapshotId": snapshot_id,
            "snapshotCursor": int(row["snapshot_cursor"]),
            "offset": offset,
            "nextOffset": next_offset,
            "hasMore": next_offset < total,
            "changes": changes,
        })
    except RelayRequestError as exc:
        return _error(exc)
    except (sqlite3.Error, OSError) as exc:
        current_app.logger.exception("reader sync relay snapshot failed")
        return _error(RelayRequestError(
            "同步快照暂时不可用",
            "BW_SYNC_RETRYABLE",
            503,
        ))


def register_reader_sync_relay(app, *, root: str | Path | None = None):
    app.extensions["reader_sync_root"] = Path(
        root or app.extensions.get("reader_sync_root") or default_sync_root()
    ).resolve()
    app.register_blueprint(bp)


__all__ = [
    "CONTRACT",
    "SIGNAL_CONTRACT",
    "OWNER_LEASE_CONTRACT",
    "OWNER_LEASE_TTL_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_CHANGES",
    "MAX_LIMIT",
    "MAX_SIGNALS",
    "MAX_SIGNAL_PEERS",
    "MAX_SIGNAL_PAYLOAD_BYTES",
    "SIGNAL_PRESENCE_TTL_SECONDS",
    "SIGNAL_MESSAGE_TTL_SECONDS",
    "RelayRequestError",
    "default_sync_root",
    "register_reader_sync_relay",
]
