# 英语语法分析系统（task #182 / #183）

网页 PDF 阅读器里「按你正在跟踪的语法点，分析一句英文」的整套系统。选中 PDF 里一段英文 →
「📊 语法分析」→ 右侧抽屉里出现一张分析卡：整句中文翻译 + 句子结构图（spaCy 词性/依存/成分，
四种视图）+ 命中的跟踪语法点逐条讲解 + 底部追问框 + 一键制 Anki。

跟单词系统（[`vocab-system.md`](vocab-system.md)）的分工：单词系统管「词」，语法系统管「句子结构 + 语法点」。
两者都挂在 PDF 阅读器右侧同一个面板（`#grammar-panel`）上，靠顶部 tab 切换（语法 / 知识点 / 单词本）。

---

## 1. 概述：数据流总览

```
语法书 PDF
   │  scripts/kg/build_grammar_nodes.py（离线，一次性）
   ▼
knowledge_graph/<BOOK>.json   kind="grammar" 的三层 KG（L0 主题组 / L1 Unit / L2 语法点）
   │
   │  用户在技能树页 /skilltree/<BOOK>/ 点 L2 节点「👁 跟踪」→ n["tracked"]=true（skilltree.py toggle-tracked）
   │  用户在 PDF 设置面板勾选「本 PDF 启用哪些语法 KG」→ state/grammar-tracked/<sha>.json
   ▼
PDF 阅读器选中英文 → 「📊 语法分析」（onGrammarAnalyze）
   │
   ├─ /api/grammar-analyze   spaCy venv 子进程：词性 + 依存 + 成分 + 子句树（零 AI，秒级）
   │                          → state/grammar-cache/<sha>.json
   │
   └─ /api/grammar-stream    AI SSE 流式：先整句翻译，再命中的语法点讲解
                              → 完成后 /api/grammar-history-save 落 state/grammar-history/<sha>.json
```

核心理念：**结构分析（词性 / 依存 / 成分）走本地 spaCy，零 AI、毫秒级**；**翻译 + 语法点讲解走 AI 流式**。
语法点匹配的「语料库」就是用户**显式跟踪**的那批 L2 节点——只讲你正在学的点，不泛泛而谈。

---

## 2. grammar KG 三层抽取（`scripts/kg/build_grammar_nodes.py`）

从英语语法书 PDF 离线抽出 `kind="grammar"` 的知识图谱。跟数学书 `build_nodes.py` 的关键区别：

- **三层映射**：L0 = 主题组（Contents 页文字解析）/ L1 = Unit（PDF 内置 TOC）/ L2 = 讲解页 AI 抽的语法点。
- L2 **无「教材编号」**（数学书有 numeric_label），按语法点本身抽；节点带 `en` + `examples` + `tracked` 字段。
- 只扫**讲解页**，跳过习题页（首 160 字含 `Exercises`）+ 目录 / 答案 / Study Guide。
- 顶层 `kind="grammar"`，供 PDF 阅读器「按跟踪语法点分析」 + skilltree toggle-tracked 识别。

### 2.1 骨架解析（零 AI）

- `_find_contents_pages(doc)`：从 PDF TOC 自动定位 Contents 页范围（Contents 条目 → 第一个 Unit 条目之间）。
  找不到就兜底 `[5, 11]`。可用 `--contents-pages START-END`（1-based）手动指定。
- `get_toc()` 拿 Unit 列表（`_TOC_UNIT` 正则 `^\s*(\d+)\s+(.+)`，即「N Title」+ 页码）= **L1**。
  每 Unit 限 `≤2 页`（讲解 + 习题），免得最后一个 Unit 把全书末尾附录 / 答案 / Study Guide 全吃进去：
  `page_end = min(max(pg, end), pg + 1)`。
- `parse_unit_to_group(doc, contents_pages)`：解析 Contents 页**文字**得 `{unit_no: group_name}`。
  规则：非编号、非前言（`_FRONT_MATTER`）行 = 新主题组标题；其下的「N Title」行归属该组。
  主题组标题过滤：排除全大写提示语、含 `/` 的介词列表续行、超长（>40 字）说明行、罗马数字、纯数字行。
- `build_skeleton(...)`：生成 L0（主题组，按 Unit 首次出现顺序排）+ L1（Unit）节点，
  组的页范围 = 组内 Unit 的 min/max。

### 2.2 L2 语法点抽取（AI，按页）

- `_GRAMMAR_PROMPT`：对每张**讲解页**（图像 + 文字层，文字层乱码以图像为准）让 AI 抽「读者要掌握的具体语法点」
  （一个 Unit 通常 1~4 个）。每点字段：`name`（≤16 字中文）/ `en`（≤40 字英文术语）/ `summary`（≤45 字）/
  `examples`（该页 1~2 个英文例句，最多取 3 个）。明确**不要抽**练习题说明、Exercises、纯例句、Unit 标题复述。
  输出严格 JSON 数组，本页无语法点输出 `[]`。
- 页渲染缓存：`state/kg/page-cache/<book>-p<N>.png`（DPI 144）。
- **并行**：`ThreadPoolExecutor(max_workers=4)` 按 Unit 并发。`fitz`（PyMuPDF）非线程安全，渲染 / 取文字用
  `fitz_lock` 串行（有缓存很快）；AI 调用不加锁（claude_cli 每次 spawn 独立子进程，Unit 间真正并行）；
  `kg["nodes"]` 追加 + 落盘用 `write_lock` 串行。
- **同 Unit 内去重**：同名语法点合并 pages，不重复建节点。L2 id 形如 `egiu.l2.u1.1`。
- **断点续传**：out 已存在时加载旧 L2 + edges，按 `parent_id` 跳过已扫 Unit。
- AI 后端走 `make_backend("claude_cli", {model, effort})`，默认 `--model sonnet --effort medium`。

### 2.3 CLI 用法

```bash
python3 scripts/kg/build_grammar_nodes.py --pdf <path> --book EGIU \
    [--book-full "English Grammar in Use (5th ed.)"] \
    [--contents-pages 5-8]     # Contents 页范围（1-based），默认自动找
    [--units 7-12]             # 只跑 Unit 号范围（试水）
    [--limit N]                # 只跑前 N 个 Unit（试水）
    [--model sonnet] [--effort medium] [--workers 4]
    [--dry-run]                # 只生成 L0/L1 骨架，不调 AI
```

输出 `knowledge_graph/<book>.json`。**实战已抽**：`EGIU.json`（English Grammar in Use 5th ed.）
= 18 个 L0 主题组 + 145 个 L1 Unit + 598 个 L2 语法点。

> 注意：抽出来的是「裸 L2 节点」（`state` / `mastery` 等字段缺省）。register / KG 同步流程
> （见 [`skill-tree-system.md`](skill-tree-system.md)）会补 `state`/`unlocked`/`mastered`/`mastery_level` 等字段。
> EGIU 全是 `unlockable`（语法书无前置依赖链，不像数学需要锁）。

---

## 3. grammar KG 节点结构

### 3.1 顶层（`knowledge_graph/EGIU.json`）

```json
{ "book": "EGIU", "kind": "grammar", "title": "English Grammar in Use (5th ed.)",
  "pdf": "...", "nodes": [...], "edges": [] }
```

`kind == "grammar"` 是整个系统的「开关位」：

- `pdf_reader.py::pdf_api_grammar_books` 只列 `kind=="grammar"` 的 KG；
- `_collect_grammar_tracked_nodes` 只从 grammar KG 收 tracked 节点；
- `skilltree.py::toggle-tracked` 只允许 grammar KG 的 level-2 节点切 tracked；
- PDF 知识点抽屉里只有 `n.kind === 'grammar'` 的节点才渲染「👁 跟踪」按钮。

### 3.2 三层节点字段

| level | id 例 | 关键字段 |
|---|---|---|
| 0 主题组 | `egiu.l0.g1` | `name`（"Present and past"）、`pages`、`parent_id=null` |
| 1 Unit | `egiu.l1.u1` | `name`（"Present continuous (I am doing)"）、`unit_no`、`pages`、`parent_id`=L0 |
| 2 语法点 | `egiu.l2.u1.1` | `type="grammar_point"`、`name`/`en`/`summary`/`examples`/`pages`、**`tracked`**（bool）、`parent_id`=L1 |

L2 完整字段（KG 同步后）：
```json
{"id":"egiu.l2.u1.1","level":2,"parent_id":"egiu.l1.u1","type":"grammar_point",
 "name":"现在进行时的基本结构与用法",
 "en":"present continuous: am/is/are + -ing for actions happening now",
 "summary":"用 am/is/are + 动词 -ing 表示说话时刻正在发生的动作，动作尚未结束",
 "examples":["She's driving to work.","He's having a shower."],
 "pages":[14],"tracked":true,
 "state":"unlockable","unlocked":true,"mastered":false,"mastery_level":0, ...}
```

**`tracked`** 是语法系统独有的字段——用户在技能树页点节点的「👁 跟踪」按钮 toggle 它。
分析句子时只把 tracked=true 的 L2 节点喂给 AI。

### 3.3 `_server_deploy/grammar-nodes.json`（旧 demo，已弃用但保留兼容）

阶段 1（#182）的扁平 demo 数据：24 条手写语法点，字段是 `{id, name, category, summary, keywords}`（**无层级**，
跟正式 KG 的三层结构完全不同）。只被 `/api/grammar-nodes` 这个老路由读，前端已不用它。
正式系统改用 `knowledge_graph/<book>.json` 的三层 KG + per-PDF 启用书机制。

> 历史：曾有 `knowledge_graph/grammar-demo.json`（kind="grammar"、35 节点的 demo KG，
> book="grammar-demo"），后被删（git 工作树 D）。PDF 设置面板里那个
> `/skilltree/grammar-demo/` 链接是这段历史的残留指向。

---

## 4. spaCy 解析（`scripts/spacy_parse.py`）—— 为何独立 venv 隔离

### 4.1 venv 隔离原因

webapp 跑系统 Python（`/root/webapp` 的 Flask 进程），**装不了 spaCy + en_core_web_sm 模型**
（依赖重、跟 webapp 环境冲突）。所以 spaCy 单独装在一个隔离 venv 里，webapp 通过 **subprocess** 调它：

- venv 路径：`SPACY_PYTHON` env（默认 `/home/bwicarus/spacy-venv/bin/python`）。
- 脚本：`scripts/spacy_parse.py`（`SPACY_SCRIPT`）。
- `pdf_reader.py::_spacy_available()` = `SPACY_PY.exists() and SPACY_SCRIPT.exists()`，
  不可达时 `/api/grammar-analyze` 回退到 AI 兜底路径。
- 调用：`subprocess.run([SPACY_PY, SPACY_SCRIPT, sentence], capture_output=True, timeout=30)`，
  stdout 是 UTF-8 JSON。

### 4.2 spacy_parse.py 做的解析

`spacy.load("en_core_web_sm", disable=["ner","lemmatizer"])`（只要 tagger + parser，关 ner 提速）。
`parse(text)` 返回五块结构，全部供前端画「句子结构」用：

| 字段 | 内容 | 前端视图 |
|---|---|---|
| `tokens` | `[{text, pos}]`，pos 是 UPOS 映射成前端约定的小写键（noun/verb/adj/adv/pron/prep/det/conj/aux/num/part/intj/punct）| 弧线图节点 |
| `deps` | `[{head, child, label}]`，label 是英文依存标签译成中文（主语/宾语/定语/状语…，见 `DEP` 表）| 弧线图弧 |
| `clauses` | 按依存树切子句段：每个子句根（ROOT/relcl/advcl/ccomp/…）管辖一段，token 归属最近的子句根祖先 | 旧数据回退：从句平铺分段 |
| `components` | 句子按「成分」线性切块（主语/谓语/宾语/状语/各类从句…），带 `parent`（挂靠上层成分序号，-1=顶层）+ `start` | 块 / 树 / 主干视图 |
| `clause_tree` | 嵌套子句树：每层 `{label, nodes, deps, children}`，子从句收成占位节点 `⟨从句类型⟩`（pos='clause', ref=子层下标），点占位展开该层弧线 | 可逐级展开的弧线图 |

中文词义（`zh`）和整句翻译（`sentence_zh`）**不在 spacy_parse 里做**——由 webapp 端补：
`pdf_reader.py::_spacy_grammar` 拿到 spaCy JSON 后，用 **ECDICT 离线字典**（`scripts/vocab/dict_sources.lookup_ecdict`）
给每个 token / 子句 token / 子句树节点补 `zh`（毫秒级、零 AI、带缓存）；整句翻译留空，交给 AI 流式（`/api/grammar-stream`）。

`_split_components` / `_split_clauses` / `_clause_tree` 的共同套路：先标出「边界 token」（ROOT + 各类从句根 +
主要成分头），再让每个 token 通过 `owner()` 沿依存树向上找最近的边界祖先归属，从而把嵌套从句从宿主里「挖」出来。

---

## 5. `pdf_reader.py` grammar 路由清单

全部在 `pdf_reader.py` 约 L2028–2645（`bp` blueprint，挂在 `/pdf` 前缀下）。

| 路由 | 方法 | body / query | 返回 / 行为 |
|---|---|---|---|
| `/api/grammar-nodes` | GET | — | 旧 demo 扁平节点 list（读 `grammar-nodes.json`，保留兼容，前端已不用）|
| `/api/grammar-books` | GET | — | 所有 `kind=grammar` KG：`[{book, title, total_l2, tracked_count}]`（扫 `knowledge_graph/*.json`，跳 `.bak.json`）|
| `/api/grammar-tracked` | GET/POST | GET `?file=<rel>` / POST `{file, enabled_books:[...]}` | **per-PDF 启用的 grammar KG 书列表**（不是节点 id！）。存 `state/grammar-tracked/<sha1(file_rel)[:16]>.json`，格式 `{pdf_rel, enabled_books}` |
| `/api/grammar-analyze` | POST | `{text, sentence?, file, enabled_books?, model?, effort?}` | 句子结构分析。**优先 spaCy**（`engine:"spacy"`，返回 tokens/deps/clauses/components/clause_tree），spaCy 不可达才 AI 兜底。缓存 `state/grammar-cache/<sha1(sentence‖text‖sorted(tracked_ids))[:20]>.json` |
| `/api/grammar-stream` | POST | `{sentence, text, file, enabled_books?, model?, effort?}` | **SSE 流式**。先 `[[TRANS]]整句翻译[[/TRANS]]`（先到先显示），再 `[[POINTS]]JSON 数组[[/POINTS]]`（命中的跟踪语法点讲解）。配合 spaCy 出的依存图用——依存图不在这里出 |
| `/api/grammar-history` | GET | `?file=<rel>` | 该 PDF 的分析历史，按 `ts` 倒序（新在前）|
| `/api/grammar-history-save` | POST | `{file, item}` | 保存一条结果（同句去重更新，限 200 条）。`item` 含 sentence/text/sentence_zh/tokens/deps/clauses/components/clause_tree/analyses。存 `state/grammar-history/<sha1(file_rel)[:16]>.json` |
| `/api/grammar-forget` | POST | `{sentence, text, file, enabled_books?}` | 删该句缓存 + 历史（删卡 / 删块时调）。用**跟 grammar-analyze 完全一致的 cache_key 算法** 命中缓存文件 |

辅助函数：`_grammar_nodes()`（读 demo）、`_tracked_path()`（sha 路径）、`_load/_save_grammar_enabled()`（启用书读写，兼容老格式）、`_collect_grammar_tracked_nodes(enabled_books)`（汇总所有启用书的 tracked L2 节点给 AI）、`_spacy_grammar(sentence)`（subprocess + ECDICT 补 zh）、`_grammar_hist_load/write()`。

### 5.1 整句翻译 + 语法点讲解的 SSE 流式

`/api/grammar-stream` 用 `_sse_stream(prompt, model, effort)`（与 PDF 阅读器其它 AI 路由共用）。prompt 强制 AI
**按顺序、用标志输出两部分**，标志原样出现、不加代码块围栏：

```
[[TRANS]]整句中文翻译[[/TRANS]]
[[POINTS]][{"point":..,"phrase":句中实例,"explanation":针对该句的讲解,"examples":[相似例句]}][[/POINTS]]
```

前端 `_streamGrammar`（pdf_reader.html）边收边正则抠：翻译标志一闭合就立刻填到常驻翻译行（`.gb-trans`），
语法点标志闭合再 parse JSON 渲染。流中断有兜底（从残文里抠未闭合的标志）。
默认 `model=haiku, effort=low`（轻量任务）。命中语料只取 `_collect_grammar_tracked_nodes` 给的 tracked 节点，
无跟踪点则 prompt 让 AI 直接输出 `[[POINTS]][][[/POINTS]]`。

---

## 6. 「右侧行对齐抽屉」UI（task #183，`templates/pdf_reader.html`）

> 命名沿用 task #183「右侧行对齐抽屉」。实际落地的形态：**右侧固定抽屉 + 分析卡按时间堆叠**（最新在最上），
> 不是逐行左右对齐 / 滚动同步（代码注释 L3684「抽屉开关（流式堆叠，无左右对齐/滚动同步）」明确说明了演进结果）。

### 6.1 抽屉骨架

- `#grammar-panel`（`position:fixed; right:0; width:38vw; max-width:560px`，窄屏 58vw）：右侧滑出抽屉，
  `.open` 才显示。`body.grammar-open` 时 `#main`/`#header` 加 `padding-right` 给抽屉让位，
  并触发 `_refitToWidth(true)` 重算 PDF scale（PDF 变窄不糊）。
- 顶部 **side-tabs**：`📊 语法` / 知识点 / 单词本，`switchSideTab(pane)` 切换。语法 tab 才显示「🗑 清空」。
  首次进语法 tab 自动 `loadGrammarHistory()`（刷新后默认语法 tab 也能显示历史记录）。
- 入口：选中工具栏的 `📊 语法分析` 按钮（`#grammar-btn-row`，仅当 `_grammarHasTracked && lastSelText` 时显示）；
  顶栏 / 侧边 handle `toggleGrammarPanel()`；单词小框里的「📊 语法」按钮（复用 onGrammarAnalyze，单词自动扩成整句）。

### 6.2 分析卡（`.grammar-block`）

每次分析往抽屉顶部插一张卡（`_addLoadingBlock`），同句旧卡先移除避免重复。卡结构：

- **标题栏**（`.gb-header`，可点折叠）：句子摘要（前 60 字）+ 语法点角标（`语法点 N`）+ `🗑` 删除 + 折叠箭头。
  `🗑` 调 `/api/grammar-forget` 真删后端缓存 + 历史。
- **常驻翻译行**（`.gb-trans`，折叠也可见）：`🌐 整句翻译`，流式到达前显示「🌐 翻译中…」（呼吸动画 `gb-pending`）。
- **结构图区**（`.gb-diagram-wrap`，可折叠）：标题「📐 句子结构」+ 四视图切换按钮 + 图。
- **语法点区**（`.gb-analyses`）：每条 `📊 点名 / 📍 句中实例 / 讲解 + 例句`，点击展开。
- **底部追问**（`.gb-followup`）：输入框 + 「追问」（`_grammarFollowup`，走 `/api/explain` SSE，带原句+译文+已有分析作上下文，Markdown+MathJax）+ `🎴` 制 Anki（`_grammarAnki`，整句+译文+分析+追问+原文出处链接 → `/api/snippets-to-async` 后台 job）。

### 6.3 四种句子结构视图（`setGrammarView` + `_renderStructInto`）

全局模式存 `localStorage['pdf-grammar-view']`，默认 `components`。`GV_MODES = [['tree','树'],['components','块'],['skeleton','主干'],['deps','弧线']]`。切换立即重渲染所有已显示卡的结构区。

| 模式 | 渲染函数 | 形态 |
|---|---|---|
| **树** | `_renderTree(components)` | 成分树：折叠看整句片段，逐级点开看成分细节；从句节点展开后谓语作子项 |
| **块** | `_renderComponents(components)` | 主谓宾定状从句彩色块，无弧线、可换行（长句清晰）|
| **主干** | `_renderSkeleton(components)` | 核心成分（主谓宾表）行内显示，修饰 / 从句折叠成可点 chip |
| **弧线** | `_renderDepTree(clause_tree)` / 旧数据回退 `_renderDepSvg` | displaCy 风格依存弧线：弧在词上方、词性着色、词下中文词性标，占位节点点开看从句弧 |

成分配色 `_compColor`（谓红 / 主蓝 / 宾绿 / 定青 / 状紫 / 表黄）；词性配色 `POS_COLORS` + 中文短标 `POS_LABEL`。

### 6.4 分析触发流程（`onGrammarAnalyze`）

1. 校验：有选中、至少启用一个语法 KG、启用 KG 中有 tracked 节点、有 char-layer 选中位置。
2. 从选中字符位置用 `_expandSentenceFromRange` 向两端扩成完整句子（句太短 / 识别不出报 toast）。
3. 打开抽屉 + 切语法 tab，插 loading 卡。
4. `POST /api/grammar-analyze` → spaCy 结构图秒出（`_fillGrammarBlock`，翻译 / 语法点先占位）。
5. 若 `engine==="spacy"`，接着 `_streamGrammar` 走 `/api/grammar-stream` SSE 补翻译（先）+ 语法点（后）。
6. 流完 `/api/grammar-history-save` 落历史。

### 6.5 per-PDF 启用书设置

PDF 设置面板「📊 启用语法分析（per-PDF）」`renderGrammarTrackList`：列所有 grammar KG（checkbox + tracked 数 +
「技能树 →」链接）。勾选 → `_onGrammarBookToggle` → `saveGrammarEnabledBooks` → POST `/api/grammar-tracked`。
**「启用哪本书」（per-PDF，PDF 设置）** 和 **「跟踪哪些语法点」（per-节点，技能树页 toggle-tracked）** 是两件事，
分析时取「启用书 ∩ 各书内 tracked 节点」。

---

## 7. 跟踪机制 / 掌握度现状（task #183 完成、#185 待办）

**已落地（#182 阶段1 + #183 阶段2）**：

- 节点级 `tracked` 开关（技能树 toggle-tracked）+ per-PDF 启用书（grammar-tracked）。
- 「跟踪 = 进入语法分析的语料」：只有跟踪的点才会在句子里被识别 + 讲解。

**尚未落地（task #185「B: 语法点学习闭环（掌握度机制）」pending）**：

- L2 节点虽有 `mastery_level` / `mastery` / `state` 等字段（KG 同步补的），但**语法点本身没有独立的复习 / 掌握度闭环**。
  目前 `tracked` 是个纯布尔开关，不随「这个点在多少句子里被正确识别 / 复习过」演进。
- 设想的闭环（#185）：语法点像单词的 mastery 一样累积暴露 / 复习信号，自动调整跟踪优先级、提示「该复习哪个语法点」。
- 相关待办还有 #186（grammar AI 任务全部走后台 job + 断连恢复，现在 grammar-stream 是前端直连 SSE，移动端断连会丢）
  和 #187（pdf_reader 前端模块化 + 状态收敛）。

> 当前「学习」靠两条人工路径：分析卡 `🎴` 制 Anki（整句 + 译文 + 分析进卡片），以及 grammar-history 持久化（同句再看不重算）。

---

## 8. 踩坑

1. **`grammar-tracked` 是「启用书列表」不是「节点 id 列表」**。文件名带 `tracked` 容易误解。`_load_grammar_enabled`
   做了兼容：老格式（`tracked` 存 node ids）直接返回 `[]`，只认新格式的 `enabled_books`。改这块别把两层语义搞混。
2. **删卡 / 删块清缓存的 cache_key 必须跟 grammar-analyze 完全一致**：`sha1(sentence + "||" + text + "||" + ",".join(sorted(tracked_ids)))[:20]`。
   tracked_ids 变了（用户改了跟踪 / 启用书）→ key 变 → 同句会重算、旧缓存命不中（这是预期：跟踪集变了分析也该变）。
3. **spaCy venv 必须真装**：`SPACY_PYTHON` 指向的 venv 不存在 → `_spacy_available()` 假 → 退 AI 兜底（慢且耗额度）。
   迁移 / 新机器记得建 `/home/bwicarus/spacy-venv` 装 spacy + `en_core_web_sm`。
4. **PyMuPDF（fitz）非线程安全**：`build_grammar_nodes.py` 并行抽 L2 时，渲染 / 取文字必须在 `fitz_lock` 里串行，
   只有 AI 调用能真并行。
5. **build 时 Unit 页范围卡 ≤2 页**：否则最后一个 Unit 的 `page_end` 会吃到全书末尾的附录 / 答案 / Study Guide，
   抽出一堆垃圾语法点。
6. **`_GRAMMAR_PROMPT` 输出严格 JSON 数组**：AI 偶尔裹 ` ```json ` 围栏，`extract_points` 会剥围栏 + 取
   `[ ... ]` 子串再 parse；`grammar-analyze` AI 兜底路径同样剥 ` ``` ` 再试一次。
7. **SSE 标志解析的兜底**：`_streamGrammar` 流中断 / AI 没闭合标志时，从残文里用宽松正则（`[[/TRANS]]|[[POINTS]]|$`）
   抠翻译，语法点兜底成 `[]`。后端 prompt 必须强调「标志原样出现、不要加代码块围栏」，否则前端正则抠不到。
8. **grammar-stream 是前端直连 SSE，没走后台 job**：移动端切后台 / 断连会丢分析结果（#186 待解）。

---

## 相关文档

- [`references/pdf-reader.md`](pdf-reader.md) — 网页 PDF 阅读器主文档（char-layer 选中、高亮、AI 翻译/解释、右侧抽屉宿主）
- [`references/skill-tree-system.md`](skill-tree-system.md) — 知识图谱 / 技能树系统（KG 结构、register 同步、toggle-tracked、状态机）
- [`references/vocab-system.md`](vocab-system.md) — 单词系统（同一抽屉的「单词本」tab、ECDICT 复用、mastery 闭环可参照 #185）
