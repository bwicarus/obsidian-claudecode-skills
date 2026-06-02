#!/usr/bin/env python3
"""Google Cloud Vision API OCR (DOCUMENT_TEXT_DETECTION) 跑 PDF 每页。

特性 vs mokuro:
- char-level bbox 像素级精度 → 嵌入文字层不再需要 segmentation 推断
- API ~1.5-3s/页,比 mokuro CPU 60-100s/页 快 30-60 倍
- 准确率 95%+ for 印刷日文(实测 ≥ mokuro)
- 配额:每月前 1000 units 免费(够 1 本 679 页书)

Sidecar 格式: state/google-vision-ocr/<sha-of-pdf>/p<num>.json
{
    "img_width": int, "img_height": int,
    "chars": [{"c": "プ", "bbox": [x0, y0, x1, y1]}, ...]
}

CLI: python google_vision_ocr.py --pdf <PDF>
环境变量: GOOGLE_VISION_API_KEY 或 /home/bwicarus/.config/gcp-vision-key
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import fitz
import requests

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATE_DIR = PROJECT / "state" / "google-vision-ocr"
KEY_FILE = Path("/home/bwicarus/.config/gcp-vision-key")


def _load_key() -> str:
    k = os.environ.get("GOOGLE_VISION_API_KEY")
    if k:
        return k.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    raise SystemExit("缺 GOOGLE_VISION_API_KEY env 或 /home/bwicarus/.config/gcp-vision-key")


def pdf_sha(pdf_path: Path) -> str:
    return hashlib.sha1(str(pdf_path.resolve()).encode()).hexdigest()[:16]


def ocr_one_page(api_key: str, png_bytes: bytes) -> dict:
    """对一张图(PNG/JPEG)调 Vision API,提取 char-level (text + bbox)。
    瞬时网络/SSL 抖动(SSLEOFError 等)重试 3 次,避免单页因一次抖动永久缺 OCR。"""
    payload = {
        "requests": [{
            "image": {"content": base64.b64encode(png_bytes).decode()},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            "imageContext": {"languageHints": ["ja"]},
        }]
    }
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError) as ex:
            last_exc = ex
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    else:
        raise last_exc
    # 配额计数(Vision API 每张图 1 unit,免费 1000/月)
    try:
        import sys as _sys, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent))
        from google_api_quota import log_usage as _logq
        _logq("vision", 1, "images:annotate",
              note=f"DOCUMENT_TEXT_DETECTION status={resp.status_code}")
    except Exception:
        pass
    resp.raise_for_status()
    data = resp.json()
    r0 = data.get("responses", [{}])[0]
    if "error" in r0:
        raise RuntimeError(f"Vision API: {r0['error']}")
    full = r0.get("fullTextAnnotation")
    if not full:
        return {"chars": [], "text": ""}
    chars = []
    for page in full.get("pages", []):
        for block in page.get("blocks", []):
            for para in block.get("paragraphs", []):
                for word in para.get("words", []):
                    for sym in word.get("symbols", []):
                        bb = sym.get("boundingBox", {}).get("vertices", [])
                        if not bb:
                            continue
                        xs = [v.get("x", 0) for v in bb]
                        ys = [v.get("y", 0) for v in bb]
                        chars.append({
                            "c": sym.get("text", ""),
                            "bbox": [min(xs), min(ys), max(xs), max(ys)],
                        })
                        # detectedBreak:WORD-end SPACE / EOL / SURE_SPACE / LINE_BREAK
                        brk = sym.get("property", {}).get("detectedBreak", {})
                        if brk.get("type") in ("SPACE", "SURE_SPACE", "EOL_SURE_SPACE"):
                            chars.append({"c": " ", "bbox": chars[-1]["bbox"], "sp": 1})
                        elif brk.get("type") in ("LINE_BREAK",):
                            chars.append({"c": "\n", "bbox": chars[-1]["bbox"], "sp": 1})
    return {"chars": chars, "text": full.get("text", "")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--pages", default=None,
                    help="如 '10' 单页, '10,80,200' 多页, '10-15' 范围;不指定 = 全本")
    ap.add_argument("--workers", type=int, default=10,
                    help="并发 worker 数(API 调用 IO-bound,10-20 倍效提速)")
    ap.add_argument("--max-long-side", type=int, default=4000,
                    help="渲染长边像素上限。巨幅扫描页按 DPI 会出几十 MB 图把 Vision "
                         "请求体撑爆(SSL 超时)/把内存吃光,封顶后等比缩小")
    ap.add_argument("--jpeg-quality", type=int, default=90,
                    help="上传用 JPEG 质量。JPEG 比 PNG 小一个量级,OCR 质量无可感知损失")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2
    api_key = _load_key()
    sha = pdf_sha(pdf_path)
    work = STATE_DIR / sha
    work.mkdir(parents=True, exist_ok=True)
    progress_path = work / "progress.json"

    doc = fitz.open(str(pdf_path))
    n = len(doc)
    print(f"PDF: {pdf_path.name}  共 {n} 页  workers={args.workers}", flush=True)

    if args.pages:
        pages = set()
        for tok in args.pages.split(","):
            tok = tok.strip()
            if "-" in tok:
                lo, hi = tok.split("-")
                pages.update(range(int(lo) - 1, int(hi)))
            else:
                pages.add(int(tok) - 1)
        page_list = sorted(p for p in pages if 0 <= p < n)
    else:
        page_list = list(range(n))

    def _page_done(i: int) -> bool:
        """已成功 OCR 才算完成；上次报错的页(写了带 error 的 json)要重跑。"""
        jf = work / f"p{i:04d}.json"
        if not jf.exists():
            return False
        try:
            sc = json.loads(jf.read_text("utf-8"))
        except Exception:
            return False
        return not sc.get("error")

    todo = [i for i in page_list if not _page_done(i)]
    print(f"  需 OCR: {len(todo)}/{len(page_list)}(已完成 {len(page_list) - len(todo)})", flush=True)

    if not todo:
        print("全部已完成,无需 OCR", flush=True)
        return 0

    # 并发:fitz Doc 不 thread-safe → 主线程串行 render PNG,workers 跑 API call + 写 sidecar
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    write_lock = threading.Lock()
    state = {"completed": 0}
    pdf_path_str = str(pdf_path)
    dpi = args.dpi
    max_long = max(256, args.max_long_side)
    jpg_q = max(40, min(100, args.jpeg_quality))

    def worker(page_idx: int):
        """每 worker 自己 fitz.open + render + API call(避免主线程 render 成 bottleneck)。
        fitz 多实例 open 同一 PDF 是 thread-safe(各自独立 file handle)。"""
        try:
            doc_local = fitz.open(pdf_path_str)
            try:
                page = doc_local[page_idx]
                # 自适应缩放:按 DPI 算,但长边封顶 max_long。巨幅扫描页(本例页面
                # 原生 2227×3242pt,300dpi → 9280×13509px / 49MB PNG)会把 Vision
                # 请求体撑爆(SSL 上传超时重试耗尽)且 10 worker 同时渲染会 OOM。
                long_pt = max(page.rect.width, page.rect.height) or 1.0
                zoom = min(dpi / 72.0, max_long / long_pt)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                # 上传用 JPEG:扫描照片页 JPEG 比 PNG 小一个量级(49MB→~1MB),
                # OCR 文字识别对 q90 JPEG 无可感知损失。
                img_bytes = pix.tobytes("jpg", jpg_quality=jpg_q)
                pix_w, pix_h = pix.width, pix.height
                del pix
            finally:
                doc_local.close()
            t = time.time()
            result = ocr_one_page(api_key, img_bytes)
            dt = time.time() - t
            err = None
        except Exception as ex:
            result = None; dt = 0
            err = f"{type(ex).__name__}: {ex}"
            pix_w = pix_h = 0
        out = result if result else {"error": err}
        out["_page"] = page_idx
        out["_seconds"] = round(dt, 2)
        out["img_width"] = pix_w
        out["img_height"] = pix_h
        (work / f"p{page_idx:04d}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8"
        )
        return page_idx, dt

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(worker, i) for i in todo]

        for fut in as_completed(futures):
            page_idx, dt = fut.result()
            with write_lock:
                state["completed"] += 1
                k = state["completed"]
                avg = (time.time() - t0) / k
                eta = (len(todo) - k) * avg
                progress = {
                    "completed": k,
                    "total": len(todo),
                    "last_page": page_idx + 1,
                    "last_seconds": round(dt, 2),
                    "avg_seconds": round(avg, 2),
                    "eta_minutes": round(eta / 60, 1),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                progress_path.write_text(
                    json.dumps(progress, ensure_ascii=False, indent=2), "utf-8"
                )
                if k % 20 == 0 or k == len(todo):
                    print(f"  [{k}/{len(todo)}] p{page_idx+1} {dt:.1f}s "
                          f"| avg {avg:.1f}s | ETA {eta/60:.1f}min", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] 完成,总耗时 {time.time()-t0:.1f}s "
          f"({(time.time()-t0)/len(todo):.2f}s/页 平均)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
