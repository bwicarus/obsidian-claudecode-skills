from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from _server_deploy import mcp_server


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    result = CallToolResult(content=[TextContent(type="text", text="ok")])
    called = None

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=[
            Tool(
                name="reader_context_snapshot",
                description="fresh snapshot",
                inputSchema={"type": "object", "properties": {}},
                annotations=ToolAnnotations(readOnlyHint=True),
            ),
            Tool(
                name="not_a_reader_tool",
                description="must not escape the allowlist",
                inputSchema={"type": "object", "properties": {}},
            ),
        ])

    async def call_tool(self, name, arguments):
        type(self).called = (name, arguments)
        return type(self).result


class ReaderPcProxyTests(unittest.IsolatedAsyncioTestCase):
    def _patch_transport(self):
        return (
            patch.object(mcp_server, "_reader_pc_server_parameters", return_value=object()),
            patch.object(mcp_server, "stdio_client", return_value=_AsyncContext((object(), object()))),
            patch.object(mcp_server, "ClientSession", _FakeSession),
        )

    async def test_catalog_projects_only_allowlisted_reader_tools(self):
        one, two, three = self._patch_transport()
        with one, two, three:
            result = await mcp_server._reader_pc_list_tools()

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "windows-readerpc")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["tools"][0]["name"], "reader_context_snapshot")
        self.assertEqual(result["tools"][0]["annotations"]["readOnlyHint"], True)

    async def test_call_preserves_original_mcp_result(self):
        expected = CallToolResult(
            content=[TextContent(type="text", text="original receipt")],
            isError=False,
        )
        _FakeSession.result = expected
        _FakeSession.called = None
        one, two, three = self._patch_transport()
        with one, two, three:
            result = await mcp_server._reader_pc_call(
                "reader_context_snapshot", {"include": "page"}
            )

        self.assertIs(result, expected)
        self.assertEqual(
            _FakeSession.called,
            ("reader_context_snapshot", {"include": "page"}),
        )

    async def test_call_rejects_tools_outside_allowlist_without_starting_child(self):
        with patch.object(mcp_server, "stdio_client") as transport:
            result = await mcp_server._reader_pc_call("shell", {"command": "whoami"})

        self.assertTrue(result.isError)
        self.assertFalse(transport.called)
        self.assertIn("READER_PC_TOOL_NOT_ALLOWED", result.content[0].text)


if __name__ == "__main__":
    unittest.main()


class ReaderPcParametersTests(unittest.TestCase):
    """2026-09-26：迁到 Mac 后代理一直 READER_PC_UNAVAILABLE —— 只认 Windows EXE，且子进程拿不到
    DOTNET_ROOT（MCP SDK 默认只传 HOME/PATH/SHELL/TERM）。"""

    def test_explicit_command_and_state_pass_dotnet_environment(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile() as exe:
            env = {"READER_CONTEXT_MCP_COMMAND": exe.name,
                   "READER_CONTEXT_MCP_STATE": "/tmp/runtime/reader-context-snapshot.json",
                   "DOTNET_ROOT": "/Users/x/.dotnet", "LOCALAPPDATA": "/Users/x/BW/data",
                   "MCP_WEBAPP_TOKEN": "secret-token"}
            with patch.dict(os.environ, env, clear=False):
                params = mcp_server._reader_pc_server_parameters()
        self.assertEqual(params.command, exe.name)
        self.assertEqual(params.args, ["--reader-context-mcp", "--state", "/tmp/runtime/reader-context-snapshot.json"])
        self.assertEqual(params.env["DOTNET_ROOT"], "/Users/x/.dotnet")
        self.assertEqual(params.env["LOCALAPPDATA"], "/Users/x/BW/data")
        self.assertNotIn("MCP_WEBAPP_TOKEN", params.env, "only the listed runtime keys may reach the child")

    def test_missing_executable_names_the_path_it_looked_at(self):
        import os
        with patch.dict(os.environ, {"READER_CONTEXT_MCP_COMMAND": "/nonexistent/bw-reader-bridge"}, clear=False):
            with self.assertRaisesRegex(FileNotFoundError, "/nonexistent/bw-reader-bridge"):
                mcp_server._reader_pc_server_parameters()


class VoiceBriefTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_brief_combines_situation_and_full_snapshot_in_one_call(self):
        snapshot = CallToolResult(content=[TextContent(type="text", text="basis=live 当前页 p.12 …" + "x" * 20000)])
        calls = []

        async def fake_call(name, args):
            calls.append((name, args))
            return snapshot

        with patch.object(mcp_server, "user_situation", return_value={"ok": True, "place": {"known": False}}), \
             patch.object(mcp_server, "_reader_pc_call", fake_call):
            result = await mcp_server.voice_brief()
        self.assertEqual(calls, [("reader_context_snapshot", {})], "full snapshot, not brief:true")
        self.assertTrue(result["ok"])
        self.assertEqual(result["situation"]["place"]["known"], False)
        self.assertTrue(result["reader"]["ok"])
        self.assertTrue(result["reader"]["truncated"])
        self.assertEqual(len(result["reader"]["snapshot"]), mcp_server._VOICE_BRIEF_SNAPSHOT_LIMIT)

    async def test_voice_brief_says_why_when_reader_is_unavailable(self):
        async def fake_call(name, args):
            return mcp_server._reader_pc_error(FileNotFoundError("no bridge"))

        with patch.object(mcp_server, "user_situation", return_value={"ok": True}), \
             patch.object(mcp_server, "_reader_pc_call", fake_call):
            result = await mcp_server.voice_brief()
        self.assertFalse(result["reader"]["ok"])
        self.assertIn("READER_PC_UNAVAILABLE", result["reader"]["why"])
