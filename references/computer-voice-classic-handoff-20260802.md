# 电脑客户端语音：Codex 已通 / Classic 上行未通（2026-08-02 JST）

> 本文接续 `claude-computer-client-full-handoff-20260802.md`。
> 当天由 Claude 接手 GPT Classic，Codex 已恢复可用，剩两处生命周期问题未修。

## 0. 当前可用性

| 目标 | 状态 |
|---|---|
| Codex | ✅ **可正常通话**（用户 2026-08-02 实测确认） |
| GPT Classic | ⚠ 语音能唤起，但**无法实际对话**（上行无数据） |

已安装：`0.1.45`，EXE SHA `FB04697A78CF5CC3BA18C8CED1781EEDAC72D74C9DFEB841BCAC3C065AE236E5`
回滚点（黄金 Codex-only）：`C:\Users\bwica\bw-computer-voice-bridge-backups\claude-rollback-goldenCodex-20260802`，SHA `9ED80E75…`

## 1. 今天修掉的四层（按发现顺序）

### ① Swift 改动写进了死副本 —— 最大的时间黑洞

真实 iCloud Drive 是 **`C:\iCloudDrive`**（大写 D），
`C:\icloud\iCloudDrive` 是孤立残骸（旁边有 `C:\Users\bwica\iCloud-repair-backup`）。
此前两天所有 Swift 改动都写进了残骸，**从未到达 iPad**。
Playgrounds 实际目录：`C:\iCloudDrive\iCloud~com~apple~Playgrounds\BWReaderNative.swiftpm`
（Codex 已把新版同步到该目录，16 文件 SHA 一致）。

⚠ 旧交接文档里 `C:\icloud\iCloudDrive\BWReaderNative.swiftpm` 这个路径**是错的**。

### ② 探测器硬编码进程名（0.1.41）

`WindowsCodexAppProbe.Probe` 曾写死 `GetProcessesByName("ChatGPT")` 与
`path.EndsWith(@"\ChatGPT.exe")`。本机实测：

```
Codex    进程名 "ChatGPT"          映像 ChatGPT.exe          包 OpenAI.Codex_
Classic  进程名 "ChatGPT Classic"  映像 ChatGPT Classic.exe  包 OpenAI.ChatGPT-Desktop_
```

Classic 的进程一个都通不过筛选 → `ReadyTarget=null` → `APP_READY_TIMEOUT`，
**它的窗口从未被探测到**。修法：进程名/映像名收进 `DirectAppTargetProfile`
（新增 `ProcessName` / `ExecutableSuffix`），Probe 按目标取值。

⚠ 旧文档"两者 exe 名都可能是 ChatGPT.exe"的假设与实测相反。

### ③ `MEDIA_START_UNCONFIRMED` 是空壳（0.1.43）

`DirectBridgeAdapters.cs` 的四条件检查共用一个 code，且
`DirectRuntimeStatus` **只序列化 `Stage` 不落 message**，所以真因看不见。
更关键：`_mediaAdapter.Completion` 类型是 `Task<DirectProtocolException?>`，
**真实异常一直装在 `Result` 里**（972 行 `TrySetResult(failure)`），
而检查只读 `IsCompleted` 把 Result 丢了。
修法：`IsCompleted` 时取出 `Result` 直接抛；另用 stage 区分四种情况
（`media-start.host-not-ready` / `.started-capture-inactive` /
`.adapter-capture-inactive` / `.adapter-completed`）。

### ④ PCM 启动窗口只有 200ms（0.1.45）—— Codex 可用的关键

`DirectPcmStartGate.FramesPerTrack` 原为 `PcmQueueLimitMilliseconds(200ms)/20 = 10 帧`。
START 回执发出前，目标应用输出的音频缓冲于此。当本次 START 需要**先发快捷键唤起语音**时，
等待应用响应 + voice activity 确认远超 200ms → 缓冲撑爆 → `PCM_START_GATE_FULL` →
**杀掉整个通话**，表现为"语音刚起来就被关、App 按钮变绿一两秒又灭"。

用户的关键对照实验定位了它：**语音本来就开着时点击成功**（不必发快捷键、流程够快），
**语音关着时点击必失败**。

修法：窗口放大到 15 秒（`StartGateWindowMilliseconds = 15_000`）。

⚠ **不能靠丢帧化解**——下行帧要过 `DirectPcmSequenceGuard`，序列必须严格 +1，
丢一帧就是 `PCM_SEQUENCE_INVALID`。我在 0.1.44 试过丢帧，直接引入回归，已撤回。

## 2. 剩余两处未修（下一步的全部工作）

### A. Classic 无法对话 —— 上行 render session 提前 `_stopped`

实测证据：

```
14:58:33.854  state=active capture=True          ← 通话建立
14:58:37.513  faulted | UPLINK_NOT_ACTIVE  stage=uplink-rejected   ← 4 秒后上行死
```

`VirtualMicrophoneRenderSession.cs:73-77`：`_stopped` 为真时上行帧一律拒。
`_stopped` 只在 `StopAndClear()`(120 行) 置位，调用点两处：
- `:773` 正常停止路径
- `:861` runtime `Dispose()` 之后的终止路径（此处有 `terminalError`）

**推断（未验证）**：861 那条路径先跑了 —— render runtime 线程异常退出，
`terminalError` 被记录但**没有浮到 runtime status**，随后每一帧上行都撞
`UPLINK_NOT_ACTIVE`，把真因盖住。这与 ③ 是同一种"真实异常被表象错误覆盖"的模式。

**下一步**：照 ③ 的做法，让 render session 的 `terminalError` 优先上报
（而不是被后续 `UPLINK_NOT_ACTIVE` 覆盖），点一次就能拿到上行为何死。

用户侧现象佐证：ChatGPT Classic 弹「麦克风已在系统设置/硬件切换中静音
Communications - CABLE Output」。**那是结果不是原因**——桥接器没往 CABLE Input
写数据，CABLE Output 自然一片静默。用户手动把 4 个会话的输入设备全改成
CABLE Output **无效**，已排除 per-app 路由方向问题。

### B. App 挂断关不掉电脑端语音

用户明确：**是桥接器自己唤起的语音**，所以不是 `OwnsVoice` 保护的预期行为。

日志里每次 faulted 都伴随 `MEDIA_STOP_FAILED`（`WindowsDirectAdapters.cs:2060`），
那是清理链的**失败聚合码**。`StopOwnedVoiceAsync`（发停止快捷键）在清理链
lambda 里（:1453），前面还有 voiceMonitorLifetime 等步骤。

需要确认的分支（`WindowsDirectAdapters.cs:1304-1340`）：
- `confirmation is null && baseline is null` → 直接 return，不发快捷键
- `!confirmation.OwnsVoice` → 抛 `VOICE_OWNERSHIP_UNCONFIRMED`，刻意不关
- `!plan.VoiceGenerationMatches` → 抛 `VOICE_REPLACED_CLEANUP_PENDING`

**推断（未验证）**：通话 faulted 时 confirmation 可能已被清空/代际不匹配，
导致停止快捷键被这些安全闸拦下。修法方向：faulted 清理路径下，若本次
**确实发过启动快捷键**（`shortcutReceipt` 存在），应保留足够凭据让停止快捷键发出。

## 3. 诊断工具（继续查时直接用）

**旁路监控**（不碰服务，捕获重启前的瞬时错误）：
`C:\Users\bwica\AppData\Local\Temp\claude\<session>\scratchpad\watch-status.ps1`
400ms 轮询 `runtime\computer-voice-direct.status.json`，记录状态跃迁、错误码+stage、
路由 journal、以及**服务进程换代**（那正是 lastError 被清空的时刻）。

**安装脚本**：`install-0.1.4X.ps1`，停任务→停进程→换 7 文件→校验 SHA→清残留
journal→重启任务。⚠ **必须存为 UTF-8 with BOM**，否则 Windows PowerShell 5.1
按 GBK 解码中文直接语法错误。

## 4. 血的教训（这些坑我今天都踩过）

1. **不要用关键词搜索"证明某物不存在"**。同一天翻车三次：查 AI 调用漏了
   `gemini`/`openai`；扫 EXE 用 ASCII 漏掉 UTF-16 字符串常量；把**文件名**当类名搜
   （`ChatGptClassicVoiceAutomation` 是文件名，类叫 `WindowsChatGptClassicVoiceShortcutSender`）。
   **对策**：先拿一个已知存在的正例校准搜索词，正例命中了结论才作数。
   扫 .NET EXE 至少要 ASCII + UTF-16(偏移0) + UTF-16(偏移1) 三种解码。
2. **日志里的错误必须先核对时间戳**晚于当前进程启动时间，否则是陈旧残留。
   我曾拿一条旧 `RENDER_ENDPOINT_UNAVAILABLE` 当现状，差点让用户白重启一次机器。
3. **诊断不能改变被诊断系统的结构**。我停掉 launcher 只留裸 `--direct-serve`，
   造出连接风暴（35 秒 27 次 reader-connect、零 START），比原故障更糟。
   launcher 在握手链路里有实质作用。
4. **`C:\claude` 是共享脏工作区**，读到的前端源码可能落后生产。
   核对生产一律用干净克隆 + `git show`，别信工作区副本。
