# 电脑客户端语音桥接

## 当前架构

电脑语音是单用户实验功能。Reader/PWA 与扩展页面都通过同一份
`rc-computer-voice.js`；书籍 PWA 与普通网页使用不同的受信传输入口，最终都只连接同一固定
Windows 地址：

```text
书籍 PWA（精确生产 Origin）
    ⇅ wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1

普通 HTTP(S) 页面
    ⇅ 扩展 isolated content runtime
    ⇅ 固定 chrome.runtime Port
    ⇅ 扩展 background
    ⇅ wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1

Windows Tailscale Serve
    ⇅ 127.0.0.1:43128
Windows C# direct server
```

Pi 仍提供 Reader/PWA 和书籍数据，但不参与电脑语音的配对、控制、信令或音频传输。
旧 Pi `/api/reader/computer-voice/*` 路由、扩展 popup 配对入口和
offscreen/Native Messaging 媒体链已退出当前路径；保留的 `offscreen.js` 只是惰性
tombstone，不能读取旧记录、访问 Pi、连接 native host 或启动采音。新的 background
只做固定 WSS 字节中继，不读配对或账户 storage，也不自行生成 START。

## 用户流程

1. 在 Reader/PWA 或任意已加载扩展的 HTTP(S) 页面选择“电脑客户端”。选择模型、打开设置、
   刷新状态都不会启动 Windows 应用、采音或发送快捷键。
2. 页面不生成或输入配对码，不要求填写 endpoint，也不生成、保存浏览器设备身份或长期凭据。
   页面只使用代码内固定 WSS；扩展和 PWA 的行为相同。
3. 只有一次 `event.isTrusted === true` 的电话按钮点击才签发五秒、一次性 start lease；
   顶栏电话同步转发到隐藏电话按钮仍只消费同一 lease。脚本 `.click()`、直接调用
   `startFromUserGesture()`、过期或重复消费都不能发送 START。通过门禁后，页面发送 direct
   v2 `HELLO`，随后为该次随机 session 发送 `START`。Windows 可在 START 中按白名单自动
   拉起 Codex；完全离线、睡眠或尚未安装登录 supervisor 的电脑不能被浏览器凭空唤醒。
4. Windows 在 START 路径重新检查本机 `localOptIn`、明确选定的麦克风 endpoint、唯一 Codex
   进程树和 `process-only` 输出范围，再启动两条 PCM 管线。禁止默认麦克风、默认输出和
   全系统输出回退。
5. 活跃通话必须持续发送递增 heartbeat；挂断、连接关闭、心跳超时或媒体错误都会停止本次
   capture、停止本次 START 新建且 PID 仍匹配的 voice-typist，并释放唯一连接。START
   中途失败和服务退出走同一清理；原本已运行或由别处竞态启动的 typist 不归桥接器停止。
   状态刷新和重连不能续期 START。
6. 浏览器若阻止或暂停 AudioContext，下行只保留最新 20 ms，旧帧丢弃且不回放，也不会因
   原 400 ms 队列上限自动断开 WSS。播放已运行时若合法 PCM 突发令排程超过 400 ms，则丢弃
   已排队 source 并从当前时刻重建排程，同样不关闭连接。用户再次真实点击电话按钮只恢复
   当前通话的声音，不发送第二次 START。

浏览器侧免配置不等于 Windows 采音边界被取消。麦克风选择和 `localOptIn` 仍只在 Windows
本地保存；网页不能下发设备 ID、路径、命令、AUMID、任意目标进程或快捷键。

## 实验单用户认证边界

direct v2 不做浏览器配对、公钥签名或 bearer token 认证，但也不再接受任意 HTTP(S) Origin：

- 书籍 PWA 只允许精确 `https://bwicarus.taile44d0c.ts.net`；
- Chrome 只允许本项目固定扩展 ID
  `chrome-extension://jddhhakcblmihidgdobfkcejjinpigak`；
- Safari Web Extension 的实际 Origin host 是安装期 UUID，当前只允许 canonical
  `safari-web-extension://<UUID>`，且禁止端口、路径、userinfo、query 与 fragment。这比
  任意网页 Origin 窄，但同一 Tailnet 设备上另一个 Safari 扩展仍是单用户实验阶段的剩余边界。

普通网页不能直接使用 WSS；共享 DOM 上的脚本点击又不能取得 trusted start lease。扩展
isolated content runtime 只把固定操作发给 background，background 还会复核扩展自身 sender、
顶层 frame、canonical HTTP(S) 页面、每标签唯一连接、精确 wrapper schema、64 KiB 文本上限和
1,956-byte PCM，下游 URL 不可由页面指定。

当前仍保留以下边界：

- WSS host/path、localhost listener 和 Tailscale Serve 映射均固定，C# 只绑定
  `127.0.0.1:43128`；
- Tailscale Serve 注入的 `Tailscale-User-Login` 必须是单值，并与 Windows 配置中的唯一
  Tailnet 用户精确匹配；缺失、多值或不匹配在 WebSocket upgrade 前拒绝；
- 同一时刻只允许一条 Reader WebSocket 和一个活跃 session；
- Windows 必须已经本地 opt-in，麦克风必须明确选定，输出固定为 Codex 进程树；
- 只有真实电话按钮点击的一次性 lease 产生的 START 可以启动应用、capture 和一次快捷键；
  HELLO、STATUS、刷新、选择模型以及 synthetic click 均不能；
- 连接关闭、取消、心跳超时和媒体 pump 故障都 fail closed。

关闭 `experimentalSingleUserMode` 会回到严格 Origin/旧 v1 认证兼容路径；当前 Reader 和扩展
只使用 v2。配置中的旧 pairing 字段仅为向后兼容保留，不是现行用户流程。

桥接 STOP 能确定停止两路 capture、PCM pump、本次 owned typist 和 WSS session；截至当前
Codex 桌面应用没有已接线且可验证的“退出 Voice”自动化 primitive，因此桥接器不会猜测第二次
快捷键是 toggle。也就是说，桥接资源的停止可验证，但 Codex Voice UI 是否退出仍需用户观察；
不能把前者冒充成后者。

## 代码入口

- 共享 Reader/扩展入口：`_server_deploy/static/pdf/rc-computer-voice.js`
- 电话按钮集成：`_server_deploy/static/pdf/rc-voicecall.js`
- 扩展固定 WSS relay 与旧路径 tombstone：
  `extensions/bw-reader-webext/manifest.json`、`background.js`、`offscreen.js`
- Windows direct 协议与媒体：
  `extensions/bw-reader-webext/windows/ComputerVoiceAudio/`
- Windows 桌面控制面：
  `extensions/bw-reader-webext/windows/computer-voice-desktop/`

## 验证与未完成边界

合同回归直接运行：

```powershell
node --test tests/reader_contract/*.test.mjs
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe `
  -m unittest discover -s tests -p "test_*.py"
```

Windows C# 的 `--describe`、`--self-test` 和
`--diagnose-direct-audio-no-start` 均不得启动 capture 或发送快捷键。服务在线、合同测试
通过和无启动诊断通过，都不能单独证明真实双向声音已经可用；最终仍需在真实 Reader/PWA 或
扩展页面点击电话按钮，分别验证麦克风上行与 Codex 进程输出下行。

RDP 会把“远程音频”作为只在该会话中存在的虚拟 endpoint，并不等于独占 Core Audio 接口。
本机 13:54 的 Session 1 为 Active 时，无启动诊断对 process output 与“远程音频”麦克风均
返回 HRESULT 0；13:58 断开后，同一会话的精确麦克风枚举已变为 0 个 Active。WTS disconnect
会关闭或失效该会话既有 WASAPI stream：此时新麦克风初始化应失败，而 process-loopback 即使
仍能初始化，在 Codex 没有活动 render stream 时也只会持续静音。因此实声验收必须在相同
Session 1 已连接、状态为 Active、远程麦克风与声音重定向均已启用时进行。若目标是断开 RDP
后只靠 iPad Reader 使用，就必须改选本地活动会话中真实存在的物理麦克风；当前协议不包含
Reader/iPad 麦克风上行。
