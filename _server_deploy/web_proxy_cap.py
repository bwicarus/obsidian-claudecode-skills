"""Short-lived capability for sandboxed web-reader proxy documents/resources.

The sandbox has an opaque origin and therefore sends no application session
cookie for subresources.  This token authorizes only the /pdf/web proxy
transport for one top-level website scope; it is never accepted by ordinary
account or reader APIs.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit


PREFIX = "wcap-v3"
MAX_TTL_SECONDS = 5 * 60
MAX_RESOURCE_REQUESTS_PER_CAP = 512
MAX_RESOURCE_BYTES_PER_CAP = 128 * 1024 * 1024
MAX_RESOURCE_BYTES_PER_RESPONSE = 24 * 1024 * 1024
_DEFAULT_PORT = {"http": 80, "https": 443}


@dataclass(frozen=True)
class WebProxyCapDetails:
    user_id: int
    expires_at: int
    scope_scheme: str
    scope_host: str
    scope_port: int

    @property
    def scope_origin(self) -> str:
        return canonical_web_proxy_origin(
            self.scope_scheme,
            self.scope_host,
            self.scope_port,
        )


def _key(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        return secret
    return str(secret or "").encode("utf-8")


def normalize_web_proxy_scope(value: str) -> str:
    """Return the canonical ASCII hostname used as the capability scope."""
    host = str(value or "").strip().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 253:
        raise ValueError("invalid proxy capability scope")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise ValueError("invalid proxy capability scope") from None
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in label)
        for label in labels
    ):
        raise ValueError("invalid proxy capability scope")
    return host


def canonical_web_proxy_origin(scheme: str, host: str, port: int) -> str:
    canonical_scheme = str(scheme or "").lower()
    if canonical_scheme not in _DEFAULT_PORT:
        raise ValueError("invalid proxy capability scheme")
    canonical_host = normalize_web_proxy_scope(host)
    canonical_port = int(port)
    if canonical_port <= 0 or canonical_port > 65535:
        raise ValueError("invalid proxy capability port")
    rendered_host = (
        f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    )
    # Keep the effective port explicit in the signed scope.  Thus
    # ``https://host`` and ``https://host:443`` normalize identically, while
    # ``https://host:8443`` is a distinct capability.
    return f"{canonical_scheme}://{rendered_host}:{canonical_port}"


def normalize_web_proxy_origin(value: str) -> str:
    """Return a canonical signed origin with an explicit effective port."""
    try:
        parsed = urlsplit(str(value or "").strip())
        scheme = parsed.scheme.lower()
        if scheme not in _DEFAULT_PORT or parsed.username or parsed.password:
            raise ValueError
        host = normalize_web_proxy_scope(parsed.hostname or "")
        port = parsed.port or _DEFAULT_PORT[scheme]
    except (ValueError, TypeError):
        raise ValueError("invalid proxy capability origin") from None
    return canonical_web_proxy_origin(scheme, host, port)


def web_proxy_cap_matches_url(details: WebProxyCapDetails, url: str) -> bool:
    try:
        return details.scope_origin == normalize_web_proxy_origin(url)
    except ValueError:
        return False


def _encode_scope(scope_url: str) -> str:
    raw = normalize_web_proxy_origin(scope_url).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_scope(value: str) -> str:
    if not value or len(value) > 384:
        raise ValueError("invalid proxy capability scope")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
        raise ValueError("invalid proxy capability scope")
    padding = "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        origin = raw.decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid proxy capability scope") from None
    canonical = normalize_web_proxy_origin(origin)
    if _encode_scope(canonical) != value:
        raise ValueError("non-canonical proxy capability scope")
    return canonical


def issue_web_proxy_cap(
    secret: str | bytes,
    user_id: int,
    *,
    scope_url: str,
    now: int | None = None,
    ttl_seconds: int = MAX_TTL_SECONDS,
) -> str:
    uid = int(user_id)
    if uid <= 0:
        raise ValueError("invalid proxy capability user")
    issued_at = int(time.time() if now is None else now)
    ttl = max(60, min(MAX_TTL_SECONDS, int(ttl_seconds)))
    scope = _encode_scope(scope_url)
    unsigned = ".".join(
        (PREFIX, str(uid), str(issued_at + ttl), scope, secrets.token_hex(16))
    )
    signature = hmac.new(_key(secret), unsigned.encode("ascii"), hashlib.sha256).hexdigest()
    return unsigned + "." + signature


def verify_web_proxy_cap_details(
    secret: str | bytes,
    token: str,
    *,
    now: int | None = None,
) -> WebProxyCapDetails | None:
    value = str(token or "").strip()
    if len(value) > 768:
        return None
    parts = value.split(".")
    if len(parts) != 6 or parts[0] != PREFIX:
        return None
    _, uid_text, expiry_text, scope_text, nonce, signature = parts
    if (
        not uid_text.isdigit()
        or not expiry_text.isdigit()
        or len(nonce) != 32
        or any(ch not in "0123456789abcdef" for ch in nonce)
        or len(signature) != 64
        or any(ch not in "0123456789abcdef" for ch in signature)
    ):
        return None
    uid = int(uid_text)
    expiry = int(expiry_text)
    current = int(time.time() if now is None else now)
    if uid <= 0 or expiry <= current or expiry > current + MAX_TTL_SECONDS:
        return None
    try:
        scope_origin = _decode_scope(scope_text)
        parsed_scope = urlsplit(scope_origin)
        scope_scheme = parsed_scope.scheme
        scope_host = normalize_web_proxy_scope(parsed_scope.hostname or "")
        scope_port = parsed_scope.port or _DEFAULT_PORT[scope_scheme]
    except ValueError:
        return None
    unsigned = ".".join(parts[:5])
    expected = hmac.new(_key(secret), unsigned.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return WebProxyCapDetails(
        user_id=uid,
        expires_at=expiry,
        scope_scheme=scope_scheme,
        scope_host=scope_host,
        scope_port=scope_port,
    )


def verify_web_proxy_cap(
    secret: str | bytes,
    token: str,
    *,
    now: int | None = None,
) -> int | None:
    """Compatibility wrapper for callers that only need the authorized user."""
    details = verify_web_proxy_cap_details(secret, token, now=now)
    return details.user_id if details else None


@dataclass
class _CapBudgetState:
    expires_at: int
    requests: int = 0
    transferred_bytes: int = 0


class WebProxyCapBudgetLease:
    """One response's view of the process-local capability traffic budget."""

    def __init__(self, registry, key: str):
        self._registry = registry
        self._key = key
        self.response_bytes = 0

    def consume(self, amount: int) -> bool:
        count = max(0, int(amount))
        if self.response_bytes + count > MAX_RESOURCE_BYTES_PER_RESPONSE:
            return False
        if not self._registry._consume(self._key, count):
            return False
        self.response_bytes += count
        return True

    @property
    def response_remaining(self) -> int:
        return MAX_RESOURCE_BYTES_PER_RESPONSE - self.response_bytes


class WebProxyCapBudgetRegistry:
    """Bound requests and bytes authorized by one bearer capability.

    This is deliberately process-local: it is a low-cost damage bound for each
    web worker, while nginx/application-wide limits remain the outer boundary.
    """

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        max_requests: int = MAX_RESOURCE_REQUESTS_PER_CAP,
        max_bytes: int = MAX_RESOURCE_BYTES_PER_CAP,
    ):
        self.max_entries = max(1, int(max_entries))
        self.max_requests = max(1, int(max_requests))
        self.max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()
        self._states: dict[str, _CapBudgetState] = {}

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    def begin(
        self,
        token: str,
        details: WebProxyCapDetails,
        *,
        now: int | None = None,
    ) -> WebProxyCapBudgetLease | None:
        current = int(time.time() if now is None else now)
        if details.expires_at <= current:
            return None
        key = self._token_key(token)
        with self._lock:
            expired = [
                item_key
                for item_key, state in self._states.items()
                if state.expires_at <= current
            ]
            for item_key in expired:
                self._states.pop(item_key, None)
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= self.max_entries:
                    return None
                state = _CapBudgetState(expires_at=details.expires_at)
                self._states[key] = state
            elif state.expires_at != details.expires_at:
                return None
            if state.requests >= self.max_requests:
                return None
            state.requests += 1
        return WebProxyCapBudgetLease(self, key)

    def _consume(self, key: str, amount: int) -> bool:
        with self._lock:
            state = self._states.get(key)
            if state is None or state.transferred_bytes + amount > self.max_bytes:
                return False
            state.transferred_bytes += amount
            return True
