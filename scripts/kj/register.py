"""格式化登记工具：按类型校验 → 追加事件。AI 负责理解与匹配，这里负责编号、校验、保存。

每个函数只做"校验 + 追加事件"，不重算、不渲染（由 service 层统一收尾），便于测试与重放。
校验失败抛 RegisterError(code, message, **extra)，service/HTTP 层原样转成 {ok:false, code, error, ...}。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
from typing import Any

from . import ids
from .compute import RESULT_KINDS, prereq_cycle_path
from .store import Event, Ledger
from . import wikidata as WD

NODE_KINDS = ("concept", "person", "method", "object", "event", "problem", "analysis", "other")
RECORD_KINDS = ("reading", "note", "handwriting", "conversation", "observation", "analysis", "anki", "other")
RELATION_TYPES = ("prereq", "part_of", "subclass_of", "instance_of", "related", "causes", "solves", "explains",
                  "uses", "example", "contrast", "influenced_by", "facet_of", "different_from", "studied_by",
                  "studies", "practiced_by", "custom")
SOURCE_KINDS = ("pdf", "epub", "web", "conversation", "audio", "image", "handwriting", "manual", "anki", "wikidata", "other")


class RegisterError(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        d = {"ok": False, "code": self.code, "error": self.message}
        d.update(self.extra)
        return d


# ── 通用校验 ────────────────────────────────────────────────────────────────
def _text(v: Any, field: str, *, required: bool = True, limit: int = 20000) -> str:
    s = "" if v is None else str(v).strip()
    if required and not s:
        raise RegisterError("missing_field", f"缺少 {field}")
    if len(s) > limit:
        raise RegisterError("too_long", f"{field} 超过 {limit} 字")
    return s


def parse_time(v: Any) -> int | None:
    """发生时间：epoch 秒 / 毫秒 / ISO 字符串；空 → None（不伪造）。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n // 1000 if n > 10_000_000_000 else n
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return parse_time(int(s))
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.astimezone()
        return int(d.timestamp())
    except Exception:
        raise RegisterError("bad_time", f"看不懂的时间：{s}（用 ISO 8601 或 epoch 秒）")


def normalize_source(src: Any, *, required: bool = False) -> dict | None:
    if src is None or src == "" or src == {}:
        if required:
            raise RegisterError("missing_source", "必须给出处 source（{kind, book/url/..., page, quote}）")
        return None
    if isinstance(src, str):
        src = {"kind": "manual", "ref": src}
    if not isinstance(src, dict):
        raise RegisterError("bad_source", "source 要是对象")
    kind = str(src.get("kind") or "other").strip().lower()
    if kind not in SOURCE_KINDS:
        kind = "other"
    out = {k: v for k, v in src.items() if v not in (None, "", [], {})}
    out["kind"] = kind
    return out


def _node(ledger: Ledger, node_id: str, field: str = "node_id") -> str:
    node_id = (node_id or "").strip()
    if not node_id:
        raise RegisterError("missing_field", f"缺少 {field}")
    row = ledger.resolve(node_id)
    if row is None:
        raise RegisterError("node_not_found", f"节点不存在：{node_id}", node_id=node_id)
    return row["id"]


def _aliases(v: Any) -> list[dict]:
    out: list[dict] = []
    for a in v or []:
        if isinstance(a, dict):
            alias, lang = str(a.get("alias") or "").strip(), str(a.get("lang") or "").strip()
        else:
            alias, lang = str(a).strip(), ""
        if alias and alias not in [x["alias"] for x in out]:
            out.append({"alias": alias, "lang": lang})
    return out


# ── 节点 ────────────────────────────────────────────────────────────────────
def create_node(ledger: Ledger, *, name: str, kind: str = "concept", aliases: Any = None, qid: str | None = None,
                summary: str = "", actor: str = "", source: dict | None = None) -> Event:
    name = _text(name, "name", limit=200)
    kind = (kind or "concept").strip().lower()
    if kind not in NODE_KINDS:
        kind = "other"
    qid = (qid or "").strip() or None
    if qid:
        if not ids.is_qid(qid):
            raise RegisterError("bad_qid", f"编号格式不对：{qid}")
        existing = ledger.find_by_qid(qid)
        if existing is not None:
            raise RegisterError("qid_taken", f"编号 {qid} 已绑定到节点 {existing['id']}（{existing['name']}），请关联而非新建",
                                node_id=existing["id"], name=existing["name"])
    dup = ledger.db.execute("SELECT id, name FROM nodes WHERE status='active' AND lower(name)=lower(?)", (name,)).fetchone()
    node_id = ids.mint("kj")
    payload = {"id": node_id, "name": name, "kind": kind, "aliases": _aliases(aliases), "qid": qid, "summary": _text(summary, "summary", required=False, limit=2000)}
    if dup is not None:
        payload["possible_duplicate_of"] = dup["id"]
    return ledger.append("node.create", payload, node_ids=[node_id], actor=actor, source=normalize_source(source))


def update_node(ledger: Ledger, node_id: str, *, name: str | None = None, kind: str | None = None,
                aliases: Any = None, summary: str | None = None, actor: str = "") -> Event:
    node_id = _node(ledger, node_id)
    payload: dict[str, Any] = {"id": node_id}
    if name is not None:
        payload["name"] = _text(name, "name", limit=200)
    if kind is not None:
        payload["kind"] = kind if kind in NODE_KINDS else "other"
    if aliases is not None:
        payload["aliases"] = _aliases(aliases)
    if summary is not None:
        payload["summary"] = _text(summary, "summary", required=False, limit=2000)
    if len(payload) == 1:
        raise RegisterError("nothing_to_update", "没有要改的字段")
    return ledger.append("node.update", payload, node_ids=[node_id], actor=actor)


def merge_node(ledger: Ledger, node_id: str, into: str, *, reason: str = "", actor: str = "") -> Event:
    a, b = _node(ledger, node_id), _node(ledger, into, "into")
    if a == b:
        raise RegisterError("same_node", "不能把节点合并进自己")
    return ledger.append("node.merge", {"id": a, "into": b, "reason": reason}, node_ids=[a, b], actor=actor)


def bind_qid(ledger: Ledger, node_id: str, qid: str, *, actor: str = "") -> tuple[Event, list[Event], list[Event]]:
    """绑定/换绑公共编号。返回 (绑定事件, 撤回的旧自动关系事件, 新生成的自动关系事件)。
    新编号已被别的节点占用 → qid_taken（返回两者，由 AI 判断合并）。"""
    node_id = _node(ledger, node_id)
    qid = (qid or "").strip()
    if not ids.is_qid(qid):
        raise RegisterError("bad_qid", f"编号格式不对：{qid}")
    other = ledger.find_by_qid(qid)
    if other is not None and other["id"] != node_id:
        raise RegisterError("qid_taken", f"编号 {qid} 已绑定到 {other['id']}（{other['name']}）", node_id=other["id"], name=other["name"],
                            this_node=node_id)
    node = ledger.node(node_id)
    prev = node["qid"]
    retracted: list[Event] = []
    if prev and prev != qid:
        for rel in ledger.db.execute("SELECT id FROM relations WHERE origin='wikidata' AND derived_qid=? AND status='active' AND (from_id=? OR to_id=?)",
                                     (prev, node_id, node_id)).fetchall():
            retracted.append(ledger.append("relation.retract", {"id": rel[0], "reason": f"换绑 {prev}→{qid}"}, node_ids=[node_id], actor=actor))
    ev = ledger.append("node.bind_qid", {"id": node_id, "qid": qid, "prev": prev}, node_ids=[node_id], actor=actor)
    added = generate_auto_relations(ledger, node_id, qid, actor=actor)
    return ev, retracted, added


def unbind_qid(ledger: Ledger, node_id: str, *, actor: str = "") -> Event:
    node_id = _node(ledger, node_id)
    node = ledger.node(node_id)
    prev = node["qid"]
    if prev:
        for rel in ledger.db.execute("SELECT id FROM relations WHERE origin='wikidata' AND derived_qid=? AND status='active' AND (from_id=? OR to_id=?)",
                                     (prev, node_id, node_id)).fetchall():
            ledger.append("relation.retract", {"id": rel[0], "reason": f"解绑 {prev}"}, node_ids=[node_id], actor=actor)
    return ledger.append("node.unbind_qid", {"id": node_id, "prev": prev}, node_ids=[node_id], actor=actor)


def sync_public_aliases(ledger: Ledger, node_id: str, qid: str, *, actor: str = "", limit: int = 16) -> Event | None:
    """把公共实体的三语名称与别名回填成节点别名（origin=wikidata）—— 第二本书用英文名/日文名来搜，本地才搜得到。
    跳过节点本名、过长（>60）、重复（不分大小写）的；最多 limit 个，标签优先、别名按 zh/en/ja 补。
    目录里没有这个实体（未 fetch）→ 不发事件，返回 None。"""
    e = WD.entity(ledger, qid)
    if e is None:
        return None
    node = ledger.node(node_id)
    seen = {((node["name"] if node else "") or "").strip().lower()}
    picked: list[dict] = []

    def take(text: Any, lang: str) -> None:
        t = str(text or "").strip()
        if not t or len(t) > 60 or t.lower() in seen or len(picked) >= limit:
            return
        seen.add(t.lower())
        picked.append({"alias": t, "lang": lang})

    for lang in ("zh", "en", "ja"):
        take(e.get(f"label_{lang}"), lang)
    for lang in ("zh", "en", "ja"):
        for a in (e.get("aliases") or {}).get(lang) or []:
            take(a, lang)
    return ledger.append("node.aliases_sync", {"id": node_id, "qid": qid, "origin": "wikidata", "aliases": picked},
                         node_ids=[node_id], actor=actor or "wikidata")


def generate_auto_relations(ledger: Ledger, node_id: str, qid: str, *, actor: str = "") -> list[Event]:
    """两端都已绑定编号的公共关系 → 本地关系（幂等：同 from/to/type/derived 只生成一次）。"""
    out: list[Event] = []
    for rel in WD.auto_relations_for(ledger, node_id, qid):
        exists = ledger.db.execute(
            "SELECT 1 FROM relations WHERE from_id=? AND to_id=? AND type=? AND origin='wikidata' AND status='active'",
            (rel["from"], rel["to"], rel["type"])).fetchone()
        if exists:
            continue
        rid = ids.mint("rel")
        payload = dict(rel, id=rid, source={"kind": "wikidata", "qid": rel["derived_qid"]})
        out.append(ledger.append("relation.add", payload, node_ids=[rel["from"], rel["to"]], actor=actor or "wikidata",
                                 dedupe_key=f"wdrel:{rel['from']}:{rel['to']}:{rel['type']}:{rel['derived_qid']}"))
    return out


# ── 定义 / 记录 ─────────────────────────────────────────────────────────────
def add_definition(ledger: Ledger, node_id: str, *, text: str, source: Any, context_key: str = "",
                   decision: str = "", supersedes: str = "", actor: str = "") -> Event:
    """已有同语境定义时先返回旧正文（definition_exists），AI 比较后带 decision 再来：
    keep → 不改（返回 no_change）；supersede → 覆盖（要给 supersedes=旧 id）；add → 并存（不同语境）。"""
    node_id = _node(ledger, node_id)
    text = _text(text, "text")
    src = normalize_source(source, required=True)
    context_key = (context_key or "").strip() or _context_from_source(src)
    existing = [d for d in ledger.definitions(node_id) if (d["context_key"] or "") == context_key]
    if existing and not decision:
        raise RegisterError("definition_exists", "该语境已有定义，先比较：基本相同 → decision=keep；新的明显更完善 → decision=supersede + supersedes=<旧 id>；不同语境 → 换 context_key 或 decision=add",
                            node_id=node_id, context_key=context_key,
                            existing=[{"id": d["id"], "text": d["text"], "source": d["source"], "created_at": d["created_at"]} for d in existing])
    if decision == "keep":
        raise RegisterError("no_change", "保持原定义不变", node_id=node_id, existing=[d["id"] for d in existing])
    if decision == "supersede":
        if not supersedes or not any(d["id"] == supersedes for d in existing):
            raise RegisterError("bad_supersedes", "supersede 必须指明要覆盖的旧定义 id", existing=[d["id"] for d in existing])
    did = ids.mint("def")
    payload = {"id": did, "node_id": node_id, "text": text, "context_key": context_key, "source": src}
    if decision == "supersede":
        payload["supersedes"] = supersedes
    return ledger.append("definition.add", payload, node_ids=[node_id], actor=actor, source=src)


def _context_from_source(src: dict | None) -> str:
    if not src:
        return ""
    for k in ("book", "sha", "url", "ref", "title"):
        if src.get(k):
            return f"{src['kind']}:{src[k]}"
    return src.get("kind", "")


def add_record(ledger: Ledger, node_id: str, *, text: str, kind: str = "note", source: Any = None,
               occurred_at: Any = None, actor: str = "", dedupe_key: str | None = None) -> Event:
    """每次单独追加一条，不查重（归并由 AI 读取时做，见 merge_records）。"""
    node_id = _node(ledger, node_id)
    text = _text(text, "text")
    kind = (kind or "note").strip().lower()
    if kind not in RECORD_KINDS:
        kind = "other"
    src = normalize_source(source)
    rid = ids.mint("rec")
    return ledger.append("record.add", {"id": rid, "node_id": node_id, "kind": kind, "text": text, "source": src},
                         node_ids=[node_id], occurred_at=parse_time(occurred_at), actor=actor, source=src, dedupe_key=dedupe_key)


def merge_records(ledger: Ledger, node_id: str, *, record_ids: list[str], text: str, occurrences: int | None = None,
                  kind: str = "merged", earliest: Any = None, actor: str = "") -> Event:
    """AI 完整读取后归并重复表述：新记录 = 整理结果（保留真实次数/时间/来源），原记录标 merged_into 不删。"""
    node_id = _node(ledger, node_id)
    text = _text(text, "text")
    rids = [r for r in dict.fromkeys(record_ids or []) if r]
    if len(rids) < 2:
        raise RegisterError("too_few", "归并至少要两条记录")
    rows = ledger.db.execute(f"SELECT id, source_json FROM records WHERE node_id=? AND merged_into IS NULL AND id IN ({','.join('?' * len(rids))})",
                             [node_id, *rids]).fetchall()
    found = {r["id"] for r in rows}
    missing = [r for r in rids if r not in found]
    if missing:
        raise RegisterError("record_not_found", f"这些记录不存在或已归并：{missing}", missing=missing)
    sources = []
    for r in rows:
        s = r["source_json"]
        if s and s not in sources:
            sources.append(s)
    src = {"kind": "other", "merged_from": rids, "sources": [json.loads(s) for s in sources]}
    rid = ids.mint("rec")
    return ledger.append("record.merge", {"id": rid, "node_id": node_id, "kind": kind, "text": text, "record_ids": rids,
                                          "occurrences": int(occurrences or len(rids)), "source": src},
                         node_ids=[node_id], occurred_at=parse_time(earliest), actor=actor)


# ── 关系 ────────────────────────────────────────────────────────────────────
def add_relation(ledger: Ledger, *, from_id: str, to_id: str, type: str, evidence: str = "", source: Any = None,
                 actor: str = "", origin: str = "manual") -> Event:
    """from →(type)→ to。prereq 语义：from 是 to 的前置。成环 → prereq_cycle（返回路径）。"""
    a, b = _node(ledger, from_id, "from"), _node(ledger, to_id, "to")
    if a == b:
        raise RegisterError("self_relation", "关系两端不能是同一节点")
    rtype = (type or "").strip().lower()
    if rtype not in RELATION_TYPES:
        raise RegisterError("bad_relation_type", f"未知关系类型 {type}；可用：{', '.join(RELATION_TYPES)}")
    if rtype == "prereq":
        if not (evidence or "").strip():
            raise RegisterError("missing_evidence", "教学前置必须带原文依据 evidence（哪句话说明学 to 要先会 from）")
        path = prereq_cycle_path(ledger, a, b)
        if path:
            raise RegisterError("prereq_cycle", "加入后前置关系成环，已拒绝", path=path)
    dup = ledger.db.execute("SELECT id FROM relations WHERE from_id=? AND to_id=? AND type=? AND status='active'", (a, b, rtype)).fetchone()
    if dup:
        raise RegisterError("relation_exists", "同方向同类型关系已存在（要换依据请先撤回旧的）", relation_id=dup["id"])
    src = normalize_source(source)
    rid = ids.mint("rel")
    return ledger.append("relation.add", {"id": rid, "from": a, "to": b, "type": rtype, "evidence": (evidence or "").strip(),
                                          "source": src, "origin": origin}, node_ids=[a, b], actor=actor, source=src)


def retract_relation(ledger: Ledger, relation_id: str, *, reason: str = "", actor: str = "") -> Event:
    row = ledger.db.execute("SELECT * FROM relations WHERE id=?", (relation_id,)).fetchone()
    if row is None:
        raise RegisterError("relation_not_found", f"关系不存在：{relation_id}")
    if row["status"] != "active":
        raise RegisterError("already_retracted", "该关系早已撤回")
    return ledger.append("relation.retract", {"id": relation_id, "reason": reason or ""}, node_ids=[row["from_id"], row["to_id"]], actor=actor)


def change_relation(ledger: Ledger, relation_id: str, *, type: str | None = None, reverse: bool = False,
                    evidence: str = "", source: Any = None, reason: str = "", actor: str = "") -> tuple[Event, Event]:
    """改类型/改方向 = 撤回旧的 + 追加新的（历史可追溯）。"""
    row = ledger.db.execute("SELECT * FROM relations WHERE id=? AND status='active'", (relation_id,)).fetchone()
    if row is None:
        raise RegisterError("relation_not_found", f"活跃关系不存在：{relation_id}")
    frm, to = (row["to_id"], row["from_id"]) if reverse else (row["from_id"], row["to_id"])
    new_type = (type or row["type"]).strip().lower()
    if new_type not in RELATION_TYPES:
        raise RegisterError("bad_relation_type", f"未知关系类型 {type}")
    if new_type == "prereq":
        path = prereq_cycle_path(ledger, frm, to, exclude_relation=relation_id)  # 旧边即将撤回，不算进环
        if path:
            raise RegisterError("prereq_cycle", "改动后前置关系成环，已拒绝", path=path)
        if not (evidence or row["evidence"] or "").strip():
            raise RegisterError("missing_evidence", "教学前置必须带原文依据 evidence")
    old = retract_relation(ledger, relation_id, reason=reason or "改类型/方向", actor=actor)
    new = add_relation(ledger, from_id=frm, to_id=to, type=new_type, evidence=evidence or row["evidence"] or "",
                       source=source if source is not None else json.loads(row["source_json"] or "null"), actor=actor)
    return old, new


# ── 卡片 ────────────────────────────────────────────────────────────────────
def bind_card(ledger: Ledger, *, node_ids: list[str], anki_note_id: int | None = None, anki_card_ids: list[int] | None = None,
              front: str = "", back: str = "", deck: str = "", card_key: str | None = None, actor: str = "") -> Event:
    nids = [_node(ledger, n, "node_ids[]") for n in (node_ids or [])]
    nids = list(dict.fromkeys(nids))
    if not nids:
        raise RegisterError("missing_node", "制卡必须关联至少一个有效节点（没有就先建节点）")
    key = (card_key or "").strip() or (f"anki:{int(anki_note_id)}" if anki_note_id else ids.mint("card"))
    return ledger.append("card.bind", {"card_key": key, "node_ids": nids, "anki_note_id": anki_note_id,
                                       "anki_card_ids": [int(c) for c in (anki_card_ids or [])], "front": front or "",
                                       "back": back or "", "deck": deck or ""}, node_ids=nids, actor=actor)


def unbind_card(ledger: Ledger, card_key: str, *, actor: str = "") -> Event:
    row = ledger.db.execute("SELECT card_key FROM cards WHERE card_key=? AND status='active'", (card_key,)).fetchone()
    if row is None:
        raise RegisterError("card_not_found", f"卡不存在：{card_key}")
    nids = [r[0] for r in ledger.db.execute("SELECT node_id FROM card_nodes WHERE card_key=?", (card_key,))]
    return ledger.append("card.unbind", {"card_key": card_key}, node_ids=nids, actor=actor)


def anki_snapshot(ledger: Ledger, *, card_id: int, mastery: float | None, node_ids: list[str], card_key: str | None = None,
                  ts: int | None = None, actor: str = "anki") -> Event:
    ts = int(ts or time.time())
    day = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return ledger.append("anki.snapshot", {"card_id": int(card_id), "card_key": card_key, "mastery": mastery, "ts": ts},
                         node_ids=node_ids, occurred_at=ts, actor=actor, dedupe_key=f"anki:{int(card_id)}:{day}")


# ── 出题 / 判分 / 自评 ──────────────────────────────────────────────────────
def register_quiz(ledger: Ledger, *, items: list[dict], target_node: str | None = None, title: str = "",
                  source: Any = None, actor: str = "") -> Event:
    """出题时就绑定每题检验的节点；答案只在服务端投影里。"""
    if not items:
        raise RegisterError("missing_items", "至少一道题")
    target = _node(ledger, target_node, "target_node") if target_node else None
    norm = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise RegisterError("bad_item", f"第 {i} 题要是对象")
        nids = list(dict.fromkeys(_node(ledger, n, f"items[{i}].node_ids") for n in (it.get("node_ids") or ([it["node_id"]] if it.get("node_id") else []))))
        if not nids:
            raise RegisterError("item_unbound", f"第 {i} 题没有绑定节点（node_ids）")
        norm.append({"item_id": str(it.get("item_id") or f"q{i}"), "question": _text(it.get("question"), f"items[{i}].question", limit=4000),
                     "answer": _text(it.get("answer"), "answer", required=False, limit=4000), "kind": str(it.get("kind") or "choice"), "node_ids": nids})
    if len({x["item_id"] for x in norm}) != len(norm):
        raise RegisterError("dup_item_id", "item_id 重复")
    qid = ids.mint("quiz")
    all_nodes = list(dict.fromkeys([n for x in norm for n in x["node_ids"]] + ([target] if target else [])))
    src = normalize_source(source)
    return ledger.append("quiz.register", {"id": qid, "title": title or "", "target_node": target, "items": norm, "source": src},
                         node_ids=all_nodes, actor=actor, source=src)


def submit_results(ledger: Ledger, *, quiz_id: str, results: list[dict], actor: str = "",
                   occurred_at: Any = None) -> list[Event]:
    """逐题回传。同一题再次提交 = 更正（重算时以最终结果为准）。"""
    quiz = ledger.quiz(quiz_id)
    if quiz is None:
        raise RegisterError("quiz_not_found", f"测验不存在：{quiz_id}")
    by_id = {it["item_id"]: it for it in quiz["items"]}
    if not results:
        raise RegisterError("missing_results", "没有判分结果")
    out: list[Event] = []
    when = parse_time(occurred_at)
    for r in results:
        iid = str(r.get("item_id") or "")
        if iid not in by_id:
            raise RegisterError("item_not_found", f"题 {iid} 不在测验里", known=list(by_id))
        res = str(r.get("result") or "").strip().lower()
        if res == "incorrect":
            res = "wrong"
        if res not in RESULT_KINDS:
            raise RegisterError("bad_result", f"result 只能是 {RESULT_KINDS}", item_id=iid)
        prev = by_id[iid].get("result")
        out.append(ledger.append("quiz.result", {"quiz_id": quiz_id, "item_id": iid, "result": res, "note": str(r.get("note") or ""),
                                                 "correction_of": prev if prev and prev != res else None},
                                 node_ids=by_id[iid]["node_ids"], occurred_at=when, actor=actor))
    return out


def self_assess(ledger: Ledger, node_id: str, *, value: float, reason: str = "", actor: str = "") -> Event:
    """自评：普通记录，登记时刻把掌握度设为该值一次；之后证据照常推动。"""
    node_id = _node(ledger, node_id)
    try:
        v = float(value)
    except Exception:
        raise RegisterError("bad_value", "自评值要是 0~1 的数")
    if not 0.0 <= v <= 1.0:
        raise RegisterError("bad_value", "自评值要在 0~1 之间")
    return ledger.append("self_assess", {"node_id": node_id, "value": v, "reason": reason or ""}, node_ids=[node_id], actor=actor,
                         occurred_at=int(time.time()))
