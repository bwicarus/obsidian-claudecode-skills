# 电脑客户端语音桥接

## direct v3 固定拓扑（已实施并安装；Windows Direct 自 0.1.11 起持续原子安装，当前候选 0.1.159）

（历史背景，v2 已下线）direct v2 曾把 Windows 会话中的一个精确麦克风 endpoint 作为输入。RDP 的“远程音频” endpoint 会随远程会话建立、断开和重定向策略消失；配置仍引用旧 ID 时，START 会在 `microphone.get-explicit-device / HRESULT 0x80070490` 失败。这不是进程输出捕获失败，也不是 Pi 中继问题 —— v3 因此改为固定的双向直连。RDP 的“远程音频”
endpoint 会随远程会话建立、断开和重定向策略消失；配置仍引用旧 ID 时，START 会在
`microphone.get-explicit-device / HRESULT 0x80070490` 失败。这不是进程输出捕获失败，也
不是 Pi 中继问题。

direct v3 改为固定的双向直连：

```text
Reader/PWA 或扩展页面的当前麦克风
    → 固定 Tailnet WSS（48 kHz / mono / s16le / 20 ms）
    → Windows 桥接器精确写入虚拟线缆 A 的 render endpoint
    → 虚拟线缆 A 的 recording endpoint
    → Codex/ChatGPT Voice 选择该固定虚拟麦克风

Codex/ChatGPT 目标进程树输出
    → Codex/ChatGPT Voice 选择虚拟线缆 B 的固定 render endpoint
    → Windows process-loopback
    → 同一 WSS
    → Reader/PWA 或扩展页面播放
```

输入和输出必须使用两根彼此独立的虚拟线缆。复用同一根线缆会把网页麦克风与
Codex/ChatGPT 自己的输出混进同一个 recording endpoint，产生自听或回声。线缆 B 的
recording 端不参与桥接；它只保证 RDP 断开后目标应用仍有稳定 render stream。Windows
process-loopback 本身仍按目标进程树捕获，不退回全系统混音。

实现顺序与门禁：

1. direct 协议升级为 v3；新增只允许当前已认证且已 START session 使用的浏览器麦克风二进制
   上行。固定帧长、方向、session、sequence 和 timestamp 均严格校验，实时队列最多 200 ms，
   欠载写静音，过载或协议错配 fail closed。
2. Windows 保留目标进程输出捕获，移除本次会话对物理/RDP 麦克风 capture 的依赖；START
   必须在发语音快捷键前确认显式虚拟 render endpoint 已打开，STOP、断线或故障统一释放
   render、process capture、pump 和本次 owned typist。
3. Reader 仅在真实电话按钮手势中请求当前页面麦克风权限；选择模型、刷新状态、脚本点击和
   后台重连均不得采音。麦克风 track mute/ended、页面隐藏导致的系统中断和权限拒绝必须显示
   真实失败并清理，不可偷偷重获权限。
4. 普通网页仍只能通过扩展 isolated runtime → background 中继；中继只新增固定 1,956-byte
   上行二进制帧，不开放任意 URL、设备 ID、命令或凭据。
5. 设备层采用两根成熟的已签名虚拟音频线缆；项目不自研、不测试签名 Windows 驱动，也不把
   第三方驱动打进安装包。驱动下载、管理员安装、重启、精确 endpoint 选择，以及 Codex 的
   虚拟麦克风/虚拟扬声器选择属于单独的用户安装验收门；在该门之前只完成代码、无副作用测试
   与候选构建。

浏览器仍受操作系统/浏览器麦克风权限约束；“免配对、免地址配置”不等于绕过系统隐私授权。
Pi 为扩展与网页表面提供书籍与 outgoing journal 数据；**App 内 `/pdf/api/outgoing/journal` 是本地实现（`native-local-runtime.js`），不打 Pi —— 改服务端只影响扩展/网页那一侧，到 App 需要新的 TestFlight 构建**。Pi 都不代理 Windows WSS、控制消息或 PCM 媒体。；它不代理 Windows WSS、控制消息或
PCM 媒体。

## v3 固定拓扑

电脑语音是单用户实验功能。书籍 PWA 与普通网页使用不同的受信入口，但最终都只连接代码内
固定的 Windows 地址：

```text
App 内阅读器（本地 ReaderBundle 环回 Origin；专用 context WSS 见下文「实验上下文末端」）
〔原「书籍 PWA（精确生产 Origin）」入口已退役：`/pdf/*` 阅读器页面返回 410 Gone〕
    ├─ 同源 GET /pdf/api/outgoing/journal
    └─ wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1

普通 HTTP(S) 页面
    ⇅ 扩展 isolated content runtime
    ⇅ 固定 chrome.runtime Port
    ⇅ 扩展 background
    ⇅ 同一个固定 Windows WSS

Windows Tailscale Serve
    ⇅ 127.0.0.1:43128
Windows C# direct server
    ├─ 浏览器麦克风 PCM → 虚拟线缆 A
    ├─ Codex 进程树输出 → Reader 扬声器
    └─ Reader context → CurrentUserOnly named pipe → voice-typist
```

旧 Pi `/api/reader/computer-voice/*` 路由、popup 配对入口和 offscreen/Native Messaging
媒体链不在 v3 路径。`offscreen.js` 只是惰性 tombstone；扩展 background 只允许固定
Windows WSS、固定文本合同和固定长度 PCM，不读取配对记录，也不能由网页指定下游地址。

## 两个虚拟音频端点

- `virtualMicrophoneRenderEndpointId` 是 A 的 Active `eRender` 端。桥接器把 Reader 网页
  麦克风写入 A；Codex/ChatGPT Voice 一次性选择 A 的 recording 端作为麦克风。
- `virtualSpeakerRenderEndpointId` 是 B 的 Active `eRender` 端。Codex/ChatGPT 在 Windows
  音量混合器中一次性把应用输出选择为 B；桥接器仍按目标进程树做 process-loopback，不读取
  B 的 recording 端。
- A、B 必须非空、Active、精确匹配且互不相同。不存在 first/default fallback，不修改系统
  默认输入或输出，也不退回全系统混音。
- B 处于 Active 只能证明端点可打开，不能证明 Codex 已路由到 B。桥接器另在该精确端点上
  用公开 Core Audio session API 观察当前 Codex 进程树的 Active session；
  未出现该正向证据时 STATUS 返回
  `BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_UNVERIFIED`。桌面控制面仍只用官方
  `ms-settings:apps-volume` 打开音量混合器，不修改系统或应用默认路由；最终有声链路仍由
  人工 E2E 验收。

项目不自研或测试签名音频驱动，也不把第三方驱动打入候选包。驱动下载、管理员安装、可能的
重启和 endpoint 选择是独立安装门。

## 用户与生命周期流程

1. 选择“电脑客户端”、打开设置或刷新状态只读配置，不启动应用、不申请麦克风、不发送
   快捷键。
2. 只有注册电话按钮的一次真实用户点击才能同步创建/恢复 AudioContext 并申请当前网页
   麦克风。脚本点击、直接调用、过期 lease、迟到配置响应和第二次取消点击都不能启动。
3. 浏览器先发送 v3 `HELLO`，再发送随机 session 的 `START`；收到 START 成功前不发送
   上行 PCM，也不启动 outgoing journal 泵。
4. 已安装且登录会话中的 supervisor 负责保持 Windows listener 在线。START 可按既有白名单
   自动拉起 Codex、typist、媒体管线和一次语音快捷键；电脑关机、睡眠、listener 未安装或
   已停止时，网页不能凭空唤醒 Windows，只能显示离线。
5. START 在任何快捷键副作用前重新检查 `localOptIn`、A/B 两个精确 render endpoint、
   `process-only` 输出范围、唯一 session ownership，以及当前用户本地唯一的
   `realtimeVoice=Ctrl+Shift+C`。该命令由 Codex 注册为 OS-global hotkey，桥不抢占或
   校验 Windows 前台窗口；只要唯一 packaged-app root 未变化，Electron 子进程增减不应
   误拒绝 START。
6. 活跃通话持续发送递增 heartbeat。STOP、断线、心跳超时、启动失败或媒体故障释放 A
   render、process-loopback、PCM pump、named pipe 和当前 exact PID + process-start
   FILETIME owned typist。
   原本由别处运行的 typist 不归桥接器停止。
   bridge-owned typist 还持续核对 bridge owner 的 PID + process-start FILETIME；
   C# 整体崩溃或 PID 复用时自行退出。managed 模式禁用 idle 误杀，手工启动仍保留
   600 秒无活动兜底。
7. AudioContext 暂停时只保留最新 20 ms 下行帧；合法突发超过 400 ms 排程时丢弃旧排程并
   从当前时刻恢复，不因播放阻塞自动关闭 WSS。再次真实点击只恢复当前播放，不重复 START。
8. Codex Voice 的 capability-ledger 启动确认最长 5 秒，故 START 成功回执前使用独立、
   有界的 6 秒 bootstrap PCM 缓冲；这不改变浏览器 400 ms 实时播放上限。START 窗口内
   若媒体异步终止，Windows 先传播首个 pump/render/Voice terminal code，再完整恢复
   六路应用音频、Voice generation、媒体与 typist 所有权，不能只显示泛化“未确认”。

浏览器麦克风 track mute/ended、权限拒绝和页面退出会清理采音；不会静默重新申请权限。
桥不读取蓝色 Voice 球或其他 UI 状态；它用 Codex 包在 Windows 麦克风 capability ledger
中的 start/stop 时间戳作为代理信号。START 只在出现新 generation 后认领，STOP 只对仍属
本桥且根进程代次未变的同一 generation 发送一次全局快捷键，并等待 stop 时间戳确认；
预先存在、已本地关闭或已被新 generation 替换的 Voice 不会被桥切换。

## 协议与配置合同

- WebSocket root contract：`reader-computer-voice-direct/1`；`HELLO.protocolVersion=3`。
- PCM：48 kHz、mono、s16le、20 ms。浏览器麦克风使用 BWCV track 3，完整帧固定 1,956
  bytes；只在 Active/current session 接受，实时队列上限 200 ms，欠载写静音，溢出或
  sequence/session/timestamp 错配 fail closed。
- Reader context：浏览器从 `reader-outgoing-context/1` journal 读取，只在 START 后串行发送
  `context`。每条 Windows exact ACK 后才推进 event `seq`；截断批次不能使用 outer cursor
  跳过事件。context 的网络、schema、gap 或 typist IPC 故障只停止 context 泵，音频继续。
- 本地 IPC：`\\.\pipe\bw-reader-voice-typist-v1`，C# 为 `CurrentUserOnly` server，typist
  为 client；4-byte little-endian `uint32` 长度 + 严格 UTF-8 JSON，1..65,536 bytes，
  单请求/单响应、单 in-flight。IPC contract 为 `reader-voice-typist-ipc/1`。
- typist 先把事件 stage 为不可消费的 durable queue item，再提交 durable ledger，
  最后发布为 committed；三步完成后才返回 `accepted|duplicate`，不等待 UI 打字完成。
  queue/3 同时持久化 exact staging receipt；`duplicate` 只有在 item 仍在，或该
  session/event/seq 的 receipt 能证明它确实曾进入队列时才可成功，不能只凭 ledger
  把“从未 stage 的 missing item”误报为已接管。
  committed item 在 UI submit 前先持久化 `delivery_started`；中途进程退出后标记
  delivery-uncertain，禁止盲目重发。typist 停止后，launcher 的 `Status` 通过只读
  `queue-status` 直接检查 durable queue，而不是相信可能过期的 `status.json`；只公开
  exact session/event/seq，不公开正文或 receipts。停止 typist 并
  人工核对 transcript 后，固定 launcher 的 `ResolveUncertain` 才能确认已送达并丢弃，
  或确认未送达并解除重试围栏。session 切换与 UI submit 串行，新 session 激活时清掉
  旧 session backlog。正常断管的 Win32
  `BROKEN_PIPE/NO_DATA/PIPE_NOT_CONNECTED` 均按 EOF 收敛。正文反转义单次从左到右：
  `\\→\`、`\⟦→⟦`、`\⟧→⟧`；未知 `\x`
  原样保留；末尾孤立 `\` fail closed。不能用链式 replace 的理由是它无法拒绝这个坏输入，
  不是合法 round-trip 会二次反转义。
- runtime status：`reader-computer-voice-direct-status/2`，`lastError` 为 `null` 或严格的
  `failureId/code/stage/hresult/atUtc`；只有后续 START 真正成功才清除最近错误。
- strict config：`reader-computer-voice-direct-config/5`，在 `/4` 字段上增加独立的
  `virtualMicrophoneCaptureEndpointId`。A 的 eRender、A 的 eCapture 与 B 的 eRender
  必须分别明确选择且 flow 匹配；不得从 render ID 推导 capture ID。`/5` 才启用 Codex
  六角色按应用音频路由；兼容 `/4` 不含 capture 字段并明确关闭自动路由。当前
  `experimentalSingleUserMode` 必须为 `true`；不存在配对码、公钥或旧 pairing 字段。
  旧 `/1` + `microphoneEndpointId` 配置只能进入 `legacy-migration-required`，经本地显式
  迁移后清除，绝不作为运行时 fallback；`/3` 及更旧合同不进入运行时。
- `/5` 的六角色真值按稳定 Codex AUMID 定位当前用户
  `Multimedia\Audio\DefaultEndpoint` 应用键；每项 endpoint 主值与 `_p` property-set ID
  必须成对匹配，目标 `_p` 只从对应 MMDevice 的只读属性解析。内部 AudioPolicyConfig
  在无活动音频 session 时会按 PID 返回 `0x80070057`，不能拿它冒充“未配置”或恢复快照。
  Windows 重置音量合成器后若整个 AUMID 应用键缺失，六路快照为 `unset`；首个已记入事务
  日志的写操作按本机验证过的 Windows 身份键规则创建并读回 AUMID，停止时只恢复/删除六组
  pair，保留空身份键。该键名规则是兼容实现而非公开 Windows 合同；身份重复、pair 不一致
  或写后读回失败仍在快捷键前停止并按事务日志回滚。

## 实验上下文末端（2026-07-30）

音频 direct v3 保持不变；上下文末端由 strict config 明确二选一：

- `legacy-inject`：沿用 journal → Windows named pipe → voice-typist；
- `snapshot-mcp`：扩展表面仍从 Pi 的 `/pdf/api/active-reading` 与 `/pdf/api/outgoing/journal` 取事件；**App 内这两条都由 `native-local-runtime.js` 本地应答（前者属于 owner=pi 但已本地化的名实不符路由），改 Pi 无效**。事件经现有固定 WSS 直接送到 Windows，原子更新 `runtime/reader-context-snapshot.json`。，但经现有固定 WSS 直接送到
  Windows，原子更新 `runtime/reader-context-snapshot.json`。active GET 负责把 vbook
  全局页解析回真实卷/卷内页，journal 再提供稳定正文；Pi 的旧 context.md 推送停止，
  START 不启动 typist，正文不进入客户端输入框。

`snapshot-mcp` 在没有通话时用 `/reader-context/v1` 保持一条纯上下文连接，不启动 Codex、
采音或快捷键；通话时它也与音频 WSS 分开，避免音频代次变化中断快照。Windows 的
`--direct-serve` 常驻 EXE 原子更新 `runtime/reader-context-snapshot.json`；每个 Codex
会话通过已配置的 `--reader-context-mcp --state <absolute-path>` 轻量 stdio 进程读取同一份
快照，并按实际配置注册 `reader_context_snapshot`、`reader_visual_image`、受限浏览控制、
结构化 Reader 输出与渐进能力指南。这里没有 `/mcp` Streamable HTTP 端点。App 本机 Reader
使用专用 context WSS 与同一视觉合同，原生合成图按需回传 Windows，不经过 Pi、不进入文字
快照。活动心跳同时携带选区三态（有选区 / 已清空 / 未上报），
换页、取消选择或超过三分钟时都不会把旧正文、旧选区继续当作当前内容；新鲜度按 Windows
实际收到心跳的时间计算。关闭同步或切回旧注入时，`context-clear` 先清本地页与选区，
再停止实验末端或恢复 Pi 旧推送。旧代码与 `/4` 回滚入口保留，但两条路径不得并跑。

Reader 语音任务必须直接调用该 MCP 工具，不能先用 PowerShell/Python 读取快照。后一种做法会
在 Codex Desktop 中生成 `commandExecution`；当前 Windows 客户端的 Process Manager 会为
历史命令持续启动进程快照查询。共享状态文件只是 Windows 服务与 stdio MCP 之间的实现细节，
不是 agent 的备用读取接口。

## 单用户安全边界

免浏览器配对并不等于接受任意来源：

- PWA 只允许精确生产 Origin；
- Chrome 只允许项目固定扩展 Origin；普通网页不能直接选择 WSS URL；
- Tailscale Serve 注入的单值 `Tailscale-User-Login` 必须与本机配置精确匹配；
- C# 只绑定 `127.0.0.1:43128`，同一时刻只允许一个 Reader 连接和一个 Active session；
- 网页不能下发 endpoint ID、路径、命令、AUMID、目标进程或快捷键；
- `HELLO`、`STATUS`、模型选择、刷新和 synthetic click 都不能产生启动副作用。

`experimentalSingleUserMode=false` 在 `/4` 与 `/5` 中均 fail closed；不回落旧 v1 配对协议。

## 代码入口

### ReaderPC 服务器托盘总控

`ReaderPC 服务器`是 Windows 上的独立托盘总控，不替换或重打包已验收的
`BW-Computer-Voice-Bridge.exe`。它统一展示电脑语音、Reader 上下文快照和
PC 预处理，但三者仍是独立故障域。PC worker 只向 Pi 发起出站 HTTPS；
空闲只用 `nvidia-smi` 读取显卡信息，真正任务开始后才导入 PyTorch、模型和显存。

统一的无凭据本机状态写入
`%LOCALAPPDATA%\BWReader\readerpc-server.status.json`。安装器使用版本化
`%LOCALAPPDATA%\BWReader\ReaderPC-Server\releases\<version>`，新版只切换
`current.json`、开始菜单和桌面快捷方式，旧 release 保留为回退点。最小化仅隐藏到
托盘；关闭窗口或从托盘退出会先停止 PC 预处理与电脑语音/上下文直连。安装与运行不会
自动创建开机项；开机启动仍是用户显式选项。

```powershell
$py = 'C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe'
& $py extensions\bw-reader-webext\windows\package_readerpc_server.py --build <version>
& $py extensions\bw-reader-webext\windows\package_readerpc_server.py `
  --self-test extensions\bw-reader-webext\windows\readerpc-candidates\<version>\readerpc-server-<version>-windows-x64.zip
& $py extensions\bw-reader-webext\windows\package_readerpc_server.py `
  --install extensions\bw-reader-webext\windows\readerpc-candidates\<version>\readerpc-server-<version>-windows-x64.zip --launch
```

图像型 PDF 没有可供拖选的原始字符；必须先显式选择 Apple、Pi 或 PC
预处理，再在 App 中采用/切换对应派生文字层。ReaderPC 只解决 PC 执行器
在线与任务运行，不会伪造 PDF 自带文字层，也不覆盖原 PDF。

- Reader 直连、麦克风上行与 context journal：
  `_server_deploy/static/pdf/rc-computer-voice.js`
- 电话按钮与引擎同步：
  `_server_deploy/static/pdf/rc-voicecall.js`、`rc-assistant.js`
- 扩展固定 WSS relay：
  `extensions/bw-reader-webext/background.js`
- Windows 协议、process-loopback、虚拟麦克风写入与 named pipe：
  `extensions/bw-reader-webext/windows/ComputerVoiceAudio/`
- Windows 桌面控制面：
  `extensions/bw-reader-webext/windows/computer-voice-desktop/`
- typist 候选真源：
  `extensions/bw-reader-webext/windows/typist-runtime/`
- 可复现候选打包、事务安装与显式回退：
  `extensions/bw-reader-webext/windows/package_computer_voice_direct.py`

Direct 发布只使用该脚本的 `--build` / `--verify` / `--self-test` / `--install` /
`--rollback`。安装前脚本验证候选和现有安装、备份固定 payload，精确停止 owned
`direct-serve` 与同一安装路径下、参数严格匹配的 Reader MCP 进程；安装、自检或服务恢复
任一步失败都会恢复旧 payload。配置、`runtime/`、`dotnet8/` 与其它本地数据不在替换范围。

## 验证与尚未完成

```powershell
node --test tests/reader_contract/*.test.mjs
$env:PYTHONUTF8 = "1"
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe `
  -m unittest discover -s tests -p "test_*.py"
```

C# 的 `--describe`、`--self-test`、endpoint 枚举、
`--probe-codex-app-audio-route`、`--probe-direct-output-route` 和无启动诊断不得启动
capture、Codex、typist 或快捷键。前者只读取当前唯一 Codex root PID 的六项持久应用路由，
逐项返回 target/match/`Present|Unset|Error`，并固定报告
`audioRouteMutated:false`；可在通话前后精确核对恢复，但仍不能证明真实双向声音。

安装、部署与发布事实以协作状态最新登记和现场命令为准。最后证据必须由用户在真实
Reader/PWA 与扩展页面完成：

1. Windows 控制面分别选择 A eRender、A eCapture 与 B eRender，确认保存 `/5` 并启用服务；
2. 通话前运行只读 app-route probe，记录 Codex 六项原路由；无需预先打开音量混合器；
3. 页面选择 Windows 桥接器并点击电话，确认能自动拉起所需应用；
4. Reader 麦克风能进 Codex，Codex 输出只回 Reader，物理/RDP 扬声器不发噪音；
5. Reader context 能按所选末端到达当前会话；挂断后所有 owned 资源停止，再运行只读
   app-route probe，确认六项恢复原值或保留用户中途手工改动；
6. 分别在 PWA 与隔离扩展测试 profile 验证错误状态、再次拨号和 RDP 断开后的稳定性。

由于这是新功能、UI 与交互变更，正式 Reader/PWA 部署前必须完成该人工验收。第三方驱动安装、
登录 supervisor、替换现有 EXE、提交/推送和生产部署均是后续显式门；不能用旧 v2 的
RDP endpoint 诊断或无声单元测试冒充 v3 实声验收。
