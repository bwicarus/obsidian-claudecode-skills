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

# ★路径自愈(实锤:webapp/.env 只设了 CLAUDE_PROJECT、**没设 OBSIDIAN_VAULT** → webapp 进程里
#   VAULT_ROOT 会退回 config.py 的 Windows 默认 `C:\obsidian`,笔记渠道静默 0 条、账本可能写错地方)。
#   本模块被 webapp / voice / 脚本 三种进程 import,不能靠各自的环境变量猜 —— 默认值不存在就按
#   **本文件的位置**推回项目根(scripts/ 的上一级),vault 按同级 obsidian/ 兜底。
if not Path(PROJECT_DIR).exists():
    PROJECT_DIR = Path(__file__).resolve().parent.parent
if not Path(VAULT_ROOT).exists():
    _v = Path(PROJECT_DIR).parent / "obsidian"
    if _v.exists():
        VAULT_ROOT = _v

ATT_DIR = Path(PROJECT_DIR) / "state" / "attention"
DB = ATT_DIR / "events.db"
FOCUS = ATT_DIR / "focus.json"
STATE = Path(PROJECT_DIR) / "state"

# ★焦点是**下游系统消费的关键数据**(不是给人看的榜)——三条硬要求(用户 2026-07-17 定调):
#   可靠性:不做不可逆截断(源可重算的才允许截);账本(无上游)一律全文;抽取器版本可追。
#   及时性:**读时保证新鲜**(_ensure_fresh:源 mtime 变了就增量导入)——不靠 15min timer。
#   可回溯性:每个焦点词带证据链(evidence 事件 id + 分数构成 by_channel),explain() 可查。
EXTRACTOR_VER = 7      # 抽取器版本(改分词/归一/权重算法就 +1;events.xver 记录,可查哪些事件是旧版抽的)。v5:笔记去 Excalidraw/压缩元数据 + IDF 只数真书(审查 #6)
TERMS_MAX = 40         # 单事件术语上限(原 12 → 实测 4 条事件撞顶=真丢词;40 足够,存的是 JSON 数组)
TEXT_MAX = 4000        # 派生索引里留的原文(源还在,截了也能重算;账本不截,见 append_raw)

HALF_SHORT_D = 7.0     # 短期半衰期(天)
HALF_LONG_D = 90.0     # 长期半衰期
ALPHA = 0.65           # 短期占比
TOP_N = 40

# 渠道权重(设计稿 §5① 草案;调这里即可,画像全量重算立即生效)
W = {"lookup": 1.0, "highlight": 3.0, "qa": 2.0, "check": 4.0, "note": 5.0, "read": 0.5,
     "anki_lapse": 2.0,   # Anki 答错 = 薄弱信号(只收 lapse,不收全部复习:52174 条会淹没一切)
     "tool": 2.0}         # 主动查找(搜书/联网/跨库)的**查询词** = 强意图信号
ANKI_DB = Path("/home/bwicarus/.local/share/Anki2/User 1/collection.anki2")
ANKI_LAPSE_DAYS = 180     # 只导近 N 天的答错(历史 18040 条 lapse 是积累,不是当前焦点)
NOTE_MAX_CHARS = 600      # 笔记事件取标题+首段(全文会把一篇笔记的所有词都拉进画像)
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


CONCEPTS = ATT_DIR / "concepts.json"   # L2 语义归一:{别名(norm后): 概念键} —— AI 判定的结果,永久缓存
_CONCEPTS = None


def _concepts():
    global _CONCEPTS
    if _CONCEPTS is None:
        try:
            _CONCEPTS = json.loads(CONCEPTS.read_text("utf-8")).get("alias", {})
        except Exception:
            _CONCEPTS = {}
    return _CONCEPTS


def norm_key(term):
    """术语 → 归一键(聚合用)。
    L1 字形层:NFKC + 日本新字体 + 繁→简(証明→证明);
    L2 语义层:concepts.json 的别名表(proof / 証明 / 证明 → 同一概念键)—— 由 build_concepts()
    夜间用 AI 判定后永久缓存;查表 O(1),不在热路径调 AI。"""
    t = unicodedata.normalize("NFKC", str(term or "")).strip()
    if not t:
        return ""
    if _CJK.search(t):
        t = _t2s(t.translate(_JP_TRANS))
    t = t.lower()
    al = _concepts()
    if t in al:
        return al[t]                       # L2:命中别名表 → 概念键
    if " " in t and t.isascii():           # ★英文词组:每个部件都有中文别名 → 拼成中文长词组
        parts = t.split()                  #   (vector space → 向量+空间 = 向量空间;用户的「长词组跨语言对应」)
        zh = [al.get(w) for w in parts]
        if all(zh) and all(not x.isascii() for x in zh):
            return "".join(zh)
    return t                               # 没命中 → 字形键(原样)


DOMAIN_DICT = ATT_DIR / "domain-terms.json"   # 领域词典(自动长出来的;缓存,可随时删)
_DOMAIN = None


def build_domain_dict():
    """★领域词典 —— **从系统自己的高质量数据长出来**(不是外部词典)。

    为什么必须要(2026-07-17 用户实锤 + 数据佐证):通用分词器不认领域术语 ——
    「向量空间」原文出现 11 次却抽成术语 **0 次**(jieba 切成 向量+空间)、「子空间」18 次也是 0;
    英文侧 473 个术语里**多词词组 0 个**(正则只抽单词)。

    来源(按质量排序,都是**人工/AI 已经确认过**的术语):
      ① **KG 节点名**(最好):1164 个,如 LADR 的「向量空间」「向量空间的定义」、
         EGIU 的「Present perfect」—— 本来就是「知识点」的名字;
      ② 书目录章节名(book_toc);
      ③ 用户查过的词(vocab-lookups 的 lemma:他亲手点的 = 他认的词)。
    ⚠ **不用** Anki 卡正面(实测是问句「什么是有限维向量空间?」不是术语)、
      不用笔记标题(实测混 Excalidraw 元数据噪声)。
    """
    terms = {}

    def _add(t, src):
        t = str(t or "").strip()
        if not (2 <= len(t) <= 20) or t.startswith(("#", "http")):
            return
        # ⚠ 实测:lookup 源会带进 would/even/by/the(用户真点过这些词查释义)+ F^n/F^S(公式残渣)
        #   —— 它们进领域词典 = 分词时被当术语最长匹配,榜单立刻被虚词污染。
        if t.lower() in _STOP_EN or t in _STOP_ZH or t in _STOP_JA:
            return
        if t.isascii() and (len(t) < 5 or not t.isalpha()):
            return                      # 英文短词/含符号(F^n)一律不进:英文单词由正则抽,词典只负责**词组**
        terms.setdefault(t, src)

    for f in (Path(PROJECT_DIR) / "knowledge_graph").glob("*.json"):   # ① KG 节点名
        if ".bak." in f.name:
            continue
        try:
            for n in (json.loads(f.read_text("utf-8")).get("nodes") or []):
                _add(n.get("name"), "kg")
        except Exception:
            continue
    try:                                                               # ② 书目录章节名
        for f in (Path(PROJECT_DIR) / "state" / "book-toc").glob("*.json"):
            d = json.loads(f.read_text("utf-8"))
            for it in (d.get("toc") or d.get("items") or []):
                if isinstance(it, dict):
                    _add(it.get("title") or it.get("name"), "toc")
    except Exception:
        pass
    try:                                                               # ③ 用户亲手查过的词
        for ln in (STATE / "vocab-lookups.jsonl").read_text("utf-8").splitlines()[-3000:]:
            d = json.loads(ln)
            w = d.get("lemma") or d.get("word")
            if w and len(str(w)) >= 2:
                _add(w, "lookup")
    except Exception:
        pass
    DOMAIN_DICT.write_text(json.dumps({"terms": terms, "updated": int(time.time()), "n": len(terms)},
                                      ensure_ascii=False, indent=1), "utf-8")
    global _DOMAIN
    _DOMAIN = None
    return {"ok": True, "n": len(terms), "by_src": dict(Counter(terms.values()))}


def _domain():
    """领域词典 → {原词} + 喂给 jieba(中文侧靠它切出「向量空间」)。"""
    global _DOMAIN
    if _DOMAIN is None:
        try:
            _DOMAIN = set(json.loads(DOMAIN_DICT.read_text("utf-8")).get("terms", {}))
        except Exception:
            _DOMAIN = set()
        if _DOMAIN:
            try:
                import jieba
                jieba.setLogLevel(60)
                for t in _DOMAIN:
                    if _CJK.search(t) and not _KANA.search(t):
                        jieba.add_word(t, freq=10000)      # 中文侧:领域词优先切
            except Exception:
                pass
    return _DOMAIN


def _combo_en(text):
    """★英文词组 = **已归一概念词在原文里真的相邻**(用户的洞察:「在文中没有实际前后连接就毫无关系」)。
    领域词典里没有英文词组(KG 节点是中文名、lookup 全是单词)→ 靠组合律现场发现:
    concepts.json 已有 vector/space → 原文出现 `vector space` 相邻 → 录长词组「vector space」。
    只组 2 元(3 元噪声大);两个词都必须是**已知概念**(否则 of/the 也会被组进来)。"""
    # 已知英文概念词 = concepts 的英文键(vector) ∪ 领域词典的英文词(KG 里的英文术语)∪
    #   ECDICT 里"有中文对应且我用过"的词 —— 用最宽的已知集,组合律才不因某轮 AI 漏判而失效。
    kn = {k for k in _concepts() if k.isascii() and len(k) >= 3}
    kn |= {t.lower() for t in _domain() if t.isascii() and t.isalpha() and len(t) >= 3}
    # 常见数学/CS 构词词(它们本身可能不进焦点,但作为词组部件必须认)
    kn |= {"vector", "space", "linear", "inner", "product", "sub", "subspace", "dot",
           "scalar", "matrix", "field", "map", "basis", "dimension", "kernel", "image",
           "data", "structure", "protocol", "system", "network", "function", "set"}
    if not text:
        return []
    toks = re.findall(r"[a-z][a-z\-']+", text.lower())
    out = []
    for i in range(len(toks) - 1):
        if toks[i] in kn and toks[i + 1] in kn and toks[i] not in _STOP_EN and toks[i + 1] not in _STOP_EN:
            out.append(toks[i] + " " + toks[i + 1])
    return out[:6]


def _domain_match(text):
    """★原文里的领域术语(最长匹配)—— 英文词组靠它(「vector space」正则抽不出来),
    中文/日语的长词组也靠它兜底。用户的洞察:**在原文里真的连在一起**才算词组。"""
    d = _domain()
    if not d or not text:
        return []
    low = text.lower()
    out = []
    for t in d:
        if len(t) < 3:
            continue
        if (t.lower() in low) if not t.isascii() else _re_word(t.lower(), low):
            out.append(t)
    # 去掉被更长术语包含的(用户点的「包含问题」:录长词组比录短词有意义)
    out.sort(key=len, reverse=True)
    keep = []
    for t in out:
        if not any(t != k and t.lower() in k.lower() for k in keep):
            keep.append(t)
    return keep[:8]


def _re_word(t, low):
    """英文按词边界匹配(防 'set' 命中 'settle')。"""
    return re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low) is not None


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


def _dedup_nest(terms, text=""):
    """★去重 + **长词组吃掉被它包含的短词**(用户的「词组包含问题」)。
    原文里既然是「向量空间的定义」,就不该同时再记「向量空间」「向量」「空间」——
    否则同一处注意力被计四遍、榜单被同一概念的碎片占满。
    ⚠ 只在**同一段文本内**做(跨事件不做):同一句里出现长词组 = 这次的注意力就是它;
      别的地方单独出现「向量」时,那是另一次注意力,照记不误。"""
    from collections import Counter
    cnt = Counter(str(t).lower() for t in terms if str(t).strip())
    forms = {}
    for t in terms:                                                   # 显示原形(首个胜出)
        forms.setdefault(str(t).lower(), t)
    tl_text = (text or "").lower()                                    # ★用**原文**数独立出现(不靠 span 也能判:审查 #6)
    out, kept = [], []
    for tl in sorted(cnt, key=len, reverse=True):                     # 长的先占位
        # 原文里这个短词被已保留的更长术语"包住"的次数;occ - eaten = 独立出现次数
        if tl_text:
            _cont = [k for k in kept if tl in k and tl != k]
            _maximal = [k for k in _cont if not any(k in k2 and k != k2 for k2 in _cont)]   # 极大包含词(不被其它已保留长词再包含)→ 避免嵌套双重扣减(审查 #4)
            eaten = sum(tl_text.count(k) for k in _maximal)
            occ = tl_text.count(tl)
        else:
            eaten = sum(cnt[k] for k in kept if tl in k and tl != k)
            occ = cnt[tl]
        if occ - eaten > 0:                                          # 有独立出现 → 保留(第二句独立的"向量"不再被吞)
            out.append(forms[tl])
        kept.append(tl)
    return out


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
    if hint == "word":   # 查词事件(text 本身就是一个词);hint="sel"=选中的原文,走正常抽词
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
    # ★① 领域词典最长匹配(原文里真的连在一起的长词组 —— 用户的洞察):
    #    「向量空间」「vector space」「公衆衛生学」这类,通用分词器切碎、正则抽不出。
    out = list(_domain_match(text)) + _combo_en(text)
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
    return _dedup_nest(out, text)[:60]


# ── 事件层 ─────────────────────────────────────────────────────────────────────
def _db():
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, ts INTEGER, channel TEXT, weight REAL,
        text TEXT, terms TEXT, file TEXT, page INTEGER, uid TEXT, src_key TEXT UNIQUE,
        xver INTEGER DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts)")
    try:                      # 老库补列(可回溯性:哪些事件是旧抽取器抽的)
        c.execute("ALTER TABLE events ADD COLUMN xver INTEGER DEFAULT 0")
    except Exception:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
    return c


RAW = ATT_DIR / "raw-events.jsonl"   # ★没有天然源文件的渠道的**账本**(append-only,永不删;--rebuild 也重导它)


LEDGER_SCHEMA = 2   # 账本行协议版本(加字段就 +1;读侧按此兼容老行)


def append_raw(channel, text, ts=None, file="", page=0, uid="", weight=None, hint="", lang=None,
               extra=None, actor="user", session_id=None, turn_id=None, call_id=None, anchor=None):
    """★统一事件入口(给**没有天然源文件**的渠道:眼镜 gaze / 工具调用 / 外部 App…)。
    写 append-only 账本 raw-events.jsonl,由 import_raw() 导进派生索引 —— 于是新渠道
    **既不用各造一套源日志格式,也不会被 --rebuild 删掉**。

    ⚠ 为什么不直接写 events.db(外部审查实锤的矛盾):events.db 是**可重建派生索引**
      (`--rebuild` 删库重导,分词/权重/归一算法改版必须能重算);直写它而不写账本的数据,
      重建时会永久消失。已有 5 个渠道天然带源(查词 jsonl/高亮 sidecar/对话+归档/dwell/检查报告),
      走各自导入器;没有源的渠道走这里。
    """
    # ⚠ 账本**不截原文**:它没有上游,截了就是永久损失(派生索引可以截,因为源还在能重算)
    # 字段协议(采纳外部审查建议:**协议改起来贵、存储换起来便宜** → 字段一次定对,载体先用 jsonl):
    #   v/ts/channel/text/file/page/uid  = 基本;actor = user|ai|system(谁做的);
    #   session_id/turn_id/call_id       = 追溯锚(哪轮对话/哪次工具调用产生的);
    #   anchor/extra                     = 结构化补充(选区/坐标/任意 payload)
    rec = {"v": LEDGER_SCHEMA, "ts": int(ts or time.time()), "channel": str(channel),
           "actor": str(actor or "user"), "text": str(text or ""),
           "file": file or "", "page": int(page or 0), "uid": str(uid or "")}
    for k, v in (("weight", weight), ("hint", hint), ("lang", lang),
                 ("session_id", session_id), ("turn_id", turn_id), ("call_id", call_id)):
        if v is not None and v != "":
            rec[k] = v
    for k, v in (("anchor", anchor), ("extra", extra)):
        if isinstance(v, dict) and v:
            rec[k] = v
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    # ★flock:webapp / voice / 后台脚本都可能同时写(外部审查点出的多进程写入)。
    #   POSIX 只保证 <PIPE_BUF(4096) 的 append 原子 —— 而账本**故意不截原文**,大行会超。
    #   实测「无锁并发写 6000B 行」这次没坏,但那是运气不是保证 → 加锁(开销 ~微秒)。
    with open(RAW, "a", encoding="utf-8") as f:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        f.write(line)
        f.flush()
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
        # v1 行没有 v/actor 字段 —— 照收(账本协议向后兼容,老数据不丢)
        n += add_event(c, d.get("ts") or 0, d.get("channel") or "raw", d.get("text") or "",
                       d.get("file") or "", d.get("page") or 0, uid=d.get("uid") or "",
                       weight=d.get("weight"), hint=d.get("hint") or "", lang=d.get("lang"))
    return n


def add_event(c, ts, channel, text, file="", page=0, uid="", weight=None, hint="", lang=None):
    """把一条事件写进**派生索引**(自动抽词、按 src_key 幂等)。
    ⚠ 这是内部函数:调用方必须是导入器(读的是 append-only 的源)。
      新渠道请用 **append_raw()**(写账本)—— 直写本表的数据 --rebuild 时会丢。"""
    key = hashlib.sha1(f"{channel}|{int(ts)}|{hint}|{(text or '')[:80]}|{file}|{page}".encode()).hexdigest()[:20]
    # ★及时性:同版本已存在 → 直接跳过(**别白跑分词**,它是增量导入的瓶颈)。
    #   抽取器升版(EXTRACTOR_VER)时旧行会被重抽(下面 REPLACE),所以升版即自动全量刷新。
    row = c.execute("SELECT xver FROM events WHERE src_key=?", (key,)).fetchone()
    if row and int(row[0] or 0) >= EXTRACTOR_VER:
        return 0
    terms = extract_terms(text, hint=hint, lang=(lang if lang is not None else _book_lang(file)))
    if not terms:
        return 0
    cur = c.execute("INSERT OR REPLACE INTO events(id,ts,channel,weight,text,terms,file,page,uid,src_key,xver)"
                    " VALUES((SELECT id FROM events WHERE src_key=?),?,?,?,?,?,?,?,?,?,?)",
                    (key, int(ts), channel, float(weight if weight is not None else W.get(channel, 1.0)),
                     (text or "")[:TEXT_MAX], json.dumps(terms[:TERMS_MAX], ensure_ascii=False),
                     file or "", int(page or 0), str(uid or ""), key, EXTRACTOR_VER))
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
            # ★问题 = 问句 + **带入的上下文**(用户实锤:选中一段原文提问,选中内容整个被丢了)。
            #   两者语言常常不同(中文问 + 日语原文)→ **分开抽词**:问句 lang=[] 按字形判,
            #   选中的原文 lang=None 按书语言(它是书的内容)。
            n += add_event(c, m.get("ts") or 0, "qa", m.get("content") or "", fr, m.get("page") or 0,
                           uid=f.stem, lang=[])
            _sel = str(m.get("selection") or "").strip()
            if _sel:
                n += add_event(c, m.get("ts") or 0, "qa", _sel[:1000], fr, m.get("page") or 0,
                               uid=f.stem, hint="sel")   # lang=None → 按书语言(选中的是原文)
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
            # ★问题 = 问句 + **带入的上下文**(用户实锤:选中一段原文提问,选中内容整个被丢了)。
            #   两者语言常常不同(中文问 + 日语原文)→ **分开抽词**:问句 lang=[] 按字形判,
            #   选中的原文 lang=None 按书语言(它是书的内容)。
            n += add_event(c, m.get("ts") or 0, "qa", m.get("content") or "", fr, m.get("page") or 0,
                           uid=f.stem, lang=[])
            _sel = str(m.get("selection") or "").strip()
            if _sel:
                n += add_event(c, m.get("ts") or 0, "qa", _sel[:1000], fr, m.get("page") or 0,
                               uid=f.stem, hint="sel")   # lang=None → 按书语言(选中的是原文)
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
    # cache miss → PyMuPDF 直读(AI 想看链接原文时**保证读得到**,不只提示 read_page;用户实锤)
    try:
        import fitz
        ap = Path(VAULT_ROOT) / rel
        if ap.exists() and 1 <= page:
            d = fitz.open(str(ap))
            if page <= d.page_count:
                t = (d[page - 1].get_text() or "").strip()
                d.close()
                return t[:limit]
            d.close()
    except Exception:
        pass
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
        terms = extract_terms(txt, lang=_book_lang(rel))   # 页文本按书语言分词(纯汉字日语必须)
        if not terms:
            continue
        c.execute("INSERT OR REPLACE INTO events(id,ts,channel,weight,text,terms,file,page,uid,src_key,xver)"
                  " VALUES((SELECT id FROM events WHERE src_key=?),?,?,?,?,?,?,?,?,?,?)",
                  (key, ts, "read", w, txt[:TEXT_MAX], json.dumps(terms[:TERMS_MAX], ensure_ascii=False),
                   rel, page_no, "", key, EXTRACTOR_VER))
        n += 1
    return n


def import_anki_lapses(c):
    """Anki **答错**(revlog.ease=1)→ 薄弱信号。只读打开 collection(Anki 在跑也不干扰)。
    ★只收 lapse 不收全部复习:总 revlog 52174 条(大批量日语沉浸牌组的历史积累),
      全导会把 2 千条真实注意力事件淹没;而「答错」量小(近 180 天 51 条)且信号最强。
    文本 = 卡片正面(notes.sfld);牌组名进 extra,便于日后按牌组过滤。"""
    if not ANKI_DB.exists():
        return 0
    n = 0
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
        con.create_collation("unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
        cut = int((time.time() - ANKI_LAPSE_DAYS * 86400) * 1000)
        rows = con.execute(
            "SELECT r.id, r.cid, n.sfld, d.name FROM revlog r"
            " JOIN cards ca ON ca.id = r.cid JOIN notes n ON n.id = ca.nid"
            " LEFT JOIN decks d ON d.id = ca.did"
            " WHERE r.ease = 1 AND r.id > ? ORDER BY r.id DESC LIMIT 2000", (cut,)).fetchall()
        con.close()
    except Exception as ex:
        sys.stderr.write("[anki] 读 collection 失败(跳过): %s\n" % str(ex)[:80])
        return 0
    import re as _re
    for rid, cid, sfld, deck in rows:
        txt = _re.sub(r"<[^>]+>|\\\([^)]*\\\)|\$[^$]*\$", " ", str(sfld or ""))   # 去 HTML / LaTeX(公式不是术语)
        txt = _re.sub(r"\s+", " ", txt).strip()[:200]
        if len(txt) < 2:
            continue
        key = hashlib.sha1(("anki|%d" % rid).encode()).hexdigest()[:20]
        row = c.execute("SELECT xver FROM events WHERE src_key=?", (key,)).fetchone()
        if row and int(row[0] or 0) >= EXTRACTOR_VER:
            continue
        terms = extract_terms(txt)          # 卡片语言不定 → 按字形判(牌组语言没有权威表)
        if not terms:
            continue
        c.execute("INSERT OR REPLACE INTO events(id,ts,channel,weight,text,terms,file,page,uid,src_key,xver)"
                  " VALUES((SELECT id FROM events WHERE src_key=?),?,?,?,?,?,?,?,?,?,?)",
                  (key, int(rid / 1000), "anki_lapse", W["anki_lapse"], txt,
                   json.dumps(terms[:TERMS_MAX], ensure_ascii=False),
                   ("anki:" + str(cid)), 0, "", key, EXTRACTOR_VER))
        n += 1
    return n


def import_notes(c):
    """笔记(vault 根的 md)→ 最强主动信号(权重 5.0:为一个知识点写笔记 = 深度投入)。
    ★只取**标题 + 首段**(NOTE_MAX_CHARS):全文会把一篇笔记里的所有词都拉进画像,
      淹没真正的焦点(同 check 只收纸标题的道理)。ts = 文件 mtime(按天去重:同一天多次改算一条)。
    跳过:索引/模板/资源目录、`_` 开头、空文件。"""
    root = Path(VAULT_ROOT)
    if not root.exists():
        return 0
    n = 0
    for f in root.glob("*.md"):                       # 只扫 vault 根(学习笔记都在这;资源/books 是素材不是笔记)
        if f.name.startswith(("_", ".")):
            continue
        try:
            st = f.stat()
            raw = f.read_text("utf-8")
        except Exception:
            continue
        # #3(审查):Excalidraw 笔记**先读全**,只取 ## Text Elements(真文字),丢弃 banner/Drawing/Embedded Files;
        #   别先截断——原实现截到 4000 字把 %% 闭合符和 banner 都切掉了,正则失效(52 条 note 仍含 excalidraw)。
        if ("excalidraw-plugin:" in raw[:400]) or ("## Text Elements" in raw):
            _m = re.search(r"##\s*Text Elements\s*\n(.*?)(?:\n#{1,2}\s|\n%%|\Z)", raw, re.S)
            txt = (_m.group(1) if _m else "")[:4000]
        else:
            txt = raw[:4000]
        # 去 frontmatter
        if txt.startswith("---"):
            _e = txt.find("\n---", 3)
            if _e > 0:
                txt = txt[_e + 4:]
        # 残留清洗:%% 绘图块、compressed-json/excalidraw/drawing 代码块、超长 base64/哈希串
        txt = re.sub(r"%%[\s\S]*?%%", " ", txt)
        txt = re.sub(r"```(?:compressed-json|excalidraw|drawing|json)[\s\S]*?```", " ", txt)
        txt = re.sub(r"\S{45,}", " ", txt)
        body = " ".join(x.strip("#> -*") for x in txt.split("\n") if x.strip())[:NOTE_MAX_CHARS]
        body = (f.stem + " " + body).strip()
        if len(body) < 4:
            continue
        day = int(st.st_mtime // 86400)
        key = hashlib.sha1(("note|%s|%d" % (f.name, day)).encode()).hexdigest()[:20]
        row = c.execute("SELECT xver FROM events WHERE src_key=?", (key,)).fetchone()
        if row and int(row[0] or 0) >= EXTRACTOR_VER:
            continue
        terms = extract_terms(body)
        if not terms:
            continue
        c.execute("INSERT OR REPLACE INTO events(id,ts,channel,weight,text,terms,file,page,uid,src_key,xver)"
                  " VALUES((SELECT id FROM events WHERE src_key=?),?,?,?,?,?,?,?,?,?,?)",
                  (key, int(st.st_mtime), "note", W["note"], body[:TEXT_MAX],
                   json.dumps(terms[:TERMS_MAX], ensure_ascii=False), f.relative_to(root).as_posix(), 0, "", key, EXTRACTOR_VER))
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
        if file and ch not in ("note", "anki_lapse"):   # #6 IDF 只数真书(排除笔记/卡)
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
            if file and ch not in ("note", "anki_lapse"):   # #6 同上
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


# ── 及时性:读时保证新鲜(下游拿到的永远是最新数据,不等 15min timer)──────────────────
def _sources_fp():
    """所有源的指纹(mtime+size)。变了才需要导入 —— 没变时这一步是纯 stat,微秒级。"""
    fp = []
    for p_ in [STATE / "vocab-lookups.jsonl", ATT_DIR / "dwell.jsonl", RAW, ANKI_DB]:
        try:
            st = p_.stat()
            fp.append("%s:%d:%d" % (p_.name, st.st_mtime_ns, st.st_size))
        except Exception:
            pass
    try:                                  # vault 根笔记(note 渠道)
        _nm = max((f.stat().st_mtime_ns for f in Path(VAULT_ROOT).glob("*.md")), default=0)
        _nn = sum(1 for _ in Path(VAULT_ROOT).glob("*.md"))
        fp.append("notes:%d:%d" % (_nm, _nn))
    except Exception:
        pass
    for d in ("pdf-highlights", "epub-highlights", "html-highlights", "assistant-convo",
              "assistant-convo-archive", "reader-check-reports"):
        try:
            dd = STATE / d
            mt = max((f.stat().st_mtime_ns for f in dd.glob("*")), default=0)
            n = sum(1 for _ in dd.glob("*"))
            fp.append("%s:%d:%d" % (d, mt, n))
        except Exception:
            pass
    return hashlib.sha1("|".join(fp).encode()).hexdigest()[:16]


def ensure_fresh(c=None, force=False):
    """★读时新鲜:源变了(或抽取器升版)就**增量导入**;没变直接返回(微秒)。
    focus_window / focus_of_text / focus 查询前都会调 —— 下游读到的永远是当下的数据,
    不依赖 quick_sync 的 15min 周期(那只是兜底 + 焦点快照落盘)。
    增量导入不是全量重算:add_event 对同版本已存在的 src_key 直接跳过(不跑分词),所以很便宜。"""
    own = c is None
    c = c or _db()
    try:
        cur = _sources_fp() + "|x%d" % EXTRACTOR_VER
        row = c.execute("SELECT v FROM meta WHERE k='sources_fp'").fetchone()
        if not force and row and row[0] == cur:
            return {"fresh": True, "imported": 0, "took_s": 0.0}
        t0 = time.time()
        stats = {"lookup": import_lookups(c), "highlight": import_highlights(c),
                 "qa": import_convo(c), "qa_arch": import_convo_archive(c),
                 "check": import_checks(c), "read": import_dwell(c), "raw": import_raw(c),
                 "anki_lapse": import_anki_lapses(c), "note": import_notes(c)}
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('sources_fp',?)", (cur,))
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_import',?)", (str(int(time.time())),))
        c.commit()
        return {"fresh": False, "imported": sum(stats.values()), "stats": stats,
                "took_s": round(time.time() - t0, 2)}
    finally:
        if own:
            c.close()


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
    ensure_fresh(c)          # ★读时新鲜(源没变时纯 stat,微秒)
    q = "SELECT ts, channel, weight, terms, file, page, id FROM events WHERE ts>=? AND ts<?"
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
    by_ch = defaultdict(lambda: defaultdict(float))   # 可回溯:分数按渠道拆
    evid = defaultdict(list)                          # 可回溯:证据事件 id
    # ★双权重(用户设计):最终权重 = 渠道基础权重 × **时间权重**。窗内时间权重 = 相对**窗口末端**的
    #   半衰期衰减(窗越长半衰期越长:窗口 1/3 处衰减到一半)——「上个月」里月末的比月初的更代表当时焦点,
    #   但不会像全局画像那样把整个窗口压平。窗 ≤2 天(今天/昨天)不衰减(一天内先后没有意义)。
    _span_d = max(0.5, (u_ts - s_ts) / 86400.0)
    _half = (_span_d / 3.0) if _span_d > 2 else 0.0
    for ts, ch, w, terms_j, f, page, _eid in rows:
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
            by_ch[t][ch] += w
            if len(evid[t]) < 20:
                evid[t].append(_eid)
            if f and len(refs[t]) < 6:
                refs[t].append((ts, f, page))
    nb = max(1, len(all_books))
    out = []
    for t, v in sc.items():
        idf = math.log((1 + nb) / (1 + len(tb.get(t, ())))) + 0.3
        _disp = surf[t].most_common(1)[0][0] if surf.get(t) else t
        out.append({"term": _disp, "key": t, "score": round(v * idf, 2), "n": cnt[t], "channels": sorted(chs[t]),
                    "idf": round(idf, 2), "by_channel": {k2: round(v2, 2) for k2, v2 in by_ch[t].items()},
                    "evidence": evid[t],        # 可回溯:事件 id → explain() 能取回原文
                    "refs": [{"file": f, "page": p} for _, f, p in sorted(refs[t], reverse=True)[:2]]})
    out.sort(key=lambda x: -x["score"])
    c.close()
    return {"ok": True, "n_events": len(rows),
            "window": {"since": int(s_ts), "until": int(u_ts),
                       "label": (when or (f"最近{days}天" if days else "最近7天"))},
            "top": out[:top]}


def explain(term, when="", days=None, limit=12):
    """★可回溯:这个词**为什么**在焦点里——列出贡献它的每一条事件(渠道/时间/权重/书页/原文片段)
    + 分数构成。下游(或用户)要审计焦点数据时调它。"""
    c = _db()
    ensure_fresh(c)
    k = norm_key(term) or term
    s_ts, u_ts = parse_when(when, days) if (when or days) else (0, time.time())
    rows = c.execute("SELECT id, ts, channel, weight, text, terms, file, page, xver FROM events"
                     " WHERE ts>=? AND ts<? ORDER BY ts DESC", (int(s_ts), int(u_ts))).fetchall()
    hits = []
    for _id, ts, ch, w, txt, tj, f, page, xv in rows:
        try:
            terms = json.loads(tj)
        except Exception:
            continue
        m = [x for x in terms if (norm_key(x) or x) == k]
        if not m:
            continue
        hits.append({"event_id": _id, "ts": ts, "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
                     "channel": ch, "base_weight": w, "surface": m[0],
                     "terms_in_event": len(set(terms)), "dilution": round(1 / math.sqrt(max(1, len(set(terms)))), 3),
                     "file": f, "page": page, "extractor_ver": xv,
                     "text": (txt or "")[:160]})
        if len(hits) >= limit:
            break
    c.close()
    return {"ok": True, "term": term, "key": k, "n_events": len(hits), "events": hits,
            "note": "score = Σ(渠道权重 × 事件内稀释 1/√N × 时间衰减) × IDF;"
                    "extractor_ver < %d 的事件是旧分词器抽的(下次 --rebuild 会重抽)" % EXTRACTOR_VER}


# ─────────────────────────────────────────────────────────────────────────────
# 第二步:焦点词 → 材料(设计 §5l)。material ref 统一寻址 + 关联打分聚合器。
#   本块只做 events 路(我**实际关注过**的材料);FTS5 路 / 语义相似留待第 ② 块。
# ─────────────────────────────────────────────────────────────────────────────
_REL_HALF_D = 60.0      # 材料检索的时间半衰期(★比焦点画像的 7d 长得多:相关性主导、长记忆——
                        #   半年前学的高相关材料照样要找到。时间是加分不是门槛,见 §5l)
_MATERIAL_CH_W = {"highlight": 3.0, "check": 4.0, "note": 5.0, "qa": 2.0,
                  "read": 1.0, "lookup": 1.0, "tool": 1.5, "anki_lapse": 2.5}


def _material_ref(channel, file, page, uid=""):
    """事件 → 统一材料地址(下游操作靠它定位;设计 §5l)。
    书页=book:<file>#p<page>;检查报告/笔记等特殊渠道各自成型。页码漂移由 PAGE_ANCHOR_MIGRATIONS 兜。"""
    f = file or ""
    if channel == "check":
        return "check:" + f            # file 里其实存的是 report file;精确 rid 在 explain 时补
    if channel == "note":
        return "note:" + f             # f = vault 相对路径(现已存)
    if channel == "anki_lapse":
        return f or "anki:?"           # f = "anki:<cid>"(卡片级 → read_material 能读正反面)
    if f and page:
        return "book:%s#p%d" % (f, int(page))
    if f:
        return "book:%s" % f
    return "%s:?" % channel


def _material_label(ref):
    """material ref → 人话(给 AI/用户看)。"""
    try:
        kind, rest = ref.split(":", 1)
    except ValueError:
        return ref
    if kind == "book":
        f, _, pg = rest.partition("#p")
        nm = f.split("/")[-1]
        return "%s%s" % (nm, (" 第%s页" % pg) if pg else "")
    if kind == "note":
        return "笔记《%s》" % rest.split("/")[-1].replace(".md", "")
    if kind == "check":
        return "检查报告"
    if kind == "anki":
        return "Anki 卡片"             # 详细正反面靠 read_material(ref)
    if kind == "kg":
        _b, _, _nid = rest.partition("#")
        n = _kg_all()["nodes"].get(_nid) or {}
        return "知识点「%s」" % (n.get("name") or _nid)
    return ref


def relate_material(term, when="", days=None, top=12, order="relevance", now=None):
    """★焦点词 → 我**实际关注过**的材料(events 路;设计 §5l)。
    按 material ref 聚合该词的所有行为事件,融合打分:
        rel = Σ_events[ 渠道权重 × 时间加权(半衰期 60d,加分不门槛) ]
    order='relevance'(默认,相关性主导)| 'recent'(时间主导:满足「我最近在看什么」)。
    跨语言归一后匹配(norm_key)→ 用中文焦点词也能找到英/日原文材料。
    返回 {ok, term, key, n, materials:[{ref, label, rel, hits, last_ts, last_when, channels, sample}]}。
    """
    now = now or time.time()
    c = _db()
    ensure_fresh(c)
    k = norm_key(term) or term
    s_ts, u_ts = parse_when(when, days) if (when or days) else (0, now)
    rows = c.execute("SELECT id, ts, channel, weight, text, terms, file, page, uid FROM events"
                     " WHERE ts>=? AND ts<? ORDER BY ts DESC", (int(s_ts), int(u_ts))).fetchall()
    agg = {}
    for _id, ts, ch, w, txt, tj, f, page, uid in rows:
        try:
            terms = set(json.loads(tj))
        except Exception:
            continue
        if not any((norm_key(x) or x) == k for x in terms):
            continue
        ref = _material_ref(ch, f, page, uid)
        chw = _MATERIAL_CH_W.get(ch, 1.0)
        tw = 2 ** (-max(0.0, (now - ts) / 86400.0) / _REL_HALF_D)   # 时间加权(加分,不为 0 门槛)
        contrib = chw * (0.4 + 0.6 * tw)                            # 0.4 底:久远材料也保 40% 权重(长记忆)
        a = agg.setdefault(ref, {"rel": 0.0, "hits": 0, "last_ts": 0, "channels": {}, "sample": ""})
        a["rel"] += contrib
        a["hits"] += 1
        a["channels"][ch] = a["channels"].get(ch, 0) + 1
        if ts > a["last_ts"]:
            a["last_ts"] = ts
            a["sample"] = (txt or "")[:100]
    c.close()
    mats = []
    for ref, a in agg.items():
        mats.append({"ref": ref, "label": _material_label(ref), "rel": round(a["rel"], 2),
                     "hits": a["hits"], "last_ts": a["last_ts"],
                     "last_when": time.strftime("%Y-%m-%d", time.localtime(a["last_ts"])),
                     "channels": sorted(a["channels"], key=lambda x: -a["channels"][x]),
                     "sample": a["sample"]})
    key = (lambda m: -m["last_ts"]) if order == "recent" else (lambda m: -m["rel"])
    mats.sort(key=key)
    return {"ok": True, "term": term, "key": k, "n": len(mats),
            "order": order, "materials": mats[:top]}


# ─────────────────────────────────────────────────────────────────────────────
# Obsidian 格式归一(用户实锤:数据格式没统一)。把三种 vault 内链接统一成 material ref:
#   [[名字]]                         → note:<vault相对路径>(wikilink 只给文件名,要 resolve)
#   [[名字.pdf#page=N]] / ![[..]]     → book:<pdf相对路径>#p<N>(嵌入带 rect/color,只取 page)
# ─────────────────────────────────────────────────────────────────────────────
_VAULT_IDX = None


def _vault_index():
    """{文件名stem(小写): vault相对路径}。wikilink 用文件名不含路径 → 靠它 resolve。缓存。"""
    global _VAULT_IDX
    if _VAULT_IDX is None:
        _VAULT_IDX = {}
        try:
            root = Path(VAULT_ROOT)
            for ext in ("*.md", "*.pdf", "*.epub"):
                for f in root.rglob(ext):
                    rel = f.relative_to(root).as_posix()
                    if "/.sandbox/" in rel:
                        continue
                    _VAULT_IDX.setdefault(f.stem.lower(), rel)   # 首个胜出(同名少见)
                    _VAULT_IDX.setdefault(f.name.lower(), rel)   # 带扩展名也认
        except Exception:
            pass
    return _VAULT_IDX


def obsidian_to_ref(link):
    """Obsidian 链接 → material ref(统一格式)。认 [[..]]/![[..]]/裸文件名#page=N。"""
    if not link:
        return None
    t = link.strip()
    t = re.sub(r"^!?\[\[|\]\]$", "", t).strip()        # 去 [[ ]] / ![[ ]]
    t = t.split("|", 1)[0].strip()                         # 去别名 [[x|显示]]
    idx = _vault_index()
    # 带 #page= 的书页嵌入
    m = re.search(r"#page=(\d+)", t)
    if m:
        name = t.split("#", 1)[0].strip()
        rel = idx.get(name.lower()) or idx.get((name + ".pdf").lower())
        if rel:
            return "book:%s#p%d" % (rel, int(m.group(1)))
        return None
    # 纯 wikilink(笔记名)
    if "#" in t:
        t = t.split("#", 1)[0].strip()
    rel = idx.get(t.lower()) or idx.get((t + ".md").lower())
    if rel:
        return ("book:" + rel) if rel.lower().endswith((".pdf", ".epub")) else ("note:" + rel)
    return None


_ANKI_SRC = None


def _anki_source_map():
    """{anki_note_id(=nid): 源笔记 material ref}(从 anki/records/*.json 建;缓存)。
    这就是「卡片→源」的连接(用户实锤:数据在,只是没接)。"""
    global _ANKI_SRC
    if _ANKI_SRC is None:
        _ANKI_SRC = {}
        try:
            import glob
            for f in glob.glob(str(Path(PROJECT_DIR) / "anki" / "records" / "*.json")):
                try:
                    d = json.loads(Path(f).read_text("utf-8"))
                except Exception:
                    continue
                sn = d.get("source_note") or ""
                sl = d.get("source_link") or ""
                ref = ("note:" + sn) if sn else obsidian_to_ref(sl)
                if not ref:
                    continue
                for card in (d.get("cards") or []):
                    nid = card.get("anki_note_id")
                    if nid:
                        _ANKI_SRC[int(nid)] = ref
        except Exception:
            pass
    return _ANKI_SRC


def _note_book_refs(note_rel, limit=8):
    """笔记里嵌的书页链接 ![[x.pdf#page=N]] → [book ref…](卡片→源笔记→书页 的最后一跳)。"""
    out = []
    try:
        body = (Path(VAULT_ROOT) / note_rel).read_text("utf-8")
    except Exception:
        return out
    for m in re.finditer(r"!?\[\[[^\]]*#page=\d+[^\]]*\]\]", body):
        r = obsidian_to_ref(m.group(0))
        if r and r not in out:
            out.append(r)
        if len(out) >= limit:
            break
    return out


def _anki_cid_to_nid(cid):
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
        r = con.execute("SELECT nid FROM cards WHERE id=?", (int(cid),)).fetchone()
        con.close()
        return int(r[0]) if r else None
    except Exception:
        return None


_KG_CACHE = None


def _kg_all():
    """所有 KG 的 {node_id: node} + {book: kg}(缓存)。node 带 pages/parent_id/edges。"""
    global _KG_CACHE
    if _KG_CACHE is None:
        _KG_CACHE = {"nodes": {}, "by_book": {}, "edges": []}
        import glob
        for f in glob.glob(str(Path(PROJECT_DIR) / "knowledge_graph" / "*.json")):
            if ".bak." in f:
                continue
            try:
                d = json.loads(Path(f).read_text("utf-8"))
            except Exception:
                continue
            book = d.get("book") or Path(f).stem
            _KG_CACHE["by_book"][book] = d
            for n in (d.get("nodes") or []):
                nid = n.get("id")
                if nid:
                    n["_book"] = book
                    _KG_CACHE["nodes"][nid] = n
            for e in (d.get("edges") or []):
                e["_book"] = book
                _KG_CACHE["edges"].append(e)
    return _KG_CACHE


def _kg_pdf_rel(book):
    """KG 的 book → 它对应的 vault 相对 PDF 路径(pages 命中要按这本 PDF 的页)。"""
    kg = _kg_all()["by_book"].get(book) or {}
    pdf = kg.get("pdf") or ""
    if pdf:
        try:
            return Path(pdf).resolve().relative_to(Path(VAULT_ROOT).resolve()).as_posix()
        except Exception:
            return pdf.split("/obsidian/")[-1] if "/obsidian/" in pdf else pdf
    idx = _vault_index()
    return idx.get((book or "").lower()) or ""


def material_neighbors(ref):
    """一份材料在诊断链里的**直接邻居**(有向图的一跳)。返回 {up:[refs], down:[refs]}。
      anki:cid  → up:源笔记            book:file#p → up:嵌它的笔记 / down:命中的KG节点
      note:rel  → down:嵌的书页        kg:book#node → up:节点页对应的书页 / down:前置节点(prereq)
    """
    up, down = [], []
    try:
        kind, rest = ref.split(":", 1)
    except ValueError:
        return {"up": [], "down": []}
    if kind == "anki":
        d = read_material(ref)
        src = (d.get("源") or {}).get("ref")
        if src:
            up.append(src)
    elif kind == "note":
        down += _note_book_refs(rest)
    elif kind == "book":
        f, _, pg = rest.partition("#p")
        page = int(pg) if pg.isdigit() else 0
        # down: 这一页归属的 KG 节点
        for nid, n in _kg_all()["nodes"].items():
            pgs = n.get("pages") or []
            if len(pgs) == 2 and pgs[0] <= page <= pgs[1] and _kg_pdf_rel(n["_book"]) == f:
                down.append("kg:%s#%s" % (n["_book"], nid))
        # up: 哪些笔记嵌了这一页(反查)—— 便宜起见只在 note 渠道事件里找引用了这本书的
        # (完整反查要扫全 vault,留给需要时;这里从画像里已知的笔记推)
    elif kind == "kg":
        book, _, nid = rest.partition("#")
        n = _kg_all()["nodes"].get(nid)
        if n:
            pgs = n.get("pages") or []
            rel = _kg_pdf_rel(book)
            if len(pgs) == 2 and rel:
                for pg in range(pgs[0], min(pgs[1], pgs[0] + 3) + 1):   # 节点页范围(限前几页)
                    up.append("book:%s#p%d" % (rel, pg))
            for e in _kg_all()["edges"]:
                if e.get("kind") == "prereq" and e.get("to") == nid:   # 前置:指向本节点的 prereq 边的 from
                    down.append("kg:%s#%s" % (e.get("_book", book), e.get("from")))
    return {"up": up, "down": down}


def material_graph(ref, direction="both", depth=2, limit=30):
    """★从**任意位置**出发,取**任意后续**链条(用户设计:任意层级、任意方向、任意深度)。
    direction: 'up'(往来源:卡→笔记→书页) | 'down'(往派生/前置:书页→节点→前置) | 'both'。
    BFS 展开 depth 跳。每个节点带 label + kind,方便 AI 挑一层读内容(配合 read_material)。
    """
    seen = {ref}
    layers = [[{"ref": ref, "label": _material_label(ref), "depth": 0}]]
    frontier = [ref]
    for d in range(1, depth + 1):
        nxt, nodes = [], []
        for r in frontier:
            nb = material_neighbors(r)
            picks = ((nb["up"] if direction in ("up", "both") else []) +
                     (nb["down"] if direction in ("down", "both") else []))
            for x in picks:
                if x in seen:
                    continue
                seen.add(x)
                nxt.append(x)
                nodes.append({"ref": x, "label": _material_label(x), "depth": d, "from": r})
                if len(seen) >= limit:
                    break
        if not nodes:
            break
        layers.append(nodes)
        frontier = nxt
    return {"ok": True, "start": ref, "direction": direction, "depth": depth,
            "n": sum(len(l) for l in layers), "layers": layers,
            "note": "任一 ref 用 read_material(ref) 读它那一层的内容(书页原文/笔记/卡片正反面/KG节点)"}


def _material_label_kg(ref):
    pass


def _safe_vault_path(rest, exts=(".md",)):
    """#5 安全:material ref 的路径必须 resolve 后仍在 vault 内,且扩展名白名单。
    挡绝对路径(note:/etc/passwd)、.. 越界、非白名单扩展。返回 Path 或 None。"""
    try:
        root = Path(VAULT_ROOT).resolve()
        pth = (root / str(rest)).resolve()
    except Exception:
        return None
    if pth != root and root not in pth.parents:
        return None
    if exts and pth.suffix.lower() not in exts:
        return None
    return pth


def read_material(ref, limit=1500):
    """★material ref → **详细内容**(用户实锤:能看到材料却读不到内容)。统一取详细内容入口,
    下游(出题/复习/AI 分析)靠它把「地址」变成「内容」。
      book:<file>#p<page> → 那页正文(离线从 char-cache 拼)
      note:<vault相对路径>  → 笔记全文(前 limit 字)
      anki:<cid>           → 那张卡的正面+背面(去 HTML)
      check:<file>         → 提示用 read_check_report(检查报告有专门工具)
    返回 {ok, ref, kind, title, content} 或 {error}。"""
    if ref.startswith("[[") or ref.startswith("![["):    # 直接传 Obsidian 链接 → 先归一
        _r = obsidian_to_ref(ref)
        if not _r:
            return {"error": "解析不了这个 Obsidian 链接:%s" % ref}
        ref = _r
    try:
        kind, rest = ref.split(":", 1)
    except ValueError:
        return {"error": "ref 格式不对:%s" % ref}
    if kind == "book":
        f, _, pg = rest.partition("#p")
        if f.startswith("/") or ".." in f.split("/"):
            return {"error": "非法书路径:%s" % f[:60]}
        page = int(pg) if pg.isdigit() else 0
        txt = _page_text(f, page, limit=limit) if page else ""
        if not txt:
            return {"ok": True, "ref": ref, "kind": "book", "title": _material_label(ref),
                    "content": "", "note": "这页没缓存正文;用 read_page(file='%s', page=%d) 读" % (f, page)}
        return {"ok": True, "ref": ref, "kind": "book", "title": _material_label(ref), "content": txt}
    if kind == "note":
        _np = _safe_vault_path(rest, exts=(".md",))
        if not _np:
            return {"error": "非法笔记路径(越界/非 .md):%s" % str(rest)[:60]}
        try:
            body = _np.read_text("utf-8")
        except Exception as e:
            return {"error": "读笔记失败:%s" % str(e)[:60]}
        if body.startswith("---"):                 # 跳过 frontmatter(anki_total 等元数据不是内容)
            _e = body.find("\n---", 3)
            if _e > 0:
                body = body[_e + 4:]
        body = body.strip()[:limit]
        return {"ok": True, "ref": ref, "kind": "note",
                "title": "笔记《%s》" % rest.split("/")[-1].replace(".md", ""), "content": body}
    if kind == "anki":
        try:
            cid = int(rest)
        except ValueError:
            return {"error": "anki ref 无 cid"}
        try:
            con = sqlite3.connect("file:%s?mode=ro&immutable=1" % ANKI_DB, uri=True)
            r = con.execute("SELECT n.flds, d.name FROM cards ca JOIN notes n ON n.id = ca.nid"
                            " LEFT JOIN decks d ON d.id = ca.did WHERE ca.id = ?", (cid,)).fetchone()
            con.close()
        except Exception as e:
            return {"error": "读卡片失败:%s" % str(e)[:60]}
        if not r:
            return {"error": "卡片 %d 不存在(可能已删)" % cid}
        import re as _re
        parts = [_re.sub(r"<[^>]+>", " ", x).strip() for x in str(r[0]).split("\x1f")]
        deck = str(r[1] or "").replace("\x1f", "::").strip(":")
        res = {"ok": True, "ref": ref, "kind": "anki", "title": "Anki 卡片(%s)" % deck,
               "content": {"正面": parts[0][:600] if parts else "", "背面": parts[1][:600] if len(parts) > 1 else ""}}
        # ★卡片→源:这张卡是学哪份材料做的(用户设计的诊断链入口)。
        #   优先从**卡片自带**的 @src 标记提取(制卡时注入,覆盖所有新卡);没有再查 records(旧卡兜底)。
        src = None
        _m = re.search(r"@src:(.+?)-->", str(r[0]))
        if _m:
            src = obsidian_to_ref(_m.group(1).strip())
        if not src:
            nid = _anki_cid_to_nid(cid)
            src = _anki_source_map().get(nid) if nid else None
        if src:
            res["源"] = {"ref": src, "label": _material_label(src)}
            if src.startswith("note:"):
                books = _note_book_refs(src[5:])   # 源笔记里嵌的书页 → 追到书
                if books:
                    res["源"]["书页"] = [{"ref": b, "label": _material_label(b)} for b in books]
        return res
    if kind == "kg":
        _b, _, _nid = rest.partition("#")
        n = _kg_all()["nodes"].get(_nid)
        if not n:
            return {"error": "KG 节点不存在:%s" % _nid}
        prereq = [e.get("from") for e in _kg_all()["edges"]
                  if e.get("kind") == "prereq" and e.get("to") == _nid]
        return {"ok": True, "ref": ref, "kind": "kg", "title": "知识点「%s」" % n.get("name"),
                "content": {"节点": n.get("name"), "所在页": n.get("pages"), "层级": n.get("level"),
                            "父节点": n.get("parent_id"), "前置节点id": prereq[:8],
                            "书": n.get("_book")}}
    if kind == "check":
        return {"ok": True, "ref": ref, "kind": "check",
                "note": "检查报告用 read_check_report 工具读(有专门的问答式接口)"}
    return {"error": "未知材料类型:%s" % kind}


def focus_of_text(text, top=8):
    """★「当前对话/这段文字的焦点」:直接抽词 + 用全库 IDF 排序(无需事件表)。
    用于:助手看本轮对话说了什么 → 焦点术语 → 再去 focus_window/FTS 找相关材料。"""
    terms = extract_terms(text or "")
    if not terms:
        return {"ok": True, "top": []}
    c = _db()
    ensure_fresh(c)
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


def _lang_of(t):
    if _KANA.search(t):
        return "ja"
    if _CJK.search(t):
        return "cjk"
    return "en"


def _ecdict_zh(word):
    """ECDICT 释义里的中文词(候选生成用;它是英中词典,340 万词条,项目已有)。"""
    try:
        e = sqlite3.connect("file:%s?mode=ro" % (Path(PROJECT_DIR) / "data" / "ecdict.db"), uri=True)
        r = e.execute("SELECT translation FROM stardict WHERE word=?", (word,)).fetchone()
        e.close()
        if not r or not r[0]:
            return set()
        return set(re.findall(r"[\u4e00-\u9fff]{2,6}", r[0]))
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────────────
# 跨语言关联 · 融合打分(用户设计,2026-07-18):
#   「多个可能性乘以权重达到一定程度就进行 AI 判断」—— 信息检索里的 late fusion + 阈值门控。
#   四个信号各归一到 [0,1],加权几何平均(用户强调「乘」:多个都强才高;任一为 0 靠 ε 不致命):
#       P = ∏ (ε + wᵢ·sᵢ)^(1/n)          P > τ → 送 AI 判定(贵,只判高分候选)
#   信号:
#     T 时间共现  = NPMI 取正(★关键:用点互信息而非裸共现 —— 高频词即使共现 PMI 也低,
#                   天然压制「feynman↔原子」这类高频互撞;这修正了「时间共现是垃圾」的旧结论:
#                   不是时间没用,是没归一化)
#     S 向量相似  = (cos−0.5)×2 取正(gemini-embedding,跨语言 + 短词长句通用;实测同义 0.8/无关 0.57)
#     O 部件重合  = 已确认别名的部件数 / 部件数(vector↔向量已确认 → vector space 加分,用户「某些字眼互相对应」)
#     D 词典对应  = ECDICT 命中(单词才有,长句自然为 0)
_FUSION_W = {"T": 1.0, "S": 1.2, "O": 1.0, "D": 0.8}   # 权重(向量相似最可靠,给最高)
_FUSION_EPS = 0.15                                       # 平滑:缺一个信号不致命
_FUSION_TAU = 0.30                                       # 阈值:P > τ 才送 AI(调这个控制 AI 调用量/召回)
_EMB_CACHE = ATT_DIR / "emb-cache.npz"                  # 术语向量永久缓存(numpy 二进制:比 51MB JSON 快几十倍)
_EMB_JSON_OLD = ATT_DIR / "emb-cache.json"             # 旧 JSON 缓存(一次性迁移后删)


def _emb_load():
    """{term: np.array}。旧 JSON 缓存自动迁移到 npz。"""
    import numpy as np
    try:
        d = np.load(_EMB_CACHE, allow_pickle=True)
        terms = list(d["terms"]); M = d["M"]
        return {t: M[i] for i, t in enumerate(terms)}
    except Exception:
        pass
    if _EMB_JSON_OLD.exists():                          # 迁移:旧 51MB JSON → npz(一次)
        try:
            j = json.loads(_EMB_JSON_OLD.read_text("utf-8"))
            cache = {t: np.array(v, dtype=np.float32) for t, v in j.items()}
            _emb_save(cache)
            _EMB_JSON_OLD.rename(_EMB_JSON_OLD.with_suffix(".json.bak"))
            return cache
        except Exception:
            pass
    return {}


def _emb_save(cache):
    import numpy as np
    if not cache:
        return
    terms = list(cache)
    M = np.array([np.asarray(cache[t], dtype=np.float32) for t in terms])
    np.savez_compressed(_EMB_CACHE, terms=np.array(terms, dtype=object), M=M)


def _emb_get(terms, cache):
    """要 embedding 的术语 → {term: vec(np)};缺的批量调 API(768 维,省流量)。"""
    import numpy as np
    need = [t for t in terms if t not in cache]
    if need:
        try:
            sys.path.insert(0, "/home/bwicarus/webapp")
            import assistant as A
            for i in range(0, len(need), 40):
                chunk = need[i:i + 40]
                vs = A.gemini_embed(chunk, dim=768)     # 768 足够,存储/传输小 4 倍
                if not vs:
                    break
                for t, v in zip(chunk, vs):
                    cache[t] = np.asarray(v, dtype=np.float32)
            _emb_save(cache)
        except Exception:
            pass
    return cache


def _cos(a, b):
    import numpy as np
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32); b = np.asarray(b, dtype=np.float32)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    na = float(np.linalg.norm(a)) or 1e-9; nb = float(np.linalg.norm(b)) or 1e-9
    return float(a @ b) / (na * nb)


def _sig_time(A, B, day_of, ndays):
    """T = NPMI 取正。day_of[x] = x 出现过的天集合。"""
    da, db = day_of.get(A) or set(), day_of.get(B) or set()
    if not da or not db or not ndays:
        return 0.0
    co = len(da & db)
    if co == 0:
        return 0.0
    pab = co / ndays
    pa, pb = len(da) / ndays, len(db) / ndays
    pmi = math.log(pab / (pa * pb))
    npmi = pmi / (-math.log(pab))          # ∈ [-1,1]
    return max(0.0, npmi)


def _sig_parts(A, B):
    """O = A、B 拆成部件后,已被 concepts 确认为同一别名的部件比例。"""
    al = _concepts()
    pa = re.findall(r"[a-z][a-z\-']+", A.lower()) if A.isascii() else list(A)
    pb = set(B)                            # 中文按字
    if not pa:
        return 0.0
    ok = 0
    for w in pa:
        cn = al.get(w)                     # 部件的中文别名
        if cn and any(ch in pb for ch in cn):
            ok += 1
    return ok / len(pa)


_FUSION_WFILE = ATT_DIR / "fusion-weights.json"   # 学出来的权重(有则覆盖手调默认)


def _load_weights():
    """启动时读学出来的权重(fit_weights 产物);没有就用手调默认。"""
    global _FUSION_W, _FUSION_TAU
    try:
        d = json.loads(_FUSION_WFILE.read_text("utf-8"))
        _FUSION_W = {k: float(v) for k, v in d["W"].items()}
        _FUSION_TAU = float(d["tau"])
    except Exception:
        pass


def fit_weights(dry=False):
    """★用词典金标准**反向学习**融合权重(用户设计:「用百分百确认的词典对应反向调整,找到正确的值」)。
    把手调的 _FUSION_W/τ 变成**数据学出来的** —— 金标准免费(词典+向量),零人工标注。

    训练集(全自动构造):
      正例(同义 label=1):ECDICT 命中 **AND** 向量 cos>0.72(两个独立信号双确认)
                          ∪ concepts.json 里 AI 已判 same 的对。
      负例(非同义 label=0):随机配对 ∪ 时间共现>0 但向量 cos<0.40(feynman↔原子 类)。
    ⚠ 正例用「词典 AND 向量」双确认、负例用「共现但向量远」—— 让**每个信号都有独立的正负样本**,
      学出的权重才不偏向单一信号(否则「正例都是词典选的 → 学出来 D 权重最大」是循环论证)。
    模型:逻辑回归 P=σ(w·[T,S,O,D]+b),numpy 梯度下降(4 维,秒级)。
    输出:学到的 W(非负,几何平均语义)+ 按 F1 最优选的 τ → fusion-weights.json。
    """
    import numpy as np
    c = _db()
    ensure_fresh(c)
    book_days, mine_days = defaultdict(set), defaultdict(set)
    for ts, ch, tj, f in c.execute("SELECT ts, channel, terms, file FROM events"):
        day = int(ts // 86400)
        try:
            terms = set(json.loads(tj))
        except Exception:
            continue
        tgt = book_days if (ch in ("lookup", "read", "highlight", "check") and f) else mine_days
        for t in terms:
            tgt[t].add(day)
    ndays = len({int(ts // 86400) for ts, in c.execute("SELECT ts FROM events")}) or 1
    day_of = {**book_days, **mine_days}
    mine_cjk = [t for t in mine_days if _CJK.search(t) and not _KANA.search(t)]
    en_all = [w for w in book_days if re.match(r"^[a-z\-']{3,}$", w) and w not in _STOP_EN]
    en_all += [w for w in mine_days if re.match(r"^[a-z\-']{3,}$", w) and w not in _STOP_EN]
    en_all = list(dict.fromkeys(en_all))
    # 候选正/负对
    pos, neg = [], []
    for w in en_all:
        zs = _ecdict_zh(w) & set(mine_cjk)
        for z in zs:
            pos.append((w, z, "dict"))              # 词典命中 → 待向量二次确认
    al = _concepts()
    for k, v in al.items():
        if k.isascii():
            pos.append((k, v, "concept"))           # AI 已判 same
    # ★embedding 只用**已缓存**的术语(fit 是校准,不为造负例狂调 API;缓存由 --concepts 填)
    emb = _emb_load()
    cached = set(emb)
    pos = [(a_, b_, sr) for (a_, b_, sr) in pos if a_ in cached and b_ in cached]
    en_c = [w for w in en_all if w in cached]
    mine_c = [z for z in mine_cjk if z in cached]
    import random as _rnd
    rng = _rnd.Random(42)                            # 固定种子(可复现)
    if en_c and mine_c:
        for _ in range(min(400, len(en_c) * 3)):
            neg.append((rng.choice(en_c), rng.choice(mine_c), "rand"))
    fctx = {"day_of": day_of, "ndays": ndays, "emb": emb, "dict_fn": _ecdict_zh}

    def _feat(a_, b_):
        _, sg = fuse_score(a_, b_, fctx)             # 复用信号计算(权重此刻无所谓,只取 sigs)
        return [sg["T"], sg["S"], sg["O"], sg["D"]]

    X, y = [], []
    for a_, b_, src in pos:
        f_ = _feat(a_, b_)
        if src == "dict" and f_[1] < 0.34:   # 词典命中但向量远(preface↔向量类)→ 当 **D=1 的负例**(破 D 泄漏,审查 #3:负例也含 D=1,D 不能靠抄标签)
            X.append(f_); y.append(0)
            continue
        X.append(f_); y.append(1)
    for a_, b_, src in neg:
        f_ = _feat(a_, b_)
        if f_[1] > 0.5:                              # 随机对里偶尔真同义 → 剔除(别污染负例)
            continue
        X.append(f_); y.append(0)
    X = np.array(X, float); y = np.array(y, float)
    if len(y) < 30 or y.sum() < 8 or (len(y) - y.sum()) < 8:
        return {"ok": False, "error": "样本不足(正 %d 负 %d)" % (int(y.sum()), int(len(y) - y.sum()))}
    # ★hold-out 20%:训练集 F1 天生虚高(正负例好分),**留出集 F1 才是真实泛化**(过拟合检查)
    idx = np.arange(len(y)); np.random.RandomState(7).shuffle(idx)
    cut = int(len(y) * 0.8); tr, te = idx[:cut], idx[cut:]
    Xtr, ytr = X[tr], y[tr]
    # 逻辑回归(权重**非负**约束:融合是「证据累积」,负权重无意义)
    w = np.array([1.0, 1.2, 1.0, 0.8]); b = -1.5
    lr, lam = 0.3, 5e-3                               # L2 稍强(防 4 维小样本过拟合)
    for _ in range(3000):
        z = Xtr @ w + b
        pr = 1 / (1 + np.exp(-z))
        g = pr - ytr
        w -= lr * (Xtr.T @ g / len(ytr) + lam * w)
        b -= lr * g.mean()
        w = np.clip(w, 0, None)                      # 非负
    Wd = {"T": float(w[0]), "S": float(w[1]), "O": float(w[2]), "D": float(w[3])}
    # 上线打分器 = fuse_score 的几何平均 P(不是 logistic pr),τ 在此空间选
    ps = np.array([np.prod([_FUSION_EPS + Wd[k] * X[i][j] for j, k in enumerate(("T","S","O","D"))]) ** 0.25
                   for i in range(len(y))])
    def _f1(tau, mask):
        pred = (ps[mask] >= tau).astype(float); yy = y[mask]
        tp = ((pred==1)&(yy==1)).sum(); fp=((pred==1)&(yy==0)).sum(); fn=((pred==0)&(yy==1)).sum()
        pr_=tp/(tp+fp+1e-9); rc=tp/(tp+fn+1e-9); return 2*pr_*rc/(pr_+rc+1e-9)
    best_tau, best_f1 = _FUSION_TAU, -1
    for tau in np.linspace(0.15, 0.8, 40):           # τ 只在**训练集**上选(不偷看留出集)
        f = _f1(tau, tr)
        if f > best_f1: best_f1, best_tau = f, float(tau)
    res = {"ok": True, "W": Wd, "tau": round(best_tau, 3),
           "train_f1": round(float(best_f1), 3), "holdout_f1": round(float(_f1(best_tau, te)), 3),
           "n_pos": int(y.sum()), "n_neg": int(len(y) - y.sum())}
    if not dry:
        _FUSION_WFILE.write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
        _load_weights()
    c.close()
    return res


def fuse_score(A, B, ctx):
    """四信号融合 → P(A,B 是同一概念的可能性)。ctx = {day_of, ndays, emb, dict_fn}。"""
    T = _sig_time(A, B, ctx["day_of"], ctx["ndays"])
    S = max(0.0, (_cos(ctx["emb"].get(A), ctx["emb"].get(B)) - 0.5) * 2)
    O = _sig_parts(A, B)
    D = 1.0 if (A.isascii() and B in (ctx["dict_fn"](A) or set())) else 0.0
    sigs = {"T": T, "S": S, "O": O, "D": D}
    prod = 1.0
    for k, w in _FUSION_W.items():
        prod *= (_FUSION_EPS + w * sigs[k])
    P = prod ** (1.0 / len(_FUSION_W))
    return P, sigs


def build_concepts(dry=False, max_pairs=120):   # 120:一次判完(实测 131 个候选;vector↔向量 曾被 top50 挤掉)
    """★跨语言概念归一 L2(用户要求:「相同含义的不同语言单词必须在数据库中被看作同一单词」)。

    **数据实证**(2026-07-17):库里 proof↔证明、theorem↔定理、vector↔向量 同时存在
    (书是英/日原文,笔记和提问是中文)—— 同一知识点的焦点被劈成两半。

    三步(分层:高召回候选 → 高精度判定):
      ① **候选 = 词典**(ECDICT 释义 ∩ 我的中文术语):95 对,含 change↔变化/compute↔计算
         这种真对应,也含 also↔并且 的噪声 —— 高召回低精度,正好当候选。
      ② **排序 = 时间共现 × 双边热度**:同一天在「书」和「我的笔记/提问」里都出现过的对优先
         —— 用户设计「靠保存时间建立联系」。⚠ 时间**只排序不判定**:实测纯共现候选是
         「feynman↔原子」「preface↔向量」这种垃圾(高频词互撞)。
      ③ **AI 判词义**(gemini,一次批量):只收明确同义;**严防过度合并**——包含/上下位
         (统计 vs 人口动态统计、向量 vs 向量空间)、同领域相关(证明 vs 定理)一律 false。
    结果写 concepts.json 永久缓存;norm_key 查表 O(1)(热路径不调 AI)。
    """
    c = _db()
    ensure_fresh(c)
    # 收集两侧术语 + 各自出现的天
    book_days, mine_days = defaultdict(set), defaultdict(set)
    for ts, ch, tj, f in c.execute("SELECT ts, channel, terms, file FROM events"):
        day = int(ts // 86400)
        try:
            terms = set(json.loads(tj))
        except Exception:
            continue
        tgt = book_days if (ch in ("lookup", "read", "highlight", "check") and f) else mine_days
        for t in terms:
            tgt[t].add(day)
    mine_cjk = {t for t in mine_days if _CJK.search(t) and not _KANA.search(t)}
    ndays = len({int(ts // 86400) for ts, in c.execute("SELECT ts FROM events")}) or 1
    book_terms = {t for t in book_days if not (_CJK.search(t) and not _KANA.search(t))}   # 外语侧(英/日)
    day_of = {**book_days, **mine_days}
    # ── 候选两路(统一进融合打分):① 词典召回(短词强项) ② 高共现召回(长句/词组,词典查不到)──
    en_all = {}
    for src in (book_days, mine_days):
        for w, days in src.items():
            if re.match(r"^[a-z\-']{3,}$", w) and w not in _STOP_EN:
                en_all.setdefault(w, set()).update(days)
    pair_set = set()
    for w in en_all:                                   # ① 词典对
        for z in (_ecdict_zh(w) & mine_cjk):
            pair_set.add((w, z))
    for a_t in list(book_terms)[:400]:                 # ② 高共现对(NPMI 粗筛;长句/词组靠这条)
        best = []
        for b_t in mine_cjk:
            npmi = _sig_time(a_t, b_t, day_of, ndays)
            if npmi > 0.15:
                best.append((npmi, b_t))
        for _, b_t in sorted(best, reverse=True)[:3]:
            pair_set.add((a_t, b_t))
    if not pair_set:
        return {"ok": True, "pairs": 0, "merged": 0, "note": "没有跨语言候选"}
    # ── 融合打分:每对算 P(时间×向量×部件×词典),P>τ 才送 AI ──────────────────
    allterms = sorted({x for pr in pair_set for x in pr})
    emb = {} if dry else _emb_get(allterms, _emb_load())
    fctx = {"day_of": day_of, "ndays": ndays, "emb": emb, "dict_fn": _ecdict_zh}
    scored = []
    for a_t, b_t in pair_set:
        P, sigs = (0.0, {}) if dry else fuse_score(a_t, b_t, fctx)
        if dry or P >= _FUSION_TAU:
            scored.append((P, a_t, b_t, sigs))
    scored.sort(key=lambda x: -x[0])
    if dry:
        return {"ok": True, "n_pairs": len(pair_set), "sample": sorted(pair_set)[:24]}
    grp = {}
    for P, w, z, sg in scored[:max_pairs * 2]:          # 按英文词分组(一词多义:AI 选一个或全拒)
        grp.setdefault(w, [])
        if z not in grp[w]:
            grp[w].append(z)
    words = list(grp)[:max_pairs]
    top = [(w, grp[w]) for w in words]
    if not top:
        return {"ok": True, "pairs": 0, "merged": 0,
                "note": "融合分都没过 τ=%.2f(候选 %d 对)" % (_FUSION_TAU, len(pair_set))}
    _books = sorted({(f or "").split("/")[-1] for f, in c.execute(
        "SELECT DISTINCT file FROM events WHERE file<>'' AND channel IN ('lookup','read','highlight')")})[:6]
    listing = "\n".join("%d. %s → 候选:%s" % (i + 1, w, " / ".join(zs[:6])) for i, (w, zs) in enumerate(top))
    prompt = (
        "用户在读这些书:%s。\n"
        "下面每行:左边是**这些书里出现的英文词**,右边是从英中词典初筛出的**候选中文词**"
        "(这些中文词来自用户自己的中文笔记/提问,含噪声)。\n"
        "对每一行,在候选里**挑出这个英文词在上述学习语境中真正对应的那一个**中文词;"
        "**没有合适的就整行跳过**(宁可漏,不可错)。\n"
        "⚠ 必须跳过的情况:① 候选只是词典义项之一、但不是这些书的语境里的意思"
        "(例:数学书里 set 是『集合』不是『设定/结果』;element 是『元素』不是『成分』);"
        "② 包含/上下位关系(『向量』vs『向量空间』不是同义);③ 同领域但不同义"
        "(『证明』vs『定理』);④ 你拿不准的。\n"
        "**只输出 JSON**:{\"pairs\":[{\"i\":行号,\"zh\":\"选中的那个候选中文词(必须原样来自候选列表)\"}]}\n\n"
        % ("、".join(_books) or "(未知)") + listing)
    try:
        sys.path.insert(0, "/home/bwicarus/webapp")   # assistant.py 在 webapp(脚本进程要显式加)
        import assistant as A
        raw = A._gemini_text(prompt, max_tokens=2000, think=False) or ""
    except Exception as ex:
        return {"ok": False, "error": "AI 判定失败:%s" % str(ex)[:80]}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"ok": False, "error": "AI 没给出 JSON", "raw": raw[:200]}
    try:
        got = json.loads(m.group(0)).get("pairs") or []
    except Exception:
        return {"ok": False, "error": "AI 的 JSON 解析失败", "raw": raw[:200]}
    alias = dict(_concepts())
    merged = []
    for r in got:
        try:
            w, zs = top[int(r["i"]) - 1]
            zh = str(r.get("zh") or "").strip()
            if not zh or zh not in zs:
                continue                      # ★概念名**必须原样来自候选**(不让 AI 自己起名:
                                              #   实测它会起「设定」这种在语境里错的名字)
            ck = _t2s(unicodedata.normalize("NFKC", zh).translate(_JP_TRANS)).lower()
            wk = _t2s(unicodedata.normalize("NFKC", w).translate(_JP_TRANS)).lower()
            if not ck or wk == ck:
                continue
            alias[wk] = ck                    # ★**只映射英文词 → 中文概念**;中文词一律不动
                                              #   (防误伤:上一版把「显示」也映射到「指示」,
                                              #    近义中文词被合并是过度合并的温床)
            merged.append("%s→%s" % (w, zh))
        except Exception:
            continue
    CONCEPTS.write_text(json.dumps({"alias": alias, "updated": int(time.time()),
                                    "n_concepts": len(set(alias.values()))},
                                   ensure_ascii=False, indent=1), "utf-8")
    global _CONCEPTS
    _CONCEPTS = alias
    c.close()
    return {"ok": True, "judged": len(top), "merged": len(merged), "aliases": len(alias),
            "examples": merged[:12]}


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
             "check": import_checks(c), "read": import_dwell(c), "raw": import_raw(c),
             "anki_lapse": import_anki_lapses(c), "note": import_notes(c)}
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


try:
    _load_weights()          # 有学出来的权重就用它(fit_weights 产物)
except Exception:
    pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="删库重导全量(分词算法改版后用)")
    ap.add_argument("--domain-dict", action="store_true", help="重建领域词典(KG/目录/查词 → 让分词认长词组)")
    ap.add_argument("--fit", action="store_true", help="用词典金标准反向学习融合权重 → fusion-weights.json")
    ap.add_argument("--concepts", action="store_true", help="跨语言概念归一:AI 判定同义对 → concepts.json")
    ap.add_argument("--dry", action="store_true", help="配合 --concepts:只看候选,不调 AI")
    a = ap.parse_args()
    if a.fit:
        print("fit:", json.dumps(fit_weights(dry=a.dry), ensure_ascii=False))
        raise SystemExit
    if a.domain_dict:
        print("domain:", json.dumps(build_domain_dict(), ensure_ascii=False))
        run(rebuild=True)      # 词典变了 → 分词变了 → 重算
        raise SystemExit
    if a.concepts:
        print("concepts:", json.dumps(build_concepts(dry=a.dry), ensure_ascii=False)[:600])
        if not a.dry:
            run(rebuild=True)      # 别名变了 → 归一键变了 → 重算画像
        raise SystemExit
    st, total = run(rebuild=a.rebuild)
    print("imported:", st, "| total events:", total, "| focus →", FOCUS)
