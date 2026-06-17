#!/usr/bin/env python3
"""给 yolo_figures.py 合成的「图组」(figures_geom 里 group=true 且 needs_redescribe)重新调 Claude 视觉,
把整组当一张图描述成连贯的一段,覆盖临时拼接的 desc。跑在主环境(/usr/bin/python3,有 fitz + Claude CLI)。

  python3 scripts/redescribe_groups.py --all          # 处理所有(开关开着的)书的待重描述图组
  python3 scripts/redescribe_groups.py --book "<abs.pdf>"
  python3 scripts/redescribe_groups.py --all --force   # 忽略 per-book 开关
"""
import sys, os, json, glob, argparse, time

CLAUDE_DIR = os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude")
sys.path.insert(0, os.path.join(CLAUDE_DIR, "scripts"))
FIG_DIR = os.path.join(CLAUDE_DIR, "state", "pdf-figures")
BOOK_FIG_TOGGLE = os.path.join(CLAUDE_DIR, "state", "pdf-book-figures.json")
VAULT = os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian")

PROMPT = (
    "下面这张图是某教材第 {page} 页上**一组相关插图**裁在一起(图组,可能含多张子图/示意图/分子图/表格)。\n"
    "【本页正文片段】{ctx}\n"
    "这组里各部分原有的图注/说明(仅供参考,别逐条复述):\n{members}\n\n"
    "请把整组当作一个整体,用 2~4 句**中文**说明:这组图整体画的是什么、有哪些部分、各部分之间的关系、在讲什么概念。"
    "综合成连贯的一段,不要前后缀、不要 markdown 标题。只输出这段描述。"
)


def _book_enabled(pdf):
    try:
        rel = os.path.relpath(os.path.realpath(pdf), os.path.realpath(VAULT))
        return bool(json.loads(open(BOOK_FIG_TOGGLE, encoding="utf-8").read()).get(rel, False))
    except Exception:
        return False


def _page_ctx(pg, limit=600):
    try:
        return (pg.get_text("text") or "").replace("\n", " ").strip()[:limit]
    except Exception:
        return ""


def process(path, model="sonnet", force=False, dry=False):
    import fitz
    import describe_figures as DF
    data = json.loads(open(path, encoding="utf-8").read())
    pdf = data.get("pdf")
    if not pdf or not os.path.exists(pdf):
        print(f"  skip(no pdf): {os.path.basename(path)}"); return
    if not force and not _book_enabled(pdf):
        print(f"  skip(toggle off): {os.path.basename(path)}"); return
    geom = data.get("figures_geom")
    if not geom:
        print(f"  skip(no figures_geom — 先跑 yolo_figures): {os.path.basename(path)}"); return
    todo = [f for f in geom if f.get("group") and f.get("needs_redescribe") and f.get("fbox")]
    if not todo:
        print(f"  {os.path.basename(path)}: 无待重描述图组"); return
    doc = fitz.open(pdf); done = 0
    for f in todo:
        pn = f.get("page"); fb = f["fbox"]
        if not pn or pn < 1 or pn > doc.page_count:
            continue
        pg = doc[pn - 1]; pr = pg.rect; W = pr.width; H = pr.height
        clip = fitz.Rect(fb[0] * W, fb[1] * H, fb[2] * W, fb[3] * H)
        z = 2.0
        png = pg.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip, alpha=False).tobytes("png")
        members = "\n".join(f"- {m.get('caption', '')}: {m.get('desc', '')}".strip() for m in (f.get("members") or []))
        prompt = PROMPT.format(page=pn, ctx=_page_ctx(pg), members=members or "（无）")
        out = DF._claude_vision(prompt, png, model)
        if out and len(out.strip()) > 4:
            f["desc"] = out.strip()
            f["needs_redescribe"] = False
            done += 1
            print(f"    p{pn} 图组重描述 ✓ ({len(out.strip())}字)")
        else:
            print(f"    p{pn} 图组重描述 ✗(保留拼接 desc)")
    doc.close()
    if done and not dry:
        tmp = path + ".tmp"
        open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    print(f"  {os.path.basename(path)}: 图组 {len(todo)} 个,重描述成功 {done}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--book")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    cards = []
    for p in sorted(glob.glob(os.path.join(FIG_DIR, "*.json"))):
        if "progress" in p:
            continue
        if a.book:
            try:
                if json.loads(open(p, encoding="utf-8").read()).get("pdf") != a.book:
                    continue
            except Exception:
                continue
        cards.append(p)
    if not a.all and not a.book:
        print("need --all or --book"); return 2
    t0 = time.time()
    for p in cards:
        try:
            process(p, model=a.model, force=a.force, dry=a.dry)
        except Exception as e:
            print(f"  ERROR {os.path.basename(p)}: {e}")
    print(f"done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
