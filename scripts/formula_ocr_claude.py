#!/usr/bin/env python3
"""公式 OCR via Claude 视觉(方案3:每次喂多张**独立**图 + 编号 + JSON 输出 + 数量校验)。
渲染本书"还没 latex"的公式框 → claude CLI 看图 → [{idx,latex}] → 写回 sidecar。
pix2tex 对中文公式吐乱码,这个用 Claude 视觉,中文混排出 \text{}。幂等可断点续(每批原子写回)。
用法: scripts/formula_ocr_claude.py --book "<abs>" [--model sonnet|opus] [--effort low] [--batch 8]
       [--limit N] [--only-pages 54,55] [--dpi 6] [--dry]   # --dry 只打印+存crop到/tmp/fml_test,不写回
"""
import sys, os, json, base64, argparse, time, hashlib, subprocess, select, re
from pathlib import Path
import fitz

CLA = os.environ.get("APP_CLAUDE", "/home/bwicarus/.local/bin/claude")
INSTR = ("下面是从中文物理教材扫描页裁出的若干**独立公式**图片,按「图N:」编号(N 从 0 起)。"
         "请把每张图转写成 LaTeX:纯数学符号用标准 LaTeX;公式里的**中文文字**用 \\text{} 包(如 \\text{重量}、\\text{常数});"
         "单位(oz/in/ft/kg/s 等)用 \\mathrm{};上下标/分式/根号/积分照实写。"
         "**只转写图中真实存在的内容,不要补全、不要解释、不要算**;若某图不是公式(纯文字/噪声)其 latex 置 null。"
         "**只输出一个 JSON 数组**,格式 [{\"idx\":0,\"latex\":\"...\"}, ...],idx 对应图号,每张图都要有一项,别加任何别的字。")


def book_sha(p):
    return hashlib.sha1(str(Path(p).resolve()).encode("utf-8")).hexdigest()[:16]


def ask_vision(blocks, model, effort, timeout=200):
    p = subprocess.Popen(
        [CLA, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
         "--disallowedTools", "Bash Edit Write Read NotebookEdit WebFetch WebSearch Glob Grep Task",
         "--verbose", "--model", model, "--effort", effort],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n")
        p.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            r, _, _ = select.select([p.stdout], [], [], 0.5)
            if r:
                ln = p.stdout.readline()
                if not ln:
                    break
                if '"type":"result"' in ln:
                    try:
                        return (json.loads(ln).get("result") or "").strip()
                    except Exception:
                        return None
            if p.poll() is not None:
                break
        return None
    finally:
        try:
            p.kill()
        except Exception:
            pass


def parse_arr(txt):
    if not txt:
        return None
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    m = re.search(r'\[.*\]', s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-pages", default="")
    ap.add_argument("--dpi", type=float, default=6.0)   # 小公式提高倍率保清晰
    ap.add_argument("--sidecar", default=None)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    sc = a.sidecar or os.path.join("state", "pdf-figures", book_sha(a.book) + ".json")
    data = json.load(open(sc, encoding="utf-8"))
    fmls = data.get("formulas") or []
    doc = fitz.open(a.book)
    only = set(int(x) for x in a.only_pages.split(",") if x.strip()) if a.only_pages else None
    if a.dry:
        os.makedirs("/tmp/fml_test", exist_ok=True)

    def render_png(f):
        page = doc[int(f["page"]) - 1]
        W, H = page.rect.width, page.rect.height
        x0, y0, x1, y1 = f["bbox"]
        bx0, by0, bx1, by1 = x0 * W, y0 * H, x1 * W, y1 * H
        padh = max(4.0, 0.02 * (bx1 - bx0)); padv = max(4.0, 0.08 * (by1 - by0))
        clip = fitz.Rect(max(0, bx0 - padh), max(0, by0 - padv), min(W, bx1 + padh), min(H, by1 + padv))
        return page.get_pixmap(matrix=fitz.Matrix(a.dpi, a.dpi), clip=clip, alpha=False).tobytes("png")

    todo = [i for i, f in enumerate(fmls)
            if not (f.get("latex") or "").strip() and f.get("bbox") and len(f["bbox"]) == 4
            and (only is None or f.get("page") in only)]
    if a.limit:
        todo = todo[:a.limit]
    print(f"待 OCR: {len(todo)}  模型={a.model}·{a.effort}  batch={a.batch}  dpi={a.dpi}{'  [DRY]' if a.dry else ''}", flush=True)

    done = 0
    for s in range(0, len(todo), a.batch):
        idxs = todo[s:s + a.batch]
        blocks = [{"type": "text", "text": INSTR}]
        for k, i in enumerate(idxs):
            png = render_png(fmls[i])
            if a.dry:
                open(f"/tmp/fml_test/{i}_p{fmls[i]['page']}.png", "wb").write(png)
            blocks.append({"type": "text", "text": f"图{k}:"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                       "data": base64.b64encode(png).decode("ascii")}})
        out = ask_vision(blocks, a.model, a.effort)
        arr = parse_arr(out)
        if not arr or not isinstance(arr, list):
            print(f"  批 {s // a.batch + 1}: 解析失败(返回:{(out or '')[:80]})", flush=True)
            continue
        by_k = {}
        for it in arr:
            if isinstance(it, dict) and isinstance(it.get("idx"), int):
                by_k[it["idx"]] = it.get("latex")
        n = 0
        for k, i in enumerate(idxs):
            lx = by_k.get(k)
            ok = lx and str(lx).strip() and str(lx).strip().lower() != "null"
            if a.dry:
                print(f"   全局#{i} p{fmls[i]['page']} → {str(lx)[:90] if ok else '(null/空)'}", flush=True)
            elif ok:
                fmls[i]["latex"] = str(lx).strip()
                fmls[i]["latex_engine"] = f"claude-{a.model}"
                n += 1
        done += n
        if not a.dry:
            tmp = sc + ".tmp"
            open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
            os.replace(tmp, sc)
        cnt_warn = "" if len(arr) == len(idxs) else f" ⚠返回数{len(arr)}≠{len(idxs)}"
        print(f"  批 {s // a.batch + 1}/{(len(todo) + a.batch - 1) // a.batch}: 返回{len(arr)} 填{n}  累计{done}/{len(todo)}{cnt_warn}", flush=True)
    doc.close()
    print(f"完成: {'(dry)' if a.dry else '填了 ' + str(done) + ' 个 latex'}", flush=True)


if __name__ == "__main__":
    main()
