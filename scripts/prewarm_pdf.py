#!/usr/bin/env python3
"""整本预热(图 + 字符层)一把梭:先按指定宽度渲全部页图,再算全部页字符+振假名。
被 pdf_reader 的 /api/prewarm-async 以 detached 子进程启动(本地实例后台预热,不阻塞阅读)。

用法:  python scripts/prewarm_pdf.py --pdf <绝对路径> --rel <vault相对> --width <w> [--workers 4]
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    pr = argparse.ArgumentParser()
    pr.add_argument("--pdf", required=True)
    pr.add_argument("--rel", required=True)
    pr.add_argument("--width", type=int, default=1260)
    # 默认吃满 CPU(留 1 核给阅读器响应)。强 CPU 上并行渲染,不串行。
    pr.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    a = pr.parse_args()
    here = Path(__file__).resolve().parent
    py = sys.executable
    # ① 页图(按宽度,width-specific)
    subprocess.run([py, str(here / "prewarm_pdf_pages.py"),
                    "--pdf", a.pdf, "--width", str(a.width), "--workers", str(a.workers)])
    # ② 字符层 + 振假名(width-independent,只需一次)
    subprocess.run([py, str(here / "prewarm_pdf_chars.py"),
                    "--pdf", a.pdf, "--rel", a.rel, "--workers", str(a.workers)])
    print("PREWARM_ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
