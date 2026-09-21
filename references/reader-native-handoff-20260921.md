# Reader 原生迁移交接 · 2026-09-21

> 这份文件当天被改写过两次。上半场的记录见 git 历史；这里写的是**现在的状态**。

## 先读这些事实

- 用户目标：用 Apple 原生能力实现原 Reader 的全部功能；视觉统一为股票 App 的风格，
  保留原交互逻辑、账户、书籍、卡片、批注、学习记录和语音能力。
- **用户 2026-09-21 定的两条**：
  ① 「旧的和新的一起使用经常会崩溃」→ **终态是把旧的全去掉**，不是两套并存；
  ② 「原生化全部做完后出包」→ 中间批次一律 `compile_only`，**不出 TestFlight 包**。
- 阶段版的发布阻塞（Apple 登录后台没部署）**已解除**：线上
  `https://bwicarus-2.taile44d0c.ts.net/login/apple/status` 返回 200，5 条路由全注册，
  三张表已建，原账户未动。签名链路也真正跑通过一次（run 35580043980）。

## 工作位置与回退

- Windows 工作树：`C:\tmp\reader-native-shell-20260921`
- 分支：`codex/reader-fully-native-20260921`
- 原始回退标签：`reader-native-baseline-20260921-9078aecf`
- 线上 Windows 服务工作树：`C:\tmp\reader-card-anchor-release`。
  ⚠ 那棵树里 `_server_deploy/app.py` + `reader_apple_auth.py` 有**未提交的改动**
  （就是那次 Apple 登录部署）。记录它的 commit 被权限拦下了，文件已生效。

## 这一轮真正学到的一件事

大半工作不是"写新功能"，而是**把接管后断掉的那一半接回来**。断法总是同一个形状：

> 动作还在（顶栏按钮点得到、菜单项还在），**结果看不见**；而说明失败的那句
> toast 也在被藏起来的那一层里，于是**什么都不发生，也没有任何线索**。

出现过的实例：整页翻译（`_pageTranslateApplyAll` 只处理 `[data-loaded="1"]` 的页）、
图徽标（`.fig-layer` 没有宿主）、划线编辑（`.hl-layer` 不存在 → 划得上去改不了删不掉）、
新建便签（`document.elementFromPoint` 七个候选点全落空）、语法分析（要
`_charSel.pw.__charBoxes`）。**看到某个能力"已经实现"时，先问它的结果画在哪一层。**

对应的两条做法，本轮一直照着做：

1. **判据留在网页一处，原生只画 / 只发起。** 开关（`__figBookOn`、`_pageTrOn`、
   `_enabledBooks`/`_hasTracked`）、语言分流（`_isJaWord`）、色板与「取消颜色」语义、
   收藏归一化 —— 复制到原生必然漂移，表现是两个表面对同一个词/同一条划线给出
   不同结果。
2. **原生路径的失败必须自己出声**（`ReaderNativeConversationModel.report`）。

## 现在能用的（原生 PDF 主阅读区）

开关在 **阅读设置 → 原生阅读区（迁移中）**，`@AppStorage("reader.nativePDFRenderer")`，
**默认关**。打开后 `ReaderWebView` 留在层级里当数据层（opacity 0、不接触摸），
正文由 PDFKit 画。⚠ 「显示旧界面」时原生正文会让开 —— 否则是个没有出路的空白屏。

| 能力 | 做法 |
|---|---|
| 挂载 | prepare → 发布给 SwiftUI 布局 → `onGeometry` 触发 activate（attach 要求 bounds>0） |
| 位置/翻页/布局/缩放/去边 | 既有导航桥；位置仍由网页那条 JS 路径持久化 |
| 高亮/墨迹/便签显示 | 实时投影：钩在 `withNativePDFWriter` 成功分支（App 所有 PDF 用户状态写入的唯一咽喉） |
| 选区 → 划线 | 原生自算点坐标，直接 `savePDFHighlight` 落库，**不经网页渲染** |
| **点划线 → 编辑** | 改色 / 备注 / 删除，走底座 `_hlUpdate`/`_hlDelete`；无色划线画虚框 |
| 选区 → 查词 / 展开完整词典 / 翻译 / **解释** / **词组** | 原生面板；语言路由与端点都在 `__bwReaderLookupData` 一处 |
| 选区 → **语法** | `RC.grammar.analyzeData`（新加）：同两条端点、同一套前置，交出结构化结果 |
| 选区 → 对话 | 已经通：`nativePageSelection` 会写 `window.__focusSel`，助手看得见 |
| AI 精确划线 | 经 `bwNativeReaderGeometry` 问原生字符层要坐标；拿不到才退回网页 |
| 页卡 | 显示/拖动/改大小三处都走 `noteGeometry` + `canonicalPoint`，PATCH `/pdf/api/notes` |
| Pencil | 墨迹表面由原生按可见页发布（`__bwNativeInkSurfaces`），id 仍是 `page:N` |
| 生词下划线 / 振假名 / 生词句子 / 搜索命中 | 原生画；判据在网页一处，经 `__bwReaderPageOverlay` 一次取数 |
| **整页翻译（译页）** | 切片 `_pageTranslateSlices` 用点坐标算一次，DOM 与原生各自缩放 |
| **图徽标 / 插图描述 / 带入助手** | 原生画徽标与绿框；带入权威仍是 `window.__figAttached` |
| **新建便签** | 落点由 PDFKit 给（页号 + 归一点），落库走 `RC.stickynote.createAt` |
| 顶栏「阅读工具」 | 点的是网页工具栏按钮；`liveAction` 成功后统一重取一次叠加数据 |
| 真实改页 | 内容摘要变了会重挂原生文档（不重挂＝静默停更） |

**崩溃的止血**：原生接管时网页层不再批量渲染（IntersectionObserver 整批早返回、
首屏也不渲但遮罩照撤）。按需渲单页的路保留给还依赖 `__charBoxes` 的地方。

## 存储与 iCloud 同步（2026-09-21 下午起）

用户要求：「能使用 iCloud 同账号不同设备同步数据是最好的」。选型与调研见
[`reader-native-storage-and-epub-20260921.md`](reader-native-storage-and-epub-20260921.md)。
结论是 **CKSyncEngine**（本地存储仍归我们，它只管同步管线）。

已经落地的两块：

| 块 | 文件 | 状态 |
|---|---|---|
| 三方合并规则 | `reader-runtime/user-state-merge.js` | ✅ node 里 14 条用例真的在跑 |
| 合并器（JSCore 壳） | `ios/.../ReaderUserStateMerge.swift` | ✅ 打包器原样烤进包并逐字校验 |
| 同步引擎 | `ios/.../ReaderCloudUserStateSync.swift` | ✅ 已接线（桥/开关/标脏/开书即合） |

**代码侧已经齐了**（2026-09-22）：桥、开关（阅读设置 → 同步，默认关）、
本地写入标脏、开书即合待处理、EPUB 写入也标脏。容器是用户当天在后台建的
**`iCloud.BWICARUS`**（描述 "READER"），entitlements 已加，profiles 已重下。

⚠ **后台那一项必须选「Include CloudKit support」**：「Compatible with Xcode 5」
只给 iCloud Documents / key-value store，不带 CloudKit，而我们用的是
CKSyncEngine，选错就完全用不了。（括号里的 Xcode 版本是 2014 年的历史包袱。）

**还没验证的**：真机上两台设备互相同步。`compile_only` 是
`CODE_SIGNING_ALLOWED=NO` 的无签名构建，验不到 entitlements —— 所以加完
entitlements 跑了一次 `compile_only=false upload=false`（签名+归档+校验，
**不上传**）来验签名链路。真正的收敛还要等一次装到设备上的构建。

设计上已经定死、改之前先想清楚的两条：
- **跨设备的书籍身份 = 内容摘要**，不是 localBookId。本机导入的书在每台设备上
  id 都不同，而同一个 PDF 的 sha256 一样。字节不同就是不同的书：高亮锚在页坐标
  上，硬迁过去只会错位。
- **域负载用 CKAsset**，不是字段。墨迹很容易超过单字段 1MB，写成字段会在真实的
  书上炸 —— 而且是在最重度的那本书上。

✅ **用户 2026-09-22 拍板**：iCloud 管 Apple↔Apple、**Windows 只收单向留底**。
由此定下两条 —— Windows 那条 outbox/sync-batch 的角色是留底**不是仲裁者**
（别再往它上面加"谁更新"的判断）；也**不要**把 iCloud 的合并结果再推一份给
Windows 当"同步"，推回去就又是双写。

## 还没做的

1. **EPUB 的其余部分。** ⚠ 先想清楚「原生化 EPUB」是什么：EPUB 正文是 XHTML，
   Apple 没有对应 PDFKit 的渲染器 —— 业界（Readium）也是 web view 渲正文、外面
   包一层原生。所以**不换渲染器**。
   已经做了：**选区操作条换成原生**（查词/词组/翻译/解释/语法，与 PDF 选区菜单
   同一组动作、同样顺序），取数口 `__bwReaderLookupData` 与 PDF **同名同形状**，
   壳那段代码不关心自己站在哪个阅读器上。
   ⚠ **不要再试系统的编辑菜单**：`UIEditMenuInteractionAnimating` 只有
   `addAnimations`/`addCompletion`，是纯动画协议加不了项；WKWebView 的编辑菜单
   也没有稳妥的公开路子让宿主插项（`buildMenu(with:)` 管的是菜单栏与上下文菜单）。
   2026-09-22 为此红过一轮 CI。现在是自己画的一条 SwiftUI 条，完全可控。
   划线（新建）也已经在条上：色板与网页工具栏同一份来源，落库走底座 `saveHl`，
   锚点用 `captureSel` 对齐过的 `cur.anchor`（重算一次就会和用户看见的选中范围
   差几个字）。
   ⚠ 条读的是 `readerSelectionText` 而**不是** `selectionText`：后者来自
   `__focusSel`，而 `__setFocusSel` 第一行就是「助手侧栏没开就 return」——
   用它的话侧栏关着时条永远不出现，且没有任何线索。
   还没做：**点已有划线去编辑**（PDF 那侧有，EPUB 还没）、叠加层（生词下划线/
   振假名等仍由网页画，而 EPUB 的网页层是可见的，所以**没有坏**，只是不统一）。
2. **把旧的删掉**：网页层仍承担 IndexedDB 存储、对话上下文、EPUB 正文。存储那条
   已经动起来了（见上一节），但"删掉"要等调用方也搬完，不在本轮范围内。

### 已经处理掉、别再当待办的两条

- **选区 OCR** ✅ 做了，但**走的是 App 自己那套**：`/pdf/api/ocr-selection` 在
  App 内由本地 runtime 接管（`NativeBookOCRBridge`），这条 fetch 根本不出网。
  ⚠ 这里原本记的是"别做"，因为直连服务端那套只会校正"刚选中的那段文字"，
  页面字符层还是旧的（App 的字符层来自本机 OCR 存储，不读服务端的 `cv`）——
  一个看起来成功、下次选还是错的假修复。走本地那条就没这个问题。
- **短语/解释的临时高亮**：原生这边**不需要**。网页那圈呼吸高亮是"查询进行中"
  的等待指示（结果框是另开的，正文上得有个东西告诉你在查哪一段）；原生是直接弹
  面板，面板自带 ProgressView，选区也一直亮着。补一个只是把网页的形态搬过来。

## 经过验证的工作纪律

- **本机没有 Swift 编译器**，契约测试只看文本 —— Swift 的真实语法只有 CI 会说话。
  每批改完跑 `compile_only`，**等它出结果再往下叠**。
- **一串 `min/max` 嵌在 `.position` 里会让 Swift 直接放弃类型检查**
  （unable to type-check this expression in reasonable time）。位置算法拆成具名步骤。
- **契约测试里凡是按位置/字面量比对的，先剥注释**。说明「这道闸为什么存在」必然要
  提到被挡的那个符号，照字面比会把注释当代码。
- **`body(src, from, to)` 这种切片要确认 `to` 在 `from` 之后**：文件里常有更早的同名
  片段，切出来是空串，于是断言"通过"得毫无意义（本轮被咬过两次）。
- **新 fetch 要有注册过的交互 id**，否则网络审计判成新增债务；改动既有 fetch 的位置/
  调用者也算新债，顺手把它登记掉即可（本轮 201 → 197）。
- **Swift 文件里 CRLF/LF 是混的**，批量替换前按实际内容判断，别整文件一刀切。
- **heredoc 会吃掉反斜杠**（`\n`、`\u3000`、`\s` 都中过招）。写含转义的代码用 Edit，
  或写完立刻 grep 确认。
- **不确定的系统 API 先查文档再写**。2026-09-22 凭印象用了
  `UIEditMenuInteractionAnimating.addMenuElement`（根本不存在，那是个纯动画协议），
  红一轮 CI 才发现 —— 而本机没有 Swift 编译器，每次猜错的代价就是一整轮。
  查一次两分钟。
- **`@Published` 这类存储属性只能待在类主体里**，extension 里放会直接编译失败
  （extensions must not contain stored properties）。方法放 extension 没问题 ——
  同一天因为这个又红了一轮：往 `WKUIDelegate` 那个 extension 里加功能时，
  顺手把状态也写在了旁边。
- 一天两次「猜一下 → 一轮 CI」，合起来的教训是：**改 Swift 时，凡是自己没有
  十成把握的语言规则或系统 API，先查**。契约测试挡不住这类错（它只看文本），
  本机也编译不了，CI 是唯一的判官而它很慢。

## 代码入口

- `ios/BWReader/App/BWReaderNativeApp.swift`：主视图挂载点、两个开关、`nativePDFSurfaceActive`
- `ReaderWebView.swift`：挂载/重挂、叠加数据取数、各原生面板的打开、划线/页卡/墨迹表面的壳侧
- `ReaderNativePDFDocument.swift`：PDFKit 文档 + 所有原生绘制（Canvas）+ 选区菜单 + 图徽标
- `ReaderNativeLookupView.swift` / `ReaderNativeGrammarView.swift` /
  `ReaderNativeFigureView.swift` / `ReaderNativeHighlightEditor.swift`：四个原生面板
- `ReaderNativeConversationScript.swift`：注入脚本里的命令分发（⚠ 新命令要同时登记
  脚本里的 `parameterKeys` 和 `ReaderWebView` 的 `allowed` 两道闸）
- `reader.src/08-charlayer.js`：`__bwReaderPageOverlay`（叠加数据唯一入口）
- `reader.src/10-pagetranslate.js`：`_pageTranslateSlices` / `__bwReaderPageTranslateSlices`
- `reader.src/15-phrase-wordpop.js`：`__bwReaderLookupData`（查词/展开/翻译/解释/词组）、
  `__bwReaderMarkVocab`、`__bwReaderPhraseFav`、`__bwReaderCreateNote`
- `reader.src/17-highlight.js`：`_nativeExactHighlight`（AI 划线的原生分支）
- `reader.src/19-dict.js`：`__bwReaderHighlightsOnPage` / `__bwReaderHighlightEdit`
- `reader.src/26-figures.js`：`__bwReaderPageFigures` / `__bwReaderFigureAttach`
- `rc-grammar.js`：`analyzeData`（结构化语法结果）、`saveHistoryItem`（两个表面共用历史）
- `pdf-tail.js`：墨迹表面在接管时改用原生矩形
