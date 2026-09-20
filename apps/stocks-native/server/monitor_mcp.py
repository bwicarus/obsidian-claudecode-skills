"""Per-session stdio monitoring MCP with account identity from trusted config."""
from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from monitoring import MonitorService
from monitor_tools import dispatch_monitor_safe, dispatch_call_safe
from notification_delivery import NotificationDelivery


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


@server.tool(description=(
    "给当前账户的股票 App 发起系统来电，接听后播报 text。用户明确说打给我/给我来电/打电话告诉我时使用；"
    "这是 App 网络语音来电，不是拨打手机号。request 动作的 request 参数为 {requestId:唯一意图编号,text:接听后要说的话,title?:标题,code?:六位股票代码}。"
    "当前有语音则排队，告知用户关闭当前通话后等待来电；不会强行挂断，最多等10分钟且只拨一次。"
    "status 动作不传 request 查询能力，或传 {notificationId:此前回执编号} 查询投递结果。"
    "queued/waiting_for_current_voice仅表示排队，push_accepted仅表示推送被接受，answered才是接听，audioSubmitted也不保证听见。"
    "重试必须复用requestId，结果不明先查status，禁止反复创建来电。账户由会话固定，不接受owner或手机号。"
))
def stocks_call(action: Literal["status", "request"], request: dict[str, Any] | None = None) -> dict[str, Any]:
    service, owner = _runtime()
    delivery = NotificationDelivery(os.environ["STOCKS_MONITOR_STATE_DIR"])
    return dispatch_call_safe(service, delivery, owner, action, request,
                              push_configured=os.environ.get("STOCKS_MONITOR_CALLS_CONFIGURED") == "1")


if __name__ == "__main__":
    server.run(transport="stdio")
