#!/usr/bin/env python3
"""智能压缩 PDF：**只重压图像 XObject**(PIL 降采样+JPEG 重压)，**完全不碰字体/文字层**
→ qpdf 重新线性化(Fast Web View)→ 原地替换。

⚠ **为什么不用 Ghostscript**:gs pdfwrite 会重写/子集化字体,**破坏 OCR 文字层的 ToUnicode CMap**
→ PDF.js 靠字形照常显示,但 PyMuPDF 抽文字得到乱码 → 只能选单字、字典/搜索/振假名全废
(2026-06-02 实测 gs /ebook 压完 part1/part2 文字层全乱)。改用 PyMuPDF 逐图重压:文字/字体
对象一字不动,只换图像流 → ToUnicode 完好,抽字照常。

实测扫描书省可观体积、文字层完整。状态写 state/book-preprocess/<sha>.json(跟预处理共用一套,
前端进度条/刷新恢复/KillMode 防杀全复用);phase=compressing。

用法: python compress_pdf.py --pdf <绝对路径> [--max-px 2400] [--quality 72]
注:压缩是有损图像。原书的 .orig.pdf(真·扫描原图)不动 → 不满意可重跑「增加清晰度」从原图重建。
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fitz
from PIL import Image

ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATUS_DIR = ROOT / "state" / "book-preprocess"


def _sha(p: Path) -> str:
    return hashlib.sha1(str(Path(p).resolve()).encode("utf-8")).hexdigest()[:16]


def _write(sha: str, **kw):
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    kw["updated_at"] = time.time()
    tmp = STATUS_DIR / f"{sha}.json.tmp"
    tmp.write_text(json.dumps(kw, ensure_ascii=False), "utf-8")
    tmp.replace(STATUS_DIR / f"{sha}.json")


def _recompress_image(data: bytes, max_px: int, quality: int):
    """把一张图字节降采样(长边≤max_px)+ JPEG 重压。返回 (新字节, 是否变小)。失败/没变小返回 (None, False)。"""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None, False
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")   # CMYK/带 alpha → RGB(扫描书无透明需求)
    w, h = im.size
    long_side = max(w, h)
    if long_side > max_px:
        scale = max_px / long_side
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BICUBIC)  # BICUBIC 比 LANCZOS 快很多,降采样质量足够
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)   # 不用 optimize(多一遍 Huffman,Pi 上太慢、省的有限)
    out = buf.getvalue()
    return (out, True) if len(out) < len(data) else (None, False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--max-px", type=int, default=2400, help="图像长边上限 px(超出降采样;阅读+缩放够用)")
    ap.add_argument("--quality", type=int, default=72, help="JPEG 重压质量(72≈几乎不影响阅读)")
    a = ap.parse_args()
    pdf = Path(a.pdf)
    sha = _sha(pdf)
    try:
        if not pdf.exists():
            _write(sha, phase="error", error="PDF 不存在", pdf=str(pdf))
            return 1
        before = pdf.stat().st_size
        doc = fitz.open(str(pdf))
        total = doc.page_count
        _write(sha, phase="compressing", percent=2, total=total, completed=0,
               pid=os.getpid(), pdf=str(pdf), error="",
               msg=f"压缩中（重压图像，保留文字层）0/{total} 页…")

        # 逐页重压图像 XObject（文字/字体对象一字不动 → ToUnicode 完好 → 抽字照常）。
        # 同一 xref 可能多页共用 → 用 seen 去重只压一次。
        seen, n_done, n_img = set(), 0, 0
        for pno in range(total):
            page = doc[pno]
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    base = doc.extract_image(xref)
                except Exception:
                    continue
                if not base or not base.get("image"):
                    continue
                new, ok = _recompress_image(base["image"], a.max_px, a.quality)
                if ok and new:
                    try:
                        page.replace_image(xref, stream=new)
                        n_img += 1
                    except Exception:
                        pass
            n_done += 1
            if n_done % 3 == 0 or n_done == total:
                _write(sha, phase="compressing", total=total, completed=n_done,
                       percent=2 + int(n_done * 86 / max(1, total)),
                       pid=os.getpid(), pdf=str(pdf),
                       msg=f"压缩中（重压图像，保留文字层）{n_done}/{total} 页…")

        out = STATUS_DIR / f"{sha}.compressed.pdf"
        out.unlink(missing_ok=True)
        _write(sha, phase="compressing", percent=90, total=total, completed=total,
               pid=os.getpid(), pdf=str(pdf), msg=f"保存（已重压 {n_img} 张图）…")
        # garbage=1 只丢被 replace_image 弃用的旧大图流(够回收空间)；**不**用 garbage=4(全量去重,
        # 对 100MB+/几百页文档极慢,实测卡 6min+ 的真凶是它,不是 deflate)。deflate=True 压缩
        # 文字层/内容流——**不能省**:大书(679 页)文字层流巨大,不 deflate 会膨胀到比原文件还大
        # (2026-06 踩:省了 deflate → 252MB 压成 294MB)。JPEG 图是 DCTDecode,deflate 自动跳过,不亏。
        doc.save(str(out), garbage=1, deflate=True)
        doc.close()
        if not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            _write(sha, phase="error", error="保存失败（原书未改动）")
            return 1

        # 重新线性化(Fast Web View)
        if shutil.which("qpdf"):
            _write(sha, phase="compressing", percent=93, total=total, completed=total,
                   pid=os.getpid(), pdf=str(pdf), msg="线性化(Fast Web View)…")
            lin = STATUS_DIR / f"{sha}.clin.pdf"
            try:
                # --object-streams=generate 把海量对象塞进压缩对象流(大书省一截);--compress-streams=y 兜底
                rc = subprocess.run(["qpdf", "--linearize", "--object-streams=generate",
                                     "--compress-streams=y", str(out), str(lin)],
                                    capture_output=True, timeout=600).returncode
                if rc in (0, 3) and lin.exists():
                    out.unlink(missing_ok=True)
                    out = lin
                else:
                    lin.unlink(missing_ok=True)
            except Exception:
                pass

        after = out.stat().st_size
        # **变大就别替换**(彻底杜绝"越压越大"):图已是最优/B&W CCITT 之类 PIL 压不动时,
        # 重存反而可能略大 → 保持原文件不动,如实告知。
        if after >= before:
            out.unlink(missing_ok=True)
            _write(sha, phase="done", percent=100, has_text=True, total=total, completed=total,
                   pdf=str(pdf),
                   msg=f"已是最优：重压后 {after/1048576:.0f}MB ≥ 原 {before/1048576:.0f}MB，保持原文件不变")
            return 0
        shutil.move(str(out), str(pdf))   # 原地替换(.orig.pdf 真·扫描原图不动,可据此重建)
        pct = round(after / max(1, before) * 100)
        _write(sha, phase="done", percent=100, has_text=True, total=total, completed=total,
               pdf=str(pdf),
               msg=f"压缩完成：{before/1048576:.0f}MB → {after/1048576:.0f}MB（{pct}%，省 {100-pct}%），文字层保留")
        return 0
    except Exception as e:
        _write(sha, phase="error", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
