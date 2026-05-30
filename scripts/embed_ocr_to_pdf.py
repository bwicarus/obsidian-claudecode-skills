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

import unicodedata

import fitz  # PyMuPDF
import numpy as np


def _is_cjk_punct_or_bullet(c: str) -> bool:
    """字符是否为强装饰圆形 bullet(嵌入这些已占视觉 bullet 位置,不需要额外 offset)。
    收紧到只含厚实 bullet 形状 —— `『「【〔・` 等细括号/中点都不在此列,因为
    mokuro 经常把视觉 `●` 误识别为它们(`『マルウェア対策` 实为 `●`,`・不正ログイン` 实为 `●`)。
    真 `『「【〔` 等括号细窄,detector 用 blob 几何会判 False 不会误偏移。"""
    return c in "●◆■▲▶" or (c and ord(c) in (0x25CF, 0x25C6, 0x25A0, 0x25B2, 0x25B6))


def _segment_line_chars(patch_gray: "np.ndarray", line_h: int) -> list[tuple[int, int]]:
    """vertical column projection 分出 line 内每个 visual 字符的 (x0, x1) bbox。
    CJK 字间通常 1-3px 空白可分 → 合并 close-by gaps (< 15% line_h) 防止字内笔画散开。
    返回 list of (x0, x1) in patch-local image coords."""
    binary = patch_gray < 100
    col_sum = binary.sum(axis=0)
    segs: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for x in range(len(col_sum)):
        if col_sum[x] > 0:
            if not in_run:
                start = x
                in_run = True
        else:
            if in_run:
                if x - start >= 3:
                    segs.append((start, x))
                in_run = False
    if in_run:
        segs.append((start, len(col_sum)))
    # 合并同字内白(gap < 15% line_h)
    merged: list[tuple[int, int]] = []
    for s, e in segs:
        if merged and s - merged[-1][1] < line_h * 0.15:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def _seg_is_bullet(patch_gray: "np.ndarray", seg: tuple[int, int], line_h: int) -> bool:
    """判断 segmentation 出来的某 seg 是不是 bullet (实心圆/方块)还是普通文字。
    bullet: blob 高度 < 70% line_h, 宽高比 0.6-1.7(近正方形)。
    标题文字粗体也 dark 重,但 blob 高度 ≈ line_h,会 fail 高度 check。"""
    if seg[1] - seg[0] < 3:
        return False
    blob_patch = patch_gray[:, seg[0]:seg[1]]
    dark = blob_patch < 100
    if int(dark.sum()) < 20:
        return False
    y_idx = np.where(dark.any(axis=1))[0]
    if len(y_idx) == 0:
        return False
    bh = int(y_idx.max() - y_idx.min()) + 1
    if bh >= line_h * 0.70:
        return False
    seg_w = seg[1] - seg[0]
    ratio = seg_w / max(1, bh)
    return 0.6 <= ratio <= 1.7

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATE_DIR = PROJECT / "state" / "mokuro-ocr"


def pdf_sha(pdf_path: Path) -> str:
    return hashlib.sha1(str(pdf_path.resolve()).encode()).hexdigest()[:16]


def embed_page(page: fitz.Page, sidecar: dict, sx: float, sy: float,
               image: "np.ndarray | None" = None) -> int:
    """对一页嵌入文字层。返回插入字符数。

    image: 该 page 的原始扫描图(image 坐标系,跟 sidecar 的 img_width/height 同),
    用于检测每 line 左侧是否有 bullet(●)做 per-line offset 修正。
    None 时跳过修正(行为同之前)。"""
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
            # mokuro 输出全角数字/标点(如 "１１．３")，跟图像印刷半角不一致 → NFKC 还原
            text = unicodedata.normalize("NFKC", text)
            # ASCII↔CJK 切换边界手动插空格(mokuro 不加,但图像里有视觉间距):
            # 比如 "11.3セキュリティ対策" → "11.3 セキュリティ対策"。
            # 空格不嵌字符但占 weight 位置 → ASCII 字符 bbox 不延伸到下个 CJK 字符位置,
            # 防止用户拖选 CJK 时误选末尾 ASCII。
            def _is_asc(c: str) -> bool:
                return c.isascii() and not c.isspace()
            new_text = []
            prev_asc = None
            for c in text:
                cur_asc = _is_asc(c)
                if new_text and prev_asc is not None and prev_asc != cur_asc:
                    new_text.append(" ")
                new_text.append(c)
                prev_asc = cur_asc
            text = "".join(new_text)
            # 字符宽度权重:CJK 假名/汉字 = 1.0,ASCII/标点/空格 = 0.5(图像里半宽印刷)
            def _w(c: str) -> float:
                return 0.5 if (c.isascii() or c.isspace() or ord(c) <= 0xFF) else 1.0
            weights = [_w(c) for c in text]
            total_w = sum(weights) or 1.0
            xs = [pt[0] for pt in coords]
            ys = [pt[1] for pt in coords]
            x1_img = min(xs); x2_img = max(xs)
            y1_img = min(ys); y2_img = max(ys)
            # 用 visual char segmentation 精确定位 OCR text 起点(根本解,替代之前的 detector + 短路 list)
            # 1) 分割 visual 行内每字 bbox
            # 2) 判断第一段是不是 bullet (blob h<70% line_h + 宽高比≈1)
            # 3) 若 visual 有 bullet 但 OCR text 不是 strong bullet 字符 → 跳过 visual 第一段,
            #    OCR text 起点对齐到 visual 第二段(mokuro 漏识别/误识别 bullet 的情况)
            # 4) 其它情况:OCR text 起点对齐到 visual 第一段(覆盖正常情况 + OCR 起 bullet 正确)
            if image is not None:
                py0 = max(0, int(y1_img)); py1 = min(image.shape[0], int(y2_img))
                px0 = max(0, int(x1_img)); px1 = min(image.shape[1], int(x2_img))
                if px1 > px0 + 5 and py1 > py0 + 5:
                    patch = image[py0:py1, px0:px1]
                    if patch.ndim == 3:
                        patch = patch.mean(axis=2)
                    segs = _segment_line_chars(patch, py1 - py0)
                    if segs:
                        text_first = text[0] if text else ""
                        first_is_strong_bullet_text = _is_cjk_punct_or_bullet(text_first)
                        first_seg_is_bullet = _seg_is_bullet(patch, segs[0], py1 - py0)
                        if first_seg_is_bullet and not first_is_strong_bullet_text:
                            # mokuro 漏识 / 误识 bullet → OCR text 起点对齐 visual 第二段
                            actual_start_x = px0 + (segs[1][0] if len(segs) > 1 else segs[0][1])
                        else:
                            # OCR 跟 visual 第一段对齐(正常 / OCR 起 bullet 准确)
                            actual_start_x = px0 + segs[0][0]
                        x1_img = float(actual_start_x)
            x1 = x1_img * sx
            y1 = y1_img * sy
            x2 = x2_img * sx
            y2 = y2_img * sy
            line_w = x2 - x1
            line_h = y2 - y1
            if not text:
                continue
            # fs 同时受 line_h 和 char_w_cjk 限制:
            # 1) line_h*0.95 让 char bbox 高度填满行高(纵向选中准)
            # 2) char_w_cjk = line_w/total_w 让 char bbox 宽不超过单字符 spacing
            #    (大字号标题 line_h > char_w 时,fs 取 char_w 防 ASCII bbox 越界到下个字符)
            char_w_cjk = line_w / total_w  # CJK 全宽字符的间距(weight=1.0 对应宽度)
            fs = max(4.0, min(80.0, line_h * 0.95, char_w_cjk))
            # mokuro detector 输出 line bbox 比 visual text 起点偏左 ~ 2-4 image px,
            # user 实测反馈 v11(5%)还左偏 → 加大到 char_w * 10% 的右偏移修正
            x1 += char_w_cjk * 0.10
            baseline = y2 - line_h * 0.10
            pos_acc = 0.0
            # 统一 'japan' 字体(切 'helv' 会触发 PyMuPDF reflow 插空格污染整 page text)
            # ASCII 字符 char bbox 宽仍 ~fs(japan 是全宽 metric),但 position 按 weight 紧排
            # 相邻 ASCII chars 的 bbox 会重叠,但 selection 准确(PyMuPDF 不插空格因为 japan 全宽 advance)
            for ci, c in enumerate(text):
                w = weights[ci]
                x = x1 + (pos_acc / total_w) * line_w
                pos_acc += w
                if c.isspace():
                    continue
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
