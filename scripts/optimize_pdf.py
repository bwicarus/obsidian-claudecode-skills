#!/usr/bin/env python3
"""PDF 瘦身/提速(无损):根治 epub→PDF 转换产生的「海量碎对象」结构。

典型病症:epub→PDF 转换器把每页文字层拆成几百个碎内容流,一本书几十万个对象,
PDF 1.3 经典 xref 表能到 ~10MB → PDF.js 打开(尤其定位深层页)必须先下完整 xref,极慢。

修法(全程不动图片像素 = 清晰度无损):
  1. PyMuPDF save(garbage=4 回收去重 + clean=True 合并内容流 + deflate 压缩)
     → 对象数从几十万降到几千,xref 表缩到几十 KB
  2. qpdf --linearize → 线性化(首页 + 结构前置,PDF.js 秒开)
  3. 多页抽样校验:文字层文本 + 图片尺寸必须与原文件逐页一致,否则放弃

用法:
  python3 scripts/optimize_pdf.py <pdf>            # 原地优化(自动备份到 data/pdf-backup/)
  python3 scripts/optimize_pdf.py <pdf> -o OUT     # 输出到别处,不动原文件
  python3 scripts/optimize_pdf.py <pdf> --check    # 只看对象数/体积,不改
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))


def _stats(path: Path) -> dict:
    import fitz
    d = fitz.open(str(path))
    try:
        return {"objects": d.xref_length(), "pages": d.page_count,
                "mb": round(path.stat().st_size / 1024 / 1024, 1),
                "version": d.metadata.get("format", "")}
    finally:
        d.close()


def _verify_lossless(orig: Path, new: Path, sample: int = 8) -> bool:
    """抽样校验:文字层文本 + 各图片宽度逐页一致。"""
    import fitz
    o = fitz.open(str(orig)); n = fitz.open(str(new))
    try:
        if o.page_count != n.page_count:
            print(f"  ✗ 页数不一致 {o.page_count} vs {n.page_count}")
            return False
        pc = o.page_count
        pages = sorted(set([0, pc - 1] + [int(pc * k / sample) for k in range(sample)]))
        for pno in pages:
            if pno >= pc:
                continue
            if o[pno].get_text("text") != n[pno].get_text("text"):
                print(f"  ✗ p{pno+1} 文字层不一致"); return False
            do = [o.extract_image(i[0])["width"] for i in o[pno].get_images()]
            dn = [n.extract_image(i[0])["width"] for i in n[pno].get_images()]
            if do != dn:
                print(f"  ✗ p{pno+1} 图片尺寸不一致 {do}->{dn}"); return False
        return True
    finally:
        o.close(); n.close()


def optimize(pdf: Path, out: Path | None, do_check: bool) -> int:
    import fitz
    if not pdf.exists():
        print(f"找不到: {pdf}", file=sys.stderr); return 1
    st0 = _stats(pdf)
    print(f"原始: {st0['objects']} 对象, {st0['pages']} 页, {st0['mb']}MB, {st0['version']}")
    if do_check:
        return 0

    tmp = Path(tempfile.mkstemp(suffix=".pdf", dir="/tmp")[1])
    tmp2 = Path(tempfile.mkstemp(suffix=".pdf", dir="/tmp")[1])
    try:
        print("[1/3] PyMuPDF garbage4 + clean(合并内容流) + deflate …", flush=True)
        d = fitz.open(str(pdf))
        d.save(str(tmp), garbage=4, deflate=True, clean=True)
        d.close()
        # qpdf 线性化(可选,失败不致命)
        linearized = tmp
        if shutil.which("qpdf"):
            print("[2/3] qpdf --linearize …", flush=True)
            r = subprocess.run(["qpdf", "--linearize", str(tmp), str(tmp2)],
                               capture_output=True, text=True)
            if r.returncode in (0, 3):   # 3 = warnings only
                linearized = tmp2
            else:
                print(f"  qpdf 失败(用未线性化版): {r.stderr[:120]}")
        else:
            print("[2/3] 无 qpdf,跳过线性化")

        print("[3/3] 多页无损校验 …", flush=True)
        if not _verify_lossless(pdf, linearized):
            print("✗ 校验失败,放弃(原文件未动)", file=sys.stderr)
            return 2
        st1 = _stats(linearized)
        print(f"优化后: {st1['objects']} 对象, {st1['pages']} 页, {st1['mb']}MB  "
              f"(对象 -{100-round(st1['objects']/st0['objects']*100)}%, 体积 -{100-round(st1['mb']/st0['mb']*100)}%) ✅无损")

        if out:
            shutil.move(str(linearized), str(out))
            print(f"已写出: {out}")
        else:
            # 原地:先备份到 data/pdf-backup/
            bdir = PROJECT_ROOT / "data" / "pdf-backup"
            bdir.mkdir(parents=True, exist_ok=True)
            bak = bdir / (pdf.stem + ".orig.pdf")
            if not bak.exists():
                shutil.copy2(str(pdf), str(bak))
                print(f"原文件已备份: {bak}")
            shutil.move(str(linearized), str(pdf))
            print(f"已原地替换: {pdf}")
        return 0
    finally:
        for t in (tmp, tmp2):
            try: t.unlink(missing_ok=True)
            except Exception: pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    return optimize(Path(args.pdf), Path(args.out) if args.out else None, args.check)


if __name__ == "__main__":
    sys.exit(main())
