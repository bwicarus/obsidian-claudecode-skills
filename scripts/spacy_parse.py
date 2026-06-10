#!/usr/bin/env python3
"""用 spaCy 做英文句子的词性标注 + 依存句法分析，输出 displaCy 风格 JSON。

在独立 venv（默认 /home/bwicarus/spacy-venv）里跑，被 webapp 通过 subprocess 调用
（webapp 跑系统 python，装不了 spaCy，故隔离 venv + subprocess）。

用法：
    python spacy_parse.py "The cat sat on the mat."
    echo '<sentence>' | python spacy_parse.py
输出 stdout（UTF-8 JSON）：
    {"tokens":[{"text","pos"}...], "deps":[{"head","child","label"}...]}

pos 用前端约定的小写英文（noun/verb/adj/...）；dep label 译成中文关系名。
中文词义(zh) 和整句翻译(sentence_zh) 不在这里做——由 webapp 端用 ECDICT + MyMemory 补。
"""
import sys
import json

# spaCy UPOS → 前端词性键
UPOS = {
    "NOUN": "noun", "PROPN": "noun", "VERB": "verb", "AUX": "aux",
    "ADJ": "adj", "ADV": "adv", "PRON": "pron", "ADP": "prep",
    "DET": "det", "CCONJ": "conj", "SCONJ": "conj", "NUM": "num",
    "PART": "part", "INTJ": "intj", "PUNCT": "punct", "SYM": "punct",
    "X": "part", "SPACE": "punct",
}

# spaCy 英文依存标签 → 中文关系名（教学友好）
DEP = {
    "nsubj": "主语", "nsubjpass": "被动主语", "csubj": "主语从句", "csubjpass": "被动主语从句",
    "dobj": "宾语", "obj": "宾语", "iobj": "间接宾语", "pobj": "介词宾语", "dative": "与格",
    "obl": "状语", "attr": "表语", "acomp": "形容词补语", "oprd": "宾补",
    "ccomp": "补语从句", "xcomp": "补语", "amod": "定语", "advmod": "状语",
    "det": "限定", "predet": "前限定", "prep": "介词", "case": "格标记",
    "aux": "助动", "auxpass": "被动助动", "cop": "系动",
    "cc": "连词", "conj": "并列", "mark": "引导词",
    "relcl": "定语从句", "acl": "修饰从句", "advcl": "状语从句",
    "poss": "所有格", "nmod": "名词修饰", "nummod": "数量定语", "quantmod": "数量修饰",
    "compound": "复合", "prt": "小品词", "neg": "否定", "expl": "形式主语",
    "appos": "同位", "agent": "施事", "npadvmod": "名词状语", "pcomp": "介词补语",
    "nounmod": "名词修饰", "poss_mod": "所有格", "dep": "依存", "punct": "标点", "intj": "叹词",
}

_NLP = None


def _load():
    global _NLP
    if _NLP is None:
        import spacy
        # 只需 tagger + parser，关掉 ner 提速
        _NLP = spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"])
    return _NLP


def parse(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"tokens": [], "deps": [], "clauses": [], "components": []}
    nlp = _load()
    doc = nlp(text)
    tokens = [{"text": t.text, "pos": UPOS.get(t.pos_, t.pos_.lower())} for t in doc]
    deps = []
    for t in doc:
        if t.dep_ == "ROOT" or t.head.i == t.i:
            continue
        deps.append({
            "head": t.head.i, "child": t.i,
            "label": DEP.get(t.dep_, t.dep_),
        })
    return {
        "tokens": tokens, "deps": deps,
        "clauses": _split_clauses(doc),
        "components": _split_components(doc),
        "clause_tree": _clause_tree(doc),
    }


def _clause_tree(doc):
    """嵌套子句树（供可逐级展开的弧线图）：
    每层 = {label, nodes:[{text,pos,ref?}], deps:[{head,child,label}], children:[子层]}。
    本层只含直属本子句的词；子从句收成一个占位节点 ⟨从句类型⟩(pos='clause', ref=子层下标)，
    点占位即可展开那一层的弧线。"""
    root = None
    for t in doc:
        if t.dep_ == "ROOT":
            root = t
            break
    if root is None:
        return None

    def nearest(t):   # 向上第一个子句根(含 ROOT)，返回 token.i（spaCy Token 不能用 is 比较）
        cur, hops = t, 0
        while hops < 300:
            if cur.i == root.i or cur.dep_ in _CLAUSE_BOUNDARY:
                return cur.i
            if cur.head.i == cur.i:
                return cur.i
            cur = cur.head
            hops += 1
        return cur.i

    def build(head, label):
        # 直属子从句根：head 子树里 dep∈从句 且 其 head 仍属本层
        subs = [t for t in head.subtree
                if t.i != head.i and t.dep_ in _CLAUSE_BOUNDARY and nearest(t.head) == head.i]
        # 本层自有词（不进入任何子从句）
        own = [t for t in head.subtree if nearest(t) == head.i and not (t.is_space or t.is_punct)]
        seq = [(t.i, "tok", t) for t in own] + [(s.i, "sub", s) for s in subs]
        seq.sort(key=lambda x: x[0])
        nodes, idxmap, children = [], {}, []
        for gi, kind, tok in seq:
            idxmap[gi] = len(nodes)
            if kind == "tok":
                nodes.append({"text": tok.text, "pos": UPOS.get(tok.pos_, tok.pos_.lower())})
            else:
                clab = _CLAUSE_DEP.get(tok.dep_, "从句")
                ci = len(children)
                children.append(build(tok, clab))
                nodes.append({"text": "⟨" + clab + "⟩", "pos": "clause", "ref": ci})
        deps = []
        for gi, kind, tok in seq:
            if tok.i == head.i:
                continue
            h = tok.head
            if h.i in idxmap and tok.i in idxmap and h.i != tok.i:
                deps.append({"head": idxmap[h.i], "child": idxmap[tok.i], "label": DEP.get(tok.dep_, tok.dep_)})
        return {"label": label, "nodes": nodes, "deps": deps, "children": children}

    return build(root, "主句")


# 依存标签 → 中文句子成分名（成分分块用）
_COMPONENT = {
    "nsubj": "主语", "nsubjpass": "主语", "csubj": "主语从句", "csubjpass": "主语从句",
    "expl": "形式主语",
    "dobj": "宾语", "obj": "宾语", "iobj": "间接宾语", "dative": "间接宾语", "pobj": "介词宾语",
    "attr": "表语", "acomp": "表语", "oprd": "宾语补足语",
    "ccomp": "宾语从句", "xcomp": "补语", "advcl": "状语从句", "relcl": "定语从句", "acl": "定语",
    "advmod": "状语", "npadvmod": "状语", "prep": "状语", "agent": "施事", "obl": "状语",
    "cc": "连词", "conj": "并列成分", "mark": "引导词", "intj": "感叹",
    "parataxis": "插入语", "appos": "同位语", "dep": "成分", "punct": "标点",
}


_CLAUSE_BOUNDARY = {"relcl", "ccomp", "advcl", "acl", "csubj", "csubjpass", "xcomp", "pcomp"}


def _split_components(doc) -> list:
    """把句子按「句子成分」线性切块：主语/谓语/宾语/状语/各类从句…
    成分边界 = ROOT(谓语) + ROOT 的直接非粘连子(顶层成分头) + 所有从句根(从宿主里挖出)。
    每个非标点 token 归属于最近的边界祖先。返回 [{label(中文), text}] 按出现顺序。
    无弧线、可换行，长句也清晰。"""
    root = None
    for t in doc:
        if t.dep_ == "ROOT":
            root = t
            break
    if root is None:
        return []
    # 成分根（任意层级都算，支持逐级展开）：主谓宾定状从句等实义成分；
    # 附属(det/amod/aux/cop/case/compound/poss/nummod)归核心短语、不单列，避免过碎
    MAJOR = {
        "nsubj", "nsubjpass", "csubj", "csubjpass", "dobj", "obj", "iobj", "dative", "pobj",
        "attr", "acomp", "oprd", "ccomp", "xcomp", "advcl", "relcl", "acl",
        "advmod", "npadvmod", "prep", "agent", "obl", "conj", "appos", "parataxis", "expl",
    }
    boundaries = {root.i: "谓语"}
    for t in doc:
        if t.i != root.i and t.dep_ in MAJOR:
            boundaries[t.i] = _COMPONENT.get(t.dep_, t.dep_)

    def owner(t):
        cur, hops = t, 0
        while hops < 200:
            if cur.i in boundaries:
                return cur.i
            if cur.head == cur:
                return None
            cur = cur.head
            hops += 1
        return None

    groups = {}
    for t in doc:
        if t.is_punct or t.is_space:
            continue
        o = owner(t)
        if o is not None:
            groups.setdefault(o, []).append(t.i)

    items = []
    for hi, idxs in groups.items():
        idxs = sorted(idxs)
        # 自有 token 拼接（不含被挖出的下级成分），各级文本不重复
        text = " ".join(doc[i].text for i in idxs).strip()
        if not (text and any(ch.isalnum() for ch in text)):
            continue
        items.append({"hi": hi, "label": boundaries[hi], "text": text, "start": idxs[0]})
    items.sort(key=lambda c: c["start"])
    # 挂靠关系：每个成分修饰的上层成分（parent 序号，-1=顶层）。
    # 句子级成分(主谓宾，挂 ROOT)都算顶层平级；从句内成分 / 定语从句 缩进在其宿主下。
    bt = {c["hi"]: i for i, c in enumerate(items)}   # boundary token i → 成分序号
    out = []
    for c in items:
        hi = c["hi"]
        if hi == root.i:
            parent = -1
        else:
            ph = owner(doc[hi].head)
            parent = -1 if (ph is None or ph == root.i) else bt.get(ph, -1)
        out.append({"label": c["label"], "text": c["text"], "parent": parent, "start": c["start"]})
    return out


# 子句根的依存标签 → 中文从句名（长句按这些切段，每段单独画小图）
_CLAUSE_DEP = {
    "relcl": "定语从句", "advcl": "状语从句", "ccomp": "宾语从句", "xcomp": "补语从句",
    "acl": "修饰从句", "csubj": "主语从句", "csubjpass": "被动主语从句", "pcomp": "介词从句",
}


def _split_clauses(doc) -> list:
    """按依存树把句子切成子句段：每个子句根(ROOT/relcl/advcl/...)管辖一段。
    每个 token 归属于最近的「子句根祖先」，从而把嵌套从句从主句里挖出来。
    返回 [{label, tokens:[{text,pos}], deps:[{head,child,label}](段内局部 index)}]。"""
    roots = {}   # token.i -> 中文标签
    for t in doc:
        if t.dep_ == "ROOT":
            roots[t.i] = "主句"
        elif t.dep_ in _CLAUSE_DEP:
            roots[t.i] = _CLAUSE_DEP[t.dep_]
        elif t.dep_ == "conj" and t.pos_ in ("VERB", "AUX") and t.head.pos_ in ("VERB", "AUX"):
            roots[t.i] = "并列分句"

    def owner(t):
        cur, hops = t, 0
        while hops < 200:
            if cur.i in roots:
                return cur.i
            if cur.head.i == cur.i:
                return None
            cur = cur.head
            hops += 1
        return None

    groups = {}   # root.i -> [token.i]
    for t in doc:
        o = owner(t)
        if o is not None:
            groups.setdefault(o, []).append(t.i)

    items = sorted(
        ({"root": ri, "label": roots[ri], "idx": sorted(idxs)} for ri, idxs in groups.items()),
        key=lambda c: c["idx"][0],
    )
    clauses = []
    for c in items:
        idx = c["idx"]
        if not idx:
            continue
        pos_map = {gi: li for li, gi in enumerate(idx)}
        toks = [{"text": doc[gi].text, "pos": UPOS.get(doc[gi].pos_, doc[gi].pos_.lower())} for gi in idx]
        dps = []
        for gi in idx:
            t = doc[gi]
            if t.dep_ == "ROOT" or t.head.i == t.i:
                continue
            if t.head.i in pos_map and t.i in pos_map:   # 只保留段内边
                dps.append({"head": pos_map[t.head.i], "child": pos_map[t.i], "label": DEP.get(t.dep_, t.dep_)})
        clauses.append({"label": c["label"], "tokens": toks, "deps": dps})
    return clauses


def serve():
    """常驻 worker 模式(--server):启动即加载模型,stdin 每行一个 {"text":...},
    每行回一个 JSON + flush。webapp 常驻复用 → 免掉每句 ~3.3s 的进程+模型加载税。
    JSON 编码天然处理句内换行;空行跳过;EOF/管道断 → 退出。"""
    _load()   # 预加载,首个请求即快
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            text = (json.loads(line) or {}).get("text", "")
            out = parse(text)
        except Exception as ex:
            out = {"error": str(ex), "tokens": [], "deps": [], "clauses": [], "components": []}
        print(json.dumps(out, ensure_ascii=False), flush=True)


def main():
    if "--server" in sys.argv:
        serve()
        return
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        out = parse(text)
    except Exception as ex:
        print(json.dumps({"error": str(ex), "tokens": [], "deps": [], "clauses": [], "components": []}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
