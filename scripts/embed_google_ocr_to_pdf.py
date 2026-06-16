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
    """对一页嵌入 char-level 文字层。

    OCR bbox 在「视觉(渲染后)」坐标系(get_pixmap 已应用 /Rotate)。但 page.insert_text
    用的是 mediabox(未旋转)坐标系,旋转页(如扫描书常见 /Rotate 90)直接喂视觉坐标会让
    落在 mediabox 外的字被裁掉(实测某 90° 页 107 字只剩 35,中下部全丢→"选不中")。
    用 page.derotation_matrix 把视觉点转回 mediabox 点,并 rotate=page.rotation 让字形朝向
    跟视觉一致(读回 bbox 才对得上选中层)。rotation=0 时 derotation 是单位阵、rotate=0,
    对所有非旋转书完全无变化(向后兼容)。"""
    chars = sidecar.get("chars") or []
    derot = page.derotation_matrix
    rot = page.rotation
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
        pt = fitz.Point(x_pdf, baseline_pdf) * derot   # 视觉坐标 → mediabox 坐标(旋转页关键)
        page.insert_text(
            pt,
            c,
            fontname="japan",
            fontsize=fs_pdf,
            rotate=rot,
            color=(0, 0, 0),
            fill=(0, 0, 0),
            render_mode=0,
            fill_opacity=0,
            stroke_opacity=0,
        )
        n += 1
    return n


def _write_progress(path, done, total, phase):
    """嵌入阶段进度落盘,供 preprocess_book 轮询(否则编排进度条整段嵌入冻在 94% 看着像卡死)。"""
    if not path:
        return
    try:
        Path(path).write_text(json.dumps({"done": done, "total": total, "phase": phase}), "utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--sidecar", default=None)
    ap.add_argument("--progress", default=None, help="进度文件路径(写 {done,total,phase});嵌入阶段供编排轮询")
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
        if (i + 1) % 20 == 0:
            _write_progress(args.progress, i + 1, n_pages, "embed")
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{n_pages}] {total_chars} chars  {time.time()-t0:.1f}s", flush=True)

    print(f"嵌入 {total_chars} chars  保存 → {out_path}", flush=True)
    _write_progress(args.progress, n_pages, n_pages, "save")   # 大书 save 也耗时,标记保存中
    # ⚠ garbage=4(流去重)在大书(重建栅格化后 100+MB / 几百张图流)上病态慢——实测费曼(588 页
    # 151MB)嵌入循环 46min 跑完后,save(garbage=4) 在 Pi 上磨 2.5h+ 没完。改 garbage=1(只清无引用
    # 对象,不做 O(n) 流去重),输出几乎一样大但秒级保存。deflate 只压未压缩流(图本是 DCT 不动)。
    src.save(str(out_path), garbage=1, deflate=True)
    src.close()
    _write_progress(args.progress, n_pages, n_pages, "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
