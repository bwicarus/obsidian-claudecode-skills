# Claude 完整接管：电脑按钮、Swift App、Codex / GPT Classic 语音与文字接力

> ⚠ **2026-08-02 时点的产品全貌记录，勿据其推断现状**：三条地基前提都已被推翻 —— ① Swift 真值不再是 `C:\iCloudDrive\BWReaderNative.swiftpm`，2026-08-03 `49abb569` 已把它合进单一 Xcode 工程 `ios/BWReader/`；② App 不再是「WKWebView 加载 Pi 上 Reader 网页」的壳，阅读器 runtime 由 `ios/BWReader/package_local_reader.py` 烤成 ReaderBundle 随包发、走环回 `127.0.0.1:43129`，改前端要到 iPad 只能出 TestFlight 构建；③ §0 里「Safari/PWA 无原生能力时按钮显示不可用」所指的 PWA 表面已于 2026-08-14 整体下线（`_server_deploy/reader_pwa_retirement.py`，`/pdf/`、`/pdf/search`、`/pdf/epub/view`、`/pdf/fav/view` 返 410）。§6/§9 的版本坐标（桥 0.1.38 回滚点、生产 HEAD `2ba49dc`、黄金基线 `4fc0dfa + preview10`）也全部过期，桥候选已到 0.1.159。仍然成立的只有按钮语义与 DOM（`#asst-computer` / `#vc-top-computer`、电脑按钮与普通电话按钮分流，见 `_server_deploy/static/pdf/rc-voicecall.js`）。现行架构见 CLAUDE.md「iOS App 形态」，分工见「工程所有权」。

> 日期：2026-08-02 JST  
> 用户决定：本模块后续由 Claude 接管。本文是产品与架构全貌，不只是一次故障记录。  
> 安全边界：不要自动启动语音、采音或发送快捷键；iPad 实机通话由用户测试。

## 0. 先统一名词（用户说的“电视按钮”是什么）

用户口中的**电视按钮**，就是新做的**电脑图标按钮 / 电脑客户端按钮**，并不是电视功能。
图标外形是简约 Apple 风格的显示器，所以用户也会叫它“电视按钮”。

- 侧栏/输入区 DOM：`#asst-computer`
- 顶栏 DOM：`#vc-top-computer`
- 网页实现：`_server_deploy/static/pdf/rc-voicecall.js`
- 它替代了原来普通电话按钮左侧的麦克风位置，是 **BWReader 原生 App 专属**的 Windows 电脑语音入口。
- 它和右侧的**普通电话按钮完全分开**：
  - 电脑/电视按钮：只负责 iPad App ↔ Windows 电脑客户端（Codex 或 GPT Classic）。
  - 普通电话按钮：只负责原有豆包 / GPT / Grok 网页语音，不得启动 Windows 桥接器。
- 按钮状态沿用电话按钮视觉：连接中琥珀闪烁、已连接绿色呼吸、说话蓝色脉冲。
- 当前原生脚本会主动移除 `.speaking`，所以原生电脑通话实际只显示“连接中/已连接”，
  不会像普通电话那样根据声音蓝闪；不要把这误判成音频未传输。
- 普通 Safari/PWA 没有原生能力时，该按钮显示不可用，提示
  `电脑客户端语音仅在 BWReader App 中可用`；不要重新做第二套 Safari 启动逻辑。

Swift App 在页面开始时注入：

- `window.__BW_NATIVE_COMPUTER_VOICE__ = true`
- `window.__BW_NATIVE_COMPUTER_VOICE_APP_VERSION__`
- WK handler：`bwNativeComputerVoice`

对应源码：

- `C:\iCloudDrive\BWReaderNative.swiftpm\AppModule\ReaderWebView.swift`
- 网页只发送严格两字段：
  `{ action: "toggle", appKind: "codex-desktop" | "chatgpt-classic" }`
- Swift 当前严格要求主 frame、同一 WKWebView、Reader 固定 host、字段数为 2、目标在枚举内。
- Swift 源码里的可见 build 标识为 `2026-08-02.2`，拒绝 toggle 时会在 iPad 顶部显示红色诊断横幅；
  该版本已修正 START 字段的 `DirectJSONValue` 类型名并使用 `.allowBluetoothHFP`。
  Windows 只能确认源码，不能证明用户 iPad 当前安装的一定就是这一版；若 WebView 还缓存旧的一字段
  `{action:"toggle"}`，严格两字段 guard 会拒绝，必须用屏幕诊断或实际 App 版本确认。

## 1. 用户要的最终产品行为

1. 用户在 Reader“电脑客户端”设置页选择 **Codex** 或 **GPT Classic**。
2. 这个选择同时决定：
   - Windows 上启动/停止哪个语音应用；
   - 旧文字注入模式下注入到哪个应用。
3. 用户点电脑/电视按钮：App 申请 iPad 当前网页麦克风，建立到 Windows 的固定 Tailnet WSS，
   Windows 启动所选应用和语音，把目标应用输出音频回传 iPad。
4. 再点同一个按钮：停止当前桥接、关闭所选目标的语音、释放 iPad 音频会话并归还上下文连接。
5. “查看状态”“刷新直连状态”“选择目标”都不能启动应用、采音或发送快捷键。
6. Codex 与 GPT Classic 必须隔离；新增 Classic 不能再破坏此前可用的 Codex 路径。

## 2. Swift 原生 App（不是独立测试按钮，而是正式入口壳）

新版源码目录：`C:\iCloudDrive\BWReaderNative.swiftpm`。实际供 Swift Playgrounds 同步/打开的
部署目录是 `C:\iCloudDrive\iCloud~com~apple~Playgrounds\BWReaderNative.swiftpm`；用户在 iPad
端直接看到 iCloud 同步结果，不需要另打 zip。

关键文件：

- `AppModule/BWReaderNativeApp.swift`：原生 App 外壳，嵌入 Reader WKWebView。
- `AppModule/ReaderWebView.swift`：注入电脑按钮能力、接收 toggle、网页/原生上下文 WSS 交接。
- `AppModule/NativeVoiceBridge.swift`：启动/停止状态机、麦克风和 WSS 生命周期。
- `AppModule/DirectVoiceSocket.swift`：HELLO / START / PCM / HEARTBEAT / STOP 和上下文消息。
- `AppModule/DirectVoiceProtocol.swift`：固定 WSS、协议和 `DirectVoiceTargetApp`。
- `AppModule/NativeAudioEngine.swift`：`AVAudioSession.playAndRecord + voiceChat`，iPad 麦克风上行和
  Windows 音频下行播放。
- `Info.plist`：`UIBackgroundModes = audio`；用户已实测发布为真正 App 后锁屏仍可通话。

固定直连：

- WSS：`wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1`
- Origin：`https://bwicarus.taile44d0c.ts.net`
- 音频不经 Pi 中继；Pi 仍承载 Reader 页面/接口/书籍数据。

### 上下文 WSS 唯一所有者

- 空闲时：网页维持 snapshot/context 常驻 WSS。
- 原生语音 START 前：`ReaderWebView.prepareForNativeVoice()` 调
  `RC.computerVoice.prepareNativeContextHandoff()`，网页让出连接。
- 通话中：Swift 原生 WSS 是唯一连接；网页通过 `bwNativeComputerContext` 把 Reader context
  交给 Swift，再由同一原生 WSS 发 Windows。
- 停止或失败：Swift 释放连接，网页 snapshot WSS 必须重新接管。
- 两条 WSS 不得同时运行，也不能都停在“以为对方会接管”的状态。

### 当前兼容处理

`DirectVoiceSocket.start(appKind:)` 当前为了兼容已回滚的旧 Windows bridge：

- Codex START 只发 `sessionId`（保持旧 wire shape）。
- GPT Classic START 才发 `sessionId + appKind`。

这不是最终理想合同，而是当前回滚期间的兼容层；恢复新版 bridge 后要确认是否仍保留。

## 3. Reader“电脑客户端”设置 Tab

设置已经从“AI·翻译”里独立出一个 **电脑客户端** Tab，源码：

- `_server_deploy/static/pdf/rc-settings.js`
- `_server_deploy/static/pdf/rc-computer-voice.js`

主要项目：

- **语音与文字接力目标**：Codex / GPT Classic
- Windows 桥接器状态与“刷新直连状态”
- 上下文接力模式：`legacy-inject` / `snapshot-mcp`

目标持久化：

- API：`/api/assistant/voice-config`
- 字段：`rt_computer_target`
- 值：`codex-desktop` / `chatgpt-classic`
- 后端白名单入口：`_server_deploy/assistant.py` 的 voice-config 处理。

注意：目标选择只是配置，不代表 Windows 端 Classic 已可用。

## 4. 语音的两种 Windows 目标

### 4.1 Codex Desktop（旧稳定基线）

- 固定 AUMID：`OpenAI.Codex_2p2nqsd0c76g0!App`
- 包路径标识：`\WindowsApps\OpenAI.Codex_`
- 麦克风 consent key：`OpenAI.Codex_2p2nqsd0c76g0`
- 语音启停：Codex 全局快捷键；同时以 Codex voice activity 状态确认，不能盲目重复按键。
- 音频：iPad 麦克风送 Windows 虚拟麦克风；Codex 进程树输出固定到独立虚拟扬声器后回传。

### 4.2 GPT Classic（新增目标，当前仍是 WIP）

GPT Classic 没有可用的全局语音快捷键，不能套 Codex 的按键逻辑。用户已手动定位过语音开始、
语音结束、输入框和发送按钮。设计采用 Windows UI Automation，不使用坐标点击：

- 目标：`chatgpt-classic`
- AUMID / 包身份由 `DirectAppTargetProfile.cs` 固定白名单校验。
- 语音开始按钮 accessible name：`启动语音功能`
- 语音停止按钮 accessible name：`结束语音功能`
- 只允许目标窗口进程树内**唯一、可见、可用**的 Button，并通过 `InvokePattern` 调用；否则 fail closed。

WIP 源码（当前共享 `C:\claude` 脏工作区里，不能当已发布事实）：

- `extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectAppTargetProfile.cs`
- `.../ChatGptClassicVoiceAutomation.cs`
- `.../DirectBridgeProtocol.cs`
- `.../DirectBridgeAdapters.cs`
- `.../WindowsDirectAdapters.cs`
- `.../WindowsCodexAppProbe.cs`

新版协议方向：START 可选 `appKind`；省略默认 Codex；显式 Classic 时全链按目标 profile 选择
启动、语音启停、进程音频捕获、按应用路由和 typist。

## 5. 文字接力不是同一种能力

用户要求设置里的目标同时切换“语音＋文字注入”，但要保留这两个上下文模式的边界：

### `snapshot-mcp`

- 当前主实验路径，正文只更新 Windows 本地快照，不写进客户端输入框。
- AI 需要时调用 `reader_snapshot` MCP 工具读取。
- 当前只在 Codex 有这条 MCP；**GPT Classic 没有 snapshot MCP 接入**，不得宣称可用。
- snapshot 模式下不能同时启动 legacy typist。

### `legacy-inject`

- 保留的旧版文字注入/回滚路径。
- Codex：既有 `voice_typist.py` 路径。
- GPT Classic：WIP 中按包身份识别（因为 Codex 与 Classic 的 exe 名都可能是 ChatGPT.exe），再用 UIA：
  - composer automation id：`prompt-textarea`
  - send button id：`composer-submit-button`
  - 聚焦、粘贴读回、清空 fence、`InvokePattern` 发送
  - 不套 Codex session-id / 快捷键验证。
  - Classic 最多能按 composer 清空确认 `submitted_unverified`，不能冒充已经在对话线程中验证成功。

相关 WIP：

- `extensions/bw-reader-webext/windows/typist-runtime/voice_typist.py`
- `.../voice-typist-launcher.ps1`
- `extensions/bw-reader-webext/windows/bw_computer_voice_supervisor.py`
- `extensions/bw-reader-webext/windows/bw_computer_voice_typist_helper.py`

已发现必须先统一的命名漂移：supervisor 旧配置校验仍接受
`chatgpt-desktop` / `codex-desktop`，而新目标与 typist 使用
`chatgpt-classic` / `codex-desktop`。这会造成一段接受、一段拒绝；不要只改其中一处。

## 6. 已部署、已安装、WIP 三者必须分清

### Reader / Pi 当前生产

生产与干净部署克隆当前 HEAD：

- `2ba49dc4893390f9e234e32dd232c66bd52debc7`
- `fix(reader): deliver native voice toggle synchronously`

它修复了新增目标选择后的首个必败点：按钮过去先等
`/api/assistant/voice-config` 网络请求再 postMessage；App cookie 缺失/过期时拿到登录 HTML，
JSON 解析失败，网页却已返回“成功”，导致 Swift 永远收不到 toggle、Windows 零 START。
现在按钮使用内存目标**同步** postMessage，网络读取不再挡住启动入口。
核对时 Pi 的 webapp / voice-rt 均为 active，最新 Reader 部署事务为 complete；这只证明网页生产
已更新，不证明 iPad App build 或 Windows 新 bridge 已更新。

相关提交脉络：

- `df27b0d`：电脑客户端设置独立入口
- `4fc0dfa`：电脑语音改由原生 App
- `30ec1de`：Codex / GPT Classic 目标选择
- `2ba49dc`：原生 toggle 改回同步投递

### Windows 当前实际安装

为恢复旧 Codex 基线，已从 0.1.38 回滚到安装前备份：

- 当前 EXE：`C:\Users\bwica\bw-computer-voice-bridge\native-host\bw-computer-voice-audio.exe`
- 旧备份：`C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.38-20260801-230501\native-host\bw-computer-voice-audio.exe`
- SHA-256：`9ED80E75E32526C90FF6771138FDA5BF09CC98E6847C5193050E3A1D7554F032`
- 新 0.1.38 回退副本：`C:\Users\bwica\bw-computer-voice-bridge-backups\rollback-current-0.1.38-20260802-011752`
- 当前 config：`reader-computer-voice-direct-config/5`、`appKind=codex-desktop`、
  `contextDeliveryMode=snapshot-mcp`

因此**当前安装只支持 Codex，不支持 GPT Classic**。0.1.38 曾构建/安装，但两种目标均不可用，
所以已回滚；其源码只是待修 WIP，不能告诉用户“Classic 已经能用”。

另有一个已知但未修的纯文案偏差：`NativeVoiceBridge.swift` 收到 socket `.starting` 时仍可能显示
“正在等待 Windows 启动 Codex Voice…”，即使本次目标是 GPT Classic；这不改变实际 appKind 路由，
但修 Classic 时应一起改成按目标显示。

### 工作区边界

`C:\claude` 有大量未提交/他人 WIP，且可能落后生产。禁止 reset/clean、宽范围 checkout、
`git add -A`。以干净克隆
`C:\Users\bwica\AppData\Local\Temp\codex-reader-deploy-20260801-2314`
和 Pi HEAD 核对生产；开发另开 worktree，按目标小范围移植 WIP。

## 7. 接管顺序（用户要快，但不能再混淆目标）

1. 先复述确认你已理解：
   - 电视按钮 = App 专属电脑按钮；
   - 普通电话按钮独立；
   - GPT Classic 没有全局快捷键，走 UIA；
   - 语音目标和 legacy 文字目标绑定，但 snapshot MCP 目前仅 Codex。
2. 以当前已部署 `2ba49dc` + 当前 Swift App + 旧 Codex bridge 恢复 Codex 单目标，由用户实测。
3. 在隔离 worktree 修正 0.1.38，尤其统一 `chatgpt-classic` 命名和 Classic UIA/typist；
   不要让 Classic 改动污染 Codex。
4. 用户验证 Codex 后再安装 Classic 候选；真实通话仍由用户点击。
5. 每轮只报告：改了什么 / 验证了什么 / 没做什么 / 下一步谁做。

## 8. 当前故障交接补充

较窄的现场故障记录仍在：

`C:\claude\references\claude-voice-text-injection-handoff-20260802.md`

先以本文理解产品全貌，再用该文件看“Windows 已连接但零 START”的现场证据。

## 9. 之前实际能用的黄金基线（不要再猜版本）

用户要求明确告诉接管者“改 GPT Classic 以前能用的那一版”。本机仍保留完整旧包和二进制，
可直接作为行为真值，不需要从当前 WIP 反推。

### 9.1 iPad Swift App：最后一个单目标完整交付包

- ZIP：`C:\Users\bwica\AppData\Local\Temp\codex-ios-native-voice-playground-20260731\ios\BWReaderNative-0.1.0-preview10.swiftpm.zip`
- SHA-256：`91D2E143CA53A27960E2E292E74AB9DEDBE2654F7E0FBD968F1734EF26DE68E4`
- 同内容展开目录：`C:\Users\bwica\AppData\Local\Temp\codex-ios-native-voice-playground-20260731\ios\BWReaderNative.swiftpm`

该包的关键合同已经从 ZIP 内只读核对：

- `ReaderWebView.swift` 只接受**恰好一字段** `{action:"toggle"}`，没有 `appKind`。
- `toggleNativeComputerVoice()` 永远启动 Codex 单目标。
- START 只发 `sessionId`，没有 GPT Classic 分流。
- START 前调用旧 `RC.computerVoice.setDialPending(true)`，等待 800ms 让网页 snapshot WSS
  释放；停止后 `setDialPending(false)` 恢复网页链。
- 尚无 `bwNativeComputerContext`，通话中没有把 Reader context 复用进原生 WSS；这是旧版能力边界，
  但音频/锁屏通话路径是用户此前实际跑通过的基线。

### 9.2 与旧 App 匹配的 Reader 网页入口

- Git 提交：`4fc0dfaac175a46ef9d4f04f8003541996e20f2c`
- 标题：`feat(reader): route computer voice through native app`
- `_toggleNativeComputerVoiceApp()` 同步发送单字段 `{action:'toggle'}`。
- 电脑按钮 App 专属、普通电话分流已经存在；尚未加入 Codex / GPT Classic 目标选择。

`30ec1de` 才开始加入双目标与两字段 `appKind`；所以排查“以前能用、现在都不能用”时，
应以 `4fc0dfa + preview10` 为分界，而不是只盯当前 `2ba49dc`。

### 9.3 与旧 App 匹配的 Windows Codex bridge

- EXE：`C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.38-20260801-230501\native-host\bw-computer-voice-audio.exe`
- SHA-256：`9ED80E75E32526C90FF6771138FDA5BF09CC98E6847C5193050E3A1D7554F032`
- 这是安装 0.1.38 前自动备份的 Codex-only build；当前安装目录已回滚为同一 SHA。
- 文件元数据只写 `1.0.0`，所以不要靠 ProductVersion 猜；“pre-0.1.38 / Codex-only + SHA”
  才是无歧义身份。

### 9.4 恢复策略

先把 `4fc0dfa + preview10 + 9ED80E75…` 当黄金对照，证明 Codex 单目标能工作；然后逐层移植：

1. 先只加新版原生 context relay，仍保持 Codex 单目标；
2. 用户实测；
3. 再加目标选择与 Classic UIA；
4. 最后加 Classic legacy typist。

不要继续在同时变化的网页两字段、Swift 严格 guard、原生 context relay、Windows 双目标、
Classic UIA 与 typist 六层之间猜是哪一层坏了。
