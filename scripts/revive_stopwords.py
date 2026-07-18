#!/usr/bin/env python3
"""revive_stopwords.py — 被停用词误伤的术语**复活赛**(用户设计 2026-07-19)。

用户方案:停用词按百分比激进划分、宁可扩大范围,**误伤由复活机制兜底**——
「计算经常和焦点词一同出现的词,就得到非泛用词的候选,积攒一定数量后让 AI 统一判断,
然后一定时间内不能再次进入复活赛」。

关键细化(干跑数据逼出来的):判据不能是"共现次数",也不能是简单的条件概率。
① 必须排除**自共现**——词自己也在焦点榜上时,「与自己同页」恒为 100%(实测「定义」647/647)。
② 条件概率 c(w,f)/n(w) 会被**常见锚点**带偏:「定义/向量」本身出现在几百页,任何词跟它们
   的共现率都天然高,于是 0.37~0.40 区间全是「于是/上述/接下来」这类噪声。
   改用 **PMI**:pmi = log2( c(w,f)·N / (n(w)·n(f)) ) —— 锚点越常见,期望共现越高、
   PMI 自动扣掉,只有**超出独立假设**的搭配才留下。「空间」跟「向量」超额共现 → 高 PMI;
   「于是」跟谁都按期望共现 → PMI≈0。
③ 快捷通道:被滤的词**本身就在焦点榜上** = 用户明确在关注它,直接进候选(最强信号)。

流程:候选发现(纯算法) → 冷却过滤 → 攒够 BATCH_MIN 个 → AI 一次判断 → 复活者写
revived-terms.json(build_auto_stopwords 把它并进保护名单,下轮不再滤);
判过的(无论复活与否)进冷却,COOLDOWN_DAYS 内不再参赛,不重复烧 AI。

out: state/attention/revived-terms.json + revive-log.json
CLI: [--dry] [--limit N] [--min-conc F]
"""
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

ATT = config.PROJECT_DIR / "state" / "attention"
SEARCH_DB = config.PROJECT_DIR / "state" / "pdf-search.db"
STOPWORDS = ATT / "auto-stopwords.json"
FOCUS = ATT / "focus.json"
REVIVED = ATT / "revived-terms.json"
LOG = ATT / "revive-log.json"

TOP_FOCUS = 40         # 拿前 N 个焦点词当"知识点锚"
MIN_SUPPORT = 15       # 与某焦点词同页 ≥ 这么多页才算数。PMI 的经典毛病是**小样本高估**:
#                        低频锚点(如书名词 millennium)偶然共现 3~5 页就分数虚高,实测
#                        quickly/error/university 全是这么冒出来的;好候选共现都 ≥17。
MIN_ANCHOR_PAGES = 30  # 锚点自身出现页数下限(同上,排除只在几十页露面的书名/专名)
MIN_CONC = 1.0         # PMI 阈值(bit):≥1 = 共现比"互相独立"高一倍以上
BATCH_MIN = 6          # 攒够这么多候选才叫 AI(省调用)
BATCH_MAX = 25         # 一次最多判这么多
COOLDOWN_DAYS = 30     # 判过的词多久内不再参赛
SAMPLE_PAGES = 4000    # 扫描页数上限(全库 1.2 万页,抽样足够看共现)


def _load(p, dflt):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return dflt


def _save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


_SENT_SPLIT = re.compile(r"(?<=[。．.!?！？；;])\s*")


def _sentence_with(body, w):
    """从整页正文里取出**包含 w 的那一整句**(而不是 ±50 字的字符窗口——那常从半个词开始)。"""
    lw = w.lower()
    for chunk in _SENT_SPLIT.split(re.sub(r"\s+", " ", body or "")):
        c = chunk.strip()
        if 8 <= len(c) <= 180 and lw in c.lower():
            return c
    return ""


def find_candidates(limit=BATCH_MAX, min_conc=MIN_CONC):
    """算法侧:被滤的词里,哪些其实是"只跟特定焦点词一起出现"的术语。"""
    sw = _load(STOPWORDS, {})
    words = set()
    for arr in (sw.get("words") or {}).values():
        words |= {str(w) for w in arr}
    if not words:
        return [], "还没有停用词表"
    focus = [r.get("key") or "" for r in (_load(FOCUS, {}).get("top") or [])[:TOP_FOCUS]]
    focus = [f for f in focus if f and len(f) >= 2]
    if not focus:
        return [], "focus.json 为空"
    log = _load(LOG, {})
    cut = time.time() - COOLDOWN_DAYS * 86400
    words = {w for w in words if float(log.get(w, {}).get("ts", 0)) < cut}   # 冷却期内不再参赛
    if not words:
        return [], "候选都在冷却期内"

    # 页级共现:子串匹配(比逐页分词快两个量级,共现统计够用)
    c = sqlite3.connect(str(SEARCH_DB))
    rows = c.execute("SELECT file, page, body FROM pages_data").fetchall()
    c.close()
    step = max(1, len(rows) // SAMPLE_PAGES)
    rows = rows[::step]
    n_pages = defaultdict(int)                 # w → 出现页数
    n_focus = defaultdict(int)                 # f → 出现页数(PMI 要拿它算期望)
    co = defaultdict(lambda: defaultdict(int))  # w → f → 同页数
    ex = defaultdict(list)                      # w → 例句们(给 AI 看:孤词判不准,要看它在原文怎么用)
    N = 0
    for _f, _p, body in rows:
        b = body or ""
        if not b:
            continue
        N += 1
        bl = b.lower()
        hits_f = [f for f in focus if f.lower() in bl]
        for f in hits_f:
            n_focus[f] += 1
        for w in words:
            if w.lower() not in bl:
                continue
            n_pages[w] += 1
            if len(ex[w]) < 3:
                # ★用户指出:只给一个孤词 AI 很难判——给**原文里的完整句子**,而且优先取
                #   「与焦点词同页」那种(最能体现它到底是术语还是虚词),多给两三条免得偏。
                sent = _sentence_with(b, w)
                if sent and all(sent != e[0] for e in ex[w]):
                    ex[w].append((sent, bool(hits_f)))
            for f in hits_f:
                if f.lower() != w.lower():        # ★排除自共现(否则恒 100%)
                    co[w][f] += 1

    import math
    focus_set = {f.lower() for f in focus}
    out = []
    for w, n in n_pages.items():
        if n < MIN_SUPPORT:
            continue
        best_f, best_pmi, best_c = "", -9.9, 0
        for f, k in co[w].items():
            if k < MIN_SUPPORT or n_focus.get(f, 0) < MIN_ANCHOR_PAGES:
                continue
            # PMI:实际共现 / 独立假设下的期望共现。锚点越常见,期望越高、分数自动被扣。
            pmi = math.log2((k * float(N)) / (n * float(n_focus[f])))
            if pmi > best_pmi:
                best_f, best_pmi, best_c = f, pmi, k
        hot = w.lower() in focus_set        # 快捷通道:被滤的词本身就在焦点榜 = 用户在关注它
        if hot or (best_f and best_pmi >= min_conc):
            out.append({"term": w, "pmi": round(best_pmi, 2), "pages": n,
                        "with": best_f, "co_pages": best_c, "on_focus": hot,
                        "peers": sorted(co[w], key=lambda x: -co[w][x])[:5],
                        "examples": [e[0][:170] for e in
                                     sorted(ex.get(w, []), key=lambda e: -int(e[1]))[:3]]})
    out.sort(key=lambda x: (-int(x["on_focus"]), -x["pmi"]))
    return out[:limit], ""


_PROMPT = """下面这些词此前被"通用语过滤器"滤掉了(因为它们在很多书里都出现),但统计发现
它们**主要只跟某个特定知识点一起出现**,可能是被误伤的学科术语。

请判断每个词:它是**学科术语/知识点**(应该复活),还是**通用语**(该继续过滤)?
判断标准:**结合下面给出的原文例句**看——它在这些句子里是承载学科含义的概念,
还是只起连接/修饰作用的虚词?单独拿出来这个词,是不是某学科里有确定含义的概念?
——「特征值」「感染症」是术语;「形式」「例子」「重要」是通用语。
注意:哪怕它常和某术语同时出现,只要它本身是泛用词,就仍是通用语。

严格只输出 JSON 数组,不要任何解释:
[{"term":"原词","revive":true/false,"why":"不超过15字"}]

候选(每个词附它在书里的真实句子):
"""


def judge(cands):
    """AI 批量判断(严格 JSON)。返回 {term: (revive_bool, why)}。"""
    import ai_client
    lines = []
    for c in cands:
        head = (f'常与「{c["with"]}」同页({c["co_pages"]}/{c["pages"]} 页)' if c["with"]
                else "它本身就在焦点词榜上")
        lines.append(f'\n### {c["term"]}  —— {head}\n' +
                     "\n".join(f'  · {e}' for e in (c.get("examples") or ["(没取到例句)"])))
    raw = ai_client.ask(_PROMPT + "\n".join(lines),
                        claude_model="sonnet", claude_effort="low") or ""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return {}
    return {str(x.get("term")): (bool(x.get("revive")), str(x.get("why") or "")[:40])
            for x in arr if isinstance(x, dict) and x.get("term")}


def run(dry=False, limit=BATCH_MAX, min_conc=MIN_CONC):
    cands, err = find_candidates(limit=limit, min_conc=min_conc)
    if err:
        return {"ok": True, "note": err, "candidates": 0}
    if len(cands) < BATCH_MIN and not dry:
        return {"ok": True, "note": f"候选只有 {len(cands)} 个,不足 {BATCH_MIN},攒着下次一起判",
                "candidates": len(cands), "preview": [c["term"] for c in cands]}
    if dry:
        return {"ok": True, "dry": True, "candidates": len(cands), "detail": cands}
    verdict = judge(cands)
    if not verdict:
        return {"ok": False, "error": "AI 没给出可解析的结果(本轮不写盘,候选留到下次)"}
    revived = _load(REVIVED, {"terms": {}})
    log = _load(LOG, {})
    now = int(time.time())
    on, off = [], []
    for c in cands:
        v = verdict.get(c["term"])
        if v is None:
            continue                     # AI 漏判的不进冷却,下轮再试
        ok, why = v
        # ★这条日志同时是**训练集**:AI 判决=标签,统计量=特征。攒够几百条就能拟合一个
        #   判别器来代替/辅助阈值,把 AI 调用降到只判真正拿不准的边界样本(用户提的"反向训练")。
        log[c["term"]] = {"ts": now, "revive": ok, "why": why,
                          "feat": {"pmi": c["pmi"], "pages": c["pages"], "co_pages": c["co_pages"],
                                   "on_focus": c["on_focus"], "len": len(c["term"]),
                                   "ascii": c["term"].isascii(), "with": c["with"],
                                   "n_peers": len(c.get("peers") or [])}}
        (on if ok else off).append(c["term"])
        if ok:
            revived["terms"][c["term"]] = {"ts": now, "why": why, "with": c["with"]}
    revived["updated"] = now
    _save(REVIVED, revived)
    _save(LOG, log)
    return {"ok": True, "judged": len(cands), "revived": on, "kept_filtered": off,
            "total_revived": len(revived["terms"])}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=BATCH_MAX)
    ap.add_argument("--min-conc", type=float, default=MIN_CONC)
    a = ap.parse_args()
    r = run(dry=a.dry, limit=a.limit, min_conc=a.min_conc)
    if r.get("dry"):
        print(f"候选 {r['candidates']} 个(集中度降序):")
        for c in r["detail"]:
            tag = "★在焦点榜" if c["on_focus"] else ""
            print(f"  {c['term']:<14} PMI {c['pmi']:<6} 与「{c['with']}」同页 "
                  f"{c['co_pages']}/{c['pages']} {tag}")
            for e in (c.get("examples") or [])[:2]:
                print(f"        · {e[:90]}")
    else:
        print(json.dumps(r, ensure_ascii=False))
