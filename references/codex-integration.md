# Codex 集成参考(exec 现状 + 升级路径)

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
- 定位:**只当纯文本/看图模型用**(read-only + approvalPolicy never + cwd=/tmp),不让它当 agent。
  编排循环未接。

## 1. 四种集成方式(升级路径)

| 方式 | 场景 | 流式 | 会话 | 备注 |
|---|---|---|---|---|
| `codex exec` | 脚本/CI/一次性 ✅ 现用 | `--json` JSONL 事件级(非文字 delta) | `resume --last / resume <id>` ⚪ | 官方不建议做长期 API 层 |
| `codex mcp-server` | 另一个 AI 调 Codex ⚪ | 由 MCP 客户端 | `threadId` 续会话 | 只暴露 `codex` + `codex-reply` 两工具 |
| SDK(`@openai/codex-sdk` / `pip openai-codex`)⚪ | 后端程序集成 | 结构化事件流 | thread 恢复(`~/.codex/sessions`) | Python SDK beta |
| `codex app-server` | 自制客户端/最完整 ⚪ | **真文字 delta**(`item/agentMessage/delta`) | thread 全套 CRUD+fork | JSON-RPC,stdio 或 ws(实验) |

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
  `turn/interrupt` 中止;`thread/fork` 分叉对比方案;`model/list` 动态拉型号+
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
- `--yolo`(=bypass approvals+sandbox)只允许在容器/VM 隔离环境;网络默认关(`--search`
  开实时搜索);多个并行 Codex 各用独立 worktree;app-server 对外必须认证+TLS。

## 4. 给调用方(我们的 relay/assistant)的约定要点

- 请求带全:目标/工作目录/能否改文件/是否跑测试/禁触目录/期望格式/成功标准。
- 保存 `threadId`+工作目录+模型,后续**续 thread 而非重塞历史**。
- 完成状态只认 `turn/completed status=completed`;failed/interrupted 不得报成功。

## 5. Realtime 语音探测(2026-07-11,实测结论:暂不可用)

- ✅ 0.144.1 接口真实存在(`codex app-server generate-json-schema` 为准):`thread/realtime/start|appendAudio|appendText|appendSpeech|stop` + transcript/outputAudio 事件;**transport 有 `websocket` 型**(不需要浏览器 WebRTC,纯服务端可接,GPT 说明书没提);Schema 有 **RealtimeVoice 19 音色枚举**(alloy/cedar/marin/sage…,说明书说"无 voice 字段"是错的);音频块 = {data, sampleRate, numChannels}。
- ✅ 前置开关:feature **`realtime_conversation`**(underDevelopment,默认关)——不开报"thread does not support realtime conversation";`experimentalFeature/list`(带 cursor 翻页)可拉全部 90+ features 现状。已在 `~/.reader-codex/home/config.toml` 开启(无副作用)。
- ❌ **认证卡死**:`thread/realtime/error: "realtime conversation requires API key auth"`——**ChatGPT 订阅登录不行,必须 OpenAI Platform API Key(独立按量计费)**。说明书"认证方式=已登录 ChatGPT 账号"实测为错。
- 判断:即使掏 API Key,OpenAI Realtime 音频价(~$32/$64 每 M)比豆包 S2S 贵近一个量级,且我们豆包线已深度打磨——不值得切。留档等 OpenAI 把 Realtime 纳入订阅额度再启用(初始化需 `capabilities.experimentalApi: true`)。

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
