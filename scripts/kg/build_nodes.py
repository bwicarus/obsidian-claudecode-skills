"""
kg/build_nodes.py — 从 PDF 抽取知识图谱节点（三层结构）。

L0/L1 直接用 PDF TOC（确定性、零 AI 成本）：
  L0 = TOC level 1（章）
  L1 = TOC level 2（节，如 1A、1B）

L2（原子知识点：定义/定理/方法/例）按页用 AI（sonnet）抽取，挂到所在 L1 上。

输出 knowledge_graph/<book>.json，节点结构：
{
  "book": "LADR",
  "pdf": "...",
  "nodes": [
    {"id":"ladr.l0.ch1","level":0,"parent_id":null,"name":"向量空间","pages":[11,33]},
    {"id":"ladr.l1.1A","level":1,"parent_id":"ladr.l0.ch1","name":"R^n和C^n","pages":[12,19]},
    {"id":"ladr.l2.<numeric>","level":2,"parent_id":"ladr.l1.1A","type":"definition","name":"复数","summary":"...","pages":[12],"numeric_label":""}
  ],
  "edges": []   # 由 extract_edges.py 填
}

用法：
  python3 scripts/kg/build_nodes.py --pdf <path> --book LADR
    [--pages START-END]   只跑这个页范围（试水）
    [--limit N]           只跑 TOC 中前 N 个 L1 section（试水）
    [--model sonnet|opus]
    [--effort low|medium|high|max]
    [--dry-run]           只生成 L0/L1 骨架，不调 AI
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402

KG_DIR = config.PROJECT_DIR / "knowledge_graph"
PAGE_CACHE = config.PROJECT_DIR / "state" / "kg" / "page-cache"
DPI = 144  # 公式清晰 + token 节省的折中


def render_page_png(doc, page_idx_0based: int, book_id: str) -> bytes:
    """渲染指定页（0-indexed）为 PNG bytes；本地缓存。"""
    PAGE_CACHE.mkdir(parents=True, exist_ok=True)
    cache = PAGE_CACHE / f"{book_id}-p{page_idx_0based + 1}.png"
    if cache.exists():
        return cache.read_bytes()
    page = doc[page_idx_0based]
    mat = fitz.Matrix(DPI / 72, DPI / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    data = pix.tobytes("png")
    cache.write_bytes(data)
    return data


def build_skeleton_from_toc(doc, book_id: str, pdf_path: str) -> dict:
    """从 TOC 抽 L0（章）+ L1（节）。返回完整 KG dict。"""
    toc = doc.get_toc()  # [[level, name, page_1based], ...]
    n_pages = doc.page_count
    # 为每个条目算结束页：下一同层级或更高层级条目的页 - 1
    items: list[dict] = []
    for i, (lvl, name, pg) in enumerate(toc):
        end = n_pages
        for j in range(i + 1, len(toc)):
            if toc[j][0] <= lvl:
                end = toc[j][2] - 1
                break
        items.append({"toc_level": lvl, "name": name.strip(), "page_start": pg, "page_end": end})

    nodes: list[dict] = []
    current_l0_id = None
    for it in items:
        if it["toc_level"] == 1:
            # L0 章。命名形如 "第 1 章 向量空间"
            m = re.match(r"第\s*([0-9A-Za-z一二三四五六七八九十]+)\s*章\s*(.+)", it["name"])
            if not m:
                continue  # 跳过「译者序」「目录」等非正文条目
            chap_id = m.group(1)
            current_l0_id = f"{book_id.lower()}.l0.ch{chap_id}"
            nodes.append({
                "id": current_l0_id, "level": 0, "parent_id": None,
                "name": m.group(2).strip(),
                "chapter_label": f"第 {chap_id} 章",
                "pages": [it["page_start"], it["page_end"]],
            })
        elif it["toc_level"] == 2 and current_l0_id:
            # L1 节。命名形如 "1A R^n和C^n"
            m = re.match(r"^([0-9]+[A-Za-z]+)\s+(.+)", it["name"])
            sec_label = m.group(1) if m else it["name"][:6]
            sec_name = m.group(2).strip() if m else it["name"]
            nodes.append({
                "id": f"{book_id.lower()}.l1.{sec_label}",
                "level": 1, "parent_id": current_l0_id,
                "name": sec_name, "section_label": sec_label,
                "pages": [it["page_start"], it["page_end"]],
            })
        # TOC level >=3 不建独立节点，让 AI 抽 L2 时自然覆盖

    return {"book": book_id, "pdf": str(pdf_path), "nodes": nodes, "edges": []}


_EXTRACT_PROMPT = """你正在为教材建立知识图谱。书：《{book_full}》
当前位置：{chapter_label}「{chapter_name}」→ 第 {section_label} 节「{section_name}」→ 第 {page} 页

**严格只抽**这一页里**教材正式编号的**条目（标题里就带编号那种）：
- 定义（编号如 1.4 / 2.13 / "Definition 1.10"）
- 定理 / 命题 / 推论 / 引理（编号同上）
- 教材作为"标志性记号/术语"反复使用、且明确给出编号的对象

**不要抽**：
- 没有教材编号的小术语（"标量""向量""零元""减法""F 的含义"这种顺带提及）
- 习题（X.A.N 题号）、过渡段、说明文字、图示标题、表格、计算例
- 章节标题里出现过的总称概念（这些是父节点，不重复）

每项字段：
- type: "definition" | "theorem" | "proposition" | "corollary" | "lemma" | "method"
- name: 简短中文名（如「直和」「子空间的和」），不带「定义/定理」前缀，**不超过 12 字**
- summary: 不超过 30 字的一句话本质描述
- numeric_label: **必须填**教材完整编号（如 "1.41" / "2.A.13"）；**无编号的条目不要列入**

输出严格 JSON 数组，无任何额外文字、说明或代码围栏。本页无符合条件的条目输出 []。
例：[{{"type":"definition","name":"直和","summary":"和中每元素唯一表示","numeric_label":"1.41"}}]
"""


def extract_l2_from_page(backend, page_png: bytes, *, book_full: str, l0: dict, l1: dict,
                        page_1based: int) -> list[dict]:
    prompt = _EXTRACT_PROMPT.format(
        book_full=book_full,
        chapter_label=l0.get("chapter_label", ""), chapter_name=l0["name"],
        section_label=l1.get("section_label", ""), section_name=l1["name"],
        page=page_1based,
    )
    raw = backend.chat([{"role": "user", "content": prompt}], image=page_png)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e <= s:
        return []
    try:
        items = json.loads(raw[s:e + 1])
    except Exception:
        return []
    return items if isinstance(items, list) else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--book", required=True, help="书 id（如 LADR）")
    ap.add_argument("--book-full", default=None, help="书全称（喂给 AI）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--pages", default=None, help="页范围 START-END（1-based）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个 L1 section（试水）")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).resolve()
    doc = fitz.open(pdf_path)
    book_full = args.book_full or args.book
    KG_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else KG_DIR / f"{args.book}.json"

    kg = build_skeleton_from_toc(doc, args.book, pdf_path)
    l0_by_id = {n["id"]: n for n in kg["nodes"] if n["level"] == 0}
    l1_list = [n for n in kg["nodes"] if n["level"] == 1]
    print(f"骨架：L0 {len(l0_by_id)} 章 + L1 {len(l1_list)} 节")

    if args.dry_run:
        out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"dry-run 完成 → {out}")
        return 0

    # 决定 L1 处理范围
    if args.pages:
        a, b = args.pages.split("-"); pg_lo, pg_hi = int(a), int(b)
        l1_list = [n for n in l1_list if not (n["pages"][1] < pg_lo or n["pages"][0] > pg_hi)]
    if args.limit:
        l1_list = l1_list[:args.limit]
    print(f"本次处理 L1 节数: {len(l1_list)}")

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude", "model": args.model, "effort": args.effort,
    })
    t_start = time.time()
    total_l2 = 0
    for li, l1 in enumerate(l1_list, 1):
        l0 = l0_by_id[l1["parent_id"]]
        ps, pe = l1["pages"]
        sec_l2: dict[str, dict] = {}   # name → node（节内去重）
        for pg in range(ps, pe + 1):
            png = render_page_png(doc, pg - 1, args.book)
            try:
                items = extract_l2_from_page(
                    backend, png, book_full=book_full, l0=l0, l1=l1, page_1based=pg)
            except Exception as ex:
                print(f"  page {pg}: AI 失败 {ex}", flush=True)
                continue
            for it in items:
                if not isinstance(it, dict) or not it.get("name"):
                    continue
                lbl = (it.get("numeric_label") or "").strip()
                if not lbl:          # 必须有教材编号才入图，避免抽到无编号小术语
                    continue
                # 按编号归一化（去空格、去全角点），同编号合并
                key = re.sub(r"\s+", "", lbl).replace("．", ".")
                node = sec_l2.get(key)
                if not node:
                    node = {
                        "id": f"{args.book.lower()}.l2.{l1['section_label']}.{key.replace('.','_')}",
                        "level": 2, "parent_id": l1["id"],
                        "type": it.get("type", "definition"),
                        "name": it["name"].strip(),
                        "summary": (it.get("summary") or "").strip(),
                        "numeric_label": (it.get("numeric_label") or "").strip(),
                        "pages": [pg],
                    }
                    sec_l2[key] = node
                else:
                    if pg not in node["pages"]:
                        node["pages"].append(pg)
        kg["nodes"].extend(sec_l2.values())
        total_l2 += len(sec_l2)
        elapsed = time.time() - t_start
        print(f"  [{li}/{len(l1_list)}] {l1['section_label']} {l1['name']}: "
              f"{len(sec_l2)} 个 L2（累计 {total_l2}，{elapsed:.0f}s）", flush=True)

    out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 输出 → {out}（共 {len(kg['nodes'])} 节点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
