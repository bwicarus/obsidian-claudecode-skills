#!/usr/bin/env python3
"""把 mokuro 输出的 sidecar JSON 嵌入 PDF 当不可见文字层。

设计要点(经过 5 轮 demo 验证):
- 逐字符 page.insert_text 而不是 insert_textbox:CJK 方块字 insert_textbox 在
  line_w / N 紧时会被 PyMuPDF 缩字号导致塞左上一小撮。
- 字号 = line_h * 0.95:让 char bbox 高度填满 mokuro 行高(实测覆盖完整且略大)
- 横向 char_w = line_w / len(text) 均分,空格留位置不 insert:文字层位置跟图对齐
- baseline = y2 - line_h * 0.10:descender 留 10% 空间
- render_mode=0 + fill_opacity=0 + stroke_opacity=0:不显示但可选可搜索(比
  render_mode=3 invisible 兼容性好,iOS Files/Safari 等也认)
- fontname='japan' PyMuPDF 内置 CJK 字体

用法:
  python embed_ocr_to_pdf.py --pdf <原PDF> [--out <输出>] [--sidecar <目录>]
  默认 sidecar 在 state/mokuro-ocr/<sha-of-pdf>/,输出 <pdf>-ocr.pdf
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
from pathlib import Path

import fitz  # PyMuPDF

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATE_DIR = PROJECT / "state" / "mokuro-ocr"


def pdf_sha(pdf_path: Path) -> str:
    return hashlib.sha1(str(pdf_path.resolve()).encode()).hexdigest()[:16]


def embed_page(page: fitz.Page, sidecar: dict, sx: float, sy: float) -> int:
    """对一页嵌入文字层。返回插入字符数。"""
    n_chars = 0
    for b in sidecar.get("blocks") or []:
        lines = b.get("lines") or []
        line_coords = b.get("lines_coords") or []
        for li, text in enumerate(lines):
            if li >= len(line_coords):
                continue
            coords = line_coords[li]
            if not coords or not text.strip():
                continue
            xs = [pt[0] for pt in coords]
            ys = [pt[1] for pt in coords]
            x1 = min(xs) * sx
            y1 = min(ys) * sy
            x2 = max(xs) * sx
            y2 = max(ys) * sy
            line_w = x2 - x1
            line_h = y2 - y1
            n = len(text)  # 含空格,空格只跳过 insert
            if n == 0:
                continue
            fs = max(4.0, min(80.0, line_h * 0.95))
            char_w = line_w / n
            baseline = y2 - line_h * 0.10
            for ci, c in enumerate(text):
                if c.isspace():
                    continue
                x = x1 + ci * char_w
                page.insert_text(
                    fitz.Point(x, baseline),
                    c,
                    fontname="japan",
                    fontsize=fs,
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                    render_mode=0,
                    fill_opacity=0,
                    stroke_opacity=0,
                )
                n_chars += 1
    return n_chars


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default=None, help="默认 <pdf>-ocr.pdf")
    ap.add_argument("--sidecar", default=None,
                    help="默认 state/mokuro-ocr/<sha-of-pdf>/")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2
    sidecar_dir = (
        Path(args.sidecar) if args.sidecar else STATE_DIR / pdf_sha(pdf_path)
    )
    out_path = (
        Path(args.out)
        if args.out
        else pdf_path.parent / f"{pdf_path.stem}-ocr.pdf"
    )

    src = fitz.open(str(pdf_path))
    n_pages = len(src)
    print(f"PDF: {pdf_path.name}  pages: {n_pages}", flush=True)
    print(f"sidecar: {sidecar_dir}", flush=True)
    print(f"out: {out_path}", flush=True)

    done = {}
    for f in glob.glob(str(sidecar_dir / "p*.json")):
        m = re.match(r"p(\d+)\.json$", Path(f).name)
        if m:
            done[int(m.group(1))] = f
    print(f"sidecar 已完成: {len(done)}/{n_pages}", flush=True)
    if len(done) < n_pages:
        missing = [i for i in range(n_pages) if i not in done]
        print(f"⚠ 缺 {len(missing)} 页 OCR(从 {missing[0]+1} 开始)", flush=True)

    t0 = time.time()
    total_chars = 0
    for i in range(n_pages):
        sc_path = done.get(i)
        if not sc_path:
            continue
        try:
            sc = json.loads(Path(sc_path).read_text(encoding="utf-8"))
        except Exception as ex:
            print(f"  page {i+1}: sidecar 读失败 {ex}", flush=True)
            continue
        if "error" in sc:
            continue  # OCR 这页失败,跳过

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
            print(
                f"  [{i+1}/{n_pages}] 累计 {total_chars} chars  "
                f"耗时 {time.time()-t0:.1f}s",
                flush=True,
            )

    print(f"嵌入总字符: {total_chars}  耗时 {time.time()-t0:.1f}s", flush=True)
    print(f"保存 → {out_path}", flush=True)
    src.save(str(out_path), garbage=4, deflate=True)
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
