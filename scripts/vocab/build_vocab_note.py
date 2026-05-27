"""根据 compose_entry() 结果生成 / 更新 vault 里的 vocab 笔记。

路径：<VAULT>/资源/vocab/<首字母>/<lemma>.md

合并策略：
  - 重新生成 `<!-- USER NOTES BELOW -->` 之上的全部内容
  - frontmatter 内 user_note / mastery / first_seen 等用户/算法字段保留并合并
  - lookup_count / last_lookup / exposure_count 由调用者传入新增量
  - sources 累加，去重 (按 pdf + page)

CLI：
  python3 scripts/vocab/build_vocab_note.py construction
  python3 scripts/vocab/build_vocab_note.py construction \\
      --add-source-pdf '资源/books/000-LADR/X.pdf' --page 18 --context '...'
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT   = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
CFG_PATH     = PROJECT_ROOT / "state" / "server-config.json"

sys.path.insert(0, str(Path(__file__).parent))
from dict_sources import compose_entry   # noqa: E402

_USER_NOTES_MARKER = "<!-- USER NOTES BELOW -->"


def _cfg() -> dict:
    try:
        return json.loads(CFG_PATH.read_text("utf-8")).get("vocab", {})
    except Exception:
        return {}


def _vocab_dir() -> Path:
    return VAULT_ROOT / _cfg().get("vault_subdir", "资源/vocab")

def _audio_dir() -> Path:
    return VAULT_ROOT / _cfg().get("audio_subdir", "资源/vocab/_audio")


def _word_path(lemma: str) -> Path:
    lemma = lemma.lower()
    first = lemma[0] if lemma and lemma[0].isalpha() else "_"
    return _vocab_dir() / first / f"{lemma}.md"


def _load_existing(path: Path) -> tuple[dict, str]:
    """返回 (frontmatter dict, user_notes_part)。"""
    if not path.exists():
        return {}, ""
    raw = path.read_text("utf-8")
    fm: dict = {}
    body = raw
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end > 0:
            fm_text = raw[4:end]
            body = raw[end + 5:]
            try:
                # 简易 YAML：仅 key: value / key: [a, b] / 多行 list 用 - 前缀
                fm = _parse_simple_yaml(fm_text)
            except Exception:
                fm = {}
    user_notes = ""
    if _USER_NOTES_MARKER in body:
        # 用 rsplit 取最后一次出现之后的内容（防止说明文本里曾经字面出现 marker
        # 导致每次重写都把旧 marker 当成用户备注保留 → 雪崩重复）
        user_notes = body.rsplit(_USER_NOTES_MARKER, 1)[-1]
    return fm, user_notes


def _parse_simple_yaml(text: str) -> dict:
    """简易 YAML：支持 key: scalar / key: [a,b] / key: 然后下面 -  - 多行 list。

    空值（key: 之后没字符）→ ""，仅当下一行出现 `  - xxx` 才转为 list。
    """
    out: dict = {}
    cur_key = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            if cur_key is not None:
                if not isinstance(out.get(cur_key), list):
                    out[cur_key] = []
                out[cur_key].append(line[4:].strip().strip('"\''))
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v == "":
            out[k] = ""        # 默认空字符串；遇到 `- xxx` 时才转 list
            cur_key = k
        elif v == "[]":
            out[k] = []
            cur_key = None
        elif v.startswith("[") and v.endswith("]"):
            items = [x.strip().strip('"\'') for x in v[1:-1].split(",") if x.strip()]
            out[k] = items
            cur_key = None
        else:
            out[k] = v.strip().strip('"\'')
            cur_key = None
    return out


def _dump_simple_yaml(fm: dict) -> str:
    lines = []
    for k, v in fm.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for it in v:
                    if isinstance(it, dict):
                        # 多键 dict（如 sources 数组）
                        first = True
                        for kk, vv in it.items():
                            prefix = "  - " if first else "    "
                            lines.append(f"{prefix}{kk}: {_yaml_scalar(vv)}")
                            first = False
                    else:
                        lines.append(f"  - {_yaml_scalar(it)}")
        elif isinstance(v, dict):
            lines.append(f"{k}:")
            for kk, vv in v.items():
                lines.append(f"  {kk}: {_yaml_scalar(vv)}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    return "\n".join(lines)


def _yaml_scalar(v) -> str:
    if v is None: return "null"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int, float)): return str(v)
    s = str(v)
    if any(c in s for c in ":#[]{}\n,") or s.startswith(("- ", "  ", "? ")):
        return json.dumps(s, ensure_ascii=False)
    return s


def _slug_for_audio(lemma: str, region: str) -> str:
    return f"{lemma.lower()}-{region}.mp3"


def _download_audio(url: str, out_path: Path) -> bool:
    if not url or out_path.exists():
        return out_path.exists()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "bwicarus-vocab/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        out_path.write_bytes(data)
        return True
    except Exception:
        return False


def _obsidian_url_for_pdf(rel_pdf: str, page: int) -> str:
    # Obsidian PDF.js plugin 支持 #page=N
    vault_name = VAULT_ROOT.name
    enc = "vault=" + vault_name + "&file=" + rel_pdf.replace(" ", "%20")
    return f"obsidian://open?{enc}#page={page}"


def _webapp_url_for_pdf(rel_pdf: str, page: int) -> str:
    return f"https://bwicarus.space/pdf/view?file={rel_pdf}&page={page}"


def render_md(entry: dict, fm_extra: dict, sources: list[dict], user_notes: str = "") -> str:
    """完整渲染 .md 文本。"""
    lemma = entry["lemma"]
    fm = {
        "word": lemma,
        "lemma": lemma,
        "forms": entry.get("forms", [lemma]),
        "phonetic_us": entry.get("phonetics", {}).get("us", ""),
        "phonetic_uk": entry.get("phonetics", {}).get("uk", ""),
        "pos": entry.get("pos", []),
        "freq_bnc": entry.get("freq", {}).get("bnc", 0),
        "freq_coc": entry.get("freq", {}).get("coc", 0),
        "audio_us": fm_extra.get("audio_us", ""),
        "audio_uk": fm_extra.get("audio_uk", ""),
        "first_seen": fm_extra.get("first_seen", _dt.date.today().isoformat()),
        "last_lookup": fm_extra.get("last_lookup", _dt.date.today().isoformat()),
        "last_lookup_ts": fm_extra.get("last_lookup_ts", 0),
        "lookup_count": fm_extra.get("lookup_count", 1),
        "exposure_count": fm_extra.get("exposure_count", 0),
        "mastery": fm_extra.get("mastery", 0.0),
        "mastery_label": fm_extra.get("mastery_label", "新词"),
        "anki_card_id": fm_extra.get("anki_card_id", ""),
        "sources_hit": entry.get("sources_hit", []),
        "tags": ["vocab", f"vocab/{fm_extra.get('mastery_label_slug', 'new')}"],
    }
    out: list[str] = []
    out.append("---")
    out.append(_dump_simple_yaml(fm))
    out.append("---")
    out.append("")
    out.append(f"# {lemma}")
    out.append("")
    # 音频 + 音标
    badge_parts = []
    if fm["audio_us"]:
        badge_parts.append(f"🔊 ![[{Path(fm['audio_us']).name}]]")
    if fm["phonetic_us"]:
        badge_parts.append(f"*US {fm['phonetic_us']}*")
    if fm["phonetic_uk"]:
        badge_parts.append(f"*UK {fm['phonetic_uk']}*")
    if fm["freq_bnc"]:
        badge_parts.append(f"BNC #{fm['freq_bnc']}")
    if badge_parts:
        out.append(" · ".join(badge_parts))
        out.append("")

    # 词性 + 词形
    if entry.get("forms"):
        out.append(f"**词形**：{' / '.join(entry['forms'])}")
        out.append("")

    # 释义按源分块
    zh_defs = [d for d in entry["definitions"] if d.get("zh")]
    ec_en_defs = [d for d in entry["definitions"] if d.get("source") == "ecdict_en"]
    mw_defs = [d for d in entry["definitions"] if d.get("source") == "mw"]
    wi_defs = [d for d in entry["definitions"] if d.get("source") == "wiktionary"]

    if zh_defs:
        out.append("## 📖 朗道双语 / 21世纪大英汉")
        for d in zh_defs:
            pos = d.get("pos") or ""
            out.append(f"- **{pos}** {d['zh']}" if pos else f"- {d['zh']}")
        out.append("")

    zh_for = entry.get("examples_zh", {}) or {}
    def _ex_block(ex_list, indent="  "):
        lines = []
        for ex in ex_list:
            lines.append(f"{indent}> {ex}")
            zh = zh_for.get(ex)
            if zh:
                lines.append(f"{indent}> 🇨🇳 {zh}")
        return lines

    if mw_defs:
        out.append("## 📚 Merriam-Webster Learner's")
        for d in mw_defs:
            pos = d.get("pos") or ""
            out.append(f"- **{pos}** {d['en']}" if pos else f"- {d['en']}")
            out.extend(_ex_block(d.get("examples", [])[:2]))
        out.append("")

    if wi_defs:
        out.append("## 🌐 Wiktionary")
        for d in wi_defs[:6]:
            pos = d.get("pos") or ""
            out.append(f"- **{pos}** {d['en']}" if pos else f"- {d['en']}")
            out.extend(_ex_block(d.get("examples", [])[:1]))
        out.append("")

    if ec_en_defs and not mw_defs and not wi_defs:
        out.append("## 📘 ECDICT (EN)")
        for d in ec_en_defs:
            pos = d.get("pos") or ""
            out.append(f"- **{pos}** {d['en']}" if pos else f"- {d['en']}")
        out.append("")

    # 例句汇总（按源分头都列在上面了；这里只做"额外例句"）
    extra_examples = [e for e in entry.get("examples", []) if not any(
        e in d.get("examples", []) for d in mw_defs + wi_defs
    )]
    if extra_examples:
        out.append("## 💬 更多例句")
        for ex in extra_examples[:8]:
            out.append(f"> {ex}")
            zh = zh_for.get(ex)
            if zh:
                out.append(f"> 🇨🇳 {zh}")
        out.append("")

    # 同义反义
    if entry.get("synonyms"):
        out.append(f"**近义词**：{', '.join(entry['synonyms'][:10])}")
        out.append("")
    if entry.get("antonyms"):
        out.append(f"**反义词**：{', '.join(entry['antonyms'][:10])}")
        out.append("")

    # 词源
    if entry.get("etymology"):
        out.append("## 📜 词源")
        out.append(entry["etymology"])
        out.append("")

    # 文中出现（仅用户实际查过的地方 + 所在整个句子）
    # 全文 exposure 数据保留在 state/vocab-exposure.json 仅用于 mastery 算法，不渲染到 .md
    if sources:
        out.append("## 📝 文中出现")
        for s in sources:
            if s.get("pdf"):
                pdf_rel = s["pdf"]
                page = int(s.get("page", 1))
                ctx = (s.get("context") or "").strip()
                # 整个句子保留（_expandSentenceFromRange 算的就是句子边界），仅多行折成单行
                ctx_one = re.sub(r"\s+", " ", ctx)
                out.append(f"### `{Path(pdf_rel).name}` · p.{page}")
                if ctx_one:
                    out.append(f"> {ctx_one}")
                out.append(f"- [在 PDF 阅读器打开 →]({_webapp_url_for_pdf(pdf_rel, page)})")
                out.append(f"- [在 Obsidian 打开 →]({_obsidian_url_for_pdf(pdf_rel, page)})")
                if s.get("ts"):
                    out.append(f"- 时间：{_dt.datetime.fromtimestamp(int(s['ts'])).strftime('%Y-%m-%d %H:%M')}")
                out.append("")
            elif s.get("note"):
                note_rel = s["note"]
                out.append(f"### [[{Path(note_rel).stem}]]")
                if s.get("context"):
                    out.append(f"> {s['context']}")
                out.append("")

    out.append("## 💭 个人备注")
    out.append("（请在下面分隔线之后写，脚本不会覆盖）")
    out.append("")
    out.append(_USER_NOTES_MARKER)
    if user_notes.strip():
        out.append(user_notes.lstrip("\n"))
    else:
        out.append("")

    return "\n".join(out) + "\n"


def update_word_note(
    word: str,
    *,
    add_source: dict | None = None,
    online: bool = True,
    download_audio: bool = True,
) -> tuple[Path, dict]:
    """生成 / 更新一个词的 vault 笔记，返回 (path, frontmatter)。"""
    entry = compose_entry(word, online=online)
    if not entry or not entry.get("lemma"):
        raise SystemExit(f"compose_entry returned empty for {word!r}")
    lemma = entry["lemma"]
    path = _word_path(lemma)
    path.parent.mkdir(parents=True, exist_ok=True)

    old_fm, user_notes = _load_existing(path)

    # 合并 sources
    sources = []
    if isinstance(old_fm.get("sources"), list):
        sources = list(old_fm["sources"])
    # 注：sources 在旧 frontmatter 里现在不是 list[dict]，是缺省
    # 我们改为：sources 字段不存 frontmatter，仅渲染 markdown 段落；存放结构在 state/vocab-sources.json

    # 用本调用新增的 source（如果有）
    new_source = None
    if add_source:
        s = {
            "pdf": add_source.get("pdf") or "",
            "note": add_source.get("note") or "",
            "page": add_source.get("page") or 0,
            "context": add_source.get("context") or "",
            "ts": add_source.get("ts") or int(time.time()),
        }
        new_source = s

    sources_db = _load_sources_db(lemma)
    if new_source:
        # 去重：同 pdf + page 视为同一来源（更新 ctx 和 ts）
        key = (new_source["pdf"], new_source["note"], int(new_source["page"]))
        existing_idx = -1
        for i, s in enumerate(sources_db):
            if (s.get("pdf",""), s.get("note",""), int(s.get("page",0))) == key:
                existing_idx = i; break
        if existing_idx >= 0:
            sources_db[existing_idx].update(new_source)
        else:
            sources_db.append(new_source)
    _save_sources_db(lemma, sources_db)

    # 音频下载（仅美音；UK 暂时 skip）
    audio_us_local = old_fm.get("audio_us") or ""
    audio_uk_local = old_fm.get("audio_uk") or ""
    if download_audio and entry["audio"]["us"]:
        fname_us = _slug_for_audio(lemma, "us")
        out_us = _audio_dir() / fname_us
        if _download_audio(entry["audio"]["us"], out_us):
            audio_us_local = f"资源/vocab/_audio/{fname_us}"
    if download_audio and entry["audio"]["uk"]:
        fname_uk = _slug_for_audio(lemma, "uk")
        out_uk = _audio_dir() / fname_uk
        if _download_audio(entry["audio"]["uk"], out_uk):
            audio_uk_local = f"资源/vocab/_audio/{fname_uk}"

    # mastery 模型（新）：用户主动查 = 不会，所以新建/再次查 → 重置 0.0
    # 默认 1.0 是"未查过的词"隐式状态，不会进入 vocab dir
    cur_mastery = 0.0 if new_source else float(old_fm.get("mastery", 0.0) or 0.0)
    # 用 paragraph_exposure 的 LABELS_NEW 保持一致
    try:
        from paragraph_exposure import _label_for as _label_fn
        lbl, slug = _label_fn(cur_mastery)
    except Exception:
        lbl, slug = "完全不会", "new"
    fm_extra = {
        "first_seen": old_fm.get("first_seen") or _dt.date.today().isoformat(),
        "last_lookup": _dt.date.today().isoformat(),
        "last_lookup_ts": int(time.time()) if new_source else int(old_fm.get("last_lookup_ts", 0) or 0),
        "lookup_count": int(old_fm.get("lookup_count", 0) or 0) + (1 if new_source else 0),
        "exposure_count": int(old_fm.get("exposure_count", 0) or 0),
        "mastery": cur_mastery,
        "mastery_label": lbl,
        "mastery_label_slug": slug,
        "anki_card_id": old_fm.get("anki_card_id", ""),
        "audio_us": audio_us_local,
        "audio_uk": audio_uk_local,
    }
    md_text = render_md(entry, fm_extra, sources_db, user_notes)
    path.write_text(md_text, "utf-8")
    return path, fm_extra


# ── sources db：存 state（不放 frontmatter，让 .md 更可读）─────────────────

_SOURCES_DB = PROJECT_ROOT / "state" / "vocab-sources.json"

def _load_sources_db_all() -> dict:
    try:
        return json.loads(_SOURCES_DB.read_text("utf-8"))
    except Exception:
        return {}

def _load_sources_db(lemma: str) -> list[dict]:
    return _load_sources_db_all().get(lemma.lower(), [])

def _save_sources_db(lemma: str, sources: list[dict]):
    db = _load_sources_db_all()
    db[lemma.lower()] = sources
    _SOURCES_DB.parent.mkdir(parents=True, exist_ok=True)
    _SOURCES_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("word")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--add-source-pdf", default="")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--context", default="")
    args = ap.parse_args()
    add_src = None
    if args.add_source_pdf:
        add_src = {"pdf": args.add_source_pdf, "page": args.page, "context": args.context}
    path, fm = update_word_note(
        args.word, add_source=add_src,
        online=not args.offline,
        download_audio=not args.no_audio,
    )
    print(f"OK {path}")
    print(json.dumps(fm, ensure_ascii=False, indent=2))
