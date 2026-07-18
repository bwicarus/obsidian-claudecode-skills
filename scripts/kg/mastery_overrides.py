#!/usr/bin/env python3
"""kg/mastery_overrides.py — 人工掌握度 override 层(学习闭环②:诊断卷客观证据 + 用户确认)。

link_and_mastery.py 每天从 Anki 卡反算 mastery/state 并 `--in-place` 重写 KG json,
所以**不能直接改 KG 的 state**(下次 daily 会覆盖)。改用这个独立 override store:
  - 诊断卷客观判分 → AI 提「掌握度变更提案」 → **用户确认** → 写 override(守铁律:绝不自动改)。
  - link_and_mastery 在状态计算**之前**注入 override 的 mastery → 解锁沿 prereq DAG 自然传播。
  - daily 重算不覆盖(override 是外挂层,每次算完重新应用);可追溯(source/reason/ts)、可撤销(remove)。

store: state/kg-mastery-overrides.json = { "<book>#<node_id>": {mastery, ts, source, reason, by, prev} }
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

PATH = Path(config.PROJECT_DIR) / "state" / "kg-mastery-overrides.json"


def load():
    try:
        return json.loads(PATH.read_text("utf-8"))
    except Exception:
        return {}


def save(d):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(PATH)


def key_of(book, node_id):
    return "%s#%s" % (book, node_id)


def set_override(book, node_id, mastery, source="", reason="", by="user_confirmed", prev=None):
    """写/更新一个节点的掌握度 override。mastery ∈ [0,1](≥0.8 会让它 mastered 并解锁后继)。"""
    d = load()
    d[key_of(book, node_id)] = {"mastery": float(mastery), "ts": int(time.time()),
                                "source": source, "reason": reason, "by": by, "prev": prev}
    save(d)
    return d[key_of(book, node_id)]


def remove(book, node_id):
    """撤销一个 override(留档思想:这里是真删 override,KG 会回到 Anki 反算的自然值)。"""
    d = load()
    k = key_of(book, node_id)
    if k in d:
        gone = d.pop(k)
        save(d)
        return gone
    return None


def apply_to_kg(kg):
    """把本 book 的 override mastery 注入 KG 节点(在状态计算前调用)。返回应用条数。
    只覆盖 mastery 数值 + 打 `mastery_override` 标记(供 UI 显示「人工确认」徽标 + 溯源);
    state/mastery_level/unlocked 交给后续拓扑计算,让解锁沿 DAG 传播。"""
    ov = load()
    if not ov:
        return 0
    book = kg.get("book") or ""
    n = 0
    for node in kg.get("nodes", []):
        o = ov.get(key_of(book, node.get("id")))
        if not o or o.get("mastery") is None:
            continue
        node["mastery_override"] = {"mastery": o["mastery"], "source": o.get("source", ""),
                                    "reason": o.get("reason", ""), "ts": o.get("ts"),
                                    "prev": node.get("mastery")}
        node["mastery"] = float(o["mastery"])
        n += 1
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--set", nargs=3, metavar=("BOOK", "NODE_ID", "MASTERY"))
    ap.add_argument("--rm", nargs=2, metavar=("BOOK", "NODE_ID"))
    a = ap.parse_args()
    if a.set:
        print(set_override(a.set[0], a.set[1], float(a.set[2]), source="cli", reason="manual"))
    elif a.rm:
        print(remove(a.rm[0], a.rm[1]))
    else:
        print(json.dumps(load(), ensure_ascii=False, indent=1))
