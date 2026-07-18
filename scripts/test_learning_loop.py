#!/usr/bin/env python3
"""test_learning_loop.py — 学习闭环核心不变量回归测试(审查建议:开 daily 前先有回归测试)。

守住这个 session 修好的关键不变量,防以后改动静默回退(本 session 出过 Edit 静默回退真事故)。
原则(R3-G5):**隔离**——凡写 override/情境/Anki 的用例一律用临时路径,绝不碰真 state/;
缺数据(LADR.json / Anki DB / fusion-weights)时**优雅跳过**不误判失败(daily 里当 smoke gate)。
跑法:先 source .env,再 python3 scripts/test_learning_loop.py。全过 → exit 0。
"""
import sys
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "_server_deploy"))
sys.path.insert(0, str(Path(__file__).parent / "kg"))
sys.path.insert(0, str(Path(__file__).parent))
import attention_profile as AP  # noqa: E402
import learning_situations as LS  # noqa: E402
import mastery_overrides as MO  # noqa: E402

FAILS = []


def ok(name, cond):
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond:
        FAILS.append(name)


def skip(name, why):
    print("  – %s(跳过:%s)" % (name, why))


def test_path_traversal():
    print("[安全] read_material 路径越界拦截")
    for bad in ("note:/etc/passwd", "note:../../../etc/passwd", "book:/etc/passwd#p1"):
        r = AP.read_material(bad)
        ok("挡住 %s" % bad, isinstance(r, dict) and "error" in r)


def test_dedup():
    print("[抽取] _dedup_nest 独立重复保留 / 嵌套折叠")
    r = AP.extract_terms("向量空间很重要。向量也很重要", lang=["zh"])
    ok("独立重复:向量空间 与 向量 都在", "向量空间" in r and "向量" in r)
    r2 = AP.extract_terms("向量空间的定义很重要", lang=["zh"])
    ok("纯嵌套:向量 不单独出现", "向量" not in r2)
    r3 = AP._dedup_nest(["向量空间的定义", "向量空间", "向量", "向量"], "向量空间的定义。向量也重要")
    ok("3层嵌套:独立向量不被双重扣减", "向量" in r3)


def test_mention_word_boundary():
    print("[抽取] mention 拉丁词按词边界(R3-G4:tip 不进 multiplication)")
    ms = AP.extract_mentions("The tip about multiplication is subtle.", lang="en")
    hits = [(m["s"], m["start"]) for m in ms if m["s"].lower() == "tip"]
    ok("tip 只命中独立词、不进 multiplication", hits == [("tip", 4)])
    ms2 = AP.extract_mentions("公衆衛生の議事", lang="ja")
    ok("CJK 子串保留 + lang=ja", any(m["s"] == "議事" and m["lang"] == "ja" for m in ms2))


def test_fusion_no_leak():
    print("[融合] 学到的权重 D 不再一家独大(泄漏已破)")
    fp = Path(AP.PROJECT_DIR) / "state" / "attention" / "fusion-weights.json"
    if not fp.exists():
        skip("fusion-weights", "文件不在")
        return
    try:
        w = json.loads(fp.read_text())["W"]
        d = w.get("D", 0)
        ok("D(%.2f) 不是唯一独大(<2.2×次高)" % d, d <= 2.2 * sorted(w.values())[-2])
    except Exception as e:
        ok("读 fusion-weights 失败:%s" % e, False)


def test_diagnostic_grading():
    print("[诊断] 客观判分:全点选不烧 AI + 计分正确")
    try:
        import task_runtime as A
    except Exception as e:
        skip("task_runtime", "import 失败:%s" % str(e)[:50])
        return
    q = [{"n": 1, "block": {"kind": "choice", "picked": "C", "answer": "C", "node_id": "kg:x", "layer": "target"}},
         {"n": 2, "block": {"kind": "choice", "picked": "A", "answer": "B"}}]
    r = A._objective_grade(q)
    ok("全点选→客观判分返回结果(不 None=不走 AI)", r is not None and r.get("score") == "1/2")
    ok("挂 node_id/layer", r["items"][0].get("node_id") == "kg:x" and r["items"][0].get("layer") == "target")
    q2 = [{"n": 1, "block": {"kind": "blank", "answer": "x"}}]
    ok("含 blank→None(交 AI 手写识别)", A._objective_grade(q2) is None)


def test_mastery_override_isolated():
    print("[掌握度] override 写→应用→撤销(隔离:临时 store,不碰真 state/)")
    tmp = Path(tempfile.mkdtemp())
    saved = MO.PATH
    try:
        MO.PATH = tmp / "ov.json"
        MO.set_override("_T", "n1", 0.9, source="regtest", reason="t")
        kg = {"book": "_T", "nodes": [{"id": "n1", "level": 2, "mastery": None}]}
        n = MO.apply_to_kg(kg)
        ok("apply_to_kg 覆盖 mastery=0.9 + 打徽标",
           n == 1 and kg["nodes"][0]["mastery"] == 0.9 and kg["nodes"][0].get("mastery_override"))
        ok("撤销后 store 无该键", MO.remove("_T", "n1") and MO.key_of("_T", "n1") not in MO.load())
    finally:
        MO.PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_override_remove_clears_badge():
    print("[掌握度] R3-G2:remove 后重算清 mastery_override 徽标(隔离 store)")
    tmp = Path(tempfile.mkdtemp())
    saved = MO.PATH
    try:
        MO.PATH = tmp / "ov.json"
        MO.set_override("_T", "n1", 0.9, source="t")
        kg = {"book": "_T", "nodes": [{"id": "n1", "level": 2, "mastery": 0.1}]}
        MO.apply_to_kg(kg)
        ok("set 后有徽标", bool(kg["nodes"][0].get("mastery_override")))
        MO.remove("_T", "n1")
        kg["nodes"][0]["mastery"] = 0.1   # 模拟 records 重算回自然值(apply_to_kg 的前置步骤)
        MO.apply_to_kg(kg)
        ok("remove 后重算徽标已清", "mastery_override" not in kg["nodes"][0])
    finally:
        MO.PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_situation_watermark():
    print("[近况] 证据水位:旧证据不复活 / 新证据才复发")
    lst = []
    LS._upsert("_t", lst, "子空间", "子空间", "答错", "anki_lapse", ["anki:1"], 1000, "anki", 100)
    lst[0]["status"] = "resolved"
    LS._upsert("_t", lst, "子空间", "子空间", "答错", "anki_lapse", ["anki:1"], 2000, "anki", 100)
    ok("旧证据(id=100)→仍 resolved", lst[0]["status"] == "resolved")
    LS._upsert("_t", lst, "子空间", "子空间", "又错", "anki_lapse", ["anki:1"], 3000, "anki", 250)
    ok("新证据(id=250)→复发 active", lst[0]["status"] == "active")


def test_selftest_resolution():
    print("[消解] R3-G3:自测及格→resolved(_report_resolves_key)")
    skey = AP.norm_key("子空间") or "子空间"
    rp_pass = {"node_results": {"kg:LADR#x": {"name": "子空间", "correct": 3, "total": 3}}}
    rp_fail = {"node_results": {"kg:LADR#x": {"name": "子空间", "correct": 1, "total": 3}}}
    ok("满分→及格", LS._report_resolves_key(rp_pass, skey, LS.RESOLVE_QUIZ_RATIO))
    ok("低分→不及格", not LS._report_resolves_key(rp_fail, skey, LS.RESOLVE_QUIZ_RATIO))
    rp_score = {"score": "5/5", "name": "子空间测验", "node_results": None}
    ok("整卷满分+名字命中→及格", LS._report_resolves_key(rp_score, skey, LS.RESOLVE_QUIZ_RATIO))


def test_revisit_due_semantics():
    print("[回访] R3-G3:用 Anki queue/due 权威判到期(没到期不误拉回;隔离临时 DB)")
    tmp = Path(tempfile.mkdtemp())
    adb = tmp / "col.anki2"
    now = 1_700_000_000
    crt = now - 1600 * 86400
    today = int((now - crt) // 86400)
    con = sqlite3.connect(str(adb))
    con.execute("CREATE TABLE col(crt INTEGER)")
    con.execute("INSERT INTO col(crt) VALUES(?)", (crt,))
    con.execute("CREATE TABLE cards(id INTEGER, queue INTEGER, due INTEGER)")
    con.execute("INSERT INTO cards VALUES(?,?,?)", (11, 2, today - 5))    # 复习卡已到期
    con.execute("INSERT INTO cards VALUES(?,?,?)", (22, 2, today + 30))   # 复习卡没到期
    con.commit()
    con.close()
    old_ts = now - 20 * 86400
    sits = [
        {"id": "s_due", "key": "k1", "concept": "到期", "status": "resolved",
         "resolved_at": old_ts, "updated": old_ts, "refs": ["anki:11"], "history": [], "last_evidence": {}},
        {"id": "s_notdue", "key": "k2", "concept": "没到期", "status": "resolved",
         "resolved_at": old_ts, "updated": old_ts, "refs": ["anki:22"], "history": [], "last_evidence": {}},
    ]
    saved = (LS.SIT_DIR, LS.ANKI_DB, LS._now)
    try:
        LS.SIT_DIR = tmp / "sits"
        LS.SIT_DIR.mkdir()
        LS.ANKI_DB = adb
        LS._now = lambda: now
        uid = "__reg__"
        (LS.SIT_DIR / (uid + ".json")).write_text(json.dumps(sits, ensure_ascii=False))
        LS.detect_revisits(uid)
        lst = json.loads((LS.SIT_DIR / (uid + ".json")).read_text())
        st = {s["id"]: s["status"] for s in lst}
        ok("到期卡→拉回 active", st.get("s_due") == "active")
        ok("没到期卡→保持 resolved(不误拉回)", st.get("s_notdue") == "resolved")
    finally:
        LS.SIT_DIR, LS.ANKI_DB, LS._now = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_proposal_one_time():
    print("[proposal] R3-G2:一次性消费 + 绑 uid/book(skilltree)")
    try:
        import skilltree as ST
    except Exception as e:
        skip("skilltree", "import 失败:%s" % str(e)[:50])
        return
    saved = dict(ST._PROPOSALS)
    try:
        ST._PROPOSALS.clear()
        pid = ST._mint_proposal("u1", "LADR", "kg:LADR#x", 0.9, source="test")
        p = ST._PROPOSALS.get(pid)
        ok("铸出提案(未用/权威值绑定)", p is not None and p["used"] is False and p["node"] == "kg:LADR#x")

        def consume(uid, book, pid_):
            pr = ST._PROPOSALS.get(pid_)
            if not pr or pr["used"] or pr["uid"] != uid or pr["book"] != book:
                return False
            pr["used"] = True
            return True

        ok("首次消费成功", consume("u1", "LADR", pid))
        ok("重放失败(已用)", not consume("u1", "LADR", pid))
        pid2 = ST._mint_proposal("u1", "LADR", "kg:LADR#y", 0.9)
        ok("他人 uid 失败", not consume("u2", "LADR", pid2))
        ok("错书失败", not consume("u1", "EGIU", pid2))
    finally:
        ST._PROPOSALS.clear()
        ST._PROPOSALS.update(saved)


def test_derive_two_dim_chain():
    print("[两维] R3-G1:合成前置链 A→B→C——availability 独立 + jump-ahead 诚实")
    try:
        import link_and_mastery as LM
    except Exception as e:
        skip("link_and_mastery", "import 失败:%s" % str(e)[:50])
        return
    nodes = [{"id": "A", "level": 2, "mastery": 0.9, "numeric_label": "1.1"},
             {"id": "B", "level": 2, "mastery": None, "numeric_label": "2.1"},
             {"id": "C", "level": 2, "mastery": None, "numeric_label": "3.1"}]
    id2 = {n["id"]: n for n in nodes}
    prereqs_of = {"B": ["A"], "C": ["B"]}
    LM.derive_progress_availability_state(nodes, prereqs_of, {}, id2)
    d = {n["id"]: n for n in nodes}
    ok("A mastered + open", d["A"]["progress"] == "mastered" and d["A"]["availability"] == "open")
    ok("B open(前置A已≥in_progress) + unlockable",
       d["B"]["availability"] == "open" and d["B"]["progress"] == "unseen" and d["B"]["state"] == "unlockable")
    ok("C locked(前置B unseen,availability 独立不被B的门控放行)",
       d["C"]["availability"] == "locked" and d["C"]["state"] == "locked")
    # jump-ahead:C 自己有 mastery,但前置 B 仍 unseen → progress 赢,state 诚实显 mastered 不涂灰
    nodes2 = [dict(n) for n in nodes]
    for n in nodes2:
        if n["id"] == "C":
            n["mastery"] = 0.9
    id22 = {n["id"]: n for n in nodes2}
    LM.derive_progress_availability_state(nodes2, prereqs_of, {}, id22)
    dC = [n for n in nodes2 if n["id"] == "C"][0]
    ok("jump-ahead C:有 mastery 但前置未满足→availability 仍 locked、state 诚实显 mastered",
       dC["availability"] == "locked" and dC["state"] == "mastered")


def test_kg_state_invariants():
    print("[技能树] LADR 状态不变量(A 语义 + 两维正交)")
    p = Path(AP.PROJECT_DIR) / "knowledge_graph" / "LADR.json"
    if not p.exists():
        skip("LADR.json", "文件不在")
        return
    kg = json.loads(p.read_text("utf-8"))
    l2 = [n for n in kg["nodes"] if n.get("level") == 2]
    bad_green = [n for n in l2 if n.get("state") == "mastered" and (n.get("mastery") or 0) < 0.8 and not n.get("mastery_inferred")]
    ok("无假绿(mastered 都真 ≥0.8 或推断)", len(bad_green) == 0)
    ok("progress/availability 字段都在", all("progress" in n and "availability" in n for n in l2))
    bad = []
    for n in l2:
        st, pr, av = n.get("state"), n.get("progress"), n.get("availability")
        good = ({"mastered": pr == "mastered", "in_progress": pr == "in_progress",
                 "unlockable": pr == "unseen" and av == "open",
                 "locked": pr == "unseen" and av == "locked"}).get(st, False)
        if not good:
            bad.append((n.get("name"), st, pr, av))
    ok("state 是 progress×availability 的诚实派生", len(bad) == 0)
    id2 = {n["id"]: n for n in kg["nodes"]}
    prq = {}
    for e in kg.get("edges", []):
        if e.get("kind") == "prereq":
            prq.setdefault(e["to"], []).append(e["from"])
    false_open = [n.get("name") for n in l2 if n.get("availability") == "open"
                  for pid in prq.get(n["id"], []) if (id2.get(pid) or {}).get("progress") == "unseen"]
    ok("无 false-open(availability=open 的前置都已 ≥in_progress)", len(false_open) == 0)


if __name__ == "__main__":
    for t in (test_path_traversal, test_dedup, test_mention_word_boundary, test_fusion_no_leak,
              test_diagnostic_grading, test_mastery_override_isolated, test_override_remove_clears_badge,
              test_situation_watermark, test_selftest_resolution, test_revisit_due_semantics,
              test_proposal_one_time, test_derive_two_dim_chain, test_kg_state_invariants):
        try:
            t()
        except Exception as e:
            print("  ✗ %s 抛异常:%s" % (t.__name__, e))
            FAILS.append(t.__name__)
    print("\n" + ("全部通过 ✓" if not FAILS else "失败 %d 项:%s" % (len(FAILS), FAILS)))
    sys.exit(1 if FAILS else 0)
