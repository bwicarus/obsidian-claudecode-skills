# Windows 电脑语音直连桌面启动器

这是外部原型
`C:\Users\bwica\bw-computer-voice-bridge\desktop-launcher\src`
的仓库内源码版本。目录是有界的：它不依赖浏览器扩展、
offscreen document、Chrome、CDP、Native Messaging 或 Pi 音频中继。

本次只整理源码与隔离测试，没有复制到外部安装目录，没有启动服务、
采集音频、启动 GPT、发送快捷键、注册计划任务、写注册表、执行
Tailscale Serve 或部署 Reader。

## 目标拓扑

```text
Reader/PWA
    ⇅ wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1
Windows Tailscale Serve
    ⇅ 127.0.0.1:43128
Windows C# direct server（空闲时只监听）
    ├─ Reader 网页麦克风 PCM → 虚拟麦克风 A 播放端
    │                              └─ A 录音端 → Codex 输入
    └─ Codex 输出 → 虚拟扬声器 B 播放端
                    └─ 仅捕获 Codex 进程树 → Reader 扬声器
```

Pi 继续提供 Reader/PWA、书籍和 outgoing journal 数据，但不代理 Windows
WSS、控制消息或 PCM 媒体。
浏览器只能连接已经存在的 Windows listener，不能在完全无 listener、电脑
睡眠或 bootstrap 未安装/未运行时凭空唤醒本机进程；这时 Reader 必须显示
“Windows 桥接器离线”。一次显式安装登录后台引导器后，listener 才能在
Windows 登录会话中被持续监督，而电话 `START` 只负责按合同启动 Codex、
捕获与快捷键阶段。

两个虚拟设备都从本机 **Active eRender** endpoint 枚举，必须逐一明确
选择、非空且互不相同，没有 default fallback。A 由 bridge 写入；B 只作为
Codex/ChatGPT 的应用输出目标，bridge 不向 B 写入。B 的录音侧不接物理
扬声器，所以 RDP 重定向或真实扬声器不会承担这条通话输出链。

枚举框显示的是 Windows 当前已有的全部 Active eRender endpoint；窗口中的
“虚拟麦克风 A / 虚拟扬声器 B”是桥接角色名，不表示程序已经创建了设备。
项目不安装驱动，也不能把 Realtek、Steam、Oculus 等现有端点冒充为两根独立线缆。
没有两根已签名虚拟线缆时必须停在安装门，不能保存占位配置。

端点存在不等于应用已路由。严格 `/5` 另需安装时明确写入 A 的 Active eCapture
MMDevice ID；桌面窗口不会根据名称或设备关联自行猜测。`/5` 的 C# 在 START 内使用
Windows 内部 AudioPolicyConfig 只切换精确 Codex PID 的六项应用端点，并在 STOP
精确恢复，不修改系统全局默认设备或打开设置 UI。C# 同时在 B 上通过公开 Core Audio
session API 观察当前 Codex 进程树；
只有出现当前 Active session 才报告 route verified，Inactive/Expired 历史 session
不算；未观察到时保持
`BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_UNVERIFIED`。这条正向证据仍不能代替
最终有声 E2E。旧 `/4` 保留“打开 Windows 音量混合器”的手工回滚路径，不进行自动路由。

## UI 的三个状态

三个状态彼此独立，不能互相推断：

1. **配置已启用**：严格 direct config 存在、合同有效且
   `localOptIn=true`。
2. **直连服务在线**：服务记录中的 PID 指向固定
   `bw-computer-voice-audio.exe`，并且 C# 原子状态文件合同、PID、
   state 和 15 秒新鲜度全部通过。
3. **Reader 已连接**：只有“直连服务在线”成立且新鲜状态文件明确
   `readerConnected=true` 才成立。

配置存在、进程存在、旧状态文件或页面自称 connected 都不能冒充后两项。
`faulted`、`stopping`、`stopped` 也不算在线。

## 固定路径与命令

生产安装根固定为：

```text
C:\Users\bwica\bw-computer-voice-bridge
```

相对根目录的固定文件：

```text
native-host\bw-computer-voice-audio.exe
native-host\computer-voice-direct.config.json
runtime\computer-voice-direct.status.json
runtime\computer-voice-direct.service.json
desktop-launcher\BW-Computer-Voice-Bridge.exe
```

显式点击“启用并启动”时，先原子保存 strict config 的
`localOptIn=true`。随后：

- 同名任务已通过当前 SID、description marker、exact action/args 的完整
  ownership 复核时，只调用：

```text
schtasks.exe /Run /TN "BW Computer Voice Direct Bootstrap"
```

- 任务确实不存在时，才直接调用
  `<exact-native-host> --direct-serve --config <exact-direct-config>`，
  并在 UI 明确显示“本登录未受后台 supervisor 保护”。
- 同名任务存在但 ownership 未通过时，不 `/Run`、不写 opt-in、也不旁路
  启动 C#。

所有命令不用 shell。直接启动和停止均走可注入 `ProcessRunner`；Windows
停止会在同一个 `QUERY_LIMITED_INFORMATION | TERMINATE` 进程句柄上先复核
完整 EXE 路径、再终止，避免两次打开之间的 PID reuse。偏离时 fail closed。
状态刷新、两个播放端点选择和独立的麦克风录音端选择不会调用 `ProcessRunner.start()` 或
`schtasks /Run`。

计划任务的固定动作不是任意命令，而是无控制台桌面启动器：

```text
<exact-desktop-launcher> --bootstrap
```

该入口是常驻空闲 supervisor。strict config 缺失、失效或
`localOptIn=false` 时它 fail closed 且零启动；启用时持续监督已有的精确
PID，不会看到 online 就退出或再起第二实例。child 正常退出和异常退出都
按 `1/2/5/10/30` 秒封顶退避重启；连续 3 次轮询仍只有精确 owned PID、
没有新鲜 heartbeat 时，才安全停止该 PID 并重启。陌生 PID 不会被停止、
覆盖或冒充。它本身不采音、不启动目标应用，也不发送快捷键。

GUI 不再提供会被 supervisor 立即抵消的单独“停止”按钮。“停用并停止”
先原子写入 `localOptIn=false`，成功后才按 PID + EXE 双重校验停止服务；
无法先写入有效 opt-out 时拒绝停止。为封住 GUI 与独立 bootstrap 进程之间
“已读到 opt-in、尚未发布 PID”的启动临界区，启动器写入 owned PID record
后必须再次读取 strict config：若此时已 opt-out，就用同句柄精确停止刚启动
的 child 并清理该 PID 的 owned record；前台停用路径也会做总计小于一秒的
有界 record 重查。两种时序保证成功回执时本轮 listener 不会在停用后遗留；
若同句柄终止或最终 record 证明失败，则保留 owned record、返回错误且不把
结果冒充成功（`localOptIn=false` 仍先落盘）。

## direct config 合同

自动路由合同为 `reader-computer-voice-direct-config/5`，字段集合必须恰好为：

```json
{
  "contract": "reader-computer-voice-direct-config/5",
  "localOptIn": true,
  "experimentalSingleUserMode": true,
  "virtualMicrophoneRenderEndpointId": "{explicit-virtual-microphone-render-endpoint-id}",
  "virtualMicrophoneCaptureEndpointId": "{explicit-virtual-microphone-capture-endpoint-id}",
  "virtualSpeakerRenderEndpointId": "{explicit-virtual-speaker-render-endpoint-id}",
  "listenHost": "127.0.0.1",
  "listenPort": 43128,
  "allowedOrigins": [
    "https://bwicarus.taile44d0c.ts.net"
  ],
  "allowedTailscaleUserLogin": "bwicarus@gmail.com",
  "outputScope": "process-only",
  "appKind": "codex-desktop",
  "runtimeStatusPath": "C:\\Users\\bwica\\bw-computer-voice-bridge\\runtime\\computer-voice-direct.status.json",
  "contextDeliveryMode": "snapshot-mcp"
}
```

- `listenHost` 必须逐字为 `127.0.0.1`，端口固定为 `43128`。
- `allowedOrigins` 中的显式条目只接受无路径、查询或 fragment 的 HTTPS
  origin；实验模式另允许规范的 HTTP(S) 当前页面 Origin。
- `allowedTailscaleUserLogin` 必须逐字为已确认的
  `bwicarus@gmail.com`，GUI 和 Reader 都不能提供替代值。
- `experimentalSingleUserMode=true` 是当前单用户实验模式：浏览器使用
  direct v3，不需要配对码、设备密钥或 WSS endpoint 输入。任意规范的 HTTP(S)
  页面 Origin 都可到达握手，权限边界改由固定 WSS + Tailscale 注入的唯一
  用户身份 + Windows 本地 opt-in 共同承担。
- `outputScope` 固定 `process-only`，没有系统输出回退。
- `contextDeliveryMode` 只接受 `legacy-inject` 或 `snapshot-mcp`。前者保留
  named-pipe → voice-typist 的旧文字注入末端；后者把 PWA 直连事件原子折叠到
  Windows `runtime/reader-context-snapshot.json`，START 明确跳过 typist，并由同一个
  原生 EXE 的只读 MCP 模式按需返回快照。两条路径互斥。
- `/5` 才启用实验性的 Codex 按应用快速切换：直接调用 Windows 内部
  AudioPolicyConfig，不打开音量混合器或设置 UI，不修改系统默认设备；START 前快照
  render/capture × Console/Multimedia/Communications 六项并逐项读回，Voice 关闭后
  只在当前值仍等于桥目标时恢复原值。
- 旧 `/4` 只作为 `no-route-automation` 兼容配置加载；它没有 capture ID，绝不从
  render ID、名称或 association 属性推导。桌面 GUI 用第三个只读下拉框独立枚举
  Active eCapture；已有 `/5` 只按完整 endpoint ID 精确回显。下拉框首项是明确的
  “不启用自动路由（兼容 `/4`）”，保存前仍须在确认框中确认；配置中的 capture
  当前失活时保持空白并拒绝保存，不能把失活误当成用户选择了 `/4`。
- `virtualMicrophoneRenderEndpointId` 与
  `virtualSpeakerRenderEndpointId` 必须是两个不同的 Active eRender
  endpoint；空值、同值、失活或默认设备推断全部拒绝。
- `virtualMicrophoneCaptureEndpointId` 必须是另行明确选择的 A eCapture
  MMDevice ID；render/capture 方向交叉或缺失均拒绝。
- 旧配置中的 `microphoneEndpointId` 只显示显式迁移提示。runtime、
  supervisor 和启动路径绝不把它当作 v3 fallback；只有用户选好 A/B 并
  另行选好 A 的 eCapture、再次确认保存时才迁移为 `/5`；不选 capture
  只能明确迁移为无自动路由的 `/4`。
- `appKind` 固定 `codex-desktop`。Reader 不能提交路径、命令或 AUMID。
- 本机应用 allowlist 只在代码内维护：
  - Codex：`OpenAI.Codex_2p2nqsd0c76g0!App`
  - Classic：`OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT`

当前 strict config 只选择 Codex；Classic ID 仅作为本机枚举预留，不能由
Reader 选择。

## 浏览器免配对模式

GUI 不提供配对码、重新配对、浏览器 endpoint 或设备身份配置。Reader/PWA
和扩展页面都连接代码内固定的
`wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1`，先发送
`protocolVersion: 3` 的 HELLO，再由电话按钮发送 START。浏览器不生成、
保存或提交 ECDSA 密钥、设备 token 或 bearer token。

为支持扩展运行在任意网页，实验模式允许规范的 HTTP(S) Origin，而不只允许
Reader 域名。任何知道固定 WSS 的已访问页面都可能尝试 START，这是单用户
实验模式的明确取舍；C# 仍在 upgrade 前要求 Tailscale Serve 注入的单值
`Tailscale-User-Login` 精确匹配，并保留唯一连接、固定 localhost、Windows
本地 opt-in、明确双虚拟端点、Codex 进程树输出、START-only 激活及
heartbeat/close fail-closed。

direct v3 的浏览器麦克风帧只写入 A。在 `legacy-inject` 模式下，direct 媒体
adapter 还把本次 START 新建的 voice-typist 绑定到同一通话生命周期：
正常挂断、浏览器断开、心跳超时、START 失败、媒体异常和服务退出都会通过固定 launcher
执行 `Stop`。只有 helper 返回 `started` 且结束时 PID 与 process-start FILETIME
都仍精确匹配才拥有停止权；
`already-running` / `raced-running` 不会被桥接器停止。
bridge-owned typist 也持续核对 bridge owner 的 PID + process-start FILETIME；即使
C# 进程整体崩溃、无法执行 Stop，它也会自行退出。managed 启动不再受 10 分钟 idle
误杀；无 owner 的手工启动仍保留 600 秒孤儿兜底。

在 `snapshot-mcp` 模式下，音频 START 顺序和已验收的 PCM/快捷键路径不变，但
`StartTypist=false`；Reader 的 `context` / `active-reading` 只更新 Windows 本地
JSON，绝不进入客户端输入框或 typist 管道。MCP 进程由客户端通过以下固定只读入口
常驻启动：

```powershell
bw-computer-voice-audio.exe --reader-context-mcp --state `
  C:\Users\bwica\bw-computer-voice-bridge\runtime\reader-context-snapshot.json
```

工具面只有 `reader_context_snapshot`。快照超过三分钟未收到 PWA
`active-reading` 心跳时，工具返回 `contextStatus=stale`，并清空可供回答的正文与选区；
活动心跳还会明确区分有选区、已清空和未上报，不能沿用旧选择。关闭同步或切回旧注入时，
PWA 会先用无音频副作用的 `context-clear` 清空快照，再停止实验末端。

安装后的 Codex CLI 注册形式如下（这是安装/配置动作，候选验收前不执行）：

```powershell
codex mcp add reader_snapshot -- `
  C:\Users\bwica\bw-computer-voice-bridge\native-host\bw-computer-voice-audio.exe `
  --reader-context-mcp --state `
  C:\Users\bwica\bw-computer-voice-bridge\runtime\reader-context-snapshot.json
```

## 语音聊天侧栏同步

同步核心与桌面启动器一起进入 PyInstaller 单文件 EXE，不依赖开发仓库或
系统 Python。已安装计划任务运行 `--bootstrap` 时，同步监视器与既有空闲
supervisor 共用同一进程；没有计划任务而由 GUI 直启时，桌面 EXE 派生一个
固定的无界面 `--history-sync-worker`。Windows 命名 mutex 保证同一安装根
最多一个发布者。

监视器只信严格 config 与 runtime status：必须同时满足
`localOptIn=true`、`contextDeliveryMode=snapshot-mcp`、
`serviceOnline=true` 和 `captureActive=true`。旧注入模式、未知/损坏配置或服务离线
都立即停用并丢弃内存 lease，不做最终发布。每次捕获从 false 变为 true 的第一次轮询
只归档当前水位并绑定这个 exact local thread，不发布此前历史，也不把激活前留下的
`user` 与激活后出现的 `assistant` 拼成一轮；随后只发布该 lease 内、水位之后新增的
相邻 `user`/`assistant`。捕获结束后有三次有界尾随，以接住稍晚落盘的最后一轮。

稳定 requestId 与 durable published 表保证重试不重复；线程绑定在通话中变化时
fail closed。任何包含 `[[READER_SYNC]]` / `[[/READER_SYNC]]` 的文本在归档输入与
最终 transport 两层都会拒绝。快照 anchor 仅在 `contextStatus=ready`、三分钟内更新、
`activeReading.fresh=true` 且 `currentPage.stable=true` 时贡献安全的 `file/page`；
否则不带 anchor，由 Pi 按现有规则解析。同步始终只调用现有 `assistant_turn`，不会从
回答文本猜测 weather/news/card，不调用 MCP，也不把 Reader 正文、选区或图像送回客户端。
结构化结果仍只由独立 `reader-result/1 → result.present` 直接命令触发。

v3 strict config 不包含任何 pairing 字段。旧 `/1`、
`microphoneEndpointId` 与四个 pairing 字段只用于识别
`legacy-migration-required`；显式迁移后全部移除，不能进入 runtime。

## runtime status 合同

C# 原子写
`reader-computer-voice-direct-runtime-status/2`，字段必须恰好为：

```text
contract / serviceInstanceId / pid / state /
readerConnected / captureActive / lastError / updatedAtUtc
```

`serviceInstanceId` 是 UUID N（32 个小写十六进制字符）。在线 state：

```text
starting
idle
reader-connected
starting-app
waiting-app-ready
starting-capture
active
```

空闲 `idle` 只监听；不得采音、启动 GPT、启动 typist 或发送快捷键。
只有通过固定 WSS/Tailnet 身份门禁的 Reader 电话 `START` 才能请求这些
后续动作。C# 已接入当前
生产 capture 适配器，但 Windows ↔ Reader/PWA 的真实设备 E2E 尚未验收；
在验收完成前不能把 listener 在线或 mock 通过冒充通话可用。

`lastError` 为 `null` 或严格对象：
`failureId / code / stage / hresult / atUtc`。桌面窗口只显示安全错误代码、
阶段和可选 HRESULT，不显示 endpoint ID、origin 或 PCM。双端点配置不完整、
相同、失活或仍为旧 `microphoneEndpointId` 时，配置不算 ready，listener
不会启动。

## 登录后台引导器

GUI 提供“安装/回滚登录后台引导器”两个显式按钮，每次 mutation 前都
二次确认。刷新只运行 `whoami /USER /FO CSV /NH` 与固定任务名的
`schtasks /Query ... /XML`，不会创建、覆盖、结束或删除任务。

安装时只在受控临时目录生成固定 XML，然后用 `shell=False` 的 exact argv
调用 `schtasks.exe`：

- 任务名固定为 `BW Computer Voice Direct Bootstrap`；
- 当前用户 `LogonTrigger` + `InteractiveToken` + `LeastPrivilege`；
- 隐藏运行，动作固定为 exact 桌面启动器 `--bootstrap`；
- `StartWhenAvailable=true`，supervisor 自身异常退出后每分钟重启，
  最多 3 次；
- `/Create` 不带 `/F`；任何同名任务存在时都拒绝覆盖；
- `schtasks` 返回码 1 本身不算“未安装”；还必须出现系统明确的
  not-found 文本，否则按查询失败处理，禁止 direct fallback；
- 创建后重新读取 XML 做完整 ownership 校验；若 post-check 看到未知
  同名任务，拒绝用自动 rollback 删除它；
- 回滚前必须证明现有 XML 完全属于本启动器，再精确 `/End`、`/Delete`，
  未知同名任务拒绝改动。
- 显式启动每次都重新做相同 ownership 复核，只有通过后才能 exact
  `/Run /TN BW Computer Voice Direct Bootstrap`；`IgnoreNew` 防止任务
  自身重复实例。

剩余 P2 边界：`schtasks` 的 XML 查询与后续按名称 `/Run`、`/End`、`/Delete`
不是同一个原子对象句柄；若另一个拥有当前用户权限的进程恰好在两者之间替换
同名任务，仍存在 name-based TOCTOU。当前实现不会因而扩大权限，但不能把
“查询时 ownership 通过”表述成对并发替换的绝对证明；需要后续改用可绑定
对象身份/ACL 的 Task Scheduler API 才能彻底关闭。

XML 只会出现在临时目录；mock 测试退出临时上下文后确认文件已清除。本次
开发没有真实注册、运行、结束或删除任何计划任务。

## Tailscale 边界

GUI 提供路径级 Serve 的“应用/回滚”显式按钮，每次 mutation 前都二次
确认；刷新只运行 `serve status --json`。固定 exact argv 为：

```text
tailscale.exe status --json
tailscale.exe serve status --json
tailscale.exe serve --yes --bg --https=443 --set-path=/reader-computer-voice/v1 http://127.0.0.1:43128/reader-computer-voice/v1
tailscale.exe serve --yes --bg --https=443 --set-path=/reader-computer-voice/v1 off
```

后端 target 也带同一路径，因为 path mount 会从转发请求剥离外部前缀，
而 C# 只接受 `/reader-computer-voice/v1`。helper 先解析完整
`ServeConfig` JSON：owned 状态必须同时且只包含
`TCP["443"].HTTPS=true`、固定
`Web["bwicarus-2.taile44d0c.ts.net:443"]`、唯一 handler 和固定 backend。
任何其它 host/port、TCP forward、`AllowFunnel`、`Foreground`、`Services`、
额外 handler 或未知结构都算 foreign，不能 apply/off。只有空配置可以
apply；只有完整 owned 配置可以 rollback。apply/off 后都重新查询；若 apply
后的完整 ownership 无法证明，不对混合或未知配置追加 `off` mutation。

`--yes` 防止首次启用 HTTPS 时在隐藏进程里等待交互；GUI 自己承担显式
二次确认。默认 runner 始终 `shell=False`、`stdin=DEVNULL`，没有用户可控
参数。mock 测试只通过显式 GUI 按钮路径触发 mutation helper；取消确认、
刷新、麦克风选择和固定
`appKind` 不触发任务或 Serve mutation。本次开发没有执行真实 Serve
apply/off。

## 隔离测试

在 Windows 开发机运行：

```powershell
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe `
  -m unittest discover `
  -s C:\claude\extensions\bw-reader-webext\windows\computer-voice-desktop\tests `
  -p "test_*.py" -v
```

安装后可运行无副作用自检：

```powershell
BW-Computer-Voice-Bridge.exe --self-test
```

自检只检查固定文件落点和内存中的合同构造；输出明确标记配置写入、服务
启动、采音、应用/typist/快捷键、任务/注册表、Tailscale Serve 和浏览器
操作全部为 `false`。

覆盖：

- GUI 不出现配对码、重配对或浏览器 endpoint 控件；
- A/B 均来自 native `--list-direct-render-endpoints` 的 Active eRender
  清单，必须明确选择且互不相同，无默认回退；
- 旧 `microphoneEndpointId` 只触发显式迁移提示，不能静默启动；
- runtime status v2 严格校验 `lastError`，UI 不泄露 endpoint；
- offline/stale/PID 不匹配不能显示 online 或 Reader connected；
- direct config v5 严格字段、`/4` no-route 回滚、双模式互斥、实验单用户 v3、
  固定 Tailscale 登录身份且无
  浏览器长期 token；
- 无 Chrome/CDP/WebSocket 运行依赖；
- START/STOP 只经过注入 runner 且使用严格路径；
- supervisor 在 opt-out 时零启动，正常/异常退出都封顶退避重启，已在线
  时持续监督且不双启，连续无心跳只重启精确 owned PID；
- “停用并停止”严格先写 `localOptIn=false` 再停止，陌生 PID 不杀、不
  覆盖、不冒充；覆盖 opt-out 落在启动 record 发布前后的竞态；
- 状态刷新不启动服务；
- 计划任务与 Serve 只接受固定 argv、严格 ownership/postcondition，
  默认 runner 明确 `shell=False`；
- Serve ownership 覆盖 Web host/port、HTTPS TCP handler、Funnel、
  Foreground/Services、额外 handler 与未知结构；混合状态不会执行 off；
- mutation 全部要求 GUI 二次确认；所有测试 runner 均为 mock，没有执行
  真实任务、Serve、服务、音频、应用或快捷键。
