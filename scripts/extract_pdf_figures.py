#!/usr/bin/env python3
"""born-digital(原生数字)PDF 的插图提取:**直接拿嵌入图像对象的位置**,不跑 YOLO。

适用:转换自 EPUB 的书、出版社原生数字 PDF —— 图和文字本来就分开(真矢量文字层 + 独立栅格图),
`page.get_image_info()` 给出每张嵌入图的精确位置 → 比"扫描书整页是一张图、靠 YOLO/视觉找图"简单可靠得多。

写进跟 yolo_figures / describe_figures_batch 同一个 sidecar(`state/pdf-figures/<sha>.json`)的 `figures_geom`,
条目格式一致(page/bbox/fbox/fsrc=embedded/needs_describe),交给 describe_figures_batch.py 裁图描述(带跨书去重)。

  python3 extract_pdf_figures.py --book "/abs/path.pdf"
  is_born_digital(doc) 供 webapp 判定该用本提取还是 YOLO。
"""
import os, sys, json, time, hashlib, argparse
from pathlib import Path
import fitz

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
FIG_DIR = PROJECT / "state" / "pdf-figures"


def book_sha(p) -> str:
    return hashlib.sha1(str(Path(p).resolve()).encode("utf-8")).hexdigest()[:16]


def is_born_digital(doc, sample=8) -> bool:
    """采样若干页判定:多数页有**真文字层**且**没有占满整页的大图** → 原生数字(非扫描)。"""
    n = doc.page_count
    if n == 0:
        return False
    pnos = sorted({max(0, min(n - 1, int(n * i / (sample + 1)))) for i in range(1, sample + 1)})
    born = checked = 0
    for pno in pnos:
        p = doc[pno]; checked += 1
        area = (p.rect.width * p.rect.height) or 1.0
        full = False
        for im in p.get_image_info():
            b = im.get("bbox")
            if b and (b[2] - b[0]) * (b[3] - b[1]) > 0.85 * area:
                full = True; break
        if len((p.get_text("text") or "").strip()) > 40 and not full:
            born += 1
    return born >= max(1, checked // 2)


def page_figures(page):
    """该页的嵌入图 → 归一 bbox 列表。过滤:占满整页(扫描底图)、太小(图标/项目符)、太细长(分隔线)。"""
    W, H = page.rect.width, page.rect.height
    area = (W * H) or 1.0
    out = []
    seen = set()
    for im in page.get_image_info(xrefs=True):
        b = im.get("bbox")
        if not b:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if w < 24 or h < 24:
            continue
        a = w * h
        if a > 0.85 * area:                 # 占满整页 → 多半是底图/全幅,跳过(留给 YOLO 类处理)
            continue
        if a < 0.012 * area:                # < 1.2% 页面 → 图标/装饰/项目符
            continue
        ar = max(w, h) / max(1.0, min(w, h))
        if ar > 12:                         # 极细长 → 分隔线/边框
            continue
        key = im.get("xref") or (round(b[0]), round(b[1]), round(w), round(h))
        if key in seen:
            continue
        seen.add(key)
        out.append([round(b[0] / W, 4), round(b[1] / H, 4), round(b[2] / W, 4), round(b[3] / H, 4)])
    return out


def build(pdf_path: str, dry=False) -> dict:
    doc = fitz.open(pdf_path)
    sha = book_sha(pdf_path)
    sc = FIG_DIR / f"{sha}.json"
    data = {}
    if sc.exists():
        try:
            data = json.loads(sc.read_text("utf-8"))
        except Exception:
            data = {}
    data["pdf"] = pdf_path
    born = is_born_digital(doc)
    geom = data.get("figures_geom") or []
    # 已有(YOLO/旧)条目按 (page, 量化 bbox) 去重,避免重复加
    have = set()
    for f in geom:
        bb = f.get("fbox") or f.get("bbox")
        if f.get("page") and bb:
            have.add((f["page"], round(bb[0], 2), round(bb[1], 2), round(bb[2], 2), round(bb[3], 2)))
    n_add = 0
    for pno in range(doc.page_count):
        for bb in page_figures(doc[pno]):
            page1 = pno + 1
            key = (page1, round(bb[0], 2), round(bb[1], 2), round(bb[2], 2), round(bb[3], 2))
            if key in have:
                continue
            have.add(key)
            geom.append({"page": page1, "bbox": bb, "fbox": bb, "fsrc": "embedded",
                         "needs_describe": True})
            n_add += 1
    doc.close()
    data["figures_geom"] = geom
    data["figures_geom_src"] = "embedded" if born else (data.get("figures_geom_src") or "yolo")
    if not dry:
        FIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(sc) + ".tmp"
        Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, str(sc))
    return {"born_digital": born, "added": n_add, "total": len(geom), "sidecar": str(sc)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    r = build(a.book, a.dry)
    print(json.dumps(r, ensure_ascii=False))
