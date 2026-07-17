#!/usr/bin/env python3
"""attention_profile.py — 注意力画像地基(设计:references/attention-kb-design.md)。

阶段 0+1:统一事件层 + 术语画像 + 学习焦点。
- **零侵入导入**:现有渠道都已有持久层(查词 jsonl/高亮 sidecar/助手对话/检查报告),
  每次跑增量导入(src_key 去重 + INSERT OR IGNORE,幂等,不需要游标);
  新渠道(将来的智能眼镜等)直接写 events 表即可插入。
- **画像全量重算**:个人规模(~几千事件)全量重算秒级完成——无状态无 bug,
  不搞流式累加器(那是百万级事件才需要的优化)。
- 公式(成熟方案,见设计稿 §1):score = (0.65·S_short + 0.35·S_long) × IDF_books,
  S = Σ w_i·sat_i·2^(-Δt/half_life),半衰期 短7d/长90d;
  sat = 同词同日第 n 次贡献 ×1/(1+0.3(n-1))(BM25 饱和的廉价等价,防单日刷量);
  IDF 按「出现过的不同书数」算,跨书泛词自动降权(比人工停用词表自适应)。
- burst(当前焦点):近 7d 原始次数 vs 前 56d 日均的倍数,>3 标 🔥。

跑法:quick_sync 每 15min 调一次;手动 `python3 attention_profile.py`;`--rebuild` 重导全量。
输出:state/attention/focus.json(insights 看板消费)+ events.db/terms 可查。
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PROJECT_DIR, VAULT_ROOT  # noqa: E402

ATT_DIR = Path(PROJECT_DIR) / "state" / "attention"
DB = ATT_DIR / "events.db"
FOCUS = ATT_DIR / "focus.json"
STATE = Path(PROJECT_DIR) / "state"

HALF_SHORT_D = 7.0     # 短期半衰期(天)
HALF_LONG_D = 90.0     # 长期半衰期
ALPHA = 0.65           # 短期占比
TOP_N = 40

# 渠道权重(设计稿 §5① 草案;调这里即可,画像全量重算立即生效)
W = {"lookup": 1.0, "highlight": 3.0, "qa": 2.0, "check": 4.0, "note": 5.0}

# ── 停用词(精简;IDF 跨书降权是主力,这里只挡最硬的) ──────────────────────────
_STOP_EN = set("""the a an and or but of to in on at for with from by as is are was were be been being
this that these those it its i you he she we they them his her my your our their not no yes do does did
have has had will would can could should may might must about into over under after before between
what which who whom how when where why all any some more most other than then so if because while
""".split())
_STOP_JA = set("こと もの ため よう それ これ あれ どれ ここ そこ とき ところ ほう 場合 とこ さん たち "
               "の は が を に で と も や から まで など なり つつ".split())
_STOP_ZH = set("这个 那个 什么 怎么 为什么 时候 问题 内容 东西 地方 方面 情况 时间 一个 一些 就是 可以 "
               "需要 觉得 知道 现在 然后 但是 因为 所以 如果 还是 或者 已经 应该 帮我 请问 一下 这里 那里 "
               "一张 一下 别再 就在 下面 上面 给你 给我 看看 告诉 解释 分析 这道 那道 这题 那题 这张 那张 "
               "再来 一遍 一次 全部 所有 以下 如下 以上 关于 对于 通过 进行 这样 那样 怎样 是什么 有没有 "
               "多少 几个 哪些 哪个 为何 是否 能否 可否 谢谢 好的 明白 继续 开始 结束 一句 一段 一点 部分".split())
_STOP_JA |= set("これ それ どれ ください お願い 教え 説明 分析 質問 問題 答え 全部 以下 上記 について "
                "という です ます でしょう ですか ますか なに どう".split())

_KANA = re.compile(r"[぀-ヿ]")
_CJK = re.compile(r"[一-鿿]")
_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z\-']{2,}")

_fugashi = None
_jieba = None


def _tag_ja():
    global _fugashi
    if _fugashi is None:
        import fugashi
        _fugashi = fugashi.Tagger()
    return _fugashi


def _cut_zh(text):
    global _jieba
    if _jieba is None:
        import jieba
        jieba.setLogLevel(60)
        _jieba = jieba
    return _jieba.lcut(text)


def _ja_terms(text):
    """日语:连续名詞合并成复合名词(termextract 候选生成的 lite 版)。助詞/助動詞/動詞断开 → 功能词自然出局。"""
    out, run = [], []
    def _flush():
        if run:
            t = "".join(run)
            if 2 <= len(t) <= 14 and t not in _STOP_JA and not t.isdigit():
                out.append(t)
        run.clear()
    try:
        for tk in _tag_ja()(text):
            f = tk.feature
            pos = getattr(f, "pos1", None) or str(f).split(",")[0]
            if pos == "名詞":
                run.append(str(tk.surface))
            else:
                _flush()
        _flush()
    except Exception:
        pass
    return out


def extract_terms(text, hint=""):
    """文本 → 术语列表(名词/名词短语粒度,非裸单字)。语言路由:假名→ja;CJK→zh;拉丁→en。
    hint='word' 时(查词事件)text 本身就是术语,原样返回。"""
    text = (text or "").strip()
    if not text:
        return []
    if hint == "word":
        # 查词日志的 word 不一定干净(实测:用户点到功能词「という/いう」、点到整句「全問未回答です」)。
        # 规则:ASCII→原样;纯汉字→原样(日/中汉字词都是有效术语,别切碎「議事」);
        #      含假名→过日语词性(功能词/动词自然被过滤掉,长短语抽出其中名词)。
        w = text.strip()
        if w.isascii():
            lw = w.lower()
            return [lw] if len(lw) > 1 and lw not in _STOP_EN else []
        if not _KANA.search(w):
            return [w] if 1 < len(w) <= 8 else []
        return [t for t in _ja_terms(w) if t not in _STOP_JA][:4]
    out = []
    if _KANA.search(text):                                   # 日语:连续名词合并成复合名词(LRValue 候选生成的 lite 版)
        out += _ja_terms(text)
    elif _CJK.search(text):                                  # 中文:jieba,留 ≥2 字词
        try:
            for w in _cut_zh(text):
                w = w.strip()
                if len(w) >= 2 and w not in _STOP_ZH and not w.isdigit() and _CJK.search(w):
                    out.append(w)
        except Exception:
            pass
    for w in _EN_TOKEN.findall(text):                        # 拉丁词(混排也抽)
        lw = w.lower()
        if lw not in _STOP_EN:
            out.append(lw)
    return out[:60]


# ── 事件层 ─────────────────────────────────────────────────────────────────────
def _db():
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, ts INTEGER, channel TEXT, weight REAL,
        text TEXT, terms TEXT, file TEXT, page INTEGER, uid TEXT, src_key TEXT UNIQUE)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts)")
    return c


def add_event(c, ts, channel, text, file="", page=0, uid="", weight=None, hint=""):
    """新渠道(眼镜/自定义)也走这里:自动抽词、按 src_key 幂等。"""
    key = hashlib.sha1(f"{channel}|{int(ts)}|{(text or '')[:80]}|{file}|{page}".encode()).hexdigest()[:20]
    terms = extract_terms(text, hint=hint)
    if not terms:
        return 0
    cur = c.execute("INSERT OR IGNORE INTO events(ts,channel,weight,text,terms,file,page,uid,src_key)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(ts), channel, float(weight if weight is not None else W.get(channel, 1.0)),
                     (text or "")[:500], json.dumps(terms[:12], ensure_ascii=False), file or "", int(page or 0),
                     str(uid or ""), key))
    return cur.rowcount


# ── 导入器(全部幂等增量) ──────────────────────────────────────────────────────
def _rel_by_sha():
    """sidecar 文件名(sha1(rel))→ rel 反查表(pdf+epub 全 vault)。"""
    m = {}
    for p in Path(VAULT_ROOT).rglob("*"):
        if p.suffix.lower() in (".pdf", ".epub") and p.is_file():
            rel = p.relative_to(VAULT_ROOT).as_posix()
            if "/.sandbox/" in rel:
                continue
            m[hashlib.sha1(rel.encode()).hexdigest()] = rel
    return m


def import_lookups(c):
    f = STATE / "vocab-lookups.jsonl"
    n = 0
    if not f.exists():
        return 0
    for ln in f.read_text("utf-8").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        w = d.get("lemma") or d.get("word") or ""
        n += add_event(c, d.get("ts") or 0, "lookup", w, d.get("pdf") or "", d.get("page") or 0, hint="word")
    return n


def import_highlights(c):
    n = 0
    sha2rel = _rel_by_sha()
    for dname in ("pdf-highlights", "epub-highlights", "html-highlights"):
        d = STATE / dname
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            rel = sha2rel.get(f.stem, "")
            if not rel and "/.sandbox/" in (rel or ""):
                continue
            try:
                data = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            hls = data.get("highlights") if isinstance(data, dict) else data   # 两种形态:{highlights:[…]} 或直接 […](epub/html)
            for h in (hls or []):
                if not isinstance(h, dict):
                    continue
                txt = " ".join(x for x in (h.get("text"), h.get("sentence"), h.get("note")) if x)
                n += add_event(c, h.get("time") or 0, "highlight", txt, rel, h.get("page") or 0)
    return n


def import_convo(c):
    n = 0
    d = STATE / "assistant-convo"
    if not d.exists():
        return 0
    for f in d.glob("*.json"):
        if ".summary." in f.name:
            continue
        try:
            msgs = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if not isinstance(msgs, list):
            continue
        for m in msgs:
            if m.get("role") != "user":
                continue          # v1 只收用户提问(AI 回答按用户构想是低权渠道,权重待讨论,先不入)
            fr = m.get("file_rel") or ""
            if "/.sandbox/" in fr:
                continue
            n += add_event(c, m.get("ts") or 0, "qa", m.get("content") or "", fr, m.get("page") or 0,
                           uid=f.stem)
    return n


def import_checks(c):
    n = 0
    d = STATE / "reader-check-reports"
    if not d.exists():
        return 0
    for f in d.glob("*.json"):
        try:
            lst = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        for r in lst if isinstance(lst, list) else []:
            if r.get("sandbox") or "/.sandbox/" in (r.get("file") or ""):
                continue
            # 只用纸标题(如「公衆衛生小テスト」)——report 正文是判分叙述/系统模板,不是学习内容
            n += add_event(c, r.get("ts") or 0, "check", r.get("name") or "", r.get("file") or "",
                           r.get("src_page") or 0, uid=f.stem)
    return n


# ── 画像 + 焦点(全量重算) ─────────────────────────────────────────────────────
def rebuild_profile(c, now=None):
    now = now or time.time()
    rows = c.execute("SELECT ts, channel, weight, terms, file, page FROM events WHERE ts > 0").fetchall()
    day_seen = defaultdict(int)                    # (term, day) → n(饱和)
    S_s, S_l = defaultdict(float), defaultdict(float)
    books = defaultdict(set)
    cnt7, cnt63 = defaultdict(int), defaultdict(int)
    refs = defaultdict(list)                        # term → [(ts, file, page)]
    all_books = set()
    for ts, ch, w, terms_j, file, page in rows:
        try:
            terms = json.loads(terms_j)
        except Exception:
            continue
        dt_d = max(0.0, (now - ts) / 86400.0)
        day = int(ts // 86400)
        if file:
            all_books.add(file)
        _uterms = set(terms)
        w = w / math.sqrt(max(1, len(_uterms)))   # 事件内稀释:一句话提 N 个词,每词注意力 = w/√N(防长报告/长提问轰炸)
        for t in _uterms:
            day_seen[(t, day)] += 1
            sat = 1.0 / (1.0 + 0.3 * (day_seen[(t, day)] - 1))     # 同日重复贡献递减(防刷量)
            S_s[t] += w * sat * (2 ** (-dt_d / HALF_SHORT_D))
            S_l[t] += w * sat * (2 ** (-dt_d / HALF_LONG_D))
            if file:
                books[t].add(file)
            if dt_d <= 7:
                cnt7[t] += 1
            elif dt_d <= 63:
                cnt63[t] += 1
            if file and len(refs[t]) < 12:
                refs[t].append((ts, file, page))
    nb = max(1, len(all_books))
    out = []
    for t, ss in S_s.items():
        idf = math.log((1 + nb) / (1 + len(books[t]))) + 0.3        # 跨书泛词降权(+0.3 底,单书词不至于无穷大优势)
        score = (ALPHA * ss + (1 - ALPHA) * S_l[t]) * idf
        base = cnt63[t] / 8.0                                        # 前 8 周周均
        burst = (cnt7[t] / max(0.5, base)) if cnt7[t] >= 3 else 0.0
        out.append({"term": t, "score": round(score, 3), "burst": round(burst, 1),
                    "n7": cnt7[t], "books": len(books[t]),
                    "refs": [{"file": f, "page": p, "ts": ts0}
                             for ts0, f, p in sorted(refs[t], reverse=True)[:3]]})
    out.sort(key=lambda x: -x["score"])
    return out


def run(rebuild=False):
    if rebuild and DB.exists():
        DB.unlink()
    c = _db()
    t0 = time.time()
    stats = {"lookup": import_lookups(c), "highlight": import_highlights(c),
             "qa": import_convo(c), "check": import_checks(c)}
    c.commit()
    prof = rebuild_profile(c)
    total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    FOCUS.write_text(json.dumps({
        "updated": int(time.time()), "n_events": total, "imported_now": stats,
        "took_s": round(time.time() - t0, 2),
        "top": prof[:TOP_N],
        "burst": sorted([x for x in prof[:200] if x["burst"] >= 3], key=lambda x: -x["burst"])[:10],
    }, ensure_ascii=False, indent=1), "utf-8")
    c.close()
    return stats, total


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="删库重导全量(分词算法改版后用)")
    a = ap.parse_args()
    st, total = run(rebuild=a.rebuild)
    print("imported:", st, "| total events:", total, "| focus →", FOCUS)
