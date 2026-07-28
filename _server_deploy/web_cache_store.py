"""Per-account storage for extracted web-page material.

New records live below ``web-cache/by-user/<uid>/`` and carry the uid in their
payload.  The previous uid-derived root-level filename remains readable for the
same uid, but is never enumerated globally or rewritten implicitly.  The oldest
shared URL-only cache is readable only when an operator explicitly assigns its
ownership with ``READER_LEGACY_WEB_CACHE_OWNER_UID``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Iterator


WEB_CACHE_FORMAT = "bw-web-material-v2"


def normalize_user_id(value: str | int | None) -> str:
    uid = str(value or "")
    return uid if uid.isdigit() and int(uid) > 0 else ""


def _account_dir(root: str | Path, user_id: str | int) -> Path:
    uid = normalize_user_id(user_id)
    if not uid:
        raise ValueError("verified positive user id required")
    base = (Path(root).resolve() / "by-user").resolve()
    expected = base / uid
    resolved = expected.resolve()
    if resolved != expected or resolved.parent != base:
        raise ValueError("web cache account directory escaped its root")
    return resolved


def web_cache_path(root: str | Path, user_id: str | int, url: str) -> Path:
    key = hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:32]
    return _account_dir(root, user_id) / (key + ".json")


def legacy_account_cache_path(
    root: str | Path,
    user_id: str | int,
    url: str,
) -> Path:
    uid = normalize_user_id(user_id)
    if not uid:
        raise ValueError("verified positive user id required")
    key = hashlib.sha256(
        ("web-material\0" + uid + "\0" + str(url or "")).encode("utf-8")
    ).hexdigest()[:32]
    return Path(root).resolve() / (key + ".json")


def legacy_shared_cache_path(root: str | Path, url: str) -> Path:
    key = hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:20]
    return Path(root).resolve() / (key + ".json")


def _valid_record(data, *, user_id: str, url: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    stored_url = str(data.get("url") or "")
    if stored_url != str(url or ""):
        return None
    stored_uid = normalize_user_id(data.get("user_id"))
    if stored_uid and stored_uid != user_id:
        return None
    return data


def read_web_cache(
    root: str | Path,
    user_id: str | int,
    url: str,
) -> tuple[dict | None, Path | None]:
    uid = normalize_user_id(user_id)
    if not uid:
        return None, None
    candidates = [
        web_cache_path(root, uid, url),
        legacy_account_cache_path(root, uid, url),
    ]
    if os.environ.get("READER_LEGACY_WEB_CACHE_OWNER_UID") == uid:
        candidates.append(legacy_shared_cache_path(root, url))
    for path in candidates:
        try:
            data = _valid_record(
                json.loads(path.read_text("utf-8")),
                user_id=uid,
                url=url,
            )
        except Exception:
            continue
        if data is not None:
            return data, path
    return None, None


def write_web_cache(
    root: str | Path,
    user_id: str | int,
    url: str,
    *,
    title: str,
    text: str,
    timestamp: int | None = None,
) -> Path:
    uid = normalize_user_id(user_id)
    if not uid:
        raise ValueError("verified positive user id required")
    path = web_cache_path(root, uid, url)
    payload = {
        "format": WEB_CACHE_FORMAT,
        "user_id": uid,
        "url": str(url or ""),
        "title": str(title or "")[:120],
        "text": str(text or "")[:200000],
        "ts": int(time.time() if timestamp is None else timestamp),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return path


def iter_account_web_cache(
    root: str | Path,
    *,
    user_id: str | int,
) -> Iterator[tuple[str, Path, dict]]:
    uid = normalize_user_id(user_id)
    if not uid:
        raise ValueError("explicit verified user id required for cache enumeration")
    base = Path(root).resolve() / "by-user"
    account_dir = _account_dir(root, uid)
    try:
        if account_dir.resolve().parent != base.resolve():
            return
    except OSError:
        return
    for path in account_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            continue
        if (
            isinstance(data, dict)
            and data.get("format") == WEB_CACHE_FORMAT
            and normalize_user_id(data.get("user_id")) == uid
            and data.get("url")
        ):
            yield uid, path, data
