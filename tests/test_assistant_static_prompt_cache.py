"""Contracts for cache-stable, per-user assistant system prompts."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import assistant  # noqa: E402


class AssistantStaticPromptCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._cache = assistant._SYS_STATIC_CACHE
        self._warm = (
            assistant._warm_p,
            assistant._warm_on,
            assistant._warm_uid,
        )
        assistant._SYS_STATIC_CACHE = {}
        assistant._warm_p = None
        assistant._warm_on = False
        assistant._warm_uid = ""

    def tearDown(self) -> None:
        assistant._SYS_STATIC_CACHE = self._cache
        (
            assistant._warm_p,
            assistant._warm_on,
            assistant._warm_uid,
        ) = self._warm

    def test_static_prompt_is_cached_per_user(self) -> None:
        builds: list[str] = []

        def fake_prompt(ctx):
            uid = str(ctx.get("_uid") or "")
            builds.append(uid)
            return f"static:{uid}\n【当前页面】dynamic"

        with patch.object(assistant, "_sys_prompt", side_effect=fake_prompt):
            self.assertEqual(assistant._sys_static("alice"), "static:alice")
            self.assertEqual(assistant._sys_static("bob"), "static:bob")
            self.assertEqual(assistant._sys_static("alice"), "static:alice")

        self.assertEqual(builds, ["alice", "bob"])

    def test_custom_description_is_in_its_owners_static_prefix(self) -> None:
        def fake_tp(uid, tool, slot, default):
            if slot == "desc" and tool == "read_page":
                return f"custom description for {uid}/{tool}"
            return default

        recipes = Mock(return_value="\nDYNAMIC_RECIPE_CATALOG")
        with (
            patch.object(assistant, "_tp", side_effect=fake_tp),
            patch.object(assistant, "_recipes_prompt_line", recipes),
        ):
            static = assistant._sys_static("alice")
            self.assertIn("custom description for alice/read_page", static)
            self.assertIn(
                assistant.TOOL_REGISTRY.catalog_version,
                static,
            )
            self.assertNotIn("DYNAMIC_RECIPE_CATALOG", static)
            recipes.assert_not_called()

            dynamic = assistant._ctx_block({"_uid": "alice", "no_book": True})
            self.assertIn("DYNAMIC_RECIPE_CATALOG", dynamic)
            recipes.assert_called_once()

    def test_reset_invalidates_only_the_selected_user(self) -> None:
        revision = {"alice": 1, "bob": 1}

        def fake_prompt(ctx):
            uid = str(ctx.get("_uid") or "")
            return f"{uid}:v{revision[uid]}\n【当前页面】dynamic"

        with patch.object(assistant, "_sys_prompt", side_effect=fake_prompt):
            self.assertEqual(assistant._sys_static("alice"), "alice:v1")
            self.assertEqual(assistant._sys_static("bob"), "bob:v1")
            revision.update(alice=2, bob=2)

            assistant._sys_cache_reset("alice")

            self.assertEqual(assistant._sys_static("alice"), "alice:v2")
            self.assertEqual(
                assistant._sys_static("bob"),
                "bob:v1",
                "another user's exact cached prefix must remain reusable",
            )

    def test_reading_or_review_context_does_not_change_static_prefix(self) -> None:
        calls: list[str] = []

        def fake_prompt(ctx):
            calls.append(str(ctx.get("_uid") or ""))
            return "one stable prefix\n【当前页面】dynamic"

        with (
            patch.object(assistant, "_sys_prompt", side_effect=fake_prompt),
            patch.object(assistant, "_recipes_prompt_line", return_value=""),
        ):
            before = assistant._sys_static("alice")
            assistant._ctx_block({"_uid": "alice", "assistant_mode": "reading"})
            assistant._ctx_block({"_uid": "alice", "assistant_mode": "review"})
            after = assistant._sys_static("alice")

        self.assertEqual(before, after)
        self.assertEqual(
            calls.count("alice"),
            3,
            "one static build plus two independent dynamic context builds",
        )

    def test_prewarm_process_is_not_reused_across_users(self) -> None:
        spawned = []

        class FakeProcess:
            def __init__(self, system):
                self.system = system

            def poll(self):
                return None

        def fake_spawn(*_args, **kwargs):
            proc = FakeProcess(kwargs.get("system"))
            spawned.append(proc)
            return proc

        killed = []
        with (
            patch.object(assistant, "_sys_static", side_effect=lambda uid="": f"static:{uid}"),
            patch.object(assistant, "_spawn", side_effect=fake_spawn),
            patch.object(assistant, "_kill", side_effect=killed.append),
        ):
            assistant._warm_prewarm("alice")
            alice_process = assistant._warm_p
            bob_process = assistant._take_proc(uid="bob")

        self.assertEqual([p.system for p in spawned], ["static:alice", "static:bob"])
        self.assertIs(bob_process, spawned[1])
        self.assertIn(alice_process, killed)

    def test_claude_runner_respawns_for_the_same_user(self) -> None:
        source = inspect.getsource(assistant._agent_run_claude)
        self.assertIn(
            "target=_warm_respawn, args=(uid,)",
            source,
            "a consumed user-scoped process must not be replaced by the default user's prompt",
        )


if __name__ == "__main__":
    unittest.main()
