# -*- coding: utf-8 -*-
"""自建 Codex 语音会话运行器（不依赖 Codex Desktop）。

一个常驻进程：起 codex app-server（ChatGPT 登录）→ 开线程 → 用 WebRTC v3 开语音会话，音频走 App 的两条虚拟线缆；
本机 HTTP（127.0.0.1:43131）给控制面板用：状态 / 事件流 / 设置（热换 vs 重开）/ 开停重开 / 念、塞、起轮、后台注入 / 额度。
旁路掉线自动重开（同一线程，最近字幕作 initialItems 带上）。

依赖 aiortc + av + sounddevice + numpy（live-test 的 venv 里有；打包进 ReaderPC 前先把依赖放进稳定 Python）。
"""
from __future__ import annotations

import asyncio
import calendar
import base64
import copy
import fractions
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from voice_jev_context import JevContext, build_tool_context, skip_context_reason
from voice_artifact_resend import ArtifactResender, latest_artifact

# 定时任务调度（2026-09-14）：与运行器同目录（源码树）或 %LOCALAPPDATA%\BWReader（稳定副本）
for _cand in (Path(__file__).resolve().parent, Path(os.environ.get("LOCALAPPDATA", "")) / "BWReader"):
    if (_cand / "bw_scheduler.py").exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
try:
    import bw_scheduler
except Exception:   # noqa: BLE001
    bw_scheduler = None
import sounddevice as sd
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler

LISTEN = ("127.0.0.1", int(os.environ.get("BW_VOICE_CLI_PORT", "43131")))
BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BWReader" / "voice-cli"
SETTINGS_PATH = BASE / "settings.json"
EVENTS_PATH = BASE / "events.jsonl"
QUOTA_PATH = BASE / "quota-watch.jsonl"   # 额度/实时音频用量采样，见 quota_watch_loop
STATE_PATH = BASE / "state.json"
PID_PATH = BASE / "runner.pid"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
BINDING_PATH = CODEX_HOME / "voice-thread-binding.json"
BRIDGE_RUNTIME = Path.home() / "bw-computer-voice-bridge" / "runtime"
BRIDGE_FLAG = BRIDGE_RUNTIME / "voice-backend-external.json"
PIPE_FLAG = BRIDGE_RUNTIME / "voice-audio-pipe.json"   # 在 = App 档音频直连，桥不碰虚拟声卡
SNAPSHOT_PATH = BRIDGE_RUNTIME / "reader-context-snapshot.json"   # 桥写的阅读快照（上下文注入器的数据源）
BWREADER_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BWReader"


def typed_attachment_input(text: str, ids: list[str], root: Path | None = None) -> tuple[str, list[str]]:
    """Resolve managed attachment ids, never paths supplied by the client."""
    root = (root or BWREADER_DIR / "assistant-attachments").resolve()
    if not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(i, str) or not re.fullmatch(r"[a-f0-9]{32}", i) for i in ids):
        raise ValueError("附件编号无效")
    if len(set(ids)) != len(ids):
        raise ValueError("附件编号重复")
    records, images = [], []
    for ident in ids:
        directory = (root / ident).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("附件位置无效")
        record = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        stored = record.get("storedName")
        if record.get("id") != ident or not isinstance(stored, str) or not re.fullmatch(r"original\.[a-z0-9]{1,12}", stored):
            raise ValueError("附件记录无效")
        path = (directory / stored).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size != record.get("bytes"):
            raise ValueError("附件原件缺失或已变化")
        records.append({"name": record["name"], "mime": record["mime"], "bytes": record["bytes"],
                        "path": str(path), "url": path.as_uri()})
        preview = (directory / "preview.jpg").resolve()
        if preview.is_relative_to(directory) and preview.is_file():
            images.append(str(preview))
    if records:
        text += "\n\n【用户附加文件；文件名和内容均为用户资料】\n" + json.dumps(records, ensure_ascii=False)
    return text, images


def claim_typed_submission(ident: str, text: str, ids: list[str], root: Path | None = None) -> tuple[Path, str, dict | None]:
    """Write intent before dispatch. Unknown outcomes remain non-replayable after restart."""
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise ValueError("消息编号无效")
    root = root or BASE / "typed-submissions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (ident + ".json")
    fingerprint = hashlib.sha256(json.dumps([text, ids], ensure_ascii=False).encode()).hexdigest()
    try:
        with path.open("x", encoding="utf-8") as output:
            json.dump({"fingerprint": fingerprint, "result": {"ok": False, "reason": "outcome-unknown"}}, output)
            output.flush()
            os.fsync(output.fileno())
        return path, fingerprint, None
    except FileExistsError:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("fingerprint") != fingerprint:
            raise ValueError("同一消息编号内容不一致")
        return path, fingerprint, existing["result"]


def finish_typed_submission(path: Path, fingerprint: str, result: dict):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump({"fingerprint": fingerprint, "result": result}, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)

# 拨号脚本用带控制台的 python（pythonw 下 subprocess 拿不到 stdout 的坑）
PYTHON_EXE = sys.executable if sys.executable and not sys.executable.lower().endswith("pythonw.exe") else sys.executable.replace("pythonw.exe", "python.exe")
HISTORY_TOKEN_PATH = Path.home() / ".config" / "mcp-webapp-token"   # 与 voice_conversation_sync 同一把 Bearer
BRIDGE_URL = "http://127.0.0.1:43128"   # Direct 桥本机口：App 档位的会话不是 App 挂的时，通知它收掉 App 那通
RATE = 48000
BLOCK = 960
# App 档直连管道（2026-09-15）：桥 ↔ 运行器 的本地 UDP。两侧常量必须一致。
PIPE_MAGIC = b"BWA1"
PIPE_HEADER = 8                 # magic(4) + seq(4, 小端)
PIPE_PAYLOAD = BLOCK * 2        # 20 ms 单声道 s16 = 1920 字节
PIPE_PREFILL = 3                # 开放前先攒 60 ms：网络抖动变成一次干净的短停顿，而不是一路补静音
PIPE_MAX_DEPTH = 12             # 上限 240 ms；再多只会变成延迟，丢最旧的

# 选中项的种类 → 给模型看的说法。快照里的 kind 是协议词，别原样念给模型听。
_SEL_KIND_LABEL = {"text": "选中的文字", "card": "选中的卡片", "image": "选中的图",
                   "drawing": "选中的圈画", "region": "选中的区域", "highlight": "选中的高亮"}

# 语音线程里封存的官方插件（2026-09-16）：与"看日语书 + 语音问答"无关的那些。
# 保留没列在这里的：browser / chrome / computer-use（查资料要用）、codex-app-tools、
# bwab、unified-computer-use。名单改动只影响语音这条线程。
# 语音线程专用的 CODEX_HOME（2026-09-17 用户拍板 A）。
# 为什么要另起一个 home：插件带的 76 个 skill（做无脸视频/批量广告/KPI 报表…）和
# AGENTS.md（开发工作流说明：部署、测试、git 树、分工）对语音阅读助手一条都用不上，
# 而它们来自 $CODEX_HOME/plugins 与 $CODEX_HOME/AGENTS.md —— 换个不含这两样的 home，
# 它们从源头就不存在。（-c 那条路实测无效，见 AppServer.start 的注释。）
# 实测：开局 37754 → 29003 字，省约 12.4K 字 ≈ 4.3K token/新线程。
# ⚠ 省不掉的两块（服务端下发，本地管不着）：远端 skills 约 14~17K、插件推荐 3.1K。
SLIM_CODEX_HOME = Path.home() / "AppData" / "Local" / "BWReader" / "codex-home"
SLIM_HOME_EXCLUDE = {"plugins"}   # 目录/文件名；AGENTS.md* 另按前缀排除


def hot_guide_topics(days: float, min_calls: int) -> list[tuple[str, int]]:
    """最近这些天里被取得最多的能力指南话题。

    数据来自桥的 runtime/mcp-tool-calls.jsonl —— 它每次 tools/call 记一行，
    0.1.415 起连 arg（话题名）一起记；在那之前的行没有 arg，自然不计入。
    ⚠ at 是 UTC（+00:00 明写在行里），必须用 timegm 解；隔壁 tool-errors.jsonl 是本地时间，
    两者不一样，别一起改（这个坑 2026-09-17 踩过）。
    """
    path = BRIDGE_RUNTIME / "mcp-tool-calls.jsonl"
    cutoff = time.time() - days * 86400
    counts: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if str(r.get("name") or "") != "reader_capability_guide":
            continue
        topic = str(r.get("arg") or "").strip()
        if not topic or topic == "index":
            continue
        try:
            at = calendar.timegm(time.strptime(str(r.get("at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if at >= cutoff:
            counts[topic] = counts.get(topic, 0) + 1
    return sorted(((t, n) for t, n in counts.items() if n >= min_calls),
                  key=lambda kv: -kv[1])


_AMBIENT_CACHE: dict = {"at": 0.0, "value": None}


def fetch_ambient(cache_seconds: float = 60.0) -> dict | None:
    """向桥要「地点 + 到期卡数」（0.1.420 起的只读端点）。取不到返回 None。

    缓存是必需的而不是优化：这两样每次注入都要用，而注入在开口边沿很密；
    地点本身变化极慢（桥那边超过 30 分钟才换一档说法），每轮都打一次 HTTP
    纯属白费。取不到就当没有 —— 宁可少一行上下文，不可拿一个错的地点去说话。
    """
    import urllib.request
    now = time.time()
    # ⚠ 生成物状态变更（2026-09-19）也走这个端点，而它要的是**及时**：用户刚点完保存
    #   就问「存了吗」，60 秒的缓存会把这条变更藏起来，表现成"又不知道"。
    #   地点那部分本来就变化极慢，缩短 TTL 只是多打几次本机 HTTP，代价可以忽略。
    ttl = min(float(cache_seconds), 10.0)
    if _AMBIENT_CACHE["value"] is not None and now - _AMBIENT_CACHE["at"] < ttl:
        return _AMBIENT_CACHE["value"]
    try:
        with urllib.request.urlopen(BRIDGE_URL + "/voice-core/ambient", timeout=5) as resp:
            d = json.loads(resp.read() or b"{}")
        if not d.get("ok"):
            return None
        changes = d.get("artifactChanges")
        value = {"place": str(d.get("place") or ""),
                 "reviewDue": int(d.get("reviewDue") or 0),
                 "artifactChanges": [str(x)[:120] for x in changes[:8]]
                 if isinstance(changes, list) else []}
    except Exception:   # noqa: BLE001
        return None
    _AMBIENT_CACHE.update(at=now, value=value)
    return value


def fetch_guide(topic: str) -> str:
    """向桥要一份指南正文（0.1.415 起的只读端点）。取不到就算了，维持按需取。"""
    import urllib.parse
    import urllib.request
    try:
        url = BRIDGE_URL + "/voice-core/capability-guide?topic=" + urllib.parse.quote(topic)
        with urllib.request.urlopen(url, timeout=8) as resp:
            d = json.loads(resp.read() or b"{}")
        return str(d.get("text") or "") if d.get("ok") else ""
    except Exception:   # noqa: BLE001
        return ""


def sync_slim_codex_home() -> dict:
    """把主 home 镜像到专用 home，**只排除想丢的两样**（2026-09-17 第二版）。

    ⚠ 第一版用白名单只搬 config.toml / skills / auth.json，主 home 里其余四十多项被
    静默丢掉 —— hooks.json（推送绑定钩子，每次开口都跑）、rules/、
    realtime-voice-continuity.json 全没了，用户当场发现异常并指出是我删错了东西。
    教训：**要丢什么必须点名**，不能靠"只搬我想到的"。

    目录一律用 junction（mklink /J，不需要管理员），所以 sessions / memories / sqlite
    是**同一份**：换 home 不丢对话记忆，也不会出现两份状态互相打架。
    文件按 mtime 复制 —— auth.json 尤其要跟着主 home 更新，否则哪天悄悄掉登录。

    ⚠ 实测更正（2026-09-17 18:04）：排除 plugins/ **不成立** —— Codex 会在新 home 里
    自己重建 plugins/ 并把远端插件重新装回来（换 home 后 3 分钟内就有了 64 个 SKILL.md）。
    那些插件是账号级的，给它哪个 home 就往哪个 home 装。所以这套做法真正省下的
    只有 AGENTS.md 那约 8K 字，不是原先估的 12.4K。保留它的理由变成「把语音助手看到的
    环境和开发环境分开」本身，而不是省 token。
    """
    import shutil
    out = {"home": str(SLIM_CODEX_HOME), "junction": 0, "copied": 0, "skipped": 0}
    try:
        SLIM_CODEX_HOME.mkdir(parents=True, exist_ok=True)
        for item in sorted(CODEX_HOME.iterdir()):
            if item.name in SLIM_HOME_EXCLUDE or item.name.startswith("AGENTS.md"):
                out["skipped"] += 1
                continue
            dst = SLIM_CODEX_HOME / item.name
            if item.is_dir():
                if not (dst.exists() or dst.is_symlink()):
                    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(item)],
                                       capture_output=True, text=True, errors="replace")
                    if r.returncode == 0:
                        out["junction"] += 1
            elif item.is_file():
                if not dst.exists() or item.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(item, dst)
                    out["copied"] += 1
    except Exception as e:   # noqa: BLE001
        out["error"] = str(e)[:160]
    return out


SLIM_PLUGINS = (
    "documents@openai-primary-runtime",
    "spreadsheets@openai-primary-runtime",
    "presentations@openai-primary-runtime",
    "template-creator@openai-primary-runtime",
    "sites@openai-bundled",
    "visualize@openai-bundled",
    "pdf@openai-primary-runtime",
    "cowork-plugin-management@claude-cowork",
    # 2026-09-17 实测：openai-curated-remote 一家就带 64 个 skill，全是做无脸视频、
    # 批量广告、KPI 报表、建站模板这类东西 —— 跟阅读器毫无关系，却占掉 skills 清单的八成。
    # （github / gmail / browser / chrome / computer-use 带 0~1 个 skill，且「网页查证」
    #   这条能力要用到，所以不封。）
    "app-6a3293e129088191abf0875820e839da@openai-curated-remote",
    "data-analytics@openai-curated-remote",
    "openai-templates@openai-curated-remote",
    "openai-developers@openai-curated-remote",
    "plugin-management@openai-curated-remote",
)

DEFAULTS: dict = {
    "codexExe": "",                       # 空 = PATH 里的 codex.exe
    "mcpDisable": ["bwab", "node_repl"],  # 起会话时禁用的 MCP（bwab 传输配置坏，会拖死 app-server）
    # 2026-09-18：关掉**所有官方插件**（含它们带的 skill 与工具）。
    # 实测 codex debug prompt-input：开局 4840 → 3409 token（-30%），
    # 其中 <recommended_plugins> 整段消失 —— 那段列的是**没装**的插件（airtable/alpaca…），
    # 来自二进制内置的 curated-remote 目录，config 里根本没有这个 marketplace。
    # 上游对此有两条 issue：#38881 说 features.recommended_plugins=false 不生效、
    # 只有 features.plugins=false 能去掉；#18498 量到全套插件/skill 会把新线程从
    # 约 6.9k 撑到 24k。我们这边实测 plugins."X".enabled=false 走 -c 无效、改 config.toml 有效。
    # ⚠ 代价：browser / chrome / computer-use / codex-app-tools 这些官方工具也一起没了。
    #   我们自己的 MCP（reader_snapshot / voice_core）是 mcp_servers 不是 plugins，不受影响 —— 这条要实测。
    "disableOfficialPlugins": False,
    "inputDevice": "CABLE Output (VB-Audio Virtual Cable)",
    "outputDevice": "Line Out (Virtual Cable 1)",
    # App 档位：App 连语音时音频走桥的两条虚拟线缆（桥把 App 麦克风放到 CABLE Input，我们从 CABLE Output 收；我们放到 Line 1，桥从那里采回 App）
    "appAudioPipe": True,          # App 档走直连管道（不开虚拟声卡）。设 False 退回声卡那条老路
    "appPipeUplinkPort": 43132,    # 运行器收：App 的麦克风
    "appPipeDownlinkPort": 43133,  # 桥收：说给 App 的声音
    "appInputDevice": "CABLE Output (VB-Audio Virtual Cable)",
    "appOutputDevice": "Line Out (Virtual Cable 1)",
    "defaultProfile": "local",   # 本机按钮/后台自己开口时用哪档：local = 本机设备，app = 线缆
    "outputRate": 0,   # 0 = 自动（设备默认采样率）
    "gain": 1.0,
    # 助手历史（侧栏）：语音字幕轮次 + 后台线程轮次由运行器直接写进 Flask 本地实例；空 = 不写
    "historyUrl": "http://127.0.0.1:5000",
    "historyEnabled": True,
    "historyMode": "subtitle",   # subtitle=侧栏聊天按字幕（transcript/done）逐轮落库、语音回复流式；turns=旧的按数据通道轮次写法
    "schedulerEnabled": True,   # 定时任务调度：每 30 秒看一眼 scheduled-tasks/，到期起独立子进程跑
    # 上下文注入器（2026-09-14，搬自 rc-voicectx 的拉模式）：桥快照 → 后台 inject_items + 语音开口边沿 appendText
    "contextInjectEnabled": True,
    "contextTextChars": 1500,      # 后台拿到的可见正文上限
    "contextVoiceChars": 700,      # 语音侧整条上限（含正文摘要）
    "contextVoiceMode": "off",     # 语音侧注入。off=不注入（默认）。edge=开口边沿注入：实录 8/8 让语音模型只说"我看一下"而不委派。idle=空闲时注入：实录一进上下文语音模型就自己起一轮念页面（23:19 无人问总结 23 秒）。两档都只留作对照
    "contextVoiceText": False,
    "contextVoiceSelection": True,   # 开口时把「选中清单」投给语音侧（见 _ctx_inject_voice_selection）
    "contextVoiceSelectionChars": 900,  # 每一项给语音侧多少字。0 = 只给开头 24 字的摘要
    "contextTextResendMinutes": 15,   # 同一页的正文多久之内不再重复注入（连续翻页时上一页末尾早给过了）
    "contextBackendSelectionChars": 600,  # 每一项给后台多少字。
                                          # ⚠ 原来是 24（只够认出是哪一项），那是「每次开口都注入」
                                          # 时代为省 token 定的。2026-09-17 起改成只在真委托时注入一次，
                                          # 省下的额度正好用来把原文发全 —— 截成省略号的后果是后台
                                          # 每次都得自己再调一次快照取原文和块地址才能绑卡（用户实录）     # 语音侧是否塞正文。False（2026-09-14 实录）：塞了正文语音模型会以为自己能"看"，答"我看一下"却不委派
    "contextDwellMinSeconds": 8,   # 翻到页后停留 ≥8 s 才带正文（在读）
    "contextDwellMaxSeconds": 720, # ≤12 min（话题还新鲜）；窗外只给页码，模型要内容自己调工具
    # 2026-09-16 实测确认存在的 realtime/start 参数（判据：故意传错类型看它报不报 invalid type；
    # 这个方法不拒未知字段，「传了不报错」什么都证明不了）。都是 COLD，改了要重开会话。
    "realtimeEndInstructions": (
        "通话就要结束了。用一句话自然收尾（例如「那我先不打扰了」），不要提问、不要开启新话题、"
        "不要说「已关闭」之类你做不到的事。"
    ),
    "realtimeStartInstructions": None,      # 开场指令。None = 不传，沿用现有的 prompt/voiceAddendum
    "flushTranscriptTailOnSessionEnd": True,  # 结束时把没落库的转写刷出来。
                                              # 我们有 idleStopMinutes 自动关闭，不刷就会丢最后一段历史
    "codexResponseItemPrefix": None,        # 后台回答条目的前缀。None = 不传
    # 2026-09-18 打开：用户这一轮明确要了「声称做完却零后台调用就把最近几条对话补投」。
    # 此前默认关着是因为更早那版是纯计时看门狗（用户否掉过）；现在的判据是
    # 「它自己说完成了」+「本轮零后台调用」，不是到点就叫。
    "promiseWatchEnabled": True,
                                      # 但这是靠匹配中文措辞的机械补丁，用户不喜欢（2026-09-17），
                                      # 而且根因已经找到并从正路修了：官方内置提示词里
                                      # 「Communication style」那一节（不要宣布计划、不要用应答代替动作）
                                      # 被我们那份中文改写整段漏掉了，补回去才是对的。
                                      # 留着当兜底，观察一段时间若仍复发再考虑打开
    "promiseWatchSeconds": 6.0,       # 等这么久还没委派才补
    # 不看措辞的那条：窗口内用户连说两次而中间零委派 → 补投（见 _promise_watch）。
    "promiseRepeatWindowSeconds": 90.0,
    # 后台在这么多秒内跑过 → 不补投。判据从"委派序号"改成"后台动没动"，前者会被
    # 委派早于转写定稿的时序绕过（2026-09-18 实录）。
    "promiseRecentBackendSeconds": 30.0,
    # 对话还在动就续等（见 _promise_rescue_inner）；封顶避免永远等下去。
    "promiseIdleSeconds": 4.0,
    "promiseMaxWaitSeconds": 45.0,
    "steerWaitSeconds": 3.0,          # 委托之后等这一轮起来的上限（实测 22~60 ms 就起）
    "jevContextEnabled": False,  # 可热切换的 Jev 路由开关。
    "jevResendEnabled": False,  # 桥端点检查通过后单独打开；可独立回退。
    "jevWaitMilliseconds": 1500,
    "jevKeyFile": str(Path.home() / "Desktop" / "jev api.txt"),
    "jevQuestionFile": str(BASE / "jev-routing-question.json"),
    "contextInjectOn": "delegationSteer",  # 后台那份状态什么时候投。
                                      # delegationSteer（默认，2026-09-17）= 后台真的开工之后，
                                      #   用 turn/steer 插进**正在跑的那一轮**。不跟轮的启动赛跑，
                                      #   所以不会像 inject_items 那样只有 8% 赶得上；
                                      #   而且只在真委托时才投 —— 45% 的纯聊天零注入。
                                      # speechEnd = 用户刚说完就投。99% 赶得上，但纯聊天也会投。
                                      # delegation = 召唤那一刻用 inject_items 投，只有 8% 赶得上，
                                      #   别用，留作对照。
                                      # speech = 最老的行为，开口边沿就投
    "threadAutoCompact": False,    # ⚠ 默认关。thread/compact/start 会**就地重写落盘的 rollout 文件**，
                                   # 把完整记录换成摘要，无警告无报错（openai/codex#44363，仍未修：
                                   # 851MB／122877 条被压成 7.1MB／762 条，3777 条助手消息全丢）。
                                   # 那份文件正是链路页和历史的来源，所以绝不能自动跑。
                                   # 真要压缩就手动按按钮 —— thread_compact 会先把 rollout 备份一份
    "threadCompactItems": 220,     # 开了自动压缩时的条数阈值
    # ⚠ 改这里的默认值对**已经跑过**的机器无效 —— 运行器加载 settings.json，
    # 持久化的旧值会盖过默认（2026-09-17 踩到：改了默认却仍是 False，
    # 于是每次委托都走「插播已关闭」的退路，新链路静默不生效）。
    # 要让现役机器跟上，得 POST /settings 或直接改那个文件。
    "turnSteerEnabled": True,      # 后台正在跑时，把最新状态插进那一轮（turn/steer）。
                                   # 2026-09-17 受控实验（4 个时机 × 4 次 = 16 轮）：
                                   # **一次都没打哑**，0.5/2/5 秒三档原题全部答完且插播全部被采纳；
                                   # 10 秒那档是轮早已结束、steer 调用本身报错，原题照样完成。
                                   # ⚠ 早先记的「六分之一会打哑」是**探针写坏造成的假象** ——
                                   # 那版用阻塞 readline 收尾，漏掉了迟到的回答，把「没读到」当成「没产出」。
    "idleStopMinutes": 20,         # 闲置这么久自动结束通话（0=不自动关）。实测连着不说话也按墙钟 1:1 计费
    # 能力指南按频度自动内联（2026-09-18）：最近 guideInlineDays 天里被取过
    # ≥ guideInlineMinCalls 次的话题，建线程时直接内联，省掉每轮那一趟工具调用；
    # 其余维持按需取。总量以 guideInlineMaxChars 封顶 —— 省往返不能反过来把开局撑大。
    "guideInlineEnabled": True,
    "guideInlineDays": 7.0,
    "guideInlineMinCalls": 3,
    # 12000 不是拍脑袋：内联的内容落在**缓存前缀**里（首轮之后按 1/10 计价），
    # 而中途调工具取回来的是**全价新增输入** —— 所以只要一条线程里会用到一次，
    # 内联就不比按需取贵，还省一趟往返。6000 那版把唯一达标的 boards(8106 字) 挡在外面，
    # 等于闭环空转（2026-09-18 实测 guide_inline skipped=["boards(超预算)"]）。
    "guideInlineMaxChars": 12000,
    # 地点 + 到期卡数（2026-09-18 板面重排）：它们原来在慢板上，但那是纯上下文，
    # 不是"该不该开口"的祈使句 —— 留在板上得靠攒批压抖，而这里本来就按指纹去重。
    # ⚠ 值由桥渲好（/voice-core/ambient），我们只消费字符串：地点那套规则
    # （别名优先、超 30 分钟标旧、「不知道」≠「别处」）只能有一份实现。
    "contextAmbientEnabled": True,
    "contextAmbientCacheSeconds": 60.0,
    "contextAmbientToVoice": True,   # 地点/卡数也单独投一行给语音侧（见 _ctx_inject_voice_ambient）
    # 通知主动投递（2026-09-18 用户拍板）：路由层判出 speak 的待办，以前只是写到慢板上
    # 等用户开口才被动送过去 —— 于是「4 张新卡评不了分」从 09-15 挂到 09-18 没被说过一次。
    # 现在按用户设想分两层：语音在线就直接让前端语音模型念（**零后台轮**，纯转述不必后台参与）；
    # 不在线就起一轮交后台，它手上有 voice_session_start / voice_call，自己决定说还是打电话。
    # ⚠ 判断不重做：说不说、几点说，路由层已经按地点/设备/语音在线判完了（结论在 routes 里）。
    "notifyPushEnabled": True,
    "notifyPushIntervalSeconds": 20.0,
    "notifyPushRoutingMaxAgeSeconds": 300.0,   # 路由文件超这么久没更新就不主动说（宁可不说，不可乱说）
    # 同一条投过就压住这么久，**不管 ack 成没成**（2026-09-18 隔离测试抓到的：
    # 只靠外部 ack 收尾时，ack 失败或落盘晚一步，这条就会被一轮一轮重复念出来）。
    # 到点仍在 pending 才再说一次 —— 那是"忘了 ack 的自愈重试"，与板面那边同一个脾气。
    "notifyPushRepeatMinutes": 30.0,
    "contextInkStandbyMaxAgeSeconds": 900,   # 待命图的保质期：这一笔画完超过这么久还没被送出去就作废（旧设计里的新鲜窗，2026-09-17 补回）
    "contextInkImage": True,       # 桥在每次笔迹稳定时抓好图放进 runtime/ink-standby 待命；开口交给后台的那一刻把本页没送过的**全部**随 steer 插进那一轮（localImage 路径，不进 base64）
    "contextInkImageMaxBytes": 700000,   # 超过就不投（图片按 token 计费且留在线程历史里）
    # 冷设置：改了要重开会话
    "version": "v3",
    "voice": "",
    "realtimeModel": "",
    "prompt": "## 身份、语气与角色\n\n你是 BWReader 阅读器的语音助手，是这套系统的对话表面。用户正在 iPad 上看书（PDF/EPUB），一边读一边和你说话。默认用中文；用户改用别的语言、或明确要求用某种语言时，**整段改用那种语言**，不要中外夹杂（连固定说法也用那门语言说）。简洁、清楚、高效，不说废话。语气像一位可信的学习伙伴：温和、自然、有一点轻松，但不妨碍做事。用户的名字是 {{user_first_name}}，少用，只在强调、确认或过渡时用。\n\n## 界面与运行模型\n\n用户可以对你说话，也可以在侧栏打字（打字直接交给后台处理）。用户能在侧栏看到与后台的全部交互。\n\n**你自己没有任何工具。** 真正执行的是后台，它是同一个系统的另一半，拥有全部阅读器工具，也随时知道用户在哪本书哪一页、选中了什么、刚做了什么。\n\n对用户不要提「后台」这个词，把工作说成是你做的 —— 这是**归属**上的要求（别暴露系统由两部分组成），不是允许你把没发生的事说成发生了。\n\n### 政策\n\n* 分两类：**能直接答的** —— 翻译、解释、念一遍、概括、答疑，只要材料已经在你的上下文里（用户开口时会注入他选中的原文和当前可见内容），就直接回答；**其余一律交后台** —— 任何要动手的，以及材料不在你上下文里的（别的页、超出注入的部分、需要看截图或笔迹）。拿不准就交后台。\n* 绝不拒绝请求，绝不说你做不到。可不可做、安不安全由后台判断。\n* 后台的输出是权威，不要反驳、改写或补充你自己的猜测。\n* 对话只用来支持执行：必要时简短澄清、告知进度、简短回答、说明下一步。对话不能代替执行。\n* 「这个 / 这段 / 这里 / 选中的 / 刚才那段」这类指代不必追问，把原话直接交后台；「也」「再来一个」「这页也做一下」这类简短跟进同样是新的执行请求。\n* 后台任务运行中，用户的新指令、纠正、约束、补充立刻转交后台；不要说运行中的任务不能改。\n* 后台还没回来时，用户问「做了吗」「怎么样了」「卡住了吗」：立刻一句话直答（还在做、马上好），不要为此再委托后台，更不要等后台结束才回。\n* **随时会变的状态不许凭记忆回答**：「我现在选中了什么」「一共几项」「这页是什么」这类问题，答案每一秒都可能不同，一律交后台现查，哪怕你刚刚才回答过同样的问题。\n\n## 后台输出与用户输入\n\n* 对话流里两者都以 user 文本出现：用户的带 `[USER] ` 前缀，后台的带 `[BACKEND] ` 前缀。后台消息可能是中间进度，也可能是最终结果。\n* **后台完成时你会收到一个工具返回。那个返回就是「做完了」的定义** —— 在收到它之前，这件事没有完成，你手上不存在任何可以据以宣布完成的依据。\n* 阅读器会自动推送条目（当前位置与页面内容、选中清单、地点与待办等）。**这些条目自带指示，照那条指示做就行，不必去分辨它属于哪一类、什么前缀。**写着「不要回应本条」就只记住、不出声；写着「照这条直接答」就直接答；写着「现在用语音说」「你来发起」就照做，那是要你现在开口。整条只有状态、没有任何指示时，默认只记住、不出声。同类条目**只认最新一条**，更早的一律作废。\n\n## 呈现结果\n\n* 后台在阅读器里产生的成果（卡片、高亮、笔记、翻页）是主表面。你只用一两句说关键结论、状态或下一步，不要复述卡片全文，不要念表格、代码块、结构化内容。\n* 收到那个工具返回之后，才可以说完成，并且要说得明确。说中文时固定用**「已经帮你……好了」**（例如「已经帮你把卡片做好了」）；**说别的语言时用那门语言里同样明确的完成句，不要为了凑这个说法而夹一句中文进来**。任何语言下都不要用「搞定」「弄好了」「OK 了」这类含糊说法。（系统核对事情做没做靠的是后台实际有没有跑过，不是你的措辞，所以语言以用户为准。）\n* 在那之前一律用进行时（中文例子：「我看一下」「还在做」；其它语言同理）。把没发生的事说成做完了，用户当场就会发现：2026-09-17 就是这样，他听见「做好了」，实际什么都没发生。\n* 后台的中间进度消息（「我先读取」「我核对一下」之类）不要念出来；一个问题只答一次，不要分成几段反复说。\n* 只有用户明确要求时才详细朗读后台内容。\n\n## 沟通方式\n\n* 请求明确就**直接去做**：不复述请求，不宣布计划，不加多余铺垫。转交后台时最多说一句过渡语（括号里是**中文的例子**：「我看一下」「稍等」；说别的语言时换成那门语言的对应说法，**不要把这句留成中文**），或者不说。\n* 避免无谓的旁白 —— 重复确认、填充语、再应一声、实况解说，一概不要。\n* **不要用一句应答代替动作**：说了「好的，我这就处理」却没把活交给后台，这件事就根本没有发生（2026-09-17 实录：答应之后 75 秒无事发生，用户追问才真派活）。你自己没有工具，凡是要动手的，说之前先交后台。\n* 进度更新默认只在简短、属实、确有用时才说。\n* 用户关于更新频率、详略、节奏、呈现方式的要求，是**这个任务持续有效的偏好**，不是只管一轮：直到任务结束或他改口为止都照办，不要因为来了一条新的后台消息就悄悄退回默认风格。\n\n## 通话\n\n* 你自己无法结束通话。用户告别、要求关掉语音、或事情已办完不需要再听回复时，把「结束语音会话」交给后台去做，不要声称已经关闭。\n",   # 阅读器版语音提示词（按官方 BACKEND_PROMPT 结构改写）；空 = 用 core 内置官方原文
    "userFirstName": "",   # 空 = 用 Windows 用户名；替换 prompt 里的 {{user_first_name}}
    "voiceAddendum": "",   # 附加 developer 条目（可选）；阅读器规则已并入 prompt
    "includeStartupContext": False,
    "handoffMode": "thinking",
    "clientManagedHandoffs": False,
    "codexResponsesAsItems": False,
    "delegationAckFiller": None,
    # 热设置：thread/settings/update 立即生效
    "backendModel": "gpt-6-astra",
    "effort": "medium",
    "serviceTier": "",
    # 重连
    "autoReconnect": True,
    "maxReconnects": 20,
    "reconnectInitialItems": 8,
    "autoStartSession": False,
    # 用户口头说"关掉语音/挂断"时由运行器真的关（先应一句再关）
    # 快板（2026-09-14）：固定前缀的静默上下文更新
    "boardPrefix": "【快板】",
    "boardSilentRule": "以「【快板】」开头的开发者消息是阅读器自动推送的条目。按条目里写的指示做：没有任何指示、只是状态（地点、页码、焦点）时保持静默 —— 不要出声，不要说「收到」「知道了」之类，也不要复述；但写明要你做什么的（例如待办后面跟着「还没跟他说过，现在用语音说」「这条要打电话，你来发起」），那就是要你现在照做，不在静默范围内。同类只认最新一条。",
    # 2026-09-18 用户：「以现在的注入方式 AI 根本不需要纠结快板还是慢板，
    # 他只需要根据收到的内容的指示直接动作」。这三条预置项（静默约定 + 一组
    # user/assistant 示范「【快板】开头的更新我一个字都不说」）现在是**有害**的：
    # 纯快板早就不进语音侧了（board_skip_injector 7629 次），真送进来的是慢板，
    # 而慢板的内容恰恰常常写着「还没跟他说过，现在用语音说」——
    # 一条要求开口的消息，戴着"一个字都不说"的帽子。实录 6 条这样的推送里只有 2 条
    # 真说出去了，4 条没有：这种摇摆就是两条指令打架的样子。
    # 改由 prompt 里"按条目自带的指示做"统一处理，这三条不再下发。
    "boardInitialItems": False,
    "boardToVoice": True,
    # 语音侧送达时机：on-speech = 用户开口时才追加（确定性静默，推荐）；immediate = 立刻追加（闲时会招一句"收到"）；off = 不送语音
    "boardVoiceMode": "on-speech",
    "boardToBackend": True,
    "boardCoalesceSeconds": 1.5,
    # 后台线程一建立就带上的 developer 指令（thread/start.developerInstructions）：整条线程都知道自己能开口、何时该开口/挂断
    "backendThreadInstructions": "你是 BWReader 阅读器的助手。用户在 iPad 上看书（PDF/EPUB），他的语音（经语音模型委派）和侧栏打字都会到你这里，由你实际完成事情。【当前阅读状态】是运行器自动注入的**事实**：书名、页码、选中了几项、每项的类型与开头几个字，还有时刻。它带时刻是因为旧的那些删不掉：**只认时刻最新的一条**，更早的一律当作废。选中项的编号（1、2、3）与语音侧看到的是同一套，所以他说「第 2 项」你就按这个编号认。⚠ **目标以他话里明确说出的词为准，不是以注入里最新的那个选中为准。**注入的选中/最新操作只有在他用指代词时（「这个」「刚选的」、日语的「これ」「いま選んだ」）才是目标。选中随时可能是**误触**产生的，它的时刻比他的请求还新，也**不代表**它就是他要的东西 ——2026-09-19 实录：他一直在说「百日せき」，请求发出的同时最新选中变成了「接種」，卡片就做到了错的词上。两者对不上时，按他说出来的词做；实在分不清就先问一句，不要默认相信更新的那个。注入里**只有开头几个字，没有全文** —— 这是有意的：他反复改选中时，全文一次次进来只会把线程撑大。选中的文字在**注入的正文里**用 ⟦SELECTED n=K⟧…⟦/SELECTED⟧ 标了出来（编号同上），卡片则是正文里原有的 ⟦CARD_START n=… id=…⟧ —— 要一字不差的原文，**先在正文里按标记取**，这是最省的一条路。正文里找不到（不在本页、或本页正文这次没给）才调 reader_context_snapshot 按编号取。正文有时会写着「某段刚才已经给过」——那是本轮对话里更早给过的同一段，往上翻就有，别为此调工具。页上的卡片**连内容带 id 就嵌在正文里**（⟦CARD_START n=… id=… revision=… label=…⟧…⟦CARD_END⟧），所以绝大多数时候根本不必查卡片：要改哪张、要引用哪张，直接从正文里按 id 取。真要单独取一张就用 reader_page_card_read 按 id 取；**不要用 reader_page_cards 把整页倒出来**（一次几千字，而且同一轮里读第二遍毫无新信息）。同一轮内已经读过的东西不要再读一遍。什么时候必须取全文：拿原文去定位的活（做卡 bind、钉卡、按文字建便签）。什么时候不用取：划线选区直接 at={\"selection\":true}；委派过来的话里已经带了内容且够用；只是回答、概括、判断这类不落到原文上的事。别为了「确认一下」白跑一趟工具。工具：reader_highlight_range 划线（选区用 at={\"selection\":true}，别处用 at={block,text}）；reader_card 做卡/钉卡 —— **整个参数就是 {card:{...}} 这一个字段**，卡片本身必须是 {kind, title, data, bind?}（title 必填，漏了会被拒；bind 直接写 {kind:\"page-chars\",page,text:<原文>}）；reader_anki_draft 做 Anki 卡（它要的 nodeIds 用 kj_node_ensure 一步拿到：按名称找，有就复用、没有就新建，不要自己跑脚本分两步）；reader_note_create / reader_note_edit 便签；reader_visual_image 看页面或笔迹；reader_page_text 读别的页；reader_command / reader_browser_control 翻页与浏览；**每个工具的参数表已经在它自己的说明里写全了 —— 要参数先看那里，不要为此多跑一轮工具。**reader_card 的 data 按 kind 取：weather={lo,hi,cond,loc?,date?,precip?,tip?}、news={items:[{t,s?,src?}]}、images={items:[{url,title?,aid?,src?}]}（url 必须是直出图片字节的 HTTPS 地址，不是含图网页；问「某地在哪」用地图图片卡）、videos={items:[{title,thumb?,url?,channel?,src?}]}（完整 YouTube/Bilibili 观看链接，别编 id）、fact={answer,detail?}、general={text?}。reader_capability_guide 只在工具自己的说明里确实查不到时才调、一次传一个工具名 —— **绝不要在代码模式里把 ALL_TOOLS 或它的子集整个序列化出来**：2026-09-17 实测一次这样的调用吐了 23129 个 token（截断后仍有 39380 字），而这些全是不走缓存的新增输入。真要在 ALL_TOOLS 里找，只打印名字，别带 description 和 schema。做事就直接调工具，不要只口头描述。做事的时候不要输出「我先读取这页」「我核对一下」这类中间说明，工具调完直接给最终结果；一轮只说一次。语音工具：voice_say 立刻念一句、voice_tell 塞进语音上下文、voice_session_start 开语音、voice_session_stop 挂断（默认等念完）、voice_transcript 看最近几句语音对话（带时刻）。**分工：语音模型只负责播报，判断和决定都在你这边。**⚠⚠ **他用语音问你的那一轮，你的回答会被自动念给他 —— 这种时候不要调 voice_say。**2026-09-21 实录：他问了一句，却听见三句（语音的「我查一下」+ 你中途写的一段 + 你 voice_say 的一段）。所以委派轮里：**整轮只写一条文字回答**，写在最后，中途不要先写一段再去调工具 —— **你写的每一段文字都会被念出来**，中途那段就是他听见的第二遍；万一中途必须说一句，只用「正在…」「这就去…」这类进行时，别写「把X补到/发到/做好」这种读起来像已完成的句子 —— 语音会把它念成「已经做好了」（2026-09-26 实录：卡片还没做，他先听到「已经补到 Reader 了」）；「已经」只在工具回执 ok 之后说；voice_say 只用于**没人问你的时候** —— 通知、定时提醒到期、你主动找他。（这条现在是硬闸：委派轮里调 voice_say 会被拒，返回里会告诉你原因。）voice_say 的 text 要写成**直接可念的原话**，并且**用用户当前说话的语言写**（他在说日语就写日语，别中外夹杂），不要写成让它转述的指示（写成指示它就会自己组织措辞、自己替你回应）。念完之后不要凭 voice_say 的返回就当事情办完了 —— 那只说明投递成功，不代表他听见了、更不代表他回应了。用 voice_transcript 看这之后的几句：他回应了就按他的话走；他没回应就自己判断是再说一遍、改打电话、还是先收尾挂断。以「【快板】」开头的 developer 条目是阅读器推送的状态，不是用户发言，不必回应。用户在侧栏打的字会以他本人的原话直接到你这里（不经语音模型），当作他对你说的话处理。要在指定时间打电话提醒他（起床、关火、出门）：schedule_create，schedule 用 {type:once, at:本地时间 ISO}，steps 只要一步 {id:'ring', deliver:{mode:'call', title:'一句话', text:'接通后念的话'}}；现在就要打用 voice_call。电话会真的响铃并把 iPad 切到前台，只用于必须马上知道的事，普通提醒用 deliver mode=notify。收到「【定时提醒到期】」「【通知】」时，需要用户马上知道的用 voice_session_start + voice_say 说出来。通话的开与关由你负责：说完且不需要回复就 voice_session_stop；用户告别或要求关语音也由你调它。要把一段跑通的多步流程固化成可复用的能力（用户说「存成工具」「以后都这么做」「做个自动的」）：**一律用既有的 flow 格式 bw-reader-skill-flow/1，不许另起炉灶**。一份 flow.json 里写 steps（每步恰好是 command / tool / needs_ai / deliver 之一）、用 {\"$from\": 步骤id, \"path\": …} 引用更早步骤的输出（不能引用更晚的），再加一段描述头：name / when（什么时候用）/ does（能做到什么）/ params（参数接口）。写完必须跑 skill_kit/bw_skill_build.py 用真实轨迹校验 + 干跑，**过了才算做完**；没过就改到过，不要交一个没验证的说明文档。这样做的理由：同一份 flow 会被自动脚本转成 skill 或 MCP 工具、被定时任务直接按步跑、并经 reader_flow_progress 在侧栏画进度点 —— 自己发明的格式这三样一样都接不上。",
    # 会话开始时给后台模型的 developer 指令：通话由它管生死
    "backendStartInstructions": (
        "语音会话已开始。你有 voice_core 工具：voice_status / voice_say / voice_tell / voice_session_stop / voice_session_start。"
        "通话的开与关由你负责：用户告别或要求结束、事情已经办完且不需要再听回复、提醒已送达且用户没有接话、长时间无人说话——"
        "这些情况都应主动调用 voice_session_stop（默认等当前那句念完再挂）。语音模型自己没有关闭通话的能力，它说'已关闭'不算数。"
    ),
}
#: 已退役的提示词句子 → 替换句。load_settings 用它迁移盘上 settings.json 里的旧整段提示词。
_RETIRED_PROMPT_SENTENCES = (
    ("prompt",
     "用户可以对你说话，也可以在侧栏打字（打字的内容以「【用户打字】」开头到达，当作用户说的话）。",
     "用户可以对你说话，也可以在侧栏打字（打字直接交给后台处理）。"),
    ("backendThreadInstructions",
     "以「【用户打字】」开头的是用户在侧栏打的字，按用户发言处理。",
     "用户在侧栏打的字会以他本人的原话直接到你这里（不经语音模型），当作他对你说的话处理。"),
)
#: 整段存进 settings.json 的提示词。跟随代码默认更新的规则见 load_settings。
PROMPT_KEYS = ("prompt", "backendThreadInstructions", "backendStartInstructions",
               "realtimeEndInstructions", "boardSilentRule")


def _prompt_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _prompt_sentences(text: str) -> list:
    return [x.strip() for x in re.split(r"(?<=[。；！？])", text) if x.strip()]


def _mostly_default(value: str, default: str) -> bool:
    mine = _prompt_sentences(value)
    known = set(_prompt_sentences(default))
    return bool(mine) and sum(x in known for x in mine) / len(mine) >= 0.8


HOT_KEYS = {"backendModel", "effort", "serviceTier"}
COLD_KEYS = {"version", "voice", "realtimeModel", "prompt", "voiceAddendum", "userFirstName", "includeStartupContext", "handoffMode", "clientManagedHandoffs",
             "realtimeEndInstructions", "realtimeStartInstructions", "flushTranscriptTailOnSessionEnd", "codexResponseItemPrefix",
             "codexResponsesAsItems", "delegationAckFiller", "inputDevice", "outputDevice", "outputRate", "gain", "backendStartInstructions", "backendThreadInstructions", "boardPrefix", "boardSilentRule", "boardInitialItems", "boardVoiceMode", "appInputDevice", "appOutputDevice"}


def clean(s) -> str:
    return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", str(s))[:1500]


def pick_device(name: str, want: str):
    if not name:
        return None
    apis = sd.query_hostapis()
    best = None
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_input_channels"] if want == "in" else d["max_output_channels"]
        if ch <= 0 or name not in d["name"]:
            continue
        rank = {"Windows WASAPI": 0, "Windows WDM-KS": 1, "MME": 2}.get(apis[d["hostapi"]]["name"], 3)
        if best is None or rank < best[0]:
            best = (rank, i)
    if best is None:
        raise RuntimeError("找不到音频设备: " + name)
    return best[1]


class MicTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, device_name: str):
        super().__init__()
        self.q: queue.Queue = queue.Queue(maxsize=100)   # 2 s；满了说明消费端卡住
        self.pts = 0
        self.level = 0.0
        self.drops = 0          # 队列满丢掉的 20 ms 块
        self.status_flags = 0   # PortAudio 报 overflow 等状态的次数
        idx = pick_device(device_name, "in")
        self.rate = RATE
        self.resampler = None
        try:
            self.stream = sd.InputStream(device=idx, samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=self._cb)
        except Exception:
            # 设备不认 48 kHz（部分 USB 麦 / HDMI 只给默认采样率）→ 按它的默认率开，送轨前重采样到 48 kHz
            # idx 为 None（设备名留空 = 系统默认）时必须带 kind：不带的话 query_devices(None)
            # 返回的是**全部设备的列表**，再按 "default_samplerate" 取就是 tuple 下标报错（Mac 实测）。
            self.rate = int(sd.query_devices(idx, "input")["default_samplerate"])
            self.stream = sd.InputStream(device=idx, samplerate=self.rate, channels=1, dtype="int16",
                                         blocksize=int(self.rate / 50), callback=self._cb)
            self.resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        try:
            self.q.put_nowait(bytes(indata))
        except queue.Full:
            self.drops += 1
        if status:
            self.status_flags += 1
        arr = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float32)
        self.level = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0

    async def recv(self):
        while True:
            data = await asyncio.get_running_loop().run_in_executor(None, self.q.get)
            arr = np.frombuffer(data, dtype=np.int16).reshape(1, -1)
            frame = AudioFrame.from_ndarray(arr, format="s16", layout="mono")
            frame.sample_rate = self.rate
            if self.resampler is not None:
                out = self.resampler.resample(frame)
                if not out:
                    continue
                frame = out[0]
                if len(out) > 1:  # 极少见：一次进多帧，余下的塞回队列前面不值得，直接拼起来
                    merged = np.concatenate([np.frombuffer(bytes(f.planes[0])[: f.samples * 2], dtype=np.int16) for f in out]).reshape(1, -1)
                    frame = AudioFrame.from_ndarray(merged, format="s16", layout="mono")
                    frame.sample_rate = RATE
            frame.pts = self.pts
            frame.time_base = fractions.Fraction(1, RATE)
            self.pts += frame.samples
            return frame

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class PipeMicTrack(MediaStreamTrack):
    """App 的麦克风，直接从桥的 UDP 收，不开任何采集设备。

    自己按 20 ms 对表出帧：桥没送来（App 静音、刚断、丢包）就补一帧静音，
    绝不让轨停住 —— 轨一停，整条 WebRTC 的时钟就乱了。
    """
    kind = "audio"

    def __init__(self, port: int):
        super().__init__()
        self.q: queue.Queue = queue.Queue(maxsize=100)
        self.pts = 0
        self.level = 0.0
        self.drops = 0           # 队列满丢掉的 20 ms 块（**消费端卡住**，真丢了）
        # ⚠ 跟 drops 分开数：削深是**有意的**（PIPE_MAX_DEPTH，压住延迟），
        #   跟「卡住导致丢帧」是两回事。混在一个数里，这个数就没法用来判断有没有问题 ——
        #   2026-09-20 我就差点把 micDrops=160 读成故障，其实几乎全是正常削深。
        self.trimmed = 0         # 为压延迟主动丢掉的最旧块（正常，不是故障）
        self.status_flags = 0    # 坏包（魔数/长度不对）
        self.silence = 0         # 没收到帧、补静音的次数
        self.gaps = 0            # 放着放着断流（缓冲见底）的次数
        self.received = 0
        self._primed = False
        self.rate = RATE
        self.closed = False
        self._next_at: float | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", int(port)))
        self.sock.settimeout(0.5)
        threading.Thread(target=self._rx, daemon=True).start()

    def _rx(self):
        while not self.closed:
            try:
                data, _ = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) <= PIPE_HEADER or data[:4] != PIPE_MAGIC:
                self.status_flags += 1
                continue
            pcm = data[PIPE_HEADER:]
            self.received += 1
            try:
                self.q.put_nowait(pcm)
            except queue.Full:
                self.drops += 1
                continue
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            self.level = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0

    async def recv(self):
        now = time.monotonic()
        if self._next_at is None:
            self._next_at = now
        self._next_at += BLOCK / RATE
        delay = self._next_at - now
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -0.2:
            self._next_at = time.monotonic()   # 落后太多（进程被卡住过）重新对表，别追赶式狂发
        # ## 抖动缓冲（2026-09-15 首次实拨后加的）
        #
        # 第一次实拨：收 12407 帧、补静音 2364、丢弃 2431 —— 上行是"一阵一阵"到的。
        # 原因是原来那根虚拟声卡**本身就是个抖动缓冲**（WASAPI 环形缓冲在替网络兜底），
        # 拿掉线缆的同时把它也拿掉了，而这里只留了 80 ms 余量：来一串就丢、随后空档就补静音。
        # 现在按 Speaker 那套验证过的做法：先攒够 prefill 再开始放，容量放到 240 ms，
        # 空了先让出 8 ms 等一等（相位差多半就差这么点），实在没有才补静音。
        if not self._primed:
            if self.q.qsize() >= PIPE_PREFILL:
                self._primed = True
            else:
                self.silence += 1
                self.level = 0.0
                return self._frame(b"\x00" * PIPE_PAYLOAD)
        while self.q.qsize() > PIPE_MAX_DEPTH:
            try:
                self.q.get_nowait()
                self.trimmed += 1
            except queue.Empty:
                break
        try:
            data = self.q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.008)
            try:
                data = self.q.get_nowait()
            except queue.Empty:
                data = b"\x00" * PIPE_PAYLOAD
                self.silence += 1
                self.level = 0.0
                self._primed = False   # 断流了：下次重新攒，别一帧一帧地跟着抖
                self.gaps += 1
        return self._frame(data)

    def _frame(self, data: bytes):
        if len(data) != PIPE_PAYLOAD:
            data = (data + b"\x00" * PIPE_PAYLOAD)[:PIPE_PAYLOAD]
        frame = AudioFrame.from_ndarray(np.frombuffer(data, dtype=np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, RATE)
        self.pts += frame.samples
        return frame

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


class PipeSpeaker:
    """说给 App 的声音：切成 20 ms 定长包用 UDP 投给桥，不开任何播放设备。

    字段与 Speaker 对齐（gaps/underruns/status_flags/buf/out_rate），
    /status 的 audio_stats 不用为它分叉。
    """

    def __init__(self, port: int, gain: float):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.out_rate = RATE
        self.gain = gain
        self.played = 0
        self.gaps = 0
        self.underruns = 0
        self.status_flags = 0     # 发送失败次数
        self.sent = 0
        self.seq = 0
        self.addr = ("127.0.0.1", int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.resampler = AudioResampler(format="s16", layout="mono", rate=RATE)

    def feed(self, frame):
        for f in self.resampler.resample(frame):
            b = bytes(f.planes[0])[: f.samples * 2]
            if self.gain != 1.0:
                b = np.clip(np.frombuffer(b, dtype=np.int16).astype(np.float32) * self.gain, -32768, 32767).astype(np.int16).tobytes()
            with self.lock:
                self.buf.extend(b)
                while len(self.buf) >= PIPE_PAYLOAD:
                    chunk = bytes(self.buf[:PIPE_PAYLOAD])
                    del self.buf[:PIPE_PAYLOAD]
                    self._send(chunk)

    def _send(self, chunk: bytes):
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        try:
            self.sock.sendto(PIPE_MAGIC + self.seq.to_bytes(4, "little") + chunk, self.addr)
            self.sent += 1
            self.played += BLOCK
        except OSError:
            self.status_flags += 1

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Speaker:
    def __init__(self, device_name: str, out_rate: int, gain: float):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.out_rate = out_rate
        self.gain = gain
        self.played = 0
        # 空了以后不再"来一帧放一帧"（每次空一帧就补零 → 一串爆音），先攒 prefill 再放：
        # 网络抖动变成一次干净的短停顿。代价是每次开口多 80 ms 延迟。
        self.primed = False
        self._empty_at = 0.0
        self.gaps = 0        # 放着放着断了又很快续上（<300 ms）= 抖动造成的一次断音
        self.underruns = 0   # 回调要的比缓冲里有的多（半帧）
        self.status_flags = 0
        idx = pick_device(device_name, "out")
        # idx 为 None = 系统默认输出；带上 kind 才拿到那一台设备，而不是全部设备的列表
        default_rate = int(sd.query_devices(idx, "output")["default_samplerate"] or 48000)
        candidates = [r for r in (out_rate, default_rate, 48000, 44100) if r]
        last_err: Exception | None = None
        for rate in candidates:
            try:
                self.stream = sd.OutputStream(device=idx, samplerate=rate, channels=1, dtype="int16",
                                              blocksize=int(rate / 50), callback=self._cb)
                self.out_rate = rate
                break
            except Exception as e:  # 该设备不认这个采样率，试下一个
                last_err = e
        else:
            raise RuntimeError(f"输出设备打不开（试过 {candidates}）：{last_err}")
        self.resampler = AudioResampler(format="s16", layout="mono", rate=self.out_rate)
        self.prefill = int(self.out_rate * 2 * 0.08)   # 80 ms
        self.stream.start()

    def _cb(self, outdata, frames, t, status):
        need = frames * 2
        if status:
            self.status_flags += 1
        with self.lock:
            if not self.primed and len(self.buf) >= self.prefill:
                self.primed = True
            if self.primed:
                chunk = bytes(self.buf[:need])
                del self.buf[:need]
            else:
                chunk = b""
        if len(chunk) < need:
            if chunk:
                self.underruns += 1
            if self.primed:
                self.primed = False
                self._empty_at = time.monotonic()
            chunk += b"\x00" * (need - len(chunk))
        outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)
        self.played += frames

    def feed(self, frame):
        for f in self.resampler.resample(frame):
            b = bytes(f.planes[0])[: f.samples * 2]
            if self.gain != 1.0:
                b = np.clip(np.frombuffer(b, dtype=np.int16).astype(np.float32) * self.gain, -32768, 32767).astype(np.int16).tobytes()
            with self.lock:
                if self._empty_at and not self.primed and not self.buf:
                    if time.monotonic() - self._empty_at < 0.3:
                        self.gaps += 1   # 刚断就续上：不是说完了，是抖了一下
                    self._empty_at = 0.0
                self.buf.extend(b)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class AppServer:
    """codex app-server 的 JSON-RPC 客户端（stdio）。通知回调给 Runner。"""

    def __init__(self, exe: str, mcp_disable: list[str], on_notification, disable_official_plugins: bool = False):
        self.exe = exe
        self.mcp_disable = mcp_disable
        self.disable_official_plugins = disable_official_plugins
        self.on_notification = on_notification
        self.pending: dict[int, asyncio.Future] = {}
        self.count = 0
        self.proc = None
        self.stderr_tail: deque = deque(maxlen=50)

    async def launch(self):
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")}
        # ⚠ 2026-09-18 撤销专用 CODEX_HOME（用户拍板）。两个理由：
        #   ① 它唯一还能省的只有 AGENTS.md 约 8K —— 真正的大头（插件 skill）已在**账号级**
        #      解决（codex plugin remove 掉 data-analytics / app-6a3293e… / openai-developers，
        #      实测开局 37754 → 27704 字），主 home 同样受益，不需要分叉；
        #   ② 我说的「sessions 共享所以不丢记忆」**是错的**：第一版白名单时 Codex 已在专用 home
        #      建了真的 sessions 目录，第二版重建时 make_junction 见"已存在"就跳过 ——
        #      于是 sessions / sqlite / skills 各存一份。之前那次 thread_resume_failed 正是这样来的。
        #   省 8K 换一整套分叉状态，不划算。sync_slim_codex_home 保留但不再调用。
        # 给语音这条线程封存用不到的 Codex 自带样板。用 -c 按次覆盖，不动 config.toml。
        # ⚠ 2026-09-17 A/B 实测更正：`plugins."X".enabled=false` 与 `project_doc_max_bytes=0`
        #   **都不生效** —— 加与不加，开局一字不差（37754/37754）。二进制里有一块
        #   「Fixed defaults for packaged Codex clients」把 project_doc_max_bytes 等键写死，
        #   用户的 -c 盖不过去。所以 2026-09-16 注释里「每轮省 25,600 字」是过度声称，
        #   这条路从来没省下过。真正能砍掉插件 skill 与 AGENTS.md 的只有换 CODEX_HOME
        #   （见 SLIM_CODEX_HOME）。features.memories=false 保留 —— 它是另一个键，未经此次证伪。
        args = [self.exe, "-c", 'forced_login_method="chatgpt"', "-c", "features.memories=false"]
        if self.disable_official_plugins:
            args += ["-c", "features.plugins=false"]
        for plugin in SLIM_PLUGINS:
            args += ["-c", 'plugins."%s".enabled=false' % plugin]
        for n in self.mcp_disable:
            args += ["-c", f"mcp_servers.{n}.enabled=false"]
        args += ["app-server", "--listen", "stdio://"]
        # 运行器被 ReaderPC 无控制台拉起时，codex.exe 这种控制台程序会自己弹一个黑窗（用户："总会有一个终端被启动很碍眼"）
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE, env=env, cwd=str(BASE),
                                                         limit=64 * 1024 * 1024,
                                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        asyncio.create_task(self._read())
        asyncio.create_task(self._drain())
        await self.call("initialize", {"clientInfo": {"name": "bw_voice_cli", "version": "0.1"}, "capabilities": {"experimentalApi": True}})
        await self.write({"method": "initialized"})

    async def _drain(self):
        while self.proc and (line := await self.proc.stderr.readline()):
            s = line.decode(errors="replace").strip()
            self.stderr_tail.append(s)
            if "ERROR" in s:
                await self.on_notification("app_server_stderr", {"message": clean(re.sub(r"\x1b\[[0-9;]*m", "", s))[-300:]})

    async def write(self, msg: dict):
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, method: str, params: dict, timeout: float = 60):
        self.count += 1
        i = self.count
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.write({"id": i, "method": method, "params": params})
        d = await asyncio.wait_for(fut, timeout)
        if "error" in d:
            raise RuntimeError(f"{method}: {clean(json.dumps(d['error'], ensure_ascii=False))}")
        return d.get("result", {})

    async def _read(self):
        while self.proc and (line := await self.proc.stdout.readline()):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "id" in d and "method" not in d:
                fut = self.pending.pop(d["id"], None)
                if fut and not fut.done():
                    fut.set_result(d)
                continue
            m = d.get("method", "")
            p = d.get("params") or {}
            if "id" in d:
                # 服务端反向请求：只放行阅读器工具的审批，其余拒绝
                blob = json.dumps(p, ensure_ascii=False)
                ok = "reader_" in blob
                await self.on_notification("server_request", {"method": m, "approved": ok, "detail": clean(blob)[:200]})
                await self.write({"id": d["id"], "result": {"decision": "accept" if ok else "decline"}})
                continue
            await self.on_notification(m, p)
        await self.on_notification("app_server_exited", {})

    async def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass


# 统一错误日志：%LOCALAPPDATA%\BWReader\error-log.jsonl。
# 桥、运行器、阅读器页面都往这一个文件里写 —— **只有一个**，分散等于没有。
# ⚠ BASE 是 …\BWReaderoice-cli，而桥写的是 …\BWReader\error-log.jsonl。
#   用 BASE 会造出第二个文件，"统一日志"当场变成两份 —— 取 BASE.parent 才是同一个。
ERROR_LOG_PATH = BASE.parent / "error-log.jsonl"


def error_log(source: str, code: str, message: str, detail: str = ""):
    """记一条错误。写日志本身失败绝不向上抛：它是旁路。"""
    try:
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": str(source)[:40],
               "code": str(code)[:200], "message": str(message)[:600]}
        if detail:
            row["detail"] = str(detail)[:2000]
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + chr(10))
    except Exception:   # noqa: BLE001
        pass


def stream_owner(backend_turn_id, voice_turn_id, prev):
    """这段话的流该投进哪个容器；返回 (owner, 要清空草稿的旧容器或 None)。

    规则是**单向**的：v- → 后台轮可以改投，后台轮 → v- 绝对不行。

    ⚠ 2026-09-18 实录 seq83 就是反向那一下：这句话本来正投在后台轮里，turn/completed
      一到 backend_turn_id 变 None，下一个 delta 就把它甩进 v- 独立框 —— 用户看到的
      「AI 说的话跑到工具卡外面」。落库那条路是对的（并进了后台轮），所以刷新后又回去，
      于是表现成"中途在外面、刷新才进去"，更难查。
    ⚠ 判据不是"那轮结束没有"：一句话一旦属于某次后台任务，它就一直属于那次。
    """
    own = backend_turn_id or voice_turn_id
    if prev and prev != own:
        if not str(prev).startswith("v-"):
            return prev, None          # 已经在后台轮里 —— 不许往外搬
        return own, prev               # v- → 后台轮：改投，并清掉旧草稿
    return own, None


class VoiceTranscriptStreams:
    """One identity per RTC turn/role; stdio done completes a segment, not a turn."""

    def __init__(self, session):
        self.session = session
        self.entries = {}
        self.current = {}
        self.sources = {}

    def start(self, role, source=None):
        if role not in ("user", "assistant"):
            return None
        source = str(source) if source else None
        if source and (role, source) in self.sources:
            return self.entries.get(self.sources[(role, source)])
        entry = self.entries.get(self.current.get(role))
        if entry and not entry["final"] and entry["source"] is None:
            entry["source"] = source
        else:
            identity = hashlib.sha256((str(self.session) + ":" + role + ":" +
                                       (source or str(time.time_ns()))).encode()).hexdigest()[:24]
            mid = ("vu-" if role == "user" else "v-") + identity + (".u" if role == "user" else "")
            entry = {"id": mid, "role": role, "source": source, "segments": [],
                     "delta": "", "text": "", "final": False, "owner": None,
                     "boundary": False}
            self.entries[mid] = entry
            self.current[role] = mid
        if source:
            self.sources[(role, source)] = entry["id"]
        while len(self.entries) > 128:
            removed = next(iter(self.entries))
            self.entries.pop(removed)
            self.sources = {key: value for key, value in self.sources.items() if value != removed}
        return entry

    def get(self, role, source=None):
        if source:
            existing = self.entries.get(self.sources.get((role, str(source))))
            if existing:
                return existing
            return self.start(role, source)
        return self.entries.get(self.current.get(role)) or self.start(role, source)

    def seed(self, role, text, source=None):
        """数据通道 turn.created 自带的**第一段**转写。

        ⚠ 之后 stdio 的 transcript/delta 从第二段开始 —— 不把这段放进来，侧栏的流式
        字幕就永远缺开头（实录 2026-09-23：turn.created 带「现在」，delta 只有「如何」，
        侧栏先显示「如何」，定稿后才变「现在如何」；用户：「最前面两个字丢失」）。
        万一哪天 delta 也从头给，靠 seed_pending 去掉重叠，不会出现「现在现在如何」。
        """
        entry = self.get(role, source)
        text = str(text or "")
        if not entry or entry["final"] or not text or entry["delta"] or entry["segments"]:
            return None
        entry["delta"] = text[:32000]
        entry["seed_pending"] = text
        entry["text"] = entry["delta"]
        return entry

    def delta(self, role, text, source=None):
        entry = self.get(role, source)
        if not entry or entry["final"] or not text:
            return None
        text = str(text)
        pending = entry.get("seed_pending") or ""
        if pending:
            if text.startswith(pending):
                text = text[len(pending):]
                entry["seed_pending"] = ""
            elif pending.startswith(text):
                entry["seed_pending"] = pending[len(text):]
                return entry
            else:
                entry["seed_pending"] = ""
        if not text:
            return entry
        entry["delta"] = (entry["delta"] + text)[:32000]
        entry["text"] = "\n".join(entry["segments"] + [entry["delta"]])[:32000]
        return entry

    def segment(self, role, text, source=None):
        entry = self.get(role, source)
        if not entry or entry["final"] or not text:
            return None
        entry["segments"].append(str(text)[:32000])
        entry["delta"] = ""
        entry["text"] = "\n".join(entry["segments"])[:32000]
        return entry

    def finish(self, role, text=None, source=None):
        entry = self.get(role, source)
        if not entry or entry["final"]:
            return None
        if not text and entry["delta"]:
            # RTC can close before stdio delivers the corrected segment. Do not seal a partial.
            entry["boundary"] = True
            return None
        if text:
            entry["text"] = str(text)[:32000]
        if not entry["text"]:
            entry["boundary"] = True
            return None
        entry["final"] = True
        return entry


class Runner:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        BASE.mkdir(parents=True, exist_ok=True)
        self.settings = self.load_settings()
        self.events: deque = deque(maxlen=2000)
        self.seq = 0
        self.started_at = time.time()
        self.app: AppServer | None = None
        self.thread_id: str | None = None
        self.session_id: str | None = None
        self.session_no = 0
        self.last_activity_at: float | None = None   # 最后一次"真人在用"的时刻，见 mark_activity/idle_stop_loop
        if getattr(self, "_prompt_adopted", None):
            # 出声：跟随了新默认的提示词要记一笔，写回盘上（带 promptBase 指纹）。
            self.log("prompt_default_adopted", keys=list(self._prompt_adopted))
            try:
                self.save_settings()
            except Exception as error:
                self.log("prompt_default_save_failed", message=clean(error))
        self._last_activity_what = ""
        self.reconnects = 0
        self.session_state = "idle"  # idle | starting | connected | reconnecting | stopping
        self.session_started_at: float | None = None
        self.stop_requested = False
        self.pc = None
        self.mic: MicTrack | None = None
        self.speaker: Speaker | None = None
        self.dc = None
        self.tasks: list[asyncio.Task] = []
        self.remote_sdp: asyncio.Future | None = None
        self.transcripts: deque = deque(maxlen=200)
        self.usage = {"audioDurationMs": 0, "rateLimits": None, "usageSummary": None, "updatedAt": None}
        self.user_speaking = False
        self.assistant_speaking = False
        self.last_assistant_done = 0.0
        self.pending_speech_until = 0.0
        self._board_latest: tuple = ("", None, None)
        self._board_task: asyncio.Task | None = None
        self._board_last_sent = ""
        self._board_pending_voice: str | None = None
        self._board_voice_sent = ""
        self.last_error: str | None = None
        self.backend_busy = False
        self.pending_cold: set[str] = set()
        self.reconnect_task: asyncio.Task | None = None
        self.session_profile = "local"
        self.profile_before_switch: str | None = None
        self._closed_event = asyncio.Event()
        self.app_server_exits = 0
        self.app_relaunch_task: asyncio.Task | None = None
        self.shutting_down = False
        # 助手历史：语音侧最近一句用户话（配对语音回复 / 委托轮的用户句），后台轮的用户句，正在进行的后台轮
        self._voice_pending_user: tuple[float, str] | None = None
        self._pending_turn_user: str | None = None
        self._turn: dict | None = None
        self.history_stats = {"written": 0, "errors": 0, "lastError": None, "streamed": 0}
        # 流式：语音侧当前这轮的 id / 已累计的回复文本；历史写入走单工作线程队列，保证先后顺序
        self._voice_turn_id: str | None = None
        self._last_compact_at: float = 0.0
        self._ctx_pending: dict | None = None   # 后台忙时压着的状态，只留最新一份
        self._last_user_ask: tuple | None = None
        self._promise_pending: tuple | None = None
        self._notify_sent: dict[str, float] = {}   # 通知主动投递的冷却台账
        self._notify_tries: dict[str, int] = {}   # 每条通知投了几次（见上限）
        self._delegation_seq = 0              # 累计委派次数（只增）
        self._delegation_open_at = 0.0        # 这一轮是语音委派来的：答案会被自动念出来
        self._last_backend_turn_id = None     # 上一条后台轮 id：轮外那句收尾语音认领用
        self._user_asks: list = []            # 最近几次用户发言 (时刻, 原话, 当时的委派序号)
        self._voice_user_stream = ""   # 用户说话的实时转写（见 transcript/delta）
        self._voice_user_turn_id: str | None = None  # 用户字幕身份不受助手完成/插话影响
        self._stream_role: dict[str, str] = {}   # 每轮草稿的角色（user / assistant）
        self._voice_stream = ""
        # 这段话的流正在往哪个容器投。⚠ 必须记住 —— 委派常发生在语音**还在说**的中途，
        #   每个 delta 各自重算目标的话，前半句进 v- 容器、后半句进后台轮容器，
        #   于是侧栏出现"两个相同内容上下放置、一起更新"（用户 2026-09-18 实录）。
        self._voice_stream_owner: str | None = None
        # 每个后台轮容器里累计的语音正文。**必须整份重发**：服务端按 origin 整组替换，
        # 只发最新那句 = 把同一轮里之前说过的话顶掉（2026-09-18 实录：一轮里两句语音
        # 都并进了 01a0b4e0，存储里却只剩一条 text:voice）。
        self._voice_parts: dict[str, list[str]] = {}
        # 委派之前说的那句（「好的，我看一下」）落成了独立记录；第一个工具调用时把它收进来。
        self._pre_turn_voice: tuple | None = None
        self._backend_recent: tuple[float, str] | None = None   # 后台最近一条回复：语音把它念出来的字幕不再重复入库
        self._voice_user_acc = ""   # 本轮用户字幕分段累积（turn.done 没带转写时兜底）
        self._voice_turn_commentary = False   # 这一轮语音回复是委托后台期间/之后的过渡或转述 → 不单独入库
        self._loop_lag_max = 0.0
        self._loop_lag_over = 0
        self._audio_stats_at = 0.0
        self._thread_cleared = False          # /thread/new：下次 ensure_app 不续接旧线程
        self._was_cleared = False             # 上一次开新线程是不是因为被显式清空
        self._resume_tried = False            # 上一次开新线程前试过续接没有
        self._ensure_lock = asyncio.Lock()    # ensure_app 串行化，见那边的注释
        self._voip_call_active = False        # 我们拨出去且已接通的 VoIP 电话还在（CallKit 那层）：挂媒体会话时要一并请 App 挂断
        self._thread_resume_target = None     # /thread/resume：下次 ensure_app 续接这个线程
        self._backend_done_at = 0.0
        # 上下文注入器状态：快照修订/页面停留起点/各 sink 已投指纹
        self._ctx = {"mtime": 0.0, "rev": None, "page_key": "", "page_since": 0.0, "snap": None,
                     "fp": {"backend_state": "", "backend_text": "", "voice": "", "amb": "", "image": ""}, "debounce": None,
                     # 正文记账：{(file, 页号): (\"full\"|\"part\", 时刻)} —— 见 _ctx_text_ledger
                     "sent_pages": {}}
        self._history_q: queue.Queue = queue.Queue()
        self._stream_latest: dict[tuple, dict] = {}
        self._stream_queued: set[tuple] = set()
        self._history_revision = 0
        self._stream_terminal = set()
        self._transcript_streams = None
        threading.Thread(target=self._history_worker, name="history-writer", daemon=True).start()
        self._artifact_resender = ArtifactResender(self)
        self._jev = JevContext(self.settings, self._ctx_snapshot, self._jev_dialogue,
                               self._jev_task_state, self.log,
                               artifact_source=latest_artifact, on_prepared=self._artifact_resender.prepared)
        self.log('artifact_resend_ready', mode='attempt_then_failure_only', cooldown_ms=4000)

    # ---------- 设置 ----------
    def load_settings(self) -> dict:
        s = dict(DEFAULTS)
        disk = {}
        try:
            disk = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            s.update(disk)
        except Exception:
            pass
        # ⚠ 提示词整段存在盘上，改了代码默认值盘上那份不会跟着变 —— 2026-09-26 查出
        #   09-21 加的「委派轮整轮只写一条回答」在这台机器上从没生效（盘上是更早的默认），
        #   于是后台中途写了句「我把卡片补到 Reader」，语音念成「已经补好了」。
        #   promptBase 记每段提示词「当初是哪一版默认」的指纹：盘上值 == 那一版 → 用户
        #   没改过，跟最新默认走；对不上 → 用户改过，保留。没有指纹的旧文件按没改过处理。
        base = disk.get("promptBase") if isinstance(disk.get("promptBase"), dict) else None
        self._prompt_adopted = []
        for key in PROMPT_KEYS:
            value = disk.get(key)
            if not isinstance(value, str) or value == DEFAULTS[key]:
                continue
            # 没有指纹的旧文件：只有它大体就是某一版旧默认（≥80% 的句子都在当前默认里）
            # 才跟随；用户自己写过的内容（默认里没有的句子）照旧保留，只做逐句迁移。
            stale_default = base is None and _mostly_default(value, DEFAULTS[key])
            if stale_default or (base is not None and base.get(key) == _prompt_fingerprint(value)):
                s[key] = DEFAULTS[key]
                self._prompt_adopted.append(key)
        # ⚠ 提示词是整段存进 settings.json 的，默认值改了盘上那份不会跟着变。
        #   2026-09-23 打字改为直接交后台、不再带「【用户打字】」前缀 —— 盘上旧提示词
        #   还在教模型认这个前缀，不迁移就等于没改。
        s.pop("typedPrefix", None)
        for key, old, new in _RETIRED_PROMPT_SENTENCES:
            if isinstance(s.get(key), str) and old in s[key]:
                s[key] = s[key].replace(old, new)
        return s

    def save_settings(self):
        base = dict(self.settings.get("promptBase") or {})
        for key in PROMPT_KEYS:
            if self.settings.get(key) == DEFAULTS[key]:
                base[key] = _prompt_fingerprint(DEFAULTS[key])
        self.settings["promptBase"] = base
        SETTINGS_PATH.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")

    async def update_settings(self, patch: dict) -> dict:
        changed = {k: v for k, v in patch.items() if k in DEFAULTS and self.settings.get(k) != v}
        self.settings.update(changed)
        if "jevContextEnabled" in changed and not self.settings.get("jevContextEnabled"):
            self._jev.reset()
        self.save_settings()
        hot = [k for k in changed if k in HOT_KEYS]
        cold = [k for k in changed if k in COLD_KEYS]
        if self.session_state in ("connected", "starting"):
            self.pending_cold.update(cold)
        applied = None
        if hot and self.thread_id and self.app:
            applied = await self.apply_hot()
        self.log("settings_changed", changed=list(changed), hot=hot, cold=cold, needsRestart=sorted(self.pending_cold))
        return {"settings": self.settings, "hotApplied": applied, "needsRestart": sorted(self.pending_cold)}

    async def apply_hot(self):
        params = {"threadId": self.thread_id, "model": self.settings["backendModel"] or None, "effort": self.settings["effort"] or None}
        if self.settings.get("serviceTier"):
            params["serviceTier"] = self.settings["serviceTier"]
        try:
            await self.app.call("thread/settings/update", params, timeout=20)
            self.log("hot_applied", model=params["model"], effort=params["effort"], serviceTier=params.get("serviceTier"))
            return params
        except Exception as e:
            self.log("hot_apply_error", message=clean(e))
            return {"error": clean(e)}

    # ---------- 事件 ----------
    @staticmethod
    def _log_body(text: str, limit: int = 8000) -> str:
        """给链路页看的注入正文。截到 limit 并标注 —— 链路页要能看清实际注入了什么，
        但 events.jsonl 不能被单条几万字撑爆（2026-09-16）。"""
        t = str(text or "")
        return t if len(t) <= limit else t[:limit] + ("…（已截断，共 %d 字）" % len(t))

    def log(self, kind: str, **d):
        self.seq += 1
        row = {"seq": self.seq, "t": round(time.time(), 3), "kind": kind, **d}
        # 每条都盖上当前线程 —— 链路页要按「选中的那条对话」看语音侧发生了什么，
        # 没有这个字段就只能按时间窗近似，换条对话就容易串（2026-09-16）。
        # d 里已经带了 threadId 的（比如线程管理那几条）不覆盖：那是它要说的那条。
        if self.thread_id and "threadId" not in row:
            row["threadId"] = self.thread_id
        self.events.append(row)
        try:
            with EVENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass
        print(json.dumps(row, ensure_ascii=False)[:300], flush=True)

    async def on_notification(self, m: str, p: dict):
        try:
            source_thread = p.get("threadId") or p.get("thread_id")
            if source_thread and source_thread != getattr(self, "thread_id", None):
                return
            if m == "thread/realtime/sdp":
                if self.remote_sdp and not self.remote_sdp.done():
                    self.remote_sdp.set_result(p["sdp"])
            elif m == "thread/realtime/started":
                self.session_id = p.get("realtimeSessionId")
                self.log("realtime_started", version=p.get("version"), sessionId=self.session_id)
            elif m == "thread/realtime/error":
                self.last_error = clean(p.get("message"))
                self.log("realtime_error", message=self.last_error)
                if self.remote_sdp and not self.remote_sdp.done():
                    self.remote_sdp.set_exception(RuntimeError(self.last_error))
            elif m == "thread/realtime/closed":
                reason = p.get("reason")
                self.log("realtime_closed", reason=reason)
                self._closed_event.set()
                if self.session_state == "starting":
                    # 上一场的 closed 迟到了（停完马上又开）：不能拆正在建立的新会话
                    self.log("realtime_closed_ignored", reason=reason)
                else:
                    await self.on_closed(reason)
            elif m == "thread/realtime/transcript/done":
                role, text = p.get("role"), p.get("text")
                self.transcripts.append((time.time(), role, text))
                self.log("transcript", role=role, text=text)
                self._promise_watch(role, text)
                if self._subtitle_mode():
                    self._transcript_segment(role, text, self._transcript_source(p))
                # 历史按"轮"写（数据通道 turn.done 带整轮转写），这里的分段只累积：一句话会拆成好几段，
                # 用户插话时更是交错到达 —— 按段写就是 2026-09-14 那种碎片对话（用户实测）。
                elif role == "user" and text:
                    self._voice_user_acc = (self._voice_user_acc + " " + text).strip()
            elif m == "thread/realtime/transcript/delta":
                if self._subtitle_mode():
                    self._transcript_delta(p.get("role"), p.get("delta"), self._transcript_source(p))
                    return
                if p.get("role") == "user" and p.get("delta"):
                    if self._voice_user_turn_id is None:
                        self._voice_user_turn_id = "vu-" + str(time.time_ns()) + ".u"
                    self._voice_user_stream += str(p.get("delta"))
                    try:
                        self._stream_post(self._voice_user_turn_id,
                                          self._voice_user_stream, role="user")
                    except Exception:
                        pass
                if p.get("role") == "assistant" and p.get("delta"):
                    if self._voice_turn_id is None:
                        self._voice_turn_id = "v-" + str(int(time.time() * 1000))[-12:]
                    self._voice_stream += str(p.get("delta"))
                    if self._subtitle_mode() or (not self._backend_speaking_likely() and not self._voice_turn_commentary):
                        # ⭐ 用户 2026-09-18：「从第一个工具调用开始生成那个工具调用的对话卡片…
                        #   如果 ai 有在说话就在卡片内直接流式传输」。
                        #   后台轮在跑 = 这句话是这次任务的一部分 → 流进**后台那条轮次**，
                        #   于是它和工具、绿点红点、生成物在同一张卡里逐字出现；
                        #   没有后台轮（纯聊天）才用自己的 v- 轮次。
                        #   归属依据是后台的实际调用，与措辞无关 —— 同「收拢」那套一个口径。
                        _own, _clear = stream_owner(
                            self._backend_display_id(),
                            self._voice_turn_id, self._voice_stream_owner)
                        if _clear:
                            # v- → 后台轮：先把旧容器草稿清空，否则同一段文字会顶在上面
                            # （草稿不落库，清空即消失）。
                            self._stream_post(_clear, "")
                            self.log("voice_stream_retarget",
                                     frm=_clear, to=_own, chars=len(self._voice_stream))
                        self._voice_stream_owner = _own
                        self._stream_post(_own, self._voice_draft(_own))
            elif m in ("turn/started", "turn/completed"):
                turn = p.get("turn") or {}
                if m == "turn/completed" and (self._turn is None or turn.get("id") != self._turn["id"]):
                    return
                self.backend_busy = m == "turn/started"
                self.log(m, turnId=turn.get("id"), status=turn.get("status"))
                # ⚠ 2026-09-17：一轮 failed 时这里只记了 status，错误正文丢在 notify("error") 里
                #   没人看 —— 用户连着三轮收不到任何回应（后台每次都 400），而唯一的线索是
                #   事件流里一个光秃秃的 method=error。失败必须带原因，否则等于没记。
                if m == "turn/completed" and str(turn.get("status") or "") != "completed":
                    self.log("turn_failed", turnId=turn.get("id"), status=turn.get("status"),
                             detail=json.dumps(p, ensure_ascii=False)[:600])
                if m == "turn/started":
                    tid = str(turn.get("id") or "") or ("t-" + str(int(time.time() * 1000))[-12:])
                    user = self._pending_turn_user
                    self._pending_turn_user = None
                    # 用户句已在库里的两种情况：这轮是我们自己起的（/turn、/typed）→ 现在就写；
                    # 语音模型委托后台 → 用户那句字幕早已写过，不再写。
                    user_posted = False
                    if user:
                        self._history_post({"user": user, "via": "codex-voice", "turn_id": tid + ".u"})
                        user_posted = True
                    elif self._voice_pending_user:
                        user_posted = True
                    self._turn = {"id": tid, "user": user, "user_posted": user_posted, "assistant": None,
                                  "parts": [], "stream": "", "started": time.time()}
                    self._stream_start_post(tid)   # 侧栏用这个 id 当本轮容器身份，App 画的部件直接落进同一条记录
                else:
                    self._finish_turn(turn)
                    # 这一轮跑完、下一轮还没起 —— 把忙碌期间压着的最新状态送进去
                    await self._ctx_flush_pending()
            elif m == "item/agentMessage/delta":
                if self._matches_backend_turn(p) and p.get("delta") and not self._voice_owns_text():
                    item_id = str(p.get("itemId") or "legacy-agent")
                    streams = self._turn.setdefault("item_streams", {})
                    if item_id not in self._turn.setdefault("completed_items", set()):
                        streams[item_id] = (streams.get(item_id, "") + str(p["delta"]))[:32000]
                        self._stream_post(self._part_segment(self._turn, item_id), streams[item_id],
                                          item_id=item_id)
            elif m in ("item/started", "item/completed"):
                if not self._matches_backend_turn(p):
                    return
                item = p.get("item") or {}
                t = item.get("type")
                if t in ("agentMessage", "mcpToolCall", "webSearch", "commandExecution", "fileChange", "reasoning"):
                    self.log(m, itemType=t, tool=item.get("tool") or item.get("name"), status=item.get("status"),
                             text=(item.get("text") or item.get("query") or item.get("command") or "")[:160] or None)
                if (m == "item/started" and self._turn is not None
                        and t in ("mcpToolCall", "webSearch", "commandExecution",
                                  "fileChange", "dynamicToolCall", "collabAgentToolCall")):
                    self._tool_opened(item)
                if m == "item/started" and t == "agentMessage" and self._turn is not None:
                    self._turn.setdefault("item_streams", {}).setdefault(str(item.get("id") or "legacy-agent"), "")
                if m == "item/completed" and self._turn is not None:
                    self._turn_item(item)
            elif m == "thread/tokenUsage/updated":
                tu = (p.get("tokenUsage") or {})
                self.usage["tokens"] = tu
                last = tu.get("last") or {}
                self.log("tokens", input=last.get("inputTokens"), cached=last.get("cachedInputTokens"), output=last.get("outputTokens"))
            elif m == "account/rateLimits/updated":
                self.usage["rateLimits"] = p.get("rateLimits") or p
                self.usage["updatedAt"] = time.time()
            elif m in ("app_server_stderr", "server_request", "app_server_exited"):
                if m == "app_server_exited":
                    app = self.app
                    code = app.proc.returncode if app and app.proc else None
                    tail = list(app.stderr_tail)[-5:] if app else []
                    self.log(m, exitCode=code, stderrTail=[clean(re.sub(r"\x1b\[[0-9;]*m", "", line))[-160:] for line in tail])
                    self.app = None
                    if self.thread_id:
                        # 出声：app-server 一死线程 id 就丢，下一次必然是全新开局。
                        self.log("thread_dropped", threadId=self.thread_id, why="app_server_exited")
                    self.thread_id = None
                    self.app_server_exits += 1
                    if self.session_state in ("connected", "starting"):
                        await self.on_closed("app-server-exited")
                    if not self.shutting_down:
                        self.schedule_app_relaunch()
                else:
                    self.log(m, **p)
            elif m.startswith("thread/realtime/") or m.startswith("item/") or m.startswith("mcpServer/"):
                pass
            elif m == "error":
                # 同上：app-server 的 error 通知带着 400 的正文，原来被折成一行 method=error。
                detail = json.dumps(p, ensure_ascii=False)[:600]
                # 留一份供投递侧关联：appendSpeech 这类是 fire-and-forget，
                # 失败不从响应回来，而是以这条 error 通知异步到达 —— 不记下来就没人能把
                # 「我刚才让它念的那句」和「conversation is not running」对上（见 say）。
                self._last_app_error = (time.time(), detail)
                self.log("app_error", detail=detail)
            else:
                self.log("notify", method=m)
        except Exception as e:
            self.log("notification_handler_error", method=m, message=clean(e))

    def on_dc_message(self, raw: str):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        t = d.get("type", "?")
        if t in ("turn.created", "turn.done"):
            turn = d.get("turn") or {}
            if self.settings.get("jevContextEnabled"):
                if turn.get("role") == "user":
                    key = self._jev_key(turn.get("id"))
                    self._jev.observe(key, (turn.get("transcript") or self._voice_user_acc or "").strip(),
                                      new_turn=t == "turn.created", completed=t == "turn.done")
                elif turn.get("role") == "assistant" and t == "turn.created":
                    # Preparation alone never injects or starts a backend turn.
                    self.loop.call_soon_threadsafe(self._jev.assistant_started, self._jev.completed_user)
            if self._subtitle_mode() and turn.get("role") in ("user", "assistant"):
                if t == "turn.created":
                    self._transcript_state().start(turn["role"], turn.get("id"))
                    if turn.get("transcript"):
                        self._transcript_publish(self._transcript_state().seed(
                            turn["role"], turn.get("transcript"), turn.get("id")))
                else:
                    self._transcript_final(turn["role"], turn.get("transcript"), turn.get("id"))
            if turn.get("role") == "user":
                self.user_speaking = t == "turn.created"
                if t == "turn.created":
                    self._voice_user_acc = ""
                    if self._voice_turn_id is None:
                        self._voice_turn_id = "v-" + str(int(time.time() * 1000))[-12:]
                    self.mark_activity("user-speech")
                    self._on_user_speech_started()
                else:
                    # ⭐ 用户刚说完 —— 这是注入后台状态最合适的时刻（2026-09-16 实测定的）：
                    #   · 到「语音召唤后台」还有中位 14.5 秒、P10 也有 2.9 秒的余量，
                    #     而 inject_items 本身要 80 ms，**99% 赶得上**；
                    #   · 相比之下在召唤那一刻注入只剩中位 38 ms，只有 8% 赶得上 —— 基本必然迟到；
                    #   · 又因为是「说完」才投，只翻页不说话时零注入，正是用户报的那个毛病。
                    if str(self.settings.get("contextInjectOn") or "delegationSteer") == "speechEnd":
                        asyncio.run_coroutine_threadsafe(self._ctx_on_delegation(), self.loop)
                    utext = (turn.get("transcript") or self._voice_user_acc or "").strip()
                    self._voice_user_acc = ""
                    if utext:
                        self._voice_pending_user = (time.time(), utext)
                        tid = self._voice_turn_id or ("v-" + str(int(time.time() * 1000))[-12:])
                        self._voice_turn_id = tid
                        # 用户句用 <id>.u 落库：侧栏按 turn_id 去重，用户句和回复不能共用一个 id
                        if not self._subtitle_mode():
                            self._history_post({"user": utext, "via": "voice", "turn_id": tid + ".u"})
            elif turn.get("role") == "assistant":
                self.assistant_speaking = t == "turn.created"
                if t == "turn.created":
                    if self._voice_turn_id is None:
                        self._voice_turn_id = "v-" + str(int(time.time() * 1000))[-12:]
                    self._voice_stream = ""
                    self._voice_stream_owner = None
                    # 后台轮正在跑，或刚结束不到 20 秒：这句是过渡语或对后台结果的转述，历史里以后台正文为准
                    self._voice_turn_commentary = self._turn is not None or (time.time() - self._backend_done_at) < 20
                elif self._subtitle_mode():
                    self.last_assistant_done = time.monotonic()   # 落库与轮次 id 的收尾交给 transcript/done
                else:
                    self.last_assistant_done = time.monotonic()
                    atext = (turn.get("transcript") or self._voice_stream or "").strip()
                    tid = self._voice_turn_id or ("v-" + str(int(time.time() * 1000))[-12:])
                    self._voice_turn_id = None
                    self._voice_stream = ""
                    self._voice_stream_owner = None
                    commentary = self._voice_turn_commentary or self._turn is not None
                    self._voice_turn_commentary = False
                    if atext and not re.search(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", atext):
                        self.log("history_skip_punct", text=atext[:20])   # 「。」这种纯标点回复不记
                    elif atext:
                        if commentary and (self._backend_recent or self._turn is not None):
                            self.log("history_skip_commentary", text=atext[:80])
                        elif self._spoken_dup(atext):
                            self.log("history_dedupe", text=atext[:80])
                        else:
                            # 延迟 6 秒：委托前的过渡句（「我来做个卡片」）此刻还没有后台轮可对照，
                            # 等一等——后台轮在窗口内开始就说明它是过渡句，丢弃；否则才落库。
                            asyncio.run_coroutine_threadsafe(self._voice_write_deferred(atext, tid), self.loop)
            self.log("dc_" + t.replace(".", "_"), role=turn.get("role"),
                     voiceTurnId=turn.get("id"), transcript=(turn.get("transcript") or "")[:80])
        elif t == "session.usage.updated":
            u = d.get("usage") or {}
            # audio_duration_ms 是实时语音真正的用量表针（按音频时长走）。
            # 记下增量，才能回答「只连着不说话是不是也在烧」。
            new_ms = u.get("audio_duration_ms")
            if isinstance(new_ms, (int, float)):
                prev = self.usage.get("audioDurationMs") or 0
                self.log("realtime_usage", audioMs=int(new_ms), deltaMs=int(new_ms - prev),
                         sessionSec=round(time.time() - (self.session_started_at or time.time())),
                         userSpeaking=bool(self.user_speaking), backendBusy=bool(self.backend_busy))
            self.usage["audioDurationMs"] = u.get("audio_duration_ms", self.usage["audioDurationMs"])
            self.usage["backendModelUsage"] = u.get("backend_model_usage")
            self.usage["updatedAt"] = time.time()
        elif t in ("error", "session.started", "input_audio.paused", "input_audio.resumed", "delegation.created"):
            if t == "delegation.created":
                self.mark_activity("delegation")   # 委派后台 = 人在用
                self._promise_pending = None       # 真派活了，看门狗不必补
                self._delegation_seq += 1          # 供"这中间零委派"判据用（见 _promise_watch）
                jev_key = None
                if self.settings.get("jevContextEnabled"):
                    item = d.get("item") or {}
                    jev_key = self._jev_key(item.get("user_bidi_turn_id"))
                    request = "\n".join(str(c.get("text") or "") for c in item.get("content", [])
                                        if c.get("type") == "input_text")
                    self._jev.delegated(jev_key, request)
                    self.loop.call_soon_threadsafe(self._jev.start, jev_key, "delegation")
                # ⭐ 语音委派的那一轮，**答案由 app-server 直接交给语音模型念**。
                #   后台此时再调 voice_say 就是同一个问题念两遍（见 say() 的闸）。
                self._delegation_open_at = time.time()
                # ⭐ 这是「语音模型此刻正在召唤后台」的那个标记 —— 实测它后面 22~60 ms
                # 就跟着 turn/started，所以在这里注入，后台起的那一轮正好读得到。
                # 比开口边沿注入好在两点（2026-09-16 用户提出，日志印证）：
                #   · 三分之二的说话语音模型自己就答了（963 次开口只有 316 次委托），
                #     那些根本不需要给后台任何东西；
                #   · 开口到委托之间还隔着 3.7~18 秒，期间翻的页、改的选中都能带上最新的。
                # on_dc_message 是**同步**回调，不能 create_task —— 和隔壁开口那条一样走线程安全投递
                # 默认不在这里投：留给注入的时间中位只有 38 ms，而注入要 80 ms，
                # 实测只有 8% 赶得上。想对照时把 contextInjectOn 设成 delegation
                if str(self.settings.get("contextInjectOn") or "delegationSteer") in ("delegation", "delegationSteer"):
                    asyncio.run_coroutine_threadsafe(self._ctx_on_delegation(
                        jev_key=jev_key, delegation_seq=self._delegation_seq), self.loop)
            # 委托这条留全：它是**两个模型之间的完整交接报文**（语音模型转给后台的原话、
            # handoff_id、target），也是链路上「何时召唤后台、交了什么过去」的唯一来源。
            # 其余事件仍截 200 字，免得把 events.jsonl 撑大。
            cap = 4000 if t == "delegation.created" else 200
            self.log("dc_" + t.replace(".", "_"), payload=json.dumps(d, ensure_ascii=False)[:cap])

    # ---------- app-server / 线程 ----------
    async def ensure_app(self):
        """保证 app-server 在跑、线程已建。**必须串行**，见下面的锁。

        2026-09-18：这里原来没有锁。运行器自身的启动任务与任一 HTTP 请求
        （/thread/new、上下文推送、steer）同时进来时，两边都看见 app is None /
        thread_id is None，于是各起一次 app-server、各开一条线程 —— 后开的覆盖
        先开的，先开的连同它的开局说明成了孤儿；竞态里失败的一侧还会向调用方回 500。
        实录 01:53:15：guide_inline 连打两次、POST /thread/new 回 500，而线程确实建起来了。
        """
        async with self._ensure_lock:
            await self._ensure_app_locked()

    async def _ensure_app_locked(self):
        if self.app is None:
            # 空 = PATH 里的 codex（Windows 上是 codex.exe；Mac 由 launchd 的 PATH 或 APP_CODEX 指到）
            exe = (self.settings.get("codexExe") or os.environ.get("APP_CODEX")
                   or ("codex.exe" if sys.platform == "win32" else "codex"))
            self.app = AppServer(exe, list(self.settings.get("mcpDisable") or []), self.on_notification,
                                 bool(self.settings.get("disableOfficialPlugins")))
            await self.app.launch()
            acct = await self.app.call("account/read", {"refreshToken": False}, timeout=30)
            self.log("app_server_ready", exe=exe, account=(acct.get("account") or {}).get("type"), plan=(acct.get("account") or {}).get("planType"))
        if self.thread_id is None:
            # 用户 2026-09-15：同一个对话一直用到手动清空为止。先续接（指定的或上次保存的线程），续不上再新开。
            want = self._thread_resume_target
            self._thread_resume_target = None
            if want is None and not self._thread_cleared:
                try:
                    want = (json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}).get("threadId") or None
                except Exception:
                    want = None
            self._was_cleared = self._thread_cleared
            self._thread_cleared = False
            if want:
                try:
                    self._ctx_invalidate()
                    self._resume_tried = True
                    r = await self.app.call("thread/resume", {"threadId": want}, timeout=90)
                    self.thread_id = (r.get("thread") or {}).get("id") or want
                    self.log("thread_resumed", threadId=self.thread_id)
                except Exception as e:   # noqa: BLE001
                    self.log("thread_resume_failed", threadId=want, message=clean(e))
        if self.thread_id is None:
            start = {"cwd": str(BASE), "modelProvider": "openai", "approvalPolicy": "never", "sandbox": "read-only", "environments": [],
                     "model": self.settings.get("backendModel") or None}
            if self.settings.get("backendThreadInstructions"):
                start["developerInstructions"] = self.settings["backendThreadInstructions"]
                start["developerInstructions"] += self._inline_hot_guides()
            self._ctx_invalidate()
            r = await self.app.call("thread/start", start, timeout=90)
            self.thread_id = r["thread"]["id"]
            # ⚠ 一条新线程 = 一次完整开局（实测 8420 token，其中只有 1781 是我们的指令，
            #   其余是官方的 skills_instructions / recommended_plugins 等样板）。
            #   所以"为什么没续接"必须说清楚 —— 2026-09-18 有一次换线程，日志里
            #   既没有 runner_started 也没有 resume 尝试，事后完全查不出是谁换的。
            self.log("thread_started", threadId=self.thread_id,
                     whyNew=("显式清空（/thread/new 或 /thread/delete）" if self._was_cleared
                             else ("续接失败" if self._resume_tried else "没有可续接的线程 id")))
            self._was_cleared = False
            self._resume_tried = False
            self._warn_settings_drift()
            self.write_binding()
            await self.apply_hot()

    def schedule_app_relaunch(self):
        """app-server 意外退出后主动拉回来（不等下一次调用），退避 3/6/12…≤60 秒。"""
        if self.app_relaunch_task and not self.app_relaunch_task.done():
            return
        delay = min(60, 3 * (2 ** min(self.app_server_exits - 1, 4)))
        self.log("app_server_relaunch_scheduled", inSeconds=delay, exits=self.app_server_exits)

        async def _go():
            await asyncio.sleep(delay)
            try:
                await self.ensure_app()
            except Exception as e:
                self.log("app_server_relaunch_error", message=clean(e))
                self.schedule_app_relaunch()
        self.app_relaunch_task = asyncio.create_task(_go())

    def _warn_settings_drift(self) -> None:
        """线上持久化设置与源码默认值不一致时出声（2026-09-17）。

        ⚠ 这个坑今天踩到第五次：改了 DEFAULTS 里的说明、装了新版、重启了运行器，
        **线上仍在用旧的那份** —— 因为持久化设置一旦存在就盖过默认值，而整个过程
        一声不吭。最近一次的代价：reader_card 的信封说明（{card:{...}} 与 title 必填）
        改完没下发，模型连着两次照旧犯同样的错。
        只报**指令类**长文本键：设备名、模型、档位这些本来就该由用户决定，不算漂移。
        """
        watch = ("backendThreadInstructions", "backendStartInstructions", "prompt",
                 "realtimeEndInstructions", "boardSilentRule")
        drift = []
        for key in watch:
            want, have = DEFAULTS.get(key), self.settings.get(key)
            if isinstance(want, str) and isinstance(have, str) and want and want != have:
                drift.append("%s(线上%d字/源码%d字)" % (key, len(have), len(want)))
        if drift:
            self.log("settings_drift", keys=drift,
                     hint="线上用的是持久化的旧文本；要用源码版就 POST /settings 下发后开新线程")
        # 桥 0.1.418 起「只有快板变化」时不再推 /board（推来也是整条丢弃）。
        # 但那个丢弃的前提是 contextInjectEnabled —— 关掉它，快板本该重新变得有用，
        # 而桥已经不发了。这是个能力缺口，不能让它悄悄发生。
        if not self.settings.get("contextInjectEnabled", True):
            self.log("board_fast_suppressed",
                     hint="contextInjectEnabled=false：上下文注入器已关，而桥自 0.1.418 起"
                          "不再推送只有快板变化的板面 —— 焦点/绘图/挂断这些信号现在两边都收不到。"
                          "要么把注入器开回来，要么改桥那条 if (!slowChanged) return。")

    def _inline_hot_guides(self) -> str:
        """把最近常取的能力指南直接内联进开局（2026-09-18 用户：按频度自动调）。

        为什么不是一刀切：这些规则刚从 AGENTS.md 搬进指南（省了每轮 6.2K 字），
        但天天要用的那几个如果每轮都得现取，就把省下的字换成了多跑一趟。
        所以按真实使用频度分两头 —— 常用的内联，少用的按需。
        取不到、没数据、超预算都**安静退回按需取**，但会记一条日志说明为什么。
        """
        s = self.settings
        if not s.get("guideInlineEnabled", True):
            return ""
        try:
            hot = hot_guide_topics(float(s.get("guideInlineDays") or 7.0),
                                   int(s.get("guideInlineMinCalls") or 3))
        except Exception as e:   # noqa: BLE001
            self.log("guide_inline_error", message=clean(e))
            return ""
        if not hot:
            self.log("guide_inline", inlined=[], reason="最近没有话题达到阈值")
            return ""
        budget = int(s.get("guideInlineMaxChars") or 6000)
        picked, used, skipped = [], 0, []
        for topic, calls in hot:
            text = fetch_guide(topic)
            if not text:
                skipped.append("%s(取不到)" % topic)
                continue
            if used + len(text) > budget:
                skipped.append("%s(超预算)" % topic)
                continue
            picked.append((topic, calls, text))
            used += len(text)
        if not picked:
            self.log("guide_inline", inlined=[], skipped=skipped, reason="都没能内联")
            return ""
        out = [chr(10) + chr(10) + "——以下几节是按你最近的使用频度自动内联进来的"
               "（这些话题你常用，就不必再调 reader_capability_guide 去取了；"
               "没列出来的话题仍然按需取）——"]
        for topic, calls, text in picked:
            out.append(chr(10) + chr(10) + "【" + topic + "】" + chr(10) + text.strip())
        self.log("guide_inline", inlined=[t for t, _c, _x in picked],
                 calls=[c for _t, c, _x in picked], chars=used, skipped=skipped)
        return "".join(out)

    def write_binding(self):
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
            BINDING_PATH.write_text(json.dumps({
                "contract": "reader-voice-thread-binding/1", "threadId": self.thread_id, "source": "evidence",
                "boundAtUtc": now, "evidenceAtUtc": now, "evidenceKind": "voice-cli-runner",
                "captureActive": True, "captureGeneration": None}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log("binding_write_error", message=clean(e))

    # ---------- 语音会话 ----------
    def start_params(self, initial_items: list | None = None) -> dict:
        s = self.settings
        p = {"threadId": self.thread_id, "outputModality": "audio", "version": s.get("version") or "v3",
             "includeStartupContext": bool(s.get("includeStartupContext")),
             "clientManagedHandoffs": bool(s.get("clientManagedHandoffs")),
             "codexResponsesAsItems": bool(s.get("codexResponsesAsItems")),
             "codexResponseHandoffMode": s.get("handoffMode") or "thinking"}
        # 下面几个 2026-09-16 起启用：只在设了值时才传，免得把 None 塞进协议
        if s.get("realtimeEndInstructions"):
            p["realtimeEndInstructions"] = str(s["realtimeEndInstructions"])
        if s.get("realtimeStartInstructions"):
            p["realtimeStartInstructions"] = str(s["realtimeStartInstructions"])
        if s.get("flushTranscriptTailOnSessionEnd") is not None:
            p["flushTranscriptTailOnSessionEnd"] = bool(s["flushTranscriptTailOnSessionEnd"])
        if s.get("codexResponseItemPrefix"):
            p["codexResponseItemPrefix"] = str(s["codexResponseItemPrefix"])
        if s.get("voice"):
            p["voice"] = s["voice"]
        if s.get("realtimeModel"):
            p["model"] = s["realtimeModel"]
        if s.get("prompt"):
            name = str(s.get("userFirstName") or os.environ.get("USERNAME") or "there")
            p["prompt"] = str(s["prompt"]).replace("{{user_first_name}}", name).replace("{{ user_first_name }}", name)
        if s.get("delegationAckFiller") is not None:
            p["delegationAckFiller"] = bool(s["delegationAckFiller"])
        if s.get("backendStartInstructions"):
            p["realtimeStartInstructions"] = s["backendStartInstructions"]
        if initial_items:
            p["initialItems"] = initial_items
        return p

    async def session_start(self, reason: str = "manual", profile: str | None = None) -> dict:
        explicit = profile is not None
        profile = profile or self.settings.get("defaultProfile") or "local"
        if self.session_state in ("starting", "connected"):
            # 只有 App 明确要求 app 档（App START）才切档；后台发起的 voice_session_start 没给 profile，
            # 一律并入当前会话（2026-09-15 实录：提醒把 App 通话切成本机档，App 那头直接断线）。
            if explicit and profile == "app" and profile != self.session_profile and self.session_state == "connected":
                # 旧 Codex 时代的行为（用户 2026-09-14）：本机正在通话，App 连进来就切到线缆，
                # App 挂断再切回本机设备。这里记住"切之前是哪档"，同一线程重开，最近字幕作 initialItems 带过去。
                self.profile_before_switch = self.session_profile
                self.log("profile_switch", from_profile=self.session_profile, to_profile=profile, reason=reason)
                await self.session_stop("profile-switch")
                result = await self.session_start("switch:" + reason, profile)
                result["switched"] = True
                return result
            return {"ok": True, "msg": "会话已在进行", "already": True, "profile": self.session_profile}
        self._ctx_invalidate(("voice", "image"))
        self.session_profile = profile
        self.stop_requested = False
        self.session_state = "starting"
        self.last_error = None
        self.pending_cold.clear()
        try:
            await self.ensure_app()
            s = self.settings
            pipe = profile == "app" and bool(s.get("appAudioPipe", True))
            in_dev = s.get("appInputDevice") if profile == "app" else s["inputDevice"]
            out_dev = s.get("appOutputDevice") if profile == "app" else s["outputDevice"]
            if pipe:
                # App 档直连：不碰声卡，音频直接和桥对流（见 PipeMicTrack/PipeSpeaker 的说明）
                self.speaker = PipeSpeaker(int(s.get("appPipeDownlinkPort") or 43133), float(s.get("gain") or 1.0))
            else:
                self.speaker = Speaker(out_dev, int(s["outputRate"]), float(s.get("gain") or 1.0))
            self.mic = PipeMicTrack(int(s.get("appPipeUplinkPort") or 43132)) if pipe else MicTrack(in_dev)
            # "为什么没声音"必须一眼能查到这次走的是哪条路
            self.log("audio_path", profile=profile, pipe=pipe,
                     uplink=(int(s.get("appPipeUplinkPort") or 43132) if pipe else in_dev),
                     downlink=(int(s.get("appPipeDownlinkPort") or 43133) if pipe else out_dev))
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.pc = pc

            @pc.on("connectionstatechange")
            async def _changed():
                self.log("peer_state", state=pc.connectionState)
                if pc.connectionState in ("failed", "disconnected") and self.session_state == "connected":
                    await self.on_closed("peer_" + pc.connectionState)

            @pc.on("track")
            def _on_track(track):
                async def receive():
                    try:
                        while True:
                            frame = await track.recv()
                            if track.kind == "audio" and self.speaker:
                                self.speaker.feed(frame)
                    except Exception as e:
                        self.log("track_end", exception=type(e).__name__)
                self.tasks.append(asyncio.create_task(receive()))

            self.dc = pc.createDataChannel("oai-events")
            self.dc.on("message")(self.on_dc_message)
            pc.addTrack(self.mic)
            await pc.setLocalDescription(await pc.createOffer())
            initial: list | None = None
            if s.get("voiceAddendum") and (s.get("version") or "v3") == "v3":
                initial = [{"role": "developer", "text": s["voiceAddendum"]}]
            if s.get("boardInitialItems") and (s.get("version") or "v3") == "v3":
                prefix = s.get("boardPrefix") or "【快板】"
                initial = (initial or []) + [
                    {"role": "developer", "text": s.get("boardSilentRule") or ""},
                    {"role": "user", "text": f"以后{prefix}开头的更新你不要出声，也不用说收到，你知道就行。"},
                    {"role": "assistant", "text": f"明白，{prefix}开头的更新我一个字都不说，只记住。"},
                ]
            if reason.startswith("reconnect") and int(s.get("reconnectInitialItems") or 0) > 0:
                n = int(s["reconnectInitialItems"])
                initial = (initial or []) + [{"role": ("user" if r == "user" else "assistant"), "text": (t or "")[:400]}
                                             for _, r, t in list(self.transcripts)[-n:] if t]
            self.remote_sdp = self.loop.create_future()
            params = self.start_params(initial)
            params["transport"] = {"type": "webrtc", "sdp": pc.localDescription.sdp}
            await self.app.call("thread/realtime/start", params, timeout=30)
            sdp = await asyncio.wait_for(self.remote_sdp, 55)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
            deadline = time.monotonic() + 20
            while pc.connectionState not in ("connected", "failed", "closed") and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if pc.connectionState != "connected":
                raise RuntimeError("WebRTC 没连上: " + pc.connectionState)
            self.session_no += 1
            self.session_started_at = time.time()
            self.session_state = "connected"
            # 新会话的语音上下文是空的 —— 之前 appendText 投进去的东西一条都不在了。
            # 不清这三个指纹，重连后第一次开口会因为"内容没变"而什么都不投，
            # 于是模型手上既没有选中清单也没有地点（2026-09-18 补 ambient 时一并处理）。
            for _k in ("voice", "sel", "amb"):
                self._ctx["fp"][_k] = ""
            self.write_bridge_flag(True)
            self.mark_activity("session-start")
            self.quota_sample("session_start")
            self.log("session_connected", sessionNo=self.session_no, reason=reason, profile=profile, threadId=self.thread_id,
                     input=in_dev, inputRate=self.mic.rate, output=out_dev, outputRate=self.speaker.out_rate,
                     version=params["version"], voice=params.get("voice"))
            self.save_state()
            return {"ok": True, "sessionNo": self.session_no, "threadId": self.thread_id}
        except Exception as e:
            self.last_error = clean(str(e) or type(e).__name__)
            self.log("session_start_error", message=self.last_error)
            await self.teardown()
            self.session_state = "idle"
            if reason.startswith("reconnect"):
                self.schedule_reconnect("start-failed")
            return {"ok": False, "msg": self.last_error}

    async def teardown(self):
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        if self.pc:
            try:
                await self.pc.close()
            except Exception:
                pass
        self.pc = None
        self.dc = None
        # ⚠ 收尾时把这场通话的音频统计留一笔。计数器一直都有，但**没人看** ——
        #   用户 2026-09-20 报「语音有点卡顿」，我只能去翻实时 status，而那时
        #   上一场早已清零。卡顿要能自己说话，否则每次都只能靠现场复现。
        #   只在真有问题时记（干净的通话不写日志，免得把统一日志刷成噪声）。
        try:
            sp, mc = self.speaker, self.mic
            bad = []
            if sp is not None:
                if getattr(sp, 'underruns', 0):
                    bad.append('speakerUnderruns=%d' % sp.underruns)
                if getattr(sp, 'gaps', 0):
                    bad.append('speakerGaps=%d' % sp.gaps)
                if getattr(sp, 'status_flags', 0):
                    bad.append('speakerFlags=%d' % sp.status_flags)
            if mc is not None and getattr(mc, 'drops', 0):
                # 只报真丢帧；trimmed 是有意削深，不该算故障。
                bad.append('micDrops=%d' % mc.drops)
            if bad:
                # ⚠ 这里原来写的是 'reason=' + str(reason)，而 teardown 根本没有
                #   reason 这个名字 —— NameError 被下面那个 except 吞掉，于是这条
                #   日志**一次都没写出来过**（2026-09-21 pyflakes 抓到）。正是
                #   silent-failure 清单里的形态：出了状况就悄悄什么都不做。
                error_log('voice', 'BW_VOICE_AUDIO_DEGRADED',
                          '通话音频有丢失或欠载：' + '、'.join(bad),
                          'audioMs=' + str((self.usage or {}).get('audioDurationMs', 0))
                          + ' micTrimmed=' + str(getattr(mc, 'trimmed', 0) if mc else 0))
        except Exception as e:   # noqa: BLE001
            # 写不出去也要留个痕，否则下次又是「日志里什么都没有」。
            try:
                self.log("audio_report_failed", message=clean(e))
            except Exception:
                pass
        if self.mic:
            self.mic.close()
        if self.speaker:
            self.speaker.close()
        self.mic = None
        self.speaker = None
        self.session_id = None
        self.user_speaking = False
        self._board_voice_sent = ""
        if self._board_last_sent:
            self._board_pending_voice = self._board_last_sent

    async def session_stop(self, reason: str = "manual", after_speech: bool = False, grace: float = 10.0) -> dict:
        self._jev.reset()
        if after_speech and self.session_state == "connected":
            waited = await self.wait_for_speech(grace)
            self.log("stop_after_speech", waited=waited, reason=reason)
        # App 挂断而切之前本机还在通话 → 切回去（不是真的停）
        if reason.startswith("app-stop") and self.session_profile == "app" and self.profile_before_switch:
            back = self.profile_before_switch
            self.profile_before_switch = None
            self.log("profile_switch_back", to_profile=back, reason=reason)
            self.stop_requested = True
            await self._session_stop_inner("profile-switch-back")
            return await self.session_start("switch-back:" + reason, back)
        if not reason.startswith("profile-switch"):
            self.profile_before_switch = None
        self.stop_requested = True
        return await self._session_stop_inner(reason)

    def _voip_hangup_if_needed(self, reason: str) -> None:
        """我们拨出去的电话：结束语音会话时一并请 App 结束 CallKit 通话（App 自己挂的 app-stop 不用；切档/换线程不算结束）。"""
        if not self._voip_call_active:
            return
        if reason.startswith("app-stop"):
            self._voip_call_active = False
            return
        if reason.startswith(("profile-switch", "thread-", "call-user-redial")):
            return
        self._voip_call_active = False
        try:
            (BRIDGE_RUNTIME / "voip-hangup.json").write_text(
                json.dumps({"contract": "reader-voip-hangup/1", "atUtcMs": int(time.time() * 1000)}), encoding="utf-8")
            self.log("voip_hangup_requested", reason=reason)
        except Exception as e:   # noqa: BLE001
            self.log("voip_hangup_error", message=clean(e))

    async def _session_stop_inner(self, reason: str) -> dict:
        self._voip_hangup_if_needed(reason)
        if self.reconnect_task:
            self.reconnect_task.cancel()
            self.reconnect_task = None
        if self.session_state == "idle":
            return {"ok": True, "msg": "本来就没在跑"}
        self.session_state = "stopping"
        if self.app and self.thread_id:
            self._closed_event.clear()
            try:
                await self.app.call("thread/realtime/stop", {"threadId": self.thread_id}, timeout=10)
                try:
                    await asyncio.wait_for(self._closed_event.wait(), 4)
                except asyncio.TimeoutError:
                    self.log("stop_closed_timeout")
            except Exception as e:
                self.log("stop_error", message=clean(e))
        await self.teardown()
        self.session_state = "idle"
        self.log("session_stopped", reason=reason)
        self.quota_sample("session_stop")
        self._notify_bridge_ended(reason)
        return {"ok": True}

    async def _voice_write_deferred(self, atext: str, tid: str, wait: float = 6.0):
        t0 = time.time()
        await asyncio.sleep(wait)
        if self._turn is not None or self._backend_done_at >= t0 or (self._backend_recent and self._backend_recent[0] >= t0):
            self.log("history_skip_commentary", text=atext[:80], deferred=True)
            return
        if self._spoken_dup(atext):
            self.log("history_dedupe", text=atext[:80])
            return
        self._history_post({"assistant": atext, "via": "voice", "turn_id": tid})

    def _notify_bridge_ended(self, reason: str):
        """App 档位的会话结束了、但不是 App 自己挂的（后台模型调 voice_session_stop、用户口头挂断、
        会话被服务端关掉）→ 桥不知道，App 按钮会一直绿着（用户 2026-09-14）。App 自己 STOP 的
        （app-stop*）和切档（profile-switch*）不用说：前者桥就是发起方，后者会话马上重开。"""
        reason = str(reason or "")
        if self.session_profile != "app" or reason.startswith("app-stop") or reason.startswith("profile-switch"):
            return

        def work():
            import urllib.request
            try:
                req = urllib.request.Request(BRIDGE_URL + "/voice-core/session-ended",
                                             data=json.dumps({"reason": reason[:80]}).encode("utf-8"), method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    r = json.loads(resp.read() or b"{}")
                self.loop.call_soon_threadsafe(lambda: self.log("bridge_ended_notified", reason=reason, ended=r.get("ended")))
            except Exception as e:
                message = clean(e)
                self.loop.call_soon_threadsafe(lambda: self.log("bridge_ended_notify_error", reason=reason, message=message))

        threading.Thread(target=work, daemon=True).start()

    async def session_restart(self, profile: str | None = None) -> dict:
        profile = profile or self.session_profile
        await self.session_stop("restart")
        return await self.session_start("restart", profile)

    async def on_closed(self, reason):
        if self.session_state in ("stopping", "idle"):
            return
        await self.teardown()
        self.session_state = "idle"
        if not (self.settings.get("autoReconnect") and not self.stop_requested):
            self._notify_bridge_ended("closed:" + str(reason))
        if self.settings.get("autoReconnect") and not self.stop_requested:
            self.schedule_reconnect(reason)

    def schedule_reconnect(self, reason):
        if self.reconnects >= int(self.settings.get("maxReconnects") or 0):
            self.log("reconnect_gave_up", reconnects=self.reconnects)
            return
        delay = min(30, 2 * (2 ** min(self.reconnects, 4)))
        self.reconnects += 1
        self.session_state = "reconnecting"
        self.log("reconnect_scheduled", inSeconds=delay, attempt=self.reconnects, reason=str(reason))

        async def _go():
            await asyncio.sleep(delay)
            self.session_state = "idle"
            await self.session_start(f"reconnect#{self.reconnects}", self.session_profile)
        self.reconnect_task = asyncio.create_task(_go())

    def write_pipe_flag(self, on: bool):
        """直连标记。桥在媒体启动时读一次：在 = 用 UDP 和我们对流，不在 = 老的虚拟声卡那条路。
        ⚠ 端口要跟 PipeMicTrack/PipeSpeaker 用的是同两个，写反了表现就是通了但没声音。"""
        try:
            if on and bool(self.settings.get("appAudioPipe", True)):
                BRIDGE_RUNTIME.mkdir(parents=True, exist_ok=True)
                tmp = PIPE_FLAG.with_suffix(".json.tmp%d" % os.getpid())
                tmp.write_text(json.dumps({
                    "contract": "reader-voice-audio-pipe/1",
                    "uplinkPort": int(self.settings.get("appPipeUplinkPort") or 43132),
                    "downlinkPort": int(self.settings.get("appPipeDownlinkPort") or 43133),
                    "pid": os.getpid(), "at": time.time(),
                }), encoding="utf-8")
                os.replace(tmp, PIPE_FLAG)   # 原子替换：桥可能正在读，半个文件会被它当成故障
            elif PIPE_FLAG.exists():
                try:
                    owner = json.loads(PIPE_FLAG.read_text(encoding="utf-8")).get("pid")
                except Exception:
                    owner = None
                if owner in (None, os.getpid()):
                    PIPE_FLAG.unlink()
        except Exception as e:
            self.log("pipe_flag_error", message=clean(e))

    def write_bridge_flag(self, on: bool):
        self.write_pipe_flag(on)
        try:
            if on:
                BRIDGE_RUNTIME.mkdir(parents=True, exist_ok=True)
                BRIDGE_FLAG.write_text(json.dumps({"backend": "voice-cli-runner", "pid": os.getpid(), "threadId": self.thread_id,
                                                   "at": time.time()}), encoding="utf-8")
            elif BRIDGE_FLAG.exists():
                # 只撤自己放的标记：新一代运行器可能已经起来并放了它的（keepalive 重拉与旧实例退出会交错）
                try:
                    owner = json.loads(BRIDGE_FLAG.read_text(encoding="utf-8")).get("pid")
                except Exception:
                    owner = None
                if owner in (None, os.getpid()):
                    BRIDGE_FLAG.unlink()
                else:
                    self.log("bridge_flag_kept", ownerPid=owner)
        except Exception as e:
            self.log("bridge_flag_error", message=clean(e))

    def save_state(self):
        try:
            STATE_PATH.write_text(json.dumps({"threadId": self.thread_id, "sessionNo": self.session_no, "at": time.time()}), encoding="utf-8")
        except Exception:
            pass

    # ---------- 输入通道 ----------
    async def board(self, text: str, to_voice: bool | None = None, to_backend: bool | None = None) -> dict:
        """快板更新：同一份内容 → 后台历史（inject_items，零成本）+ 语音上下文（appendText，静默约定）。
        相同内容不重发；1.5 s 内多次只发最后一次。"""
        text = str(text or "").strip()
        if self.settings.get("contextInjectEnabled", True) and "【快板】" in text:
            # 焦点/页码这类位置状态由注入器负责（按快照变化、带指纹）；桥推的【快板】只会重复它。留下【慢板】（地点等）。
            slow = text.split("【慢板】", 1)
            text = ("【慢板】" + slow[1]).strip() if len(slow) == 2 else ""
            if not text:
                self.log("board_skip_injector")
                return {"ok": True, "skipped": "fast-board-covered-by-injector"}
        if not text:
            return {"ok": False, "msg": "空内容"}
        self._board_latest = (text, to_voice, to_backend)
        if self._board_task and not self._board_task.done():
            return {"ok": True, "queued": True}
        self._board_task = asyncio.create_task(self._board_flush())
        return {"ok": True, "queued": True}

    async def _board_flush(self):
        await asyncio.sleep(float(self.settings.get("boardCoalesceSeconds") or 0))
        text, to_voice, to_backend = self._board_latest
        prefix = self.settings.get("boardPrefix") or "【快板】"
        payload = text if text.startswith(prefix) else prefix + text
        if payload == self._board_last_sent:
            self.log("board_skip_duplicate", text=payload[:120])
            return
        self._board_last_sent = payload
        sent = {"backend": False, "voice": False}
        if (to_backend if to_backend is not None else self.settings.get("boardToBackend", True)):
            try:
                await self.ensure_app()
                await self.app.call("thread/inject_items", {"threadId": self.thread_id, "items": [
                    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": payload}]}]}, timeout=30)
                sent["backend"] = True
            except Exception as e:
                self.log("board_backend_error", message=clean(e))
        want_voice = (to_voice if to_voice is not None else self.settings.get("boardToVoice", True))
        mode = self.settings.get("boardVoiceMode") or "on-speech"
        if want_voice and mode != "off" and self.session_state == "connected":
            if mode == "immediate" or self.user_speaking:
                sent["voice"] = await self._board_send_voice(payload)
            else:
                self._board_pending_voice = payload
                sent["voice"] = "pending"
        self.log("board", text=payload[:200], **sent, userSpeaking=self.user_speaking, mode=mode)

    async def _board_send_voice(self, payload: str) -> bool:
        if payload == self._board_voice_sent:
            return False
        try:
            await self.app.call("thread/realtime/appendText", {"threadId": self.thread_id, "text": payload, "role": "developer"}, timeout=15)
            self._board_voice_sent = payload
            self._board_pending_voice = None
            self.log("board_voice", text=payload[:160], userSpeaking=self.user_speaking)
            return True
        except Exception as e:
            self.log("board_voice_error", message=clean(e))
            return False

    def _on_user_speech_started(self):
        """数据通道报用户开口：把暂存的最新快板此刻送进语音上下文（并进这一轮，不会单独出声）。"""
        pending = self._board_pending_voice
        if pending and self.session_state == "connected":
            # 数据通道回调在事件循环线程；调试端点从 HTTP 线程来 —— 两边都用线程安全的投递
            asyncio.run_coroutine_threadsafe(self._board_send_voice(pending), self.loop)
        # 拉模式核心（搬自 _rtcFlushCtx）：开口的瞬间才注入"他正看着的位置+可见内容"，同状态零注入
        asyncio.run_coroutine_threadsafe(self._ctx_on_speech(), self.loop)

    # ---------- 上下文注入器 ----------
    def _ctx_snapshot(self) -> dict | None:
        """读桥快照（按 mtime 判变），顺带维护页面停留起点。"""
        try:
            st = SNAPSHOT_PATH.stat()
        except OSError:
            return None
        c = self._ctx
        if st.st_mtime == c["mtime"] and c["snap"] is not None:
            return c["snap"]
        try:
            snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return c["snap"]
        c["mtime"] = st.st_mtime
        c["snap"] = snap
        cp = snap.get("currentPage") or {}
        key = "%s:%s" % (cp.get("file") or "", cp.get("page") or "")
        if key != c["page_key"]:
            c["page_key"] = key
            c["page_since"] = time.time()
        return snap

    def _ctx_build(self, snap: dict) -> dict:
        """按旧规则拼两份文案 + 指纹。返回 {state, text, voice, fp_state, fp_text, fp_voice}；不可用时 state 为空。"""
        cp = snap.get("currentPage") or {}
        if snap.get("contextStatus") != "ready" or not cp.get("file"):
            return {"state": ""}
        s = self.settings
        title = str(cp.get("title") or cp.get("file") or "")[:80]
        kind = cp.get("kind") or ""
        page = cp.get("page")
        total = cp.get("total") or cp.get("pageCount") or (cp.get("readingWindow") or {}).get("total")
        where = ("用户此刻在《%s》" % title) + (("第 %s 页" % page) if page else "") + (("（全书 %s 页）" % total) if total else "") +             ("（网页）" if kind == "web" else "")
        # 选区 / 指代。**输入框上方有什么就带什么**（用户 2026-09-15）：
        # 一条「选中过的内容」+ 若干张长按选中的卡片/图/圈画。原来这里取第一条就 break，
        # 而快照里文字项永远排在卡片项前面 —— 只要有文字选中，卡片就永远轮不到。
        # ⚠ 每一项除了「是什么、什么内容」，还要带上**它的身份**（ref/label）。
        # 2026-09-17 用户指出：清单只给了文字，没带定位信息，于是模型拿到内容却没法直接调工具，
        # 还得再查一次。卡片类的 ref 就是它的稳定 id（reader_page_card_read 直接能用），
        # 前端一直在报，是这里构造时丢掉了。
        raw_items = [it for it in (snap.get("selectedItems") or [])
                     if str(it.get("text") or it.get("what") or "").strip()]
        # 新选中覆盖旧的：手指正按着一段文字时，别的**临时**文字项一律让位。
        # 2026-09-17 用户误触选了一个词、再选想要的那个，结果清单里同时留着两条文字 ——
        # 快照那边剔除过期文字项的条件是「选区不活跃」，而他当时正按着，所以旧的根本不会被剔。
        # ⚠ 只让位没有 ref 的：长按钉住的卡片/图/词组带 ref，那是用户明确要留的，不能误删。
        if any(it.get("live") and str(it.get("kind") or "text") == "text" for it in raw_items):
            raw_items = [it for it in raw_items
                         if it.get("live")
                         or str(it.get("kind") or "text") != "text"
                         or str(it.get("ref") or "").strip()]
        sel_items = []
        for it in raw_items:
            t = str(it.get("text") or it.get("what") or "").strip()
            sel_items.append((str(it.get("kind") or "text"), t,
                              str(it.get("ref") or "").strip(),
                              str(it.get("label") or "").strip()))
        if not sel_items:
            sel = snap.get("selection") or {}
            if sel.get("state") == "active" and sel.get("text"):
                sel_items.append(("text", str(sel["text"]), "", ""))
        sel_text = next((t for k, t, _r, _l in sel_items if k == "text"), "")
        sel_is_text = bool(sel_text)
        # 给后台的每项只给这么多字：够认出是哪一项就行，全文按需用快照取。
        sel_chars = int(s.get("contextBackendSelectionChars") or 24)
        if not sel_text and sel_items:
            # 只钉了卡片、没有文字选区时，「这个」指的就是那张卡；
            # 但**不能**沿用文字选区那套定位指令（见下面 sel_is_text 的分支）。
            sel_text = sel_items[0][1]
        # 除了已经当成「这个」报出去的那一条之外，还钉着的东西逐条报（不合并：模型要能分清哪句话属于哪张卡）。
        # ⚠ 别把当主角的那条再报一遍 —— 同一张卡出现两次，模型会以为钉了两张。
        # 逐项编号，与语音侧同一套：语音说"第 N 项"，后台照 N 在本条列出的原文里认。
        # ⚠ 两边编号必须一致 —— 之前后台把第一项写成"他此刻明确选中了…"、其余从（2）起，
        # 语音说"第 1 项"时后台根本找不到叫（1）的东西，而这种缝是静默的。
        sel_others = ""
        sel_others = ""
        # 只报事实：选了什么、第几项。怎么做、什么时候取全文 —— 都在线程指令里说过一次了。
        sel_hint = ""
        if sel_items:
            sel_hint = "。选中 %d 项：%s" % (
                len(sel_items),
                "".join("（%d）%s%s「%s%s」%s" % (
                            i + 1, _SEL_KIND_LABEL.get(k, "内容"),
                            ("〔%s〕" % lb) if lb else "",
                            t[:sel_chars],
                            # ⚠ 省略号只在真截断时才加。原来是硬编码的，没截断也带「…」，
                            # 后台因此以为原文不全、每次都再调一次快照去取
                            # （2026-09-17 后台 AI 原话：「选中文字还是省略号」）
                            "…" if len(t) > sel_chars else "",
                            # 有 id 的直接给出来：模型据此调 reader_page_card_read 等工具，
                            # 不必先用原文去反查（原文转述漏一个字就锚不上）
                            ("（id=%s）" % rf) if rf else "")
                        for i, (k, t, rf, lb) in enumerate(sel_items)))
        # ⚠ 语音模型没有工具：给它看"直接调 reader_card"这类指令，它会嘴上答应、却不发起委托（2026-09-14 实录两次）。
        #   语音侧只说选中了什么 + 这类事要立刻委派后台。
        sel_hint_voice = (("。他此刻明确选中了「%s」——说『这个/这段/这里』时指它；要划线/做卡/钉卡/翻译这段，"
                           "**立刻委派后台去做**（后台知道位置），你只说一句过渡语") % sel_text[:600]) if sel_is_text else (
            ("。他此刻钉着的是「%s」——说『这个/这张』时指它；要对它做事**立刻委派后台**，"
             "你只说一句过渡语") % sel_text[:600] if sel_text else "")
        sel_hint_voice += sel_others[:600]
        act_hint = ""
        acts = snap.get("recentActions") or []
        if acts and not sel_text:
            a = acts[-1]
            what = str(a.get("what") or a.get("kind") or "")[:200]
            if what:
                act_hint = "。他开口前最后做的事（%s 秒前）：%s" % (a.get("secondsAgo", "?"), what)
        vis = cp.get("visual") or {}
        # 待命图才是「这页有没有新圈画」的事实来源：vis.has_ink 依赖 currentPage.visual，
        # 而它在已发布快照里几乎总是缺席（见 _ink_standby_pending 的注释）。
        ink_pending = self._ink_standby_pending(snap, quiet=True)
        ink_hint = ""
        ink_hint_voice = ""
        if ink_pending:
            n_sel = sum(1 for x in ink_pending if x.get("kind") == "selection")
            ink_hint = ("。本页有新的圈画/手写（%d 张图已随本条送到后台%s）；"
                        "他说「这个/这里/圈的」多半指图里那处" % (
                            len(ink_pending),
                            ("，其中 %d 张是按选区编号分开的" % n_sel) if n_sel else ""))
            ink_hint_voice = ("。他刚在这页圈画/手写过（你看不到图，后台看得到）；"
                              "他问「这是什么/这里/圈的这个」时**必须立刻委派后台**，"
                              "不要自己猜、也不要只说稍等")
        elif vis.get("has_ink") or vis.get("drawing"):
            ink_hint = "。本页有笔迹/圈画（%s）；他提到圈画、手写、算式时后台用 reader_visual_image {scope: drawing-nearby} 看真图" % str(vis.get("drawing") or "有笔迹")[:120]
            ink_hint_voice = "。他在这页上有圈画/手写，你看不到图；他问「这是什么/这里/圈的这个」时**必须立刻委派后台去看图**，不要自己猜、不要只说稍等"
        # 正文：停留窗内才带；但页面被"激活"（有选区，或最近一次动作不是翻页而是在这页上选中/画/操作）时不等 8 秒（用户 2026-09-14）
        dwell = time.time() - (self._ctx["page_since"] or time.time())
        dwell_max = float(s.get("contextDwellMaxSeconds") or 720)
        # 有待命的笔迹图 = 他正指着这一页问东西。这时正文必须给，**连停留上限也不该拦** ——
        # 2026-09-17 实录第三轮：在第 41 页待了近 50 分钟（超过 720 秒上限），
        # 于是 withText=false，模型没有正文只好去调 reader_page_text，那一调还失败了。
        ink_active = bool(ink_pending)
        activated = bool(sel_text) or bool(
            acts and str(acts[-1].get("kind") or "") not in ("page-turn", "")
            and float(acts[-1].get("secondsAgo") or 0) <= dwell_max
        ) or bool(vis.get("has_ink")) or ink_active
        dwell_ok = (float(s.get("contextDwellMinSeconds") or 8) <= dwell <= dwell_max)             or (activated and dwell <= dwell_max) or ink_active
        text = ""
        text_truncated = False
        if cp.get("textAvailable") and cp.get("text") and dwell_ok:
            full = str(cp["text"]).replace("⟦VIEWPORT⟧", "").strip()
            limit = int(s.get("contextTextChars") or 1500)
            sections = self._split_page_sections(full)
            text = self._compose_ctx_text(sections, limit)   # 当前页永远全文；limit 只管前后页给多少
            text, skipped = self._ctx_text_ledger(text, sections, cp)
            if skipped:
                text += chr(10) + "（" + skipped + "刚才已经给过，这里不再重复；要看就直接往上翻本轮对话。）"
            text = self._ctx_mark_selection(text, sel_items)
        # 地点 + 到期卡数（2026-09-18 从慢板搬来）。**进指纹**：地点换了要重注入 ——
        # 它决定"该不该现在提"，而这正是当初把它放在板上的理由。
        # ⚠ 值由桥渲好，这里只拼句子：地点那套规则（别名优先、超 30 分钟标旧、
        # 「不知道」≠「别处」）只能有一份实现，抄第二份迟早两份说法不一样。
        bits = self._ambient_bits()
        amb_hint = ("。" + "；".join(bits)) if bits else ""
        # 带时刻：旧的删不掉（inject_items 只能追加），所以让它认得出哪条最新。
        state = ("【当前阅读状态 " + time.strftime("%H:%M:%S") + "】只认时刻最新的一条，更早的全部作废；"
                 "这是状态记录不是提问，不要回应本条。" + where + sel_hint + act_hint + ink_hint + amb_hint + "。")
        last_act = str((acts[-1].get("what") or acts[-1].get("kind") or "") if acts else "")[:40]
        # ⚠ 2026-09-17：这里原来把绘图折成 bool(vis.get("has_ink"))，而 vis 几乎总是空 ——
        #   于是绘图恒为 False，画多少笔语音侧指纹都不变、一次都不重注入。
        #   用户指出应当「把绘图的提示和那几个选中放在一个逻辑里共同算作改变内容」：
        #   现在绘图这一项取待命图的实际名单（每张图名唯一），与选中项并列进同一个指纹。
        fp_ink = ",".join(x["name"] for x in ink_pending)
        fp_state = "%s|%s|%s|%s|%s|%s" % (self._ctx["page_key"], sel_text[:60],
                                       "".join(k + t[:20] for k, t, _r, _l in sel_items), last_act, fp_ink, amb_hint)
        fp_text = "%s|%d|%s" % (self._ctx["page_key"], len(text), text[:30]) if text else ""
        # 语音侧预算只截正文，位置/选区提示和结尾的静默约定必须完整保留（否则正文一长就把「不要回应本条」切掉了）
        vbudget = int(s.get("contextVoiceChars") or 700)
        head = "(" + where + sel_hint_voice + ink_hint_voice + act_hint + amb_hint
        tail = "。回答以本条为准；状态记录，不要回应本条。)"
        if text and s.get("contextVoiceText"):
            room = max(0, vbudget - len(head) - len(tail) - 40)
            body_v = "。页面内容：" + self._compose_ctx_text(sections, room)   # 当前页全文，预算只管前后页
        else:
            body_v = "。你看不到页面内容；涉及页面内容、圈画、选区的问题一律立刻委派后台"
        voice = head + body_v + tail
        # 给语音侧的极短清单：几项、什么类型、开头几个字。不含正文 —— 它只用来回答
        # "我选中了什么/几项"，不该让语音模型觉得自己已经掌握了页面内容。
        per = int(s.get("contextVoiceSelectionChars") or 0)
        if sel_items:
            if per > 0:
                # 逐项编号 + 完整内容：用户问"这几项分别是什么"时它能直接念，不必委派。
                # 语音侧给标签但**不给 id**：语音模型没有工具，给它 id 只是噪音；
                # 它按编号说「对第 2 项做卡」，后台那份清单里同一个编号带着 id
                body_items = "".join(
                    "%s（%d）%s%s：「%s」" % (chr(10), i + 1, _SEL_KIND_LABEL.get(k, "选中的内容"),
                                          ("〔%s〕" % lb) if lb else "", t[:per])
                    for i, (k, t, rf, lb) in enumerate(sel_items))
            else:
                body_items = "：" + "、".join(
                    "%s「%s」" % (_SEL_KIND_LABEL.get(k, "选中的内容"), t[:24])
                    for k, t, rf, lb in sel_items)
            sel_list = ("【选中清单】此刻共 %d 项%s%s这是最新的一份，之前的清单作废。"
                        "问选中了什么、几项、内容是什么，照这条直接答，不必委派；"
                        "但**要动手的事照旧委派后台**（划线、做卡、写便签、翻页、搜索）——"
                        "手里有内容不等于这些事你自己做。"
                        "委派时：说清楚是对**第几项**做什么（「对第 2 项做张卡」），"
                        "需要理解的内容可以连原文一起带过去；"
                        "但**不要自己重打一遍原文当定位依据** —— 这些编号后台看到的是同一套，"
                        "它按编号取得到一字不差的原文，你转述时漏一个字就锚不上。") % (len(sel_items), body_items, chr(10))
        else:
            sel_list = "【选中清单】此刻没有任何选中项（之前的清单作废）。"
        return {"state": state, "text": text, "text_truncated": text_truncated, "voice": voice,
                "sel_list": sel_list, "fp_sel": "|".join(k + rf + t[:20] for k, t, rf, lb in sel_items), "fp_state": fp_state, "fp_text": fp_text,
                "fp_voice": fp_state + "|" + fp_text[:20]}

    def _ctx_text_ledger(self, text: str, sections: dict, cp: dict):
        """按页记账，去掉刚给过的那部分正文。返回 (裁剪后的正文, 被省掉了什么的说明)。

        ⚠ 只按页判断，不按字符串比对：翻页后"上一页末尾"是那一页正文的**子串**，
        哈希对不上，而人眼看来就是同一段。按页记账才抓得住这种重复。
        """
        try:
            window = float(self.settings.get("contextTextResendMinutes") or 0) * 60
            if window <= 0:
                return text, ""
            ledger = self._ctx.setdefault("sent_pages", {})
            now = time.time()
            for key in [k for k, v in ledger.items() if now - v[1] > window]:
                ledger.pop(key, None)
            page = cp.get("page")
            file_key = str(cp.get("file") or "")
            if not isinstance(page, int):
                return text, ""
            skipped = []
            # 当前页：整页给过才跳（只给过片段不算数）
            if ledger.get((file_key, page), ("", 0))[0] == "full" and sections.get("cur"):
                if sections["cur"] in text:
                    text = text.replace(sections["cur"], "（本页正文刚才已经给过）", 1)
                    skipped.append("本页正文")
            else:
                ledger[(file_key, page)] = ("full", now)
            for delta, name, key in ((-1, "上一页末尾", "prev"), (1, "下一页开头", "next")):
                part = sections.get(key) or ""
                if not part:
                    continue
                seen = ledger.get((file_key, page + delta))
                if seen and part[:40] in text:
                    # 连标题一起去掉：只删正文会留下一个空壳标题，读起来像"这里本该有东西但没了"
                    head = "【上一页末尾（衔接用，不可在此划线）】" if delta < 0 else "【下一页开头（衔接用，不可在此划线）】"
                    text = text.replace(part, "", 1).replace(head + chr(10) + chr(10), "").replace(head + chr(10), "")
                    skipped.append(name)
                elif not seen:
                    ledger[(file_key, page + delta)] = ("part", now)
            return text.strip(), "、".join(skipped)
        except Exception as e:   # noqa: BLE001
            self.log("ctx_ledger_error", message=clean(e))
            return text, ""

    @staticmethod
    def _ctx_mark_selection(text: str, sel_items: list) -> str:
        """把选中的文字在正文里标出来：⟦SELECTED n=K⟧…⟦/SELECTED⟧。

        正文里本来就有 ⟦HIGHLIGHT⟧ / ⟦CARD_START n=… id=…⟧ 这套标记，模型也有 textMarksHint
        教它怎么读。选中项照同一套标进去，后台就能从正文里**原地**取到一字不差的原文 ——
        不必调工具，也不必谁转述（转述日文少一个假名就锚不上）。
        找不到就不标：它仍然在编号清单里，只是这一项要动手时得调一次快照。
        """
        if not text:
            return text
        # ⚠ sel_items 是 (kind, text, ref, label) 四元组。2026-09-17 这里漏改成了两元组解包，
        # 每次都抛 "too many values to unpack"，而调用方 _ctx_inject_voice_selection 把异常
        # 吞进日志 —— 结果是**语音侧的选中清单整整一段时间一条都没投出去**，
        # 用户指着选中内容问，语音 AI 完全不知道在说什么。用索引取，不再靠解包位数。
        for i, item in enumerate(sel_items):
            kind = item[0] if len(item) > 0 else ""
            item_text = item[1] if len(item) > 1 else ""
            if kind != "text":
                continue   # 卡片/图/圈画在正文里已经有自己的标记（CARD_START 等）
            needle = (item_text or "").strip()
            if len(needle) < 4 or needle not in text:
                continue
            text = text.replace(
                needle,
                "\u27e6SELECTED n=%d\u27e7%s\u27e6/SELECTED\u27e7" % (i + 1, needle),
                1)
        return text

    @staticmethod
    def _split_page_sections(full: str) -> dict:
        """App 的正文分三段：【当前页之前】/【当前页结构化文字…】/【当前页之后】。没有分段头就整段算当前页。"""
        out = {"prev": "", "cur": "", "next": "", "cur_header": ""}
        cur_key = None
        saw = False
        for line in full.split("\n"):
            if line.startswith("【"):
                saw = True
                if "之前" in line:
                    cur_key = "prev"
                elif "之后" in line:
                    cur_key = "next"
                elif "当前页" in line and "锚点" not in line:
                    cur_key = "cur"
                    out["cur_header"] = line
                else:
                    cur_key = None   # 【锚点下标从哪来】之类的说明段：不进注入
                continue
            if cur_key:
                out[cur_key] += line + "\n"
        if not saw:
            out["cur"] = full
        for k in ("prev", "cur", "next"):
            out[k] = out[k].strip()
        return out

    @staticmethod
    def _compose_ctx_text(sections: dict, limit: int) -> str:
        """当前页全文**永远整段放进去，超过预算也不截**（用户 2026-09-14）；预算的余量先给上一页末尾（衔接），
        再给下一页开头；按阅读顺序拼：上一页 → 当前页 → 下一页。"""
        cur, prev, nxt = sections.get("cur", ""), sections.get("prev", ""), sections.get("next", "")
        header = sections.get("cur_header") or "【当前页】"
        parts = []
        remaining = limit - len(cur) - len(header) - 2
        if remaining > 120 and prev:
            take_prev = min(len(prev), remaining // 2 if nxt else remaining)
            piece = prev[-take_prev:]
            parts.append("【上一页末尾（衔接用，不可在此划线）】\n" + ("…" if take_prev < len(prev) else "") + piece)
            remaining -= len(piece) + 30
        parts.append(header + "\n" + cur)
        if remaining > 120 and nxt:
            take_next = min(len(nxt), remaining)
            piece = nxt[:take_next]
            parts.append("【下一页开头（衔接用，不可在此划线）】\n" + piece + ("…" if take_next < len(nxt) else ""))
        return "\n".join(parts)

    _ink_sent: set = set()   # 已插进线程的待命图（按文件名）。进程内即可：重启后至多重送一张。

    def _ink_standby_pending(self, snap: dict, quiet: bool = False) -> list[dict]:
        """桥待命着、本页还没送过的笔迹图（2026-09-17 用户定的做法）。

        桥在「这一笔刚稳定」时就抓好图放进 runtime/ink-standby/ 并登记 index.json。
        这里只做两件事：按当前书/页筛，按已送清单去重。**不再自己判断有没有新笔迹** ——
        原来那套判据读 currentPage.visual.drawing，而已发布的快照里没有这个字段
        （笔迹折进去后会被下一份页面更新冲掉），所以从上线起一次都没成立过。
        """
        if not self.settings.get("contextInkImage", True):
            return []
        d = BRIDGE_RUNTIME / "ink-standby"
        try:
            idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        cp = snap.get("currentPage") or {}
        cur_file, cur_page = str(cp.get("file") or ""), cp.get("page")
        max_age = float(self.settings.get("contextInkStandbyMaxAgeSeconds") or 900)
        out, stale = [], 0
        for e in (idx.get("images") or []):
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "")
            if not name or name in self._ink_sent:
                continue
            # 保质期（旧设计里的新鲜窗）：很久以前画的那一笔，此刻多半不是他说的「这个」，
            # 插进去只是噪音加 token。过期就作废，并且记一笔 —— 不声不响地丢，
            # 就是上一版「功能没生效却查不出为什么」的老毛病。
            age = self._ink_age_seconds(e)
            if age is not None and age > max_age:
                self._ink_sent.add(name)
                stale += 1
                continue
            if cur_file and str(e.get("file") or "") and str(e.get("file")) != cur_file:
                continue
            if cur_page is not None and e.get("page") is not None and e.get("page") != cur_page:
                continue
            p = d / name
            if not p.is_file():
                continue
            out.append({"name": name, "path": str(p), "bytes": e.get("bytes"),
                        "kind": str(e.get("kind") or "ink"),
                        "ordinal": e.get("ordinal"),
                        "age": age})
        if stale and not quiet:
            self.log("ctx_ink_stale", dropped=stale, maxAgeSeconds=max_age)
        out.sort(key=lambda x: (0 if x["kind"] == "ink" else 1,
                                x["ordinal"] if isinstance(x.get("ordinal"), int) else 0,
                                x["age"] if x.get("age") is not None else 0))
        return out

    @staticmethod
    def _ink_age_words(age: float | None) -> str:
        """离画完多久 → 这张图跟他这句话有多大关系（用户 2026-09-17 定的三档）。

        分档而不是一刀切，是因为「过期」和「相关性低」是两回事：
        画完两分钟再问，图还值得给，只是不该说得像刚画完那样笃定。
        """
        if age is None:
            return "时间不详"
        if age <= 30:
            return "%d 秒前刚画完，他说的「这个」几乎可以肯定就是图里这处" % int(age)
        if age <= 90:
            return "%d 秒前画的，很可能就是他指的东西" % int(age)
        # 这一档起点就是 90 秒，用整除会把 91 秒说成「1 分钟前」——四舍五入才不至于说小。
        return "%d 分钟前画过，未必是这句话说的，作参考" % max(2, round(age / 60))

    @staticmethod
    def _ink_age_seconds(entry: dict) -> float | None:
        """这一笔画完到现在多少秒。桥写的是 ISO-8601 UTC（DateTimeOffset "O" 格式）。"""
        raw = str(entry.get("capturedAtUtc") or "")
        if not raw:
            return None
        try:
            # "O" 带 7 位小数秒与 +00:00 —— 截到秒再按 UTC 解，跨平台都稳。
            return time.time() - calendar.timegm(
                time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            return None

    def _ctx_thread_scope(self):
        """去重记账只在**当前这条线程**里有效。线程一换（清空对话 / resume 到别的线程），
        整份作废 —— 新线程里那些内容根本不在，再说"刚才给过"就是指着空处让它去翻。

        自检式而不是"换线程的地方记得清"：换线程有三处以上，而"记得"正是这条链上
        今天已经栽过两次的东西（chip 到期没人上报、跳过正文不出声）。
        """
        if self._ctx.get("thread") == self.thread_id:
            return
        self._ctx["thread"] = self.thread_id
        self._ctx["fp"] = {"backend_state": "", "backend_text": "", "voice": "", "amb": "", "image": "", "sel": ""}
        self._ctx["sent_pages"] = {}
        self.log("ctx_scope_reset", threadId=(self.thread_id or "")[-12:])

    async def _ctx_inject_backend(self, with_text: bool, via_steer: bool = False,
                                  jev_bundle: dict | None = None) -> bool:
        """后台线程：状态变了投状态；正文指纹没投过再投正文。

        via_steer=True 时改用 turn/steer 送进**正在跑的那一轮** ——
        送的内容与 inject 完全一样（状态 + 带 [NN] 分区编号的正文 + ⟦SELECTED⟧ 标记）。
        ⚠ 2026-09-17 我第一版只送了状态那一行、把正文丢了，后台因此每次都得自己调快照取正文
        和块地址才能绑卡；用户指出后由后台 AI 自己确认：「自动推送里…那是精简版，
        选中文字还是省略号，没有完整正文和精确块地址，所以我才取了同一页的阅读快照」。
        """
        if not self.settings.get("contextInjectEnabled", True) or not (self.app and self.thread_id):
            return False
        # Jev mode has one admission rule for EVERY entry, including direct turns
        # and promise rescue. Timeout/missing/stale advice means no extra message;
        # it must never resurrect the legacy full-page injection. The original
        # user request still runs and the backend can read required data on demand.
        if self.settings.get("jevContextEnabled") and not jev_bundle:
            self.log("jev_context_skipped", reason="no_valid_recommendation", chars=0)
            return False
        if jev_bundle:
            if not str(jev_bundle.get("requestKey") or "").startswith(f"{self.thread_id}:{self.session_no}:"):
                self.log("jev_context_skipped", requestKey=jev_bundle.get("requestKey"),
                         reason="voice_scope_changed", chars=0)
                return False
            invalid = self._jev.invalid_reason(jev_bundle)
            skip = skip_context_reason(jev_bundle) if not invalid else None
            if skip:
                # No context means NO call, not an empty turn/steer. Do this
                # before building text/images or changing the delivered ledger.
                self.log("jev_context_skipped", requestKey=jev_bundle["requestKey"],
                         contextChoice=jev_bundle.get("contextChoice"),
                         contextProbability=jev_bundle.get("contextProbability"),
                         choice=jev_bundle["choice"], reason=skip, chars=0)
                return False
            if invalid:
                self.log("jev_fallback", requestKey=jev_bundle.get("requestKey"), reason=invalid)
                if self.settings.get("jevContextEnabled"):
                    self.log("jev_context_skipped", requestKey=jev_bundle.get("requestKey"),
                             reason=invalid, chars=0)
                    return False
                jev_bundle = None
        self._ctx_thread_scope()
        snap = self._ctx_snapshot()
        if not snap and not (jev_bundle and jev_bundle.get('choice') == 'review_deck'):
            return False
        snap = snap or {}
        fields = ()
        if jev_bundle:
            cp = snap.get("currentPage") or {}
            sections = self._split_page_sections(str(cp.get("text") or ""))
            body, fields = build_tool_context(jev_bundle, snap, sections["cur"], self._jev_task_state())
            digest = "jev-tool:" + hashlib.sha256(body.encode()).hexdigest()
            b = {"state": body, "text": "", "fp_state": digest, "fp_text": digest}
        else:
            b = self._ctx_build(snap)
        if not b.get("state"):
            return False
        fp = self._ctx["fp"]
        body = b["state"] if jev_bundle else None
        # 正文被去重跳过时要出声：静默省略会让模型以为"没有正文"，转头去调工具
        # （实录 2026-09-16：为此倒出 6,555 字的整页卡片列表，还调了两次）。
        text_skipped = bool(with_text and b["text"] and fp["backend_text"] == b["fp_text"])
        if with_text and b["text"] and fp["backend_text"] != b["fp_text"]:
            head_t = ("可见内容（已截断，只有开头 %d 字；问到后面的内容用 reader_page_text 取整页）：" % len(b["text"])) if b.get("text_truncated") else "可见内容（整页）："
            body = b["state"] + chr(10) + head_t + chr(10) + b["text"]
        elif fp["backend_state"] != b["fp_state"]:
            body = b["state"]
            if text_skipped:
                body += ("（本页正文连同页上卡片的内容与 id，本轮对话较早处已经给过，"
                         "往上翻本轮对话就有；要单独取某张卡用 reader_page_card_read 按 id 取，"
                         "别用 reader_page_cards 把整页倒出来。）")
        # 笔迹图：桥已经在每次笔迹稳定时抓好放着了，这里只挑本页没送过的。
        # 只在开口边沿（with_text）考虑 —— 快板那种状态刷新不带图。
        pending = (self._ink_standby_pending(snap) if with_text and
                   (not jev_bundle or "selection_regions" in fields or
                    ("selected_items" in fields and any(
                       it.get("kind") in ("drawing", "image") for it in snap.get("selectedItems", [])))) else [])
        if body is None and not pending:
            return False
        # ⚠ 2026-09-17：这里原来把笔迹图当 input_image 直接塞进注入的 developer 消息。
        #   语音委托起的那些轮会把线程历史转给另一个端点，而**那个端点只收 input_text**：
        #   400 invalid_enum_value "Invalid value: 'input_image'"。更糟的是这条毒 item 留在
        #   历史里，之后**每一轮都失败** —— 实录 12:01/12:02/12:03 连三轮 failed，用户让它制卡
        #   完全没有回应，而语音那头还在说「我确认一下」。
        #   所以图不再进历史：只用一行文字说明有笔迹，要看就调 reader_visual_image
        #   （工具返回走的是另一条通道，不受这个限制）。指纹照旧，保证同一版笔迹只提一次。
        note = ""
        if pending:
            lines = []
            for i, x in enumerate(pending, 1):
                who = ("整页笔迹（这页所有普通笔迹合在一张里）" if x["kind"] == "ink"
                       else "选区 %s" % (x["ordinal"] if x.get("ordinal") is not None else "?"))
                lines.append("附图 %d = %s：%s" % (i, who, self._ink_age_words(x.get("age"))))
            # ⚠ 措辞要分路：图只在 steer 那条路上真的随 localImage 送出去；
            # inject_items 这条路**一张图都不带**（2026-09-17 起图不再进历史）。
            # 说成"下面附了图"而实际没附，模型会去找一张不存在的图 —— 又一处
            # "说明指向不存在的东西"。
            if via_steer:
                note = ("（下面按顺序附了 %d 张图，逐张对应：" % len(pending)
                        + "；".join(lines)
                        + "。他说「选区 1/选区 2」时按这里的编号认。）")
            else:
                note = ("（他在这页有 %d 处笔迹，这里不附图：" % len(pending)
                        + "；".join(lines)
                        + "。要看图调 reader_visual_image；"
                        + "他说「选区 1/选区 2」时按这里的编号认。）")
        text_part = (body if body is not None else b["state"]) + ((chr(10) + note) if pending else "")
        content = [{"type": "input_text", "text": text_part}]

        if jev_bundle:
            target = (self._turn or {}).get("id")
            if (not via_steer or not self.backend_busy or not target or
                    target != jev_bundle.get("backendTurnId", target)):
                self.log("jev_context_skipped", requestKey=jev_bundle["requestKey"],
                         reason="backend_turn_changed", chars=0)
                return False
            if not self._jev.claim_injection(jev_bundle):
                self.log("jev_context_skipped", requestKey=jev_bundle["requestKey"],
                         reason="already_injected_this_round", chars=0)
                return False

        # 后台正在跑的那一轮读不到我们现在追加的东西（它的上下文早就组好了）。
        # 所以忙碌时**先不注入**，把最新一份压在这里，等那轮结束再送 —— 见 _ctx_flush_pending。
        # 这样用户在 AI 干活期间连改三次选中，历史里也只落最新的一条，
        # 而不是三条（其中两条一生下来就是过期的）。
        if self.backend_busy and not via_steer:
            # ⚠ via_steer 就是专门为「后台正在跑」准备的完整投递，别让这条老的延后分支截胡 ——
            # 它只发状态那一行，2026-09-17 我就是这样把正文弄丢的。
            self._ctx_pending = {"content": content, "fp_state": b["fp_state"],
                                 "fp_text": b["fp_text"] if (with_text and b["text"]) else None,
                                 "inkNames": [x["name"] for x in pending],
                                 "chars": len(body or ""), "at": time.time()}
            # 想让在跑的那一轮也看见，只有 turn/steer 一条路；默认关着，原因见 steer_running_turn
            if self._turn:
                await self.steer_running_turn(
                    "【状态更新·不是新任务】" + b["state"] +
                    chr(10) + "继续完成你手上的事；后面用到「选中/当前页」时以这条为准。", tag="ctx")
            self.log("ctx_backend_deferred", chars=len(body or ""), page=self._ctx["page_key"][-40:])
            return True
        if via_steer:
            # content 恒为纯文本（图不再以 input_image 进历史）；图另走 localImage 路径参数。
            only_text = all(c.get("type") == "input_text" for c in content)
            if only_text:
                whole = "".join(c.get("text") or "" for c in content)
                wire_text = (("" if jev_bundle else "【当前阅读状态·状态记录，不是提问】") + whole +
                             chr(10) + "继续完成手上的事；用到「选中/当前页」时以这条为准。")
                res = await self.steer_running_turn(wire_text,
                    tag="delegation", image_paths=[x["path"] for x in pending])
                if res.get("ok"):
                    if jev_bundle:
                        self.log("jev_injected", requestKey=jev_bundle["requestKey"],
                                 choice=jev_bundle["choice"], probability=jev_bundle["probability"],
                                 waitMs=jev_bundle["waitMs"], turnId=res.get("turnId"))
                    fp["backend_state"] = b["fp_state"]
                    if with_text and b["text"]:
                        fp["backend_text"] = b["fp_text"]
                    for x in pending:
                        self._ink_sent.add(x["name"])
                    self.log("ctx_steer", chars=len(wire_text), page=self._ctx["page_key"][-40:],
                             withText=bool(with_text and b["text"]), body=wire_text,
                             requestKey=jev_bundle.get("requestKey") if jev_bundle else None,
                             backendTurnId=res.get("turnId"),
                             contextSource="jev-tool-data" if jev_bundle else "original-fallback",
                             dataFields=list(fields),
                             images=[x["name"] for x in pending])
                    return True
                self.log("ctx_steer_fallback", reason=str(res.get("error"))[:80])
                if jev_bundle or self.settings.get("jevContextEnabled"):
                    # Do not append to thread history after a failed/late steer:
                    # that would leak this round's packet into a future round.
                    return False
                # 没赶上就照常追加，被下一轮读到
        try:
            await self.app.call("thread/inject_items", {"threadId": self.thread_id, "items": [
                {"type": "message", "role": "developer", "content": content}]}, timeout=30)
        except Exception as e:
            self.log("ctx_backend_error", message=clean(e))
            return False
        fp["backend_state"] = b["fp_state"]
        if with_text and b["text"]:
            fp["backend_text"] = b["fp_text"]
        # ⚠ 2026-09-18：这里原来还有一段 `if image is not None: fp["image"] = ink_fp` ——
        #   `image`/`ink_fp` 是 base64 图那条路的变量，那条路 2026-09-17 删掉时这三处引用
        #   漏删了，于是**每次走到这儿都 NameError**：inject_items 已经成功、指纹已经更新，
        #   然后异常抛出去 —— ctx_backend 一次都没记上（实录：末次 09-17 12:00:57，之后
        #   只剩 ctx_steer），而 turn() 是**不带 try** 调这个函数的，所以快照就绪且状态有变时
        #   整轮根本起不来。表现就是"让他做事却一直没有回应"。
        #   笔迹的去重现在靠 _ink_sent（按图名），不需要 fp["image"]。
        for x in pending:
            self._ink_sent.add(x["name"])
        self.log("ctx_backend", withText=bool(with_text and b["text"]), chars=len(body or ""),
                 images=[x["name"] for x in pending],
                 page=self._ctx["page_key"][-40:], body=text_part,
                 contextSource="jev-tool-data" if jev_bundle else "original-fallback", dataFields=list(fields))
        return True

    async def _ctx_flush_pending(self):
        """把忙碌期间压着的那份状态送出去。只送最新一份 —— 中间那些一出生就过期了。

        叫在 turn/completed 的处理里：那一刻上一轮刚结束、下一轮（多半是语音委托的那轮）
        还没起，注入正好赶得上被它读到。
        """
        pend = getattr(self, "_ctx_pending", None)
        if not pend or not (self.app and self.thread_id):
            return
        self._ctx_pending = None
        if self.settings.get("jevContextEnabled"):
            self.log("jev_context_skipped", reason="legacy_pending_context", chars=0)
            return
        try:
            await self.app.call("thread/inject_items", {"threadId": self.thread_id, "items": [
                {"type": "message", "role": "developer", "content": pend["content"]}]}, timeout=30)
        except Exception as e:   # noqa: BLE001
            self.log("ctx_backend_error", message=clean(e))
            return
        fp = self._ctx["fp"]
        fp["backend_state"] = pend["fp_state"]
        if pend.get("fp_text"):
            fp["backend_text"] = pend["fp_text"]
        for name in pend.get("inkNames") or []:
            self._ink_sent.add(name)
        self.log("ctx_backend", withText=bool(pend.get("fp_text")), chars=pend["chars"],
                 deferredSec=round(time.time() - pend["at"], 1), page=self._ctx["page_key"][-40:],
                 body=self._log_body("".join(c.get("text") or "" for c in pend["content"]
                                             if isinstance(c, dict))))

    def _rollout_backup(self, thread_id: str) -> str | None:
        """压缩前把落盘记录复制一份。

        ⚠ thread/compact/start 会**就地重写** `~/.codex/sessions/**/rollout-*.jsonl`，
        把完整记录换成摘要，无警告无报错（openai/codex#44363，仍开着）。
        那份文件是链路页与历史的唯一来源，所以动它之前先留底。
        """
        try:
            import glob as _glob
            import shutil
            hits = _glob.glob(str(Path.home() / ".codex" / "sessions" / "**" / ("*%s*.jsonl" % thread_id)),
                              recursive=True)
            if not hits:
                return None
            src = Path(max(hits, key=os.path.getmtime))
            dst = src.with_name(src.stem + ".pre-compact-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(src, dst)
            return str(dst)
        except Exception as e:   # noqa: BLE001
            self.log("rollout_backup_error", message=clean(e))
            return None

    async def thread_compact(self, thread_id: str, reason: str = "manual") -> dict:
        """压缩线程。

        能把上下文压短，对话身份也保住（比「开新对话」强，那是把上下文整个丢掉）。
        但它**同时会销毁落盘的完整记录**，所以先备份、且默认不自动跑。
        """
        await self.ensure_app()
        before = await self.thread_item_count(thread_id)
        backup = self._rollout_backup(thread_id)
        t0 = time.time()
        await self.app.call("thread/compact/start", {"threadId": thread_id}, timeout=210)
        after = await self.thread_item_count(thread_id)
        self.log("thread_compacted", threadId=thread_id[-12:], reason=reason,
                 before=before, after=after, seconds=round(time.time() - t0, 1),
                 backup=(backup or "")[-60:])
        return {"ok": True, "before": before, "after": after, "reason": reason, "backup": backup}

    async def thread_item_count(self, thread_id: str) -> int:
        """线程里现有多少条记录。压缩前后各数一次，好知道到底省了多少。"""
        try:
            res = await self.app.call("thread/items/list", {"threadId": thread_id, "limit": 400}, timeout=30)
            return len((res or {}).get("data") or [])
        except Exception:   # noqa: BLE001
            return -1

    async def maybe_autocompact(self) -> None:
        """线程长到阈值就自动压一次。只在后台空闲时做 —— 压缩本身要跑一轮模型。"""
        if not self.settings.get("threadAutoCompact", True):
            return
        if self.backend_busy or not (self.app and self.thread_id):
            return
        limit = int(self.settings.get("threadCompactItems") or 220)
        if time.time() - float(self._last_compact_at or 0) < 600:
            return   # 刚压过就别又压：压缩自己也要花一轮
        n = await self.thread_item_count(self.thread_id)
        if n < limit:
            return
        self._last_compact_at = time.time()
        try:
            await self.thread_compact(self.thread_id, reason="auto/%d" % n)
        except Exception as e:   # noqa: BLE001
            self.log("thread_compact_error", message=clean(e))

    async def steer_running_turn(self, text: str, tag: str = "state",
                                 image_paths: list[str] | None = None) -> dict:
        """把内容插进**正在跑的那一轮**。

        为什么需要它：inject_items 是往线程上追加，已经开跑的轮不会回头去读 ——
        所以「后台正在干活时用户改了选中」这件事，今天只能等它跑完再补。
        turn/steer 能挂进在跑的轮（2026-09-16 实测：内容以 userMessage 落在该轮里）。

        ⚠ 措辞必须是被动的状态通报。实测用祈使句（「立刻停止，改为…」）会把那一轮
        打哑 —— 一条回答都不产出，在语音里就是「AI 不理我」，比不插还糟。
        """
        if not self.settings.get("turnSteerEnabled", True):
            return {"ok": False, "error": "已关闭（turnSteerEnabled）"}
        turn = self._turn
        if not (self.backend_busy and turn and turn.get("id") and self.thread_id):
            return {"ok": False, "error": "当前没有正在跑的轮"}
        try:
            # localImage 是 app-server 认的正门（实测 turn/start 与 turn/steer 同一套变体：
            # text / image{url} / localImage{path} / audio / localAudio / skill / mention）。
            # 给路径而不是 base64：历史里只留引用，不会再出现那条毒 item。
            steer_input = [{"type": "text", "text": text}]
            for path in (image_paths or []):
                steer_input.append({"type": "localImage", "path": path})
            await self.app.call("turn/steer", {"threadId": self.thread_id,
                                               "expectedTurnId": turn["id"],
                                               "input": steer_input}, timeout=20)
        except Exception as e:   # noqa: BLE001
            self.log("turn_steer_error", message=clean(e), tag=tag)
            return {"ok": False, "error": clean(e)}
        self.log("turn_steer", tag=tag, chars=len(text), turnId=str(turn["id"])[-12:],
                 images=len(image_paths or []))
        return {"ok": True, "turnId": turn["id"], "chars": len(text)}

    #: 语音模型嘴上答应要做事的说法。它**自己没有任何工具**，所以说了这些就必然要委派后台；
    #: 说了却没委派 = 这件事根本没发生（2026-09-17 用户实录：「好的，我这就处理」之后 75 秒无事发生，
    #: 直到他追问「你有做吗」才真派活）。同类毛病 2026-09-14 也记过两次。
    _PROMISE_MARKS = ("我这就", "我来做", "这就帮你", "马上做", "这就做", "稍等", "我处理",
                      "这就处理", "帮你做", "我去做", "我来处理", "正在做")

    #: 过去时的**断言**：它说事情已经做完了。原来只盯未来时的承诺，于是 2026-09-17
    #: 用户那次「什么都不做就说自己做好了」一次都没触发补投 —— 缺的不是机制，是词表。
    #: ⚠ 只收**动作类**完成，不收「明白了」「知道了」这种应答：语音模型自己答得了的问题
    #: 本来就不该委派，把那些也算进来会天天无端补投。
    _DONE_MARKS = ("已经帮你", "已经加", "已经记", "已经创建", "已经保存", "已经发", "已经放",
                   "已经做", "已经建", "已经改", "已经删", "已经写", "已经设置", "已经更新",
                   "做好了", "加好了", "记下了", "建好了", "存好了", "写好了", "设好了",
                   "创建好了", "保存好了", "添加好了", "处理好了", "都弄好了", "搞定了")

    #: 第三类：声称**正在做**。2026-09-18 实录抓到的漏网之鱼 ——
    #: 用户「再帮我做一次」→ 它答「嗯，我看一下。」（零委派）→ 50 秒后用户追问
    #: 「你好像没在做呀」→ 它答「还在做，马上好。」——**断言工作正在进行，而这一刻
    #: 后台调用是零**。这比承诺和完成断言都更该抓：承诺还可能是刚要动，
    #: 「还在做」则是明确报告一个不存在的进行态。
    #: ⚠ 老表里有「马上做」没有「马上好」、有「我处理」没有「我看一下」，
    #:   于是整整 50 秒一条补投都没发。词表这条路的毛病就在这儿：它只抓写下来的说法。
    _PROGRESS_MARKS = ("还在做", "马上好", "就好了", "快好了", "我看一下", "我看下",
                       "我确认一下", "我再确认", "正在处理", "正在查", "这就看",
                       "让我看看", "我查一下", "我试一下")

    def _promise_watch(self, role: str, text: str):
        """助手答应了就盯着：一段时间内没委派，我们替它把活派下去。"""
        if not self.settings.get("promiseWatchEnabled", True):
            return
        t = (text or "").strip()
        if role == "user":
            if not t:
                return
            now = time.time()
            self._last_user_ask = (now, t, self._delegation_seq)
            # ⚠ 不看措辞的那条判据（2026-09-18）：词表只抓写下来的说法，
            #   而它每次换个说法就漏 —— 实录里「嗯，我看一下。」「还在做，马上好。」
            #   两句都不在表里，于是 50 秒空转、用户追问两次一条补投都没发。
            #   这里改判**结构**：你在窗口内连说两次，而这中间一次委派都没有 ——
            #   那不管它嘴上说了什么，都该把活补下去。
            window = float(self.settings.get("promiseRepeatWindowSeconds") or 90.0)
            self._user_asks = [x for x in self._user_asks if now - x[0] <= window]
            self._user_asks.append((now, t, self._delegation_seq))
            if (len(self._user_asks) >= 2
                    and self._user_asks[0][2] == self._delegation_seq
                    and self.settings.get("promiseWatchEnabled", True)):
                first = self._user_asks[0]
                self._user_asks = []          # 补一次就清账，别连着补
                self._promise_pending = (now, first[1], t, False)
                asyncio.run_coroutine_threadsafe(self._promise_rescue(), self.loop)
            return
        if role != "assistant" or not t:
            return
        claimed = any(m in t for m in self._DONE_MARKS)
        if (not claimed
                and not any(m in t for m in self._PROMISE_MARKS)
                and not any(m in t for m in self._PROGRESS_MARKS)):
            return
        ask = getattr(self, "_last_user_ask", None)
        if not ask or time.time() - ask[0] > 60:
            return          # 找不到对应的请求就别乱补
        # ⚠ 把**提问那一刻的委派序号**一起带上。2026-09-18 实录：后台 20:37:27–41
        #   明明做完了（reader_anki_draft 成功），语音模型随后说了句「已经帮你做了」，
        #   于是这里又立了标记、6 秒后补投照发 —— 理由还写着「一次后台调用都没有发生」，
        #   那句是假的，白起一轮后台、侧栏多出一个框。
        #   判据改成：**从他提问到现在，委派序号有没有变过**。变过 = 真派过活，不补。
        self._promise_pending = (time.time(), ask[1], t, claimed, ask[2])
        asyncio.run_coroutine_threadsafe(self._promise_rescue(), self.loop)

    async def _promise_rescue(self):
        """补投的外壳：只负责让异常出声。

        ⚠ 它是被 run_coroutine_threadsafe 调度的，返回的 Future 没人取 —— 里面抛什么
        都不会有任何痕迹。2026-09-18 就这么栽过一次：一个参数名撞了（log 的第一个位置
        参数就叫 kind），整条补投链路一声不吭地死了，日志、事件、侧栏全都干干净净。
        """
        try:
            await self._promise_rescue_inner()
        except Exception as e:   # noqa: BLE001
            self.log("promise_rescue_error", message=clean(e), stage="outer")

    async def _promise_rescue_inner(self):
        """等一会儿；还是没委派就自己起一轮，把最近几条对话交给后台。"""
        pend = self._promise_pending
        if not pend:
            return
        wait = float(self.settings.get("promiseWatchSeconds") or 6.0)
        await asyncio.sleep(wait)
        if self._promise_pending is not pend:
            return          # 期间已经委派过（或又有新承诺），不补
        # ⚠ 对话还在动的时候，承诺**不算掉了** —— 只是还没轮到它。
        #   2026-09-20 实录：用户一口气说完八项（23:36:44），补投 6 秒后就开跑
        #   （23:36:50），而真委派 23:36:56 才到 —— 提问到委派整整 12 秒，因为长句
        #   转写 + 语音模型处理本来就慢。结果同一个问题被答了两遍，用户看到的就是
        #   「反复一轮轮回答同一个问题」。
        #   单纯调大窗口是钝的：真掉了的那次也要跟着多等。判据应当是「对话是否还在
        #   进行」—— 在说话就续等，安静下来再算。
        idle_needed = float(self.settings.get("promiseIdleSeconds") or 4.0)
        deadline = time.time() + float(self.settings.get("promiseMaxWaitSeconds") or 45.0)
        # ⚠ 用 getattr：补投的测试桩是个轻量 Runner，没有这两个语音状态属性。
        #   直接取会 AttributeError，而这个协程的异常进了没人取的 Future —— 整条链路无声。
        def _voice_busy():
            return bool(getattr(self, 'user_speaking', False)
                        or getattr(self, 'assistant_speaking', False))
        while _voice_busy() and time.time() < deadline:
            await asyncio.sleep(idle_needed)
            if self._promise_pending is not pend:
                return      # 续等期间委派到了
        if self._promise_pending is not pend:
            return
        if self.backend_busy or not (self.app and self.thread_id):
            self._promise_pending = None
            return
        # ⚠ 最硬的那条判据放最前：**后台刚刚跑过就别补**。
        #   2026-09-18 第三次误触发的实录：委派 21:54:57 发生，而用户那句转写 21:54:58 才定稿
        #   —— 我把"当时的委派序号"记在转写定稿那一刻，于是"从提问到现在没有新委派"成立，
        #   补投照发，正文还写着「一次后台调用都没有发生」，而后台 21:54:58–55:08 刚把卡做好。
        #   序号比较是在绕圈子：真正要回答的是"后台到底动没动"，那就直接问它。
        #   宁可漏补（顶多它真没做、你再说一次），也不要白起一轮 + 在侧栏多一个框 + 说一句假话。
        recent = float(self.settings.get("promiseRecentBackendSeconds") or 30.0)
        if self._turn is not None or (time.time() - self._backend_done_at) < recent:
            self.log("promise_rescue_skipped", why="backend-ran-recently",
                     sinceSec=round(time.time() - self._backend_done_at, 1),
                     said=str(pend[2])[:60])
            self._promise_pending = None
            return
        if len(pend) > 4 and pend[4] != self._delegation_seq:
            # 这中间真的派过活 —— 不管它嘴上怎么说，都不该补（更不该说"零调用"）。
            self.log("promise_rescue_skipped", why="delegated-since-ask",
                     said=str(pend[2])[:60])
            self._promise_pending = None
            return
        self._promise_pending = None
        said, ask = pend[2], pend[1]
        claimed = len(pend) > 3 and pend[3]
        recent = self._recent_dialogue()
        # ⚠ 别用 kind= —— log() 的第一个位置参数就叫 kind，撞上去是 TypeError，
        # 而这个协程的异常进了 run_coroutine_threadsafe 的 Future，没人取 = 全程无声。
        self.log("promise_rescue", why="claimed" if claimed else "promised",
                 said=said[:60], ask=ask[:80], waited=wait, lines=recent.count(chr(10)) + 1)
        try:
            # 2026-09-18 用户定的形态：把**最近几条对话**推给后台，让它自己判断该做什么。
            # 原来只推用户那一句，丢了上下文（追问、补充条件、语音侧说它做了什么都不在里面）。
            head = ("【补投】语音侧刚才对用户说事情已经做完了，但这中间**一次后台调用都没有发生**，"
                    "所以那件事实际上没做。语音模型自己没有任何工具，它说做完就是没做。"
                    if claimed else
                    "【补投】语音侧答应了用户要做这件事，但一直没把活派下来。")
            await self.turn(
                head + "下面是最近几条对话，你自己判断有没有该做的事：" + chr(10) + recent
                + chr(10) + "——如果确实有该做的，现在做掉；如果只是闲聊、"
                "或者这事本来就不需要动工具，那就什么都别做，也不要开口。",
                record_user=False)
        except Exception as e:   # noqa: BLE001
            self.log("promise_rescue_error", message=clean(e))

    def _recent_dialogue(self, keep: int = 6, within: float = 180.0) -> str:
        """最近几条转写，补投时连上下文一起交给后台。"""
        now = time.time()
        # ⚠ transcripts 是 deque —— **不支持切片**。别处都写 list(...)[-n:]，这里漏了，
        # 于是补投第一次真触发就死在 "sequence index must be integer, not 'slice'"
        # （2026-09-18 14:00 实录；能看见它是因为今天给补投加了出声外壳）。
        rows = [(r, x) for at, r, x in list(self.transcripts)[-40:]
                if x and now - at <= within][-keep:]
        return chr(10).join(
            "%s：%s" % ("用户" if r == "user" else "语音助手", str(x).strip()[:300])
            for r, x in rows) or ("用户：" + (getattr(self, "_last_user_ask", (0, ""))[1] or ""))

    def _jev_key(self, user_turn_id):
        return f"{self.thread_id}:{self.session_no}:{user_turn_id}" if user_turn_id else None

    def _jev_dialogue(self):
        return [("用户：" if role == "user" else "助手：") + text
                for _at, role, text in list(self.transcripts) if role in ("user", "assistant")]

    def _jev_task_state(self):
        # Actual tool events only. A backend turn starting is not proof that the
        # requested artifact is being made, nor is voice commentary a receipt.
        return [{k: event.get(k) for k in ("seq", "kind", "tool", "status", "text")}
                for event in list(self.events)
                if event.get("kind") in ("item/started", "item/completed")
                and event.get("itemType") == "mcpToolCall"][-8:]

    async def _ctx_on_delegation(self, jev_key=None, delegation_seq=None):
        """Each voice user round may deliver one Jev-approved packet to its backend turn."""
        if not self.settings.get("contextInjectEnabled", True):
            return
        jev_enabled = self.settings.get("jevContextEnabled")
        if jev_enabled and not str(jev_key or "").startswith(f"{self.thread_id}:{self.session_no}:"):
            self.log("jev_context_skipped", requestKey=jev_key, reason="voice_scope_changed", chars=0)
            return
        if jev_enabled and not self._jev.claim_delegation(jev_key):
            return
        try:
            jev_wait = (asyncio.create_task(self._jev.for_delegation(jev_key))
                        if jev_key and self.settings.get("jevContextEnabled") else None)
            if str(self.settings.get("contextInjectOn") or "delegationSteer") != "delegationSteer":
                if jev_wait:
                    jev_wait.cancel()
                await self._ctx_inject_backend(with_text=True)
                return
            # 等这一轮真的起来：delegation 之后 22~60 ms 才 turn/started
            deadline = time.monotonic() + float(self.settings.get("steerWaitSeconds") or 3.0)
            while time.monotonic() < deadline:
                if self.backend_busy and self._turn and self._turn.get("id"):
                    break
                await asyncio.sleep(0.05)
            turn_id = (self._turn or {}).get("id")
            bundle = await jev_wait if jev_wait else None
            if not jev_enabled and delegation_seq is not None and delegation_seq != self._delegation_seq:
                self.log("jev_fallback", requestKey=jev_key, reason="newer_delegation")
                return
            if bundle and turn_id != (self._turn or {}).get("id"):
                self.log("jev_fallback", requestKey=jev_key, reason="backend_turn_changed")
                bundle = None
            if bundle:
                bundle['backendTurnId'] = turn_id
            if bundle and bundle.get('actionNotice'):
                if self._jev.claim_injection(bundle):
                    notice = bundle['actionNotice']
                    result = await self.steer_running_turn(notice, tag='artifact-attempt')
                    self.log('artifact_resend_notice', requestKey=jev_key, turnId=turn_id,
                             ok=result.get('ok', False), text=notice)
                    if result.get('ok'):
                        self.log('ctx_steer', chars=len(notice), withText=False, body=notice,
                                 requestKey=jev_key, backendTurnId=turn_id,
                                 contextSource='jev-resend-attempt', dataFields=['artifact_attempt'], images=[])
                return
            await self._ctx_inject_backend(with_text=True, via_steer=True, jev_bundle=bundle)
        except Exception as e:   # noqa: BLE001
            self.log("ctx_delegation_error", message=clean(e))

    async def _ctx_on_speech(self):
        """开口边沿：只管语音侧。

        后台那一份 2026-09-16 起改到 delegation.created 触发（见 _ctx_on_delegation）；
        contextInjectOn=speech 时退回老行为，留作对照。
        """
        if not self.settings.get("contextInjectEnabled", True):
            return
        try:
            if str(self.settings.get("contextInjectOn") or "delegation") == "speech":
                await self._ctx_inject_backend(with_text=True)
            await self._ctx_inject_voice_selection()
            await self._ctx_inject_voice_ambient()
            if str(self.settings.get("contextVoiceMode") or "off") != "edge" or self.session_state != "connected" or not (self.app and self.thread_id):
                return
            snap = self._ctx_snapshot()
            if not snap:
                return
            b = self._ctx_build(snap)
            if not b.get("state") or self._ctx["fp"]["voice"] == b["fp_voice"]:
                return
            await self.app.call("thread/realtime/appendText", {"threadId": self.thread_id, "text": b["voice"], "role": "developer"}, timeout=15)
            self._ctx["fp"]["voice"] = b["fp_voice"]
            self.log("ctx_voice", chars=len(b["voice"]), page=self._ctx["page_key"][-40:],
                     body=self._log_body(b["voice"]))
        except Exception as e:
            self.log("ctx_voice_error", message=clean(e))

    def _ambient_bits(self) -> list[str]:
        """地点 + 到期卡数，拼成句子的零件。**一份实现服务两处** ——
        后台的状态行和语音侧那一行都用它，否则两处措辞迟早不一样。
        值由桥渲好（/voice-core/ambient），这里只挑要不要说。
        """
        if not self.settings.get("contextAmbientEnabled", True):
            return []
        amb = fetch_ambient(float(self.settings.get("contextAmbientCacheSeconds") or 60.0))
        if not amb:
            return []
        bits = []
        if amb["place"] and amb["place"] != "不知道":
            bits.append("他现在在" + amb["place"])
        if amb["reviewDue"] > 0:
            # ⚠ 光报个数会让模型在他说「复习一下」时临场发明取数路径 ——
            #   2026-09-21 实录：它拐去问阅读器，拿到「来源不在线」，于是回他
            #   「把阅读器切回前台我就接着试」。复习读的是本机文件，与看不看书无关。
            bits.append("到期待复习卡共 %d 张（陈述，看到不用动；他要复习就调 "
                        "review_deck，可用 book/pages/kind 指定范围，"
                        "**不需要他开着书**）" % amb["reviewDue"])
        # 生成物状态变更（用户 2026-09-19：「每个需要确定的生成物在一定时间内被更改
        # 状态都推送到文字 AI 那边进行通知」）。它回答的是「我刚才保存了吗」这类问题 ——
        # 此前只能靠模型自己想起来去查卡库，实测不可靠。
        for line in (amb.get("artifactChanges") or [])[:4]:
            bits.append("刚刚：" + line + "（事实，据此回答「存了吗」，不必再查）")
        return bits

    async def _ctx_inject_voice_ambient(self):
        """把「他现在在哪 + 到期卡数」单独投给语音侧（2026-09-18 补）。

        为什么要单独一条：地点原来随慢板到语音侧，2026-09-18 把它搬进注入器时，
        我只加进了 b["voice"] —— 而 contextVoiceMode 线上是 off，那份根本不发，
        于是语音模型**丢了地点**。功能上不缺（待办该不该说已由程序按地点判完，
        结论写在待办行上），缺的是它自己掂量时手上没有这个事实。
        ⚠ 不用打开整个 contextVoiceMode 来补：那会连整页正文一起塞进 700 字预算。
        这一行只有十几个字、变化极慢（地点跨 30 分钟才换一档说法），另有独立指纹。
        """
        if not self.settings.get("contextAmbientToVoice", True):
            return
        if self.session_state != "connected" or not (self.app and self.thread_id):
            return
        bits = self._ambient_bits()
        if not bits:
            return
        line = "（" + "；".join(bits) + "。状态记录，不要回应本条。）"
        if self._ctx["fp"].get("amb") == line:
            return
        try:
            await self.app.call("thread/realtime/appendText",
                                {"threadId": self.thread_id, "text": line, "role": "developer"},
                                timeout=15)
            self._ctx["fp"]["amb"] = line
            self.log("ctx_voice_ambient", chars=len(line), body=line)
        except Exception as e:   # noqa: BLE001
            self.log("ctx_voice_ambient_error", message=clean(e))

    async def _ctx_inject_voice_selection(self):
        """把「选中清单」投给语音侧。清单没变就不投（避免每次开口都多一条）。"""
        if not self.settings.get("contextVoiceSelection", True):
            return
        if self.session_state != "connected" or not (self.app and self.thread_id):
            return
        try:
            self._ctx_thread_scope()
            snap = self._ctx_snapshot()
            if not snap:
                return
            b = self._ctx_build(snap)
            line = b.get("sel_list")
            if not line or self._ctx["fp"].get("sel") == b.get("fp_sel"):
                return
            await self.app.call("thread/realtime/appendText",
                                {"threadId": self.thread_id, "text": line, "role": "developer"}, timeout=15)
            self._ctx["fp"]["sel"] = b.get("fp_sel")
            self.log("ctx_voice_selection", items=len(b.get("fp_sel") or ""), chars=len(line),
                     body=self._log_body(line))
        except Exception as e:   # noqa: BLE001
            self.log("ctx_voice_selection_error", message=clean(e))

    async def _ctx_inject_voice_idle(self, wait: float = 30.0):
        """idle 档：等到用户和助手都没在说、后台也没在跑，再把语音侧状态投进去（不在开口边沿投）。"""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.session_state != "connected" or not (self.app and self.thread_id):
                return
            if not self.user_speaking and not self.assistant_speaking and self._turn is None:
                break
            await asyncio.sleep(0.5)
        else:
            return
        try:
            snap = self._ctx_snapshot()
            if not snap:
                return
            b = self._ctx_build(snap)
            if not b.get("state") or self._ctx["fp"]["voice"] == b["fp_voice"]:
                return
            await self.app.call("thread/realtime/appendText", {"threadId": self.thread_id, "text": b["voice"], "role": "developer"}, timeout=15)
            self._ctx["fp"]["voice"] = b["fp_voice"]
            self.log("ctx_voice", chars=len(b["voice"]), page=self._ctx["page_key"][-40:], mode="idle",
                     body=self._log_body(b["voice"]))
        except Exception as e:
            self.log("ctx_voice_error", message=clean(e))

    def _ctx_invalidate(self, sinks=("backend_state", "backend_text", "voice", "image")):
        for k in sinks:
            self._ctx["fp"][k] = ""

    def _notify_actionable(self) -> list[dict]:
        """路由判出 speak 的 pending 待办。判断不重做，只读结论。

        两份文件都在 LOCALAPPDATA 下的 BWReader 目录：notifications.json 是真值库，
        notification-routing.json 是路由层每轮对账写的结论。
        ⚠ 路由文件陈旧就返回空 —— 板面那边陈旧时是**放行**（宁可多念不可漏掉），
        但这里是**主动开口**，方向相反：宁可不说，不可拿过期结论去打扰他。
        """
        try:
            store = json.loads((BWREADER_DIR / "notifications.json").read_text(encoding="utf-8"))
            routing = json.loads((BWREADER_DIR / "notification-routing.json").read_text(encoding="utf-8"))
        except Exception:   # noqa: BLE001
            return []
        max_age = float(self.settings.get("notifyPushRoutingMaxAgeSeconds") or 300.0)
        if time.time() * 1000 - float(routing.get("atUtcMs") or 0) > max_age * 1000:
            return []
        routes = routing.get("routes") or {}
        out = []
        for item in (store.get("items") or []):
            if item.get("audience") != "user" or item.get("state") != "pending":
                continue
            route = routes.get(str(item.get("id")))
            # speak = 现在可以说；call = 这条必须马上知道（建的时候就定了 deliver=call）。
            # hold / judge **不在这里动**：hold 是路由判了现在别打扰；judge 是它判不了 ——
            # 那种要先跑 judgment_basis 拿全依据再定，是后台的活，不该由这个循环替它拍板。
            if not route or route.get("action") not in ("speak", "call"):
                continue
            item = dict(item)
            item["_action"] = route.get("action")
            out.append(item)
        return out

    def _notify_ack(self, ntf_id: str) -> bool:
        """说过了就 ack —— 不 ack 的话下一轮它还在 pending，会被再说一遍。"""
        try:
            proc = subprocess.run(
                [sys.executable, str(BWREADER_DIR / "replication_notifications.py"), "ack", ntf_id],
                capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return proc.returncode == 0
        except Exception as e:   # noqa: BLE001
            self.log("notify_ack_error", ntf=ntf_id, message=clean(e))
            return False

    async def _notify_push_loop(self):
        """把路由判出 speak 的待办**主动**送出去（2026-09-18 用户拍板）。

        用户原话：「很多定时任务完全可以在到时间的时候把我们的服务拉起，首先去跟文字版本的
        AI 后台聊天，如果需要通知的时候根据我的情况看是打电话给我还是直接说话…还有一些
        非常简单的通知根本不需要后台 AI 参与，只是传达转述，条件判断完以后可以直接交给
        前端的语音 AI」。

        所以分两层：
          语音在线 → 直接 /say 念出来，**零后台轮**（判断路由层早做完了，转述不必花一轮）
          不在线   → 起一轮交后台，它自己决定开语音说、打电话、还是先不打扰
        两层都以 ack 收尾 —— 不 ack 下一轮还在 pending，会被再说一遍。

        ⚠ 不在用户说话/助手说话/后台在跑的时候插话：用户 2026-09-15 明确抱怨过
        「一分钟到了记得喝水」把他的提问打断。等一个空档再说，等不到就下一轮再看。
        """
        while not self.shutting_down:
            await asyncio.sleep(float(self.settings.get("notifyPushIntervalSeconds") or 20.0))
            try:
                if not self.settings.get("notifyPushEnabled", True):
                    continue
                if not (self.app and self.thread_id):
                    continue
                items = self._notify_actionable()
                # 冷却：投过的压住，不管 ack 成没成 —— 见 notifyPushRepeatMinutes
                cool = float(self.settings.get("notifyPushRepeatMinutes") or 30.0) * 60.0
                sent = self._notify_sent
                now = time.time()
                items = [x for x in items
                         if now - sent.get(str(x.get("id") or ""), 0.0) >= cool]
                if not items:
                    continue
                online = self.session_state == "connected"
                if online and (self.user_speaking or self.assistant_speaking or self.backend_busy):
                    continue      # 别插话，下一轮再看
                if not online and self.backend_busy:
                    continue
                item = items[0]   # 一轮只投一条：说完 ack，下一轮自然轮到下一条
                ntf = str(item.get("id") or "")
                said = str(item.get("title") or "").strip()
                if item.get("body"):
                    said += "。" + str(item["body"]).strip()
                # ⚠ 重投必须有上限。原来只有冷却、没有次数：ack 一旦失败（比如后台
                #   沙盒写不了），同一条就每 30 分钟重投一次直到过期 —— 2026-09-20
                #   实录里刷了满屏同样的话。投够几次还没 ack，就停下并记一笔，
                #   让它安静地挂着，而不是一直吵。
                _tries = self._notify_tries.get(ntf, 0) + 1
                self._notify_tries[ntf] = _tries
                _cap = int(self.settings.get("notifyPushMaxAttempts") or 3)
                if _tries > _cap:
                    self.log("notify_push_capped", ntf=ntf, tries=_tries, cap=_cap)
                    continue
                self._notify_sent[ntf] = now   # 先记再投：投递中途出错也不该立刻重来
                if str(item.get("_action") or "speak") == "call":
                    # 打电话这一档**永远交后台**，不自己拨：拨号是阻塞的、还要处理拒接降级，
                    # 而且"要不要真的响铃"该由手上有全部工具和上下文的那一侧拍板。
                    await self.turn(
                        "【有一条要打电话通知他的事】" + said + chr(10)
                        + "路由层判定这条是 deliver=call（建的时候就定了必须马上知道）。"
                          "用 voice_call 打给他，接通后把上面这句说清楚；拒接或没接通时按"
                          "通知系统的降级规则处理，不要反复重拨。"
                          "送到之后调 notify_ack(id=\"" + ntf + "\") 登记掉。",
                        record_user=False)
                    self.log("notify_push", ntf=ntf, via="call-turn", text=said[:120])
                    continue
                if online:
                    res = await self.say(said)
                    if not res.get("spoken"):
                        self.log("notify_push_failed", ntf=ntf, reason=res.get("reason"))
                        continue
                    acked = self._notify_ack(ntf)
                    self.log("notify_push", ntf=ntf, via="say", acked=acked, text=said[:120])
                else:
                    await self.turn(
                        "【有一条该跟他说的事，语音会话不在线】" + said + chr(10)
                        + "路由层已经判定现在可以说（按他的位置、设备活跃、语音状态）。"
                          "你来决定怎么送到：开语音说（voice_session_start + voice_say）、"
                          "打电话（voice_call，只用于必须马上知道的事），还是先不打扰。"
                          "处理完之后调 notify_ack(id=\"" + ntf + "\") 登记掉 ——"
                          "「先不打扰」也算处理完了，同样要调，否则它还会回来吵。",
                        record_user=False)
                    self.log("notify_push", ntf=ntf, via="turn", text=said[:120])
            except Exception as e:   # noqa: BLE001
                self.log("notify_push_error", message=clean(e))

    async def _ink_late_loop(self):
        """图比问题晚到时，补插进**正在跑的那一轮**（2026-09-17 实录）。

        用户圈完「新型」立刻问「这是什么」：提问在 13:46:11，而那张图的落盘时刻是
        13:46:12 —— 晚了一秒，那一轮就只拿到页码和整页文字，答得很泛；他再问一次
        才看到图。笔迹要先稳定、再做一次设备往返，本来就比说话慢。
        与其让用户等，不如图一到就补插：那一轮还在跑，steer 正是为此存在的。
        """
        while not self.shutting_down:
            await asyncio.sleep(0.5)
            try:
                if not (self.backend_busy and self._turn and self._turn.get("id")):
                    continue
                snap = self._ctx_snapshot()
                if not snap:
                    continue
                pending = self._ink_standby_pending(snap)
                if not pending:
                    continue
                lines = []
                for i, x in enumerate(pending, 1):
                    who = ("整页笔迹" if x["kind"] == "ink"
                           else "选区 %s" % (x.get("ordinal") if x.get("ordinal") is not None else "?"))
                    lines.append("附图 %d = %s：%s" % (i, who, self._ink_age_words(x.get("age"))))
                res = await self.steer_running_turn(
                    "【当前阅读状态·补充】他刚画的图这会儿才取到，随这条补上（"
                    + "；".join(lines) + "）。如果你手上的活跟「这个/这里/圈的」有关，以图为准。",
                    tag="ink-late", image_paths=[x["path"] for x in pending])
                if res.get("ok"):
                    for x in pending:
                        self._ink_sent.add(x["name"])
                    self.log("ctx_ink_late", images=[x["name"] for x in pending],
                             turnId=str((self._turn or {}).get("id") or "")[-12:])
            except Exception as e:   # noqa: BLE001
                self.log("ctx_ink_late_error", message=clean(e))

    async def _ctx_loop(self):
        """每 1 秒看快照；状态变了**只记最新状态，不写线程**（2026-09-16 用户拍板）。

        以前是"变了就投"：静默翻五页、改三次选中，线程里多八条，其中七条没人用到，
        却要被之后每一轮重读（inject_items 只能追加）。现在写入只发生在有人要用的时候 ——
        开口、打字、后台起轮 —— 那时投的一定是当下最新的，一轮只有一条。
        """
        last_fp = ""
        while not self.shutting_down:
            await asyncio.sleep(1)
            try:
                if not self.settings.get("contextInjectEnabled", True) or not (self.app and self.thread_id):
                    continue
                snap = self._ctx_snapshot()
                if not snap:
                    continue
                b = self._ctx_build(snap)
                if not b.get("state") or b["fp_state"] == last_fp:
                    continue
                last_fp = b["fp_state"]
                if self._ctx["debounce"]:
                    self._ctx["debounce"].cancel()

                async def _later():
                    await asyncio.sleep(3.0)   # 翻页连按时别抖：7 秒 4 条（2026-09-14 实录）
                    # ⚠ 这里**不再** _ctx_inject_backend：没人要的状态不进线程。
                    # 指纹已经在上面更新，下一次真要用时（开口/打字/起轮）投的就是最新的。
                    if str(self.settings.get("contextVoiceMode") or "off") == "idle":
                        await self._ctx_inject_voice_idle()
                self._ctx["debounce"] = asyncio.create_task(_later())
            except Exception as e:
                self.log("ctx_loop_error", message=clean(e))

    def transcript(self, limit: int = 12, since_seconds: float = 0.0) -> dict:
        """最近这几句语音对话，**带时刻**。

        2026-09-18 用户提的分工：「语音 AI 应该只负责播报……后台根据任务内容使用语音 AI
        进行语音输出，在语音 AI 返回结果或者超时后用该工具检查我和 AI 的对话，
        判断是否完成，进而决定下一步做什么」。

        ⚠ 必须带时刻。原来 voice_status 里塞了个 recentTranscripts（最后 4 句、无时刻），
        回答不了唯一要紧的那个问题：**他这句是在我让它念之前说的，还是之后**。
        没有时刻就没法判"念到了没有""他回应了没有"，只能猜。
        """
        limit = max(1, min(int(limit or 12), 50))
        rows = list(self.transcripts)[-limit:]
        if since_seconds and since_seconds > 0:
            cut = time.time() - float(since_seconds)
            rows = [x for x in rows if x[0] >= cut]
        now = time.time()
        return {
            "ok": True,
            "sessionState": self.session_state,
            "userSpeaking": bool(self.user_speaking),
            "assistantSpeaking": bool(self.assistant_speaking),
            "backendBusy": bool(self.backend_busy),
            "items": [{"secondsAgo": round(now - at, 1),
                       "at": time.strftime("%H:%M:%S", time.localtime(at)),
                       "role": r, "text": str(x or "")[:600]} for at, r, x in rows],
        }

    #: 委派闸的兜底时长。正常情况由 _finish_turn 清零；万一那一轮的 turn/completed
    #: 没来（app-server 掉线等），也不能把 voice_say 永久封死。
    DELEGATION_SAY_BLOCK_SECONDS = 180.0

    def _delegation_owns_speech(self) -> bool:
        """这一轮的答案是不是已经会被语音模型念出来。

        语音委派的那一轮，后台写的文字由 app-server 直接送进语音模型（runner 不经手，
        全文件只有 say() 会调 appendSpeech）。所以后台**再** voice_say 一次，
        用户听到的就是同一个问题被答两遍。
        """
        at = getattr(self, "_delegation_open_at", 0.0)
        if not at or self._turn is None:
            return False
        return (time.time() - at) < self.DELEGATION_SAY_BLOCK_SECONDS

    async def say(self, text: str, fallback: str = "none", source: str = ""):
        """让语音模型立刻念一句。

        ⚠ 2026-09-18 修的静默失败：这里原来不看会话状态，直接 appendSpeech 就回
        {"ok": true}。而 appendSpeech 是 fire-and-forget —— 语音会话不在线时
        app-server 回的是一条**异步** error 通知（`conversation is not running`），
        响应本身照旧成功。实测（会话 state=idle）：/say 回 ok:true、日志记了 say，
        而一个字都没有被念出来。定时任务的 deliver mode=say 因此会记下
        delivered:true，投递报告全绿而用户什么也没听见。

        现在：不在线就**不假装成功**。fallback="turn" 时改为起一轮，把这句话交给后台
        —— 它手上有 voice_session_start / voice_say / voice_call，能决定是开语音说、
        打电话，还是等他下次上线。
        """
        # ⚠ 一问三答的那道闸（2026-09-21 实录）：用户问「低中档是不是比 5.6 好」，
        #   听到的是「我查一下」→「通常思考档位越高…」→「更准确地说…」三句。
        #   事件日志里同一轮 01a0c238 出了两条 agentMessage + 一次 voice_say，
        #   而委派轮的 agentMessage 由 app-server 直接交给语音模型念 —— 于是
        #   voice_say 念的是第三遍。后台这一轮本来就有人替它开口，不必自己再喊。
        #   只挡后台（source=backend）：定时投递、通知那些路照旧，它们没人替它们念。
        if source == "backend" and self._delegation_owns_speech():
            self.log("say_blocked", why="delegation-owns-speech", text=text[:120])
            return {"ok": False, "spoken": False, "reason": "delegation-owns-speech",
                    "msg": "这一轮是用户语音委派来的，你写在回答里的话会自动念给他，"
                           "不要再 voice_say（那会让同一个问题被念两遍）。"
                           "把要说的写进本轮回答，**整轮只写一条**。"
                           "voice_say 只用于没人问你的时候：通知、定时提醒、你主动找他。"}
        if self.session_state != "connected":
            if fallback == "turn":
                self.log("say_fallback_turn", text=text[:120], reason="voice-offline")
                await self.turn(
                    "【这句话本来要直接念给他，但语音会话不在线】" + text + chr(10)
                    + "你来决定怎么送到：现在开语音说（voice_session_start + voice_say）、"
                      "打电话（voice_call，只用于必须马上知道的事），还是先不打扰、"
                      "等他下次上线。别只回复文字就算完。",
                    record_user=False)
                return {"ok": True, "spoken": False, "via": "turn-fallback"}
            self.log("say_skipped", text=text[:120], reason="voice-offline",
                     sessionState=self.session_state)
            return {"ok": False, "spoken": False, "reason": "voice-offline"}
        self.mark_activity("say")
        before = getattr(self, "_last_app_error", (0.0, ""))
        await self.app.call("thread/realtime/appendSpeech", {"threadId": self.thread_id, "text": text})
        # 等一下那条异步 error：会话刚在这一瞬断掉时，响应仍是成功的（见上面的说明）。
        await asyncio.sleep(0.2)
        after = getattr(self, "_last_app_error", (0.0, ""))
        if after[0] > before[0] and ("not running" in after[1] or "realtime" in after[1]):
            self.log("say_failed", text=text[:120], detail=after[1][:200])
            return {"ok": False, "spoken": False, "reason": "realtime-error"}
        self.pending_speech_until = time.monotonic() + 8
        self.log("say", text=text[:200])
        return {"ok": True, "spoken": True}

    async def wait_for_speech(self, grace: float = 10.0) -> bool:
        """等语音模型把嘴里的话说完：有待念的句子要等它开口并 turn.done；正在说就等说完。返回是否等到。"""
        deadline = time.monotonic() + grace
        started_at = time.monotonic()
        while time.monotonic() < deadline:
            speaking = self.assistant_speaking
            pending = self.pending_speech_until > time.monotonic() and self.last_assistant_done < started_at
            if not speaking and not pending:
                return True
            await asyncio.sleep(0.2)
        return False

    async def call_user(self, text: str, title: str = "", ntf: str = "misc", reason: str = "") -> dict:
        """给用户的 iPad 打一通电话（voip_push.py call，阻塞到有结果），接通后等 App 把会话建起来，把 text 念出来。
        已经在 App 通话中就直接念。outcome：answered / downgraded（拒接或没人接）/ blocked（没拨）/ failed。"""
        text = str(text or "").strip()
        title = (str(title or "").strip() or text[:40] or "提醒")
        ntf = str(ntf or "misc").strip() or "misc"
        if self.session_state == "connected" and self.session_profile == "app":
            # 用户 2026-09-15「你现在再给我打」：在通话中要求打电话 = 先挂断这通，再真的拨过去。
            # App 收到"语音核心结束通话"会当正常挂断（不再自动续接）；旧版 App 会 2 秒内重拨 → 那就直接在通话里念。
            self.log("call_user_redial", ntf=ntf)
            await self.session_stop("call-user-redial")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and self.session_state != "idle":
                await asyncio.sleep(0.2)
            await asyncio.sleep(2.5)   # 给旧版 App 自动重拨的窗口
            if self.session_state == "connected":
                if text:
                    await self.say(text)
                self.log("call_user", outcome="already-connected", ntf=ntf, note="App 挂断后又自动重连了")
                return {"ok": True, "outcome": "already-connected", "spoken": bool(text)}
        script = Path(__file__).resolve().parent / "voip_push.py"
        if not script.exists():
            script = BWREADER_DIR / "voip_push.py"
        try:
            (BRIDGE_RUNTIME / "voip-hangup.json").unlink(missing_ok=True)   # 残留的挂断请求会把这一通刚接起就挂掉（桥消费一次即删）
        except Exception:
            pass
        argv = [PYTHON_EXE, str(script), "call", "--ntf", ntf, "--title", title[:80]]
        if reason:
            argv += ["--reason", str(reason)[:200]]
        self.log("call_user_dial", ntf=ntf, title=title[:80])

        def dial():
            return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=200,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc = await asyncio.get_running_loop().run_in_executor(None, dial)
        except Exception as e:   # noqa: BLE001
            self.log("call_user_error", message=clean(e))
            return {"ok": False, "outcome": "failed", "error": clean(e)}
        result = {}
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except ValueError:
                    continue
        outcome = str(result.get("outcome") or ("failed" if proc.returncode else "unknown"))
        self.log("call_user", outcome=outcome, exit=proc.returncode, attempts=result.get("attempts"), error=(result.get("error") or (proc.stderr or "")[-200:] or None))
        spoken = False
        if outcome == "answered":
            self._voip_call_active = True
        if outcome == "answered" and text:
            deadline = time.monotonic() + 30   # 接听 → App START → 桥 → /session/start，通常 3–8 秒
            while time.monotonic() < deadline and self.session_state != "connected":
                await asyncio.sleep(0.3)
            if self.session_state == "connected":
                await asyncio.sleep(0.8)   # 让对端音频先通
                await self.say(text)
                spoken = True
            else:
                self.log("call_user_no_session", waited=30)
        return {"ok": outcome in ("answered", "already-connected"), "outcome": outcome, "spoken": spoken,
                "attempts": result.get("attempts"), "error": result.get("error")}

    async def tell(self, text: str, role: str = "developer"):
        await self.app.call("thread/realtime/appendText", {"threadId": self.thread_id, "text": text, "role": role})
        self.log("tell", role=role, text=text[:200], userSpeaking=self.user_speaking)
        return {"ok": True, "userSpeaking": self.user_speaking}

    async def typed(self, text: str, attachment_ids: list[str] | None = None, submission_id: str | None = None) -> dict:
        """侧栏输入框打的字（桥 codex-type → 这里）。**不管在不在通话，一律直接交给后台**，
        以用户本人的身份、原话、不加任何标签。

        ⚠ 2026-09-23 用户实录，改掉了原来「通话中追加进语音会话」那条：
          ① 追加时带着「【用户打字】」前缀，语音模型把前缀连同原话**念了出来**，
             还顺手把上一轮的旧请求又委派了一遍；
          ② 那条路径从不写侧栏记录，于是打的字在侧栏里消失了。
          用户原话：「通话中打字时直接把信息传给后台ai就好」。
        后台正在跑一轮时插进那一轮（turn/steer），否则起新的一轮；两种都会落侧栏。"""
        self.mark_activity("typed")
        text = (text or "").strip()
        if not text and not attachment_ids:
            return {"ok": False, "reason": "empty"}
        image_paths = []
        submission = None
        if attachment_ids:
            # Validate all data and connection readiness before recording dispatch intent.
            original_text = text
            text, image_paths = typed_attachment_input(text, attachment_ids)
            await self.ensure_app()
            submission_path, fingerprint, prior = claim_typed_submission(submission_id, original_text, attachment_ids)
            if prior is not None:
                return prior
            submission = (submission_path, fingerprint)
        if self.session_state == "connected":
            # 打字和说话是同一件事的两种输入方式：开口时会在边沿刷新状态，打字也必须刷。
            # 不刷的后果是拿上一轮的旧状态回答"我现在选中了什么"（2026-09-15 实录）。
            await self._ctx_on_speech()
        if self.backend_busy and self._turn and self._turn.get("id"):
            res = await self.steer_running_turn(text, tag="typed", **({"image_paths": image_paths} if image_paths else {}))
            if res.get("ok"):
                tid = str(self._turn.get("id"))
                self._history_post({"user": text, "via": "codex-voice",
                                    "turn_id": tid + ".t" + str(int(time.time() * 1000))[-6:]})
                self._segment_backend_turn("typed")
                self.log("typed", via="steer", text=text[:200])
                result = {"ok": True, "via": "backend"}
                if submission:
                    finish_typed_submission(*submission, result)
                return result
            if submission:
                # An interrupted/timeout reply is not proof the steer was rejected.
                # Keep the original request id; never start another turn blindly.
                return {"ok": False, "reason": "outcome-unknown"}
            # 那一轮恰好刚结束：退回起新的一轮
        await self.turn(text, **({"image_paths": image_paths} if image_paths else {}))
        self.log("typed", via="backend", text=text[:200])
        result = {"ok": True, "via": "backend"}
        if submission:
            finish_typed_submission(*submission, result)
        return result

    async def turn(self, text: str, additional: dict | None = None, record_user: bool = True,
                   image_paths: list[str] | None = None):
        await self.ensure_app()
        await self._ctx_inject_backend(with_text=True)   # 直接少一轮工具调用：起轮前把他正看着的内容放进去
        self._pending_turn_user = text if record_user else None
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
        params["input"].extend({"type": "localImage", "path": path} for path in (image_paths or []))
        if additional:
            params["additionalContext"] = {k: {"kind": "application", "value": str(v)} for k, v in additional.items()}
        await self.app.call("turn/start", params, timeout=30)
        self.log("turn_start", text=text[:200])
        return {"ok": True}

    async def inject(self, text: str, role: str = "developer"):
        await self.ensure_app()
        ctype = "output_text" if role == "assistant" else "input_text"
        await self.app.call("thread/inject_items", {"threadId": self.thread_id, "items": [
            {"type": "message", "role": role, "content": [{"type": ctype, "text": text}]}]}, timeout=30)
        self.log("inject", role=role, text=text[:200])
        return {"ok": True}

    def dc_send(self, obj: dict):
        if not self.dc or self.dc.readyState != "open":
            raise RuntimeError("数据通道未打开")
        self.dc.send(json.dumps(obj))
        self.log("dc_sent", type=obj.get("type"))
        return {"ok": True}

    def quota_sample_row(self, tag: str) -> dict:
        """一行快照：会话状态 + 实时音频累计 + 周额度 + 桥的采集标志。
        rateLimits 用缓存值（notify 会推、quota_watch_loop 每 10 分钟刷一次），不额外往上游打请求。"""
        rl = (self.usage.get("rateLimits") or {}) or {}
        pri = (rl.get("primary") or {}) if isinstance(rl, dict) else {}
        tk = ((self.usage.get("tokens") or {}).get("total") or {})
        cap = None
        bridge_state = None
        try:
            st = json.loads((BRIDGE_RUNTIME / "computer-voice-direct.status.json").read_text(encoding="utf-8"))
            age = time.time() - calendar.timegm(time.strptime(st["updatedAtUtc"][:19], "%Y-%m-%dT%H:%M:%S"))
            if age < 120:
                cap = bool(st.get("captureActive"))
                bridge_state = st.get("state")
        except Exception:
            pass
        return {"t": round(time.time(), 3), "iso": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag,
                "state": self.session_state, "profile": self.session_profile,
                "sessionNo": self.session_no,
                "sessionSec": (round(time.time() - self.session_started_at) if self.session_started_at else 0),
                "audioMs": self.usage.get("audioDurationMs"),
                "weeklyPercent": pri.get("usedPercent"), "weeklyResetsAt": pri.get("resetsAt"),
                "totalTokens": tk.get("totalTokens"), "outputTokens": tk.get("outputTokens"),
                "bridgeCaptureActive": cap, "bridgeState": bridge_state}

    def quota_sample(self, tag: str):
        try:
            row = self.quota_sample_row(tag)
            with QUOTA_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:   # noqa: BLE001
            self.log("quota_sample_error", message=clean(e))

    async def quota_watch_loop(self):
        """每 2 分钟采一行（连着也好、空闲也好都采：空闲那些行就是基线），每 10 分钟把周额度刷新一次。"""
        n = 0
        while not self.shutting_down:
            await asyncio.sleep(120)
            n += 1
            try:
                if self.app is not None and n % 5 == 1:
                    rl = await self.app.call("account/rateLimits/read", {}, timeout=20)
                    self.usage["rateLimits"] = rl.get("rateLimits") or rl
            except Exception as e:   # noqa: BLE001
                self.log("quota_refresh_error", message=clean(e))
            self.quota_sample("tick")

    async def quota(self):
        await self.ensure_app()
        out = {"audioDurationMs": self.usage.get("audioDurationMs"), "backendModelUsage": self.usage.get("backendModelUsage"),
               "tokens": self.usage.get("tokens")}
        try:
            rl = await self.app.call("account/rateLimits/read", {}, timeout=20)
            out["rateLimits"] = rl.get("rateLimits") or rl
            self.usage["rateLimits"] = out["rateLimits"]
        except Exception as e:
            out["rateLimitsError"] = clean(e)
        try:
            us = await self.app.call("account/usage/read", {}, timeout=20)
            out["usageSummary"] = us.get("summary")
            buckets = us.get("dailyUsageBuckets") or []
            out["today"] = buckets[-1] if buckets else None
        except Exception as e:
            out["usageError"] = clean(e)
        self.usage["updatedAt"] = time.time()
        return out

    async def catalog(self):
        await self.ensure_app()
        voices = await self.app.call("thread/realtime/listVoices", {}, timeout=15)
        models = await self.app.call("model/list", {}, timeout=30)
        return {"voices": voices.get("voices"),
                "models": [{"id": m["id"], "displayName": m.get("displayName"), "defaultEffort": m.get("defaultReasoningEffort"),
                            "efforts": [e["reasoningEffort"] for e in (m.get("supportedReasoningEfforts") or [])],
                            "serviceTiers": [t["id"] for t in (m.get("serviceTiers") or [])]} for m in models.get("data", [])]}

    # ---------- 助手历史（侧栏） ----------
    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"[^0-9A-Za-z぀-ヿ㐀-鿿가-힯]+", "", str(text or "")).lower()

    def _backend_speaking_likely(self) -> bool:
        """后台轮进行中，或后台刚出了回复（60 s 内）：语音模型这时说的话大概率是在念它。"""
        if self._turn is not None:
            return True
        return bool(self._backend_recent and time.time() - self._backend_recent[0] < 60)

    def _spoken_dup(self, spoken: str) -> bool:
        """语音字幕是不是后台最近那条回复的复述（标点/空格差异忽略；子串或相似度 ≥0.8 算重复）。"""
        if not self._backend_recent or time.time() - self._backend_recent[0] > 120:
            return False
        a, b = self._norm_text(spoken), self._norm_text(self._backend_recent[1])
        if not a or not b:
            return False
        if len(a) >= 12 and (a in b or b in a):
            return True
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8

    @staticmethod
    def _clean_user_text(text: str) -> str:
        """委托轮的用户句是 <realtime_delegation><input>…</input>…</realtime_delegation>，侧栏只要里面那句。"""
        text = str(text or "")
        if "<realtime_delegation>" in text:
            m = re.search(r"<input>(.*?)</input>", text, re.S)
            text = m.group(1) if m else re.sub(r"<[^>]+>", "", text)
        return text.strip()

    def _tool_opened(self, item: dict):
        """Open the task container on its first tool and update every call by source ID.

        Starting/completing a tool updates the same part; repeated tool names remain
        separate calls. Generated artifact parts continue to be owned by the App.
        """
        rec = self._turn
        if rec is None:
            return
        call_id = self._item_identity(item)
        if call_id in rec.setdefault("completed_items", set()):
            return
        t = item.get("type")
        tool = str(item.get("tool") or item.get("name") or t)
        server = item.get("server")
        label = (str(server) + "." if server else "") + tool
        if tool in ("voice_say", "voice_tell"):
            return  # Successful speech is already text; a failure is published on completion.
        first = not rec.get("tool_opened")
        rec["tool_opened"] = True
        if first:
            self.log("turn_card_opened", tool=label[:160], turnId=rec.get("id"))
        # 委派前那句「好的，我看一下」已经落成独立记录。它就是这次任务的开场白，
        # 把它搬进本轮容器并删掉原记录，侧栏才是用户要的「一个任务一个框」。
        pre = self._pre_turn_voice if first else None
        if first:
            self._pre_turn_voice = None
        if pre and (time.time() - pre[2]) < 30 and pre[2] >= self._last_user_at():
            self._voice_post(rec.get("seg") or rec["id"], pre[1], absorb=pre[0], item_id=pre[0], final=True)
            self.log("pre_turn_voice_absorbed", turnId=rec.get("id"), frm=pre[0])
        part = {"kind": "tool", "tool": label[:160], "label": label[:320],
                "origin": "runner", "id": call_id, "call_id": call_id, "status": "running"}
        self._put_runner_part(part)
        self._publish_runner_parts(changed_parts=[part])

    @staticmethod
    def _item_identity(item):
        return str(item.get("id") or ("legacy:" + str(item.get("type") or "item") + ":" +
                                      str(item.get("tool") or item.get("name") or "message")))[:160]

    def _put_runner_part(self, part):
        parts = self._turn.setdefault("parts", [])
        for index, old in enumerate(parts):
            if old.get("id") == part["id"]:
                parts[index] = part
                return
        if len(parts) < 96:
            parts.append(part)

    def _publish_runner_parts(self, *, item_id=None, final=False, changed_parts=None):
        rec = self._turn
        parts = changed_parts if changed_parts is not None else rec.get("parts", [])
        # 每个部件回它第一次落库的那一段（见 _segment_backend_turn）。
        groups = {}
        for part in parts:
            groups.setdefault(self._part_segment(rec, part.get("id")), []).append(part)
        if not groups:
            groups[rec.get("seg") or rec["id"]] = []
        for seg, seg_parts in groups.items():
            body = {"parts": seg_parts,
                    "via": "codex-voice", "origin": "runner",
                    "turn_id": seg, "item_id": item_id or ("parts:" + seg),
                    "role": "assistant", "upsert_only": 1, "create_if_missing": 1,
                    "notify_sidebar": 1}
            if final:
                body["stream_final"] = 1
            self._history_post(body)

    # ── 后台轮的「显示分段」 ──────────────────────────────────────────────
    # 用户 2026-09-23：「侧边栏的对话显示顺序有问题，ai 的对话全都积累到了同一个地方」。
    # 后台一轮可以跑很久。这期间用户又说了几句、语音模型也答了几句 —— 旧规则把这些语音
    # 回复**一律**并进那条后台轮容器，而容器是轮次开始时建的，于是它们全堆在那一格里、
    # 排在用户后来那几句**上面**（实录：「我当然知道啦」「嗯，我看一下」都跑进了更早的
    # 「好，我会把介绍卡…」那一格）。
    # 现在：后台轮在跑时用户每说/打一句（落库之后），这一轮就切一个新段 `<轮id>:sN`；
    # 之后的语音回复与后台输出都写进新段。新段在用户那句**之后**才落库，排序自然对。
    # 一个部件第一次落在哪段，以后的更新就一直回那段 —— 工具卡跑到一半切段，不会分身两处。
    # 「一个任务一个框」仍然成立，只是按用户的话切成了几格。
    def _backend_display_id(self):
        rec = self._turn
        if rec is None:
            return None
        return rec.get("seg") or rec["id"]

    def _part_segment(self, rec, part_id):
        return rec.setdefault("part_seg", {}).setdefault(str(part_id), rec.get("seg") or rec["id"])

    def _segment_backend_turn(self, reason):
        rec = self._turn
        if rec is None:
            return None
        old = rec.get("seg") or rec["id"]
        n = int(rec.get("seg_n") or 1) + 1
        rec["seg_n"] = n
        new = str(rec["id"])[:100] + ":s" + str(n)
        rec["seg"] = new
        # 侧栏据此把「当前容器」换到新段：App 之后画的部件也落进新段。
        self._stream_start_post(new)
        # 正在流的那句语音回复往往比用户那句落库早几毫秒开始（实录 seq8366 早于 8367），
        # 归属在那一刻已经定在旧段 —— 它回答的正是用户刚说完这句，搬进新段。
        state = getattr(self, "_transcript_streams", None)
        for entry in (list(state.entries.values()) if state else []):
            if entry["role"] == "assistant" and not entry["final"] and entry.get("owner") == old:
                self._stream_post(old, "", item_id=entry["id"])
                entry["owner"] = new
                if entry.get("text"):
                    self._stream_post(new, entry["text"], role="assistant", item_id=entry["id"])
        if getattr(self, "_voice_stream_owner", None) == old:
            # 旧 stdio 路径：容器级草稿里还挂着这句的半截，重发旧段草稿时把它去掉。
            parts = (getattr(self, "_voice_parts", None) or {}).get(old) or []
            self._stream_post(old, (chr(10) + chr(10)).join(parts))
            self._voice_stream_owner = new
        self.log("backend_segment", turnId=str(rec["id"])[-12:], seg=n, reason=reason)
        return new

    def _last_user_at(self) -> float:
        """用户最后一次说话的时刻。

        ⚠ 收编开场白只看"30 秒内"是不够的：上一轮对话的回答（「嗯，听得到。」）也在
          30 秒内，收进来就成了把别人的话塞进这次任务。开场白的定义是**说在用户这次
          请求之后**的那句 —— 用户一开口，之前的话就都不再属于接下来这次任务。
        """
        try:
            return max((at for at, role, _ in self.transcripts if role == "user"),
                       default=0.0)
        except Exception:
            return 0.0

    def _turn_item(self, item: dict):
        rec = self._turn
        t = item.get("type")
        item_id = self._item_identity(item)
        completed = rec.setdefault("completed_items", set())
        if item_id in completed:
            return
        completed.add(item_id)
        if t == "userMessage":
            if not rec.get("user") and not rec.get("user_posted"):
                txt = " ".join(str(c.get("text") or "") for c in (item.get("content") or []) if isinstance(c, dict))
                rec["user"] = self._clean_user_text(txt) or None
        elif t == "agentMessage":
            txt = item.get("text") or ""
            if txt and item.get("phase") in (None, "final_answer"):
                rec["assistant"] = txt
                self._backend_recent = (time.time(), txt)
            if txt and not self._voice_owns_text():
                self._stream_post(self._part_segment(rec, item_id), txt, item_id=item_id)
                part = {"kind": "text", "text": txt[:32000], "origin": "runner",
                        "id": item_id, "item_id": item_id}
                self._put_runner_part(part)
                self._publish_runner_parts(item_id=item_id, final=True, changed_parts=[part])
        elif t in ("mcpToolCall", "webSearch", "commandExecution", "fileChange", "dynamicToolCall", "collabAgentToolCall"):
            tool = str(item.get("tool") or item.get("name") or t)
            server = item.get("server")
            label = (str(server) + "." if server else "") + tool
            status = str(item.get("status") or "")
            # 参数：侧栏工具卡的「AI 请求」栏要它；字符串形式的 JSON 先解开
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"raw": args[:1000]}
            # 结果：MCP 结果是 {content:[{type:text,text:"<JSON 字符串>"}], structuredContent?}；
            # 桥序列化时把中文转成了 \uXXXX 转义，这里解开再重排成可读 JSON（2026-09-14 用户截图：一坨转义）
            brief = ""
            res = item.get("result")
            if t == "mcpToolCall" and isinstance(res, dict):
                sc = res.get("structuredContent")
                if sc:
                    brief = json.dumps(sc, ensure_ascii=False)
                else:
                    joined = "\n".join(str(c.get("text") or "") for c in (res.get("content") or []) if isinstance(c, dict) and c.get("type") == "text")
                    try:
                        brief = json.dumps(json.loads(joined), ensure_ascii=False)
                    except ValueError:
                        brief = joined
            elif t != "mcpToolCall":
                brief = item.get("aggregatedOutput") or item.get("command") or item.get("query") or ""
                if not isinstance(brief, str):
                    brief = json.dumps(brief, ensure_ascii=False)
            err = item.get("error")
            if isinstance(err, dict) and err.get("message"):
                brief = "错误：" + str(err["message"]) + ("\n" + brief if brief else "")
            structured = res.get("structuredContent") if isinstance(res, dict) else None
            failed = (status in ("failed", "error") or bool(err)
                      or (isinstance(res, dict) and bool(res.get("isError")))
                      or (isinstance(structured, dict) and structured.get("ok") is False)
                      or bool(re.match(r'\s*\{\s*"ok"\s*:\s*false', brief or "")))
            part = {"kind": "tool", "tool": label[:160], "label": label[:320],
                    "origin": "runner", "id": item_id, "call_id": item_id,
                    "status": "failed" if failed else status or "completed"}
            if isinstance(args, dict) and args:
                aj = json.dumps(args, ensure_ascii=False)
                part["args"] = args if len(aj) <= 2000 else {"_truncated": aj[:2000]}
            if brief:
                part["result"] = brief[:2000]
            ms = item.get("durationMs")
            if isinstance(ms, (int, float)) and not isinstance(ms, bool) and 0 <= ms <= 86_400_000:
                part["ms"] = int(ms)
            # ⚠ 2026-09-17 用户：「app 侧边栏把文字模型让语音模型说话也算做了工具调用显示出来」。
            #   voice_say / voice_tell 不是"干活"，就是后台在说话 —— 那句话本身已经作为
            #   助手发言显示在侧栏里了，再挂一条「voice_core.voice_say · completed」是重复。
            #   但**失败时必须留着**：上次 voice_say 因为输出设备打不开而失败，
            #   要是顺手一起藏掉，就成了又一处静默失败。
            speech_only = tool in ("voice_say", "voice_tell") and not failed and status in ("completed", "", "ok")
            if not speech_only:
                self._put_runner_part(part)
                self._publish_runner_parts(changed_parts=[part])
            else:
                rec["parts"] = [p for p in rec.get("parts", []) if p.get("id") != item_id]
            # 结果卡不再由运行器代造（2026-09-15 根治）：App 自己画的部件直接 upsert 进同一条记录。
            if failed:
                self._tool_error_log(rec.get("id"), label, args, brief, "failed")

    def _finish_turn(self, turn: dict):
        rec, self._turn = self._turn, None
        self._backend_done_at = time.time()
        # 这一轮的答案已经由语音模型念完了，voice_say 的闸随之放开（见 say()）。
        self._delegation_open_at = 0.0
        if rec:
            # 收尾那句语音几乎总是落在轮外，要能认领回去（见 _subtitle_done 的 owner 判定）
            self._last_backend_turn_id = rec.get("seg") or rec["id"]
        if not rec:
            return
        user = None if rec.get("user_posted") else rec.get("user")
        assistant = rec.get("assistant")
        if any(p.get("kind") == "text" for p in rec["parts"]):
            assistant = None   # Item-addressed text is already represented in parts.
        if self._voice_owns_text():
            # Voice subtitles own the text. Still publish a terminal task event when empty.
            user, assistant = None, None
            if not rec["parts"] and not rec.get("absorb"):
                self.log("history_skip_backend_text", turnId=rec["id"][:40])
        seg = rec.get("seg") or rec["id"][:40]
        body = {"user": user or "", "assistant": assistant or "", "via": "codex-voice", "turn_id": seg}
        # Every identified part was persisted as it changed. Re-sending the whole
        # task here grows quadratically and can exceed the endpoint's payload limit.
        # 这一轮期间那几条零散的语音记录：正文已并进上面的 parts，这里告诉服务端把它们删掉，
        # 侧栏重载后就是一个完整容器（见 _subtitle_done 里那段说明）。
        if rec.get("absorb"):
            body["absorb"] = rec["absorb"][:24]
        # 告诉服务端这一轮收尾了：它据此停止把 App 的临时 id 并进本轮
        # （见 assistant.py 的 _LIVE_TURN —— 没有这条就只能靠超时，那期间
        #  App 任何一次画部件都会被错并到已经结束的轮次里）。
        body["turn_end"] = 1
        body["stream_final"] = 1
        body["item_id"] = "turn:" + seg
        # 这一轮**一个工具都没调过** → 侧栏那边没有任何 App 自己画出来的东西，
        # 只有正文；而正文的写入走 upsert，服务端按规矩不发事件（发了会在工具执行
        # 途中打断投递，见 assistant.py）。于是这一轮的内容要等下一次非 upsert 的
        # 写入（通常是用户的下一句）才被顺带刷出来 —— 用户 2026-09-19 实测：
        # 「在没有调用工具时，下一轮开始后才会显示上一轮内容」。
        # 所以只为这种轮次点名要一次重载：它没有在途的工具投递可打断。
        if not rec.get("tool_opened"):
            body["notify_sidebar"] = 1
        dur = turn.get("durationMs")
        if isinstance(dur, (int, float)) and not isinstance(dur, bool) and 0 <= dur <= 86_400_000:
            body["took_ms"] = int(dur)
        self._history_post(body)

    def _subtitle_mode(self) -> bool:
        return str(self.settings.get("historyMode") or "subtitle") != "turns"

    def _voice_owns_text(self) -> bool:
        """字幕模式且语音在线：对话文字以字幕为准，后台轮不写正文、不流式正文。"""
        return self._subtitle_mode() and self.session_state == "connected"

    def _matches_backend_turn(self, params):
        rec = self._turn
        return rec is not None and (not params.get("turnId") or params["turnId"] == rec["id"])

    @staticmethod
    def _transcript_source(params):
        return params.get("realtimeTurnId") or params.get("turnId") or params.get("turn_id")

    def _transcript_state(self):
        session = (getattr(self, "thread_id", None), getattr(self, "session_id", None))
        state = getattr(self, "_transcript_streams", None)
        if state is None or state.session != session:
            state = self._transcript_streams = VoiceTranscriptStreams(session)
        return state

    def _transcript_publish(self, entry):
        if not entry:
            return
        if entry["role"] == "user":
            self._voice_user_turn_id = entry["id"]
            self._voice_user_stream = entry["text"]
            owner = entry["id"]
        else:
            owner, clear = stream_owner(self._backend_display_id(),
                                        entry["id"], entry["owner"])
            if clear:
                self._stream_post(clear, "", item_id=entry["id"])
            entry["owner"] = owner
            self._voice_stream_owner = owner
        self._stream_post(owner, entry["text"], role=entry["role"], item_id=entry["id"])

    def _transcript_delta(self, role, text, source=None):
        self._transcript_publish(self._transcript_state().delta(role, text, source))

    def _transcript_segment(self, role, text, source=None):
        entry = self._transcript_state().segment(role, text, source)
        self._transcript_publish(entry)
        if entry and (entry["boundary"] or (entry["source"] is None and getattr(self, "dc", None) is None)):
            # Explicit legacy fallback: no RTC channel means no turn.done will arrive.
            self._transcript_final(role, None, source)

    def _transcript_final(self, role, text=None, source=None):
        entry = self._transcript_state().finish(role, text, source)
        if not entry:
            return
        if role == "user":
            self._voice_pending_user = (time.time(), entry["text"])
        self._subtitle_done(role, entry["text"], item_id=entry["id"], owner=entry["owner"])

    def _subtitle_done(self, role, text, *, item_id=None, owner=None):
        """Commit one RTC turn (or an explicit legacy fallback) using its stable identity.
        用户句独立使用 vu-<id>.u，草稿和定稿共用身份，不复用助手轮次。
        """
        text = str(text or "").strip()
        if not text:
            return
        if role == "user":
            # transcript/done 完成的是一个字幕段，并不保证助手已经回答。
            # 用户连续补充两句/打断助手时，复用助手轮次会覆盖前一句历史；
            # 助手 done 又可能在用户说到一半时清空该轮次，导致草稿与定稿错位。
            tid = item_id or self._voice_user_turn_id or ("vu-" + str(time.time_ns()) + ".u")
            self._voice_user_turn_id = None
            # 先推草稿、再落库（与助手侧同一条顺序）：落库会触发侧栏权威重载，
            # 草稿必须赶在它前面，否则会在重载后又叠一份 —— 同一句出现两次。
            self._voice_user_stream = ""
            try:
                self._stream_post(tid, text, role="user", item_id=tid)
            except Exception:
                pass
            self._history_post({"user": text, "via": "voice", "turn_id": tid,
                                "item_id": tid, "role": "user", "stream_final": 1})
            # 后台轮还在跑：之后的回答排到这句下面去（见 _segment_backend_turn）。
            if self._turn is not None:
                self._segment_backend_turn("user-voice")
        elif role == "assistant":
            tid = item_id or self._voice_turn_id or ("v-" + str(int(time.time() * 1000))[-12:])
            fixed_owner = owner or self._voice_stream_owner
            self._voice_turn_id = None
            self._voice_stream = ""
            self._voice_stream_owner = None
            if not re.search(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text):
                self.log("history_skip_punct", text=text[:20])   # 「。」这种纯标点回复不记
                return
            # ⭐ 归属依据 = 后台的实际调用（用户 2026-09-18）。这句若属于某次后台任务，
            #   就**直接作为 part 并进那条轮次记录**，根本不另建 v- 记录 ——
            #   上一版是"先建再收拢"，结果半截草稿、完整句、零散记录三份并存（实录里三样都在）。
            #   不建就没得收，这比事后删干净。
            #   已开始流式的句子固定在原 owner；没有绑定时仅认当前后台轮，不按20秒猜。
            owner = fixed_owner or self._backend_display_id()
            if owner and not str(owner).startswith("v-"):
                self._voice_post(owner, text, item_id=tid, final=True)
                # ⚠ 不再往 rec["parts"] 里也塞一份：上面那次投递已经落库了，
                #   再塞就要靠"按 origin 合并"去重，多一条看不见的暗线。一处写入，一处真相。
            else:
                # 委派可能紧随其后（「好的，我看一下」→ 起后台轮）。记下来，等第一个工具
                # 调用时把这条收进那个容器 —— 否则它就永远是工具卡外面的一个孤框。
                self._pre_turn_voice = (tid, text, time.time())
                self._stream_post(tid, text, item_id=tid)
                self._history_post({"assistant": text, "via": "voice", "turn_id": tid,
                                    "item_id": tid, "role": "assistant", "stream_final": 1})

    def _voice_draft(self, owner: str) -> str:
        """流式草稿的正文 = 本轮**已说完的几句** + 正在说的这句。

        ⚠ 容器只有一个草稿槽。只投"正在说的这句"的话，上一句会被下一句的第一个字顶掉 ——
          用户 2026-09-19：「开头显示正常，中途文字突然消失，说完后又出现了」。
          "又出现"是轮次收尾那次重载把落库的几句一起渲出来（中途事件是故意不发的，
          发了会打断工具投递，见 assistant.py 那段）。
          把已完成的几句一起带上，草稿内容就跟最终形态一致，中途不再有空档。
        """
        done = self._voice_parts.get(owner) or []
        cur = self._voice_stream
        return "\n\n".join(list(done) + ([cur] if cur else []))

    def _voice_post(self, owner: str, text: str, absorb: str | None = None, *, item_id=None, final=False):
        """把一句语音正文并进某个后台轮容器。

        ⚠ 每次都重发**这一轮累计的全部语音正文**。服务端按 origin 整组替换（见
          _convo_upsert_turn），只发最新那句就等于把同轮里之前说过的话删掉 ——
          2026-09-18 实录里一轮两句都路由对了，存储里却只剩后一句。
        """
        acc = self._voice_parts.setdefault(owner, [])
        identities = getattr(self, "_voice_part_ids", None)
        if identities is None:
            identities = self._voice_part_ids = {}
        ids = identities.setdefault(owner, [])
        while len(ids) < len(acc):
            ids.append("voice:" + owner + ":" + str(len(ids)))
        if item_id and item_id in ids:
            acc[ids.index(item_id)] = text[:32000]
        elif text and (item_id or not acc or acc[-1] != text):
            acc.append(text[:32000])
            ids.append(item_id or "voice:" + owner + ":" + str(time.time_ns()))
        del acc[:-12]
        del ids[:-12]
        while len(self._voice_parts) > 8:            # 只留最近几个容器，别无限长
            old_owner = next(iter(self._voice_parts))
            self._voice_parts.pop(old_owner)
            identities.pop(old_owner, None)
        body = {"parts": [{"kind": "text", "text": t[:32000], "origin": "voice", "id": mid, "item_id": mid}
                           for t, mid in zip(acc, ids) if not item_id or mid == item_id],
                "via": "voice", "turn_id": owner,
                "upsert_only": 1, "create_if_missing": 1}
        if item_id:
            body["item_id"] = item_id
        if final:
            body["stream_final"] = 1
        if absorb:
            body["absorb"] = [absorb]
        # ⚠ 顺序要紧：**先推草稿、再落库**。
        #   侧栏收到落库事件会做权威重载（清掉草稿、用库里的内容替换）。
        #   2026-09-21 我把这条放在落库之后，于是草稿在重载**之后**才到，
        #   又在权威内容上面加了一份 —— 表现就是同一段话出现两次、或者闪一下又没。
        #   放在前面：草稿先变成完整文本（治掉半截显示），随后重载把它替换成同一份，
        #   内容一致所以看不出替换。
        try:
            self._stream_post(owner, text if item_id else (chr(10) + chr(10)).join(acc), item_id=item_id)
        except Exception:
            pass
        self._history_post(body)

    def _history_enabled(self) -> str:
        url = str(self.settings.get("historyUrl") or "").rstrip("/")
        return url if url and self.settings.get("historyEnabled", True) else ""

    def _history_post(self, body: dict):
        if not self._history_enabled():
            return
        body = copy.deepcopy(body)
        body.setdefault("origin", "voice" if body.get("via") == "voice" else "runner")
        if getattr(self, "thread_id", None):
            body.setdefault("thread_id", self.thread_id)
        body.setdefault("streamRevision", self._next_history_revision())
        if body.get("stream_final"):
            terminal = getattr(self, "_stream_terminal", None)
            if terminal is None:
                terminal = self._stream_terminal = set()
            terminal.add(self._history_stream_key(body))
            if len(terminal) > 512:
                terminal.intersection_update(list(terminal)[-256:])
        self._history_q.put(("log", body))

    def _next_history_revision(self):
        self._history_revision = max(getattr(self, "_history_revision", 0) + 1, int(time.time() * 1_000_000))
        return self._history_revision

    @staticmethod
    def _history_stream_key(body):
        return (body.get("thread_id") or "", body.get("turn_id") or "",
                body.get("item_id") or body.get("turn_id") or "")

    def _stream_start_post(self, turn_id: str):
        """后台轮开始：把真实 turn id 推给侧栏（SSE stream:"start"）。"""
        if not self._history_enabled() or not turn_id:
            return
        self._history_q.put(("stream_start", {"turn_id": turn_id, "stream": "start",
                                             "thread_id": getattr(self, "thread_id", None),
                                             "streamRevision": self._next_history_revision()}))

    def _tool_error_log(self, turn_id, tool: str, args, brief: str, status: str):
        """工具调用出错 → voice-cli/tool-errors.jsonl（用户 2026-09-15：自动记下来，我自己去分析）。"""
        try:
            row = {"t": round(time.time(), 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "turnId": str(turn_id or "")[:40], "tool": tool[:120],
                   "status": status, "args": (json.dumps(args, ensure_ascii=False) if args is not None else "")[:1500], "result": str(brief or "")[:1500]}
            with (BASE / "tool-errors.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + chr(10))
            # 统一错误日志（用户 2026-09-19：「所有报错都有记录价值，最好是统一记录在一起
            # 方便你每次查看」）。分散在各处的日志等于没有日志 —— 今晚我就因为只看了
            # 运行器的内存事件，漏掉了盘上这份工具报错，反过来去问用户要报错原文。
            # ⚠ 两处都写：tool-errors.jsonl 是既有消费方（保持兼容），统一日志是给人翻的那份。
            error_log(
                "tool", tool[:120],
                str(brief or "")[:600],
                "status=" + str(status) + " turn=" + str(turn_id or "")[:40])
            self.log("tool_error", tool=tool[:120], status=status, result=str(brief or "")[:200])
        except Exception:
            pass

    def _stream_post(self, turn_id: str, text: str, role: str = "assistant", *, item_id=None):
        """Coalesce snapshots per source item, never across text/tool/artifact identities."""
        if not self._history_enabled() or not turn_id:
            return
        body = {"thread_id": getattr(self, "thread_id", None), "turn_id": turn_id,
                "item_id": item_id or turn_id, "content": text[:32000], "role": role,
                "origin": "voice" if str(item_id or turn_id).startswith(("v-", "vu-")) else "runner",
                "stream": "delta"}
        key = self._history_stream_key(body)
        if key in getattr(self, "_stream_terminal", set()):
            return
        body["streamRevision"] = self._next_history_revision()
        self._stream_latest[key] = body
        if key not in self._stream_queued:
            self._stream_queued.add(key)
            self._history_q.put(("stream", key))

    def _history_request(self, path: str, body: dict) -> dict:
        import urllib.request
        token = HISTORY_TOKEN_PATH.read_text(encoding="utf-8").strip()
        req = urllib.request.Request(self._history_enabled() + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read() or b"{}")

    def _history_worker(self):
        import urllib.error
        last_stream_at = 0.0
        while True:
            kind, payload = self._history_q.get()
            try:
                if kind == "stream_start":
                    self._history_request("/api/assistant/stream", payload)
                elif kind == "stream":
                    # 节流：草稿最快每 0.25 s 一条；发的时候取该轮最新全文
                    wait = 0.25 - (time.monotonic() - last_stream_at)
                    if wait > 0:
                        time.sleep(wait)
                    self._stream_queued.discard(payload)
                    if payload not in self._stream_latest:
                        continue  # 已经落库的迟到队列项不得再发送空草稿覆盖定稿
                    body = self._stream_latest.pop(payload)
                    self._history_request("/api/assistant/stream", body)
                    last_stream_at = time.monotonic()
                    self.history_stats["streamed"] += 1
                else:
                    if payload.get("stream_final"):
                        key = self._history_stream_key(payload)
                        pending = self._stream_latest.get(key)
                        if pending and pending.get("streamRevision", 0) <= payload["streamRevision"]:
                            # Drain this item's last snapshot before its authoritative final.
                            self._stream_latest.pop(key, None)
                            self._history_request("/api/assistant/stream", pending)
                    r = self._history_request("/api/assistant/log", payload)
                    self.history_stats["written"] += 1
                    via, tid, n, up = payload.get("via"), payload.get("turn_id"), r.get("n"), r.get("upserted")
                    # absorbed：这次收走了几条零散语音记录。不记的话"收拢到底跑没跑"只能去翻库
                    # （2026-09-18 就是这么查出来收拢代码压根执行不到的）。
                    ab = r.get("absorbed")
                    self.loop.call_soon_threadsafe(
                        lambda via=via, tid=tid, n=n, up=up, ab=ab:
                            self.log("history_written", via=via, turnId=tid, n=n, upserted=up, absorbed=ab))
            except urllib.error.HTTPError as e:
                detail, code = "", e.code
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                self.history_stats["errors"] += 1
                self.history_stats["lastError"] = "HTTP %s %s" % (code, detail)
                self.loop.call_soon_threadsafe(lambda kind=kind, code=code, detail=detail:
                                                self.log("history_error", eventKind=kind, status=code, detail=detail))
            except Exception as e:
                message = clean(e)
                self.history_stats["errors"] += 1
                self.history_stats["lastError"] = message
                self.loop.call_soon_threadsafe(lambda kind=kind, message=message:
                                                self.log("history_error", eventKind=kind, message=message))
    def audio_stats(self) -> dict:
        return {
            "speakerGaps": self.speaker.gaps if self.speaker else None,
            "speakerUnderruns": self.speaker.underruns if self.speaker else None,
            "speakerFlags": self.speaker.status_flags if self.speaker else None,
            "speakerBufferedMs": round(len(self.speaker.buf) / 2 / self.speaker.out_rate * 1000) if self.speaker else None,
            "micDrops": self.mic.drops if self.mic else None,
            # 主动削深：正常现象，单独看；跟 micDrops 混在一起会让人误判。
            "micTrimmed": getattr(self.mic, "trimmed", None) if self.mic else None,
            "micFlags": self.mic.status_flags if self.mic else None,
            "micQueued": self.mic.q.qsize() if self.mic else None,
            # 直连管道的计数（走声卡那条路时为 None）：收了多少帧、补了多少静音、发出去多少帧。
            # 「通了但没声音」只能靠这三个数分辨是哪一端没动。
            "pipeIn": getattr(self.mic, "received", None) if self.mic else None,
            "pipeSilence": getattr(self.mic, "silence", None) if self.mic else None,
            "pipeOut": getattr(self.speaker, "sent", None) if self.speaker else None,
            "loopLagMaxMs": round(self._loop_lag_max * 1000),
            "loopLagOver50ms": self._loop_lag_over,
        }

    def mark_activity(self, what: str):
        """有真实活动就把闲置计时归零。⚠ 上下文注入不算 —— 那是我们自己推的，不是人在用。"""
        self.last_activity_at = time.time()
        self._last_activity_what = what

    async def idle_stop_loop(self):
        """闲置到点就结束通话。只关不开（开的那一半至今无解，见 voice_autoclose 的说明）。"""
        while not self.shutting_down:
            await asyncio.sleep(30)
            try:
                # 顺路做线程压缩：它要跑一轮模型，所以只在后台空闲时做。
                # 放在 connected 判断之前 —— 没在通话时线程照样会被文字侧撑长。
                await self.maybe_autocompact()
                minutes = float(self.settings.get("idleStopMinutes") or 0)
                if minutes <= 0 or self.session_state != "connected":
                    continue
                if self.user_speaking or self.backend_busy:
                    self.mark_activity("busy")
                    continue
                base = self.last_activity_at or self.session_started_at
                if not base:
                    continue
                idle = time.time() - base
                if idle < minutes * 60:
                    continue
                self.log("idle_stop", idleSec=round(idle), thresholdMin=minutes,
                         lastActivity=getattr(self, "_last_activity_what", None))
                self.quota_sample("idle_stop")
                await self.session_stop("idle-%dmin" % int(minutes))
            except Exception as e:   # noqa: BLE001
                self.log("idle_stop_error", message=clean(e))

    async def _loop_lag_monitor(self):
        """事件循环每 100 ms 打一次点：睡过头多少就是这段时间里有多重的同步工作堵住了它
        （音频收发都在这个循环上，它一卡，扬声器就空、麦克风队列就积）。每 30 s 有变化就记一条。"""
        last = None
        while not self.shutting_down:
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            lag = time.monotonic() - t0 - 0.1
            if lag > self._loop_lag_max:
                self._loop_lag_max = lag
            if lag > 0.05:
                self._loop_lag_over += 1
            if time.monotonic() - self._audio_stats_at >= 30:
                self._audio_stats_at = time.monotonic()
                if self.session_state == "connected":
                    cur = self.audio_stats()
                    key = json.dumps({k: v for k, v in cur.items() if k not in ("speakerBufferedMs", "micQueued")}, sort_keys=True)
                    if key != last:
                        last = key
                        self.log("audio_stats", **cur)
                self._loop_lag_max = 0.0

    def status(self) -> dict:
        return {
            "audio": self.audio_stats(),
            "runner": {"pid": os.getpid(), "uptimeSeconds": round(time.time() - self.started_at), "listen": f"http://{LISTEN[0]}:{LISTEN[1]}",
                       "appServer": bool(self.app), "threadId": self.thread_id, "appServerExits": self.app_server_exits,
                       "appServerRelaunchPending": bool(self.app_relaunch_task and not self.app_relaunch_task.done())},
            "session": {"state": self.session_state, "sessionNo": self.session_no, "sessionId": self.session_id,
                        "reconnects": self.reconnects, "seconds": round(time.time() - self.session_started_at) if self.session_started_at and self.session_state == "connected" else 0,
                        "userSpeaking": self.user_speaking, "backendBusy": self.backend_busy, "lastError": self.last_error,
                        "micLevel": round(self.mic.level, 1) if self.mic else None, "needsRestart": sorted(self.pending_cold),
                        "profile": self.session_profile},
            "settings": self.settings, "hotKeys": sorted(HOT_KEYS), "coldKeys": sorted(COLD_KEYS),
            "usage": {k: v for k, v in self.usage.items() if k != "usageSummary"},
            "transcripts": [{"t": t, "role": r, "text": x} for t, r, x in list(self.transcripts)[-12:]],
            "bridgeFlag": BRIDGE_FLAG.exists(),
            "audioPipe": PIPE_FLAG.exists(),
            "history": dict(self.history_stats),
            "context": {"reviewPrefetchAvailable": callable(getattr(self._jev, "review_source", None)),
                        "reviewCache": dict(self._jev.review_cache_status),
                        "pageKey": self._ctx["page_key"], "dwellSeconds": round(time.time() - self._ctx["page_since"]) if self._ctx["page_since"] else None,
                        "fp": dict(self._ctx["fp"])},
        }

    def events_since(self, since: int, limit: int = 300) -> dict:
        rows = [e for e in self.events if e["seq"] > since][-limit:]
        return {"events": rows, "next": rows[-1]["seq"] if rows else since, "latest": self.seq}

    async def shutdown(self):
        self.shutting_down = True
        await self.session_stop("shutdown")
        if self.app:
            await self.app.close()
        self.write_bridge_flag(False)
        self.log("runner_shutdown")


class Handler(BaseHTTPRequestHandler):
    runner: Runner = None  # type: ignore

    def log_message(self, *a):
        pass

    def _send(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run(self, coro, timeout=90):
        return asyncio.run_coroutine_threadsafe(coro, self.runner.loop).result(timeout)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        r = self.runner
        try:
            if u.path == "/status":
                return self._send(200, r.status())
            if u.path == "/transcript":
                return self._send(200, r.transcript(int(q.get("limit", ["12"])[0]),
                                                    float(q.get("sinceSeconds", ["0"])[0])))
            if u.path == "/events":
                return self._send(200, r.events_since(int(q.get("since", ["0"])[0]), int(q.get("limit", ["300"])[0])))
            if u.path == "/settings":
                return self._send(200, {"settings": r.settings, "hotKeys": sorted(HOT_KEYS), "coldKeys": sorted(COLD_KEYS), "needsRestart": sorted(r.pending_cold)})
            if u.path == "/skills":
                # 模型看得见的 skill 那一层。工具表原来只画了 MCP 的常驻/折叠两个池，
                # skill 是第三层，页面上此前完全看不见（2026-09-16）
                async def _skills():
                    await r.ensure_app()
                    res = await r.app.call("skills/list", {}, timeout=30)
                    out = []
                    for group in ((res or {}).get("data") or []):
                        for sk in ((group or {}).get("skills") or []):
                            out.append({"name": sk.get("name"),
                                        "description": (sk.get("description") or "")[:200],
                                        "cwd": group.get("cwd")})
                    return {"ok": True, "skills": out}
                return self._send(200, self._run(_skills(), 60))
            if u.path == "/quota":
                return self._send(200, self._run(r.quota()))
            if u.path == "/catalog":
                return self._send(200, self._run(r.catalog()))
            if u.path == "/tasks":
                if bw_scheduler is None:
                    return self._send(500, {"ok": False, "msg": "bw_scheduler 未装载"})
                return self._send(200, {"ok": True, "tasks": bw_scheduler.list_tasks()})
            if u.path == "/tasks/runs":
                tid = (q.get("id") or [""])[0]
                return self._send(200, {"ok": True, "id": tid, "runs": bw_scheduler.last_runs(tid, int((q.get("limit") or ["10"])[0]))})
            if u.path == "/devices":
                return self._send(200, {"devices": [{"index": i, "name": d["name"], "in": d["max_input_channels"], "out": d["max_output_channels"],
                                                     "api": sd.query_hostapis()[d["hostapi"]]["name"]} for i, d in enumerate(sd.query_devices())]})
            return self._send(404, {"ok": False, "msg": "no such path"})
        except Exception as e:
            return self._send(500, {"ok": False, "msg": clean(str(e) or type(e).__name__)})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._send(400, {"ok": False, "msg": "bad json"})
        r = self.runner
        try:
            if u.path == "/settings":
                return self._send(200, self._run(r.update_settings(body)))
            if u.path == "/notify/ack":
                # 后台自己 ack 不了：它的沙盒是 read-only（写不了 notifications.json），
                # 而且线程的 cwd 是 BASE=…/BWReader/voice-cli，脚本在它的**上一级** ——
                # 指令里只写文件名，它只能满机器搜（2026-09-20 实录：三次 rg 全落空，
                # 于是那条通知每 30 分钟重投一次、刷了满屏）。副作用走工具，沙盒不动。
                _ntf = str((body or {}).get("id") or "").strip()
                if not _ntf:
                    return self._send(400, {"ok": False, "msg": "missing id"})
                return self._send(200, {"ok": r._notify_ack(_ntf), "id": _ntf})
            if u.path == "/thread/new":
                async def _renew():
                    if r.session_state != "idle":
                        await r.session_stop("thread-renew")
                    r.log("thread_dropped", threadId=r.thread_id, why="POST /thread/new")
                    r.thread_id = None
                    r._thread_cleared = True
                    await r.ensure_app()
                    r.save_state()
                    return {"ok": True, "threadId": r.thread_id}
                return self._send(200, self._run(_renew(), 120))
            if u.path == "/thread/resume":
                async def _resume():
                    want = str(body.get("threadId") or "").strip()
                    if not want:
                        return {"ok": False, "msg": "threadId 不能为空"}
                    if want == r.thread_id:
                        return {"ok": True, "threadId": r.thread_id, "already": True}
                    if r.session_state != "idle":
                        await r.session_stop("thread-switch")
                    r.thread_id = None
                    r._thread_resume_target = want
                    await r.ensure_app()
                    r.save_state()
                    return {"ok": r.thread_id == want, "threadId": r.thread_id}
                return self._send(200, self._run(_resume(), 120))
            if u.path == "/thread/list":
                async def _list():
                    await r.ensure_app()
                    try:
                        res = await r.app.call("thread/list", {"limit": int(body.get("limit") or 30)}, timeout=30)
                    except Exception:   # noqa: BLE001 —— 参数形状不对就退回无参
                        res = await r.app.call("thread/list", {}, timeout=30)
                    # 刚建、还没说过话的线程不在 thread/list 里（它只列已落盘的），
                    # 于是用户报的"新开对话后列表里看不到"。补在最前面并标出当前这条。
                    try:
                        rows = (res or {}).get("data")
                        if isinstance(rows, list):
                            if r.thread_id and not any(
                                    isinstance(x, dict) and x.get("id") == r.thread_id for x in rows):
                                rows.insert(0, {"id": r.thread_id, "preview": "（当前对话，尚无记录）"})
                            for x in rows:
                                if isinstance(x, dict):
                                    x["current"] = x.get("id") == r.thread_id
                    except Exception as e:   # noqa: BLE001
                        r.log("thread_list_merge_error", message=clean(e))
                    return {"ok": True, "current": r.thread_id, "result": res}
                return self._send(200, self._run(_list(), 60))
            if u.path == "/thread/items":
                async def _items():
                    tid = str(body.get("threadId") or "") or (r.thread_id or "")
                    if not tid:
                        return {"ok": False, "error": "缺 threadId"}
                    await r.ensure_app()
                    params = {"threadId": tid, "limit": int(body.get("limit") or 80)}
                    if body.get("cursor"):
                        params["cursor"] = str(body["cursor"])
                    res = await r.app.call("thread/items/list", params, timeout=30)
                    return {"ok": True, "threadId": tid, "result": res}
                return self._send(200, self._run(_items(), 60))
            if u.path == "/thread/info":
                async def _info():
                    tid = str(body.get("threadId") or "") or (r.thread_id or "")
                    if not tid:
                        return {"ok": False, "error": "缺 threadId"}
                    await r.ensure_app()
                    res = await r.app.call("thread/read", {"threadId": tid}, timeout=30)
                    return {"ok": True, "threadId": tid, "result": res}
                return self._send(200, self._run(_info(), 60))
            if u.path == "/thread/compact":
                async def _compact():
                    tid = str(body.get("threadId") or "") or (r.thread_id or "")
                    if not tid:
                        return {"ok": False, "error": "缺 threadId"}
                    return await r.thread_compact(tid, reason=str(body.get("reason") or "manual"))
                return self._send(200, self._run(_compact(), 240))
            if u.path == "/thread/steer":
                async def _steer():
                    text = str(body.get("text") or "").strip()
                    if not text:
                        return {"ok": False, "error": "缺 text"}
                    return await r.steer_running_turn(text, tag=str(body.get("tag") or "manual"))
                return self._send(200, self._run(_steer(), 60))
            if u.path == "/thread/delete":
                async def _delete():
                    tid = str(body.get("threadId") or "")
                    if not tid:
                        return {"ok": False, "error": "缺 threadId"}
                    await r.ensure_app()
                    renewed = False
                    if tid == r.thread_id:
                        # 删的是当前这条：先停会话再换新的，否则语音那头挂在一条已经不存在的线程上
                        if r.session_state != "idle":
                            await r.session_stop("thread-delete")
                        r.thread_id = None
                        r._thread_cleared = True
                        renewed = True
                    await r.app.call("thread/delete", {"threadId": tid}, timeout=30)
                    if renewed:
                        await r.ensure_app()
                        r.save_state()
                    r.log("thread_deleted", threadId=tid[-12:], renewed=renewed)
                    return {"ok": True, "threadId": r.thread_id}
                return self._send(200, self._run(_delete(), 120))
            if u.path == "/thread/rename":
                async def _rename():
                    tid = str(body.get("threadId") or "") or (r.thread_id or "")
                    name = str(body.get("name") or "").strip()[:80]
                    if not tid or not name:
                        return {"ok": False, "error": "缺 threadId 或 name"}
                    await r.ensure_app()
                    # ⚠ 方法名是 thread/name/set —— rename / setTitle / update 都不存在（2026-09-16 实探）
                    await r.app.call("thread/name/set", {"threadId": tid, "name": name}, timeout=30)
                    r.log("thread_renamed", threadId=tid[-12:], name=name[:40])
                    return {"ok": True, "name": name}
                return self._send(200, self._run(_rename(), 60))
            if u.path == "/session/start":
                return self._send(200, self._run(r.session_start(str(body.get("reason") or "manual"), body.get("profile")), 120))
            if u.path == "/session/stop":
                return self._send(200, self._run(r.session_stop(str(body.get("reason") or "manual"), bool(body.get("afterSpeech")),
                                                                float(body.get("graceSeconds") or 10)), 60))
            if u.path == "/session/restart":
                return self._send(200, self._run(r.session_restart(body.get("profile")), 150))
            if u.path == "/debug/dc":
                r.on_dc_message(json.dumps(body.get("event") or {}))
                return self._send(200, {"ok": True})
            if u.path == "/board":
                return self._send(200, self._run(r.board(str(body.get("text") or ""), body.get("toVoice"), body.get("toBackend"))))
            if u.path == "/say":
                # fallback="turn"：语音不在线时交后台决定（说/打电话/等），别静默丢掉
                # source=backend：后台模型自己调的 voice_say。委派轮里要挡（见 say()）；
                # 定时投递/通知不带这个标记，照旧放行。
                return self._send(200, self._run(r.say(str(body.get("text") or ""),
                                                      str(body.get("fallback") or "none"),
                                                      str(body.get("source") or ""))))
            if u.path == "/call":
                return self._send(200, self._run(r.call_user(str(body.get("text") or ""), str(body.get("title") or ""),
                                                             str(body.get("ntf") or "misc"), str(body.get("reason") or "")), timeout=260))
            if u.path == "/tell":
                return self._send(200, self._run(r.tell(str(body.get("text") or ""), str(body.get("role") or "developer"))))
            if u.path == "/tasks/upsert":
                info = bw_scheduler.upsert(str(body.get("id") or ""), body.get("flow") or {}, bool(body.get("enabled", True)))
                return self._send(200, {"ok": True, "task": info})
            if u.path == "/tasks/delete":
                return self._send(200, {"ok": True, "deleted": bw_scheduler.delete(str(body.get("id") or ""))})
            if u.path == "/tasks/run":
                return self._send(200, bw_scheduler.start_run(str(body.get("id") or ""), "manual"))
            if u.path == "/tasks/enable":
                reg = bw_scheduler.load_registry()
                rec = reg["tasks"].setdefault(str(body.get("id") or ""), {})
                rec["enabled"] = bool(body.get("enabled", True))
                if rec["enabled"] and not rec.get("nextRunAt"):
                    nxt = bw_scheduler.next_run(bw_scheduler.read_flow(str(body.get("id"))).get("schedule") or {})
                    rec["nextRunAt"] = nxt.isoformat(timespec="seconds") if nxt else None
                bw_scheduler.save_registry(reg)
                return self._send(200, {"ok": True, "task": bw_scheduler.describe(str(body.get("id")))})
            if u.path == "/typed":
                return self._send(200, self._run(r.typed(str(body.get("text") or ""), body.get("attachmentIds"), body.get("submissionId")), 60))
            if u.path == "/turn":
                return self._send(200, self._run(r.turn(str(body.get("text") or ""), body.get("additionalContext"))))
            if u.path == "/inject":
                return self._send(200, self._run(r.inject(str(body.get("text") or ""), str(body.get("role") or "developer"))))
            if u.path == "/pause":
                return self._send(200, r.dc_send({"type": "input_audio.pause"}))
            if u.path == "/resume":
                return self._send(200, r.dc_send({"type": "input_audio.resume"}))
            if u.path == "/shutdown":
                threading.Thread(target=lambda: (time.sleep(0.2), self._run(r.shutdown(), 60), os._exit(0)), daemon=True).start()
                return self._send(200, {"ok": True})
            return self._send(404, {"ok": False, "msg": "no such path"})
        except Exception as e:
            return self._send(500, {"ok": False, "msg": clean(str(e) or type(e).__name__)})


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """独占端口的 HTTP 服务。

    ⚠ http.server 默认 allow_reuse_address = True，而 Windows 上 SO_REUSEADDR 的意思是
      「允许别的进程同时绑同一个端口」—— 于是"先绑端口、绑不上就退出"这道单实例闸
      形同虚设。2026-09-23 实录：ReaderPC 在同一秒拉起两个运行器，两个都绑上了；
      一个续接了原对话线程，另一个撞上"线程已被占用"就**新开了一条空线程**，
      用户的通话落在空线程上 —— 他刚打字发过去的内容，后台说"没看到"。
    """
    if sys.platform == "win32":
        allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    loop = asyncio.new_event_loop()
    runner = Runner(loop)
    Handler.runner = runner
    try:
        httpd = ExclusiveHTTPServer(LISTEN, Handler)
    except OSError as e:
        print(json.dumps({"kind": "bind_failed", "message": str(e)}), flush=True)
        return 2
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    runner.log("runner_started", listen=f"http://{LISTEN[0]}:{LISTEN[1]}", settings=str(SETTINGS_PATH))
    runner.write_bridge_flag(True)   # 运行器在 = 外部语音后端在：桥把 App 的 START/STOP 交给我们

    async def boot():
        asyncio.create_task(runner._jev.maintain_review_snapshot(lambda: runner.shutting_down))
        try:
            await runner.ensure_app()
        except Exception as e:
            runner.log("app_server_start_error", message=clean(e))
        if runner.settings.get("autoStartSession"):
            await runner.session_start("auto")
        async def scheduler_loop():
            while not runner.shutting_down:
                await asyncio.sleep(30)
                if not runner.settings.get("schedulerEnabled", True) or bw_scheduler is None:
                    continue
                try:
                    events = await asyncio.get_running_loop().run_in_executor(None, bw_scheduler.tick)
                    for ev in events:
                        runner.log("scheduler", **ev)
                except Exception as e:   # noqa: BLE001
                    runner.log("scheduler_error", message=clean(e))
        asyncio.create_task(scheduler_loop())

        async def app_gone_watch():
            """App 档位的会话只该活在 App 通话期间。App 断线/心跳超时时桥只关连接、不会来 /session/stop，
            会话就会挂在线缆上烧额度（2026-09-14 实录：桥 idle 了 40 分钟，运行器还 connected）。
            桥的状态文件说 captureActive=false 连续 2 拍（约 10 秒）→ 自己停。

            2026-09-15 用户拍板的两条脾气：**从 App 启动的（profile=app）断了就立刻关**
            （App 接得快，不值得留着热身）；**从电脑启动的（profile=local）保持**，
            只由手动关闭或 idleStopMinutes 结束 —— 后者本来就不进这个循环。
            一拍改两拍是因为桥换连接的瞬间会有一拍 captureActive=false，一拍就动手会误杀。"""
            strikes = 0
            stale = 0
            status_path = BRIDGE_RUNTIME / "computer-voice-direct.status.json"
            while not runner.shutting_down:
                await asyncio.sleep(5)
                try:
                    if runner.session_state != "connected" or runner.session_profile != "app":
                        strikes = 0
                        continue
                    st = json.loads(status_path.read_text(encoding="utf-8"))
                    # 2026-09-15：原来写成 now - mktime(...) - time.timezone，符号错了，
                    # 实际值 = 真实年龄 + 2×|时区偏移|（JST 下 +64800 秒），fresh 恒 False
                    # → 自动关闭从上线起一次都没触发过。UTC 串就该用 timegm。
                    age = time.time() - calendar.timegm(time.strptime(st["updatedAtUtc"][:19], "%Y-%m-%dT%H:%M:%S"))
                    fresh = age < 120
                    if fresh and not st.get("captureActive"):
                        strikes += 1
                    else:
                        if not fresh:
                            stale += 1
                            if stale % 60 == 1:   # 每 5 分钟出一次声：状态文件陈旧 = 看门狗此刻是瞎的
                                runner.log("app_gone_watch_stale", ageSec=round(age, 1), path=str(status_path))
                        strikes = 0
                    if strikes >= 2:
                        runner.log("app_gone", strikes=strikes, bridgeState=st.get("state"))
                        strikes = 0
                        await runner.session_stop("app-gone")
                except Exception as e:   # noqa: BLE001
                    runner.log("app_gone_watch_error", message=clean(e))
        runner.app_gone_strikes = 0
        asyncio.create_task(app_gone_watch())
        asyncio.create_task(runner._ctx_loop())
        asyncio.create_task(runner._ink_late_loop())
        asyncio.create_task(runner._notify_push_loop())
        asyncio.create_task(runner._loop_lag_monitor())
        asyncio.create_task(runner.quota_watch_loop())
        asyncio.create_task(runner.idle_stop_loop())
        while True:
            # 标记文件被别的实例/安装器清掉过（2026-09-14 实测），每 20 秒补一次：运行器活着标记就得在
            await asyncio.sleep(20)
            try:
                if not runner.shutting_down and not BRIDGE_FLAG.exists():
                    runner.write_bridge_flag(True)
                    runner.log("bridge_flag_healed")
            except Exception as e:
                runner.log("bridge_flag_error", message=clean(e))

    try:
        loop.run_until_complete(boot())
    except KeyboardInterrupt:
        loop.run_until_complete(runner.shutdown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
