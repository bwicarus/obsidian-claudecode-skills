#!/usr/bin/env python3
"""把 google_vision_ocr.py 输出的 char-level sidecar 嵌入 PDF 当文字层。

Google Vision 直接给每字 bbox,**不需要 segmentation/weight 推断**,完美对齐。
每字独立 insert_text,字号按 bbox 高度算,bbox 宽度由 PyMuPDF font metric 控制。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import fitz

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATE_DIR = PROJECT / "state" / "google-vision-ocr"


def pdf_sha(pdf_path: Path) -> str:
    return hashlib.sha1(str(pdf_path.resolve()).encode()).hexdigest()[:16]


def embed_page(page: fitz.Page, sidecar: dict, sx: float, sy: float) -> int:
    """对一页嵌入 char-level 文字层。"""
    chars = sidecar.get("chars") or []
    n = 0
    for ch in chars:
        c = ch.get("c", "")
        if not c or ch.get("sp"):
            continue
        bb = ch.get("bbox")
        if not bb or len(bb) != 4:
            continue
        x0, y0, x1, y1 = bb
        char_w_img = x1 - x0
        char_h_img = y1 - y0
        if char_w_img <= 0 or char_h_img <= 0:
            continue
        # 字号:用 char bbox 高度(visual char 实际高度)
        # PyMuPDF japan 字体 char bbox 宽 ≈ fs × 0.78,所以 fs = char_w / 0.78 让 bbox 跟 visual char 一致
        fs_pdf = max(4.0, min(80.0, char_w_img * sx / 0.78))
        # baseline:bbox 底部稍上
        baseline_pdf = y1 * sy - char_h_img * sy * 0.10
        x_pdf = x0 * sx
        page.insert_text(
            fitz.Point(x_pdf, baseline_pdf),
            c,
            fontname="japan",
            fontsize=fs_pdf,
            color=(0, 0, 0),
            fill=(0, 0, 0),
            render_mode=0,
            fill_opacity=0,
            stroke_opacity=0,
        )
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--sidecar", default=None)
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2
    sidecar_dir = Path(args.sidecar) if args.sidecar else STATE_DIR / pdf_sha(pdf_path)
    out_path = Path(args.out) if args.out else pdf_path.parent / f"{pdf_path.stem}-ocr.pdf"

    src = fitz.open(str(pdf_path))
    n_pages = len(src)
    print(f"PDF: {pdf_path.name}  pages: {n_pages}", flush=True)

    done = {}
    for f in glob.glob(str(sidecar_dir / "p*.json")):
        m = re.match(r"p(\d+)\.json$", Path(f).name)
        if m:
            done[int(m.group(1))] = f
    print(f"sidecar 已完成: {len(done)}/{n_pages}", flush=True)

    t0 = time.time()
    total_chars = 0
    for i in range(n_pages):
        sc_path = done.get(i)
        if not sc_path:
            continue
        try:
            sc = json.loads(Path(sc_path).read_text(encoding="utf-8"))
        except Exception:
            continue
        if "error" in sc:
            continue
        img_w = sc.get("img_width")
        img_h = sc.get("img_height")
        if not (img_w and img_h):
            continue
        page = src[i]
        sx = page.rect.width / img_w
        sy = page.rect.height / img_h
        nc = embed_page(page, sc, sx, sy)
        total_chars += nc
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{n_pages}] {total_chars} chars  {time.time()-t0:.1f}s", flush=True)

    print(f"嵌入 {total_chars} chars  保存 → {out_path}", flush=True)
    src.save(str(out_path), garbage=4, deflate=True)
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
