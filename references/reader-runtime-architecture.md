# 阅读器本地优先统一架构

> 当前基线不要在本文写死版本号；以 `python3 extensions/bw-reader-webext/handoff_check.py`
> 和 `references/reader-collaboration-status.md` 的最新发布记录为准。历史文档、测试或注释若仍写
> “PWA 内网页阅读器”“五个 PWA 入口”“扩展只做 provider”或 `?ui=legacy`，均为已经废弃的
> 0.2.37 方案，不能继续作为实现依据。

## 1. 产品边界

正式产品只有本地优先的 iOS App 与浏览器扩展。PWA 已于 2026-08-14 废除：`/pdf/`、`/pdf/search`、`/pdf/epub/view`、`/pdf/fav/view` 返回 410（`_server_deploy/reader_pwa_retirement.py`），网页阅读器不再是交付表面。下表中的「PWA」列只作为历史契约保留，不得据此新增或保留兼容取舍。App 不再内嵌远程 PWA；
它把同一套 Reader HTML/JS/CSS 作为本地渲染组件打进安装包，由 Swift 持有文件、生命周期、
系统能力和同步入口。网页/真书运行矩阵固定为：

| 页面 | 无扩展 | 有扩展 |
|---|---|---|
| 普通 HTTP/HTTPS 网页 | 不提供 BW 功能 | 扩展提供完整网页阅读功能 |
| PWA 真书：PDF、EPUB、导入 HTML/Markdown、收藏书 | PWA 提供完整阅读器与本地 fallback | 扩展提供共享 UI、网络和通用数据；PWA 只保留书籍渲染、精确锚点和书籍私有数据 |
| iOS App 本机真书 | App 内置 Reader + 本机数据完整工作 | Safari 扩展不进入 App 的 WKWebView，也不参与所有权 |

由此得到六条硬边界：

1. PWA 不再抓取、解析、代理或远程渲染第三方网页。
2. 扩展在任意普通 `http://` / `https://` 页面上都可提供 BW 网页功能，不要求旁边开着 PWA。
3. 扩展加载到 PWA 真书页后，PWA 的重复共享界面只有在接管成功后才隐藏；渲染器和
   `DocumentHost` 不更换。
4. 服务器主要承担账户认证、跨设备变化中转和确实不能在本机完成的受控计算，不再作为普通
   网页阅读器。
5. App 内禁止启动 Service Worker、PWA install/cache、扩展 TAKEOVER、PWA owner lease 或
   远程首页；WKWebView 只是 App 内部渲染器，不是第二个客户端。
6. App 本机文件与本机记录是默认真源；Pi 只在用户触发同步或启用明确的同步策略时参与，
   断网不能阻止打开、阅读、标注、制卡和保存。

旧 PWA 网页/RBI 路径已经退役并留有只读备份。`/pdf/web/live?url=` 只校验并跳转原网页；
旧 proxy/frame/resource/RBI 接口返回 `410 pwa_web_reader_retired`。不得重新把它们接回产品。

## 2. 唯一目标结构

```text
共享产品层
  ├─ 唯一视觉令牌、组件和动作名
  ├─ 选区工具、侧栏、AI、卡片、设置、翻译、词汇
  ├─ Card/Note/Ink controller
  └─ StorageRouter / NetworkGateway
             │
             ├── 普通网页 + 扩展
             │     ├─ WebDocumentHost
             │     ├─ Extension local store（权威）
             │     └─ Web surfaces（DOM/quote/CSS highlight）
             │
             ├── iOS App 本机真书
             │     ├─ Swift 文件/安全作用域/系统能力/同步入口
             │     ├─ App-owned local store（权威）
             │     ├─ PDFKit 基础页面 + PDF DocumentHost / 共享网页叠层
             │     └─ EPUB 内置网页 renderer + EPUB DocumentHost
             │
             ├── 真书 PWA + 扩展
             │     ├─ Extension shared UI（唯一可见共享 UI）
             │     ├─ PWA BookHost RPC
             │     └─ PWA renderer/anchors/document store
             │
             └── 真书 PWA 无扩展
                   ├─ PWA fallback shared UI
                   ├─ 同一个 PWA renderer/anchors
                   └─ PWA local fallback store
```

“统一”指上层语义、状态机、视觉组件和数据接口统一，不表示所有宿主使用同一种坐标或 DOM：

- PDF：页码、字符层、OCR、页几何；
- EPUB：章节、offset、reflow；
- 导入 HTML/Markdown：文档内文字/DOM 锚；
- 普通网页：URL、quote/DOM 锚和响应式页面元素。

这些宿主私有数据只能由对应 `DocumentHost` 创建和解析。禁止把 PDF 矩形冒充网页锚点，
也禁止因接口名字相同而删除格式专属能力。

## 3. 运行时与接管协议

### 普通网页

扩展 manifest 对任意 HTTP/HTTPS 页面静态注入完整网页功能链。`WebAdapter` 负责：

- 选区、可见内容、页面位置和网页内搜索；
- DOM/quote 高亮与未掌握词下划线；
- 句级预翻译和沉浸式翻译；
- 页面卡片、便签与元素绑定；
- 网页临时绘图；
- 共享侧栏、AI、设置和网络请求。

普通网页不得出现 PDF/EPUB 专属的页码、裁边、双页或书籍设置死按钮。UI 必须从 adapter
声明的 capability 生成，缺失能力时 fail closed。

### iOS App 本机真书

- App 从安装包内加载固定版本的 Reader 壳与共享组件，不请求远程首页。PDF 的目标基础页面由
  `PDFView`/PDFKit 承担原生渲染、滚动、缩放和自带文字层选择；EPUB 继续使用安装包内网页
  renderer。
- Swift 只向页面提供不透明本机书 ID；bookmark、绝对路径和 security-scoped URL 永不进入 JS。
- PDF `DocumentHost` 继续作为页码、字符/OCR 层、稳定锚点、搜索和页几何的语义权威；PDFKit
  只替换基础页图与原生选择表面，不能另造第二套身份或坐标真源。共享网页层以透明叠层同步当前页、
  zoom 与 viewport，仅绘制笔迹、卡片、便签、AI 工具和其他跨端组件，不再重复绘制整张 PDF 页。
- 迁移期间仍允许现有 PDF.js 基础页面作为兼容回退，但新增本机 PDF 能力优先接入 PDFKit 路径，
  回退不得成为长期双实现。EPUB 由本机容器提供 manifest、section 与资源等价接口，继续复用 EPUB
  `DocumentHost`。本地首版不注入书籍自带
  CSS，只使用 Reader 的受信默认样式；恢复书内 CSS 的前提是先完成可靠的作用域隔离与协议净化，
  不能让不受信书籍样式覆盖 App 的可信 UI。
- 共享网页组件负责卡片、便签、选区、侧栏和阅读交互；App 本机存储负责它们的数据真值，
  原生层负责 Pencil、语音、后台、文件、分享和系统入口。
- 普通电话选择 OpenAI Realtime 时，长期项目 key 只保存在 App 与 Safari 扩展共享的 Apple
  Keychain access group。App/扩展原生进程用它向 OpenAI 申请短期 `ek_` client secret；页面只拿
  短期凭证，SDP、麦克风和 `oai-events` DataChannel 由设备直连 OpenAI，禁止把长期 key 注入 JS、
  URL、日志或快照。
- App 本机书与 Safari 扩展的 Realtime 上下文也不经过 Pi：当前页文字、可见窗口、选区和笔迹状态
  由本机 `RC.voiceCtx` 注入，`see_ink` / `see_page` / `see_figure` 的真实合成图由 App 原生桥送入
  同一通话。App 或原生桥不可用时失败可见，禁止静默回落 `/rtc-client-secret`、`/voice-rt` 或其他
  Pi 控制 sideband。本机 `make_note` 也只写 App；制卡、联网搜索、深度思考、后台 CLI、造纸和
  长文路由可作为明示工具按需调用 Pi AI API，但不能反向成为 App 通话或本地工具的前置。
  PWA/非 Safari 客户端可继续把 Pi 当显式 AI API。
- 联网 AI、翻译或 Pi 同步失败只降级相应网络能力，不能令本机文档或本机修改不可用。

### PWA 真书

PWA 真书入口现状（2026-08-14 退役后）：

- `/pdf/view` —— 仍 200（带 `?file=`），历史入口
- `/pdf/html/view` —— 仍 200，且是 vault 里 .md/.html 书的唯一阅读途径
- `/pdf/fav/open` —— 仍 200
- `/pdf/epub/view`、`/pdf/`、`/pdf/search`、`/pdf/fav/view` —— **410**（`reader_pwa_retirement.py`）

下文的 `book-host/1` 两阶段接管只对仍 200 的入口有意义，且不再是投入方向。

PWA 暴露 `book-host/1`，扩展通过 `bw-reader-pwa/1` 两阶段接管：

1. 扩展发送 `HELLO`，校验 origin、精确路由、页面 marker、协议和 capability。
2. 扩展把共享 Shell 与 adapter 初始化完成。
3. 扩展发送 `TAKEOVER`；只有此时 PWA 才隐藏原生顶部栏、原生选区工具和重复共享侧栏。
4. 扩展每 5 秒发送心跳；PWA 的租约为 15 秒。
5. `GOODBYE`、端口断开、扩展崩溃或租约过期时，PWA 立即恢复原生完整 UI。

接管前后必须保持同一个 PWA `DocumentHost` 和渲染器实例。扩展只调用白名单动作，不读取或
解释 PDF/EPUB/HTML 私有 anchor。

### 能力状态

每项能力只能是：

- `supported`：存在真实闭环并通过行为测试；
- `pending`：旧能力仍在，但统一接口尚未接通；
- `unsupported`：该格式确实不适用，并已经用户确认。

`pending` 与未声明能力都必须明确失败，不能用空函数返回成功。功能实现有差别或矛盾时登记在
`reader-runtime-conflicts.md` / `reader-ui-conflicts.md`，在用户决定前保留两边行为。

## 4. UI 唯一来源

目标是同一视觉元素只维护一份源码：

- 视觉令牌和基础组件：`_server_deploy/static/pdf/rc-ui.js`
- 共享 RC 组件：`_server_deploy/static/pdf/rc-*.js`
- 共享运行时契约：`_server_deploy/static/reader-runtime/*.js`
- 扩展宿主、PWA RPC adapter 和 Shell：`extensions/bw-reader-webext/src/*.js`
- PWA 书籍宿主：`_server_deploy/static/reader-runtime/book-host.js`
- PWA 接管桥：`_server_deploy/static/pdf/pwa-extension-bridge.js`

扩展的 `vendor/` 是生成物，禁止手改。共享源码变化后必须运行：

```bash
python3 extensions/bw-reader-webext/build.py
```

无扩展 PWA 与扩展 Shell 可以有不同挂载器，但按钮、卡片、选区工具、侧栏、AI 轮次和设置面板
应消费同一组件/令牌与动作契约；宿主只提供 capability 和 surface。

## 5. 数据归属

### 扩展优先的通用数据

有扩展时，以下内容优先保存在扩展本地数据库：

- 全局用户设置；
- 模型凭据与网络配置（凭据只能在后台私有区）；
- 对话、词汇、卡片及其学习状态；
- 收藏和跨书引用；
- 查询、翻译、词典缓存；
- 普通网页高亮、便签、卡片 placement；
- 通用标注元数据和同步 journal。

服务器只中转带稳定 ID、revision、mutation ID、tombstone 和父业务状态证明的变化，
不自动裁决冲突。当前进入跨设备 `sync-v3` 白名单的是 `card-entities`、`card-states`、
`user-settings` 与 `vocabulary-state`；翻译/查询等派生缓存只保留在本机，不能因为
DataRegistry 中存在 collection 就自动上传。无扩展真书 PWA 使用自己的本地 fallback；
逐 collection 接线前，旧数据源继续保留。

Reader 卡库以本机 `card-entities` + `card-states` 为权威：一个 `card_*` gid 表示整批
cards，批内 index 永不因删除而重编号；草稿、编辑、确认、复习评分都先原子写本地，成功后
才能显示为已保存或已评分。Pi 只同步这两个 collection 并兼容导入旧 registry；Pi/ReaderPC
AnkiConnect 与 AnkiMobile 都是可选投影，外部 note/card ID 只能写入对应 index 的 receipt，
不得成为 Reader 卡片身份或阻断本机复习。

### iOS App 的可选 Obsidian 笔记线路

- `/pdf/api/to-note` 在 App 与 Safari 扩展中始终由 App 原生桥接管；用户选择 Vault 并开启后才写入，
  未配置或关闭时明确失败，禁止回落 Pi。此设备级设置不进入 `user-settings` 或 `sync-v3`。
- 开启后，BWReader App 接管其 WKWebView 中精确的 `/pdf/api/to-note` 请求；Safari 扩展
  对同一路由使用严格的 `notes.create` 原生消息。两端共享创建、列表与读取能力。
- 安全作用域 bookmark 只保存在 App 容器并只由 App 解析。Safari 扩展不得取得 bookmark
  或直接访问 Vault；扩展创建的笔记先原子写入 App Group outbox，并立即进入两端共享投影，
  再由持有目录权限的 App 自动落盘。相同 request ID 与相同正文均保持幂等，禁止失败时
  静默回落 Pi 造成双份笔记。
- 本地 Markdown 是可读笔记，不替代书内便签的稳定 ID、锚点、revision、mutation ID、
  tombstone 或 `DocumentNoteRepository`；两类数据不得合并成同一真源。

### iOS 本机书库与 Pi 书库

- App 本机书库是默认权威来源；Pi 是可选的同步、备份和分发端。App 可列目录、手动上传
  PDF/EPUB，并把远程书按摘要校验后原子下载到用户选择的本机目录。两端都不得自动覆盖或删除
  同名文件，分叉必须显示为冲突。
- App 只保存安全作用域 bookmark、相对路径和有界索引；绝对路径与 bookmark 不得进入 WebView、
  Safari 扩展、快照或日志。Pi 书籍用持久 `bookId`；本机索引另有持久文件实例 ID，首次发现时
  可用相对路径区分内容完全相同的副本，随后以索引继承身份，不能把路径当成跨端书籍身份。
- 本地书直接由 App 内置 Reader 打开，绝不以“上传到 Pi”作为阅读前置；Pi 下载后的书也立即
  进入同一本机打开路径。
- App 从同一 Reader 源码打包本地壳，并由原生本地资源接口提供 PDF 与 EPUB
  manifest/section/resource 等价能力；本地首版的书籍自带 CSS 处于明确禁用状态，待作用域隔离
  合同成熟后再恢复。PDFKit 可以取代 PDF.js 的基础渲染与原生选择表面，但不得取代 PDF
  `DocumentHost` 的身份、锚点、状态与共享功能合同；EPUB renderer 不在这次替换范围内，也不得
  把本地壳重新命名为 PWA。

### PDF 预处理执行器与派生附件

- App 的本机 Apple 识别、Pi 识别和 PC 识别是三个显式入口；任何失败都不能自动改派到另一端。
  App 本机书 ID 与附件是本机真源；Pi 只协调用户明确提交到 Pi 的远端任务、备份及跨端同步，不能
  成为本机/PC 识别结果被采用的必经中转。Windows worker 不开放公网入站，也不取得可写 Pi 书库路径。
- Windows 侧统一托盘总控的正式产品名为“ReaderPC 服务器”。它只统一展示和控制语音、Reader
  上下文桥与 PC 预处理等独立子进程，不把故障域合并成一个进程；App 与扩展只查询一个本机状态
  入口。开机启动必须由用户显式开启，空闲时不得加载 OCR 模型或占用 GPU。
- 派生结果身份至少包含书籍内容摘要、引擎、执行器与 `processingProfile`。切换 Pi/PC 或质量档时
  必须使用干净的可变 staging，不能复用另一档的残页；已发布 release 保持不可变并可按 revision
  审计。原 PDF 永不因 OCR 被覆盖。
- PC 默认使用 `quality-first-v3`：要求 CUDA；漫画页继续使用 MangaPageOcr 的分框、分行与方向，
  仅在每个既有行内按实际墨迹对齐字符位置，不能以 Vision 替换漫画布局判断。模型按任务惰性加载
  并在结束后释放，进程保持低优先级；
  空闲轮询不得占用 GPU。质量模型不可用时要显示明确原因，不能静默退回 CPU 或较轻模型。
- PC worker 一次只持有一个短租约；页、公式和完成请求都绑定 worker instance、job、generation、
  lease 与源摘要。租约失效、源文件变化、协议回执不完整或重复进程身份冲突时均 fail closed。
- 公式识别为 `unavailable` 时仍可发布完整文字层，但 `pending` / `failed` 等非终态不得伪装成整书成功；
  文字、分词和公式进度分别报告。

### 属于书籍 DocumentHost 的数据

- PDF/EPUB/HTML/Markdown 文件身份与渲染缓存（文件字节可位于 Pi 或用户授权的本机目录）；
- PDF 文字层、OCR、页几何；
- EPUB 章节结构与 reflow 定位；
- 导入 HTML/Markdown 的文档锚点；
- 书内高亮投影、卡片/便签落点；
- 书内墨迹、插入页、裁边、页码对齐、缩放与阅读位置。

同一实体可以有“通用语义记录”和“文档 placement/anchor 记录”，两者由同一个稳定 ID 连接，
但不能复制成两个互不相干的实体。

### 普通网页绘图的特别规则

普通网页会因视口宽度、字体和响应式布局变化而重排，持久坐标无法可靠对应原内容。因此：

- 本轮网页绘图只保存在当前标签页会话；
- 刷新、关闭标签页或扩展重启后不恢复；
- 正文有效宽度变化时保留已提交笔迹与闭合选区；只取消尚未完成的当前路径，并回滚在途橡皮，
  避免响应式重排本身销毁用户已经画好的内容；
- 不删除历史版本已经保存的 `webInkV1`，只停止把新 stroke 写进去；
- Windows 主动笔采用 32px 笔尖预命中区，在接触前声明 `touch-action:none`；预命中区在
  笔/橡皮抬起或任意手指落下时同步撤销，不允许用延时器恢复，也不得扩大为全页输入层；
- 如果未来要持久化，必须先设计基于内容锚点的 stroke 投影，再由用户确认。

书籍绘图不受此规则影响：App 本机真书由 App-owned document store 保存，网页版真书由 PWA
document store 保存；两端都只通过对应 `DocumentHost` 解释书籍坐标。

### 闭合选区元素

- App PencilKit、PDF、EPUB 与普通网页扩展统一使用 `t: "region"`；它是可持久识别的页面元素，
  不是普通画笔笔画，也不是 PencilKit 临时套索状态。
- 每个选区有稳定 ID、创建时间与只增不减的显示序号；旧数据首次加载时按
  `(createdAtEpochMs, id)` 补齐 `ordinal`，此后随笔画数据持久化。删除旧选区不重排后续编号，
  新选区从当前最大编号继续递增；`ordinal` 只用于人读标识，稳定身份仍是选区 ID。
- 选区数量不设专用上限；单条路径最多保留 512 个采样点，防止异常输入拖垮渲染。
- 快照只携带选区身份与存在性等有界元数据。选区附近图、笔迹附近图和视口合成图由 AI 工具按需请求，
  不持续塞入文本快照；工具必须绑定当前 source 与快照 revision，不能接受模型指定任意本地路径。

### AI 阅读上下文、视觉与页面控制

- 网页正文分为文档语料和阅读窗口两层：文档语料记录规范化全文及内容 revision；阅读窗口分别记录
  当前可见正文及其前后邻近文本。当前可见正文必须是独立字段，不能靠模型从整页文本猜测。
- 同一 Codex 线程第一次读取某个文档 revision 时可取得一次全文；成功写回工具结果后才登记已读。
  后续读取只返回最新阅读窗口与“全文已交付”状态。缺少线程身份时只在当前 MCP 进程交付一次，
  新进程允许安全重复，不能因去重失败让新对话漏掉全文。
- 合成图不进入常规文本快照。AI 只能通过只读视觉工具请求当前 snapshot 指向的 source/revision，
  范围固定为视口、笔迹附近或指定闭合选区附近；来源、页面或 revision 在抓取期间变化即丢弃。
- App 与 Safari 扩展的合成图在当前设备生成，并在交给原生图像 API 前写入 App Group 中有界的
  本地缓存；本机 Realtime 由原生 API 直接注入当前会话，Pi 不承担该路径的渲染、保存或搬运。
  网页 `p` 与 App-owned `pts` 两种笔迹点字段在视觉消费边界统一解释，不能因格式差异静默丢图。
- 浏览控制只允许固定动作：前后滚动一屏、滚到可见文字、标题或已知闭合选区。请求必须绑定当前
  source/revision，禁止任意 URL、脚本与调用方 CSS selector；操作后必须复核快照身份。

## 6. 稳定身份与卡片规则

- 实体在第一次渲染前生成唯一 ID。
- 学习卡保持 `id === cid === gid`；旧有效 ID 不重编号。
- 侧栏、收藏夹、页面钉住和 AI 工具结果只是同一实体的引用或 placement。
- 从侧栏拖到页面会创建 placement，不复制实体。
- 删除页面 placement 不得删除侧栏对话、收藏实体或源卡。
- 同一 ID 的内容发生真实分叉时记录冲突，不能只按时间戳静默覆盖。
- 图片/资产引用按唯一编号解析；页面不得直接猜测私有服务端路径或泄露 Bearer token。

## 7. 账户、凭据与同步

- PWA 账户由服务端 session 验证。
- 扩展记住最后一次经 PWA 验证的账户 namespace，因此普通网页功能不依赖持续打开 PWA。
- namespace 只做租户隔离，不是授权凭据。
- API token 保存在扩展后台私有 IndexedDB；内容脚本和页面脚本不能读取明文。
- 跨站设置由扩展本地 store 统一；进入 PWA 时只补齐缺失值，不用 PWA 旧值覆盖扩展权威。
- `DataRegistry` 仍是可同步 collection 的唯一白名单；未迁移 collection 继续使用旧读取源。
- registry 从仅设置/词汇升级到含卡片的代际，只允许 relay 在无活跃 owner 时执行精确的
  旧摘要→新摘要原子迁移。App 在领取新代 owner 前先分页校验并只导入本机缺失的 Pi 旧卡；
  本机已存在或已 tombstone 的 gid 绝不被旧 Pi 数据覆盖。checkpoint 绑定 relay 返回的
  不透明账户摘要，换账户或旧 schema 时从空游标重新核对，但不删除 App 本地仓。
- 当前线框固定为 `sync-v3` + `record-parent-state/1` + `sync-gateway/2`。客户端、PWA、
  直连 peer 与 HTTP relay 任一版本或 registry digest 不一致时，必须在读写前 fail closed。
- 每次同步写入携带它实际看到的父业务状态，而不是只带设备本地 revision。远端只有在父状态
  与当前 head 精确一致时才接受真实变化；更大的 revision 不能成为 winner。同业务值可合并
  传输元数据，真实分叉则返回显式 conflict 并暂停自动同步。接受后的 revision 只允许单调增加。
- 从 `sync-v2` 升级时只保留精确匹配旧摘要的服务端 checkpoint，清空直连 peer。旧 relay
  journal 会触发一次稳定快照；无父证明的旧 head 只可填入本地不存在的 key，或与本地同业务值
  收敛，不能覆盖已有分叉。
- 精确旧摘要下若本地待上传 journal 仍有 v2 proofless 记录，升级必须先执行
  `sync-v2-causal-migration/1`：检查 journal 无裁剪，再用 `snapshotCursor` 精确等于旧
  `remoteCursor` 的冻结服务端快照作为每键父链起点，在单一本地事务中只补 `causal`，同时更新
  journal、对应当前 head 与仍保留的 mutation receipt。迁移完成并保存新 checkpoint 前不得
  push；基线前进、revision 无法证明相邻、既有 proof 不一致或日志有缺口时必须零写入并
  fail closed。未知摘要和普通 sync-v3 proofless 记录不享受该迁移。
- `SyncGateway` 只传变化，不保存 UI、页面几何或文档内容，也不执行任意 URL/method 请求。
- `SyncRuntime`/`SyncCoordinator` 是 PWA 与扩展共用的唯一同步状态机。所有 `sync:true`
  collection 先写本地 journal，再通过固定的认证服务端 relay 持久备份；服务端只按账户分区
  中转 revision、mutation ID 与 tombstone，不静默裁决冲突。
- 多设备都完成同一服务端 head 基线后，可以通过 `direct-signal/1` 建立 WebRTC DataChannel
  加速双向变化交换；直连成功后仍必须继续服务端备份。服务端 head 改变、账户 lease 变化、
  registry 不一致、冲突或信道异常时关闭旧直连并保留可靠服务端通道。
- 扩展模式由后台持有账户 namespace、token、owner lease、Vault 和 SyncCoordinator；扩展
  content host 只持有不透明 deviceId/registryDigest、内存中的不透明 `accountProof` 和 RTC，
  不持有 namespace、Bearer token 或 owner token。无扩展 PWA 不经过扩展后台：它的可信同源
  运行时为领取/续租 PWA owner lease 必须在内存中持有 namespace 与 owner lease，但
  `direct-sync-host`、RTC 帧及 peer 仍只接触不透明 `accountProof`，不会取得或向外泄露
  namespace、Bearer token、owner token。`accountProof` 只用于 RTC 两端账户相等性围栏，
  不能换取任何服务端权限，也不作为业务身份持久化。
- `accountProof` 的线框保持 `account-proof-v1-<64hex>`；服务端以已认证账户 namespace 和
  当前已验证的 `registryDigest` 共同做 HMAC 输入，其中 registry digest 是协议代际盐。因此
  同账户、同 registry 代际的真实设备得到相同证明；不同账户或不同 registry 代际证明不同。
  单个内容宿主生命周期内 registry 固定，证明不得中途变化；变化时直连 fail closed，可靠
  服务端同步继续可用。通用 registry 迁移和运行期双代轮换仍须另立协议，不能在此静默降级。
  PWA 无扩展模式由 Web Lock 选出唯一标签页 RTC owner。
  扩展 marker 出现后，当前 document 的 PWA 直连与耐久同步所有权即保留给扩展。扩展断开、
  `GOODBYE` 或租约过期会立即恢复 PWA 的完整 UI，但同一已标记 document 不会重新启动 PWA
  同步/直连；必须由新的、没有扩展 marker 的文档生命周期重新取得所有权。未裁决冲突时保持暂停。
- 服务端 `owner-lease/1` 以“账户 namespace + 持久 `deviceFamilyId`”为作用域，只排除同一设备
  family 内的 PWA/扩展双 owner，不会把不同真实设备错误地互斥。无 marker 的 PWA 领取 `pwa`
  角色；已完成账户配对的扩展领取 `extension` 角色，PWA 请求接管时具有明确 handoff 优先级。
  普通网页可继续使用扩展后台 owner，不要求同一 PWA document 保持打开。
- exchange、snapshot 和 direct signal 都必须携带当前 generation/token 与精确
  device/family/role/instance 身份；服务端在接触业务状态前验证，客户端在异步操作前后再次围栏。
  owner token 只保存在后台/运行时内存，不进入页面或公开状态。
- 客户端租约最多从请求发出时保留 29 秒，并同时受墙钟与单调时钟约束；服务端时钟偏移、响应延迟、
  系统睡眠或本地时钟回退都不能延长写权限。失租时先暂停可靠通道和直连，再丢弃迟到成功结果。
- 同步 checkpoint 绑定实际 IndexedDB Vault instance epoch；数据库重建后不能沿用旧游标跳过历史。
  PWA `pagehide` 会先暂停网络 owner，并以 `keepalive:true` 尽力释放服务端租约；进入 BFCache
  时使用可恢复 stop，`pageshow` 最多等待旧 release 2 秒，之后只发起受 generation/token
  围栏的新 claim，claim 成功前持久同步与直连保持暂停并沿用既有安全重试。等待期间再次
  `pagehide` 或出现扩展 marker 会使旧恢复 continuation 失效；迟到的旧 release 不能释放新
  generation。普通关闭则永久销毁本页 owner。
- 当前不内置公共 STUN/TURN，避免把阅读元数据静默交给第三方。直连优先覆盖宿主候选可达的
  局域网/私网场景；不可达时自动保留服务端同步，不得把“已启用”误报成“必然直连成功”。

### 自动 PageBrief 与 KG 建点

- 真书 PDF 读页时可后台生成 PageBrief；只有带逐字 quote、且概念名确实出现在 quote 中的
  `knowledge` 结果才进入唯一 `ConceptNodeService`。
- 来源 mutation、node ID 和 evidence ID 均由规范化语义与来源稳定派生；KG 写入失败时保留
  pending brief，下一次只重放同一 mutation，不再次调用 AI。全空生成结果视为临时失败，
  不得写成“本页无内容”；显式 `page_type=skip` 才是可持久的无建点结论。
- 图与 mutation ledger 的更新必须持有跨进程文件锁；这条规则同时适用于 Pi 和 Windows。
- PageBrief/KG 当前仍是阅读器服务器侧的书籍数据与受控 AI 计算，不属于 `sync-v3`，也不会
  自动进入扩展数据库。
- PDF 重命名以 `pdf-page-brief-rename/1` 持久 intent 串联 PDF 路径、PageBrief sidecar、
  书级开关与 ConceptNodeService journal。`nodeId/evidenceId/sourceId/signal` 是不可变审计
  身份；只迁移 `documentRef` 与可证明的 `node.books` 路径投影。pending/synced/显式 skip、
  `_none_pages` 与显式关闭状态原样搬迁，不重新调用 AI。目标冲突、损坏 sidecar、在途生成
  或无法判定的 KG 结果均 fail closed；graph 已 replace 但 commit 尚未追加时先由节点服务
  recovery 判定，再继续完成，不能盲目把 PDF 改回旧名。事务意图保存来源 PageBrief 的原始
  字节，回滚先恢复并校验唯一副本再切回 PDF；提交后的重试只验证 PDF 身份及当前 PageBrief/
  书级设置的路径结构，不冻结后续正常生成内容或用户开关。旧 sidecar 迁移使用无覆盖搬迁，
  目标冲突时保留双方并 fail closed，完成标记持久化后不会因重试再次搬迁。
- 有界 hot receipt / display provenance 之外，`kg-node-history/1` 以一次性、可验证的
  v1 baseline 加后续串行 prepare/terminal 链保存冷 receipt 与 occurrence ledger。相同
  mutation 在 hot 淘汰后仍返回原 transaction 结果，不重新执行；同 mutationId 不同 payload
  fail closed；provenance 淘汰不再使旧 evidence 重计 signal。
- history baseline、prepare、commit、receipt、图 head 和当前图摘要逐层交叉校验；transactionId
  全历史唯一，JSONL 只以物理 ASCII LF 分隔，只有最终 torn tail 可在锁内修复。恢复必须先验证
  已完成历史，再写 recovery terminal；已存在 conflict 时不能误报恢复成功。
- PageBrief 路径投影记录逐 occurrence 的 `from/to` move，因此冷证据的链式改名、目标碰撞和
  history-only rollback 都可精确证明；回滚不会把目标文档原有 occurrence 一并搬回。v1
  `beforeNodes` 没有摘要证明，只保留其既有提交/回滚事实，禁止 baseline 后据此发起新回滚。
- edge audit 的人读日志通过持久 outbox 与 mutation 状态对账，只有已应用 mutation 才幂等
  追加；并发 flush、进程在图提交后退出或日志/outbox 损坏均不会静默丢失或重复审计记录。

## 8. 旧服务端 sidecar 的一次性认领

开发阶段目前只有用户本人。现存未分区 reader sidecar 属于当前已认证主账户，但代码必须绑定
认证得到的数字 uid + `storage_namespace`，不得硬编码用户名、固定 uid 或使用
“first login wins”。

认领规则保持：

1. 认领前确认现存账户唯一并盘点 checksum。
2. 原子复制到账户分区，复核后最后写不可变 manifest。
3. 旧源不移动、不删除、不覆盖，永久作为只读回滚依据。
4. 重试幂等；身份、账户数、manifest 或 checksum 不一致时 fail closed。
5. 未来账户从空分区开始。
6. `id/cid/gid`、entity/asset ID、引用和 membership 原样保留。

首批 `reading-pos`、phrases、notes、PDF/EPUB/HTML highlights、entity/assets 已完成分区代码与
隔离/恢复测试；vocab/Anki、conversation、attention、ink、favorites、userpages 等仍是
deferred-owned，未迁移不代表可以删除。

## 9. 当前完成与待收口

已完成：

- 旧 PWA 网页/RBI 产品入口退役并备份；
- 扩展恢复任意网页完整注入；
- 四类真书的 `book-host/1` 与两阶段接管；
- 扩展断线/崩溃后 PWA 原生 UI 恢复；
- 网页绘图改为 session-only；
- 扩展持久账户、私有 token、跨站设置和受限存储网关；
- `sync:true` 白名单数据的本地优先 journal、认证服务端 relay、快照恢复和显式冲突暂停；
- PWA/扩展共用的 WebRTC 直连协议、认证信令、单宿主选举、分片/背压/资源上限和服务端兜底；
- 同设备 PWA/扩展的 device-family owner lease、PWA 优先 handoff、双时钟到期与 Vault epoch
  checkpoint 围栏；
- PageBrief 自动 KG 建点的证据门禁、稳定身份、pending 重放和跨进程写锁；
- PDF 重命名时 PageBrief/KG 路径投影、书级开关与 sidecar 的持久事务及崩溃恢复；
- KG 冷 receipt/occurrence 长期重放、严格 history 恢复与 edge-audit outbox；
- 0.2.39 顶栏按书籍 capability 接通导航、进度、缩放、排版、裁边、全屏、书设、收藏与用户页。

仍需逐项迁移，不能误报完成：

- 所有旧 collection 的唯一 DataStore 真源；
- 卡片、便签、墨迹 controller 与各宿主 surface 的彻底分离；
- 冲突裁决 UI，以及两个真实设备在局域网/私网与服务端 fallback 下的端到端验收；
- MV3 后台重启后的 owner token 不落持久明文，因此最坏会等待旧租约 TTL 后重新领取；这是
  fail-closed 的短时可用性边界，不能为消除等待把 token 暴露给页面存储；
- registry digest 跨版本升级仍需显式迁移协议，当前按设计 fail closed；
- PWA fallback 与扩展共享组件的进一步源码去重；
- EPUB/HTML/PDF/Web 各差异功能的 capability 全量登记。

## 10. 验证矩阵

最低回归矩阵：

| 场景 | 必须验证 |
|---|---|
| 普通网页无扩展 | 页面行为完全不受 BW 影响 |
| 普通网页有扩展 | 选区、翻译、高亮、卡片、便签、临时绘图、侧栏、AI、设置 |
| PDF/EPUB/HTML/收藏无扩展 | PWA 完整原生阅读器 |
| PDF/EPUB/HTML/收藏有扩展 | 只出现一套共享 UI，所有 capability 可达，PWA renderer 不变 |
| 扩展退出/崩溃 | 15 秒内或 GOODBYE 后 PWA 原生 UI 恢复 |
| Windows 触控/鼠标 | 页面仍可点击滚动，拖拽不中断，卡片与绘图不慢半拍 |

常用检查：

```bash
python3 extensions/bw-reader-webext/build.py
node --test --test-reporter=spec tests/reader_contract/*.test.mjs
python3 extensions/bw-reader-webext/handoff_check.py
xvfb-run -a python3 extensions/bw-reader-webext/test_smoke.py
xvfb-run -a python3 extensions/bw-reader-webext/test_pwa_takeover.py
xvfb-run -a python3 extensions/bw-reader-webext/test_card_drag.py
xvfb-run -a python3 extensions/bw-reader-webext/test_sidebar_layout.py
python3 -m unittest -v tests.test_pwa_web_reader_retirement
```
