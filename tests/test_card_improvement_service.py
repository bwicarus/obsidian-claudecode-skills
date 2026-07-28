from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


CORE = Path(__file__).resolve().parents[1] / "_client" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from card_improvement_service import (  # noqa: E402
    CardImprovementError,
    CardImprovementService,
    CardReference,
    CompositeCardResolver,
    JsonEntityRegistryResolver,
    clean_note_draft,
    normalize_pairs,
)


class CardImprovementServiceTest(unittest.TestCase):
    def test_card_reference_accepts_query_index_and_inline_compatibility(self):
        self.assertEqual(
            CardReference.parse("card_abc123", "2"),
            CardReference("card_abc123", 2),
        )
        self.assertEqual(
            CardReference.parse("card_abc123:3"),
            CardReference("card_abc123", 3),
        )
        self.assertEqual(
            CardReference.parse("card_abc123/4"),
            CardReference("card_abc123", 4),
        )
        self.assertFalse(CardReference.parse("old-local-id", None).is_entity)
        with self.assertRaises(CardImprovementError):
            CardReference.parse("card_abc123", "-1")

    def test_json_entity_registry_resolves_card_and_source_without_scanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "card_abc123": {
                            "kind": "cards",
                            "src": "book.pdf#p12",
                            "source_note": "知识/例子.md",
                            "data": [
                                {"type": "basic", "front": "Q1", "back": "A1"},
                                {
                                    "type": "cloze",
                                    "cloze": "{{c1::Q2}}",
                                    "back": "extra",
                                },
                            ],
                            "states": {"1": {"_nid": 12345, "_st": "learn"}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            got = JsonEntityRegistryResolver(registry).resolve(
                CardReference("card_abc123", 1)
            )

        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["local_id"], "card_abc123")
        self.assertEqual(got["entity_index"], 1)
        self.assertEqual(got["text"], "{{c1::Q2}}")
        self.assertEqual(got["anki_note_id"], 12345)
        self.assertEqual(got["source_ref"], "book.pdf#p12")
        self.assertEqual(got["source_note"], "知识/例子.md")

    def test_composite_resolver_keeps_legacy_fallback_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = JsonEntityRegistryResolver(Path(tmp) / "missing.json")

            class Legacy:
                def resolve(self, reference):
                    return {"local_id": reference.card_id, "front": "legacy"}

            got = CompositeCardResolver((missing, Legacy())).resolve(
                CardReference("legacy-42")
            )
        self.assertEqual(got, {"local_id": "legacy-42", "front": "legacy"})

    def test_service_generates_valid_drafts_and_recipe_compatible_trace(self):
        prompts: list[str] = []

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return (
                '说明文字\n[{"type":"basic","front":"改进后的问题",'
                '"back":"改进后的答案","reason":"补足困惑"}]'
            )

        service = CardImprovementService(ask, runner_label="fake-native-thread")
        result = service.improve_cards(
            {"type": "basic", "front": "旧问题", "back": "旧答案"},
            [{"question": "为什么？", "answer": "因为这个条件。"}],
        )

        self.assertEqual(result["cards"][0]["front"], "改进后的问题")
        self.assertIn("旧问题", prompts[0])
        self.assertIn("因为这个条件", prompts[0])
        self.assertEqual(result["trace"]["runner"], "fake-native-thread")
        self.assertEqual(
            result["trace"]["recipe_candidate"],
            {
                "kind": "intent",
                "intent": "improve_review_card",
                "version": 1,
            },
        )
        self.assertEqual(result["trace"]["steps"][0]["name"], "improve_cards")
        self.assertIn("prompt_sha256", result["trace"]["steps"][0])

    def test_prepare_bundle_reuses_one_injected_runner_for_two_turns(self):
        class Runner:
            label = "stateful-test-runner"
            can_reuse_context = True

            def __init__(self):
                self.calls = 0
                self.prompts = []

            def ask(self, prompt):
                self.calls += 1
                self.prompts.append(prompt)
                if self.calls == 1:
                    return (
                        '[{"type":"cloze","text":"{{c1::答案}}",'
                        '"back":"","reason":"复习"}]'
                    )
                return "---\ntitle: x\n---\n原内容。\n补充解释。"

        runner = Runner()
        result = CardImprovementService(runner).prepare_bundle(
            {"type": "basic", "front": "Q", "back": "A"},
            [{"question": "q", "answer": "a"}],
            original_note="---\ntitle: x\n---\n原内容。",
        )

        self.assertEqual(runner.calls, 2)
        self.assertTrue(result["note"]["content"].endswith("补充解释。"))
        self.assertEqual(result["anki"]["cards"][0]["type"], "cloze")
        self.assertEqual(
            [step["name"] for step in result["trace"]["steps"]],
            ["improve_cards", "improve_note"],
        )
        self.assertIn("上一轮", runner.prompts[1])
        self.assertNotIn("问：q", runner.prompts[1])

    def test_note_cleaner_rejects_destructive_short_output(self):
        original = "这是很长的原文。" * 30
        with self.assertRaises(CardImprovementError):
            clean_note_draft(original, "太短")

    def test_pair_normalization_requires_an_answer(self):
        self.assertEqual(
            normalize_pairs(
                [{"question": " q ", "answer": " a "}, {"question": "ignored"}]
            ),
            [{"question": "q", "answer": "a"}],
        )
        with self.assertRaises(CardImprovementError):
            normalize_pairs([{"question": "no answer"}])


if __name__ == "__main__":
    unittest.main()
