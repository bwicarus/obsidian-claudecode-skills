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
    """对一张 PNG 调 Vision API,提取 char-level (text + bbox)。"""
    resp = requests.post(
        f"https://vision.googleapis.com/v1/images:annotate?key={api_key}",
        json={
            "requests": [{
                "image": {"content": base64.b64encode(png_bytes).decode()},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["ja"]},
            }]
        },
        timeout=60,
    )
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

    todo = [i for i in page_list if not (work / f"p{i:04d}.json").exists()]
    print(f"  需 OCR: {len(todo)}/{len(page_list)}(已完成 {len(page_list) - len(todo)})", flush=True)

    if not todo:
        print("全部已完成,无需 OCR", flush=True)
        return 0

    # 并发:fitz Doc 不 thread-safe → 主线程串行 render PNG,workers 跑 API call + 写 sidecar
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    sem = threading.BoundedSemaphore(args.workers * 2)   # in-flight 限制(防 png buffer 爆内存)
    write_lock = threading.Lock()                          # progress.json 写互斥
    state = {"completed": 0}

    def worker(page_idx: int, png_bytes: bytes, pix_w: int, pix_h: int):
        try:
            t = time.time()
            result = ocr_one_page(api_key, png_bytes)
            dt = time.time() - t
            err = None
        except Exception as ex:
            result = None; dt = 0
            err = f"{type(ex).__name__}: {ex}"
        out = result if result else {"error": err}
        out["_page"] = page_idx
        out["_seconds"] = round(dt, 2)
        out["img_width"] = pix_w
        out["img_height"] = pix_h
        (work / f"p{page_idx:04d}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8"
        )
        return page_idx, dt

    def worker_release(*a):
        try:
            return worker(*a)
        finally:
            sem.release()

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        # submit pipeline: 主线程 render,worker 处理
        futures = []
        for i in todo:
            sem.acquire()
            page = doc[i]
            pix = page.get_pixmap(dpi=args.dpi)
            png = pix.tobytes("png")
            futures.append(ex.submit(worker_release, i, png, pix.width, pix.height))
            del pix, png

        # 收 results + 刷 progress
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
