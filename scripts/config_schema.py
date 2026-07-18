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
    "ai.claude_cli.model":             str,
    "ai.claude_cli.effort":            str,
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
    "daily.enabled":                   bool,   # 总开关:整套凌晨 daily 跑不跑(Pi 的 daily_anki_status.py 顶部读)
    "scheduled_register.enabled":      bool,
    "scheduled_register.time":         str,
    "scheduled_register.wake_anki":    bool,
    "scheduled_register.upload_after": bool,

    # 薄弱卡 AI 改写（凌晨）。数字项用 str：控制面板非 bool 字段是
    # text input，提交即字符串；daily 端 int() 容错转换。
    "weak_card_refresh.enabled":         bool,
    "weak_card_refresh.auto_escalate":   bool,
    "weak_card_refresh.min_lapses":      str,
    "weak_card_refresh.limit":           str,
    "weak_card_refresh.cooldown_days":   str,
    "weak_card_refresh.escalate_lapses": str,

    # 停用词治理(通用语统计 + 复活赛,daily 内跑)
    "stopword_gov.enabled":              bool,
    "stopword_gov.ai_judge":             bool,

    # 已掌握卡换问法（防模式记忆）
    "card_antimodel.enabled":            bool,
    "card_antimodel.min_stability_days": str,
    "card_antimodel.min_reps":           str,
    "card_antimodel.limit":              str,
    "card_antimodel.cooldown_days":      str,

    # 卡片质量体检
    "card_quality.enabled":              bool,
    "card_quality.auto_split":           bool,
    "card_quality.relative_threshold":   bool,
    "card_quality.max_back_len":         str,
    "card_quality.hard_again_ratio":     str,
    "card_quality.min_reviews":          str,
    "card_quality.sample_per_run":       str,
    "card_quality.limit":                str,
    "card_quality.cooldown_days":        str,

    # 卡片 QA 改进（从 Anki 卡片「问 AI / 改进这张卡」链接进入截图问答页的改卡流程）
    "card_qa.delete_original":           bool,

    # KG 节点审查（每本书可单独开关 + 增量只审变动节点）
    "kg_audit.enabled":                  bool,   # 全局总开关
    "kg_audit.incremental":              bool,   # 只审新增/改过的节点
    "kg_audit.default":                  bool,   # 未列出的书默认开/关
    "kg_audit.books.*":                  bool,   # 每本书一个开关（* 通配，书名任意）
}


# UI 元数据：控制面板「设置」panel 显示的字段（顺序 = 渲染顺序，group = 分组）。
# 不在这里的 SCHEMA 字段仍然走校验（例如 AI 后端 cli command），只是不在 UI 显示。
FIELD_META: dict[str, dict] = {
    "ai.claude_cli.model": {
        "group": "AI 后端（Claude CLI）",
        "label": "模型（留空=默认；可填 opus / sonnet / 或完整模型名如 claude-opus-4-7）",
    },
    "ai.claude_cli.effort": {
        "group": "AI 后端（Claude CLI）",
        "label": "思考深度 effort（留空=默认；low / medium / high / xhigh / max）",
    },
    "anki.auto_restart": {
        "group": "Anki",
        "label": "AnkiConnect 不可达时自动重启 anki-headless",
    },
    "auto_upload_after_register": {
        "group": "笔记登记",
        "label": "登记完成后自动「刷新并上传网页」",
    },
    "daily.enabled": {
        "group": "凌晨定时",
        "label": "★ 总开关:启用每日凌晨任务(关掉则整套 daily 不跑;timer 仍触发但脚本立即空跑退出)",
    },
    "scheduled_register.wake_anki": {
        "group": "凌晨定时",
        "label": "触发前确保 Anki 可用",
    },
    "scheduled_register.upload_after": {
        "group": "凌晨定时",
        "label": "完成后部署 dashboard",
    },
    "weak_card_refresh.enabled": {
        "group": "薄弱卡改写",
        "label": "启用：凌晨对 leech/反复记不住的卡 AI 重写问法（原地改，不破坏 FSRS）",
    },
    "weak_card_refresh.min_lapses": {
        "group": "薄弱卡改写",
        "label": "失败次数阈值（lapses ≥ 此值算待改写，默认 3）",
    },
    "weak_card_refresh.limit": {
        "group": "薄弱卡改写",
        "label": "每晚最多处理张数（默认 5）",
    },
    "weak_card_refresh.cooldown_days": {
        "group": "薄弱卡改写",
        "label": "改写后冷却天数（期间不再动，让 FSRS 重新稳定，默认 30）",
    },
    "weak_card_refresh.escalate_lapses": {
        "group": "薄弱卡改写",
        "label": "改写后再失败几次→升级拆卡/删卡（默认 2）",
    },
    "weak_card_refresh.auto_escalate": {
        "group": "薄弱卡改写",
        "label": "凌晨自动执行拆/删（L2，破坏性！不勾则只在日志给建议、需手动确认）",
    },
    "stopword_gov.enabled": {
        "group": "停用词治理",
        "label": "启用：凌晨统计书库通用语 + 复活赛（关掉则跳过整套,词表冻结在当前状态）",
    },
    "stopword_gov.ai_judge": {
        "group": "停用词治理",
        "label": "允许调用 AI 裁决（关掉则只积累候选/滞留计时,零 AI 消耗;默认开）",
    },
    "card_antimodel.enabled": {
        "group": "已掌握卡换问法",
        "label": "启用：对已掌握(久经复习)的卡 AI 换角度重问，防只记问法不懂（原地，语义不变）",
    },
    "card_antimodel.min_stability_days": {
        "group": "已掌握卡换问法",
        "label": "FSRS stability 阈值天（≥此值算已稳固，默认 60）",
    },
    "card_antimodel.min_reps": {
        "group": "已掌握卡换问法",
        "label": "最少复习次数（reps ≥ 此值才换，默认 5）",
    },
    "card_antimodel.limit": {
        "group": "已掌握卡换问法",
        "label": "每晚最多处理张数（默认 5）",
    },
    "card_antimodel.cooldown_days": {
        "group": "已掌握卡换问法",
        "label": "换问法后冷却天数（已掌握别太勤换，默认 90）",
    },
    "card_quality.enabled": {
        "group": "卡片质量体检",
        "label": "启用：全量扫低质卡（答案过长/多知识点/指代不清）AI 评分后原地优化",
    },
    "card_quality.relative_threshold": {
        "group": "卡片质量体检",
        "label": "过长阈值用同类型卡 P85 相对值（关掉则用下面绝对字数）",
    },
    "card_quality.max_back_len": {
        "group": "卡片质量体检",
        "label": "答案绝对长度阈值（相对模式下作下限保护，默认 280）",
    },
    "card_quality.hard_again_ratio": {
        "group": "卡片质量体检",
        "label": "again+hard 占比超此值算「难用」候选（行为信号，默认 0.4）",
    },
    "card_quality.min_reviews": {
        "group": "卡片质量体检",
        "label": "行为信号至少需多少次复习才采信（默认 4）",
    },
    "card_quality.sample_per_run": {
        "group": "卡片质量体检",
        "label": "每晚额外随机抽几张绕过启发式（盲区安全网，默认 3，0=关）",
    },
    "card_quality.limit": {
        "group": "卡片质量体检",
        "label": "每晚最多处理张数（默认 5）",
    },
    "card_quality.cooldown_days": {
        "group": "卡片质量体检",
        "label": "优化后冷却天数（默认 45）",
    },
    "card_quality.auto_split": {
        "group": "卡片质量体检",
        "label": "AI 判定「该拆」时凌晨自动拆（破坏性！不勾则只建议）",
    },
    "qa_remote_daemon": {
        "group": "iPad 截图问答",
        "label": "启用 iPad 远程截图问答",
    },
    "qa_exercises_subdir": {
        "group": "iPad 截图问答",
        "label": "习题子目录",
    },
    "qa_wrong_subdir": {
        "group": "iPad 截图问答",
        "label": "错题子目录",
    },
    "card_qa.delete_original": {
        "group": "卡片 QA 改进",
        "label": "「根据此修改 Anki」生成新卡后删除原卡（不勾＝保留原卡，默认；保留可不丢 FSRS 历史）",
    },
}


def schema_for_ui() -> list[dict]:
    """给前端渲染设置 panel 用：每条 {path, type, group, label}。

    顺序遵循 FIELD_META，type 来自 SCHEMA（bool → checkbox / str → text input）。
    """
    out: list[dict] = []
    for path, meta in FIELD_META.items():
        if path not in SCHEMA:
            # 防 drift：FIELD_META 配了但 SCHEMA 没声明的字段直接跳过
            continue
        out.append({
            "path":  path,
            "type":  SCHEMA[path].__name__,    # 'bool' / 'str'
            "group": meta["group"],
            "label": meta["label"],
        })
    return out


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
            # 通配:把最后一段换成 * 再查一次(支持 kg_audit.books.<任意书名> 这种动态键)
            head = path.rsplit(".", 1)[0]
            if "." in path:
                expected = SCHEMA.get(head + ".*")
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
