#!/usr/bin/env python3
"""Build the pinned, compact JMdict exact-lookup data used by Reader clients.

The generated directory is checked in and is the single source copied into the
native ReaderBundle and the browser extension.  Updating the dictionary is an
explicit operation: change the immutable release URL/version/digest together,
run this script, and review the generated manifest before accepting the update.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
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
REPOSITORY_ROOT = HERE.parents[1]

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

MANIFEST_CONTRACT = "bw-jmdict-manifest/1"
SHARD_CONTRACT = "bw-jmdict-shard/1"
SHARD_ALGORITHM = "utf8-prefix-2-kana-3/1"
KANA_SPLIT_PREFIXES = {"e381", "e382", "e383"}
MANIFEST_NAME = "manifest.json"
LICENSE_NAME = "LICENSE-JMdict.txt"
ZH_OVERLAY_NAME = "zh-overlay.json"
ZH_OVERLAY_CONTRACT = "bw-japanese-zh-overlay/1"
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


def find_overlay_cache(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit.resolve()
    local = REPOSITORY_ROOT / "state" / "dict-cache"
    if local.is_dir():
        return local
    windows_fallback = Path(r"C:\claude\state\dict-cache")
    if windows_fallback.is_dir():
        return windows_fallback
    return None


def _overlay_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _overlay_examples(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    examples: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        example = {
            key: text
            for key in ("ja", "zh")
            if (text := _overlay_text(raw.get(key)))
        }
        if example:
            examples.append(example)
    return examples


def build_zh_overlay(cache_dir: Path | None) -> tuple[dict[str, object], dict[str, int]]:
    """Read the user's existing cache as a non-authoritative optional layer."""
    selected: dict[str, tuple[tuple[float, int, int, str], dict[str, object]]] = {}
    scanned = 0
    rejected = 0
    if cache_dir is not None and cache_dir.is_dir():
        for path in sorted(cache_dir.glob("jp-*.json"), key=lambda item: item.name):
            if not path.is_file():
                continue
            scanned += 1
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                rejected += 1
                continue
            if not isinstance(raw, dict):
                rejected += 1
                continue
            word = _overlay_text(raw.get("word"))
            zh = _overlay_text(raw.get("zh"))
            if not word or not zh:
                rejected += 1
                continue
            word = normalize_term(word)
            reading = _overlay_text(raw.get("reading"))
            pos = _overlay_text(raw.get("pos"))
            source = _overlay_text(raw.get("source"))
            raw_pv = raw.get("pv")
            pv = (
                float(raw_pv)
                if isinstance(raw_pv, (int, float))
                and not isinstance(raw_pv, bool)
                and math.isfinite(float(raw_pv))
                else 0.0
            )
            if pv.is_integer():
                pv_value: int | float = int(pv)
            else:
                pv_value = pv
            examples = _overlay_examples(raw.get("examples"))
            entry: dict[str, object] = {
                "word": word,
                "reading": reading,
                "pos": pos,
                "zh": zh,
                "examples": examples,
                "source": source,
                "pv": pv_value,
            }
            # Higher pv wins.  At equal pv prefer records with populated
            # optional display fields and examples; the filename is the final
            # deterministic tie-breaker and is not exposed in the output.
            rank = (pv, int(bool(reading)) + int(bool(pos)) + int(bool(source)), len(examples), path.name)
            previous = selected.get(word)
            if previous is None or rank > previous[0]:
                selected[word] = (rank, entry)
    overlay = {
        "contract": ZH_OVERLAY_CONTRACT,
        "normalization": "NFC",
        "complete": False,
        "authoritative": False,
        "source": "user-existing-dict-cache",
        "entries": {
            word: selected[word][1]
            for word in sorted(selected)
        },
    }
    return overlay, {
        "scannedFiles": scanned,
        "acceptedTerms": len(selected),
        "rejectedFiles": rejected,
    }


def build_from_payload(
    payload: object,
    output: Path,
    *,
    overlay_cache: Path | None = None,
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

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".DictionaryData-", dir=output.parent))
    try:
        license_payload = LICENSE_SOURCE.read_bytes()
        (staging / LICENSE_NAME).write_bytes(license_payload)
        overlay, overlay_counts = build_zh_overlay(find_overlay_cache(overlay_cache))
        overlay_path = staging / ZH_OVERLAY_NAME
        _write_json(overlay_path, overlay)
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
                "attribution": "JMdict by the Electronic Dictionary Research and Development Group",
                "projectUrl": "https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project",
                "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "zhOverlay": {
                "path": ZH_OVERLAY_NAME,
                "sha256": sha256_file(overlay_path),
                "bytes": overlay_path.stat().st_size,
                "complete": False,
                "authoritative": False,
                "source": "user-existing-dict-cache",
                **overlay_counts,
            },
            "posLabels": {code: tags[code] for code in sorted(used_pos)},
            "shards": shard_manifest,
            "counts": {
                "sourceEntries": len(entries_by_id),
                "exactTerms": total_terms,
                "shards": len(shard_manifest),
                "entryCopies": total_entry_copies,
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
            if existing_contract != MANIFEST_CONTRACT:
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
    overlay_value = manifest.get("zhOverlay")
    if not isinstance(overlay_value, dict) or overlay_value.get("path") != ZH_OVERLAY_NAME:
        raise ValueError("JMdict Chinese overlay declaration mismatch")
    if overlay_value.get("complete") is not False or overlay_value.get("authoritative") is not False:
        raise ValueError("JMdict Chinese overlay must remain partial and non-authoritative")
    overlay_path = root / ZH_OVERLAY_NAME
    if (
        not overlay_path.is_file()
        or overlay_path.stat().st_size != overlay_value.get("bytes")
        or sha256_file(overlay_path) != overlay_value.get("sha256")
    ):
        raise ValueError("JMdict Chinese overlay file mismatch")
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    if (
        overlay.get("contract") != ZH_OVERLAY_CONTRACT
        or overlay.get("normalization") != "NFC"
        or overlay.get("complete") is not False
        or overlay.get("authoritative") is not False
        or not isinstance(overlay.get("entries"), dict)
    ):
        raise ValueError("JMdict Chinese overlay contract mismatch")

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
        "--overlay-cache",
        type=Path,
        help="explicit jp-*.json cache directory (default: workspace, then C:\\claude)",
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
        manifest = build_from_payload(
            load_source_archive(archive),
            args.output,
            overlay_cache=args.overlay_cache,
        )
    counts = manifest["counts"]
    print(f"entries={counts['sourceEntries']}")
    print(f"terms={counts['exactTerms']}")
    print(f"shards={counts['shards']}")
    print(f"manifest_sha256={sha256_file((args.verify or args.output).resolve() / MANIFEST_NAME)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
