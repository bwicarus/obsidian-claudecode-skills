# ADR:造纸编排器 —— AI 现场设计交互页,可保存成工具

日期:2026-07-15 · 状态:**已采纳,待实现** · 提出者:用户

> 前置:`adr-task-runtime.md`(任务运行时的两条铁律 + 按 bbox 裁图批改)、
> `paper.py`(纸张 + 格子布局器,已上线)。本 ADR 在其上再抽象一层。

---

## 1. 背景:硬编码的 `dictation` 是死胡同

`adr-task-runtime.md` 落地后,听写能跑了 —— 但流程(`_dictation_tick`)和纸张(`_paper_blocks`)
都是**我硬编码**的。想要试卷、九九乘法练习、任何别的交互页,就得再写一个程序。不可扩展。

用户提出的方向:**让 AI 现场设计整张交互页(内容 + 按钮 + 按钮按下之后干什么),并能保存成工具。**

---

## 2. 决策总览

```
语音模型  ──①一次委托──▶  create_page(intent: "把当前所有高亮做成听写")
                                    │  它做完这一步就不管了,不被阻塞
后台编排 AI(无头 Claude + 我们的 MCP,复用 _task_agent)
   查高亮 → 提取词 → create_paper → write_blocks → **define_handler**(声明式)
                                    │  每一步挂进流程面板(轮次容器)实时展示
运行时(task_runtime,已有骨架)
   接管按钮事件 → 按 handler 规格确定性执行(裁图 → 检查 CLI → 流式回传)
                                    │
[保存按钮] ──▶ 把 (页模板 + handler 规格) 冻成配方文件 → 变成一个具名工具
```

四个角色,边界必须清:
- **语音模型**:只认识**一个**工具 `create_page(intent)`,**一次委托,零编排**。
- **编排 AI**:读上下文 + 用 MCP 工具查内容 → **造页 + 写按钮处理器**。只在**创建时跑一次**。
- **运行时**:确定性地执行编排 AI 写好的 handler。按钮事件驱动,**LLM 不在循环里**。
- **配方**:保存后的 (页模板 + handler),数据不是代码。

---

## 3. 铁律与那个"一字之差"的陷阱

沿用 `adr-task-runtime.md` 的两条铁律(LLM 不做等待/循环;运行时无阻塞线程)。

⚠ **生死分界**(用户原话"他设计的预定操作"里藏着的陷阱):

- ✅ 按钮事件 → 运行时执行编排 AI **预先写好的** handler 步骤(编排 AI 创建时跑一次)
- ❌ 按钮事件 → **唤醒编排 AI** 重新决定干什么(LLM 进循环 = 烧钱/慢/刷新即丢)

用户的例子(交卷 → 裁图 → 发检查 CLI → 流式回传)是**前者**:路由固定,里面的「检查」
是一次**有界 LLM 调用**(像 `_grade`),不是「等 LLM 决定下一步」。守住这条,铁律不破。

---

## 4. 三道保命栏杆(少一道就变成一堆一重启就碎的动态代码)

### 栏杆① handler 是**声明式规格**,不是代码
编排 AI 从**白名单**里挑步骤填模板,**永远不写可执行代码**。步骤类型(初版):

| step | 语义 |
|---|---|
| `say: "文本" \| {block}` | 念(走已有 TTS 遥控) |
| `capture: "page" \| block_id` | 拿页面/某块的合成图(裁图,see_* 那套) |
| `llm: {action, prompt, stream?}` | 一次有界 LLM 调用(批改/点评);stream=流式回传网页 |
| `write: [blocks]` | 往页里追加块(结果展示) |
| `reveal / hide: block_id` | 显隐某块(看答案) |
| `say_wait / wait: event` | 挂起等某个按钮事件 |
| `wait_ms: n` | 计划内停顿 |
| `set_enabled: {block_id, bool}` | 改按钮可点状态 |
| `loop: {over, do:[...]}` / `branch` | 循环 / 分支 |

**保存/运行前用 schema 校验**:未知 step、悬空 button id、越界引用 → 拒。
否则 = `eval()` 安全洞 + 不可靠。

### 栏杆② "保存成工具" = 写配方 JSON + **一个通用派发器**,不是动态注册 Python
用户要的结果(「一个新具名工具出现,AI 能调」)照给,但实现必须是:
- 配方存 `state/recipes/<name>.json`(结构见下面 §5.5)。
- 语音模型 / MCP 看到的是**一个**通用工具 `run_saved_task(name, params)`。
- **绝不**动态生成 Python 函数、**绝不**往 `TOOLS` / MCP 动态塞工具对象 —— 那一重启就散、还是安全隐患。
- 工具目录是**数据**。"AI 眼里多了个『日语听写』工具" 由一份**配方清单**(注入进
  create_page/run_saved_task 的 description 或单独的 list_recipes)实现,不是真的多一个函数。

### 栏杆③ 造纸任务**永远委托后台编排 AI**,不做"≥2 步检测"
用户担心"听写所有高亮"需要 查高亮 + 造页 两步,而语音模型自己串步骤会出乱。
**不检测步数**(实时模型没法提前知道自己要几步,检测本身不可靠)。规则改成:
> **任何造纸/填页请求 → 语音模型只调一次 create_page(intent),整串多步全在编排 AI 里发生。**

于是「后台版启没启动」的不确定性**天然消失** —— create_page 的定义就是「委托后台 agent」,永远启动。
create_page 的工具说明必须写死:**"整件事交给我,包括需要先查高亮/搜索/读页面的 —— 你别自己先查,把用户要求原样给我。"**

---

## 5. 复用 `_task_agent`(已核实的真实接线)

编排 AI = 现有无头 CLI agent 的一个变体(voice.py:1184 `_task_agent`):
- 起法:`claude -p <prompt> --mcp-config <我们的MCP> --allowedTools mcp__bwapp --disallowedTools <本地文件工具>`
  (voice.py:1130-1134;⚠ `--allowedTools` 只是自动批准名单**不是**能力名单,必须靠 `--disallowedTools`
  真掐 Bash/Read/Write —— `_AGENT_DENY` voice.py:1010,实测 haiku 真去调过 Bash)。
- 派发:`_bg_task(kind, params, ctx)`(assistant.py:1445)→ `_run_task`(voice.py:1247,`_task_sema`
  Semaphore(2))→ 内存任务表 `_vtasks`(1800s TTL)+ `/api/voice/task-status` 轮询。

**要动的**:
- `_VTASK_KINDS`(voice.py:693,现 `note/anki/vocab/search`)**加 `page`**;`_run_task` 的 dispatch map 同步加(漏一个 → 404/KeyError)。
- 编排 agent 的**工具集**要含造纸原语(create_paper / write_blocks / define_handler)—— 这几个要作为
  MCP 工具暴露给无头 agent(它只能用 `mcp__bwapp`)。
- **别复用 `_task_sema`(Semaphore(2) 阻塞排队)**给需要长活的 run —— 编排是有界短任务可以用,
  但按钮事件循环归 task_runtime(零线程),不占信号量。

**关键岔路(实现前要定)**:编排产出的是 **run**(task_runtime 的状态),而 `_vtasks` 是内存表、
1800s 就清、重启即丢。→ **编排结果(页 + handler)必须落进 durable 的 `state/reader-runs/`**
(task_runtime 已是文件驱动),`_vtasks` 只用来展示"编排进度"这个短过程。两者别混。

---

## 6. 展示:每一步都进流程面板
用户要求「创建的每个阶段、用的每个 AI,都尽可能详细地用工具流程界面展示」。
→ 复用 `adr-turn-container.md` 的轮次容器 + 流程面板(tool part):
编排 agent 每走一步(查高亮/出题/造页/写 handler)publish 一个 sub-step;检查 CLI 的流式输出
作为 llm-step 的实时内容。语音模型侧只显示一张卡 + 进度,点开流程看全过程。

---

## 5.5 配方结构 · 去壳 · 保存合并(用户 2026-07-15 设计,本 ADR 的核心)

### 5.5.1 配方三段解耦
```jsonc
{
  "name": "听写",
  "inputs": { "words": {"type": "list"} },      // 参数(被"取消选中"的工具变成这里的参数)
  "sources": {                                   // 可选:自动填某个 input(去壳调一个已有工具)
    "words": { "call": "get_highlights", "extract": "text", "args": {} }
  },
  "page": [ ...模板块... ],                       // 纸(固定)
  "flow": [ ...声明式 step... ]                   // 流程(固定:念 → 等 → 批改)
}
```
**关键洞见**:「高亮听写 / 未掌握词听写 / 随机页听写」的 page+flow **一模一样**,只有 `sources.words.call`
不同(`get_highlights` / `get_weak_words` / `read_page`)。→ 它们是**一个工具的三个数据源**,不是三个工具。

### 5.5.2 可视化框选 = 数据源解耦的界面
在流程面板(§6)上点「💾 保存为工具」→ 流程里用过的每个工具前出现选中点(实心/空心,**默认全选**):
- **保留选中** = 这个工具**去壳烤进**配方(成为一个 `source`,运行时自动取数)
- **取消选中** = 它和它之前的流程**不进新工具** = 它的输出变成新工具的一个 **`input` 参数**(由别人提供)

即:**框选 = 决定每个上游工具是「烤进来自动跑」还是「变成参数」。** 这就是 5.5.1 的手动界面。

### 5.5.3 去壳 = 进程内直接调 TOOLS,不是拷贝代码(⭐ 本 ADR 最省事的一点)
用户担心「烤进来的旧工具还走 MCP 太慢」,又不想要「拷代码 + 标注 + 反向更新」那套负担。**正解天然存在**:

**已核实(2026-07-15)**:我们的 MCP 服务器**全线是 HTTP 转发**(mcp_server.py 用 httpx,
`_post("/api/assistant/tool",...)`,连 read_page/lookup_word 原生工具也转发;注释:"不 import webapp 代码,
只走 HTTP")。最终落点都是 **`TOOLS[name][1](args, ctx)`**(assistant.py 5 处)。

⇒ **「去壳」= 配方运行时不走 MCP、进程内直接调 `TOOLS[name][1](args, ctx)`** —— 调的**就是 MCP 转发到的
同一个函数**。于是:
- **零代码拷贝**(引用同一函数,不复制)
- **零标注、零反向更新**(改了 `get_highlights`,所有烤了它的配方自动跟着变 —— 同一个函数)
- 用户担心的「调旧工具要更新所有用它的地方」这个负担**根本不存在**

去壳的 `source` 运行时:`ctx = {file_rel, page, _uid}` 由 run 现成合成(工具函数签名 `_t_xxx(args, ctx)`),
直接调,拿返回体的 `extract` 字段填进 input。**这是"流程即数据"的自然延伸,让设计更少不是更多。**

### 5.5.4 唯一需要标注的例外:真·外部 MCP
只有当框选里包含**真正的第三方外部 MCP 工具**(代码不在我们进程里)时,才**没法去壳** —— 保留 MCP 调用,
并在配方里标一笔「此步走外部 MCP」(慢,但没别的办法)。**我们自己 TOOLS 里的工具永远不用标。**
框选界面用不同颜色区分「可去壳(本地)」vs「须保留(外部 MCP)」。

### 5.5.5 保存时按结构合并
保存判定:**新配方的 page + flow 结构** 跟已有某配方**一致**(忽略具体数据)→ **不新建工具**,只给它
**加一个数据源选项**。于是用户三次保存 = 一个「听写」工具 + 一个「内容来源」选择器,而不是三个工具。
结构一致性由保存时的编排 AI 判断(它本就要整理保存内容,能理解"这两张纸本质是同一种")。

### 5.5.6 数据流展示(§6 的增量)
流程面板已捕获每个 tool part 的 `args` + `result`。展示「工具间数据流动」= 在节点之间画
「A 的输出 → B 的输入」的线 —— 主要是**展示**工作,不是新采集。

---

## 7. 保存流程
工具卡上一个「💾 保存为工具」按钮:
1. 用户框选要烤进来的工具(§5.5.2,默认全选)+ 填一个工具名。
2. 后台 agent 把 (页模板留占位 + flow + 选中工具 → `sources` 去壳 + 未选工具 → `inputs` 参数) 打包成
   `state/recipes/<name>.json`;**先按 §5.5.5 查能否合并进已有配方**。
3. 之后 `run_saved_task(name, {source: "高亮" | "未掌握词" | ...})` **跳过编排 AI**,
   去壳调数据源工具 → 模板实例化 → 铺纸 → 起状态机。**秒开、零 LLM、确定性。**

**性质(本 ADR 最漂亮的一点)**:第一次是 AI 现写(慢、灵活);保存后是模板实例化(快、确定)。
编排 LLM 的成本**只付一次**,之后摊薄到零。这就是用户要的「设计好固定任务后保存成高级工具」。

---

## 8. 明确不做
- **不让编排 AI 写可执行代码**(栏杆①)。
- **不动态注册 Python 工具 / 动态改 MCP 工具表**(栏杆②)。
- **不做"≥2 步检测路由"**(栏杆③,改成"造纸永远委托")。
- **不让按钮事件唤醒编排 AI**(§3 的陷阱)。
- **不拷贝旧工具的代码进新工具、不搞"标注 + 反向更新"**(§5.5.3:去壳 = 进程内调同一个 TOOLS 函数)。
- 初版**只做 PDF**(EPUB 插入页坐标系不同,`adr-task-runtime.md` §3.3 已记)。

---

## 9. 分阶段
- **阶段 A(已上线)**:paper.py 格子布局器 + task_runtime 骨架 + 硬编码 dictation。
- **阶段 B(已上线)**:make_page 工具 + 按钮块 + 内置动作(check/say/reveal/hide/goto)+ 通用 _check_page
  —— AI 能造**静态纸 + 内置动作**,"AI 出卷 + 让 AI 检查"不需编排 AI 即可用。
- **阶段 C(已上线)**:声明式配方解释器(flow 白名单 + validate_flow + _recipe_tick pc 挂起恢复
  + repeat 块展开 + 模板变量)。dictation 用纯 JSON 重写,证明"流程即数据"。
- **阶段 D(下一步)**:create_page 编排器(复用 _task_agent 无头 CLI + 造纸 MCP 原语)+ 流程面板展示;
  编排产出落 durable `state/reader-runs/`(§5 岔路)。
- **阶段 E**:保存流程 —— 可视化框选(§5.5.2)+ 去壳 sources(§5.5.3,进程内 TOOLS 调度)
  + 按结构合并(§5.5.5)+ run_saved_task 派发器。

「AI 出卷 + 让 AI 检查」已能用(阶段 B 的内置 check);D/E 把它变成「AI 造任意交互页 + 一键固化成工具」。

---

## 10. 相关
- `adr-task-runtime.md` / `adr-turn-container.md` / `unified-control-layer.md`
- `paper.py`(格子布局器)· `task_runtime.py`(状态机)
- memory `sse-thread-starvation`(运行时为何无阻塞线程)· `tool-output-quality-beats-model.md`
- voice.py `_task_agent`(编排 agent 复用点)· assistant.py `_bg_task`
