"""Detached worker for :mod:`reader_book_ocr`.

The worker performs page-bounded OCR and writes each successful page atomically.
Pause/cancel are checkpoint requests: completed pages remain durable, while the
page in progress may be repeated after resume.  The original PDF is read-only.
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid


CONTRACT = "reader-library-ocr/1"
PAGE_SCHEMA = "reader-page-chars/1"
PUBLICATION_CONTRACT = "reader-book-ocr-publication/1"
RELEASE_INDEX_CONTRACT = "reader-book-ocr-release-index/1"
RUN_ID_RE = re.compile(r"ocrrun_[0-9a-f]{16}")
PROCESSING_PROFILES = {"pi": "pi-default-v5", "pc": "quality-first-v6"}
LEGACY_PROCESSING_PROFILES = {"pi": "pi-default-v1", "pc": "quality-first-v1"}
_EXPECTED_JOB_ID: str | None = None
_EXPECTED_WORKER_GENERATION: str | None = None


def _processing_identity(value: dict | None) -> tuple[str, str]:
    item = value if isinstance(value, dict) else {}
    executor = str(item.get("executor") or "pi")
    if executor not in PROCESSING_PROFILES:
        executor = "pi"
    profile = str(
        item.get("processingProfile") or LEGACY_PROCESSING_PROFILES[executor]
    )
    return executor, profile


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _file_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_file_snapshot(path: Path, payload: bytes | None) -> None:
    if payload is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.rollback"
    )
    try:
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _release_jobs_lock_path(version_dir: Path, *, book_id: str, content_sha256: str) -> Path:
    """Return the service lock for a real version dir, with a local test fallback."""

    del book_id  # bookId may drift while the content-addressed version dir is reused.
    if (
        version_dir.name == content_sha256
        and re.fullmatch(r"book_[0-9a-f]{32}", version_dir.parent.name)
    ):
        return version_dir.parent.parent / "jobs.lock"
    return version_dir / ".jobs.lock"


def _migrated_run_id(engine: str, revision: str) -> str:
    digest = hashlib.sha256(
        f"{engine}\x00{revision}\x00migrated".encode("utf-8")
    ).hexdigest()
    return "ocrrun_" + digest[:16]


def _load_release_index_for_publish(args, version_dir: Path) -> dict:
    """Load an existing authoritative ledger, failing closed on corruption."""

    path = version_dir / "releases-index.json"
    if not path.exists():
        return {}
    try:
        index = json.loads(path.read_text("utf-8"))
    except Exception as exc:
        raise RuntimeError("OCR release index is invalid") from exc
    stored_book_id = str(index.get("bookId") or "") if isinstance(index, dict) else ""
    allowed_book_ids = {str(args.book_id), version_dir.parent.name}
    if (
        not isinstance(index, dict)
        or index.get("contract") != RELEASE_INDEX_CONTRACT
        or not re.fullmatch(r"book_[0-9a-f]{32}", stored_book_id)
        or stored_book_id not in allowed_book_ids
        or index.get("contentSha256") != args.content_sha256
        or not isinstance(index.get("generation"), int)
        or isinstance(index.get("generation"), bool)
        or index.get("generation", -1) < 0
        or not isinstance(index.get("runs"), list)
        or (
            index.get("activeRunId") is not None
            and not RUN_ID_RE.fullmatch(str(index.get("activeRunId") or ""))
        )
    ):
        raise RuntimeError("OCR release index identity is invalid")
    seen_run_ids: set[str] = set()
    for item in index["runs"]:
        run_id = str(item.get("runId") or "") if isinstance(item, dict) else ""
        job_id = item.get("jobId") if isinstance(item, dict) else None
        engine = str(item.get("engine") or "") if isinstance(item, dict) else ""
        revision = str(item.get("revision") or "") if isinstance(item, dict) else ""
        release = (
            f"legacy/releases/{revision}" if engine == "legacy" else f"releases/{revision}"
        )
        if (
            not isinstance(item, dict)
            or not RUN_ID_RE.fullmatch(run_id)
            or run_id in seen_run_ids
            or (
                job_id is not None
                and (
                    not isinstance(job_id, str)
                    or not re.fullmatch(r"ocrjob_[A-Za-z0-9_-]{1,128}", job_id)
                )
            )
            or engine not in ("vision", "manga", "legacy")
            or not re.fullmatch(r"ocr_[0-9a-f]{20}", revision)
            or item.get("release") != release
        ):
            raise RuntimeError("OCR release index run is invalid")
        seen_run_ids.add(run_id)
    active_run_id = index.get("activeRunId")
    if active_run_id is not None and active_run_id not in seen_run_ids:
        raise RuntimeError("OCR release index active run is invalid")
    return index


def _release_index_after_publish(
    args,
    version_dir: Path,
    release_dir: Path,
    revision: str,
    run_job: dict,
    run_result: dict,
    manifest: dict,
) -> dict:
    """Upsert the one verified release without duplicating service-side scanning."""

    executor, processing_profile = _processing_identity(run_job)
    run_id = str(run_job.get("runId") or "")
    if not RUN_ID_RE.fullmatch(run_id):
        run_id = _migrated_run_id(args.engine, revision)
    published_at = run_result.get("completedAtEpochMs")
    if not isinstance(published_at, int):
        try:
            published_at = int(release_dir.stat().st_mtime * 1000)
        except OSError:
            published_at = None
    started_at = run_job.get("createdAtEpochMs")
    if not isinstance(started_at, int):
        started_at = None
    total_pages = manifest.get("totalPages")
    if not isinstance(total_pages, int):
        total_pages = None
    entry = {
        "runId": run_id,
        "jobId": str(run_job.get("jobId") or "") or None,
        "revision": revision,
        "engine": args.engine,
        "executor": executor,
        "processingProfile": processing_profile,
        "release": f"releases/{revision}",
        "startedAtEpochMs": started_at,
        "publishedAtEpochMs": published_at,
        "totalPages": total_pages,
        "origin": executor,
    }
    existing = _load_release_index_for_publish(args, version_dir)
    old_runs = existing.get("runs")
    runs = [
        dict(item)
        for item in old_runs
        if isinstance(item, dict) and item.get("runId") != run_id
    ] if isinstance(old_runs, list) else []
    runs.insert(0, entry)
    try:
        generation = int(existing.get("generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    return {
        "contract": RELEASE_INDEX_CONTRACT,
        "bookId": args.book_id,
        "contentSha256": args.content_sha256,
        "generation": generation + 1,
        "activeRunId": run_id,
        "runs": runs,
    }


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _set_worker_identity(job_id: str | None, worker_generation: str | None) -> None:
    global _EXPECTED_JOB_ID, _EXPECTED_WORKER_GENERATION
    _EXPECTED_JOB_ID = str(job_id or "") or None
    _EXPECTED_WORKER_GENERATION = str(worker_generation or "") or None


def _assert_worker_identity(job_path: Path, job: dict | None = None) -> dict:
    current = job if isinstance(job, dict) else (_load(job_path, {}) or {})
    if _EXPECTED_JOB_ID is None and _EXPECTED_WORKER_GENERATION is None:
        return current
    if (
        current.get("jobId") != _EXPECTED_JOB_ID
        or current.get("workerGeneration") != _EXPECTED_WORKER_GENERATION
    ):
        raise RuntimeError("OCR worker generation is no longer current")
    return current


def _update_job(job_path: Path, **changes) -> dict:
    job = _load(job_path, {}) or {}
    _assert_worker_identity(job_path, job)
    job.update(changes)
    job["updatedAtEpochMs"] = int(time.time() * 1000)
    _atomic_json(job_path, job)
    return job


def _progress(total=0, completed=0, failed=0, unavailable=0, pending=None) -> dict:
    total = max(0, int(total or 0))
    completed = max(0, int(completed or 0))
    failed = max(0, int(failed or 0))
    unavailable = max(0, int(unavailable or 0))
    if pending is None:
        pending = max(0, total - completed - failed - unavailable)
    return {
        "total": total,
        "completed": completed,
        "pending": max(0, int(pending or 0)),
        "failed": failed,
        "unavailable": unavailable,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(stat) -> dict:
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtimeNs": int(stat.st_mtime_ns),
    }


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_source_guard(path: Path, expected_sha256: str, max_bytes: int) -> dict:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    try:
        before = os.fstat(fd)
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise RuntimeError("PDF exceeds the configured preprocessing size limit")
        digest = _hash_fd(fd)
        after = os.fstat(fd)
        path_stat = path.stat(follow_symlinks=False)
        identity = _source_identity(after)
        if (
            _source_identity(before) != identity
            or _source_identity(path_stat) != identity
            or digest != expected_sha256
        ):
            raise RuntimeError("book content changed after the preprocessing request")
        return {
            "fd": fd,
            "path": path,
            "contentSha256": expected_sha256,
            "identity": identity,
        }
    except Exception:
        os.close(fd)
        raise


def _assert_source_guard(guard: dict, *, rehash: bool) -> None:
    fd = int(guard["fd"])
    if (
        _source_identity(os.fstat(fd)) != guard["identity"]
        or _source_identity(Path(guard["path"]).stat(follow_symlinks=False))
        != guard["identity"]
        or (rehash and _hash_fd(fd) != guard["contentSha256"])
    ):
        raise RuntimeError("book content changed before OCR publication")


def _close_source_guard(guard: dict | None) -> None:
    if guard is not None:
        try:
            os.close(int(guard["fd"]))
        except OSError:
            pass


def _verify_source_content(path: Path, expected_sha256: str, max_bytes: int) -> None:
    guard = _open_source_guard(path, expected_sha256, max_bytes)
    _close_source_guard(guard)


def _manifest_revision(manifest: dict) -> str:
    """Address every immutable manifest field, excluding its self references."""
    canonical = {
        key: value
        for key, value in manifest.items()
        if key not in ("revision", "generatedAtEpochMs")
    }
    canonical["files"] = [
        {key: value for key, value in entry.items() if key != "downloadUrl"}
        for entry in manifest.get("files") or []
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "ocr_" + hashlib.sha256(payload).hexdigest()[:20]


def _safe_error(exc: Exception, pdf: Path, project: Path) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(exc))
    for sensitive_path, label in (
        (str(pdf), "<book>"),
        (str(project), "<project>"),
        (str(Path.home()), "<home>"),
    ):
        if sensitive_path:
            message = message.replace(sensitive_path, label)
    message = re.sub(
        r"(?i)(authorization|bearer|api[-_ ]?key|access[-_ ]?token)\s*[:=]?\s*\S+",
        r"\1=<redacted>",
        message,
    )
    return f"{type(exc).__name__}: {message[:240]}"


def _desired(control_path: Path) -> str:
    value = (_load(control_path, {}) or {}).get("desiredState")
    return value if value in ("running", "paused", "cancelled") else "cancelled"


def _stop_state(job_path: Path, desired: str) -> int:
    if desired == "paused":
        _update_job(
            job_path,
            state="paused",
            message="已保存完成页；继续时当前未完成页会重做",
            canPause=False,
            canResume=True,
            canCancel=True,
            canRetry=False,
        )
        return 20
    _update_job(
        job_path,
        state="cancelled",
        message="已取消；成功页面 sidecar 已保留",
        canPause=False,
        canResume=False,
        canCancel=False,
        canRetry=True,
    )
    return 21


def _page_done(
    path: Path,
    book_id: str,
    content_sha256: str,
    engine: str,
    processing_profile: str,
) -> bool:
    value = _load(path, {}) or {}
    return (
        value.get("schema") == PAGE_SCHEMA
        and value.get("bookId") == book_id
        and value.get("contentSha256") == content_sha256
        and value.get("engine") == engine
        and value.get("processingProfile") == processing_profile
        and isinstance(value.get("chars"), list)
    )


# 送给 Google Vision 的那张图的分辨率策略。
#
# 2026-08-19 用户实测:某本 1684x2405pt 的超大跨页扫描书,文字层"高度太高,选下面
# 会连带选上面"。量出来的实据 —— 同一套代码、三份结果:
#   595x890pt  -> 3308x4946px = 400dpi -> 框高/行距 0.51, 行重叠 0/28
#   515x731pt  -> 2145x3045px = 300dpi -> 框高/行距 0.64, 行重叠 2/38
#   1684x2405pt-> 2802x4000px = **120dpi** -> 框高/行距 **1.34**, 行重叠 **31/56**
# 根因不是引擎也不是执行者,是**固定的长边像素封顶**:页面越大,封顶把有效 DPI 压得
# 越低,而 Vision 在低分辨率上出的 symbol 框会粗到吃掉行距。
#
# 所以封顶的对象改成它真正要防的东西 —— **上传字节数**(Vision 请求体上限 ~10MB,
# base64 膨胀 4/3),而不是像素数;分辨率只受"目标 DPI"和"保底 DPI"约束。
# 字节不靠估:渲染完实测,超了按面积比一次算准再渲,最多三轮。
#
# ⚠ 若某页连保底 DPI 都塞不进字节预算(极端巨幅),这里**不静默降级** —— 会把实际
#   有效 DPI 写进页 sidecar 的 visionEffectiveDpi,低于保底时另记 visionDpiShortfall。
#   真要解决那种页面得**切片 OCR**(分块 300dpi 各送一次再把框映回页面坐标),
#   那是明确的下一步,不是这次的范围。
VISION_TARGET_DPI = 300.0
VISION_MIN_DPI = 200.0
VISION_MAX_UPLOAD_BYTES = 6_000_000
VISION_ABSOLUTE_MAX_LONG_EDGE = 12000.0
# 再怎么压也不往下走的地板。到了这里还超预算就**照发** —— 预算是我们保守的
# 自我约束(Vision 实际能收更多),把一页压成糊的去换"一定不超预算"是赔本的。
VISION_ABSOLUTE_MIN_DPI = 96.0
VISION_JPEG_QUALITY = 90
VISION_SHRINK_ATTEMPTS = 6


def _vision_render(page) -> tuple[bytes, int, int, float]:
    """Render one page for Vision at the highest DPI that fits the upload budget."""
    fitz = __import__("fitz")
    width_pt = float(page.rect.width) or 1.0
    long_pt = max(width_pt, float(page.rect.height)) or 1.0
    zoom = min(
        VISION_TARGET_DPI / 72.0,
        VISION_ABSOLUTE_MAX_LONG_EDGE / long_pt,
    )
    floor_zoom = VISION_MIN_DPI / 72.0
    hard_floor_zoom = VISION_ABSOLUTE_MIN_DPI / 72.0
    image = b""
    image_w = image_h = 0
    for _attempt in range(VISION_SHRINK_ATTEMPTS):
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        try:
            image = pix.tobytes("jpg", jpg_quality=VISION_JPEG_QUALITY)
            image_w, image_h = int(pix.width), int(pix.height)
        finally:
            del pix
        if len(image) <= VISION_MAX_UPLOAD_BYTES or zoom <= hard_floor_zoom:
            break
        # JPEG 字节数**大致**随像素面积走,但只是大致:大片均匀区域几乎不随分辨率
        # 变小。所以面积比只当作起点,再强制至少降 15%,保证每轮真的在推进 ——
        # 否则遇到不可压缩的页面会空转到轮次用完，看起来"试过了"其实原地踏步。
        estimate = zoom * ((VISION_MAX_UPLOAD_BYTES / len(image)) ** 0.5) * 0.98
        zoom = max(hard_floor_zoom, min(estimate, zoom * 0.85))
        if zoom < floor_zoom:
            # 已经掉到保底以下:调用方会据返回的有效 DPI 记 visionDpiShortfall,
            # 这一页不会被假装成跟别的页一样准。
            zoom = max(hard_floor_zoom, zoom)
    effective_dpi = (image_w / width_pt * 72.0) if width_pt else 0.0
    return image, image_w, image_h, effective_dpi


def _vision_page(page, project: Path) -> tuple[list[dict], str, int, int, float]:
    scripts = project / "scripts"
    sys.path.insert(0, str(scripts))
    from google_vision_ocr import _load_key, ocr_one_page  # type: ignore

    image, image_w, image_h, effective_dpi = _vision_render(page)
    raw = ocr_one_page(_load_key(), image)
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


def _manga_line_char_boxes(
    text: str,
    points: list,
    *,
    vertical: bool | None = None,
    image_gray=None,
) -> list[tuple[str, float, float, float, float]]:
    """Approximate character geometry in the detector's actual writing direction.

    MangaPageOcr exposes a four-point polygon per recognized line, not symbol
    polygons.  Preserve its authoritative writing-direction flag and split the
    actual quadrilateral rather than its axis-aligned bounding rectangle.  Each
    slice is returned as an AABB because ``reader-page-chars/1`` stores boxes,
    but the slice boundaries still follow a rotated/skewed line.  Exact symbol
    geometry from Google Vision remains untouched and stays the highest-
    fidelity option.
    """
    visible = [character for character in text if not character.isspace()]
    try:
        valid_points = [
            (float(point[0]), float(point[1]))
            for point in points
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
    except (TypeError, ValueError, OverflowError):
        return []
    if not visible or len(valid_points) != 4:
        return []
    if not all(math.isfinite(value) for point in valid_points for value in point):
        return []
    xs = [point[0] for point in valid_points]
    ys = [point[1] for point in valid_points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    width = max(0.0, x1 - x0)
    height = max(0.0, y1 - y0)
    if width <= 0 or height <= 0:
        return []
    is_vertical = bool(vertical) if isinstance(vertical, bool) else height > width * 1.2

    # MangaPageOcr only exposes one polygon for a whole line.  Equal division
    # is a safe fallback, but it accumulates error as soon as the line contains
    # narrow punctuation or the recognizer omits one glyph.  When the rendered
    # page is available, rectify the polygon and recover the actual ink runs;
    # the monotonic alignment below retains the manga block/line identity while
    # giving the Reader the same kind of tight character geometry as Vision.
    if image_gray is not None:
        try:
            optical = _manga_optical_char_boxes(
                visible,
                valid_points,
                image_gray,
                vertical=is_vertical,
            )
        except Exception:
            optical = []
        if len(optical) == len(visible):
            return optical

    def between(start: tuple[float, float], end: tuple[float, float], ratio: float):
        return (
            start[0] + (end[0] - start[0]) * ratio,
            start[1] + (end[1] - start[1]) * ratio,
        )

    top_left, top_right, bottom_right, bottom_left = valid_points
    cells: list[tuple[str, float, float, float, float]] = []
    for offset, character in enumerate(visible):
        start_ratio = offset / len(visible)
        end_ratio = (offset + 1) / len(visible)
        if is_vertical:
            slice_points = (
                between(top_left, bottom_left, start_ratio),
                between(top_right, bottom_right, start_ratio),
                between(top_right, bottom_right, end_ratio),
                between(top_left, bottom_left, end_ratio),
            )
        else:
            slice_points = (
                between(top_left, top_right, start_ratio),
                between(top_left, top_right, end_ratio),
                between(bottom_left, bottom_right, end_ratio),
                between(bottom_left, bottom_right, start_ratio),
            )
        slice_x = [point[0] for point in slice_points]
        slice_y = [point[1] for point in slice_points]
        cell_x0, cell_x1 = min(slice_x), max(slice_x)
        cell_y0, cell_y1 = min(slice_y), max(slice_y)
        if cell_x1 <= cell_x0 or cell_y1 <= cell_y0:
            return []
        cells.append((character, cell_x0, cell_y0, cell_x1, cell_y1))
    return cells


def _detect_ruled_table_grids(
    image_gray,
    *,
    sx: float,
    sy: float,
) -> list[dict]:
    """Find conservative ruled-table grids in a rendered page.

    MangaPageOcr intentionally owns ordinary page and manga-panel order, but a
    long horizontal text line can cross several table cells.  Only a page area
    with at least three matching horizontal rules and a long interior vertical
    separator is considered a table.  This keeps the table correction out of
    borderless prose, illustrations, and comic panels.
    """
    if image_gray is None or sx <= 0 or sy <= 0:
        return []
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        gray = np.asarray(image_gray)
        if gray.ndim != 2 or gray.shape[0] < 80 or gray.shape[1] < 80:
            return []
        # The OCR render is 300 DPI and may exceed seventy million pixels.
        # Rules survive a much smaller analysis image, so cap this one pass
        # rather than adding several full-page masks to the OCR memory peak.
        original_h, original_w = int(gray.shape[0]), int(gray.shape[1])
        analysis_scale = min(1.0, 2400.0 / max(original_h, original_w))
        if analysis_scale < 1.0:
            gray = cv2.resize(
                gray,
                (
                    max(80, int(round(original_w * analysis_scale))),
                    max(80, int(round(original_h * analysis_scale))),
                ),
                interpolation=cv2.INTER_AREA,
            )
        map_sx = sx / analysis_scale
        map_sy = sy / analysis_scale
        image_h, image_w = int(gray.shape[0]), int(gray.shape[1])
        _threshold, ink = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
        )
        horizontal = cv2.morphologyEx(
            ink,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(
                cv2.MORPH_RECT,
                (max(40, image_w // 24), 1),
            ),
        )
        contours, _hierarchy = cv2.findContours(
            horizontal,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
    except Exception:
        return []

    raw_rules: list[tuple[float, float, float]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < image_w * 0.28 or height > max(32, image_h * 0.015):
            continue
        raw_rules.append((y + height / 2.0, float(x), float(x + width)))
    if len(raw_rules) < 3:
        return []

    def overlap_ratio(
        left: tuple[float, float, float],
        right: tuple[float, float, float],
    ) -> float:
        overlap = max(0.0, min(left[2], right[2]) - max(left[1], right[1]))
        return overlap / max(1.0, min(left[2] - left[1], right[2] - right[1]))

    # Collapse the two edges of a thick/coloured rule into one y coordinate.
    # Two disjoint rules at the same y are instead evidence of parallel tables
    # or comic panels.  The single-track detector cannot safely separate those
    # layouts, so keep MangaPageOcr's authoritative regions unchanged.
    merged_rules: list[tuple[float, float, float]] = []
    merge_distance = max(3.0, image_h * 0.0015)
    for y, x0, x1 in sorted(raw_rules):
        rule = (y, x0, x1)
        if merged_rules and y - merged_rules[-1][0] <= merge_distance:
            if overlap_ratio(rule, merged_rules[-1]) < 0.72:
                return []
            old_y, old_x0, old_x1 = merged_rules[-1]
            merged_rules[-1] = (
                (old_y + y) / 2.0,
                min(old_x0, x0),
                max(old_x1, x1),
            )
        else:
            merged_rules.append(rule)

    sequences: list[list[tuple[float, float, float]]] = []
    max_rule_gap = max(45.0, image_h * 0.10)
    for rule in merged_rules:
        if (
            not sequences
            or rule[0] - sequences[-1][-1][0] > max_rule_gap
            or overlap_ratio(rule, sequences[-1][-1]) < 0.72
        ):
            sequences.append([rule])
        else:
            sequences[-1].append(rule)

    grids: list[dict] = []
    for rules in sequences:
        if len(rules) < 3 or rules[-1][0] - rules[0][0] < image_h * 0.055:
            continue
        x0_values = sorted(rule[1] for rule in rules)
        x1_values = sorted(rule[2] for rule in rules)
        table_x0 = x0_values[len(x0_values) // 2]
        table_x1 = x1_values[len(x1_values) // 2]

        # A nearby full-page border can overlap every table rule and otherwise
        # become a false final row.  Trim only a leading/trailing rule whose
        # span is materially wider than the consensus table span; ordinary
        # outer table borders remain untouched.
        median_width = max(1.0, table_x1 - table_x0)
        span_tolerance = max(8.0, image_w * 0.04)

        def is_outer_span_outlier(rule: tuple[float, float, float]) -> bool:
            return (
                rule[2] - rule[1] > median_width * 1.20
                and (
                    rule[1] < table_x0 - span_tolerance
                    or rule[2] > table_x1 + span_tolerance
                )
            )

        while len(rules) > 3 and is_outer_span_outlier(rules[0]):
            rules = rules[1:]
        while len(rules) > 3 and is_outer_span_outlier(rules[-1]):
            rules = rules[:-1]
        if len(rules) < 3 or rules[-1][0] - rules[0][0] < image_h * 0.055:
            continue
        x0_values = sorted(rule[1] for rule in rules)
        x1_values = sorted(rule[2] for rule in rules)
        table_x0 = x0_values[len(x0_values) // 2]
        table_x1 = x1_values[len(x1_values) // 2]
        table_y0 = rules[0][0]
        table_y1 = rules[-1][0]
        table_width = table_x1 - table_x0
        table_height = table_y1 - table_y0
        if table_width < image_w * 0.28 or table_height <= 0:
            continue
        try:
            vertical = cv2.morphologyEx(
                ink,
                cv2.MORPH_OPEN,
                cv2.getStructuringElement(
                    cv2.MORPH_RECT,
                    (1, max(40, int(table_height * 0.15))),
                ),
            )
            vertical_contours, _hierarchy = cv2.findContours(
                vertical,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE,
            )
        except Exception:
            continue
        separator_centers: list[float] = []
        for contour in vertical_contours:
            x, y, width, height = cv2.boundingRect(contour)
            overlap_y = max(
                0.0,
                min(float(y + height), table_y1) - max(float(y), table_y0),
            )
            center_x = x + width / 2.0
            if (
                overlap_y < table_height * 0.52
                or width > max(36, image_w * 0.018)
                or center_x <= table_x0 + table_width * 0.025
                or center_x >= table_x1 - table_width * 0.025
            ):
                continue
            separator_centers.append(center_x)
        if not separator_centers:
            continue
        merged_separators: list[float] = []
        x_merge_distance = max(4.0, image_w * 0.004)
        for center in sorted(separator_centers):
            if (
                merged_separators
                and center - merged_separators[-1] <= x_merge_distance
            ):
                merged_separators[-1] = (
                    merged_separators[-1] + center
                ) / 2.0
            else:
                merged_separators.append(center)
        x_edges = [table_x0, *merged_separators, table_x1]
        if any(
            right - left < table_width * 0.07
            for left, right in zip(x_edges, x_edges[1:])
        ):
            continue
        y_edges = [rule[0] for rule in rules]
        # Two-column ruled regions are too easily confused with adjacent manga
        # panels.  Only grids with at least three columns are table evidence.
        if len(x_edges) < 4:
            continue
        grids.append({
            "xEdges": [round(value * map_sx, 3) for value in x_edges],
            "yEdges": [round(value * map_sy, 3) for value in y_edges],
        })
    return grids


_PAGE_LAYOUT_SCHEMA = "reader-page-layout/1"
_MAX_PAGE_LAYOUT_TABLE_CELLS = 16_384


def _unavailable_page_layout() -> dict:
    """Return a fail-closed layout when Vision is not authoritative.

    Manga OCR may still provide a useful visible character layer in this case,
    but those characters are not the Vision sequence used by model-facing
    spatial projections.  Empty ranges make that boundary explicit.
    """
    return {
        "schema": _PAGE_LAYOUT_SCHEMA,
        "textSource": "unavailable",
        "layoutSource": "vision",
        "mode": "fallback",
        "readingDirection": "ltr",
        "confidence": "fallback",
        "gridColumns": 4,
        "gridRows": 0,
        "regions": [],
        "tables": [],
    }


def _layout_ranges(indices: list[int]) -> list[list[int]]:
    values = sorted(set(int(value) for value in indices))
    if not values:
        return []
    result: list[list[int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append([start, previous])
        start = previous = value
    result.append([start, previous])
    return result


def _layout_bounds(
    chars: list[dict],
    indices: list[int],
    *,
    page_w: float,
    page_h: float,
    preferred=None,
) -> list[float]:
    values = []
    for index in indices:
        try:
            item = chars[index]
            values.append((
                float(item["x0"]), float(item["y0"]),
                float(item["x1"]), float(item["y1"]),
            ))
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            continue
    if preferred is not None:
        try:
            x0, y0, x1, y1 = (float(value) for value in preferred)
            if all(math.isfinite(value) for value in (x0, y0, x1, y1)):
                return [
                    round(max(0.0, min(page_w, x0)), 3),
                    round(max(0.0, min(page_h, y0)), 3),
                    round(max(0.0, min(page_w, x1)), 3),
                    round(max(0.0, min(page_h, y1)), 3),
                ]
        except (TypeError, ValueError, OverflowError):
            pass
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(max(0.0, min(page_w, min(value[0] for value in values))), 3),
        round(max(0.0, min(page_h, min(value[1] for value in values))), 3),
        round(max(0.0, min(page_w, max(value[2] for value in values))), 3),
        round(max(0.0, min(page_h, max(value[3] for value in values))), 3),
    ]


def _layout_line_score(char: dict, line: dict) -> float | None:
    """Return a geometry-only score; Manga text never participates."""
    try:
        x0, y0, x1, y1 = (
            float(char["x0"]), float(char["y0"]),
            float(char["x1"]), float(char["y1"]),
        )
        lx0, ly0, lx1, ly1 = (float(value) for value in line["bounds"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(
        math.isfinite(value)
        for value in (x0, y0, x1, y1, lx0, ly0, lx1, ly1)
    ):
        return None
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    line_w = max(0.001, lx1 - lx0)
    line_h = max(0.001, ly1 - ly0)
    vertical = (
        bool(line["vertical"])
        if isinstance(line.get("vertical"), bool)
        else line_h > line_w * 1.2
    )
    cross = line_w if vertical else line_h
    margin = cross * 0.35
    if not (
        lx0 - margin <= cx <= lx1 + margin
        and ly0 - margin <= cy <= ly1 + margin
    ):
        return None
    overlap_x = max(0.0, min(x1, lx1) - max(x0, lx0))
    overlap_y = max(0.0, min(y1, ly1) - max(y0, ly0))
    char_area = max(0.001, (x1 - x0) * (y1 - y0))
    overlap_ratio = overlap_x * overlap_y / char_area
    if overlap_ratio < 0.08 and not (lx0 <= cx <= lx1 and ly0 <= cy <= ly1):
        return None
    center_x = (lx0 + lx1) / 2.0
    center_y = (ly0 + ly1) / 2.0
    cross_distance = abs(cx - center_x) / line_w if vertical else abs(cy - center_y) / line_h
    return overlap_ratio - cross_distance * 0.08


def _layout_row_bands(regions: list[dict], page_h: float) -> list[list[dict]]:
    if not regions:
        return []
    heights = sorted(
        max(0.001, float(item["bounds"][3]) - float(item["bounds"][1]))
        for item in regions
    )
    median_height = heights[len(heights) // 2]
    tolerance = max(page_h * 0.018, median_height * 0.38)
    bands: list[dict] = []
    for region in sorted(
        regions,
        key=lambda item: (float(item["bounds"][1]), float(item["bounds"][0])),
    ):
        top = float(region["bounds"][1])
        target = None
        for band in bands:
            if abs(top - band["top"]) <= tolerance:
                target = band
                break
        if target is None:
            bands.append({"top": top, "items": [region]})
        else:
            target["items"].append(region)
            target["top"] = sum(
                float(item["bounds"][1]) for item in target["items"]
            ) / len(target["items"])
    bands.sort(key=lambda item: item["top"])
    return [band["items"] for band in bands]


def _table_cell_layout_runs(chars: list[dict], indices: list[int]) -> list[list[int]]:
    """Return visual-line/source-wrap runs without reordering ``chars``.

    Vision source order can jump backwards inside one ruled cell.  Regions are
    therefore emitted in geometric line/x order, splitting whenever the source
    index wraps.  Non-visible entries remain in the index space and attach to
    the run owning their nearest preceding visible source index when possible.
    """
    visible_symbols = []
    hidden_indices = []
    for index in sorted(set(indices)):
        char = chars[index]
        if char.get("sp") or not str(char.get("c") or ""):
            hidden_indices.append(index)
            continue
        try:
            geometry = tuple(
                float(char[key]) for key in ("x0", "y0", "x1", "y1")
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            hidden_indices.append(index)
            continue
        if not all(math.isfinite(value) for value in geometry):
            hidden_indices.append(index)
            continue
        symbol = dict(char)
        symbol["_layoutIndex"] = index
        visible_symbols.append(symbol)

    if not visible_symbols:
        return [sorted(set(indices))] if indices else []

    runs: list[list[int]] = []
    visible_run_by_index: dict[int, list[int]] = {}
    for line in _cluster_table_cell_symbols(visible_symbols):
        current: list[int] = []
        previous = -1
        for symbol in line:
            index = int(symbol["_layoutIndex"])
            if current and index != previous + 1:
                runs.append(current)
                visible_run_by_index.update((value, current) for value in current)
                current = []
            current.append(index)
            previous = index
        if current:
            runs.append(current)
            visible_run_by_index.update((value, current) for value in current)

    visible_indices = sorted(visible_run_by_index)
    for index in hidden_indices:
        insertion = bisect.bisect_left(visible_indices, index)
        if insertion:
            owner = visible_indices[insertion - 1]
        elif insertion < len(visible_indices):
            owner = visible_indices[insertion]
        else:
            preferred = runs[0]
            if index == preferred[0] - 1 or index == preferred[-1] + 1:
                preferred.append(index)
            else:
                runs.insert(0, [index])
            continue
        preferred = visible_run_by_index[owner]
        if index == preferred[0] - 1 or index == preferred[-1] + 1:
            preferred.append(index)
            continue
        adjacent = next((
            run
            for run in runs
            if index == min(run) - 1 or index == max(run) + 1
        ), None)
        if adjacent is not None:
            adjacent.append(index)
            continue
        position = next(
            run_index
            for run_index, run in enumerate(runs)
            if run is preferred
        )
        runs.insert(position + (1 if index > owner else 0), [index])
    return [sorted(run) for run in runs]


def _vision_page_layout(
    chars: list[dict],
    *,
    page_w: float,
    page_h: float,
) -> dict:
    """Describe ordinary Vision order without changing the character array."""
    if not chars:
        return _unavailable_page_layout()
    groups: dict[tuple[int, int], list[int]] = {}
    for index, char in enumerate(chars):
        key = (int(char.get("bk", -1)), int(char.get("line", -1)))
        groups.setdefault(key, []).append(index)
    grouped_indices = list(groups.values())
    if len(grouped_indices) > 4096:
        grouped_indices = [
            *grouped_indices[:4095],
            [index for group in grouped_indices[4095:] for index in group],
        ]
    regions = []
    for order, indices in enumerate(grouped_indices):
        regions.append({
            "id": order,
            "kind": "vision-block",
            "order": order,
            "bounds": _layout_bounds(
                chars, indices, page_w=page_w, page_h=page_h
            ),
            "ranges": _layout_ranges(indices),
            "gridRow": order,
            "gridColumn": 0,
            "rowSpan": 1,
            "columnSpan": 1,
            "vertical": False,
            "tableId": None,
            "row": None,
            "column": None,
        })
    return {
        "schema": _PAGE_LAYOUT_SCHEMA,
        "textSource": "vision",
        "layoutSource": "vision",
        "mode": "vision",
        "readingDirection": "ltr",
        "confidence": "high" if chars else "low",
        "gridColumns": 1,
        "gridRows": max(1, len(regions)),
        "regions": regions,
        "tables": [],
    }


def _manga_page_layout(
    chars: list[dict],
    manga_lines: list[dict],
    grids: list[dict],
    *,
    page_w: float,
    page_h: float,
) -> dict:
    """Project authoritative Vision indices through Manga/table geometry.

    Every range indexes ``chars`` exactly as sent to the App.  Table ownership
    wins over Manga geometry; any remaining symbol that cannot be assigned by
    geometry becomes an explicit Vision supplement instead of disappearing or
    being replaced by Manga OCR text.
    """
    assigned: list[tuple[str, object] | None] = [None] * len(chars)
    tables: list[dict] = []
    region_drafts: list[dict] = []
    table_cell_count = 0

    for raw_grid in sorted(
        grids or [], key=lambda item: (item.get("yEdges") or [0])[0]
    )[:64]:
        try:
            x_edges = [float(value) for value in raw_grid.get("xEdges") or []]
            y_edges = [float(value) for value in raw_grid.get("yEdges") or []]
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            len(x_edges) < 4
            or len(y_edges) < 2
            or len(x_edges) - 1 > 4096
            or len(y_edges) - 1 > 4096
            or any(not math.isfinite(value) for value in (*x_edges, *y_edges))
            or any(right <= left for left, right in zip(x_edges, x_edges[1:]))
            or any(bottom <= top for top, bottom in zip(y_edges, y_edges[1:]))
        ):
            continue
        grid_cell_count = (len(x_edges) - 1) * (len(y_edges) - 1)
        if table_cell_count + grid_cell_count > _MAX_PAGE_LAYOUT_TABLE_CELLS:
            continue
        cells: dict[tuple[int, int], list[int]] = {}
        for index, char in enumerate(chars):
            if assigned[index] is not None:
                continue
            try:
                cx = (float(char["x0"]) + float(char["x1"])) / 2.0
                cy = (float(char["y0"]) + float(char["y1"])) / 2.0
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            column = next((
                value
                for value, (left, right) in enumerate(zip(x_edges, x_edges[1:]))
                if left <= cx <= right
            ), None)
            row = next((
                value
                for value, (top, bottom) in enumerate(zip(y_edges, y_edges[1:]))
                if top <= cy <= bottom
            ), None)
            if row is not None and column is not None:
                cells.setdefault((row, column), []).append(index)
        visible_cells = {
            cell: indices
            for cell, indices in cells.items()
            if any(not chars[index].get("sp") for index in indices)
        }
        if (
            len({row for row, _column in visible_cells}) < 2
            or len({column for _row, column in visible_cells}) < 2
        ):
            continue
        table_cell_count += grid_cell_count
        table_id = len(tables)
        table = {
            "id": table_id,
            "rows": len(y_edges) - 1,
            "columns": len(x_edges) - 1,
            "xEdges": [round(value, 3) for value in x_edges],
            "yEdges": [round(value, 3) for value in y_edges],
        }
        tables.append(table)
        for (row, column), indices in sorted(cells.items()):
            owned = [index for index in indices if assigned[index] is None]
            if not owned:
                continue
            key = ("table", table_id, row, column)
            for index in owned:
                assigned[index] = ("table", key)
            for run in _table_cell_layout_runs(chars, owned):
                region_drafts.append({
                    "kind": "table-cell",
                    "bounds": _layout_bounds(
                        chars,
                        run,
                        page_w=page_w,
                        page_h=page_h,
                    ),
                    "indices": run,
                    "vertical": False,
                    "tableId": table_id,
                    "row": row,
                    "column": column,
                })

    manga_groups: dict[int, dict] = {}
    for line in manga_lines or []:
        try:
            block = int(line.get("bk", -1))
            bounds = tuple(float(value) for value in line["bounds"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
            continue
        group = manga_groups.setdefault(block, {"lines": [], "bounds": [], "vertical": []})
        group["lines"].append(line)
        group["bounds"].append(bounds)
        if isinstance(line.get("vertical"), bool):
            group["vertical"].append(bool(line["vertical"]))

    manga_indices: dict[int, list[int]] = {}
    for index, char in enumerate(chars):
        if assigned[index] is not None:
            continue
        best: tuple[float, int] | None = None
        for block, group in manga_groups.items():
            for line in group["lines"]:
                score = _layout_line_score(char, line)
                if score is not None and (best is None or score > best[0]):
                    best = (score, block)
        if best is None:
            continue
        block = best[1]
        assigned[index] = ("manga", block)
        manga_indices.setdefault(block, []).append(index)

    for block, indices in manga_indices.items():
        group = manga_groups[block]
        group_bounds = group["bounds"]
        preferred = (
            min(value[0] for value in group_bounds),
            min(value[1] for value in group_bounds),
            max(value[2] for value in group_bounds),
            max(value[3] for value in group_bounds),
        )
        vertical_flags = group["vertical"]
        region_drafts.append({
            "kind": "manga-region",
            "bounds": _layout_bounds(
                chars, indices, page_w=page_w, page_h=page_h,
                preferred=preferred,
            ),
            "indices": indices,
            "vertical": (
                sum(1 for value in vertical_flags if value)
                > len(vertical_flags) / 2.0
                if vertical_flags else False
            ),
            "tableId": None,
            "row": None,
            "column": None,
        })

    supplements: dict[tuple[int, int], list[int]] = {}
    for index, char in enumerate(chars):
        if assigned[index] is not None:
            continue
        key = (int(char.get("bk", -1)), int(char.get("line", -1)))
        supplements.setdefault(key, []).append(index)
        assigned[index] = ("supplement", key)
    for indices in supplements.values():
        region_drafts.append({
            "kind": "vision-supplement",
            "bounds": _layout_bounds(
                chars, indices, page_w=page_w, page_h=page_h
            ),
            "indices": indices,
            "vertical": False,
            "tableId": None,
            "row": None,
            "column": None,
        })

    if len(region_drafts) > 4096:
        return _vision_page_layout(chars, page_w=page_w, page_h=page_h)

    if not manga_indices and not tables:
        return _vision_page_layout(chars, page_w=page_w, page_h=page_h)

    non_table = [
        draft for draft in region_drafts if draft["kind"] != "table-cell"
    ]
    bands = _layout_row_bands(non_table, page_h)
    table_rows = max((int(table["rows"]) for table in tables), default=0)
    grid_rows = max(1, len(bands), table_rows)
    grid_columns = max(
        4 if manga_indices else 1,
        min(8, max((int(table["columns"]) for table in tables), default=1)),
    )
    band_for = {
        id(region): row
        for row, items in enumerate(bands)
        for region in items
    }
    for draft in region_drafts:
        if draft["kind"] == "table-cell":
            columns = int(tables[int(draft["tableId"])]["columns"])
            draft["gridRow"] = min(grid_rows - 1, int(draft["row"]))
            draft["gridColumn"] = min(
                grid_columns - 1,
                int(int(draft["column"]) * grid_columns / max(1, columns)),
            )
        else:
            center_x = (float(draft["bounds"][0]) + float(draft["bounds"][2])) / 2.0
            draft["gridRow"] = min(grid_rows - 1, band_for.get(id(draft), 0))
            draft["gridColumn"] = min(
                grid_columns - 1,
                max(0, int(center_x * grid_columns / max(0.001, page_w))),
            )

    vertical_weight = sum(
        len(draft["indices"])
        for draft in region_drafts
        if draft["kind"] == "manga-region" and draft["vertical"]
    )
    horizontal_weight = sum(
        len(draft["indices"])
        for draft in region_drafts
        if draft["kind"] == "manga-region" and not draft["vertical"]
    )
    reading_direction = "rtl" if vertical_weight > horizontal_weight else "ltr"

    table_by_id = {int(table["id"]): table for table in tables}
    table_cells: dict[int, list[dict]] = {}
    ordinary = []
    for draft in region_drafts:
        if draft["kind"] == "table-cell":
            table_cells.setdefault(int(draft["tableId"]), []).append(draft)
        else:
            ordinary.append(draft)
    band_tops = {
        row: min(float(item["bounds"][1]) for item in items)
        for row, items in enumerate(bands)
    }
    ordered_units = [
        (
            band_tops.get(int(draft["gridRow"]), float(draft["bounds"][1])),
            1,
            -int(draft["gridColumn"])
            if reading_direction == "rtl" else int(draft["gridColumn"]),
            -float(draft["bounds"][0])
            if reading_direction == "rtl" else float(draft["bounds"][0]),
            draft,
        )
        for draft in ordinary
    ] + [
        (float(table["yEdges"][0]), 0, 0, 0.0, ("table", table_id))
        for table_id, table in table_by_id.items()
    ]
    ordered_units.sort(key=lambda item: item[:4])
    ordered_drafts: list[dict] = []
    for _top, _kind, _column, _left, value in ordered_units:
        if isinstance(value, tuple):
            table_id = int(value[1])
            ordered_drafts.extend(sorted(
                table_cells.get(table_id, []),
                key=lambda item: (int(item["row"]), int(item["column"])),
            ))
        else:
            ordered_drafts.append(value)
    if not tables:
        ordered_drafts.sort(key=lambda item: (
            int(item["gridRow"]),
            -int(item["gridColumn"])
            if reading_direction == "rtl" else int(item["gridColumn"]),
            float(item["bounds"][1]),
            -float(item["bounds"][0])
            if reading_direction == "rtl" else float(item["bounds"][0]),
        ))

    regions = []
    for order, draft in enumerate(ordered_drafts):
        regions.append({
            "id": order,
            "kind": draft["kind"],
            "order": order,
            "bounds": [round(float(value), 3) for value in draft["bounds"]],
            "ranges": _layout_ranges(draft["indices"]),
            "gridRow": int(draft["gridRow"]),
            "gridColumn": int(draft["gridColumn"]),
            "rowSpan": 1,
            "columnSpan": 1,
            "vertical": bool(draft["vertical"]),
            "tableId": draft["tableId"],
            "row": draft["row"],
            "column": draft["column"],
        })

    covered = sorted(
        index
        for region in regions
        for start, end in region["ranges"]
        for index in range(start, end + 1)
    )
    if covered != list(range(len(chars))):
        raise RuntimeError("page layout did not conserve Vision character indices")
    visible_indices = {
        index
        for index, char in enumerate(chars)
        if not char.get("sp") and str(char.get("c") or "")
    }
    structured_indices = {
        index
        for draft in region_drafts
        if draft["kind"] in {"table-cell", "manga-region"}
        for index in draft["indices"]
        if index in visible_indices
    }
    structured_coverage = (
        len(structured_indices) / len(visible_indices)
        if visible_indices else 0.0
    )
    confidence_threshold = 0.25 if tables else 0.75
    confidence = "high" if structured_coverage >= confidence_threshold else "low"
    return {
        "schema": _PAGE_LAYOUT_SCHEMA,
        "textSource": "vision",
        "layoutSource": "ruled-table" if tables else "manga",
        "mode": "table" if tables else "manga",
        "readingDirection": reading_direction,
        "confidence": confidence,
        "gridColumns": grid_columns,
        "gridRows": grid_rows,
        "regions": regions,
        "tables": tables,
    }


def _manga_vision_text_is_complete(manga_text: str, vision_text: str) -> bool:
    """Accept complete Vision text plus the established one-glyph corrections."""
    matcher = difflib.SequenceMatcher(
        None, manga_text, vision_text, autojunk=False
    )
    if len(manga_text) == len(vision_text):
        if matcher.ratio() < 0.75:
            return False
        return all(
            tag in ("equal", "replace")
            for tag, _i1, _i2, _j1, _j2 in matcher.get_opcodes()
        )
    opcodes = matcher.get_opcodes()
    if len(vision_text) == len(manga_text) + 1:
        edits = [opcode for opcode in opcodes if opcode[0] != "equal"]
        return (
            len(edits) == 1
            and edits[0][0] == "insert"
            and edits[0][1] == edits[0][2]
            and edits[0][4] - edits[0][3] == 1
        )
    if len(manga_text) != len(vision_text) + 1:
        return False
    edits = [opcode for opcode in opcodes if opcode[0] != "equal"]
    if (
        len(edits) != 1
        or edits[0][0] != "delete"
        or edits[0][2] - edits[0][1] != 1
        or edits[0][3] != edits[0][4]
    ):
        return False
    deleted_at = edits[0][1]
    deleted = manga_text[deleted_at]
    return (
        (deleted_at > 0 and manga_text[deleted_at - 1] == deleted)
        or (
            deleted_at + 1 < len(manga_text)
            and manga_text[deleted_at + 1] == deleted
        )
    )


def _cluster_table_cell_symbols(symbols: list[dict]) -> list[list[dict]]:
    """Cluster exact Vision symbols into horizontal lines inside one cell."""
    lines: list[dict] = []
    ordered = sorted(
        symbols,
        key=lambda item: (
            (float(item["y0"]) + float(item["y1"])) / 2.0,
            float(item["x0"]),
        ),
    )
    for symbol in ordered:
        y0, y1 = float(symbol["y0"]), float(symbol["y1"])
        center_y = (y0 + y1) / 2.0
        height = max(0.001, y1 - y0)
        best: tuple[float, int] | None = None
        for index, line in enumerate(lines):
            overlap = max(0.0, min(y1, line["y1"]) - max(y0, line["y0"]))
            overlap_ratio = overlap / max(0.001, min(height, line["height"]))
            center_distance = abs(center_y - line["center"])
            if overlap_ratio < 0.42 and center_distance > max(height, line["height"]) * 0.55:
                continue
            score = overlap_ratio - center_distance / max(height, line["height"]) * 0.08
            if best is None or score > best[0]:
                best = (score, index)
        if best is None:
            lines.append({
                "items": [symbol],
                "y0": y0,
                "y1": y1,
                "center": center_y,
                "height": height,
            })
            continue
        line = lines[best[1]]
        line["items"].append(symbol)
        line["y0"] = min(line["y0"], y0)
        line["y1"] = max(line["y1"], y1)
        line["center"] = (line["y0"] + line["y1"]) / 2.0
        line["height"] = max(0.001, line["y1"] - line["y0"])
    lines.sort(key=lambda line: (line["y0"], line["center"]))
    result: list[list[dict]] = []
    for line in lines:
        line["items"].sort(key=lambda item: (float(item["x0"]), float(item["y0"])))
        result.append(line["items"])
    return result


def _table_cell_vision_is_complete(
    manga_symbols: list[dict],
    vision_symbols: list[dict],
) -> bool:
    """Prove that Vision did not omit ordinary Manga text in one cell.

    Text agreement handles the established equal-length and one-glyph OCR
    corrections.  Page OCR can also reorder or over-segment a few symbols even
    when both engines cover the same ink, so otherwise require good text
    similarity and spatial coverage for every Manga glyph.  One geometrically
    uncovered duplicate is allowed only when the other copy is itself backed
    by an overlapping same-character Vision symbol.
    """
    if not manga_symbols:
        return True
    if not vision_symbols:
        return False
    ordered_vision = [
        symbol
        for line in _cluster_table_cell_symbols(vision_symbols)
        for symbol in line
    ]
    manga_text = "".join(str(symbol.get("c") or "") for symbol in manga_symbols)
    vision_text = "".join(str(symbol.get("c") or "") for symbol in ordered_vision)
    if not manga_text or not vision_text:
        return False
    if _manga_vision_text_is_complete(manga_text, vision_text):
        return True

    matcher = difflib.SequenceMatcher(
        None, manga_text, vision_text, autojunk=False
    )
    short_one_substitution = (
        len(manga_text) == len(vision_text)
        and 2 <= len(manga_text) <= 3
        and sum(
            1
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1
        ) == 1
        and all(
            tag in ("equal", "replace")
            for tag, _i1, _i2, _j1, _j2 in matcher.get_opcodes()
        )
    )
    if matcher.ratio() < 0.72 and not short_one_substitution:
        return False

    def overlap_ratio(source: dict, candidate: dict) -> float:
        overlap_x = max(
            0.0,
            min(float(source["x1"]), float(candidate["x1"]))
            - max(float(source["x0"]), float(candidate["x0"])),
        )
        overlap_y = max(
            0.0,
            min(float(source["y1"]), float(candidate["y1"]))
            - max(float(source["y0"]), float(candidate["y0"])),
        )
        source_area = max(
            0.001,
            (float(source["x1"]) - float(source["x0"]))
            * (float(source["y1"]) - float(source["y0"])),
        )
        candidate_area = max(
            0.001,
            (float(candidate["x1"]) - float(candidate["x0"]))
            * (float(candidate["y1"]) - float(candidate["y0"])),
        )
        return overlap_x * overlap_y / min(source_area, candidate_area)

    covered = [
        any(overlap_ratio(source, candidate) >= 0.20 for candidate in ordered_vision)
        for source in manga_symbols
    ]
    uncovered = [index for index, value in enumerate(covered) if not value]
    if not uncovered:
        return True
    if len(uncovered) != 1:
        return False

    missing_index = uncovered[0]
    missing_character = str(manga_symbols[missing_index].get("c") or "")
    for neighbor_index in range(
        max(0, missing_index - 2),
        min(len(manga_symbols), missing_index + 3),
    ):
        if neighbor_index == missing_index or not covered[neighbor_index]:
            continue
        neighbor = manga_symbols[neighbor_index]
        if str(neighbor.get("c") or "") != missing_character:
            continue
        if any(
            str(candidate.get("c") or "") == missing_character
            and overlap_ratio(neighbor, candidate) >= 0.20
            for candidate in ordered_vision
        ):
            return True
    return False


def _manga_table_cell_lines(
    lines: list[dict],
    vision_chars: list[dict] | None,
    grids: list[dict],
    *,
    sx: float = 1.0,
    sy: float = 1.0,
) -> list[dict]:
    """Replace proven ruled-table regions with cell-scoped Vision lines.

    The grid supplies ownership and row/column order; Vision supplies the exact
    symbol boxes.  Every cell gets a distinct block identity so selecting text
    in one cell cannot pull in a visually adjacent row or column.
    """
    if not lines or not vision_chars or not grids:
        return lines
    max_block = max((int(line.get("bk", -1)) for line in lines), default=-1)
    max_line = max((int(line.get("line", -1)) for line in lines), default=-1)
    next_block = max_block + 1
    next_line = max_line + 1
    consumed_all: set[int] = set()
    generated_by_position: dict[int, list[dict]] = {}

    for grid in sorted(grids, key=lambda item: item["yEdges"][0]):
        x_edges = [float(value) for value in grid.get("xEdges") or []]
        y_edges = [float(value) for value in grid.get("yEdges") or []]
        if len(x_edges) < 4 or len(y_edges) < 3:
            continue
        consumed: list[int] = []
        for index, line in enumerate(lines):
            if index in consumed_all:
                continue
            try:
                x0, y0, x1, y1 = (float(value) for value in line["bounds"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            center_y = (y0 + y1) / 2.0
            overlap_x = max(0.0, min(x1, x_edges[-1]) - max(x0, x_edges[0]))
            if (
                y_edges[0] < center_y < y_edges[-1]
                and overlap_x >= min(x1 - x0, x_edges[-1] - x_edges[0]) * 0.45
            ):
                consumed.append(index)
        if not consumed:
            continue
        if any(lines[index].get("vertical") is True for index in consumed):
            continue

        cell_symbols: dict[tuple[int, int], list[dict]] = {}
        for source in vision_chars:
            character = unicodedata.normalize("NFKC", str(source.get("c") or ""))
            if source.get("sp") or not character.strip():
                continue
            try:
                x0 = float(source["x0"])
                y0 = float(source["y0"])
                x1 = float(source["x1"])
                y1 = float(source["y1"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if x1 <= x0 or y1 <= y0:
                continue
            center_x = (x0 + x1) / 2.0
            center_y = (y0 + y1) / 2.0
            column = next((
                index for index, (left, right) in enumerate(zip(x_edges, x_edges[1:]))
                if left < center_x < right
            ), None)
            row = next((
                index for index, (top, bottom) in enumerate(zip(y_edges, y_edges[1:]))
                if top < center_y < bottom
            ), None)
            if row is None or column is None:
                continue
            cell_symbols.setdefault((row, column), []).append({
                "c": character,
                "x0": round(x0, 3),
                "y0": round(y0, 3),
                "x1": round(x1, 3),
                "y1": round(y1, 3),
                "w": int(source.get("w", -1)),
                "b": int(source.get("b", 0)),
            })

        manga_count = sum(
            1
            for index in consumed
            for character in unicodedata.normalize(
                "NFKC", str(lines[index].get("text") or "")
            )
            if not character.isspace()
        )
        vision_count = sum(len(items) for items in cell_symbols.values())
        populated_rows = {row for row, _column in cell_symbols}
        populated_columns = {column for _row, column in cell_symbols}
        manga_cell_symbols: dict[tuple[int, int], list[dict]] = {}
        if sx > 0 and sy > 0:
            for index in consumed:
                for cell in lines[index].get("cells") or []:
                    try:
                        character, x0, y0, x1, y1 = cell
                        if not unicodedata.normalize(
                            "NFKC", str(character or "")
                        ).strip():
                            continue
                        page_x0 = float(x0) * sx
                        page_y0 = float(y0) * sy
                        page_x1 = float(x1) * sx
                        page_y1 = float(y1) * sy
                        center_x = (page_x0 + page_x1) / 2.0
                        center_y = (page_y0 + page_y1) / 2.0
                    except (TypeError, ValueError, OverflowError):
                        continue
                    column = next((
                        cell_index
                        for cell_index, (left, right) in enumerate(
                            zip(x_edges, x_edges[1:])
                        )
                        if left < center_x < right
                    ), None)
                    row = next((
                        cell_index
                        for cell_index, (top, bottom) in enumerate(
                            zip(y_edges, y_edges[1:])
                        )
                        if top < center_y < bottom
                    ), None)
                    if row is not None and column is not None:
                        manga_cell_symbols.setdefault((row, column), []).append({
                            "c": unicodedata.normalize("NFKC", str(character)),
                            "x0": page_x0,
                            "y0": page_y0,
                            "x1": page_x1,
                            "y1": page_y1,
                        })
        if (
            vision_count < max(6, int(manga_count * 0.68))
            or vision_count > max(10, int(manga_count * 1.45))
            or len(populated_rows) < 2
            or len(populated_columns) < 2
            # A partial or whole-cell Vision omission must not delete Manga
            # text.  Every Manga-populated cell must independently prove that
            # Vision contains its ordinary characters.
            or any(
                not _table_cell_vision_is_complete(
                    manga_symbols,
                    cell_symbols.get(cell, []),
                )
                for cell, manga_symbols in manga_cell_symbols.items()
            )
        ):
            continue

        generated: list[dict] = []
        for row in range(len(y_edges) - 1):
            for column in range(len(x_edges) - 1):
                symbols = cell_symbols.get((row, column))
                if not symbols:
                    continue
                cell_block = next_block
                next_block += 1
                for clustered in _cluster_table_cell_symbols(symbols):
                    table_chars = []
                    for source in clustered:
                        value = dict(source)
                        value["bk"] = cell_block
                        value["line"] = next_line
                        value["vertical"] = False
                        table_chars.append(value)
                    text = "".join(item["c"] for item in table_chars)
                    generated.append({
                        "text": text,
                        "cells": [],
                        "bounds": (
                            min(item["x0"] for item in table_chars),
                            min(item["y0"] for item in table_chars),
                            max(item["x1"] for item in table_chars),
                            max(item["y1"] for item in table_chars),
                        ),
                        "polygon": None,
                        "bk": cell_block,
                        "line": next_line,
                        "vertical": False,
                        "vision_chars": table_chars,
                    })
                    next_line += 1
        if not generated:
            continue
        start = min(consumed)
        # Manga block order can put the first table row before its immediately
        # preceding heading.  Move only past nearby, horizontally overlapping
        # lines above the top rule; do not globally y-sort manga panels.
        first_row_height = max(1.0, y_edges[1] - y_edges[0])
        heading_gap = max(6.0, first_row_height * 1.5)
        for index, line in enumerate(lines):
            if index in consumed_all or index in consumed:
                continue
            try:
                x0, _y0, x1, y1 = (
                    float(value) for value in line["bounds"]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            overlap_x = max(
                0.0,
                min(x1, x_edges[-1]) - max(x0, x_edges[0]),
            )
            if (
                y1 <= y_edges[0] + 1.0
                and y_edges[0] - y1 <= heading_gap
                and overlap_x >= (
                    min(x1 - x0, x_edges[-1] - x_edges[0]) * 0.45
                )
            ):
                start = max(start, index + 1)
        generated_by_position.setdefault(start, []).extend(generated)
        consumed_all.update(consumed)

    if not consumed_all:
        return lines
    result: list[dict] = []
    for index in range(len(lines) + 1):
        result.extend(generated_by_position.get(index, []))
        if index < len(lines) and index not in consumed_all:
            result.append(lines[index])
    return result


def _proven_table_layout_grids(
    lines: list[dict],
    vision_chars: list[dict] | None,
    grids: list[dict],
    *,
    sx: float,
    sy: float,
) -> list[dict]:
    """Return only grids that pass the established Manga/Vision proof gate."""
    if not lines or not vision_chars or not grids:
        return []
    candidates = []
    cell_count = 0
    for grid in sorted(grids, key=lambda item: (item.get("yEdges") or [0])[0]):
        x_edges = list(grid.get("xEdges") or [])
        y_edges = list(grid.get("yEdges") or [])
        if len(x_edges) < 4 or len(y_edges) < 2:
            continue
        grid_cells = (len(x_edges) - 1) * (len(y_edges) - 1)
        if cell_count + grid_cells > _MAX_PAGE_LAYOUT_TABLE_CELLS:
            continue
        candidates.append(grid)
        cell_count += grid_cells
    if not candidates:
        return []
    proven_lines = _manga_table_cell_lines(
        lines, vision_chars, candidates, sx=sx, sy=sy
    )
    generated = [line for line in proven_lines if line.get("vision_chars")]
    accepted = []
    for grid in candidates:
        x_edges = [float(value) for value in grid.get("xEdges") or []]
        y_edges = [float(value) for value in grid.get("yEdges") or []]
        cells = set()
        for line in generated:
            try:
                x0, y0, x1, y1 = (float(value) for value in line["bounds"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            center_x, center_y = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            column = next((
                index
                for index, (left, right) in enumerate(zip(x_edges, x_edges[1:]))
                if left <= center_x <= right
            ), None)
            row = next((
                index
                for index, (top, bottom) in enumerate(zip(y_edges, y_edges[1:]))
                if top <= center_y <= bottom
            ), None)
            if row is not None and column is not None:
                cells.add((row, column))
        if (
            len({row for row, _column in cells}) >= 2
            and len({column for _row, column in cells}) >= 2
        ):
            accepted.append(grid)
    return accepted


def _manga_character_ink_factor(character: str) -> float:
    """Expected ink width relative to an ordinary CJK glyph.

    This is used only to align already-observed ink runs.  It does not invent
    geometry: the returned box still comes from pixels in the rectified line.
    """
    if character in "、，,":
        return 0.08
    if character in "。．.":
        return 0.32
    category = unicodedata.category(character)
    if category in {"Pi", "Pf", "Ps", "Pe"}:
        return 0.25
    if category.startswith("P"):
        return 0.20
    if character.isascii():
        return 0.62
    return 1.0


def _manga_align_visual_segments(
    text: list[str],
    segments: list[tuple[float, float, float, float]],
) -> list[tuple[str, float, float, float, float]]:
    """Monotonically align OCR characters to one or more observed ink runs.

    A glyph such as ``い`` may be two visual runs, while manga OCR may omit a
    narrow closing quote entirely.  The dynamic program therefore permits a
    character to consume up to three adjacent runs and permits a bounded number
    of unclaimed runs.  It never reorders characters or crosses a manga line.
    """
    if not text or not segments:
        return []
    count = len(text)
    observed = len(segments)
    if observed < max(1, count // 2) or observed > count + max(8, count // 3):
        return []
    widths = [max(0.0, item[1] - item[0]) for item in segments]
    if not widths or any(not math.isfinite(value) or value <= 0 for value in widths):
        return []
    ordered = sorted(widths)
    raw_median = ordered[len(ordered) // 2]
    substantial = sorted(value for value in widths if value >= raw_median * 0.55)
    base = substantial[len(substantial) // 2] if substantial else raw_median
    if not math.isfinite(base) or base <= 0:
        return []

    infinity = float("inf")
    costs = [[infinity] * (observed + 1) for _ in range(count + 1)]
    previous = [[None] * (observed + 1) for _ in range(count + 1)]
    costs[0][0] = 0.0
    for char_index in range(count + 1):
        for segment_index in range(observed + 1):
            current = costs[char_index][segment_index]
            if not math.isfinite(current):
                continue
            if segment_index < observed:
                # Skipping a narrow run is cheap enough to represent a glyph
                # omitted by OCR; skipping a full CJK glyph remains expensive.
                skip_cost = 0.12 + 0.55 * min(2.0, widths[segment_index] / base)
                if current + skip_cost < costs[char_index][segment_index + 1]:
                    costs[char_index][segment_index + 1] = current + skip_cost
                    previous[char_index][segment_index + 1] = (
                        char_index,
                        segment_index,
                        None,
                    )
            if char_index >= count:
                continue
            for consumed in range(1, min(3, observed - segment_index) + 1):
                first = segments[segment_index]
                last = segments[segment_index + consumed - 1]
                span = last[1] - first[0]
                target = base * _manga_character_ink_factor(text[char_index])
                if span <= 0 or target <= 0:
                    continue
                ratio = max(0.04, span / target)
                assignment_cost = 3.2 * math.log(ratio) ** 2 + 0.08 * (consumed - 1)
                if consumed > 1:
                    assignment_cost += 0.15 * sum(
                        max(0.0, segments[index + 1][0] - segments[index][1])
                        for index in range(segment_index, segment_index + consumed - 1)
                    ) / base
                destination = current + assignment_cost
                if destination < costs[char_index + 1][segment_index + consumed]:
                    costs[char_index + 1][segment_index + consumed] = destination
                    previous[char_index + 1][segment_index + consumed] = (
                        char_index,
                        segment_index,
                        consumed,
                    )

    final_cost = costs[count][observed]
    if (
        not math.isfinite(final_cost)
        or final_cost / max(1, count) > 2.25
    ):
        return []
    operations = []
    char_index, segment_index = count, observed
    while char_index or segment_index:
        step = previous[char_index][segment_index]
        if step is None:
            return []
        operations.append((char_index, segment_index, step))
        char_index, segment_index = step[0], step[1]
    operations.reverse()

    aligned = []
    skipped = 0
    consecutive_skipped = 0
    maximum_consecutive_skipped = 0
    output_index = 0
    for _next_char, next_segment, step in operations:
        consumed = step[2]
        if consumed is None:
            skipped += 1
            consecutive_skipped += 1
            maximum_consecutive_skipped = max(
                maximum_consecutive_skipped,
                consecutive_skipped,
            )
            continue
        consecutive_skipped = 0
        start_segment = step[1]
        end_segment = next_segment
        members = segments[start_segment:end_segment]
        span = members[-1][1] - members[0][0]
        target = base * _manga_character_ink_factor(text[output_index])
        ratio = span / target if target > 0 else float("inf")
        if not math.isfinite(ratio) or ratio < 0.12 or ratio > 5.0:
            return []
        aligned.append((
            text[output_index],
            members[0][0],
            members[-1][1],
            min(item[2] for item in members),
            max(item[3] for item in members),
        ))
        output_index += 1
    if (
        len(aligned) != count
        or skipped > max(3, math.ceil(count * 0.15))
        or maximum_consecutive_skipped > 2
    ):
        return []
    return aligned


def _manga_optical_char_boxes(
    text: list[str],
    points: list[tuple[float, float]],
    image_gray,
    *,
    vertical: bool,
) -> list[tuple[str, float, float, float, float]]:
    """Rectify one manga line, align ink runs, and map boxes to page pixels."""
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    source = np.asarray(points, dtype=np.float32)
    if source.shape != (4, 2) or getattr(image_gray, "ndim", 0) != 2:
        return []
    top, right, bottom, left = (
        float(np.linalg.norm(source[1] - source[0])),
        float(np.linalg.norm(source[2] - source[1])),
        float(np.linalg.norm(source[2] - source[3])),
        float(np.linalg.norm(source[3] - source[0])),
    )
    rect_width = max(2, int(round(max(top, bottom))))
    rect_height = max(2, int(round(max(left, right))))
    destination = np.asarray([
        [0, 0],
        [rect_width, 0],
        [rect_width, rect_height],
        [0, rect_height],
    ], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(source, destination)
    inverse = cv2.getPerspectiveTransform(destination, source)
    rectified = cv2.warpPerspective(
        image_gray,
        transform,
        (rect_width, rect_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    if vertical:
        working = rectified.T
    else:
        working = rectified
    _threshold, binary = cv2.threshold(
        working,
        0,
        255,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )
    ink = binary > 0
    cross_extent, main_extent = ink.shape
    if cross_extent < 2 or main_extent < 2:
        return []
    minimum_column_ink = max(1, int(round(cross_extent * 0.02)))
    occupied = np.asarray(ink.sum(axis=0) >= minimum_column_ink, dtype=np.bool_)
    runs = []
    start = None
    for index, present in enumerate(occupied.tolist() + [False]):
        if present and start is None:
            start = index
        elif not present and start is not None:
            if index - start >= max(1, int(round(cross_extent * 0.01))):
                runs.append((start, index))
            start = None
    if not runs:
        return []
    merged = []
    maximum_internal_gap = cross_extent * 0.05
    for start, end in runs:
        if merged and start - merged[-1][1] < maximum_internal_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    run_widths = sorted(end - start for start, end in merged)
    median_width = run_widths[len(run_widths) // 2]
    normalized = []
    for start, end in merged:
        width = end - start
        if median_width > 0 and width > median_width * 1.55:
            pieces = max(2, int(round(width / median_width)))
            for offset in range(pieces):
                normalized.append((
                    int(round(start + width * offset / pieces)),
                    int(round(start + width * (offset + 1) / pieces)),
                ))
        else:
            normalized.append((start, end))

    segments = []
    for start, end in normalized:
        sample = ink[:, start:end]
        cross_positions = np.flatnonzero(sample.any(axis=1))
        if cross_positions.size == 0:
            continue
        segments.append((
            float(start),
            float(end),
            float(cross_positions[0]),
            float(cross_positions[-1] + 1),
        ))
    aligned = _manga_align_visual_segments(text, segments)
    if len(aligned) != len(text):
        return []

    # Each glyph keeps its own cross-axis ink band.  Using the union for the
    # whole line makes one underline, ruby mark, or omitted visual glyph inflate
    # every neighbouring selection box.  A small minimum span keeps punctuation
    # easy to hit without reintroducing that line-wide padding.  Main-axis
    # padding adds a side bearing without accumulating across the line.
    cross_positions = np.flatnonzero(ink.any(axis=1))
    if cross_positions.size == 0:
        return []
    line_cross_start = float(cross_positions[0])
    line_cross_end = float(cross_positions[-1] + 1)
    cross_padding = max(1.0, cross_extent * 0.04)
    minimum_cross_span = max(2.0, (line_cross_end - line_cross_start) * 0.45)
    main_padding = max(1.0, (line_cross_end - line_cross_start) * 0.05)
    cells = []
    for character, main_start, main_end, local_cross_start, local_cross_end in aligned:
        main_start = max(0.0, main_start - main_padding)
        main_end = min(float(main_extent), main_end + main_padding)
        cross_start = max(0.0, local_cross_start - cross_padding)
        cross_end = min(float(cross_extent), local_cross_end + cross_padding)
        if cross_end - cross_start < minimum_cross_span:
            center = (cross_start + cross_end) / 2.0
            cross_start = max(0.0, center - minimum_cross_span / 2.0)
            cross_end = min(float(cross_extent), cross_start + minimum_cross_span)
            cross_start = max(0.0, cross_end - minimum_cross_span)
        if vertical:
            rect_points = np.asarray([[[
                cross_start, main_start,
            ], [
                cross_end, main_start,
            ], [
                cross_end, main_end,
            ], [
                cross_start, main_end,
            ]]], dtype=np.float32)
        else:
            rect_points = np.asarray([[[
                main_start, cross_start,
            ], [
                main_end, cross_start,
            ], [
                main_end, cross_end,
            ], [
                main_start, cross_end,
            ]]], dtype=np.float32)
        mapped = cv2.perspectiveTransform(rect_points, inverse)[0]
        xs = mapped[:, 0]
        ys = mapped[:, 1]
        cell_x0, cell_x1 = float(xs.min()), float(xs.max())
        cell_y0, cell_y1 = float(ys.min()), float(ys.max())
        if cell_x1 <= cell_x0 or cell_y1 <= cell_y0:
            return []
        cells.append((character, cell_x0, cell_y0, cell_x1, cell_y1))
    return cells


def _manga_vision_line_chars(
    lines: list[dict],
    vision_chars: list[dict] | None,
) -> dict[int, list[dict]]:
    """Put Vision symbols inside MangaPageOcr's authoritative line regions.

    MangaPageOcr is materially better at separating manga panels and preserving
    their reading order, while Vision exposes tighter symbol boxes and is less
    prone to recognizer insertions that shift every following glyph.  Vision is
    therefore allowed to replace *only* the contents of a Manga line.  It never
    creates, merges, reorders, or moves a Manga region.

    A symbol is assigned to at most one line by cross-axis overlap.  Lines with
    implausible symbol counts or weak text agreement keep their existing Manga
    geometry, which also makes a missing/offline Vision service a safe fallback.
    """
    if not lines or not vision_chars:
        return {}

    def polygon_contains_point(value, x: float, y: float) -> bool | None:
        """Return None for unusable polygons so old AABB inputs still work."""
        try:
            points = [
                (float(point[0]), float(point[1]))
                for point in (value or [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
        except (TypeError, ValueError, OverflowError):
            return None
        if len(points) != 4 or not all(
            math.isfinite(number) for point in points for number in point
        ):
            return None
        inside = False
        previous_x, previous_y = points[-1]
        for current_x, current_y in points:
            edge_x = current_x - previous_x
            edge_y = current_y - previous_y
            cross = edge_x * (y - previous_y) - edge_y * (x - previous_x)
            tolerance = 1e-7 * max(1.0, abs(edge_x), abs(edge_y))
            if (
                abs(cross) <= tolerance
                and min(previous_x, current_x) - tolerance
                <= x
                <= max(previous_x, current_x) + tolerance
                and min(previous_y, current_y) - tolerance
                <= y
                <= max(previous_y, current_y) + tolerance
            ):
                return True
            crosses_ray = (current_y > y) != (previous_y > y)
            if crosses_ray:
                crossing_x = (
                    (previous_x - current_x)
                    * (y - current_y)
                    / (previous_y - current_y)
                    + current_x
                )
                if x < crossing_x:
                    inside = not inside
            previous_x, previous_y = current_x, current_y
        return inside

    assigned: dict[int, list[dict]] = {}
    for source in vision_chars:
        character = unicodedata.normalize("NFKC", str(source.get("c") or ""))
        if source.get("sp") or not character.strip():
            continue
        try:
            x0 = float(source["x0"])
            y0 = float(source["y0"])
            x1 = float(source["x1"])
            y1 = float(source["y1"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        center_x = (x0 + x1) / 2.0
        center_y = (y0 + y1) / 2.0
        best: tuple[float, int] | None = None
        for line_index, line in enumerate(lines):
            line_x0, line_y0, line_x1, line_y1 = line["bounds"]
            polygon_match = polygon_contains_point(
                line.get("polygon"), center_x, center_y
            )
            if polygon_match is False:
                continue
            vertical = (
                bool(line["vertical"])
                if isinstance(line.get("vertical"), bool)
                else (line_y1 - line_y0) > (line_x1 - line_x0) * 1.2
            )
            if vertical:
                glyph_cross = x1 - x0
                line_cross = max(0.001, line_x1 - line_x0)
                overlap = max(0.0, min(x1, line_x1) - max(x0, line_x0))
                main_inside = (
                    line_y0 - line_cross * 0.30
                    <= center_y
                    <= line_y1 + line_cross * 0.30
                )
                cross_distance = abs(center_x - (line_x0 + line_x1) / 2.0)
            else:
                glyph_cross = y1 - y0
                line_cross = max(0.001, line_y1 - line_y0)
                overlap = max(0.0, min(y1, line_y1) - max(y0, line_y0))
                main_inside = (
                    line_x0 - line_cross * 0.30
                    <= center_x
                    <= line_x1 + line_cross * 0.30
                )
                cross_distance = abs(center_y - (line_y0 + line_y1) / 2.0)
            overlap_ratio = overlap / max(0.001, glyph_cross)
            if overlap_ratio < 0.35 or not main_inside:
                continue
            score = overlap_ratio - 0.12 * cross_distance / line_cross
            if best is None or score > best[0]:
                best = (score, line_index)
        if best is None:
            continue
        candidate = {
            "c": character,
            "x0": round(x0, 3),
            "y0": round(y0, 3),
            "x1": round(x1, 3),
            "y1": round(y1, 3),
            "w": -1,
            "b": 0,
        }
        assigned.setdefault(best[1], []).append(candidate)

    accepted: dict[int, list[dict]] = {}
    for line_index, candidates in assigned.items():
        line = lines[line_index]
        line_x0, line_y0, line_x1, line_y1 = line["bounds"]
        vertical = (
            bool(line["vertical"])
            if isinstance(line.get("vertical"), bool)
            else (line_y1 - line_y0) > (line_x1 - line_x0) * 1.2
        )
        candidates.sort(
            key=(
                (lambda item: (item["y0"], item["x0"]))
                if vertical
                else (lambda item: (item["x0"], item["y0"]))
            )
        )
        manga_text = "".join(
            character
            for character in unicodedata.normalize(
                "NFKC", str(line.get("text") or "")
            )
            if not character.isspace()
        )
        vision_text = "".join(item["c"] for item in candidates)
        if not manga_text or not vision_text:
            continue
        if not _manga_vision_text_is_complete(manga_text, vision_text):
            continue
        for item in candidates:
            item["bk"] = int(line["bk"])
            item["line"] = int(line["line"])
            if isinstance(line.get("vertical"), bool):
                item["vertical"] = bool(line["vertical"])
        accepted[line_index] = candidates
    return accepted


def _manga_page(
    page,
    engine,
    *,
    vision_chars: list[dict] | None = None,
    include_layout: bool = False,
):
    pix = page.get_pixmap(dpi=300, alpha=False)
    image_w, image_h = pix.width, pix.height
    temp_name = None
    image_gray = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_name = handle.name
        pix.save(temp_name)
        raw = engine(temp_name) or {}
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore

            channels = int(getattr(pix, "n", 0) or 0)
            samples = np.frombuffer(pix.samples, dtype=np.uint8)
            if channels > 0 and samples.size == image_w * image_h * channels:
                image = samples.reshape(image_h, image_w, channels)
                if channels >= 3:
                    image_gray = cv2.cvtColor(image[:, :, :3], cv2.COLOR_RGB2GRAY)
                else:
                    image_gray = image[:, :, 0].copy()
        except Exception:
            # The line geometry has a deterministic polygon fallback.  Missing
            # image/vision dependencies must not make manga OCR unusable.
            image_gray = None
    finally:
        del pix
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    sx = float(page.rect.width) / image_w
    sy = float(page.rect.height) / image_h
    chars = []
    text_lines = []
    prepared_lines = []
    line_no = 0
    for block_no, block in enumerate(raw.get("blocks") or []):
        lines = block.get("lines") or []
        coords = block.get("lines_coords") or []
        for index, value in enumerate(lines):
            if index >= len(coords):
                continue
            text = unicodedata.normalize("NFKC", str(value or ""))
            points = coords[index] or []
            if not text.strip() or not points:
                continue
            block_vertical = block.get("vertical")
            cells = _manga_line_char_boxes(
                text,
                points,
                vertical=(block_vertical if isinstance(block_vertical, bool) else None),
                image_gray=image_gray,
            )
            if not cells:
                continue
            try:
                page_points = [
                    (float(point[0]) * sx, float(point[1]) * sy)
                    for point in points
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
            except (TypeError, ValueError, OverflowError):
                page_points = []
            if len(page_points) == 4:
                point_x = [point[0] for point in page_points]
                point_y = [point[1] for point in page_points]
                bounds = (
                    min(point_x), min(point_y), max(point_x), max(point_y)
                )
            else:
                bounds = (
                    min(cell[1] * sx for cell in cells),
                    min(cell[2] * sy for cell in cells),
                    max(cell[3] * sx for cell in cells),
                    max(cell[4] * sy for cell in cells),
                )
            prepared_lines.append({
                "text": text,
                "cells": cells,
                "bounds": bounds,
                "polygon": page_points if len(page_points) == 4 else None,
                "bk": block_no,
                "line": line_no,
                "vertical": (
                    block_vertical if isinstance(block_vertical, bool) else None
                ),
            })
            text_lines.append(text)
            line_no += 1

    grids = _detect_ruled_table_grids(image_gray, sx=sx, sy=sy)
    if vision_chars is not None:
        # Vision remains the sole authority for characters, geometry, and
        # source order.  Manga/table inference is metadata only; it must never
        # replace, omit, or reorder a symbol in the visible layer.
        proven_grids = _proven_table_layout_grids(
            prepared_lines,
            vision_chars,
            grids,
            sx=sx,
            sy=sy,
        )
        chars = [dict(item) for item in vision_chars]
        layout = _manga_page_layout(
            chars,
            prepared_lines,
            proven_grids,
            page_w=float(page.rect.width),
            page_h=float(page.rect.height),
        )
        result = (
            chars,
            "".join(str(item.get("c") or "") for item in chars),
            image_w,
            image_h,
        )
        return (*result, layout) if include_layout else result

    prepared_lines = _manga_table_cell_lines(
        prepared_lines,
        None,
        grids,
        sx=sx,
        sy=sy,
    )
    text_lines = [str(prepared.get("text") or "") for prepared in prepared_lines]
    vision_lines = _manga_vision_line_chars(prepared_lines, vision_chars)
    for prepared_index, prepared in enumerate(prepared_lines):
        replacement = prepared.get("vision_chars") or vision_lines.get(prepared_index)
        if replacement:
            chars.extend(replacement)
            text_lines[prepared_index] = "".join(item["c"] for item in replacement)
            continue
        block_no = int(prepared["bk"])
        line_no = int(prepared["line"])
        block_vertical = prepared.get("vertical")
        for character, x0, y0, x1, y1 in prepared["cells"]:
            char = {
                "c": character,
                "x0": round(x0 * sx, 3),
                "y0": round(y0 * sy, 3),
                "x1": round(x1 * sx, 3),
                "y1": round(y1 * sy, 3),
                "w": -1,
                "bk": block_no,
                "line": line_no,
                "b": 0,
            }
            # Preserve MangaPageOcr's authoritative writing direction.  A
            # square block can contain several vertical lines, so the whole
            # block aspect ratio is not a reliable substitute downstream.
            if isinstance(block_vertical, bool):
                char["vertical"] = block_vertical
            chars.append(char)
    result = (chars, "\n".join(text_lines), image_w, image_h)
    return (*result, _unavailable_page_layout()) if include_layout else result


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


_TOKENIZE_SCHEMA = 2   # 2 = 以块为边界分词(2026-09-02);1/缺失 = 旧的按行分词


def _tokenize_groups(chars: list[dict], layout: dict | None) -> list[list[int]]:
    """分词的单位是**块**,不是行(用户 2026-09-02:"预处理时需要以块为边界,并在考虑到
    块内换行的前提下进行分词")。

    块的来源两层:① 版面里的表格单元格(layout.regions kind=table-cell,同一格的所有行
    归一组 —— 跨行词 コチュジャ|ン 从此在分词器眼里是连着的);② 其余字符按 (bk,line)
    的行,再按几何聚成段落:相邻两行竖直间距 ≤ 0.9 行高且横向重叠 ≥ 50% 视为同段。
    竖排字符(vertical)不做段落聚合,仍按 (bk,line) 一行一组。
    返回每组的字符下标列表,组内已按阅读顺序(行的 y、行内原序)排好。"""
    n = len(chars)
    assigned = [False] * n
    groups: list[list[int]] = []

    def _line_key(char: dict) -> tuple[int, int]:
        return (int(char.get("bk", -1)), int(char.get("line", -1)))

    def _order_lines(indices: list[int]) -> list[int]:
        lines: dict[tuple[int, int], list[int]] = {}
        for index in indices:
            lines.setdefault(_line_key(chars[index]), []).append(index)

        def _line_pos(items: list[int]) -> tuple[float, float]:
            ys = [float(chars[i].get("y0", 0.0)) for i in items]
            xs = [float(chars[i].get("x0", 0.0)) for i in items]
            return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)

        ordered: list[int] = []
        for items in sorted(lines.values(), key=_line_pos):
            ordered.extend(sorted(items))
        return ordered

    regions = layout.get("regions") if isinstance(layout, dict) else None
    cells: dict[tuple[int, int, int], list[int]] = {}
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, dict) or region.get("kind") != "table-cell":
                continue
            try:
                key = (int(region.get("tableId")), int(region.get("row")), int(region.get("column")))
            except (TypeError, ValueError):
                continue
            for rng in region.get("ranges") or []:
                try:
                    a, b = int(rng[0]), int(rng[1])
                except (TypeError, ValueError, IndexError):
                    continue
                for index in range(max(0, min(a, b)), min(n - 1, max(a, b)) + 1):
                    if not assigned[index]:
                        assigned[index] = True
                        cells.setdefault(key, []).append(index)
    for key in sorted(cells):
        groups.append(_order_lines(cells[key]))

    by_line: dict[tuple[int, int], list[int]] = {}
    for index, char in enumerate(chars):
        if assigned[index]:
            continue
        by_line.setdefault(_line_key(char), []).append(index)
    line_items = []
    for indices in by_line.values():
        boxes = [chars[i] for i in indices if chars[i].get("x0") is not None]
        if not boxes:
            line_items.append({"indices": indices, "x0": 0.0, "x1": 0.0, "y0": 0.0, "y1": 0.0, "vertical": False})
            continue
        vertical = any(bool(chars[i].get("vertical")) for i in indices)
        line_items.append({
            "indices": indices,
            "x0": min(float(b.get("x0", 0.0)) for b in boxes),
            "x1": max(float(b.get("x1", 0.0)) for b in boxes),
            "y0": min(float(b.get("y0", 0.0)) for b in boxes),
            "y1": max(float(b.get("y1", 0.0)) for b in boxes),
            "vertical": vertical,
        })
    line_items.sort(key=lambda item: (item["y0"], item["x0"]))
    paragraphs: list[dict] = []
    for item in line_items:
        if item["vertical"]:
            groups.append(sorted(item["indices"]))
            continue
        line_h = max(1.0, item["y1"] - item["y0"])
        merged = False
        for para in paragraphs:
            gap = item["y0"] - para["y1"]
            overlap = min(item["x1"], para["x1"]) - max(item["x0"], para["x0"])
            width = max(1.0, min(item["x1"] - item["x0"], para["x1"] - para["x0"]))
            if -0.3 * line_h <= gap <= 0.9 * line_h and overlap / width >= 0.5:
                para["indices"].extend(item["indices"])
                para["x0"] = min(para["x0"], item["x0"])
                para["x1"] = max(para["x1"], item["x1"])
                para["y1"] = max(para["y1"], item["y1"])
                merged = True
                break
        if not merged:
            paragraphs.append({"indices": list(item["indices"]), "x0": item["x0"], "x1": item["x1"],
                               "y0": item["y0"], "y1": item["y1"]})
    for para in paragraphs:
        groups.append(_order_lines(para["indices"]))
    return groups


def _tokenize_chars(chars: list[dict], layout: dict | None = None) -> list[dict]:
    """Assign real tokenizer boundaries; never invent words on mismatch.

    以块为单位喂分词器(见 _tokenize_groups):同一块内所有行的文字连成一串,行尾不再是
    词边界;字符上的 line 字段保留,下游据此处理换行。"""
    tagger = None
    for group_no, indices in enumerate(_tokenize_groups(chars, layout)):
        group = [chars[i] for i in indices]
        text_chars = [char for char in group if not char.get("sp") and char.get("c")]
        text = "".join(char["c"] for char in text_chars)
        if not text:
            continue
        if _KANA_RE.search(text):
            if tagger is None:
                try:
                    from fugashi import Tagger
                    tagger = Tagger()
                except Exception as exc:
                    raise RuntimeError("fugashi is required to tokenize Japanese manga OCR") from exc
            cursor = 0
            for word_no, token in enumerate(tagger(text)):
                surface = str(token.surface or "")
                if not surface or text[cursor:cursor + len(surface)] != surface:
                    raise RuntimeError("Japanese tokenization did not align with OCR characters")
                word_id = group_no * 1_000_000 + word_no
                for char in text_chars[cursor:cursor + len(surface)]:
                    char["w"] = word_id
                cursor += len(surface)
            if cursor != len(text_chars):
                raise RuntimeError("Japanese tokenization left unassigned OCR characters")
            continue
        word_no = 0
        index = 0
        while index < len(text_chars):
            current = text_chars[index]
            value = current["c"]
            if _CJK_RE.match(value):
                current["w"] = group_no * 1_000_000 + word_no
                word_no += 1
                index += 1
                continue
            if value.isascii() and value.isalnum():
                end = index + 1
                while end < len(text_chars):
                    other = text_chars[end]["c"]
                    if not (other.isascii() and other.isalnum()):
                        break
                    end += 1
                wid = group_no * 1_000_000 + word_no
                for char in text_chars[index:end]:
                    char["w"] = wid
                word_no += 1
                index = end
                continue
            current["w"] = group_no * 1_000_000 + word_no
            word_no += 1
            index += 1
    return chars


def _tokenize_directory(
    job_dir: Path,
    job_path: Path | None = None,
    control_path: Path | None = None,
) -> int:
    page_paths = sorted((job_dir / "pages").glob("p*.json"))
    total = len(page_paths)
    def _is_tokenized(value: dict) -> bool:
        return bool(value.get("tokenized")) and int(value.get("tokenizeSchema") or 1) >= _TOKENIZE_SCHEMA

    completed = sum(
        1 for page_path in page_paths
        if _is_tokenized(_load(page_path, {}) or {})
    )
    if job_path is not None:
        _update_job(
            job_path,
            state="running",
            phase="tokenizing",
            currentPage=None,
            wordProgress=_progress(total, completed),
            message=f"分词 {completed}/{total}…",
        )
    for page_number, page_path in enumerate(page_paths, start=1):
        if control_path is not None:
            desired = _desired(control_path)
            if desired != "running":
                return _stop_state(job_path, desired) if job_path is not None else 21
        value = _load(page_path, {}) or {}
        if _is_tokenized(value):
            continue
        if job_path is not None:
            _update_job(
                job_path,
                currentPage=page_number,
                wordProgress=_progress(total, completed),
                message=f"分词 {completed}/{total}…",
            )
        chars = value.get("chars")
        if not isinstance(chars, list):
            raise RuntimeError(f"page {page_number} has no OCR character layer")
        # Vision 会给 word id,但它对日文的分词是碎的(实测每组中位 1-2 字,
        # 「フランス」「受けている」这种都切开)。以前"给了 w 就当分好词"直接把
        # 日文页的分词权交给了 Vision —— 用户 2026-08-19 报的"分词也有问题"就是它。
        # 有假名就一律用 fugashi 重分;其余(纯英文/纯中文)保留 Vision 的分组。
        needs_tokenizing = any(
            int(char.get("w", -1)) < 0 for char in chars if not char.get("sp")
        ) or _KANA_RE.search(
            "".join(str(char.get("c") or "") for char in chars if not char.get("sp"))
        )
        # 旧版(按行)分好的词也要按块重分:凡 schema 落后就重跑,不看 w 是否已有。
        stale_schema = bool(value.get("tokenized")) and int(value.get("tokenizeSchema") or 1) < _TOKENIZE_SCHEMA
        if needs_tokenizing or stale_schema:
            value["chars"] = _tokenize_chars(chars, value.get("layout"))
        value["tokenized"] = True
        value["tokenizeSchema"] = _TOKENIZE_SCHEMA
        if job_path is not None:
            _assert_worker_identity(job_path)
        _atomic_json(page_path, value)
        completed += 1
        if job_path is not None:
            _update_job(
                job_path,
                currentPage=None,
                wordProgress=_progress(total, completed),
                message=f"分词 {completed}/{total}…",
            )
    return 0


def _formula_path(project: Path, pdf: Path) -> Path:
    key = hashlib.sha1(str(pdf.resolve()).encode("utf-8")).hexdigest()[:16]
    return project / "state" / "pdf-figures" / f"{key}.json"


def _formula_counts(path: Path) -> tuple[int, int]:
    data = _load(path, {}) or {}
    formulas = data.get("formulas") if isinstance(data.get("formulas"), list) else []
    have = sum(1 for formula in formulas if str(formula.get("latex") or "").strip())
    return len(formulas), have


def _formula_detect_log_path(formula_path: Path, job_path: Path) -> Path:
    job = _load(job_path, {}) or {}
    task_id = str(
        job.get("jobId")
        or _EXPECTED_JOB_ID
        or job.get("workerGeneration")
        or _EXPECTED_WORKER_GENERATION
        or f"pid-{os.getpid()}"
    )
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._")[:96]
    if not safe_task_id:
        safe_task_id = f"pid-{os.getpid()}"
    return (
        formula_path.parent
        / "formula-detect-logs"
        / f"{formula_path.stem}--{safe_task_id}.log"
    )


def _open_formula_detect_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        os.chmod(path, 0o600)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _read_formula_detect_log_tail(path: Path, max_bytes: int = 64 * 1024) -> str:
    limit = max(1, int(max_bytes))
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit), os.SEEK_SET)
        payload = handle.read(limit)
    return payload.decode("utf-8", "replace")


def _formula_sidecar_fingerprint(path: Path) -> tuple[dict, str]:
    return _source_identity(path.stat()), _sha256(path)


def _assert_formula_detection_output(
    formula_path: Path,
    pdf: Path,
    expected_mtime: int,
    baseline: tuple[dict, str],
    started_at_epoch: int,
) -> None:
    try:
        current = json.loads(formula_path.read_text("utf-8"))
        fingerprint = _formula_sidecar_fingerprint(formula_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "formula detector did not leave a valid target sidecar"
        ) from exc
    if not isinstance(current, dict):
        raise RuntimeError("formula detector did not leave a valid target sidecar")
    if current.get("pdf") != str(pdf):
        raise RuntimeError("formula detector updated the wrong target sidecar")
    try:
        book_mtime = int(current.get("book_mtime"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("formula detector left a stale target sidecar") from exc
    if book_mtime != expected_mtime:
        raise RuntimeError("formula detector left a stale target sidecar")
    if fingerprint == baseline:
        raise RuntimeError("formula detector did not update the target sidecar")
    try:
        geom_at = int(current.get("geom_at"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "formula detector did not leave a success marker"
        ) from exc
    if geom_at < started_at_epoch - 1:
        raise RuntimeError("formula detector left a stale success marker")


def _terminate(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    try:
        # Formula/tokenizer children deliberately inherit the worker's process
        # group.  child.pid is therefore not a process-group id; signalling it
        # with killpg can miss the child and leave it running after the worker.
        child.terminate()
        child.wait(timeout=8)
    except Exception:
        try:
            child.kill()
            child.wait(timeout=8)
        except Exception:
            pass


def _run_controlled(child: subprocess.Popen, control_path: Path, job_path: Path, phase: str, formula_path: Path | None = None) -> int | None:
    while child.poll() is None:
        desired = _desired(control_path)
        total = have = 0
        if formula_path is not None:
            total, have = _formula_counts(formula_path)
        job = _load(job_path, {}) or {}
        total_pages = int(job.get("totalPages") or 0)
        detected = phase == "formula-latex"
        _update_job(
            job_path,
            pid=os.getpid(),
            state="running",
            phase=phase,
            formulaTotal=total,
            formulaRecognized=have,
            formulaPendingRegions=(max(0, total - have) if detected else 0),
            formulaFailedRegions=0,
            formulaProgress=_progress(
                total_pages,
                total_pages if detected else 0,
            ),
            formulaState="running",
            message=("检测公式区域…" if phase == "formula-detect" else f"识别公式 {have}/{total}…"),
            percent=(75 if phase == "formula-detect" else round(80 + have * 18 / max(1, total), 1)),
            canPause=True,
            canCancel=True,
        )
        if desired != "running":
            _terminate(child)
            return _stop_state(job_path, desired)
        time.sleep(2)
    return None if child.returncode == 0 else int(child.returncode or 1)


def _run_formula_pipeline(args, job_path: Path, control_path: Path) -> int:
    project = Path(args.project)
    pdf = Path(args.pdf)
    formula_path = _formula_path(project, pdf)
    formula_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load(formula_path, {}) or {}
    expected_mtime = int(pdf.stat().st_mtime)
    if (
        not formula_path.exists()
        or existing.get("pdf") != str(pdf)
        or int(existing.get("book_mtime") or 0) != expected_mtime
    ):
        _atomic_json(formula_path, {
            "pdf": str(pdf),
            "book_mtime": expected_mtime,
            "figures": [],
            "_none_pages": [],
            "formulas": [],
        })
    doclayout_py = os.environ.get(
        "DOCLAYOUT_PYTHON", "/home/bwicarus/doclayout-venv/bin/python"
    )
    yolo = project / "scripts" / "yolo_figures.py"
    cmd = [doclayout_py, str(yolo), "--book", str(pdf), "--force"]
    if os.name == "posix":
        cmd = ["nice", "-n", "19", *cmd]
    # Keeps stderr instead of discarding it.
    #
    # Both streams went to DEVNULL, so a detector that could not start -- a
    # missing interpreter, an unimportable model, a CUDA error -- left the job
    # sitting at "检测公式区域…" with nothing to read anywhere. The phase looked
    # slow when it had already failed.
    baseline = _formula_sidecar_fingerprint(formula_path)
    started_at_epoch = int(time.time())
    detect_log = _formula_detect_log_path(formula_path, job_path)
    try:
        log_handle = _open_formula_detect_log(detect_log)
    except OSError as exc:
        raise RuntimeError(
            f"formula detection log could not open: {exc}"
        ) from exc
    child = None
    try:
        try:
            child = subprocess.Popen(
                cmd,
                cwd=str(project),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            # Names the missing piece. "detection did not start" would send the
            # next reader looking at the wrong layer.
            raise RuntimeError(
                f"formula detection could not start: {exc} (cmd={cmd[0]})"
            ) from exc
        try:
            stopped = _run_controlled(
                child, control_path, job_path, "formula-detect", formula_path
            )
        except BaseException:
            _terminate(child)
            raise
    finally:
        log_handle.close()
    if stopped is not None:
        if stopped in (20, 21):
            return stopped
        detail = ""
        try:
            tail = _read_formula_detect_log_tail(detect_log, 800).strip()
            if tail:
                detail = " | " + " ".join(tail.split())[-400:]
        except OSError:
            pass
        raise RuntimeError(
            f"formula detection exited with {stopped}{detail}"
        )
    try:
        detect_output = _read_formula_detect_log_tail(detect_log)
    except OSError as exc:
        raise RuntimeError(
            f"formula detection log could not be read: {exc}"
        ) from exc
    if re.search(r"(?im)^\s*\[?ERROR(?:\]|\s|:)", detect_output):
        detail = " ".join(detect_output.strip().split())[-400:]
        raise RuntimeError(f"formula detector reported ERROR | {detail}")
    match = re.search(
        r"(?im)^processing\s+(\d+)\s+sidecar\(s\)\.\.\.\s*$",
        detect_output,
    )
    if match is None:
        raise RuntimeError("formula detector did not report its target match count")
    matched_sidecars = int(match.group(1))
    if matched_sidecars == 0:
        raise RuntimeError("formula detector matched no target sidecar")
    if matched_sidecars != 1:
        raise RuntimeError(
            f"formula detector matched an unexpected number of sidecars: {matched_sidecars}"
        )
    _assert_formula_detection_output(
        formula_path,
        pdf,
        expected_mtime,
        baseline,
        started_at_epoch,
    )
    total, have = _formula_counts(formula_path)
    current = _load(job_path, {}) or {}
    total_pages = int(current.get("totalPages") or 0)
    _update_job(
        job_path,
        formulaProgress=_progress(total_pages, total_pages),
        formulaTotal=total,
        formulaRecognized=have,
        formulaPendingRegions=max(0, total - have),
        formulaFailedRegions=0,
    )
    if total == 0:
        _update_job(
            job_path,
            formulaState="succeeded",
            formulaTotal=0,
            formulaRecognized=0,
            formulaPendingRegions=0,
            formulaFailedRegions=0,
        )
        return 0
    app_py = os.environ.get("APP_PYTHON") or sys.executable
    script = project / "scripts" / "formula_ocr_claude.py"
    cmd = [app_py, str(script), "--book", str(pdf), "--sidecar", str(formula_path), "--model", "sonnet", "--effort", "low", "--batch", "8"]
    if os.name == "posix":
        cmd = ["nice", "-n", "10", *cmd]
    child = subprocess.Popen(
        cmd,
        cwd=str(project),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stopped = _run_controlled(child, control_path, job_path, "formula-latex", formula_path)
    if stopped is not None:
        if stopped in (20, 21):
            return stopped
        raise RuntimeError(f"formula OCR exited with {stopped}")
    total, have = _formula_counts(formula_path)
    _update_job(
        job_path,
        formulaState=("succeeded" if have == total else "partial"),
        formulaTotal=total,
        formulaRecognized=have,
        formulaPendingRegions=0,
        formulaFailedRegions=max(0, total - have),
    )
    return 0


def _publish_attachments(
    args,
    job_dir: Path,
    formula_path: Path | None = None,
    *,
    formula_records: list[dict] | None = None,
    manifest_metadata: dict | None = None,
    publish_manifest: bool = True,
    output_dir: Path | None = None,
    generated_at_epoch_ms: int | None = None,
) -> tuple[str, dict]:
    """Publish a content-addressed, path-free derived-attachment manifest."""
    version_dir = Path(output_dir) if output_dir is not None else job_dir.parent
    raw_formula = (
        {"formulas": formula_records}
        if formula_records is not None
        else (_load(formula_path, {}) or {})
    )
    formulas = []
    for value in raw_formula.get("formulas") or []:
        if not isinstance(value, dict):
            continue
        try:
            page = int(value.get("page"))
            bbox = [float(item) for item in value.get("bbox")]
        except (TypeError, ValueError):
            continue
        if (
            page < 1
            or len(bbox) != 4
            or not all(math.isfinite(number) and 0 <= number <= 1 for number in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            continue
        item = {
            "page": page,
            "bbox": bbox,
            "conf": value.get("conf"),
            "latex": value.get("latex"),
        }
        if value.get("multiline") is not None:
            item["multiline"] = bool(value.get("multiline"))
        if value.get("latex_engine"):
            item["latexEngine"] = str(value["latex_engine"])
        formulas.append(item)
    formula_export = {
        "schema": "reader-formula-regions/1",
        "bookId": args.book_id,
        "contentSha256": args.content_sha256,
        "formulas": formulas,
    }
    formula_export_path = version_dir / "formulas.json"
    _atomic_json(formula_export_path, formula_export)

    candidates = []
    for page_path in sorted((job_dir / "pages").glob("p*.json")):
        match = re.fullmatch(r"p(\d{6})\.json", page_path.name)
        if not match:
            continue
        candidates.append((
            "ocr-page-" + match.group(1),
            "ocr-page-chars",
            "pages/" + page_path.name,
            page_path,
        ))
    candidates.append((
        "ocr-formulas",
        "ocr-formula-regions",
        "formulas.json",
        formula_export_path,
    ))
    files = []
    for attachment_id, kind, logical_name, path in candidates:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        files.append({
            "attachmentId": attachment_id,
            "category": "derived",
            "mergePolicy": "immutable",
            "kind": kind,
            "name": logical_name,
            "mediaType": "application/json",
            "size": len(payload),
            "sha256": digest,
        })
    manifest = {
        "contract": "reader-book-attachments/1",
        "schema": 1,
        "bookId": args.book_id,
        "contentSha256": args.content_sha256,
        "category": "derived",
        "mergePolicy": "immutable",
        "files": files,
        "generatedAtEpochMs": (
            int(time.time() * 1000)
            if generated_at_epoch_ms is None
            else int(generated_at_epoch_ms)
        ),
    }
    if manifest_metadata:
        allowed_metadata = {
            "adoptionContract", "source", "pageSources",
            "formulaState", "formulaReason", "formulaCount",
            "engine", "executor", "processingProfile", "totalPages",
        }
        if set(manifest_metadata) - allowed_metadata:
            raise ValueError("unsupported attachment manifest metadata")
        manifest.update(dict(manifest_metadata))
    revision = _manifest_revision(manifest)
    manifest["revision"] = revision
    for item in files:
        item["downloadUrl"] = (
            f"/pdf/api/library/attachments/{args.book_id}/{item['attachmentId']}"
            f"?contentSha256={args.content_sha256}&revision={revision}"
        )
    if publish_manifest:
        _atomic_json(version_dir / "attachments.json", manifest)
    return revision, manifest


def _validate_release_artifacts(
    args,
    release_dir: Path,
    revision: str,
    *,
    total_pages: int,
    formula_state: str,
    executor: str,
    processing_profile: str,
    source_identity: dict | None,
) -> tuple[dict, dict, dict, dict]:
    """Fully validate a staging or immutable release without holding jobs.lock."""

    release_rel = f"releases/{revision}"
    manifest_path = release_dir / "attachments.json"
    committed_manifest = _load(manifest_path, {}) or {}
    committed_job = _load(release_dir / "job.json", {}) or {}
    committed_result = _load(release_dir / "result.json", {}) or {}
    committed_current = _load(release_dir / "current.json", {}) or {}
    if (
        committed_manifest.get("contract") != "reader-book-attachments/1"
        or committed_manifest.get("bookId") != args.book_id
        or committed_manifest.get("contentSha256") != args.content_sha256
        or committed_manifest.get("revision") != revision
        or committed_manifest.get("engine") != args.engine
        or _processing_identity(committed_manifest) != (executor, processing_profile)
        or int(committed_manifest.get("totalPages") or 0) != total_pages
        or committed_manifest.get("formulaState") != formula_state
        or committed_job.get("bookId") != args.book_id
        or committed_job.get("contentSha256") != args.content_sha256
        or committed_job.get("engine") != args.engine
        or _processing_identity(committed_job) != (executor, processing_profile)
        or committed_job.get("state") != "succeeded"
        or not committed_job.get("resultAvailable")
        or committed_job.get("pageCharsRevision") != revision
        or committed_job.get("formulaState") != formula_state
        or int(committed_job.get("totalPages") or 0) != total_pages
        or int(committed_job.get("successfulPages") or 0) != total_pages
        or committed_result.get("engine") != args.engine
        or _processing_identity(committed_result) != (executor, processing_profile)
        or committed_result.get("pageCharsRevision") != revision
        or committed_result.get("release") != release_rel
        or not isinstance(committed_result.get("sourceIdentity"), dict)
        or (
            source_identity is not None
            and committed_result.get("sourceIdentity") != source_identity
        )
        or committed_current != {
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
        }
    ):
        raise RuntimeError("published OCR release metadata is inconsistent")
    page_ids = [
        int(match.group(1))
        for entry in committed_manifest.get("files") or []
        if (match := re.fullmatch(
            r"ocr-page-(\d{6})", str(entry.get("attachmentId") or "")
        ))
    ]
    if page_ids != list(range(1, total_pages + 1)):
        raise RuntimeError("published OCR release has incomplete pages")
    if len(committed_manifest.get("files") or []) != total_pages + 1:
        raise RuntimeError("published OCR release has unexpected files")
    for entry in committed_manifest.get("files") or []:
        attachment_id = str(entry.get("attachmentId") or "")
        match = re.fullmatch(r"ocr-page-(\d{6})", attachment_id)
        if match:
            path = release_dir / "pages" / f"p{int(match.group(1)):06d}.json"
        elif attachment_id == "ocr-formulas":
            path = release_dir / "formulas.json"
        else:
            raise RuntimeError("published OCR release has an unknown attachment")
        payload = path.read_bytes()
        if (
            len(payload) != int(entry.get("size") or -1)
            or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
        ):
            raise RuntimeError("published OCR release digest mismatch")
        try:
            value = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("published OCR release JSON is invalid") from exc
        if match:
            page_number = int(match.group(1))
            if (
                not isinstance(value, dict)
                or value.get("schema") != PAGE_SCHEMA
                or value.get("bookId") != args.book_id
                or value.get("contentSha256") != args.content_sha256
                or value.get("engine") != args.engine
                or _processing_identity(value) != (executor, processing_profile)
                or value.get("pageNumber") != page_number
                or not isinstance(value.get("chars"), list)
                or not isinstance(value.get("furigana"), list)
            ):
                raise RuntimeError("published OCR page identity is invalid")
        else:
            formulas = value.get("formulas") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict)
                or value.get("schema") != "reader-formula-regions/1"
                or value.get("bookId") != args.book_id
                or value.get("contentSha256") != args.content_sha256
                or not isinstance(formulas, list)
                or len(formulas) != int(committed_manifest.get("formulaCount") or 0)
            ):
                raise RuntimeError("published OCR formula identity is invalid")
            for formula in formulas:
                try:
                    formula_page = int(formula.get("page"))
                    bbox = [float(number) for number in formula.get("bbox")]
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RuntimeError("published OCR formula record is invalid") from exc
                if (
                    formula_page < 1
                    or formula_page > total_pages
                    or len(bbox) != 4
                    or not all(math.isfinite(number) and 0 <= number <= 1 for number in bbox)
                    or bbox[0] >= bbox[2]
                    or bbox[1] >= bbox[3]
                ):
                    raise RuntimeError("published OCR formula record is invalid")
    return committed_manifest, committed_job, committed_result, committed_current


def _publish_release(
    args,
    job_dir: Path,
    formula_path: Path,
    final_job: dict,
    *,
    source_path: Path | None = None,
    source_guard: dict | None = None,
    lock_held: bool = False,
    terminal_job: dict | None = None,
    finalizer_token: str | None = None,
) -> str:
    """Build/verify lock-free, then atomically activate one run under jobs.lock."""

    version_dir = job_dir.parent
    staging = version_dir / (".release-staging-" + uuid.uuid4().hex)
    own_source_guard = None
    try:
        _assert_worker_identity(job_dir / "job.json")
        if source_guard is None:
            if source_path is None:
                raise RuntimeError("verified source guard is required for publication")
            own_source_guard = _open_source_guard(
                Path(source_path), args.content_sha256, int(args.max_bytes)
            )
            source_guard = own_source_guard
        pages = staging / "pages"
        pages.mkdir(parents=True, exist_ok=False)
        for source in sorted((job_dir / "pages").glob("p*.json")):
            if not re.fullmatch(r"p\d{6}\.json", source.name):
                continue
            destination = pages / source.name
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
        total_pages = int(final_job.get("totalPages") or 0)
        formula_state = str(final_job.get("formulaState") or "failed")
        executor, processing_profile = _processing_identity(final_job)
        revision, manifest = _publish_attachments(
            args,
            staging,
            formula_path,
            manifest_metadata={
                "engine": args.engine,
                "executor": executor,
                "processingProfile": processing_profile,
                "totalPages": total_pages,
                "formulaState": formula_state,
                "formulaReason": final_job.get("formulaReason"),
                "formulaCount": int(final_job.get("formulaTotal") or 0),
            },
            publish_manifest=False,
            output_dir=staging,
            generated_at_epoch_ms=0,
        )
        release_rel = f"releases/{revision}"
        release_job = {
            **final_job,
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "state": "succeeded",
            "resultAvailable": True,
            "pageCharsRevision": revision,
        }
        release_result = {
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
            "pageCharsRevision": revision,
            "release": release_rel,
            "completedAtEpochMs": int(final_job.get("updatedAtEpochMs") or 0),
            "sourceIdentity": dict(source_guard["identity"]),
        }
        release_current = {
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
        }
        _atomic_json(staging / "job.json", release_job)
        _atomic_json(staging / "result.json", release_result)
        _atomic_json(staging / "current.json", release_current)
        _atomic_json(staging / "attachments.json", manifest)
        _validate_release_artifacts(
            args,
            staging,
            revision,
            total_pages=total_pages,
            formula_state=formula_state,
            executor=executor,
            processing_profile=processing_profile,
            source_identity=source_guard["identity"],
        )
        release_dir = version_dir / "releases" / revision
        staging_manifest = (staging / "attachments.json").read_bytes()
        immutable_result = release_result
        immutable_current = release_current
        if release_dir.exists():
            (
                _immutable_manifest,
                _immutable_job,
                immutable_result,
                immutable_current,
            ) = _validate_release_artifacts(
                args,
                release_dir,
                revision,
                total_pages=total_pages,
                formula_state=formula_state,
                executor=executor,
                processing_profile=processing_profile,
                # The release is content-addressed and immutable.  A rerun of
                # the same bytes may legitimately open the source through a
                # new inode; validate its stored identity shape, not equality
                # with this execution's guarded descriptor.
                source_identity=None,
            )
            if (release_dir / "attachments.json").read_bytes() != staging_manifest:
                raise RuntimeError("content-addressed OCR release conflict")
        # The only expensive source hash in the final phase stays outside the
        # global jobs lock.  The lock-held check below compares fd/path identity.
        _assert_source_guard(source_guard, rehash=True)
        fence = {
            "contract": PUBLICATION_CONTRACT,
            "bookId": args.book_id,
            "contentSha256": args.content_sha256,
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
            "release": release_rel,
            "manifestSha256": hashlib.sha256(staging_manifest).hexdigest(),
            "sourceIdentity": dict(immutable_result["sourceIdentity"]),
        }

        def commit() -> None:
            nonlocal staging
            current_job = _assert_worker_identity(job_dir / "job.json")
            if finalizer_token is not None and current_job.get(
                "finalizerOwnerToken"
            ) != finalizer_token:
                raise RuntimeError("OCR publication finalizer is no longer current")
            if (
                _desired(job_dir / "control.json") != "running"
                or current_job.get("state") in ("pause-requested", "cancel-requested")
            ):
                raise RuntimeError("OCR worker stop requested before publication")
            _assert_source_guard(source_guard, rehash=False)
            release_dir.parent.mkdir(parents=True, exist_ok=True)
            if release_dir.exists():
                existing_manifest = release_dir / "attachments.json"
                if (
                    not existing_manifest.is_file()
                    or existing_manifest.read_bytes() != staging_manifest
                ):
                    raise RuntimeError("content-addressed OCR release conflict")
                # Keep the duplicate staging reachable until after jobs.lock
                # is released; recursive cleanup can be large.
            else:
                os.replace(staging, release_dir)
                staging = None
            release_index = _release_index_after_publish(
                args,
                version_dir,
                release_dir,
                revision,
                release_job,
                release_result,
                manifest,
            )
            index_path = version_dir / "releases-index.json"
            previous_index = _file_snapshot(index_path)
            try:
                _atomic_json(index_path, release_index)
                _assert_source_guard(source_guard, rehash=False)
                _assert_worker_identity(job_dir / "job.json")
            except Exception:
                _restore_file_snapshot(index_path, previous_index)
                raise
            if terminal_job is not None:
                committed_terminal = {
                    **terminal_job,
                    "state": "succeeded",
                    "resultAvailable": True,
                    "pageCharsRevision": revision,
                }
                for field in (
                    "finalizerStage",
                    "finalizerOwnerToken",
                    "finalizerPid",
                    "finalizerProcessStartToken",
                ):
                    committed_terminal.pop(field, None)
                # The index and mutable terminal identity linearize under the
                # same jobs.lock.  If this replace is interrupted after the
                # index commit, status reconstructs this exact run identity
                # from the immutable release plus the ledger entry.
                _atomic_json(job_dir / "job.json", committed_terminal)
            # Compatibility mirrors are repairable and may never revoke the
            # already-committed activeRunId if one replace fails.
            for path, value in (
                (version_dir / "publication.json", fence),
                (version_dir / "result.json", immutable_result),
                (version_dir / "current.json", immutable_current),
            ):
                try:
                    _atomic_json(path, value)
                except Exception:
                    continue

        if lock_held:
            commit()
        else:
            # ReaderPC imports this module without Pi's sidecar store; only the
            # detached Pi commit path loads the shared process lock.
            from reader_sidecar_store import exclusive_lock

            with exclusive_lock(_release_jobs_lock_path(
                version_dir,
                book_id=args.book_id,
                content_sha256=args.content_sha256,
            )):
                commit()
        return revision
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _close_source_guard(own_source_guard)


def _worker_jobs_lock(args, job_dir: Path):
    """Pi-only lazy lock import; ReaderPC can still import this module alone."""

    from reader_sidecar_store import exclusive_lock

    return exclusive_lock(_release_jobs_lock_path(
        job_dir.parent,
        book_id=args.book_id,
        content_sha256=args.content_sha256,
    ))


def _publication_committed_for_job(args, job_dir: Path, job: dict) -> bool:
    """Return true only when the authoritative active run is this execution."""

    index = _load_release_index_for_publish(args, job_dir.parent)
    active = index.get("activeRunId")
    if active is None:
        return False
    entry = next(
        (
            item
            for item in index.get("runs") or []
            if isinstance(item, dict) and item.get("runId") == active
        ),
        None,
    )
    return bool(
        entry is not None
        and entry.get("runId") == job.get("runId")
        and (
            not entry.get("jobId")
            or entry.get("jobId") == job.get("jobId")
        )
    )


def _write_terminal_job(args, job_dir: Path, value: dict) -> None:
    """Linearize the post-publication mutable success write by generation."""

    job_path = job_dir / "job.json"
    with _worker_jobs_lock(args, job_dir):
        current = _assert_worker_identity(job_path)
        if _publication_committed_for_job(args, job_dir, value):
            current = {**current, "state": "succeeded"}
        if current.get("state") in ("pause-requested", "cancel-requested"):
            raise RuntimeError("OCR worker stop requested after publication")
        _atomic_json(job_path, value)


def _record_worker_failure(
    args,
    job_dir: Path,
    exc: Exception,
    pdf: Path,
    project: Path,
) -> bool:
    """Write failure only while this exact worker generation still owns job.json."""

    job_path = job_dir / "job.json"
    try:
        with _worker_jobs_lock(args, job_dir):
            current = _load(job_path, {}) or {}
            try:
                _assert_worker_identity(job_path, current)
            except RuntimeError:
                return False
            if _publication_committed_for_job(args, job_dir, current):
                return False
            if current.get("state") in ("pause-requested", "cancel-requested"):
                return False
            phase = current.get("phase") or "preparing"
            text_progress = (
                current.get("textProgress")
                if isinstance(current.get("textProgress"), dict)
                else _progress()
            )
            word_progress = (
                current.get("wordProgress")
                if isinstance(current.get("wordProgress"), dict)
                else _progress()
            )
            formula_progress = (
                current.get("formulaProgress")
                if isinstance(current.get("formulaProgress"), dict)
                else _progress()
            )
            failed_pages = int(current.get("failedPages") or 0)
            if phase == "text-ocr":
                text_progress = _progress(
                    text_progress.get("total"),
                    text_progress.get("completed"),
                    failed=int(text_progress.get("failed") or 0) + 1,
                )
                word_progress = _progress(
                    word_progress.get("total"),
                    word_progress.get("completed"),
                    failed=int(word_progress.get("failed") or 0) + 1,
                )
                failed_pages = max(1, failed_pages)
            elif phase == "tokenizing":
                word_progress = _progress(
                    word_progress.get("total"),
                    word_progress.get("completed"),
                    failed=max(1, int(word_progress.get("failed") or 0)),
                )
            elif phase == "formula-detect":
                formula_progress = _progress(
                    formula_progress.get("total"),
                    formula_progress.get("completed"),
                    failed=max(
                        1,
                        int(formula_progress.get("total") or 0)
                        - int(formula_progress.get("completed") or 0),
                    ),
                )
            elif phase == "formula-latex":
                current_pending = int(current.get("formulaPendingRegions") or 0)
                current["formulaFailedRegions"] = max(
                    int(current.get("formulaFailedRegions") or 0), current_pending
                )
                current["formulaPendingRegions"] = 0
            failed = {
                **current,
                "state": "failed",
                "phase": phase,
                "textState": current.get("textState") or "failed",
                "formulaState": (
                    "failed"
                    if phase in ("formula-detect", "formula-latex")
                    else current.get("formulaState") or "idle"
                ),
                "failedPages": failed_pages,
                "currentPage": current.get("currentPage"),
                "textProgress": text_progress,
                "wordProgress": word_progress,
                "formulaProgress": formula_progress,
                "formulaPendingRegions": int(current.get("formulaPendingRegions") or 0),
                "formulaFailedRegions": int(current.get("formulaFailedRegions") or 0),
                "errorCode": "ocr-worker-failed",
                "error": _safe_error(exc, pdf, project),
                "message": "Pi 预处理失败；已完成页保留，可重试",
                "canPause": False,
                "canResume": False,
                "canCancel": False,
                "canRetry": True,
                "updatedAtEpochMs": int(time.time() * 1000),
            }
            _atomic_json(job_path, failed)
            return True
    except Exception:
        return False


def run(args) -> int:
    import fitz

    job_dir = Path(args.job_dir)
    job_path = job_dir / "job.json"
    control_path = job_dir / "control.json"
    pdf = Path(args.pdf)
    pages_dir = job_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    source_guard = None
    try:
        _update_job(
            job_path,
            pid=os.getpid(),
            workerPid=os.getpid(),
            processGroupId=(os.getpgrp() if os.name == "posix" else None),
        )
        source_guard = _open_source_guard(pdf, args.content_sha256, args.max_bytes)
        document = fitz.open(str(pdf))
        try:
            total = document.page_count
            if total <= 0 or total > args.max_pages:
                raise RuntimeError("PDF page count exceeds the configured preprocessing limit")
            engine = None
            if args.engine == "manga":
                from mokuro.manga_page_ocr import MangaPageOcr
                engine = MangaPageOcr(force_cpu=True)
            existing = sum(
                1 for page_number in range(1, total + 1)
                if _page_done(
                    pages_dir / f"p{page_number:06d}.json",
                    args.book_id,
                    args.content_sha256,
                    args.engine,
                    PROCESSING_PROFILES["pi"],
                )
            )
            tokenized = (
                existing
                if args.engine == "vision"
                else sum(
                    1 for page_number in range(1, total + 1)
                    if _page_done(
                        pages_dir / f"p{page_number:06d}.json",
                        args.book_id,
                        args.content_sha256,
                        args.engine,
                        PROCESSING_PROFILES["pi"],
                    ) and bool(
                        (_load(pages_dir / f"p{page_number:06d}.json", {}) or {}).get("tokenized")
                    )
                )
            )
            _update_job(
                job_path,
                state="running",
                phase="text-ocr",
                totalPages=total,
                textProgress=_progress(total, existing),
                wordProgress=_progress(total, tokenized),
                formulaProgress=_progress(total, 0),
                formulaPendingRegions=0,
                formulaFailedRegions=0,
                currentPage=None,
            )
            start = time.time()
            recognized = 0
            for page_number in range(1, total + 1):
                desired = _desired(control_path)
                if desired != "running":
                    return _stop_state(job_path, desired)
                page_path = pages_dir / f"p{page_number:06d}.json"
                if _page_done(
                    page_path,
                    args.book_id,
                    args.content_sha256,
                    args.engine,
                    PROCESSING_PROFILES["pi"],
                ):
                    value = _load(page_path, {}) or {}
                    if value.get("chars"):
                        recognized += 1
                    continue
                _update_job(
                    job_path,
                    pid=os.getpid(),
                    state="running",
                    phase="text-ocr",
                    textState="running",
                    totalPages=total,
                    processedPages=existing,
                    successfulPages=existing,
                    failedPages=0,
                    recognizedPages=recognized,
                    currentPage=page_number,
                    textProgress=_progress(total, existing),
                    wordProgress=_progress(
                        total, tokenized
                    ),
                    formulaProgress=_progress(total, 0),
                    percent=round(existing * 75 / max(1, total), 1),
                    message=f"文字识别 {existing}/{total}；暂停会保留完成页",
                    canPause=True,
                    canResume=False,
                    canCancel=True,
                    canRetry=False,
                )
                page = document[page_number - 1]
                effective_dpi = None
                if args.engine == "vision":
                    chars, text, image_w, image_h, effective_dpi = _vision_page(
                        page, Path(args.project)
                    )
                    layout = _vision_page_layout(
                        chars,
                        page_w=float(page.rect.width),
                        page_h=float(page.rect.height),
                    )
                else:
                    vision_chars = None
                    try:
                        (
                            vision_chars,
                            _vision_text,
                            _vision_image_w,
                            _vision_image_h,
                            effective_dpi,
                        ) = _vision_page(page, Path(args.project))
                    except Exception:
                        # Manga segmentation remains independently usable when
                        # Vision is temporarily unavailable.  Each low-quality
                        # or missing Vision line also falls back inside
                        # _manga_vision_line_chars instead of failing the page.
                        vision_chars = None
                        effective_dpi = None
                    chars, text, image_w, image_h, layout = _manga_page(
                        page,
                        engine,
                        vision_chars=vision_chars,
                        include_layout=True,
                    )
                sidecar = {
                    "schema": PAGE_SCHEMA,
                    "bookId": args.book_id,
                    "contentSha256": args.content_sha256,
                    "engine": args.engine,
                    "executor": "pi",
                    "processingProfile": PROCESSING_PROFILES["pi"],
                    "pageNumber": page_number,
                    "page_w": float(page.rect.width),
                    "page_h": float(page.rect.height),
                    "imageWidth": image_w,
                    "imageHeight": image_h,
                    "chars": chars,
                    "layout": layout,
                    "furigana": [],
                    "textCharCount": len("".join(text.split())),
                    "generatedAtEpochMs": int(time.time() * 1000),
                }
                if effective_dpi is not None:
                    # 贴合度问题永远先看这一项 —— 有效 DPI 掉下去,框就会开始糊。
                    sidecar["visionEffectiveDpi"] = round(effective_dpi, 1)
                    if effective_dpi < VISION_MIN_DPI:
                        sidecar["visionDpiShortfall"] = True
                _assert_worker_identity(job_path)
                _atomic_json(page_path, sidecar)
                existing += 1
                if chars:
                    recognized += 1
                elapsed = max(0.001, time.time() - start)
                eta = int((total - existing) * elapsed / max(1, existing))
                _update_job(
                    job_path,
                    processedPages=existing,
                    successfulPages=existing,
                    recognizedPages=recognized,
                    totalPages=total,
                    currentPage=None,
                    textProgress=_progress(total, existing),
                    wordProgress=_progress(total, tokenized),
                    percent=round(existing * 75 / max(1, total), 1),
                    etaSeconds=eta,
                )
        finally:
            document.close()

        _update_job(
            job_path,
            textState="succeeded",
            currentPage=None,
            textProgress=_progress(total, existing),
            wordProgress=_progress(total, tokenized),
        )

        # Manga OCR has line boxes but no word ids.  Run the existing App Python
        # environment's real fugashi tokenizer; a missing tokenizer is a real
        # failure, not a reason to fabricate word boundaries.
        if args.engine == "manga":
            app_py = os.environ.get("APP_PYTHON") or sys.executable
            _update_job(
                job_path,
                phase="tokenizing",
                currentPage=None,
                wordProgress=_progress(total, tokenized),
                message=f"分词 {tokenized}/{total}…",
            )
            child = subprocess.run(
                [
                    app_py,
                    str(Path(__file__)),
                    "--tokenize-dir", str(job_dir),
                    "--job-path", str(job_path),
                    "--control-path", str(control_path),
                    "--job-id", args.job_id,
                    "--worker-generation", args.worker_generation,
                ],
                cwd=str(args.project),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
            if child.returncode in (20, 21):
                return child.returncode
            if child.returncode != 0:
                raise RuntimeError("manga OCR word tokenization failed")
            tokenized = total
        _update_job(
            job_path,
            phase="finalizing",
            currentPage=None,
            textProgress=_progress(total, existing),
            wordProgress=_progress(total, tokenized),
        )

        formula_path = _formula_path(Path(args.project), pdf)
        _update_job(
            job_path,
            textState="succeeded",
            resultAvailable=False,
            percent=75,
            phase="formula-detect",
            formulaState="queued",
            message="文字 sidecar 已完成，开始检测公式区域",
            pageCharsRevision=None,
            currentPage=None,
            textProgress=_progress(total, existing),
            wordProgress=_progress(total, tokenized),
            formulaProgress=_progress(total, 0),
            formulaPendingRegions=0,
            formulaFailedRegions=0,
        )
        formula_result = _run_formula_pipeline(args, job_path, control_path)
        if formula_result in (20, 21):
            return formula_result
        total, have = _formula_counts(formula_path)
        current_job = _load(job_path, {}) or {}
        final_job = {
            **current_job,
            "state": "succeeded",
            "phase": "finalizing",
            "textState": "succeeded",
            "formulaState": ("succeeded" if have == total else "partial"),
            "formulaTotal": total,
            "formulaRecognized": have,
            "formulaPendingRegions": 0,
            "formulaFailedRegions": max(0, total - have),
            "currentPage": None,
            "textProgress": _progress(existing, existing),
            "wordProgress": _progress(existing, tokenized),
            "formulaProgress": _progress(existing, existing),
            "percent": 100,
            "etaSeconds": 0,
            "message": f"Pi 预处理完成：文字 {existing} 页，公式 {have}/{total}",
            "canPause": False,
            "canResume": False,
            "canCancel": False,
            "canRetry": False,
            "resultAvailable": True,
            "pageCharsRevision": None,
        }
        final_job["updatedAtEpochMs"] = int(time.time() * 1000)
        _publish_release(
            args,
            job_dir,
            formula_path,
            final_job,
            source_guard=source_guard,
            terminal_job=final_job,
        )
        return 0
    except Exception as exc:
        _record_worker_failure(
            args, job_dir, exc, pdf, Path(args.project)
        )
        return 1
    finally:
        _close_source_guard(source_guard)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenize-dir")
    parser.add_argument("--job-path")
    parser.add_argument("--control-path")
    parser.add_argument("--job-dir")
    parser.add_argument("--pdf")
    parser.add_argument("--project")
    parser.add_argument("--book-id")
    parser.add_argument("--content-sha256")
    parser.add_argument("--engine", choices=("vision", "manga"))
    parser.add_argument("--job-id")
    parser.add_argument("--worker-generation")
    parser.add_argument("--max-pages", type=int, default=5000)
    parser.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    _set_worker_identity(args.job_id, args.worker_generation)
    if args.tokenize_dir:
        return _tokenize_directory(
            Path(args.tokenize_dir),
            Path(args.job_path) if args.job_path else None,
            Path(args.control_path) if args.control_path else None,
        )
    required = (
        args.job_dir, args.pdf, args.project, args.book_id,
        args.content_sha256, args.engine, args.job_id, args.worker_generation,
    )
    if not all(required):
        raise SystemExit("worker arguments are required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
