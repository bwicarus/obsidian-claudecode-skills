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
| GET | `/api/page-chars?file=<rel>&page=N&cv=` | PyMuPDF 提取该页所有字符 bbox + 内容（驱动 char-layer）。2026-06-07 拆分后**只回不变的** `{chars,page_w,page_h,furigana}`，`Cache-Control: private,max-age=3600` 可缓存（见 §32）|
| GET | `/api/page-overlay?file=<rel>&page=N` | 该页**可变**叠层：`{vocab_marks,vocab_sentences,offset,cv}`，`no-store`。前端拿 `cv` 再拉可缓存的 page-chars（§32）|
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
| GET | `/api/page-vocab-marks` | vocab：该页生词下划线标记（查词后前端用它刷新下划线；2026-06-10 起与 `/api/page-overlay` 完全同管道、字段一致，见 §35③。page-chars 自 §32 拆分后**不再内嵌** vocab_marks） |
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
| GET | `/api/ping` | 连接质量探针（no-store，前端连接小圆点量 RTT，见 §37②） |
| GET | `/api/cache-stats` | 各层缓存命中/重算计数（进程内，重启清零，见 §37④） |

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
- **连续**：`setupContinuousMode()` 给每页生成 placeholder `.page-wrap[data-loaded=0]`（高度估算自 page1 viewport），`IntersectionObserver`（`rootMargin:3000px`）监视，进入 ±3000px 才真渲染（占位现**分批建**，见 §34②）

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
- 日语生词下划线**密度治理待定**：`_build_jp_vocab_marks` 把库里非 mastered 的词全划，库攒大后词密页几乎全划（机制如实工作，非 bug）；限量/开关/频率阈值还没做，见 §34④

---

## 14. 2026-05-31 批次：日语支持 + 性能 + 翻译显示（坑 + 结论）

### 14.1 日语词典（详见 [`vocab-system.md`](vocab-system.md) §14）
点日语词：小框 = 读音 + **音调线**（unidic aType，`_renderPitch` 画高低 overline + 下降标记）+ 中文 + 2 句母语例句。「展开完整字典」走 `dictStreamJP`（`/api/dict-jp`）：多义释义 + **汉字 chip 拆解**（点字展开音读/训读，默认展开首字）+ 5 句 Tanaka 母语例句 + 「✨AI 深入」按需 SSE。`_isJaWord(w)` 据 `BOOK_LANGS` 路由日/英。

### 14.2 整页只能选单字（root cause = 缓存，非代码）
- 现象：某些页每个汉字/假名都只能选 1 个字。
- 真因：`/api/page-chars` **之前无 Cache-Control** → iOS Safari **启发式缓存**了 fugashi 分词上线**前**的旧 JSON（每字 `w` 各自独立）。后端 `w` 一直对、HTML 早 no-store、JS 最新，唯独 page-chars 吃旧缓存。
- 修：page-chars 响应加 `no-store/no-cache/must-revalidate + Pragma`；前端请求加 `?v=<pdf mtime>` 换 cache key（立即绕开旧缓存，不用用户清缓存）。（历史：§32 拆分后 page-chars 改回**可缓存** `max-age=3600`，靠 `&cv=` 换 key 防陈旧；`no-store` 移到 page-overlay）
- 排查留痕：全书端到端复刻 0 退化页；非传递排序比较器只拆散 3/261（**不是**根因，且改传递性排序会打乱英文混排页 reading order，故没动）。

### 14.2.1 「某些页整页单字」复发 = 坏缓存（fugashi 临时挂掉时写入）
`state/pdf-char-cache/<sha>-p<page>-<mtime>.json` 若在 **fugashi 临时不可用**（`_get_jp_tagger()` 返回 None）时被写入，会存下「全 `w=-1`（无分词）+ `furigana=[]`」的坏结果；F5 的「无 furigana 键才重算」守卫挡不住它（它有 furigana 键，只是空）→ 之后**永久命中坏缓存 → 那几页整页单字选中**。根治（commit 见 §15.2）：① `_CHAR_CACHE_VER`（缓存 schema 版本号，写进 payload，读时版本不符就重算——改抽取/分词逻辑就 +1 一次性废掉所有旧缓存）。**前端 page-chars 缓存键也并入它**(`CHARS_VER = PDF_mtime + "." + __PDF_CFG.chars_ver`),所以 bump 版本同时冲掉 iOS Safari 同 mtime 下的旧分词缓存(否则后端改了、客户端还命中旧"单字"缓存)；② **安全阀**：`_page_chars_cached` 里若 `tagger is None && 有 CJK` 或 `有汉字却 furigana 为空` → 判分词失败、**不写缓存**（返回结果但等 fugashi 恢复后重算）。排查命令：`_compute_page_chars` 现算 vs 缓存里各页 CJK 的「多字共享 w 的 token 数」对比，缓存=0 而现算>0 即坏缓存。

### 14.3 加载像在「下整本书」（disableStream）
- PDF.js **`disableAutoFetch:true` 必须同时 `disableStream:true` 才生效**（官方）。之前 `disableStream:false` → 即便禁了预取仍后台**流式下整本 408MB**（进度条跑的就是整本）。range 已确认 Flask+nginx 两端都 206，故关 stream 纯按需取当前页。

### 14.4 大 PDF 打开慢（对象结构病，根治）
- `応用情報技術者.pdf` = 679 张全页 JPEG（1352×1920，~536KB/页）+ 隐藏文字层，**48.9 万个对象**（epub→PDF 把每页文字层拆成几百个碎流），PDF 1.3 **经典 xref 表 ~9.3MB** → PDF.js 打开（尤其恢复到深层页）必须先下完整 xref，极慢。
- 修：`scripts/optimize_pdf.py`（PyMuPDF `garbage=4 + clean=True 合并内容流 + deflate` → qpdf `--linearize`，**不动图片像素=清晰度无损**，多页抽样校验文字+图尺寸一致才落地、自动备份到 `data/pdf-backup/`）。该书 489786→**2045 对象**，408→318MB，xref 9.3MB→~40KB。
- page-chars 落盘缓存：`_page_chars_cached`（键 = rel+page+mtime，存 `state/pdf-char-cache/`），只缓存不变的 chars/page_w/page_h；vocab_marks/句子框（依赖可变掌握度）每次现算。重复打开同页 387ms→13ms。
- **「打开即可用」加载顺序**（`setupContinuousMode`）：旧逻辑先 `observe` 全部占位（IntersectionObserver 立刻渲染页 1-3，再 50ms 后才滚到目标页）→ 白白渲染没人看的页 1-3、和目标页抢带宽。改为：① 先 `scrollIntoView` 到目标页（占位高已按首页估算、各页同尺寸→定位准）② **`_renderPageInto(目标页).catch()`（不 await）后台渲染目标页 + 立即 `pdfLoadHide()` 撤遮罩**（占位一就位即可读/可返回，图像随后弹出）③ 才 `observe` 其余页懒加载（视口已在目标页 → 只渲染其 ±3000px（IO `rootMargin`）邻页，目标页 loaded=1 跳过）。打开只渲染 1 页即可用,其余后台补。**占位现分批建（CHUNK=80 + `setTimeout(0)` 让出事件循环），见 §34②**。
- **加载遮罩别等渲染**:`#pdf-loading` 全屏遮罩只该挂到「文档结构就绪 + 占位建好 + 滚到目标页」,**不能等目标页图像渲染完**(否则遮罩挂太久)。故 setupContinuousMode 里目标页渲染 `_renderPageInto(...).catch()` **不 await**,`loadPdf` 的 `pdfLoadHide()` 紧随其后即触发 → 遮罩秒撤、先显占位「…第N页」、目标页图像几百ms 后弹出。(教训:别为了"打开即可用"把整页渲染塞进遮罩等待里,那只会让遮罩更久。)
- **PDF 字节浏览器缓存**:`/pdf/file` 的 Cache-Control 从 `max-age=0, must-revalidate`(每块都回服务器校验→每次打开反复读)改成 **`private, max-age=31536000, immutable`**。URL 带 `?v=<mtime>`,同一 URL 字节永不变 → 浏览器长期缓存已取的 Range 分块、**重复打开读过的页直接命中本地缓存、零网络往返**;文件改了 mtime 变→新 URL 自然取新版,不串味。注:首次打开仍要取;只缓存实际看过的页;iPad Safari 缓存有容量上限,超大书只保最近用的块。(为什么这本书慢:它是 318MB 扫描图书、每页 ~644KB 大图,其它书是几 MB 矢量文字书、整本秒下;这是物理下限,缓存只能让重复打开快。)
- **PDF.js 库静态缓存**(nginx,Pi 手工 patch):`/static/pdfjs/pdf.mjs`(625KB)+`pdf.worker.mjs`(**2.19MB**)原来 nginx 无 Cache-Control → 每次刷新都回源校验/重下这 ~2.8MB。两者 import 时都带 `?v=PDFJS_V`(版本变 URL 变)→ 安全设 immutable。Pi nginx `/etc/nginx/sites-available/bwicarus` 两个 server 块各加 `location ^~ /static/pdfjs/ { add_header Cache-Control "public, max-age=2592000, immutable" always; try_files $uri =404; }`(⚠ Pi 专属、**不在 git**,VPS 那份 `_server_deploy/nginx/bwicarus.conf` 结构不同勿混)。
- **「刷新为何不秒开」**:HTTP 缓存只省**字节下载**(PDF 分块 + PDF.js 库);但每次刷新 PDF.js 仍要**重新 init worker + 重解析文档结构 + 重解码当前页那张 644KB 图 + 重渲染 canvas**(这些活在内存、刷新即丢),加上 HTML(`/pdf/view` no-store,取最新代码)+ page-chars(当时 no-store;§32 后已可缓存)都重取。所以刷新已大幅变快(不再下 2.8MB 库 + 不再下已缓存的图字节),但非"瞬间"——剩下的是 CPU 重建那部分,属固有。

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
| **F3 整页翻译** | 「译页」 | `_split_page_sentences`（切整页所有句，不限生词不排标题）+ `/api/page-translate`（gtranslate_batch + `_cache_get/put` sidecar 缓存，无 GCP key/Google 故障时逐句兜底限 60 句；2026-06-10 起兜底走 `backend="no_ai"` + 10s 墙钟预算，见 §35④） | `togglePageTranslate` + `_drawPageTranslate`（`.page-tr-layer` z9，逐行白底中文覆盖，复用 `_drawSentenceOverlay` 的按中心行合并+贪心填充+字号 0.72×原文高；`pointer-events:none` 仍可选原文）。连续模式滚入新页自动译（`__pageTrSeq` 防重复） |
| **F4 全文搜索** | 「🔍」 | `_book_text_index`（全书逐页 `get_text` 磁盘缓存，键含 mtime，679 页首建 ~3s 之后秒读）+ `/api/search`（子串、大小写不敏感、适配中日无词边界，limit 200 页） | `#search-panel` 顶部下拉，debounce 320ms / Enter / Esc，结果页码·命中数+片段 `<mark>` 高亮，点击 `goToPage` |
| **F5 振假名/音标** | 「あ」 | page-chars 新增 `furigana`：日语 unidic `feature.kana`→平假名（`_furigana_item` 剥送り仮名贴汉字核心）+ 英文 `_build_en_furigana`（**单连接直查 ECDICT phonetic，不走 lemma/LIKE 全表扫描**）。**随 chars 进磁盘缓存**（老缓存无该键自动重算）。**分词按段落非逐行**（见下）| `.ruby-layer`（**z-index 8 + `pointer-events:none` → 点击穿透到 char-layer 选词**）+ `toggleRuby`（localStorage `pdf-ruby` 默认关）。字号 `min(词高×0.52, 词宽/读音字数×1.75)` 防溢出 |

> **块内 gutter 切列 —— 漫画并排气泡能单独选中（2026-06，`_CHAR_CACHE_VER`→8）**：并排两个气泡的字选中时混在一起。根因:Vision 按整页视觉行读 → 并排气泡左右**交错**(左行1→右行1→左行2…),且 PyMuPDF 并成同一 block(bk 相同)→ 块过滤分不开、拖选 index 区间跨进另一气泡。修:`_split_block_columns` 在每个 block 内检测**竖直空白条**(gutter)切成左右列。阈值 **CJK 感知**:CJK 列(列内字紧挨)用 `0.8×字宽`、Latin(有词缝)用 `3×字宽`——并排气泡间隙常只有 ~1 字宽(料理师1 p12 左列伸到 x847、右列从 889、仅 42px),固定大阈值切不开。每列给唯一 `bk`(选中/句子框块过滤天然分开)+ 整列一起分词(`_apply_jp_tokenize` 改为接收"一列的 char 列表")。`page-chars` 与 `page-vocab-marks` 两处同改（历史；2026-06-10 起 page-vocab-marks 复用 `_page_chars_cached` 同一管道，不再有第二份内联实现，见 §35③）。普通单栏段落无 gutter → 不切(英文教材大段落仍各一个 bk)。

> **分词按段落（非逐行）—— 跨行复合词读对音（2026-06，`_CHAR_CACHE_VER`→7）**：`_compute_page_chars` 原**逐行**调 fugashi → 跨行的词（`間食`：間 行尾、食 行首）被行边界拆成 `間→あいだ`+`食→しょく`（读音全错，单击只选半个词）。改成 **`_apply_jp_tokenize` 整块(段落)调一次**（块内各行本就连续 reading order，拼起喂 fugashi 就把 `間食` 读 `かんしょく`；word_id=`block*1e6+块内token序号`）。跨行 token 的振假名由 `_furigana_item` 只放在**首字所在行的连续片段**上方（取首字 y 同行的连续 char 算 bbox，否则 ruby 飘行间）。副带：单击 `間食` 选中整词。
| **F6 词组模式** | 选中后「📘词组」 | `_merge_favorite_phrases`（收藏词组在页内出现处合并 chars 的 `w`→单击选整词组；空白不敏感/ASCII 大小写不敏感/长词组优先/防重叠/**连续文本流校验：相邻字竖直跳变 >2.2 行高拒绝，防跨段误并**；**live 应用不进缓存**）+ `/api/phrases` CRUD（`state/pdf-phrases.json` 全局） | `_isShortPhrase`（中日 2-8 字/拉丁 2-5 词，无句末标点）→ 工具栏「📘词组」按钮（高亮+呼吸）+ 选中框呼吸 1.6s 转常亮；`showPhrasePopover`（复用 word-pop，JP 走 dict-jp 读音+声调，兜底 translate-sentence）+ ☆收藏切换 → `refreshCharsWForAllPages` 原地更新 `w` |
| **F7 日语生词高亮** | 自动（同英语下划线开关） | `state/jp-vocab.json` store（查过的 JP 词记 looks/last_ts/user_mark）+ `_jp_vocab_bump`（dict-quick want_ja / dict-jp 查词时 looks+1）+ `_build_jp_vocab_marks`（**按 fugashi `w` 分组成 token**，按 mastery 上色画下划线，mastered 不画）。合并进 `vocab_marks`（由 `/api/page-overlay` 与 `/api/page-vocab-marks` 同管道返回，`w` 来自缓存 chars，见 §32 / §35③）+ `/api/jp-vocab-mark`（known/unknown/""/forget） | 复用现有 `.vocab-underline` 渲染；查 JP 词后 `refreshVocabUnderlinesForAllPages` |

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
- ⚠ **坑（2026-06-03 回归）**：Flask X-Accel 分支**绝不能再设 `Accept-Ranges`**。nginx 服务静态文件时会自己加一个；若 Flask 也加 → 两份合成 `Accept-Ranges: bytes, bytes`，PDF.js 检查 `=== 'bytes'` 不等 → **判定不支持 range → 回退整本下载几百 MB → `Load failed`/极慢**。症状：nginx access.log 里该文件大量 `200`(整文件,client abort 后记录已发字节如 20MB)+`499`,而非 `206`。排查：`curl -I -b session=<cookie>` 看 `accept-ranges` 出现几次（应=1）。curl 带 `-H Range` 仍能 206（curl 不看 Accept-Ranges 直接发 Range），所以**只测 curl Range 会漏掉这个 bug，必须测 Safari/PDF.js 或数 Accept-Ranges 头数量**。

### 20. 句子翻译出现「AI 拒绝」+ 被缓存（2026-06-03）
- 现象：自动生词句的译文浮层显示「我是一个软件工程助手，专注于编程和代码…翻译数学不在我的职责范围」之类——**AI 拒绝**当成了译文。
- 根因：`translate()` auto 链 `gtranslate→deepl→ai→mymemory`，gtranslate/deepl 偶发失败时落到 `_ai_translate`，它用 **`cfg.ai_backend`=`claude_cli`（Claude Code）**——其内置「编程助手」人格**间歇性拒翻**非编程内容，返回的拒绝说明**非空** → `if tr:` 当成功 → **`_cache_put` 缓存了拒绝**（之后永久命中）。
- 修：① `_ai_translate` 加**拒绝识别**（`scripts/vocab/translate.py`）：只匹配拒绝特有的**完整短语**（`"我是一个软件工程"`/`"专注于编程和代码"`/`"不在我的职责范围"`/`"software engineering assistant"`/`"i cannot translate"`…），**不能用「软件工程/编程/代码」单词**否则 IT 教材的正经译文会被误判；命中 → 返回 `None` 落下个源、不缓存。② `translate(..., no_cache=False)`：`重新翻译`(`fresh=1`)绕读缓存、仍写回覆盖坏译文；路由 `/api/translate-sentence` 收 `fresh`，前端 `_sentRetranslate` 带 `fresh:1`。③ 一次性清掉 `state/dict-cache/` 里 45 条已缓存拒绝。
- 注：`claude_cli` 翻译质量好**当它不拒绝时**；拒绝是概率性的。长期更稳的是给翻译走 `claude_api`（直 API + 自定 system prompt，不带 Claude Code 人格），但需配 key；当前用「拒绝识别 + 落 mymemory」兜底已够。
- 2026-06-10 起批量路径（overlay 句子预翻译 §35② + 译页兜底 §35④）已**绝不落 AI CLI**，拒绝暴露面只剩手动单句 `/api/translate-sentence`（仍走配置的 auto 链）。

### 21. 压缩版开关 + 大文件非阻塞加载（2026-06-03）
- 背景:iPad 远程经 Tailscale 连家里 Pi(测得 Pi 上下行 700Mbit/s、本机 nginx 发 318MB 仅 0.5s → **服务器/家宽都不是瓶颈**;iPad 那侧远程网络慢+断续才是)。OCR 质量依赖图像质量,故**原书做 OCR + 默认/好网传输**,慢网才用压缩版。
- **架构**:原书=主文件(`vault/<name>.pdf`,OCR + 默认源);压缩版单独存 `state/pdf-compressed/<sha1(rel)[:16]>.pdf`(**不进 Obsidian 同步**,省得 143MB 同步到所有设备),X-Accel internal location `/_compressed_pdf/`(alias 到该目录,www-data 经 /home/bwicarus 的 ACL 可读)。`.orig.pdf`/`.compressed.pdf` 从 list 扫描排除。
- **compress_pdf.py**:加 `--out`(输出单独文件、不原地替换)+ `--status`(独立状态文件 `<sha>.status.json`,不和预处理 book-preprocess 状态冲突)。
- **后端**(`pdf_reader.py`):`_compressed_paths/_compressed_info`;`pdf_file`/`pdf_view` 支持 `?compressed=1`(X-Accel 到 `/_compressed_pdf/`,pdf_size 用压缩版);`/api/compressed-status`(exists/compressing/percent) + `/api/compress-make`(后台 detached compress_pdf --out --status,默认 max-px 1150 q55 ≈ 省 55%);`_list_vault_pdfs` 带每本 `comp_exists/comp_compressing`。
- **list 页开关**(`pdf_index.html`):`🗜 压缩版` 三态——灰(无压缩版,点击 → compress-make + 呼吸 `.breathing` + 轮询 compressed-status,完成转可用) / ○(有但不用) / ●(用)。偏好存 `localStorage['pdf-use-compressed:'+rel]`;`openBook` 开关开 → 链接带 `&compressed=1`。
- **reader**(`reader.src`):`__PDF_CFG.compressed/comp_avail`;`loadPdf` 起 13s 计时,若仍没出首页 + 有压缩版 + 当前没在用 → 加载层显「⚡ 切换压缩版」按钮(`_switchToCompressed` 记偏好 + 重载带 `&compressed=1`)。
- ⚠ **大文件加载改非阻塞**(撤回更早版本的"阻塞式整本下载"——慢网下盯进度条等几分钟是噩梦):**range 立即看当前页** + 整本在**后台分块续传缓存**(`_fetchFullWithProgress(url,{silent,priority:'low'})`,6MB 块、每块退避重试抗中断,≤220MB 才存);小文件(<30MB)才前台整本取。**iOS Safari 无 Background Fetch API → 无法"关掉浏览器还在后台下",只能阅读器开着时后台缓存**(已如实告知用户)。
- 验证:Pi 端 curl 确认 `?compressed=1` 返回 206/来自压缩版(Content-Range .../149MB);compressed-status exists:true;view?compressed=1 的 __PDF_CFG compressed:1/comp_avail:1/size=压缩版。

### 22. 图片模式：服务端按页出图(大型文档网站成熟方案)+ read-ahead 预取（2026-06-03）
- 起因:用户指出之前每次打开都重拉整本(nginx 日志实测 15734 个 1MB range = 11.5GB)。range 模式 PDF.js 对扫描书 disableAutoFetch 挡不住、且 >220MB 无法缓存 → 每开重拉。用户要求**参考大公司成熟方案**(已记 memory `prefer-big-company-solutions`)。
- **成熟方案 = 服务端按页渲染成图,客户端只按需取看到的页**(Google Books / Scribd / issuu)。`_imgMode` 默认开(`localStorage['pdf-img-mode']='0'` 关作安全阀,回退经典 PDF.js canvas 模式)。
- **后端**:`/api/book-meta`(页数+首页尺寸 pt,不下载 PDF)+ `/api/page-image?file=&page=&w=`(PyMuPDF `get_pixmap` 渲染该页→JPEG q78,磁盘缓存 `state/pdf-page-img/<book_sha>-p<page>-w<w>-<mtime>.jpg`,~285KB/页@1200px,首次 ~0.35s、缓存后 ~0.01s)。2026-06-10 起精确宽 miss 时有**宽度容差回退 + 后台补渲精确宽**(同页其它宽度的缓存图直接可用,见 §35①)。
- **前端**:① boot:图片模式**跳过 PDF.js 库 import**(省 2.8MB + 那 5 秒"import PDF.js"等待)。② loadPdf:图片模式只取 book-meta 建 **pdfDoc shim**(`{numPages, getPage:()=>({getViewport:({scale})=>({width:page_w*scale,height:page_h*scale,scale})})}`)→ 其余代码(setupContinuousMode/_refitToWidth/_computeFitScale)靠 shim 照常工作,**完全跳过下载 PDF/IndexedDB 整本缓存那套**(都在 `else` 非图片模式分支)。③ `_renderPageInto` 开头 `if(_imgMode){await _renderPageImg();return;}`:`_renderPageImg` 用 `<img src=page-image?w=cw*dpr>` 代替 canvas,其余叠层(sel-overlay/char 层/墨迹)照搬。
- **关键能成立的原因**:选词/高亮/振假名/搜索全走 **char 层**(`loadCharsAndBindLayer`,数据来自 `/api/page-chars` PyMuPDF),它**只用 `viewport.scale`(数字)+ 自己做 PDF→viewport 坐标转换**,不调 PDF.js 方法 → shim 的 viewport 够用。`__itemBoxes/__textDivs`(legacy textlayer 选中)已被 char 层取代,图片模式不建它们不影响。
- **缓存 + read-ahead**(`_prefetchAround`):已加载页被 page-image 的 `Cache-Control: immutable` 缓存(re-view 零网络);翻到某页后**低优先级预取前后 ±3 页**(偏向后页,顺序阅读)→ 消费 blob 进缓存 → 翻页瞬开。`_prefetched` Set 去重。渲染完当前页(延后 400ms)+ 连续模式翻到新页触发。
- 效果:打开书**不下载整本、不加载 PDF.js**,只下看到的页(每页 ~285KB),慢网秒翻。图片模式下原书 vs 压缩版的区分基本无意义(按屏幕尺寸出图本就小);压缩版开关仅留给经典模式。
- ⚠ shim 用首页尺寸算所有页 cssW/cssH。**已渲染页**：`_renderPageImg` 改按**图自身真实宽高比**定高（不再用 page1 高），根治扫描书「每页高度不同 → 越往下错位越严重」（详见 §34③，2026-06-08）。**残留**：未渲染的占位仍按 page1 估高（`07-continuous.js` `estH`），滚动微抖。

### 23. Service Worker + Cache API：页图持久缓存(抗 iOS 清 + 离线)（2026-06-03）
- 动机:HTTP `immutable` 缓存能存页图,但 iOS Safari 定期清(7天 ITP/存储紧张)→"过几天又重下"。用 PWA 标准做法(Google Docs/Books 同款)持久化。
- `/pdf/sw.js`(Flask 路由,内联脚本):**必须从 `/pdf/` 下提供**——SW 作用域=其所在目录,要覆盖 `/pdf/api/page-image`;响应带 `Service-Worker-Allowed: /pdf/` + `Cache-Control: no-cache`(SW 更新及时拉取,改 `CACHE` 版本号即生效)。
- SW 策略:**只接管 `GET /pdf/api/page-image`**(cache-first:命中 Cache Storage 零网络/离线;未命中 fetch+put)。**其余请求一律 `return`(不 respondWith)→ 浏览器默认处理**,绝不拦截 SSE/POST/查词(否则会断流/出错)。`activate` 删非当前版本缓存。
- 注册:boot `navigator.serviceWorker.register('/pdf/sw.js',{scope:'/pdf/'})` + `navigator.storage.persist()`(配合 Cache Storage,系统几乎不清)。
- 三层缓存叠加:① page-image immutable HTTP 缓存(同会话快) ② Cache Storage(SW,跨会话/抗清/离线) ③ read-ahead 预取(`_prefetchAround` 前后±3页 → 经 SW 进 Cache Storage)。看过+预取的页持久存住,翻页瞬开、离线可读。
- 注:Cache Storage 会随阅读增长;`pdf-pages-v1` 版本号 bump 会清旧缓存。需要时可加按页数/容量的 LRU 修剪(暂未做,靠 iOS 配额兜底)。

### 24. 跨端偏好同步：PWA ↔ Safari ↔ 多设备（2026-06-03）
- 动机:用户反馈「装到主屏的 PWA 里的 PDF 设置、阅读进度(当前页)、旋转排版都跟 Safari 不同步」。**根因:iOS 把"安装的 PWA(standalone)"和"Safari 标签"当成两个独立存储沙箱**——localStorage 不互通。阅读器所有偏好(设置面板项、`pdf-last-positions` 阅读进度、`pdf-layout:*`/`pdf-auto-orient` 旋转排版)都存 localStorage → 两边各存各的,旋转不切排版/进度不还原全是因为目标沙箱里这些键是空的。
- **成熟方案 = 服务端按用户存偏好,各端启动时拉取**(Kindle/Google Books 同步阅读进度同款)。
- **后端**(`pdf_reader.py`):`/pdf/api/prefs` GET 回 `{ok, prefs:{...}}`,POST `{patch:{k:v|null}}` 合并(null=删键);存 `state/pdf-prefs/<safe-username>.json`(用户名经 `[^A-Za-z0-9_.-]→_` 清洗,无 session 回 `anon`)。需登录(`/pdf` 蓝图本就在 PROTECTED_PREFIXES,未登录 GET 会 302 到 /login,前端 fetch 跟随后拿到 HTML→`.json()` 抛错被 try/catch 吞,优雅降级)。
- **前端**(`01-boot.js` 顶部,**必须在任何 `pdf-*` localStorage 读取之前**):
  - `_seedPrefs()`:GET 偏好,用**原始 `setItem`**(`_origSetItem`,不触发回传)灌进 localStorage;跳过本次会话已本地改过的键(`_prefTouched` Set,防后台刷新覆盖刚改的值)。
  - **SWR(stale-while-revalidate)**:本沙箱**首次**(无 `pdf-prefs-synced` 标记)才 `await` 阻塞拉取(否则下面 `_imgMode`/`readMode`/orient 读到空)——配 **1.5s 超时**防慢网卡死;之后每次用本地已同步值**秒开** + 后台静默 `_seedPrefs()` 刷新(跨设备改动延迟"下一次打开"生效)。`pdf-prefs-synced` 标记用 `_origSetItem` 写 → 不回传 → 永不落服务端 → 每个沙箱独立判首次。
  - **回传**:override `localStorage.setItem`——凡 `pdf-*`(排除大块 `pdf-qhist-*` 问答历史、排除 `pdf-prefs-synced`)写入都进 `_prefQueue`,防抖 1.5s POST `{patch}`。`_prefTouched.add(k)` 标记。
- 效果:设置/阅读进度/旋转排版三者在 PWA、Safari、多设备间自动同步;旋转排版恢复正常(`pdf-auto-orient`+`pdf-layout:*` 带过去了)。**注:旋转自动切排版是 opt-in**(设置里开`pdf-auto-orient`);宽度适应随容器宽变由 ResizeObserver 始终重算(与同步无关)。
- ⚠ 单用户低冲突场景够用(末写胜),没做向量时钟/冲突合并。`pdf-qhist-*` 故意不同步(体积大、本就各端独立查看)。

### 25. 全局搜索：跨所有 PDF 书的全文（SQLite FTS5 trigram）（2026-06-03）
- 需求:用户选「全部 PDF 书的正文」搜索(Google Books 图书馆搜索体验),点结果直接深链到阅读器对应页。**不建笔记查看器**(只搜 PDF 正文)。
- **索引器** `scripts/build_search_index.py`:扫 vault 所有 PDF(排除 `.orig/.compressed`),逐页纯文本入 **SQLite FTS5**(`state/pdf-search.db`)。
  - **trigram 分词器**:中/日/英统一**子串匹配**(避开 unicode61 把整段 CJK 当一个 token 的坑),≥3 字走 FTS5 + **bm25 排序**。
  - **external-content + 触发器**:`pages_data`(真源 id/file/page/body)+ `pages_fts`(`content='pages_data'`)+ ai/ad/au 三触发器自动同步 → 删书 = `DELETE FROM pages_data WHERE file=?`,FTS 自动跟删。
  - **增量**:`meta` 表记每本 mtime,只重建新增/改动的书;磁盘消失的书清出。页文本**复用** `state/pdf-text-index/<sha1(rel)[:16]>-<mtime>.json`(与阅读器 F4 单本搜索共享缓存)。`optimize` 仅在有变动时跑。
  - 实测:24 本 9959 页全量 ~38s;无变动增量 ~0.07s。少数扫描书(无文字层)只索引到很少页 → 需 OCR 流水线补文字层(已知限制)。
- **后端**(`pdf_reader.py`):
  - `GET /pdf/search` → 渲染 `pdf_search.html`。
  - `GET /pdf/api/global-search?q=&limit=` → `{ok,q,books:[{file,name,dir,hits,pages:[{page,snippet,pos,qlen}]}],total_books,total_hits,truncated}`。**q≥3 字**:FTS5 `MATCH '"..."'`(双引号当字符串避开查询语法)+ bm25 order;**q<3 字**(如 2 字 CJK 词「情報」「向量」):trigram 无法匹配 → `body LIKE '%q%' ESCAPE` 子串兜底。snippet **不用 FTS5 的 `snippet()`**(trigram 只高亮 3-char token,如 `《deriva》`)→ 用 `_clean_snippet` 从整页 body 找首处命中、折叠空白、±40 字上下文 + 返回 `pos` 供前端精确加粗。结果按 bm25 序分组到书(dict 插入序=最佳命中书在前),书内按页序。`mode=ro` 只读打开。cap 300 行 + `truncated` 标记。
- **前端** `templates/pdf_search.html`:暗色(匹配书库页 #1a1d24/#10162a),搜索即查(250ms 防抖 + AbortController + `_seq` 防竞态),结果按书分组(>4 本默认折叠、≤4 本展开),每条命中 `pos+qlen` 精确 `<mark>` 加粗(避 HTML 注入 + 大小写差异),点命中 → `/pdf/view?file=<rel>&page=<n>`。支持 `?q=` 预填。
- **入口**:书库页(`pdf_index.html`)筛选框旁加「🔎 全文搜索」按钮。nav 抽屉链接是用户自管的(默认仅 3 条),没硬塞;Cmd+K 是桌面思路对 iPad 触屏价值低,未做。
- **保鲜**:`scripts/quick_sync.py`(每 15min systemd timer)第 4 步跑 `build_search_index.py` 增量 → 新书/改动 15 分钟内入索引,无变动近零成本。

### 26. 日语阅读 3 连修：跨行词组下划线 / 西文词内空格 / 词组标掌握（2026-06-04，`_CHAR_CACHE_VER`→9）
用户报三个连带 bug，根因各不同：

- **① 跨行词组上半段没下划线**（如收藏词组「公表する」跨行，只下半行有线）。根因:`_build_jp_vocab_marks` 按 `w` 分组 token 时，**遇到中间的 sp char 就 `break` 终止分组** → 换行处的空格把同一个 `w` 的词组切成两段，各自查词（上半段查不到 vocab 笔记 → 不画）。修:分组 `while` 条件只判 `chars[j].get("w")==wid`，**sp char 跳过不计入 surface 但不终止分组**（`_merge_favorite_phrases` 给词组内部含跨行空格的 char 也设了同 `w`）。rects 循环本就按行高分段 → 多行各画一条。`reader.src/08-charlayer.js::renderVocabUnderlines` 早已逐 rect 画，无需改前端。

- **② 选「Web」变成「W eb」+ 只出多词工具条、无「已掌握」**。根因:PyMuPDF rawdict 对**字距拉开(tracking)的西文词**会在词内插一个**合成空格 glyph**；PyMuPDF `words` 把 W/e/b 归同一个 `w`（所以选中按 `w` 扩展会带上那个空格 → surface「W eb」），含空格 → 前端单词正则 `/^[A-Za-z]+$/` 不通过 → 落到词组工具条、查不出单词、无「已掌握」。修:`_compute_page_chars` 抽完字符、分词后加 `_stitch_latin_words(chars)` —— 删掉「同行前后非空格 char 都是单个 ASCII 字母且二者 `w` 相同且 ≥0」的内部空格（用 PyMuPDF 自己的词分组当判据，**词间空格**前后 `w` 不同→保留，零误删）。删后「Web」连续 → 单词工具条 + 已掌握正常。须 bump `_CHAR_CACHE_VER`（8→9）废旧缓存。覆盖 U+0020/00A0/2009/200A/202F/2008/2006 等空格。

- **③ 收藏词组「标记掌握」无效（且会建幽灵 vocab 笔记）**。根因:词组 popover 的「标记掌握」原走 `/api/vocab-mark`，用词组 surface 当 lemma → 建出 `资源/vocab/w/web browser.md` 幽灵笔记，跟下划线（按组成词/合并 token 的 `w`）脱节。修:**独立词组掌握 store** `state/pdf-phrase-mark.json`（归一化键=折叠空白+小写）+ 路由 `/pdf/api/phrase-mark`（GET 列表 / POST `{text,mark}`）。`_merge_favorite_phrases` 现合并 `收藏∪已掌握` 词组，已掌握的给 char 打 `favm=1`；`_build_vocab_marks`/`_build_jp_vocab_marks` 见 `favm` 即跳过下划线。前端 `15-phrase-wordpop.js`:`showPhrasePopover` 初始态读 `_phraseMarkSet`、`_wordPopMaster` 对 `phrase` 走 phrase-mark（标完 `refreshCharsWForAllPages` 重画→线消失）。另:`/api/vocab-mark` 加守卫，含空格的「词」直接 400（防别处再建幽灵笔记，词组必须走 phrase-mark）。
- 验证:`_stitch_latin_words` 5 单测 + 6 真实页残留词内空格=0；phrase-mark/vocab-mark 守卫 test_client 端到端通过。

### 27. PWA 冷启动恢复上次页面（2026-06-04，`_server_deploy/app.py`）
需求:把 PWA 装到主屏后,每次打开都进固定的 `start_url=/dashboard/`(复习仪表盘),而非上次在读的书/页。
- **做法（不改 manifest → 已装的 PWA 免重装）**:`_PWA_HEAD` 里加一段 `<head>` 内联脚本(`_PWA_RESUME_JS`),`after_request` 全站注入(同 manifest 那套,登录态 + `NAV_INJECT_PREFIXES`):
  - **记录**:每页(除首页 `/dashboard`、`/login`、`/logout`、`/app`)把 `location.pathname+search` 存进 `localStorage['pwa:lastUrl']`。阅读器 `/pdf/view` 记录时**剥掉 `page`/`mode` 参数** → 让阅读器自己的「上次位置恢复」(§见 02-position.js,URL 不带 page 才恢复)接管,精确到滚动位置。
  - **跳转**:仅在 **standalone**(`navigator.standalone` 或 `display-mode: standalone`,即装到主屏)且**落在首页**且 **本会话首次**(`sessionStorage['pwa:launched']` 未置)时,`location.replace(lastUrl)`。`sessionStorage` 标记保证一次会话只跳一次(会话内再点回首页不被跳走;关掉 PWA 重开=新会话再跳)。Safari 普通浏览不受影响(非 standalone 不跳)。
- **为何放 `<head>` 内联而非 nav.js**:同步早于 body 渲染 → 无「先闪一下仪表盘再跳」;且 SW(`/pdf/sw.js`)只拦 `/pdf/api/page-image`、不缓存 HTML,故服务端注入的脚本恒新鲜。
- **安全**:跳转目标须以单 `/` 开头且非 `//`(挡协议相对开放重定向);非内容页不记录。
- 验证:node 模拟 8 场景(冷启动跳/同会话不跳/剥 page/login 不覆盖/Safari 不跳/无 lastUrl 不跳/挡 `//`/insights 记录)全过;test_client 确认脚本注入 `/dashboard` `/insights` `/pdf/view` 且在 `</head>` 前。

### 28. 单击查词「等待」改呼吸高亮（2026-06-04，`reader.src/15-phrase-wordpop.js`）
需求:慢词(日语 AI 查词 2-4s)单击后弹一个挡视线的「查词中…」框。改成跟「解释/翻译」一致——给词建呼吸高亮当等待指示,点高亮才出结果+高亮消失。
- **快/慢分流(关键)**:`showWordPopover` 不再立刻弹 loading 框。先后台 `_lookupWordFetch`,**300ms 计时器**:≤300ms 回(英语 ecdict / 已缓存日语)→ `_renderWordPop` 直接弹小框(全程无「查词中」框);>300ms 未回(日语 AI 等)→ `_showWordHighlight` 建**青绿呼吸高亮**(`.word-hl-layer`,区别于选区蓝/词组蓝/解释琥珀)当等待指示,清掉蓝选区。
- **就绪/点击**:fetch 回来 → 高亮 `ready=true` 呼吸转常亮(不自动弹);**点高亮** `_wordHlClick` → `_renderWordPop` 弹小框 + 移除高亮。查询中就点(`boxOpen`)→ 这才显示「查词中」小框,fetch 回来自动填。
- **照搬解释三件套**:`_activeWordHl` 状态 + `renderWordHl(pw)`(渲染循环 `08-charlayer.js` 调,重渲染/滚动后从状态恢复;`boxOpen` 后不再重画)+ `_charRangeToPtRects` 算 pt rects。`_positionWordPop(pop, cs)` 加可选 `cs` 参数(慢词点高亮时 `_charSel` 可能已变,用查词时捕获的 charSel 定位)。
- **重构**:原 `showWordPopover` 拆成 `_lookupWordFetch`(纯查) + `_renderWordPop`(纯渲染,含 `!ok→_expandWordFull`) + 编排器。`_wordPopSeq` 竞态守卫保留。
- 验证:node 模拟 5 场景(快词直接弹/慢词建高亮就绪不自动弹/点就绪弹框/查询中点开框后自动填/竞态 A 作废 B 正常)全过;build+check OK。**词组 `showPhrasePopover` 暂未改**(它已有呼吸高亮,只是仍显 loading 框,需要的话同法改)。
- **多个高亮并存（2026-06-04 改）**：单全局 `_activeWordHl` → 数组 `_wordHls`，可同时点多个生词、各自后台查、各自呼吸高亮、各自点开（边读边点的体验）。每次查词建独立 `hl`（`id=++_wordHlSeq`），`showWordPopover` 不再清别的高亮。`renderWordHl(pw)` 渲染该页**所有**匹配高亮(各一个 `.word-hl-layer`，`boxOpen` 的不画)；`_materializeWordHl` 同范围去重防重复点同词叠层；`_removeWordHl(hl)` 移除单个；`_removeWordHighlight()` 保留为"清全部"。**框是单例**：`_wordPopOwnerId` 记当前框归属的 hl id，并发查词回来只填仍归属自己的框(否则被后点的接管),查完无论填没填都清掉自己。node 模拟 8 断言(3 慢词并存/就绪不自动弹/逐个点开只移除对应/快词不扰慢词/并发抢框)全过。
- **拖选经过高亮被截获修复（2026-06-04）**：词查/词组/解释呼吸高亮的 `.hl` 是 `pointer-events:auto`、z6 盖在 char-layer 上。拖选起点在 char-layer，但**拖动经过某高亮 `.hl` 时被它截获** → 丢 move/up(选区乱涨成多词→误弹词组按钮呼吸)+ 误触发其 click(弹出别词结果)。原 onStart 只禁 `.vocab-layer`(L 按钮),漏了这三类。修:`13-selection.js` 加 `_OVL_HL_SEL`,onStart 禁这些 `.hl` 点击、onEnd/单页横滑放弃/touchcancel/竖滑转滚动 都恢复(顺手补了 vocab-layer 在 touchcancel 的同类遗漏)。deliberate 点高亮(onStart 不触发)不受影响、照常开结果。
- **已查过的词单击直接秒显(2026-06-04)**：用户诉求"已有现成数据的词单击应直接出结果,不要先高亮再点"。加本会话客户端缓存 `_dictCache`(word→dict-quick d):`showWordPopover` 命中缓存 → **直接 `_renderWordPop` 秒显小框,不发请求/不建高亮**,后台再打一次 `_lookupWordFetch` 刷新暴露计数+缓存;`_renderWordPop` 在 d.ok 时存缓存(>600 条淘汰最旧);`_wordPopMaster` 切掌握后同步缓存 `mastered`(再点不显旧态)。另把慢词高亮阈值 300→400ms(让首次但服务端已缓存的词更可能走直显)。

### 29. 全屏阅读（隐藏顶栏，2026-06-04）
顶栏 `#header`(固定 48px)在 iPad 上挺占竖向空间。加全屏阅读:
- 顶栏「⛶ 全屏」按钮 `toggleFullscreen()` → 给 `<html>` 加 `.fs-mode` class,CSS `.fs-mode #header{display:none}` 隐藏顶栏(`#main` flex:1 自动撑满)。**页宽不变 → 无需重渲染**(只多露竖向)。
- 隐藏后右上角浮出半透明圆形 `#fs-restore`(⤢,fixed top-right,`backdrop-filter` 毛玻璃,`.fs-mode #fs-restore{display:flex}`) → 点它退出全屏。
- **持久化**:`localStorage['pdf-fullscreen']`,`_applyFullscreen`/`_fsEnabled` 在 06-layout.js。**防刷新闪顶栏**:class 挂 `<html>`(不是 body),`<head>` 里一段**渲染前内联脚本**读 localStorage 先加 `.fs-mode`(早于 body 绘制),reader.js boot 再 `_applyFullscreen` 同步按钮激活态。
- 左侧 nav `☰` 抽屉手柄、右侧「语法·知识点」侧 tab 不受影响(都是 fixed 边缘元素,全屏下仍可用)。

### 30. 双指缩小不再「重新加载」+ 选择工具栏不溢出屏（2026-06-04）
两个反馈:
- **缩小时整页像重新加载**。两个叠加原因 + 各自修法:
  - ① 图片模式按 `scale` 算栅格宽 `reqW`,缩小→更小 `w` 的新 `img.src`→**网络重取、白屏一下**。修:`_ratchetReqW(page,w)`(`04-render.js`)按页记**最大用过的 w**,缩小不降 → 复用已缓存的更大图、浏览器降采样显示(清晰),不换 src 不重 fetch。放大超过当前栅格才取更高清。`_renderPageImg` + `_prefetchAround` 共用同一 ratchet(缓存键一致)。
  - ② `_applyZoom` 连续/双页缩放时 `setupContinuousMode()` 会 `container.innerHTML=''` 把整列塌成占位再重建(滚动位置丢→`scrollIntoView`→明显"重载")。修:新增 `_rescaleContinuousInPlace()`(`07-continuous.js`)**不清空容器**,只把各 `page-wrap` 按新 `scale` 调:已渲染页就地重渲(img 走 ratchet 缓存秒回、叠层按新 scale 重算坐标)、未渲染占位只改宽高;IO 不重建(元素还在,继续观察)。`_applyZoom` 优先用它,没建过列表才回退 `setupContinuousMode`。single 模式仍重渲单页(快)。
- **选区靠右/靠下时工具栏(`#sel-toolbar` max-width 480)跑出屏外被裁**(截图只剩边缘一排色板)。修:`_clampToolbarIntoView(mainEl, selTopY)`(`13-selection.js`)在 `open` 后量 `offsetW/H`,把工具栏夹进 `#main` 可见区(`scrollLeft..+clientW` × `scrollTop..+clientH`):右溢左移、底溢翻到选区上方。
- 验证:`_ratchetReqW` node 自测(只增不减 + 按页独立)通过;build+check OK。

### 31. 句末 L 按钮被去边裁成半截（2026-06-04）
反馈:开**去边**时句子框「⌟」(句末 L 按钮)不完整。根因:`.vocab-sentence-btn-l`/`-l-start` 用 `content-box`+padding，
让边框比字符边缘**外扩 ~6-8px**；去边把页面裁到正文区(`.crop-on overflow:hidden`)，外扩的右/下边框落进被裁的页边
margin → 被切掉,L 只剩一条边。修:① CSS 两个按钮 `content-box`→`border-box`(border 改画在框内缘);② `12-vocab-sentences.js`
几何**贴齐字符边缘**——句末按钮右外缘=末字右边缘(`lc[2]*sx`)、句首按钮左外缘=首字左边缘(`fc[0]*sx`),去掉 `-2` 外扩,
`top/left` 夹 `≥0`。字符本身一定在可见区内 → 边框画在其边缘内侧必可见,不再被裁(crop 与否都成立,无需算裁切坐标)。
- **修正(同日)**：§31 把边框**贴齐字符边缘**导致竖线压在字形上「看不见」(用户报"竖线又不见了")。改成正解:边框落在字**外侧 GAP=3px 间隙**(看得见、不压字形),**再夹回可见窗口**——去边时 `.crop-on>*` 给本层加 `translate(-crop-l,-crop-t)` + wrap `overflow:hidden`,可见窗口(样式坐标)=`[cropL, cropL+layer.clientWidth] × [cropT, cropT+clientH]`(非去边=`[0,全宽]`)。句首⌐ `left=max(visL, fc0*sx-3)`、句末⌟ `right=min(visR, lc2*sx+3)`/`bot=min(visB, lc3*sy+3)`,leftE 也夹 `≥visL`。普通字符→边框在间隙(可见);贴裁切边界的字符→夹回边界内(仍可见,不被裁)。node 自测 8 例(去边/非去边 × 普通/边缘)通过。
- **角标臂长固定(2026-06-04，三修)**：用户报"左上角句首角标不正常、右下角正常"。把 PDF 页图 + 句子数据真渲出来对比才定位:角标臂长 `Math.max(charW*6,48)` 对 **CJK 首字(34pt)×6=204pt** → ⌐ 顶横线像给「共通フレーム」加了条**上划线**;而句末「。」(13pt)×6=78pt 像正常小角标 → 首尾差 2.7 倍、单行句两框同行时尤其突兀。改成**固定臂长 `Math.max(charW, 44)`≈44px**(不再按字宽×6),首尾一致的干净小角标。**调试法**:`fitz` 渲页图 + PIL 按 JS 几何画 ⌐/⌟ + first_char bbox 蓝框,肉眼对比(`/tmp/Lfix_p*.png`)——比盯代码/截图猜可靠得多。
- **句末「⌟」竖线太短/点不动(2026-06-04)**：句末字常是小标点「。」(高仅 ~15pt 且低在行底)，竖线按它算→只有 15pt 高(像只剩横线)、点击区也只 15pt 高→点不中(句首是整字「共」41pt 所以正常)。修:`_lineRectOf(ch,rects)` 找末字**所在行 rect**(y 中心落其中且明显比字宽,排除退化小 rect)，竖线/点击区用**整行高**(~46pt)。渲染对比确认竖线 16.7pt→46.6pt。
- **句末「⌟」竖线仍没了 = `.vocab-layer{overflow:hidden}` × 去边 translate 的双重裁切(2026-06-04)**：句子框/L按钮用**整页坐标** `X`,但 `.vocab-layer` 自己 `overflow:hidden` 的裁切区是本地 `[0,裁后宽]`;去边时 `.crop-on>.vocab-layer` 带 `translate(-crop-l)`,左边裁掉一截→裁后宽 < 右侧文字的整页 X → **右侧内容(X>裁后宽)被本层错误裁掉**。句首「⌐」在左侧(X 小)完整,句末「⌟」在右侧(X 大)最右竖线正好被裁→只剩横线(完全吻合"句首正常句末没竖线")。修:**去掉 `.vocab-layer` 的 `overflow:hidden`**——裁切交给外层 `.crop-on{overflow:hidden}`(它带 translate、裁切区正确)+ JS visR 夹取。渲染验证:`fitz`+PIL 按 border-box 画 END 框,竖线在「。」「超」间隙清晰可见(`/tmp/Lend2.png`),证明几何对、问题在 CSS 裁切。

### 32. 2026-06-07 批次：缩放/模式原地化 + page-chars 拆分缓存 + 单页重扫 + 选词提速

- **缩放/模式切换全部原地化（不再"重新加载"）**：
  - 双指/`+−`/去边/侧栏挤压 → `_rescaleContinuousInPlace`（不整列重建）。双指松手 `_applyZoom` **先用现有位图按 `__pageWPt×scale` 瞬时 CSS 缩放**（布局即正确、无闪、不 snap），高清图后台**并发** decode-first 换入（不串行阻塞手势）。`_renderPageImg` 加 `__imgGen` 重入守卫（最后发起的赢）+ decode 失败不贴坏图 + 记 `__renderScale`。
  - **单/双页模式切换**（`toggleSpread`/旋转自动切）改 `_remodeListInPlace`（`07-continuous.js`）：把已渲染的 `.page-wrap` 节点 **reparent**（`appendChild` 移动 DOM 不重渲）重组行/列结构（连续=直挂；双页=`.spread-row` 裹 1-2 个）→ 不再清空容器重建。`readMode` 只有 `continuous`/`spread`（single 已废）。
- **page-chars 拆分（缓存命中即本地）**：`/api/page-chars` 只回不变的 `{chars,page_w,page_h,furigana}` → `Cache-Control: private,max-age=3600` **可缓存**；新 `/api/page-overlay` 回会变的 `{vocab_marks,vocab_sentences,offset,cv}` → `no-store`。
  - `cv` = `_page_content_version` = md5(`_CHAR_CACHE_VER` + PDF mtime + 偏移 + 单页重扫覆盖签名 + 收藏词组 mtime + **已掌握词组 `_PHRASE_MARK_PATH` mtime**)。**任一改 chars 的因素都必须进 cv**（复审查出最初漏 `_PHRASE_MARK_PATH` → 标记词组掌握后 `favm` 陈旧，已补）。
  - `/pdf/sw.js` 现也缓存 `/pdf/api/page-chars`（cache-first by URL，URL 带 `&cv` → 不会陈旧）。读过的书：图（早缓存）+ 字 全本地、秒开/离线，没看过的新页才碰 Pi。
- **选词浮层提速（修"浮层很久才可用"）**：`loadCharsAndBindLayer` 不再 overlay→chars 串行。改 **chars 优先**（用 localStorage `pdf-cv:<file>:<page>` 猜测 → 命中 SW 缓存秒回）→ 立即建 char 层（`_bindCharLayer`，**选词此刻可用**）；overlay **并行**、到了再渲生词 + 校正 cv（猜错 → 后台用真 cv 重取刷新 `__charBoxes` 兜底）。改内容的操作（重扫/偏移/收藏词组）主动写 localStorage cv → 重渲即新。实测 page-chars 4ms vs overlay 17ms（后者跑分词/生词）。
- **文字层校准 + 单页重扫**（⚙ 设置「🔧 文字层校准」，仅扫描/OCR 书）：① 可视化文字框（复用 `?dbg=1` 红框，开关 localStorage `pdf-charbox`）；② 手动微调偏移（方向键 nudge，per-page sidecar `state/pdf-char-offset.json`，`_apply_char_offset` live 应用，`/api/char-offset` GET/POST 返 cv）；③ **单页重扫**（Google Vision）`/api/reocr-page`：渲该页（长边封顶 4000px + JPEG q90 避开 Vision 40MB 上限）→ `google_vision_ocr.ocr_one_page`（已补词/块 id `w`/`bk`）→ Vision px ×(pt/px) 转 PDF 点（**rotation=0；实测与 rawdict 同坐标系、无需 y 翻转**：同页第一字都「版」@y74、末字@y786/788）→ 存覆盖 sidecar `state/pdf-page-ocr/<sha>-p<page>.json`，`_page_chars_cached` 顶部优先读它；`/api/reocr-page/clear` 撤销。实测料理师4（原零文字层）p5 重扫 396 字 2.9s 可读。
- **查词等待先标注音**（用户要"等查词时除高亮闪烁，先把这处注音标了，英日都一样"）：慢词建呼吸高亮时，`_furiHitsForRects` 按词 rects 匹配本页 furigana（日读音/英音标，后端 `_compute_page_chars` **始终计算**、不受 ruby 开关 gate），用**抽出共用的 `_makeRubySpan`**（与 `renderRubyLayer` 同款字号/位置/`.ruby-layer .rt` 样式）在词上方先标读音 → 跟开「あ」看到的振假名长一模一样。

### 33. 整本预热子系统（2026-06-08，commit 22f6b8a）

**动机**：图片模式（§22）按需取页，**第一次翻到某页**才渲背景图 + 算字符层/振假名，慢网/弱机有等待。预热 = 开书后（或手动点）在后台把**全书所有页**一次渲好进磁盘缓存 → 之后翻页/查词/振假名全部命中缓存秒开。**本地实例（强 CPU）尤其值得**——全本一次备齐、零等待。

**链路**：

| 层 | 文件 | 关键点 |
|---|---|---|
| 端点（启动） | `pdf_reader.py::pdf_api_prewarm_async`（`/api/prewarm-async` POST `{file,width}`，`pdf_reader.py:768`）| detached 子进程跑 `scripts/prewarm_pdf.py`；**已在跑则不重复启**（读状态文件 `state/pdf-prewarm/<sha>-w<w>.json` 的 `pid` + `_pid_alive`）；**低优先级**：Linux `nice -n 19` / Windows `BELOW_NORMAL_PRIORITY_CLASS|CREATE_NO_WINDOW`（不抢翻页/查词/返回选书页的交互 CPU），Linux 还 `start_new_session=True` detach；width clamp `[400,3000]` |
| 端点（进度） | `pdf_reader.py::pdf_api_prewarm_status`（`/api/prewarm-status?file=&width=`，`pdf_reader.py:811`）| `percent = 已缓存页图张数 / 总页数`：数 `state/pdf-page-img/<sha>-p*-w<w>-<mtime>.jpg` 的文件数 / `fitz` 页数；附 `running`（pid 还活着否） |
| 一把梭脚本 | `scripts/prewarm_pdf.py` | 串行跑两步：① `prewarm_pdf_pages.py`（按 width 渲全部页图）② `prewarm_pdf_chars.py`（算全部页字符 + 振假名，width-independent 只跑一次）。`--workers` 默认 `cpu-1` |
| 页图脚本 | `scripts/prewarm_pdf_pages.py` | `ProcessPoolExecutor` 并发 `fitz get_pixmap(matrix=w/page_width).tobytes('jpg', q78)` → atomic 写到 `state/pdf-page-img/<sha1(resolve(abs))[:16]>-p<page>-w<w>-<mtime>.jpg`。**缓存键与 `/api/page-image` 完全一致**（复刻 `_book_sha` = `sha1(resolve 后绝对路径)[:16]` + `int(mtime)`），已存的 `cached` 跳过 |
| 字符脚本 | `scripts/prewarm_pdf_chars.py` | 并发调阅读器同款 `pdf_reader._page_chars_cached(ap, rel, page)`（缓存键含 mtime），把 chars + fugashi 振假名算进 `state/pdf-char-cache`。需 `CLAUDE_PROJECT`/`OBSIDIAN_VAULT` env + fugashi/unidic-lite |
| 选书页按钮 | `templates/pdf_index.html::prewarmFromList`（`pdf_index.html:230`）| 每本「📥 预热」按钮：先 `/api/book-meta` 取 `page_w`（**渲染基准 = 页原生点宽**，clamp `[400,2400]`，与显示解耦）→ POST `/api/prewarm-async` → **复用智能压缩那条进度条**（`.prep-prog/.prep-bar/.prep-msg`），2s 轮询 `/api/prewarm-status` 到 100% |
| 阅读器前端 | `reader.src/22-prewarm.js` | `window._prewarmBook(manual)` + 开书自动 `window._maybeAutoPrewarm`（`04-render.js:138` 首页渲完触发一次） |

**前端 `22-prewarm.js` 要点**（`reader.src/22-prewarm.js`）：
- 宽度 `_prewarmWidth()` = `clamp(__imgMeta.page_w, 400, 2400)`（页**原生点宽**，与窗口/缩放级别无关）——**和 `_renderPageImg` 的 `reqW` 同基准**（`04-render.js:82`：`max(_natW, cw*dpr)` 也以原生宽 `_natW=__imgMeta.page_w` 为底）→ 预热宽度=阅读器实际请求宽度，不会错配「换窗口就没命中缓存」。2026-06-10 起服务端另有宽度容差回退（§35①）双保险：即便宽度错配（高 dpr 设备 `cw*dpr > _natW`），预热渲的图也能即时回用 + 后台补渲精确宽。
- 仅图片模式（`window._imgMode && __imgMeta && scale` 才跑）。手动入口 `_prewarmBook(true)`；自动入口 `_maybeAutoPrewarm()`：`localStorage['pdf-auto-prewarm']==='0'` 关，**默认开**，首页渲完延迟 1.5s 触发，且 `_autoPrewarmDone` 只触发一次。
- 自动模式 `percent >= 95` 直接跳过（已基本预热好不重启）；`running` 时只 `_prewarmTrack` 跟进度不重启。
- `_prewarmTrack(w)` 2.5s 轮询，把进度写进顶栏「📥 N%」按钮，100%（或 `!running && percent>0`）停轮询、`_toast` 提示「整本预热完成 ✓ 翻页秒开」。

### 34. 三个根因修复 + 日语下划线密度成因（2026-06-08，commit 22f6b8a，`READER_BUILD`→`reader-fix-9`）

> **构建提醒**：`reader.js` = 纯 `cat reader.src/*.js`（NN- 前缀保序、无分隔），见 §1。这三处改的都是 `reader.src/NN-*.js`，重建/校验走 `bash scripts/check_pdf_reader_js.sh`。build tag 在 `reader.src/07-continuous.js` 的 `READER_BUILD` 常量（dlog 打点用，每次改前端 bump +1）：当前值以代码为准（本批次=fix-9 → 性能审计批=fix-10 → 点词三修=fix-11 → 去边叠层修=fix-14，见 §35/§36）。

**① 加载遮罩盖住标题 → 点不动「返回」**：
- 现象：`#pdf-loading`（`pdf_reader.html:602`，`position:fixed;inset:0;z-index:1500` **整页覆盖**）在加载期间盖住顶栏标题 → 想退回选书页点不动，"点了名半天没反应"。
- 修 A（遮罩自带出口）：遮罩内加**原生 `<a href="/pdf/">← 返回书架</a>`**（`pdf_reader.html:606`，`position:absolute;top:16px;left:16px`）——原生导航即刻生效、不依赖任何 JS/不等加载完。
- 修 B（标题返回有即时反馈）：`<h1 onclick="goPdfList()">`（`pdf_reader.html:587`）。`goPdfList`（`reader.src/03-loader.js:26`，module 作用域挂 `window` 供内联 onclick 调）**先同步把遮罩切到「← 返回书架…」再 paint，然后才 `location.href='/pdf/'`**——整页卸载前先给视觉反馈（重阅读器整页卸载要点时间）。坑：`goPdfList` 里要 `_pdfInitDone=false` 解锁，否则 `pdfLoadShow` 的「已过初次加载不再遮挡」守卫（`03-loader.js:6`）会拦住遮罩不显示。

**② 打开大书冻主线程 → 连原生 `<a>` 都点不动**（`reader.src/07-continuous.js`）：
- 根因：旧版 `setupContinuousMode` **同步 `for`** 给全书每页建占位 DOM（几千页）→ 冻主线程 2-5s，期间事件队列停摆，连原生 `<a>`/标题返回都点不动。
- 修：**分批建占位**（`CHUNK=80`，每批 `await new Promise(r=>setTimeout(r,0))` 让出事件循环，`07-continuous.js:50/65/76`）+ **边建边 observe**（IntersectionObserver 先建好，占位 push 进 `phs` 后 `phs.forEach(ph=>_contIO.observe(ph))`，把 O(N) 的 observe 也分摊，不再一次性 observe 几千个）+ **目标页占位一就位就 `pdfLoadHide()`**（`_afterTargetReady`，`07-continuous.js:38`：`scrollIntoView` + 渲染目标页 + 撤遮罩，不等全书占位都建完，用户已可读/可返回，余下占位继续后台分批建）。每批主线程只忙几 ms，加载中也点得动。

**③ 图片模式扫描书「越往下错位越严重」**（`reader.src/04-render.js::_renderPageImg`，`04-render.js:73`）：
- 根因 = **image-mode shim 用 page1 尺寸是陷阱**：图片模式 `pdfDoc` 是 shim（§22），`getPage` 对**所有页**都返回 page1 的 meta 尺寸 → `viewport.height` 初值 `ch=floor(meta.page_h×scale)` 是 page1 的高。但**扫描书每页高度不同**（料理师 part1 53 页高 2215~2487pt 各不同）→ 本页图被压/拉到 page1 高度显示，而 char 层按本页**真实**高度铺坐标 → 纵向比例不一致、误差随 y 累积（顶部不偏、底部最偏）。
- 修：`decode` 后改用**图自身真实宽高比**算显示高 `ch = round(cw × naturalHeight/naturalWidth)`（`04-render.js:101`）——扫描书宽统一（reqW 固定）→ 按图真实比例对齐准。**残留**：未渲染的占位仍按 page1 估高（`07-continuous.js` `estH`），滚动时微抖。

**④ 日语生词下划线密度成因（非 bug，是库攒大了）**：
- `_build_jp_vocab_marks`（`pdf_reader.py:1861`）按统一 `vocab_index`（`_vocab_idx()`，英日**同一库** `资源/vocab/`，见 §15「日英 vocab 完全统一」）判定：每个 fugashi `w` token 解析原形查库，**`label_slug` 存在且 `!= "mastered"` 的词全部画下划线**（mastered 不画）。
- 故密度直接由「库里非 mastered 词数」决定：库越攒越大（查过的词都入库、`new_source` 查词 mastery 重置 0），词密的日语页几乎整页被划。**这是机制如实工作，不是 bug**。密度治理（限量/开关/频率阈值）**待定**，目前无 UI 旋钮。

### 35. 2026-06-10 性能审计批次（commits 264d140 / 75ebafa / 27a7396，`READER_BUILD`→`reader-fix-10`）

> 同批次还有：点词 `lookup_jp` stale-while-revalidate + `/api/dict-jp-ai` 深入讲解服务端永久缓存（709a918）、ECDICT exchange 死扫描删除 + vocab-list mtime 签名缓存（75ebafa rank1-2）→ 归 [`vocab-system.md`](vocab-system.md)；spaCy 常驻 worker + grammar 双键缓存 + grammar-stream 回放缓存（f449521）→ 归 [`grammar-analysis-system.md`](grammar-analysis-system.md)。本节只记阅读器自己的。

**① page-image 宽度容差回退 + 后台补渲精确宽**（264d140，`pdf_reader.py::pdf_api_page_image`，:588）
- 根因：缓存键含**精确宽度** `-w<w>-`，每台设备/窗口/缩放请求的 `w` 都差一点 → 服务器明明有该页（预热过/别的设备渲过）却 miss、现场重渲 ~1-2s（实测 応用情報 被存了 ~85 种宽度、料理师 13 种；只有 SW 看过的页才秒开）。
- 修：精确 miss 时 glob 同页其它宽度（`{sha}-p{page}-w*-{mt}.jpg`）：**≥请求宽 → 回最小那张**（缩小显示零质量损失，`immutable` 长缓存）；**≥70% 请求宽 → 先回它即时显示**（短缓存 `max-age=3600`）+ `_spawn_exact_render`（:570）后台线程补渲精确宽（`_BG_RENDERS` set 去重在途，下次请求命中精确图）；都没有才同步渲。回退响应带 `X-PageImg-Fallback: <实际宽>` 头可辨。渲染逻辑抽成 `_render_page_jpg(ap,page,w,cf)`（:544，tmp→replace 原子写）供同步路径与后台补渲共用。实测原 ~1-2s 重渲路径 → 9ms。
- 客户端 `<img>` 是显式 CSS 定宽 → 固有宽度不同显示也正确。与 §33 预热协同：预热渲的图从此对**任意**设备/窗口宽都即时有效。

**② overlay 句子预翻译改 SWR**（75ebafa，`_build_unmastered_sentences` 尾部 + `_bg_translate_sentences`，`pdf_reader.py:2386`）
- 此前 miss 句**逐句同步翻**（Google ~300ms/句，抖动时退化为每句数秒 AI CLI）→ 首访页 page-overlay 阻塞 1-3s+ 且可耗尽 worker 线程池。改：**只取 `_cache_get` 已缓存译文即回**；miss 句交 `_bg_translate_sentences` 后台线程 `gtranslate_batch` 一次批量补翻进 tr-cache（`_BG_TR_INFLIGHT` set 对多页并发去重、失败静默下次再试、**绝不调 AI CLI**），下次 overlay/vocab-marks 重取自然带上。首访功能不丢——前端句子 L 按钮本就有按需翻译路径（`/api/translate-sentence`）。

**③ page-vocab-marks 统一到 page-overlay 同管道**（75ebafa，`pdf_reader.py::pdf_api_page_vocab_marks`，:2570）
- 此前该路由内联 `fitz.open`+rawdict+分词**全重算**（查词后前端刷 3-4 轮 × 多页，每页几百 ms 纯浪费）+ `vocab_index.index(force_reload=True)` 全库重读（~150ms）。改成与 `/api/page-overlay` 完全同管道：`_page_chars_cached`（磁盘缓存秒回）+ `_apply_char_offset`（顺带修「校准过偏移的页查词后下划线错位」）+ `_merge_favorite_phrases` + OCR override 一致；vocab_index 普通 mtime 扫描足以感知刚写盘的笔记（前端刷新延迟本就 ≥1.5s）。响应与 overlay 逐字段一致（实测 diff=0）；两路由差异只剩 vocab-marks 多回 `page_w/page_h`、overlay 多回 `offset/cv`。

**④ page-translate（译页）兜底降级**（75ebafa，`pdf_reader.py::pdf_api_page_translate`，:2501）
- 第 3 步逐句兜底此前走 auto 链（含 AI）：Google 故障窗 60 句 × [8s 超时 + AI 数秒] → 单请求挂 4-7 分钟还烧几十次 AI 额度。改：① `_tr(text, backend="no_ai")` —— `scripts/vocab/translate.py` 新增 backend（源链 `gtranslate→deepl→mymemory`，**绝不落 AI CLI**，都挂留空）；② **10s 墙钟预算**（`_deadline = monotonic()+10`）超时即止，未译句 `zh` 留空原样返回（响应带 `translated/total`，前端对空 zh 优雅跳过）。故障窗从分钟级挂死 → 秒级部分返回。

**⑤ 前端五项**（27a7396，全在 `reader.src/`，build tag bump `reader-fix-9`→`reader-fix-10`）：
- **查词后全页下划线刷新：trailing-coalesce + 视口优先 + 3-worker 并发池**（`12-vocab-sentences.js::refreshVocabUnderlinesForAllPages`，:34）。原串行逐页 fetch 全部已渲染页；现运行中再触发只置 `_vocabRerun`、跑完补一轮（查词后 1.8s/3.5s/1.5s 多轮是**故意错峰**等服务端 vocab note 写盘，直接 skip 会停在旧态，所以用 coalesce 不用 skip）+ 可视页排前 + 3 并发小池（无界 `Promise.all` 会打满单 worker 8 gthread，饿死滚动渲染请求）。
- **`refreshCharsWForAllPages` 代际守卫 + 池**（`15-phrase-wordpop.js`，`_charsWGen` :218）。快速收藏→立即取消两次调用交错时，旧轮迟到响应不写回（连 localStorage cv 也不写，防旧值覆盖新）；距视口中心近的页优先 + 3-worker 池（页内 overlay→chars 的 cv 依赖仍串行）。配套：词组「标记掌握」**乐观去线**（先 `_dropVocabUnderlineOptimistic` 再发请求，失败按快照回滚——只回滚仍是本次乐观结果的页，防覆盖期间其他刷新写入的新 marks）。
- **滚动 tick 读写分离 + 二分**（`07-continuous.js::_onContinuousScroll`，:229）。中线页定位由全书 O(N) rect 扫描改**二分**（wraps 文档序=页序、top 竖向单调非降；spread 同行两页同 top → 回退同 top 组首再按 DOM 序取第一个 `bottom>=center`；center 落页间隙不更新页码，语义与原扫描一致）；`_unloadFarPages(wraps)`（:211）复用调用方已取的 wraps + 改「先一轮只读 rect 收集待卸载页、再统一写」，消除逐页 读→写→读 强制 reflow。千页书每 tick 5-15ms → <1ms。
- **char-layer document 单 dispatcher**（`13-selection.js`，模块顶层 `document.addEventListener` 块）。document 级 mousemove/mouseup 原在 `_bindCharLayer` 内注册且从不移除 → 每次重渲/缩放重绑泄漏 +2 监听，且旧闭包捕获过期 cl（缩放后 rect 失真致选区错位）。改模块顶层注册一次，经 `pw.__charDrag`（`_bindCharLayer` 尾部每次重绑覆盖为最新 cl 的 `{onStart,onMove,onEnd,ptToLocal}`）分发，天然路由到最新绑定。
- **语法追问改 `_aiStream`**（`18-grammar.js::_grammarFollowup`，:365）。原手写 SSE 循环每 chunk 全文重渲+MathJax（零节流卡主线程、断连丢结果）→ 改用现成 `_aiStream`（`21-misc-ai.js`，80ms 节流 + 非 SSE JSON 兜底 + rid 断连轮询，同 `17-highlight` 的 `_followupAsk`）。语法系统其余改动（spaCy 常驻、缓存键策略）见 [`grammar-analysis-system.md`](grammar-analysis-system.md)。

### 36. 点词链路三连修 + 去边叠层覆盖根因（2026-06-10 下午，`reader-fix-11`→`fix-14`，commits ded74db/e93fdfb）

用户报「红圈处有文字浮层但点击不选中」。前两轮修复(③④)是真实改进但都没打中，最终靠 **Playwright 无头浏览器在 Pi 上真实点击 + `elementFromPoint` 逐像素查证**才确诊(⑤)——教训:命中类 bug 先查"点上是什么元素"，再查算法。

- **① 慢词结果自动弹出**（`15-phrase-wordpop.js`，`_wordPopCancelSeq`）。点词后若期间没滚动(#main scroll/wheel)也没点别的词(每次 `showWordPopover` 自增 seq)，慢词(>400ms)结果一到自动 `_renderWordPop`，不用再点常亮高亮；有动作则保持旧行为(位置已不可信)。
- **② 工具栏闪烁消除**（`13-selection.js` 单击查词分支）。单击选词会同步开 toolbar，30ms 后 `showWordPopover` 才关 → 浏览器画一帧(慢词时=「弹框闪一下消失」)。改为判定要弹词典时**同一事件 tick 内 `toolbar.classList.remove('open')`** → 根本不被 paint。
- **③ `_findCharStrict` 振假名带容差**（第三段兜底）。振假名画在字行上方 ~0.5 行高(pointer-events:none)，点假名/行缝/行尾余白时 y 不落任何 bbox 行内 → 原直接 MISS 当点空白清选区。加竖直 ≤0.7×行高、水平 ≤1.2×行高的兜底(dy 权重×2 归属近行)。
- **④ 防御两件**：`_syncCharBoxScale`（charBoxes 像素坐标是加载瞬间 scale 烘焙的，交互入口按实时 `clientWidth/pageWPt` 重标定——boxes 自带 `_x0` pt 字段，O(N) 重算）；`ptToLocal` 改 **BCR 与 layout 尺寸比值**换算（旧实现只除 `pw.style.zoom`，祖先 zoom/transform 漏算）。
- **⑤ 真凶：crop-on 叠层只有裁后尺寸**（`04-render.js::_applyCropToWrap` + 模板 CSS）。去边模式 wrap 收成裁后宽高、子层 `translate(-cropL,-cropT)`；`inset:0` 的叠层(char-layer/vocab/ruby/hl 等)只有**裁后**尺寸 → 可见页面**右侧 cropL、底部 cropT 条带没有任何点击层**(实测 55/69px)，点击落到 `<img>` 上;振假名层 overflow 照画 → 「有浮层但点不中」。`sel-overlay`/`ink-layer` 因 `_renderPageImg` 内联整宽幸免(也因此此前难定位)。修:`_applyCropToWrap` 写 `--full-w/--full-h`(位图 css 尺寸)，CSS 对 `.crop-on` 下 8 个叠层(`char-layer/vocab-layer/ruby-layer/hl-layer/word-hl-layer/phrase-hl-layer/explain-hl-layer/char-dbg-layer`)改 `right:auto;bottom:auto;width/height:var(--full-w/h)`。新增叠层时记得加进这组选择器。
- **复现方法论**（可复用）：Pi 上 `pip install playwright` + chromium headless；用 webapp SECRET_KEY 铸 session cookie；走 nginx HTTPS(直连 gunicorn :5000 时 /static 不在 Flask 侧会 404 模块)；`page.route` 拦截 reader.js 可换任意 commit 的 bundle 做二分；注意 crop translate——瞄准要用 `charBox + transform 矩阵 e/f`。改模板后 **gunicorn 不自动重载 Jinja 缓存，必须 restart webapp**。

### 37. 链路工程化四件套（2026-06-10 晚，`reader-fix-17`→`fix-18`，commit 7980c9c）

针对「文字层↔浮层↔服务器↔PWA」这条链路反复踩的工程坑，固化四件基础设施：

- **① E2E 冒烟 + 一键部署**。`scripts/reader_e2e.py`（Playwright，Pi 本机 ~40s）：铸 session cookie → 开応用情報 p37 → charBoxes 就绪 → 视觉坐标点右缘「議」（历史死区，含 crop transform `e/f`）→ `elementFromPoint` 必须是 char-layer → 选中=議事 → 词典 ≤12s 弹出 → 书架浮层开/有书/关；页面 JS 错误零容忍（FILE_REL 历史问题白名单）。`scripts/deploy_reader.sh [--no-e2e] [--pc]`：拼 bundle → `node --check` → 部署副本尾部追加 `window.__READER_GIT='<hash>+<clean|dirty>·<时间>'`（**仓库 reader.js 保持纯 cat，戳只打在部署副本**）→ cp 模板+pdf_reader.py → py_compile → restart webapp → /login 200 → E2E。治三个反复犯的错：忘拼 bundle / 改模板忘 restart / 部署完不验证。**改阅读器一律走这个脚本部署**。
- **② 连接质量小圆点**。`/pdf/api/ping`（no-store）+ `24-connquality.js`：30s + 回前台时量 RTT，header 尾部圆点 🟢<120ms 直连 / 🟡<450ms 中继/弱网 / 🔴断，点击 toast 显示毫秒数与归因。背景：iPad Tailscale 掉中继时整站慢数秒、应用零线索，用户只能怀疑代码。纯归因用，不做降级。
- **③ 叠层工厂 `ensurePageLayer(pw, cls)`**（`01-boot.js`）。所有页级叠层（char/vocab/ruby/hl/char-dbg…8 处创建点）统一经它建，自动附加 `.page-layer` 标记类；crop CSS 在原 8 个显式类名外加 `.crop-on>.page-layer` 兜底 → **新叠层不会再漏掉去边补偿**（§36⑤「有浮层点不中」的根因再也不会因加新层复发）。注意两处**故意不用**工厂：`15-phrase-wordpop` 词典框内的 ruby-layer（不是页级层）；`17-highlight` 保留 `pw.appendChild` 重排到末尾（stacking 语义）。配 `window._auditLayers()` console 自检：逐 wrap 比对各叠层尺寸 vs `<img>`、charBoxes 烘焙 scale vs 实时 scale（>1% 警告，交互时 `_syncCharBoxScale` 会自愈）。
- **④ 缓存命中计数**。`_CACHE_STATS` 进程内计数器（gunicorn 单 worker，重启清零）+ `GET /pdf/api/cache-stats`。键：`page_img.hit/fallback_ge/fallback_lt/render_sync`、`page_chars.override/hit/compute`、`sent_tr.hit/miss`、`grammar.full_hit/sp_hit/spacy_run/ai_run`、`dict_jp.cache/ai`。用法：用户报「慢」→ 先看比值——hit 占比异常低 = 缓存键漂移/预热缺口（如 §35① 的 85 种宽度问题，当时只能靠 ls 缓存目录数文件）；比值正常 = 看 ② 的圆点怪网络。

### 38. 单页改用统一 .page-wrap 结构(2026-06-14,`reader-fix-26`,commit 9dc958c)

**反复的「单页缩放变多页 / 适应后回弹」根因 = 两套 DOM 结构并存**:连续/双页把每页渲进 `.page-wrap` 子元素(`setupContinuousMode` 建占位 + IO 懒渲染),而**单页直接渲进 `#page-container`**(`renderPage`→`_renderPageInto(page-container)`)。两套结构来回拆建,衍生一整类 bug:① 切模式/缩放时残留的多页 `.page-wrap` 没清干净 → 单页显示多页;② `page-container` 上的 CSS `zoom`/`transform` 残留没被清 → 适应回弹;③ `page-container.dataset.loaded` 在 `_renderPageInto` 被 decode 竞态/scale 漂移提前 return 时不复位 → 旧结构留存。fix-19~25 都在给这套打补丁(清 transform、清 zoom、标对 loaded、强力兜底),治标不治本。

**修法(PDF.js 官方 viewer 思路:所有模式共用同一套 DOM,单页=只有一页的连续模式)**:
- `_singleWrap()`(04-render):单页也渲进**唯一一个 `.page-wrap`**(残留多页/双页结构则清空重建),`renderPage` 单页 → 渲进该 wrap,翻页只换 `data-page-num` 重渲。
- `_applyZoom` / `_refitToWidth` / `_runFitOverflowGuard` / `zoomChange`(06-layout、05-nav)三模式**统一**:`if (!await _rescaleContinuousInPlace()) { single→renderPage(currentPage); else→setupContinuousMode() }`。单页的唯一 wrap 走跟连续一模一样的 `_rescaleContinuousInPlace`(CSS-zoom 瞬时缩放 + 后台重栅格化,zoom 落在 **wrap** 而非 page-container → 无 page-container 残留 → 无回弹)。删掉所有「单页直渲 page-container」特殊分支。
- 单页↔连续切换安全:`_remodeListInPlace` 已有 `wraps.length !== pdfDoc.numPages → return false` 守卫,单页只有 1 wrap ≠ 总页数 → 自动整列重建。
- **教训**:跨设备渲染/手势 bug,headless 复现不出来时,别一层层打补丁;先看大厂(PDF.js / Mozilla viewer)怎么用**统一数据结构**消除特殊分支。补丁堆多了本身就是新 bug 源。

### 39. 侧边栏 Copilot 助手:选中上下文 + 权威查词 + 额度护栏(2026-06-16,`reader-fix-48`,commit d4f52d3)

**侧栏 Copilot = 带工具的对话 agent**(右侧抽屉「助手」tab)。后端 `_server_deploy/assistant.py`(部署 `/home/bwicarus/webapp/assistant.py`——**不在 deploy_reader.sh 清单里,改它要单独 `cp`+`py_compile`+`systemctl restart webapp`**),前端 `reader.src/25-assistant.js`。大脑 = 预热常驻的 `claude --print --input-format stream-json --output-format stream-json --include-partial-messages --model sonnet --effort high`;**自管工具循环**(agent 吐一行 `{"tool","args"}` JSON → 服务端执行 Python → 喂回 `【工具结果】` → 循环到 final answer),不上 MCP。工具:`read_page / read_selection / search_book / translate / make_anki / make_note / add_vocab / goto_page / lookup_word / see_page / page_vocab / highlight / undo_last`。SSE 事件:`tool / tool-done / answer(流式) / task / undo / notice / actions / error`。前端 `ctx()` = `window.__voiceContext()`(05-nav.js),POST `/api/assistant/chat`。对话历史服务端持久化(跨设备)`state/assistant-convo/<uid>.json`,每轮带所在书/页/选中句标注,让助手懂「刚才那页」。

**跨页续读(2026-06-20,prompt 补强)**:用户问得模糊、且书中相关内容跨页时,助手原来只看本页就答(本页讲到一半/答不全)。能力其实早有(`read_page` 支持 `{page:N}` 读任意印刷页,`_sys_prompt` 的 `meta` 已给 AI『当前可见页』+『共N页』,能算相邻页)——缺的是**没教它什么时候该翻页**。修法纯 prompt:`_sys_prompt` 加「★【跨页续读】」规则——read_page 拿到本页后若**不足以完整回答**(被截断/主题没讲完/答不全,尤其用户问得宽泛),**主动 read_page 相邻页**(『当前可见页』±1,别超『共』N 页,通常补下一页),拼起来再答;够答即止(补 1 页、最多再补 1)。无需改任何工具代码。

本次 3 项加固(用户「除了 gemini 其他全做」中剩余 3 项,均不碰 Gemini):

- **🟢 选中失效校验**(防跨页陈旧选中误当成「现在在问的」):阅读器自绘 char-layer 选中存模块级 `lastSelText`(原生 `getSelection()` 在 OCR/漫画书常为空),翻页/开助手都不清它 → 旧选中会跨页漏给助手。修:`_updateSelPreview`(14-textlayer-legacy.js)每次选中变化打 `window.__lastSelMeta={page,t}`(清空→null);`__voiceContext` 只认「**当前页 + 10min 内**」的 char-layer 选中,否则回退**实时**原生 `getSelection()`;无 meta 一律当陈旧丢弃。
- **🟠 选中带「所在句」**(免每次都 read_page 才有上下文):`_selByCharRange`(13-selection.js)用现成 `_expandSentenceFromRange`+`_charsRangeToText` 算选中所在句存 `window.__lastSelSentence`(整句==选中则不存,免重复);`__voiceContext` 带出 `selection_sentence`;`assistant._sys_prompt` 把它拼进【当前页面】末尾,选中规则改成「**有所在句就别再 read_page**,不足才补读」。`sel`/句过 `_clean_tag`(折叠空白 + 剥 `【】「」`)防 prompt 注入。
- **🟢 额度护栏**(用户明确**不用 Gemini 降级** → 只提醒不切后端):`assistant.py` 后台 150s 周期查实时额度(`scripts/lib/claude_quota`,零 token + 缓存,**非阻塞**:`_quota_loop` 守在快照里,`_agent_run` 只读快照),近上限(5h≥85/95 或 7d-sonnet≥90/97 两档)→ `_agent_run` 头部发 `notice` 事件;前端渲成 `.asst-note` 居中小黄条(不覆盖回答)。**绝不降级到别的后端**,助手照常用 Claude。

**验证**:`_sys_prompt` 句显示/抑制/注入剥离;额度阈值 5 档 + 过期快照(>1800s 作废);staleness 6 场景 Node 矩阵;真机 e2e(`test_client` + 强制告警)SSE 顺序 `notice→answer(多段流式)→done`,真 claude 正常作答 + 新 `selection_sentence` 字段不报错。

**踩坑**:① `_sys_prompt` 自检别拿静态 prompt 里也出现的词(「用户当前选中」「选中所在句」)当断言依据——它们在固定指令文本里也有;用动态专属串(`用户当前选中:「` 带冒号引号、`(已给好的上下文`)。② 额度查询若内联进 `/chat` 同步跑会给首条消息加最多几秒延迟(`fetch_quota` 网络调用);改成后台线程刷快照、请求端只读 → 零延迟。③ 额度护栏查询本身**零 token**(走 OAuth usage 端点),不烧配额。

### 39b. AI 回答公式不渲染:两个叠加根因(2026-06-18,`reader-fix-76`,commit 6726db8)

**现象**:助手/解释/翻译等 AI 回答里的数学不显示成公式,而是裸 `$...$` 文本或乱码上标。**纵深修复(两端都补)**:
- **根因① 模型侧**:`assistant.py` `_sys_prompt`(`能回答用户时` 段后)原**没要求数学用 LaTeX、没禁反引号** → 在「用简洁中文口语聊天」语气下模型常把公式写成纯文本 / 反引号 `` `x^2` `` / Unicode 上标(x²)→ MathJax 无从渲染。加硬规则:数学一律 `$...$`/`$$..$$`;**禁反引号包数学**(会被渲成 `<code>`,而 MathJax 配置 `skipHtmlTags` 含 `code` → 跳过)、**禁 Unicode 上下标**。
- **根因② 渲染侧**:`md()`(`21-misc-ai.js`,全局函数,被 25-assistant `renderMd` / dict / grammar / draft 共用)把整串丢给 `marked.parse`,**marked 会把 `$P(A_1)P(A_2)$` 里的 `_` 当斜体、`*` 当强调、`\` 当转义拆坏** → 即便模型写对了 `$...$` 也渲染失败。改为**占位符法**:先把 `$$..$$`/`\[..\]`/`$..$`/`\(..\)` 整段抠成 `@@MJX{n}@@`(纯字母数字,marked 原样保留),marked 跑完再换回原公式交给 MathJax。行内 `$..$` 正则用 `\$(?!\s)(?:\\\$|[^$\n])+?\$`($ 后须非空白以避开「$ 5」、`\$` 转义豁免)。
- **要点**:此修复对**历史回答 retroactive**——任何过去含 `$...$` 但被 marked 拆坏的答案,重载历史即正确渲染。单测验证:数学内 `_` 受保护、数学外真 markdown `a_i_b` 仍正常变斜体。

### 39c. 切后台 Load failed:全站网络韧性(2026-06-18,`reader-fix-82/83`,commit f9120ef/25e9f26)

**现象**:iOS 切后台/锁屏会**掐死进行中的请求**,回前台后该 fetch reject `TypeError: Load failed`,直接显示成报错。**两层兜底**(关键边界:`fetch()` 只在「还没收到响应」时 reject;一旦返回 Response 就交还调用方,**流式 body 读到一半断了不归 fetch 层管**):
- **底层(连接没建成就被掐)`00-resilient-fetch.js`**:最先加载,包 `window.fetch`——**GET/HEAD(幂等读)瞬断 → 等回到前台 + 退避后自动重试(≤3 次)**;**POST(写)maxRetry=0 不自动重试**(防重复提交);**AbortError(主动取消)不重试**。另暴露 `window.__safeFetch(url,opts,{retries})` 给「幂等但用 POST 的计算类」(grammar-analyze / translate-sentence 已改用)。覆盖「点一下就切后台→回来 Load failed」的绝大多数场景,且对 PDF.js 的 range GET 也自动生效。
- **流式 body 中途断**(fetch 已返回 Response,reader.read() 才抛):**各功能自恢复**——助手 `/chat`(25-assistant `send()`):`visibilitychange` 看门狗(回前台 3s 无新进度→主动 abort 死流)+ `_recoverFromHistory()` 拉 `/api/assistant/history`(服务端早落了用户消息、`finally` 落了助手回答,完整或到断点),`_lastProgressTs` 区分「流还活着的慢回答」vs「僵死」;`_aiStream`(翻译/解释/语法)早有 `rid` + `/pdf/api/ai-stream-result` 轮询兜底。
- **约定**:以后**新的「需要等待」任务**——读类直接用 fetch(自动韧性);幂等计算 POST 用 `__safeFetch`;有持久副作用的写 / 流式,走「提交→job id→轮询」或「服务端落库→失败后拉回」,别裸 `await fetch` 然后 `catch 显示 e.message`。

### 40. 扫描书插图徽标:图区 📷 徽标 + AI bbox 像素收紧(2026-06-17,`reader-fix-50`,commit d38b762)

**图徽标系统**(`reader.src/26-figures.js` + `pdf_reader.py` 的 `_fig_*`):扫描书插图无文字层,靠 AI(`describe_figures.py`,sonnet)**懒描述**——每页首次进视口 `GET /pdf/api/page-figures?file=&page=` 触发后台描述本页+预取后 2 页,结果写 sidecar `state/pdf-figures/<book-sha>.json`(`<book-sha>=_book_sha(abspath)`,跟 describe_figures 互通);每图 `{page,caption,bbox(归一),desc,fbox,badge}`。前端在图的右上外侧画一枚磨砂玻璃 📷 圆徽标(`.fig-badge`,`renderFiguresOnPage` 由 08-charlayer 建完字符层后调,模式无关),点开 `.fig-pop` 浮层看 caption+desc(Markdown+MathJax)。徽标锚点 `badge=[bx,by]` 服务端按像素算好持久化 → **跨加载位置一致**。

**徽标几何(纯几何,用户算法)** `_fig_badge_from_block`:A = 以图中心为中心、四向各撞到最近文字框/页边/邻图围出的无文字矩形;B = 从 A 右上顶点沿 A 对角线(共线)缩到撞图为止的最大空白方块;徽标 = B 左下角再朝右上微退 0.022 落到图外。文字框来自 `page.get_text("words")`(精确,不碰像素)。

**本次根因 — AI bbox 偏大致徽标偏**(用户:「红色区域包含了图以外的文字层」):Claude 给的 `bbox` 常上含正文、下含图题,徽标几何按错框算 → 位置飘。修法 = 新增 `_fig_refine_bbox(page,bbox)` 把 AI bbox **收紧成真实图框 fbox**:渲染该区灰度图(PyMuPDF `get_pixmap` clip+csGRAY)→ 二值化取墨迹(`<205`)→ 用 `get_text("words")` 精确框把文字层抹黑(PIL `ImageDraw.rectangle`,外扩 1.5pt)→ `ImageFilter.MedianFilter(3)` 去椒盐噪(关键:**保留分子小圆/点簇**,扫描书图常是稀疏圆点)→ `getbbox()` 求剩余墨迹外接框 = 图本身;归一化夹在原 bbox 内,退化(几乎全文字/空白)回退原 bbox。`_fig_badge_anchor` = refine(图+邻图)→ `_fig_badge_from_block`;路由懒算先存 fbox 再用 fbox 算 badge,前端回退启发优先用 fbox。验证:74 图(费曼/応用情報/料理1-2)逐图核验徽标落右上外侧 + fbox 只含图不含字。

**顺修潜伏 bug**:`/api/page-figures` 懒算分支调 `fitz.open` 却没 `import fitz`(本模块所有 fitz 都函数内局部 import,无模块级全局),一直 `NameError` 被 `except` 吞 → **懒算徽标从未生效**(此前徽标只靠离线重算脚本才有,这也是早先「打开怎么没徽标」的根因)。补 `import fitz`。

**踩坑**:① 像素收紧靠灰度阈值,对线稿/漫画/点阵/表格/数学图都行;彩色照片若大面积亮色只会被收到「暗墨迹」处——本项目几本书插图都是线稿/漫画,未遇到,真彩照书需另判。② MedianFilter 一开始以为会吃掉稀疏圆点,实测 3×3 中值对成簇圆点(每点≥若干像素)无碍,只杀孤立单像素椒盐;真正会误删的是「单像素噪声」不是「圆点」。③ 别信 AI bbox 当图框,但也别完全弃用——refine 退化时回退它兜底。

### 41. 坏文字层「选区 OCR 校正」:on-demand 视觉转写 + 持久化注入字符层(2026-06-19,`reader-fix-89`→`selpage-92`,commits c41d5af 一带)

**问题**:有些 PDF(如 Z-Library 抓的费曼)**文字层 ToUnicode 是坏的**——渲染图视觉正确,但可选中/可复制的文字错。典型:上标 `10⁻⁸` 在文字层里被编码成字面 `10-6`/`10-8`(指数都错)、`Å` 无 ToUnicode 映射被 PyMuPDF 直接丢弃、`10` 中间被插空格成 `1 0`。PyMuPDF 忠实抽取 = 垃圾,选中/复制/翻译/查词全错。**唯一可靠源是渲染图**(图是对的)。公式 OCR 没救到,是因为这种是夹在中文里的行内数学,没被检测成独立公式框。

**方案**(用户拍板:选中工具栏加「🔎 OCR」按钮,选区位置+截图发 Claude 视觉,且**永久生效**):
- **后端 `_claude_ocr_crop(png,model,effort)`**(`pdf_reader.py`):裁图发 `claude` CLI `--input-format stream-json` 视觉(同 `scripts/formula_ocr_claude.py::ask_vision`),禁所有工具,prompt 要求精确逐字转写、数学进 `$...$`、特殊符号照写(Å)、**忽略图像左右边缘被裁一半的不完整字符**(防把"径"尾认成"金")、绝不臆造。`_claude_bin()` 稳健解析 claude 路径(env `APP_CLAUDE`>which>常见位)。
- **路由 `POST /pdf/api/ocr-selection`** `{file,page,bbox(PDF pt),model?,effort?}`:按 bbox 裁图(`padx=1.0` 极小横向留白防吃邻字 / `pady=(h*0.12)+2` 纵向防切上标 + 防圈进相邻行;长边目标 ~1500px 算 scale)→ `_figure_crop_png` 渲 → `_claude_ocr_crop` 识别 → 去代码围栏(用模块级 `re` **不是** `_re`!`_re` 只在个别函数内局部 import,路由里用 `_re` 会 NameError 500)→ **持久化** `_ocrfix_add` + 返回新 `cv`。
- **校正 sidecar** `state/pdf-ocr-fix/<book-sha>.json` = `{pdf,book_mtime,fixes:[{page,bbox(归一),text,ts}]}`,书 mtime 变则清空(坐标可能失效)。`_ocrfix_add` 同页**重叠过半**的旧校正覆盖(重 OCR 同一处=替换不堆叠,可纠正上次 OCR 错)。
- **注入 `_apply_ocr_corrections(chars,furigana,rel,page,pw,ph)`**(同 `_apply_formula_chars` 那套,在 `pdf_api_page_chars` 公式注入后调):删 bbox 内原坏字符+框内振假名 → 把校正文字平铺满框宽塞入(`WID=960000000`,标 `ocrfix=1`)。**词 id 按 token 分**(`_ocr_token_ids`:`$...$`/`$$` 数学整块一个、连续 ASCII 词一个、中日文/标点各自一个)→ `w=wbase+token号` → 校正文字能**分开点/选**(否则整块 65 字一起选,用户嫌);`bk` 仍同块(整段预览/翻译不受影响)。
- **cv** `_page_content_version` 并入 `o{_OCRFIX_INJECT_VER}:{sidecar mtime}` → 存校正后 cv 变 → 前端缓存自动失效,跨设备下次打开也是校正后的。
- **前端 `onOcrSel`**(`reader.src/21-misc-ai.js`):算选区并集 bbox(`c._x0.._y1` 是 PDF 点)→ POST → 成功后回填 `lastSelText`+预览(badge「OCR ✓ 已写入」)+ 更新本页 `localStorage pdf-cv:` + `_rerenderLoadedPages()` 立即重渲注入字符层。之后重选/复制/翻译/查词永久用正确文字。改注入逻辑(token 分词)bump `_OCRFIX_INJECT_VER` → 已存校正自动按新规则重注入,用户刷新即生效不必重 OCR。

**两个真踩坑(用户实测揪出)**:
1. **路由 `_re` 未定义 500**:`_re` 不是模块级别名(`re` 才是,line 17),路由里 `_re.sub` → NameError → 点 OCR 一直失败、预览回退坏文字。教训:**带鉴权的新路由必须 test_client 走完整 HTTP 链路验证**(`set -a; . webapp/.env`,`session_transaction` 设 `user_id=1`),只测内部函数会漏掉路由级 bug。
2. **发错页号**(用户「没有变化」):`onOcrSel` 原用 `page=currentPage`,但**连续滚动下视口居中页 ≠ 选中页**。在 p24 选、currentPage 是 23 → 后端拿 p23 裁 p24 坐标 → OCR 到别处乱码 → 还存到 p23 → 重选 p24 永远没校正。修:`_selPageNum()` = `_charSel.pw.dataset.pageNum||currentPage`。**同类隐患一并修**:`onTranslate`(译文浮层贴页)/`onToNote`(笔记深链)/`dictStream`+`_lookupWordFetch`(查词日志页)全改用 `_selPageNum()`。

**注入位置对齐原字形 = LCS 锚点对齐**(2026-06-19,`_OCRFIX_INJECT_VER`=7,`_ocr_align_positions`):注入文字怎么定位经历几版,最终用**用户的思路**:对比 OCR 结果跟原字符层,**相同字符当对照锚点,不同的(数学/Å/LaTeX 标记)放锚点中间插值**。
- 核心洞察:坏文字层只是 ToUnicode 把字符『值』错了,bbox『位置』是对的(PDF 渲染就靠它);而且**坏的只有上标和 Å,中文/cm/数字大多是对的**——这些『相同字』就是天然锚点,且在原层里位置精确。
- 实现:删原坏字符前先捕获其 bbox → `_ocr_align_positions`:**LCS**(最长公共子序列,O(N·M))求校正文字 txt 跟原字形串的公共字 → 锚点用原字形精确 x/y;锚点之间的字按 index 线性插值;两端外推到 bbox 边。
- **走过的弯路(都不如锚点)**:① 均匀平铺满 bbox → LaTeX 源码(`$1\times10^{-8}$` 16 字)远长于视觉(~6 字形),数学段被撑过宽。② 累积视觉权重比例映射 → 原字形数(42)≠ 校正视觉字数(34),系统性漂移,越往后(如句尾「现在称为」)偏越多。③ 贪心匹配锚点会漏(重复字乱配),LCS 才取到最优锚点集。
- **两个隐藏坑**:(a) 原字形排序不能按 `(round(y0/4),x0)`——上标 `⁻⁸` 的 y0 抬高被分到别的 y 桶,基线字一组、上标一组 → 整体 x 不单调、定位全乱;改**按行聚类**(y 中心差 >7pt 才换行,上标 <7 算同行)+ 行内按 x。(b) 改注入逻辑必须 bump `_OCRFIX_INJECT_VER` 否则 cv 不变、旧缓存不刷。
- 验证:费曼 p24「现在称为」注入 x(129/290/300/310/320/430)跟原字形真实 x(132/289/300/311/321/431)逐字差 ≤2pt,选「现在」即得「现在」。

**边界**:① 被丢的 `Å` 这类符号要被收进校正,拖选范围须**带过它的位置**(选到后面字),否则裁图不含它。② OCR 仍可能错(如边缘吃字"金为");靠"你看过结果才存"+ 重 OCR 覆盖 + ⚙ 切 opus 兜底,不焊死。③ 对齐靠 LCS 锚点;锚点多(同字多)就准,锚点稀疏的纯数学/纯符号区段靠插值、略近似;多行选区跨行 x 会重置,对齐退化——单行选最干净。④ 复制/翻译用途(整段文字)永远对,不受位置近似影响。

### 42. 中文书选中两字粘一起:日语分词器误用到中文(2026-06-19,`_CHAR_CACHE_VER`=10)

**问题**(用户:中文书选中有时只能两个字一起选):`_compute_page_chars` 的分词 `_apply_jp_tokenize`(fugashi/MeCab + unidic-lite,**日语**词典)调用处只判「含 CJK」、**没按书语言 gate** → 中文书的中文也被日语分词。很多中文双字词(粒子/原子/半径/现在)在 unidic 里也是日语词 → fugashi 归成一个 token → 单击/拖选按词边界只能两字一起选;不一致是因为取决于「这俩汉字在日语词典里算不算词」。费曼这本压根没设 langs。

**修法**:按书语言 gate 分词。`_page_chars_cached` 算 `is_ja = "ja" in _book_langs_for(rel)` 传进 `_compute_page_chars(abs_path,page,is_ja)`:
- `is_ja`(日语书,langs 含 ja):走 `_apply_jp_tokenize`(fugashi 分词 + 振假名,日语选词/查词需要)。
- 否则(中文 / 未设语言书):走 `_apply_cjk_singleton` —— 每个 CJK 字一个**独立** word id → 单击选一字、拖选任意范围、双击仍选段;非 CJK(英文词)保持 `_word_id` 成组。
- 缓存:`is_ja` 进磁盘缓存键(`{sha}-p{page}-{mtime}-{ja|zh}.json`)+ `_CHAR_CACHE_VER` 9→10 + cv 并入 `_BOOK_LANGS_PATH` mtime(改书语言→cv 变→前端重取)。安全阀(tagger 挂跳过缓存)只对 is_ja 生效。

**验证**:费曼(中文,无 langs)1095 汉字 0 个多字共享 w 组(全独立);応用情報(ja)827 字 218 组(重要/共通/フレーム,fugashi 正常)。**注**:中文按字独立是有意为之(用户母语,主要诉求是自由选任意片段喂 AI);若以后想要中文按词分(粒子=一词),可装 jieba 再加一条 `_apply_zh_tokenize` 分支(当前未装 jieba)。

### 43. 笔迹焦点:用笔圈/划/箭头标注 → 自动把"笔迹区域合成图+附近文字"喂给助手(2026-06-19,`reader-inkfocus-93`)

**问题**(用户:用笔圈了词问"这是什么",助手没理会、还答成别的):手写墨迹只画在 canvas 上,助手上下文里**没有"用户圈了什么"**;而且服务端 ink sidecar 常是空的(autosave 没触发),`see_page` 也看不到。
**两层修法**:
1. **前端把当前页内存墨迹随上下文发**:`__voiceContext` 加 `ink: window._ink.byPage[currentPage]`(实时,不依赖服务端保存时机)。
2. **`see_ink` 工具,由编排器(sonnet·low)判断要不要看**(用户二次优化:无条件每条都附图费 token 又慢,且问题可能跟笔迹无关 → 交给快速编排器路由,不是无脑附):
   - `pdf_reader._ink_focus_image(rel,page,strokes)`:裁笔迹外接框 + 留白(带上下文行/邻词)+ `_figure_crop_png(with_ink=True)` 叠笔迹 → PNG;>3MB 自动降档。
   - `assistant._t_see_ink`(注册进 TOOLS):读 `ctx["ink"]` → `_ink_focus_image` → 返回 `_vision`(复用看图回喂机制)+ note(附 `_text_under_ink` 几何标注文字当参考)。
   - **sys prompt 把判断权交给编排器**:本页有笔迹时提示『**你来判断**:跟标注有关(这是什么/我圈的/这里/指代不清)**或** 没说具体但有笔迹 → 先 `see_ink`;明显无关(下一页/总结章/翻译/查词)→ 别看图,直接答更快』。
   - `_text_under_ink(rel,page,strokes=)` 几何提取(圈=框内、扁笔画=线上方一行)当便宜 hint,纯涂画就只靠 see_ink 合成图。
   - ⚠ **撤掉了一开始的"无条件自动附图"版**(每条都附 → 费/慢/可能跟问题无关)。
**坑/边界**:① ink 坐标是归一化 0-1(跟 sidecar 一致),`_figure_crop_png`/`_text_under_ink` 都按归一化 ×页宽高转 PDF pt。② 有笔迹的每条提问都附图(小图~17KB,token 可控),满页涂画时焦点框≈整页(退化成 see_page,可接受)。③ `_ink_focus_image` >3MB 自动降档。

### 44. 按需召回 recall_notes:编排器把当前内容跟"用户已记的笔记"串起来(2026-06-19)

**思路**(用户类比 Claude Code skills/references 渐进式加载):助手的工具循环本就是"router + 按需拉"的模式;补一个**召回用户自己知识**的工具,从"只看当前页"升级到"结合他的知识库"。
- `assistant._t_recall_notes(args{query})`:**纯本地查、不调 AI**。① 先扫知识索引 `index/*.md`(`- [[名]] \`关键词\` — 摘要` 格式,带摘要最有价值)按 query 词重叠打分;② 命中 <3 条 → `grep -rIl -F` vault markdown 笔记全文兜底(C 实现快)。返回 top6 {note,subject,keywords,summary,src}。
- 注册进 TOOLS + sys prompt 路由规则:『想把当前内容跟用户已学过/已记过串起来,或用户问"我之前记过吗/我笔记里有没有X"→ recall_notes;召回到就点出"你在《X》笔记里记过…",没召回到就按通用知识答别硬扯』。
- **延迟实测**:知识索引命中 ~0–1ms,grep 兜底 ~200ms(扫 1902 篇),无 AI 调用 → 真正成本只是"多一个 agentic 回合"(几秒,跟 read_page/see_ink 同量级),且只在编排器判断需要时才花。
**设计要点**:① 召回必须**本地查不碰 AI**(否则多一次模型调用才会显著加时);② 知识索引是 curated 摘要层(最有价值),vault 全文 grep 当兜底;③ 没命中如实返回"没相关笔记"让 AI 别硬扯。**架构注记**:这是"工具=能力注册表 + 编排器按需调"模式的延伸(同 Claude Code 渐进式加载);当前 ~20 工具全列进 prompt 没问题,涨到 ~30+ 再考虑 deferred/可搜索工具注册表(Claude Code ToolSearch 那套)。

**多源扩展(2026-06-19,用户:KG/Anki 也接进来)**:`recall_notes` 现合并 4 个**高精度**来源,全本地查:
1. 知识索引 `index/*.md`(curated 摘要,最优);2. **KG 图谱节点——只召回真学过的**(`mastered` / 有 `containing_notes` / `mastery>0`;⚠ 用户强调"节点存在≠学过":book 结构自动生成大量 locked 节点,绝不能当"已学",否则 AI 误判用户掌握);3. **Anki 卡**(grep `anki/records/*.json` → 匹配 front/back/tags,用户亲手做的=真学过);4. ~~raw vault 全文 grep~~ **已砍**:短/拉丁词子串会匹配 base64 附件、`represent⊃present` 等噪声,把没学的笔记误当"学过"——违背"只算真学过的"原则。
- sys prompt + tool desc 强调:**只有召回到的才算他学过**,没召回到别假设、别硬扯。
- 实测:向量空间→7索引+1图谱;泰勒级数→索引+2 Anki卡;`present past`→0(EGIU 那个节点没 mastered 故正确排除);全 ~17ms 无 AI。

**实测踩坑(2026-06-19,端到端验证)**:① 编排器会把短语 query 传成**去虚词的复合词**(『向量空间的定义』→`向量空间定义`),整串子串匹配不到索引(索引是『向量空间』+『定义』分开)→ 命中 0。修:匹配信号 = 实词(权重2)+ **CJK 二元组 bigram**(权重1,`向量/量空/空间/间定/定义`),真词条命中多个 bigram 自然排前;阈值『score≥2 或 含≥3字实词』滤掉单 bigram 噪声。② Anki grep 预筛主键不能用整复合词(卡里拆开的),改 `grep -rIlE 实词|bigram` 宽召回再 `_score` 精筛。③ 验证:`向量空间定义`→8(6索引+2图谱)、`链式法则`→索引+2Anki卡、`present past`→0、端到端 AI 真引用 [[000-向量空间]]/[[F^s]]/图谱已掌握节点。`_send_stream` 吐的是**累积文本**(前端替换显示),别在测试里 += 累积(会平方级重复,虚惊)。

### 45. 手写时下方文字被选中:墨迹(pointerdown)与选字(touchstart/mousedown)是不同类事件(2026-06-19,`reader-inkselfix-94`)

**问题**(用户:手写笔画字时下方文字也被选中):墨迹绘制绑在 `wrap` 的 **`pointerdown`**(`_inkPointerDown`,模板内联脚本),笔落下时它 `stopPropagation` 想挡选字;但字符层选字绑的是 **`cl.mousedown` / `cl.touchstart`**(`13-selection.js`)——**不同类事件**,pointerdown 的 stopPropagation 拦不住它,且 Apple Pencil 在 iOS 上会同时派发 touchstart → 照样 `onStart` 选字。
**修**:字符层 touchstart/mousedown + onStart 加墨迹门控:① touchstart:`e.touches[0].touchType === 'stylus'`(Apple Pencil)→ 直接 return,不选字(直接在 touchstart 层识别铁笔,不依赖事件顺序);② mousedown + touchstart + onStart 都加 `if (window._ink && (_ink.mode || _ink.drawing)) return`(桌面手写模式 / 正在画)。墨迹层照常画(wrap pointerdown 不动),只挡字符层选中。
**注**:`_ink` 全局在模板内联脚本(`window._ink`),手写整模块(inkToggle/_inkPointerDown/_inkRedraw…)都在模板 `<script>` 里、不在 reader.js;reader.src 只能 `window._ink && ...` 防御性引用。

### 46. 应用 Claude Design 的设计回写:mfx 动效+美化层(2026-06-20)

**来源**:把 reader 组件库(`scripts/build_design_kit.py` 导出的 15 个组件)上传到 claude.ai/design,用户在那做了设计,**导出回写**到线上。
**改动性质**:Claude Design 给**每个组件全局注入了同一套 mfx 层**(所以 15 个"全部"都变;去 token 化后正文跟原始一致=**没改配色**,纯加层)。
**应用方式**(可一键撤):抽成 `_server_deploy/static/pdf/mfx.css`(14.5KB)+ `mfx.js`(7.4KB),`pdf_reader.html` + `pdf_index.html` 各 `<link>`+`<script defer>` 引入(→ `/var/www/html/static/pdf/`,nginx 服务)。**撤销 = 删模板里那两行**。
**mfx 6 个 CSS 层 + 3 个 JS**:`mfx-tokens`(--c-* 颜色 token,值=原色)/ `motion-fx`(入场 pop·级联 rise·按压 scale·悬浮抬,全在 `prefers-reduced-motion`)/ `polish-fx`(focus-visible 焦点环·弹层深影·加载 shimmer·弹层方向入场)/ `polish-fx2`(弹层毛玻璃+噪点·选中发光边·tabular-nums·text-wrap balance/pretty·Toast)/ `gestures-fx`(JS:生词左滑删除·KG hover 预览·模态下拉关)/ `effects-fx`(JS:成功爆点·按钮流光·语法卡呼吸·空状态极光·模态光标聚光)。JS 全 idempotent(`window.__mfx*` 守卫)。
**生词左滑删除已接真后端**(2026-06-20):mfx.js 提交分支读 `.vi-word` lemma,`/[぀-ヿ㐀-鿿]/` 判日文 → `jp-vocab-mark`/`vocab-mark`,`{word,mark:'known'}` 锁 mastery 100%(下划线消失、刷新不回来)+ `refreshVocabUnderlinesForAllPages` 即时刷;非破坏可逆(字典框「✓已掌握」取消)。
**stream-fx 层(第 7 层,2026-06-20)**:Design 更新了 `ai-assistant-panel.html`,加了助手流式输出动效。设计里的 stream-fx **是静态文本上 fake 的 demo 循环,不能直接搬**(真助手 `25-assistant.js` 每 delta 用 `renderMd` 整段重渲 innerHTML)。落地法:**只把 CSS 加进 mfx.css** + **把真 SSE 三个接点接上**:① 占位 `<span class=asst-tool>思考中…</span>` → 思考三点 `<span class=mfx-typing>`;② `answer` 事件 → `aMsg.classList.add('mfx-streaming')`(气泡提亮)+ 逐字浮现 + 末尾闪烁光标;③ 收尾 → 去 `mfx-streaming` + `_fadeInAfter`(追问 chip/「!」反馈条错峰淡入)。注:空回答 fallback 条件要把 `mfx-typing` 也算进去(原来只判 `asst-tool`,否则只有思考点时不触发"(没拿到回答)")。
**逐字浮现接整段重渲(Design 第二轮,2026-06-20)**:Design 把逐字浮现从 JS 定时器逐个 `.on` 改成 **CSS 动画驱动**(`@keyframes mfx-char` blur(4px)→清晰,走合成器,不受 JS 节流)——冲着"整段重渲冲突"来的。`_streamWrap(el, revN)`:每个 `answer` delta 重渲后把正文按 `字/词` 切片包成 `.mfx-w`,下标 < revN 的打 `.mfx-shown`(即时不重播),≥ revN 的默认隐藏等揭示。前缀稳定性已验证(累加 delta 下 token 单调增、前缀不变 → prefix-skip 对齐成立)。
**⚠ 第一版踩坑"段一段"→ 揭示游标解耦**:第一版用"每 delta 把新尾巴 `animation-delay` 错峰扫入" → 揭示节奏被 **SSE delta 到达节奏**绑死(后端 `answer` 事件大块大块到),每块快速扫完就**等下一块** = 用户看到"段一段"。根治:**揭示游标 `_revN` 跟到达解耦**,由 `requestAnimationFrame` 稳定速度推进(`_revealTick`):`rate=0.05*(1+backlog/40)` 字/ms(**落后越多揭示越快,追上自然放慢**),每帧上限 6 字、`dt` clamp 120ms(防切后台回来一次灌完)。游标推进时给 `_spans[_revN]` 加 `.mfx-reveal`(淡入)+ 移光标到 frontier。这就是"打字机+缓冲":文字到得快就排队,揭示始终连续逐字。`mfx-streaming` 下三态:默认 `opacity:0`(等揭示)/ `.mfx-shown` 即时 / `.mfx-reveal` 淡入。保护:`>5000` 字 `_noChar` 停游标走普通;收尾 `_stopReveal()` + `renderMd(...,true)` 重渲成干净 markdown+MathJax(无 span/光标);reduced-motion 下隐藏规则不生效=文字随流式正常出现。
**抽取法**:DesignSync `get_file` 大文件**持久化到 `tool-results/*.txt`**(只 2KB 预览进上下文)→ 脚本在磁盘上 parse JSON `content` + diff/抽块,避免 15×70KB 撑爆上下文。

### 47. 手写双击「临时橡皮」:空闲自动回笔 + FAB 常驻指示(2026-06-20,模板内联 `_ink`)
手指双击画布切橡皮(替代浏览器拿不到的 Apple Pencil 双击笔身),原来是**永久切换**、无自动回笔、且工具栏隐藏时(纯 Pencil 用户 `_ink.mode=false`)看不到当前工具。改成「临时快速擦除」:
- **双击进的橡皮 = 临时**(`_ink.quickErase=true`),**空闲自动回笔**:`_inkArmRevert(ms)` 定时器,**没擦过给 2500ms**(进橡皮即武装,让你把笔移到目标)、**擦完抬笔给 900ms**(`_inkPointerUp` eraser 分支重新武装,每次抬笔重置)。落笔擦时 `_inkPointerDown` `clearTimeout` 暂停计时,**只在抬笔空闲时回笔、绝不中途打断**(`_ink.drawing.eraser` 时定时器到点也再顺延 400ms)。回笔还原**进橡皮前那支工具**(`_prevTool`,非死板回 pen)。
- **工具栏手动点橡皮 = 长期**(用户选的):`inkSetTool` 里清 `quickErase`+定时器,不自动回(适合一次擦很多)。两种意图分开。
- **下方指示**:`_inkUpdateToolUI()` 统一更新——工具栏按钮 `.on` + **FAB 图标 ✏️/🧹(持久,工具栏隐藏也看得到)** + 临时橡皮时 FAB 加 `.ink-erasing`(琥珀脉冲环)。自动回笔时 `window.mfxToast('✏️ 已回到笔')` 明确提示,**防"悄悄回笔后又画上线"**。
- 状态:`_ink.{quickErase,_revertT,_prevTool}`。改的都在 `pdf_reader.html` 内联脚本(`/view` 设 `no-store`,模板即时生效,**不用 bump reader.js**;校验法=抽该 `<script>` 块去 Jinja `node --check`)。

### 48. 书架「最近打开置顶」(MRU 排序,2026-06-20,`pdf_reader.py`)
原 `_list_vault_pdfs` 按文件 `mtime` 倒序。改成**最近打开过的在最上**:`/view` 打开书时 `_lastopen_touch(rel_clean)` 戳时间戳,排序 key 改 `(-lastopen, -mtime)`(用过的按打开时间近→远,没打开过的退回文件时间)。**按用户存**(`state/pdf-lastopen/<user>.json`,user=`session.username`,跟 `_prefs_path` 一套口径,原子写 `.tmp`→`replace`)。书架是**服务端渲染**(`pdf_index` 把 `pdfs` 传模板 `{% for p in pdfs %}`,23-bookshelf.js 不再排序),所以纯后端改 + 模板副标题文案。`/view` 的 `rel_clean`(规范化相对路径)跟 `_list_vault_pdfs` 的 key 一致才对得上。
