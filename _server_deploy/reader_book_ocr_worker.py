"""Detached worker for :mod:`reader_book_ocr`.

The worker performs page-bounded OCR and writes each successful page atomically.
Pause/cancel are checkpoint requests: completed pages remain durable, while the
page in progress may be repeated after resume.  The original PDF is read-only.
"""

from __future__ import annotations

import argparse
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
PROCESSING_PROFILES = {"pi": "pi-default-v1", "pc": "quality-first-v2"}
_EXPECTED_JOB_ID: str | None = None
_EXPECTED_WORKER_GENERATION: str | None = None


def _processing_identity(value: dict | None) -> tuple[str, str]:
    item = value if isinstance(value, dict) else {}
    executor = str(item.get("executor") or "pi")
    if executor not in PROCESSING_PROFILES:
        executor = "pi"
    profile = str(item.get("processingProfile") or PROCESSING_PROFILES[executor])
    return executor, profile


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


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


def _page_done(path: Path, book_id: str, content_sha256: str, engine: str) -> bool:
    value = _load(path, {}) or {}
    return (
        value.get("schema") == PAGE_SCHEMA
        and value.get("bookId") == book_id
        and value.get("contentSha256") == content_sha256
        and value.get("engine") == engine
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


def _manga_page(page, engine) -> tuple[list[dict], str, int, int]:
    pix = page.get_pixmap(dpi=300, alpha=False)
    image_w, image_h = pix.width, pix.height
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_name = handle.name
        pix.save(temp_name)
        raw = engine(temp_name) or {}
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
            )
            for character, x0, y0, x1, y1 in cells:
                chars.append({
                    "c": character,
                    "x0": round(x0 * sx, 3),
                    "y0": round(y0 * sy, 3),
                    "x1": round(x1 * sx, 3),
                    "y1": round(y1 * sy, 3),
                    "w": -1,
                    "bk": block_no,
                    "line": line_no,
                    "b": 0,
                })
            if cells:
                text_lines.append(text)
                line_no += 1
    return chars, "\n".join(text_lines), image_w, image_h


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def _tokenize_chars(chars: list[dict]) -> list[dict]:
    """Assign real tokenizer boundaries; never invent words on mismatch."""
    by_line: dict[tuple[int, int], list[dict]] = {}
    for char in chars:
        by_line.setdefault((int(char.get("bk", -1)), int(char.get("line", 0))), []).append(char)
    tagger = None
    for group_no, group in enumerate(by_line.values()):
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
    completed = sum(
        1 for page_path in page_paths
        if bool((_load(page_path, {}) or {}).get("tokenized"))
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
        if value.get("tokenized"):
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
        if needs_tokenizing:
            value["chars"] = _tokenize_chars(chars)
        value["tokenized"] = True
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


def _publish_release(
    args,
    job_dir: Path,
    formula_path: Path,
    final_job: dict,
    *,
    source_path: Path | None = None,
    source_guard: dict | None = None,
) -> str:
    """Build one immutable release and flip the publication fence last."""
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
        _atomic_json(staging / "job.json", release_job)
        _atomic_json(staging / "result.json", release_result)
        _atomic_json(staging / "current.json", {
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
        })
        _atomic_json(staging / "attachments.json", manifest)
        release_dir = version_dir / "releases" / revision
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        if release_dir.exists():
            existing_manifest = release_dir / "attachments.json"
            if (
                not existing_manifest.is_file()
                or existing_manifest.read_bytes() != (staging / "attachments.json").read_bytes()
            ):
                raise RuntimeError("content-addressed OCR release conflict")
            shutil.rmtree(staging)
            staging = None
        else:
            os.replace(staging, release_dir)
            staging = None
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
            or committed_result.get("sourceIdentity") != source_guard["identity"]
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
        _atomic_json(version_dir / "result.json", release_result)
        _atomic_json(version_dir / "current.json", {
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
        })
        fence_path = version_dir / "publication.json"
        previous_fence = _load(fence_path, None)
        fence = {
            "contract": PUBLICATION_CONTRACT,
            "bookId": args.book_id,
            "contentSha256": args.content_sha256,
            "engine": args.engine,
            "executor": executor,
            "processingProfile": processing_profile,
            "revision": revision,
            "release": release_rel,
            "manifestSha256": _sha256(manifest_path),
            "sourceIdentity": dict(source_guard["identity"]),
        }
        _assert_source_guard(source_guard, rehash=True)
        _assert_worker_identity(job_dir / "job.json")
        _atomic_json(fence_path, fence)
        try:
            # Recheck bytes, not only path metadata: an in-place writer can keep
            # size/mtime stable while changing content during the fence write.
            _assert_source_guard(source_guard, rehash=True)
            # The service may have replaced this worker generation while the
            # fence was being written.  A stale generation must not publish.
            _assert_worker_identity(job_dir / "job.json")
        except Exception:
            if isinstance(previous_fence, dict):
                _atomic_json(fence_path, previous_fence)
            else:
                fence_path.unlink(missing_ok=True)
            raise
        return revision
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _close_source_guard(own_source_guard)


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
                )
            )
            tokenized = (
                existing
                if args.engine == "vision"
                else sum(
                    1 for page_number in range(1, total + 1)
                    if bool((_load(pages_dir / f"p{page_number:06d}.json", {}) or {}).get("tokenized"))
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
                if _page_done(page_path, args.book_id, args.content_sha256, args.engine):
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
                else:
                    chars, text, image_w, image_h = _manga_page(page, engine)
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
        revision = _publish_release(
            args, job_dir, formula_path, final_job, source_guard=source_guard
        )
        final_job["pageCharsRevision"] = revision
        final_job["updatedAtEpochMs"] = int(time.time() * 1000)
        _atomic_json(job_path, final_job)
        return 0
    except Exception as exc:
        current = _load(job_path, {}) or {}
        phase = current.get("phase") or "preparing"
        text_progress = current.get("textProgress") if isinstance(current.get("textProgress"), dict) else _progress()
        word_progress = current.get("wordProgress") if isinstance(current.get("wordProgress"), dict) else _progress()
        formula_progress = current.get("formulaProgress") if isinstance(current.get("formulaProgress"), dict) else _progress()
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
        _update_job(
            job_path,
            state="failed",
            phase=phase,
            textState=(current.get("textState") or "failed"),
            formulaState=(
                "failed"
                if phase in ("formula-detect", "formula-latex")
                else current.get("formulaState") or "idle"
            ),
            failedPages=failed_pages,
            currentPage=current.get("currentPage"),
            textProgress=text_progress,
            wordProgress=word_progress,
            formulaProgress=formula_progress,
            formulaPendingRegions=int(current.get("formulaPendingRegions") or 0),
            formulaFailedRegions=int(current.get("formulaFailedRegions") or 0),
            errorCode="ocr-worker-failed",
            error=_safe_error(exc, pdf, Path(args.project)),
            message="Pi 预处理失败；已完成页保留，可重试",
            canPause=False,
            canResume=False,
            canCancel=False,
            canRetry=True,
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
