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
  视频/旧撤销卡/EPUB 动作卡（历史里出现会记日志）；App 端工具「长条」即时反馈（__bwToolChip）不再显示。

### 原 P2 打字发送与流式（网页 send，仅退回路径用）
- 原生输入框直接调 stream bridge；`sentCtx` 由原生组（可见页正文、选区、页码、图）。
- 把 `_handleEv` 的事件表搬进 Swift（delta/parts/final/tool/cards/status/progress/task/cli/gone）。
- JS `__asstSend` 退为兼容入口。

### P3 语音事件
- `rc-voicecall` 的 runner 事件 → 轮次命令搬进 Swift（桥的事件流由 Swift 直接订阅）。

### P4 删抓取
- `ReaderNativeConversationScript` 的消息投影部分删除；只保留仍未迁移的动作句柄。

## 2. 阅读位置
- 2026-09-26 已完成第一步：原生视口是本机权威（等存储握手再开书；本机写的
  reading-position 不压原生视口；App 内网页层不再写 PDF 续读）。
- 剩：EPUB 续读、`ctxSync.report` 的当前页上报改由原生发。

## 3. 语音客户端
（P3 之后细化：录音/播放/会话状态目前在 `rc-computer-voice.js` + Swift 各一半。）

## 4. 摆放逻辑
（卡片/收藏落页的摆放目前在 JS `placement`，原生 `ReaderNativeFavoritePlacement` 已有一部分。）

## 规则
- 每步都要**出声**：未迁移的分支 dlog/postClientLog，不静默丢。
- 每步在模拟器验证后再进下一步；数据格式不变（服务端历史仍是权威）。
