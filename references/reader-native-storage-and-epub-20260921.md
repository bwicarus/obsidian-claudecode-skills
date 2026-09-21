# 存储（iCloud 同账号同步）与 EPUB：现成做法调研 · 2026-09-21

> 用户 2026-09-21 提出两件事：①「存储不能用 Apple 原生的功能代替么」
> ②「能使用 iCloud 同账号不同设备同步数据是最好的」，并要求「参考下现成做法」。
> 下面每条结论都是当天在 Apple 官方文档与 Readium 仓库上核对过的，不是凭印象。

## 先说结论

| 问题 | 现成做法 | 我们该不该照搬 |
|---|---|---|
| 本地存储 + iCloud 同账号同步 | **CKSyncEngine**（iOS 17+） | **该用**，而且我们的数据层形状几乎是为它准备的 |
| EPUB 渲染 | **Readium Swift toolkit** 的 `EPUBNavigatorViewController` | **借架构，不借依赖**（理由见下） |

## 一、iCloud 同步：CKSyncEngine

核对来源：`developer.apple.com/documentation/cloudkit/cksyncengine`（iOS 17.0+）。

官方定义就是「管理本地与远端记录数据的同步」—— 也就是**你保留自己的本地存储**，
它只负责推拉、重试、批次与冲突的管线。这跟 `NSPersistentCloudKitContainer`
（把数据模型交给 Core Data）是两条路。

**为什么它对我们特别合适**：`reader-runtime/data-store.js` 现在已经有
稳定编号、变更日志、墓碑、outbox —— 这正是 CKSyncEngine 要求调用方提供的东西
（`add(pendingRecordZoneChanges:)` + `nextRecordZoneChangeBatch`）。
换句话说我们不需要为了上 iCloud 去重塑数据模型；而如果选
`NSPersistentCloudKitContainer`，就得把记录塞进 Core Data 的形状，跟现有语义打架。

**官方文档里必须记住的几条**：

- 需要 **CloudKit** 与 **Remote notifications** 两个 entitlement。
- **不要用它同步 public database**（文档原文的 Important）。
- 同步时机是**不确定的**（要电量、网络、已登录 iCloud）。需要「现在就同步」时
  显式调 `fetchChanges(_:)` / `sendChanges(_:)`。
- 引擎自己有一份**不透明状态**，**由我们负责持久化**并在下次启动时交还它
  （`CKSyncEngine.Event.StateUpdate`）。这条最容易漏：不存＝每次冷启动全量重来。
- 一个进程里可以跑多个引擎实例（私有库一个、共享库一个）。

**⚠ 它与当前架构的冲突点（必须先定，不然会双写）**：
2026-09-01 定的是「Windows=全量留底＋中继」。若 iCloud 直接在 Apple 设备之间同步，
同一批记录就有两条写入通道。三个选项：

- **(a) iCloud 管 Apple↔Apple，Windows 只收单向留底**（推荐）：手机↔iPad 不再
  经过 Windows/Pi，Windows 仍拿到全量备份，**没有双写**。
- (b) 维持现状：Windows 当枢纽，不上 iCloud。
- (c) iCloud 成为阅读器用户状态的唯一同步通道，Windows 按需向 App 取。

**✅ 用户 2026-09-21 拍板：按 (a) 做。**
iCloud 管 Apple 设备之间，Windows 只收单向留底 —— 手机↔iPad 不再经过
Windows/Pi，Windows 仍拿全量备份，**不产生双写**。

由此定下的两条，改之前先回到这里：
- 阅读器用户状态（高亮/便签/笔迹/插入页/阅读位置）在 Apple 设备之间的权威通道
  是 **iCloud**。Windows 那条 outbox/sync-batch 继续存在，但它的角色是**留底**，
  不是仲裁者 —— 别再往它上面加"谁更新"的判断。
- 因此也**不要**为了省事把 iCloud 合并结果再推一份给 Windows 当"同步"：
  留底是单向的，推回来就又变成双写了。

**落地顺序**（每步都可单独发版，不必一次做完）：
1. Swift 侧实现与 `indexeddb-store.js` 等价的 store（collections / journal /
   mutations / causal parent / 事务批量）。⚠ 现成的验收标准已经有了：
   `tests/reader_contract/indexeddb-store.browser.html` 那套浏览器契约（87 条断言）
   可以直接对着原生实现跑 —— 不用靠人想「还有哪些边界没测」。
2. 把 JS 那侧的 store 换成「经桥调用 Swift store」的后端（`data-store.js` 的
   记录/冲突/墓碑语义不动）。
3. 接 CKSyncEngine：把 journal 里的待同步项喂给 `nextRecordZoneChangeBatch`，
   把拉回来的记录按现有冲突规则并进本地。

⚠ **换存储 ≠ 能删掉网页层**。调用方（document-host、card-repository、整个
runtime）和 EPUB 正文仍是 JS，仍跑在 WKWebView 里。换存储买到的是「数据不再
依赖那个 WebView 活着」「Swift 不开 WebView 也读得到」「能上 iCloud」——
这三件本身就值得做，但别把它当成删旧层的那一步。

## 二、EPUB：Readium Swift toolkit

核对来源：`github.com/readium/swift-toolkit`（`develop`，4.0.0-alpha.2，3553 commits）。

**它先替我们确认了一件事**：EPUB 没有 PDFKit 那样的 Apple 官方渲染器。业界标准
做法就是 `EPUBNavigatorViewController` —— 正文仍由 web view 渲染，外面包一层原生。
文档里这句可以直接当我们的设计依据：

> Navigators do not have user interfaces besides the view that displays the
> publication. Applications are responsible for providing a user interface with
> bookmark buttons, a progress bar, etc.

**也就是说我们 PDF 这一轮做的分工（原生画壳与叠加、网页留判据与正文），
跟这个行业标准是同一个形状。**EPUB 沿用它即可，不必另想架构。

**Decoration API**（`docs/Guides/Navigator/Decorations.md`）值得照抄的是它的**分层**：
- `Decoration` = 位置（Locator）+ 抽象样式 + **稳定 id**（用于跨更新追踪）；
- `Decoration.Style` 只描述**抽象外观**（highlight / underline），与渲染引擎无关；
- EPUB 侧再由 `HTMLDecorationTemplate` 把抽象样式翻成具体 HTML/CSS；
- 按「功能」分组（highlights 一组、搜索命中一组…）。

这正好是我们缺的那层命名：现在 PDF 侧的高亮/下划线/排线/搜索命中各自为政，
若按「抽象样式 + 每表面一个模板」重整，PDF 与 EPUB 能共用同一份声明。

**但不建议直接引入这个依赖**，两个理由：
1. **功能面不匹配**。`epub-html.js` 背着振假名、中日词典、插入页、便签、生词
   下划线、收藏夹物化…… Readium 的 Decoration 只覆盖高亮/下划线那一类。换过去
   等于把这些全部重做一遍，而它们现在是好的。
2. **4.0 还在 alpha**（alpha.2）。把一个阅读 App 的正文渲染钉在 alpha 上，
   风险跟收益不成比例。

⚠ 另有一条注意：文档明说「目前只有 `EPUBNavigatorViewController` 实现了
`DecorableNavigator`」—— 即便引入，PDF 侧也拿不到这套装饰 API。

**所以 EPUB 的现实路线**：保持 `epub-html.js` 当正文与判据，按 PDF 这一轮的同一
套做法，把选区菜单／查词／语法／划线编辑换成原生面板，叠加层交由原生画。
命名与分层照 Decoration 那套。
