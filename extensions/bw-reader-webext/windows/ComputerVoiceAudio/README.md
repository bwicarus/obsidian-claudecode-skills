# BW 电脑客户端进程音频 helper

这是 `电脑客户端` 语音桥的 Windows 原生音频边界。当前阶段提供：

- `net8.0`、零第三方依赖的 Win32/COM 定义；
- 固定为目标进程及其子进程的 process-loopback；
- 只接受调用方明确给出的 Windows capture endpoint ID 的麦克风核心；
- `--describe` 与 `--self-test` 两个无音频命令；
- 只允许由 Chrome 注册表白名单启动的 Native Messaging stdio 宿主；
- 两条 48 kHz mono signed-16、20 ms 的有界 PCM 流。

本目录不会捕获默认输出设备，也没有全系统输出回退。未知命令、PID `0` 和尚未接线的
capture 启动均 fail closed。麦克风路径不会枚举设备、不会调用默认端点，也不会在明确设备
失效时回退；`MicCaptureRequest` 会拒绝空值、空白值、控制字符和超长 ID，并逐字传递合法
endpoint ID。程序启动本身不会打开音频。只有本机配置 `localOptIn=true`、扩展来源精确
匹配、Codex 进程树与显式麦克风仍一致，并收到扩展的一次性 `start` 合同后才打开音频和发送
`Ctrl+Shift+C`。普通 CLI 仍不能启动 capture。

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

最低系统要求为 Windows 10 build 20348。CLI 继续只允许无音频的 `--describe` /
`--self-test`；真实 capture 只能由已注册、白名单来源精确匹配的 Chrome Native Messaging
调用，并仍受 Reader 一次性电话按钮、本机 opt-in、进程树和显式麦克风四重门禁。
