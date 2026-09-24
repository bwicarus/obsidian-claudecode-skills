# Reader 完整原生化

## 用户确认的最终目标（2026-09-21）

iOS 使用原生能力实现原软件的全部功能，包括阅读区。允许按股票 App 的简约原生风格
统一外观，但不允许以视觉统一为由删减操作、状态、数据或工作流。浏览器扩展保留自己的
网页界面；它不要求 iOS 使用网页界面。

- 不提供“打开原件界面”“完整界面操作”“返回原生助手”等往返入口来替代功能迁移。
- 旧 HTML/JavaScript 原件数据完整保留，逐类型重做原生交互；没有迁移的类型明确标记。
  标记表示尚未完成，不能计入功能验收，也不能通过删入口隐藏缺口。
- 回退是整版恢复到已保存的发布点，不是日常操作时在两套界面之间切换。
- 全部功能原生化以前，独立迁移分支不作为“已经完整原生化”的 TestFlight 版本发布。

2026-09-24 用户最新确认：**EPUB 正文保留 WebKit**，不再迁成纯原生文字排版。
已做的 Swift 按章读取、目录解析及数据层迁移继续保留；只保留正文所需的 WebKit
显示，清除隐藏界面的重复渲染。PDF、卡片、对话与其他已确认的原生迁移范围不变。

## 完整性验收

按用户能完成的任务验收，而不是按画出了多少组件验收。下列是已确认的最低范围，
还须与原版逐项核对，不能当作原版所有功能已经盘点完毕。

| 原有操作链 | 原生实现必须保留的行为 |
| --- | --- |
| 开书与阅读 | 本地书库、导入、PDF/EPUB、目录、搜索、页码、布局/缩放、书签、阅读位置、离线重开 |
| 选区与对话 | 文字/卡片/笔迹选中，输入框上方上下文附件、移除、发送时使用正确书页与选区 |
| 生成物 | 标准卡、媒体、图表、批量横滑、编辑、来源、收藏、稳定实体身份和原始内容 |
| 卡片与书页 | 拖放、锚定、锁定、移动、移除/撤销、关闭侧栏后的投递、重开书后位置保持 |
| Anki | 草稿编辑、确认保存、翻面、四级评分、复习队列、ReaderPC/AnkiMobile 导出、失败与未知结果恢复 |
| 手写 | 落笔即画、手指滚动、笔/橡皮、双击/按压设置、选区笔、批注坐标与合成上下文注入 |
| 对话 | 用户与助手流式文字、定稿去重、历史、切换/新建会话、生成物与对应轮次关联、阅读历史时不强制滚底 |
| 语音 | 模型/声音设置、通话与重连、后台、自动关闭、来电、上下文注入和诊断 |
| 工具与任务 | 分步状态、参数/结果/错误、生成物、保存工具、定时任务、多模态通知、视觉回执 |
| 数据与设置 | 原 ID/来源/学习状态、现有持久化与同步边界、账号隔离、冲突与结果未知的防重复语义 |

## 实施状态

当前候选分支：`codex/native-pdf-runtime-20260924`，独立检出，不覆盖共享主目录。
最初迁移分支：`codex/reader-fully-native-20260921`。
整版迁移前回退点：`reader-native-baseline-20260921-9078aecf`。
1.1.85 的 DOM 投影、模拟网页按钮和原件界面入口是待替换实现，不是原生功能完成的依据。

### 收尾清单（2026-09-25，固定范围）

以下是尚未关闭的原有操作链，不增加新功能。后文记录是各批次的历史，不表示整个任务完成。
发现同一链内的遗漏就在该项补齐；不能在一项完成后又将同一链描述成新的扩展任务。

- [ ] 对话：`ReaderNativeConversationScript` 的轮次排序、操作标识登记与事件分发由 Swift 接管；保留原件、卡片/媒体移除通知、选中附件和重复事件规则。
- [ ] 复习：模式/取卡、改进草稿、删除及暂存评分效果已接原生服务，等待最后统一 Apple 编译验证；本地评分和投递记录同事务保存，重试复用原回执，保留可撤销暂存、显式确认、未知结果不重发。
- [ ] 账户投递：`ReaderNativeCommandOutbox` 已接 Swift 本地/远端发送分流和逐项回执，兼容生产者仍交原命令入库；本地操作不出网，其他书籍延后，需在最后统一 Apple 编译核验。
- [ ] 开书与阅读状态：旧数据启动导入、PDF 文件写者协调、ReaderPC 上下文发布去掉网页业务依赖；EPUB 正文及局部视频继续保留 WebKit。
- [ ] 统一验证：上述代码完成后做一次完整 Apple 构建，修正实际编译/合同错误；检查原有词组、卡片排版、拖放、附件与输入法链路未回退。
- [ ] 出包：签名归档、导出并上传授权的 TestFlight 候选，记录真实构建号和结果；实机滚动/Pencil/多设备及弱网效果另列未验证项，不凭编译宣称通过。

当前没有全部迁移完成的可安装包。弱网语音协议更换仍是调研项，不混入此次出包。

2026-09-25 收尾代码：启动时的批注导入和修复、PDF 改页与页锚恢复事务、ReaderPC
上下文组装/投递、流式结果的历史恢复与定稿均已接 Swift。用户文字提交结果不明时
禁止再通过另一通道创建第二个任务；操作条目的撤销/重做保留原编号和实际更新后的编号。
PDF 工具栏不再扫描、监听或点击隐藏 HTML 按钮；收藏当前页与插入页编辑使用原生表单，
插入页本地自动保存，完成时才写回 PDF。EPUB 正文与局部视频继续使用 WebKit，兼容
生产者/状态观察适配仍保留，不应将“原生界面及事务接管”描述为仓库不再存在 JavaScript。
上一轮整批 Node 检查 2506 项中一项旧按钮断言已修正并单文件通过；这批新增工具栏代码
将集中验证，随后统一 Apple 编译。此前成功构建不能作为本批 Swift 代码的编译回执。

2026-09-25 对话成员续接：兼容生产者显式报告新增、移除、插入和历史提交，Swift 根据
事件维护侧栏顺序；不再在快照时扫描隐藏对话节点决定成员。历史取消保留旧记录，历史
提交保留正在流式生成的轮次，晚到语音转写仍插在对应回复前。兼容资源和少量动作原件
仍有网页句柄；不能把成员迁移误报为所有对话操作均已退出网页。

2026-09-25 卡片操作续接：侧栏和页面卡片的按钮标识由 Swift 生成，独立 Anki 评分
也走原生事务及发送入口；保留原 aid、失败恢复和未知结果禁止重复评分。网页只观察
已提交结果，成功/排队通知按回执去重，不再次执行评分。相关 Node 检查通过，新增
Swift 回滚及迟到回执案例留待最终统一编译；消息成员与排序的迁移继续进行。

2026-09-25 账户发送续接：Swift 从原生队列捕获原命令，本地笔记/划线/续读走同一事务，
服务器端点走现有网关白名单；网页只请求 flush 并显示摘要，不再传回命令或 HTTP 回执。
保留原操作编号、逐项确认、切换后的迟到结果隔离和其他书籍待发项；EPUB 续读设备索引
继续保存，PDF 晚到页码不能覆盖原生位置。Reader Node 整批通过；Swift 新案例待统一编译。

2026-09-25 对话续接：工具、卡片及流式回复正文改用 Swift 轮次原件引用，网页不再把这些
完整正文送回 Swift。版本不符时重新同步，工具接管不会重复显示前面的回复。媒体操作、
整卡带入和附件取消直接修改原生状态；Anki 带入保留整个组及原索引，图片只带原来的
元数据，移除通知按回执去重。缓存媒体可只读显示，不恢复过期操作。Reader Node 2500 项
通过，vendor 和离线 App 资源重建完成；新增 Swift 案例留到最终统一 Apple 编译。
消息成员/排序、部分操作标识及兼容事件仍未全部脱离网页，不能据此关闭对话整项。

当前完成的基础改动：

- `RC.turnCard.presentationOf` 提供独立于网页正文的实时展示快照，包含草稿、定稿、角色、
  工具/生成物结构与状态；返回深拷贝，不给展示层修改原件的机会。
- `partsOf` 继续只服务持久化，草稿不会因原生侧栏需要显示而被提前保存。
- App 的结构化轮次正文接入该快照，保留流式 Markdown 和稳定消息身份。

卡片操作接口已接通 `interactionState` / `performInteraction`：原生控件从卡片状态取得
可用操作，直接调用草稿编辑、答案显隐、确认/删除、评分和导出，不再查找网页按钮或伪造
input/click。编辑等待本地保存结果；确认/删除等待原仓库回执，结果未知继续阻止重复操作。
受控复习继续使用既有回调，评分/导出发起成功不等于外部完成。

卡面正文已改为结构化正面/背面/状态，原生 TextKit 显示文字、选择与链接；旧 HTML 由
SwiftSoup 2.13.9 仅作数据解析，普通排版与 ruby 注音由原生组件显示。内嵌媒体/脚本交互仍
明确标记待迁移，原始数据可在原生资料面板读取。工具参数/结果/错误/步骤按需读取到原生
详情面板，不再为查看详情切换旧侧栏。新文本选择接原焦点上下文，保持原版选中保持与释放
后 TTL 规则。iPad 注音跨行排版/系统选择手柄尚待实机验收，不能据此称全量富文本已完成。

仍有迁移依赖：轮次创建、部分独立旧消息、placement、其余阅读设置和阅读区；复习原生界面
已实现下述操作链并通过 Apple 编译，尚待 iPad 实机验收。
下一阶段接续这些原操作链，最终去掉 iOS 的网页 UI 所有权；不能只删除原有按钮。

验证：展示快照与持久化隔离合同、源数据不被快照修改、流式原文不受网页文字改动影响，
以及现有桥接生命周期/卡片交互离线测试。真实 iPad 手势、Pencil、Anki 外部写入和全部
原版任务链尚未在完整原生版本验收。新增离线验证删除网页按钮/输入框后仍可执行编辑、
显示答案、确认/删除；真实仓库配内存后端检查状态与实体 ID，受控评分检查回调和重复提交
拦截。Reader Node 合同 2104 项通过。没有执行真实外部 Anki 写入或 iPad 手势验收。
当前改动未发布、没有数据迁移或服务重启。

2026-09-21 开发续接：`69e0fedb` 已通过 Apple 工作流 `35547568072` 的模拟器编译、
设备归档和 IPA 导出（upload=false，未传 TestFlight）。继续迁移整卡上下文：原生卡片
可带入/取消，附件条直接订阅原选区登记器；沿用整卡 5 分钟与文本释放后 40 秒的区别。
学习卡的原生可见索引显式传入共享快照，保留 gid、源记录和各张卡的学习状态，隐藏网页
分页器不会决定选中哪张卡。原生图片卡读取既有 App 缓存路由，并提供查看/缩放、来源、
单图带入及移除。地图互动、SVG/动态图播放、视频和其他特殊生成物仍待迁移。
这些续接代码在离线浏览器中验证了真实登记器和原卡状态；`f3c4056e` 已在工作流
`35548259486`（构建 768）通过模拟器编译、签名归档和 IPA 导出，未上传 TestFlight。
后续 `ea57982f` 仅修复同图多实例取消上下文，共享源码与对应生成物更新，浏览器两项
通过；Swift 与构建 768 一致。此前 2104 项 Node 合同与 handoff（0 错误/0 警告）通过。
尚无 iPad 真机视觉/手势结果，不视为整版迁移完成。

下一条设置操作链已核对：共享 `rc-assistant.js` 的模型面板包括任务分组、受支持后端/
型号/思考档位、Codex 可选性/Fast、默认值恢复、预设保存/应用/删除；不能只移植模型名。
读取 `/api/assistant/action-prefs` 的目录和能力表，写入 `action-pref`，预设走
`pref-profiles`。语音独立走 `voice-config`，并保留设备级的回声桥、字幕、任务提示音、
工具口头回报互斥和卡片收起设置。原生工具面板已有 Key/文件夹等系统动作，不应把凭据
重新暴露给网页配置快照。

设置操作链现已接入原生 SwiftUI 列表、模型选择器和语音字段编辑器，原网页与 App 共用
读取/保存服务。保存仅接受目录中的有效组合和原设置白名单；服务端失败或会话变化不冒充
成功。预设仍保存/应用/删除原服务的数据，本设备选项继续使用原键。新增浏览器验证覆盖
目录、禁用模型、Fast、失败回执、单字段语音保存与本设备选项，三项浏览器用例及
2104 项 Node 合同通过。`494a395a` 在工作流 `35549846587`（769）通过模拟器编译、
签名归档、校验与 IPA 导出，未上传 TestFlight。

继续补上独立电脑通话页：接力目标、桥接器/Codex 语音状态、最近 Windows/连接错误。
状态读取复用原只读 STATUS 链路；不发送 START，不采音；目标切换复用原忙碌保护和保存
回执。仅进入电脑通话页或点击刷新时读取状态；部分服务离线不会抹掉已读取目标。
三项桥接浏览器用例和电脑通话定向合同 143 项通过；未执行真实通话或更改用户目标。
ReaderPC 服务模式等其他阅读设置仍待完整迁移；本页不伪造不存在的 CLI 模型/声音接口。
`4c448adc` 在 `35550229924` 完成模拟器编译、签名归档、校验和 IPA 导出，未上传。
本地 handoff 0 错误、1 警告（当时仅有本文件的未提交补记）；没有线上服务或真实配置写入。

复习工作区迁移保留的原语义：

- `rc-review.js` 的 `_queue`、`_idx`、`_scopeMode` 和原卡仓库仍是唯一队列状态；
  不建立 Swift 的第二套调度或保存队列。当前/全部范围、相关/到期数量、横向切卡、出处
  与队列重载都需保留。AI 用的 `snapshotState` 有正文截断，不能拿它作为完整卡面数据源。
- 卡面使用 `_projectReviewFaces` 的显隐规则，包括答案替换正面及答案追加两种，保留
  原 HTML/ruby 数据；原生展示解析数据，不能读隐藏 DOM 的 textContent 代替卡片。
- `_answerCurrent` 只暂存评分并前进；`_undoStagedRating` 在提交前恢复那一张卡。
  `_commitStagedRating` 在后续动作提交、失败放回；不能把暂存显示为 Anki 已保存。
- 改进卡片仍须从复习对话选整条回答或段落，`selectedPairs` 按当前卡稳定身份关联。
  `_prepareDraft` → 原生预览 → 按目标 `_commitDraft` 分开；保留详细/精炼、笔记/Anki/
  全部目标、流程记录、忙碌/失败/未知回执，不能将生成草稿与写入合并成一次操作。
- `openReview` 已优先进入原生复习区，通过 `presentationState` 与
  `performNativeInteraction` 使用原队列。固定评分栏、可调高度、范围/出处/重载、切卡、
  暂存撤回、删除、回答/段落选择、草稿预览与按目标确认、流程详情均已有原生控件。
  读取完整卡面不依赖网页按钮或截断的 AI 快照；原网页仍调用相同队列业务。
- 确认绑定原卡和草稿版本；晚到的旧卡/旧页面操作拒绝执行。原生选择共用原登记器的
  覆盖语义，整条回答已选时不重复注入其段落；旧回答不会重新归属到新卡。
- Node 合同 2110 项通过，后续旧卡围栏/加载保护定向 39 项通过；真实 Chromium 以原
  rc-review 和原登记器验证原生桥接，无网页按钮仍可翻面/暂存/撤回及选段，定向用例通过。
  未做真实 Anki 写入/删除。`dfc1515c` 在工作流 `35551431246` 完成模拟器编译、签名
  归档、校验及 IPA 导出，耗时 11m1s，未上传 TestFlight。iPad 手势、复杂 HTML/内嵌
  媒体卡面的验收仍待完成。

全文搜索续接：原生搜索框/结果列表复用 `RC.readerSearch` 的查询及定位服务，PDF 的
页码偏移、命中计数、未完成 OCR 提示与原文高亮，以及 EPUB 章节定位均保留。App 的
既有 native-local-runtime 接管查询端口，继续使用本地文字/OCR 数据。桥接只暴露当前
查询的临时结果 ID，更新查询取消旧请求，旧结果及换书后的跳转拒绝执行。Chromium
定向用例通过；共享生成物已同步，网络审计 0 新债务并去掉 2 条已消除的历史豁免。
搜索 Swift 尚未在 Apple 编译或 iPad 验收。
本地 handoff 首轮发现共享策略的两条声明被误作另一类书籍的实际请求；按已有 manifest
规则限定声明归属后，接口兼容性 7 项全部通过。该首轮其余 2109 项 Node 合同和其他
handoff 检查通过；仅复查失败组，未重复运行全量 handoff。没有放宽实际请求的路由检查。

构建节奏按用户最新要求调整：相关功能合并修改与本地检查，阶段性统一编译，失败时
读取详细错误修复；准备交付才做签名归档。不得每完成一个功能就单独发起完整构建。

2026-09-21 书页卡片批次：

- `placeCardAt/placeHtmlAt` 返回原仓库实际保存结果；原网页同步入口保持兼容。App 不把
  “已发起创建”报告为放置成功。已有原卡 gid/cid、完整学习状态及 HTML 原件不改编号。
- `nativePlacementState/nativePlacementAction` 沿用原仓库并检查页面代次/记录版本，
  原生卡头移动、收起/展开、显式文字锚定、移除 placement 不模拟网页按钮。
  普通拖动仍是自由卡；已绑定卡移动后重取新位置的词锚，空白处解除旧词锚。
- SwiftUI 书页卡片层复用侧栏原生卡面/卡组，当前文档 renderer 仅供坐标与原状态服务。
  已接管的卡片隐藏旧视觉；带笔迹或复杂媒体/脚本内容暂未接管，原件完整保留。
  正文词锚标记、卡片私有笔迹和完整阅读区仍待迁移；不是整版原生化完成。
- 关闭侧栏后显示原有浮动队列的原生卡片，保留原计时器、收放及阅读续时。拖入书页
  等待保存成功后才移除浮动实例，失败保留原卡；不从历史对话重新批量建卡。
- 本地 Node 2112 项通过。浏览器验证移除旧按钮后仍可收放/移动/移除，保留原 cid，
  并拒绝过期操作；旧拖放测试改为模拟异步已保存回执后定向复查通过。
- 新增 compile_only 工作流输入，合并搜索/书页卡片执行模拟器编译；签名、设备归档、
  IPA 导出及 TestFlight 上传全部跳过。`8ff8e8cd` 在 `35559373095` 通过，仅用 5m2s。
  iPad 实际触控仍未验收。

后续同批完整性补齐：卡片层置于 PencilKit 下方，避免遮住本来位于顶部的笔输入画布；
书籍/账号范围与对话模式分开，切复习不隐藏书页卡，换书则拒绝投射旧控制器。
词锚描边与原编号现在由原生控件显示，编号直接来自原阅读顺序计算的 ordinal；
打开/关闭共用 `toggleBoundCard`，不点击网页标记。字词锚定卡拖到空白后清旧标记并恢复
自由卡可见状态。带笔迹/复杂媒体卡片和笔迹数据的完整原生展示仍未完成。
Chromium 已验证文字锚定 → 标记 → 展开 → 收起 → 拖到空白解除锚定，以及失败保留
浮动原卡/成功才关闭；绑定定向 Node 原静态测试已调整为检查共用函数与旧回调连接，
检查内容不减。`dd3aaece` 在 `35560090299` 通过模拟器编译（3m14s），不重复签名归档。

原生导航批次：目录数据和动作抽为 PDF/EPUB 各自的 `RC.readerTOC`，原网页目录也使用
相同接口。SwiftUI 目录显示原层级和印刷页码，支持章节过滤/刷新；宽屏选章后保持打开，
窄屏跳转后关闭。晚到请求、换书与旧目录条目不执行跳转，网络错误不伪装为空目录。
页码导航通过 `RC.readerNavigation` 接原 renderPage/阅读位置保存/跨卷逻辑。原生滑条
只在松手提交，输入仍按书上页码换算；前后翻页和 PDF 返回最早来处继续使用原处理器。
原生顶栏页码不再模拟网页 pointerdown/up。目录与导航的 Chromium 定向用例通过，
包含偏移/跨卷/最早返回点、保存/渲染失败、旧请求与换书拒绝；共享 reader.js 已重建，
网络审计 0 新债务。`bc43a261` 在 `35561109116` 通过 Apple 模拟器编译，签名/归档/
上传跳过。随后 `201f2534` 补上 App 命令入口允许的四项导航命令，定向浏览器检查同时
验证该 Swift 白名单后通过；这处常量名单续接未单独重编译。没有 iPad 手势或发布回执。

卡片手写和账户批次：原生卡片通过现有 PencilKit 捕获层注册独立笔面，保留原 `strokes`
与 `iar` 坐标格式、压力、选区编号及保存队列。书页和卡片混合一笔要全部保存后才完成
上下文 pending；同操作重试沿用 mutation ID，卡片移动后拒绝旧坐标。原生图层进入既有
合成截图路径，带笔迹普通卡不再退回网页显示。区域选中完整注入及真实 iPad 笔操作待验收。

用户追加 Apple 账户登录：AuthenticationServices 原生按钮 → 固定账户服务器挑战/
身份验证 → 原账户绑定或受邀新建，复用原 user ID、namespace 和会话。验证 Apple RSA
签名、issuer/audience/expiry/nonce；不按邮箱合并。关联票据限时、限次数、一次消费。
共享 Safari 设备令牌在成功换账户后更新。发布工作流增加 Apple capability 与 profile
验证，仅签名发布时执行，compile_only 不操作 Apple 开发者资源。

本地账户测试 5 项及卡片桥接浏览器用例通过，卡片持久化合同 27 项通过。全量 Node
仅旧网页登录源码断言两项失败，改为检查原生回调/同源 cookie/nonce/共享令牌后对应
25 项复查通过；部署清单 15 项和网络审计 0 新债务通过。当前未部署服务端、未启用
真实 Apple capability、未签名上传，新增 Swift 等待批次编译；完整阅读 renderer 仍待迁移。

`0879438d` 在 `35563071485` 通过 Apple 模拟器编译；签名、Apple capability 操作和
TestFlight 上传均跳过。随后补齐原生账户状态/退出，退出清会话和本机共享令牌，关联关系
及书库保留，账户测试增至 6 项通过。

卡面继续迁移：SwiftSoup 只解析原件数据，原生 Layout/TextKit 展示表头、跨行/跨列合并
单元格和 ruby/文字选择，宽表仅横向滚动；书页普通表格卡也接管原生显示。原生卡片右下
尺寸手势沿用 `voiceCard.cardSize` 的设备呈现仓库，等保存回执；改变卡片呈现尺寸也会
使旧笔迹坐标失效。地图卡投射原中心/缩放/标记数组，MapKit 负责查看、缩放和回到标记。
原始卡片、来源和选中/移除入口不变。两项实际 Chromium 原卡/图片/placement 用例及
全量 Node 2113 项通过；新增表格、地图和尺寸 Swift 待下次合并编译与实机手势验收。

`91d1809f` 在 `35563988289` 通过 Apple 模拟器编译，签名/归档/上传均跳过。
随后对话生命周期接到共用语义服务：原生“新话题”沿用普通语音 fresh 连接，不删显示
与持久历史；电脑通话没有相应能力时不假装支持。清空当前模式对话有独立原生确认，
等待清空回执，失败重载原历史；回顾学习截止点只有用户明确勾选才更新。

PDF 阅读设置迁移为原生 Form：生词/点词、旋转布局、书籍语言、插图描述、四边去边、
语法呈现、高亮色板和诊断开关。共享 `readerPreferences` 直接读写原设置所有者，不依赖
设置 DOM；语言/插图保存提取共同事务，非成功回执不修改内存状态。打开设置读取原书
语言/插图/裁切值，失败显式报告；范围检查拒绝换书后的旧命令。EPUB 设置和完整书页
renderer 仍待迁移。离线 Chromium 两项覆盖对话生命周期、原书配置读写/失败/过期拒绝；
全量 Node 2113 项通过，网络审计 0 新债务。新增 Swift 等待本批编译，未发布。

`d8600995` 的 `35565202278` 在打包静态接口审计失败：共享策略声明被误识别为 EPUB
发起 book-figures 调用。沿用既有精确 metadata scanIgnore 修复，离线 314 文件打包
成功；`919fc45d` 在 `35565465555` 通过 Apple 模拟器编译，未签名上传。阅读设置随后
补齐远端插图服务离线时的局部不可用状态，本机语言/去边等配置仍可使用，浏览器验证通过。

开始主体 PDF renderer 的原生实现 `ReaderNativePDFDocument`：PDFKit 直接持有原安全
范围文件 lease，原生滚动/双页/导航；坐标经 PDFKit 映射回原旋转后 OCR 页面，保留原
字符索引/geometry digest/content digest。选区晚到或换书不投射旧内容；高亮/墨迹/选区
域沿用原子导出数据，摘要及域版本全部通过后才一起更新纯显示层，原件没有被改写。
**该组件尚未接入主界面**：需要继续接齐扫描页选区交互、卡片锚定/用户插页、笔迹保存与
上下文/复合截图后再替换，不能单独启用丢功能。原生卡片笔迹已复用同一绘制器，补齐
压感二次曲线、直线/箭头/矩形、带时间的选区编号和三/四/八位旧颜色兼容。待本批编译。

`04a5d274` 在 `35566206868` 通过模拟器编译，未签名或上传。随后补原生扫描页 OCR
文字命中/长按选区，通过原字符索引、页几何和书籍摘要隔离读缓存；OCR 层更新自动作废
旧选区。选区传递使用原文字焦点的保持/释放机制，保留非连续索引，不误用生成物的 TTL。
这些 PDF 阅读组件仍未接入主界面，待接齐锚定、插页、笔迹保存和截图后整体替换。

整模块 Chromium 加载发现此前 voicecall 新增生命周期方法漏写结束分隔符；已经补齐并
加入初始化异常断言，避免只验证提取函数漏掉整模块语法失败。实际组件浏览器用例通过。
Apple 账户区改为无缓存只读会话查询，不以历史同步回执推测登录；未知/离线单独显示，
保留原账户绑定、退出和共享令牌流程，隔离账户测试通过。该批新增 Swift 待编译，未上线。

`08c145e3` 在 `35567325100` 通过模拟器编译（含 Apple 账户真实状态和 OCR 选区组件），
未签名/发布。继续将原版纯字符算法打包给 JavaScriptCore：只计算数据，不创建网页，
原生触控和 Canvas 展示复用原版阅读顺序、词边界、多栏/表格过滤和选区矩形合并规则。
构建时从原函数确定性提取，原件源码改动会自动进入包，缺失边界或内容不符会阻止打包。
原字符索引在内部排序前后保持映射；跨行 CJK、表格隔离、竖排和 PDF 精确选区检查通过，
整模块浏览器桥及离线打包通过。新增 JavaScriptCore Swift 接线待编译，主 PDF 尚未替换。

`44716720` 在 `35567956977` 通过模拟器编译，未签名/上传。扫描页随后增加原生选区
双端手柄与系统编辑菜单（复制、按原句边界扩选），手柄拖动只占自己的触点。真实 iPad
选择/滚动竞争尚待验收。截图 broker 增加原生阅读视口入口，保留同一 Pencil/卡片共同
祖先合成、裁切与图片额度；工具截图也使用同一合成入口，不再漏掉原生卡片图层。
相关选择和截图合同通过，新 Swift 待合并编译。主 PDF 的保存、锚定、插页与上下文端口
仍待接齐，不能宣称主阅读区已切换。当前 Windows 账户服务经只读查询确认未部署 Apple 登录。

`df488317` 在 `35568795248` 通过模拟器编译，未签名/上传。扫描页随后补空白点击清选区，
手指轻点观察器不接管文字、手柄或 Pencil。检查原笔迹保存发现同步 host 回执早于 900ms
防抖落盘：现新增原 host 的异步 persist 回执，Swift 保留未确认笔迹直至原仓库成功保存，
同一操作重试不重复画笔；Realtime pending 在持久回执后才释放。PDF/EPUB 普通保存和
原插入页保存共用按书页串行的快照传输，回收 DOM 图层后重试不把空数组写回。旧网页的
关闭页 beacon 兜底保持原语义，不把其发送成功当作原生持久回执。5 项实际 host/传输用例
覆盖失败、重试、顺序、页面回收及临时插页；相关 21 项合同及 10 项 Chromium 桥用例通过。
网络清单正式登记原两个墨迹保存端口，移除对应 5 条旧债务，没有放宽豁免。该批 Swift
待合并编译，Apple 登录尚未上线；主体 PDF 仍未挂载，继续迁移坐标和原内容能力。

`57b7564c` 的 `35570049807` 停在打包接口审计，未进入 Swift 编译：共享政策声明再次被
误当成另一文档的实际请求。现从请求覆盖检查中分离纯声明文件，并禁止该文件发请求；
移除逐条声明豁免。真实同步批次路由和日志调用单独核对方法/入口；删去已无 App 调用者
的旧 `/anki-draft` 清单项，原服务端和当前原子草稿流程未删除。接口/Anki 定向 19 项通过，
离线包 315 文件生成及核验通过，打包器 7 项通过。不是新增接口豁免。

PDF 原件笔记投影与坐标解析继续补齐：保留原 ID、卡片字段、尺寸和私有笔迹，拒绝版本
倒退/重复 ID；卡片文字锚点直接复用原 `_resolveRange/_rangeRects`，保留精确字符集、
两套块号及歧义拒绝规则，6 项纯算法验证通过。这些仍是主体 PDF 接线前组件。
HTML 生成物和表格单元格开始显示原生内嵌图片，走原资源代理和原卡作用域的 opaque ID；
原件修改后旧图片请求拒绝。仅已能路由的页内图片卡接管显示，媒体/脚本等未迁移类型仍
保留原件。浏览器新增真实内嵌图片/陈旧请求用例通过，Swift 待本批编译，未发布。

`94d96fe9` 已通过模拟器编译 `35571466354`，签名、归档和上传未执行。
随后补原生导航桥：原 goToPage/前后页/双页三态/单页/缩放/适应入口保留；PDFKit 实际
位置带书籍身份、内容摘要、视口令牌和递增序号回到原阅读位置持久化及 AI 上下文。
原生准备阶段读取既有位置仲裁和原件状态包，接管需真实布局；换书、内容进程终止先撤销。
连续滚动采用事件触发合并单路发送，不轮询。隐藏网页已排队的滚动/位置恢复不能覆盖原生
页码；迟到跳页回执不能覆盖更新的手指滚动。原生可见页代替旧双页推算进入语音上下文。
7 项导航行为验证、10 项 Chromium 侧栏验证、315 文件离线打包通过。此次桥接 Swift
待合并编译；原生主体仍未挂载，裁边、卡片/Pencil 坐标、完整内容交互接齐后才能接管。
Apple 登录仍仅在开发分支，Windows 在线服务尚未部署该入口。

## 当前原生业务迁移（2026-09-24，隔离候选分支）

用户已授权把 App 的 PDF、批注卡片及对话依赖完整移到 Swift，完成后再出新版包。
`codex/native-pdf-runtime-20260924` 已停止隐藏网页页图/预取，文字定位直接读原生字符几何；
PDF 选区、三方合并改为 Swift，以网页纯函数产生的真实结果作跨端对照，不再打包原来的
两个 JavaScriptCore 资源。SQLite 就绪后的阅读域读取和 notes/highlights/ink 写入由原生接管；
便签 API 的验证、字段合并、派生索引、日志与幂等回执在同一 SQLite 事务中完成。
这些是迁移阶段成果：隐藏 WKWebView、对话/卡片交互、部分同步入口仍待替换，数据库开关
仍沿用原就绪握手，不能把候选称为完整原生版本。编译通过不等于实机内存/电量验收。

后续阶段：PDF Pencil 表面直接由 PDFKit 登记，笔画、圈选、擦除、分页撤销/重做及待同步
标记在 Swift 同事务保存，停笔 60 秒后把最新整页合并进持久队列；未知回执按同一操作
编号重试。页卡的改绑、移动、尺寸和手写也移到原生业务层，卡片更新与复制命令同时提交。
工具/HTML 注解卡开始从原生 notes 直接生成 SwiftUI 展示及操作标识，不再由隐藏网页
序列化其正文；图片沿既有本机资源路由读取。学习卡、收藏/选区共享注册表、对话和复制
传输仍有过渡适配器，尚不能卸载 WKWebView。当前未生成可发布的完整迁移包。

`92acbba2` 已在 `35573149419` 通过模拟器编译。下一批接原百分比裁边到 PDFKit
显示框，不修改原件 cropBox/OCR/卡片归一坐标；Core Graphics 的 PDF 变换处理旋转
和非零原点，Canvas 按显示框裁剪墨迹。切换需原生回执，设置先走原持久化端口；后台
保存失败不会假装已启用。原生适应宽度跟随容器宽度变化，手动缩放退出宽度适应。
新增 macOS PDFKit 实际 PDF 坐标验证，覆盖四种旋转、非零原点和原件保留，待 CI 执行。
8 项导航/裁边行为和 10 项 Chromium 侧栏验证通过，主体仍未挂载，未签名发布。

后续业务迁移：本机日语词典直接在 Swift 读取已安装且校验过的分片，复用旧候选与活用规则；
网页只消费结果。复习候选从原生卡库读，评分在原生事务中校验内容/状态修订并同时保存历史。
账户域的事件上传和复习队列交互仍有网页适配层；成功编译不表示这些依赖已全部移除。

词典面板的掌握操作改由 Swift 确认兼容词库并写原 vocabulary-state 集合，词形别名、
因果父记录及增量同步日志保持同一合同；网页暂只观察已提交记录来更新下划线。
词组收藏和词汇显示投影仍待原生接管，不能据此关闭其兼容观察器。

生词显示投影随后移至 Swift：从原生字符和 vocabulary-state 计算词元、别名与最长掌握范围，
服务器增强异步合并进原有有界本机缓存；换书/文字修订/词汇变更拒绝迟到的投影。
数据层缓存失效包含不进入出站日志的同步写入。旧网页目前仅提供阅读开关、搜索待办和
遗留即时覆盖；译页、插图及对话控制仍继续迁移，尚未生成完整原生发行包。

译页进一步改为 Swift 按本机字符分句、调用原批量翻译服务并排版，保留注音过滤、句号/
小数、跨栏及竖排规则。译文按书页和字符修订有界缓存，关闭译页、换书或 OCR 更新会取消
过期任务；页面装饰内容未变时不再发布重复刷新。插图及对话业务仍待迁移，新增分句与
排版的 Apple 对照检查随候选构建执行。

插图的数据获取与带入状态改由 Swift 持有，沿用既有几何编号、描述服务和笔迹合成字段。
已选插图独立于有界页面缓存；消费通知带本轮附件令牌，迟到的清除不会删除后来重选的图。
旧对话通道暂只观察数据投影，不再为 App 附件生成隐藏缩略图；完整对话协议仍待迁移。
新增旧实现对照与跨书/重试/消费检查，随候选 Apple 构建验证；未上传可安装完整迁移包。

性能路径按 Apple 的 PDFPageOverlayViewProvider 和 Improving app responsiveness 指引收口：
生词计算移出主线程，翻页取消离屏未完成任务，派生装饰只保留有界工作集；每页请求独立
代次避免取消旧任务时误清新任务。实机卡顿、Pencil、内存与耗电仍需在最终包上验证。
参考：https://developer.apple.com/documentation/xcode/improving-app-responsiveness

对话 SSE 接收、UTF-8 分帧、任务编号续传和取消移至 Swift actor / URLSession；请求仍经原
白名单、书籍映射和账户凭据策略授权。首次提交结果不明只按 rid/from 续传，不重发原请求；
消费者确认后才推进游标，取消、换书、关联变更及未知事件执行结果都停止接收。
后台重连等 App 激活后继续，不依赖隐藏网页计时器。现有对话 reducer 暂收结构化事件，
历史、业务动作和主接收协议仍需迁移，不能据此称为完整原生或声称实机提速。

`eb145996` 的 Apple 构建 `35943057892` 已通过完整 App 编译与数据检查。下一批词组收藏
改由 Swift 保存原 device 集合及待更新记录，OCR 分词、统一词汇状态和服务器镜像消费同一
持久化意图；明确的收藏目标使重复点击安全，迟到镜像回执不清除新修改。历史清单取回失败
保持未初始化状态，不能以空清单覆盖。原生面板直接调用此入口，网页兼容客户端只读取提交
结果；仍有对话控制、复习控制及 EPUB 等依赖，不是完整迁移发行版。SSE 待消费缓冲加上
8 MiB 上限，防止消费者暂停时无限积累数据。

词组阶段的 `35944361381` 通过数据检查但 App 编译暴露镜像入口归属错误，已修正为明确的
ReaderLocalRuntimeServer 转发入口。历史读取/清空现由 Swift actor 合并并发读取、隔离 normal
与 review、取消清空前或换书前的旧响应；服务器仍是历史权威来源，未知清空结果不重试。
现有回放操作适配器仍消费历史数据；不是已经移除全部隐藏消息节点。

`37371cdf` 的 `35944991035` 已通过完整 App 编译和历史生命周期检查。收藏夹下一批改由
Swift 串行执行原服务器集合的读取、保存、删除与恢复，学习卡完整 payload、cid/gid、修订
保留；未知保存结果只保留会话内容并提示待确认，不自动补发，删除失败不清掉原清单。
拖到 PDF 的自由卡直接调用原生 notes 事务，不为创建卡片挂载隐藏页；旧网页暂观察快照
以维持尚未迁移的共享选区注册表，收藏按钮和轮播不再生成隐藏 DOM。新增失败/版本/回收站
检查及 Apple 编译待本批验证；未发布或声称全部原生迁移完成。
# Native preference transaction ownership (2026-09-24)

`fa4e99f7` passed Apple workflow `35948651511`, including the complete App build.
The next stage moves review queue acquisition and its device-local recovery snapshot to Swift:
local Reader cards remain authoritative, remote related/due selection uses the existing native
gateway, and request leases reject delayed loads/saves after a context change. Local database or
snapshot-save failures never fall back to a second browser acquisition. The App no longer mounts
the hidden review workspace, toolbar, carousel, CSS or answer-decoration observer; the existing
structured answer selection and staged score/undo commands remain available to the native view.
The review interaction reducer, improvement drafts and HTML face projection are still transitional
JavaScript, so this is not the complete native review migration. Local Node checks: 2436 passed;
new Apple queue tests cover scope separation, local authority, offline recovery and cancellation.
No new installable release has been uploaded from this stage.

`6e05fdc2` passed Apple workflow `35949941653`, including native queue cancellation/cache checks
and the full App build. The following review-face change sends original Markdown/HTML as data
to Swift and applies divider/replacement/provenance rules with SwiftSoup and the existing native
GFM parser. A bounded cache reuses unchanged faces; original card content and scheduling IDs
are never rewritten. Native state publication and flip/undo no longer create even detached web
face nodes. Source navigation and the review action reducer remain compatibility operations;
the native parser and full App build passed Apple verification in `35950646876` (`29eaf8cf`).

Review improvement preparation and explicit confirmation now go through a native owner using
the existing server routes, frozen draft IDs, original entity/index and selected answer pairs.
Changing card, scope or verbosity cancels/discards an obsolete preview. A commit reserves a
receipt in the existing device store before dispatch; known success is reused, while an unknown
outcome is never silently submitted again, including after reload. A late confirmed receipt is
saved even when its original view has left. The web adapter currently still mirrors the resulting
state and manages navigation/staged ratings; it does not run a second improvement request.
Local Reader checks: 2438 passed. Native receipt/cancellation cases and full App compilation
passed Apple build `35951809245` (`63e8761b`). No new installable full-migration package has been produced.

User direction (2026-09-24): preserve functional equivalence during migration; discuss potential
architecture upgrades and user-visible consolidation/removal before implementing them. Purely
duplicate internal implementations may be consolidated under the approved native ownership.
Use Apple native components and Liquid Glass for appropriate navigation/floating controls;
maintain content legibility and system accessibility behavior rather than redesigning every surface.

Review staging/one-step undo now has a Swift owner scoped to the active queue lease. Staging
does not persist removal or send a score; undo saves the restored queue before consuming the
stage, and taking a score is single-use. The App adapter waits for an in-flight stage before
loading/exiting and discards obsolete card replies. Actual Anki interval adoption runs in the
existing native card transaction, proves the matching local rating and preserves committed
reps/lapses. A later score/content change rejects an obsolete scheduling response. Navigation
and external score/outbox delivery still have JavaScript compatibility operations. This stage
requires Apple validation; it is not an installable complete-native release. Apple run
`35953136025` caught use of the repository's non-negative validator for Anki's signed
learning interval; the schedule adapter now parses signed finite values explicitly.
Immediate external score submission also moves to the native gateway, joins matching in-flight
answer IDs and retains their result through view cancellation. The existing account-scoped
offline outbox remains the durable retry owner; no second unscoped retry queue is introduced.
The follow-up Apple run rejected the legacy adapter's undeclared `review.scheduleSource`:
native provenance is now kept in the durable operation receipt, without changing the shared
card-state schema. Interval/counter checks, native queue/answer tests and the full App build
passed Apple run `35953888490` on `55b84e1c`.

EPUB archive access now uses a file-backed ZIPFoundation 0.9.20 actor, independent of the UI
thread. The App reads a bounded catalog then individual original chapter/resource bytes; it
no longer loads JSZip or copies the complete compressed book into web memory. Entry reads
retain archive identity, size/CRC/path checks and the current security-scoped book lease.
The compatibility chapter sanitizer, spine/TOC parser and display remain pending migration;
this stage does not claim a native EPUB reading surface. Browser archive behavior is retained.
Local verification: 163 focused Node checks and seven packaging checks passed. Native ZIP
fixtures and full App compilation passed in Apple run `35954743708` (`4fb06d1f`).

Follow-up: package manifest, spine order and EPUB 2/3 table-of-contents parsing now run in
the native archive actor using Foundation XMLParser. Chapter indices, original UTF-8 paths,
title/TOC whitespace, duplicate-link filtering and filename fallback remain compatible.
The web adapter consumes the resulting metadata without parsing container/OPF/nav documents.
Chapter HTML sanitization/display and existing anchors remain in the EPUB WebKit reading
surface, as explicitly chosen by the user. Native publication fixtures and full App compilation
passed Apple run `35955305125` (`1cd60150`).

Plain assistant responses now publish source Markdown and completion state to the native
conversation alongside structured turns. The App no longer constructs their hidden Markdown,
inline images, math layout, word-reveal spans or animation loop, and no longer reads the
hidden thread's scroll metrics. Existing follow-up/feedback/playback actions remain available
through the compatibility action layer while it is migrated. Browser rendering is unchanged.
Full Reader regression: 2441 passed; native source publication also verifies unchanged-body
completion, long Markdown preservation and absence of web render/layout calls. These changes
passed full Apple compilation in run `35955847195` (`8bcb8ee1`); no installable
full-migration build has been released.

The explicit card/image context selection graph now has a Swift owner. Native state handles
semantic identity, parent/covers containment, stable cycle resolution and five-minute expiry;
repeated projections do not extend the lifetime. Text selection retains its distinct existing
40-second focus behavior. A disposable synchronous web projection remains for unmigrated
callers, without its own timer or persistence. Sending first drains native selection intent and
checks expiry; a failed native update blocks that snapshot instead of reusing stale web pins.
Navigation/recovery discard the selection lease, and delayed acknowledgements cannot replace
newer pending selections. Native/browser graph and expiry parity is checked by the Apple workflow;
this is not yet removal of the entire conversation compatibility runtime.

The context graph and full App compilation passed Apple run `35957118578` (`fe76d7c9`).
User-reported native card regressions are being addressed before packaging: favorites now
render original HTML/Markdown and Anki front-face previews instead of literal markup; card
body fonts use Dynamic Type body/headline sizes. Phrase queries expose an explicit save action
above definitions, keep the entire selected phrase for favorite/mastery writes, and consume
the old selection highlight without cancelling the query. Mastered phrases remain part of
native tokenization without becoming favorites. PDFView owns the Reader drop receiver and
freezes page coordinates at release; native information-card drops can use full original data
without a hidden web card body. Focused drop tests cover Anki identity, long originals and
failed saves. These changes still need Apple compilation and on-device gesture/visual acceptance.

Apple run `35958580625` passed native data checks but rejected the phrase refresh call's
missing device ID; that call is corrected. Phrase lookup now reads saved/mastered state
from the native stores on both PDF and EPUB, without inheriting a dictionary headword's
mastery. EPUB lookup consumes the visual selection after capturing its text/context, and
card body height follows the measured Dynamic Type header. Focused regression checks passed;
the follow-up App compilation passed Apple run `35959337658` (`2c4c1961`).
On-device gesture/visual acceptance remains pending; no full-migration package is published.

Outgoing assistant turns now use a native request planner for default prompts, explicit
selection versus implicit book-context policy, normal/review mode, and stable request/turn
identity. Disabling book context preserves explicitly attached text/cards/images. Preparation
is read-only, and a failure or mode switch cannot silently submit the old request. The existing
native SSE transport reuses that frozen identity for continuation. The web event/action reducer
is still a compatibility dependency and remains part of the migration work, not a completed
native release. Card tables now inherit the same readable Dynamic Type font as card text.
Request policy checks and the full App compilation passed Apple run `35960095948`
(`5a35c92f`); no signing or upload was performed.

Streamed answer ownership, cumulative response state and voice/display/follow-up parsing
now run in a native per-request reducer. The compatibility effect handler consumes its
projection; it no longer reparses native text increments or chooses between the normal,
tool and delegated-task answer surfaces. Completed or malformed events are rejected,
and an uncertain effect acknowledgement stops without replay. Legacy/browser parsing
is retained and used to generate native comparison fixtures. Tool action dispatch and
historical message assembly are still compatibility dependencies; this does not remove
the complete old conversation runtime. Full Reader checks found one obsolete busy-state
assertion; the assertion now includes the new request-preparation boundary.
Native/browser answer-parsing comparison and full App compilation passed Apple run
`35960572200` (`b9c60546`).

History reads now prepare message classification, existing valid history/turn identity,
plain response text and subtitle/follow-up behavior in the native history actor. The raw
server response and card/attachment data remain unchanged; native display metadata is
non-enumerable in the compatibility adapter and cannot leak into an upserted original.
Malformed rows retain their positions for existing per-row recovery. Legacy records without
a valid stable ID keep their previous fallback identity. Mode switches and clearing still
fence late history responses. Effect controls and historical artifact materialization remain
compatibility work; this is not a claim that the hidden conversation shell has been removed.
Native composer commands also wait for local send acceptance after context preparation,
without waiting for the streamed answer. Preparation failure retains the draft and reports
the actual error; a successful local acceptance does not claim server delivery.

History presentation and local send acceptance passed Apple run `35961414240`
(`acd37bad`). Delegated-task polling now has a native lifecycle, bounded read-only retries,
foreground suspension and cancellation when its document context changes. Unchanged
snapshots do not repeat delivery; cumulative client effects retain their indices and are
reserved before acknowledgement, so an uncertain delivery is not replayed. The existing
task-status GET is explicitly registered in the shared native interface manifest; unrelated
voice endpoints are not admitted. Result and undo controls remain connected through the
compatibility effect adapter. All 2459 Reader Node checks passed; Apple compilation of
this task-monitor stage passed run `35962906764`. No installable migration release has been published.

Review-card navigation now commits its recovery position through the native queue before
changing the visible question. Duplicate navigation joins the same operation; failed saves,
changed cards and expired queue leases retain the previous question. Earlier staged ratings
and queued cache writes settle before selecting a new card. The 100 focused review checks
passed; Swift navigation cases and App compilation passed Apple run `35963783020`
(`918408ca`).

User clarified that video playback keeps localized WebKit, like the existing EPUB
body exception. Video cards now expose native thumbnail/play/selection/remove controls;
opening playback creates a separate WebKit island with only the packaged player module.
Closing or backgrounding releases it, its network work and subtitle timers. The player
retains subtitles, transcript, start/end, speed and loop behavior; its narrow native bridge
reads/writes the existing device preference record and requests subtitles through the
registered gateway. Video note edits retain the original ID and other playback fields.
YouTube embed identity uses the installed app's bundle ID, per the official WebView
guidance (https://developers.google.com/youtube/terms/required-minimum-functionality).
Apple run `35964904274` (`6617b60e`) passed the native checks and complete App compilation.
Physical-device playback remains unverified. Legacy video cards in favorites and pinned
HTML also resolve their existing YouTube thumbnail identity into the localized player;
unresolved originals remain visible rather than being silently removed.

Follow-up: the PDF reading-settings panel now reads canonical preference/book records and performs native writes for toggles, grammar display, palettes, languages and crop. Figure settings use the existing native server gateway; a failed remote read leaves local settings usable and disables only the unavailable figure control. The small remaining observer updates legacy presentation state after committed results, without saving/fetching/rendering hidden pages. Earlier compatibility intents are drained before an explicit settings action, with uncertain writes surfaced instead of overwritten. Native crop commands persist through the shared PDFKit viewport owner and restore the previous visible crop when position persistence fails. Book-language/crop records retain their existing IDs and CAS/replay semantics. Focused JavaScript checks: 228 passed; native book-setting rollback/CAS tests are included in Apple verification. Settings still share a temporary observer with the unfinished conversation/EPUB migration.

The App now sends preference intent (including first legacy-mirror migration) to Swift. `ReaderNativePreferences` owns envelope construction, tombstones, causal parents for global settings, expected-revision checks, durable replay receipts and journal writes in one SQLite transaction. The existing 55-key DataRegistry allowlist generates the packaged native catalog; no second settings namespace or database is introduced. Browser/extension PreferenceStore behavior is retained. Native failures leave dirty compatibility intent and never invoke the old writer as a fallback.

PDF vocabulary/ruby flags read canonical settings directly. Transient translation/search state and the remaining settings UI commands still use compatibility adapters; this stage does not claim those modules or the full migration complete. Local verification: 234 focused checks, catalog parity against DataRegistry, deterministic ReaderBundle packaging. Apple workflow additionally compares 220 native/browser write envelopes and checks CAS, retries, transaction rollback and corrupt-record handling. Full App compilation is required before this candidate is considered validated.

PDF assistant highlight/note edits and undo now commit original identities, derived indexes,
retry receipts and history in a single native book transaction. Apple run `35966630474`
(`612105dd`) passed including rollback, stale-state and repeated-edit undo checks.
The native streaming transport now explicitly prepares an authoritative native book snapshot
and commits local actions before presenting events. It keeps its writer through streaming,
does not rebuild the request on reconnect, and never retries an uncertain local mutation.
This closes a bypass introduced by replacing window.fetch with URLSession. Its temporary
document adapter still handles the existing page-card saga; this is not full conversation
migration. No installable migration release has been published.

### Candidate work after `c4c2cebe` (not compiled or released)

The page-card saga, turn state/history coordination, PDF search/navigation and assistant
settings now have native implementations in the candidate working tree. The clear-history
barrier drains pending turn writes and only discards handles after an explicit successful
server acknowledgement. Native turn adapter/clear-boundary checks passed locally. Per the
user's instruction, subsequent full App builds are deferred until the code changes are ready
together; the successful Apple run `35967857219` does **not** validate these newer changes.

The composer uses UIKit marked-text state for Chinese IME Return handling instead of inferring
Send from a newline binding change. Paste, dictation and Shift-Return keep their distinct input
paths. Candidate media attachments sit above the composer, with format icons and image
thumbnails, using the existing attachment visual style. Images open the existing PencilKit
editor full-screen; exporting a marked-up attachment crops to the original image bounds and
creates a new attachment identity. Text is optional. Ordinary conversation images are added
as `localImage` input; the separate review conversation retains its destination and receives
file references. Original files are streamed into the server-managed `assistant-attachments`
directory (64 MiB/file, 10 files/message), with immutable upload ids and optional JPEG previews.
The sender persists message intent before dispatch and does not replay an uncertain image/file
submission as a new turn. Eight typed-input checks and 52 existing turn-container checks passed.
The actual realtime-completion caller is now connected to the native history writer (an
earlier candidate inserted that call in an unreachable persistence branch). Twelve focused
turn/clear/mode checks now cover the real completion entry, failed freeze/save, tab switches
and browser-host behavior. Current EPUB no longer has a separate per-book voice-history
override; that old comment was stale, not an additional active migration requirement.

These attachment routes require the matching ReaderPC server update before App rollout.
Neither server changes nor this candidate App have been installed. The attachment server's
C# Release compilation passed with no warnings. Swift compilation, actual photo picking,
Chinese keyboard interaction, Pencil editing and real delivery remain unverified. The broader
migration still contains conversation/artifact action adapters and a
review compatibility controller; this entry does not declare all hidden WebKit business logic
removed. EPUB body and localized video WebKit remain the user-approved exceptions. The voice
transport comparison is investigation only; no WebRTC/network migration is included here.

The review queue now owns answer visibility, staged-rating restoration, native card faces
and canonical refresh reconciliation. Opening a card's PDF/source link resolves its canonical
provenance in Swift; EPUB anchors stay with the retained EPUB renderer. Local queue reopen
keeps its cursor by stable identity while using newly read card content, and persists before
presenting. The composer also reserves photo-library import slots before asynchronous iCloud
exports, preventing Send from racing ahead of selected images. Software IME composition
confirmation is held through the current event loop, alongside hardware-key marked-text
handling. These changes join the same consolidated Apple verification; they are not an
installed release or proof that every compatibility adapter has been removed.

### Consolidated build and server integration, 2026-09-24

The attachment/composer/native-review batch passed all native checks and full App
compilation in Apple run `35990279682` (`b5e8456d`). Two compile errors were corrected:
the PhotosPicker SwiftUI overlay import and the shared PDF outline function's owner.
This is an unsigned build, not an installed App or TestFlight release.

The Windows candidate has been reconciled with the **actually installed** Jev runner,
artifact resend and trace handling, rather than replacing them with the older branch
copy. Their 63 checks and the existing typed-input/turn checks passed. ReaderPC 0.1.296
and Direct 0.1.440 passed packaged self-tests. Earlier 0.1.294/295 and 0.1.439 candidates
failed because the frozen parent exported expired Tcl/Tk paths to PyInstaller; both
build processes now discard those process-local paths. Failed candidates are not for
installation. Production services have not yet been switched by this entry.

The next native lookup batch removes ordinary word/phrase requests from the hidden
popup adapter. Swift preserves offline-first rich dictionary data, Chinese-only meaning
and examples, inflection/origin, live vocabulary/phrase state, bounded device cache and
the existing server/ReaderPC fallback. Dictionary inference uses a separate data socket;
it cannot occupy the audio channel. Stale server entries refresh only while the panel
is visible. Eighteen focused Reader checks passed; the new Swift checks and App build
remain pending. Supplemental dictionary actions, conversation/artifact effect adapters
and the review compatibility controller are still tracked migration work.

Apple run `35995805368` (`8cb78ab1`) subsequently passed all native checks and
full App compilation. No signed App or TestFlight release was produced.

The following candidate batch also routes related-word cards, contextual
explanations, Japanese deep explanations and dictionary Anki actions through
Swift. Related cards read live canonical notes across books and strip embedded
dictionary/script material, with revision-based invalidation and HTML parsing
off the UI actor. Explanation reconnects poll the original server job id rather
than resubmitting generation. Anki operations reserve a durable operation id
before sending and retain uncertain results across panel retries. The existing
authorized gateway and endpoints are retained. Twenty-one focused Reader
contracts passed; new native behavioral checks and full App compilation are
pending at that checkpoint. Apple run `35997315139` (`82f3a329`) subsequently
passed the native behavioral checks and full App compilation. The following
`fe5f9b2a` bounded related-card index has not yet had an Apple build.

### Installed server verification and remaining App work

ReaderPC `0.1.296` is installed; its local status endpoint reports that version
and the process runs from its release directory. Direct `0.1.440` is installed;
the canonical package verifier and installed executable self-tests pass. The
Direct installer was interrupted before producing its final install receipt;
these are independent recovery checks, not a reconstructed installer receipt.
The retained rollback snapshot is
`install-0.1.440-20260924T121344Z-a0efd264` in the existing Direct backup root.
No signed App or TestFlight migration release has been produced.

The current App batch moves learning-group drops and fact/general/weather/news
card drops onto the existing native note transaction. It reads canonical card
state, retains removed group slots and source identities, and uses complete
event originals rather than hidden card HTML or truncated previews. Original
inspection also uses native data. Review reveal/expand now updates the native
queue directly; compatibility code observes committed state without rendering
or repeating the operation. Source navigation and other review UI commands
share the native queue lease, fixing the prior hashed/raw context mismatch.
Focused conversation/review Node checks pass; the new Swift placement/lease
checks join the next consolidated Apple build and are not yet verified.

The following candidate moves media-card routes, video identity, map metadata,
selection/sibling exclusion and selection expiry to native data operations.
Image/video PDF drops serialize the complete originals through the native note
transaction. The App no longer renders semantic card bodies or initializes
hidden media/map widgets. The compatibility adapter observes committed
selection state and retains the outgoing-focus/removal notification contracts;
it does not create a second selection. Focused Reader checks pass; Swift media
and placement checks remain queued for the consolidated Apple build.

Remaining migration scope: original media event delivery and notification adapters;
conversation command orchestration and legacy selection adapters; review mode,
draft/effect coordination and account command outbox; PDF startup state and
ReaderPC context publication still provided by compatibility code. EPUB body
and localized video WebKit remain approved exceptions. This is not a release
candidate and must not be described as only waiting for packaging.

### Account command outbox handoff (candidate, not Apple-compiled)

App command capture, coalescing, exact-revision acknowledgements and rejected
records now use a native SQLite transport store, isolated from reclaimable
device caches and from learning-data replication. Accepted commands retain
small identity digests so a leftover WebKit spool cannot resurrect delivered
operations. The compatibility send API still returns its durable mutation ID
synchronously: its temporary spool is removed only after native import confirms
the exact record. Native-port failures retain that spool and do not switch to
the browser sender. Native batch receipts cannot erase later enqueues.

Reader Node checks: 2483 passed, including native-handoff disk failure, offline
retry and stale-account cases. Swift storage/receipt cases are added to the next
consolidated Apple build, not yet run. This does not finish migration: remaining
producers and the existing per-operation local/server dispatch adapter still
provide the compatibility transport entry; they must not be described as fully
Swift-owned. No App package or TestFlight upload was made.

### Native review selections and mode changes (candidate)

The native answer/paragraph buttons now apply an explicit selection state to
the Swift context graph. They validate the current queue lease and card identity
after pending registrations settle, without consulting a hidden message node.
Answer grouping, whole-answer coverage, segment order and expiry use that graph;
draft preparation waits for its committed state. The compatibility adapter reads
the resulting pairs rather than regrouping native selections. Stale mode
receipts cannot clear a newer draft; a current verbosity change invalidates the
native preview and its compatibility projection.

Reader Node checks: 2484 passed. Swift pair/coverage/expiry cases are queued for
the consolidated Apple build and are not yet run. Registration still comes from
the remaining conversation adapter; review mode/load/rating effect coordination,
conversation orchestration and startup/PC context integration remain unfinished.
No new App build, signed package or TestFlight upload is available.

### Native artifact projection and stream mutation gating (candidate)

Tool outcomes and semantic/Anki card display fields now come from original
events in Swift. The adapter supplies action handles and complete originals,
not another truncated web projection. Live learning-card records take precedence
over historical faces; multi-group messages resolve the requested gid instead
of whichever group is mounted first. Media indices and existing commands remain.

Native SSE batches now send only book mutation events to the document commit
adapter. Plain text, tool telemetry and completion bypass that extra round trip;
committed actions retain their original event positions and use a separate
mutation receipt sequence. An incomplete receipt stops delivery without replay.

Focused conversation/stream checks pass. Full Reader Node run had one failure
in the unchanged legacy POST retry test (fixed 90 ms wait); its focused rerun
passed. Swift projection/order checks are queued for the final consolidated
Apple build, not yet run. Conversation ordering/action registration, document
mutation adapters, review orchestration and startup/PC-context integration
still retain compatibility code. No new App package or TestFlight upload.

### PDF request preparation and direct document commits (candidate)

The native assistant stream now reads the PDF authority snapshot and source
characters directly. Swift builds optional card numbering and missing passage
text without web page/context reads or rasterization. Native visible markers
share its source-space numbering; the existing row/last-line ordering is stable
under viewport zoom. Missing geometry omits the complete numbering projection,
not the canonical notes or an invented anchor. The temporary web adapter only
holds the existing file-operation lease during the stream.

PDF action batches invoke the existing native book transaction and page-card
saga directly, retaining their original operation IDs and version checks.
Receipts update the compatibility observers; they cannot cause a second write.
Malformed batches, interrupted sessions and unknown receipts stop execution.

Review selection, score staging, undo and reveal commands resolve the visible
card in the native queue. The compatibility effects barrier drains earlier
work; reveal/navigation still submit the previous pending score first. A failed
flush does not reveal the next answer. Staging remains reversible and does not
submit a score; failed undo retains its stage for retry.

Focused and full Reader Node checks passed; generated vendor and offline App
resources are current. New Swift transaction/context/review checks are queued
for the final consolidated Apple build, not yet run. Remaining scope includes
conversation command/event registration, review effect/mode coordination,
PDF startup imports and ReaderPC context publication. EPUB body and localized
video WebKit remain approved exceptions. No signed App or TestFlight upload.

### Native review mode and improvement orchestration (candidate)

Review mode entry/exit, scope/reload and improvement buttons now enter Swift
directly. PDF context is read from its native document; EPUB source context
still comes from the retained body renderer. Swift owns bounded context/cache
keys, queue acquisition, rejected-score restoration and draft input from the
canonical card plus the native answer-selection graph. Web compatibility code
only drains existing score effects and observes results; App mode notifications
cannot trigger another queue acquisition. Older/browser hosts retain their own
path. A pending reversible score cannot be discarded by a queue reload.

Frozen preview confirmation and durable unknown-write receipts are unchanged.
Stale card/mode responses do not replace newer previews or queues. Focused and
full Reader Node checks passed; vendor and offline resources regenerated. Added
Swift context/queue cases remain queued for the final consolidated Apple build.
Review score effects/deletion, conversation ownership, account dispatch and
startup/context publishing remain on the fixed closeout list above. No signed
App package, Apple compile or TestFlight upload was performed in this batch.
