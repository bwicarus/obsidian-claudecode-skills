# 网页层迁出计划（2026-09-26 起）

用户拍板的顺序：**对话侧栏数据链 → 阅读位置 → 语音客户端 → 摆放逻辑**。
目标：App 里隐藏 WKWebView 最终只剩「尚未迁移的动作」，数据与呈现全部原生。

## 0. 现状（迁移前实测）

| 层 | 现在谁做 | 代码 |
|---|---|---|
| 请求/流式传输、断线恢复 | Swift | `ReaderNativeAssistantStreamBridge` |
| 历史读取、分类（user/parts/card/answer） | Swift | `ReaderNativeAssistantHistory`（把分类挂在 `__bwNativeHistory`） |
| 轮次状态（草稿/工具/卡片/合并） | Swift | `ReaderNativeTurnStore` + `ReaderNativeTurnBridge` |
| **事件 → 轮次命令**（打字流） | JS | `rc-assistant.js` `_handleEv` / `onHistoryEvent` |
| **历史 → 轮次**（回放） | JS | `rc-assistant.js` `_historyReplayOne`（混着 `addMsg` DOM 消息、上下文卡、追问、反馈、录音按钮、撤销卡、EPUB 动作卡） |
| **语音事件 → 轮次命令** | JS | `rc-voicecall.js` |
| 侧栏呈现 | Swift（SwiftUI） | `ReaderNativeConversationView` 等 |
| 非轮次消息（`asst-u/asst-a/asst-note`） | JS DOM → **抓取**投影给 Swift | `ReaderNativeConversationScript`（1700+ 行嵌入 JS） |

所以「数据链迁出」= 把三个**生产者**（历史回放、打字流、语音事件）从 JS 搬进 Swift，
让 TurnStore 直接被 Swift 喂；最后删掉 DOM 抓取。

## 1. 对话侧栏数据链

### P1 历史回放（只读，风险最低，先做）
- Swift 读到历史后**直接**生成 TurnStore 轮次（不经 JS `renderTurn`）：
  user（文本 + 上下文摘要：页码/选区/图）、parts（原样）、legacy card、answer
  （正文去心情标记、追问拆出、`via=voice` 字幕标记、trace、videos、undo_cards、actions）。
- 原生视图补：上下文小条、追问按钮、步骤/模型（trace）、撤销卡；录音回放按钮、
  EPUB 动作卡可后置（先显式标「未迁移」而不是静默丢）。
- 开关：原生模式下 JS 不再回放（`_historyReplayOne` 早退并 dlog）。
- 验证：模拟器开书看侧栏与迁移前一致（对照 `conversation-cache/normal.json`）。

### ✅ P1 已完成（2026-09-26）：历史里的用户话/纯文字回答/旧卡由原生按 ref 生成
### ✅ P2 已完成（2026-09-26）：原生对话流 `ReaderNativeConversationFeed`
- **关键发现**：App 普通模式打的字本来就不走网页 `send`，而是 Swift `submitTypedToBackend` 交给语音核心；
  回复经 `/pdf/api/reader-events` 的 `assistant-history` 事件回来。所以真正的主路径是「事件 → 侧栏」。
- 现在：Swift 自己读历史（`readHistory`）、自己订阅事件流（`subscribeReaderEvents`，断线退避重连 + 补读）、
  事件闸门（streamRevision / final / absorbed_ids）移植自 `onHistoryEvent`，写进 TurnStore，
  `TurnStore.feedMessage` 直接出侧栏消息；模型在普通模式只用对话流（`applyFeed`）。
- 消息身份 `m:<role>:<turn_id>`：实时与落库同一个 id，过渡不闪不重。
- 网页：`window.__bwNativeConversationFeed = true`（documentStart 注入）→ 普通模式的 `onHistoryEvent`/`loadHistory` 让位。
- 退回路径（语音核心不在 → 网页 `send`）：用户话（P2a 原生占位）与原生回复（replyRef 轮次）也进对话流。
- **未迁**：复习会话仍走网页；hlcard「撤销/重做」仍靠网页 turnCard（对话流里的历史轮网页不认识 → 需原生化）；
  视频/旧撤销卡/EPUB 动作卡（历史里出现会记日志）；~~App 端工具「长条」即时反馈不再显示~~ → P3 修复。

### 原 P2 打字发送与流式（网页 send，仅退回路径用）
- 原生输入框直接调 stream bridge；`sentCtx` 由原生组（可见页正文、选区、页码、图）。
- 把 `_handleEv` 的事件表搬进 Swift（delta/parts/final/tool/cards/status/progress/task/cli/gone）。
- JS `__asstSend` 退为兼容入口。

### ✅ P3 已完成（2026-09-26，待模拟器验证）：语音事件进原生对话流
- **根因（P2 回归）**：P2 让网页 `onHistoryEvent` 在普通会话整段让位，连 `stream:"start"` 里
  「服务器轮次号 → `__bwLiveTurnId`」这一步也丢了 → App 现场执行的语音工具长条（busy/idle）、
  结果卡、流程进度都写进网页本地临时轮次 `_vTid`，原生对话流从不认识它 → 侧栏看不见。
- **现在**：
  - 原生对话流订阅到 `start` → `announceLiveTurn` → 网页 `window.__bwNativeFeedLiveTurn(tid)`
    （`rc-assistant.js` 的 `_adoptLiveTurn`，与原 start 分支同一段逻辑：设 `__bwLiveTurnId`、
    未认领的本地容器改名并补存）。
  - `ReaderNativeTurnBridge` 在网页序号协议每批提交后回调 `onWebApplied(changed, removed)`；
    对话流 `observeWebTurns`：已在流里的轮次重出，新轮次（普通会话、非回放、非 `user:`/`hist_`）
    按首次出现收编，身份仍是 `m:assistant:<轮次>` → 服务器那条落库后历史自然接手，不闪不重；
    被改名/丢弃的轮次退出「进行中」。
- **仍在网页**：语音事件的**传输**（`rc-computer-voice.js` DirectSocket → `acceptRealtimeOutput`）
  和执行（工具长条状态机、后台任务轮询、结果卡渲染）。搬传输属于「3. 语音客户端」。

### ✅ P4 已完成（2026-09-26，待模拟器验证）：删抓取，复习会话也进原生对话流
- **P4a**：普通会话停抓 DOM；网页直接写的 `asst-note` 提示在挂载时以 `feed-note` 交给原生。
- **P4b 复习会话迁出**（原先阻塞删投影的唯一原因）：
  - 对话流跟随侧栏会话（`ReaderNativeConversationFeed.setMode`）：普通/复习各自读历史、各自过滤事件、
    各自清空；切换时整套重来并立即出一次（侧栏先显示该会话的本机缓存）。
  - 网页 `onHistoryEvent` / `loadHistory` 在原生对话流下对两种会话都让位；本轮身份（`__bwNativeFeedLiveTurn`）不再限普通会话。
  - 「选用回答」原由网页 `rc-review._presentationSelections` 按 DOM 节点登记后随投影附上 → 现由原生
    `ReaderNativeReviewAnswers` 生成：身份算法与网页一致（契约测试 `P4b native review answer identity…` 守两份副本），
    记录直接 upsert 进原生选择图（`ReaderNativeContextSelectionBridge.registerReviewAnswers`），`selectReview` /
    `reviewPairs` 不变；回答第一次完成时绑定当时的复习卡，换卡 / 选中变化时对话流重出。
- **删抓取**：`ReaderNativeConversationScript` 删掉消息源（`createMessageSources`）、`projectMessage`、
  轮次收编判定、增量协议（`prepareMessageDelta` / `compactNativeMessage`）和全部消息状态，快照里不再有消息
  （`payloadBytes` 应稳定在几 KB）。`__bwNativeMessages` 留一个薄壳：只转交提示，其余挂载钩子是空操作。
  原生模型认「快照里没有消息字段」（不再当跳序去要重同步）。
- **仍留在网页的动作句柄**（未迁移，按计划保留）：
  - 学习卡的卡面/状态/操作输入仍在 `rc-flashcard` —— 原生把对话流用到的卡组交给网页（`watchCards`），
    快照按卡组带 `cardInputs`（`presentationInput`），模型并进学习卡部件；没拿到的标 `card-state-pending`。
    （P2 起对话流里的学习卡缺这一份，这里一并补上。）
  - 页卡 / 浮动卡摆放（`pagePlacements` / `floatingPlacements` 仍用 `projectPart` / `liveArtifacts`）、工具栏、选区、
    设置/搜索/目录面板这些非消息部分照旧。
- **Swift 里的死代码待清**（这里没有编译器，没敢删）：`ReaderNativeConversationStore` 的增量应用、
  `ReaderNativeTurnBridge.conversationPayload`、`resolveNativeHistory` 占位解析、模型里的
  `messageDelta` / `messageRevision` 分支 —— 快照不再带这些字段后它们都不会再走到，在 Mac 上编译通过后可一并删。
- 旧网页界面（设置里关掉「原生界面」）不注入 `__bwNativeConversationFeed`，网页照旧回放与渲染。

## 2. 阅读位置
- 2026-09-26 已完成第一步：原生视口是本机权威（等存储握手再开书；本机写的
  reading-position 不压原生视口；App 内网页层不再写 PDF 续读）。
- 剩下两项**都被别的块挡着**（2026-09-26 查实）：
  - EPUB 续读：App 里 EPUB 仍由网页 `epub-html.js` 渲染（原生只解析 OPF/目录），位置天然来自网页 ——
    要等 EPUB 原生渲染才有「由原生负责」可言，不单独做。
  - 当前页上报（`ctxSync.report`）：App 里它的出口是网页里的快照链接（见 3），原生已能把 PDF
    page.context 写进本机发送队列（`publishReadingContext`），但**传输**仍在网页 → 随 3b 一起搬。

## 3. 语音客户端（2026-09-26 细化）
### 现状（查实）
- 通话音频：已是原生（`NativeVoiceBridge` + `DirectVoiceSocket` + `NativeAudioEngine`），通话时 Swift 独占语音 WSS。
- **阅读器快照链接**：仍在网页 `rc-computer-voice.js`（`reconcileSnapshotLink` 一族，约 5000 行），走独立的
  context 端点。它管：上下文上行（context pump / active-reading pump）、服务器下发的查询
  （`READER_QUERY_HANDLERS`：highlights / notes / search …，答案来自网页 `_nativeReader*`）、视觉请求（截图）、
  结果与实时输出（高亮、卡片、制卡草稿 → rc-voicecall 执行）。
- **熄屏断连的机制**：进后台 → 原生 `setReaderForeground(false)`（停本机 runtime）→ 网页
  `readerContextSurfaceVisible()` 读到 `__BW_NATIVE_READER_FOREGROUND__=false` → `snapshotLinkWanted()` 为假 →
  **主动关快照链接**；即便不关，iOS 也会挂起后台 App 的 WebKit 网页进程。所以熄屏期间通话音频可以继续，
  但语音 AI 的阅读器工具全部失效 —— 只能靠原生链接解决，网页层修不了。

### 分阶段
- ✅ **3a 诊断出口（2026-09-26）**：网页关快照链接时 `dlog` 写明是哪一条条件关的、是否在通话中；
  原生进后台时若电脑语音仍在通话，`postClientLog` 出声。→ 先拿一次熄屏通话的真实日志再动 3b。
- ✅ **3b-1 后台通话期间原生保持快照（2026-09-26，待设备验证）**：`ReaderNativeBackgroundContext`。
  进后台且电脑语音仍在通话 → 网页交出最后一份 active-reading（`RC.computerVoice.backgroundSnapshotHandoff`，
  与快照链接发的同一结构，不在原生另造）→ 等 1.5 s 让网页关掉自己的链接 → 原生开 context 会话、
  重发本机发送队列里最新一条 page.context、发 active-reading 并每 50 s 续（桥窗口 60 s）；每次续前确认
  仍在后台且仍在通话，否则交还；回前台 / 换书先交还。`DirectVoiceSocket` 只多放行 `context` /
  `active-reading` 两个动作；数据连接收到阅读器事件改为出声忽略（不断会话）。契约测试
  `native-background-snapshot` 守合同名、动作与心跳窗口。
  **效果**：熄屏通话时桥上的快照保持 ready，语音 AI 能读当前页/阅读状态。**仍不行**：查询（高亮/笔记/搜索）、
  截图、实时输出（卡片/高亮/草稿）—— 不登记 visual 来源，桥把它们留在队列等网页回来（下一步 3b-2）。
- **3b-2 后台时回答查询 / 截图**：通话中进后台时，由 Swift 用 `DirectVoiceSocket(.readerContext)`
  登记 visual 来源并回答：
  - 上下文：从原生发送队列（`native-outgoing-journal`）与 PDFKit 当前页直接上行；
  - 查询：highlights / notes / search 由原生数据库回答（PDF 优先）；
  - 视觉：PDFKit 渲当前页图，按原合同分块（`reader-visual/2`，≤768KB、24 块）；
  - 实时输出：一律回可重试的 `…_UNAVAILABLE`，让桥留在队列里，回前台后由网页执行（桥已有这条重放语义）。
  需要：Swift 侧补 context 会话的事件类型（现只收 `status`）、与网页链接的交接（同一 App 同时只一个
  context 所有者），以及桥端对「来源切换」的验证 —— 这几步都得在设备上验。
- **3c 前台也由原生持有**：网页快照链接删除；输出执行器（卡片/高亮/草稿）仍在网页时，由原生转交。
- **3d 录音/播放/会话状态收拢到原生**，`rc-computer-voice.js` 在 App 里只剩兼容入口。

## 规则
- 每步都要**出声**：未迁移的分支 dlog/postClientLog，不静默丢。
- 每步在模拟器验证后再进下一步；数据格式不变（服务端历史仍是权威）。
