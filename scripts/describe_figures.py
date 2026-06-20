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
    "你在看一页教材的扫描页(第 {page} 页)。结合上下文判图、写描述。\n"
    "{loc}"
    "【本页正文】{ctx}\n"
    "【上一页结尾】{prev}\n"
    "【下一页开头】{nxt}\n\n"
    "找出页面上有**学习价值**的**插图/示意图/曲线图/电路图/几何图/物理装置图/数据表/结构式**"
    "(不含纯文字段落,也不含普通数学公式排版)。\n"
    "**跳过没有学习价值的装帧类图像**:封面、作者/人物照片、出版社丛书缩略图、ISBN 条形码、书脊、"
    "版权页装饰、纯 logo/水印 —— 这些当作'没有插图'(不要列进数组)。\n"
    "**尤其跳过反复出现的卡通吉祥物/角色插画/讲解小人/页眉页脚装饰图标**(教材里常有的卡通形象、"
    "提示框旁的小人、章节装饰角色等)——它们是版式装饰、没有学习内容,一律当作'没有插图',别描述。\n"
    "对每张(有学习价值的)图给一个对象:\n"
    "- caption: 图注/编号原文(如 \"图1-9 空气中的紫罗兰香气分子\";没有就空串)\n"
    "- bbox: 该图在页面上的大致位置 [x0,y0,x1,y1],都是 0~1 比例(左上角原点,x 向右、y 向下),估计即可\n"
    "- desc: 中文说明这张图画的是什么、关键要素、在讲什么概念(结合上下文),2~4 句\n"
    "**只输出一个 JSON 数组**(如 [{{\"caption\":\"...\",\"bbox\":[0.1,0.4,0.6,0.7],\"desc\":\"...\"}}]);"
    "页面没有真正插图就输出 []。不要 ``` 围栏、不要数组以外的任何文字。"
)


def pdf_sha(p: Path) -> str:
    return hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]


def _clip(s: str, n: int) -> str:
    return " ".join((s or "").split())[:n].replace("「", "").replace("」", "")


def _claude_vision(prompt: str, png: bytes, model: str):
    """发一张图 + prompt 给 Claude,返回结果文本;失败 None。"""
    msg = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png).decode()}},
    ]}}
    try:
        proc = subprocess.run(
            [CLAUDE, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", model],
            input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=120)
        for ln in proc.stdout.splitlines():
            if '"type":"result"' in ln:
                return (json.loads(ln).get("result") or "").strip()
    except Exception:
        return None
    return None


def _parse_figs(raw):
    """解析 Claude 回的 JSON 数组 → list[{caption,bbox,desc}]。[] = 无图;None = 解析失败(重试)。"""
    if raw is None:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < 0:
        return None
    try:
        arr = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    out = []
    for f in arr:
        if not isinstance(f, dict):
            continue
        desc = str(f.get("desc") or "").strip()
        if not desc:
            continue
        bb = f.get("bbox")
        if isinstance(bb, list) and len(bb) == 4:
            try:
                bb = [max(0.0, min(1.0, float(v))) for v in bb]
            except Exception:
                bb = None
        else:
            bb = None
        out.append({"caption": str(f.get("caption") or "").strip(), "bbox": bb, "desc": desc})
    return out


def describe_page_figures(pdf_path: str, page_idx: int, model: str = "sonnet", location: str = ""):
    """渲染一页 + 前后页正文作上下文 → Claude 视觉 → 结构化图注 list[{caption,bbox,desc}]。
    [] = 无图;None = 失败(留待重试)。装饰性卡通吉祥物/角色由 PROMPT 在源头判掉(语义,不是像素去重)。
    location = provenance(书名/章节,如『《応用情報》「超上流工程」』),帮 AI 把图放进语境、描述更准。"""
    try:
        doc = fitz.open(pdf_path)
        try:
            n = doc.page_count
            p = doc[page_idx]
            ctx = _clip(p.get_text("text"), 700)
            prev = _clip(doc[page_idx - 1].get_text("text")[-400:], 300) if page_idx > 0 else ""
            nxt = _clip(doc[page_idx + 1].get_text("text")[:400], 300) if page_idx + 1 < n else ""
            z = min(2.0, 1540.0 / (max(p.rect.width, p.rect.height) or 1.0))
            png = p.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")
        finally:
            doc.close()
        loc = (f"【这页在书里的位置】{location}\n" if location else "")
        prompt = PROMPT.format(page=page_idx + 1, ctx=ctx, prev=prev, nxt=nxt, loc=loc)
        return _parse_figs(_claude_vision(prompt, png, model))
    except Exception:
        return None


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
        return pg, describe_page_figures(str(pdf_path), pg - 1, args.model)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, p) for p in todo]
        for fut in as_completed(futs):
            pg, figs = fut.result()
            with lock:
                state["done"] += 1
                if figs is None:
                    pass                      # 失败 → 不记,下次重试
                elif len(figs) == 0:
                    none_pages.add(pg)        # 整页无图
                else:
                    data["figures"] = [f for f in data["figures"] if f.get("page") != pg]
                    for f in figs:
                        data["figures"].append({"page": pg, "caption": f["caption"],
                                                "bbox": f["bbox"], "desc": f["desc"]})
                    state["figs"] += 1
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
