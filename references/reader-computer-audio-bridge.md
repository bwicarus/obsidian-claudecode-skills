# 电脑客户端语音桥接

状态：最小端到端实现与无音频真机验证已完成；**未部署、未配对、未注册 Native Messaging、
未启用本地 opt-in，也未启动语音、采集音频或发送快捷键**。

## 用户流程与启动门禁

1. Reader/PWA/扩展设置中选择 `电脑客户端` 只显示桥接状态，不能启动电脑或发送快捷键。
2. Reader 生成一次性 `pairId + pairingCode`；用户将二者粘贴到扩展 popup。扩展在可信
   offscreen context 本地生成设备 ID/token，token 只存 `chrome.storage.local`，页面拿不到。
   Pi 只保存 token 的带 pepper 摘要。
3. Windows 交互桌面运行安装器，必须选择一个明确的 Active 麦克风、填写当前扩展 ID，并逐字
   输入 `ENABLE`。只有这一步会设置 `localOptIn=true` 并为该扩展 ID 注册 Native Messaging。
4. 用户点击 Reader 的电话按钮后，服务端才创建 10 秒有效的一次性启动命令与短期 WebRTC
   会话。扩展 offscreen 必须同时证明：
   - 已配对设备仍有效；
   - Windows host 在线且本地 opt-in 开启；
   - Codex/ChatGPT Desktop 在当前交互会话中有唯一可用窗口；
   - 捕获范围是该应用进程树，麦克风是安装时明确选择的 endpoint；
   - WebRTC 已连接；
   - `voice-typist` companion 已可用。
5. Native host 再次检查上述条件后，才启动两条有界 PCM 管线（应用输出、用户麦克风），并至多
   发送一次用户指定的 `Ctrl+Shift+C`。只有 `capture.active=true`、WebRTC 已连接且 companion
   已运行时，服务端才接受 `started` 回执。
6. 挂断只停止本次媒体捕获和 WebRTC；不会擅自 Stop/Pause 用户已运行的 typist。

任一证明缺失都 fail closed。选择模型、刷新页面、扩展重启或普通心跳都不能触发快捷键。

## 架构与代码入口

- Reader 共享入口：`_server_deploy/static/pdf/rc-computer-voice.js`
- 双端 WebRTC 合同：
  `_server_deploy/static/reader-runtime/computer-voice-webrtc.js`
- 服务端状态、配对/信令、认证路由：
  `_server_deploy/computer_voice_bridge.py`、
  `_server_deploy/computer_voice_pairing.py`、
  `_server_deploy/computer_voice_routes.py`
- 扩展私有运行面：
  `extensions/bw-reader-webext/background.js`、
  `offscreen.html`、`offscreen.js`
- Windows Native Messaging：
  `extensions/bw-reader-webext/windows/ComputerVoiceAudio/`
- Windows 显式启用/回滚：
  `extensions/bw-reader-webext/windows/install-computer-voice-native-host.ps1`

Windows 不开放入站端口；只由扩展向 Pi 发经设备认证的 HTTPS 请求。Pi 只承载状态、命令和
WebRTC 信令，永不接收、记录或中继音频。媒体只在 Windows Native host → 扩展 offscreen →
Reader 浏览器间通过 WebRTC 传输。禁止默认麦克风、默认输出或全系统输出回退。

## 持久化与秘密边界

- 配对记录包含 revision、设备代际、过期/撤销状态；设备 token 只保存摘要。
- 扩展 token 只存在可信扩展存储，不回传 popup、内容脚本、Reader 页面或日志。
- Native host 配置只在 Windows 用户目录，固定记录扩展 ID、明确麦克风 endpoint、
  `process-only`、本地 opt-in 和 `Ctrl+Shift+C`。
- 服务端不能下发 Windows 路径、任意动作、快捷键或目标进程名。
- 重复 pairing、命令、ack 和信令都有 ID/游标/过期围栏；未知 mutation 结果不猜成功。

## 已验证证据（2026-07-28）

- Python 桥接、配对、路由与 supervisor 合同：**48/48 通过**。
- Reader/扩展 Native 协议、offscreen、WebRTC 和集成合同：**33/33 通过**；扩展 popup/provider/
  语音门禁相关定向回归：**77/77 通过**。
- 交互策略：`computer-voice.bridge.request` 是 `remote-required + direct + unavailable`，
  不进 outbox；网络审计 **0 new debt**。
- 扩展构建、部署清单、Windows/Safari 发布管线和完整 handoff 门禁通过。
- Windows 隔离 host：
  - 自包含 exe 的 `--self-test` **68 项通过**，`audioActivated=false`；
  - Native Messaging 无音频冒烟返回 `hello + capabilities`，错误扩展 origin 被拒绝；
  - 当前状态：
    `registered=false`、`localOptIn=false`、`extensionConfigured=false`、
    `hostPresent=true`、`typistHelperPresent=true`；
  - exe SHA-256：
    `8671ac8815e19a32e25f8bc515a0ccce6dff313c50762d6917db0596c84db4dc`；
  - 安装器 SHA-256：
    `cebc486690676e02e8c86c00b6aba75dd9082189ccd41be0a6035a69727d8b2e`；
  - typist helper SHA-256：
    `52ac819eae2b643bc828fd2d9785928554fc6b00115ed785a0b0497aadfc25d3`。

## 尚未执行

- 这些 Reader、服务端和扩展改动尚未部署/发布，也未加载到用户日常浏览器。
- 尚未用真实扩展 ID 运行 Windows 安装器 `Enable`；因此没有选择麦克风、写注册表或打开
  opt-in。
- 尚未进行首次真实配对、WebRTC 媒体验收或一次性电话按钮启动；这些步骤必须在候选部署/安装后，
  由用户可见地操作，不能用后台脚本代替。
- 当前无 TURN 音频中继；ICE 直连失败时应明确报错，不能静默改由 Pi 中转。
