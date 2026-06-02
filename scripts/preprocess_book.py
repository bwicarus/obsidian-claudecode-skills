#!/usr/bin/env python3
"""书本预处理编排器 —— **只粘合现有脚本，不重写 OCR**。

流程：检测文字层 → 没有就 google_vision_ocr.py(Google Vision，现成) → embed_google_ocr_to_pdf.py
(嵌入不可见可选中文字层，现成) → **原地替换**原 PDF（先备份到 state/book-preprocess/<sha>.orig.pdf，
不在 vault 里以免污染书列表）。

状态/进度写 state/book-preprocess/<sha>.json，webapp `/pdf/api/preprocess-status` 直接读它。
detached 运行(start_new_session) → 关网页 / webapp 重启都不中断；OCR sidecar 断点续传，重跑自动续。

用法: python3 scripts/preprocess_book.py --pdf <PDF 绝对路径>
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path

import fitz  # PyMuPDF（webapp 同款）

ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATUS_DIR = ROOT / "state" / "book-preprocess"
VISION_DIR = ROOT / "state" / "google-vision-ocr"
PY = sys.executable


def _sha(pdf: Path) -> str:
    return hashlib.sha1(str(pdf.resolve()).encode("utf-8")).hexdigest()[:16]


def _write(sha: str, **kw):
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    p = STATUS_DIR / f"{sha}.json"
    d = {}
    try:
        d = json.loads(p.read_text("utf-8"))
    except Exception:
        pass
    d.update(kw)
    d["updated_at"] = time.time()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    tmp.replace(p)


def has_text_layer(pdf: Path, sample: int = 8) -> bool:
    """抽样若干页：累计可提取文字 ≥20 字符 → 判为「有文字层」（数字版/已 OCR）。"""
    doc = fitz.open(str(pdf))
    try:
        n = doc.page_count
        if n <= sample:
            idxs = range(n)
        else:
            idxs = [int(i * (n - 1) / (sample - 1)) for i in range(sample)]
        chars = 0
        for i in idxs:
            chars += len((doc[i].get_text("text") or "").strip())
            if chars >= 20:
                return True
        return False
    finally:
        doc.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()
    pdf = Path(a.pdf)
    sha = _sha(pdf)
    try:
        if not pdf.exists():
            _write(sha, phase="error", error="PDF 不存在", pdf=str(pdf))
            return 1
        _write(sha, phase="detecting", percent=0, msg="检测文字层…", pdf=str(pdf), error="")
        if has_text_layer(pdf):
            _write(sha, phase="done", percent=100, has_text=True, msg="已有文字层，无需 OCR")
            return 0

        total = fitz.open(str(pdf)).page_count
        _write(sha, phase="ocr", percent=0, total=total, completed=0, msg="Google Vision OCR…")
        # ① OCR（现成脚本，自身断点续传：已完成的页跳过）
        proc = subprocess.Popen(
            [PY, str(ROOT / "scripts" / "google_vision_ocr.py"),
             "--pdf", str(pdf), "--dpi", str(a.dpi), "--workers", str(a.workers)],
            cwd=str(ROOT))
        prog = VISION_DIR / sha / "progress.json"
        while proc.poll() is None:
            time.sleep(2)
            try:
                pg = json.loads(prog.read_text("utf-8"))
                comp = int(pg.get("completed", 0))
                tot = int(pg.get("total", total) or total)
                _write(sha, phase="ocr", completed=comp, total=tot,
                       percent=int(comp * 90 / max(1, tot)),
                       msg=f"OCR 中 {comp}/{tot}（约 {pg.get('eta_minutes','?')} 分钟）")
            except Exception:
                pass
        if proc.returncode != 0:
            _write(sha, phase="error", error=f"OCR 退出码 {proc.returncode}")
            return 1

        # ② 嵌入文字层（现成脚本）→ 库外临时文件
        _write(sha, phase="embedding", percent=93, msg="嵌入文字层…")
        out = STATUS_DIR / f"{sha}.embedded.pdf"
        r = subprocess.run(
            [PY, str(ROOT / "scripts" / "embed_google_ocr_to_pdf.py"),
             "--pdf", str(pdf), "--out", str(out)], cwd=str(ROOT))
        if r.returncode != 0 or not out.exists():
            _write(sha, phase="error", error="嵌入文字层失败")
            return 1

        # ③ 原地替换（先备份原书到库外，不污染 vault 书列表；只备份一次）
        bak = STATUS_DIR / f"{sha}.orig.pdf"
        if not bak.exists():
            shutil.copy2(str(pdf), str(bak))
        shutil.move(str(out), str(pdf))   # 跨目录安全（同盘 rename / 跨盘 copy+del）
        _write(sha, phase="done", percent=100, has_text=True,
               msg=f"完成：{total} 页已加文字层（原书备份在 state/book-preprocess/{sha}.orig.pdf）")
        return 0
    except Exception as e:
        _write(sha, phase="error", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
