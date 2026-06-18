#!/usr/bin/env python3
"""DocLayout-YOLO 接管插图几何(图留 Pi)。对每个图页跑 YOLO → figure/table 框跟 AI 图注关联写 fbox
(漫画立绘 YOLO 漏检 → 回退 AI bbox);isolate_formula 框存进 sidecar 的 page-level `formulas`,
留给 PC 上的 UniMERNet 转 LaTeX(latex 先置 null)。清掉旧 badge 让 webapp 用新 fbox 重算锚点。

跑在 doclayout-venv:
  /home/bwicarus/doclayout-venv/bin/python scripts/yolo_figures.py --all
  ...                                                              --book "<abs.pdf>"
依赖:doclayout_yolo + huggingface_hub + pymupdf(都在 doclayout-venv)。
"""
import sys, os, json, glob, argparse, time
sys.path.insert(0, "/home/bwicarus/webapp")  # 借 _book_sha 口径(可选)
import fitz

CLASSES = ['title', 'plain text', 'abandon', 'figure', 'figure_caption', 'table',
           'table_caption', 'table_footnote', 'isolate_formula', 'formula_caption']
FIG_CLS = {'figure', 'table'}            # 关联到 AI 图注的几何
FORMULA_CLS = 'isolate_formula'
FIG_DIR = "/home/bwicarus/claude/state/pdf-figures"
BOOK_FIG_TOGGLE = "/home/bwicarus/claude/state/pdf-book-figures.json"
VAULT = os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian")
MODEL_REPO = "juliozhao/DocLayout-YOLO-DocStructBench"
MODEL_FILE = "doclayout_yolo_docstructbench_imgsz1024.pt"


def _book_enabled(pdf_abspath):
    """插图功能是否对这本书开启(默认关)。尊重 per-book 开关,绝不跑关着的书。"""
    try:
        rel = os.path.relpath(os.path.realpath(pdf_abspath), os.path.realpath(VAULT))
        toggles = json.loads(open(BOOK_FIG_TOGGLE, encoding="utf-8").read())
        return bool(toggles.get(rel, False))
    except Exception:
        return False

_MODEL = None
def model():
    global _MODEL
    if _MODEL is None:
        from doclayout_yolo import YOLOv10
        from huggingface_hub import hf_hub_download
        _MODEL = YOLOv10(hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE))
    return _MODEL


def _iou(a, b):
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1]); ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _center_in(p, box):
    cx = (p[0] + p[2]) / 2; cy = (p[1] + p[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _contains(a, b, tol=0.02):
    """a 是否(基本)包住 b。"""
    return a[0] - tol <= b[0] and a[1] - tol <= b[1] and a[2] + tol >= b[2] and a[3] + tol >= b[3]


def dedup_outer(yolo_figs):
    """嵌套去重:被更大的框包住的内部框直接丢弃,只留最外层(用户要的「有组框就只取外层」)。"""
    keep = []
    for i, yf in enumerate(yolo_figs):
        inner = False
        for j, other in enumerate(yolo_figs):
            if i != j and _contains(other[0], yf[0]) and _area(other[0]) > _area(yf[0]) * 1.05:
                inner = True; break
        if not inner:
            keep.append(yf)
    return keep


def badge_topright(fbox):
    """徽标中心 = 图框右上角顶点(中心与右上角重叠,徽标骑在角上)。归一。"""
    x0, y0, x1, y1 = fbox
    return [round(max(0.005, min(0.995, x1)), 4), round(max(0.005, min(0.995, y0)), 4)]


def detect_page(pg, conf=0.2):
    """→ {'fig':[(bbox_norm,conf,cls)], 'formula':[(bbox_norm,conf)]}, 归一坐标。"""
    pr = pg.rect; W = float(pr.width); H = float(pr.height)
    s = 1024.0 / max(W, H)
    pix = pg.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
    from PIL import Image
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    det = model().predict(img, imgsz=1024, conf=conf, device="cpu", verbose=False)[0]
    figs = []; formulas = []
    for b, c, cf in zip(det.boxes.xyxy.tolist(), det.boxes.cls.tolist(), det.boxes.conf.tolist()):
        cls = CLASSES[int(c)] if int(c) < len(CLASSES) else str(int(c))
        nb = [round(b[0] / pix.width, 4), round(b[1] / pix.height, 4),
              round(b[2] / pix.width, 4), round(b[3] / pix.height, 4)]
        if cls in FIG_CLS:
            figs.append((nb, round(cf, 3), cls))
        elif cls == FORMULA_CLS:
            formulas.append((nb, round(cf, 3)))
    return {"fig": figs, "formula": formulas}


def regroup_page(page, ai_figs, yolo_figs):
    """嵌套去重 → 每条 AI 图归属到最匹配的「最外层」YOLO 框(可多对一)→ 同框多图合成「图组」。
    返回该页**新的** figure 条目列表(图组为单条,标 needs_redescribe 待 Claude 重描述整组)。"""
    outer = dedup_outer(yolo_figs)
    assign = {}                                  # ai_i -> outer_i
    for ai_i, f in enumerate(ai_figs):
        ab = f.get("bbox")
        if not ab:
            continue
        best = -1.0; bi = -1
        for oi, (yb, yc, ycls) in enumerate(outer):
            sc = _iou(ab, yb)
            if _center_in(ab, yb):
                sc += 0.4                         # AI 图中心落在外框内 = 强归属(允许多图共享)
            elif _center_in(yb, ab):
                sc += 0.2
            if sc > best:
                best = sc; bi = oi
        if bi >= 0 and best > 0.05:
            assign[ai_i] = bi
    from collections import defaultdict
    members_of = defaultdict(list)
    for ai_i, oi in assign.items():
        members_of[oi].append(ai_i)
    out = []; used_ai = set()
    for oi, ai_idxs in members_of.items():
        yb, yc, ycls = outer[oi]
        for i in ai_idxs:
            used_ai.add(i)
        if len(ai_idxs) == 1:                     # 单图:直接用外框
            f = dict(ai_figs[ai_idxs[0]])
            f["fbox"] = yb; f["fsrc"] = "yolo"; f["fconf"] = yc; f["fcls"] = ycls
            f["badge"] = badge_topright(yb)
            for k in ("group", "members", "needs_redescribe"):
                f.pop(k, None)
            out.append(f)
        else:                                     # 同一外框多图 → 合成图组,标记重描述
            mem = [{"caption": ai_figs[i].get("caption", ""), "desc": ai_figs[i].get("desc", "")}
                   for i in sorted(ai_idxs)]
            caps = [m["caption"] for m in mem if m["caption"]]
            out.append({
                "page": page, "bbox": yb, "fbox": yb, "fsrc": "yolo", "fconf": yc, "fcls": ycls,
                "group": True, "members": mem, "needs_redescribe": True,
                "caption": "（图组）" + " / ".join(caps) if caps else "图组",
                "desc": "\n\n".join((f"**{m['caption']}** " + m["desc"]).strip() for m in mem),  # 临时拼接,等重描述覆盖
                "badge": badge_topright(yb),
            })
    for ai_i, f in enumerate(ai_figs):            # 没归到任何外框(漫画立绘等)→ 保留,fbox=AI bbox
        if ai_i in used_ai:
            continue
        g = dict(f); g["fbox"] = f.get("bbox"); g["fsrc"] = "ai_fallback"; g.pop("fconf", None)
        g["badge"] = badge_topright(g["fbox"]) if g.get("fbox") else None
        for k in ("group", "members", "needs_redescribe"):
            g.pop(k, None)
        out.append(g)
    return out


def process_sidecar(path, dry=False, force=False):
    data = json.loads(open(path, encoding="utf-8").read())
    pdf = data.get("pdf")
    if not pdf or not os.path.exists(pdf):
        print(f"  skip(no pdf): {os.path.basename(path)}"); return
    if not force and not _book_enabled(pdf):       # 默认关 → 不跑(除非 --force)
        print(f"  skip(toggle off): {os.path.basename(path)}"); return
    all_figs = data.get("figures", [])
    figs = [f for f in all_figs if f.get("bbox")]
    no_bbox = [f for f in all_figs if not f.get("bbox")]      # 无 bbox 的(罕见)原样保留
    by_page = {}
    for f in figs:
        by_page.setdefault(f["page"], []).append(f)
    doc = fitz.open(pdf)
    formulas_all = []
    new_figs = list(no_bbox)
    nyolo = ngrp = nsingle = nfa = 0
    # 图框出界的页(罕见)原样保留,从 by_page 摘出去
    for pn in list(by_page):
        if pn < 1 or pn > doc.page_count:
            new_figs.extend(by_page.pop(pn))
    # ⚠ 公式检测**跑全书每一页**:原来只在"有 AI 图注的页"上跑 YOLO → 无图的纯公式页永远检测不到
    #   = 整本绝大多数公式被漏(费曼全本 588 页只识出 21 个,全在第 7 章有图的页)。
    #   图/图组重排仍只在有图注的页做(regroup 依赖 AI figures);公式框则全页扫。
    fig_pages = set(by_page)
    for pn in range(1, doc.page_count + 1):
        det = detect_page(doc[pn - 1])
        if pn in fig_pages:
            nyolo += len(det["fig"])
            entries = regroup_page(pn, by_page[pn], det["fig"])
            for e in entries:
                if e.get("group"): ngrp += 1
                elif e.get("fsrc") == "yolo": nsingle += 1
                else: nfa += 1
            new_figs.extend(entries)
        for (fb, fc) in det["formula"]:
            formulas_all.append({"page": pn, "bbox": fb, "conf": fc, "latex": None})
    doc.close()
    # 保留旧 latex:重跑会重建 formulas 列表(全 latex=None)→ 会抹掉已 OCR/Claude 校正过的结果。
    # 新框跟旧框(同页 + IoU≥0.6)匹配上 → 搬运旧 latex/engine,免得重测+重校正。
    old_fmls = data.get("formulas") or []
    for nf in formulas_all:
        best, bi = 0.0, -1
        for j, of in enumerate(old_fmls):
            if of.get("page") != nf["page"] or not (of.get("latex") or "").strip():
                continue
            ov = _iou(nf["bbox"], of.get("bbox") or [0, 0, 0, 0])
            if ov > best: best, bi = ov, j
        if bi >= 0 and best >= 0.6:
            nf["latex"] = old_fmls[bi].get("latex")
            if old_fmls[bi].get("latex_engine"):
                nf["latex_engine"] = old_fmls[bi]["latex_engine"]
    n_kept = sum(1 for f in formulas_all if (f.get("latex") or "").strip())
    data["figures_geom"] = new_figs            # 派生层:几何+图组(从干净的 AI figures 重算,幂等);不动 data["figures"]
    data["formulas"] = formulas_all
    data["geom"] = "yolo"
    data["geom_at"] = int(time.time())
    if not dry:
        tmp = path + ".tmp"
        open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    print(f"  {os.path.basename(path)}: in={len(figs)} yolo_boxes={nyolo} → out={len(new_figs)} (单图yolo:{nsingle} 图组:{ngrp} 回退ai:{nfa}) formulas={len(formulas_all)}(沿用旧latex {n_kept})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--book", help="PDF abspath; 只处理该书的 sidecar")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略 per-book 开关(调试用)")
    a = ap.parse_args()
    sidecars = []
    for p in sorted(glob.glob(os.path.join(FIG_DIR, "*.json"))):
        if "progress" in p:
            continue
        if a.book:
            try:
                if json.loads(open(p, encoding="utf-8").read()).get("pdf") != a.book:
                    continue
            except Exception:
                continue
        sidecars.append(p)
    if not a.all and not a.book:
        print("need --all or --book"); return 2
    print(f"processing {len(sidecars)} sidecar(s)...")
    t0 = time.time()
    for p in sidecars:
        try:
            process_sidecar(p, dry=a.dry, force=a.force)
        except Exception as e:
            print(f"  ERROR {os.path.basename(p)}: {e}")
    print(f"done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
