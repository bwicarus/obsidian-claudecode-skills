#!/usr/bin/env python3
"""Build the pinned, App-downloadable Japanese dictionary.

JMdict remains authoritative for spelling, readings, POS and inflection.
Digest-pinned Chinese Wiktionary, Tanaka examples, UniDic accent data and
KANJIDIC supply the rich local entry.  None of these data files is bundled in
the IPA, books, Pi sync or browser extensions.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "DictionaryData"
DEFAULT_CACHE = Path(
    os.environ.get(
        "BW_JMDICT_SOURCE_CACHE",
        Path.home() / ".cache" / "bwreader-jmdict",
    )
)
LICENSE_SOURCE = HERE / "JMdict-LICENSE.txt"
CHINESE_LICENSE_SOURCE = HERE / "ZhWiktionary-LICENSE.txt"
TANAKA_LICENSE_SOURCE = HERE / "Tanaka-LICENSE.txt"

SOURCE_NAME = "JMdict English via jmdict-simplified"
SOURCE_RELEASE = "3.6.2+20260810124713"
SOURCE_DICTIONARY_VERSION = "3.6.2"
SOURCE_DICTIONARY_DATE = "2026-08-10"
SOURCE_ARCHIVE_NAME = f"jmdict-eng-{SOURCE_RELEASE}.json.tgz"
SOURCE_MEMBER_NAME = f"jmdict-eng-{SOURCE_DICTIONARY_VERSION}.json"
SOURCE_URL = (
    "https://github.com/scriptin/jmdict-simplified/releases/download/"
    "3.6.2%2B20260810124713/"
    + SOURCE_ARCHIVE_NAME
)
SOURCE_SHA256 = "e6802135b445627a8f09c544bf8c32c3d344515f6e95a473e8bd39e09ad00109"

CHINESE_SOURCE_NAME = "Chinese Wiktionary Japanese via Kaikki"
CHINESE_SOURCE_RELEASE = "zhwiktionary-ja-605256f3b7fc"
CHINESE_SOURCE_NAME_ON_DISK = "kaikki-zhwiktionary-ja.jsonl"
CHINESE_SOURCE_URL = (
    "https://kaikki.org/zhwiktionary/%E6%97%A5%E8%AF%AD/"
    "kaikki.org-dictionary-%E6%97%A5%E8%AF%AD.jsonl"
)
CHINESE_SOURCE_SHA256 = (
    "605256f3b7fc73337b9b9d47612ab27477cff92c230dfc2c900545d52de1c63c"
)

RICH_CHINESE_SOURCE_NAME = "wty Japanese-Chinese Wiktionary for Yomitan"
RICH_CHINESE_SOURCE_RELEASE = "2026.07.15"
RICH_CHINESE_ARCHIVE_NAME = "wty-ja-zh.zip"
RICH_CHINESE_SOURCE_URL = (
    "https://huggingface.co/datasets/daxida/wty-release/resolve/main/"
    "dict/ja/zh/wty-ja-zh.zip?download=true"
)
RICH_CHINESE_SOURCE_SHA256 = (
    "3ef3022e6b9310c1bc8c82af5a27d273e73b60ae5a0e500d7bc424535ede8938"
)
TANAKA_SOURCE_NAME = "Tanaka Corpus examples.utf"
TANAKA_SOURCE_RELEASE = "2026-08-13-pinned"
TANAKA_SOURCE_NAME_ON_DISK = "examples.utf.gz"
TANAKA_SOURCE_URL = "http://ftp.edrdg.org/pub/Nihongo/examples.utf.gz"
TANAKA_SOURCE_SHA256 = (
    "a5d50104737e9ab1ff40324c6d6f0bc9be32541942fd6119bea001c1b47570aa"
)
KANJIDIC_SOURCE_NAME = "KANJIDIC2"
KANJIDIC_SOURCE_RELEASE = "2026-08-13-pinned"
KANJIDIC_SOURCE_NAME_ON_DISK = "kanjidic2.xml.gz"
KANJIDIC_SOURCE_URL = "http://www.edrdg.org/kanjidic/kanjidic2.xml.gz"
KANJIDIC_SOURCE_SHA256 = (
    "05c10cb87dc109e087f6e99c95a8fb8dd02705cbd0e86130ba0e80bf8db7fa26"
)

MANIFEST_CONTRACT = "bw-jmdict-manifest/3"
SHARD_CONTRACT = "bw-jmdict-shard/3"
SHARD_ALGORITHM = "utf8-prefix-2-kana-3/1"
KANA_SPLIT_PREFIXES = {"e381", "e382", "e383"}
MANIFEST_NAME = "manifest.json"
LICENSE_NAME = "LICENSE-JMdict.txt"
CHINESE_LICENSE_NAME = "LICENSE-ZhWiktionary.txt"
TANAKA_LICENSE_NAME = "LICENSE-Tanaka.txt"
KANJI_RESOURCE_NAME = "kanji.json"
SHARD_DIRECTORY = "shards"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_term(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("JMdict term must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized:
        raise ValueError("JMdict term must not be empty")
    return normalized


def shard_key(term: str) -> str:
    """Return a bounded key made only from the NFC term's first UTF-8 bytes.

    Two bytes keep the general shard count bounded.  Hiragana and katakana all
    share just three two-byte prefixes, so those hot prefixes use the complete
    three-byte first kana scalar to avoid multi-megabyte lookup shards.
    """
    encoded = normalize_term(term).encode("utf-8")
    prefix = encoded[:2].hex()
    return encoded[:3].hex() if prefix in KANA_SPLIT_PREFIXES else prefix


def _unique_strings(values: object, *, label: str) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{label} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} contains an invalid string")
        normalized = unicodedata.normalize("NFC", value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _form_values(values: object, *, label: str) -> tuple[list[str], bool]:
    if not isinstance(values, list):
        raise ValueError(f"{label} must be an array")
    texts: list[str] = []
    seen: set[str] = set()
    common = False
    for item in values:
        if not isinstance(item, dict):
            raise ValueError(f"{label} contains a non-object")
        text = item.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError(f"{label} contains an invalid text")
        normalized = unicodedata.normalize("NFC", text)
        if normalized not in seen:
            seen.add(normalized)
            texts.append(normalized)
        if item.get("common") is True:
            common = True
    return texts, common


def compact_entry(word: object) -> dict[str, object]:
    if not isinstance(word, dict):
        raise ValueError("JMdict word must be an object")
    entry_id = word.get("id")
    if not isinstance(entry_id, str) or not entry_id.isdecimal():
        raise ValueError("JMdict word id must be a decimal string")

    forms, common_form = _form_values(word.get("kanji"), label="word.kanji")
    readings, common_reading = _form_values(word.get("kana"), label="word.kana")
    if not forms and not readings:
        raise ValueError(f"JMdict word {entry_id} has no forms or readings")

    senses = word.get("sense")
    if not isinstance(senses, list) or not senses:
        raise ValueError(f"JMdict word {entry_id} has no senses")
    pos: list[str] = []
    pos_seen: set[str] = set()
    glosses: list[str] = []
    gloss_seen: set[str] = set()
    for sense in senses:
        if not isinstance(sense, dict):
            raise ValueError(f"JMdict word {entry_id} has an invalid sense")
        for code in _unique_strings(
            sense.get("partOfSpeech"), label=f"word {entry_id} partOfSpeech"
        ):
            if code not in pos_seen:
                pos_seen.add(code)
                pos.append(code)
        gloss_items = sense.get("gloss")
        if not isinstance(gloss_items, list):
            raise ValueError(f"JMdict word {entry_id} gloss must be an array")
        for gloss in gloss_items:
            if not isinstance(gloss, dict):
                raise ValueError(f"JMdict word {entry_id} has an invalid gloss")
            text = gloss.get("text")
            language = gloss.get("lang")
            if language != "eng" or not isinstance(text, str) or not text:
                raise ValueError(f"JMdict word {entry_id} has a non-English/invalid gloss")
            if text not in gloss_seen:
                gloss_seen.add(text)
                glosses.append(text)
    if not glosses:
        raise ValueError(f"JMdict word {entry_id} has no English glosses")

    return {
        "id": entry_id,
        "lemma": forms[0] if forms else readings[0],
        "forms": forms,
        "readings": readings,
        "pos": pos,
        "glosses": glosses,
        "common": common_form or common_reading,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validate_source(payload: object) -> tuple[list[object], dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("JMdict source root must be an object")
    if payload.get("version") != SOURCE_DICTIONARY_VERSION:
        raise ValueError("JMdict source version differs from the pinned release")
    if payload.get("dictDate") != SOURCE_DICTIONARY_DATE:
        raise ValueError("JMdict source date differs from the pinned release")
    if payload.get("languages") != ["eng"] or payload.get("commonOnly") is not False:
        raise ValueError("JMdict source must contain the complete English dictionary")
    words = payload.get("words")
    tags = payload.get("tags")
    if not isinstance(words, list) or not words:
        raise ValueError("JMdict source words must be a non-empty array")
    if not isinstance(tags, dict) or not tags:
        raise ValueError("JMdict source tags must be a non-empty object")
    clean_tags: dict[str, str] = {}
    for code, label in tags.items():
        if not isinstance(code, str) or not code or not isinstance(label, str) or not label:
            raise ValueError("JMdict source tags contain an invalid item")
        clean_tags[code] = label
    return words, clean_tags


def load_chinese_records(path: Path):
    """Yield the digest-checked Kaikki JSONL records without loading all at once."""
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                yield json.loads(line)
            except ValueError as error:
                raise ValueError(
                    f"Chinese Wiktionary JSONL line {line_number} is invalid"
                ) from error


def _contains_han(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _compact_chinese_record(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict) or raw.get("lang_code") != "ja":
        return None
    word = raw.get("word")
    senses = raw.get("senses")
    if not isinstance(word, str) or not word or not isinstance(senses, list):
        return None
    word = normalize_term(word)
    readings: set[str] = set()
    forms = raw.get("forms")
    if isinstance(forms, list):
        for form in forms:
            if not isinstance(form, dict) or not isinstance(form.get("ruby"), list):
                continue
            ruby = form["ruby"]
            reading = "".join(
                item[1]
                for item in ruby
                if isinstance(item, list)
                and len(item) >= 2
                and isinstance(item[1], str)
            )
            if reading:
                readings.add(normalize_term(reading))
    sounds = raw.get("sounds")
    if isinstance(sounds, list):
        for sound in sounds:
            if isinstance(sound, dict) and isinstance(sound.get("other"), str):
                reading = sound["other"].strip()
                if reading:
                    readings.add(normalize_term(reading))

    glosses: list[str] = []
    seen: set[str] = set()
    for sense in senses:
        if not isinstance(sense, dict) or not isinstance(sense.get("glosses"), list):
            continue
        for raw_gloss in sense["glosses"]:
            if not isinstance(raw_gloss, str):
                continue
            text = unicodedata.normalize("NFC", raw_gloss).strip()
            if text.startswith(word + "【"):
                close = text.find("】", len(word) + 1)
                if close >= 0:
                    header_reading = text[len(word) + 1 : close].strip()
                    if header_reading:
                        readings.add(normalize_term(header_reading))
                    text = text[close + 1 :].lstrip(" \t\r\n：:")
            elif text.startswith(word + "\n"):
                text = text[len(word) :].lstrip()
            text = re.sub(r"\s+", " ", text).strip()
            if (
                not text
                or text == word
                or not _contains_han(text)
                or text in seen
            ):
                continue
            seen.add(text)
            glosses.append(text[:600])
            if len(glosses) >= 6:
                break
        if len(glosses) >= 6:
            break
    if not glosses:
        return None
    return {
        "word": word,
        "readings": sorted(readings),
        "glosses": glosses,
    }


def attach_chinese_glosses(
    entries_by_id: dict[str, dict[str, object]],
    exact_ids: dict[str, dict[str, set[str]]],
    records,
) -> dict[str, int]:
    source_records = 0
    accepted_records = 0
    ambiguous_skipped = 0
    matched_entry_ids: set[str] = set()
    gloss_count = 0
    for raw in records:
        source_records += 1
        record = _compact_chinese_record(raw)
        if record is None:
            continue
        accepted_records += 1
        word = str(record["word"])
        candidates = sorted(
            exact_ids.get(shard_key(word), {}).get(word, set()),
            key=int,
        )
        if not candidates:
            continue
        readings = set(str(value) for value in record["readings"])
        if readings:
            matching = [
                entry_id
                for entry_id in candidates
                if readings.intersection(
                    str(value) for value in entries_by_id[entry_id]["readings"]
                )
            ]
            if not matching:
                ambiguous_skipped += 1
                continue
            candidates = matching
        elif len(candidates) != 1:
            ambiguous_skipped += 1
            continue
        for entry_id in candidates:
            entry = entries_by_id[entry_id]
            target = entry.setdefault("zhGlosses", [])
            if not isinstance(target, list):
                raise ValueError("JMdict Chinese gloss target is invalid")
            for gloss in record["glosses"]:
                if gloss not in target and len(target) < 8:
                    target.append(gloss)
                    gloss_count += 1
            if target:
                matched_entry_ids.add(entry_id)
    return {
        "sourceRecords": source_records,
        "acceptedRecords": accepted_records,
        "matchedEntries": len(matched_entry_ids),
        "glosses": gloss_count,
        "ambiguousSkipped": ambiguous_skipped,
    }


def _content_role(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    data = node.get("data")
    return str(data.get("content", "")) if isinstance(data, dict) else ""


def _plain_content(node: object, *, skip_details: bool = True) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(
            _plain_content(item, skip_details=skip_details) for item in node
        )
    if not isinstance(node, dict):
        return ""
    role = _content_role(node)
    if (
        (skip_details and node.get("tag") == "details")
        or role in {
            "backlink", "synonyms", "example-sentence", "preamble",
            "extra-info", "summary-entry",
        }
    ):
        return ""
    return _plain_content(node.get("content"), skip_details=skip_details)


def _walk_content(node: object):
    if isinstance(node, list):
        for item in node:
            yield from _walk_content(item)
    elif isinstance(node, dict):
        yield node
        yield from _walk_content(node.get("content"))


def _clean_text(value: object, maximum: int = 800) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()[:maximum]


def _wty_examples(node: object) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in _walk_content(node):
        if _content_role(item) != "example-sentence":
            continue
        fields: dict[str, str] = {}
        for part in _walk_content(item.get("content")):
            role = _content_role(part)
            if role == "example-sentence-a":
                fields["ja"] = _clean_text(_plain_content(part, skip_details=False), 1000)
            elif role == "example-sentence-b":
                fields["zh"] = _clean_text(_plain_content(part, skip_details=False), 1000)
            elif role == "example-sentence-c":
                fields["ref"] = _clean_text(_plain_content(part, skip_details=False), 300)
        key = (fields.get("ja", ""), fields.get("zh", ""))
        if key[0] and key not in seen:
            seen.add(key)
            fields["source"] = "wty"
            result.append(fields)
    return result


def _compact_wty_row(row: object) -> dict[str, object] | None:
    if not isinstance(row, list) or len(row) < 6:
        return None
    word = _clean_text(row[0], 256)
    reading = _clean_text(row[1], 256)
    raw_pos = _clean_text(row[2], 120)
    definitions = row[5]
    if not word or not isinstance(definitions, list):
        return None
    senses: list[dict[str, object]] = []
    examples: list[dict[str, str]] = []
    etymology: list[str] = []
    synonyms: list[str] = []
    source_urls: list[str] = []
    for definition in definitions:
        root = definition.get("content") if isinstance(definition, dict) else None
        if root is None:
            continue
        for item in _walk_content(root):
            role = _content_role(item)
            if role == "Etymology-content":
                value = _clean_text(_plain_content(item, skip_details=False), 1000)
                if value and value not in etymology:
                    etymology.append(value)
            elif role == "synonym-item":
                value = _clean_text(_plain_content(item, skip_details=False), 128)
                if value and value not in synonyms:
                    synonyms.append(value)
            if item.get("tag") == "a":
                href = str(item.get("href") or "")
                if href.startswith("https://") and href not in source_urls:
                    source_urls.append(href[:1000])
            if item.get("tag") != "ol" or role != "glosses":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for li in content:
                if not isinstance(li, dict) or li.get("tag") != "li":
                    continue
                gloss = _clean_text(_plain_content(li), 600)
                if not gloss:
                    continue
                local_examples = _wty_examples(li)
                senses.append({
                    "pos": raw_pos,
                    "glosses": [gloss],
                    "examples": local_examples[:5],
                })
                examples.extend(local_examples)
    if not senses:
        plain = _clean_text(_plain_content(definitions), 600)
        if plain:
            senses.append({"pos": raw_pos, "glosses": [plain], "examples": []})
    if not senses:
        return None
    unique_examples: list[dict[str, str]] = []
    seen_examples: set[tuple[str, str]] = set()
    for example in examples:
        key = (example.get("ja", ""), example.get("zh", ""))
        if key not in seen_examples:
            seen_examples.add(key)
            unique_examples.append(example)
    return {
        "word": normalize_term(word),
        "reading": normalize_term(reading) if reading else "",
        "senses": senses[:12],
        "examples": unique_examples[:5],
        "etymology": etymology[:3],
        "synonyms": synonyms[:12],
        "sourceUrls": source_urls[:4],
    }


def load_wty_records(path: Path):
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name for name in archive.namelist()
            if re.fullmatch(r"term_bank_\d+\.json", name)
        )
        if not names:
            raise ValueError("wty archive contains no term banks")
        for name in names:
            rows = json.loads(archive.read(name))
            if not isinstance(rows, list):
                raise ValueError(f"wty term bank is invalid: {name}")
            for row in rows:
                yield row


def attach_rich_chinese(
    entries_by_id: dict[str, dict[str, object]],
    exact_ids: dict[str, dict[str, set[str]]],
    rows,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    counts = {"sourceRecords": 0, "acceptedRecords": 0, "matchedEntries": 0,
              "senses": 0, "examples": 0}
    matched: set[str] = set()
    character_meanings: dict[str, list[str]] = {}
    for row in rows:
        counts["sourceRecords"] += 1
        record = _compact_wty_row(row)
        if record is None:
            continue
        counts["acceptedRecords"] += 1
        word = str(record["word"])
        if len(word) == 1 and _contains_han(word):
            char_glosses = character_meanings.setdefault(word, [])
            for sense in record["senses"]:
                for gloss in sense["glosses"]:
                    if gloss not in char_glosses and len(char_glosses) < 6:
                        char_glosses.append(str(gloss))
        candidates = sorted(
            exact_ids.get(shard_key(word), {}).get(word, set()), key=int
        )
        if not candidates:
            continue
        reading = str(record.get("reading") or "")
        if reading:
            by_reading = [
                entry_id for entry_id in candidates
                if reading in entries_by_id[entry_id]["readings"]
            ]
            if by_reading:
                candidates = by_reading
        elif len(candidates) > 1:
            continue
        for entry_id in candidates:
            entry = entries_by_id[entry_id]
            rich_senses = entry.setdefault("zhSenses", [])
            for sense in record["senses"]:
                signature = canonical_sense_signature(sense)
                if signature not in {
                    canonical_sense_signature(existing) for existing in rich_senses
                } and len(rich_senses) < 16:
                    rich_senses.append(sense)
                    counts["senses"] += 1
                target = entry.setdefault("zhGlosses", [])
                for gloss in sense["glosses"]:
                    if gloss not in target and len(target) < 12:
                        target.append(gloss)
            target_examples = entry.setdefault("examples", [])
            for example in record["examples"]:
                if not any(old.get("ja") == example.get("ja") for old in target_examples):
                    if len(target_examples) < 5:
                        target_examples.append(example)
                        counts["examples"] += 1
            for field, maximum in (("etymology", 3), ("synonyms", 12), ("sourceUrls", 4)):
                target = entry.setdefault(field, [])
                for value in record[field]:
                    if value not in target and len(target) < maximum:
                        target.append(value)
            matched.add(entry_id)
    counts["matchedEntries"] = len(matched)
    return counts, character_meanings


def canonical_sense_signature(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return json.dumps(
        {"pos": value.get("pos", ""), "glosses": value.get("glosses", [])},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


_TANAKA_HEADWORD = re.compile(r"^([^()\[\]{}~#]+)")


def attach_tanaka_examples(
    entries_by_id: dict[str, dict[str, object]],
    exact_ids: dict[str, dict[str, set[str]]],
    source: Path,
) -> dict[str, int]:
    counts = {"sentences": 0, "mappings": 0, "attached": 0}
    pending: tuple[str, str, str] | None = None
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("A: "):
                body = line[3:].rstrip("\n")
                ja, rest = body.split("\t", 1) if "\t" in body else (body, "")
                match = re.search(r"#ID=([^\s]+)", rest)
                en = rest.split("#ID=", 1)[0].strip()
                pending = (_clean_text(ja, 1000), _clean_text(en, 1000),
                           _clean_text(match.group(1), 120) if match else "")
            elif line.startswith("B: ") and pending:
                ja, en, sentence_id = pending
                counts["sentences"] += 1
                headwords: set[str] = set()
                for token in line[3:].split():
                    match = _TANAKA_HEADWORD.match(token)
                    headword = normalize_term(match.group(1).strip()) if match else ""
                    if headword and len(headword) <= 32:
                        headwords.add(headword)
                for headword in headwords:
                    candidates = exact_ids.get(shard_key(headword), {}).get(headword, set())
                    if not candidates:
                        continue
                    counts["mappings"] += 1
                    for entry_id in candidates:
                        target = entries_by_id[entry_id].setdefault("examples", [])
                        if len(target) >= 5 or any(item.get("ja") == ja for item in target):
                            continue
                        target.append({
                            "ja": ja, "en": en, "source": "tanaka", "ref": sentence_id,
                        })
                        counts["attached"] += 1
                pending = None
    return counts


def attach_unidic_pitch(entries_by_id: dict[str, dict[str, object]]) -> dict[str, int]:
    try:
        import fugashi
        import importlib.metadata
    except ImportError as error:
        raise ValueError("fugashi + unidic-lite are required for rich dictionary build") from error
    if importlib.metadata.version("fugashi") != "1.5.2" or importlib.metadata.version("unidic-lite") != "1.0.8":
        raise ValueError("rich dictionary requires fugashi 1.5.2 and unidic-lite 1.0.8")
    tagger = fugashi.Tagger()
    matched = 0
    for entry in entries_by_id.values():
        lemma = str(entry["lemma"])
        tokens = list(tagger(lemma))
        token = tokens[0] if len(tokens) == 1 else next(
            (item for item in tokens if str(item.feature.lemma) == lemma), None
        )
        if token is None:
            continue
        feature = token.feature
        reading_kata = str(getattr(feature, "kanaBase", "") or "")
        accent_raw = str(getattr(feature, "aType", "") or "")
        accent_match = re.match(r"^\d+", accent_raw)
        if reading_kata:
            entry["readingKata"] = reading_kata
        if accent_match:
            entry["accent"] = int(accent_match.group(0))
        if reading_kata or accent_match:
            matched += 1
    return {"matchedEntries": matched}


def build_kanjidic_resource(source: Path, character_meanings: dict[str, list[str]]) -> dict[str, object]:
    result: dict[str, object] = {}
    with gzip.open(source, "rb") as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag != "character":
                continue
            literal = element.findtext("literal") or ""
            on: list[str] = []
            kun: list[str] = []
            meanings: list[str] = []
            group_root = element.find("reading_meaning")
            if group_root is not None:
                for group in group_root.findall("rmgroup"):
                    for reading in group.findall("reading"):
                        if reading.text and reading.get("r_type") == "ja_on":
                            on.append(reading.text)
                        elif reading.text and reading.get("r_type") == "ja_kun":
                            kun.append(reading.text)
                    for meaning in group.findall("meaning"):
                        if meaning.text and meaning.get("m_lang") is None:
                            meanings.append(meaning.text)
            if literal and (on or kun or meanings):
                item: dict[str, object] = {
                    "kanji": literal, "on": on, "kun": kun,
                    "meanings": meanings[:6],
                }
                zh = character_meanings.get(literal, [])
                if zh:
                    item["meanings_zh"] = "；".join(zh[:4])
                result[literal] = item
            element.clear()
    if not result:
        raise ValueError("KANJIDIC source produced no characters")
    return result


def build_from_payload(
    payload: object,
    output: Path,
    *,
    rich_chinese_rows,
    tanaka_source: Path,
    kanjidic_source: Path,
) -> dict[str, object]:
    words, tags = _validate_source(payload)
    entries_by_id: dict[str, dict[str, object]] = {}
    exact_ids: dict[str, dict[str, set[str]]] = {}
    used_pos: set[str] = set()

    for raw_word in words:
        entry = compact_entry(raw_word)
        entry_id = str(entry["id"])
        if entry_id in entries_by_id:
            raise ValueError(f"duplicate JMdict word id: {entry_id}")
        entries_by_id[entry_id] = entry
        used_pos.update(str(code) for code in entry["pos"])
        terms = [*entry["forms"], *entry["readings"]]
        for term in terms:
            normalized = normalize_term(str(term))
            exact_ids.setdefault(shard_key(normalized), {}).setdefault(
                normalized, set()
            ).add(entry_id)

    missing_tags = sorted(used_pos - set(tags))
    if missing_tags:
        raise ValueError(f"JMdict POS tags have no labels: {missing_tags!r}")

    rich_chinese_counts, character_meanings = attach_rich_chinese(
        entries_by_id,
        exact_ids,
        rich_chinese_rows,
    )
    if rich_chinese_counts["matchedEntries"] <= 0:
        raise ValueError("rich Chinese Wiktionary source matched no JMdict entries")
    tanaka_counts = attach_tanaka_examples(
        entries_by_id, exact_ids, tanaka_source
    )
    unidic_counts = attach_unidic_pitch(entries_by_id)
    kanjidic = build_kanjidic_resource(kanjidic_source, character_meanings)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".DictionaryData-", dir=output.parent))
    try:
        license_payload = LICENSE_SOURCE.read_bytes()
        (staging / LICENSE_NAME).write_bytes(license_payload)
        chinese_license_payload = CHINESE_LICENSE_SOURCE.read_bytes()
        (staging / CHINESE_LICENSE_NAME).write_bytes(chinese_license_payload)
        tanaka_license_payload = TANAKA_LICENSE_SOURCE.read_bytes()
        (staging / TANAKA_LICENSE_NAME).write_bytes(tanaka_license_payload)
        kanji_path = staging / KANJI_RESOURCE_NAME
        _write_json(kanji_path, kanjidic)
        shard_manifest: dict[str, dict[str, object]] = {}
        total_terms = 0
        total_entry_copies = 0
        for key in sorted(exact_ids):
            term_map = exact_ids[key]
            shard_entry_ids = sorted(
                {entry_id for ids in term_map.values() for entry_id in ids},
                key=lambda value: int(value),
            )
            index_by_id = {
                entry_id: index for index, entry_id in enumerate(shard_entry_ids)
            }
            entries = [entries_by_id[entry_id] for entry_id in shard_entry_ids]
            exact = {
                term: sorted(
                    (index_by_id[entry_id] for entry_id in ids),
                    key=lambda index: (
                        not bool(entries[index]["common"]),
                        int(str(entries[index]["id"])),
                    ),
                )
                for term, ids in sorted(term_map.items())
            }
            relative = f"{SHARD_DIRECTORY}/{key}.json"
            shard = {
                "contract": SHARD_CONTRACT,
                "key": key,
                "entries": entries,
                "exact": exact,
            }
            shard_path = staging / PurePosixPath(relative)
            _write_json(shard_path, shard)
            byte_count = shard_path.stat().st_size
            shard_manifest[key] = {
                "path": relative,
                "sha256": sha256_file(shard_path),
                "bytes": byte_count,
                "terms": len(exact),
                "entries": len(entries),
            }
            total_terms += len(exact)
            total_entry_copies += len(entries)

        manifest: dict[str, object] = {
            "contract": MANIFEST_CONTRACT,
            "normalization": "NFC",
            "shardAlgorithm": SHARD_ALGORITHM,
            "source": {
                "name": SOURCE_NAME,
                "release": SOURCE_RELEASE,
                "dictionaryVersion": SOURCE_DICTIONARY_VERSION,
                "dictionaryDate": SOURCE_DICTIONARY_DATE,
                "url": SOURCE_URL,
                "sha256": SOURCE_SHA256,
            },
            "license": {
                "name": "Creative Commons Attribution-ShareAlike 4.0 International",
                "path": LICENSE_NAME,
                "sha256": sha256_bytes(license_payload),
                "bytes": len(license_payload),
                "attribution": "JMdict by the Electronic Dictionary Research and Development Group",
                "projectUrl": "https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project",
                "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "chineseSource": {
                "name": RICH_CHINESE_SOURCE_NAME,
                "release": RICH_CHINESE_SOURCE_RELEASE,
                "url": RICH_CHINESE_SOURCE_URL,
                "sha256": RICH_CHINESE_SOURCE_SHA256,
            },
            "chineseLicense": {
                "name": "CC BY-SA 4.0 and GNU Free Documentation License",
                "path": CHINESE_LICENSE_NAME,
                "sha256": sha256_bytes(chinese_license_payload),
                "bytes": len(chinese_license_payload),
                "attribution": "Chinese Wiktionary contributors; extracted by Kaikki",
                "projectUrl": "https://kaikki.org/zhwiktionary/",
                "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "tanakaSource": {
                "name": TANAKA_SOURCE_NAME,
                "release": TANAKA_SOURCE_RELEASE,
                "url": TANAKA_SOURCE_URL,
                "sha256": TANAKA_SOURCE_SHA256,
            },
            "tanakaLicense": {
                "name": "CC BY 2.0 France",
                "path": TANAKA_LICENSE_NAME,
                "sha256": sha256_bytes(tanaka_license_payload),
                "bytes": len(tanaka_license_payload),
                "attribution": "Tanaka Corpus contributors",
                "projectUrl": "https://www.edrdg.org/wiki/index.php/Tanaka_Corpus",
                "licenseUrl": "https://creativecommons.org/licenses/by/2.0/fr/",
            },
            "kanjidicSource": {
                "name": KANJIDIC_SOURCE_NAME,
                "release": KANJIDIC_SOURCE_RELEASE,
                "url": KANJIDIC_SOURCE_URL,
                "sha256": KANJIDIC_SOURCE_SHA256,
            },
            "resources": {
                KANJI_RESOURCE_NAME: {
                    "path": KANJI_RESOURCE_NAME,
                    "sha256": sha256_file(kanji_path),
                    "bytes": kanji_path.stat().st_size,
                },
            },
            "unidic": {"fugashi": "1.5.2", "unidicLite": "1.0.8"},
            "posLabels": {code: tags[code] for code in sorted(used_pos)},
            "shards": shard_manifest,
            "counts": {
                "sourceEntries": len(entries_by_id),
                "exactTerms": total_terms,
                "shards": len(shard_manifest),
                "entryCopies": total_entry_copies,
                "chinese": rich_chinese_counts,
                "tanaka": tanaka_counts,
                "unidic": unidic_counts,
                "kanji": len(kanjidic),
            },
        }
        _write_json(staging / MANIFEST_NAME, manifest)
        validate_output(staging)
        if output.exists():
            existing_manifest = output / MANIFEST_NAME
            if not existing_manifest.is_file():
                raise ValueError(f"refusing to replace unknown directory: {output}")
            try:
                existing_contract = json.loads(
                    existing_manifest.read_text(encoding="utf-8")
                ).get("contract")
            except (OSError, ValueError):
                existing_contract = None
            if existing_contract not in {
                "bw-jmdict-manifest/1",
                "bw-jmdict-manifest/2",
                MANIFEST_CONTRACT,
            }:
                raise ValueError(f"refusing to replace unknown directory: {output}")
            shutil.rmtree(output)
        os.replace(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def validate_output(root: Path) -> dict[str, object]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError("JMdict manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contract") != MANIFEST_CONTRACT:
        raise ValueError("JMdict manifest contract mismatch")
    if manifest.get("normalization") != "NFC":
        raise ValueError("JMdict normalization contract mismatch")
    if manifest.get("shardAlgorithm") != SHARD_ALGORITHM:
        raise ValueError("JMdict shard algorithm mismatch")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("sha256") != SOURCE_SHA256:
        raise ValueError("JMdict source digest mismatch")
    license_value = manifest.get("license")
    if not isinstance(license_value, dict) or license_value.get("path") != LICENSE_NAME:
        raise ValueError("JMdict license declaration mismatch")
    license_path = root / LICENSE_NAME
    if not license_path.is_file() or sha256_file(license_path) != license_value.get("sha256"):
        raise ValueError("JMdict license file mismatch")
    chinese_source = manifest.get("chineseSource")
    if (
        not isinstance(chinese_source, dict)
        or chinese_source.get("release") != RICH_CHINESE_SOURCE_RELEASE
        or chinese_source.get("sha256") != RICH_CHINESE_SOURCE_SHA256
    ):
        raise ValueError("Chinese Wiktionary source digest mismatch")
    chinese_license = manifest.get("chineseLicense")
    if (
        not isinstance(chinese_license, dict)
        or chinese_license.get("path") != CHINESE_LICENSE_NAME
    ):
        raise ValueError("Chinese Wiktionary license declaration mismatch")
    chinese_license_path = root / CHINESE_LICENSE_NAME
    if (
        not chinese_license_path.is_file()
        or chinese_license_path.stat().st_size != chinese_license.get("bytes")
        or sha256_file(chinese_license_path) != chinese_license.get("sha256")
    ):
        raise ValueError("Chinese Wiktionary license file mismatch")
    tanaka_source = manifest.get("tanakaSource")
    if (
        not isinstance(tanaka_source, dict)
        or tanaka_source.get("sha256") != TANAKA_SOURCE_SHA256
    ):
        raise ValueError("Tanaka source digest mismatch")
    tanaka_license = manifest.get("tanakaLicense")
    tanaka_license_path = root / TANAKA_LICENSE_NAME
    if (
        not isinstance(tanaka_license, dict)
        or tanaka_license.get("path") != TANAKA_LICENSE_NAME
        or not tanaka_license_path.is_file()
        or tanaka_license_path.stat().st_size != tanaka_license.get("bytes")
        or sha256_file(tanaka_license_path) != tanaka_license.get("sha256")
    ):
        raise ValueError("Tanaka license file mismatch")
    kanjidic_source = manifest.get("kanjidicSource")
    if (
        not isinstance(kanjidic_source, dict)
        or kanjidic_source.get("sha256") != KANJIDIC_SOURCE_SHA256
    ):
        raise ValueError("KANJIDIC source digest mismatch")
    resources = manifest.get("resources")
    kanji_resource = resources.get(KANJI_RESOURCE_NAME) if isinstance(resources, dict) else None
    kanji_path = root / KANJI_RESOURCE_NAME
    if (
        not isinstance(kanji_resource, dict)
        or kanji_resource.get("path") != KANJI_RESOURCE_NAME
        or not kanji_path.is_file()
        or kanji_path.stat().st_size != kanji_resource.get("bytes")
        or sha256_file(kanji_path) != kanji_resource.get("sha256")
    ):
        raise ValueError("KANJIDIC runtime resource mismatch")

    shards = manifest.get("shards")
    if not isinstance(shards, dict) or not shards:
        raise ValueError("JMdict manifest shards must be a non-empty object")
    declared_paths: set[str] = set()
    for key, item in shards.items():
        if not isinstance(key, str) or len(key) not in {2, 4, 6}:
            raise ValueError("JMdict manifest contains an invalid shard key")
        if any(character not in "0123456789abcdef" for character in key):
            raise ValueError("JMdict manifest contains an invalid shard key")
        if not isinstance(item, dict):
            raise ValueError(f"JMdict shard metadata is invalid: {key}")
        expected_path = f"{SHARD_DIRECTORY}/{key}.json"
        if item.get("path") != expected_path:
            raise ValueError(f"JMdict shard path mismatch: {key}")
        path = root / PurePosixPath(expected_path)
        if not path.is_file():
            raise ValueError(f"JMdict shard is missing: {key}")
        if path.stat().st_size != item.get("bytes"):
            raise ValueError(f"JMdict shard byte count mismatch: {key}")
        if sha256_file(path) != item.get("sha256"):
            raise ValueError(f"JMdict shard digest mismatch: {key}")
        shard = json.loads(path.read_text(encoding="utf-8"))
        if shard.get("contract") != SHARD_CONTRACT or shard.get("key") != key:
            raise ValueError(f"JMdict shard contract mismatch: {key}")
        entries = shard.get("entries")
        exact = shard.get("exact")
        if not isinstance(entries, list) or not isinstance(exact, dict):
            raise ValueError(f"JMdict shard shape mismatch: {key}")
        if len(entries) != item.get("entries") or len(exact) != item.get("terms"):
            raise ValueError(f"JMdict shard count mismatch: {key}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"JMdict shard entry is invalid: {key}")
            glosses = entry.get("zhGlosses")
            if glosses is not None and (
                not isinstance(glosses, list)
                or not glosses
                or len(glosses) > 12
                or any(
                    not isinstance(gloss, str)
                    or not gloss
                    or len(gloss) > 600
                    for gloss in glosses
                )
            ):
                raise ValueError(f"JMdict Chinese glosses are invalid: {key}")
            examples = entry.get("examples")
            if examples is not None and (
                not isinstance(examples, list) or len(examples) > 5
                or any(not isinstance(example, dict) or not example.get("ja") for example in examples)
            ):
                raise ValueError(f"JMdict examples are invalid: {key}")
        for term, indices in exact.items():
            if not isinstance(term, str) or shard_key(term) != key:
                raise ValueError(f"JMdict shard contains a misrouted term: {key}")
            if (
                not isinstance(indices, list)
                or not indices
                or any(
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index >= len(entries)
                    for index in indices
                )
            ):
                raise ValueError(f"JMdict exact index is invalid: {key}/{term}")
        declared_paths.add(expected_path)

    actual_paths = {
        path.relative_to(root).as_posix()
        for path in (root / SHARD_DIRECTORY).glob("*.json")
        if path.is_file()
    }
    if actual_paths != declared_paths:
        raise ValueError("JMdict shard file set differs from the manifest")
    return manifest


def obtain_source(cache_dir: Path, *, offline: bool) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / SOURCE_ARCHIVE_NAME
    if destination.is_file() and sha256_file(destination) == SOURCE_SHA256:
        return destination
    if offline:
        raise ValueError(f"verified JMdict source archive is unavailable: {destination}")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(
            SOURCE_URL, headers={"User-Agent": "BWReader-JMdict-builder/1"}
        )
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        actual = sha256_file(temporary)
        if actual != SOURCE_SHA256:
            raise ValueError(f"JMdict archive digest mismatch: {actual} != {SOURCE_SHA256}")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def obtain_chinese_source(cache_dir: Path, *, offline: bool) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / CHINESE_SOURCE_NAME_ON_DISK
    if destination.is_file() and sha256_file(destination) == CHINESE_SOURCE_SHA256:
        return destination
    if offline:
        raise ValueError(
            f"verified Chinese Wiktionary source is unavailable: {destination}"
        )
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(
            CHINESE_SOURCE_URL,
            headers={"User-Agent": "BWReader-Japanese-dictionary-builder/2"},
        )
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open(
            "wb"
        ) as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        actual = sha256_file(temporary)
        if actual != CHINESE_SOURCE_SHA256:
            raise ValueError(
                "Chinese Wiktionary digest mismatch: "
                f"{actual} != {CHINESE_SOURCE_SHA256}"
            )
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def obtain_pinned_asset(
    cache_dir: Path,
    *,
    name: str,
    url: str,
    digest: str,
    offline: bool,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / name
    if destination.is_file() and sha256_file(destination) == digest:
        return destination
    if offline:
        raise ValueError(f"verified dictionary source is unavailable: {destination}")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "BWReader-Japanese-dictionary-builder/3"}
        )
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        actual = sha256_file(temporary)
        if actual != digest:
            raise ValueError(f"dictionary source digest mismatch: {actual} != {digest}")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def load_source_archive(path: Path) -> object:
    with tarfile.open(path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        if len(members) != 1 or members[0].name != SOURCE_MEMBER_NAME:
            raise ValueError("JMdict archive member closure differs from the pinned release")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError("JMdict archive member cannot be read")
        return json.load(stream)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--rich-chinese-source", type=Path)
    parser.add_argument("--tanaka-source", type=Path)
    parser.add_argument("--kanjidic-source", type=Path)
    parser.add_argument(
        "--verify", type=Path, help="verify generated data without downloading"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verify:
        manifest = validate_output(args.verify.resolve())
    else:
        archive = obtain_source(args.cache_dir.resolve(), offline=args.offline)
        cache = args.cache_dir.resolve()
        rich_chinese_source = args.rich_chinese_source.resolve() if args.rich_chinese_source else obtain_pinned_asset(
            cache, name=RICH_CHINESE_ARCHIVE_NAME, url=RICH_CHINESE_SOURCE_URL,
            digest=RICH_CHINESE_SOURCE_SHA256, offline=args.offline,
        )
        tanaka_source = args.tanaka_source.resolve() if args.tanaka_source else obtain_pinned_asset(
            cache, name=TANAKA_SOURCE_NAME_ON_DISK, url=TANAKA_SOURCE_URL,
            digest=TANAKA_SOURCE_SHA256, offline=args.offline,
        )
        kanjidic_source = args.kanjidic_source.resolve() if args.kanjidic_source else obtain_pinned_asset(
            cache, name=KANJIDIC_SOURCE_NAME_ON_DISK, url=KANJIDIC_SOURCE_URL,
            digest=KANJIDIC_SOURCE_SHA256, offline=args.offline,
        )
        for path, digest, label in (
            (rich_chinese_source, RICH_CHINESE_SOURCE_SHA256, "rich Chinese"),
            (tanaka_source, TANAKA_SOURCE_SHA256, "Tanaka"),
            (kanjidic_source, KANJIDIC_SOURCE_SHA256, "KANJIDIC"),
        ):
            if sha256_file(path) != digest:
                raise ValueError(f"{label} source digest mismatch")
        manifest = build_from_payload(
            load_source_archive(archive),
            args.output,
            rich_chinese_rows=load_wty_records(rich_chinese_source),
            tanaka_source=tanaka_source,
            kanjidic_source=kanjidic_source,
        )
    counts = manifest["counts"]
    print(f"entries={counts['sourceEntries']}")
    print(f"terms={counts['exactTerms']}")
    print(f"shards={counts['shards']}")
    print(f"chinese_entries={counts['chinese']['matchedEntries']}")
    print(f"manifest_sha256={sha256_file((args.verify or args.output).resolve() / MANIFEST_NAME)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
