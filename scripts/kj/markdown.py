"""节点 Markdown 页：程序生成、可重建的人可读视图。**不是数据来源**（账本在 SQLite）。

布局（文档 §4 用户修正 + 2026-09-06 晚反馈）：
- frontmatter：参与计算的值与状态，外加 Obsidian 原生 ``aliases`` / ``tags``、公共编号、前置/后续/相关的 [[链接]]、来源清单，
  让属性面板一眼看到掌握度、关系、来源、Wikidata 来源。
- 正文：定义原文（多行保留、引用原文、出处）、关系栏目（[[链接]]）、记录（**多行排版保留**，不再压成一行）、卡片。

文件名 = ``<安全名称>·<短id>.md``：改名会重命名文件（旧文件删除、链接由程序重新生成）。
每页都有 ``obsidian://open`` 深链，供 AI 推送 / 卡片来源栏打开。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import urllib.parse
from pathlib import Path

from . import ids
from .store import Ledger
from . import wikidata as WD

INDEX_NAME = "节点索引.md"
NODES_DIR_NAME = "节点"
_FM_MARK = "kj_id:"
_RECORD_TEXT_LIMIT = 4000


def _ts(t: int | None) -> str:
    return _dt.datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d %H:%M") if t else ""


def _day(t: int | None) -> str:
    return _dt.datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d") if t else "时间未知"


def filename_for(node_id: str, name: str) -> str:
    return f"{ids.safe_filename(name)}·{ids.short(node_id)}.md"


def vault_relative_dir() -> str:
    """节点页在 vault 里的相对目录（默认 KJ/节点）。"""
    return os.environ.get("KJ_VAULT_SUBDIR", "KJ") + "/" + NODES_DIR_NAME


def obsidian_url(node_id: str, name: str) -> str:
    vault = os.environ.get("OBSIDIAN_VAULT_NAME", "Obsidian Vault")
    file_rel = vault_relative_dir() + "/" + filename_for(node_id, name)[:-3]
    return "obsidian://open?vault=" + urllib.parse.quote(vault, safe="") + "&file=" + urllib.parse.quote(file_rel, safe="/")


def link_for(ledger: Ledger, node_id: str) -> str:
    n = ledger.node(node_id)
    if n is None:
        return node_id
    return f"[[{filename_for(n['id'], n['name'])[:-3]}|{n['name']}]]"


def _wikilink_plain(ledger: Ledger, node_id: str) -> str:
    n = ledger.node(node_id)
    return f"[[{filename_for(n['id'], n['name'])[:-3]}]]" if n else node_id


def _y(s) -> str:
    """YAML 双引号标量。"""
    s = "" if s is None else str(s)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _ylist(items) -> str:
    items = [x for x in items if x]
    return "[" + ", ".join(_y(x) for x in items) + "]" if items else "[]"


def source_label(src: dict | None) -> str:
    """来源一行字：pdf：书名 · 第N页 · 节；web：url。"""
    if not src:
        return ""
    parts = []
    if src.get("book") or src.get("title"):
        parts.append(str(src.get("book") or src.get("title")))
    if src.get("printed_page") is not None:
        parts.append(f"第 {src['printed_page']} 页")
    elif src.get("page") is not None:
        parts.append(f"p.{src['page']}")
    if src.get("section"):
        parts.append(str(src["section"]))
    if src.get("url"):
        parts.append(str(src["url"]))
    if src.get("ref") and not parts:
        parts.append(str(src["ref"]))
    if src.get("qid"):
        parts.append(f"Wikidata {src['qid']}")
    kind = src.get("kind", "")
    head = kind if kind and kind not in ("other", "manual") else ""
    body = " · ".join(parts)
    return f"{head}：{body}" if head and body else (head or body)


def _source_block(src: dict | None, indent: str = "  ") -> list[str]:
    """来源的多行展示：出处行 + 原文引用（blockquote）。"""
    if not src:
        return []
    out = []
    label = source_label(src)
    if label:
        out.append(f"{indent}来源：{label}")
    if src.get("file"):
        out.append(f"{indent}文件：`{src['file']}`" + (f"（页序 {src['page']}）" if src.get("page") is not None else ""))
    quote = str(src.get("quote") or "").strip()
    if quote:
        for ln in quote.splitlines():
            out.append(f"{indent}> {ln}")
    return out


def _multiline_item(text: str, first_prefix: str, indent: str = "  ", limit: int = _RECORD_TEXT_LIMIT) -> list[str]:
    """把多行正文放进一个列表项：首行跟着 ``- ``，后续行缩进两格（Markdown 列表续行），排版保留。"""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + "…（超长，完整内容在账本）"
    lines = text.splitlines() or [""]
    out = [first_prefix + lines[0]]
    for ln in lines[1:]:
        out.append(indent + ln if ln.strip() else "")
    return out


def render_node(ledger: Ledger, node_id: str, *, records_limit: int = 30) -> str:
    n = ledger.node(node_id)
    if n is None:
        raise KeyError(node_id)
    m = ledger.mastery_row(node_id) or {}
    detail = m.get("detail") or {}
    rels = ledger.relations(node_id)
    pre = [r for r in rels if r["type"] == "prereq" and r["to_id"] == node_id]
    suc = [r for r in rels if r["type"] == "prereq" and r["from_id"] == node_id]
    oth = [r for r in rels if r["type"] != "prereq"]
    defs = ledger.definitions(node_id)
    recs = ledger.records(node_id, limit=records_limit)
    rec_total = int(ledger.db.execute("SELECT COUNT(*) FROM records WHERE node_id=? AND merged_into IS NULL", (node_id,)).fetchone()[0])
    cards = ledger.cards_of(node_id)
    aliases = [a["alias"] for a in ledger.aliases(node_id)]
    weak = detail.get("prereqs", {}).get("weak", [])
    unknown = detail.get("prereqs", {}).get("unknown", [])
    sources = []
    for d in defs:
        lab = source_label(d["source"])
        if lab and lab not in sources:
            sources.append(lab)
    for r in recs:
        lab = source_label(r["source"])
        if lab and lab not in sources:
            sources.append(lab)
    public = WD.entity(ledger, n["qid"]) if n["qid"] else None

    fm = ["---",
          f"kj_id: {n['id']}",
          f"aliases: {_ylist(aliases)}",
          f"tags: {_ylist(['kj', 'kj/' + n['kind']])}",
          f"kind: {n['kind']}",
          f"wikidata: {n['qid'] or ''}",
          f"wikidata_url: {('https://www.wikidata.org/wiki/' + n['qid']) if n['qid'] else ''}",
          f"wikidata_label: {_y(public['label']) if public else _y('')}",
          f"mastery: {'null' if m.get('value') is None else m['value']}",
          f"mastery_level: {m.get('level', 0)}",
          f"progress: {m.get('progress', 'unseen')}",
          f"availability: {m.get('availability', 'open')}",
          f"readiness: {m.get('readiness', 'no_prereq_info')}",
          f"state: {m.get('state', 'unlockable')}",
          f"evidence_count: {m.get('evidence_count', 0)}",
          f"prereqs: {_ylist(_wikilink_plain(ledger, r['from_id']) for r in pre)}",
          f"successors: {_ylist(_wikilink_plain(ledger, r['to_id']) for r in suc)}",
          f"related: {_ylist(_wikilink_plain(ledger, r['to_id'] if r['from_id'] == node_id else r['from_id']) for r in oth)}",
          f"weak_prereqs: {_ylist(_wikilink_plain(ledger, w) for w in weak)}",
          f"unknown_prereqs: {_ylist(_wikilink_plain(ledger, u) for u in unknown)}",
          f"sources: {_ylist(sources[:20])}",
          f"definitions: {len(defs)}",
          f"records: {rec_total}",
          f"cards: {len(cards)}",
          f"obsidian_url: {_y(obsidian_url(n['id'], n['name']))}",
          f"updated: {_ts(m.get('updated_at') or n['updated_at'])}",
          "---"]
    out = list(fm)
    out.append(f"# {n['name']}")
    if aliases:
        out.append("别名：" + " / ".join(aliases))
    if n["summary"]:
        out += ["", n["summary"]]
    if n["qid"]:
        label = f"{public['label']} — {public['description']}" if public else "（公共目录未载入）"
        out.append(f"公共编号：[{n['qid']}](https://www.wikidata.org/wiki/{n['qid']}) {label}")

    out += ["", "## 定义"]
    if defs:
        for d in defs:
            out += _multiline_item(d["text"], "- ")
            out += _source_block(d["source"])
            if d["context_key"]:
                out.append(f"  语境：`{d['context_key']}`")
    else:
        out.append("- （尚无定义）")

    out += ["", "## 关系"]
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

    out += ["", f"## 记录（{rec_total} 条；程序生成的摘要，账本在数据库）"]
    if recs:
        for r in recs:
            when = _day(r["occurred_at"]) if r["occurred_at"] else f"登记 {_day(r['registered_at'])}"
            times = f" ×{r['occurrences']}" if (r["occurrences"] or 1) > 1 else ""
            out += _multiline_item(r["text"], f"- **{when}** [{r['kind']}]{times} ")
            out += _source_block(r["source"])
    else:
        out.append("- （尚无记录）")

    out += ["", "## 卡片"]
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
        self.nodes_dir = self.root / NODES_DIR_NAME

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
