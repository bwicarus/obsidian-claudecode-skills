# PDF 阅读器完整参考

**地址**：`https://bwicarus.space/pdf/` （需登录；客户端通过 device-link 拿 session）

**目标**：在浏览器里读 vault 里的 PDF + 多种 AI 交互（翻译 / 解释 / 问 AI / 加笔记） + iOS 风格高亮编辑（带备注、色板、左滑删除）+ AI 选段草稿系统 + 知识树关联节点查询。

不依赖客户端 EXE 或 Obsidian。所有数据持久化在服务器：高亮 → `state/pdf-highlights/<sha1>.json`；草稿 → 浏览器 localStorage（per-device）。

---

## 1. 文件清单

后端：
- `_server_deploy/pdf_reader.py` — Flask Blueprint `bp = Blueprint("pdf_reader", __name__, url_prefix="/pdf")`，所有路由
- `_server_deploy/app.py` — 入口注册 `register_pdf_reader(app)`，`/pdf` 加入 `PROTECTED_PREFIXES`
- `data/ecdict.db` — ECDICT 离线英汉字典 ~850MB（单张 `stardict` 表，含 `word/phonetic/translation/definition/exchange` 等列；`exchange` 是列里的屈折数据，不是独立表）

前端（2026-06 起 HTML 与主逻辑 JS 分离）：
- `_server_deploy/templates/pdf_reader.html` — 阅读器主页**模板**（~1116 行：HTML 标记 + 全部 CSS + 两段经典 `<script>`：① `window.dlog`/错误监听 ② 手写墨迹 `_ink`）。主逻辑模块已抽出，模板里只剩：`<script>window.__PDF_CFG={pdf_url,file_rel,page}</script>` + `<script type="module" src="/static/pdf/reader.js?v={{reader_js_v}}">`
- **`_server_deploy/static/pdf/reader.js`** — 阅读器主逻辑模块（~5831 行，运行时是**一个 ES module**；配置走 `window.__PDF_CFG`，架构/全局未变）。**它是构建产物**：由下面的分块源 `cat` 拼接而成
- **`_server_deploy/static/pdf/reader.src/NN-*.js`** — 按功能分块的源（2026-06，21 个文件 60~730 行）。**改前端 = 改这里对应的功能文件**，不是改 reader.js。拼接成单文件运行 → 所有现有交叉调用/全局原样工作（**不是** import/export 强边界，是「分文件、共享同一模块作用域」，故拆分零运行时风险，diff 拼接结果 vs 原 reader.js = 0）。分块清单：
  `01-boot`(PDFJS import/配置/langs) `02-position`(位置记忆) `03-loader`(loadPdf) `04-render`(renderPage) `05-nav`(页导航/缩放/侧栏/vocab-list/tts) `06-layout`(阅读模式/适宽/pinch) `07-continuous`(连续滚动) `08-charlayer`(char-layer绑定/生词下划线) `09-ruby`(振假名) `10-pagetranslate`(整页翻译/行间对照) `11-search`(全文搜索) `12-vocab-sentences`(句子虚框/翻译浮层) `13-selection`(char选中核心) `14-textlayer-legacy`(旧textLayer+工具栏/preview) `15-phrase-wordpop`(F6词组+单词小框) `16-caret-select`(caret/bindTextLayerClick) `17-highlight`(高亮sidecar+markFromResult) `18-grammar`(语法分析+依存图) `19-dict`(字典SSE+hl popover+日语AI) `20-result-draft`(结果modal/草稿/后台job) `21-misc-ai`(md/设置/`_aiStream`/aiCall/onTranslate等)
  - **构建**：`bash scripts/build_pdf_reader_js.sh`（= `cat reader.src/*.js > reader.js`，NN- 前缀保序）。`check_pdf_reader_js.sh` 会**先自动重建再校验**，所以正常流程 `改 src → bash scripts/check_pdf_reader_js.sh（顺带重建）→ cp reader.js → 部署`
  - 进一步要做 import/export 强边界 = 逐个模块迁移（拆全局状态，风险高、无运行时测试，须小步 + 真机验证），目前**未做**
- `_server_deploy/templates/pdf_index.html` — PDF 列表页（GET `/` render）

静态资源（部署在服务器侧）：
- `/static/pdfjs/pdf.mjs` + `pdf.worker.mjs` — PDF.js v4（运行时从 `pdfjsLib.version` 读真实版本；前端 `PDFJS_V` 只是 cache-buster query，如 `20260526a`，并非版本号）。**不在 git 仓库**
- `/static/pdfjs/cmaps/` + `standard_fonts/` — CJK + 字体回落。**不在 git 仓库**
- `/static/pdf/reader.js` — 阅读器主逻辑（**在 git**：`_server_deploy/static/pdf/reader.js`）。cache-bust 自动：`pdf_view` 注入 `reader_js_v=_reader_js_v()`（=已部署文件 mtime，每次部署 URL 自动变，免手动 bump）

服务端部署位置（VPS `/root/...` / Pi `/home/bwicarus/...`）：
```
_server_deploy/pdf_reader.py            → <webapp>/pdf_reader.py
_server_deploy/templates/pdf_reader.html → <webapp>/templates/pdf_reader.html
_server_deploy/static/pdf/reader.js     → /var/www/html/static/pdf/reader.js   ★ 新增,改前端必带,漏了阅读器白屏
data/ecdict.db                           → <webapp>/data/ecdict.db
```
改完 `cp` 三件套（py + html + reader.js）+ `systemctl restart webapp`。**⚠ 改 JS 逻辑改的是 `reader.src/NN-*.js`,不是 html、也不是直接改 reader.js**；流程：`改 reader.src/ → bash scripts/check_pdf_reader_js.sh（自动重建 reader.js + 校验）→ cp reader.js → /var/www/html/static/pdf/`（cache-bust 自动）。restart webapp 只为 py/html 改动；纯 JS 改动只需 cp reader.js（前端 ?v=mtime 自动失效缓存）。

---

## 2. 后端路由清单

所有路由前缀 `/pdf`。实际 `pdf_reader.py` 有 35+ 个 `@bp.route`，下表是**核心阅读 / 高亮 / 草稿**那一组；vocab 字典融合、英语语法分析、手写墨迹、句子翻译、后台 job 等子系统的路由见后面「2.x 其余子系统路由」一节。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | PDF 列表页（render `pdf_index.html`；vault 下所有 *.pdf 按 mtime 倒序） |
| GET | `/view?file=<rel>&page=N` | 阅读器主页（鉴权后渲染 `pdf_reader.html`） |
| GET | `/file/<vault_rel_path>` | 返回 PDF 二进制 `application/pdf`(**`conditional=True` 支持 HTTP Range/206** + `Accept-Ranges`;前端 getDocument 开 `disableAutoFetch:true`+`rangeChunkSize 256KB` → PDF.js 只取翻到的页,几百 MB 大文件 iPad Safari 不 OOM;超大书 `qpdf --linearize` 无损线性化加快首开。2026-05-31 根治"稍大文件打不开")|
| GET | `/api/list-pdfs` | JSON 列表 |
| GET | `/api/page-chars?file=<rel>&page=N` | PyMuPDF 提取该页所有字符 bbox + 内容（驱动 char-layer，含 `vocab_marks`） |
| GET | `/api/page-nodes?file=<rel>&page=N` | 该页对应的 KG 节点 |
| GET | `/api/dict?word=X[&file=&page=&context=]` | 三源融合字典（ECDICT 离线 + Free Dictionary + MW Learner's）；`Accept: text/event-stream` 时 SSE 分段（ECDICT 先到 → free → mw → translate → done），否则一次性 JSON |
| GET | `/api/dict-quick?word=X` | 单词小框：只查 ECDICT 核心（音标 + 中英释义 + lemma/forms），本地秒回 + 后台触发 vocab note 生成 |
| POST | `/api/translate` body:`{text, target_lang}` | AI 翻译 |
| POST | `/api/explain` body:`{text, context?}` | AI 解释（SSE 流式 或 JSON） |
| POST | `/api/to-note` body:`{text, name, file?, page?}` | 把选中内容 → vault 笔记 |
| POST | `/api/upload` | 上传 PDF 到 vault。**multipart form**：`file`（文件）+ `target_dir`（目录，默认 `资源/uploads`）。返回 `{ok, rel, view_url}` |
| POST | `/api/snippets-to` body:`{snippets, make_note(bool), make_anki(bool), note_name?, model?, effort?}` | 草稿 → 笔记 / Anki（同步版，兼容） |
| POST | `/api/snippets-to-async` | 同上 body，后台线程跑，立即返回 `{job_id}`；防 iPad 切后台掐断长请求 |
| GET | `/api/job-status?id=<job_id>` | 轮询后台 job：`{status: running\|done\|error\|unknown, result?, error?}` |
| GET | `/api/highlights?file=<rel>` | 列出该 PDF 的所有高亮 |
| POST | `/api/highlights` body:`{file,page,rects,color,text,kind?,sentence?,body?,note?,page_w?,page_h?}` | 新增高亮 |
| PATCH | `/api/highlights` body:`{file,id,color?,note?,sentence?,body?}` | 修改 |
| DELETE | `/api/highlights` body 或 query:`{file,id}` | 删除 |

### 2.x 其余子系统路由（vocab / grammar / 墨迹 / 句子）

这几组属于后来扩出的子系统，路由都在同一个 `pdf_reader.py`：

| 方法 | 路径 | 子系统 / 用途 |
|---|---|---|
| GET | `/api/page-vocab-marks` | vocab：该页生词下划线标记（也内嵌在 page-chars 返回里） |
| POST | `/api/vocab-mark` | vocab：标记 / 取消标记一个词 |
| POST | `/api/vocab-anki` | vocab：单词一键制卡 |
| GET | `/api/vocab-list` | vocab：生词列表 |
| GET | `/api/vocab-audio` | vocab：取真人音频 |
| GET/POST | `/api/ink` | 手写墨迹层：按页存归一化笔画（sidecar `state/pdf-ink/<sha1>.json`） |
| POST | `/api/sentence-dismiss` | 句子翻译：忽略某句虚线框 |
| POST | `/api/translate-sentence` | 句子翻译：整句翻译 |
| GET/POST | `/api/translate-config` | 句子翻译：后端配置读写 |
| GET | `/api/grammar-nodes` / `-books` | 英语语法分析：列语法 KG / 节点 |
| GET/POST | `/api/grammar-tracked` | 语法分析：选哪些书启用 + tracked 节点 |
| POST | `/api/grammar-analyze` / `-stream` | 语法分析：spaCy（零 AI）/ AI 兜底，对句子按 tracked 语法点分析 |
| GET/POST | `/api/grammar-history` / `-history-save` / `-forget` | 语法分析：分析历史读写/清除 |

vocab 字典融合系统详见 [`vocab-system.md`](vocab-system.md)；英语语法分析系统是后加的子系统（spaCy 词性依存 + tracked 语法节点对齐）。

### SSE 流式 (`/api/explain`)

`Accept: text/event-stream` → 后端 yield chunks，前端 `EventSource` / `fetch` reader 边接边渲染。普通 JSON 模式保留。

### 高亮存储格式

`state/pdf-highlights/<sha1(rel-path)>.json`：

```json
{
  "pdf_rel": "资源/books/000-LADR/000-LADR4eChinese.pdf",
  "highlights": [
    {
      "id": "h_<6-byte hex>",
      "page": 18,
      "rects": [[x0,y0,x1,y1], ...],   // PDF user pt 坐标（与 page-chars 同坐标系）
      "color": "#fff59d",               // 空字符串 "" 表示"仅备注、无颜色"
      "text": "选中原文",
      "kind": "note" | "translate" | "explain",
      "sentence": "句子上下文",          // explain 时填，其他空
      "body": "翻译 / 解释结果 markdown 文本",
      "note": "用户手动备注",
      "page_w": 612.0, "page_h": 792.0, // 用于跨设备 / 重新渲染时缩放
      "time": 1748373291
    }
  ]
}
```

PATCH 允许 `color: ""` 表示"取消颜色但保留 highlight + 备注"。**坑**：旧实现 `(v or "").strip() or found["color"]` 会把空字符串回退到旧色，要改成 `v.strip() if isinstance(v, str) else ""`。

---

## 3. 前端架构

### 3.1 整体 DOM 层

```
#app
├── #header   (顶部 toolbar：文件 / 模式 / 翻页 / 缩放 / 知识点 / ⚙️)
└── #main
    ├── #page-container
    │   └── .page-wrap[data-page-num=N data-loaded=0/1]   每页一个
    │       ├── <canvas>           PDF.js v4 渲染（dpr 倍数 backing store）
    │       ├── .textLayer         PDF.js textLayer（仅用于字体度量）
    │       ├── .sel-overlay       手绘选中高亮（蓝色半透明矩形）
    │       ├── .char-layer        透明覆盖整页，接管所有 mousedown/touchstart 选中（z-index 4）
    │       └── .hl-layer          已存高亮的渲染层 (z-index 5，pointer-events:none，子元素 hl-saved auto)
    │           └── .hl-saved      每条高亮的每个 rect 一个 div
    └── #sel-toolbar               选中浮出工具栏（preview + 按钮 + 色板）

#sidebar (右侧抽屉，默认收起，点 📋 知识点 滑出)
#settings-mask                       ⚙️ AI 设置 + debug + 高亮颜色管理
#result-mask + #result-modal         翻译/解释结果 modal（含 + 选段 + 整条回答 + 标记到 PDF）
#draft-badge                         右下角草稿计数器 badge
#draft-mask + #draft-modal           草稿列表 modal（已选段落界面）
#hl-popover                          点击已存高亮弹出的备注小框
#debug-log                           左下角调试日志（设置里开关）
#hl-toast                            短消息浮窗
```

**z-index 速查**（同一 page-wrap stacking）：

| 层 | z-index | pointer-events |
|---|---|---|
| canvas | 1 | auto |
| textLayer | 2 | none（已禁，仅字体度量用） |
| sel-overlay | 3 | none |
| char-layer | 4 | auto（接管选中） |
| hl-layer | 5 | none（子 hl-saved auto，吃点击弹 popover） |

### 3.2 关键全局变量

```js
let pdfDoc       // PDF.js document
let currentPage  // 当前页
let scale = 1.4  // 缩放因子（auto-fit 后覆盖）
let readMode = 'single' | 'continuous'
let _charSel = null    // { pw, startIdx, endIdx, dragging }  选中状态
let lastSelText = ''   // 最后选中的文本（pendant ↔ toolbar preview）
let _drafts = []       // 草稿（AI 回答里 + 选中的段落）
let _activeHlColor = ''  // 当前激活的高亮色（互斥外框，localStorage 持久化）
let _allHighlights = []  // PDF 的所有已存高亮
let _hlByPage = {}     // 按页索引
let _resultContext = null  // 给 result-modal 「🖌 标记到 PDF」用：原始 charSel + sentence + kind
let _popoverHL = null  // 当前打开 popover 的高亮（点别处关闭）
```

### 3.3 char-layer 选中机制（核心）

**为什么不用 PDF.js textLayer**：textLayer 字符位置在某些 PDF（带连字、subscript、复杂 transform）严重偏移；window.getSelection() 跨 span 拿不准。

**用 PyMuPDF rawdict 替代**：后端 `page.get_text("rawdict")` 给出每个字符的 bbox（image space，y 向下、原点左上，**不需要 y 翻转**）。

前端把 chars 数组（按 baseline + max(height)*0.8 阈值 sort）放在 `pw.__charBoxes`，记 raw PDF coords 在 `_x0/_y0/_x1/_y1`：

```js
[{c:'A', left:..., top:..., width:..., height:..., sp:0|1, _x0,_y0,_x1,_y1}, ...]
```

事件流：
- `mousedown`/`touchstart` 在 char-layer → `_findCharStrict(x,y)` 命中字符 → `_dragStartCharIdx = idx`，**未命中（空白处）**→ 不拦截，返回 false，让 main 滚动正常
- `mousemove`/`touchmove` → `_findCharAt` 同行优先 → `_selByCharRange(pw, start, end)` 渲染 sel-overlay 矩形 + 工具栏跟随
- `mouseup`/`touchend`：
  - 移动了 → 完成拖选
  - 没移动 → 单击触发 _wordExpandFromChar / 双击 _lineExpandFromChar / 三击 _paragraphExpandFromChar（按时间阈值 380ms）

**拖选自动对齐词边界**：`_expandToWordStart/_expandToWordEnd` 用 `/[A-Za-z0-9_]/` 词字符 + 同行（`<= 0.5×height`）。CJK 字符不动。

**空格处理**：char-layer 跳过 sp（避免遮挡命中）；`_charsRangeToText` 拼文本时按 X gap > 0.3×height 智能补空格（应对 PDF 数轴 "0 1 2 3 4" 在 rawdict 里没空格 char）。

### 3.4 渲染 hl-layer

```js
function renderHighlightsOnPage(pw, pageNum) {
  // 确保 hl-layer DOM 顺序在 char-layer 之后（appendChild，不用 insertBefore）
  // 配合 z-index:5 双保险让 hl-saved 收点击
  ...
  for (h of _hlByPage[pageNum]) {
    for (rect of h.rects) {
      const div = document.createElement('div');
      const hasColor = h.color && h.color.trim();
      const hasNote = h.note||h.body||h.sentence;
      div.className = 'hl-saved' + (hasNote?' has-note':'') + (hasColor?'':' no-color');
      div.style.background = hasColor ? h.color : '';   // no-color 用虚线边框
      // 事件用 capture:true 拦截，避免被 char-layer / document 抢
      ['mousedown','mouseup','touchstart','touchend'].forEach(evt =>
        div.addEventListener(evt, e => e.stopPropagation(), true));
      div.addEventListener('click', e => { e.stopPropagation(); openHlPopover(h, div, pw); }, true);
    }
  }
}
```

坐标变换：每页可能不同 page_w/page_h，所以 highlight 自带 `page_w/page_h`（pt 单位）：
```js
sx = canvas.clientWidth / (h.page_w || pw.__pageWPt)
sy = canvas.clientHeight / (h.page_h || pw.__pageHPt)
top  = y0 * sy   (无 y 翻转，PyMuPDF 已是 image space)
left = x0 * sx
```

---

## 4. 高亮编辑

### 4.1 工具栏（色板 + 按钮分流）

`#sel-toolbar` 现为左右两块（task #188「工具栏分流 + 颜色竖排」）：
- 左侧 `#hl-color-picker`：色板**竖排**一列（`flex-direction:column`），首元素仍是 `<span class="lbl">🖌</span>`
- 右侧 `#sel-main`：preview + 按钮区，按选中类型分流显示：
  - 单词选中 → `#sel-btns-word`：`📋 复制` / `🔍 查词`（`onLookupWord` 弹字典小框）
  - 多词 / 句子选中 → `#sel-btns-multi`：`📋 复制` / `🌐 翻译`（`onTranslate`）/ `💡 解释`（`onExplain`）/ `💬 对话`（`onChat`）
  - `#grammar-btn-row`：`📊 语法分析`（`onGrammarAnalyze`，按跟踪的语法点分析选中片段）

色板行为：
- 点色 → 立即 `saveHighlight({color, kind:'note'})` → POST `/api/highlights` → 渲染 hl-saved + 该色 `.active` 外框（互斥）+ 关 toolbar + `_lastHlColor` 更新
- 再点同色 → 取消激活外框（**不删除高亮**，仅清 picker 状态）
- 没选中文字时点色 → 仅切激活色 + toast

色板从 `localStorage['pdf-hl-colors']` 加载，默认 4 色 `#fff59d / #a7f3d0 / #a3d4ff / #fda4af`。

### 4.2 结果 modal 的「🖌 标记到 PDF」

`onTranslate` / `onExplain` 入口存 `_resultContext = { charSel, text, sentence, kind }`（snapshot _charSel 防止 result-modal 期间 _charSel 被清）。

点 `🖌 标记到 PDF` → `markFromResult()`：
- color = `_activeHlColor || _lastHlColor || colors[0]`
- body = `#result-content` textContent（去 head-pick / reply-pick 按钮）
- sentence = explain 的 context
- kind 透传

→ `saveHighlight({pw, sIdx, eIdx, color, kind, sentence, body})`

### 4.3 popover（点击已存高亮弹出）

布局：
```
┌──────────────────────────────────────────┐
│ [hl-snip-wrap]                           │
│   [hl-snip] [📌 选中 xxx]            [○] │ ← 单击文字内容展开 / 触屏左滑
│             [📖 所在句 ...]              │
│             [💡 解释 ...]                │
│   [hl-snip-del-row (visibility:hidden)]  │ ← 左滑后 visibility:visible
├──────────────────────────────────────────┤
│ 🎨 颜色  ● ○ ○ ○                          │ ← 点别色立即 PATCH 切换
│                                          │   点 cur 色：有备注→保留 color=""
│                                          │              无备注→直接删
│ [textarea 自定义备注]                     │
│                                  [保存]   │ ← 只保存 textarea（颜色立即生效）
└──────────────────────────────────────────┘
```

预览块行级显示：
- `text` / `sentence` / `body` 三行，每行 `white-space:nowrap;overflow:hidden;text-overflow:ellipsis`
- 单击 content → toggle `.expanded` → `white-space:pre-wrap`
- 触屏整体左滑 / 鼠标拖圆圈 → `wrap.swiped` → `.hl-snip translateX(-64px)` + `.del-row visibility:visible`

### 4.4 "仅备注、无颜色"高亮模式

color 空字符串 `""` 表示用户主动取消了颜色但留下备注。

渲染：
```css
.hl-layer .hl-saved.no-color{
  background:transparent !important;
  mix-blend-mode:normal;
  opacity:1;
  border:1px dashed rgba(150,170,200,.55)
}
.hl-layer .hl-saved.no-color:hover{
  border-color:#60a5fa;
  background:rgba(96,165,250,.08) !important
}
```

显示为虚线边框（仍可点击 → popover）。

### 4.5 设置面板的颜色管理

```
🖌 高亮颜色（点击 ✕ 删除）
[●●●●]          ← .set-hl-row：每色配 ✕ 删除按钮
[color picker] [＋ 添加] [恢复默认]
```

`localStorage['pdf-hl-colors']` JSON 数组持久化；删空时 fallback DEFAULT_HL_COLORS。

---

## 5. 草稿系统（已选段落）

### 5.1 在 AI 回答里收集

result-modal 渲染完 AI 回复后 `addResultPickers()`：
- 给每个 `h1-h6`（真标题）+ `p.fake-head/li.fake-head`（粗体段落）加右侧 `pick-btn` `+` 按钮
- result-modal 右上角小 + 圆形（class `pick-btn reply-pick-all-result`）= 选用整条回答（fallback：无标题时唯一入口）
- 点 + → 把对应文本 push 到 `_drafts`，badge 计数

`_drafts` 存 localStorage `pdf-drafts`：
```js
{id, text, source, src, time, selected: true}
```

### 5.2 草稿 modal（"已选段落"界面）

布局：
```
┌──────────────────────────────────────────┐
│ 📋 已选段落 （共 N 段, 已选 M）   [全部清空] │
├──────────────────────────────────────────┤
│ [draft-item-wrap]                        │
│   [draft-item]                           │
│     [body src / text]                [○] │ ← body 单击=展开；圆圈单击=切换 selected
│   [del-row (visibility:hidden, 64px)]    │ ← 左滑 / 拖 body 揭示
│ ...                                      │
├──────────────────────────────────────────┤
│ [📝 创建笔记]  [🎴 创建 Anki]  [📚 两者]  │ ← 用 /api/snippets-to
└──────────────────────────────────────────┘
```

`_bindDraftItem(wrap)` 绑：
- 圆圈 click → `toggleDraftSel(id)`（先 `wrap.swiped` 则复位）
- body 触屏左滑 / 鼠标按下拖动（>4px 标 dragged） → `wrap.swiped`
- body click：`dragged` 则吞掉；`swiped` 则复位；否则 `expandDraft(id)`

### 5.3 后端 `/api/snippets-to`

body 用两个独立布尔 `make_note` / `make_anki`（不是单个 `kind` 字符串），可选 `note_name` / `model` / `effort`。`_validate_snippets_body` 校验：snippets 非空、至少选一个动作、make_note 时 note_name 不能为空。
- `make_note`：AI 把 snippets 重排为连贯笔记 → 写 `vault/<note_name>.md`（含 PDF 来源、原始截图引用），返回 obsidian:// URL
- `make_anki`：AnkiConnect 加卡（deck "QA"，tag "pdf-snippets"）
- 两者都 true → 两个都做

**同步 vs 异步**：`/api/snippets-to` 是同步版（兼容）；`/api/snippets-to-async` 后台线程跑，立即返回 `{job_id}`，前端轮询 `/api/job-status?id=<job_id>`，防 iPad 切后台 / 锁屏掐断长 AI 请求。

---

## 6. iOS Mail 风格 swipe-to-delete

**两处用同一模式**：popover 预览块 + 草稿项。

```html
<div class="...-wrap">  <!-- position:relative; overflow:hidden -->
  <div class="...">      <!-- z-index:2; background; transition:transform; width:100%; box-sizing:border-box -->
    [内容] [右侧○圆圈]
  </div>
  <button class="...-del-row">🗑</button>  <!-- position:absolute right:0 width:64px z-index:1 visibility:hidden -->
</div>

/* swiped 时 */
.wrap.swiped .item { transform: translateX(-64px) }
.wrap.swiped .del-row { visibility: visible }
```

**关键三点**：
1. **inner 必须 `width:100% + box-sizing:border-box`**，否则不撑满，del-row 从右侧露出
2. **del-row 必须 `visibility:hidden`**（不仅靠 z-index）→ 未 swiped 时彻底不渲染，避免任何穿漏
3. inner background 不透明（`background: #1a2540` / `#10162a`）

swipe 检测：
- 触屏：`touchstart` 记 sx/sy → `touchmove` 算 dx/dy + axis-lock（先动方向决定，防意外触发） → `touchend`，`dx < -40` 进入 reveal；`dx > 30 || !swiped` 复位
- 鼠标：拖动 inner 或圆圈（>4px 标 dragged）；mouseup 触发 reveal/reset，dragged 时下个 click 被吞掉

---

## 7. AI 调用统一通道

`aiCall(path, body, label)`：
- `openResult(label, lastSelText, '⏳ AI 处理中…')`
- fetch + `Accept: text/event-stream`
- ReadableStream 读 chunks → `EventSource`-style parse → 120ms 节流 `marked.parse + MathJax.typesetPromise`
- 失败 SSE 时回落 JSON 一次性渲染

`onTranslate`：
- 短英文单词 + ECDICT 命中 → 离线字典渲染（不耗 AI）
- 否则 `aiCall('/pdf/api/translate')`

`onExplain`：
- 自动 context：选区 < 50 char → 句子（`_expandSentenceFromRange`）；< 300 → 段落
- 存 `_resultContext`
- `aiCall('/pdf/api/explain')`

`onToQA`：
- prompt 输入问题 → 复用 aiCall（`/api/explain` + 用户选中作 context）
- 已废弃 :9091 跳转方案（mixed content：HTTPS → HTTP 阻止）

`onToNote`：
- prompt 笔记名 → POST `/api/to-note` → 返回 `obsidian://` URL

### AI 设置 modal

`localStorage['pdf-ai-overrides']` JSON `{model, effort}`：
- model：'' / haiku / sonnet / opus / gpt-5 / gpt-5.5
- effort：'' / low / medium / high / max
- 空 = 用 server-config 默认

后端 `_ai_backend(override_model, override_effort)` 同时读 server-config + override 拼出 settings 给 ai_backends adapter。

debug 日志（左下 `#debug-log`）：`localStorage['pdf-debug']='1'` 切换。

---

## 8. 阅读模式

`localStorage['pdf-read-mode'] = 'single' | 'continuous'`，header 按钮切换。

- **单页**：`_renderPageInto(num, page-container)` 销毁旧 wrap 渲染新页
- **连续**：`setupContinuousMode()` 给每页生成 placeholder `.page-wrap[data-loaded=0]`（高度估算自 page1 viewport），`IntersectionObserver` 监视，进入 ±500px 才真渲染

zoom：`zoomChange(delta)` → 改 `scale`（0.6~3.0）→ 重渲染当前页 / 全部 placeholder（连续模式）

自适应：`loadPdf` 末尾 `scale = Math.max(0.5, Math.min(2.5, mainW / v0.width))`，让 PDF 宽 ≈ #main 可用宽。

---

## 9. 知识树关联（右侧抽屉）

点 header 「📋 知识点」→ `#sidebar.open`。

`/api/page-nodes` 查 KG（`_find_kg_nodes_for_page`）：
- 遍历 `knowledge_graph/*.json`（跳过 `*.bak.json`），按 KG 顶层 `pdf` 字段定位到本 PDF（绝对路径自动归一化为 vault 相对路径再比对）
- 在命中的 KG 里取 `level == 2` 且 `pages` 数组含当前页号的节点
- 返回字段：`{id, name, numeric_label, state, mastery_level, summary(截前120字), book, kg_file, kind, tracked}`（按 numeric_label 排序）
- 点节点 → 新窗口跳 `/skilltree/<book>/#f.<id>`

（旧文档说的 `nodes/<book>/*.yml` 目录 / `pdf_pages` 字段都不存在。）详见 [`skill-tree-system.md`](skill-tree-system.md)。

---

## 10. 踩坑总结（按时间顺序）

### 10.1 PDF.js textLayer 字符位置不准
- 试过 outputScale / itemBoxes / textDivs 多种方案
- 最终用 PyMuPDF `rawdict` char-level bbox + 自建 char-layer 接管，绕开 textLayer 完全

### 10.2 PyMuPDF y 坐标 ❌ 翻转 bug
误以为 PDF 用户空间（y 向上），用 `(pageH - y1) * scale`。其实 rawdict 给的就是 **image coordinates**（y 向下、原点左上）。**不需要任何翻转**，直接 `y0 * scale` 当 top。

### 10.3 `null bytes` SyntaxError
Edit tool 在 `c == " "` 处插入了 null byte。修复：用 `c.isspace()`。

### 10.4 `'thenn'` bug（拼接选中文本时少空格）
后端最初过滤所有 `c.isspace()`。修复：保留空格 char 但加 `sp:1` 标记，`_findCharStrict` 跳过 sp，渲染时若空格 bbox=0 估算 width。

### 10.5 拖选两端没包含完整单词
加 `_expandToWordStart/_expandToWordEnd` 在 `_selByCharRange` 入口对齐词边界。

### 10.6 sort chars 把 subscript 误判到别行
最初 `(top + 0.5×min(height))` 做 baseline，subscript 小字符 baseline 跟主行差很多，归到下一行。改用 `baseline = top + height` + `ref = max(a.height, b.height)` + 阈值 `0.8` 放宽。

### 10.7 松手后选中被清除
旧 `document.mousedown` listener 检测 `window.getSelection().toString()`，char-layer 不用 native selection 所以空，触发清 toolbar。
修复：`_shouldCloseToolbar()` 检查 `lastSelText`；char-layer 事件 stopPropagation 阻止 document 监听。

### 10.8 iOS Safari `100vh` 把 header 挤出屏外
浏览器 toolbar 占去屏幕但 100vh 不算。修复：`height: 100vh; height: 100dvh;` 双声明 fallback。

### 10.9 import pdf.mjs 报 `application/octet-stream`
nginx 不知道 .mjs MIME。修复：`/etc/nginx/mime.types` 加 `application/javascript    js mjs;` + 浏览器缓存用 `?v=20260526a` cache buster + `Cache-Control: no-store` 头。

### 10.10 mixed content：HTTPS 主页跳 HTTP :9091
QA browser daemon 直跳被 Chrome 阻止。改 in-page `/pdf/api/explain` 复用 modal。

### 10.11 高亮 popover 不弹（z-index）
`.hl-layer{z-index:3}` 低于 `.char-layer{z-index:4}`，hl-saved 收不到点击。
修复：
1. `.hl-layer{z-index:5}` + `pointer-events:none`（子 hl-saved auto）
2. `renderHighlightsOnPage` 用 `appendChild`（DOM 顺序晚于 char-layer，双保险）
3. hl-saved 全部事件用 `capture: true` 优先吃，stopPropagation 防 char-layer 拖选 / document 监听

### 10.12 高亮 popover 弹了但内容空（TypeError）
之前 popover 底部有 [🗑 删除] 按钮 `data-act=del`，改为左滑揭示后忘删 `pop.querySelector('[data-act=del]').onclick = ...` → `null is not an object` → 渲染中断，popover 空白。
**经验**：删 UI 元素后必须 grep 所有 `data-act=` selector。

### 10.13 swipe 删除按钮"漏出"显示 bug
visibility 缺失：仅靠 z-index:1 vs 2 + 不透明 background 仍漏出（user 截图证实）。
修复：del-row 加 `visibility: hidden` + `.swiped` 改 `visibility: visible`。inner 元素加 `width:100% + box-sizing:border-box` 强制覆盖。

### 10.14 PATCH 空 color 被忽略
后端 `(v or "").strip() or found["color"]`，空字符串 → fallback 到旧色。
修复：`v.strip() if isinstance(v, str) else ""`（区分 None 和空串）。

### 10.15 选色就立即标记 vs 选色后点按钮（用户改变心意）
经历：
- v1：色板点立刻保存（直接）
- v2：用户说"只能一种颜色 + 选中外框 + 再点清除" → 改加 [🖌 标记] 按钮 + 互斥激活态
- v3：用户说"按钮和🖌 lbl 可以省略，按下颜色直接标记" → 回到 v1 但保留外框激活态

最终：点色 = 立即标记 + 激活外框（互斥）；再点同色 = 仅取消激活态（不影响已存高亮）。

### 10.16 popover 色板必须立即切色不靠保存
v1：点色仅设 `h._pendingColor`，[保存] 按钮才 PATCH → 用户报"无法切换颜色"。
修复：swatch click 直接 `_hlUpdate({color: newColor})`。[保存] 只保存 textarea note 文字。

### 10.17 "取消颜色但保留备注"
点 cur 色：
- 没备注（note/body/sentence 都空）→ 删整条
- 有备注 → `_hlUpdate({color: ''})`，hl-saved 切换为 `.no-color` 虚线边框

---

## 11. iPad 远程访问

PDF reader 跟其他 webapp 路由一样走 nginx → flask :5000，需要 session 登录。

- 公网 VPS：`https://bwicarus.space/pdf/`（HTTPS 公网）
- Tailscale Pi：`https://bwicarus.taile44d0c.ts.net/pdf/`（HTTPS via Tailscale Cert）

iPad 浏览器登录后正常用；触屏拖选 / 左滑 / 色板都做了适配（`touch-action`、`passive`、axis lock）。

QA browser daemon 在 :9091 是单独的，跟 PDF reader 没直接关系（PDF reader 的「问 AI」也用 in-page，不跳 :9091）。

---

## 12. 常用改动 cheat sheet

| 任务 | 改哪 |
|---|---|
| 加新 AI 路由 | `pdf_reader.py` 加 route + 复用 `_ai_call_stream` / `_sse_stream` |
| 改高亮存储 schema | `pdf_reader.py` `_hl_load/_save` + POST/PATCH/DELETE 三个 route 同步 |
| 改预览块单行行数 | popover html template `${h.xxx ? ... : ''}` 三行 |
| 改 swipe 距离 | CSS `.swiped .item{transform:translateX(-Npx)}` 跟 `.del-row{width:Npx}` 同步 |
| 加新色板默认色 | `const DEFAULT_HL_COLORS = [...]` 在 hl 大块开头 |
| 改 AI 设置默认 | 服务端 `state/server-config.json` 的 `ai_*` 字段 |
| 添 PDF 列表过滤 | `_list_vault_pdfs` |
| 调 KG 节点匹配 | `_find_kg_nodes_for_page` |

---

## 13. 待办 / 已知限制

- 多页选中：选区横跨多页时只渲染当前页 sel-overlay（拼文本是 OK 的）
- 高亮 import/export：暂无 UI，可直接复制 `state/pdf-highlights/*.json`
- 高亮搜索：暂无 UI 列出某 PDF 所有高亮的概览（只能逐个点开）
- 多用户：高亮 sidecar 全 user 共享（一个 vault 一个 sha1）；如果未来要 per-user 可改路径加 username 前缀

---

## 14. 2026-05-31 批次：日语支持 + 性能 + 翻译显示（坑 + 结论）

### 14.1 日语词典（详见 [`vocab-system.md`](vocab-system.md) §14）
点日语词：小框 = 读音 + **音调线**（unidic aType，`_renderPitch` 画高低 overline + 下降标记）+ 中文 + 2 句母语例句。「展开完整字典」走 `dictStreamJP`（`/api/dict-jp`）：多义释义 + **汉字 chip 拆解**（点字展开音读/训读，默认展开首字）+ 5 句 Tanaka 母语例句 + 「✨AI 深入」按需 SSE。`_isJaWord(w)` 据 `BOOK_LANGS` 路由日/英。

### 14.2 整页只能选单字（root cause = 缓存，非代码）
- 现象：某些页每个汉字/假名都只能选 1 个字。
- 真因：`/api/page-chars` **之前无 Cache-Control** → iOS Safari **启发式缓存**了 fugashi 分词上线**前**的旧 JSON（每字 `w` 各自独立）。后端 `w` 一直对、HTML 早 no-store、JS 最新，唯独 page-chars 吃旧缓存。
- 修：page-chars 响应加 `no-store/no-cache/must-revalidate + Pragma`；前端请求加 `?v=<pdf mtime>` 换 cache key（立即绕开旧缓存，不用用户清缓存）。
- 排查留痕：全书端到端复刻 0 退化页；非传递排序比较器只拆散 3/261（**不是**根因，且改传递性排序会打乱英文混排页 reading order，故没动）。

### 14.2.1 「某些页整页单字」复发 = 坏缓存（fugashi 临时挂掉时写入）
`state/pdf-char-cache/<sha>-p<page>-<mtime>.json` 若在 **fugashi 临时不可用**（`_get_jp_tagger()` 返回 None）时被写入，会存下「全 `w=-1`（无分词）+ `furigana=[]`」的坏结果；F5 的「无 furigana 键才重算」守卫挡不住它（它有 furigana 键，只是空）→ 之后**永久命中坏缓存 → 那几页整页单字选中**。根治（commit 见 §15.2）：① `_CHAR_CACHE_VER`（缓存 schema 版本号，写进 payload，读时版本不符就重算——改抽取/分词逻辑就 +1 一次性废掉所有旧缓存）。**前端 page-chars 缓存键也并入它**(`CHARS_VER = PDF_mtime + "." + __PDF_CFG.chars_ver`),所以 bump 版本同时冲掉 iOS Safari 同 mtime 下的旧分词缓存(否则后端改了、客户端还命中旧"单字"缓存)；② **安全阀**：`_page_chars_cached` 里若 `tagger is None && 有 CJK` 或 `有汉字却 furigana 为空` → 判分词失败、**不写缓存**（返回结果但等 fugashi 恢复后重算）。排查命令：`_compute_page_chars` 现算 vs 缓存里各页 CJK 的「多字共享 w 的 token 数」对比，缓存=0 而现算>0 即坏缓存。

### 14.3 加载像在「下整本书」（disableStream）
- PDF.js **`disableAutoFetch:true` 必须同时 `disableStream:true` 才生效**（官方）。之前 `disableStream:false` → 即便禁了预取仍后台**流式下整本 408MB**（进度条跑的就是整本）。range 已确认 Flask+nginx 两端都 206，故关 stream 纯按需取当前页。

### 14.4 大 PDF 打开慢（对象结构病，根治）
- `応用情報技術者.pdf` = 679 张全页 JPEG（1352×1920，~536KB/页）+ 隐藏文字层，**48.9 万个对象**（epub→PDF 把每页文字层拆成几百个碎流），PDF 1.3 **经典 xref 表 ~9.3MB** → PDF.js 打开（尤其恢复到深层页）必须先下完整 xref，极慢。
- 修：`scripts/optimize_pdf.py`（PyMuPDF `garbage=4 + clean=True 合并内容流 + deflate` → qpdf `--linearize`，**不动图片像素=清晰度无损**，多页抽样校验文字+图尺寸一致才落地、自动备份到 `data/pdf-backup/`）。该书 489786→**2045 对象**，408→318MB，xref 9.3MB→~40KB。
- page-chars 落盘缓存：`_page_chars_cached`（键 = rel+page+mtime，存 `state/pdf-char-cache/`），只缓存不变的 chars/page_w/page_h；vocab_marks/句子框（依赖可变掌握度）每次现算。重复打开同页 387ms→13ms。
- **「打开即可用」加载顺序**（`setupContinuousMode`）：旧逻辑先 `observe` 全部占位（IntersectionObserver 立刻渲染页 1-3，再 50ms 后才滚到目标页）→ 白白渲染没人看的页 1-3、和目标页抢带宽。改为：① 先 `scrollIntoView` 到目标页（占位高已按首页估算、各页同尺寸→定位准）② **`await _renderPageInto(目标页)` 显式渲染并等它就绪**（图像+选词层=可用）③ 才 `observe` 其余页懒加载（视口已在目标页 → 只渲染其 ±1400px 邻页，目标页 loaded=1 跳过）。打开只渲染 1 页即可用,其余后台补。
- **加载遮罩别等渲染**:`#pdf-loading` 全屏遮罩只该挂到「文档结构就绪 + 占位建好 + 滚到目标页」,**不能等目标页图像渲染完**(否则遮罩挂太久)。故 setupContinuousMode 里目标页渲染 `_renderPageInto(...).catch()` **不 await**,`loadPdf` 的 `pdfLoadHide()` 紧随其后即触发 → 遮罩秒撤、先显占位「…第N页」、目标页图像几百ms 后弹出。(教训:别为了"打开即可用"把整页渲染塞进遮罩等待里,那只会让遮罩更久。)
- **PDF 字节浏览器缓存**:`/pdf/file` 的 Cache-Control 从 `max-age=0, must-revalidate`(每块都回服务器校验→每次打开反复读)改成 **`private, max-age=31536000, immutable`**。URL 带 `?v=<mtime>`,同一 URL 字节永不变 → 浏览器长期缓存已取的 Range 分块、**重复打开读过的页直接命中本地缓存、零网络往返**;文件改了 mtime 变→新 URL 自然取新版,不串味。注:首次打开仍要取;只缓存实际看过的页;iPad Safari 缓存有容量上限,超大书只保最近用的块。(为什么这本书慢:它是 318MB 扫描图书、每页 ~644KB 大图,其它书是几 MB 矢量文字书、整本秒下;这是物理下限,缓存只能让重复打开快。)
- **PDF.js 库静态缓存**(nginx,Pi 手工 patch):`/static/pdfjs/pdf.mjs`(625KB)+`pdf.worker.mjs`(**2.19MB**)原来 nginx 无 Cache-Control → 每次刷新都回源校验/重下这 ~2.8MB。两者 import 时都带 `?v=PDFJS_V`(版本变 URL 变)→ 安全设 immutable。Pi nginx `/etc/nginx/sites-available/bwicarus` 两个 server 块各加 `location ^~ /static/pdfjs/ { add_header Cache-Control "public, max-age=2592000, immutable" always; try_files $uri =404; }`(⚠ Pi 专属、**不在 git**,VPS 那份 `_server_deploy/nginx/bwicarus.conf` 结构不同勿混)。
- **「刷新为何不秒开」**:HTTP 缓存只省**字节下载**(PDF 分块 + PDF.js 库);但每次刷新 PDF.js 仍要**重新 init worker + 重解析文档结构 + 重解码当前页那张 644KB 图 + 重渲染 canvas**(这些活在内存、刷新即丢),加上 HTML(`/pdf/view` no-store,取最新代码)+ page-chars(no-store)都重取。所以刷新已大幅变快(不再下 2.8MB 库 + 不再下已缓存的图字节),但非"瞬间"——剩下的是 CPU 重建那部分,属固有。

### 14.5 模块作用域 vs 内联 onclick 全局（反复踩！）
`<script type="module">` 里的 `function foo(){}` **不是全局**，HTML 内联 `onclick="foo()"` 在全局作用域跑 → 找不到 → 静默失败。已中招：`openLangPicker`/`saveLangPicker`（🌐 按钮）、`_ttsWord`（完整字典 🔊 无声）。**规则：任何要被内联 onclick 调的函数必须 `window.xxx = xxx`**。小框 🔊 没事是因为走的是 `window._speakCurWord`（它内部再调模块函数 OK）。

### 14.6 点词竞态
快速点不同词时，前一个慢请求（未缓存日语词要现调 AI）的响应晚到会覆盖当前词。修：`_wordPopSeq` 序号守卫，await 回来若序号已变就丢弃旧响应（catch 分支也守）。

### 14.7 解释/句子要按句号断，不按逗号
`_expandSentenceFromRange` 原来「跨 rawdict 块就断」。日语 justified 排版把同一视觉行拆成多块（同行的「…解き,」「それら…」），按块断会在**逗号**处截断、到不了句号。改为「**跨块 _且_ 跨视觉行才断**」+ 段落大行距兜底：同行拆块继续到 。，标题/邻段（不同行+不同块）仍断开。真实数据验证：直近那句跨逗号到 。，複/構成 句仍正确排除上方绿色标题。

### 14.8 多选翻译显示错位 + 自动弹 + 提速
- **显示（就地覆盖，用户原设计）**：`_drawSentenceOverlay` —— **整句一个白盒 + 中文自然流排**。盒子 = 句子所有 rect 的 union（left/top/宽/高），中文 `white-space:normal` 自然换行（标点跟句子走，不按行硬切），**字号 = 原文字符高**（`charH*0.95`，跟原句一致），`line-height = 原文行距` → 中文逐行落在原文行上。`toggleSentenceOverlay` 画它（再点同句关闭），`onTranslate` 翻完 `btn.click()` 自动画。
  - 演进踩坑：① 最早「单段中文流进按原文每行形状切的 clip-path」→ 中日长度/换行不一致 **必错位溢出**；② 一度改 `showSentenceTranslation` 干净浮层；③ 改回就地覆盖、试过「逐行白条 + 按行宽比例切中文」→ 中文被**按行硬切**导致**标点落在每行奇怪位置、看着错乱**；④ **最终**：整句一个盒子 + 自然流排 + 字号同原文（标点自然、不错开）。`showSentenceTranslation` 浮层函数保留未用。
- **提速** 5s→~1s：见 [`google-cloud-apis.md`](google-cloud-apis.md)（Google Translate 首选、CLI 冷启动/热进程结论、translate.py 两个 guard 坑）。

### 14.10 竖直拖动当滚动、横向拖动才选字(手势消歧)
手指落在可选字符上、想上下滑页时,旧逻辑一拖就竖向选中很多行。改:`_bindCharLayer` 的 `onMove` 里,**触摸**拖动首次动够 8px 时锁定方向 `_dragDir` —— `dy>dx`(竖直为主)→ `scroll`:置 `_dragStartCharIdx=null`、清选区、**不 `preventDefault`**(页面正常上下滚);否则 `select`(原逻辑,preventDefault+选字)。**鼠标(ev=null)不受限**,竖直拖仍可选。妙处:多行选择只要**起手横向**就锁成 select、后续往下拉照常多行选;只有**一上来直上直下**才滚动。(依据:复制几乎都是沿行水平方向。)

### 14.11 top-level await 之后绑 DOMContentLoaded 永不触发(坑,曾静默废掉多个 setup)
`<script type="module">` 顶部有 `await import('/static/pdfjs/pdf.mjs?v=…')`。**top-level await 不延迟 DOMContentLoaded** → 该事件在 await 期间就已触发;await **之后**才执行的 `window.addEventListener('DOMContentLoaded', fn)` 是在事件已过之后注册的 → **fn 永不执行**。这静默废掉了 `_setupPinchZoom`(双指缩放)、`_setupResizeWatcher`、`_applyDebugVisibility`、以及新加的页码 scrubber(完全无反应)。修法(所有 await 之后的 DOMContentLoaded 绑定都改这个稳健模式):`if (document.readyState !== 'loading') fn(); else addEventListener('DOMContentLoaded', fn);`。(教训:有 top-level await 的 module 里,await 之后别再依赖 DOMContentLoaded。)

### 14.9 日语发音
有道 dictvoice 是英语库 → 日语无声。改 `_speakOnline` 日语分流到浏览器原生 `speechSynthesis` **ja-JP**（iPad 自带 Kyoko，离线，念假名读音保证读对；iOS getVoices 暂空时靠 `u.lang='ja-JP'` 路由）。免费真人录音离线日语词典基本不存在（Forvo 要联网+key），合成音够用。

## 15. 2026-06-01 批次：操作性 7 件套（F1–F7）

一次性加的 7 个阅读体验功能。前端验证脚本：`bash scripts/check_pdf_reader_js.sh`（抽 module script → 去 Jinja → `node --check`）。后端实时验证：`cd /home/bwicarus/webapp && set -a && . ./.env && set +a && WEBAPP_DATA=/tmp/wtest_data python3`（test_client + `session_transaction` 绕登录；`/pdf` 在 PROTECTED_PREFIXES，且 app 用 root 的 `/root/webapp/data`，故必须 `WEBAPP_DATA` 指临时目录）。

| 功能 | 顶栏/入口 | 后端 | 前端 |
|---|---|---|---|
| **F1 页码 scrubber** | 顶栏页码 | — | `#page-scrub` 左右拖快速跳页+预览，点击 prompt 输页码 |
| **F2 单页横滑翻页** | 手势 | — | char-layer touchstart 记 `_swipeStart`（**起点空白 + 单页模式**）→ touchend：`|dx|≥55 且 dx>1.6×dy 且 <700ms` → 右滑上一页/左滑下一页。**起点在字上仍走拖选**（不冲突） |
| **F3 整页翻译** | 「译页」 | `_split_page_sentences`（切整页所有句，不限生词不排标题）+ `/api/page-translate`（gtranslate_batch + `_cache_get/put` sidecar 缓存，无 GCP key 逐句兜底限 60 句） | `togglePageTranslate` + `_drawPageTranslate`（`.page-tr-layer` z9，逐行白底中文覆盖，复用 `_drawSentenceOverlay` 的按中心行合并+贪心填充+字号 0.72×原文高；`pointer-events:none` 仍可选原文）。连续模式滚入新页自动译（`__pageTrSeq` 防重复） |
| **F4 全文搜索** | 「🔍」 | `_book_text_index`（全书逐页 `get_text` 磁盘缓存，键含 mtime，679 页首建 ~3s 之后秒读）+ `/api/search`（子串、大小写不敏感、适配中日无词边界，limit 200 页） | `#search-panel` 顶部下拉，debounce 320ms / Enter / Esc，结果页码·命中数+片段 `<mark>` 高亮，点击 `goToPage` |
| **F5 振假名/音标** | 「あ」 | page-chars 新增 `furigana`：日语 unidic `feature.kana`→平假名（`_furigana_item` 剥送り仮名贴汉字核心）+ 英文 `_build_en_furigana`（**单连接直查 ECDICT phonetic，不走 lemma/LIKE 全表扫描**）。**随 chars 进磁盘缓存**（老缓存无该键自动重算）。**分词按段落非逐行**（见下）| `.ruby-layer`（**z-index 8 + `pointer-events:none` → 点击穿透到 char-layer 选词**）+ `toggleRuby`（localStorage `pdf-ruby` 默认关）。字号 `min(词高×0.52, 词宽/读音字数×1.75)` 防溢出 |

> **块内 gutter 切列 —— 漫画并排气泡能单独选中（2026-06，`_CHAR_CACHE_VER`→8）**：并排两个气泡的字选中时混在一起。根因:Vision 按整页视觉行读 → 并排气泡左右**交错**(左行1→右行1→左行2…),且 PyMuPDF 并成同一 block(bk 相同)→ 块过滤分不开、拖选 index 区间跨进另一气泡。修:`_split_block_columns` 在每个 block 内检测**竖直空白条**(gutter)切成左右列。阈值 **CJK 感知**:CJK 列(列内字紧挨)用 `0.8×字宽`、Latin(有词缝)用 `3×字宽`——并排气泡间隙常只有 ~1 字宽(料理师1 p12 左列伸到 x847、右列从 889、仅 42px),固定大阈值切不开。每列给唯一 `bk`(选中/句子框块过滤天然分开)+ 整列一起分词(`_apply_jp_tokenize` 改为接收"一列的 char 列表")。`page-chars` 与 `page-vocab-marks` 两处同改。普通单栏段落无 gutter → 不切(英文教材大段落仍各一个 bk)。

> **分词按段落（非逐行）—— 跨行复合词读对音（2026-06，`_CHAR_CACHE_VER`→7）**：`_compute_page_chars` 原**逐行**调 fugashi → 跨行的词（`間食`：間 行尾、食 行首）被行边界拆成 `間→あいだ`+`食→しょく`（读音全错，单击只选半个词）。改成 **`_apply_jp_tokenize` 整块(段落)调一次**（块内各行本就连续 reading order，拼起喂 fugashi 就把 `間食` 读 `かんしょく`；word_id=`block*1e6+块内token序号`）。跨行 token 的振假名由 `_furigana_item` 只放在**首字所在行的连续片段**上方（取首字 y 同行的连续 char 算 bbox，否则 ruby 飘行间）。副带：单击 `間食` 选中整词。
| **F6 词组模式** | 选中后「📘词组」 | `_merge_favorite_phrases`（收藏词组在页内出现处合并 chars 的 `w`→单击选整词组；空白不敏感/ASCII 大小写不敏感/长词组优先/防重叠/**连续文本流校验：相邻字竖直跳变 >2.2 行高拒绝，防跨段误并**；**live 应用不进缓存**）+ `/api/phrases` CRUD（`state/pdf-phrases.json` 全局） | `_isShortPhrase`（中日 2-8 字/拉丁 2-5 词，无句末标点）→ 工具栏「📘词组」按钮（高亮+呼吸）+ 选中框呼吸 1.6s 转常亮；`showPhrasePopover`（复用 word-pop，JP 走 dict-jp 读音+声调，兜底 translate-sentence）+ ☆收藏切换 → `refreshCharsWForAllPages` 原地更新 `w` |
| **F7 日语生词高亮** | 自动（同英语下划线开关） | `state/jp-vocab.json` store（查过的 JP 词记 looks/last_ts/user_mark）+ `_jp_vocab_bump`（dict-quick want_ja / dict-jp 查词时 looks+1）+ `_build_jp_vocab_marks`（**按 fugashi `w` 分组成 token**，按 mastery 上色画下划线，mastered 不画）。合并进 `vocab_marks`（page-chars + page-vocab-marks，后者补跑 `_apply_jp_tokenize` 让 `w` 存在）+ `/api/jp-vocab-mark`（known/unknown/""/forget） | 复用现有 `.vocab-underline` 渲染；查 JP 词后 `refreshVocabUnderlinesForAllPages` |

**日英 vocab 完全统一 / 日语并入 vault 词库（2026-06，真·一套系统）**：日语生词从独立 `jp-vocab.json`(looks 二元) **整体并入英语那套 vault 笔记库**(`资源/vocab/`)，从此日英共用 `vocab_index` / `compute_mastery` / `apply_user_mark` / `paragraph_exposure` 全套——一套代码、一套数据库、一套算法。
- **JP 笔记生成** `build_vocab_note.update_jp_word_note(lemma, reading, meaning, examples, forms, add_source)`：`compose_entry` 是英语 ECDICT 专属，故日语内容(读音/释义/例句)单写；frontmatter schema 跟英语完全一致(lemma/forms/mastery/mastery_label/last_lookup_ts/user_mark…)，`new_source`(查词)→ mastery 重置 0(同英语:主动查=不会)。
- **分桶按读音首假名(あいうえお順)**：`_word_path` 对日语词(`_is_jp_lemma`)用 `_jp_reading_initial`(unidic 读音首假名，片→平归一) → `资源/vocab/か/確認する.md`（不是按汉字！比汉字分有意义）。**确定性只由 lemma 决定** → `apply_user_mark`(也改用 `_word_path`)等所有工具都能定位同一笔记。
- **查词建笔记 + 句子暴露**：`_trigger_jp_note_async`(dict-quick want_ja / dict-jp 查词时后台) → `update_jp_word_note` + `_jp_exposure`(fugashi 分词整句、同句其他已入库日语词复用 `paragraph_exposure._bump_mastery` +0.05 = 被动暴露提分，跟英语一样)。
- **下划线读 vocab_index**：`_build_jp_vocab_marks` 改读 `_vocab_idx()`(=vocab_index.index())——token 先按表层查、查不到用 `_jp_inflection` 还原原形查(forms 已含活用形)；按 `mastery`/`label_slug` 上色(跟英语同阈值)，mastered 不画。
- **句子框计数 = 下划线(按 w 分组,2026-06)**：`_build_unmastered_sentences` 的 JP 计数原先用 fugashi **重新分词**,跟下划线(按 page-chars 的 `w`)不一致——收藏词组合并成一个 `w`(下划线算 1),但 fugashi 把它拆成内部词素分别计数 → 词组 + 各组成词重复算(用户报「词组 aabb + aa + bb = 3」)。改成 `_flush_word` 的 JP 路径**也按 `w` 分组**(逐 w-group 查 vocab,收藏词组=1 个 token),计数严格 == 下划线。EN 路径(按空格/字母)不变。
- **标记一条路径**：`/api/jp-vocab-mark` → `compute_mastery.apply_user_mark(base, known/unknown)`(原形为键、确保笔记存在)，跟英语 `/api/vocab-mark` 同函数；小框 toggle 日英共用 `_wordPopMaster`。
- **迁移**：`scripts/vocab/migrate_jp_vocab.py` 一次性把 `jp-vocab.json`(161 词) → vault 笔记(mastered→user_mark known，其余按旧分数落库，保留 last_ts/looks/first_seen)；**非破坏**(保留 `jp-vocab.json`+`.bak` 做备份)。旧 `_jp_mastery`/`_mastery_slug`/`_jp_vocab_slug`/`_jp_vocab_bump`/`jp-vocab.json` 自此不再驱动下划线(留作 dead code/备份)。

**单词小框（`showWordPopover`，日英统一，2026-06）**：日语/英语共用同一套 UI——词头(原型/lemma) + 音标/声调 + 词性标签 + 释义 + **变形行**（日 `_jpInflectHtml` 原形+语法标签 / 英 `_enFormsHtml` 原型+各种屈折）+ 例句(日Tanaka) + 两个动作按钮：**「☆标记掌握 / ✓已掌握 100」toggle**（统一 `_wordPopMaster`，按 `_wordPopState.jp` 分流：日→`/api/jp-vocab-mark` mastered/unknown，英→`/api/vocab-mark` known/unknown；掌握后该词不再画生词下划线，可来回切）+ 「📊语法」。dict-quick 日英都返回 `mastered` 给按钮初始态（英语态 = `_en_word_mastered`：vocab_index `label_slug=='mastered'`）。**英语小框不再有「制 Anki」按钮**（制卡走完整字典大框「🎴加入 Anki」/`addVocabAnki`，查词时已后台建 vocab 笔记）。`reader.src/15-phrase-wordpop.js`。|

**关键设计决策**
- F5/F7 都**按 `w`（fugashi token / 收藏合并）分组**，跟单击选词同一套词边界 → 振假名落在词上、生词下划线整词、收藏词组单击整选，三者一致。
- F3/F5/F7 的重活（振假名读音、整页翻译、搜索索引）全部**磁盘缓存键含 mtime**，首次慢、之后秒读；F6 收藏合并 `w` 是**每请求 live 应用**（不进缓存，否则改收藏要等缓存失效）。
- F5 ruby 层 `pointer-events:none` + 高 z-index：盖在最上但点击穿透，选词不受影响。F3 page-tr 层同理（z9 盖住 ruby）。
- **整页翻译 `_drawPageTranslate` = 行间对照（2026-06，用户最终选定）**：翻译演进史 = 就地白底覆盖（译文盖原文）→ 自适应字号 → 白底遮罩+字高量化 → reflow 重排（被否，位置全乱）→ **回退就地** → 用户提议「译文放到振假名那个位置」。**结论：行间对照（interlinear）**——原文**不遮**、译文当**行上方小字**：
  - 每句用 `_mergeLines()` 合并成视觉行，译文 `Array.from(zh)` 按行**贪心分配**（每行 `cap=floor(w/annFs)` 个汉字，末行收尾）；每行片段渲染成 `.page-tr-rt`（半透明白底 `rgba(255,255,255,.86)` + 蓝字 `#0b3d91`）。
  - **定位 = 完全照搬振假名**：`top=y0-fs*0.34`，落在**本行 char bbox 顶部留白**里。⚠ 关键坑：早期放 `y0-fs*0.95`（几乎全在行上方）会向上伸进**上一行的字形**→ 严重压住上一行（用户截图）。char bbox 含上下留白（字形只占中段 ~70%），放进**本行自己的顶部留白**才不压上一行——这正是振假名能跟密排正文共存的原因。也试过按 bbox 行间空隙 gap 反推字号，但 bbox 间隙（~5pt）远小于真实视觉空隙（含留白 ~25pt），把字号压到 7px 不可读，弃用。
  - 字号 `annFs=max(7, 行高 h×0.40)`（≈振假名 0.36 略大；随原文行高 → 同档正文统一、标题更大）；实际 `fs=max(7, min(annFs, w/sliceLen))`（片段偏长则压进行宽，保证单行不外溢）。
  - **注音(`あ`)与译页(`译页`)互斥**：`toggleRuby`/`togglePageTranslate` 互相关掉对方（清 layer + 按钮 + flag/localStorage），因为两者都占「行上方空隙」。`renderRubyLayer` 仍按 `_rubyEnabled()` 自守。
  - 取舍：原文保留可见、阅读顺序天然正确（译文贴在各自原文行上方）；代价是密排页行间空隙小、译文小字可能与上一行轻微叠（半透明底缓解）。`_maskLines`/字高量化/`gScale`/`.page-tr-mask`/`.page-tr-line` 全删。实测 p24 行片段 0 外溢、字号中位 26px。
  - 单句翻译 `_drawSentenceOverlay`（选一句译）**仍是就地白底覆盖**（一次一句、贴原文行有对照价值）：`min(自然字高, floor(Wtot/(N+行数)))` 自适应 + 放不下换行兜底。
- F2 横滑严格限「起点空白 + 单页模式」：尊重既有「起点在字上=拖选、竖滑=滚动」规则，零冲突。

**验证结论**（独立 reviewer 审 `6ed9d9c..` 全 diff）：无崩溃/解包遗漏/未定义符号/重复声明/死锁/越界/回归；4-tuple 解包、translate 签名、window 挂载、store 键、坐标缩放全部跨函数核对一致。実测 応用情報 p22 振假名 169 条、整页翻译 38 句全译（冷 439ms/缓存 103ms）、搜索 試験 256 处（冷 2.6s/缓存 18ms）；EGIU/线代书 IPA 261 条/66ms。

### 15.1 F1-F7 反馈修复 round（2026-06-01）

用户实机反馈后的 5 项修正（诊断 + 修复 + 对抗复审都走了 workflow）：

- **F2 横滑翻页**（二轮定稿）：根因之一是 touchcancel——浏览器把横滑判成滚动会 **触发 touchcancel 清掉滑动状态** → touchend 拿不到 swipe；必须在 touchmove **早早 preventDefault 认领**横向手势。最终按用户要求改为「**单页模式整页任意处都可横滑翻页**」（不再限空白）：touchstart 在单页模式记 `_swipeStart`（不管命中字否）；touchmove 移动 >8px 时定方向，横向→`h=true` 放弃 tap 选词 + 每 move preventDefault，竖向→`_swipeStart=null` 交回原生滚动；touchend 横向位移 ≥40px → `changePage(dx>0?-1:1)`。**单页模式不做拖选**（tap 选词 / 横滑翻页 / 竖滑滚动；要拖选用连续模式或词组按钮）。连续模式 touch 行为不变（仍走 `_dragDir` 拖选）。桌面 mouse 不受影响（仍拖选）。
- **F5 振假名位置**（二轮定稿，坑）：这本扫描书 **行距 ≈ 字框高（44pt vs 40pt），行间仅 ~4pt**——注音放任何可读字号都会侵入上一行；且 **OCR 字框顶部有 padding（框比实际字形高）**，按「框顶上方」放就更高、窜进上一行。最终：**字号 0.36×词高 + ruby 中心对齐汉字框顶线 `top=y0-fs*0.5`**（一半落进本行字框顶 padding=视觉空隙、一半在框顶上）→ 紧贴自己这行汉字正上方、基本不碰上一行视觉字形。CSS `.rt` line-height .95、白底 .72 透明。（教训：扫描书 furigana 定位要按「视觉字形」而非「OCR 框」，框有 padding；中心对齐框顶是补偿 padding 的简单稳健办法。）
- **F6 词组高亮 = 可点击持久层**（多轮定稿）：用户最终要的交互——拖选出短词组 → 建独立 `.phrase-hl-layer`（蓝框、`.hl` 上 `pointer-events:auto` 可点、记 `dataset.phraseText`），呼吸 1.6s 转常亮、**一直留着**；**点空白/点其他词都不消失**（独立于 sel-overlay 选词逻辑，onStart 清的是 sel-overlay 不碰这层）；**只有手动点这个高亮**（或 📘词组按钮）→ `showPhrasePopover` 打开词组结果 → 结果出来 `_removePhraseHighlight()` 移除（**不是定时淡出，是被打开动作触发移除**）。坑：高亮自带文本存 `layer.dataset`，因为 `lastSelText` 会被点空白清掉。F5 注音 `top` 二次微调到 `y0-fs*0.34`（用户要求再向下贴本行）。
- **F4 搜索原文高亮**：`_searchJump` 记 `_pendingSearchHighlight={query,page}` → goToPage；**loadCharsAndBindLayer 末尾**（此处 `__charBoxes` 已赋值）直接 `_highlightSearchResultsOnPage` 一次到位（不走轮询 re-check `dataset.loaded`，那个时序脆弱）；已加载页则 `_searchJump` 里的轮询 fallback 立即命中。命中处黄色 `.search-hl`（z6 + pointer-events:none + multiply），滚到第一处，6s 淡出。
- **F7 掌握键改 toggle**：`dict-quick(want_ja)` 返回 `mastered`；按钮显示当前态（未掌握「☆标记掌握」↔ 已掌握「✓已掌握100」），点击 toggle（mastered↔unknown）**不关框**，刷下划线。后端 `/api/jp-vocab-mark` 的 `unknown` 即清 mastered。

### 16. 上传 PDF 失败 = nginx 体积限制（2026-06-02）
- 现象：`POST /pdf/api/upload` 前端报「上传失败」，**Flask 日志里看不到这条请求**——被 nginx 在到达 Flask 前就挡了。nginx error.log：`client intended to send too large body: NNNN bytes`（HTTP 413）。
- 根因：扫描书原始 PDF 常 >50m（实测 76m 被挡），nginx `client_max_body_size` 默认/旧值太小。Flask 侧**没设** `MAX_CONTENT_LENGTH`，所以唯一闸门是 nginx。
- 处置：单用户私有实例直接 `client_max_body_size 0`（不限制）。
  - **Pi 活动配置**：`/etc/nginx/sites-available/bwicarus`（手工 patch，**不可从 git cp**，会冲掉 Tailscale 证书），两个 server 块 + /pdf + /api 共 5 处 `client_max_body_size`，`sed` 全改后 `nginx -t && systemctl reload nginx`。
  - **git VPS 配置**：`_server_deploy/nginx/bwicarus.conf` 同步改（部署到 VPS `/etc/nginx/sites-enabled/default`）。
- 排查口诀：上传失败先看 `sudo tail /var/log/nginx/error.log` 有没有 `too large body`；有就是体积限制，跟 Flask/前端无关。

### 17. 大 PDF 加载提速：线性化 + IndexedDB 本地缓存（2026-06-02）
两条互补路线（用户要"下载到本地加快加载"；纯 `file://` 网页打不开，故用浏览器本地缓存等效）：
- **A 服务器端线性化（Fast Web View）**：`preprocess_book.py` 嵌字后用 **`qpdf --linearize`** 把页对象按阅读顺序重排 + 首页/xref 放文件头 → PDF.js 开 url 只取文件头几百 KB 就能渲首页，后续页 byte-range 流式更快。
  - ⚠ **本版 PyMuPDF/MuPDF 已移除 `linear=True`**（`save` 报 `Linearisation is no longer supported`）→ 必须用 qpdf（`/usr/bin/qpdf` v12，rc 0=ok / 3=warnings 都算成功）。缺 qpdf 或失败 → 用未线性化版（不报错）。
  - 存量书一次性线性化：`scripts/linearize_pdf.py <pdf>...` 或 `--vault-larger-than 20`（无损，只重排；会改 mtime → char 缓存 + 前端缓存 key 失效自动重建）。
- **B 浏览器 IndexedDB 整本缓存**（`reader.src/03-loader.js`）：首次打开走流式（线性化后首页快）**+ 后台 4s 后下整本存 IndexedDB**（`db=pdf-blob-cache` store=`pdfs` key=`FILE_REL` value=`{v,buf}`，`v` 绑 `?v=<mtime>` → PDF 变即失效重下）；第 2 次起命中缓存直接 `getDocument({data:buf})` 零网络秒开。
  - **>220MB 不整本缓存**（`_PDF_CACHE_MAX`，`{data}` 模式整本进内存，防 iPad Safari 单页 OOM；408MB 线代书仍走 range 流式）。
  - 设计取舍：故意**不在首次就 `{data}` 全量加载**（会双份内存 + PDF.js transfer detach 竞态）→ 首次流式、后台填缓存、次次秒开，最稳。IndexedDB 不可用（隐私模式/配额）静默回落流式。

### 18. 连续模式内存虚拟化：远页卸载（2026-06-03）
- 现象（用户）：iPad「用别的软件后回来又要重新缓存」。根因：连续模式**只懒加载、从不卸载**——`_renderPageInto` 渲染后 `dataset.loaded='1'` 永久保留 canvas，翻过几百页后所有访问过的页 canvas 全堆 DOM，内存无上限增长（每页 retina canvas ~10MB，100 页 ~1GB）→ iPad 内存吃紧时 **iOS 提前回收整个 Safari 标签**，回来重载/重渲。
- 修（`07-continuous.js`）：`_unloadFarPages`（挂在 `_onContinuousScroll` 200ms 节流尾部，停滚也做最后一次清扫）扫所有 `loaded==='1'` 的 wrap，离视口 **>5000px** 的调 `_unloadPage`：`canvas.width/height=0`（iOS 显式释放 backing）+ 清各 `__*` 叠层引用 + `innerHTML=''` + **恢复成同尺寸占位**（`style.height=offsetHeight`→总高不变、滚动不跳）+ `loaded='0'`；滚回视口由 IntersectionObserver 重渲。
  - **阈值 5000px 必须 > IO `rootMargin`（3000px）**，留 2000px 缓冲，否则边界来回抖动反复卸载/重渲。内存从无上限封顶到 ~±5000px（约 10 页 canvas）。
  - 配套：`04-render.js` wrap 级 ink 监听（pointerdown/touchstart/touchmove）加 `wrap.__inkBound` 守卫——**卸载→重渲同一 wrap 不重复绑定**（否则每次重渲多挂一组 → 泄漏 + 多次触发）。
  - 与 §17 互补：卸载**降低被回收频率**；IndexedDB blob 缓存+persist **让即便被回收、重载也从磁盘读不重新下载**。

### 19. 大文件加载卡在「1%」根治：X-Accel-Redirect（2026-06-03）
- 现象（用户）：317MB 扫描书（応用情報技術者.pdf，679 页，已线性化）打开**卡在「加载中 1%」不动**。
- 排查：文件头有 `/Linearized 1`（线性化 OK，非该问题）；Werkzeug `send_file(conditional=True)` 单测返回 206 正常；**真因 = 经 `nginx → Flask(Werkzeug dev server)` 服务几百 MB**：dev server 吞吐弱 + nginx 默认 `proxy_buffering on` 把每个 range 块先缓冲到磁盘临时文件，大文件链路慢/卡。
- 根治 = **X-Accel-Redirect**：`/pdf/file/<rel>` 鉴权 + `_safe_vault_path` 校验后，**不再 `send_file`**，改返回 `X-Accel-Redirect: /_vault_pdf/<urlquote(rel)>`（+ Content-Type/Accept-Ranges/Cache-Control）；nginx 见此头 → 走 internal location `/_vault_pdf/`（`alias` 到 vault）**原生 sendfile + 原生 Range** 发文件（快、并发好、零 Python、零代理缓冲）。Range 由 nginx 自动套到内部子请求 → 完美 206。
- **env 门控**：`pdf_reader.py` 读 `PDF_XACCEL=1` 才发该头，否则回落 `send_file`（本地开发/未配 nginx 仍可用）。安全性：`/_vault_pdf/` 是 `internal`（直访 404，只能由 Flask 的 X-Accel 触发），暴露面 = `pdf_file` 原本已允许的（`_safe_vault_path` 防穿越）。
- **Pi 实例手工配置（不在 git）**：
  1. `webapp/.env` 加 `PDF_XACCEL=1` → `systemctl restart webapp`。
  2. nginx `/etc/nginx/sites-available/bwicarus` **两个 server 块各加** `location /_vault_pdf/ { internal; alias /home/bwicarus/obsidian/; }`（手工 patch，**勿 cp git**——会冲掉 Tailscale 证书）。`nginx -t && systemctl reload nginx`。
  3. **权限**：nginx worker=www-data，但 `/home/bwicarus` 是 `0700`（www-data 进不去；其下 obsidian/资源/books 本就 world-rx、文件 world-r，唯一卡点是 home 的 traverse）。装 acl + `setfacl -m u:www-data:--x /home/bwicarus`（**仅给 www-data 一个 traverse**，mode 不变、不动 group、对其他用户仍封闭；最小授权）。ACL 存 xattr，持久跨重启。
- **git VPS 配置**：`_server_deploy/nginx/bwicarus.conf` 加了 `location /_vault_pdf/ { internal; alias /root/obsidian/; }`，但 **VPS 默认不设 PDF_XACCEL → 休眠回落 send_file**；要在 VPS 启用须同样设 env + `setfacl -m u:www-data:--x /root`。
- 验证口诀：`curl -k -H "Range: bytes=0-1048575" -b "session=<签名cookie>" https://<host>/pdf/file/<urlquote路径>` → 应 `206` + `Content-Range: bytes 0-1048575/总大小` + 落地正好 1MB；响应头**不该**出现 X-Accel-Redirect（被 nginx 内部消费了）。
