#!/usr/bin/env python3
"""PDF 字符层预热:把整本书每页的 chars + 振假名(fugashi 分词)算好存进磁盘缓存(state/pdf-char-cache),
之后阅读器首次翻到某页时下划线/振假名/选中即时出来(不用现算 fugashi)。配合 prewarm_pdf_pages.py(页图)=全本秒开。

调阅读器同款 pdf_reader._page_chars_cached(缓存键含 mtime)。需 CLAUDE_PROJECT/OBSIDIAN_VAULT env + fugashi/unidic-lite。
用法:  python scripts/prewarm_pdf_chars.py --pdf <绝对路径> --rel <vault相对路径> [--workers 6]
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_DEPLOY = str(Path(__file__).resolve().parent.parent / "_server_deploy")


def _warm(args):
    ap, rel, page = args
    try:
        if _DEPLOY not in sys.path:
            sys.path.insert(0, _DEPLOY)
        import pdf_reader
        r = pdf_reader._page_chars_cached(Path(ap), rel, page)
        return "ok" if r else "none"
    except Exception as e:
        return f"err:{type(e).__name__}:{e}"


def main():
    pr = argparse.ArgumentParser()
    pr.add_argument("--pdf", required=True)   # 绝对路径
    pr.add_argument("--rel", required=True)   # vault 相对路径(如 资源/books/xxx.pdf)
    pr.add_argument("--workers", type=int, default=6)
    a = pr.parse_args()
    ap = str(Path(a.pdf).resolve())

    import fitz
    n = fitz.open(ap).page_count
    print(f"chars warm: {ap}\n rel={a.rel} pages={n} workers={a.workers}", flush=True)

    tasks = [(ap, a.rel, pg) for pg in range(1, n + 1)]
    done = ok = none = err = 0
    first_errs = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for st in ex.map(_warm, tasks):
            done += 1
            if st == "ok": ok += 1
            elif st == "none": none += 1
            else:
                err += 1
                if len(first_errs) < 3: first_errs.append(st)
            if done % 50 == 0 or done == n:
                print(f"  {done}/{n}  (ok {ok} / none {none} / err {err})", flush=True)
    if first_errs:
        print("前几个错误:", flush=True)
        for e in first_errs: print("  " + e, flush=True)
    print(f"DONE pages={n} ok={ok} none={none} err={err}", flush=True)


if __name__ == "__main__":
    main()
