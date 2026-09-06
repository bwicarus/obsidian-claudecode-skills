"""页级分析（2026-09-07 用户拍板：**页是最小单位，整本书只是页的连续**）。

AI 第一次拿到某页的全量内容时，先回答用户，再交一份结构化分析（``submit``）。程序一次落账：
- 概念 → 节点（按编号 / 公共编号 / 名称与别名解析，解析不到才新建；带 qid 就绑并回填三语别名）
- 被定义/被陈述的概念 → 定义（原句 + 书页出处）+ ``uses`` → 前置（依据 = 原句；查环、查冗余）
- 记号 → 节点别名（origin=page:<书>:<页>，重交覆盖）
- 书里点明的坑 → 记录（按页去重）
- 公式 LaTeX / 图描述 → 写回 ``state/pdf-figures/<sha>.json`` 边车（阅读器据此注入字符层、显示图注）
- 页标注、页→节点 → 事件 ``page.analyze`` 的投影 ``page_analyses`` / ``page_nodes``
已分析的页再被读到时给 ``brief``（标注 + 节点掌握度 + 公式 + 图描述），提示语也不同（``snapshot_block``）。
整本书的批处理**暂时手动**（``book_pages`` 列出未分析页），等 token 消耗量清楚再谈自动。

书的键 = pdf_reader ``_book_sha`` 口径：sha1(绝对路径)[:16]。页号 = 阅读器 / 边车同款 PDF 页号（1 起）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import ids
from . import query as Q
from . import register as R
from .register import RegisterError
from .store import Ledger, dumps, loads

ANALYSIS_VERSION = 1
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
CONCEPT_ROLES = ("defined", "stated", "used", "exercised")

UNANALYZED_HINT = (
    "这一页还没做过分析。先回答用户的问题；答完后在同一轮里用 kj_page_submit 交一份本页分析："
    "summary（这页在干什么）、kind（definition/theorem/proof/example/exercise/prose 多选）、"
    "notation（记号→含义→所属概念）、concepts（这页定义/陈述/用到/练到的概念；被定义或被陈述的带 definition{text: 原句, uses: 看懂它必须先会的概念}；"
    "定理、引理也是概念，kind=theorem；能确定公共编号就带 qid）、formulas（按 boxes 里的 idx 填 LaTeX，需看页图，没看图就不填）、"
    "figures（按 idx 写描述）、exercises（题号与练到的概念）、pitfalls（书里点明的易错处）。"
    "程序负责建节点、登定义与前置、写回公式与图描述、打标记；交错了可以再交覆盖。"
)
ANALYZED_HINT = (
    "这一页已分析过：下面是页标注、出现的节点及其掌握度与准备度、公式 LaTeX、图描述，直接用，不必重读整页；"
    "发现有误可用 kj_page_submit 重交覆盖。"
)


# ── 键与边车 ────────────────────────────────────────────────────────────────
def book_key(book: Any) -> str:
    """16 位 hex 直接用；路径 → sha1(绝对路径)[:16]（与 pdf_reader._book_sha 同口径）；别的字符串也取 sha。"""
    s = str(book or "").strip()
    if not s:
        raise RegisterError("bad_book", "book 不能为空（书的 sha 或绝对路径）")
    if _HEX16.match(s):
        return s
    p = Path(s)
    if p.is_absolute() or p.exists():
        s = str(p.resolve())
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _page_no(page: Any) -> int:
    try:
        n = int(page)
    except Exception:
        raise RegisterError("bad_page", f"page 必须是整数页号：{page!r}")
    if n <= 0:
        raise RegisterError("bad_page", f"页号从 1 起：{n}")
    return n


def figures_dir(ledger: Ledger) -> Path:
    """pdf-figures 边车目录：env KJ_FIGURES_DIR，否则 <账本所在 state>/pdf-figures。"""
    env = os.environ.get("KJ_FIGURES_DIR")
    if env:
        return Path(env)
    return ledger.path.parent.parent / "pdf-figures"


def _sidecar_path(ledger: Ledger, key: str) -> Path:
    return figures_dir(ledger) / f"{key}.json"


def _load_sidecar(ledger: Ledger, key: str) -> dict | None:
    p = _sidecar_path(ledger, key)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text("utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _page_entries(items: Any, page: int) -> list[tuple[int, dict]]:
    """边车里属于这一页的条目，(全局下标, 条目)；idx 按页内顺序从 0 起。"""
    out: list[tuple[int, dict]] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        try:
            if int(it.get("page")) == page:
                out.append((i, it))
        except Exception:
            continue
    return out


def page_boxes(ledger: Ledger, key: str, page: int) -> dict:
    """这一页 YOLO 给的几何：公式框（有无 LaTeX）与图框（有无描述）。没有边车 → 空且 sidecar=False。"""
    sc = _load_sidecar(ledger, key)
    if sc is None:
        return {"sidecar": False, "formulas": [], "figures": []}
    formulas = [{"idx": n, "bbox": it.get("bbox"), "latex": it.get("latex") or None}
                for n, (_, it) in enumerate(_page_entries(sc.get("formulas"), page))]
    figures = [{"idx": n, "bbox": it.get("fbox") or it.get("bbox"), "caption": it.get("caption") or "", "desc": it.get("desc") or None}
               for n, (_, it) in enumerate(_page_entries(sc.get("figures_geom"), page))]
    return {"sidecar": True, "formulas": formulas, "figures": figures}


def _write_sidecar(path: Path, data: dict) -> None:
    """写前备份 .bak，原子替换；保持紧凑单行 JSON（与 formula_writeback 同款）。"""
    bak = path.with_suffix(path.suffix + ".bak")
    try:
        bak.write_bytes(path.read_bytes())
    except Exception:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    os.replace(tmp, path)


def apply_sidecar(ledger: Ledger, key: str, page: int, formulas: Any, figures: Any) -> dict:
    """把 AI 交的公式 LaTeX / 图描述按页内 idx 写回边车。返回写了几条、哪些 idx 对不上。"""
    rep = {"sidecar": False, "formulas_written": 0, "figures_written": 0, "unmatched": []}
    if not (formulas or figures):
        return rep
    path = _sidecar_path(ledger, key)
    sc = _load_sidecar(ledger, key)
    if sc is None:
        rep["unmatched"] = [f"formula:{f.get('idx')}" for f in (formulas or []) if isinstance(f, dict)] + \
                           [f"figure:{f.get('idx')}" for f in (figures or []) if isinstance(f, dict)]
        return rep
    rep["sidecar"] = True
    changed = False
    f_entries = _page_entries(sc.get("formulas"), page)
    for f in formulas or []:
        if not isinstance(f, dict):
            continue
        try:
            n = int(f.get("idx"))
        except Exception:
            rep["unmatched"].append(f"formula:{f.get('idx')}")
            continue
        latex = str(f.get("latex") or "").strip()
        if not (0 <= n < len(f_entries)) or not latex:
            rep["unmatched"].append(f"formula:{n}")
            continue
        _, it = f_entries[n]
        it["latex"] = latex
        it["latex_engine"] = "page-analysis"
        if f.get("kind"):
            it["kind"] = str(f["kind"])
        rep["formulas_written"] += 1
        changed = True
    g_entries = _page_entries(sc.get("figures_geom"), page)
    for g in figures or []:
        if not isinstance(g, dict):
            continue
        try:
            n = int(g.get("idx"))
        except Exception:
            rep["unmatched"].append(f"figure:{g.get('idx')}")
            continue
        desc = str(g.get("desc") or "").strip()
        if not (0 <= n < len(g_entries)) or not desc:
            rep["unmatched"].append(f"figure:{n}")
            continue
        _, it = g_entries[n]
        it["desc"] = desc
        it["desc_engine"] = "page-analysis"
        rep["figures_written"] += 1
        changed = True
    if changed:
        _write_sidecar(path, sc)
    return rep


# ── 提交 ──────────────────────────────────────────────────────────────────
def _resolve_concept(ledger: Ledger, c: dict) -> tuple[str | None, str, list[dict]]:
    """(节点 id 或 None, 解析途径, 多义候选)。途径：id / qid / name / alias / ambiguous / new。"""
    nid = str(c.get("node_id") or "").strip()
    if nid and ids.is_node_id(nid):
        row = ledger.resolve(nid)
        if row is not None:
            return row["id"], "id", []
    qid = str(c.get("qid") or "").strip()
    if qid and ids.is_qid(qid):
        row = ledger.find_by_qid(qid)
        if row is not None:
            return row["id"], "qid", []
    name = str(c.get("name") or "").strip()
    if name:
        rid, cands = R.resolve_node_ref(ledger, name)
        if rid:
            return rid, "name", []
        if cands:
            return None, "ambiguous", cands
    for a in c.get("aliases") or []:
        rid, cands = R.resolve_node_ref(ledger, str(a))
        if rid:
            return rid, "alias", []
    return None, "new", []


def _bind_public(ledger: Ledger, nid: str, qid: str, actor: str, rep: dict) -> None:
    """节点已有 → 绑编号并回填别名、接公共关系；编号被占 → 记进报告不抛。"""
    try:
        R.bind_qid(ledger, nid, qid, actor=actor)
        R.sync_public_aliases(ledger, nid, qid, actor=actor)
        rep["qid_bound"].append({"node_id": nid, "qid": qid})
    except RegisterError as e:
        rep["qid_problems"].append({"node_id": nid, "qid": qid, "code": e.code, **{k: v for k, v in e.extra.items() if k in ("node_id", "name")}})


def submit(ledger: Ledger, payload: dict, *, actor: str = "") -> tuple[dict, set[str]]:
    """一页分析落账。返回 (报告, 受影响节点集合)。payload 结构见模块顶部与 UNANALYZED_HINT。"""
    if not isinstance(payload, dict):
        raise RegisterError("bad_payload", "payload 必须是对象")
    key = book_key(payload.get("book"))
    page = _page_no(payload.get("page"))
    title = str(payload.get("book_title") or payload.get("title") or "").strip()
    src: dict[str, Any] = {"kind": "pdf", "sha": key, "page": page}
    if title:
        src["book"] = title
    rep: dict[str, Any] = {"book": key, "page": page, "version": ANALYSIS_VERSION,
                           "nodes_created": [], "nodes_resolved": [], "ambiguous": [], "skipped": [],
                           "qid_bound": [], "qid_problems": [],
                           "definitions_added": [], "definition_exists": [], "prereqs_added": [], "relations_added": [],
                           "redundant": [], "rejected": [], "unresolved_uses": [], "also_mentioned": [],
                           "notation_set": 0, "records_added": 0, "records_duplicate": 0}
    touched: set[str] = set()
    node_roles: list[dict] = []
    name_to_id: dict[str, str] = {}

    concepts = payload.get("concepts") or []
    if not isinstance(concepts, list):
        raise RegisterError("bad_payload", "concepts 必须是数组")
    # ① 概念 → 节点（先全部解析/新建，定义的 uses 才能互相引用同页新建的概念）
    resolved: list[tuple[dict, str]] = []
    for c in concepts:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        nid, how, cands = _resolve_concept(ledger, c)
        if nid is None and how == "ambiguous":
            rep["ambiguous"].append({"name": name, "candidates": cands})
            continue
        if nid is None:
            if not name:
                rep["skipped"].append({"reason": "no_name", "concept": c})
                continue
            kind = str(c.get("kind") or "concept").strip().lower()
            qid = str(c.get("qid") or "").strip() or None
            if qid and ledger.find_by_qid(qid) is not None:
                qid = None   # 已被别的节点占用：不带着建，下面按普通节点建并记问题
            try:
                ev = R.create_node(ledger, name=name, kind=kind, aliases=c.get("aliases"), qid=qid,
                                   summary=str(c.get("summary") or ""), actor=actor, source=src)
            except RegisterError as e:
                rep["skipped"].append({"reason": e.code, "concept": name})
                continue
            nid = ev.payload["id"]
            rep["nodes_created"].append({"node_id": nid, "name": name, "kind": ev.payload["kind"], "qid": qid})
            if qid:
                R.sync_public_aliases(ledger, nid, qid, actor=actor)
                R.generate_auto_relations(ledger, nid, qid, actor=actor)
            elif c.get("qid"):
                other = ledger.find_by_qid(str(c["qid"]).strip())
                rep["qid_problems"].append({"node_id": nid, "qid": str(c["qid"]).strip(), "code": "qid_taken",
                                            "node": other["id"] if other else None})
        else:
            rep["nodes_resolved"].append({"node_id": nid, "name": name, "via": how})
            qid = str(c.get("qid") or "").strip()
            row = ledger.node(nid)
            if qid and ids.is_qid(qid) and row is not None and not row["qid"]:
                _bind_public(ledger, nid, qid, actor, rep)
            new_aliases = [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()]
            if new_aliases:
                cur = [a["alias"] for a in ledger.aliases(nid) if not a.get("origin")]
                have = {a["alias"].lower() for a in ledger.aliases(nid)} | {(row["name"] or "").lower()}
                add = [a for a in new_aliases if a.lower() not in have]
                if add:
                    R.update_node(ledger, nid, aliases=cur + add, actor=actor)
        touched.add(nid)
        if name:
            name_to_id[name.lower()] = nid
        for a in c.get("aliases") or []:
            name_to_id[str(a).strip().lower()] = nid
        resolved.append((c, nid))
        role = str(c.get("role") or ("defined" if c.get("definition") else "used")).strip().lower()
        node_roles.append({"node_id": nid, "name": ledger.node(nid)["name"], "role": role if role in CONCEPT_ROLES else "used"})

    # ② 定义 + uses → 前置
    for c, nid in resolved:
        d = c.get("definition")
        if not isinstance(d, dict) or not str(d.get("text") or "").strip():
            continue
        text = str(d["text"]).strip()
        dsrc = dict(src)
        dsrc["quote"] = text[:200]
        try:
            ev = R.add_definition(ledger, nid, text=text, source=dsrc, actor=actor)
            rep["definitions_added"].append({"node_id": nid, "definition_id": ev.payload["id"]})
        except RegisterError as e:
            if e.code == "definition_exists":
                rep["definition_exists"].append({"node_id": nid, "existing": [x["id"] for x in e.extra.get("existing", [])]})
            else:
                rep["rejected"].append({"node_id": nid, "code": e.code, "what": "definition"})
                continue
        uses = d.get("uses") or []
        uses = [name_to_id.get(str(u).strip().lower(), u) if not isinstance(u, dict) else u for u in uses]
        ur = R.attach_definition_uses(ledger, nid, definition_text=text, source=dsrc, uses=uses, actor=actor)
        for k in ("prereqs_added", "relations_added", "redundant", "rejected", "unresolved_uses"):
            rep[k].extend(ur.get(k, []))
        rep["also_mentioned"].extend({**m, "for": nid} for m in ur.get("also_mentioned", []))
        for e in ur.get("prereqs_added", []) + ur.get("relations_added", []):
            touched.add(e["from"])

    # ③ 记号 → 别名（origin=page:<书>:<页>，重交整体覆盖）
    by_node: dict[str, list[dict]] = {}
    for n in payload.get("notation") or []:
        if not isinstance(n, dict):
            continue
        sym = str(n.get("symbol") or "").strip()
        ref = str(n.get("concept") or n.get("node") or "").strip()
        if not sym or not ref:
            continue
        nid = name_to_id.get(ref.lower()) or R.resolve_node_ref(ledger, ref)[0]
        if not nid:
            rep["unresolved_uses"].append(f"notation:{ref}")
            continue
        by_node.setdefault(nid, []).append({"alias": sym, "lang": "symbol", "meaning": str(n.get("meaning") or "")[:120]})
    for nid, items in by_node.items():
        ledger.append("node.aliases_sync", {"id": nid, "origin": f"page:{key}:{page}", "aliases": items},
                      node_ids=[nid], actor=actor or "page")
        rep["notation_set"] += len(items)
        touched.add(nid)

    # ④ 坑 → 记录（按页 + 文本去重）
    for pf in payload.get("pitfalls") or []:
        if not isinstance(pf, dict) or not str(pf.get("text") or "").strip():
            continue
        text = str(pf["text"]).strip()
        ref = str(pf.get("concept") or pf.get("node") or "").strip()
        nid = name_to_id.get(ref.lower()) or (R.resolve_node_ref(ledger, ref)[0] if ref else None)
        if not nid:
            rep["unresolved_uses"].append(f"pitfall:{ref or text[:20]}")
            continue
        dk = f"page:{key}:{page}:pitfall:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"
        ev = R.add_record(ledger, nid, text=text, kind="observation", source=src, actor=actor, dedupe_key=dk)
        rep["records_duplicate" if ev.duplicate else "records_added"] += 1
        touched.add(nid)

    # ⑤ 习题 → 页→节点（role=exercised）
    exercises: list[dict] = []
    for ex in payload.get("exercises") or []:
        if not isinstance(ex, dict):
            continue
        nids = []
        for ref in ex.get("concepts") or []:
            r = str(ref).strip()
            nid = name_to_id.get(r.lower()) or R.resolve_node_ref(ledger, r)[0]
            if nid:
                nids.append(nid)
                node_roles.append({"node_id": nid, "name": ledger.node(nid)["name"], "role": "exercised"})
        exercises.append({"label": str(ex.get("label") or "").strip(), "node_ids": nids, "note": str(ex.get("note") or "")[:200]})

    # ⑥ 公式 / 图 → 边车
    side = apply_sidecar(ledger, key, page, payload.get("formulas"), payload.get("figures"))
    rep["sidecar"] = side

    # ⑦ 页事件（投影 page_analyses / page_nodes）
    kinds = payload.get("kind") or []
    if isinstance(kinds, str):
        kinds = [kinds]
    ev = ledger.append("page.analyze", {
        "book": key, "page": page, "version": ANALYSIS_VERSION, "book_title": title, "actor": actor,
        "summary": str(payload.get("summary") or "").strip()[:600],
        "kind": [str(k) for k in kinds][:8],
        "notation": [{"symbol": str(n.get("symbol") or ""), "meaning": str(n.get("meaning") or "")[:120], "concept": str(n.get("concept") or n.get("node") or "")}
                     for n in (payload.get("notation") or []) if isinstance(n, dict)][:40],
        "nodes": node_roles, "exercises": exercises,
        "pitfalls": rep["records_added"] + rep["records_duplicate"],
        "formulas": {"written": side["formulas_written"]}, "figures": {"written": side["figures_written"]},
    }, node_ids=sorted(touched), actor=actor or "page")
    rep["event_id"] = ev.id
    rep["analyzed"] = True
    return rep, touched


# ── 读取 ──────────────────────────────────────────────────────────────────
def _analysis_row(ledger: Ledger, key: str, page: int):
    return ledger.db.execute("SELECT * FROM page_analyses WHERE book=? AND page=?", (key, page)).fetchone()


def status(ledger: Ledger, book: Any, page: Any) -> dict:
    key, pg = book_key(book), _page_no(page)
    row = _analysis_row(ledger, key, pg)
    boxes = page_boxes(ledger, key, pg)
    out = {"book": key, "page": pg, "analyzed": row is not None,
           "formulas": {"total": len(boxes["formulas"]), "with_latex": sum(1 for f in boxes["formulas"] if f["latex"])},
           "figures": {"total": len(boxes["figures"]), "with_desc": sum(1 for f in boxes["figures"] if f["desc"])},
           "sidecar": boxes["sidecar"]}
    if row is not None:
        out.update({"version": row["version"], "analyzed_at": row["analyzed_at"], "actor": row["actor"],
                    "node_count": ledger.db.execute("SELECT COUNT(DISTINCT node_id) FROM page_nodes WHERE book=? AND page=?", (key, pg)).fetchone()[0]})
    return out


def brief(ledger: Ledger, book: Any, page: Any) -> dict:
    """已分析页的摘要：标注 + 节点（掌握度/准备度）+ 公式 LaTeX + 图描述。未分析 → analyzed=False。"""
    st = status(ledger, book, page)
    key, pg = st["book"], st["page"]
    row = _analysis_row(ledger, key, pg)
    if row is None:
        return st
    p = loads(row["payload_json"], {})
    nodes = []
    for r in ledger.db.execute("SELECT node_id, role FROM page_nodes WHERE book=? AND page=? ORDER BY rowid", (key, pg)):
        b = Q.node_brief(ledger, r["node_id"])
        if b:
            b["role"] = r["role"]
            nodes.append(b)
    boxes = page_boxes(ledger, key, pg)
    st.update({"summary": p.get("summary", ""), "kind": p.get("kind", []), "notation": p.get("notation", []),
               "nodes": nodes, "exercises": p.get("exercises", []),
               "formulas": [{"idx": f["idx"], "latex": f["latex"]} for f in boxes["formulas"] if f["latex"]],
               "figures": [{"idx": f["idx"], "desc": f["desc"]} for f in boxes["figures"] if f["desc"]]})
    return st


def snapshot_block(ledger: Ledger, book: Any, page: Any) -> dict:
    """附在整页快照后面的块：未分析 → 指示 + YOLO 框；已分析 → brief + 提示。所有给出整页内容的表面都调这一个函数。"""
    st = status(ledger, book, page)
    if st["analyzed"]:
        b = brief(ledger, book, page)
        b["status"] = "analyzed"
        b["instruction"] = ANALYZED_HINT
        return b
    boxes = page_boxes(ledger, st["book"], st["page"])
    return {"status": "unanalyzed", "book": st["book"], "page": st["page"], "instruction": UNANALYZED_HINT,
            "boxes": {"formulas": [{"idx": f["idx"], "bbox": f["bbox"]} for f in boxes["formulas"]],
                      "figures": [{"idx": f["idx"], "bbox": f["bbox"], "caption": f["caption"]} for f in boxes["figures"]],
                      "sidecar": boxes["sidecar"]}}


def book_pages(ledger: Ledger, book: Any, total: int | None = None) -> dict:
    """一本书哪些页分析过。total 给了就列未分析页（整本手动批处理用）。"""
    key = book_key(book)
    done = [r[0] for r in ledger.db.execute("SELECT page FROM page_analyses WHERE book=? ORDER BY page", (key,))]
    out: dict[str, Any] = {"book": key, "analyzed_pages": done, "analyzed": len(done)}
    sc = _load_sidecar(ledger, key)
    if sc is not None:
        pages_with_boxes = sorted({int(it["page"]) for k in ("formulas", "figures_geom") for it in (sc.get(k) or []) if isinstance(it, dict) and str(it.get("page", "")).isdigit()})
        out["pages_with_boxes"] = len(pages_with_boxes)
    if total:
        out["total"] = int(total)
        out["unanalyzed_pages"] = [p for p in range(1, int(total) + 1) if p not in set(done)]
    return out


def node_pages(ledger: Ledger, node_id: str) -> list[dict]:
    """节点出现在哪些书的哪些页（书内位置）。"""
    return [dict(r) for r in ledger.db.execute("SELECT book, page, role FROM page_nodes WHERE node_id=? ORDER BY book, page", (node_id,))]
