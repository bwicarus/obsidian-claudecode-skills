"""Identity-fixed AI adapter for monitoring; no arbitrary programs or owners."""
from __future__ import annotations

import copy

from monitoring import MonitorError


def _contains_owner(value):
    if isinstance(value, dict):
        return any(str(key).casefold() in ("owner", "ownerid", "owner_id") or _contains_owner(child)
                   for key, child in value.items())
    return isinstance(value, list) and any(_contains_owner(child) for child in value)


def dispatch_monitor(service, owner, action, request=None):
    if service is None or not isinstance(owner, str) or not owner:
        raise MonitorError("monitor_unavailable", "账户盯盘服务不可用", 503)
    if action not in ("catalog", "library", "mutate"):
        raise MonitorError("unknown_action", "不支持的盯盘工具动作")
    if request is not None and not isinstance(request, dict):
        raise MonitorError("invalid_request", "request 必须为对象")
    request = copy.deepcopy(request or {})
    if _contains_owner(request):
        raise MonitorError("owner_not_allowed", "账户身份不能由 AI 工具参数指定", 403)
    if action in ("catalog", "library") and request:
        raise MonitorError("unexpected_request", f"{action} 不接受 request 参数")
    result = service.catalog() if action == "catalog" else service.library(owner) if action == "library" else service.mutate(owner, request)
    result = copy.deepcopy(result)
    library = result.get("library") if action == "mutate" else result if action == "library" else None
    if library is not None:
        notifications = library.get("notifications", [])
        library["notificationCount"] = len(notifications)
        library["notifications"] = notifications[:20]
        if len(notifications) > 20:
            library["notificationsTruncated"] = True
    return {"ok": True, "action": action, "result": result}


def dispatch_monitor_safe(service, owner, action, request=None):
    try:
        return dispatch_monitor(service, owner, action, request)
    except MonitorError as exc:
        return {"ok": False, "action": action, "error": {
            "code": exc.code, "message": str(exc), "status": exc.status, "detail": copy.deepcopy(exc.detail)}}


def dispatch_call_safe(service, delivery, owner, action, request=None, *, push_configured=None):
    """Explicit user-requested app calls share durable notification deduplication."""
    try:
        if service is None or delivery is None or not isinstance(owner, str) or not owner:
            raise MonitorError("call_unavailable", "当前账户来电服务不可用", 503)
        if request is not None and not isinstance(request, dict):
            raise MonitorError("invalid_request", "request 必须为对象")
        request = copy.deepcopy(request or {})
        if _contains_owner(request):
            raise MonitorError("owner_not_allowed", "账户身份不能由 AI 工具参数指定", 403)
        if action not in ("status", "request"):
            raise MonitorError("unknown_action", "不支持的来电动作")
        allowed = {"notificationId"} if action == "status" else {"requestId", "title", "text", "code"}
        if set(request) - allowed:
            raise MonitorError("unexpected_request", "来电参数包含不支持的字段")
        if action == "status":
            notice = None
            if "notificationId" in request:
                nid = request["notificationId"]
                if not isinstance(nid, str) or not nid or len(nid) > 128:
                    raise MonitorError("invalid_request", "notificationId 无效")
                notice = service.get_notification(owner, nid)
                if notice is None:
                    raise MonitorError("notification_not_found", "当前账户没有这条通知", 404)
                if notice.get("deliveryMode") != "call":
                    raise MonitorError("not_call_request", "该通知不是显式来电请求")
            return {"ok": True, "action": action, "result": delivery.call_status(owner, notice, push_configured=push_configured)}
        readiness = delivery.call_status(owner, push_configured=push_configured)
        if not readiness["available"]:
            raise MonitorError("call_unavailable", "来电暂不可用，请检查推送配置和 App 通知权限", 409, readiness)
        result = service.mutate(owner, {"requestId": request.get("requestId"), "operation": "notification.create",
            "notification": {"title": request.get("title", "股票提醒"), "body": request.get("text"),
                             "code": request.get("code", ""), "deliveryMode": "call"}})
        notice = service.get_notification(owner, result["notificationId"])
        return {"ok": True, "action": action, "result": {
            **{key: result[key] for key in ("success", "requestId", "revision", "operation", "replayed")},
            **delivery.call_status(owner, notice, push_configured=push_configured),
            "message": "沿用已有请求，请按state报告当前结果，不要重新拨号。" if result["replayed"] else
                       "已保存一次来电请求；当前若有语音，请关闭后等待来电。仅接听后建立语音，等待最多10分钟；不自动重拨。"}}
    except MonitorError as exc:
        return {"ok": False, "action": action, "error": {
            "code": exc.code, "message": str(exc), "status": exc.status, "detail": copy.deepcopy(exc.detail)}}
