# ADR:任务运行时(Task Runtime)+ 页面块 —— AI 出卷、你手写、AI 批改

日期:2026-07-14 · 状态:**已采纳,先做「听写」垂直切片** · 提出者:用户

---

## 1. 要做什么

让 AI 能**新建页 + 往页里写结构化内容**(试卷/听写纸/练习题),你**手写填写**,AI **看图批改**;
并引入一个**确定性的任务运行时**来编排这类长任务(有停顿、有等待、有循环)。

第一个垂直切片 = **AI 听写**:

```
AI(一次调用) → 生成 words[] + 起一个 run
──────────── 之后 LLM 完全不参与循环 ────────────
运行时: 写一张听写纸(N 个 blank + 「下一个」按钮)
        loop i in words:
            say(words[i])            ← 借 client_action 让前端 TTS 念
            wait_event("next")       ← 挂起,等你按按钮
        逐个 blank 裁图 → llm 批改   ← LLM 只在这里回来一次
        把批改结果写回页面
```

---

## 2. 铁律:**LLM 不做等待,也不做循环**

最自然的想法是让 AI 在工具循环里挂起等你按按钮。**这条路会死**:

- **烧钱**:每等一次,整个上下文重发一遍
- **不可恢复**:刷新 / 断网 / 切后台 → 任务没了
- **不可靠**:模型会忘记念到第几个、会重复、会提前收尾

**正解**:循环、等待、超时、恢复 全部交给**确定性的运行时**;LLM 只做两件它擅长的事——
**生成内容**(words[])和**做判断**(批改)。

> 这与项目里 Workflow 的模式一致(确定性控制流 + 嵌入 LLM 步骤),也是业界 durable execution 的通行做法。

### 铁律二:**运行时不许有"阻塞等待的线程"**

2026-07-14 的事故记忆犹新:SSE 长连接每条独占一个 gthread 线程,8 条就把线程池吃光、**全站零响应**
(见 `reader_events.py` 文件头 + memory `sse-thread-starvation`)。

所以运行时是**事件驱动的状态机**,不是"等待的线程":

- 状态存**文件**(durable) → 进程重启不丢
- 推进由**事件**触发:① 按钮 HTTP 上报 ② 定时器到点 ③ 前端回前台对齐
- **任何时刻零线程**被这个任务占用

---

## 3. 关键设计决策(测绘之后定的)

### 3.1 🔴 批改的图从哪来 —— 绕开最大的坑

**坑**:`pdf_reader._figure_crop_png()`(pdf_reader.py:8746)是**从磁盘上的 PDF 文件渲页**。
而 overlay 插入页在 PDF 文件里**是一张空白真页** —— 内容只在 sidecar(`md_ver > synced_ver` 时压根没写回)。
→ 按 bbox 裁出来 = **白纸 + 墨迹**,AI 看不到题号/填空框/提示词。

**绕法(采纳)**:**批改不需要图里有题号。**

- 每个 `blank` 块存一个**页归一化 bbox**(0-1)
- 裁图:`_figure_crop_png(ap, page, box=blank.rect, with_ink=True, rel=rel, strokes=strokes)`
  → 只裁出**这一格里你手写的字**
- 题号 / 正确答案 **在 prompt 里用文字给**:
  「第 3 题,正确答案是『憂鬱』。这是用户写的:[图]。判断对错。」

**收益**:零新渲染器、不依赖视口截图(它只截一屏且 ≤900KB)、不依赖异步写回 PDF、
分辨率可控(现成代码会把长边动态拉到 ~1100px —— 注释明写这是手写体识别的下限,
旧版固定 scale 导致"多家模型全认错",pdf_reader.py:8896-8930)。

**被否决的两条**:
- ❌ 先把内容烧进 PDF 再裁:依赖 `_inspage_job`(单书互斥、大书慢),且现在烧的是 `_up_md_html(md)`、不认块。
- ❌ 前端视口截图:只截一屏(N 个 blank 超屏)、分辨率受限、依赖 html2canvas。

### 3.2 服务端怎么"念" —— 借道,不新建通道

**全站没有服务端 TTS**(webapp 无端点;relay 的 `handle_tts_only` 只被动读浏览器 WS)。

**采纳**:`reader_events.publish("client-action", file, uid, {"action":{"fn":"__vcSpeakText","args":[word]}})`
→ 阅读器页 SSE 收到 → `RC.execRemote`(rc-assistant.js:2149)→ `window.__vcSpeakText(word)`(rc-voicecall.js:1937)
→ 走前端已有的完整 TTS 链路(分段、打断、AEC)。**MCP 遥控翻页已经在用这条路。**

⚠ 三个硬约束:
1. **页面不可见时 SSE 事件直接丢**(`pdf-tail.js:364` / `epub-html.js:3573` 的 `visibilityState !== 'visible'` 早退)
   → 必须有 `GET /pdf/api/run-status?rid=`,前端 **visibilitychange 回前台时拉状态机对齐**,不能只靠推送。
2. **iOS AudioContext 必须在点击手势的同步栈内 warm**(`window.__vcTtsWarm()`,rc-voicecall.js:1936)
   → 听写「开始」按钮**必须**调它,否则 iOS 上无声。
3. `html_reader.html` **没有引 rc-voicecall.js** → HTML 阅读器里没有 `__vcSpeakText`。**先只做 PDF。**

### 3.3 存储:文件驱动,照抄 preprocess 范式

现有三套任务表(`_vtasks` voice.py:829 / `_JOBS` pdf_reader.py:7813 / `_chat_jobs`)**全是进程内存 dict**,
webapp 一重启就丢,且**没有"挂起等待用户事件"这个状态**。

→ 新建 `state/reader-runs/<rid>.json`,照 `state/book-preprocess/<sha>.json` 的文件驱动范式
(pdf_reader.py:1257-1276:phase + percent + updated_at + pid 存活检测)。

---

## 4. 数据结构

### 4.1 页面块(挂在插入页 sidecar 的新字段 `blocks`)

插入页记录现在只有 `{id, page, title, md, mode, md_ver, synced_ver}`(真实样本见测绘)。
**加一个 `blocks` 字段**,`md` 路径原样保留(向后兼容):

```jsonc
{
  "id": "u_26b4ffda", "page": 32, "mode": "overlay",
  "kind": "dictation",              // 这张纸是什么(供运行时/渲染识别)
  "run_id": "r_ab12",               // 关联的运行时
  "blocks": [
    {"id":"t1","kind":"text","text":"日语听写 · 2026-07-14","style":"h1"},
    {"id":"q1","kind":"blank","label":"1.","rect":[0.10,0.18,0.90,0.26]},   // ★ 页归一化 0-1
    {"id":"q2","kind":"blank","label":"2.","rect":[0.10,0.28,0.90,0.36]},
    {"id":"b1","kind":"button","label":"下一个","event":"next"},
    {"id":"c1","kind":"checkbox","label":"我写完了","event":"done"}
  ]
}
```

- **`rect` 是页归一化 0-1** —— 与墨迹坐标(`RCInk.norm`,rc-ink.js:80)、与 `_figure_crop_png` 的 `box`
  **同一坐标系**。这是"按填空裁图"能成立的关键。
- 布局后由**前端把实际 rect 写回** sidecar(PATCH),因为只有前端知道渲染后的真实位置。

### 4.2 运行(`state/reader-runs/<rid>.json`)

```jsonc
{
  "rid": "r_ab12", "kind": "dictation", "uid": "7",
  "file": "资源/uploads/xxx.pdf", "page": 32, "upage": "u_26b4ffda",
  "status": "waiting",              // running | waiting | done | error | cancelled
  "step": 3,                        // 当前走到第几步
  "wait": {"event": "next", "since": 1784041000, "timeout_s": 1800},
  "params": {"words": ["憂鬱", "薔薇", ...], "lang": "ja"},
  "state": {"i": 2},                // 循环游标
  "result": null,
  "updated_at": 1784041000
}
```

---

## 5. 要新增/改动的东西(精确到 file:line)

| # | 改哪 | 做什么 |
|---|---|---|
| 1 | **新建 `_server_deploy/task_runtime.py`** | 状态机:`start(kind, params, ctx)` / `advance(rid, event)` / `status(rid)`;文件驱动 |
| 2 | `pdf_reader.py` 新路由 | `POST /pdf/api/run-event {rid, event}`(按钮回执)· `GET /pdf/api/run-status?rid=`(回前台对齐)<br>⚠ 挂 `/pdf` 前缀 → 自动进 `PROTECTED_PREFIXES`(app.py:349)拿到 session+Bearer 双认证 |
| 3 | `pdf_reader.py` `pdf_api_userpages` PATCH 白名单(:6067-6081) | 放行 `blocks` / `kind` / `run_id`(现在只认 `title/md/after/h`,**没列进去的字段会被静默丢掉**) |
| 4 | `static/pdf/pdf-uishared.js` `_upRenderOverlay`(:721) | `if (rec.blocks) renderBlocks(body, rec) else 原 md 路径`;button/checkbox 点击 → `POST /pdf/api/run-event`<br>⚠ 覆盖层拦手势用**冒泡非捕获**(memory `overlay-gate-use-bubble-not-capture`:捕获阶段 stopPropagation 会吞掉内部按钮事件) |
| 5 | `assistant.py` `TOOLS`(:2970) | 加 `start_dictation`(生成 words + 起 run)。**唯一注册表** → 侧栏/语音/MCP **自动全都有** |
| 6 | `assistant.py` `_tool_label`(:3061) | 中文名 |
| 7 | `assistant.py` `_AP_ACTIONS`/`_AP_DEFAULTS`(:3627) | 注册 `dictation_grade` action key → 批改能进设置面板、能用 `_resolve` 兜底链 |
| 8 | `assistant.py` `VOICE_CACHEABLE_TOOLS`(:4751) | **千万别加**(写工具;"再来一次听写"是合法语义) |

**复用(零新代码)**:
- `reader_events.publish` → `client-action` → `RC.execRemote` → `window.__vcSpeakText`
- `_figure_crop_png(box, with_ink=True)` 按 bbox 裁图 + 叠墨迹
- 插入页墨迹**存在同一个 pdf-ink 边车、键=真页号**(`_upInkPersist`,pdf-uishared.js:709)→ 裁图直接拿得到
- `reader_vision(images, prompt, action=...)`(assistant.py:3841)做批改

---

## 6. 明确不做的

- **不另开 EventSource**。SSE 是稀缺资源(舱壁 12 总 / 4 每用户,每条独占一个线程)——
  必须**复用现有那条**,只加新的 `kind`。
- **不复用 `_task_sema`**(voice.py:694 的 `Semaphore(2)`,**阻塞排队**)。听写要等你写完 N 个词,
  占着它会把制卡/笔记全堵死。
- **不复用 `RC.toolChip.track`**(rc-toolchip.js:748:240 次 × 1.2s ≈ **5 分钟就 fail**)。远不够。
- **先只做 PDF**。EPUB 插入页是虚拟段(`.ep-usec`)、坐标系不同;HTML 阅读器连 `__vcSpeakText` 都没有。
- **不做通用配方系统**。先把听写这一个场景端到端跑通(它把四层全用上),再抽象。

---

## 7. 之后:配方(高级工具)

听写跑通后,把「任务定义」抽出来存成配方 → 变成 AI 的**一个工具**(`run_recipe("听写", {words})`)。
届时再定:参数化、版本、per-user 覆盖(可复用 `TOOL_SLOTS` / `state/assistant-tool-prompts.json` 的范式)。

---

## 8. 相关

- `references/adr-turn-container.md`(同一条原则:**服务端权威**,前端不是真相源)
- `references/reader-userpages-favorites.md`(插入页现状)
- memory `sse-thread-starvation`(为什么运行时不许有阻塞等待的线程)
- memory `overlay-gate-use-bubble-not-capture`(覆盖层里的按钮为什么会失灵)
- memory `ios-button-white-block` / `verify-innermost-child-not-container`(块渲染的 UI 坑)

## §8 保存工具的判型(2026-07-17,用户点破的设计漏洞)

轨迹保存(save_trace_recipe)原本把一切冻成字面 calls——**生成型任务**(读页→AI出题→建卷)回放时
AI 不在场:10 题工具永远回放当年那 10 道原题,15 题/换页出新题都不成立。修:保存时**自动判型**——
- 轨迹含造纸步骤(page_new/add/show)且有原始 instruction(vtask 完成时随 steps 存)→ **意图配方 kind:'intent'**
  {instruction 原话, anchor_page, calls 留档}:运行=重新起 CLI(paper 预设),指令=原意图+args.adjust(本次调整,
  冲突以本次为准)+「先读当前页、内容重新生成、骨架沿用」——AI 上下文=一次全新造纸会话;
- 纯机械序列 → kind:'trace' 原语义(进程内回放,零 token 秒回);无 instruction 的生成型退回 trace(兼容)。
run_saved_task args 加 adjust;intent 分支返回 task_id 走 CLI 卡(_AGENT_TASKS/前端三处路由/_tool_label 已接)。

### §8b 操作路线抽象「指挥棒」(2026-07-17,用户设计)

意图配方在 instruction 之外再存 `route` = **程序自动抽象的成功执行路线**(_abstract_route):
逐步 `工具(参数骨架)`——字符串内容→占位提示、数字标[可调]、page_add 的 blocks 归纳成构成模式
(如「批量 11 块(text:5, blank:5带answer, button:1 event=check)——按新数量复制同构,题面重新生成」)。
运行时整段注入指令:「严格按此步骤顺序与结构执行,内容按本次要求重新生成」——新 CLI 不重新摸索流程,
沿已验证路线走,只换内容/参数。意图=要做什么,路线=怎么做,合成=指挥棒。

### §8c 节选工具的「调用开端」= 决定起点那步的 AI 思路(2026-07-17,用户设计)

CLI 执行时**随步记录 rationale**(claude 分支:工具调用前累积的散文,tool_use 时摘尾 500 字存进该步
并清空;codex 无 per-step 散文,拿当时累积文本近似)。框选节选保存时:起点步 rationale = 这段子流程
的**真实局部意图**,存 rec["origin"];起点无 rationale → 轻 AI(gemini flash,think=False)按
「大任务+节选路线」总结一句;再无 → 留空。运行合成:partial+origin → "这段子流程的起始思路(当时
AI 决定这么做的原话,作为本次执行的出发点):『…』+ 执行范围以路线为准";无 origin 退回警示方案。

### §8d 节选语义收紧:origin 只按路线总结 + 运行不附原始意图(2026-07-17,用户实锤修正)

§8c 的「rationale 直取」被用户实测击穿:框掉「查高亮」步骤保存的《试卷制作》,测试时 AI 仍去查高亮。
两处泄漏——① 起点步(read_page)的 rationale 是**被框掉那一步的产物叙述**("Found 3 highlights…
reading those pages"),直取等于把删掉的语义从后门带回;② 运行合成还附了全量原始意图(『找到我画的
高亮并制卷』)。修:**节选一律 AI 总结 origin,输入只有节选路线本身**(不给 rationale、不给大任务
instruction——都带被删语义;prompt 声明"字符串参数(标题/文案)是数据示例,别把其中的词当动作",
防纸标题『高亮内容小测』这种词泄漏);**partial 运行不再给原始意图**,只给 origin + "路线里没有的
步骤类型(如没查高亮步骤就绝不查高亮)一律不要做"。rationale 机制保留(流程展示用),不再当 origin。

## §8e 选择题原语 choice + blank 长标签多行(2026-07-17,用户实测排版翻车)

用户截图:选择题被塞成一行(题干+A~D 选项全在 blank 的 label 里),超出页右缘被截断。根因:
块模型没有选择题原语,CLI 只能把整道题塞进单行 blank;且 blank 固定 [1, C] 一行高、
`.up2-b-lab` 不换行。修:
- **choice 块**:`{kind:'choice', text:'题干', options:['81.47歳',…], answer:'C'}`。
  paper.py default_span 算行数=题干行(ceil(wide/C))+选项行(贪心装行:一行放得下就一行,
  否则自动多行)+1 行作答线;渲染(pdf-uishared `_upRenderBlocks`)三段纵排:题干(wrap)/
  选项 flex-wrap(自动补 A. B. C. 前缀)/「答:____」短线。判分两路(shots 首选+服务端拼图回退)
  的空过滤加 choice(标注"选择题,答字母即可")。_norm_block 容错:options 缺→降级 text;
  字符串→按 / 拆;选项自带字母前缀→剥掉;answer 取首字母大写。
- **blank 长标签多行**(通用底座):default_span 按 ceil((wide(label)+8)/C) 给行数;
  CSS `.up2-b-blank` flex-wrap + `.up2-b-lab` 可换行 + `.up2-b-box{flex:1 1 8em;min-height:1.1em}`
  (作答线落到末行)。旧单行 blank 渲染不变(内容放得下仍一行)。
- page_add 提示词明说:**选择题必须用 choice,别把题干+选项塞进 blank 的 label**。
  重新生成型工具重跑时 CLI 看到新说明,自动改用 choice(旧纸 blocks 已定型不回改,重跑即新排版)。
- 沙盒 E2E:短选项 1 行/长选项 2 行/旧式长 blank 换行,程序化溢出检查(scrollWidth+子元素右缘)零命中。

## §9 工具库沙盒模拟环境(2026-07-17,用户设计:测试在模拟环境跑+直观看到结果)

用户:「工具测试应该在模拟环境中进行且需要能直观看到结果,而不是要跑到阅读器里检测」。落地三件套:

- **沙盒副本** `POST /pdf/api/sandbox {file, reset?}`:把书拷到 `资源/uploads/.sandbox/<原名>`
  (点目录:Obsidian Sync 不同步、list-pdfs/全文搜索/push_big_files 全排除),reset 时重拷+清边车
  (userpages/highlights/ink)。工具页 send 前先 ensure(失败**不发**,绝不落原书)。ctx.file_rel
  一律换沙盒 rel → CLI 读页/造纸/高亮/检查报告全部落副本。
- **预览 = 真实阅读器 iframe**(一份代码铁律):工具页右侧滑出面板,iframe 开 `/pdf/view?file=<沙盒rel>`
  ——纸/高亮/手写/AI 检查全套真功能免费得到,零阅读器改动。
- **产物投递 = SSE 总线** `POST /pdf/api/publish-actions {file, actions}` → `publish("client-action",…)`
  (与 MCP 遥控同通道,pdf-tail 的 SSE handler → RC.execRemote → window[fn])。工具页定义
  `window.__upStartTask/goToPage/openBookAt` **转发器**接管 rc-turncard `_applyNewCAs` 的 window[fn]
  调用(openBookAt 必须覆盖,否则 rc-assistant 的跨书导航会把工具库整页带走);SSE 无回放 → 动作先进
  `_simQ` 队列,iframe onload+2.5s(SSE 接上)后统一 flush。
- **沙盒 = 节选副本**(2026-07-17 用户方案,替代整本拷贝):整本 204MB 副本"很长时间加载不出来"
  ——慢不在拷贝,在副本是全新文件,页图/字符层缓存从零渲 + `_maybeAutoPrewarm` 自动预热整本。
  改为**围绕最近阅读位置节选 ~10 页**(fitz insert_pdf,前 2 后 7,锚页=节选内第 pos-start+1 页,
  参数存 `<名>.pdf.meta.json`;书 ≤10 页整本拷)。ctx.page=anchor → CLI"读当前页"读到的仍是用户
  正在学的内容;iframe URL 带 &page=anchor 直接落锚页;♻重置=按当前阅读位置重切。
  实测:204MB→34MB 切 1.5s,iframe 骨架 1.1s、首屏页图 4.1s(原来 30s-2min)。
- **对抗审查修复批**(2026-07-17,workflow 10-agent 全部实锤):沙盒按**源路径哈希分子目录**
  `.sandbox/<h8>/<原名>`(vault 真有同名书 Main.pdf,扁平 basename 会串书;文件名保留原名,阅读器
  标题干净)+ meta.src_rel 复用校验;`_SANDBOX_LOCK` 防并发 check-then-act;fitz 切割 try/finally
  (坏书回 JSON 500、句柄必关、tmp 清理);reset 清边车补便签 `_notes_save` + reader-positions 的
  sb_rel 键(否则 iframe 被旧续读带走不落锚页);publish-actions **只允许沙盒路径**(403 其它,
  消掉任意 fn 注入面);前端:sbEnsure in-flight 去重、send 快照 rel(ensure 在飞期间切书→中止,
  绝不回落原书)、重置无条件失效 _simRel、切书清 _simQ、↗ 新页签带 &page=anchor。
- 验证结论:publish → SSE → iframe 建纸带块渲染,端到端实测通。⚠ 测试时手搓 client_action 载荷
  注意 **paper 是纸型字符串**('exam'),不是 dict——传 dict 曾致 paper.spec `PAPERS.get(dict)` 500
  (已加防御:非 str 落默认纸型),且此前多轮"纸没出现"的假失败全是这个假载荷问题,链路一直是通的。
