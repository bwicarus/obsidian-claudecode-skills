"""一键把 vocab 笔记生成 Anki 卡，纯字典数据不调 AI。

模板：复用现有 'Saladict Word' 模板（字段 Text/Translation/Context/ContextCloze/
                  Note/Title/Url/Favicon/Audio/Date）
deck：'Vocab'（如不存在自动建）
tag：'vocab vocab/lemma::<word>'

副作用：把音频 mp3 通过 storeMediaFile 推到 Anki 媒体库；卡 Audio 字段写
        `[sound:<lemma>-us.mp3]`。把 cardId 写回 vocab .md frontmatter.anki_card_id。

CLI:
  python3 scripts/vocab/anki_from_word.py construction
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
CFG_PATH     = PROJECT_ROOT / "state" / "server-config.json"
ANKI_URL     = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")

sys.path.insert(0, str(Path(__file__).parent))
from dict_sources import compose_entry           # noqa: E402
from build_vocab_note import (_word_path, _load_existing, _audio_dir,
                              _load_sources_db, _parse_simple_yaml,
                              _USER_NOTES_MARKER)  # noqa: E402

ANKI_MODEL = "Obsidian-cloze"      # 字段 Text (cloze) + Extra；正反面
ANKI_DECK  = "Vocab"


def anki_call(action: str, params: dict | None = None, timeout: int = 15):
    payload = json.dumps({"action": action, "version": 6, "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(ANKI_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(f"AnkiConnect {action}: {body['error']}")
    return body.get("result")


def ensure_deck(name: str):
    decks = anki_call("deckNames") or []
    if name not in decks:
        anki_call("createDeck", {"deck": name})


def store_audio(lemma: str, audio_path: Path) -> str:
    """把 audio mp3 推 Anki media，返回 anki 内文件名。"""
    if not audio_path.exists():
        return ""
    fname = f"vocab-{lemma}-us.mp3"
    import base64
    data = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    anki_call("storeMediaFile", {"filename": fname, "data": data})
    return fname


def _esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_translation_html(entry: dict) -> str:
    """中文释义为主，附 MW/Wiktionary 简洁定义。"""
    parts = []
    zh_defs = [d for d in entry["definitions"] if d.get("zh")]
    if zh_defs:
        for d in zh_defs[:5]:
            pos = d.get("pos") or ""
            text = _esc_html(d["zh"])
            parts.append(f"<b>{pos}</b> {text}" if pos else text)
    mw_defs = [d for d in entry["definitions"] if d.get("source") == "mw"]
    if mw_defs:
        parts.append("<br><span style='color:#888;font-size:90%'>")
        for d in mw_defs[:3]:
            pos = d.get("pos") or ""
            text = _esc_html(d["en"])
            parts.append(f"<i>{pos}</i> {text}" if pos else text)
        parts.append("</span>")
    return "<br>".join(parts)


def _build_context(entry: dict, sources: list[dict], lemma: str) -> tuple[str, str, str]:
    """选最好的一条上下文，返回 (bold_html, cloze, zh_translation)。
    优先 PDF source 句子；其次 MW/Wiktionary 例句。zh 来自 entry.examples_zh 缓存。"""
    sentence = ""
    for s in sources:
        if (s.get("context") or "").strip():
            sentence = s["context"].strip()
            break
    if not sentence:
        for d in entry["definitions"]:
            for ex in (d.get("examples") or []):
                if lemma.lower() in ex.lower():
                    sentence = ex; break
            if sentence: break
    if not sentence and entry.get("examples"):
        for ex in entry["examples"]:
            if lemma.lower() in ex.lower():
                sentence = ex; break
        else:
            sentence = entry["examples"][0]
    if not sentence:
        return "", "", ""
    forms = entry.get("forms") or [lemma]
    pattern = r"\b(" + "|".join(re.escape(f) for f in sorted(set(forms), key=len, reverse=True)) + r")\b"
    bold = re.sub(pattern, lambda m: f"<b>{m.group(1)}</b>", sentence, count=1, flags=re.IGNORECASE)
    cloze = re.sub(pattern, lambda m: "{{c1::" + m.group(1) + "}}", sentence, count=1, flags=re.IGNORECASE)
    if "{{c1::" not in cloze:
        # 句子里没有词形（极少见），手动加在末尾
        cloze = sentence + " {{c1::" + lemma + "}}"
    zh = (entry.get("examples_zh") or {}).get(sentence, "")
    if not zh:
        # 现场翻译一次
        try:
            from translate import translate as _tr
            zh = _tr(sentence) or ""
        except Exception:
            pass
    return bold, cloze, zh


def _build_note_field(entry: dict) -> str:
    parts = []
    if entry.get("forms"):
        parts.append("<b>词形</b>：" + " / ".join(entry["forms"]))
    if entry.get("synonyms"):
        parts.append("<b>近义</b>：" + ", ".join(entry["synonyms"][:8]))
    if entry.get("antonyms"):
        parts.append("<b>反义</b>：" + ", ".join(entry["antonyms"][:8]))
    if entry.get("freq", {}).get("bnc"):
        parts.append(f"<span style='color:#888'>BNC #{entry['freq']['bnc']}</span>")
    return "<br>".join(parts)


def _build_title_url(sources: list[dict]) -> tuple[str, str]:
    if not sources:
        return "", ""
    s = sources[-1]   # 最近来源
    if s.get("pdf"):
        pdf_rel = s["pdf"]
        page = int(s.get("page", 1))
        title = f"{Path(pdf_rel).name} · p.{page}"
        url = f"https://bwicarus.space/pdf/view?file={pdf_rel}&page={page}"
        return title, url
    if s.get("note"):
        return Path(s["note"]).stem, ""
    return "", ""


def _update_card_id_in_md(lemma: str, card_id: int) -> bool:
    """把 anki_card_id 写回 vocab .md frontmatter。"""
    path = _word_path(lemma)
    if not path.exists():
        return False
    raw = path.read_text("utf-8")
    if not raw.startswith("---\n"):
        return False
    end = raw.find("\n---\n", 4)
    if end < 0:
        return False
    fm_text = raw[4:end]
    rest = raw[end:]   # 含 "\n---\n..." 起
    # 替换 anki_card_id 行 (或追加)
    if re.search(r"^anki_card_id:", fm_text, flags=re.M):
        fm_text = re.sub(r"^anki_card_id:.*$", f"anki_card_id: {card_id}", fm_text, flags=re.M)
    else:
        fm_text = fm_text.rstrip() + f"\nanki_card_id: {card_id}\n"
    path.write_text("---\n" + fm_text + rest, "utf-8")
    return True


def make_card(lemma: str, *, force: bool = False) -> dict:
    """主入口：根据现有 vocab .md + 三源字典构卡。"""
    word_path = _word_path(lemma)
    fm, _ = _load_existing(word_path) if word_path.exists() else ({}, "")
    existing_card_id = fm.get("anki_card_id")
    # 类型可能是空字符串 / [] / 数字字符串
    try:
        existing_card_id_int = int(existing_card_id) if existing_card_id and not isinstance(existing_card_id, list) else 0
    except (ValueError, TypeError):
        existing_card_id_int = 0
    if existing_card_id_int and not force:
        # 已有卡 → 更新字段而非新建
        pass

    entry = compose_entry(lemma, online=True)
    if not entry:
        # 连 ECDICT 都查不到（多半选错了非英文词）→ 正常退出 + 中文提示，前端 toast 友好
        return {"ok": False, "error": f"查不到「{lemma}」的字典数据，无法制卡"}
    lemma = entry["lemma"]
    sources_db = _load_sources_db(lemma)

    # 音频：从 vault audio 推到 anki media
    audio_field = ""
    audio_local = _audio_dir() / f"{lemma}-us.mp3"
    if audio_local.exists():
        anki_fname = store_audio(lemma, audio_local)
        if anki_fname:
            audio_field = f"[sound:{anki_fname}]"

    ensure_deck(ANKI_DECK)

    context_bold, context_cloze, context_zh = _build_context(entry, sources_db, lemma)
    title, url = _build_title_url(sources_db)

    # Extra 字段：富文本聚合（lemma + 音标 + 翻译 + 译句 + 例句 + 同义反义 + 词形 + 词频 + 来源 + 音频）
    extra_parts = [
        f"<h2 style='margin:4px 0'>{_esc_html(lemma)}</h2>",
        f"<div style='color:#888'>{_esc_html(entry['phonetics']['us'] or '')}{(' / ' + _esc_html(entry['phonetics']['uk'])) if entry['phonetics']['uk'] else ''}{(' ' + audio_field) if audio_field else ''}</div>",
        f"<hr><div>{_build_translation_html(entry)}</div>",
    ]
    if context_zh:
        extra_parts.append(f"<div style='margin-top:8px;color:#888'>🇨🇳 {_esc_html(context_zh)}</div>")
    # 其他例句
    other_examples = [e for e in entry.get("examples", []) if e not in {context_bold.replace('<b>','').replace('</b>','')}]
    if other_examples:
        ex_lines = []
        zh_map = entry.get("examples_zh") or {}
        for ex in other_examples[:4]:
            zh = zh_map.get(ex, "")
            ex_lines.append(f"<li>{_esc_html(ex)}" + (f"<br><span style='color:#888'>🇨🇳 {_esc_html(zh)}</span>" if zh else "") + "</li>")
        extra_parts.append(f"<details><summary style='cursor:pointer;color:#888'>更多例句</summary><ul style='margin:6px 0 0 18px;padding:0'>{''.join(ex_lines)}</ul></details>")
    note_field = _build_note_field(entry)
    if note_field:
        extra_parts.append(f"<div style='margin-top:8px;color:#666;font-size:90%'>{note_field}</div>")
    if title and url:
        extra_parts.append(f"<div style='margin-top:8px;font-size:80%'>来源：<a href='{_esc_html(url)}'>{_esc_html(title)}</a></div>")

    fields = {
        "Text":  context_cloze or f"{{{{c1::{lemma}}}}}",
        "Extra": "".join(extra_parts),
    }
    tags = ["vocab", f"vocab/lemma::{lemma}"]

    if existing_card_id_int:
        # 更新现有卡
        try:
            anki_call("updateNoteFields", {"note": {"id": existing_card_id_int, "fields": fields}})
            anki_call("addTags", {"notes": [existing_card_id_int], "tags": " ".join(tags)})
            return {"ok": True, "action": "updated", "note_id": existing_card_id_int}
        except Exception as ex:
            # 更新失败可能是 card 已被删，回落新建
            sys.stderr.write(f"updateNoteFields failed ({ex}), creating new\n")

    note = {
        "deckName": ANKI_DECK,
        "modelName": ANKI_MODEL,
        "fields": fields,
        "tags": tags,
        "options": {"allowDuplicate": False, "duplicateScope": "deck"},
    }
    try:
        note_id = anki_call("addNote", {"note": note})
    except Exception as ex:
        # duplicate 视为已存在
        if "duplicate" in str(ex).lower():
            # 查找现有卡
            found = anki_call("findNotes", {"query": f'deck:"{ANKI_DECK}" Text:"{lemma}*"'})
            if found:
                note_id = found[0]
                anki_call("updateNoteFields", {"note": {"id": note_id, "fields": fields}})
                anki_call("addTags", {"notes": [note_id], "tags": " ".join(tags)})
                _update_card_id_in_md(lemma, note_id)
                return {"ok": True, "action": "updated-duplicate", "note_id": note_id}
        raise

    if note_id:
        _update_card_id_in_md(lemma, note_id)
        return {"ok": True, "action": "created", "note_id": note_id}
    return {"ok": False, "error": "addNote returned no id"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("word")
    ap.add_argument("--force", action="store_true", help="即使已有 card 也重新创建")
    args = ap.parse_args()
    result = make_card(args.word, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
