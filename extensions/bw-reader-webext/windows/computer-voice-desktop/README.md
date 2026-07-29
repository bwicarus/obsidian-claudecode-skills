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
```

Pi 继续提供 Reader/PWA 和书籍数据，但不进入电脑语音的信令或媒体链路。
浏览器只能连接已经存在的 Windows listener，不能在完全无 listener、电脑
睡眠或 bootstrap 未安装/未运行时凭空唤醒本机进程；这时 Reader 必须显示
“Windows 桥接器离线”。一次显式安装登录后台引导器后，listener 才能在
Windows 登录会话中被持续监督，而电话 `START` 只负责按合同启动 Codex、
捕获与快捷键阶段。

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
完整 EXE 路径、再终止，避免两次打开之间的 PID reuse。偏离时 fail closed。状态刷新、麦克风选择和
配对码生成不会调用 `ProcessRunner.start()` 或 `schtasks /Run`。

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

合同为 `reader-computer-voice-direct-config/1`，字段集合必须恰好为：

```json
{
  "contract": "reader-computer-voice-direct-config/1",
  "localOptIn": true,
  "microphoneEndpointId": "{explicit-endpoint-id}",
  "listenHost": "127.0.0.1",
  "listenPort": 43128,
  "allowedOrigins": [
    "https://bwicarus.taile44d0c.ts.net"
  ],
  "allowedTailscaleUserLogin": "bwicarus@gmail.com",
  "pairingCodeHash": "",
  "pairingExpiresAtUtc": null,
  "pairedClientPublicKeySpki": "",
  "pairedClientFingerprintSha256": "",
  "outputScope": "process-only",
  "appKind": "codex-desktop",
  "runtimeStatusPath": "C:\\Users\\bwica\\bw-computer-voice-bridge\\runtime\\computer-voice-direct.status.json"
}
```

- `listenHost` 必须逐字为 `127.0.0.1`，端口固定为 `43128`。
- `allowedOrigins` 只接受无路径、查询或 fragment 的 HTTPS origin。
- `allowedTailscaleUserLogin` 必须逐字为已确认的
  `bwicarus@gmail.com`，GUI 和 Reader 都不能提供替代值。
- `outputScope` 固定 `process-only`，没有系统输出回退。
- `appKind` 固定 `codex-desktop`。Reader 不能提交路径、命令或 AUMID。
- 本机应用 allowlist 只在代码内维护：
  - Codex：`OpenAI.Codex_2p2nqsd0c76g0!App`
  - Classic：`OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT`

当前 strict config 只选择 Codex；Classic ID 仅作为本机枚举预留，不能由
Reader 选择。

## 一次性配对安全

- Windows 用 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` 生成恰好 10 字符的码。
- 明文只在当前 GUI 内存和标签中显示，不写文件。
- 配置只写
  `base64url-no-padding(SHA-256(UTF-8(code)))`，固定 43 字符，并带
  最长 5 分钟的 UTC 过期时间。
- PWA 生成 ECDSA P-256 密钥对，PAIR 请求提交一次性码与 SPKI 公钥。
- C# 服务消费成功后原子清空码摘要/过期时间，只长期保存客户端
  `pairedClientPublicKeySpki` 与 SHA-256 指纹；没有可导出的长期 bearer
  token。
- 已配对时服务拒绝新的 PAIR。GUI 生成新码不会暗中清旧公钥；只有用户
  确认“重新配对”后才同时清公钥与指纹。

## runtime status 合同

C# 原子写
`reader-computer-voice-direct-runtime-status/1`，字段必须恰好为：

```text
contract / serviceInstanceId / pid / state /
readerConnected / captureActive / updatedAtUtc
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
只有经认证的 Reader 电话 `START` 才能请求这些后续动作。C# 已接入当前
生产 capture 适配器，但 Windows ↔ Reader/PWA 的真实设备 E2E 尚未验收；
在验收完成前不能把 listener 在线或 mock 通过冒充通话可用。

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

- 旧原型缺失 `self.pair_button` 时所有按钮提前崩溃的回归；
- offline/stale/PID 不匹配不能显示 online 或 Reader connected；
- direct config 严格字段、固定 Tailscale 登录身份、无长期 token、明文
  pair code 不落盘；
- 已配对设备必须显式撤销才能重新配对；
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
