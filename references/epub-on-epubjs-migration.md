# EPUB 阅读器迁移到 epub.js 成熟地基(2026-06-27 起)

> ## ⚠️ 历史文档:本迁移的前进方向已废弃(2026-07 更新,读之前先看这段)
>
> 本文写于「打算把 EPUB 阅读器迁到 epub.js 成熟地基、手搓版搬齐后退役」的时期。**那个方向后来反转、已废**:
> - **正主 = 手搓主文档版 `epub-html.js`**(把 EPUB 渲进主文档那条,266KB,`epub_html_reader.html`)——它是**默认 EPUB 阅读器**(`pdf_reader.py::epub_view` 默认分支),布局不抖、tap 坑已治,收藏夹物化 EPUB / 插入页 / 便签等新功能全建在它上,近期开发**全在这条**。
> - **epub.js 版(`epub2*.js` + `?engine=epubjs` → `epub_reader.html`)= 已于 2026-07-06 整线删除**（commit 4d475c79「删除 epub.js 退役全线(12文件~250KB)」）：代码、模板、`?engine=epubjs` 分支都不存在了，`pdf_reader.py::epub_view` 直接渲 `epub_html_reader.html`。**下方「地基(已就位)」「移植阶段 P1-P10」「全部完成清单」全是那条已删分支的历史记录——别去找这些文件、也别照它改**；只有「iframe 底座坑」一节作为 epub.js 通用踩坑留档（`vendor/epub.min.js` 仍在盘上但全仓零引用）。
> - 因此本文下方的「移植阶段 P1-P10 / 全搬计划 / 全部完成清单」都是 epub.js 那条**休眠分支**的记录;**新 session 别据此以为「默认要切 epub.js」或去动 epub.js 那套**。真正在维护的 EPUB 代码是 `epub-html.js`(+ 共享 `rc-*.js`,见 [`unified-control-layer.md`](unified-control-layer.md))。
> - **仍有价值的部分**(仅当有人真去碰休眠的 epub.js 分支时):下方的 **iframe 底座坑**(iOS 单击文字不派发任何事件 → 折叠光标轮询兜底 / CFI 锚 / 选区桥接 / 装饰注 iframe document)是硬核踩坑,epub.js 特有,留档不删。

## 为什么
手搓 reflow 阅读器(`epub-html.js` 把 EPUB 渲进主文档、自己处理分页/布局/选区/所有事件)**一直冒布局 + tap 失灵的坑**(开抽屉盖顶栏、感叹号/设置关闭偶发失灵、iOS tap 阻塞)。根因:分页/布局/选区这些**本该用成熟库**(epub.js)解决,手搓必然到处踩坑。用户拍板:换 epub.js,**但功能一个不能少,而且从 PDF 阅读器最原始的 `reader.src/*.js` 搬**(不搬已 drift 的手搓代码)。

## 地基(已就位,别重造)
- **epub.js + jszip** 已 vendored:`/var/www/html/static/pdf/vendor/{epub.min.js,jszip.min.js}`。
- 后端把 .epub 解包到 `state/epub-extract/<sha>/`,用 `/pdf/epub/file/<sha>/<subpath>` 服务给 epub.js 懒加载(`_ensure_epub_extracted` + `_epub_opf_info` OPF 缓存防 490ms 堵 worker)。
- **`epub-reader.js`**(6.5KB,干净)= epub.js 渲染地基:`ePub(base)` + `renderTo('ep-viewer', {manager:'continuous', flow:'scrolled'})` + 主题/字号/行距/目录(epub.js navigation)/进度(spine idx)/续读(CFI 存 localStorage)。暴露 `window.__epub = {book, rendition, cfg}`。
- **`epub-ai.js`**(26KB)= **已做好最难的「iframe 选区桥接」**:`captureSelection(win,doc)` 监听每个章节 iframe 的 selectionchange + epub.js `selected` 事件 + **iOS Safari 轮询兜底**(iframe 里 selectionchange/touchend 常不触发)+ 选区 rect 从 iframe 坐标换算到父视口。还接了 选区工具栏/查词(dict[-jp])/翻译/解释/对话/AI侧栏。**这套选区桥接是金子,务必复用。**
- 引擎切换:`pdf_reader.py::epub_view`,`?engine=epubjs` → `epub_reader.html`(epub.js);默认 → `epub_html_reader.html`(手搓,功能齐全)。**搬齐前默认永远是手搓版,不丢功能。**

## 控制层架构(关键认知)
- `rc-*.js` 是**从 PDF 派生的、内容无关的控制层模块**(`window.RC.{wordpop,result,highlight,snippets,figures,dict,phrasepop,assistant,sidedrawer,settings,knowledge,md,typeset,core}`)。PDF 阅读器**自己不加载 rc-*.js**(用它自己的 reader.src);rc-*.js 是给 EPUB 用的。
- `RC.use(adapter)` 注册 `{config, getEndpoints, toast}`;但各 rc 模块主要靠**每次调用传 options**(file/rect/ctx/回调),不是纯 adapter 模式。
- `epub-html.js`(手搓驱动)= 调这些 rc-* 模块 + 自己的底座(选区/char 位置/高亮/装饰)。
- **epub.js 版要做的** = 写一个**新底座驱动**(类似 epub-html.js 但底座是 epub.js):epub.js 选区(复用 epub-ai.js 的 captureSelection)→ 调同一批 rc-*.js → 高亮用 epub.js annotations + CFI 锚 → 生词/振假名/词组/语法 装饰进章节 iframe 的 document。

## 移植阶段(每阶段对照 PDF reader.src 原版,搬完 `?engine=epubjs` 给用户测,全齐才切默认)
- [ ] **P1 选区工具栏 + 查词**:epub.js 选区 → rc-toolbar/selBar 分流(单词↔多词↔词组)→ 单击直弹字典 `RC.wordpop.show` / 工具栏查词。对照 `reader.src/13-selection.js` + `15-phrase-wordpop.js` + `19-dict.js`。
- [ ] **P2 翻译/解释/对话**:`RC.result.aiCall`/`openChat`,流式 + Markdown×MathJax + 选段→笔记/Anki(`RC.snippets`)。对照 `20-result-draft.js` + `21-misc-ai.js`。
- [ ] **P3 高亮**:`RC.highlight` 编辑浮层 + **epub.js `rendition.annotations.highlight(cfiRange)` 渲染 + CFI 锚持久化**(替代手搓 sidecar offset 锚)。后端高亮存储改用 CFI(EPUB 专属端点)。对照 `17-highlight.js`。
- [ ] **P4 生词下划线**:装饰章节 iframe document(按 BOOK_LANGS 门控,中文母语不划)。对照 `08-charlayer.js::renderVocabUnderlines` + `12-vocab-sentences.js`。
- [ ] **P5 振假名/音标(ruby)**:iframe 内注 ruby。对照 `reader.src` 的 ruby 实现。
- [ ] **P6 词组(F6)**:`RC.phrasepop` + 呼吸高亮(iframe 内)+ 收藏作分词。
- [ ] **P7 语法分析**:选区/抽屉。对照 grammar 路由。
- [ ] **P8 助手侧栏**:`RC.assistant`(agentic 工具循环 + 历史 + 感叹号反馈弹窗 `_buildFbPop`)接 `/api/epub-assistant`。对照 `25-assistant.js`。
- [ ] **P9 单词本 tab / 知识点 / 设置面板 / 搜索 / 整页翻译 / 图描述徽标**:`RC.sidedrawer`/`RC.settings`/`RC.figures` + 搜索。
- [ ] **P10 书本语言「需要翻译的语言」**:`/pdf/api/book-langs` 多选(已在手搓版做好,搬过来)。

## 📋 EPUB(epub.js)对照 PDF 全功能审计(2026-06-28,14 面并行 audit → 30 high + 34 med)
**用户要求「确认 PDF 所有功能 EPUB 都有」。** 审计结果分诊如下(完整 raw 见 `scratchpad/parity_gaps.json`):

### ✅ 已排除(误报 / reflow 天然不适用 —— 不补)
- **OCR 重识别**:EPUB 是干净 HTML 文本,无坏文字层,不需要。
- **选中栏「笔记/制卡」按钮**:PDF 选中栏也没有(审计员自注「设计一致」),走 AI 回答选段→草稿。
- **CFI 高亮持久化「缺失」**:误判 —— `epub2-highlight.js` 已有完整 CFI sidecar 高亮;result-ai 审计 agent 不知道该文件。**真缺**只是「AI 结果框→标记高亮/制卡」没接(见下)。
- **手写笔 / 单页双页 / 双指缩放 / 页码滑块**:reflow 内容流动,墨迹/页/缩放无法锚定,PDF 专属。
- **单击三级粒度 行/段**:reflow 下行/段选取意义不大(词级单击已足);拖选已可多词。

### ✅ 已补齐(2026-06-28 这轮)
- **助手**:整文件重写 `epub2-assist.js`(947 行)忠实搬 PDF `25-assistant.js` —— FOLLOWUP 剥离+追问 chip / 逐字浮现 reveal / 全套 SSE 事件 / ⚙ 模型设置(自包含,写回 `window.RC.assistant`+`openModelSettings`)/ 感叹号反馈弹窗 / 上下文卡 / 连续语音 / 撤销卡。根因:`rc-assistant.js` 从没在模板加载过。**设置面板 model/effort 不强制助手**(保 Gemini 省钱路由)。
- **🖌 标记高亮**:`epub2-highlight.js` 加 `markFromSel`(选区+AI 正文 body→note);`epub2.js mkResultOpts` 捕获选区+传 `markHighlight`;单击词 wordpop 算 cfi 传 `markHighlight`。
- **vocab-sentences 句子系统**:新建 `epub2-sentences.js`(497 行)—— 未掌握词≥3 的句子父文档 overlay 画 L 角标(reflow 用 live Range + getClientRects,不动 iframe DOM,跟 deco 不冲突)+ 点 L → `/api/translate-sentence` 就地浮层(内存缓存回放)+ 呼吸动画 + 长按菜单(重译/删,localStorage 持久化 dismiss)。

### 🔴 真要补(high,按优先级)
1. **助手**(用户点名)—— 无 `[[FOLLOWUP]]` 剥离(控制标记当正文)/ 无 reveal / SSE 简化 / 反馈弹窗简化。**直接搬 PDF `25-assistant.js`**(进行中)。
2. **AI 模型设置**(用户点名)—— 助手内 model/effort 入口框架在但不通(依赖助手模块);随助手港一起修。
3. **AI 结果框 → 标记高亮 + 制卡**(`markFromResult`/`ankiFromResult`)—— `epub2-highlight.js` 没实现 + `epub2.js mkResultOpts` 没传 `markHighlight` 回调。高亮存储补 `body` 字段(存 AI 解释)。
4. **解释「后台高亮闪烁 + 点击打开」** —— PDF `_runExplainBg`/`_showExplainHighlight`:解释不直接弹框,先在选区建琥珀闪烁高亮、点了才开。EPUB 有 CFI 高亮可做,现是直接弹框。
5. **句子系统(vocab-sentences)** —— 未掌握词≥3 的句子标 L 形按钮(句首+句末)+ 点击就地覆盖中文翻译 + 呼吸动画。整个子系统 EPUB 缺;需后端句子识别(PDF `/api/page-overlay` 的 vocab_sentences,EPUB 要新端点按 section 出句)。
6. **词组(F6)** —— 选短多词→词组小框(而非翻译框)+ ★收藏(`/api/phrases`,合并为分词单元)+ 标记掌握(`/api/phrase-mark`)。审计说 EPUB 缺;**待核**(epub2-deco 有 phrasepop,但收藏/掌握/工具栏按钮可能没接全)。
7. **图**:图框焦点+高亮 bbox / 拖图进助手对话 / 图附件条 / 图缩略图(`/api/figure-crop`)。

### 🟡 med(体验,后补)
reveal 流式 / 各种乐观 UI(查词后下划线即时移除、词组掌握即时反馈)/ 呼吸动画 / 振假名 AI 纠正(`furigana-verify`)/ 翻译流式自动覆盖 / 高亮左滑删除 + no-color 虚框 + 预览字段 / 草稿整条回答右上角+ / 知识点按页 vs 按全书 / 本书插图描述开关。

### 落地顺序
助手(港中)→ markFromResult/anki + explain-bg-highlight(接 CFI 高亮)→ vocab-sentences(后端句端点 + 前端 L 按钮)→ 词组核全 → 图 → med 批量。每步浏览器待用户测。

## ⚠️ 重大坑:iOS 在 epub.js 的 iframe 里「单击文字」不派发任何事件(2026-06-27 实测)
- **症状**:单击英文词无反应;长按选区能用。
- **诊断**(全事件捕获 + 服务端 `state/epub-dbg.log`):单击时 iframe doc 上 **touchstart/touchend/pointerdown/up/click/mousedown/up/selectionchange 全部不触发**,父文档也没有。长按选区能工作是靠 **350ms 轮询查 `getSelection()`**(不是事件)。
- **根治**:单击 = iOS 在 iframe 里放一个**折叠光标**(getSelection().isCollapsed + anchorNode 是文本节点)。在轮询里检测「新折叠光标」→ `onCaretTapWord(anchorNode, anchorOffset)` 取词弹字典。**这是 epub.js iframe 上单击查词的唯一可行路径**(touch/click/selectionchange 都不可靠)。
- **选中词标记**:不能用原生选区(会触发 iOS 原生菜单 + 让轮询误判为选区弹工具栏)→ 用临时 `<span class="ep-tap-mark">` 高亮包词(CSS 注进 iframe head),下次单击清掉。`_dictGate()` 500ms 去重防桌面端 click+poll 双弹。
- **远程调试法**:`state/epub-dbg.log` + 前端无条件 `_tlog()` POST 到 `/pdf/api/epub-dbg`(没设备时定位前端行为的唯一办法,务必记得)。

## 坑预警(epub.js iframe 底座)
- 选区/点击在 **iframe** 里,坐标要换算到父视口(epub-ai.js 已处理);iOS Safari iframe 事件不可靠 → 轮询兜底(已有)。
- 装饰(生词/ruby/词组高亮)要注进 **iframe 的 document**,不是父文档;epub.js 卸载离屏章节会丢装饰 → 监听 `rendition.on('rendered')` 重新装饰。
- 高亮锚用 **CFI**(epub.js 原生),别再用手搓的 section+offset。
- rc-*.js 注入的弹层/CSS 在**父文档**(浮层),不进 iframe。
- `_epub_js_v()` 已含 rc-*.js + epub-reader/epub-ai 的 mtime → 改了自动 bust 缓存。

## 当前状态(2026-06-27,用户授权「全搬」自主进行)
- 默认 = 手搓版(功能齐全,用户随时有完整功能);epub.js 版 = `?engine=epubjs`。**全程不动默认,不丢功能**。
- **已完成**:P1-2(选区桥接 verbatim 复用 + 工具栏 + 查词/翻译/解释/对话/制卡,走 rc-*.js)/ chrome 对齐(epub_reader.html 顶栏=手搓版=PDF:title/scrub/あ/译页/🔍/⛶/⚙ + 统一抽屉 + 设置走 RC.settings + scrub/全屏 + 单击直弹字典修复)。
- **驱动文件**:`epub2.js`(主驱动,从 epub-ai.js 搬选区桥接 + 调 rc-*.js + chrome wiring)+ `epub-reader.js`(epub.js 渲染地基,暴露 `window.__epub={book,rendition,controls}`)。未搬功能在 epub2.js 是 placeholder toast。
- **✅ P3-P10 已全部集成 + 部署 + 运行时 harness 验过(6 脚本 init 无抛错)**,在 `?engine=epubjs`:
  - `epub2-highlight.js`(P3:epub.js annotations + CFI 锚 + rc-highlight + 高亮 pane;后端 epub-highlights 加 sentence 字段)
  - `epub2-deco.js`(P4 生词下划线[BOOK_LANGS 门控] + P5 振假名 + P6 词组 + P10 需要翻译的语言;装饰注章节 iframe doc + `rendition.on('rendered')` 幂等重装;暴露 `window.__epubDeco`/`saveLangPicker`/`refreshVocabUnderlinesForAllPages`)
  - `epub2-assist.js`(P8 助手:**byte-for-byte 照搬手搓版** runAssistant/SSE/感叹号 `_buildFbPop`/历史/连续语音 mic/撤销卡,底座换 epub.js 章上下文+跳转+选区)
  - `epub2-extra.js`(P9 搜索 epub.js `section.find` 跨章 / 译页 `_pagetrApplySection` / 图徽标 `RC.figures` / 知识点 `RC.knowledge`)
- **集成方式**:每模块自包含等 `window.__epub` ready;epub2.js 删了所有 placeholder toast(あ/译页/🔍 列表清空、kg 分支、placeholderAsst IIFE)+ 接了设置回调(getBookLangs/onSaveLangs/onVocabUnderline)+ onMastered 重画下划线;模板加 8 个 script tag;`_epub_js_v` 含新模块。
- **✅ 单词本 pane 已做**(`epub2.js` 内 `loadVocabPane` 照搬 PDF;本章 scope 走 `RC.adapter().currentChapterText()`;scope 本章/本书/全部 + 发音 + 加卡 + 点词查词)。
- **✅ 语法分析(P7)完整版已做**(`epub2-grammar.js`,965 行,照搬 `reader.src/18-grammar.js`):字典框「📊 语法」`onGrammar` + 多词工具栏「📊 语法」`data-act=grammar` → `onGrammarAnalyze` → 句子抽取(从 ctx 按句末标点切句找含 focus 的句)→ `/api/grammar-analyze`(spaCy 依存)+ `/api/grammar-stream`(`[[TRANS]]`/`[[POINTS]]` 流)→ 抽屉新增「语法」pane(`#ep-side-grammar`/`#ep-grammar-body`)渲染分析块(依存图 4 模式 树/块/主干/弧线 + 翻译 + 语法点 + 追问 + 🎴制卡 + 历史)。grammar pane 顶部「⚙ 启用语法 KG」(grammar-books/tracked)。tab 由模块 MutationObserver 自插(不动 epub2.js 的 sidedrawer.init)。SSE 复用 `RC.result.aiStream`(rc-result.js 新暴露)。**需先启用至少一个语法 KG + 跟踪节点才出语法点**(spaCy 结构/翻译无需 KG)。
- **✅ 日语分词浮层 + 抛弃轮询光标**(2026-06-28):
  - 后端新增 `/api/epub-tokenize`(`_epub_ja_tokens`:fugashi `_get_jp_tagger()` 全分词,含纯假名/助词,find 对齐字符偏移;按文本哈希缓存 `epub-furigana-cache/jt-*.json`)。**注意 furigana 端点只给汉字词、不够分词浮层用**,所以单独建全分词。
  - 前端 `epub2-deco.js::jaWordApply`:'ja' 启用 → 每个日语词包 `.ep-w[data-jaw]` → 父文档浮层按钮覆盖 → 精确单击。装饰顺序 vocab(sync,`.ep-vocab-und` 被浮层覆盖)→ jaWordApply(async)→ ruby(嵌进 `.ep-w` 不冲突);`decoCandidates('jaw')` 跳 `.ep-vocab-und`/`.ep-w`;节点 `nodeValue !== 发送文本` 则跳过防 token 错位;切语言 `jaWordClear()`+重装饰。
  - `epub2.js`:**轮询光标查词彻底删掉**(selectionchange + poll 的折叠光标分支都改成「只关字典框、不取词」)—— 英日单击**统一走父文档浮层按钮**(精确、无「点空白光标锁别处误选」)。浮层选择器加 `ruby[data-eph]`(ruby 开时词也可点),`_dictForWordSpan` 对 `<ruby>` 剔除 `rt` 取词面。
- **仍 TODO**:① 助手内高亮目前用 epub.js 内存 annotations 未持久化(接 P3 的 sidecar)② ruby 开时多词选区可能夹带读音(单词浮层已 OK)。
- **没碰**:默认手搓版(功能齐全,随时可用)/ reader.src/* / PDF 阅读器(哨兵 200)。手搓版 session 修复保留,epub.js 切默认后退役。
