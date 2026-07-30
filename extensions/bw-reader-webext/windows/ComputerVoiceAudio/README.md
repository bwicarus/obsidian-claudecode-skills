# BW 电脑客户端原生音频与直连服务

这是“电脑客户端”语音桥的 Windows 原生边界。direct v3 只服务固定的单用户
Reader/PWA 与扩展路径：

- Reader 当前网页麦克风经固定 WSS 上行，写入显式虚拟线缆 A 的 Active
  `eRender` endpoint；
- Codex 进程树输出继续使用 Windows application process-loopback，经同一 WSS
  下行到 Reader；
- Reader outgoing context 按 strict config 二选一：经 `CurrentUserOnly` named
  pipe 交给 voice-typist，或只写 Windows 本地快照供只读 MCP 按需读取；
- 服务启动、HELLO、STATUS、endpoint 枚举和自检均无启动副作用；只有通过全部本地
  门禁的 START 才能启动 Codex、typist、媒体与一次语音快捷键。

安装、部署与真实音频验收事实以
`references/reader-collaboration-status.md` 的最新登记和现场状态命令为准；源码自检
不冒充真实 E2E。Native Messaging 宿主和旧 capture 麦克风代码仅为历史回滚面，不是
direct v3 媒体路径。

## 启动与配置

```powershell
.\bw-computer-voice-audio.exe --direct-serve --config `
  C:\Users\bwica\bw-computer-voice-bridge\native-host\computer-voice-direct.config.json
```

服务只绑定 `127.0.0.1`；预期由 Windows Tailscale Serve 在 tailnet 内终止
TLS/WSS 并反代固定路径 `/reader-computer-voice/v1`。C# 不修改 Tailscale、防火墙、
默认音频设备或系统代理。`/5` 实验配置只在一次 Codex Voice 生命周期内通过 Windows
内部 AudioPolicyConfig 接口切换 Codex 自身的应用输入/输出路由，不打开设置 UI，
不改其他应用；结束后按事务快照精确恢复。

strict config contract 为 `reader-computer-voice-direct-config/5`，字段必须恰好为：

```text
contract
localOptIn
virtualMicrophoneRenderEndpointId
virtualMicrophoneCaptureEndpointId
virtualSpeakerRenderEndpointId
listenHost
listenPort
allowedOrigins
allowedTailscaleUserLogin
experimentalSingleUserMode
outputScope
appKind
runtimeStatusPath
contextDeliveryMode
```

硬约束：

- `listenHost` 必须逐字等于 `127.0.0.1`；
- `experimentalSingleUserMode` 当前必须为 `true`；`false` 直接 fail closed；
- `outputScope` 只能为 `process-only`，`appKind` 只能为 `codex-desktop`；
- `contextDeliveryMode` 只能为 `legacy-inject` 或 `snapshot-mcp`，两条末端不同时运行；
- `/5` 的 A/B `virtual*RenderEndpointId` 必须是明确的 eRender MMDevice ID，
  `virtualMicrophoneCaptureEndpointId` 必须另行明确配置为 A 的 eCapture MMDevice ID；
  capture ID 绝不从 render ID、设备名称或 association 属性推导；
- A/B endpoint ID 必须非空、无控制字符、互不相同，并逐字传给
  `IMMDeviceEnumerator.GetDevice`；
- `/5` 才启用实验性的 Codex 按应用路由事务；旧 `/4` 仅作为
  `no-route-automation` 回滚配置加载，缺少 capture ID 时不会切换任何应用设备；
- 不存在 first/default endpoint、物理/RDP 麦克风或全系统输出 fallback；
- config 不含 pairing code、客户端公钥、旧 `microphoneEndpointId` 或认证兼容字段。

旧 `/1` 配置不由原生服务迁移。桌面控制面只能把它识别为
`legacy-migration-required`；没有显式 capture ID 时只能生成 `/4`
`no-route-automation` 配置，不能猜测并静默升级为 `/5`。

## 来源与身份边界

浏览器不使用配对码、bearer token、endpoint 输入或浏览器长期设备身份，但 WSS 仍受
两层固定门禁：

- `Origin` 必须是允许的生产 PWA 或项目扩展 canonical Origin；
- Tailscale Serve 注入的 `Tailscale-User-Login` 必须为单值，并与 config 中的唯一
 登录账号按 `OrdinalIgnoreCase` 精确匹配。

缺失、多值或不匹配均在 WebSocket upgrade 前返回 403；header 值不写日志。一次只允许
一个 Reader WebSocket 和一个 Active session。普通网页只能经扩展 isolated runtime →
background 的固定 relay，不能把 URL、设备 ID、AUMID、进程、路径、命令或快捷键传给 C#。

## direct v3 控制合同

所有文本消息使用 `reader-computer-voice-direct/1`：

```json
{"contract":"reader-computer-voice-direct/1","type":"hello|status|context-mode|context-mode-set|context-open|start|heartbeat|context|active-reading|context-clear|stop","requestId":"..."}
```

- 新连接先发严格 `hello`，`protocolVersion` 必须为 `3`；成功后进入等待 START。
- `context-mode` 只读返回该连接在 HELLO 时锁定的模式。`snapshot-mcp` 可用
- `context-mode-set` 仅允许在未启动通话的已认证连接上切换两种模式；
  从 `snapshot-mcp` 切回旧注入时，Windows 必须先清空本地快照，再原子写配置。
  `context-open` 建立无 START、无应用/采音/快捷键副作用的纯上下文连接；此阶段没有
  30 秒 START deadline。
- `status`、模型选择和刷新无副作用。
- START 的 `sessionId` 为 `session-` 加 16 随机字节的 22 位无 padding base64url。
- `legacy-inject` 的 START 顺序为：
  `localOptIn → A/B endpoint probe → ensure app → wait unique ready →
  validate A capture → attach B route observer →
  recover/acquire exact six-role app routes →
  validate local realtimeVoice binding → start typist →
  start A render → start process-loopback → revalidate endpoint/media/binding →
  global shortcut → commit/pump`。
  每个副作用前都有取消围栏；B route observer 只观察公开 Core Audio session，
  与负责六项持久应用端点的事务对象分离，且“尚未观察到”不会阻止首次 START。
- `snapshot-mcp` 使用同一条已验收音频顺序，但明确跳过 `start typist`；
  `context` 与 `active-reading` 原子写
  `runtime/reader-context-snapshot.json`，不调用 named pipe。
- 关闭同步或切回 `legacy-inject` 时先发 `context-clear`；它只清掉本地页面/选区，
  不启动或停止应用、音频、快捷键和 typist。
- 当前 Codex 把 `realtimeVoice` 注册为 `os-global`；桥只接受当前用户
  `~/.codex/keybindings.json` 中唯一的 `Ctrl+Shift+C` 绑定，且不切换、置顶或校验
  Codex 前台窗口。动态 Electron 子进程增减不改变固定 packaged-app root 的身份；
  root 变化仍在发送前 fail closed。
- `SendInput` 使用完整的 Win32 `INPUT` union（mouse/keyboard/hardware），自测按当前
  架构断言 native size、union size 与 union offset；仅以 fake sender 验证按键顺序
  不能代替这项 ABI 检查。
- 重复同 session START 幂等；其他 session 在 owner 存活时返回 busy。
- START 成功后，每 5 秒发送从 1 开始严格递增的 heartbeat；15 秒超时停止 owned
  资源并关闭连接。STATUS、重复 START 和刷新不能续期。
- STOP、peer close、心跳超时、START 中途失败、媒体 fault 或服务 dispose 都走同一
  best-effort teardown。只有 helper 证明为本次 START 新建且 exact PID + process
  start FILETIME 仍匹配的 typist
  才形成 stop lease；原本已运行或竞态启动的 typist 不归本次会话停止。
- `/5` 的路由 lease 在两条音频 session 停止并释放后逆序恢复六项；每项只有在
  当前值仍等于本桥目标时才写回快照值，用户或其他程序期间做出的外部改动保持不动。
  原值为 unset 时只删除该应用该角色的 exact pair，绝不调用全局 ClearAll。
- 无活动音频 session 时，内部 AudioPolicyConfig 的 PID 接口会返回
  `0x80070057`，因此它不作为 `/5` 的持久路由真值。桥用
  `GetApplicationUserModelId` 取得稳定 AUMID，再在当前用户
  `Multimedia\Audio\DefaultEndpoint` 中按默认值唯一匹配应用键；每个角色同时验证和写入
  endpoint 主值与 `_p` property-set ID，后者只从对应 MMDevice 的只读属性取得。
  Windows“重置声音设备和音量”后应用键可能完全不存在：此时六路快照明确视为
  `unset`，首个已写事务日志的写操作才按 Windows 已观察到的 32 位 `hash * 33 + UTF-16`
  身份键规则创建 AUMID 键并读回确认；恢复 `unset` 时只删除六组 endpoint pair，保留
  仅含 AUMID 的空身份键供下一次使用和音量合成器识别。这个键名算法是本机实测兼容实现，
  不是 Microsoft 公布的稳定合同；已有键仍一律以默认 AUMID 为权威，重复身份、值类型或
  pair 不一致、设备属性缺失均在发快捷键前 fail closed。
- bridge-owned typist 同时获得 bridge 进程的 exact PID + process-start FILETIME，
  运行时持续核对 owner 代次；C# 整体崩溃、PID 消失或复用时 typist 自退。managed
  模式不使用 10 分钟 idle 误杀，手工无 owner 启动仍保留 600 秒孤儿兜底。
- 服务不读取 Codex 蓝色 Voice 球或其他 UI 状态；它只读
  `CapabilityAccessManager\ConsentStore\microphone` 中 Codex 包的 capability-use
  起止时间戳，把它作为“该包正在使用麦克风”的代理信号。这个注册表信号不是 Codex
  官方 Voice/蓝球 API，也不证明具体 UI 形态。
- START 以该代理信号确认新 Voice generation，并持续监听本地关闭或 generation
  替换。STOP 只会在 Voice 仍属桥接器开启的同一 generation、且 Codex 根进程代次仍
  相同时发送一次关闭快捷键；预先存在、已经关闭或被新 generation 替换的 Voice
  一律不切换。

## Windows 本地快照 MCP

`--direct-serve` 在现有 `127.0.0.1:43128` 监听器内同时提供
Streamable HTTP MCP：

```toml
[mcp_servers.reader_snapshot]
url = "http://127.0.0.1:43128/mcp"
```

它与 WSS、快照查看器共用同一个常驻 EXE 和同一个 MCP instance；Codex 会话只作为
HTTP 客户端连接，不再为每个会话拉起一个快照 MCP 子进程。`GET /healthz` 的
`readerContextMcp` 只报告 path 与 instanceId，便于检查实例是否发生替换。

为回滚和隔离诊断，EXE 仍保留零额外运行时依赖的 stdio 入口：

```powershell
.\bw-computer-voice-audio.exe --reader-context-mcp --state `
  C:\Users\bwica\bw-computer-voice-bridge\runtime\reader-context-snapshot.json
```

stdio 回滚入口只注册 `reader_context_snapshot`，不接受 mutation，也没有可复用的 WSS
视觉 Broker。常驻 HTTP MCP 的快照工具返回“简短 assistant-context + 完整 JSON”两个纯文本
content；普通页文/选区读取不会截图。若快照明确给出
`drawingImageTool=reader_drawing_image`，只有当前页 ready、绘图 stable、非空且
file/page/revision/ref 全部一致时，独立无参只读工具才复用现有 Reader 合成 JPEG；收图后再验
同一身份，变化即只返回错误文本。绘图 `lastEditedAgeSec` 用同一 PWA 事件内部的相对时间建立
接收时年龄，再叠加 Windows 本地接收时钟，不直接比较两台设备的墙上时钟。

服务进程在同一 MCP 连接中保持 instance/call sequence，逐次读取原子快照；最新文件损坏时
保留上一次有效 revision。`active-reading` 超过三分钟则返回 `contextStatus=stale`，正文与
选区不会作为当前内容返回。选区状态严格区分 `active`、`cleared`、`unknown`，取消选择或换页
时不会沿用旧文本；新鲜度使用 Windows 收到心跳的时间，不信任 iPad 的墙上时钟。

`snapshot-mcp` 收到 PDF 的 `active-reading` 后，会按 Reader 的 1-based 页码从
Windows 本地书库只读提取正文并在同一次原子快照更新中标为 ready；默认书库根为
`C:\obsidian`，默认解释器为当前用户的
`AppData\Local\Programs\Python\Python313\python.exe`。需要改位置时只给服务进程设置
`BW_READER_LIBRARY_ROOT` / `BW_READER_PYTHON`；Reader 传入的 file 必须是书库根下相对
PDF 路径，绝对路径、`..`、reparse point/symlink 一律拒绝。提取固定使用
`python -I` + PyMuPDF，内存缓存键包含规范路径、文件长度、UTC mtime 与页码；失败时
当前页保持 pending/空正文并带明确 fallbackReason，不沿用旧页正文。

START 期间始终只有一个并发 `ReceiveAsync`。peer 在 START 回执前关闭会取消应用等待和
媒体链；若预取到一条非 close 消息，只做单条有界缓存，START 结算后按原顺序处理。
Voice generation 的 Windows 账本确认最长允许 5 秒；回执前下行 PCM 因此使用独立的
6 秒有界 bootstrap 缓冲，不能误用浏览器对外公布的 400 ms 实时播放窗口。回执发送后
浏览器仍只保留 400 ms 播放 horizon。若 pump、render 或 Voice monitor 在 START 返回前
终止，coordinator 必须先保留并返回首个 terminal code，再做完整 owned-resource teardown；
不得用 `MEDIA_START_UNCONFIRMED` 覆盖可诊断的真实错误。

## 浏览器麦克风上行

上行只接受 Active/current session 的 BWCV track 3。每条固定 1,956 bytes：

| 偏移 | 长度 | 合同 |
|---|---:|---|
| 0 | 4 | ASCII `BWCV` |
| 4 | 1 | 版本 `1` |
| 5 | 1 | track `3=browser-microphone` |
| 6 | 2 | flags，uint16 little-endian，必须为 `0` |
| 8 | 16 | START sessionId 的原始随机字节 |
| 24 | 4 | 从 `0` 严格递增的 uint32 little-endian sequence |
| 28 | 8 | 严格递增的 uint64 monotonic timestamp µs |
| 36 | 1920 | 960 个 48 kHz mono signed-16 little-endian 样本 |

`VirtualMicrophoneRenderSession` 在专用 MTA 线程以 shared/event-driven 模式打开 A
render endpoint；`Start` 前先向 WASAPI 初始缓冲提交确定性静音，首个事件丢失时同一
线程最多等待 100 ms 后主动推进一次。队列硬上限 200 ms，欠载写确定性静音。网络或
调度短突发导致队列满时丢弃最旧帧并保留最新语音，不累积过期延迟，也不因此结束整通
电话；帧格式、时间戳/sequence/session 错配或 native 端点失败仍 fail closed。START
在 A 成功打开前不得发送快捷键。

下行 process-loopback 只捕获目标进程及子进程，不读系统混音。`/5` 在快捷键前把精确
Codex root PID 的 eRender 三种 role 指向 B、eCapture 三种 role 指向 A 的录音侧；
六项先完整快照并落盘，再逐项 Set + Get 读回，任一步失败都逆序回滚。服务同时在 B 上
注册公开 `IAudioSessionManager2` notification，并在枚举前注册以封住创建竞态；只有当前
Codex 进程树出现当前 Active session 才报告 route verified。Inactive/Expired、外部 PID、
旧进程树或观察失败都保持 unverified；这个观察信号不代替策略接口的逐项读回。
旧 `/4` 不自动改应用路由，仍可作为手工音量混合器回滚入口。

## context → typist IPC

`context` 只允许 Active/current session，`contextContract` 必须为
`reader-outgoing-context/1`，event 核心字段、16 位小写 hex ID、序号、类型 allowlist 和
64 KiB 总预算均严格校验。允许类型：

```text
page.context
focus
drawing
command
command-failed
```

本地 IPC：

- pipe name：`bw-reader-voice-typist-v1`；
- 完整路径：`\\.\pipe\bw-reader-voice-typist-v1`；
- C# 为 `PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly` 的单实例 server；
- 双向 framing 为 4-byte little-endian uint32 长度 + 严格 UTF-8 JSON；
- payload 长度 1..65,536 bytes；串行单 in-flight；单次 exchange 3 秒；
- IPC contract：`reader-voice-typist-ipc/1`。

C# 只有收到 exact `requestId/sessionId/eventId/seq` 且 outcome 为
`accepted|duplicate` 的成功 ACK 才向浏览器确认。typist 先把事件 stage 为不可消费的
durable queue item，再原子推进 event ledger，最后把同一 item 发布为 committed，
之后才 ACK；任一步失败均不产生成功 ACK。queue/3 的 exact staging receipt 证明
duplicate 对应的 item 确实曾进入队列，不能用 ledger 为从未 stage 的 missing item
背书。committed item 在 UI submit 前先持久化
`delivery_started`；若进程在 SendInput 期间退出，重启后标记 delivery-uncertain 且不
盲目重发。停止后的 launcher `Status` 以只读 `queue-status` 检查 durable queue，
只公开其 session/event/seq，不公开正文或 receipts；停止 typist 并人工核对
conversation transcript 后，可用固定 launcher 的 `ResolveUncertain` 对 exact 三元组
确认“已送达”或“未送达”。session 切换与不可取消的 UI submit 串行，新 session 激活时
会丢弃旧 session backlog。正文标记
按合同反转义，独立命令成功保持完全静默。typist 的 schema/转义错误为
nonretryable context failure；pipe unavailable、timeout 或 framing 失败为 retryable。
两类错误都只影响 context，不停止 PCM 音频。

## runtime status 与诊断

`runtimeStatusPath` 每 5 秒及每次状态变化原子刷新，contract 为
`reader-computer-voice-direct-status/2`。字段固定为：

```text
contract
serviceInstanceId
pid
state
readerConnected
captureActive
lastError
updatedAtUtc
```

`lastError` 为 `null` 或严格
`failureId/code/stage/hresult/atUtc`；仅后续 START 真正成功才清除最近错误，回到 idle
不会擦掉诊断。状态与错误不包含 endpoint ID、登录账号、token、命令或原始上游文本。

无启动命令：

```powershell
dotnet build .\ComputerVoiceAudio.csproj -c Release
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- --describe
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- --self-test
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- `
  --list-direct-render-endpoints
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- `
  --list-direct-microphones
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- `
  --probe-codex-app-audio-route --config <absolute-direct-config>
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- `
  --probe-direct-output-route --config <absolute-direct-config>
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- `
  --diagnose-direct-audio-no-start --config <absolute-direct-config>
```

`--probe-codex-app-audio-route` 只接受 `/5`，定位当前唯一 Codex root PID，
读取 render/capture × Console/Multimedia/Communications 六项持久应用路由，并逐项返回
target、match 与 `Present|Unset|Error`。结果固定
`audioRouteMutated:false`；该命令不写路由、不恢复、不启动 capture/Codex，也不发快捷键，
可用于实测前后精确核对切换与恢复。

`--describe`、`--self-test`、endpoint 枚举和两个 route probe 不打开音频。
`--diagnose-direct-audio-no-start` 只初始化并立即释放 A render 与 process-loopback/B
probe，不调用 capture `Start`、不启动 Codex/typist，也不发送快捷键。无启动诊断通过仍
不能代替真实双向声音验收。

## 原生所有权规则

- capture 与 render session 各自固定使用一条专用 MTA 线程；
- mix format 只接受 PCM、32-bit IEEE float 或
  `WAVE_FORMAT_EXTENSIBLE` 中对应子格式；
- 每个成功 `GetBuffer` 在同一线程恰好配对一次 `ReleaseBuffer`；
- packet 在 release 前复制到自有内存；silent packet 生成确定性零数据；
- sink 同时限制 packet 数和字节数，满载明确 fault，不阻塞或静默丢包；
- 初始化、Start 或 pump 失败都执行同一 rollback/COM 释放序列；
- 异步 `ActivateAudioInterfaceAsync` 的参数、callback 和 operation 生命周期绑定，取消
  等待不能提前释放 native 仍可能读取的内存。

## 官方定义

- Microsoft Application Loopback sample：
  <https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback>
- `AUDIOCLIENT_ACTIVATION_PARAMS`：
  <https://learn.microsoft.com/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_activation_params>
- `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`：
  <https://learn.microsoft.com/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_process_loopback_params>
- `ActivateAudioInterfaceAsync`：
  <https://learn.microsoft.com/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync>

最低系统要求为 Windows build 20348。正式安装还需两个已签名虚拟音频端点、登录会话中的
supervisor、精确 A/B 选择、Codex 到 B 的应用路由，以及 Reader/PWA 与隔离扩展 profile 的
人工 E2E；自检结果不得冒充这些证据。
