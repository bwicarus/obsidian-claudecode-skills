#!/usr/bin/env python3
"""promote_concepts.py — emergent 概念图【第一节:只长节点,不连边】。

用户设计(倒转架构):不预建整棵技能树,而是从**真实学习活动**里把「知识点」长成节点,
以后再复用 link_with_ai 连边、挂进 authored KG,最终汇成一张网。

铁律 / 路由:
  - 语言项(单词/语法)= vocab 系统单独管,**绝不进概念图**(用 vocab 库 + 停用词滤掉)。
  - 概念种子只取**主动知识点信号**:登记笔记(000-xxx 标题)+ 诊断卷 node_results 名。
    高亮太吵(高亮的是句子片段)默认不当种子;焦点榜/查词被 vocab 污染,绝不当源。
  - 已在 authored KG 的概念 → 标记 in_authored_kg(以后加固/挂接,不新建重复节点)。
  - 节点带 provenance(来自哪条笔记/诊断),来源=AI/行为自动 → 可确认/否决(下一节做)。

store: state/attention/emergent-graph.json = {"nodes": {key: {...}}, "meta": {...}}
CLI: (默认 dry-run 打印) | --write 落盘 | --json
"""
import sys
import json
import re
import glob
import time
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import attention_profile as AP  # noqa: E402
from config import NOTE_PATTERN  # noqa: E402

VAULT = Path(AP.VAULT_ROOT)
OUT = config.PROJECT_DIR / "state" / "attention" / "emergent-graph.json"
CHECK_DIR = config.PROJECT_DIR / "state" / "reader-check-reports"
KG_DIR = config.PROJECT_DIR / "knowledge_graph"

STOP = {"我们", "称为", "定义", "全问未回答", "全問未回答", "如果", "可以", "一个", "这个",
        "那个", "以下", "进行", "了解", "实际上", "这种", "大多数", "一切", "过程", "离开"}


def _vocab_set():
    """vocab 库 lemma 集(用来把语言项路由出概念图)。
    v3-A:返回 None = vocab 库不可达/为空 —— 调用方必须 **fail-closed**(拒绝收种,而非全放行)。
    此前空集 fail-open 是最坏方向:env 缺失时 VAULT 退 Windows 路径 → 空集 → 日语词全漏进概念图。"""
    vroot = VAULT / "资源" / "vocab"
    if not vroot.exists():
        return None
    v = set()
    for p in vroot.rglob("*.md"):
        if "_audio" in str(p):
            continue
        v.add(p.stem.lower())
    return v or None


def _authored_kg_terms():
    """authored KG 已有的 L2 概念 norm_key(判 NEW vs 已在树)。"""
    t = {}
    for f in glob.glob(str(KG_DIR / "*.json")):
        if ".bak." in f or "pre" in Path(f).stem or "scan" in Path(f).stem:
            continue
        try:
            kg = json.loads(Path(f).read_text("utf-8"))
        except Exception:
            continue
        book = kg.get("book") or Path(f).stem
        for n in kg.get("nodes", []):
            if n.get("level") == 2:
                k = AP.norm_key(n.get("name", "")) or n.get("name", "")
                if k:
                    t[k] = "%s#%s" % (book, n.get("id"))
    return t


def collect_seeds():
    """概念种子 {key: {surface, sources:set, provenance:list, signal:int}}(已路由 vocab/停用词/回收站)。"""
    vocab = _vocab_set()
    if vocab is None:
        # v3-A fail-closed:vocab 门无法评估 → 本轮不收任何种子(打印到 stderr 便于排查)
        print("⚠ vocab 库不可达/为空 → fail-closed:本轮不收种子(防语言项漏进概念图)", file=sys.stderr)
        return {}
    seeds = {}

    def add(term, src, ref):
        term = (term or "").strip()
        if len(term) < 2 or term in STOP or "回收站" in term:
            return
        k = AP.norm_key(term) or term
        # v3-A 繁简双查:norm_key 繁→简(議事→议事)而 vocab 存原形(議事.md)→ 归一键和原 surface 都要查
        if k.lower() in vocab or term.lower() in vocab:
            return
        e = seeds.setdefault(k, {"surface": term, "sources": set(), "provenance": [], "signal": 0})
        e["sources"].add(src)
        e["signal"] += 1
        if len(e["provenance"]) < 8:
            e["provenance"].append({"type": src, "ref": ref})

    # 源1:登记笔记(000-xxx,递归)——标题=一个知识点
    for p in VAULT.glob("**/*.md"):
        if NOTE_PATTERN.match(p.name) and "回收站" not in str(p):
            add(re.sub(r"^[0-9A-Fa-f]{3}-", "", p.stem), "note", p.name)
    # 源2:诊断卷 node_results 名——被测过=当概念学过
    for f in glob.glob(str(CHECK_DIR / "*.json")):
        try:
            reps = json.loads(Path(f).read_text("utf-8"))
        except Exception:
            continue
        for r in reps if isinstance(reps, list) else []:
            if not isinstance(r, dict) or r.get("sandbox"):
                continue
            for _nid, e in (r.get("node_results") or {}).items():
                add(e.get("name"), "diagnostic", r.get("name") or "")
    return seeds


def build(write=False):
    seeds = collect_seeds()
    kg_terms = _authored_kg_terms()
    nodes = {}
    for k, v in seeds.items():
        nid = "em:" + hashlib.sha1(k.encode("utf-8")).hexdigest()[:12]
        in_kg = k in kg_terms
        _bk = kg_terms.get(k, "")
        nodes[k] = {
            "id": nid, "surface": v["surface"], "key": k,
            "sources": sorted(v["sources"]), "signal": v["signal"],
            "provenance": v["provenance"],
            "in_authored_kg": in_kg, "authored_ref": _bk,
            # 来源属性(faceted graph:UI 按 subject/books 投影出单科/单书的树)
            "books": ([_bk.split("#")[0]] if in_kg and _bk else []),
            "subject": "",
            "kind": "concept", "origin": "emergent", "confirmed": None,
        }
    out = {"nodes": nodes, "meta": {"built": int(time.time()), "n": len(nodes),
           "n_new": sum(1 for x in nodes.values() if not x["in_authored_kg"]),
           "sources": ["note", "diagnostic"], "note": "第一节:只长节点未连边;origin=emergent 待确认/连边"}}
    if write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(OUT)
    return out


_EDGE_PROMPT = """下面是用户从学习笔记里积累的一批**零散知识点**(跨科目:微积分/线代/群论/复数 等)。
请:
1. 按科目把它们分组(每个知识点归一个科目)。
2. 在**同组内**判断**严格前置**(from→to 表示"不先懂 from 就根本不可能懂 to"):只列必备前置,多数只有 0-2 条。
3. 无严格前置但强相关的,可给 related(无向,少量)。跨科目一般不连。

知识点(编号 | 名称 | 状态 | 来源):
{listing}

只输出严格 JSON,无任何额外文字:
{{"groups": {{"<科目名>": [编号,...]}},
 "edges": [{{"from": <编号>, "to": <编号>, "kind": "prereq", "reason": "<不超过20字>"}}]}}
"""


def build_edges(model="sonnet", effort="medium", write=False):
    """第二节:给 emergent 概念节点连边(同科目内严格前置 + related)。一次 AI 调用,防环。"""
    sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
    from ai_backends import make_backend
    g = json.loads(OUT.read_text("utf-8"))
    nodes = g["nodes"]
    items = list(nodes.values())
    lines = []
    for i, n in enumerate(items):
        tag = ("已在树" if n["in_authored_kg"] else "新")
        src = n["provenance"][0]["ref"] if n.get("provenance") else ""
        lines.append("%d | %s | %s | %s" % (i, n["surface"], tag, src))
    prompt = _EDGE_PROMPT.format(listing="\n".join(lines))
    backend = make_backend("claude_cli", {"command": "/usr/bin/claude",
                                          "model": model, "effort": effort, "timeout": 180})
    raw = backend.chat([{"role": "user", "content": prompt}]).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    a, b = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[a:b + 1]) if (a != -1 and b > a) else {}
    # 编号 → key
    idx2key = {i: n["key"] for i, n in enumerate(items)}
    edges = []
    seen = set()
    for e in data.get("edges", []):
        try:
            fk, tk = idx2key[int(e["from"])], idx2key[int(e["to"])]
        except Exception:
            continue
        if fk == tk:
            continue
        kind = e.get("kind") if e.get("kind") in ("prereq", "related") else "prereq"
        key = (fk, tk, kind)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"from": fk, "to": tk, "kind": kind, "reason": (e.get("reason") or "")[:40],
                      "origin": "emergent", "confirmed": None})
    # 环检测(只对 prereq 有向边):拓扑排序,剔除环内边
    from collections import defaultdict as _dd
    prq = [e for e in edges if e["kind"] == "prereq"]
    indeg = _dd(int); adj = _dd(list)
    for e in prq:
        adj[e["from"]].append(e["to"]); indeg[e["to"]] += 1
    nodes_in = set(e["from"] for e in prq) | set(e["to"] for e in prq)
    q = [x for x in nodes_in if indeg[x] == 0]; ok = set()
    while q:
        x = q.pop(); ok.add(x)
        for v in adj[x]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    cyc = nodes_in - ok
    if cyc:
        edges = [e for e in edges if not (e["kind"] == "prereq" and e["from"] in cyc and e["to"] in cyc)]
    groups = data.get("groups", {})
    # 来源属性:把 AI 科目分组写回每个节点的 subject(供按科目切换)
    for _subj, _idxs in groups.items():
        for _i in _idxs:
            try:
                _k = idx2key[int(_i)]
            except Exception:
                continue
            if _k in nodes:
                nodes[_k]["subject"] = _subj
    g["edges"] = edges
    g["groups"] = groups
    g["meta"]["n_edges"] = len(edges)
    g["meta"]["edges_built"] = int(time.time())
    if write:
        OUT.write_text(json.dumps(g, ensure_ascii=False, indent=1), "utf-8")
    return g


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--edges", action="store_true", help="第二节:给已落盘的 emergent 节点连边(需 AI)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    a = ap.parse_args()
    if a.edges:
        g = build_edges(model=a.model, effort=a.effort, write=a.write)
        gm = g["meta"]
        k2s = {k: n["surface"] for k, n in g["nodes"].items()}
        print("emergent 边:%d %s" % (gm.get("n_edges", 0), "[已落盘]" if a.write else "[dry-run]"))
        print("科目分组:", {kk: len(vv) for kk, vv in g.get("groups", {}).items()})
        for e in g.get("edges", []):
            arrow = "→" if e["kind"] == "prereq" else "—"
            print("  %s %s %s  (%s)" % (k2s.get(e["from"], e["from"])[:14], arrow,
                  k2s.get(e["to"], e["to"])[:14], e.get("reason", "")))
        sys.exit(0)
    r = build(write=a.write)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        m = r["meta"]
        print("emergent 概念节点:%d(新 %d / 已在 authored KG %d)%s" %
              (m["n"], m["n_new"], m["n"] - m["n_new"], "  [已落盘]" if a.write else "  [dry-run]"))
        new = sorted([x for x in r["nodes"].values() if not x["in_authored_kg"]], key=lambda x: -x["signal"])
        print("\n▶ 新 emergent 节点(按信号,前 25):")
        for x in new[:25]:
            print("  %-20s  来源=%s ×%d  例:%s" %
                  (x["surface"][:20], "/".join(x["sources"]), x["signal"],
                   (x["provenance"][0]["ref"] if x["provenance"] else "")[:24]))
        inkg = [x for x in r["nodes"].values() if x["in_authored_kg"]]
        print("\n▶ 已在 authored KG(加固不新建):", [x["surface"][:12] for x in inkg[:12]])
