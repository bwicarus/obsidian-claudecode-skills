# iOS 单 App 合并交接：阅读器 + 语音 + Safari 扩展（2026-08-03 JST）

> ⚠ **2026-08-03 的合并草案，方案已执行完毕，勿把它当待办**：合并当天即落地（`49abb569`「feat(ios): 合并阅读器语音与 Safari 扩展」），工程在 `ios/BWReader/`（`project.yml` + App / Extension / Shared / Widget），§0 说的「两个 App 抢同一 bundle ID、互相覆盖」已不存在；「由 Codex 执行」的分工已于 2026-08-16 作废（全部工程归 Claude）。§3 目标形态里「App target = ReaderWebView（WKWebView 加载 PWA）」也已过时：阅读器 runtime 现由 `ios/BWReader/package_local_reader.py` 烤成 ReaderBundle 随包发，PWA 阅读器页面 2026-08-14 起返 410（`_server_deploy/reader_pwa_retirement.py`）。**§2 的无 Mac 构建链路、六个 secrets 与五个踩坑仍然成立。**

> 用户决定：把 BWReaderNative（阅读器 + 原生语音）与今天新建的 Safari Web Extension
> 合并成**单个 Xcode 工程**，由 Codex 执行。本文是现状 + 合并草案。

## 0. 必须先知道的一件事：两个 App 正在互相覆盖

`BWReaderNative.swiftpm/Package.swift` 里写的是：

```swift
name: "bwicarus-test"
bundleIdentifier: "space.bwicarus.bwreader2"
teamIdentifier: "7MDVSLPV8F"
displayVersion: "0.2.3"
bundleVersion: "2"
```

而今天做的 Safari 扩展 App 也用了 `bwicarus-test` / `space.bwicarus.bwreader2`
（为匹配 App Store Connect 记录，Apple ID `6793932077`、SKU `bw-reader-ipad-002`）。

**iOS 上同一 bundle ID 只能存在一个 App**，于是：

```
TestFlight 装的 bwicarus-test（Safari 扩展空壳）
    ↓ 覆盖
Playgrounds 装的 bwicarus-test（阅读器 + 语音）
```

用户实测确认：装完扩展后阅读器 App 消失了。目前**两者互斥，装一个就没另一个**。
恢复阅读器的办法是在 Playgrounds 里重新运行，但那又会覆盖扩展。

**这正是必须合并的根本原因** —— 不是"两个图标碍事"，是它们在抢同一个身份。

## 1. Safari 扩展现状：已可用，但有架构级限制

### 已验证可用

用户在 iPad 上实测（日文维基页面）：划词菜单、颜色高亮、词组/复制/翻译/解释/对话/
制卡/笔记/语法/搜索全部出现，字数统计正确。**content script 完整注入，53 个文件
（含 mathjax-full.js）都加载成功** —— 曾怀疑的"体积超 iOS 限制"不成立。

popup 诊断显示：`✓ 连接正常`、账户 `acct-v1-c1b4...6ef12a`、`已验证设备令牌`、
`跨设备同步：正在同步`。即 background service worker 能连 Pi、令牌有效。

### 不可用的部分及原因

| 功能 | 状态 | 原因 |
|---|---|---|
| 划词/查词/翻译（短请求） | 可用但每次现拉 | 本地缓存始终为空，见下 |
| AI 对话 | 无响应 | SSE 长请求撑不过 background 回收 |
| 语音 | **平台上不可能** | content script 拿不到 `getUserMedia` |
| 数据同步 | 永远"正在同步" | 长任务被反复中断，跑不完 |

根因是同一个：**iOS 激进回收扩展 background**。而本扩展的网络层
（`src/facade.js` 的 `__bwReaderFetch`）依赖 `chrome.runtime.connect({name:"bw-fetch"})`
让 background 代发请求；background 一被杀，Port 断开，进行中的请求永远不返回
（前端表现为"一直翻译中"）。

桌面 Chrome 完全成立的架构假设（background 常驻 + 本地数据副本 + 代理全部网络），
iOS Safari 从根上不给。

**结论：扩展只适合承担轻量场景（划词、临时查词、选中翻译）；语音与重数据功能
必须留在原生 App 侧。** 这也是合并后的职责划分依据。

## 2. 已建成的无 Mac 构建链路（合并后要复用）

用户没有 Mac。今天建成的流水线让整条链完全不需要自有 Mac：

- workflow：`.github/workflows/safari-extension-ios.yml`
- 触发：仅 `workflow_dispatch` 手动，带 `upload` 布尔开关（默认只构建不上传）
- 流程：`package_safari.py` 出 Apple 形状的包 → `xcrun safari-web-extension-converter`
  转 Xcode 工程 → 导证书到临时 keychain → `xcodebuild archive` → 导出 IPA →
  （可选）`xcrun altool` 上传 TestFlight
- 已实测成功，IPA 已进 TestFlight 并装到 iPad

### 六个 secrets（已配好，勿动）

```
APPLE_DIST_P12_BASE64      Apple Distribution 证书
APPLE_DIST_P12_PASSWORD    48 字符纯 hex
APPLE_API_KEY_BASE64       App Store Connect API Key(.p8)
APPLE_API_KEY_ID           T5WY2RK237
APPLE_API_ISSUER_ID        61acd65f-f2cf-4d23-a20a-ad3905feeeec
APPLE_TEAM_ID              7MDVSLPV8F
```

证书材料在 `C:\Users\bwica\apple-signing\`（私钥，勿进仓库）。
证书有效期至 2027-08-02。

### 建这条链路时踩过的坑（合并后改 workflow 会再遇到）

1. **PEP 668**：macOS runner 的 python3 归 Homebrew 管，`pip install` 直接被拒。
   解法是 `actions/setup-python` 带独立解释器，且后续调用要统一用 `python` 而非 `python3`。
2. **p12 算法不兼容**：OpenSSL 3 默认 AES-256 + SHA256 MAC，macOS `security import`
   只认 3DES + SHA1，不匹配时报 `MAC verification failed (wrong password?)` ——
   **看起来像密码错，其实是算法**。生成时必须
   `-legacy -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1`。
3. **API Key 权限**：创建描述文件需要 **Admin**，App Manager 会报
   `Cloud signing permission error`。Key 角色创建后不可改，只能重新生成。
4. **altool 按 bundle ID 反查 App 记录**，对不上就报
   `Cannot determine the Apple ID from Bundle ID ... and platform IOS`。
5. scheme 名不可硬编码：转换器生成的 scheme 跟随 app name 并带平台后缀，
   现有 workflow 用 `xcodebuild -list -json` 动态解析。

## 3. 合并草案（供参照，非硬性）

### 目标形态

```
一个 Xcode 工程
  ├── App target        bundle: space.bwicarus.bwreader2
  │     ├── ReaderWebView（WKWebView 加载 PWA）
  │     └── 原生语音（AVAudioSession + WSS 直连 Windows）
  └── Extension target  bundle: space.bwicarus.bwreader2.Extension
        └── Safari Web Extension（今天的扩展资源）
```

App target 沿用现有 bundle ID，**复用已有的 App Store Connect 记录**，不要另建
（另建会再次陷入 ID 冲突）。

### 建议分步，每步可独立回退

**① 工程骨架（不改任何 Swift 逻辑）**
把 `AppModule/*.swift` 七个文件搬进 Xcode 工程，配好 App target。
`Package.swift` 里的这些配置要在工程设置里等价重建：

- iOS 17.0 最低版本、仅 iPad（`supportedDeviceFamilies: [.pad]`）
- 四个方向全支持
- 麦克风用途说明：「用于在阅读时与 Windows 电脑上的语音助手持续通话，
  包括应用进入后台或锁屏期间。」
- `Info.plist`：`UIBackgroundModes = [audio]`、`ITSAppUsesNonExemptEncryption = false`
- AppIcon 资源、accentColor blue、category education

**验收标准：构建出的 App 与当前 Playgrounds 版本行为等价**（能开阅读器、能起语音）。
先证明搬运没搞坏东西，再谈新增。

**② 改 workflow 指向自有工程**
现有 workflow 是"转换扩展 → 得到工程 → 构建"。合并后改为"直接构建仓库里的工程"，
`safari-web-extension-converter` 那步移除或降级为一次性脚手架。
扩展资源仍由 `package_safari.py` 生成后拷入 Extension target。

**③ 挂 Extension target**
`space.bwicarus.bwreader2.Extension` 需要在 Developer Portal 注册 App ID
（Admin 权限的 API Key 配合 `-allowProvisioningUpdates` 可自动创建，但手动建更稳）。

**④ 合入 Swift 四项改进**
音频中断恢复 / 锁屏控制 / 网络切换重连 / App 内诊断 —— 你手上那批。
建议放在骨架稳定之后，避免搬运与新功能混在一起、出问题分不清是谁的。

### 迁移的代价（用户已知悉）

迁 Xcode 后**不能再在 iPad 上改完直接跑**。今天我们迭代 Swift 七八轮成本几乎为零；
之后每轮都要 push + 触发 CI + TestFlight，半小时起。
用户接受这个代价，因为语音已趋稳定、改动频率会下降。

## 4. 源码真值与现场

- Swift 源码：`C:\iCloudDrive\BWReaderNative.swiftpm`（**大写 D 的 iCloudDrive**，
  `C:\icloud\iCloudDrive` 是死副本，曾害我们两天改动没送达 iPad）
- Playgrounds 同步目录：`C:\iCloudDrive\iCloud~com~apple~Playgrounds\BWReaderNative.swiftpm`
- AppModule 七个文件：BWReaderNativeApp / DirectVoiceProtocol / DirectVoiceSocket /
  NativeAudioEngine / NativeVoiceBridge / NativeVoiceSystemIntegration / ReaderWebView
- 扩展源码：`extensions/bw-reader-webext/`，Safari 包由 `package_safari.py` 生成
  （主 manifest 有门禁 `handoff_check.py::audit_manifest` 锁死形状，**不要改主 manifest**，
  Safari 侧的差异全部在 `package_safari.py` 里做）

## 5. 当前 Windows 侧状态（与本次合并无关，供背景）

Codex 与 GPT Classic 两个目标的电脑客户端语音均已实测可用，已安装 0.1.67。
本地有两笔未推送提交（6b3080e、a14e7e2，Classic 冷启动与启停修复），
与远端 42 个提交碰同一批 `ComputerVoiceAudio/*.cs` 文件，合并需另行协调，
不要与本次 iOS 工作混在一起。
