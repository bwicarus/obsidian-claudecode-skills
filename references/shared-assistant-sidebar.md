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
- ✅ M1 langs 书语言注入 EPUB(getContext+meta)
- ⬜ M2 visible_vocab 本页生词未注入 EPUB
- ✅ M3 讲这里别读整章(EPUB `_ESYS_RULES` 静态铁律)
- ✅ M4 read_selection 工具 EPUB 缺(一行注册复用 PDF)
- ✅ M10 空/清空后欢迎语 greet() EPUB(下方 M10 项)
- ✅ M5 make_anki 制卡后绿色回链高亮(EPUB)
- ✅ M6 make_note 蓝色回链高亮 + 无选中回退整章(EPUB)
- ✅ M7 find_highlights 支持 sections 列表 + from/to 区间(EPUB)
- ⬜ M8 用户气泡带入图缩略图不落库不回放(EPUB `figures` 白名单+回放)
- ✅ M9 高亮列表卡删除后「↪重做」(PDF,对齐 EPUB)
- ✅ M10 空/清空后欢迎语 greet() EPUB
- ⬜ M11 页面级 see_ink(EPUB 正文**有**自由墨层 _epInk/api/epub-ink → 可做;但需 H3 的栅格化管线,与 H3 合并做)

**LOW**(快改批;✅ L3/L4/L5 已修)
- ⬜ L1 see_figure 描述漏 {index?}(PDF 文案)· L2 公式收尾句(EPUB 已近似)· L3 tool-done 前端切「思考中」(PDF)· L4 gemini-paid 兜底 showNotice(EPUB)· L5 🗑清空流式守卫(PDF)· L6 空输入分流『讲讲这个便签』(EPUB)· L7 便签 chip 缩略图点开大图(EPUB)· L8 追问/反馈错峰淡入(EPUB,可收进 rc-assistant)· L9 focus_sel 独立通道(可仅登记)· L10 历史「选中」精确锚(PDF)· L11 prewarm(off)(EPUB)· L12 删冗余本地 chat[](EPUB)

**域差异/已对齐(不改)**:read_source_page(收藏集专属)· page↔section 命名 · see_page 省额度 · 落库位置死字段 · from/page_type 首连 · 收藏集 NotebookLM · rc-video ui_shared 门控(仅 legacy)

> 已修/独立修过的相关项:快捷栏共享 `rcBuildQuickBar`、视频历史回放、PDF visible_text 采集、工具 JSON 流式不泄漏+带前导散文也执行、找视频某国母语。

## ②b 进展(2026-07-07):PDF 侧栏已搬进共享层,flag 门控

- `reader.src/25-assistant.js` 整份侧栏(1300 行)机械搬进 `rc-assistant.js` 的 `RC.assistant.mountPdfSidebar()`:裸全局(md/_toast/_qhFmtTime/loadAllHighlights/renderHighlightsOnPage/renderPhraseHl/_removePhraseHighlight/_charsRangeToText/_charRangeToPtRects/openGrammarPanel)顶部从 `PdfAdapter._host.asst` 别名;live 值 FILE_REL→fileRel()/pdfDoc→pdfNumPages()/_activePhraseHl 读→activePhraseHl()·写→setActivePhraseHl();window.* 原样;本地函数照搬。
- **门控 `?asst=shared`**:默认无 flag=老 25-assistant(零风险);flag=老版自退、27-rc-adapter 在 bind 后调 mountPdfSidebar。
- 对抗验证(general-purpose agent)**零遗漏裸全局**,node --check 过,已部署(默认不变)。
- **待办**:浏览器 `?asst=shared` 跑一轮全功能确认 = 老版 → 翻默认(25-assistant 的 flag 逻辑反转/直接退役)+ 27-rc-adapter 无条件 mount。然后 ③ EPUB 提供 `asst` 袋复用这份 mountPdfSidebar(或抽成 mountSidebar(host))。

## ③ 施工图(EPUB 复用共享侧栏)——mountPdfSidebar 里待泛化的 PDF 专属点

> ②b 已把 PDF 侧栏搬进 `rc-assistant.js::RC.assistant.mountPdfSidebar()`(默认生效)。③ = 把它泛化成 reader 无关的 `mountSidebar(host)` + EPUB 提供 host + 退役 EPUB 内联引擎。规模≈②b。

**待路由到 host 的 PDF 专属点**(在 mountPdfSidebar 体内):
- **挂载点/DOM**(15):`#grammar-panel`/`#side-tabs`/`#side-pane-asst`/`#asst-quick`/`#asst-thread` → host.mountPoints()(EPUB=`#ep-side`/`#ep-asst-quick`/`#ep-ai-body`…)。或统一模板 id。
- **页↔章语义**:`「第N页」`正则(174)+ 显示(774/784/808/854)、`window.jumpWithBack`(6)、`window._dispPage`(4)、`window._pdfFromDisp`(2)、`window.changePage`/`zoomChange`、`/pdf/view?file=&page=`(695 跨书)、`page_type:'pdf'`(567)→ host.goTo/dispLoc/locLabel(EPUB=章 idx/CFI,「第N章」)。
- **端点**:`/pdf/api/highlights`(×4)、`/pdf/api/note-composite`、`/pdf/api/notes` → host.endpoints()(EPUB=`/pdf/api/epub-*` / epubHighlight client_action)。
- **window.* ~30**:jumpWithBack/switchSideTab/changePage/zoomChange/_reloadHighlights/notesReload/__noteAttached/__clearNoteAttached/__setFocusSel/__voiceContext/__figThumb/renderVideos/rcMediaPrefer/rcNoBook/rcBuildQuickBar/rcBuildMediaRow… — PDF 上 window 有、EPUB 没有 → 全改 HOST.xxx(asst 袋已有大部分转发,补齐缺的)。
- **发送/流式/工具循环**(send/loop/SSE):端点 `/api/assistant/*` 已 reader 无关(EPUB 后端 epub_assistant 有平行 `/pdf/api/epub-*`?需核)——host.chatEndpoints()。

**做法**:同 ②b 用脚本把 `window.jumpWithBack(`→`HOST.goTo(` 等批量路由(asst 袋补齐 goTo/dispLoc/switchTab/changePage/zoomBy/reloadHighlights… 已多数就位);挂载点 + 页章显示串 + 端点经 host。PDF 的 host 转发到现有 window.*(零回归);EPUB 的 host 映射到自身。flag 门控(?asst=shared 已可复用)+ 浏览器验证 → 翻默认 → 退役 epub-html 内联 sendChat/runAssistant。

## ③ 进度更新(2026-07-07 续)

- ✅ **③-1 导航/上下文层泛化**(部署):~28 处 PDF 专属 window.* 纯调用 → HOST.xxx(PDF 转发零回归;typeof 守卫→EPUB 兜底)。
- ✅ **③-2 注解端点抽象**(部署):`/pdf/api/{highlights,notes,note-composite}` → HOST.hlUrl()/notesUrl()/noteCompositeUrl()(asst 袋加这 3 方法,PDF 返回原 URL)。body 语义差异(rects/cfi)由各 reader 后端 + rects guard 处理。
- ⬜ **③-3 挂载点 DOM 抽象**:侧栏在 `#grammar-panel`/`#side-tabs` 里建 tab/pane/`#asst-quick`/`#asst-thread`/`#asst-input`;EPUB 是 `#ep-side` 内静态模板 DOM(`#ep-ai-body`/`#ep-asst-quick`/`#ep-ai-input`)。需 host 提供挂载容器/或统一模板 id;`「第N页」`显示串按 host.locLabel。
- ⬜ **③-4 EPUB host + 挂载 + 退役内联**:EPUB 实现 `EpubHtmlAdapter._host.asst`(~48 方法映射到 section/CFI/ep 端点/ep DOM)→ `mountSidebar` 挂进 `#ep-side` → 退役 epub-html.js 内联 sendChat/runAssistant/流式 → flag(?asst=shared)门控 + 浏览器验证 → 翻默认。**这是最后一大块,动 EPUB 现有可用助手,需谨慎 + 浏览器验证。**

> mountPdfSidebar 的 PDF 专属点现只剩:挂载点 DOM + `「第N页」`显示串。导航/上下文/注解端点已全经 HOST。

## ③-4 EPUB host 映射表(实现蓝图,原语已摸清)

> 在 `epub-html.js` 的 `EpubHtmlAdapter`(4526)加 `_host = { asst: {...} }`,注册处 4552(`RC.use(EpubHtmlAdapter)` 后)。EPUB 挂共享侧栏 = `RC.assistant.mountPdfSidebar()`(host 经 RC.adapter()._host.asst),flag 门控。

| asst 方法 | EPUB 原语/映射 |
|---|---|
| md(t) | `renderMd(t)`(142) | toast(m) | `toast(m)`(3076) | fmtTime | 内联简单格式化 |
| fileRel() | `FREL` | pdfNumPages()/locCount() | `COUNT`(总节数) | dispPage/pdfFromDisp | 恒等(章 idx=显示) |
| goTo(loc) | `jumpTo(loc)`(357) + `_drawerAfterJump()` | goToInBook(fr,loc) | `location.href='/pdf/epub/view?file='+fr`(跨书) |
| changePage(d) | `jumpTo(_curTopIdx+d)` | fitWidth/zoomBy/toggleTranslate | EPUB 无→no-op 或字号 |
| openDrawer() | `RC.sidedrawer.open('asst')` | switchTab(n) | `RC.sidedrawer.open(n)` | asstOpen() | `.ep-side-pane[data-pane=asst].active`(1253) |
| mountPanel() | `#ep-side` | mountTabs() | EPUB tab 栏(RC.sidedrawer 建;需确认 id) |
| voiceContext() | null(EPUB 走 getContext) | noteAttached() | `window.__noteAttached`(1406) | clearNoteAttached/renderNoteChips | 同名(1437/renderNoteChips) |
| notesReload() | `window.notesReload`(1729) | noteInject(n) | EPUB `__noteInject`(需确认) | assistEdit(d) | `_epShowAction`(2145)/`_epAssistEdit` |
| reloadHighlights/loadAllHighlights/renderHighlightsOnPage | EPUB 高亮重载(需确认函数名) | showHlPicker | 共享 `window._showHlPicker`(侧栏自带) |
| flashSelOnPage(p,t) | `_jumpFlashSel`(需确认签名) | noteNearText | EPUB 便签近文(需确认) | jumpToCtx | `jumpTo`+drawer |
| renderPhraseHl/removePhraseHighlight/activePhraseHl/setActivePhraseHl/charsRangeToText/charRangeToPtRects | PDF 字符层/词组高亮专属 → **EPUB no-op**(侧栏内 _flashSelOnPage 会因此降级,可接受或用 EPUB _jumpFlashSel 替) |
| prewarm(off) | EPUB prewarm(需确认) | getPaidNoted/setPaidNoted | `window.__paidNoted` |
| hlUrl() | `/pdf/api/epub-highlights`(1209) | notesUrl() | EPUB 便签端点(需确认) | noteCompositeUrl() | `/pdf/api/note-composite`(共享,1440) |

**③-4b 风险点**:①退役 epub-html 内联 `sendChat/runAssistant/流式`(改成共享侧栏的 send);②抽屉集成——共享侧栏 pane class 是 `.side-pane`,EPUB 抽屉认 `.ep-side-pane`(要么侧栏 pane class 也经 host、要么 EPUB CSS 认 .side-pane);③模板 `#ep-asst-quick`/`#ep-ai-*` 静态 DOM 与共享侧栏自建 `#asst-*` 冲突(EPUB 模板去掉静态助手 DOM,让共享侧栏建)。**必须 flag 门控 + 浏览器逐功能验证再翻默认。**

## ③-4 实施结果(2026-07-07 完成 flag 门控切面)

**调研把范围大幅收窄**——原以为要映射 ~48 方法 + 重写内联流式,实测只需「3 端点 + tab 守卫」:

- ✅ **③-3 挂载点抽象**:mountPanel()→`#ep-side`、mountTabs()→`#ep-side-tabs`(id,曾误写 `.ep-side-tabs` class 找不到已修)。侧栏自建 tab(`.side-tab`)+pane(`.side-pane#side-pane-asst`,靠 `#side-pane-asst.active{display:flex}` 自带 CSS 显示)。
- ✅ **③-4a EPUB host**:`EpubHtmlAdapter._host.asst`(~50 方法转发 EPUB 本地原语;页↔章语义 dispPage/pdfFromDisp 恒等;PDF 字符层/词组高亮专属→no-op;flashSelOnPage→`_jumpFlashSel`)。纯新增。
- ✅ **③-4b 端点路由 + flag 挂载**:
  - **`ctx()` 早已经 `RC.adapter().getContext()`**(rc-assistant 938)→ EPUB context 形状由 `EpubHtmlAdapter.getContext()` 产出,**最难的一环本就就位**。
  - **SSE 事件协议两后端本就同款**(meta/tool/answer/actions/undo/trace…);`runActions` 是 `window[a.fn].apply()`=**reader 无关**:EPUB `window.epubHighlight`(专属画法,不被覆盖)被正确派发;shared 覆盖的 `notesReload`→`RC.stickynote.loadAll`、`_reloadHighlights`→`HOST.loadAllHighlights` 都 reader 无关。
  - **只有 3 端点真不同** → 经 HOST:`chatUrl`(`/api/assistant/chat`↔`/pdf/api/epub-assistant`)、`historyUrl`(`…/history`↔`…/epub-convo?file=`)、`clearUrl`(`…/clear`↔`…/epub-convo/clear?file=`)。PDF 默认=原字面量(零回归)。
  - **undo / prewarm / action-pref[s] / voice task-status 两阅读器本就共用**,无需路由(EPUB `.ep-asst-undo` 早打 `/api/assistant/undo`)。
  - history 顶层 `{ok, messages}` 两后端已一致(epub-convo GET 2101)。
  - tab 守卫 `window.switchSideTab && HOST.switchTab` → `HOST.switchTab &&`(EPUB 无该 PDF 全局)。
  - 后端 `_eassistant_convo_clear` 加 `request.args` 取 file 兜底(共享 clear POST 无 body)。
  - **flag `?asst=shared`**(epub-html.js 末尾):摘内联 asst pane(`#ep-side-asst`)+ RC 建的内联 asst tab → `mountPdfSidebar()` 自建 → 补 EPUB 抽屉 class(`.side-tab`→`.ep-side-tab`,pane 加 `.ep-side-pane`)→ `setTab` 认得。默认无 flag=完全走内联助手=零影响。

**验证状态**:reader.js build+check、node --check ×3、python 语法、PDF host 返回原端点、webapp restart 200、nginx 静态=源 —— **全过**。前端行为等价**须浏览器验证**(此环境无浏览器)。

**浏览器逐项验证清单(EPUB 开 `?asst=shared`)**:① 抽屉「助手」tab 出现且点开显示共享 pane ② 发消息流式回答(打 epub-assistant)③ 快捷栏知识点/清空/模型/媒体行 ④ 历史刷新回放(文本+视频)⑤ 选中→AI 高亮画得出(`window.epubHighlight` 派发)⑥ 制卡/笔记撤销卡 ⑦ 清空清服务端历史。

**已知小缺口(记录,非阻断)**:① history 内 **action-card 回放**(EPUB 存 `actions` 非 `undo_cards`)② per-msg **section-jump chip**(存 `section` 非 `page`,`_ctxCard` 读 `page`)③ 内联 `onTab('asst')` 在 flag 下仍空跑一次 loadHistory(渲进已移除 DOM,无害)。

**翻默认步骤**(浏览器验证通过后):epub-html.js 把 `var _uSh = …indexOf('asst=shared')>=0` 改成恒 true(或去掉 flag 判定直接挂),再从模板删静态 `#ep-side-asst` 内联助手 DOM + 从 epub-html.js 删内联 `sendChat/runAssistant/流式/quick绑定/mic`(退役内联),部署验证。

## ③-4c 翻默认 + 首轮实测修复(2026-07-07,用户验证「用起来没问题」)

**EPUB 共享侧栏已是默认**;逃生舱 `&asst=inline` / `#asst=inline`(反转前是 `asst=shared` 门控)。

首轮浏览器实测暴露并修复的三类问题(全在 commit 46 前后两个提交里):
1. **图片 502 洪泛**:流式每个 answer 事件全量重渲把 `<img>` 反复销毁重建 → 同 URL 十几个并发慢代理请求打满 gunicorn → 502。修:renderMd 流式期间 img→占位符、收尾才真渲;`rcImgStabilize`(rc-video)+ 全局失败表 `__rcImgFail`;img-proxy 磁盘缓存(`state/img-proxy-cache/`)+ 同 URL single-flight;**hash 修复分支补上原图 URL**(上次只认 /thumb/,AI 编原图路径错 hash 直穿 502)。实测:错 hash 原图冷取 200/1.15s,热取 0.002s。
2. **流式动效消失**:揭示游标 CSS 在 `mfx.css`,只有 PDF 模板引 → rc-assistant 检测不到 mfx.css link 时自动注入等价规则(颜色用实值不依赖 var;PDF 不注入零重复)。
3. **选中/书页上下文断线**(三层):`getContext` 无参时 EPUB 不采选中 → adapter 自采(焦点 chip 优先+隐式兜底+锚点定格+visible_text 收进 adapter);`__asstOpen` 只认已摘的内联 pane → 也认 `#side-pane-asst`;三个 chip 渲染器(选中/图/便签)双目标 `#asst-input`。另加 `_ctxCard` 章 chip(`HOST.locLabel` 支路)+ EPUB 选中 chip 点击走 `HOST.flashSelOnPage`。

**翻默认时的接线点**:收藏夹 NotebookLM 三入口重注入 `#asst-quick`(具名函数化 + 双目标 + CSS 双选择器 + 挂载后再调);onTab('asst') loadHistory 加 `__asstLoaded` 守卫;tab 聚焦补 `#asst-ta`。

**遗留清理批次**(独立做,不阻断):物理删除 epub-html.js 内联助手代码(现完全惰性:pane 被摘、监听随 DOM 死)约 500+ 行(sendChat/runAssistant/流式渲染/mic/quick 绑定);EPUB 图 chip 缩略图(_ctxCard 过滤 f.box,EPUB 是 imgbox+src → HOST.figThumb 需按 src 实现);历史 action-card 回放(EPUB 存 actions 非 undo_cards);greet 措辞「页→章」按 host;模板静态 `#ep-side-asst` DOM 删除。

## 清理批次完成(2026-07-07,task #253)

内联助手已**物理删除**(~630 行:对话主体/流式揭示/反馈弹窗/mic/绑定 + 模板静态 pane + 逃生舱 flag)。EPUB 助手=共享侧栏,无 fallback。**动作卡基建保留**并接给共享侧栏:`_epShowAction/_epActDo/_epTaskAction/_epQueueAction/_epFlushActions/_epAssistEdit` 经 HOST(`showAction/queueAction/taskAction`)+ 容器 `_asstBody()`(=`#asst-thread`);共享侧栏新增 SSE `action` 事件分支、trackTask 的 client_actions+持久卡、历史 `m.actions` 回放、图 chip src 直链支路、greet 量词 `HOST.locNoun`。`addCard/streamInto`(showCard 入口)仍活,已重定向共享容器。四个小缺口(图 chip/action 回放/greet 措辞/物理删除)全部关闭。

## 唯一抽屉(2026-07-07,task #254——用户指正「唯一存在≠只代码相同」后的抽屉本体收敛)

**rc-sidedrawer 现在是两阅读器唯一抽屉**,PDF 默认走它(`&drawer=legacy` 逃生舱,与 ?ui=legacy 同批次物理删除)。两边 tab 完全一致:**助手·单词本·知识点·高亮·目录·语法·历史**。

**架构**(映射=workflow wf_493a012e,4 agent 并行地图):
- rc-sidedrawer 泛化(加性 opts):`appearanceKeys(name)`(PDF 按排版分档 pdf-gp-*-{mode})、`mirrorOpenClass/mirrorFloatingClass`(body 镜像 grammar-open/grammar-floating → pdf-styles.css 挤压/悬浮规则原样生效,消费方零改)、`onReflow`(PDF=_scheduleRefit 悬浮不重排)、`tabButtons`(per-tab 动作按钮,PDF 🗑 清空分析仅 grammar tab)、setTab 记忆 tab 无 pane 回退 defaultTab(两 reader 共用 LS 键 ep-side-tab)。
- PDF 侧接线=`reader.src/28-shared-drawer.js`:摘静态 chrome(#side-handle/#side-tabs/#side-settings),#grammar-panel 改名 #ep-side(12-vocab _visRight/27-adapter mountPanel 双目标),静态 pane 挂 .ep-side-pane,旧入口同名改道(switchSideTab/open·close·toggleGrammarPanel/toggleSidebar/toggleVocab/_gpSet*/_gpApplyAppearance→RC.sidedrawer);双页临时切单列走 onLayoutChange。
- PDF 新 pane:**高亮**(GET/DELETE /pdf/api/highlights + rc-highlight.renderList,跳转 jumpWithBack)、**目录**(GET /api/toc?entries=1←book_toc._effective_toc,印刷页经 _pdfFromDisp,无目录引导设置面板建立)。
- EPUB 新 pane:**历史**(rc-result beforeOpen 快照→localStorage pdf-qhist-<FREL>,渲染/回放镜像 21-misc-ai,样式注入)。
- tab 栏定稿(用户拍板):无 ✕(关=把手/afterJump)、不换行,视口 <1360px 标签隐藏只显图标(`.ep-side-tab-lb` span + title),7 tab+🗑+⚙ 单行永远放得下。

**遗留(独立批次)**:?ui=legacy 整体退役时一并物理删除——PDF 旧抽屉 JS(18-grammar 开合/外观旧体、05-nav switchSideTab 旧体)、模板静态 chrome(#side-handle/#side-tabs/#side-settings,早期兜底把手保留到 rc-sidedrawer 可更早建为止)、25-assistant legacy、drawer=legacy flag。
