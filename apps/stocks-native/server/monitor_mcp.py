"""Per-session stdio monitoring MCP with account identity from trusted config."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from monitoring import MonitorService
from monitor_tools import dispatch_monitor_safe, dispatch_call_safe
from notification_delivery import NotificationDelivery
from plans import PlanService, PlanError
from schedules import ScheduleService, dispatch_schedule_safe


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


@server.tool(description=(
    "生成并保存当前账户的个股操作方案；方案会同时显示在语音侧栏与个股详情。"
    "catalog查结构和枚举；list用{code?,limit?,includeArchived?}取历史及revision；get用{id}。"
    "save用{requestId,expectedRevision,plan}，plan包含code,title,summary,mode,recommendedVariantId,"
    "variants和basis；具体字段先查catalog。archive用{requestId,expectedRevision,id}。"
    "分析需要方案时先取得有时间的行情依据；保存不证明行情正确，不启动盯盘、不记账、不交易。"
    "来源和账户由服务固定，禁止传owner/source；重试复用同一requestId及原始payload。"
))
def stocks_plan(action: Literal["catalog", "list", "get", "save", "archive"],
                request: dict[str, Any] | None = None) -> dict[str, Any]:
    _, owner = _runtime()
    service = PlanService(os.environ["STOCKS_MONITOR_STATE_DIR"])
    try:
        if request is not None and not isinstance(request, dict):
            raise PlanError("invalid_request", "request 必须为对象")
        request = request or {}
        allowed = {"catalog": set(), "list": {"code", "limit", "includeArchived"}, "get": {"id"},
                   "save": {"requestId", "expectedRevision", "plan"},
                   "archive": {"requestId", "expectedRevision", "id"}}.get(action)
        if allowed is None or set(request) - allowed:
            raise PlanError("invalid_request", "方案动作或参数无效，账户与来源由服务器固定")
        if action == "catalog":
            result = service.catalog()
        elif action == "list":
            result = service.list(owner, request.get("code"), request.get("limit", 20), request.get("includeArchived", False))
        elif action == "get":
            result = service.get(owner, request.get("id"))
        elif action == "save":
            result = service.save(owner, request, {"sessionId": os.environ.get("STOCKS_VOICE_SESSION_ID")})
        else:
            result = service.archive(owner, request)
        return {"ok": True, "action": action, "result": result}
    except PlanError as exc:
        return {"ok": False, "action": action, "error": {
            "code": exc.code, "message": str(exc), "status": exc.status, "detail": exc.detail}}


@server.tool(description=(
    "管理当前账户的持久定时提醒和股票报价分析；语音关闭后仍由VPS按时执行。"
    "catalog查看结构、时间和限制；list列任务；get/runs用{id,limit?}；mutate用"
    "{requestId,expectedRevision,operation:'task.upsert|task.pause|task.resume|task.cancel',task或id}。"
    "task包含kind:'reminder|analysis',title,prompt,codes,schedule,deliveryMode:'auto|call'；"
    "schedule为once(at ISO),daily(time HH:MM),weekly(time,days MO..SU)，都需timezone IANA。"
    "analysis codes必须是1至10个明确六位股票代码，执行时读取报价；不提供尚未接入的报告或交易。"
    "先catalog/list，用用户设备时区理解明早等时间；时区不明先问，勿用VPS UTC猜测。"
    "只有用户明确要求届时打电话才用call，其余auto；重试复用requestId，不立即创建未来来电。"
))
def stocks_schedule(action: Literal["catalog", "list", "get", "runs", "mutate"],
                    request: dict[str, Any] | None = None) -> dict[str, Any]:
    _, owner = _runtime()
    service = ScheduleService(os.environ["STOCKS_MONITOR_STATE_DIR"])
    result = dispatch_schedule_safe(service, owner, action, request)
    if action == "catalog" and result.get("ok"):
        result["result"]["clientTimeZone"] = os.environ.get("STOCKS_CLIENT_TIME_ZONE") or None
        result["result"]["currentTimeUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


if __name__ == "__main__":
    server.run(transport="stdio")
