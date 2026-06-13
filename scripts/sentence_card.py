#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免 AI 的「例句 Anki 卡」构建器(地基件)。

输入已知的「日文句子 + 中文译文 + 来源」,产出一张 Anki 卡:
  正面 = 纯日文整句(考你能不能读+理解)
  背面 = 逐词振假名读音(<ruby>,Anki 原生) + 整句中文 + 来源(书·页)

零 AI:
  - 读音:fugashi(unidic)离线分词 → 每个含汉字的词包 <ruby>表层<rt>平假名</rt></ruby>
  - 译文:调用方给(来自 translate 模块的 no_ai 缓存),本模块不翻译
  - 建卡:复用 anki_from_word 的 AnkiConnect 通路(localhost:8765)

去重 = 隐藏 Key 字段(sha1(file|page|text))先 findNotes;退役 = 该句未掌握词全学会 → suspend。

CLI: python3 sentence_card.py --selftest   # 建一张测试卡→打印字段→删除,验证端到端
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "vocab"))
from anki_from_word import anki_call, ensure_deck  # 复用底层 AnkiConnect 调用 + 幂等建组

SENT_DECK = "例句"
SENT_MODEL = "例句JP"
_FIELDS = ["Sentence", "Reading", "Translation", "Source", "Link", "Key"]


def _pdf_link_html(file_rel: str, page: int, label: str) -> str:
    """拼回原文那一页的深链(同 anki_from_word:WEBAPP_BASE_URL,Pi=Tailscale 内网,手机直连)。"""
    if not file_rel:
        return ""
    base = os.environ.get("WEBAPP_BASE_URL", "https://bwicarus.space").rstrip("/")
    url = f"{base}/pdf/view?file={urllib.parse.quote(file_rel, safe='/')}&page={page}"
    return f'<a href="{html.escape(url)}" target="_blank">📖 {html.escape(label)}</a>'

# ── 离线读音(fugashi/unidic),全局缓存 Tagger ──
_TAGGER = None
_TAGGER_TRIED = False


def _tagger():
    global _TAGGER, _TAGGER_TRIED
    if _TAGGER is None and not _TAGGER_TRIED:
        _TAGGER_TRIED = True
        try:
            import fugashi
            _TAGGER = fugashi.Tagger()
        except Exception as e:
            sys.stderr.write(f"[sentence_card] fugashi 不可用: {e}\n")
            _TAGGER = None
    return _TAGGER


def _kata_to_hira(s: str) -> str:
    """片假名→平假名(同 pdf_reader._kata_to_hira:U+30A1..30F6 偏移 -0x60,长音等保留)。"""
    out = []
    for ch in s or "":
        o = ord(ch)
        if 0x30A1 <= o <= 0x30F6:
            out.append(chr(o - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def _has_kanji(s: str) -> bool:
    return any(0x3400 <= ord(c) <= 0x9FFF or 0xF900 <= ord(c) <= 0xFAFF for c in s)


def reading_ruby(text: str) -> str:
    """整句 → 逐词 <ruby> HTML(含汉字词才加读音)。fugashi 不可用则回退纯文本转义。"""
    t = _tagger()
    if t is None:
        return html.escape(text)
    out = []
    for w in t(text):
        surf = w.surface
        kana = getattr(w.feature, "kana", None) or getattr(w.feature, "pron", None)
        hira = _kata_to_hira(kana) if kana else ""
        if surf and _has_kanji(surf) and hira and hira != surf:
            out.append(f"<ruby>{html.escape(surf)}<rt>{html.escape(hira)}</rt></ruby>")
        else:
            out.append(html.escape(surf))
    return "".join(out)


def sentence_key(file_rel: str, page: int, text: str) -> str:
    return hashlib.sha1(f"{file_rel}|{page}|{text}".encode("utf-8")).hexdigest()[:16]


# ── Anki 卡型(幂等创建 + 每次同步模板/CSS,改了即生效于旧卡)──
_CARD_FRONT = '<div class="jp-sent">{{Sentence}}</div>'
_CARD_BACK = (
    '<div class="jp-sent">{{Reading}}</div>'
    '<hr>'
    '<div class="zh">{{Translation}}</div>'
    '{{#Link}}<div class="src">{{Link}}</div>{{/Link}}'
    '{{^Link}}<div class="src">{{Source}}</div>{{/Link}}'
)
_CARD_CSS = """
.card { font-family: -apple-system, "Hiragino Sans", "Noto Sans CJK JP", sans-serif;
        font-size: 26px; text-align: center; color: #1a1a2e; background: #fbfbfd; }
.jp-sent { line-height: 2.1; margin: 14px 8px; }
ruby rt { font-size: 0.5em; color: #6b7280; font-weight: 400; }
hr { border: none; border-top: 1px solid #e3e3ea; margin: 16px 0; }
.zh { font-size: 21px; color: #0a5; margin: 10px 6px; }
.src { font-size: 14px; margin-top: 16px; }
.src a { color: #2563eb; text-decoration: underline; }
.nightMode .src a { color: #6ea8fe; }
.nightMode .card { color: #e6e6ef; background: #16161e; }
.nightMode ruby rt { color: #8b93a3; }
.nightMode .zh { color: #4ade80; }
"""


def ensure_sentence_model() -> None:
    """幂等创建/同步「例句JP」模型。已存在则更新模板+CSS(让卡面改动作用于旧卡)。"""
    names = anki_call("modelNames") or []
    if SENT_MODEL not in names:
        anki_call("createModel", {
            "modelName": SENT_MODEL,
            "inOrderFields": _FIELDS,
            "css": _CARD_CSS,
            "cardTemplates": [{"Name": "例句", "Front": _CARD_FRONT, "Back": _CARD_BACK}],
        })
        return
    # 已存在:确保字段齐全(早期缺 Key 等)再同步模板
    try:
        cur = anki_call("modelFieldNames", {"modelName": SENT_MODEL}) or []
        for f in _FIELDS:
            if f not in cur:
                anki_call("modelFieldAdd", {"modelName": SENT_MODEL, "fieldName": f,
                                            "index": _FIELDS.index(f)})
    except Exception:
        pass
    anki_call("updateModelTemplates", {"model": {
        "name": SENT_MODEL,
        "templates": {"例句": {"Front": _CARD_FRONT, "Back": _CARD_BACK}}}})
    anki_call("updateModelStyling", {"model": {"name": SENT_MODEL, "css": _CARD_CSS}})


def _note_ids_for_key(key: str) -> list[int]:
    try:
        return anki_call("findNotes", {"query": f'"Key:{key}"'}) or []
    except Exception:
        return []


def make_sentence_card(*, file_rel: str, page: int, text: str, zh: str,
                       lemmas: list[str] | None = None, source: str = "",
                       deck: str = SENT_DECK) -> dict:
    """建一张例句卡(去重:同 Key 已存在则跳过)。返回 {ok, action, note_id, key}。"""
    key = sentence_key(file_rel, page, text)
    existing = _note_ids_for_key(key)
    if existing:
        return {"ok": True, "action": "exists", "note_id": existing[0], "key": key}
    ensure_deck(deck)
    ensure_sentence_model()
    reading = reading_ruby(text)
    tags = ["例句"]
    bk = (source or file_rel).split("/")[-1].replace(".pdf", "")
    tags.append("book::" + bk.replace(" ", "_"))
    if page:
        tags.append(f"page::{page}")
    for lm in (lemmas or [])[:12]:
        tags.append("word::" + str(lm).replace(" ", "_"))
    src_text = source or (bk + (f" · p{page}" if page else ""))
    # 链接直接进 Source(原有字段)→ 任何模板版本都显示,免新字段引发的 schema full-sync 摩擦
    src_val = _pdf_link_html(file_rel, page, src_text) or html.escape(src_text)
    note = {
        "deckName": deck, "modelName": SENT_MODEL,
        "fields": {"Sentence": html.escape(text), "Reading": reading,
                   "Translation": html.escape(zh or ""),
                   "Source": src_val, "Link": "",
                   "Key": key},
        "options": {"allowDuplicate": True},   # 我们用 Key 自管去重,不靠 Anki 内容查重
        "tags": tags,
    }
    nid = anki_call("addNote", {"note": note})
    # 这套 headless Anki 的 addNote 会忽略 deckName(落「系统默认」)→ 显式 changeDeck 兜底
    if nid:
        try:
            cids = anki_call("findCards", {"query": f'"Key:{key}"'}) or []
            if cids:
                anki_call("changeDeck", {"cards": cids, "deck": deck})
        except Exception:
            pass
    return {"ok": bool(nid), "action": "added", "note_id": nid, "key": key}


def suspend_sentence_card(key: str) -> dict:
    """该句未掌握词已全部学会 → suspend 它的卡(留历史不删)。返回 {ok, suspended}。"""
    nids = _note_ids_for_key(key)
    if not nids:
        return {"ok": True, "suspended": 0}
    cids = anki_call("findCards", {"query": f'"Key:{key}"'}) or []
    if cids:
        anki_call("suspend", {"cards": cids})
    return {"ok": True, "suspended": len(cids)}


def _selftest() -> int:
    s = "公開鍵暗号方式では受信者の公開鍵で暗号化する。"
    print("reading_ruby:\n ", reading_ruby(s))
    r = make_sentence_card(file_rel="_selftest/demo.pdf", page=1, text=s,
                           zh="在公开密钥加密方式中，用接收者的公开密钥进行加密。",
                           lemmas=["暗号化", "受信者"], source="自检 · p1")
    print("make_sentence_card:", r)
    if r.get("note_id"):
        info = anki_call("notesInfo", {"notes": [r["note_id"]]})
        print("fields:", {k: v["value"][:60] for k, v in info[0]["fields"].items()})
        anki_call("deleteNotes", {"notes": [r["note_id"]]})
        print("已删除测试卡,Anki 不留痕")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    ap.print_help()
