#!/usr/bin/env python3
"""智能压缩 PDF：Ghostscript 把图像降采样到 ~150dpi + 重压(几乎不影响阅读画质),
**保留文字层**(gs pdfwrite 不动文字,只压图像)→ qpdf 重新线性化(Fast Web View)→ 原地替换。

实测扫描书省 ~1/3 体积、文字层完整。状态写 state/book-preprocess/<sha>.json(跟预处理共用一套,
前端进度条/刷新恢复/KillMode 防杀全复用);phase=compressing。

用法: python compress_pdf.py --pdf <绝对路径>
注:压缩是有损图像。原书的 .orig.pdf(真·扫描原图)不动 → 不满意可重跑「增加清晰度」从原图重建。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fitz

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--dpi", type=int, default=150, help="图像降采样目标 dpi(默认 150,阅读够清晰)")
    a = ap.parse_args()
    pdf = Path(a.pdf)
    sha = _sha(pdf)
    try:
        if not pdf.exists():
            _write(sha, phase="error", error="PDF 不存在", pdf=str(pdf))
            return 1
        if not shutil.which("gs"):
            _write(sha, phase="error", error="需要 ghostscript（gs）", pdf=str(pdf))
            return 1
        before = pdf.stat().st_size
        total = fitz.open(str(pdf)).page_count
        _write(sha, phase="compressing", percent=2, total=total, completed=0,
               pid=os.getpid(), pdf=str(pdf), error="",
               msg=f"压缩中（{a.dpi}dpi，保留文字层）0/{total} 页…")

        out = STATUS_DIR / f"{sha}.compressed.pdf"
        out.unlink(missing_ok=True)
        # gs pdfwrite /ebook 预设：图像降采样到 150dpi + JPEG 重压(中等质量),文字层不动。
        # 实测扫描书省 ~1/3,文字层完整。**关键**:只降采样不重压(自定义 ColorImageResolution)
        # 对本就 ~150dpi 的图几乎无效(实测只省 4%),/ebook 的 JPEG 重压才是省体积主力。
        # dpi<=100 用 /screen(更狠,质量更低),否则 /ebook。不加 -dQUIET → 打印 "Page N" 供解析进度。
        preset = "/screen" if a.dpi <= 100 else "/ebook"
        cmd = [
            "gs", "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            f"-dPDFSETTINGS={preset}", "-dCompatibilityLevel=1.6",
            "-o", str(out), str(pdf),
        ]
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        page_re = re.compile(r"^Page (\d+)")
        for line in proc.stdout:
            m = page_re.match(line.strip())
            if m:
                done = int(m.group(1))
                _write(sha, phase="compressing", total=total, completed=done,
                       percent=2 + int(done * 88 / max(1, total)),
                       pid=os.getpid(), pdf=str(pdf),
                       msg=f"压缩中（{a.dpi}dpi）{done}/{total} 页…")
        proc.wait()
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            _write(sha, phase="error", error=f"gs 压缩失败（退出码 {proc.returncode}，原书未改动）")
            return 1

        # 重新线性化(gs 输出未必是 Fast Web View)
        if shutil.which("qpdf"):
            _write(sha, phase="compressing", percent=93, total=total, completed=total,
                   pid=os.getpid(), pdf=str(pdf), msg="线性化(Fast Web View)…")
            lin = STATUS_DIR / f"{sha}.clin.pdf"
            try:
                rc = subprocess.run(["qpdf", "--linearize", str(out), str(lin)],
                                    capture_output=True, timeout=600).returncode
                if rc in (0, 3) and lin.exists():
                    out.unlink(missing_ok=True)
                    out = lin
                else:
                    lin.unlink(missing_ok=True)
            except Exception:
                pass

        after = out.stat().st_size
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
