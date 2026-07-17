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
import unicodedata
import time
from collections import Counter, defaultdict
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
W = {"lookup": 1.0, "highlight": 3.0, "qa": 2.0, "check": 4.0, "note": 5.0, "read": 0.5}
ARCHIVE_KEEP_D = 180        # 对话纯文本归档保留天数(用户设计:超过几个月再清)
DWELL_MIN_S = 15            # 「读过一页」判定阈值:同页同日累计停留 ≥15s(原始秒数在 dwell.jsonl,阈值改了可重放)

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
_BOOK_LANGS = None


def _book_lang(rel):
    """书的语言(state/pdf-book-langs.json,阅读器已维护)→ 分词路由的**权威依据**。
    ★为什么必须要它(2026-07-17 实锤 bug):纯汉字文本**无法靠字形区分中/日**——
      「人口動態統計」被 jieba 切成 人口/動態/統計、「公衆衛生学」→「衛生学」(丢字)、
      「一般相対性理論」→「一般/性理」(荒谬)。焦点榜里的「公衆」就是这么来的残骸。"""
    global _BOOK_LANGS
    if _BOOK_LANGS is None:
        try:
            _BOOK_LANGS = json.loads((STATE / "pdf-book-langs.json").read_text("utf-8"))
        except Exception:
            _BOOK_LANGS = {}
    return _BOOK_LANGS.get(rel or "") or []

# ── 归一键(用户设计:相同含义的不同写法在库里应是同一个词)────────────────────────
# ★分层落地(数据驱动,2026-07-17 实测):
#   L1 **字形层**(本函数,零风险):NFKC(全角/半角)+ 日本新字体→简体 + 常见繁→简。
#      「証明」(日语书)与「证明」(中文提问)归一;中日同形词(平均寿命/人口動態統計)本来就同形。
#   L2 语义层(proof↔证明↔証明)**暂缓**——实测:用户的学习域按语言分离(英文=数学/物理,
#      日文=料理师/应用情报),**现在没有可归的对象**;而 ECDICT 释义映射实测误连过半
#      (set↔结果、now↔刚才)。等真出现「同一知识点两种语言材料」再上多语 embedding + 双闸
#      (设计稿 §2.3)。届时只改本函数(key 机制已贯通聚合/查询/焦点),不动别处。
# term 保留原形(显示用组内最高频写法),key 只用于聚合 → 归一算法改版不影响历史数据。
# 字形归一的两级分工(别自己造轮子):
#   ① opencc t2s —— **繁体→简体**(几千字表,成熟;學習→学习、証明→证明、人口動態統計→人口动态统计)
#   ② 下表 —— 只补 opencc **覆盖不到的日本新字体特有字**(pip 版 opencc 没有 jp2t 配置)
_JP_ONLY = {
    "経": "经", "発": "发", "図": "图", "単": "单", "価": "价", "薬": "药", "覚": "觉", "読": "读",
    "売": "卖", "実": "实", "処": "处", "変": "变", "圧": "压", "壊": "坏", "専": "专", "帰": "归",
    "続": "续", "戦": "战", "総": "总", "検": "检", "査": "查", "県": "县", "労": "劳", "営": "营",
    "権": "权", "廃": "废", "齢": "龄", "児": "儿", "辺": "边", "駅": "驿", "鉄": "铁", "様": "样",
    "伝": "传", "収": "收", "帯": "带", "廷": "廷", "拠": "据", "揺": "摇", "浜": "滨", "涙": "泪",
    "税": "税", "粧": "妆", "緑": "绿", "縄": "绳", "臓": "脏", "蔵": "藏", "覧": "览", "訳": "译",
    "証": "证", "誉": "誉", "軽": "轻", "遅": "迟", "鉱": "矿", "顔": "颜", "駆": "驱", "髄": "髓",
    "済": "济", "増": "增", "隠": "隐", "駐": "驻", "触": "触", "圏": "圈",
}
_JP_TRANS = str.maketrans(_JP_ONLY)
_occ = None


def _t2s(t):
    global _occ
    if _occ is None:
        try:
            import opencc
            _occ = opencc.OpenCC("t2s")
        except Exception:
            _occ = False
    return _occ.convert(t) if _occ else t


def norm_key(term):
    """术语 → 归一键(聚合用)。字形层:NFKC + 日本新字体 + 繁→简。"""
    t = unicodedata.normalize("NFKC", str(term or "")).strip()
    if not t:
        return ""
    if _CJK.search(t):
        t = _t2s(t.translate(_JP_TRANS))
    return t.lower()


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
            pos2 = getattr(f, "pos2", None) or ""
            # 名詞 + **接頭辞/接尾辞(名詞的)** 一起进复合名词串(2026-07-17 实锤:
            #   只收名詞会把「公衆衛生**学**」→「公衆衛生」、「情報処理技術**者**試験」切两半——
            #   学/性/者/化/的/率 都是 接尾辞·名詞的,它们正是复合术语的构词部件)。
            if pos == "名詞" or (pos in ("接尾辞", "接頭辞") and (pos2 == "名詞的" or pos2 == "*")):
                run.append(str(tk.surface))
            else:
                _flush()
        _flush()
    except Exception:
        pass
    return out


def extract_terms(text, hint="", lang=None):
    """文本 → 术语列表(名词/名词短语粒度,非裸单字)。
    **语言路由**(2026-07-17 修):假名→ja;纯汉字→**看 lang(书语言)**,ja→fugashi / 否则 jieba;
    拉丁→en。lang 由调用方从 _book_lang(file_rel) 传入——纯汉字靠字形猜中/日必错。"""
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
            if lang and "ja" in lang and len(w) > 4:   # 长的纯汉字日语词:过日语分词器抽复合名词
                _t = [t for t in _ja_terms(w) if t not in _STOP_JA]
                if _t:
                    return _t[:3]
            return [w] if 1 < len(w) <= 8 else []
        return [t for t in _ja_terms(w) if t not in _STOP_JA][:4]
    out = []
    _is_ja = _KANA.search(text) or (lang and "ja" in lang)   # 有假名 → 必是日语;纯汉字 → 按书语言(不猜字形)
    if _is_ja:                                               # 日语:连续名词合并成复合名词(LRValue 候选生成的 lite 版)
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


RAW = ATT_DIR / "raw-events.jsonl"   # ★没有天然源文件的渠道的**账本**(append-only,永不删;--rebuild 也重导它)


def append_raw(channel, text, ts=None, file="", page=0, uid="", weight=None, hint="", lang=None, extra=None):
    """★统一事件入口(给**没有天然源文件**的渠道:眼镜 gaze / 工具调用 / 外部 App…)。
    写 append-only 账本 raw-events.jsonl,由 import_raw() 导进派生索引 —— 于是新渠道
    **既不用各造一套源日志格式,也不会被 --rebuild 删掉**。

    ⚠ 为什么不直接写 events.db(外部审查实锤的矛盾):events.db 是**可重建派生索引**
      (`--rebuild` 删库重导,分词/权重/归一算法改版必须能重算);直写它而不写账本的数据,
      重建时会永久消失。已有 5 个渠道天然带源(查词 jsonl/高亮 sidecar/对话+归档/dwell/检查报告),
      走各自导入器;没有源的渠道走这里。
    """
    rec = {"ts": int(ts or time.time()), "channel": str(channel), "text": str(text or "")[:2000],
           "file": file or "", "page": int(page or 0), "uid": str(uid or "")}
    for k, v in (("weight", weight), ("hint", hint), ("lang", lang)):
        if v is not None:
            rec[k] = v
    if isinstance(extra, dict):
        rec["extra"] = extra
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    with open(RAW, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def import_raw(c):
    """账本 → 派生索引(幂等增量;--rebuild 后也会重导,数据不丢)。"""
    if not RAW.exists():
        return 0
    n = 0
    for ln in RAW.read_text("utf-8").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if "/.sandbox/" in (d.get("file") or ""):
            continue
        n += add_event(c, d.get("ts") or 0, d.get("channel") or "raw", d.get("text") or "",
                       d.get("file") or "", d.get("page") or 0, uid=d.get("uid") or "",
                       weight=d.get("weight"), hint=d.get("hint") or "", lang=d.get("lang"))
    return n


def add_event(c, ts, channel, text, file="", page=0, uid="", weight=None, hint="", lang=None):
    """把一条事件写进**派生索引**(自动抽词、按 src_key 幂等)。
    ⚠ 这是内部函数:调用方必须是导入器(读的是 append-only 的源)。
      新渠道请用 **append_raw()**(写账本)—— 直写本表的数据 --rebuild 时会丢。"""
    key = hashlib.sha1(f"{channel}|{int(ts)}|{(text or '')[:80]}|{file}|{page}".encode()).hexdigest()[:20]
    terms = extract_terms(text, hint=hint, lang=(lang if lang is not None else _book_lang(file)))
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


def import_convo_archive(c):
    """已删除/被截断的对话文本归档(assistant._convo_archive 写)——对话没了,询问的痕迹还在。
    顺手裁剪 >ARCHIVE_KEEP_D 的旧行(这里是它唯一的清理点)。"""
    n = 0
    d = STATE / "assistant-convo-archive"
    if not d.exists():
        return 0
    cut = time.time() - ARCHIVE_KEEP_D * 86400
    for f in d.glob("*.jsonl"):
        keep, trimmed = [], False
        for ln in f.read_text("utf-8").splitlines():
            try:
                m = json.loads(ln)
            except Exception:
                continue
            if (m.get("ts") or 0) < cut:
                trimmed = True
                continue
            keep.append(ln)
            if m.get("role") != "user":
                continue
            fr = m.get("file_rel") or ""
            if "/.sandbox/" in fr:
                continue
            n += add_event(c, m.get("ts") or 0, "qa", m.get("content") or "", fr, m.get("page") or 0,
                           uid=f.stem)
        if trimmed:
            f.write_text("\n".join(keep) + ("\n" if keep else ""), "utf-8")
    return n


def _upage_text(rel, uid, limit=400):
    """自建页(虚拟页码 u_xxxx)的正文:从 reader-userpages sidecar 取 md/blocks 文字。"""
    p = STATE / "reader-userpages" / (hashlib.sha1((rel or "").encode("utf-8")).hexdigest()[:16] + ".json")
    try:
        for it in json.loads(p.read_text("utf-8")):
            if it.get("id") != uid:
                continue
            t = (it.get("title") or "") + " " + (it.get("md") or "")
            for b in (it.get("blocks") or []):
                t += " " + str(b.get("text") or b.get("label") or "")
            return t.strip()[:limit]
    except Exception:
        pass
    return ""


def _page_text(rel, page, limit=400):
    """从 pdf-char-cache 直接拼页文本(不依赖 webapp 进程;读过的页基本都有缓存)。"""
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    cands = sorted((STATE / "pdf-char-cache").glob("%s-p%d-*.json" % (sha, page)))
    for f in reversed(cands):
        try:
            chars = json.loads(f.read_text("utf-8")).get("chars") or []
            t = "".join(ch.get("c") or "" for ch in chars)[:limit]
            if t.strip():
                return t
        except Exception:
            continue
    return ""


def import_dwell(c):
    """阅读停留 → 「读过这页」事件。原始秒数由前端严谨采集(页可见+页图已渲染+60s内有交互才计秒,
    快翻/卡加载/挂机都不计);这里按 (file,page,日) 聚合,累计 ≥DWELL_MIN_S 才算读过,
    事件文本=该页正文前 400 字(从 char-cache 拼,离线),权重随停留时长小幅加成(封顶 2×)。"""
    f = ATT_DIR / "dwell.jsonl"
    if not f.exists():
        return 0
    agg = defaultdict(lambda: [0, 0])          # (file,page,day) → [secs, last_ts]
    for ln in f.read_text("utf-8").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        rel = d.get("file") or ""
        if not rel or "/.sandbox/" in rel:
            continue
        # 虚拟页码优先(自建页 uid 永不漂移);真实页用页码(靠 PAGE_ANCHOR_MIGRATIONS 迁移)
        k = (rel, (d.get("upage") or int(d.get("page") or 0)), int((d.get("ts") or 0) // 86400))
        agg[k][0] += min(600, int(d.get("secs") or 0))
        agg[k][1] = max(agg[k][1], int(d.get("ts") or 0))
    n = 0
    for (rel, page, day), (secs, ts) in agg.items():
        if secs < DWELL_MIN_S or not page:
            continue
        if isinstance(page, str):        # 自建页(虚拟页码):正文在 sidecar,不在 PDF 字符层
            txt = _upage_text(rel, page)
            page_no = 0
        else:
            txt = _page_text(rel, page)
            page_no = page
        if not txt:
            continue
        w = W["read"] * min(2.0, secs / 30.0)
        key = hashlib.sha1(f"read|{rel}|{page}|{day}".encode()).hexdigest()[:20]
        terms = extract_terms(txt)
        if not terms:
            continue
        c.execute("INSERT OR REPLACE INTO events(ts,channel,weight,text,terms,file,page,uid,src_key)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (ts, "read", w, txt[:500], json.dumps(terms[:12], ensure_ascii=False), rel, page_no, "", key))
        n += 1
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
    day_seen = defaultdict(int)                    # (key, day) → n(饱和)
    S_s, S_l = defaultdict(float), defaultdict(float)
    books = defaultdict(set)
    cnt7, cnt63 = defaultdict(int), defaultdict(int)
    refs = defaultdict(list)                        # key → [(ts, file, page)]
    all_books = set()
    surf = defaultdict(Counter)                     # key → 原形写法计数(显示用最高频那个)
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
        # ★归一键聚合(用户设计:同一个词的不同写法要算一个):key=字形归一,显示用最高频原形
        _uterms = {norm_key(t) or t: t for t in _uterms}
        for t, _sf in _uterms.items():
            surf[t][_sf] += 1
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
        _disp = surf[t].most_common(1)[0][0] if surf.get(t) else t   # 显示=最高频原形(书里怎么写就怎么显示)
        _alt = [x for x, _ in surf[t].most_common()[1:3]] if surf.get(t) else []
        out.append({"term": _disp, "key": t, "alt": _alt,
                    "score": round(score, 3), "burst": round(burst, 1),
                    "n7": cnt7[t], "books": len(books[t]),
                    "refs": [{"file": f, "page": p, "ts": ts0}
                             for ts0, f, p in sorted(refs[t], reverse=True)[:3]]})
    out.sort(key=lambda x: -x["score"])
    return out


# ── 灵活查询:任意时间窗 / 渠道 / 书 的焦点(AI 按语境调用;"昨天""上个月"都能答) ──────
_WHEN = {   # 自然语言时间窗 → (since_days_ago, until_days_ago);AI 传 when= 这些词即可
    "今天": (0, 0), "today": (0, 0), "昨天": (1, 1), "yesterday": (1, 1), "前天": (2, 2),
    "本周": (7, 0), "这周": (7, 0), "this_week": (7, 0), "上周": (14, 7), "last_week": (14, 7),
    "最近三天": (3, 0), "最近一周": (7, 0), "最近两周": (14, 0), "本月": (30, 0), "这个月": (30, 0),
    "上个月": (60, 30), "last_month": (60, 30), "最近一个月": (30, 0), "最近三个月": (90, 0),
    "最近半年": (180, 0), "全部": (36500, 0), "all": (36500, 0),
}


def parse_when(when="", days=None, since=None, until=None):
    """自然语言/参数 → (since_ts, until_ts)。优先级:显式 since/until > days > when > 默认近 7 天。
    ⚠ 日界按 JST 本地日切(用户在日本;'昨天'指昨天 00:00-24:00,不是 24 小时前)。"""
    now = time.time()
    if since or until:
        return (float(since or 0), float(until or now))
    if days:
        return (now - float(days) * 86400, now)
    w = (when or "").strip().lower().replace(" ", "")
    if w in _WHEN:
        a_d, b_d = _WHEN[w]
        if b_d == 0 and a_d <= 2:      # 今天/昨天/前天:按本地日界对齐
            import datetime as _dt
            d0 = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            return ((d0 - _dt.timedelta(days=a_d)).timestamp(),
                    (d0 - _dt.timedelta(days=b_d) + _dt.timedelta(days=1)).timestamp())
        if a_d == b_d:                 # 昨天/前天(单日)
            import datetime as _dt
            d0 = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            return ((d0 - _dt.timedelta(days=a_d)).timestamp(), (d0 - _dt.timedelta(days=a_d - 1)).timestamp())
        return (now - a_d * 86400, now - b_d * 86400)
    m = re.search(r"(\d+)\s*(天|日|days?)", w)
    if m:
        return (now - int(m.group(1)) * 86400, now)
    return (now - 7 * 86400, now)      # 默认近一周


def focus_window(when="", days=None, since=None, until=None, channels=None, file="", top=15, uid=""):
    """★任意时间窗的焦点词(AI 按语境调:"昨天在学什么""上个月的重点""这本书我关注了啥")。
    与 rebuild_profile 的区别:**窗内不做时间衰减**(窗口本身就是时间选择),纯 w/√N 加权 × IDF。
    返回 {ok, window:{since,until,label}, n_events, top:[{term,score,n,channels,refs}]}。"""
    s_ts, u_ts = parse_when(when, days, since, until)
    c = _db()
    q = "SELECT ts, channel, weight, terms, file, page FROM events WHERE ts>=? AND ts<?"
    args = [int(s_ts), int(u_ts)]
    if channels:
        q += " AND channel IN (%s)" % ",".join("?" * len(channels))
        args += list(channels)
    if file:
        q += " AND file=?"
        args.append(file)
    if uid:
        q += " AND (uid=? OR uid='')"
        args.append(str(uid))
    rows = c.execute(q, args).fetchall()
    # IDF 用**全库**书数(窗内书太少,IDF 会失真)
    all_books = {r[0] for r in c.execute("SELECT DISTINCT file FROM events WHERE file<>''")}
    tb = defaultdict(set)
    for r in c.execute("SELECT terms, file FROM events WHERE file<>''"):
        try:
            for t in set(json.loads(r[0])):
                tb[norm_key(t) or t].add(r[1])   # IDF 也按归一键(否则查不到书数=IDF 恒定)
        except Exception:
            pass
    sc, cnt, chs, refs = defaultdict(float), defaultdict(int), defaultdict(set), defaultdict(list)
    surf = defaultdict(Counter)
    # ★双权重(用户设计):最终权重 = 渠道基础权重 × **时间权重**。窗内时间权重 = 相对**窗口末端**的
    #   半衰期衰减(窗越长半衰期越长:窗口 1/3 处衰减到一半)——「上个月」里月末的比月初的更代表当时焦点,
    #   但不会像全局画像那样把整个窗口压平。窗 ≤2 天(今天/昨天)不衰减(一天内先后没有意义)。
    _span_d = max(0.5, (u_ts - s_ts) / 86400.0)
    _half = (_span_d / 3.0) if _span_d > 2 else 0.0
    for ts, ch, w, terms_j, f, page in rows:
        try:
            terms = set(json.loads(terms_j))
        except Exception:
            continue
        w = w / math.sqrt(max(1, len(terms)))
        if _half:
            w *= 2 ** (-max(0.0, (u_ts - ts) / 86400.0) / _half)
        for _sf in terms:
            t = norm_key(_sf) or _sf          # ★归一键聚合(同上)
            surf[t][_sf] += 1
            sc[t] += w
            cnt[t] += 1
            chs[t].add(ch)
            if f and len(refs[t]) < 6:
                refs[t].append((ts, f, page))
    nb = max(1, len(all_books))
    out = []
    for t, v in sc.items():
        idf = math.log((1 + nb) / (1 + len(tb.get(t, ())))) + 0.3
        _disp = surf[t].most_common(1)[0][0] if surf.get(t) else t
        out.append({"term": _disp, "key": t, "score": round(v * idf, 2), "n": cnt[t], "channels": sorted(chs[t]),
                    "refs": [{"file": f, "page": p} for _, f, p in sorted(refs[t], reverse=True)[:2]]})
    out.sort(key=lambda x: -x["score"])
    c.close()
    return {"ok": True, "n_events": len(rows),
            "window": {"since": int(s_ts), "until": int(u_ts),
                       "label": (when or (f"最近{days}天" if days else "最近7天"))},
            "top": out[:top]}


def focus_of_text(text, top=8):
    """★「当前对话/这段文字的焦点」:直接抽词 + 用全库 IDF 排序(无需事件表)。
    用于:助手看本轮对话说了什么 → 焦点术语 → 再去 focus_window/FTS 找相关材料。"""
    terms = extract_terms(text or "")
    if not terms:
        return {"ok": True, "top": []}
    c = _db()
    tb = defaultdict(set)
    all_books = {r[0] for r in c.execute("SELECT DISTINCT file FROM events WHERE file<>''")}
    for r in c.execute("SELECT terms, file FROM events WHERE file<>''"):
        try:
            for t in set(json.loads(r[0])):
                tb[norm_key(t) or t].add(r[1])
        except Exception:
            pass
    c.close()
    nb = max(1, len(all_books))
    cnt, surf = defaultdict(int), defaultdict(Counter)
    for _sf in terms:
        t = norm_key(_sf) or _sf
        cnt[t] += 1
        surf[t][_sf] += 1
    out = []
    for t, n in cnt.items():
        idf = math.log((1 + nb) / (1 + len(tb.get(t, ())))) + 0.3
        out.append({"term": surf[t].most_common(1)[0][0], "key": t, "score": round(n * idf, 2),
                    "n": n, "seen_in_books": len(tb.get(t, ()))})
    out.sort(key=lambda x: -x["score"])
    return {"ok": True, "top": out[:top]}


def run(rebuild=False):
    # 页锚迁移(插/删页)会改各源的 page → events.db 的 src_key 含 page,增量导入会产生重复 →
    # pdf_reader._pam_attention_db 落 .rebuild-needed 标记,这里看到就重导(实测 2.3s)。
    _dirty = ATT_DIR / ".rebuild-needed"
    if _dirty.exists():
        rebuild = True
        try:
            _dirty.unlink()
        except Exception:
            pass
    if rebuild and DB.exists():
        DB.unlink()
    c = _db()
    t0 = time.time()
    stats = {"lookup": import_lookups(c), "highlight": import_highlights(c),
             "qa": import_convo(c), "qa_arch": import_convo_archive(c),
             "check": import_checks(c), "read": import_dwell(c), "raw": import_raw(c)}
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
