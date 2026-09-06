"""公共目录：Wikidata 编号 / 三语标签 / 实体值关系，本地保存；节点绑定编号后自动生成本地关系。

两条进货路径，同一张表：
- ``import_minimal_index``  读 Codex 侧 wikidata_measure.py 产出的 minimal-index.jsonl(.gz)
  （行格式 {id, labels{lang}, descriptions{lang}, aliases{lang:[...]}, relations[[prop,target,rank]]}），
  流式、可按 qid 白名单 / 语言过滤，不把 1 亿实体全灌进来。
- ``fetch_entity``           单个编号在线取 Special:EntityData（按需、带缓存），全量下载没完成前不阻塞。

铁律（文档 §三）：Wikidata 的分类 / 组成 / 一般因果只是**相关线索**，自动生成的关系 origin=wikidata、
类型按下表映射，**绝不生成 prereq**；教学前置只来自带原文依据的本地登记。
"""
from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .store import Ledger, dumps, loads

# prop → (本地关系类型, 方向)。forward: 主体→客体；inverse: 客体→主体（统一到同一种类型名）
PROP_MAP: dict[str, tuple[str, str]] = {
    "P279": ("subclass_of", "forward"),
    "P31": ("instance_of", "forward"),
    "P361": ("part_of", "forward"),
    "P527": ("part_of", "inverse"),        # has part → 客体 part_of 主体
    "P1542": ("causes", "forward"),         # has effect
    "P828": ("causes", "inverse"),          # has cause → 客体 causes 主体
    "P737": ("influenced_by", "forward"),
    "P2283": ("uses", "forward"),
    "P1269": ("facet_of", "forward"),
    "P1889": ("different_from", "forward"),
    "P2579": ("studied_by", "forward"),
    "P2578": ("studies", "forward"),
    "P1855": ("example", "forward"),
    "P3095": ("practiced_by", "forward"),
}
CLASS_PROPS = ("P279", "P31", "P361")   # 用于"分类路径"向上走
LANGS = ("en", "zh", "ja")
_ZH_FALLBACK = ("zh", "zh-hans", "zh-cn", "zh-hant", "zh-tw", "zh-hk")
ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
SEARCH_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = "bwreader-kj/1 (personal study tool; bwicarus)"


def _pick(d: dict, lang: str) -> str:
    if lang == "zh":
        for k in _ZH_FALLBACK:
            v = d.get(k)
            if v:
                return v if isinstance(v, str) else v.get("value", "")
        return ""
    v = d.get(lang)
    if not v:
        return ""
    return v if isinstance(v, str) else v.get("value", "")


def _aliases(d: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for lang, vals in (d or {}).items():
        key = "zh" if lang.startswith("zh") else lang
        if key not in LANGS:
            continue
        for v in vals or []:
            s = v if isinstance(v, str) else v.get("value", "")
            if s and s not in out.setdefault(key, []):
                out[key].append(s)
    return out


def _entity_tuple(qid: str, labels: dict, descriptions: dict, aliases: dict, now: int, source: str) -> tuple:
    al = _aliases(aliases)
    l_en, l_zh, l_ja = _pick(labels, "en"), _pick(labels, "zh"), _pick(labels, "ja")
    # 其它中文变体标签（简/繁/港台）都当别名收进来：用户输简体"凯莱-哈密顿定理"要能撞到繁体标签的条目
    for k in _ZH_FALLBACK:
        v = labels.get(k)
        v = v if isinstance(v, str) else (v.get("value") if isinstance(v, dict) else None)
        if v and v != l_zh and v not in al.setdefault("zh", []):
            al["zh"].append(v)
    text = " / ".join(x for x in [l_en, l_zh, l_ja] + sum(al.values(), []) if x)
    return (qid, l_en, l_zh, l_ja, _pick(descriptions, "en"), _pick(descriptions, "zh"), _pick(descriptions, "ja"),
            dumps(al), text, now, source)


_ENTITY_SQL = ("INSERT OR REPLACE INTO public_entities(qid, label_en, label_zh, label_ja, desc_en, desc_zh, desc_ja, aliases_json,"
               " search_text, fetched_at, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)")


def upsert_entity(ledger: Ledger, qid: str, labels: dict, descriptions: dict, aliases: dict,
                  claims: Iterable[tuple[str, str, str]], *, source: str) -> None:
    now = int(time.time())
    row = _entity_tuple(qid, labels, descriptions, aliases, now, source)
    with ledger._lock:
        db = ledger.db
        db.execute(_ENTITY_SQL, row)
        db.execute("DELETE FROM public_claims WHERE qid=?", (qid,))
        for prop, target, rank in claims:
            if rank == "deprecated":
                continue
            db.execute("INSERT OR IGNORE INTO public_claims(qid, prop, target, rank) VALUES(?,?,?,?)", (qid, prop, target, rank or "normal"))
        db.execute("DELETE FROM public_fts WHERE qid=?", (qid,))
        db.execute("INSERT INTO public_fts(qid, labels) VALUES(?,?)", (qid, row[8]))


def rebuild_public_fts(ledger: Ledger) -> int:
    """批量导入后整体重建公共目录的全文索引（逐行维护太慢）。"""
    with ledger._lock:
        db = ledger.db
        db.execute("DELETE FROM public_fts")
        db.execute("INSERT INTO public_fts(qid, labels) SELECT qid, COALESCE(search_text, '') FROM public_entities")
        return int(db.execute("SELECT COUNT(*) FROM public_fts").fetchone()[0])


def entity(ledger: Ledger, qid: str) -> dict | None:
    r = ledger.db.execute("SELECT * FROM public_entities WHERE qid=?", (qid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["aliases"] = loads(r["aliases_json"], {})
    d["label"] = r["label_zh"] or r["label_en"] or r["label_ja"] or qid
    d["description"] = r["desc_zh"] or r["desc_en"] or r["desc_ja"] or ""
    return d


def label_of(ledger: Ledger, qid: str) -> str:
    e = entity(ledger, qid)
    return e["label"] if e else qid


def claims_of(ledger: Ledger, qid: str) -> list[tuple[str, str, str]]:
    return [(r["prop"], r["target"], r["rank"]) for r in ledger.db.execute("SELECT prop, target, rank FROM public_claims WHERE qid=?", (qid,))]


def claims_to(ledger: Ledger, qid: str) -> list[tuple[str, str, str]]:
    return [(r["qid"], r["prop"], r["rank"]) for r in ledger.db.execute("SELECT qid, prop, rank FROM public_claims WHERE target=?", (qid,))]


def path_up(ledger: Ledger, qid: str, depth: int = 3) -> list[dict]:
    """沿 subclass_of / instance_of / part_of 向上取分类路径（每层最多取第一条，够 AI 定位）。"""
    out = []
    cur = qid
    seen = {qid}
    for _ in range(depth):
        nxt = None
        for prop in CLASS_PROPS:
            row = ledger.db.execute("SELECT target FROM public_claims WHERE qid=? AND prop=? ORDER BY rank='preferred' DESC LIMIT 1",
                                    (cur, prop)).fetchone()
            if row is not None and row[0] not in seen:
                nxt = (prop, row[0])
                break
        if nxt is None:
            break
        prop, tq = nxt
        seen.add(tq)
        local = ledger.find_by_qid(tq)
        out.append({"qid": tq, "label": label_of(ledger, tq), "via": PROP_MAP.get(prop, (prop, ""))[0],
                    "local_node": local["id"] if local else None})
        cur = tq
    return out


# 检索时压后排的类别（P31）：论文/文章/学位论文/章节 —— 有中文标签的 1233 万实体里大半是论文标题，
# 会把"向量空间"这种概念本体挤出前几名（2026-09-07 实测）。不是删除，只是排序时加罚分。
SEARCH_DEMOTED_CLASSES = frozenset({
    "Q13442814",  # scholarly article
    "Q191067",    # article
    "Q7318358",   # review article
    "Q23927052",  # conference paper
    "Q1266946",   # thesis
    "Q187685",    # doctoral thesis
    "Q1980247",   # chapter
    "Q18918145",  # academic journal article
    "Q58632367",  # preprint
    "Q10885494",  # scientific journal article?
    # 维基媒体自己的页面类型：分类页/消歧义页/列表/模板/项目页 —— 子集里有 120 万分类页、16 万消歧义页（2026-09-07 实测）
    "Q4167836",   # Wikimedia category
    "Q4167410",   # Wikimedia disambiguation page
    "Q13406463",  # Wikimedia list article
    "Q11266439",  # Wikimedia template
    "Q14204246",  # Wikimedia project page
    "Q17442446",  # Wikimedia internal item
    "Q15184295",  # Wikimedia module
    "Q17633526",  # Wikinews article
    # 单个汉字/假名条目：查"熵"会先撞到"CJK 中日韩文字"而不是物理概念
    "Q3595028",   # kanji
    "Q53764738",  # CJK unified ideograph
    "Q1374204",   # Chinese character? (variant)
    "Q54932297",  # CJK character
})
_SEARCH_CANDIDATES = 80


_SQUEEZE_RE = __import__("re").compile(r"[\s\-‐‑‒–—―－·・‧,，.。()（）\[\]【】「」『』:：;；'\"“”‘’]+")


def _squeeze(s: str) -> str:
    """去掉连接符/空格/标点，只留字母数字与汉字假名，用来做不计标点的匹配。"""
    return _SQUEEZE_RE.sub("", s or "").lower()


def _search_score(ledger: Ledger, e: dict, q_lower: str) -> tuple:
    labels = [x for x in (e.get("label_en"), e.get("label_zh"), e.get("label_ja")) if x]
    aliases = [a for vals in (e.get("aliases") or {}).values() for a in vals]
    names = [x.lower() for x in labels + aliases]
    q_sq = _squeeze(q_lower)
    if any(n == q_lower for n in names) or any(_squeeze(n) == q_sq for n in names):
        tier = 0
    elif any(n.startswith(q_lower) for n in names):
        tier = 1
    else:
        tier = 2
    classes = {t for p, t, _ in claims_of(ledger, e["qid"]) if p == "P31"}
    penalty = 3 if classes & SEARCH_DEMOTED_CLASSES else 0
    # 同分时按 Q 号**数值**排：老条目（小号）多是核心概念，新号多是论文/游戏/村镇。字符串比较会让 Q109753558 排在 Q45003 前面（"熵"实锤）。
    try:
        qnum = int(e["qid"][1:])
    except ValueError:
        qnum = 10**12
    return (tier + penalty, len(e.get("label") or ""), qnum)


def search_public(ledger: Ledger, q: str, limit: int = 8) -> list[dict]:
    """公共目录检索：FTS(trigram) 取候选 → 精确标签/别名优先、前缀次之、论文类压后 → 取前 limit。"""
    q = (q or "").strip()
    if not q:
        return []
    db = ledger.db
    qids: list[str] = []

    def add(rows) -> None:
        for r in rows:
            if r[0] not in qids:
                qids.append(r[0])
    # ① 精确标签（三语，走索引）② 前缀（范围查询，走索引；两字词如"宪法""牛顿"靶这条）
    add(db.execute("SELECT qid FROM public_entities WHERE label_zh=? OR label_en=? OR label_ja=? LIMIT 20", (q, q, q)))
    hi = q + "￿"
    for col in ("label_zh", "label_en", "label_ja"):
        add(db.execute(f"SELECT qid FROM public_entities WHERE {col} >= ? AND {col} < ? LIMIT 30", (q, hi)))
    # ③ 全文（trigram 要 ≥3 字）
    if len(q) >= 3:
        try:
            add(db.execute("SELECT qid FROM public_fts WHERE public_fts MATCH ? ORDER BY rank LIMIT ?",
                           ('"' + q.replace('"', '""') + '"', _SEARCH_CANDIDATES)))
        except Exception:
            pass
    # ④ 什么都没有才全表 LIKE（慢路径，只在短词且无命中时）
    if not qids and len(q) <= 2:
        like = f"%{q}%"
        add(db.execute(
            "SELECT qid FROM public_entities WHERE label_zh LIKE ? OR label_en LIKE ? OR label_ja LIKE ? LIMIT ?",
            (like, like, like, _SEARCH_CANDIDATES)))
    # ⑤ 仍没有：去掉连接符/空格后再试一次（"凯莱-哈密顿定理" vs "凯莱–哈密顿定理"），再逐字缩短前缀（"牛顿第二定律"→"牛顿第二"）
    if not qids:
        squeezed = _squeeze(q)
        if squeezed != q and len(squeezed) >= 2:
            return search_public(ledger, squeezed, limit)
        for n in range(len(q) - 1, 1, -1):
            prefix = q[:n]
            hi2 = prefix + "￿"
            for col in ("label_zh", "label_en", "label_ja"):
                add(db.execute(f"SELECT qid FROM public_entities WHERE {col} >= ? AND {col} < ? LIMIT 30", (prefix, hi2)))
            if len(prefix) >= 3:   # 别名（含简繁变体）只在全文里，前缀范围查询看不到
                try:
                    add(db.execute("SELECT qid FROM public_fts WHERE public_fts MATCH ? ORDER BY rank LIMIT 30",
                                   ('"' + prefix.replace('"', '""') + '"',)))
                except Exception:
                    pass
            if qids:
                break
    q_lower = q.lower()
    scored = []
    for qid in qids[:_SEARCH_CANDIDATES * 2]:
        e = entity(ledger, qid)
        if e:
            scored.append((_search_score(ledger, e, q_lower), e))
    scored.sort(key=lambda x: x[0])
    out = []
    for _, e in scored[:limit]:
        local = ledger.find_by_qid(e["qid"])
        # 给 AI 的候选只留定位所需：简述截 100 字、分类路径 1 层（完整的等 kj_node 再看）—— 2026-09-07 离线实测
        # 8 个候选带 2 层路径要 690 token，精简后约减半。
        out.append({"qid": e["qid"], "label": e["label"], "description": (e["description"] or "")[:100],
                    "local_node": local["id"] if local else None, "path": path_up(ledger, e["qid"], 1)})
    return out


def auto_relations_for(ledger: Ledger, node_id: str, qid: str) -> list[dict]:
    """节点绑定 qid 后：凡公共关系另一端也已绑定本地节点，生成本地关系（origin=wikidata）。"""
    out: list[dict] = []
    for prop, target, _rank in claims_of(ledger, qid):
        if prop not in PROP_MAP:
            continue
        other = ledger.find_by_qid(target)
        if other is None or other["id"] == node_id:
            continue
        rtype, direction = PROP_MAP[prop]
        frm, to = (node_id, other["id"]) if direction == "forward" else (other["id"], node_id)
        out.append({"from": frm, "to": to, "type": rtype, "origin": "wikidata", "derived_qid": qid,
                    "evidence": f"Wikidata {qid} {prop} {target}"})
    for src, prop, _rank in claims_to(ledger, qid):
        if prop not in PROP_MAP:
            continue
        other = ledger.find_by_qid(src)
        if other is None or other["id"] == node_id:
            continue
        rtype, direction = PROP_MAP[prop]
        frm, to = (other["id"], node_id) if direction == "forward" else (node_id, other["id"])
        out.append({"from": frm, "to": to, "type": rtype, "origin": "wikidata", "derived_qid": qid,
                    "evidence": f"Wikidata {src} {prop} {qid}"})
    return out


# ── 进货 ────────────────────────────────────────────────────────────────────
def import_minimal_index(ledger: Ledger, path: str | Path, *, only_qids: set[str] | None = None,
                         require_lang: str | None = None, limit: int | None = None,
                         keep_if: Callable[[dict], bool] | None = None) -> dict:
    """流式导入 minimal-index.jsonl(.gz)。不设过滤会很大 —— 调用方负责给 only_qids / require_lang。"""
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    seen = kept = claims_n = 0
    now = int(time.time())
    ent_batch: list[tuple] = []
    claim_batch: list[tuple] = []
    qid_batch: list[tuple] = []
    t0 = time.time()

    def flush() -> None:
        nonlocal ent_batch, claim_batch, qid_batch
        if not ent_batch:
            return
        db = ledger.db
        db.execute("BEGIN")
        db.executemany("DELETE FROM public_claims WHERE qid=?", qid_batch)
        db.executemany(_ENTITY_SQL, ent_batch)
        db.executemany("INSERT OR IGNORE INTO public_claims(qid, prop, target, rank) VALUES(?,?,?,?)", claim_batch)
        db.execute("COMMIT")
        ent_batch, claim_batch, qid_batch = [], [], []

    with ledger._lock:
        ledger.db.execute("PRAGMA pub.synchronous=OFF")
        try:
            with opener(p, "rt", encoding="utf-8") as fh:  # type: ignore[arg-type]
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    seen += 1
                    qid = row.get("id") or ""
                    if not qid.startswith("Q"):
                        continue
                    if only_qids is not None and qid not in only_qids:
                        continue
                    labels = row.get("labels") or {}
                    if require_lang and not _pick(labels, require_lang):
                        continue
                    if keep_if is not None and not keep_if(row):
                        continue
                    ent_batch.append(_entity_tuple(qid, labels, row.get("descriptions") or {}, row.get("aliases") or {}, now, "dump"))
                    qid_batch.append((qid,))
                    for r in row.get("relations") or []:
                        if len(r) >= 2 and (len(r) < 3 or r[2] != "deprecated"):
                            claim_batch.append((qid, r[0], r[1], r[2] if len(r) > 2 else "normal"))
                            claims_n += 1
                    kept += 1
                    if len(ent_batch) >= 5000:
                        flush()
                        if kept % 200_000 == 0:
                            print(f"[wikidata-import] kept={kept:,} claims={claims_n:,} seen={seen:,} {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
                    if limit and kept >= limit:
                        break
            flush()
            print(f"[wikidata-import] rows done kept={kept:,} claims={claims_n:,}; rebuilding FTS…", file=sys.stderr, flush=True)
            fts = rebuild_public_fts(ledger)
        finally:
            if ledger.db.in_transaction:   # flush 半途抛错时把事务收掉，否则下面的 PRAGMA 会再抛一个把真错误盖住
                ledger.db.execute("ROLLBACK")
            ledger.db.execute("PRAGMA pub.synchronous=NORMAL")
    return {"seen": seen, "kept": kept, "claims": claims_n, "fts_rows": fts, "seconds": round(time.time() - t0, 1)}


def _http_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_entity_json(doc: dict, qid: str) -> dict:
    ent = (doc.get("entities") or {}).get(qid) or {}
    claims = []
    for prop, stmts in (ent.get("claims") or {}).items():
        for s in stmts or []:
            val = ((s.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(val, dict) and "id" in val:
                claims.append((prop, val["id"], s.get("rank") or "normal"))
    return {"labels": ent.get("labels") or {}, "descriptions": ent.get("descriptions") or {},
            "aliases": ent.get("aliases") or {}, "claims": claims}


def fetch_entity(ledger: Ledger, qid: str, *, timeout: float = 15.0, refresh: bool = False,
                 fetcher: Callable[[str, float], dict] | None = None) -> dict | None:
    """在线取一个编号进本地目录（已有且不 refresh 就直接用本地）。"""
    if not refresh:
        e = entity(ledger, qid)
        if e is not None:
            return e
    doc = (fetcher or _http_json)(ENTITY_URL.format(qid=qid), timeout)
    parsed = parse_entity_json(doc, qid)
    if not parsed["labels"] and not parsed["claims"]:
        return None
    upsert_entity(ledger, qid, parsed["labels"], parsed["descriptions"], parsed["aliases"], parsed["claims"], source="online")
    return entity(ledger, qid)


def search_online(q: str, *, langs: Iterable[str] = ("zh", "en", "ja"), limit: int = 6, timeout: float = 15.0,
                  fetcher: Callable[[str, float], dict] | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for lang in langs:
        params = {"action": "wbsearchentities", "search": q, "language": lang, "uselang": lang,
                  "format": "json", "limit": str(limit), "type": "item"}
        try:
            doc = (fetcher or _http_json)(SEARCH_URL + "?" + urllib.parse.urlencode(params), timeout)
        except Exception:
            continue
        for hit in doc.get("search") or []:
            qid = hit.get("id")
            if not qid or qid in seen:
                continue
            seen.add(qid)
            out.append({"qid": qid, "label": hit.get("label") or qid, "description": hit.get("description") or "", "lang": lang})
        if len(out) >= limit:
            break
    return out[:limit]
