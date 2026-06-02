# iOS 壳（Swift Playgrounds App）

`PDFReaderApp.swift` —— bwicarus PDF 阅读器的 iOS 原生壳：全屏 WKWebView 指向网页阅读器，用
**持久数据仓**（`WKWebsiteDataStore.default()`），让网页的 IndexedDB 整本缓存 + 登录 cookie
存进 **App 沙盒容器** → 不受 Safari 7 天 ITP 清除 / 标签页配额驱逐 → 缓存真正长存、只需登录一次。

## 为什么要这个壳（相对「加到主屏 PWA」）
| | Safari 标签页 | PWA 加主屏 | WKWebView 壳 |
|---|---|---|---|
| 存储清除 | 7 天 ITP 清 + 配额驱逐 | 较稳但 iOS 清过 | App 容器，最稳 |
| 切走重载丢状态 | 常丢 | 常丢 | 壳持有 webview，不重载 |
| 登录 | 可能反复登 | 同左 | 一次，cookie 持久 |

进一步「把书下到真文件系统、彻底离线」需加原生下载 + 自定义 scheme 桥（更复杂，未做；当前靠
WKWebView 持久仓里的 IndexedDB 缓存已能持久存已开过的书，≤220MB/本）。

## 装法（iPad，无需 Mac）
1. App Store 装 **Swift Playgrounds**（免费）。
2. 打开 → **新建** → 选 **App** 模板。
3. 把 `PDFReaderApp.swift` 全部内容覆盖进模板自动生成的那个 `.swift`（含 `@main`，删掉模板原有的 `@main`）。
4. 右上 **▶ 运行** → 装到本机。首次要登录网站账号，之后保持。
5. 改地址：只改 `kStartURL` 一行。

## 注意
- 免费 Apple ID 签的 App 约 **7 天**要回 Swift Playgrounds 再跑一次重签；**App 沙盒里缓存的 PDF 不会丢**，重签后还在。99 美元/年开发者账号可免重签。
- 站点走 Tailscale HTTPS（Let's Encrypt 有效证书），无需 ATS 例外。
- obsidian:// 等自定义 scheme 链接会丢给系统打开。
