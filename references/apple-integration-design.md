# Apple 系统联动设计（2026-08-27 用户拍板方向）

用户决定：① 苹果提醒事项做**显示副本**（我们的通知系统是真值）；
② 小组件**按功能分开设计**；③ 地图不止深链——学习场景（"这个国家
在哪"）要能**在 App 甚至 Safari 里显示**。

## 一、提醒事项显示副本（EventKit）

**方向：单向投影 + 完成状态回流。** Windows 通知系统（audience=user）
是唯一真值，苹果提醒事项只是它在系统层的投影 —— 这样锁屏/手表/Siri
全都能看到我们的待办，而状态机仍归我们管。

- **投影**：App 同步收到用户向通知（现有 Windows→App 数据路线）→
  `EKReminder` upsert。`dedupe_key ↔ reminder calendarItemIdentifier`
  的映射存 App 本地（App Group，widget 也要读）。standing todo 的
  `activate_at` 映射为提醒的到期时间。
- **回流**：用户在苹果提醒里勾完成 → App 下次前台/同步时读回
  completed → 走既有 `notification-action resolve`（by=user）回流
  Windows。**苹果侧勾选 = 用户 resolve**，语义完全等价于侧栏 tab 里
  按「完成」。
- **真值优先**：Windows 侧 resolve/cancel/expire → App 把对应提醒
  删除（不是勾完成 —— 显示副本退场就该消失，留一堆已勾条目是噪音）。
- **权限**：iOS 17 Reminders full access（要读回勾选状态，write-only
  不够）。专用日历列表「BW 待办」，绝不碰用户自己的提醒列表。

## 二、小组件（WidgetKit，按功能分开）

**数据通道统一**：App 主进程把各功能数据写进 App Group 共享容器的
JSON（`widget-review.json` / `widget-notifications.json` /
`widget-sync.json`），widget timeline provider 只读。App 在前台、
后台刷新、收到同步时写并 `WidgetCenter.reloadTimelines`。

⚠ 诚实原则：小组件是**快照模型**（系统给刷新预算，分钟级），
不假装实时 —— 每个组件都带"数据时刻"。

| 组件 | 内容 | 尺寸 | 交互 |
|---|---|---|---|
| **复习** | 今日到期卡数 / 已完成数 / 下一批到期时刻（App 本地 replica 卡字段 `_next`/`_st` 直接算，数据零依赖网络） | 小、中 | 点击深链进复习 |
| **待办通知** | 用户向通知前 N 条，[新] 标记，standing todo 置顶 | 中、大 | iOS 17 交互按钮「完成」→ App Intent → 既有 resolve 回流链 |
| **同步状态** | 最后一次与 Windows 成功同步「x 分钟前」+ 成功/失败色点。**不做"当前连接"**（快照模型撑不住秒级；真实时是 Live Activity 的活，二期） | 小 | 点击打开 App 触发一次同步 |

- **工程影响**：`ios/BWReader` 新增 Widget Extension target + App
  Group entitlement（主 App 同时要加）→ CI `safari-extension-ios.yml`
  的构建/签名配置要跟着改。这是三件事里唯一动工程结构的。

## 三、地图（学习场景的"它在哪"）

用户纠偏：不只是导航深链 —— 学到某个国家/城市时要能**看到**它在
地图上的位置，App 和 Safari 网页里都要能显示。

**第一档（先做）：静态地图卡。** 复用现有 `images` 卡渲染管线
（零新卡 kind —— 那份白名单全链 17 份副本，不动它）：AI 查到坐标后
送 `images` 卡，`url` 指向静态地图图片（OSM staticmap 类服务），
`page` 字段放 Apple Maps 深链（点图跳导航/大地图）。App 与 Safari
两个表面立即同时生效，因为 images 卡本来就在两边都能渲染。

**第二档（有实际不满再做）：交互式内嵌。** Leaflet + OSM tiles 嵌进
卡片 HTML。代价：外网 tile 依赖、扩展/App 的 CSP 开口、离线不可用、
一份新的 JS 依赖进 vendor。静态图满足"看到在哪"的 90%，先验证
需求强度再付这个价。

**AI 侧**：工具文档补一句"用户问地理位置时，送静态地图 images 卡
（附 Apple Maps 深链），不要只用文字描述"。

## 实施顺序（用户 2026-08-27 之前的优先级 + 本次拍板合并）

1. 提醒事项投影 + 本地通知（纯 App 侧，不动工程结构）
2. 地图静态卡（AI 文档 + 一个 staticmap URL 约定，最小）
3. 小组件三件套（动 Xcode 工程 + CI，单独一批做）
4. Live Activity 连接状态（二期）
