#!/usr/bin/env python3
"""电子书(epub/mobi/fb2/xps/cbz) → 带文字层的分页 PDF。后台 detached 跑(大书/多卷集转换要几分钟,
别阻塞上传请求 → 否则手机端 fetch 超时报错而服务端其实在转)。

首选 Calibre `ebook-convert`(业界标准,智能分页、图不切断、CJK fallback);没装/失败 → 回退 PyMuPDF。
写进度文件 {status: converting|done|error, ...} 供 webapp 轮询。

  convert_ebook.py <src 文件> <out.pdf> [--progress <json路径>]
"""
import os, sys, json, time, shutil, argparse, subprocess
from pathlib import Path


def _write(p, **kw):
    if not p:
        return
    kw["ts"] = int(time.time()); kw["pid"] = os.getpid()
    try:
        Path(p).write_text(json.dumps(kw, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def convert(src, out, progress=None):
    src, out = Path(src), Path(out)
    _write(progress, status="converting")
    eb = shutil.which("ebook-convert")
    if eb:
        env = dict(os.environ); env["QT_QPA_PLATFORM"] = "offscreen"
        # ⭐ 防图/表跨页(业界标准做法):page-break-inside:avoid 让放不下的整块图推到下一页;
        #   max-height:86vh + !important **覆盖 epub 里写死的图尺寸** → 比页高的图缩到一页内、不再被拦腰切两页。
        anti_split_css = (
            "img,svg,figure,table,figcaption{page-break-inside:avoid !important;}"
            "img,svg{max-width:100% !important;max-height:86vh !important;height:auto !important;width:auto !important;}"
            "figure{max-height:92vh !important;}"
            "h1,h2,h3,h4,figcaption,caption{page-break-after:avoid;}"
        )
        cmd = [eb, str(src), str(out), "--paper-size", "a4", "--pdf-default-font-size", "16",
               "--margin-top", "40", "--margin-bottom", "40", "--margin-left", "48", "--margin-right", "48",
               "--extra-css", anti_split_css]
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                _write(progress, status="done", engine="calibre")
                return True
        except Exception as e:
            _write(progress, status="converting", note=f"calibre 失败转 PyMuPDF: {str(e)[:80]}")
    # 回退 PyMuPDF
    import fitz
    d = fitz.open(str(src))
    try:
        if getattr(d, "is_reflowable", False):
            d.layout(rect=fitz.paper_rect("a4"), fontsize=11)
        out.write_bytes(d.convert_to_pdf())
    finally:
        d.close()
    _write(progress, status="done", engine="pymupdf")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out"); ap.add_argument("--progress", default=None)
    a = ap.parse_args()
    try:
        convert(a.src, a.out, a.progress)
    except Exception as e:
        _write(a.progress, status="error", error=str(e)[:200])
        raise
