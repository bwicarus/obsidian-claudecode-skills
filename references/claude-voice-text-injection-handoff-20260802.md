# Claude 接管：BWReader App 电脑语音与文字注入（2026-08-02 JST）

> 这份文件只记录一次“零 START”故障。产品全貌（用户所称“电视按钮”、Swift App、
> 普通电话分流、Codex / GPT Classic、语音与文字模式、生产/安装/WIP 边界）请先读：
> `C:\claude\references\claude-computer-client-full-handoff-20260802.md`

## 用户当前要求

用户明确要求把“电脑客户端语音＋文字注入”整块交给 Claude 继续处理，Codex 停止继续修改。
目标不是继续叠补丁，而是恢复此前真实可用的 Codex 路径，再把 GPT Classic 作为隔离目标加入；
不要让新目标污染旧目标。实机 START、麦克风、快捷键与通话由用户亲手测试，排查时不要自动触发。

## 本轮真实失败边界

用户更新并重新打开 Swift App 后反馈“完全不可用”。Windows 现场证明：

- `127.0.0.1:43128` listener 正常，iPad/Tailscale 连接已到 Windows；
- runtime 长期保持 `reader-connected / readerConnected=true / captureActive=false / lastError=null`；
- 没有任何 START 到达 Windows，故问题位于 App 内网页按钮消息之后、
  `DirectVoiceSocket.start()` 发 START 之前；不是 Windows 音频、快捷键或应用路由故障；
- Windows listener 当时 PID 33248，旧桥 status contract `/2`，config contract `/5`；
- 生产 `rc-voicecall.js` 会发
  `{action:'toggle', appKind:'codex-desktop'|'chatgpt-classic'}`，当前 Swift handler 的两字段合同匹配。

## 当前运行状态与回滚

为了恢复先前路径，Codex 已把 Windows native 从 0.1.38 回滚到安装前备份：

- 旧 EXE：`C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.38-20260801-230501\native-host\bw-computer-voice-audio.exe`
- SHA-256：`9ED80E75E32526C90FF6771138FDA5BF09CC98E6847C5193050E3A1D7554F032`
- 当前 0.1.38 恢复点：`C:\Users\bwica\bw-computer-voice-bridge-backups\rollback-current-0.1.38-20260802-011752`
- 当前 config 已恢复为 `reader-computer-voice-direct-config/5`、`appKind=codex-desktop`、
  `contextDeliveryMode=snapshot-mcp`；旧桥不支持 `chatgpt-classic`。

旧桥 START 合同严格只接受 `sessionId`，不能带 START.appKind。为兼容旧桥，已修改：

- `C:\iCloudDrive\BWReaderNative.swiftpm\AppModule\DirectVoiceSocket.swift`
- Codex START 只发 `sessionId`；仅 GPT Classic 才附加 `appKind`。

用户更新 App 后仍失败，说明故障不止 START 字段。

## 当前 Swift / Reader 关键接点

- 新版源码目录：`C:\iCloudDrive\BWReaderNative.swiftpm`
- Swift Playgrounds 实际同步部署目录：`C:\iCloudDrive\iCloud~com~apple~Playgrounds\BWReaderNative.swiftpm`
- 按钮入口：`AppModule\ReaderWebView.swift`
- 生命周期：`AppModule\NativeVoiceBridge.swift`
- WSS/START：`AppModule\DirectVoiceSocket.swift`
- 生产按钮消息：`_server_deploy/static/pdf/rc-voicecall.js::_toggleNativeComputerVoiceApp`
- PWA/App WSS 交接：`_server_deploy/static/pdf/rc-computer-voice.js::prepareNativeContextHandoff`
- Windows START：`extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeProtocol.cs`

Codex 曾短暂尝试把 App handoff 改回旧 `setDialPending(true)+800ms`，随后查到当前生产
`setDialPending(true)` 会刻意保留 snapshot WSS（供网页复用），与原生 App 新建 WSS 不兼容，
因此这段未测试修改已经撤销。当前 App 仍使用 `prepareNativeContextHandoff()`，现场保持为用户失败时的逻辑。

## 首要可疑点（需你自己验证，不要直接当结论）

新增目标选择后，`rc-voicecall.js::_toggleNativeComputerVoiceApp()` 不再同步 postMessage，
而是先 `loadTargetApp()` 请求 `/api/assistant/voice-config`，Promise 成功后才向 Swift 发 toggle。
旧版入口是同步进入 App。Windows“连接存在但零 START”的边界与 App 前置步骤未完成一致。
建议首先给 App 增加最小的本地阶段证据（收到 toggle / 麦克风授权 / handoff 完成 / WSS connect /
START 已写出），或让按钮使用已加载的内存目标同步 postMessage；不要靠 Windows final status 猜 App 阶段。

另需核对 `prepareNativeContextHandoff()` 是否在失败后可靠清除
`nativeContextHandoffPending`，以及 Swift failed/stop 是否让网页 snapshot 重新接管；两条 WSS 不得同时拥有。

## 文字注入边界

用户要求语音目标与文字注入目标都可在 Reader“电脑客户端”Tab 选择 Codex / GPT Classic。
但必须保持两条路径隔离：

- Codex：旧稳定语音路径优先恢复；snapshot MCP 与 legacy text injection 的既有互斥保留；
- GPT Classic：语音和 legacy 文字注入可以单独实现；不要改坏 Codex；
- snapshot MCP 目前属于 Codex，不要谎称 GPT Classic 能读取；
- 不允许自动采音、自动发语音快捷键或在查看状态时启动目标应用；
- 用户负责 iPad 实机通话，工程侧可读日志与部署无副作用修复。

## 工作区边界

`C:\claude` 共享检出有大量别人的未提交 WIP，禁止 `git add -A`、reset/clean 或宽范围 checkout。
优先从干净部署克隆 `C:\Users\bwica\AppData\Local\Temp\codex-reader-deploy-20260801-2314`
或新 worktree 开始，并先核对生产 HEAD。Swift 真值只在上述 iCloudDrive 包内。

用户偏好快速修复后直接让他测试，不要把时间花在大量重复测试；但至少保留一条能区分
“按钮未进 Swift / handoff 未完成 / WSS 未连 / START 未发 / Windows START 失败”的证据。

## 下一步交付格式

1. 先回报你确认的首个必败点；
2. 恢复 Codex 单目标可用，用户实测；
3. 再隔离加入 GPT Classic；
4. 说明改了什么 / 验证了什么 / 没做什么 / 下一步谁做。
