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
