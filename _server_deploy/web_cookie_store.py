"""Account-scoped imported web cookies with conservative host semantics.

The reader accepts a pasted ``Cookie`` header for compatibility, but persists it
as an explicit v2 structure.  Imported cookies are always:

* bound to one exact canonical host (never a parent-domain wildcard);
* sent only over HTTPS;
* treated as Secure, host-only cookies.

Legacy ``{domain: {name: value}}`` files are read without being rewritten.
Their old domain key is interpreted as an exact host, so reading old data cannot
retain the previous parent-domain expansion.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlparse

from web_proxy_cap import normalize_web_proxy_scope


COOKIE_STORE_VERSION = 2
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}$")
_MAX_COOKIE_VALUE = 8192


def _user_id(value: str | int) -> str:
    uid = str(value)
    if not uid.isdigit() or int(uid) <= 0:
        raise ValueError("verified positive user id required")
    return uid


def _host(value: str) -> str:
    host = normalize_web_proxy_scope(str(value or "").strip().lstrip("."))
    # Imported browser cookies for a bare public suffix or local single-label
    # name are never useful to this public-web proxy.  IP literals remain valid.
    if "." not in host and ":" not in host and not host.replace(".", "").isdigit():
        raise ValueError("cookie host must be a fully-qualified public host")
    return host


def _cookie_value(value) -> str:
    text = str(value)
    if (
        len(text) > _MAX_COOKIE_VALUE
        or "\x00" in text
        or "\r" in text
        or "\n" in text
    ):
        raise ValueError("invalid cookie value")
    return text


def _cookie_record(value) -> dict | None:
    if isinstance(value, Mapping):
        raw = value.get("value")
    else:
        raw = value
    if raw is None:
        return None
    try:
        text = _cookie_value(raw)
    except ValueError:
        return None
    return {
        "value": text,
        "secure": True,
        "hostOnly": True,
        "path": "/",
    }


def empty_cookie_store() -> dict:
    return {"version": COOKIE_STORE_VERSION, "hosts": {}}


def normalize_cookie_store(raw) -> dict:
    """Return canonical v2 data; legacy domains become exact hosts in memory."""
    out = empty_cookie_store()
    if not isinstance(raw, Mapping):
        return out
    if raw.get("version") == COOKIE_STORE_VERSION and isinstance(raw.get("hosts"), Mapping):
        source = raw["hosts"]
        versioned = True
    else:
        source = raw
        versioned = False
    for supplied_host, host_record in source.items():
        if not isinstance(supplied_host, str) or supplied_host.startswith("_"):
            continue
        try:
            host = _host(supplied_host)
        except ValueError:
            continue
        if versioned:
            cookies = host_record.get("cookies") if isinstance(host_record, Mapping) else None
        else:
            cookies = host_record
        if not isinstance(cookies, Mapping):
            continue
        clean = {}
        for name, value in cookies.items():
            name = str(name or "")
            if not _COOKIE_NAME_RE.fullmatch(name):
                continue
            record = _cookie_record(value)
            if record is not None:
                clean[name] = record
        if clean:
            out["hosts"][host] = {"cookies": clean}
    return out


def cookie_store_path(root: str | Path, user_id: str | int) -> Path:
    uid = _user_id(user_id)
    base = Path(root).resolve()
    expected = base / f"{uid}.json"
    resolved = expected.resolve()
    if resolved != expected or resolved.parent != base:
        raise ValueError("cookie store escaped its root")
    return resolved


def load_cookie_store(root: str | Path, user_id: str | int) -> dict:
    try:
        path = cookie_store_path(root, user_id)
        return normalize_cookie_store(json.loads(path.read_text("utf-8")))
    except Exception:
        return empty_cookie_store()


def save_cookie_store(root: str | Path, user_id: str | int, data: dict) -> None:
    path = cookie_store_path(root, user_id)
    clean = normalize_cookie_store(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(clean, ensure_ascii=False), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def cookie_hosts(root: str | Path, user_id: str | int) -> list[str]:
    return sorted(load_cookie_store(root, user_id)["hosts"])


def parse_cookie_header(value: str) -> dict:
    cookies = {}
    for part in str(value or "").split(";"):
        if "=" not in part:
            continue
        name, raw = part.split("=", 1)
        name = name.strip()
        if not _COOKIE_NAME_RE.fullmatch(name):
            continue
        try:
            cookies[name] = _cookie_record(raw.strip())
        except ValueError:
            continue
    return {name: record for name, record in cookies.items() if record is not None}


def put_cookie_header(
    root: str | Path,
    user_id: str | int,
    host: str,
    header: str,
) -> int:
    exact_host = _host(host)
    cookies = parse_cookie_header(header)
    if not cookies:
        raise ValueError("cookie header is empty or invalid")
    store = load_cookie_store(root, user_id)
    store["hosts"][exact_host] = {"cookies": cookies}
    save_cookie_store(root, user_id, store)
    return len(cookies)


def remove_cookie_host(root: str | Path, user_id: str | int, host: str) -> bool:
    exact_host = _host(host)
    store = load_cookie_store(root, user_id)
    removed = store["hosts"].pop(exact_host, None) is not None
    save_cookie_store(root, user_id, store)
    return removed


def _https_exact_host(url: str, expected_host: str | None = None) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme.lower() != "https":
        return ""
    try:
        host = _host(parsed.hostname or "")
        expected = _host(expected_host) if expected_host else host
    except ValueError:
        return ""
    return host if host == expected else ""


def cookie_values_for_url(
    root: str | Path,
    user_id: str | int,
    url: str,
    *,
    expected_host: str | None = None,
) -> dict[str, str]:
    host = _https_exact_host(url, expected_host)
    if not host:
        return {}
    record = load_cookie_store(root, user_id)["hosts"].get(host) or {}
    cookies = record.get("cookies") if isinstance(record, Mapping) else {}
    if not isinstance(cookies, Mapping):
        return {}
    return {
        str(name): str(value.get("value") or "")
        for name, value in cookies.items()
        if isinstance(value, Mapping) and value.get("secure") is True
    }


def update_cookie_values_for_url(
    root: str | Path,
    user_id: str | int,
    url: str,
    values: Mapping,
    *,
    expected_host: str | None = None,
) -> bool:
    host = _https_exact_host(url, expected_host)
    if not host or not isinstance(values, Mapping):
        return False
    store = load_cookie_store(root, user_id)
    host_record = store["hosts"].get(host)
    if not isinstance(host_record, Mapping):
        return False
    cookies = dict(host_record.get("cookies") or {})
    changed = False
    for name, raw in values.items():
        name = str(name or "")
        if not _COOKIE_NAME_RE.fullmatch(name):
            continue
        record = _cookie_record(raw)
        if record is not None:
            cookies[name] = record
            changed = True
    if not changed:
        return False
    store["hosts"][host] = {"cookies": cookies}
    save_cookie_store(root, user_id, store)
    return True


def playwright_cookies_for_user(
    root: str | Path,
    user_id: str | int,
    url: str,
) -> list[dict]:
    parsed = urlparse(str(url or ""))
    host = _https_exact_host(url)
    if not host:
        return []
    values = cookie_values_for_url(root, user_id, url, expected_host=host)
    origin_host = f"[{host}]" if ":" in host else host
    origin = f"https://{origin_host}"
    if parsed.port and parsed.port != 443:
        origin += f":{parsed.port}"
    return [
        {
            "name": name,
            "value": value,
            "url": origin + "/",
            "secure": True,
        }
        for name, value in values.items()
    ]
