from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "ios" / "BWReader" / "build_jmdict_dictionary.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("bw_build_jmdict_dictionary", BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load JMdict builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def source_fixture() -> dict[str, object]:
    return {
        "version": "3.6.2",
        "languages": ["eng"],
        "commonOnly": False,
        "dictDate": "2026-08-10",
        "tags": {
            "v1": "Ichidan verb",
            "vt": "transitive verb",
            "n": "noun (common) (futsuumeishi)",
        },
        "words": [
            {
                "id": "2000000",
                "kanji": [{"common": True, "text": "取り寄せる", "tags": []}],
                "kana": [
                    {
                        "common": True,
                        "text": "とりよせる",
                        "tags": [],
                        "appliesToKanji": ["*"],
                    }
                ],
                "sense": [
                    {
                        "partOfSpeech": ["v1", "vt"],
                        "gloss": [
                            {"lang": "eng", "gender": None, "type": None, "text": "to order"},
                            {"lang": "eng", "gender": None, "type": None, "text": "to have sent"},
                        ],
                    }
                ],
            },
            {
                "id": "2000010",
                "kanji": [],
                "kana": [
                    {
                        "common": False,
                        "text": "か\u3099く",
                        "tags": [],
                        "appliesToKanji": ["*"],
                    }
                ],
                "sense": [
                    {
                        "partOfSpeech": ["n"],
                        "gloss": [
                            {"lang": "eng", "gender": None, "type": None, "text": "fictional fixture"}
                        ],
                    }
                ],
            },
        ],
    }


def chinese_fixture() -> list[dict[str, object]]:
    return [
        {
            "word": "取り寄せる",
            "lang_code": "ja",
            "forms": [
                {
                    "form": "取り寄せる",
                    "ruby": [["取り寄", "とりよ"], ["せる", "せる"]],
                }
            ],
            "senses": [
                {
                    "glosses": [
                        "取り寄せる【とりよせる】\n订购；调货；从外地寄来"
                    ]
                }
            ],
        }
    ]


class JMdictBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = load_builder()

    def test_source_release_is_immutable_and_digest_pinned(self) -> None:
        self.assertEqual(self.builder.SOURCE_RELEASE, "3.6.2+20260810124713")
        self.assertIn("releases/download/3.6.2%2B20260810124713/", self.builder.SOURCE_URL)
        self.assertRegex(self.builder.SOURCE_SHA256, r"^[0-9a-f]{64}$")
        self.assertIn("kaikki.org/zhwiktionary/", self.builder.CHINESE_SOURCE_URL)
        self.assertRegex(self.builder.CHINESE_SOURCE_SHA256, r"^[0-9a-f]{64}$")

    def test_builds_exact_shards_with_chinese_first_glosses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "DictionaryData"
            manifest = self.builder.build_from_payload(
                source_fixture(), output, chinese_records=chinese_fixture()
            )

            self.assertEqual(manifest["contract"], "bw-jmdict-manifest/2")
            self.assertEqual(
                manifest["shardAlgorithm"], "utf8-prefix-2-kana-3/1"
            )
            self.assertEqual(
                manifest["chineseSource"]["sha256"],
                self.builder.CHINESE_SOURCE_SHA256,
            )
            self.assertEqual(manifest["counts"]["chinese"]["matchedEntries"], 1)

            key = self.builder.shard_key("取り寄せる")
            self.assertEqual(key, "e58f")
            shard = json.loads(
                (output / manifest["shards"][key]["path"]).read_text(encoding="utf-8")
            )
            candidates = [shard["entries"][index] for index in shard["exact"]["取り寄せる"]]
            self.assertEqual(candidates[0]["lemma"], "取り寄せる")
            self.assertEqual(candidates[0]["readings"], ["とりよせる"])
            self.assertEqual(candidates[0]["pos"], ["v1", "vt"])
            self.assertEqual(candidates[0]["glosses"], ["to order", "to have sent"])
            self.assertEqual(
                candidates[0]["zhGlosses"],
                ["订购；调货；从外地寄来"],
            )
            self.assertTrue(candidates[0]["common"])

            normalized = "がく"
            self.assertEqual(self.builder.normalize_term("か\u3099く"), normalized)
            normalized_key = self.builder.shard_key(normalized)
            normalized_shard = json.loads(
                (output / manifest["shards"][normalized_key]["path"]).read_text(encoding="utf-8")
            )
            self.assertIn(normalized, normalized_shard["exact"])

            self.assertFalse((output / "zh-overlay.json").exists())
            self.assertTrue((output / "LICENSE-ZhWiktionary.txt").is_file())
            self.builder.validate_output(output)

    def test_verifier_detects_shard_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "DictionaryData"
            manifest = self.builder.build_from_payload(
                source_fixture(), output, chinese_records=chinese_fixture()
            )
            shard_path = output / next(iter(manifest["shards"].values()))["path"]
            shard_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "byte count mismatch|digest mismatch"):
                self.builder.validate_output(output)


if __name__ == "__main__":
    unittest.main()
