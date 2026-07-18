#!/usr/bin/env python3
"""test_learning_loop.py — 学习闭环核心不变量回归测试(审查建议:开 daily 前先有回归测试)。

守住这个 session 修好的关键不变量,防以后改动静默回退(本 session 出过 Edit 静默回退真事故)。
跑法:先 source .env,再 python3 scripts/test_learning_loop.py。全过 → exit 0。
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "kg"))
import attention_profile as AP  # noqa: E402
import learning_situations as LS  # noqa: E402
import mastery_overrides as MO  # noqa: E402

FAILS = []


def ok(name, cond):
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond:
        FAILS.append(name)


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


def test_fusion_no_leak():
    print("[融合] 学到的权重 D 不再一家独大(泄漏已破)")
    try:
        w = json.loads((Path(AP.PROJECT_DIR) / "state" / "attention" / "fusion-weights.json").read_text())["W"]
        d, mx = w.get("D", 0), max(w.values())
        ok("D(%.2f) 不是唯一独大(<2×次高)" % d, d <= 2.2 * sorted(w.values())[-2])
    except Exception as e:
        ok("读 fusion-weights 失败:%s" % e, False)


def test_diagnostic_grading():
    print("[诊断] 客观判分:全点选不烧 AI + 计分正确")
    try:
        sys.path.insert(0, "/home/bwicarus/webapp")
        import task_runtime as A   # _objective_grade/_attach_nodes 在判分核心 task_runtime
    except Exception as e:
        ok("import task_runtime:%s" % e, False)
        return
    q = [{"n": 1, "block": {"kind": "choice", "picked": "C", "answer": "C", "node_id": "kg:x", "layer": "target"}},
         {"n": 2, "block": {"kind": "choice", "picked": "A", "answer": "B"}}]
    r = A._objective_grade(q)
    ok("全点选→客观判分返回结果(不 None=不走 AI)", r is not None and r.get("score") == "1/2")
    ok("挂 node_id/layer", r["items"][0].get("node_id") == "kg:x" and r["items"][0].get("layer") == "target")
    q2 = [{"n": 1, "block": {"kind": "blank", "answer": "x"}}]
    ok("含 blank→None(交 AI 手写识别)", A._objective_grade(q2) is None)


def test_mastery_override():
    print("[掌握度] override:写→应用(状态计算前注入)→撤销")
    MO.remove("_TEST", "n1")
    MO.set_override("_TEST", "n1", 0.9, source="regtest", reason="t")
    kg = {"book": "_TEST", "nodes": [{"id": "n1", "level": 2, "mastery": None}]}
    n = MO.apply_to_kg(kg)
    ok("apply_to_kg 覆盖 mastery=0.9 + 标记", n == 1 and kg["nodes"][0]["mastery"] == 0.9 and kg["nodes"][0].get("mastery_override"))
    ok("撤销后 store 无该键", MO.remove("_TEST", "n1") and MO.key_of("_TEST", "n1") not in MO.load())


def test_situation_watermark():
    print("[近况] 证据水位:旧证据不复活 / 新证据才复发")
    lst = []
    LS._upsert("_t", lst, "子空间", "子空间", "答错", "anki_lapse", ["anki:1"], 1000, "anki", 100)
    lst[0]["status"] = "resolved"
    LS._upsert("_t", lst, "子空间", "子空间", "答错", "anki_lapse", ["anki:1"], 2000, "anki", 100)
    ok("旧证据(id=100)→仍 resolved", lst[0]["status"] == "resolved")
    LS._upsert("_t", lst, "子空间", "子空间", "又错", "anki_lapse", ["anki:1"], 3000, "anki", 250)
    ok("新证据(id=250)→复发 active", lst[0]["status"] == "active")


def test_kg_state_invariants():
    print("[技能树] LADR 状态不变量(A 语义 + 两维正交)")
    p = Path(AP.PROJECT_DIR) / "knowledge_graph" / "LADR.json"
    if not p.exists():
        ok("LADR.json 存在", False)
        return
    kg = json.loads(p.read_text("utf-8"))
    l2 = [n for n in kg["nodes"] if n.get("level") == 2]
    # 无假绿:mastered 的 mastery 必须 ≥0.8(或推断)
    bad_green = [n for n in l2 if n.get("state") == "mastered" and (n.get("mastery") or 0) < 0.8 and not n.get("mastery_inferred")]
    ok("无假绿(mastered 都真 ≥0.8 或推断)", len(bad_green) == 0)
    # 两维正交字段存在
    ok("progress/availability 字段都在", all("progress" in n and "availability" in n for n in l2))
    # state = 派生一致:locked ⟺ availability=locked
    inconsistent = [n for n in l2 if (n.get("state") == "locked") != (n.get("availability") == "locked")]
    ok("state 与 availability 派生一致", len(inconsistent) == 0)


if __name__ == "__main__":
    for t in (test_path_traversal, test_dedup, test_fusion_no_leak, test_diagnostic_grading,
              test_mastery_override, test_situation_watermark, test_kg_state_invariants):
        try:
            t()
        except Exception as e:
            print("  ✗ %s 抛异常:%s" % (t.__name__, e))
            FAILS.append(t.__name__)
    print("\n" + ("全部通过 ✓" if not FAILS else "失败 %d 项:%s" % (len(FAILS), FAILS)))
    sys.exit(1 if FAILS else 0)
