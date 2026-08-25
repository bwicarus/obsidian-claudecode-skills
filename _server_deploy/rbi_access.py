"""RBI authentication tickets and public-network URL guards.

The Flask process and the standalone RBI WebSocket process share this module.
Tickets are short-lived bearer credentials that bind one positive numeric user
id.  The browser process must derive profile and cookie paths from the verified
identity returned here, never from a client-supplied path or uid.
"""

from __future__ import annotations

from dataclasses import dataclass
try:
    import fcntl
except ImportError:
    # Windows（本地 Flask 实例）没有 fcntl。票据/URL 守卫是跨平台的，
    # 只有 profile 租约依赖 flock —— 那两处在无 fcntl 平台上响亮失败，
    # 而不是让整条服务端导入链在 Windows 上炸掉。
    fcntl = None  # type: ignore[assignment]
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlparse


RBI_TICKET_VERSION = "rbit-v1"
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 900
RBI_PROFILE_DIRNAME = "rbi-profiles"
RBI_LEGACY_PROFILE_DIRNAME = "rbi-profile"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_SAFE_LOCAL_SCHEMES = frozenset({"about", "blob", "data"})
_CHROMIUM_RUNTIME_PROFILE_NAMES = frozenset({
    "DevToolsActivePort",
    "LOCK",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
})


@dataclass(frozen=True)
class RbiIdentity:
    """An identity that can only be created with a positive numeric uid."""

    user_id: int

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int):
            raise TypeError("RBI user id must be an integer")
        if self.user_id <= 0:
            raise ValueError("RBI user id must be positive")


@dataclass(frozen=True)
class RbiTicketClaims:
    identity: RbiIdentity
    expires_at: int
    nonce: str


class RbiTicketNonceRegistry:
    """Process-local, bounded single-use registry for verified RBI tickets."""

    def __init__(self, max_entries: int = 8192) -> None:
        if isinstance(max_entries, bool) or int(max_entries) < 1:
            raise ValueError("RBI nonce registry size must be positive")
        self._max_entries = int(max_entries)
        self._entries: dict[str, int] = {}
        self._lock = threading.Lock()

    def consume(
        self,
        claims: RbiTicketClaims,
        *,
        now: int | None = None,
    ) -> bool:
        """Consume a verified nonce once, failing closed when storage is full."""

        if not isinstance(claims, RbiTicketClaims):
            raise TypeError("verified RBI ticket claims required")
        current = int(time.time()) if now is None else int(now)
        with self._lock:
            expired = [
                nonce
                for nonce, expires_at in self._entries.items()
                if expires_at <= current
            ]
            for nonce in expired:
                self._entries.pop(nonce, None)
            if claims.expires_at <= current or claims.nonce in self._entries:
                return False
            # Never evict an unexpired entry: doing so would make its ticket
            # replayable.  A full registry rejects new sessions until cleanup.
            if len(self._entries) >= self._max_entries:
                return False
            self._entries[claims.nonce] = claims.expires_at
            return True

    def active_count(self, *, now: int | None = None) -> int:
        """Return the live entry count while opportunistically pruning expiry."""

        current = int(time.time()) if now is None else int(now)
        with self._lock:
            expired = [
                nonce
                for nonce, expires_at in self._entries.items()
                if expires_at <= current
            ]
            for nonce in expired:
                self._entries.pop(nonce, None)
            return len(self._entries)


class RbiProfileLease:
    """An advisory exclusive lease understood by live and demo processes."""

    def __init__(self, descriptor: int, path: Path) -> None:
        self._descriptor = descriptor
        self.path = path
        self._release_lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._descriptor >= 0

    def release(self) -> None:
        with self._release_lock:
            descriptor = self._descriptor
            if descriptor < 0:
                return
            self._descriptor = -1
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> RbiProfileLease:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.release()


class RbiDemoProfile:
    """A canonical leased profile or an isolated disposable snapshot."""

    def __init__(
        self,
        path: Path,
        *,
        lease: RbiProfileLease | None = None,
        temporary: bool = False,
    ) -> None:
        self.path = path
        self.lease = lease
        self.temporary = temporary
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.lease is not None:
            self.lease.release()
        if self.temporary and self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> RbiDemoProfile:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    value = bytes(secret)
    if len(value) < 32:
        raise ValueError("RBI ticket secret must be at least 32 bytes")
    return value


def load_rbi_ticket_secret(project_root: str | Path) -> bytes:
    """Load a shared secret, creating a private persistent key if necessary."""

    configured = os.environ.get("READER_RBI_SECRET")
    if configured:
        return _secret_bytes(configured)
    secret_path = Path(project_root).resolve() / "state" / "rbi-ticket-secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if not secret_path.exists():
        # Publish only after the complete key is on disk.  Flask and the
        # standalone WS process can start simultaneously; creating the final
        # file first would let the loser briefly read an empty secret.
        temporary = secret_path.with_name(
            secret_path.name + "." + secrets.token_hex(8) + ".tmp"
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            material = secrets.token_hex(32).encode("ascii")
            offset = 0
            while offset < len(material):
                written = os.write(fd, material[offset:])
                if written <= 0:
                    raise OSError("could not persist RBI ticket secret")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, secret_path)
        except FileExistsError:
            pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    return _secret_bytes(secret_path.read_bytes().strip())


def issue_rbi_ticket(
    secret: str | bytes,
    user_id: int,
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    identity = RbiIdentity(user_id)
    ttl = int(ttl_seconds)
    if ttl < 1 or ttl > MAX_TTL_SECONDS:
        raise ValueError("RBI ticket ttl is out of range")
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + ttl
    nonce = secrets.token_urlsafe(18)
    payload = (
        f"{RBI_TICKET_VERSION}.{identity.user_id}.{expires_at}.{nonce}"
    )
    signature = hmac.new(
        _secret_bytes(secret),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_rbi_ticket(
    secret: str | bytes,
    ticket: str,
    *,
    now: int | None = None,
    expected_user_id: int | None = None,
) -> RbiTicketClaims | None:
    """Verify signature, exclusive expiry, maximum future window and uid."""

    token = str(ticket or "")
    if len(token) > 256:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != RBI_TICKET_VERSION:
        return None
    version, uid_raw, expiry_raw, nonce, supplied = parts
    if not uid_raw.isdigit() or not expiry_raw.isdigit():
        return None
    try:
        identity = RbiIdentity(int(uid_raw))
        expires_at = int(expiry_raw)
        secret_bytes = _secret_bytes(secret)
    except (TypeError, ValueError):
        return None
    if expected_user_id is not None:
        try:
            expected = RbiIdentity(int(expected_user_id))
        except (TypeError, ValueError):
            return None
        if identity != expected:
            return None
    if not _NONCE_RE.fullmatch(nonce):
        return None
    current = int(time.time()) if now is None else int(now)
    if expires_at <= current or expires_at > current + MAX_TTL_SECONDS:
        return None
    payload = f"{version}.{identity.user_id}.{expires_at}.{nonce}"
    wanted = hmac.new(
        secret_bytes,
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if len(supplied) != len(wanted) or not hmac.compare_digest(supplied, wanted):
        return None
    return RbiTicketClaims(
        identity=identity,
        expires_at=expires_at,
        nonce=nonce,
    )


def rbi_profile_path(
    profile_root: str | Path,
    identity: RbiIdentity,
) -> Path:
    """Return one confined profile directory for a verified identity."""

    if not isinstance(identity, RbiIdentity):
        raise TypeError("verified RBI identity required")
    root = Path(profile_root).resolve()
    expected = root / str(identity.user_id)
    result = expected.resolve()
    if result != expected or result.parent != root:
        raise ValueError("RBI profile escaped its root")
    return result


def _copy_profile_read_only(source: Path, destination: Path) -> None:
    """Copy regular profile files without following links or special files."""

    for current, directory_names, file_names in os.walk(
        source,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)

        kept_directories = []
        for name in directory_names:
            child = current_path / name
            try:
                mode = child.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in file_names:
            child = current_path / name
            try:
                mode = child.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
                shutil.copy2(child, target_dir / name, follow_symlinks=False)


def prepare_rbi_profile(
    profile_root: str | Path,
    legacy_profile_root: str | Path,
    identity: RbiIdentity,
) -> Path:
    """Prepare the canonical profile, copying the exact uid's legacy data once.

    The legacy tree is treated as read-only.  Links and special files are not
    copied, and no other uid directory is ever inspected.
    """

    canonical_root = Path(profile_root).resolve()
    canonical_root.mkdir(parents=True, exist_ok=True)
    target = rbi_profile_path(canonical_root, identity)
    if target.exists():
        if not target.is_dir():
            raise ValueError("RBI profile path is not a directory")
        return target

    legacy_root = Path(legacy_profile_root).resolve()
    source = rbi_profile_path(legacy_root, identity)
    if source.exists():
        if not source.is_dir():
            raise ValueError("legacy RBI profile path is not a directory")
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{identity.user_id}.migrate-",
                dir=canonical_root,
            )
        )
        installed = False
        try:
            _copy_profile_read_only(source, temporary)
            try:
                temporary.rename(target)
                installed = True
            except OSError:
                # Another live/demo process may have completed the same
                # one-time migration first.  Accept only its confined result.
                if not target.is_dir():
                    raise
        finally:
            if not installed and temporary.exists():
                shutil.rmtree(temporary)
    else:
        try:
            target.mkdir()
        except FileExistsError:
            if not target.is_dir():
                raise ValueError("RBI profile path is not a directory")
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def acquire_rbi_profile_lease(
    profile_root: str | Path,
    identity: RbiIdentity,
    *,
    blocking: bool = False,
) -> RbiProfileLease | None:
    """Acquire one uid's cross-process profile lease without trusting a path."""

    if not isinstance(identity, RbiIdentity):
        raise TypeError("verified RBI identity required")
    if fcntl is None:
        raise OSError("RBI profile leases require flock (POSIX only)")
    root = Path(profile_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_root = root / ".locks"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    if lock_root.resolve() != lock_root or lock_root.parent != root:
        raise ValueError("RBI profile lock directory escaped its root")
    try:
        os.chmod(lock_root, 0o700)
    except OSError:
        pass
    lock_path = lock_root / f"{identity.user_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise ValueError("RBI profile lock file is unsafe")
        os.chmod(lock_path, 0o600)
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return RbiProfileLease(descriptor, lock_path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _copy_demo_profile_snapshot(source: Path, destination: Path) -> None:
    """Best-effort copy that excludes active Chromium lock artefacts."""

    for current, directory_names, file_names in os.walk(
        source,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        if relative.parts and relative.parts[0] == ".locks":
            directory_names[:] = []
            continue
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)

        kept_directories = []
        for name in directory_names:
            child = current_path / name
            try:
                mode = child.lstat().st_mode
            except OSError:
                continue
            if (
                name not in _CHROMIUM_RUNTIME_PROFILE_NAMES
                and stat.S_ISDIR(mode)
                and not stat.S_ISLNK(mode)
            ):
                kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in file_names:
            if (
                name in _CHROMIUM_RUNTIME_PROFILE_NAMES
                or name.endswith((".lock", ".lockfile"))
            ):
                continue
            child = current_path / name
            try:
                mode = child.lstat().st_mode
                if stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
                    shutil.copy2(
                        child,
                        target_dir / name,
                        follow_symlinks=False,
                    )
            except OSError:
                # A live Chromium can rotate profile files during the snapshot.
                # Missing one file is safer than touching its active profile.
                continue


def open_rbi_demo_profile(
    profile_root: str | Path,
    identity: RbiIdentity,
) -> RbiDemoProfile:
    """Use the canonical profile exclusively, or an isolated live snapshot."""

    canonical = rbi_profile_path(profile_root, identity)
    if not canonical.is_dir():
        raise ValueError("canonical RBI profile is not prepared")
    lease = acquire_rbi_profile_lease(
        profile_root,
        identity,
        blocking=False,
    )
    if lease is not None:
        return RbiDemoProfile(canonical, lease=lease)

    temporary = Path(
        tempfile.mkdtemp(prefix=f"rbi-demo-{identity.user_id}-")
    )
    try:
        os.chmod(temporary, 0o700)
    except OSError:
        pass
    try:
        _copy_demo_profile_snapshot(canonical, temporary)
    except Exception:
        shutil.rmtree(temporary)
        raise
    return RbiDemoProfile(temporary, temporary=True)


Resolver = Callable[..., Iterable[tuple]]


def public_network_url_error(
    url: str,
    *,
    allowed_schemes: Iterable[str] = ("http", "https"),
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Return an error unless every resolved address is globally routable."""

    raw = str(url or "")
    if (
        not raw
        or len(raw) > 8192
        or "\\" in raw
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw)
    ):
        return "URL 无法解析"
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").strip().rstrip(".").lower()
        port = parsed.port
    except Exception:
        return "URL 无法解析"
    schemes = frozenset(str(s).lower() for s in allowed_schemes)
    if parsed.scheme.lower() not in schemes:
        return "只允许公网 " + "/".join(sorted(schemes))
    if parsed.username is not None or parsed.password is not None:
        return "URL 不允许内嵌账号"
    if not host or host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal", ".home.arpa")
    ):
        return "不允许本机或内网地址"
    if port is not None and not (1 <= port <= 65535):
        return "URL 端口无效"
    try:
        answers = list(resolver(host, None, type=socket.SOCK_STREAM))
    except Exception:
        return "域名解析失败"
    if not answers:
        return "域名解析失败"
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0].split("%", 1)[0])
        except (IndexError, TypeError, ValueError):
            return "域名解析结果无效"
        if not address.is_global:
            return "不允许本机或内网地址"
    return ""


def browser_request_url_error(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Guard a Chromium navigation, redirect, subresource or WebSocket URL."""

    try:
        scheme = urlparse(str(url or "")).scheme.lower()
    except Exception:
        return "URL 无法解析"
    if scheme in _SAFE_LOCAL_SCHEMES:
        return ""
    if scheme in ("http", "https"):
        return public_network_url_error(url, resolver=resolver)
    if scheme in ("ws", "wss"):
        return public_network_url_error(
            url,
            allowed_schemes=("ws", "wss"),
            resolver=resolver,
        )
    return "不允许的浏览器请求协议"
