# Reader ↔ Windows 电脑语音直连

状态以 `references/reader-collaboration-status.md` 的最新登记和现场命令为准。本文件
前半段保留了最初配对设计的历史推演；当前生产直连已经改为固定 Tailnet
identity、无需配对码，权威媒体合同见 `reader-computer-audio-bridge.md`。

2026-07-30 起的上下文末端复用已验收的 Windows WSS 直连，把 Reader 事件写成 Windows
本地快照，再由常驻 MCP 的 `reader_context_snapshot` 按需读取；
`reader_visual_image` 则向快照标识的当前在线来源临时请求合成图，不把大图常驻写入快照。
App 本机 Reader 的原生合成图沿同一视觉合同回传 Windows，不经过 Pi。旧 voice-typist 注入代码保留为
`legacy-inject` 回滚模式；当前实验为 `snapshot-mcp`，两条互斥。该实验不改已经通过
用户实测的麦克风、虚拟扬声器、快捷键或 PCM 链路。

## 目标

电脑语音的控制、状态、错误与媒体全部由 Reader/PWA 直接交给 Windows 桥接器。Pi 继续提供
书籍和 PWA 页面，但不再参与电脑语音的配对、心跳、启动命令、SDP/ICE 或媒体。

```text
Reader/PWA
    │  wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1
    │  控制 JSON + 有界二进制 PCM
    ▼
Tailscale Serve（仅 tailnet，TLS 终止）
    │  http://127.0.0.1:<固定本机端口>
    ▼
Windows 电脑客户端桥接器
    ├─ 显式选择的麦克风
    ├─ 仅 Codex/ChatGPT Desktop 目标进程树输出
    └─ 本机 opt-in、失败日志和停止边界
```

Windows 服务只监听 loopback；不得直接监听 LAN、`0.0.0.0` 或公网。Tailscale Serve 只在用户
明确启用服务后配置，停用服务时有精确回滚命令。Funnel 永不启用。

Windows 登录后保留一个无采音能力的轻量 bootstrap，使 Tailscale Serve 始终有可达的
loopback 目标。关闭桌面控制窗不等于关闭 bootstrap；bootstrap 空闲时不得打开 Codex/ChatGPT、
不得枚举或占用麦克风、不得启动进程音频捕获，也不得发送语音快捷键。若连 bootstrap 都没有
运行，远端 PWA 无法凭空启动 Windows 程序，只能明确显示“Windows 桥接器离线”。

## 配对与认证

1. Windows EXE 显式打开短期配对窗口并显示一次性配对码；Reader 不再生成配对码。
2. PWA 用 WebCrypto 生成不可导出的 ECDSA P-256 私钥，私钥只存 IndexedDB。
3. 配对请求只提交 SPKI 公钥和一次性配对码。Windows 消费配对码后只保存公钥、指纹、代际和
   撤销状态，不保存长期 bearer token。
4. 每条新 WebSocket 连接先由 Windows 发随机 challenge；PWA 对以下精确 UTF-8 字节串签名：
   `reader-computer-voice-auth/1\n<challengeId>\n<nonce>\n<Origin>`。认证成功前只允许
   `PAIR`/`AUTH`，不允许状态、启动或媒体。连接建立后 10 秒内未认证即
   `AUTH_TIMEOUT`；认证后 30 秒内未收到首个有效 `START` 即 `START_TIMEOUT`，普通状态读取、
   重复或无效消息均不能续期。
5. Windows 必须校验精确 HTTPS Origin；经 Tailscale Serve 进入时还要校验可信 identity header。
   本机隔离测试只能通过显式 test adapter 放宽，生产配置不得接受通配 Origin。

配对码、challenge、签名、私钥和音频不得写入日志。日志只保留时间、阶段、会话 ID 摘要和有界
错误码。

## 生命周期

- 选择“电脑客户端”只读取本地配置，不启动 Windows 应用、捕获 worker 或音频。
- 电话按钮的一次真实用户手势先同步解锁 PWA 音频，再只发送一次带幂等键的 `START`。
- 收到 `START` 后，Windows bootstrap 才可按顺序进入 `starting-service`、`starting-app`、
  `waiting-app-ready`。捕获 worker 未运行时启动 worker；配置的 Codex/ChatGPT Desktop 未运行时，
  只允许用登记的 packaged app identity 激活它，不从网页接受可执行文件路径或任意命令。
- 当前直连服务只接受固定 `appKind=codex-desktop`，并只激活
  `OpenAI.Codex_2p2nqsd0c76g0!App`；Reader 不提交 AUMID、可执行路径或命令。
  `OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT` 仅作为桌面控制器里的保留本机常量，
  尚未开放到当前服务合同。启动后必须重新归并进程树并等待唯一根目标就绪，超时、多根或
  身份不符均失败。
- Codex 的 `realtimeVoice` 是本机 OS-global hotkey。Windows 每次 START 都从当前用户
  `~/.codex/keybindings.json` 验证它唯一绑定为固定 `Ctrl+Shift+C`，不从 Reader 接收
  组合键，也不切换或抢占 Windows 前台；发送前只允许同一个唯一 packaged-app root，
  不因无关 Electron 子进程增减误拒绝。
- Windows 随后依次证明：本机 opt-in、PWA 已认证、目标进程树唯一、显式麦克风仍存在、输出范围
  仍是 `process-only`、voice-typist 可用。任一步失败都返回精确错误码并保持 `idle`。
- 只有 Windows 回 `active` 后 Reader 才显示通话中。失败或断线必须释放 Reader 的 audio surface
  和 active 状态；下一次点击是重新拨号，不得被误判为挂断旧会话。
- `STOP` 只停止本次捕获和传输，不停止或暂停用户原已运行的 voice-typist。
- START 尚未确认时，Reader 的再次点击不把 STOP 排在同一连接后面等待，而是立即关闭 WSS；
  Windows 在处理 START 的同时维持唯一一个预取接收，先观察 close、取消应用等待与媒体启动，
  再回滚已准备的 capture。非 close 消息最多预取一条并保持原顺序。
- 同一幂等键不得重复启动应用或发送语音快捷键。不自动重连；未知 mutation 结果保持
  pending/failed，禁止乐观重试。
- `START` 成功后 Reader 每 5 秒发送严格递增的会话 heartbeat；Windows 只允许 heartbeat
  续活，15 秒未收到下一序号即先停采集再关闭连接。状态读取、重复 START 和其它消息不能续活。

## 媒体合同

首个候选使用固定 20 ms PCM 帧，避免在 Windows 另引入原生 WebRTC/Opus 依赖：

- 48 kHz、mono、signed 16-bit little-endian；
- `app-output` 与 `user-mic` 两个固定 track ID；
- 每帧 960 samples / 1,920 bytes；
- 帧头包含合同版本、会话、track、严格递增 sequence 和单调 timestamp；
- 每轨和总队列都有硬上限；帧格式、会话和传输 sequence 缺口仍立即 fail closed；
- 浏览器麦克风上行的已验证帧若因网络/调度突发填满 200 ms 本地 render 队列，只丢最旧
  待播放帧、保留最新语音并记录丢帧，不让一次瞬时拥塞结束整通电话或累积陈旧延迟；
- PWA 只播放 `app-output`，`user-mic` 不回放，防止耳返。

PCM 只在 Tailscale 加密直连中出现，不写 Pi、磁盘、日志或浏览器持久存储。后续若切换 Opus，
必须另立版本合同并做听感、抖动、断线和资源上限验收，不能静默复用 PCM 版本号。

## 迁移与发布边界

- 旧 Pi `computer-voice` 路由、扩展 offscreen 和 Native Messaging 先保留为未启用回滚路径；
  新直连通过前不删除。
- 新 Reader 设置已配置时只显示真实直连状态与刷新/断开，不显示“生成一次性配对码”。
- Windows 隔离测试使用 mock capture 与 loopback WSS，不启动真实音频或快捷键。
- Windows 候选由 `windows/package_computer_voice_direct.py` 在仓库忽略的
  `windows/candidates/<version>` 下构建；ZIP 只允许 manifest 与固定 payload 白名单。
  `--build`、`--verify` 和包内 `--self-test` 都不是安装动作；实际替换只走同一脚本的
  `--install`，它会先验证并备份当前 payload，精确静默 owned 服务与同路径 Reader MCP，
  再替换、自检和恢复服务，失败自动回滚。人工回退使用 `--rollback <backup-dir>`，不得手工
  覆盖运行中的 EXE；配置、运行状态与 bundled .NET 不属于 payload。
- 登录 bootstrap 的安装必须显式 opt-in、可查询、可精确卸载；开发与测试只验证任务定义和命令
  构造，不在本轮创建计划任务或开机项。
- 真机验收顺序：Windows 服务可见启用 → PWA 配对 → idle 状态 → 用户点击电话按钮 →
  app-output 听感 / mic 活性 / 明确停止。任何失败都先保存错误码，再停止。
- Reader/PWA 生产部署仍必须先提交、推送，再从 Windows 运行项目远程预检与部署流程；不得直接
  修改 Pi 工作树或生产文件。

## 2026-07-29 Windows 候选证据

- Reader 源码与生成 vendor 已切到固定
  `wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1`；同 tailnet 其它节点也会被
  拒绝。完整 Reader 合同 578/578，网络审计 `0 new debt`。
- Windows C# 服务 Release build 为 0 warning / 0 error；无音频自检 107 项通过，
  `audioActivated=false`。桌面控制器隔离测试 63/63，打包器安全测试 10/10。
- 本地不可变候选：
  `windows/candidates/0.1.0/bw-computer-voice-direct-0.1.0-windows-x64.zip`，
  SHA-256 `f05f50c663d0f0e459ef6c2de68006fa13d2cefbe9c4a0e672d3034d47f024ae`，
  53,772,804 bytes。ZIP 只含 manifest 与两个 EXE；包内双 EXE `--self-test` 通过。
- 固定 `PYTHONHASHSEED=0` 与 `SOURCE_DATE_EPOCH=315532800` 后，第二次独立构建的两个 EXE
  与 0.1.0 哈希逐字一致；复现探针已删除，只保留 0.1.0。
- 0.1.0 的两个 EXE 已原子替换到固定安装目录；替换前版本保存在
  `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T003728326Z`。安装后从
  固定路径运行双自检均通过，音频服务仍报告 `audioActivated=false`。
- 用户随后从桌面 EXE 手动启动服务；本机 `127.0.0.1:43128` 正常监听，runtime 状态为
  `idle / readerConnected=false / captureActive=false`。登录 bootstrap 任务仍不存在，
  Tailscale Serve 仍为 `{}`；启动本机 listener 不等于已建立远端配对或开始采音。
- 浏览器无法唤醒一个完全不存在的 Windows listener。产品保证是：用户一次明确安装并启用
  登录 bootstrap 后，服务空闲常驻且不采音；认证的电话 `START` 才自动打开 Codex、启动
  明确麦克风与进程音频。真实麦克风、扬声器、快捷键和 iPad/PWA E2E 仍待用户可见验收。
- 仍有低风险本机 TOCTOU：Windows Task Scheduler CLI 只能在 ownership 查询后按任务名
  `/Run`/`End`/`Delete`；Tailscale Serve CLI 也没有查询结果 ETag。两者均做严格前后检查并
  对混合状态 fail closed，但同权限进程并发替换时不能提供内核级原子身份保证。
