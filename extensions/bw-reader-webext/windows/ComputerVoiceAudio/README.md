# BW 电脑客户端进程音频与直连服务

这是 `电脑客户端` 语音桥的 Windows 原生音频边界。当前阶段提供：

- `net8.0`、零第三方依赖的 Win32/COM 定义；
- 固定为目标进程及其子进程的 process-loopback；
- 只接受调用方明确给出的 Windows capture endpoint ID 的麦克风核心；
- `--describe` 与 `--self-test` 两个无音频命令；
- 只允许由 Chrome 注册表白名单启动的 Native Messaging stdio 宿主；
- 可选的 `--direct-serve --config <path>` Reader→Windows 直连控制面；
- 两条 48 kHz mono signed-16、20 ms 的有界 PCM 流。

本目录不会捕获默认输出设备，也没有全系统输出回退。未知命令和 PID `0` 均 fail closed。
麦克风路径不会枚举设备、不会调用默认端点，也不会在明确设备
失效时回退；`MicCaptureRequest` 会拒绝空值、空白值、控制字符和超长 ID，并逐字传递合法
endpoint ID。程序启动本身不会打开音频。只有本机配置 `localOptIn=true`、扩展来源精确
匹配、Codex 进程树与显式麦克风仍一致，并收到扩展的一次性 `start` 合同后才打开音频和发送
`Ctrl+Shift+C`。`--describe`、`--self-test`、服务启动、健康检查、配对、认证、状态刷新和
选择模型都不能启动 capture。

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

浏览器 WebSocket 只开放 `/reader-computer-voice/v1`，握手必须携带与
`allowedOrigins` 中某一项逐字相等的
HTTPS `Origin`，并且由 Tailscale Serve 注入的 `Tailscale-User-Login` 必须是单值、
与 `allowedTailscaleUserLogin` 按 OrdinalIgnoreCase 精确相等。缺失、多值或不匹配都在
Upgrade 前返回 403；header 值不写日志。一次只允许一条 Reader WebSocket。控制消息必须为
UTF-8 JSON 文本，单条
最多 65536 字节；客户端二进制消息一律拒绝。`/healthz` 仅返回无秘密的进程健康信息：没有
Origin 的本机探针可读；一旦带 Origin，也必须命中相同白名单。

配置示例见 `computer-voice-direct.config.example.json`。其字段必须恰好为：

- `contract`、`localOptIn`、`microphoneEndpointId`；
- `listenHost`、`listenPort`、`allowedOrigins`；
- `allowedTailscaleUserLogin`（当前固定为 `bwicarus@gmail.com`）；
- `pairingCodeHash`、`pairingExpiresAtUtc`；
- `pairedClientPublicKeySpki`、`pairedClientFingerprintSha256`；
- `outputScope`（只能为 `process-only`）、`appKind`（只能为
  `codex-desktop`）、`runtimeStatusPath`。

Windows GUI 生成的配对码恰好 10 位，只使用
`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`。明文只在 GUI 内存中显示；配置只保存
`base64url(SHA-256(UTF-8 code))`，固定 43 字符，以及 UTC 过期时间。新配对不会覆盖已有
客户端；重新配对必须由本地 GUI 明确清除旧公钥。配对成功后服务原子清空 code hash/过期时间，
保存 PWA 自生成的 ECDSA P-256 SPKI 公钥和
`base64url(SHA-256(SPKI DER))` 指纹。没有可导出的长期 bearer token。

每条新 WSS 先发 `hello`。其成功结果带 30 秒 challenge；客户端签名的字节必须逐字为：

```text
reader-computer-voice-auth/1\n<challengeId>\n<nonce>\n<Origin>
```

编码为 UTF-8，算法 ECDSA P-256/SHA-256，签名格式为 IEEE-P1363 固定 64 字节
`r || s`，线上字段采用无 padding base64url。challenge 绑定当前精确 Origin，认证尝试后立即
消费。配对只登记公钥，不自动证明私钥持有；客户端仍须发送 `auth`。

WebSocket Accept 后必须在固定 10000 ms 内完成 `auth`；只发 `hello`、只完成 `pair`、
状态查询或无效消息都不能延长该期限。认证完成后必须在固定 30000 ms 内发送有效 START；
`status`、重复消息和失败请求同样不能续期。阶段超时会发送稳定 `status:error`（若连接仍可写）
并在最多 2 秒的通知窗口后强制关闭，从而释放唯一连接槽；进入 active 后改由下述媒体心跳管理。

所有控制请求都使用：

```json
{"contract":"reader-computer-voice-direct/1","type":"hello|pair|auth|status|start|heartbeat|stop","requestId":"..."}
```

成功结果固定为 `type:"result"`、原 `requestId`、`ok:true`、`action`、`payload`；失败结果
固定为 `ok:false` 和 `error:{code,message,retryable}`。`status`、设置刷新或选择模型绝不
启动应用、音频或快捷键。只有通过公钥认证后的 `start` 才进入
`localOptIn → ensure app running → wait unique ready → capture`。应用目标不接受 Reader
下发路径、命令或 AUMID；代码内当前只允许
`OpenAI.Codex_2p2nqsd0c76g0!App`。START 的 `sessionId` 必须为
`session-` 加 16 个随机字节的 22 位无 padding base64url。重复同 session START 幂等，不会
再次启动应用。

服务端处理 START 时始终保留且只保留一个并发 `ReceiveAsync`。Reader 在 START 回执前挂断
会立即取消传给应用等待和媒体启动链的 token，等待该链退出并回滚捕获后才释放连接；不会把
STOP 排在尚未完成的 START 后面。若先收到一条非 close 控制消息，只允许有界预取这一条并在
START 完成后按原顺序处理，不会再开第二个并发 receive。生产媒体 adapter 在启动 typist 进程
和发送语音快捷键前各有取消围栏，捕获初始化中的取消统一走现有 rollback。

START 成功后，Reader 必须每 5000 ms 在同一条已认证 WSS 上发送
`heartbeat`，字段严格为 `sessionId` 和从 1 开始逐次加 1 的 uint32 `sequence`。
服务端用单调时钟维护 15000 ms 截止时间；重复 START、`status` 和设置刷新都不能续期。
心跳超时会先停止两路捕获，再发送 `event:"status"`（`state:"error"`、
`reason:"BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_TIMEOUT"`）并关闭连接，避免 iPad
休眠或半开连接后 Windows 仍继续采音。

START 期间可推送 `starting-app`、`waiting-app-ready`、`starting-capture` 状态事件。
本地未 opt-in、启动超时、非唯一目标、媒体未确认或繁忙都返回稳定失败码。生产
`WindowsDirectAppLauncher` 只通过代码内白名单 AUMID 启动 packaged Codex，已运行时不会
重复启动，并等待唯一进程树和唯一窗口；`WindowsDirectMediaAdapter` 复用既有
process-loopback、显式麦克风、48 kHz framer、typist 和快捷键门禁。两者只有认证 START
路径可达。自检始终注入 fake adapter，不会实际启动程序、typist、快捷键或采音。

媒体接线后，服务端只在同一条已认证 WSS 上发送固定二进制 PCM message。每条必须恰好
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
```

以上两个运行命令不会调用 `ActivateAudioInterfaceAsync`，不会打开麦克风、读取应用声音或发送
快捷键。

首次启用使用同目录
`install-computer-voice-native-host.ps1 -Action Enable -ExtensionId <扩展弹窗显示的 ID>`。
脚本必须在交互桌面运行，会先列出 Active 麦克风，再要求用户选择并准确输入 `ENABLE`；在确认前
零写入。`-Action Disable` 会撤销 Chrome Native Messaging 注册并把本机 opt-in 设回 false。
安装脚本本身不启动语音、采集或发送快捷键。

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

最低系统要求为 Windows 10 build 20348。真实 capture 只能由已注册、白名单来源精确匹配的
Chrome Native Messaging，或 localhost 直连服务中通过 ECDSA 公钥认证的显式 START 调用；
两条路径都仍受 Reader 电话按钮、本机 opt-in、进程树和显式麦克风门禁。
