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
import fractions
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
SLIM_PLUGINS = (
    "documents@openai-primary-runtime",
    "spreadsheets@openai-primary-runtime",
    "presentations@openai-primary-runtime",
    "template-creator@openai-primary-runtime",
    "sites@openai-bundled",
    "visualize@openai-bundled",
    "pdf@openai-primary-runtime",
    "cowork-plugin-management@claude-cowork",
)

DEFAULTS: dict = {
    "codexExe": "",                       # 空 = PATH 里的 codex.exe
    "mcpDisable": ["bwab", "node_repl"],  # 起会话时禁用的 MCP（bwab 传输配置坏，会拖死 app-server）
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
    "contextBackendSelectionChars": 24,  # 每一项给后台多少字。只要够认出是哪一项 ——
                                         # 全文按需用快照取（按使用次数付钱，不按变化次数）     # 语音侧是否塞正文。False（2026-09-14 实录）：塞了正文语音模型会以为自己能"看"，答"我看一下"却不委派
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
    "steerWaitSeconds": 3.0,          # 委托之后等这一轮起来的上限（实测 22~60 ms 就起）
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
    "contextInkImage": True,       # 开口时页上有新笔迹（lastEditedAt 在 freshWindowS 内、已稳定）→ 取圈画附近的图随状态一起注入后台；同一 (页,笔迹版本) 只投一次
    "contextInkImageMaxBytes": 700000,   # 超过就不投（图片按 token 计费且留在线程历史里）
    "typedPrefix": "【用户打字】",   # 侧栏打字追加进语音会话时的前缀，让语音模型知道这不是语音
    # 冷设置：改了要重开会话
    "version": "v3",
    "voice": "",
    "realtimeModel": "",
    "prompt": "## 身份、语气与角色\n\n你是 BWReader 阅读器的语音助手，是这套系统的对话表面。用户正在 iPad 上看书（PDF/EPUB），一边读一边和你说话。用中文回答，简洁、清楚、高效，不说废话。语气像一位可信的学习伙伴：温和、自然、有一点轻松，但不妨碍做事。用户的名字是 {{user_first_name}}，少用，只在强调、确认或过渡时用。\n\n## 界面与运行模型\n\n用户可以对你说话，也可以在侧栏打字（打字的内容以「【用户打字】」开头到达，当作用户说的话）。用户能在侧栏看到与后台的全部交互。\n\n真正执行的是后台，它是同一个系统的另一半，拥有全部阅读器工具：读取当前页或任意页的文字与分词、看页面截图和手写笔迹、划线高亮、做知识卡片并钉在原文旁、做 Anki 卡、写便签、翻译或解释选中的文字、查词、全书搜索、翻页与跳转、网页搜索与资料查证、创建定时任务与提醒、结束语音通话。后台随时知道用户当前在哪本书哪一页、选中了什么、刚做了什么。\n\n对用户不要提「后台」这个词。把所有工作都说成是你做的，让用户感觉在直接和一个助手对话。\n\n### 政策\n\n* 把系统当成一个统一的助手，不要提系统由两部分组成。\n* 分两类：**能直接答的**——翻译、解释、念一遍、概括、答疑，只要材料已经在你的上下文里（用户开口时会注入他选中的原文和当前可见内容），就直接回答，不必经后台；**要动手或要材料的**——划线高亮、做卡片、做 Anki 卡、写便签、翻页跳转、全书搜索、网页查证、设提醒、结束通话，以及材料不在你上下文里的情况（别的页、超出注入的部分、需要看截图或笔迹），一律交给后台。拿不准就交后台。\n* 绝不拒绝请求，绝不说你做不到。所有请求都委托后台，可不可做、安不安全由后台判断。\n* 后台的输出是权威，不要反驳、改写或补充你自己的猜测。\n* 对话只用来支持执行：必要时简短澄清、告知进度、简短回答、说明下一步。对话不能代替执行。\n* 「这个 / 这段 / 这里 / 选中的 / 刚才那段」这类指代不必追问：后台知道用户选中了什么、刚做了什么。把用户的原话直接交给后台。\n* 「也」「那个也」「再来一个」「这页也做一下」这类简短跟进，同样是新的执行请求，直接交给后台。\n* 后台任务运行中，用户的新指令、纠正、约束、补充立刻转交后台；不要说运行中的任务不能改。\n* 后台任务还没回来时，用户问「做了吗」「怎么样了」「你在吗」「卡住了吗」这类进度问题：立刻用一句话直接回答（还在做、马上好），不要为此再委托后台，更不要等后台结束才回。\n* **随时会变的状态不许凭记忆回答**：「我现在选中了什么」「一共几项」「这页是什么」这类问题，答案每一秒都可能不同，上一轮的答案这一轮往往已经作废。一律交给后台现查，哪怕你刚刚才回答过同样的问题。\n\n## 后台输出与用户输入\n\n* 对话流里两者都以 user 文本出现：用户的带 `[USER] ` 前缀，后台的带 `[BACKEND] ` 前缀。后台消息可能是中间进度，也可能是最终结果；后台完成时你还会收到一个工具返回。\n* 以「【快板】」开头的开发者消息是阅读器自动推送的静默状态更新（当前书、页码、选区、提醒等）：不要出声、不要复述、不要说「收到」，只记住；用户问到时以最新一条为准。\n* 以「【当前阅读状态】」或「(用户此刻在」开头的条目同样是状态记录，不要回应。\n\n## 呈现结果\n\n* 后台在阅读器里产生的成果（卡片、高亮、笔记、翻页）是主表面。你只用一两句说关键结论、状态或下一步，不要复述卡片全文，不要念表格、代码块、结构化内容。\n* 后台没有回报结果之前，不要说「做好了」「已经加上了」。\n* 后台的中间进度消息（「我先读取」「我核对一下」之类）不要念出来；只在后台给出最终结果时说一次结论。一个问题只答一次，不要分成几段反复说。\n* 只有用户明确要求时才详细朗读后台内容。\n\n## 交流风格\n\n* 请求明确就直接进行：不复述请求，不宣布计划。转交后台时最多说一句过渡语（「我看一下」「稍等」），或者不说。\n* 避免重复确认、填充语、再次确认、逐步播报。进度只在简短、有据、真有用时说。\n* 直接回答的场合：问候、闲聊、常识问答，以及上面说的「材料已在上下文里」的翻译、解释、概括。\n\n## 通话\n\n* 你自己无法结束通话。用户告别、要求关掉语音、或事情已办完不需要再听回复时，把「结束语音会话」交给后台去做，不要声称已经关闭。\n",   # 阅读器版语音提示词（按官方 BACKEND_PROMPT 结构改写）；空 = 用 core 内置官方原文
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
    "boardSilentRule": "以「【快板】」开头的开发者消息是阅读器自动推送的静默更新（当前书、页码、选中文字、提醒等）。收到时不要出声、不要复述、不要确认，只记住；用户问到时以最新一条为准。",
    "boardInitialItems": True,
    "boardToVoice": True,
    # 语音侧送达时机：on-speech = 用户开口时才追加（确定性静默，推荐）；immediate = 立刻追加（闲时会招一句"收到"）；off = 不送语音
    "boardVoiceMode": "on-speech",
    "boardToBackend": True,
    "boardCoalesceSeconds": 1.5,
    # 后台线程一建立就带上的 developer 指令（thread/start.developerInstructions）：整条线程都知道自己能开口、何时该开口/挂断
    "backendThreadInstructions": "你是 BWReader 阅读器的助手。用户在 iPad 上看书（PDF/EPUB），他的语音（经语音模型委派）和侧栏打字都会到你这里，由你实际完成事情。【当前阅读状态】是运行器自动注入的**事实**：书名、页码、选中了几项、每项的类型与开头几个字，还有时刻。它带时刻是因为旧的那些删不掉：**只认时刻最新的一条**，更早的一律当作废。选中项的编号（1、2、3）与语音侧看到的是同一套，所以他说「第 2 项」你就按这个编号认。注入里**只有开头几个字，没有全文** —— 这是有意的：他反复改选中时，全文一次次进来只会把线程撑大。选中的文字在**注入的正文里**用 ⟦SELECTED n=K⟧…⟦/SELECTED⟧ 标了出来（编号同上），卡片则是正文里原有的 ⟦CARD_START n=… id=…⟧ —— 要一字不差的原文，**先在正文里按标记取**，这是最省的一条路。正文里找不到（不在本页、或本页正文这次没给）才调 reader_context_snapshot 按编号取。正文有时会写着「某段刚才已经给过」——那是本轮对话里更早给过的同一段，往上翻就有，别为此调工具。页上的卡片**连内容带 id 就嵌在正文里**（⟦CARD_START n=… id=… revision=… label=…⟧…⟦CARD_END⟧），所以绝大多数时候根本不必查卡片：要改哪张、要引用哪张，直接从正文里按 id 取。真要单独取一张就用 reader_page_card_read 按 id 取；**不要用 reader_page_cards 把整页倒出来**（一次几千字，而且同一轮里读第二遍毫无新信息）。同一轮内已经读过的东西不要再读一遍。什么时候必须取全文：拿原文去定位的活（做卡 bind、钉卡、按文字建便签）。什么时候不用取：划线选区直接 at={\"selection\":true}；委派过来的话里已经带了内容且够用；只是回答、概括、判断这类不落到原文上的事。别为了「确认一下」白跑一趟工具。工具：reader_highlight_range 划线（选区用 at={\"selection\":true}，别处用 at={block,text}）；reader_card 做卡/钉卡（bind 直接写 {kind:\"page-chars\",page,text:<原文>}）；reader_anki_draft 做 Anki 卡（它要的 nodeIds 用 kj_node_ensure 一步拿到：按名称找，有就复用、没有就新建，不要自己跑脚本分两步）；reader_note_create / reader_note_edit 便签；reader_visual_image 看页面或笔迹；reader_page_text 读别的页；reader_command / reader_browser_control 翻页与浏览；reader_capability_guide 查能力细节。要某个工具的参数表就调它、一次传一个工具名 —— **绝不要在代码模式里把 ALL_TOOLS 或它的子集整个序列化出来**：2026-09-17 实测一次这样的调用吐了 23129 个 token（截断后仍有 39380 字），而这些全是不走缓存的新增输入。真要在 ALL_TOOLS 里找，只打印名字，别带 description 和 schema。做事就直接调工具，不要只口头描述。做事的时候不要输出「我先读取这页」「我核对一下」这类中间说明，工具调完直接给最终结果；一轮只说一次。语音工具：voice_say 立刻念一句、voice_tell 塞进语音上下文、voice_session_start 开语音、voice_session_stop 挂断（默认等念完）。以「【快板】」开头的 developer 条目是阅读器推送的状态，不是用户发言，不必回应；以「【用户打字】」开头的是用户在侧栏打的字，按用户发言处理。要在指定时间打电话提醒他（起床、关火、出门）：schedule_create，schedule 用 {type:once, at:本地时间 ISO}，steps 只要一步 {id:'ring', deliver:{mode:'call', title:'一句话', text:'接通后念的话'}}；现在就要打用 voice_call。电话会真的响铃并把 iPad 切到前台，只用于必须马上知道的事，普通提醒用 deliver mode=notify。收到「【定时提醒到期】」「【通知】」时，需要用户马上知道的用 voice_session_start + voice_say 说出来。通话的开与关由你负责：说完且不需要回复就 voice_session_stop；用户告别或要求关语音也由你调它。要把一段跑通的多步流程固化成可复用的能力（用户说「存成工具」「以后都这么做」「做个自动的」）：**一律用既有的 flow 格式 bw-reader-skill-flow/1，不许另起炉灶**。一份 flow.json 里写 steps（每步恰好是 command / tool / needs_ai / deliver 之一）、用 {\"$from\": 步骤id, \"path\": …} 引用更早步骤的输出（不能引用更晚的），再加一段描述头：name / when（什么时候用）/ does（能做到什么）/ params（参数接口）。写完必须跑 skill_kit/bw_skill_build.py 用真实轨迹校验 + 干跑，**过了才算做完**；没过就改到过，不要交一个没验证的说明文档。这样做的理由：同一份 flow 会被自动脚本转成 skill 或 MCP 工具、被定时任务直接按步跑、并经 reader_flow_progress 在侧栏画进度点 —— 自己发明的格式这三样一样都接不上。",
    # 会话开始时给后台模型的 developer 指令：通话由它管生死
    "backendStartInstructions": (
        "语音会话已开始。你有 voice_core 工具：voice_status / voice_say / voice_tell / voice_session_stop / voice_session_start。"
        "通话的开与关由你负责：用户告别或要求结束、事情已经办完且不需要再听回复、提醒已送达且用户没有接话、长时间无人说话——"
        "这些情况都应主动调用 voice_session_stop（默认等当前那句念完再挂）。语音模型自己没有关闭通话的能力，它说'已关闭'不算数。"
    ),
}
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
            self.rate = int(sd.query_devices(idx)["default_samplerate"])
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
        self.drops = 0           # 队列满丢掉的 20 ms 块
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
                self.drops += 1
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
        default_rate = int(sd.query_devices(idx)["default_samplerate"] or 48000)
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

    def __init__(self, exe: str, mcp_disable: list[str], on_notification):
        self.exe = exe
        self.mcp_disable = mcp_disable
        self.on_notification = on_notification
        self.pending: dict[int, asyncio.Future] = {}
        self.count = 0
        self.proc = None
        self.stderr_tail: deque = deque(maxlen=50)

    async def launch(self):
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")}
        # 2026-09-16：给语音这条线程封存用不到的 Codex 自带样板。实测后台线程里最大的一条
        # developer 消息 43,010 字，我们自己的指令只占 1,157 字；其余是 Memory 说明（16,569）
        # 与 Skills 目录（21,212，其中绝大多数是插件带的"做 KPI 报表/市场规模估算"这类条目）。
        # 关掉后每轮约省 25,600 字 —— 作为对照，注入瘦身一整天省的是 400 字/次。
        # ⚠ 用 -c 按次覆盖，**不动 config.toml**：用户别处的 Codex 照常拥有这些功能。
        args = [self.exe, "-c", 'forced_login_method="chatgpt"', "-c", "features.memories=false"]
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
        self._voice_stream = ""
        self._backend_recent: tuple[float, str] | None = None   # 后台最近一条回复：语音把它念出来的字幕不再重复入库
        self._voice_user_acc = ""   # 本轮用户字幕分段累积（turn.done 没带转写时兜底）
        self._voice_turn_commentary = False   # 这一轮语音回复是委托后台期间/之后的过渡或转述 → 不单独入库
        self._loop_lag_max = 0.0
        self._loop_lag_over = 0
        self._audio_stats_at = 0.0
        self._thread_cleared = False          # /thread/new：下次 ensure_app 不续接旧线程
        self._voip_call_active = False        # 我们拨出去且已接通的 VoIP 电话还在（CallKit 那层）：挂媒体会话时要一并请 App 挂断
        self._thread_resume_target = None     # /thread/resume：下次 ensure_app 续接这个线程
        self._backend_done_at = 0.0
        # 上下文注入器状态：快照修订/页面停留起点/各 sink 已投指纹
        self._ctx = {"mtime": 0.0, "rev": None, "page_key": "", "page_since": 0.0, "snap": None,
                     "fp": {"backend_state": "", "backend_text": "", "voice": "", "image": ""}, "debounce": None,
                     # 正文记账：{(file, 页号): (\"full\"|\"part\", 时刻)} —— 见 _ctx_text_ledger
                     "sent_pages": {}}
        self._history_q: queue.Queue = queue.Queue()
        self._stream_latest: dict[str, str] = {}
        self._stream_queued: set[str] = set()
        threading.Thread(target=self._history_worker, name="history-writer", daemon=True).start()

    # ---------- 设置 ----------
    def load_settings(self) -> dict:
        s = dict(DEFAULTS)
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
        return s

    def save_settings(self):
        SETTINGS_PATH.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")

    async def update_settings(self, patch: dict) -> dict:
        changed = {k: v for k, v in patch.items() if k in DEFAULTS and self.settings.get(k) != v}
        self.settings.update(changed)
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
                if self._subtitle_mode():
                    self._subtitle_done(role, text)
                # 历史按"轮"写（数据通道 turn.done 带整轮转写），这里的分段只累积：一句话会拆成好几段，
                # 用户插话时更是交错到达 —— 按段写就是 2026-09-14 那种碎片对话（用户实测）。
                elif role == "user" and text:
                    self._voice_user_acc = (self._voice_user_acc + " " + text).strip()
            elif m == "thread/realtime/transcript/delta":
                if p.get("role") == "assistant" and p.get("delta"):
                    if self._voice_turn_id is None:
                        self._voice_turn_id = "v-" + str(int(time.time() * 1000))[-12:]
                    self._voice_stream += str(p.get("delta"))
                    if self._subtitle_mode() or (not self._backend_speaking_likely() and not self._voice_turn_commentary):
                        self._stream_post(self._voice_turn_id, self._voice_stream)
            elif m in ("turn/started", "turn/completed"):
                self.backend_busy = m == "turn/started"
                turn = p.get("turn") or {}
                self.log(m, turnId=turn.get("id"), status=turn.get("status"))
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
                if self._turn is not None and p.get("delta") and not self._voice_owns_text():
                    self._turn["stream"] += str(p.get("delta"))
                    self._stream_post(self._turn["id"], self._turn["stream"])
            elif m in ("item/started", "item/completed"):
                item = p.get("item") or {}
                t = item.get("type")
                if t in ("agentMessage", "mcpToolCall", "webSearch", "commandExecution", "fileChange", "reasoning"):
                    self.log(m, itemType=t, tool=item.get("tool") or item.get("name"), status=item.get("status"),
                             text=(item.get("text") or item.get("query") or item.get("command") or "")[:160] or None)
                if m == "item/started" and t == "agentMessage" and self._turn is not None:
                    self._turn["stream"] = ""   # 一轮里可能有多条 agentMessage（先说"我看一下"再正答）：草稿只显示当前这条
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
            self.log("dc_" + t.replace(".", "_"), role=turn.get("role"), transcript=(turn.get("transcript") or "")[:80])
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
                    asyncio.run_coroutine_threadsafe(self._ctx_on_delegation(), self.loop)
            # 委托这条留全：它是**两个模型之间的完整交接报文**（语音模型转给后台的原话、
            # handoff_id、target），也是链路上「何时召唤后台、交了什么过去」的唯一来源。
            # 其余事件仍截 200 字，免得把 events.jsonl 撑大。
            cap = 4000 if t == "delegation.created" else 200
            self.log("dc_" + t.replace(".", "_"), payload=json.dumps(d, ensure_ascii=False)[:cap])

    # ---------- app-server / 线程 ----------
    async def ensure_app(self):
        if self.app is None:
            exe = self.settings.get("codexExe") or "codex.exe"
            self.app = AppServer(exe, list(self.settings.get("mcpDisable") or []), self.on_notification)
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
            self._thread_cleared = False
            if want:
                try:
                    self._ctx_invalidate()
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
            self._ctx_invalidate()
            r = await self.app.call("thread/start", start, timeout=90)
            self.thread_id = r["thread"]["id"]
            self.log("thread_started", threadId=self.thread_id)
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
        sel_items = []
        for it in (snap.get("selectedItems") or []):
            t = str(it.get("text") or it.get("what") or "").strip()
            if not t:
                continue
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
                "".join("（%d）%s%s「%s…」%s" % (
                            i + 1, _SEL_KIND_LABEL.get(k, "内容"),
                            ("〔%s〕" % lb) if lb else "",
                            t[:sel_chars],
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
        ink_hint = ""
        ink_hint_voice = ""
        if vis.get("has_ink") or vis.get("drawing"):
            ink_hint = "。本页有笔迹/圈画（%s）；他提到圈画、手写、算式时后台用 reader_visual_image {scope: drawing-nearby} 看真图" % str(vis.get("drawing") or "有笔迹")[:120]
            ink_hint_voice = "。他在这页上有圈画/手写，你看不到图；他问「这是什么/这里/圈的这个」时**必须立刻委派后台去看图**，不要自己猜、不要只说稍等"
        # 正文：停留窗内才带；但页面被"激活"（有选区，或最近一次动作不是翻页而是在这页上选中/画/操作）时不等 8 秒（用户 2026-09-14）
        dwell = time.time() - (self._ctx["page_since"] or time.time())
        dwell_max = float(s.get("contextDwellMaxSeconds") or 720)
        activated = bool(sel_text) or bool(
            acts and str(acts[-1].get("kind") or "") not in ("page-turn", "")
            and float(acts[-1].get("secondsAgo") or 0) <= dwell_max
        ) or bool(vis.get("has_ink"))
        dwell_ok = (float(s.get("contextDwellMinSeconds") or 8) <= dwell <= dwell_max) or (activated and dwell <= dwell_max)
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
        # 带时刻：旧的删不掉（inject_items 只能追加），所以让它认得出哪条最新。
        state = ("【当前阅读状态 " + time.strftime("%H:%M:%S") + "】只认时刻最新的一条，更早的全部作废；"
                 "这是状态记录不是提问，不要回应本条。" + where + sel_hint + act_hint + ink_hint + "。")
        last_act = str((acts[-1].get("what") or acts[-1].get("kind") or "") if acts else "")[:40]
        fp_state = "%s|%s|%s|%s|%s" % (self._ctx["page_key"], sel_text[:60],
                                       "".join(k + t[:20] for k, t, _r, _l in sel_items), last_act, bool(vis.get("has_ink")))
        fp_text = "%s|%d|%s" % (self._ctx["page_key"], len(text), text[:30]) if text else ""
        # 语音侧预算只截正文，位置/选区提示和结尾的静默约定必须完整保留（否则正文一长就把「不要回应本条」切掉了）
        vbudget = int(s.get("contextVoiceChars") or 700)
        head = "(" + where + sel_hint_voice + ink_hint_voice + act_hint
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

    def _ctx_ink_fingerprint(self, snap: dict) -> str:
        """页上有"最近新画、且已稳定"的笔迹 → 返回 (页, 笔迹版本) 指纹；否则空串。"""
        if not self.settings.get("contextInkImage", True):
            return ""
        cp = snap.get("currentPage") or {}
        vis = cp.get("visual") or {}
        dr = vis.get("drawing") or {}
        if not isinstance(dr, dict) or not dr.get("stable") or dr.get("inProgress") or dr.get("empty") or not dr.get("drawingRevision"):
            return ""
        try:
            edited = float(dr.get("lastEditedAt") or 0)
            window = float(dr.get("freshWindowS") or 120)
        except (TypeError, ValueError):
            return ""
        if edited <= 0 or time.time() - edited > window:
            return ""
        return "%s|%s" % (self._ctx["page_key"], dr.get("drawingRevision"))

    async def _ctx_fetch_ink_image(self) -> dict | None:
        """向桥要圈画附近的合成图（桥复用 reader_visual_image 同一条取图路）。失败/太大 → None。"""
        def work():
            import urllib.request
            req = urllib.request.Request(BRIDGE_URL + "/voice-core/visual-image", data=json.dumps({"scope": "drawing-nearby"}).encode("utf-8"),
                                         method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read() or b"{}")
        try:
            r = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as e:   # noqa: BLE001
            self.log("ctx_image_error", message=clean(e))
            return None
        if not r.get("ok"):
            self.log("ctx_image_skip", reason=r.get("reason"), message=(r.get("message") or "")[:120])
            return None
        limit = int(self.settings.get("contextInkImageMaxBytes") or 700000)
        if int(r.get("bytes") or 0) > limit:
            self.log("ctx_image_skip", reason="too-large", bytes=r.get("bytes"), limit=limit)
            return None
        return r

    def _ctx_thread_scope(self):
        """去重记账只在**当前这条线程**里有效。线程一换（清空对话 / resume 到别的线程），
        整份作废 —— 新线程里那些内容根本不在，再说"刚才给过"就是指着空处让它去翻。

        自检式而不是"换线程的地方记得清"：换线程有三处以上，而"记得"正是这条链上
        今天已经栽过两次的东西（chip 到期没人上报、跳过正文不出声）。
        """
        if self._ctx.get("thread") == self.thread_id:
            return
        self._ctx["thread"] = self.thread_id
        self._ctx["fp"] = {"backend_state": "", "backend_text": "", "voice": "", "image": "", "sel": ""}
        self._ctx["sent_pages"] = {}
        self.log("ctx_scope_reset", threadId=(self.thread_id or "")[-12:])

    async def _ctx_inject_backend(self, with_text: bool) -> bool:
        """后台线程：状态变了投状态；开口边沿且正文指纹没投过再投正文（inject_items：零成本、latest wins）。"""
        if not self.settings.get("contextInjectEnabled", True) or not (self.app and self.thread_id):
            return False
        self._ctx_thread_scope()
        snap = self._ctx_snapshot()
        if not snap:
            return False
        b = self._ctx_build(snap)
        if not b.get("state"):
            return False
        fp = self._ctx["fp"]
        body = None
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
        # 新笔迹的图：只在开口边沿（with_text）考虑；同一 (页, drawingRevision) 只投一次 —— 相同或相邻轮次不重复
        image = None
        ink_fp = self._ctx_ink_fingerprint(snap) if with_text else ""
        if ink_fp and fp["image"] != ink_fp:
            image = await self._ctx_fetch_ink_image()
            if image is None:
                fp["image"] = ink_fp   # 取不到就算了，别每次开口都再试同一版笔迹
        if body is None and image is None:
            return False
        note = "（下图是他刚在这页圈画/手写的部分，带页面上下文；他问「这个/这里/圈的」就指它。）"
        text_part = (body if body is not None else b["state"]) + ((chr(10) + note) if image is not None else "")
        content = [{"type": "input_text", "text": text_part}]
        if image is not None:
            content.append({"type": "input_image", "image_url": "data:%s;base64,%s" % (image["mimeType"], image["base64"]), "detail": "auto"})

        # 后台正在跑的那一轮读不到我们现在追加的东西（它的上下文早就组好了）。
        # 所以忙碌时**先不注入**，把最新一份压在这里，等那轮结束再送 —— 见 _ctx_flush_pending。
        # 这样用户在 AI 干活期间连改三次选中，历史里也只落最新的一条，
        # 而不是三条（其中两条一生下来就是过期的）。
        if self.backend_busy:
            self._ctx_pending = {"content": content, "fp_state": b["fp_state"],
                                 "fp_text": b["fp_text"] if (with_text and b["text"]) else None,
                                 "ink": ink_fp if image is not None else None,
                                 "chars": len(body or ""), "at": time.time()}
            # 想让在跑的那一轮也看见，只有 turn/steer 一条路；默认关着，原因见 steer_running_turn
            if self._turn:
                await self.steer_running_turn(
                    "【状态更新·不是新任务】" + b["state"] +
                    chr(10) + "继续完成你手上的事；后面用到「选中/当前页」时以这条为准。", tag="ctx")
            self.log("ctx_backend_deferred", chars=len(body or ""), page=self._ctx["page_key"][-40:])
            return True
        try:
            await self.app.call("thread/inject_items", {"threadId": self.thread_id, "items": [
                {"type": "message", "role": "developer", "content": content}]}, timeout=30)
        except Exception as e:
            self.log("ctx_backend_error", message=clean(e))
            return False
        fp["backend_state"] = b["fp_state"]
        if with_text and b["text"]:
            fp["backend_text"] = b["fp_text"]
        if image is not None:
            fp["image"] = ink_fp
            self.log("ctx_image", bytes=image.get("bytes"), drawingRevision=image.get("drawingRevision"), page=self._ctx["page_key"][-40:])
        self.log("ctx_backend", withText=bool(with_text and b["text"]), chars=len(body),
                 page=self._ctx["page_key"][-40:], body=self._log_body(text_part))
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
        if pend.get("ink"):
            fp["image"] = pend["ink"]
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

    async def steer_running_turn(self, text: str, tag: str = "state") -> dict:
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
            await self.app.call("turn/steer", {"threadId": self.thread_id,
                                               "expectedTurnId": turn["id"],
                                               "input": [{"type": "text", "text": text}]}, timeout=20)
        except Exception as e:   # noqa: BLE001
            self.log("turn_steer_error", message=clean(e), tag=tag)
            return {"ok": False, "error": clean(e)}
        self.log("turn_steer", tag=tag, chars=len(text), turnId=str(turn["id"])[-12:])
        return {"ok": True, "turnId": turn["id"], "chars": len(text)}

    async def _ctx_on_delegation(self):
        """后台真的开工了 —— 这一刻才把状态送进去，而且是送进**正在跑的那一轮**。

        这是 2026-09-17 用户提的形态：有了中途插入，就不必「一有变化就注入」，
        只在真正需要时插一次。

        为什么现在才做得到：早先试过在 delegation.created 那一刻 inject_items，
        只有 8% 赶得上 —— 因为那是在**跟轮的启动赛跑**（inject 要 80 ms，
        而 delegation → turn/started 中位只有 38 ms）。turn/steer 不参加这场赛跑：
        它挂的是已经在跑的轮，等轮起来之后再插也来得及。
        受控实验 16 轮：0.5/2/5 秒三档插入全部被采纳，一次没打哑。

        好处是「只在后台干活时注入」：477 句开口里只有 262 句真的委托，
        剩下 45% 的纯聊天现在零注入；而且内容是这一刻现算的，永远最新。

        赶不上（轮已经结束 / 还没起来）就退回 inject_items —— 迟到而不是丢失。
        """
        if not self.settings.get("contextInjectEnabled", True):
            return
        try:
            if str(self.settings.get("contextInjectOn") or "delegationSteer") != "delegationSteer":
                await self._ctx_inject_backend(with_text=True)
                return
            # 等这一轮真的起来：delegation 之后 22~60 ms 才 turn/started，给它一点时间
            deadline = time.monotonic() + float(self.settings.get("steerWaitSeconds") or 3.0)
            while time.monotonic() < deadline:
                if self.backend_busy and self._turn and self._turn.get("id"):
                    break
                await asyncio.sleep(0.05)
            snap = self._ctx_snapshot()
            body = self._ctx_build(snap) if snap else None
            state = (body or {}).get("state") or ""
            if not state:
                return
            res = await self.steer_running_turn(
                "【当前阅读状态·状态记录，不是提问】" + state +
                chr(10) + "继续完成手上的事；用到「选中/当前页」时以这条为准。", tag="delegation")
            if res.get("ok"):
                # 指纹跟着走，免得同一状态在下一次委托里再送一遍
                self._ctx["fp"]["backend_state"] = body.get("fp_state")
                self.log("ctx_steer", chars=len(state), page=self._ctx["page_key"][-40:],
                         body=self._log_body(state))
                return
            # 轮没起来或已经结束 —— 退回追加，被下一轮读到
            self.log("ctx_steer_fallback", reason=str(res.get("error"))[:80])
            await self._ctx_inject_backend(with_text=True)
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

    async def say(self, text: str):
        self.mark_activity("say")
        await self.app.call("thread/realtime/appendSpeech", {"threadId": self.thread_id, "text": text})
        self.pending_speech_until = time.monotonic() + 8
        self.log("say", text=text[:200])
        return {"ok": True}

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

    async def typed(self, text: str) -> dict:
        """侧栏输入框打的字（桥 codex-type → 这里）。语音在线：追加进语音会话，v3 空闲时会自动起一轮、
        由语音模型开口回答；不在线：交给后台文字线程起一轮（它有 voice_* 工具，要出声自己开）。"""
        self.mark_activity("typed")
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        if self.session_state == "connected" and self.app and self.thread_id:
            # 打字和说话是同一件事的两种输入方式：开口时会在边沿刷新状态，打字也必须刷。
            # 不刷的后果是模型拿上一轮的旧状态回答"我现在选中了什么"（2026-09-15 实录：
            # 文字选中早就到期消失了，它还在说三项）。
            await self._ctx_on_speech()
            payload = (self.settings.get("typedPrefix") or "") + text
            await self.app.call("thread/realtime/appendText", {"threadId": self.thread_id, "text": payload, "role": "user"}, timeout=15)
            self._voice_pending_user = (time.time(), text)
            self.log("typed", via="voice", text=text[:200], userSpeaking=self.user_speaking)
            return {"ok": True, "via": "voice"}
        await self.turn(text)
        self.log("typed", via="backend", text=text[:200])
        return {"ok": True, "via": "backend"}

    async def turn(self, text: str, additional: dict | None = None, record_user: bool = True):
        await self.ensure_app()
        await self._ctx_inject_backend(with_text=True)   # 直接少一轮工具调用：起轮前把他正看着的内容放进去
        self._pending_turn_user = text if record_user else None
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
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

    def _turn_item(self, item: dict):
        rec = self._turn
        t = item.get("type")
        if t == "userMessage":
            if not rec.get("user") and not rec.get("user_posted"):
                txt = " ".join(str(c.get("text") or "") for c in (item.get("content") or []) if isinstance(c, dict))
                rec["user"] = self._clean_user_text(txt) or None
        elif t == "agentMessage":
            txt = item.get("text") or ""
            if txt and item.get("phase") in (None, "final_answer"):
                rec["assistant"] = txt
                self._backend_recent = (time.time(), txt)
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
            part = {"kind": "tool", "tool": label[:160], "label": (label + (" · " + status if status else ""))[:320]}
            if isinstance(args, dict) and args:
                aj = json.dumps(args, ensure_ascii=False)
                part["args"] = args if len(aj) <= 2000 else {"_truncated": aj[:2000]}
            if brief:
                part["result"] = brief[:2000]
            ms = item.get("durationMs")
            if isinstance(ms, (int, float)) and not isinstance(ms, bool) and 0 <= ms <= 86_400_000:
                part["ms"] = int(ms)
            if len(rec["parts"]) < 24:
                rec["parts"].append(part)
            # 结果卡不再由运行器代造（2026-09-15 根治）：App 自己画的部件直接 upsert 进同一条记录。
            failed = status in ("failed", "error") or (isinstance(err, dict) and bool(err.get("message"))) or bool(re.match(r'\s*\{\s*"ok"\s*:\s*false', brief or ""))
            if failed:
                self._tool_error_log(rec.get("id"), label, args, brief, status or "failed")

    def _finish_turn(self, turn: dict):
        rec, self._turn = self._turn, None
        self._backend_done_at = time.time()
        if not rec:
            return
        user = None if rec.get("user_posted") else rec.get("user")
        assistant = rec.get("assistant")
        if self._voice_owns_text():
            # 字幕模式 + 语音在线：文字由语音念出来（字幕里已有），这一轮只留工具/卡片；没有就不写
            user, assistant = None, None
            if not rec["parts"]:
                self.log("history_skip_backend_text", turnId=rec["id"][:40])
                return
        if not (user or assistant or rec["parts"]):
            return
        body = {"user": user or "", "assistant": assistant or "", "via": "codex-voice", "turn_id": rec["id"][:40]}
        if rec["parts"]:
            body["parts"] = rec["parts"]
        dur = turn.get("durationMs")
        if isinstance(dur, (int, float)) and not isinstance(dur, bool) and 0 <= dur <= 86_400_000:
            body["took_ms"] = int(dur)
        self._history_post(body)

    def _subtitle_mode(self) -> bool:
        return str(self.settings.get("historyMode") or "subtitle") != "turns"

    def _voice_owns_text(self) -> bool:
        """字幕模式且语音在线：对话文字以字幕为准，后台轮不写正文、不流式正文。"""
        return self._subtitle_mode() and self.session_state == "connected"

    def _subtitle_done(self, role, text):
        """字幕模式：一条 transcript/done 就是一条聊天记录。用户句 <id>.u，回复 <id>，同一轮共用 id
        （id 在数据通道 turn.created 时分配；回复落库后归零，下一轮再分配）。"""
        text = str(text or "").strip()
        if not text:
            return
        if role == "user":
            tid = self._voice_turn_id or ("v-" + str(int(time.time() * 1000))[-12:])
            self._voice_turn_id = tid
            self._history_post({"user": text, "via": "voice", "turn_id": tid + ".u"})
        elif role == "assistant":
            tid = self._voice_turn_id or ("v-" + str(int(time.time() * 1000))[-12:])
            self._voice_turn_id = None
            self._voice_stream = ""
            if not re.search(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text):
                self.log("history_skip_punct", text=text[:20])   # 「。」这种纯标点回复不记
                return
            self._history_post({"assistant": text, "via": "voice", "turn_id": tid})

    def _history_enabled(self) -> str:
        url = str(self.settings.get("historyUrl") or "").rstrip("/")
        return url if url and self.settings.get("historyEnabled", True) else ""

    def _history_post(self, body: dict):
        if not self._history_enabled():
            return
        if self.thread_id:
            body.setdefault("thread_id", self.thread_id)
        self._history_q.put(("log", body))

    def _stream_start_post(self, turn_id: str):
        """后台轮开始：把真实 turn id 推给侧栏（SSE stream:"start"）。"""
        if not self._history_enabled() or not turn_id:
            return
        self._history_q.put(("stream_start", turn_id))

    def _tool_error_log(self, turn_id, tool: str, args, brief: str, status: str):
        """工具调用出错 → voice-cli/tool-errors.jsonl（用户 2026-09-15：自动记下来，我自己去分析）。"""
        try:
            row = {"t": round(time.time(), 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "turnId": str(turn_id or "")[:40], "tool": tool[:120],
                   "status": status, "args": (json.dumps(args, ensure_ascii=False) if args is not None else "")[:1500], "result": str(brief or "")[:1500]}
            with (BASE / "tool-errors.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + chr(10))
            self.log("tool_error", tool=tool[:120], status=status, result=str(brief or "")[:200])
        except Exception:
            pass

    def _stream_post(self, turn_id: str, text: str):
        """流式草稿：只保留每轮最新全文，队列里同一轮最多挂一条（到达节奏快于发送节奏时自然合并）。"""
        if not self._history_enabled() or not turn_id:
            return
        self._stream_latest[turn_id] = text
        if turn_id not in self._stream_queued:
            self._stream_queued.add(turn_id)
            self._history_q.put(("stream", turn_id))

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
                    self._history_request("/api/assistant/stream", {"turn_id": payload, "stream": "start"})
                elif kind == "stream":
                    # 节流：草稿最快每 0.25 s 一条；发的时候取该轮最新全文
                    wait = 0.25 - (time.monotonic() - last_stream_at)
                    if wait > 0:
                        time.sleep(wait)
                    self._stream_queued.discard(payload)
                    text = self._stream_latest.get(payload, "")
                    self._history_request("/api/assistant/stream", {"turn_id": payload, "content": text[:8000]})
                    last_stream_at = time.monotonic()
                    self.history_stats["streamed"] += 1
                else:
                    self._stream_latest.pop(str(payload.get("turn_id") or ""), None)
                    r = self._history_request("/api/assistant/log", payload)
                    self.history_stats["written"] += 1
                    via, tid, n, up = payload.get("via"), payload.get("turn_id"), r.get("n"), r.get("upserted")
                    self.loop.call_soon_threadsafe(lambda: self.log("history_written", via=via, turnId=tid, n=n, upserted=up))
            except urllib.error.HTTPError as e:
                detail, code = "", e.code
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                self.history_stats["errors"] += 1
                self.history_stats["lastError"] = "HTTP %s %s" % (code, detail)
                self.loop.call_soon_threadsafe(lambda: self.log("history_error", kind=kind, status=code, detail=detail))
            except Exception as e:
                message = clean(e)
                self.history_stats["errors"] += 1
                self.history_stats["lastError"] = message
                self.loop.call_soon_threadsafe(lambda: self.log("history_error", kind=kind, message=message))
    def audio_stats(self) -> dict:
        return {
            "speakerGaps": self.speaker.gaps if self.speaker else None,
            "speakerUnderruns": self.speaker.underruns if self.speaker else None,
            "speakerFlags": self.speaker.status_flags if self.speaker else None,
            "speakerBufferedMs": round(len(self.speaker.buf) / 2 / self.speaker.out_rate * 1000) if self.speaker else None,
            "micDrops": self.mic.drops if self.mic else None,
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
            "context": {"pageKey": self._ctx["page_key"], "dwellSeconds": round(time.time() - self._ctx["page_since"]) if self._ctx["page_since"] else None,
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
            if u.path == "/thread/new":
                async def _renew():
                    if r.session_state != "idle":
                        await r.session_stop("thread-renew")
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
                return self._send(200, self._run(r.say(str(body.get("text") or ""))))
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
                return self._send(200, self._run(r.typed(str(body.get("text") or "")), 60))
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


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    loop = asyncio.new_event_loop()
    runner = Runner(loop)
    Handler.runner = runner
    try:
        httpd = ThreadingHTTPServer(LISTEN, Handler)
    except OSError as e:
        print(json.dumps({"kind": "bind_failed", "message": str(e)}), flush=True)
        return 2
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    runner.log("runner_started", listen=f"http://{LISTEN[0]}:{LISTEN[1]}", settings=str(SETTINGS_PATH))
    runner.write_bridge_flag(True)   # 运行器在 = 外部语音后端在：桥把 App 的 START/STOP 交给我们

    async def boot():
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
