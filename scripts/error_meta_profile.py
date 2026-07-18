#!/usr/bin/env python3
"""error_meta_profile.py — 错误模式元画像(设计 references/attention-kb-design.md 支线)。

学习近况(§5m)是"你卡在**这个点**";元画像再上一层,是"你有**这类**系统性弱点"
(证明题弱 / 定义容易混 / 跨语言术语对应不清 / 计算粗心…)。给的是**学习策略**建议,不是单点补救。

样本源(都是"答错/卡住"的证据,零侵入复用):
- 学习近况库(concept + reason + 关联卡的卡面,让 AI 能看出题型)
- 检查报告的 node_results(诊断卷里没全对的知识点)

流程:collect → AI 归纳 2-4 个模式(name + 证据 + 策略)→ 存 state/error-meta/<uid>.json。
低频(daily)跑一次;工具 error_patterns() 让助手回答"我有什么系统性弱点"。不强注入(元层信息宏观,按需查)。
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import attention_profile as AP  # noqa: E402
from config import PROJECT_DIR  # noqa: E402

META_DIR = Path(PROJECT_DIR) / "state" / "error-meta"
ANKI_DB = AP.ANKI_DB
SAMPLE_MAX = 40


def _meta_path(uid):
    return META_DIR / ("%s.json" % (uid or "anon"))


def load(uid="1"):
    try:
        return json.loads(_meta_path(uid).read_text("utf-8"))
    except Exception:
        return {}


def _save(uid, d):
    META_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _meta_path(uid).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(_meta_path(uid))


def _card_front(cid):
    """cid -> 卡面正面纯文本(前 120 字),让 AI 看出题型。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
        r = con.execute("SELECT n.sfld FROM cards c JOIN notes n ON n.id=c.nid WHERE c.id=?", (int(cid),)).fetchone()
        con.close()
    except Exception:
        return ""
    if not r:
        return ""
    txt = re.sub(r"<[^>]+>|&nbsp;", " ", str(r[0] or ""))
    return re.sub(r"\s+", " ", txt).strip()[:120]


def collect_samples(uid="1"):
    """收集"答错/卡住"样本:学习近况(带卡面样例)+ 检查报告未全对的知识点。"""
    samples = []
    try:
        import learning_situations as LS
        for s in LS._sit_load(uid):
            if s.get("status") == "archived":
                continue
            samp = {"知识点": s.get("concept"), "信号": s.get("reason")}
            for ref in s.get("refs", []):
                if ref.startswith("anki:"):
                    front = _card_front(ref.split(":", 1)[1])
                    if front:
                        samp["题目样例"] = front
                        break
            samples.append(samp)
    except Exception:
        pass
    try:
        sys.path.insert(0, "/home/bwicarus/webapp")
        import assistant as A
        for rep in A._check_reports_load(uid)[-20:]:
            if rep.get("sandbox"):
                continue
            for nid, e in (rep.get("node_results") or {}).items():
                if (e.get("total") or 0) and (e.get("correct") or 0) < e["total"]:
                    samples.append({"知识点": AP._material_label(nid),
                                    "信号": "诊断卷 %d/%d" % (e.get("correct") or 0, e["total"])})
    except Exception:
        pass
    seen, out = set(), []
    for s in samples:
        k = s.get("知识点") or ""
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out[:SAMPLE_MAX]


def generate(uid="1"):
    """collect -> AI 归纳系统性弱点模式 -> 存。返回 {patterns, n_samples}。"""
    samples = collect_samples(uid)
    if len(samples) < 3:
        _save(uid, {"patterns": [], "n_samples": len(samples), "note": "样本太少,先多学一阵"})
        return {"patterns": 0, "n_samples": len(samples)}
    sys.path.insert(0, "/home/bwicarus/webapp")
    import assistant as A
    lines = []
    for s in samples:
        seg = "- %s(%s)" % (s.get("知识点"), s.get("信号"))
        if s.get("题目样例"):
            seg += " 题例:%s" % s["题目样例"]
        lines.append(seg)
    prompt = ("下面是一个学生最近**答错/反复卡住**的知识点(含信号和部分题目样例):\n"
              + "\n".join(lines) + "\n\n"
              "请跳出单个知识点,归纳出这个学生 **2-4 个系统性/元认知层面的弱点模式**"
              "(例如:证明题薄弱 / 定义与定理容易混 / 跨语言(中英日)术语对应不清 / 计算粗心 / "
              "抽象概念难落地 等——具体看上面数据归纳,别套模板)。每个模式给:name(<=10字)、"
              "evidence(2-4 个上面出现的知识点名)、strategy(一句可执行的学习策略建议)。\n"
              '只输出 JSON:{"patterns":[{"name":"...","evidence":["...","..."],"strategy":"..."}]}')
    try:
        raw = A._gemini_text(prompt, max_tokens=800, think=False) or ""
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        pats = d.get("patterns") or []
        pats = [{"name": str(p.get("name") or "")[:20],
                 "evidence": [str(x)[:40] for x in (p.get("evidence") or [])][:5],
                 "strategy": str(p.get("strategy") or "")[:200]}
                for p in pats if p.get("name")][:5]
    except Exception:
        pats = []
    _save(uid, {"patterns": pats, "n_samples": len(samples), "generated_ts": int(time.time())})
    return {"patterns": len(pats), "n_samples": len(samples)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="1")
    ap.add_argument("--gen", action="store_true")
    a = ap.parse_args()
    if a.gen:
        print(json.dumps(generate(a.uid), ensure_ascii=False))
    print(json.dumps(load(a.uid), ensure_ascii=False, indent=1))
