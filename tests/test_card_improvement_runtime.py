from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from card_improvement_runtime import (  # noqa: E402
    CardImprovementDraftStore,
    CardImprovementRuntimeError,
    prepare_card_improvement_draft,
)


CARD = {
    "type": "basic",
    "front": "旧问题",
    "back": "旧答案",
    "local_id": "legacy-1",
    "entity_id": "card_abc123",
    "entity_index": 2,
    "anki_note_id": 41,
    "source_note": "知识/原笔记.md",
    "source_ref": "book.pdf#p3",
    "source_link": "obsidian://open?vault=x&file=y",
    "source_url": "https://example.test/source",
    "deck": "QA",
}
PAIRS = [{"question": "为什么？", "answer": "因为满足这个条件。"}]
CARDS_JSON = (
    '[{"type":"basic","front":"新问题","back":"新答案",'
    '"reason":"补足困惑"}]'
)
ORIGINAL = "---\ntitle: 原笔记\n---\n这是一段足够长的原始笔记内容。"
NOTE_DRAFT = ORIGINAL + "\n\n这是根据有效问答补充的解释。"


class FakeCodexApp:
    def __init__(self, responses=None, *, start_error=None, fail_turn=None):
        self.responses = list(responses or ())
        self.start_error = start_error
        self.fail_turn = fail_turn
        self.started = []
        self.turns = []
        self.closed = []

    def thread_start(self, model):
        self.started.append(model)
        if self.start_error:
            raise self.start_error
        return "thread-1"

    def turn_stream(self, thread_id, prompt, effort, timeout=180):
        turn_no = len(self.turns) + 1
        self.turns.append((thread_id, prompt, effort, timeout))
        if self.fail_turn == turn_no:
            raise RuntimeError(f"turn {turn_no} failed")
        yield self.responses.pop(0)

    def thread_close(self, thread_id):
        self.closed.append(thread_id)


class TierAwareFakeCodexApp(FakeCodexApp):
    def thread_start(self, model, service_tier=""):
        self.started.append((model, service_tier))
        if self.start_error:
            raise self.start_error
        return "thread-1"

    def turn_stream(
        self,
        thread_id,
        prompt,
        effort,
        timeout=180,
        service_tier="",
    ):
        turn_no = len(self.turns) + 1
        self.turns.append(
            (thread_id, prompt, effort, timeout, service_tier)
        )
        if self.fail_turn == turn_no:
            raise RuntimeError(f"turn {turn_no} failed")
        yield self.responses.pop(0)


class CardImprovementRuntimeTest(unittest.TestCase):
    def make_store(self):
        return CardImprovementDraftStore(secret=b"x" * 32)

    def test_bundle_uses_one_native_thread_and_owner_bound_draft(self):
        app = FakeCodexApp([CARDS_JSON, NOTE_DRAFT])
        store = self.make_store()

        result = prepare_card_improvement_draft(
            owner=" user-1 ",
            card=CARD,
            pairs=PAIRS,
            # Deliberately reversed: runtime must canonicalize cards → note.
            target=["note", "anki"],
            original_note=ORIGINAL,
            codex_app=app,
            one_shot=lambda _: self.fail("fallback must not run"),
            store=store,
        )

        self.assertEqual(result["targets"], ["anki", "note"])
        self.assertEqual(app.started, ["gpt-5.6-luna"])
        self.assertEqual([turn[0] for turn in app.turns], ["thread-1", "thread-1"])
        self.assertEqual(app.closed, ["thread-1"])
        self.assertIn("旧问题", app.turns[0][1])
        self.assertIn("上一轮", app.turns[1][1])
        self.assertNotIn("问：为什么？", app.turns[1][1])
        self.assertTrue(result["runner"]["native_multiturn_used"])
        self.assertEqual(result["runner"]["native_turns"], 2)
        self.assertEqual(result["runner"]["service_tier_request"], "")
        self.assertEqual(result["runner"]["service_tier_effective"], "")
        self.assertEqual(result["drafts"]["cards"][0]["front"], "新问题")
        self.assertEqual(result["drafts"]["note"]["content"], NOTE_DRAFT)
        self.assertEqual(len(result["drafts"]["note"]["base_sha256"]), 64)
        self.assertEqual(len(result["identity"]["card_base_sha256"]), 64)
        self.assertEqual(result["identity"]["source_url"], CARD["source_url"])

        stored = store.get(result["draft_id"], "user-1")
        self.assertEqual(stored["drafts"], result["drafts"])
        with self.assertRaises(CardImprovementRuntimeError):
            store.get(result["draft_id"], "user-2")

    def test_thread_start_failure_is_honest_one_shot_fallback(self):
        app = FakeCodexApp(start_error=RuntimeError("app unavailable"))
        prompts = []

        def one_shot(prompt):
            prompts.append(prompt)
            return CARDS_JSON if "只输出 JSON 数组" in prompt else NOTE_DRAFT

        result = prepare_card_improvement_draft(
            owner="user-1",
            card=CARD,
            pairs=PAIRS,
            target="all",
            original_note=ORIGINAL,
            codex_app=app,
            one_shot=one_shot,
            store=self.make_store(),
        )

        self.assertEqual(result["runner"]["mode"], "one_shot_fallback")
        self.assertFalse(result["runner"]["native_multiturn_used"])
        self.assertEqual(result["runner"]["native_turns"], 0)
        self.assertEqual(result["runner"]["one_shot_turns"], 2)
        self.assertIn("app_server_start_failed", result["runner"]["fallback_reason"])
        self.assertEqual(len(prompts), 2)
        # A one-shot note fallback must receive the full source context.
        self.assertIn("问：为什么？", prompts[1])
        self.assertIn("旧问题", prompts[1])

    def test_second_native_turn_failure_falls_back_with_full_note_prompt(self):
        app = FakeCodexApp([CARDS_JSON], fail_turn=2)
        fallbacks = []

        def fallback(prompt):
            fallbacks.append(prompt)
            return NOTE_DRAFT

        result = prepare_card_improvement_draft(
            owner="user-1",
            card=CARD,
            pairs=PAIRS,
            target="all",
            original_note=ORIGINAL,
            codex_app=app,
            one_shot=fallback,
            store=self.make_store(),
        )

        self.assertEqual(result["runner"]["mode"], "hybrid_fallback")
        self.assertEqual(result["runner"]["native_turns"], 1)
        self.assertEqual(result["runner"]["one_shot_turns"], 1)
        self.assertFalse(result["runner"]["native_multiturn_used"])
        self.assertEqual(len(fallbacks), 1)
        self.assertIn("问：为什么？", fallbacks[0])
        self.assertIn("旧问题", fallbacks[0])
        self.assertEqual(result["drafts"]["note"]["content"], NOTE_DRAFT)

    def test_prepare_never_marks_a_target_committed(self):
        store = self.make_store()
        result = prepare_card_improvement_draft(
            owner="user-1",
            card=CARD,
            pairs=PAIRS,
            target="anki",
            one_shot=lambda _: CARDS_JSON,
            store=store,
        )
        self.assertEqual(store.get(result["draft_id"], "user-1")["committed"], [])

    def test_priority_service_tier_reaches_thread_start_and_every_turn(self):
        app = TierAwareFakeCodexApp([CARDS_JSON, NOTE_DRAFT])

        result = prepare_card_improvement_draft(
            owner="user-1",
            card=CARD,
            pairs=PAIRS,
            target="all",
            original_note=ORIGINAL,
            codex_app=app,
            one_shot=lambda _: self.fail("fallback must not run"),
            service_tier="priority",
            store=self.make_store(),
        )

        self.assertEqual(
            app.started,
            [("gpt-5.6-luna", "priority")],
        )
        self.assertEqual(
            [turn[4] for turn in app.turns],
            ["priority", "priority"],
        )
        self.assertEqual(
            result["runner"]["service_tier_request"],
            "priority",
        )
        self.assertEqual(
            result["runner"]["service_tier_effective"],
            "priority",
        )
        self.assertEqual(result["runner"]["fallback_reason"], "")

    def test_priority_on_old_adapter_falls_back_without_false_effective_tier(self):
        app = FakeCodexApp([CARDS_JSON])
        fallback_prompts = []

        def fallback(prompt):
            fallback_prompts.append(prompt)
            return CARDS_JSON

        result = prepare_card_improvement_draft(
            owner="user-1",
            card=CARD,
            pairs=PAIRS,
            target="anki",
            codex_app=app,
            one_shot=fallback,
            service_tier="priority",
            store=self.make_store(),
        )

        # Capability detection refuses to start a thread which cannot carry
        # the requested tier, rather than silently running at the default tier.
        self.assertEqual(app.started, [])
        self.assertEqual(len(fallback_prompts), 1)
        self.assertEqual(result["runner"]["mode"], "one_shot_fallback")
        self.assertEqual(
            result["runner"]["service_tier_request"],
            "priority",
        )
        self.assertEqual(
            result["runner"]["service_tier_effective"],
            "",
        )
        self.assertIn(
            "app_server_service_tier_unsupported:thread_start",
            result["runner"]["fallback_reason"],
        )

    def test_invalid_service_tier_fails_closed(self):
        with self.assertRaisesRegex(
            CardImprovementRuntimeError,
            "service_tier.*priority",
        ):
            prepare_card_improvement_draft(
                owner="user-1",
                card=CARD,
                pairs=PAIRS,
                target="anki",
                one_shot=lambda _: CARDS_JSON,
                service_tier="fast-ish",
                store=self.make_store(),
            )


if __name__ == "__main__":
    unittest.main()
