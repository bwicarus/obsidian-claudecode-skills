"""节点 Markdown 页：程序生成、可重建的人可读视图。**不是数据来源**（账本在 SQLite）。

布局（文档 §4 用户修正）：标题 = 名称；正文 = 定义原文 / 关系链接 / 记录摘要 / 卡片 / 来源；
frontmatter 只放参与计算的值与状态。关系用固定栏目里的 [[内部链接]] 表达，程序可解析。

文件名 = ``<安全名称>·<短id>.md``：改名会重命名文件（旧文件删除、链接全部由程序重新生成）。
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from . import ids
from .store import Ledger
from . import wikidata as WD

INDEX_NAME = "节点索引.md"
_FM_MARK = "kj_id:"


def _ts(t: int | None) -> str:
    if not t:
        return ""
    return _dt.datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d %H:%M")


def _day(t: int | None) -> str:
    return _dt.datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d") if t else "时间未知"


def filename_for(node_id: str, name: str) -> str:
    return f"{ids.safe_filename(name)}·{ids.short(node_id)}.md"


def link_for(ledger: Ledger, node_id: str) -> str:
    n = ledger.node(node_id)
    if n is None:
        return node_id
    return f"[[{filename_for(n['id'], n['name'])[:-3]}|{n['name']}]]"


def _source_line(src: dict | None) -> str:
    if not src:
        return ""
    parts = []
    kind = src.get("kind", "")
    if src.get("book") or src.get("title"):
        parts.append(str(src.get("book") or src.get("title")))
    if src.get("page") is not None:
        parts.append(f"p.{src['page']}")
    if src.get("section"):
        parts.append(str(src["section"]))
    if src.get("url"):
        parts.append(str(src["url"]))
    if src.get("ref") and not parts:
        parts.append(str(src["ref"]))
    if src.get("qid"):
        parts.append(f"Wikidata {src['qid']}")
    head = f"{kind}" if kind and kind not in ("other", "manual") else ""
    body = " · ".join(parts)
    return f"（{head}{'：' if head and body else ''}{body}）" if (head or body) else ""


def render_node(ledger: Ledger, node_id: str, *, records_limit: int = 20) -> str:
    n = ledger.node(node_id)
    if n is None:
        raise KeyError(node_id)
    m = ledger.mastery_row(node_id) or {}
    detail = m.get("detail") or {}
    fm = [
        "---",
        f"kj_id: {n['id']}",
        f"kind: {n['kind']}",
        f"qid: {n['qid'] or ''}",
        f"mastery: {'' if m.get('value') is None else m['value']}",
        f"mastery_level: {m.get('level', 0)}",
        f"progress: {m.get('progress', 'unseen')}",
        f"availability: {m.get('availability', 'open')}",
        f"readiness: {m.get('readiness', 'no_prereq_info')}",
        f"state: {m.get('state', 'unlockable')}",
        f"evidence_count: {m.get('evidence_count', 0)}",
        f"cards: {len(ledger.cards_of(node_id))}",
        f"records: {ledger.db.execute('SELECT COUNT(*) FROM records WHERE node_id=? AND merged_into IS NULL', (node_id,)).fetchone()[0]}",
        f"updated: {_ts(m.get('updated_at') or n['updated_at'])}",
        "---",
    ]
    out = list(fm)
    out.append(f"# {n['name']}")
    aliases = ledger.aliases(node_id)
    if aliases:
        out.append("别名：" + " / ".join(a["alias"] for a in aliases))
    if n["summary"]:
        out.append("")
        out.append(n["summary"])
    if n["qid"]:
        e = WD.entity(ledger, n["qid"])
        label = f"{e['label']} — {e['description']}" if e else "（公共目录未载入）"
        out.append(f"公共编号：[{n['qid']}](https://www.wikidata.org/wiki/{n['qid']}) {label}")

    out += ["", "## 定义"]
    defs = ledger.definitions(node_id)
    if defs:
        for d in defs:
            ctx = f" `{d['context_key']}`" if d["context_key"] else ""
            out.append(f"- {d['text']}{_source_line(d['source'])}{ctx}")
    else:
        out.append("- （尚无定义）")

    out += ["", "## 关系"]
    rels = ledger.relations(node_id)
    pre = [r for r in rels if r["type"] == "prereq" and r["to_id"] == node_id]
    suc = [r for r in rels if r["type"] == "prereq" and r["from_id"] == node_id]
    oth = [r for r in rels if r["type"] != "prereq"]
    out.append("### 前置")
    out += [f"- {link_for(ledger, r['from_id'])} — {r['evidence']}" for r in pre] or ["- （无）"]
    out.append("### 后续")
    out += [f"- {link_for(ledger, r['to_id'])} — {r['evidence']}" for r in suc] or ["- （无）"]
    out.append("### 其它")
    if oth:
        for r in oth:
            other = r["to_id"] if r["from_id"] == node_id else r["from_id"]
            arrow = "→" if r["from_id"] == node_id else "←"
            tag = "（Wikidata）" if r["origin"] == "wikidata" else ""
            out.append(f"- {arrow} {r['type']} {link_for(ledger, other)}{tag}")
    else:
        out.append("- （无）")

    weak = detail.get("prereqs", {}).get("weak", [])
    unknown = detail.get("prereqs", {}).get("unknown", [])
    out += ["", "## 掌握与准备度"]
    val = "无证据" if m.get("value") is None else f"{m['value']:.2f}（{m.get('level', 0)}/5）"
    out.append(f"- 掌握度：{val}，进度 {m.get('progress', 'unseen')}，准备度 {m.get('readiness', 'no_prereq_info')}")
    if weak:
        out.append("- 显示薄弱的前置：" + "、".join(link_for(ledger, w) for w in weak))
    if unknown:
        out.append("- 掌握未知的前置：" + "、".join(link_for(ledger, u) for u in unknown))
    sig = detail.get("signals") or []
    if sig:
        out.append("- 最近证据：" + "；".join(_signal_text(s) for s in sig[-5:]))

    out += ["", "## 记录（程序生成的摘要，账本在数据库）"]
    recs = ledger.records(node_id, limit=records_limit)
    if recs:
        for r in recs:
            when = _day(r["occurred_at"]) if r["occurred_at"] else f"登记 {_day(r['registered_at'])}"
            times = f" ×{r['occurrences']}" if (r["occurrences"] or 1) > 1 else ""
            text = r["text"].replace("\n", " ").strip()
            if len(text) > 300:
                text = text[:300] + "…"
            out.append(f"- {when} [{r['kind']}]{times} {text}{_source_line(r['source'])}")
    else:
        out.append("- （尚无记录）")

    out += ["", "## 卡片"]
    cards = ledger.cards_of(node_id)
    if cards:
        for c in cards:
            front = (c["front"] or "").replace("\n", " ")[:120]
            nid_txt = f" Anki {c['anki_note_id']}" if c["anki_note_id"] else ""
            out.append(f"- {front or c['card_key']}{nid_txt}")
    else:
        out.append("- （无）")
    out.append("")
    return "\n".join(out)


def _signal_text(s: dict) -> str:
    k = s.get("kind")
    if k == "quiz":
        return f"答题 {s.get('result')}" + (f"→{s['m']:.2f}" if "m" in s else "（未计）")
    if k == "anki":
        return f"Anki {s.get('cards')} 卡→{s.get('m', 0):.2f}"
    if k == "self":
        return f"自评→{s.get('m', 0):.2f}"
    return str(s)


class VaultWriter:
    """把节点页写进 vault 的 KJ 目录；只动带 ``kj_id:`` frontmatter 的文件。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.nodes_dir = self.root / "节点"

    def _existing(self, node_id: str) -> list[Path]:
        if not self.nodes_dir.exists():
            return []
        suffix = f"·{ids.short(node_id)}.md"
        return [p for p in self.nodes_dir.iterdir() if p.name.endswith(suffix)]

    def write_node(self, ledger: Ledger, node_id: str) -> Path | None:
        n = ledger.node(node_id)
        if n is None:
            return None
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        if n["status"] != "active":
            for p in self._existing(node_id):
                p.unlink(missing_ok=True)
            return None
        target = self.nodes_dir / filename_for(node_id, n["name"])
        for p in self._existing(node_id):
            if p != target:
                p.unlink(missing_ok=True)
        text = render_node(ledger, node_id)
        if target.exists() and target.read_text("utf-8") == text:
            return target
        target.write_text(text, "utf-8")
        return target

    def write_index(self, ledger: Ledger) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        rows = ledger.db.execute(
            "SELECT n.id, n.name, n.kind, n.qid, m.value, m.level, m.progress, m.readiness FROM nodes n"
            " LEFT JOIN mastery m ON m.node_id=n.id WHERE n.status='active' ORDER BY n.kind, n.name").fetchall()
        out = ["---", "kj_id: index", f"updated: {_ts(int(_dt.datetime.now().timestamp()))}", f"nodes: {len(rows)}", "---",
               "# KJ 节点索引", "", "程序生成；改动请通过登记工具，不要手改本文件。", ""]
        cur_kind = None
        for r in rows:
            if r["kind"] != cur_kind:
                cur_kind = r["kind"]
                out += ["", f"## {cur_kind}"]
            val = "—" if r["value"] is None else f"{r['value']:.2f}"
            out.append(f"- [[{filename_for(r['id'], r['name'])[:-3]}|{r['name']}]] · 掌握 {val} · {r['progress'] or 'unseen'} · {r['readiness'] or 'no_prereq_info'}"
                       + (f" · {r['qid']}" if r["qid"] else ""))
        out.append("")
        p = self.root / INDEX_NAME
        text = "\n".join(out)
        if not (p.exists() and p.read_text("utf-8") == text):
            p.write_text(text, "utf-8")
        return p

    def rebuild_all(self, ledger: Ledger) -> int:
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        live = set()
        for nid in ledger.active_node_ids():
            p = self.write_node(ledger, nid)
            if p:
                live.add(p)
        # 清理孤儿：带 kj_id 头但对应节点已不活跃
        for p in self.nodes_dir.glob("*.md"):
            if p in live:
                continue
            try:
                head = p.read_text("utf-8")[:400]
            except Exception:
                continue
            if _FM_MARK in head:
                p.unlink(missing_ok=True)
        self.write_index(ledger)
        return len(live)


_NODE_ID_IN_FM = re.compile(r"^kj_id:\s*(kj:[0-9A-Z]{10})", re.M)


def node_id_from_file(path: Path) -> str | None:
    try:
        m = _NODE_ID_IN_FM.search(path.read_text("utf-8")[:400])
    except Exception:
        return None
    return m.group(1) if m else None
