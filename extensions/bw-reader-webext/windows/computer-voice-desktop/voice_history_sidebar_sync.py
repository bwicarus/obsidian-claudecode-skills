#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archive the active local Codex Voice thread and optionally mirror turns.

The two Codex files are treated as a small, untrusted shared-memory surface:

* ``.codex-global-state.json`` selects one exact local UUID.
* ``realtime-voice-continuity.json`` supplies the recent window for that UUID.

The recent window is appended with longest suffix/prefix overlap.  A window
with no overlap is kept, but an explicit gap boundary prevents a user message
on one side from being paired with an assistant message on the other.

Publishing is opt-in.  Importing this module and the default CLI invocation do
not import ``bridge_client`` or make a network call.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


VERSION = 1
RECENT_THREAD_KEY = "realtime-voice-most-recent-thread"

MAX_GLOBAL_STATE_BYTES = 1024 * 1024
MAX_CONTINUITY_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_THREADS = 128
MAX_RECENT_ITEMS = 20
MAX_ARCHIVE_ITEMS = 10_000
MAX_TEXT_CHARS = 4_000
MAX_PUBLISHED = MAX_ARCHIVE_ITEMS // 2
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
SNAPSHOT_ANCHOR_MAX_AGE = timedelta(minutes=3)
HISTORY_POLL_SECONDS = 0.75
FINAL_TAIL_POLLS = 3
OFFLINE_EXIT_POLLS = 40
PUBLISH_FAILURE_BACKOFF_POLLS = 20
ERROR_ALREADY_EXISTS = 183
READER_SYNC_MARKERS = ("[[READER_SYNC]]", "[[/READER_SYNC]]")

Publisher = Callable[..., dict[str, Any]]
StatusProvider = Callable[[], Any]


class SyncDataError(ValueError):
    """A local input or durable state violated its bounded schema."""


def _contains_reader_sync_marker(text: str) -> bool:
    return any(marker in text for marker in READER_SYNC_MARKERS)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SyncDataError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise SyncDataError(f"non-finite JSON number: {value}")


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except SyncDataError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncDataError(f"{label}: invalid UTF-8 JSON") from exc


def _read_bounded_json(path: Path, limit: int, label: str) -> Any:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise SyncDataError(f"{label}: unreadable") from exc
    if len(raw) > limit:
        raise SyncDataError(f"{label}: exceeds {limit} bytes")
    return _decode_json(raw, label)


def _exact_fields(
    value: dict[str, Any],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise SyncDataError(
            f"{label}: schema mismatch"
            f" missing={sorted(missing)} extra={sorted(extra)}"
        )


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SyncDataError(f"{label}: UUID must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SyncDataError(f"{label}: invalid UUID") from exc
    canonical = str(parsed)
    if parsed.int == 0 or value != canonical:
        raise SyncDataError(f"{label}: UUID must be canonical and non-zero")
    return canonical


def _normalize_item(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SyncDataError(f"{label}: item must be an object")
    _exact_fields(value, {"role", "text"}, set(), label)
    role = value["role"]
    text = value["text"]
    if role not in {"user", "assistant"}:
        raise SyncDataError(f"{label}: unsupported role")
    if not isinstance(text, str):
        raise SyncDataError(f"{label}: text must be a string")
    text = text.strip()
    if not text or len(text) > MAX_TEXT_CHARS or "\x00" in text:
        raise SyncDataError(f"{label}: invalid text")
    if _contains_reader_sync_marker(text):
        raise SyncDataError(f"{label}: Reader sync payload is forbidden")
    return {"role": role, "text": text}


def _parse_binding(root: Any) -> tuple[str, dict[str, str] | None]:
    """Return ``bound``, ``unbound``, or raise for a malformed binding.

    Top-level Electron state is intentionally extensible.  Only the selected
    atom has an exact schema.  ``version`` is tolerated because current Codex
    builds may include it, but selection never depends on it.
    """
    if not isinstance(root, dict):
        raise SyncDataError("global-state: root must be an object")
    persisted = root.get("electron-persisted-atom-state")
    if persisted is None:
        return "unbound", None
    if not isinstance(persisted, dict):
        raise SyncDataError("global-state: persisted atom must be an object")
    recent = persisted.get(RECENT_THREAD_KEY)
    if recent is None:
        return "unbound", None
    if not isinstance(recent, dict):
        raise SyncDataError("global-state: recent thread must be an object")
    _exact_fields(
        recent,
        {"conversationId", "hostId"},
        {"version"},
        "global-state.recent-thread",
    )
    if "version" in recent and (
        isinstance(recent["version"], bool)
        or not isinstance(recent["version"], int)
        or recent["version"] < 0
    ):
        raise SyncDataError("global-state: invalid binding version")
    host_id = recent["hostId"]
    if not isinstance(host_id, str):
        raise SyncDataError("global-state: hostId must be a string")
    thread_id = _canonical_uuid(
        recent["conversationId"], "global-state.conversationId"
    )
    if host_id != "local":
        return "unbound", None
    return "bound", {"conversationId": thread_id, "hostId": "local"}


def _parse_continuity(root: Any, selected_thread: str) -> list[dict[str, str]]:
    if not isinstance(root, dict):
        raise SyncDataError("continuity: root must be an object")
    _exact_fields(root, {"version", "threads"}, set(), "continuity")
    if (
        isinstance(root["version"], bool)
        or not isinstance(root["version"], int)
        or root["version"] != 1
    ):
        raise SyncDataError("continuity: version must be 1")
    threads = root["threads"]
    if not isinstance(threads, dict) or len(threads) > MAX_THREADS:
        raise SyncDataError("continuity: invalid threads object")

    selected: list[dict[str, str]] = []
    for raw_thread_id, thread in threads.items():
        thread_id = _canonical_uuid(raw_thread_id, "continuity.threadId")
        if not isinstance(thread, dict):
            raise SyncDataError(f"continuity.threads.{thread_id}: not an object")
        _exact_fields(
            thread,
            {"items"},
            set(),
            f"continuity.threads.{thread_id}",
        )
        items = thread["items"]
        if not isinstance(items, list) or len(items) > MAX_RECENT_ITEMS:
            raise SyncDataError(
                f"continuity.threads.{thread_id}: invalid items"
            )
        normalized = [
            _normalize_item(item, f"continuity.threads.{thread_id}.items[{i}]")
            for i, item in enumerate(items)
        ]
        if thread_id == selected_thread:
            selected = normalized
    return selected


def _default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "lastGood": None,
        "published": {},
    }


def _default_archive() -> dict[str, Any]:
    return {"version": VERSION, "threads": {}}


def _validate_last_good(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SyncDataError("state.lastGood: must be null or an object")
    _exact_fields(
        value,
        {"binding", "threadId", "items"},
        set(),
        "state.lastGood",
    )
    binding = value["binding"]
    if not isinstance(binding, dict):
        raise SyncDataError("state.lastGood.binding: must be an object")
    _exact_fields(
        binding,
        {"conversationId", "hostId"},
        set(),
        "state.lastGood.binding",
    )
    thread_id = _canonical_uuid(value["threadId"], "state.lastGood.threadId")
    if (
        binding["conversationId"] != thread_id
        or binding["hostId"] != "local"
    ):
        raise SyncDataError("state.lastGood: binding mismatch")
    items = value["items"]
    if not isinstance(items, list) or len(items) > MAX_RECENT_ITEMS:
        raise SyncDataError("state.lastGood.items: invalid list")
    normalized = [
        _normalize_item(item, f"state.lastGood.items[{i}]")
        for i, item in enumerate(items)
    ]
    return {
        "binding": {"conversationId": thread_id, "hostId": "local"},
        "threadId": thread_id,
        "items": normalized,
    }


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SyncDataError("state: root must be an object")
    _exact_fields(value, {"version", "lastGood", "published"}, set(), "state")
    if value["version"] != VERSION:
        raise SyncDataError("state: unsupported version")
    published = value["published"]
    if not isinstance(published, dict) or len(published) > MAX_PUBLISHED:
        raise SyncDataError("state.published: invalid object")
    clean_published: dict[str, bool] = {}
    for request_id, marker in published.items():
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 64
            or marker is not True
        ):
            raise SyncDataError("state.published: invalid entry")
        clean_published[request_id] = True
    return {
        "version": VERSION,
        "lastGood": _validate_last_good(value["lastGood"]),
        "published": clean_published,
    }


def _validate_archive(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SyncDataError("archive: root must be an object")
    _exact_fields(value, {"version", "threads"}, set(), "archive")
    if value["version"] != VERSION:
        raise SyncDataError("archive: unsupported version")
    threads = value["threads"]
    if not isinstance(threads, dict) or len(threads) > MAX_THREADS:
        raise SyncDataError("archive.threads: invalid object")

    total_items = 0
    clean_threads: dict[str, Any] = {}
    for raw_thread_id, thread in threads.items():
        thread_id = _canonical_uuid(raw_thread_id, "archive.threadId")
        if not isinstance(thread, dict):
            raise SyncDataError(f"archive.threads.{thread_id}: not an object")
        _exact_fields(
            thread, {"items", "gaps"}, set(), f"archive.threads.{thread_id}"
        )
        items = thread["items"]
        if not isinstance(items, list):
            raise SyncDataError(f"archive.threads.{thread_id}.items: invalid")
        total_items += len(items)
        if total_items > MAX_ARCHIVE_ITEMS:
            raise SyncDataError("archive: too many items")
        clean_items = [
            _normalize_item(item, f"archive.threads.{thread_id}.items[{i}]")
            for i, item in enumerate(items)
        ]
        gaps = thread["gaps"]
        if not isinstance(gaps, list):
            raise SyncDataError(f"archive.threads.{thread_id}.gaps: invalid")
        clean_gaps: list[int] = []
        prior = 0
        for gap in gaps:
            if (
                isinstance(gap, bool)
                or not isinstance(gap, int)
                or gap <= 0
                or gap >= len(clean_items)
                or gap <= prior
            ):
                raise SyncDataError(
                    f"archive.threads.{thread_id}.gaps: invalid boundary"
                )
            clean_gaps.append(gap)
            prior = gap
        clean_threads[thread_id] = {
            "items": clean_items,
            "gaps": clean_gaps,
        }
    return {"version": VERSION, "threads": clean_threads}


def _load_durable(
    path: Path,
    limit: int,
    label: str,
    default: dict[str, Any],
    validator: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if not path.exists():
        return default
    return validator(_read_bounded_json(path, limit, label))


def _atomic_write_json(path: Path, value: dict[str, Any], limit: int) -> None:
    raw = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(raw) > limit:
        raise SyncDataError(f"{path.name}: serialized data exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _overlap(
    archived: list[dict[str, str]], recent: list[dict[str, str]]
) -> int:
    for size in range(min(len(archived), len(recent)), 0, -1):
        if archived[-size:] == recent[:size]:
            return size
    return 0


def _merge_recent(
    thread: dict[str, Any], recent: list[dict[str, str]]
) -> bool:
    archived = thread["items"]
    if not recent:
        return False
    if not archived:
        archived.extend(recent)
        return False
    overlap = _overlap(archived, recent)
    if overlap == 0:
        thread["gaps"].append(len(archived))
    archived.extend(recent[overlap:])
    if len(archived) > MAX_ARCHIVE_ITEMS:
        raise SyncDataError("archive: item limit reached")
    return overlap == 0


def _request_id(
    thread_id: str, assistant_index: int, user: str, assistant: str
) -> str:
    digest = hashlib.sha256(
        (user + "\x00" + assistant).encode("utf-8")
    ).hexdigest()[:12]
    request_id = f"vh:{thread_id}:{assistant_index}:{digest}"
    if len(request_id) > 64:
        raise SyncDataError("internal request_id exceeds bridge limit")
    return request_id


def _pairs(thread_id: str, thread: dict[str, Any]) -> list[dict[str, Any]]:
    items = thread["items"]
    boundaries = set(thread["gaps"])
    pairs: list[dict[str, Any]] = []
    for index in range(1, len(items)):
        if index in boundaries:
            continue
        user = items[index - 1]
        assistant = items[index]
        if user["role"] != "user" or assistant["role"] != "assistant":
            continue
        pairs.append(
            {
                "requestId": _request_id(
                    thread_id, index, user["text"], assistant["text"]
                ),
                "assistantIndex": index,
                "user": user["text"],
                "assistant": assistant["text"],
            }
        )
    return pairs


def _status(
    *,
    thread_id: str | None,
    items: int = 0,
    pairs: int = 0,
    gap: bool = False,
    pending: int = 0,
    published: int = 0,
    dry_run: bool = True,
    stale: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "threadId": thread_id,
        "items": items,
        "pairs": pairs,
        "gap": gap,
        "pending": pending,
        "published": published,
        "dryRun": dry_run,
        "stale": stale,
    }
    if error:
        out["error"] = error
    return out


def sync_once(
    *,
    global_state_path: Path,
    continuity_path: Path,
    state_path: Path,
    archive_path: Path,
    publish: bool = False,
    publisher: Publisher | None = None,
    file: str | None = None,
    page: int | str | None = None,
    source_instance_id: str | None = None,
    snapshot_revision: int | None = None,
    expected_thread_id: str | None = None,
    minimum_assistant_index: int = 1,
    allow_last_good: bool = True,
) -> dict[str, Any]:
    """Perform one bounded poll/archive/publish cycle.

    ``publisher`` has the same keyword contract as ``bridge_client.call`` and
    is injected by tests.  A false ``ok`` result or exception stops ordered
    publication; it never advances durable published state.  Capture-bound
    callers additionally pin ``expected_thread_id`` and set a per-call item
    watermark through ``minimum_assistant_index``; standalone archival keeps
    the historical defaults.
    """
    if (
        isinstance(minimum_assistant_index, bool)
        or not isinstance(minimum_assistant_index, int)
        or minimum_assistant_index < 1
    ):
        raise ValueError("minimum_assistant_index must be a positive integer")
    if expected_thread_id is not None:
        expected_thread_id = _canonical_uuid(
            expected_thread_id, "expected_thread_id"
        )
    try:
        state = _load_durable(
            state_path,
            MAX_STATE_BYTES,
            "state",
            _default_state(),
            _validate_state,
        )
        archive = _load_durable(
            archive_path,
            MAX_ARCHIVE_BYTES,
            "archive",
            _default_archive(),
            _validate_archive,
        )
    except SyncDataError as exc:
        return _status(
            thread_id=None,
            dry_run=not publish,
            gap=True,
            stale=True,
            error=str(exc),
        )

    stale = False
    errors: list[str] = []
    binding: dict[str, str] | None
    try:
        binding_status, binding = _parse_binding(
            _read_bounded_json(
                global_state_path,
                MAX_GLOBAL_STATE_BYTES,
                "global-state",
            )
        )
    except SyncDataError as exc:
        if not allow_last_good:
            return _status(
                thread_id=None,
                dry_run=not publish,
                gap=True,
                stale=True,
                error=str(exc),
            )
        last_good = state["lastGood"]
        if last_good is None:
            return _status(
                thread_id=None,
                dry_run=not publish,
                gap=True,
                stale=True,
                error=str(exc),
            )
        binding_status = "bound"
        binding = dict(last_good["binding"])
        stale = True
        errors.append(str(exc))

    if binding_status == "unbound" or binding is None:
        return _status(thread_id=None, dry_run=not publish)

    thread_id = binding["conversationId"]
    if expected_thread_id is not None and thread_id != expected_thread_id:
        return _status(
            thread_id=expected_thread_id,
            dry_run=not publish,
            gap=True,
            stale=True,
            error="active capture thread lease changed",
        )
    try:
        recent = _parse_continuity(
            _read_bounded_json(
                continuity_path,
                MAX_CONTINUITY_BYTES,
                "continuity",
            ),
            thread_id,
        )
        state["lastGood"] = {
            "binding": binding,
            "threadId": thread_id,
            "items": recent,
        }
    except SyncDataError as exc:
        if not allow_last_good:
            return _status(
                thread_id=thread_id,
                dry_run=not publish,
                gap=True,
                stale=True,
                error=str(exc),
            )
        last_good = state["lastGood"]
        if last_good is not None and last_good["threadId"] == thread_id:
            recent = list(last_good["items"])
        else:
            recent = []
        stale = True
        errors.append(str(exc))

    thread = archive["threads"].setdefault(
        thread_id, {"items": [], "gaps": []}
    )
    try:
        new_gap = _merge_recent(thread, recent)
        if sum(
            len(entry["items"]) for entry in archive["threads"].values()
        ) > MAX_ARCHIVE_ITEMS:
            raise SyncDataError("archive: total item limit reached")
        _atomic_write_json(archive_path, archive, MAX_ARCHIVE_BYTES)
        _atomic_write_json(state_path, state, MAX_STATE_BYTES)
    except (OSError, SyncDataError) as exc:
        return _status(
            thread_id=thread_id,
            items=len(thread["items"]),
            gap=True,
            dry_run=not publish,
            stale=True,
            error=f"durable-write: {exc}",
        )

    pairs = _pairs(thread_id, thread)
    pending = [
        pair
        for pair in pairs
        if (
            pair["assistantIndex"] >= minimum_assistant_index
            and pair["requestId"] not in state["published"]
        )
    ]
    published_count = 0
    publish_error: str | None = None
    if publish and pending:
        if publisher is None:
            publish_error = "publisher unavailable"
        else:
            for pair in pending:
                payload = {
                    "text": pair["assistant"],
                    "user_utterance": pair["user"],
                }
                try:
                    publish_kwargs: dict[str, Any] = {
                        "request_id": pair["requestId"],
                        "file": file,
                        "page": page,
                    }
                    if source_instance_id is not None:
                        publish_kwargs["source_instance_id"] = source_instance_id
                        publish_kwargs["thread_id"] = thread_id
                    if snapshot_revision is not None:
                        publish_kwargs["snapshot_revision"] = snapshot_revision
                    result = publisher(
                        "assistant_turn",
                        payload,
                        **publish_kwargs,
                    )
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        publish_error = "publisher rejected assistant_turn"
                        break
                except Exception as exc:  # publisher defines its own error type
                    publish_error = f"publisher failed: {type(exc).__name__}"
                    break
                state["published"][pair["requestId"]] = True
                published_count += 1
                try:
                    _atomic_write_json(state_path, state, MAX_STATE_BYTES)
                except (OSError, SyncDataError) as exc:
                    publish_error = f"state-write-after-publish: {exc}"
                    break

    remaining = sum(
        pair["assistantIndex"] >= minimum_assistant_index
        and pair["requestId"] not in state["published"]
        for pair in pairs
    )
    all_errors = errors + ([publish_error] if publish_error else [])
    return _status(
        thread_id=thread_id,
        items=len(thread["items"]),
        pairs=len(pairs),
        gap=bool(thread["gaps"]) or new_gap or stale,
        pending=remaining,
        published=published_count,
        dry_run=not publish,
        stale=stale,
        error="; ".join(all_errors) if all_errors else None,
    )


def _default_paths() -> tuple[Path, Path, Path, Path]:
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    codex = profile / ".codex"
    return (
        codex / ".codex-global-state.json",
        codex / "realtime-voice-continuity.json",
        codex / "voice-history-sidebar-sync-state.json",
        codex / "voice-history-sidebar-sync-archive.json",
    )


def _bridge_publisher(*args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        from sidebar_bridge_client import call
    except ImportError:
        # The standalone scripts/ wrapper remains useful in source checkouts.
        from bridge_client import call

    return call(*args, **kwargs)


def snapshot_anchor(
    snapshot_path: Path,
    *,
    now: datetime | None = None,
) -> tuple[str | None, int | None]:
    """Read only a safe current-page anchor from the Windows snapshot.

    Failure is intentionally non-fatal for archival-only callers. No text,
    drawing, selection, or other snapshot content is forwarded through this
    path.
    """
    try:
        root = _read_bounded_json(
            snapshot_path,
            MAX_SNAPSHOT_BYTES,
            "reader-snapshot",
        )
    except SyncDataError:
        return None, None
    if (
        not isinstance(root, dict)
        or root.get("schema") != "reader-context-snapshot/1"
        or root.get("contextStatus") != "ready"
    ):
        return None, None
    raw_updated = root.get("updatedAtUtc")
    if not isinstance(raw_updated, str):
        return None, None
    try:
        updated = datetime.fromisoformat(raw_updated)
    except ValueError:
        return None, None
    if updated.tzinfo is None:
        return None, None
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        return None, None
    age = checked_at.astimezone(timezone.utc) - updated.astimezone(timezone.utc)
    if age < timedelta(0) or age > SNAPSHOT_ANCHOR_MAX_AGE:
        return None, None
    active = root.get("activeReading")
    if (
        not isinstance(active, dict)
        or active.get("fresh") is not True
        or isinstance(active.get("ageSec"), bool)
        or not isinstance(active.get("ageSec"), (int, float))
        or active["ageSec"] < 0
        or active["ageSec"] > SNAPSHOT_ANCHOR_MAX_AGE.total_seconds()
    ):
        return None, None
    current = root.get("currentPage")
    if (
        not isinstance(current, dict)
        or current.get("stable") is not True
    ):
        return None, None
    file = current.get("file")
    page = current.get("page")
    if (
        not isinstance(file, str)
        or not file
        or len(file) > 1024
        or file.startswith(("/", "\\"))
        or "\x00" in file
        or ":" in file
        or ".." in file.replace("\\", "/").split("/")
        or isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
    ):
        return None, None
    return file, page


def snapshot_output_identity(
    snapshot_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the exact online Reader source used by the local WSS router.

    Unlike the historical Pi anchor, ``file`` may be an HTTPS URL and page 0
    is valid for an ordinary browser tab.  No page text or drawing data leaves
    the snapshot file through this helper.
    """
    try:
        root = _read_bounded_json(
            snapshot_path,
            MAX_SNAPSHOT_BYTES,
            "reader-snapshot",
        )
    except SyncDataError:
        return None
    if (
        not isinstance(root, dict)
        or root.get("schema") != "reader-context-snapshot/1"
        or root.get("contextStatus") != "ready"
    ):
        return None
    raw_updated = root.get("updatedAtUtc")
    if not isinstance(raw_updated, str):
        return None
    try:
        updated = datetime.fromisoformat(raw_updated)
    except ValueError:
        return None
    checked_at = now or datetime.now(timezone.utc)
    if updated.tzinfo is None or checked_at.tzinfo is None:
        return None
    age = checked_at.astimezone(timezone.utc) - updated.astimezone(timezone.utc)
    if age < timedelta(0) or age > SNAPSHOT_ANCHOR_MAX_AGE:
        return None
    active = root.get("activeReading")
    current = root.get("currentPage")
    revision = root.get("revision")
    if (
        not isinstance(active, dict)
        or active.get("fresh") is not True
        or not isinstance(current, dict)
        or current.get("stable") is not True
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        return None
    source = current.get("sourceInstanceId")
    active_source = active.get("sourceInstanceId")
    file = current.get("file")
    page = current.get("page")
    if (
        not isinstance(source, str)
        or source != active_source
        or not 1 <= len(source) <= 160
        or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in source)
        or not isinstance(file, str)
        or not file
        or len(file) > 4096
        or "\x00" in file
        or isinstance(page, bool)
        or not (
            isinstance(page, int) and page >= 0
            or isinstance(page, str) and 1 <= len(page) <= 256 and "\x00" not in page
        )
    ):
        return None
    return {
        "source_instance_id": source,
        "snapshot_revision": revision,
        "file": file,
        "page": page,
    }


class CaptureBoundHistorySynchronizer:
    """Publish chat pairs only while an owned voice capture is active.

    ``requestId`` generation and durable dedupe remain inside ``sync_once``.
    Three trailing polls after capture closes catch continuity-file writes
    that land just after the native status transition.
    """

    def __init__(
        self,
        *,
        root: Path,
        publisher: Publisher = _bridge_publisher,
        global_state_path: Path | None = None,
        continuity_path: Path | None = None,
        state_path: Path | None = None,
        archive_path: Path | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        defaults = _default_paths()
        self.global_state_path = global_state_path or defaults[0]
        self.continuity_path = continuity_path or defaults[1]
        self.state_path = state_path or defaults[2]
        self.archive_path = archive_path or defaults[3]
        self.snapshot_path = (
            snapshot_path
            or root / "runtime" / "reader-context-snapshot.json"
        )
        self.publisher = publisher
        self.was_active = False
        self.tail_polls = 0
        self._lease_thread_id: str | None = None
        self._minimum_assistant_index: int | None = None
        self._lease_source_instance_id: str | None = None
        self._failure_backoff_polls = 0
        self.last_result: dict[str, Any] | None = None

    def _release_lease(self) -> None:
        self._lease_thread_id = None
        self._minimum_assistant_index = None
        self._lease_source_instance_id = None
        self._failure_backoff_polls = 0

    def cancel(self) -> None:
        """Drop the in-memory capture lease without publishing a final turn."""
        self.was_active = False
        self.tail_polls = 0
        self._release_lease()

    def _arm(self) -> dict[str, Any]:
        """Archive the activation boundary without publishing prior history."""
        self.last_result = sync_once(
            global_state_path=self.global_state_path,
            continuity_path=self.continuity_path,
            state_path=self.state_path,
            archive_path=self.archive_path,
            publish=False,
            allow_last_good=False,
        )
        thread_id = self.last_result.get("threadId")
        items = self.last_result.get("items")
        if (
            self.last_result.get("stale") is not False
            or not isinstance(thread_id, str)
            or isinstance(items, bool)
            or not isinstance(items, int)
            or items < 0
        ):
            self._release_lease()
            return self.last_result
        self._lease_thread_id = thread_id
        # Item indexes are zero-based.  Requiring assistant index N + 1
        # ensures that both sides of an adjacent user/assistant pair were
        # appended after a baseline containing N items.
        self._minimum_assistant_index = items + 1
        identity = snapshot_output_identity(self.snapshot_path)
        if identity is not None:
            self._lease_source_instance_id = identity[
                "source_instance_id"
            ]
        return self.last_result

    def _sync(self) -> dict[str, Any] | None:
        if (
            self._lease_thread_id is None
            or self._minimum_assistant_index is None
        ):
            return None
        if self._failure_backoff_polls > 0:
            self._failure_backoff_polls -= 1
            return self.last_result
        identity = snapshot_output_identity(self.snapshot_path)
        if identity is not None and self._lease_source_instance_id is None:
            self._lease_source_instance_id = identity[
                "source_instance_id"
            ]
        source_matches_lease = (
            identity is None and self._lease_source_instance_id is None
            or identity is not None
            and identity.get("source_instance_id")
                == self._lease_source_instance_id
        )
        if not source_matches_lease:
            self.last_result = sync_once(
                global_state_path=self.global_state_path,
                continuity_path=self.continuity_path,
                state_path=self.state_path,
                archive_path=self.archive_path,
                publish=False,
                expected_thread_id=self._lease_thread_id,
                minimum_assistant_index=self._minimum_assistant_index,
                allow_last_good=False,
            )
            route_error = (
                "Reader output source unavailable during capture lease"
                if identity is None
                else "active Reader source changed during capture lease"
            )
            prior_error = self.last_result.get("error")
            self.last_result["stale"] = True
            self.last_result["error"] = (
                f"{prior_error}; {route_error}"
                if prior_error
                else route_error
            )
            self._failure_backoff_polls = 0
            return self.last_result
        file = identity.get("file") if identity else None
        page = identity.get("page") if identity else None
        self.last_result = sync_once(
            global_state_path=self.global_state_path,
            continuity_path=self.continuity_path,
            state_path=self.state_path,
            archive_path=self.archive_path,
            publish=True,
            publisher=self.publisher,
            file=file,
            page=page,
            source_instance_id=(
                identity.get("source_instance_id") if identity else None
            ),
            snapshot_revision=(
                identity.get("snapshot_revision") if identity else None
            ),
            expected_thread_id=self._lease_thread_id,
            minimum_assistant_index=self._minimum_assistant_index,
            allow_last_good=False,
        )
        if (
            self.last_result.get("error")
            and self.last_result.get("pending", 0) > 0
            and self.last_result.get("published", 0) == 0
        ):
            self._failure_backoff_polls = (
                PUBLISH_FAILURE_BACKOFF_POLLS
            )
        else:
            self._failure_backoff_polls = 0
        return self.last_result

    def observe(
        self,
        *,
        service_online: bool,
        capture_active: bool,
        snapshot_mode: bool,
    ) -> dict[str, Any] | None:
        if snapshot_mode is not True:
            self.cancel()
            return None
        if service_online is not True:
            self.cancel()
            return None
        active = bool(service_online and capture_active)
        if active:
            if not self.was_active:
                self.tail_polls = 0
                self._release_lease()
                self.was_active = True
                return self._arm()
            self.was_active = True
            self.tail_polls = 0
            return self._sync()
        if self.was_active:
            self.was_active = False
            if self._lease_thread_id is not None:
                self.tail_polls = FINAL_TAIL_POLLS
        if self.tail_polls > 0:
            self.tail_polls -= 1
            result = self._sync()
            if self.tail_polls == 0:
                self._release_lease()
            return result
        return None

    def finish(self) -> dict[str, Any] | None:
        if (
            self._lease_thread_id is not None
            and (self.was_active or self.tail_polls > 0)
        ):
            self.was_active = False
            self.tail_polls = 0
            result = self._sync()
            self._release_lease()
            return result
        self.cancel()
        return None


def monitor_capture_history(
    *,
    stop_event: threading.Event,
    status_provider: StatusProvider,
    enabled_provider: Callable[[], bool],
    synchronizer: CaptureBoundHistorySynchronizer,
    poll_seconds: float = HISTORY_POLL_SECONDS,
) -> None:
    """Poll strict native status until the owning bridge lifecycle stops."""
    try:
        while not stop_event.is_set():
            try:
                snapshot_mode = enabled_provider() is True
            except Exception:
                snapshot_mode = False
            try:
                status = status_provider()
                service_online = status.service_online is True
                capture_active = status.capture_active is True
            except Exception:
                service_online = False
                capture_active = False
            synchronizer.observe(
                service_online=service_online,
                capture_active=capture_active,
                snapshot_mode=snapshot_mode,
            )
            stop_event.wait(poll_seconds)
    finally:
        try:
            snapshot_mode = enabled_provider() is True
        except Exception:
            snapshot_mode = False
        if snapshot_mode:
            synchronizer.finish()
        else:
            synchronizer.cancel()


@contextmanager
def history_worker_lease(root: Path) -> Iterator[bool]:
    """Own one per-install Windows history worker, without a durable PID file."""
    if os.name != "nt":
        yield True
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    digest = hashlib.sha256(
        str(root.resolve()).casefold().encode("utf-8")
    ).hexdigest()[:20]
    handle = kernel32.CreateMutexW(
        None,
        True,
        f"Local\\BWReaderSidebarHistory-{digest}",
    )
    if not handle:
        yield False
        return
    try:
        yield ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    finally:
        kernel32.CloseHandle(handle)


def run_service_bound_history_worker(
    *,
    root: Path,
    status_provider: StatusProvider,
    enabled_provider: Callable[[], bool],
    publisher: Publisher = _bridge_publisher,
    poll_seconds: float = HISTORY_POLL_SECONDS,
    wait: Callable[[float], None] | None = None,
    max_cycles: int | None = None,
) -> int:
    """Run the packaged worker until opt-out or sustained service shutdown."""
    if max_cycles is not None and max_cycles <= 0:
        raise ValueError("max_cycles must be positive")
    waiter = wait or threading.Event().wait
    synchronizer = CaptureBoundHistorySynchronizer(
        root=root,
        publisher=publisher,
    )
    offline_polls = 0
    seen_online = False
    cycles = 0
    with history_worker_lease(root) as owned:
        if not owned:
            return 0
        try:
            while True:
                try:
                    snapshot_mode = enabled_provider() is True
                except Exception:
                    snapshot_mode = False
                if not snapshot_mode:
                    synchronizer.cancel()
                    break
                try:
                    status = status_provider()
                    service_online = status.service_online is True
                    capture_active = status.capture_active is True
                except Exception:
                    service_online = False
                    capture_active = False
                synchronizer.observe(
                    service_online=service_online,
                    capture_active=capture_active,
                    snapshot_mode=True,
                )
                if service_online:
                    seen_online = True
                    offline_polls = 0
                else:
                    offline_polls += 1
                    if seen_online and offline_polls >= OFFLINE_EXIT_POLLS:
                        break
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                waiter(poll_seconds)
        finally:
            try:
                snapshot_mode = enabled_provider() is True
            except Exception:
                snapshot_mode = False
            if snapshot_mode:
                synchronizer.finish()
            else:
                synchronizer.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    global_default, continuity_default, state_default, archive_default = (
        _default_paths()
    )
    parser = argparse.ArgumentParser(
        description="Archive local Codex Voice history; publish only on request"
    )
    parser.add_argument("--global-state", type=Path, default=global_default)
    parser.add_argument("--continuity", type=Path, default=continuity_default)
    parser.add_argument("--state", type=Path, default=state_default)
    parser.add_argument("--archive", type=Path, default=archive_default)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--file", help="optional Reader vault-relative file")
    parser.add_argument("--page", type=int, help="optional Reader page")
    args = parser.parse_args(argv)

    if args.page is not None and args.page < 1:
        status = _status(
            thread_id=None,
            dry_run=not args.publish,
            gap=True,
            error="page must be a positive integer",
        )
        print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
        return 2

    result = sync_once(
        global_state_path=args.global_state,
        continuity_path=args.continuity,
        state_path=args.state,
        archive_path=args.archive,
        publish=args.publish,
        publisher=_bridge_publisher if args.publish else None,
        file=args.file,
        page=args.page,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if "error" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
