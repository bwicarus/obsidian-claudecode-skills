# 统一控制层(window.RC):PDF / EPUB 阅读器共用一份控制层代码

## ⚖️ 设计铁律(2026-07-02 用户拍板,凌驾于本文档其余所有内容)

> 用户原话:"让 epub 还有新的 pdf 阅读器的操作层能够拥有高亮、下划线、内容的输入输出等共同的接口,**完全满足原有 pdf 阅读器相关操作功能的所有需求**,也就是说**让中间层去适应旧代码,甚至说完全复用旧代码**,而不是为了不同的阅读器创造新的上层建筑。"

拆解成三条可执行规则:

1. **接口规格以 PDF 原生代码为 ground truth**。设计任何共享接口(高亮/下划线/呼吸高亮/选区/定位/内容 I/O)之前,先把 PDF 原生对应实现**完整读一遍**(函数体+全部调用点+CSS),接口的状态机/时序/交互语义必须 1:1 覆盖原生行为——接口去适应旧代码,不是旧代码来适应接口。
2. **共享层只管"策略"(状态机/时序/流程),不许自己发明"机制"(渲染/坐标/DOM 结构)**。"怎么把高亮画在内容上"由各阅读器经接口提供:PDF 侧 = **直接复用原生函数/原生 CSS 类名**(host-bind,如 `wordHlWrap` 照抄 `renderWordHl`、`explainHighlight` 直调 `_showExplainHighlight`);EPUB 侧 = 用它自己已被验证的机制(splitText+mark 包裹,同生词下划线/词组高亮)满足同一规格。
3. **凡是"锚定在内容上、要跟着内容滚动"的视觉元素,严禁用视口坐标 overlay + JS 监听滚动手动跟随**。必须锚进内容自己的坐标系(PDF=pw 内绝对定位层,EPUB=真实 DOM 包裹)。fixed+跟滚有结构性窗口期漂移,调参数救不了(2026-07-01/02 连环 bug 的根因)。

**这条铁律的实证**(2026-07 初呼吸高亮连环 bug,四次被用户抓包,根因全是违反上面规则):
- 共享层自己发明 fixed 呼吸高亮 → 滚动漂移(违反规则3);
- 凭印象重写等待时序 → 丢了"快词不闪/慢词才物化"语义(违反规则1);
- 自己发明"新查词作废旧查词"单例 → 丢了原生多实例并存/转常亮/框归属整套状态机(违反规则1+2);
- PDF 侧没接 host 渲染钩子 → 原生零漂移的 `.word-hl-layer` 被闲置,走了 fixed 兜底(违反规则2)。
每一个的修法最终都是同一个动作:**把机制退还给旧代码,共享层只留策略**。

**现存偏离清单**(按此铁律清理,2026-07-02 处理):
- [x] `rc-wordpop._positionPop`:核对发现确有行为差(原生框 absolute-in-#main **随内容滚动**,共享层 fixed 停在视口)→ 已加 `opts.positionPop` host hook(随框归属 `_wordPopOwnerId` 切换,per-hl 捕获定位锚),PDF 经 `27-rc-adapter.js::positionWordPop` 直调原生 `_positionWordPop`;EPUB 不传 hook,fixed 视口定位就是它自己的既有行为。
- [x] position:fixed 兜底已确认仅剩"无 breathe hook 且无有效 rect"的极端路径,PDF/EPUB 常规路径都不再经过。
- [x] `rc-grammar` 差异②(无跟踪时按钮不隐藏):EPUB `showSel()` 已加 `RC.grammar.hasTracked()` 门槛 + 开书时 `loadTracked(FREL)` 预载,对齐原生 `_updateGrammarBtnVisibility`(`_grammarHasTracked && lastSelText`)。差异①(KG 状态缓存双份)核查结论=各司其职(PDF 原生缓存管工具栏按钮显隐,RC.grammar 缓存管共享分析流程),幂等、只多一次廉价 GET,接受现状。差异③(Anki 进度小圆点第三份实现)保留,`RC.snippets.toAnki` 的 showCard 契约(会弹助手面板)不适配该场景。
- [x] `rc-dict.js`:4 个模板 script 标签 + `pdf_reader.py` 3 处缓存清单引用已全清,epub-html.js 过时注释已改;文件本体删除+部署待 Bash 恢复后执行。
- [x] 下划线刷新核查结论:PDF 原生与 EPUB(epub-html.js:2280)定义**同名同义**的 `window.refreshVocabUnderlinesForAllPages`,`rc-wordpop._refreshUnderlines` 防御式统一调用、全局刷新幂等——本质是已在工作的隐式契约(同 `window.onGrammarAnalyze` 用法),强行显式化收益低,记录为既定契约不重构。

## 📦 2026-07-02 收尾批次:设置面板统一(阶段7)+ reveal 移植 + 乐观下划线
- [x] **设置面板统一(rc-settings 全面重写,PDF=阶段7)**:一套面板代码两边内容一致,规格=PDF 原生面板逐字(4 tab + 取消/保存两段式)。
  - rc-settings.js:承载 PDF 原生面板全部结构,**控件 id 全用原生名**(set-sent-*/set-toc-*/set-hl-colors/lang-checks…;set-model/set-effort 已随 2026-07 AI 配置收口批次删除,见下);PDF 特有块(页码对齐/插图描述/书籍目录/auto-orient/去边/文字层校准)= 原生 HTML 逐字含内联 onclick(直调原生 window.*),EPUB 特有块(字号/主题/行距/侧栏外观/插图徽标/转PDF)经 `data-sec` + opts 门控。存量键名不迁:PDF 用 pdf-*,EPUB 用 eph-*。
  - PDF 接线:21-misc-ai.js `openSettings` 拆成 `_fillSettings`(原函数体)+ `_openSettingsNative`,__uiShared → `PdfAdapter.openSettings({fill,fallback})` → `RC.settings.open({host:'pdf', ids:{mask:'settings-mask',langChecks:'lang-checks'}, onFill=_fillSettings, onSave=原生 saveSettings, onCancel=原生 closeSettings,…})`。**关键机制:rc 面板 mask 也叫 `settings-mask` + 原生 id 全套 → 原生 fill/save/close/renderHlColorSetting/loadTocStatus/renderGrammarTrackList/_populatePageOffsetUI/_initCharOfsPanel 零改动直接工作在 rc DOM 上;模板原生 mask 由 pdf-adapter 在共享模式首开时移除(防 getElementById 撞旧模板,rc mask 带 data-rc 标记区分)**。?ui=legacy 不加载 rc → 模板面板逐字保留。pdf_reader.html 共享块 + `_pdf_shared_js_v` 清单加 rc-settings.js。
  - EPUB 行为对齐 PDF:model/effort/debug/生词下划线/点词翻译/句子翻译源改为「保存」才落盘(原改即存+关闭即POST;取消=丢弃);语法 pane 补上跟 PDF 同款 KG 启用列表(epub-html 传 `grammarFile:FREL` → RC.grammar.renderTrackList('set-grammar-list'))+ `onGrammarView` 即时重渲(RC.grammar.setViewMode('ep-grammar-body',v,'eph-grammar-view'))。EPUB mask id 仍 `ep-settings-mask`(epub2-extra.js MutationObserver 注入 epubjs 版插图开关靠它,勿改)。epub2.js/html-reader.js 旧 opts 契约全兼容。
- [x] **reveal tick 逐字浮现移植进 epub-html 助手**:照搬 PDF 25-assistant.js `_streamWrap/_revealTick/_appendCaret`(经 epub2-assist.js 已验证的 ep-mfx- 前缀移植,函数体逐字)。answer 分支从「每 delta setMd(含 MathJax typeset!)」改为「流式轻量渲(不 typeset)+ 揭示游标」,收尾才 setMd 一次(= PDF 原生 typeset 节流语义,顺带修了流式期间每 delta 全量 typeset 的性能问题)。CSS 在 epub_html_reader.html。
- [x] **乐观下划线接线**:epub-html.js 生词装饰区定义 `window.__epubDeco.optimisticMaster(word)→restore()`(rc-wordpop.js:444「☆ 标记掌握」调)。实现逐字照搬 epubjs 引擎 epub2-deco.js 的已验证版本(匹配 `.ep-vocab-und` 文本 → 加 `ep2-und-opt-off` class 隐下划线,不动 DOM;失败 restore 摘 class;服务端刷新兜底)。注意:任务前提"__epubDeco 全项目无定义"不准确——epub2-deco.js(epubjs 引擎)早有,缺的只是主文档版。**PDF 共享模式暂无此 hook**(rc-wordpop 只认 __epubDeco 这个名),PDF 掌握后靠 `_refreshUnderlines`→`refreshVocabUnderlinesForAllPages` 刷新,与迁移前行为一致,未回归。

## 📦 2026-07-02 批次:AI 模型配置体系整体重设计(收口到 action 预设单层)
- [x] **界面收口**:rc-settings AI tab 删掉旧 model/effort 两个下拉(「保留仅兼容」层)+「打开 AI 模型设置」跳转按钮,主体改为**内嵌**按功能配置表 `#rcset-ai-inline` ← `RC.assistant.renderModelSettings(container)`(从 openModelSettings 抽出的可复用渲染,浮层与内嵌共用同一实现/同组端点 `/api/assistant/action-pref[s]`;助手侧边栏 ⚙ 浮层入口保留)。epub_reader.html / html_reader.html 补加载 rc-assistant.js(内嵌表依赖)。
- [x] **pdf-ai-overrides 全链废弃**:前端三个收口点恒返 `{}`——reader.src/21-misc-ai.js `_getAiOverrides`(覆盖 aiCall/onOcrSel/onTranslate/_runExplainBg/17-highlight/18-grammar/20-result-draft/05-nav 全部消费点)、rc-settings `aiParams()`(eph-ai-model/effort 键弃用)、pdf-adapter `_aiParams`(注入 rc-result 的)。saveSettings 顺手 removeItem 清存量键。**后端同步忽略**:grammar-analyze/grammar-stream 死变量删除、ocr-selection 死参数链删除(`_claude_ocr_crop`/`_claude_vision_pages`/`_build_toc_job` 的 model/effort 形参一并清)、translate-sentence 不再收 request model/effort(空参 → translate.py 自动回退 translate-config;「句子翻译源」set-sent-* 独立系统保留)。`?ui=legacy` 模板不动(其 model/effort 下拉自然失效)。
- [x] **grammar action 拆出**:assistant.py `_AP_ACTIONS` 加 `grammar`(默认 gemini-3.5-flash·think),pdf_reader.py grammar-analyze / grammar-stream 从 explain 改挂 grammar;面板(rc-assistant + 25-assistant legacy 副本)`_renderActs` 第二组加 'grammar'(`if (!ai) return` 向后兼容)。
- [x] **Gemini 清单/付费路由**(assistant.py,细节见 references/pdf-reader.md「AI 模型设置」节):`_gemini_models()` 合并 free+paid 两把 key 的 ListModels(修「3.1-pro 面板永远没有」);`_is_paid_only` 两信号判定(清单差集 ∨ paid 清单有+免费档验证不支持——**实测免费 ListModels 也会「列出」paid-only 型号,光差集不够**),持久化 `state/gemini-paid-only.json`;action-prefs 面板对仅付费型号**标💰不隐藏**、免费探测跳过;`_gemini_keys` 对仅付费型号跳过 free key(不白吃伪 429;text/vision/stream 三条路一处覆盖)。

## 📍 进度快照(2026-06-28 起，**PDF 侧已大幅落地，2026-07 更新如下**)
**架构已被证明可行，且 P3(迁 PDF)已在生产默认启用。** 四个阅读器现状:
| 阅读器 | 驱动(per-reader substrate) | 用共享 rc-*.js? | 适配器 | 状态 |
|---|---|---|---|---|
| **EPUB(主文档,默认)** | `epub-html.js`(手搓,266KB) | ✅ 全套 | `EpubAdapter`/host hook | **默认 EPUB 阅读器**,功能最全。`/pdf/epub/view` 默认走它 |
| EPUB(epub.js,退役中) | `epub2*.js` | ✅ 全套 | `EpubAdapter`(epub2.js 内) | `?engine=epubjs` 才走;开发已停(见 [`epub-on-epubjs-migration.md`](epub-on-epubjs-migration.md),epub.js 迁移计划已废) |
| **HTML** | `html-reader.js` | ✅ 核心 | `HtmlAdapter`(html-reader.js 内) | **P2 架构验收**:`/pdf/html/view`。选区/词典/翻译/解释/对话/笔记/制卡/高亮(offset sidecar)。**证明「给新阅读器写个 adapter,共享功能全有」** |
| **PDF(主力,已迁)** | `reader.js`+`reader.src/*` | ✅ **默认 `ui_shared=1`** | `PdfAdapter`(`pdf-adapter.js`+`27-rc-adapter.js`)✅ | **已迁**:`pdf_view` 默认加载 rc-*;`?ui=legacy` 逃生口回落纯 reader.src。见下「✅ P3 落地实况」 |

**关键认知(过夜厘清):**
- 「一套功能模块」= **`rc-*.js`**(共享 UI/控制层,已被 EPUB + HTML 两个 adapter 复用证明)。**per-reader 驱动(epub2*.js / html-reader.js / reader.src)不共享**,各自处理底座(iframe/CFI、主文档 offset、char 层/PDF.js)。
- ⚠️ **把 EPUB 专属模块(epub2-*)改成调自己的 EpubAdapter 是低价值间接层** —— 它们不会被别的阅读器复用,不必为此重构(deco 已接,够了)。
- ⚠️ **真正的「未共享」缺口**:`grammar`(epub2-grammar.js)、`vocab pane`(epub2.js 内)、`分词浮层` 这些**功能逻辑**目前在 EPUB 专属文件里,HTML/PDF 复用不到。要真正「一套」,得把它们的**逻辑层**抽进共享 `rc-grammar.js` / `rc-vocab.js`(底座经 adapter),per-reader 只留薄驱动。**这是大重构,且改的是刚建的代码、无法浏览器盲测 → 留用户能测时做,别盲改。**

## ✅ P3(迁 PDF)落地实况(2026-07,原「为什么过夜没盲做」的谨慎路线已按计划走完主体)
当初的顾虑(PDF 是主力、char 层深耦合、无法浏览器盲测、违反「绝不弄坏 PDF」铁律)通过**逃生口 + 逐功能接**化解,现状:
- **`PdfAdapter` 已建 + 全接线**:`pdf-adapter.js`(window.PdfAdapter,方法 captureSelection/clearSelection/lookupWord/explain/translate/chat/openFullDict/lookupPhrase/openHlEditor/figurePop/renderHighlightList/renderPageNodes/openModelSettings/splitFollowups/openSettings…)+ `reader.src/27-rc-adapter.js` 经 `PdfAdapter.bind({...})` 把 reader.src 模块作用域内部(`_charSel`/`lastSelText`/char 层坐标/端点)桥进去,并 init `RC.stickynote`。
- **旁路 flag 反了方向**:不是当初设想的 opt-in `?ui=shared`,而是**默认 `ui_shared=1`(共享层)+ `?ui=legacy` 一键逃生**(`pdf_view`:`ui_shared = 0 if request.args.get("ui")=="legacy" else 1`)。legacy 不加载 rc-*、模板原生面板逐字保留。
- **已切共享(默认走 rc-*)**:字典/查词(rc-wordpop)、翻译/解释/对话(rc-result)、词组(rc-phrasepop)、图徽标(rc-figures)、知识点(rc-knowledge.renderInto)、语法(rc-grammar)、设置面板(rc-settings.open,复用原生 id)、便签(rc-stickynote)、收藏(rc-favorites)、插入页(rc-userpages)、助手薄增量(openModelSettings+splitFollowups→rc-assistant)。
- **仍留 reader.src(未收敛,有意)**:①**选区/工具栏底座**——`#sel-toolbar` + char 层 `_charSel`/`lastSelText` 仍是 PDF 自己的,adapter 只经 `captureSelection`/`clearSelection` 桥;②**高亮叠层渲染**留 char 层像素几何(`renderHighlight/List` 等钩子就位但暂无 live 调用方,PDF 无「高亮抽屉」UI);③**助手主体** `25-assistant.js`(rc-sidedrawer 有意不给 PDF 载,只把型号设置/追问路由到 rc-assistant)。这三块是 char 层/PDF.js 深耦合处,把它们真正抽进共享 rc-* 是更大重构、且改的是主力工具 → 留到有把握时做,不阻塞。

**原分阶段迁移路线(历史,主体已走完)**:写 PdfAdapter 契约 → 旁路 flag 逐功能对比 → selection/toolbar/dict → result/highlight/snippets/figures/settings → grammar/assistant → 全绿后切默认。**每步的 PDF 回归清单仍适用**(选词三级粒度 / 拖选词边界 / 高亮坐标 / 双指缩放 / 单页双页 / 公式 / 手写笔;固定回归书集 应用情報 p37 / 双栏书 / 日语书 / 公式页)。


## 🎯 最终架构(2026-06-27 用户拍板,目标:一套功能模块 + 任意阅读器适配器)
**愿景**:每个功能抽成**独立、零底座耦合的模块**,通过统一 `RC.adapter` 契约接入**任何阅读器**(EPUB/PDF/未来 HTML/MOBI…)。改一个功能只改一处,所有阅读器受益。**PDF 阅读器最终也迁过来(PdfAdapter),不永久搞特殊**(否则改功能要改两套=维护噩梦)。

### 三层
1. **功能模块**(`rc-*.js`,从 PDF 抽的,零底座耦合):toolbar/dict/result/highlight/vocab/ruby/phrase/grammar/assistant/figures/snippets/sidedrawer/settings/search。只调 `RC.adapter()` 取 I/O + RC 工具,**不碰具体 DOM/坐标/iframe**。
2. **`RC.adapter` 契约**(每种阅读器实现一次):
   - 选区 I/O:`captureSelection()→{text,context,anchor,rect}` / `clearSelection()`
   - 定位:`jumpToAnchor(anchor)`(anchor 不透明:EPUB=CFI、PDF=page+rect)
   - 内容:`currentChapterText()` / `eachContentDoc(cb)` / `onContentRendered(cb)`(供装饰)
   - 元信息:`fileInfo()→{file,langs}` / `getEndpoints()` / `config`(能力开关:isPDF/reflow/dictMode/anchorKind/clickWordDetect/hasFigures/supportsVoice…)
   - 可选:`toast` / `positionPopover`
3. **阅读器适配器**(每格式一个文件):`EpubAdapter`(epub2.js 内,**已建 + RC.use 注册** ✅)/ 未来 `PdfAdapter` / `HtmlAdapter`…

### 三阶段(顺序=风险排序,EPUB 当试验场,PDF 最后收)
- **P1 立契约 + 净化模块**:定义 `RC.adapter`(rc-core 已有 use/adapter/config/endpoints/esc/debounce/reqJson/toast)+ 把功能模块改成只通过 adapter 取 I/O。**进行中**:EpubAdapter 骨架已落(config/endpoints/captureSelection/clearSelection/jumpToAnchor/eachContentDoc/onContentRendered/currentChapterText/fileInfo);下一步逐个功能模块接到 `RC.adapter()`。
- **P2 EpubAdapter 验全功能**:EPUB(`?engine=epubjs`)在新架构上把所有功能跑通跑顺(单击查词/高亮/生词/词组/助手/搜索…)。验收:给纯 HTML 写个 `HtmlAdapter` 几十行 → 所有功能全有 = 架构成。
- **P3 PdfAdapter 迁 PDF**(最后、最谨慎、重度验证)✅ **主体已落地**(默认 `ui_shared=1`,见上「✅ P3 落地实况」):把 `reader.src` 功能逻辑替换成调共享模块,char 层收敛成 `PdfAdapter`。做完=一套代码两个阅读器。**PDF 是主力,迁前必须 EPUB 全验过 + 完整回归**(选区/工具栏底座 + 高亮叠层渲染 + 助手主体仍留 reader.src,有意未收敛)。



2026-06-27 启动。目标:消除「给 EPUB 阅读器加功能要手动照搬 PDF 阅读器、总 drift」的根因——把控制层(选中工具栏/字典/AI结果渲染/高亮/图/助手侧栏/草稿笔记)抽成 **一份共享代码 `window.RC`**,PDF(`reader.src/*.js`,功能最全、绝不能弄坏)和 EPUB(`epub-html.js`)各自只提供一个 **适配器**(底座耦合的唯一落点)。

> 设计由 workflow `unify-control-layer-design`(8 个只读测绘 agent + 1 综合)产出。**铁律:每一步 PDF 阅读器都不能坏。**

## 为什么要它
PDF 控制层绑死在「字符层(PyMuPDF 字符像素 bbox)+ page+rect 锚」;EPUB 绑在「原生 Selection + section+offset 锚」。但**控制层真正依赖的只是**:选中文本/上下文/坐标/锚点、把高亮画回去、跳转、取上下文、后端端点、能力开关。这些用适配器抽象后,UI/渲染/AI 调用就能**一份代码两边用**。

## 适配器接口(39 方法,各 reader 实现 + 末尾 `RC.use(adapter)`)
选区:`captureSelection()`→{text,context,anchor,rect,eventType}(内部先做坐标同步,PDF=_syncCharBoxScale)、`getSelectionText/Context/Rect(anchor)`、`clearSelection()`、`expandToWord(anchor)`[config.clickWordDetect 门控]。
高亮:`renderHighlight/removeHighlight`、`saveHighlight/deleteHighlight/listHighlights`、`getHighlightColors()`、`jumpToAnchor(anchor,smooth)`。
图:`getImageElement/getImageRect/getImageContext/getBadgePosition/getDescription/getFigureId/setFigureHighlight`[hasFigureHighlight 门控]。
AI/上下文:`getFileInfo/getPageContext/getCurrentChapterText/getAssistantContext(reason)/getAiParams/buildTranslateBody/drawTranslationOverlay`、`pageDisplayToIndex/pageIndexToDisplay`(EPUB 返 null,助手页号跳转必经)。
草稿/笔记:`collectSnippets/getSnippetSource/getNoteName/onTaskStarted|Success|Failed/openNoteInObsidian/getThumbnail`。
通用:`positionPopover(popup,rectOrHl,mode)`、`getEndpoints()`(所有后端 URL 参数化)、`config`(能力开关:isPDF/hasFigures/hasFormula/clickWordDetect/supportsVoice/popupMode/dictMode:'sse'|'json'/hasFigureHighlight/iosSyntheticDelay)、`onDragStart/onDragEnd`[hook]。

## 共享模块(`_server_deploy/static/pdf/rc-*.js`)
- **rc-core.js** ✅ 已建 — RC 命名空间 + RC.use(adapter) + endpoints/config 透传 + esc/debounce/reqJson/toast。零底座耦合,是底座。
- **rc-md.js** ✅ 已建 — `RC.md(s)`(逐字搬自 `21-misc-ai.js::md()`:公式占位→marked→还原 + CJK 与 `*`/`` ` `` 间零宽空格 + `$(?!\s)` 边界)+ `RC.typeset(el)`。**100% 同构,最大最安全的共享点。**
- rc-toolbar.js — 选中工具栏显隐 + 防溢出屏 + 8 按钮分流。依赖 captureSelection/getSelectionRect/clearSelection/config。
- rc-dict.js — 字典浮层 + 防抖 + **双模式**(config.dictMode:三源 SSE / 一次性 JSON)。
- rc-airesult.js — 翻译/解释/对话流式 + rc-md 渲染 + 选段 picker(真假标题级联→笔记/Anki)。
- rc-highlight.js — 高亮 CRUD UI(4 色 popover + 列表面板)。anchor 全程不透明,ID 加 reader+file 前缀防串污。
- rc-figures.js — 图徽标(fig-badge/fig-pop)+ openPop 生命周期 + rc-md 渲染。
- rc-snippets.js — 选段→笔记/Anki:job 发起+轮询+失败恢复。草稿 localStorage key 两端隔离。
- rc-assistant.js — 助手侧栏(**最后切,最大 73KB**):气泡/追问 chip/openModelSettings/上下文卡片/SSE 断线恢复/loadHistory。
- **pdf-adapter.js**(reader.src 内,如 27-rc-adapter.js)+ **epub-adapter.js** — 各 reader 实现整套适配器。**底座耦合的唯一落点。**

## 分步安全迁移顺序(每步带回归点)
- **步0 ✅(已完成)**:建 rc-core + rc-md,**只接 EPUB**(epub-html.js 的 renderMd 委托 RC.md),PDF 一行不碰。回归:EPUB 含公式/CJK 渲染与旧版一致;PDF 零影响。
- 步1:写 `epub-adapter.js` 实现全套方法(EPUB 低风险侧先跑通契约),EPUB 逐个切到 rc-toolbar/dict/airesult/highlight/figures/snippets/assistant 并验证。产出**被真实第二实现验证过**的共享层。
- 步2:PDF 接 rc-md(新增 27-rc-adapter.js,pdf_reader.html 加载 rc-core/rc-md,`md()` behind `window.RC_USE.md` flag 委托 RC.md)。build_pdf_reader_js.sh + check_pdf_reader_js.sh。回归:应用情報+日语书,翻译/解释/助手公式与零宽空格逐项对比。
- 步3 图 → 步4 字典(双模式)→ 步5 airesult+toolbar → 步6 highlight(坐标 sx/sy 留适配器,応用情報 p37 缩放后建高亮不偏)→ 步7 snippets(onTaskFailed 放回草稿)→ **步8 助手(最后)**。
- 每个 rc-* 在 PDF 侧 behind `window.RC_USE.<mod>` flag 并行,旧实现保留;回归失败=翻 flag 回旧路径;固定回归书集 **应用情報 p37 / 双栏书 / 日语书 / 公式页**;旧代码留到一个完整发布周期后再删。

## 关键风险(P0)
1. 坐标同步脱节(charBoxes 像素烘焙,zoom/pinch 后失效,实测应用情報 p37 偏 19%):captureSelection 内部强制先 syncCoordinates,共享层永不碰 `__charBoxes`。
2. document 级拖动监听泄漏:`__charDrag` 分发器与 document 监听**完全留 PDF 适配器**,共享层不注册任何 document 拖动监听。
3. 块过滤/文本拼接/锚点格式不兼容:getSelectionText/Context 与 anchor **全程当不透明**,后端按端点路由,高亮 ID 加 reader+file 前缀。
4. 印刷页↔索引转换:pageDisplayToIndex/pageIndexToDisplay 留适配器,助手页号跳转必经,EPUB 返 null 时 UI 降级。

> 缓存:`epub_html_reader.html` 用 `_epub_js_v()`(含 rc-core/rc-md mtime)cache-bust。PDF 侧将来用 `reader_js_v`(reader.js mtime),改 rc-* 要记得 bump 两边或合并入 build。

---

## 「EPUB 做到与 PDF 完全一致(版面/按钮/全功能)」整夜执行计划(2026-06-27 起)

用户要求:EPUB 阅读器的**所有功能 + 版面 + 按钮**跟 PDF 阅读器**没有区别**;保留所有交互细节;持续自主推进到完成。**不提交**(用户控制 commit)。蓝图由 workflow `epub-vs-pdf-gap-map`(7 子系统精确测绘 + 综合)产出(原始在 /tmp tasks/w1zdlyxll.output,易丢)。

### 铁律(每步)
1. **EPUB-only**:只改 `templates/epub_html_reader.html` + `static/pdf/epub-html.js` + 新建 `static/pdf/rc-*.js` + `pdf_reader.py` **新增** EPUB 路由。**绝不编辑 `reader.src/*` / pdf_reader.html / PDF 现有路由**(这是「PDF 一行不动坏」的根本)。
2. 每个 .js 跑 `node --check`;改完 `cp` 到 `/var/www/html/static/pdf/` + 模板/pdf_reader.py `cp` 到 `/home/bwicarus/webapp/` + restart webapp。
3. 验证:test_client `GET /pdf/epub/view?file=费恩曼…epub` 断言 200 + **`GET /pdf/` 断言 200(PDF 哨兵)**。
4. `#ep-*` id 一个都不许改名(epub-html.js 全靠 id 取元素);新增 PDF 同名件用新 id。
5. rc-* 新模块**只让 EPUB 引**;PDF 旧实现保留不切(rc-highlight 是已共享文件,改它要跑 PDF 回归)。

### 🔧 用户验收返工(2026-06-27,审计 workflow `epub-pdf-gap-audit-2` 驱动)
**关键事实**:PDF 阅读器**只加载 reader.js,不加载任何 rc-*.js** → **改 rc-*.js / epub-html.js / epub 专属后端路由(epub-*) = EPUB 专属,零 PDF 回归**。只有改 `reader.src/*`(要重建 reader.js)或两端共用后端路由(`/api/dict`/`phrases`/`vocab-*`/`furigana-*`/`translate-config`)才回归 PDF。
**根因**:我用 agent **重写**了助手/trace/撤销等,而非**复用 PDF 原码** → 逻辑 drift + bug。用户要求「直接复用之前的」。
**已修(本轮)**:撤销⇄重做卡片(照搬 PDF `_assistEdit` 切换逻辑)/ H1 查词竞态守卫(rc-result 暴露 `window._resultReqId`)/ 单击词直弹字典(招牌交互,`eph-click-translate` 开关默认开)/ 生词下划线查后刷新(`window.refreshVocabUnderlinesForAllPages`)/ 顶栏安全区+按钮加大+`touch-action:manipulation`(偶尔没响应)/ 抽屉不再点外即关 / 高亮编辑 tap 兜底(iOS click 不稳)/ 书本语言 seg + 直翻/生词开关。
**已修(2026-06-27 第二轮,逐字照搬 PDF,全部 EPUB 200 + PDF 哨兵 200)**:
- ✅ trace 感叹号:改**内联展开**(原 absolute 弹层往上弹被气泡裁掉 = 看着没反应)。
- ✅ 复制:execCommand 兜底 + 按真实成败 toast。
- ✅ H2 搜索命中:`_searchHilite` 跳章后正文 span 高亮(span 非 mark,守 G0)+ scrollIntoView center + 6s 淡出。
- ✅ H4 自定义高亮色:`HL_COLORS`→`hlColors()`(读 `RC.settings.hlColors()`)+ `renderHlPicker()` 动态色板 + 模板删写死 swatch + saveHlColors 通知 `onHlColors`。
- ✅ H5 连续语音:**整段照搬** `25-assistant.js:930-998`(令牌 thisRec/onend 自重启/退避/2min 上限/重基线/切后台停/无SR聚焦),只改 DOM id。
- ✅ H6 单词本 tab:rc-sidedrawer tab+CSS / 模板 pane(本章·本书·全部) / epub-html `loadVocabPane`+`_renderVocabItem`+`_speakWord`+`_vocabAddAnki`(照搬 05-nav.js),复用 `/api/vocab-list|audio|anki`。
- ✅ H8 高亮编辑只读预览:rc-highlight openEditor 加 `.rc-hl-prev`/`.rc-hl-sent`,openHlEditor 传 `preview/sentence`(完整 body/kind schema 待后端)。
- ✅ H9 F6 词组:新建 `rc-phrasepop.js`(照搬 showPhrasePopover)+ epub-html 6 编辑(`_isShortPhrase`/原生 mark 呼吸高亮[G0]/`onPhrase`/分流/收藏进 `_vocabTokens` 最长匹配)+ 模板+`_epub_js_v`。
- 单击直弹字典 / 查词竞态守卫(rc-result 暴露 `window._resultReqId`) / 生词查后刷新 / 顶栏 touch-action / 撤销⇄重做卡片(照搬 `_assistEdit`) / 书本语言。
- M1 nav☰:EPUB 不载 nav.js → **N/A**。
- ✅ H7 `openModelSettings`(按功能配 后端/型号/深度):**逐字 port 进 rc-assistant.js(EPUB 专属,不碰 reader.src = 零 PDF 回归)**。关键发现:模型偏好是**服务端存储**(`/api/assistant/action-pref[s]` → `state/assistant-action-prefs.json` 按 uid+action),`epub_assistant.py` 复用 assistant.py 的 `_resolve` 读同一套 → 命中相同端点即生效(无 localStorage 键)。入口:助手快捷栏 `⚙模型` + 设置 AI pane「打开 AI 模型设置」+ trace 步骤旁 `⚙` 齿轮(`st.action` 时)。`RC.assistant.openModelSettings` 暴露。
**审计清单全清。** EPUB 阅读器版面/按钮/功能已与 PDF 对齐。
> 审计:/tmp tasks/wduj7t41q.output;port agent transcripts:/tmp tasks/{a5bab*,adafb*,a1009*,a1eb*}.output(易丢)。

### ⭐ 全部完成(2026-06-27):A–H + 顶栏/侧栏照搬 PDF + G + H 全做完
- **顶栏+右侧栏照搬 PDF**:`rc-sidedrawer.js`——顶栏 = title/scrub/あ/译页/🔍/⛶/⚙️(单行横滑);右侧 = 单个标签抽屉(助手/知识点/高亮/目录,`#ep-side-handle` 打开 + tab 切换),旧 `#ep-ai`/`#ep-toc`/`#ep-mask` 合并入抽屉;rc-knowledge 加 `embedded` 模式(不自建抽屉,只渲进 pane)。
- **G 振假名/译页/生词**:后端 `/api/epub-furigana`(unidic+IPA 合成 char 偏移)/`/api/epub-translate-section`(gtranslate 不走 AI)/`/api/vocab-mastery-map`;前端 **G0 偏移锚加固**(`_countable` 只排除我们加的 `rt[data-eph=1]`/`.ep-tr-rt`,**不排原生 rt** → 原生振假名页旧高亮零漂移;offsetOf/applyHl/captureSel/wordAt 全过)+ あ(原生 `<ruby><rt>`)/译页(块级 `.ep-tr-rt`)/生词(`<span.ep-vocab-und>` 按 mastery 着色)三开关,仅装饰可视 section、幂等、off 干净还原。
- **H agentic 助手 + 历史**:后端 `epub_assistant.py`——section 级工具(read_section/goto_section/epub_highlight/search_book/list_sections + 复用通用 make_note/anki/lookup/translate/recall)+ `/api/epub-assistant`(SSE,rid 重连,detached worker,额度护栏)+ `/api/epub-convo` 历史(uid+file 分键,自动持久化),复用 assistant.py 的 AI/SSE/工具循环骨架(import,**不改 assistant.py**);前端 sendChat→agentic 循环(meta/notice/tool/answer/actions/task/undo/trace/error/done 全事件 + `window.epubHighlight`(文本定位→偏移锚→saveHl)+ runActions 跳章/跨书 + (第N章)linkify + trace ℹ️ + undo + 停止按钮 + rid 重连+看门狗+历史恢复 + loadHistory 回灌)。
- 共享模块共 **13 个**(rc-core/md/figures/highlight/dict/snippets/result/wordpop/settings/knowledge/assistant/sidedrawer + epub-html),全 node-check 过、全 200、epub-html 最后加载;PDF 阅读器页 200 + reader.js 在 + **零 rc-* 污染**。
- 小遗留(非阻塞):生词下划线默认开,设置面板的开关 UI 未接(localStorage `eph-vocab-underline`,默认开即工作);助手 make_anki/note 出处链接走 voice `_deep_link` 硬编码 `/pdf/view?page=`(指 PDF viewer 而非 epub,卡片内容正确);日语生词客户端最长匹配(无 epub JP 分词端点);「更强重答」force_model 已 plumb 无 UI 按钮。

### (历史)阶段顺序(依赖+风险) — 进度(2026-06-27 夜)
- ✅ **A/B/C/D 已集成部署**(每步 EPUB 200 + PDF 哨兵 200 + PDF 零引用):
  - A 头部对齐+scrubber+全屏+搜索浮层;
  - B `rc-result.js`(836 行,AI 结果模态+草稿+选段 picker,翻译/解释/对话走居中模态);
  - C 工具栏两栏(`#ep-hl-pick` 竖排色板 + `#ep-main` preview+按钮组)+ PDF 配色 `#fff59d/#a7f3d0/#a3d4ff/#fda4af` + 色板激活态 + 词组骨架隐藏;
  - D `rc-wordpop.js`(811 行,核心字典小框→展开三源英文 SSE/日语声调+汉字+AI深入,掌握/Anki;dict 分支改 `RC.wordpop.show`)。
  - 已建共享文件:rc-core/rc-md/rc-figures/rc-highlight/rc-dict/rc-snippets/rc-result/rc-wordpop(都在 `_epub_js_v()` cache-bust 元组)。
- ✅ **E 完成** `rc-settings.js`(4-tab:AI·翻译 model/effort/debug + 阅读 字号/主题/行距/转PDF + 语法占位 + 高亮颜色管理;Aa 与助手 ⚙ 都调 `openSettings(tab)`;AI 调用经 `RC.settings.aiParams()` 带 model/effort,含助手 epub-chat)。
- ✅ **F 完成** `rc-knowledge.js` + 后端 `/api/epub-nodes`(`_find_kg_nodes_for_book` 按书 stem/parent/sha 模糊匹配 kg.book,整本 level-2 节点;LADR→355、费曼→空优雅)。右侧 `#ep-side-handle`「知识点」+ `#ep-kg-panel` 磨砂抽屉 + `.kg-node` 卡 + 跳 skilltree + ☆跟踪;z 76/77 < #ep-sel 78。
- ✅ 助手 `#ep-ai-head` 加 ⚙ 模型设置入口。
- ✅ **H 部分完成(P0)**:`rc-assistant.js`(splitFollowups/renderFollowups/contextCard,自包含)。EPUB 助手已加:**追问 chips**(后端 epub-chat prompt 加 `[[FOLLOWUP]]a|b|c[[/FOLLOWUP]]` 指令 + 前端流式剥标记 + 渲 chip 点击发)、**上下文卡**(每条提问显示「正在看:章 + 选中」)、**⚙ 模型设置入口**(`#ep-ai-head`)、AI 调用带 model/effort。
  - **H 暂缓项**:(P0-1)服务端/本地对话历史——`#ep-ai-body` 被助手聊天/高亮列表/笔记卡**共用**,载入历史会打架,需先拆独立助手 body(更大重构);(P1)SSE 断线重连(后端 `_start_ai_stream` 已就绪,前端 `sse()` 没重连;轻量版=切后台回前台从 `/api/ai-stream-result?id=rid` 拉完整结果);(P2)**section 级 agentic 工具循环**——PDF 的 `/api/assistant` 工具全绑死「页」,EPUB 要新开 `epub_assistant.py`(read_section/list_sections/goto_section/epub_highlight + 通用工具复用 + section 级 ctx/sys_prompt),独立中等工程(~1.5d);(跳过)mfx 逐字动画/图缩略卡/页码 linkify/`!` trace 梯子/连续语音(诚实评估 ROI 低或不适用,详见 task a29ada…output)。
- ⏳ **G(F1-F7 正文注入)暂缓**:G0 offset walker 加固是硬前置,注入 `<ruby>/<span>/<div>` 改文本节点偏移会**静默错位已有高亮**,无头环境无法可靠回归 → 留待真机(尤其日语书)验证再做;译页/振假名/生词对费曼(中文)价值低、风险高。已知缺口,非遗漏。已沉淀模块共 9 个(+rc-settings/rc-knowledge)。
- ⚠ 已知差距:H 的 agentic 工具循环依赖 PDF 页工具(read_page 等),reflow 需另写 section 级工具(后端 lift),暂保留 EPUB 现有 `/api/epub-chat` 简易聊天 + 升级 UI;F 的知识点对多数 EPUB 可能空(KG 按书名/sha 兜底匹配)。

#### (原始 A-H 详表)
- **Phase A ✅ 完成**:头部 `#ep-top` 视觉对齐 PDF `#header`(#10162a/#2a3550/可横滚 + .active 约定)+ 标题可点返回 + **章节 scrubber `#ep-scrub`**(横拖跳 section + `#ep-scrub-pop` 浮层,reflow 按 idx/%)+ **全屏 `#fs-toggle`/`#fs-restore`**(body.fs-mode)+ **搜索 `#ep-search` 改 PDF 居中浮层**(`#search-panel` 样式 + RC.debounce 防抖 + `#ep-search-stat` 计数 + Esc 关)。
- **Phase B(进行中,agent ae79… 写 rc-result.js)**:AI 结果模态 + 草稿 + 选段 picker。移植 `20-result-draft.js` + 21 的 `_aiStream/aiCall`(SSE+rid 重连)+ 17 的 `_followupAsk/markFromResult/ankiFromResult`。新 DOM `#result-mask/#draft-*`(z 200/300,不撞 #ep-*)。接线:selBar 的 translate/explain/chat → `RC.result.aiCall/openChat`;note/anki 不动(留 RC.snippets)。草稿 LS key `'epub-drafts'`。markHighlight 走 opts 适配器(EPUB saveHl)。
- **Phase D(进行中,agent ad1e… 写 rc-wordpop.js)**:字典两级。移植 15(核心小框)+ 19(三源英文 SSE + 日语声调/汉字/AI深入)。展开调 `RC.result.openResult`(依赖 B)。定位 charBox→opts.rect。接线:dict 分支 `RC.dict.show`→`RC.wordpop.show`;非英日留 onFallback。删 dead `#ep-dict` + `var dictBox`。
- **Phase C(待做,B 后)**:选中工具栏 `#ep-sel` 重构 = 两栏(左竖排色板激活态 + 右 preview「已选 N 字」+ word/multi/phrase 组)。色值改 PDF `DEFAULT_HL_COLORS`(#fff59d/#a7f3d0/#a3d4ff/#fda4af),同步 epub-html `HL_COLORS`。🔎OCR 永久省略(reflow 无)。📝笔记/🎴制卡 EPUB 多出的保留。**改现役工具栏,iOS 真机测换行**。
- **Phase E(待做)**:设置 4-tab modal `#ep-settings-mask`(AI·翻译/阅读/语法占位/高亮);现 `#ep-set` 字号/主题/行距/转PDF 移进「阅读」pane;颜色管理沉淀进 rc-highlight(getHlColors,LS `pdf-hl-colors`,改它要跑 PDF 回归);`openModelSettings` **复制**成独立 `rc-modelsettings.js`(别动 25-assistant.js);reflow 不适用项(页码对齐/去边/charbox 校准/OCR 重扫)不放。
- **Phase F(待做)**:知识点抽屉 `#ep-grammar-panel` + `#side-handle` 把手(照搬 #grammar-panel 磨砂 + `body.grammar-open` 挤压 + `>*{z-index:1}` 两行别漏)+ 新后端 `/pdf/api/epub-nodes`(先整本列表;**坑:_find_kg_nodes_for_page 按 obsidian 相对路径匹配 kg.pdf,EPUB rel 可能不匹配 → 先确认书归属 sha/book 名**)。
- **Phase G(待做,最高风险:往正文塞节点撞 offset 锚)**:G0 加固 offset walker(跳 `<rt>`/vocab span/mark,装饰不增删可见字符)是 G2-G5 硬前置 → G1 scrubber(=A2 已做)/ G2 F6 词组 / G3 F7 生词下划线(后端 `/api/vocab-mastery-map`)/ G4 F5 ruby 振假名(后端 `/api/epub-furigana`,原生 `<ruby><rt>`)/ G5 F3 译页(后端 `/api/epub-translate-section`,gtranslate_batch 不走 AI,块级 `.ep-tr-rt`)。每步在含多条高亮的真实书回归「高亮不漂移」。
- **Phase H(待做,最高:动线上 PDF 25-assistant.js + agentic 后端,用 worktree)**:助手 Copilot 全套(追问/反馈梯子/模型设置/上下文卡/SSE 断线恢复/历史/连续语音/停止/agentic 工具循环)。抽 rc-assistant **复制**而非原地改 25,PDF 暂不切。

### reflow 永久不适用(非漏做,有意省略)
⊞双页 / ↔适应宽 / ✂️去边 / 单页横滑翻页 / canvas pinch 缩放 / 🔎OCR / 文字层校准·单页重扫 —— 全依赖 PDF 页位图/char-layer,reflow 无对应。scrubber/搜索/译页/振假名/生词/词组/知识点 等用 reflow 等价物实现。

---

## 🧩 工具指示器 v2:扩展**现有那张语音卡片**(2026-07-14,用户设计)

⚠ **铁律(用户拍板)**:*不另造 UI*。用的就是现有的 `.vc-card`(DOM/CSS 在 `rc-voicecall.js`)——「我很喜欢这个方块的样式,在这个基础上进行修改就好」。`rc-toolchip.js` **只是状态机 + 内容渲染**,拖动 / 收藏 / TTS ▶ / ✕ / 紫边选中全部复用 `RC.voiceCard` 暴露的那张卡。

### 三形态(在原卡片的「方块 ↔ 长条」之上**加第三态:圆形标记**)

| 形态 | class | 语义 | 外观 |
|---|---|---|---|
| 标记 | `.vc-dot` | 创建 / 收起 | **圆角方形**(12px,套长条的外观材质);进行中=透明玻璃(`.vc-busy`),完成=有色磨砂(`.vc-typed`);图标呼吸 = 正在干活 |
| 长条 | `.vc-min` | 折叠 / 进行中 | **一行**(标题 + 状态 + ▶ + ✕),与标记**同高 40px** → 标记→长条 = 上下边不动、纯向右拉长 |
| 方块 | (无) | 展开 / 结果 | 长条→方块 = 纯向下伸长;正文 = **数据流图**(见下) |

### 形态循环的三条铁律(133,用户实测踩坑)

1. **单向**:顺序恒为 `小方块 → 长条 → 方块 → 小方块`(`_cycleForm()` 是唯一入口)。
   ⚠ 标记和头部**必须同方向** —— 长条态标记是隐藏的、只有头部可点;当初头部写成反向(`full→min→dot`),
   于是 长条 →点头部→ 小方块 →点标记→ 长条 = **死循环**,永远到不了方块。
2. **只认短按抬手**:`pointerdown` 记时刻,抬手时若已达长按阈值(600ms,与 `_pinBind` 同口径)或被判为拖动 → **不改形态**。
   长按 = 选中、拖动 = 移动,这两种松手都不该改形态(旧版绑在 `click` 上,长按松手也触发 → 误切)。
3. **双击已退役**(三态卡):单击就是三态循环,双击会连触发两次、还跟旧的 `dblclick` toggle 打架。
   非三态卡(普通文字卡 / 收藏夹拖出的副本)保留旧的双击折叠。

### 展开视图 = 数据流图(用户设计,不是一坨纯文本)

`AI 请求 → 各处理步骤 → 结果` 每一环是一个**可点开的小方块**(`.vc-fn`,图标是 Apple 线条 SVG,**不用 emoji**),之间用**带箭头的连接线**(`.vc-fw`)表示数据传递。点方块展开它经手的载荷(`.vc-fp`):

- **AI 请求** → 指令(S2S 原话) / 携带上下文 / 参数,全部 markdown 渲染
- **步骤** → 该步详情
- **结果**(默认展开)→ markdown + 公式 + 图正常渲染(`RC.assistant.renderMd` + MathJax;侧栏未挂载时 `miniMd` 兜底,**绝不吐原始文本**)。工具回给模型的常是 JSON(`{"kind":"weather","note":…}`)→ `prettyResult()` **抽人话**再渲染。制卡则是完整卡片预览(正反面翻页 / Cloze,卡面走 `mdInline`)。

### 按钮纪律(用户拍板)—— 标题栏按钮清单

| 卡 | 标题栏按钮 | 说明 |
|---|---|---|
| **工具卡**(字幕浮层 + 侧栏) | 只有 **数据流** `.vc-flowb` | 点=展开/收起流程图。**没有 ▶ / ✕**(它是过程指示,不是可播可删的内容卡) |
| **结果卡**(天气/搜索/配图/视频,浮层 + 侧栏) | 只有 **数据流** `.vc-flowb` | 点=卡内展开流程图。遗留的 ⠿ 拖动按钮已删(整条头部本来就是把手) |
| **普通文字卡**(AI 文字回复,非工具产物) | ▶(TTS) + ✕ | 不变 |

- **唯一保留 ▶ 的另一处**:纯文字最终结果那块内容的**角落**(`.vc-fp-tts`),点它 TTS 念。
- 展开成长条/方块后,左上角那枚**标记按钮不再显示**;形态切换改点头部或数据流按钮。
- ⚠ 白块坑:`.vc-flowb` 的样式在 `injectCss()` 里 —— 历史回放/侧栏先出卡时通话 UI 可能还没建过,
  样式没注入,按钮就成了**裸 `<button>`(白块)**。`_infoCardEl()` 和 `hdSplit()`/`mountFlow()` 都先调
  `injectCss()` / `RC.voiceCard.css()` 兜住。

### 结果卡也走同一套三态(132,用户)

天气 / 配图 / 视频 / 新闻这类**自带结果卡**的工具,卡片本身也接进三态系统:

- **浮层**:`_cardPush(..., {dot:true, form:'full', noAuto:true, type/icon})` → 带标记、进 `#vc-tlayer`(左上角锚定)、**以方块出生**,单击**头部**三态循环 `方块 → 长条 → 小方块 → 方块`(展开时标记隐藏,所以**头部就是形态按钮**;到了标记态整张卡就是那枚标记,点它继续循环)。颜色/图标复用 `RC.toolChip.styleOf(kind)`。
- **侧栏**:点 `.vc-if-hd` 折叠成一行(`.vc-if-min`),即 长条 ↔ 方块 两态(侧栏不要标记)。

### 字幕模式不再弹「AI 文字输出」卡(132,用户)

字幕本来就在显示 AI 的文字输出 → 文字输出档下**不再另弹**「文字回复 / 路由详答」浮层卡(重复且挡内容)。三处 `_cardPush` 已撤。

### 结果卡吸收(天气 / 网络搜索 / 配图 / 视频)

这类工具**本来就有自己的结果卡**(`renderInfo`)。以前工具指示器还会另造一张 → 字幕模式一次弹**两张**。现在:

- `renderInfo()` 建完卡就调 `RC.toolChip.absorb(hosts)` → 认领最近那个未被吸收的 chip,**撤掉它自己的浮层+侧栏视图**;
- 在结果卡标题栏挂一个**「流程」按钮**(`.vc-flowb`),点开就在卡片内展开那条线性流程图(`.vc-flowbox`);
- 结果卡的标题栏顺手统一成我们方块的头部样式,**删掉遗留的 ⠿ 拖动按钮**(整条头部本来就是把手)。

### 选中 = 按 cid 全局广播

`_pins.byCid`(cid → 所有实例 el)+ `_pinReg` / `_pinPaint`:同一张卡在**浮层 / 侧栏 / 收藏夹**处处高亮、处处取消。这修掉了用户报的「字幕里选中了,侧栏同一张卡没高亮」。

### 颜色 = 输出类型(紫色只表示选中,不参与类型编码)

`TYPE_C`:anki `#39d98a`(制卡单独一色)/ text `#7b9cff` / image `#c77dff` / video `#ff7a59` / weather `#2dd4bf` / news `#fbbf24` / **action `#8194b8`**。
`typeOf(toolName)` 按真实工具名分类。**执行类**(`goto_*` / `highlight` / `epub_highlight` / `auto_highlight` / `open_book` / `undo_last`):浮层上无方块、不可选中、**完成即消失**;但**侧栏里保留**(点开 = 完成后的状态确认)。

### 数据链路(内部步骤全推出来)

```
后端 _run_snippets_to(on_step=…) → voice._task_anki → _vtask_set(step=, steps=[])
   → GET /api/voice/task-status → {step, steps[], result:{kind:'anki', n, deck, cards[]}}
   → RC.toolChip.track(chip, task_id) → 步骤进长条,结果进方块
```

- 方块底部的「步骤」区(`.vc-stp`)= **原「!」详情面板的内容**:steps 列表 + 参数 / 耗时 / 喂回给模型播报的结果。
- **结构化 `tool2` 事件**(`assistant.py::_tool2`,与旧 `tool`/`tool-done` 并行发,老前端忽略):`{name, label, args, status, sec, brief, task_id}`,发在 assistant 的 3 个驱动(claude/gemini/codex)+ epub_assistant 的 2 个驱动。**文字对话**(`rc-assistant.js::_toolChip`)与**语音通话**(`rc-voicecall.js::_chipStart/_chipEnd`)由此共用同一套卡。
- 🗑 清空对话 → `RC.toolChip.clearAll()`。字幕框不再重复显示工具状态(卡片是它的高级替代)。

### API(`RC.voiceCard`,rc-voicecall 暴露)

`push(text,label,isHtml,force,cid,opts)`(`opts={tool,type,icon,dot,noAuto}`)/ `close` / `form(el,'dot'|'min'|'full')` / `layout` / `mkCid` / `pinReg` / `pinBind` / `dragToDock` / `sideOpen`。

### ⚠ 部署

`rc-toolchip.js` 必须同时进 `pdf_reader.py` 的 **`_epub_js_v` 和 `_pdf_shared_js_v`** 两个缓存击穿清单——漏了会让阅读器永远跑旧缓存 JS(EPUB 曾因 `rc-voicecall.js` 漏在 `_epub_js_v` 里,导致「设置面板没有语音 tab」排查一整轮)。前端只由 nginx 从 `/var/www/html/static/pdf/` 服务。


---

## 🎙 通话录音的切分:必须按**真实播放边界**,不能按文字时间线(2026-07-14,用户实测)

用户症状:连续两段回复的录音,按 ▶ 播出来拼在一起是**第一段的前半部分**。

**根因**:录音(remote 轨 `MediaRecorder`)的起止是按**文字时间线**驱动的——
- 开录 = 首个音频转写 delta;
- 停录 = `response.done` 时按「字数 ÷ 5.5 字/秒」**估算**播放结束再延时 stop。

但转写 delta 跑在音频播放**前面**很多(生成快、播放慢)。于是开录时**上一轮的音频还在播**、停录时本轮才播了个开头 → 录到的是「上轮尾巴 + 本轮开头」,长度还全靠猜。

**正解**:用 WebRTC 专属的官方事件(API reference 里没写,但实际在发):

| 事件 | 含义 | 动作 |
|---|---|---|
| `output_audio_buffer.started` | 模型音频**真正开始播** | `_recStart()`(clipId 此刻就发,落库不用等录完) |
| `output_audio_buffer.stopped` | 音频**播完** | `_recStop()` → blob 完整,上传 |
| `output_audio_buffer.cleared` | 音频**被打断** | `_recStop()`(半截也收下) |

- `response.done` 只取 clipId 落库,**不再掐录音**(那时音频还在播)。
- `_rec.oab` 标志:一旦见过任一 `output_audio_buffer.*` 就走新路;万一环境不发这组事件,回退老的 delta/估算路径。
- **看门狗**:社区实测 `.stopped` 偶发大延迟甚至不来 → `response.done` 后按「估算播完 + 20s」兜底强停,免得录音器一直转。

Sources: [community.openai.com — output_audio_buffer.stopped 未文档化](https://community.openai.com/t/why-is-the-realtime-server-event-output-audio-buffer-stopped-not-documented/1132028) / [.stopped 延迟报告](https://community.openai.com/t/big-delays-receiving-output-audio-buffer-stopped-event-since-apr-18th/1379823)
