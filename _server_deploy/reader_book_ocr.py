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
WORKER_CONTRACT = "reader-library-ocr-worker/1"
ADOPTION_CONTRACT = "reader-library-ocr-adoption/1"
ENGINES = frozenset(("vision", "manga"))
EXECUTORS = frozenset(("pi", "pc"))
PROCESSING_PROFILES = {
    "pi": "pi-default-v2",
    "pc": "quality-first-v3",
}
# Results created before processingProfile became mandatory were produced by
# the first-generation pipelines.  This fallback must never follow the current
# profile or an unversioned historical release could masquerade as new output.
LEGACY_PROCESSING_PROFILES = {
    "pi": "pi-default-v1",
    "pc": "quality-first-v1",
}
# Immutable releases outlive the worker profile that produced them.  Keep the
# current profile above strict for newly requested/claimed work, while allowing
# already-published profiles that the App still knows how to import.
READABLE_PROCESSING_PROFILES = {
    "pi": frozenset(("pi-default-v1", "pi-default-v2")),
    "pc": frozenset(("quality-first-v1", "quality-first-v2", "quality-first-v3")),
}
LEGACY_ENGINE = "legacy"
RESULT_ENGINES = ENGINES | frozenset((LEGACY_ENGINE,))
ACTIVE_STATES = frozenset(("queued", "running", "pause-requested", "cancel-requested"))
TERMINAL_STATES = frozenset(("paused", "cancelled", "succeeded", "failed"))
CONTROL_STATES = frozenset(("running", "paused", "cancelled"))
FORMULA_STATES = frozenset(("pending", "succeeded", "partial", "failed", "unavailable"))
PC_PUBLISHABLE_FORMULA_STATES = frozenset(("succeeded", "partial", "unavailable"))
MAX_PDF_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024
MAX_PAGES_DEFAULT = 5000
MAX_ADOPTION_BYTES_DEFAULT = 512 * 1024 * 1024
MAX_ADOPTION_PAGE_BYTES = 64 * 1024 * 1024
PUBLICATION_CONTRACT = "reader-book-ocr-publication/1"
# 运行台账：一次预处理 = 一条 run。
#
# 为什么不能直接给 revision 加日期：revision 是**内容寻址**的
# （_manifest_revision 明确把 generatedAtEpochMs 剔除，发布时还硬传 0），
# 同参数重跑出相同字节会塌成同一个目录 —— 第二次运行连一条记录都不会留下。
# 所以另立 run 这一层：一个 revision 可被多条 run 引用，删除按 run 删，
# 引用归零才删物理目录。目录布局一个字节不改（日期进目录名会破坏内容寻址与去重）。
#
# 台账是**纯派生物**：删掉它，旧代码按 publication.json 照常工作，一个字节不丢。
RELEASE_INDEX_CONTRACT = "reader-book-ocr-release-index/1"
RUN_ID_RE = re.compile(r"ocrrun_[0-9a-f]{16}")
MAX_PC_PAGE_BYTES_DEFAULT = 16 * 1024 * 1024
MAX_PC_FORMULA_BYTES_DEFAULT = 32 * 1024 * 1024
MAX_PC_PROGRESS_BYTES_DEFAULT = 64 * 1024
PC_LEASE_SECONDS_DEFAULT = 45
PC_ONLINE_SECONDS_DEFAULT = 30
PC_WORKER_ID_RE = re.compile(r"pc_[A-Za-z0-9_-]{1,64}")
PC_LEASE_ID_RE = re.compile(r"ocrlease_[0-9a-f]{32}")
PC_PHASES = frozenset((
    "preparing", "downloading", "text-ocr", "tokenizing",
    "formula-detect", "formula-latex", "uploading", "finalizing",
))
PC_IDENTITY_FIELDS = frozenset((
    "contract", "workerId", "bookId", "contentSha256",
    "jobId", "generation", "leaseId",
))


def _redact_sensitive_text(value) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(value))[:300]
    message = re.sub(
        r"(?i)(\b(?:authorization|bearer|api[-_ ]?key|access[-_ ]?token|"
        r"refresh[-_ ]?token|token|key|secret|password)\b\s*[:=]\s*)"
        r"([^&#;\s]*)",
        r"\1<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:authorization|bearer)\b\s+)([^&#;\s]+)",
        r"\1<redacted>",
        message,
    )
    return re.sub(
        r"(?:[A-Za-z]:\\|/(?:home|tmp|var|opt|srv)/)[^\r\n]*",
        "<path>",
        message,
    )


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
        "jobId", "bookId", "contentSha256", "engine", "executor",
        "processingProfile", "state", "phase",
        "processedPages", "totalPages", "successfulPages", "failedPages",
        "recognizedPages", "percent", "etaSeconds", "message", "canPause",
        "canResume", "canCancel", "canRetry", "createdAtEpochMs",
        "updatedAtEpochMs", "resultAvailable", "pageCharsRevision",
        "pauseMode", "textState", "formulaState", "formulaTotal",
        "formulaRecognized", "formulaPendingRegions", "formulaFailedRegions",
        "formulaReason", "currentPage", "errorCode", "error", "executorOnline",
        "executorLastSeenAtEpochMs",
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
        "executor": "pi", "state": "idle", "phase": "idle", "processedPages": 0,
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
    if not out.get("processingProfile"):
        profiles = LEGACY_PROCESSING_PROFILES if job else PROCESSING_PROFILES
        out["processingProfile"] = profiles.get(
            out.get("executor"), profiles["pi"]
        )
    if out.get("error"):
        out["error"] = _redact_sensitive_text(out["error"])
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
        max_pc_page_bytes: int | None = None,
        max_pc_formula_bytes: int | None = None,
        pc_lease_seconds: int | None = None,
        pc_online_seconds: int | None = None,
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
        self.max_pc_page_bytes = int(max_pc_page_bytes or _env_positive_int(
            "READER_BOOK_OCR_MAX_PC_PAGE_BYTES", MAX_PC_PAGE_BYTES_DEFAULT
        ))
        self.max_pc_formula_bytes = int(max_pc_formula_bytes or _env_positive_int(
            "READER_BOOK_OCR_MAX_PC_FORMULA_BYTES", MAX_PC_FORMULA_BYTES_DEFAULT
        ))
        self.pc_lease_ms = int(pc_lease_seconds or _env_positive_int(
            "READER_BOOK_OCR_PC_LEASE_SECONDS", PC_LEASE_SECONDS_DEFAULT
        )) * 1000
        self.pc_online_ms = int(pc_online_seconds or _env_positive_int(
            "READER_BOOK_OCR_PC_ONLINE_SECONDS", PC_ONLINE_SECONDS_DEFAULT
        )) * 1000
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
        direct = self.state_root / book_id / content_sha256
        if direct.exists():
            return direct
        # bookId 会漂移，内容哈希不会。
        #
        # 2026-08-19 实测：Pi 上三本书的预处理结果全部挂在**已经不在 catalog 里**
        # 的 bookId 下，而 catalog 里都有同 contentSha256、不同 bookId 的条目 ——
        # 书被重新登记过（重传/目录重建），旧结果就此够不着：既列不出来、
        # 也用不上，看起来就像"从来没跑过"。
        #
        # 结果本来就是内容寻址的（同样的字节 → 同样的 OCR），所以按内容找回来是
        # 安全的：两本书真共享同一份字节时共用结果，也正确。
        alias = self._version_dir_by_content(content_sha256)
        if alias is not None:
            return alias
        return direct

    def _version_dir_by_content(self, content_sha256: str) -> Path | None:
        """在别的 bookId 下找同一份内容的结果目录。"""

        try:
            entries = sorted(self.state_root.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.is_dir() or not BOOK_ID_RE.fullmatch(entry.name):
                continue
            candidate = entry / content_sha256
            if candidate.is_dir():
                return candidate
        return None

    def _job_dir(self, book_id: str, content_sha256: str, engine: str) -> Path:
        if engine not in ENGINES:
            raise ReaderBookOcrError("invalid-engine", "unsupported OCR engine", status=400)
        return self._version_dir(book_id, content_sha256) / engine

    def _archive_mutable_staging_locked(
        self,
        version_dir: Path,
        job_dir: Path,
        existing: dict | None,
    ) -> Path | None:
        if not job_dir.exists():
            return None
        archive_root = version_dir / "staging-archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        executor, _profile = self._processing_identity(existing)
        job_id = str((existing or {}).get("jobId") or "nojob")
        # The directory name is only a short locator (and must stay below
        # Windows path limits); job.json remains the complete identity record.
        job_tag = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
        name = f"{_now_ms()}-{executor}-{job_tag}"
        destination = archive_root / name
        suffix = 0
        while destination.exists():
            suffix += 1
            destination = archive_root / f"{name}-{suffix}"
        os.replace(job_dir, destination)
        return destination

    @staticmethod
    def _read_optional(path: Path) -> dict | None:
        try:
            value = read_json(path)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    @staticmethod
    def _validate_executor(executor: str) -> str:
        value = str(executor or "pi").strip().lower()
        if value not in EXECUTORS:
            raise ReaderBookOcrError(
                "invalid-executor", "unsupported OCR executor", status=400
            )
        return value

    @classmethod
    def _validate_processing_profile(cls, executor: str, profile: str | None) -> str:
        executor = cls._validate_executor(executor)
        expected = PROCESSING_PROFILES[executor]
        value = str(profile or expected).strip().lower()
        if value != expected:
            raise ReaderBookOcrError(
                "invalid-processing-profile",
                f"{executor} OCR requires processingProfile={expected}",
                status=400,
            )
        return value

    @classmethod
    def _processing_identity(cls, value: dict | None) -> tuple[str, str]:
        item = value if isinstance(value, dict) else {}
        executor = str(item.get("executor") or "pi").strip().lower()
        if executor not in EXECUTORS:
            executor = "pi"
        profile = str(
            item.get("processingProfile") or LEGACY_PROCESSING_PROFILES[executor]
        ).strip().lower()
        return executor, profile

    @classmethod
    def _processing_identity_valid(cls, value: dict | None) -> bool:
        item = value if isinstance(value, dict) else {}
        executor = str(item.get("executor") or "pi").strip().lower()
        if executor not in EXECUTORS:
            return False
        profile = str(
            item.get("processingProfile") or LEGACY_PROCESSING_PROFILES[executor]
        ).strip().lower()
        return profile in READABLE_PROCESSING_PROFILES[executor]

    @staticmethod
    def _validate_worker_id(worker_id: str) -> str:
        value = str(worker_id or "")
        if not PC_WORKER_ID_RE.fullmatch(value):
            raise ReaderBookOcrError(
                "invalid-worker-id", "invalid PC OCR workerId", status=400
            )
        return value

    @staticmethod
    def _bounded_json_bytes(value, limit: int, code: str) -> bytes:
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError(code, "OCR worker JSON is invalid", status=400) from exc
        if len(payload) > int(limit):
            raise ReaderBookOcrError(code, "OCR worker payload exceeds its limit", status=413)
        return payload

    @staticmethod
    def _sanitize_worker_error(value) -> str | None:
        if value is None:
            return None
        return _redact_sensitive_text(value)

    def _worker_record_path(self, worker_id: str) -> Path:
        return self.state_root / "workers" / (self._validate_worker_id(worker_id) + ".json")

    def _touch_pc_worker_locked(self, worker_id: str, capabilities: dict | None = None) -> dict:
        worker_id = self._validate_worker_id(worker_id)
        existing = self._read_optional(self._worker_record_path(worker_id)) or {}
        engines = existing.get("engines") if isinstance(existing.get("engines"), list) else []
        max_pdf_bytes = int(existing.get("maxPdfBytes") or 0)
        max_page_bytes = int(existing.get("maxPageBytes") or 0)
        if capabilities is not None:
            if not isinstance(capabilities, dict) or set(capabilities) - {
                "engines", "maxPdfBytes", "maxPageBytes", "processingProfile"
            }:
                raise ReaderBookOcrError(
                    "invalid-worker-capabilities", "invalid PC OCR capabilities", status=400
                )
            raw_engines = capabilities.get("engines")
            if (
                not isinstance(raw_engines, list)
                or not raw_engines
                or len(raw_engines) > len(ENGINES)
            ):
                raise ReaderBookOcrError(
                    "invalid-worker-capabilities", "PC OCR engines are required", status=400
                )
            engines = sorted(set(str(item or "").strip().lower() for item in raw_engines))
            if not engines or any(item not in ENGINES for item in engines):
                raise ReaderBookOcrError(
                    "invalid-worker-capabilities", "unsupported PC OCR engine", status=400
                )
            if capabilities.get("processingProfile") != PROCESSING_PROFILES["pc"]:
                raise ReaderBookOcrError(
                    "invalid-processing-profile",
                    "PC OCR worker requires processingProfile=quality-first-v3",
                    status=400,
                )
            try:
                max_pdf_bytes = int(capabilities.get("maxPdfBytes") or self.max_pdf_bytes)
                max_page_bytes = int(capabilities.get("maxPageBytes") or self.max_pc_page_bytes)
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "invalid-worker-capabilities", "invalid PC OCR limits", status=400
                ) from exc
            if max_pdf_bytes <= 0 or max_page_bytes <= 0:
                raise ReaderBookOcrError(
                    "invalid-worker-capabilities", "invalid PC OCR limits", status=400
                )
        now = _now_ms()
        record = {
            "contract": WORKER_CONTRACT,
            "workerId": worker_id,
            "executor": "pc",
            "engines": engines,
            "processingProfile": PROCESSING_PROFILES["pc"],
            "maxPdfBytes": max_pdf_bytes,
            "maxPageBytes": max_page_bytes,
            "lastSeenAtEpochMs": now,
        }
        atomic_write_json(self._worker_record_path(worker_id), record, indent=2, mode=0o600)
        return record

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

    @staticmethod
    def _pc_desired_state(job_dir: Path) -> str:
        value = ReaderBookOcrService._read_optional(job_dir / "control.json") or {}
        desired = value.get("desiredState")
        return desired if desired in CONTROL_STATES else "cancelled"

    def _pc_worker_records_locked(self) -> list[dict]:
        records = []
        root = self.state_root / "workers"
        if not root.is_dir():
            return records
        for path in root.glob("pc_*.json"):
            if not PC_WORKER_ID_RE.fullmatch(path.stem):
                continue
            record = self._read_optional(path)
            if (
                isinstance(record, dict)
                and record.get("contract") == WORKER_CONTRACT
                and record.get("workerId") == path.stem
            ):
                records.append(record)
        return records

    def _pc_online_summary_locked(self, engine: str | None = None) -> dict:
        now = _now_ms()
        online = []
        for record in self._pc_worker_records_locked():
            try:
                last_seen = int(record.get("lastSeenAtEpochMs") or 0)
            except (TypeError, ValueError):
                continue
            engines = record.get("engines") if isinstance(record.get("engines"), list) else []
            if now - last_seen <= self.pc_online_ms and (
                engine is None or engine in engines
            ):
                online.append(record)
        last_seen = max(
            (int(item.get("lastSeenAtEpochMs") or 0) for item in online),
            default=None,
        )
        return {
            "online": bool(online),
            "lastSeenAtEpochMs": last_seen,
            "workerCount": len(online),
            "engines": sorted({
                value
                for item in online
                for value in (item.get("engines") or [])
                if value in ENGINES
            }),
        }

    def executor_status(self) -> list[dict]:
        with exclusive_lock(self.lock_path):
            active = self._active_jobs_locked()
            pc = self._pc_online_summary_locked()
            capacity_free = not active
            return [
                {
                    "executor": "pi",
                    "online": True,
                    "acceptingJobs": capacity_free,
                    "engines": sorted(ENGINES),
                },
                {
                    "executor": "pc",
                    "online": pc["online"],
                    "acceptingJobs": bool(pc["online"] and capacity_free),
                    "engines": pc["engines"],
                    "lastSeenAtEpochMs": pc["lastSeenAtEpochMs"],
                    "workerCount": pc["workerCount"],
                },
            ]

    def _pc_identity_job_locked(
        self,
        payload: dict,
        *,
        renew: bool = True,
    ) -> tuple[Path, Path, dict, str]:
        if not isinstance(payload, dict) or payload.get("contract") != WORKER_CONTRACT:
            raise ReaderBookOcrError(
                "invalid-worker-contract", "invalid PC OCR worker contract", status=400
            )
        worker_id = self._validate_worker_id(payload.get("workerId"))
        book_id = str(payload.get("bookId") or "")
        content_sha256 = str(payload.get("contentSha256") or "")
        self._validate_identity(book_id, content_sha256)
        job_id = str(payload.get("jobId") or "")
        generation = str(payload.get("generation") or "")
        lease_id = str(payload.get("leaseId") or "")
        if (
            not re.fullmatch(r"ocrjob_[0-9a-f]{32}", job_id)
            or not re.fullmatch(r"ocrgen_[0-9a-f]{32}", generation)
            or not PC_LEASE_ID_RE.fullmatch(lease_id)
        ):
            raise ReaderBookOcrError(
                "invalid-worker-identity", "invalid PC OCR job identity", status=400
            )
        version_dir = self._version_dir(book_id, content_sha256)
        engine, job = self._current_job_locked(version_dir)
        if (
            engine not in ENGINES
            or not isinstance(job, dict)
            or job.get("executor") != "pc"
            or job.get("jobId") != job_id
            or job.get("workerGeneration") != generation
            or job.get("leaseId") != lease_id
            or job.get("leaseWorkerId") != worker_id
        ):
            raise ReaderBookOcrError(
                "ocr-worker-lease-stale", "PC OCR lease is no longer current", status=409
            )
        try:
            expires_at = int(job.get("leaseExpiresAtEpochMs") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        now = _now_ms()
        if expires_at <= now:
            raise ReaderBookOcrError(
                "ocr-worker-lease-expired", "PC OCR lease expired; claim the job again", status=409
            )
        job_dir = version_dir / engine
        desired = self._pc_desired_state(job_dir)
        worker = self._touch_pc_worker_locked(worker_id)
        if renew:
            job = {
                **job,
                "leaseExpiresAtEpochMs": now + self.pc_lease_ms,
                "executorOnline": True,
                "executorLastSeenAtEpochMs": worker["lastSeenAtEpochMs"],
                "updatedAtEpochMs": now,
            }
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
        return version_dir, job_dir, job, desired

    @staticmethod
    def _pc_lease_wire(job: dict, desired: str) -> dict:
        return {
            "desiredState": desired,
            "lease": {
                "leaseId": job.get("leaseId"),
                "expiresAtEpochMs": job.get("leaseExpiresAtEpochMs"),
                "renewAfterMs": 15_000,
            },
        }

    def claim_pc_worker(self, worker_id: str, capabilities: dict) -> dict | None:
        with exclusive_lock(self.lock_path):
            worker = self._touch_pc_worker_locked(worker_id, capabilities)
            now = _now_ms()
            for pointer in sorted(self.state_root.glob("book_*/*/current.json")):
                version_dir = pointer.parent
                if (
                    not BOOK_ID_RE.fullmatch(version_dir.parent.name)
                    or not SHA256_RE.fullmatch(version_dir.name)
                ):
                    continue
                engine, job = self._current_job_locked(version_dir)
                if (
                    engine not in worker["engines"]
                    or not isinstance(job, dict)
                    or job.get("executor") != "pc"
                    or self._processing_identity(job)
                        != ("pc", PROCESSING_PROFILES["pc"])
                    or job.get("state") not in ACTIVE_STATES
                ):
                    continue
                job_dir = version_dir / engine
                if self._pc_desired_state(job_dir) != "running":
                    continue
                resolved = self.resolve(job["bookId"], job["contentSha256"])
                try:
                    source_size = int(resolved.path.stat().st_size)
                except OSError:
                    continue
                if source_size > int(worker.get("maxPdfBytes") or 0):
                    continue
                try:
                    lease_expires = int(job.get("leaseExpiresAtEpochMs") or 0)
                except (TypeError, ValueError):
                    lease_expires = 0
                lease_worker = str(job.get("leaseWorkerId") or "")
                if lease_expires > now and lease_worker and lease_worker != worker["workerId"]:
                    continue
                lease_id = (
                    str(job.get("leaseId"))
                    if lease_expires > now and lease_worker == worker["workerId"]
                    else "ocrlease_" + uuid.uuid4().hex
                )
                total_pages = int(job.get("totalPages") or 0)
                if total_pages <= 0 or total_pages > self.max_pages:
                    continue
                completed_pages = [
                    page_number
                    for page_number in range(1, total_pages + 1)
                    if self._page_for_pc_done(job_dir, page_number, job)
                ]
                recognized_pages = sum(
                    1
                    for page_number in completed_pages
                    if (self._read_optional(
                        job_dir / "pages" / f"p{page_number:06d}.json"
                    ) or {}).get("chars")
                )
                job = {
                    **job,
                    "state": "running",
                    "phase": job.get("phase") if job.get("phase") in PC_PHASES else "preparing",
                    "leaseId": lease_id,
                    "leaseWorkerId": worker["workerId"],
                    "leaseExpiresAtEpochMs": now + self.pc_lease_ms,
                    "executorOnline": True,
                    "executorLastSeenAtEpochMs": worker["lastSeenAtEpochMs"],
                    "processedPages": len(completed_pages),
                    "successfulPages": len(completed_pages),
                    "recognizedPages": recognized_pages,
                    "textProgress": {
                        "total": total_pages,
                        "completed": len(completed_pages),
                        "pending": total_pages - len(completed_pages),
                        "failed": 0,
                        "unavailable": 0,
                    },
                    "wordProgress": {
                        "total": total_pages,
                        "completed": len(completed_pages),
                        "pending": total_pages - len(completed_pages),
                        "failed": 0,
                        "unavailable": 0,
                    },
                    "message": "PC 已领取预处理任务",
                    "updatedAtEpochMs": now,
                }
                atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
                source_url = (
                    "/pdf/api/library/ocr/worker/source"
                    f"?contract=reader-library-ocr-worker%2F1&workerId={worker['workerId']}"
                    f"&bookId={job['bookId']}"
                    f"&contentSha256={job['contentSha256']}&jobId={job['jobId']}"
                    f"&generation={job['workerGeneration']}&leaseId={lease_id}"
                )
                return {
                    "lease": {
                        "leaseId": lease_id,
                        "expiresAtEpochMs": job["leaseExpiresAtEpochMs"],
                        "renewAfterMs": 15_000,
                    },
                    "job": {
                        "jobId": job["jobId"],
                        "bookId": job["bookId"],
                        "contentSha256": job["contentSha256"],
                        "engine": engine,
                        "executor": "pc",
                        "processingProfile": job["processingProfile"],
                        "generation": job["workerGeneration"],
                        "totalPages": total_pages,
                        "sourceSize": source_size,
                        "sourceUrl": source_url,
                        "completedPages": completed_pages,
                        "limits": {
                            "maxPages": self.max_pages,
                            "maxPdfBytes": self.max_pdf_bytes,
                            "maxPageBytes": min(
                                self.max_pc_page_bytes,
                                int(worker.get("maxPageBytes") or self.max_pc_page_bytes),
                            ),
                            "maxFormulaBytes": self.max_pc_formula_bytes,
                        },
                    },
                    "desiredState": "running",
                }
        return None

    @staticmethod
    def _page_for_pc_done(job_dir: Path, page_number: int, job: dict) -> bool:
        from reader_book_ocr_worker import _page_done

        if not _page_done(
            job_dir / "pages" / f"p{page_number:06d}.json",
            job["bookId"],
            job["contentSha256"],
            job["engine"],
            job["processingProfile"],
        ):
            return False
        value = ReaderBookOcrService._read_optional(
            job_dir / "pages" / f"p{page_number:06d}.json"
        ) or {}
        return ReaderBookOcrService._processing_identity(value) == (
            "pc", PROCESSING_PROFILES["pc"]
        )

    def pc_worker_source(self, payload: dict) -> tuple[dict, Path, dict]:
        if set(payload) != set(PC_IDENTITY_FIELDS):
            raise ReaderBookOcrError(
                "invalid-request", "invalid PC OCR source fields", status=400
            )
        resolved = self.resolve(
            str(payload.get("bookId") or ""), str(payload.get("contentSha256") or "")
        )
        with exclusive_lock(self.lock_path):
            _version_dir, _job_dir, job, desired = self._pc_identity_job_locked(payload)
            return resolved.entry, resolved.path, self._pc_lease_wire(job, desired)

    @staticmethod
    def _validate_pc_progress(value, total_pages: int) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {
            "textCompleted", "wordCompleted", "formulaDetected",
            "formulaRecognized", "totalPages",
        }:
            raise ReaderBookOcrError(
                "invalid-worker-progress", "invalid PC OCR progress", status=400
            )
        out = {}
        for key, raw in value.items():
            if isinstance(raw, bool):
                raise ReaderBookOcrError(
                    "invalid-worker-progress", "invalid PC OCR progress", status=400
                )
            try:
                number = int(raw)
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "invalid-worker-progress", "invalid PC OCR progress", status=400
                ) from exc
            if number < 0 or number > 10_000_000:
                raise ReaderBookOcrError(
                    "invalid-worker-progress", "invalid PC OCR progress", status=400
                )
            out[key] = number
        if "totalPages" in out and out["totalPages"] != int(total_pages):
            raise ReaderBookOcrError(
                "worker-page-count-changed", "PC OCR page count disagrees with Pi", status=409
            )
        if out.get("textCompleted", 0) > total_pages or out.get("wordCompleted", 0) > total_pages:
            raise ReaderBookOcrError(
                "invalid-worker-progress", "PC OCR page progress exceeds the book", status=400
            )
        if out.get("formulaRecognized", 0) > out.get(
            "formulaDetected", out.get("formulaRecognized", 0)
        ):
            raise ReaderBookOcrError(
                "invalid-worker-progress", "PC OCR formula progress is inconsistent", status=400
            )
        return out

    def pc_worker_heartbeat(self, payload: dict) -> dict:
        allowed = set(PC_IDENTITY_FIELDS) | {
            "phase", "currentPage", "state", "error", "progress"
        }
        if not isinstance(payload, dict) or set(payload) - allowed or not PC_IDENTITY_FIELDS <= set(payload):
            raise ReaderBookOcrError(
                "invalid-request", "invalid PC OCR heartbeat fields", status=400
            )
        self._bounded_json_bytes(payload, MAX_PC_PROGRESS_BYTES_DEFAULT, "worker-progress-too-large")
        self.resolve(str(payload.get("bookId") or ""), str(payload.get("contentSha256") or ""))
        with exclusive_lock(self.lock_path):
            _version_dir, job_dir, job, desired = self._pc_identity_job_locked(payload)
            total_pages = int(job.get("totalPages") or 0)
            self._validate_pc_progress(payload.get("progress"), total_pages)
            phase = payload.get("phase")
            if phase is not None and phase not in PC_PHASES:
                raise ReaderBookOcrError(
                    "invalid-worker-phase", "invalid PC OCR phase", status=400
                )
            current_page = payload.get("currentPage")
            if current_page is not None:
                try:
                    current_page = int(current_page)
                except (TypeError, ValueError) as exc:
                    raise ReaderBookOcrError(
                        "invalid-worker-page", "invalid PC OCR current page", status=400
                    ) from exc
                if current_page < 1 or current_page > total_pages:
                    raise ReaderBookOcrError(
                        "invalid-worker-page", "invalid PC OCR current page", status=400
                    )
            reported_state = payload.get("state", "running")
            if reported_state not in ("running", "paused", "cancelled", "failed"):
                raise ReaderBookOcrError(
                    "invalid-worker-state", "invalid PC OCR worker state", status=400
                )
            changes = {
                "phase": phase or job.get("phase") or "preparing",
                "currentPage": current_page,
                "executorOnline": True,
                "executorLastSeenAtEpochMs": _now_ms(),
            }
            if reported_state == "paused":
                if desired != "paused":
                    raise ReaderBookOcrError(
                        "unexpected-worker-stop", "PC OCR pause was not requested", status=409
                    )
                changes.update({
                    "state": "paused", "message": "PC 已保存完成页并暂停",
                    "canPause": False, "canResume": True, "canCancel": True,
                    "leaseId": None, "leaseWorkerId": None, "leaseExpiresAtEpochMs": None,
                })
            elif reported_state == "cancelled":
                if desired != "cancelled":
                    raise ReaderBookOcrError(
                        "unexpected-worker-stop", "PC OCR cancellation was not requested", status=409
                    )
                changes.update({
                    "state": "cancelled", "message": "PC 预处理已取消；完成页保留",
                    "canPause": False, "canResume": False, "canCancel": False,
                    "canRetry": True, "leaseId": None, "leaseWorkerId": None,
                    "leaseExpiresAtEpochMs": None,
                })
            elif reported_state == "failed":
                changes.update({
                    "state": "failed", "errorCode": "pc-worker-failed",
                    "error": self._sanitize_worker_error(payload.get("error") or "PC OCR worker failed"),
                    "message": "PC 预处理失败；完成页保留，可重试",
                    "canPause": False, "canResume": False, "canCancel": False,
                    "canRetry": True, "leaseId": None, "leaseWorkerId": None,
                    "leaseExpiresAtEpochMs": None,
                })
            elif desired == "running":
                changes.update({
                    "state": "running", "canPause": True, "canCancel": True,
                    "message": "PC 正在预处理",
                })
            else:
                changes.update({
                    "state": "pause-requested" if desired == "paused" else "cancel-requested",
                    "message": "PC 正在保存完成页后停止",
                })
            job = {**job, **changes, "updatedAtEpochMs": _now_ms()}
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
            response = self._pc_lease_wire(job, desired)
            response["job"] = _safe_public_job(job)
            return response

    def _normalize_pc_page(self, page: dict, page_number: int, job: dict) -> tuple[dict, bytes]:
        allowed = {
            "schema", "bookId", "contentSha256", "engine", "pageNumber",
            "page_w", "page_h", "imageWidth", "imageHeight", "chars",
            "furigana", "textCharCount", "generatedAtEpochMs", "tokenized",
            # 送 Vision 那张图的实际有效 DPI。贴合度出问题时第一个要看的就是它，
            # 所以它必须跟着页一起存下来 —— 事后没法重算。
            #
            # ⚠ 2026-08-19 的教训:worker 那边加了字段却忘了在这里放行,于是 PC 传
            #   上来的**每一页**都被 400 拒,整本预处理 0/53 失败。这个白名单是
            #   拒绝式的(`set(page) - allowed` 非空即拒),给页加字段必须同时改这里。
            "visionEffectiveDpi", "visionDpiShortfall",
        }
        if not isinstance(page, dict) or set(page) - allowed:
            raise ReaderBookOcrError(
                "invalid-worker-page", "invalid PC OCR page schema", status=400
            )
        if (
            page.get("schema") != "reader-page-chars/1"
            or page.get("bookId") != job.get("bookId")
            or page.get("contentSha256") != job.get("contentSha256")
            or page.get("engine") != job.get("engine")
            or page.get("pageNumber") != page_number
        ):
            raise ReaderBookOcrError(
                "worker-page-identity-mismatch", "PC OCR page identity disagrees", status=409
            )
        try:
            page_w = float(page.get("page_w"))
            page_h = float(page.get("page_h"))
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError(
                "invalid-worker-page", "invalid PC OCR page geometry", status=400
            ) from exc
        chars = page.get("chars")
        furigana = page.get("furigana")
        if (
            not math.isfinite(page_w) or page_w <= 0
            or not math.isfinite(page_h) or page_h <= 0
            or not isinstance(chars, list) or len(chars) > 2_000_000
            or not isinstance(furigana, list) or len(furigana) > 2_000_000
        ):
            raise ReaderBookOcrError(
                "invalid-worker-page", "invalid PC OCR page content", status=400
            )
        for key, maximum in (
            ("imageWidth", 100_000),
            ("imageHeight", 100_000),
            ("textCharCount", 100_000_000),
            ("generatedAtEpochMs", 10_000_000_000_000),
        ):
            if key in page and (
                isinstance(page[key], bool)
                or not isinstance(page[key], int)
                or page[key] < 0
                or page[key] > maximum
            ):
                raise ReaderBookOcrError(
                    "invalid-worker-page", "invalid PC OCR page metadata", status=400
                )
        if "tokenized" in page and not isinstance(page["tokenized"], bool):
            raise ReaderBookOcrError(
                "invalid-worker-page", "invalid PC OCR tokenization state", status=400
            )
        char_fields = {
            "c", "x0", "y0", "x1", "y1", "w", "bk", "b", "sp", "line", "conf",
            "vertical",
        }
        for char in chars:
            if (
                not isinstance(char, dict)
                or set(char) - char_fields
                or not isinstance(char.get("c"), str)
                or len(char.get("c")) > 32
            ):
                raise ReaderBookOcrError(
                    "invalid-worker-page", "invalid PC OCR character", status=400
                )
            try:
                geometry = [float(char.get(key)) for key in ("x0", "y0", "x1", "y1")]
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "invalid-worker-page", "invalid PC OCR character geometry", status=400
                ) from exc
            if (
                not all(math.isfinite(value) and abs(value) <= 10_000_000 for value in geometry)
                or geometry[0] > geometry[2]
                or geometry[1] > geometry[3]
            ):
                raise ReaderBookOcrError(
                    "invalid-worker-page", "invalid PC OCR character geometry", status=400
                )
            for key in ("w", "bk", "b", "sp", "line"):
                if key in char and (isinstance(char[key], bool) or not isinstance(char[key], int)):
                    raise ReaderBookOcrError(
                        "invalid-worker-page", "invalid PC OCR character metadata", status=400
                    )
            if "vertical" in char and not isinstance(char["vertical"], bool):
                raise ReaderBookOcrError(
                    "invalid-worker-page", "invalid PC OCR character direction", status=400
                )
            if "conf" in char and char["conf"] is not None:
                try:
                    confidence = float(char["conf"])
                except (TypeError, ValueError) as exc:
                    raise ReaderBookOcrError(
                        "invalid-worker-page", "invalid PC OCR character confidence", status=400
                    ) from exc
                if not math.isfinite(confidence):
                    raise ReaderBookOcrError(
                        "invalid-worker-page", "invalid PC OCR character confidence", status=400
                    )
        if any(not isinstance(item, dict) for item in furigana):
            raise ReaderBookOcrError(
                "invalid-worker-page", "invalid PC OCR furigana", status=400
            )
        normalized = dict(page)
        normalized["page_w"] = page_w
        normalized["page_h"] = page_h
        normalized["executor"] = "pc"
        normalized["processingProfile"] = job["processingProfile"]
        encoded = self._bounded_json_bytes(
            normalized, self.max_pc_page_bytes, "worker-page-too-large"
        )
        return normalized, encoded

    def upload_pc_page(self, page_number: int, payload: dict) -> dict:
        allowed = set(PC_IDENTITY_FIELDS) | {"page", "progress"}
        if not isinstance(payload, dict) or set(payload) - allowed or not PC_IDENTITY_FIELDS <= set(payload):
            raise ReaderBookOcrError(
                "invalid-request", "invalid PC OCR page upload fields", status=400
            )
        self._bounded_json_bytes(
            payload, self.max_pc_page_bytes + MAX_PC_PROGRESS_BYTES_DEFAULT,
            "worker-page-too-large",
        )
        self.resolve(str(payload.get("bookId") or ""), str(payload.get("contentSha256") or ""))
        with exclusive_lock(self.lock_path):
            _version_dir, job_dir, job, desired = self._pc_identity_job_locked(payload)
            total_pages = int(job.get("totalPages") or 0)
            if page_number < 1 or page_number > total_pages:
                raise ReaderBookOcrError(
                    "invalid-worker-page", "PC OCR page is outside the book", status=400
                )
            self._validate_pc_progress(payload.get("progress"), total_pages)
            normalized, _encoded = self._normalize_pc_page(payload.get("page"), page_number, job)
            page_path = job_dir / "pages" / f"p{page_number:06d}.json"
            existing_page = self._read_optional(page_path)
            already = existing_page == normalized
            was_done = self._page_for_pc_done(job_dir, page_number, job)
            old_recognized = bool((existing_page or {}).get("chars")) if was_done else False
            atomic_write_json(page_path, normalized, indent=None, mode=0o600)
            completed = min(total_pages, int(job.get("successfulPages") or 0) + (0 if was_done else 1))
            recognized = max(
                0,
                int(job.get("recognizedPages") or 0)
                - (1 if old_recognized else 0)
                + (1 if normalized.get("chars") else 0),
            )
            state = job.get("state")
            if desired == "running":
                state = "running"
            job = {
                **job,
                "state": state,
                "processedPages": completed,
                "successfulPages": completed,
                "recognizedPages": recognized,
                "currentPage": None,
                "textState": "succeeded" if completed == total_pages else "running",
                "textProgress": {
                    "total": total_pages, "completed": completed,
                    "pending": total_pages - completed, "failed": 0, "unavailable": 0,
                },
                "wordProgress": {
                    "total": total_pages, "completed": completed,
                    "pending": total_pages - completed, "failed": 0, "unavailable": 0,
                },
                "percent": round(completed * 75 / max(1, total_pages), 1),
                "message": f"PC 已上传文字页 {completed}/{total_pages}",
                "updatedAtEpochMs": _now_ms(),
            }
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
            response = self._pc_lease_wire(job, desired)
            response.update({"accepted": True, "already": already, "page": page_number})
            response["job"] = _safe_public_job(job)
            return response

    def upload_pc_formulas(self, payload: dict) -> dict:
        allowed = set(PC_IDENTITY_FIELDS) | {
            "formula", "formulaState", "formulaReason", "progress"
        }
        if not isinstance(payload, dict) or set(payload) - allowed or not PC_IDENTITY_FIELDS <= set(payload):
            raise ReaderBookOcrError(
                "invalid-request", "invalid PC OCR formula upload fields", status=400
            )
        self._bounded_json_bytes(payload, self.max_pc_formula_bytes, "worker-formula-too-large")
        self.resolve(str(payload.get("bookId") or ""), str(payload.get("contentSha256") or ""))
        with exclusive_lock(self.lock_path):
            _version_dir, job_dir, job, desired = self._pc_identity_job_locked(payload)
            total_pages = int(job.get("totalPages") or 0)
            self._validate_pc_progress(payload.get("progress"), total_pages)
            formula = payload.get("formula")
            if (
                not isinstance(formula, dict)
                or set(formula) - {"schema", "bookId", "contentSha256", "formulas"}
                or formula.get("schema") != "reader-formula-regions/1"
                or formula.get("bookId") != job.get("bookId")
                or formula.get("contentSha256") != job.get("contentSha256")
                or not isinstance(formula.get("formulas"), list)
                or len(formula.get("formulas")) > 200_000
            ):
                raise ReaderBookOcrError(
                    "invalid-worker-formula", "invalid PC OCR formula schema", status=400
                )
            normalized = []
            recognized = 0
            for item in formula["formulas"]:
                if not isinstance(item, dict) or set(item) - {
                    "page", "bbox", "conf", "latex", "multiline", "latexEngine", "latex_engine"
                }:
                    raise ReaderBookOcrError(
                        "invalid-worker-formula", "invalid PC OCR formula record", status=400
                    )
                try:
                    formula_page = int(item.get("page"))
                    bbox = [float(number) for number in item.get("bbox")]
                except (TypeError, ValueError) as exc:
                    raise ReaderBookOcrError(
                        "invalid-worker-formula", "invalid PC OCR formula geometry", status=400
                    ) from exc
                latex = item.get("latex")
                if latex is not None and (not isinstance(latex, str) or len(latex) > 10_000):
                    raise ReaderBookOcrError(
                        "invalid-worker-formula", "invalid PC OCR formula text", status=400
                    )
                confidence = item.get("conf")
                if confidence is not None:
                    try:
                        confidence = float(confidence)
                    except (TypeError, ValueError) as exc:
                        raise ReaderBookOcrError(
                            "invalid-worker-formula", "invalid PC OCR formula confidence", status=400
                        ) from exc
                    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
                        raise ReaderBookOcrError(
                            "invalid-worker-formula", "invalid PC OCR formula confidence", status=400
                        )
                if "multiline" in item and not isinstance(item["multiline"], bool):
                    raise ReaderBookOcrError(
                        "invalid-worker-formula", "invalid PC OCR multiline state", status=400
                    )
                if (
                    formula_page < 1 or formula_page > total_pages
                    or len(bbox) != 4
                    or not all(math.isfinite(number) and 0 <= number <= 1 for number in bbox)
                    or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]
                ):
                    raise ReaderBookOcrError(
                        "invalid-worker-formula", "invalid PC OCR formula geometry", status=400
                    )
                value = {
                    "page": formula_page,
                    "bbox": bbox,
                    "conf": confidence,
                    "latex": latex,
                }
                if item.get("multiline") is not None:
                    value["multiline"] = bool(item.get("multiline"))
                latex_engine = item.get("latexEngine", item.get("latex_engine"))
                if latex_engine is not None:
                    if not isinstance(latex_engine, str) or len(latex_engine) > 80:
                        raise ReaderBookOcrError(
                            "invalid-worker-formula", "invalid PC OCR formula engine", status=400
                        )
                    value["latex_engine"] = latex_engine
                if latex:
                    recognized += 1
                normalized.append(value)
            formula_state = str(payload.get("formulaState") or "")
            if formula_state not in PC_PUBLISHABLE_FORMULA_STATES:
                raise ReaderBookOcrError(
                    "worker-formula-not-publishable",
                    "PC OCR formula state is not publishable",
                    status=409,
                )
            raw_formula_reason = payload.get("formulaReason")
            if raw_formula_reason is not None and not re.fullmatch(
                r"[a-z][a-z0-9-]{0,63}", str(raw_formula_reason)
            ):
                raise ReaderBookOcrError(
                    "invalid-worker-formula", "invalid PC OCR formula reason", status=400
                )
            formula_reason = str(raw_formula_reason) if raw_formula_reason is not None else None
            formula_count = len(normalized)
            formula_identity_valid = (
                (
                    formula_state == "succeeded"
                    and recognized == formula_count
                    and formula_reason is None
                )
                or (
                    formula_state == "partial"
                    and formula_count > 0
                    and recognized < formula_count
                    and bool(formula_reason)
                )
                or (
                    formula_state == "unavailable"
                    and recognized == 0
                    and bool(formula_reason)
                )
            )
            if not formula_identity_valid:
                raise ReaderBookOcrError(
                    "worker-formula-inconsistent",
                    "PC OCR formula state, reason, and recognized count disagree",
                    status=409,
                )
            formula_path = job_dir / "pc-formulas.json"
            atomic_write_json(formula_path, {"formulas": normalized}, indent=None, mode=0o600)
            formula_unavailable = formula_state == "unavailable"
            job = {
                **job,
                "phase": "finalizing",
                "formulaState": formula_state,
                "formulaReason": formula_reason,
                "formulaTotal": len(normalized),
                "formulaRecognized": recognized,
                "formulaPendingRegions": 0,
                "formulaFailedRegions": max(0, len(normalized) - recognized),
                "formulaProgress": {
                    "total": total_pages,
                    "completed": (0 if formula_unavailable else total_pages),
                    "pending": 0,
                    "failed": 0,
                    "unavailable": (total_pages if formula_unavailable else 0),
                },
                "percent": 95,
                "message": (
                    f"PC 公式不可用（{formula_reason}）；文字层仍可发布"
                    if formula_unavailable
                    else f"PC 已上传公式结果 {recognized}/{len(normalized)}"
                ),
                "updatedAtEpochMs": _now_ms(),
            }
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
            response = self._pc_lease_wire(job, desired)
            response.update({"accepted": True, "formulaCount": len(normalized)})
            response["job"] = _safe_public_job(job)
            return response

    def complete_pc_worker(self, payload: dict) -> dict:
        allowed = set(PC_IDENTITY_FIELDS) | {"totalPages", "progress"}
        if not isinstance(payload, dict) or set(payload) - allowed or not PC_IDENTITY_FIELDS <= set(payload):
            raise ReaderBookOcrError(
                "invalid-request", "invalid PC OCR completion fields", status=400
            )
        self._bounded_json_bytes(payload, MAX_PC_PROGRESS_BYTES_DEFAULT, "worker-progress-too-large")
        resolved = self.resolve(
            str(payload.get("bookId") or ""), str(payload.get("contentSha256") or "")
        )
        with exclusive_lock(self.lock_path):
            version_dir, job_dir, job, desired = self._pc_identity_job_locked(payload)
            if desired != "running":
                raise ReaderBookOcrError(
                    "ocr-worker-stop-requested", "PC OCR must stop before publication", status=409
                )
            total_pages = int(job.get("totalPages") or 0)
            try:
                reported_total = int(payload.get("totalPages"))
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "invalid-worker-page", "PC OCR completion page count is invalid", status=400
                ) from exc
            if reported_total != total_pages:
                raise ReaderBookOcrError(
                    "worker-page-count-changed", "PC OCR page count disagrees with Pi", status=409
                )
            self._validate_pc_progress(payload.get("progress"), total_pages)
            missing = [
                page_number
                for page_number in range(1, total_pages + 1)
                if not self._page_for_pc_done(job_dir, page_number, job)
            ]
            if missing:
                raise ReaderBookOcrError(
                    "worker-pages-incomplete", "PC OCR pages are incomplete", status=409
                )
            formula_path = job_dir / "pc-formulas.json"
            if not formula_path.is_file():
                raise ReaderBookOcrError(
                    "worker-formulas-incomplete", "PC OCR formula result is missing", status=409
                )
            formula_payload = self._read_optional(formula_path) or {}
            formula_records = formula_payload.get("formulas")
            if not isinstance(formula_records, list):
                raise ReaderBookOcrError(
                    "worker-formulas-incomplete", "PC OCR formula result is invalid", status=409
                )
            formula_count = len(formula_records)
            formula_recognized = sum(
                1 for item in formula_records
                if isinstance(item, dict) and bool(item.get("latex"))
            )
            formula_state = str(job.get("formulaState") or "")
            formula_reason = job.get("formulaReason")
            formula_identity_valid = (
                int(job.get("formulaTotal") or 0) == formula_count
                and int(job.get("formulaRecognized") or 0) == formula_recognized
                and (
                    (
                        formula_state == "succeeded"
                        and formula_recognized == formula_count
                        and formula_reason is None
                    )
                    or (
                        formula_state == "partial"
                        and formula_count > 0
                        and formula_recognized < formula_count
                        and bool(formula_reason)
                    )
                    or (
                        formula_state == "unavailable"
                        and formula_recognized == 0
                        and bool(formula_reason)
                    )
                )
            )
            if not formula_identity_valid:
                raise ReaderBookOcrError(
                    "worker-formula-not-publishable",
                    "PC OCR formula result is not in a publishable terminal state",
                    status=409,
                )
            now = _now_ms()
            final_job = {
                key: value
                for key, value in job.items()
                if key not in ("leaseId", "leaseWorkerId", "leaseExpiresAtEpochMs")
            }
            final_job.update({
                "state": "succeeded", "phase": "finalizing",
                "textState": "succeeded", "processedPages": total_pages,
                "successfulPages": total_pages, "failedPages": 0,
                "textProgress": {
                    "total": total_pages, "completed": total_pages,
                    "pending": 0, "failed": 0, "unavailable": 0,
                },
                "wordProgress": {
                    "total": total_pages, "completed": total_pages,
                    "pending": 0, "failed": 0, "unavailable": 0,
                },
                "formulaProgress": ({
                    "total": total_pages, "completed": 0,
                    "pending": 0, "failed": 0, "unavailable": total_pages,
                } if job.get("formulaState") == "unavailable" else {
                    "total": total_pages, "completed": total_pages,
                    "pending": 0, "failed": 0, "unavailable": 0,
                }),
                "currentPage": None, "percent": 100, "etaSeconds": 0,
                "message": (
                    f"PC 预处理完成：文字 {total_pages} 页；公式不可用（{job.get('formulaReason')}）"
                    if job.get("formulaState") == "unavailable"
                    else (
                        f"PC 预处理完成：文字 {total_pages} 页，公式 "
                        f"{int(job.get('formulaRecognized') or 0)}/{int(job.get('formulaTotal') or 0)}"
                    )
                ),
                "canPause": False, "canResume": False, "canCancel": False,
                "canRetry": False, "resultAvailable": True,
                "pageCharsRevision": None, "updatedAtEpochMs": now,
            })
            atomic_write_json(job_dir / "job.json", {**job, "phase": "finalizing"}, indent=2, mode=0o600)
            from reader_book_ocr_worker import _publish_release

            args = SimpleNamespace(
                book_id=job["bookId"], content_sha256=job["contentSha256"],
                engine=job["engine"], max_bytes=self.max_pdf_bytes,
            )
            try:
                revision = _publish_release(
                    args,
                    job_dir,
                    formula_path,
                    final_job,
                    source_path=resolved.path,
                )
            except Exception as exc:
                failed = {
                    **job,
                    "state": "failed", "phase": "finalizing",
                    "errorCode": "ocr-publication-failed",
                    "error": self._sanitize_worker_error(exc),
                    "message": "PC 结果发布失败；完成页保留，可重试",
                    "canPause": False, "canResume": False, "canCancel": False,
                    "canRetry": True, "resultAvailable": False,
                    "leaseId": None, "leaseWorkerId": None, "leaseExpiresAtEpochMs": None,
                    "updatedAtEpochMs": _now_ms(),
                }
                atomic_write_json(job_dir / "job.json", failed, indent=2, mode=0o600)
                raise ReaderBookOcrError(
                    "ocr-publication-failed", "PC OCR publication failed", status=500
                ) from exc
            final_job["pageCharsRevision"] = revision
            final_job["updatedAtEpochMs"] = _now_ms()
            atomic_write_json(job_dir / "job.json", final_job, indent=2, mode=0o600)
            # The common publication path writes the immutable release and fence.
            published = self._published_snapshot(job["bookId"], job["contentSha256"])
            if published is None or published["revision"] != revision:
                raise ReaderBookOcrError(
                    "ocr-publication-incomplete", "PC OCR publication did not commit", status=500
                )
            self._activate_published_locked(version_dir, published)
            return {"published": True, "revision": revision, "job": _safe_public_job(published["job"])}

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
        if job.get("executor") == "pc":
            try:
                lease_expires = int(job.get("leaseExpiresAtEpochMs") or 0)
            except (TypeError, ValueError):
                lease_expires = 0
            if lease_expires > _now_ms():
                return job
            desired = self._pc_desired_state(job_dir)
            if desired == "paused":
                state, message = "paused", "PC 租约已结束，任务已暂停"
            elif desired == "cancelled":
                state, message = "cancelled", "PC 租约已结束，任务已取消"
            else:
                state, message = "queued", "等待 PC 重新领取预处理任务"
            next_job = {
                **job, "state": state, "message": message,
                "leaseId": None, "leaseWorkerId": None, "leaseExpiresAtEpochMs": None,
                "executorOnline": self._pc_online_summary_locked(job.get("engine"))["online"],
                "canPause": state == "queued", "canResume": state == "paused",
                "canCancel": state in ("queued", "paused"),
                "canRetry": state == "cancelled", "updatedAtEpochMs": _now_ms(),
            }
            atomic_write_json(job_dir / "job.json", next_job, indent=2, mode=0o600)
            return next_job
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

    # ── 运行台账（releases-index.json）─────────────────────────────────
    #
    # 真相是 activeRunId；publication.json 降级为它的派生镜像。这样
    # _published_snapshot 与全部读路由一行不改就继续正确工作，也随时可回滚。

    @staticmethod
    def _index_path(version_dir: Path) -> Path:
        return version_dir / "releases-index.json"

    @classmethod
    def _release_dir_for(cls, version_dir: Path, engine: str, revision: str) -> Path:
        if engine == LEGACY_ENGINE:
            return version_dir / LEGACY_ENGINE / "releases" / revision
        return version_dir / "releases" / revision

    @staticmethod
    def _release_rel_for(engine: str, revision: str) -> str:
        if engine == LEGACY_ENGINE:
            return f"legacy/releases/{revision}"
        return f"releases/{revision}"

    @staticmethod
    def _migrated_run_id(engine: str, revision: str) -> str:
        """给磁盘上已存在的 release 派生一个稳定 runId。

        必须是确定性的：回填会在每次 list/status 时按需重跑，非确定性的 id
        会让同一份结果每次都变成"新的一条"。
        """

        digest = hashlib.sha256(f"{engine}\x00{revision}\x00migrated".encode("utf-8")).hexdigest()
        return "ocrrun_" + digest[:16]

    def _scan_release_dirs(self, version_dir: Path) -> list[tuple[str, str]]:
        """枚举磁盘上真实存在的 release，返回 [(engine, revision)]。"""

        found: list[tuple[str, str]] = []
        for engine, root in (
            ("", version_dir / "releases"),
            (LEGACY_ENGINE, version_dir / LEGACY_ENGINE / "releases"),
        ):
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or not re.fullmatch(r"ocr_[0-9a-f]{20}", entry.name):
                    continue
                if not (entry / "attachments.json").is_file():
                    continue
                if engine:
                    found.append((engine, entry.name))
                    continue
                # 非 legacy 的 release 目录本身不带引擎名，engine 记在 job.json 里。
                job = self._read_optional(entry / "job.json") or {}
                actual = str(job.get("engine") or "")
                found.append((actual if actual in ENGINES else "vision", entry.name))
        return found

    def _run_entry_from_release(
        self, version_dir: Path, engine: str, revision: str
    ) -> dict | None:
        release_dir = self._release_dir_for(version_dir, engine, revision)
        if not release_dir.is_dir():
            return None
        job = self._read_optional(release_dir / "job.json") or {}
        result = self._read_optional(release_dir / "result.json") or {}
        manifest = self._read_optional(release_dir / "attachments.json") or {}
        executor, profile = self._processing_identity(job if job else result)
        # 时间取值链，取不到就留 None —— **不编造**，UI 显示"日期未知"。
        published_at = result.get("completedAtEpochMs")
        if not isinstance(published_at, int):
            published_at = result.get("adoptedAtEpochMs")
        if not isinstance(published_at, int):
            try:
                published_at = int(release_dir.stat().st_mtime * 1000)
            except OSError:
                published_at = None
        started_at = job.get("createdAtEpochMs")
        if not isinstance(started_at, int):
            started_at = None
        run_id = str(job.get("runId") or "")
        if not RUN_ID_RE.fullmatch(run_id):
            run_id = self._migrated_run_id(engine, revision)
        total_pages = manifest.get("totalPages")
        if not isinstance(total_pages, int):
            total_pages = result.get("totalPages")
        return {
            "runId": run_id,
            "revision": revision,
            "engine": engine,
            "executor": executor,
            "processingProfile": profile,
            "release": self._release_rel_for(engine, revision),
            "startedAtEpochMs": started_at,
            "publishedAtEpochMs": published_at,
            "totalPages": total_pages if isinstance(total_pages, int) else None,
            "origin": "legacy-adopt" if engine == LEGACY_ENGINE else executor,
        }

    def _reconcile_index_locked(self, book_id: str, content_sha256: str) -> dict:
        """按需回填/校正台账。惰性迁移：不写迁移脚本，也不删任何东西。"""

        version_dir = self._version_dir(book_id, content_sha256)
        runs: list[dict] = []
        for engine, revision in self._scan_release_dirs(version_dir):
            entry = self._run_entry_from_release(version_dir, engine, revision)
            if entry is not None:
                runs.append(entry)
        runs.sort(
            key=lambda item: (item.get("publishedAtEpochMs") or 0, item["runId"]),
            reverse=True,
        )
        existing = self._read_optional(self._index_path(version_dir)) or {}
        active = str(existing.get("activeRunId") or "")
        known = {item["runId"] for item in runs}
        if active not in known:
            active = ""
        if not active:
            # 台账缺 active 时以围栏为准（这也是首次回填的入口）。
            fence = self._read_optional(version_dir / "publication.json") or {}
            fence_revision = str(fence.get("revision") or "")
            fence_engine = str(fence.get("engine") or "")
            for item in runs:
                if item["revision"] == fence_revision and item["engine"] == fence_engine:
                    active = item["runId"]
                    break
        try:
            generation = int(existing.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        index = {
            "contract": RELEASE_INDEX_CONTRACT,
            "bookId": book_id,
            "contentSha256": content_sha256,
            "generation": generation + 1,
            "activeRunId": active or None,
            "runs": runs,
        }
        if version_dir.is_dir():
            atomic_write_json(self._index_path(version_dir), index, indent=2, mode=0o600)
        return index

    def _build_fence_from_release(
        self, book_id: str, content_sha256: str, engine: str, revision: str
    ) -> dict:
        """从 release 目录**重建**发布围栏。

        围栏的每个字段都能从磁盘推出来，所以不在台账里存副本 ——
        存副本就会出现"台账说 A、结果是 B"的第三种不一致。
        """

        version_dir = self._version_dir(book_id, content_sha256)
        release_dir = self._release_dir_for(version_dir, engine, revision)
        manifest_path = release_dir / "attachments.json"
        result = self._read_optional(release_dir / "result.json") or {}
        job = self._read_optional(release_dir / "job.json") or {}
        try:
            manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReaderBookOcrError(
                "ocr-release-missing", "该预处理结果已不在服务器上", status=409
            ) from exc
        source_identity = result.get("sourceIdentity")
        if not isinstance(source_identity, dict):
            raise ReaderBookOcrError(
                "ocr-release-invalid", "该预处理结果缺少来源身份，无法启用", status=409
            )
        executor, profile = self._processing_identity(job if job else result)
        return {
            "contract": PUBLICATION_CONTRACT,
            "bookId": book_id,
            "contentSha256": content_sha256,
            "engine": engine,
            "executor": executor,
            "processingProfile": profile,
            "revision": revision,
            "release": self._release_rel_for(engine, revision),
            "manifestSha256": manifest_sha,
            "sourceIdentity": dict(source_identity),
        }

    def _activate_published_locked(self, version_dir: Path, published: dict) -> None:
        """Repair mutable status pointers from one already-validated release."""
        engine = published["engine"]
        revision = published["revision"]
        executor, processing_profile = self._processing_identity(published["job"])
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
            {
                "engine": engine,
                "executor": executor,
                "processingProfile": processing_profile,
                "revision": revision,
            },
            indent=2,
            mode=0o600,
        )

    def start(
        self,
        book_id: str,
        content_sha256: str,
        engine: str = "vision",
        executor: str = "pi",
        processing_profile: str | None = None,
        force: bool = False,
    ) -> tuple[dict, bool]:
        """Queue preprocessing.

        `force=True` 表示"我知道已经有一份，就是要再跑一份"。用户 2026-08-18
        明确要过：「而不是覆盖或者拒绝进行多次预处理」。默认仍然复用已发布的
        结果 —— 那是省钱省时的正确默认；只有用户明说重跑时才绕过。

        绕过的是两处复用，缺一不可：已发布结果的直接返回，以及 staging 里
        同 identity 的断点续跑（后者会让新参数只作用于"还没做过的页"）。
        """

        resolved = self.resolve(book_id, content_sha256)
        engine = str(engine or "vision").strip().lower()
        if engine not in ENGINES:
            raise ReaderBookOcrError("invalid-engine", "unsupported OCR engine", status=400)
        executor = self._validate_executor(executor)
        processing_profile = self._validate_processing_profile(
            executor, processing_profile
        )
        requested_identity = (executor, processing_profile)
        version_dir = self._version_dir(book_id, content_sha256)
        job_dir = self._job_dir(book_id, content_sha256, engine)
        with exclusive_lock(self.lock_path):
            version_dir.mkdir(parents=True, exist_ok=True)
            current_engine, current = self._current_job_locked(version_dir)
            if current and current.get("state") in ACTIVE_STATES:
                if (
                    current_engine == engine
                    and self._processing_identity(current) == requested_identity
                ):
                    return _safe_public_job(current), True
                raise ReaderBookOcrError(
                    "book-ocr-busy", "another engine is already preprocessing this book", status=409
                )
            active = self._active_jobs_locked()
            if active:
                raise ReaderBookOcrError(
                    "ocr-capacity-busy", "another executor is already preprocessing a book", status=429
                )
            existing = self._job_for_engine(version_dir, engine)
            published = self._published_snapshot(book_id, content_sha256)
            if (
                not force
                and published is not None
                and published["engine"] == engine
                and self._processing_identity(published["job"]) == requested_identity
            ):
                self._activate_published_locked(version_dir, published)
                return _safe_public_job(published["job"]), True
            if job_dir.exists() and (
                force
                or existing is None
                or self._processing_identity(existing) != requested_identity
            ):
                self._archive_mutable_staging_locked(
                    version_dir, job_dir, existing
                )
                existing = None
            now = _now_ms()
            total_pages = (
                self._legacy_page_count(resolved.path)
                if executor == "pc"
                else int((existing or {}).get("totalPages") or 0)
            )
            job = {
                "contract": CONTRACT,
                "jobId": "ocrjob_" + uuid.uuid4().hex,
                "workerGeneration": "ocrgen_" + uuid.uuid4().hex,
                "bookId": book_id,
                "contentSha256": content_sha256,
                "engine": engine,
                "executor": executor,
                "processingProfile": processing_profile,
                "state": "queued",
                "phase": "preparing",
                "processedPages": int((existing or {}).get("successfulPages") or 0),
                "totalPages": total_pages,
                "successfulPages": int((existing or {}).get("successfulPages") or 0),
                "failedPages": 0,
                "recognizedPages": int((existing or {}).get("recognizedPages") or 0),
                "percent": 0,
                "etaSeconds": None,
                "message": (
                    "等待 PC 出站执行器领取任务"
                    if executor == "pc" else "等待 Pi 预处理进程启动"
                ),
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
                    "total": total_pages,
                    "completed": int((existing or {}).get("successfulPages") or 0),
                    "pending": max(
                        0,
                        total_pages
                        - int((existing or {}).get("successfulPages") or 0),
                    ),
                    "failed": 0,
                    "unavailable": 0,
                },
                "wordProgress": {
                    "total": total_pages,
                    "completed": (
                        int((existing or {}).get("successfulPages") or 0)
                        if engine == "vision" else 0
                    ),
                    "pending": max(
                        0,
                        total_pages
                        - (
                            int((existing or {}).get("successfulPages") or 0)
                            if engine == "vision" else 0
                        ),
                    ),
                    "failed": 0,
                    "unavailable": 0,
                },
                "formulaProgress": {
                    "total": total_pages,
                    "completed": 0,
                    "pending": total_pages,
                    "failed": 0,
                    "unavailable": 0,
                },
            }
            job_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(job_dir / "control.json", {"desiredState": "running"}, indent=2, mode=0o600)
            atomic_write_json(job_dir / "job.json", job, indent=2, mode=0o600)
            atomic_write_json(
                version_dir / "current.json",
                {
                    "engine": engine,
                    "executor": executor,
                    "processingProfile": processing_profile,
                },
                indent=2,
                mode=0o600,
            )
            if executor == "pi":
                self._spawn(job_dir, resolved.path, job)
            return _safe_public_job(job), False

    def _decorate_executor_status_locked(self, job: dict, engine: str | None = None) -> dict:
        if job.get("executor") != "pc":
            return job
        pc = self._pc_online_summary_locked(engine or job.get("engine"))
        return {
            **job,
            "executorOnline": pc["online"],
            "executorLastSeenAtEpochMs": pc["lastSeenAtEpochMs"],
        }

    # ── 列举 / 切换 / 删除 ─────────────────────────────────────────────

    def _staging_archive_bytes(self, version_dir: Path) -> int:
        root = version_dir / "staging-archive"
        if not root.is_dir():
            return 0
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def _has_active_job_locked(self, version_dir: Path) -> bool:
        for engine in sorted(ENGINES):
            job = self._read_optional(version_dir / engine / "job.json") or {}
            if job.get("state") in ACTIVE_STATES:
                return True
        return False

    def _public_runs(self, index: dict) -> list[dict]:
        active = index.get("activeRunId")
        by_revision: dict[str, list[str]] = {}
        for item in index.get("runs") or []:
            by_revision.setdefault(item["revision"], []).append(item["runId"])
        out = []
        for item in index.get("runs") or []:
            same = [
                run_id for run_id in by_revision.get(item["revision"], [])
                if run_id != item["runId"]
            ]
            entry = dict(item)
            entry["isActive"] = item["runId"] == active
            # 同一 revision 的多条 run = 重跑出了完全一样的结果。
            # 如实告诉用户，别让他以为两份内容不同。
            entry["sameRevisionRunIds"] = same
            out.append(entry)
        return out

    def list_releases(self, book_id: str, content_sha256: str) -> dict:
        """列出这本书这一版内容下的**全部**预处理结果。"""

        self.resolve(book_id, content_sha256)
        version_dir = self._version_dir(book_id, content_sha256)
        with exclusive_lock(self.lock_path):
            index = self._reconcile_index_locked(book_id, content_sha256)
            return {
                # 跟这一族其它响应同壳（ok + contract），客户端才能用同一套校验。
                "ok": True,
                "contract": RELEASE_INDEX_CONTRACT,
                "bookId": book_id,
                "contentSha256": content_sha256,
                "generation": index["generation"],
                "activeRunId": index.get("activeRunId"),
                "runs": self._public_runs(index),
                "stagingArchiveBytes": self._staging_archive_bytes(version_dir),
            }

    def _write_index_locked(self, version_dir: Path, index: dict) -> None:
        atomic_write_json(self._index_path(version_dir), index, indent=2, mode=0o600)

    def _apply_active_locked(
        self, book_id: str, content_sha256: str, index: dict, run_id: str | None
    ) -> None:
        """把 activeRunId 落到台账，并据此重建/清除派生镜像。

        顺序固定：先写台账（真相），再重建围栏与镜像（派生）。反过来会出现
        "围栏已指向新的、台账还说旧的"，而所有读路由都信围栏。
        """

        version_dir = self._version_dir(book_id, content_sha256)
        index = dict(index)
        index["activeRunId"] = run_id
        index["generation"] = int(index.get("generation") or 0) + 1
        self._write_index_locked(version_dir, index)
        fence_path = version_dir / "publication.json"
        if run_id is None:
            # 没有生效结果：把围栏与镜像一并清掉，让 _published_snapshot
            # 走它已有的"围栏不存在 → None"合法分支。
            for name in ("publication.json", "current.json", "result.json"):
                try:
                    (version_dir / name).unlink()
                except FileNotFoundError:
                    pass
            return
        entry = next(
            (item for item in index.get("runs") or [] if item["runId"] == run_id), None
        )
        if entry is None:
            raise ReaderBookOcrError("ocr-run-unknown", "没有这条预处理记录", status=404)
        fence = self._build_fence_from_release(
            book_id, content_sha256, entry["engine"], entry["revision"]
        )
        atomic_write_json(fence_path, fence, indent=2, mode=0o600)
        published = self._published_snapshot(book_id, content_sha256)
        if published is None:
            raise ReaderBookOcrError(
                "ocr-release-invalid", "该预处理结果无法启用", status=409
            )
        self._activate_published_locked(version_dir, published)

    def activate_run(self, book_id: str, content_sha256: str, run_id: str) -> dict:
        """把某一次预处理结果设为当前生效。"""

        self.resolve(book_id, content_sha256)
        if not RUN_ID_RE.fullmatch(str(run_id or "")):
            raise ReaderBookOcrError("ocr-run-invalid", "预处理记录编号无效", status=400)
        version_dir = self._version_dir(book_id, content_sha256)
        with exclusive_lock(self.lock_path):
            if self._has_active_job_locked(version_dir):
                raise ReaderBookOcrError(
                    "ocr-busy", "这本书正在预处理，请先等它结束", status=409
                )
            index = self._reconcile_index_locked(book_id, content_sha256)
            if not any(item["runId"] == run_id for item in index.get("runs") or []):
                raise ReaderBookOcrError("ocr-run-unknown", "没有这条预处理记录", status=404)
            self._apply_active_locked(book_id, content_sha256, index, run_id)
        return self.list_releases(book_id, content_sha256)

    def delete_run(
        self,
        book_id: str,
        content_sha256: str,
        run_id: str,
        *,
        allow_deactivate: bool = False,
    ) -> dict:
        """删除某一次预处理结果。**只删预处理产物，永不碰原书。**"""

        self.resolve(book_id, content_sha256)
        if not RUN_ID_RE.fullmatch(str(run_id or "")):
            raise ReaderBookOcrError("ocr-run-invalid", "预处理记录编号无效", status=400)
        version_dir = self._version_dir(book_id, content_sha256)
        with exclusive_lock(self.lock_path):
            if self._has_active_job_locked(version_dir):
                raise ReaderBookOcrError(
                    "ocr-busy", "这本书正在预处理，请先等它结束", status=409
                )
            index = self._reconcile_index_locked(book_id, content_sha256)
            runs = list(index.get("runs") or [])
            target = next((item for item in runs if item["runId"] == run_id), None)
            if target is None:
                raise ReaderBookOcrError("ocr-run-unknown", "没有这条预处理记录", status=404)
            remaining = [item for item in runs if item["runId"] != run_id]
            was_active = index.get("activeRunId") == run_id
            if was_active and not allow_deactivate:
                # 不让系统进入"有结果但没有当前生效"的状态：要么先切走，
                # 要么调用方明确表示"删了就先不用了"。
                raise ReaderBookOcrError(
                    "ocr-run-active",
                    "这是当前生效的结果；请先切换到别的结果，或确认删除后暂不使用预处理",
                    status=409,
                )
            # 先把 active 挪走（还有别的就顺位接上，没有就置空），再动磁盘。
            if was_active:
                next_active = remaining[0]["runId"] if remaining else None
                index = {**index, "runs": remaining}
                self._apply_active_locked(book_id, content_sha256, index, next_active)
            else:
                index = {**index, "runs": remaining}
                self._write_index_locked(version_dir, index)
            # 同一 revision 还被别的 run 引用时只减引用，不删物理目录。
            still_referenced = any(
                item["revision"] == target["revision"] and item["engine"] == target["engine"]
                for item in remaining
            )
            if not still_referenced:
                self._discard_release_dir(
                    version_dir, target["engine"], target["revision"]
                )
            # 可变暂存若还指着被删的那一版，一并归档 —— 否则 status() 会拿它
            # 编出"发布未完成，请重试"这种与事实不符的提示。
            self._archive_stale_staging_locked(version_dir, target["revision"])
        return self.list_releases(book_id, content_sha256)

    def _discard_release_dir(self, version_dir: Path, engine: str, revision: str) -> None:
        """先整目录改名，再递归删。

        直接 rmtree 会让目录短暂处于"残缺但存在"的状态，而按 revision 读的路径
        （_snapshot_for_revision）在那一瞬间会从"目录不在 → 409"掉进
        "目录残缺 → 500"。改名让"在"与"不在"之间没有中间态。
        """

        release_dir = self._release_dir_for(version_dir, engine, revision)
        if not release_dir.exists():
            return
        trash = version_dir / f".trash-{uuid.uuid4().hex}"
        try:
            os.replace(release_dir, trash)
        except OSError as exc:
            raise ReaderBookOcrError(
                "ocr-run-delete-failed", "删除预处理结果失败", status=500
            ) from exc
        shutil.rmtree(trash, ignore_errors=True)

    def _archive_stale_staging_locked(self, version_dir: Path, revision: str) -> None:
        for engine in sorted(ENGINES):
            job_path = version_dir / engine / "job.json"
            job = self._read_optional(job_path)
            if not job:
                continue
            if job.get("pageCharsRevision") != revision and job.get("revision") != revision:
                continue
            self._archive_mutable_staging_locked(
                version_dir, version_dir / engine, job
            )

    def status(self, book_id: str, content_sha256: str) -> dict:
        self.resolve(book_id, content_sha256)
        version_dir = self._version_dir(book_id, content_sha256)
        with exclusive_lock(self.lock_path):
            engine, job = self._current_job_locked(version_dir)
            published = self._published_snapshot(book_id, content_sha256)
            if not job:
                if published is not None:
                    self._activate_published_locked(version_dir, published)
                    return _safe_public_job(self._decorate_executor_status_locked(
                        published["job"], published["engine"]
                    ))
                return _safe_public_job({
                    "bookId": book_id,
                    "contentSha256": content_sha256,
                    "state": "idle",
                    "phase": "idle",
                })
            job = self._decorate_executor_status_locked(job, engine)
            publication_matches_job = (
                published is not None
                and published["engine"] == engine
                and self._processing_identity(published["job"])
                    == self._processing_identity(job)
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
                return _safe_public_job(self._decorate_executor_status_locked(
                    published["job"], published["engine"]
                ))
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
                unleased_pc = job.get("executor") == "pc" and not job.get("leaseId")
                job = {**job, "state": ("paused" if unleased_pc else "pause-requested"), "message": ("PC 任务已暂停" if unleased_pc else "保存已完成页后暂停；当前页可能在继续时重做"), "canPause": False, "canResume": unleased_pc, "canCancel": True, "updatedAtEpochMs": _now_ms()}
            elif action == "cancel":
                if state in ("cancelled", "succeeded"):
                    return _safe_public_job(job)
                if state not in ACTIVE_STATES and state != "paused":
                    raise ReaderBookOcrError("ocr-cannot-cancel", "OCR job cannot be cancelled", status=409)
                atomic_write_json(job_dir / "control.json", {"desiredState": "cancelled"}, indent=2, mode=0o600)
                unleased_pc = job.get("executor") == "pc" and not job.get("leaseId")
                job = {**job, "state": ("cancelled" if unleased_pc else "cancel-requested"), "message": ("PC 任务已取消；已完成页面会保留" if unleased_pc else "正在停止；已完成页面会保留"), "canPause": False, "canResume": False, "canCancel": False, "canRetry": unleased_pc, "updatedAtEpochMs": _now_ms()}
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
                    "leaseId": None,
                    "leaseWorkerId": None,
                    "leaseExpiresAtEpochMs": None,
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
                if job.get("executor", "pi") == "pi":
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
        if fence_override is None and not release_dir.is_dir():
            # 这条围栏指向的 release 已经不在磁盘上了（结果被删除，或上一次删除
            # 中途崩了）。**这是合法状态，不是损坏。**
            #
            # 旧行为是继续往下走，然后在 attachments.json 读不到时抛 500
            # ocr-publication-invalid —— 而 status() 是无条件调用本函数的，
            # 于是整本书连状态都查不了、连重跑都发不出去，**书被焊死**。
            # 删除功能一旦上线，这条路径就会被真实走到。
            #
            # 正常的删除会在同一把锁里把围栏一并清掉，所以走到这里通常意味着
            # 中途崩溃；把它当作"这本书当前没有已发布结果"是安全且自愈的：
            # 调用方拿到 None 走它已有的合法分支，随后的 reconcile 会把围栏补掉。
            #
            # ⚠ 只对"读围栏"这条路生效。fence_override 是按 revision 显式读某一份
            #   （_snapshot_for_revision），那里目录不在必须如实报错，不能悄悄回落。
            return None
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
        publication_identity = self._processing_identity(job)
        identity_values = (current, result, job, manifest)
        if any(
            not isinstance(value, dict)
            or not self._processing_identity_valid(value)
            or value.get("engine") != engine
            or value.get("revision", value.get("pageCharsRevision")) != revision
            or self._processing_identity(value) != publication_identity
            for value in identity_values
        ) or (
            not self._processing_identity_valid(fence)
            or self._processing_identity(fence) != publication_identity
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
            formula_state not in FORMULA_STATES
            or manifest.get("formulaState") != formula_state
            or (
                publication_identity[0] == "pc"
                and formula_state not in PC_PUBLISHABLE_FORMULA_STATES
            )
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
                    or not self._processing_identity_valid(derived)
                    or derived.get("schema") != "reader-page-chars/1"
                    or derived.get("bookId") != book_id
                    or derived.get("contentSha256") != content_sha256
                    or derived.get("engine") != engine
                    or self._processing_identity(derived) != publication_identity
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
        published_formula_recognized = 0
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
            if formula.get("latex"):
                published_formula_recognized += 1
        if publication_identity[0] == "pc":
            formula_reason = job.get("formulaReason")
            formula_consistent = (
                int(job.get("formulaTotal") or 0) == len(formula_records)
                and int(job.get("formulaRecognized") or 0)
                    == published_formula_recognized
                and manifest.get("formulaReason") == formula_reason
                and (
                    (
                        formula_state == "succeeded"
                        and published_formula_recognized == len(formula_records)
                        and formula_reason is None
                    )
                    or (
                        formula_state == "partial"
                        and len(formula_records) > 0
                        and published_formula_recognized < len(formula_records)
                        and bool(formula_reason)
                    )
                    or (
                        formula_state == "unavailable"
                        and published_formula_recognized == 0
                        and bool(formula_reason)
                    )
                )
            )
            if not formula_consistent:
                raise ReaderBookOcrError(
                    "ocr-publication-invalid",
                    "PC OCR formula publication is inconsistent",
                    status=500,
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
            executor, processing_profile = self._processing_identity(manifest)
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
                "executor": executor,
                "processingProfile": processing_profile,
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
