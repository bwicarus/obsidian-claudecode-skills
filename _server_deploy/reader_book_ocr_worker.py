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
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata


CONTRACT = "reader-library-ocr/1"
PAGE_SCHEMA = "reader-page-chars/1"


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


def _update_job(job_path: Path, **changes) -> dict:
    job = _load(job_path, {}) or {}
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


def _vision_page(page, project: Path) -> tuple[list[dict], str, int, int]:
    scripts = project / "scripts"
    sys.path.insert(0, str(scripts))
    from google_vision_ocr import _load_key, ocr_one_page  # type: ignore

    long_pt = max(float(page.rect.width), float(page.rect.height)) or 1.0
    zoom = min(300.0 / 72.0, 4000.0 / long_pt)
    pix = page.get_pixmap(matrix=__import__("fitz").Matrix(zoom, zoom), alpha=False)
    try:
        image = pix.tobytes("jpg", jpg_quality=90)
        image_w, image_h = pix.width, pix.height
    finally:
        del pix
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
    return chars, str(raw.get("text") or ""), image_w, image_h


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
            xs = [float(point[0]) for point in points if len(point) >= 2]
            ys = [float(point[1]) for point in points if len(point) >= 2]
            if not xs or not ys:
                continue
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            visible = [character for character in text if not character.isspace()]
            if not visible:
                continue
            cell = max(1.0, (x1 - x0) / len(visible))
            for offset, character in enumerate(visible):
                chars.append({
                    "c": character,
                    "x0": round((x0 + offset * cell) * sx, 3),
                    "y0": round(y0 * sy, 3),
                    "x1": round((x0 + (offset + 1) * cell) * sx, 3),
                    "y1": round(y1 * sy, 3),
                    "w": -1,
                    "bk": block_no,
                    "line": line_no,
                    "b": 0,
                })
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
        if any(int(char.get("w", -1)) < 0 for char in chars if not char.get("sp")):
            value["chars"] = _tokenize_chars(chars)
        value["tokenized"] = True
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


def _terminate(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(child.pid, signal.SIGTERM)
        else:
            child.terminate()
        child.wait(timeout=8)
    except Exception:
        try:
            child.kill()
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
    child = subprocess.Popen(
        cmd,
        cwd=str(project),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    stopped = _run_controlled(child, control_path, job_path, "formula-detect", formula_path)
    if stopped is not None:
        if stopped in (20, 21):
            return stopped
        raise RuntimeError(f"formula detection exited with {stopped}")
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
        start_new_session=(os.name == "posix"),
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


def _publish_attachments(args, job_dir: Path, formula_path: Path) -> tuple[str, dict]:
    """Publish a content-addressed, path-free derived-attachment manifest."""
    version_dir = job_dir.parent
    raw_formula = _load(formula_path, {}) or {}
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
    revision_digest = hashlib.sha256()
    for attachment_id, kind, logical_name, path in candidates:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        revision_digest.update(attachment_id.encode("ascii"))
        revision_digest.update(bytes.fromhex(digest))
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
    revision = "ocr_" + revision_digest.hexdigest()[:20]
    for item in files:
        item["downloadUrl"] = (
            f"/pdf/api/library/attachments/{args.book_id}/{item['attachmentId']}"
            f"?contentSha256={args.content_sha256}&revision={revision}"
        )
    manifest = {
        "contract": "reader-book-attachments/1",
        "schema": 1,
        "bookId": args.book_id,
        "contentSha256": args.content_sha256,
        "revision": revision,
        "category": "derived",
        "mergePolicy": "immutable",
        "files": files,
        "generatedAtEpochMs": int(time.time() * 1000),
    }
    _atomic_json(version_dir / "attachments.json", manifest)
    return revision, manifest


def run(args) -> int:
    import fitz

    job_dir = Path(args.job_dir)
    job_path = job_dir / "job.json"
    control_path = job_dir / "control.json"
    pdf = Path(args.pdf)
    pages_dir = job_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    try:
        if not pdf.is_file() or pdf.is_symlink():
            raise RuntimeError("catalogued PDF is unavailable or is a symlink")
        stat = pdf.stat()
        if stat.st_size <= 0 or stat.st_size > args.max_bytes:
            raise RuntimeError("PDF exceeds the configured preprocessing size limit")
        if _sha256(pdf) != args.content_sha256:
            raise RuntimeError("book content changed after the preprocessing request")
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
                        total, existing if args.engine == "vision" else tokenized
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
                if args.engine == "vision":
                    chars, text, image_w, image_h = _vision_page(page, Path(args.project))
                else:
                    chars, text, image_w, image_h = _manga_page(page, engine)
                sidecar = {
                    "schema": PAGE_SCHEMA,
                    "bookId": args.book_id,
                    "contentSha256": args.content_sha256,
                    "engine": args.engine,
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
                _atomic_json(page_path, sidecar)
                existing += 1
                if args.engine == "vision":
                    tokenized = existing
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

        version_dir = job_dir.parent
        _atomic_json(version_dir / "result.json", {
            "engine": args.engine,
            "textCompletedAtEpochMs": int(time.time() * 1000),
        })
        formula_path = _formula_path(Path(args.project), pdf)
        partial_revision, _partial_manifest = _publish_attachments(
            args, job_dir, formula_path
        )
        partial_result = _load(version_dir / "result.json", {}) or {}
        partial_result["pageCharsRevision"] = partial_revision
        _atomic_json(version_dir / "result.json", partial_result)
        _update_job(
            job_path,
            textState="succeeded",
            resultAvailable=True,
            percent=75,
            phase="formula-detect",
            formulaState="queued",
            message="文字 sidecar 已完成，开始检测公式区域",
            pageCharsRevision=partial_revision,
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
        revision, _manifest = _publish_attachments(args, job_dir, formula_path)
        result = _load(version_dir / "result.json", {}) or {}
        result.update({
            "engine": args.engine,
            "pageCharsRevision": revision,
            "completedAtEpochMs": int(time.time() * 1000),
        })
        _atomic_json(version_dir / "result.json", result)
        total, have = _formula_counts(formula_path)
        _update_job(
            job_path,
            state="succeeded",
            phase="finalizing",
            textState="succeeded",
            formulaState=("succeeded" if have == total else "partial"),
            formulaTotal=total,
            formulaRecognized=have,
            formulaPendingRegions=0,
            formulaFailedRegions=max(0, total - have),
            currentPage=None,
            textProgress=_progress(existing, existing),
            wordProgress=_progress(existing, tokenized),
            formulaProgress=_progress(existing, existing),
            percent=100,
            etaSeconds=0,
            message=f"Pi 预处理完成：文字 {existing} 页，公式 {have}/{total}",
            canPause=False,
            canResume=False,
            canCancel=False,
            canRetry=False,
            resultAvailable=True,
            pageCharsRevision=revision,
        )
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
    parser.add_argument("--max-pages", type=int, default=5000)
    parser.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.tokenize_dir:
        return _tokenize_directory(
            Path(args.tokenize_dir),
            Path(args.job_path) if args.job_path else None,
            Path(args.control_path) if args.control_path else None,
        )
    required = (args.job_dir, args.pdf, args.project, args.book_id, args.content_sha256, args.engine)
    if not all(required):
        raise SystemExit("worker arguments are required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
