# 阅读器统一中间层 —— 设计文档 (草案 v0)

目标:一个**操作层**通过**中间层(适配器)**,用完全相同的方式驱动任意阅读格式(PDF / EPUB / 未来:网页、TXT、漫画…)。上层(UI + AI 助手)只对接中间层,永不、也无法区分底下是哪种格式。

> ⚠ 本文 §2~§6 是**契约设计**(我的设计决定,可现在定稿)。§7「每个阅读器现状怎么接」依赖只读勘察结果,待补实后再进入实现。**不要把 §7 里任何未标「已核实」的说法当事实。**

---

## 1. 分层

```
┌─────────────────────────────────────────────┐
│  上层消费者:UI 工具栏 / 侧栏 AI 助手 / 语音助手  │  ← 只调 RC.* 和 RC.reader().*,格式无关
├─────────────────────────────────────────────┤
│  操作层  window.RC  (rc-*.js,共享)            │  ← 策略/状态机/流程;不发明机制
│    RC.reader()  → 当前阅读器适配器             │
│    RC.wordpop / result / highlight / …(共享UI) │
├─────────────────────────────────────────────┤
│  适配器层 ReaderAdapter (每格式一个)           │  ← 把抽象操作翻译成该格式真实机制
│    PdfAdapter / EpubAdapter / WebAdapter …    │     所有坐标系/页章差异在此消化
├─────────────────────────────────────────────┤
│  阅读器本体 (reader.js / epub-html.js / …)     │  ← 渲染、DOM、原生交互
└─────────────────────────────────────────────┘
                       ⇅  (opaque ref 透传)
┌─────────────────────────────────────────────┐
│  后端 per-format 解析器 (按 ref.kind 分发)      │  ← 前端一个适配器 ↔ 后端一个解析器
└─────────────────────────────────────────────┘
```

**铁律对齐**(用户 2026-07-02 拍板):①接口以 PDF 原生为 ground truth,中间层适应旧代码;②共享层只管策略,机制留适配器;③锚定内容的视觉元素禁 fixed+JS 跟滚。

---

## 2. 核心概念

### 2.1 能力声明 (Capabilities)
每个适配器声明支持哪些能力,操作层按能力走,不支持的自动降级(灰掉,不报错)。

```
adapter.capabilities() → {
  selection, wordLookup, phrase, translate, explain, chat,
  highlight, figures, ink, notes, search, ruby, pageTranslate,
  renderRegion,   // 后端能把任意区域渲成图(PDF 有,EPUB 无,网页无)
  hasImages,      // 内容里有真实 <img> 文件可直接取字节(EPUB/网页有,扫描PDF靠 renderRegion)
  location,       // 支持跳转/定位
}  // 值为 true/false
```

### 2.2 统一数据结构 (reader-agnostic DTO)
上层只见这些,永远长一样:

```
Rect      = { x, y, w, h }                 // 视口像素,用于弹层定位
InkStroke = { t:'pen'|'line'|'arrow'|'rect', c, w, p:[[x,y]…] }
                                           // ⚠ 统一约定:坐标一律【相对目标图自身归一化 0-1】
Selection = { text, sentence, anchorRect:Rect, loc:Location }
Location  = { ref:Ref }                    // 不透明位置锚(见 2.3)
Figure    = {
  ref:Ref,          // 拿图字节的抽象引用(见 2.3),上层只透传
  caption, desc, group,
  hasInk, ink:[InkStroke]   // 已归一化到本图自身
}
Context   = {                              // 助手要的"当前上下文"总包
  loc:Location, selection?:Selection,
  figures:[Figure],                        // 手动带入的 + 自动检测到"有圈画"的
  text?:string,                            // 当前视野/页/章的可读文字(可选)
  meta:{ book, title, langs, … }
}
```

### 2.3 不透明引用 (Ref) —— 格式无关的关键
`ref` 对上层是黑盒,只透传给后端;只有"该适配器 + 它对应的后端解析器"懂它。

```
Ref (PDF)  = { kind:'pdf',  file, page, box }      // box=页归一化
Ref (EPUB) = { kind:'epub', file, src,  imgbox }   // src=图片文件, imgbox=章内归一化
Ref (Web)  = { kind:'web',  url, selector|rectAbs } // 将来:DOM 选择器 / 绝对矩形
```
上层拿到 `figure.ref` 从不拆看;发给后端时后端按 `ref.kind` 找对应解析器取图字节。**新增格式只动"前端适配器 + 后端解析器"这一对,助手/UI 零改动。**

---

## 3. ReaderAdapter 接口契约

每种格式实现下面这套(不支持的能力返回 null / 抛 NotSupported,由 capabilities 提前声明)。方法按组:

### 3.1 生命周期
- `capabilities() → {…}`
- `bind(host)` — 阅读器本体启动末尾把内部量注入(照现有 PdfAdapter.bind 语义)
- `destroy()` — 可选

### 3.2 上下文采集(助手用)—— **本次收口的核心**
- `getContext(opts?) → Context` — 统一入口。内部:取选区 + 收集 figures(含手动带入 + 自动检测圈画)+ 当前文字/位置。**助手只调这一个,拿到统一 Context,不区分格式。**
- `collectFigures(opts?) → [Figure]` — 若助手只要图(getContext 内部也复用它)

### 3.3 选区
- `getSelection() → Selection | null`
- `clearSelection()`

### 3.4 内容能力(适配器把该格式的"查词/翻译/解释/对话"接到共享 UI)
> 这些**大部分已由现有 PdfAdapter/HtmlAdapter 覆盖**(lookupWord/explain/translate/chat/lookupPhrase/openFullDict…),本次不重写,只是纳入统一接口命名 + 让 EPUB 也实现同名。
- `lookupWord(opts)` / `lookupPhrase(opts)`
- `translate(opts)` / `explain(opts)` / `chat(opts)`

### 3.5 标注(高亮/墨迹/便签)
- `highlights` 子接口:`list() / add(h) / update(id,patch) / remove(id)`
- `ink` 子接口:`get(loc) / save(loc, strokes)`(坐标归一化约定见 2.2)
- `notes` 子接口:`list() / read(id) / create(n) / edit(id,patch)`

### 3.6 导航 / 位置
- `currentLocation() → Location`
- `gotoLocation(loc)`
- `readingPosition` 子接口:`get() / save(loc)`(已服务端化)

### 3.7 文字提取
- `getText(loc?|range?) → string` — 当前页/章或指定范围的可读文字(助手 read_page/read_section 的前端侧)

### 3.8 检索 / 叠加
- `search(query) → [{loc, snippet}]`
- `ruby(on)` / `pageTranslate(on)`(能力可选)

> **注**:§3.4~§3.8 很多已经在跑,只是散在各 driver。收口是**渐进**的——先做 §3.2 上下文采集(本次任务 + 最痛点),其余按同一契约逐步归位,不一次性重写主力工具。

---

## 4. 后端对称:per-format 解析器

后端已有 `_figure_crop_png`(PDF)、`_epub_figure_ink_png`(EPUB)。统一为:
```
resolve_figure_image(ref, ink) → png_bytes
  ref.kind=='pdf'  → PyMuPDF 裁 page/box + 叠 ink(ink 已图归一化,直接 ×像素)
  ref.kind=='epub' → 读 src 图片文件      + 叠 ink
  ref.kind=='web'  → (将来) 截图/取 <img>  + 叠 ink
```
关键:**因为 ink 在前端适配器里已归一化到"图自身 0-1"**,后端叠墨迹逻辑对所有格式**完全一样**,只有"取图字节"按 kind 分。合成循环(pen/line/arrow/rect)抽成一个共用函数。

---

## 5. 加一种新格式的清单(以"网页阅读"为例)

未来接网页时,只需:
1. 写 `WebAdapter`(实现 §3 接口 + `capabilities()` 声明 web 能支持啥;不支持 renderRegion 就声明 false)。
2. `RC.use(WebAdapter)`。
3. 定义 `Ref(kind:'web')` 形状 + 后端 `resolve_figure_image` 加 `kind=='web'` 分支(取 DOM 截图或 <img>)。
4. **完毕。** 助手、UI、共享 rc-* 全不动。

这就是"opaque ref + 能力声明"两条设计换来的扩展性。

---

## 6. 迁移原则(动主力工具的安全网)
- **带 fallback,新旧并存**:助手调用点改 `RC.reader().getContext?.() || <旧路径>`,新契约缺失时回退旧代码,可随时回退。
- **分步 + 每步真实验证**:`node --check` / `ast.parse` / `diff -q 部署目标` / `curl nginx grep 新符号` / PDF 主阅读器零回归(reader_e2e 若在)。
- **`__voiceContext` 只读不改本体**(它被语音助手共用,改它外溢)——适配器**包**它,不动它。

---

## 7. 各阅读器现状怎么接(⚠ 待勘察结果补实,勿信未标"已核实"项)

### 已核实(2026-07-05 只读勘察,真实工具确认):
- **统一入口 = `RC.adapter()`**(rc-core.js:10-13,`use(a){RC._adapter=a}` / `adapter(){return RC._adapter}`)。rc-core 只从 `_adapter` 取 **`config` / `getEndpoints()` / `toast`** 三件(15/17/29);`RC.config()` **零消费者**。
- **全仓库只有 3 处 `RC.use()`**:`epub2.js:56`(EpubAdapter,epub.js iframe 版)、`html-reader.js:67`(HtmlAdapter)、`fav-reader.js:57`(FavAdapter)。
- **`window.PdfAdapter`(pdf-adapter.js)从不 `RC.use`** —— 它是平行门控对象,`?ui=shared` 时由 `pdf_reader.html:2174` 加载 + `27-rc-adapter.js:4` `bind()`,分流全靠 `window.__uiShared && window.PdfAdapter`(如 reader.js:3757)。**PDF 页 `RC._adapter` 无论 shared 与否永远是 null。** 真开关是 `__uiShared`(pdf-adapter.js:9,读 `__PDF_CFG.ui_shared`)。PdfAdapter 无 `config`。
- **epub-html.js 无 adapter 对象**(grep `Adapter`/`RC.use` 零命中),direct driver,直连 `RC.*`(assistant/sidedrawer/settings/stickynote/userpages/result/wordpop/grammar/phrasepop/favorites/highlight/knowledge/snippets/figures/typeset/md…)。无 `config`。
- **两个 EPUB 阅读器**:`epub-html.js`(HTML 直渲,`epub_html_reader.html`,收藏夹/完整,**无 adapter**=收口目标)vs `epub2.js`(epub.js iframe,`epub_reader.html`,**有干净 EpubAdapter**=样板参照)。
- **助手上下文两套不兼容实现**:PDF `25-assistant.js:533 ctx()` → `window.__voiceContext()`(05-nav.js:420,字段:page/pages/total/page_offset/langs/selection/selection_sentence/figures/ink/focus_sel/visible_kg_nodes/visible_vocab/books)+ 叠便签;epub-html `runAssistant`(2196)自建 context = `_epCollectFigures()`(1075)+`selInfo`+`_epImgInkMeta`(1046)。
- **⚠ `__voiceContext`/`__lastPageNodes`/`__lastVocab` 是 PDF 侧栏助手与前端语音助手(voice.js:85)的共享数据面**,后端 voice.py:290/assistant.py:2311 依赖其字段形状。**PDF adapter 必须只读地包 `__voiceContext`,绝不改它本体/字段名。** epub-html 不读它 → EPUB 侧改动不外溢语音(但也意味着语音当前只服务 PDF)。
- **能力位(capability)现状**:EpubAdapter/HtmlAdapter/FavAdapter 各有 `config{isPDF,reflow,hasFigures,hasFormula,dictMode,anchorKind,supportsVoice,popupMode,clickWordDetect}`;**PdfAdapter 和 epub-html.js 都没有 config** → 两个收口目标在能力体系里是空白,要补齐。
- **Anchor 是打标签联合**:`pdf-char{page,startIdx,endIdx}` / `pdf-norm{page,x,y}`(便签图)/ `epub-offset{section,offset}` / `cfi`(epub2)/ `html-offset`。统一接口的 Location/Anchor 不能假设单一形状。
- **后端渲染能力**:PDF PyMuPDF 可渲任意页/区域(`_figure_crop_png` 传 page+box 归一);epub-html 只有 HTML+图片文件(figure 传 section+imgbox+src,无任意区域渲染)→ `renderRegion` 必须是可选能力位。

---

## ✅ 实现进度(2026-07-05 更新)

- **[已上线] 步骤1 EPUB 接 adapter**:`epub-html.js` 新增 `EpubHtmlAdapter`(config + getContext + collectFigures + currentLocation)+ `RC.use`;`runAssistant` 经 `RC.adapter().getContext()`(带回退)。
- **[已上线] 步骤2 PDF 接 adapter**:`pdf-adapter.js` 加 config + `getContext()`(**只读**包 `__voiceContext`,不碰本体→语音安全)+ `collectFigures()`;`27-rc-adapter.js` 加 `RC.use(PdfAdapter)`(仅 shared);`25-assistant.js` `ctx()` 经 `RC.adapter().getContext()`(带回退)。**用户已确认 PDF 助手零回归。**
- **[已上线] 步骤3 过渡策略**:getContext 返回超集字段,两个后端各读各的字段、零改动。
- **[已上线·smoke-test 验证零回归] 步骤4 后端收敛**:`pdf_reader.py` 把 `_figure_crop_png`/`_epub_figure_ink_png` 的画笔循环抽成共享 `_draw_ink(im,strokes,mp,scale)`(坐标差异全在 mp);加统一入口 `resolve_figure_image(ref,ink)` 按 `ref.kind`(pdf/epub)分发到既有函数。**两个 see_figure**(assistant.py / epub_assistant.py)都改成经 `resolve_figure_image({kind,path,...geom}, ink)` 走——助手侧只透传 opaque ref+ink,合成入口统一。behavior 等价(smoke-test:pdf 红框 0.3-0.7、epub 0.33-0.67/0.25-0.75 精确,has_ink+空 strokes 走 sidecar 回退)。**加一种格式=在 resolve_figure_image 加一个 kind 分支 + 各格式的 crop/open 函数。** 待验:用户真机确认 EPUB 圈图问 AI + PDF 圈图问 AI 仍正常。
- **[已上线] 前端 figure DTO 统一 ref**:`pdf-adapter.js getContext` / `epub-html.js _epCollectFigures` 给每张图补 opaque `ref`(pdf=`{kind,page,box}` / epub=`{kind,src,imgbox,imgsw}`,note 类不加);两个后端 see_figure 改成**优先读 `fg['ref']`**、旧字段兜底(向后兼容,ref 不对就回退当前正常路径)。至此 figure 定位=统一 opaque ref、助手/后端只透传。旧字段暂留作安全兜底,ref 真机验证稳后可清。墨迹坐标保留自然口径(page/章归一),由 mp 现算到图内像素——功能等价"归一化到图自身"。

**✅ 中间层主线(助手上下文 getContext 收口 + 后端 Figure 合成收敛 + 前端 DTO 统一 ref)全部闭环上线。** 剩 §8 步骤5(其余能力入 adapter)/ 步骤6(第三格式网页阅读器)为后续。
- **[待做] 步骤5/6**:其余能力(高亮/便签/导航/搜索)按同契约逐步归位;接第三格式(网页)验证扩展性。

> 结论:**"助手只对接中间层、用完全一样的方式取两个阅读器的上下文" 已结构上成立并上线**(两个阅读器都经 `RC.adapter().getContext()`)。剩纯 DTO 收敛(步骤4)是可选精修,当前超集已零回归工作。

## 8. 实现顺序(基于 §7 核实事实,分步 + 每步真实验证)

**第一个垂直切片 = 「助手上下文采集」(getContext),因为它是本次痛点、且两侧都没进 adapter,收口它最能证明架构。**

1. **EPUB 侧先建 adapter(最低风险,不碰 PDF/语音)**:在 epub-html.js 造一个 adapter 对象(名字避开 epub2 的 `EpubAdapter`,用 `EpubHtmlAdapter`)含 `config{...}` + `getContext()`(包现有 `_epCollectFigures`+`selInfo`,产出统一 DTO §2)+ `RC.use(EpubHtmlAdapter)`。改 `runAssistant` 经 `RC.adapter().getContext?.() || <旧内联>`。验证 EPUB 看图仍工作。
2. **PDF 侧接 adapter**:给 `PdfAdapter` 补 `config{isPDF:true,...}` + `getContext()`(**只读**包 `window.__voiceContext()` → 归一化成同一 DTO,**不改 __voiceContext 本体**);在 `27-rc-adapter.js`(shared 路径)加 `RC.use(PdfAdapter)`。改 `25-assistant.js ctx()` 经 `RC.adapter().getContext?.() || window.__voiceContext()...`(带 fallback)。**验证 PDF 侧栏助手 + voice.js 都不受影响。**
3. **统一 DTO 的过渡策略**:getContext 返回的 figure **先带超集字段**(既有统一 `ref`,又保留后端现读的 page/box/section/imgbox/src/ink)→ 后端 assistant.py/epub_assistant.py **暂不改**,新旧并存、零回归。
4. **后端收敛(增量2)**:把 `_figure_crop_png`(ink部分)与 `_epub_figure_ink_png` 抽成一个 `overlay_ink_on_image(img_bytes, ink_归一化到图)`;两个 `see_figure` 改用统一 `resolve_figure_image(ref)` 取字节。此时可从 DTO 去掉 legacy 字段,达成**纯统一**。此步动后端两套助手,风险中等,PDF 零回归验证必做。
5. **能力位 + 其余操作面**:给两个目标补 `config` 能力位;其余 §3 能力(选区/查词/翻译/解释/对话已大量走 per-call opts,只需纳入 `capabilities()` 声明 + 让 EPUB 实现同名)按同契约**逐步**归位,每步独立验证。
6. **扩展验证**:待一个能力(getContext)全链路收口 + PDF 零回归后,再照 §5 清单接第三种格式(网页)验证扩展性。

> 关键取舍:不 big-bang。先 EPUB(不外溢)→ 再 PDF(只读包 __voiceContext,带 fallback)→ 后端收敛。每步 `node --check`/`ast.parse`/`diff -q 部署目标`/`curl nginx`/PDF 零回归。
