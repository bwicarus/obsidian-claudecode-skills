"""server-config.json 字段 schema + 校验。

control.py POST /control/api/config 用 validate_partial 过滤未声明字段 +
类型不匹配的字段，避免「字段名打错静默生效」这种 silent failure。

schema 是 dot-path → type 映射，支持嵌套（如 'anki.auto_restart'）。
跟字段对应的具体含义在 CLAUDE.md「服务器侧配置」节。
"""
from __future__ import annotations

from typing import Any

SCHEMA: dict[str, type] = {
    # iPad 截图问答 / qa-server
    "qa_vault_path":                   str,
    "qa_index_dir":                    str,
    "qa_anki_records_dir":             str,
    "qa_exercises_subdir":             str,
    "qa_wrong_subdir":                 str,
    "qa_remote_daemon":                bool,
    "qa_remote_access":                bool,

    # AI 后端
    "ai_backend":                      str,
    "ai.claude_cli.command":           str,
    "ai.codex_cli.command":            str,
    "ai.ollama.api_key":               str,
    "ai.ollama.model":                 str,
    "ai.ollama.base_url":              str,

    # Anki
    "anki.exe_path":                   str,
    "anki.connect_url":                str,
    "anki.auto_restart":               bool,

    # 登记 + 上传
    "auto_upload_after_register":      bool,

    # 凌晨 daily 定时
    "scheduled_register.enabled":      bool,
    "scheduled_register.time":         str,
    "scheduled_register.wake_anki":    bool,
    "scheduled_register.upload_after": bool,
}


def _flatten(d: dict, prefix: str = "") -> dict:
    """嵌套 dict 拍扁成 dot-path → value（非 dict 的叶子）。"""
    out: dict = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, path))
        else:
            out[path] = v
    return out


def _unflatten(flat: dict) -> dict:
    """dot-path 还原为嵌套 dict。"""
    out: dict = {}
    for path, v in flat.items():
        parts = path.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


def validate_partial(data: Any) -> tuple[dict, list[str]]:
    """校验 partial config 更新。

    返回 (clean_data, errors)：
    - clean_data: 只含已声明字段且类型正确的嵌套 dict（可直接 deep_merge 进 existing）
    - errors:    被拒字段的人类可读说明（未知字段、类型不匹配）

    设计取舍：errors 不阻断保存，合法部分仍写入。控制面板的开关大半是
    勾选框，单字段错不该让整次保存失败 —— 让 caller 在 response 里
    把 errors 回传给前端，由 UI 决定怎么提示。
    """
    if not isinstance(data, dict):
        return {}, ["顶层必须是 JSON 对象"]
    flat = _flatten(data)
    clean: dict = {}
    errors: list[str] = []
    for path, val in flat.items():
        expected = SCHEMA.get(path)
        if expected is None:
            errors.append(f"未知字段 {path}")
            continue
        # bool 是 int 的子类，必须显式区分（不然 True 会被当 int 通过 str/int 校验）
        if expected is bool:
            if not isinstance(val, bool):
                errors.append(f"{path} 期望 bool，得到 {type(val).__name__}")
                continue
        elif isinstance(val, bool):
            errors.append(f"{path} 期望 {expected.__name__}，得到 bool")
            continue
        elif not isinstance(val, expected):
            errors.append(f"{path} 期望 {expected.__name__}，得到 {type(val).__name__}")
            continue
        clean[path] = val
    return _unflatten(clean), errors
