"""Per-session stdio monitoring MCP with account identity from trusted config."""
from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from monitoring import MonitorService
from monitor_tools import dispatch_monitor_safe


server = FastMCP("stocks_monitor")
_service: MonitorService | None = None


def _runtime():
    global _service
    owner = os.environ.get("STOCKS_MONITOR_OWNER", "")
    root = os.environ.get("STOCKS_MONITOR_STATE_DIR", "")
    if not owner or not root:
        raise RuntimeError("stocks monitor MCP is missing trusted session configuration")
    if _service is None:
        _service = MonitorService(root)
    return _service, owner


@server.tool(description=(
    "管理当前 Apple 账户的规则盯盘与通知。catalog 查询支持指标和阈值语义；library 读取规则及通知；"
    "mutate 执行 rule.upsert/rule.pause/rule.resume/rule.delete/notification.create/notification.read/notification.resolve。"
    "request 必须包含唯一 requestId，建议包含刚读取的 expectedRevision，以及 operation；"
    "upsert 携带 rule，create 携带 notification，其他操作携带 id。"
    "rule 包含 title,code,conditions:[{metric,op,threshold}]，可选 match,confirmSeconds,cooldownSeconds,rearmPercent,severity,enabled。"
    "通知包含 title,body，可选 code,severity。账户身份由会话固定，禁止传 owner。"
    "通知已读与已处理不同；规则触发不保证行情未来走势，不得把投递成功当作已执行交易。"
))
def stocks_monitor(action: Literal["catalog", "library", "mutate"], request: dict[str, Any] | None = None) -> dict[str, Any]:
    service, owner = _runtime()
    return dispatch_monitor_safe(service, owner, action, request)


if __name__ == "__main__":
    server.run(transport="stdio")
