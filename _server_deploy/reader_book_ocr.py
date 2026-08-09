"""Authenticated, manual-only OCR orchestration for catalogued Reader books.

The HTTP adapter must resolve an opaque ``bookId`` through ``BookLibrary`` and
must also supply the exact catalog ``contentSha256``.  This module never accepts
a vault-relative or absolute path from a client.  Paths only cross the boundary
between the trusted catalog and the detached worker process.

OCR output is derived data.  It is stored as per-page ``page-chars`` sidecars
and never replaces or edits the original PDF.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

from reader_book_library import BOOK_ID_RE, SHA256_RE, BookLibrary, BookLibraryError, UnknownBookError
from reader_sidecar_store import atomic_write_json, exclusive_lock, read_json


CONTRACT = "reader-library-ocr/1"
ADOPTION_CONTRACT = "reader-library-ocr-adoption/1"
ENGINES = frozenset(("vision", "manga"))
LEGACY_ENGINE = "legacy"
RESULT_ENGINES = ENGINES | frozenset((LEGACY_ENGINE,))
ACTIVE_STATES = frozenset(("queued", "running", "pause-requested", "cancel-requested"))
TERMINAL_STATES = frozenset(("paused", "cancelled", "succeeded", "failed"))
CONTROL_STATES = frozenset(("running", "paused", "cancelled"))
MAX_PDF_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024
MAX_PAGES_DEFAULT = 5000
MAX_ADOPTION_BYTES_DEFAULT = 512 * 1024 * 1024
MAX_ADOPTION_PAGE_BYTES = 64 * 1024 * 1024
PUBLICATION_CONTRACT = "reader-book-ocr-publication/1"


class ReaderBookOcrError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        self.code = str(code)
        self.status = int(status)
        super().__init__(message)


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pid_alive(pid) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        stat_path = Path("/proc") / str(value) / "stat"
        state = stat_path.read_bytes().rsplit(b")", 1)[1].split()[0]
        if state in (b"Z", b"X", b"x"):
            return False
    except Exception:
        pass
    return True


def _process_start_token(pid) -> str | None:
    if os.name != "posix":
        return None
    try:
        fields = (Path("/proc") / str(int(pid)) / "stat").read_text("utf-8").rsplit(
            ")", 1
        )[1].split()
        return str(fields[19])
    except Exception:
        return None


def _safe_public_job(job: dict) -> dict:
    """Return the stable wire shape and never leak a filesystem path."""
    keys = (
        "jobId", "bookId", "contentSha256", "engine", "state", "phase",
        "processedPages", "totalPages", "successfulPages", "failedPages",
        "recognizedPages", "percent", "etaSeconds", "message", "canPause",
        "canResume", "canCancel", "canRetry", "createdAtEpochMs",
        "updatedAtEpochMs", "resultAvailable", "pageCharsRevision",
        "pauseMode", "textState", "formulaState", "formulaTotal",
        "formulaRecognized", "formulaPendingRegions", "formulaFailedRegions",
        "currentPage", "errorCode", "error",
    )
    out = {key: job.get(key) for key in keys}

    def _public_progress(name: str) -> dict:
        value = job.get(name) if isinstance(job.get(name), dict) else {}
        result = {}
        for field in ("total", "completed", "pending", "failed", "unavailable"):
            try:
                result[field] = max(0, int(value.get(field) or 0))
            except (TypeError, ValueError):
                result[field] = 0
        return result

    out["textProgress"] = _public_progress("textProgress")
    out["wordProgress"] = _public_progress("wordProgress")
    out["formulaProgress"] = _public_progress("formulaProgress")
    defaults = {
        "state": "idle", "phase": "idle", "processedPages": 0,
        "totalPages": 0, "successfulPages": 0, "failedPages": 0,
        "recognizedPages": 0, "percent": 0, "etaSeconds": None,
        "message": "尚未开始 Pi 预处理", "canPause": False,
        "canResume": False, "canCancel": False, "canRetry": False,
        "resultAvailable": False, "pauseMode": "checkpoint-restart",
        "textState": "idle", "formulaState": "idle", "formulaTotal": 0,
        "formulaRecognized": 0, "formulaPendingRegions": 0,
        "formulaFailedRegions": 0, "currentPage": None,
    }
    for key, default in defaults.items():
        if out.get(key) is None:
            out[key] = default
    if out.get("error"):
        error = re.sub(r"[\r\n\t]+", " ", str(out["error"]))[:300]
        error = re.sub(
            r"(?i)(authorization|bearer|api[-_ ]?key|access[-_ ]?token)\s*[:=]?\s*\S+",
            r"\1=<redacted>",
            error,
        )
        error = re.sub(r"(?:[A-Za-z]:\\|/(?:home|tmp|var|opt|srv)/)[^\r\n]*", "<path>", error)
        out["error"] = error
    out.pop("sourcePath", None)
    out.pop("pid", None)
    return out


@dataclass(frozen=True)
class ResolvedOcrBook:
    entry: dict
    path: Path


class ReaderBookOcrService:
    """Persistent job state plus a detached, checkpointed worker."""

    def __init__(
        self,
        library: BookLibrary,
        state_root: str | Path,
        project_root: str | Path,
        *,
        launcher=None,
        max_pdf_bytes: int | None = None,
        max_pages: int | None = None,
        legacy_page_count_reader=None,
        legacy_embedded_page_reader=None,
        legacy_language_resolver=None,
        legacy_char_cache_version: int | None = None,
        max_adoption_bytes: int | None = None,
    ) -> None:
        self.library = library
        self.state_root = Path(state_root)
        self.project_root = Path(project_root)
        self.lock_path = self.state_root / "jobs.lock"
        self._owns_launcher = launcher is None
        self.launcher = launcher or self._launch_process
        self.max_pdf_bytes = int(max_pdf_bytes or _env_positive_int(
            "READER_BOOK_OCR_MAX_PDF_BYTES", MAX_PDF_BYTES_DEFAULT
        ))
        self.max_pages = int(max_pages or _env_positive_int(
            "READER_BOOK_OCR_MAX_PAGES", MAX_PAGES_DEFAULT
        ))
        self.legacy_page_count_reader = legacy_page_count_reader
        self.legacy_embedded_page_reader = legacy_embedded_page_reader
        self.legacy_language_resolver = legacy_language_resolver
        self.legacy_char_cache_version = (
            int(legacy_char_cache_version)
            if legacy_char_cache_version is not None
            else None
        )
        self.max_adoption_bytes = int(max_adoption_bytes or _env_positive_int(
            "READER_BOOK_OCR_MAX_ADOPTION_BYTES", MAX_ADOPTION_BYTES_DEFAULT
        ))
        self._adoption_singleflight = threading.Lock()
        self._verified_source_cache: dict[tuple, bool] = {}

    @staticmethod
    def _validate_identity(book_id: str, content_sha256: str) -> None:
        if not BOOK_ID_RE.fullmatch(str(book_id or "")):
            raise ReaderBookOcrError("invalid-book-id", "invalid bookId", status=400)
        if not SHA256_RE.fullmatch(str(content_sha256 or "")):
            raise ReaderBookOcrError(
                "invalid-content-sha", "invalid contentSha256", status=400
            )

    def resolve(self, book_id: str, content_sha256: str) -> ResolvedOcrBook:
        self._validate_identity(book_id, content_sha256)
        try:
            entry, path = self.library.resolve(book_id)
        except UnknownBookError as exc:
            raise ReaderBookOcrError("book-not-found", "book not found", status=404) from exc
        except BookLibraryError as exc:
            raise ReaderBookOcrError(
                "book-catalog-unavailable", "book catalog unavailable", status=503
            ) from exc
        if entry.get("contentSha256") != content_sha256:
            raise ReaderBookOcrError(
                "book-version-changed",
                "book version changed; refresh the library before preprocessing",
                status=409,
            )
        if entry.get("kind") != "pdf" or path.suffix.lower() != ".pdf":
            raise ReaderBookOcrError(
                "unsupported-book-kind",
                "Pi preprocessing currently supports PDF books only",
                status=400,
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ReaderBookOcrError("book-unavailable", "book is unavailable", status=503) from exc
        if size <= 0 or size > self.max_pdf_bytes:
            raise ReaderBookOcrError(
                "book-size-limit",
                f"PDF size exceeds the Pi preprocessing limit ({self.max_pdf_bytes} bytes)",
                status=413,
            )
        return ResolvedOcrBook(entry=entry, path=path)

    def _version_dir(self, book_id: str, content_sha256: str) -> Path:
        self._validate_identity(book_id, content_sha256)
        return self.state_root / book_id / content_sha256

    def _job_dir(self, book_id: str, content_sha256: str, engine: str) -> Path:
        if engine not in ENGINES:
            raise ReaderBookOcrError("invalid-engine", "unsupported OCR engine", status=400)
        return self._version_dir(book_id, content_sha256) / engine

    @staticmethod
    def _read_optional(path: Path) -> dict | None:
        try:
            value = read_json(path)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def _read_bounded_legacy_json(self, path: Path, source_bytes: list[int]):
        """Read one legacy sidecar without allowing an unbounded allocation."""
        try:
            before = path.stat()
        except OSError as exc:
            raise ReaderBookOcrError(
                "legacy-result-unavailable", "old preprocessing data is unavailable", status=503
            ) from exc
        if before.st_size < 0 or before.st_size > MAX_ADOPTION_PAGE_BYTES:
            raise ReaderBookOcrError(
                "legacy-result-too-large", "one old sidecar exceeds the adoption limit", status=413
            )
        source_bytes[0] += int(before.st_size)
        if source_bytes[0] > self.max_adoption_bytes:
            raise ReaderBookOcrError(
                "legacy-result-too-large", "old source sidecars exceed the adoption limit", status=413
            )
        try:
            payload = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise ReaderBookOcrError(
                "legacy-result-unavailable", "old preprocessing data is unavailable", status=503
            ) from exc
        if (
            self._stat_fingerprint(before) != self._stat_fingerprint(after)
            or len(payload) != int(before.st_size)
        ):
            raise ReaderBookOcrError(
                "legacy-result-changed", "old preprocessing data changed while reading", status=409
            )
        return json.loads(payload.decode("utf-8"))

    @staticmethod
    def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
            int(value.st_dev),
            int(value.st_ino),
        )

    @staticmethod
    def _source_identity(value: os.stat_result) -> dict:
        return {
            "device": int(value.st_dev),
            "inode": int(value.st_ino),
            "size": int(value.st_size),
            "mtimeNs": int(value.st_mtime_ns),
        }

    @classmethod
    def _source_fingerprint(cls, value: os.stat_result) -> tuple[int, int, int, int]:
        """Compare one source across fd/path stat without trusting ctime precision.

        Windows can report the same change time through ``fstat`` and ``stat``
        with slightly different nanosecond rounding.  Device/inode/size/mtime
        keep the publication-window identity check stable; the full SHA-256 is
        still verified separately before a release becomes visible.
        """
        identity = cls._source_identity(value)
        return (
            identity["device"],
            identity["inode"],
            identity["size"],
            identity["mtimeNs"],
        )

    def _verify_current_source_content(
        self, resolved: ResolvedOcrBook, content_sha256: str
    ) -> None:
        try:
            stat = resolved.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ReaderBookOcrError(
                "book-unavailable", "book is unavailable", status=503
            ) from exc
        key = (
            str(resolved.path),
            content_sha256,
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            # This cache compares Path.stat with later Path.stat calls, so it
            # can retain the exact ctime.  Only fd-vs-path publication checks
            # omit ctime because Windows rounds those two surfaces differently.
            int(stat.st_ctime_ns),
        )
        if self._verified_source_cache.get(key):
            return
        guard = self._open_source_guard(resolved, content_sha256)
        try:
            if len(self._verified_source_cache) >= 64:
                self._verified_source_cache.clear()
            self._verified_source_cache[key] = True
        finally:
            self._close_source_guard(guard)

    def _open_source_guard(
        self, resolved: ResolvedOcrBook, content_sha256: str
    ) -> dict:
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(resolved.path), flags)
            before = os.fstat(fd)
            digest = hashlib.sha256()
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), "rb") as handle:
                for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
            os.lseek(fd, 0, os.SEEK_SET)
            after = os.fstat(fd)
            path_stat = resolved.path.stat(follow_symlinks=False)
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            raise ReaderBookOcrError(
                "book-unavailable", "book is unavailable", status=503
            ) from exc
        identity = self._source_identity(after)
        if (
            self._source_identity(before) != identity
            or self._source_identity(path_stat) != identity
            or digest.hexdigest() != content_sha256
        ):
            os.close(fd)
            raise ReaderBookOcrError(
                "book-version-changed", "book content changed while verifying", status=409
            )
        return {
            "fd": fd,
            "path": resolved.path,
            "contentSha256": content_sha256,
            "identity": identity,
        }

    def _assert_source_guard(self, guard: dict, *, rehash: bool) -> None:
        try:
            fd_stat = os.fstat(int(guard["fd"]))
            path_stat = Path(guard["path"]).stat(follow_symlinks=False)
            if (
                self._source_identity(fd_stat) != guard["identity"]
                or self._source_identity(path_stat) != guard["identity"]
            ):
                raise ReaderBookOcrError(
                    "book-version-changed", "book changed before publication", status=409
                )
            if rehash:
                digest = hashlib.sha256()
                os.lseek(int(guard["fd"]), 0, os.SEEK_SET)
                with os.fdopen(os.dup(int(guard["fd"])), "rb") as handle:
                    for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                        digest.update(chunk)
                os.lseek(int(guard["fd"]), 0, os.SEEK_SET)
                if digest.hexdigest() != guard["contentSha256"]:
                    raise ReaderBookOcrError(
                        "book-version-changed", "book changed before publication", status=409
                    )
        except ReaderBookOcrError:
            raise
        except OSError as exc:
            raise ReaderBookOcrError(
                "book-version-changed", "book changed before publication", status=409
            ) from exc

    @staticmethod
    def _close_source_guard(guard: dict | None) -> None:
        if guard is not None:
            try:
                os.close(int(guard["fd"]))
            except OSError:
                pass

    def _verify_actual_identity(
        self,
        resolved: ResolvedOcrBook,
        content_sha256: str,
    ) -> tuple[int, int, int, int]:
        """Hash the resolved catalog file without trusting a cached catalog digest."""
        guard = self._open_source_guard(resolved, content_sha256)
        try:
            return self._source_fingerprint(os.fstat(int(guard["fd"])))
        finally:
            self._close_source_guard(guard)

    def _legacy_page_count(self, path: Path) -> int:
        try:
            if self.legacy_page_count_reader is not None:
                total = int(self.legacy_page_count_reader(path))
            else:
                import fitz

                document = fitz.open(str(path))
                try:
                    total = int(document.page_count)
                finally:
                    document.close()
        except Exception as exc:
            raise ReaderBookOcrError(
                "legacy-page-read-failed", "PDF pages could not be inspected", status=500
            ) from exc
        if total <= 0 or total > self.max_pages:
            raise ReaderBookOcrError(
                "book-page-limit", "PDF page count exceeds the preprocessing limit", status=413
            )
        return total

    @staticmethod
    def _normalize_legacy_page(
        value,
        *,
        book_id: str,
        content_sha256: str,
        page_number: int,
        source: str,
    ) -> tuple[dict | None, int]:
        if isinstance(value, (tuple, list)) and len(value) == 4:
            chars, page_w, page_h, furigana = value
            value = {
                "chars": chars,
                "page_w": page_w,
                "page_h": page_h,
                "furigana": furigana,
            }
        if not isinstance(value, dict):
            return None, 0
        chars = value.get("chars")
        furigana = value.get("furigana", [])
        try:
            page_w = float(value.get("page_w"))
            page_h = float(value.get("page_h"))
        except (TypeError, ValueError):
            return None, 0
        if (
            not math.isfinite(page_w)
            or not math.isfinite(page_h)
            or page_w <= 0
            or page_h <= 0
            or not isinstance(chars, list)
            or len(chars) > 2_000_000
            or not isinstance(furigana, list)
            or len(furigana) > 2_000_000
        ):
            return None, 0
        for char in chars:
            if not isinstance(char, dict) or not isinstance(char.get("c"), str):
                return None, 0
            if len(char["c"]) > 32:
                return None, 0
            try:
                geometry = [float(char.get(key)) for key in ("x0", "y0", "x1", "y1")]
            except (TypeError, ValueError):
                return None, 0
            if (
                not all(math.isfinite(number) and abs(number) <= 10_000_000 for number in geometry)
                or geometry[0] > geometry[2]
                or geometry[1] > geometry[3]
            ):
                return None, 0
        normalized = {
            "schema": "reader-page-chars/1",
            "bookId": book_id,
            "contentSha256": content_sha256,
            "engine": LEGACY_ENGINE,
            "pageNumber": page_number,
            "page_w": page_w,
            "page_h": page_h,
            "chars": chars,
            "furigana": furigana,
            "textCharCount": len("".join(
                str(item.get("c") or "") for item in chars if not item.get("sp")
            )),
            "legacySource": source,
        }
        try:
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None, 0
        if len(encoded) > MAX_ADOPTION_PAGE_BYTES:
            raise ReaderBookOcrError(
                "legacy-page-too-large", "one old page layer exceeds the adoption limit", status=413
            )
        return normalized, len(encoded)

    def _legacy_language(self, rel: str) -> str | None:
        if self.legacy_language_resolver is None:
            return None
        try:
            value = self.legacy_language_resolver(rel)
        except Exception:
            return None
        if isinstance(value, bool):
            return "ja" if value else "zh"
        if isinstance(value, (list, tuple, set, frozenset)):
            return "ja" if "ja" in value else "zh"
        value = str(value or "").strip().lower()
        return value if value in ("ja", "zh") else None

    @staticmethod
    def _legacy_source_is_bound(value: dict | None, content_sha256: str) -> bool:
        if not isinstance(value, dict):
            return False
        source_sha = value.get("sourceContentSha256", value.get("contentSha256"))
        return (
            isinstance(source_sha, str)
            and SHA256_RE.fullmatch(source_sha) is not None
            and source_sha == content_sha256
        )

    def _legacy_page_value(
        self,
        resolved: ResolvedOcrBook,
        page_number: int,
        source_bytes: list[int],
    ) -> tuple[str | None, dict | None, int]:
        rel = str(resolved.entry.get("rel") or "")
        rel_key = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
        override = self.project_root / "state" / "pdf-page-ocr" / f"{rel_key}-p{page_number}.json"
        candidates: list[tuple[str, Path]] = [("override", override)]
        try:
            mtime = int(resolved.path.stat().st_mtime)
        except OSError:
            mtime = 0
        language = self._legacy_language(rel)
        if language and self.legacy_char_cache_version is not None:
            candidates.append((
                "char-cache",
                self.project_root / "state" / "pdf-char-cache"
                / f"{rel_key}-p{page_number}-{mtime}-{language}.json",
            ))
        for source, path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            try:
                value = self._read_bounded_legacy_json(path, source_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if not self._legacy_source_is_bound(
                value, resolved.entry["contentSha256"]
            ):
                continue
            if (
                source == "char-cache"
                and (not value or value.get("cver") != self.legacy_char_cache_version)
            ):
                continue
            normalized, size = self._normalize_legacy_page(
                value,
                book_id=resolved.entry["bookId"],
                content_sha256=resolved.entry["contentSha256"],
                page_number=page_number,
                source=source,
            )
            if normalized is not None:
                return source, normalized, size
        if self.legacy_embedded_page_reader is None:
            return None, None, 0
        try:
            value = self.legacy_embedded_page_reader(resolved.path, rel, page_number)
        except Exception:
            value = None
        normalized, size = self._normalize_legacy_page(
            value,
            book_id=resolved.entry["bookId"],
            content_sha256=resolved.entry["contentSha256"],
            page_number=page_number,
            source="embedded",
        )
        return (
            ("embedded", normalized, size)
            if normalized is not None
            else (None, None, 0)
        )

    def _legacy_formula_records(
        self,
        resolved: ResolvedOcrBook,
        total_pages: int,
        source_bytes: list[int],
    ) -> tuple[list[dict], str, str | None]:
        key = hashlib.sha1(str(resolved.path.resolve()).encode("utf-8")).hexdigest()[:16]
        path = self.project_root / "state" / "pdf-figures" / f"{key}.json"
        if not path.is_file() or path.is_symlink():
            return [], "pending", "legacy-formulas-missing"
        try:
            value = self._read_bounded_legacy_json(path, source_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return [], "pending", "legacy-formulas-invalid-json"
        if not isinstance(value, dict) or not isinstance(value.get("formulas"), list):
            return [], "pending", "legacy-formulas-invalid-schema"
        if not self._legacy_source_is_bound(value, resolved.entry["contentSha256"]):
            return [], "pending", "legacy-formulas-unbound"
        if not value.get("pdf") or value.get("book_mtime") is None:
            return [], "pending", "legacy-formulas-unbound"
        try:
            if Path(str(value["pdf"])).resolve() != resolved.path.resolve():
                return [], "pending", "legacy-formulas-identity-mismatch"
        except Exception:
            return [], "pending", "legacy-formulas-identity-mismatch"
        try:
            if int(value["book_mtime"]) != int(resolved.path.stat().st_mtime):
                return [], "pending", "legacy-formulas-version-mismatch"
        except (OSError, TypeError, ValueError):
            return [], "pending", "legacy-formulas-version-mismatch"
        formulas = []
        for item in value["formulas"]:
            if not isinstance(item, dict):
                return [], "pending", "legacy-formulas-invalid-record"
            try:
                page = int(item.get("page"))
                bbox = [float(number) for number in item.get("bbox")]
            except (TypeError, ValueError):
                return [], "pending", "legacy-formulas-invalid-record"
            latex = item.get("latex")
            if (
                page < 1
                or page > total_pages
                or len(bbox) != 4
                or not all(math.isfinite(number) and 0 <= number <= 1 for number in bbox)
                or bbox[0] >= bbox[2]
                or bbox[1] >= bbox[3]
                or (latex is not None and not isinstance(latex, str))
                or (isinstance(latex, str) and len(latex) > 20_000)
            ):
                return [], "pending", "legacy-formulas-invalid-record"
            normalized = {
                "page": page,
                "bbox": bbox,
                "conf": item.get("conf"),
                "latex": latex,
            }
            if item.get("multiline") is not None:
                normalized["multiline"] = bool(item.get("multiline"))
            if item.get("latex_engine"):
                normalized["latex_engine"] = str(item["latex_engine"])
            try:
                json.dumps(normalized, allow_nan=False)
            except (TypeError, ValueError):
                return [], "pending", "legacy-formulas-invalid-record"
            formulas.append(normalized)
        return formulas, "succeeded", None

    def _pointer_engine(self, version_dir: Path, name: str) -> str | None:
        pointer = self._read_optional(version_dir / name)
        engine = pointer.get("engine") if pointer else None
        return engine if engine in RESULT_ENGINES else None

    def _job_for_engine(self, version_dir: Path, engine: str) -> dict | None:
        return self._read_optional(version_dir / engine / "job.json")

    @staticmethod
    def _worker_identity_alive(job: dict) -> bool:
        pid = job.get("workerPid", job.get("pid"))
        if not _pid_alive(pid):
            return False
        expected = job.get("processStartToken")
        current = _process_start_token(pid)
        return not expected or not current or str(expected) == str(current)

    @staticmethod
    def _process_group_alive(pgid: int) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(int(pgid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _terminate_worker_generation(self, job: dict) -> bool:
        """Stop the worker and every inherited child before permitting a retry."""
        if os.name != "posix":
            return not self._worker_identity_alive(job)
        try:
            pid = int(job.get("workerPid", job.get("pid")) or 0)
            pgid = int(job.get("processGroupId") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0 or pgid <= 0 or pgid != pid or pgid == os.getpgrp():
            return not self._worker_identity_alive(job)
        expected = job.get("processStartToken")
        current = _process_start_token(pid)
        if current is not None and expected is not None and str(current) != str(expected):
            return False
        if not self._process_group_alive(pgid):
            return True
        for sig, timeout in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self._process_group_alive(pgid):
                    return True
                time.sleep(0.05)
        return not self._process_group_alive(pgid)

    def _normalize_dead_job(self, job_dir: Path, job: dict) -> dict:
        if job.get("state") not in ACTIVE_STATES:
            return job
        if not job.get("workerPid") and not job.get("pid"):
            # start() holds the global lock through the spawn handshake.  A
            # missing owner cannot be declared dead from elapsed time alone.
            return job
        if self._worker_identity_alive(job):
            return job
        cleanup_complete = self._terminate_worker_generation(job)
        next_job = {
            **job,
            "state": "failed",
            "phase": job.get("phase") or "ocr",
            "errorCode": "worker-stopped",
            "error": "Pi preprocessing worker stopped unexpectedly; retry will resume from saved pages",
            "message": "处理进程已中断；重试会从已保存页面继续",
            "canPause": False,
            "canResume": False,
            "canCancel": False,
            "canRetry": cleanup_complete,
            "workerCleanupComplete": cleanup_complete,
            "updatedAtEpochMs": _now_ms(),
        }
        atomic_write_json(job_dir / "job.json", next_job, indent=2, mode=0o600)
        return next_job

    def _current_job_locked(self, version_dir: Path) -> tuple[str | None, dict | None]:
        pointer = self._read_optional(version_dir / "current.json") or {}
        engine = pointer.get("engine")
        if engine not in RESULT_ENGINES:
            return None, None
        if engine == LEGACY_ENGINE:
            revision = str(pointer.get("revision") or "")
            if not re.fullmatch(r"ocr_[0-9a-f]{20}", revision):
                return engine, None
            job_dir = version_dir / LEGACY_ENGINE / "releases" / revision
            job = self._read_optional(job_dir / "job.json")
            return engine, job
        if not engine:
            return None, None
        job_dir = version_dir / engine
        job = self._job_for_engine(version_dir, engine)
        if job:
            job = self._normalize_dead_job(job_dir, job)
        return engine, job

    def _active_jobs_locked(self) -> list[dict]:
        jobs = []
        if not self.state_root.exists():
            return jobs
        for current in self.state_root.glob("book_*/*/current.json"):
            version_dir = current.parent
            if (
                not BOOK_ID_RE.fullmatch(version_dir.parent.name)
                or not SHA256_RE.fullmatch(version_dir.name)
            ):
                continue
            _engine, job = self._current_job_locked(version_dir)
            if job and job.get("state") in ACTIVE_STATES:
                jobs.append(job)
        return jobs

    def _launch_process(self, job_dir: Path, source_path: Path, job: dict):
        engine = job["engine"]
        # Run the atomically deployed sibling, never a mutable checkout copy.
        worker = Path(__file__).resolve().with_name("reader_book_ocr_worker.py")
        py = os.environ.get("APP_PYTHON") or sys.executable
        if engine == "manga":
            py = os.environ.get(
                "MANGA_OCR_PYTHON", "/home/bwicarus/manga-ocr-venv/bin/python"
            )
        cmd = [
            py,
            str(worker),
            "--job-dir", str(job_dir),
            "--pdf", str(source_path),
            "--project", str(self.project_root),
            "--book-id", job["bookId"],
            "--content-sha256", job["contentSha256"],
            "--engine", engine,
            "--job-id", job["jobId"],
            "--worker-generation", job["workerGeneration"],
            "--max-pages", str(self.max_pages),
            "--max-bytes", str(self.max_pdf_bytes),
        ]
        kwargs = {
            "cwd": str(self.project_root),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00004000 | 0x08000000
        else:
            kwargs["start_new_session"] = True
            cmd = ["nice", "-n", "19", *cmd]
        return subprocess.Popen(cmd, **kwargs)

    def _spawn(self, job_dir: Path, source_path: Path, job: dict) -> None:
        try:
            process = self.launcher(job_dir, source_path, job)
            pid = int(getattr(process, "pid", 0) or 0)
            if pid <= 0:
                raise RuntimeError("worker launcher did not return a process id")
            pgid = None
            start_token = _process_start_token(pid)
            if os.name == "posix" and self._owns_launcher:
                pgid = int(os.getpgid(pid))
                if pgid != pid or not start_token:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                    raise RuntimeError("worker process-group handshake failed")
            current = self._read_optional(job_dir / "job.json") or {}
            if (
                current.get("jobId") != job.get("jobId")
                or current.get("workerGeneration") != job.get("workerGeneration")
            ):
                if pgid and pgid == pid:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                raise RuntimeError("worker generation changed during launch")
            ownership = {
                "workerGeneration": job["workerGeneration"],
                "pid": pid,
                "workerPid": pid,
                "processGroupId": pgid,
                "processStartToken": start_token,
                "workerStartedAtEpochMs": _now_ms(),
            }
            current.update(ownership)
            job.update(ownership)
            atomic_write_json(job_dir / "job.json", current, indent=2, mode=0o600)
        except Exception as exc:
            if os.name == "posix" and "pgid" in locals() and pgid and pgid == locals().get("pid"):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            failed = {
                **job,
                "state": "failed",
                "errorCode": "worker-start-failed",
                "error": f"{type(exc).__name__}: Pi preprocessing worker could not start",
                "message": "Pi 预处理启动失败",
                "canRetry": True,
                "canCancel": False,
                "updatedAtEpochMs": _now_ms(),
            }
            atomic_write_json(job_dir / "job.json", failed, indent=2, mode=0o600)
            raise ReaderBookOcrError(
                "worker-start-failed", "Pi preprocessing failed to start", status=500
            ) from exc

    def _activate_published_locked(self, version_dir: Path, published: dict) -> None:
        """Repair mutable status pointers from one already-validated release."""
        engine = published["engine"]
        revision = published["revision"]
        if engine in ENGINES:
            atomic_write_json(
                version_dir / engine / "job.json",
                published["job"],
                indent=2,
                mode=0o600,
            )
        atomic_write_json(
            version_dir / "result.json", published["result"], indent=2, mode=0o600
        )
        atomic_write_json(
            version_dir / "current.json",
            {"engine": engine, "revision": revision},
            indent=2,
            mode=0o600,
        )

    def start(self, book_id: str, content_sha256: str, engine: str = "vision") -> tuple[dict, bool]:
        resolved = self.resolve(book_id, content_sha256)
        engine = str(engine or "vision").strip().lower()
        if engine not in ENGINES:
            raise ReaderBookOcrError("invalid-engine", "unsupported OCR engine", status=400)
        version_dir = self._version_dir(book_id, content_sha256)
        job_dir = self._job_dir(book_id, content_sha256, engine)
        with exclusive_lock(self.lock_path):
            version_dir.mkdir(parents=True, exist_ok=True)
            current_engine, current = self._current_job_locked(version_dir)
            if current and current.get("state") in ACTIVE_STATES:
                if current_engine == engine:
                    return _safe_public_job(current), True
                raise ReaderBookOcrError(
                    "book-ocr-busy", "another engine is already preprocessing this book", status=409
                )
            active = self._active_jobs_locked()
            if active:
                raise ReaderBookOcrError(
                    "ocr-capacity-busy", "Pi is already preprocessing another book", status=429
                )
            existing = self._job_for_engine(version_dir, engine)
            published = self._published_snapshot(book_id, content_sha256)
            if published is not None and published["engine"] == engine:
                self._activate_published_locked(version_dir, published)
                return _safe_public_job(published["job"]), True
            now = _now_ms()
            job = {
                "contract": CONTRACT,
                "jobId": "ocrjob_" + uuid.uuid4().hex,
                "workerGeneration": "ocrgen_" + uuid.uuid4().hex,
                "bookId": book_id,
                "contentSha256": content_sha256,
                "engine": engine,
                "state": "queued",
                "phase": "preparing",
                "processedPages": int((existing or {}).get("successfulPages") or 0),
                "totalPages": int((existing or {}).get("totalPages") or 0),
                "successfulPages": int((existing or {}).get("successfulPages") or 0),
                "failedPages": 0,
                "recognizedPages": int((existing or {}).get("recognizedPages") or 0),
                "percent": 0,
                "etaSeconds": None,
                "message": "等待 Pi 预处理进程启动",
                "canPause": True,
                "canResume": False,
                "canCancel": True,
                "canRetry": False,
                "createdAtEpochMs": now,
                "updatedAtEpochMs": now,
                "resultAvailable": False,
                "pageCharsRevision": None,
                "pauseMode": "checkpoint-restart",
                "textState": "queued",
                "formulaState": "idle",
                "formulaTotal": int((existing or {}).get("formulaTotal") or 0),
                "formulaRecognized": int((existing or {}).get("formulaRecognized") or 0),
                "formulaPendingRegions": int((existing or {}).get("formulaPendingRegions") or 0),
                "formulaFailedRegions": int((existing or {}).get("formulaFailedRegions") or 0),
                "currentPage": None,
                "textProgress": {
                    "total": int((existing or {}).get("totalPages") or 0),
                    "completed": int((existing or {}).get("successfulPages") or 0),
                    "pending": max(
                        0,
                        int((existing or {}).get("totalPages") or 0)
                        - int((existing or {}).get("successfulPages") or 0),
                    ),
                    "failed": 0,
                    "unavailable": 0,
                },
                "wordProgress": {
                    "total": int((existing or {}).get("totalPages") or 0),
                    "completed": (
                        int((existing or {}).get("successfulPages") or 0)
                        if engine == "vision" else 0
                    ),
                    "pending": max(
                        0,
                        int((existing or {}).get("totalPages") or 0)
                        - (
                            int((existing or {}).get("successfulPages") or 0)
                            if engine == "vision" else 0
                        ),
                    ),
                    "failed": 0,
                    "unavailable": 0,
                },
                "formulaProgress": {
                    "total": int((existing or {}).get("totalPages") or 0),
                    "completed": 0,
                    "pending": int((existing or {}).get("totalPages") or 0),
                    "failed": 0,
                    "unavailable": 0,
                },
            }
            job_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(job_dir / "control.json", {"desiredState": "running"}, indent=2, mode=0o600)
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
            atomic_write_json(version_dir / "current.json", {"engine": engine}, indent=2, mode=0o600)
            self._spawn(job_dir, resolved.path, job)
            return _safe_public_job(job), False

    def status(self, book_id: str, content_sha256: str) -> dict:
        self.resolve(book_id, content_sha256)
        version_dir = self._version_dir(book_id, content_sha256)
        with exclusive_lock(self.lock_path):
            engine, job = self._current_job_locked(version_dir)
            published = self._published_snapshot(book_id, content_sha256)
            if not job:
                if published is not None:
                    self._activate_published_locked(version_dir, published)
                    return _safe_public_job(published["job"])
                return _safe_public_job({
                    "bookId": book_id,
                    "contentSha256": content_sha256,
                    "state": "idle",
                    "phase": "idle",
                })
            publication_matches_job = (
                published is not None
                and published["engine"] == engine
                and (
                    published["job"].get("jobId") == job.get("jobId")
                    or (
                        job.get("pageCharsRevision")
                        and job.get("pageCharsRevision") == published["revision"]
                    )
                )
            )
            if publication_matches_job:
                self._activate_published_locked(version_dir, published)
                return _safe_public_job(published["job"])
            if job.get("state") == "succeeded":
                if engine == LEGACY_ENGINE:
                    raise ReaderBookOcrError(
                        "ocr-publication-incomplete",
                        "OCR finished but its publication did not commit; retry preprocessing",
                        status=503,
                    )
                job = {
                    **job,
                    "state": "failed",
                    "errorCode": "ocr-publication-incomplete",
                    "error": "OCR worker finished but its publication did not commit",
                    "message": "发布未完成；重试会复用已完成页面",
                    "canPause": False,
                    "canResume": False,
                    "canCancel": False,
                    "canRetry": True,
                    "resultAvailable": False,
                    "pageCharsRevision": None,
                    "updatedAtEpochMs": _now_ms(),
                }
                atomic_write_json(
                    version_dir / engine / "job.json", job, indent=2, mode=0o600
                )
            return _safe_public_job(job)

    def _control(self, book_id: str, content_sha256: str, action: str) -> dict:
        resolved = self.resolve(book_id, content_sha256)
        version_dir = self._version_dir(book_id, content_sha256)
        with exclusive_lock(self.lock_path):
            engine, job = self._current_job_locked(version_dir)
            if not engine or not job:
                raise ReaderBookOcrError("ocr-job-not-found", "OCR job not found", status=404)
            job_dir = version_dir / engine
            state = job.get("state")
            if action == "pause":
                if state not in ("queued", "running", "pause-requested"):
                    raise ReaderBookOcrError("ocr-cannot-pause", "OCR job cannot be paused", status=409)
                atomic_write_json(job_dir / "control.json", {"desiredState": "paused"}, indent=2, mode=0o600)
                job = {**job, "state": "pause-requested", "message": "保存已完成页后暂停；当前页可能在继续时重做", "canPause": False, "canCancel": True, "updatedAtEpochMs": _now_ms()}
            elif action == "cancel":
                if state in ("cancelled", "succeeded"):
                    return _safe_public_job(job)
                if state not in ACTIVE_STATES and state != "paused":
                    raise ReaderBookOcrError("ocr-cannot-cancel", "OCR job cannot be cancelled", status=409)
                atomic_write_json(job_dir / "control.json", {"desiredState": "cancelled"}, indent=2, mode=0o600)
                job = {**job, "state": "cancel-requested", "message": "正在停止；已完成页面会保留", "canPause": False, "canResume": False, "canCancel": False, "updatedAtEpochMs": _now_ms()}
            elif action in ("resume", "retry"):
                allowed = ("paused",) if action == "resume" else ("failed", "cancelled")
                if state not in allowed:
                    raise ReaderBookOcrError(f"ocr-cannot-{action}", f"OCR job cannot {action}", status=409)
                if (
                    job.get("processGroupId")
                    and not self._terminate_worker_generation(job)
                ):
                    raise ReaderBookOcrError(
                        "ocr-worker-cleanup-busy",
                        "the previous OCR worker generation is still stopping",
                        status=409,
                    )
                active = [item for item in self._active_jobs_locked() if item.get("jobId") != job.get("jobId")]
                if active:
                    raise ReaderBookOcrError("ocr-capacity-busy", "Pi is already preprocessing another book", status=429)
                now = _now_ms()
                job = {
                    **job,
                    "jobId": "ocrjob_" + uuid.uuid4().hex,
                    "workerGeneration": "ocrgen_" + uuid.uuid4().hex,
                    "pid": None,
                    "workerPid": None,
                    "processGroupId": None,
                    "processStartToken": None,
                    "workerStartedAtEpochMs": None,
                    "state": "queued",
                    "phase": "preparing",
                    "message": "从已保存页面继续",
                    "canPause": True,
                    "canResume": False,
                    "canCancel": True,
                    "canRetry": False,
                    "errorCode": None,
                    "error": None,
                    "updatedAtEpochMs": now,
                }
                atomic_write_json(job_dir / "control.json", {"desiredState": "running"}, indent=2, mode=0o600)
                atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
                self._spawn(job_dir, resolved.path, job)
                return _safe_public_job(job)
            else:
                raise ValueError(action)
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
            return _safe_public_job(job)

    def pause(self, book_id: str, content_sha256: str) -> dict:
        return self._control(book_id, content_sha256, "pause")

    def resume(self, book_id: str, content_sha256: str) -> dict:
        return self._control(book_id, content_sha256, "resume")

    def cancel(self, book_id: str, content_sha256: str) -> dict:
        return self._control(book_id, content_sha256, "cancel")

    def retry(self, book_id: str, content_sha256: str) -> dict:
        return self._control(book_id, content_sha256, "retry")

    def _published_snapshot(
        self,
        book_id: str,
        content_sha256: str,
        *,
        require_legacy: bool = False,
        fence_override: dict | None = None,
    ) -> dict | None:
        """Validate the single publication fence and every referenced artifact."""
        version_dir = self._version_dir(book_id, content_sha256)
        fence_path = version_dir / "publication.json"
        if fence_override is None:
            if not fence_path.exists():
                return None
            try:
                fence = read_json(fence_path)
            except Exception as exc:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR publication fence is invalid", status=500
                ) from exc
        else:
            fence = dict(fence_override)
        engine = fence.get("engine") if isinstance(fence, dict) else None
        revision = str(fence.get("revision") or "") if isinstance(fence, dict) else ""
        if (
            not isinstance(fence, dict)
            or fence.get("contract") != PUBLICATION_CONTRACT
            or fence.get("bookId") != book_id
            or fence.get("contentSha256") != content_sha256
            or engine not in RESULT_ENGINES
            or (require_legacy and engine != LEGACY_ENGINE)
            or not re.fullmatch(r"ocr_[0-9a-f]{20}", revision)
            or not SHA256_RE.fullmatch(str(fence.get("manifestSha256") or ""))
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication identity is inconsistent", status=500
            )
        expected_release = (
            f"legacy/releases/{revision}"
            if engine == LEGACY_ENGINE
            else f"releases/{revision}"
        )
        if fence.get("release") != expected_release:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication release is inconsistent", status=500
            )
        release_dir = (
            version_dir / LEGACY_ENGINE / "releases" / revision
            if engine == LEGACY_ENGINE
            else version_dir / "releases" / revision
        )
        job_path = release_dir / "job.json"
        manifest_path = release_dir / "attachments.json"
        formulas_path = release_dir / "formulas.json"
        pages_dir = release_dir / "pages"
        current = self._read_optional(release_dir / "current.json") or {}
        result = self._read_optional(release_dir / "result.json") or {}
        job = self._read_optional(job_path) or {}
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as exc:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication manifest is unavailable", status=500
            ) from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != fence["manifestSha256"]:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication manifest digest changed", status=500
            )
        source_identity = result.get("sourceIdentity") if isinstance(result, dict) else None
        if (
            not isinstance(source_identity, dict)
            or set(source_identity) != {"device", "inode", "size", "mtimeNs"}
            or fence.get("sourceIdentity") != source_identity
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication source identity is missing", status=500
            )
        self._verify_current_source_content(
            self.resolve(book_id, content_sha256), content_sha256
        )
        identity_values = (current, result, job, manifest)
        if any(
            not isinstance(value, dict)
            or value.get("engine") != engine
            or value.get("revision", value.get("pageCharsRevision")) != revision
            for value in identity_values
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication pointers disagree", status=500
            )
        if job.get("state") != "succeeded" or not job.get("resultAvailable"):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication job is not complete", status=500
            )
        formula_state = str(job.get("formulaState") or "")
        if (
            formula_state not in ("pending", "succeeded", "partial", "failed")
            or manifest.get("formulaState") != formula_state
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR formula publication state disagrees", status=500
            )
        if (
            manifest.get("contract") != "reader-book-attachments/1"
            or manifest.get("schema") != 1
            or manifest.get("bookId") != book_id
            or manifest.get("contentSha256") != content_sha256
            or manifest.get("revision") != revision
            or manifest.get("category") != "derived"
            or manifest.get("mergePolicy") != "immutable"
            or not isinstance(manifest.get("files"), list)
            or job.get("bookId") != book_id
            or job.get("contentSha256") != content_sha256
            or result.get("release") != fence.get("release")
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication manifest identity is invalid", status=500
            )
        try:
            total_pages = int(manifest.get("totalPages"))
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication page count is invalid", status=500
            ) from exc
        if total_pages <= 0 or total_pages > self.max_pages:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication page count is invalid", status=500
            )
        if engine == LEGACY_ENGINE:
            if (
                manifest.get("adoptionContract") != ADOPTION_CONTRACT
                or manifest.get("source") != "legacy-sidecars"
            ):
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "legacy adoption provenance is invalid", status=500
                )
        files_by_id = {}
        page_numbers = []
        attachment_paths = {}
        formula_records = None
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR attachment entry is invalid", status=500
                )
            attachment_id = str(entry.get("attachmentId") or "")
            page_match = re.fullmatch(r"ocr-page-(\d{6})", attachment_id)
            if page_match:
                page_number = int(page_match.group(1))
                path = pages_dir / f"p{page_number:06d}.json"
                expected_name = f"pages/p{page_number:06d}.json"
                page_numbers.append(page_number)
            elif attachment_id == "ocr-formulas":
                path = formulas_path
                expected_name = "formulas.json"
            else:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR attachment id is invalid", status=500
                )
            expected_url = (
                f"/pdf/api/library/attachments/{book_id}/{attachment_id}"
                f"?contentSha256={content_sha256}&revision={revision}"
            )
            try:
                expected_size = int(entry.get("size"))
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR attachment size is invalid", status=500
                ) from exc
            if (
                attachment_id in files_by_id
                or entry.get("name") != expected_name
                or entry.get("downloadUrl") != expected_url
                or entry.get("category") != "derived"
                or entry.get("mergePolicy") != "immutable"
                or entry.get("mediaType") != "application/json"
                or not SHA256_RE.fullmatch(str(entry.get("sha256") or ""))
                or expected_size < 0
                or not path.is_file()
                or path.is_symlink()
            ):
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR attachment metadata is invalid", status=500
                )
            payload = path.read_bytes()
            if (
                len(payload) != expected_size
                or hashlib.sha256(payload).hexdigest() != entry["sha256"]
            ):
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR attachment digest mismatch", status=500
                )
            try:
                derived = json.loads(payload.decode("utf-8"))
            except Exception as exc:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR attachment JSON is invalid", status=500
                ) from exc
            if page_match:
                if (
                    not isinstance(derived, dict)
                    or derived.get("schema") != "reader-page-chars/1"
                    or derived.get("bookId") != book_id
                    or derived.get("contentSha256") != content_sha256
                    or derived.get("engine") != engine
                    or derived.get("pageNumber") != page_number
                    or not isinstance(derived.get("chars"), list)
                    or len(derived.get("chars")) > 2_000_000
                    or not isinstance(derived.get("furigana"), list)
                ):
                    raise ReaderBookOcrError(
                        "ocr-publication-invalid", "OCR page attachment is inconsistent", status=500
                    )
            else:
                formula_records = derived.get("formulas") if isinstance(derived, dict) else None
                if (
                    not isinstance(derived, dict)
                    or derived.get("schema") != "reader-formula-regions/1"
                    or derived.get("bookId") != book_id
                    or derived.get("contentSha256") != content_sha256
                    or not isinstance(formula_records, list)
                ):
                    raise ReaderBookOcrError(
                        "ocr-publication-invalid", "OCR formula attachment is inconsistent", status=500
                    )
            files_by_id[attachment_id] = entry
            attachment_paths[attachment_id] = path
        if (
            page_numbers != list(range(1, total_pages + 1))
            or len(files_by_id) != total_pages + 1
            or "ocr-formulas" not in files_by_id
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication pages are incomplete", status=500
            )
        try:
            expected_formula_count = int(manifest.get("formulaCount"))
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR formula count is invalid", status=500
            ) from exc
        if formula_records is None or len(formula_records) != expected_formula_count:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR formula count is inconsistent", status=500
            )
        for formula in formula_records:
            try:
                formula_page = int(formula.get("page"))
                bbox = [float(number) for number in formula.get("bbox")]
            except (AttributeError, TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR formula record is invalid", status=500
                ) from exc
            if (
                formula_page < 1
                or formula_page > total_pages
                or len(bbox) != 4
                or not all(math.isfinite(number) and 0 <= number <= 1 for number in bbox)
                or bbox[0] >= bbox[2]
                or bbox[1] >= bbox[3]
            ):
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "OCR formula record is invalid", status=500
                )
        from reader_book_ocr_worker import _manifest_revision

        if revision != _manifest_revision(manifest):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR publication revision is not content-addressed", status=500
            )
        return {
            "engine": engine,
            "revision": revision,
            "current": current,
            "result": result,
            "job": job,
            "manifest": manifest,
            "releaseDir": release_dir,
            "attachmentPaths": attachment_paths,
        }

    def _snapshot_for_revision(
        self, book_id: str, content_sha256: str, revision: str
    ) -> dict:
        if not re.fullmatch(r"ocr_[0-9a-f]{20}", str(revision or "")):
            raise ReaderBookOcrError(
                "ocr-attachment-revision-changed", "attachment revision is invalid", status=409
            )
        version_dir = self._version_dir(book_id, content_sha256)
        candidates = [
            (LEGACY_ENGINE, version_dir / LEGACY_ENGINE / "releases" / revision),
            ("normal", version_dir / "releases" / revision),
        ]
        matches = [(kind, path) for kind, path in candidates if path.is_dir()]
        if len(matches) != 1:
            raise ReaderBookOcrError(
                "ocr-attachment-revision-changed", "attachment revision is unavailable", status=409
            )
        kind, release_dir = matches[0]
        manifest_path = release_dir / "attachments.json"
        try:
            manifest = read_json(manifest_path)
            engine = str(manifest.get("engine") or "")
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            source_identity = (read_json(release_dir / "result.json") or {}).get(
                "sourceIdentity"
            )
        except Exception as exc:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR release manifest is unavailable", status=500
            ) from exc
        if (kind == LEGACY_ENGINE and engine != LEGACY_ENGINE) or (
            kind == "normal" and engine not in ENGINES
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR release engine is inconsistent", status=500
            )
        snapshot = self._published_snapshot(
            book_id,
            content_sha256,
            fence_override={
                "contract": PUBLICATION_CONTRACT,
                "bookId": book_id,
                "contentSha256": content_sha256,
                "engine": engine,
                "revision": revision,
                "release": (
                    f"legacy/releases/{revision}"
                    if engine == LEGACY_ENGINE
                    else f"releases/{revision}"
                ),
                "manifestSha256": manifest_sha256,
                "sourceIdentity": source_identity,
            },
        )
        if snapshot is None:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "OCR release is unavailable", status=500
            )
        return snapshot

    def _existing_adoption(self, book_id: str, content_sha256: str) -> dict | None:
        snapshot = self._published_snapshot(book_id, content_sha256)
        if snapshot is None:
            return None
        if snapshot["engine"] != LEGACY_ENGINE:
            return None
        manifest = snapshot["manifest"]
        actual_pages = self._legacy_page_count(self.resolve(book_id, content_sha256).path)
        if int(manifest["totalPages"]) != actual_pages:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "legacy adoption page count changed", status=500
            )
        job = snapshot["job"]
        try:
            formula_payload = read_json(snapshot["attachmentPaths"]["ocr-formulas"])
            formula_count = int(manifest.get("formulaCount"))
        except Exception as exc:
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "legacy formula publication is invalid", status=500
            ) from exc
        if (
            not isinstance(formula_payload, dict)
            or not isinstance(formula_payload.get("formulas"), list)
            or formula_count != len(formula_payload["formulas"])
        ):
            raise ReaderBookOcrError(
                "ocr-publication-invalid", "legacy formula publication is inconsistent", status=500
            )
        page_count = sum(
            1 for item in manifest["files"]
            if str(item.get("attachmentId") or "").startswith("ocr-page-")
        )
        return {
            "contract": ADOPTION_CONTRACT,
            "bookId": book_id,
            "contentSha256": content_sha256,
            "available": True,
            "alreadyAdopted": True,
            "sourceEngine": LEGACY_ENGINE,
            "totalPages": page_count,
            "pageSources": dict(manifest.get("pageSources") or {}),
            "missingPages": [],
            "formula": {
                "state": str(job.get("formulaState") or ""),
                "count": formula_count,
                "reason": manifest.get("formulaReason"),
            },
            "revision": manifest["revision"],
        }

    def _scan_legacy(
        self,
        resolved: ResolvedOcrBook,
        content_sha256: str,
        *,
        page_sink=None,
    ) -> tuple[dict, dict]:
        fingerprint = self._verify_actual_identity(resolved, content_sha256)
        total_pages = self._legacy_page_count(resolved.path)
        counts = {"override": 0, "char-cache": 0, "embedded": 0, "missing": 0}
        missing_pages = []
        total_bytes = 0
        source_bytes = [0]
        recognized_pages = 0
        tokenized_pages = 0
        for page_number in range(1, total_pages + 1):
            source, page, page_bytes = self._legacy_page_value(
                resolved, page_number, source_bytes
            )
            if page is None or source is None:
                counts["missing"] += 1
                missing_pages.append(page_number)
                continue
            counts[source] += 1
            total_bytes += page_bytes
            if total_bytes > self.max_adoption_bytes:
                raise ReaderBookOcrError(
                    "legacy-result-too-large",
                    "old page layers exceed the adoption size limit",
                    status=413,
                )
            if page.get("chars"):
                recognized_pages += 1
            if all(
                item.get("sp")
                or (isinstance(item.get("w"), int) and item.get("w") >= 0)
                for item in page.get("chars") or []
            ):
                tokenized_pages += 1
            if page_sink is not None:
                page_sink(page_number, page)
        formulas, formula_state, formula_reason = self._legacy_formula_records(
            resolved, total_pages, source_bytes
        )
        try:
            formula_bytes = len(json.dumps(
                formulas, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError(
                "legacy-formulas-invalid", "old formula records are invalid", status=500
            ) from exc
        total_bytes += formula_bytes
        if total_bytes > self.max_adoption_bytes:
            raise ReaderBookOcrError(
                "legacy-result-too-large",
                "old derived data exceeds the adoption size limit",
                status=413,
            )
        try:
            current_fingerprint = self._source_fingerprint(resolved.path.stat())
        except OSError as exc:
            raise ReaderBookOcrError(
                "book-unavailable", "book is unavailable", status=503
            ) from exc
        if current_fingerprint != fingerprint:
            raise ReaderBookOcrError(
                "book-version-changed", "book changed while reading old results", status=409
            )
        preview = {
            "contract": ADOPTION_CONTRACT,
            "bookId": resolved.entry["bookId"],
            "contentSha256": content_sha256,
            "available": not missing_pages,
            "alreadyAdopted": False,
            "sourceEngine": LEGACY_ENGINE,
            "totalPages": total_pages,
            "pageSources": counts,
            "missingPages": missing_pages,
            "formula": {
                "state": formula_state,
                "count": len(formulas),
                "reason": formula_reason,
            },
            "totalBytes": total_bytes,
            "sourceBytes": source_bytes[0],
        }
        return preview, {
            "formulas": formulas,
            "sourceFingerprint": fingerprint,
            "recognizedPages": recognized_pages,
            "tokenizedPages": tokenized_pages,
        }

    def preview_adoption(self, book_id: str, content_sha256: str) -> dict:
        if not self._adoption_singleflight.acquire(blocking=False):
            raise ReaderBookOcrError(
                "legacy-adoption-busy", "old-result inspection is already running", status=409
            )
        try:
            resolved = self.resolve(book_id, content_sha256)
            existing = self._existing_adoption(book_id, content_sha256)
            if existing is not None:
                self._verify_actual_identity(resolved, content_sha256)
                return existing
            preview, _details = self._scan_legacy(resolved, content_sha256)
            return preview
        finally:
            self._adoption_singleflight.release()

    def adopt_legacy(self, book_id: str, content_sha256: str) -> tuple[dict, dict, bool]:
        """Copy old path-keyed results into one immutable content-versioned attachment set."""
        if not self._adoption_singleflight.acquire(blocking=False):
            raise ReaderBookOcrError(
                "legacy-adoption-busy", "old-result adoption is already running", status=409
            )
        staging_dir = None
        source_guard = None
        try:
            resolved = self.resolve(book_id, content_sha256)
            existing = self._existing_adoption(book_id, content_sha256)
            if existing is not None:
                self._verify_actual_identity(resolved, content_sha256)
                published = self._published_snapshot(
                    book_id, content_sha256, require_legacy=True
                )
                return _safe_public_job(published["job"]), existing, True
            source_guard = self._open_source_guard(resolved, content_sha256)
            version_dir = self._version_dir(book_id, content_sha256)
            legacy_root = version_dir / LEGACY_ENGINE
            staging_dir = self.state_root / (".adopt-staging-" + uuid.uuid4().hex[:12])
            pages_dir = staging_dir / "pages"
            pages_dir.mkdir(parents=True, exist_ok=False)

            def _write_page(page_number: int, page: dict) -> None:
                atomic_write_json(
                    pages_dir / f"p{page_number:06d}.json",
                    page,
                    indent=None,
                    mode=0o600,
                )

            preview, details = self._scan_legacy(
                resolved, content_sha256, page_sink=_write_page
            )
            if not preview["available"]:
                raise ReaderBookOcrError(
                    "legacy-result-incomplete",
                    "old preprocessing results do not cover every PDF page",
                    status=409,
                )
            formulas = details["formulas"]
            source_fingerprint = details["sourceFingerprint"]
            total_pages = int(preview["totalPages"])
            now = _now_ms()
            formula_state = preview["formula"]["state"]
            formula_recognized = sum(
                1 for item in formulas if str(item.get("latex") or "").strip()
            )
            from reader_book_ocr_worker import _publish_attachments

            revision, manifest = _publish_attachments(
                SimpleNamespace(book_id=book_id, content_sha256=content_sha256),
                staging_dir,
                formula_records=formulas,
                manifest_metadata={
                    "adoptionContract": ADOPTION_CONTRACT,
                    "source": "legacy-sidecars",
                    "pageSources": preview["pageSources"],
                    "formulaState": formula_state,
                    "formulaReason": preview["formula"]["reason"],
                    "formulaCount": len(formulas),
                    "engine": LEGACY_ENGINE,
                    "totalPages": total_pages,
                },
                publish_manifest=False,
                output_dir=staging_dir,
                generated_at_epoch_ms=0,
            )
            job = {
                "contract": CONTRACT,
                "jobId": "ocrjob_" + uuid.uuid4().hex,
                "bookId": book_id,
                "contentSha256": content_sha256,
                "engine": LEGACY_ENGINE,
                "state": "succeeded",
                "phase": "adopted",
                "processedPages": total_pages,
                "totalPages": total_pages,
                "successfulPages": total_pages,
                "failedPages": 0,
                "recognizedPages": details["recognizedPages"],
                "percent": 100,
                "etaSeconds": 0,
                "message": "已采用现有 Pi 预处理结果",
                "canPause": False,
                "canResume": False,
                "canCancel": False,
                "canRetry": False,
                "createdAtEpochMs": now,
                "updatedAtEpochMs": now,
                "resultAvailable": True,
                "pauseMode": "checkpoint-restart",
                "textState": "succeeded",
                "formulaState": formula_state,
                "formulaTotal": len(formulas),
                "formulaRecognized": formula_recognized,
                "formulaPendingRegions": 0,
                "formulaFailedRegions": 0,
                "currentPage": None,
                "textProgress": {
                    "total": total_pages, "completed": total_pages,
                    "pending": 0, "failed": 0, "unavailable": 0,
                },
                "wordProgress": {
                    "total": total_pages, "completed": details["tokenizedPages"],
                    "pending": total_pages - details["tokenizedPages"],
                    "failed": 0, "unavailable": 0,
                },
                "formulaProgress": {
                    "total": total_pages,
                    "completed": total_pages if formula_state == "succeeded" else 0,
                    "pending": 0 if formula_state == "succeeded" else total_pages,
                    "failed": 0, "unavailable": 0,
                },
                "pageCharsRevision": revision,
            }
            release_rel = f"legacy/releases/{revision}"
            release_result = {
                "engine": LEGACY_ENGINE,
                "revision": revision,
                "pageCharsRevision": revision,
                "release": release_rel,
                "adoptedAtEpochMs": now,
                "sourceIdentity": dict(source_guard["identity"]),
            }
            atomic_write_json(staging_dir / "job.json", job, indent=2, mode=0o600)
            atomic_write_json(
                staging_dir / "result.json", release_result,
                indent=2,
                mode=0o600,
            )
            atomic_write_json(
                staging_dir / "current.json",
                {"engine": LEGACY_ENGINE, "revision": revision},
                indent=2,
                mode=0o600,
            )
            atomic_write_json(
                staging_dir / "attachments.json", manifest, indent=2, mode=0o600
            )
            release_dir = legacy_root / "releases" / revision
            releases_dir = release_dir.parent
            releases_dir.mkdir(parents=True, exist_ok=True)
            with exclusive_lock(self.lock_path):
                existing = self._existing_adoption(book_id, content_sha256)
                if existing is not None:
                    published = self._published_snapshot(
                        book_id, content_sha256, require_legacy=True
                    )
                    return _safe_public_job(published["job"]), existing, True
                _engine, current = self._current_job_locked(version_dir)
                if current and current.get("state") in ACTIVE_STATES:
                    raise ReaderBookOcrError(
                        "book-ocr-busy", "this book is currently being preprocessed", status=409
                    )
                if self._verify_actual_identity(resolved, content_sha256) != source_fingerprint:
                    raise ReaderBookOcrError(
                        "book-version-changed",
                        "book changed before old results were adopted",
                        status=409,
                    )
                if release_dir.exists():
                    # A prior crash may have promoted the complete immutable release.
                    # Never merge or read a partial directory into the new attempt.
                    existing_manifest = self._read_optional(release_dir / "attachments.json")
                    if (
                        not existing_manifest
                        or existing_manifest.get("revision") != revision
                        or hashlib.sha256((release_dir / "attachments.json").read_bytes()).hexdigest()
                        != hashlib.sha256((staging_dir / "attachments.json").read_bytes()).hexdigest()
                    ):
                        raise ReaderBookOcrError(
                            "legacy-release-conflict",
                            "an incomplete legacy release blocks safe adoption",
                            status=500,
                        )
                else:
                    os.replace(staging_dir, release_dir)
                    staging_dir = None
                committed_manifest_path = release_dir / "attachments.json"
                committed_manifest = read_json(committed_manifest_path)
                committed_job = read_json(release_dir / "job.json")
                committed_result = read_json(release_dir / "result.json")
                committed_current = read_json(release_dir / "current.json")
                if (
                    committed_manifest.get("revision") != revision
                    or committed_manifest.get("engine") != LEGACY_ENGINE
                    or int(committed_manifest.get("totalPages") or 0) != total_pages
                    or committed_job.get("engine") != LEGACY_ENGINE
                    or committed_job.get("state") != "succeeded"
                    or committed_job.get("pageCharsRevision") != revision
                    or committed_job.get("formulaState") != committed_manifest.get("formulaState")
                    or committed_result.get("engine") != LEGACY_ENGINE
                    or committed_result.get("pageCharsRevision") != revision
                    or committed_result.get("sourceIdentity") != source_guard["identity"]
                    or committed_current != {"engine": LEGACY_ENGINE, "revision": revision}
                ):
                    raise ReaderBookOcrError(
                        "legacy-release-conflict",
                        "legacy release metadata is incomplete",
                        status=500,
                    )
                expected_pages = []
                for entry in committed_manifest.get("files") or []:
                    attachment_id = str(entry.get("attachmentId") or "")
                    page_match = re.fullmatch(r"ocr-page-(\d{6})", attachment_id)
                    if page_match:
                        page_number = int(page_match.group(1))
                        artifact = release_dir / "pages" / f"p{page_number:06d}.json"
                        expected_pages.append(page_number)
                    elif attachment_id == "ocr-formulas":
                        artifact = release_dir / "formulas.json"
                    else:
                        raise ReaderBookOcrError(
                            "legacy-release-conflict", "legacy release has an unknown file", status=500
                        )
                    if not artifact.is_file() or artifact.is_symlink():
                        raise ReaderBookOcrError(
                            "legacy-release-conflict", "legacy release file is missing", status=500
                        )
                    payload = artifact.read_bytes()
                    if (
                        len(payload) != int(entry.get("size") or -1)
                        or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
                    ):
                        raise ReaderBookOcrError(
                            "legacy-release-conflict", "legacy release digest mismatch", status=500
                        )
                if expected_pages != list(range(1, total_pages + 1)):
                    raise ReaderBookOcrError(
                        "legacy-release-conflict", "legacy release pages are incomplete", status=500
                    )
                atomic_write_json(
                    version_dir / "result.json", committed_result, indent=2, mode=0o600
                )
                atomic_write_json(
                    version_dir / "current.json",
                    {"engine": LEGACY_ENGINE, "revision": revision},
                    indent=2,
                    mode=0o600,
                )
                fence_path = version_dir / "publication.json"
                previous_fence = self._read_optional(fence_path)
                fence = {
                    "contract": PUBLICATION_CONTRACT,
                    "bookId": book_id,
                    "contentSha256": content_sha256,
                    "engine": LEGACY_ENGINE,
                    "revision": revision,
                    "release": release_rel,
                    "manifestSha256": hashlib.sha256(
                        committed_manifest_path.read_bytes()
                    ).hexdigest(),
                    "sourceIdentity": dict(source_guard["identity"]),
                }
                self._assert_source_guard(source_guard, rehash=True)
                atomic_write_json(fence_path, fence, indent=2, mode=0o600)
                try:
                    # The path identity check catches ordinary atomic replacement,
                    # but an in-place writer can preserve size and mtime.  Rehash
                    # after the fence as well so no content change can become
                    # visible in the verify-to-publication window.
                    self._assert_source_guard(source_guard, rehash=True)
                except Exception:
                    if previous_fence is not None:
                        atomic_write_json(
                            fence_path, previous_fence, indent=2, mode=0o600
                        )
                    else:
                        fence_path.unlink(missing_ok=True)
                    raise
            snapshot = self._published_snapshot(
                book_id, content_sha256, require_legacy=True
            )
            if snapshot is None:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid", "legacy adoption was not published", status=500
                )
            preview["revision"] = revision
            preview["alreadyAdopted"] = True
            return _safe_public_job(committed_job), preview, False
        finally:
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            self._close_source_guard(source_guard)
            self._adoption_singleflight.release()

    def read_page(
        self,
        book_id: str,
        content_sha256: str,
        page: int,
        *,
        _snapshot: dict | None = None,
    ) -> tuple[dict, Path]:
        resolved = self.resolve(book_id, content_sha256)
        try:
            page_number = int(page)
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError("invalid-page", "invalid page", status=400) from exc
        if page_number < 1 or page_number > self.max_pages:
            raise ReaderBookOcrError("invalid-page", "invalid page", status=400)
        snapshot = _snapshot or self._published_snapshot(book_id, content_sha256)
        if snapshot is None:
            raise ReaderBookOcrError("ocr-result-not-found", "OCR result not found", status=404)
        engine = snapshot["engine"]
        path = snapshot["attachmentPaths"].get(f"ocr-page-{page_number:06d}")
        if path is None:
            raise ReaderBookOcrError("ocr-page-not-found", "OCR page not found", status=404)
        sidecar = self._read_optional(path)
        chars = sidecar.get("chars") if isinstance(sidecar, dict) else None
        furigana = sidecar.get("furigana") if isinstance(sidecar, dict) else None
        if (
            not sidecar
            or sidecar.get("schema") != "reader-page-chars/1"
            or sidecar.get("bookId") != book_id
            or sidecar.get("contentSha256") != content_sha256
            or sidecar.get("engine") != engine
            or sidecar.get("pageNumber") != page_number
            or not isinstance(chars, list)
            or len(chars) > 2_000_000
            or not isinstance(furigana, list)
        ):
            raise ReaderBookOcrError("ocr-sidecar-invalid", "OCR sidecar identity mismatch", status=500)
        return sidecar, resolved.path

    def read_formulas(
        self,
        book_id: str,
        content_sha256: str,
        *,
        _snapshot: dict | None = None,
    ) -> list[dict]:
        """Read the content-versioned formula attachment without using a vault path.

        Text OCR remains usable while formula detection is pending or when an
        older result predates formula export, so a missing attachment is an
        empty formula layer.  A present but malformed or identity-mismatched
        attachment is rejected instead of being silently trusted.
        """
        self.resolve(book_id, content_sha256)
        snapshot = _snapshot or self._published_snapshot(book_id, content_sha256)
        if snapshot is None:
            return []
        path = snapshot["attachmentPaths"]["ocr-formulas"]
        try:
            value = read_json(path)
        except Exception as exc:
            raise ReaderBookOcrError(
                "ocr-formula-sidecar-invalid", "formula sidecar cannot be read", status=500
            ) from exc
        formulas = value.get("formulas") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("schema") != "reader-formula-regions/1"
            or value.get("bookId") != book_id
            or value.get("contentSha256") != content_sha256
            or not isinstance(formulas, list)
            or len(formulas) > self.max_pages * 1000
        ):
            raise ReaderBookOcrError(
                "ocr-formula-sidecar-invalid", "formula sidecar identity mismatch", status=500
            )
        normalized = []
        for item in formulas:
            if not isinstance(item, dict):
                raise ReaderBookOcrError(
                    "ocr-formula-sidecar-invalid", "formula record is invalid", status=500
                )
            try:
                page = int(item.get("page"))
                bbox = [float(number) for number in item.get("bbox")]
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "ocr-formula-sidecar-invalid", "formula geometry is invalid", status=500
                ) from exc
            latex = item.get("latex")
            if (
                page < 1
                or page > self.max_pages
                or len(bbox) != 4
                or not all(math.isfinite(number) and 0 <= number <= 1 for number in bbox)
                or bbox[0] >= bbox[2]
                or bbox[1] >= bbox[3]
                or (latex is not None and not isinstance(latex, str))
                or (isinstance(latex, str) and len(latex) > 20_000)
            ):
                raise ReaderBookOcrError(
                    "ocr-formula-sidecar-invalid", "formula record is invalid", status=500
                )
            normalized.append({**item, "page": page, "bbox": bbox})
        return normalized

    def read_page_bundle(
        self, book_id: str, content_sha256: str, page: int
    ) -> tuple[dict, Path, list[dict]]:
        """Pin one immutable publication for both page chars and formulas."""
        self.resolve(book_id, content_sha256)
        snapshot = self._published_snapshot(book_id, content_sha256)
        if snapshot is None:
            raise ReaderBookOcrError("ocr-result-not-found", "OCR result not found", status=404)
        sidecar, source_path = self.read_page(
            book_id, content_sha256, page, _snapshot=snapshot
        )
        formulas = self.read_formulas(
            book_id, content_sha256, _snapshot=snapshot
        )
        return sidecar, source_path, formulas

    def attachment_manifest(self, book_id: str, content_sha256: str) -> dict:
        self.resolve(book_id, content_sha256)
        snapshot = self._published_snapshot(book_id, content_sha256)
        if snapshot is None:
            raise ReaderBookOcrError(
                "ocr-attachments-not-found", "OCR attachments are not available", status=404
            )
        return snapshot["manifest"]

    def read_attachment(
        self,
        book_id: str,
        content_sha256: str,
        attachment_id: str,
        *,
        expected_revision: str | None = None,
    ) -> tuple[dict, Path, dict]:
        self.resolve(book_id, content_sha256)
        snapshot = (
            self._snapshot_for_revision(book_id, content_sha256, expected_revision)
            if expected_revision
            else self._published_snapshot(book_id, content_sha256)
        )
        if snapshot is None:
            raise ReaderBookOcrError(
                "ocr-attachments-not-found", "OCR attachments are not available", status=404
            )
        manifest = snapshot["manifest"]
        entry = next(
            (
                item for item in manifest["files"]
                if isinstance(item, dict) and item.get("attachmentId") == attachment_id
            ),
            None,
        )
        if entry is None:
            raise ReaderBookOcrError(
                "ocr-attachment-not-found", "OCR attachment not found", status=404
            )
        path = snapshot["attachmentPaths"].get(str(attachment_id or ""))
        if path is None:
            raise ReaderBookOcrError(
                "ocr-attachment-not-found", "OCR attachment not found", status=404
            )
        return entry, path, manifest


def wire_payload(job: dict, *, already: bool | None = None) -> dict:
    payload = {"ok": True, "contract": CONTRACT, "job": _safe_public_job(job)}
    if already is not None:
        payload["already"] = bool(already)
    return payload
