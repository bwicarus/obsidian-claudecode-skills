"""Authenticated, account-isolated realtime shared note for the Reader web app.

The note is deliberately not a Reader document annotation and is not part of
sync-v3.  It is a small server-authoritative Markdown document that browser
sessions and authenticated bridges can edit through one revisioned contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, render_template, request

from reader_sidecar_store import (
    NAMESPACE_RE,
    atomic_write_json,
    exclusive_lock,
)


CONTRACT = "reader-shared-note/1"
MAX_CONTENT_BYTES = 512 * 1024
MAX_RECEIPTS = 128
_UPDATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_OPERATIONS = frozenset({"replace", "append", "replace-text"})

INITIAL_UPDATE_ID = "seed-reader-context-injection-v1"
INITIAL_CONTENT = """# 阅读器上下文注入方案（讨论稿）

> 这份便签用于 Codex、Claude 与浏览器共同讨论，不替代
> `references/reader-collaboration-status.md`，也不是部署或完成状态的事实源。

- 普通网页默认只取当前可见内容、用户选区及焦点附近的最小相关 HTML/文本，不把整页无条件送入模型。
- PWA 真书由统一 `DocumentHost` 提供读取、跳转和定位能力；PDF/EPUB 私有 anchor 与几何仍留在各自宿主解释。
- 已选整张实体卡时，发送上下文只保留整卡一次，内部重复段落从本次模型输入中去重；本地缓存、卡片状态和唯一 ID 不删除。
- 工具结果优先保留可复用的查询履历与实体引用；大正文按需再由工具读取，避免每轮重复灌入。
- 后续仍需确定：可见范围边界、各层 token 预算、缓存前缀稳定策略，以及网页可点击元素的选择规则。
"""

bp = Blueprint("reader_shared_note", __name__)


class SharedNoteRequestError(ValueError):
    """A stable API error with an explicit machine-readable code."""

    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def default_shared_note_root() -> Path:
    base = Path(os.environ.get("WEBAPP_DATA", "/root/webapp/data"))
    return (base / "reader-shared-notes-v1").resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _identity() -> dict[str, Any]:
    resolver = current_app.extensions.get("reader_storage_identity_resolver")
    identity = resolver() if callable(resolver) else None
    if not isinstance(identity, dict):
        raise SharedNoteRequestError(
            "需要登录或有效 Bearer token",
            "BW_SHARED_NOTE_AUTH",
            401,
        )
    user_id = identity.get("user_id")
    namespace = str(identity.get("storage_namespace") or "")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not NAMESPACE_RE.fullmatch(namespace)
    ):
        raise SharedNoteRequestError(
            "认证身份无效",
            "BW_SHARED_NOTE_AUTH",
            401,
        )
    return {"user_id": user_id, "storage_namespace": namespace}


def _paths(identity: dict[str, Any]) -> tuple[Path, Path]:
    root = Path(
        current_app.extensions.get("reader_shared_note_root")
        or default_shared_note_root()
    ).resolve()
    account_key = hashlib.sha256(
        identity["storage_namespace"].encode("ascii")
    ).hexdigest()
    account_root = root / "by-account" / account_key
    return account_root / "shared-note.json", account_root / ".shared-note.lock"


def _default_state(now: str) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "revision": 1,
        "updatedAt": now,
        "source": "system-context-draft",
        "updateId": INITIAL_UPDATE_ID,
        "content": INITIAL_CONTENT,
        "receipts": [],
    }


def _valid_text(value: Any, *, name: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "非空字符串" if not allow_empty else "字符串"
        raise SharedNoteRequestError(
            f"{name} 必须是{suffix}",
            "BW_SHARED_NOTE_INVALID",
        )
    return value


def _valid_source(value: Any) -> str:
    source = _valid_text(value, name="source", allow_empty=False).strip()
    if (
        not source
        or len(source) > 80
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in source)
    ):
        raise SharedNoteRequestError(
            "source 必须是 1–80 字符的可显示文本",
            "BW_SHARED_NOTE_INVALID",
        )
    return source


def _valid_update_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _UPDATE_ID_RE.fullmatch(value):
        raise SharedNoteRequestError(
            "updateId 只能包含字母、数字、点、下划线、冒号或连字符，最长 128",
            "BW_SHARED_NOTE_INVALID",
        )
    return value


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise RuntimeError("shared note state contract mismatch")
    revision = value.get("revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(value.get("updatedAt"), str)
        or not isinstance(value.get("source"), str)
        or not isinstance(value.get("content"), str)
        or not isinstance(value.get("receipts"), list)
    ):
        raise RuntimeError("shared note state is invalid")
    if len(value["content"].encode("utf-8")) > MAX_CONTENT_BYTES:
        raise RuntimeError("shared note state exceeds content limit")
    for receipt in value["receipts"]:
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("updateId"), str)
            or not isinstance(receipt.get("requestHash"), str)
            or not isinstance(receipt.get("revision"), int)
            or not isinstance(receipt.get("updatedAt"), str)
        ):
            raise RuntimeError("shared note receipt ledger is invalid")
    return value


def _load_or_create(state_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(state_path.read_text("utf-8"))
    except FileNotFoundError:
        value = _default_state(_utc_now())
        atomic_write_json(state_path, value)
    return _validate_state(value)


def _public_note(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "revision": state["revision"],
        "updatedAt": state["updatedAt"],
        "source": state["source"],
        "updateId": state.get("updateId"),
        "content": state["content"],
    }


def _error(
    exc: SharedNoteRequestError,
    *,
    state: dict[str, Any] | None = None,
):
    payload: dict[str, Any] = {
        "ok": False,
        "contract": CONTRACT,
        "error": {"code": exc.code, "message": str(exc)},
    }
    if state is not None:
        payload["note"] = _public_note(state)
    response = jsonify(payload)
    response.status_code = exc.status
    response.headers["Cache-Control"] = "no-store"
    return response


def _request_json() -> dict[str, Any]:
    content_length = request.content_length
    if content_length is not None and content_length > MAX_CONTENT_BYTES + 8192:
        raise SharedNoteRequestError(
            "请求过大",
            "BW_SHARED_NOTE_TOO_LARGE",
            413,
        )
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise SharedNoteRequestError(
            "请求必须是 JSON 对象",
            "BW_SHARED_NOTE_INVALID",
        )
    if value.get("contract") != CONTRACT:
        raise SharedNoteRequestError(
            "共享便签合同不匹配",
            "BW_SHARED_NOTE_CONTRACT",
        )
    operation = value.get("operation")
    if operation not in _OPERATIONS:
        raise SharedNoteRequestError(
            "operation 必须是 replace、append 或 replace-text",
            "BW_SHARED_NOTE_INVALID",
        )
    base_revision = value.get("baseRevision")
    if (
        isinstance(base_revision, bool)
        or not isinstance(base_revision, int)
        or base_revision < 1
    ):
        raise SharedNoteRequestError(
            "baseRevision 必须是正整数",
            "BW_SHARED_NOTE_INVALID",
        )
    common = {
        "contract",
        "operation",
        "baseRevision",
        "source",
        "updateId",
    }
    operation_fields = {
        "replace": {"content"},
        "append": {"text"},
        "replace-text": {"oldText", "newText"},
    }[operation]
    unknown = set(value) - common - operation_fields
    missing = operation_fields - set(value)
    if unknown or missing:
        detail = []
        if unknown:
            detail.append("未知字段: " + ", ".join(sorted(unknown)))
        if missing:
            detail.append("缺少字段: " + ", ".join(sorted(missing)))
        raise SharedNoteRequestError(
            "；".join(detail),
            "BW_SHARED_NOTE_INVALID",
        )
    normalized: dict[str, Any] = {
        "contract": CONTRACT,
        "operation": operation,
        "baseRevision": base_revision,
        "source": _valid_source(value.get("source")),
        "updateId": _valid_update_id(value.get("updateId")),
    }
    if operation == "replace":
        normalized["content"] = _valid_text(value.get("content"), name="content")
    elif operation == "append":
        normalized["text"] = _valid_text(
            value.get("text"),
            name="text",
            allow_empty=False,
        )
    else:
        normalized["oldText"] = _valid_text(
            value.get("oldText"),
            name="oldText",
            allow_empty=False,
        )
        normalized["newText"] = _valid_text(value.get("newText"), name="newText")
    return normalized


def _apply_operation(state: dict[str, Any], mutation: dict[str, Any]) -> str:
    content = state["content"]
    operation = mutation["operation"]
    if operation == "replace":
        result = mutation["content"]
    elif operation == "append":
        result = content + mutation["text"]
    else:
        old_text = mutation["oldText"]
        matches = content.count(old_text)
        if matches == 0:
            raise SharedNoteRequestError(
                "oldText 在当前权威正文中不存在",
                "BW_SHARED_NOTE_TARGET_MISSING",
                409,
            )
        if matches != 1:
            raise SharedNoteRequestError(
                "oldText 在当前权威正文中不唯一，拒绝猜测替换位置",
                "BW_SHARED_NOTE_TARGET_NOT_UNIQUE",
                409,
            )
        result = content.replace(old_text, mutation["newText"], 1)
    if len(result.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise SharedNoteRequestError(
            "共享便签正文超过 512 KiB",
            "BW_SHARED_NOTE_TOO_LARGE",
            413,
        )
    return result


def _publish_change() -> int:
    publisher: Callable[..., Any] | None = current_app.extensions.get(
        "reader_shared_note_publish"
    )
    if publisher is None:
        from reader_events import publish as publisher
    # Existing reader-events is a broadcast notification bus.  Never place
    # note content, revision, source, updateId, account id or namespace in the event.
    # Authenticated clients refetch their own account-isolated authoritative
    # state when they see this generic invalidation.
    try:
        delivered = publisher("shared-note", "", None)
        return max(0, int(delivered or 0))
    except Exception:
        current_app.logger.exception("shared note realtime notification failed")
        return 0


@bp.route("/pdf/shared-note", methods=["GET"], strict_slashes=False)
def shared_note_page():
    try:
        _identity()
    except SharedNoteRequestError as exc:
        return _error(exc)
    response = current_app.make_response(render_template("shared_note.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/pdf/api/shared-note", methods=["GET"])
def shared_note_get():
    try:
        identity = _identity()
        state_path, lock_path = _paths(identity)
        with exclusive_lock(lock_path):
            state = _load_or_create(state_path)
        response = jsonify({
            "ok": True,
            "contract": CONTRACT,
            "note": _public_note(state),
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except SharedNoteRequestError as exc:
        return _error(exc)
    except (OSError, RuntimeError, json.JSONDecodeError):
        current_app.logger.exception("shared note read failed")
        return _error(SharedNoteRequestError(
            "共享便签存储暂时不可用",
            "BW_SHARED_NOTE_STORAGE",
            503,
        ))


@bp.route("/pdf/api/shared-note", methods=["POST"])
def shared_note_post():
    state: dict[str, Any] | None = None
    try:
        identity = _identity()
        mutation = _request_json()
        request_hash = hashlib.sha256(
            _canonical_json(mutation).encode("utf-8")
        ).hexdigest()
        state_path, lock_path = _paths(identity)
        with exclusive_lock(lock_path):
            state = _load_or_create(state_path)
            update_id = mutation.get("updateId")
            if update_id:
                previous = next(
                    (
                        item for item in reversed(state["receipts"])
                        if item["updateId"] == update_id
                    ),
                    None,
                )
                if previous:
                    if previous["requestHash"] != request_hash:
                        raise SharedNoteRequestError(
                            "updateId 已被另一份写入内容使用",
                            "BW_SHARED_NOTE_UPDATE_ID_REUSE",
                            409,
                        )
                    response = jsonify({
                        "ok": True,
                        "contract": CONTRACT,
                        "result": "idempotent-replay",
                        "revision": previous["revision"],
                        "updatedAt": previous["updatedAt"],
                        "source": previous["source"],
                        "updateId": update_id,
                        "liveNotified": False,
                    })
                    response.headers["Cache-Control"] = "no-store"
                    return response
            if mutation["baseRevision"] != state["revision"]:
                raise SharedNoteRequestError(
                    "baseRevision 已过期，请读取当前正文后明确合并或重试",
                    "BW_SHARED_NOTE_REVISION_CONFLICT",
                    409,
                )
            content = _apply_operation(state, mutation)
            updated_at = _utc_now()
            state.update({
                "revision": state["revision"] + 1,
                "updatedAt": updated_at,
                "source": mutation["source"],
                "updateId": mutation.get("updateId"),
                "content": content,
            })
            if mutation.get("updateId"):
                state["receipts"].append({
                    "updateId": mutation["updateId"],
                    "requestHash": request_hash,
                    "revision": state["revision"],
                    "updatedAt": updated_at,
                    "source": mutation["source"],
                })
                state["receipts"] = state["receipts"][-MAX_RECEIPTS:]
            atomic_write_json(state_path, state)
        delivered = _publish_change()
        response = jsonify({
            "ok": True,
            "contract": CONTRACT,
            "result": "updated",
            "revision": state["revision"],
            "updatedAt": state["updatedAt"],
            "source": state["source"],
            "updateId": state.get("updateId"),
            "liveNotified": delivered > 0,
            "liveSubscriberCount": delivered,
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except SharedNoteRequestError as exc:
        return _error(exc, state=state if exc.status == 409 else None)
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError):
        current_app.logger.exception("shared note write failed")
        return _error(SharedNoteRequestError(
            "共享便签存储暂时不可用",
            "BW_SHARED_NOTE_STORAGE",
            503,
        ))


def register_shared_note(
    app,
    *,
    root: str | Path | None = None,
    publish_fn: Callable[..., Any] | None = None,
):
    app.extensions["reader_shared_note_root"] = Path(
        root
        or app.extensions.get("reader_shared_note_root")
        or default_shared_note_root()
    ).resolve()
    if publish_fn is not None:
        app.extensions["reader_shared_note_publish"] = publish_fn
    app.register_blueprint(bp)


__all__ = [
    "CONTRACT",
    "INITIAL_CONTENT",
    "INITIAL_UPDATE_ID",
    "MAX_CONTENT_BYTES",
    "SharedNoteRequestError",
    "default_shared_note_root",
    "register_shared_note",
]
