#!/usr/bin/env python3
"""learning_situations.py — 学习近况系统(设计 references/attention-kb-design.md §5m)。

用户设计的「困难档案」状态机:行为信号触发 → AI 后台分析 → 双路注入 AI 上下文 → 自然响应 → 消解。
AI **只在两处参与**(生成时分析一次、对话中被动响应),全程不碰实时判断。

- 触发(后台,行为信号,不用 AI):Anki 同卡连续答错 ≥N / 自测正确率 <阈值。
- 消解(后台,行为对称):那些卡转为连续答对 / 自测及格 / 兴趣消退(archived)。
- **resolve 不删除,只转状态 + 留档**(用户铁律:删除会漏信息;全时间线可检索,复发可查)。
- 注入(双路,同焦点画像):最近 N 条 active(recency)+ 当前语境关键词检索 K 条(relevance,全时间线)。
- 反馈(第四条,AI 对话中捕获):situation_feedback(concept, understood|mute|still_stuck)。

状态:active(注入上下文) / resolved(解决,留档) / archived(不学了,留档)。
"""
import json
import hashlib
import re
import sqlite3
import sys
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import attention_profile as AP  # noqa: E402
from config import PROJECT_DIR  # noqa: E402

SIT_DIR = Path(PROJECT_DIR) / "state" / "learning-situations"
ANKI_DB = AP.ANKI_DB

LAPSE_TRIGGER_N = 3      # 同一卡近期答错 ≥ 这么多次 → 困难
LAPSE_WINDOW_D = 45      # 只看近 N 天的复习
CHECK_LOW = 0.5         # 自测正确率 < 此 → 低分触发
RESOLVE_CORRECT_N = 2   # 困难卡最近连续答对 ≥ 此 → 消解
STALE_DAYS = 30         # 近况 N 天没动静 → archived(兴趣消退)
REVISIT_MIN_DAYS = 7    # resolved 至少过 N 天才考虑遗忘回访
CTX_RECENT = 3          # 注入:最近 active 条数(recency)
CTX_REL = 4             # 注入:语境检索条数(relevance)


# ── 库(留档不删) ────────────────────────────────────────────────────────────
def _sit_path(uid):
    return SIT_DIR / ("%s.json" % (uid or "anon"))


def _sit_load(uid):
    try:
        return json.loads(_sit_path(uid).read_text("utf-8"))
    except Exception:
        return []


def _sit_save(uid, lst):
    SIT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _sit_path(uid).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(lst, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(_sit_path(uid))


@contextmanager
def _sit_locked(uid):
    """审查 #5:webapp feedback 与 daily 并发 load→改→save 会丢更新。整个改写序列持一把文件锁串行化。"""
    SIT_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(SIT_DIR / (".%s.lock" % (uid or "anon")), "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


def _now():
    return int(time.time())


def _upsert(uid, lst, concept, display, reason, trigger, refs, now, ev_src="", ev_id=0):
    """同 concept(归一键)合并 + **证据水位**(审查:旧错题不该每天重触发)。
    ev_src=证据源(anki/check),ev_id=该源单调 id(anki=MAX revlog.id / check=报告 ts);
    只有超过已消化水位的**新证据**才触发/复发,旧证据既不复活 resolved、也不刷新 active 的 updated。"""
    key = AP.norm_key(concept) or concept
    for s in lst:
        if s.get("key") == key:
            was = s.get("status")
            _wm = s.setdefault("last_evidence", {})
            _has_new = bool(ev_src) and ev_id > _wm.get(ev_src, 0)
            if was == "active":
                if not _has_new:
                    return s, False              # 已 active + 无新证据 → 完全不动
            else:                                # resolved / archived
                if not _has_new:
                    return s, False              # 旧证据不拉回(你说懂了不会被同一批旧错题复活)
                s["status"] = "active"
                s.setdefault("history", []).append({"ts": now, "ev": "复发(新证据,曾%s)" % was})
                s["analyzed"] = False
            s["reason"], s["trigger"], s["updated"] = reason, trigger, now
            s["refs"] = sorted(set((s.get("refs") or []) + refs))[:12]
            if ev_src:
                _wm[ev_src] = ev_id
            return s, False
    s = {"id": "s_" + hashlib.sha1(("%s|%d" % (key, now)).encode()).hexdigest()[:10],
         "key": key, "concept": display, "reason": reason, "trigger": trigger,
         "refs": refs[:12], "suspect": "", "suggested": "", "prereq": [],
         "status": "active", "analyzed": False, "created": now, "updated": now,
         "last_evidence": ({ev_src: ev_id} if ev_src else {}),
         "history": [{"ts": now, "ev": "触发:%s" % reason}]}
    lst.append(s)
    return s, True


# ── 触发(行为信号,不用 AI) ─────────────────────────────────────────────────
def _card_concept(sfld, cid, cache):
    """困难卡 → 知识点(concept)。优先 material_graph 到 KG 节点名(可解释、能连前置);否则卡面抽词 top1。
    带缓存:同一 cid 不重复 BFS(触发扫描里多次用到)。"""
    if cid in cache:
        return cache[cid]
    concept = ""
    try:
        g = AP.material_graph("anki:%d" % cid, direction="both", depth=4, limit=15)
        for layer in g.get("layers", []):
            for n in layer:
                if n["ref"].startswith("kg:"):
                    concept = n["label"].replace("知识点「", "").rstrip("」")
                    break
            if concept:
                break
    except Exception:
        pass
    if not concept:
        txt = re.sub(r"<[^>]+>", " ", str(sfld or ""))
        terms = AP.extract_terms(txt)
        concept = terms[0] if terms else ""
    cache[cid] = concept
    return concept


def detect_triggers(uid="1"):
    """扫行为信号 → 生成/更新 active 近况。返回 {new, updated, total_active}。
    Anki:按**知识点**聚合窗内 lapse(同 KG 节点的多张卡累计),≥N 触发;检查报告:正确率<阈值触发。"""
    lst = _sit_load(uid)
    now = _now()
    new = upd = 0
    ccache = {}
    # ① Anki 反复答错(窗内 ease=1,按 concept 聚合)
    if ANKI_DB.exists():
        try:
            con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
            cut = int((time.time() - LAPSE_WINDOW_D * 86400) * 1000)
            # ⚠ 不能 SELECT SUM(...) lapses + HAVING lapses>=N:cards 表有同名列 lapses(全历史累计),
            # SQLite 在 HAVING 里优先解析成 ca.lapses → 用错列、不受时间窗约束。
            # 改为 WHERE ease=1 后 COUNT=窗内答错数,concept 级聚合放 Python 侧。
            rows = con.execute(
                "SELECT r.cid, n.sfld, COUNT(*) wl, MAX(r.id) mx"
                " FROM revlog r JOIN cards ca ON ca.id=r.cid JOIN notes n ON n.id=ca.nid"
                " WHERE r.id>? AND r.ease=1 GROUP BY r.cid", (cut,)).fetchall()
            con.close()
        except Exception:
            rows = []
        by_c = {}
        for cid, sfld, wl, mx in rows:
            concept = _card_concept(sfld, cid, ccache)
            if not concept:
                continue
            e = by_c.setdefault(concept, {"lapses": 0, "cids": [], "mx": 0})
            e["lapses"] += wl
            e["cids"].append(cid)
            e["mx"] = max(e["mx"], mx or 0)
        for concept, e in by_c.items():
            if e["lapses"] < LAPSE_TRIGGER_N:
                continue
            n_cards = len(e["cids"])
            reason = ("Anki 反复答错 %d 次" % e["lapses"]) + (("(%d张卡)" % n_cards) if n_cards > 1 else "")
            refs = ["anki:%d" % c for c in e["cids"][:6]]
            _, is_new = _upsert(uid, lst, concept, concept, reason, "anki_lapse", refs, now, "anki", e["mx"])
            new += is_new
            upd += (not is_new)
    # ② 检查报告低分(直接读文件,不依赖 assistant——daily 子进程里 assistant 不在 path,原静默吞异常致此腿从不触发,审查指出)
    reports = []
    try:
        _cr = Path(PROJECT_DIR) / "state" / "reader-check-reports" / ("%s.json" % uid)
        reports = json.loads(_cr.read_text("utf-8"))
    except Exception:
        reports = []
    for r in reports[-30:]:
        if r.get("sandbox"):
            continue
        m = re.match(r"(\d+)\s*/\s*(\d+)", str(r.get("score") or ""))
        if not m:
            continue
        got, tot = int(m.group(1)), int(m.group(2))
        if tot == 0 or got / tot >= CHECK_LOW:
            continue
        terms = AP.extract_terms(str(r.get("name") or ""))
        concept = terms[0] if terms else str(r.get("name") or "")[:12]
        if not concept:
            continue
        reason = "自测低分(%s)" % m.group(0)
        ref = ("check:" + (r.get("file") or "")) if r.get("file") else ""
        _, is_new = _upsert(uid, lst, concept, concept, reason, "check_low",
                            [ref] if ref else [], now, "check", int(r.get("ts") or 0))
        new += is_new
        upd += (not is_new)
    _sit_save(uid, lst)
    return {"new": new, "updated": upd, "total_active": sum(1 for s in lst if s["status"] == "active")}


# ── AI 后台分析(生成新近况时一次;顺 material_graph 找前置根源) ────────────────
def analyze(uid="1", limit=6):
    """给未分析的 active 近况补「怀疑根源 + 建议」。AI 只在这里用一次,不碰对话。"""
    lst = _sit_load(uid)
    todo = [s for s in lst if s["status"] == "active" and not s.get("analyzed")][:limit]
    if not todo:
        return {"analyzed": 0}
    sys.path.insert(0, "/home/bwicarus/webapp")
    import assistant as A
    done = 0
    for s in todo:
        prereq = []
        for ref in s.get("refs", []):
            try:
                # both:anki 只有 up(源笔记),要先上溯才能顺 笔记→书页→KG→前置 链下探
                g = AP.material_graph(ref, direction="both", depth=4, limit=20)
                for layer in g.get("layers", []):
                    for n in layer:
                        if n["ref"].startswith("kg:") and n["ref"] not in prereq:
                            prereq.append(n["ref"])
            except Exception:
                pass
        prereq_names = [AP._material_label(r) for r in prereq[:6]]
        prompt = ("学生在「%s」这个知识点上反复出错(%s)。它在知识图谱里的相关/前置节点:%s。\n"
                  "用一句话(≤40字)推测**最可能的根源**(尤其是不是某个前置没掌握),再用一句话给**一个**"
                  "最该做的具体建议(复习某前置 / 做诊断卷 / 重看某页)。\n"
                  '只输出 JSON:{"suspect":"...","suggested":"..."}' 
                  % (s["concept"], s["reason"], "、".join(prereq_names) or "(无)"))
        try:
            raw = A._gemini_text(prompt, max_tokens=300, think=False) or ""
            m = re.search(r"\{.*\}", raw, re.S)
            d = json.loads(m.group(0)) if m else {}
            s["suspect"] = str(d.get("suspect") or "")[:120]
            s["suggested"] = str(d.get("suggested") or "")[:120]
        except Exception:
            pass
        s["prereq"] = prereq[:6]
        s["analyzed"] = True
        s["updated"] = _now()
        done += 1
    _sit_save(uid, lst)
    return {"analyzed": done}


# ── 消解(行为对称,不用 AI) ─────────────────────────────────────────────────
def detect_resolutions(uid="1"):
    """困难卡转连续答对 / 自测及格 → resolved;久无动静 → archived。留档不删。"""
    lst = _sit_load(uid)
    now = _now()
    res = arch = 0
    con = None
    if ANKI_DB.exists():
        try:
            con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
        except Exception:
            con = None
    for s in lst:
        if s["status"] != "active":
            continue
        # 兴趣消退
        if now - s["updated"] > STALE_DAYS * 86400:
            s["status"] = "archived"
            s.setdefault("history", []).append({"ts": now, "ev": "%d天没动静→归档" % STALE_DAYS})
            arch += 1
            continue
        # 困难卡最近连续答对
        cids = [int(r.split(":")[1]) for r in s.get("refs", []) if r.startswith("anki:")]
        if cids and con:
            ok = True
            for cid in cids:
                last = con.execute("SELECT ease FROM revlog WHERE cid=? ORDER BY id DESC LIMIT ?",
                                   (cid, RESOLVE_CORRECT_N)).fetchall()
                if len(last) < RESOLVE_CORRECT_N or any(e[0] == 1 for e in last):
                    ok = False
                    break
            if ok:
                s["status"] = "resolved"
                s["resolved_at"] = now   # 审查 #5:回访按 resolved_at 判,不用旧 updated
                s.setdefault("history", []).append({"ts": now, "ev": "困难卡连对%d次→解决" % RESOLVE_CORRECT_N})
                res += 1
    if con:
        con.close()
    _sit_save(uid, lst)
    return {"resolved": res, "archived": arch}


# ── 双路检索(注入 AI 上下文) ───────────────────────────────────────────────
def context(uid="1", query="", n_recent=CTX_RECENT, k_rel=CTX_REL):
    """recency(最近 active)+ relevance(当前语境关键词检索全时间线)。返回精简列表(注入用)。"""
    lst = _sit_load(uid)
    active = [s for s in lst if s["status"] == "active"]
    picked, seen = [], set()
    for s in sorted(active, key=lambda x: -x["updated"])[:n_recent]:
        picked.append(s)
        seen.add(s["id"])
    # relevance:当前语境抽词 → 归一键 → 匹配 concept(active 优先,resolved 低权也翻)
    qkeys = {AP.norm_key(t) or t for t in AP.extract_terms(query or "")}
    if qkeys:
        cand = active + [s for s in lst if s["status"] == "resolved"]
        rel = [s for s in cand if s["id"] not in seen and (s["key"] in qkeys or any(k in s["key"] or s["key"] in k for k in qkeys))]
        for s in sorted(rel, key=lambda x: (x["status"] != "active", -x["updated"]))[:k_rel]:
            picked.append(s)
            seen.add(s["id"])
    out = []
    for s in picked:
        out.append({"concept": s["concept"], "reason": s["reason"],
                    "suspect": s.get("suspect") or "", "suggested": s.get("suggested") or "",
                    "status": s["status"], "refs": s.get("refs", [])[:4]})
    return out


def context_line(uid="1", query=""):
    """注入系统提示的一段文字(和最近焦点/创造物清单同款:清单式,详情靠工具)。"""
    items = context(uid, query)
    if not items:
        return ""
    rows = []
    for it in items:
        tag = "" if it["status"] == "active" else "(曾困扰)"
        extra = ("——怀疑:%s;建议:%s" % (it["suspect"], it["suggested"])) if it["suspect"] else ""
        rows.append("- 「%s」%s%s%s" % (it["concept"], tag, it["reason"], extra))
    return ("\n\n★学习近况(你最近卡住的知识点;**别主动打断**,只有用户自然问到相关内容时才结合它——"
            "按情况提议做卷子/反问定义/直接出题;用户明确表态懂了或不会 → 调 situation_feedback):\n"
            + "\n".join(rows))


# ── 用户反馈(第四条:AI 对话中捕获) ─────────────────────────────────────────
def feedback(uid, concept, kind):
    """understood→resolved / mute→archived / still_stuck→强化(留 active,升级优先/建议)。留档。
    整个 load→改→save 持文件锁,防与 daily 并发丢更新(审查 #5)。"""
    with _sit_locked(uid):
        return _feedback_locked(uid, concept, kind)


def _feedback_locked(uid, concept, kind):
    lst = _sit_load(uid)
    key = AP.norm_key(concept) or concept
    now = _now()
    hit = None
    for s in lst:
        if s["key"] == key or key in s["key"] or s["key"] in key:
            hit = s
            break
    if not hit:
        return {"ok": False, "error": "没有叫「%s」的学习近况" % concept}
    if kind == "understood":
        hit["status"] = "resolved"
        hit["resolved_at"] = now
        ev = "用户表态已懂→解决"
    elif kind == "mute":
        hit["status"] = "archived"
        ev = "用户要求别提醒→归档"
    elif kind == "still_stuck":
        hit["priority"] = "high"
        ev = "用户表态确实不会→强化"
    else:
        return {"ok": False, "error": "kind 只能 understood/mute/still_stuck"}
    hit.setdefault("history", []).append({"ts": now, "ev": ev})
    hit["updated"] = now
    _sit_save(uid, lst)
    return {"ok": True, "concept": hit["concept"], "新状态": hit["status"], "记": ev}


def detect_revisits(uid="1"):
    """③ 遗忘回访:resolved 近况的困难卡在 Anki 里已到期(记忆可能衰减)+ 距 resolved ≥N 天 → 降级回 active。
    用 Anki 自己排的 ivl(下次间隔天数)判到期:now > 上次复习 + ivl 天 = 该复习了(不自算 FSRS,借调度器)。
    降级后由注入规则让 AI「问你是否还记得 X」;你确认记得 → situation_feedback(understood) 又回 resolved。留档不删。"""
    lst = _sit_load(uid)
    now = _now()
    revived = 0
    if not ANKI_DB.exists():
        return {"revisit": 0}
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
    except Exception:
        return {"revisit": 0}
    for s2 in lst:
        if s2.get("status") != "resolved":   # 去掉 trigger==revisit 永久跳过 → resolved 可被重复回访(审查)
            continue
        if now - (s2.get("resolved_at") or s2.get("updated", 0)) < REVISIT_MIN_DAYS * 86400:
            continue
        cids = [int(r.split(":")[1]) for r in s2.get("refs", []) if r.startswith("anki:")]
        overdue = False
        for cid in cids:
            try:
                row = con.execute("SELECT ivl FROM cards WHERE id=?", (cid,)).fetchone()
                last = con.execute("SELECT MAX(id) FROM revlog WHERE cid=?", (cid,)).fetchone()
            except Exception:
                continue
            if not row or not last or not last[0]:
                continue
            ivl_days = max(1, int(row[0]))          # cards.ivl>0=天;<0=秒(学习中),兜底成 1 天
            if now > last[0] / 1000.0 + ivl_days * 86400:
                overdue = True
                break
        if overdue:
            days = int((now - s2.get("updated", now)) / 86400)
            s2["status"] = "active"
            s2["trigger"] = "revisit"
            s2["reason"] = "遗忘回访:掌握 %d 天了、Anki 判定该复习" % days
            s2["analyzed"] = True                   # 回访不必再 AI 分析,建议固定=确认是否还记得
            s2["suggested"] = "问用户是否还记得「%s」的定义/要点,记不清就一起重做一遍" % s2.get("concept", "")
            s2.setdefault("history", []).append({"ts": now, "ev": "遗忘回访→拉回 active"})
            s2["updated"] = now
            revived += 1
    con.close()
    _sit_save(uid, lst)
    return {"revisit": revived}


def run_daily(uid="1"):
    """daily 一把梭:触发 → 分析 → 消解。整段持锁,与 webapp feedback 串行(审查 #5)。"""
    with _sit_locked(uid):
        t = detect_triggers(uid)
        a = analyze(uid)
        r = detect_resolutions(uid)
        v = detect_revisits(uid)
    return {"trigger": t, "analyze": a, "resolve": r, "revisit": v}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="1")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if a.daily:
        print(json.dumps(run_daily(a.uid), ensure_ascii=False))
    if a.show or not a.daily:
        print(context_line(a.uid) or "(暂无学习近况)")
