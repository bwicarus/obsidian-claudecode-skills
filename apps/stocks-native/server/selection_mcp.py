"""Per-voice-session stdio MCP exposing only account-scoped stock selection."""
from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from data import StockDataStore
from selection import SelectionService
from selection_tools import dispatch_selection_safe


server = FastMCP("stocks_selection")
_service: SelectionService | None = None


def _runtime() -> tuple[SelectionService, str]:
    global _service
    owner = os.environ.get("STOCKS_SELECTION_OWNER", "")
    state_root = os.environ.get("STOCKS_SELECTION_STATE_DIR", "")
    data_root = os.environ.get("STOCKS_SELECTION_DATA_ROOT", "")
    if not owner or not state_root or not data_root:
        raise RuntimeError("stocks selection MCP is missing trusted session configuration")
    if _service is None:
        _service = SelectionService(StockDataStore(data_root), state_root)
    return _service, owner


@server.tool(
    description=(
        "管理当前 Apple 账户的选股器、观察池和智能收藏夹。"
        "action=catalog 读取条件目录；library 读取方案和观察组；"
        "evaluate 运行 OR 组/组内 AND-NOT 选股；mutate 执行一次明确写操作。"
        "mutate.request 必须包含唯一 requestId、刚读取的 expectedRevision、operation 和 payload；"
        "revision_conflict 时重新读取 library 后再决定，不能覆盖。账户身份由会话固定，参数中禁止 owner。"
    ),
)
def stocks_selection(
    action: Literal["catalog", "evaluate", "library", "mutate"],
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service, owner = _runtime()
    return dispatch_selection_safe(service, owner, action, request)


if __name__ == "__main__":
    server.run(transport="stdio")
