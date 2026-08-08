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
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from reader_book_library import BOOK_ID_RE, SHA256_RE, BookLibrary, BookLibraryError, UnknownBookError
from reader_sidecar_store import atomic_write_json, exclusive_lock, read_json


CONTRACT = "reader-library-ocr/1"
ENGINES = frozenset(("vision", "manga"))
ACTIVE_STATES = frozenset(("queued", "running", "pause-requested", "cancel-requested"))
TERMINAL_STATES = frozenset(("paused", "cancelled", "succeeded", "failed"))
CONTROL_STATES = frozenset(("running", "paused", "cancelled"))
MAX_PDF_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024
MAX_PAGES_DEFAULT = 5000


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
    ) -> None:
        self.library = library
        self.state_root = Path(state_root)
        self.project_root = Path(project_root)
        self.lock_path = self.state_root / "jobs.lock"
        self.launcher = launcher or self._launch_process
        self.max_pdf_bytes = int(max_pdf_bytes or _env_positive_int(
            "READER_BOOK_OCR_MAX_PDF_BYTES", MAX_PDF_BYTES_DEFAULT
        ))
        self.max_pages = int(max_pages or _env_positive_int(
            "READER_BOOK_OCR_MAX_PAGES", MAX_PAGES_DEFAULT
        ))

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

    def _pointer_engine(self, version_dir: Path, name: str) -> str | None:
        pointer = self._read_optional(version_dir / name)
        engine = pointer.get("engine") if pointer else None
        return engine if engine in ENGINES else None

    def _job_for_engine(self, version_dir: Path, engine: str) -> dict | None:
        return self._read_optional(version_dir / engine / "job.json")

    def _normalize_dead_job(self, job_dir: Path, job: dict) -> dict:
        if job.get("state") not in ACTIVE_STATES:
            return job
        if job.get("state") == "queued" and not job.get("pid"):
            if _now_ms() - int(job.get("updatedAtEpochMs") or 0) < 120_000:
                return job
        elif _pid_alive(job.get("pid")):
            return job
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
            "canRetry": True,
            "updatedAtEpochMs": _now_ms(),
        }
        atomic_write_json(job_dir / "job.json", next_job, indent=2, mode=0o600)
        return next_job

    def _current_job_locked(self, version_dir: Path) -> tuple[str | None, dict | None]:
        engine = self._pointer_engine(version_dir, "current.json")
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
            self.launcher(job_dir, source_path, job)
        except Exception as exc:
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
            if existing and existing.get("state") == "succeeded":
                atomic_write_json(
                    version_dir / "current.json", {"engine": engine}, indent=2, mode=0o600
                )
                return _safe_public_job(existing), True
            now = _now_ms()
            job = {
                "contract": CONTRACT,
                "jobId": "ocrjob_" + uuid.uuid4().hex,
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
                "resultAvailable": bool((existing or {}).get("resultAvailable")),
                "pageCharsRevision": (existing or {}).get("pageCharsRevision"),
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
            _engine, job = self._current_job_locked(version_dir)
            if not job:
                return _safe_public_job({
                    "bookId": book_id,
                    "contentSha256": content_sha256,
                    "state": "idle",
                    "phase": "idle",
                })
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
                active = [item for item in self._active_jobs_locked() if item.get("jobId") != job.get("jobId")]
                if active:
                    raise ReaderBookOcrError("ocr-capacity-busy", "Pi is already preprocessing another book", status=429)
                now = _now_ms()
                job = {
                    **job,
                    "jobId": "ocrjob_" + uuid.uuid4().hex,
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

    def read_page(self, book_id: str, content_sha256: str, page: int) -> tuple[dict, Path]:
        resolved = self.resolve(book_id, content_sha256)
        try:
            page_number = int(page)
        except (TypeError, ValueError) as exc:
            raise ReaderBookOcrError("invalid-page", "invalid page", status=400) from exc
        if page_number < 1 or page_number > self.max_pages:
            raise ReaderBookOcrError("invalid-page", "invalid page", status=400)
        version_dir = self._version_dir(book_id, content_sha256)
        result = self._read_optional(version_dir / "result.json")
        engine = result.get("engine") if result else None
        if engine not in ENGINES:
            raise ReaderBookOcrError("ocr-result-not-found", "OCR result not found", status=404)
        path = version_dir / engine / "pages" / f"p{page_number:06d}.json"
        if not path.is_file() or path.is_symlink():
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

    def read_formulas(self, book_id: str, content_sha256: str) -> list[dict]:
        """Read the content-versioned formula attachment without using a vault path.

        Text OCR remains usable while formula detection is pending or when an
        older result predates formula export, so a missing attachment is an
        empty formula layer.  A present but malformed or identity-mismatched
        attachment is rejected instead of being silently trusted.
        """
        self.resolve(book_id, content_sha256)
        path = self._version_dir(book_id, content_sha256) / "formulas.json"
        if not path.exists():
            return []
        if not path.is_file() or path.is_symlink():
            raise ReaderBookOcrError(
                "ocr-formula-sidecar-invalid", "formula sidecar is invalid", status=500
            )
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

    def attachment_manifest(self, book_id: str, content_sha256: str) -> dict:
        self.resolve(book_id, content_sha256)
        path = self._version_dir(book_id, content_sha256) / "attachments.json"
        manifest = self._read_optional(path)
        if (
            not manifest
            or manifest.get("contract") != "reader-book-attachments/1"
            or manifest.get("schema") != 1
            or manifest.get("bookId") != book_id
            or manifest.get("contentSha256") != content_sha256
            or manifest.get("category") != "derived"
            or manifest.get("mergePolicy") != "immutable"
            or not re.fullmatch(r"ocr_[0-9a-f]{20}", str(manifest.get("revision") or ""))
            or not isinstance(manifest.get("files"), list)
            or len(manifest.get("files")) > self.max_pages + 1
        ):
            raise ReaderBookOcrError(
                "ocr-attachments-not-found", "OCR attachments are not available", status=404
            )
        seen = set()
        revision = manifest["revision"]
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                raise ReaderBookOcrError(
                    "ocr-attachments-invalid", "OCR attachment manifest is invalid", status=500
                )
            attachment_id = str(entry.get("attachmentId") or "")
            page_match = re.fullmatch(r"ocr-page-(\d{6})", attachment_id)
            if attachment_id != "ocr-formulas" and not page_match:
                raise ReaderBookOcrError(
                    "ocr-attachments-invalid", "OCR attachment id is invalid", status=500
                )
            if page_match and not (1 <= int(page_match.group(1)) <= self.max_pages):
                raise ReaderBookOcrError(
                    "ocr-attachments-invalid", "OCR attachment page is invalid", status=500
                )
            try:
                size = int(entry.get("size"))
            except (TypeError, ValueError) as exc:
                raise ReaderBookOcrError(
                    "ocr-attachments-invalid", "OCR attachment size is invalid", status=500
                ) from exc
            expected_url = (
                f"/pdf/api/library/attachments/{book_id}/{attachment_id}"
                f"?contentSha256={content_sha256}&revision={revision}"
            )
            if (
                attachment_id in seen
                or size < 0
                or not SHA256_RE.fullmatch(str(entry.get("sha256") or ""))
                or entry.get("category") != "derived"
                or entry.get("mergePolicy") != "immutable"
                or entry.get("mediaType") != "application/json"
                or entry.get("downloadUrl") != expected_url
            ):
                raise ReaderBookOcrError(
                    "ocr-attachments-invalid", "OCR attachment manifest is invalid", status=500
                )
            seen.add(attachment_id)
        return manifest

    def read_attachment(
        self,
        book_id: str,
        content_sha256: str,
        attachment_id: str,
    ) -> tuple[dict, Path]:
        manifest = self.attachment_manifest(book_id, content_sha256)
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
        version_dir = self._version_dir(book_id, content_sha256)
        result = self._read_optional(version_dir / "result.json") or {}
        engine = result.get("engine")
        page_match = re.fullmatch(r"ocr-page-(\d{6})", str(attachment_id or ""))
        if page_match and engine in ENGINES:
            path = version_dir / engine / "pages" / f"p{page_match.group(1)}.json"
        elif attachment_id == "ocr-formulas":
            path = version_dir / "formulas.json"
        else:
            raise ReaderBookOcrError(
                "ocr-attachment-not-found", "OCR attachment not found", status=404
            )
        if not path.is_file() or path.is_symlink():
            raise ReaderBookOcrError(
                "ocr-attachment-not-found", "OCR attachment not found", status=404
            )
        # The immutable manifest is also the download integrity boundary.
        import hashlib

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.get("sha256") or path.stat().st_size != entry.get("size"):
            raise ReaderBookOcrError(
                "ocr-attachment-corrupt", "OCR attachment digest mismatch", status=500
            )
        return entry, path


def wire_payload(job: dict, *, already: bool | None = None) -> dict:
    payload = {"ok": True, "contract": CONTRACT, "job": _safe_public_job(job)}
    if already is not None:
        payload["already"] = bool(already)
    return payload
