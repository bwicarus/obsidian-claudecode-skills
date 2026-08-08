"""Persistent catalog for original PDF/EPUB files stored in the Reader vault.

The catalog is deliberately independent from Flask.  It assigns an opaque
``bookId`` to each real vault file and caches its content digest together with
``size`` and ``mtime_ns``.  Unchanged large books therefore do not get hashed
again on every catalog request.  A file which appears at a new path can reclaim
the id of one missing record with the same digest, preserving identity across a
normal rename without treating two simultaneously present copies as one book.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import uuid
import zipfile

from reader_sidecar_store import atomic_write_json, exclusive_lock, read_json


CATALOG_SCHEMA = "reader-book-library/1"
BOOK_ID_RE = re.compile(r"^book_[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
BOOK_KINDS = {".pdf": "pdf", ".epub": "epub"}
DEFAULT_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_MAX_BYTES_ENV = "READER_BOOK_LIBRARY_MAX_UPLOAD_BYTES"
UPLOAD_TARGET_PARTS = ("资源", "uploads")
MAX_EPUB_MEMBERS = 10_000
MAX_EPUB_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EPUB_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 200
DEFAULT_EXCLUDED_PREFIXES = ("资源/收藏夹/", "资源/uploads/.sandbox/")
DEFAULT_EXCLUDED_PDF_SUFFIXES = (".orig.pdf", ".compressed.pdf")


class BookLibraryError(RuntimeError):
    """Base class for local-library contract failures."""


class CatalogCorruptError(BookLibraryError):
    """The persistent catalog exists but violates its schema."""


class UnsafeLibraryPathError(ValueError, BookLibraryError):
    """A caller supplied a path outside the bounded vault namespace."""


class UnsupportedBookError(ValueError, BookLibraryError):
    """The upload is not an original PDF or EPUB."""


class InvalidBookContentError(ValueError, BookLibraryError):
    """The file extension and the bounded format signature disagree."""


class UploadTooLargeError(ValueError, BookLibraryError):
    """The request or streamed file exceeded the configured upload bound."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = int(max_bytes)
        super().__init__(f"book upload exceeds {self.max_bytes} bytes")


class UnknownBookError(KeyError, BookLibraryError):
    """No currently present book owns this id."""


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _valid_relative_parts(value: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    raw = str(value or "").replace("\\", "/")
    if _has_control_character(raw):
        raise UnsafeLibraryPathError("control characters are not allowed")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise UnsafeLibraryPathError("absolute paths are not allowed")
    raw = raw.rstrip("/")
    if not raw:
        if allow_empty:
            return ()
        raise UnsafeLibraryPathError("relative path is required")
    raw_parts = raw.split("/")
    if (
        not raw_parts
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise UnsafeLibraryPathError("unsafe relative path")
    return tuple(PurePosixPath(raw).parts)


def _valid_epub_member_name(value: str) -> bool:
    raw = str(value or "")
    if (
        not raw
        or "\\" in raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or _has_control_character(raw)
    ):
        return False
    normalized = raw[:-1] if raw.endswith("/") else raw
    return bool(normalized) and all(
        part not in ("", ".", "..")
        for part in normalized.split("/")
    )


def _reject_symlink_chain(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise UnsafeLibraryPathError("path is outside vault") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise UnsafeLibraryPathError("symlink paths are not library books")
    try:
        target.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafeLibraryPathError("resolved path is outside vault") from exc


def _safe_existing_file(root: Path, rel: str) -> Path:
    parts = _valid_relative_parts(rel)
    target = root.joinpath(*parts)
    _reject_symlink_chain(root, target)
    if not target.exists() or not target.is_file():
        raise UnknownBookError(rel)
    return target


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def configured_upload_max_bytes() -> int:
    raw = os.environ.get(UPLOAD_MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_UPLOAD_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_UPLOAD_MAX_BYTES
    return value if value > 0 else DEFAULT_UPLOAD_MAX_BYTES


def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def _record_fingerprint(record: dict) -> tuple[int, int, int, int, int] | None:
    keys = ("size", "mtimeNs", "ctimeNs", "device", "inode")
    if any(not isinstance(record.get(key), int) for key in keys):
        return None
    return tuple(int(record[key]) for key in keys)  # type: ignore[return-value]


def _stable_sha256(path: Path) -> tuple[str, os.stat_result]:
    """Hash one stable file, retrying once if it changed during the read."""
    for _attempt in range(2):
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = path.stat()
        if _stat_fingerprint(before) == _stat_fingerprint(after):
            return digest.hexdigest(), after
    raise BookLibraryError("book changed while hashing")


def _record_is_valid(book_id: str, record: object) -> bool:
    if not BOOK_ID_RE.fullmatch(str(book_id or "")) or not isinstance(record, dict):
        return False
    try:
        _valid_relative_parts(record.get("rel", ""))
    except UnsafeLibraryPathError:
        return False
    fingerprint_values = [record.get(key) for key in ("ctimeNs", "device", "inode")]
    fingerprint_valid = all(value is None for value in fingerprint_values) or all(
        isinstance(value, int) and value >= 0 for value in fingerprint_values
    )
    return (
        record.get("kind") in ("pdf", "epub")
        and isinstance(record.get("name"), str)
        and 0 < len(record["name"]) <= 255
        and isinstance(record.get("size"), int)
        and record["size"] >= 0
        and isinstance(record.get("mtimeNs"), int)
        and record["mtimeNs"] >= 0
        and fingerprint_valid
        and SHA256_RE.fullmatch(str(record.get("contentSha256") or "")) is not None
        and isinstance(record.get("present"), bool)
    )


class BookLibrary:
    """Catalog, resolve and atomically ingest original Reader books."""

    def __init__(
        self,
        vault_root: str | Path,
        state_root: str | Path,
        *,
        excluded_prefixes: tuple[str, ...] = DEFAULT_EXCLUDED_PREFIXES,
        excluded_pdf_suffixes: tuple[str, ...] = DEFAULT_EXCLUDED_PDF_SUFFIXES,
        max_upload_bytes: int | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser()
        self.state_root = Path(state_root).expanduser()
        self.catalog_path = self.state_root / "catalog.json"
        self.lock_path = self.state_root / "catalog.lock"
        self.excluded_prefixes = tuple(excluded_prefixes)
        self.excluded_pdf_suffixes = tuple(s.lower() for s in excluded_pdf_suffixes)
        configured_max = (
            configured_upload_max_bytes()
            if max_upload_bytes is None
            else int(max_upload_bytes)
        )
        if configured_max <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self.max_upload_bytes = configured_max

    def _load_records(self) -> dict[str, dict]:
        try:
            payload = read_json(self.catalog_path)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            raise CatalogCorruptError("book catalog cannot be read") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != CATALOG_SCHEMA
            or not isinstance(payload.get("records"), dict)
        ):
            raise CatalogCorruptError("book catalog schema is invalid")
        records = payload["records"]
        if any(not _record_is_valid(book_id, record) for book_id, record in records.items()):
            raise CatalogCorruptError("book catalog record is invalid")
        return {str(book_id): dict(record) for book_id, record in records.items()}

    def _write_records(self, records: dict[str, dict]) -> None:
        atomic_write_json(
            self.catalog_path,
            {"schema": CATALOG_SCHEMA, "records": records},
            indent=2,
            mode=0o600,
        )

    def _is_catalog_book(self, path: Path) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        kind = BOOK_KINDS.get(path.suffix.lower())
        if not kind:
            return False
        rel = path.relative_to(self.vault_root).as_posix()
        try:
            _valid_relative_parts(rel)
        except UnsafeLibraryPathError:
            return False
        if any(rel.startswith(prefix) for prefix in self.excluded_prefixes):
            return False
        if kind == "pdf" and path.name.lower().endswith(self.excluded_pdf_suffixes):
            return False
        return True

    def _vault_books(self) -> list[Path]:
        if not self.vault_root.exists() or not self.vault_root.is_dir():
            raise BookLibraryError("vault root is unavailable")
        books: list[Path] = []
        candidates = list(self.vault_root.rglob("*.[pP][dD][fF]"))
        candidates.extend(self.vault_root.rglob("*.[eE][pP][uU][bB]"))
        for path in candidates:
            try:
                if self._is_catalog_book(path):
                    _reject_symlink_chain(self.vault_root, path)
                    books.append(path)
            except (OSError, UnsafeLibraryPathError):
                continue
        return sorted(books, key=lambda item: item.relative_to(self.vault_root).as_posix())

    @staticmethod
    def _public_entry(book_id: str, record: dict) -> dict:
        content_sha256 = record["contentSha256"]
        return {
            "bookId": book_id,
            "name": record["name"],
            "kind": record["kind"],
            "rel": record["rel"],
            "size": record["size"],
            "mtime": record["mtimeNs"] // 1_000_000_000,
            "version": content_sha256,
            "contentSha256": content_sha256,
            "downloadUrl": f"/pdf/api/library/download/{book_id}",
            # One versioned manifest may grow additional attachment domains in
            # future.  This release publishes derived OCR/formula data only.
            "attachmentsUrl": (
                f"/pdf/api/library/attachments/{book_id}"
                f"?contentSha256={content_sha256}"
            ),
        }

    def _catalog_locked(self) -> tuple[list[dict], dict[str, dict]]:
        records = self._load_records()
        paths = self._vault_books()
        actual_rels = {
            path.relative_to(self.vault_root).as_posix()
            for path in paths
        }
        by_rel = {record["rel"]: book_id for book_id, record in records.items()}
        missing_ids = {
            book_id
            for book_id, record in records.items()
            if record["rel"] not in actual_rels
        }
        changed = False
        current_ids: set[str] = set()

        for path in paths:
            rel = path.relative_to(self.vault_root).as_posix()
            kind = BOOK_KINDS[path.suffix.lower()]
            stat = path.stat()
            book_id = by_rel.get(rel)
            record = records.get(book_id) if book_id else None
            if (
                record is not None
                and _record_fingerprint(record) == _stat_fingerprint(stat)
                and record["kind"] == kind
            ):
                digest = record["contentSha256"]
            else:
                digest, stat = _stable_sha256(path)

            if record is None:
                # A normal rename leaves exactly one missing record with this
                # content.  Do not merge two simultaneously present copies.
                candidates = [
                    candidate
                    for candidate in missing_ids
                    if records[candidate]["contentSha256"] == digest
                    and records[candidate]["kind"] == kind
                    and records[candidate]["size"] == stat.st_size
                ]
                if len(candidates) == 1:
                    book_id = candidates[0]
                    missing_ids.remove(book_id)
                    record = records[book_id]
                else:
                    book_id = "book_" + uuid.uuid4().hex
                    record = {}
                    records[book_id] = record
                changed = True

            next_record = {
                "rel": rel,
                "name": path.name,
                "kind": kind,
                "size": int(stat.st_size),
                "mtimeNs": int(stat.st_mtime_ns),
                "ctimeNs": int(stat.st_ctime_ns),
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "contentSha256": digest,
                "present": True,
            }
            if record != next_record:
                records[book_id] = next_record
                changed = True
            current_ids.add(book_id)

        for book_id, record in records.items():
            if book_id not in current_ids and record.get("present"):
                record["present"] = False
                changed = True

        if changed or not self.catalog_path.exists():
            self._write_records(records)
        entries = [
            self._public_entry(book_id, records[book_id])
            for book_id in current_ids
        ]
        entries.sort(key=lambda entry: (entry["name"].casefold(), entry["rel"].casefold()))
        return entries, records

    def catalog(self) -> list[dict]:
        with exclusive_lock(self.lock_path):
            entries, _records = self._catalog_locked()
            return entries

    def resolve(self, book_id: str) -> tuple[dict, Path]:
        if not BOOK_ID_RE.fullmatch(str(book_id or "")):
            raise UnknownBookError(book_id)
        with exclusive_lock(self.lock_path):
            entries, records = self._catalog_locked()
            entry = next((item for item in entries if item["bookId"] == book_id), None)
            if entry is None:
                raise UnknownBookError(book_id)
            path = _safe_existing_file(self.vault_root, entry["rel"])
            if BOOK_KINDS.get(path.suffix.lower()) != entry["kind"]:
                raise UnknownBookError(book_id)
            if _record_fingerprint(records[book_id]) != _stat_fingerprint(path.stat()):
                # The file changed after the catalog pass. Rebuild its digest
                # once, then require the refreshed fingerprint to remain stable
                # before returning a path to the HTTP layer.
                entries, records = self._catalog_locked()
                entry = next((item for item in entries if item["bookId"] == book_id), None)
                if entry is None:
                    raise UnknownBookError(book_id)
                path = _safe_existing_file(self.vault_root, entry["rel"])
                if _record_fingerprint(records[book_id]) != _stat_fingerprint(path.stat()):
                    raise BookLibraryError("book changed while resolving download")
            return entry, path

    def _safe_target_dir(self, target_dir: str) -> Path:
        parts = _valid_relative_parts(target_dir)
        if parts != UPLOAD_TARGET_PARTS:
            raise UnsafeLibraryPathError("uploads are restricted to 资源/uploads")
        target = self.vault_root.joinpath(*parts)
        _reject_symlink_chain(self.vault_root, target)
        target.mkdir(parents=True, exist_ok=True)
        _reject_symlink_chain(self.vault_root, target)
        if not target.is_dir():
            raise UnsafeLibraryPathError("upload target is not a directory")
        return target

    @staticmethod
    def _validate_content(path: Path, kind: str) -> None:
        if path.stat().st_size <= 0:
            raise InvalidBookContentError("empty book")
        if kind == "pdf":
            with path.open("rb") as handle:
                if b"%PDF-" not in handle.read(1024):
                    raise InvalidBookContentError("invalid PDF signature")
            return
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > MAX_EPUB_MEMBERS:
                    raise InvalidBookContentError("EPUB has too many members")
                total_size = 0
                mimetypes = []
                for member in members:
                    name = member.filename
                    if not _valid_epub_member_name(name):
                        raise InvalidBookContentError("unsafe EPUB member path")
                    unix_mode = (member.external_attr >> 16) & 0o170000
                    if unix_mode == stat.S_IFLNK:
                        raise InvalidBookContentError("EPUB symlinks are not allowed")
                    if member.flag_bits & 0x1:
                        raise InvalidBookContentError("encrypted EPUB is not supported")
                    if member.file_size > MAX_EPUB_MEMBER_BYTES:
                        raise InvalidBookContentError("EPUB member is too large")
                    total_size += int(member.file_size)
                    if total_size > MAX_EPUB_TOTAL_BYTES:
                        raise InvalidBookContentError("EPUB expands beyond the safe limit")
                    if member.file_size > 0 and (
                        member.compress_size <= 0
                        or member.file_size / member.compress_size > MAX_EPUB_COMPRESSION_RATIO
                    ):
                        raise InvalidBookContentError("EPUB compression ratio is unsafe")
                    if name == "mimetype":
                        mimetypes.append(member)
                if len(mimetypes) != 1:
                    raise InvalidBookContentError("invalid EPUB mimetype entry")
                mimetype = mimetypes[0]
                if (
                    mimetype.compress_type != zipfile.ZIP_STORED
                    or mimetype.file_size != 20
                    or mimetype.compress_size != 20
                    or archive.read(mimetype) != b"application/epub+zip"
                ):
                    raise InvalidBookContentError("invalid EPUB mimetype")
        except InvalidBookContentError:
            raise
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise InvalidBookContentError("invalid EPUB container") from exc

    @staticmethod
    def _activate_without_overwrite(
        temporary: Path,
        directory: Path,
        safe_name: str,
        extension: str,
    ) -> Path:
        """Atomically publish one same-directory temp inode without replacement."""
        stem = safe_name[: -len(extension)]
        for index in range(0, 10_000):
            candidate = (
                directory / safe_name
                if index == 0
                else directory / f"{stem}-{index}{extension}"
            )
            try:
                # Hard-link creation is atomic and fails if another writer has
                # claimed the name.  Both paths are in one directory, so no
                # cross-device fallback (which could expose a partial file) is
                # permitted.
                os.link(temporary, candidate)
            except FileExistsError:
                continue
            else:
                temporary.unlink()
                return candidate
        raise BookLibraryError("no available destination name")

    def ingest(
        self,
        stream,
        original_filename: str,
        *,
        target_dir: str,
        sanitize_filename,
    ) -> tuple[dict, bool]:
        leaf = str(original_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        extension = Path(leaf).suffix.lower()
        kind = BOOK_KINDS.get(extension)
        if kind is None:
            raise UnsupportedBookError("only PDF and EPUB are accepted")
        safe_stem = str(sanitize_filename(Path(leaf).stem) or "").strip()
        if not safe_stem:
            safe_stem = "untitled"
        safe_name = safe_stem + extension
        directory = self._safe_target_dir(target_dir)
        temporary = directory / f".bw-library-upload-{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                while True:
                    chunk = stream.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise BookLibraryError("upload stream returned non-bytes")
                    if size + len(chunk) > self.max_upload_bytes:
                        raise UploadTooLargeError(self.max_upload_bytes)
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_content(temporary, kind)
            content_sha = digest.hexdigest()

            with exclusive_lock(self.lock_path):
                entries, records = self._catalog_locked()
                duplicate = next(
                    (
                        entry
                        for entry in entries
                        if entry["kind"] == kind
                        and entry["size"] == size
                        and entry["contentSha256"] == content_sha
                    ),
                    None,
                )
                if duplicate is not None:
                    return duplicate, True

                destination = self._activate_without_overwrite(
                    temporary,
                    directory,
                    safe_name,
                    extension,
                )
                _fsync_dir(directory)
                stat = destination.stat()
                rel = destination.relative_to(self.vault_root).as_posix()
                book_id = "book_" + uuid.uuid4().hex
                record = {
                    "rel": rel,
                    "name": destination.name,
                    "kind": kind,
                    "size": int(stat.st_size),
                    "mtimeNs": int(stat.st_mtime_ns),
                    "ctimeNs": int(stat.st_ctime_ns),
                    "device": int(stat.st_dev),
                    "inode": int(stat.st_ino),
                    "contentSha256": content_sha,
                    "present": True,
                }
                records[book_id] = record
                self._write_records(records)
                return self._public_entry(book_id, record), False
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "BOOK_ID_RE",
    "BookLibrary",
    "BookLibraryError",
    "CatalogCorruptError",
    "InvalidBookContentError",
    "UploadTooLargeError",
    "UnknownBookError",
    "UnsafeLibraryPathError",
    "UnsupportedBookError",
]
