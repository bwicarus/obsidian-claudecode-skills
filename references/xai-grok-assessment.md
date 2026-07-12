# xAI Grok 综合考核(2026-07-13,四路并行调研原始档案)

> 结论速览:①Realtime 语音=技术上能接(OpenAI 协议兼容)但**无原生 WebRTC**,与我们 #280 回声治本架构冲突,$0.05/min 平价制;②Web Search=旧 Live Search 已死(410),新 Agent Tools $5/1k 比 OpenAI 贵且 agentic 计费不可控,唯一独家=x_search;③grok-4.5 智力 AA54 第一梯队第4,每美元智力断层第一,但幻觉率 54% 四家最差+TTFT 12.6s,不适合讲解主力,适合夜间批处理;④账号 0 credits,充$5 后可 opt-in 数据共享换 $150/月(永久不可退,隐私权衡)。key 在 ~/.config/xai-grok.json(600,不进 git)。
> 以下为四个调研 agent 的完整原始报告(全部数字含来源 URL)。

## 实机实测(2026-07-13,充值后,Pi 直连)

| 项 | 实测结果 |
|---|---|
| **realtime 语音** | `wss://api.x.ai/v1/realtime?model=grok-voice-latest`:**连接 0.48s、首音频 1.03s**(官方宣称<1s 基本兑现);事件流=OpenAI GA 形制(`response.output_audio.delta` 等)完全兼容,session.update/item.create/response.create 原样可用;一次响应内中文→日文双语切换正常,eve 音色自然;24kHz PCM。样本 `grok-voice-test.wav` 已给用户 |
| **web_search** | Responses API + `tools:[{type:"web_search"}]` 通,7.3s;**agentic 计费陷阱实锤**:一条天气问答模型自主搜了 3 次(`web_search_calls:3`),`cost_in_usd_ticks:275583500`≈**$0.028/问**(3×$0.005 搜索+11k tokens)vs Gemini grounding 同题免费。行内 [[n]](url) 引用,顶层 citations 空 |
| **grok-4.3 (effort=none)** | 日语语法讲解题:1.4s、回答正确、$0.00025/条;⚠ 未指定简繁时**输出繁体中文**倾向 |

实测结论:①realtime 技术上随时可接(协议兼容+中日文过关+延迟优秀),但 WS-only——外放场景回声问题(#280 迁 WebRTC 的原因)会回来,只适合耳机档/实验档;②web_search 维持不接的判断(单问成本≈Gemini 的 ∞ 倍、OpenAI 的 7 倍);③grok-4.3 effort=none 快且便宜,夜间批处理候选,需 prompt 钉简体。
消费进度:实测共花约 $0.03,距 data-sharing 门槛($5 累计消费)还远。





================================================================================
AGENT RESULT #1
================================================================================
# xAI Grok 语音能力调研(2026-07 现状)

## 结论先行

**xAI 有公开的语音 API,而且是全家桶**——不是"只在 Grok App 内"。截至 2026-07,对外开放的有 4 样:

1. **Voice Agent API**(realtime speech-to-speech,WebSocket,**兼容 OpenAI Realtime API 协议**)— 2025-12-17 正式发布
2. **Grok TTS API**(独立文本转语音)— 2026-04 GA
3. **Grok STT API**(独立语音转文字,批量+流式)— 2026-04 GA
4. **Voice Agent Builder**(no-code 建语音 agent)— 2026-07-01 beta

官方文档:https://docs.x.ai/developers/model-capabilities/audio/voice (总览页,列出全部三个 API 端点)

---

## 1. Voice Agent API(realtime S2S)详情

### 端点与协议
- **WebSocket**:`wss://api.x.ai/v1/realtime?model=grok-voice-latest`(来源:https://docs.x.ai/developers/model-capabilities/audio/voice-agent)
- **兼容 OpenAI Realtime API**:官方明说大多数 OpenAI 客户端库/SDK 改 base URL 即可用。已知事件名差异:`conversation.item.input_audio_transcription.delta` 在 xAI 叫 `...transcription.updated`;`conversation.item.retrieve` 等少数事件不支持(来源:同上 docs 页)
- **⚠ 无原生 WebRTC 端点**:与 OpenAI 不同,xAI **没有** SDP offer/answer 式的原生 WebRTC 接入。docs 里的 "WebRTC Agent" 只是 xai-cookbook 里的示例 app(https://github.com/xai-org/xai-cookbook/tree/main/voice-examples/agent/webrtc);浏览器 WebRTC 场景官方路线是配 **LiveKit**(官方 LiveKit 插件:https://docs.livekit.io/agents/models/realtime/plugins/xai/)
- **SIP 电话**:支持 PSTN/呼叫中心/PBX 呼入(docs 有专门 SIP 页;cookbook 有 Twilio 示例)
- **浏览器安全接入**:后端换发短时 ephemeral token(前缀 `xai-client-secret.`),客户端拿 token 开 socket,长期 API key 不下发(来源:docs voice-agent 页;第三方提到换发端点为 `POST https://api.x.ai/v1/realtime/client_secrets`,https://www.evalgent.com/blog/xai-grok-voice-agent)

### 模型
- 现役旗舰:`grok-voice-think-fast-1.0`;旧版 `grok-voice-fast-1.0` 已 deprecated;别名 `grok-voice-latest` 指向最新(来源:docs voice-agent 页)

### 价格(官方 docs.x.ai/developers/models 模型页)
| 项 | 价格 |
|---|---|
| Realtime 语音 | **$0.05/分钟($3.00/小时)**,按音频时长计,音色包含在内 |
| Realtime 文本输入 | $0.004/条 message |
| 电话号码(provisioned) | +$0.01/分钟(第三方:https://www.eesel.ai/blog/grok-voice-agent-builder-pricing) |
| 工具调用 | **另计**:web_search / x_search / file_search / MCP 工具每次单独计费 |

对比:xAI 官方新闻稿把 OpenAI Realtime 混合成本估为 ~$0.10/min,自称一半价(来源:https://x.ai/news/grok-voice-agent-api)。**注意 $0.05/min 是按连接时长纯平价,无免费额度、无公开量价折扣**(第三方 eesel 指出)。

### 音色 / 语言
- 内置音色:**eve(默认)、ara、rex、sal、leo** 5 个;另有 **Custom Voices API**(`https://api.x.ai/v1/custom-voices`)支持 ≤120 秒音频克隆自定义音色;2026-07 又加了 **21 个新旗舰音色**(第三方汇总:https://releasebot.io/updates/xai)
- 语言:**20+ 种含中文(简体)和日语**——英、阿(埃及/沙特/阿联酋)、孟加拉、**中文(简体)**、法、德、印地、印尼、意、**日**、韩、葡(巴西/葡萄牙)、俄、西(墨西哥/西班牙)、土、越(来源:docs voice-agent 页)。自动跟随用户语言,支持对话中途切换语言(来源:x.ai 新闻稿)

### Function calling / 工具
- **完整支持**:JSON schema 自定义 function(客户端执行)+ **并行工具调用**(一次响应可连发多个 function)
- 服务端托管工具:`web_search`、`x_search`(X 平台实时搜索)、`file_search`(Collections 文档检索)、**MCP**(来源:docs voice-agent 页)

### 会话特性
- 会话上限 30 分钟、每团队 100 并发(第三方:https://blog.laozhang.ai/en/posts/grok-voice-agent-api);断线恢复 `resumption.enabled` 缓存 30 分钟;`server_vad` 自动转轮;`reasoning.effort`(默认 high,可设 none);xAI 独有扩展:`force_message`(硬编码 TTS 输出不过模型)、`replace`(发音映射修正)
- 音频格式:PCM(8k–48kHz,默认 24kHz)/ G.711 μ-law / A-law
- 延迟宣称:平均 time-to-first-audio <1 秒,"比最近竞品快近 5 倍";Big Bench Audio audio-reasoning 榜第一(95%),Artificial Analysis 独立验证(来源:https://x.ai/news/grok-voice-agent-api)

## 2. TTS API
- 端点:`https://api.x.ai/v1/tts`(一次性)+ `wss://api.x.ai/v1/tts`(流式,文本 delta 进、base64 音频 chunk 出)(来源:https://docs.x.ai/developers/model-capabilities/audio/text-to-speech)
- **价格:官方模型页现价 $15.00/100 万字符**(https://docs.x.ai/developers/models;OpenRouter 同价佐证:https://openrouter.ai/x-ai/grok-voice-tts-1.0/pricing)。⚠ 2026-04 发布时媒体报道为 $4.20/M 字符(https://www.marktechpost.com/2026/04/18/xai-launches-standalone-grok-speech-to-text-and-text-to-speech-apis-targeting-enterprise-voice-developers/),现官方页是 $15——以官方现价为准,疑似发布价后调涨
- 语言:20 种,**含中文(简体)、日语**(BCP-47 指定;官方注明列表外语言也能生成但精度不保证)
- 特色:语气标签 `[pause]/[laugh]/[cry]/[breath]` 等 + 包裹标签 `<whisper>/<sing>/<slow>` 等;单次 ≤15,000 字符;MP3/WAV/PCM/μ-law/A-law

## 3. STT API
- 端点:`https://api.x.ai/v1/stt`(REST 批量,文件 ≤500MB)+ `wss://api.x.ai/v1/stt`(流式,二进制帧)(来源:https://docs.x.ai/developers/model-capabilities/audio/speech-to-text)
- **价格:$0.10/小时(批量 REST)、$0.20/小时(流式)**(官方模型页 + marktechpost 一致)——折合流式约 **$0.0033/分钟**,极便宜
- 语言:24-25 种。**格式化(language 参数)列表里有日语(`ja`)但没有中文**——中文转写支持情况官方文档未明示,需实测
- 特色:词级时间戳、说话人分离、多声道(≤8)、Smart Turn 端点检测(0–1 阈值)、关键词偏置(≤100 词,对你们的"热词注入"场景直接对口)

## 4. Voice Agent Builder(no-code)
2026-07-01 beta,网页拖拽建语音 agent,底层就是 Voice Agent API,同价 $0.05/min(来源:https://cryptobriefing.com/xai-voice-agent-builder-beta-pricing/、https://www.eesel.ai/blog/grok-voice-agent-builder)

---

## 对你们 App 的适配性评估

- **协议兼容**:你们现有 GPT Realtime mini 代码若走 WebSocket,理论上改 base URL 到 `wss://api.x.ai/v1/realtime` + 换 key 即可试跑(官方声称 OpenAI SDK 兼容)。
- **⚠ 最大坑**:你们语音㉔/#280 刚把 GPT Realtime **迁到 WebRTC 直连**(治外放回声)——**xAI 没有原生 WebRTC 端点**,浏览器直连要么退回 WS(回声问题重来),要么中间加 LiveKit 层(架构变重)。这是接入的实质障碍。
- **成本**:$0.05/min 平价制 vs OpenAI realtime mini 按 token。长通话(伴读场景挂 30 分钟)xAI = $1.5/次封顶可预算;但**工具调用另计费**且未见单价明细,web_search/x_search 每次多少钱官方 docs 未公开具体数字(本次未查到,不编数)。
- **中日文**:S2S 和 TTS 官方明确支持中文(简体)+日语;STT 的中文支持存疑(格式化列表缺 zh),伴读若靠它做中文 ASR 需先实测。
- **会话 30 分钟上限**:长伴读需做续接(有 resumption 机制,缓存 30 分钟)。

## 查不到 / 存疑的点
- web_search / x_search / MCP 工具**每次调用的具体单价**:官方 docs 提"另计费"但本次未检索到公开数字
- STT 对**中文**的转写支持:官方语言列表未列 zh
- TTS $4.20→$15 的价格变动官方未见说明,以 docs.x.ai/developers/models 现价 $15/M chars 为准
- 原生 WebRTC 端点的 roadmap:官方无公开说法

--- KEY FACTS ---
• xAI 有公开语音 API 全家桶,不是 App 专属:Voice Agent API(realtime S2S,2025-12-17 发布)+ TTS/STT 独立 API(2026-04 GA)+ Voice Agent Builder beta(2026-07-01)。来源:https://docs.x.ai/developers/model-capabilities/audio/voice 、https://x.ai/news/grok-voice-agent-api
• Voice Agent API 端点 wss://api.x.ai/v1/realtime?model=grok-voice-latest,兼容 OpenAI Realtime API(OpenAI SDK 改 base URL 即可),浏览器用 ephemeral token(xai-client-secret. 前缀)。来源:https://docs.x.ai/developers/model-capabilities/audio/voice-agent
• ⚠ 无原生 WebRTC 端点(与 OpenAI 不同):WebRTC 只有 cookbook 示例或配 LiveKit 官方插件;另支持 SIP 电话接入。对我们已迁 WebRTC 直连的架构是实质障碍。来源:https://docs.x.ai/developers/model-capabilities/audio/voice-agent 、https://docs.livekit.io/agents/models/realtime/plugins/xai/
• Realtime 定价 $0.05/分钟($3.00/小时)平价制、文本输入 $0.004/条;电话号码 +$0.01/min;工具调用(web_search/x_search/MCP)另计费但单价未公开。来源:https://docs.x.ai/developers/models
• TTS 官方现价 $15.00/100万字符(2026-04 发布时媒体报 $4.20/M,疑似已涨价,以官方页为准);端点 /v1/tts + wss 流式,支持语气标签。来源:https://docs.x.ai/developers/models 、https://openrouter.ai/x-ai/grok-voice-tts-1.0/pricing
• STT 极便宜:$0.10/小时(批量)、$0.20/小时(流式)≈$0.0033/min;词级时间戳+说话人分离+关键词偏置(≤100热词)。来源:https://docs.x.ai/developers/models 、https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
• 中日文支持:Voice Agent 和 TTS 官方明确含中文(简体)+日语(20+语言,支持对话中途切换);但 STT 的 language 格式化列表有日语无中文,中文 ASR 需实测。来源:https://docs.x.ai/developers/model-capabilities/audio/voice-agent 、.../speech-to-text
• Function calling 完整支持:JSON schema 自定义工具+并行调用+服务端托管工具(web_search/x_search/file_search/MCP);现役模型 grok-voice-think-fast-1.0,可调 reasoning.effort。来源:https://docs.x.ai/developers/model-capabilities/audio/voice-agent
• 音色:5 内置(eve/ara/rex/sal/leo)+ 2026-07 新增 21 旗舰音色 + Custom Voices API 克隆(≤120s 样本);会话上限 30 分钟、100 并发/团队,resumption 断线恢复缓存 30 分钟。来源:https://docs.x.ai/developers/model-capabilities/audio/voice-agent 、https://releasebot.io/updates/xai
• 延迟/榜单宣称:time-to-first-audio <1s、比最近竞品快近5倍、Big Bench Audio 第一(95%,Artificial Analysis 独立验证)。来源:https://x.ai/news/grok-voice-agent-api


================================================================================
AGENT RESULT #2
================================================================================
# xAI (现 SpaceXAI) Grok API 模型阵容与价格 — 2026-07-13 现状

> 注:xAI 2026 年已上市并收购 Cursor,官方文档站标题现为 "SpaceXAI Docs"(docs.x.ai 域名不变)。以下全部数字取自 docs.x.ai 官方模型页,并与 artificialanalysis.ai / OpenRouter / 第三方价格库交叉核对一致。

## 1. 当前在售文本模型(全部支持 cached input 折扣)

| 模型 | 定位 | Context | Input $/1M | **Cached** $/1M | Output $/1M | Rate limit (Tier 0 默认) |
|---|---|---|---|---|---|---|
| **grok-4.5**(2026-07-08 发布) | 旗舰,Musk 称 "Opus-class 但更快"(~80-119 tps) | 500k | $2.00 | $0.50(75% off) | $6.00 | 150 RPS / 50M TPM |
| **grok-4.3**(2026-04-30 发布) | 性价比主力,别名 `grok-latest`;首个原生视频输入;reasoning effort none/low/medium/high | 1M | $1.25 | $0.20(84% off) | $2.50 | 37 RPS / 10M TPM |
| grok-4.20-0309-reasoning | 4.20 系(0309=3月9日快照)推理版 | 1M | $1.25 | $0.20 | $2.50 | 37 RPS / 10M TPM |
| grok-4.20-0309-non-reasoning | 同上非推理版 | 1M | $1.25 | $0.20 | $2.50 | 37 RPS / 10M TPM |
| grok-4.20-multi-agent-0309 | 多 agent 并行深度研究(beta) | 1M | $1.25 | $0.20 | $2.50 | 仅 9 RPS / 2.5M TPM |
| **grok-build-0.1** | 编码/agentic 专用(grok-code-fast-1 后继) | 256k | $1.00 | $0.20 | $2.00 | 37 RPS / 10M TPM |

来源:官方模型页 https://docs.x.ai/developers/models 、https://docs.x.ai/developers/models/grok-4.5 、https://docs.x.ai/developers/models/grok-4.3 、https://docs.x.ai/developers/models/grok-build-0.1 、https://docs.x.ai/developers/models/grok-4.20-multi-agent-0309 ;交叉核对 https://artificialanalysis.ai/models/grok-4-5 (输入$2/输出$6、500k、119 tps、智能指数54=第8/188)、https://openrouter.ai/x-ai/grok-4.3 。

**额外计费点**:
- **Batch API 全线 8 折**(官方模型页注明 "Batch API requests are billed at a 20% discount")。
- 服务端工具按次计费:Web Search / X Search / Code Execution 各 **$5/1K 次**、File Attachments $10/1K、Collections Search $2.50/1K(来源: https://x.ai/news/grok-4-1-fast 首发的 Agent Tools 价目,现行价见 https://costgoat.com/pricing/grok-api 等价格库)。对比:你现在用的 OpenAI web_search 是 $4/1K($0.004/次),xAI $5/1K 略贵,且没有 Gemini 那样的免费 grounding 配额。
- 语音相关(与你的语音通话栈可比):Realtime 语音 **$0.05/min**、TTS $15/1M 字符、STT $0.10/hr(REST)/$0.20/hr(流式)(来源: https://docs.x.ai/developers/models )。

## 2. 你问到的具体型号现状

- **grok-5:不存在**。截至 2026-07-13 没有任何官方或第三方来源显示 grok-5;最新旗舰是 7 月 8 日发布的 grok-4.5( https://x.ai/news/grok-4-5 、https://techcrunch.com/2026/07/08/spacexai-releases-grok-4-5-which-elon-describes-as-an-opus-class-model/ )。注意 grok-4.5 发布时**未在 EU 上线**。
- **grok-4 / grok-4-fast / grok-4.1-fast / grok-code-fast-1 / grok-3:已于 2026-05-15 12:00 PT 全部退役**(共 8 个:grok-4-1-fast-reasoning/non-reasoning、grok-4-fast-reasoning/non-reasoning、grok-4-0709、grok-code-fast-1、grok-3、grok-imagine-image-pro)。旧 slug **不报错、自动重定向**:多数 → grok-4.3(fast 非推理版映射到 reasoning effort=none),grok-code-fast-1 → grok-build-0.1;**按新模型价格计费**(即原 grok-4-fast $0.20/$0.50 的超低价档已消失,重定向后按 $1.25/$2.50 收)。来源:官方迁移页 https://docs.x.ai/developers/migration/may-15-retirement 。
- grok-4.1(非 fast 旗舰)也不在当前任何在售列表中(已被 4.20/4.3 取代),具体下线时间未查到官方记录。
- **超低价 "fast" 档现已无直接替代**:目前最便宜的文本模型是 grok-build-0.1($1/$2)和 grok-4.3(cached input $0.20)。原 grok-4.1-fast $0.20/$0.50 档在第三方文章中仍被引用,但那是退役前的旧价。

## 3. Rate limits 机制

按 2026-01-01 起的累计 API 消费分 6 档:Tier 0(默认 $0)/ Tier 1($50+)/ Tier 2($250+)/ Tier 3($1,000+)/ Tier 4($5,000+)/ Enterprise(申请)。**升档永久、永不降级**。Tier 0→Tier 4 示例:标准模型 37→208 RPS、10M→85M TPM;grok-4.5 150→500 RPS、50M→100M TPM。超限返回 429。来源: https://docs.x.ai/developers/rate-limits 。

## 4. 免费 credits / 数据共享计划(2026 现状)

**① 新账号促销 credits:$25**。多个第三方来源一致( https://www.getaiperks.com/en/blogs/22-xai-grok-free-credits 2026-02-09 更新、https://www.aifreeapi.com/en/posts/xai-grok-api-pricing );官方 quickstart 页未明写,以注册后 console 显示为准。

**② "数据共享换每月 $150 credits" 计划:2026 年仍存在**(官方文档 Free Credits 页仍在,镜像: https://grok-api.apidog.io/free-credits-934025m0 ;追踪站 https://cloudcredits.io/providers/xai/programs/data-sharing-program 列为在效;2026-02 文章确认新用户首月可拿 $25+$150=$175)。条款:
- 金额:**$150/月**(不是 $175;"$175"是第三方把首月 $25 签约金+$150 加总的说法)
- 门槛:团队需**已消费 ≥$5**才能 opt-in
- 开通:console.x.ai → Billing 页 → Credits Section,**仅 team admin** 可开
- ⚠️ **opt-in 后永久不可退出**(团队级;可以开多个 team,一个共享一个不共享)
- ⚠️ 地区限制:**EU 全部成员国 + 英国 + 冰岛/挪威/列支敦士登等 30+ 国不可用**(日本未在排除名单内)
- 反面信息:有 SEO 站( https://yangmao.ai/en/questions/grok-api-free-credits/ )称计划"应视为已结束",但其声称的结束时间(2025-05)与 2026-02 的在效报道矛盾、无实证,可信度低。**结论:计划大概率仍在,最终以你 console 的 Billing→Credits 区是否显示为准**。
- 未能找到 docs.x.ai 上该页的现行直达 URL(试了 /developers/free-credits、/console/free-credits 均 404,官方文档几次改版),这是本次调研唯一没钉死的点。

**③ 新渠道:X API 消费返 xAI credits**(2026 年新推,官方: https://docs.x.com/x-api/getting-started/pricing + @xai 官推)。把 xAI team 链接到 X developer account(console.x.com 账户设置),按每个 billing cycle 的 X API 累计消费返点:$200-499 返 10%、$500-999 返 15%、$1,000+ 返 20%。只对同时买 X API 的团队有意义。

## 5. 对你 App 的适配性速评

- 若想要"便宜大杯":grok-4.3($1.25/$2.50、1M context、cached $0.20、支持图像+视频输入、reasoning 可关到 none 当非推理快模型用)是目前 xAI 唯一高性价比选项,价位介于 Gemini Flash 和 Claude Sonnet 之间。
- $150/月数据共享 credits 若在你的 console 可见,足够覆盖轻中度用量(≈60M input tokens/月 @grok-4.3),但**永久共享 API 请求数据、不可反悔**,把阅读内容/学习数据发给 xAI 训练需自己权衡;先充 $5 烧完才够格 opt-in。
- 搜索工具 $5/1K 次比你现有 OpenAI $4/1K 贵,且无免费额度,无迁移动机。

--- KEY FACTS ---
• 2026-07 xAI 在售文本模型仅 6 个:grok-4.5 / grok-4.3 / grok-4.20-0309(reasoning|non-reasoning|multi-agent) / grok-build-0.1;不存在 grok-5 — https://docs.x.ai/developers/models
• grok-4.5(2026-07-08 发布,'Opus-class'):500k context,$2.00 input / $0.50 cached(75% off)/ $6.00 output,Tier0 限速 150 RPS / 50M TPM,发布时不在 EU 提供 — https://docs.x.ai/developers/models/grok-4.5 + https://x.ai/news/grok-4-5
• grok-4.3(2026-04-30 发布,别名 grok-latest):1M context,$1.25 / cached $0.20(84% off)/ $2.50,37 RPS / 10M TPM,reasoning effort 可调 none~high — https://docs.x.ai/developers/models/grok-4.3
• grok-build-0.1(编码,接替 grok-code-fast-1):256k context,$1.00 / cached $0.20 / $2.00 — https://docs.x.ai/developers/models/grok-build-0.1
• grok-4 / grok-4-fast / grok-4.1-fast / grok-code-fast-1 / grok-3 等 8 模型已于 2026-05-15 退役,旧 slug 自动重定向到 grok-4.3(code→grok-build-0.1)并按新价 $1.25/$2.50 计费,原 fast 超低价档($0.20/$0.50)消失 — https://docs.x.ai/developers/migration/may-15-retirement
• 全线支持 cached input 折扣 + Batch API 统一 8 折;rate limits 按 2026-01-01 起累计消费分 Tier 0($0)~Tier 4($5,000+)+Enterprise,升档永不降级 — https://docs.x.ai/developers/rate-limits
• 数据共享计划 2026 年仍在:$150/月 credits,门槛=团队已消费≥$5,console.x.ai Billing→Credits Section 由 team admin 开通,opt-in 后永久不可退出,EU/UK 等 30+ 国不可用(日本可)— https://grok-api.apidog.io/free-credits-934025m0 (官方文档镜像) + https://cloudcredits.io/providers/xai/programs/data-sharing-program
• 新账号另有 $25 促销 credits(首月合计可达 $175;'$175/月'是第三方误传,月常态是 $150)— https://www.getaiperks.com/en/blogs/22-xai-grok-free-credits
• 2026 新渠道:xAI team 绑 X developer account 后,X API 消费按档返 xAI credits(每 billing cycle $200+返10%、$500+返15%、$1000+返20%)— https://docs.x.com/x-api/getting-started/pricing
• 服务端工具按次计费:Web Search / X Search / Code Execution 各 $5/1K 次(比你现用 OpenAI web_search $4/1K 贵且无免费额度);Realtime 语音 $0.05/min、TTS $15/1M 字符 — https://docs.x.ai/developers/models + https://x.ai/news/grok-4-1-fast


================================================================================
AGENT RESULT #3
================================================================================
# xAI 搜索能力现状(2026-07)

## 一、最重要的事实:Live Search API 已死

你记忆中的 **Live Search API(`search_parameters`,$25/1k sources)已于 2026-01-12 正式移除**,请求返回 **410 Gone**。多个独立来源确认:
- LangChain issue: "XAI Live Search API will be deprecated, use tools instead" (https://github.com/langchain-ai/langchain/issues/33961)
- openclaw issue: "xAI deprecated Live Search API (410 Gone)" (https://github.com/openclaw/openclaw/issues/26355)
- Make 社区: "[410] Live Search Deprecated (Switch to Agent Tools API)" (https://community.make.com/t/x-ai-node-integration-error-410-live-search-deprecated-switch-to-agent-tools-api/102097)
- 第三方迁移指南(含官方公告转述"retired on January 12, 2026...410 Gone"): https://help.apiyi.com/en/xai-grok-api-x-search-web-search-guide-en.html

随之消失的还有:**$25/1k sources 的按源计费模式**、以及旧版的 **news / RSS 两种专用数据源**(新体系里没有对应物)。

## 二、现行形态:Agent Tools API(server-side tools)

官方时间线(https://docs.x.ai/developers/release-notes):
- 2025-05 Live Search 上线 → 2025-10 server-side tools(`web_search`/`x_search`/`code_execution`)上线 → 2025-11 工具降价"最多降 50%,不超过 $5/1000 次成功调用" → 2026-01-12 旧 Live Search 移除。

**请求方式**(https://docs.x.ai/docs/guides/tools/overview):在 **`/v1/responses`(Responses API)** 请求里加 `tools` 数组:
```json
{
  "model": "grok-4.5",
  "input": [{"role": "user", "content": "..."}],
  "tools": [{"type": "web_search"}, {"type": "x_search"}]
}
```
⚠️ **Chat Completions(`/v1/chat/completions`)已被官方标为 legacy**,新功能只进 Responses API(https://docs.x.ai/docs/guides/chat-completions 标题即 "Chat Completions (Deprecated)")。想用搜索工具必须走 Responses API。执行模式是 **agentic**:模型自主决定搜几轮、搜什么,服务端自动执行(与旧版"每请求注入 N 个 sources"的一次性模式本质不同)。

## 三、价格(官方 docs.x.ai/developers/pricing)

| 工具 | 价格 |
|---|---|
| Web Search | **$5 / 1000 次调用**(= $0.005/次) |
| X Search | **$5 / 1000 次调用** |
| Code Execution | $5 / 1000 次 |
| File Attachments | $10 / 1000 次 |
| Collections Search | $2.50 / 1000 次 |

计费 = **工具调用次数 + token 用量**两部分("priced based on two components: token usage and server-side tool invocations")。不再按 source 计费。release notes 的措辞是 "per 1000 **successful** calls"。

⚠️ 关键成本陷阱:因为是 agentic 搜索,**一次用户请求可能触发多次工具调用**(模型自主追加搜索),每次都计 $0.005;且搜索结果内容会进上下文按正常 token 价计费。单请求实际成本 ≈ N×$0.005 + 模型 token(官方未提供每请求调用次数上限的默认值,可用 `max_turns` 限制,用 `response.server_side_tool_usage` 事后核账)。

**配套模型价格**(https://docs.x.ai/developers/models,2026-07 在售):grok-4.3 = $1.25/$2.50 每 M token(1M 上下文);grok-4.5 = $2/$6(500k 上下文,官方推荐)。第三方页(https://pricepertoken.com/pricing-page/provider/xai)另列有 grok-4.1-fast $0.20/M input 的低价档,官方模型页未展示,以 console 实际可用为准。

## 四、数据源与参数

**web_search**(https://docs.x.ai/docs/guides/live-search,该 URL 现在就是 Web Search 工具文档):
- `allowed_domains` / `excluded_domains`(各最多 5 个域名)
- `enable_image_understanding`、`enable_image_search`(2026-05 新增图片搜索)

**x_search**(https://docs.x.ai/developers/tools/x-search):
- 能搜 X 帖子/用户/线程,支持**关键词搜索+语义搜索+用户搜索+线程获取**
- `allowed_x_handles` / `excluded_x_handles`(各最多 20 个账号,互斥)
- `from_date`/`to_date`(ISO8601)
- `enable_image_understanding`、`enable_video_understanding`(**视频理解为 X Search 独有**,web_search 不支持;图/视频理解按 token 计费而非按次)

**news / RSS 专用源已消失**——新体系只有 web + X 两类,新闻内容靠 web_search 覆盖。

## 五、返回格式(citations)

- 默认:完整引用在 **`response.citations`**(来源 URL 列表)
- 可选:**`response.inline_citations`** 行内引用(markdown 链接嵌入正文)
- 计量:`response.server_side_tool_usage` + `response.tool_calls` 可看每次请求实际触发了哪些工具、多少次(https://docs.x.ai/docs/guides/tools/advanced-usage)
- 流式:SSE 下 server-side tools 自动执行,`include:["verbose_streaming"]` 可实时看到工具调用过程

## 六、免费额度

- 注册送 **$25** 一次性推广额度
- **Data Sharing Program:每月 $150 免费额度**,条件:团队已消费 ≥$5、opt-in 后**不可退出**(共享 API 请求元数据+输出给 xAI 训练)、限支持国家。来源:https://cloudcredits.io/providers/xai/programs/data-sharing-program 、https://www.getaiperks.com/en/blogs/22-xai-grok-free-credits (console Billing 页可操作)

## 七、对你们 App 的评估(vs 现有 Gemini grounding / OpenAI web_search)

| | xAI web_search/x_search | OpenAI web_search | Gemini google_search grounding |
|---|---|---|---|
| 单次价 | $0.005/工具调用(agentic 可能一请求多次) | $0.004/次(你们现价) | 每月 5000 次免费额度内 $0 |
| 计费可预测性 | ❌ 较差(模型自主决定调用次数) | ✅ 好 | ✅ 好 |
| 端点 | 仅 Responses API(需接第 4 家 provider) | 已接 | 已接 |
| 独特数据 | ✅ **X 实时帖子/线程/账号过滤/语义搜索/视频理解**,全行业独家 | 无 | Google 索引质量高 |

**结论:**
1. **纯 web 搜索维度,xAI 无性价比优势**:$0.005/调用 高于 OpenAI $0.004/次,且 agentic 多次调用+token 计费让单请求成本更难控;Gemini 5000 次/月免费额度对你们的量级仍是最优解。
2. **唯一不可替代的价值是 x_search**:实时 X 数据(语义搜索、按账号过滤、日期范围、帖子视频理解)是任何其他 frontier 厂商都没有的。但对自学阅读 App(教材/语言学习场景),X 实时社交数据的场景价值有限——除非要做"时事英语阅读材料抓取"之类的功能。
3. **如果想零成本试水**:data-sharing 每月 $150 额度足够覆盖每月 3 万次搜索调用或大量 token,相当于免费——代价是 API 数据共享给 xAI 训练且不可退出,注意你们请求里会带用户阅读内容(隐私权衡)。
4. **接入成本**:必须走 Responses API(Chat Completions 是 legacy 不支持 tools),等于为第 4 家 provider 单独写 adapter;citations 格式(`citations`/`inline_citations`)与 Gemini groundingMetadata、OpenAI annotations 均不同,需归一化。

**建议**:维持 Gemini grounding(免费)+ OpenAI web_search(溢出)现状;仅当出现「需要 X 实时内容」的明确功能需求,或想白嫖 data-sharing $150/月 且接受隐私条款时再接 xAI。

--- KEY FACTS ---
• 旧 Live Search API(search_parameters,$25/1k sources)已于 2026-01-12 移除,请求返回 410 Gone;官方要求迁移到 Agent Tools API——来源: https://github.com/langchain-ai/langchain/issues/33961 、https://community.make.com/t/x-ai-node-integration-error-410-live-search-deprecated-switch-to-agent-tools-api/102097
• 现行形态是 server-side tools(2025-10 上线):请求 /v1/responses 时在 tools 数组加 {"type":"web_search"} / {"type":"x_search"},模型 agentic 自主多轮搜索,服务端自动执行——来源: https://docs.x.ai/docs/guides/tools/overview 、https://docs.x.ai/developers/release-notes
• 官方价格(docs.x.ai/developers/pricing):Web Search 和 X Search 均 $5/1000 次成功调用(=$0.005/次),Collections Search $2.50/1k,File Attachments $10/1k;计费=工具调用次数+token 用量两部分,不再按 source 计费——来源: https://docs.x.ai/developers/pricing
• 计费陷阱:agentic 模式下一次用户请求可能触发多次工具调用(每次各计 $0.005),搜索结果还按正常 token 价进上下文;可用 max_turns 限轮、response.server_side_tool_usage 核账——来源: https://docs.x.ai/docs/guides/tools/advanced-usage
• 数据源从旧版 4 类(web/x/news/rss)收缩为 2 类:web_search(allowed/excluded_domains 各限 5 个 + 图片搜索)和 x_search(帖子/用户/线程,语义搜索,allowed/excluded_x_handles 各限 20 个互斥,日期过滤,视频理解为 X 独有);news/RSS 专用源已消失——来源: https://docs.x.ai/docs/guides/live-search 、https://docs.x.ai/developers/tools/x-search
• 返回格式:默认完整引用在 response.citations,可选 response.inline_citations(行内 markdown 链接);流式下可 include verbose_streaming 实时看工具调用——来源: https://docs.x.ai/docs/guides/tools/advanced-usage 、https://help.apiyi.com/en/xai-grok-api-x-search-web-search-guide-en.html
• 搜索工具只在 Responses API 可用;/v1/chat/completions 已被官方标记为 legacy/deprecated,新功能不再进入——来源: https://docs.x.ai/docs/guides/chat-completions 、https://docs.x.ai/developers/rest-api-reference/inference/chat
• 免费额度:注册送 $25 + Data Sharing Program 每月 $150(团队需先消费 $5,opt-in 后不可退出,共享 API 数据给 xAI 训练)——来源: https://cloudcredits.io/providers/xai/programs/data-sharing-program 、https://www.getaiperks.com/en/blogs/22-xai-grok-free-credits
• 配套模型价(2026-07 官方在售):grok-4.3 $1.25/$2.50 每 M token(1M 上下文)、grok-4.5 $2/$6(500k,官方推荐);第三方另列 grok-4.1-fast $0.20/M input 低价档但官方模型页未展示——来源: https://docs.x.ai/developers/models 、https://pricepertoken.com/pricing-page/provider/xai
• 性价比结论:纯 web 搜索 xAI($0.005/调用且次数不可控)不敌 OpenAI($0.004/次)更不敌 Gemini(5000 次/月免费);唯一独家价值是 x_search 的 X 实时数据(全行业独有),对自学阅读 App 场景价值有限,建议仅在需要 X 内容或愿用 data-sharing 白嫖 $150/月时接入


================================================================================
AGENT RESULT #4
================================================================================
# Grok 旗舰模型智力水平综合评估(2026-07-13 现状)

## 0. 先校正前提:当前各家旗舰是谁

- **Grok 旗舰 = Grok 4.5**,2026-07-08 刚发布(xAI 上市并购 Cursor 后改称 SpaceXAI 的首个发布)。次旗舰 = Grok 4.3(1M 上下文,$1.25/$2.50)。来源:https://x.ai/news/grok-4-5 、https://techcrunch.com/2026/07/08/spacexai-releases-grok-4-5-which-elon-describes-as-an-opus-class-model/
- 你问的 "Gemini 3.5 Pro" **查无此模型**。Google 最新 = Gemini 3.5 Flash(2026-05-19 发布)+ Gemini 3.1 Pro Preview。来源:https://artificialanalysis.ai/articles/gemini-3-5-flash-everything-you-need-to-know
- Claude 最新 = Fable 5(旗舰)/ Opus 4.8 / Sonnet 5(2026-06-30);OpenAI 最新 = GPT-5.6 Sol(2026-07 上榜),GPT-5.5 仍是对照主力。

## 1. 综合智力:Artificial Analysis Intelligence Index(2026-07-13 访问)

| 模型 | AA 智力指数 | API 价格(in/out /M) | 来源 |
|---|---|---|---|
| Claude Fable 5 (adaptive/max) | **60** | — | https://artificialanalysis.ai/models |
| GPT-5.5 (xhigh / high) | **60 / 59** | $5 / $30 | https://artificialanalysis.ai/models/comparisons/gpt-5-5-high-vs-gemini-3-1-pro-preview |
| GPT-5.6 Sol (max/xhigh/high) | 59 / 58 / 56 | — | https://artificialanalysis.ai/models |
| Claude Opus 4.8 (max) | **56** | $5 / $25 | https://artificialanalysis.ai/models 、TechCrunch(Opus 价格) |
| Gemini 3.5 Flash | **55** | — | https://artificialanalysis.ai/articles/gemini-3-5-flash-everything-you-need-to-know |
| **Grok 4.5 (high)** | **54** | **$2 / $6**(缓存命中 $0.50,-75%) | https://artificialanalysis.ai/models/grok-4-5 |
| Claude Sonnet 5 | 53 | $3/$15(9-1 前促销 $2/$10) | https://artificialanalysis.ai/articles/claude-sonnet-5-agentic-cost |
| Gemini 3.1 Pro Preview | 46 | $2 / $12 | https://artificialanalysis.ai/models/gemini-3-1-pro-preview |
| Grok 4.3(次旗舰) | 38 | $1.25 / $2.50 | AA 文章 + https://openrouter.ai/x-ai/grok-4.3 |

**结论:Grok 4.5 = 54 分,历史首次把 xAI 推进第一梯队(AA 官方措辞"第四名,仅次于 Fable 5、GPT-5.5、Opus 4.8"),比前代 Grok 4.3 暴涨 16 分。但离 Fable 5/GPT-5.5 仍差 5-6 分,"Opus-class" 的马斯克说法被独立评测打折——比 Opus 4.8 低 2 分。** 来源:https://artificialanalysis.ai/articles/grok-4-5-brings-spacexai-to-the-the-intelligence-frontier

## 2. LMArena Elo(arena.ai/leaderboard/text,2026-07-13 访问)

- **Grok 4.5 尚未上榜**(发布仅 5 天)。可参照:grok-4.20-beta1 第 19 名 **1475**,grok-4.3 第 63 名 **1443**。
- 对照:claude-fable-5 第 1(**1509**)、gpt-5.6-sol-xhigh 第 8(1486)、gemini-3-pro 第 9(1486)、opus-4.8-thinking(1482)、gpt-5.5-high(1481)、gemini-3.5-flash-high(1476)、sonnet-4.6 第 26(1472)。
- 历史注脚:Grok 4.1 Thinking 曾在 2025-11 登顶 LMArena,xAI 有过人类偏好榜冲顶记录,但当前世代在盲测口碑上落后 Anthropic ~30-60 Elo。来源:https://arena.ai/leaderboard/text

## 3. 主流基准(Grok 4.5 官方 + 第三方)

⚠️ **xAI 发布时没有公布 GPQA/AIME/MMLU-Pro/HLE/SWE-bench Verified 等经典学术基准**,只报 agentic/编码类——这本身是个信号(定位编码/agent 而非全能)。来源:https://benchlm.ai/models/grok-4-5

| 基准 | Grok 4.5 | 对照 | 来源 |
|---|---|---|---|
| SWE-bench Pro | 64.7% | Fable(max) 80.4% / Opus 4.8(max) 69.2% | x.ai 官方页 + benchlm |
| DeepSWE 1.1(第三方标准化 agent) | 53% | GPT-5.5 67% / Fable 5 70% | benchlm.ai/models/grok-4-5 |
| Terminal Bench 2.1 | 83.3% | — | x.ai/news/grok-4-5 |
| AA Coding Agent Index(Grok Build 内) | 76 | 与 GPT-5.5(xhigh)+Codex 持平 | AA 文章 |
| GDPval-AA v2(知识工作) | Elo 1543,第 4 | Opus 4.8=1600 | AA 文章 |
| τ³-Banking(agentic) | 33% | GPT-5.5=31%(小胜) | AA 文章 |

**Token 效率是真实杀手锏**:SWE-bench Pro 每任务平均输出 15,954 tokens vs Opus 4.8 的 67,020(**4.2 倍差**);agentic 任务 1.9M tokens vs Fable 5 的 7.2M、GPT-5.5 的 6.2M。来源:AA 文章 + x.ai 官方页。

## 4. 软指标

- **幻觉率(最大黑点)**:AA-Omniscience 幻觉率 **54%**(前代 Grok 4.3 仅 25%),准确率 35%→52%——"知道得更多但更自信地瞎说"。**在四家旗舰里幻觉口碑最差**,多家媒体单独拎出来做标题。来源:https://artificialanalysis.ai/evaluations/omniscience 、https://roo.beehiiv.com/p/grok-4-5-launch-the-accuracy-trade-off-nobody-headlined
- **长上下文**:窗口 **500k**(反而比 Grok 4.3 的 1M 缩水;GPT-5.6 为 1.05M),且 >200k tokens 部分**价格翻倍**;AA-LCR 单项分数未公布,实测口碑太新查不到。来源:techtimes.com/articles/320038
- **指令遵循**:IFBench 单项分未公布,查不到独立数据(如实说明)。
- **多语言(中/日)**:官方未发中日文基准。历史依据:Grok 4.1 model card 评过 EN/ES/ZH/JA/AR/RU 且改善非英语输出(https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf);日本人 day-1 实测(note.com/kazu_t)对日语输出无负评、赞速度/成本,但明说"不是冲绝对精度的模型";中文场景 BenchLM 中文榜被 DeepSeek V4 Pro(87)/GLM-5.1(83)/Kimi K2.6(81) 霸榜,Grok 不在前列(https://benchlm.ai/blog/posts/best-chinese-llm)。**总体:中日文能力"可用但非卖点",无强口碑背书**。
- **速度/延迟**:AA 实测 119.3 tok/s(快于中位数 73),但 **TTFT 12.61 秒**(high reasoning 档)——对你 App 的交互式阅读助手场景是显著劣势。来源:https://artificialanalysis.ai/models/grok-4-5
- 欧盟暂不可用(7 月中预计开放);知识截止 2026-02-01(docs.x.ai)。

## 5. "每美元智力"判断

AA 实测跑完整套智力指数的成本:**Grok 4.5 = $0.31/任务**(54 分) vs Sonnet 5 = $2.29/任务(53 分)——**同档智力下便宜约 7 倍**;agentic 编码任务 $2.49 vs GPT-5.5+Codex $5.07 vs Fable 5+Claude Code $11.80。来源:AA Grok 4.5 文章 + Sonnet 5 文章。

**性价比结论:Grok 4.5 是当前"每美元智力"的断层第一**(第一梯队智力 × 次级价格 × 4 倍 token 效率),the-decoder 的标题概括到位:"便宜到基准差距可能不重要"(https://the-decoder.com/grok-4-5-is-so-cheap-compared-to-fable-5-and-gpt-5-5-that-benchmark-gaps-may-not-matter-much/)。

**但对本 App 的适用性要打三个折扣**:① 54% 幻觉率对"事实性辅导/讲解"场景是硬伤(我们的场景恰是知识讲解,不是代码 agent);② 12.6s 首 token 延迟不适合阅读器交互;③ 中日文无强背书,而我们重度依赖日语。**建议定位:若引入,当"廉价批处理 agent 层"(夜间制卡/批量摘要/工具循环),而非替换 Claude 的讲解主力或 Gemini Flash 的免费实时层;且我们现有 Claude 订阅 CLI 边际成本为零,Grok 4.5 的价格优势只在超出订阅额度的增量负载上成立。**

## 数据可信度备注
第三方聚合站数字互相打架(如 swfte 称"Opus 4.8 = 61.4""Gemini 3.2 Pro"——后者查无此物),本报告以 artificialanalysis.ai 官方页/文章、x.ai 官方页、arena.ai 官方榜为准;Grok 4.5 发布仅 5 天,LMArena Elo、长上下文实测、中日文实测口碑均属"数据尚不存在",已如实标注。

--- KEY FACTS ---
• Grok 旗舰 = Grok 4.5,2026-07-08 发布,$2/$6 每百万 token(缓存命中 $0.50),500k 上下文(比前代 1M 缩水),>200k 部分价格翻倍 — https://x.ai/news/grok-4-5 + https://artificialanalysis.ai/models/grok-4-5
• AA 智力指数:Grok 4.5 = 54,史上首次进第一梯队(第4名),仅次于 Claude Fable 5(60)、GPT-5.5 xhigh(60)、Opus 4.8 max(56);比前代 Grok 4.3(38)暴涨 16 分,但低于 Opus 4.8,'Opus-class'宣传打折 — https://artificialanalysis.ai/articles/grok-4-5-brings-spacexai-to-the-the-intelligence-frontier
• 对照组:Gemini 3.5 Flash = 55(2026-05-19 发布,'Gemini 3.5 Pro'不存在,Pro 线最新是 3.1 Pro Preview = 46)、Claude Sonnet 5 = 53、GPT-5.6 Sol max = 59 — https://artificialanalysis.ai/articles/gemini-3-5-flash-everything-you-need-to-know + https://artificialanalysis.ai/articles/claude-sonnet-5-agentic-cost
• xAI 发布时未公布 GPQA/AIME/MMLU-Pro 等经典学术基准,只报编码/agent 类:SWE-bench Pro 64.7%(vs Fable max 80.4%、Opus 4.8 max 69.2%)、第三方 DeepSWE 1.1 仅 53%(vs GPT-5.5 67%、Fable 5 70%)— https://benchlm.ai/models/grok-4-5
• 最大黑点=幻觉:AA-Omniscience 幻觉率 54%(前代仅 25%,准确率 35%→52%),四家旗舰中幻觉口碑最差,对事实性辅导场景是硬伤 — https://artificialanalysis.ai/evaluations/omniscience + https://roo.beehiiv.com/p/grok-4-5-launch-the-accuracy-trade-off-nobody-headlined
• 真实杀手锏=token 效率:每任务输出 tokens 比 Opus 4.8 少 4.2 倍(15,954 vs 67,020),agentic 任务 1.9M vs Fable 5 的 7.2M — https://artificialanalysis.ai/articles/grok-4-5-brings-spacexai-to-the-the-intelligence-frontier
• 每美元智力断层第一:跑 AA 智力指数 $0.31/任务(54分) vs Sonnet 5 $2.29/任务(53分),同档智力便宜约 7 倍;agentic 任务 $2.49 vs GPT-5.5+Codex $5.07 vs Fable 5+Claude Code $11.80 — 同上 AA 文章
• LMArena:Grok 4.5 尚未上榜(发布仅5天);参照 grok-4.20-beta1 第19名 Elo 1475、grok-4.3 第63名 1443,vs claude-fable-5 第1名 1509 — https://arena.ai/leaderboard/text (2026-07-13 访问)
• 中日文:官方无基准,Grok 4.1 model card 曾评 ZH/JA 并改善非英语输出;日本用户 day-1 实测无负评但称'非冲精度的模型';中文榜被 DeepSeek V4 Pro/GLM-5.1/Kimi 霸榜,Grok 不在前列 — https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf + https://note.com/kazu_t/n/n9eac6270b41f + https://benchlm.ai/blog/posts/best-chinese-llm
• 交互延迟劣势:TTFT 12.61s(high 档)、119.3 tok/s,不适合阅读器实时交互;适合定位=廉价批处理 agent 层(夜间制卡/批量摘要),且欧盟暂不可用 — https://artificialanalysis.ai/models/grok-4-5
