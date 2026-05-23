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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    ap.add_argument("--workers", type=int, default=4, help="并行 AI 调用数（默认 4）")
    ap.add_argument("--rescan-pages", default="", help="只重扫指定页（逗号分隔，如 97,103,171）。需 --kg-output 已存在")
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

    # 断点续传：若 out 已存在，加载已扫的 L2 节点，跳过其父 L1
    done_l1: set[str] = set()
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            old_l2 = [n for n in prev.get("nodes", []) if n.get("level") == 2]
            kg["nodes"].extend(old_l2)
            done_l1 = set(n["parent_id"] for n in old_l2)
            print(f"续跑：加载 {len(old_l2)} 个已扫 L2 节点，跳过已扫 L1 {len(done_l1)} 个")
        except Exception as ex:
            print(f"加载已存 KG 失败，全量扫：{ex}")

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
    # --rescan-pages：只重扫指定页（用已扫 L1 的 page range 找到所属节，但只处理那几页）
    rescan_pages = set()
    if args.rescan_pages:
        rescan_pages = set(int(x) for x in args.rescan_pages.split(",") if x.strip())
        # 强制 OVERRIDE done_l1：被重扫页所属的 L1 节即便已扫也需要再跑（只跑那几页）
        l1_list = [n for n in nodes if n.get("level")==1] if False else l1_list
        # 重新计算：以原 l1_list（含已扫）为基础，但仅保留含 rescan 页的节
        all_l1 = [n for n in kg["nodes"] if n.get("level") == 1]
        l1_list = [n for n in all_l1 if any(p in rescan_pages for p in range(n["pages"][0], n["pages"][1]+1))]
        print(f"--rescan-pages 模式：仅重跑 {sorted(rescan_pages)}，覆盖 {len(l1_list)} 个 L1 节")
    else:
        # 跳过已扫
        l1_list = [n for n in l1_list if n["id"] not in done_l1]
    print(f"本次处理 L1 节数: {len(l1_list)}")

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude", "model": args.model, "effort": args.effort,
    })
    t_start = time.time()
    total_l2 = 0
    for li, l1 in enumerate(l1_list, 1):
        l0 = l0_by_id[l1["parent_id"]]
        ps, pe = l1["pages"]
        sec_l2: dict[str, dict] = {}   # numeric_label → node（节内去重）
        # 渲染所有页（串行，fitz 不保证线程安全，但快）
        page_range = (sorted(p for p in range(ps, pe + 1) if p in rescan_pages)
                      if rescan_pages else list(range(ps, pe + 1)))
        pngs = {pg: render_page_png(doc, pg - 1, args.book) for pg in page_range}
        # AI 调用并行（ClaudeCli 每次 spawn 子进程，subprocess 间互不影响）
        def _work(pg, png=None):
            png = png if png is not None else pngs[pg]
            try:
                return pg, extract_l2_from_page(
                    backend, png, book_full=book_full, l0=l0, l1=l1, page_1based=pg), None
            except Exception as ex:
                return pg, None, ex
        page_results: dict[int, list] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(_work, pg) for pg in pngs]
            for fut in as_completed(futs):
                pg, items, err = fut.result()
                if err is not None or items is None:
                    print(f"  page {pg}: AI 失败 {err}", flush=True)
                    continue
                page_results[pg] = items
        # 按页号顺序合并（保证 numeric_label 排序稳定）
        for pg in sorted(page_results):
            for it in page_results[pg]:
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
        # rescan 模式：将新结果与已存的 L2 合并到 kg["nodes"]（按 numeric_label 去重）
        if rescan_pages:
            existing = {n.get("numeric_label",""): n for n in kg["nodes"]
                        if n.get("level")==2 and n.get("parent_id")==l1["id"]
                        and n.get("numeric_label","")}
            for k, new in sec_l2.items():
                if new.get("numeric_label","") in existing:
                    # 合并 pages 到已有节点
                    cur = existing[new["numeric_label"]]
                    cur.setdefault("pages", []).extend(p for p in new.get("pages",[])
                                                       if p not in cur.get("pages",[]))
                else:
                    kg["nodes"].append(new)
            total_l2 += len(sec_l2)
        else:
            kg["nodes"].extend(sec_l2.values())
            total_l2 += len(sec_l2)
        elapsed = time.time() - t_start
        # 增量写：每完成一节立刻落盘
        out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [{li}/{len(l1_list)}] {l1['section_label']} {l1['name']}: "
              f"{len(sec_l2)} 个 L2（累计 {total_l2}，{elapsed:.0f}s, 已写盘）", flush=True)

    out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 输出 → {out}（共 {len(kg['nodes'])} 节点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
