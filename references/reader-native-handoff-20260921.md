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

## 还没做的

1. **EPUB 主体。** ⚠ 先想清楚「原生化 EPUB」是什么：EPUB 正文是 XHTML，Apple 没有
   对应 PDFKit 的渲染器，所以它**只能**是 WKWebView。能做的是把选区菜单/查词/划线/
   叠加层按 PDF 那套搬过去，而不是换渲染器。目前 EPUB 仍整个用网页 UI（它是可见的，
   所以**没有坏**，只是不统一）。
2. **选区 OCR**（文字层坏掉时重新识别）。⚠ 别直接把 `/pdf/api/ocr-selection` 接过来：
   App 的字符层来自 `NativeBookOCRManager.readerPageCharacters`（本机 OCR 存储），
   **不读**服务端那套 `cv` 版本号。照搬只会校正"你刚选中的那段文字"，页面的字符层
   还是旧的 —— 一个看起来成功、下次选还是错的假修复。要做就走 App 自己的 OCR。
3. **短语/解释的临时高亮**（查词时正文上那圈呼吸高亮）。
4. **把旧的删掉**：网页层仍承担 IndexedDB 存储、对话上下文、EPUB 正文。真正"删掉"
   要先把存储层搬到 Swift，那是另一件大工程，不在本轮范围内。

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
