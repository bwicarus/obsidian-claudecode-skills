#!/usr/bin/env python3
"""build_unified_graph.py — 统一知识网络(用户设计:新 emergent 图为主体,authored KG 折进来丰富结构)。

不删 authored KG(它们是来源)。把:
  - authored KG 全部节点/边(命名空间 <book>::<id> 防撞,origin=authored,confirmed=true)
  - emergent 新节点(origin=emergent,confirmed=null,合成 subject 章节挂上去)+ emergent 边(端点重映射)
合并成**一张跨书大网**,格式兼容 skilltree.html(id/level/parent_id/name/state/edges),
每节点带来源属性(origin/book/subject)供 UI facet 投影单科/单书。emergent 边端点里"已在树"的
锚点重映射到对应 authored 节点 → 新概念真正挂进旧骨架。

out: state/attention/unified-graph.json   CLI: [--write] [--json]
"""
import sys
import json
import glob
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import attention_profile as AP  # noqa: E402

KG_DIR = config.PROJECT_DIR / "knowledge_graph"
EMERGENT = config.PROJECT_DIR / "state" / "attention" / "emergent-graph.json"
OUT = config.PROJECT_DIR / "state" / "attention" / "unified-graph.json"
CONF = config.PROJECT_DIR / "state" / "attention" / "emergent-confirmations.json"

_KEEP = ("level", "name", "pages", "state", "mastery", "progress", "availability",
         "mastered", "unlocked", "bottleneck_score", "engaged", "numeric_label", "summary")


def _real_kgs():
    out = []
    for p in KG_DIR.glob("*.json"):
        s = p.stem
        if ".bak." in p.name or "pre" in s or "scan" in s:
            continue
        out.append(p)
    return sorted(out)


def build(write=False):
    nodes = []
    edges = []
    try:
        conf = json.loads(CONF.read_text("utf-8"))
    except Exception:
        conf = {"nodes": {}, "edges": {}}
    conf_nodes = conf.get("nodes", {})
    rejected = set()   # 被否决的 emergent key(节点剔除 + 级联剔边)
    # authored_ref("book#nodeid") → 统一图节点 id,用于 emergent 边锚点重映射
    authored_id_by_ref = {}

    # 1) 折入 authored KG(命名空间 <book>::<id>)
    for p in _real_kgs():
        try:
            kg = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        book = kg.get("book") or p.stem
        pref = book + "::"
        for n in kg.get("nodes", []):
            nid = n.get("id")
            un = {k: n.get(k) for k in _KEEP if k in n}
            un["id"] = pref + str(nid)
            un["parent_id"] = (pref + str(n["parent_id"])) if n.get("parent_id") else ""
            un["origin"] = "authored"
            un["book"] = book
            un["subject"] = book
            un["confirmed"] = True
            un["src_id"] = str(nid)
            nodes.append(un)
            authored_id_by_ref["%s#%s" % (book, nid)] = un["id"]
        for e in kg.get("edges", []):
            if not e.get("from") or not e.get("to"):
                continue
            edges.append({"from": pref + str(e["from"]), "to": pref + str(e["to"]),
                          "kind": e.get("kind", "prereq"), "level": e.get("level", 2),
                          "evidence": e.get("evidence", ""), "origin": "authored", "confirmed": True})

    # 2) emergent:新节点 + 合成 subject 章节;anchor(已在树)不新建,记它的 authored 统一 id
    key2uid = {}   # emergent key → 统一图 node id(新节点=em::key;anchor=对应 authored id)
    subj_l0 = {}
    try:
        g = json.loads(EMERGENT.read_text("utf-8"))
    except Exception:
        g = {"nodes": {}, "edges": []}
    for key, n in g.get("nodes", {}).items():
        if n.get("in_authored_kg") and n.get("authored_ref") in authored_id_by_ref:
            key2uid[key] = authored_id_by_ref[n["authored_ref"]]     # anchor → 已有 authored 节点
            continue
        _cf = conf_nodes.get(key)
        if _cf is False:              # 用户否决 → 从图剔除(级联剔边),也不建空章节
            rejected.add(key)
            continue
        subj = n.get("subject") or "未分类"
        # 合成该 subject 的 L0/L1 章节(只一次,且只在有存活节点时)
        if subj not in subj_l0:
            l0 = "em0::" + subj
            l1 = "em1::" + subj
            _chap = {"state": "unlockable", "progress": "in_progress", "availability": "open",
                     "mastered": False, "unlocked": True, "origin": "emergent", "book": "",
                     "subject": subj, "confirmed": None}
            nodes.append(dict(_chap, id=l0, level=0, name=subj, parent_id=""))
            nodes.append(dict(_chap, id=l1, level=1, name=subj, parent_id=l0))
            subj_l0[subj] = l1
        uid = "em::" + key
        key2uid[key] = uid
        nodes.append({"id": uid, "key": key, "level": 2, "name": n.get("surface") or key,
                      "parent_id": subj_l0[subj], "pages": [],
                      "state": "unlockable", "mastery": None, "progress": "unseen",
                      "availability": "open", "mastered": False, "unlocked": True,
                      "origin": "emergent", "book": "", "subject": subj, "confirmed": _cf,
                      "provenance": n.get("provenance", [])})
    conf_edges = conf.get("edges", {})
    for e in g.get("edges", []):
        fu, tu = key2uid.get(e["from"]), key2uid.get(e["to"])
        if not fu or not tu or fu == tu:
            continue
        ov = conf_edges.get("%s|%s|%s" % (e["from"], e["to"], e.get("kind", "prereq")))
        if ov is False:
            continue                      # 用户否决 → 不进统一图
        edges.append({"from": fu, "to": tu, "kind": e.get("kind", "prereq"), "level": 2,
                      "evidence": e.get("quote") or e.get("reason", ""), "origin": "emergent",
                      "status": e.get("status", "auto"),
                      "confirmed": True if ov else (e.get("status") == "audited" or None)})

    # R4:emergent 节点 availability 不再硬编码 open——按 **effective**(audited/user_confirmed)
    # prereq 真算:有未掌握前置 → locked。shadow/auto 边只展示不 gating(审计过才影响解锁)。
    id2u = {n["id"]: n for n in nodes}
    eff_pre = {}
    for e in edges:
        if e.get("origin") == "emergent" and e.get("kind") == "prereq"            and e.get("status") in ("audited", "user_confirmed"):
            eff_pre.setdefault(e["to"], []).append(e["from"])
    for n in nodes:
        if not str(n.get("id", "")).startswith("em::") or n.get("level") != 2:
            continue
        prs = eff_pre.get(n["id"], [])
        blocked = any((id2u.get(pid) or {}).get("progress") not in ("in_progress", "mastered")
                      for pid in prs)
        if prs and blocked:
            n["availability"] = "locked"
            n["state"] = "locked"
            n["unlocked"] = False

    # 去重边
    seen = set(); ded = []
    for e in edges:
        k = (e["from"], e["to"], e["kind"])
        if k in seen:
            continue
        seen.add(k); ded.append(e)
    edges = ded

    out = {"book": "unified", "nodes": nodes, "edges": edges,
           "meta": {"built": int(time.time()), "n_nodes": len(nodes), "n_edges": len(edges),
                    "n_emergent_nodes": sum(1 for n in nodes if n["origin"] == "emergent" and n["level"] == 2),
                    "n_emergent_edges": sum(1 for e in edges if e["origin"] == "emergent"),
                    "books": sorted({n["book"] for n in nodes if n.get("book")}),
                    "subjects": sorted({n["subject"] for n in nodes if n.get("subject")})}}
    if write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(OUT)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = build(write=a.write)
    m = r["meta"]
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("统一图:节点 %d,边 %d %s" % (m["n_nodes"], m["n_edges"], "[已落盘]" if a.write else "[dry-run]"))
        print("  其中 emergent 概念节点 %d,emergent 边 %d" % (m["n_emergent_nodes"], m["n_emergent_edges"]))
        print("  books facet:", m["books"])
        print("  subjects facet:", m["subjects"])
        # 抽查:emergent 新概念挂进 authored 的跨源边
        cross = [e for e in r["edges"] if e["origin"] == "emergent"
                 and (e["from"].startswith("em::") != e["to"].startswith("em::"))]
        id2 = {n["id"]: n.get("name") for n in r["nodes"]}
        print("  跨源边(emergent 新概念↔authored 骨架):")
        for e in cross[:8]:
            print("     %s → %s" % (id2.get(e["from"], e["from"])[:16], id2.get(e["to"], e["to"])[:16]))
