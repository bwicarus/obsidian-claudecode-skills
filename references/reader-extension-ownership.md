# PWA / 浏览器扩展能力与数据归属

> ⚠ **状态复核（2026-08-19）**：PWA 已于 2026-08-14 退役 —— `/pdf/`、`/pdf/search`、`/pdf/epub/view`、`/pdf/fav/view` 由 `_server_deploy/reader_pwa_retirement.py` 的 `RETIRED_PAGE_ENDPOINTS` 返回 410；正式交付表面只剩 **iOS App**（`ios/BWReader` 的 ReaderBundle 本地渲染）**与浏览器扩展**。下文所有「PWA」列/行只作历史契约保留，不得据此新增或保留为 PWA 做的兼容取舍。本文未登记 App 表面：某条阅读器路由到底由谁执行，以 `ios/BWReader/native_reader_interface_manifest.json` 的 owner **加** `_server_deploy/static/pdf/native-local-runtime.js` 里有无本地分支为准（判据 `python scripts/where_does_this_route_run.py <路由>`）。

> 当前候选版本以 `extensions/bw-reader-webext/manifest.json` 为唯一来源，本文不写死版本号（本轮为 0.2.121，仅作参照）。目标不是让扩展成为 PWA 的后台 provider，而是：
> 普通网页全部由扩展实现；真书由 PWA 渲染，扩展存在时接管共享 UI/网络/通用数据。

## 运行矩阵

| 场景 | UI 所有者 | 内容宿主 | 通用数据 | 文档私有数据 |
|---|---|---|---|---|
| 普通网页，无扩展 | 无 BW | 原网页 | 无 | 无 |
| 普通网页，有扩展 | 扩展 | `WebAdapter` | 扩展本地优先 | 网页 DOM/quote placement；新墨迹仅当前会话 |
| 真书 PWA，无扩展 | PWA 原生 fallback | PWA PDF/EPUB/HTML/Favorite host | PWA fallback/尚未迁移的旧源 | PWA |
| 真书 PWA，有扩展 | 扩展共享 Shell | 同一个 PWA host | 扩展本地优先/尚未迁移的旧源 | PWA |

真书是 PDF、EPUB、导入 HTML/Markdown 和收藏书。PWA 不再承载任意 URL 网页阅读器。

## 能力归属矩阵

| 能力 | 普通网页 + 扩展 | 真书 PWA 无扩展 | 真书 PWA + 扩展 |
|---|---|---|---|
| 选区工具条 | 扩展唯一 UI，`WebAdapter` 提供选区 | PWA 原生 UI | 扩展唯一 UI，PWA host 提供选区 |
| 查词、翻译、解释、语法 | 扩展共享组件与网络层 | PWA fallback | 扩展共享组件与网络层 |
| AI 助手、模型设置、语音 | 扩展 | PWA fallback | 扩展 |
| 侧栏与布局 | 扩展；浮动/挤压影响网页可视区 | PWA | 扩展；PWA host 只报告可用能力/位置 |
| 顶部控制栏 | 只显示网页支持项 | PWA 书籍按钮 | 扩展按书籍 capability 显示 |
| 高亮语义与编辑 UI | 扩展 | PWA fallback/旧源 | 扩展共享 UI |
| 高亮锚点与投影 | Web surface | PWA DocumentHost | PWA DocumentHost |
| 卡片实体与学习状态 | 扩展优先，稳定 ID | PWA fallback/旧源 | 扩展优先 |
| 页面卡片 placement | Web surface | PWA document store | PWA document store |
| 便签实体/编辑器 | 扩展优先 + 共享 UI | PWA fallback | 扩展优先 + 共享 UI |
| 便签落点 | Web surface | PWA document store | PWA document store |
| 绘图状态机与工具 UI | 扩展 | PWA | 扩展 UI 调 PWA host |
| 绘图 stroke | 仅标签页会话，不持久化 | PWA document store | PWA document store |
| 句级预翻译、未掌握词下划线 | 扩展 Web 功能 | 由书籍 host 的现实现决定 | 扩展 UI + 书籍 host |
| 阅读位置、页码、裁边、缩放、排版 | 网页不适用或 capability 隐藏 | PWA | PWA host，扩展只发白名单动作 |
| PDF 文字层/OCR/页几何 | 不适用 | PWA | PWA |
| EPUB 结构/offset/reflow | 不适用 | PWA | PWA |
| 导入 HTML/Markdown 文档锚 | 不适用 | PWA | PWA |
| 账户 namespace | 扩展保存最后一次经 PWA 验证的账户 | 服务端 session | PWA session + 扩展已验证 namespace |
| API token | 扩展后台私有 IndexedDB | 无扩展私有 token | 扩展后台私有 IndexedDB |
| 全局设置 | 扩展本地权威 | PWA fallback | 扩展权威；PWA 只补缺失值 |
| 设备/书籍设置 | 扩展设备项；网页会话项不跨设备 | PWA | PWA document/device store |
| 查询/翻译/词典缓存 | 扩展按账户 | PWA fallback | 扩展按账户 |
| 跨设备同步 | 本地 journal + 服务器 relay 已落地（`_server_deploy/reader_sync_relay.py`，`sync-gateway/2` / `sync-v3`，`/api/reader/sync/{exchange,snapshot,signal,owner/*}`，由 `app.py` 无条件 `register_reader_sync_relay` 注册） | 历史 PWA journal | 各端提交归属内变化 |
| 第三方网页抓取 | 浏览器直接访问；受控后台请求 | 不支持 | 不支持 |
| 旧网页代理/RBI | 不使用 | 不使用 | 不使用 |

## 所有权规则

### 扩展负责

- 任意普通网页的完整 BW 产品界面与交互；
- 有扩展时，PWA 真书页上的共享顶部栏、选区工具、侧栏、AI、卡片编辑和设置；
- 通用实体、跨书数据、全局设置、派生缓存；
- 模型 token 与网络请求执行；
- 普通网页 DOM/quote anchor 的创建和恢复；
- 普通网页 session-only ink；
- 与服务器交换带 revision/mutation 的变化。

扩展不得解释 PWA 的 PDF/EPUB/HTML 私有 anchor，也不得在普通网页显示只有书籍 host 才能执行
的死按钮。

### PWA 负责

- 真书文件的导入、保存、解析和显示；
- PDF/EPUB/HTML/Favorite `DocumentHost`；
- 书内精确选区、可见内容、位置、导航、搜索；
- 书内高亮投影、卡片/便签 placement、墨迹；
- 页几何、OCR、reflow、裁边、缩放、排版和渲染缓存；
- 无扩展时完整 fallback UI 与数据。

扩展接管后，PWA 只隐藏重复共享 UI，不得停止 renderer、删除原 UI 或改变 DocumentHost。

### 服务器负责

- 认证与账户 namespace；
- 跨设备变化 relay（逐步实现）；
- 必须远程执行的受控计算；
- 按账户隔离的旧 sidecar 兼容和无损迁移；
- 编号私有资产的受控读取。

服务器不再抓取、代理、frame 或 RBI 显示任意网页。

## 稳定 ID

- 每个实体第一次渲染前生成唯一 ID。
- 学习卡保持 `id === cid === gid`；普通卡至少保持 `id === cid`。
- 侧栏、收藏夹、AI 结果和页面钉住引用同一个实体；placement 有自己的位置 ID，但不是新卡。
- 删除 placement 不删除源实体；只有明确删除实体命令才能删除本体。
- PWA fallback 与扩展数据合并时按 ID + revision；真实分叉进入 conflict。
- 旧 `card_`、`c_`、`fcg_`、entity/asset ID 不重编号。

## 存储范围

统一记录外壳使用稳定 ID、revision、updatedAt、mutationId 和 tombstone。物理归属分三类：

- `global`：扩展优先；无扩展真书使用 PWA fallback。
- `document`：PWA 真书 document store；普通网页由扩展 Web surface/store。
- `device/session`：当前设备或标签页；不得误同步成业务数据。

普通网页新墨迹明确属于 `session`。历史 `webInkV1` 保留但不继续写，防止为了新规则破坏旧数据。

`DataRegistry` 是允许进入统一 DataStore/provider/sync 的唯一 collection 白名单。尚未接线的
卡片、收藏、词汇、对话、标注等继续使用旧源，不能因目标归属已经确定就删除旧读取链。

## PWA 接管安全门

扩展只对四个精确真书路由尝试接管：

- `/pdf/view`
- `/pdf/epub/view`（⚠ 2026-08-14 起返回 410，见 `_server_deploy/reader_pwa_retirement.py` 的 `RETIRED_PAGE_ENDPOINTS`；扩展 manifest 仍匹配这条 URL，但已无可接管的页面。四条里实际还活着的是 `/pdf/view`、`/pdf/html/view`、`/pdf/fav/open`）
- `/pdf/html/view`
- `/pdf/fav/open`

接管必须同时满足：

1. 唯一 BW origin；
2. 顶层 frame；
3. 页面 meta 的 app/route 身份匹配；
4. `book-host/1` 和 `bw-reader-pwa/1` 版本匹配；
5. `HELLO` 返回 capability；
6. 扩展 Shell/adapter 初始化成功；
7. `TAKEOVER` 得到确认。

只有第 7 步后 PWA 才隐藏原生共享 UI。心跳 5 秒、租约 15 秒；GOODBYE、断开或过期恢复 PWA。
Favorite 路由使用 EPUB 壳，但身份必须是 `app=epub + route=favorite`，不能误判成普通 EPUB。

## 设置与凭据

- 扩展 global settings 是有扩展场景的权威，跨普通网站一致。
- 进入 PWA 时只把 PWA 中缺失的键补入扩展，不以旧 PWA 值覆盖扩展。
- 无扩展时 PWA 的 PreferenceStore 保持完整可用。
- API token 只能存于扩展后台私有 IndexedDB；内容脚本通过固定 operation 请求网络，不能拿到明文。
- WebRTC 内容宿主可在内存中持有服务端返回的不透明 `accountProof`，用于 peer 账户相等性
  围栏；它不含 namespace、不能换取服务端权限、不得作为业务身份持久化。证明以已验证的
  `registryDigest` 作为协议代际盐，同账户同代际稳定，跨代际 fail closed。
- 旧裸 `apiToken` 只可迁移成不含 token 的审计存根；不得继续暴露。
- 网络层只接受固定 operation 和参数 schema，禁止退化成任意 URL/method/body 代理。

## 旧 sidecar owner

2026-07-24 用户确认：开发阶段只有本人，现有未分区服务端 reader sidecar 属于当前认证主账户。
代码仍必须使用认证解析的数字 uid + `storage_namespace`，不得硬编码用户名/uid。

迁移采用 inventory → checksum → 原子复制 → 复核 → manifest-last；旧源永久只读保留。身份、
账户数、manifest 或 checksum 不一致时 fail closed。新账户从空分区开始，所有稳定 ID 原样复制。

首批已覆盖 reading-pos、phrases、notes、PDF/EPUB/HTML highlights、entity/assets。
ink 与 userpages 已随后纳入分区（`_server_deploy/reader_sidecar_store.py` 的 `LEGACY_DATASETS` 含 `pdf-ink`/`epub-ink`/`reader-userpages`）；仅剩 vocab/Anki、conversation、attention、favorites 等 deferred-owned 域继续保留
原功能，之后逐域无损迁移。

## 禁止事项

- 不得恢复 PWA 任意网页解析器或 `/pdf/web/live` 阅读模式。
- 不得把扩展降回“只给 PWA 提供数据”的 0.2.37 模式。
- 不得在 TAKEOVER 前隐藏 PWA fallback UI。
- 不得为统一源码而删除有差异的现有功能。
- 不得让扩展读取或改写 PWA 私有 anchor。
- 不得把普通网页响应式坐标当成可长期保存的墨迹坐标。
- 不得为解决外站图片而给扩展增加无约束的全网读取权限；应通过编号资产的受控服务接口。
