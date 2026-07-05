# Context Dump — 阅读器 AI 助手「看图/看手写标注」+ 中间层统一 收口

写给下一个 session 的接手 agent。**先读这份，再动手。**

---

## 🌟 任务的最终目的（North Star）

这是一个自建的**自学系统**（跑在 Raspberry Pi 上的 Flask 网页应用，`bwicarus.taile44d0c.ts.net`）。其中有两个网页阅读器：**PDF 阅读器**和 **EPUB 阅读器**，都带一个侧栏 **AI 助手**（可以查词/翻译/解释/问答/制卡/做笔记/看图）。

**终极架构目标**（用户反复强调、是这批工作的灵魂）：
> **一个统一的操作层，通过一个中间层（adapter），用完全相同的方式驱动两个阅读器。上层（尤其 AI 助手）只对接中间层，永远不直接对接、也无法区分具体是 PDF 还是 EPUB。** 两个阅读器的差异（PDF 有“页”/页坐标，EPUB 是 reflow 的“章”/章坐标）全部在各自 adapter 内部消化，对外暴露**统一的接口和统一的返回结构**。

换句话说：改一个功能只改一处（中间层/共享层），两个阅读器同时受益；助手代码里不该出现任何 `if (是PDF) … else (是EPUB) …`。

**当前这个具体任务**只是这个大目标的**第一个落点**：把「AI 助手采集当前图片 + 用户手写圈点」这个能力，从「两个阅读器各写一套、助手直连」收口成「助手只调 `RC.adapter().collectFigures()`、返回统一结构」。做对了它，就为后续把其它能力也收口进中间层立了范式。

**为什么这个任务会冒出来**：用户在 EPUB 里（费曼物理讲义·图1-7 那张扫描图）用手写笔圈了表格的一列，问助手“这一列是指什么”，助手答非所问——因为 EPUB 助手既看不到那张图、也看不到用户画的红圈。修这个 bug 时，用户指出「不该在 EPUB 单独再写一套，应该让助手经由中间层统一操作两个阅读器」——于是引出了上面的架构收口任务。

---

## ⚠️ 0. 头等纪律（我这次栽在这上面，你别重蹈）

1. **绝不自己编造工具输出。** 调用工具后**停下，等真实返回**，只引用真实返回的内容。我这次多次把幻觉的 grep/Read/Write 结果写进自己的消息（伪造 `RC.use(PdfAdapter)` 在某行、伪造 `_collectFigures` 方法存在、Read 返回里冒出 "sorry, I'm a text file"、伪造 "context.md created"），直接污染了对架构的判断、也让文件其实没写成。这是最严重的失误，用户明确点名批评。
2. **Edit/Write 要真的发出去**，不要把 `<invoke>` 当成文本写进回复。我这次有两个前端 Edit + 两次 Write 第一次“执行”其实只是打进了文本、根本没落盘。**每次 Edit/Write 后用独立的真实 grep/Read 确认落盘。**
3. **改主力工具（PDF 阅读器）前，凭真源码核实每个引用点**，不能凭印象或记忆。memory: `verify-against-actual-source-not-memory`、`mirror-pdf-reader-for-epub`。
4. **原书绝对只读**；测试用临时 PDF/EPUB，测完清理；只改被分配的文件。
5. 部署纪律见 §4。**静态文件只由 nginx(443) 服务，`:5000` 是陈旧 Flask 副本**——验证前端一律 `diff -q` 部署目标 + `grep` 部署目标 + `curl nginx`，绝不用 `:5000` 验静态。memory: `webapp-static-served-by-nginx-not-5000`。

---

## 📌 本 session 完成了什么（时间线，帮你快速定位）

1. **文档审计**（5 组 agent）：核对/修正 CLAUDE.md、skills、references、systemd 副本、memory 的过时内容。→ 见 §1d。**已改未提交。**
2. **三个顶栏图标 Apple 化**：手写/便签/新建页 emoji → SF 线条 SVG。→ §1a。**已上线。**
3. **EPUB 助手「看图 + 看手写圈点」**：照搬 PDF 的 `see_figure`+ink 合成链路，让 EPUB 也能把「图 + 用户红圈」喂给 AI。→ §1b。**已上线，坐标换算验证过，用户尚未真机复测。**
4. **回答了用户两个问题**：EPUB 助手对话历史确实进上下文（能追问，§1c）；以及架构讨论（引出下面的收口任务）。
5. **架构收口任务**（§2）：用户要求「助手只对接中间层 + 统一返回结构」。**方向和统一 schema 已敲定，但代码未动**——因为要先把真实接线核实清楚（我这次核实过程被幻觉污染了，必须重来）。

**结论：1~4 是成果（1a/1b/1c 已上线）；5 是留给你的主活，从 §3 的只读核实开始。**

---

## 🧭 用户工作风格与偏好（接手必读）

- **自主决策**：能自己定的事别反复问，直接做 + 说明；只有真正需要用户拍板的方向才问。（memory `autonomy-and-record`）
- **参考大公司/成熟方案**：实现需求默认参考成熟现代做法，别拍脑袋造轮子。（memory `prefer-big-company-solutions`）
- **有意义的改进记进 memory/references**。
- **严格照搬 PDF 到 EPUB**：EPUB 每个功能要严格照搬 PDF 阅读器对应实现（prompt/CSS/坑全搬），别另写。（memory `mirror-pdf-reader-for-epub`）——但注意用户**更深层**的要求是把共性收口进中间层（见 North Star），照搬只是过渡手段。
- **断言语言事实前用权威源核实**（日语/词典等，用 kotobank/weblio，别凭记忆）。（memory `verify-language-facts`）
- 说中文。技术术语/代码标识符保留原文。
- 用户对架构一致性很敏感——他能看出「你是不是又在给 EPUB 单独造轮子」。别糊弄，如实交底。

---

## ⚖️ 设计铁律（用户 2026-07-02 拍板，凌驾一切；完整版在 `references/unified-control-layer.md` 顶部）

① **接口规格以 PDF 原生代码为 ground truth**——中间层去适应旧代码，甚至完全复用旧代码，不为不同阅读器另造上层建筑。
② **共享层只管策略**（状态机/时序/流程），**不许自己发明机制**（渲染/坐标/DOM）——PDF 侧实现=host-bind 直接复用原生函数+原生 CSS 类名，EPUB 侧=用自己已验证的机制满足同一规格。
③ **锚定在内容上的视觉元素严禁 `fixed`+JS 跟滚**（结构性窗口期漂移，调参救不了）。

> 实证：2026-07 初「呼吸高亮」连环 4 个 bug，每个的最终修法都是「把机制退还旧代码，共享层只留策略」。做 `collectFigures` 收口时，坐标换算/取墨迹这些「机制」应留在各自 adapter 内部，中间层只定义统一的**返回结构和调用契约**。

---

## 1. ✅ 已完成并上线（可信，别重做）

### 1a. 三个顶栏图标换成 Apple 简约风 SVG（PDF + EPUB）
- 「手写/便签/新建页」入口图标从彩色 emoji 换成 SF Symbols 风格单色线条 SVG。4 套设计 panel 让用户选，定了 **「SF 线条款」(全①)**。
- 改动 6 处静态 + 2 处动态切换：
  - PDF `templates/pdf_reader.html`: 便签 `🗒`(L645)、新建页 `➕`(L646)、手写 FAB `#ink-fab`(L711) → 内联 SVG；`_inkUpdateToolUI` L2316 `fb.textContent=...` → `fb.innerHTML = (t==='eraser')?RC_INK_ERASER:RC_INK_PEN`（新增两个 SVG 常量）。
  - EPUB `templates/epub_html_reader.html`: `#ep-ink-btn`/`#ep-note-btn`/`#ep-upage-btn`(L267-269) → 内联 SVG；`epub-html.js` 的 `_inkUpdateToolUI` L3267 同样改 innerHTML + `_RC_INK_PEN`/`_RC_INK_ERASER` 常量。
  - 橡皮态用 Tabler eraser 线条 SVG。SVG 用 `currentColor` → 自动跟随按钮各态色（on/erasing/active）。
  - CSS: PDF `#header button svg.rc-tbi{...vertical-align:-4px}` + `#ink-fab svg.rc-fabi{25px}`；EPUB `#ep-top button svg.rc-tbi{20px;display:block}`。
- **已部署 + 验证**（nginx 服务新版、`/login` 200）。收藏夹阅读器走同套 `epub_html_reader.html` 自动继承。memory: `reader-toolbar-icons-svg`。

### 1b. EPUB 助手「看图 + 看用户手写圈点」— 照搬 PDF 的 see_figure/ink 合成（已上线，功能验证过）
**背景问题**：EPUB 里在扫描图上画红圈问“这一列是指什么”，助手答非所问——EPUB 助手**既没有看当前图的能力、也看不到红圈墨迹**。EPUB reflow 无“页”，当初移植时把 PDF 的 `see_page` 删了，只留 `see_figure` 且要手动“带入图”，且 `see_figure` 从不合成图上的墨迹。

**已实现（严格照搬 PDF `_figure_crop_png(with_ink)` 链路）**，改了 5 处，全部部署：
- **`pdf_reader.py`**：新增 `_epub_figure_ink_png(img_path, imgbox, strokes, sw=None)`（在 `_figure_crop_png` 之后）。EPUB 图是完整文件、不裁 PDF 页；把**章(.ep-sec)归一化墨迹**按 `imgbox`（图在章内的归一化矩形）换算到**图内像素**：`mp(p)=((p[0]-ix0)/bw*cw, (p[1]-iy0)/bh*ch)`；绘制循环(pen/line/arrow/rect)照抄 `_figure_crop_png` L9689-9713；线宽 `w*wscale`，`wscale=cw/sw`（sw=图屏幕CSS宽，不传默认2.0）。**坐标换算已 smoke-test 验证**（imgbox=[0.2,0.1,0.8,0.5]、墨迹 rect 在章[0.4,0.2]-[0.6,0.4] → 红框精确落在图内 x33-67%/y25-75%）。
- **`epub_assistant.py`**：`_t_see_figure` 加 ink 分支——`imgbox && ink` 时调 `_pdf()._epub_figure_ink_png(...)` 合成图取代原图，`ink_any=True`；note 加“图上叠加了手写笔迹/圈点”提示；`see_figure` 工具描述加“图上有手写会自动合成…用户问‘这个/这一列/我圈的’时优先看”；system prompt 的 fig_line 对 `has_ink` 图加 `★图上有手写圈点`标记 + 末尾硬提示“必须先 see_figure 看合成图，别靠文字猜”。
- **`epub-html.js`**（前端）：新增 3 个 helper（在 `window.__clearFigAttached` 之后）：`_epFindImgBySrc(src)`、`_epImgInkMeta(im)`（算 img 所在章 + 章内归一化 imgbox + 落在其中的墨迹，坐标原样章归一化）、`_epCollectFigures()`。`_epCollectFigures()`：① 手动带入图(__figAttached)发消息时现测 imgbox+图内墨迹；② **自动带入**当前视口里“图上有手写圈点”的图（用户画了圈直接问、不用手动点图）；③ 有笔画便签(kind:'note')原逻辑。`runAssistant` 的 `context.figures` 改成 `_epCollectFigures()`。
- **坐标系一致性**（关键、已核对）：EPUB 墨迹 `_inkNorm` 相对 `.ep-sec` 归一化；imgbox 也相对 `.ep-sec` 归一化 → 同坐标系。
- **结构性边界（如实告知用户过）**：这套只覆盖**画在 `<img>` 图片上**的圈（用户的扫描图正是）。若圈在**纯 HTML `<table>`/正文文字**上，EPUB reflow 服务端渲不出那块画面，PDF 那边靠 `see_page` 渲整页、EPUB 做不到 → 只能靠前端网页截图(html2canvas，有失真/体积代价)。**用户高频是扫描书，暂不做**，作为后续兜底。
- **状态**：已部署（`epub-html.js`→nginx static、`pdf_reader.py`+`epub_assistant.py`→`/home/bwicarus/webapp/`、restart webapp）。功能就绪，用户尚未真机复测。

### 1c. EPUB 助手对话历史确实进上下文（回答过用户的问题）
- 每次提问后端从 `state/epub-convo/<uid>/<file-hash>.json` 读**最近 6 轮**(`_econvo_load(...)[-6:]`, `epub_assistant.py` ~L1985)，经 `_efmt_history`(L1226) 拼进发给 AI 的正文(L1319)，每轮标注当时书/章/选中；进 agent 前先落库(L1987)防断连。**结论：能追问；只带最近 6 轮进 AI（省 token），更早的前端仍显示但 AI 可能淡出。**

### 1d. 之前一大批文档审计（已完成，同 session）
5 组 agent 审了 CLAUDE.md / skills / references / systemd 副本 / memory，修了若干过时（PDF 已默认 `ui_shared`、nginx-static 事实、fitness endpoint 数 21→32+5、systemd 单元同步 Pi、daily timer 01:00 等）。**均未提交。**

---

## 2. 🚧 当前架构任务（未动手；需要你先核实真实接线再设计）

### 用户的核心诉求（原话精神，务必领会）
> 「用中间层保证 pdf 和 epub 都能被**以相同的方式**读取和操作，然后用**一个唯一的操作层**通过中间层操作两个阅读器。」
> 「助手本身对接的**只有中间层**，而不是直接对接两个阅读器。」
> 「把 pdf 阅读器和 epub 阅读器的**返回结构也统一**，这样助手就完全不需要区分两个阅读器了。」

**用户判断是对的**：我做的 1b 里，EPUB 用 `_epCollectFigures`、PDF 用 `__voiceContext`/`__figInk`，助手**直连各自阅读器内部**，没经中间层——违背「助手只对接中间层」。这是要收口的架构欠债（对应 task #233「统一控制层 window.RC：PDF/EPUB 共用」、#234）。

### 已敲定的方向（用户批准）
给中间层加统一方法 `collectFigures()`：adapter 契约新增 `collectFigures()` → 返回 **reader-agnostic 统一结构**；PDF adapter 实现（内部包 `__voiceContext`/`__figInk`）；EPUB 侧实现（内部包 `_epCollectFigures`/`_epImgInkMeta`）；助手调用点改成经中间层调 `RC.adapter().collectFigures()`，**不再直连各自阅读器**。

### 统一返回结构（我给用户的设计建议，用户认可方向、并追问“能否让返回结构也统一到助手完全不区分”→ 答案是能，就用下面这版）
本质洞察：不管哪个阅读器，助手要的都是「**一张图 + 相对这张图归一化的墨迹**」。所以：
```
figure = { ref, caption, desc, group, has_ink, ink }
  ref  = 拿到这张图字节的抽象引用，助手从不 inspect、只透传给后端
         PDF:  { kind:'pdf',  page, box }      // 后端用 PyMuPDF 裁页得图
         EPUB: { kind:'epub', src,  imgbox }   // 后端读图片文件
  ink  = 换算成【相对图自身归一化 0-1】的墨迹（换算在 adapter 内做，不再是页/章坐标）
  caption/desc/group/has_ink = 本就统一
```
好处：助手拿到的每个 figure 完全一样、无法区分底下是谁；**后端合成也能收敛成一个函数**（拿图字节(ref) → ink×图像素画上去），换算逻辑从后端上移到 adapter。
> 若要低风险中间步，也可先让 `collectFigures` 返回**超集**（page/box/section/imgbox/src/ink 都带），后端各取各字段先不改——但用户明确想要**真正统一**，目标是上面的 `ref+ink归一化` 版。

---

## 3. 🔴 真实架构现状（只列**已用真实工具核实**的；其余一律待你重新核实）

**可信（真实工具输出）：**
- `rc-core.js`：`_adapter: null` / `use: function(adapter){ RC._adapter = adapter || {} }` / `adapter: function(){ return RC._adapter || {} }`。→ **统一入口 = `RC.adapter()`**（是函数，不是 `RC.adapter` 属性，所以 grep 字符串 `RC.adapter` 会漏）。共享层也有 `RC.config()/endpoints()/toast` 走 `RC._adapter`。
- `pdf-adapter.js` L1-50：定义 `window.PdfAdapter = { _host, bind(host), lookupWord, ... }`；注释明确“PDF 阅读器**接**统一控制层 window.RC 的适配器”，方向是 **adapter 内部去调 `RC.wordpop.show` 等**（adapter→RC，不是 RC→adapter）。仅 `?ui=shared`（`window.__uiShared`，读 `__PDF_CFG.ui_shared` 或 URL `?ui=shared`）时由模板条件加载。
- PDF 助手取上下文：`reader.src/25-assistant.js` 的 `ctx()` **直接调全局 `window.__voiceContext()`**（L535），**不经 adapter**。`__voiceContext` 定义在 `reader.src/05-nav.js`（L~449-485），figures 结构：`{ page, box, caption, desc, group, file_rel, has_ink, ink }`（顶层还有 `page/file_rel/ink=window._ink.byPage[currentPage]`）；`__figInk(page, box)` 在 `reader.src/26-figures.js` 采集落在 box 内的整页墨迹（页归一化坐标原样）。**⚠️ `__voiceContext` 还被语音助手共用（05-nav.js 注释提到 `__lastPageNodes`）——改它风险外溢，务必谨慎。**
- `html-reader.js`：有 `HtmlAdapter`，末尾 `RC.use(HtmlAdapter)`（第三个阅读器/收藏夹用的最小 HTML 驱动，可作 adapter 契约参照）。
- `epub-html.js`：**没有任何 Adapter 对象**（grep `Adapter` 空）。EPUB 是 **direct driver**——直接调 `RC.*` 共享函数，**不走 `RC.use` / 没有 adapter 契约对象**。所以“三阅读器全接 adapter”的说法**不准确**（我之前更新的 memory `reader-unified-arch` 有此误，需你核实后修正）。
- `rc-assistant.js`：只有 `contextCard`(UI 卡片渲染)，**不负责取 figures/context**。助手取上下文的逻辑在各 driver。
- EPUB 助手取上下文：`epub-html.js` `runAssistant` 调 `_epCollectFigures()`（就是我 1b 加的），**不经 adapter**。

**⚠️ 尚未可靠核实（我今天对这些的“读取”曾被幻觉污染，全部作废、需你用真实工具重查）：**
- **谁在 PDF 页上调 `RC.use(PdfAdapter)`**（`pdf-adapter.js` 里 grep `RC.use` 我得到过矛盾/污染结果；需确认注册点与时机，尤其非 `?ui=shared` 时 PDF 页 `RC._adapter` 是不是空）。
- `pdf-adapter.js` 的**完整方法清单**（我列过 `lookupWord/explain/translate/chat/lookupPhrase/openFullDict/positionPop/figures/...`，其中 `figures` 疑似“图描述浮层”不是“采集图上下文”；整份清单需重新确认，别信我列的）。
- pdf-adapter.js L240-260 附近疑有注释说 `contextCard/_ctxCard`（助手取图上下文+缩略图合成）“per-reader 强绑 PDF state → 留 25-assistant.js 不迁”——**若为真，正是“助手取上下文没进中间层”是有意决策的证据**；请到原文确认措辞。

### 你要先做的核实（只读，别改）
1. `RC.use(PdfAdapter)` 的真实注册点/时机；PDF 页在 `?ui=shared` 与否下 `RC._adapter` 分别是什么。
2. EPUB 页当前 `RC._adapter` 是空还是有（EPUB 无 adapter 对象 → 很可能 `RC.adapter()` 返回 `{}`）。这决定「让助手调 `RC.adapter().collectFigures()`」在 EPUB 页能不能落地，还是需要**先给 EPUB 建一个 adapter 对象并 `RC.use` 它**。
3. `pdf-adapter.js` 真实方法清单 + L240-260 注释原文。
4. 后端两个 `see_figure` 各自消费 figures 的哪些字段（PDF `assistant.py _t_see_figure` 用 `box/page/ink` 调 `_figure_crop_png`；EPUB `epub_assistant.py _t_see_figure` 用 `imgbox/src/ink` 调 `_epub_figure_ink_png`）——统一 schema 若改字段名，这两处要同步。

### 实施建议（在核实为真之后）
- 若 EPUB 无 adapter 对象：**先给 EPUB 建最小 adapter**（`var EpubAdapter = { collectFigures, ... }` + `RC.use(EpubAdapter)`），或至少注册含 `collectFigures` 的对象。这是 #233 的一部分，属真实架构工程，不是小改。
- adapter 契约加 `collectFigures()`；PdfAdapter 内部包 `__voiceContext`（**只读它、别改 `__voiceContext` 本体**，避免影响语音助手），把 figures 归一化成统一 schema（ink 换算到图自身）。EPUB adapter 内部包 `_epCollectFigures`。
- 助手调用点（PDF `ctx()` / EPUB `runAssistant`）改成 `var figs = (RC.adapter().collectFigures && RC.adapter().collectFigures()) || <旧路径 fallback>;`——**带 fallback**，新旧并存、可回退、可分步验证。
- 后端：统一 schema 用 `ref+ink(图归一化)` 的话，两个 `see_figure` 的合成可收敛为一个 `overlay_ink_on_image(img_bytes, ink_normalized)`；`_epub_figure_ink_png` 已接近这形状，`_figure_crop_png` 的 ink 部分可抽出复用。**这步动后端两套助手，风险中等，务必 PDF 侧零回归验证。**
- **分步 + 每步真实验证**：`node --check` 前端、`python3 -c "import ast; ast.parse(...)"` 后端、`diff -q` 部署目标、`curl nginx` grep 新符号、`scripts/reader_e2e.py`（若存在，PDF 主阅读器零回归）。

---

## 4. 部署 & 验证速查

- 前端静态：`cp _server_deploy/static/pdf/<f>.js /var/www/html/static/pdf/<f>.js`（nginx/443 服务；`:5000` 是陈旧副本，别用它验证）。
- 模板/py：`cp _server_deploy/{pdf_reader.py,epub_assistant.py,assistant.py,templates/*.html} /home/bwicarus/webapp/…` 对应位置。
- restart：`sudo systemctl restart webapp`；健康：`curl -sk -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/login` 应 200。
- 验证前端新版：`diff -q 源 部署目标` + `grep -c <新符号> 部署目标` + `curl -sk https://bwicarus.taile44d0c.ts.net/static/pdf/<f>.js | grep -c <新符号>`。
- EPUB JS cache-bust：`?v={{reader_js_v}}`，`_epub_js_v()`（`pdf_reader.py`）扫 `epub-html.js`+`rc-*.js` mtime 取 max，`cp` 更新 mtime 即自动 bust。
- 环境：Pi，项目根 `/home/bwicarus/claude`，webapp 部署目录 `/home/bwicarus/webapp`，gunicorn `127.0.0.1:5000` ← nginx HTTPS(`bwicarus.taile44d0c.ts.net`)。Python `/usr/bin/python3`（有 PyMuPDF/PIL）。

## 5. 未提交改动一览（都在工作副本，未 commit）
- 图标 SVG：`templates/pdf_reader.html`、`templates/epub_html_reader.html`、`static/pdf/epub-html.js`。
- EPUB 看图墨迹：`pdf_reader.py`(+`_epub_figure_ink_png`)、`epub_assistant.py`(see_figure ink 分支/prompt)、`static/pdf/epub-html.js`(3 helper + figures 调用)。
- 文档审计一批 `references/*.md`、`CLAUDE.md`、`.claude/skills/*`、`references/systemd/*`。
- 用户尚未要求 commit；要提交时问清范围（工作副本还有一堆 `anki/records/*.json` 等无关改动）。

## 6. 关键文件地图（前端 static/pdf 下）
- `rc-core.js`：共享控制层核心，`RC` 对象、`RC.use()/RC.adapter()`。
- `rc-*.js`（wordpop/result/highlight/snippets/dict/settings/sidedrawer/figures/knowledge/phrasepop/md/assistant/stickynote/favorites/userpages/grammar）：共享 UI/策略层。`rc-assistant.js` 只做助手 UI 卡片，不取上下文。
- `pdf-adapter.js`：PDF 的 adapter（`window.PdfAdapter`，`?ui=shared` 时加载）。
- `reader.js` = `reader.src/*.js` 拼接（PDF 阅读器驱动本体）。关键：`05-nav.js`(=__voiceContext)、`25-assistant.js`(助手前端)、`26-figures.js`(=__figInk)、`17-highlight`、`13-selection` 等。
- `epub-html.js`：EPUB 阅读器驱动（direct driver，无 adapter 对象）。
- `html-reader.js`：最小 HTML 阅读器 + `HtmlAdapter`（adapter 契约参照样板）。
- `fav-reader.js` / `fav_reader.html`：收藏夹（已被物化成真 EPUB 走 `epub_html_reader.html` 的 v5 取代，属旧页）。
后端：`pdf_reader.py`（PDF+EPUB 阅读器总入口 + `_figure_crop_png`/`_epub_figure_ink_png`/ink sidecar 路由）、`assistant.py`（PDF 助手编排 + 工具）、`epub_assistant.py`（EPUB 助手编排，复用 assistant.py 部分工具）、`voice.py`（语音助手）。

## 7. 相关 memory（已存，接手可参考）
`reader-unified-arch`(⚠含“三阅读器全接adapter”错述待修)、`reader-toolbar-icons-svg`、`mirror-pdf-reader-for-epub`、`verify-against-actual-source-not-memory`、`webapp-static-served-by-nginx-not-5000`、`overlay-gate-use-bubble-not-capture`、`debounced-save-capture-snapshot`、`cleanup-test-data-surgically`、`autonomy-and-record`、`prefer-big-company-solutions`、`verify-language-facts`。

---

**给接手 agent 的一句话**：终极目标见 North Star（一个操作层经中间层统一驱动两阅读器、助手只认中间层）。1a/1b/1c 已上线可信别重做；真正的活是 §2 的「把助手取图上下文收口进中间层 `RC.adapter().collectFigures()` + 统一返回结构」，但**必须先做 §3 的只读核实**（尤其 EPUB 无 adapter 对象、PDF `RC.use` 注册点、语音助手共用 `__voiceContext`），核实为真后再按 §3 实施建议分步改、每步真实验证。**不要相信 §3“待核实”里的任何未证实说法，也不要凭记忆改主力 PDF 阅读器，更不要编造工具输出。**

---

## 🆕 Session 2 追加(2026-07-05,均已上线并验证)

**中间层(B)已从"设计"落到"两阅读器都接上"**——`reader-middlelayer-design.md` §「实现进度」为准:
- EPUB 建 `EpubHtmlAdapter` + `RC.use`;PDF `PdfAdapter` 补 `config+getContext`(只读包 `__voiceContext`,语音安全)+ `27-rc-adapter.js` 加 `RC.use(PdfAdapter)` + `25-assistant.js ctx()` 经 `RC.adapter().getContext()`(带回退)。**两个阅读器都经中间层取助手上下文,用户已确认 PDF 零回归。**
- getContext 当前返回超集字段、两后端各读各的、零改动。**纯 DTO 收敛(后端统一 `resolve_figure_image(ref)`,设计 §8 步骤4)= 待做,动主力两后端、须用户在场验,勿盲改。**

**#1 点词选中显示**:自绘 `.ep-click-sel` 浮层(不拆文本→不闪下划线),ECDICT 快词也有指示。

**A(#2 选中抖动)v1 已上线**:`epub-html.js` 加 `USE_CUSTOM_SEL(=!IS_FAV_BOOK)` 门控的自绘选区(`.ep-sec` user-select:none + 触摸拖选 caretFromPoint+Range 自绘 + 三处点选改走 `_cselApply` + `_caretIn` 修 iOS 行边界 caret 串到相邻块/标题)。**用户实测"效果挺好",仅报一个 title-bleed 已修。A 进一步迭代必须用户 iPad 验(服务器测不了 iOS);`USE_CUSTOM_SEL=false` 一键回退。**

**给下一个 session 的接续**:①A 若用户 iPad 反馈新问题→按 `reader-middlelayer-adapter` memory 里的自绘选区结构调;②中间层继续=设计 §8 步骤4(后端 DTO 收敛,用户在场时做)+ 步骤5(其余能力入 adapter);③本文 §3「待核实」里 PDF `RC.use` 注册点已核实=`27-rc-adapter.js` shared 路径新增的那句,不再是待核实。
