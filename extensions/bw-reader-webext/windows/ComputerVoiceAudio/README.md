# BW 电脑客户端进程音频与直连服务

这是 `电脑客户端` 语音桥的 Windows 原生音频边界。当前阶段提供：

- `net8.0`、零第三方依赖的 Win32/COM 定义；
- 固定为目标进程及其子进程的 process-loopback；
- 只接受调用方明确给出的 Windows capture endpoint ID 的麦克风核心；
- `--describe` 与 `--self-test` 两个无音频命令；
- 保留供旧版回滚的 Native Messaging stdio 宿主；
- 可选的 `--direct-serve --config <path>` Reader→Windows 直连控制面；
- 两条 48 kHz mono signed-16、20 ms 的有界 PCM 流。

本目录不会捕获默认输出设备，也没有全系统输出回退。未知命令和 PID `0` 均 fail closed。
麦克风路径不会枚举设备、不会调用默认端点，也不会在明确设备
失效时回退；`MicCaptureRequest` 会拒绝空值、空白值、控制字符和超长 ID，并逐字传递合法
endpoint ID。程序启动本身不会打开音频。只有本机配置 `localOptIn=true`、浏览器请求通过
固定 WSS/Tailnet 身份门禁、Codex 进程树与显式麦克风仍一致，并收到电话按钮的一次
`start` 合同后才打开音频和发送 `Ctrl+Shift+C`。`--describe`、`--self-test`、服务启动、
健康检查、HELLO、状态刷新和选择模型都不能启动 capture。

## Reader→Windows 直连候选

直连入口：

```powershell
.\bw-computer-voice-audio.exe --direct-serve --config `
  C:\Users\bwica\bw-computer-voice-bridge\native-host\computer-voice-direct.config.json
```

服务只接受严格字段的 `reader-computer-voice-direct-config/1` 配置，并且
`listenHost` 必须逐字等于 `127.0.0.1`；默认端口为 `43128`。Kestrel 也直接绑定
`IPAddress.Loopback`，不能由配置改为局域网或公网地址。预期由 Tailscale Serve 在 tailnet
内终止 TLS/WSS 并反代到该 localhost 端口；C# 服务不修改 Tailscale、防火墙或系统代理。

浏览器 WebSocket 只开放 `/reader-computer-voice/v1`。当前
`experimentalSingleUserMode=true` 时，为让扩展在任意网页工作，规范的 HTTP(S)
`Origin` 可通过 Origin 门禁；关闭该模式才要求与 `allowedOrigins` 中一项逐字相等。
两种模式都要求 Tailscale Serve 注入的 `Tailscale-User-Login` 是单值，并与
`allowedTailscaleUserLogin` 按 OrdinalIgnoreCase 精确相等。缺失、多值或不匹配都在
Upgrade 前返回 403；header 值不写日志。一次只允许一条 Reader WebSocket。控制消息必须为
UTF-8 JSON 文本，单条
最多 65536 字节；客户端二进制消息一律拒绝。`/healthz` 仅返回无秘密的进程健康信息：没有
Origin 的本机探针可读；一旦带 Origin，也必须通过相同 Origin/Tailnet 身份门禁。

当前桌面 GUI 写出的 strict config 字段必须恰好为：

- `contract`、`localOptIn`、`microphoneEndpointId`；
- `listenHost`、`listenPort`、`allowedOrigins`；
- `allowedTailscaleUserLogin`（当前固定为 `bwicarus@gmail.com`）；
- `experimentalSingleUserMode`（当前单用户实验模式为 `true`）；
- `pairingCodeHash`、`pairingExpiresAtUtc`；
- `pairedClientPublicKeySpki`、`pairedClientFingerprintSha256`；
- `outputScope`（只能为 `process-only`）、`appKind`（只能为
  `codex-desktop`）、`runtimeStatusPath`。

迁移兼容上，旧配置可以暂时缺少 `experimentalSingleUserMode`，加载时等同于
`true`；重新保存后会显式写出该字段。同目录
`computer-voice-direct.config.example.json` 是这种旧形状，桌面目录中的示例是当前形状。

当前 Reader/PWA 和扩展使用 direct v2。每条新 WSS 先发送严格字段的
`hello`，其中 `protocolVersion` 必须为 `2`；成功结果只返回版本与固定资源限制，随后直接进入
等待 START 阶段。浏览器不需要配对码、endpoint 输入、设备身份、ECDSA 密钥或 bearer token，
也不会把这些内容写入 IndexedDB、extension storage 或 localStorage。

允许任意 HTTP(S) Origin 是单用户实验模式的显式风险取舍：任意知道固定 WSS 的已访问页面都可
尝试 START。风险由固定 tailnet WSS、Tailscale 唯一用户 header、本机 opt-in、显式麦克风、
Codex 进程树、单连接、START-only 激活和 heartbeat/close fail-closed 共同约束，而不是被描述成
公开通用服务。

四个 pairing 字段与 v1 `pair/auth` 协议暂时只为旧客户端回滚兼容保留；当前 GUI 保持其为空，
当前 Reader 和扩展不会进入 v1。关闭 `experimentalSingleUserMode` 后 v2 HELLO 会拒绝，并要求
显式旧认证客户端。

所有控制请求都使用：

```json
{"contract":"reader-computer-voice-direct/1","type":"hello|status|start|heartbeat|stop","requestId":"..."}
```

成功结果固定为 `type:"result"`、原 `requestId`、`ok:true`、`action`、`payload`；失败结果
固定为 `ok:false` 和 `error:{code,message,retryable}`。`status`、设置刷新或选择模型绝不
启动应用、音频或快捷键。只有通过 direct v2 门禁后的 `start` 才进入
`localOptIn → ensure app running → wait unique ready → capture`。应用目标不接受 Reader
下发路径、命令或 AUMID；代码内当前只允许
`OpenAI.Codex_2p2nqsd0c76g0!App`。START 的 `sessionId` 必须为
`session-` 加 16 个随机字节的 22 位无 padding base64url。重复同 session START 幂等，不会
再次启动应用。

服务端处理 START 时始终保留且只保留一个并发 `ReceiveAsync`。Reader 在 START 回执前挂断
会立即取消传给应用等待和媒体启动链的 token，等待该链退出并回滚捕获后才释放连接；不会把
STOP 排在尚未完成的 START 后面。若先收到一条非 close 控制消息，只允许有界预取这一条并在
START 完成后按原顺序处理，不会再开第二个并发 receive。生产媒体 adapter 在启动 typist 进程
和发送语音快捷键前各有取消围栏，捕获初始化中的取消统一走现有 rollback。只有 helper
明确报告为本次 START 新建、且 PID 仍精确匹配的 typist 才形成内存 lease；正常 STOP、
连接关闭、心跳超时、媒体错误、START 中途失败或服务 Dispose 都会释放该 lease 并调用固定
launcher 的 `Stop`。原本已运行或竞态中由别处启动的 typist 不归本次通话所有，绝不停止。

START 成功后，Reader 必须每 5000 ms 在同一条已接受 WSS 上发送
`heartbeat`，字段严格为 `sessionId` 和从 1 开始逐次加 1 的 uint32 `sequence`。
服务端用单调时钟维护 15000 ms 截止时间；重复 START、`status` 和设置刷新都不能续期。
心跳超时会先停止两路捕获，再发送 `event:"status"`（`state:"error"`、
`reason:"BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_TIMEOUT"`）并关闭连接，避免 iPad
休眠或半开连接后 Windows 仍继续采音。

START 期间可推送 `starting-app`、`waiting-app-ready`、`starting-capture` 状态事件。
本地未 opt-in、启动超时、非唯一目标、媒体未确认或繁忙都返回稳定失败码。生产
`WindowsDirectAppLauncher` 只通过代码内白名单 AUMID 启动 packaged Codex，已运行时不会
重复启动，并等待唯一进程树和唯一窗口；`WindowsDirectMediaAdapter` 复用既有
process-loopback、显式麦克风、48 kHz framer、typist 和快捷键门禁。两者只有通过门禁的 START
路径可达。自检始终注入 fake adapter，不会实际启动程序、typist、快捷键或采音。

媒体接线后，服务端只在同一条已接受 WSS 上发送固定二进制 PCM message。每条必须恰好
1956 字节：

| 偏移 | 长度 | 合同 |
|---|---:|---|
| 0 | 4 | ASCII `BWCV` |
| 4 | 1 | 版本 `1` |
| 5 | 1 | track：`1=app-output`，`2=user-mic` |
| 6 | 2 | flags，uint16 little-endian，当前必须为 `0` |
| 8 | 16 | START sessionId 中的原始随机字节 |
| 24 | 4 | 每 track 从 0 严格递增的 uint32 little-endian sequence |
| 28 | 8 | 严格递增的 uint64 little-endian monotonic timestamp µs |
| 36 | 1920 | 960 个 48 kHz mono signed-16 little-endian 样本（20 ms） |

`runtimeStatusPath` 由服务每 5 秒及每次状态变化原子刷新，合同为
`reader-computer-voice-direct-runtime-status/1`，字段固定为
`contract/serviceInstanceId/pid/state/readerConnected/captureActive/updatedAtUtc`。
状态枚举为 `starting|idle|reader-connected|starting-app|waiting-app-ready|`
`starting-capture|active|faulted|stopping|stopped`。`idle` 仅表示轻量服务在线，不代表
采音、启动 Codex 或发送快捷键。

媒体 pump、PCM 队列、序列或 WebSocket 发送失败时，adapter 的有界 `Completion` 会被服务
监控：runtime status 先写 `faulted`，再向仍可写的 WSS 发送 Reader 已支持的
`event:"status"`（`state:"error"`、`reason:<稳定失败码>`），随后用 Internal Server Error
关闭连接并清理 session。不能再以“按钮自动停止但没有原因”的形式静默失败。START 期间产生的
PCM 最多按每轨 400 ms 有界缓存，START 成功 result 发出后才释放，
所以首个二进制帧不会抢在 START 回执前面。

## 无音频验证

```powershell
dotnet build .\ComputerVoiceAudio.csproj -c Release
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- --describe
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- --self-test
dotnet run --project .\ComputerVoiceAudio.csproj -c Release --no-build -- `
  --diagnose-direct-audio-no-start --config <absolute-direct-config>
```

`--describe` 与 `--self-test` 不会调用 `ActivateAudioInterfaceAsync`。
`--diagnose-direct-audio-no-start` 只初始化并立即释放两条音频接口，明确不调用 capture
`Start`。三个命令都不会读取应用/麦克风 PCM 或发送快捷键。

当前 direct v2 首次启用走桌面
`BW-Computer-Voice-Bridge.exe`：在 Windows 本地明确选择 Active 麦克风并启用服务即可，
不再填写扩展 ID、浏览器 endpoint 或配对码。安装登录 supervisor、应用/回滚 Tailscale Serve
仍是单独的显式本地 mutation；刷新状态不会启动语音、采集或发送快捷键。

两个内部 capture session 各自固定使用一条专用 MTA 线程。进程输出使用 process-loopback，
麦克风只允许 `IMMDeviceEnumerator.GetDevice(explicitEndpointId)`；COM vtable 中不可省略的
枚举/默认端点 slot 被标为编译期错误，生产 resolver 只暴露 `OpenExact`。两条路径共同复用
同一个 shared/event-driven native runtime，同步执行
`GetMixFormat → Initialize(shared, LOOPBACK|EVENTCALLBACK) → GetBufferSize →
SetEventHandle → GetService → Start`。格式只接受 PCM、32-bit IEEE float，或
`WAVE_FORMAT_EXTENSIBLE` 中对应的 PCM/IEEE-float 子格式；压缩和未知格式 fail closed。
`GetMixFormat` 的内存总在 `finally` 中用 `FreeCoTaskMem` 释放。

上面的 flags 对进程输出是 `LOOPBACK|EVENTCALLBACK`，对显式麦克风是
`EVENTCALLBACK`；麦克风绝不会使用 loopback。初始化、启动或 sink backpressure 失败都会在
专用线程上执行相同的 rollback/COM 释放序列。

每个真正取得的 `IAudioCaptureClient.GetBuffer` 都在同一线程恰好配对一次
`ReleaseBuffer`；`AUDCLNT_S_BUFFER_EMPTY` 因没有取得 packet 而不调用 Release。音频先复制到
自有数组再释放原生 buffer，`SILENT` 生成确定性零数据，时间戳错误会让 position/QPC 失效。
sink 同时限制 packet 数和字节数；满载时不会阻塞采集线程或无声丢包，而是让 session 进入
`Faulted` 并带明确 backpressure 错误完成。

异步 activation 的原生参数、回调和 operation 生命周期绑定在一起：调用者超时/取消只停止
等待，不能提前释放原生仍可能读取的内存；callback 已取得结果且不再读取 activation 参数后才
清理参数内存。托管 continuation 不能严格证明自己运行在 `ActivateCompleted` 返回之后，因此
operation RCW 不在该 continuation 中强制 `FinalReleaseComObject`，而交给活动回调和 CLR 的
正常 COM 生命周期管理。`IAudioCaptureClient.GetBuffer/ReleaseBuffer` 已固定由同一条专用
采集线程配对调用；停止会先唤醒该线程，再按
`Stop → Reset → capture client → audio client → event` 的所有权顺序清理。

## 对照的官方定义

- Microsoft Application Loopback sample：
  <https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback>
- `AUDIOCLIENT_ACTIVATION_PARAMS`：
  <https://learn.microsoft.com/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_activation_params>
- `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`：
  <https://learn.microsoft.com/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_process_loopback_params>
- `ActivateAudioInterfaceAsync`：
  <https://learn.microsoft.com/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync>

最低系统要求为 Windows 10 build 20348。当前真实 capture 只能由 fixed-WSS direct v2
连接中的显式 START 调用，并仍受 Reader 电话按钮、Tailnet 唯一用户、本机 opt-in、进程树和
显式麦克风门禁。Native Messaging 只保留作旧版回滚兼容，不是扩展现行媒体路径。
