# 手表伴侣 app（watchOS）

2026-08-27 加。三件事：看 AI 发的卡片、看待办、开关电脑语音桥。

## 一句话架构：手表看到的是手机的镜子

**手表够不到 tailnet。** watchOS 没有 Tailscale 客户端，手表也不走 iPhone 的
VPN 隧道。而这三样东西的数据源（Windows 桥 `bwicarus-2.taile44d0c.ts.net`、
Pi 的 `/voice-rt`）都只在 tailnet 内可达，**认证方式就是「你在 tailnet 里」**。

所以 iPhone 是手表**唯一**的数据源，走 WatchConnectivity。由此推出一条必须
写在 UI 上的语义：

> 手机 App 不在跑时，手表上不会有新卡片。这不是 bug，是这个架构的定义。

每一屏顶部都显示「这份数据有多旧」。宁可让人看见陈旧，也不让陈旧冒充现状。

## 手表直连电脑语音（方案 A，2026-08-27 用户拍板）

⚠ **本节推翻了下一节。** 下一节那三条理由里两条已经不成立，保留它是为了
记录当时的判断错在哪 —— 直接删掉的话，后来的人会重新推一遍同样的错误。

**关键事实（当时漏掉的）**：TN3135 有三条豁免，第二条是
「**CallKit 通话进行中，低层网络解禁**」（watchOS 9+），
`URLSessionWebSocketTask` 正在解禁之列。所以手表直连**不是死路**。

Apple 工程师在论坛原话：「Your Apple Watch does not need to be in proximity
with your iPhone in order to receive VoIP calls via a watchOS app that uses
CallKit; the Watch just needs a WiFi or cellular connection.」—— 这正是
「手机 app 关着也行」。

### 已定的取舍

| 决定 | 结果 |
|---|---|
| 路线 | **A**：Windows 经 Funnel 暴露 + OAuth，手表直连（不走 Pi 中继） |
| 方向 | **只要手表呼叫电脑**，不要电脑呼叫手表 |

第二条省掉一大块：来电唤醒要 PushKit → App ID 上的 Push Notifications
capability → **那是唯一需要人去 Apple 后台点的东西**，而 ASC API 自动开
capability 这条路本仓库 2026-08-05 试过并整段回退（ac119d01 → 64e5ab2e）。
去电全程 controller→transaction→delegate，不碰 push。
**所以方案 A 现在零 Apple 后台操作。**

`UIBackgroundModes = ["voip"]` 是 Info.plist 属性**不是 entitlement**：
不进 App ID、不进描述文件，手表 target「零 capability」的约束不破。

### ⚠ 第 0 步：一个 Apple 从未文档化的前提

**CallKit 的网络豁免在熄屏 / 放下手腕之后是否存续 —— Apple 没写过。**
不成立的话方案 A 整个不成立（形态退化成「抬腕才能说话」）。

实验在 `Watch/WatchCallProbe.swift` + `scripts/watch_probe_server.py`。
两条会让实验白做的坑（都已避开）：

- **模拟器恒通**。TN3135 原文：「the simulator always allows low-level
  networking」。只能真机，而且**不能挂 Xcode 调试器**（挂着 app 不会被挂起，
  测的是调试器不是系统）。所以证据必须落盘 + 服务端旁证。
- **判据是时间序列，不是单次事件**。已有报告称豁免生效期间网络路径本身就会
  周期性 withdrawn/restored ——「掉一次线」≠「豁免没了」。

另两处纠正：被拦住的 `URLSessionWebSocketTask` 报 `NSURLErrorDomain -1009`
而**不是** `ENETDOWN=50`（那是 NWConnection 的形态，守着 50 会一直等不到）；
主信号用 `NWPathMonitor`（TN3135 点名：被拦时恒 `.unsatisfied`），
它区分得开「豁免没了」和「网断了」，WebSocket 通不通区分不开。

### 顺带验证到的事实

- **Funnel 在这个 tailnet 里确实能用**：从 tailnet 外取 Pi 的 Funnel 端点
  拿到 HTTP 401（OAuth 闸正常拦截）。⚠ 但公共 DoH 查 `bwicarus.taile44d0c.ts.net`
  返回 NXDOMAIN —— **那是假阴性**，Funnel 的名字解析不走那条路。
  **能实测的别去推断。**
- `--set-path` 是 A 路要用的机制，第 0 步顺带先验一遍。

## ⚠ 为什么不做「手表直接语音对话」

用户原本要的第四件事。**明确不做**，理由不是工作量：

要让手表直连语音端点，只能把 Windows 桥暴露到公网。而那台机器能控浏览器、
能开麦克风、能操作 Anki —— 这个交换不划算。仓库里本来就有代码在拦这条路
（`references/reader-computer-direct-bridge.md`「Funnel 永不启用」；
`control_plane.py` 会把带 `AllowFunnel` 的配置判成 foreign 拒绝操作）。

替代形态：**手表当遥控，手机当耳朵嘴** —— 戴耳机对手机说话，手表管开关和
看状态。这跟「语音桥开关」是同一件工程，增量几乎为零。

另外两条硬约束（就算愿意开公网也绕不过）：48kHz 双工音频经手机中继不现实；
watchOS 不给持续后台录音。

## 状态

2026-08-27 首个构建已上传 TestFlight（CI run 33002752800，三轮：
第一轮挂在 Shared/ 文件漏加进 App target，第二轮挂在嵌入目录搞反了，
第三轮全过）。bundle ID 与描述文件都是 CI 自动建的，Apple 后台没动手。

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
