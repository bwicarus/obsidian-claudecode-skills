# Codex 主动通知通道（2026-09-10 实测定稿）

> 这一天为这条链绕了十几圈，几乎每一圈都是被一个「看起来合理但错了」的假设带偏的。
> 下面每一条都有实测出处，**动手前先读，别重新推导一遍**。

## 一句话

**通道 = 一条 Codex 的 app-tools 命名管道 + 一个目标线程 id。**
管道可以由我们自己枚举并验证，线程 id 可以按名字或按活跃度挑 ——
所以通道**不需要 Codex 配合、不需要钩子、不需要环境变量**就能建起来。

## 怎么建（现行做法，`codex_channel.py`）

```
1. 枚举   \\.\pipe\ 下的 codex-browser-use-*
2. 验证   逐条问 tools/list，只认返回 send_message_to_thread 的那条
3. 列对话 用 list_threads 拿 (id, title, status, updatedAt)
4. 挑     recent / last-used / title 三种模式（见下）
5. 登记   POST {pipePath, threadId, enabled} 给 /reader-codex-endpoint/v1
```

选择存在**桥 runtime 的 `codex-channel-choice.json`**：服务器设置页与 App
读写同一份（用户 2026-09-10 定：「app和服务器设置页的设置需要是相同的才行」）。

## 六个实测得来的坑

### 1. `CODEX_APP_TOOLS_PIPE_PATH` 只在桌面应用自己的会话进程里

终端接续同一段对话时 **`CODEX_THREAD_ID` 有，管道路径没有**（用
`thread/shellCommand` 列过环境变量）。所以：

- 从 app-server 送指令让 Codex 跑登记脚本 → **必然失败**，试过三次；
- 用户在 App 里打一句话 → 成功（命令跑在 App 的进程里）。

⚠ 我们自己的 `codex_push_register.py` 曾把这件事说成「两个变量都拿不到」——
任一缺失就打两个名字。**那句话把排查方向带偏了整整一轮**，我和 Codex 都被它
绕进去。现在分别报告。这是 `silent-failure-lessons.md` 里「折成布尔前先报
原始值」的反面教材，而且是我们自己写的。

### 2. 管道名每次 Codex 重启都会变

绑定文件存的是名字，Codex 一重启就指向不存在的管道。实测：
`600d7d50…` → 重启后变成 `ebb92b93 / f77ace1a / 2a651cf1` 三条全新的。

**所以绑定必须能自愈**：推送前重新发现，而不是指望它一直有效。

### 3. 管道不存在时 `ConnectAsync` **不抛 FileNotFound，它会等到超时**

判失效原本挂在 `FileNotFoundException` 上，于是**永远不触发**；账本里看到的是
八次「被取消」，每次干等满 4 秒，白花 90 秒才轮到兜底。

**连接超时才是「管道不在」的确证**，而且必须用
`when (!cancellationToken.IsCancellationRequested)` 与**调用方取消**分开：
前者是确证，后者是我们自己在收摊。

### 4. 同时存在的管道里只有一条是活的

实测 3~4 条并存，只有 1 条 `tools/list` 返回 38 个工具，其余返回 0 个。
**按名字或按顺序猜都会挑错** —— 每一条都要自证身份才能采用。

（当初定「不枚举命名管道」是为了防止**猜地址**；枚举 + 验证是发现，不是猜。）

### 5. `list_threads` 的信封里那个 `threadId` 必须是**真实存在**的线程

拿全零 UUID 当占位 → `Codex app tool request failed`，而那句话不说是哪里不对。
它只用来认信封，不用来筛结果，所以随便一条真的就行
（按序退：上次连过的 → 当前绑定 → 磁盘上最新的会话记录）。

信封还必须带**顶层 `threadId`**（除了 `arguments` 里的）——
少了它回 `Invalid app tool request`。

### 6. `updatedAt` 同一个字段里混着两种单位

置顶那几条是**毫秒**（`1786779621000`），其余是**秒**（`1789021032`）。
直接比大小会把一条 2026-05 的旧对话判成「最近活跃」——
**不报任何异常，只是悄悄连错对话**。必须先归一。

## 另外三条与"挑对话"有关的事实

- **`thread/list`（app-server 的）既不按时间排序，也不把最近的给全**：
  拿到 40 条里最新的是前一天，当天的一条都不在（有 nextCursor，只是一页）。
  按它的顺序取「第一条」当最新，是个错的假设。
- **App 正开着的线程连不上**：`thread/resume` 报
  `already has an active writer`。那不是失败，只是说这条不能由我们来写 ——
  往下找一条就好。
- **磁盘上的会话记录**（`~/.codex/sessions/**/rollout-*.jsonl`）首行
  `session_meta` 里有 `thread_source`：observed 取值 `voice_chat` / `user` /
  `automation` / `subagent` / `realtime_voice`。要挡的是 automation 与 subagent
  （它们会抢绑定、也不该代表用户）。
  ⚠ **不要用 `thread/list` 回的 `threadSource`** —— 那是**客户端**
  （实测 25 条全是 `vscode`），拿它做排除表是空转：永远不匹配，
  而空转的排除跟没有排除行为一样，只是看起来像有。

## 起语音这件事，与通道无关

**推送不能用来起通话**，而且是结构性的：登记钩子只在
`SessionStart` / `UserPromptSubmit` 触发，而实测

- 冷启动后**启动 Codex** → 钩子不触发、无绑定；
- 用 CLI **建新对话** → 钩子不触发、无绑定；
- App 产生 `voice_chat` 会话的时刻 → **正是语音起来的时候**。

所以「通道要 App 的会话 → App 的会话要语音起来 → 而我们想用通道去起语音」
是个闭环。**起通话由桥自己按**（拉起 Codex → 等就绪 → 按一次 → 用台账确认，
实测 3.7~13.7 秒），语音一起来钩子自然触发，通道随之而来。

⚠ 另有一条独立的观察：**麦克风台账只能作为麦克风使用迹象，不能单独证明实时
语音已经接通**（Codex 2026-09-10 提醒）。现有确认链全建在台账上，属于过度解读
同一个信号 —— 待改。

## 排查时先看这四本账

| 账本 | 位置 | 回答什么 |
|---|---|---|
| 推送尝试 | `runtime/codex-push-attempts.jsonl` | 发没发出去、为什么没发出去 |
| 启动尝试 | `runtime/voice-start-attempts.jsonl` | 按了没有、确认了没有、HTTP 状态 |
| 登记钩子 | `~/Documents/Codex/…/reader-registration-hook.jsonl` | 钩子触没触发、被拒的原因 |
| 状态回执 | `runtime/voice-status-receipts.jsonl` | 对面收到了吗、它怎么说 |

⚠ **`reader-registration-hook.py` 不在仓库里**（在
`~/Documents/Codex/2026-09-07/realtime-voice-chat/`），没有版本控制、进不了测试，
而它是承重件。应该搬进仓库。
