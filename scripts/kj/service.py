"""统一门面：CLI / Flask / 助手工具都从这里进。每次登记 = 校验 → 追加事件 → 重算 → 重渲染 Markdown。

返回值一律 dict，``ok`` 为真假；错误带 ``code``（RegisterError.to_dict）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import anki_sync as AK
from . import compute
from . import query as Q
from . import register as R
from . import wikidata as WD
from .markdown import VaultWriter
from .register import RegisterError
from .store import Ledger


def project_root() -> Path:
    """$CLAUDE_PROJECT 优先；没设就用本包所在的 checkout（scripts/kj/ 往上两级），不猜 C:\\claude。"""
    env = os.environ.get("CLAUDE_PROJECT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def default_db_path() -> Path:
    return project_root() / "state" / "kj" / "kj.db"


def default_vault_dir() -> Path:
    return Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian")) / "KJ"


class KJService:
    def __init__(self, db_path: str | Path | None = None, vault_dir: str | Path | None = None, *, render: bool = True,
                 actor: str = ""):
        self.ledger = Ledger(db_path or default_db_path())
        self.writer = VaultWriter(vault_dir or default_vault_dir()) if render else None
        self.actor = actor

    def close(self) -> None:
        self.ledger.close()

    # ── 收尾：重算 + 渲染 ─────────────────────────────────────────────────
    def _finish(self, node_ids: list[str], base: dict | None = None) -> dict:
        rows = compute.recompute(self.ledger, node_ids)
        paths = []
        if self.writer is not None:
            # 受影响节点 + 它们的直接邻居都重渲染：改名/改关系后，邻居页里的 [[链接]] 文件名也得跟上
            to_render = set(rows)
            for nid in list(rows):
                for r in self.ledger.relations(nid):
                    to_render.add(r["from_id"])
                    to_render.add(r["to_id"])
            for nid in to_render:
                p = self.writer.write_node(self.ledger, nid)
                if p:
                    paths.append(str(p))
            self.writer.write_index(self.ledger)
        out = dict(base or {})
        out.setdefault("ok", True)
        out["computed"] = {nid: self._brief_from_row(nid, row) for nid, row in rows.items()}
        if paths:
            out["markdown"] = paths
        return out

    def _brief_from_row(self, nid: str, row: dict) -> dict:
        n = self.ledger.node(nid)
        return {"id": nid, "name": n["name"] if n else nid, "mastery": row.get("value"), "level": row.get("level"),
                "progress": row.get("progress"), "availability": row.get("availability"), "readiness": row.get("readiness"),
                "state": row.get("state")}

    def _run(self, fn, *args, **kw) -> dict:
        try:
            return fn(*args, **kw)
        except RegisterError as e:
            return e.to_dict()

    # ── 节点 ──────────────────────────────────────────────────────────────
    def create_node(self, *, name: str, kind: str = "concept", aliases: Any = None, qid: str | None = None,
                    summary: str = "", source: Any = None, fetch_public: bool = False) -> dict:
        def go():
            ev = R.create_node(self.ledger, name=name, kind=kind, aliases=aliases, qid=qid, summary=summary, actor=self.actor, source=source)
            nid = ev.payload["id"]
            extra: dict[str, Any] = {"node_id": nid}
            if ev.payload.get("possible_duplicate_of"):
                dup = self.ledger.node(ev.payload["possible_duplicate_of"])
                extra["possible_duplicate_of"] = {"id": dup["id"], "name": dup["name"]} if dup else ev.payload["possible_duplicate_of"]
                extra["hint"] = "已有同名节点；若是同一概念请 merge_node，不同含义请补别名/说明区分"
            if qid:
                if fetch_public and WD.entity(self.ledger, qid) is None:
                    try:
                        WD.fetch_entity(self.ledger, qid)
                    except Exception as e:  # 在线失败不阻塞登记
                        extra["public_fetch_error"] = str(e)[:120]
                synced = R.sync_public_aliases(self.ledger, nid, qid, actor=self.actor)
                extra["aliases_synced"] = len(synced.payload["aliases"]) if synced else 0
                added = R.generate_auto_relations(self.ledger, nid, qid, actor=self.actor)
                extra["auto_relations"] = len(added)
            return self._finish([nid], extra)
        return self._run(go)

    def update_node(self, node_id: str, **kw) -> dict:
        def go():
            ev = R.update_node(self.ledger, node_id, actor=self.actor, **kw)
            return self._finish([ev.payload["id"]], {"node_id": ev.payload["id"]})
        return self._run(go)

    def merge_node(self, node_id: str, into: str, reason: str = "") -> dict:
        def go():
            ev = R.merge_node(self.ledger, node_id, into, reason=reason, actor=self.actor)
            if self.writer is not None:
                self.writer.write_node(self.ledger, ev.payload["id"])  # 删掉被并节点的页
            return self._finish([ev.payload["into"]], {"merged": ev.payload["id"], "into": ev.payload["into"]})
        return self._run(go)

    def bind_qid(self, node_id: str, qid: str, *, fetch_public: bool = True) -> dict:
        def go():
            if fetch_public and WD.entity(self.ledger, qid) is None:
                try:
                    WD.fetch_entity(self.ledger, qid)
                except Exception:
                    pass
            ev, retracted, added = R.bind_qid(self.ledger, node_id, qid, actor=self.actor)
            nid = ev.payload["id"]
            synced = R.sync_public_aliases(self.ledger, nid, qid, actor=self.actor)
            touched = {nid}
            for e in added:
                touched.update(e.node_ids)
            for e in retracted:
                touched.update(e.node_ids)
            e = WD.entity(self.ledger, qid)
            return self._finish(sorted(touched), {"node_id": nid, "qid": qid, "prev": ev.payload.get("prev"),
                                                  "retracted_auto_relations": len(retracted), "auto_relations": len(added),
                                                  "aliases_synced": len(synced.payload["aliases"]) if synced else 0,
                                                  "public": {"label": e["label"], "description": e["description"]} if e else None})
        return self._run(go)

    def unbind_qid(self, node_id: str) -> dict:
        def go():
            ev = R.unbind_qid(self.ledger, node_id, actor=self.actor)
            return self._finish([ev.payload["id"]], {"node_id": ev.payload["id"], "prev": ev.payload.get("prev")})
        return self._run(go)

    # ── 定义 / 记录 ───────────────────────────────────────────────────────
    def add_definition(self, node_id: str, *, text: str, source: Any, context_key: str = "", decision: str = "", supersedes: str = "",
                       uses: Any = None) -> dict:
        def go():
            ev = R.add_definition(self.ledger, node_id, text=text, source=source, context_key=context_key, decision=decision,
                                  supersedes=supersedes, actor=self.actor)
            nid = ev.payload["node_id"]
            extra: dict[str, Any] = {"definition_id": ev.payload["id"], "node_id": nid, "context_key": ev.payload["context_key"]}
            # 申报的依赖以定义原句为依据登前置；没申报也扫一遍定义里出现的节点名（also_mentioned）防漏
            rep = R.attach_definition_uses(self.ledger, nid, definition_text=ev.payload["text"], source=ev.payload.get("source"),
                                           uses=uses or [], actor=self.actor)
            extra.update(rep)
            touched = {nid} | {e["from"] for e in rep["prereqs_added"] + rep["relations_added"]}
            return self._finish(sorted(touched), extra)
        return self._run(go)

    def add_record(self, node_id: str, *, text: str, kind: str = "note", source: Any = None, occurred_at: Any = None,
                   dedupe_key: str | None = None) -> dict:
        def go():
            ev = R.add_record(self.ledger, node_id, text=text, kind=kind, source=source, occurred_at=occurred_at, actor=self.actor, dedupe_key=dedupe_key)
            return self._finish([ev.payload["node_id"]], {"record_id": ev.payload["id"], "node_id": ev.payload["node_id"], "duplicate": ev.duplicate})
        return self._run(go)

    def merge_records(self, node_id: str, *, record_ids: list[str], text: str, occurrences: int | None = None, earliest: Any = None) -> dict:
        def go():
            ev = R.merge_records(self.ledger, node_id, record_ids=record_ids, text=text, occurrences=occurrences, earliest=earliest, actor=self.actor)
            return self._finish([ev.payload["node_id"]], {"record_id": ev.payload["id"], "merged": ev.payload["record_ids"]})
        return self._run(go)

    # ── 关系 ──────────────────────────────────────────────────────────────
    def add_relation(self, *, from_id: str, to_id: str, type: str, evidence: str = "", source: Any = None,
                     allow_redundant: bool = False) -> dict:
        def go():
            ev = R.add_relation(self.ledger, from_id=from_id, to_id=to_id, type=type, evidence=evidence, source=source, actor=self.actor,
                                allow_redundant=allow_redundant)
            return self._finish([ev.payload["from"], ev.payload["to"]], {"relation_id": ev.payload["id"], "from": ev.payload["from"],
                                                                       "to": ev.payload["to"], "type": ev.payload["type"]})
        return self._run(go)

    def retract_relation(self, relation_id: str, reason: str = "") -> dict:
        def go():
            ev = R.retract_relation(self.ledger, relation_id, reason=reason, actor=self.actor)
            return self._finish(list(ev.node_ids), {"relation_id": relation_id, "retracted": True})
        return self._run(go)

    def change_relation(self, relation_id: str, *, type: str | None = None, reverse: bool = False, evidence: str = "",
                        source: Any = None, reason: str = "") -> dict:
        def go():
            old, new = R.change_relation(self.ledger, relation_id, type=type, reverse=reverse, evidence=evidence, source=source,
                                         reason=reason, actor=self.actor)
            return self._finish(list(dict.fromkeys(old.node_ids + new.node_ids)),
                                {"retracted": relation_id, "relation_id": new.payload["id"], "from": new.payload["from"],
                                 "to": new.payload["to"], "type": new.payload["type"]})
        return self._run(go)

    # ── 卡片 / Anki ───────────────────────────────────────────────────────
    def bind_card(self, *, node_ids: list[str], anki_note_id: int | None = None, anki_card_ids: list[int] | None = None,
                  front: str = "", back: str = "", deck: str = "", card_key: str | None = None) -> dict:
        def go():
            ev = R.bind_card(self.ledger, node_ids=node_ids, anki_note_id=anki_note_id, anki_card_ids=anki_card_ids, front=front,
                             back=back, deck=deck, card_key=card_key, actor=self.actor)
            return self._finish(list(ev.node_ids), {"card_key": ev.payload["card_key"], "node_ids": ev.payload["node_ids"]})
        return self._run(go)

    def make_card(self, *, node_ids: list[str], front: str, back: str, deck: str = AK.DEFAULT_DECK, tags: list[str] | None = None,
                  anki_url: str | None = None, request=None) -> dict:
        def go():
            res = AK.make_card(self.ledger, node_ids=node_ids, front=front, back=back, deck=deck, tags=tags, anki_url=anki_url,
                               request=request, actor=self.actor)
            return self._finish(list(res["node_ids"]), res)
        return self._run(go)

    def anki_sync(self, *, anki_url: str | None = None, request=None, fsrs=None, bindings_path=None) -> dict:
        """先吸收桥的绑定账本（不依赖 Anki 在线），再拉复习快照；Anki 不在线时前者仍生效、后者报 anki_unavailable。"""
        def go():
            ingest = AK.ingest_bridge_bindings(self.ledger, bindings_path, anki_url=anki_url, request=request)
            if ingest.get("nodes"):
                self._finish(list(ingest["nodes"]))
            try:
                res = AK.sync_snapshots(self.ledger, anki_url=anki_url, request=request, fsrs=fsrs, bindings_path=bindings_path)
            except Exception as e:  # AnkiConnect 不在线等：绑定已吸收，只是没拉到复习数据
                return {"ok": False, "code": "anki_unavailable", "error": str(e)[:200], "ingest": ingest}
            return self._finish(list(res.get("nodes") or []), res) if res.get("nodes") else dict(res, ok=True)
        return self._run(go)

    def ingest_bindings(self, path=None, *, anki_url: str | None = None, request=None, add_provenance: bool = True) -> dict:
        """只吸收桥的卡↔节点绑定账本（不拉复习快照）。"""
        def go():
            res = AK.ingest_bridge_bindings(self.ledger, path, anki_url=anki_url, request=request, add_provenance=add_provenance)
            return self._finish(list(res.get("nodes") or []), res) if res.get("nodes") else dict(res, ok=True)
        return self._run(go)

    # ── 出题 / 判分 / 自评 ────────────────────────────────────────────────
    def register_quiz(self, *, items: list[dict], target_node: str | None = None, title: str = "", source: Any = None) -> dict:
        def go():
            ev = R.register_quiz(self.ledger, items=items, target_node=target_node, title=title, source=source, actor=self.actor)
            return {"ok": True, "quiz_id": ev.payload["id"], "target_node": ev.payload["target_node"],
                    "items": [{"item_id": it["item_id"], "node_ids": it["node_ids"]} for it in ev.payload["items"]],
                    "next": "让用户作答后，按 item_id 逐题回传 result（correct/wrong/partial/unanswered/undetermined）"}
        return self._run(go)

    def submit_results(self, *, quiz_id: str, results: list[dict], occurred_at: Any = None) -> dict:
        def go():
            quiz = self.ledger.quiz(quiz_id)
            if quiz is None:
                raise RegisterError("quiz_not_found", f"测验不存在：{quiz_id}")
            nodes = list(dict.fromkeys(n for it in quiz["items"] for n in it["node_ids"]))
            before = {n: (self.ledger.mastery_row(n) or {}).get("value") for n in nodes}
            evs = R.submit_results(self.ledger, quiz_id=quiz_id, results=results, occurred_at=occurred_at, actor=self.actor)
            out = self._finish(nodes, {"quiz_id": quiz_id, "events": len(evs)})
            out.update(self._quiz_conclusion(quiz_id, before, out["computed"]))
            return out
        return self._run(go)

    def _quiz_conclusion(self, quiz_id: str, before: dict, computed: dict) -> dict:
        quiz = self.ledger.quiz(quiz_id) or {"items": [], "target_node": None}
        counts: dict[str, dict[str, int]] = {}
        for it in quiz["items"]:
            for n in it["node_ids"]:
                c = counts.setdefault(n, {"correct": 0, "wrong": 0, "partial": 0, "unanswered": 0, "undetermined": 0, "pending": 0})
                c[it["result"] or "pending"] += 1
        target = quiz.get("target_node")
        per_node = []
        weak, insufficient = [], []
        for n, c in counts.items():
            row = computed.get(n) or {}
            after = row.get("mastery")
            counted = c["correct"] + c["wrong"] + c["partial"]
            entry = {"node": n, "name": row.get("name"), "before": before.get(n), "after": after, "level": row.get("level"),
                     "readiness": row.get("readiness"), "items": c, "is_target": n == target}
            per_node.append(entry)
            if counted < 2:
                insufficient.append(n)
            if n != target and after is not None and after < 0.45:
                weak.append(n)
        target_after = (computed.get(target) or {}).get("mastery") if target else None
        if weak:
            code, nxt = "prereq_weak", "这些前置在本次检查中表现不足，结合上下文提出针对性学习，再回到原问题"
        elif target and target_after is not None and target_after < 0.45:
            code, nxt = "prereqs_ok_target_stuck", "前置检查通过但目标仍困难：回到原问题继续梳理讲解，不再扩展额外诊断流程"
        elif per_node and all((e["after"] or 0) >= 0.65 for e in per_node if e["items"]["correct"] + e["items"]["wrong"] + e["items"]["partial"] > 0):
            code, nxt = "all_passed", "本轮全部通过；回到原问题或继续下一步"
        else:
            code, nxt = "mixed", "结果分化：关注掌握度下降的节点，继续练习"
        if insufficient:
            nxt += f"；证据不足（题数<2）的节点：{len(insufficient)} 个，可补测"
        return {"conclusion": code, "next": nxt, "per_node": per_node, "insufficient": insufficient, "weak_prereqs": weak}

    def self_assess(self, node_id: str, *, value: float, reason: str = "") -> dict:
        def go():
            before = (self.ledger.mastery_row(node_id) or {}).get("value")
            ev = R.self_assess(self.ledger, node_id, value=value, reason=reason, actor=self.actor)
            return self._finish([ev.payload["node_id"]], {"node_id": ev.payload["node_id"], "before": before, "set_to": ev.payload["value"],
                                                          "note": "已按自评设为该值一次；之后的作答/复习照常推动"})
        return self._run(go)

    # ── 查询 ──────────────────────────────────────────────────────────────
    def search(self, q: str, *, limit: int = 8, online: bool = False, include_public: bool = True,
               public_limit: int | None = None) -> dict:
        res = Q.search(self.ledger, q, limit=limit, include_public=include_public, public_limit=public_limit)
        res["ok"] = True
        if online and not res["local"]:
            try:
                hits = WD.search_online(q, limit=limit)
            except Exception as e:
                hits, res["online_error"] = [], str(e)[:120]
            known = {p["qid"] for p in res["public"]}
            for h in hits:
                if h["qid"] in known:
                    continue
                local = self.ledger.find_by_qid(h["qid"])
                res["public"].append({"qid": h["qid"], "label": h["label"], "description": h["description"],
                                      "local_node": local["id"] if local else None, "path": [], "online": True})
        if not res["local"] and not res["public"]:
            res["hint"] = "本地与公共目录都没有；可 online=true 再查，或按原文与语境新建本地节点"
        return res

    def browse(self, parent: str | None = None, *, limit: int = 40) -> dict:
        return dict(Q.browse(self.ledger, parent, limit=limit), ok=True)

    def node(self, node_id: str, *, records_limit: int = 8) -> dict:
        d = Q.node_detail(self.ledger, node_id, records_limit=records_limit)
        if d is None:
            return {"ok": False, "code": "node_not_found", "error": f"节点不存在：{node_id}"}
        d["ok"] = True
        return d

    def neighbors(self, node_id: str, depth: int = 1) -> dict:
        return dict(Q.neighbors(self.ledger, node_id, depth=depth), ok=True)

    def stats(self) -> dict:
        return dict(Q.stats(self.ledger), ok=True, db=str(self.ledger.path), vault=str(self.writer.root) if self.writer else None)

    # ── 维护 ──────────────────────────────────────────────────────────────
    def rebuild(self) -> dict:
        n = self.ledger.rebuild()
        compute.recompute(self.ledger)
        pages = self.writer.rebuild_all(self.ledger) if self.writer else 0
        return {"ok": True, "events_replayed": n, "pages": pages}

    def rebuild_markdown(self) -> dict:
        if self.writer is None:
            return {"ok": False, "code": "no_vault", "error": "未配置 vault 目录"}
        return {"ok": True, "pages": self.writer.rebuild_all(self.ledger), "dir": str(self.writer.nodes_dir)}

    def wikidata_fetch(self, qid: str, *, refresh: bool = False) -> dict:
        try:
            e = WD.fetch_entity(self.ledger, qid, refresh=refresh)
        except Exception as ex:
            return {"ok": False, "code": "fetch_failed", "error": str(ex)[:200]}
        if e is None:
            return {"ok": False, "code": "not_found", "error": f"Wikidata 没有 {qid}"}
        touched = []
        local = self.ledger.find_by_qid(qid)
        if local is not None:
            for ev in R.generate_auto_relations(self.ledger, local["id"], qid, actor=self.actor):
                touched.extend(ev.node_ids)
        base = {"qid": qid, "label": e["label"], "description": e["description"], "claims": len(WD.claims_of(self.ledger, qid))}
        return self._finish(sorted(set(touched)), base) if touched else dict(base, ok=True)

    def wikidata_import(self, path: str, **kw) -> dict:
        res = WD.import_minimal_index(self.ledger, path, **kw)
        touched = []
        for row in self.ledger.db.execute("SELECT id, qid FROM nodes WHERE status='active' AND qid IS NOT NULL").fetchall():
            for ev in R.generate_auto_relations(self.ledger, row["id"], row["qid"], actor=self.actor):
                touched.extend(ev.node_ids)
        return self._finish(sorted(set(touched)), res) if touched else dict(res, ok=True)

    # ── 通用派发（HTTP / 工具用）──────────────────────────────────────────
    def register(self, payload: dict) -> dict:
        t = str((payload or {}).get("type") or "").strip().lower()
        p = {k: v for k, v in (payload or {}).items() if k != "type"}
        try:
            if t == "node":
                return self.create_node(name=p.get("name", ""), kind=p.get("kind", "concept"), aliases=p.get("aliases"), qid=p.get("qid"),
                                        summary=p.get("summary", ""), source=p.get("source"), fetch_public=bool(p.get("fetch_public")))
            if t == "node_update":
                return self.update_node(p.get("node_id", ""), name=p.get("name"), kind=p.get("kind"), aliases=p.get("aliases"), summary=p.get("summary"))
            if t == "merge_node":
                return self.merge_node(p.get("node_id", ""), p.get("into", ""), reason=p.get("reason", ""))
            if t == "bind_qid":
                return self.bind_qid(p.get("node_id", ""), p.get("qid", ""), fetch_public=p.get("fetch_public", True))
            if t == "unbind_qid":
                return self.unbind_qid(p.get("node_id", ""))
            if t == "definition":
                return self.add_definition(p.get("node_id", ""), text=p.get("text", ""), source=p.get("source"), context_key=p.get("context_key", ""),
                                           decision=p.get("decision", ""), supersedes=p.get("supersedes", ""), uses=p.get("uses"))
            if t == "record":
                return self.add_record(p.get("node_id", ""), text=p.get("text", ""), kind=p.get("kind", "note"), source=p.get("source"),
                                       occurred_at=p.get("occurred_at"), dedupe_key=p.get("dedupe_key"))
            if t == "merge_records":
                return self.merge_records(p.get("node_id", ""), record_ids=p.get("record_ids") or [], text=p.get("text", ""),
                                          occurrences=p.get("occurrences"), earliest=p.get("earliest"))
            if t == "relation":
                return self.add_relation(from_id=p.get("from", p.get("from_id", "")), to_id=p.get("to", p.get("to_id", "")),
                                         type=p.get("relation_type", p.get("rtype", "")), evidence=p.get("evidence", ""), source=p.get("source"),
                                         allow_redundant=bool(p.get("allow_redundant")))
            if t == "relation_retract":
                return self.retract_relation(p.get("relation_id", ""), reason=p.get("reason", ""))
            if t == "relation_change":
                return self.change_relation(p.get("relation_id", ""), type=p.get("relation_type"), reverse=bool(p.get("reverse")),
                                            evidence=p.get("evidence", ""), source=p.get("source"), reason=p.get("reason", ""))
            if t == "card":
                return self.bind_card(node_ids=p.get("node_ids") or [], anki_note_id=p.get("anki_note_id"), anki_card_ids=p.get("anki_card_ids"),
                                      front=p.get("front", ""), back=p.get("back", ""), deck=p.get("deck", ""), card_key=p.get("card_key"))
            if t == "card_make":
                return self.make_card(node_ids=p.get("node_ids") or [], front=p.get("front", ""), back=p.get("back", ""),
                                      deck=p.get("deck") or AK.DEFAULT_DECK, tags=p.get("tags"))
            if t == "quiz":
                return self.register_quiz(items=p.get("items") or [], target_node=p.get("target_node"), title=p.get("title", ""), source=p.get("source"))
            if t == "quiz_result":
                return self.submit_results(quiz_id=p.get("quiz_id", ""), results=p.get("results") or [], occurred_at=p.get("occurred_at"))
            if t == "self_assess":
                return self.self_assess(p.get("node_id", ""), value=p.get("value"), reason=p.get("reason", ""))
        except RegisterError as e:
            return e.to_dict()
        return {"ok": False, "code": "bad_type", "error": f"未知登记类型：{t}",
                "types": ["node", "node_update", "merge_node", "bind_qid", "unbind_qid", "definition", "record", "merge_records",
                          "relation", "relation_retract", "relation_change", "card", "card_make", "quiz", "quiz_result", "self_assess"]}
