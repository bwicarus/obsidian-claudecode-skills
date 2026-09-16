# Codex 集成参考（同一 CLI 的 exec 与 app-server）

> 来源:GPT 整理的官方接口说明(2026-07,用户提供)+ 本项目实测。⚠ **GPT 转述部分未逐条验证**
> (模型名/事件名可能有出入),动手前以 `codex --help` / `model/list` 运行时结果为准。
> 已实测打 ✅;其余标 ⚪(转述,待验证)。

## 0. 本项目现状(2026-07-11)

- ✅ **已上线(v2,2026-07-11)**:主路=**常驻 `codex app-server`**(assistant.py `_CodexApp` 单例,
  JSON-RPC over stdio):进程死亡自动重启;每次调用开 **ephemeral thread**(不落盘、任务间零污染)+
  turn/start;**真文字 delta 流式**(reader_stream 的 codex 分支逐字吐);并发按 threadId 路由。
  失败回落 `codex exec` 一次性(`_codex_exec_text`,独立退路)。
- ✅ **实测 schema 修正(GPT 转述有出入的地方)**:sandbox 枚举=`read-only`(非 readOnly);
  `model/list` 真实清单=gpt-5.6-sol/terra/luna(effort 到 max/ultra)、gpt-5.5、gpt-5.4(-mini)——
  **gpt-5.5-codex 在 app-server 下 400 不可用**(exec 下可用,别名路由);turn/start 可带 effort。
- ✅ **v3 提速(2026-07-11,GPT 诊断方案实施)**:独立干净 `CODEX_HOME=~/.reader-codex/home`
  (精简 config:features 全关 apps/hooks/goals/memories/multi_agent/remote_plugin/shell_tool/
  shell_snapshot/unified_exec/personality + web_search=disabled + history.persistence=none)+
  空 untrusted cwd `~/.reader-codex/empty`。效果:**thread/start 0.8s→0.05s、turn→首delta 3.3-4.7s→
  1.4-1.8s、热调用 5.9s→2.1s**(agent 周边初始化=原延迟大头,GPT 判断正确)。环境由
  `_codex_rc_bootstrap()` 自举(auth 从 ~/.codex 拷,0600)。thread 预创建池不需要了(0.05s 可忽略)。
  ⚠ 配置陷阱(实测):`features.fast_mode` 键非法、`[mcp_servers.X] enabled=false` 覆盖语法非法
  (报 invalid transport)——**任一非法键=整份配置静默回默认**,改完必须看 configWarning/RUST_LOG。
  默认模型改 gpt-5.6-luna+low(官方定位:清晰重复的提取/转换/摘要=阅读场景)。
- 定位:**只当纯文本/看图模型用**(read-only + approvalPolicy never + 主路 cwd=`~/.reader-codex/empty`,见 assistant.py `_CODEX_RC_CWD`:1401 与 `_CodexApp.thread_start`:1695;⚠ 只有 `codex exec` 兜底那条仍 `cwd="/tmp"`,见 `_codex_exec_text`:1817),不让它当 agent;
  编排循环现已接入,见 §6。

## 1. 四种集成方式(升级路径)

`app-server` 不是取代 `codex` 的另一套程序；它由同一个 CLI 二进制通过
`codex app-server` 启动。与通常所说的“原来 CLI”相比，真正需要区分的是交互式 TUI、
一次性自动化入口 `codex exec`，以及供自制客户端连接的长驻协议入口 `codex app-server`。

| 对比 | `codex exec` | `codex app-server` |
|---|---|---|
| 生命周期 | 一次命令/一次任务，完成即退出 | 长驻进程，可承载多个 thread/turn |
| 接口 | prompt + stdout/stderr；可选 JSONL | 双向 JSON-RPC，稳定 transport 为 stdio |
| 状态 | 默认单次；可用 `resume` 显式恢复 | thread CRUD/fork/archive，进程内可直接多轮 |
| 流式 | 进度/结构化 item 事件 | 文本 delta、item、工具、审批、turn 状态等细粒度事件 |
| 运行中控制 | 通常等待或终止进程 | `turn/steer`、`turn/interrupt`、审批响应 |
| 适合 | shell 脚本、CI、一次性后台任务 | PWA/桌面端/扩展配套的富客户端服务 |

两者使用同一 Codex 登录和模型能力；`app-server` 的优势是协议、状态与控制能力，不代表
模型更聪明，也不会把 ChatGPT 高级语音订阅自动变成可调用的 Realtime API。

官方入口：
[Codex App Server](https://learn.chatgpt.com/docs/app-server)；
[Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)。

| 方式 | 场景 | 流式 | 会话 | 备注 |
|---|---|---|---|---|
| `codex exec` | 脚本/CI/一次性 ✅ 现用 | `--json` JSONL 事件级(非文字 delta) | `resume --last / resume <id>` ⚪ | 官方不建议做长期 API 层 |
| `codex mcp-server` | 另一个 AI 调 Codex ⚪ | 由 MCP 客户端 | `threadId` 续会话 | 只暴露 `codex` + `codex-reply` 两工具 |
| SDK(`@openai/codex-sdk` / `pip openai-codex`)⚪ | 后端程序集成 | 结构化事件流 | thread 恢复(`~/.codex/sessions`) | Python SDK beta |
| `codex app-server` | 自制客户端/最完整 ✅ 官方文档+本机实测 | **真文字 delta**(`item/agentMessage/delta`) | thread 全套 CRUD+fork | JSON-RPC;stdio 稳定,ws 为实验/unsupported |

**何时升级**:①要 codex 做多轮/编排(工具循环)→ mcp-server 或 app-server(threadId 续用,
不重拼历史——服务端会话与 Anthropic 前缀缓存同解);②要流式打字机体验 → 只有 app-server 有
文字 delta;③一次性问答 → 留在 exec,够用。

## 2. 关键 API 速查(⚪ GPT 转述)

- **MCP 工具**:`codex {prompt, cwd, model, sandbox, approval-policy, …}` → 返回
  `structuredContent.threadId`;续聊 `codex-reply {threadId, prompt}`。
- **app-server 最小流程**:`initialize`→`initialized`→`thread/start {model,cwd,sandbox,
  approvalPolicy}`→`turn/start {threadId,input:[{type:"text",text}]}`;听
  `item/agentMessage/delta`(文字增量)/`turn/diff/updated`/`turn/completed{status:
  completed|interrupted|failed}`(**以此判结束,别靠静默超时**);中途 `turn/steer` 追加指令、
  `turn/interrupt {threadId,turnId}` 中止(⚠ **两个 ID 都是必填**);`thread/fork`
  分叉对比方案;`model/list` 动态拉型号+
  `supportedReasoningEfforts`(**别写死档位清单**)。
- **结构化输出**:exec `--output-schema schema.json` / SDK `outputSchema` / turn 参数
  `outputSchema` ——要机器可读结果就用 JSON Schema,别解析自然语言。
- **exec 补充**:`--json` 事件流(thread.started/item.completed/turn.completed 带 usage);
  `resume --last "继续…"` 续最近会话;`--ephemeral` 不落盘;进度走 stderr、答案走 stdout。
- **图片**:exec `--image a.png,b.png` ✅(-i 实测);SDK `{type:"local_image",path}`。
- **本地模型**:`codex --oss --local-provider ollama|lmstudio` ⚪ —— M4 Mac mini 本地推理
  规划的备选入口之一。

## 3. 沙盒与安全底线

- 沙盒三档:`read-only`(✅ 我们用)/ `workspace-write` / `danger-full-access`;
  审批三档:`untrusted / on-request / never`。**两者独立**:自动化场景=
  `approval-policy never + sandbox workspace-write`(不等人但锁在工作区)。
- `read-only` 是**禁止写入**,不是禁止读取;默认 read access 仍可能是 full access。无密钥
  协议探针把 `cwd` 设为空临时目录,只会降低模型偶然发现项目内容的概率,不能阻止绝对路径、
  父目录或用户配置允许范围内的读取。不要把它写成“零文件读取权限”。
- `--yolo`(=bypass approvals+sandbox)只允许在容器/VM 隔离环境;网络默认关(`--search`
  开实时搜索);多个并行 Codex 各用独立 worktree;app-server 对外必须认证+TLS。

## 4. 给调用方(我们的 relay/assistant)的约定要点

- 请求带全:目标/工作目录/能否改文件/是否跑测试/禁触目录/期望格式/成功标准。
- 保存 `threadId`+工作目录+模型,后续**续 thread 而非重塞历史**。
- 完成状态只认 `turn/completed status=completed`;failed/interrupted 不得报成功。

### Windows Codex Voice 的 Reader 学习卡工具

Direct 的 Reader MCP 暴露五个 canonical 学习卡工具：`reader_learning_cards`、
`reader_learning_card_read`、`reader_learning_card_edit`、`reader_learning_card_delete`、
`reader_review_current_card`。三条读取查询在页面执行侧分别映射到 `learning-cards`、
`learning-card`、`review-current`；列表/单卡结果直接带完整卡片内容、出处、学习状态、稳定
`card_*` ID、批内 `cardIndex` 与当前 revision，模型不必先读页面 placement 或逐卡补查。

编辑必须提交 `id + cardIndex + expectedEntityRevision`，并至少给出 `card` 或 `source`；
`card` 只替换该批内 index 的语义内容，`source` 是从读取结果取得的完整批级出处对象，替换后
同一 `card_*` 的所有卡共同使用新出处。两者可在一次原子写中同时修改，也可只补旧卡出处，
不会新建卡片。删除必须提交
`id + cardIndex + expectedStateRevision`；严格字段、版本和稳定身份检查通过后，Direct 才发送
`_nativeReaderLearningCardMutate`，并回读同一 canonical 卡验证结果。默认
`externalPolicy=sync-if-projected`：Reader 本地仓是权威，已有 Windows 投影走本机
`anki-card-operation-local`，已有 Pi 投影走受保护的 `/pdf/api/anki-card-operation`；成功修改后
请求 AnkiWeb sync；批级出处变更会按各 index 保存的精确 note ID 更新该批所有已有投影的
出处 footer/marker，并保留原 note 身份和复习调度。结果把 `reader_applied`、
`anki_local_applied`、`anki_web_sync` 分开，未知
结果禁止自动重试；删除 Anki 投影是 note 级。`reader-only` 只改 Reader，AnkiMobile 没有可靠
按 ID 写通道时 fail closed。Direct 工具面更新后须新开 Codex Voice 任务才能载入新列表。

## 5. Realtime 语音探测(2026-07-11 初测;2026-07-25 复测)

- ✅ 0.144.1 接口真实存在(`codex app-server generate-json-schema` 为准):`thread/realtime/start|appendAudio|appendText|appendSpeech|stop` + transcript/outputAudio 事件;**transport 有 `websocket` 型**(不需要浏览器 WebRTC,纯服务端可接,GPT 说明书没提);Schema 有 **RealtimeVoice 19 音色枚举**(alloy/cedar/marin/sage…,说明书说"无 voice 字段"是错的);音频块 = {data, sampleRate, numChannels}。
- ✅ 前置开关:feature **`realtime_conversation`**(underDevelopment,默认关)——不开报"thread does not support realtime conversation";`experimentalFeature/list`(带 cursor 翻页)可拉全部 90+ features 现状。已在 `~/.reader-codex/home/config.toml` 开启(无副作用)。
- ❌ **认证卡死**:`thread/realtime/error: "realtime conversation requires API key auth"`——**ChatGPT 订阅登录不行,必须 OpenAI Platform API Key(独立按量计费)**。说明书"认证方式=已登录 ChatGPT 账号"实测为错。
- 判断:当前 ChatGPT 登录边界已经足够否决“直接拿订阅高级语音替换现有 Realtime API”
  的方案。若以后认证边界改变,再按当时官方价格、模型、音频 token 口径、延迟和质量重新
  对比;本文不保存容易过期的价格数字。实验接口初始化需
  `capabilities.experimentalApi: true`。

### 可复跑的无密钥协议探针(2026-07-25)

入口:`scripts/codex_appserver_probe.py`。它只使用**现有 ChatGPT/Codex 登录**。它仍会
加载现有 Codex 本地配置,因此只应在**可信的本机配置和可信的 `--codex-command`**下运行;
这不是隔离 hooks、MCP、Apps 或文件读取的安全边界:

- 不打开/复制/打印 `auth.json`,也不索要或写入 API Key;
- 启动子进程前剔除 `OPENAI_API_KEY` / `AZURE_OPENAI_API_KEY` / `CODEX_API_KEY`
  等 API-key 环境变量;若 `codex login status` 不是 ChatGPT 登录则跳过 live 测试;
- 在空临时目录中创建 `ephemeral + read-only + approvalPolicy=never` thread;空目录只降低
  偶然读取项目的概率,`read-only` 不禁止读取;
- 不打印 raw RPC/stderr/模型正文,只输出白名单化的能力和结果字段;
- 审计模型产生的已知工具 item(`mcpToolCall` / `commandExecution` / `fileChange` /
  `dynamicToolCall` / `collabAgentToolCall|collabToolCall` / `webSearch` / `imageView`),最终
  断言数量为 0。`mcpServer/startupStatus/updated` 只是 MCP 启动状态,不算工具调用;
  用户配置里的自动 hook 不属于这项审计,所以“0”不能解释成“隔离了用户配置”;
- Realtime 探针不发送任何音频;若未来意外接通会立刻请求
  `thread/realtime/stop`,分别记录停止请求是否被接受、是否收到
  `thread/realtime/closed`;只有两者都成立才记 `safe_stop=true`。

复跑:

```bash
# 当前安装版
python3 scripts/codex_appserver_probe.py --timeout 120

# 精确对照 0.145.0(不替换本机安装)
python3 scripts/codex_appserver_probe.py \
  --codex-command "npx -y @openai/codex@0.145.0" --timeout 120

# 只看 help/schema/login,完全不发模型 turn
python3 scripts/codex_appserver_probe.py --schema-only
```

同一 ChatGPT 登录的 live 实测矩阵(2026-07-25 留档):

| 能力 | 本机 0.144.1 | `npx` 0.145.0 |
|---|---|---|
| `initialize` / ephemeral `thread/start` | ✅ | ✅ |
| 最小文字 turn | ✅ `CODEX_PROBE_OK`,completed | ✅ `CODEX_PROBE_OK`,completed |
| 运行中 `turn/interrupt {threadId,turnId}` | ✅ `interrupted` | ✅ `interrupted` |
| 缺 `turnId` 的 interrupt | ✅ 被拒,`-32600`,归类 invalid_params | 同左 |
| 未知方法错误路径 | ✅ 被拒,`-32600` | 同左 |
| Realtime 方法 | start/appendAudio/appendText/appendSpeech/listVoices/stop | 同左 |
| Realtime transport | WebSocket + WebRTC | 同左 |
| Realtime 协议版本 | V1/V2 | V1/V2/**V3** |
| `initialItems` / `codexResponseHandoffMode` | ❌ / ❌ | ✅ / ✅ |
| ChatGPT 登录调用 Realtime | ❌ V2:`requires API key auth` | ❌ V3:`requires API key auth` |
| `codex exec` 的文字/自动化 flags | JSONL、图片、output-schema、`-o`、ephemeral、resume 均有 | 同左 |
| `codex exec` 音频/Realtime flag | **没有** | **没有** |

两次 live 记录当时均 `passed:true`(耗时受当次 Pi 状态与网络影响,不作为性能基准)。
此后只收紧了已知工具 item 审计、安全字段断言及“收到 Realtime closed 才算安全停止”的
判定;没有为了文档重复消耗模型 turn。当前代码的离线聚焦测试与 `--schema-only` 已通过。
`evaluate()` 会同时核验报告中的空临时目录、`read-only`、ephemeral 请求标志和服务端
返回的 `ephemeral=true`,以及文字 marker 精确匹配、有效/无效 interrupt、未知方法错误
路径和已知模型工具 item 为 0。这里核验的是探针自身设置与事件报告,不是操作系统级文件
读取隔离。Realtime 的认证失败按错误结构分类,
不依赖服务端完整英文措辞。若未来 Realtime 成功启动,结果记为
`capability_available`;停止请求被接受记 `stop_request_accepted=true`,只有随后收到
`thread/realtime/closed` 才记 `safe_stop=true` 并允许整次探针通过。
0.145.0 的新增点是实验性 **V3/Frameless Bidi 协议字段**,不是把 ChatGPT 高级语音
订阅开放给 CLI;认证边界没有变化。

⚠ `RealtimeVoice` schema 总枚举有 19 个音色,但它**没有表达每个协议版本的兼容子集**。
探针只从 schema 的版本/transport/voice 交集中选择,版本优先级显式为
`V3 > V2 > V1`;当前 V2 优先 `marin`,V3 优先 `juniper`。不要因为总枚举含某音色就
假定它能用于每个版本。

✅ 生产 `_CodexApp.turn_stream()` 已保存 `turn/start` 返回的 turn ID;超时后使用 RPC
`turn/interrupt {threadId,turnId}` 安全取消,不再发送缺 `turnId` 的 notification。
`tests/test_codex_appserver_client.py` 用假 RPC 覆盖超时、缺 turn ID 和正常完成路径,
不启动真实 Codex 或模型。

## 6. Codex 编排循环(㉖,2026-07-11,用户拍板接入)

- ✅ **orchestrator 三后端全通**:`_agent_run_codex`(assistant.py)= app-server **threadId 多轮会话**——
  `_CodexApp` 拆出多轮原语 `thread_start / turn_stream(tid, text) / thread_close`(stream() 改为单轮便捷壳);
  每轮只发新内容(【工具结果】…),**服务端保存历史不重拼**(与 Anthropic 前缀缓存同解,§4 的约定落地)。
- ✅ **ephemeral thread 可多轮**(实测):ephemeral 只是不落盘,thread 活在 app-server 进程内存——两轮记忆
  冒烟(轮1 记暗号/轮2 正确回出)+端到端编排冒烟(真调 search_all_books→结果喂回→合成回答+FOLLOWUP 格式全守)。
- **驯服编程 agent 本性三重锁**:read-only 沙盒 + 空 untrusted cwd + 首轮 prompt 明令"不要用内置 shell/文件工具
  (空目录什么都没有),JSON 工具协议是唯一工具通道"。实测服帖。
- **vision 工具**(see_page 等):turn 输入 localImage 在多轮语境未验证 → 稳妥路径=图先经 `_vision_for`
  (用户 vision 预设的模型)转文字再喂回;后续可实测 localImage 直喂。
- 兜底:thread 起不来/首轮无响应(未调工具前)→ 自动回退 `_agent_run_claude`(fallback_from 标注);
  调过工具后失败→报错(thread 内上下文无法迁移)。
- 事件语义:与 claude/gemini 编排完全一致(answer=轮内全量/tool/tool-done/actions/task/undo/trace)。

## codex exec 调「远程 HTTP MCP 工具」:**已打通**(2026-07-14 实测)

**结论:通了,而且走的是 ChatGPT 订阅额度(plan_type=plus),不是 API Key 计费。** codex 可以当白嫖额度的 MCP worker。

### 唯一的关键:`default_tools_approval_mode = "approve"`

单变量隔离实测(其余配置完全相同):

| 配置 | 结果 |
|---|---|
| 只加 `default_tools_approval_mode="approve"` | ✅ 成功(拿到真实数据) |
| 不加它 | ❌ `user cancelled MCP tool call` |
| 加它 + `features.shell_tool=false` | ✅ 成功 |

**根因**:`~/.codex/config.toml` 里的 `approval_policy = "on-request"` + `approvals_reviewer = "user"` 让 **MCP 工具默认需要人工审批**;无头 `codex exec` 没有人能批 → 立刻自动取消。CLI 打印的 `user cancelled MCP tool call` 措辞极具误导性(并没有人取消),而 debug 日志里的 `SSE stream disconnected / hyper::Error(IncompleteMessage)` + `turn aborted reason=stream_disconnected` **只是取消后 turn 被 abort 的下游现象,不是根因**。

⚠ cookbook `articles/codex_mcp_tools` 里那句「exec 模式 MCP 工具 auto-approved」**与实际不符**(至少 0.144.1 + 用户 config 有 `approval_policy=on-request` 时不成立)。别信它,显式写 `default_tools_approval_mode="approve"`。

### 不需要的东西(实测排除,别浪费时间)
- ❌ **不需要 ChatGPT 网页端「开发者模式」**(开了没用;它当时把「秒放弃」变成「重试」纯属巧合/灰度)
- ❌ 不需要 `--ignore-user-config` / `--ephemeral` / `--strict-config`(保留用户配置照样成功)
- ❌ 不需要 `experimental_use_rmcp_client`(0.144.1 已内置 streamable HTTP client,该字段已过时,`--strict-config` 下会报未知字段)
- ❌ 不需要改我们的 MCP 成 stateless(stateful 正常工作)
- ❌ 不是沙箱问题(Claude Code 的 Bash 沙箱内外都成功)
- ❌ 不是工具集大小(`enabled_tools` 收窄与否都一样)

### 可用配置(实测通过)

```bash
export BWAPP_TOKEN='<token>'
codex exec --skip-git-repo-check --color never -s read-only \
  -c 'model="gpt-5.6-terra"' \
  -c 'model_reasoning_effort="low"' \
  -c 'mcp_servers.bwapp={url="https://bwicarus.space/mcp",bearer_token_env_var="BWAPP_TOKEN",required=true,enabled_tools=["list_books"],default_tools_approval_mode="approve",startup_timeout_sec=20,tool_timeout_sec=60}' \
  -c 'features.shell_tool=false' \
  '只调用一次 bwapp/list_books,然后输出前三本书的标题。'
```

- `features.shell_tool=false` — **安全底线**:worker 只能调 MCP,不能跑 shell(语音驱动的 worker 必须加)
- `required=true` — MCP 初始化失败直接退出,不静默降级
- `enabled_tools=[...]` — 按任务收窄工具面(可选;cookbook `articles/codex_mcp_tools` 讲的白名单,默认全暴露)
- `-o <file>` — 最后一条消息写文件(取结果用)

### 模型(ChatGPT 账号目录,`codex debug models` 可查)
`gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.5` / `gpt-5.4` / `gpt-5.4-mini`。
⚠ `gpt-5.1` / `gpt-5-codex` **不在目录里**,用了会报 `model is not supported when using Codex with a ChatGPT account`(那个 400 跟 MCP 无关)。
建议:一般 MCP 编排 = `gpt-5.6-terra` + `low`;简单重复批量 = `gpt-5.6-luna` + `low`;复杂判断 = `gpt-5.6-sol`。**高 reasoning effort 会明显变慢且爱画蛇添足**(实测它在工具返回空时会自己去翻本地文件)。

### ⚠ 操作坑
`codex mcp get` 等**交互子命令**在无 TTY 环境会卡在 `Reading additional input from stdin...`,Ctrl+C 后会**污染整个工具执行层**(后续所有 Bash/Read/Glob 返回空,只能重开 session)。**调 codex 一律 `< /dev/null` + `timeout` 双保险**;`codex exec` 传了 prompt arg 则不读 stdin,安全。

## app-server 方法实探（2026-09-16，160 个方法逐条试出来的）

问的是「有没有比 `thread/inject_items` 更好用的注入通道」。结论：**没有**。
下面每一条都在真机上跑过，不是看名字猜的 —— 名字最像的那几个恰恰都不能用。

### 能用，已经接进来

| 方法 | 用途 | 接在哪 |
|---|---|---|
| `thread/items/list` | 按 threadId 精确取内容，**带 turnId** | `voice_trace._text_lane` 优先走它，取不到才退回啃 `rollout-*.jsonl` |
| `thread/read` | 线程的 model / reasoningEffort / preview | 链路页对话行的副标题 |
| `thread/compact/start` | **就地把历史折成摘要**，对话身份不变 | `/thread/compact` 端点 + 闲时自动压（`threadAutoCompact`，阈值 `threadCompactItems`） |
| `thread/name/set` | 重命名（`rename` / `setTitle` / `update` 都不存在） | `/thread/rename` |
| `thread/delete` | 删除 | `/thread/delete`，删当前那条会先停会话再换新线程 |
| `turn/steer` | 把内容挂进**正在跑的那一轮** | `/thread/steer` + `_ctx_inject_backend`，默认关，见下 |

`thread/compact/start` 是「线程只增不减」的正解。在它之前只能「长到一定程度就开新对话」，
代价是上下文整个丢掉；压缩保住对话身份，实测能把 items 压到个位数。

### 试过不能用（省得下次再试一遍）

- **`thread/goal/set` / `goal/get` / `goal/clear`** —— 名字最像「可替换的状态槽」，实际建不出来：
  set 报 `cannot update goal for thread …: no goal exists`，`thread/start` 里带 `goal` 被静默忽略
  （`thread/start` 不拒绝未知字段），`goal/get` 恒为 `null`，模型也从来看不见。
- **`thread/metadata/update`** —— `name` / `tags` / `title` / `custom` / `extra` / `archived` / `goal`
  全部被拒为 `must include at least one field`；且元数据本来也不进模型上下文。
- **`thread/queue/add`** —— 内容**确实到得了模型**，但语义跟我们要的正好相反：
  ① 一轮只消费队首**一条**（连排三条状态，后两条直接蒸发）；
  ② 空闲时排队会**自己起一轮**（白烧一轮）；
  ③ `thread/queue/delete` 返回 `deleted:false`，撤不回来。
  传「最新选中」要的是后盖前，它是先进先出，比现有的 `inject_items` 退步。
  它唯一合适的位置是「让文字模型自己跑一件事」（通知/提醒），那种场合本来就该独立成一轮。

### `turn/steer`：唯一的真空，但默认关着

`inject_items` 是往线程上追加，**已经开跑的那一轮不会回头读** —— 所以「后台正在干活时
用户改了选中」今天只能等它跑完再补。`turn/steer` 能把内容挂进在跑的轮
（实测以 `userMessage` 落在该轮里，下一轮也记得）。

⚠ 但它有概率把那一轮**打哑**：一条回答都不产出。在语音里就是「AI 不理我」，比不插还糟。
试过换措辞（被动状态通报 vs 祈使句）—— **不是措辞的问题**，两种都出现过哑和不哑。
所以 `turnSteerEnabled` 默认 `False`，端点留着可手动验证。要开之前先把打哑的条件找出来。

### 参数表怎么白嫖

app-server 的 serde 报错会把缺的字段名说出来，比翻文档快：
传个空 params 过去，`Invalid request: missing field \`expectedTurnId\`` 就是答案。
方法名写错时它会把**全部 160 个合法方法名**列在 `unknown variant` 错误里 —— 这份清单就是这么来的。

### `skills/list` 是目录查询，不是「模型看到什么」

链路页加 skill 那一块时差点算错一笔账：经运行器查 `skills/list` 列出 95 个 skill、
名字加描述近 2 万字，看起来像是每轮都在烧的隐形成本。

实测不是：同一台机器起两个 app-server，一个裸起、一个带 `plugins."x".enabled=false`
封存 8 个官方插件，`skills/list` **都是同样的条数**（36/36，封掉 0 个）。
它反映的是安装了哪些，不是本次会话启用了哪些。

所以：**别拿 `skills/list` 的条数推算上下文成本**。要知道模型实际看到什么，
去量线程里那条 developer 消息 —— SLIM_PLUGINS 省下的 25.6K/轮就是那么量出来的，
那个数仍然作数。链路页那一块的标题写的是「skill 目录（≠ 本次会话实际启用）」，
就是为了不让下一个人照着它算账。

## 跟官方 API 的对应关系（2026-09-16 查证）

用户问得对：app-server 的**数据形状**基本就是官方 API 的，查官方文档确实能少猜很多。
但**方法面**是 Codex 自己的，官方文档解释不了 —— 分清这两层能省很多冤枉路。

### 相通的部分（查官方文档有用）

| 我们看到的 | 官方对应 |
|---|---|
| `thread/inject_items` 的 `{type:"message", role, content:[{type:"input_text"}]}` | Responses API 的 input item 格式 |
| 条目类型 `agentMessage` / `reasoning` / `mcpToolCall` / `webSearch` / `commandExecution` | Responses API 的 output item 类型 |
| `turn/started` → `item/completed` → `turn/completed` 事件序列 | Responses 的 `response.*` 流式事件 |
| `thread/realtime/appendAudio` / `appendText` / `listVoices` | Realtime API 的 `input_audio_buffer.append` / `conversation.item.create` 等 |

**当前用的模型**：语音侧 v3 = **GPT-Live**（全双工，能边听边说，边干活边继续对话）；
后台 = **gpt-6-astra**（`thread/read` 报的就是这个）。所以语音那半查 GPT-Live 与
Realtime 文档，后台那半查 Responses 文档。

### 官方有、Codex 没往外接的（这才是我们缺的那两样）

- **`conversation.item.delete`**（Realtime）「Removes any item from the conversation history」——
  这正是「注入只增不减」缺的那一半。162 个方法里**没有**任何 `thread/realtime/delete*`。
- **`session.update` 改 `session.delegation.responses.instructions`**（GPT-Live，Responses delegation）
  ——不重开会话就换掉后台指令，是个**可替换的槽**。Codex 这边 `thread/settings/update`
  **对任何字段都返回 `{}` 然后什么都不做**（连瞎编的字段名都「成功」，模型始终答旧暗号）；
  `turn/settings/update` 则严格拒未知字段，白名单里只有 model/effort/summary 这些，没有指令。
- **`session.instructions.append` / `thinking.append` / `commentary.append`**（GPT-Live，
  client delegation，都带 `delegation_id`）—— 在**委托发生那一刻**挂上下文，
  正是「在文字 AI 开工的一瞬间注入最新内容」。app-server 没有对应方法。

结论：要用上这三样只能绕开 Codex 直连 GPT-Live，那就从订阅额度变成按 token 付费 ——
正是当初套 CLI 的经济学理由。所以现阶段只能在 app-server 给的面里凑合：
忙碌时压在本地只留最新（见 voice_cli_runner 的 `_ctx_flush_pending`）+ 定期 compact。

### thread/queue 到底能不能当「可替换的状态槽」

能，但只有一格：**后台正在跑一轮**时，排队项会老实待着，`queue/update` 可以就地改写，
实测连改两次后只有最新那条进了历史，两条过期的从未出现。
**空闲时不行** —— 一进队就立刻被消费并自己起一轮，`update`/`delete` 全返回
`queued submission not found` / `deleted:false`。
即便在能用的那一格也没采用：那条排队项**会单独起一轮**，多花一轮 token 只为说句「已收到」；
压在本地效果相同且零成本。

另外两个「按轮撤」的方法记在这：`thread/rollback` 要 `numTurns`，
`thread/revert` 要 `beforeTurnId` —— 都是整轮粒度，而 inject_items 的条目不属于任何轮
（它们连 `thread/items/list` 都不出现），所以撤不掉。

## 上网查过之后的结论（2026-09-16）

用户让去查「这些紧缺能力有没有人讨论过怎么在 CLI 里用」。查到了官方文档、
官方仓库的 issue/PR，以及别人踩同一个坑的记录。三件有用的、一件危险的。

### ⚠ 危险：`thread/compact/start` 会**销毁落盘记录**

[openai/codex#44363](https://github.com/openai/codex/issues/44363)（开着，无维护者回应）：
压缩会**就地重写** `rollout-*.jsonl`，把完整记录换成摘要。报告里 851MB／122877 条
被压成 7.1MB／762 条，3777 条助手消息全丢，而且「Loss is silent. No warning before,
no error after.」

对我们尤其要命，因为那份文件正是链路页与历史的唯一来源。所以：
`threadAutoCompact` **默认关**，手动压缩前 `_rollout_backup()` 先复制一份
`.pre-compact-<时间>.jsonl`，界面的确认框明说「会重写磁盘上的完整记录」。

### 官方文档确认的（不用再探了）

- `thread/inject_items` —— 「persisted to the rollout and included in subsequent
  model requests」，且**没有**删除或替换机制。跟实测一致。
- `turn/steer` —— 往在跑的轮追加 user input，必须带 `expectedTurnId`，
  不接受轮级覆盖（model/cwd/sandboxPolicy/outputSchema）。
- `thread/settings/update` —— **只认 `disabledPluginIds`**。这解释了为什么它对我试的
  七个字段全部返回 `{}` 却什么都不做。⭐ 反过来说这条是能用的：插件面可以按线程热切换，
  不必像现在这样只能在 app-server 启动时用 `-c` 封存；`turn/start` 也收这个参数，
  意味着还能**按轮**切。
- `thread/rollback` 在上游**已从 API 移除**（改用 `thread/revert`）——
  我们这个 Windows 构建还留着，别指望它长期存在。

### Codex hooks：正是这个用途，但在 app-server 下不跑

Codex CLI 有正式的 hooks 框架，11 个事件，`UserPromptSubmit` 的处理器返回
`{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"…"}}`，
文档说它「added as extra developer context」——**正是「开工那一刻注入现算的最新状态」**。

配置形状（照本机 `~/.codex/hooks.json` 的真实写法，**双层 hooks**）：

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [
  {"type": "command", "command": "<解释器> <脚本>", "timeout": 30}]}]}}
```

⭐ 而且可以用 `-c` 传，**只对我们这条 app-server 生效**，不碰用户全局配置：

```
codex -c 'hooks.UserPromptSubmit=[{hooks=[{type="command",command="…",timeout=30}]}]' app-server
```

这样挂上去 `hooks/list` 会显示 `enabled=true`（仓库级 `.codex/hooks.json` 则要走信任流程，
config.toml 里 `[hooks.state.'<key>']` 存 `trusted_hash`/`enabled`，默认不生效）。

**但它对 app-server 驱动的轮完全不触发。** 2026-09-16 实测：一次挂上
UserPromptSubmit / PreToolUse / PostToolUse / SessionStart / Stop 五个事件，
`hooks/list` 全部 `enabled=true`，跑一轮真的执行了命令（条目里有 `commandExecution`），
**处理器一次都没被调用**。hooks 是交互式 CLI 的特性。
（旁证：本机 `~/.codex/hooks.json` 里那两条 reader-registration-hook 也是 `enabled=false`。）

### 别人踩的同一个坑

- [agentscope-ai/QwenPaw#7211](https://github.com/agentscope-ai/QwenPaw/pull/7211)
  的问题描述跟我们几乎一字不差：注入的请求级上下文被当成用户消息持久化，
  「Each turn adds another stale context block… potentially mixing old page state into
  later requests」。他们的修法是给注入消息打标记，在内存与落盘两处按标记剔除 ——
  **必须改 agent runtime**，我们用官方二进制，做不了。
- [openai/codex#23218](https://github.com/openai/codex/issues/23218) 任务之间清上下文、
  [#19829](https://github.com/openai/codex/issues/19829) 会话内清上下文 —— 都还是开着的需求。

结论没变：在 app-server 这个面里，注入只能追加、不能撤。现阶段的做法是
「忙碌时压在本地只留最新」（`_ctx_flush_pending`）+ 需要时手动压缩（先备份）。
