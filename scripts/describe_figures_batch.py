#!/usr/bin/env python3
"""夜间裁图描述批处理(off-peak 额度跑)。读 enabled 书的 figures_geom(YOLO 框,由 yolo_figures.py 填),
按页 gate 描述,把 desc 填回 geom 条目:
  · 该页 0 框(geom 无条目)→ 跳过(YOLO 已扫=无图,零 AI)
  · 1 框 → 裁那张图 + 本页文字 + provenance → 描述(明说『这是从书里截出来的图、描述它的内容』,实测比整页质量更好且省 ~74% token)
  · ≥2 框 → 整页图 + 各框坐标 + 文字 + provenance → AI 按框顺序逐张描述(多图保版面关系,但仍填回各 YOLO 框→框精确+幂等)
幂等:已有 desc 的条目跳过。provenance 来自「书籍目录」(state/pdf-toc/<sha>.json,无则原生 get_toc)。

  scripts/describe_figures_batch.py --all
  scripts/describe_figures_batch.py --book "/abs/path.pdf"
"""
from __future__ import annotations
import argparse, base64, glob, json, os, sys, time
from pathlib import Path

import fitz

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
FIG_DIR = PROJECT / "state" / "pdf-figures"
TOC_DIR = PROJECT / "state" / "pdf-toc"
OFFSET_PATH = PROJECT / "state" / "pdf-book-offset.json"
BOOK_FIG_TOGGLE = PROJECT / "state" / "pdf-book-figures.json"
VAULT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))

sys.path.insert(0, str(PROJECT / "scripts"))
import describe_figures as DF   # 复用 _claude_vision / _parse_figs / pdf_sha / _clip
try:
    import figure_dedup as _dedup   # 跨书图像去重缓存(同/极相似图只描述一次)
except Exception:
    _dedup = None


def _rel(pdf_abspath) -> str:
    try:
        return os.path.relpath(os.path.realpath(pdf_abspath), os.path.realpath(VAULT))
    except Exception:
        return ""

def _book_enabled(pdf_abspath) -> bool:
    try:
        return bool(json.loads(BOOK_FIG_TOGGLE.read_text("utf-8")).get(_rel(pdf_abspath), False))
    except Exception:
        return False

def _offset(rel) -> int:
    try:
        return int(json.loads(OFFSET_PATH.read_text("utf-8")).get(rel, 0) or 0)
    except Exception:
        return 0

def _toc_entries(pdf_path, rel) -> list:
    """目录(印刷页):自定义优先;无则原生 get_toc(PDF页→减 offset 归一到印刷页)。"""
    sha = DF.pdf_sha(Path(pdf_path))
    cp = TOC_DIR / f"{sha}.json"
    try:
        cust = json.loads(cp.read_text("utf-8")).get("entries") if cp.exists() else None
    except Exception:
        cust = None
    if cust:
        return cust
    try:
        doc = fitz.open(pdf_path); toc = doc.get_toc() or []; doc.close()
        off = _offset(rel)
        return [{"title": str(t[1]).strip(), "page": int(t[2]) - off, "level": int(t[0])} for t in toc if t[2]]
    except Exception:
        return []

def _location(pdf_path, rel, pdf_page) -> str:
    """provenance:《书名》「该页所属章节」。pdf_page→印刷页=pdf_page-offset,匹配目录印刷页。"""
    bn = Path(pdf_path).stem
    printed = pdf_page - _offset(rel)
    entries = _toc_entries(pdf_path, rel)
    cands = [e for e in entries if e.get("page") and e["page"] <= printed]
    sec = (max(cands, key=lambda e: e["page"]).get("title") or "").strip() if cands else ""
    return f"《{bn}》" + (f"「{sec}」" if sec else "")


def _crop_png(page, bbox, max_px=1400):
    W, H = page.rect.width, page.rect.height
    clip = fitz.Rect(bbox[0] * W, bbox[1] * H, bbox[2] * W, bbox[3] * H)
    if clip.width < 4 or clip.height < 4:
        return None
    z = min(4.0, max_px / (max(clip.width, clip.height) or 1.0))
    return page.get_pixmap(clip=clip, matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")

def _page_png(page, max_px=1540):
    z = min(2.0, max_px / (max(page.rect.width, page.rect.height) or 1.0))
    return page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")


def describe_one_crop(png, ctx, location, model="sonnet"):
    """单图:发裁图 → 一句话描述(明说是截图)。返回 desc 文本或 None。"""
    prompt = (
        f"下面这张图是从 {location} 里**截取出来**的一张插图/示意图/图表。\n"
        f"【这页正文(帮你理解上下文)】{ctx}\n\n"
        "用中文描述**这张截图本身画的内容**:它是什么图、关键要素/结构、在讲什么概念(结合上下文),2~4 句。"
        "数学一律用 $...$。只描述图,别复述正文、别加引号或代码围栏。直接给描述。"
    )
    return DF._claude_vision(prompt, png, model)

def describe_multi(page_png, boxes, ctx, location, model="sonnet"):
    """多图:整页 + 各框坐标 → AI 按框顺序逐张描述。返回 {i: desc} (i 从 0)。"""
    lines = "\n".join(f"  图{i+1}: 归一坐标 [{b[0]:.2f},{b[1]:.2f},{b[2]:.2f},{b[3]:.2f}]" for i, b in enumerate(boxes))
    prompt = (
        f"这是 {location} 的一页,页面上有 {len(boxes)} 张图(插图/示意图/图表),它们在页面上的位置(归一坐标,左上原点)依次是:\n"
        f"{lines}\n"
        f"【这页正文(帮你理解上下文)】{ctx}\n\n"
        "请**按上面图1、图2…的顺序**,为每张图写中文描述(它是什么图、关键要素、讲什么概念,2~4 句,结合上下文)。"
        "数学用 $...$。**只输出一个 JSON 数组**,每条 {\"i\":图序号从1起,\"desc\":\"...\"};"
        "不要 ``` 围栏、不要数组以外任何文字。"
    )
    raw = DF._claude_vision(prompt, page_png, model)
    out = {}
    if not raw:
        return out
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"): s = s[:-3]
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < 0:
        return out
    try:
        arr = json.loads(s[i:j+1])
    except Exception:
        return out
    for e in (arr if isinstance(arr, list) else []):
        try:
            idx = int(e.get("i")) - 1
            d = str(e.get("desc") or "").strip()
            if 0 <= idx < len(boxes) and d:
                out[idx] = d
        except Exception:
            continue
    return out


def process_book(sidecar_path, model="sonnet", force=False, dry=False):
    data = json.loads(open(sidecar_path, encoding="utf-8").read())
    pdf = data.get("pdf")
    if not pdf or not os.path.exists(pdf):
        print(f"  skip(no pdf): {os.path.basename(sidecar_path)}"); return
    if not force and not _book_enabled(pdf):
        print(f"  skip(toggle off): {os.path.basename(sidecar_path)}"); return
    geom = data.get("figures_geom")
    if geom is None:
        print(f"  skip(no figures_geom — 先跑 yolo_figures): {os.path.basename(sidecar_path)}"); return
    rel = _rel(pdf)
    # 待描述的页:该页有 geom 条目且至少一条没 desc
    by_page = {}
    for idx, f in enumerate(geom):
        by_page.setdefault(f.get("page"), []).append(idx)
    todo_pages = [pg for pg, idxs in by_page.items()
                  if pg and any(not (geom[k].get("desc") or "").strip() for k in idxs)]
    todo_pages.sort()
    if not todo_pages:
        print(f"  {os.path.basename(sidecar_path)}: 全部已描述,跳过"); return
    doc = fitz.open(pdf)
    n_one = n_multi = n_fail = n_dedup = 0
    for pg in todo_pages:
        idxs = by_page[pg]
        # 只处理还没 desc 的条目
        need = [k for k in idxs if not (geom[k].get("desc") or "").strip()]
        if not need:
            continue
        page = doc[pg - 1]
        ctx = DF._clip(page.get_text("text"), 700)
        loc = _location(pdf, rel, pg)
        # ── 先用跨书去重缓存:每框裁图查近邻已描述图,命中直接复用(省 AI 视觉调用) ──
        crops = {}; miss = []
        for k in need:
            png = _crop_png(page, geom[k].get("fbox") or geom[k].get("bbox") or [0, 0, 1, 1])
            crops[k] = png
            hit = _dedup.lookup(png) if (_dedup and png) else None
            if hit:
                geom[k]["desc"] = hit[0]; geom[k]["desc_src"] = "dedup"
                geom[k].pop("needs_describe", None); n_dedup += 1
            else:
                miss.append(k)
        # ── 缓存没命中的才调 AI(1 张走单图,多张走整页多图) ──
        if len(miss) == 1:
            k = miss[0]; png = crops[k]
            desc = describe_one_crop(png, ctx, loc, model) if png else None
            if desc:
                geom[k]["desc"] = desc.strip(); geom[k].pop("needs_describe", None); n_one += 1
                if _dedup and png:
                    _dedup.store(png, desc.strip(), book=rel, page=pg)
            else:
                n_fail += 1
        elif len(miss) > 1:
            boxes = [geom[k].get("fbox") or geom[k].get("bbox") or [0, 0, 1, 1] for k in miss]
            res = describe_multi(_page_png(page), boxes, ctx, loc, model)
            got = 0
            for pos, k in enumerate(miss):
                if pos in res and not (geom[k].get("desc") or "").strip():
                    geom[k]["desc"] = res[pos]; geom[k].pop("needs_describe", None); got += 1
                    if _dedup and crops.get(k):
                        _dedup.store(crops[k], res[pos], book=rel, page=pg)
            n_multi += got
            if not got:
                n_fail += 1
        # 每页写一次(断点续传:中途挂了已描述的不丢)
        if not dry:
            data["figures_geom"] = geom
            data["figures_described_at"] = int(time.time())
            tmp = sidecar_path + ".tmp"
            open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
            os.replace(tmp, sidecar_path)
    doc.close()
    print(f"  {os.path.basename(sidecar_path)}: 单图描述 {n_one} / 多图描述 {n_multi} / 复用缓存 {n_dedup} / 失败页 {n_fail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--book", help="PDF abspath; 只处理该书")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--force", action="store_true", help="忽略 per-book 开关")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.book:
        sc = FIG_DIR / f"{DF.pdf_sha(Path(a.book))}.json"
        if not sc.exists():
            print(f"no sidecar: {sc}"); return 2
        process_book(str(sc), a.model, a.force, a.dry)
    elif a.all:
        for sc in sorted(glob.glob(str(FIG_DIR / "*.json"))):
            if sc.endswith((".bak", ".progress.json", ".tmp")):
                continue
            process_book(sc, a.model, a.force, a.dry)
    else:
        print("need --all or --book"); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
