# 手表伴侣 app（watchOS）

2026-08-27 加。三件事：看 AI 发的卡片、看待办、开关电脑语音桥。

## 一句话架构：手表看到的是手机的镜子

**手表够不到 tailnet。** watchOS 没有 Tailscale 客户端，手表也不走 iPhone 的
VPN 隧道。而这三样东西的数据源（Windows 桥 `bwicarus-2.taile44d0c.ts.net`、
Pi 的 `/voice-rt`）都只在 tailnet 内可达，**认证方式就是「你在 tailnet 里」**。

所以**卡片和待办**这两样，iPhone 是手表唯一的数据源，走 WatchConnectivity。
由此推出一条必须写在 UI 上的语义：

> 手机 App 不在跑时，手表上不会有新卡片。这不是 bug，是这个架构的定义。

⚠ **语音是例外，别把这句推广过去**：语音走手表**直连 Pi 的公网 Funnel**
（见下「连续双工」节），不经手机、不需要 tailnet。「手表够不到 tailnet」限制的
是 tailnet-only 的端点，而 Funnel 恰恰是把端点搬出 tailnet 的机制。

每一屏顶部都显示「这份数据有多旧」。宁可让人看见陈旧，也不让陈旧冒充现状。

## 🔴 方案 A（CallKit 直连）已作废 —— 2026-08-27 用户真机实测

**作废理由不是网络，是界面：通话进行中手表只显示系统通话 UI，
我们自己的界面一点都显示不出来。**

用户当天先用原生通话实测确认了这一点。它跟 Apple DTS 的说法对得上 ——
watchOS 的 VoIP 支持「basically locks the screen into the CallUI」。

于是整条推理链塌了一半：CallKit 是**用来换 WebSocket 的**（TN3135 豁免②），
而换来的代价是锁死界面。**只要走 CallKit，就没有我们自己的 UI。**
所以「第 0 步验豁免存不存续」也不必做了 —— 就算存续，形态也不可接受。

`scripts/watch_probe_server.py` 与 `Watch/WatchCallProbe.swift` 保留但不再需要跑。

### 作废前已定、现在一并失效的取舍

| 当时的决定 | 现状 |
|---|---|
| 路线 A：Windows 经 Funnel 暴露 + OAuth，手表直连 | ❌ 界面被锁，作废 |
| 只要手表呼叫电脑，不要电脑呼叫手表 | 这条**仍然成立**，见下 |

第二条之所以还成立：来电唤醒要 PushKit → App ID 上的 Push Notifications
capability → **那是唯一需要人去 Apple 后台点的东西**，而 ASC API 自动开
capability 这条路本仓库 2026-08-05 试过并整段回退（ac119d01 → 64e5ab2e）。

### ⚠ 顺带作废：「打电话过来汇报」也不成立（免费约束下）

2026-08-27 调研（workflow `wn2mn6tsw`）结论：**iOS 没有音频注入 API**。
FaceTime 吃的是物理麦克风，第三方 app 无法把 TTS 送进通话 ——
macOS 有 BlackHole 那类虚拟音频设备，iOS 没有对应物。
所以 iPad 发起的 FaceTime **接起来是静音的**。

> **FaceTime 只能当门铃，不能当话筒。**

另有一条 Apple 文档化的拦路虎：「运行会打开 app 的快捷指令时，设备锁定要先
解锁」，而 `打开 URL: facetime-audio://` 正是打开 FaceTime app。

「AI 主动把人叫住」和「叫住之后跟他说话」是**两件事**：前者免费可做（见下），
后者在手表上只有第一方通话能干，而第一方通话不对第三方开放音频。

用户已确认：**电话只作为通知手段保留，不为手表做任何迁就。**

## 连续双工：不用 CallKit，也不用 HTTP 凑合 —— 用音频会话豁免

用户要的是「按一下按键开始桥接电脑上的通话」——连续双工，不是按住说话。

### 🔑 关键事实：TN3135 有**三条**豁免，CallKit 只是第二条

**第一条是「app 有活动音频会话时」（watchOS 6+）** —— 而语音通话本来就有
音频会话。所以 **WebSocket 在手表上是可用的，不需要 CallKit，界面归我们。**

所有人（包括本仓库前两版判断）以为这条路不通，是因为一个**静默陷阱**：

> `AVAudioSession.setActive()` **不抛错，但也不解禁 WebSocket。**
> 必须用异步的 `activate(options:completionHandler:)`。

出处（2026-08-27 一手核实，不是转述）：

- Apple 开发者论坛 thread 773362。开发者在 Series 7 + Series 10 /
  watchOS 26.6 双机实测得出上面那句；Apple DTS 的 Quinn 在同帖回复。
- 公开参考实现 `github.com/leptos-null/WatchOS-WebSocket`，提交历史就是全部故事：
  `57d5899` "Migrate to asynchronous AVAudioSession activate call"（**修好的那一行**）、
  `4e322c1` "Remove unneeded audio playback"（**不用真的放声音**，有活动会话就够）、
  `f3c6fa1` "Remove unneeded UIBackgroundModes"（**连后台模式都不用**）。
  它的 `Info.plist` 是空的 `<dict/>`：零 capability、零后台模式、不用 AirPods。

⚠ **这正是 `silent-failure-lessons.md` 规则一的形状**：提前退出不出声，于是
十个人撞上同一堵墙、都以为是平台限制。那两个「证明双工不可能」的论坛帖
（777373、781095）用的都是 `setActive`，都在模拟器通、真机报 POSIX 50 ——
**那不是硬件限制，那是拒绝的签名**。而 TN3135 明写「the simulator always
allows low-level networking」，所以模拟器永远给假阳性。

### 由此定下的形状

```
手表（我们自己的界面 + 活动音频会话解禁 WebSocket）
   ── WSS ──→ Pi（已有公网 Funnel + OAuth）
   ── WSS，走 tailnet ──→ Windows 语音桥
```

| | 前一版（已废）的以为 | 现在 |
|---|---|---|
| 传输 | HTTP 双流（无先例，三个坑） | **`NWConnection` + `NWProtocolWebSocket`** |
| 进程存活 | `WKExtendedRuntimeSession` | **活动音频会话** |

后者一并解决了 ERS 的两个毛病：**没有 1 小时硬顶**，且**扛得住按数码表冠**
（frontmost 型 ERS 一离开 app 就 `.resignedFrontmost`）。

Quinn 另外点名：**用 `NWConnection` 不要用 `URLSessionWebSocketTask`** ——
「watchOS 上每个 session 都有点像后台 session，实际工作在进程外做」。
参数用 `parameters.serviceClass = .interactiveVoice`。

### ⚠ Pi 必须是「减震器」，不是「转发器」

Windows 桥的序号是 **fail-closed 硬校验**（`DirectPcmSequenceGuard` /
`DirectUplinkSequenceGuard`：帧序号必须恰好等于上一帧 +1，否则
`InvalidSequence`，无 resume）。那对 LAN/USB 级链路是合理契约，**对手表射频
是致命的 —— 掉一帧就挂断整通电话**。

所以 Pi 侧：**自己当上行序号的唯一来源**，按 50Hz 恒定节拍发帧，手表的帧只是
「这一格填什么」，没来就填静音（尾部淡出防咔哒），迟到即丢。
`seq = up_seq++`、`ts = up_t0 + seq*20000` 天然满足严格递增。

> **严格的一侧永不断，容错的一侧随便断。**

同理 **零转发**：Pi 只接受 `op ∈ {start,stop,ping}` 枚举，自己拼 hello/start/
heartbeat/stop。代码里不存在「把手表来的 JSON 塞给 Windows」的路径 ——
所以 `anki-*` / `browser-control` / `codex-voice-set` 那一族**在结构上够不到**，
而不是靠黑名单拦。这是 CLAUDE.md「改白名单先数有几份副本」的正面版本：
**不存在的代码路径不会被顺手加一条绕过。**

### 📋 第一轮真机实测（2026-08-27，用户实机）

探针建在已上 TestFlight 的手表 app 里（构建 498）。结果：

> **A/B/C/D/E 全部连上**，然后 **一息屏或切后台就断**
> （`POSIXErrorCode 57: Socket is not connected`）。

两条结论，第二条才是要害：

#### 1. ⚠ 负对照没失败 → 这一轮什么都没证明

`D`（同步 `setActive`）和 `E`（完全不碰音频会话）**本该连不上，却都连上了**。

只要负对照没失败，正面结果就**不能归因于音频豁免** —— 连上可能只是因为
前台的 watchOS 压根没拦，或者被前面跑过的档留下的音频会话搭了便车
（第一轮是按 A→B→C→D→E 顺序跑的，污染很可能就来自这里）。

> **这是 `evidence-quality-lessons.md` 那条规则的反面演示**：
> 采集设计错了，分析层再怎么写也救不回来。

第二版把「D/E 必须杀掉 app 重开后**第一个**跑」写在按钮上方 ——
不指望人记得测试顺序。

#### 2. 前台能连、后台就断 —— 这条才决定设计成不成立

而第一版有个盲点让它查不下去：**分不清「豁免被收回」和「进程被挂起」**。
这两件事修法完全不同（前者改音频会话怎么维持，后者改后台模式怎么声明）。

第二版补了两样：

- **`NWPathMonitor`**：TN3135 点名，豁免被拦时 path 恒 `.unsatisfied`。
  日志里有 unsatisfied = 豁免被收回；日志直接断在息屏那一刻、恢复前台才继续
  = 进程被挂起。战报里分开报。
- **`.duplexCall` 档（边放边录）**：第一版**从头到尾没有真的"播放"过声音**。
  而 `UIBackgroundModes: [audio]` 的保活语义是「app **正在播放**音频时给额外
  运行时」，C 档只装了输入 tap（只录不放），很可能压根不满足它。

  ⚠ 参考实现那条 `4e322c1 "Remove unneeded audio playback"` 移除的正是一段
  持续的近静音循环 —— **它敢标 "unneeded" 是因为那个 demo 只在前台跑**。
  我们要的恰恰是后台存活，所以那段不是多余的。

  近静音的幅度取 `1/32768` 而不是真零：完全无声的流系统有可能不认账。

### 测不掉、只能承受的

- **音频中断不可后台恢复**：来电/Siri 打断后 watchOS 不允许在后台重新激活
  录音。长通话期间高概率发生，无软件侧规避，只能提示用户回前台。
- **电池**：估算 15–25%/小时，**无实测数字**。
- **后台模式键已从 `UIBackgroundModes: [voip]` 改成 `UIBackgroundModes: [audio]`**。
  ⚠ 两个坑：① `voip` 是 CallKit 时代的遗留，在没有 CallKit 的 watchOS app 上是
  死键；② **watchOS 用 `UIBackgroundModes` 不是 `WKBackgroundModes`** ——
  后者存在但取值只有 self-care / mindfulness / physical-therapy / alarm /
  workout-processing，填 `audio` 会被 App Store 判 90362，**而且只在 altool
  上传时才报**（归档、签名、校验全过）。2026-08-27 为此烧掉一整轮流水线，
  已在 CI 校验段加断言提前拦。依据是参考实现 `f3c6fa1` 提交移除的正是
  `UIBackgroundModes[audio]`。

### 顺带验证到的事实

- **Funnel 在这个 tailnet 里确实能用**：从 tailnet 外取 Pi 的 Funnel 端点
  拿到 HTTP 401（OAuth 闸正常拦截）。⚠ 但公共 DoH 查 `bwicarus.taile44d0c.ts.net`
  返回 NXDOMAIN —— **那是假阴性**，Funnel 的名字解析不走那条路。
  **能实测的别去推断。**
- `--set-path` 仍然是要用的机制（现在用来挂 Pi 的语音路径，不再是暴露 Windows）。
- **模拟器永远给假阳性**：TN3135 原文「the simulator always allows low-level
  networking」。手表侧任何网络结论只认真机。

## ⚠ 「不做手表直接语音对话」——这个旧结论已作废，留着记录错在哪

原文说不做，给了三条理由。**三条现在全部不成立**，逐条对照：

| 当时写的 | 现在 |
|---|---|
| 「只能把 Windows 桥暴露到公网」 | ❌ 不用。走 Pi 的**已有** Funnel 中继，Windows 一行不改、一个端口不开 |
| 「48kHz 双工音频经手机中继不现实」 | ✅ 这条**对**，但结论下错了 —— 不现实的是**经手机**（WCSession 单条 64KB 上限），手表**直连**不经手机 |
| 「watchOS 不给持续后台录音」 | ❌ 给。Apple 框架工程师原话：前台、用户发起的录音，**进后台可继续**。真正的约束是「不能在后台**重新**激活」，不是「不能持续」 |

⚠ 这一段之所以留着不删：**同一段推理已经错了三次**（先「纯技术死路」、
再「必须 CallKit」、再「只能 HTTP 双流」），每次都是因为漏看一条豁免或者
把一个约束的适用范围放大了。删掉的话下一个人还会重推一遍。

替代形态「手表当遥控、手机当耳朵嘴」仍然作为 **fallback 保留**
（`WatchVoice.swift` 那条按住说话），不删 —— 它不依赖上面任何一条未验证假设。

## 状态

2026-08-27 首个构建已上传 TestFlight（CI run 33002752800，三轮：
第一轮挂在 Shared/ 文件漏加进 App target，第二轮挂在嵌入目录搞反了，
第三轮全过）。bundle ID 与描述文件都是 CI 自动建的，Apple 后台没动手。

**2026-08-27 第二个构建（含网络豁免探针）已上传**（CI run 33044146611，
build 498）。这一轮又烧了四次，三个错**没有一个在探针本身**：

1. `encode` 是 @MainActor 类的静态方法，却要在 nonisolated 的 WCSession
   代理里同步调用（早先「replyHandler 立刻回执」那个修复留下的）；
2. `activateFromLaunch` 是 nonisolated static，被写成了 `.shared.` 实例调用；
3. 我把 `UIBackgroundModes` 整个换成了 `WKBackgroundModes` —— 只该换值。

⚠ 第 3 条**编译、归档、签名、校验全过，只在 altool 上传时报 90362**。
这是本仓库第二次栽在「只有上传才报」的错上（上一次是 PlugIns/ 位置）。
已在 CI 校验段加断言提前拦。

**装法**：iPhone 的「Watch」App → 可用 App → BWReader → 安装。

## 文件

| 文件 | 作用 |
|---|---|
| `ios/BWReader/Shared/ReaderWatchPayload.swift` | 手机↔手表的载荷契约（两个 target 都引用） |
| `ios/BWReader/Watch/BWReaderWatchApp.swift` | 手表 UI（卡片/待办/语音三屏） |
| `ios/BWReader/Watch/WatchLink.swift` | 手表侧 WCSession + 本地缓存 |
| `ios/BWReader/App/ReaderWatchLink.swift` | 手机侧 WCSession + 缩略图降采样 |
| `_server_deploy/static/pdf/native-local-runtime.js` | `__bwWatchCardMirror`（App 专属入口） |
| `_server_deploy/static/pdf/rc-voicecall.js` | `_mirrorCardToWatch`（卡片渲染时顺路镜像） |

## 三个会烧掉构建的坑（都已处理，改动前先看）

### 1. 手表 app 必须在 `Watch/`，**不能在 `PlugIns/`**

`PlugIns/` 是给 `.appex` 扩展的。把一个 watchOS app bundle 放进去，**归档会过、
校验会过**，但 `altool` 上传时判不出平台，报
`Cannot determine the 'platform' from the info.plist`。

⚠ 2026-08-27 我在这里绕了一圈：调研引 XcodeGen issue #1613 说 Xcode 16+ 要求
嵌进 `PlugIns/`，我照着打了 pbxproj 补丁，结果就是上面那个上传失败。
**那个 issue 说的是 "Foundation extension"（扩展），不是手表 app**，不该照搬。
补丁已撤，XcodeGen 生成的 `Watch/` 本来就是对的。

CI 校验段有一条位置断言（必须在 `Watch/`、且不能出现在 `PlugIns/`）——
就是它把这件事钉出来的。**留着它**：这类错误归档不报、上传才报，而上传
在流水线最后，错一次要等很久。

### 2. `SUPPORTED_PLATFORMS` 会漏进手表 target

`project.yml` 项目级 `settings.base` 写死了 `iphoneos iphonesimulator`，而
XcodeGen 的 watchOS preset 里**没有这一项**压不住它（preset 全文只有三行：
SDKROOT / SKIP_INSTALL / TARGETED_DEVICE_FAMILY）。手表 target 必须自己覆写。

同理：必须写 `platform: watchOS`，**不能用 `supportedDestinations`** ——
后者会让 XcodeGen 不生成 Embed Watch Content 阶段，手表 app 根本不会被嵌进去
（XcodeGen issue #1463）。主 app 那条依赖也**不能写 `copy: destination`**，
跟扩展/小组件那两条形状不同是有原因的。

### 3. CI 校验函数写死了 iOS 的假设

`validate_platform` 原本硬编码 `MinimumOSVersion == "17.0"` 和设备家族
`[1, 2]`。手表两样都不同。已改成参数化 —— **不是放宽**，四个 target 各自
仍被钉住。

## 手表零 capability，是刻意的

手表 target 不要 App Group、不要推送。原因：ASC API 自动开 capability 这条路
本仓库 2026-08-05 试过并整段回退（`ac119d01` → `64e5ab2e`），要 capability
只能人去 Apple 后台点。零 capability 才能让 CI 全自动。

顺带：`BWReaderWidget.entitlements` 在磁盘上存在但 `project.yml` 里没接
（第 164-187 行没有 `CODE_SIGN_ENTITLEMENTS`）——就是上面那次回退留下的，
小组件因此读不到 App Group 容器，这正是它要自己联网拉数据的原因。

## 载荷预算

`applicationContext` 约 262KB 硬上限，**超了整份静默丢失** —— 手表上表现为
「一直没更新」，没有任何地方会说为什么。所以：

- 上限设 60KB（留足余量）
- 超限时**先扔缩略图、再一张张扔老卡片**，而不是整份送不出去
- 缩略图手机侧降到 180pt / JPEG 0.6 —— 手表 46mm 可用区约 208 点宽

⚠ 卡片图**必须随载荷过去**，不能给 URL：手表取不到 tailnet 的地址。JS 侧只在
图已经是 `data:` 时才带过来。

## 卡片只投影，不新增契约

卡片的 kind/字段权威仍是 `_server_deploy/reader_card_contract.py`（六种 kind、
单卡 ≤32KiB、`contract_gaps()` 非空就 fail-closed）。手表拿到的是压扁后的
`{id, kind, title, text, thumbnail}`，**加字段不用碰那份契约**。

文本保底用现成的 `_infoText(card)`：原生视图只是「有则更好」，任何 kind 至少
有字能看。

⚠ `_mirrorCardToWatch(card)` 的调用点有个 `typeof` 守卫，**那不是保险是必需**：
契约测试 `bind-receipt-truthful` 会把 `renderInfo` 单独抽出来 eval，那时同文件
的函数不在作用域里。

## ⚠ 语音路由**故意不进** native_reader_interface_manifest.json

手表说话走 `/api/voice/transcribe` + `/api/voice/agent`，但这两条**不在**
那份清单里，是刻意的：

- 那份清单是 **WebView 网关（ReaderNativePiGateway）的授权表**。手机侧这段
  代码用 `URLSession` 直连 Pi 并附 cookie，压根不经过网关。
- `native-local-runtime.js` 的校验器只放行 `/pdf/api/` 与 `/api/assistant/`
  两个前缀（第 158 行那条正则）。那条规则**存在的意义就是圈住 WebView 能
  代理的范围** —— 为了一个用不着网关的功能去放宽它，是拿安全边界换零收益。
- 2026-08-27 我先加了、被那个校验器拒了，然后撤掉。**撤掉是对的，不是绕过。**

代价：`scripts/where_does_this_route_run.py` 查不到这两条。可以接受 —— 那个
工具回答的是「阅读器路由由谁执行」，而这两条不是阅读器路由。

## 加 target 要同步几处

```bash
python scripts/contract_sites.py ios-target-roster
```

6 处。其中 `MARKETING_VERSION` 2026-08-27 起是**四份**（App/Extension/Widget/
Watch），CI 会校验四者相等。

## Apple 后台不用人动手

CI 照 Widget 那条路：先用 App Store Connect API 查/建 bundle ID，再
`fastlane sigh` 出描述文件 —— **两步都是不存在就创建**。用的是已有的
`APPLE_API_KEY_ID` / `APPLE_API_ISSUER_ID`，不需要新 secret。

唯一需要人去后台的情况是手表要 capability —— 而设计上就不要（见上）。
