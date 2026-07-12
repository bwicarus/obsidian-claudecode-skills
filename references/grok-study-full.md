

======================================================================
AGENT #1
======================================================================
# xAI Voice Agent 官方文档 vs 我们的 Grok 引擎现状

来源:指南 https://docs.x.ai/developers/model-capabilities/audio/voice-agent(下称"指南"),规格计费 https://docs.x.ai/developers/models/voice-agent-api(下称"规格页")。两页直接 WebFetch 可读,另用 r.jina.ai 复核了引文一致。

## ② per-response instructions 的确切语义(最重要)

指南原文:
> "Override the session-level system prompt for a single response by setting `instructions` on `response.create`"
> "The override applies only to this response — subsequent responses revert to the session `instructions`."

结论:**替换(override)session instructions,不是追加**;只作用当轮,之后自动恢复 session 版。**tools 不受影响**(tools 是 session 的独立字段,文档全程未提 per-response instructions 会动 tools;工具规则若写在 session instructions 文本里则该轮被一并覆盖掉)。

对照现状:`voice_realtime_relay.py:1550` 每次用户 turn_end 都发 `response.instructions = "你是简短口语化的伴读助手。" + 页码/正文/选中/笔迹`。这意味着**每个用户轮的响应里,`_oa_instructions()`(relay.py:1236-1277)的全部行为规则——see_ink 铁律、联网三工具声明、"绝不假装贴图"、写操作先说一句、失败别重复、wait_for_user 静音规则、生词表、插图描述、回答语言规则——统统被一句话人设替换**。session instructions 实际只在"工具回填后的续答"(relay.py:1502 裸 `response.create`)时生效。relay.py:1567 注释写的"官方确认之后自动恢复"理解正确,但"替换"的代价没有体现在注入内容里。

## ③ 工具回填后 create 的官方时序(我们已合规)

指南原文(四步):
> "1. Receive multiple `response.function_call_arguments.done` events (one per function call) 2. Execute all functions (can be done in parallel for performance) 3. Send a `conversation.item.create` with `function_call_output` for **each** function call 4. Only after all function outputs have been sent, emit a single `response.create` to continue"
> "Do not send `response.create` until all function call outputs have been submitted. Sending `response.create` prematurely will cause the model to respond without the complete context from all tool results."

另有播放仲裁建议:
> "**Wait until audio playback of the current turn is complete** (or nearly complete). Then send `response.create`"

对照现状:tools_n 在飞计数 + 末位工具才 create(relay.py:1487-1502)= 官方第 3/4 步原样;create 前等 `aEnd` 播放估算(relay.py:1499)= 官方播放仲裁原样。**这一块我们是教科书实现,不用改。**官方还提到函数可并行执行(我们串行意图执行,属可选优化非违规)。

## ④ 打断/barge-in:官方几乎没有手动路径

指南关于打断的**全部**内容只有两句:
> "Enable `server_vad` for automatic, natural barge-in."
> "`force_message` with `\"interruptible\": False` — caller audio is dropped until playback completes"

**没有**文档化 `response.cancel`、`conversation.item.truncate`、清客户端缓冲等手动打断流程(与 OpenAI Realtime 形成对比)。我们 relay.py:1572 的 `response.cancel` 是超出文档的用法——实测可用,但无官方语义保证(升级模型时是回归风险点)。官方指定的打断路径就一条:server_vad。

VAD 默认值(指南参数表):`turn_detection.threshold` 默认 **0.85**;`prefix_padding_ms` 默认 **333**;`silence_duration_ms` 可配 **0–10000ms**("How long the user must be silent (in ms) before the server ends the turn");`idle_timeout_ms` 默认 null;`turn_detection: null` = 手动轮次(我们现状)。**xAI 没有 semantic_vad**(指南只有 server_vad;relay.py:1369 的 semantic_vad 是 OpenAI 分支的)。

## ⑤ 计费:两处理解有误 + 多处证实

规格页原文:
> "$0.05 / min of audio sent or received ($3.00 / hr)"
> "$0.004 per conversation.item.create event"
> "function_call_output items (server-requested tool results) are not billed"
> "response.create is not a billable event"
> 模态:"Text, Audio → Text, Audio";并发 "100 per team";会话上限 "120 minutes";区域 us-east-1

- ✅ 证实:**per-response instructions 免费**(response.create 不计费,且无 token 维度)——relay.py:1540 注释的判断正确。
- ✅ 证实:静默停推省的是真钱(音频按 "sent or received" 分钟计,不按连接时长)。
- ❌ 纠正 1:**工具调用不计费**。relay.py:1540 注释"计价=音频时长+item 条数+工具次数"里的"工具次数"不存在——function_call_output 明文不计费。账本 `tool_calls` 维度可留作观测但不是钱。
- ❌ 纠正 2:relay.py:2020 的"按连接≈$conn_min×0.05"估价与官方口径不符,官方只按音频分钟;正确估价 = audio_min×0.05 + text_items×0.004。
- 💸 隐性成本:**每条 `conversation.item.create` 都是 $0.004**——直喂图的图 item、状态 system 消息都在计费,而规格页模态表证实**无 image 输入**(与实测"input_image 收下但看不见"互相印证)→ 给 Grok 喂图 = 纯付费喂空气。
- ⏱ 硬限:**单会话 120 分钟上限** → 长伴读通话必然被掐,resumption 重连不再是可选项。

## ① 指南推荐但我们没做的实践

1. **force_message**(未用):"Use `force_message` to make the agent speak a **hard-coded, TTS-synthesized line** without involving the model.";"Do NOT send response.create — the force_message IS the turn."——工具在飞期的垫话(豆包架构里的「我去查」套子)在 Grok 上可以零模型、零往返地用 force_message 实现。
2. **麦克风采集不等 WS open**:"Do **not** wait for the WebSocket `open` event before starting to collect microphone samples." + "Initiate the WebSocket connection … **as early as possible** — ideally when the voice interface loads"。前端 rc-voicecall.js:2438-2452 getUserMedia/worklet 确实先于 new WebSocket(顺序对),但上游 wss://api.x.ai 是点通话才连——官方建议界面加载/开麦界面时就预连。
3. **resumption 重连流程**(enabled 已开,续接未做,即 #290):"capture the server's `conversation.created.conversation.id` and pass it back as `?conversation_id=<id>` on reconnect";"**Opt-in both ways.** No history replays unless the resuming session also sends `resumption.enabled: true`";"History is dropped after 30 minutes of inactivity";replay 范围 = "user and assistant transcripts, assistant tool calls, and your `function_call_output` results"。
4. 已对齐的:24k PCM 格式匹配("Match input/output format (24 kHz PCM) to avoid resampling")、流式播放("Stream output audio deltas … instantly")、工具时序、播放完再 create——都已做。

## 延迟差距的官方视角

我们 turn 延迟 ≈ 0.8s hangover + 0.45s 反悔窗 + 0.5s 尾静音 append(relay.py:1529,文档未要求,turn_detection:null 只需 commit)+ 网络 ≈ 1.75s+ 起步,对比 GPT2.1 semantic_vad 的 0.5-1s。官方给的低延迟路子就是 server_vad(silence_duration_ms 可压到几百毫秒)+ barge-in 全托管,代价是持续上传音频按 $0.05/min 全程计费(~$3/hr),与静默停推互斥——这是一个明码标价的"手感换钱"开关。

--- GAPS ---
• 【P0|语义】per-response instructions 是替换不是追加:每个用户轮我们都发 response.instructions(relay.py:1550),该轮 session instructions 全文(see_ink 铁律/联网工具声明/禁假装贴图/写操作先声明/失败别重复/wait_for_user/生词表/插图描述/回答语言)被一句话人设覆盖,行为规则只在工具续答轮生效。官方依据:"Override the session-level system prompt for a single response" + "The override applies only to this response — subsequent responses revert to the session instructions."(tools 字段不受影响,但写在 instructions 文本里的工具行为规则该轮失效)。改法二选一:A(推荐,反正免费无 token 计价)_grok_commit 里把 _oa_instructions(book,file_rel,page) 全文 + 增量状态拼进 response.instructions,替换现在那句短人设;B 只在 book._dirty(状态变了)时才带 instructions,平时发裸 response.create 让 session instructions 生效。

• 【P0|钱】给 Grok 喂图是付费喂空气:规格页模态 "Text, Audio → Text, Audio" 无 image(印证实测无视觉),而 "$0.004 per conversation.item.create event" 意味着每张直喂图 + 每次焚旧图重喂都白花钱。改法:handle_openai 的 grok 分支禁用直喂图/焚旧图逻辑(engine=='grok' 时 see_ink/see_page 只走文字描述回填 function_call_output——后者官方明文不计费),conversation.item.delete 留着无害。

• 【P1|钱】计费口径修正:官方 "function_call_output items (server-requested tool results) are not billed" + "response.create is not a billable event" → relay.py:1540 注释"计价=音频+item+工具次数"里工具次数不存在;relay.py:2020 "按连接≈$conn_min×0.05" 与官方"$0.05 / min of audio sent or received"不符。改法:grok-usage.json 估价改 audio_min*0.05 + text_items*0.004,删按连接估价行,tool_calls 降级为纯观测指标;注释同步改。

• 【P1|延迟/打断手感】官方唯一 barge-in 路径是 server_vad("Enable server_vad for automatic, natural barge-in."),手动 response.cancel 无文档保证(升级模型时的回归风险点)。要对齐 GPT2.1 手感可加实验开关:session.turn_detection={type:'server_vad', threshold:0.85(默认), prefix_padding_ms:333(默认), silence_duration_ms:~400},本地 VAD/反悔窗/commit/尾静音全撤,打断交服务端;代价=持续上传全程按 $0.05/min 计费(~$3/hr)与静默停推互斥,做成设置项让用户选"手感/省钱"。保守替代:维持手动但压缩 hangover 0.8→0.5s、反悔窗 450→300ms,并试撤 relay.py:1529 的 0.5s 尾静音(turn_detection:null 官方只要求 commit,尾静音是我们自加的,直接贡献 0.5s 延迟且计费)。注意 xAI 无 semantic_vad,别照搬 OpenAI 分支配置。

• 【P1|工具节奏】force_message 未用:"Use force_message to make the agent speak a hard-coded, TTS-synthesized line without involving the model." + "Do NOT send response.create — the force_message IS the turn." 改法:tools_n 从 0→1(首个工具出飞)且预估耗时长(web_search/deep_think/see_ink)时发 force_message 垫话("稍等,我查一下"),零模型往返即时出声,替代现在的静默等待;可配 interruptible(官方:False 时 "caller audio is dropped until playback completes")。注意发了 force_message 后本轮不要再发 response.create,等工具回填完的末位 create 照旧。

• 【P2|resumption 落地=task#290】官方确切流程已齐:捕获 conversation.created.conversation.id → 断线重连 URL 加 ?conversation_id=<id> 且重发 resumption.enabled:true("Opt-in both ways. No history replays unless the resuming session also sends resumption.enabled: true"),30min 过期,replay 含双方转写+tool calls+function_call_output。且规格页 "Max session duration: 120 minutes" 是硬上限 → 长伴读必须做。改法:relay 收到 conversation.created 时把 conversation.id 存进 _vg,上游 WS 断开(或临近 120min)时自动重连带 conversation_id + resumption.enabled,重连期间前端只提示不掉线。

• 【P2|首字延迟】官方:"Initiate the WebSocket connection … as early as possible — ideally when the voice interface loads" + "Do not wait for the WebSocket open event before starting to collect microphone samples." 前端采集顺序已对(rc-voicecall.js:2438 getUserMedia 先于 :2452 new WebSocket),但 Pi→wss://api.x.ai 的上游连接是点通话才建。改法:长按/侧栏打开语音面板时预连上游(或至少预解析+TLS 预热),用户说第一句前连接已就绪;配合 resumption 复用 conversation_id 预连也不丢上下文。



======================================================================
AGENT #2
======================================================================
## 权威源
- **协议参考页**:https://docs.x.ai/developers/rest-api-reference/inference/voice
- **完整机器可读 schema(该页脚注指向,最权威)**:https://docs.x.ai/voice-realtime.ws.json —— 95KB,含 9 个客户端事件、37 个服务端事件的逐字段 JSON Schema + exampleFlow。已存本地 `/tmp/claude-1000/-home-bwicarus-claude/5ca50850-ce9a-439d-8fc3-1c221f2fa1ba/scratchpad/ws.json`
- **指南页**(补充语义):https://docs.x.ai/developers/model-capabilities/audio/voice-agent

## 协议全貌(与我们现状逐项核对)

### session 字段(官方 session schema 只有这 9 个 key)
`model / instructions / reasoning / voice / turn_detection / resumption / audio / tools / replace`
- ✅ 我们对的:`voice` 顶层、`audio.output.speed`("Range: 0.7–1.5. Default: 1.0")、`audio.input.transcription.{language_hint,keyterms}`("Max 100 terms, each up to 50 characters. **Can be updated mid-session**")、`reasoning.effort`(enum 只有 `high|none`,default high)、`resumption.enabled`、pcm 24k(官方最佳实践原话:"Match input/output format (24 kHz PCM) to avoid resampling")。
- ⚠ 我们发但**官方 schema 不存在**的字段(relay:1362-1377,grok 分支未裁):`"type":"realtime"`、`output_modalities`、`tool_choice`、`parallel_tool_calls`;transcription 里的 `model:"grok-transcribe"` 在 ws.json 的 transcription schema 里**没有**(只有 language_hint/keyterms),但 `.updated` 事件描述又写 "Only emitted when `audio.input.transcription.model` is set to `grok-transcribe`" ——官方自相矛盾,实测发了有事件,保留现状但记档。
- ⚠ **tools 官方形制是嵌套的**:`{"type":"function","function":{name,description,parameters}}`(Chat-Completions 式),我们发扁平 OpenAI-Realtime 式(relay:1348-1350)——实测能跑(兼容层收了),但排查工具怪象时这是第一个该归一的字段。tools.type enum 还有 **`web_search` / `x_search` / `file_search` / `mcp`** 四种服务端托管工具(37 事件里有整套 `mcp_list_tools.*` / `response.mcp_call.*` 与之配套)。
- **`replace`(未用)**:"Spoken-text find-and-replace map applied to the model's output before TTS… Changes only the spoken audio, not the transcript the user sees. The applied map is echoed back on `session.updated`."
- **turn_detection**:`server_vad` 完整字段:`threshold`(0.1-0.9,default **0.85**)、`silence_duration_ms`(0-10000,"Shorter values respond faster but may interrupt pauses")、`prefix_padding_ms`(default **333**)、`idle_timeout_ms`("server proactively re-engages the user… emitting `input_audio_buffer.timeout_triggered` and generating a check-in. Re-arms after every response")。官方最佳实践原话:"**Enable `server_vad` for automatic, natural barge-in.**"

### response.create(我们用法✅,但漏了 metadata)
schema 三字段:`modalities` / `instructions` / `metadata`。
- `instructions`:"Per-response system prompt override. When set, this **replaces the session-level `instructions` for this response only** — subsequent responses revert" ——我们 117/118 的每轮状态注入(relay:1539-1552)与官方语义完全一致 ✅。
- `metadata`(未用):"Developer-provided key-value pairs… **echoed back on `response.created` and `response.done`**… Useful for correlating responses with what triggered them. Up to 16 pairs";response.created 里 "`null` for responses not triggered by a client `response.create`" ——这是双响应/竞态取证的官方钩子。

### response.cancel
"`response_id`: Optional. The ID of the response to cancel. If not provided, cancels the current in-progress response." + "In VAD mode, interruptions are automatic — use this for manual cancel in non-VAD mode." 我们手动模式用法 ✅,但没用 response_id,也没按 response_id 丢弃 cancel 后在飞的音频 delta(response.created 明言 "Audio deltas from this turn share the same response_id")。

### input_audio_buffer 语义
- `commit`:"Only available when `turn_detection` type is `null`. **Confirmed by `input_audio_buffer.committed`**";committed 载荷含 **`item_id`(新建 user message 的 ID)+ `previous_item_id`** ——我们没监听。
- `clear`:"discard any pending audio data without committing" ——我们没用(反悔窗设计下确实不需要)。
- `append`:"The server does not send back a corresponding message." ✅

### 转写事件
- `.updated`:"cumulative transcript which **may have corrections** to previous updated transcripts — this is different from a transcript delta",载荷 `{item_id, transcript}` ——我们按 item_id 覆盖式处理 ✅(112 规范与官方一致)。
- `.completed`:`{item_id, transcript}` 定稿 ——我们即时定稿+updated debounce 兜底 ✅。

### 打断/截断
`conversation.item.truncate` **required: [type, item_id, content_index, audio_end_ms]**;`audio_end_ms`:"How many milliseconds of audio **the client has actually played back** before the interruption. Audio and transcript after this point is **removed from the conversation context**." 确认事件 `conversation.item.truncated` 还带 **`transcript`**("The truncated transcript text… Useful for updating the displayed transcript in the client UI after an interruption. **xAI extension**")。
→ **我们 relay 有完整 truncate 管线(played_ms 精确值+600ms 字节兜底,relay:1719-1727、1866-1879),但 pend_trunc 只在 `input_audio_buffer.speech_started` 处理器里赋值——该事件官方明言 "Only available with server_vad turn detection",而 grok 分支 turn_detection=null,本地 VAD 打断走 `_grok_turn_start`(relay:1565),那里只 cancel+清播放,从不设 pend_trunc → grok 通话从没发过一次 truncate。**

### force_message(xAI 扩展,未用)
`conversation.item.create` 的第 4 种 item 类型:"Make the agent speak a **hard-coded, TTS-synthesized line (not model-generated)**. The server synthesizes the text, injects a full response lifecycle (`response.created` → audio deltas → `response.done`), and records the utterance in conversation context as an assistant message. **Do not send `response.create` after this — the force message is the complete turn.**" 指南页另示例 `"interruptible": false` 字段(合规话术不可打断)。

### resumption(已 enable,续接机制官方已写明)
"Caches conversation turns **keyed by the `conversation_id` query parameter** and replays them on reconnect";指南页:捕获 `conversation.created.conversation.id` → 重连 `wss://…/realtime?conversation_id=<id>` → **重连侧必须再发 `resumption.enabled: true`** → 回放 "user and assistant transcripts, assistant tool calls, and your `function_call_output` results";"History is dropped after **30 minutes** of inactivity"。
→ 我们 first-frame 只认 `session.created`,等 session.updated 的 while 循环把 `conversation.created` **静默吞掉**(relay:1978-1988),conversation.id 从未落手——续接的第一块砖没捡。

### error 形态
`error.type` enum:`invalid_request_error / invalid_event / internal_error / timeout / **max_duration**`("for exceeding maximum conversation duration"),另有 `code / message / param / event_id(肇事客户端事件ID)`。"**Most errors are recoverable and the session stays open.**" ——我们只做了 cancel 噪声过滤,没对 max_duration/timeout 分支处理(30min 会话上限撞上就是死当,而它+resumption 本可无缝续)。

### 其它已核对
- exampleFlow 顺序:`session.created` → `conversation.created` → `session.update` → `session.updated` ✅ 与我们等待逻辑兼容。
- `response.done.usage` = `{input_tokens, output_tokens, total_tokens}` ——xAI **有** token 用量上报(计价无 token 维度≠协议不报);我们现在 grok 的 usage 会误入 `_oa_log_usage` 写进 openai-usage.json(relay:1950-1953)。
- 音频格式 enum 含 **`audio/opus`**("each `audio` or `delta` payload contains one raw Opus packet",24kHz)——Pi↔xAI 广域网腿可从 base64-PCM(~512kbps)降到 ~32kbps。
- `conversation.item.create` 的 message role enum = `user|assistant|system` ——我们的 system 状态注入**合法** ✅;另支持顶层 `previous_item_id` 定点插入、`function_call`+`function_call_output` 成对**回灌工具历史**(重连恢复的官方姿势)。
- 前端时序官方指引:"Do **not** wait for the WebSocket `open` event before starting to collect microphone samples" ——rc-voicecall.js:2438→2452 是先 await getUserMedia 再建 WS 的串行。
- 实测钉死的三点(恒纯语音/无视觉/completed 多发)与官方一致:schema 无 input_image content 类型(只有 input_text/input_audio/text/audio),视觉确实不存在;output_modalities 不在 session schema 里,被忽略合理。

--- GAPS ---
• 【P0|打断上下文断裂:grok 从不 truncate】问题:truncate 管线只挂在 input_audio_buffer.speech_started 处理器(voice_realtime_relay.py:1848-1879),官方明言该事件 "Only available with server_vad turn detection",grok 分支 turn_detection=null → 本地 VAD 打断走 _grok_turn_start(:1565)只发 response.cancel+前端450清播放,pend_trunc 永远不设,played_ms 回报(:1719)白收——被打断的回答**全文(含用户没听到的尾巴)留在会话上下文**,模型以为用户听完了,追问对不齐;这是打断手感与 GPT2.1 的隐性差距。官方依据:conversation.item.truncate required=[item_id,content_index,audio_end_ms],audio_end_ms="How many milliseconds of audio the client has actually played back before the interruption. Audio and transcript after this point is removed from the conversation context."。改法:_grok_turn_start 里复制 speech_started 的那段——if played['id']: pend_trunc 赋值+played 清零+起 0.6s 字节兜底 task;其余(played_ms 精确截)现成管线自动接上。

• 【P0|resumption 断线续接:机制官方已写全,我们连 conversation.id 都没存】问题:sess 已发 resumption.enabled=true(:1389)但 first-frame 只认 session.created,等 session.updated 的 while 循环(:1978-1988)把 conversation.created 静默吞掉,重连续接(#290 二期)无从做起;30min 会话上限(error.type=max_duration)撞上=死当。官方依据:resumption "Caches conversation turns keyed by the conversation_id query parameter and replays them on reconnect";指南:捕获 conversation.created.conversation.id → 重连 URL 加 ?conversation_id=<id> → 重连侧必须再发 resumption.enabled:true → 回放 "user and assistant transcripts, assistant tool calls, and your function_call_output results","History is dropped after 30 minutes of inactivity"。改法:①等待循环里 t0=='conversation.created' 时存 conversation.id;②ows 异常断开/收到 error.type in (max_duration,timeout) 时:重建 websockets.connect(XAI_RT_URL+model+'&conversation_id='+cid),重发同一份 session.update(含 resumption.enabled),前端无感(bws 不动);③工具历史已由服务端回放,不用自己回灌。

• 【P1|force_message 未用:工具等待/深度思考的空窗有官方零成本填法】问题:grok 工具轮(尤其 deep_think 最长 180s)期间全程静默,用户不知道助手在干活——工具节奏是验收项之一。官方依据:conversation.item.create item.type=force_message = "Make the agent speak a hard-coded, TTS-synthesized line (not model-generated)… injects a full response lifecycle… records the utterance in conversation context as an assistant message. Do not send response.create after this — the force message is the complete turn."(xAI extension;另有 interruptible:false 字段)。改法:在 _tool() 开头(或仅 deep_think/预计>2s 的工具)发 {type:'conversation.item.create', item:{type:'force_message', role:'assistant', content:[{type:'output_text', text:'我去查一下,稍等。'}]}},不跟 response.create;注意它自带 response.created→done 生命周期,_vg.busy/aEnd 会被正常驱动,工具回填的续答等播完的现有仲裁(:1498-1501)自动兼容。开场问候同法可即时出声。

• 【P1|应答延迟:server_vad 是官方推荐路,至少该拿账单数据做一次裁决】问题:我们 0.8s hangover+0.45s 反悔窗+commit/create RTT ≈1.5-2s,验收目标 GPT2.1≈0.5-1s;当年选本地 VAD 的唯一理由是「静默停推省钱」,但该前提未经账单验证——官方定价是 $0.05/min 平价制,若按**连接分钟**计费则静默停推一分钱不省,本地 VAD 纯亏延迟。官方依据:最佳实践原话 "Enable server_vad for automatic, natural barge-in";turn_detection.silence_duration_ms(0-10000,"Shorter values respond faster but may interrupt pauses")、threshold default 0.85、prefix_padding_ms default 333(替代我们 0.6s 预滚)、VAD 模式下 commit/response.create/打断全自动(response.cancel 文档:"In VAD mode, interruptions are automatic")。改法:①先对照 state/grok-usage.json 两口径 est_by_audio_usd vs est_by_conn_usd 与 console 实扣,定计费维度;②若按连接计费→切 server_vad(silence_duration_ms≈600-700 起调),砍掉 0.45s 反悔窗+commit RTT,本地 RMS 只留给前端静麦指示;每轮状态注入改挂 response.created(metadata==null 的自动响应无法带 per-response instructions——改回对话内 system item 或接受 session instructions);③若确按音频分钟计费→保留现架构,但把 hangover 收到 0.5-0.6s、反悔窗收到 0.25-0.3s,超触发靠 truncate 兜底(P0 第一条修好后超触发的代价大幅下降)。

• 【P1|grok 状态注入双通道重复烧钱(漏 continue)】问题::1696 的 grok 拦截块(page/state/ink 只标 _dirty,注释明言 117 用 per-response instructions『替代』item 注入)**没有 continue**,落穿到 :1733/1753/1768 的通用 elif——每次翻页额外同步拉一遍 ctx+发 1200 字 system item、每次选中/圈画也各发一条 item($0.004/条 text_items 计价),内容又会在下一轮 response.instructions 里再注入一遍=双份上下文+双份钱。官方依据:response.create.instructions "replaces the session-level instructions for this response only"(单通道已足);conversation.item.create role enum 含 system(通道合法,但重复)。改法:grok 拦截块内补齐 book 字段落地(state→book['sel']/_sel_fp、ink→book['ink_strokes']、page→现有 _refresh_pt+book['page']=np)后 continue,通用 elif 不再执行;instructions 注入照旧。

• 【P2|cancel 后在飞音频尾巴:按 response_id 丢弃迟到 delta】问题:手动 cancel(_grok_turn_start)到 xAI 真停之间在飞的 response.output_audio.delta 仍被转发,前端 450 清完队后又被重新入队=打断后偶发『尾巴音』。官方依据:response.created "Audio deltas from this turn share the same response_id";response.output_audio.delta 载荷含 response_id;response.cancel 可带 "response_id: Optional. The ID of the response to cancel";response.done.status enum 含 cancelled。改法:down() 里存当前 response.id(response.created 时),cancel 时记入 cancelled_ids,后续 delta 若 response_id ∈ cancelled_ids 直接丢,收到对应 response.done(status=cancelled)后清除;cancel 事件顺带带上 response_id 精确指靶。

• 【P2|response.create metadata:双响应/竞态取证的官方钩子】问题:『一句话答好几遍』类问题当前靠 stderr [grok-diag] 时序肉眼对——协议原生支持关联。官方依据:response.create.metadata "echoed back on response.created and response.done… Useful for correlating responses with what triggered them. Up to 16 pairs";response.created.metadata "null for responses not triggered by a client response.create"。改法:三处 create 各打标——turn_end 提交 {src:'turn'}, 工具续答 {src:'tool:<name>'}, 打字 {src:'text'};down() 在 response.created 读回 metadata:src 为 null 或意外值即取证到『幽灵响应』,pend_resp 竞态也能按标签精确对账。

• 【P2|conversation.item.truncated.transcript 未消费:字幕/落库与用户实听不符】问题:打断后 cur_a 已积累的全文在 response.done 落 _vlog('a'),侧栏气泡也是全文——但用户只听到一半。官方依据(xAI extension):conversation.item.truncated 载荷含 transcript="The truncated transcript text (up to the truncation point). Useful for updating the displayed transcript in the client UI after an interruption."。改法:down() 监听 conversation.item.truncated,用其 transcript 覆盖该轮的 cur_a/_vlog 记录,并给前端发一条字幕修正事件(现有 550 通道加 replace 语义)。依赖 P0 第一条先修(不发 truncate 就没有 truncated)。

• 【P2|keyterms/language_hint 可 mid-session 更新,我们钉死在连接时 5 个词】问题::1386 keyterms=书名 stem+『这一页/翻页/做卡片/笔记』4 固定词,翻页后本页生词/专有名词不进 ASR 偏置——转写热词的官方半上限(100×50字符)几乎全浪费。官方依据:keyterms 与 language_hint 字段描述均标注 "Can be updated mid-session."。改法:翻页的 _refresh_pt 里顺手发一条只含 audio.input.transcription 的 session.update:keyterms=[书名+固定词+book['vocab'][:80]];注意 session.update 是整包语义风险低的字段级更新,先实测只发 transcription 子树不会重置其它配置(等 session.updated 确认)。

• 【P2|托管工具 web_search/x_search 未用:联网能力白放着】问题:grok 分支现在对模型声明无联网(㉑批次『联网能力声明』),用户问时新问题只能靠 deep_think 绕。官方依据:session.tools[].type enum = function | web_search | x_search | file_search | mcp(服务端托管,xAI 自己执行,另计费);配套服务端事件 mcp_list_tools.* / response.mcp_call.* 已在协议里。改法:tools 里追加 {"type":"web_search"} 实测(计价未公开,先小流量+账本观察);中期评估 {"type":"mcp"} 指向我们自己的 bwicarus-app MCP(nginx /mcp + Bearer)——33 个 function 工具可整体下沉为 xAI 直调,省 relay 往返一跳,但要先核 mcp 工具字段(server_url/鉴权头)与安全面。

• 【P3|tools 形制与未文档字段:排障时第一批归一项】问题:我们 tools 用扁平 OpenAI-Realtime 式 {type,name,description,parameters}(:1348),官方 schema 是嵌套 {type:'function',function:{...}};session 里还带着 xAI schema 不存在的 type:'realtime'/output_modalities/tool_choice/parallel_tool_calls(grok 分支未裁,:1362-1377)——实测被兼容层容忍,但 error.type enum 里专有 invalid_event,且 parallel_tool_calls:false 大概率被忽略(这正是要靠 tools_n 在飞计数的原因)。官方依据:ws.json session.properties 仅 9 键;tools items schema 见 findings。改法:grok 分支把 tools 转嵌套式、裁掉 4 个幽灵字段;不改行为,纯降未定义行为面——若哪天工具选择变怪,这是第一嫌疑清单。

• 【P3|committed/added 事件未监听 + usage 记错账本】问题:①input_audio_buffer.committed 带 item_id("ID of the newly created user message item")+previous_item_id,可把转写 item_id 与轮次硬关联并确认 commit 成功,现在丢弃;②response.done.usage={input_tokens,output_tokens,total_tokens}——xAI 有 token 上报,适合监控上下文膨胀(何时删旧 item),但现在 grok 的 usage 落进 _oa_log_usage 写 openai-usage.json(:1950-1953)污染 GPT 账本。改法:committed 至少 stderr 记 item_id;usage 按 engine 分流,grok 写进 state/grok-usage.json 加 tokens 字段。

• 【P3|音频传输可换 audio/opus:WAN 带宽 10 倍降】问题:Pi↔xAI 走 base64 PCM 24k(~512kbps 单向),iPad 弱网时延迟抖动直接吃在 WAN 腿上。官方依据:audio format enum 含 audio/opus("for base64-encoded raw Opus packets (24 kHz)","each audio or delta payload contains one raw Opus packet")。改法:Pi 侧用现成 av(aiortc 依赖)做 opus 编解码,session.audio in/out format.type 换 audio/opus;浏览器⇄Pi 一段不动。收益主要是弱网稳定性,带宽不紧张可缓做。

• 【P3|通话建立延迟:官方明示并行初始化】问题:rc-voicecall.js:2438 先 await getUserMedia 再 :2452 建 WS,relay 又在浏览器 WS 建好后才连 xAI——三段串行都算进『按下电话到能说话』。官方依据:"Initiate the WebSocket connection (including authentication) as early as possible… Do not wait for the WebSocket open event before starting to collect microphone samples."。改法:前端 getUserMedia 与 WS 建立 Promise.all 并行(预滚 deque 本来就兜句首);更进一步可在侧栏打开时预热 relay→xAI 连接(但注意 xAI 连接分钟计费,预热空连有成本,先并行化零成本部分)。

• 【P3|idle_timeout_ms 主动再接话:伴读场景可选体验位】问题:用户长时间不说话,助手永远干等——伴读场景『读到哪了/要不要继续』的主动关怀没有。官方依据:turn_detection.idle_timeout_ms="the server proactively re-engages the user if no speech is detected for this many milliseconds after the assistant finishes responding, emitting input_audio_buffer.timeout_triggered and generating a check-in. Re-arms after every response."。改法:仅在切 server_vad 后可用(挂在 turn_detection 下,手动模式无 VAD 无从计时);若采纳 P1 的 server_vad 切换,可设 idle_timeout_ms≈120000 做轻量 check-in,并监听 timeout_triggered 在字幕区提示『助手主动询问』。

• 【P3|replace 发音替换表:书名/术语读音一次配平】问题:TTS 读错书名/专有名词/日语汉字读音时现在无手段(只能改 prompt 碰运气)。官方依据:session.replace="Spoken-text find-and-replace map applied to the model's output before TTS… matched case-insensitively on whole-word boundaries… Changes only the spoken audio, not the transcript the user sees. The applied map is echoed back on session.updated."。改法:session-config 加 rt_grok_replace 字典(书名→注音写法等),session.update 时带上;字幕不受影响,零风险。



======================================================================
AGENT #3
======================================================================
## xAI 官方 cookbook `voice-examples/agent/` 源码精读(web=WS 版 + webrtc 版)

源码基址:`https://raw.githubusercontent.com/xai-org/xai-cookbook/main/voice-examples/agent/`(下述行号即该文件行号;本地副本在 `/tmp/claude-1000/-home-bwicarus-claude/5ca50850-ce9a-439d-8fc3-1c221f2fa1ba/scratchpad/xai-voice/`)

### 0. 架构定调(比预期重要)
- **web 版的"后端"根本不代理音频**。`web/xai/backend-python/main.py` 只有一个 `/session` 路由:POST `https://api.x.ai/v1/realtime/client_secrets`,body `{"expires_after": {"seconds": 300}}`(L26/L110),返回 ephemeral token;浏览器**直连** `wss://api.x.ai/v1/realtime`。鉴权走 WS subprotocol:`new WebSocket(url, ["realtime", "openai-insecure-api-key.<ephemeralToken>", "openai-beta.realtime-v1"])`(`web/client/src/hooks/useWebSocket.ts` L182-186)。→ **xAI 有 ephemeral token 机制 + 浏览器 WS 直连是官方推荐形态**,音频路径零中转。注意 client 直连 URL **不带 `?model=`**(用默认模型)。
- **webrtc 版不是 RTP 媒体面**:音频仍是 base64 PCM16 JSON,只是隧道换成 WebRTC DataChannel(ordered:true),服务器(werift)再 WS 转发给 xAI(`webrtc/server/src/rtc-peer.ts` L171-208;`audio-processor.ts` 头注释:"PCM16 audio is sent via DataChannel instead of native WebRTC audio tracks. This avoids complex Opus codec dependencies")。→ **xAI 没有 OpenAI 式 WebRTC 媒体端点**;官方"低延迟"路线=WS 直连,不是媒体 WebRTC。
- `web/README.md` L183:"**Mobile browsers not officially supported.**" —— 官方示例没踩过 iOS,我们的 iOS 坑经验仍是自己的资产。

### ① 音频采集参数
`web/client/src/hooks/useAudioStream.ts`:
- **采样率=浏览器原生率,不强制 24k**:`new AudioContext()` 不传 sampleRate(L37),检测到的 native rate(48k/44.1k)直接写进 session 的 in/out format。README L163:"frontend automatically detects the browser's native audio sample rate and configures the session accordingly, **eliminating resampling overhead and improving audio quality**"。→ xAI 服务端接受任意采样率。
- getUserMedia 约束(L53-60):`{sampleRate: native, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}`——三开关全开,依赖浏览器 AEC(没有自己的回声处理)。
- 采集节点=**ScriptProcessorNode(bufferSize 4096)**(L75-76,已废弃 API;我们用 AudioWorklet 更好)。
- **chunk 聚合到 ~100ms 再发**(`CHUNK_DURATION_MS = 100`,L8;L98-125 手工拼帧),PCM16 转换用非对称缩放 `s<0 ? s*0x8000 : s*0x7fff`(`utils/audio.ts` L13)。→ 官方认为 100ms 粒度足够(消息数、base64/JSON 开销降 5 倍 vs 20ms)。
- 音量表=RMS(L86-91)。webrtc 版 `utils/audio.ts` 另有线性插值 resampleTo24kHz(L54-83)但实际未用(native rate 直传)。

### ② 播放调度
`useAudioStream.ts` L187-234:
- 队列=`Float32Array[]`;每 chunk 建一个 AudioBuffer+AudioBufferSourceNode,**靠 `source.onended` 回调链式播下一个**(L225-233),`currentPlaybackSourceRef` 存当前 source 供打断。
- **这是坑不是宝**:onended 链式=每 chunk 之间隔一次 JS 事件回调,chunk 边界必有微缝/爆音。我们 rc-voicecall 按 AudioContext 时间轴预排队(playPcm)是更对的做法,**不要抄**。
- 清播放 `stopPlayback`(L168-184):`source.stop()+disconnect`(try 包住,可能已停)→ 队列数组清空 → isPlaying=false。语义与我们 450 清队一致。
- 播放 buffer 用 `audioContext.sampleRate` 建(L215),因 session output rate=native rate,**播放零重采样**。

### ③ 打断处理(speech_started 时做什么)
`web/client/src/App.tsx` L66-91:
- 收到 `input_audio_buffer.speech_started` → **只做本地 `stopPlayback()`**(停当前 source+清队列),再插一个 "..." 占位用户气泡。
- **全程没有 `response.cancel`,也没有 `conversation.item.truncate`**——server_vad 模式下 xAI 服务端自动掐正在生成的响应,客户端只管闭嘴。示例也不做"已播毫秒回报/截断"记账(它的对话历史里 assistant 项会保留未播完的完整文本,是示例的已知粗糙点;我们的 played-ms + truncate 语义反而更精细)。
- **官方注释明示坑**:"avoid duplicates from **multiple speech_started events**"(L76-80)——同一轮 speech_started 可能多发,处理必须幂等。

### ④ 字幕事件用法
`App.tsx`:
- **助手侧**:`response.output_audio_transcript.delta` 逐 delta 追加进当前 assistant 气泡(L30-58),`response.done` 时置空 currentTranscript(L61-63)。没有音频同步节流,delta 即到即渲。
- **用户侧**(注意:示例 **session.update 里根本没配 transcription**,转写仍会出现):speech_started 插 "..." 占位 → `input_audio_buffer.committed` 结束当前指针 → **定稿从 `conversation.item.added` 拿**:遍历 `item.content[]` 找 `type==="input_audio"` 的 `.transcript`(L101-134)。
- **合并策略(值得抄)**:server_vad 下一次用户发言可能被拆成多个 committed item;示例把**连续的 user item 全部合并进同一个气泡,直到 assistant 开口**(L108-118,"consolidate all user transcripts into a single bubble until assistant responds"),而不是一 item 一气泡。

### ⑤ session 配置实值
两处一致(`useWebSocket.ts` L75-98 / `webrtc/server/src/xai-client.ts` L161-184),**总共只有 5 个键**:
```json
{"type":"session.update","session":{
  "instructions": "...(短人设,一句话)",
  "voice": "ara",                       // session 顶层(与我们 97a 定稿一致)
  "audio": {"input":  {"format": {"type":"audio/pcm","rate": <native>}},
            "output": {"format": {"type":"audio/pcm","rate": <native>}}},
  "turn_detection": {"type": "server_vad"}   // 无任何阈值参数;xAI 无 semantic_vad
}}
```
时序(两版一致,`xai-client.ts` L106-132/L190-192 注释最清楚):
1. WS open → 等 `conversation.created` 事件 → 才发 session.update;
2. 等 `session.updated` 确认 → 才发开场(`input_audio_buffer.commit` + `conversation.item.create`(input_text "Greet me briefly")+ `response.create`)→ 才通知客户端可以发音频("**This prevents mixing audio frame rates (sending audio before config is applied)**");
3. 客户端在 configured 前**丢弃**一切 `input_audio_buffer.append`(`useWebSocket.ts` L247-249)。
Ephemeral token:300s 过期 + 后端 slowapi 限 10/min(main.py L94/L110)。

### ⑥ 可借鉴 + 示例的坑(避雷)
**借**:原生采样率直传(免双端重采样)/ session.updated 前 gate 音频 / speech_started 幂等去重 / 用户字幕"合并到 assistant 开口"策略 / item.added 作转写定稿兜底 / ephemeral token+subprotocol 直连(未来砍掉 Pi 音频中转跳)/ 打断=纯本地清播放(server_vad 下不用发 cancel,佐证我们手动模式下 cancel 是必要的自担职责)。
**避雷**:onended 链式播放(有缝)/ ScriptProcessorNode(废弃)/ 公共 openrelay TURN(README 明说可加 10-15s 连接延迟;且 client ENABLE_TURN=true 与 server=false 默认还不一致)/ 示例无 truncate 记账(历史会带未播完文本)/ 移动端官方不支持 / webrtc 版 DataChannel 隧道并不比纯 WS 低延迟(音频还是 JSON base64,仅多了 ICE/stats)。

### 与我们现状的总对照
我们(`_server_deploy/voice_realtime_relay.py` grok 分支)在 cookbook 之上的部分(保留):33 工具+熔断、response.instructions 免费上下文、resumption、keyterms 热词、reasoning.effort、转写 .updated/.completed 双轨、played-ms+truncate、账本、静默停推、回声桥。cookbook 在我们之上的部分(差距)→ 见 gaps。延迟对比根因:cookbook=server_vad(xAI 侧判停即答,链路又是浏览器直连单跳);我们=本地 VAD 0.8s hangover+0.45s 反悔窗+0.5s 尾静音+iPad→Pi→xAI 双跳 ≈ 1.75s+2×网络。

--- GAPS ---
• 【采样率:改原生率直传,砍掉双端重采样】问题:rc-voicecall.js WS 引擎强制 24k 采集(iOS 原生 48k→worklet 降采样),播放又按 24k 建 buffer。官方依据:web/README.md L157-163『native sample rate auto-detected…eliminating resampling overhead and improving audio quality』;useAudioStream.ts L37 AudioContext 不指定 rate,session.update 把 native rate 写进 audio.input/output.format.rate(useWebSocket.ts L80-93),xAI 接受任意采样率。改法:rc-voicecall.js 建 AudioContext 去掉 {sampleRate:24000},握手包把 ctx.sampleRate 传给 relay;voice_realtime_relay.py L1365 附近 grok 分支 session.update 的 in/out rate 改成前端上报值(默认仍 24k 兜底);playPcm 按同 rate 建 buffer。收益:省 iPad CPU/避免降采样失真,输出 48k 音质更好。注意仅 grok 分支做(OpenAI 分支 rate 语义另查)。

• 【音频 gate 到 session.updated:防裸配置窗口期串帧率】问题:relay 连上 xAI 后是否在 session.updated 确认前就转发 mic 帧未设防(本地 VAD 静默不推能掩盖,但拨号即说话时首帧可能先于配置生效)。官方依据:xai-client.ts L190-192『Initial greeting will be sent after session.updated…This prevents mixing audio frame rates (sending audio before config is applied)』;useWebSocket.ts L247-249 在 configured 前直接丢弃 append。改法:relay grok 分支加 sess_ready flag,收到 session.updated 前丢弃(或缓冲≤1s)上行 append;开场 item.create/response.create 也移到 updated 之后(若尚未如此)。

• 【speech_started 幂等】问题:voice_realtime_relay.py L1848/L2474 的 input_audio_buffer.speech_started 处理(450 清播放/焚图/truncate 记账)若同轮多发会重复执行。官方依据:App.tsx L76-80 注释『avoid duplicates from multiple speech_started events』——官方明确此事件一轮可能多发。改法:话轮级 flag(_turn.n 或 played.id 维度)去重,同轮第二个 speech_started 只忽略;前端 450 清队本身幂等即可不动。

• 【用户字幕:合并到 assistant 开口 + item.added 定稿兜底】问题:我们定稿只靠 transcription .completed(已按 item 去重)+updated debounce;server_vad/多 commit 场景一次发言拆成多 item 时会出多个用户气泡。官方依据:App.tsx L101-134——定稿从 conversation.item.added 的 item.content[].transcript 取,且『consolidate all user transcripts into a single bubble until assistant responds』(连续 user item 并进一个气泡直到 assistant 回复)。改法:relay 增收 conversation.item.added,有 transcript 且该 item 未定稿时补定稿;前端字幕渲染把连续 user 条目合并显示(assistant 首个 delta 到达才另起气泡)。

• 【延迟预算:1.75s 静态开销压到 ≈0.9s,对齐 GPT2.1 手感】问题:0.8s hangover+0.45s 反悔窗+0.5s 尾静音串行,加 iPad→Pi→xAI 双跳,应答 1.5-2s(目标 0.5-1s)。官方依据:cookbook 全线 server_vad(session 只此一键,useWebSocket.ts L94-97)=判停即答零本地等待,且浏览器直连单跳——官方形态里这三段等待根本不存在。改法(渐进):a) 尾静音 0.5s→0.15-0.2s(手动 commit 不需要长静音喂 VAD,只为 flush);b) hangover/反悔窗收进 server-config 可调(0.8→0.6、0.45→0.3 实测);c) 激进项=投机提交:hangover 到点立即 commit+response.create,反悔窗内用户再开口则 response.cancel+新 item 继续(xAI 无 token 计价,偶发取消成本≈几秒输出音频),把 0.45s 体感归零——与现有 pend_resp 取消路径复用。

• 【二期架构:ephemeral token 浏览器直连,Pi 退成控制面】问题:音频面 iPad→Pi(relay)→xAI 双跳,Tailscale+Pi 转发抬高 RTT 且 relay 是单点。官方依据:web/xai/backend-python/main.py L26/L110——官方后端只造 client_secrets(expires_after 300s),浏览器 useWebSocket.ts L182-186 用 subprotocol ['realtime','openai-insecure-api-key.<token>','openai-beta.realtime-v1'] 直连 wss://api.x.ai/v1/realtime。改法:与 #283 sideband RtcController 同构——relay 增 /api/voice/grok-secret(限流 10/min 学官方),前端直连 xAI 收发音频,session.update/工具执行/账本/落库经现有控制 WS 走 Pi;response.instructions 注入改由前端发(或控制面代发 session.update)。这是把 Grok 追平 GPT2.1 WebRTC 手感的最大单刀,可先 flag 化并存。

• 【避雷:播放调度别学 cookbook】useAudioStream.ts L206-234 的 onended 链式播放每 chunk 间隔一次 JS 回调=可听微缝;我们 AudioContext 时间轴预排队(playPcm)是正确做法,保持不动。同理 ScriptProcessorNode(L75,废弃 API)不如我们的 AudioWorklet。此条是『确认现状正确』,防止未来『参照官方』时倒退。

• 【上行帧聚合(低优先级)】问题:我们 20ms/帧=50 msg/s,每条 JSON+base64 有 ~33% 体积开销,iPad 电量/Pi CPU 白耗。官方依据:useAudioStream.ts L8 CHUNK_DURATION_MS=100(README L162『Chunk Duration: ~100ms』)。改法:worklet 聚 2-3 帧(40-60ms)再发——保留本地 VAD 20ms 判定粒度,只合并网络发送;100ms 全抄会伤打断检测延迟,不必到位。

• 【TURN/webrtc 结论钉死进文档】问题:references/doubao-realtime-voice.md 的 Grok 章节若暗示『xAI WebRTC 版更低延迟』会误导后续 session。官方依据:webrtc/server/src/audio-processor.ts 头注释+rtc-server-README L289-302——官方 webrtc 版音频走 DataChannel JSON(非 RTP 媒体面),与 WS 同一协议只换隧道;rtc-README L153『TURN may add 10-15 seconds to connection time』且用公共 openrelay 凭据。改法:在 references 里补一段『xAI 无媒体面 WebRTC;低延迟正道=ephemeral token WS 直连;公共 TURN 别抄』,免得重蹈调研弯路。

• 【session.update 最小面确认+开场时序】问题:我们 grok 分支靠“裁 OpenAI 字段”逼近合法配置,属黑名单法,新字段进来仍可能整条被拒(已知 session.update 被拒=裸配置复读机)。官方依据:官方合法集合就 5 键(instructions/voice(顶层)/audio.in.format/audio.out.format/turn_detection),见 useWebSocket.ts L75-98;开场 greeting(commit+item.create+response.create)严格在 session.updated 之后(xai-client.ts L106-127)。改法:grok 分支改白名单构造(官方 5 键+已在 docs.x.ai 核实过的 transcription/keyterms/resumption/reasoning/speed),不再从 OpenAI sess 上 pop;开场寒暄若存在,确保挂在 session.updated 事件后。



======================================================================
AGENT #4
======================================================================
# xAI 官方 Cookbook WebRTC 示例源码精读(vs 我们的 Pi relay)

源:https://github.com/xai-org/xai-cookbook/tree/main/voice-examples/agent/webrtc(server=Node/Express+werift,client=React/Vite;全部源文件已拉到本地核对:`/tmp/claude-1000/-home-bwicarus-claude/5ca50850-ce9a-439d-8fc3-1c221f2fa1ba/scratchpad/xai-webrtc/`)

## 总体判断(先说结论)

**官方这个"WebRTC"示例不是媒体轨 WebRTC,是"DataChannel 当传输层"**:音频上下行全部是 base64 PCM16 塞在 JSON 里走一条 reliable ordered DataChannel,werift(纯 JS WebRTC)只提供 ICE/STUN/TURN 打洞和 DC。server README 原话:"**Why not use WebRTC audio tracks directly? Avoids complex Opus ↔ PCM16 codec conversion / Eliminates need for additional native dependencies**"。README 顶部自带免责声明:"**NOT PRODUCTION-READY WITHOUT ADDITIONAL HARDENING**"。

对齐验收目标而言,示例给出的**唯一大杠杆是:xAI Realtime 支持 `turn_detection: {type:"server_vad"}`**(xai-client.ts L180-183)——示例把端点判定完全交给云端,speech_started/stopped/自动 response 全由 xAI 发,打断=客户端只清播放、**全程不发 response.cancel**。这就是它能接近 GPT2.1 手感的原因;我们 1.5-2s 延迟里的 0.8s hangover+0.45s 反悔窗是自己加的固定成本。

## ① relay 端与 xAI WS 的交互(事件转发/缓冲/背压)

- **纯 verbatim 透传,零翻译层**:DC 收到的 JSON 原样 `xaiClient.sendMessage()`(rtc-peer.ts L197-213);xAI 来的每条事件(含音频 delta)原样 `dataChannel.send(JSON.stringify(message))`(rtc-peer.ts L223-236)。
- **零缓冲/零背压**:全仓无一处 `bufferedAmount` 检查;WS 未 open 时直接丢弃并打日志 "Cannot send message - WebSocket not open"(xai-client.ts L198-204)——静音期音频会无声丢失。
- **零重连**:xAI WS close 只打日志(rtc-peer.ts L242-244),会话即死;无 resumption。**我们的 resumption:{enabled:true}(relay:1389)已超官方基线**。
- **并行初始化**:xAI WS 连接与 WebRTC offer 创建并行不阻塞(index.ts L258-266 注释 "Initialize XAI connection in parallel (don't block offer creation)");信令 handler **先于**发 offer 注册(index.ts L268-270 注释 "prevents race condition where answer arrives before we're listening")。
- **配置时序(严格串行门控)**:`conversation.created` → 发 `session.update`(server_vad + audio in/out `{type:"audio/pcm", rate:协商值}` + instructions + voice)→ **等到 `session.updated`** → 才发开场白(`input_audio_buffer.commit` + `conversation.item.create(input_text:"Greet me briefly.")` + `response.create`)→ 经 DC 发自定义事件 **`xai.ready`**(xai-client.ts L102-128、rtc-peer.ts L246-255)。注释原话:"**This prevents mixing audio frame rates (sending audio before config is applied)**"。

## ② DataChannel 协议设计

- 一条 DC,名 `"xai-voice"`,`{ordered: true}`(reliable+ordered,rtc-peer.ts L171-173)——**对实时音频是反模式**(丢包时队头阻塞),官方接受了。双方都 createDataChannel 并同时监听 ondatachannel(谁先 open 用谁)。
- **线格式 = 原生 xAI Realtime 事件 JSON,唯一自定义消息是 `xai.ready`**。上行音频 `input_audio_buffer.append`(~100ms/块,useAudioStream.ts CHUNK_DURATION_MS=100),下行 `response.output_audio.delta`,都是 base64 PCM16 → 48kHz 时约 1.0 Mbps 下行,无 binary frame(+33% base64 开销)。
- **采样率协商(亮点)**:客户端探测 `AudioContext.sampleRate`(原生率,常 48k)→ `POST /sessions {sample_rate}` → 服务端夹到支持表 `[8000,16000,21050,24000,32000,44100,48000]`(index.ts L148,21050 疑为 22050 笔误)→ session.update 双向 format=该率 → **端到端零重采样**(采集、xAI、播放同率)。

## ③ 播放确认/状态机

- **唯一应用层门控 = `xai.ready`**:客户端收到才开麦(App.tsx L29-41),之前只建连不采集。无 per-chunk ack、无已播毫秒回报。
- **播放 = 每个 delta 一个 AudioBufferSourceNode,靠 `onended` 链下一块**(useAudioStream.ts L187-238),无累积时间轴调度、无 jitter buffer → 块边界可能有微缝。**我们的 playPcm 是 `playT` 累积 cursor + `start(t)` 调度且回报真实已播毫秒做 truncate(rc-voicecall.js:511-527、_reportPlayed)——两点都优于官方示例**。
- **字幕状态机(值得抄)**:assistant 用 `response.output_audio_transcript.delta` 原地追加末气泡,`response.done` 收口;user 在 `speech_started` 时先插 "..." 占位泡(末泡已是 user 则不重复插);**用户转写到达于 `conversation.item.added` 的 content[].transcript,且"只要末泡是 user 就一直合并进去,直到 assistant 回话"**(App.tsx L121-154 注释 "Consolidate all user transcripts into a single bubble until assistant responds")——官方对"committed/added 同轮多发"的解法是 **UI 层合并**而非 item_id 去重。另注意:示例 **完全没配 input_audio_transcription**,用户转写是 xAI 默认随 item.added 附带的。

## ④ 打断链路

- 全靠 server_vad:xAI 发 `input_audio_buffer.speech_started` → relay 透传 → 客户端 `stopPlayback()`(停当前 source+清队,App.tsx L85-111)。**全链路没有 response.cancel、没有 conversation.item.truncate、没有已播位置回报**——依赖 server_vad 自动掐生成;模型上下文里保留整条没播完的回答(上下文漂移),示例不管。
- **坑**:speech_started 之后仍在途的 `response.output_audio.delta` 到达会重新入队播出(playAudio 无轮次守卫)→ 打断后残尾音。

## ⑤ 与我们 aiortc 回声桥的可比性(用户判断确认)

**确认:官方示例全程没有真 RTP 音轨**。client `ontrack` 是空壳(useWebRTC.ts L174-178 注释 "For now, we'll use DataChannel for audio data"),server `ontrack` 只打日志(rtc-peer.ts L159-161),麦克风走 getUserMedia→ScriptProcessor→base64→DC。**我们的回声桥(浏览器真 Opus RTP ⇄ Pi aiortc)在"WebRTC 化"程度上超过 xAI 自家示例**;它的 relay 本质就是我们的 WS relay 换了个 DC 皮。回声方面它只靠 `getUserMedia` 的 `echoCancellation:true` 约束(useAudioStream.ts L53-61),没有任何额外处理——间接印证我们"外放场景 AEC 不可靠→半双工/回声桥"路线的必要性。relay 侧值得借鉴的只有:xai.ready 门控、handler-先于-offer、并行初始化、原生率直通、user 气泡合并、client getStats 分级(useWebRTCStats.ts L63-99:data-channel/transport/candidate-pair 三源取数;useWebRTC.ts L429-437 分级阈值 packetLost<10&&jitter<0.03=excellent /<50&&<0.05=good /<100&&<0.1=fair)。

## ⑥ 示例的坑(实录)

1. `POST /session` 拉了 ephemeral client_secrets(300s)但 **后续流程完全没用它**(useWebRTC.ts L244-259 拿到就扔)——直连版遗留代码。
2. 开场白前发**空 buffer 的 `input_audio_buffer.commit`**(xai-client.ts L107)——OpenAI 会报 buffer too small,xAI 显然容忍。
3. 支持率表里 `21050`(应为 22050,index.ts L148)。
4. 客户端 `ENABLE_TURN = true`(useWebRTC.ts L23)与 README "Default: false (line 13)" 自相矛盾;TURN 用公共 openrelay 凭据,自己警告"可能加 10-15s 连接时间"。
5. 采集用**已废弃的 ScriptProcessorNode**(我们已是 AudioWorklet)。
6. **客户端从不 trickle ICE**(onicecandidate 空实现,注释还写错 "sent automatically"),连通靠服务端 offer 里的 host/srflx + 服务端从入站 STUN 学到 peer-reflexive;双侧对称 NAT 会挂(我们 Tailscale 场景天然可达,同款做法可行)。
7. werift `getStats()` 基本不可用,服务端统计是摆设(rtc-peer.ts L311-340 自己打日志承认)。
8. `/sessions` 和 `/signaling` **无鉴权**(session_id 即全部秘密),只有 CORS。
9. 两条腿都无重连;打断后在途 delta 重入队(见④)。

--- GAPS ---
• 【延迟主杠杆】问题:我们说完→应答 1.5-2s,固定成本=本地 0.8s hangover+0.45s 反悔窗+手动 commit/response.create(voice_realtime_relay.py:1390 turn_detection=None、:1558-1619)。官方依据:示例证明 xAI Realtime 支持 turn_detection server_vad(xai-client.ts L180-183),端点判定全交云端、speech_started/stopped/自动 response 由 xAI 发,这正是 GPT2.1 级手感的来源。建议改法:做可回退实验分支——grok 分支 session.update 改 server_vad;保留 RMS 门+0.6s 预滚(静默停推不变:voiced 才推流,尾部本就补 0.8s hangover 静音,足够触发服务端 speech_stopped),删本地 endT/反悔窗/手动 commit+create;per-response instructions 因 auto-create 不可用,改为开口时(450)推轻量 session.update{instructions}(官方示例即在 session.update 带 instructions,且 xAI 无 token 计价=依旧免费,与现有 SP 指纹确认制同路)。代价:失去反悔窗的『句中停顿不切轮』;若 xAI 支持 silence_duration_ms 可调大折衷,实测不行再回退。

• 【打断残尾音 epoch 守卫】问题:打断(response.cancel+450 清播放)后,cancel 生效前已在途的旧轮 PCM 二进制帧到达前端仍被无条件 playPcm 入队(rc-voicecall.js:2477 `ev.data instanceof ArrayBuffer → playPcm`),可能播出残尾。官方依据:示例同款坑(App.tsx stopPlayback 清队后,后续 response.output_audio.delta 照样 playAudio 重入队,无轮次守卫)——官方都没防,说明这是该架构的固有暗坑而非我们独有。建议改法:relay 给 PCM 二进制帧加 1 字节轮次 epoch 头(或打断后丢弃音频直到下一个 response.created 才恢复转发);前端收 450 时记当前 epoch,旧 epoch 帧直接丢。先加 relay 侧『cancel 后丢弃到 response.created』最省事,前端零改动。

• 【session.updated 超时不该裸跑】问题:我们等 session.updated 超时后『继续,但配置状态未知』(voice_realtime_relay.py:1990)——裸配置=错音色/错采样率/无 instructions 的会话照常开聊。官方依据:示例严格门控,session.updated 确认前客户端连麦都不开(xai.ready 机制,rtc-peer.ts L246-255;注释明言防『混采样率』),error=配置被拒时直接收场。建议改法:超时改为重发一次 session.update 再等;二次超时 fail-fast 断开让前端走既有重连路径,不进入未知配置状态(我们 1976-1984 已有 error=收场逻辑,只补超时分支)。

• 【用户字幕同轮多气泡】问题:我们对 completed 同轮多发按 item_id 去重,但同轮多次 commit 产生多个不同 item 时仍会出多个 user 气泡。官方依据:App.tsx L121-154 官方解法是 UI 语义层合并——『只要末气泡是 user 就一直往里合并转写,直到 assistant 回话』(注释 Consolidate all user transcripts into a single bubble until assistant responds)。建议改法:侧栏对话流/字幕的 user 泡渲染加同款规则:assistant 开口前到达的所有 user 定稿转写合并进末 user 泡(空格拼接),item_id 去重照旧防重复。

• 【#287 getStats 遥测可直接抄官方阈值表】问题:回声桥 aiortc 是真 RTP 但我们没做质量遥测,弱网时无从判断该不该自动回退 ws 音频。官方依据:useWebRTCStats.ts L63-99 三源取数(data-channel/transport 取字节算码率、candidate-pair 取 RTT、inbound-rtp 取 jitter/packetsLost)+ useWebRTC.ts L429-437 四级分级(丢包<10 且 jitter<0.03s=excellent,<50/<0.05=good,<100/<0.1=fair,否则 poor),1-2s 轮询。建议改法:回声桥前端对 _abridge.pc 起同款 getStats 轮询(我们有真 inbound-rtp,数据比官方 DC-only 更有意义),连续 N 次 poor→自动 _abridgeStop() 回退 ws 音频并提示;顺手把码率/RTT 进 grok-usage 账本作观测。

• 【可选低优:采样率直通】问题:我们 AudioWorklet 固定 24k(前端重采样),iOS 原生 AudioContext 常为 48k。官方依据:示例让浏览器原生率直通 xAI 端到端零重采样(index.ts L143-167 协商+夹取支持表 [8000,16000,21050(sic),24000,32000,44100,48000];App.tsx L184-195 探测原生率)。建议改法:仅当追音质/省前端 CPU 时做——session audio format rate 改传前端上报的原生率,playPcm createBuffer 的 rate 跟着改;xAI 按分钟计价与字节无关=免费,但上行带宽约×2 且与豆包/GPT 分支的 24k 假设耦合,收益小,挂起即可。

• 【反向确认,不必做的事】问题:是否要参考官方示例做 relay 缓冲/背压/重连。官方依据:示例零缓冲零背压(无任何 bufferedAmount 检查)、WS 未 open 直接丢帧(xai-client.ts L198-204)、xAI WS close 仅打日志无重连(rtc-peer.ts L242-244)、werift getStats 不可用统计是摆设——官方基线远低于我们现状(resumption enabled、played_ms 精确 truncate、累积时间轴播放调度均已超出)。建议改法:#290(控制 WS 可恢复重连)按既定方案推进即可,本示例无参考价值;不要因『官方也没做』而砍掉我们已有的 truncate/回报机制——那是我们对齐 GPT2.1 的既有优势。



======================================================================
AGENT #5
======================================================================
## 审计结论(Grok=handle_openai(engine=grok) + rc-voicecall.js WS 路径)

以下按六维列出,全部基于当前源码逐行核对(`voice_realtime_relay.py` 简称 R,`rc-voicecall.js` 简称 F)。逻辑 bug 优先,行号为当前工作区版本。

### ① 打断手感

**A. 打断后残余音频/幽灵字幕——down() 无 response 门控(最影响手感)**。`_grok_turn_start`(R:1565-1575)发 `response.cancel`+450,前端清队(F:2492-2497);但 cancel 生效前**在飞的 `response.output_audio.delta` 继续到达**,R:1819-1843 无条件转发 → 前端刚清完队又被旧响应尾巴填回,用户开口后还能听到被取消回答的半句;同理 R:1844-1847 的 transcript delta 继续走 550,而 450 已把 `curAText` 清零(F:2495),旧响应尾巴文字变成"新气泡幽灵字幕"。GPT 2.1 的 WebRTC 路径由服务端 semantic_vad 即刻停流,没这个问题。**改法**:在 `response.created`(R:1938)记 `_vg["resp_id"]=(e.get("response") or {}).get("id")`;`_grok_turn_start` 发 cancel 时把该 id 放进 `dropped` 集合;delta/transcript.delta 处理里 `if e.get("response_id") in dropped: continue`,`response.done` 时清集合。

**B. cancel 竞态无兜底**。`pend_resp` 窗口内 cancel(R:1570-1575)后,服务器可能仍把 response 建出来 → `response.created` 到达置 `busy=True`,旧响应照播压过用户说话,现无 re-cancel。**改法**:`response.created` 处理里加 `if engine=="grok" and _vg["active"]: await ows.send(response.cancel)`(用户正在说话时到达的 created 必是该打断的)。

**C. 单帧触发打断,误触即毁回答**。R:1600 `_voiced = _rms(chunk) > 350`,单个 20ms 帧过阈就 `_grok_turn_start` → cancel+清播放,**不可逆**。咳嗽/键盘敲击/放杯子=回答被杀。**改法**:turn_start 需连续 2-3 帧过阈(40-60ms,手感无感知差异);已 active 的持续判定维持单帧。

**D. RMS 固定阈值 350 无自适应**。350≈-39dBFS:轻声/远麦低于阈值 → 永远开不了口(turn 不启动=全聋);嘈杂环境地板高于 350 → active 永不结束(`last` 每帧刷新,hangover 永不触发=永不 commit,同样表现为"没反应")。且 `_rms`(R:1519-1524)不去 DC——带直流偏置的麦会恒过阈。**改法**:滚动噪声地板(如最近 3s RMS 的 P20)+ 相对阈值 `max(350, floor*2.5)`;计算前减均值去 DC;出错返回 9999(=voiced)也应改为返回地板值。

**E. truncate 链路对 grok 是死代码**。`pend_trunc` 只在 `input_audio_buffer.speech_started`(R:1848-1868)里设置,而 grok `turn_detection=None` 永远收不到该事件;前端 450 后老实回报 `played_ms`(F:2493→R:1719-1727),relay 因 `pend_trunc["id"]=None` 直接丢弃 → **打断后从不发 `conversation.item.truncate`**,模型上下文里保留用户没听到的整段回答(与 GPT 2.1 行为不对齐,后续轮次模型会引用"用户没听过的话")。**改法**:`_grok_turn_start` 里镜像 speech_started 的逻辑(`if played["id"]: pend_trunc 设值 + 600ms 兜底 task`);若 xAI 不支持 truncate 会回 error 事件,可按 message 关键字静默。

### ② 应答延迟

现链路=0.8s hangover(R:1619)+0.45s 反悔窗(R:1560)+commit/create RTT+推理 ≈1.5-2s,vs 2.1 semantic_vad 0.5-1s。可压缩点:

**A. 两窗职责重叠**。hangover 防"句中喘气",反悔窗**也是**防句中停顿(回来即撤销提交)——保护冗余但延迟串行相加。hangover 可降到 0.45-0.5s,反悔窗不变:总保护窗仍约 0.95s(句中停顿 <0.95s 都能续轮),端到端省 ~0.35s。反悔窗撤销已实现"同轮继续不焚图不 cancel"(R:1605-1608),语义无损。

**B. 免费的伪语义 VAD**。grok-transcribe 的 `.updated` 累计全文在说话期间就流式到达(`_tr_pend`,R:1896-1899)。commit 前文本已在手:**若 `_tr_pend` 最新文本以 。?!/吗/呢/吧 结尾 → hangover 缩到 0.3s+反悔窗 0.2s;无终结标点(半句)→ 维持 0.8+0.45**。这是本地能做的最接近 semantic_vad 的自适应,零成本。

**C. 页面 fetch 阻塞音频循环拖延迟(见⑤C)**——翻页瞬间提问,VAD 判定被 HTTP 卡秒级。

**D. 尾静音 0.5s(R:1529)是数据不是墙钟**,不加延迟,不用动;但 hangover 期间 0.8s 静音全程上传计费(音频计费),缩 hangover 顺带省钱。

### ③ 工具节奏

**A. `tools_n` 卡死=会话级永久哑巴**。R:1486-1506:出飞递减(1490)在 `await ows.send(function_call_output)` **之后**、同一个 try 里——send 抛异常(ws 抖一下)→ `except: pass`(1505)→ 递减被跳过 → `tools_n` 永远 >0 → 此后**每个工具回填都走"暂不 create"分支(1495-1496),整场再也不会主动续答**(正是 119b 修过的症状换个入口复发)。**改法**:递减放 `finally`,或移到 send 之前。

**B. aEnd 等待不感知打断 → 双响应**。R:1498-1501:工具回填前 `sleep(aEnd-now)` 最长 20s;睡眠期间用户开口(turn_start 已把 aEnd 清零,R:1569)→ 用户 commit 发了 create(#1),工具睡醒**无条件再发 create(#2)**——竞态双响应/被拒。**改法**:sleep 前快照 `_turn["n"]`,睡醒后 `if _turn["n"] != snap or _vg["active"]: 跳过 create`(函数输出已入历史,用户新轮的响应自然会用到)。

**C. 同因异形:工具执行中(busy=False)用户说话** → commit+create(#1),工具完成后回填+create(#2),同样双响应。修法同 B(工具 create 前检查话轮代数)。打字输入路径(R:1801-1810)的 create 同样不查 busy/aEnd,次要。

**D. aEnd 估算系统性偏早**。R:1829 按 relay 收到字节即计,未含前端排队 0.02s 起步 + 网络在途 + playPcm 调度间隙;且前端"朗读灭"(F:513 丢音频)时 relay 仍按在播计算,工具白等最长 20s。**改法**:估算加 +0.3s 余量;359(response.done 转发)后前端可回报真实播完时刻校准(已有 359 事件通道)。

### ④ 字幕实时性

**A. completed 多发 → 学习日志重复**。xAI 已实测"completed 同轮多发";前端气泡靠同 iid 覆盖(F:444-446)兜住了,但 R:1887-1895 每个 completed 都 `_vlog("q")` → `voice-log` 同一句问话落多条,recall_study 的 digest 会把同一问题复述 N 遍。**改法**:completed 分支加 `if book.get("_q_done_iid") == _iid: 只发 451 不 vlog`(或维护最近 iid 集合)。

**B. 需实测确认**:手动轮次(turn_detection=None)下 `.updated` 是否在 commit **前**就流式到达——若 xAI 只在 commit 后才吐转写,用户句字幕要等 1.25s+ 才首现,与"字幕实时"目标冲突;若确实滞后,可用本地 interim(前端无)或接受现状。这是①B/②B 方案的前提,建议先在 grok-diag 里记 updated 首达时间戳验证。

**C. 幽灵字幕**(残余 transcript delta 污染下一轮气泡)见①A,同一个门控修掉。

### ⑤ 上下文注入

**A. 【最重】`response.instructions` 是覆盖不是追加**。OpenAI Realtime 语义(xAI 兼容):`response.create.response.instructions` **替换**本响应的 session instructions。R:1550 每个用户轮都带 `"你是简短口语化的伴读助手。当前第N页;本页正文…"` → **所有用户轮的实际生效 SP 只剩这一句**,`_oa_instructions` 里的语言策略(别用中文读音念日语)、see_ink 铁律、联网声明、工具诚实纪律、wait_for_user 指引**在每个用户轮全部失效**(只有工具续答轮的裸 create 还吃 session instructions)。这能直接解释工具选择/口音不稳。**改法**:把 `_oa_instructions` 的纪律段拼进每轮 response.instructions(xAI 无 token 计价,长度免费),或状态改走 system item、response.instructions 不用。

**B. grok 状态注入双路径(设计漂移,漏 `continue`)**。R:1696-1713 grok 块注释称"状态只标脏,开口时随 instructions 注入",但块尾**没有 continue** → page/state/ink 继续落进通用 elif 链(R:1733-1800):每次翻页/选中/圈画**又**注入一条 conversation system item。后果:(a) 与 per-turn instructions 内容重复;(b) xAI 按条计价 $0.004/item,而这些 item 不计入 `_gk["items"]`(只在 R:1803 text 路径计)→ 账本系统性少记;(c) `book["_dirty"]` 只在 R:1697 置、R:1551 弹出,**从未被读**=死旗;(d) 翻页双重 fetch(grok 块 `_refresh_pt` + 通用块 R:1737 又拉一次,2 次 HTTP/页)。**改法**:二选一——按注释意图在 grok 块尾加 `continue`(state/ink 的去重指纹逻辑要搬进 grok 块或 commit 时机);或承认双注入有价值,则删 `_refresh_pt`+`_dirty`,并把 item 计数入账。

**C. 通用 page 处理器阻塞音频循环**。R:1737 `vc2 = await _fetch_book_ctx(...)` 在 up() 主循环内联 await(超时 10s)——期间 ws 音频帧全部排队,本地 VAD 的时间判定用**处理时刻**(R:1599 `_tm2.time()`)而非帧时刻,卡顿会压扁/扭曲静音间隔判定,翻页瞬间提问会丢句首判定、应答显著变慢。**改法**:fetch 挪进 `asyncio.create_task`(grok 块 `_refresh_pt` 就是这个形态);或 VAD 计时改按累计字节推帧时刻(每 640B=20ms)。

**D. EPUB 视口文本被丢**。前端 `setPage` 带 `text`(F:2647,EPUB 动态视口文本),handle_openai 两个 page 处理器都不读 `j["text"]`,只按 `/pdf/api/page-text` 重拉——EPUB 的 file+section 组合拉到的可能是空/整章 → grok 在 EPUB 上 instructions 页正文陈旧或缺失。**改法**:`j.get("text")` 存在时直接作为 `book["page_text"]`,跳过 HTTP。

**E. 陈旧竞态整体可控**:sel/ink 有指纹去重(R:1756/1783),450 时前端 `__vcSyncNow` 立即重推(F:2497),up() 串行保证状态消息先于后续音频——修掉 C 之后这条链是健康的。

### ⑥ 播放稳定

**A. 桥模式长回答必坏音**。桥队列 `maxsize=600`=12s(R:1646);grok 音频是突发下发(远快于实时),回答 >12s 时 `QueueFull → get_nowait 丢最旧`(R:1835-1840)——**答案中段被成块丢弃**(用户听感=跳音/漏句)。ws 路径无此上限(前端全排队)。**改法**:队列改存"待播字节"无硬上限+总量护栏(如 120s),或满时丢**最新**并停止入队直到腾出(保序),最简单是 maxsize 提到 3000(60s)。

**B. 桥尾块永不冲刷**。`_bridge["pend"]` 不足 960B 的尾巴(<20ms)留到下一个 delta;response 结束时不冲 → 每轮尾部丢一小片,轮间还会把上轮尾巴拼进下轮开头(轻微杂音)。**改法**:`response.done` 时把 pend 补零到 960B 入队并清空。

**C. `_hpIn` 初始化漏洞(前端)**。F:2456-2464:桥偏好为 '0'(用户关桥)时 `if (bp === '0') return;` 在 `_headphonesIn().then(hp => _hpIn = hp)` **之前**返回 → 戴耳机+关桥的用户 `_hpIn` 恒 false(除非恰好触发 devicechange)→ F:597 半双工静麦误生效,**播放期完全无法打断**。**改法**:耳机检测挪到 `bp==='0'` 判断之前无条件执行。

**D. up_rate 到达前的 16k 帧**。前端 ws 一开就按默认 `_upRate=16000` 发帧(F:608),relay 的 `up_rate:24000` 要等 session.updated 握手后才发(R:1993)——窗口期(通常 <1s,超时可达 8s)的音频以 16k 编码被当 24k 喂给 grok(变调垃圾,还计费)。RMS 门挡住了纯静音,但开场即说话会污染首轮。**改法**:前端收到 up_rate/150 前不发音频;或 relay 在发出 up_rate 前丢弃二进制帧。

**E. playPcm 本体健康**:0.02s 起步 gap、playT 连续调度、359 清统计、`_reportPlayed` 用 `ac.currentTime-t0` 测真实已播(中途排空的空窗会高估,轻微)——无需动。

### 已确认非问题
- 反悔窗 cancel/timer 竞态:`_grok_end_timer` 的 CancelledError 捕获 + `_grok_commit` 首行置 endT=None,事件循环粒度内闭合(但 commit 中段 await 期间的 abuf 并发是真问题,见 gaps 第2条)。
- 预滚回放双分支(撤销提交/新轮)都补发 pre,句首不丢、不重复。
- wait_for_user 不递增 tools_n、不 create,路径正确。
- 熔断(≥3 强提示/≥6 硬断)逻辑自洽,成功即清零。

--- GAPS ---
• 【①/⑥·最影响打断手感】打断后无 delta 门控:R:1819-1847 对 output_audio.delta / transcript.delta 无条件转发,response.cancel(R:1572)生效前在飞的旧响应音频把前端刚清的队再填回、幽灵字幕污染新气泡。依据:OpenAI Realtime 语义 cancel 是异步的,delta 事件带 response_id 供客户端过滤。改法:response.created 记 resp_id;_grok_turn_start 发 cancel 时把当前 resp_id 加入 dropped 集合;两个 delta 分支开头 `if e.get('response_id') in dropped: continue`;response.done 清集合。

• 【③·数据完整性】abuf 无锁共享竞态:_grok_commit(R:1529-1536)payload 快照后 `await ows.send` 期间,up()/桥 _pump 任务可继续 extend abuf(甚至自行 flush 发送=同段音频重复上传),commit 恢复后 `abuf.clear()` 把并发追加的新语音清掉=句首丢字。依据:asyncio 单线程但 await 点可交错,bytearray 无版本保护。改法:commit 里改 `snap = bytes(abuf); del abuf[:len(snap)]`(clear 前移到 await 前),_feed_audio_raw 同样先 del 后 send;或统一经 asyncio.Lock。

• 【⑤·最重语义错误】per-response instructions 覆盖而非追加:R:1550 每个用户轮的 response.instructions='你是简短口语化的伴读助手…'+页状态,按 OpenAI Realtime 官方语义(response.create 的 instructions overrides session instructions for that response)整条替换 _oa_instructions——语言策略/see_ink 铁律/联网声明/工具诚实纪律在**所有用户轮**失效。改法:把 _oa_instructions(book,...) 的纪律段(或全文,xAI 无 token 计价=免费)拼进每轮 response.instructions 前缀,页状态附在末尾;或状态走 system item、放弃 response.instructions。

• 【③·会话级哑巴】tools_n 卡死:R:1486-1506 出飞递减在 `await ows.send(function_call_output)` 之后同一 try 内,send 抛异常→except pass→递减跳过→tools_n 永久>0→此后所有工具回填都走'暂不 create'分支(R:1495),整场工具再无主动续答(119b 症状复发的新入口)。改法:递减移到 send 之前,或包 finally。

• 【③·双响应】工具回填 create 不感知用户新轮:R:1498-1501 等 aEnd 的 sleep(≤20s)期间用户打断(aEnd 已被 turn_start 清零但 sleep 不撤),睡醒无条件 response.create,与用户 commit 的 create 竞争=双响应/被拒;工具执行中(busy=False)用户说话同理。改法:_tool 进入时快照 _turn['n'],create 前 `if _turn['n']!=snap or _vg['active']: 跳过 create`(输出已入历史,用户轮响应自然消费);R:1801 打字路径同样加 busy/aEnd 检查。

• 【⑤·设计漂移+双倍成本】grok 块漏 continue:R:1696-1713 注释称'状态只标脏,开口时随 instructions 注入',实际无 continue,page/state/ink 落进通用处理(R:1733-1800)每次都再注入 system item——与 per-turn instructions 重复、每条 $0.004 不入 _gk 账本、翻页双 fetch(_refresh_pt+R:1737)、book['_dirty'] 置而不读=死旗。改法:按意图加 continue 并把 sel/ink 指纹去重搬到 grok 块;或保留双注入则删 _refresh_pt/_dirty 并把注入 item 计入 _gk['items']。

• 【②/⑤·延迟+VAD 失真】通用 page 处理器 R:1737 在 up() 音频主循环内联 await HTTP(10s 超时):翻页瞬间音频帧全排队,VAD 用处理时刻(R:1599)判间隔被卡顿扭曲,翻页后立刻提问=句首判定丢失/应答明显变慢。改法:fetch 包 asyncio.create_task(带 page 一致性守卫,同 _refresh_pt 形态);根治可把 VAD 计时改按累计字节推帧时刻(640B=20ms)。

• 【①·上下文对齐】truncate 链路对 grok 是死代码:pend_trunc 只在 input_audio_buffer.speech_started(R:1848-1868)设置,grok turn_detection=None 永远收不到;前端 450 回报的 played_ms(F:2493)到 R:1719 因 pid=None 被丢——打断后从不 truncate,模型上下文保留用户没听到的整段回答。改法:_grok_turn_start 里镜像 speech_started 的 pend_trunc 设置+600ms 兜底 task;xAI 若不支持 truncate 按 error message 关键字静默(现有 'cancel' 过滤同款)。

• 【①·误触发】单帧 RMS>350 即打断(R:1600-1610):20ms 一声咳嗽/敲击=cancel+清播放不可逆;固定阈值对轻声/远麦(低于350=全聋)和高噪声地板(恒过阈=hangover 永不触发同样全聋)双向脆;_rms 不去 DC、异常返回 9999=voiced。改法:turn_start 需连续 2-3 帧过阈(40-60ms);滚动噪声地板(近3s P20)+相对阈值 max(350, floor*2.5);RMS 前减均值;异常改返回地板值。

• 【②·可压 ~0.35-0.6s】hangover 与反悔窗保护重叠:0.8s hangover 防句中停顿,0.45s 反悔窗也能撤销提交续轮(R:1605-1608)——hangover 降 0.5s 总保护窗仍 0.95s;进一步用已在手的 _tr_pend 流式转写做伪语义 VAD:末尾有终结标点(。?!吗呢吧)→0.3+0.2s,半句无标点→维持 0.8+0.45。前提:先在 grok-diag 记 .updated 首达时刻,确认手动轮次下转写在 commit 前就流(若不流,②B与④字幕实时性都要重估)。

• 【④·日志污染】completed 同轮多发只在 UI 层去重(F:444 同 iid 覆盖气泡),R:1887-1895 每个 completed 都 _vlog('q')→voice-log 同句重复,recall_study 的 digest 把同一问题复述 N 遍。改法:completed 分支记最近已定稿 iid,重复 iid 只发 451 不落盘。

• 【⑥·桥模式长答必坏音】桥队列 12s 上限(R:1646 maxsize=600)+满时丢最旧(R:1835-1840),grok 突发下发>实时消费,回答超 ~12s 即中段成块丢失(跳音);另 _bridge['pend'] <960B 尾块永不冲刷,轮尾丢片+下轮开头拼上轮残渣。改法:maxsize 提到 3000(60s)或改丢最新保序;response.done 时把 pend 补零冲入队。

• 【⑥·前端打断失效】_hpIn 初始化漏:F:2456-2464 桥偏好='0' 时 return 在耳机检测之前,戴耳机+关桥用户 _hpIn 恒 false→F:597 半双工静麦误生效=播放期完全无法打断(devicechange 才偶然修正)。改法:_headphonesIn().then(hp=>_hpIn=hp) 无条件执行,再判 bp。

• 【⑤·EPUB 语境缺失】前端 setPage 推的视口文本(F:2647 j.text)在 handle_openai 两个 page 处理器均被忽略,只按 /pdf/api/page-text 以 EPUB rel+section 重拉(可能空/整章)→grok 在 EPUB 上 instructions 页正文陈旧或缺失。改法:j.get('text') 存在时直接写 book['page_text'] 并跳过 HTTP fetch。

• 【⑥·开场污染】up_rate(24000)在 session.updated 握手后才发(R:1993),前端 ws 一开就按默认 16k 发帧(F:608)——窗口期开口说话=16k 音频被当 24k 喂给 grok(变调垃圾+计费)。改法:前端收到 up_rate/150 事件前不发二进制;或 relay 在发 up_rate 前丢弃收到的二进制帧。

• 【①·cancel 竞态兜底】pend_resp 窗口 cancel 后服务器仍可能建出 response(created 迟到),busy=True 旧响应压过用户说话,现无 re-cancel。改法:response.created 处理(R:1938)加 `if engine=='grok' and _vg['active']: 再发一次 response.cancel`。

• 【账本口径】_gk 少记:state/ink/page 注入的 system item(通用处理路径)与 commit 补的 0.5s 静音字节(R:1529 直接 extend abuf 绕过 _feed_audio_raw 的 in_b 计数)都不入账,est_by_audio 系统性偏低。改法:注入处 _gk['items']+=1;commit 补静音处 _gk['in_b']+=24000。

