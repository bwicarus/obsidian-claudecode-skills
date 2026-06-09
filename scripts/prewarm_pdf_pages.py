#!/usr/bin/env python3
"""PDF 页图预热:把整本书所有页按指定宽度一次渲染进页图缓存(key 与 pdf_reader 的 /api/page-image 完全一致),
之后阅读器翻页直接命中缓存=秒开。本地实例(强 CPU)尤其值得——大书一次备齐、之后零等待。

key 复刻 pdf_reader.pdf_api_page_image:
  cache = <CLAUDE_PROJECT>/state/pdf-page-img/<sha1(resolve(abs))[:16]>-p<page>-w<w>-<int(mtime)>.jpg
  render = fitz get_pixmap(matrix=w/page_width, alpha=False).tobytes('jpg', jpg_quality=78),atomic 写

用法:  python scripts/prewarm_pdf_pages.py --pdf <绝对路径> [--width 1260] [--workers 4]
"""
import argparse
import hashlib
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path


def _render(args):
    ap, page, w, mt, sha, cache_dir = args
    import fitz
    cf = Path(cache_dir) / f"{sha}-p{page}-w{w}-{mt}.jpg"
    if cf.exists():
        return "cached"
    try:
        d = fitz.open(ap)
        p = d[page - 1]
        zoom = w / max(1.0, p.rect.width)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        tmp = cf.with_suffix(".jpg.tmp")
        tmp.write_bytes(pix.tobytes("jpg", jpg_quality=78))
        tmp.replace(cf)
        d.close()
        return "ok"
    except Exception as e:
        return f"err:{e}"


def main():
    pr = argparse.ArgumentParser()
    pr.add_argument("--pdf", required=True)
    pr.add_argument("--width", type=int, default=1260)
    pr.add_argument("--workers", type=int, default=4)
    a = pr.parse_args()

    ap = str(Path(a.pdf).resolve())
    sha = hashlib.sha1(ap.encode("utf-8")).hexdigest()[:16]   # 与 _book_sha 一致(按 resolve 后绝对路径)
    mt = int(os.stat(ap).st_mtime)
    proj = os.environ.get("CLAUDE_PROJECT", str(Path(__file__).resolve().parent.parent))
    cache_dir = Path(proj) / "state" / "pdf-page-img"
    cache_dir.mkdir(parents=True, exist_ok=True)

    import fitz
    n = fitz.open(ap).page_count
    print(f"book={ap}\n sha={sha} mt={mt} pages={n} w={a.width} workers={a.workers}\n cache={cache_dir}", flush=True)

    tasks = [(ap, pg, a.width, mt, sha, str(cache_dir)) for pg in range(1, n + 1)]
    done = ok = cached = err = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for st in ex.map(_render, tasks):
            done += 1
            if st == "ok": ok += 1
            elif st == "cached": cached += 1
            else: err += 1
            if done % 50 == 0 or done == n:
                print(f"  {done}/{n}  (新渲 {ok} / 已存 {cached} / 错 {err})", flush=True)
    print(f"DONE pages={n} 新渲={ok} 已存={cached} 错={err}", flush=True)


if __name__ == "__main__":
    main()
