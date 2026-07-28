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
POOL = ATT / "revive-pool.json"        # 候选池滞留计时
RUNS = ATT / "revive-runs.json"        # AI 调用令牌桶(promote/demote 共享)

TOP_FOCUS = 40         # 拿前 N 个焦点词当"知识点锚"
MIN_SUPPORT = 15       # 与某焦点词同页 ≥ 这么多页才算数。PMI 的经典毛病是**小样本高估**:
#                        低频锚点(如书名词 millennium)偶然共现 3~5 页就分数虚高,实测
#                        quickly/error/university 全是这么冒出来的;好候选共现都 ≥17。
MIN_ANCHOR_PAGES = 30  # 锚点自身出现页数下限(同上,排除只在几十页露面的书名/专名)
MIN_CONC = 1.0         # PMI 阈值(bit):≥1 = 共现比"互相独立"高一倍以上
BATCH_MIN = 6          # 攒够这么多候选才叫 AI(省调用)
DWELL_DAYS = 7         # ★候选池**滞留时间**(用户设计):必须在池里待够这么久才计入
#                        "攒够数量"。防的是偶然波动就触发判定——它是证据积累,不是防震荡。
MAX_FLIPS = 3          # ★翻转上限:同一个词被判过这么多次 → PIN 钉住,永不再参赛。
#                        有些词本质模糊(既是术语又是通用语),不钉住就会无限烧 AI。
AI_RUNS_PER_30D = 6    # ★令牌桶:promote 与 demote **共享**,防双边化后成本翻倍
BATCH_MAX = 25         # 一次最多判这么多
COOLDOWN_DAYS = 30     # 判过的词多久内不再参赛
MIN_BOOKS_FOR_DEMOTE = 8   # 降级方向的书库规模下限(小书库里"跨书普遍"是假信号)
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


def find_demote_candidates(limit=12):
    """**淘汰方向**(用户要的双边):筛选器放行的泛用词,靠什么发现?

    不能用复活的反面(高 PMI 共现)——那两条判据会同时成立、无法收敛(实测「集合/空间/证明」
    同时满足高 DF 与高 PMI)。漏网泛用语的特征是**跨领域普遍**,而系统里已经有这个量:
    焦点榜每个词都带 books(出现在多少本书)。判据:
      ① 榜上有名(说明真在污染画像)  ② 跨书数 ≥ 一半(普遍)  ③ 无「主动渠道」证据
    第③条是**否决式护栏**:只要用户高亮过/做过卡/查过体检,就绝不降级——单调于新鲜证据,
    因此可证明不会震荡(用户还在碰它,它就永远降不下去)。
    """
    import sqlite3 as _sq
    # 先检查整个书库规模。focus 为空往往正是因为书库/索引还没准备好，
    # 这时最重要的裁决仍是“样本不足，禁止降级”，不能让较次要的缺文件
    # 错误掩盖这个 fail-safe。
    try:
        with _sq.connect(f"file:{SEARCH_DB}?mode=ro", uri=True) as _books_db:
            n_books = int(
                _books_db.execute(
                    "SELECT COUNT(DISTINCT file) FROM pages_data"
                ).fetchone()[0]
                or 0
            )
    except Exception:
        n_books = 0
    if n_books < MIN_BOOKS_FOR_DEMOTE:
        return [], (
            f"书库规模不足({n_books} < {MIN_BOOKS_FOR_DEMOTE}),"
            "降级判据不可靠,本轮不降级"
        )
    prof = (_load(FOCUS, {}).get("top") or [])[:60]
    if not prof:
        return [], "focus.json 为空"
    log = _load(LOG, {})
    cut = time.time() - COOLDOWN_DAYS * 86400
    # 主动行为 = 用户**刻意**对这个词做过什么。lookup(查词)必须算——本用户 96% 的信号
    # 就是查词,把它排除等于所有词都"无主动证据",降级会误杀一片(实测 衛生/議事/調理師
    # 这些真术语全进了候选)。只有 read(整页停留,不针对具体词)不算。
    ACTIVE = ("highlight", "check", "note", "anki_lapse", "tool", "lookup", "qa")
    act = {}
    try:
        c = _sq.connect(str(config.PROJECT_DIR / "state" / "attention" / "events.db"))
        since = int(time.time() - 90 * 86400)
        for sf, in c.execute(
                "SELECT DISTINCT m.surface FROM event_mentions m JOIN events e ON e.src_key=m.src_key "
                "WHERE e.ts>=? AND e.channel IN (%s)" % ",".join("?" * len(ACTIVE)),
                [since] + list(ACTIVE)):
            act[str(sf)] = 1
        c.close()
    except Exception:
        return [], "读不到 events.db"
    out = []
    for r in prof:
        k = r.get("key") or ""
        if not k or float(log.get(k, {}).get("ts", 0)) >= cut:
            continue
        nb = int(r.get("books") or 0)      # focus 里 books 是**计数**不是列表
        if nb < max(2, (n_books + 1) // 2):
            continue                       # 不够普遍
        if act.get(k) or act.get(r.get("term") or "") or any(act.get(s) for s in (r.get("alt") or [])):
            continue                       # ★有主动渠道证据 → 一票否决,绝不降级
        out.append({"term": k, "surface": r.get("term") or k, "books": nb, "of_books": n_books,
                    "score": round(float(r.get("score") or 0), 1),
                    "direction": "demote"})
    out.sort(key=lambda x: (-x["books"], -x["score"]))
    return out[:limit], ""


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
    try:   # ★手工软表也要能参赛:它不在统计表里,此前复活赛根本扫不到(16 个真术语被永久活埋)
        import attention_profile as _AP
        words |= set(_AP._SOFT_GENERIC)
    except Exception:
        pass
    if not words:
        return [], "还没有停用词表"
    focus = [r.get("key") or "" for r in (_load(FOCUS, {}).get("top") or [])[:TOP_FOCUS]]
    focus = [f for f in focus if f and len(f) >= 2]
    if not focus:
        return [], "focus.json 为空"
    log = _load(LOG, {})
    def _cool_ok(w):
        e = log.get(w) or {}
        if e.get("pinned"):
            return False                                   # PIN:永不再参赛
        return time.time() - float(e.get("ts", 0)) >= float(e.get("cooldown_d", COOLDOWN_DAYS)) * 86400
    words = {w for w in words if _cool_ok(w)}              # 冷却(带指数退避)期内不再参赛
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


def _pool_dwell(cands, log):
    """★滞留闸(用户设计):候选首次出现时记时间戳,**待够 DWELL_DAYS 才算数**。
    返回 (够格的, 还在滞留的)。首次见到的写进池子就返回,不立刻参与计数。"""
    pool = _load(POOL, {})
    now = time.time()
    ripe, young = [], []
    for c in cands:
        t = c["term"]
        first = float(pool.get(t, {}).get("first", 0))
        if not first:
            pool[t] = {"first": now, "dir": c.get("direction", "revive")}
            young.append(t)
            continue
        (ripe if (now - first) >= DWELL_DAYS * 86400 else young).append(t)
    for t in list(pool):        # 已不再是候选的清出池子(证据消失就不该继续计时)
        if t not in {c["term"] for c in cands}:
            pool.pop(t, None)
    _save(POOL, pool)
    rs = set(ripe)
    return [c for c in cands if c["term"] in rs], young


def _budget_ok(log):
    """令牌桶:30 天内最多 AI_RUNS_PER_30D 批,promote/demote 共享(否则双边化成本翻倍)。"""
    runs = _load(RUNS, {"ts": []})
    cut = time.time() - 30 * 86400
    keep = [t for t in (runs.get("ts") or []) if t > cut]
    runs["ts"] = keep
    _save(RUNS, runs)
    return len(keep) < AI_RUNS_PER_30D, len(keep)


def _budget_spend():
    runs = _load(RUNS, {"ts": []})
    runs.setdefault("ts", []).append(time.time())
    _save(RUNS, runs)


def run(dry=False, limit=BATCH_MAX, min_conc=MIN_CONC, no_ai=False):
    log = _load(LOG, {})
    cands, err = find_candidates(limit=limit, min_conc=min_conc)
    if err:
        cands = []
    dem, derr = find_demote_candidates()        # ★双边:淘汰方向(数据不足时自动空)
    for d in dem:
        d.setdefault("examples", [])
    cands = cands + dem
    if not cands:
        return {"ok": True, "note": err or derr or "无候选", "candidates": 0}
    # 已被 PIN 的词永不再参赛(本质模糊的词,钉住免得无限烧 AI)
    cands = [c for c in cands if int(log.get(c["term"], {}).get("flips", 0)) < MAX_FLIPS]
    cands, young = _pool_dwell(cands, log)      # ★滞留闸
    if no_ai:
        # 控制面板子开关 ai_judge=false:滞留计时照常走(池子继续积累),只是不进 AI 裁决。
        # 重新打开后已滞留够的候选立刻可判——关开关不清进度。
        return {"ok": True, "note": "ai_judge 关闭:候选照常积累,不调用 AI",
                "candidates": len(cands), "in_dwell": len(young)}
    if not dry:
        okb, used = _budget_ok(log)
        if not okb:
            return {"ok": True, "note": f"本月 AI 预算已用完({used}/{AI_RUNS_PER_30D}),候选留到下月",
                    "candidates": len(cands)}
    if len(cands) < BATCH_MIN and not dry:
        return {"ok": True, "note": f"够滞留时间的候选只有 {len(cands)} 个(另有 {len(young)} 个"
                                    f"还在 {DWELL_DAYS} 天滞留期),不足 {BATCH_MIN},攒着下次一起判",
                "candidates": len(cands), "preview": [c["term"] for c in cands]}
    if dry:
        return {"ok": True, "dry": True, "candidates": len(cands), "detail": cands}
    verdict = judge(cands)
    if not verdict:
        return {"ok": False, "error": "AI 没给出可解析的结果(本轮不写盘,候选留到下次)"}
    _budget_spend()
    revived = _load(REVIVED, {"terms": {}})
    log = _load(LOG, {})
    now = int(time.time())
    on, off, pinned = [], [], []
    for c in cands:
        v = verdict.get(c["term"])
        if v is None:
            continue                     # AI 漏判的不进冷却,下轮再试
        ok, why = v
        _prev = log.get(c["term"], {})
        _flips = int(_prev.get("flips", 0)) + (1 if _prev and bool(_prev.get("revive")) != ok else 0)
        if not _prev:
            _flips = 1
        # ★这条日志同时是**训练集**:AI 判决=标签,统计量=特征。攒够几百条就能拟合一个
        #   判别器来代替/辅助阈值,把 AI 调用降到只判真正拿不准的边界样本(用户提的"反向训练")。
        log[c["term"]] = {"ts": now, "revive": ok, "why": why,
                          "feat": {"pmi": c["pmi"], "pages": c["pages"], "co_pages": c["co_pages"],
                                   "on_focus": c["on_focus"], "len": len(c["term"]),
                                   "ascii": c["term"].isascii(), "with": c["with"],
                                   "n_peers": len(c.get("peers") or [])}}
        log[c["term"]]["flips"] = _flips
        log[c["term"]]["cooldown_d"] = min(COOLDOWN_DAYS * (2 ** max(0, _flips - 1)), 180)  # 指数退避
        if _flips >= MAX_FLIPS:
            log[c["term"]]["pinned"] = True          # 反复横跳 = 本质模糊 → 钉住,不再参赛
            pinned.append(c["term"])
        (on if ok else off).append(c["term"])
        if ok:
            revived["terms"][c["term"]] = {"ts": now, "why": why, "with": c.get("with", "")}
        else:
            revived["terms"].pop(c["term"], None)     # ★双边:判为通用语则**撤销**此前的复活
    revived["updated"] = now
    _save(REVIVED, revived)
    _save(LOG, log)
    return {"ok": True, "judged": len(cands), "revived": on, "kept_filtered": off,
            "pinned": pinned, "total_revived": len(revived["terms"])}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=BATCH_MAX)
    ap.add_argument("--min-conc", type=float, default=MIN_CONC)
    ap.add_argument("--no-ai", action="store_true", help="只积累候选/滞留计时,不调用 AI(控制面板 ai_judge 开关)")
    a = ap.parse_args()
    r = run(dry=a.dry, limit=a.limit, min_conc=a.min_conc, no_ai=a.no_ai)
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
