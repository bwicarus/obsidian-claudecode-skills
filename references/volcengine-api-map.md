# 火山引擎/豆包 API 能力地图(2026-07-10 七域并行文档 sweep 的精选结论)

> 来源:7 个研究 agent 并行翻官方文档+第三方交叉(328 次检索)。置信度:标 ⚠ 的需实测。
> 🔑 鉴权总原则(官方原文):**现有 UUID 语音 key(X-Api-Key header)通吃豆包语音全线**(S2S/ASR/TTS/同传/播客/妙记/机器翻译)——"在任意接口中填入即可,不用填写 appid";但**每个能力要在语音控制台单独开通授权**(当前已授权:volc.speech.dialog / volc.bigasr.sauc.* / volc.service_type.10029)。方舟 ark key 是另一摊(文本/视觉),**两把 key 不互通,方舟无语音模型**。

## A. 零开通、现有 key 立即可用(高价值排序)

| 能力 | 要点 | 本系统接入点 |
|---|---|---|
| ⭐**日语 TTS 音色**(7 个,10029 已授权) | multi_* 跨语种音色须 `additions.explicit_language:"ja"` 否则汉字按中文读 | **vocab 日语发音音频空洞**:批量合成落盘 `_audio/` + Anki 配音 + 阅读器点词发音 + Tanaka 例句朗读。全量生成一次性 ≈15-20 元 |
| ⭐**TTS 字级时间戳**(`enable_timestamp`,**仅 TTS1.0 音色**;2.0 只有 enable_subtitle 不到词级) | words[{word,startTime,endTime}] | **朗读跟随高亮**(Kindle immersion reading):时间戳对齐 char-layer/EPUB 节点,audio.currentTime 驱动逐词高亮 |
| ⭐**S2S ConversationTruncate(513)** | 需 `enable_conversation_truncate:true`(2.0 模型);payload {item_id, audio_end_ms} | 打断对齐:浏览器播放是客户端缓冲,打断时服务端以为整段播完;513 把模型记忆截到**用户实际听到的位置** |
| ⭐**S2S push_to_talk 模式** | `input_mod:"push_to_talk"` 屏蔽服务端 VAD + 客户端事件 400 EndASR(松键)+ 515 ClientInterrupt | iPad 按住说话、松手即答;根治环境噪声/朗读声误触发 |
| S2S 纯文本模式 | `input_mod:"text"` + 501,服务端自动补静音 | "把这段讲给我听":选中文字直接语音讲解,省输入音频 token(¥10 vs ¥80/M) |
| S2S 录音文件模式 | `input_mod:"audio_file"`,20ms/包+休眠 20ms | 跟读练习:用户朗读录音整段喂给模型点评 |
| SayHello(300) | 接通即播指定开场白 | "继续读《xxx》第 n 页?" |
| TTS LaTeX 朗读(`latex_tn`)⚠ | 配 formula-ocr 的 $..$ 文字层 | 数学书整页朗读能把公式念对 |
| 异步长文本合成(≤10万字符/次)⚠ | 同 10029;整章一次 | 夜间批量生成整章有声书 + 时间戳 sidecar,≈5-6 元/万字 |
| 上下文管理 510-514 | 511 可改写已发生轮次(ASR 听岔事后纠正);512 拉服务端记忆;514 删轮 | 备用工具箱 |

**已实现项的勘误**:①热词只在 `enable_asr_twopass=true` 时生效(我们已开,没踩坑);②**热词/替换词只支持中英文——日语热词此路不通**(日语只能试 `corpus.context` 直传,未验证);③替换词正确参数名是 `corpus.correct_table_id`(不是 regex_correct_table);④ChatRAGText 上限 4K 字符(已知)。

## B. 控制台点几下就有(免费或便宜)

| 能力 | 开通 | 本系统接入点 |
|---|---|---|
| ⭐**方舟模型开通**(解 ModelNotOpen 正解) | 控制台「开通管理」逐模型点开通,**免费即时**,每模型送 50 万 token | 开 `doubao-seed-2.0-lite/mini + 1.6-vision + seed-translation` → ai_backends 加 ark adapter(Gemini 级兜底,中文母语级);vision 做看图第二兜底;1.8 短输出仅 2元/M 适合查词类 |
| ⭐**Seed-Translation 翻译模型** | 同上,免费开通 | 插 translate.py auto 链,LLM 带源语言识别,正治**中日同形词 echo** 老毛病 |
| ⭐**融合信息搜索**(S2S 内置联网同源) | console 自助开通,**月 500 次免费**,并发≤5 | ①助手加通用 web_search 工具(补 B站/YouTube 之外的缺位)②同一把 key 填 S2S `volc_websearch_api_key` 让伴读能查时效信息。端点 open.feedcoopapi.com,Bearer 鉴权 |
| **录音文件识别 auc 2.0** | 语音控制台开通 volc.seedasr.auc | **替代健身管线 Google Cloud STT**:0.8 元/h(Google ≈7 元/h),utterances 时间戳直接拼 SRT;闲时版 1.2 元/h 契合夜间批处理 |
| 流式 ASR 2.0(seedasr) | 语音控制台开通 | 价格是 1.0 的 22%,还能带视觉上下文;⚠日语支持公告说有、文档语种表存疑,实测再切 |
| ⭐**语音播客大模型**(10050) | 语音控制台开通 | 笔记/收藏夹章节 → 双人对话播客(action=0 自动成稿),≈9 元/小时;挂收藏夹 NotebookLM 入口旁"变播客"按钮;**仅中文** |
| 机器翻译大模型(volc.speech.mt) | 语音控制台开通 | translate.py 另一档(32 语种含日语,带术语表) |
| 同传 2.0(10053) | 语音控制台开通 | 日语/英语视频实时双字幕(s2t);⚠贵:s2s ≈27 元/h,片段用;"中文进日语语音出"未获文档保证须实测 |
| 妙记(volc.lark.minutes) | 语音控制台开通 | 长英文视频一发入魂(转写+章节+摘要),1.8 元/h;**不支持日语** |
| 自动字幕打轴(legacy)⚠ | 旧版控制台 | **有声书对齐**:原文+音频→句级时间戳→"边听边读"高亮;legacy 接口先小样验证 |

## C. 确认不存在/死胡同(省得再找)

- **语音评测/发音打分**:豆包语音线**没有**此产品(要做发音打分只能讯飞 ISE / 腾讯 SOE / Azure Pronunciation)
- S2S 的字级时间戳/波形输出:无(只有句级 350/359)
- 官方 Web/JS SDK for S2S:不存在(iOS/Android only;自建 wss relay 就是正解)
- 方舟 ark key 直调 Seed-ASR/Seed-TTS:不存在,语音全在语音技术线
- 独立 VAD HTTP API / 独立声纹 API:不存在
- character_manifest(SC2.0 角色卡)公开 schema:文档没有,只能实测摸
- Coze/HiAgent:与本系统无交集

## D. 备忘细节

- S2S 计费实数:输入音频¥80/M、输入文本¥10/M、cached¥5/M、输出音频¥300/M、输出文本¥30/M;当轮用户音频≈6.25 tok/s,输出/上下文音频≈25 tok/s
- 方舟分段计费坑:按**本次请求输入落在哪个区间**给全部 token 定价——书页上下文控制在 32k 内最划算;1.8 输出≤200 token 仅 2 元/M
- RTC 对话式 AI 的独特点:CustomLLM 模式可把**自己的大脑**(Claude)接进语音闭环(火山只管 RTC+ASR+TTS)+ WebRTC 回声消除/弱网——若 relay 在 iPad 上碰到回声/弱网天花板,这是备胎路径
- 官方 ai-app-lab(GitHub volcengine)有网页语音伴读完整参考实现(live_voice_call)
- **📌 文档 JS 壳破解法**:`https://r.jina.ai/<原URL>` 前缀可稳定拿到 docs.volcengine.com 正文(WebFetch 直接抓约一半概率只有导航壳)
