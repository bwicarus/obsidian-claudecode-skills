#!/usr/bin/env python3
"""页级图注:逐页让视觉模型(Claude)判定有无插图并描述,存 sidecar 供全文搜索/阅读器/助手用。

为什么不靠几何检测:扫描书整页是一张图(get_images=1),插图常和正文并排,空白连通,
几何"找大块无文字区"在并排图上根本分不开(版面分析硬骨头)。视觉模型能"看见"图,
直接判定+描述远更可靠(实测:有图页给出准确图注,纯文字页回 NONE)。

sidecar: state/pdf-figures/<sha-of-pdf-abspath>.json
  {pdf, model, updated_at, figures: [{page, description}]}   # 只存有图的页
断点续传:已在 sidecar(或本次 NONE 记录)的页跳过。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz

PROJECT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
OUT_DIR = PROJECT / "state" / "pdf-figures"
CLAUDE = os.environ.get("APP_CLAUDE") or "claude"

PROMPT = (
    "你在看一页教材扫描页。本页正文(供上下文):「{ctx}」。\n"
    "请只针对页面上的**插图/图表/示意图/曲线图/电路图/几何图/物理装置图/数据图表**"
    "(不含纯文字段落,也不含数学公式排版)用中文说明:每张图画的是什么、关键要素、在讲什么概念;"
    "有图注/编号(如 图1-9)就带上。多张图分点列。\n"
    "若整页没有真正的插图,只输出 NONE 这四个字母,别的都不要。"
)


def pdf_sha(p: Path) -> str:
    return hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]


def describe_page(pdf_path: str, page_idx: int, model: str) -> str:
    """渲染一页 → Claude 视觉 → 描述文本或 'NONE'。失败返回 ''(留待重试)。"""
    try:
        doc = fitz.open(pdf_path)
        try:
            p = doc[page_idx]
            ctx = (p.get_text("text") or "")[:280].replace("\n", " ").replace("「", "").replace("」", "")
            z = min(2.0, 1540.0 / (max(p.rect.width, p.rect.height) or 1.0))
            png = p.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")
        finally:
            doc.close()
        msg = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": PROMPT.format(ctx=ctx)},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}},
        ]}}
        proc = subprocess.run(
            [CLAUDE, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", model],
            input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=120)
        for ln in proc.stdout.splitlines():
            if '"type":"result"' in ln:
                try:
                    return (json.loads(ln).get("result") or "").strip()
                except Exception:
                    return ""
        return ""
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default=None, help="'10' / '10,80' / '10-20';默认全本")
    ap.add_argument("--workers", type=int, default=4, help="并发(Claude 订阅有限流,别太大)")
    ap.add_argument("--model", default="sonnet")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2
    sha = pdf_sha(pdf_path)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{sha}.json"
    prog_path = OUT_DIR / f"{sha}.progress.json"

    data = {"pdf": str(pdf_path), "model": args.model, "updated_at": 0, "figures": []}
    if out_path.exists():
        try:
            data = json.loads(out_path.read_text("utf-8"))
        except Exception:
            pass
    done_pages = {f["page"] for f in data.get("figures", [])}
    none_pages = set(data.get("_none_pages", []))   # 记 NONE 的页也算处理过(断点续传)

    doc = fitz.open(str(pdf_path))
    n = doc.page_count
    doc.close()
    if args.pages:
        sel = set()
        for tok in args.pages.split(","):
            tok = tok.strip()
            if "-" in tok:
                lo, hi = tok.split("-"); sel.update(range(int(lo), int(hi) + 1))
            elif tok:
                sel.add(int(tok))
        pages = [p for p in sorted(sel) if 1 <= p <= n]
    else:
        pages = list(range(1, n + 1))
    todo = [p for p in pages if p not in done_pages and p not in none_pages]
    print(f"PDF {pdf_path.name} 共 {n} 页;待处理 {len(todo)}(已 {len(pages) - len(todo)})", flush=True)

    lock = __import__("threading").Lock()
    state = {"done": 0, "figs": len(done_pages)}

    def save():
        data["updated_at"] = int(time.time())
        data["_none_pages"] = sorted(none_pages)
        data["figures"].sort(key=lambda f: f["page"])
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(out_path)

    def work(pg):
        desc = describe_page(str(pdf_path), pg - 1, args.model)
        return pg, desc

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, p) for p in todo]
        for fut in as_completed(futs):
            pg, desc = fut.result()
            with lock:
                state["done"] += 1
                if desc and desc.strip().upper() != "NONE" and len(desc.strip()) > 4:
                    data["figures"] = [f for f in data["figures"] if f["page"] != pg]
                    data["figures"].append({"page": pg, "description": desc.strip()})
                    state["figs"] += 1
                elif desc.strip().upper() == "NONE":
                    none_pages.add(pg)
                # desc=='' (失败) 不记 → 下次重试
                if state["done"] % 5 == 0 or state["done"] == len(todo):
                    save()
                    eta = (time.time() - t0) / max(1, state["done"]) * (len(todo) - state["done"])
                    prog_path.write_text(json.dumps({"done": state["done"], "total": len(todo),
                                                     "figs": state["figs"], "eta_min": round(eta / 60, 1)}), "utf-8")
                    print(f"  {state['done']}/{len(todo)}  有图页累计 {state['figs']}  ETA {eta/60:.1f}min", flush=True)
    save()
    print(f"完成:{len(data['figures'])} 个有图页 → {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
