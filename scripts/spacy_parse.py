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
        return {"tokens": [], "deps": []}
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
    return {"tokens": tokens, "deps": deps}


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        out = parse(text)
    except Exception as ex:
        print(json.dumps({"error": str(ex), "tokens": [], "deps": []}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
