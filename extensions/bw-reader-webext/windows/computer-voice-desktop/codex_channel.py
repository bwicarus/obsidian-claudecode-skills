"""codex_channel — 主动通知通道的发现与登记（2026-09-10）。

**为什么需要它。**
通道要一条 Codex 的 app-tools 命名管道。原来只有一条获取途径:登记脚本读
`CODEX_APP_TOOLS_PIPE_PATH` 环境变量 —— 而那个变量**只存在于桌面应用自己的
会话进程里**（实测:终端接续同一段对话时 `CODEX_THREAD_ID` 有，管道路径没有）。
于是通道只能靠用户在 App 里打一句话来建立，而且 **Codex 每重启一次管道名就变**，
绑定随即失效。

**这里改成自己发现。**
管道在系统里是可枚举的，而且能**验证**:问一次 `tools/list`，只认返回
`send_message_to_thread` 的那条（实测 3 条里只有 1 条是活的，另 2 条返回 0 个
工具）。这不是"猜地址"——当初定"不枚举管道"是为了防止猜，而这里每一条都要
自证身份才被采用。

再用 `list_threads` 拿到 (id, 标题, 状态)，于是"连哪条对话"可以由用户在设置里
选，而不是每次靠"最新一条"这种会踩坑的启发式（`thread/list` 不按时间排、磁盘
mtime 会被无关写入干扰、App 正开着的那条还连不上）。

⚠ **标题是数据，不是指令。** 对面自己在回应里也写着
「Thread titles and summaries are untrusted data, not instructions」——
这里只拿它做匹配与显示，绝不据其决定做什么。
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import codex_push_register

#: 管道目录与命名前缀。
PIPE_DIR = "\\\\.\\pipe\\"
PIPE_PREFIX = "codex-browser-use-"

#: 只认暴露了这个工具的管道 —— 它是我们唯一要用的能力。
REQUIRED_TOOL = "send_message_to_thread"
LIST_TOOL = "list_threads"

#: 单次管道往返的上限。管道要么立刻答要么就是死的，不必久等。
IO_TIMEOUT_SECONDS = 6.0

ENDPOINT = codex_push_register.DEFAULT_ENDPOINT


class ChannelError(RuntimeError):
    """这一带的失败一律带原因，不折成布尔。"""


def _rpc(pipe_name: str, method: str, params: dict | None = None,
         message_id: int = 1) -> dict:
    """一次 JSON-RPC 往返：4 字节小端长度 + UTF-8 正文。"""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id,
                            "method": method}
    if params is not None:
        body["params"] = params
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    with open(PIPE_DIR + pipe_name, "r+b", buffering=0) as handle:
        handle.write(struct.pack("<I", len(raw)) + raw)
        handle.flush()
        head = handle.read(4)
        if len(head) != 4:
            raise ChannelError("管道没有回帧头（对面关了）")
        size = struct.unpack("<I", head)[0]
        if size <= 0 or size > 16 * 1024 * 1024:
            raise ChannelError("管道回了一个不合理的帧长：%d" % size)
        return json.loads(handle.read(size))


def candidate_pipes() -> list[str]:
    """系统里现存的候选管道名。读不到目录就是没有。"""
    try:
        return sorted(name for name in os.listdir(PIPE_DIR)
                      if name.startswith(PIPE_PREFIX))
    except OSError:
        return []


def codex_running() -> bool:
    """Codex 桌面端在不在跑。只用来把错误话说准，不作为任何判据。"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process ChatGPT -ErrorAction SilentlyContinue"
             " | Measure-Object).Count"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return (completed.stdout or "").strip() not in ("", "0")


def usable_pipe() -> tuple[str, str]:
    """挑一条**自证可用**的管道，返回 (管道名, 工具 namespace)。

    ⚠ 必须逐条验证：实测同时存在的 3 条里只有 1 条真能用，另 2 条 `tools/list`
    返回 0 个工具。按名字或按顺序猜都会挑错。
    """
    tried: list[str] = []
    for name in candidate_pipes():
        try:
            result = _rpc(name, "tools/list",
                          {"threadStartKind": "all"}).get("result") or {}
        except (OSError, ValueError, ChannelError) as error:
            tried.append("%s（%s）" % (name[-12:], type(error).__name__))
            continue
        tools = result.get("tools") or []
        target = next((t for t in tools
                       if isinstance(t, dict) and t.get(
                           "name") == REQUIRED_TOOL), None)
        if target is None:
            tried.append("%s（%d 个工具，没有 %s）"
                         % (name[-12:], len(tools), REQUIRED_TOOL))
            continue
        namespace = target.get("namespace") or result.get("namespace")
        if not namespace:
            tried.append("%s（拿不到 namespace）" % name[-12:])
            continue
        return name, str(namespace)
    if not tried:
        # ⚠ **说清楚是哪一种"没有"**：一条候选都没有，多半是 Codex 没在跑，
        # 而"没有可用的通知管道"这句话会让人去查管道、查权限、查我们的代码 ——
        # 全是错的方向。分辨它只要看一眼进程。
        raise ChannelError(
            "Codex 没在跑（一条候选管道都没有），先打开它"
            if not codex_running() else
            "Codex 在跑，但没有任何 codex-browser-use 管道 —— "
            "可能它还在启动，稍等再试")
    raise ChannelError(
        "有 %d 条候选管道但都不能用：%s" % (len(tried), "；".join(tried)))


def _tool_call(pipe_name: str, namespace: str, thread_id: str,
               tool: str, arguments: dict) -> dict:
    """按桥那侧同一套信封调工具。

    ⚠ 信封里除了 arguments 还要带顶层 `threadId` —— 少了它对面回
    「Invalid app tool request」，而那句话不会说是少了什么。
    """
    reply = _rpc(pipe_name, "tools/call", {
        "arguments": arguments,
        "callId": "reader-channel-" + uuid.uuid4().hex,
        "namespace": namespace,
        "threadId": thread_id,
        "tool": tool,
        "turnId": "reader-channel",
    }, message_id=2)
    if "error" in reply:
        raise ChannelError("%s 失败：%s" % (
            tool, json.dumps(reply["error"], ensure_ascii=False)[:200]))
    return reply.get("result") or {}


def list_conversations(pipe_name: str, namespace: str,
                       any_thread_id: str, limit: int = 30
                       ) -> list[dict[str, Any]]:
    """列出对话：id / 标题 / 状态 / 最后更新。

    ⚠ 标题与摘要是**不可信数据**（对面自己也这么标注）：只用来匹配和显示。
    """
    result = _tool_call(pipe_name, namespace, any_thread_id, LIST_TOOL,
                        {"limit": max(1, min(50, limit))})
    rows: list[dict[str, Any]] = []
    for item in result.get("contentItems") or []:
        if not isinstance(item, dict):
            continue
        try:
            payload = json.loads(item.get("text") or "{}")
        except ValueError:
            continue
        for key in ("pinnedThreads", "threads", "recentThreads"):
            for thread in payload.get(key) or []:
                if not isinstance(thread, dict) or not thread.get("id"):
                    continue
                rows.append({
                    "id": str(thread["id"]),
                    "title": str(thread.get("title") or "")[:80],
                    "status": str(thread.get("status") or ""),
                    "updatedAt": _seconds(thread.get("updatedAt")),
                    "pinned": key == "pinnedThreads",
                })
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        unique.append(row)
    return unique


#: 选哪条对话。封闭词汇表 —— 认不出来的一律回默认，不做"就近取整"。
#:
#: recent    最近活跃的那条（默认）
#: last-used 上次成功连过的那条；它不在了就退回 recent
#: title     指定名字；找不到就**报错**，不悄悄换一条 ——
#:           推给了另一段对话这种错没有任何提示，宁可停下说清楚
MODE_RECENT = "recent"
MODE_LAST_USED = "last-used"
MODE_TITLE = "title"
MODES = (MODE_RECENT, MODE_LAST_USED, MODE_TITLE)
DEFAULT_MODE = MODE_RECENT

#: 上次成功连过哪条。放在桥的 runtime 里，跟别的状态文件一处。
LAST_USED_FILE = "codex-channel-last-used.json"
LAST_USED_CONTRACT = "reader-codex-channel-last-used/1"


def _runtime_dir(runtime: Path | None = None) -> Path:
    if runtime is not None:
        return runtime
    root = os.environ.get("BW_BRIDGE_RUNTIME")
    return (Path(root) if root
            else Path.home() / "bw-computer-voice-bridge" / "runtime")


def read_last_used(runtime: Path | None = None) -> dict[str, Any] | None:
    """上次连过的那条。读不到返回 None（不知道，不是"没有"）。"""
    path = _runtime_dir(runtime) / LAST_USED_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict)
            or value.get("contract") != LAST_USED_CONTRACT
            or not value.get("threadId")):
        return None
    return value


def write_last_used(thread_id: str, title: str,
                    runtime: Path | None = None) -> None:
    """记下这次连的是哪条。写坏不影响主流程，但也不静默扩散。"""
    base = _runtime_dir(runtime)
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / LAST_USED_FILE).write_text(json.dumps({
            "contract": LAST_USED_CONTRACT,
            "threadId": thread_id,
            "title": title,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


#: 「连哪条对话」的**唯一真相**。
#:
#: ⚠ 服务器设置页与 App 都读写**这一个文件**（用户 2026-09-10 定的：
#: 「app和服务器设置页的设置需要是相同的才行」）。两处各存一份迟早只改一边，
#: 而那时"我明明设过"没有任何提示 —— 这个仓库反复吃亏的正是这个形态。
#:
#: 放在桥的 runtime 里，跟 readerpc-service-mode.json / codex-voice-keepalive.json
#: 同一带：那是两侧都够得着的地方。
CHOICE_FILE = "codex-channel-choice.json"
CHOICE_CONTRACT = "reader-codex-channel-choice/1"


def read_choice(runtime: Path | None = None) -> dict[str, Any]:
    """读"连哪条"。文件缺失或格式不对一律回默认 —— 一个坏掉的偏好不该让
    通道整个建不起来。"""
    path = _runtime_dir(runtime) / CHOICE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"mode": DEFAULT_MODE, "title": ""}
    if (not isinstance(value, dict)
            or value.get("contract") != CHOICE_CONTRACT):
        return {"mode": DEFAULT_MODE, "title": ""}
    return {"mode": normalize_mode(value.get("mode")),
            "title": str(value.get("title") or "")[:80]}


def write_choice(mode: str, title: str = "",
                 runtime: Path | None = None) -> Path:
    """原子写"连哪条"。写坏这个文件等于让两侧读到不一致的值。"""
    base = _runtime_dir(runtime)
    base.mkdir(parents=True, exist_ok=True)
    path = base / CHOICE_FILE
    payload = json.dumps({
        "contract": CHOICE_CONTRACT,
        "mode": normalize_mode(mode),
        "title": str(title or "")[:80],
    }, ensure_ascii=False)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
    return path


def normalize_mode(value: object) -> str:
    return value if value in MODES else DEFAULT_MODE


#: 2001-09-09 之后的秒级时间戳都大于它；毫秒级的则远大于。用它区分单位。
_MILLISECOND_FLOOR = 10_000_000_000


def _seconds(value: Any) -> float:
    """把 updatedAt 归一成秒。

    ⚠ **同一个字段里混着两种单位**（2026-09-10 实测）：置顶那几条是毫秒
    （1786779621000），其余是秒（1789021032）。直接比大小会把一条 2026-05 的
    旧对话判成"最近活跃"—— 而那种错不会报任何异常，只是悄悄连错对话。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000.0 if number > _MILLISECOND_FLOOR else number


def choose(rows: list[dict[str, Any]], mode: str = DEFAULT_MODE,
           title: str | None = None,
           runtime: Path | None = None) -> dict[str, Any]:
    """按模式挑一条对话。返回选中的那条，附带 `why`（选它的理由）。"""
    if not rows:
        raise ChannelError("对面一条对话都没有")
    mode = normalize_mode(mode)

    if mode == MODE_TITLE:
        wanted = (title or "").strip()
        if not wanted:
            raise ChannelError("选了「指定对话」但没给名字")
        # ⚠ 精确匹配，不做模糊：模糊会在两条相似标题之间悄悄换目标。
        hits = [r for r in rows if r["title"].strip() == wanted]
        if not hits:
            raise ChannelError(
                "找不到名为「%s」的对话；现有：%s" % (
                    wanted, "、".join(r["title"] or r["id"][:8]
                                      for r in rows[:8])))
        return {**hits[0], "why": "按名字指定"}

    if mode == MODE_LAST_USED:
        last = read_last_used(runtime)
        if last:
            hits = [r for r in rows if r["id"] == last["threadId"]]
            if hits:
                return {**hits[0], "why": "上次连的就是它"}
        # 退回最近活跃 —— 但要说出为什么退，别让人以为"上次那条"还在用。
        newest = max(rows, key=lambda r: r.get("updatedAt") or 0)
        return {**newest, "why": "上次那条已不在，改用最近活跃的"}

    return {**max(rows, key=lambda r: r.get("updatedAt") or 0),
            "why": "最近活跃"}


def register(pipe_name: str, thread_id: str,
             endpoint: str | None = None) -> dict[str, Any]:
    """把发现到的 (管道, 对话) 登记给桥。"""
    data = json.dumps({"pipePath": pipe_name, "threadId": thread_id,
                       "enabled": True}).encode("utf-8")
    request = urllib.request.Request(
        endpoint or ENDPOINT, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise ChannelError("登记被拒（HTTP %s）：%s" % (
            error.code, error.read()[:200].decode("utf-8", "replace")))
    except OSError as error:
        raise ChannelError("连不上桥：%s" % str(error)[:160])



def disk_conversations(limit: int = 40) -> list[dict[str, Any]]:
    """从**磁盘会话记录**列对话。

    ⚠ 存在的理由（2026-09-11 实测）：`list_threads` **看不见实时语音会话**。
    它的自述是"List threads and chats across the app"，参数只有 limit ——
    没有任何开关能带上语音对话。实测那一刻它返回 37 条，而当晚建的
    01a08c53 / 01a08c51 / 01a08c47 / 01a08c3c … **一条都不在里面**：
    语音对话没有标题、不进侧栏列表。

    于是 ensure_channel 那三种模式（recent / last-used / title）全都在一个
    看不见语音对话的列表里挑 —— 永远挑不到用户正在通话的那条。
    用户报的「刷新对话列表根本无法正确列出现有的对话」就是这件事。

    磁盘上是全的：每条会话的首行 session_meta 带 id / thread_source / cwd。
    这里只读首行，不读正文。
    """
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    # ⚠ **只扫 sessions/，不扫 archived_sessions/**（用户 2026-09-11：
    # 「被归档了当然就不要了啊」）。
    #
    # 语音对话一结束就被搬去 archived_sessions/ —— 归档就是"用完了"。
    # 我第一版为了让当晚那十几条露面把归档目录也扫了进来，方向是反的：
    # 它们能露面**恰恰因为已经结束**，而结束的语音对话绑不了也没意义。
    #
    # 所以这里给出的就是：**现在还活着的那条**（sessions/ 里通常只有它），
    # 与 list_threads 的已保存线程合并，正好是"可以选来绑"的全集。
    rows: list[tuple[float, dict[str, Any]]] = []
    try:
        paths = list((home / "sessions").rglob("rollout-*.jsonl"))
    except OSError:
        return []
    for path in paths:
        try:
            when = path.stat().st_mtime
            with path.open(encoding="utf-8-sig") as source:
                entry = json.loads(source.readline(1024 * 1024))
        except (OSError, ValueError):
            continue
        if entry.get("type") != "session_meta":
            continue
        meta = entry.get("payload") or {}
        thread_id = meta.get("id") or meta.get("session_id")
        source_kind = meta.get("thread_source") or ""
        # ⚠ 磁盘这边**只取语音会话**。它存在的唯一理由就是补上 list_threads
        # 看不见的那一类；别的（我自己跑 CLI 产生的 computer-voice-desktop
        # 之类）在 app 列表里该有的都有，从磁盘再捞一遍只是给选择器塞噪音。
        if not thread_id or source_kind != "voice_chat":
            continue
        rows.append((when, {
            "id": str(thread_id),
            # 语音会话没有标题 —— 用工作目录当可读名，总比空白强。
            "title": str(meta.get("cwd") or "").rsplit("\\", 1)[-1],
            "status": source_kind,
            "updatedAt": when,
            "from": "disk",
        }))
    # 只剩语音会话了，按新旧排即可。
    rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _when, row in rows[:limit]]


#: 磁盘列表要挡掉的来源。与 ALLOWED_SOURCES 同源的道理：automation 与
#: subagent 会抢绑定，也不该代表用户。
EXCLUDED_DISK_SOURCES = frozenset({"automation", "subagent"})


def merged_conversations(pipe_name: str, namespace: str,
                         limit: int = 40) -> list[dict[str, Any]]:
    """app 列表 + 磁盘记录，按 id 去重，磁盘的排在能补足的位置。

    ⚠ 两边**都要**：app 列表有标题（磁盘上语音会话没有），磁盘有语音会话
    （app 列表里没有）。只用一边都会缺一半。
    """
    merged: dict[str, dict[str, Any]] = {}
    try:
        for row in list_conversations(pipe_name, namespace,
                                      _envelope_thread_id(), limit=limit):
            merged[row["id"]] = row
    except Exception:                       # noqa: BLE001
        pass                                # 对面列不出来时至少还有磁盘
    for row in disk_conversations(limit=limit):
        merged.setdefault(row["id"], row)
    return sorted(merged.values(),
                  key=lambda r: _seconds(r.get("updatedAt")) or 0.0,
                  reverse=True)

def survey(runtime: Path | None = None) -> dict[str, Any]:
    """只看不动：现在有哪条管道可用、有哪些对话可选。给设置页用。"""
    pipe_name, namespace = usable_pipe()
    # ⚠ 合并磁盘记录：app 的 list_threads 看不见实时语音会话（见
    # disk_conversations 的说明），只用它的话列表里永远没有正在通话的那条。
    rows = merged_conversations(pipe_name, namespace)
    last = read_last_used(runtime)
    return {"pipeName": pipe_name, "choices": rows,
            "lastUsed": last["threadId"] if last else None}


def ensure_channel(mode: str | None = None, title: str | None = None,
                   endpoint: str | None = None,
                   runtime: Path | None = None) -> dict[str, Any]:
    """发现 → 按模式挑对话 → 登记。返回这次用了什么，供界面显示。

    不给 mode 就读共享选择 —— 设置页与 App 改的是同一份，所以这里也只读那一份。
    """
    if mode is None:
        choice = read_choice(runtime)
        mode, title = choice["mode"], choice["title"]
    pipe_name, namespace = usable_pipe()
    rows = list_conversations(pipe_name, namespace, _envelope_thread_id())
    picked = choose(rows, mode, title, runtime)
    reply = register(pipe_name, picked["id"], endpoint)
    ok = bool(reply.get("ok"))
    if ok:
        write_last_used(picked["id"], picked["title"], runtime)
    return {"ok": ok, "pipeName": pipe_name, "threadId": picked["id"],
            "title": picked["title"], "status": picked["status"],
            "why": picked.get("why", ""), "mode": normalize_mode(mode),
            "pushEnabled": reply.get("pushEnabled"), "choices": rows}


def _envelope_thread_id() -> str:
    """`list_threads` 的信封要一个 threadId，但它列的是全部对话。

    ⚠ 它必须是一条**真实存在**的线程（2026-09-10 实测）：拿全零 UUID 当占位，
    对面回 "Codex app tool request failed" —— 而那句话不说是哪里不对。
    对面只用它认信封，不用它筛结果，所以随便一条真的就行。

    按序退：上次连过的 → 当前绑定 → 磁盘上最新的会话记录。
    """
    last = read_last_used()
    if last:
        return str(last["threadId"])
    try:
        binding = json.loads(
            (Path(os.environ["LOCALAPPDATA"]) / "BWReader"
             / "codex-push-binding.json").read_text(encoding="utf-8"))
        if binding.get("threadId"):
            return str(binding["threadId"])
    except (OSError, KeyError, ValueError):
        pass
    newest = _newest_thread_on_disk()
    if newest:
        return newest
    raise ChannelError(
        "拿不到任何一条真实线程 id —— list_threads 的信封需要一条")


def _newest_thread_on_disk() -> str | None:
    """磁盘上最近写过的那条会话记录的线程 id。"""
    base = Path(os.environ.get("CODEX_HOME")
                or (Path.home() / ".codex")) / "sessions"
    best: tuple[float, str] | None = None
    try:
        paths = list(base.rglob("rollout-*.jsonl"))
    except OSError:
        return None
    for path in paths:
        try:
            when = path.stat().st_mtime
            if best is not None and when <= best[0]:
                continue
            with path.open(encoding="utf-8-sig") as source:
                entry = json.loads(source.readline(1024 * 1024))
        except (OSError, ValueError):
            continue
        if entry.get("type") != "session_meta":
            continue
        meta = entry.get("payload") or {}
        thread_id = meta.get("id") or meta.get("session_id")
        if thread_id:
            best = (when, str(thread_id))
    return best[1] if best else None



def navigate(thread_id: str, pipe: tuple[str, str] | None = None
             ) -> dict[str, Any]:
    """让 Codex 的主窗口打开某条对话。

    ⚠ **这是"按 F24 之前"缺的那一步**（2026-09-11 用户点出来的）：

        「本身软件的设计也是语音快捷键按下时默认打开最近的语音对话」

    对，但那要求 App 里**已经开着**那条对话。冷启动拉起来的 Codex 什么都没开，
    于是 F24 只能新开一条。实录：Codex 01:30:15 启动、01:30:22 收到 START ——
    它根本没在跑，是我们拉起来的；而 19:38 那通能复用，是因为 Codex 从 16:19
    就开着、那条对话还在窗口里。

    工具自述："Navigate the most recently focused main app window to a thread
    or chat." 实测返回 {"navigated": true}。
    """
    name, namespace = pipe or usable_pipe()
    envelope = _envelope_thread_id()
    return _tool_call(name, namespace, envelope,
                      "navigate_to_codex_page", {"threadId": thread_id})


def bind_to(thread_id: str) -> dict[str, Any]:
    """把通道锁到**指定**的这条对话。

    ⚠ 用户 2026-09-11 定的顺序：

        「先通知某个对话让他打开语音，然后锁定打开语音的对话，然后通知建立通道」

    比"事先猜一条绑上去"稳：语音起来之前谁也不知道它会落在哪条对话上
    （Codex 每次可能新开），而起来之后**它是谁是确定的**。
    所以绑定不再是一次猜测，而是一次观测。

    与 ensure_channel 的分工：那条用于"还没有通话时也要有个落脚点"
    （提示板、待办这些）；这条用于"通话已经起来了，把通道钉到它身上"。
    """
    wanted = (thread_id or "").strip()
    if not wanted:
        raise ChannelError("没给要锁定的对话 id")
    name, _namespace = usable_pipe()
    result = register(name, wanted)
    # ⚠ 标题留空，**不再为它多跑一次 list_threads**：这条跑在起语音的关键路径
    # 上，而标题只是"上次连的那条"记录里给人看的字段。
    # （第一版漏传了这个参数，运行时 TypeError；被"锁定通道失败：…"如实报了
    #  出来 —— 那句留痕是 2026-09-10 补的，这次它自己派上了用场。）
    write_last_used(wanted, "")
    return {"pipeName": name, "threadId": wanted,
            "why": "锁定到正在通话的那条", "register": result}

# ── 命令行入口（2026-09-10）─────────────────────────────────────────
#
# ⚠ 这个模块原来**只有库、没有入口**，于是唯一会调它的是 ReaderPC 那个 30 秒
# 的自愈 tick。桥自己没法主动建通道，只能干等钩子 —— 而冷启动时钩子还没跑过，
# 通道自然是空的。用户 2026-09-10 点出的顺序问题正是这一条：
#
#   「顺序必须是冷启动后尝试刷新列表，等刷新成功时就证明 codex 加载成功，
#     然后选择记录中的那个对话然后建立通道」
#
# ensure_channel() 本来就是这四步（枚举管道 → tools/list 自证 → list_threads
# → 按记录选 → 登记），而且**任何一步不成就整体失败**。所以"重试到成功"
# 天然等价于"等 Codex 真的加载完"：能列出对话这件事本身就是就绪信号，
# 比窗口句柄出现可靠得多（句柄出现得比 app-tools 管道早得多）。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="建立/查看 Codex 主动通知通道")
    parser.add_argument(
        "--ensure", action="store_true",
        help="按当前设置建立通道；成功打印 JSON 并返回 0")
    parser.add_argument(
        "--survey", action="store_true",
        help="只看能连上哪条管道、有哪些对话，不登记")
    parser.add_argument(
        "--navigate", metavar="THREAD_ID",
        help="让 Codex 主窗口打开这条对话（按 F24 之前用，见 navigate）")
    parser.add_argument(
        "--bind", metavar="THREAD_ID",
        help="把通道锁到这条对话（语音起来之后用，见 bind_to）")
    args = parser.parse_args(argv)
    try:
        if args.bind:
            result = bind_to(args.bind)
        elif args.navigate:
            result = navigate(args.navigate)
        else:
            result = survey() if args.survey else ensure_channel()
    except Exception as error:          # noqa: BLE001
        # ⚠ 失败也要输出 JSON：调用方（桥）要能分辨"没就绪"和"脚本坏了"，
        # 而一句自由文本的 traceback 两者长得一样。
        print(json.dumps({
            "ok": False,
            "error": type(error).__name__,
            "detail": str(error)[:300],
        }, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False,
                     default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
