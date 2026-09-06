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


def upsert_entity(ledger: Ledger, qid: str, labels: dict, descriptions: dict, aliases: dict,
                  claims: Iterable[tuple[str, str, str]], *, source: str) -> None:
    now = int(time.time())
    al = _aliases(aliases)
    with ledger._lock:
        db = ledger.db
        db.execute(
            "INSERT OR REPLACE INTO public_entities(qid, label_en, label_zh, label_ja, desc_en, desc_zh, desc_ja, aliases_json, fetched_at, source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (qid, _pick(labels, "en"), _pick(labels, "zh"), _pick(labels, "ja"),
             _pick(descriptions, "en"), _pick(descriptions, "zh"), _pick(descriptions, "ja"), dumps(al), now, source))
        db.execute("DELETE FROM public_claims WHERE qid=?", (qid,))
        for prop, target, rank in claims:
            if rank == "deprecated":
                continue
            db.execute("INSERT OR IGNORE INTO public_claims(qid, prop, target, rank) VALUES(?,?,?,?)", (qid, prop, target, rank or "normal"))
        text = " / ".join(x for x in [_pick(labels, "en"), _pick(labels, "zh"), _pick(labels, "ja")] + sum(al.values(), []) if x)
        db.execute("DELETE FROM public_fts WHERE qid=?", (qid,))
        db.execute("INSERT INTO public_fts(qid, labels) VALUES(?,?)", (qid, text))


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


def search_public(ledger: Ledger, q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    rows = []
    if len(q) >= 3:
        try:
            rows = ledger.db.execute(
                "SELECT qid FROM public_fts WHERE public_fts MATCH ? ORDER BY rank LIMIT ?",
                ('"' + q.replace('"', '""') + '"', limit)).fetchall()
        except Exception:
            rows = []
    if not rows:
        like = f"%{q}%"
        rows = ledger.db.execute(
            "SELECT qid FROM public_entities WHERE label_en LIKE ? OR label_zh LIKE ? OR label_ja LIKE ? OR aliases_json LIKE ? LIMIT ?",
            (like, like, like, like, limit)).fetchall()
    out = []
    for r in rows:
        e = entity(ledger, r[0])
        if not e:
            continue
        local = ledger.find_by_qid(e["qid"])
        out.append({"qid": e["qid"], "label": e["label"], "description": e["description"],
                    "local_node": local["id"] if local else None, "path": path_up(ledger, e["qid"], 2)})
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
    seen = kept = 0
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
            claims = [(r[0], r[1], r[2] if len(r) > 2 else "normal") for r in row.get("relations") or [] if len(r) >= 2]
            upsert_entity(ledger, qid, labels, row.get("descriptions") or {}, row.get("aliases") or {}, claims, source="dump")
            kept += 1
            if limit and kept >= limit:
                break
    return {"seen": seen, "kept": kept}


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
