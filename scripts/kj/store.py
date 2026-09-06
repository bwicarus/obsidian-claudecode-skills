"""追加式事件账本（唯一权威）+ 可重建投影。

铁律：
- 一切改动先成为 ``events`` 表里的一行；投影表（nodes / definitions / records /
  relations / cards / quiz_* / mastery）全部可以从 events 重放得到（``rebuild``）。
- ``dedupe_key`` 让"同一次事件重复提交"自然幂等（Anki 快照按卡+日、判分按卷+题）。
- ``occurred_at``（事实发生时间，可空、不伪造）与 ``registered_at``（登记时间）分开存。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import CONTRACT, DB_SCHEMA_VERSION
from . import ids


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


@dataclass
class Event:
    seq: int
    id: str
    kind: str
    occurred_at: int | None
    registered_at: int
    actor: str
    source: dict | None
    payload: dict
    node_ids: list[str]
    duplicate: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    occurred_at INTEGER,
    registered_at INTEGER NOT NULL,
    actor TEXT DEFAULT '',
    source_json TEXT,
    payload_json TEXT NOT NULL,
    dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS event_nodes(
    seq INTEGER NOT NULL, node_id TEXT NOT NULL,
    PRIMARY KEY(seq, node_id)
);
CREATE INDEX IF NOT EXISTS idx_event_nodes_node ON event_nodes(node_id, seq);

CREATE TABLE IF NOT EXISTS nodes(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'concept',
    qid TEXT, summary TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
    merged_into TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_qid ON nodes(qid);
CREATE TABLE IF NOT EXISTS node_aliases(
    node_id TEXT NOT NULL, alias TEXT NOT NULL, lang TEXT DEFAULT '',
    PRIMARY KEY(node_id, alias)
);
CREATE TABLE IF NOT EXISTS definitions(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL, text TEXT NOT NULL,
    context_key TEXT DEFAULT '', source_json TEXT, created_at INTEGER NOT NULL,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_defs_node ON definitions(node_id);
CREATE TABLE IF NOT EXISTS records(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'note',
    text TEXT NOT NULL, occurred_at INTEGER, registered_at INTEGER NOT NULL,
    source_json TEXT, merged_into TEXT, occurrences INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_records_node ON records(node_id, registered_at);
CREATE TABLE IF NOT EXISTS relations(
    id TEXT PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL, type TEXT NOT NULL,
    evidence TEXT DEFAULT '', source_json TEXT, origin TEXT NOT NULL DEFAULT 'manual',
    derived_qid TEXT, status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL, retracted_at INTEGER, retract_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(from_id, status);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relations(to_id, status);
CREATE TABLE IF NOT EXISTS cards(
    card_key TEXT PRIMARY KEY, anki_note_id INTEGER, anki_card_ids_json TEXT,
    front TEXT DEFAULT '', back TEXT DEFAULT '', deck TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS card_nodes(
    card_key TEXT NOT NULL, node_id TEXT NOT NULL, PRIMARY KEY(card_key, node_id)
);
CREATE INDEX IF NOT EXISTS idx_card_nodes_node ON card_nodes(node_id);
CREATE TABLE IF NOT EXISTS card_snapshots(
    card_id INTEGER PRIMARY KEY, card_key TEXT, mastery REAL, ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quizzes(
    id TEXT PRIMARY KEY, title TEXT DEFAULT '', target_node TEXT, created_at INTEGER NOT NULL,
    source_json TEXT, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS quiz_items(
    quiz_id TEXT NOT NULL, item_id TEXT NOT NULL, question TEXT DEFAULT '',
    answer TEXT DEFAULT '', kind TEXT DEFAULT 'choice', node_ids_json TEXT NOT NULL,
    result TEXT, result_note TEXT, result_at INTEGER,
    PRIMARY KEY(quiz_id, item_id)
);
CREATE TABLE IF NOT EXISTS mastery(
    node_id TEXT PRIMARY KEY, value REAL, level INTEGER NOT NULL DEFAULT 0,
    progress TEXT NOT NULL DEFAULT 'unseen', availability TEXT NOT NULL DEFAULT 'open',
    readiness TEXT NOT NULL DEFAULT 'no_prereq_info', state TEXT NOT NULL DEFAULT 'unlockable',
    evidence_count INTEGER NOT NULL DEFAULT 0, last_seq INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS search_text(
    node_id TEXT PRIMARY KEY, name TEXT, aliases TEXT, body TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    node_id UNINDEXED, name, aliases, body, tokenize='trigram'
);
"""

# 公共目录单独一个文件（kj-public.db，ATTACH 为 pub）：几百万条 Wikidata 行不该把私人账本撑大、拖慢备份。
# 未加限定的表名 SQLite 先查 main 再查 pub，所以查询代码不用写 pub. 前缀。
_PUBLIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS pub.public_entities(
    qid TEXT PRIMARY KEY, label_en TEXT, label_zh TEXT, label_ja TEXT,
    desc_en TEXT, desc_zh TEXT, desc_ja TEXT, aliases_json TEXT,
    search_text TEXT, fetched_at INTEGER, source TEXT
);
CREATE TABLE IF NOT EXISTS pub.public_claims(
    qid TEXT NOT NULL, prop TEXT NOT NULL, target TEXT NOT NULL, rank TEXT DEFAULT 'normal',
    PRIMARY KEY(qid, prop, target)
);
CREATE INDEX IF NOT EXISTS pub.idx_public_claims_target ON public_claims(target, prop);
CREATE VIRTUAL TABLE IF NOT EXISTS pub.public_fts USING fts5(
    qid UNINDEXED, labels, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS pub.meta(k TEXT PRIMARY KEY, v TEXT);
"""

_PROJECTION_TABLES = (
    "nodes", "node_aliases", "definitions", "records", "relations", "cards", "card_nodes",
    "card_snapshots", "quizzes", "quiz_items", "mastery", "search_text", "node_fts",
)


class Ledger:
    """一个 SQLite 文件 = 一套账本。线程安全靠一把进程内锁 + WAL。"""

    def __init__(self, path: str | Path, public_path: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.public_path = Path(public_path) if public_path else self.path.with_name(self.path.stem + "-public" + self.path.suffix)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("ATTACH DATABASE ? AS pub", (str(self.public_path),))
        self.db.execute("PRAGMA pub.journal_mode=WAL")
        self._ensure_schema()

    # ── schema ────────────────────────────────────────────────────────────
    def _ensure_schema(self) -> None:
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.executescript(_PUBLIC_SCHEMA)
            self._migrate_public_tables_out_of_main()
            cur = self.db.execute("SELECT v FROM meta WHERE k='contract'")
            row = cur.fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta(k, v) VALUES('contract', ?)", (CONTRACT,))
                self.db.execute("INSERT INTO meta(k, v) VALUES('schema', ?)", (str(DB_SCHEMA_VERSION),))
            elif row[0] != CONTRACT:
                raise RuntimeError(f"账本合同不符：{row[0]} ≠ {CONTRACT}（{self.path}）")

    def _migrate_public_tables_out_of_main(self) -> None:
        """2026-09-06 晚拆库前，公共目录曾建在主账本里。若主库还留着 public_* 表，它会遮住 pub 里的同名表
        （未限定的表名先查 main），导入就写进旧表、旧表又没有 search_text 列 —— 实测第一次全量导入就是这么炸的。
        这里把旧行搬进 pub、删掉主库副本；幂等，没有旧表时什么都不做。"""
        db = self.db
        has_old = db.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type IN ('table','view') AND name='public_entities'").fetchone()
        if not has_old:
            return
        cols = [r[1] for r in db.execute("PRAGMA main.table_info(public_entities)")]
        search_expr = "search_text" if "search_text" in cols else \
            "TRIM(COALESCE(label_en,'') || ' / ' || COALESCE(label_zh,'') || ' / ' || COALESCE(label_ja,''), ' /')"
        db.execute("BEGIN")
        try:
            db.execute(
                "INSERT OR IGNORE INTO pub.public_entities(qid, label_en, label_zh, label_ja, desc_en, desc_zh, desc_ja,"
                " aliases_json, search_text, fetched_at, source)"
                f" SELECT qid, label_en, label_zh, label_ja, desc_en, desc_zh, desc_ja, aliases_json, {search_expr}, fetched_at, source"
                " FROM main.public_entities")
            if db.execute("SELECT 1 FROM main.sqlite_master WHERE name='public_claims'").fetchone():
                db.execute("INSERT OR IGNORE INTO pub.public_claims(qid, prop, target, rank)"
                           " SELECT qid, prop, target, rank FROM main.public_claims")
                db.execute("DROP TABLE main.public_claims")
            if db.execute("SELECT 1 FROM main.sqlite_master WHERE name='public_fts'").fetchone():
                db.execute("DROP TABLE main.public_fts")
            db.execute("DROP TABLE main.public_entities")
            db.execute("DELETE FROM pub.public_fts")
            db.execute("INSERT INTO pub.public_fts(qid, labels) SELECT qid, COALESCE(search_text, '') FROM pub.public_entities")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ── events ────────────────────────────────────────────────────────────
    def append(self, kind: str, payload: dict, *, node_ids: Iterable[str] = (),
               occurred_at: int | None = None, source: dict | None = None,
               actor: str = "", dedupe_key: str | None = None,
               registered_at: int | None = None) -> Event:
        """追加一条事件并立即更新投影。dedupe_key 命中 → 原样返回旧事件（duplicate=True）。"""
        node_ids = [n for n in dict.fromkeys(node_ids) if n]
        now = int(registered_at if registered_at is not None else time.time())
        with self._lock:
            if dedupe_key:
                row = self.db.execute("SELECT * FROM events WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                if row is not None:
                    ev = self._row_event(row)
                    ev.duplicate = True
                    return ev
            eid = ids.mint("ev")
            self.db.execute("BEGIN")
            try:
                cur = self.db.execute(
                    "INSERT INTO events(id, kind, occurred_at, registered_at, actor, source_json, payload_json, dedupe_key)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (eid, kind, occurred_at, now, actor or "", dumps(source) if source else None,
                     dumps(payload), dedupe_key),
                )
                seq = int(cur.lastrowid)
                for nid in node_ids:
                    self.db.execute("INSERT OR IGNORE INTO event_nodes(seq, node_id) VALUES(?,?)", (seq, nid))
                ev = Event(seq, eid, kind, occurred_at, now, actor or "", source, dict(payload), node_ids)
                self._apply(ev)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return ev

    def _row_event(self, row: sqlite3.Row) -> Event:
        seq = int(row["seq"])
        nids = [r[0] for r in self.db.execute("SELECT node_id FROM event_nodes WHERE seq=? ORDER BY node_id", (seq,))]
        return Event(seq, row["id"], row["kind"], row["occurred_at"], int(row["registered_at"]),
                     row["actor"] or "", loads(row["source_json"]), loads(row["payload_json"], {}), nids)

    def events(self, *, node_id: str | None = None, kinds: Iterable[str] | None = None,
               after_seq: int = 0) -> Iterator[Event]:
        sql = "SELECT e.* FROM events e"
        args: list[Any] = []
        where = ["e.seq > ?"]
        args.append(after_seq)
        if node_id:
            sql += " JOIN event_nodes n ON n.seq=e.seq"
            where.append("n.node_id=?")
            args.append(node_id)
        if kinds:
            ks = list(kinds)
            where.append("e.kind IN (%s)" % ",".join("?" * len(ks)))
            args.extend(ks)
        sql += " WHERE " + " AND ".join(where) + " ORDER BY e.seq"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        for row in rows:
            yield self._row_event(row)

    def last_seq(self) -> int:
        row = self.db.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def count(self, table: str) -> int:
        return int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # ── rebuild ───────────────────────────────────────────────────────────
    def rebuild(self) -> int:
        """清空全部投影，按 seq 重放事件。返回重放条数。掌握度由 compute 层随后重算。"""
        with self._lock:
            self.db.execute("BEGIN")
            try:
                for t in _PROJECTION_TABLES:
                    self.db.execute(f"DELETE FROM {t}")
                n = 0
                for ev in list(self.events()):
                    self._apply(ev)
                    n += 1
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return n

    # ── projections ───────────────────────────────────────────────────────
    def _apply(self, ev: Event) -> None:
        p = ev.payload
        k = ev.kind
        now = ev.registered_at
        db = self.db
        if k == "node.create":
            db.execute(
                "INSERT OR REPLACE INTO nodes(id, name, kind, qid, summary, status, merged_into, created_at, updated_at)"
                " VALUES(?,?,?,?,?, 'active', NULL, ?, ?)",
                (p["id"], p["name"], p.get("kind") or "concept", p.get("qid"), p.get("summary") or "", now, now))
            self._set_aliases(p["id"], p.get("aliases") or [])
            self._refresh_search(p["id"])
        elif k == "node.update":
            sets, args = [], []
            for f in ("name", "kind", "summary", "status"):
                if f in p and p[f] is not None:
                    sets.append(f"{f}=?"); args.append(p[f])
            if sets:
                args += [now, p["id"]]
                db.execute(f"UPDATE nodes SET {', '.join(sets)}, updated_at=? WHERE id=?", args)
            if "aliases" in p and p["aliases"] is not None:
                self._set_aliases(p["id"], p["aliases"])
            self._refresh_search(p["id"])
        elif k == "node.merge":
            db.execute("UPDATE nodes SET status='merged', merged_into=?, updated_at=? WHERE id=?", (p["into"], now, p["id"]))
            db.execute("UPDATE definitions SET node_id=? WHERE node_id=?", (p["into"], p["id"]))
            db.execute("UPDATE records SET node_id=? WHERE node_id=?", (p["into"], p["id"]))
            db.execute("UPDATE relations SET from_id=? WHERE from_id=? AND status='active'", (p["into"], p["id"]))
            db.execute("UPDATE relations SET to_id=? WHERE to_id=? AND status='active'", (p["into"], p["id"]))
            db.execute("UPDATE OR IGNORE card_nodes SET node_id=? WHERE node_id=?", (p["into"], p["id"]))
            db.execute("DELETE FROM search_text WHERE node_id=?", (p["id"],))
            db.execute("DELETE FROM node_fts WHERE node_id=?", (p["id"],))
            self._refresh_search(p["into"])
        elif k == "node.bind_qid":
            db.execute("UPDATE nodes SET qid=?, updated_at=? WHERE id=?", (p["qid"], now, p["id"]))
        elif k == "node.unbind_qid":
            db.execute("UPDATE nodes SET qid=NULL, updated_at=? WHERE id=?", (now, p["id"]))
        elif k == "definition.add":
            db.execute(
                "INSERT OR REPLACE INTO definitions(id, node_id, text, context_key, source_json, created_at, superseded_by)"
                " VALUES(?,?,?,?,?,?,NULL)",
                (p["id"], p["node_id"], p["text"], p.get("context_key") or "", dumps(p.get("source")) if p.get("source") else None, now))
            if p.get("supersedes"):
                db.execute("UPDATE definitions SET superseded_by=? WHERE id=?", (p["id"], p["supersedes"]))
            self._refresh_search(p["node_id"])
        elif k == "record.add":
            db.execute(
                "INSERT OR REPLACE INTO records(id, node_id, kind, text, occurred_at, registered_at, source_json, merged_into, occurrences)"
                " VALUES(?,?,?,?,?,?,?,NULL,1)",
                (p["id"], p["node_id"], p.get("kind") or "note", p["text"], ev.occurred_at, now,
                 dumps(p.get("source")) if p.get("source") else None))
        elif k == "record.merge":
            db.execute(
                "INSERT OR REPLACE INTO records(id, node_id, kind, text, occurred_at, registered_at, source_json, merged_into, occurrences)"
                " VALUES(?,?,?,?,?,?,?,NULL,?)",
                (p["id"], p["node_id"], p.get("kind") or "merged", p["text"], ev.occurred_at, now,
                 dumps(p.get("source")) if p.get("source") else None, int(p.get("occurrences") or len(p.get("record_ids") or []) or 1)))
            for rid in p.get("record_ids") or []:
                db.execute("UPDATE records SET merged_into=? WHERE id=? AND node_id=?", (p["id"], rid, p["node_id"]))
        elif k == "relation.add":
            db.execute(
                "INSERT OR REPLACE INTO relations(id, from_id, to_id, type, evidence, source_json, origin, derived_qid, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,'active',?)",
                (p["id"], p["from"], p["to"], p["type"], p.get("evidence") or "",
                 dumps(p.get("source")) if p.get("source") else None, p.get("origin") or "manual", p.get("derived_qid"), now))
        elif k == "relation.retract":
            db.execute("UPDATE relations SET status='retracted', retracted_at=?, retract_reason=? WHERE id=?",
                       (now, p.get("reason") or "", p["id"]))
        elif k == "card.bind":
            db.execute(
                "INSERT OR REPLACE INTO cards(card_key, anki_note_id, anki_card_ids_json, front, back, deck, status, created_at)"
                " VALUES(?,?,?,?,?,?,'active',?)",
                (p["card_key"], p.get("anki_note_id"), dumps(p.get("anki_card_ids") or []), p.get("front") or "",
                 p.get("back") or "", p.get("deck") or "", now))
            db.execute("DELETE FROM card_nodes WHERE card_key=?", (p["card_key"],))
            for nid in p.get("node_ids") or []:
                db.execute("INSERT OR IGNORE INTO card_nodes(card_key, node_id) VALUES(?,?)", (p["card_key"], nid))
        elif k == "card.unbind":
            db.execute("UPDATE cards SET status='inactive' WHERE card_key=?", (p["card_key"],))
        elif k == "anki.snapshot":
            db.execute("INSERT OR REPLACE INTO card_snapshots(card_id, card_key, mastery, ts) VALUES(?,?,?,?)",
                       (int(p["card_id"]), p.get("card_key"), p.get("mastery"), int(p.get("ts") or now)))
        elif k == "quiz.register":
            db.execute("INSERT OR REPLACE INTO quizzes(id, title, target_node, created_at, source_json, status) VALUES(?,?,?,?,?,'open')",
                       (p["id"], p.get("title") or "", p.get("target_node"), now, dumps(p.get("source")) if p.get("source") else None))
            for it in p.get("items") or []:
                db.execute(
                    "INSERT OR REPLACE INTO quiz_items(quiz_id, item_id, question, answer, kind, node_ids_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (p["id"], it["item_id"], it.get("question") or "", it.get("answer") or "", it.get("kind") or "choice",
                     dumps(it.get("node_ids") or [])))
        elif k == "quiz.result":
            db.execute("UPDATE quiz_items SET result=?, result_note=?, result_at=? WHERE quiz_id=? AND item_id=?",
                       (p["result"], p.get("note") or "", now, p["quiz_id"], p["item_id"]))
            db.execute("UPDATE quizzes SET status='graded' WHERE id=?", (p["quiz_id"],))
        elif k == "self_assess":
            pass  # 只在折叠时起作用，没有投影字段
        elif k == "mastery.note":
            pass  # 计算层写的解释性事件（可选）
        else:
            raise ValueError(f"未知事件类型：{k}")

    def _set_aliases(self, node_id: str, aliases: list) -> None:
        self.db.execute("DELETE FROM node_aliases WHERE node_id=?", (node_id,))
        for a in aliases:
            if isinstance(a, dict):
                alias, lang = str(a.get("alias") or "").strip(), str(a.get("lang") or "")
            else:
                alias, lang = str(a).strip(), ""
            if alias:
                self.db.execute("INSERT OR IGNORE INTO node_aliases(node_id, alias, lang) VALUES(?,?,?)", (node_id, alias, lang))

    def _refresh_search(self, node_id: str) -> None:
        n = self.db.execute("SELECT name, summary, status FROM nodes WHERE id=?", (node_id,)).fetchone()
        self.db.execute("DELETE FROM search_text WHERE node_id=?", (node_id,))
        self.db.execute("DELETE FROM node_fts WHERE node_id=?", (node_id,))
        if n is None or n["status"] != "active":
            return
        aliases = " / ".join(r[0] for r in self.db.execute("SELECT alias FROM node_aliases WHERE node_id=? ORDER BY alias", (node_id,)))
        defs = [r[0] for r in self.db.execute(
            "SELECT text FROM definitions WHERE node_id=? AND superseded_by IS NULL ORDER BY created_at", (node_id,))]
        body = " ".join([n["summary"] or ""] + [d[:300] for d in defs])
        self.db.execute("INSERT INTO search_text(node_id, name, aliases, body) VALUES(?,?,?,?)", (node_id, n["name"], aliases, body))
        self.db.execute("INSERT INTO node_fts(node_id, name, aliases, body) VALUES(?,?,?,?)", (node_id, n["name"], aliases, body))

    # ── 常用读取 ──────────────────────────────────────────────────────────
    def node(self, node_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()

    def resolve(self, node_id: str) -> sqlite3.Row | None:
        """跟随 merged_into 找到最终活跃节点。"""
        seen = set()
        cur = node_id
        while cur and cur not in seen:
            seen.add(cur)
            row = self.node(cur)
            if row is None:
                return None
            if row["status"] == "merged" and row["merged_into"]:
                cur = row["merged_into"]
                continue
            return row
        return None

    def aliases(self, node_id: str) -> list[dict]:
        return [{"alias": r["alias"], "lang": r["lang"]} for r in
                self.db.execute("SELECT alias, lang FROM node_aliases WHERE node_id=? ORDER BY alias", (node_id,))]

    def definitions(self, node_id: str, *, include_superseded: bool = False) -> list[dict]:
        sql = "SELECT * FROM definitions WHERE node_id=?" + ("" if include_superseded else " AND superseded_by IS NULL") + " ORDER BY created_at"
        return [dict(r) | {"source": loads(r["source_json"])} for r in self.db.execute(sql, (node_id,))]

    def records(self, node_id: str, *, include_merged: bool = False, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM records WHERE node_id=?" + ("" if include_merged else " AND merged_into IS NULL") + \
              " ORDER BY COALESCE(occurred_at, registered_at) DESC, registered_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) | {"source": loads(r["source_json"])} for r in self.db.execute(sql, (node_id,))]

    def relations(self, node_id: str, *, status: str = "active") -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM relations WHERE (from_id=? OR to_id=?) AND status=? ORDER BY created_at", (node_id, node_id, status))
        return [dict(r) | {"source": loads(r["source_json"])} for r in rows]

    def prereqs_of(self, node_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT from_id FROM relations WHERE to_id=? AND type='prereq' AND status='active'", (node_id,))]

    def successors_of(self, node_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT to_id FROM relations WHERE from_id=? AND type='prereq' AND status='active'", (node_id,))]

    def cards_of(self, node_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT c.* FROM cards c JOIN card_nodes cn ON cn.card_key=c.card_key WHERE cn.node_id=? AND c.status='active' ORDER BY c.created_at",
            (node_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["anki_card_ids"] = loads(r["anki_card_ids_json"], [])
            d["node_ids"] = [x[0] for x in self.db.execute("SELECT node_id FROM card_nodes WHERE card_key=?", (r["card_key"],))]
            out.append(d)
        return out

    def mastery_row(self, node_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM mastery WHERE node_id=?", (node_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["detail"] = loads(r["detail_json"], {})
        return d

    def active_node_ids(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT id FROM nodes WHERE status='active' ORDER BY created_at")]

    def find_by_qid(self, qid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM nodes WHERE qid=? AND status='active'", (qid,)).fetchone()

    def quiz(self, quiz_id: str) -> dict | None:
        q = self.db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if q is None:
            return None
        items = [dict(r) | {"node_ids": loads(r["node_ids_json"], [])} for r in
                 self.db.execute("SELECT * FROM quiz_items WHERE quiz_id=? ORDER BY item_id", (quiz_id,))]
        return dict(q) | {"source": loads(q["source_json"]), "items": items}
