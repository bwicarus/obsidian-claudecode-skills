# PWA 阅读器 / 浏览器扩展统一层交接

当前候选由 `extensions/bw-reader-webext/manifest.json` 唯一确定；本轮登记版本为
`0.2.105`。发布事实仍只写入 `reader-collaboration-status.md`，这里不把候选误写成已部署。

本文是该主线的唯一新会话入口。它记录用户已经确认的产品边界、现状和继续工作的门禁。
不得用旧聊天中的 0.2.37 provider-only 方案覆盖本文。

## 0. 接手顺序

1. 完整读本文。
2. 读 `references/reader-collaboration-status.md`，确认当前写入 owner 和 scope。
3. 读 `references/reader-runtime-architecture.md`。
4. 读 `references/reader-extension-ownership.md`。
5. 读 `references/reader-runtime-conflicts.md` 和 `references/reader-ui-conflicts.md`。
6. 读 `extensions/bw-reader-webext/README.md`。
7. 运行 `python3 extensions/bw-reader-webext/handoff_check.py`。
8. 先修事实/契约不一致，再继续加功能。

工作区长期存在大量用户和历史改动。禁止 reset、clean、checkout 或把无关差异纳入本次提交。

## 1. 用户已经确认的产品模型

| 页面 | 无扩展 | 有扩展 |
|---|---|---|
| 普通网页 | 没有 BW 功能 | 扩展提供完整 BW 网页阅读功能 |
| PWA 真书 | PWA 完整阅读器 | 扩展接管共享 UI/网络/通用数据；PWA 保留 renderer、锚点和书籍私有数据 |

“真书”只有：

- PDF
- EPUB
- 导入的 HTML/Markdown
- 收藏书

必须牢记：

- PWA 不再解析任意网站，也不再提供网页阅读器。
- 安装扩展的浏览器访问任意网页时，都应能使用选区、查词、翻译、解释、句级预翻译、未掌握词
  下划线、沉浸翻译、高亮、卡片、便签、AI、侧栏等网页能力。
- 扩展加载到 PWA 真书页时，PWA 的重复共享 UI 停止，功能移交给扩展；但 PWA renderer 和
  `DocumentHost` 不能更换。
- 无扩展时 PWA 真书的所有功能仍然可用。
- 有冲突或实现差异时，不得擅自放弃任何一边；先登记并让用户选择。

## 2. 当前版本和已经完成的主线

当前扩展版本用 `python3 extensions/bw-reader-webext/handoff_check.py` 现场查询；App/TestFlight
发布事实只看 `references/reader-collaboration-status.md` 的最新登记和对应 GitHub Actions 运行，
不得把本节历史数字当成当前值。产品名精确为 **“BW网页伴读”**。

当前 App/Safari 设计：现有 OpenAI Key 只在 App 安全输入框中输入并写入 Apple Keychain；App 与
Safari 扩展原生进程经签名 access group 共享读取，网页和扩展 JavaScript 仅得到 90 秒 `ek_`。
普通 Realtime、选区、可见正文与合成图注入均不经过 Pi；Pi 只保留显式备份/同步和 AI API 能力。

**0.2.104 / App 1.1.45 日语词典改为 App 按需安装**：安装包、ReaderBundle 和浏览器扩展
不再携带 JMdict 数据。用户只能在 App 的“原生阅读工具”设置中手动下载或删除；下载内容保存在
App 私有 Application Support、排除 iCloud 备份，并且不进入书籍附件、App Group、Pi、扩展或
设置同步。阅读器运行时只经令牌保护的本机接口读取已安装分片；未安装时明确提示前往设置下载，
普通网页扩展仍可使用既有服务端查词但不在扩展包内保存词典。

**0.2.103 / App 1.1.44 日语词组句境与高质量回退**：PDF、EPUB 与普通网页的词组入口
都会把邻近句子明确传给结构化日中词典；首选 Claude CLI 未登录或返回空时自动切 Codex，
两边都不可用时显示“无翻译”，不再采用无句境机器翻译的猜测结果。既有 Realtime、CLI 与
PWA 归属边界均未改变。

**0.2.102 / App 1.1.42 原生语音覆盖层下的快照保活**：原生 App 的 Swift scene 前后台标志
现在是专用快照 WSS 的唯一可见性依据；WKWebView 因原生语音或覆盖层暂时报告 hidden 时不再
主动关闭快照。普通网页/扩展仍按 document 可见性门禁，真正切到 App 后台也仍会释放连接。

**0.2.101 / App 1.1.41 快照与电脑语音恢复候选**：专用快照传输故障保留已确认配置并
有界重连，App 回前台先用只读请求验活；Windows 服务重启后的新快照代次可被常驻 MCP 接受。
App 电脑语音对心跳断线与 Windows 媒体清理竞态持续退避恢复，但未知 START 结果仍 fail closed。
Windows Codex 原生任务只按需读取一个工作流指南，旧 Realtime 与 CLI 路径保持不变。

**0.2.100 / App 1.1.40 Reader 输出与渐进能力说明**：Windows 电脑语音的已完成聊天轮次
经本机私有管道送入最新快照精确指向的 App/扩展，并复用既有对话流；卡片、导航、高亮和工具
状态复用同一 Reader WSS 与现有 UI/action，不开放任意函数执行。`snapshot-mcp` 提供短索引和
按功能拆分的只读资源，以及统一 `BWREADER/1 <kind> <JSON>` 命令。来源、快照版本、书页任一
变化都会拒绝陈旧投递。现有 Realtime 与 CLI 的调用、工具循环和委托方式完全不变；Pi PWA
暂停新增功能且本候选不部署 Pi。

**0.2.99 / App 1.1.39 快照视觉发现与 see_ink 单图输入**：Windows 快照返回
`visualAccess`，明确说明当前 App 页面可通过 `reader_visual_image` 按需取得内联合成图；
`page_image=null` 不再被误读为“没有图片来源”，同时不暴露本机路径或能力令牌。App 的
`see_ink` 只向 Realtime 送入原生合成图，不再查询或重复附带当前页全文、前后页文字及选区；
`see_page` / `see_figure` 原有页面上下文语义保持不变。

**0.2.98 / App 1.1.38 语音就绪与按需视觉来源修复**：Windows 启动 Codex 语音时只发送一次
F24，先观察语音浮标代次再等待其稳定可用；首次失败只允许精确重启一次并在新进程就绪后重试，
不会连续按键。实时快照在同一规范页上合并在线 `sourceInstanceId`，使按需 `see_ink` 能绑定
当前 App 页面；`see_page` 对本机 PDF 改走 PDFKit 离屏整页合成，不再被强制降为当前视口图。

**0.2.96 / App 1.1.36 可选视觉能力诊断修复**：PDF/EPUB 未实现可选的
`getVisualSurface` 时，实时快照的高频选区查询只在能力状态首次出现或真正变化时报告一次，
不再每次轮询重复写“原生取图面 放弃”；既有 App 原生 `see_page` / `see_ink` 取图路径不变。

**0.2.97 / App 1.1.37 Codex 语音失效代次恢复**：Windows 首次确认 F24 启动失败时，
只重启刚刚核验过的 Codex 进程代次，等待新代次就绪后再启动一次；第二次仍失败即停止，
不会由持续运行监视器形成无限重启。设置页允许这一有界恢复完成，并在桥暂时离线时保留
已保存的持续运行勾选状态。

**0.2.95 / App 1.1.35 本机阅读上下文与电脑语音候选**：App 本机 PDF/EPUB 以 1.5 秒有界轮询
写入“显示区域之前 / 当前显示区域（重点）/ 显示区域之后”的同一 `page.context` journal；App 与
扩展设置可切换 Windows 的 Codex 语音持续运行。调试日志仍在阅读内容之上，但统一低于设置遮罩，
不能遮住或截获设置控件的触摸；三项均由合同测试锁定。

**0.2.90 / App 1.1.30 原生合成图直投**：App 原生生成的视口、笔迹区域与离屏 PDF
合成图不再经 HTTP 返回 JPEG、转 base64、穿过 WKWebView 消息桥后再送回 Swift；本机能力
路由只接收通话标识与进程内旁路 capability，原生完成合成、本地有界保存和 Realtime 注入后
仅向网页返回小型收据。Safari/PWA 与非直投调用仍保留原二进制 GET 和网页合成回退。

**0.2.91 / App 1.1.31 Realtime 图像确认**：原生 sideband 为每次图像创建独立
`event_id` 与 `item.id`，并按图片 ID 接受当前 GA 的 `conversation.item.added` / `done`
回执；旧 `created` 仅作兼容。错误按本次 client event 关联，无关会话事件不能误报成功或失败。

**0.2.92 / App 1.1.32 Realtime 图像 ID 边界**：原生直投生成的 `event_id` 与
`item.id` 都限制为 32 字符，避免 OpenAI 在接收合成图前以 `string_above_max_length`
拒绝客户端条目；ID 仍保留类型前缀和足够的随机位用于精确匹配回执。

**0.2.93 / App 1.1.33 Realtime 单一视觉目标**：本次通话内按页以内容指纹和递增版本记录模型
尚未看过的新笔迹；只检查当前显示页，别页的待看版本切回后仍保留，首次同步的旧批注只建基线。
当前页新笔迹轮直接锁定 `see_ink`，其合成图已经包含邻近页面，不再先读选区/正文或随后查看
整页；工具结果和唯一最终回答都送达后才消费该页版本。无新笔迹时按选区/当前文字、
`read_page`、最后 `see_page` 的顺序退回。用户中途插话会取消旧回答；若旧轮原生图已经直投，
App 回传的 OpenAI item ID 会用于精确删除。图已入会话但 HTTP 回执尚未返回的极短窗口，只能在
回执抵达后删除，因而不会再产生旧轮第二个回答，但图条目可能短暂存在于新轮上下文中。

**0.2.94 / App 1.1.34 候选 Realtime 图文合并**：本机 PDF 的 `read_page` 不再把 adapter
漏掉 `visible_text` 误报成没有文字层；它直接读取已渲染字符盒或原生/PC 预处理 page-text
provider，并把前页、当前页、后页、选区和书名作为同一页上下文返回。`see_ink`、`see_page`
与 `see_figure` 在取图前冻结相同上下文，图像直投成功后随同一个 function output 交给模型，
最终回答必须综合文字语义与图像空间证据。同一用户轮即使已并发排出多个视觉 function call，
也只允许一次取图和一次最终回答；新通话会清掉旧上下文指纹，不能因同页重拨跳过首轮注入。

**0.2.89 / App 1.1.29 原生合成图**：App 内 `see_page` 优先截取 WKWebView、PencilKit
与可见卡片的公共原生视图层级；`see_ink` 在笔迹仍位于视口时按区域截取同一层级，滚出视口的
PDF 笔迹则由 PDFKit 离屏渲染目标页并叠加本机权威墨迹。三条路径均走能力前缀本地 API，
空笔迹是有图的有效结果；失败必须保留具体阶段和 `X-BW-Reader-Error`，不得退化为“无图”。

**0.2.88 / App 1.1.28 看图逐步日志与等待上界**：`see_ink`、`see_page` 与 `see_figure`
从工具入口、三种页面合成途径、图像大小到原生通道结果逐步写入既有可见调试浮窗；裁图/视口、
整页合成、原生往返分别有明确等待上界。原生返回 `ok=false` 时不得提前记为“已接受”，桥状态
也必须以可调用的 `request` 方法为准，避免诊断日志与真实判定各说各话。

**0.2.87 / App 1.1.27 看图路由与失败可见性**：视觉工具在本机直连未建立时明确拒绝绕到
看不见设备笔迹的 Pi 路径，并报告本机/服务器路由、失败阶段及三个不含凭据的原生桥状态。
语音工具失败也必须把完整 `rag/result` 写进轮次流程；不得只保存“失败”标签而让展开卡显示
“没有额外内容”，否则设备读数无法证明实际走过哪条合成或传输路径。

**0.2.86 / App 1.1.26 看图阶段诊断**：在 0.2.85 的 `p`/`pts` 合成图修复上继续区分
页面合成、call 身份、sideband、本地保存与传输阶段；设备端提示必须保留实际失败原因，
不能再把凭证、类型、体积或存储错误统一说成“合成图生成失败”。

**0.2.85 / App 1.1.25 合成图修复**：网页旧笔迹 `p` 与 App 原生笔迹 `pts` 统一进入同一
设备端合成器；App 在调用本机 Realtime 图像接口前先把实际合成图写入 App Group 有界缓存，
再直接注入当前 OpenAI 会话。Pi 不参与 App 合成图的生成、保存或传输，仅保留旧链兼容。

**当前本机 Realtime 设计**：App 内普通电话由原生层验证 Key、提交 SDP 并读取 call ID；
启动失败会按麦克风权限、凭证、OpenAI 建连与应答阶段直接显示。普通电话仍优先于旧 `agent`
模式进入本机 Realtime，移除
Pi 凭证、上下文、历史、用量、任意工具和挂断回退；页面、选区、笔迹、合成图与笔记只走 App。
仅 `make_anki`、联网搜索、深度思考、后台 CLI、造纸和长文路由作为显式 AI/API 工具按需访问
Pi，失败只影响该工具。显式视觉工具在设备端合成，兼容网页 `p` 与 App `pts` 笔迹格式，先写
App Group 有界缓存再由原生 API 注入当前会话；本机书库默认只展示本地索引，视频浮窗设置与 Safari 笔记也由 App 持有；
普通阅读与本机修改的 manifest 合同逐项禁止 `owner=pi`。

**0.2.80 / App 1.1.18 已发布**：直连 Realtime 会话复用设备建连时取得的短期 `ek_`
credential 建立同一 call 的 sideband 与挂断请求；选区仍在有效语音轮次后注入，显式
`see_ink` / `see_page` / `see_figure` 始终回传实际合成图，不受机会式图片开关影响。

**0.2.79 已部署**：App 本机书与浏览器扩展的 `openai_rtc` 普通电话由服务端签发 90 秒短期
`ek_` credential，设备随后直接连接 OpenAI Realtime calls；长期 key、预算与完整会话配置仍由
Pi 持有。Pi `/voice-rt?mode=rtc` 只保留同一 call 的上下文/工具 sideband，页码、可见正文、
选区、笔迹、合成图、工具和重连补投合同不变。Windows 不可变测试 ZIP 的 SHA-256 为
`42bfd1ce874cccea8a96f2c0073db650be0aa880328af2ac6f5d9f2c1e49de0d`；发布与回滚证据见当前协作状态。

**0.2.76 起保留的产品边界**：原麦克风位置是独立电脑图标，普通电话按钮只负责豆包、GPT 或
Grok；电脑桥状态、上下文同步与 `legacy-inject` / `snapshot-mcp` 回退开关集中在“电脑客户端”
设置标签。普通电话 Realtime 与电脑/Codex 语音是两条独立功能，不能用其中一条的状态推断另一条。

历史上 0.2.72 把“电脑客户端”通话收口为免配对 direct v2：书籍 PWA 只从精确
生产 Origin 连接固定 Windows WSS；普通网页只能经 isolated content runtime 与扩展
background 的固定 relay 连接同一地址。Pi 不再中继配对、状态、启动、心跳、信令或音频；
Windows 只捕获明确选择的活动麦克风与 Codex 目标进程树输出。Reader/扩展不再生成或输入
配对码、设备私钥或 endpoint；选择模型、打开设置和刷新状态都不会启动本机动作，只有闭包
登记的真实电话按钮点击才签发五秒、一次性 START lease。AudioContext blocked 与合法 PCM
突发均有界丢帧/重排，不再自动关 WSS。Windows 0.1.8 已安装；Reader/PWA 0.2.72 已经由正式
部署事务发布，真实双向可听 E2E 仍需在活动 Windows 音频会话中人工验证。
0.2.67 的发布安全门禁继续保留：Windows launcher 在专用 profile 更新后通过 loopback
DevTools 执行 `chrome.runtime.reload()`、核对实际 worker 构建版本，阻止新版内容脚本与旧
后台混合运行。launcher 为 v12；功能逻辑沿用已验收的 v10，v12 用于
承认跨机换行契约使 Windows `.cmd` 真源与已部署 v11 ZIP 的字节发生变化，避免覆盖
不可变的 v11 资产。
历史 0.2.72 Windows 测试 ZIP SHA-256 为
`b365fb2d8ba9d64dc622fd0dca66f4e67e999c168eaec715dba30cbd627b2960`；Safari/iOS ZIP
SHA-256 为 `71b0242c35d7de4ac0ecfea7bbc971c7551285a1b966f43f3040998273ed80d0`。
Reader/PWA 实际发布、Windows 安装、回滚与健康证据见当前协作状态。
上一版 0.2.60 已完成页面 placement 尺寸修复的人工验收；其不可变 Windows ZIP 含 74 个
文件、1,198,019 bytes，SHA-256：
`fb988e4aa0e25c1096568ad167416abab2b3658c880373e179862a0f7db457a7`。
0.2.59 的不可变 Windows ZIP 含 74 个文件、1,196,505 bytes，SHA-256：
`a1247bcd42ee9acc4580a35244d2e8ad681ce8aaaed096fcfcca2f8e08adf1f6`。
0.2.58 的不可变 Windows ZIP 含 74 个文件、1,191,389 bytes，SHA-256：
`ff5bd67991bf622fd364c4a32fca7bde26edcc6f30900839d9b7f49583b24581`。
0.2.57 的不可变 Windows ZIP 含 74 个文件，
SHA-256：
`45c9362ec8ffb9760f8a5a0eb376cb9e747c5f50df9d6a5afb92c1824cb90c9a`。
0.2.56 曾在统一卡片收口过程中生成本地包，但随后又补上页面卡 pending 崩溃恢复和制卡
at-most-once 围栏，因此已经在发布前废弃，绝不能覆盖或激活其不可变资产。
0.2.55 已完成同版本 PWA/服务端部署和 Windows 固定独立 Chrome 验收，并已原子发布到
Windows 测试渠道。其不可变 ZIP SHA-256：
`ce70f64f3d4bdc782680baa6a70ef0d778b9e565cde2ddf0e7e855ddf23eabb6`。

已经完成：

- 0.2.66 把页面卡尺寸编辑从三次短点改为鼠标双击/触屏双点；两者继续复用长按正文命中面。
  触屏序列不再被抬手后的 `pointerleave` 清空，PointerEvent 与浏览器兼容 click 只计一次，
  同时以 `touch-action:manipulation` 保留滚动并避免浏览器双点缩放抢手势；
- 0.2.65 将页面 Anki 的高度链从无法形成确定交叉轴高度的百分比 flex 改为
  `minmax(0,1fr)` 网格行：横向 track、slide 和真实卡面严格受外壳正文高度约束，长正反面
  只在 `.fc-review-scroll` 内滚动，常驻“显示答案/四档掌握度”底栏不再落到裁剪区外；
- 0.2.61 将原始 Anki HTML 与复习显示投影分开：按卡型选择答案追加或整面替换，隐藏经严格
  证明的模板 provenance/旧来源/音频占位并保留原实体，补充内容默认折叠；四档评分和
  “改进”区域不再跟随长卡正文滚动。旧书籍来源只在唯一、安全、无冲突时提升为 `book:`
  引用并经 adapter 打开，未来词汇卡的 `file/page` 查询参数由生成器完整编码；
- 0.2.61 让页面卡三击缩放待命在卡外普通点击后立即退出，不拦截宿主网页事件；
- 0.2.62 将所有学习态 Anki 投影统一为“卡面正文独立滚动、翻面后四档掌握度固定底栏”，
  不再只有复习队列卡固定评分；复习模式的“改进”折叠入口和工具区也移入卡片区域底部操作坞，
  长卡面与长工具结果互不挤掉关键操作。
- 0.2.64 在 0.2.63 页面 placement 高度传递修复上继续收敛 Anki 操作层：学习卡底栏在
  正面态也始终属于卡片本体并显示“显示答案”，翻面后同一底栏原位替换为四档掌握度，不再
  依赖正文滚动区中的临时提示；侧栏、收藏、页面与复习队列仍复用同一 `rc-flashcard`。
- 0.2.63 把页面 placement 的真实正文高度传递从“仅三击调整过尺寸的卡”扩展到所有页面
  Anki 卡，修复默认 260px 外壳内的 300px 卡面把评分底栏裁到可视区外的问题。
- 0.2.60 把尺寸明确收窄为页面 placement 的设备级呈现：稳定 `cid === gid` 只让同一卡的
  页面副本共享大小，侧栏、收藏夹和复习区不读取也不应用；扩展通过后台逐 `cid` 原子合并到
  `pageCardPresentationV1`，不依赖已验证 PWA 账户，无扩展 PWA 落自己的 device store；
- 0.2.60 的三次短点与长按选中严格复用同一正文 `pressTarget` 和同一控件排除规则；
  Anki 正反面正文可触发，卡头、评分键、链接、输入控件和分页圆点不可触发；
- 0.2.60 将页面 Anki 的自定义高度从共享卡壳一直传递到真实正反面卡面，解除旧 300px
  上限；同时隐藏 Anki 卡内层、横向 pager 和共享卡壳的滚动槽，但保留滚轮、触摸、键盘和
  惯性滚动。0.2.59 的“尺寸广播到所有投影/按账户保存”行为已在人工验收中判定错误并废弃；
  Windows 固定环境验收必须直接装本地不可变候选，不能运行仍指向 0.2.55 的在线 launcher；
- manifest 恢复任意 HTTP/HTTPS 网页的完整扩展功能链。
- PWA marker 只匹配四个真书路由：
  `/pdf/view`、`/pdf/epub/view`、`/pdf/html/view`、`/pdf/fav/open`。
- 删除产品模型中的 `/pdf/web/live`、`?ui=legacy` 和“五入口”概念。
- 普通网页不依赖 PWA 标签页或 provider lease；扩展记住最后一次经 PWA 验证的账户 namespace。
- API token 移入扩展后台私有 IndexedDB，内容脚本不可读取。
- 设置以扩展本地为有扩展场景的权威，跨网站一致；进入 PWA 时只补缺失值。
- `book-host/1` 和 `bw-reader-pwa/1` 两阶段接管已接入 PDF、EPUB、HTML、Favorite。
- `HELLO` 后先初始化 Shell，再 `TAKEOVER`；5 秒心跳，15 秒租约，GOODBYE/崩溃/过期恢复 PWA。
- 扩展顶栏已接书籍导航、当前/总进度拖动、缩放、适合、排版、裁边、全屏、书设、收藏、用户页、
  注音、整页翻译、绘图、便签、搜索、设置和 AI。
- 普通网页只显示真实支持的能力；书籍专属按钮不得成为死按钮。
- 网页绘图改为当前标签页 session-only；历史 `webInkV1` 不删除。
- 0.2.40 恢复 Surface Pen 小盾预命中契约：canvas 始终穿透，140px shield 只在笔尖附近
  使用 `touch-action:none`，笔/手指互不扩大为整页事件接管。
- 0.2.41 将 Windows 启动器升级到 v6：SHA-256 校验改用兼容旧 PowerShell 环境的
  .NET 实现，仍保持下载后校验，不因缺少 `Get-FileHash` 而中断自动更新。
- 0.2.42 修复手写结束后的首轮触摸滚动：网页笔尖 shield 在 `pointerup/cancel` 同步撤销，
  PDF/EPUB 只允许发起笔画的 `pointerId` 阻止默认动作并显式释放捕获；词汇索引冲突改用既有
  ECDICT 原形管线确定唯一归属，`was` 会继承 `be` 的掌握状态；网页词汇下划线改为逐句
  Range 原子替换和 lemma 定向移除，查词不再清空并闪烁整页下划线。
- 0.2.43 将网页文字命中统一为真实字形几何，空白处不再吸附最近单词；生词下划线和句级
  预翻译改为可见区观察、空闲发现和分帧更新，滚动不再同步遍历整页。网页 AI 翻译新增
  无状态、页面短时会话和自动三模式：自动按预计阅读 70% 计算，短内容无状态、超过阈值才
  建立会话；会话按账户和页面隔离，失败显式降级，session/stateless 缓存严格分桶。
- 0.2.44 尝试用落笔后的 Pointer Events 捕获彻底移除普通网页悬停小盾；自动化中的触屏
  行为正常，但 Windows 真笔会在 `pointerdown` 前被浏览器判为 direct manipulation，
  因而暴露“下笔时页面一起滚动”的真机回归。该版保留下来的有效改动是：触屏双击切橡皮
  只认“抬手且未移动”的两次轻点，不再把连续滚动误判成双击；网页翻译会话达到 32 轮或
  约 24k 上下文 token 时，旧会话先生成经白名单重建的非可信翻译摘要，再创建新会话续译，
  摘要失败则以干净上下文继续且不中断当前批。
- 0.2.45 按用户选择改为自动混合：Windows 笔尖悬停时只有其周围 32px 预命中区使用
  `touch-action:none`，确保浏览器在落笔前把这一笔交给绘图；笔/橡皮抬起或任意手指落下
  都同步撤销，且没有恢复定时器。同一点附近的悬停抖动不会重新武装，笔尖明确移动 8px 后
  才恢复下一笔。显示 canvas 仍完全穿透；手指若恰好在尚未落笔但已武装的 32px 内起手，
  当前手势可能被浏览器仲裁掉，这是已选择的自动混合边界，不能扩成全页拦截或自制滚动物理。
- 0.2.46 把普通网页笔迹接回阅读器唯一的 AI 链路：WebAdapter 只输出统一视觉表面和
  canonical strokes，共享 `rc-voicecall` 负责局部截图、笔迹合成、文字/Realtime 同步；
  `/voice-rt` 通过宿主服务地址 hook 指回阅读器服务器，避免外网站点绕过单回复控制器。
- 0.2.47 延续同一份 `rc-voicecall`，只加入可选传输接口：PWA 仍用原生同源 WebSocket；
  普通网页由扩展后台代建严格限定到 `/voice-rt` 的连接，以绕过宿主页 CSP。扩展没有复制
  侧边栏、语音状态机、卡片渲染或截图逻辑。
- 0.2.48 修复模板预置 `#word-pop` 时共享组件没有绑定点击委托的问题；PDF、EPUB、
  HTML 与扩展现在都从同一份 `rc-wordpop` 执行“标记掌握”等动作。上下文选择新增整卡
  覆盖内部段落的稳定集合规则，复习卡“详细/精炼/问 AI”入口使用同一动作协议。
- 0.2.49 将单词掌握、短语掌握和短语收藏接入唯一的 `vocabulary-state/1` 本地仓库：
  点击后先同步更新内存与页面，再异步持久化和兼容同步；断网、迟到回包和旧快照均不得
  回滚本地状态。PDF、EPUB、HTML 与普通网页共用相同状态投影，扩展后台按账户隔离并支持
  跨标签实时通知、后台重启恢复和账户切换失效；网络依赖审计同时锁定为零新增债务。
- 0.2.50 将普通网页便签迁到扩展本地的 `document-notes` 仓库：账户、文档和 provider
  Vault 三层隔离，创建/修改/删除不再访问 `/pdf/api/notes`；刷新、浏览器重启、跨标签
  `CHANGE`、删除 tombstone 和同源 SPA 路由切换均保持一致。便签 UI/手势仍只使用共享
  `rc-stickynote.js`，PWA 真书暂保留旧 HTTP fallback，避免在 AI `notes_create/notes_edit`
  迁移完成前静默丢弃能力。后台以浏览器当前 `tab.url` 派生 SPA 文档身份，并只允许
  `sender.url` 的同源 Document 快照；跨源错配仍 fail closed。
- 0.2.53 将复习收敛为助手 Tab 内的模式开关：相关 Anki 卡片工作区位于现有对话上方，
  下方继续使用完整助手聊天、输入和工具链。普通助手与复习助手按账户加模式分别使用物理
  历史、摘要、归档、媒体引用、任务代次和清空事务；切换、清空、迟到历史/SSE/TTS 回调都
  不能跨模式覆盖或复活记录。该版曾误删标准 Anki 翻面/评分流程；这属于回归，不能作为
  产品取舍，已由 0.2.55 恢复。复习工作区同时接入旧 AI 卡片改进页的
  选段、详细/精炼、更新笔记、生成新 Anki、两者都做、预览和显式确认；旧入口继续保留，
  但草稿生成与最终提交都调用同一个 owner-bound runtime，永不直接覆盖或删除旧卡。
  复习模式保留输入框听写和逐条 TTS；旧 Realtime relay 尚未携带冻结 mode，因而在复习模式
  明确禁用电话和长按连续语音入口，避免把普通历史、复习历史和 compact/clear 串在一轮通话里。
  Codex 型号改为读取实时 app-server catalog；Fast 只对实时声明 priority 的型号开放，并在
  文字、语音、旧 QA 普通截图与卡片改进的实际调用前再次 fail-closed 校验。
- 0.2.55 恢复标准 Anki 状态机：正面 → 显示答案 → 再来/困难/良好/简单 →
  `/pdf/api/review-answer`，网络中断只在存在账户隔离 outbox 时耐久入队，服务端拒绝则恢复
  乐观移出的卡；即使评分后切到另一页面，拒绝结果也会按原页面缓存恢复，返回时不丢卡。
  服务端用 `aid`、跨进程锁和原子 pending/done receipt 保证评分最多执行一次；无法证明
  Anki 结果的崩溃窗口保持 pending 并拒绝重放，宁可等待将来对账也不重复评分。
  卡片区域和“改进”动作区分别可折叠，来源/原因/Local ID/问 AI footer 只在侧栏投影中隐藏，
  原始卡内容不删除；答案与 footer 位于同一容器时也只剥离 footer，不误删答案。旧卡可见
  “来源：”Obsidian 链接会安全提升为结构化 `source_ref/source_url`，普通链接、脚本伪造及
  路径穿越均拒绝；可信用户点击只打开一次来源页，不以 `window.open(..., noopener)` 的空返回值
  误判失败并再次导航。右上界面设置同时管理上方 Tab 和下方快捷按钮，隐藏只改变显示状态，
  不销毁按钮或监听器。
- 0.2.55 同步主 `~/.codex/auth.json` 到阅读器专用 `CODEX_HOME`，缓存按认证摘要隔离；
  打开的多轮 thread 是进程重启围栏，认证更新不能在工具循环间重启并丢上下文。
  `gpt-5.3-codex-spark` 作为普通 CLI 兼容型号可选择；`available/catalog_advertised` 只表示
  实时目录声明，不再被误写成“账号未开放”。目录读取与缓存发布使用同一真实 app-server
  认证代次的不可变快照，认证切换不能把旧目录贴到新代次；Fast 仍仅在该型号明确声明
  priority 时可开启。
- 0.2.57 把制卡结果、工具结果中的学习卡和复习模式当前卡收敛到唯一
  `RC.flashcard.renderEntity()` 组合入口：外层复用 `rc-voicecall` 的视觉、长按选中和
  页面/收藏拖放，卡面复用 `rc-flashcard` 的正面、翻面与评分状态；复习队列仍由
  `rc-review` 独占评分事务和失败恢复，不产生第二次提交。单张 Anki 卡使用稳定
  `anki_card_<card id>`，保持 `cid === gid`，页面 placement 另有自身编号；同一来源生成的
  多张子卡也不会碰撞。拖放快照保留完整 Anki 身份、来源、原因和实体字段，侧栏与页面投影
  只隐藏来源/原因/Local ID/问 AI，不删除底层数据。复习工作区去掉重复卡框并统一为现有
  半透明卡片设计，翻面、四档评分、折叠改进区和下方独立复习聊天保持不变。页面和收藏卡
  长按时读取实时完整快照，拖拽会取消长按仲裁；评分/制卡在响应未知时先持久化 pending，
  重载、旧 entity 快照或随后打开复习区都不能重新暴露提交按钮。服务端制卡以 per-aid
  跨进程锁、payload fingerprint 和原子 pending/done receipt 保证补投不重复 addNote。
- 0.2.58 把固定 Anki/工具卡的长按选择面收窄到展开正文，整卡仍作为稳定 owner 与同编号
  高亮主体；按钮、链接和卡头不再误选。卡头、侧栏拖出和浮卡统一采用 420ms 蓄力、8px
  容差及 pointer-capture 状态机，蓄力前快划取消且吞掉一次合成 click，系统中断只回滚；
  PWA 卡头严格绑定首个 pointer，多指或笔+手指不能抢占或误落卡，结束/取消会释放捕获并清理
  删除区、收藏区和锚点暂态。卡片正文不会再误入旧便签 EDIT；明确失效或脱离 DOM 的
  charged-drag session 可由原 binding 安全回收，真实活跃手势仍保持全局互斥。复习队列复用
  `rc-flashcard` 的横向 scroll-snap 与圆点导航，移除独立上一张/下一张按钮；评分拒绝异步回插
  也经过完整切卡围栏，不继承下一张卡的答案或改进 busy 状态。
- 网页沉浸翻译新增 Google（默认）与 AI 无状态编号批翻；仍以句子为单位，后台预翻译固定
  Google。网页 URL/页级译文只在扩展账户分区 IndexedDB 中保存，Google、AI 和模型缓存严格
  分桶；任意网页正文进入 AI 前必须经过无工具文本边界，不能交给可读取本机文件的 agent。
- 卡片稳定身份不变：学习卡 `id === cid === gid`，placement 与实体分离。
- 私有编号图片由扩展后台携 Bearer 获取后变为 Blob URL；外站 remote-only 资产必须走 BW
  服务端受控代理，不能扩大全网 host permission。
- 受控资产代理只读取当前认证账户 registry 中的 URL，并逐跳检查公网目标、图片 MIME、16 MiB
  上限和 15 秒总预算。
- 共享语音组件发送的 Blob 只允许经固定 `/api/assistant/voice-clip` POST 进入最多 8 MiB 的
  base64 消息桥；其它二进制路由必须在出网前拒绝。
- PWA 旧网页解析/proxy/RBI 产品入口已退役。
- Windows 测试通道发布前必须通过版本递增、完整回归、精确 ZIP/channel/launcher 校验；
  Surface Pen 的 compositor 行为仍需按 `windows/SURFACE-PEN-CHECKLIST.md` 真机验收。

退役前备份：

- `state/retired-web-backups/20260724T155933Z/pwa-web-rbi-state.tar.gz`
- SHA256：
  `051eff441f67de7837d3b5fb3865a0c142dff2270fd9bbe3efdfdf7661b3f3d8`
- 备份包含 inventory、原文件 checksum 和 archive checksum；原文件未删除。

## 3. 共享源码与生成物

共享底座契约仍分别是 `account-context/1`、`document-host/1`、`data-store/1`，同步线框为
`sync-v3` + `record-parent-state/1` + `sync-gateway/2`。它们统一账户代际、宿主语义、
稳定记录与变化中转，但不改变本文件定义的 UI 所有权。尚未迁入唯一真源的能力必须标为
`pending` 并保留旧读取链。

0.2.52 候选把 `DataRegistry` 中已经标记 `sync:true` 的通用数据接入同一
`SyncRuntime`：本地写入先完成，认证服务端 relay 始终承担持久备份；两台设备只有在同一
server head 上完成 live baseline 后才可建立 WebRTC 直连。直连是加速通道而不是新真源，
冲突、账户切换、registry 变化或信令过期都会关闭/暂停直连，不能绕过服务端冲突边界。
扩展后台独占 token、namespace、owner lease、Vault 和 coordinator。内容脚本只拿
deviceId/registryDigest、内存中的不透明 `accountProof` 与 RTC，不拿 namespace、Bearer 或
owner token；`accountProof` 只能比较 RTC peer 是否属于同一账户，不能调用任何服务端权限。
服务端以 registry digest 作为协议代际盐参与账户证明 HMAC：同账户同 registry 代际稳定，
跨账户或跨 registry 代际不同；保持 `account-proof-v1-<64hex>` 不透明线框。运行中证明变化
会让直连 fail closed，服务器可靠同步仍保留；registry 的正式迁移/双代轮换不在本候选中。
PWA 无扩展时用 Web Lock 保证同一安装只有一个直连标签页。当前未配置公共 STUN/TURN，因此
无法直连时按设计继续走服务端。

当前跨设备白名单只有 `user-settings` 与 `vocabulary-state`；派生缓存不上传。每条变化携带
它实际看到的父业务状态，relay 只有在父状态与当前 head 精确一致时才接纳真实变化，更高
revision 不能绕过分叉。同业务值只收敛元数据，真实分叉保留 conflict 并暂停；旧 `sync-v2`
只通过精确摘要 checkpoint + 稳定快照迁移，旧无证明 head 不能覆盖已有本地值。

扩展 marker 会为整个 document 生命周期保留同步/直连所有权。`GOODBYE`、断线和租约过期
仍会恢复完整 PWA UI，但不会在同一个已标记 document 中重新启动 PWA sync owner。
0.2.52 已加入服务端 `owner-lease/1`：租约按账户 namespace + 持久 `deviceFamilyId` 分区，
只互斥同一设备 family 内的 PWA/扩展，不阻止真实多设备并行；PWA handoff 优先于扩展。
exchange、snapshot、signal 在读取业务状态前都验证 device/family/role/instance/generation/token，
客户端又在异步调用前后围栏。租约从请求开始最多保留 29 秒，并用墙钟+单调时钟同时限制，
因此响应延迟、睡眠和时钟跳变不能留下双 owner 窗口。PWA 从 BFCache 恢复时会在旧租约释放后
重新领取；`pagehide` release 使用 `keepalive:true`，`pageshow` 最多等待 2 秒，超时后也只能
重新 claim，服务端仍占用时保持暂停并安全重试，不会直接恢复或形成双写。普通关闭永久销毁
本页 owner。checkpoint 也绑定 IndexedDB Vault instance epoch，数据库重建后从安全基线恢复，
不沿用旧游标。

同一 0.2.52 候选也把 PDF PageBrief 自动接到唯一 `ConceptNodeService`：只有逐字证据通过的
knowledge concept 才建点，稳定 mutation/ID 支持 KG pending 重放；全空 AI 结果会重试，
Windows/Pi 都必须持有跨进程图锁。PageBrief/KG 仍是阅读器服务器侧数据，不进入 sync-v3。
PDF 重命名现通过持久 intent 同步迁移 PageBrief sidecar、书级开关与 KG 的
`documentRef/books` 路径投影；节点/证据/来源 ID 与 signal 不因改名重算，崩溃窗口由同一
mutation receipt 和 KG journal 判定后继续或安全回滚，不重新调用 AI。回滚会先从 intent
逐字节恢复来源 PageBrief，已提交重试允许目标 PageBrief 与开关继续正常演进；旧 sidecar
只做无覆盖搬迁并记录完成，重试不会再用旧数据覆盖新路径。KG 的有界 hot ledger /
display provenance 现已由 `kg-node-history/1` 冷 receipt 与 occurrence ledger 补齐：
淘汰后仍稳定重放原结果并拒绝 mutationId 换 payload，旧证据不重复增加 signal。
baseline、prepare/terminal、receipt、图 head、transactionId 和当前图摘要均 fail closed
交叉校验；恢复先验历史再写 terminal。PageBrief 改名持久化逐 occurrence move，因此纯冷
投影也能精确回滚且不会误搬目标文档原有证据。v1 未摘要的 `beforeNodes` 不允许用于新回滚。
edge audit 人读日志使用持久 outbox 与 mutation 状态对账，提交后崩溃或并发 flush 不会静默
丢失/重复日志。
MV3 后台重启时 owner token 不写入持久/页面可读存储，最坏会 fail closed 等待旧租约 TTL；
registry digest 跨版本迁移和两台真实设备端到端验收仍是后续明确工作。

| 内容 | 唯一源码 | 生成物/部署位置 |
|---|---|---|
| 共享视觉和 RC 组件 | `_server_deploy/static/pdf/rc-*.js` | 扩展 `vendor/rc-*.js`、nginx static |
| 阅读运行时契约 | `_server_deploy/static/reader-runtime/*.js` | 扩展 runtime vendor、nginx static |
| 真书宿主 | `_server_deploy/static/reader-runtime/book-host.js` | PWA 四类书页面 |
| PWA 接管桥 | `_server_deploy/static/pdf/pwa-extension-bridge.js` | PWA 四类书页面 |
| PDF 宿主注册 | `_server_deploy/static/pdf/reader.src/32-extension-host.js` | 合并后的 `reader.js` |
| EPUB/Favorite 宿主 | `_server_deploy/static/pdf/epub-html.js` | nginx static |
| HTML/Markdown 宿主 | `_server_deploy/static/pdf/html-reader.js` | nginx static |
| 扩展 Web/PWA adapter 与 Shell | `extensions/bw-reader-webext/src/*.js` | Windows/Safari ZIP |
| 扩展入口/后台 | `content.js`、`background.js`、`manifest.json` | Windows/Safari ZIP |
| PWA 网页退役路由 | `_server_deploy/html_reader.py`、`app.py` | `/home/bwicarus/webapp/` |
| SW 认证公共实现 | `_server_deploy/reader_sw_auth.py` | `/home/bwicarus/webapp/` |

扩展 `vendor/` 禁止手改。共享源码改完必须运行：

```bash
python3 extensions/bw-reader-webext/build.py
```

PDF `reader.js` 由 `reader.src/*.js` 按既定排序机械生成，不能只改合并产物。

## 4. 用户已拍板的视觉和交互

- **1A**：页面卡片只显示卡片本体，不套第二层标题窗口。
- **2A**：选区工具是紧贴选区的横向浮条。
- **3A**：侧栏统一半透明磨砂；浮动/挤压由宿主布局 adapter 实现。
- **4B**：顶部栏可收起，收起后只留小把手并释放正文高度。
- **5V**：AI 普通文字使用气泡；含工具时整轮进入唯一轮次容器；工具结果有标记、长条、完整卡三态。
- **6A**：拖卡时显示左上删除区和底部收藏区；删除 placement 不删除源对话/侧栏/卡片实体。
- 卡片和便签必须绑定内容元素，滚动依靠内容坐标/DOM 挂载，不用 fixed + scroll 事件追赶。
- 侧栏长按把手后可实时调宽，设最小宽度；文字本身不缩放。
- 对话滚动轨道按一轮问答一条短线，可悬停展开摘要并点击跳转。

普通网页绘图的最终决定：响应式重排会使持久坐标失效，因此只做临时绘图；检测到正文
有效宽度变化时立即清空本次会话的网页笔迹，避免旧坐标指向已经重排的内容；真书绘图仍持久化。

注意编号来自两轮不同决策：上面的 **4B** 是视觉方案（可收起顶部栏）；架构方案
**4A** 是退役 PWA 远程网页/RBI。用户最近回复中的“第 3 项”指普通网页绘图的
session-only 决定，不是推翻上面的视觉 **3A**。这些决定同时成立，后续不得因编号相近互相覆盖。

## 5. PWA 接管不变量

接管前后必须满足：

1. `DocumentHost` 对象身份不变。
2. PDF/EPUB/HTML renderer 不卸载、不重建。
3. PWA 私有 anchor、墨迹、placement 和书籍文件不进入扩展数据库。
4. PWA 原生 UI 只在收到成功 `TAKEOVER` 后隐藏。
5. 扩展 Shell 只根据 host capability 显示按钮。
6. 扩展只发白名单动作；缺能力时明确失败。
7. GOODBYE、端口断开或 15 秒无心跳，PWA 原生完整 UI 恢复。
8. Favorite 的身份是 `app=epub + route=favorite`，不要与普通 EPUB 混淆。

## 6. 网页功能不变量

- 普通网页不要求登录 PWA 标签页持续打开。
- 扩展 Shadow/overlay 不得拦截网页本身的触摸滚动和鼠标点击。
- 卡片 drop 后一次 pointer/touch 结束只能创建一个 placement。
- 页面滚动时卡片、便签、绘图不靠异步 scroll handler 追赶。
- 网页沉浸翻译以句子为单位；按钮显示译文时不得额外打开大翻译面板。
- 未掌握词继续显示下划线；预翻译阈值沿用句长/词数判定。
- 网页临时绘图只存在内存/标签页会话，不覆盖或删除旧持久数据。
- 页面没有书籍 capability 时不得显示页码、裁边、书籍设置等空按钮。

## 7. 数据和 ID 门禁

- 每个实体首次渲染前已有稳定 ID。
- 学习卡保持 `id/cid/gid` 同一身份；placement 使用独立位置 ID。
- 收藏是实体 membership，不创建新卡。
- 编号图片按 asset ID 解析；AI 文本中的 `#img_xxx` 必须在对应位置渲染实际资源。
- PWA 真书 anchor 与扩展通用实体可用同一个 ID 连接，但分别存放。
- 同 ID 内容分叉产生显式 conflict，不能按时间戳自动吞掉一边。
- 未迁移 collection 继续保留旧读取源；目标归属不等于已经可以删除旧代码。

## 8. 旧 sidecar owner

开发期当前只有用户本人。现存未分区服务端 reader sidecar 一次性属于当前认证主账户。
实现按数字 uid + `storage_namespace` 绑定，不能硬编码用户名/uid，不能 first-login-wins。

迁移采用 copy + checksum + immutable manifest-last；旧源不移动、不删除、不覆盖。未来账户为空，
身份或 checksum 有歧义时 fail closed。卡片、entity、asset 和引用 ID 原样保留。

首批 reading-pos、phrases、notes、PDF/EPUB/HTML highlights、entity/assets 已有分区实现和测试；
deferred-owned 域继续保留旧功能，之后逐项迁移。

## 9. 测试

至少运行：

```bash
python3 extensions/bw-reader-webext/build.py
node --test --test-reporter=spec tests/reader_contract/*.test.mjs
python3 extensions/bw-reader-webext/handoff_check.py
python3 -m unittest -v tests.test_pwa_web_reader_retirement
xvfb-run -a python3 extensions/bw-reader-webext/test_smoke.py
xvfb-run -a python3 extensions/bw-reader-webext/test_pwa_takeover.py
xvfb-run -a python3 extensions/bw-reader-webext/test_pwa_handoff.py
xvfb-run -a python3 extensions/bw-reader-webext/test_card_drag.py
xvfb-run -a python3 extensions/bw-reader-webext/test_sidebar_layout.py
xvfb-run -a python3 extensions/bw-reader-webext/test_web_notes_local.py
python3 extensions/bw-reader-webext/test_pwa_native_contract.py
python3 extensions/bw-reader-webext/test_release_pipeline.py
```

其中 `book-extension-handoff.contract.test.mjs` 固定四类真书接管边界，
`reader-service-worker.contract.test.mjs` 固定四入口的私有缓存/身份边界。

旧测试若仍断言 provider-only、五入口、`?ui=legacy` 或普通网页不注入，必须更新测试，而不是把
实现退回旧模型。

## 10. Windows 真机测试渠道

本节只保留为按需渠道。2026-07-31 起，Reader/PWA 共享改动默认直接部署到生产 iPad
由用户验收；只有扩展专属改动或用户明确要求时，才先进入这里的独立测试环境。

- Windows：`100.99.9.124`，用户 `bwicarus`
- Chrome：`C:\Program Files\Google\Chrome\Application\chrome.exe`
- 测试 profile：`%LOCALAPPDATA%\BWReaderExtensionTest\browser-profile-v2`
- 固定 unpacked 目录：`%LOCALAPPDATA%\BWReaderExtensionTest\extension`
- 扩展 ID：`jddhhakcblmihidgdobfkcejjinpigak`
- 计划任务：`BW Codex Chrome Test`
- Chrome 通过 loopback CDP `9222` 启动，再由 SSH/Tailscale tunnel 控制。

Chrome 137 起不再接受命令行 `--load-extension`，所以测试流程是保持同一 unpacked 路径，替换文件，
然后在 `chrome://extensions` 对 BW 扩展点击 reload。不要误判为“打开的浏览器没装扩展”。

0.2.49 已在同一台 Windows 上用独立 Playwright Chromium profile 直接验收：

- 主包 SHA-256 为 `ffc5e7fadaa4e9a50057b51ad471c7ae9440ca0c5626cea090a240a62c414bcd`；
- 普通网页注入和原页面点击正常，词汇本地投影实测约 20.7 ms；
- 网络离线时单词/词组状态仍写入扩展 Vault，刷新及完整浏览器重启后恢复；
- 两个标签页收到实时状态变更；`be` 与 `was/were/been/being` 共用同一记录；
- 词组掌握与收藏是两个独立记录，真实弹框按钮点击已通过。

这轮只验证普通网页扩展链；当时生产 PWA 仍是旧代码，不能把它与 0.2.49 混合后判定接管通过。

0.2.50 又在同一台 Windows 上直接从不可变 ZIP 解压，并用随机临时 Playwright Chromium
profile 验收：

- 主包 SHA-256 为 `ee4b14351f2791bcb4163b8f9502c737f11d6d8105f66c2385931b600a3f2e08`；
- 原网页按钮真实点击和扩展顶栏真实点击均正常；
- 普通网页便签创建/修改/删除全程没有 `/pdf/api/notes` 请求；
- 页面刷新、完整浏览器重启、同文档双标签 `CHANGE`、同源 SPA 文档隔离和删除 tombstone
  全部通过；
- 使用的是 Playwright Chromium `chromium-1217`，没有启动或修改日常 Chrome profile；
  验收后独立 Chrome 进程为 0，临时目录已删除；
- 同一 ZIP 已放到 Windows
  `C:\Users\bwica\Downloads\bw-reader-webext-0.2.50-windows-test.zip`。

0.2.50 的书籍 PWA 接管还不能与线上旧 runtime 混测：线上 `data-registry.js` 尚未包含
`vocabulary-state` provider，扩展因此以 `BW_RUNTIME_PROVIDER_REGISTRY` 正确 fail closed
并保留 PWA fallback。先部署同版本 PWA runtime，再继续四类真书接管验收。

0.2.57 已在固定 `BW Codex Chrome Test` 环境从不可变 ZIP 替换 unpacked 目录并验收：

- Windows 端重新计算的 ZIP SHA-256 与上述摘要一致，service worker/manifest 均为 0.2.57；
- Anki 实体卡先显示正面，翻面后显示背面和四级评分；语义身份保持
  `id === cid === gid`；
- 从侧栏拖到网页后保留 card/note/entity/source 元数据，页面只显示一层共享卡片外壳；
- 长按卡片正文后，侧栏原卡与页面 placement 同步高亮，并导出
  `anki-card-context/1` 完整上下文；
- 收藏结构化 payload 通过合同预检；页面卡随文档原生滚动，普通网页按钮仍可点击，后续点击
  不会重复生成 placement；
- 助手 Tab 同时存在可折叠复习 workspace 与下方普通聊天；
- 测试记录已从扩展本地 store 清理。收尾后计划任务为 `Ready`，专用 Chrome 进程和 9222
  监听均为 0，SSH/CDP 隧道已关闭；未触碰日常 Chrome/profile。

0.2.57 的 PWA/服务端 payload 已部署并完成遗留事务收尾，但扩展 channel 没有从 0.2.55
切换到 0.2.57。0.2.58 仍需按新的人工视觉验收 + 后台证据门禁完成；不能拿 0.2.55 扩展
channel 与 0.2.57 PWA 混测后误报同版本接管。

0.2.58 本地候选的自动证据：

- Node reader contracts：512/512；
- Python 全量：667/667，skip 14；
- 真实 Chromium：
  `test_review_candidates.py`、`test_card_favorite_payload.py`、
  `test_pinned_anki_selection.py`、`test_card_drag.py` 全部通过；
- `handoff_check.py`：errors 0 / READY，唯一 warning 为既有脏工作区；
- release pipeline：18/18；Reader `--preflight-only` 通过 114 项定向部署回归；
- `release_preflight.py --artifact ...0.2.58... --skip-browser`：READY，版本门为
  `0.2.55 → 0.2.58`。

截至 2026-07-27 05:19 JST，Windows 固定测试机的 Tailscale peer 虽仍在目录中，但
Tailscale ping 与 SSH 22 均超时；因此 0.2.58 尚未写入固定 unpacked 目录，也没有完成
Windows 工程验证或用户人工验收。不得把上述本地自动证据写成“已部署”。

Windows 真机必须验证：

- 普通网页能注入且网页本身仍可点击/触摸滚动；
- 卡片拖放不重复、不半拍，跟随页面原生滚动；
- 侧栏浮动/挤压/实时调宽；
- 句级翻译、下划线、高亮、便签、临时绘图；
- 编号图片（含 remote-only）；
- PDF/EPUB/HTML/Favorite 只出现一套共享 UI；
- GOODBYE/reload/崩溃恢复 PWA 原生 UI。

## 11. 部署门禁

> ⚠ **怎么部署看 [`deployment-workflow.md`](deployment-workflow.md)（唯一权威）。**
> 生产文件清单的唯一事实源是 `scripts/reader_deploy_manifest.py`（当前 150 项），
> 唯一写入口是 `scripts/deploy_reader.sh`。下面这份文件列表是**给人看的重点提示**，
> 不能代替清单；漏没漏以 `python3 scripts/reader_deploy_manifest.py` 为准。

部署必须包含而不只包含旧 PDF 文件：

- `book-host.js`
- `pwa-extension-bridge.js`
- `epub-html.js`
- `html-reader.js`
- `reader.js`
- 四类书模板
- `pdf_reader.py`
- `html_reader.py`
- `assistant.py`（网页 AI 批翻的共享 action 与无工具文本边界）
- `app.py`
- `vbook_route_policy.py`
- `reader_sw_auth.py`

同时更新 cache-bust 资产清单和 Service Worker 的四入口私有页面清单。

**时间戳回滚副本、Python compile、JS syntax、restart 顺序、健康检查全部由
`deploy_reader.sh` 内建（含失败自动 `rollback_deploy`），不要手工重做一遍。**
脚本不覆盖、仍需人做的只有上线后的人工验收：`/login`、四书入口、旧网页端点 410/跳转、
扩展 handoff。

## 12. 禁止的“简化”

- 不得删掉普通网页扩展功能。
- 不得恢复 PWA 任意网页解析器。
- 不得把扩展变成只服务 PWA 的 provider。
- 不得在扩展接管时替换 PWA renderer。
- 不得把所有宿主 anchor/坐标强制成一种格式。
- 不得以统一 UI 为理由删除差异按钮或旧数据读取。
- 不得直接编辑扩展 `vendor/`。
- 不得扩大扩展到无约束全网读取权限来绕过编号图片问题。
