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
                # 服务端反向请求（审批之类）
                reply = (on_server_request(message)
                         if on_server_request else None)
                self._write({"jsonrpc": "2.0", "id": message["id"],
                             "result": reply if reply is not None
                             else {"decision": "denied"}})
        raise NotifyError("%s 超时（%.0f 秒）" % (method, timeout))


def newest_thread(server: AppServer, limit: int = 20) -> dict:
    """挑一个能代表用户的最近对话。

    ⚠ 用 `thread/list` 而不是 `thread/loaded/list`：后者只列**本进程**加载的，
    桌面端开着的那些不在其中（实测返回空）。
    """
    result = server.request("thread/list", {"limit": limit})
    rows = result.get("data") if isinstance(result, dict) else result
    if not isinstance(rows, list) or not rows:
        raise NotifyError("Codex 一条对话都没有，没有可送达的目标")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("ephemeral"):
            continue
        if thread_source_of(str(row.get("id") or "")) in EXCLUDED_SOURCES:
            continue
        if row.get("id"):
            return row
    raise NotifyError(
        "最近 %d 条对话里没有可用目标（都是子任务/定时任务或临时会话）" % len(rows))


def approve(message: dict) -> dict | None:
    """审批应答：只放行这条链自己的脚本。

    ⚠ 写成"什么都同意"就等于把一条通知变成任意命令执行入口。宁可拒绝，
    并让拒绝的原因出现在 turn 里 —— 那样至少说得清为什么没做成。
    """
    method = str(message.get("method") or "")
    if "pproval" not in method:
        return None
    blob = json.dumps(message.get("params") or {}, ensure_ascii=False)
    allowed = any(name in blob for name in ALLOWED_SCRIPTS)
    return {"decision": "approved" if allowed else "denied"}


def send(text: str, *, thread_id: str | None = None,
         entry: list[str] | None = None,
         turn_timeout: float = TURN_TIMEOUT_SECONDS) -> dict:
    """把一条通知送进 Codex 的对话，并等这一轮跑完。"""
    with AppServer(entry) as server:
        server.request("initialize", {
            "clientInfo": {"name": "bw-reader-voice-entry", "version": "1"},
        })
        server.notify("initialized")
        target = thread_id
        preview = ""
        if target is None:
            row = newest_thread(server)
            target, preview = row["id"], str(row.get("preview") or "")[:40]
        server.request("thread/resume", {"threadId": target})
        server.request(
            "turn/start",
            {"threadId": target,
             "input": [{"type": "text", "text": text}]},
            timeout=turn_timeout,
            on_server_request=approve)
        return {"ok": True, "threadId": target, "preview": preview}

#: 语音入口那条通知的正文。
#:
#: 两件事一起说（用户 2026-09-10：「找到最新的对话然后让他打开通道，通道中
#: 预先放入打开语音那一套操作的通知」）：先把主动通知的通道登记好，再按能力
#: 说明开语音。登记那步失败也不影响开语音 —— 入口脚本只是 POST 给桥，不经过
#: 通道；登记只是让**以后**的推送（挂断、状态回报）有地方可送。
VOICE_ENTRY_TEXT = (
    "语音入口（requestId: %s）。用户刚在 App 上要求开始语音通话。\n"
    "\n"
    "第一步，把主动通知的通道登记好（这一步失败不要停，继续第二步）：\n"
    '    python "%%LOCALAPPDATA%%\\BWReader\\codex_push_register.py" --enable\n'
    "\n"
    "第二步，按能力说明 voice-entry.md 开语音：跑一次入口脚本，看它打印的"
    " confirmed；没进就再跑一次；两次都不成就跑报错脚本并停下。\n"
    "不要跑第三次，也不要自己另想办法开语音。\n"
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
