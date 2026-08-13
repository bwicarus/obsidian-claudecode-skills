#!/usr/bin/env python3
"""Build the pinned, App-downloadable Japanese dictionary.

JMdict remains authoritative for spelling, readings, POS and inflection.  A
digest-pinned Simplified-Chinese Wiktionary extract supplies display glosses.
Neither source is bundled in the IPA, books, Pi sync or browser extensions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import unicodedata
import urllib.request


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

MANIFEST_CONTRACT = "bw-jmdict-manifest/2"
SHARD_CONTRACT = "bw-jmdict-shard/2"
SHARD_ALGORITHM = "utf8-prefix-2-kana-3/1"
KANA_SPLIT_PREFIXES = {"e381", "e382", "e383"}
MANIFEST_NAME = "manifest.json"
LICENSE_NAME = "LICENSE-JMdict.txt"
CHINESE_LICENSE_NAME = "LICENSE-ZhWiktionary.txt"
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


def build_from_payload(
    payload: object,
    output: Path,
    *,
    chinese_records,
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

    chinese_counts = attach_chinese_glosses(
        entries_by_id,
        exact_ids,
        chinese_records,
    )
    if chinese_counts["matchedEntries"] <= 0:
        raise ValueError("Chinese Wiktionary source matched no JMdict entries")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".DictionaryData-", dir=output.parent))
    try:
        license_payload = LICENSE_SOURCE.read_bytes()
        (staging / LICENSE_NAME).write_bytes(license_payload)
        chinese_license_payload = CHINESE_LICENSE_SOURCE.read_bytes()
        (staging / CHINESE_LICENSE_NAME).write_bytes(chinese_license_payload)
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
                "name": CHINESE_SOURCE_NAME,
                "release": CHINESE_SOURCE_RELEASE,
                "url": CHINESE_SOURCE_URL,
                "sha256": CHINESE_SOURCE_SHA256,
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
            "posLabels": {code: tags[code] for code in sorted(used_pos)},
            "shards": shard_manifest,
            "counts": {
                "sourceEntries": len(entries_by_id),
                "exactTerms": total_terms,
                "shards": len(shard_manifest),
                "entryCopies": total_entry_copies,
                "chinese": chinese_counts,
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
        or chinese_source.get("release") != CHINESE_SOURCE_RELEASE
        or chinese_source.get("sha256") != CHINESE_SOURCE_SHA256
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
                or len(glosses) > 8
                or any(
                    not isinstance(gloss, str)
                    or not gloss
                    or len(gloss) > 600
                    for gloss in glosses
                )
            ):
                raise ValueError(f"JMdict Chinese glosses are invalid: {key}")
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
    parser.add_argument(
        "--chinese-source",
        type=Path,
        help="explicit digest-pinned Chinese Wiktionary JSONL source",
    )
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
        chinese_source = (
            args.chinese_source.resolve()
            if args.chinese_source is not None
            else obtain_chinese_source(
                args.cache_dir.resolve(),
                offline=args.offline,
            )
        )
        actual_chinese_digest = sha256_file(chinese_source)
        if actual_chinese_digest != CHINESE_SOURCE_SHA256:
            raise ValueError(
                "Chinese Wiktionary digest mismatch: "
                f"{actual_chinese_digest} != {CHINESE_SOURCE_SHA256}"
            )
        manifest = build_from_payload(
            load_source_archive(archive),
            args.output,
            chinese_records=load_chinese_records(chinese_source),
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
