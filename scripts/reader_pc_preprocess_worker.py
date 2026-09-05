#!/usr/bin/env python3
"""Outbound-only Windows worker for Reader book preprocessing.

The Pi remains the authenticated library/coordinator and the only publisher.
This process claims one leased job at a time, downloads the immutable source by
content digest, performs quality-first OCR locally, and uploads canonical
sidecars.  It never listens on a socket and never receives a bearer token on
the command line.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


WORKER_CONTRACT = "reader-library-ocr-worker/1"
PROCESSING_PROFILE = "quality-first-v6"
PAGE_SCHEMA = "reader-page-chars/1"
FORMULA_SCHEMA = "reader-formula-regions/1"
WORKER_PREFIX = "/pdf/api/library/ocr/worker"
CLAIM_PATH = WORKER_PREFIX + "/claim"
SOURCE_PATH = WORKER_PREFIX + "/source"
HEARTBEAT_PATH = WORKER_PREFIX + "/heartbeat"
FORMULAS_PATH = WORKER_PREFIX + "/formulas"
COMPLETE_PATH = WORKER_PREFIX + "/complete"

SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
FORMULA_REASON_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
FORMULA_RECOGNITION_FAILED = "formula-recognition-failed"
TOKEN_RE = re.compile(
    r"(?i)\b(authorization|api[-_ ]?key|access[-_ ]?token)\s*[:=]?\s*"
    r"(?:bearer\s+)?\S+|\bbearer\s+\S+"
)
QUERY_SECRET_RE = re.compile(
    r"(?i)([?&;](?:api[-_]?key|key|token|access[-_]?token|auth(?:orization)?|"
    r"secret|password)=)[^&#;\s]*"
)
PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/(?:home|tmp|var|opt|srv)/)[^\r\n]*")
PROCESS_INSTANCE_NONCE = secrets.token_hex(16)

QUALITY_PROFILE = {
    "name": "quality-first-v6",
    "textGeometry": "vision-symbols-page-layout-v6",
    "gpuRequired": True,
    # ⚠ 送 Vision 的那张图的分辨率**不在这里定** —— 由 Pi worker 的 _vision_render
    #   统一决定(目标 300dpi / 保底 200dpi / 按上传字节实测回退)。两边共用同一个
    #   函数,不再各写一份参数。
    #
    #   2026-08-18 我在这里放过 ocrRenderDpi/maxImageLongEdge/jpegQuality 三项,并
    #   按"Vision 内部会缩图,大图反而更粗"的假设把它们从 400/6000/95 调到 300/4000/90。
    #   2026-08-19 实测把这个假设证伪了:400dpi 那本的框高/行距是 0.51(最好的一份),
    #   而被长边封顶压到 120dpi 的那本是 1.34(框比行距还高)。真正的变量是**有效 DPI**,
    #   跟提交图的绝对像素数无关。固定的长边封顶对超大页面就是一台降质机器。
    "mangaModel": "mokuro-manga-ocr",
    "layoutModel": "DocLayout-YOLO-DocStructBench",
    "formulaModel": "unimernet-base",
    "formulaCompatibilityFallback": "explicit-pix2tex-only",
}

ALLOWED_PHASES = frozenset(
    (
        "preparing",
        "downloading",
        "text-ocr",
        "text-layer",
        "tokenizing",
        "formula-detect",
        "formula-latex",
        "uploading",
        "finalizing",
    )
)
ALLOWED_STATES = frozenset(("running", "paused", "cancelled", "failed"))


class WorkerError(RuntimeError):
    """A bounded, user-actionable worker error."""


class LeaseStopped(WorkerError):
    def __init__(self, desired_state: str):
        self.desired_state = desired_state
        super().__init__(f"leased job requested {desired_state}")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _current_process_identity() -> dict[str, int]:
    """Return the exact Windows PID generation used by ReaderPC controls."""

    pid = os.getpid()
    if os.name != "nt":
        return {
            "pid": pid,
            "startFileTimeUtc": int(time.time_ns() // 100),
        }

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        kernel32.GetCurrentProcess(),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise WorkerError(
            "could not read PC worker process generation "
            f"(win32={ctypes.get_last_error()})"
        )
    return {
        "pid": pid,
        "startFileTimeUtc": (
            (int(creation.dwHighDateTime) << 32)
            | int(creation.dwLowDateTime)
        ),
    }


def _replace_with_retry(source: Path, target: Path, attempts: int = 6) -> None:
    """Windows 上 os.replace 会撞上 WinError 5/32(杀软/索引器短暂占用目标文件)。
    2026-09-02/04 实锤:worker 因此每隔几页就 PermissionError 退避 10s。短退避重试,最后一次才抛。"""
    delay = 0.2
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(2.0, delay * 2)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    _replace_with_retry(temporary, path)


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def safe_error(exc: BaseException) -> str:
    value = f"{type(exc).__name__}: {exc}"
    value = QUERY_SECRET_RE.sub(r"\1<redacted>", value)
    value = TOKEN_RE.sub(
        lambda match: f"{match.group(1) or 'bearer'}=<redacted>", value
    )
    value = PATH_RE.sub("<path>", value)
    return " ".join(value.split())[:300]


def _same_origin(left: str, right: str) -> bool:
    a = urllib.parse.urlsplit(left)
    b = urllib.parse.urlsplit(right)
    return (a.scheme.lower(), a.hostname, a.port) == (
        b.scheme.lower(),
        b.hostname,
        b.port,
    )


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _same_origin(self.origin, newurl):
            raise WorkerError("Pi response attempted a cross-origin redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    """Small injectable transport; responses are streamed for PDF downloads."""

    def __init__(self, origin: str, timeout: float = 60.0):
        self.origin = origin
        self.timeout = timeout

    def open(self, method: str, url: str, headers: dict[str, str], body: bytes | None):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = urllib.request.build_opener(_SameOriginRedirect(self.origin))
        try:
            return opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read(4096)
                parsed = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(parsed, dict):
                    detail = str(parsed.get("code") or parsed.get("error") or "")
            except Exception:
                detail = ""
            suffix = f" ({detail[:120]})" if detail else ""
            raise WorkerError(f"Pi worker API returned HTTP {exc.code}{suffix}") from None
        except urllib.error.URLError as exc:
            raise WorkerError(f"Pi worker API is unreachable: {exc.reason}") from None


@dataclass(frozen=True)
class Claim:
    worker_id: str
    lease_id: str
    lease_expires_ms: int
    renew_after_ms: int
    job_id: str
    book_id: str
    content_sha256: str
    engine: str
    processing_profile: str
    generation: str
    total_pages: int
    source_url: str
    completed_pages: frozenset[int]
    source_size: int
    max_pages: int
    max_pdf_bytes: int
    max_page_bytes: int
    max_formula_bytes: int

    @classmethod
    def parse(cls, worker_id: str, payload: dict, defaults: dict) -> "Claim":
        if payload.get("contract") != WORKER_CONTRACT:
            raise WorkerError("Pi returned an unsupported worker contract")
        lease = payload.get("lease")
        job = payload.get("job")
        if not isinstance(lease, dict) or not isinstance(job, dict):
            raise WorkerError("Pi returned an incomplete worker claim")
        required_ids = {
            "leaseId": lease.get("leaseId"),
            "jobId": job.get("jobId"),
            "bookId": job.get("bookId"),
            "generation": job.get("generation"),
        }
        if any(not SAFE_ID_RE.fullmatch(str(value or "")) for value in required_ids.values()):
            raise WorkerError("Pi returned an invalid worker identity")
        digest = str(job.get("contentSha256") or "").lower()
        if not SHA256_RE.fullmatch(digest):
            raise WorkerError("Pi returned an invalid content digest")
        engine = str(job.get("engine") or "")
        if engine not in ("vision", "manga", "native"):
            raise WorkerError("Pi returned an unsupported OCR engine")
        if job.get("executor") != "pc":
            raise WorkerError("Pi returned a job for a different executor")
        processing_profile = str(job.get("processingProfile") or "")
        if processing_profile != PROCESSING_PROFILE:
            raise WorkerError("Pi returned an incompatible processing profile")
        source_url = str(job.get("sourceUrl") or "")
        if not source_url:
            raise WorkerError("Pi claim omitted the source URL")
        limits = job.get("limits") if isinstance(job.get("limits"), dict) else {}
        total_pages = max(0, int(job.get("totalPages") or 0))
        source_size = max(0, int(job.get("sourceSize") or 0))
        max_pages = max(1, int(limits.get("maxPages") or 5000))
        max_pdf_bytes = max(
            1,
            min(
                int(defaults["max_pdf_bytes"]),
                int(limits.get("maxPdfBytes") or defaults["max_pdf_bytes"]),
            ),
        )
        completed = set()
        for value in job.get("completedPages") or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                completed.add(number)
        if total_pages > max_pages or any(number > total_pages for number in completed):
            raise WorkerError("Pi returned invalid page bounds")
        if source_size > max_pdf_bytes:
            raise WorkerError("Pi source exceeds the negotiated PDF limit")
        return cls(
            worker_id=worker_id,
            lease_id=str(lease["leaseId"]),
            lease_expires_ms=max(0, int(lease.get("expiresAtEpochMs") or 0)),
            renew_after_ms=max(1000, int(lease.get("renewAfterMs") or 10_000)),
            job_id=str(job["jobId"]),
            book_id=str(job["bookId"]),
            content_sha256=digest,
            engine=engine,
            processing_profile=processing_profile,
            generation=str(job["generation"]),
            total_pages=total_pages,
            source_url=source_url,
            completed_pages=frozenset(completed),
            source_size=source_size,
            max_pages=max_pages,
            max_pdf_bytes=max_pdf_bytes,
            max_page_bytes=max(
                1,
                min(
                    int(defaults["max_page_bytes"]),
                    int(limits.get("maxPageBytes") or defaults["max_page_bytes"]),
                ),
            ),
            max_formula_bytes=max(
                1,
                min(
                    int(defaults["max_page_bytes"]),
                    int(limits.get("maxFormulaBytes") or defaults["max_page_bytes"]),
                ),
            ),
        )

    def identity(self) -> dict:
        return {
            "contract": WORKER_CONTRACT,
            "workerId": self.worker_id,
            "leaseId": self.lease_id,
            "jobId": self.job_id,
            "bookId": self.book_id,
            "contentSha256": self.content_sha256,
            "generation": self.generation,
        }


def _progress(
    total_pages: int,
    *,
    text: int = 0,
    words: int = 0,
    detected: int = 0,
    recognized: int = 0,
) -> dict:
    return {
        "textCompleted": max(0, int(text)),
        "wordCompleted": max(0, int(words)),
        "formulaDetected": max(0, int(detected)),
        "formulaRecognized": max(0, int(recognized)),
        "totalPages": max(0, int(total_pages)),
    }


class PiWorkerApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        worker_id: str,
        *,
        transport=None,
        max_pdf_bytes: int = 2 * 1024 * 1024 * 1024,
        max_page_bytes: int = 32 * 1024 * 1024,
    ):
        base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise WorkerError("PC worker base URL must be Pi HTTPS")
        if not token.strip():
            raise WorkerError("PC worker Bearer token is empty")
        if not SAFE_ID_RE.fullmatch(worker_id):
            raise WorkerError("PC worker ID is invalid")
        self.base_url = base_url
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.token = token.strip()
        self.worker_id = worker_id
        self.transport = transport or UrllibTransport(self.origin)
        self.defaults = {
            "max_pdf_bytes": int(max_pdf_bytes),
            "max_page_bytes": int(max_page_bytes),
        }

    def _url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "User-Agent": "BWReader-PC-OCR/1",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        raw = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self.transport.open(
            method, self._url(path), self._headers(body is not None), raw
        )
        with response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            if status == 204:
                return {}
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise WorkerError("Pi worker API response is too large")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkerError("Pi worker API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise WorkerError("Pi worker API returned a non-object response")
        return value

    @staticmethod
    def _validated_success(
        payload: dict,
        operation: str,
        *,
        required_flag: str | None = None,
    ) -> dict:
        if (
            payload.get("ok") is not True
            or payload.get("contract") != WORKER_CONTRACT
            or (required_flag is not None and payload.get(required_flag) is not True)
        ):
            raise WorkerError(f"Pi returned an invalid {operation} acknowledgement")
        return payload

    def claim(self, engines: tuple[str, ...]) -> Claim | None:
        payload = self._json(
            "POST",
            CLAIM_PATH,
            {
                "contract": WORKER_CONTRACT,
                "workerId": self.worker_id,
                "capabilities": {
                    "engines": list(engines),
                    "maxPdfBytes": self.defaults["max_pdf_bytes"],
                    "maxPageBytes": self.defaults["max_page_bytes"],
                    "processingProfile": PROCESSING_PROFILE,
                },
            },
        )
        if not payload:
            return None
        return Claim.parse(self.worker_id, payload, self.defaults)

    def source_response(self, claim: Claim, offset: int = 0):
        absolute = urllib.parse.urljoin(self.base_url + "/", claim.source_url)
        parsed = urllib.parse.urlsplit(absolute)
        if (
            not _same_origin(self.origin, absolute)
            or parsed.path != SOURCE_PATH
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise WorkerError("Pi claim returned an untrusted source URL")
        headers = self._headers(False)
        headers["Accept"] = "application/pdf"
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        return self.transport.open("GET", absolute, headers, None)

    def heartbeat(
        self,
        claim: Claim,
        *,
        phase: str,
        current_page: int | None = None,
        state: str = "running",
        progress: dict | None = None,
        error: str | None = None,
    ) -> dict:
        if phase not in ALLOWED_PHASES or state not in ALLOWED_STATES:
            raise WorkerError("invalid local worker status")
        body = {
            **claim.identity(),
            "phase": phase,
            "state": state,
            "currentPage": current_page,
        }
        if progress is not None:
            body["progress"] = progress
        if error:
            body["error"] = error[:300]
        return self._validated_success(
            self._json("POST", HEARTBEAT_PATH, body), "heartbeat"
        )

    def put_page(self, claim: Claim, page_number: int, page: dict, progress: dict) -> dict:
        encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > claim.max_page_bytes:
            raise WorkerError(f"canonical page {page_number} exceeds the negotiated limit")
        return self._validated_success(
            self._json(
                "PUT",
                f"{WORKER_PREFIX}/pages/{int(page_number)}",
                {**claim.identity(), "page": page, "progress": progress},
            ),
            "page upload",
            required_flag="accepted",
        )

    def put_formulas(
        self,
        claim: Claim,
        formula: dict,
        formula_state: str,
        formula_reason: str | None,
        progress: dict,
    ) -> dict:
        if formula_state not in ("succeeded", "partial", "unavailable"):
            raise WorkerError("invalid local formula state")
        if formula_reason is not None and not FORMULA_REASON_RE.fullmatch(formula_reason):
            raise WorkerError("invalid local formula reason")
        body = {
            **claim.identity(),
            "formula": formula,
            "formulaState": formula_state,
            "formulaReason": formula_reason,
            "progress": progress,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > claim.max_formula_bytes:
            raise WorkerError("canonical formula attachment exceeds the negotiated limit")
        return self._validated_success(
            self._json("PUT", FORMULAS_PATH, body),
            "formula upload",
            required_flag="accepted",
        )

    def complete(self, claim: Claim, total_pages: int, progress: dict) -> dict:
        return self._validated_success(
            self._json(
                "POST",
                COMPLETE_PATH,
                {
                    **claim.identity(),
                    "totalPages": int(total_pages),
                    "progress": progress,
                },
            ),
            "completion",
            required_flag="published",
        )


def _vision_render_contract(project_root: Path) -> dict:
    """送 Vision 那张图的渲染契约 —— **必须进页缓存的键**。

    这几个值定义在 Pi worker(两边共用同一份实现),不在 QUALITY_PROFILE 里。
    2026-08-19 把它们从 QUALITY_PROFILE 搬走时差点留下一个坑:profile 是页缓存
    的比对项,搬走等于把渲染参数移出了缓存键 —— 以后再调 DPI,旧页会被当成
    "已经做过"直接复用,新参数永远到不了用户手上。所以在这里显式接回来。
    """

    deploy = project_root / "_server_deploy"
    if str(deploy) not in sys.path:
        sys.path.insert(0, str(deploy))
    core = importlib.import_module("reader_book_ocr_worker")
    return {
        "targetDpi": core.VISION_TARGET_DPI,
        "minDpi": core.VISION_MIN_DPI,
        "absoluteMinDpi": core.VISION_ABSOLUTE_MIN_DPI,
        "maxUploadBytes": core.VISION_MAX_UPLOAD_BYTES,
        "absoluteMaxLongEdge": core.VISION_ABSOLUTE_MAX_LONG_EDGE,
        "jpegQuality": core.VISION_JPEG_QUALITY,
    }


class ContentCache:
    def __init__(self, root: Path, project_root: Path | None = None):
        self.root = root
        self.sources = root / "sources"
        self.jobs = root / "jobs"
        self.status_path = root / "worker-status.json"
        self.project_root = project_root
        self._render_contract: dict | None = None

    def render_contract(self) -> dict:
        if self._render_contract is None and self.project_root is not None:
            self._render_contract = _vision_render_contract(self.project_root)
        return self._render_contract or {}

    def status(self, **changes) -> None:
        current = _load_json(self.status_path) or {
            "contract": "reader-pc-ocr-status/1",
            "profile": dict(QUALITY_PROFILE),
        }
        current.update(changes)
        current["updatedAtEpochMs"] = _now_ms()
        _atomic_json(self.status_path, current)

    def source_path(self, digest: str) -> Path:
        return self.sources / f"{digest}.pdf"

    def page_dir(self, claim: Claim) -> Path:
        book_key = hashlib.sha256(claim.book_id.encode("utf-8")).hexdigest()[:20]
        return self.jobs / book_key / claim.content_sha256 / claim.engine / "pages"

    def cached_page(self, claim: Claim, page_number: int) -> dict | None:
        value = _load_json(self.page_dir(claim) / f"p{page_number:06d}.json")
        if (
            value
            and value.get("contract") == "reader-pc-ocr-page-cache/1"
            and value.get("profile") == QUALITY_PROFILE
            and value.get("visionRender") == self.render_contract()
            and isinstance(value.get("page"), dict)
        ):
            value = value["page"]
        if (
            value
            and value.get("schema") == PAGE_SCHEMA
            and value.get("bookId") == claim.book_id
            and value.get("contentSha256") == claim.content_sha256
            and value.get("engine") == claim.engine
            and int(value.get("pageNumber") or 0) == page_number
            and isinstance(value.get("chars"), list)
        ):
            return value
        return None

    def save_page(self, claim: Claim, page_number: int, page: dict) -> None:
        _atomic_json(
            self.page_dir(claim) / f"p{page_number:06d}.json",
            {
                "contract": "reader-pc-ocr-page-cache/1",
                "profile": dict(QUALITY_PROFILE),
                "visionRender": dict(self.render_contract()),
                "page": page,
            },
        )

    def download(
        self,
        api: PiWorkerApi,
        claim: Claim,
        checkpoint: Callable[[], None] | None = None,
    ) -> Path:
        self.sources.mkdir(parents=True, exist_ok=True)
        final = self.source_path(claim.content_sha256)
        if final.exists():
            final_size = final.stat().st_size
            if (
                final_size <= claim.max_pdf_bytes
                and (claim.source_size == 0 or final_size == claim.source_size)
                and _sha256_file(final) == claim.content_sha256
            ):
                return final
            final.replace(final.with_suffix(f".invalid-{_now_ms()}"))
        partial = final.with_suffix(".pdf.part")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > claim.max_pdf_bytes:
            partial.replace(partial.with_suffix(f".oversize-{_now_ms()}"))
            offset = 0
        elif offset > 0 and _sha256_file(partial) == claim.content_sha256:
            if claim.source_size > 0 and offset != claim.source_size:
                partial.replace(partial.with_suffix(f".size-mismatch-{_now_ms()}"))
                raise WorkerError("cached PDF size did not match the claimed source")
            _replace_with_retry(partial, final)
            return final
        response = api.source_response(claim, offset)
        with response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            if status not in (200, 206):
                raise WorkerError(f"Pi source download returned HTTP {status}")
            append = status == 206 and offset > 0
            if append:
                content_range = str(response.headers.get("Content-Range") or "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise WorkerError("Pi source resume range did not match the local cache")
            mode = "ab" if append else "wb"
            total = offset if append else 0
            with partial.open(mode) as output:
                while True:
                    if checkpoint is not None:
                        checkpoint()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > claim.max_pdf_bytes:
                        raise WorkerError("Pi PDF exceeds the negotiated size limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if _sha256_file(partial) != claim.content_sha256:
            partial.replace(partial.with_suffix(f".digest-mismatch-{_now_ms()}"))
            raise WorkerError("downloaded PDF digest did not match the claimed content")
        if claim.source_size > 0 and partial.stat().st_size != claim.source_size:
            partial.replace(partial.with_suffix(f".size-mismatch-{_now_ms()}"))
            raise WorkerError("downloaded PDF size did not match the claimed source")
        _replace_with_retry(partial, final)
        return final


class QualityPipeline:
    """Quality-first local pipeline with strict CUDA and lazy model loading."""

    DOCLAYOUT_REPO = "juliozhao/DocLayout-YOLO-DocStructBench"
    DOCLAYOUT_FILE = "doclayout_yolo_docstructbench_imgsz1024.pt"

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.document = None
        self._core = None
        self._manga = None
        self._layout = None
        self._formula = None
        self._formula_backend = None
        self._torch = None

    @staticmethod
    def supported_engines(configured: tuple[str, ...]) -> tuple[str, ...]:
        # native（2026-09-06）：有文字层的书直接读字符层，不需要 CUDA 模型。
        return tuple(engine for engine in configured if engine in ("vision", "manga", "native"))

    @staticmethod
    def cuda_status() -> dict:
        try:
            torch = importlib.import_module("torch")
        except Exception as exc:
            raise WorkerError("quality-first profile requires an importable PyTorch") from exc
        if not bool(torch.cuda.is_available()):
            raise WorkerError("quality-first profile requires CUDA; CPU fallback is disabled")
        try:
            index = int(torch.cuda.current_device())
            name = str(torch.cuda.get_device_name(index))
        except Exception as exc:
            raise WorkerError("CUDA device could not be inspected") from exc
        return {
            "available": True,
            "deviceIndex": index,
            "deviceName": name,
            "cudaVersion": str(getattr(torch.version, "cuda", "") or "unknown"),
        }

    def _require_cuda(self):
        if self._torch is None:
            self._torch = importlib.import_module("torch")
        if not bool(self._torch.cuda.is_available()):
            raise WorkerError("quality-first profile requires CUDA; CPU fallback is disabled")
        return self._torch

    def open(self, pdf: Path) -> int:
        self._require_cuda()
        fitz = importlib.import_module("fitz")
        self.document = fitz.open(str(pdf))
        total = int(self.document.page_count)
        if total <= 0:
            raise WorkerError("PDF has no pages")
        return total

    def _worker_core(self):
        if self._core is None:
            deploy = self.project_root / "_server_deploy"
            if str(deploy) not in sys.path:
                sys.path.insert(0, str(deploy))
            self._core = importlib.import_module("reader_book_ocr_worker")
        return self._core

    def _manga_engine(self):
        if self._manga is None:
            self._require_cuda()
            module = importlib.import_module("mokuro.manga_page_ocr")
            # PC quality profile explicitly allows/needs GPU.  Never copy the
            # Pi worker's force_cpu=True setting.
            self._manga = module.MangaPageOcr(force_cpu=False)
            self._assert_model_cuda(self._manga, "manga OCR")
        return self._manga

    @staticmethod
    def _assert_model_cuda(model, label: str) -> None:
        """Reject unknown/CPU placement instead of silently losing quality."""
        queue = [(model, 0)]
        seen = set()
        devices = set()
        while queue:
            value, depth = queue.pop(0)
            if id(value) in seen or depth > 3:
                continue
            seen.add(id(value))
            try:
                device = getattr(value, "device", None)
            except Exception:
                device = None
            if device is not None:
                try:
                    devices.add(str(device).lower())
                except Exception:
                    pass
            if any("cuda" in item for item in devices):
                return
            try:
                parameters = getattr(value, "parameters", None)
            except Exception:
                parameters = None
            if callable(parameters):
                try:
                    first = next(iter(parameters()))
                    devices.add(str(getattr(first, "device", "unknown")).lower())
                except (StopIteration, TypeError, RuntimeError):
                    pass
            if any("cuda" in item for item in devices):
                return
            try:
                providers = getattr(value, "get_providers", None)
            except Exception:
                providers = None
            if callable(providers):
                try:
                    devices.update(str(item).lower() for item in providers())
                except Exception:
                    pass
            if any("cuda" in item for item in devices):
                return
            if depth < 3:
                try:
                    children = tuple(vars(value).values())
                except (TypeError, RuntimeError):
                    children = ()
                for child in children:
                    try:
                        traversable = hasattr(child, "__dict__") or callable(
                            getattr(child, "parameters", None)
                        )
                    except Exception:
                        traversable = False
                    if traversable:
                        queue.append((child, depth + 1))
        if not any("cuda" in item for item in devices):
            detail = ",".join(sorted(devices))[:120] or "unknown"
            raise WorkerError(
                f"{label} did not prove CUDA placement ({detail}); CPU fallback is disabled"
            )

    def _vision_page(self, page) -> tuple[list[dict], str, int, int, float]:
        scripts = self.project_root / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        vision = importlib.import_module("google_vision_ocr")
        # 渲染参数与 Pi 共用一份实现;这里若再抄一遍,两边迟早又会漂开。
        image, image_w, image_h, effective_dpi = self._worker_core()._vision_render(page)
        raw = vision.ocr_one_page(vision._load_key(), image)
        sx = float(page.rect.width) / image_w
        sy = float(page.rect.height) / image_h
        chars = []
        for item in raw.get("chars") or []:
            bbox = item.get("bbox")
            text = item.get("c")
            if not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(text, str) or not text:
                continue
            x0, y0, x1, y1 = (float(value) for value in bbox)
            char = {
                "c": text,
                "x0": round(x0 * sx, 3),
                "y0": round(y0 * sy, 3),
                "x1": round(x1 * sx, 3),
                "y1": round(y1 * sy, 3),
                "w": int(item.get("w", -1)),
                "bk": int(item.get("bk", -1)),
                "b": 0,
            }
            if item.get("sp"):
                char["sp"] = 1
            chars.append(char)
        return chars, str(raw.get("text") or ""), image_w, image_h, effective_dpi

    def page(self, claim: Claim, page_number: int) -> dict:
        if self.document is None:
            raise WorkerError("quality pipeline is not open")
        page = self.document[page_number - 1]
        core = self._worker_core()
        effective_dpi = None
        if claim.engine == "vision":
            chars, text, image_w, image_h, effective_dpi = self._vision_page(page)
            layout = core._vision_page_layout(
                chars,
                page_w=float(page.rect.width),
                page_h=float(page.rect.height),
            )
            # vision 分支以前直接跳到下面标 tokenized=True,却从没真跑过分词 ——
            # 服务器那趟看见这个标记就 continue,于是 PC 出的 vision 页永远没分词。
            # 2026-09-02:分词以块为边界,必须把版面(表格格子)一起交给分词器。
            chars = core._tokenize_chars(chars, layout)
        elif claim.engine == "native":
            # 有文字层的书：不 OCR，直接读 PDF 字符层，只做分词（用户 2026-09-06）。
            # 实现与 Pi worker 共用 core._native_page，这里不再抄一遍。
            chars, text, image_w, image_h = core._native_page(page)
            layout = core._vision_page_layout(
                chars,
                page_w=float(page.rect.width),
                page_h=float(page.rect.height),
            )
            if chars:
                layout["textSource"] = "native"
            chars = core._tokenize_chars(chars, layout)
        else:
            vision_chars = None
            try:
                (
                    vision_chars,
                    _vision_text,
                    _vision_image_w,
                    _vision_image_h,
                    effective_dpi,
                ) = self._vision_page(page)
            except Exception:
                # Preserve a usable Manga-only result if Vision is offline.
                # Lines that Vision cannot match also fall back independently.
                vision_chars = None
                effective_dpi = None
            chars, text, image_w, image_h, layout = core._manga_page(
                page,
                self._manga_engine(),
                vision_chars=vision_chars,
                include_layout=True,
            )
            chars = core._tokenize_chars(chars, layout)   # 以块为边界(2026-09-02)
        sidecar = {
            "schema": PAGE_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "engine": claim.engine,
            "pageNumber": int(page_number),
            "page_w": float(page.rect.width),
            "page_h": float(page.rect.height),
            "imageWidth": image_w,
            "imageHeight": image_h,
            "chars": chars,
            "layout": layout,
            "furigana": [],
            "textCharCount": len("".join(text.split())),
            "tokenized": True,
            "tokenizeSchema": int(getattr(core, "_TOKENIZE_SCHEMA", 1)),
            "generatedAtEpochMs": _now_ms(),
        }
        if effective_dpi is not None:
            sidecar["visionEffectiveDpi"] = round(effective_dpi, 1)
            if effective_dpi < core.VISION_MIN_DPI:
                sidecar["visionDpiShortfall"] = True
        return sidecar

    def release_text_model(self) -> None:
        self._manga = None
        self._release_cuda()

    def _layout_model(self):
        if self._layout is None:
            self._require_cuda()
            model_path = str(os.environ.get("BW_READER_PC_DOCLAYOUT_MODEL") or "").strip()
            if not model_path:
                try:
                    hub = importlib.import_module("huggingface_hub")
                    model_path = hub.hf_hub_download(
                        repo_id=self.DOCLAYOUT_REPO,
                        filename=self.DOCLAYOUT_FILE,
                        local_files_only=True,
                    )
                except Exception as exc:
                    raise WorkerError(
                        "quality DocLayout weight is not cached; model download is not automatic"
                    ) from exc
            if not Path(model_path).is_file():
                raise WorkerError("configured DocLayout weight does not exist")
            try:
                doclayout = importlib.import_module("doclayout_yolo")
                self._layout = doclayout.YOLOv10(model_path)
            except Exception as exc:
                raise WorkerError("quality DocLayout model could not be loaded") from exc
        return self._layout

    def _formula_model(self):
        if self._formula is None:
            self._require_cuda()
            backend = str(
                os.environ.get("BW_READER_PC_FORMULA_BACKEND") or "unimernet-base"
            ).strip().lower()
            if backend == "unimernet-base":
                entrypoint = str(
                    os.environ.get("BW_READER_PC_UNIMERNET_ADAPTER") or ""
                ).strip()
                if ":" not in entrypoint:
                    raise WorkerError(
                        "formula-model-unavailable: configure a UniMERNet base adapter"
                    )
                module_name, factory_name = entrypoint.rsplit(":", 1)
                try:
                    module = importlib.import_module(module_name)
                    factory = getattr(module, factory_name)
                    self._formula = factory(model_name="unimernet-base", device="cuda")
                except Exception as exc:
                    self._formula = None
                    raise WorkerError("formula-model-unavailable: UniMERNet base") from exc
                self._assert_model_cuda(self._formula, "UniMERNet base")
                self._formula_backend = "unimernet-base-local"
            elif backend == "pix2tex":
                # Compatibility is intentionally opt-in.  Absence/failure of
                # UniMERNet must never cause an automatic downgrade.
                try:
                    pix2tex = importlib.import_module("pix2tex.cli")
                    self._formula = pix2tex.LatexOCR()
                except Exception as exc:
                    self._formula = None
                    raise WorkerError("formula-model-unavailable: pix2tex") from exc
                device = str(
                    getattr(getattr(self._formula, "args", None), "device", "unknown")
                ).lower()
                if "cuda" not in device:
                    self._formula = None
                    raise WorkerError(
                        "pix2tex did not select CUDA; CPU fallback is disabled"
                    )
                self._formula_backend = "pix2tex-quality-local"
            else:
                raise WorkerError("formula-model-unavailable: unknown formula backend")
        return self._formula

    def formulas(
        self,
        claim: Claim,
        checkpoint: Callable[[str, int | None, dict], None],
        total_pages: int,
    ) -> tuple[dict, str, str | None, int, int]:
        if self.document is None:
            raise WorkerError("quality pipeline is not open")
        pillow = importlib.import_module("PIL.Image")
        try:
            layout = self._layout_model()
        except WorkerError:
            return (
                {
                    "schema": FORMULA_SCHEMA,
                    "bookId": claim.book_id,
                    "contentSha256": claim.content_sha256,
                    "formulas": [],
                },
                "unavailable",
                "formula-detector-unavailable",
                0,
                0,
            )
        torch = self._require_cuda()
        formulas: list[dict] = []
        for page_number in range(1, total_pages + 1):
            checkpoint(
                "formula-detect",
                page_number,
                _progress(total_pages, text=total_pages, words=total_pages, detected=len(formulas)),
            )
            page = self.document[page_number - 1]
            width, height = float(page.rect.width), float(page.rect.height)
            scale = 1600.0 / max(width, height)
            fitz = importlib.import_module("fitz")
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            try:
                image = pillow.frombytes("RGB", (pix.width, pix.height), pix.samples)
                result = layout.predict(
                    image, imgsz=1600, conf=0.12, device=0, verbose=False
                )[0]
                names = getattr(result, "names", {}) or {}
                for bbox, class_no, confidence in zip(
                    result.boxes.xyxy.tolist(),
                    result.boxes.cls.tolist(),
                    result.boxes.conf.tolist(),
                ):
                    label = names.get(int(class_no), str(int(class_no))) if isinstance(names, dict) else str(int(class_no))
                    if label != "isolate_formula" and int(class_no) != 8:
                        continue
                    normalized = [
                        max(0.0, min(1.0, float(bbox[0]) / pix.width)),
                        max(0.0, min(1.0, float(bbox[1]) / pix.height)),
                        max(0.0, min(1.0, float(bbox[2]) / pix.width)),
                        max(0.0, min(1.0, float(bbox[3]) / pix.height)),
                    ]
                    if normalized[0] < normalized[2] and normalized[1] < normalized[3]:
                        formulas.append(
                            {
                                "page": page_number,
                                "bbox": [round(value, 6) for value in normalized],
                                "conf": round(float(confidence), 6),
                                "latex": None,
                            }
                        )
            finally:
                del pix
        recognized = 0
        failed = 0
        if formulas:
            try:
                formula_model = self._formula_model()
            except WorkerError:
                return (
                    {
                        "schema": FORMULA_SCHEMA,
                        "bookId": claim.book_id,
                        "contentSha256": claim.content_sha256,
                        "formulas": formulas,
                    },
                    "unavailable",
                    "formula-model-unavailable",
                    len(formulas),
                    0,
                )
            for index, formula in enumerate(formulas):
                checkpoint(
                    "formula-latex",
                    int(formula["page"]),
                    _progress(
                        total_pages,
                        text=total_pages,
                        words=total_pages,
                        detected=len(formulas),
                        recognized=recognized,
                    ),
                )
                page = self.document[int(formula["page"]) - 1]
                x0, y0, x1, y1 = formula["bbox"]
                width, height = float(page.rect.width), float(page.rect.height)
                pad_x = max(4.0, (x1 - x0) * width * 0.02)
                pad_y = max(4.0, (y1 - y0) * height * 0.08)
                fitz = importlib.import_module("fitz")
                clip = fitz.Rect(
                    max(0.0, x0 * width - pad_x),
                    max(0.0, y0 * height - pad_y),
                    min(width, x1 * width + pad_x),
                    min(height, y1 * height + pad_y),
                )
                pix = page.get_pixmap(matrix=fitz.Matrix(7.0, 7.0), clip=clip, alpha=False)
                try:
                    image = pillow.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    latex = str(formula_model(image) or "").strip()
                except Exception:
                    latex = ""
                finally:
                    del pix
                if latex:
                    formula["latex"] = latex
                    formula["latexEngine"] = self._formula_backend
                    recognized += 1
                else:
                    failed += 1
        state = "succeeded" if failed == 0 else "partial"
        reason = None if failed == 0 else FORMULA_RECOGNITION_FAILED
        payload = {
            "schema": FORMULA_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "formulas": formulas,
        }
        del torch
        return payload, state, reason, len(formulas), recognized

    def _release_cuda(self) -> None:
        gc.collect()
        try:
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        except Exception:
            pass

    def close(self) -> None:
        if self.document is not None:
            self.document.close()
            self.document = None
        formula = self._formula
        if formula is not None and callable(getattr(formula, "close", None)):
            try:
                formula.close()
            except Exception:
                pass
        self._manga = None
        self._layout = None
        self._formula = None
        self._formula_backend = None
        self._release_cuda()


class LeaseMonitor:
    def __init__(self, api: PiWorkerApi, claim: Claim):
        self.api = api
        self.claim = claim
        self.phase = "preparing"
        self.current_page = None
        self.progress = _progress(claim.total_pages)
        self.desired = "running"
        self.error: BaseException | None = None
        self.lease_expires_ms = claim.lease_expires_ms
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.poll_now()
        interval = max(1.0, min(10.0, self.claim.renew_after_ms / 2000.0))

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.poll_now()
                except BaseException as exc:
                    with self._lock:
                        self.error = exc
                    return

        self._thread = threading.Thread(target=loop, name="reader-pc-ocr-lease", daemon=True)
        self._thread.start()

    def update(self, phase: str, current_page: int | None, progress: dict) -> None:
        with self._lock:
            self.phase = phase
            self.current_page = current_page
            self.progress = dict(progress)

    def accept(self, response: dict) -> None:
        desired = str(response.get("desiredState") or "running").lower()
        if desired in ("pause", "pause-requested"):
            desired = "paused"
        if desired in ("cancel", "cancel-requested"):
            desired = "cancelled"
        if desired not in ("running", "paused", "cancelled"):
            raise WorkerError("Pi returned an invalid desired state")
        lease = response.get("lease") if isinstance(response.get("lease"), dict) else {}
        expires = int(lease.get("expiresAtEpochMs") or 0)
        with self._lock:
            self.desired = desired
            if expires > 0:
                self.lease_expires_ms = expires

    def poll_now(self) -> None:
        with self._lock:
            phase, page, progress = self.phase, self.current_page, dict(self.progress)
        self.accept(
            self.api.heartbeat(
                self.claim,
                phase=phase,
                current_page=page,
                progress=progress,
            )
        )

    def checkpoint(self) -> None:
        with self._lock:
            error, desired, expires = (
                self.error,
                self.desired,
                self.lease_expires_ms,
            )
        if error is not None:
            raise WorkerError("lease heartbeat failed; refusing further uploads") from error
        if expires > 0 and _now_ms() >= expires:
            raise WorkerError("worker lease expired; refusing further uploads")
        if desired != "running":
            raise LeaseStopped(desired)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class WorkerRunner:
    def __init__(
        self,
        api: PiWorkerApi,
        cache: ContentCache,
        project_root: Path,
        engines: tuple[str, ...],
        *,
        pipeline_factory=QualityPipeline,
        monitor_factory=LeaseMonitor,
    ):
        self.api = api
        self.cache = cache
        self.project_root = project_root
        self.engines = engines
        self.pipeline_factory = pipeline_factory
        self.monitor_factory = monitor_factory

    def run_once(self) -> bool:
        claim = self.api.claim(self.engines)
        if claim is None:
            # A successful empty claim proves that the Pi API is reachable and
            # accepted this worker generation.  Do not leave an earlier 502 or
            # profile-negotiation error visible forever while the queue is
            # healthy but empty.
            self.cache.status(
                state="idle",
                phase="preparing",
                currentPage=None,
                jobId=None,
                error=None,
            )
            return False
        monitor = self.monitor_factory(self.api, claim)
        pipeline = self.pipeline_factory(self.project_root)
        total = claim.total_pages
        text_done = len(claim.completed_pages)
        word_done = text_done
        detected = recognized = 0
        monitor.start()
        self.cache.status(
            state="running",
            phase="preparing",
            workerId=claim.worker_id,
            jobId=claim.job_id,
            bookId=claim.book_id,
            contentSha256=claim.content_sha256,
            engine=claim.engine,
            error=None,
        )
        try:
            monitor.update("downloading", None, _progress(total, text=text_done, words=word_done))
            self.cache.status(state="running", phase="downloading", currentPage=None)
            monitor.checkpoint()
            pdf = self.cache.download(self.api, claim, monitor.checkpoint)
            monitor.checkpoint()
            actual_total = int(pipeline.open(pdf))
            if actual_total > claim.max_pages:
                raise WorkerError("PDF page count exceeds the negotiated limit")
            if claim.total_pages not in (0, actual_total):
                raise WorkerError("Pi page count does not match the immutable PDF")
            total = actual_total
            completed = set(claim.completed_pages)
            # native 引擎不识别,只读字符层:阶段名要说实话,否则 ReaderPC 界面显示「文字识别」。
            page_phase = "text-layer" if claim.engine == "native" else "text-ocr"
            for page_number in range(1, total + 1):
                if page_number in completed:
                    continue
                progress = _progress(total, text=text_done, words=word_done)
                monitor.update(page_phase, page_number, progress)
                self.cache.status(
                    state="running",
                    phase=page_phase,
                    currentPage=page_number,
                    progress=progress,
                )
                monitor.poll_now()
                monitor.checkpoint()
                page = self.cache.cached_page(claim, page_number)
                if page is None:
                    page = pipeline.page(claim, page_number)
                    self.cache.save_page(claim, page_number, page)
                # Manga tokenization occurs inside page() before this first PUT.
                text_done += 1
                word_done += 1
                progress = _progress(total, text=text_done, words=word_done)
                monitor.update("uploading", page_number, progress)
                monitor.accept(self.api.put_page(claim, page_number, page, progress))
                monitor.checkpoint()
            monitor.update(
                "tokenizing", None, _progress(total, text=text_done, words=word_done)
            )
            self.cache.status(
                state="running",
                phase="tokenizing",
                currentPage=None,
                progress=_progress(total, text=text_done, words=word_done),
            )
            monitor.poll_now()
            monitor.checkpoint()
            pipeline.release_text_model()

            def formula_checkpoint(phase: str, page: int | None, progress: dict) -> None:
                monitor.update(phase, page, progress)
                self.cache.status(
                    state="running", phase=phase, currentPage=page, progress=progress
                )
                monitor.checkpoint()

            formula_checkpoint(
                "formula-detect",
                None,
                _progress(total, text=text_done, words=word_done),
            )
            formula, formula_state, formula_reason, detected, recognized = pipeline.formulas(
                claim, formula_checkpoint, total
            )
            progress = _progress(
                total,
                text=text_done,
                words=word_done,
                detected=detected,
                recognized=recognized,
            )
            monitor.update("uploading", None, progress)
            monitor.accept(
                self.api.put_formulas(
                    claim, formula, formula_state, formula_reason, progress
                )
            )
            self.cache.status(
                state="running",
                phase="uploading",
                currentPage=None,
                progress=progress,
                formulaState=formula_state,
                formulaReason=formula_reason,
            )
            monitor.checkpoint()
            monitor.update("finalizing", None, progress)
            monitor.accept(self.api.complete(claim, total, progress))
            monitor.checkpoint()
            self.cache.status(
                state="idle",
                phase="finalizing",
                currentPage=None,
                progress=progress,
                lastCompletedJobId=claim.job_id,
                jobId=None,
            )
            return True
        except LeaseStopped as exc:
            self.cache.status(
                state=exc.desired_state,
                phase=monitor.phase,
                currentPage=monitor.current_page,
            )
            try:
                self.api.heartbeat(
                    claim,
                    phase=monitor.phase,
                    current_page=monitor.current_page,
                    state=exc.desired_state,
                    progress=dict(monitor.progress),
                )
            except Exception:
                # The completed pages are already durable.  If the stop ACK is
                # lost, the Pi lease expires fail-closed and can be reclaimed.
                pass
            raise
        except BaseException as exc:
            self.cache.status(
                state="failed",
                phase=monitor.phase,
                currentPage=monitor.current_page,
                error=safe_error(exc),
            )
            try:
                self.api.heartbeat(
                    claim,
                    phase=monitor.phase,
                    current_page=monitor.current_page,
                    state="failed",
                    progress=_progress(
                        total,
                        text=text_done,
                        words=word_done,
                        detected=detected,
                        recognized=recognized,
                    ),
                    error=safe_error(exc),
                )
            except Exception:
                pass
            raise
        finally:
            monitor.close()
            pipeline.close()


def _lower_process_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes

            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if not kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS):
                error = ctypes.get_last_error()
                raise OSError(error, "SetPriorityClass failed")
        else:
            os.nice(10)
    except Exception as exc:
        raise WorkerError("could not lower PC worker process priority") from exc


def _lightweight_cuda_status(
    *,
    executable: str | None = None,
    command_runner: Callable[..., Any] = subprocess.run,
) -> dict:
    """Inspect the NVIDIA adapter without importing PyTorch while idle."""

    command = executable or os.environ.get("BW_READER_PC_NVIDIA_SMI")
    command = str(command or shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi") or "")
    if not command:
        raise WorkerError("quality-first profile requires nvidia-smi for the idle GPU probe")
    try:
        completed = command_runner(
            [
                command,
                "--query-gpu=index,name,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkerError("quality-first idle GPU probe failed") from exc
    if completed.returncode != 0:
        raise WorkerError("quality-first idle GPU probe returned an error")
    try:
        rows = list(csv.reader(str(completed.stdout or "").splitlines()))
        row = rows[0]
        index = int(row[0].strip())
        name = row[1].strip()
        driver = row[2].strip()
    except (IndexError, TypeError, ValueError) as exc:
        raise WorkerError("quality-first idle GPU probe returned invalid data") from exc
    if index < 0 or not name:
        raise WorkerError("quality-first idle GPU probe found no usable adapter")
    return {
        "available": True,
        "deviceIndex": index,
        "deviceName": name,
        "driverVersion": driver,
        "cudaVersion": "validated-on-job-start",
        "probe": "nvidia-smi-idle",
    }


def _default_worker_id() -> str:
    identity = (
        f"{socket.gethostname()}\0{os.environ.get('USERNAME') or ''}\0"
        f"{os.getpid()}\0{PROCESS_INSTANCE_NONCE}"
    )
    return "pc_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _read_token(token_file: str | None) -> str:
    direct = str(os.environ.get("BW_READER_PC_OCR_TOKEN") or "").strip()
    if direct:
        return direct
    configured = token_file or os.environ.get("BW_READER_PC_OCR_TOKEN_FILE")
    path = Path(configured).expanduser() if configured else Path.home() / ".config" / "mcp-webapp-token"
    try:
        token = path.read_text("utf-8").strip()
    except OSError as exc:
        raise WorkerError(
            "set BW_READER_PC_OCR_TOKEN or BW_READER_PC_OCR_TOKEN_FILE"
        ) from exc
    if not token:
        raise WorkerError("PC worker token file is empty")
    return token


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="BWReader outbound PC OCR worker")
    parser.add_argument("--base-url", default=os.environ.get("BW_READER_PC_OCR_BASE_URL"))
    parser.add_argument("--token-file")
    parser.add_argument("--worker-id", default=os.environ.get("BW_READER_PC_OCR_WORKER_ID"))
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--cache-root", default=os.environ.get("BW_READER_PC_OCR_CACHE"))
    parser.add_argument("--engines", default=os.environ.get("BW_READER_PC_OCR_ENGINES", "vision,manga,native"))
    parser.add_argument("--idle-poll-seconds", type=float, default=20.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--recycle-after-job",
        action="store_true",
        help=(
            "exit after a claimed job so the ReaderPC supervisor can restart "
            "a lightweight process without retained model memory"
        ),
    )
    return parser.parse_args(argv)


def build_runner(args) -> WorkerRunner:
    if not args.base_url:
        raise WorkerError("set BW_READER_PC_OCR_BASE_URL to the Pi HTTPS origin")
    token = _read_token(args.token_file)
    worker_id = str(args.worker_id or _default_worker_id())
    project_root = Path(args.project_root).resolve()
    default_cache = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader" / "pc-ocr-cache"
    cache_root = Path(args.cache_root).expanduser() if args.cache_root else default_cache
    requested = tuple(
        value.strip().lower() for value in str(args.engines).split(",") if value.strip()
    )
    engines = QualityPipeline.supported_engines(requested)
    if not engines or len(engines) != len(requested):
        raise WorkerError("configured PC OCR engines must be vision and/or manga")
    cuda = _lightweight_cuda_status()
    cache = ContentCache(cache_root, project_root)
    process_identity = _current_process_identity()
    cache.status(
        state="idle",
        phase="preparing",
        workerId=worker_id,
        processId=process_identity["pid"],
        processStartFileTimeUtc=process_identity["startFileTimeUtc"],
        startedAtEpochMs=_now_ms(),
        gpu=cuda,
        formulaBackendConfigured=str(
            os.environ.get("BW_READER_PC_FORMULA_BACKEND") or "unimernet-base"
        ).strip().lower(),
        modelConfiguration={
            "docLayoutWeight": (
                "explicit-local"
                if os.environ.get("BW_READER_PC_DOCLAYOUT_MODEL")
                else "cached-only"
            ),
            "unimernetAdapterConfigured": bool(
                os.environ.get("BW_READER_PC_UNIMERNET_ADAPTER")
            ),
            "automaticWeightDownload": False,
        },
        projectRoot=str(project_root),
    )
    api = PiWorkerApi(args.base_url, token, worker_id)
    return WorkerRunner(api, cache, project_root, engines)


def main(argv=None) -> int:
    try:
        args = parse_args(argv)
        _lower_process_priority()
        runner = build_runner(args)
        failure_streak = 0
        while True:
            failed = False
            try:
                worked = runner.run_once()
            except LeaseStopped as exc:
                print(f"PC OCR job {exc.desired_state}; checkpoint preserved", flush=True)
                worked = True
            except KeyboardInterrupt:
                return 130
            except Exception as exc:
                print("PC OCR worker error: " + safe_error(exc), file=sys.stderr, flush=True)
                # 本地 err 日志留完整 traceback(含路径):这是本机文件,不上传;
                # 只打脱敏一行的话「PermissionError: <path>」永远查不出是哪个文件(2026-09-04)。
                try:
                    import traceback as _tb
                    _tb.print_exc(file=sys.stderr)
                    sys.stderr.flush()
                except Exception:
                    pass
                # ⚠ 2026-08-17 修:失败以前被记成 worked=True → recycle 模式直接
                # return 0 → 监督者 30s 后重启 → 持续性错误(Pi 证书过期/502)变成
                # "每 30 秒冷启动一个进程"的重启风暴,而下面写好的退避永远走不到。
                # recycle 的本意是"干完活释放模型显存"——失败根本没加载模型,
                # 留在进程内按指数退避重试才是便宜的路径。
                worked = False
                failed = True
            if args.once or (args.recycle_after_job and worked):
                return 0
            if failed:
                failure_streak += 1
                backoff = min(600.0, 10.0 * (2 ** min(failure_streak - 1, 6)))
                print(f"PC OCR worker backing off {backoff:.0f}s (streak {failure_streak})",
                      file=sys.stderr, flush=True)
                time.sleep(backoff)
                continue
            failure_streak = 0
            # Empty queues poll slowly so an idle worker stays cheap.
            time.sleep(max(5.0, float(args.idle_poll_seconds if not worked else 10.0)))
    except Exception as exc:
        print("PC OCR worker startup failed: " + safe_error(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
