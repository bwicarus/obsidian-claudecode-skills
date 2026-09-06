"""实测一次渐进式查询要花多少 token（临时账本，不碰真实数据）。

    python scripts/kj/measure_tokens.py [概念名] [--no-sparql]

路径 A = 默认路径：名称直搜（本地空 → 上网）→ 绑编号 → 读节点详情。
路径 B = 最差情况：沿 Wikidata 分类链从顶层大类逐层下钻，每层最多回 20 个候选（用 SPARQL 数真实分支数）。
分词用 tiktoken o200k_base（Codex/GPT 系）；没装就用启发式估算并注明。
Wikidata 的 SPARQL 端点要求带联系方式的 User-Agent，否则 429：环境变量 KJ_WIKIDATA_CONTACT 可覆盖。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kj.service import KJService  # type: ignore
    from kj import wikidata as WD  # type: ignore
else:
    from .service import KJService
    from . import wikidata as WD

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")

    def tok(s: str) -> int:
        return len(_ENC.encode(s))
    TOKNOTE = "o200k_base（GPT/Codex 分词）"
except Exception:  # pragma: no cover
    def tok(s: str) -> int:
        cjk = sum(1 for ch in s if "぀" <= ch <= "鿿")
        return int(cjk + (len(s) - cjk) / 3.5)
    TOKNOTE = "启发式估算（CJK 1 字≈1 token，其它 3.5 字符≈1 token）"

CONTACT = os.environ.get("KJ_WIKIDATA_CONTACT", "https://github.com/bwicarus")
UA = {"User-Agent": f"bwreader-kj-measure/1 ({CONTACT})", "Accept": "application/json"}
HTTP: list[tuple[str, int, float]] = []


def http(url: str, timeout: float = 30, accept: str = "application/json") -> dict:
    t = time.time()
    req = urllib.request.Request(url, headers=dict(UA, Accept=accept))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    HTTP.append((url[:80], len(data), round(time.time() - t, 2)))
    return json.loads(data.decode("utf-8"))


def sparql(q: str) -> dict:
    time.sleep(1.5)  # 429 保护
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q, "format": "json"})
    return http(url, timeout=60, accept="application/sparql-results+json")


def J(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    args = [a for a in (argv if argv is not None else sys.argv[1:])]
    do_sparql = "--no-sparql" not in args
    online = "--offline" not in args
    public_db = None
    for a in args:
        if a.startswith("--public-db="):
            public_db = a.split("=", 1)[1]
    concept = next((a for a in args if not a.startswith("--")), "向量空间")
    tmp = Path(tempfile.mkdtemp(prefix="kjtok"))
    # --public-db=<kj-public.db>：把真实的公共目录挂到临时账本上（只读用），量的就是离线本地检索
    svc = KJService(tmp / "kj.db", render=False, actor="measure")
    if public_db:
        svc.close()
        from kj.store import Ledger as _Ledger  # type: ignore
        svc = KJService.__new__(KJService)
        svc.ledger = _Ledger(tmp / "kj.db", public_path=public_db)
        svc.writer = None
        svc.actor = "measure"
    rows: list[tuple[str, int, int, float | None]] = []
    try:
        t = time.time()
        res = svc.search(concept, online=online, limit=8)
        s = J(res)
        rows.append((f"A1 search {concept} {'--online（本地空库→上网）' if online else '--offline（本地公共目录）'}", len(s), tok(s), round(time.time() - t, 2)))
        pub = res.get("public") or []
        if not pub:
            print("公共目录与在线搜索都没有候选，无法继续路径 A/B")
            return 1
        exact = [p for p in pub if p.get("label") == concept]
        qid = (exact or pub)[0]["qid"]
        chain: list[tuple[str, str]] = []
        cur = qid
        for _ in range(6):
            e = WD.entity(svc.ledger, cur) if not online else None
            if e is None:
                e = WD.fetch_entity(svc.ledger, cur, fetcher=lambda url, to: http(url, to))
            if e is None:
                break
            chain.append((cur, e["label"]))
            nxt = None
            for prop in ("P279", "P31"):
                c = [tg for p, tg, _ in WD.claims_of(svc.ledger, cur) if p == prop]
                if c:
                    nxt = c[0]
                    break
            if not nxt:
                break
            cur = nxt
        nid = svc.create_node(name=concept, qid=qid, fetch_public=False)["node_id"]
        detail = svc.node(nid)
        s = J(detail)
        rows.append(("A2 node 详情（含公共邻居/分类路径）", len(s), tok(s), None))
        s2 = J({k: detail[k] for k in ("id", "name", "path", "mastery", "readiness", "next_hint")})
        rows.append(("A2' 只取交流必需字段", len(s2), tok(s2), None))
        s = J(svc.search(concept))
        rows.append(("A3 再搜一次（本地已有节点，不上网）", len(s), tok(s), None))

        print("分词器:", TOKNOTE)
        print("\n== 路径 A：直搜 → 读节点（默认路径）==")
        print(f"{'步骤':52} {'字符':>7} {'token':>6} {'耗时s':>6}")
        for name, ch, tk, dt in rows:
            print(f"{name:52} {ch:>7} {tk:>6} {'' if dt is None else dt:>6}")
        print("向上分类链:", " → ".join(f"{l}({q})" for q, l in chain))
        print("在线 HTTP 次数:", len(HTTP), "字节合计:", sum(b for _, b, _ in HTTP), "总耗时 s:", round(sum(x for _, _, x in HTTP), 1))

        if not do_sparql:
            return 0
        levels = []
        for q, label in reversed(chain):
            try:
                n = int(sparql(f"SELECT (COUNT(?x) AS ?n) WHERE {{ ?x wdt:P279 wd:{q} }}")["results"]["bindings"][0]["n"]["value"])
            except Exception:
                n = -1
            page: list[dict] = []
            try:
                b = sparql(f'SELECT ?x ?xLabel ?xDescription WHERE {{ ?x wdt:P279 wd:{q} . '
                           f'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "zh,en". }} }} LIMIT 20')["results"]["bindings"]
                page = [{"qid": x["x"]["value"].rsplit("/", 1)[1], "label": x.get("xLabel", {}).get("value", ""),
                         "description": x.get("xDescription", {}).get("value", "")[:60]} for x in b]
            except Exception:
                pass
            s = J({"parent": {"qid": q, "label": label}, "total_children": n, "children": page})
            levels.append((q, label, n, len(page), len(s), tok(s)))
        print("\n== 路径 B：从顶层大类逐层下钻（每层最多回 20 个候选，含 zh/en 标签+简述）==")
        print(f"{'层':4} {'类':30} {'直接子类数':>8} {'返回数':>5} {'字符':>6} {'token':>6}")
        for i, (q, label, n, k, ch, tk) in enumerate(levels, 1):
            print(f"{i:<4} {label[:28]:30} {n:>8} {k:>5} {ch:>6} {tk:>6}")
        total = sum(l[5] for l in levels)
        cumulative = sum(sum(l[5] for l in levels[:i]) for i in range(1, len(levels) + 1))
        print("逐层输出 token 合计:", total, "| 若每轮都把前面输出留在上下文里，累计输入约:", cumulative)
        return 0
    finally:
        svc.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
