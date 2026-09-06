"""掌握度折叠、等级、准备度、前置成环校验。

模型（实施侧设计，常数手定、待数据校准；口径与旧 KG 保持一致以便对照）：

- 每个节点一个标量 m ∈ [0,1]，None = 没有任何掌握证据（与 0 区分）。
- 按事件时间顺序折叠（事件溯源，可重放）：
    quiz.result   信号 s = correct 1 / partial 0.5 / wrong 0；m ← m + K_QUIZ·(s − m)，
                  首条从中性先验 PRIOR=0.5 出发；unanswered / undetermined 记录但不动 m（"未答 ≠ 答错"）
    anki.snapshot 该卡最新 card_mastery 进卡表；信号 = 已绑卡均值；m ← m + K_ANKI·(信号 − m)
                  （首条直接 m = 信号：card_mastery 本身已是校准过的估计）
    self_assess   m ← 自评值（只在此刻改一次，之后证据照常推动 —— 用户 2026-09-06 拍板）
  近因加权，所以新错误立刻显现、不会被旧正确记录稀释；mastered 还要求证据 ≥ 2 条。
- 等级 level（沿用旧 KG 分桶）：无证据 → 0（没碰过）/ 1（有记录、定义或卡但没测过）；
  m ≥ 0.20 → 2；≥ 0.45 → 3；≥ 0.65 → 4；≥ 0.85 → 5；m < 0.20 → 1。
- progress：unseen / touched / in_progress / mastered(m ≥ 0.8)。
- availability 只由**有证据显示薄弱**的前置决定：某前置 m 不为 None 且 < 0.20 → locked；
  没有记录的前置不算薄弱（"没有记录不等于未掌握"），只进 unknown 清单。
- readiness：no_prereq_info / needs_basics / unknown_basics / ready。
- 关系（prereq）成环 → 拒绝并返回路径。
"""
from __future__ import annotations

from collections import deque
import time
from typing import Iterable

from .store import Ledger, dumps, loads

K_QUIZ = 0.5
K_ANKI = 0.3
PRIOR = 0.5            # 第一条答题证据从中性先验出发：一题对 → 0.75，一题错 → 0.25，不会一题就"已掌握"
MIN_EVIDENCE_MASTERED = 2   # 少于两条证据不判 mastered（证据不足 ≠ 已掌握）
MASTERED_THRESHOLD = 0.8
WEAK_THRESHOLD = 0.2
SIGNAL = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}
RESULT_KINDS = ("correct", "wrong", "partial", "unanswered", "undetermined")
LEVEL_BUCKETS = ((0.85, 5), (0.65, 4), (0.45, 3), (0.20, 2))


def clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def level_of(m: float | None, touched: bool) -> int:
    if m is None:
        return 1 if touched else 0
    for th, lv in LEVEL_BUCKETS:
        if m >= th:
            return lv
    return 1


def progress_of(m: float | None, touched: bool, evidence_count: int = 0) -> str:
    if m is None:
        return "touched" if touched else "unseen"
    return "mastered" if (m >= MASTERED_THRESHOLD and evidence_count >= MIN_EVIDENCE_MASTERED) else "in_progress"


def fold_node(ledger: Ledger, node_id: str) -> dict:
    """按时间顺序重放该节点的证据事件，返回 {value, evidence_count, cards, signals}。"""
    m: float | None = None
    evidence = 0
    cards: dict[int, float | None] = {}
    signals: list[dict] = []
    # 判分更正：同一题只在**首次**出现的位置生效一次，取投影里的最终结果（更正证据后重算）
    first_seq: dict[tuple[str, str], int] = {}
    evs = list(ledger.events(node_id=node_id, kinds=("quiz.result", "anki.snapshot", "self_assess")))
    for ev in evs:
        if ev.kind == "quiz.result":
            key = (ev.payload["quiz_id"], ev.payload["item_id"])
            first_seq.setdefault(key, ev.seq)
    for ev in evs:
        p = ev.payload
        if ev.kind == "quiz.result":
            key = (p["quiz_id"], p["item_id"])
            if first_seq.get(key) != ev.seq:
                continue
            row = ledger.db.execute("SELECT result FROM quiz_items WHERE quiz_id=? AND item_id=?", key).fetchone()
            result = (row["result"] if row is not None else p.get("result")) or ""
            if result not in SIGNAL:
                signals.append({"seq": ev.seq, "kind": "quiz", "result": result, "applied": False})
                continue
            s = SIGNAL[result]
            base = PRIOR if m is None else m
            m = base + K_QUIZ * (s - base)
            evidence += 1
            signals.append({"seq": ev.seq, "kind": "quiz", "result": result, "m": round(m, 4)})
        elif ev.kind == "anki.snapshot":
            cards[int(p["card_id"])] = p.get("mastery")
            vals = [v for v in cards.values() if v is not None]
            if not vals:
                continue
            s = clamp(sum(vals) / len(vals))
            m = s if m is None else m + K_ANKI * (s - m)
            evidence += 1
            signals.append({"seq": ev.seq, "kind": "anki", "cards": len(vals), "signal": round(s, 4), "m": round(m, 4)})
        elif ev.kind == "self_assess":
            m = clamp(p["value"])
            evidence += 1
            signals.append({"seq": ev.seq, "kind": "self", "m": round(m, 4)})
    return {"value": None if m is None else round(m, 4), "evidence_count": evidence,
            "cards": len(cards), "signals": signals[-8:], "last_seq": evs[-1].seq if evs else 0}


def touched_flag(ledger: Ledger, node_id: str) -> bool:
    db = ledger.db
    for sql in ("SELECT 1 FROM definitions WHERE node_id=? LIMIT 1",
                "SELECT 1 FROM records WHERE node_id=? LIMIT 1",
                "SELECT 1 FROM card_nodes WHERE node_id=? LIMIT 1"):
        if db.execute(sql, (node_id,)).fetchone() is not None:
            return True
    return False


def _value_of(ledger: Ledger, node_id: str, cache: dict[str, float | None]) -> float | None:
    if node_id in cache:
        return cache[node_id]
    row = ledger.db.execute("SELECT value FROM mastery WHERE node_id=?", (node_id,)).fetchone()
    v = None if row is None else row["value"]
    cache[node_id] = v
    return v


def readiness_of(ledger: Ledger, node_id: str, cache: dict[str, float | None] | None = None) -> dict:
    cache = cache if cache is not None else {}
    prereqs = ledger.prereqs_of(node_id)
    if not prereqs:
        return {"availability": "open", "readiness": "no_prereq_info", "weak": [], "unknown": [], "prereqs": []}
    weak, unknown = [], []
    for p in prereqs:
        v = _value_of(ledger, p, cache)
        if v is None:
            unknown.append(p)
        elif v < WEAK_THRESHOLD:
            weak.append(p)
    availability = "locked" if weak else "open"
    readiness = "needs_basics" if weak else ("unknown_basics" if unknown else "ready")
    return {"availability": availability, "readiness": readiness, "weak": weak, "unknown": unknown, "prereqs": prereqs}


def state_of(progress: str, availability: str) -> str:
    if progress == "mastered":
        return "mastered"
    if availability == "locked":
        return "locked"
    if progress == "in_progress":
        return "in_progress"
    return "unlockable"


def downstream_closure(ledger: Ledger, seeds: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    dq = deque(seeds)
    while dq:
        cur = dq.popleft()
        for nxt in ledger.successors_of(cur):
            if nxt not in seen:
                seen.add(nxt)
                dq.append(nxt)
    return seen


def recompute(ledger: Ledger, node_ids: Iterable[str] | None = None) -> dict[str, dict]:
    """重算掌握度（目标节点）+ 准备度（目标节点及其全部下游）。返回受影响节点的行。"""
    targets = list(dict.fromkeys(node_ids)) if node_ids is not None else ledger.active_node_ids()
    now = int(time.time())
    db = ledger.db
    with ledger._lock:
        for nid in targets:
            if ledger.node(nid) is None:
                continue
            f = fold_node(ledger, nid)
            touched = touched_flag(ledger, nid)
            m = f["value"]
            db.execute(
                "INSERT INTO mastery(node_id, value, level, progress, evidence_count, last_seq, updated_at, detail_json)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(node_id) DO UPDATE SET value=excluded.value, level=excluded.level, progress=excluded.progress,"
                " evidence_count=excluded.evidence_count, last_seq=excluded.last_seq, updated_at=excluded.updated_at,"
                " detail_json=excluded.detail_json",
                (nid, m, level_of(m, touched), progress_of(m, touched, f["evidence_count"]), f["evidence_count"], f["last_seq"], now,
                 dumps({"cards": f["cards"], "signals": f["signals"], "touched": touched})))
        affected = set(targets) | downstream_closure(ledger, targets)
        cache: dict[str, float | None] = {}
        out: dict[str, dict] = {}
        for nid in affected:
            row = ledger.db.execute("SELECT progress, detail_json FROM mastery WHERE node_id=?", (nid,)).fetchone()
            if row is None:
                if ledger.node(nid) is None:
                    continue
                touched = touched_flag(ledger, nid)
                db.execute("INSERT OR IGNORE INTO mastery(node_id, value, level, progress, updated_at, detail_json) VALUES(?,?,?,?,?,?)",
                           (nid, None, level_of(None, touched), progress_of(None, touched), now, dumps({"touched": touched})))
                row = ledger.db.execute("SELECT progress, detail_json FROM mastery WHERE node_id=?", (nid,)).fetchone()
            r = readiness_of(ledger, nid, cache)
            state = state_of(row["progress"], r["availability"])
            detail = ledger.db.execute("SELECT detail_json FROM mastery WHERE node_id=?", (nid,)).fetchone()
            d = loads(detail["detail_json"], {}) if detail else {}
            d["prereqs"] = {"weak": r["weak"], "unknown": r["unknown"], "all": r["prereqs"]}
            db.execute("UPDATE mastery SET availability=?, readiness=?, state=?, updated_at=?, detail_json=? WHERE node_id=?",
                       (r["availability"], r["readiness"], state, now, dumps(d), nid))
            out[nid] = ledger.mastery_row(nid) or {}
    return out


def prereq_cycle_path(ledger: Ledger, from_id: str, to_id: str, *, exclude_relation: str | None = None) -> list[str] | None:
    """若加入 from→to（from 是 to 的前置）会成环，返回环路径（从 to 走到 from），否则 None。
    exclude_relation：改方向时忽略即将撤回的那条旧边。"""
    if from_id == to_id:
        return [from_id, to_id]

    def succ(nid: str) -> list[str]:
        rows = ledger.db.execute(
            "SELECT to_id FROM relations WHERE from_id=? AND type='prereq' AND status='active' AND (? IS NULL OR id<>?)",
            (nid, exclude_relation, exclude_relation))
        return [r[0] for r in rows]

    parent: dict[str, str | None] = {to_id: None}
    dq = deque([to_id])
    while dq:
        cur = dq.popleft()
        for nxt in succ(cur):
            if nxt in parent:
                continue
            parent[nxt] = cur
            if nxt == from_id:
                path = [nxt]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])  # type: ignore[arg-type]
                path.reverse()
                return path + [to_id]
            dq.append(nxt)
    return None
