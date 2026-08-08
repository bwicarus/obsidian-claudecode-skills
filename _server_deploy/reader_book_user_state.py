"""Deterministic, account-scoped user-state packages for one Reader book.

This module deliberately owns no HTTP routes and imports no Flask state.  The
HTTP layer must resolve a verified :class:`ReaderStorageIdentity`, resolve the
opaque ``bookId`` to the current book, and provide adapters which read the
existing sidecars for that identity.  Keeping those responsibilities outside
this file prevents a package from ever carrying an account id, owner token,
credential, or server filesystem path.

The wire contract stores every domain as canonical JSON text.  This is
intentional: its SHA-256 can be verified byte-for-byte by Swift before the JSON
is decoded, without relying on two languages to serialize floating point
numbers identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from reader_sidecar_store import (
    ReaderStorageIdentity,
    SidecarStore,
    atomic_write_json,
    read_json,
)


CONTRACT = "reader-book-user-state/1"
METADATA_SCHEMA = "reader-book-user-state-meta/1"
PLAN_CONTRACT = "reader-book-user-state-plan/1"
BOOK_ID_RE = re.compile(r"^book_[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

DOMAIN_NAMES: tuple[str, ...] = (
    "reading-position",
    "highlights",
    "ink",
    "closed-regions",
    "notes",
    "user-pages",
    "card-placements",
    "entity-references",
)

# Large free-form assets do not belong here.  User pages/cards may reference a
# separately verified asset attachment, but cannot inline an unbounded image.
DOMAIN_MAX_BYTES: dict[str, int] = {
    "reading-position": 64 * 1024,
    "highlights": 6 * 1024 * 1024,
    "ink": 24 * 1024 * 1024,
    "closed-regions": 6 * 1024 * 1024,
    "notes": 10 * 1024 * 1024,
    "user-pages": 12 * 1024 * 1024,
    "card-placements": 3 * 1024 * 1024,
    "entity-references": 3 * 1024 * 1024,
}
MAX_RAW_DOMAIN_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_JSON_DEPTH = 40
MAX_JSON_NODES = 750_000
MAX_STRING_BYTES = 2 * 1024 * 1024
MAX_REVISION = 9_007_199_254_740_991  # JavaScript Number.MAX_SAFE_INTEGER.

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "credentials",
    "ownertoken",
    "password",
    "secret",
    "storagenamespace",
    "token",
    "userid",
}
_PATH_KEYS = {
    "absolutepath",
    "file",
    "filepath",
    "filesystempath",
    "localpath",
    "path",
    "sourcepath",
}
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_INK_SURFACE_RE = re.compile(r"^(?:\d+|u_[a-fA-F0-9]{4,32})$")
_EPUB_PDF_SURFACE_RE = re.compile(r"^pdf\|([^|\x00]{1,512})\|([1-9]\d*)$")


class UserStatePackageError(ValueError):
    """A package or integration adapter violated the public contract."""


class UserStatePackageTooLarge(UserStatePackageError):
    """A bounded package/domain limit was exceeded."""


class UserStateImportConflict(UserStatePackageError):
    """An integration attempted to overwrite a both-changed domain."""


@dataclass(frozen=True, slots=True)
class DomainHeader:
    digest: str
    revision: int
    empty: bool

    def __post_init__(self) -> None:
        if not SHA256_RE.fullmatch(str(self.digest or "")):
            raise UserStatePackageError("domain digest must be lowercase sha256")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
            or self.revision > MAX_REVISION
        ):
            raise UserStatePackageError("domain revision is invalid")
        if not isinstance(self.empty, bool):
            raise UserStatePackageError("domain empty flag is invalid")


@dataclass(frozen=True, slots=True)
class BaselineHeader:
    digest: str
    remote_revision: int

    def __post_init__(self) -> None:
        DomainHeader(self.digest, self.remote_revision, False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_key(value: object) -> str:
    return re.sub(r"[-_]", "", str(value or "")).casefold()


def _looks_absolute_local_path(value: str) -> bool:
    return bool(
        _WINDOWS_ABSOLUTE_RE.match(value)
        or value.casefold().startswith("file:")
        or value.startswith("\\")
        or value.startswith("/")
        or value.startswith("~\\")
        or value.startswith("~/")
    )


def _validate_json_value(value: Any) -> None:
    """Reject non-JSON, secret-bearing, path-bearing and unbounded values."""

    nodes = 0

    def visit(item: Any, depth: int, parent_key: str = "") -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise UserStatePackageTooLarge("domain has too many JSON nodes")
        if depth > MAX_JSON_DEPTH:
            raise UserStatePackageTooLarge("domain JSON nesting is too deep")
        if item is None or isinstance(item, bool) or isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise UserStatePackageError("non-finite JSON numbers are forbidden")
            return
        if isinstance(item, str):
            # The package envelope contains each already-validated canonical
            # domain as payloadJson. It is bounded by per-domain/total limits,
            # not by the ordinary one-field text limit used inside a domain.
            if (
                parent_key != "payloadJson"
                and len(item.encode("utf-8")) > MAX_STRING_BYTES
            ):
                raise UserStatePackageTooLarge("one JSON string is too large")
            if _normalized_key(parent_key) in _PATH_KEYS and _looks_absolute_local_path(item):
                raise UserStatePackageError("absolute local paths are forbidden")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key:
                    raise UserStatePackageError("JSON object keys must be non-empty strings")
                normalized = _normalized_key(key)
                if normalized in _SENSITIVE_KEYS:
                    raise UserStatePackageError(
                        f"sensitive field is forbidden in user-state package: {key}"
                    )
                visit(child, depth + 1, key)
            return
        raise UserStatePackageError(
            f"unsupported JSON value type: {type(item).__name__}"
        )

    visit(value, 0)


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise UserStatePackageError("domain is not canonical JSON") from exc
    return text.encode("utf-8")


def _is_empty_payload(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _is_empty_domain(name: str, value: Any) -> bool:
    if name in ("highlights", "ink", "closed-regions") and isinstance(value, dict):
        return all(not value.get(host) for host in ("pdf", "epub"))
    return _is_empty_payload(value)


def _valid_ink_surface(value: str) -> bool:
    if _INK_SURFACE_RE.fullmatch(value):
        return True
    match = _EPUB_PDF_SURFACE_RE.fullmatch(value)
    if match is None:
        return False
    relative_book = match.group(1)
    return bool(
        relative_book
        and len(relative_book.encode("utf-8")) <= 512
        and not relative_book.startswith("/")
        and "\\" not in relative_book
        and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", relative_book)
        and all(part not in ("", ".", "..") for part in relative_book.split("/"))
    )


def _validate_domain_shape(name: str, value: Any) -> None:
    """Keep the server/App/runtime split and join rules unambiguous."""

    if name == "reading-position":
        if value is not None and not isinstance(value, dict):
            raise UserStatePackageError("reading-position must be an object or null")
        return
    if name in ("notes", "user-pages", "card-placements", "entity-references"):
        if not isinstance(value, list):
            raise UserStatePackageError(f"{name} must be an array")
        return
    if name == "highlights":
        if (
            not isinstance(value, dict)
            or set(value) != {"pdf", "epub"}
            or not isinstance(value.get("pdf"), list)
            or not isinstance(value.get("epub"), list)
        ):
            raise UserStatePackageError(
                "highlights must contain pdf and epub arrays"
            )
        return
    if name in ("ink", "closed-regions"):
        if not isinstance(value, dict) or set(value) != {"pdf", "epub"}:
            raise UserStatePackageError(f"{name} must contain pdf and epub maps")
        for host in ("pdf", "epub"):
            surfaces = value.get(host)
            if not isinstance(surfaces, dict) or any(
                not isinstance(key, str)
                or not _valid_ink_surface(key)
                or not isinstance(strokes, list)
                or any(
                    not isinstance(stroke, dict)
                    or (
                        (stroke.get("t") == "region")
                        != (name == "closed-regions")
                    )
                    for stroke in strokes
                )
                for key, strokes in surfaces.items()
            ):
                raise UserStatePackageError(f"{name}.{host} surface map is invalid")
        return
    raise UserStatePackageError(f"unknown user-state domain: {name}")


def _valid_revision(value: Any, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_REVISION
    ):
        raise UserStatePackageError("revision is invalid")
    return value


def _validate_identity(identity: ReaderStorageIdentity) -> None:
    if not isinstance(identity, ReaderStorageIdentity):
        raise UserStatePackageError("verified ReaderStorageIdentity required")


def build_package(
    *,
    book_id: str,
    content_sha256: str,
    revision: int,
    updated_at: str,
    domains: Mapping[str, Any],
    domain_revisions: Mapping[str, int],
) -> dict[str, Any]:
    """Build one complete, deterministic package from domain payloads."""

    if not BOOK_ID_RE.fullmatch(str(book_id or "")):
        raise UserStatePackageError("bookId is invalid")
    if not SHA256_RE.fullmatch(str(content_sha256 or "")):
        raise UserStatePackageError("contentSha256 is invalid")
    _valid_revision(revision)
    if not isinstance(updated_at, str) or not ISO_UTC_RE.fullmatch(updated_at):
        raise UserStatePackageError("updatedAt must be an ISO UTC timestamp")
    unknown = set(domains) - set(DOMAIN_NAMES)
    if unknown:
        raise UserStatePackageError(f"unknown user-state domain: {sorted(unknown)[0]}")
    if set(domain_revisions) != set(DOMAIN_NAMES):
        raise UserStatePackageError("every user-state domain needs a revision")

    raw_total = 0
    records: list[dict[str, Any]] = []
    for name in DOMAIN_NAMES:
        payload = domains.get(name)
        _validate_domain_shape(name, payload)
        payload_bytes = canonical_json_bytes(payload)
        byte_count = len(payload_bytes)
        if byte_count > DOMAIN_MAX_BYTES[name]:
            raise UserStatePackageTooLarge(f"{name} exceeds its byte limit")
        raw_total += byte_count
        if raw_total > MAX_RAW_DOMAIN_BYTES:
            raise UserStatePackageTooLarge("user-state domains exceed total limit")
        records.append({
            "name": name,
            "revision": _valid_revision(domain_revisions[name]),
            "digest": hashlib.sha256(payload_bytes).hexdigest(),
            "byteCount": byte_count,
            "empty": _is_empty_domain(name, payload),
            "payloadJson": payload_bytes.decode("utf-8"),
        })

    package = {
        "contract": CONTRACT,
        "bookId": book_id,
        "contentSha256": content_sha256,
        "revision": revision,
        "updatedAt": updated_at,
        "domains": records,
    }
    if len(canonical_json_bytes(package)) > MAX_PACKAGE_BYTES:
        raise UserStatePackageTooLarge("encoded user-state package is too large")
    return package


def encode_package(package: Mapping[str, Any]) -> bytes:
    validated = validate_package(package)
    payload = canonical_json_bytes(validated)
    if len(payload) > MAX_PACKAGE_BYTES:
        raise UserStatePackageTooLarge("encoded user-state package is too large")
    return payload


def decode_package(payload: bytes | bytearray | memoryview) -> dict[str, Any]:
    raw = bytes(payload)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise UserStatePackageTooLarge("encoded user-state package is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserStatePackageError("user-state package JSON is invalid") from exc
    return validate_package(value)


def validate_package(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "contract",
        "bookId",
        "contentSha256",
        "revision",
        "updatedAt",
        "domains",
    }:
        raise UserStatePackageError("user-state package fields are invalid")
    if value.get("contract") != CONTRACT:
        raise UserStatePackageError("user-state package contract is invalid")
    if not BOOK_ID_RE.fullmatch(str(value.get("bookId") or "")):
        raise UserStatePackageError("bookId is invalid")
    if not SHA256_RE.fullmatch(str(value.get("contentSha256") or "")):
        raise UserStatePackageError("contentSha256 is invalid")
    _valid_revision(value.get("revision"))
    if not isinstance(value.get("updatedAt"), str) or not ISO_UTC_RE.fullmatch(
        value["updatedAt"]
    ):
        raise UserStatePackageError("updatedAt is invalid")
    records = value.get("domains")
    if not isinstance(records, list) or len(records) != len(DOMAIN_NAMES):
        raise UserStatePackageError("user-state domains are incomplete")

    raw_total = 0
    names: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "name", "revision", "digest", "byteCount", "empty", "payloadJson"
        }:
            raise UserStatePackageError("user-state domain fields are invalid")
        name = record.get("name")
        if name not in DOMAIN_NAMES:
            raise UserStatePackageError("user-state domain name is invalid")
        names.append(name)
        _valid_revision(record.get("revision"))
        if not SHA256_RE.fullmatch(str(record.get("digest") or "")):
            raise UserStatePackageError("user-state domain digest is invalid")
        if not isinstance(record.get("empty"), bool):
            raise UserStatePackageError("user-state domain empty flag is invalid")
        payload_json = record.get("payloadJson")
        if not isinstance(payload_json, str):
            raise UserStatePackageError("user-state domain payloadJson is invalid")
        payload_bytes = payload_json.encode("utf-8")
        if (
            isinstance(record.get("byteCount"), bool)
            or record.get("byteCount") != len(payload_bytes)
            or len(payload_bytes) > DOMAIN_MAX_BYTES[name]
        ):
            raise UserStatePackageError("user-state domain byteCount is invalid")
        if hashlib.sha256(payload_bytes).hexdigest() != record["digest"]:
            raise UserStatePackageError("user-state domain digest mismatch")
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise UserStatePackageError("user-state domain payload is invalid JSON") from exc
        _validate_domain_shape(name, parsed)
        canonical = canonical_json_bytes(parsed)
        if canonical != payload_bytes:
            raise UserStatePackageError("user-state domain payload is not canonical")
        if _is_empty_domain(name, parsed) != record["empty"]:
            raise UserStatePackageError("user-state domain empty flag mismatch")
        raw_total += len(payload_bytes)
        if raw_total > MAX_RAW_DOMAIN_BYTES:
            raise UserStatePackageTooLarge("user-state domains exceed total limit")
    if tuple(names) != DOMAIN_NAMES:
        raise UserStatePackageError("user-state domains must use canonical order")
    return json.loads(json.dumps(value, ensure_ascii=False))


def package_headers(package: Mapping[str, Any]) -> dict[str, DomainHeader]:
    validated = validate_package(package)
    return {
        record["name"]: DomainHeader(
            digest=record["digest"],
            revision=record["revision"],
            empty=record["empty"],
        )
        for record in validated["domains"]
    }


def plan_import(
    *,
    package: Mapping[str, Any],
    local_headers: Mapping[str, DomainHeader],
    baseline_headers: Mapping[str, BaselineHeader],
    local_is_new_or_empty: bool = False,
) -> dict[str, Any]:
    """Classify every domain without ever selecting a conflict for overwrite."""

    remote = package_headers(package)
    unknown_local = set(local_headers) - set(DOMAIN_NAMES)
    unknown_baseline = set(baseline_headers) - set(DOMAIN_NAMES)
    if unknown_local or unknown_baseline:
        raise UserStatePackageError("unknown domain in local/baseline headers")

    decisions: list[dict[str, Any]] = []
    for name in DOMAIN_NAMES:
        pi_header = remote[name]
        local = local_headers.get(name)
        baseline = baseline_headers.get(name)

        if baseline is not None and (
            pi_header.revision < baseline.remote_revision
            or (
                pi_header.revision == baseline.remote_revision
                and pi_header.digest != baseline.digest
            )
        ):
            classification, action, reason = (
                "conflict", "keep", "Pi revision moved backward or changed in place"
            )
        elif local is not None and local.digest == pi_header.digest:
            classification, action, reason = (
                "unchanged", "keep", "local and Pi digests match"
            )
        elif local_is_new_or_empty and (local is None or local.empty):
            if pi_header.empty:
                classification, action, reason = (
                    "unchanged", "keep", "both domains are empty"
                )
            else:
                classification, action, reason = (
                    "pi-newer", "import", "new local book has no domain state"
                )
        elif baseline is None:
            if pi_header.empty:
                classification, action, reason = (
                    "local-newer", "keep", "Pi is empty and no baseline exists"
                )
            elif local is None or local.empty:
                classification, action, reason = (
                    "pi-newer", "import", "local domain is empty and no baseline exists"
                )
            else:
                classification, action, reason = (
                    "conflict", "keep", "both sides contain unbased state"
                )
        else:
            local_changed = local is None or local.digest != baseline.digest
            pi_changed = pi_header.digest != baseline.digest
            if not local_changed and not pi_changed:
                classification, action, reason = (
                    "unchanged", "keep", "both sides match the baseline"
                )
            elif local_changed and not pi_changed:
                classification, action, reason = (
                    "local-newer", "keep", "only local changed since baseline"
                )
            elif not local_changed and pi_changed:
                classification, action, reason = (
                    "pi-newer", "import", "only Pi changed since baseline"
                )
            else:
                classification, action, reason = (
                    "conflict", "keep", "local and Pi both changed since baseline"
                )

        decisions.append({
            "name": name,
            "classification": classification,
            "action": action,
            "reason": reason,
            "localDigest": None if local is None else local.digest,
            "localRevision": None if local is None else local.revision,
            "piDigest": pi_header.digest,
            "piRevision": pi_header.revision,
            "baselineDigest": None if baseline is None else baseline.digest,
            "baselinePiRevision": (
                None if baseline is None else baseline.remote_revision
            ),
        })

    return {
        "contract": PLAN_CONTRACT,
        "bookId": package["bookId"],
        "contentSha256": package["contentSha256"],
        "packageRevision": package["revision"],
        "hasConflicts": any(
            item["classification"] == "conflict" for item in decisions
        ),
        "decisions": decisions,
    }


class ReaderBookUserStateService:
    """Account-scoped deterministic exporter ready for an HTTP adapter."""

    def __init__(
        self,
        sidecar_store: SidecarStore,
        domain_reader: Callable[[ReaderStorageIdentity, str, str], Any],
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        if not isinstance(sidecar_store, SidecarStore):
            raise TypeError("SidecarStore required")
        if not callable(domain_reader):
            raise TypeError("domain_reader callback required")
        self.sidecar_store = sidecar_store
        self.domain_reader = domain_reader
        self.clock = clock

    def _metadata_path(
        self,
        identity: ReaderStorageIdentity,
        book_id: str,
        content_sha256: str,
    ) -> Path:
        return self.sidecar_store.account_path(
            identity,
            "reader-book-user-state",
            "books",
            f"{book_id}-{content_sha256}.json",
        )

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any] | None:
        value = read_json(path, default=None)
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or value.get("schema") != METADATA_SCHEMA
            or not BOOK_ID_RE.fullmatch(str(value.get("bookId") or ""))
            or not SHA256_RE.fullmatch(str(value.get("contentSha256") or ""))
            or not isinstance(value.get("domains"), dict)
        ):
            raise UserStatePackageError("user-state revision metadata is invalid")
        _valid_revision(value.get("revision"))
        if not isinstance(value.get("updatedAt"), str) or not ISO_UTC_RE.fullmatch(
            value["updatedAt"]
        ):
            raise UserStatePackageError("user-state revision timestamp is invalid")
        for name, record in value["domains"].items():
            if name not in DOMAIN_NAMES or not isinstance(record, dict):
                raise UserStatePackageError("user-state revision domain is invalid")
            if not isinstance(record.get("empty"), bool):
                raise UserStatePackageError("user-state revision empty flag is invalid")
            DomainHeader(
                str(record.get("digest") or ""),
                _valid_revision(record.get("revision")),
                record["empty"],
            )
        return value

    def export_package(
        self,
        *,
        identity: ReaderStorageIdentity,
        book_id: str,
        content_sha256: str,
        book_reference: str,
    ) -> dict[str, Any]:
        _validate_identity(identity)
        if not BOOK_ID_RE.fullmatch(str(book_id or "")):
            raise UserStatePackageError("bookId is invalid")
        if not SHA256_RE.fullmatch(str(content_sha256 or "")):
            raise UserStatePackageError("contentSha256 is invalid")
        if not isinstance(book_reference, str) or not book_reference:
            raise UserStatePackageError("private book reference required")

        lock_key = f"{book_id}:{content_sha256}"
        with self.sidecar_store.lock(identity, "reader-book-user-state", lock_key):
            metadata_path = self._metadata_path(identity, book_id, content_sha256)
            previous = self._read_metadata(metadata_path)
            values: dict[str, Any] = {}
            digests: dict[str, str] = {}
            empties: dict[str, bool] = {}
            revisions: dict[str, int] = {}
            changed = previous is None
            previous_domains = previous.get("domains", {}) if previous else {}

            for name in DOMAIN_NAMES:
                value = self.domain_reader(identity, book_reference, name)
                payload = canonical_json_bytes(value)
                if len(payload) > DOMAIN_MAX_BYTES[name]:
                    raise UserStatePackageTooLarge(f"{name} exceeds its byte limit")
                digest = hashlib.sha256(payload).hexdigest()
                prior = previous_domains.get(name)
                prior_digest = prior.get("digest") if isinstance(prior, dict) else None
                prior_revision = (
                    int(prior.get("revision"))
                    if isinstance(prior, dict) and isinstance(prior.get("revision"), int)
                    else 0
                )
                domain_changed = prior_digest != digest
                changed = changed or domain_changed
                values[name] = value
                digests[name] = digest
                empties[name] = _is_empty_domain(name, value)
                revisions[name] = max(1, prior_revision + (1 if domain_changed else 0))

            revision = 1 if previous is None else int(previous["revision"])
            updated_at = self.clock() if changed else str(previous["updatedAt"])
            if changed and previous is not None:
                revision += 1
            _valid_revision(revision)
            if not ISO_UTC_RE.fullmatch(str(updated_at or "")):
                raise UserStatePackageError("clock returned an invalid ISO UTC timestamp")

            metadata = {
                "schema": METADATA_SCHEMA,
                "bookId": book_id,
                "contentSha256": content_sha256,
                "revision": revision,
                "updatedAt": updated_at,
                "domains": {
                    name: {
                        "revision": revisions[name],
                        "digest": digests[name],
                        "empty": empties[name],
                    }
                    for name in DOMAIN_NAMES
                },
            }
            if changed or not metadata_path.exists():
                atomic_write_json(metadata_path, metadata, indent=2, mode=0o600)
            return build_package(
                book_id=book_id,
                content_sha256=content_sha256,
                revision=revision,
                updated_at=updated_at,
                domains=values,
                domain_revisions=revisions,
            )

    def export_bytes(self, **kwargs: Any) -> bytes:
        return encode_package(self.export_package(**kwargs))


def attachment_descriptor(
    package_bytes: bytes,
    *,
    download_url: str,
) -> dict[str, Any]:
    """Describe the package without exposing its account or filesystem root."""

    package = decode_package(package_bytes)
    if not isinstance(download_url, str):
        raise UserStatePackageError("user-state downloadUrl is invalid")
    parsed_url = urlsplit(download_url)
    expected_path = f"/pdf/api/library/user-state/{package['bookId']}"
    expected_query = [("contentSha256", package["contentSha256"])]
    if (
        parsed_url.scheme
        or parsed_url.netloc
        or parsed_url.fragment
        or parsed_url.path != expected_path
        or parse_qsl(parsed_url.query, keep_blank_values=True) != expected_query
    ):
        raise UserStatePackageError("user-state downloadUrl is invalid")
    return {
        "attachmentId": "user-state/package.json",
        "kind": "user-state",
        "category": "user-state",
        "mergePolicy": "per-domain-explicit",
        "mediaType": "application/json",
        "size": len(package_bytes),
        "sha256": hashlib.sha256(package_bytes).hexdigest(),
        "downloadUrl": download_url,
        "contract": CONTRACT,
        "revision": package["revision"],
    }


__all__ = [
    "BaselineHeader",
    "BOOK_ID_RE",
    "CONTRACT",
    "DOMAIN_MAX_BYTES",
    "DOMAIN_NAMES",
    "DomainHeader",
    "MAX_PACKAGE_BYTES",
    "PLAN_CONTRACT",
    "ReaderBookUserStateService",
    "UserStateImportConflict",
    "UserStatePackageError",
    "UserStatePackageTooLarge",
    "attachment_descriptor",
    "build_package",
    "canonical_json_bytes",
    "decode_package",
    "encode_package",
    "package_headers",
    "plan_import",
    "validate_package",
]
