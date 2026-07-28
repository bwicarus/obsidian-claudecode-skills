"""
kg/build_grammar_nodes.py — 从英语语法书 PDF 抽语法点知识图谱（grammar KG）。

跟数学书的 build_nodes.py 区别：
  - 三层映射：L0=主题组(Contents 页解析) / L1=Unit(TOC) / L2=讲解页 AI 抽的语法点
  - L2 无"教材编号"，按语法点本身抽；节点带 en + examples + tracked 字段
  - 只扫**讲解页**，跳过习题页(含 "Exercises") + 目录/答案/Study Guide
  - 顶层 kind="grammar"，供 PDF 阅读器"按跟踪语法点分析"识别

骨架解析（零 AI）：
  - get_toc() 拿 Unit 列表（"N Title"，带页码）= L1
  - Contents 页文字解析"主题组标题行 + 其下 Unit 号" = L0 + L1 归属

用法：
  python3 scripts/kg/build_grammar_nodes.py --pdf <path> --book EGIU
    [--contents-pages 5-8]   Contents 所在页(1-based)，默认自动找
    [--limit N]              只跑前 N 个 Unit（试水）
    [--units A-B]            只跑 Unit 号范围（试水，如 7-12）
    [--model sonnet] [--effort medium] [--workers 4]
    [--dry-run]              只生成 L0/L1 骨架，不调 AI
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
_CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_CODE_ROOT / "_client" / "core"))
from ai_backends import make_backend  # noqa: E402

KG_DIR = config.PROJECT_DIR / "knowledge_graph"
PAGE_CACHE = config.PROJECT_DIR / "state" / "kg" / "page-cache"
DPI = 144

# Contents 页里要跳过的前言行（不是主题组也不是 Unit）
_FRONT_MATTER = re.compile(r"^(Contents|Thanks|To the student|To the teacher|Additional exercises|Study guide|Appendix|Index)\b", re.I)
_UNIT_LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")   # "7 Present perfect 1 (...)"
_TOC_UNIT = re.compile(r"^\s*(\d+)\s+(.+)")          # TOC 条目 "7 Present perfect 1 (...)"


def render_page_png(doc, page_idx_0based: int, book_id: str) -> bytes:
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


def _find_contents_pages(doc) -> list[int]:
    """自动定位 Contents 页（1-based）。从 TOC 的 Contents 条目到第一个 Unit 条目之间。"""
    toc = doc.get_toc()
    contents_pg = None
    first_unit_pg = None
    for lvl, name, pg in toc:
        if contents_pg is None and re.match(r"\s*Contents\b", name, re.I):
            contents_pg = pg
        if first_unit_pg is None and _TOC_UNIT.match(name.strip()):
            first_unit_pg = pg
            break
    if contents_pg is None:
        contents_pg = 5
    if first_unit_pg is None:
        first_unit_pg = contents_pg + 6
    return list(range(contents_pg, first_unit_pg))   # [contents .. just before unit1]


def parse_unit_to_group(doc, contents_pages: list[int]) -> dict[int, str]:
    """解析 Contents 页文字 → {unit_no: group_name}。
    规则：非编号、非前言行 = 新主题组；其下的 "N Title" 行归该组。"""
    unit_group: dict[int, str] = {}
    cur_group = "其它"
    for pg in contents_pages:
        if pg - 1 >= doc.page_count:
            break
        for line in doc[pg - 1].get_text().splitlines():
            s = line.strip()
            if not s or _FRONT_MATTER.match(s):
                continue
            m = _UNIT_LINE.match(s)
            if m and m.group(2).strip():
                # 是 Unit 行：归当前组
                no = int(m.group(1))
                if 1 <= no <= 200 and no not in unit_group:
                    unit_group[no] = cur_group
            elif not re.match(r"^[ivxlcdm]+$", s, re.I) and not s.isdigit():
                # 主题组标题：排除全大写提示语、介词列表续行(含 /)、超长说明行
                if s.isupper() or "/" in s or len(s) > 40:
                    continue
                cur_group = s
    return unit_group


def build_skeleton(doc, book_id: str, pdf_path: str, contents_pages: list[int]) -> dict:
    """L0(主题组) + L1(Unit)。"""
    toc = doc.get_toc()
    n_pages = doc.page_count
    bid = book_id.lower()

    # 1. 从 TOC 拿 Unit（编号 + 标题 + 起始页 + 结束页）
    units = []  # {no, title, page_start, page_end}
    toc_units = [(name.strip(), pg) for lvl, name, pg in toc if _TOC_UNIT.match(name.strip())]
    for i, (name, pg) in enumerate(toc_units):
        m = _TOC_UNIT.match(name)
        no = int(m.group(1)); title = m.group(2).strip()
        end = toc_units[i + 1][1] - 1 if i + 1 < len(toc_units) else n_pages
        # 每 Unit 限 ≤2 页(讲解+习题)，免得最后一个 Unit 吃到全书末尾的附录/答案/Study Guide
        units.append({"no": no, "title": title, "page_start": pg, "page_end": min(max(pg, end), pg + 1)})

    # 2. Unit → 主题组
    unit_group = parse_unit_to_group(doc, contents_pages)
    # 主题组按 Unit 首次出现顺序排
    group_order: list[str] = []
    for u in units:
        g = unit_group.get(u["no"], "其它")
        if g not in group_order:
            group_order.append(g)

    nodes: list[dict] = []
    group_id_map: dict[str, str] = {}
    for gi, g in enumerate(group_order, 1):
        gid = f"{bid}.l0.g{gi}"
        group_id_map[g] = gid
        # 组的页范围 = 组内 Unit 的 min/max
        gu = [u for u in units if unit_group.get(u["no"], "其它") == g]
        nodes.append({
            "id": gid, "level": 0, "parent_id": None,
            "name": g,
            "pages": [min(u["page_start"] for u in gu), max(u["page_end"] for u in gu)] if gu else [0, 0],
        })
    for u in units:
        g = unit_group.get(u["no"], "其它")
        nodes.append({
            "id": f"{bid}.l1.u{u['no']}",
            "level": 1, "parent_id": group_id_map[g],
            "name": u["title"], "unit_no": u["no"],
            "pages": [u["page_start"], u["page_end"]],
        })

    return {
        "book": book_id, "pdf": str(pdf_path), "title": book_id,
        "kind": "grammar", "nodes": nodes, "edges": [],
    }


_GRAMMAR_PROMPT = """你在为英语语法书《{book_full}》建「语法点」知识图谱。
当前 Unit {unit_no}「{unit_title}」的讲解页（第 {page} 页）。

【该页文字层】
————
{page_text}
————
（同时附该页图像，文字层乱码时以图像为准。）

从这一页的语法**讲解**里，抽出读者要掌握的**具体语法点**（一个 Unit 通常 1~4 个）。
每个语法点是一条能单独学习/在真实句子里识别的规则或用法，例如：
- 「现在完成时表示过去动作对现在的影响」
- 「just / already / yet 与现在完成时连用」
- 「for 和 since 表示时间的区别」

每项字段：
- name: 简短中文语法点名（≤16 字），如「现在完成时表经历」
- en: 对应英文语法术语/结构（≤40 字），如 "present perfect for experience"
- summary: 一句话说清这个点是什么 + 怎么用（≤45 字）
- examples: 该页出现的 1~2 个英文例句（字符串数组；没有就空数组）

**不要抽**：练习题说明、Exercises、纯例句（例句放进 examples）、Unit 标题的笼统复述、跟语法无关的话。
输出严格 JSON 数组，无任何额外文字或代码围栏。本页无语法点输出 []。
例：[{{"name":"现在完成时表经历","en":"present perfect for experience","summary":"用 have/has done 谈到目前为止的人生经历，不强调具体时间","examples":["I have been to Japan."]}}]
"""


def extract_points(backend, png: bytes, *, book_full: str, unit_no: int, unit_title: str, page: int, page_text: str) -> list[dict]:
    prompt = _GRAMMAR_PROMPT.format(
        book_full=book_full, unit_no=unit_no, unit_title=unit_title,
        page=page, page_text=(page_text[:3500] if page_text else "（无文字层，仅靠图像）"),
    )
    raw = (backend.chat([{"role": "user", "content": prompt}], image=png) or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw); raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e <= s:
        return []
    try:
        items = json.loads(raw[s:e + 1])
    except Exception:
        return []
    return items if isinstance(items, list) else []


def _is_exercise_page(text: str) -> bool:
    return bool(re.search(r"\bExercises\b", text[:160]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--book", required=True, help="书 id（如 EGIU）")
    ap.add_argument("--book-full", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--contents-pages", default=None, help="Contents 页范围 START-END(1-based)，默认自动")
    ap.add_argument("--units", default=None, help="只跑 Unit 号范围 A-B（试水）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个 Unit")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).resolve()
    doc = fitz.open(pdf_path)
    book_full = args.book_full or args.book
    KG_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else KG_DIR / f"{args.book}.json"

    if args.contents_pages:
        a, b = args.contents_pages.split("-"); contents_pages = list(range(int(a), int(b) + 1))
    else:
        contents_pages = _find_contents_pages(doc)

    kg = build_skeleton(doc, args.book, pdf_path, contents_pages)
    if args.book_full:
        kg["title"] = args.book_full
    l1_list = [n for n in kg["nodes"] if n["level"] == 1]
    n_l0 = len([n for n in kg["nodes"] if n["level"] == 0])
    print(f"骨架：L0 {n_l0} 个主题组 + L1 {len(l1_list)} 个 Unit（Contents 页 {contents_pages[0]}-{contents_pages[-1]}）", flush=True)

    # 断点续传
    done_l1: set[str] = set()
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            old_l2 = [n for n in prev.get("nodes", []) if n.get("level") == 2]
            kg["nodes"].extend(old_l2)
            kg["edges"] = prev.get("edges", [])
            done_l1 = set(n["parent_id"] for n in old_l2)
            print(f"续跑：加载 {len(old_l2)} 个已扫 L2，跳过已扫 Unit {len(done_l1)} 个", flush=True)
        except Exception as ex:
            print(f"加载已存 KG 失败，全量扫：{ex}", flush=True)

    if args.dry_run:
        out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"dry-run 完成 → {out}", flush=True)
        return 0

    # 处理范围
    if args.units:
        a, b = args.units.split("-"); ulo, uhi = int(a), int(b)
        l1_list = [n for n in l1_list if ulo <= n.get("unit_no", 0) <= uhi]
    if args.limit:
        l1_list = l1_list[:args.limit]
    l1_list = [n for n in l1_list if n["id"] not in done_l1]
    print(f"本次处理 Unit 数: {len(l1_list)}", flush=True)

    backend = make_backend("claude_cli", {
        "command": "/usr/bin/claude", "model": args.model, "effort": args.effort,
    })
    t0 = time.time(); total = 0
    fitz_lock = threading.Lock()   # PyMuPDF 非线程安全：渲染/取文字串行（很快）
    write_lock = threading.Lock()  # kg["nodes"] 追加 + 落盘 串行

    def process_unit(l1: dict) -> tuple[dict, dict]:
        """处理一个 Unit：取讲解页 → AI 抽语法点 → 返回 (l1, {name: node})。可并行调用。"""
        ps, pe = l1["pages"]
        with fitz_lock:   # fitz 操作加锁（渲染有缓存、取文字都很快）
            expl_pages = []
            for pg in range(ps, pe + 1):
                if pg - 1 >= doc.page_count:
                    break
                if not _is_exercise_page(doc[pg - 1].get_text()):
                    expl_pages.append(pg)
            if not expl_pages:
                expl_pages = [ps]
            pngs = {pg: render_page_png(doc, pg - 1, args.book) for pg in expl_pages}
            texts = {pg: doc[pg - 1].get_text() for pg in expl_pages}
        # AI 调用不加锁（claude_cli 每次 spawn 独立子进程，Unit 间真正并行）
        results: dict[int, list] = {}
        for pg in expl_pages:
            try:
                results[pg] = extract_points(backend, pngs[pg], book_full=book_full,
                                             unit_no=l1.get("unit_no", 0), unit_title=l1["name"],
                                             page=pg, page_text=texts.get(pg, ""))
            except Exception as ex:
                print(f"  U{l1.get('unit_no')} page {pg}: AI 失败 {ex}", flush=True)
        seen: dict[str, dict] = {}
        idx = 0
        for pg in sorted(results):
            for it in results[pg]:
                if not isinstance(it, dict) or not (it.get("name") or "").strip():
                    continue
                nm = it["name"].strip()
                if nm in seen:
                    if pg not in seen[nm]["pages"]:
                        seen[nm]["pages"].append(pg)
                    continue
                idx += 1
                seen[nm] = {
                    "id": f"{args.book.lower()}.l2.u{l1.get('unit_no',0)}.{idx}",
                    "level": 2, "parent_id": l1["id"],
                    "type": "grammar_point",
                    "name": nm,
                    "en": (it.get("en") or "").strip(),
                    "summary": (it.get("summary") or "").strip(),
                    "examples": [e for e in (it.get("examples") or []) if isinstance(e, str)][:3],
                    "pages": [pg],
                    "tracked": False,
                }
        return l1, seen

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_unit, l1): l1 for l1 in l1_list}
        for fut in as_completed(futs):
            l1, seen = fut.result()
            with write_lock:
                kg["nodes"].extend(seen.values())
                total += len(seen); done += 1
                out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  [{done}/{len(l1_list)}] U{l1.get('unit_no')} {l1['name'][:40]}: "
                      f"{len(seen)} 点（累计 {total}, {time.time()-t0:.0f}s）", flush=True)

    out.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 输出 → {out}（共 {len(kg['nodes'])} 节点）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
