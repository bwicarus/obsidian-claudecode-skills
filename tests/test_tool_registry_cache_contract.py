"""Offline contracts for cache-stable progressive tool disclosure."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from tool_registry import (  # noqa: E402
    MonotonicToolSession,
    ToolNamespace,
    ToolRegistry,
    ToolRegistryError,
    ToolSpec,
    openai_cache_observation,
)


def _registry(*, reverse: bool = False) -> ToolRegistry:
    namespaces = [
        ToolNamespace("reading", "Read and navigate the active document."),
        ToolNamespace("review", "Review cards and update mastery."),
    ]
    tools = [
        ToolSpec(
            "read_current",
            "Read the currently focused content.",
            "reading",
            core=True,
        ),
        ToolSpec(
            "goto_anchor",
            "Navigate to a document anchor.",
            "reading",
        ),
        ToolSpec(
            "grade_card",
            "Grade the current review card.",
            "review",
            modes=frozenset({"review"}),
        ),
    ]
    if reverse:
        namespaces.reverse()
        tools.reverse()
    return ToolRegistry(namespaces, tools)


class ToolRegistryCacheContractTest(unittest.TestCase):
    def test_catalog_and_prefix_ignore_input_insertion_order(self) -> None:
        first = _registry()
        second = _registry(reverse=True)
        self.assertEqual(first.catalog_version, second.catalog_version)
        self.assertEqual(
            first.stable_text_prefix("text"),
            second.stable_text_prefix("text"),
        )
        self.assertEqual(
            first.openai_tool_search_tools("text"),
            second.openai_tool_search_tools("text"),
        )

    def test_mode_gate_never_changes_cached_tool_projection(self) -> None:
        registry = _registry()
        before = registry.openai_tool_search_tools("text")
        self.assertFalse(
            registry.execution_allowed("grade_card", mode="reading")
        )
        self.assertTrue(
            registry.execution_allowed("grade_card", mode="review")
        )
        self.assertEqual(before, registry.openai_tool_search_tools("text"))
        self.assertNotIn("reading", registry.cache_key("text"))
        self.assertNotIn("review", registry.cache_key("text"))

    def test_text_loading_is_monotonic_and_does_not_rewrite_prefix(self) -> None:
        session = MonotonicToolSession(_registry(), surface="text")
        prefix = session.stable_prefix
        event = session.load("review")
        self.assertIn("grade_card", event)
        self.assertEqual(session.stable_prefix, prefix)
        self.assertEqual(session.load("review"), "")
        with self.assertRaisesRegex(ToolRegistryError, "append-only"):
            session.unload("review")

    def test_openai_projection_defers_non_core_tools(self) -> None:
        projected = _registry().openai_tool_search_tools("text")
        self.assertEqual(projected[-1], {"type": "tool_search"})
        rows = {
            tool["name"]: tool
            for namespace in projected[:-1]
            for tool in namespace["tools"]
        }
        self.assertNotIn("defer_loading", rows["read_current"])
        self.assertTrue(rows["goto_anchor"]["defer_loading"])
        self.assertTrue(rows["grade_card"]["defer_loading"])

    def test_namespace_size_limit_is_enforced(self) -> None:
        namespace = ToolNamespace("crowded", "Too many tools.")
        specs = [
            ToolSpec(f"tool_{index}", "Do one thing.", "crowded")
            for index in range(10)
        ]
        with self.assertRaisesRegex(ToolRegistryError, "limit is 9"):
            ToolRegistry([namespace], specs)

    def test_openai_cache_metrics_keep_catalog_identity(self) -> None:
        registry = _registry()
        row = openai_cache_observation(
            {
                "input_tokens": 2_000,
                "input_tokens_details": {
                    "cached_tokens": 1_536,
                    "cache_write_tokens": 0,
                },
            },
            registry=registry,
            surface="text",
            loaded_namespaces=("review",),
        )
        self.assertEqual(row["catalog_version"], registry.catalog_version)
        self.assertEqual(row["cached_tokens"], 1_536)
        self.assertEqual(row["cache_read_ratio"], 0.768)
        self.assertEqual(row["loaded_namespaces"], ["review"])


if __name__ == "__main__":
    unittest.main()
