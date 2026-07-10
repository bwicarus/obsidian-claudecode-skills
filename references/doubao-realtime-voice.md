# 豆包端到端实时语音通话(S2S)

> 2026-07 上线,用户实测通话正常。音频进音频出、可打断、带人设,像豆包 App 的语音通话。
> 入口:`https://bwicarus.taile44d0c.ts.net/static/pdf/voice-call.html`(点 📞 说话即聊)。

## 架构

```
浏览器通话页 voice-call.html(AudioWorklet 采麦克风 → 降采样 PCM16k int16 → 20ms/640B 包)
   ↕ wss …/voice-rt(nginx 80/443 反代,ws Upgrade + 3600s 超时)
voice_realtime_relay.py(systemd voice-rt.service,mcp-venv,127.0.0.1:8767)
   ↕ wss openspeech.bytedance.com/api/v3/realtime/dialogue(豆包自定义二进制协议)
豆包端到端 S2S(O2.0,model="1.2.1.1";回传 PCM s16le 24k)
```

中继存在的原因:浏览器 WebSocket 发不了自定义 header + 豆包是二进制协议。

## 鉴权(⚠ 关键坑)

- **新版 = 单 API Key(UUID 形状)**:header 只要 `X-Api-Key: <key>` + `X-Api-Resource-Id: volc.speech.dialog`。
  凭证:`~/.config/doubao-voice.json` `{"api_key": "..."}`(600,不进 git;relay 现读,改了不用重启)。
- 旧版双凭证(X-Api-App-ID + X-Api-Access-Key + 固定 X-Api-App-Key=PlgvMymc7f3tQnJ6)relay 也兼容,但**官方接入文档写的是旧版**——照文档实现会一直 401「grant not found」。当时用户坚持"新版直接用 api key"才试出正解。
- **火山三套凭证互不通用**:方舟 ark key(`ark-...`,文本模型)/ 语音 API Key(UUID,S2S)/ GCP 式双凭证(旧)。
- 服务端报错有指向性:`grant not found`=凭证组合无开通记录;`resourceId ... not allowed`=header 已被正确解析(可用来自证代码没问题)。

## 豆包二进制协议(relay 内实现)

帧 = `[0x11, type<<4|0b0100, serialization<<4|0, 0]` + event(int32 BE) + [session_id(len+bytes)] + payload(len+bytes)。
- 上行:StartConnection(1)→StartSession(100,JSON 配置)→TaskRequest(200,裸 PCM)/SayHello(300)/ChatTextQuery(501)/FinishSession(102)
- 下行:SessionStarted(150)/ASRInfo(450,**用户开口=打断信号**)/ASRResponse(451 字幕)/ChatResponse(550 流式文本)/TTSResponse(352,**audio-only 帧**=PCM24k)/TTSEnded(359)/DialogCommonError(599)
- StartSession 要点:`dialog.extra.model="1.2.1.1"`(O2.0,必传)、`input_mod="keep_alive"`(静音不断流)、`tts.audio_config={format:"pcm_s16le",sample_rate:24000}`(浏览器免解码)。
- ⚠ `ChatTextQuery`(501)在 keep_alive 模式只回文本**不合成音频**;测音频链路用 SayHello。

## 前端要点(voice-call.html,nginx 静态)

- 采集:AudioWorklet(Blob URL 内联)→ 主线程凑 20ms 段降采样 48k→16k int16 → ws 二进制帧。
- 播放:下行二进制帧=PCM24k → AudioBuffer 排队(`playT` 累计起点);**event 450 到达即 stop 全部 source 实现打断**。
- iOS:AudioContext 必须在点击手势内 resume;wss 走同源 443。
- 人设/音色可调:`~/.config/doubao-voice.json` 可加 `bot_name/system_role/speaking_style/speaker/model`(relay `_start_session_payload` 读)。音色:vv(默认)/xiaohe(台湾腔)/yunzhou(沉稳男)/xiaotian(磁性男),全名如 `zh_female_vv_jupiter_bigtts`。

## agent 模式(2026-07-10 第五批,**现默认**):豆包只当耳+嘴,大脑=侧栏助手

用户拍板:S2S 对工具失明(正则旁路+幻觉承诺是死胡同)→ 换架构:**豆包流式 ASR(耳)+ 豆包流式 TTS(嘴)+ 原侧栏助手完整管线(脑)**。说话=直接问助手,29 个工具/对话历史/视频卡/撤销卡全部原生可用,过程全在侧栏可见。

- **授权实测**:同一把语音 API Key 对 `volc.bigasr.sauc.duration|concurrent`(流式ASR)和 `volc.service_type.10029`(TTS)grant 全通,零开通操作。⚠ TTS 音色只认 `*_moon_bigtts` 系(默认 `zh_female_shuangkuaisisi_moon_bigtts`,凭证文件 `tts_speaker` 可覆盖);`vv_jupiter` 系是 S2S 专属,用了报 55000000 "resource ID is mismatched with speaker"。
- **链路**:同一 relay/端口,`?mode=agent` 分流 → `handle_agent`:单 ws 全双工——上行二进制=麦克风 PCM16k、上行 JSON `{type:'speak'|'cancel'}`;下行二进制=TTS PCM24k、下行 JSON `{event:'agent_ready'|'asr'(字幕)|'utterance'(终稿句)|'tts_end'}`。大脑不经 relay:前端拿 utterance 调 `window.__asstSend`(rc-assistant 暴露的 send)。
- **耳(sauc)协议要点**:v3 seq 帧(≠dialogue 的 event 帧):header `[0x11, type<<4|flags, 0x11, 0]` + seq(int32 BE) + size + **gzip payload**;full request(0b0001)带 config,audio-only(0b0010)100ms/3200B 包;response(0b1001)result.text=全量累计、utterances[].definite=句终稿。**⚠ 必须设 `request.end_window_size`(默认 800ms,凭证 `asr_end_window_ms` 可调)**——不设的话连续流里 definite 永远不翻(独立测试靠负 seq 最后帧强制定稿才看到,连续对话没有最后帧)。
- **嘴(TTS,2026-07-10 二版=双向流式)**:`wss /api/v3/tts/bidirection`,event 帧族同 dialogue(enc/dec 直接复用)。协议:StartConnection(1)→StartSession(100,payload `{"user","event":100,"namespace":"BidirectionalTTS","req_params":{speaker,audio_params:{format:"pcm",sample_rate:24000}}}`)→TaskRequest(200,`req_params:{text:片段}` 可连发)→FinishSession(102)→音频帧(0b1011)+SessionFinished(152)。**一轮回答=一条连接+一个 session**:片段连续合成 → 韵律连贯(修"按句独立合成念得生硬")、实测首块音频 ~1s。**打断=直接 close 连接**(⚠FinishSession 语义是"把已收文本全部合成完",打断绝不能用它),实测残余 0 字节;重开 ~1.3s。旧 HTTP 单向端点(`POST /api/v3/tts/unidirectional` NDJSON,ws 探它 405)留作备用 `_tts_stream`。
- **前端**(rc-voicecall agent 模式,二版=无浮层):**不再建通话方块**——状态全在 `#asst-call` 按钮(绿呼吸=在听,蓝快脉冲=在念)+ 侧栏输入框(ASR 进行中转写直接写 `#asst-ta`,placeholder 提示状态;终稿清空并发送)。utterance → `sendToAssistant`(忙时覆盖式排队 pendingUtter,回答完自动发且不掐尾音;新问题 bargeIn 停旧播报);回答朗读靠 rc-assistant 两处 tap `window.__asstVoiceTap(全量文本, done)` → **逗号级**边界(。!?;,\n)切片段即时喂同一 TTS session,done 时发 `speak_done`(=FinishSession 合成尾巴);`cleanForSpeech` 剥 markdown/公式。`window.__asstVoiceOn` → 发送体带 `voice:1` → assistant.py `_sys_prompt` 注入"适合朗读"风格(口语短句/别写 LaTeX/先结论/转写或有同音错字按语境理解)。
- **ASR 质量注记**:sauc 是独立识别、无对话语义上下文,专业词识别弱于 S2S 端到端(用户已观察到)。已有缓解:voice prompt 让大脑按语境纠错理解同音错字。候选改进(未做):火山热词/自学习平台 boosting 表;把当前页关键词做热词;ASR 终稿先过轻量纠错。
- S2S 模式保留:`RC.voicecall.toggle({mode:'s2s'})`(翻页同步/🧹/意图旁路仅 s2s 用;agent 模式上下文天然跟侧栏 ctx() 走)。
- 冒烟:TTS 合成音频回灌 ASR 一字不差;`?mode=agent` 灌语音 → utterance → speak → 2.65s 语音 + tts_end,全通。
- 延迟代价(明知选择):S2S 开口回话 1~2s;agent 模式=ASR 定稿(0.8s 窗)+ 助手首 token(数秒,工具轮更久)+ TTS 首句,换来真编排能力。

## S2S 工具协议 v3(2026-07 第八批,**现行**;用户定调:与编排 agent 完全同一套协议,升级永远同步)

第七批的「我去查→整套 chat 编排」被替换:S2S **亲自当编排者**,输出与编排 agent **同一格式**的 `{"tool":"名","args":{...}}`(独立成句)。链路:

- **SP**:人设+直塞上下文+**工具目录**(开话拉 `/api/assistant/tools`,desc 压缩成第一句≤52字——O2.0 上下文 12K 硬限)+协议(先说自然话→紧接一条 JSON→停;args 字符串里用「」别用双引号)。
- **relay 550**:攒 reply → `_extract_tool_json`(标准 raw_decode)完整即 fire;**字幕重组转发**(sent_len 记账+尾部 `{"tool` 撕裂前缀 hold)→ JSON 整段不进字幕;559 兜底把含 `"tool"` 的整轮交服务端。
- **命令句静音 v3(现行:纪律+早触发+tts_type 状态机)**:v2 的两个盲区被用户实测暴露——①fire(JSON 完整)时**自然话音频还没传完**→腰斩用户听到的话("断声+字幕突变"bug);②纯 JSON 轮 TTS 合成超实时,fire 时**念白早放出去 6 秒**。根治三件套:**(a)纪律对齐编排 agent**:调工具时整条回复只输出 JSON,一个字不多说(编排 agent 原话同款)——没有自然话可误伤;**(b)确认语代播**:fire 后 relay 经 ChatTTSText(500) 指定文本播确认语(`_ACK_TEXT` 按工具映射,"好,我找找看"),音频完全可控,前端 550 字幕同步补;**(c)音频状态机**:`{"tool` 开头一出现(suppress 时刻)立即 drop_audio=True(不等 JSON 完整);350.tts_type∈{chat_tts_text, external_rag} → drop=False(确认语/播报轮绝不误丢);359 兜底复位。真语音实测:JSON 轮漏出 **0B**、确认语 1.6s、播报 5.6s 完整。听感=「好,我找找看」→安静执行→「给你找到…」。
- **旧 v2(时序法)记录**:⚠实测推翻文档——**S2S 的 350 TTSSentenceStart.text 恒为空**且粒度=整轮 TTS(非每句),350 判文本的 v1 方案不可行(用户真机实锤"文字没显示但读了出来")。v2:协议保证 JSON 在回复**末尾** → relay 在 550 流检测到完整 JSON(fire 时刻)后**丢弃此后到达的所有音频帧直到 359 TTSEnded**(文本流领先音频流,fire 后到达的音频≈JSON 段)。真语音链路实测:闲聊轮音频 18s(含 JSON 念白)→1.2s(恰好"我来查一下"),RAG 播报轮完整无损。v1 代码留作兜底(万一某版本 350 带 text)。
- **实时墨迹随工具走**:`_t_see_ink` 等工具吃 `ctx["ink"]`(侧栏原行为),壳子 voice-tool 的 ctx 曾漏传 → "说我没画"的根因(另一半=sidecar 防抖延迟)。修:relay ink 消息存 `book["ink_strokes"][:60]`,`_run_voice_tool` ctx 带上 → see_ink 用内存实时墨迹合成,不等保存。
- **任务进程状态进 SP**(用户设计):`_run_voice_tool` 起止各 `_push_sp`——SP 段"⏳系统正在后台执行:X(用户催→告诉他正在做请稍等,绝不重复调用同一工具)";墨迹新鲜度 `ink_fresh`(通话中刚画的)也进 SP;翻页清 ink_strokes/ink_fresh。
- **选中/chip 状态实时同步(v3-④)**:21-misc-ai 轮询直接调 `window.__voiceContext()`(与侧栏完全同源)取 selection/focus_sel/figures → `RC.voicecall.syncState`(指纹去重,**变空也推**=取消选中同步)→ relay `{type:"state"}` → book 比对变了才 `_push_sp`。SP:有选中给原文("他说『这段/我选的』就指它")、无选中显式声明("现在没选中,不是你看不到"防旧毛病措辞);focus/figs_n 同段。工具 ctx 也带 `selection`(read_selection/translate/make_anki 同侧栏口径)。**清空联动**:document 捕获阶段旁听 `[data-q="clear"]` 点击(共享/native 两版按钮同名)→ s2s 通话中→teardown+fresh 重连(语音记忆同步清)。冒烟:选中→准确复述+翻译;取消→答"现在没选中"。
- **笔迹版本三态(用户实测暴露:加笔后它拿旧印象答"没变化")**:`ink_ver` 每次变化+1(前端指纹去重,到达即真变),`ink_seen_ver`=最近一次 see_ink/see_page/see_figure 成功时记账,换页清零。SP 三态:没看过→"调 see_ink 看";看过且 ver==seen→"没有新变化,可直接说没变"(正向也程序化,不白跑);**看过但 ver>seen→"⚠他在你上次查看之后又画了,你的记忆已过时,必须重新调 see_ink,严禁答『没有变化』"**。缓存键的墨迹指纹同步改用 ink_ver。冒烟:画→看(识别出图形)→加两笔→问变化→**重新看并对比出增量**("引线分叉了,分别指向…")。通话级 `book["tool_cache"]`,key=`工具|args(sort_keys)|页码|墨迹指纹`——翻页/新画自然失效;**只读工具才缓存**(名单 `VOICE_CACHEABLE_TOOLS` 放 assistant.py 挨着 TOOLS,voice-tool 返回 `cacheable` 标志,relay 只认它;写操作"再做一张卡"是合法语义绝不缓存)。命中→重放 client_action+RAG 标注"此前同样查询的结果直接复用,没有重新执行"→秒回。实测:同问两次 read_highlights,第二次 cached=True 1s 回,模型用自己的话重新播报。上限 20 条 FIFO。另观察:模型自己也会省略重复(口头答"刚才找过了")——两级防重复互补。
- **see_ink 对比模式(用户实测:花补几笔被认成"花丸+小太阳"两个东西)**:根因=视觉调用每次独立看图、不知道"上次这里是什么"——补笔被当成新物体。修在**工具层**(侧栏同受益):`_t_see_ink` 支持 `ctx["prev_ink_desc"]` → note 加对比指引("此前这块是「…」,用户又添了笔画;新增常是**同一图形的补笔**,先判断整体,确实独立才算新东西");relay 侧 see_ink 成功后存 `book["last_ink_desc"]`,下次调用随 ctx 带上。冒烟:补两笔后描述为"在**原来那个**菱形左边**多了**一条斜线"(增量口径,不再拆成两个物体)。副产品验证:①还在跑时问②,模型答"要我调用工具看看吗"=任务状态防重复真实生效。
- **状态门控工具目录(用户设计:无笔迹连工具都不给)**:`_fetch_tools_lines` 返回 {name:行},`_role_text` 组装时按状态过滤——has_ink=False→摘 see_ink+SP 显式"本页没有任何笔迹,直接说没画";figs_n=0→摘 see_figure;无选中→摘 read_selection。工具的存在与否跟着状态走,无模糊地带且省上下文。冒烟:无笔迹问"画了什么"→"这页目前没有任何手写笔迹哦"(秒答零调用)。
- **深度思考分流(v3-⑥)**:虚拟工具 `deep_think`(不在 TOOLS 注册表,relay 目录注入 `DEEP_TOOL_LINE`+拦截)——统一 JSON 协议让它自动获得静音/确认语("稍等,让我好好想想")/状态按钮。执行=`_run_deep_think`:调 `/api/assistant/chat`(SSE,凭证 `deep_model`/`deep_effort` 经 force_model/force_effort 指定深度模型,⑦接设置 UI)→ answer 增量攒句(`_SENT_SPLIT`)→ **ChatTTSText(500) 分片边生成边播**(start/content…/end)+550 字幕同步+`_speech_clean` 剥 markdown;client_actions 照旧下发。**打断**:450 置 `book["deep_abort"]`→停发且**不补 end 包**(官方要求);答案完成后 **510 注入问答摘要**(ChatTTSText 是否进上下文文档未明说,注入保追问不失忆)。冒烟:推导类问题→deep_think→确认语→13.8s 流式代播,全通。
- **工具调用状态按钮(v3-⑤)**:执行通知**不进侧栏对话流**——`tool_status` 事件收敛到固定小按钮 `#vc-tool-btn`(📞 右侧):running=CSS 转圈、done=绿✓、error=红⚠;点击弹 `#vc-tool-pop` 详情(最近 8 次调用:label/args/耗时/result_brief/缓存标记)。旧 intent 气泡逻辑(relay 已不发该事件)删除;**视频结果是内容非通知**,照旧进侧栏(承载气泡+rc-video 可播放卡)。
- **记忆注入(用户裁定替代程序兜底:兜底有漏洞且模型升级体验不跟涨;改为"更新 AI 的认知")**:两个残余洞的修法——①通话重连(iOS 断线)后 book 重建,`see_ink` 拿空 ctx.ink 报"没有笔迹" → `_t_see_ink` 加 `_page_ink_strokes` sidecar 回退;②dialog 历史"看不到"旧话压过 SP → **ConversationCreate(510) 按时序注入事件对**:笔迹变化防抖平息时,向对话历史末尾追加 user"(我刚在本页添加了笔迹,现共 N 笔)" + assistant"收到,我之前看到的已过时,再问我会调 see_ink 重新看"——**"历史压 SP"的机制反用**:把正确认知+它自己的承诺写成最近记忆,模型自我一致倾向让它下轮兑现。10s 冷却防灌 20 轮历史(风暴只注一条),每个未看版本至多一次。带历史+风暴冒烟:重新查看+增量描述准确;曾做过的 `_INK_QUERY` 程序兜底已按用户意见撤除(翻页兜底保留——那是动作补执行,非认知问题)。
- **加笔风暴丢 UpdateConfig(用户实测:加很多笔它毫无反应,答"还没同步过来")**:日志显示 relay 同步全成功(v2→v7),但 20s 内 6 连发 UpdateConfig 疑被豆包丢弃/未生效,而用户开口时指纹已无变化不再推 → SP 停在旧版。修三件:①`_push_sp_debounced`(1.2s 防抖,风暴合并成平息后一次;翻页/任务起止/开口仍立即推);②**450 用户开口时无条件重推最新 SP**(兜被丢的底,ASR 处理话音的 1-3s 内生效);③过时警告加"若你此前说过『没看到新的笔迹』那是同步没到时的旧话"对冲。复刻冒烟(6 轮 2s 加笔+带历史)通过。
- **画完立刻问的竞态(用户实测:加笔问→说没变化,追问才查;离远画新内容"像没画过")**:根因=墨迹同步 2s 轮询 vs 用户画完秒问——模型答题时 ink_ver 还没+1,三态正向分支+缓存键都拿着旧版本。修三层:①**说话即同步**——450(用户开口)事件时前端立即调 `window.__vcSyncNow()`(21-misc-ai 把轮询体抽成此函数,轮询降为兜底;ASR 处理用户的话要 2-5s,同步稳赶在模型答题前);②三态 ver==seen 分支加"**他若说自己刚画了,以他说的为准**(可能是同步延迟),调 see_ink 别争没变化";③**⚠prompt 反噬教训**:"严禁答『没有变化』"诱导模型**编造一个变化**(没调工具就描述出"蓝笔圈了母子保健"纯幻觉)——负面禁令会被理解成"必须有 X";改成正面行为指令("你现在并不知道新笔迹是什么,没看就描述=编造;唯一正确回复是 {\"tool\":\"see_ink\"...}")后,竞态冒烟(加笔后 0.3s 问)重新查看+增量描述准确。
- **执行 = `/api/assistant/voice-tool`**(webapp 新端点,与编排 agent **物理同源**:`_parse_tool` 顽强解析→正则级修补(中文引号/尾逗号)→dispatch TOOLS);解析失败返回 feedback(编排 agent 同款自愈措辞)→ relay 经 ChatRAGText 喂回让它重出。
- **结果三路**:client_action→页面(实测 goto_page 出的是 `jumpWithBack`,与侧栏完全同款=统一协议红利)/ 文本 slim 后 ≤3000 字 ChatRAGText 回 S2S 播报 / `tool_status` 事件(running/done/error+label+args+took_s+result_brief)→ 前端状态按钮(⑤)。
- **翻页唯一特例兜底**:模型对轻操作顽固嘴上应承不出 JSON(fresh 实测)→ 559 时它说过「翻到第N页」且无 fired → 直接下发 goToPage;翻页后 setPage→UpdateConfig 自动把新页同步回模型,闭环自洽。
- **笔迹回归原设计(用户裁定)**:撤文字直塞;SP 只留 has_ink 状态+指路 see_ink 工具(合成图给图分析);ink 同步只更新状态布尔。
- ⚠prompt 措辞会被模型照念(「另起一句」被它念出来)——例子里别放动作指令词,直接展示完整回答格式。
- 冒烟:找视频(字幕干净+6视频+RAG 播报主题正确)/翻页(真出 JSON 调 goto_page)全通;历史污染仍是最大干扰源(旧协议轮次会压新 SP,🧹 fresh 可清)。

## S2S「套子」架构(2026-07 第七批,已被 v3 取代;ChatRAGText 机制沿用)

第六批的"直调工具"暴露三病(用户截图实锤):①模型在结果回来前**编造**"找到的视频";②直拿关键词搜质量差;③看不到用户圈画。第七批统一治:**S2S 只当人格外壳,一切实际工作交原有助手 agent 完整编排**:

- **通用协议「我去查:任务描述」**(替代每功能一句式):system_role 教它凡需实际查看/操作(看圈画/看图/找视频/查资料/总结/做卡)都说这句,冒号后一句话说清任务——实测模型把书页语义细节全组织进任务描述(如"找几个讲日本公共卫生(结合第3页阿拉木图宣言、PHC 8项活动)的日语视频,优先院校公开课"),这就是给 agent 的高质量输入。「翻到第N页」保留直达;「关键词是X」旧句式兼容(也升级走编排)。
- **执行 = `_run_agent_task`**:POST `/api/assistant/chat`(SSE,voice=1,ctx={file_rel,page})→ `tool` 事件转 intent 进度、`actions` 即时下发 client_action(视频卡/跳页实时生效)、`answer` 攒全量 → 剥 FOLLOWUP → **ChatRAGText(502) 回填**(content 尾注"你此前口头猜测的内容一律作废,以本结果为准")。圈画场景零额外代码:助手 `_sys_prompt` 本就注入本页手写提示+有提取/see_page 工具。
- **防幻觉三件套**:①prompt 铁腕闭嘴令("说完这句立刻停,一个字都不要再说,你自己看不到屏幕,编的结果会害用户");②**550 增量即时触发**(协议句一说完整就开跑,不等整轮 559,缩短它自由发挥的窗口;`reply_fired` 集合与 559 兜底去重,增量态正则要求句式后随标点防半句触发);③RAG 尾注作废声明。实测:播报的是助手**真实**结论(甚至诚实说"没找到特别对口的日语视频"并给搜索建议——诚实的失败好过编造的成功)。
- 前端浮层 intent 事件升级:running 显示任务/工具名滚动,done 显示"执行完成"。
- **直塞分层(第七批补丁,用户指正)**:上下文能直接给的纯文本**不走中间层**——用户截图实锤"这页讲什么"也去跑了几十秒编排(页文本明明已在 SP 里)。修:①`/api/assistant/voice-ctx` 聚合端点(圈画文字 `_text_under_ink` + 本页插图离线描述 `_figdescs_for`,均现成函数);②`_fetch_book_ctx` 拉三件套(页文本/圈画/图描述)、`_role_text` 全塞进 SP+边界令("直接给你的内容能答的直接答,**禁止**说我去查;只有手头没有的才去查");③翻页 UpdateConfig 三件套整体换页。实测:"能看到这页吗"秒答不走协议,"找视频"正确走「我去查」。分层原则:**纯文本现成→直塞;要动手/要视觉/跨页→套子**。
- **⚠ 壳子不得超原功能范围(用户纠偏 ×2)**:手写笔迹的原有语义是两级——`_text_under_ink` 文字只是"几何上大概标在 X 附近,**仅参考**",主路径是 `see_ink` **看笔迹合成图**(位置/形状/箭头/手写字要视觉判断)。壳子第一版错把参考文字当"直接答的事实"→ 已改对齐:SP 里标注为"仅几何位置参考",涉及标注的问题除"就是问参考文字里那个词且无歧义"外**一律「我去查」**(助手编排会自己走 see_ink 视觉);纯涂画(提取不到字)时 `has_ink` 仍进 SP,一律去查。改壳子前先读 `_sys_prompt` 原逻辑,措辞语义照搬,别自创升格。
- **通话中圈画热同步(用户实测暴露的时序洞)**:SP 是开话/翻页快照,通话中**新圈**的它不知道 → 21-misc-ai 的 2s 轮询在 setPage 外加 `syncInk(currentPage, _ink.byPage[currentPage])`(**内存实时墨迹**,不等 sidecar 防抖保存——对齐侧栏 ctx["ink"] 机制;指纹去重,换页重置);rc-voicecall 发 `{type:"ink",page,strokes[:60]}` → relay POST `/api/assistant/voice-ctx`(改支持 POST+strokes,`_text_under_ink(strokes=实时)`)→ `_push_sp()`(UpdateConfig,与翻页共用)。⚠**历史污染坑**:机制通了(251 ack+提取到字)模型仍嘴硬"看不到"——dialog_id 记忆里它旧轮次说过"看不到你圈的位置",压过新 SP;fresh 对照实验实锤;修=SP 笔迹段加对冲句("如果你之前说过看不到,那已过时,以本条为准")→ 带历史也正确走「我去查」。**SP 指令与 dialog 历史矛盾时历史常赢,要显式否定旧说法**。
- **直塞第四件:本页未掌握生词**(用户实测"连查生词都不会"):侧栏原功能是前端 `visible_vocab` 采集直注 prompt;壳子的服务端等价物=voice-ctx 里跑 `_page_chars_cached→_build_vocab_marks(+jp)`(与 F7 下划线同一套 mastery 判定),lemma 去重限 30 → SP 段"本页『还没掌握』的生词…直接用这个列表答"。实测"这页哪些单词我没掌握"直接列出全部生词。直塞清单现为:页文本/圈画/插图描述/生词四件套(开话+翻页+圈画热同步)。

## S2S 自然语言工具协议(2026-07-10 第六批,已被第七批取代;协议解析/ChatRAGText 机制沿用)

用户想让 S2S 真正控制工具("输出工具调用文本+火山替换功能滤掉不念")。查官方文档:**无 function calling、TTS 侧无输出替换**(端到端模型音频=文本,藏不住标记);正则替换功能存在但在 **ASR 输入侧**。改用"**可念的协议**"实现同等效果:

- **协议**(system_role 约定,relay 解析**模型自己说的话**,不再匹配用户 ASR):找视频→回复必须带「我找找看,关键词是○○」(模型自己从对话+书页提炼主题词,实测提炼出"日本公共卫生 三级预防"这种高质量 query);翻页→「翻到第N页」。550 按 reply_id 攒全文、559 时跑 `_MODEL_KEYWORD`/`_MODEL_GOPAGE`(⚠正则要容引号+空格)→ 命中执行(搜视频走助手工具桥;翻页 client_action `goToPage`)。
- **结果回填 = ChatRAGText(502)**:`{"external_rag": "<json数组字符串[{title,content}]>"}`(≤4K 字)。实测**延迟几十秒灌回也被接受**,`TTSSentenceStart(350).tts_type="external_rag"`,模型消化列表后**用自己的话**总结播报("有尾岛俊之讲日本公共卫生体系的…"),且结果进上下文可追问——治愈"对工具结果失明"。替代旧 ChatTTSText 照稿念。
- **文档细读捡到的**(用户贴了全文):`enable_asr_twopass=true`(二遍识别提准,顺带解锁 `context.hotwords` 热词,书名已默认加入;`regex_correct_table`/`correct_words` 传值即生效无需 twopass);`enable_user_query_exit=true`(说"挂了吧"→TTSEnded `status_code=20000002`→前端播完告别自动挂断);`tts.audio_config.speech_rate`([-50,100],2.0 生效,凭证 `speech_rate` 可调伴读语速);154 UsageResponse 每轮 token 用量;ChatTTSText 必须在 ASREnded 后发;10 分钟无交互服务端断连(45000003)。
- **入口**:侧栏 📞 **短按=agent 语音输入**(屏前主力)/**长按 600ms=S2S 伴读通话**(`window._voiceCallS2S`,带 file/page;离屏场景:自然闲聊+语音控页)。

## 计费(2026-07 查证,火山官方口径)

- 按 **token** 计费(不是按连接时长):输入音频 80元/M tok、输入文本 10元/M tok、输出音频 300元/M tok、输出文本 30元/M tok。
- 折算:**用户当前轮输入音频 ≈6.25 tok/s**;上下文音频/输出音频 ≈25 tok/s。→ 豆包说话 ≈0.45元/分钟(大头),用户说话 ≈0.03元/分钟。
- **计费挂在"轮"上**(官方措辞"当前轮请求输入的音频"):静音保活(keep_alive 持续传静音)不构成对话轮 → 挂着不说话按官方口径基本不产生费用;但未找到明说"静音免费"的条文,可在火山控制台用量页实测验证(挂10分钟对比 token 曲线)。
- 上下文(system prompt/历史)每轮重复计入输入,**命中 cache 按 cached 费率**(低于正常输入)——我们注入的书页文本(~1800字)不翻页时轮轮 cache 命中;**翻页 UpdateConfig 会换 SP → 下一轮 cache 部分失效**,成本略增(一次性)。
- 限流:QPM 60 / TPM 100k。挂断(FinishSession/断 ws)后零消耗;浮层 ✕/📞 都会真挂断。

- `systemctl status voice-rt` / `journalctl -u voice-rt -n 50`;unit 副本 `references/systemd/voice-rt.service`。
- 相关但独立的两条豆包线:①方舟文本模型(ark key,`scripts/doubao_orchestrator.py` MCP 编排器 + voice.py 文本大脑 `voice.brain=doubao` 开关,均待方舟模型开通);②本 S2S 线(已通)。

## 阅读器集成 + 意图控页(2026-07 第二批,已上线)

用户愿景:语音不只聊天,要**听懂要求并控制页面**(说"找视频"→搜好显示在页面)。S2S 不支持 function calling → 在 **relay 上做意图旁路**:

- **阅读器内通话浮层 `rc-voicecall.js`**(顶栏 🎙 按钮,`window._voiceCall` 挂在 21-misc-ai.js;模板 pdf_reader.html 加载 + cache-bust 清单加了 rc-voicecall.js):右下角小窗,字幕两行+视频结果横条+挂断;独立页 voice-call.html 保留(纯聊天)。
- **书页上下文注入**:ws URL 带 `?file=&page=` → relay `_fetch_book_ctx` 经 webapp(Bearer=mcp-webapp-token)拉 page-text + 助手历史 → 动态 system_role(实测豆包能答"你在看《料理师part2》")+ `dialog_context`(严格 user/assistant QA 对,偶数条)。
- **dialog_id 跨通话记忆**:150 事件回的 dialog_id 存 `state/doubao-dialog-id.txt`,下次 StartSession 带上(服务端接续最近 20 轮)。
- **意图旁路**:relay 监听 ASR 最终文本(451 is_interim=false)+ 文本 query → `_VIDEO_INTENT` 正则(⚠间隔要宽:`[^。!?,]{0,20}`,"找**几个讲这一页内容的**视频"隔 9 字)→ 调 `/api/assistant/tool` search_video → ①`client_action` JSON 事件下发浮层渲染视频卡(点开 RC.videoPlayer)②ChatTTSText(500)让豆包播报结果。
- **⚠ query 必须拼页面内容**:意图指令原文("找讲这一页的视频")没有信息量,直接当 query 搜出来全是垃圾;拼 `page_text[:150]` 后 pick_video AI 正确提炼主题(实测:料理师 p3 讲公共卫生 → 搜到「公共卫生与预防医学」「Food safety 101」)。
- 加新意图 = relay 加一个正则 + 一个 `_run_xxx`(调 assistant 工具桥,现成 29 工具)+ 浮层 dispatch 分支。
- **翻页同步(2026-07 第三批)**:StartSession 注入的是快照,翻页后豆包停在旧页(用户实测发现)→ 用 **`UpdateConfig`(201)通话中热更新 system prompt**:21-misc-ai.js 定时器(2s,仅通话开着时)读模块变量 `currentPage` → `RC.voicecall.setPage`(去重)→ ws `{type:"page",page}` → relay 拉新页文本 → UpdateConfig 全量 dialog(bot_name/system_role/speaking_style)→ ConfigUpdated(251) ack;同时更新 `book["page_text"]` 让意图旁路(找视频)也用新页。实测:p3 开话→同步 p11→答"第11页,讲日本卫生统计",正确。

## 侧栏整合 + 记忆管理(2026-07 第四批)

- **入口整合进 AI 侧栏**(用户要求):顶栏 🎙 按钮撤掉;`rc-voicecall.js` 自注入 `#asst-call` 电话按钮到侧栏 composer(`#asst-mic` 语音输入旁,轮询注入直到 pane 出现;通话中绿色呼吸 `.on`)。点击走 `window._voiceCall`(21-misc-ai 提供 FILE_REL/currentPage)。通话条**内嵌**在侧栏输入框上方(`#rc-vc.vc-inline`,position:static);侧栏组件不在时回退右下角浮层。
- **意图执行过程可见**(修「豆包嘴上说完成了但页面没动静」):relay 的 intent 事件带 `query` 原文 → 浮层往 `#asst-thread` 追加气泡:用户侧 `🎙 <指令>` + 助手侧「🔎 正在找相关视频…」→ 结果到达改文案「给你找到了 N 个」+ `window.renderVideos` 把可播放卡插进对话流(rc-video 的 `_hostBubble` 找最后一个 `.asst-msg.asst-a` = 这条状态气泡);empty 时也改文案。⚠ 这些气泡是 ephemeral(不写服务端对话历史),刷新即消失。
- **防幻觉承诺**:S2S 模型会在搜索还没跑完时就嘴快说"已经帮你搜好了"→ `_role_text` 加硬约束:搜索是系统后台执行、要几十秒,**回应时绝不许说已完成**,系统搜完会让它播报(ChatTTSText)那时才确认。prompt 级缓解,非根治。
- **🧹 新话题(记忆清除)**:UpdateConfig 只全量替换 SP(旧页原文被顶掉),但**对话历史不清**(服务端滚动保留最近 20 轮,靠它才有"刚才你说的那个"式追问)。要彻底清:浮层头部 🧹 按钮 → 挂断 → 重连带 `fresh=1` → relay 删 `state/doubao-dialog-id.txt` + StartSession 不带 dialog_id/dialog_context(全新会话,只留当前书页)。实测 fresh 连接后 dialog_id 换新。
- 文档 2.0 另有精细手术:ConversationRetrieve(512)/Truncate(513)/Delete(514) 可按轮删,暂未用。

## 模型预设 + 深度思考模型项(2026-07-10 第八批,v3-⑦)

- **`deep` action**(assistant.py):`_AP_ACTIONS` 加 `"deep"`,默认 `{backend:claude, variant:opus, depth:high}`,label「深度思考(语音通话专用)」。relay `_run_deep_think` 选型改为**先拉 `GET /api/assistant/action-prefs` 的 deep 项**(backend==claude → force_model=variant/force_effort=depth),凭证 `deep_model`/`deep_effort` 只作兜底——语音深度思考的模型从此在设置面板可视化调整。
- **预设(profiles)**:`/api/assistant/pref-profiles`(GET 列表;POST `{op:save|apply|delete, name}`),存 `state/assistant-pref-profiles.json`。save=当前用户 action-prefs 全套快照;apply=整包写回。前端 `renderModelSettings` 顶部加**预设条**(`.ams-profiles` chips):点=一键应用+重渲染面板;**长按 600ms/右键=删**(touchstart 计时,`_held` 标志吞 iOS 长按后补发的 click);末尾「＋存为预设」prompt 起名。
- **删「这页知识点」按钮**(用户裁定):`rcBuildQuickBar` 的 kSend/kLabel 段、rc-assistant native fallback、25-assistant.js legacy 三处全删;调用处改传 `{}`。

## 通话独立于侧栏 + 断线自愈(2026-07-10 第八批,v3-⑧)

排查结论:**侧栏关闭本来就不断通话**——`rc-sidedrawer.close()` 只是 CSS 滑出(`classList.remove('open')`),DOM 不销毁、不碰 teardown。真正"断话"的是**网络波动 / iOS 切后台掐 ws** → 旧代码 `ws.onclose` 一律当"已挂断"。修法(rc-voicecall.js):

- **teardown 摘回调再关**:`ws.onclose=ws.onerror=ws.onmessage=null` 后再 close → 主动挂断(✕/📞/359 告别/🧹 fresh)不触发 onclose;**顺带根治 fresh 重连时旧 ws 迟到的 onclose 误杀新连接**的隐患。于是还挂着回调的 onclose **必然是意外断线** → `_scheduleReconnect()`。
- **自动重连**:指数退避 600ms×2^n(封顶 8s,最多 8 次),重连**不带 fresh** → relay 用存的 dialog_id 接续最近 20 轮记忆,体验连续。`document.hidden` 时不空转烧次数(`_reconnPend` 标志),回前台 visibilitychange 立即连。start() 失败(promise resolve 后 ws 仍 null)继续退避。
- **新连接指纹清零**:start() 开头 `_stateFp=null; _inkFp=''` → __vcSyncNow 下一轮把选中/墨迹/页码重推。修既有 bug:重连/🧹 后指纹残留导致状态永不重推,relay 不知道选中和圈画。
- **Wake Lock**:onopen `navigator.wakeLock.request('screen')`(iOS 16.4+),teardown 释放;切后台系统会自动释放 → 回前台重新拿。
- **回前台恢复**(visibilitychange):`ac.state!=='running'→ac.resume()`(iOS 音频会话被挂起);ws 活着→补 wake lock;ws 没了且非主动挂断→立即重连。
- **iOS 后台保活边界**(系统行为,代码只能兜底):Safari 里 getUserMedia 活跃(麦克风红标)时页面**通常**不被冻结,但锁屏/长时间后台仍可能掐 ws——被掐就走上面的自动重连,回前台几秒内接续记忆恢复通话。真机体验待用户实测反馈。

## 一键开关 + Apple 简约 UI + 对话窗拖高(2026-07-10 第九批,v3-⑨,用户需求)

- **#asst-call 单击 = S2S 开关**:点=直接开通话(_voiceCallS2S,零中间步骤),再点=挂断(teardown(true),重连排队中点=取消重连+挂断)。**长按逻辑删除,agent 模式入口撤掉**(代码保留,`window._voiceCall` 仍可调)——语音输入归旁边的系统听写 #asst-mic,豆包按钮只管 S2S 伴读(与产品反思一致:sauc≈Apple 听写无优势)。
- **通话条 Apple 风重做**(#rc-vc):毛玻璃(`backdrop-filter:blur(24px) saturate(1.5)`+rgba 半透底+细白边)、iOS 系统色(绿 #30d158/蓝 #0a84ff/橙 #ff9f0a)、emoji 全换 **SF 线条 SVG**(↺=新话题、✕=结束)、头部**状态点**(橙呼吸=连接中/绿=通话中,`.vc-dot`+`#rc-vc.on`)、挂断大圆钮撤掉(✕ 或入口按钮再点即挂)。⚠ box 无 transform(iOS backdrop-filter+transform 命中盒坑,见 [[reader-toolbar-icons-svg]] 侧栏教训)。
- **字幕改累积对话流**(用户反馈"看不到对话内容"——旧版覆盖式只显示最后一句):iMessage 风右蓝(你)/左灰(AI)气泡,AI 一轮=一个气泡(550 增量更新同一元素,450 用户开口=上轮定稿 curAEl=null),上限 80 条滚动裁剪。
- **对话区可拖高**:顶部 **Apple sheet 抓手**(36×5 圆条,`.vc-grab` 整条 ns-resize+touch-action:none),pointerdown+setPointerCapture 上拖=变高(窗在输入框上方向上扩展),clamp 56px~60vh,松手存 `localStorage.rcVcSubH` 下次复原。

## 上下文/缓存/消耗优化(2026-07-11 第十批,v3-⑩,用户拍板全做)

计费杠杆:输出音频 300元/M(最大头,豆包说话≈0.45元/分钟)>输入靠 cache 打折;12K 硬限封顶单轮输入。六件套:

- **A. 154 记账**:relay 接 UsageResponse(154,fwd=False 不转发前端)→ `_usage_classify` 宽容解析(官方没给字段清单:嵌套扫数字 token 键,按 input/output×text/audio+cached 归类,DEBUG 打原样观察)→ `_log_usage` 按天累计到 `state/doubao-usage.json`(rounds/各类token/cost_est,费率 in_audio 80/in_text 10/out_audio 300/out_text 30 元/M,cached 单列不计价)。控制面板额度日志 modal 加「🎙 豆包语音通话」块(`/control/api/doubao-cost`,总计/近30天/近7天明细,Gemini 花费块同款)。
- **B. SP 指纹 + 251 ack 确认制**:`_push_sp` 算 `_role_text` md5——`==confirmed` 不推(没变);`==pending 且 <5s` 不推(在途);**251 ConfigUpdated ack 才把 pending 前移为 confirmed**;pending 超 5s 无 ack 视为被豆包丢弃 → 放行重推。450 开口"无条件重推"的丢包兜底语义完整保留,但 SP 没变的开口不再发 UpdateConfig(之前每次开口都全量替换 SP=每轮打掉前缀缓存)。StartSession 后把初始 SP 指纹锚定为 confirmed。
- **C. SP 三层分层**(前缀缓存按最长公共前缀命中,按变化频率排):①稳定前缀=角色+协议+简洁约束+**全量工具目录**(整场不变)②中层=页文本/插图描述/生词(翻页才变)③易变尾部=「——以下是实时页面状态」+笔迹三态/选中/focus/图/任务。**门控改声明式**:原设计"无笔迹连 see_ink 都不给"(目录按状态增删行)会让一画笔目录就变 → ②整层缓存连坐;改为目录恒定、尾部显式"see_ink/read_selection/see_figure 此刻无效"承接同一语义。冒烟验证:画笔/选中变化时 ①+② **字节级不变**。
- **D. 输出瘦身**(300元/M 的大头):`_ACK_TEXT` 全员缩短("好,我找找看"→"我找找");SP 稳定段加"回答默认两三句话说清,用户说详细讲讲才展开"。
- **E. 历史增量限长**(进历史的字轮轮计费):RAG 回填按工具分级 `_RAG_LIMIT`(列表类 search_video 900/see_* 1600-1800/read_page 2200/写操作 600,未列 1400,替代统一 3000);深度思考 510 注入 800→500。
- **F. 长对话摘要护栏**:559 时自然对话轮(非工具 JSON 轮)记 `book["qa_log"]`(u[:24]+a[:36]);攒满 26 轮 → 最旧 12 轮拼接成一条 510 注入("我们更早聊过这些…"),不调外部模型。豆包 12K 硬限已封顶输入费用,这条主要保认知连续(旧轮滚出窗口时脉络已固化);ConversationTruncate(513) 真删除因官方 payload 格式未公开暂不用,拿到格式可升级为"删+摘"真压缩。

前提待实证:豆包 S2S 前缀缓存行为(cache 命中费率官方提过但字段未见)——A 的账本跑几天真实通话后看 cached 列即知 B/C 实际效果。

## iOS 重连卡死修复(2026-07-11,用户真机反馈"切一次后台就一直显示正在重连")

根因:后台掐 ws → 回前台自动重连调 start(),但 **iOS 非用户手势环境里 suspended AudioContext 的 `resume()` 可能永远 pending(不 resolve 不 reject)** → `await ac.resume()` 死等,start 整个卡住,`.then` 永不触发 → 状态永远"重连中…";且每次再切后台回来又 new 一个 AudioContext 卡住(iOS 有 ~4 个上限,泄漏几次后彻底废)。三层修法(rc-voicecall.js):

- **resume 超时竞速**:`Promise.race([ac.resume(), 800ms])` ——suspended 也继续建链路(ws/字幕全通,只是暂时无声),声音由**常驻捕获 pointerdown** 监听恢复(用户碰一下屏幕即 resume;没通话时 ac=null 零开销)。
- **连接世代 `_gen`**:teardown/start 都推进;在飞的旧 start 每个 await 后 `_dead()` 自检,过期就清掉**自己建的局部资源**(myAc/myMic)退出——防 iOS 卡死的旧回合在用户触屏后"复活",跟新回合抢出双连接。
- **重连 watchdog `_tryStart`**:每次尝试 12s 没建成 ws(且世代没被替代、非主动挂断)→ 强制 `_scheduleReconnect` 推进下一轮;覆盖 start 卡死在任何一个 await 上的情况。

## 朗读开关=S2S 语音/文本回复切换 + 官方价格表修正(2026-07-11)

- **「🔊 朗读」开关转岗给 S2S**(agent 入口已撤,开关原本无活消费者):亮=语音朗读(默认,伴读场景要出声);灭=**文本回复模式**——音频前端直接丢(playPcm gate `mode==='s2s'&&!s2sSpeakOn()`),回复文字照常进通话条对话窗(550 字幕增量驱动,不依赖音频)。独立键 `rc-voice-speak-s2s`(默认亮,与 agent 旧键分离);通话中点击即时生效,按灭瞬间 stopPlayback 清播放队列;确认语/深度思考代播同走 gate。
- **⚠ 协议无输出模态开关**(文档全查证:`tts.audio_config` 只有 channel/format/sample_rate/speech_rate/loudness_rate;`input_mod` 只管输入侧 text/audio_file/keep_alive/push_to_talk)→ S2S 输出恒为文本+音频双流、**双流都计费**,灭灯省的是听觉干扰不是钱。想真省输出费的候选:speech_rate 拉快(音频 token≈25/s 按时长折算,语速快时长短;未实测,可用 154 账本 A/B 验证)。
- **154 官方字段清单**(文档 500 行拿到):input_text_tokens/input_audio_tokens/**cached_text_tokens/cached_audio_tokens**/output_text_tokens/output_audio_tokens → `_usage_classify` 改精确解析(宽容子串扫降为兜底)。
- **官方价格表修正**(用户截图核对):输入-文本 10 / 输入-音频 80 / **输入-文本cached 5 / 输入-音频cached 5** / **输出-文本 80(旧文档误记 30)** / 输出-音频 300(元/M)。`_usage_cost` 新公式:cached 视为 input 子集,未命中按全价+命中按 5 元。首日账本已迁移重算(¥2.17→¥2.14;cached 14.6k 按 5 元仅 ¥0.07——**缓存优化把输入文本费砍掉近半**)。
