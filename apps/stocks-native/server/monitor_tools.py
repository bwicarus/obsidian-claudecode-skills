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
