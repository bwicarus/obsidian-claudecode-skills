#!/usr/bin/env python3
"""把 pdf-figures sidecar 里的公式框(归一化 bbox)从源 PDF 渲成高 DPI crop PNG,供公式 OCR(UniMERNet 等)。

用法:
  python formula_crops.py <sidecar.json|sha> [--outdir DIR] [--zoom 4.0] [--padv 0.06] [--padh 0.02]

输出:
  <outdir>/f00.png ... fNN.png  + manifest.json(idx→page/bbox/png/w/h)
默认 outdir = state/formula-crops/<sha>/
扫描版 PDF 的公式是位图,放大裁剪 + 少量留白,UniMERNet 对真实/扫描公式鲁棒。
"""
import sys, os, json, argparse
from pathlib import Path
import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
SIDECAR_DIR = PROJ / "state" / "pdf-figures"


def resolve_sidecar(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    cand = SIDECAR_DIR / (arg if arg.endswith(".json") else arg + ".json")
    if cand.exists():
        return cand
    raise SystemExit(f"sidecar not found: {arg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sidecar")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--zoom", type=float, default=4.0)
    ap.add_argument("--padv", type=float, default=0.06, help="vertical pad as fraction of box height")
    ap.add_argument("--padh", type=float, default=0.02, help="horizontal pad as fraction of box width")
    ap.add_argument("--minpad", type=float, default=4.0, help="min pad in PDF points")
    args = ap.parse_args()

    sc_path = resolve_sidecar(args.sidecar)
    data = json.loads(sc_path.read_text("utf-8"))
    pdf_path = data.get("pdf")
    formulas = data.get("formulas") or []
    if not pdf_path or not os.path.exists(pdf_path):
        raise SystemExit(f"pdf missing: {pdf_path}")
    if not formulas:
        raise SystemExit("no formulas in sidecar")

    sha = sc_path.stem
    outdir = Path(args.outdir) if args.outdir else (PROJ / "state" / "formula-crops" / sha)
    outdir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(args.zoom, args.zoom)
    manifest = {"pdf": pdf_path, "sha": sha, "zoom": args.zoom, "items": []}

    for idx, f in enumerate(formulas):
        page_no = int(f["page"])            # 1-based
        x0, y0, x1, y1 = f["bbox"]          # normalized 0..1
        page = doc[page_no - 1]
        pr = page.rect
        W, H = pr.width, pr.height
        bx0, by0, bx1, by1 = x0 * W, y0 * H, x1 * W, y1 * H
        bw, bh = max(1.0, bx1 - bx0), max(1.0, by1 - by0)
        padh = max(args.minpad, args.padh * bw)
        padv = max(args.minpad, args.padv * bh)
        clip = fitz.Rect(max(0, bx0 - padh), max(0, by0 - padv),
                         min(W, bx1 + padh), min(H, by1 + padv))
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        name = f"f{idx:02d}.png"
        pix.save(str(outdir / name))
        manifest["items"].append({
            "idx": idx, "png": name, "page": page_no, "bbox": [x0, y0, x1, y1],
            "conf": f.get("conf"), "w": pix.width, "h": pix.height,
        })
        print(f"[{idx:02d}] p{page_no} -> {name}  {pix.width}x{pix.height}", flush=True)

    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n{len(formulas)} crops -> {outdir}", flush=True)
    print(f"MANIFEST {outdir/'manifest.json'}", flush=True)


if __name__ == "__main__":
    main()
