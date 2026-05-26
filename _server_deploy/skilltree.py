"""
skilltree.py — /skilltree/<book>/ 知识图谱可视化路由 + 编辑 API。

模式跟 control.py 一致：register_skilltree(app) 注册路由。
KG 数据 = CLAUDE_PROJECT/knowledge_graph/<book>.json
"""
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import abort, jsonify, render_template, request, send_file

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
KG_DIR = CLAUDE_DIR / "knowledge_graph"


def list_books():
    out = []
    if not KG_DIR.exists():
        return out
    for f in sorted(KG_DIR.glob("*.json")):
        if f.name.endswith(".bak.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": d.get("book") or f.stem,
            "file": f.name,
            "nodes": len(d.get("nodes", [])),
            "edges": len(d.get("edges", [])),
        })
    return out


def _kg_path(book):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "", book) or "book"
    p = KG_DIR / f"{safe}.json"
    return p if p.exists() else None


def _load_kg(book):
    p = _kg_path(book)
    if not p:
        return None, None
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return p, None


def _save_kg(p, kg):
    """先写临时文件再 rename，避免半写状态。"""
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


_edit_lock = threading.Lock()


# ─── 回收站：trivial 节点收纳，按章节分册 ───────────────────────────────────────
TRASH_MAX_NODES_PER_FILE = 50   # 单回收站笔记最多容纳节点数（超后开新册同章节）

def _chap_num_from_label(label: str) -> str:
    """从 numeric_label '1.21' 取章号 '1'；无法解析返 'misc'。"""
    if not label: return "misc"
    m = re.match(r"^(\d+)\.", label)
    return m.group(1) if m else "misc"

def _trash_file_for(kg: dict, chap_num: str) -> tuple[str, Path]:
    """选这一章的回收站笔记：找现有 {prefix}回收站-{chap}*.md，没满就用，满了开新册。
    返回 (vault 相对路径, 绝对路径)。"""
    vault_root = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
    prefix = (kg.get("note_prefix") or "").strip()
    base = f"{prefix}回收站-{chap_num}"
    # 找现有册
    persistent = kg.get("_note_to_covered_l2") or {}
    candidates = []
    for rel in persistent.keys():
        stem = Path(rel).stem
        if stem == base or stem.startswith(base + "-"):
            n_count = len(persistent.get(rel) or [])
            candidates.append((rel, n_count))
    candidates.sort(key=lambda x: x[0])
    for rel, n in candidates:
        if n < TRASH_MAX_NODES_PER_FILE:
            return rel, vault_root / rel
    # 开新册：base.md / base-2.md / base-3.md...
    n_try = len(candidates) + 1
    while True:
        rel = f"{base}.md" if n_try == 1 else f"{base}-{n_try}.md"
        full = vault_root / rel
        if not full.exists() and rel not in persistent:
            return rel, full
        n_try += 1

def _slugify_anchor(label: str, name: str) -> str:
    """Obsidian heading anchor：'1.21 向量、点' → '#1.21 向量、点'（保留原标题，
    Obsidian 直接用 markdown heading 文本作 anchor）。"""
    return f"{label} {name}".strip()

def _archive_node_to_trash(kg: dict, node: dict, pdf_abs_path: Path) -> str:
    """把节点 PDF 提取内容 append 到对应章节的回收站笔记。返回 vault 相对路径。"""
    import fitz
    chap_num = _chap_num_from_label(node.get("numeric_label", ""))
    trash_rel, trash_abs = _trash_file_for(kg, chap_num)
    # 提取 PDF 段
    pdf_name = pdf_abs_path.name
    doc = fitz.open(str(pdf_abs_path))
    sec_parts = [f"\n## {node.get('numeric_label','?')} {node['name']}"]
    tyz = TYPE_ZH.get(node.get("type"), node.get("type") or "")
    if tyz: sec_parts.append(f"*[{tyz}]*")
    sec_parts.append("")
    pages = node.get("pages") or []
    for pg in pages:
        if pg < 1 or pg > len(doc): continue
        page = doc[pg - 1]
        blocks = _find_node_blocks_in_page(page, node.get("numeric_label", ""))
        if blocks:
            bbox = _union_bbox([b["bbox"] for b in blocks if b.get("bbox")],
                                page_height=page.rect.height)
            if bbox:
                rect_str = ",".join(map(str, bbox))
                sec_parts.append(f"![[{pdf_name}#page={pg}&rect={rect_str}&color=yellow]]")
                raw = "\n".join(b["text"] for b in blocks).strip()
                if raw:
                    sec_parts.append("")
                    sec_parts.append(f"<!-- 原文\n{raw}\n-->")
                continue
        sec_parts.append(f"![[{pdf_name}#page={pg}]]")
    if node.get("summary"):
        sec_parts.append("")
        sec_parts.append(f"**摘要**：{node['summary']}")
    sec_parts.append(f"\n<!-- kg_node_id: {node['id']} -->\n")
    doc.close()

    # 文件不存在 → 新建带 frontmatter；存在 → append
    if not trash_abs.exists():
        book = kg.get("book", "?")
        chap_node = next((n for n in kg["nodes"]
                          if n.get("level")==0 and _chap_num_from_label(n.get("chapter_label","").replace("第","").replace("章","").strip()) == chap_num), None)
        chap_name = chap_node.get("name", f"第{chap_num}章") if chap_node else f"第{chap_num}章"
        header = f"""---
subject: 数学
source: {book} 第 {chap_num} 章 回收站
chapter: {chap_name}
kg_trash: true
anki_max_per_section: 1
---

# 第 {chap_num} 章 · 回收站

_收纳过于简单的记号、术语、单句定义等。AI 制卡限 1 张/节点。_

"""
        trash_abs.write_text(header + "\n".join(sec_parts), encoding="utf-8")
    else:
        with trash_abs.open("a", encoding="utf-8") as f:
            f.write("\n".join(sec_parts))
    return trash_rel

def _restore_node_from_trash(kg: dict, node: dict) -> tuple[str, str]:
    """从回收站笔记移除该节点的段，新建独立笔记。返回 (new_path, old_trash_path)。"""
    vault_root = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
    notes = node.get("containing_notes") or []
    trash_rel = next((nt for nt in notes if "回收站" in nt), "")
    if not trash_rel:
        raise RuntimeError("节点不在任何回收站笔记")
    trash_abs = vault_root / trash_rel
    if not trash_abs.exists():
        raise RuntimeError(f"回收站笔记不存在：{trash_rel}")
    txt = trash_abs.read_text(encoding="utf-8")
    # 找该节点对应的段：用 <!-- kg_node_id: X --> 锚定
    marker = f"<!-- kg_node_id: {node['id']} -->"
    if marker not in txt:
        raise RuntimeError(f"回收站笔记未找到节点段（{marker}）")
    # 段 = 从上一个 ## 标题开始，到下一个 ## 或文件末尾
    idx_marker = txt.index(marker)
    # 往前找最近的 \n##
    h_start = txt.rfind("\n## ", 0, idx_marker)
    if h_start == -1:
        raise RuntimeError("无法定位段起始")
    # 往后找下一个 \n## 或 EOF
    h_end = txt.find("\n## ", idx_marker)
    if h_end == -1:
        h_end = len(txt)
    section_text = txt[h_start + 1: h_end].rstrip()  # 去掉前导换行
    # 从回收站删除这段
    new_trash_txt = (txt[:h_start] + txt[h_end:]).rstrip() + "\n"
    trash_abs.write_text(new_trash_txt, encoding="utf-8")

    # 新建独立笔记
    prefix = (kg.get("note_prefix") or "").strip()
    safe = _safe_filename(node["name"])
    new_rel = f"{prefix}{safe}.md"
    new_abs = vault_root / new_rel
    n_try = 2
    while new_abs.exists() and n_try < 100:
        new_rel = f"{prefix}{safe}-{n_try}.md"
        new_abs = vault_root / new_rel
        n_try += 1
    book = kg.get("book", "?")
    pages = node.get("pages") or []
    header = f"""---
subject: 数学
source: {book} {node.get('numeric_label','')}
pages: {', '.join(map(str, pages))}
kg_node_ids: [{node['id']}]
---

# {node.get('numeric_label','')} {node['name']}

"""
    # section_text 是 "## 1.21 向量、点 ..."，去掉 ## 标题（用上方 # 替代）
    body = re.sub(r"^##\s+[^\n]*\n", "", section_text)
    new_abs.write_text(header + body + "\n\n## 我的笔记\n\n（待补充）\n", encoding="utf-8")
    return new_rel, trash_rel
# ───────────────────────────────────────────────────────────────────────


# ─── AI 验证：节点是否真实/描述准确 + 自动 merge/delete 决策 ─────────────────
def _ai_verify_node(kg: dict, node: dict, raw_pdf_text: str) -> dict:
    """让 AI 看节点元数据 + PDF 实际内容 + 兄弟节点，决定 ok/merge/delete。
    返回 {action, target_id?, reason}。失败时返回 {action:'ok'}（不阻断）。"""
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "_client" / "core"))
        from ai_backends import make_backend
    except Exception:
        return {"action": "ok", "reason": "ai_backends unavailable"}
    id2 = {n["id"]: n for n in kg["nodes"]}
    parent = id2.get(node.get("parent_id"))
    siblings = [n for n in kg["nodes"]
                if n["level"] == 2 and n.get("parent_id") == node.get("parent_id")
                and n["id"] != node["id"]]
    sib_text = "\n".join(
        f"  {s['id']} | {s.get('numeric_label','')} | {s['name']} | {(s.get('summary','') or '')[:50]}"
        for s in siblings[:40]
    ) or "  （无）"

    prompt = f"""你正在审查《{kg.get('book','?')}》知识图谱中的一个节点。判断它是否真实/准确。

【候选节点】
id: {node['id']}
编号: {node.get('numeric_label','')}
名称: {node['name']}
类型: {node.get('type','')}
摘要: {node.get('summary','')}
所在节: {parent.get('name') if parent else '?'}

【PDF 该 page 实际内容】
————
{raw_pdf_text[:2500]}
————

【该节兄弟节点】
{sib_text}

请严格判断（一律返回 JSON）：

1) PDF 内容里是否真的有「{node['name']}」这个**特定对象**的明确定义/陈述？
2) 节点的 name/summary 跟 PDF 对得上吗？
3) 如果 PDF 里 "{node.get('numeric_label')}" 是例子或别的不相关内容（AI 误识别）→ action=delete
4) 如果讲的跟某个兄弟节点是同一概念 → action=merge + 给该兄弟的 id
5) 都没问题 → action=ok

输出严格 JSON（不要别的文字）：
{{
  "action": "ok" | "merge" | "delete",
  "target_id": "<merge 时给兄弟 id>",
  "reason": "<30 字内判定依据>"
}}
"""
    try:
        backend = make_backend("claude_cli", {
            "command": "/usr/bin/claude", "model": "sonnet",
            "effort": "medium", "timeout": 120,
        })
        raw = backend.chat([{"role": "user", "content": prompt}]).strip()
    except Exception as ex:
        return {"action": "ok", "reason": f"ai 调用失败: {ex}"}
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return {"action": "ok", "reason": "无 JSON"}
    try:
        data = json.loads(raw[s:e+1])
    except Exception:
        return {"action": "ok", "reason": "JSON 解析失败"}
    if data.get("action") not in ("ok", "merge", "delete"):
        return {"action": "ok", "reason": "unknown action"}
    return data
# ───────────────────────────────────────────────────────────────────────


# ─── PDF→笔记 辅助函数 ────────────────────────────────────────────────────
TYPE_ZH = {"definition":"定义", "theorem":"定理", "proposition":"命题",
           "corollary":"推论", "lemma":"引理", "example":"例子"}

def _safe_filename(s):
    return re.sub(r'[\\/:*?"<>|]', '', s or "新节点")[:50]

def _find_node_blocks_in_page(page, numeric_label, max_blocks=8):
    """在 PDF page 里找含 numeric_label 的 text blocks + 后续相关 block。
    返回 [{text, bbox}, ...]。pymupdf 坐标系跟 obsidian 一致。"""
    blocks = page.get_text("dict").get("blocks", [])
    text_blocks = [b for b in blocks if b.get("type") == 0]   # type=0 文本块
    # 拼出每块的纯文本 + bbox
    items = []
    for b in text_blocks:
        text = ""
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                text += span.get("text", "") + " "
            text += "\n"
        items.append({"text": text.strip(), "bbox": b.get("bbox")})
    if not numeric_label:
        return items[:max_blocks]
    # 找包含 numeric_label 的块
    start = -1
    for i, it in enumerate(items):
        if numeric_label in it["text"]:
            start = i
            break
    if start < 0:
        return []
    # 从 start 开始取后续 N 块，直到遇到下一个明显的 numeric_label 或上限
    # 简化判别：下一个块开头出现 X.YY (两段数字+点) 即新节点
    import re as _re
    out = [items[start]]
    next_label_pat = _re.compile(r"^\s*\d+\.\d+")
    for j in range(start + 1, min(start + max_blocks + 1, len(items))):
        t = items[j]["text"]
        if next_label_pat.match(t) and not t.startswith(numeric_label):
            break
        out.append(items[j])
    return out

def _union_bbox(boxes, page_height=None):
    """合并多个 fitz bbox (x0, y0, x1, y1) → 外包矩形。
    fitz 用 top-down（y0=顶）；Obsidian PDF rect 用 PDF user space bottom-up
    （y0=底）。如果传 page_height，自动翻转 y → bottom-up 坐标。"""
    if not boxes: return None
    xs0 = min(b[0] for b in boxes); ys0 = min(b[1] for b in boxes)
    xs1 = max(b[2] for b in boxes); ys1 = max(b[3] for b in boxes)
    if page_height is not None:
        # fitz y0_top, y1_bottom (y 向下增长)
        # → obsidian y0_user = page_h - y1_fitz, y1_user = page_h - y0_fitz
        return [int(xs0), int(page_height - ys1), int(xs1), int(page_height - ys0)]
    return [int(xs0), int(ys0), int(xs1), int(ys1)]

def _build_note_for_node(kg, node, pdf_abs_path: Path):
    """生成笔记文件并返回 (note_rel_path, covered_node_ids)。"""
    import fitz
    vault_root = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
    pdf_vault_path = pdf_abs_path.resolve().relative_to(vault_root.resolve()).as_posix()
    pdf_name = pdf_abs_path.name
    book = kg.get("book", "?")
    id2 = {n["id"]: n for n in kg["nodes"]}
    chap_of = lambda nid: id2.get(id2.get(nid, {}).get("parent_id", ""), {})

    # 决定要处理哪些节点：L1 → 该节所有 L2；L2 → 仅自己
    if node["level"] == 1:
        targets = [n for n in kg["nodes"] if n["level"]==2 and n.get("parent_id")==node["id"]]
        targets.sort(key=lambda n: (n.get("pages") or [99999])[0])
        title_node = node
    elif node["level"] == 2:
        targets = [node]
        title_node = node
    else:
        raise RuntimeError("仅支持 L1/L2 节点建笔记")
    if not targets:
        raise RuntimeError("无 L2 节点可建笔记")

    # 打开 PDF
    doc = fitz.open(str(pdf_abs_path))
    sections_md = []
    covered_ids = []
    for t in targets:
        covered_ids.append(t["id"])
        pages = t.get("pages") or []
        numeric_label = t.get("numeric_label", "")
        tyz = TYPE_ZH.get(t.get("type"), t.get("type") or "")
        sec_parts = [f"## {numeric_label} {t['name']}"]
        if tyz:
            sec_parts.append(f"*[{tyz}]*")
        sec_parts.append("")
        # 对每个 page 提取并嵌入
        embedded = False
        for pg in pages:
            if pg < 1 or pg > len(doc): continue
            page = doc[pg - 1]
            blocks = _find_node_blocks_in_page(page, numeric_label)
            if blocks:
                # 传 page.rect.height 把 fitz top-down y 翻转成 obsidian 用的 bottom-up
                bbox = _union_bbox([b["bbox"] for b in blocks if b.get("bbox")],
                                    page_height=page.rect.height)
                if bbox:
                    rect_str = ",".join(map(str, bbox))
                    sec_parts.append(f"![[{pdf_name}#page={pg}&rect={rect_str}&color=yellow]]")
                    embedded = True
                    # 用 annotate_note 兼容格式（<!-- 原文 ... --> HTML 注释），
                    # 避免 register_notes 流程下次跑时 annotate_note 重复追加
                    raw = "\n".join(b["text"] for b in blocks).strip()
                    if raw:
                        sec_parts.append("")
                        sec_parts.append(f"<!-- 原文\n{raw}\n-->")
                    continue
            # fallback：无 block 匹配，嵌整页（仍是兼容格式，注释为空）
            sec_parts.append(f"![[{pdf_name}#page={pg}]]")
            embedded = True
        if not embedded:
            sec_parts.append("*（PDF 找不到对应内容，请手动补充）*")
        # 节点 summary 也写上作快速参考
        if t.get("summary"):
            sec_parts.append("")
            sec_parts.append(f"**摘要**：{t['summary']}")
        sec_parts.append("")
        sec_parts.append("")
        sections_md.append("\n".join(sec_parts))
    doc.close()

    # 拼装最终笔记
    chap = chap_of(title_node["id"]) if title_node["level"]==2 else id2.get(title_node.get("parent_id",""), {})
    title = title_node["name"] if title_node["level"]==1 else (title_node.get("numeric_label","") + " " + title_node["name"])
    all_pages = sorted(set(p for t in targets for p in (t.get("pages") or [])))
    fm_lines = [
        "---",
        "subject: 数学",
        f"source: {book}",
        f"pages: {', '.join(map(str, all_pages))}" if all_pages else "pages: ",
        f"chapter: {chap.get('name', '') if chap else ''}",
        f"section: {title_node['name'] if title_node['level']==1 else ''}",
        f"kg_node_ids: [{', '.join(covered_ids)}]",
        "---",
        "",
        f"# {title}",
        "",
        f"_含 {len(covered_ids)} 个知识点 · 页码 {', '.join(map(str, all_pages))}_",
        "",
    ]
    content = "\n".join(fm_lines) + "\n".join(sections_md) + "\n## 我的笔记\n\n（待补充）\n"

    # 写文件：用 KG 顶层 note_prefix（如 "000-"），同名冲突时追加 "-2"/"-3"
    safe = _safe_filename(title_node["name"])
    prefix = (kg.get("note_prefix") or "").strip()
    fname = f"{prefix}{safe}.md"
    out_path = vault_root / fname
    n_try = 2
    while out_path.exists() and n_try < 100:
        fname = f"{prefix}{safe}-{n_try}.md"
        out_path = vault_root / fname
        n_try += 1
    out_path.write_text(content, encoding="utf-8")
    return fname, covered_ids
# ───────────────────────────────────────────────────────────────────────


def _trigger_mastery_recompute(kg_path: Path):
    """后台跑 link_and_mastery 重新计算掌握度/解锁态。"""
    script = CLAUDE_DIR / "scripts" / "kg" / "link_and_mastery.py"
    if not script.exists():
        return
    env = os.environ.copy()
    env.setdefault("CLAUDE_PROJECT", str(CLAUDE_DIR))
    env.setdefault("OBSIDIAN_VAULT", os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
    env.setdefault("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
    log_dir = CLAUDE_DIR / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "kg_edit_recompute.log").open("ab") as logf:
        logf.write(f"\n=== {datetime.now().isoformat()} {kg_path.name} ===\n".encode())
        subprocess.Popen(
            ["/usr/bin/python3", str(script), "--kg", str(kg_path), "--in-place"],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
        )


def _apply_edit(kg, op, payload):
    """对 kg dict 做编辑操作；返回 (ok, info_str)。"""
    nodes = kg.setdefault("nodes", [])
    edges = kg.setdefault("edges", [])
    id2 = {n["id"]: n for n in nodes}
    if op == "delete_node":
        nid = payload.get("id")
        if nid not in id2:
            return False, "node not found"
        kg["nodes"] = [n for n in nodes if n["id"] != nid]
        kg["edges"] = [e for e in edges
                       if e.get("from") != nid and e.get("to") != nid]
        return True, f"deleted node {nid}"
    if op == "delete_edge":
        a = payload.get("from"); b = payload.get("to")
        before = len(edges)
        kg["edges"] = [e for e in edges
                       if not (e.get("from") == a and e.get("to") == b)]
        return True, f"removed {before - len(kg['edges'])} edge(s)"
    if op == "add_edge":
        a = payload.get("from"); b = payload.get("to")
        if a not in id2 or b not in id2 or a == b:
            return False, "invalid endpoints"
        # 防重复
        for e in edges:
            if e.get("from") == a and e.get("to") == b and e.get("kind") == "prereq":
                return False, "edge already exists"
        # 防环（简单 DFS：从 b 能否到 a）
        adj = {}
        for e in edges:
            adj.setdefault(e.get("from"), []).append(e.get("to"))
        stack = [b]; seen = {b}
        while stack:
            cur = stack.pop()
            if cur == a:
                return False, "would create cycle"
            for nx in adj.get(cur, []):
                if nx not in seen:
                    seen.add(nx); stack.append(nx)
        edges.append({
            "from": a, "to": b, "kind": "prereq", "level": id2[a].get("level", 2),
            "evidence": payload.get("evidence", "手动添加"),
        })
        return True, f"added {a} → {b}"
    if op == "merge":
        canonical = payload.get("canonical")
        drop_ids = payload.get("drop") or []
        if canonical not in id2:
            return False, "canonical not found"
        canon = id2[canonical]
        actual_drops = []
        for d in drop_ids:
            if d not in id2 or d == canonical:
                continue
            n = id2[d]
            # 合并 pages
            pgs = set(canon.get("pages", [])); pgs.update(n.get("pages", []))
            canon["pages"] = sorted(pgs)
            canon.setdefault("_merged_from", []).append(
                {"id": d, "name": n["name"], "label": n.get("numeric_label", "")})
            actual_drops.append(d)
        # 重定向边
        for e in edges:
            if e.get("from") in actual_drops: e["from"] = canonical
            if e.get("to") in actual_drops: e["to"] = canonical
        # 删自环 + 去重
        seen = set(); new_edges = []
        for e in edges:
            if e.get("from") == e.get("to"): continue
            k = (e.get("from"), e.get("to"), e.get("kind"))
            if k in seen: continue
            seen.add(k); new_edges.append(e)
        kg["edges"] = new_edges
        kg["nodes"] = [n for n in nodes if n["id"] not in actual_drops]
        return True, f"merged {len(actual_drops)} into {canonical}"
    if op == "update_summary":
        nid = payload.get("id")
        if nid not in id2:
            return False, "node not found"
        id2[nid]["summary"] = (payload.get("summary") or "").strip()
        return True, "summary updated"
    if op == "set_meta":
        # 改 KG 顶层元数据。允许的字段白名单（含 scan_config 嵌套支持）。
        ALLOWED = {"note_prefix", "title", "scan_config"}
        key = payload.get("key", "")
        val = payload.get("value")
        if key not in ALLOWED:
            return False, f"unknown meta key: {key}"
        kg[key] = val
        return True, f"set {key}"
    return False, f"unknown op: {op}"


def register_skilltree(app):

    @app.route("/skilltree/")
    def skilltree_index():
        return render_template("skilltree_index.html", books=list_books())

    @app.route("/skilltree/api/books-meta")
    def skilltree_books_meta():
        """返回所有书的元数据（控制面板技能树 tab 用）。"""
        out = []
        if not KG_DIR.exists():
            return jsonify(out)
        for f in sorted(KG_DIR.glob("*.json")):
            if f.name.endswith(".bak.json"): continue
            try:
                kg = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            l2 = [n for n in kg.get("nodes", []) if n.get("level") == 2]
            mastered = sum(1 for n in l2 if n.get("state") == "mastered")
            pdf_path = kg.get("pdf", "")
            pdf_exists = bool(pdf_path) and Path(pdf_path).exists()
            scan_cfg = kg.get("scan_config") or {}
            out.append({
                "id": kg.get("book") or f.stem,
                "title": kg.get("title") or kg.get("book") or f.stem,
                "kg_file": f.name,
                "pdf_path": pdf_path,
                "pdf_exists": pdf_exists,
                "note_prefix": kg.get("note_prefix") or "",
                "node_count": len(kg.get("nodes", [])),
                "l2_count": len(l2),
                "mastered_count": mastered,
                "edge_count": len(kg.get("edges", [])),
                "scan_config": {
                    "pages_per_night":  scan_cfg.get("pages_per_night", 30),
                    "stable_threshold": scan_cfg.get("stable_threshold", 3),
                    "max_stable_days":  scan_cfg.get("max_stable_days", 90),
                    "deep_model":       scan_cfg.get("deep_model", "opus"),
                    "deep_effort":      scan_cfg.get("deep_effort", "high"),
                    "enabled":          scan_cfg.get("enabled", True),
                },
            })
        return jsonify(out)

    @app.route("/skilltree/<book>/api/delete", methods=["POST"])
    def skilltree_delete_book(book):
        """删除一本书：KG json 移到归档 + 可选删 PDF。"""
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        delete_pdf = bool(body.get("delete_pdf"))
        p = _kg_path(book)
        if not p:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        archive_dir = KG_DIR / "_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = archive_dir / f"{p.stem}.{ts}.json.bak"
        # 同时归档 progress / audit 状态相关
        try:
            kg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            kg = None
        pdf_path = (kg or {}).get("pdf", "") if kg else ""
        p.rename(archived)
        msg = f"KG 已归档至 {archived.name}"
        if delete_pdf and pdf_path and Path(pdf_path).exists():
            try:
                Path(pdf_path).unlink()
                msg += f"；PDF 已删除"
            except Exception as ex:
                msg += f"；PDF 删除失败: {ex}"
        return jsonify({"ok": True, "info": msg, "archived": archived.name})

    @app.route("/skilltree/<book>/")
    def skilltree_view(book):
        p = _kg_path(book)
        if not p:
            abort(404)
        return render_template("skilltree.html", book=book)

    @app.route("/skilltree/<book>/data.json")
    def skilltree_data(book):
        p, kg = _load_kg(book)
        if not kg:
            abort(404)
        # 派生 PDF 的 vault 相对路径（给前端构 obsidian:// 链接）
        pdf_abs = kg.get("pdf")
        if pdf_abs:
            vault_root = os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian")
            try:
                rel = Path(pdf_abs).resolve().relative_to(Path(vault_root).resolve())
                kg["pdf_vault_path"] = str(rel).replace("\\", "/")
            except (ValueError, OSError):
                pass  # PDF 不在 vault 内则忽略
        return jsonify(kg)

    @app.route("/skilltree/<book>/pdf")
    def skilltree_pdf(book):
        """流式返回 KG 关联的 PDF；浏览器原生 PDF viewer 支持 #page=N 锚点。"""
        p, kg = _load_kg(book)
        if not kg:
            abort(404)
        pdf_path = kg.get("pdf")
        if not pdf_path:
            abort(404)
        pdf = Path(pdf_path)
        if not pdf.exists() or not pdf.is_file():
            abort(404)
        # 防 path traversal：只允许 KG 显式声明的那个 PDF
        try:
            return send_file(str(pdf), mimetype="application/pdf",
                             as_attachment=False,
                             download_name=pdf.name,
                             max_age=86400)
        except Exception:
            abort(500)

    @app.route("/skilltree/<book>/api/archive-node", methods=["POST"])
    def skilltree_archive_node(book):
        """把节点 PDF 内容 append 到回收站笔记（按章分册）；containing_notes
        指向该回收站笔记 → link_and_mastery 自动 level=2/5（不阻塞下游）。"""
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        node_id = body.get("node_id") or ""
        node = next((n for n in kg["nodes"] if n["id"] == node_id), None)
        if not node or node.get("level") != 2:
            return jsonify({"ok": False, "error": "node not found / not L2"}), 404
        pdf_path = kg.get("pdf")
        if not pdf_path or not Path(pdf_path).exists():
            return jsonify({"ok": False, "error": "pdf 缺失"}), 400
        try:
            trash_path = _archive_node_to_trash(kg, node, Path(pdf_path))
        except Exception as ex:
            return jsonify({"ok": False, "error": f"archive 失败: {ex}"}), 500
        with _edit_lock:
            _, kg2 = _load_kg(book)
            if not kg2:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            persistent = kg2.setdefault("_note_to_covered_l2", {})
            persistent.setdefault(trash_path, [])
            if node_id not in persistent[trash_path]:
                persistent[trash_path].append(node_id)
            for n in kg2["nodes"]:
                if n["id"] == node_id:
                    cn = sorted(set((n.get("containing_notes") or []) + [trash_path]))
                    n["containing_notes"] = cn
                    n["note_ref"] = cn[0]
                    n["note_ref_ai_verified"] = True
                    break
            # 用户手动归档后，从 suggestions 移除该 id
            sugg = kg2.get("_archive_suggestions") or []
            if node_id in sugg:
                kg2["_archive_suggestions"] = [x for x in sugg if x != node_id]
            _save_kg(p, kg2)
            _trigger_mastery_recompute(p)
        return jsonify({"ok": True, "trash_path": trash_path,
                         "message": f"已收入回收站：{trash_path}"})

    @app.route("/skilltree/<book>/api/restore-node", methods=["POST"])
    def skilltree_restore_node(book):
        """从回收站笔记移除该节点段，新建独立笔记（不调 AI，纯文件操作）。"""
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        node_id = body.get("node_id") or ""
        node = next((n for n in kg["nodes"] if n["id"] == node_id), None)
        if not node:
            return jsonify({"ok": False, "error": "node not found"}), 404
        try:
            new_path, removed_from = _restore_node_from_trash(kg, node)
        except Exception as ex:
            return jsonify({"ok": False, "error": f"restore 失败: {ex}"}), 500
        with _edit_lock:
            _, kg2 = _load_kg(book)
            if not kg2:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            persistent = kg2.setdefault("_note_to_covered_l2", {})
            # 旧回收站笔记里删该 node
            if removed_from in persistent and node_id in persistent[removed_from]:
                persistent[removed_from].remove(node_id)
            if new_path:
                persistent.setdefault(new_path, [])
                if node_id not in persistent[new_path]:
                    persistent[new_path].append(node_id)
            for n in kg2["nodes"]:
                if n["id"] == node_id:
                    cn = [c for c in (n.get("containing_notes") or []) if c != removed_from]
                    if new_path and new_path not in cn:
                        cn.append(new_path)
                    n["containing_notes"] = sorted(cn)
                    n["note_ref"] = n["containing_notes"][0] if n["containing_notes"] else ""
                    n["note_ref_ai_verified"] = bool(n["containing_notes"])
                    break
            _save_kg(p, kg2)
            _trigger_mastery_recompute(p)
        return jsonify({"ok": True, "new_path": new_path, "removed_from": removed_from,
                         "message": f"已还原为独立笔记：{new_path}"})

    @app.route("/skilltree/<book>/api/prune-notes", methods=["POST"])
    def skilltree_prune_notes(book):
        """清理 KG 里悬空笔记关联（笔记文件已从 vault 删除）。
        重建每个节点的 containing_notes，更新 KG 后触发 mastery 重算。"""
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        vault_root = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
        with _edit_lock:
            _, kg = _load_kg(book)
            if not kg:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            persistent = kg.get("_note_to_covered_l2", {}) or {}
            pruned = []
            for path in list(persistent.keys()):
                if not (vault_root / path).exists():
                    del persistent[path]
                    pruned.append(path)
            if pruned:
                kg["_note_to_covered_l2"] = persistent
                # 重建每个 L2 节点的 containing_notes
                from collections import defaultdict as _dd
                node_to_notes: dict = _dd(list)
                for note_rel, covered_ids in persistent.items():
                    for nid in covered_ids:
                        if note_rel not in node_to_notes[nid]:
                            node_to_notes[nid].append(note_rel)
                id2 = {n["id"]: n for n in kg["nodes"]}
                for n in kg["nodes"]:
                    if n["level"] != 2: continue
                    notes = sorted(node_to_notes.get(n["id"], []))
                    if notes:
                        n["containing_notes"] = notes
                        n["note_ref"] = notes[0]
                        n["note_ref_ai_verified"] = True
                    else:
                        n["containing_notes"] = []
                        n["note_ref"] = ""
                        n["note_ref_ai_verified"] = False
                # L1 也按 union 重建
                l1_notes: dict = _dd(set)
                for n in kg["nodes"]:
                    if n["level"]==2 and n.get("containing_notes") and n.get("parent_id"):
                        for nt in n["containing_notes"]:
                            l1_notes[n["parent_id"]].add(nt)
                for n in kg["nodes"]:
                    if n["level"] != 1: continue
                    nts = sorted(l1_notes.get(n["id"], set()))
                    if nts:
                        n["containing_notes"] = nts
                        n["note_ref"] = nts[0]
                        n["note_ref_ai_verified"] = True
                    else:
                        n["containing_notes"] = []
                        n["note_ref"] = ""
                        n["note_ref_ai_verified"] = False
                _save_kg(p, kg)
                _trigger_mastery_recompute(p)
        return jsonify({"ok": True, "pruned": len(pruned), "examples": pruned[:5]})

    @app.route("/skilltree/<book>/api/build-note", methods=["POST"])
    def skilltree_build_note(book):
        """根据 KG 节点的 pages 从 PDF 提取相关内容，生成笔记。
        - L2 节点：单节点笔记，含该节点 numeric_label 起的文本块
        - L1 节点：整节笔记，含该节所有 L2 节点各一段
        生成的笔记用 ![[X.pdf#page=N&rect=...]] obsidian 嵌入语法。
        写入 vault 根 + 更新 KG containing_notes + 触发 link_and_mastery 重算。
        """
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        node_id = body.get("node_id") or ""
        node = next((n for n in kg["nodes"] if n["id"] == node_id), None)
        if not node:
            return jsonify({"ok": False, "error": "node not found"}), 404
        pdf_path = kg.get("pdf")
        if not pdf_path or not Path(pdf_path).exists():
            return jsonify({"ok": False, "error": "pdf 缺失"}), 400

        # ===== AI 验证（仅 L2，L1 整节笔记不验证因为节点本身没具体定义）=====
        if node["level"] == 2:
            try:
                import fitz
                doc = fitz.open(pdf_path)
                page_texts = []
                for pg in (node.get("pages") or []):
                    if 1 <= pg <= len(doc):
                        page_texts.append(doc[pg-1].get_text())
                doc.close()
                raw_pdf = "\n---\n".join(page_texts) if page_texts else ""
            except Exception:
                raw_pdf = ""
            if raw_pdf:
                verify = _ai_verify_node(kg, node, raw_pdf)
                action = verify.get("action")
                reason = verify.get("reason", "")
                if action == "delete":
                    # AI 判断 KG 节点是误识别 → 自动删除
                    with _edit_lock:
                        _, kg2 = _load_kg(book)
                        if kg2:
                            ok, info = _apply_edit(kg2, "delete_node", {"id": node_id})
                            if ok:
                                _save_kg(p, kg2); _trigger_mastery_recompute(p)
                    return jsonify({
                        "ok": False, "auto_action": "deleted",
                        "reason": reason,
                        "message": f"AI 判定节点是 KG 误识别，已删除：{reason}",
                    })
                if action == "merge":
                    target_id = verify.get("target_id", "")
                    target = next((n for n in kg["nodes"] if n["id"] == target_id), None)
                    if target_id and target:
                        with _edit_lock:
                            _, kg2 = _load_kg(book)
                            if kg2:
                                ok, info = _apply_edit(kg2, "merge",
                                    {"canonical": target_id, "drop": [node_id]})
                                if ok:
                                    _save_kg(p, kg2); _trigger_mastery_recompute(p)
                        return jsonify({
                            "ok": False, "auto_action": "merged",
                            "target_id": target_id, "target_name": target["name"],
                            "reason": reason,
                            "message": f"AI 判定该节点是「{target['name']}」的重复，已合并：{reason}",
                        })
                # action=ok 继续走正常 build-note

        try:
            new_path, covered_ids = _build_note_for_node(kg, node, Path(pdf_path))
        except Exception as ex:
            return jsonify({"ok": False, "error": f"生成失败: {ex}"}), 500

        # 更新 KG 持久化字典 + 节点
        with _edit_lock:
            _, kg2 = _load_kg(book)
            if not kg2:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            persistent = kg2.setdefault("_note_to_covered_l2", {})
            persistent[new_path] = covered_ids
            # 重建节点 containing_notes
            id2 = {n["id"]: n for n in kg2["nodes"]}
            for cid in covered_ids:
                if cid in id2:
                    n2 = id2[cid]
                    notes = list(set((n2.get("containing_notes") or []) + [new_path]))
                    n2["containing_notes"] = sorted(notes)
                    n2["note_ref"] = sorted(notes)[0]
                    n2["note_ref_ai_verified"] = True
            _save_kg(p, kg2)
            _trigger_mastery_recompute(p)

        return jsonify({
            "ok": True, "note_path": new_path,
            "covered_node_ids": covered_ids,
            "obsidian_url": f"obsidian://open?vault={os.environ.get('OBSIDIAN_VAULT_NAME','Obsidian Vault')}&file={new_path.replace('.md','')}",
        })

    @app.route("/skilltree/<book>/api/edit", methods=["POST"])
    def skilltree_edit(book):
        p, kg = _load_kg(book)
        if not kg:
            return jsonify({"ok": False, "error": "kg not found"}), 404
        try:
            body = request.get_json(force=True) or {}
        except Exception:
            body = {}
        op = body.get("op") or ""
        with _edit_lock:
            # 重新加载一次（防止并发覆盖）
            _, kg = _load_kg(book)
            if not kg:
                return jsonify({"ok": False, "error": "kg reload failed"}), 500
            ok, info = _apply_edit(kg, op, body)
            if ok:
                # 备份（最近一份）
                bak = p.with_suffix(".bak.json")
                try: bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception: pass
                _save_kg(p, kg)
                _trigger_mastery_recompute(p)
        return jsonify({"ok": ok, "info": info,
                        "nodes": len(kg.get("nodes", [])),
                        "edges": len(kg.get("edges", []))})
