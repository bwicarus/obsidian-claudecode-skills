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
    """vocab 库 lemma 集(用来把语言项路由出概念图)。"""
    v = set()
    vroot = VAULT / "资源" / "vocab"
    if vroot.exists():
        for p in vroot.rglob("*.md"):
            if "_audio" in str(p):
                continue
            v.add(p.stem.lower())
    return v


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
    seeds = {}

    def add(term, src, ref):
        term = (term or "").strip()
        if len(term) < 2 or term in STOP or "回收站" in term:
            return
        k = AP.norm_key(term) or term
        if k.lower() in vocab:          # 路由:语言项 → vocab,排除
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
        nodes[k] = {
            "id": nid, "surface": v["surface"], "key": k,
            "sources": sorted(v["sources"]), "signal": v["signal"],
            "provenance": v["provenance"],
            "in_authored_kg": in_kg, "authored_ref": kg_terms.get(k, ""),
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


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
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
