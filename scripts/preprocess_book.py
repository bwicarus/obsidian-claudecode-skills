#!/usr/bin/env python3
"""书本预处理编排器 —— **只粘合现有脚本，不重写 OCR**。

流程：检测文字层 → 没有就 google_vision_ocr.py(Google Vision，现成) → embed_google_ocr_to_pdf.py
(嵌入不可见可选中文字层，现成) → **原地替换**原 PDF（先备份到 state/book-preprocess/<sha>.orig.pdf，
不在 vault 里以免污染书列表）。

状态/进度写 state/book-preprocess/<sha>.json，webapp `/pdf/api/preprocess-status` 直接读它。
detached 运行(start_new_session) → 关网页 / webapp 重启都不中断；OCR sidecar 断点续传，重跑自动续。

用法: python3 scripts/preprocess_book.py --pdf <PDF 绝对路径>
"""
import argparse, hashlib, io, json, os, shutil, statistics, subprocess, sys, time
from pathlib import Path

import fitz  # PyMuPDF（webapp 同款）

ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
STATUS_DIR = ROOT / "state" / "book-preprocess"
VISION_DIR = ROOT / "state" / "google-vision-ocr"
MOKURO_DIR = ROOT / "state" / "mokuro-ocr"
PY = sys.executable
# manga-ocr(mokuro)需要独立 venv(torch + manga_ocr),非默认 python
MANGA_PY = os.environ.get("MANGA_OCR_PYTHON", "/home/bwicarus/manga-ocr-venv/bin/python")


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


def _needs_width_norm(pdf: Path, tol: float = 0.02) -> bool:
    """各页(视觉)宽是否参差到值得归一。单页或基本等宽 → 不归一。"""
    doc = fitz.open(str(pdf))
    try:
        ws = [p.rect.width for p in doc if p.rect.width > 0]
    finally:
        doc.close()
    if len(ws) < 2:
        return False
    return (max(ws) - min(ws)) / max(ws) > tol


def _pages_missing_text(pdf: Path, min_chars: int = 4) -> list:
    """**逐页**(非抽样)检查文字层:返回缺文字层的页 index 列表。每页可提取文字 < min_chars 视为缺。
    抽样的 has_text_layer 会被"前段有文字、后段没有"的合并书骗过(只看到前几页有文字就判整书 OK),
    这里逐页保证"是否所有页都 OCR 过"判得准。"""
    doc = fitz.open(str(pdf))
    try:
        return [i for i in range(doc.page_count)
                if len((doc[i].get_text("text") or "").strip()) < min_chars]
    finally:
        doc.close()


def _enhance_jpeg(pix, jpg_quality: int) -> bytes:
    """对一页渲染图做"字迹更清晰"增强:UnsharpMask 锐化 + 轻对比度,**保留色彩**
    (封面/橙色边框不变灰)。实测模糊扫描书字迹明显变黑变利、网点不崩、颜色不偏。"""
    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    img = img.filter(ImageFilter.UnsharpMask(radius=2.2, percent=170, threshold=2))
    img = ImageEnhance.Contrast(img).enhance(1.35)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=jpg_quality)
    return buf.getvalue()


def rebuild_pages(src_path: Path, out_path: Path, uniform: bool = True,
                  enhance: bool = False, raster_scale: float = 2.0,
                  jpg_quality: int = 82) -> bool:
    """重建扫描书每页(栅格化)。两个独立开关:
      uniform=True  → 视觉宽统一成中位宽(前端单一全局 scale 即等宽显示,不碰浮层锚定);
      enhance=True  → 锐化+对比增强,让模糊字迹更清晰(见 _enhance_jpeg)。

    **用栅格化(get_pixmap)而非 show_pdf_page**:get_pixmap 自动应用 /Rotate → 正立图,
    顺带烘焙旋转成 rotation=0 → 后续 embed 不必 derotation、坐标平凡(旋转页 show_pdf_page
    嵌文字 save 后会整体错位,栅格化根治);且正立铺满无 letterbox。
    raster_scale=2 渲 2× 显示分辨率保证缩放清晰;JPEG 压缩控体积。
    **务必在 OCR 之后调用**:OCR 要在锐利原图上做(栅格化有一次重采样会降命中率);embed 时
    sidecar(原图渲染坐标)按 sx=页宽/sidecar.img_w 映射到重建页,与其像素分辨率无关。
    失败返回 False(不动原书)。"""
    src = fitz.open(str(src_path))
    try:
        ws = [p.rect.width for p in src if p.rect.width > 0]   # 视觉宽(已含旋转)
        if not ws:
            return False
        target = statistics.median(ws)
        dst = fitz.open()
        try:
            for p in src:
                vw, vh = p.rect.width, p.rect.height           # 视觉尺寸
                if vw <= 0 or vh <= 0:
                    return False
                k = (target / vw) if uniform else 1.0
                mat = fitz.Matrix(k * raster_scale, k * raster_scale)
                pix = p.get_pixmap(matrix=mat)                 # 自动应用 /Rotate → 正立
                jpg = _enhance_jpeg(pix, jpg_quality) if enhance \
                    else pix.tobytes("jpg", jpg_quality=jpg_quality)
                del pix
                npg = dst.new_page(width=vw * k, height=vh * k)  # rotation=0;uniform 时宽=target
                npg.insert_image(npg.rect, stream=jpg)
            dst.save(str(out_path), garbage=4, deflate=True)
        finally:
            dst.close()
        return True
    except Exception:
        return False
    finally:
        src.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--engine", choices=["vision", "manga"], default="vision",
                    help="vision=Google Vision(印刷/文档,快,~2s/页);"
                         "manga=manga-ocr(漫画气泡/竖排/手写体,慢,~40s/页 CPU)")
    ap.add_argument("--no-uniform", dest="uniform", action="store_false",
                    help="不统一页宽(默认:扫描书各页宽参差时归一成等宽+烘焙旋转再 OCR)")
    ap.add_argument("--enhance", dest="enhance", action="store_true",
                    help="清晰度增强:栅格化+锐化+对比,让模糊扫描字迹更清晰(由「增加清晰度」按钮触发)")
    a = ap.parse_args()
    pdf = Path(a.pdf)
    sha = _sha(pdf)

    # 引擎参数表：OCR 子命令 / 进度文件 / sidecar 目录 / 嵌入脚本 / 文案
    if a.engine == "manga":
        ocr_cmd = [MANGA_PY, str(ROOT / "scripts" / "mokuro_ocr_book.py"),
                   "--pdf", str(pdf), "--dpi", str(a.dpi)]
        prog_path = MOKURO_DIR / sha / "progress.json"
        sidecar_dir = MOKURO_DIR / sha
        embed_script = "embed_ocr_to_pdf.py"
        engine_label = "manga-ocr（漫画）"
    else:
        ocr_cmd = [PY, str(ROOT / "scripts" / "google_vision_ocr.py"),
                   "--pdf", str(pdf), "--dpi", str(a.dpi), "--workers", str(a.workers)]
        prog_path = VISION_DIR / sha / "progress.json"
        sidecar_dir = VISION_DIR / sha
        embed_script = "embed_google_ocr_to_pdf.py"
        engine_label = "Google Vision"

    try:
        if not pdf.exists():
            _write(sha, phase="error", error="PDF 不存在", pdf=str(pdf))
            return 1
        _write(sha, phase="detecting", percent=0, msg="检测文字层…", pdf=str(pdf),
               error="", pid=os.getpid(), engine=a.engine)

        orig = STATUS_DIR / f"{sha}.orig.pdf"
        # 有 .orig.pdf 备份 = 之前从扫描件 OCR 过 → 视为扫描书,从干净原图重做(支持改归一/换引擎)。
        prev_ocrd = orig.exists()
        clean_src = orig if prev_ocrd else pdf       # 干净原图(归一/重 OCR 的源)

        # ── 检查流程（用户要求）──：① 先查页宽是否一致 ② 再逐页查是否都 OCR 过(有文字层)。
        _write(sha, phase="detecting", percent=1, msg="① 检查页宽一致性…", engine=a.engine)
        need_width = bool(a.uniform and _needs_width_norm(clean_src))
        enhance = bool(a.enhance)
        _write(sha, phase="detecting", percent=2, msg="② 逐页检查文字层覆盖…", engine=a.engine)
        missing = _pages_missing_text(clean_src)     # 缺文字层的页(逐页,非抽样)
        n_total = fitz.open(str(clean_src)).page_count

        # 早退：非漫画 + 没要增强 + 页宽已一致 + 所有页都有文字层（且不是"曾处理过的扫描书"）→ 无需处理。
        if a.engine != "manga" and not enhance and not need_width and not missing and not prev_ocrd:
            _write(sha, phase="done", percent=100, has_text=True,
                   msg=f"检查通过：{n_total} 页宽度一致且都有文字层，无需处理")
            return 0

        # 决定是否栅格化重建。**触发条件**：增强 / 页宽不一致(归一) / 部分页缺文字层(0<缺<总→
        # 重建后全页统一 OCR,避免给已有文字的页叠加重复层)。栅格化会毁掉所有现存文字层 → 之后对
        # 重建图全页 OCR,这正是"所有页都 OCR 过"的落实。整书都没文字层(缺==全部)且没要增强/归一时
        # 不重建,直接在锐利原图上 OCR(质量更好),embed 也不会重复(本就无文字)。
        # 重建页 rotation=0 → OCR/embed 坐标平凡(get_pixmap 烘焙旋转,见 rebuild_pages 注释)。
        uniform = need_width
        partial_missing = 0 < len(missing) < n_total
        rebuild = enhance or uniform or partial_missing
        if rebuild:
            tags = []
            if uniform: tags.append(f"统一页宽（{n_total} 页参差→中位宽）")
            if enhance: tags.append("增强清晰度（锐化）")
            if missing: tags.append(f"{len(missing)}/{n_total} 页缺文字层→重建后全页 OCR")
            _write(sha, phase="normalizing", percent=3,
                   msg="检查后处理：" + "、".join(tags) + "…", engine=a.engine)
            if not orig.exists():
                shutil.copy2(str(pdf), str(orig))          # 备份真·原始(重建前,clean_src=pdf 时仍是原图)
            tmp = STATUS_DIR / f"{sha}.norm.pdf"
            if not rebuild_pages(clean_src, tmp, uniform=uniform, enhance=enhance):
                _write(sha, phase="error", error="页重建失败（原书未改动）")
                return 1
            shutil.move(str(tmp), str(pdf))                # pdf = 重建后(等宽/增强/rotation=0)的图
            for d in (VISION_DIR / sha, MOKURO_DIR / sha):
                shutil.rmtree(d, ignore_errors=True)       # 几何变了,重新 OCR
        elif prev_ocrd:
            shutil.copy2(str(orig), str(pdf))              # 之前处理过、本次不重建:从干净原图恢复再 OCR
            for d in (VISION_DIR / sha, MOKURO_DIR / sha):
                shutil.rmtree(d, ignore_errors=True)

        total = fitz.open(str(pdf)).page_count
        _write(sha, phase="ocr", percent=0, total=total, completed=0,
               msg=f"{engine_label} OCR…", engine=a.engine)
        # ① OCR（现成脚本，自身断点续传：已完成的页跳过）
        proc = subprocess.Popen(ocr_cmd, cwd=str(ROOT))
        while proc.poll() is None:
            time.sleep(2)
            try:
                pg = json.loads(prog_path.read_text("utf-8"))
                comp = int(pg.get("completed", 0))
                tot = int(pg.get("total", total) or total)
                # ETA：vision 给 eta_minutes，mokuro 给 eta_hours，取到哪个用哪个
                if pg.get("eta_minutes") is not None:
                    eta = f"约 {pg['eta_minutes']} 分钟"
                elif pg.get("eta_hours") is not None:
                    eta = f"约 {round(float(pg['eta_hours']) * 60)} 分钟"
                else:
                    eta = "计算中"
                _write(sha, phase="ocr", completed=comp, total=tot,
                       percent=int(comp * 90 / max(1, tot)),
                       msg=f"{engine_label} OCR {comp}/{tot}（{eta}）")
            except Exception:
                pass
        if proc.returncode != 0:
            _write(sha, phase="error", error=f"OCR 退出码 {proc.returncode}")
            return 1

        # OCR 子进程返回 0 不代表识别成功：逐页 sidecar 可能整本都是空/error
        # (vision: 网络/SSL/图过大；mokuro: 检测不到文字块)。全错就别嵌入空文字层动原书。
        ok_pages = err_pages = 0
        sample_err = ""
        for jf in sorted(sidecar_dir.glob("p*.json")):
            try:
                sc = json.loads(jf.read_text("utf-8"))
            except Exception:
                continue
            if sc.get("error"):
                err_pages += 1
                sample_err = sample_err or str(sc.get("error"))[:120]
            elif sc.get("text") or sc.get("chars") or sc.get("blocks"):
                ok_pages += 1
        if ok_pages == 0:
            _write(sha, phase="error",
                   error=f"OCR 未识别到任何文字（{err_pages} 页失败，原书未改动）"
                         + (f"：{sample_err}" if sample_err else ""))
            return 1

        # ② 嵌入文字层(现成脚本)→ 库外临时文件。pdf 跟 OCR 的是同一张图(重建后 rotation=0),
        # sidecar 坐标直接对得上,embed 无需 derotation/缩放映射。
        _write(sha, phase="embedding", percent=94, msg="嵌入文字层…")
        out = STATUS_DIR / f"{sha}.embedded.pdf"
        r = subprocess.run(
            [PY, str(ROOT / "scripts" / embed_script),
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
