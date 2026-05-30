#!/usr/bin/env python3
"""Mokuro 日文 OCR：对扫描 PDF 每页跑文字检测 + manga-ocr，存 sidecar JSON。

设计要点：
- 断点续传：每页 sidecar 存 state/mokuro-ocr/<sha>/p<num>.json，已存的页跳过；
  systemd Restart=on-failure 配合 → 重启/断电也能接着跑。
- 进度文件：state/mokuro-ocr/<sha>/progress.json 持续刷新 (完成数 / ETA)。
- 低优先级：systemd Nice=19 + CPUWeight=20 让 webapp/AI 调用优先，OCR 跑闲时。
- 不立即嵌 PDF：OCR 阶段只产 sidecar。embed 由独立脚本 embed_ocr_to_pdf.py 做
  （等 OCR 完了再调），失败重跑也不浪费 OCR 时间。

用法（venv 跑）：
  /home/bwicarus/manga-ocr-venv/bin/python scripts/mokuro_ocr_book.py \\
    --pdf "/home/bwicarus/obsidian/资源/books/応用情報技術者.pdf"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import fitz  # PyMuPDF
from mokuro.manga_page_ocr import MangaPageOcr

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATE_DIR = PROJECT / "state" / "mokuro-ocr"


def pdf_sha(pdf_path: Path) -> str:
    return hashlib.sha1(str(pdf_path.resolve()).encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2

    sha = pdf_sha(pdf_path)
    work = STATE_DIR / sha
    work.mkdir(parents=True, exist_ok=True)
    (work / "source.txt").write_text(str(pdf_path), encoding="utf-8")
    progress_path = work / "progress.json"

    doc = fitz.open(str(pdf_path))
    n = len(doc)
    print(f"[{time.strftime('%H:%M:%S')}] PDF: {pdf_path.name}", flush=True)
    print(f"  pages: {n}", flush=True)
    print(f"  sidecar: {work}", flush=True)

    done = set()
    for f in work.glob("p*.json"):
        try:
            done.add(int(f.stem[1:]))
        except ValueError:
            pass
    print(f"  已完成: {len(done)}/{n}", flush=True)
    if len(done) >= n:
        print("全部页已 OCR 完成。", flush=True)
        return 0

    print(f"[{time.strftime('%H:%M:%S')}] 加载 mokuro/manga-ocr 模型…", flush=True)
    t0 = time.time()
    mpo = MangaPageOcr(force_cpu=True)
    print(f"  init: {time.time() - t0:.1f}s", flush=True)

    start_time = time.time()
    last_log = 0.0
    init_done = len(done)

    for i in range(n):
        if i in done:
            continue
        page = doc[i]
        pix = page.get_pixmap(dpi=args.dpi)
        img_path = work / f"p{i:04d}.png"
        pix.save(str(img_path))
        del pix

        t = time.time()
        try:
            result = mpo(str(img_path))
            err = None
        except Exception as ex:
            result = None
            err = f"{type(ex).__name__}: {ex}"
        dt = time.time() - t

        out = result if result else {"error": err}
        out["_page"] = i
        out["_ocr_seconds"] = round(dt, 2)
        (work / f"p{i:04d}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8"
        )
        img_path.unlink(missing_ok=True)  # 删 PNG 省空间，sidecar JSON 足够
        done.add(i)

        completed = len(done)
        completed_run = completed - init_done
        elapsed_run = time.time() - start_time
        avg = elapsed_run / max(1, completed_run)
        remaining = n - completed
        eta_h = remaining * avg / 3600.0
        progress = {
            "pdf": pdf_path.name,
            "completed": completed,
            "total": n,
            "percent": round(completed / n * 100, 1),
            "last_page_seconds": round(dt, 2),
            "avg_seconds_per_page": round(avg, 2),
            "eta_hours": round(eta_h, 2),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        progress_path.write_text(
            json.dumps(progress, ensure_ascii=False, indent=2), "utf-8"
        )

        now = time.time()
        if now - last_log > 60 or completed % 50 == 0:
            print(
                f"  [{completed}/{n} {completed/n*100:.1f}%] page {i+1} {dt:.1f}s "
                f"| avg {avg:.1f}s | ETA {eta_h:.1f}h",
                flush=True,
            )
            last_log = now

    print(f"[{time.strftime('%H:%M:%S')}] OCR 全部完成。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
