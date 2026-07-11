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
