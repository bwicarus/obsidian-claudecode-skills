# PDF 阅读器完整参考

**地址**：`https://bwicarus.space/pdf/` （需登录；客户端通过 device-link 拿 session）

**目标**：在浏览器里读 vault 里的 PDF + 多种 AI 交互（翻译 / 解释 / 问 AI / 加笔记） + iOS 风格高亮编辑（带备注、色板、左滑删除）+ AI 选段草稿系统 + 知识树关联节点查询。

不依赖客户端 EXE 或 Obsidian。所有数据持久化在服务器：高亮 → `state/pdf-highlights/<sha1>.json`；草稿 → 浏览器 localStorage（per-device）。

---

## 1. 文件清单

后端：
- `_server_deploy/pdf_reader.py` — Flask Blueprint `bp = Blueprint("pdf_reader", __name__, url_prefix="/pdf")`，所有路由
- `_server_deploy/app.py` — 入口注册 `register_pdf_reader(app)`，`/pdf` 加入 `PROTECTED_PREFIXES`
- `data/ecdict.db` — ECDICT 离线英汉字典 ~850MB（`stardict` 表 + `exchange` 屈折表）

前端（单文件 ~2200 行）：
- `_server_deploy/templates/pdf_reader.html` — 主页（PDF.js + textLayer + char-layer + selToolbar + sidebar + result-modal + draft-modal + hl-popover）

静态资源：
- `/static/pdfjs/pdf.mjs` + `pdf.worker.mjs` — PDF.js v4.7.76
- `/static/pdfjs/cmaps/` + `standard_fonts/` — CJK + 字体回落

服务端部署位置（VPS / Pi）：
```
/root/claude/_server_deploy/pdf_reader.py        → /root/webapp/pdf_reader.py
/root/claude/_server_deploy/templates/pdf_reader.html → /root/webapp/templates/pdf_reader.html
/root/claude/data/ecdict.db                       → /root/webapp/data/ecdict.db
```
改完 `cp` 到 webapp 目录 + `systemctl restart webapp`。

---

## 2. 后端路由完整清单

所有路由前缀 `/pdf`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | PDF 列表（vault 下所有 *.pdf；按 mtime 倒序） |
| GET | `/view?file=<rel>&page=N` | 阅读器主页（鉴权后渲染 template） |
| GET | `/file/<vault_rel_path>` | 返回 PDF 二进制 `application/pdf` |
| GET | `/api/list-pdfs` | JSON 列表 |
| GET | `/api/page-chars?file=<rel>&page=N` | PyMuPDF 提取该页所有字符 bbox + 内容（驱动 char-layer） |
| GET | `/api/page-nodes?file=<rel>&page=N` | 该页对应的 KG 节点 |
| GET | `/api/dict?word=X` | ECDICT 离线英汉查询 |
| POST | `/api/translate` body:`{text, target_lang}` | AI 翻译 |
| POST | `/api/explain` body:`{text, context?}` | AI 解释（SSE 流式 或 JSON） |
| POST | `/api/to-note` body:`{text, name, file?, page?}` | 把选中内容 → vault 笔记 |
| POST | `/api/upload` body:`{file, dest}` | 上传 PDF 到 vault |
| POST | `/api/snippets-to` body:`{snippets, kind, note_name?}` | 草稿 → 笔记 / Anki / 两者 |
| GET | `/api/highlights?file=<rel>` | 列出该 PDF 的所有高亮 |
| POST | `/api/highlights` body:`{file,page,rects,color,text,kind?,sentence?,body?,note?,page_w?,page_h?}` | 新增高亮 |
| PATCH | `/api/highlights` body:`{file,id,color?,note?,sentence?,body?}` | 修改 |
| DELETE | `/api/highlights` body 或 query:`{file,id}` | 删除 |

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

### 4.1 工具栏色板

`#sel-toolbar` 底部一行 `[○○○○]`，没有 lbl / 标记按钮（之前曾加过，被用户要求去掉）。

行为：
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

`kind = 'note' | 'anki' | 'both'`：
- note：AI 把 snippets 重排为连贯笔记 → 写 `vault/<note_name>.md`（含 PDF 来源、原始截图引用），返回 obsidian:// URL
- anki：AnkiConnect 加卡（deck "QA"，tag "pdf-snippets"）
- both：两个都做

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

`/api/page-nodes` 查 KG：
- 遍历 `nodes/<book>/*.yml` 的 `pdf_pages` 字段，命中当前页号 → 返回 `{id, name, summary, state, numeric_label, book}`
- 点节点 → 新窗口跳 `/skilltree/<book>/#f.<id>`

详见 [`skill-tree-system.md`](skill-tree-system.md)。

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
