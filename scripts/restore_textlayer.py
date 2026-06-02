#!/usr/bin/env python3
"""恢复被 gs 压缩破坏文字层(ToUnicode)的扫描书——**免重跑 OCR**:
用 .orig.pdf 重建增强图(同原预处理参数,几何与现存 sidecar 对齐)→ 复用 state/google-vision-ocr
现存 char sidecar 重新嵌入正确文字层 → qpdf 线性化 → 原地替换。

前提:state/book-preprocess/<sha>.orig.pdf 与 state/google-vision-ocr/<sha>/p*.json 都在。
用法: python restore_textlayer.py --pdf <vault 绝对路径>
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preprocess_book import rebuild_pages   # 复用同款重建(uniform+enhance)

ROOT = Path("/home/bwicarus/claude")
STATUS_DIR = ROOT / "state" / "book-preprocess"
VISION_DIR = ROOT / "state" / "google-vision-ocr"


def sha_of(p: Path) -> str:
    return hashlib.sha1(str(Path(p).resolve()).encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    a = ap.parse_args()
    pdf = Path(a.pdf)
    sha = sha_of(pdf)
    orig = STATUS_DIR / f"{sha}.orig.pdf"
    sidecar_dir = VISION_DIR / sha
    if not orig.exists():
        print(f"✗ 没有 .orig.pdf 备份: {orig}"); return 1
    n_sc = len(list(sidecar_dir.glob("p*.json"))) if sidecar_dir.exists() else 0
    if n_sc == 0:
        print(f"✗ 没有 OCR sidecar: {sidecar_dir}（只能重跑 OCR）"); return 1
    print(f"sha={sha}  orig={orig.stat().st_size/1048576:.0f}MB  sidecar={n_sc} 页")

    tmp = STATUS_DIR / f"{sha}.restore.pdf"
    print("① 重建增强图(uniform+enhance,无 OCR)…")
    if not rebuild_pages(orig, tmp, uniform=True, enhance=True):
        print("✗ 重建失败"); return 1
    shutil.move(str(tmp), str(pdf))   # 先落到 vault 路径,让 embed 按该 sha 找到 sidecar

    print("② 嵌入文字层(复用现存 sidecar)…")
    emb = STATUS_DIR / f"{sha}.reembed.pdf"
    rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "embed_google_ocr_to_pdf.py"),
                         "--pdf", str(pdf), "--out", str(emb)], cwd=str(ROOT)).returncode
    if rc != 0 or not emb.exists():
        print("✗ 嵌入失败（vault 已是无文字层重建版，可重试）"); return 1

    print("③ 线性化(Fast Web View)…")
    if shutil.which("qpdf"):
        lin = STATUS_DIR / f"{sha}.rlin.pdf"
        r = subprocess.run(["qpdf", "--linearize", str(emb), str(lin)], capture_output=True).returncode
        if r in (0, 3) and lin.exists():
            emb.unlink(missing_ok=True); emb = lin
    shutil.move(str(emb), str(pdf))

    # 验证文字层
    d = fitz.open(str(pdf))
    cjk = 0
    for pg in d:
        for ch in (pg.get_text("text") or ""):
            if 0x3040 <= ord(ch) <= 0x30FF or 0x4E00 <= ord(ch) <= 0x9FFF:
                cjk += 1
    d.close()
    print(f"✓ 完成 {pdf.name}: {pdf.stat().st_size/1048576:.0f}MB, 文字层 CJK 字符≈{cjk}（>0 即恢复成功）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
