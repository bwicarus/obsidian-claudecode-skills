# GPT-Realtime-2.1 API 中文详细说明书

> 核对日期：2026-07-11（Asia/Tokyo）  
> 模型 ID：`gpt-realtime-2.1`  
> 资料范围：OpenAI 官方模型页、Realtime GA 指南、连接指南、会话/事件参考、成本与缓存指南、Realtime 提示指南。

## 1. 一页结论

`gpt-realtime-2.1` 是面向低延迟语音代理的推理模型。它直接接收文本、音频和图像，输出文本或音频，支持函数调用、MCP 工具和自动提示缓存。2.1 相对 2.0 主要改善字母数字识别、静音/噪声处理和打断行为。

生产环境的推荐起点：

- 浏览器/移动端语音：WebRTC；服务端媒体管道：WebSocket；电话：SIP。
- 普通语音代理从 `reasoning.effort: "low"` 开始，不要默认上 `high`/`xhigh`。
- API 主密钥只放后端。前端使用后端统一建立 WebRTC 会话，或使用后端签发的短期 client secret。
- 固定 instructions、工具定义及其顺序，不要每轮重写；这是提高缓存命中率的核心。
- 只在需要时启用额外输入转写，因为转写模型另行计费。
- 通过每个 `response.done.response.usage` 统计实际成本，不用音频时长简单代替账单。
- 长会话使用摘要 + 批量截断，建议先试 `retention_ratio: 0.8`，避免每一轮小幅截断导致缓存持续失效。
- 只读、低风险工具可以主动调用；写操作必须先复述目标、后果和精确标识，并获得明确确认。

## 2. 模型能力与限制

| 项目 | 官方当前值 |
|---|---|
| 模型 | `gpt-realtime-2.1` |
| 类型 | 带工具使用能力的推理模型 |
| 上下文窗口 | 128,000 tokens |
| 模型页最大输出 | 32,000 tokens |
| 知识截止 | 2024-09-30 |
| 输入 | 文本、音频、图像 |
| 输出 | 文本、音频 |
| 视频 | 不支持 |
| 函数调用 | 支持 |
| 提示缓存 | 支持，自动、best effort |
| Structured Outputs | 不支持 |
| 微调 | 不支持 |
| 单个 Realtime 会话最长时间 | 60 分钟 |

### 2.1 必须注意的上限口径

模型页的 **32,000 最大输出** 是模型能力口径；当前 Realtime 会话 API 参数参考仍说明 `max_output_tokens` 可设为 1–4096 或 `"inf"`。因此：

1. 不要直接把 `32000` 写进会话配置并假定它合法；
2. 使用 `"inf"` 或 schema 允许的整数；
3. 以 `session.updated` 返回的最终配置和运行时错误为准；
4. 对长答案应设计分段输出，而不是依赖单轮极大输出。

### 2.2 输出模态

当前会话参考的语义是：

- `output_modalities: ["audio"]`：音频输出，并带音频 transcript；
- `output_modalities: ["text"]`：纯文本；
- 不要假定可以同时请求 `["text", "audio"]`。音频响应附带的 transcript 已可用于字幕和日志。

语音一旦在本会话中产生过音频输出，voice 通常不能再切换；应在首次响应前确定。

## 3. 架构选择

| 场景 | 推荐传输 | 原因 |
|---|---|---|
| 浏览器/移动端直接采集、播放音频 | WebRTC | 媒体处理、抖动和实时播放更稳健 |
| 后端已有 PCM/电话媒体流/worker | WebSocket | 服务端可直接管理 Base64 音频块和事件 |
| 电话呼入呼出 | SIP | 原生电话会话，可接听、拒绝、转接、挂断 |

不要把对话式 `/v1/realtime` 与专用翻译/转写会话混为一谈：翻译和转写是连续流架构，不使用正常的 `response.create` 回合生命周期，且按音频时长计费。

## 4. 鉴权与连接

### 4.1 浏览器 WebRTC

最简单的安全架构是浏览器把 SDP 发给自己的后端，后端用标准 API key 调用：

```http
POST https://api.openai.com/v1/realtime/calls
Authorization: Bearer $OPENAI_API_KEY
Content-Type: multipart/form-data
OpenAI-Safety-Identifier: <稳定且隐私保护的用户哈希>
```

multipart 中传 `sdp` 与 `session`。后端将 OpenAI 返回的 SDP answer 原样交回浏览器。标准 API key 绝不能下发前端。

另一种方式是后端调用 `POST /v1/realtime/client_secrets` 创建短期凭证，再由浏览器携短期凭证连接 `/v1/realtime/calls`。安全标识应在创建 client secret 的后端请求中设置，它会绑定到该临时凭证。

### 4.2 服务端 WebSocket

```text
wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1
Authorization: Bearer $OPENAI_API_KEY
OpenAI-Safety-Identifier: <hashed-user-id>
```

WebSocket 中所有控制消息都是 JSON 字符串；输入和输出音频块需要应用自行 Base64 编解码。浏览器虽可用短期凭证连接 WebSocket，但官方对客户端媒体仍优先推荐 WebRTC。

### 4.3 GA 与旧 Beta 的区别

- GA 不再发送 `OpenAI-Beta: realtime=v1`；
- 临时凭证使用 `/v1/realtime/client_secrets`；
- WebRTC 建连使用 `/v1/realtime/calls`；
- 配置中应包含 `session.type: "realtime"`；
- 输出音频配置位于 `session.audio.output`；
- 使用 `response.output_text.delta`、`response.output_audio.delta`、`response.output_audio_transcript.delta` 等 GA 事件名。

## 5. 推荐会话配置

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "model": "gpt-realtime-2.1",
    "output_modalities": ["audio"],
    "reasoning": { "effort": "low" },
    "max_output_tokens": 1024,
    "audio": {
      "input": {
        "format": { "type": "audio/pcm", "rate": 24000 },
        "turn_detection": { "type": "semantic_vad" }
      },
      "output": {
        "format": { "type": "audio/pcm" },
        "voice": "marin"
      }
    },
    "instructions": "# Role and Objective\n...固定版本的生产提示...",
    "tool_choice": "auto",
    "parallel_tool_calls": true,
    "tools": [],
    "truncation": {
      "type": "retention_ratio",
      "retention_ratio": 0.8,
      "token_limits": { "post_instructions": 8000 }
    },
    "tracing": "auto"
  }
}
```

说明：

- 示例中的 `max_output_tokens: 1024` 是成本/延迟保护值，不是官方统一推荐值；需用真实对话评测。
- `post_instructions` 不包含 instructions 自身，限制的是 instructions 之后送入模型的输入。
- `parallel_tool_calls` 仅在工具彼此独立时启用；写操作或有依赖顺序的工具应关闭或由服务端串行化。
- `tracing` 一旦在会话中开启，当前文档称不能再修改。
- 服务端接受 `session.update` 后应等待并记录 `session.updated`，确认实际生效配置。

## 6. 事件与对话生命周期

典型语音回合：

```text
session.created
  → session.update / session.updated
  → 用户音频进入 input buffer
  → VAD speech_started / speech_stopped（或应用手动 commit）
  → conversation item 创建
  → response.created
  → 文本/音频/transcript delta
  → response.done（含 usage）
```

### 6.1 文本输入

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "message",
    "role": "user",
    "content": [{ "type": "input_text", "text": "查询订单 ORD-3125B23" }]
  }
}
```

然后发送：

```json
{ "type": "response.create" }
```

如果 VAD 配置为自动创建响应，则音频回合不应再重复发送 `response.create`，否则可能产生重复回答。手动回合模式中，WebSocket 客户端需 append 音频、commit buffer，再创建 response。

### 6.2 打断（barge-in）

WebRTC 负责媒体播放，但应用仍需正确处理用户打断：停止本地播放、取消当前 response，并把未播放的助手音频从对话上下文中截断到实际播放位置。否则模型可能“记住”用户没有听到的内容。应记录：

- 用户开始说话时间；
- 当前 response/item ID；
- 已实际播放的音频毫秒数；
- cancel/truncate 是否成功。

2.1 改善了打断行为，但客户端的播放边界和状态同步仍是应用责任。

## 7. Token、计费与实际测量

### 7.1 当前单价（美元 / 100 万 tokens）

| 模态 | 输入 | 缓存输入 | 输出 |
|---|---:|---:|---:|
| 文本 | $4.00 | $0.40 | $24.00 |
| 音频 | $32.00 | $0.40 | $64.00 |
| 图像 | $5.00 | $0.50 | — |

价格会变动，上线前应再次查看模型页。工具型模型或外部 MCP/连接器还可能有独立调用成本；上表只表示 `gpt-realtime-2.1` 的模型 token 价格。

### 7.2 音频 token 近似

- 用户输入音频：约每 100 ms 1 token，即每分钟约 600 tokens；
- 助手输出音频：约每 50 ms 1 token，即每分钟约 1,200 tokens；
- 还会有消息结构等特殊 token，因此只是估算。

按当前音频价格粗算，仅音频 token：

- 1 分钟用户语音：`600 / 1,000,000 × $32 ≈ $0.0192`；
- 1 分钟助手语音：`1,200 / 1,000,000 × $64 ≈ $0.0768`。

这不包括文本上下文、推理/输出、工具结果、图像和可选输入转写，也没有计入缓存折扣。

### 7.3 为什么后面的回合更贵

每次创建 Response，整个当前 Conversation 都会成为输入。上一轮的用户消息、助手响应和工具结果会在后续轮次中再次成为输入。因此不做缓存或裁剪时，长会话的每轮输入成本会持续增长。

### 7.4 以 `response.done` 为账本

```json
{
  "type": "response.done",
  "response": {
    "usage": {
      "total_tokens": 253,
      "input_tokens": 132,
      "output_tokens": 121,
      "input_token_details": {
        "text_tokens": 119,
        "audio_tokens": 13,
        "image_tokens": 0,
        "cached_tokens": 64,
        "cached_tokens_details": {
          "text_tokens": 64,
          "audio_tokens": 0,
          "image_tokens": 0
        }
      },
      "output_token_details": {
        "text_tokens": 30,
        "audio_tokens": 91
      }
    }
  }
}
```

建议按 `session_id + response_id` 幂等入账，并分别累计文本/音频/图像、缓存/非缓存、输入/输出。不要只存 `total_tokens`，否则无法定位成本来源。

### 7.5 输入转写是额外账单

启用 input transcription 时，转写由另一个模型完成并按该模型价格另计。其用量出现在 `conversation.item.input_audio_transcription.completed.usage`，不应与 `response.done` 混为一笔或漏记。

如果业务只需要 speech-to-speech，不需要可靠用户逐字稿，可评估关闭额外转写；模型本身理解输入音频并不依赖这个异步 transcript。

## 8. 缓存优化：真正有效的做法

提示缓存自动发生，无需 `cache: true`。它匹配此前请求的相同输入 token 前缀，属于 best effort，不保证每次命中。

### 8.1 保持稳定前缀

按以下顺序组织：

1. 固定 instructions 或固定版本的 stored prompt；
2. 固定工具定义和固定工具顺序；
3. 稳定的产品/政策上下文；
4. 最后才放动态用户数据和最新消息。

避免：

- 每轮把当前时间、随机 ID 写进 instructions；
- 根据权限随意重排 tools；
- 修改工具 description 的标点或 schema；
- 在会话中途频繁 `session.update` instructions/tools；
- 删除或改写对话中间位置的旧 item。

若动态信息必须更新，放到末端新 item 或工具结果中，不要修改稳定前缀。

### 8.2 Stored Prompt

可在 session 中引用服务器存储的 prompt：

```json
{
  "prompt": {
    "id": "pmpt_123",
    "version": "89",
    "variables": { "city": "Paris" }
  }
}
```

明确 pin `version` 能让生产行为和缓存前缀更稳定。直接设置的 session 字段若与 prompt 重叠，会覆盖 prompt 中对应字段。

### 8.3 截断策略

默认接近“只删刚好够用的最旧内容”。这可能导致每轮都从前部删一点，每轮都破坏缓存前缀。更好的策略是一次多删：

```json
{
  "type": "session.update",
  "session": {
    "truncation": {
      "type": "retention_ratio",
      "retention_ratio": 0.8,
      "token_limits": { "post_instructions": 8000 }
    }
  }
}
```

`0.8` 不是普适最优值，而是官方成本指南给出的示例。它在触发截断时保留上限的 80%，用牺牲部分短期记忆换取更多后续回合的缓存稳定性。

完全自行管理时可设置：

```json
{
  "type": "session.update",
  "session": { "truncation": "disabled" }
}
```

超限时会直接报错；只有在应用可靠地摘要、删除和恢复时才建议这样做。

### 8.4 摘要而不是无限历史

达到预算阈值时：

1. 将已稳定的用户偏好、已确认实体、已完成操作和未决事项写成短摘要；
2. 用 `conversation.item.delete` 删除一批旧 item；
3. 用 `conversation.item.create` 加入摘要；
4. 保留最近若干原始回合，避免摘要丢失局部语义；
5. 摘要后接受一次缓存失效，随后保持新前缀稳定。

频繁逐条删除的缓存效果通常差于低频批量压缩。

## 9. 推理强度与 token/延迟控制

| effort | 适用场景 |
|---|---|
| `minimal` | 定时器、简单命令、极低延迟优先 |
| `low` | 客服、订单查询、简单政策问题；生产默认起点 |
| `medium` | 多步诊断、复杂路由 |
| `high` | 高精度、多约束且错误代价高 |
| `xhigh` | 复杂规划/关键编排，接受更高延迟和成本 |

更高 effort 可能增加延迟和输出 token。不要按会话统一设置最高档：如果应用允许，可按任务类型更新，但频繁修改会话前缀可能影响缓存，应先评估收益。更简单的做法是为不同工作流建立不同固定配置的会话。

同时使用三道预算闸门：

- 提示中按任务类型规定回答长度；
- `max_output_tokens` 设置合理上限；
- 服务端设置单会话成本/时长/工具调用次数上限。

## 10. 工具调用

### 10.1 Function tool 定义

```json
{
  "type": "function",
  "name": "lookup_order",
  "description": "按已确认的订单号读取订单状态；只读，不修改订单。",
  "parameters": {
    "type": "object",
    "properties": {
      "order_id": {
        "type": "string",
        "description": "完整订单号，例如 ORD-3125B23"
      }
    },
    "required": ["order_id"],
    "additionalProperties": false
  }
}
```

工具 schema 本身会进入上下文并消耗输入 token。工具越多、描述越长，每一轮越贵。只挂载当前工作流需要的工具；合并高度重复的工具，但不要合并成一个语义模糊的“万能工具”。

### 10.2 标准函数调用闭环

模型产生 function call 后：

1. 收集增量参数，等待参数完成事件；
2. 按 `call_id` 去重，校验 JSON 和业务 schema；
3. 在服务端执行工具；
4. 把结果作为 `function_call_output` item 加回 Conversation；
5. 发送 `response.create`，让模型解释结果或继续下一步。

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "call_abc123",
    "output": "{\"status\":\"shipped\",\"eta\":\"2026-07-13\"}"
  }
}
```

随后：

```json
{ "type": "response.create" }
```

不要在参数 delta 尚未完成时执行；不要相信模型参数已经过业务授权；不要在超时重连后按同一 `call_id` 重复写入。

### 10.3 工具安全策略

| 工具类型 | 默认策略 |
|---|---|
| 低风险只读查询 | 意图与必填字段清楚即可调用 |
| 依赖精确标识的查询 | 先复述/确认订单号、邮箱、电话号码等 |
| 发消息/邮件 | 先草拟或概述，再确认发送 |
| 修改账户 | 先说明将修改什么并确认 |
| 付款、购买、取消 | 确认金额、对象和后果 |
| 不可逆/高影响 | 明确确认，必要时转人工 |

只有工具返回成功后才能向用户声称“已完成”。失败时不要暴露原始堆栈或密钥；临时错误最多自动重试一次，同参数重复失败后转备用路径或人工。

### 10.4 并行调用

`parallel_tool_calls: true` 允许推理型 Realtime 模型并行调用多个工具。仅当以下条件全部成立时使用：

- 工具互不依赖；
- 都是只读或可安全幂等；
- 顺序不影响结果；
- 服务端能分别按 call ID 跟踪超时和失败。

付款后发票、先建单后通知等有依赖流程必须串行。

### 10.5 MCP 与连接器

Realtime 会话可以配置远程 MCP server/connector，让模型调用其工具。生产中必须：

- 限制可见工具集合；
- 对写操作使用审批策略；
- 不把长期访问令牌放进 prompt、tool output 或浏览器；
- 对第三方工具的调用费、速率限制和数据驻留单独核算；
- 把第三方输出视为不可信输入，限制长度并清理敏感字段。

MCP 不等于“免费工具调用”：模型读取工具定义、生成调用参数、读取返回结果都会占模型上下文，第三方服务还可能另收费。

### 10.6 静音 no-op 工具

语音环境中的静音、背景电视、等待音乐不应触发寒暄。官方提示指南建议提供空操作：

```json
{
  "type": "function",
  "name": "wait_for_user",
  "description": "当最新音频是静音、背景噪声、等待音乐、电视或未对助手说的话时调用；结束本轮且不说话。",
  "parameters": {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": false
  }
}
```

不清楚但明显是在对助手说话时，应简短请求重说，而不是调用 `wait_for_user`。

## 11. 生产提示模板

```text
# Role and Objective
你是……；你的任务是……

# Personality and Tone
自然、冷静、简洁。

# Language
跟随用户语言。专有名词和编号逐字符确认。

# Reasoning
直接答案和简单查询快速回答；多步诊断、工具选择和升级前先推理。
音频不清楚时不要推理或猜测，先请用户重说。

# Preambles
仅在明显耗时的工具或多步任务前说一句短提示；快速查询不说填充语。
描述即将执行的动作，不透露内部推理。

# Verbosity
直接答案 1–2 个短句；澄清时一次只问一个问题；工具结果先给结论，再给下一步。

# Tools
只使用当前工具列表中的工具，绝不虚构或模拟工具。
只读低风险查询在字段齐全时直接调用。
写操作先复述目标和后果，获得明确确认后才能调用。
工具成功后才能声称已完成；同参数失败不重复调用超过一次。

# Unclear Audio
音频含糊、截断或噪声严重时，不猜测、不调用业务工具，简短请求重说。

# Entity Capture
订单号、邮箱、电话、金额、日期等属于高精度字段；调用账户查询或写操作前逐项确认。

# Long Context Behavior
优先使用最新的已确认信息；发现摘要与用户最新陈述冲突时，以最新明确确认内容为准并指出冲突。

# Escalation
权限不足、重复失败或高风险不确定时停止操作，说明原因并转人工。
```

不要一开始堆满规则。官方建议从最小提示开始，以真实音频、口音、噪声和工具失败评测，再只为已观察到的失败添加规则。

## 12. 可观测性与故障排查

每个事件至少记录：

- session ID、response ID、item ID、call ID、event ID；
- 事件类型和时间戳；
- speech start/stop、commit、response created/first delta/done；
- 首音延迟、完整响应延迟、工具耗时；
- response status/error，但敏感内容脱敏；
- 分模态 usage、cached tokens、转写 usage；
- 打断发生时间和实际播放边界。

语音回合是“检测说话 → 停止/commit → 创建响应 → 音频 delta → done”的链路。回答正确但感觉慢，可能是网络、VAD 或工具延迟；音频被切坏，可能是打断边界；不要把所有问题都归因于模型。

推荐告警：

- 缓存命中率突然下降；
- 单轮 input tokens 持续增长；
- 连续 tool call 相同参数失败；
- `response.done` 缺失或重复入账；
- VAD 产生大量空回合；
- 转写费用异常增长；
- 会话接近 60 分钟；
- 429、会话超限和截断频率上升。

## 13. 成本优化检查表

- [ ] 先用正式模型验证质量，再评估 `gpt-realtime-2.1-mini`。
- [ ] 默认 reasoning `low`，只有评测证明有收益才提高。
- [ ] instructions 和 tools 固定版本、固定顺序。
- [ ] 动态数据追加在上下文末端，不改稳定前缀。
- [ ] 工具只暴露当前流程需要的最小集合。
- [ ] 工具结果裁剪为模型需要的字段，不回传完整数据库对象。
- [ ] 设置合理 `max_output_tokens` 和回答长度规则。
- [ ] 用 `retention_ratio < 1` 做低频批量截断。
- [ ] 长会话定期摘要，保留最近原始回合。
- [ ] 不需要逐字稿时关闭额外 input transcription。
- [ ] VAD 过滤空白音频，并为背景音提供 no-op 工具。
- [ ] 逐 response 保存 usage 和 cached token 明细。
- [ ] 设置单用户/单会话预算、时长和工具调用上限。

## 14. 上线前验证矩阵

至少覆盖：

1. 正常说话、插话、连续追问、长时间静音；
2. 背景音乐、电视、多人侧聊、弱网、丢包；
3. 中英混合、不同口音、字母数字串、邮箱、电话、日期和金额；
4. 工具成功、超时、临时失败、权限失败、空结果和重复回调；
5. 写操作未确认、确认后改口、打断确认句；
6. 30–60 分钟长会话、摘要、截断和缓存命中率；
7. WebRTC 重连、WebSocket 断线、重复事件和幂等恢复；
8. 预算达到阈值时是否能安全结束或降级。

## 15. 官方资料

- [GPT-Realtime-2.1 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
- [Realtime API 总览](https://developers.openai.com/api/docs/guides/realtime)
- [管理 Realtime 对话](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Realtime 成本与缓存](https://developers.openai.com/api/docs/guides/realtime-costs)
- [Realtime 模型提示指南](https://developers.openai.com/api/docs/guides/realtime-models-prompting)
- [Realtime 工具与 MCP](https://developers.openai.com/api/docs/guides/realtime-mcp)
- [WebRTC 连接指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [WebSocket 连接指南](https://developers.openai.com/api/docs/guides/realtime-websocket)
- [服务端控制与 Webhook](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- [Realtime 客户端事件参考](https://developers.openai.com/api/docs/api-reference/realtime_client_events)
- [Realtime 服务端事件参考](https://developers.openai.com/api/docs/api-reference/realtime_server_events)

## 16. 文档口径说明

- 当前模型页和连接指南已明确使用 `gpt-realtime-2.1`；提示指南标题/部分正文仍以 Realtime 2.0 或 `gpt-realtime-2` 讲解。2.1 属于该推理型 Realtime 系列的更新版，本手册将这些通用提示策略用于 2.1，并以 2.1 模型页为能力与价格依据。
- 官方成本指南的一处 JSON 示例使用了顶层 `"event": "session.update"`，而 GA 客户端事件的标准 discriminator 是 `"type": "session.update"`；本手册统一使用 `type`。
- 价格、速率限制和 schema 可能变化；生产代码应校验 `session.updated`、错误事件与官方 API reference，而不是只依赖本文静态值。

---

## 工具调用的计费形态(2026-07-14 实测,别信文档字面)

**背景**:想知道"AI 说『我去查一下』→ 调工具 → 再回答"到底算几次 response(=几次计费)。Grok 那边看起来把工具前后两段输出算成一次。

### 实测结论(真调 `gpt-realtime-2.1-mini`,`scratchpad/rt_probe.py`)

| 场景 | response 数 | 这一次 response 的 output items | output token |
|---|---|---|---|
| 指令要求「调工具前先说一句」 | **1** | `message`(语音"我去查一下东京的天气哦。")**+** `function_call` | 104(text 65 + audio 39 + reasoning 23) |
| 指令要求「静默直接调工具」 | **1** | 只有 `function_call` | 43 |

⚠ **官方文档这句话是不准的**:
> "**Instead of** immediately returning a text or audio response, the model will **instead** generate a response that contains the arguments…"

实测:模型**完全可以在同一个 response 里既说话又发 function_call**(output 是数组,两种 item 并存)。所以「垫话」**不额外多一次 response**。

### 由此得出的计费真相

一次工具往返 = **恒定 2 次 response**(客户端 function tool 路径):
1. 说话(可选)+ 产出函数参数 —— 1 次
2. 工具结果回填(`function_call_output`)后 `response.create` 组织回答 —— 1 次

**官方计费口径**(Managing costs 页):"**The entire conversation is sent to the model for each Response**" / "costs are accrued **when a Response is created**"。
→ 第 2 次 response = **整段会话按 input 重新计费一遍**。本项目实测均值:**每 response input 5367 token(cached 4115)、output 仅 311** —— **输入是输出的 17 倍**,大头全在 input 重算。

**因此**:
- 「静默调工具」省的只是那句垫话的 output(61 token ≈ **$0.0008/次**)。84 次工具调用总共省 8 美分 —— **可忽略,是体验取舍不是成本优化**。
- 想真省一半,唯一路径是 **远程 MCP 托管工具**(`realtime-mcp`:`{type:"mcp", server_url, require_approval:"never"}`)—— 工具由 **OpenAI 服务端自己执行**,`mcp_call` → assistant message → `response.done` **同一 turn 收尾**,客户端**不发第二次 `response.create`**。对应 task #279 + #291。
  ⚠ MCP 的计费口径官方文档**没写**,上线前必须实测 `response.done.usage` 对账。
  ⚠ **动作类工具(翻页/高亮/看画面)不适合迁 MCP**(要操作用户当前打开的页面);查询类(web_search / read_page / notes_query / lookup_word)才吃得到红利 → **混合**,不是全迁。

### 本项目真实账单(`state/openai-usage.json` + `state/voice-ledger.db`)

- 累计 **670 个 response,$3.83**(gpt-realtime-2.1-mini 官方价:audio in $10 / cached $0.30 / audio out $20;text in $0.60 / cached $0.06 / text out $2.40 每 1M)
- 近期 **84 次工具调用 / 220 个 response** → **约 38% 的 response 是"工具回来后再说一次"** 产生的
- `_OA_RATE`(relay 里的单价表)已经是 mini 官方价,**没问题**;⚠ 别跟同文件里 `_log_usage` 的豆包**人民币**表(10/5/80/300)搞混
- `usage.output_token_details` 里有 `reasoning_tokens`(23~25/次)—— 2.1 带 reasoning,effort 可配,也是成本项

## 异步函数调用(C):**已经开着,且关不掉**

官方(开发者博客 / GA 公告)只写了两句,但都是硬结论:
> "This feature is **automatically enabled for new models—no changes necessary on your end**."
> "available natively in `gpt-realtime`, so **developers do not need to update their code**."

- **没有任何 API 字段/开关**(session 级、response 级都没有)。我们跑的 `gpt-realtime-2.1-mini` 是 GA 代 → **异步函数调用本来就在生效**。
- 语义:function_call 发出后客户端**不必立刻**回 `function_call_output`,会话不冻结;用户问结果时模型会自己说 **"I'm still waiting on that"**(这句 placeholder 是**模型内置调过的**,不是 prompt 工程)。
- ⚠ **不省钱**:工具结果回来后仍要 `function_call_output` + **显式 `response.create`**(`conversation.item.create` 本身**从不**触发生成)= 又一次 response = 整段会话 input 全量重算。异步只解决"卡住",不解决"重算"。
- ⚠ 官方**没有**:pending 超时上限 / 多个 function_call 并存 / 回填顺序错乱 的任何说明,也**没有 cookbook 示例**。
- ⚠ **MCP 必须配 GA 模型**(官方唯一明确的兼容性警告):老 beta 模型 "lacks async function calling, pending MCP tool calls without an output **may not be treated well by the model**"。→ 直接约束 task #279 的模型选型。
- 🎁 副产品:**豆包 S2S 上手搓的「我去查一下」垫话编排,在 OpenAI 这边是原生的** —— 迁过去可以砍掉那一整块自研代码。

### 我们的工具耗时实测(`tool_calls` 表,84 次)

| 档 | 占比 | 说明 |
|---|---|---|
| <1s | **61.9%** | `read_page` 0.1s / `goto_page` / `toc` / `make_anki` 等,秒回 |
| 1–3s | 1.2% | |
| 3–8s | 34.5% | 几乎全是 `web_search`(29 次,均 3.9s)+ `search_image` 6.9s |
| >8s | 2.4% | 含一次 `read_page` **卡 164.9s**(挂起,非正常耗时) |

→ **异步的边际价值对我们很低**:六成工具秒回,唯一慢的 `web_search` 4 秒有垫话完全能忍;长任务我们早就用 `task_id` + 后台 job + 进度卡片(#205/#206/#260)自己实现了同等效果。

### 但它暴露了一个真 bug:工具调用没有超时(2026-07-14 已修)

`rc-voicecall.js::_rtcTool` 的 `fetch('/api/assistant/voice-tool')` **原来没有 AbortController** → 工具不回 = `function_call_output` 不回填 = `response.create` 不发 = **整通对话死在那里,AI 一句话都不说**(账本实录:一次 `read_page` 卡 164.9s)。

修法:`AbortController` + **45s** 超时(同步工具实测最慢 6.9s,长任务后端本来就走 task_id 派发 → 45s 只可能兜住真挂起)。超时抛错 → 落进原有的 catch → 照常回填「工具超时」的 `function_call_output` + `response.create`,**让 AI 开口说"没查到,换个方式"而不是哑掉**;工具卡标 error(按设计留在页面上可点)。
同文件里「系统代提交」那条 fire-and-forget 的 fetch 也补了 60s 超时(它挂了不会掐死对话,但会留一张**永远转圈**的工具卡)。


## A/B 混合:**调用前垫话策略**(2026-07-14 上线)

「AI 调工具前先说一句『我去查一下』」= A;「静默直接调」= B。实测两者**都只有 1 次 response**(垫话搭在 function_call 那一次里),差别仅是那句话的 output token(≈$0.0008/次)。
→ **这是体验旋钮,不是省钱旋钮**:秒回的工具垫话反而啰嗦,慢工具不垫话就是干等。所以做成**混合**:

**每个工具一条策略**:`auto`(默认)/ `always` / `never`。
- **`auto` 不拍脑袋** —— 读 `state/voice-ledger.db` 的 `tool_calls`,取这个工具的**中位耗时**(取中位不取均值:一次 164.9s 的挂起会毁掉均值),`median >= 阈值`(默认 2.5s)→ 垫话,否则静默。没数据的新工具 → 保守垫话。
- 实测判定:`web_search` 3.8s → **垫话**;`read_page` 0.1s → **静默**;`goto_page` 0.0s → **静默**。

**唯一注入口**:策略拼进实时语音 session 的 `tools[].description`(`assistant.py::_tool_desc_rtc`)—— 跟工具说明同一条通道,改完下次开通话即生效。

| 层 | 位置 |
|---|---|
| 存储 | `state/assistant-tool-prompts.json`:per-tool `{"filler": "auto|always|never"}`;全局 `{"_global":{"filler":{"mode","threshold_s"}}}` |
| 后端 | `assistant.py`:`_tool_median_s` / `_filler_global` / `_filler_mode` / `_FILLER_TXT` / `_tool_desc_rtc`;端点 `/api/assistant/tool-prompt` 加 `op=filler`(单工具)、`op=filler_global`(总设置)、GET `?tool=_global` |
| 单工具 UI | **长按工具卡 → 详情窗**顶部分段控件(`rc-toolchip.js::mountFiller`),状态行直接显示「这个工具中位耗时 3.8s ≥ 2.5s 阈值 → 会先说一句」 |
| 总设置 UI | 设置面板 **AI tab → 🗣 语音·调用前垫话**(`rc-settings.js::_fillFiller`):默认策略 + 自动的耗时阈值 |

⚠ **动作类工具本来就是单轮,不用管垫话** —— `_SILENT_ACT = {goto_page, highlight, auto_highlight, add_vocab, open_book}`(加 search_image / search_video)成功后标 `silent`,前端**不发第二次 `response.create`**(`rc-voicecall.js`:`if (!_rtcTool._silent) _rtcRespCreate(...)`)。所以「翻页没必要两次」这件事**早就做到了**。


## 远程 MCP 托管工具:**跑通了,但省不了钱**(2026-07-14 实测,推翻先前推断)

### 结论先行

**❌ MCP 不能把工具往返变成 1 次 response。** 我先前基于官方 `realtime-mcp` 页(「assistant message 和 response.done 收尾同一个 turn」)推断它能砍一半 —— **实测是错的**。

实测(`scratchpad/rt_mcp.py`,gpt-realtime-2.1-mini + 真实 MCP):

| | |
|---|---|
| response#1(0.6s) | items=`['mcp_call']`(或 `['message','mcp_call']`),此时 `mcp_call.output = None` |
| mcp_call 完成 | **1.9s**(OpenAI → VPS → Pi → 回,网络往返仅 ~1.3s) |
| response#2(3.1s) | 模型说出真实结果,**in=2898** out=184 |
| **合计** | **2 次 response** —— 跟客户端 function tool 一样 |

**根因 = 异步函数调用(GA 模型自动开、关不掉)**:模型**不等**工具结果就 `response.done`,结果晚到后落进 conversation,必须再 `response.create` 才会说出来。所以官方 `realtime-mcp` 页那段事件序列描述,**在 realtime 上实际不成立**。

⚠ **还可能更贵**:MCP 工具结果**不经我们截断**直接进上下文。`list_books` 吐 9.8KB JSON(`comp_compressing`/`comp_percent` 等无用字段)→ 第二次 response 的 input 冲到 **2898 token**。我们自己的 function tool 路径有 `out.slice(0, 1800)`。**要用 MCP 就必须先把工具返回瘦身。**

### MCP 真正买到的东西

1. **零客户端代码** —— 工具循环不在浏览器里,页面切后台/关掉也能跑
2. **少一趟往返** —— OpenAI 直连我们服务器(1.3s),不再 OpenAI→浏览器→Pi→浏览器→OpenAI
3. **工具定义唯一** —— 跟 claude.ai / ChatGPT 连接器共用同一套

**但动作类工具(翻页/高亮/看画面)本来就不该走 MCP**(要操作用户当前打开的页面),而它们**早就是单轮**(`_SILENT_ACT` 标 silent → 前端不发第二次 `response.create`)。

### 接通过程中踩的三个坑

1. **`server_url` 不能用 Funnel 的 `:8443`** —— OpenAI 不接受非标准端口,`mcp_list_tools.failed` 且我们服务器日志里**一条请求都没有**。必须用 **`https://bwicarus.space/mcp`**(VPS 443 → tailnet 反代到 Pi:8766)。
2. **必须 stateless** —— 原来 `mcp.streamable_http_app()` 是 stateful(SSE 会话)。OpenAI 的 MCP 客户端跟它对不上:日志里每次请求都 `Created new transport` + 夹一个 400,`tools/call` 我们这侧 0.9s 就返回了(直连公网 URL 验证过),但 OpenAI 那边 `mcp_call.output` 恒为 `None`、模型一直"还在等"、最后编书名。
   → `mcp_server.py` 加 `mcp.settings.stateless_http = True` + `json_response = True`(env `MCP_STATELESS=0` 可回退)。改完立刻通。
   ⚠ **claude.ai 连接器请复测**(同一个 transport,理论上 stateless 也支持)。
3. **工具列表跟第一次 response 赛跑** —— `mcp_list_tools` 是懒加载的,`session.update` 后立刻 `response.create`,模型手上还没有工具 → 只能瞎编。**要么等 ~2s,要么第二轮才可靠。**

### 认证

`authorization` 字段直接塞**静态 token**(`~/.config/mcp-http-token`)即可 —— `mcp_oauth.py` 的门禁是「静态 token 或 OAuth token 二者其一」,不用走 OAuth 舞蹈。


---

# ⚠ 2026-07-14 更正批次(GPT 复核 + 我方复测,推翻本文档前面三条结论)

把材料给 GPT(有全套官方资料)复核后,**三条我先前写下的结论被证伪**。以下为最终版,前面章节里与此冲突的以本节为准。

## 更正 1:**成本大头是 output(音频),不是 input 重算** ❌→✅

我先前反复讲「每 response input 5367 / output 311 → 输入是输出的 17 倍 → 大头在 input 全量重算」。
**错在拿 token 数量比当成本比。** 各档单价差 30 倍以上,必须分档乘价。

按 gpt-realtime-2.1-mini 官方价重算我们真实账本(670 response,$3.83):

| 档 | token | 美元 | 占比 |
|---|---|---|---|
| **output·音频** | 136,361 | **$2.727** | **71.1%** |
| input·文字(未命中) | 726,421 | $0.436 | 11.4% |
| input·音频(未命中) | 24,100 | $0.241 | 6.3% |
| output·文字 | 96,190 | $0.231 | 6.0% |
| input·文字(命中缓存) | 2,512,640 | $0.151 | 3.9% |
| input·图片 / 音频缓存 | — | $0.048 | 1.2% |
| **INPUT 合计** | | **$0.875** | **22.8%** |
| **OUTPUT 合计** | | **$2.958** | **77.2%** |

**token 数量比 14:1(input 多),美元比 0.30:1(output 多)—— 完全反过来。**

**推论(重要)**:第二次 response 贵,是因为它**要说话**;而说话就是答案本身,省不掉。
→ 就算真能做到「工具调用 = 1 次 response」,省下的也只是那次 response 的 **input 重算 ≈ $0.001**,**不是一半**。
→ **真正的省钱杠杆是砍音频 output**(答案更短、silent 工具不发言、TTS 分流),不是砍 response 次数。

## 更正 2:**stateless 不是必需的** ❌→✅

我先前写「OpenAI Realtime 的 MCP 客户端跟 stateful 模式对不上,必须 stateless+json_response」。
GPT 指出我**同时改了两个变量、不能断因**。做了 stateless × json_response **四格交叉实验**:

| stateless | json_response | 结果 |
|---|---|---|
| 1 | 1 | ✅ mcp_list_tools + mcp_call 有结果 |
| 0 | 1 | ✅ |
| 1 | 0 | ✅ |
| **0** | **0**(原生配置) | **✅** |

**四种组合全部正常。** 真因是 **mcp_call 走异步生命周期**(`response.done` 先结束,工具 1.9s 后才完成),
而我在 `response.done` 就断开了 WS —— 把「我自己没等结果」误判成了「transport 不兼容」。
→ `mcp_server.py` 已**改回原生 stateful**(`MCP_STATELESS` / `MCP_JSON` env 保留但默认关);claude.ai 连接器不受影响。

## 更正 3:`reasoning.effort` **可配**(先前标"未知")

实测 `session.update` 里 `{"reasoning":{"effort":"minimal"|"low"|...}}` —— **2.1 和 2.1-mini 都接受**,`session.updated` 回显生效。
但我们每次只有 23~25 reasoning token,**省钱空间可忽略**(mini 约 $0.00006/次),收益主要在**延迟**。
且 GPT 提醒:`reasoning_tokens` 是 output 的**子集**(65 text + 39 audio = 104 总数,23 reasoning 已含在 65 text 里),别重复相加。

## GPT 复核确认的其余各条

- ✅ **垫话 + function_call 同一 response 是官方支持的**(官方有 "Tool Call Preambles" 专章推荐这么做),不是未定义行为。但客户端要同时兼容「只有 function_call」和「message + function_call」两种。
- ✅ **客户端 function tool 无法在原 response 内续写** —— 没有 `response.resume` / `continue` / realtime 版 `previous_response_id`。需要读工具结果的调用**必然 2 次 response**。
- ✅ **异步函数调用没有公开关闭开关**(`parallel_tool_calls:false` 不是它)。
- ✅ **MCP 没有官方保证「一次工具调用只计一个 Response」**,也没有同步等待配置 → **生产按 2 次 response 预算**。官方 `realtime-mcp` 页写的是同一个 "turn",而 **turn ≠ 计费单位,Response 才是**(我先前把两者混为一谈)。
- ✅ **MCP 输出没有官方截断上限**(没有 `max_tool_output_tokens`;`max_output_tokens` 管的是模型生成)→ **必须服务端自己瘦身**。
- ✅ **非 443 端口不是官方要求**(schema 无端口白名单)→ `:8443` 打不通属于 OpenAI 出站/校验的实现问题,`mcp_list_tools.failed` **确实没有 error 字段**(不是我们漏读)。生产继续用 443。
- 🆕 **`mcp_list_tools` 赛跑的正确解法是等 `mcp_list_tools.completed` 事件**,不是固定 sleep;并且要检查 `conversation.item.done`(`item.type === "mcp_list_tools"`)确认真导入了哪些工具。
- 🆕 **out-of-band response**(`{"conversation":"none","input":[…]}`):第二次播报可以只带「本次请求 + 工具调用 + 精简结果」的小上下文,不重送整段会话。仍是第 2 次 response,但 input 更小(鉴于 input 只占 23%,收益有限);缺点是输出不自动写回主会话,要自己补摘要。
- 🆕 **静态 Bearer 不要硬编码进浏览器** —— MCP 的 `authorization` 应在**后端**创建 session 时配置。

## 最终分层建议(GPT 给的,我认同)

| 工具类型 | 走哪条路 | response 数 |
|---|---|---|
| 翻页/滚动/高亮/跳转(结果用户已看见) | 客户端 function + **silent** | **1** ✅ 已实现(`_SILENT_ACT`) |
| 查词/笔记查询(要口头报结果) | 客户端 function,**结果精简**,可选 OOB 小上下文 | 2(不可避免) |
| 视频/书单等富 UI 数据 | 完整结果**直达侧栏**,给 AI 只喂摘要/`result_id` | 2 |
| 仅后端可达、输出很小、认证复杂 | remote MCP | 2 |

**不要把 30 个工具统一迁 MCP。** MCP 的确定价值是托管执行 / 认证 / 工具发现 / 少写浏览器转发代码,**不是省钱**。


---

## 全链路成本(2026-07-14 定稿,含火山半边)

⚠ **前面「成本大头是音频 output」那节只算了 OpenAI 一半。** 我们的语音链路是**混合**的:

| 环节 | 用的是 | 单价(官方计费页 2026-07-14) |
|---|---|---|
| **耳朵**(用户说话→文字) | 火山 **豆包流式语音识别模型2.0**(`SAUC_RID_V2`,`asr_v2` 开关) | **1 元/小时**(后付费);1.0 版是 4.5 元/小时 → **切 2.0 省 4.5×** |
| **大脑** | gpt-realtime-2.1-mini(输入 **97.9% 是文字**,不是音频) | text in $0.60 / cached $0.06 / text out $2.40 per 1M |
| **嘴**(方案 A) | GPT 自己说(audio output) | **$20/1M audio tok** |
| **嘴**(方案 B) | 火山 **豆包语音合成模型2.0**(`seed-tts-2.0`,uranus 音色) | **3 元/万字符**(后付费);资源包 2.1–2.8 |
| **嘴**(方案 B′) | 火山 **大模型语音合成**(`volc.service_type.10029`,moon 1.0 音色) | 5 元/万字符 —— **比 2.0 贵,别用** |

### 每字成本(同一句话实测:音频模式 text138+audio531 tok/128字;文字模式 text83 tok/94字;RMB 按 7.2 折 USD)

| 路径 | 美元/字 | 对比 |
|---|---|---|
| ① GPT 直接说(S2S 音频) | $0.0000856 | 基准 |
| ② **文字模式 + 豆包TTS2.0(3元/万字)** | **$0.0000438** | **便宜 2.0×** ✅ |
| ③ 同上 + 资源包(2.1元/万字) | $0.0000313 | 便宜 2.7× |
| ④ 文字 + 大模型语音合成(5元/万字) | $0.0000716 | 便宜 1.2×(别选) |
| ⑤ 文字 + 系统 TTS(浏览器 speechSynthesis,免费) | $0.0000021 | 便宜 40×(机器音) |

→ **「文字输出模式 + TTS 代读」确实省一半**(用户的原始直觉正确)。四态架构(#289)里这个模式**已经有了**。

### 我在这里连错三次,教训

1. 先拿**营销文**的「超自然音色 6.5元/万字」→ 结论"TTS 比 GPT 还贵"。**错**。
2. 改用官方页的「大模型语音合成 5元/万字」→ 结论"只便宜 1.2×"。**还是错** —— 那不是我们用的模型。
3. 用户指出「我们用的是合成 2.0」→ 查代码(`X-Api-Resource-Id: "seed-tts-2.0"`)→ **3元/万字** → 真相是**便宜 2×**。

**根因:定价前没先确认「我们到底调的是哪个模型」。** 以后算 API 成本,**第一步是 grep 代码里的 model/resource id**,不是去搜价格。

### 账本已补火山半边(2026-07-14)

`voice_realtime_relay.py`:
- `_ledger_volc_asr(span, seconds, v2)` —— ASR 按时长(relay 每帧恒 100ms,数帧即秒;每 30s 落一笔,断连不丢)
- `_ledger_volc_tts(span, chars, rid)` —— TTS 按字符(1 汉字 = 1 字符,标点空格也算)
- 单价常量 `VOLC_ASR_RMB_H` / `VOLC_TTS_RMB_10K` / `RMB_USD`,**env 可覆盖**(买了资源包就改 env)
- ⚠ 历史用量无法追溯,从 2026-07-14 起才有数


---

## 通用 agent worker:多步任务甩给无头 CLI + 自家 MCP(2026-07-14 上线,用户点子)

### 为什么

一个 3 步任务走**语音模型**:每个工具一个来回 ≈ **4~6 次 realtime response**,每次全量 input 重算,而且**工具结果全堆进语音上下文**(`list_books` 一次就能顶 2.7k token)。
走 **worker**:语音模型只花 **1 次**工具调用 → **2 次 response**,它只看见最后那句 40 字摘要。省的是 **N-1 轮 realtime + 上下文不膨胀**。

CLI 走**订阅额度**(不是 API 计费)→ 白捡的算力。**所以选型只看成功率和速度,不看钱**(用户裁定)。

### 实现

- `voice.py::_task_agent` —— 新的第 5 个 task kind(`agent`),复用既有的 `_vtask_new/_vtask_set` + 进度卡片 + 完成播报
- `assistant.py::_t_do_task` / TOOLS 里的 **`do_task(instruction)`** —— 语音模型把用户原话**原样**转述给它,别自己拆步骤
- 无头调用:`claude -p <任务> --append-system-prompt <工具目录> --mcp-config <HTTP+静态Bearer> --allowedTools mcp__bwapp --disallowedTools <本地工具> --model opus --output-format stream-json`
- `stream-json` 里的每个 `tool_use` → `_vtask_set(step=…, steps=[…])` → **工具卡长条实时滚动**显示 worker 在调哪个工具

### 三个实测踩坑

1. **`--allowedTools` 不是「能力」名单,只是「自动批准」名单** —— 不在名单里的工具**依然存在**。实测 haiku **真的去调了 Bash 和 Read**。
   → 必须再加 **`--disallowedTools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,…"`**。加上之后 CLI 会老实回「没有 Bash 工具可用」。**这是安全底线,不能省。**

2. **MCP 工具在 Claude Code 里是「延迟加载」的,关不掉** —— `system.init` 事件里 `tools` 有 21 个,**其中 MCP 直接可见 0 个**;`--settings` 里塞 `toolSearch/deferTools/enableToolSearch/mcp.deferLoading` 全部无效。
   → `ToolSearch` 是**一轮固定开销**,省不掉。但可以**不让它探索**。

3. **把工具目录预先写进 system prompt**(`--append-system-prompt`)+ 要求「**一次 `select:` 精确加载,禁止关键词搜索/禁止探索/禁止多次 ToolSearch**」→ **轮数 4→3、耗时 15.5s→6.7s**。
   目录从我们自己的 MCP `tools/list` 拉,10 分钟缓存(`_agent_catalog`)。

### 选型实测(同一个 3 步任务)

| 模型 | 轮数 | 耗时 | 表现 |
|---|---|---|---|
| **opus**(默认) | **3** | **6.8s** | 干净,一次到位 |
| sonnet | 4 | 13.6s | 干净 |
| haiku | 7 | 29s | 乱,还去试 Bash/Read |

env 可覆盖:`AGENT_TASK_MODEL` / `AGENT_TASK_TIMEOUT`(默认 240s)/ `MCP_PUBLIC_URL`。

### 端到端实测(读页 + 制卡)

```
[4s]  交给助手规划…
[8s]  read_page…          ← worker 自己决定
[13s] make_anki_card…     ← 自己接着做
[17s] done → 「已按第30页『请求必由客户端发起』这一要点生成 Anki 卡」
```
语音模型全程 **1 次工具调用**。

⚠ `state/mcp-headless.json` 含**静态 Bearer token**,600 权限,**不要进 git**。


### ⚠ A/B 实测:**worker 不是速度方案,1~3 步的活直接调更快**(用户提出,实测证实)

同一个任务(读当前页 → 挑要点做 Anki 卡):

| | response 数 | 耗时 | 时间线 |
|---|---|---|---|
| **A) 语音模型直接调工具** | 3 | **7.8s** | `[2.5s] resp#1 [message+function_call read_page]` → `[2.6s] 执行 0.1s` → `[3.4s] resp#2 [function_call make_anki]` → `[3.4s] 执行 0.1s` → `[5.7s] resp#3 说话` |
| **B) worker(`do_task`)** | 2~3 | **17s** | 派发 → CLI 冷启 ~2s → ToolSearch 一轮 → read_page → make_anki → 回报 |

**直接调快一倍多。** 而且语音模型**自己就会串链**:`resp#1` 里边说话边调 `read_page`,拿到结果 `resp#2` 直接接着调 `make_anki` —— 工具各 0.1s,几乎零开销。
worker 的 17s 里大头是**纯开销**:CLI 冷启动 + **强制 ToolSearch 一轮** + opus 每轮 ~4s。**它永远追不上直接调。**

#### 所以 worker 只在这三种情况才值(已写死进 `do_task` 的工具说明,把语音模型的手按住)

1. **≥4 步**的活(「把这一章重点全标出来再逐条做成卡片」)
2. **探索性**的活 —— 事先不知道要翻几本书/查几次(「找找我读过的书里哪本提过 X」)
3. **要跑很久**的活(整章处理),不能让用户干等

**其余一律直接调。** 另外两个 worker 独有的好处(跟速度无关):
- 工具结果**不进语音模型的上下文**(直接调时,`read_page` 的整页正文会永久插进 realtime 会话,之后每次 response 都重算一遍 input)
- 跑在服务端,**页面切后台/关掉照样跑**


### 147c:**`ENABLE_TOOL_SEARCH=false`** —— ToolSearch 不是强制的(我一度以为关不掉)

**现象**:Claude Code 把 MCP 工具做成**延迟加载**——`system.init` 事件里 `tools` 有 21 个,但 **MCP 直接可见 0 个**。模型必须先调 `ToolSearch` 拿 schema,白烧 1~2 轮。
我试了 `--settings` 里塞 `toolSearch` / `deferTools` / `enableToolSearch` / `mcp.deferLoading` —— **全部无效**,于是写进文档说"关不掉"。**那是错的。**

**真开关**(在 CLI 二进制里 grep 出来的):
```js
function OBr(){                                    // 决定 tool-search 模式
  if(W4e()) return "standard";
  let e = process.env.ENABLE_TOOL_SEARCH;
  if (t === 100) return "standard";                // 100 → 关
  if (Wc(process.env.ENABLE_TOOL_SEARCH)) return "standard";   // 假值 → 关
  return "tst";                                    // 默认 → 开(ToolSearch)
}
```
→ **`ENABLE_TOOL_SEARCH=false`**(或 `100`)⇒ `standard` 模式 = **工具直接给,不用搜**。

**实测同一任务**:

| | 初始工具 | MCP 直接可见 | 轮数 | 耗时 | 调用链 |
|---|---|---|---|---|---|
| 默认 | 21 | **0** | 4 | 12.2s | `ToolSearch → ToolSearch → read_page` |
| **`ENABLE_TOOL_SEARCH=false`** | 43 | **20** | **2** | **6.0s** | `read_page` |

已接进 `voice.py::_task_agent` 的 subprocess env。**目录预注入(`_agent_catalog`)因此退役** —— 工具都直接可见了,再塞一份目录纯属浪费 token。

⚠ 另外禁掉了 `mcp__bwapp__assistant_log_chat` / `assistant_history`:我们 MCP server 的 `instructions` 让**外部编排 agent**每轮把对话写进助手历史 —— worker 是内部执行体,不需要,**实测它真去调了**,白烧 2 轮。

### 为什么**不**迁到 Claude Agent SDK(2026-07-14 查官方文档)

官方 `code.claude.com/docs/en/agent-sdk/overview` 明确:
> "Unless previously approved, Anthropic does **not** allow third party developers to offer claude.ai login or **rate limits** for their products, **including agents built on the Claude Agent SDK**. Please use the **API key** authentication methods."

→ **Agent SDK 是给 `ANTHROPIC_API_KEY` 按量计费的,不是订阅额度。** 我们 worker 的整个经济基础就是"走订阅额度、免费",换过去等于把免费的活变成按量付费。且 TS 版"bundles a native Claude Code binary",连"省掉冷启动"都不成立。
→ **维持 subprocess 调 `claude -p`。**
