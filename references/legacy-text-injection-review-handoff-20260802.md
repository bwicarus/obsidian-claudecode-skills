# 旧版文字注入双目标可用性复核（2026-08-02 JST）

## 用户目标

请 Claude **只读复核**旧版文字注入当前是否真的可用于两个电脑客户端目标：

- `codex-desktop`
- `chatgpt-classic`

目标选择应同时决定语音目标和文字注入目标；`legacy-inject` 与 `snapshot-mcp`
必须互斥。当前先要证据和首个阻塞点，不要修改、构建、安装、部署、启动语音、
采音、发送快捷键或向任一真实会话注入测试文本。

## 当前已证实的接线

1. Windows 协议仍保留双模式：
   `DirectBridgeProtocol.cs::HandleContextAsync` 在 `legacy-inject` 下只允许活动通话，
   并调用 `ForwardLegacyContextAsync`；`snapshot-mcp` 走独立快照折叠路径。
2. `DirectBridgeAdapters.cs` 构造 `DirectMediaStartRequest` 时，仅在
   `contextDeliveryMode == legacy-inject` 时令 `StartTypist=true`，因此两条末端不会同时运行。
3. `WindowsDirectAdapters.cs` 在 START 前按 `request.AppKind` 启动 typist，并持有精确
   PID/进程代次 lease；正常 STOP、启动失败与异常清理均尝试释放该 lease。
4. 已安装的 `voice-typist-launcher.ps1` 明确接受 `codex-desktop` 与
   `chatgpt-classic`；已安装的 `voice_typist.py` 也有两目标分支。
5. Classic 分支当前设计为 `follow_active`，不读取 Codex session id，不读取 Codex rollout；
   它通过 `ClassicComposerAutomation` 聚焦输入框、粘贴并读回、调用唯一可用的发送按钮。

## 尚未证明、请重点复核

1. **生产 Reader 与本地检出存在代际差异。** 当前共享检出为
   `learning-loop-review-fixes@6b3080e`，本地 `_server_deploy/static/pdf/rc-voicecall.js`
   并不包含生产代际里的 `appKind` 目标选择代码；不要把这个缺失误判为生产已删除。
   请先从生产真值或干净部署检出确认设置 Tab 的目标选择与 START 字段。
2. Codex 的旧文字注入虽然合同、IPC、typist 都在，但本轮尚未做真实活动通话中的端到端投递。
3. Classic 的实现目前把 `verification.method` 设为 `none`；“输入框清空”只能证明应用接受发送，
   不能证明文字已进入对话历史。请明确它当前能达到哪一层，不要把代码存在写成已验收可用。
4. 核对 Reader 切到 `legacy-inject` 后是否可靠清空/停止 snapshot，然后才启动 typist；切回
   `snapshot-mcp` 时是否不启动 typist，并恢复快照常驻连接。
5. 核对同一设置里的 `rt_computer_target`（或当前等价字段）是否一路传到 Swift START、
   Windows `appKind` 和 typist `--target-app`，尤其排除历史名称 `chatgpt-desktop` 漂移。

## 现场版本与边界

- 当前已安装 Windows 桥为 `0.1.50`；`0.1.51` 只是尚未安装的麦克风静音租约候选，
  与本次文字注入复核分开，不要覆盖或安装。
- Codex 语音启停已由用户实测可用；Classic 语音已能打开且下行可听，麦克风端点静音是另一问题。
- 不要联系其他 AI，不要改共享工作区；只读核对后按“改了什么 / 验证了什么 /
  没做什么 / 下一步谁做”回报，并给出两个目标各自的第一个真实阻塞点和最小用户验收步骤。

