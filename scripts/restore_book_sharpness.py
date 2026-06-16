#!/usr/bin/env python3
"""一次性:把 2026-06-16 被 rebuild_pages(写死 raster_scale=2=144dpi)降采样变糊的书,
从 state/book-preprocess/<sha>.orig.pdf(高清原始)重做并换回 vault。

每本两种方法(已逐本核实):
  embed        费曼:orig 是纯扫描(无文字层)+ 页宽已一致 → 直接把现成 OCR sidecar 嵌到
               未触碰的高清 orig 上(零栅格化,最清晰),换回 vault。
  rebuild      料理 part1/2:orig 有旧(乱码)文字层 + 页宽不一 → 按原生分辨率重建(我已修好
               rebuild_pages:去旧文字层 + 烘焙统一页宽 + 不降采样)→ 再嵌 sidecar → 换回。

安全:换前把当前(糊的)vault 文件备份到 <sha>.pre-restore.pdf;只在 sha 校验通过时换;
sidecar 用现成的(state/google-vision-ocr/<sha>),不重 OCR(不烧 GCP)。换后 mtime 变 →
阅读器 page-img / char-cache(都含 mtime 键)自动失效,顺手清旧缓存垃圾。
"""
from __future__ import annotations
import hashlib, os, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from preprocess_book import rebuild_pages  # noqa: E402

ROOT = config.PROJECT_DIR
STATUS_DIR = ROOT / "state" / "book-preprocess"
VISION_DIR = ROOT / "state" / "google-vision-ocr"
PAGE_IMG = ROOT / "state" / "pdf-page-img"
PREWARM = ROOT / "state" / "pdf-prewarm"
PY = config.APP_PYTHON if hasattr(config, "APP_PYTHON") else sys.executable
EMBED = ROOT / "scripts" / "embed_google_ocr_to_pdf.py"

# (sha, vault 绝对路径, 方法, 预构产物)。prebuilt 非空且校验通过 → 直接用它换,跳过重嵌
# (费曼嵌入 626k 字逐字插入 ~40min,先前已嵌好临时产物 → 复用,免再跑一遍)。
BOOKS = [
    ("1eadda6a2a33cb4d", "/home/bwicarus/obsidian/Excalidraw/费恩曼物理学讲义（第1卷）：新千年版 (R.P.Feynman) (Z-Library).pdf", "embed", "/tmp/feynman_test.pdf"),
    ("a4fd5b1e983602ff", "/home/bwicarus/obsidian/资源/uploads/料理师part1.pdf", "rebuild", None),
    ("5f1af7ee25e05278", "/home/bwicarus/obsidian/资源/uploads/料理师part2.pdf", "rebuild", None),
]


def sha16(p: str) -> str:
    return hashlib.sha1(str(os.path.realpath(p)).encode()).hexdigest()[:16]


def log(m: str):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def restore(sha: str, vault: str, method: str, prebuilt=None) -> bool:
    vp = Path(vault)
    orig = STATUS_DIR / f"{sha}.orig.pdf"
    sidecar = VISION_DIR / sha
    if sha16(vault) != sha:
        log(f"✗ {vp.name}: sha 不匹配(期望 {sha} 得 {sha16(vault)}),跳过"); return False
    if not orig.exists():
        log(f"✗ {vp.name}: 缺 orig 备份,跳过"); return False
    if not sidecar.exists() or not list(sidecar.glob("p*.json")):
        log(f"✗ {vp.name}: 缺 OCR sidecar,跳过"); return False

    work = STATUS_DIR / f"{sha}.restore.work.pdf"
    final = STATUS_DIR / f"{sha}.restore.final.pdf"
    t0 = time.time()

    if prebuilt and Path(prebuilt).exists() and Path(prebuilt).stat().st_size > 1_000_000:
        final = Path(prebuilt)          # 复用预构产物(费曼嵌入很慢,先前已嵌好)→ 直接换,跳过重嵌
        log(f"{vp.name}: 复用预构产物 {final}")
    else:
        for f in (work, final):
            f.unlink(missing_ok=True)
        if method == "rebuild":
            log(f"{vp.name}: 按原生分辨率重建(去旧文字层+统一页宽)…")
            ok = rebuild_pages(orig, work, uniform=True, enhance=False,
                               progress_cb=lambda d, t: (d % 10 == 0) and log(f"  重建 {d}/{t}"))
            if not ok:
                log(f"✗ {vp.name}: 重建失败"); return False
            embed_src = work
        else:
            embed_src = orig   # 直接嵌到未触碰的高清 orig
        log(f"{vp.name}: 嵌 OCR 文字层(sidecar={sidecar.name})…")
        r = subprocess.run([PY, str(EMBED), "--pdf", str(embed_src), "--out", str(final),
                            "--sidecar", str(sidecar)], cwd=str(ROOT))
        if r.returncode != 0 or not final.exists():
            log(f"✗ {vp.name}: 嵌入失败(rc={r.returncode})"); return False

    # 校验:页数一致 + 文件非空 + 抽样有文字层
    import fitz
    try:
        d = fitz.open(str(final)); npg = d.page_count
        txt = sum(len(d[i].get_text("text").strip()) for i in range(0, npg, max(1, npg // 6)))
        # 抽样页图分辨率
        res = ""
        for i in range(0, npg, max(1, npg // 4)):
            im = d[i].get_images(full=True)
            if im:
                ix = d.extract_image(im[0][0]); res = f"{ix['width']}x{ix['height']}"; break
        d.close()
    except Exception as e:
        log(f"✗ {vp.name}: 产物校验失败 {e}"); return False
    if npg < 3 or txt < 100:
        log(f"✗ {vp.name}: 产物异常(页{npg} 文字{txt}),不换"); return False

    # 备份当前(糊的)→ 换入清晰版(mtime 变→缓存自动失效)
    backup = STATUS_DIR / f"{sha}.pre-restore.pdf"
    shutil.copy2(str(vp), str(backup))
    shutil.move(str(final), str(vp))
    os.utime(str(vp), None)   # 确保新 mtime
    work.unlink(missing_ok=True)

    # 清旧缓存垃圾(page-img 含 mtime 键,旧 mtime 的图已用不上)
    cleaned = 0
    for g in PAGE_IMG.glob(f"{sha}-p*.jpg"):
        try: g.unlink(); cleaned += 1
        except Exception: pass
    for g in PREWARM.glob(f"{sha}-*.json"):
        try: g.unlink()
        except Exception: pass

    newsz = vp.stat().st_size // 1024 // 1024
    log(f"✓ {vp.name}: 完成 {npg}页 图≈{res} 文字层✓ → {newsz}MB(清 {cleaned} 旧页图)  {time.time()-t0:.0f}s")
    return True


def main() -> int:
    log(f"恢复 {len(BOOKS)} 本书清晰度(从 orig 重做,不重 OCR)")
    ok = 0
    for sha, vault, method, prebuilt in BOOKS:
        try:
            if restore(sha, vault, method, prebuilt): ok += 1
        except Exception as e:
            log(f"✗ {Path(vault).name}: 异常 {e}")
    log(f"完成:{ok}/{len(BOOKS)} 本恢复成功")
    return 0 if ok == len(BOOKS) else 1


if __name__ == "__main__":
    sys.exit(main())
