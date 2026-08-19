# iOS Reader + Safari 扩展语音失败交接（2026-08-03）

> ⚠ **2026-08-03 的失败现场记录（TestFlight 1.0.1–1.0.3），勿据其推断现状**：文中点名的工作树 `bwreader-ios-merge` / 分支 `agent/ios-reader-extension-merge` 已被主线取代（合并工程在 `ios/BWReader/`，`49abb569`），CLAUDE.md 更明确写着「旧 worktree（如 `bwreader-ios-merge`）里的远程壳 `ReaderWebView.swift` 是过时代码，勿据其推断」；App 也已从远程 Reader 页改为本地 ReaderBundle 渲染（`ios/BWReader/package_local_reader.py`，环回 `127.0.0.1:43129`）。文末「构建上传需分寸」已放宽：2026-08-18 用户明确 `gh workflow run safari-extension-ios.yml -f upload=true` 可直接触发。§「用户要求的正确产品边界」五条（App 内 Reader 是完整产品、扩展只送当前网页上下文、电脑按钮与普通电话按钮各自独立）**至今仍然成立**。

## 用户要求的正确产品边界

1. `bwicarus-test` App 内的 Reader 是完整产品：书籍阅读、Reader 上下文、电脑客户端语音、
   Realtime 语音等原有功能必须继续独立可用，不能被 Safari 扩展路径替换或降级。
2. Safari Web Extension 在普通网页上提供与 Reader 接近的完整 UI/功能；iOS 浏览器后台和
   音频限制无法承载的部分，应通过 App Group + containing App 的原生能力实现。
3. App 内 Reader 与 Safari 普通网页是两种上下文：
   - App 内电脑语音继续发送当前书籍/页码/选区/绘图等 Reader 上下文；
   - Safari 扩展电脑语音只能发送当前网页标题、可见正文和选区，不得误用 App 书架或上次书籍。
4. 普通 Realtime 电话按钮与电脑客户端按钮仍是两个独立入口：
   - 电脑按钮启动 Windows Codex / GPT Classic 桥；
   - 普通电话按钮使用 Reader assistant 的 agent/S2S 语音，不得启动 Windows 桥。
5. 用户期望 Safari 扩展通过 App 获得原生麦克风、后台音频、WSS 和存储能力；不是把 App
   变成只供扩展使用的空壳，也不是在 App 里重新打开 Reader 书架来冒充当前网页。

## 当前真值与版本

- 工作树：`C:\Users\bwica\.codex\worktrees\bwreader-ios-merge`
- 分支：`agent/ios-reader-extension-merge`
- 1.0.1 基线：`f889f53`，此前已修 Realtime 历史重复，但用户随后报告 Safari 语音问题。
- 1.0.2：`afd817f`，TestFlight 上传成功，但用户实测两个原问题均未解决，并新增 App 内电脑
  按钮不能启动 Windows Codex Voice。
- 1.0.3：`037f8d4`，TestFlight 上传成功；尝试显式接扩展 agent bridge、清理外部 agent
  残留，用户仍明确实测“不行”。
- 当前工作树干净；请从上述分支另建 worktree，不要在 `C:\claude` 脏检出上直接改。

## 用户实测失败（均以用户现场为准）

1. Safari 扩展普通 Realtime 电话按钮仍无法正常启动语音。
2. Safari 扩展电脑语音此前打开 App 后停在 `/pdf/` 书架，不能正确启动；上下文曾显示为
   Reader 书籍而非当前 Safari 网页。1.0.2/1.0.3 声称修复，但用户确认仍未解决。
3. 1.0.2 后连 App 内 Reader 自己的电脑按钮也无法启动 Windows 上的 Codex Voice；1.0.3
   的隔离/清理补丁仍未恢复。
4. 因三次实机结果已否定 Codex 的推断，请不要把下面的“已写代码”当成根因已证实。

## Codex 已写但未获实机证明的实现

### Safari → containing App 合同

- `ios/BWReader/Shared/ReaderNativeBridgeContract.swift`
  - 增加 `voice.context`、`agent.status/toggle/events/command`；
  - App Group 中保存网页上下文、agent 状态、事件和控制命令。
- `ios/BWReader/Extension/SafariWebExtensionHandler.swift`
  - native messaging 写 App Group，并给 start 返回 `bwreader://native-voice` 或
    `bwreader://native-agent`。
- `extensions/bw-reader-webext/background.js`、`src/facade.js`
  - 普通网页采集 `{url,title,visibleText,selection,revision}`；
  - 电脑语音与 agent 语音各自走 native message；agent 事件用游标轮询。

### App 接收端

- `ReaderNativeCommandReceiver.swift`
  - 接 `native-voice` / `native-agent` deep link；
  - 电脑语音把 Safari context 交 `NativeVoiceBridge`；
  - agent 语音调用 `ReaderWebViewModel.startExternalNativeAgentVoice`；
  - 成功后尝试 `UIApplication.shared.open(sourceURL)` 返回 Safari。
- `NativeVoiceBridge.swift`
  - 新增可选 `safariWebContext` 分支，跳过 App Reader 的
    `prepareForNativeVoice()`，并直接向 Windows 发 page.context / active-reading；
  - 500ms 读取 App Group 的网页上下文变化。
- `ReaderWebView.swift`
  - 用现有 `NativeAgentVoiceSession` 承载扩展 Realtime；事件落 App Group，控制命令轮询。
- `rc-voicecall.js`（1.0.3）
  - 显式识别 `window.__bwNativeAgentVoiceExtensionBridge`；App WKWebView 原 handler 优先。

## 已验证与没有验证

已验证：

- 两个版本均通过 GitHub macOS runner 的 simulator build、device archive、签名、IPA 结构校验和
  TestFlight 上传；这只能证明可编译/可打包。
- JS `node --check` 与 Safari 打包成功。

没有验证：

- 没有证明 Safari native messaging 的每个请求真实到达 Extension handler。
- 没有证明 custom URL、App Group pending file、App receiver、原生 session、返回 Safari 这五跳中
  的任何一跳在真机按预期发生。
- 没有拿到 App 内电脑按钮失败时的 NativeVoiceBridge diagnostics / Windows runtime 错误。
- 没有证明 Safari 扩展页当前上下文采集正确，也没有证明 Windows fold 接受 web kind 合同。
- 1.0.3 的“Safari WebKit 对象不可可靠写入”和“外部 agent 残留破坏内部电脑按钮”只是 Codex
  推断，用户实测已表明修补不足，请独立重查。

## 请 Claude 接手的方式与验收标准

先只读建立逐跳证据，不要继续猜：

1. 对 App 内 Reader 电脑按钮，从 WK message 收到开始，一路记录到 `NativeVoiceBridge.start`、
   `prepareForNativeVoice`、WSS HELLO/START 与 Windows runtime；先恢复 `f889f53` 基线行为。
2. 对 Safari 扩展电脑按钮，为每一跳加入用户可见/诊断页可读的 requestId 状态：content →
   background → Safari handler → App Group → deep link receiver → Windows START → context ACK。
3. 对 Safari Realtime 同样证明：rc-voicecall → facade → background → Safari handler → App Group →
   NativeAgentVoiceSession → event 回 Safari → assistant → speak 回 App。
4. 不要让扩展功能依赖 App Reader 页面“准备好”；App 可短暂前台取得启动权，但扩展上下文必须
   始终是原网页。App 内 Reader 路径不得读取 Safari App Group 网页上下文。
5. 首个实机验收顺序：
   - App 内打开一本书，电脑按钮能启动/停止 Codex，AI 看到书籍当前页；
   - Safari 普通网页电脑按钮能启动/停止 Codex，AI 看到网页内容而非书；
   - Safari 普通 Realtime 能听、能在原网页侧栏产生回答、能原生播音并挂断。

请先回报：**改了什么 / 验证了什么 / 没做什么 / 下一步谁做**。若需真机点击，直接列一次
最小动作和要观察的屏幕现象；用户明确愿意协助实机测试，不要用大量推测替代现场证据。

## 构建、上传与发布授权

- 唯一工作流：`.github/workflows/safari-extension-ios.yml`，显示名 `BWReader iOS App`。
- 工作流会依次运行 Safari 打包、XcodeGen、模拟器编译、设备 archive、App/Extension 结构校验、
  IPA 导出和 TestFlight 上传；签名证书、provisioning profiles 与 App Store Connect API Key
  已作为仓库 secrets 配置完成，不要另建证书或 App Store 记录。
- App bundle ID：`space.bwicarus.bwreader2`；Extension：
  `space.bwicarus.bwreader2.Extension`；沿用现有 App Store Connect Apple ID `6793932077`。
- 当前版本从 `1.0.3 (1)` 往上递增；用户此前明确下一次从 `1.0.1` 起，现已使用到 1.0.3，
  不要再上传相同 marketing version/build 组合。
- 上传命令（在目标提交已 push 后）：
  `gh workflow run safari-extension-ios.yml --ref <branch> -f upload=true`
- 状态查看：
  `gh run list --workflow safari-extension-ios.yml --branch <branch> --limit 3`
  与 `gh run view <run-id> --json status,conclusion,jobs,url`。
- 1.0.2 成功 run：`30802300532`；1.0.3 成功 run：`30803788735`。
- 用户已明确：这种可逆测试版修复完成后可以直接构建、push、上传 TestFlight，不必每次等待确认；
  只需在执行后告知为上传中断/替换了什么。不可逆操作、凭据变更、另建 App ID/证书仍需停下。
- 不需要部署 Pi 才能发布 App/Extension 自身改动。若同时修改 `_server_deploy` 的生产 Reader
  文件，Pi 部署是另一条独立流程，不得把 TestFlight 上传当成 Reader 已部署。
