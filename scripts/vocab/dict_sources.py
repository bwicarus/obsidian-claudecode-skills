"""字典多源融合：ECDICT (离线) + Free Dictionary API + Merriam-Webster Learner's API。

核心入口：
  compose_entry(word, *, online=True) → dict
    {
      "word": "construction",
      "lemma": "construction",                  # 屈折形态归原型
      "forms": ["construction", "constructions"],  # ECDICT exchange 派生
      "phonetics": {"us": "/.../", "uk": "/.../"},
      "audio": {"us": "...mp3", "uk": "...mp3"}, # 远程 URL，本地下载在 vocab 流程外
      "pos": ["n.", "v."],
      "freq": {"bnc": 1854, "coc": 1415},
      "definitions": [
        {"pos": "n.", "zh": "...", "en": "...", "source": "ecdict"},
        {"pos": "n.", "en": "...", "source": "wiktionary", "examples": ["..."]},
        {"pos": "n.", "en": "...", "source": "mw", "examples": ["..."]},
      ],
      "examples": [...],                        # 所有源的例句平铺（去重）
      "synonyms": [...], "antonyms": [...],
      "etymology": "...",
      "sources_hit": ["ecdict", "free_dict", "mw"],
      "from_cache": bool,
    }

CLI：
  python3 scripts/vocab/dict_sources.py construction
  python3 scripts/vocab/dict_sources.py constructed   # 自动 lemma 化
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
ECDICT_DB    = PROJECT_ROOT / "data" / "ecdict.db"
CFG_PATH     = PROJECT_ROOT / "state" / "server-config.json"

_CFG_CACHE: dict | None = None
def _cfg() -> dict:
    global _CFG_CACHE
    if _CFG_CACHE is None:
        try:
            _CFG_CACHE = json.loads(CFG_PATH.read_text("utf-8"))
        except Exception:
            _CFG_CACHE = {}
    return _CFG_CACHE.get("dict", {})

def _cache_dir() -> Path:
    d = PROJECT_ROOT / _cfg().get("cache_dir", "state/dict-cache")
    d.mkdir(parents=True, exist_ok=True)
    return d

def _cache_path(source: str, word: str) -> Path:
    sha = hashlib.sha1(f"{source}::{word.lower()}".encode("utf-8")).hexdigest()[:16]
    return _cache_dir() / f"{source}-{sha}.json"

def _cache_load(source: str, word: str, ttl_days: int = 30) -> dict | None:
    p = _cache_path(source, word)
    if not p.exists():
        return None
    try:
        st = p.stat()
        if (time.time() - st.st_mtime) > ttl_days * 86400:
            return None
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None

def _cache_save(source: str, word: str, data: dict):
    try:
        _cache_path(source, word).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass

# ── ECDICT ─────────────────────────────────────────────────────────────────

def _parse_exchange(raw: str) -> dict[str, str]:
    """ECDICT exchange 字段 → dict。
    样例：'d:bettered/i:bettering/s:betters/p:bettered/3:betters/0:good/1:r'
      d = past         (did)
      i = -ing         (doing)
      s = third sing   (does)
      p = past part    (done)
      3 = plural       (n.)
      0 = lemma (原型)
      1 = type indicator (单复数等)
      r = comparative  (better) ／ t = superlative (best)
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for seg in raw.split("/"):
        if ":" in seg:
            k, v = seg.split(":", 1)
            out[k.strip()] = v.strip()
    return out

def _ec_row(word: str) -> dict | None:
    if not ECDICT_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{ECDICT_DB}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            "SELECT word, phonetic, translation, definition, exchange, pos, bnc, frq, collins, oxford "
            "FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1", (word,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "word": row[0], "phonetic": row[1] or "",
            "translation": row[2] or "", "definition": row[3] or "",
            "exchange": row[4] or "", "pos": row[5] or "",
            "bnc": row[6] or 0, "frq": row[7] or 0,
            "collins": row[8] or 0, "oxford": row[9] or 0,
        }
    except Exception:
        return None

def lookup_ecdict(word: str) -> dict | None:
    """查 ECDICT，返回原始 + lemma 化后的派生词列表。"""
    row = _ec_row(word)
    if not row:
        # 屈折形态：从 exchange LIKE 反查原型
        try:
            conn = sqlite3.connect(f"file:{ECDICT_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT word FROM stardict WHERE exchange LIKE ? LIMIT 1",
                (f"%/{word.lower()}%",))
            r = cur.fetchone()
            conn.close()
            if r:
                row = _ec_row(r[0])
        except Exception:
            pass
    if not row:
        return None

    ex = _parse_exchange(row.get("exchange", ""))
    lemma_word = ex.get("0", row["word"])

    # lemma 化：如果当前词不是 lemma，递归再拿 lemma 的完整行
    if lemma_word.lower() != row["word"].lower():
        lemma_row = _ec_row(lemma_word)
        if lemma_row:
            row = lemma_row
            ex = _parse_exchange(row.get("exchange", ""))
            lemma_word = ex.get("0", row["word"])

    forms = {row["word"].lower()}
    for k in ("d", "i", "s", "p", "3", "r", "t"):
        if k in ex and ex[k] and not ex[k].startswith(("dp", "i", "p", "d", "r", "t")):
            forms.add(ex[k].lower())
        elif k in ex and len(ex[k]) > 2:   # 真实词不是 type indicator
            forms.add(ex[k].lower())
    # 兼容 type indicator 漏判：'d:bettered' v.s. '1:r'
    # 简单清洗：长度 ≥ 3 才算词形
    forms = {f for f in forms if len(f) >= 2}
    return {
        **row,
        "lemma": lemma_word,
        "forms": sorted(forms),
        "exchange_parsed": ex,
    }

def _ec_definitions(row: dict) -> list[dict]:
    """ECDICT translation + definition 拆条目。translation 是中文，definition 是英文。"""
    out: list[dict] = []
    # 中文：translation 多条用 \n 分；可能开头 'n. xxx, xxx'
    for line in (row.get("translation") or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(n\.|v\.|adj\.|adv\.|prep\.|conj\.|pron\.|art\.|num\.|interj\.|aux\.|abbr\.|[a-z]+\.) ?(.*)", line)
        if m:
            out.append({"pos": m.group(1), "zh": m.group(2).strip(), "source": "ecdict"})
        else:
            out.append({"pos": "", "zh": line, "source": "ecdict"})
    # 英文：definition 多条用 \n 分
    for line in (row.get("definition") or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(n\.|v\.|adj\.|adv\.|prep\.|conj\.|pron\.|art\.|num\.|interj\.|aux\.|abbr\.|[a-z]+\.) ?(.*)", line)
        if m:
            out.append({"pos": m.group(1), "en": m.group(2).strip(), "source": "ecdict_en"})
        else:
            out.append({"pos": "", "en": line, "source": "ecdict_en"})
    return out

# ── Free Dictionary API ────────────────────────────────────────────────────

def lookup_free_dict(word: str) -> dict | None:
    """https://api.dictionaryapi.dev/api/v2/entries/en/<word>
    免费、无 key、源 Wiktionary + others。返回结构含 phonetics(audio) + meanings + examples + synonyms / antonyms。
    """
    if not _cfg().get("free_dict_enabled", True):
        return None
    cached = _cache_load("freedict", word)
    if cached is not None:
        cached["_from_cache"] = True
        return cached
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bwicarus-vocab/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _cache_save("freedict", word, {"_404": True})
            return None
        return None
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    _cache_save("freedict", word, data[0])
    return data[0]

def _free_dict_unpack(raw: dict | None) -> dict:
    """归一化 Free Dictionary 结构。"""
    if not raw or raw.get("_404"):
        return {}
    phon_us = phon_uk = ""
    audio_us = audio_uk = ""
    for p in raw.get("phonetics", []) or []:
        text = (p.get("text") or "").strip()
        audio = (p.get("audio") or "").strip()
        if audio and "-us.mp3" in audio.lower() and not audio_us:
            audio_us = audio; phon_us = text or phon_us
        elif audio and "-uk.mp3" in audio.lower() and not audio_uk:
            audio_uk = audio; phon_uk = text or phon_uk
        elif audio and not audio_us:
            audio_us = audio; phon_us = text or phon_us
        elif text and not (phon_us or phon_uk):
            phon_us = text
    definitions = []
    examples = []
    synonyms, antonyms = set(), set()
    for m in raw.get("meanings", []) or []:
        pos = (m.get("partOfSpeech") or "").strip()
        for d in m.get("definitions", []) or []:
            text = (d.get("definition") or "").strip()
            if not text:
                continue
            ex = (d.get("example") or "").strip()
            definitions.append({
                "pos": pos, "en": text, "source": "wiktionary",
                **({"examples": [ex]} if ex else {}),
            })
            if ex:
                examples.append(ex)
        for s in m.get("synonyms", []) or []:
            if s: synonyms.add(s)
        for s in m.get("antonyms", []) or []:
            if s: antonyms.add(s)
    return {
        "phon_us": phon_us, "phon_uk": phon_uk,
        "audio_us": audio_us, "audio_uk": audio_uk,
        "definitions": definitions,
        "examples": examples,
        "synonyms": sorted(synonyms),
        "antonyms": sorted(antonyms),
        "etymology": (raw.get("origin") or "").strip(),
    }

# ── Merriam-Webster Learner's Dictionary API ───────────────────────────────

def lookup_mw_learner(word: str) -> list | None:
    """https://www.dictionaryapi.com/api/v3/references/learners/json/<word>?key=<KEY>
    Free tier 1000 req/day。需要 key 配在 server-config.dict.mw_key。
    返回 list，每个元素是一条词条（同形异义会多条）。
    """
    key = _cfg().get("mw_key", "").strip()
    if not key:
        return None
    cached = _cache_load("mw", word)
    if cached is not None:
        cached_list = cached.get("data") if isinstance(cached, dict) else cached
        if isinstance(cached_list, list):
            return cached_list
        if isinstance(cached, dict) and cached.get("_404"):
            return None
    url = (f"https://www.dictionaryapi.com/api/v3/references/learners/json/"
           f"{urllib.parse.quote(word)}?key={urllib.parse.quote(key)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bwicarus-vocab/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    if not data or isinstance(data[0], str):
        # MW 返回 list[str] 表示"没命中，但拼写建议"
        _cache_save("mw", word, {"_404": True, "suggestions": data if data else []})
        return None
    _cache_save("mw", word, {"data": data})
    return data

def _mw_strip(s: str) -> str:
    """剥 MW 富文本标记：{bc}{it}{wi}... → 纯文本"""
    if not s:
        return ""
    # 去 {xxx} 标记，保留内容
    s = re.sub(r"\{bc\}", ": ", s)
    s = re.sub(r"\{phrase\}([^{}]+)\{/phrase\}", r"\1", s)
    s = re.sub(r"\{it\}([^{}]+)\{/it\}", r"\1", s)
    s = re.sub(r"\{wi\}([^{}]+)\{/wi\}", r"\1", s)
    s = re.sub(r"\{dx[^{}]*\}[^{}]*\{/dx[^{}]*\}", "", s)
    s = re.sub(r"\{[^{}]+\}", "", s)
    return s.strip()

def _mw_unpack(raw: list | None, lemma: str = "") -> dict:
    if not raw:
        return {}
    out_defs: list[dict] = []
    examples: list[str] = []
    phon_us = ""
    audio_us_path = ""
    pos_set: set[str] = set()
    lemma_key = (lemma or "").lower()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # 过滤：MW 偶尔返回完全不相关的 related entry（如查 construction 返回 under）
        # 头词去 * / 空格后必须以 lemma 开头才采用（保留 'construction paper' 这种合理派生）
        hw = (entry.get("hwi", {}).get("hw") or "").replace("*", "").replace(" ", "").lower()
        if lemma_key and hw and not hw.startswith(lemma_key):
            continue
        # 词性
        fl = entry.get("fl") or ""
        if fl:
            short = {"verb":"v.","noun":"n.","adjective":"adj.","adverb":"adv.",
                     "preposition":"prep.","conjunction":"conj.","pronoun":"pron.",
                     "interjection":"interj."}.get(fl, fl)
            pos_set.add(short)
        # 音标 + 音频
        for hwi in [entry.get("hwi", {})]:
            for pr in (hwi.get("prs") or []):
                if not phon_us and pr.get("mw"):
                    phon_us = f"/{pr['mw']}/"
                sound = pr.get("sound", {})
                if not audio_us_path and sound.get("audio"):
                    audio_us_path = sound["audio"]
        # 定义
        for d in (entry.get("def") or []):
            for sseq in (d.get("sseq") or []):
                for unit in sseq:
                    if not isinstance(unit, list) or len(unit) < 2:
                        continue
                    typ, payload = unit[0], unit[1]
                    if typ != "sense" or not isinstance(payload, dict):
                        continue
                    dt = payload.get("dt") or []
                    text_parts, exs = [], []
                    for item in dt:
                        if not isinstance(item, list) or not item:
                            continue
                        if item[0] == "text":
                            text_parts.append(_mw_strip(item[1]))
                        elif item[0] == "vis":
                            for v in (item[1] or []):
                                if isinstance(v, dict) and v.get("t"):
                                    exs.append(_mw_strip(v["t"]))
                    text = " ".join(t for t in text_parts if t).strip(" :,")
                    if text:
                        out_defs.append({
                            "pos": short if fl else "",
                            "en": text, "source": "mw",
                            **({"examples": exs} if exs else {}),
                        })
                    examples.extend(exs)
    return {
        "phon_us": phon_us,
        "audio_path": audio_us_path,    # MW 音频路径，需补全 URL
        "definitions": out_defs,
        "examples": examples,
        "pos": sorted(pos_set),
    }

def _mw_audio_url(audio_path: str) -> str:
    """MW 音频路径 → 完整 URL。
    subdir 规则：以 bix 开头 → bix；以 gg 开头 → gg；数字开头 → number；带特殊符号 → ?；否则首字母。"""
    if not audio_path:
        return ""
    p = audio_path
    if p.startswith("bix"): sub = "bix"
    elif p.startswith("gg"): sub = "gg"
    elif p[:1].isdigit() or p[:1] in {"_", "-"}: sub = "number"
    else: sub = p[0]
    return f"https://media.merriam-webster.com/audio/prons/en/us/mp3/{sub}/{p}.mp3"

# ── 三源融合 ────────────────────────────────────────────────────────────────

def compose_entry(word: str, *, online: bool = True) -> dict:
    word = (word or "").strip().lower()
    if not word:
        return {}
    sources_hit: list[str] = []
    ec = lookup_ecdict(word)
    if ec:
        sources_hit.append("ecdict")
    lemma = ec["lemma"] if ec else word
    forms = ec["forms"] if ec else [word]

    fd_raw = lookup_free_dict(lemma) if online else None
    fd = _free_dict_unpack(fd_raw)
    if fd_raw and not fd_raw.get("_404"):
        sources_hit.append("free_dict")

    mw_raw = lookup_mw_learner(lemma) if online else None
    mw = _mw_unpack(mw_raw, lemma=lemma)
    if mw_raw:
        sources_hit.append("mw")

    # 音标 + 音频优先级：MW > FreeDict > ECDICT
    phon_us = mw.get("phon_us") or fd.get("phon_us") or (("/" + ec["phonetic"] + "/") if ec and ec.get("phonetic") else "")
    phon_uk = fd.get("phon_uk") or ""
    audio_us = (_mw_audio_url(mw.get("audio_path", "")) or fd.get("audio_us") or "")
    audio_uk = fd.get("audio_uk") or ""

    # POS 合并
    pos_set: set[str] = set(mw.get("pos") or [])
    if ec and ec.get("pos"):
        # ec pos 是 "n:100" 这种比例字串
        for p in re.findall(r"([a-z]+):\d+", ec["pos"]):
            pos_set.add(p + ".")
    pos_list = sorted(pos_set)

    # 定义合并：MW 中文先 ECDICT-zh，再 MW-en，再 wiktionary
    defs_ec = _ec_definitions(ec) if ec else []
    definitions = defs_ec + (mw.get("definitions") or []) + (fd.get("definitions") or [])

    # 例句去重
    seen = set()
    examples: list[str] = []
    for ex in (mw.get("examples") or []) + (fd.get("examples") or []):
        k = re.sub(r"\W+", "", ex.lower())[:60]
        if k and k not in seen:
            seen.add(k); examples.append(ex)
    examples = examples[:20]

    # 例句中文翻译（前 N 条，缓存避免重复 API；离线模式跳过）
    examples_zh: dict[str, str] = {}
    if online:
        try:
            from translate import translate as _tr   # 同包
            for ex in examples[:8]:
                t = _tr(ex)
                if t: examples_zh[ex] = t
        except Exception:
            pass

    return {
        "word": word, "lemma": lemma, "forms": forms,
        "phonetics": {"us": phon_us, "uk": phon_uk},
        "audio": {"us": audio_us, "uk": audio_uk},
        "pos": pos_list,
        "freq": {"bnc": (ec or {}).get("bnc", 0), "coc": (ec or {}).get("frq", 0)},
        "definitions": definitions,
        "examples": examples,
        "examples_zh": examples_zh,
        "synonyms": fd.get("synonyms") or [],
        "antonyms": fd.get("antonyms") or [],
        "etymology": fd.get("etymology") or "",
        "sources_hit": sources_hit,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("word")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    entry = compose_entry(args.word, online=not args.offline)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
