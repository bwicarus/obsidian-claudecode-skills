"""Production contracts for the shared reader ToolRegistry."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402
import task_runtime  # noqa: E402
import voice  # noqa: E402
import voice_realtime_relay as relay  # noqa: E402


class ToolRegistryProductionTest(unittest.TestCase):
    def test_every_legacy_handler_is_registered_once(self) -> None:
        handler_names = {
            spec.name
            for spec in assistant.TOOL_REGISTRY.tools
            if spec.name in assistant.TOOL_HANDLER_NAMES
        }
        self.assertEqual(handler_names, set(assistant.TOOLS))
        self.assertTrue(
            all(callable(row[1]) for row in assistant.TOOLS.values())
        )
        self.assertTrue(
            all(
                len(assistant.TOOL_REGISTRY.tools_in(namespace.name)) <= 9
                for namespace in assistant.TOOL_REGISTRY.namespaces
            )
        )

    def test_catalog_is_deterministic_when_handler_input_order_changes(self) -> None:
        reversed_tools = dict(reversed(tuple(assistant.TOOLS.items())))
        rebuilt = assistant._build_tool_registry(reversed_tools)
        self.assertEqual(
            rebuilt.catalog_version,
            assistant.TOOL_REGISTRY.catalog_version,
        )
        for surface in (
            assistant.SURFACE_ASSISTANT_TEXT,
            assistant.SURFACE_MCP_WORKER,
            assistant.SURFACE_VOICE_EXECUTE,
            assistant.SURFACE_RTC_DIRECT,
            assistant.SURFACE_REALTIME_WS,
            assistant.SURFACE_DOUBAO_S2S,
        ):
            self.assertEqual(
                rebuilt.realtime_tools(surface),
                assistant.TOOL_REGISTRY.realtime_tools(surface),
            )

    def test_surface_projections_preserve_existing_capabilities(self) -> None:
        handlers = set(assistant.TOOLS)

        def names(surface):
            return {
                spec.name
                for spec in assistant.TOOL_REGISTRY.visible_tools(surface)
            }

        self.assertEqual(
            names(assistant.SURFACE_ASSISTANT_TEXT),
            handlers - {"page_new", "page_add", "page_show"},
        )
        self.assertEqual(names(assistant.SURFACE_MCP_WORKER), handlers)
        self.assertEqual(names(assistant.SURFACE_VOICE_EXECUTE), handlers)
        self.assertEqual(
            names(assistant.SURFACE_RTC_DIRECT),
            handlers
            - {"page_new", "page_add", "page_show", "read_selection"}
            | {"deep_think", "route_to_text", "wait_for_user"},
        )
        self.assertEqual(
            names(assistant.SURFACE_REALTIME_WS),
            handlers | {"deep_think", "recall_study", "wait_for_user"},
        )
        self.assertEqual(
            names(assistant.SURFACE_DOUBAO_S2S),
            handlers | {"deep_think", "recall_study"},
        )

    def test_executor_gate_denies_hidden_tool_before_handler(self) -> None:
        fake = Mock(return_value={"ok": True})
        with patch.dict(
            assistant.TOOLS,
            {"page_new": (assistant.TOOLS["page_new"][0], fake)},
        ):
            denied = assistant._run_tool(
                "page_new",
                {},
                {},
                surface=assistant.SURFACE_ASSISTANT_TEXT,
            )
            self.assertEqual(denied["code"], "tool_not_available")
            fake.assert_not_called()

            allowed = assistant._run_tool(
                "page_new",
                {},
                {},
                surface=assistant.SURFACE_MCP_WORKER,
            )
            self.assertTrue(allowed["ok"])
            fake.assert_called_once()

    def test_registry_api_and_dispatch_use_trusted_surfaces(self) -> None:
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(assistant.bp)
        client = app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = "only-user"

        directory = client.get(
            "/api/assistant/tools?surface=realtime_ws&namespace=runtime"
        )
        self.assertEqual(directory.status_code, 200)
        body = directory.get_json()
        self.assertEqual(
            body["catalog_version"],
            assistant.TOOL_REGISTRY.catalog_version,
        )
        self.assertEqual(
            {row["name"] for row in body["tools"]},
            {"deep_think", "recall_study", "wait_for_user"},
        )
        self.assertEqual(
            body["tools"][0]["parameters"]["type"],
            "object",
        )

        run = Mock(return_value={"ok": True})
        with patch.object(assistant, "_run_tool", run):
            response = client.post(
                "/api/assistant/tool",
                json={
                    "name": "read_page",
                    "args": {},
                    "surface": "internal",
                    "ctx": {"surface": "internal"},
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            run.call_args.kwargs["surface"],
            assistant.SURFACE_MCP_WORKER,
        )

        run.reset_mock()
        with (
            patch.object(assistant, "_run_tool", run),
            patch.object(assistant, "_creation_register"),
            patch.object(assistant, "_attn_tool_event"),
        ):
            response = client.post(
                "/api/assistant/voice-tool",
                json={
                    "cmd": '{"tool":"read_page","args":{}}',
                    "surface": "internal",
                    "ctx": {"surface": "internal"},
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            run.call_args.kwargs["surface"],
            assistant.SURFACE_VOICE_EXECUTE,
        )

    def test_user_description_overlay_does_not_change_catalog_identity(self) -> None:
        version = assistant.TOOL_REGISTRY.catalog_version

        def fake_tp(uid, tool, slot, default):
            if uid == "alice" and tool == "read_page" and slot == "desc":
                return "Alice-specific read page description"
            return default

        with patch.object(assistant, "_tp", side_effect=fake_tp):
            catalog = assistant._tool_catalog_text("alice")
        self.assertIn("Alice-specific read page description", catalog)
        self.assertEqual(version, assistant.TOOL_REGISTRY.catalog_version)

    def test_rtc_tool_schema_is_stable_when_filler_policy_changes(self) -> None:
        with (
            patch.object(assistant, "_rtc_cfg", return_value={}),
            patch.object(
                assistant,
                "_tp",
                side_effect=lambda _uid, _tool, _slot, default: default,
            ),
            patch.object(assistant, "_filler_mode", return_value="always"),
        ):
            first, _threshold, _image = assistant._build_rtc_session(
                "only-user",
                "",
                0,
            )
        with (
            patch.object(assistant, "_rtc_cfg", return_value={}),
            patch.object(
                assistant,
                "_tp",
                side_effect=lambda _uid, _tool, _slot, default: default,
            ),
            patch.object(assistant, "_filler_mode", return_value="never"),
        ):
            second, _threshold, _image = assistant._build_rtc_session(
                "only-user",
                "",
                0,
            )
        self.assertEqual(first["tools"], second["tools"])
        self.assertNotEqual(first["instructions"], second["instructions"])
        self.assertEqual(
            len(first["tools"]),
            len(
                assistant.TOOL_REGISTRY.visible_tools(
                    assistant.SURFACE_RTC_DIRECT
                )
            ),
        )

    def test_voice_cli_worker_references_registry_version_and_mcp_bridge(self) -> None:
        prompt = voice._agent_registry_prompt()
        self.assertIn(assistant.TOOL_REGISTRY.catalog_version, prompt)
        self.assertIn("assistant_tools", prompt)
        self.assertIn("assistant_call_tool", prompt)

    def test_relay_uses_registry_schema_rows_without_local_copy(self) -> None:
        rows = [
            {
                "name": "goto_page",
                "description": "navigate",
                "parameters": assistant.TOOL_REGISTRY.get(
                    "goto_page"
                ).parameters,
            }
        ]
        projected = relay._catalog_to_realtime_tools(rows)
        self.assertEqual(projected[0]["name"], "goto_page")
        self.assertEqual(
            projected[0]["parameters"],
            assistant.TOOL_REGISTRY.get("goto_page").parameters,
        )

    def test_composite_task_replay_uses_registry_executor(self) -> None:
        run = Mock(return_value={
            "ok": True,
            "client_action": {"fn": "render", "args": []},
        })
        with patch.object(assistant, "_run_tool", run):
            result = task_runtime.run_trace(
                {
                    "calls": [{"tool": "page_show", "args": {}}],
                    "rebind": False,
                },
                {"file_rel": "book.pdf", "page": 3, "_uid": "only-user"},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["n_actions"], 1)
        self.assertEqual(
            run.call_args.kwargs["surface"],
            assistant.SURFACE_INTERNAL,
        )


if __name__ == "__main__":
    unittest.main()
