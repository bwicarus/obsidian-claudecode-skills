# Reader 原生迁移交接 · 2026-09-21

## 先读这些事实

- 用户目标：用 Apple 原生能力实现原 Reader 的全部功能；视觉统一为股票 App 的风格，保留原交互逻辑、账户、书籍、卡片、批注、学习记录和语音能力。不要用“切换到旧版功能”入口替代迁移。
- 最新关注：额度即将用完，希望其他 AI 接手，并询问能否先发布已完成部分。用户尚未明确要求立即执行这次阶段发布；上一回复结论是可以准备阶段测试版，但必须先解决登录阻塞。
- **没有完成完整迁移。主阅读区仍然是 ReaderWebView；PDFKit 组件尚未挂载。编译成功不能写成迁移完成。**
- **开发版已换成 Apple 登录页，但在线登录后台没有部署。2026-09-21 最后一次只读检查 `/login/apple/status` 返回 HTTP 404。不能把当前分支直接上传让用户安装，否则可能无法登录。**
- 代码全部已提交；最后一个代码提交 `f54b0a5954f5c0c23847845b192d056e5dc4f44b`。此交接文件是之后的文档变更。

## 工作位置与回退

- Windows 工作树：`C:\tmp\reader-native-shell-20260921`
- 分支：`codex/reader-fully-native-20260921`
- GitHub：`https://github.com/bwicarus/obsidian-claudecode-skills.git`，上述代码提交已推送。
- 原始回退标签：`reader-native-baseline-20260921-9078aecf`，仍存在。
- 线上 Windows 服务工作树：`C:\tmp\reader-card-anchor-release`，此前检查无本任务改动。**不要直接把整个原生开发分支覆盖到线上。**
- Reader 账户服务器：`https://bwicarus-2.taile44d0c.ts.net`。这一部署在 Windows，不是旧 Pi 路线；先读 `references/deployment-workflow.md`。
- 服务监视 Python 源码，直接修改线上文件会触发重载；发布前准备好完整补丁及回退，避免逐文件写入让服务反复加载半套代码。

## 已完成的开发内容

详细过程及之前功能见 `references/reader-native-migration.md`。以下均是开发分支状态：

- 原生导航、AI 侧栏、流式消息、标准生成物/Anki 等部分原生卡片、选择内容 chip、部分设置和阅读工具。
- 原生富文本/表格及卡片内图片。图片使用原资源代理和作用域化资源 ID，原卡更新后旧请求失效；不是任意 HTML/JavaScript 都已原生化。
- Apple 登录前后端：AuthenticationServices 挑战/nonce/state 校验；首次绑定原账户，不按邮箱自动合并，不换原 user ID。成功后同步原 Safari 共享设备令牌。
- 原生 PDFKit 组件：原件安全访问、OCR 字符选区、原算法卡片文字锚点、墨迹/高亮/笔记只读投影、旋转坐标、页内位置、单页/连续/双页、缩放和宽度适应。
- PDF 原生导航桥：既有前后页/跳页/模式/缩放命令转入 PDFKit；实际位置回到原持久化与 AI 上下文。视口令牌、书 ID、内容摘要、递增序号拒绝换书/迟到回调；180ms 事件合并，不轮询。旧网页排队滚动回调不得覆盖原生页码。
- 原生裁边：沿用原百分比设置，只修改内存显示文档的 artBox；原件文件和 cropBox/批注坐标不变。Core Graphics 变换处理旋转及非零原点，墨迹按显示框裁剪。
- 原有 Pencil 写入改为等待持久回执；按书页串行快照保存，回收旧页面后重试不写入空笔迹。主 PDFKit 上的 Pencil 几何/提交端口仍未接齐。
- 修复打包审计把共享政策声明误认作实际请求的问题；没有用新增逐条豁免绕过审计。

## 验证与发布状态

- `94d96fe9` → CI `35571466354` 成功。
- `92acbba2` → CI `35573149419` 成功。
- **`f54b0a59` → CI `35573878770` 成功**：`https://github.com/bwicarus/obsidian-claudecode-skills/actions/runs/35573878770`
- 最新 CI 包含实际 PDFKit 裁边检查：0/90/180/270 度、非零 cropBox 原点、原件坐标保留、非法输入，以及 iOS 模拟器编译。
- 定向导航/裁边行为 8 项通过；原聊天侧栏 Chromium 集成 10 项通过；上一批离线 ReaderBundle 315 文件打包通过，最新 CI 也已完成打包。
- **这些 CI 都是 `compile_only=true, upload=false`，没有签名归档或上传 TestFlight，没有真实 Apple 登录验证，没有 iPad/Pencil 实机验收。**
- 只编译请同时指定 `compile_only=true`；仅 `upload=false` 仍可能签名归档。
- 构建入口：`.github/workflows/safari-extension-ios.yml`。签名能力/描述文件准备步骤已写，但 Apple 登录 capability 尚未实际执行。

## 发布已完成部分前的最小收尾

1. 检查 `ReaderPiLoginView.swift`、`ReaderAppleSignIn.swift` 与 `_server_deploy/reader_apple_auth.py`；读 `tests/test_reader_apple_auth.py`。登录页现在依赖新后台，不能漏部署。
2. 根据正式 Windows 部署说明抽取 Apple 登录所需模块、app 注册、数据库 schema 与依赖；准备备份和回退，不复制整棵开发目录覆盖线上。
3. 配置/核验应用签名的 Sign in with Apple capability 与描述文件；现应用 Bundle ID 为 `space.bwicarus.bwreader2`，复用既有发布渠道。
4. 验证原账户关联、原数据仍在、重登与共享令牌；需要用户参与的 Apple 身份验证交给用户，不能声称模拟器编译验证过真实登录。
5. 明确阶段版边界：已完成原生 UI 改进先发布，主阅读区暂时仍是当前实现；不要默认启用未接完功能的 PDFKit 组件。
6. 获得本次发布明确指令后再签名上传，不再顺便扩充迁移范围。不要把现有 App Store Connect 浏览器登录状态当成发布凭证；优先用已配置的上传/API 渠道。

## 完整迁移的真实剩余工作

### PDF/EPUB 主体

- `BWReaderNativeApp.swift` 当前仍显示 `ReaderWebView(model:)`。
- `ReaderWebViewModel.prepareNativePDFDocument()`、`activateNativePDFDocument(_:)` 已实现，但主界面没有调用/挂载它们。
- 在接齐下述功能前不要直接切换 renderer：卡片位置与拖放/大小/锁定、Pencil 页面采样和保存、图像与选区操作、原插页与页内内容、当前页生成物、阅读状态变更的原生实时投影。
- EPUB 主体尚未完整迁移；不是 PDFKit 完成就代表 Reader 全部完成。

### 原有交互

- 原生 OCR 自定义选区菜单目前主要是复制/选整句；原有查词、翻译、解释、OCR 修正、搜索、语法分析、高亮编辑等操作尚需逐项原生接线。
- 原生 PDF 的 notes/ink/highlights 是原件只读投影，尚需把后续保存/导入/AI 工具变更及时投影回原生视图。
- 页卡当前仍依赖既有 JS 的 liveAction/放置数据，部分几何来自旧 DOM；切主阅读区前必须替换坐标来源并保留原卡 ID、状态、私有笔迹与绑定。
- 已有 HTML/JavaScript 交互生成物保留原件数据，逐类重做；不能删除，也不能宣称原生已支持所有类型。
- 查词/语法/知识点/查询历史/结果面板等完整原生页面仍需补齐。
- 最后按实际使用链验证：选词→查词/制卡→拖放绑定→Pencil 标记→语音注入→关闭侧栏显示生成物→切书/重启→数据恢复。减少零散编译与重复测试。

## 代码入口

- `ios/BWReader/App/BWReaderNativeApp.swift`：主视图挂载位置。
- `ReaderWebView.swift`：原生桥接、当前原件身份、prepare/activate/invalidate、选区和截图入口。
- `ReaderNativePDFDocument.swift`、`ReaderNativePDFCrop.swift`、`ReaderNativePDFNavigationBridge.swift`：PDF 原生组件和裁边/导航。
- `ReaderNativePDFSelection.swift`、`NativePDFSelectionCore.js`：JavaScriptCore 执行抽取的原始选区/绑定算法，不是 DOM renderer。
- `ReaderNativeConversationScript.swift`、`ReaderNativeConversationModel.swift`、`ReaderNativeConversationView.swift`、`ReaderNativePageCards.swift`：侧栏和页卡语义桥、原生显示。
- `ReaderBookUserStateWebAdapter.swift`：原件状态包导出/导入，继续沿用原数据所有者。
- `_server_deploy/static/pdf/reader.src/02-position.js`、`03-loader.js`、`04-render.js`、`05-nav.js`、`06-layout.js`、`07-continuous.js`、`21-misc-ai.js`：原有导航/裁边/位置与原生接管入口。
- `reader.src/17-highlight.js`：原高亮保存及原 `__BW_READER_RUNTIME__.savePDFHighlight` 原子写入，可供后续原生高亮接线参考。

## 省额度与工作规则

- 先读本文件，不需要重读数百条对话或全仓库。用户现在最关心额度与可交付版本。
- 不自行开新 agent、持续目标或自动化；本轮没有建立这些。
- 不把编译通过说成实机验证；不把未挂载的原生组件说成已经上线。
- 每个有意义的集成批次编译一次，等待工具/脚本完成通知；不要让模型频繁查询同一构建。
- 修改 `reader.src` 后只用 `scripts/build_pdf_reader_js.sh` 生成 `reader.js`。
- 扩展 vendor 只用 `extensions/bw-reader-webext/build.py`；生成器可能把 rc-phrasepop/rc-wordpop 的 CRLF 改为 LF，确认纯行尾变化后才能只还原那两个文件。
- 不 reset/clean，不覆盖其他 AI 的改动。写之前 `git status`；路径使用显式 workdir。
- PowerShell 下不要把 `Reader*.swift` 这类通配符作为 rg 路径；对目录使用 `-g '*.swift'` 或先 `rg --files`。
- Python 需 `PYTHONUTF8=1`；机器 Python 为 `C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`，gh 为 `C:\Users\bwica\scoop\shims\gh.exe`。

## 给接手 AI 的短指令

> 请先读取 references/reader-native-handoff-20260921.md。接续 codex/reader-fully-native-20260921 分支。用户希望完整原生迁移，但额度紧张，当前优先评估/完成已做好部分的阶段版发布收尾；Apple 登录后台尚未上线，不能直接上传现分支。保留所有原数据和原功能，不启动未接齐的 PDFKit 主阅读区。不要重复扫描全仓库或频繁编译；每步明确区分代码、编译、实机与发布状态。
