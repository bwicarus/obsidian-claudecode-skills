"""Bounded, account-scoped adapter for the selection service.

The model chooses an operation and its business payload.  The authenticated
owner is supplied by the voice session and never accepted from tool input.
"""
from __future__ import annotations

import copy
from typing import Any

from selection import SelectionError


ACTIONS = ("catalog", "evaluate", "library", "mutate")
OWNER_FIELDS = frozenset(("owner", "ownerid", "owner_id"))


def _contains_owner_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).casefold() in OWNER_FIELDS or _contains_owner_field(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_owner_field(child) for child in value)
    return False


def _bounded_library(value: Any) -> Any:
    """Keep an AI receipt useful without returning an entire large watchlist."""
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    groups = result.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group.pop("legacySource", None)
            group.pop("legacyDefinition", None)
            if not isinstance(group.get("codes"), list):
                continue
            codes = group["codes"]
            group["codeCount"] = len(codes)
            group["codes"] = codes[:50]
            if len(codes) > 50:
                group["codesTruncated"] = True
    presets = result.get("presets")
    if isinstance(presets, list):
        for preset in presets:
            if isinstance(preset, dict):
                preset.pop("legacySource", None)
                preset.pop("legacyDefinition", None)
    return result


def _bounded_result(action: str, value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    if action == "evaluate" and isinstance(result.get("items"), list):
        items = result["items"]
        result["items"] = items[:30]
        if len(items) > 30:
            result["itemsTruncated"] = True
    if action == "library":
        return _bounded_library(result)
    if action == "mutate" and isinstance(result.get("library"), dict):
        result["library"] = _bounded_library(result["library"])
        evaluation = result.get("evaluation")
        if isinstance(evaluation, dict) and isinstance(evaluation.get("items"), list):
            items = evaluation["items"]
            evaluation["items"] = items[:30]
            if len(items) > 30:
                evaluation["itemsTruncated"] = True
    return result


def dispatch_selection(service: Any, owner: str, action: str,
                       request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call the shared business service with a trusted owner."""
    if service is None or not isinstance(owner, str) or not owner:
        raise SelectionError("selection_unavailable", "账户选股服务不可用", 503)
    if action not in ACTIONS:
        raise SelectionError("unknown_action", "不支持的选股工具动作")
    if request is not None and not isinstance(request, dict):
        raise SelectionError("invalid_request", "request 必须是对象")
    request = copy.deepcopy(request or {})
    if _contains_owner_field(request):
        raise SelectionError("owner_not_allowed", "账户身份不能由 AI 工具参数指定", 403)
    if action in ("catalog", "library") and request:
        raise SelectionError("unexpected_request", f"{action} 不接受 request 参数")

    if action == "catalog":
        result = service.catalog()
    elif action == "library":
        result = service.load_library(owner)
    elif action == "evaluate":
        # Large candidate arrays add cost without helping a spoken answer.
        request.setdefault("limit", 30)
        if isinstance(request.get("limit"), int):
            request["limit"] = min(request["limit"], 50)
        result = service.evaluate(owner, request)
    else:
        result = service.mutate(owner, request)
    return {"ok": True, "action": action, "result": _bounded_result(action, result)}


def dispatch_selection_safe(service: Any, owner: str, action: str,
                            request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return business errors as structured tool output for the model."""
    try:
        return dispatch_selection(service, owner, action, request)
    except SelectionError as exc:
        return {"ok": False, "action": action, "error": {
            "code": exc.code, "message": str(exc), "status": exc.status,
            "detail": copy.deepcopy(exc.detail),
        }}
