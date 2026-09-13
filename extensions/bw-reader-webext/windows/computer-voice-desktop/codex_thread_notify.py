"""codex_thread_notify — 不靠钩子，直接把通知送进 Codex 的对话（2026-09-10）。

**为什么要有这条路。**
原来送通知只有一条路:钩子在 `SessionStart` / `UserPromptSubmit` 时把
(管道, 线程 id) 登记给桥,桥再往那条 per-thread 管道推。它的毛病是**登记时机
不由我们决定** —— Codex 起来后没跟它说过话,就没有绑定;而"想开语音"恰恰常常
就发生在那种时候。用户原话:「我们可以不用钩子进行绑定就能开启通道,只需要找到
一个文字的对话或者语音对话都行然后让他登记主动通知就行」。

**这条路怎么走。**
`codex app-server` 是同一个 CLI 二进制的长驻协议入口(stdio 上的 JSON-RPC)。
它提供 `thread/list`(列出对话,带 id)、`thread/resume`、`turn/start`(往对话里
送输入)。于是:列出最近的对话 → 挑一个 → 送一条 turn。全程不需要钩子,也不需要
那条 per-thread 管道。

⚠ **这不是"绕过 Codex 自己决定"** —— 送进去的仍然是一条通知,由它读、由它按
能力说明决定做什么。区别只在**送达方式**。

⚠ **一次 turn 要花订阅额度**,所以调用方必须先确认"确实需要开语音"
(台账说没在通话、保活也没在负责),别把它当轮询用。

⚠ **审批**:headless 的 turn 里跑命令会触发审批请求。我们**自己应答**,不改用户
的全局配置 —— 只放行这条链要用的那几个脚本,别的一律拒绝并说明原因。
"""
from __future__ import annotations

import json
from datetime import datetime
import re
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

#: 只放行这条链自己的脚本。审批一旦写成"什么都同意"，这个入口就成了
#: 任意命令执行 —— 而它是从一条通知触发的。
ALLOWED_SCRIPTS = ("voice_start_step.py", "voice_start_failed.py",
                   "voice_status_receipt.py", "codex_push_register.py")

#: 不能拿来送通知的线程类型。跟登记钩子同一套理由：定时任务与子任务
#: 不该被塞进这种指令，也不该代表用户。
#:
#: ⚠ **取值要从会话记录里读，不能用 `thread/list` 回的 `threadSource`**
#: （2026-09-10 实测）：那个字段回的是**客户端**（25 条全是 'vscode'），
#: 不是会话来源。拿它做排除表是空转 —— 永远不匹配，而空转的排除跟没有排除
#: 在行为上一样，只是看起来像有。
EXCLUDED_SOURCES = frozenset({"subagent", "automation"})


def thread_source_of(thread_id: str, home: Path | None = None) -> str | None:
    """从会话记录里读这条线程的来源。读不到返回 None（不知道，不是"没问题"）。"""
    base = home or Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    try:
        for path in (base / "sessions").rglob("rollout-*%s.jsonl" % thread_id):
            with path.open(encoding="utf-8-sig") as source:
                entry = json.loads(source.readline(1024 * 1024))
            if entry.get("type") != "session_meta":
                return None
            return (entry.get("payload") or {}).get("thread_source")
    except (OSError, ValueError, TypeError):
        return None
    return None

STARTUP_TIMEOUT_SECONDS = 40.0
TURN_TIMEOUT_SECONDS = 180.0


class NotifyError(RuntimeError):
    """这一带的失败一律带原因，不折成布尔。"""


def codex_entry() -> list[str]:
    """找到能直接 Popen 的 codex 入口。

    ⚠ PATH 上的 `codex` 在 Windows 是 `.cmd` 外壳，CreateProcess 起不了它
    （实测 WinError 2）。真正的入口是 `node …/@openai/codex/bin/codex.js`。
    """
    node = shutil.which("node")
    if node:
        appdata = os.environ.get("APPDATA")
        if appdata:
            entry = (Path(appdata) / "npm" / "node_modules" / "@openai"
                     / "codex" / "bin" / "codex.js")
            if entry.is_file():
                return [node, str(entry)]
    direct = shutil.which("codex.exe")
    if direct:
        return [direct]
    raise NotifyError("找不到可直接启动的 codex 入口（node + codex.js 都不在）")


class AppServer:
    """一次性的 app-server 会话。用完就关。"""

    def __init__(self, entry: list[str] | None = None) -> None:
        self._proc = subprocess.Popen(
            (entry or codex_entry()) + ["app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._id = 0
        self._stderr: list[str] = []
        threading.Thread(
            target=lambda: self._stderr.extend(self._proc.stderr or []),
            daemon=True).start()

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
        except OSError:
            pass

    def __enter__(self) -> "AppServer":
        return self

    def __exit__(self, *_) -> bool:
        self.close()
        return False

    def _write(self, message: dict) -> None:
        if not self._proc.stdin:
            raise NotifyError("app-server 的 stdin 不可用")
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method: str, params: dict | None = None,
                timeout: float = STARTUP_TIMEOUT_SECONDS,
                on_server_request=None) -> dict:
        """发一个请求并等它的回应。

        ⚠ 等待期间可能夹着**服务端反向请求**（最典型的是审批）。不应答它，
        turn 会一直挂着 —— 而挂着跟"还在想"在外面看长得一样。所以这里把
        它们交给 on_server_request 处理，处理不了就明确拒绝。
        """
        self._id += 1
        mine = self._id
        self._write({"jsonrpc": "2.0", "id": mine,
                     "method": method, **({"params": params}
                                          if params is not None else {})})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._proc.stdout:
                raise NotifyError("app-server 的 stdout 不可用")
            line = self._proc.stdout.readline()
            if not line:
                raise NotifyError(
                    "app-server 提前退出：%s"
                    % "".join(self._stderr[-4:])[:300])
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == mine and (
                "result" in message or "error" in message
            ):
                if "error" in message:
                    raise NotifyError(
                        "%s 失败：%s" % (method, json.dumps(
                            message["error"], ensure_ascii=False)[:200]))
                return message.get("result") or {}
            if "method" in message and "id" in message:
                # 服务端反向请求（审批之类）。
                #
                # ⚠ 认不出来的**回协议错误，不编一个结果**（2026-09-10 改）。
                # 上一版给所有反向请求一律回 `{"decision":"denied"}` —— 那个
                # 取值连审批都不对（现代方法要 accept/decline），更别说
                # `account/chatgptAuthTokens/refresh` 这种根本不是审批的。
                # 拿一个形状不对的结果去应答，对面只能当协议错误处理，
                # 而表现就是"turn 送到了却什么都没发生"。
                reply = (on_server_request(message)
                         if on_server_request else None)
                if reply is None:
                    self._write({
                        "jsonrpc": "2.0", "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "这个客户端不处理 %s" % message["method"],
                        }})
                else:
                    self._write({"jsonrpc": "2.0", "id": message["id"],
                                 "result": reply})
        raise NotifyError("%s 超时（%.0f 秒）" % (method, timeout))


    def run_turn(self, thread_id: str, text: str,
                 timeout: float = TURN_TIMEOUT_SECONDS) -> dict:
        """起一轮并等它跑完。返回途中看到的证据。

        ⚠ 只等 `turn/start` 的回应是不够的：那是"接受"，不是"完成"。真正的
        终点是 `turn/completed`；途中的 `item/commandExecution/*` 才说明
        它确实去跑脚本了。没有这些证据就说 ok，等于又造一个说谎的读数。
        """
        self._id += 1
        mine = self._id
        self._write({"jsonrpc": "2.0", "id": mine, "method": "turn/start",
                     "params": {"threadId": thread_id,
                                "input": [{"type": "text", "text": text}]}})
        commands: list[str] = []
        errors: list[str] = []
        accepted = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._proc.stdout:
                raise NotifyError("app-server 的 stdout 不可用")
            line = self._proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            method = str(message.get("method") or "")
            if message.get("id") == mine and (
                "result" in message or "error" in message
            ):
                if "error" in message:
                    raise NotifyError("turn/start 失败：%s" % json.dumps(
                        message["error"], ensure_ascii=False)[:200])
                accepted = True
                continue
            if "id" in message and method:
                reply = approve(message)
                if reply is None:
                    self._write({"jsonrpc": "2.0", "id": message["id"],
                                 "error": {"code": -32601,
                                           "message": "不处理 " + method}})
                else:
                    self._write({"jsonrpc": "2.0", "id": message["id"],
                                 "result": reply})
                continue
            if method.startswith("item/commandExecution/"):
                blob = json.dumps(message.get("params") or {},
                                  ensure_ascii=False)
                for name in ALLOWED_SCRIPTS:
                    if name in blob and name not in commands:
                        commands.append(name)
            elif method == "error":
                errors.append(json.dumps(message.get("params") or {},
                                         ensure_ascii=False)[:160])
            elif method == "turn/completed":
                return {"completed": True, "accepted": accepted,
                        "commands": commands, "errors": errors}
        return {"completed": False, "accepted": accepted,
                "commands": commands, "errors": errors,
                "detail": "没等到 turn/completed（%.0f 秒）" % timeout}


#: 能代表用户、可以被送指令的会话来源。
#:
#: ⚠ 用**白名单**而不是黑名单：`thread_source` 还可能出现没见过的取值，
#: 而"没见过"不该默认可用 —— 定时任务与子任务被误选中的代价是把指令塞进
#: 别人的工作流。
ALLOWED_SOURCES = frozenset({"voice_chat", "user", "realtime_voice"})


def recent_threads(home: Path | None = None,
                   limit: int = 12) -> list[tuple[str, str, float]]:
    """从**磁盘上的会话记录**列出最近的可送达对话，最新在前。

    返回 [(线程 id, 来源, 最后写入时间), …]。

    ⚠ **不用 `thread/list`**（2026-09-10 实测）：它既不按时间排序，也不把最近的
    给全 —— 拿到 40 条里最新的是前一天，当天的一条都不在里面（有 nextCursor，
    只是一页）。按它的顺序取"第一条"当最新是个错的假设。磁盘记录才是事实：
    文件名带线程 id，首行 session_meta 带 thread_source，最后写入时间就是
    最近活动时间。

    ⚠ 要**一串**而不是一条：最新那条常常正被 Codex App 占着写，
    `thread/resume` 会报 "already has an active writer"。那不是失败，
    只是说这条不能由我们来写 —— 往下找一条就好。
    """
    base = (home or Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))) / "sessions"
    rows: list[tuple[float, str, str]] = []
    for path in base.rglob("rollout-*.jsonl"):
        try:
            when = path.stat().st_mtime
            with path.open(encoding="utf-8-sig") as source:
                entry = json.loads(source.readline(1024 * 1024))
        except (OSError, ValueError):
            continue
        if entry.get("type") != "session_meta":
            continue
        meta = entry.get("payload") or {}
        if meta.get("thread_source") not in ALLOWED_SOURCES:
            continue
        thread_id = meta.get("id") or meta.get("session_id")
        if thread_id:
            rows.append((when, str(thread_id), str(meta.get("thread_source"))))
    rows.sort(reverse=True)
    # 2026-09-13（用户拍板）：所有绑定都在"语音真的开了 + 找到语音所在对话"之后。
    # 同步器按证据找到的线程写在绑定文件里，送指令先送它；找不到才按磁盘时间猜。
    bound = bound_voice_thread(base.parent)
    if bound:
        rows = [(time.time(), bound, "binding")] + [r for r in rows if r[1] != bound]
    if not rows:
        raise NotifyError("找不到可送达的对话（没有 %s 这几类会话记录）"
                          % "/".join(sorted(ALLOWED_SOURCES)))
    return [(tid, src, when) for when, tid, src in rows[:limit]]


#: 同步器写的绑定文件（voice_conversation_sync.write_binding）。这里不 import 那个模块：
#: 本脚本被单独拷到 %LOCALAPPDATA%\BWReader 下独立运行，多一个 import 就多一处会断的依赖。
BINDING_CONTRACT = "reader-voice-thread-binding/1"
BINDING_MAX_AGE_SECONDS = 24 * 3600


def bound_voice_thread(codex_home: Path) -> str | None:
    """读绑定文件里的线程；contract 不对、不像 uuid、超过一天 → None。"""
    path = codex_home / "voice-thread-binding.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("contract") != BINDING_CONTRACT:
        return None
    thread_id = value.get("threadId")
    if not isinstance(thread_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", thread_id):
        return None
    try:
        bound_at = datetime.fromisoformat(str(value.get("boundAtUtc"))).timestamp()
    except (TypeError, ValueError):
        return None
    if time.time() - bound_at > BINDING_MAX_AGE_SECONDS:
        return None
    return thread_id


#: `thread/resume` 说"这条正被别人写"时的原话片段。
ACTIVE_WRITER = "active writer"


#: 审批方法 → 该用哪套取值。**两套不通用**（2026-09-10 从官方 schema 取的）：
#: 现代 `item/*/requestApproval` 要 accept/acceptForSession/decline/cancel；
#: 旧的 execCommandApproval / applyPatchApproval 要 approved/denied。
#: 上一版一律回 "approved"，对现代方法而言是非法值 —— 表现就是
#: "turn 送到了却什么都没发生"。
APPROVAL_VOCABULARY = {
    "item/commandExecution/requestApproval": ("accept", "decline"),
    "item/fileChange/requestApproval": ("accept", "decline"),
    "item/permissions/requestApproval": ("accept", "decline"),
    "execCommandApproval": ("approved", "denied"),
    "applyPatchApproval": ("approved", "denied"),
}


def approve(message: dict) -> dict | None:
    """审批应答：只放行这条链自己的脚本。

    ⚠ 写成"什么都同意"就等于把一条通知变成任意命令执行入口。宁可拒绝，
    并让拒绝的原因出现在 turn 里 —— 那样至少说得清为什么没做成。

    ⚠ 不认识的反向请求返回 None，由调用方回协议错误 —— **不要**替它编一个
    结果。编出来的结果形状多半不对，而对面只能当协议错误处理。
    """
    yes_no = APPROVAL_VOCABULARY.get(str(message.get("method") or ""))
    if yes_no is None:
        return None
    blob = json.dumps(message.get("params") or {}, ensure_ascii=False)
    allowed = any(name in blob for name in ALLOWED_SCRIPTS)
    return {"decision": yes_no[0] if allowed else yes_no[1]}


def send(text: str, *, thread_id: str | None = None,
         entry: list[str] | None = None,
         turn_timeout: float = TURN_TIMEOUT_SECONDS) -> dict:
    """把一条通知送进 Codex 的对话，并**等这一轮真的跑完**。

    ⚠ `turn/start` 返回只代表"这一轮被接受了"，不代表跑完、更不代表脚本执行了
    （2026-09-10：账本里记成 ok=True，而实际上什么都没发生）。这跟"按了不等于
    关了"是同一个毛病。所以这里等 `turn/completed`，并把途中看到的命令执行
    记下来 —— 那才是"它到底做了没有"的证据。
    """
    with AppServer(entry) as server:
        server.request("initialize", {
            "clientInfo": {"name": "bw-reader-voice-entry", "version": "1"},
        })
        server.notify("initialized")
        candidates = ([(thread_id, "", 0.0)] if thread_id
                      else recent_threads())
        skipped: list[str] = []
        for target, source, _when in candidates:
            try:
                server.request("thread/resume", {"threadId": target})
            except NotifyError as error:
                # 被 App 占着写 → 换下一条；别的错才是真失败。
                if ACTIVE_WRITER in str(error):
                    skipped.append("%s（%s，App 正开着）" % (target[:13], source))
                    continue
                raise
            seen = server.run_turn(target, text, timeout=turn_timeout)
            return {"ok": seen["completed"], "threadId": target,
                    "source": source, "skipped": skipped, **seen}
        raise NotifyError(
            "最近 %d 条对话都连不上（都被 App 占着写）：%s"
            % (len(candidates), "; ".join(skipped)))


#: 指定操作那条通知的正文。
#:
#: ⚠ **不叙述用途**（用户 2026-09-09 与 09-10 两次点明）：这是一条运维指令，
#: 不是一段说明文。脚本自己带守卫（已在通话中不动作、台账读不到不动作、
#: 冷却期内不动作），所以对面不需要先判断什么，照着跑就行。
#: 开口纪律。⚠ 与 C# 的 ReaderCodexPush.OperationSilenceLine **必须一致** ——
#: 同一条纪律的第二份实现，测试钉住了两边的措辞。
#:
#: 2026-09-10 用户实录：起语音和板面更新，对面都直接语音念了出来
#: （「这次也没问题,已经确认成功了,不用重试。」）。原因不是它多话，是我们
#: 一个字都没说过该不该开口 —— 而原文还字面写着「并回报它打印的 confirmed
#: 值」，在通话里"回报"就是说出来，等于我们自己点的那句噪音。
OPERATION_SILENCE_LINE = (
    "这是后台运维指令，不是对话：照做即可，"
    "**全程不要在通话里提这件事，也不要念出任何结果**。\n"
)

VOICE_ENTRY_TEXT = (
    OPERATION_SILENCE_LINE
    + "指定操作（requestId: %s）。\n"
    "\n"
    "请先运行这一行（失败不要停，继续下一步）：\n"
    '    python "%%LOCALAPPDATA%%\\BWReader\\codex_push_register.py" --enable\n'
    "\n"
    "再运行这一行**一次**，它自己会重试并在放弃时上报，你不用再做别的：\n"
    '    python "%%LOCALAPPDATA%%\\BWReader\\voice_start_step.py"'
    " --attempts 2 --report-failure\n"
    "用 exec_command 起它，yield_time_ms 给 1500，返回后**就当做完了**：\n"
    "不要 wait、不要 write_stdin 查看、不要再运行第二次 —— 继续跟用户说话。\n"
    "它自己会重试，放弃时会自己上报，你不需要它的结果。\n"
    "同一编号再次出现表示上一次没有生效。"
)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="把通知直接送进 Codex 的对话（不经钩子与 per-thread 管道）")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--thread-id", default=None,
                        help="不给就挑最近一条能代表用户的对话")
    parser.add_argument("--text", default=None,
                        help="不给就用语音入口那条正文")
    parser.add_argument("--turn-timeout", type=float,
                        default=TURN_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    text = args.text or (VOICE_ENTRY_TEXT % args.request_id)
    try:
        result = send(text, thread_id=args.thread_id,
                      turn_timeout=args.turn_timeout)
    except NotifyError as error:
        print(json.dumps({"ok": False, "detail": str(error)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
