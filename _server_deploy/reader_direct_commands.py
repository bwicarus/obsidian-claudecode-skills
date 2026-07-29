#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 AI 直接命令服务(任务书 A4)。**纯增量**:不改写、不接管任何既有路径。

设计约束(来自任务书九节与 A4):
- 只调用**确定性底层能力**。任何执行期会再次调用 AI 的工具都不在白名单里
  (见 `references/reader-agent-capability-audit.md` §1.1 的 23 个)——上游助手负责认知,
  这里只负责执行。
- 独立单步:接收即异步执行,**成功静默**;失败才产生结构化失败事件。
- 依赖多步:只有"下一步真的需要上一步结果"时才逐步返回并等待;任一步失败停止整链。
- 失败事件进可订阅队列,带语音任务关联与命令编号,供 Windows 监听器消费。

命令 envelope 与回执格式见本文件常量与 `_result()`;不返回聊天文本,不调用其它 AI。
"""
from __future__ import annotations

import json
import threading
import time
import uuid

CONTRACT = "reader-direct-command/1"

# ── 确定性动作白名单 ──────────────────────────────────────────────────────────
# 每项:(需要的目标字段, 说明)。**新增动作前先确认它执行期不调 AI**。
ACTIONS: dict[str, dict] = {
    "read.page":        {"target": ("file", "page"), "desc": "取页正文(字符层/章节段落)"},
    "read.selection":   {"target": (),               "desc": "取当前选区(无选区返回空,不沿用旧值)"},
    "read.pageimage":   {"target": ("file", "page"), "desc": "取页图元数据(视觉判断仍归上游)"},
    "nav.goto":         {"target": ("file", "page"), "desc": "跳页"},
    "nav.open":         {"target": ("file",),        "desc": "打开书"},
    "toc.get":          {"target": ("file",),        "desc": "取目录"},
    "search.book":      {"target": ("file",),        "desc": "书内检索"},
    "search.all":       {"target": (),               "desc": "全库检索"},
    "dict.lookup":      {"target": (),               "desc": "离线词典查词"},
    "highlight.create": {"target": ("file",),        "desc": "创建高亮(正文原位)"},
    "highlight.list":   {"target": ("file",),        "desc": "列出高亮"},
    "note.create":      {"target": ("file",),        "desc": "创建便签"},
    "note.list":        {"target": ("file",),        "desc": "列出便签"},
    "page.new":         {"target": ("file",),        "desc": "新建插入页,返回锚点"},
    "page.add":         {"target": ("file",),        "desc": "向插入页写元素"},
    "anki.draft":       {"target": (),               "desc": "提交制卡草稿批"},
    # ↓ 2026-07-29 第八节迁移:上游自带联网与推理,只补它**拿不到的本地数据**。
    #   联网类(搜图/搜视频/天气/新闻)一律不接线 —— 上游自查更直接。
    "section.read":     {"target": ("file", "page"), "desc": "取该页所在整章正文(summarize_section 的确定性那一半;总结归上游)"},
    "vocab.add":        {"target": (),               "desc": "加生词(ECDICT/unidic 确定性词典 + 写 vault)"},
    "recall.creation":  {"target": (),               "desc": "召回本地创造物注册表(纸/报告/搜索/翻译等的句柄与内容;引用型只回 ref 不解引用)"},
    "recall.notes":     {"target": (),               "desc": "召回已学内容(知识索引/已学 KG 节点/Anki);query 必填,不扫 raw vault、不联网、不调 AI"},
}
MODES = ("independent", "dependent")
_MAX_STEPS = 20


class CommandError(ValueError):
    """命令不合规。消息里必须能看出是哪个字段。"""


def validate(cmd: dict) -> dict:
    """校验并规范化一条命令。字段:correlation/target/anchor/params/idempotency/
    dependencies/precondition/mode/steps。"""
    if not isinstance(cmd, dict):
        raise CommandError("命令必须是对象")
    if str(cmd.get("contract") or CONTRACT) != CONTRACT:
        raise CommandError(f"contract 必须是 {CONTRACT}")
    corr = str(cmd.get("correlation") or "").strip()[:64]
    if not corr:
        raise CommandError("correlation 必填(命令编号,用于回执关联与去重)")
    mode = str(cmd.get("mode") or "independent")
    if mode not in MODES:
        raise CommandError(f"mode 必须是 {MODES}")
    steps = cmd.get("steps")
    if steps is None:
        steps = [{k: cmd.get(k) for k in ("action", "anchor", "params") if cmd.get(k) is not None}]
    if not isinstance(steps, list) or not steps:
        raise CommandError("steps 必须是非空数组(单步也写成一条)")
    if len(steps) > _MAX_STEPS:
        raise CommandError(f"steps 超过上限 {_MAX_STEPS}")
    norm_steps = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise CommandError(f"steps[{i}] 必须是对象")
        act = str(st.get("action") or "").strip()
        if act not in ACTIONS:
            raise CommandError(f"steps[{i}].action={act!r} 不在确定性白名单;"
                               f"可用:{sorted(ACTIONS)}")
        anchor = st.get("anchor") or {}
        if not isinstance(anchor, dict):
            raise CommandError(f"steps[{i}].anchor 必须是对象")
        for need in ACTIONS[act]["target"]:
            if anchor.get(need) in (None, ""):
                raise CommandError(f"steps[{i}]({act}) 缺 anchor.{need}")
        params = st.get("params") or {}
        if not isinstance(params, dict):
            raise CommandError(f"steps[{i}].params 必须是对象")
        one = {"action": act, "anchor": anchor, "params": params}
        if st.get("idempotency"):
            one["idempotency"] = str(st["idempotency"])[:80]
        if st.get("precondition") is not None:
            if not isinstance(st["precondition"], dict):
                raise CommandError(f"steps[{i}].precondition 必须是对象")
            one["precondition"] = st["precondition"]
        norm_steps.append(one)
    out = {"contract": CONTRACT, "correlation": corr, "mode": mode, "steps": norm_steps}
    for k in ("voiceTask", "idempotency"):
        if cmd.get(k):
            out[k] = str(cmd[k])[:80]
    deps = cmd.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            raise CommandError("dependencies 必须是数组")
        out["dependencies"] = [str(x)[:64] for x in deps[:20]]
    return out


def _result(corr: str, ok: bool, steps: list, *, error: str | None = None,
             retryable: bool | None = None, command_id: str = "", task_id: str = "") -> dict:
    """统一回执:命令编号 / 语音任务 / 关联编号 / 成败 / 结果或错误 / 可否重试 / 子步骤状态。

    三个编号各有用途,不可互相替代:
      commandId   —— 本次提交的唯一身份(服务端签发),用于日志与去重排查;
      taskId      —— 语音任务关联,失败事件据此路由回正确的对话,不串台;
      correlation —— 调用方自带的关联编号,回执按它对回原请求。
    """
    r = {"contract": CONTRACT, "commandId": command_id, "taskId": task_id,
         "correlation": corr, "ok": ok, "steps": steps}
    if error:
        r["error"] = error
        r["retryable"] = bool(retryable)
    return r


class FailureBus:
    """失败事件队列。**只装失败**——独立单步成功不产生噪声事件(任务书 A4)。

    订阅者(Windows 监听器)按 voiceTask/correlation 路由;事件保留有限条数,
    避免长时间无人消费时无限增长。
    """

    def __init__(self, keep: int = 200):
        self._q: list[dict] = []
        self._keep = keep
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, corr: str, error: str, *, voice_task: str = "", step: int | None = None,
             retryable: bool = False, command_id: str = "") -> dict:
        with self._lock:
            self._seq += 1
            ev = {"contract": CONTRACT, "type": "command-failed", "seq": self._seq,
                  "commandId": command_id, "correlation": corr, "voiceTask": voice_task,
                  "error": error,
                  "step": step, "retryable": retryable, "ts": int(time.time())}
            self._q.append(ev)
            if len(self._q) > self._keep:
                del self._q[: len(self._q) - self._keep]
            return ev

    def since(self, seq: int = 0, voice_task: str = "") -> list[dict]:
        with self._lock:
            out = [e for e in self._q if e["seq"] > seq]
        if voice_task:
            out = [e for e in out if e.get("voiceTask") == voice_task]
        return out

    def cursor(self) -> int:
        with self._lock:
            return self._seq


class DirectCommandService:
    """执行器。`handlers` 由宿主注入(每个 action → 确定性可调用),本模块**不 import 阅读器**,
    因此可在隔离环境里用假 handler 完整验证协议与执行模式。"""

    def __init__(self, handlers: dict, bus: FailureBus | None = None, seen_keep: int = 500):
        self.handlers = handlers
        self.bus = bus or FailureBus()
        self._seen: dict[str, dict] = {}
        self._seen_order: list[str] = []
        self._seen_keep = seen_keep
        self._lock = threading.Lock()

    # ── 幂等 ──────────────────────────────────────────────────────────────
    def _remember(self, key: str, val: dict) -> None:
        with self._lock:
            if key not in self._seen:
                self._seen_order.append(key)
            self._seen[key] = val
            while len(self._seen_order) > self._seen_keep:
                self._seen.pop(self._seen_order.pop(0), None)

    def _recall(self, key: str):
        with self._lock:
            return self._seen.get(key)

    # ── 执行 ──────────────────────────────────────────────────────────────
    def submit(self, raw: dict) -> dict:
        cmd = validate(raw)
        corr, mode = cmd["correlation"], cmd["mode"]
        vt = cmd.get("voiceTask", "")
        cid = "cmd_" + uuid.uuid4().hex[:12]      # 服务端签发,调用方不得伪造
        ikey = cmd.get("idempotency") or corr
        prev = self._recall(ikey)
        if prev is not None:
            return dict(prev, replayed=True)

        steps_out, prev_result = [], None
        for i, st in enumerate(cmd["steps"]):
            pre = st.get("precondition")
            if pre and not self._check_pre(pre, prev_result):
                err = f"steps[{i}] 前置条件不满足:{json.dumps(pre, ensure_ascii=False)}"
                steps_out.append({"i": i, "action": st["action"], "ok": False, "error": err})
                res = _result(corr, False, steps_out, error=err, retryable=False, command_id=cid, task_id=vt)
                self.bus.emit(corr, err, voice_task=vt, step=i, retryable=False, command_id=cid)
                self._remember(ikey, res)
                return res
            fn = self.handlers.get(st["action"])
            if fn is None:
                err = f"steps[{i}].action={st['action']} 未注册处理器"
                steps_out.append({"i": i, "action": st["action"], "ok": False, "error": err})
                res = _result(corr, False, steps_out, error=err, retryable=False, command_id=cid, task_id=vt)
                self.bus.emit(corr, err, voice_task=vt, step=i, retryable=False, command_id=cid)
                self._remember(ikey, res)
                return res
            try:
                # 依赖模式把上一步结果传下去;独立模式各步互不可见
                data = fn(st["anchor"], st["params"],
                          prev_result if mode == "dependent" else None)
                steps_out.append({"i": i, "action": st["action"], "ok": True, "data": data})
                prev_result = data
            except Exception as ex:                       # noqa: BLE001 —— 执行器要吞异常转事件
                err = f"{type(ex).__name__}: {str(ex)[:200]}"
                steps_out.append({"i": i, "action": st["action"], "ok": False, "error": err})
                retryable = not isinstance(ex, (CommandError, ValueError))
                res = _result(corr, False, steps_out, error=err, retryable=retryable, command_id=cid, task_id=vt)
                # 依赖链失败即停;独立模式也停(同一命令内的步骤属同一意图)
                self.bus.emit(corr, err, voice_task=vt, step=i, retryable=retryable, command_id=cid)
                self._remember(ikey, res)
                return res
        res = _result(corr, True, steps_out, command_id=cid, task_id=vt)
        self._remember(ikey, res)
        return res

    @staticmethod
    def _check_pre(pre: dict, prev) -> bool:
        """前置条件:目前支持 {'prevOk': True} 与 {'equals': {'field': value}}(对上一步结果)。"""
        if pre.get("prevOk") and not isinstance(prev, dict):
            return False
        eq = pre.get("equals")
        if isinstance(eq, dict):
            if not isinstance(prev, dict):
                return False
            for k, v in eq.items():
                if prev.get(k) != v:
                    return False
        return True
