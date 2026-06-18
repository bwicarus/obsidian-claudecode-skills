#!/usr/bin/env python3
"""批处理跑公式 OCR:渲染本书"还没 latex"的公式框 → 发 PC pix2tex(/ocr)→ 增量写回 sidecar。
幂等、可断点续(已填的跳过,每批原子存一次)。比 web 端点 /api/formula-ocr 更稳(分批,不一次塞全部)。
用法: /usr/bin/python3 scripts/run_formula_ocr.py --book "<abs.pdf>" [--batch 40] [--url http://100.99.9.124:8765]
"""
import sys, os, json, base64, argparse, time, hashlib
from pathlib import Path
import fitz
import requests


def book_sha(p):
    return hashlib.sha1(str(Path(p).resolve()).encode("utf-8")).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--url", default=os.environ.get("FORMULA_OCR_URL", "http://100.99.9.124:8765"))
    ap.add_argument("--sidecar", default=None)
    a = ap.parse_args()

    sc = a.sidecar or os.path.join("state", "pdf-figures", book_sha(a.book) + ".json")
    data = json.load(open(sc, encoding="utf-8"))
    fmls = data.get("formulas") or []
    doc = fitz.open(a.book)
    mat = fitz.Matrix(4, 4)

    def render(f):   # 跟 /api/formula-ocr 同样的裁切(4x + 同 padding)
        page = doc[int(f["page"]) - 1]
        W, H = page.rect.width, page.rect.height
        x0, y0, x1, y1 = f["bbox"]
        bx0, by0, bx1, by1 = x0 * W, y0 * H, x1 * W, y1 * H
        padh = max(4.0, 0.02 * (bx1 - bx0))
        padv = max(4.0, 0.06 * (by1 - by0))
        clip = fitz.Rect(max(0, bx0 - padh), max(0, by0 - padv), min(W, bx1 + padh), min(H, by1 + padv))
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        return base64.b64encode(pix.tobytes("png")).decode("ascii")

    todo = [i for i, f in enumerate(fmls)
            if not (f.get("latex") or "").strip() and f.get("bbox") and len(f["bbox"]) == 4]
    print(f"待 OCR: {len(todo)} / {len(fmls)}", flush=True)
    done = 0
    for s in range(0, len(todo), a.batch):
        idxs = todo[s:s + a.batch]
        crops = [{"idx": i, "png_b64": render(fmls[i])} for i in idxs]
        try:
            r = requests.post(a.url + "/ocr", json={"crops": crops, "key": os.environ.get("FORMULA_OCR_KEY", "")}, timeout=180)
            res = (r.json() or {}).get("results", [])
        except Exception as e:
            print(f"  批 {s // a.batch + 1} 失败(跳过):{str(e)[:90]}", flush=True)
            time.sleep(2)
            continue
        n = 0
        for it in res:
            i = it.get("idx"); lx = it.get("latex")
            if isinstance(i, int) and 0 <= i < len(fmls) and lx and str(lx).strip():
                fmls[i]["latex"] = str(lx).strip()
                fmls[i]["latex_engine"] = "pix2tex-local"
                n += 1
        done += n
        tmp = sc + ".tmp"   # 增量原子写回 → 断了重跑接着填
        open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, sc)
        print(f"  批 {s // a.batch + 1}/{(len(todo) + a.batch - 1) // a.batch}: +{n}  累计 {done}/{len(todo)}", flush=True)
    doc.close()
    print(f"完成: 共填 {done} 个 latex", flush=True)


if __name__ == "__main__":
    main()
