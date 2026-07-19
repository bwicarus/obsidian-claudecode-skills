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


_LINE_NOISE = set("|│丨︱‖∥┃┆┇┊┋╎╏!ⅰ")   # OCR 把插图边框/表格线认成的"字符"
_KANA_RE = re.compile(r"^[ぁ-んァ-ヶー]$")


def clean_chars(chars: list) -> tuple[list, int, int]:
    """**文字层生成时的整体清洁**(用户拍板 2026-07-19:噪声在源头剔,别逐个消费端补)。
    与阅读器消费端同一套规格(pdf_reader._strip_graphic_noise + ruby 跳过):
      ① 线条噪声:插图边框/表格线被 OCR 认成 |│丨 串(实测料理师 p46 头像框 → 6 根竖线,
         y 远离正文、高仅正文 14%,把断句的段落判定炸穿);字符∈线条集 且 高<正文中位×0.45 → 剔。
      ② 振假名:ruby 小字混进正文流(「宮廷料理人いいんほんぞうがくだった伊尹」),
         假名 且 高<正文中位×0.60 → 剔(注音展示走 furigana sidecar,文字层里只是污染)。
    CJK 页才启用(拉丁/代码页 | 是真字符);返回 (干净chars, 剔线条数, 剔ruby数)。"""
    hs = sorted((c["bbox"][3] - c["bbox"][1]) for c in chars
                if c.get("c", "").strip() and c.get("bbox") and len(c["bbox"]) == 4)
    if not hs:
        return chars, 0, 0
    med = hs[len(hs) // 2]
    n_cjk = sum(1 for c in chars if re.search(r"[぀-ヿ㐀-鿿]", c.get("c", "") or ""))
    if n_cjk < max(10, len(chars) * 0.2):
        return chars, 0, 0
    out, n_line, n_ruby = [], 0, 0
    for c in chars:
        ch, bb = c.get("c", ""), c.get("bbox")
        h = (bb[3] - bb[1]) if bb and len(bb) == 4 else med
        if ch in _LINE_NOISE and h < med * 0.45:
            n_line += 1
            continue
        if _KANA_RE.match(ch) and h < med * 0.60:
            n_ruby += 1
            continue
        out.append(c)
    return out, n_line, n_ruby


def strip_text_layer(page: fitz.Page) -> None:
    """删掉本页旧文字层(重嵌前用;只对**有 sidecar** 的页调用——用户插入页无 sidecar,
    绝不会被碰)。redact 全页但保图像:扫描书正文都在图里,文字全是此前 embed 的 OCR 层。"""
    page.add_redact_annot(page.rect)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)


def embed_page(page: fitz.Page, sidecar: dict, sx: float, sy: float) -> int:
    """对一页嵌入 char-level 文字层。

    OCR bbox 在「视觉(渲染后)」坐标系(get_pixmap 已应用 /Rotate)。但 page.insert_text
    用的是 mediabox(未旋转)坐标系,旋转页(如扫描书常见 /Rotate 90)直接喂视觉坐标会让
    落在 mediabox 外的字被裁掉(实测某 90° 页 107 字只剩 35,中下部全丢→"选不中")。
    用 page.derotation_matrix 把视觉点转回 mediabox 点,并 rotate=page.rotation 让字形朝向
    跟视觉一致(读回 bbox 才对得上选中层)。rotation=0 时 derotation 是单位阵、rotate=0,
    对所有非旋转书完全无变化(向后兼容)。"""
    chars = sidecar.get("chars") or []
    chars, _nl, _nr = clean_chars(chars)   # 源头清洁:线条噪声+ruby 不进文字层
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
            # china-s 是 pan-CJK 超集:实测 insert_text+读回,简体/繁体/假名/和制汉字/共用全覆盖、一个不丢;
            # 原写死 "japan"(为日语书做)对简体专用字(费/查/纽/约/获/战/间…)无字形 → 静默丢字 → 选字层缺字、
            # "浮层近一半盖不全"。用 china-s 中日繁通吃,日语书也不退化(假名+和制汉字都在)。
            fontname="china-s",
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
    ap.add_argument("--strip-old", action="store_true",
                    help="嵌入前先删该页旧文字层(对已 embed 过的书重嵌用;只动有 sidecar 的页)")
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
        if args.strip_old:
            strip_text_layer(page)         # 重嵌:先删旧文字层(叠两层=选中/搜索双份乱码)
            page = src[i]                  # redact 后重新取页对象(apply_redactions 可能重建内容流)
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
