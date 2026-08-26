# iOS 提醒送达架构（2026-08-26）

用户诉求：「提醒要真的叫醒我」——问到了虚拟来电与系统闹钟。本文记录
**已建成的三条通道**、调研确立的事实、以及剩下那个洞的两条候选方案。

⚠ 本文所有 Apple 侧事实都来自 developer.apple.com 的 DocC JSON 端点
实取（SPA 页面 WebFetch 取不到内容，要 `curl .../tutorials/data/...json`）。
凡标注"未确认"的，就是真的没查到，不要当成结论用。

---

## 一、已建成：三条并行通道

一条提醒从 AI 创建到叫醒用户，现在有三条独立通道，**失效场景不重叠**：

| # | 通道 | 覆盖 | 失效于 |
|---|---|---|---|
| 1 | 到点本地通知（`UNCalendarNotificationTrigger`） | 全 iPadOS 版本 | 静音键；通知权限被拒 |
| 2 | 苹果提醒事项 + `EKAlarm` | 全版本 | 提醒事项权限被拒 |
| 3 | **AlarmKit 系统闹钟** | iPadOS 26+ | 系统低于 26 |

通道 1 与 3 的**共同优点**：排上之后就归 iOS 管 —— App 被杀、桥离线、
iPad 断网都照响。通道 3 官方明文**压过静音与专注模式**，并转发到配对
的 Apple Watch。

排程发生在两处，都用**同一批 identifier**（同 id 是替换语义，天然幂等）：

- **App 内**：书页运行时每 30 分钟一次的系统投影同步；
- **Widget 内**：timeline 每 15–60 分钟被系统唤起时顺路排。
  这一条补住了最后的洞 —— AI 在电脑上建的提醒，若 iPad 到点前**没打开过
  App**，此前没有任何人去排那条通知。

### AlarmKit 的三条反直觉事实（实取，别按常识猜）

1. **没有 entitlement**。官方 entitlement 索引里 "alarm" 零命中（对照
   "healthkit" 有命中，说明检索方式没错）。唯一的门是 Info.plist 的
   `NSAlarmKitUsageDescription` —— 缺了就排不了闹钟。
2. **Widget extension 只在用 countdown 呈现时才必需**。原文：
   "AlarmKit expects a widget extension if an app supports a countdown
   presentation." 我们用固定时刻 + 纯 alert，不触发该条件。
3. **`stopIntent` 可选**，停止/贪睡由 `AlarmManager` 自己处理。

API 已随 iOS 26 点版本漂移：26.1 加了无 stopButton 的 `Alert` 初始化，
27.0 beta 给三个 `AlarmConfiguration` 工厂都加了 `appEntityIdentifier:`。
**照 26.0 的签名写**，别抄文档页上混排的 beta 符号。

### 编译期的一个隐患（已知，未消除）

CI 用 `macos-latest`（当前 = Xcode 26.6 / iPhoneOS26.5 SDK，从成功构建
的工具链输出读到，非推断），**但没有任何 pin**。哪天该标签回退到旧
Xcode，`#if canImport(AlarmKit)` 会**静默**把整块编译掉 —— 绿色构建、
功能消失、零日志。所以闹钟状态串必须一路带回投影回执
（`revision: system-projection/1:notifications=…;reminders=…;alarms=…`），
让"编译进没进"在运行时可见。

---

## 二、剩下的洞

三条通道都要求**有人在到点之前把提醒排上**。目前排程只发生在 App 打开
或 widget 被系统唤起时。极端场景仍会漏：iPad 整天没碰、widget 没放在
常翻到的页上、系统预算耗尽。

补法有两条，调研把它们的优劣翻了个个儿：

### ❌ 后台刷新（BGAppRefreshTask）——不作为主通道

技术可行、API 明确，但它是**机会性**通道不是定时器：Apple 只承诺"不早
于 `earliestBeginDate`"和"最多 30 秒"，何时跑完全由系统定，且被三个开关
静默掐断（设置里的后台 App 刷新、低电量模式、用户强制退出）。
把 `earliestBeginDate` 当闹钟用会得到"提醒晚了几小时或压根没响，且设备
上没有任何报错"。

⚠ 落地时必查的坑：Info.plist 的 key 真名是
`BGTaskSchedulerPermittedIdentifiers`（**不是** `…PermittedTaskIdentifiers`）。
写错的表现是静默失败：`register` 返回 false、`submit` 抛 `.notPermitted`。
两个返回值都必须记进日志。

结论：只配当"趁醒来时预排"的补货器，不承担到达保证。

### ✅ APNs 推送——推荐，但调研推翻了两个前提

原设想是"Windows 桥发静默推送唤醒 App 去排提醒"。两处都要改：

1. **发普通 alert 推送，不要静默推送。** 静默推送（content-available）
   被限流到约 2–3 次/小时、低电量或关了后台刷新时被丢弃、**用户强制
   退出 App 后完全不投递**；而普通 alert 推送不限流、扛得住强制退出、
   且**根本不需要唤醒 App**——iOS 自己画通知。提醒文本在发送时就已知，
   所以我们压根不需要唤醒 App。
2. **发送方应该是 Pi，不是 Windows。** 提醒必须在到点那一刻发出，而
   Windows 是一台会睡的笔记本——本仓库旁边整个 `C:\autoscreen\` 项目
   就是为管它的睡眠而存在的。Pi 24 小时在线、本来就是同步中继。
   让 **Windows 创建**提醒、**Pi 发送**。
   .p8 凭据也该放 Pi：它是团队级凭据（每团队上限 2 把、不可重新下载），
   放一台会睡会备份的笔记本上是错的地方。

工程量：几天级（.NET 8 / Python 发推送本身约 120 行，HTTP/2 与 ECDsa
都不需要新依赖）。**真正的成本不在推送代码**，在于：
- App 目前没有任何远程通知代码；
- 加 Push Notifications capability 后必须**手工重新生成描述文件并轮换
  `APPLE_APP_PROFILE_BASE64` secret**，CI 不会替你做。忘了的表现可能是
  构建成功但 profile 里没有 `aps-environment`，推送**静默不工作**；
- **没有 Mac = 没法本地调试推送**（装不了 development 构建、用不了
  `xcrun simctl push`、attach 不了调试器）。每一个关于 entitlement /
  token / 环境的假设都只能靠一次 TestFlight 往返来验证——这才是排期
  的主导项；
- 推送权限**只能弹一次**，反射性点了"不允许"就永久粘住，需要一个
  刻意设计的前置说明。

---

## 三、虚拟来电（CallKit + PushKit）——远期，先做 go/no-go

用户问过"能不能像打电话一样提醒"。技术链条标准且文档明确：
Pi → APNs(HTTP/2) → PushKit → `CallKit.reportNewIncomingCall`。
但工程量是 **2–3 周级**，且有三个必须先看清的东西：

1. **【go/no-go】中国区域风险**。CallKit 自 2018 起对中国大陆用户不可用。
   已确认的是分发层要求与各家 App 自行按 region 关闭的做法（Telegram、
   element-ios 都是在 App 代码里判 CN 后跳过，说明那层是 App 自律）。
   **未确认**：iOS 是否在"设备区域 = 中国大陆"时直接让
   `reportNewIncomingCall` 失败 —— Apple 从未回应，论坛帖挂了三年多。
   TestFlight 绕开了中国区 App Store 分发，所以**可能**能用。
   → 用一个丢弃构建花半天先测掉，不要放到最后。
2. **AVAudioSession 归属要重构现有能跑的语音功能**。实测
   `NativeAudioEngine.swift` 自己调 `session.setActive(true)`；CallKit 下
   **禁止**这么做（只能配 category，等 `provider(_:didActivate:)` 再启动
   音频）。这会动到 120KB+ 已在工作的语音代码，是工期最大项也是回归
   风险最高项（2–5 天）。典型失败形态：来电界面正常、接通后没声音，
   或接通后既有语音助手坏掉。
3. **设计前提可能被推翻**：VoIP 推送的终止检查发生在**从 delegate 返回
   的那一刻**，不是超时。所以**不能**"先被唤醒 → 问 Windows 该说什么 →
   再响铃"，payload 必须自带响铃所需的一切（≤5KB）。
   另：反复不上报会导致系统**永久停止**向该 App 投递 VoIP 推送，Apple
   未给出恢复方法 —— delegate 第一行就应无条件 `reportNewIncomingCall`，
   宁可响一次假铃，不要被拉黑。

---

## 四、附带修掉的既有问题（调研顺手抓到的）

- **Widget target 从来没有 `CODE_SIGN_ENTITLEMENTS`**（App 与 Safari
  扩展都有，唯独它没有）→ `containerURL(forSecurityApplicationGroupIdentifier:)`
  在 widget 进程里返回 nil，App Group 读写一直静默无效。这是「最近阅读」
  小组件长年显示占位内容的真正原因。
  当前修法是零签名风险的一条：widget 自拉数据 + 自己沙箱缓存，不依赖
  共享容器。**要真修**得在 Apple 后台给 widget 的 App ID 开 App Groups
  能力并重签描述文件（会动到目前全绿的签名链），留作独立改动。
- **通知权限被拒时投影回执完全看不出来** → 已加 `notificationsState`。
- **广泛的 64 条上限**：iOS 对单 App 未触发的本地通知上限 64，超出静默
  丢弃更远的。App 侧与 widget 侧都只排最近 32 条。

## 五、诊断出口（下次"没响"时先看这里）

| 现象 | 看哪里 |
|---|---|
| 提醒没排上 | 投影回执 `revision` 的三段状态（页面 `window.__BW_SYSTEM_PROJECTION_STATE__`） |
| widget 排没排 | 桥的 `reader-output-pickup.log` 里的 `widget-fetch` 行（widget 每次拉数据时捎回上一轮排程结果） |
| 闹钟没响 | `alarms=` 段：`scheduled=N` / `denied` / `unsupported-os` / `sdk-unavailable` / `failed:…` |
| 页面提示 | 闹钟不可用且确有到点提醒时，页面会 toast 一次 |
