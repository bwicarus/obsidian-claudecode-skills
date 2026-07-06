# 助手侧栏彻底共享(PDF + EPUB 一份代码)

> 目标:PDF 阅读器与 EPUB 阅读器**用同一份助手侧栏代码**,差异全部由 adapter 吸收。
> 用户拍板方向(2026-07-06):不是对齐按钮,而是**彻底共享整个侧栏**。
> 铁律:中间层去适应旧代码、完全复用 PDF 阅读器原有全部能力,不为不同阅读器另造上层建筑。

## 为什么(现状=两份独立实现)

| | PDF | EPUB |
|---|---|---|
| 侧栏主体 | `reader.src/25-assistant.js`(编进 `reader.js`,IIFE 自建 DOM) | 写死在 `templates/epub_html_reader.html`(`ep-asst-quick`/`ep-ai-body`/`ep-ai-ta`) |
| 发送/流式/工具循环 | `reader.js` 内联一套(`send`/`runActions`) | `epub-html.js` 内联另一套(`sendChat`/`runAssistant`) |
| 挂载前置 | 硬依赖 `#grammar-panel` + `#side-tabs`(缺则整段 return) | `ep-side` 抽屉 |
| 真正共享的 | 仅 `rc-assistant.js`(追问 chips / 上下文卡片 / ⚙模型面板 / paidNotice)+ `rcBuildMediaRow` + 同一组 `/api/assistant/*` 端点 | 同左 |

后果:改一处漏一处就分叉(「总结本页/本页生词」当初只从 EPUB 删,PDF 没删)。

## 分阶段(每阶段可回退,保留 `?ui=legacy` 逃生)

- **①(done, commit 见 tasks #249)**:快捷按钮栏抽成共享构建器 `window.rcBuildQuickBar(container,{knowledgeSend,knowledgeLabel})`(在 `rc-assistant.js`)。PDF/EPUB 都调它 → 按钮单一来源永不分叉。历史「总结本页/本页生词」去掉。只产 markup 不绑事件(点击走各 reader 容器既有委托),零耦合。
- **②a**:在 `reader.src/25-assistant.js` **原地** adapter 化——所有宿主绑定点(见下「契约」)改走一个 `AsstHost`。PDF 的 `PdfAdapter`(`27-rc-adapter.js`)提供**纯转发**到现有 window 函数 → PDF 行为零变化。build+deploy+PDF 全功能回归。
- **②b**:把 adapter 化后的侧栏主体**机械搬进** `rc-assistant.js`(pyflakes 式验零漂移思路)。`reader.src/25-assistant.js` 变薄壳/移出 build。PDF 加载 `rc-assistant.js` 侧栏,经 `PdfAdapter` 自挂载。PDF 再验一次。
- **③**:EPUB 实现同一套 `AsstHost` 接口(章=section 语义)挂共享侧栏,**退役** `epub-html.js` 内联 `sendChat/runAssistant/流式` + 模板内联 DOM。EPUB 全功能回归。
- **④**:文档 + 清 dead code + memory。

## AsstHost adapter 契约(来自 25-assistant.js 宿主绑定点全量清点)

一个 reader 一个实例。分组(PDF 转发目标 / EPUB 映射):

**位置/导航(吸收全部 页↔章 语义)**
- `getCurrentLocation()` → 归一化 locator(PDF=页 index;EPUB=section/CFI)
- `goToLocation(loc,{back:true})` ← `jumpWithBack`
- `goToLocationInBook(fileRel,loc)` ← `_jumpToCtx`/`openBookAt`(拥有 `/pdf/view?...` 跨书路由)
- `dispFromInternal(loc)` / `internalFromDisp(disp)` ← `_dispPage`/`_pdfFromDisp`(EPUB 恒等/章标签)
- `locationCount()` ← `pdfDoc.numPages`(链接范围钳)
- `linkifyLocations(el)` ← `_linkifyPages`(正则「第N页」vs「第N章/节」+ 链接语义 per-reader)
- `prevLocation()/nextLocation()/fitWidth()/zoomBy(d)/toggleLocationTranslate()` ← 快捷导航

**内容/选区/上下文**
- `getContext()` → 归一 context(`page_type`/selection/当前位置/figures/file_rel/focus_sel…);已部分统一在 `RC.adapter().getContext()`
- `getSelection()` / `setFocusSelection(text,kind)` ← `__setFocusSel` / `clearFigureFocus()` ← `__clearFigFocus`
- `nearText(anchor)` ← `_noteNearText`(字符层/页特定)
- `figureThumb(desc,imgEl,live)` ← `__figThumb`;`fileRel()` ← 裸 `FILE_REL`

**批注(拥有 `/pdf/api/*` URL + page/section 字段映射)**
- `reloadNotes()` ← `notesReload`;`reloadHighlights()` ← `_reloadHighlights`
- `createHighlight/deleteHighlight/redoHighlight`、`createNote/editNote/deleteNote` ← 包 `/pdf/api/highlights`、`/pdf/api/notes`,映射 `page`↔location
- `noteComposite(id)` ← `/pdf/api/note-composite`;`flashSelectionAt(loc,text)` ← `_flashSelOnPage`

**UI 宿主接线**
- `openDrawer()`/`switchToAssistantTab()`/`isAssistantOpen()` ← `openGrammarPanel`/`switchSideTab`/`__asstOpen`;adapter 供挂载点(`#grammar-panel`/`#side-tabs` 等价物)
- `renderMarkdown(el,text)` ← `md`;`typesetMath(el)` ← `MathJax`;`toast(msg)` ← `_toast`
- `buildQuickBar(el)`(已由 `rcBuildQuickBar` 共享)、`mediaPrefer()` ← `rcMediaPrefer`、`noBook()` ← `rcNoBook`

**保持共享(已 reader 无关,不用进 adapter)**
- 后端端点 `/api/assistant/{chat,history,undo,clear,prewarm,action-pref[s]}` + `/api/voice/task-status`
- SSE 事件协议:`meta/tool/answer/notice/gemini-paid/actions/trace/task/undo/done/error`
- ⚠ `actions` 事件里后端按名调 `window[fn]`(如 `notesReload`)→ 共享层必须把这些路由到 **adapter**,不能依赖全局命名空间。

## 关键坑

- `reader.js` = `scripts/build_pdf_reader_js.sh` 拼 `reader.src/*.js`(含 25-assistant + 27-rc-adapter)。改 reader.src 后必须 rebuild + `check_pdf_reader_js.sh` + 部署 `reader.js`。
- `PdfAdapter` 已有 `bind({...})` → `_host` 袋 + `RC.use(PdfAdapter)` 注册 + `RC.adapter()._host` 取。②a 就是往这个 `bind` 补齐上面所有方法。
- 静态由 nginx `/var/www/html/static/pdf/` 服务;`:5000` Flask static 陈旧。验前端用 nginx curl + `diff -q`。
- EPUB 无 legacy 模式(恒加载 rc-*);PDF 有 `?ui=legacy` → 共享层不加载,25-assistant 必须留 native 兜底。
- 便签透明度:PDF 缩放用祖先 CSS `zoom`/`transform` → `backdrop-filter` 磨砂失效(平台限制),EPUB 无此问题。收口时评估是否换不依赖磨砂的透明方案。

## PDF↔EPUB 助手分叉清单(2026-07-07 审计,39 条 → 3高/11中/12低/7域差异)

> 收口成一份代码前的**行为对齐清单**(逐条抹平 → 保证搬迁不丢功能)。✅=已修。

**HIGH**
- ✅ H1 no_book「书页」开关 EPUB 失效(前端发了后端零处理)→ `_ectx_block` 加 no_book 分支
- ⬜ H2 写操作撤销/重做卡 PDF 刷新即丢(EPUB 已持久化)→ PDF 补 action 事件+`_convo_append` 白名单+`/pdf-action`+loadHistory 回放(大工程)
- ⬜ H3 EPUB 无 see_page 章节级看图 → 加 section 级 rasterize 截图管线+后端 vision(大工程)

**MEDIUM**
- ⬜ M1 langs 书语言未注入 EPUB(getContext+meta)
- ⬜ M2 visible_vocab 本页生词未注入 EPUB
- ✅ M3 讲这里别读整章(EPUB `_ESYS_RULES` 静态铁律)
- ✅ M4 read_selection 工具 EPUB 缺(一行注册复用 PDF)
- ⬜ M5 make_anki 制卡后无 CFI 回链高亮(EPUB)
- ⬜ M6 make_note 无回链高亮+无选中不回退整章(EPUB)
- ⬜ M7 find_highlights 范围参数(sections/from-to)EPUB 受限
- ⬜ M8 用户气泡带入图缩略图不落库不回放(EPUB `figures` 白名单+回放)
- ⬜ M9 高亮列表卡删除后不可恢复(PDF 缺重做,抄 EPUB)
- ⬜ M10 空/清空后欢迎语 greet() EPUB 缺
- ⬜ M11 页面级 see_ink(gated:先确认 EPUB 正文有无自由墨层)

**LOW**(快改批)
- ⬜ L1 see_figure 描述漏 {index?}(PDF 文案)· L2 公式收尾句(EPUB 已近似)· L3 tool-done 前端切「思考中」(PDF)· L4 gemini-paid 兜底 showNotice(EPUB)· L5 🗑清空流式守卫(PDF)· L6 空输入分流『讲讲这个便签』(EPUB)· L7 便签 chip 缩略图点开大图(EPUB)· L8 追问/反馈错峰淡入(EPUB,可收进 rc-assistant)· L9 focus_sel 独立通道(可仅登记)· L10 历史「选中」精确锚(PDF)· L11 prewarm(off)(EPUB)· L12 删冗余本地 chat[](EPUB)

**域差异/已对齐(不改)**:read_source_page(收藏集专属)· page↔section 命名 · see_page 省额度 · 落库位置死字段 · from/page_type 首连 · 收藏集 NotebookLM · rc-video ui_shared 门控(仅 legacy)

> 已修/独立修过的相关项:快捷栏共享 `rcBuildQuickBar`、视频历史回放、PDF visible_text 采集、工具 JSON 流式不泄漏+带前导散文也执行、找视频某国母语。
