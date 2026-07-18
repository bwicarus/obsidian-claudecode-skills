#!/usr/bin/env python3
"""build_auto_stopwords.py — 从**书库自己**统计通用语,自动生成停用词(用户设计 2026-07-19)。

思路(用户原话):「找很多不同类型的同语言书,把重复最多的那些找出来,这些肯定是非知识点的
通用语」。即 **DF(document frequency)**:一个词出现在同语言的绝大多数书里 → 它是通用语,
不是知识点。这比手工穷举停用词表稳健得多,而且加新书就自动更新。

三条护栏:
① **按语言分组**,且语言由**正文字符分布**判定(书名/路径不可靠——路径里的中文目录名
   曾把 Feynman 英文原版误判成中文书,于是 new/value 混进了中文榜)。
② **书太少不生成**(MIN_BOOKS):日语现在只有 3 本且都偏资格考试,3/3 的词里混着
   「予防/技能/評価」这种真术语——样本不足时 DF 必然误杀。等书够了自动启用。
③ **保护名单**:领域词典(domain-terms)/ KG 节点名 / 概念图节点名 里的词一律不滤——
   它们是用户真实在学的东西。

⚠ 只滤**完整词**(消费端做精确匹配)。用户点出的关键:复合词里常夹着通用语——
   "平均寿命"含"平均"、"vector space"含"space"——滤单词绝不能连累复合词。
   抽词器本身长词优先(_dedup_nest),复合词存在时短词会被吃掉,两边配合才安全。

out: state/attention/auto-stopwords.json    CLI: [--write] [--ratio R] [--show N]
"""
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import attention_profile as AP  # noqa: E402

SEARCH_DB = config.PROJECT_DIR / "state" / "pdf-search.db"
OUT = config.PROJECT_DIR / "state" / "attention" / "auto-stopwords.json"
ALERT = config.PROJECT_DIR / "state" / "attention" / "stopwords-alert.json"
DOMAIN = config.PROJECT_DIR / "state" / "attention" / "domain-terms.json"
EMERGENT = config.PROJECT_DIR / "state" / "attention" / "emergent-graph.json"
KG_DIR = config.PROJECT_DIR / "knowledge_graph"

MIN_BOOKS = 8          # 该语言至少这么多本书才敢统计(样本不足 → DF 误杀真术语)
# DF/书数 ≥ 此比例 = 通用语。**按语言分别定**——阈值能多激进,取决于该语言书库的
# **领域多样性**:书跨的领域越杂,高 DF 就越确定是通用语;书都挤在一个领域,该领域的
# 核心术语也会跨书高频。实测(测试当场抓到):
#   zh 12 本跨费恩曼/CSAPP/UML/分子生物学/HTTP/线代… → 0.6 很干净;
#   en 8 本里 6 本线代物理 → 0.6 把 eigenvalue/determinant/inverse/invertible 全滤了。
# 激进档留给多样性够的语言,其余保守 + 复活赛兜底(用户设计的两段式)。
RATIO_BY_LANG = {"zh": 0.60, "en": 0.85}
RATIO = 0.75           # 未列出的语言用它
MIN_PAGES = 30         # 太薄的书不参与(目录/习题册)
SAMPLE_PAGES = 60      # 每本均匀抽样页数(够代表全书用词,又不至于跑很久)

_KANA = re.compile(r"[ぁ-んァ-ヶ]")
_HAN = re.compile(r"[一-鿿]")
_LAT = re.compile(r"[A-Za-z]")


def _lang_of(sample):
    """按**正文**字符分布判语言(不看书名/路径)。"""
    blob = "".join((p or "")[:1500] for p in sample[:20])
    kana, han, lat = len(_KANA.findall(blob)), len(_HAN.findall(blob)), len(_LAT.findall(blob))
    if kana > max(20, han * 0.05):
        return "ja"
    if han > lat * 0.15:
        return "zh"
    return "en"


REVIVED = config.PROJECT_DIR / "state" / "attention" / "revived-terms.json"


def _protected():
    """保护名单:领域词典 + KG 节点名 + 概念图节点名 + 复活赛捞回来的词(绝不再滤)。

    返回 (keep, errors)。**必须把错误报上去**——原来四个来源各自 `except: pass`,
    读不到就静默当空集:实测一旦路径错位(CLAUDE_PROJECT 没传给 systemd 就会),
    保护名单从 1938 塌成 0,而 build 照常**全量覆盖写**,集合/空间/证明/子空间/
    向量空间/感染症/matrix 连同刚花 AI 判决复活回来的词一起进停用表。
    更糟的是不可逆:停用表下轮能恢复,但 events.terms 是抽取时快照,误滤期间入库的
    事件已经把那些词剔掉了,源一归档就再也重抽不了。故:**瞎了就不许写盘**。
    """
    keep, errors = set(), []
    try:   # 复活赛判定为术语的词:永久保护(它们正是被激进阈值误伤的那批)
        for t in (json.loads(REVIVED.read_text("utf-8")).get("terms") or {}):
            keep.add(str(t).strip().lower())
    except FileNotFoundError:
        pass                       # 还没跑过复活赛 = 正常,不算故障
    except Exception as e:
        errors.append(f"revived: {e}")
    try:
        d = json.loads(DOMAIN.read_text("utf-8"))
        for t in (d if isinstance(d, list) else (d.get("terms") or list(d.keys()))):
            keep.add(str(t).strip().lower())
    except Exception as e:
        errors.append(f"domain-terms: {e}")      # 领域词典是主力保护源,缺了就是故障
    try:
        g = json.loads(EMERGENT.read_text("utf-8"))
        for n in (g.get("nodes") or {}).values():
            if n.get("surface"):
                keep.add(str(n["surface"]).strip().lower())
    except FileNotFoundError:
        pass                       # 概念图可能还没长出来
    except Exception as e:
        errors.append(f"emergent-graph: {e}")
    if not KG_DIR.is_dir():
        errors.append(f"kg-dir 不存在: {KG_DIR}")
    else:
        for p in KG_DIR.glob("*.json"):
            if ".bak." in p.name:
                continue
            try:
                for n in (json.loads(p.read_text("utf-8")).get("nodes") or []):
                    if n.get("name") and n.get("level") == 2:
                        keep.add(str(n["name"]).strip().lower())
            except Exception as e:
                errors.append(f"kg/{p.name}: {e}")
    return keep, errors


def build(ratio=RATIO, write=False, show=0):
    if not SEARCH_DB.exists():
        return {"ok": False, "error": "没有 pdf-search.db(先跑 scripts/build_search_index.py)"}
    # ⚠ 统计必须绕过**本脚本自己的产物**:抽词器已经在用上一轮的停用词表,不关掉的话
    #   那些词不再出现在样本里 → 本轮统计不到 → 全量覆盖写就把上一轮成果冲掉(实测
    #   ratio=0.85 从 65/85 坍缩到 18/22)。这里临时关掉,统计的永远是**原始**词频。
    _orig_sw = AP._auto_stopwords
    AP._auto_stopwords = lambda: frozenset()
    try:
        return _build_inner(ratio, write, show)
    finally:
        AP._auto_stopwords = _orig_sw


def _build_inner(ratio, write, show):
    c = sqlite3.connect(str(SEARCH_DB))
    books = [f for (f,) in c.execute("SELECT file FROM pages_data GROUP BY file "
                                     "HAVING COUNT(*)>=?", (MIN_PAGES,))]
    df = defaultdict(set)
    lang_books = defaultdict(set)
    for b in books:
        pages = [r[0] for r in c.execute("SELECT body FROM pages_data WHERE file=? ORDER BY page", (b,))]
        if not pages:
            continue
        step = max(1, len(pages) // SAMPLE_PAGES)
        sample = pages[::step][:SAMPLE_PAGES]
        lang = _lang_of(sample)
        lang_books[lang].add(b)
        seen = set()
        for body in sample:
            for t in AP.extract_terms((body or "")[:3000], lang=(["ja"] if lang == "ja" else [])):
                seen.add(t)
        for t in seen:
            df[t].add(b)
    c.close()

    keep, perrors = _protected()
    out, skipped, report = {}, {}, {}
    for lang, bs in lang_books.items():
        n = len(bs)
        if n < MIN_BOOKS:
            skipped[lang] = n          # 样本不足:宁可不滤,也不误杀真术语
            continue
        _r = RATIO_BY_LANG.get(lang, ratio)
        need = max(2, int(round(n * _r)))
        words = sorted((t for t, s in df.items() if len(s & bs) >= need and t.lower() not in keep),
                       key=lambda t: (-len(df[t] & bs), t))
        out[lang] = words
        report[lang] = {"books": n, "need": need, "n": len(words), "ratio": _r}
    res = {"ok": True, "built": int(time.time()), "ratio": ratio,
           "min_books": MIN_BOOKS, "by_lang": report,
           "skipped_langs": skipped, "protected_n": len(keep), "words": out}

    # ── 写盘前三道闸(fail-closed:宁可这轮不更新,也不能在瞎了的状态下覆盖写)──
    prev = {}
    try:
        prev = json.loads(OUT.read_text("utf-8"))
    except Exception:
        prev = {}
    blocks = []
    if perrors:
        blocks.append("保护名单来源读取失败:" + "; ".join(perrors[:4]))
    prev_keep = int(prev.get("protected_n") or 0)
    if prev_keep and len(keep) < prev_keep * 0.9:
        blocks.append(f"保护名单缩水:{prev_keep} → {len(keep)}(超过 10%)")
    prev_n = sum(len(v) for v in (prev.get("words") or {}).values())
    new_n = sum(len(v) for v in out.values())
    if prev_n and new_n > prev_n * 1.25:
        blocks.append(f"停用表暴涨:{prev_n} → {new_n}(超过 25%)")
    res["blocked"] = blocks
    if blocks:
        res["ok"] = False
        try:   # 告警落盘,控制面板/仪表盘可见;**不动 auto-stopwords.json**
            ALERT.write_text(json.dumps(
                {"ts": int(time.time()), "blocks": blocks, "protected_n": len(keep),
                 "prev_protected_n": prev_keep, "new_words": new_n, "prev_words": prev_n},
                ensure_ascii=False, indent=1), "utf-8")
        except Exception:
            pass
        return res
    try:
        ALERT.unlink()       # 恢复正常 → 清掉旧告警
    except OSError:
        pass
    if write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(f".{os.getpid()}.tmp")   # tmp 名带 pid:两个进程同时跑不会互相踩
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(OUT)
    if show:
        for lang, ws in out.items():
            print(f"\n── {lang}({report[lang]['books']} 本,需 ≥{report[lang]['need']} 本)"
                  f" 共 {len(ws)} 词 ──")
            print("  ", "、".join(ws[:show]))
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--ratio", type=float, default=RATIO)
    ap.add_argument("--show", type=int, default=40)
    a = ap.parse_args()
    r = build(ratio=a.ratio, write=a.write, show=a.show)
    if not r.get("ok"):
        if r.get("blocked"):
            print("⛔ 拒绝写盘(fail-closed),原表保持不变:")
            for b in r["blocked"]:
                print("   -", b)
            print(f"   告警已写 {ALERT}")
            sys.exit(2)
        print("失败:", r.get("error"))
        sys.exit(1)
    print("\n统计:", json.dumps(r["by_lang"], ensure_ascii=False),
          "| 样本不足跳过:", r["skipped_langs"], "| 已落盘" if a.write else "| dry-run")
