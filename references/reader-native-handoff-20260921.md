# Reader 原生迁移交接 · 2026-09-21

> 本文件在 2026-09-21 下午被改写过一次。上半场（`acdf0d26` 之前）的记录见
> git 历史；这里写的是**现在的状态**。

## 先读这些事实

- 用户目标：用 Apple 原生能力实现原 Reader 的全部功能；视觉统一为股票 App 的风格，
  保留原交互逻辑、账户、书籍、卡片、批注、学习记录和语音能力。
- **用户 2026-09-21 下午定的两条**：
  ① 「旧的和新的一起使用经常会崩溃」→ **终态是把旧的全去掉**，不是两套并存；
  ② 「原生化全部做完后出包」→ 中间批次一律 `compile_only`，**不出 TestFlight 包**。
- 阶段版的发布阻塞（Apple 登录后台没部署）**已解除**：线上
  `https://bwicarus-2.taile44d0c.ts.net/login/apple/status` 返回 200，5 条路由全注册，
  三张表已建，原账户未动。签名链路也第一次真正跑通过（run 35580043980：
  Sign in with Apple capability → 归档 → 校验 → TestFlight 上传全部成功）。

## 工作位置与回退

- Windows 工作树：`C:\tmp\reader-native-shell-20260921`
- 分支：`codex/reader-fully-native-20260921`
- 原始回退标签：`reader-native-baseline-20260921-9078aecf`
- 线上 Windows 服务工作树：`C:\tmp\reader-card-anchor-release`。
  ⚠ 那棵树里 `_server_deploy/app.py` + `reader_apple_auth.py` 有**未提交的改动**
  （就是上面那次 Apple 登录部署）。记录它的 commit 被权限拦下了，文件已生效。

## 现在能用的（原生 PDF 主阅读区）

开关在 **阅读设置 → 原生阅读区（迁移中）**，`@AppStorage("reader.nativePDFRenderer")`，
**默认关**。打开后 `ReaderWebView` 留在层级里当数据层（opacity 0、不接触摸），
正文由 PDFKit 画。

| 能力 | 做法 |
|---|---|
| 挂载 | prepare → 发布给 SwiftUI 布局 → `onGeometry` 触发 activate（attach 要求 bounds>0） |
| 位置/翻页/布局/缩放/去边 | 既有导航桥；位置仍由网页那条 JS 路径持久化 |
| 高亮/墨迹/便签显示 | 实时投影：钩在 `withNativePDFWriter` 成功分支（App 所有 PDF 用户状态写入的唯一咽喉） |
| 选区 → 划线 | 原生自算点坐标，直接 `savePDFHighlight` 落库，**不经网页渲染** |
| 选区 → 查词/翻译 | 原生面板；语言路由仍在 `_isJaWord` 一处（`__bwReaderLookupData`） |
| AI 精确划线 | 经 `bwNativeReaderGeometry` 问原生字符层要坐标；拿不到才退回网页 |
| 页卡 | 显示/拖动/改大小三处都走 `noteGeometry` + `canonicalPoint`，PATCH `/pdf/api/notes` |
| Pencil | 墨迹表面由原生按可见页发布（`__bwNativeInkSurfaces`），id 仍是 `page:N` |
| 生词下划线 / 振假名 / 生词句子 / 搜索命中 | 原生画；判据仍在网页一处（`_vocabMarksForDisplay` 等），经 `__bwReaderPageOverlay` 一次取数 |
| 真实改页 | 内容摘要变了会重挂原生文档（不重挂＝静默停更） |

**崩溃的止血**：原生接管时网页层不再批量渲染（IntersectionObserver 整批早返回、
首屏也不渲但遮罩照撤）。按需渲单页的路保留给还依赖 `__charBoxes` 的地方。

## 还没做的

1. **EPUB 主体。** ⚠ 动手前先想清楚「原生化 EPUB」是什么意思：EPUB 正文是 XHTML，
   Apple 没有对应 PDFKit 的渲染器，所以它**只能**是 WKWebView。可做的是把它的
   选区菜单/查词/划线/叠加层按 PDF 那套搬过去，而不是换渲染器。
2. **短语/解释的临时高亮**（查词时正文上那圈呼吸高亮）。
3. **图徽标 / 插图描述**、整页翻译的译文层、语法分析面板。
4. **把旧的删掉**：目前网页层仍承担 IndexedDB 存储、对话上下文、EPUB 正文。
   真正"删掉"要先把存储层搬到 Swift，那是另一件大工程。

## 两条经过验证的工作纪律

- **本机没有 Swift 编译器**，契约测试只看文本 —— Swift 的真实语法只有 CI 会说话。
  每批改完跑 `compile_only`，**等它出结果再往下叠**（有两批是连着红了才发现的）。
- **契约测试里凡是按位置/字面量比对的，先剥注释**。说明「这道闸为什么存在」必然
  要提到被挡的那个符号，照字面比会把注释当代码 —— 这个坑一天踩了三次。

## 反复出现的一个模式

这一轮大半工作不是"写新功能"，而是**把已经写好却从没被调用的原生组件接上**：
`ReaderNativePDFViewport`、`noteGeometry` 都曾是全仓库零引用。所以"编译通过"
和"屏幕上能用"之间一直隔着接线这一步 —— 看到某个原生能力"已实现"时，
先搜一下有没有调用方。

## 代码入口

- `ios/BWReader/App/BWReaderNativeApp.swift`：主视图挂载点、两个开关
- `ReaderWebView.swift`：挂载/重挂、叠加数据取数、划线/页卡/墨迹表面的壳侧
- `ReaderNativePDFDocument.swift`：PDFKit 文档 + 所有原生绘制（Canvas）
- `ReaderNativeConversationScript.swift`：注入脚本里的命令分发（⚠ 新命令要同时登记
  脚本里的 `parameterKeys` 和 `ReaderWebView` 的 `allowed` 两道闸）
- `reader.src/08-charlayer.js`：`__bwReaderPageOverlay`（叠加数据唯一入口）
- `reader.src/17-highlight.js`：`_nativeExactHighlight`（AI 划线的原生分支）
- `pdf-tail.js`：墨迹表面在接管时改用原生矩形
