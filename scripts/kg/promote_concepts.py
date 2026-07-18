#!/usr/bin/env python3
"""promote_concepts.py — emergent 概念图【第一节:只长节点,不连边】。

用户设计(倒转架构):不预建整棵技能树,而是从**真实学习活动**里把「知识点」长成节点,
以后再复用 link_with_ai 连边、挂进 authored KG,最终汇成一张网。

铁律 / 路由:
  - 语言项(单词/语法)= vocab 系统单独管,**绝不进概念图**(用 vocab 库 + 停用词滤掉)。
  - 概念种子只取**主动知识点信号**:登记笔记(000-xxx 标题)+ 诊断卷 node_results 名。
    高亮太吵(高亮的是句子片段)默认不当种子;焦点榜/查词被 vocab 污染,绝不当源。
  - 已在 authored KG 的概念 → 标记 in_authored_kg(以后加固/挂接,不新建重复节点)。
  - 节点带 provenance(来自哪条笔记/诊断),来源=AI/行为自动 → 可确认/否决(下一节做)。

store: state/attention/emergent-graph.json = {"nodes": {key: {...}}, "meta": {...}}
CLI: (默认 dry-run 打印) | --write 落盘 | --json
"""
import sys
import json
import re
import glob
import time
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import attention_profile as AP  # noqa: E402
from config import NOTE_PATTERN  # noqa: E402

VAULT = Path(AP.VAULT_ROOT)
OUT = config.PROJECT_DIR / "state" / "attention" / "emergent-graph.json"
CHECK_DIR = config.PROJECT_DIR / "state" / "reader-check-reports"
KG_DIR = config.PROJECT_DIR / "knowledge_graph"

STOP = {"我们", "称为", "定义", "全问未回答", "全問未回答", "如果", "可以", "一个", "这个",
        "那个", "以下", "进行", "了解", "实际上", "这种", "大多数", "一切", "过程", "离开"}


def _vocab_set():
    """vocab 库 lemma 集(用来把语言项路由出概念图)。
    v3-A:返回 None = vocab 库不可达/为空 —— 调用方必须 **fail-closed**(拒绝收种,而非全放行)。
    此前空集 fail-open 是最坏方向:env 缺失时 VAULT 退 Windows 路径 → 空集 → 日语词全漏进概念图。"""
    vroot = VAULT / "资源" / "vocab"
    if not vroot.exists():
        return None
    v = set()
    for p in vroot.rglob("*.md"):
        if "_audio" in str(p):
            continue
        v.add(p.stem.lower())
    return v or None


def _authored_kg_terms():
    """authored KG 已有的 L2 概念 norm_key(判 NEW vs 已在树)。"""
    t = {}
    for f in glob.glob(str(KG_DIR / "*.json")):
        if ".bak." in f or "pre" in Path(f).stem or "scan" in Path(f).stem:
            continue
        try:
            kg = json.loads(Path(f).read_text("utf-8"))
        except Exception:
            continue
        book = kg.get("book") or Path(f).stem
        for n in kg.get("nodes", []):
            if n.get("level") == 2:
                k = AP.norm_key(n.get("name", "")) or n.get("name", "")
                if k:
                    t[k] = "%s#%s" % (book, n.get("id"))
    return t


def collect_seeds():
    """概念种子 {key: {surface, sources:set, provenance:list, signal:int}}(已路由 vocab/停用词/回收站)。"""
    vocab = _vocab_set()
    if vocab is None:
        # v3-A fail-closed:vocab 门无法评估 → 本轮不收任何种子(打印到 stderr 便于排查)
        print("⚠ vocab 库不可达/为空 → fail-closed:本轮不收种子(防语言项漏进概念图)", file=sys.stderr)
        return {}
    seeds = {}

    def add(term, src, ref):
        term = (term or "").strip()
        if len(term) < 2 or term in STOP or "回收站" in term:
            return
        k = AP.norm_key(term) or term
        # v3-A 繁简双查:norm_key 繁→简(議事→议事)而 vocab 存原形(議事.md)→ 归一键和原 surface 都要查
        if k.lower() in vocab or term.lower() in vocab:
            return
        e = seeds.setdefault(k, {"surface": term, "sources": set(), "provenance": [], "signal": 0})
        e["sources"].add(src)
        e["signal"] += 1
        if len(e["provenance"]) < 8:
            e["provenance"].append({"type": src, "ref": ref})

    # 源1:登记笔记(000-xxx,递归)——标题=一个知识点
    for p in VAULT.glob("**/*.md"):
        if NOTE_PATTERN.match(p.name) and "回收站" not in str(p):
            add(re.sub(r"^[0-9A-Fa-f]{3}-", "", p.stem), "note", p.name)
    # 源2:诊断卷 node_results 名——被测过=当概念学过
    for f in glob.glob(str(CHECK_DIR / "*.json")):
        try:
            reps = json.loads(Path(f).read_text("utf-8"))
        except Exception:
            continue
        for r in reps if isinstance(reps, list) else []:
            if not isinstance(r, dict) or r.get("sandbox"):
                continue
            for _nid, e in (r.get("node_results") or {}).items():
                add(e.get("name"), "diagnostic", r.get("name") or "")
    return seeds


def build(write=False):
    seeds = collect_seeds()
    kg_terms = _authored_kg_terms()
    nodes = {}
    for k, v in seeds.items():
        nid = "em:" + hashlib.sha1(k.encode("utf-8")).hexdigest()[:12]
        in_kg = k in kg_terms
        _bk = kg_terms.get(k, "")
        nodes[k] = {
            "id": nid, "surface": v["surface"], "key": k,
            "sources": sorted(v["sources"]), "signal": v["signal"],
            "provenance": v["provenance"],
            "in_authored_kg": in_kg, "authored_ref": _bk,
            # 来源属性(faceted graph:UI 按 subject/books 投影出单科/单书的树)
            "books": ([_bk.split("#")[0]] if in_kg and _bk else []),
            "subject": "",
            "kind": "concept", "origin": "emergent", "confirmed": None,
        }
    out = {"nodes": nodes, "meta": {"built": int(time.time()), "n": len(nodes),
           "n_new": sum(1 for x in nodes.values() if not x.get("in_authored_kg")),
           "sources": ["note", "diagnostic", "autonote"]}}
    out["edges"] = derive_edges(out)
    if write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(OUT)
    return out


# ══════════════ v3-B 存量引文扫描引擎(算法拼边 + AI 一句话确认)══════════════
# 规格:references/emergent-edge-algorithm.md §3。替换 v1 name-only AI 提名(候选/方向不该 AI 猜)。

ALIASES_FILE = config.PROJECT_DIR / "state" / "attention" / "concept-aliases.json"
EDGE_CACHE_FILE = config.PROJECT_DIR / "state" / "attention" / "edge-confirm-cache.json"
EDGE_VER = 3
_SUFFIXES = ("的定义", "的基本性质", "的性质", "求导")   # 别名后缀剥离(剥后 ≥2 字才留)
_DEF_WORDS = ("定义", "定理", "设", "称为", "是指", "依靠", "基于", "需要")   # 定义性措辞(选证据句用)


def _load_manual_aliases():
    try:
        return json.loads(ALIASES_FILE.read_text("utf-8"))
    except Exception:
        return {}


def build_alias_table(nodes):
    """key → 别名集(§1):surface + mentions 真实写法 + 后缀剥离 + 手工变体。全小写比较、保留原形匹配。"""
    manual = _load_manual_aliases()
    # mentions 真实写法:norm_key 归一到某 key 的所有 surface
    mention_surfaces = {}
    try:
        c = AP._db()
        for (sf,) in c.execute("SELECT DISTINCT surface FROM event_mentions WHERE surface != ''"):
            k = AP.norm_key(sf) or sf
            mention_surfaces.setdefault(k, set()).add(sf)
        c.close()
    except Exception:
        pass
    table = {}
    for key, n in nodes.items():
        al = {n.get("surface") or key, key}
        al |= mention_surfaces.get(key, set())
        for a in list(al):
            for suf in _SUFFIXES:
                if a.endswith(suf) and len(a) - len(suf) >= 2:
                    al.add(a[: -len(suf)])
        al |= set(manual.get(key, []))
        table[key] = {a for a in al if len(a) >= 2}
    return table


def _note_scannable_text(md):
    """§3:把笔记 md 拆成 [(text, tier)] 段。tier: quote=<!-- 原文 -->引文块(高置信) / prose=用户手写正文。
    剥除(绝不当边源):frontmatter、ankicards 代码块、<!-- 图片描述 -->、## 相关笔记 区、AI 解释/理解区。"""
    # frontmatter
    if md.startswith("---"):
        e = md.find("\n---", 3)
        if e > 0:
            md = md[e + 4:]
    # ankicards fenced block
    md = re.sub(r"```ankicards.*?```", "", md, flags=re.S)
    # AI 图片描述(转述,非原文)
    md = re.sub(r"<!--\s*图片描述.*?-->", "", md, flags=re.S)
    segs = []
    # 抽 quote 段(<!-- 原文 ... -->),抽完从正文剔除
    for m in re.finditer(r"<!--\s*原文(.*?)-->", md, flags=re.S):
        segs.append((m.group(1).strip(), "quote"))
    md = re.sub(r"<!--\s*原文.*?-->", "", md, flags=re.S)
    # 剔 AI 生成的关联/解释区(## 相关笔记 / ## AI 解释 / ## 理解(AI) 到下个同级标题或文末)
    md = re.sub(r"\n##\s*(相关笔记|概念链接[^\n]*|AI\s*解释[^\n]*|理解\s*\(AI\)[^\n]*)\s*\n.*?(?=\n##\s|\Z)", "\n", md, flags=re.S)
    # R4-P0-5:AI 生成的定义行(自动笔记兜底)绝不当边源——否则 AI定义→扫描→边→AI再确认 自强化环
    md = re.sub(r"^\*\*AI 生成[^\n]*$", "", md, flags=re.M)
    # 剔嵌入链接行/图片行(![[...]])与纯分隔线
    md = re.sub(r"^!\[\[[^\]]*\]\]\s*$", "", md, flags=re.M)
    prose = md.strip()
    if prose:
        segs.append((prose, "prose"))
    return segs


def _split_sentences(text):
    """切句:中日按句读;英文按「句点+空白+大写」(R4:F^s / 1.20 仍不误切,英文书可用)。"""
    parts = re.split(r"(?<=[。！？!?；;])|(?<=[.!?])\s+(?=[A-Z])|\n", text)
    return [p.strip() for p in parts if p and len(p.strip()) >= 4]


def _find_note_file(ref):
    """按 provenance 的文件名在 vault 定位笔记(排除回收站)。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    for p in VAULT.glob("**/" + ref):
        if "回收站" not in str(p):
            return p
    return None


def _is_latin(a):
    return bool(re.fullmatch(r"[A-Za-z0-9^'\-]+", a))


def scan_candidate_edges(g):
    """§2/§3 主扫描:A 的别名出现在 C 的可扫描正文里 ⟹ 候选有向边 A→C(带证据句/tier/来源)。
    最长优先占位;C 自己的别名预先占位当 blocker;拉丁词按词边界。返回 [{from,to,quote,quote_src,src_tier}]。"""
    nodes = g.get("nodes", {})
    aliases = build_alias_table(nodes)
    # 每个概念的可扫描句子(带 tier/来源)
    per_c = {}
    for key, n in nodes.items():
        sents = []
        seen_files = set()
        for pv in (n.get("provenance") or []):
            if pv.get("type") != "note":
                continue
            f = _find_note_file(pv.get("ref"))
            if not f or str(f) in seen_files:
                continue
            seen_files.add(str(f))
            try:
                md = f.read_text("utf-8")
            except Exception:
                continue
            for seg, tier in _note_scannable_text(md):
                for sent in _split_sentences(seg):
                    sents.append((sent, tier, "note:" + f.name))
        if sents:
            per_c[key] = sents
    # 扫描
    best = {}   # (a,c) -> cand(按 tier/定义性择优)
    for c_key, sents in per_c.items():
        own = aliases.get(c_key, set())
        # 其它概念别名,最长优先
        others = []
        for a_key, als in aliases.items():
            if a_key == c_key:
                continue
            for a in als:
                others.append((a, a_key))
        others.sort(key=lambda x: -len(x[0]))
        for sent, tier, src in sents:
            low = sent.lower()
            claimed = []
            def _claim(i, j):
                claimed.append((i, j))
            def _overlaps(i, j):
                return any(not (j <= x or i >= y) for x, y in claimed)
            # C 自己的别名先占位(blocker)
            for a in sorted(own, key=len, reverse=True):
                al = a.lower(); pos = 0
                while True:
                    i = low.find(al, pos)
                    if i < 0:
                        break
                    if not _overlaps(i, i + len(al)):
                        _claim(i, i + len(al))
                    pos = i + 1
            for a, a_key in others:
                al = a.lower(); pos = 0
                while True:
                    i = low.find(al, pos)
                    if i < 0:
                        break
                    j = i + len(al)
                    if _overlaps(i, j):
                        pos = i + 1
                        continue
                    if _is_latin(a):
                        b1 = i > 0 and low[i - 1].isalnum() and ord(low[i - 1]) < 128
                        b2 = j < len(low) and low[j].isalnum() and ord(low[j]) < 128
                        if b1 or b2:
                            pos = i + 1
                            continue
                    _claim(i, j)
                    cand = {"from": a_key, "to": c_key, "quote": sent[:300],
                            "quote_src": src, "src_tier": tier}
                    old = best.get((a_key, c_key))
                    def _rank(x):
                        return ((x["src_tier"] == "quote"),
                                any(w in x["quote"] for w in _DEF_WORDS))
                    if old is None or _rank(cand) > _rank(old):
                        best[(a_key, c_key)] = cand
                    pos = j
    return list(best.values())


def confirm_candidates(cands, model="sonnet", effort="low", use_cache=True):
    """§4 AI 一句话确认(批量):对每条候选按证据句判 prereq|demote|drop。带永久缓存。"""
    import hashlib as _h
    try:
        cache = json.loads(EDGE_CACHE_FILE.read_text("utf-8"))
    except Exception:
        cache = {}
    def _ck(c):
        return "%s|%s|%s|v%d" % (c["from"], c["to"],
                                 _h.sha1(c["quote"].encode("utf-8")).hexdigest()[:8], EDGE_VER)
    todo = [c for c in cands if not (use_cache and _ck(c) in cache)]
    if todo:
        nodes_sf = {}
        lines = []
        for i, c in enumerate(todo):
            lines.append("第%d条: A=%s, C=%s\n证据句:「%s」" % (i, c["from"], c["to"], c["quote"]))
        prompt = ("下面是从用户学习笔记原文里**机械抽出**的候选前置关系,每条给出逐字证据句。\n"
                  "逐条判断:仅就证据句本身,A 是学 C **之前必须先懂**的前置吗?\n"
                  "- prereq: 是前置(证据句表明 C 的定义/推导实际用到 A)\n"
                  "- demote: 不是前置,只是相关/例子/推广/顺带提及(如「X 是 Y 的一个例子」)\n"
                  "- drop: 证据句根本没体现 A 与 C 的关系\n\n"
                  + "\n\n".join(lines)
                  + "\n\n只输出严格 JSON 数组,无其他文字: "
                    '[{"i":0,"verdict":"prereq|demote|drop","reason":"≤15字"}, ...]')
        sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
        from ai_backends import make_backend
        backend = make_backend("claude_cli", {"command": "/usr/bin/claude",
                                              "model": model, "effort": effort, "timeout": 180})
        try:
            raw = backend.chat([{"role": "user", "content": prompt}]).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
                raw = re.sub(r"\n```\s*$", "", raw)
            a, b = raw.find("["), raw.rfind("]")
            arr = json.loads(raw[a:b + 1]) if (a != -1 and b > a) else []
            got = {int(x["i"]): x for x in arr if isinstance(x, dict) and "i" in x}
            for i, c in enumerate(todo):
                x = got.get(i) or {}
                v = x.get("verdict")
                if v not in ("prereq", "demote", "drop"):
                    # R4-P0-4:缺答不持久缓存(volatile),下轮重问;绝不当已判
                    cache[_ck(c)] = {"verdict": "unconfirmed", "ts": int(time.time()),
                                     "ver": EDGE_VER, "_volatile": True}
                    continue
                cache[_ck(c)] = {"verdict": v, "reason": (x.get("reason") or "")[:30],
                                 "ts": int(time.time()), "ver": EDGE_VER}
        except Exception as e:
            for c in todo:   # AI 整体不可用:全部 unconfirmed(不缓存,下次重试)
                cache.setdefault(_ck(c), {"verdict": "unconfirmed", "reason": "ai_error:%s" % str(e)[:20],
                                          "ts": int(time.time()), "ver": EDGE_VER, "_volatile": True})
        # 只持久化非 volatile
        persist = {k: v for k, v in cache.items() if not v.get("_volatile")}
        EDGE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        EDGE_CACHE_FILE.write_text(json.dumps(persist, ensure_ascii=False, indent=1), "utf-8")
    return {(c["from"], c["to"]): cache.get(_ck(c), {"verdict": "unconfirmed"}) for c in cands}


CONF_FILE = config.PROJECT_DIR / "state" / "attention" / "emergent-confirmations.json"


def _edge_id(a, b):
    """稳定边 id:只看方向端点,与 kind 无关(prereq→related 仍是同一条边,签字/墓碑不失效)。"""
    return hashlib.sha1(("%s|%s" % (a, b)).encode("utf-8")).hexdigest()[:12]


def upsert_claim(g, frm, to, kind, rel_detail, quote, quote_src, src_tier, method, reason=""):
    """R4 四层模型第 1/2 层:observation(不可变证据,append 去重)+ claim(最新主张,覆盖)。
    **只 upsert,绝不整表替换**——propose/存量扫描/挂起激活 都走这里,互不破坏。"""
    claims = g.setdefault("edge_claims", {})
    eid = _edge_id(frm, to)
    c = claims.get(eid) or {"from": frm, "to": to, "observations": []}
    obs = {"quote": (quote or "")[:300], "quote_src": quote_src, "src_tier": src_tier,
           "method": method, "ts": int(time.time())}
    if not any(o.get("quote") == obs["quote"] and o.get("quote_src") == obs["quote_src"]
               for o in c["observations"]):
        c["observations"] = (c["observations"] + [obs])[-8:]
    c["claim"] = {"kind": kind, "rel_detail": rel_detail, "reason": (reason or "")[:40],
                  "ver": EDGE_VER, "ts": int(time.time())}
    claims[eid] = c
    return eid


def _load_conf_edges():
    """用户 override(第 4 层):emergent-confirmations.json edges,键归一为 "from|to"(兼容旧 "from|to|kind")。"""
    try:
        raw = (json.loads(CONF_FILE.read_text("utf-8")) or {}).get("edges", {})
    except Exception:
        raw = {}
    out = {}
    for k, v in raw.items():
        parts = k.split("|")
        if len(parts) >= 2:
            out["%s|%s" % (parts[0], parts[1])] = v
    return out


_TIER_RANK = {"quote": 3, "book": 2, "prose": 1}


def derive_edges(g):
    """R4 派生视图:override > 墓碑(audit remove) > audited > auto。
    激活策略:related 生成即生效(auto);prereq 未审计= shadow(展示但不进 availability);
    unconfirmed 不产 claim(上游已 fail-closed)。防环:只在 effective(audited/user_confirmed)
    prereq 间检,环内证据最弱者**降 shadow**(非破坏)。"""
    conf = _load_conf_edges()
    audits = g.get("edge_audits", {})
    out = []
    for eid, c in (g.get("edge_claims") or {}).items():
        ov = conf.get("%s|%s" % (c["from"], c["to"]))
        if ov is False:
            continue                                   # 用户否决=最高优先墓碑
        ad = audits.get(eid)
        kind = (c.get("claim") or {}).get("kind", "related")
        status = "auto"
        if ad:
            if ad.get("verdict") == "remove" and ov is not True:
                continue                               # 审计墓碑(用户 True 可复活)
            if ad.get("verdict") == "demote":
                kind = "related"
            status = "audited"
        if ov is True:
            status = "user_confirmed"
        elif status == "auto" and kind == "prereq":
            status = "shadow"                          # prereq 审计过才 effective
        obs = sorted(c.get("observations") or [], key=lambda o: -_TIER_RANK.get(o.get("src_tier"), 0))
        best = obs[0] if obs else {}
        out.append({"id": eid, "from": c["from"], "to": c["to"], "kind": kind,
                    "origin": "emergent", "status": status,
                    "method": best.get("method", ""), "quote": best.get("quote", ""),
                    "quote_src": best.get("quote_src", ""), "src_tier": best.get("src_tier", ""),
                    "rel_detail": (c.get("claim") or {}).get("rel_detail", ""),
                    "reason": (c.get("claim") or {}).get("reason", ""), "ver": EDGE_VER})
    # 防环(effective prereq 间;弱者降 shadow 不删)
    from collections import defaultdict as _dd
    while True:
        eff = [e for e in out if e["kind"] == "prereq" and e["status"] in ("audited", "user_confirmed")]
        indeg = _dd(int); adj = _dd(list)
        for e in eff:
            adj[e["from"]].append(e["to"]); indeg[e["to"]] += 1
        ns = set(x for e in eff for x in (e["from"], e["to"]))
        q = [x for x in ns if indeg[x] == 0]; okset = set()
        while q:
            x = q.pop(); okset.add(x)
            for v in adj[x]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        cyc = ns - okset
        if not cyc:
            break
        in_cyc = [e for e in eff if e["from"] in cyc and e["to"] in cyc and e["status"] != "user_confirmed"]
        if not in_cyc:
            break                                      # 全是用户签字边,环留给用户裁
        weakest = min(in_cyc, key=lambda e: _TIER_RANK.get(e["src_tier"], 0))
        weakest["status"] = "shadow"
    return out


def build_edges(model="sonnet", effort="low", write=False, candidates_only=False):
    """v3-B:扫描拼边(零 AI)→ AI 一句话确认 → 落边(status:auto 生成即生效)→ 防环。
    不再改 nodes/subject/groups(保留既有)。"""
    g = json.loads(OUT.read_text("utf-8"))
    cands = scan_candidate_edges(g)
    if candidates_only:
        g["_candidates"] = cands
        return g
    verdicts = confirm_candidates(cands, model=model, effort=effort)
    for c in cands:
        v = verdicts.get((c["from"], c["to"]), {})
        vd = v.get("verdict", "unconfirmed")
        if vd in ("drop", "unconfirmed"):   # fail-closed:没点头的绝不进图,下轮重试
            continue
        upsert_claim(g, c["from"], c["to"],
                     kind=("prereq" if vd == "prereq" else "related"), rel_detail=vd,
                     quote=c["quote"], quote_src=c["quote_src"], src_tier=c["src_tier"],
                     method="aliasscan+sentconfirm", reason=v.get("reason", ""))
    g["edges"] = derive_edges(g)          # R4:派生视图(override>墓碑>audited>auto;prereq未审=shadow)
    g["meta"]["n_edges"] = len(g["edges"])
    g["meta"]["edges_built"] = int(time.time())
    g["meta"]["edges_ver"] = EDGE_VER
    if write:
        OUT.write_text(json.dumps(g, ensure_ascii=False, indent=1), "utf-8")
    return g


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--edges", action="store_true", help="v3-B:扫描拼边+AI一句话确认")
    ap.add_argument("--candidates-only", action="store_true", help="只跑扫描不调 AI(离线)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    if a.edges or a.candidates_only:
        g = build_edges(model=a.model, effort=a.effort, write=(a.write and not a.candidates_only),
                        candidates_only=a.candidates_only)
        k2s = {k: n["surface"] for k, n in g["nodes"].items()}
        if a.candidates_only:
            cands = g.get("_candidates", [])
            print("候选边(纯算法,未确认):%d" % len(cands))
            for c in cands:
                print("  %s → %s [%s|%s]\n    「%s」" % (k2s.get(c["from"], c["from"]), k2s.get(c["to"], c["to"]),
                      c["src_tier"], c["quote_src"], c["quote"][:80]))
        else:
            print("边:%d %s" % (g["meta"].get("n_edges", 0), "[已落盘]" if a.write else "[dry-run]"))
            for e in g.get("edges", []):
                arrow = "→" if e["kind"] == "prereq" else "—"
                print("  %s %s %s [%s/%s] %s\n    「%s」" % (k2s.get(e["from"], e["from"]), arrow,
                      k2s.get(e["to"], e["to"]), e["rel_detail"], e["src_tier"], e.get("reason", ""), e["quote"][:80]))
        sys.exit(0)
    r = build(write=a.write)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        m = r["meta"]
        print("emergent 概念节点:%d(新 %d / 已在 authored KG %d)%s" %
              (m["n"], m["n_new"], m["n"] - m["n_new"], "  [已落盘]" if a.write else "  [dry-run]"))
