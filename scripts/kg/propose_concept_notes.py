#!/usr/bin/env python3
"""propose_concept_notes.py — v3-C 主流程「阅读时生长」(规格 references/emergent-edge-algorithm.md §1-§2b)。

注意力新词 → 门控(vocab/碎片/科目/阈值) → 向前搜索(1..当前页) → 摘句段 → 相关性 top-k
→ AI①按原句分类关系 → §2b 身份解析(防多语言重复笔记) → 机械查卡分支(有卡建边/无卡挂起/主体新建)
→ AI②从最前几次出现提取定义(找不到才生成,打标) → 概念笔记落 资源/概念/<书>/<编码>-<概念名>.md。

分工铁律:算法做全部预选/组装;AI 只有两处有界判断(①关系分类限定词表+quote校验 ②定义提取);
边生成即生效(status:auto),后台审计(v3-D)把关;dry-run 默认,--run 才落盘。
CLI:
  --dry-run(默认) | --run 落盘
  --force-term X --book REL [--page N]   测试:指定词跑全流程(绕过阈值/科目门,但打印哪些门本会拦)
"""
import json
import re
import sys
import time
import sqlite3
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import attention_profile as AP  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import promote_concepts as PC  # noqa: E402

STATE = config.PROJECT_DIR / "state"
SEARCH_DB = STATE / "pdf-search.db"
POSITIONS = STATE / "reader-positions.json"
FOCUS = STATE / "attention" / "focus.json"
CODES = STATE / "attention" / "note-codes.json"
PENDING_EDGES = STATE / "attention" / "pending-edges.json"
EMERGENT = STATE / "attention" / "emergent-graph.json"
CONCEPT_DIR_NAME = "资源/概念"

TOP_N = 20            # 注意力榜前 N 才考虑
DEEP_CHANNELS = {"highlight", "qa", "check", "read"}
TOPK_RELATED = 4      # 相关词上限
STOP = PC.STOP | {"内容", "问题", "方法", "情况", "部分", "时候", "东西"}


# ── 注册表(编码/科目/书开火) ──────────────────────────────────────────────────
def _load_codes():
    try:
        return json.loads(CODES.read_text("utf-8"))
    except Exception:
        return {"books": {}, "codes": {},
                "legacy": {"000": "数学/线性代数/LADR", "010": "数学/微积分/手绘"}}


def _save_codes(d):
    CODES.parent.mkdir(parents=True, exist_ok=True)
    tmp = CODES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(CODES)


def _book_entry(reg, book_rel, create=False):
    b = reg["books"].get(book_rel)
    if b or not create:
        return b
    # 分配下一个空闲三位码(跳过 legacy/已用);科目名先用书名,用户可改
    used = set(reg["codes"]) | set(reg.get("legacy", {}))
    code = None
    for first in "23456789ABCDEF":          # 0=数学(legacy 占) 1 留人工
        for third in "0123456789ABCDEF":
            cand = first + "0" + third
            if cand not in used:
                code = cand
                break
        if code:
            break
    stem = Path(book_rel).stem
    b = {"code": code, "subject": stem, "enabled": False}   # 新书默认不开火(科目门)
    reg["books"][book_rel] = b
    reg["codes"][code] = {"subject": stem, "book_rel": book_rel}
    return b


# ── 门控 ─────────────────────────────────────────────────────────────────────
def _gates(term, book_rel, vocab, reg, focus_row=None, force=False):
    """返回 (通过?, [被拦原因们])。force=True 时仍评估并打印,但不拦。"""
    blocks = []
    k = AP.norm_key(term) or term
    if vocab is None:
        blocks.append("vocab库不可达(fail-closed)")
    else:
        if k.lower() in vocab or term.lower() in vocab:
            blocks.append("vocab路由(语言项)")
        if any(term != w and term in w for w in vocab if len(w) > len(term)):
            blocks.append("碎片门(是更长vocab词的子串)")
    b = _book_entry(reg, book_rel) if book_rel else None
    if not (b and b.get("enabled")):
        blocks.append("科目门(本书未开火)")
    if focus_row is not None:
        if not (focus_row.get("n7", 0) >= 3 or focus_row.get("burst", 0) >= 3):
            blocks.append("热度阈值(n7<3且burst<3)")
        chs = _term_channels(k)
        if len(chs) < 2 or not (chs & DEEP_CHANNELS):
            blocks.append("渠道阈值(需≥2渠道含≥1深渠道,现=%s)" % sorted(chs))
    return (force or not blocks), blocks


def _term_channels(key):
    try:
        c = AP._db()
        rows = c.execute("SELECT DISTINCT m.surface, e.channel FROM event_mentions m "
                         "JOIN events e ON e.src_key=m.src_key WHERE m.surface != ''").fetchall()
        c.close()
        return {ch for sf, ch in rows if (AP.norm_key(sf) or sf) == key}
    except Exception:
        return set()


# ── §2b 身份解析(防多语言重复) ────────────────────────────────────────────────
def _identity_resolve(term, use_ai=True):
    """级联:norm_key → 既有节点/authored KG → aliases 反查 → (外语新词)AI 判。
    返回 (existing_key or None, how)。"""
    k = AP.norm_key(term) or term
    try:
        g = json.loads(EMERGENT.read_text("utf-8"))
        nodes = g.get("nodes", {})
    except Exception:
        nodes = {}
    if k in nodes:
        return k, "norm_key"
    kgt = PC._authored_kg_terms()
    if k in kgt:
        return k, "authored_kg"
    aliases = PC._load_manual_aliases()
    for nk, als in aliases.items():
        if term in als or k in {(AP.norm_key(a) or a) for a in als}:
            return nk, "alias"
    # concept-auto 笔记:文件名(200-食中毒.md→食中毒)+ frontmatter aliases 都认(R4)
    for p in (Path(AP.VAULT_ROOT) / CONCEPT_DIR_NAME).glob("**/*.md"):
        stem = re.sub(r"^[0-9A-Fa-f]{3}-", "", p.stem)
        sk = AP.norm_key(stem) or stem
        if sk == k or stem == term:
            return sk, "note_filename"
        try:
            head = p.read_text("utf-8", errors="ignore")[:400]
        except OSError:
            continue
        m = re.search(r"aliases:\s*\[([^\]]*)\]", head)
        if m and term in [x.strip().strip('"\'') for x in m.group(1).split(",")]:
            return sk, "note_alias"
    # 外语词(纯拉丁)且库里有非拉丁节点 → 一次有界 AI 判(是否同一概念)
    if use_ai and re.fullmatch(r"[A-Za-z][A-Za-z0-9 \-]+", term) and nodes:
        cands = [n.get("surface") or kk for kk, n in list(nodes.items())[:40]]
        try:
            sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
            from ai_backends import make_backend
            be = make_backend("claude_cli", {"command": "/usr/bin/claude",
                                             "model": "sonnet", "effort": "low", "timeout": 120})
            raw = be.chat([{"role": "user", "content":
                "术语「%s」与下面清单里的某个概念是**同一概念的另一语言写法**吗?清单:%s\n"
                '只输出严格 JSON:{"match":"<清单中的原名或null>"}' % (term, "、".join(cands))}]).strip()
            a, bb = raw.find("{"), raw.rfind("}")
            mt = (json.loads(raw[a:bb + 1]) or {}).get("match")
            if mt and mt in cands:
                return (AP.norm_key(mt) or mt), "ai_identity"
        except Exception:
            return None, "identity_uncertain"   # 判不成 → 调用方宁可缓建
    return None, "new"


# ── 向前搜索 + 相关性 ─────────────────────────────────────────────────────────
def _forward_occurrences(book_rel, term, upto_page):
    """该书 1..upto_page 中 term 出现处 → [(page, sentence, paragraph)](逐字)。"""
    if not SEARCH_DB.exists():
        return []
    con = sqlite3.connect("file:%s?mode=ro" % SEARCH_DB, uri=True)
    rows = con.execute("SELECT page, body FROM pages_data WHERE file=? AND page<=? ORDER BY page",
                       (book_rel, upto_page)).fetchall()
    con.close()
    tl = term.lower()      # R4:拉丁词大小写不敏感(英文书);CJK lower 无副作用
    out = []
    for pg, body in rows:
        if not body or tl not in body.lower():
            continue
        for para in re.split(r"\n\s*\n", body):
            if tl not in para.lower():
                continue
            for sent in PC._split_sentences(para):
                if tl in sent.lower():
                    out.append((pg, sent, para.strip()[:800]))
    return out


def _related_terms(term, occurrences, book_rel, vocab):
    """句段内其它词的相关性排序(纯算法):同句×2 + 频次;滤 STOP/自身/纯数字。top-k。"""
    lang = None
    try:
        lang = AP._book_lang(book_rel)
    except Exception:
        pass
    score = {}
    for pg, sent, para in occurrences:
        for scope, w in ((sent, 2.0), (para, 1.0)):
            try:
                terms = AP.extract_terms(scope, lang=lang)
            except Exception:
                terms = []
            for t in set(terms):
                if t == term or t in STOP or len(t) < 2 or t.isdigit():
                    continue
                if term in t or t in term:
                    continue
                e = score.setdefault(t, [0.0, None, None])
                e[0] += w
                if e[1] is None and t in sent:
                    e[1], e[2] = sent, pg          # 首个同句证据
    ranked = sorted(score.items(), key=lambda x: -x[1][0])
    return [{"term": t, "score": round(v[0], 1), "evidence": v[1], "page": v[2]}
            for t, v in ranked[:TOPK_RELATED] if v[1]]


# ── AI ①关系分类 / ②定义提取 ─────────────────────────────────────────────────
def _backend(model="sonnet", effort="low"):
    sys.path.insert(0, str(config.PROJECT_DIR / "_client" / "core"))
    from ai_backends import make_backend
    return make_backend("claude_cli", {"command": "/usr/bin/claude",
                                       "model": model, "effort": effort, "timeout": 180})


def ai_classify(term, related):
    """AI①:对 (term, top-k 相关词) 按原句分类。只能在给定词里选;quote 必须是所给原句子串。"""
    if not related:
        return []
    lines = ["%d. 词=%s\n   原句:「%s」" % (i, r["term"], r["evidence"]) for i, r in enumerate(related)]
    prompt = ("主体概念:「%s」。下面是从书中**机械摘出**的、与它同句出现的候选相关词及原句。\n"
              "逐条判断该词与主体概念的关系(仅依据原句):\n"
              "- prereq: 学主体概念之前必须先懂它\n- related: 相关但非前置\n- none: 原句看不出关系\n\n"
              % term + "\n".join(lines)
              + '\n\n只输出严格 JSON 数组:[{"i":0,"relation":"prereq|related|none"},...]')
    try:
        raw = _backend().chat([{"role": "user", "content": prompt}]).strip()
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```\s*$", "", raw)
        a, b = raw.find("["), raw.rfind("]")
        arr = json.loads(raw[a:b + 1]) if a != -1 and b > a else []
        got = {int(x["i"]): x.get("relation") for x in arr if isinstance(x, dict)}
        out = []
        for i, r in enumerate(related):
            rel = got.get(i)
            if rel in ("prereq", "related"):
                out.append(dict(r, relation=rel))
        return out
    except Exception as e:
        print("  ⚠ AI①失败:%s → 本轮不建边(留待下轮)" % str(e)[:60], file=sys.stderr)
        return []


def ai_definition(term, book_rel, occurrences):
    """AI②:取最前 2-3 次出现的前后文,尽量逐字找定义;找不到才生成(打标)。子串校验。"""
    ctxs = []
    seen_pg = set()
    for pg, sent, para in occurrences:
        if pg in seen_pg:
            continue
        seen_pg.add(pg)
        ctxs.append((pg, para))
        if len(ctxs) >= 3:
            break
    if not ctxs:
        return None
    blocks = "\n\n".join("── p%d ──\n%s" % (pg, para) for pg, para in ctxs)
    prompt = ("下面是「%s」在书中最早几次出现的原文段落。请**尽量从原文里逐字**找出能当它定义/核心解释的 1-2 句;"
              "确实没有,才自己写一段简洁定义。\n\n%s\n\n只输出严格 JSON:"
              '{"found":true|false,"quote":"<逐字原句,found=false 时为空>","page":<页码>,"definition":"<found=false 时你写的定义>"}'
              % (term, blocks))
    try:
        raw = _backend().chat([{"role": "user", "content": prompt}]).strip()
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```\s*$", "", raw)
        a, b = raw.find("{"), raw.rfind("}")
        d = json.loads(raw[a:b + 1]) if a != -1 and b > a else {}
        if d.get("found") and d.get("quote"):
            norm = lambda x: re.sub(r"\s+", "", x or "")
            if norm(d["quote"]) not in norm(blocks):
                d = {"found": False, "definition": d.get("definition") or d.get("quote", ""), "page": None}
        return d
    except Exception as e:
        print("  ⚠ AI②失败:%s → 本轮不建笔记" % str(e)[:60], file=sys.stderr)
        return None


# ── 笔记生成 ─────────────────────────────────────────────────────────────────
def _note_md(term, code, book_rel, definition, relations, reg):
    subj = reg["books"].get(book_rel, {}).get("subject", "")
    book_name = Path(book_rel).name
    lines = ["---", "type: concept-auto", "book: %s" % book_rel, "code: \"%s\"" % code,
             "subject: %s" % subj, "status: auto", "aliases: []",
             "created: %s" % time.strftime("%Y-%m-%d"), "---", "", "# %s" % term, "", "## 定义"]
    if definition and definition.get("found"):
        lines.append("> %s" % definition["quote"].strip())
        if definition.get("page"):
            lines.append("")
            lines.append("![[%s#page=%d]]" % (book_name, int(definition["page"])))
    elif definition:
        lines.append("**AI 生成(仅参考,非原文)**:%s" % (definition.get("definition") or "").strip())
    lines += ["", "## 概念链接(自动)"]
    for r in relations:
        tag = "前置" if r["relation"] == "prereq" else "相关"
        target = r.get("link") or r["term"]
        lines.append("- %s:[[%s|%s]] — 证据 p%s:「%s」"
                     % (tag, target, r["term"], r.get("page", "?"), (r.get("evidence") or "")[:60]))
    if not relations:
        lines.append("(暂无)")
    lines += ["", "## AI 解释(自动)", "(待生成)", ""]
    return "\n".join(lines)


# ── 编排 ─────────────────────────────────────────────────────────────────────
IDENTITY_PENDING = STATE / "attention" / "identity-pending.json"


def _queue_pending_identity(term, book, dry=True):
    """身份判不准 → 待定队列(下轮重判;夜间归并可消费)。"""
    if dry:
        return
    try:
        q = json.loads(IDENTITY_PENDING.read_text("utf-8")) if IDENTITY_PENDING.exists() else []
    except Exception:
        q = []
    if not any(x.get("term") == term for x in q):
        q.append({"term": term, "book": book, "ts": int(time.time())})
        IDENTITY_PENDING.write_text(json.dumps(q, ensure_ascii=False, indent=1), "utf-8")


def _enrich_existing(ident, term, book, page, dry=True):
    """R4 命中即合并不新建:①新写法进 aliases;②新书首现句追加「## 来源」分节(逐字,零 AI)。
    **只动机器自有笔记**(concept-auto);手写 000- 笔记只记日志不碰。返回做了什么。"""
    did = []
    target = None
    for p in (Path(AP.VAULT_ROOT) / CONCEPT_DIR_NAME).glob("**/*.md"):
        stem = re.sub(r"^[0-9A-Fa-f]{3}-", "", p.stem)
        if (AP.norm_key(stem) or stem) == ident:
            target = p
            break
    if not target:
        return did          # 身份在 authored KG/手写笔记 → 不碰(所有权)
    try:
        md = target.read_text("utf-8")
    except OSError:
        return did
    if "type: concept-auto" not in md[:200]:
        return did
    stem = re.sub(r"^[0-9A-Fa-f]{3}-", "", target.stem)
    # ① aliases 写回
    m = re.search(r"^aliases:\s*\[([^\]]*)\]\s*$", md, flags=re.M)
    if m and term != stem:
        cur = [x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()]
        if term not in cur:
            newline = "aliases: [%s]" % ", ".join(cur + [term])
            md = md[:m.start()] + newline + md[m.end():]
            did.append("alias+%s" % term)
    # ② 新来源分节(该书还没记过 → 首现句逐字追加)
    bname = Path(book).name
    if ("book: %s" % book) not in md and ("## 来源(%s" % bname) not in md:
        occ = _forward_occurrences(book, term, page or 9999)
        if occ:
            pg, sent, _para = occ[0]
            md = md.rstrip() + "\n\n## 来源(%s p%d)\n> %s\n" % (bname, pg, sent.strip()[:300])
            did.append("来源分节 p%d" % pg)
    if did and not dry:
        target.write_text(md, "utf-8")
    return did


def run(dry=True, force_term=None, force_book=None, force_page=None):
    vocab = PC._vocab_set()
    reg = _load_codes()
    try:
        focus = json.loads(FOCUS.read_text("utf-8")).get("top", [])[:TOP_N]
    except Exception:
        focus = []
    try:
        pos = json.loads(POSITIONS.read_text("utf-8"))
    except Exception:
        pos = {}

    if force_term:
        cands = [{"term": force_term, "book": force_book,
                  "page": force_page or (pos.get(force_book, {}) or {}).get("pos") or 9999,
                  "row": None, "force": True}]
    else:
        cands = []
        for row in focus:
            refs = row.get("refs") or []
            book = refs[0]["file"] if refs else ""
            if not book or not book.lower().endswith(".pdf"):
                continue
            page = (pos.get(book, {}) or {}).get("pos") or max(r.get("page", 0) for r in refs)
            cands.append({"term": row["term"], "book": book, "page": page, "row": row, "force": False})

    made = []
    for c in cands:
        term, book, page = c["term"], c["book"], c["page"]
        ok, blocks = _gates(term, book, vocab, reg, focus_row=c["row"], force=c["force"])
        tag = "「%s」@%s p≤%s" % (term, Path(book).name if book else "?", page)
        if blocks:
            print("%s %s 门:%s" % ("⚠(force放行)" if c["force"] else "⏭", tag, ";".join(blocks)))
        if not ok:
            continue
        # §2b 身份解析:已有身份 → 不新建,改为**富化**(aliases 写回 + 新来源分节;只动机器自有笔记)
        ident, how = _identity_resolve(term)
        if ident:
            did = _enrich_existing(ident, term, book, page, dry=dry)
            print("⏭ %s 已有概念身份(%s→%s)%s" % (tag, how, ident,
                  ",富化:" + "+".join(did) if did else ",无需富化"))
            continue
        if how == "identity_uncertain":
            _queue_pending_identity(term, book, dry=dry)
            print("⏸ %s 身份判不准 → 进待定队列(下轮/归并重判)" % tag)
            continue
        occ = _forward_occurrences(book, term, page)
        if not occ:
            print("⏭ %s 向前搜索 0 命中(FTS 无文字层?)→ 不建" % tag)
            continue
        related = _related_terms(term, occ, book, vocab)
        print("▶ %s:出现 %d 处,相关词 %s" % (tag, len(occ), [r["term"] for r in related]))
        rels = ai_classify(term, related)
        # 机械查卡分支:相关词解析身份 → 有卡=真边;无卡=挂起(wikilink 预判码)
        b_entry = _book_entry(reg, book, create=True)
        code = b_entry["code"]
        edges, pend = [], []
        for r in rels:
            rk, rhow = _identity_resolve(r["term"], use_ai=False)
            if rk:
                r["link"] = None   # 已有节点:笔记里链概念名,真边进图
                edges.append({"from": rk, "to": AP.norm_key(term) or term, "kind": r["relation"] if r["relation"] == "prereq" else "related",
                              "origin": "emergent", "status": "auto", "method": "forwardsearch+aiclassify",
                              "quote": (r.get("evidence") or "")[:300], "quote_src": "book:%s#p%s" % (book, r.get("page")),
                              "src_tier": "book", "rel_detail": r["relation"], "ver": PC.EDGE_VER})
            else:
                r["link"] = "%s-%s" % (code, r["term"])   # 挂起:同书预判码 wikilink(未解析灰显)
                pend.append({"from_term": r["term"], "to_key": AP.norm_key(term) or term,
                             "relation": r["relation"], "quote": (r.get("evidence") or "")[:300],
                             "book": book, "page": r.get("page"), "ts": int(time.time())})
        definition = ai_definition(term, book, occ)
        if definition is None:
            continue
        # 挂起边激活:此前有别的笔记挂起了指向本词的边(from_term==本词)→ 本词现在成卡,转真边
        try:
            pend_all = json.loads(PENDING_EDGES.read_text("utf-8")) if PENDING_EDGES.exists() else []
        except Exception:
            pend_all = []
        me = AP.norm_key(term) or term
        activated = [pe for pe in pend_all if (AP.norm_key(pe.get("from_term", "")) or pe.get("from_term")) == me]
        for pe in activated:
            edges.append({"from": me, "to": pe["to_key"],
                          "kind": pe.get("relation") if pe.get("relation") == "prereq" else "related",
                          "origin": "emergent", "status": "auto", "method": "pending_activated",
                          "quote": pe.get("quote", ""), "quote_src": "book:%s#p%s" % (pe.get("book"), pe.get("page")),
                          "src_tier": "book", "rel_detail": pe.get("relation", ""), "ver": PC.EDGE_VER})
        if activated:
            print("  ⚡ 激活挂起边 %d 条(此前指向「%s」的)" % (len(activated), term))
            remaining = [pe for pe in pend_all if pe not in activated]
            if not dry:
                PENDING_EDGES.write_text(json.dumps(remaining, ensure_ascii=False, indent=1), "utf-8")
        md = _note_md(term, code, book, definition, rels, reg)
        rel_path = "%s/%s/%s-%s.md" % (CONCEPT_DIR_NAME, Path(book).stem, code, term)
        made.append({"path": rel_path, "md": md, "edges": edges, "pending": pend})
        print("  ✎ 笔记:%s(定义:%s;真边 %d,挂起 %d)"
              % (rel_path, "原文引用 p%s" % definition.get("page") if definition.get("found") else "AI生成打标",
                 len(edges), len(pend)))
        if dry:
            print("  ── dry-run 预览 ──")
            print("  " + md.replace("\n", "\n  ")[:1200])
    if not dry and made:
        for m in made:
            fp = Path(AP.VAULT_ROOT) / m["path"]
            fp.parent.mkdir(parents=True, exist_ok=True)
            if fp.exists():
                print("⏭ 已存在不覆盖:%s" % m["path"])
                continue
            fp.write_text(m["md"], "utf-8")
        # R4:原子 upsert——node + edge claims + derive,单次写文件;绝不 append 裸边/整表替换
        try:
            g = json.loads(EMERGENT.read_text("utf-8"))
            g.setdefault("nodes", {})
            for m in made:
                me_term = m["path"].rsplit("/", 1)[-1].split("-", 1)[-1][:-3]   # 编码-名.md → 名
                me = AP.norm_key(me_term) or me_term
                import hashlib as _h
                node = g["nodes"].get(me) or {"id": "em:" + _h.sha1(me.encode()).hexdigest()[:12],
                                              "surface": me_term, "key": me, "sources": [], "signal": 0,
                                              "provenance": [], "in_authored_kg": False, "authored_ref": "",
                                              "books": [], "subject": "", "kind": "concept",
                                              "origin": "emergent", "confirmed": None}
                if "autonote" not in node["sources"]:
                    node["sources"] = sorted(set(node["sources"]) | {"autonote"})
                node["signal"] += 1
                ref = m["path"].rsplit("/", 1)[-1]
                if not any(pv.get("ref") == ref for pv in node["provenance"]):
                    node["provenance"] = (node["provenance"] + [{"type": "autonote", "ref": ref}])[:8]
                for e in m["edges"]:
                    if e["to"] == me or e["from"] == me:
                        bk = e.get("quote_src", "")
                        node["books"] = sorted(set(node["books"]) | ({bk.split("#")[0][5:]} if bk.startswith("book:") else set()))
                g["nodes"][me] = node
                for e in m["edges"]:
                    PC.upsert_claim(g, e["from"], e["to"], kind=e["kind"], rel_detail=e.get("rel_detail", ""),
                                    quote=e.get("quote", ""), quote_src=e.get("quote_src", ""),
                                    src_tier=e.get("src_tier", "book"), method=e.get("method", "forwardsearch"))
            g["edges"] = PC.derive_edges(g)
            EMERGENT.write_text(json.dumps(g, ensure_ascii=False, indent=1), "utf-8")
        except Exception as ex:
            print("⚠ 写图失败:%s" % ex, file=sys.stderr)
        try:
            pd = json.loads(PENDING_EDGES.read_text("utf-8")) if PENDING_EDGES.exists() else []
        except Exception:
            pd = []
        for m in made:
            pd.extend(m["pending"])
        PENDING_EDGES.write_text(json.dumps(pd, ensure_ascii=False, indent=1), "utf-8")
        _save_codes(reg)
        print("✓ 落盘 %d 篇 + 边/挂起/注册表" % len(made))
    return made


def detect_only():
    """quick_sync 用:零 AI 评估注意力榜哪些词过门,落 state/attention/autonote-candidates.json 供观察。"""
    vocab = PC._vocab_set()
    reg = _load_codes()
    try:
        focus = json.loads(FOCUS.read_text("utf-8")).get("top", [])[:TOP_N]
    except Exception:
        focus = []
    try:
        pos = json.loads(POSITIONS.read_text("utf-8"))
    except Exception:
        pos = {}
    out = []
    dirty = False
    for row in focus:
        refs = row.get("refs") or []
        book = refs[0]["file"] if refs else ""
        if not book or not book.lower().endswith(".pdf"):
            continue
        # R4-P0-1:登记先于门控且不受科目门限制——新书自动落表 enabled:false,
        # 用户才有现成条目可改 true(否则 没登记→科目门拒→永远到不了登记 死锁)
        if book not in reg["books"]:
            _book_entry(reg, book, create=True)
            dirty = True
        ok, blocks = _gates(row["term"], book, vocab, reg, focus_row=row)
        out.append({"term": row["term"], "book": book, "pass": ok, "blocks": blocks})
    if dirty:
        _save_codes(reg)
    fp = STATE / "attention" / "autonote-candidates.json"
    fp.write_text(json.dumps({"ts": int(time.time()), "candidates": out}, ensure_ascii=False, indent=1), "utf-8")
    n_pass = sum(1 for x in out if x["pass"])
    print("autonote 候选:%d 词评估,%d 过门" % (len(out), n_pass))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="真落盘(默认 dry-run)")
    ap.add_argument("--force-term")
    ap.add_argument("--book")
    ap.add_argument("--page", type=int)
    ap.add_argument("--detect-only", action="store_true", help="零AI:只评估门控落候选文件(quick_sync 用)")
    a = ap.parse_args()
    if a.detect_only:
        detect_only()
        sys.exit(0)
    run(dry=not a.run, force_term=a.force_term, force_book=a.book, force_page=a.page)
