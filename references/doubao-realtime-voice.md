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

## 确认语 PCM 缓存回放(2026-07-11 v3-⑪,用户点子:"固定确认语存下来复用,别每次让 AI 生成")

确认语是固定集合(_ACK_TEXT 十来句),旧流程每次工具调用都发 500 让豆包重新合成 → 每次都付输出音频费(300元/M)+等 1.6s 排轮。改成**首次合成时录下来,之后 relay 直接回放**:

- `_say_ack` 先查 `state/doubao-ack-pcm/<md5(句子)>.pcm`:命中 → relay 把 PCM 按 ~100ms 分片直发前端(与豆包帧同格式 pcm_s16le 24k,前端 playPcm 队列照播)——**零合成费+零延迟**,豆包连 500 都不收;未命中 → 发 500 + 设 `book["ack_rec"]` 进入录音流程。
- **录音窗口**:350(tts_type=chat_tts_text)置 `on=True` 开录 → 音频帧下发同时 tee 进 buf(cap 500KB)→ 359 存盘(<0.1s 不存);**450 用户开口打断 → 丢弃残缺录音**(防缓存半句)。改 _ACK_TEXT 文案=换 md5=自动重录,无需清缓存。
- deep_think 正文流式代播不受影响(ack 缓存回放时不发 500,正文 350 到来时 ack_rec 已空,不会误录)。
- 工具轮成本残余:JSON 命令句本身的 TTS 音频(被丢但生成即计费,~几秒)协议上避不开;确认语部分归零。

## 文本回复模式=切 agent 引擎(2026-07-11 v3-⑫,回应用户查证"同传 S2T")

用户问官方客服得到"可用同声传译大模型的 S2T 模式"——**不适用**:同传是翻译器(输入语音→输出**翻译文本**),无对话大脑/工具/上下文,替代不了对话模型;端到端实时语音大模型官方确认无纯文本输出模式。正解=文本模式根本不该用 S2S 烧钱,**切回自家 agent 链路**(v3 之前完整做过、代码全在):耳=豆包 sauc 流式 ASR(时长版计费,便宜)+ 大脑=侧栏助手(Claude 全 29 工具,已有额度)+ 回复=纯文字进侧栏对话流——**豆包输出音频费整个归零**,大脑还更强。

- **「🔊 朗读」开关升级为引擎选择**:亮=S2S 端到端语音(豆包开口,现在这样);灭=agent 文本(你说话、AI 文字答)。开始通话时按开关状态选链路;**通话中切=自动挂断以新引擎重拨**(taPlaceholder 提示"切换到语音对话…/文字回复…")。
- **两引擎记忆各自独立**(S2S=dialog_id 服务端 20 轮;agent=助手服务端对话历史)——切换=换脑子,不互通,重拨提示即用户预期管理。
- `__asstVoiceOn` 收紧为 `mode==='agent'&&ws&&speakOn()`:文本模式不带 voice:1 标志(后端不用"适合朗读"风格,答题排版正常 markdown)。
- S2S 内的 playPcm 丢音频 gate 保留(切换瞬间保护)。relay 零改动(handle_agent 分支现成,2026-07-10 冒烟全通)。

## 三按钮职责重排 + 朗读专用通道(2026-07-11 v3-⑬,用户设计)

按钮分工定稿(替代 ⑫ 的"朗读开关=引擎选择"):
- **#asst-mic**:单击=系统听写(原功能,rc-assistant 的 handler 一字不动);**长按 600ms=豆包 ASR 连续听**(agent 模式:说话→sauc 转写→自动问助手,文字回答;朗读亮则也念)。长按后**捕获阶段吞 click**(stopImmediatePropagation,不触发听写);再长按=挂断。ASR 通话中 mic **紫色呼吸 `.asr`**(#bf5af2,区分系统听写的蓝 .on)。S2S 开着时长按=先挂再开 ASR。
- **#asst-call**:S2S 专属(点=开/挂,绿呼吸)。
- **「🔊 朗读」= 统一的"要不要出声",双语境双键**:S2S 通话中=播/不播豆包音频(`rc-voice-speak-s2s` 默认亮;灭=丢音频看对话窗字幕,⚠音频仍计费,title 已注明);其余场景(ASR 通话/纯打字提问)=回答的 **T2S 流式朗读**(旧键 `rc-voice-speak` 默认灭)。语境切换(onopen/teardown)时按钮亮灭自动刷新 `_refreshSpeakTg`。播报中开关呼吸 `.speaking`。
- **朗读专用通道 `?mode=tts`**(relay `handle_tts_only`):没开任何通话时点亮朗读→回答也能念——**不开麦、不连 ASR**,只有双向流式 TTS;speak/speak_done/cancel 协议与 agent 同款。relay 侧 TTS 状态机抽成 **`_tts_channel(bws,key,speaker)` 工厂**(agent/tts-only 共用,pyflakes 验证);前端独立小状态机 `_tts{ws,ac,playT,playing}`(独立 AudioContext 与通话互不干扰),**点亮开关(手势内)预热** `_ttsEnsure`(iOS AudioContext 必须手势启动);新一轮回答开始自动 bargeIn 打断残播。
- **关于用户查证的"同传 S2T"**(二次确认):同声传译大模型的 S2T 输出的是**目标语种的翻译文本**(端到端识别+理解+翻译),不是对话回答——没有 QA/工具/人设能力,仍不能替代对话模型。"通话中要文本"由朗读开关(S2S 灭=字幕)与 ASR 模式(真省钱)覆盖。

## 增量上下文架构(2026-07-11 v3-⑭,用户 transformer 缓存洞察)

用户查了 transformer/KV-cache 知识后指出要害:**SP 在序列最前,改 SP 尾部一个字=其后整个对话历史的前缀缓存全失效**——⑩ 的"SP 内分层"只救了 SP 自身,救不了历史;每次画笔/选中变化的 UpdateConfig 都在打掉全部历史缓存。正解=**别改过去,只追加增量**:

- **状态整段撤出 SP**:笔迹三态/选中/focus/图/任务段删除,SP 只剩 角色+协议+目录(整场不变)+页文本/插图/生词(翻页才变)→ **SP 唯一变化源=翻页**。SP 留稳定说明:"实时状态以『(系统状态更新:…)』消息出现在对话里,以最新一条为准;一条都没有=页面什么都没有"。
- **状态=510 追加事件**:`_state_event_text(book)` 合并快照(选中/焦点/图/笔迹三态,~170 字),防抖平息后 `_inject_state` 经 ConversationCreate 追加(user+assistant"收到,以这条最新状态为准")——前缀不变,历史缓存零损失。指纹去重(**fp 掺 ink_ver**:防"看过v2→又画v3"字面相同漏注);开话时已有 sidecar 笔迹/选中 → 立即注初始事件。原"笔迹记忆注入"被吸收(三态语义全在事件文本里)。"历史压 SP"定律在此是助力:状态写进最近历史比 SP 快照更能压住旧对话的过时说法。
- **任务护栏取舍**:「⏳正在执行X别重复调」从 SP 撤掉且不注事件(每次调用+2条历史不值)——护栏改依赖:①确认语在历史里 ②工具缓存(真重调也秒回复用)③RAG 到达即结束语义。起止的 push_sp 调用保留(SP 指纹不变=no-op,零成本)。
- 翻页仍 `_push_sp`(SP 换页文本,一次性缓存重建不可免)+顺带状态事件(换页状态清零要告知)。450 的 SP 兜底重推保留(指纹制,平时 no-op)。
- 冒烟:状态全组合下 SP **字节级一致**;三态/选中/图事件文本全通。

## 同传 S2T 结论修正(2026-07-11,用户查证推动)

细读同传 2.0 文档(6561/1756902)发现**双字幕流**:`SourceSubtitle*(650/651/652)=原文转写`、`TranslationSubtitle*=译文`。→ **S2T 设 zh→en 但只消费原文字幕 = 带语义理解的强中文 ASR**(语义纠错/多说话人 spk_chg/热词 hot_words_list/替换词/中英混杂),正治 agent 模式"sauc 无上下文识别弱"的痛点;计费=推理服务 输入80/输出文本80/输出音频300 元/M(S2T 无音频输出)。它仍不是对话模型(理解服务于翻译,无 QA/工具),"当大脑"不成立、**"当耳朵"成立**。待办:控制台开通同传服务 → relay handle_agent 加 `agent_asr:"ast"` 引擎选项(取 651 原文,协议为 event 帧族,X-Api-Resource-Id 待从文档鉴权段确认)。

## S2S 语音设置面板(2026-07-11 v3-⑮,用户提议"设置里该有音色人设等")

从官方文档梳理 O2.0 可配项全部接入设置面板(「⚙ AI 模型设置」底部「— 🎙 语音通话(豆包 S2S)—」区):

- **音色 7 选 1**(StartSession `tts.speaker`):vv 活泼女(默认,唯一支持方言)/xiaohe 台湾腔/yunzhou 沉稳男/xiaotian 磁性男/Tim/Dacey/Stokie 美音。**方言**(`tts.extra.explicit_dialect`:东北/四川/陕西,仅 vv;⚠ tts.extra 置空报 42000020 → 没方言不带 extra 键)。**语速/音量**滑条(`audio_config.speech_rate/loudness_rate` [-50,100])。**人设三件套**(bot_name/speaking_style/system_role 自定义前缀——伴读工具协议自动拼在人设后面)。**唱歌**(`dialog.extra.enable_music`,仅 1.2.1.1)。
- **存储** = `/api/assistant/voice-config`(assistant.py):读写凭证文件 `~/.config/doubao-voice.json` 的**非密钥白名单字段**(api_key 绝不经此暴露);值为 ""/null/False 即删字段回默认(⚠ Python `0 in ("",None,False)` 为 True → 0 也会删,恰好 0=默认值语义一致)。relay 每次 `_creds()` 现读 → 写完下次开话即生效。
- **通话中热切**:前端 change→POST→`RC.voicecall.pushCfg()` 发 `{type:"cfg"}` → relay `_push_sp`(UpdateConfig 现带 `tts` 字段,`_tts_cfg()` 与 StartSession 共用;**指纹掺 tts json**——只有真改了才发,兼容 ⑭ 的"SP 只随翻页变")。音色/语速立即生效;人设/风格影响 SP 下次开话生效(通话中改也会触发一次 UpdateConfig 换 SP,低频可接受)。
- **SC2.0(2.2.0.0)不接**:角色扮演线用 `character_manifest`+克隆音色但**不支持 system_role** → 工具协议/页文本/状态注入全废;人物扮演在 O2.0 内用 人设+风格+音色 实现(面板 tdef 已注明)。
- 文档另捡:`enable_conversation_truncate`(513 截断的前置开关,未来真·历史压缩要先开它)、26.02.26 更新"基于客户端实际播报进度对齐上下文,仅向模型暴露已播放内容"(打断场景的上下文精度,relay 现未用)。

### 朗读音色可设置(v3-⑮ 补,2026-07-11)

- 设置面板语音区加「朗读音色」下拉(12 选 1,官方音色列表核对):爽快思思(默认)/温暖阿虎/少年梓辛(三个中英双语)/渊博小叔/解说小明/深夜播客/亲切女声/邻家女孩/开朗姐姐/高冷御姐/湾湾小何(台湾腔)/Lauren(纯英语)。字段 `tts_speaker`(voice-config 白名单)。
- **⚠ 音色系列不通用**:朗读走双向流式 TTS(10029),只认 `*_moon_bigtts` 系;S2S 通话的 `*_jupiter_bigtts` 系传过去报 55000000(反之亦然)——所以是两个独立下拉。
- **生效机制零重连**:`_tts_channel._ensure` 每次建 session 时**现读凭证**(一轮回答=一个 session,152 后重建)→ 改完设置**下一句回答**即换声,agent 通话中/tts-only 通道都不用重连。

### 通话/朗读语速分离(v3-⑮ 补 2)

- 面板滑条改三条:「通话语速(S2S)」`speech_rate`/「通话音量(S2S)」`loudness_rate`/**「朗读语速」`tts_speech_rate`**(白名单新字段)。朗读语速由 `_tts_channel._ensure` 每 session 现读注入 `req_params.audio_params.speech_rate` → 改完下一句生效。
- **实验实证**(bidi TTS 直连冒烟):speech_rate=0 → 3.24s/155KB;=80 → 1.78s/85KB——bidi 认此参数,且**音频量随语速等比减 ~45%**;同理适用 S2S 通话(300元/M 的输出音频按时长折算,语速调快=真省钱)。

## 工具调用详情卡进侧栏对话流(2026-07-11 v3-⑯,用户需求"要感叹号那种,点开看每一步")

v3-⑤ 的固定状态按钮(转圈/✓)保留做**进行中**状态;**完成后**在侧栏对话流(#asst-thread)追加一条**可折叠详情卡** `.vc-tcard`(折叠=一行「✓ 找视频 · 2.3s ▸」,点头部展开):
- 展开内容 = 全过程:**指令(S2S 原话 JSON)** / **携带上下文**(第N页·墨迹N笔·选中N字) / **参数** / **喂回给它播报的结果**(RAG 全文 ≤1600)。
- 数据源 = relay tool_status done/error 事件扩容:`cmd`(S2S 原始指令 ≤500)+ `ctx_brief{page,ink,sel}` + `rag`(喂回内容);缓存命中路径带 cmd+rag(cached 标记);深度思考带 `cmd="deep_think(模型/深度): 问题"` + rag=答案全文。done 的 tool_status 发送挪到 RAG 构造之后(原在 content 计算前拿不到)。
- 卡片 ephemeral(不写服务端对话历史,刷新即无——与旧 intent 气泡同性质);持久化待用户提。

### 进行中按钮=可中止的瞬态指示(v3-⑯b,用户设计)

- #vc-tool-btn 职责收窄:调用开始**出现并转圈**(title"点击中止")→ **点击=中止**(发 `{type:"tool_abort"}`)→ done/error/aborted **自动消失**。常驻 ✓/⚠ 与详情弹层(toolLog/renderToolPop/#vc-tool-pop)整体退役——查记录归对话流详情卡。
- **中止实现**:relay 存 `book["tool_task"]`(两处 create_task),tool_abort → `task.cancel()`(掐掉 relay 侧等待,webapp 已发出的执行不追,finally 摘牌照跑)+ `deep_abort=True`(深度思考走自己的打断路径)+ external_rag 注入"用户手动取消了,不会再有结果,简短确认即可"(豆包知情且自然回应一句)+ tool_status aborted → 前端按钮消失+对话流一条「⊘ 已中止」小卡。

## 学习时间线一期(2026-07-11 v3-⑰):落盘+焦点聚合+recall_study

**背景(用户洞察链)**:S2S 12K 硬限→"学了什么"这类长记忆需求它干不了;且**模型无法感知自己上下文之外的存在**(丢掉的轮次没有"缺失感",会拿残缺记忆自信瞎答)——解法不是模型自省而是**路由规则**(工具协议天生是路由层);顺带 S2S 只出一条 ~30 token JSON 比自己现编便宜一个量级。

- **A. 全量落盘** `state/voice-log/<日>.jsonl`(`_vlog`):对话 q(451终稿/文本输入)/a(559 自然轮全文≤2000)+ page/sel/ink/tool 事件,全带 ts+book+page。**写原始事实、不做任何过滤**(用户设计:焦点规则会进化,写时过滤=信息永久丢失;事件溯源哲学)。
- **B. 焦点聚合器** `_study_digest(span)`(读取时执行,规则可重算):按(书,页)切段,段时长=到下一段;**①段<5s→整段丢,连段内选中/查词都算误触**(用户规则);②丢后相邻同页合并(A→B→A 抖动自然消失——不用页中心几何判定,复用现有 currentPage 信号+时间防抖足够,连续滚动模式下"页中心"有两页交界无主的坑);③纯停留段≥60s 留一行"阅读"、不足丢。**查词日志直接合并**(vocab-lookups.jsonl 本来就有 ts+page+pdf,通话外查词也进时间线——原定二期,白捡)。输出=按页叙事「HH:MM《书》pN(约M分):查词:…;选中:…;问:…→答:…」,限 6000 字保最近。
- **C. recall_study 虚拟工具**:目录行写死路由规则("你的记忆只有最近20轮,更早的已被裁掉且**你感知不到丢了什么**——回顾类一律用本工具,凭印象答=编造");relay 拦截(deep_think 同位)→ digest 喂大模型(`_run_deep_think` 参数化 tool_name/tool_label 复用流式代播骨架,prompt="只依据记录,别编造,按主题归纳别流水账")→ 边生成边播;记录为空时 RAG 如实告知。确认语"我翻翻记录。"。
- 冒烟:合成事件流验证 保留/误触丢弃(含段内操作)/抖动折叠合并 全通过,输出为干净的学习纪要体。
- **二期待做**:高亮 sidecar 合并(需补时间戳)、`anki_from_study`(把时间线喂 Claude 按制卡规则出卡)、跨天 span 扩展。

### recall 改 agentic 按需拉页(v3-⑰b,用户设计:"全部注入太多,由 Claude 决定是否拉取")

digest 只含页码+操作摘要、无页面原文——全量注入页文本(每页~2000字×多页)不可行。解法=**摘要给线索,原文 agentic 拉取**:侧栏助手 chat 管线本就带完整工具循环(read_page 支持指定印刷页码),recall 复用 _run_deep_think 时把硬编码的"少用工具"前缀**参数化**(`preamble`,deep 默认不变),recall 传"(语音回顾:可用工具查证)"放开;prompt 明示"记录里没有原文,需要时用 read_page 只拉学习重点那几页,别整本拉;read_page 只能读当前书,别的书依据摘要说"。Claude 在工具循环里自己决定拉哪几页——标准 agentic RAG 形态。

### 记忆起点(recall 界限,v3-⑰c,用户设计)

- 快捷栏「⏱ 记忆起点」chip:点开=datetime-local(日+时粒度)+「设为现在」+「不限」;字段 `recall_cutoff`(epoch 秒,voice-config 白名单,0/空=不限);chip 文字实时显示当前起点(如「⏱ 7/11 09时起」)。`_study_digest` 读取时过滤 ts≥cutoff——**档案不删,只是回顾的视界**(原始日志永远都在,改起点即恢复)。
- **清空联动**:点侧栏「🗑 清空」→ 独立捕获旁听(不限通话状态)延迟 50ms confirm「把回顾记忆起点也设为现在吗?(之前的记录不再被提起;档案本身保留)」——对话记忆与学习档案分层,斩断过去由用户显式确认,不自动。

### 端到端实测(v3-⑰,2026-07-11,真实数据)

- **段时长封顶修正**:纯查词段(事件稀疏)的"到下一段"时长会把整夜算成停留(实测 p10 段 580 分)→ 封顶"最后事件+5 分"(00:12 查完词人走了 → 约5分)。
- **真实跑通**:今天 9 条真实查词(无通话记录)→ digest 三段干净纪要 → 按 recall 同款 prompt 调 /api/assistant/chat:Claude **自己调了 read_page 拉 p8 原文**(4 个查词都在 p8=学习重点页,正是"只拉重点页"设计行为),回答把查词逐个对回原文语境("『促し』就是文中『感染症予防を促したり』这句"),对没拉的 p10 诚实说"估计是…"并主动问要不要拉——agentic 按需拉取形态完全符合预期。
- SSE 解析注意:chat 的 answer 事件 data 是 **JSON 编码的字符串**(非对象),分帧靠空行;照抄 _run_deep_think 的解析。

## Codex 第三后端(2026-07-11,用户提议:Pi 已登录 codex CLI)

助手模型面板 backend 三选:claude / gemini / **codex**(GPT,走 Pi 上已登录的 ChatGPT 订阅——**额度与 Claude/Gemini 完全独立**,等于白捡一路配额)。
- **执行器 `_codex_text`**:`codex exec --skip-git-repo-check -m <型号> -c model_reasoning_effort="<档>" -c sandbox_mode="read-only" -o <tmpfile> [-i 图…] <prompt>`,cwd=/tmp(只当纯文本/看图模型用,不让它当 agent 乱跑);`-o` 文件拿最终消息,输出干净。实测:短答 ~5.6s、explain 全链 ~19s(与 Claude CLI 量级相当;codex exec **无流式接口**)。
- **型号**(实测):gpt-5.5-codex(默认)/gpt-5.5 有效,-mini 无效;`_variant_ok` 宽松收 gpt-*。**深度**=low/medium/high/xhigh(映射 model_reasoning_effort)。
- **接入面**:_deep_ask/_vision_describe(图落盘 -i)/reader_ask/reader_stream(无流式→一次性整段 yield,失败落 gemini→claude)——即 summarize/vision/explain/translate/dict/grammar/pick_video/deep 全部单轮动作可选 codex;兜底链 codex→gemini→claude。
- **orchestrator 不接**(编排工具循环是 claude/gemini 的交互式多轮实现):预设选了 codex → chat 两处入口守卫降级出厂默认。语音 deep 面板项选 codex 同理暂不生效(relay force_model 仅 claude)。
- 前端零逻辑改动(catalog 数据驱动),仅 _BACKEND_LABEL 加 'Codex(GPT)' ×2。

## 朗读升级 2.0:语气指令+停顿控制(2026-07-11 v3-⑱,用户需求"升 2.0+提示词加控制符")

**实测定谳(ASR 回转写判别法)**:标记系统的真实支持矩阵——
| 通道×音色 | SSML | [#语音指令] | context_texts |
|---|---|---|---|
| 双向 bidi(1.0/2.0 资源都) | ❌剥除 | ❌照念 | 无此字段 |
| 单向 uni + 1.0(moon) | ✅ break 生效 | — | ❌仅2.0 |
| 单向 uni + 2.0(uranus) | ❌剥除 | — | ✅**生效且不被念出** |

即官方设计:1.0=SSML 机械标记;2.0=LLM 式 TTS(**自然语言语气指令 `additions.context_texts`**,如"说慢一点/用痛心的语气",不计费不进文本)+标点智能停顿。"2.0+SSML"官方就不兼得——用户目标(停顿+语气)在 2.0 上由 context_texts(语气)+标点/省略号(停顿,实测有效)达成,比 SSML 更强。

- **relay `_tts_channel` 双引擎**(speak 时现读凭证自动选):`*_uranus_bigtts` → **单向流式 `TTS_UNI_WSS` + `seed-tts-2.0`**——每句一请求经 asyncio 队列 worker **串行**(保音频顺序),同一通道同一 **`section_id`**(服务端保持对话式韵律,治逐句请求的韵律断裂);`context_texts`=面板「朗读语气」(`tts_instruction` 白名单字段);speech_rate 同名直传。moon 系 → 原双向引擎不动(回退安全)。cancel=换代+清队+close 在流请求。单向请求帧 flags=0000(无 event 号,`_uni_req_frame`)。
- **停顿=AI 标点**:三处朗读 prompt(assistant voice_mode 段/deep preamble/recall preamble)加"用标点控制节奏:短句逗号断句、明显停顿用省略号……、别用其它标记符号"——**不让 AI 写 SSML/标记**(2.0 不支持+会污染侧栏显示;程序+标点承担)。
- 面板:朗读音色下拉加 2.0 组(vv 2.0 推荐/爽快思思/渊博小叔/深夜播客/温柔小雅/儒雅青年/亲切女声)+「朗读语气」输入框(仅 2.0 生效)。默认已切 vv_uranus+温柔指令。
- 单向接口另有存货未接:`emotion/emotion_scale`(多情感音色)、`silence_duration`(句尾静音)、`disable_markdown_filter=false` 原生过滤 md、`cache_config`(相同文本 1h 缓存)、pitch。

### AI 动态语气(v3-⑱b,用户设计:"语气由回答的 agent 定,流式要求开头给出")

- **机制**:voice_mode prompt 要求 AI 在回答**最开头**输出 `[语气:XX]` 标签(2~6 字情绪描述,普通内容用"平静")——正好赶在第一句合成前确定情绪(流式时序约束)。前端 `stripMoodTag`(rc-assistant,挂 RC.assistant 导出):**四处渲染/消费点全剥**(流式增量渲染/收尾最终渲染/历史回放/两处 tap),**流式撕裂保护**(首块只到"[语气:开"没闭合 → hold 显示空等下一增量);mood 经 tap 第三参数 → rc-voicecall `vt.mood`(新一轮 reset 清)→ speak 消息 `mood` 字段 → relay 两处 speak 分支 → `_tts_channel.speak(text, mood)` → uni 引擎 **context_texts 优先级:AI mood(`用{mood}的语气说`)> 面板 tts_instruction 兜底**。
- bidi(1.0 音色)引擎忽略 mood;S2S 通话 tap 天然不触发(语气归豆包人设)。面板语气框语义改"默认/兜底"。
- 冒烟:mood 贯穿真实合成链;JS 单元=完整标记剥离/撕裂 hold/无标记不误伤 全过。

### 语气转折 + iOS 耳机路由修复(v3-⑱c,2026-07-11)

- **句中语气转折**(用户设计):`[语气:XX]` 标签不限于开头——AI 在**情绪转折句前**插新标签,其后句子全按新语气,直到下一个标签(prompt 已加转折规则+标签内禁标点)。实现:tap 改收**原始文本**(含标签),`_speakSeg` 在句片内解析标签流(标签前残句按旧 mood speak → 切 vt.mood → 继续),单向 2.0 引擎每句一请求天然承接逐句变奏;渲染侧 `stripMoodTag` 升级**全局剥**+尾部撕裂截;done flush 时剥未闭合残段。单元:多标签分段/转折切换/撕裂全过。
- **iOS 耳机路由**(用户实测:听写+朗读时戴耳机却走扬声器):页面用过麦克风后 WebKit 音频会话粘在 `play-and-record` 类别——**该类别默认强制扬声器**(蓝牙耳机被无视)。修复三件套(Safari 17+ Audio Session API):①默认+朗读 ensure 时 `navigator.audioSession.type='playback'`(纯播放=耳机优先);②开麦(getUserMedia 前)显式 `'play-and-record'`;③teardown 切回 `'playback'` + **重建朗读 AudioContext**(通话期间建的 ac 路由粘扬声器,close+null 下次拿干净会话)。

### 朗读 prompt 分层 + 结巴修复(v3-⑲,2026-07-11)

- **prompt 三态分层**(用户要求:朗读亮=更简短口语化;朗读灭=prompt 必须零口语/零语气标签):`/chat` 的 `body.voice` 三值——`1`=前端朗读点亮(口语段+语气标签段)/`"s2s"`=relay 深度思考代播(**只给口语段**,bidi 引擎不吃标签,给了会被念出)/缺省=文字模式(纯净 prompt)。口语段加强:"像当面聊天,默认两三句话说完,别铺开别客套别复述问题,内容多只讲最重要的一点末尾问要不要展开"。
- **历史落库剥标签**(隐蔽泄漏根源):`_convo_append` 存的是模型原始输出(含 `[语气:XX]`),关掉朗读后模型看到自己历史带标签会**模仿续写**——落库前剥,历史干净,非朗读轮次真正零污染。relay `_speech_clean` 同步补剥(含未闭合撕裂),bidi 代播链路防念出。
- **朗读结巴根因**(用户报告"念几个字→停顿→从头重念"):claude 编排管线的 answer 事件=**轮内全量、跨工具轮替换**(每个工具轮后新轮文本从头开始)→ 前端 tap 旧版按 `full.length < vt.sent` 判"新回答"→ bargeIn 打断刚念的开场白+从头念新轮。修:tap 改**前缀判定**(`full.startsWith(vt.pref)`),轮次替换=念完上轮残句+从新文本头接着念、**不打断**;真正的 bargeIn 只在新提问(sendToAssistant,连带清 pref/mood)。
- **gemini 编排管线 answer 改全量**:原流式发**增量片段**(`disp[_emit_len:]`)而前端/relay 都按全量替换消费——屏幕只剩最新一小段+朗读反复重念的既有语义错位,统一成与 claude 管线相同的轮内全量。relay `_run_deep_think` 解析加轮次重置(`len(answer)<sent_len → sent_len=0`)。
- 单元:轮1残句"我来看看这道题"念完→轮2顺接→轮内语气转折正常→全程零 bargeIn。

### 朗读字幕(v3-⑳,用户设计,2026-07-11)

- **需求原话**:侧栏没开时朗读显示字幕(当前句清晰+上一句半透明);日语/错字念不出所以然时能看见;其他查询/agent 工作时兼作状态显示;只能通过设置开关;不挡后方触控。
- **同步机制(核心)**:relay 在**每句音频前**向 bws 发 `{"event":"tts_seg","payload":{"text":…}}`——uni 2.0 引擎 worker 串行合成,帧发在 `_uni_synth_one` 开头正好紧贴该句首个音频块;前端收帧入队+置 `bind` 标记,下一个音频 chunk 调度时(`_ttsPlay`/`playPcm`)取队首句、`setTimeout` 到该 chunk 的 `playT` 开播时刻亮字幕 → **字幕与声音精确同步**。bidi/moon 帧发在 speak 分支(音频不分句),退化为略超前。
- **前端**(`rc-voicecall.js` `_cap` 模块):`#vc-cap` fixed 底部居中(bottom 76px+safe-area),`pointer-events:none` 全链穿透;Apple TV 字幕风(深毛玻璃胶囊 rgba(28,28,30,.6)+blur);三行=上一句(.45 透明小字)/当前句/状态行。显示 gate=`capOn()`(localStorage `rc-voice-sub`,默认开)+语音活跃(朗读亮或通话中)+**侧栏关着**(`RC.sidedrawer.isOpen()` 开着有对话流不重复)。播完 4s 淡出(还在播/状态行亮着则续等);bargeIn/挂断 `capClear`(清句队列+定时器)。
- **状态显示**:rc-assistant `tool`/`tool-done` 事件 + relay `tool_status` → `window.__vcCapStatus('⚙︎ 查词典…')`,工具跑完清除。
- **开关唯一入口**:语音设置卡「朗读字幕」checkbox(设备级 localStorage,不进服务端凭证,不跟 data-k 保存链路混)。

### 字幕二期:S2S 接入 + 等待指示 + 用户句 + audioSession 修复(v3-㉑,2026-07-11)

- **S2S 全聋事故(用户报告"说什么都没反应")**:⑱c 在页面加载时全局 `navigator.audioSession.type='playback'`——WebKit 该类别**静音麦克风采集**(getUserMedia 拿到无声流,豆包连 450 都不发;relay 日志实锤:251/567 ack 全正常、零语音活动)。修:撤全局声明(playback 只在 _ttsEnsure(gate `!ws && !_connecting`)和 teardown 后声明);start() 开头先 `await _ttsShutdown()`(等 playback ac close 落地)再声明 `play-and-record`、再建 AudioContext。**⚠ 铁律:audioSession 类别必须在音频会话激活前声明,活跃中改类别 iOS 不可靠;'playback' 有静音麦克风副作用,任何开麦链路的窗口期都不能被它插队**(_connecting 标志挡流式 tap 的 _ttsEnsure)。
- **等待指示**:ASR/S2S 通话空闲时字幕位置常驻小胶囊(mic 线条 SVG+三点跳动 vcCapDot);说话/回答时让位,_capMaybeHide 淡出后 `if(ws) capWait(true)` 回位;capClear(打断)也立即回位;agent_ready/150 触发;侧栏开着被 gate 吞的场景由常驻 pointerdown 350ms 兜底补亮。
- **用户句上屏**:asr interim → `capUser`(当前句是用户句则原地更新,否则 capShow(text,'u'));utterance 定稿 capUser 在 sendToAssistant **之后**(send 内 bargeIn→capClear 会清)+`_capMaybeHide(10000)` 兜底(朗读灭+纯文本回答无任何后续字幕事件,10s 淡出回等待,否则用户句永久钉死)。样式 `.vc-cap-u`=iMessage 蓝。
- **S2S 字幕**:451(interim 也上屏)→capUser;550→`capStream('a',curAText)`(全量累积切句:尾句进 cur、倒数第二句进 prev;S2S 音频不分句,文本驱动略超前);359 非挂断→_capMaybeHide;`_capPlace()` 在 rc-vc 浮层可见时把字幕抬到浮层顶部上方(iPhone 浮层近全宽,不避让被盖)。
- **S2S"搜资料"真伪核查**(用户问):voice-log 实锤它真调了工具但是 **search_all_books(书库搜索,非联网)**,且第一次 args 空 `{}` 失败(⚠卡)、重试成功、第三次命中缓存(三张卡的由来);播报的"总务省2026年1.2494亿"工具结果里没有=**模型记忆披着工具外衣**。修:SP 加"**你没有联网/网页搜索能力**,search_all_books 只搜书库;要网上实时信息就如实说没法联网,凭记忆答必须声明『记忆数据可能过时』";目录行加"query 必填";空 query 错误改指导性(带完整 JSON 示例);缓存命中卡 label 用缓存里存的中文 label;_vlog tool 记录加 args+brief(事后可查播报依据)。
- **对抗验证修复批**(3 verifier,8 findings 全修):H1=_connecting 挡建立窗口的 playback 插队;H2=utterance 后 10s 兜底 hide;H3=`_stripTornFU` 截尾部撕裂 `[[FOLLOWUP]]` 前缀(k≥2 不误伤正文单括号;否则标记补全时 _raw 变短→tap 误判新轮→整答重念+念出"FOLLO");M1=start await _ttsShutdown 返回的 close promise;M2=常驻 pointerdown 同时 resume _tts.ac(通话失败后非手势重建的 suspended 朗读 ac 只有触屏能救);M3=capClear 通话中回 wait+pointerdown 兜底;L1=_cap.gen 世代防打断后 straggler 音频块闪回旧句;L2=teardown 改调 _ttsShutdown(顺带 close 悬空 _tts.ws)。

### 音色锁定 + 顶栏语音入口 + 长按反馈 + 听写自动发送 + ASR 2.0(v3-㉒,2026-07-11)

- **朗读音色漂移**(用户报告"有时完全变了一个人"):2.0 是 LLM 式 TTS,念到引语/对话内容会自己"演绎"换声线。官方**没有**硬开关(additions 全量字段查证:无 disable_emotion 之外的一致性参数;`context_texts` 官方定位=对话式合成辅助指令)→ 修:`_uni_synth_one` 每句**恒带**音色锁定指令("始终保持同一个说话人的声音和音色,不要模仿内容里的角色变声"+逗号拼语气指令)。备用杠杆(未动):seed-tts-2.0-**standard** vs expressive(表现力版演绎强,standard 最接近"关演绎")、emotion+低 emotion_scale。
- **顶栏语音入口**(用户设计):侧栏收起时阅读器顶栏出 mic+电话(`injectTopbarBtns`,锚 #fs-toggle 前,PDF/EPUB 模板都有);**逻辑与侧栏 100% 一致**——实现=远程遥控+状态镜像:单击转发 click 给 #asst-mic/#asst-call(原 handler),长按用共享 `_bindLongPress`+`_micLongAction`;MutationObserver 镜像侧栏按钮的 on/asr/speaking 类(顶栏只动颜色+呼吸);侧栏开→隐藏(observer #ep-side class)。
- **长按到点反馈**(用户:"不知道需要按多久"):`_lpPop`=600ms 到点瞬间弹一下(scale 1.28)+变紫(.vc-lp-pop),侧栏+顶栏共用 `_bindLongPress`。
- **Apple 听写自动发送**(用户:与豆包 ASR 同逻辑):onresult 有定稿且无 interim → 0.9s 静默自动 `send()`(streaming 时 1.2s 重试不丢话);发送后识别段重起继续听(连续对话);手动编辑输入框暂停计时;听写转写经 `__vcCapUser` 上字幕(`_cap.dictating` 加入 _capVisible gate,侧栏关也能看到自己说的),micStop→`__vcCapDictEnd` 淡出。
- **ASR 2.0**(agent 调研,官方文档实锤):Doubao-Seed-ASR-2.0(2025-12-05)=**只换 Resource-Id** `volc.bigasr.sauc.duration`→`volc.seedasr.sauc.duration`(端点/model_name="bigmodel" 不变),关键词召回+20%(PPO 强化,专治专有名词/多音字),1元/h;⚠**控制台须先开通 2.0 商品否则鉴权失败**→ 做成凭证开关 `asr_v2`(voice-config 白名单+语音设置卡 checkbox,默认关保 1.0)。**未接的准确率大杀器**:`corpus.context` 热词直传 + **`context_type:"dialog_ctx"`+`context_data`**(≤800 tokens/20 轮,官方支持塞"业务场景信息"=书页关键词/术语注入,2.0 的 +20% 正是这个场景)——候选下一批。同传 ast 当纯 ASR:端到端同传模型(非 sauc 同源),按 token 计费更贵,不划算,维持不接。

### ASR 语境注入(v3-㉓,用户设计,2026-07-11)

- **范围拍板**(用户:"固定的任务相关关键词 + 页面相关的动态内容"):双通道各司其职——
  ①**hotwords**(`corpus.context` 直传,词条式权重高,1.0/2.0 都吃):固定层=`_ASR_TASK_WORDS` 指令词表(翻页/高亮/制卡/Anki/深度思考/找视频/挂断…26 个,认错指令最伤)+ 动态层=书名+本页未掌握生词(≤50 条去重);
  ②**dialog_ctx**(`context_type:"dialog_ctx"`+`context_data`,成段语境,**仅 asr_v2 开时**注入防 1.0 握手不认):从新到旧=页面文本摘要(350字)+ 最近两轮对话(各120字),≤800 tokens 预算内——2.0 的 +20% 关键词召回主打场景。
- **注入时机**:sauc 协议只认握手配置(无中途更新帧)→ **每次长按开 ASR 时快照当下语境**;通话中翻页滞后接受(下次开启即新)。
- 链路:前端 `_agentCtxQs()`(RC.adapter().getContext() 回退 __voiceContext)把 file/page 拼进 `?mode=agent` 的 qs → relay `handle_agent(bws, file_rel, page)` 握手前复用 `_fetch_book_ctx`(页文本/生词/助手历史一把拉)构造 corpus,拉取失败裸连不阻塞。冒烟:hot=34 注入成功、1.0 兼容、agent_ready 正常。

### 字幕联动 + 听写朗读互斥 + GPT Realtime 第二引擎(v3-㉔,2026-07-11)

- **字幕↔侧栏联动**(用户设计):MutationObserver 侦听 `#ep-side` class——开侧栏 `capClear()` 字幕立即消失(其内的"回等待"被 `_capVisible` gate 自然拦住);关侧栏且语音功能活跃(通话/朗读/听写)→ `capWait(true)` 自动回来。
- **听写-朗读互斥**(耳机路由残余根治):Apple 听写(Web Speech)占麦时 iOS 会话=录音类别 → 朗读被强制扬声器,这是"修完还不走耳机"的根因。修 = 自动发送后 `micHold` 暂停听写(onend 不重启,按钮回暗)+ `__vcDictAudioOff` 关掉听写期间建的朗读 AudioContext(路由已粘,重建拿干净 playback=耳机);回答完且 `__vcTtsBusy`(还有句在播/在队)为假 → `_micResumeWhenQuiet` 恢复连续听、按钮亮回。副产物=根治 AI 朗读被听写录回去的回声。
- **GPT Realtime 第二引擎**(用户要求,`gpt-realtime-2.1-mini`,2026-07-06 发布):
  - **凭证** `~/.config/openai-realtime.json`(chmod 600,**绝不进 git**);key 已验证有效。
  - **架构=协议翻译层** `handle_openai(bws, file_rel, page)`:OpenAI GA 事件 ↔ 前端既有豆包事件语义——`speech_started→450`(打断+truncate)、`input_audio_transcription.completed→451`、`output_audio_transcript.delta→550`、`output_audio.delta→PCM 裸转发`、`response.done→359`、工具→`tool_status`/`client_action`。**前端唯一改动**=`up_rate` 事件把上行采样从 16k 切 24k(OpenAI 只吃 24kHz;`_upRate` 参数化 onCap,teardown/start 复位 16k)。
  - **工具=原生 function calling**:voice-tools 目录行直接当 description(args 用法在文中),parameters 宽松 schema(`additionalProperties:true`)透传 dispatch——**不再需要豆包那套 JSON 协议/静音/代播确认语 hack**。`deep_think`(转交侧栏 chat/Claude 拿答案回填,GPT 自己念)和 `recall_study`(`_study_digest` 直接回填,128k 上下文吃得下)作虚拟工具保留。
  - **上下文**:页文本进 instructions(GA 的 session.update 部分更新**不重置对话**,翻页即热更);选中/笔迹状态走 `conversation.item.create` system 消息——与豆包 510 增量哲学同构。128k 窗口(豆包 12K 的 10 倍)。
  - **打断**:server_vad `interrupt_response:true` 自动 cancel;WS 场景 **truncate 必须 relay 发**(`conversation.item.truncate`,audio_end_ms=已转发字节/48 近似已播时长),不发模型会以为整段都说完了。
  - **切换**:语音设置卡「通话引擎」下拉(voice-config `rt_engine`:空=豆包 S2S/`openai`)——`handle_browser` 无 mode 分发时按凭证选 handler,**电话按钮同一入口零改**。`rt_model`/`rt_voice`(默认 marin)可配。
  - **价格**(官方):mini 音频 in $10/M、cached $0.3/M、out $20/M(**输出约豆包 S2S 的一半**);文本 $0.6/$2.4。会话上限 60 分钟(到点断线,前端既有重连机制会重拨=新会话)。
  - 调研全文(GA 事件名/字段/坑)存档:见本节上方 agent 调研结论(模型谱系/事件清单/function calling 序列/truncation 语义)。

**㉔ GPT Realtime session 配置补全**(据用户上传的 2.1 官方说明书):`reasoning.effort` 默认 low(官方:普通语音代理别默认 high,延迟/成本;凭证 rt_effort 可调)、`max_output_tokens:2048`(护栏,1–4096/inf)、`parallel_tool_calls:False`(我们工具多有副作用/顺序依赖,串行稳)、`truncation:retention_ratio 0.8`(官方力荐:低频批量截断保缓存前缀,比默认逐轮小截强)。加 **`wait_for_user` 静音 no-op 工具**(官方提示指南:背景噪声/等待音乐/没在对助手说话时它调这个 → relay 收到回空 output **不发 response.create**=不出声,省音频费不寒暄)。instructions 加写操作先说一句+成功才报完成+音频含糊请重说(官方工具安全策略)。**冒烟**:relay 连 OpenAI 全链路通(handle_openai 分发/WebSocket 连接/认证全过),唯一卡点=**测试 key 报 insufficient_quota(额度不足)**——协议翻译层无误,待 OpenAI 账户充值即可用;首帧 error 已明确回前端(带充值提示)。豆包引擎不受影响(rt_engine 默认空)。**账单**(官方 response.done.usage 为准):按 session+response 幂等入账,分模态/缓存/输入输出累计;input transcription 是**另一个模型另计费**(在 transcription.completed.usage,别漏)。会话 60min 上限,到点断线走前端既有重连=新会话(上下文不跨连接,需要续接得自己重建)。

**㉔b 用户质疑修正**("复用 S2S 前端会不会压制 2.1-mini 能力"):前端复用的只是**显示层**(字幕/对话窗/工具卡/播放),不碰模型能力;真被第一版压掉的在 relay 协议层,已修三处——①`server_vad`→**`semantic_vad`**(2.1 招牌:按语义判断说完没,停顿思考不抢话;官方推荐配置,eagerness auto);②**翻页不再 session.update instructions**(改前缀=prompt cache 全灭,cached $0.06 vs 全价 $0.6 差 10 倍;官方成本指南明确反对+豆包⑭同款教训)→ instructions 整场固定,新页内容走 conversation.item.create **system 增量消息**("翻到第N页,内容:…之前页作废以本条为准");③usage 按天账本 `state/openai-usage.json`(response.done.usage 分模态/缓存入账+官方单价估算,样例1轮≈$0.3 音频out主导)。**尚未吃到的 2.1 能力(候选)**:图像输入(see_page 类工具可把渲染图直接喂 GPT 而非 Claude 文字转述——需确认 GA input_image item 格式+voice-tool 端点 _vision 穿透);MCP 服务端代管工具(我们 MCP server 在 Tailscale 内网,OpenAI 云端够不到,不适用);Stored Prompt(单用户场景收益小)。

**㉕ 编排选 Codex 静默降级 bug(用户报告)+图像开关+MCP 研究**:①bug 根因=编排/快路两处守卫把 codex 预设**静默降级回出厂默认 Claude**(catalog 又允许所有 action 选全部后端)→ Claude 限流时用户以为切走了实际还在撞。修:快路守卫撤掉(单轮 _deep_ask 本就支持 codex);完整编排守卫降级目标改 **Gemini 编排+notice 明示**;catalog 加 `backends_by_action`(orchestrator 去 codex/deep 仅 claude),前端两份 _buildMsTask 都按它过滤下拉(选不了=不会坑)。②`rt_image` 开关(白名单+设置卡,默认关):看图类工具返回的 `_vision` 原图从 output 文本剔除(防 base64 截烂 JSON),开关开→以 input_image conversation item 直喂 GPT(⚪格式按 GA 推定待实测,报错关掉即回文字链路);开关关→附注告知模型。③MCP 代管研究(任务#280):ts.net 仅 tailnet 可达,OpenAI 云端要 Funnel 公网暴露;工具定义+结果都进上下文计费;内网 relay 直调延迟必然更低——倾向维持现状,充值后实测对比再定。

**㉕b "疯狂打招呼"事故复盘**(用户真机首测):两个叠加根因——①`session.audio.output.format` 漏 `rate` 字段 → **session.update 整条被拒**(GA 严格校验),会话跑 OpenAI 默认裸配置(无人设/无工具/无转写/默认 VAD 热情通用助手);②**回声自激**:GPT 声音经扬声器被麦拾回 → 默认 VAD 判"用户又说话"→ 再回应 → 无限打招呼(日志实锤:连续 9 条 assistant 无一条用户转写)。OpenAI 官方:**WS 模式回声抑制是应用责任**(WebRTC 才自带;豆包是服务端一体化自带,所以豆包链路从没这问题)。修:①output.format 补 rate:24000;②session.update 后**必须等 session.updated 确认**,收到 error 就报错收场(绝不让裸配置会话跑起来烧钱);③前端**回声能量门**(s2s 且 playing.length>0 时,20ms 段抽样 RMS<0.02=回声残留→丢包;静默期不启用,真人打断近场响亮能过门);④上行合批 100ms(50msg/s→10)。**教训:严格校验的 API 必须以服务端 ack(session.updated)为配置生效依据,不能发完就当成功。**

**㉖b GPT 引擎语言+设置分组**(用户需求):①`rt_lang`(""自动/zh/ja/en):instructions 语言段按它生成(**自动=跟随用户说话语言+朗读原文用原生发音**,治"写死中文→用中文读音念日语"),transcription 的 language 提示也跟着走(转写准确率受益);②语音设置卡**按引擎分组渲染**:选 GPT 藏豆包 S2S 专属项(S2S音色/方言/语速/音量/人设/唱歌),显 GPT 组=模型档(2.1-mini/2.1)+音色(10个,marin/cedar官方推荐)+语言+**接话灵敏度**(semantic_vad eagerness:auto/low/medium/high,凭证 rt_eagerness)+思考强度(rt_effort)+**人设 rt_instructions**(GPT 独立字段,优先于 system_role)+图像输入;朗读/字幕/ASR 2.0 与通话引擎无关恒显(标注分隔);切引擎 `_save` 后整卡重绘(`.ams-voice-part` 清旧重画)。白名单齐:rt_engine/model/voice/effort/image/lang/instructions/eagerness。

**㉗ 笔迹/看页类图直喂**(用户提议):see_ink/see_page 原本在**工具内部**就调 _vision_for 转文字(rt_image 开关碰不到它们的图)→ 加 `ctx["_want_vision"]` 标志(GPT 引擎+rt_image 开时 relay 传入):工具跳过本地转述,返回 `{"_vision":[原图], "看图提示":note}` 穿透 → relay 既有 input_image 直喂管线自动生效(note 留在 function output 文本里跟图一起到 GPT)。收益:少一跳视觉模型转述(快)+ 省一次 Claude/Gemini 视觉调用(转述细节不再有损)。Claude/Gemini 编排路径不带标志,行为不变。see_figure 本就返回 _vision,天然生效。

**㉘ 官方范例对照修复批**(用户方法论:别自研,抄官方/社区成熟方案;agent 挖 openai SDK 源码+Chromium/WebKit issue 实锤):
- **图像格式实锤无误**(openai-python SDK 类型定义:content type `Literal["input_text","input_audio","input_image"]`+`image_url: str`(data URL 可)+可选 `detail: auto/low/high`)——"看不到图"真凶=**跛脚会话**(webapp 重启窗口 _fetch_tools_lines 静默失败→会话只挂 3 个工具连 see_ink 都没有;日志 `tools=3 p50(0字)` 实锤)。修:<10 工具重试一次,仍败报错收场不哑巴开场。
- **回声真根源**(Chromium issue 40252911):浏览器 `echoCancellation` 的参考信号**只取 WebRTC/<audio> 元素播放路径,纯 WebAudio 播的声音不被 AEC 消**——我们 playPcm 正是纯 WebAudio→外放时模型声音全量进麦,能量门只是创可贴。**标准解=本地 RTCPeerConnection 环回**(社区公认 workaround):`createMediaStreamDestination→pc1↔pc2 环回→pc2.ontrack→<audio> 元素播` → AEC 把它当"远端参与者音频"自动从麦克风消掉。实现 `_aecSetup/_aecTeardown`(onopen 手势链内建,playPcm 的 connect 目标=环回 dest,失败回落直连;teardown 清 pc+audio 元素)。
- **音量忽大忽小=iOS 系统 ducking**(WebKit #218012/#262569:麦克风活跃时系统压低页面播放音量,iOS 16.4 时代已知 bug 链):Web 无 API 直接关;环回后播放走"通话音频"路径通常更平稳,属实测项。
- **官方降噪**:session.audio.input.`noise_reduction {type:"far_field"}`(SDK 字段,near_field/far_field)——外放/桌面麦场景远场降噪,回声残留+环境噪对 VAD 的干扰双降。冒烟:session.updated 通过(字段被 GA 接受)。

**㉘b 播放器官方形态对齐**:①**AudioContext 定频 24k**(通话 ac+朗读 _tts.ac,`{sampleRate:24000}` 失败回退默认)——旧版默认 48k 时每个 24k chunk 被浏览器**独立重采样**,边界无连续性=周期性 click/断续(官方 console 即 24k 定频+worklet 环形缓冲;我们先做定频,环形缓冲视实测再说);采集侧同受益(worklet 24k 出帧)。②**truncate 精确化**:官方语义 audio_end_ms=用户**实际听到**的毫秒,旧版用已转发字节(转发远快于播放)严重高估≈不截→上下文残留没听到的内容;改=前端 `_playStat`(首块开播时刻+累计入队时长)在 450 打断时算真实已播毫秒发 `played_ms` 消息→relay `pend_trunc` 收到即精确 truncate(600ms 没回报按字节兜底);359 整轮播完清零。③usage 账本加 `in_image` token($0.80/M mini)——**>0=模型真看到了图**,rt_image 直喂的最硬验证信号。④rt_image 开关链路复核:设置存 doubao-voice.json、relay _creds() 读同一份,**链路本来就通**(agent 报告该条有误);用户截图"看不到图"真凶=跛脚会话(㉘已拦截)。外放断续若仍存在,下一张牌=interrupt_response:false+手动打断(社区 speakerphone 实测配方)。

**㉘c 笔迹链路断点修复**(用户二测仍"看不到",截图 Gmm/r²):前端确实发了 `{type:"ink",page,strokes}`(顶栏"已同步你的圈画"为证),断在我 openai up() 的 state/ink 合并分支**没对齐豆包版真实字段**——ink 消息没有 sel 字段,合并处理误清 book["sel"],且注入措辞太弱("本页有手写笔迹(看内容用 see_ink)"),GPT 宁可让用户截图也不调工具。修:①state/ink 拆开、字段与豆包版同源(state=sel/focus/figs;ink=page/strokes,校验 ip==当前页);②ink 注入**指令化**("必须立即调用 see_ink…绝不说看不到、不要让他粘贴/截图");③instructions 加手写铁律(提到『我写的/我画的』永远先调 see_ink,说看不到=错误行为);④两分支补 stderr 日志(之前无日志=断点不可见)。**教训=verify-against-actual-source 再犯**:当时"篇幅控制简化"合并了两个消息类型没读豆包版字段定义。

**㉘d 手写公式认错复盘**(用户疑"图太模糊"):亲测**图不模糊**——_ink_focus_image 实际输出 1253×1253、笔迹鲜红清晰(该书页面 pt 大,scale 2.6 已出高清;顺手把 scale 改成按目标长边 1100px 动态算,小页面书的小笔迹也不再 400px 级)。真凶=**模型缺语境**:营养学书页上冒出物理公式,模型没有"这是公式"的先验,手写 G/A 形近就猜错。修=see_ink 的看图提示加**手写算式解读指引**(先看整体结构分数线/上标→再认字符;易混对 G↔A↔C、r↔n↔v 用整体含义合理性定夺;明示"他学的内容不限于本页主题")+GPT input_image 显式 `detail:"high"`。**端到端验证**:同图+新提示,vision 预设一次认对"分子 Gmm(万有引力)/分母 r²/分数线"。教训:视觉识别差先亲眼看图源排除质量问题,再补语境——人认得出靠的是先验,把先验写进 note 模型也认得出。

**㉘e 笔迹更新被连评两次**(用户报告+定性正确):两因叠加——①㉘c 的注入措辞"必须立即调用 see_ink"被模型当**行动指令**,在下个回合(含 VAD 误触发的空回合)主动评论笔迹;②画一幅图前端防抖后仍推多次 ink,每次都注入=每回合评一次。修:①措辞条件化("这只是状态记录,不要对本条做任何回应/主动评论;只有他问到时才调 see_ink——那时绝不说看不到"——**状态记录≠行动请求**,能力提示保留主动性去掉);②ink 注入指纹去重(页+笔画数+末笔末点)、sel 同理;③instructions 加"状态消息永远不回应不主动评论;没听到清晰说话调 wait_for_user 安静结束,别自己找话说"。教训:提示强度是个旋钮——治"不作为"拧太狠会反弹成"过度作为",条件化措辞(何时做+何时不做)比强度更稳。

**㉘f 撤回声能量门**(用户实锤"说到一半就插话/有段没听清"):门(㉕b)丢低能量包的机制性缺陷——**PCM 流无时间戳,丢包≠插入静默而是把话剪辑拼接**:句中轻声段(句尾弱化/低声思考)被丢 → OpenAI 收到残缺音频 → semantic_vad 把人为空缺判成"说完了"→ 句子中间插话;"有段没听清"=音频被剪碎的直接症状。回声正解=AEC 环回(㉘)已上,门是既多余又有害的创可贴 → 全撤。**教训:任何在上行音频流里做丢弃/门控的方案都会破坏 VAD/转写的输入完整性,回声必须在信号层(AEC)解决,不能在传输层裁剪。**接话过快的余量调节=设置卡「接话灵敏度」选"慢热"(semantic_vad eagerness:low)。

**㉙ 半双工外放模式**(用户实锤 AEC 环回在其设备无效——voice-log 里 AI 的话被转写成用户 Q「あ、ごめんごめん…」触发自答;调研早有结论:WS+外放无银弹,可靠解=耳机/半双工/WebRTC):**默认半双工**——AI 播放期整段静麦+播完 350ms 残响缓冲(onCap 头部 gate,f32buf 清空)。与能量门的本质区别:**全有或全无=干净静默**,不像选择性丢包那样把话剪碎破坏 VAD。代价=AI 说话期间不能语音打断(等它说完/点按钮);耳机用户在 GPT 设置勾「全双工打断」(rt_full_duplex,⚠白名单 False=删字段语义,所以用**正字段=勾表示开全双工**,默认无字段=半双工)恢复随时插话。链路:relay up_rate 事件带 half_duplex 布尔→前端 _halfDuplex(teardown/start 复位);仅 GPT 引擎生效(豆包无 up_rate=false,其服务端自带回声处理)。WebRTC 直连(治本,官方对浏览器场景的正式推荐)列任务 #280。

## ㉚ GPT Realtime WebRTC 直连(2026-07-12,用户拍板"那就webrtc呗",外放回声治本)

**为什么治本**:浏览器 AEC 的参考信号只取 WebRTC/`<audio>` 元素播放路径(Chromium issue 40252911)——WebRTC 模式下远端音频天然走 `pc.ontrack→<audio>`,**回声消除全自动生效**,外放+全双工随时插话都成立,㉙半双工/AEC 环回这些 WS 妥协全都不需要。这正是 OpenAI 对浏览器场景的正式推荐形态。

**架构:媒体直连,控制面留在自家后端**(密钥绝不下发):
```
浏览器 getUserMedia(echoCancellation) ──媒体轨──> OpenAI (WebRTC 直连,音频不过 Pi)
   │ createDataChannel('oai-events')  <──事件──>  (转写/函数调用/response.done 走 dc)
   └ SDP offer ─POST /api/assistant/rtc-call(Pi 代理,带 key)→ OpenAI /v1/realtime/calls → answer
工具循环搬到前端:dc 收 function_call → fetch /api/assistant/voice-tool → dc 回填 output+response.create
```

**后端三端点**(assistant.py,key 读 `~/.config/openai-realtime.json`):
- `POST /rtc-session`:下发完整 GA session 配置——instructions 与 relay `_oa_instructions` 同源(语言段 rt_lang/手写铁律/状态消息规则/页面文本 fitz 直读)+TOOLS 目录转扁平 schema+deep_think/wait_for_user+semantic_vad(rt_eagerness)+noise_reduction near_field(WebRTC 场景近场)+voice/effort/model 全在 session 内;**不带 audio format**(WebRTC 媒体轨自动协商,这点与 WS 版必须 rate:24000 相反);返回 `{session, model, rt_image}`
- `POST /rtc-call`:SDP 代理——`requests.post(…/v1/realtime/calls?model=X, files={"sdp":(None,sdp,"application/sdp"),"session":(None,json,"application/json")})`(multipart,官方形态),answer SDP 原样回传
- `POST /rtc-usage`:前端把 response.done 的 usage 转发来记账(state/openai-usage.json 与 WS 版同一本账,_RTC_RATE 含 in_image 0.80)

**前端 rtc 模块**(rc-voicecall.js,~180 行):核心技巧=**ws shim**——`rtcStart` 成功后 `ws = _rtcShimWs()`(readyState:1,send 把既有 `{type:page/ink/state/text/cancel}` 同步消息翻译成 dc 事件,二进制音频忽略)→ 翻页同步/选中注入/输入框发送/挂断检测等**全部现有代码零改动照常工作**。各消息语义与 relay WS 版对齐:page→fetch `/pdf/api/page-text` 拼 system 增量;ink→指纹去重(页+笔画数)+条件化措辞(状态记录≠行动请求);state→sel 去重;text→item.create+response.create。工具循环 `_rtcTool`:wait_for_user=静音回填不 response.create;deep_think→`_rtcDeep`(fetch /chat SSE 解析 answer 事件);其余→fetch voice-tool(ctx 带 ink/selection/_want_vision)→client_action 本地 dispatch(比经 relay 更直接)→`_vision` 转 dc input_image detail:high 直喂→onToolStatus 工具卡全复用。下行 `_rtcOnEvent`:transcript.delta→对话窗+字幕 capStream;speech_started→清 curAText+__vcSyncNow;transcription.completed→capUser;response.done→usage 上报+_capMaybeHide。远端音频=ontrack→隐藏 `<audio>` autoplay+playsinline(AEC 生效的关键,**不要**改成 WebAudio 播)。

**引擎分流**:`toggle._connect`(toggle 连接/vc-new 重连共用)——**仅 s2s 模式**查 voice-config,`rt_engine==='openai_rtc'`→rtcStart,否则 start(WS relay);**agent 模式(mic 长按 ASR)恒走豆包 relay 不受 rt_engine 影响**(与 relay 按 mode 分发同语义,⚠差点漏:第一版分流没判 mode 会把 ASR 也劫走)。teardown 头部挂 rtcTeardown(pc/dc/audio 元素/mic 全清)。设置卡引擎三选:豆包 S2S(默认)/GPT WebRTC(推荐:外放无回声+可随时插话)/GPT WebSocket(外放半双工,保留);isOA 判定覆盖 openai|openai_rtc 两值(GPT 组设置两引擎共用)。

**WebRTC 版不需要的 WS 包袱**(天然消失):半双工 gate/AEC 环回/播放毫秒回报 played_ms(truncate 由 OpenAI 端媒体流自己算)/AudioContext 24k 定频/上行合批。新会话=重连即得(WebRTC 每连接就是新 session,无 dialog_id 概念)。**留意**:60min 会话上限同 WS;iOS ducking(麦活跃压播放音量)理论上 WebRTC 通话路径更稳,属实测项。

**㉚b-e 首测修复批**(2026-07-12,用户真机):
- **b 回复显示两遍**:GPT 用户转写(whisper)**异步迟到**于 AI 回复开始流(豆包 ASR 恒先于回复,从没暴露这问题)→ setSub('u') 插用户气泡时断开 curAEl 指针 → 后续 delta 带全量文本另起新气泡=前半段定格+完整版重来。修:迟到用户句按时序 insertBefore 到进行中 AI 气泡**前面**不断轮;response.created 重置气泡(text 输入触发的响应没有 speech_started)。
- **c rtc 字幕**:transcript delta 是文字生成速度(1-2s 全到)≠音频播放速度 → 字幕瞬跳末句;response.done=生成完≠播完,而 rtc 音频在 `<audio>` 元素里,`playing` 队列看不见 → 提前淡出。修:rtc 专用**逐句估时队列**(TTS≈6字/秒,`_rtcCapFeed/_rtcCapPump` 复用 capShow 滚动原语;残句等闭合),淡出由队列放完收尾;speech_started 补 capClear;连接成功补 capWait(true)。
- **d 笔迹边沿触发**(用户设计"只告诉一次变化"):AI 调过 see_ink 的旧结果在历史里,轻飘飘的"笔迹有更新"打不过它"亲眼看过"的工具结果 → 嘴硬"没变化"。修:变化只通知一次(`_rtc.inkDirty` 边沿,继续画不再打扰),但这一次**显式作废旧记忆**("你之前看到的已过时作废,你现在不知道纸面上是什么;没重新看就答『没变化』是错误行为");see_ink/see_page/see_figure 成功复位边沿;instructions 铁律补跟进问句(『现在呢/看到了什么』)。**影响 AI 认知的通道权重**:instructions<system 状态消息<user 消息<工具结果;下一档手段=伪对话对(user+assistant 自我承诺,豆包 510 注入验证过)。**缓存无忧**:状态消息是 append 不是改前缀,前缀缓存照常命中。
- **e 图像 sideband**(第二次 see_ink 哑死根因):WebRTC data channel 单条消息有 SCTP 上限(Safari≈64KB),base64 笔迹图几百 KB,**超限发送按规范直接关闭 dc**=通话哑死。修:图像改**服务端 sideband 注入**——`/rtc-call` 从 OpenAI 响应 Location header 提取 call_id 下发;voice-tool 带 rtc_call_id 时后端 `_rtc_sideband_images`(websockets.sync 连 `wss://api.openai.com/v1/realtime?call_id=X`,官方服务端通道)把 input_image 直接注入会话,图**绝不经 dc**;sideband 失败也不回退 dc(如实告知传输失败);dc.onclose 明示"数据通道断开"。依赖:`apt install python3-websockets`(webapp 用系统 python3)。

## ㉛ 通话对话进侧栏对话流(2026-07-12,用户设计)

通话(豆包 S2S + GPT rtc)的双方对话不再显示在通话浮层的迷你对话区(vc-sub),**直接进侧栏 #asst-thread**,与文字 AI 对话同流同清:
- **投递**:rc-assistant 暴露 `__asstVoiceMsg(who,text)`('a'=全量覆盖当前 AI 轮气泡/'u'=用户句,AI 轮活跃时按时序插前/'reset'=断轮)+`__asstVoiceLog(q,a,file,page)`(POST `/api/assistant/log` via:'voice',与文字对话同一历史库);rc-voicecall 的 setSub 头部分流(接口在→侧栏,不在→vc-sub 兜底,收藏夹独立页等无侧栏场景不受影响)。
- **轮次管理**:豆包 451 定稿存 `_lastU`、359 落库、450 打断=半截轮先落库+reset;rtc transcription.completed/text 输入存 `_lastU`、response.done 落库、speech_started/response.created reset;挂断 teardown 清 `_lastU`+reset。
- **浮层瘦身**:侧栏投递可用时 vc-sub+vc-grab(拖高抓手)隐藏,通话条只剩状态行;视频卡区(vc-vids)保留。
- **清空三位一体**:侧栏 🗑 清空本体清显示+POST clear 清服务端记录;rc-voicecall 捕获旁听同步 fresh 重连(豆包清 dialog_id/rtc 重连即新会话)→ 语音侧记忆和缓存自然作废。旁听重连顺修为 `toggle._connect` 引擎分流(原硬调 start 会把 rtc 清空后重连回 WS 链路)。
- 字幕系统不变:侧栏开=看侧栏对话流(字幕 gate 掉),侧栏关=底部字幕。

## ㉜ 语音配图走原生工具(2026-07-12,用户截图揪出 markdown 假图)

用户语音说"想看照片",GPT 没调工具,直接输出 `![七草粥の写真](image_url)` markdown 占位符假装贴图。**三层根因链**:
1. **instructions"你没有联网搜索能力"误伤**:search_image(Commons+Google 图搜)是**真联网工具**,这句一刀切声明把模型劝退,想帮忙只剩幻觉一条路。修:精确化——"没有**通用网页搜索**;但 search_image/search_video 是真联网工具,想看图/视频**必须调用**;绝不输出 markdown 图片占位符假装贴图"。三处同修:rtc-session / relay `_oa_instructions`(GPT WS)/ relay 豆包 SP。
2. **目录行 markdown 教学有毒**:search_image 的 TOOLS 目录行是给**文字助手**写的("拿回结果后在回答对应概念旁用 markdown ![..](image_url) 插入")——rtc-session 把目录行全量当 description(1024 字),模型把这套 markdown 用法学去了语音回答里。修:rtc-session 加语音场景 description 覆盖表 `_vo`(search_image 语音版:"图会自动显示在用户界面,口头简短说明即可,绝不编链接");relay WS 版 description 截第一句(52字)本就没带毒,不用改。
3. **语音场景图卡无渲染管道**:`_t_search_image` 返回纯数据(文字助手靠模型 markdown 嵌图+侧栏渲染;语音回答是音频,没这管道)——即使调了工具图也显示不出来。修:voice-tool 端点(仅语音链路走)对 search_image 结果注入 `client_action {fn:'renderImages'}`,前端 dispatch 渲染图卡进侧栏对话流(缩略图 grid+概念标签,点开原条目页);文字助手不走此端点,零影响。

**教训**:接入原生 function calling 的模型时,复查所有"能力声明"(负面声明会误伤同类工具)和"目录行教学文案"(写给别的渲染管道的用法会被模型带进当前场景)——工具目录是跨场景共享的,per-场景 description 覆盖是正解。

## ㉝-㉞ 联网工具+配图多源+rtc 断线自愈(2026-07-12)

**㉝ web_search 通用联网**(用户问"模型没联网能力吗"):模型权重不联网;官方指南实锤 Realtime API 只支持 function calling+MCP,**无内建 web search**(那是 Responses API 的)。加 `web_search` 工具=Google Programmable Search 的**网页模式**(同一个 CSE 引擎不带 searchType 即网页结果,`image_search.search_web`),返回标题+摘要+链接;进 TOOLS 一处注册全线受益(GPT rtc/WS/豆包/文字助手),VOICE_CACHEABLE 只读缓存;三处能力声明改"联网=web_search/search_image/search_video 三个真工具"。⚠ **GCP 项目未启用 Custom Search JSON API(403)**——需用户控制台启用,同时解锁配图的 Google 图搜源;与配图共享 100 次/天免费池,目录行叮嘱省着用。配图"没搜到"从 error 降为 ok:false+换英文词引导(error 会亮⚠且被模型当故障弃用);voice-tool 报错上 stderr 日志(journalctl 直查,不再猜)。

**㉞ 配图 Bing 零 key 兜底 + rtc 断线自愈**(用户提议"最原始的浏览器地址式谷歌搜图"+报告后台切回假活/重连失败):
- **搜索引擎直爬实测**:Google 对无浏览器 HTTP 请求甩 **JS challenge 重定向占位页**(带 SOCS/CONSENT cookie 也不放行,要无头浏览器,弃);Bing 图搜一次 HTTP 全通(`"murl"`/`"purl"` 结构稳定)曾落地为零 key 兜底——**㉞b 已按用户裁定撤除**(结果质量太差:水印图库/新闻配图充数),第二腿只留 Google API(待启用)。缓存 partial 标记保留(第二腿没跑成的残缺结果只缓存 1 天,防断腿期污染 30 天);撤除时清掉了已写入的 bing 缓存条目。
- **rtc 假活根治**(用户:后台切回显示通话中实际全聋):ws shim 的 readyState **恒为 1**,WS 版靠 onclose 的断线检测在 rtc 完全失明;iOS 切后台系统掐 WebRTC。修三件:`pc.onconnectionstatechange`(failed/closed 立即判死;disconnected 等 3s 自愈窗口)+ visibilitychange 回前台检查真实 `pc.connectionState` + dc.onclose→`_rtcDead`。判死=rtcTeardown+状态复位+**非主动挂断自动重连**(800ms)。

## ㉟ EPUB 语音全链路对齐(2026-07-12 深夜,用户拍板"唯一侧栏原则"后 autonomous 完成)

**原则**:侧栏唯一(rc-sidedrawer+rc-assistant+rc-voicecall 都是共享层),PDF/EPUB 只是 adapter——语音必须在 EPUB(正主=统一 HTML 主文档版 `/pdf/epub/view`)同等可用。侦察实锤五层断点,全部补齐:

1. **脚本没上页**:`epub_html_reader.html` 根本没加载 rc-voicecall.js(PDF 模板独占)→ 加载(rc-assistant 之后);按钮注入锚点(#asst-input/#asst-mic/#asst-quick/#fs-toggle)EPUB 本就齐备,脚本一上按钮自动出现。
2. **opts 无接线**(PDF 靠 21-misc-ai 传 file/page,EPUB 无等价物)→ **中间层正道**:`toggle()` 开头 opts 缺 file 时经 `RC.adapter().getContext()` 补齐;**EPUB 的"page"=current_section_idx+1(1-based 章号)**,此约定贯穿全链。`_agentCtxQs`(ASR 语境)同款兼容。
3. **位置/选中同步无等价物**(PDF=21-misc-ai 的 2s 轮询 __vcSyncNow)→ rc-voicecall 自建**共享轮询**:`window.__vcSyncNow` 不存在时(=非 PDF)每 2s 读 adapter getContext → setPage(page)+syncState(selection);PDF 有自己的轮询不受影响。
4. **上下文后端 PDF 专用**:`/pdf/api/page-text` 与 `/rtc-session` 按 `.epub` 后缀分流走 `_epub_section_paragraphs(rel, page-1)` 章节纯文本(⚠ **fitz 能打开 epub 但用自己的 reflow 分页,与阅读器 section 完全错位——绝不能落进 fitz 分支**);relay 的 `_fetch_book_ctx` 调的就是 page-text,零改动自动受益(豆包/GPT-WS 链路同吃)。`/voice-ctx`(圈画/生词,全按 PDF 页渲染)对 epub 返回干净空结构。
5. **落库/历史/清空命名空间错位**(通话记录写死全局 `/api/assistant/log`,EPUB 侧栏历史/清空是 book-scoped epub-convo)→ `__asstVoiceLog` 优先走 `HOST.voiceLog`(adapter 钩子;EPUB 实现=复用现成 `/pdf/api/epub-convo/append` 一轮两条);`_rtcInjectHistory` 经 `window.__asstHistUrl()`(=HOST.historyUrl,EPUB=本书 epub-convo)。清空三位一体在 EPUB 闭环:侧栏 clear(epub-convo/clear)+rc-voicecall 旁听 fresh 重连+回放读已清空的本书历史。

冒烟(test_client)四链路全过:page-text epub 分流出章节文本/rtc-session instructions 含"第 N 章(节)"正文/voice-ctx 空结构/epub-convo 落库→历史→清空闭环。**真机待验**:EPUB 页面按钮出现、通话上下文=当前章、翻章同步、对话进侧栏、清空。已知余项:插图/生词直塞对 epub 为空(可后续按需接 epub 数据源)。

## ㊱ 动态视口注入 + 视口截图 + 单双页 bug(2026-07-12 深夜,用户睡前三点+一个 bug)

1. **EPUB 动态窗口注入**(用户:"整章当页注入太长;EPUB 有按实际显示内容变化的动态窗口"):EPUB adapter 的 `getContext().visible_text`(视口可见文本)本就存在——**前端直供**替代服务端整章:共享位置轮询 `setPage(pg, visible_text)`(text 指纹变化也推,同章内滚动=窗口变也同步);rtc `_rtcHandleUp('page')` 消息带 text 时直接注入"当前可见内容"(不再 fetch);relay `up 'page'` 带 text 时 `book["page_text"]=text` 直接 `_push_sp`(不走 `_fetch_book_ctx`);rtc-session 开话时 body.text=前端视口文本优先。PDF 的 getContext 无 visible_text=不受影响走页文本。整章截断(1500 字)保留为无 visible_text 时的 fallback。
2. **视口截图机制**(用户:"EPUB 笔迹要把即时的重叠渲染后的结果交给 AI"+"临时自建页的内容和笔迹 AI 看不见"——两个问题一个机制):`html2canvas` **自托管**(`/static/pdf/html2canvas.min.js`,CSP 内)+ rc-voicecall `_captureView()` 懒加载截当前视口(正文+墨迹 canvas+插入页 overlay **所见即所得**,排除侧栏/通话条/字幕等悬浮 UI,JPEG q0.82,<5KB 视为失败丢弃);rtc `_rtcTool` 对 see_ink/see_page **恒附** `ctx.view_image`(走 HTTP POST 无 dc 大小限制;返回图走 sideband)。后端 `_viewshot_result()`:**EPUB 恒用截图**(服务端渲不了 HTML;没拿到截图明确 error 不瞎猜);**PDF 三档兜底**——服务端无该页笔迹存档(自建页未写回场景)/裁不出笔迹区域 → 用前端截图。⚠ WS 链路(豆包/GPT-WS)工具由 relay 服务端调用拿不到前端截图,EPUB 看笔迹仅 rtc(WebRTC)引擎支持。余项:PDF 自建页"页码错位而不报错"(未写回时该页码指向原文档他页)前端判定未做——现兜底覆盖主诉求(无笔迹存档→截图)。
3. **单页开关侧栏被切双页 bug**(用户报告):`_spreadBeforePanel`(双页开栏临时切单列、关栏还原)的还原标记会**残留**——单列模式下开栏不清它,关栏时 `!= null` 判定成立就把用户切到 spread。修(18-grammar 旧抽屉+28-shared-drawer 共享抽屉**两处同款**):开栏时非 spread 分支显式清残留;关栏还原加 `readMode === 'continuous'` 校验(开栏期间手动改过模式=不还原),还原后无条件清标记。reader.js 经 `check_pdf_reader_js.sh` 重建部署。

## ㊲-㊳ 对话历史压缩(2026-07-12,用户需求"参考成熟方案")

**㊲ 会话间压缩**(挂断后空闲期做功):官方 Realtime 指南 8.4"摘要替代无限历史"+业界滚动摘要(LangChain ConversationSummaryBuffer 同款)。链路:teardown fire-and-forget POST `/compact-history`(幂等:新增<14 轮跳过;**清空竞态守卫**=写入前重载历史,已空就放弃,防"清空后压缩把记忆复活")→ Gemini 便宜档把旧轮滚动合并成 ≤300 字摘要(只留偏好/已确认事实/学习进度/未决事项),最近 6 轮保留原文(同秒轮次不拆开——upto_ts 按秒);摘要 sidecar(`state/assistant-convo/<uid>.summary.json`,EPUB=`epub-convo/<uid>/<key>.summary.json`)。回放:`_rtcInjectHistory` 走 `history?compact=1` 压缩视图(摘要+近几轮原文),替代全量灌注;侧栏显示仍全量。**清空三位一体扩展**:两处 clear 端点顺带删 summary。缓存友好:压缩只发生在会话之间,不碰在用前缀。实测 20 轮→170 字摘要核心事实全留。

**㊳ 会话内自动压缩**(⚠已被 ㊹ 外部审核默认关闭,待按官方 Cookbook 重做=task#285):经济账=会话内历史每轮按 cached 价重复计费(**音频缓存 $0.30/M 是大头**),压缩代价=一次缓存失效(摘要+近几轮全价),**会话再续 ≥2 轮即回本** → 触发判据用"每轮 `input_tokens` 超阈值"(usage 实时监控,response.done 每轮都有 cached 明细)。链路:rtc 前端记 item 账本(`conversation.item.added|created` 两个事件名都认)→ 每轮 done 检查 `input_tokens ≥ rt_compact_tokens`(默认 20k,voice-config 可配,0=关)→ `_rtcCompactNow()`:POST compact-history(force=1,阈值降 8 轮)拿摘要 → **批量** `conversation.item.delete` 删旧 item(保尾部 8 个)→ 摘要 system 顶上 → `_pageFp/_inkFp` 作废(轮询 2s 内重推当前页/笔迹状态)→ 对话流出 📦 note。90s 低频保护(官方:低频批量>频繁逐条)。**㊲b 顺修**:WebRTC 挂断后重拨必死——日志实锤 rtc-call 从未到达=死在 getUserMedia;根因=rtcStart 漏了 WS 版音频会话舞步(挂断把 iOS 会话切回 playback=静音麦)→ 开麦前先 `_ttsShutdown()` 再声明 `play-and-record`(㉑铁律再犯);失败提示补错误 name。
- **重连历史回放**(用户:重启对话没把聊天记录放回去——判断正确,rtc 每连接=全新 session 之前根本没做):dc.onopen 时拉 `/api/assistant/history`(㉛落库的同一份)近 14 条压成**一条 system 消息**注入("延续语境,不要重新打招呼")——官方指南 8.4 的摘要形态,不逐条造 item(省 item 数+不赌 assistant content type);vc-new 新话题(fresh)不回放。rtcStart 失败路径补状态复位(ws/按钮不残留假活)。


## ㊴-53 承诺核查 + 官方通读对照 + 外部审核修订 + 输出模态体系(2026-07-12 下午)

**㊸/㊸b 承诺核查→程序代执行**:实锤 mini 两次"已整理成卡片放进后台"实际零工具调用(journalctl+对话流无工具卡+任务系统零启动三重证据)。v1"打脸再走一轮"被用户否决(语音模型只是扳机不产卡片内容,多走一轮=白烧音频费)→ v2=**程序直接替它调 make_anki/make_note**(种子=用户请求句+它口头总结的要点,后台制卡模型自判内容,ctx 带 file/page),工具卡可见+零成本 system 记录;⚠别走 _rtcTool(空 callId 的 function_call_output 被拒),直调 fetch。voice-tool **成功调用也记日志**(`[voice-tool] name ok Ns`)——"调没调"从此一句 grep 实锤。

**㊶ 指南通读对照**(用户批评"没认真看指南"成立——grep 挑段落漏结构性要求):①instructions 含动态页文本=违反 §8.1 真 bug(跨会话缓存从未命中)→ 恒定化,页面内容并入拉模式池;②补 `truncation.token_limits.post_instructions=24000` 硬顶(§5);③账本按模型分价表(§7.1 标准版 $4/$32 vs mini $0.6/$10,混算=切标准版错账;usage 上报带 _model);④rtc-call 补 `OpenAI-Safety-Identifier`(§4.1,uid 哈希)。要点存档:§2.2 音频响应自带 transcript(assistant 历史本就以文字存);§7.5 whisper 转写是独立账单;§2.1 max_output_tokens 合法域 1-4096|inf。

**㊹ 外部审核修订**(用户拿代码给别的 AI 审,高质量已核实):实测成本结构=**输出音频占 80%**($1.29/$1.61,220 响应均 14.7s),输入才 14%、图片 1.1%——**控制回答时长才是主战场**。立即项:max_output_tokens 2048→512(≈25s 硬顶)+可测量长度规则(8s/15s/20s 摘要问继续/不复述/整段朗读转专用通道);会话内压缩默认关(㊳三处不安全:没等 item.deleted 确认/没按 turn 组删/12k<官方 Cookbook 生产区间 20k-32k,摘要应 root:true 放根部)。大项列 task:#283 持久 sideband RtcController(官方 server-side controls 形态)/#284 全链路 SQLite 账本/#285 压缩重做/#286 selection+ink 拉模式补全/#287 工具缓存+熔断+getStats+截图质量。

**㊿/51/52/53 输出模态体系**(输出音频 80% 成本的完整解法):
- ㊿ 手动挡:`create_response:false`,speech_stopped/打字/工具回填三路统一走 `_rtcRespCreate(src)` 按需选 `output_modalities`;`output_text.delta` 并线进 transcript 渲染管线。
- 51 **四态循环按钮**(通话中🔊按钮,键 rt_voice_mode):audio 全音频/mixed 混合(src='user' 直接提问=音频,'tool'/'deep'=文字)/text 全文字(音频费归零)/**tts=文字回复+豆包朗读通道代念**(done 时 speak(curAText);⚠TTS 走本地 WebAudio 不在 WebRTC AEC 参考里→播放期 `mic.enabled=false` 防它听见自己,__vcTtsBusy 轮询恢复+2min 兜底)。豆包引擎映射:audio|mixed=播,text|tts=静音(它无模态开关不省钱)。
- 52 **reply_text 自动路由**(外部审核设计,一次推理完成选择+回答):模型自判长内容→调 reply_text 把完整答案放参数(输出文本价),前端拦截(与 wait_for_user 同构)显示+落库+字幕+闭合 call **不 response.create**=零输出音频;tts 档答案照样代念。
- 53 **auto 独立开关**(用户否决全档兜底):🤖小按钮在四态旁——开=rtc-session 挂载 reply_text+注入判断规则/关=**工具根本不挂**(真禁用;工具表随会话建立→切换重拨生效);**四态+auto 持久化服务器** voice-config(rt_voice_mode/rt_auto_text),点按=localStorage 即时+POST 持久,初始 fetch 同步(服务器=真相源跨设备)。

**㊷ 检查表收尾**:单会话工具调用护栏(40 次提醒)+60min 上限预警(55min 知会,到点 _rtcDead 自动重连+摘要回放兜底)。官方 realtime-costs 页 WebFetch 复核=无新字段遗漏,§13 检查表 12/13 ✓(工具最小集一项=有意取舍:33 工具 schema 实测仅 3.5k tokens)。

## 54-58 RtcController P1-P2 + 插入页双bug + text腰斩 + TTS哑死(2026-07-12)

**54(㊺P1)骨架**:设计文档定稿 `references/rtc-controller-design.md`(5 步渐进/双通道分工防双执行/断线回退韧性);relay `handle_rtc_ctl`(?mode=rtc:sideband 挂载+事件镜像观察,P1 零动作);前端 rtcStart 连控制 WS(失败/断线=静默纯前端模式)。真机验证:journalctl 见 sideband 事件流全镜像。

**55 插入页双 bug**(#288,侦察实锤):①新建页刷新前 AI 看不见=currentPage 死区(中线落乐观插入页 `.pdf-upage` 时返回 null 冻结上一页)→fallback 读 `__upRec.page`;②笔迹串下一页=SSE 自回声(自己 POST 保存触发的广播,按页号命中未重编号的旧同名页)→`_ink.echo` 指纹 3s 抑制(pdf-tail 保存记账+SSE 守卫+_upInkPersist 同账)。

**56 text 模态腰斩修**:账本实锤 text 档多轮 out=512 满额(≈350 中文字被砍)→输出预算按模态分级:audio 轮 512(≈25s)/text 轮 2048,response 级 max_output_tokens;P1 镜像日志升级(done 带 audio/text 分模态 tokens)。

**57 TTS 代念哑死**(用户实测:日语页没念+之后全哑):根因链=朗读通道死(rtcStart _ttsShutdown/relay 重启)时 speak 静默丢弃=没声;且 __vcTtsBusy 含 _cap.pend 字幕句队列→卡死=麦禁满 2min(日志实锤 2分43秒空白)。修:speak 前保证通道(没 ready 先 _ttsEnsure+900ms 延迟发);禁麦恢复改看**真实播放**(_tts.playing:5s 没响=通道坏立即恢复+清 pend+提示/播完即恢复/60s 硬顶)。

**58(㊺P2)工具执行搬服务端**:relay 成为唯一工具执行者(sideband function_call_arguments.done→_tool→voice-tool→function_call_output+response.create 按 rt_voice_mode 模态化);**工具缓存** ck=name|args|页|笔迹指纹(voice-tool `cacheable` 白名单才存,命中重放 client_action=#287 的 read_page 重复调用提前落地);need_shot 截图往返(see_ink/see_page 视口截图只有前端能拍,Future+6s 超时);前端 ctl=true 时只放行 reply_text/wait_for_user,tool_status 下行置 turnTool(承诺核查放行)+see_* done 复位 inkDirty(边沿复位镜像),shim send 上行镜像 page/state/ink 给控制 WS。⚠坑:rtc_call_id 必须在 voice-tool **请求体顶层**(webapp 读 body,放 ctx=图像 sideband 注入静默失效)。P3(usage+注入)/P4(response.create+承诺核查)未动,前端对应逻辑仍在。

## 59 P2首测双执行修(版本握手)+转写升级(2026-07-12,用户截图:读取页面×2+TTS×2+转写错认)

**双执行根因**(webapp 日志同秒实锤):用户页面是部署前加载的**旧版 JS**(无 P2 分工)+新 relay——同一 function_call 前端(Safari UA)与 relay(python-httpx)各执行一遍 read_page(两张工具卡),双 response.create 撞 `conversation_already_has_active_response`(rtc-ctl err 实锤)。修:①**fe=2 版本握手**——前端控制 WS URL 声明 `fe=2`,relay 只对 fe≥2 接管工具,旧前端(不带参数)自动退回 P1 观察模式(日志区分「P2 已挂/P1 观察」);**教训:改变双端分工的升级必须 capability 声明,不能假设前端已刷新**。②撞车被拒的 create 记 pend,response.done 补发(否则那次工具结果永远无人回答)。TTS"念两次"=决策轮开场白+回答轮正文各念一遍(设计行为)+双执行放大,握手修后待重测。

**转写升级**:用户句转写错认("这一页讲了什么"→"毕业讲了什么",模型听音频本身理解对)根因=whisper 无语境音近错认+模型名 `gpt-realtime-whisper` 非官方名。修=`gpt-4o-mini-transcribe`(官方,中文 WER 低于 whisper)+**prompt 语境**(书名+高频指令词:这一页/翻到第N页/做卡片…——与豆包 ASR 热词㉓同思路,官方 transcription.prompt 字段)+language 跟随 rt_lang(仅 zh/ja/en 合法值)。**转写费用**(独立账单,usage 在 `conversation.item.input_audio_transcription.completed` 事件,不进 response.done;relay 已加日志观察):mini-transcribe 音频 $3/M tokens≈**$0.002/分钟纯说话**,一次通话几美分零头,不值得为省它引入 Apple 听写(通话中麦被 WebRTC 占用,iOS 并行 SpeechRecognition 抢麦+录音路由副作用=听写互斥血泪重演)。

## 60 审核二轮落地:视觉链路真修+turn epoch+shot_id+截图上限(2026-07-12)

外部审核二轮(拿 58b 版代码)揪出 **P0×2 全部成立**,并暴露 58 的一个实施事故:

**P0#1 视觉链路实际是断的**(58 的"修复"改错了地方):python `replace(old,new,1)` 匹配到**两个同形代码块的第一个**——rtc_call_id 被错加到 GPT-WS `_tool`(那里 call_id 是**函数调用 ID**非 WebRTC 会话 ID),真正要改的 P2 `handle_rtc_ctl._tool` 没改到且 `res.pop("_vision")` 无条件丢图=webapp 不注入+relay 丢弃,模型拿不到截图。修(60):①撤 WS 版错误参数;②P2 改用**自己已持有的持久 sideband(ows)直喂 input_image**(与 WS 版 ows 直喂同构,不传 rtc_call_id、不让 webapp 开第二条临时连接);③webapp `_rtc_sideband_images`(仍服务前端 fallback)等每张 created 再关(旧版第一张确认就返回)。⚠**教训:手写 python replace 改重复代码没有 Edit 工具的唯一性保护,同形代码必先 count**。

**P0#2 抢话竞态**:慢工具跑着用户开新话轮→旧工具完成时无条件回填+response.create=旧结果混新问题/答完新的又答旧的。修=**turn epoch**:speech_started/打字=epoch+1;_tool 捕获发起纪元,完成时纪元已变→只读工具换"(结果已过期,如需请重调)"回填、写工具回填真实结果,**都不 create**(旧结果不抢话);vis 过期丢弃;撞车补发也带纪元检查。"显示与执行无共享状态"的旧判断不成立(共享 conversation/活动 response/turn 边界)——response.create 全归 relay 的完整仲裁=P4。

**P1 批**:①shot_fut 改 **shot_id 配对**(旧版单槽,两轮工具重叠=Future 被覆盖/迟到截图错配;无 id 回退取唯一 pending 兼容 59 前端);②截图**尺寸上限**(长边≤1600px+质量阶梯 0.8/0.6/0.45 至 base64≤900KB;relay serve max_size 2→8MiB 保险层——旧状态 2×DPR 整视口无上限,复杂页超限断的是整条控制 WS);③缓存键强化:sel 全文 md5(80 字前缀碰撞)+ink 全笔画 md5("笔画数+末点"易撞);**see_*/web_search/search_image/search_video 退出缓存**(viewport 无 revision/时变数据);**写工具成功→tool_cache.clear()**(revision 体系的保守替身,治 notes/highlights/vocab 写后读旧);④reply_text 截断**前端抢救**(正则捞 JSON 未闭合的 text 已生成部分+提示,治标;治本=route_to_text 薄参数+服务端文本模型,task#289)。

**审核采纳的排期**:#284 SQLite 账本**提前**到 P3 前(JSON 账本无锁 read-modify-write 且浏览器上报不可为硬闸权威);P3=usage+注入归 relay;P4=create/cancel 全归 relay(响应仲裁终态);#285 压缩;控制 WS 可恢复重连(task#290,当前"断线保持前端模式"比朴素重连安全)。

## 61 输出体系定稿:四态重构+TTS 通用开关+route_to_text(2026-07-12,用户设计)

**用户拍板终版**(讨论结论:512 冲突只存在于音频档——stt/tts 本来就是 2048 文本随便写,不需要任何路由):
- **模式按钮四态**(循环,键 rt_voice_mode 新值域):`sts` 纯语音(512 音频硬顶)/ `stt` 纯文字(2048,无路由无限制)/ `half` 混合(提问=语音·工具/深度=文字)/ **`route` 智能路由**(语音短答 512 + 长内容模型自调 route_to_text 转服务端文本模型写全文)。旧值迁移 audio→sts/mixed→half/text→stt/**tts→stt+TTS 开**(前后端 _norm_vm/_VM_OLD 双侧归一,读到旧值一次性写回服务器)。
- **TTS 退出模式行列,改独立通用开关**(📢 与模式按钮相邻,键 rt_tts_speak):任何模式的**文字输出**(stt 回复/half 工具轮/route 长文/reply_text 兼容)都用豆包朗读通道**流式切句代念**——`_mkTtsFeeder` 每个文字流一个 feeder,增量文本按句边界(。!?\n;…)切片即念,**尽快开口不等全文**;`_speakSafe`(57 通道韧性)+`_ttsMicGuard` 麦守护单例(流式多段 speak 刷新活动时间不重启;fin=播完+3拍静默+1.5s 无新句,dead=6s 没响,hard=180s)。用户抢话(speech_started)bargeIn 打断残播。豆包引擎原生出声不受此开关影响(stt=丢音频静音,其余=播)。
- **route_to_text**(审核 route 方案+程序门控):工具**恒挂载**+instructions 恒定说明(§8.1 缓存友好),实际放行由 relay `_tool` 按**当前** rt_voice_mode 判定——非 route 档调用被驳回"请口头简答"(**模式按钮通话中热切立即生效**,不像 53 auto 开关要重拨);route 档→`_oa_route` 调 webapp `/api/assistant/route-text`(Gemini flash `_gemini_stream` SSE,系统 prompt=文字详答引擎+页文本 3.5k+用户原话 last_q+intent),delta 边收边经控制 WS `route_text` 事件下行(前端显示进侧栏气泡+字幕+TTS 开则流式代念),完成**摘要回填**(全文前 240 字进 function_call_output=模型知道自己"说"了什么防追问失忆)+**不 response.create**(零输出音频)。fallback 路径(ctl=false)前端 fetch 同端点同语义。reply_text 退役(rtc-session 不再挂,前端拦截分支留兼容)。
- **UI**:TTS 开关紧跟模式按钮注入(用户要求相邻);快捷栏整排紧凑(`#asst-quick .rc-media-tg{padding:4px 7px;font-size:12px;gap:3px}`);🤖auto 按钮删除。
- 冒烟:rtc-session 工具表(route_to_text 在/reply_text 无/规则段在)+voice-config rt_tts_speak 读写+route-text 真实流式(5 delta 块)全过。

## 62 OpenAI搜索双源+制卡带对话现场+deep三后端+route掐断兜底(2026-07-12,用户三需求+实测反馈)

**①网页/图片搜索主路换 OpenAI**(用户需求,替代待启用的 GCP CSE):`image_search.openai_web(query)`=Responses API 内建 `web_search`(gpt-4.1-mini,综合回答+url_citation 来源;计费=每次固定 8k input tokens 块≈$0.004,**无每日次数额度**);`search_openai_img(query)`=web_search 找图片直链(限 .jpg/.png+HEAD 验 content-type image/*,偏好 wikimedia/museum)。`_t_web_search` 主路 openai_web→CSE 兜底;配图落阶链 Commons→Google(未启用/403)→**OpenAI 第二腿**。key 复用 `~/.config/openai-realtime.json`。冒烟真调:调理师试验日期联网答对+来源。

**②制卡/笔记带对话现场**(用户需求:"搜过网页和图片后要求制卡,卡片要能用上"):此前制卡 AI 只拿 text 种子+出处链接——`_card_extra(ctx)` 汇集 **recent_tools**(relay P2/前端 fallback 各自记最近 6 条工具结果环:label+rag 600 字+images URL)+服务端对话历史近 6 轮 → params.extra_ctx 经 `voice._content_for` 拼进制卡素材("与主题相关就采用,无关忽略");**图**:模型没显式传 image_url 且对话里恰有一张配图→默认进卡(下载存 Anki 媒体库),多张→列 URL 给制卡 AI 参考。承诺核查代执行的 ctx 同带。

**③deep 三后端放开**(用户 bug 报告"深度思考只能选 Claude"):根因=chat 管线 `force_model in _CLAUDE_VARIANTS` 白名单(㉕遗留)——chat 加 `force_backend`(claude/gemini/codex,_agent_run 直接分派对应 runner,force_model 白名单按后端放行);relay deep pref 消费不再只认 claude(`body["force_backend"]=pref["backend"]`);catalog `backends_by_action.deep=["claude","gemini","codex"]`(面板自动跟随)。

**④route 档掐断自动升级文字**(用户实测:route 档文字语音对不上+卡壳;日志实锤=模型没调 route_to_text 而是 read_page 后**口头念页面**,`out=512 status=incomplete` 硬掐):prompt 管不住 → **程序判断**:relay response.done 见 `status=incomplete` 且当前 route 档 → 自动 `_route_rescue`(用户问题+被掐半截当 intent→_oa_route 生成完整文字详答显示+TTS 可代念)+system 注入"你被截断了,全文已显示,别重复"(纪元没变才注入);防重入 busy 标志。**模型自调=快路,掐断兜底=慢路**——route 档从此不依赖模型自觉。_route_line 同步强化"工具结果轮要转述大段内容也先调 route_to_text"。

## 63 输出预算按档分级 v2(2026-07-12,用户"512是不是太小了")

㊹ 的 512 一刀切(≈25s)是没有模态体系时的唯一手段;账本实锤平均响应才 14.7s=多数轮次到不了顶,**被掐的是长尾轮=体验最差的时刻**,上限翻倍只影响长尾、平均成本几乎不变。分档:**sts/half=1024**(≈50s;纯语音掐断无兜底=硬事故,日常时长仍由 instructions 8s/20s 规则管,上限只当保险丝)/**route=512 故意保持**(掐断=62 自动转文字详答的触发器,提高反而迟钝)/text=2048。session 级 max_output_tokens 512→1024(纯兜底,每个 response.create 都带显式分档值)。

## 64 路由体验定稿:等待语+专属视觉+全档2048撤硬兜底+Gemini搜索首选(2026-07-12,用户拍板)

**用户裁定**:不喜欢硬截断("需要处理的问题太多且省不了多少钱,大部分情况做对就行,之后按记录调 prompt")。
- **全档 2048**(≈100s 音频,正常轮永远碰不到=纯保险丝):前端/relay/session 级三处;63 的分档预算(1024/512)与 **62 的掐断硬兜底(route_rescue)全部撤除**——incomplete 只落 `_vlog("truncated")` 记录当分析素材(哪轮该走 route 却口头念了,供之后调 prompt/工具描述提高快路命中率)。
- **route 等待语**(用户设计):工具描述+_route_line 明确"调用的**同一轮先口头说一句等待语**(按话题自然措辞,如『说来话长,我写给你稍等』),说完就调,绝不口头讲解内容本身"——利用 audio 轮可同时输出音频+function_call 的特性,消灭"沉默调工具"的怪异感。
- **路由专属视觉**:工具卡 running=「🧠 文字详答生成中」/done=「🧠 文字详答」(relay 快路+前端 fallback 双侧);**字幕**:route 长文滚动时 cur 行加 `.vc-cap-route`(紫左边框+🧠 前缀+淡紫字),response.created 时摘除不残留;字幕状态行经既有 tool_status 机制自动显示 🧠 label。
- **网页搜索首选换 Gemini google_search grounding**(用户指出免费额度,核实属实:**3.x 系每月 5000 次免费**、之后 $14/1k;2.x 时代=1500 次/天 $35/1k):`_gemini_websearch`(generateContent+tools:[{google_search:{}}],免费 key 优先/403冷却同 _gemini_text,来源=groundingMetadata.groundingChunks.web);`_t_web_search` 落阶=**gemini(免费)→openai_web($0.004)→CSE**。冒烟真调:调理师实技试验答对+grounding 来源。⚠grounding 来源 URL 是 vertexaisearch redirect 链(正常,点击可达真页)。

## 65 语音 UI 批次:按钮 Apple 化+字幕美化+文字卡片堆叠浮层(2026-07-12,用户设计)

- **新按钮 Apple 化**:模式四态+TTS 开关的 emoji 全换 **SF 风线条 SVG**(`_VMI` 表:sts=声波竖线/stt=对话气泡/half=半波半行/route=分叉箭头/spk=喇叭波,currentColor 跟随亮灭)+短中文标签;`_VM_TXT` 供状态行纯文字(setSt 不能吃 HTML);工具卡 label 去 emoji(「路由详答·生成中」/「路由详答」,relay+前端双侧)。
- **字幕**:底子本就是 Apple TV 磨砂胶囊(v3-⑳);route 样式去 🧠 emoji 改克制版=内嵌紫光条(inset box-shadow)+微紫底+淡紫字。
- **文字卡片堆叠浮层**(用户设计,route/stt 等文字回复的侧栏关闭出口):`_cardPush(text,label)`——右下固定锚 `#vc-cards`,**按时间层叠**(新卡在前,旧卡向左上交错 9/13px+缩小 3.5%,只露 3 张,上限 4);半透明磨砂(blur24+saturate1.6+0.5px 白边+16px 圆角);每张头部=类型标签+**关闭×**(圆形毛玻璃钮);**自动消失**=设置卡新开关「文字卡自动消失」(默认开)+5-60s 滑条(localStorage rc-voice-card-hide/rc-voice-card-secs,设备级同字幕先例);**碰卡=取消该卡计时**(在读不收);侧栏开着不弹(内容已在对话流)。触发四点:route done(relay 下行+fallback)/文字轮 done/reply_text 兼容。

## 66 通话打字直达+通话条撤除+语音历史回放(2026-07-12,用户四需求)

**①2.1 通话中文字输入**:能力早在(㊿ 手动挡时 _rtcHandleUp 'text' 分支:flush ctx+item.create+RespCreate),但 grep 实锤**没有任何发送方**=死路。接线:`window.__vcSendText(text)`(rtc 通话中→用户气泡 __asstVoiceMsg('u')+字幕 capUser+ws.send({type:'text'}) 走 shim);rc-assistant `send()` 开头拦截(消费成功=清输入框 #asst-ta+return,不走文字助手管线);**输入框紫光** `#asst-input.vc-live`(rtcStart 加/teardown 摘)=可视化"现在打字直达 2.1"。
**②通话条残留版面撤除**(用户裁定):`#rc-vc.vc-inline{display:none!important}`——输入框上方内嵌通话条不再显示(状态看按钮呼吸/字幕,对话在侧栏流;setSt 等写入逻辑不动,零风险)。
**③通话语音按轮录制**:pc.ontrack 存 `_rtc.remoteStream`→MediaRecorder(mime 探测 mp4/webm;Safari=AAC-mp4)——音频轮 response.created 开录(文字轮不录)/done `_recFinish()` **先拿 clipId 立即落库、blob 在 onstop 异步上传**(POST /api/assistant/voice-clip?id=,≤8MB,每用户保留 400 段按 mtime 清);打断的半截轮也收;历史消息 `clip` 字段(_convo_append 白名单+log 端点只挂 assistant 侧)。
**④历史语音回放按钮**:loadHistory 每条 AI 气泡尾加圆形播放钮——**有 clip=紫**(new Audio 播 GET /voice-clip/<id>,再点停,单例互斥)/**无 clip=灰**(点击=`__vcTtsCapture`:朗读通道现场念+`_tts.tap` MediaStreamDestination 抽头 MediaRecorder 同步录→上传→**clip-attach 回写历史**(ts+内容前缀定位)→按钮变紫);播放 404(当时 blob 没传成)自动降级灰流程。⚠EPUB 的 HOST.voiceLog 路径暂不带 clip(epub-convo 结构不同,灰钮 TTS 现场念可用,回写不通)。冒烟:上传/下载/落库/补挂全链路通过。

### 66b route 快路命中率首份日志分析(2026-07-12 用户实测)

三通 route 档电话实锤:管线全通(P2 在/read_page relay 执行/66 语音录制 5 段全传成功),但**模型零次调 route_to_text**——read_page 后口头念整页 60s(1185 audio tokens,2048 内没被掐所以"看似正常")。结论:**instructions 顶部规则对工具回填轮命中率 0**。修=提醒放离决策最近处(just-in-time):route 档+只读工具+结果>800 字 → relay 在 function_call_output **尾部就地追加**"内容较长请调 route_to_text,口头只概括两三句"。非硬兜底(不掐不代打),纯 prompt 位置优化;后续按 voice-log 持续观察命中率。

### 66c route 工具轮改程序模态分流(2026-07-12,0/4 定论)

66b 就地提醒也没管住(第 4 通 read_page 后又念 50s/966 audio tokens)——**定论:「拿到资料+音频模态」=mini 条件反射念,prompt 任何位置都治不了**。换程序判断(用户哲学):**工具结果长度是回填时刻程序已知的事实**——route 档工具回填轮 `_resp_create(long_tool)`:结果<800 字=audio(口头说,保留短答体验)/≥800 字=**text 模态让模型自己写**(无截断、无 Gemini 双引擎文风、route_to_text 留给不调工具的长答场景);前端 fallback `_rtcRespCreate(src, longTool)` 同构;就地提醒文案改为文字轮引导。**顺修既有 bug**:relay 发的工具轮 create 前端不经过 → `_rtc.turnText` 停留在上一轮的值=half/route 的文字工具轮 TTS 代念+文字卡片全断——改由 **delta 事件类型驱动**(output_text.delta=文字轮/audio_transcript.delta=音频轮,以实际到达的事件为准)。

### 67 route 三问题批(2026-07-12 用户截图:叠音/两句等待语/md 没渲染)

20:51 日志钉死链条:决策轮(音频等待语①+read_page)→66c text 工具轮:模型写等待语②后**又转手调 route_to_text**(instructions 的路由规则和 66b 提示叠加)→Gemini 长文=**三段式冗余**+双引擎。修:①relay 记 `turn.text`(最近下发模态),**文字轮里调 route_to_text=程序驳回**"你就在文字轮,直接写正文";66b 提示文案加"不要写过渡句/不要再调工具"。②**TTS 叠音**:2.1 等待语还在 WebRTC 播放队列时 text 轮 delta 已到即开念——加 `_rtc.aStart/aEnd`(音频轮转写字数≈5.5字/秒+800ms 缓冲估播放结束),`_speakSafe` 按 aEnd 延迟开念(禁麦也延到真开念,等待期用户仍可抢话);打断清零。③**Markdown 渲染**:语音气泡 __asstVoiceMsg('a') 一直是 textContent(md 源码裸奔)——加 `{md:true}` 终态渲染(文字轮 done/route done 用 renderMd,流式期间纯文本省性能);renderMd 导出 RC.assistant 供**文字卡片**同渲染。

### 68 read_page 参数别名(2026-07-12 用户截图"翻页后还读上一页")

21:05 日志实锤:模型传 `{"pages":[9]}` 被静默忽略(实现只认单数 `page`)→回退 ctx 旧页码=goto_page 都成功了还读到第 8 页;21:06 模型自己试出 `{"page":9}` 才对。**根因:工具返回结构里是复数 `"pages":[N]`,模型照着返回学传参**,而宽松 schema(additionalProperties)不校验=静默吞。修:`_t_read_page` 接受 pages 数组/标量别名(单数优先);冒烟三形态全过。**教训:工具的参数名和返回字段名不一致=模型必然踩;宽松 schema 下所有合理别名都要收。**

### 69 文字卡拖动/置顶+工具可视化升级(2026-07-12 用户三需求)

①**卡片拖动**:按住头部(vc-card-hd,cursor:grab/touch-action:none)pointer 拖——拖超 6px=`c.free` 脱离堆叠自由停放(_cardLayout 只排非 free 卡);②**点击置顶**:任意 pointerdown → `_cards.topZ++` 抬 z-index;③**工具可视化**:`_toolIcon(name)` 按类别映射 8 个 SF 线条 SVG(read/search/eye/write/nav/route/dict/net/gear)——字幕状态行升级(capStatus 支持 {html,cls,hold} 对象:**running=图标+label+小转圈,done=图标+绿✓停留 2.5s,error=红⚠停留 4s**;旧行为 done 立即清=侧栏关着的用户什么都看不见的根因)+对话流工具卡 label 前加同款图标。

## 70 结构化结果卡+双击入上下文(2026-07-12,用户设计)

**结构化结果路由**(用户设计,与 route 同哲学:对 2.1 只是"工具调用+知道完成了"):无需额外小模型——`_gemini_websearch` 的**同一次** grounding 调用直接输出 `{kind: weather|news|fact|general, title, data(按类型 schema), brief}`;`_t_web_search` 结构化成功→卡片经 client_action `renderInfoCard` 显示,**给 2.1 的回填只有 brief**("已显示,口头只说一句概况,不要念细节"——卡片数据在 client_action 里被 pop,不进 RAG);parse 失败→回退纯文本旧行为。前端 `renderInfo`:按 kind 渲 Apple 风卡(天气=大字温度+降水+tip/新闻=条目列表+来源/事实=结论+补充+sources 链接)——**侧栏开=进对话流,关(字幕模式)=磨砂浮层卡**;search_image 侧栏关时也弹浮层图卡。**70b 关键修**:Gemini flash 的 **thinking 不关会泄漏 thought parts 进输出**("Wait, the prompt says…"内心戏实锤)且思考 tokens 吃掉输出预算致正文截断,JSON 守规率 1/3→加 `thinkingConfig:{thinkingBudget:0}`+parts 过滤 `thought` 标记→**4/4**(fact/news/weather)。

**双击入上下文**(用户设计"我们已有向 AI 灌文字和图的渠道"):信息卡/浮层卡/图卡 `_pinBind`——**双击=选中**(紫描边 vc-picked)+`_rtcSys` 注入"用户把「X」带入对话,请参考:内容"(2.1 通话中才生效);**再双击=移出**+注入"已移除不必参考";**选中的浮层卡不自动消失**(自动消失计时豁免+数量裁剪豁免,即用户说的"选中后气泡的计时暂停到取消为止")。

### 72 浮层卡交互重做(2026-07-12 用户设计:磁吸拖动/双击收展/长按选中)

**拖动粘滞/闪烁根因**=堆叠布局的 `transform .38s` 过渡在拖动中每帧追赶——**跟手期必须 transition:none**。新物理感:阈值 6px 内粘住不动→拽过瞬间「弹起」(.vc-lift:scale1.03+深阴影+grabbing)→零动画跟手→松手**落定回弹**(overshoot 曲线 cubic-bezier(.34,1.56,.64,1) 像重新粘回)。**双击**改=收起/展开(收起态 .vc-min:头部+一行正文摘要 42 字,仍可拖/关/长按)。**选中带入上下文改长按 600ms**(pointerdown 计时,move>8px 或抬手取消,到点亮度脉冲 pop+紫框 toggle;⚠pop 动画不能用 transform——会覆盖拖动的内联 translate 致瞬移)。

### 74 重复工具短路+搜索静默入库(2026-07-12 用户两设计)

**①重复类工具程序短路**(用户提议"不给会重复的工具入口"):⚠不从工具表摘(动态改 tools=前缀变=缓存全灭,㊶教训)——**程序短路等效且缓存无伤**:relay/前端 fallback 收到 `read_selection` 且选中在手(state 通道早就有)→零调用直接回填"内容就在这里:「sel」直接使用"。**②搜索静默入库**(用户设计"结果屏幕视觉确认,AI 本轮零输出,知识下一轮才用"):web_search 结构化分支标 `silent:true`→relay/前端回填后 **no_create**(与 route_to_text 成功同构)——模型沉默,知识在上下文待命,用户下轮开口时自然运用;note 改"本轮到此结束,下次相关时直接运用";视觉闭环=卡片+工具状态行绿✓(69)。成本备注:让它说一句"搜完了"其实只要 ~$0.001(cached 输入+2s 音频),但实测 mini 管不住嘴会念全文,静默是更稳的形态。

### 75 read_selection 永久摘除+搜索完成音+长按修复(2026-07-12 用户三点)

①**read_selection 从 rtc 工具表永久删除**(用户裁定,74 的"程序短路"升级为不挂):选中恒经 state 通道注入=工具是纯重复入口;**永久删除≠动态增删**——工具表每次会话恒定一致,前缀缓存无伤;长选中措辞改引导 read_page 当前页;74 的 relay 短路分支保留当兜底。仅 rtc 链路摘(豆包/侧栏 agent 不动)。②**搜索完成提示音**:silent 静默入库时 renderInfo 弹卡念一声"搜索完成"(_speakSafe 走朗读通道,队列/禁麦守护全复用;条件=通话中且有语音输出形态:非 stt 档或代念开)。③**长按选中失效双因修复**:pointermove 取消阈值 8px→14px(**手指按住 600ms 必然微抖超 8px=长按永远触发不了的根因**)+iOS 长按系统文本菜单抢手势(.vc-pinnable 补 -webkit-touch-callout:none + contextmenu preventDefault)。

### 76 搜图失败三层修(2026-07-12 用户"富士山照片失败")

**根因**(日志实锤,非解析问题):模型 query="日本富士山 **真实照片**"——修饰词让 Commons 名称索引全灭(㉞e 教训重演,中文修饰词裁短逻辑治不了);当时 Google 403(仍未启用)+OpenAI 找直链兜底实测归零→found:0。修:①**程序剥修饰尾**(真实照片/图片/写真/photo… 正则剪,复现词即中);②**Gemini 免费档规范化**(用户"wiki 关键词查询"方案):全落阶没中→_gemini_text(think=False,80 tokens,纯知识不占 grounding 5000 额度)输出 Commons 规范名(英文+原语言)重搜;③**空结果不缓存**(源抖动/坏词失败不钉 1 天)。**关于"用 5000 免费额度直接搜图"**:实测 grounding 无图片模式(文本+网页引用),让模型提取直链不可靠(OpenAI 同思路归零)——免费额度用在规范化关键词上,Commons 直链天然真实。⚠已知质量边界:Commons"日本富士山"首图=E-2C 预警机飞越富士山(相关度排序),主体非山——不满意的话下一步把规范化提前到首搜前。

## 77 卡片体系大批次(2026-07-12,用户八项设计)

**a. pin 状态化**(修用户问的"反复选中/取消会怎样"):选中集合 `_pins` 为唯一真相(卡紫框=视图)——注入改**覆盖式快照**(防抖 1.2s+集合指纹):反复折腾最终状态没变=零注入;变了=一条"当前带入清单(以本条为准,旧声明全部作废)"——历史不膨胀、隔时间重选=一条新快照,语义永远无歧义。label 唯一化(同名卡加 ·2)。
**b. 侧栏联动**:开侧栏=浮层卡**全部消失**(display:none 不挡内容);**选中的**变**输入框上方竖排 chip**(复用 asst-fig-chip 风格+紫调,label+摘要+✕=取消带入);关侧栏=浮层卡回来。选中的卡被 ✕ 关闭=同时解除带入(防幽灵参考)。
**c. dock 收藏夹**(用户设计):拖卡接近右下角=**边缘紫色径向渐变光晕**提示→松手收入;收藏卡**无自动消失计时**;右下托盘钮(数量徽标)开磨砂面板:折叠条目可**长按选中**(同一状态中心)/**拖出 60px=变回浮层卡**(选中状态跟随)/✕删。
**d. 感叹号详情**(`__asstInfoBtn`,rc-assistant 导出):语音文字轮/路由长文/搜索卡右下角小「!」——点开 popover:输出形态/本段工具/类型+「⚙ 调整各环节 AI 模型」直通 openModelSettings(现成全局入口);中间步骤指引到对话流工具卡。
**e. TTS 按钮白方块**:spk SVG 加显式 width/height 属性+简化 path(不赌外部 CSS 加载序)。
**f. img_norm 设置项**(用户指正"规范化不应默认某模型"):_AP_ACTIONS 加 `img_norm`(「配图关键词规范化」,默认 gemini flash,面板可换型号;backends 目前只放 gemini=诚实选项);_one 规范化经 _resolve 按用户设置选型号。
**g. OpenAI 图搜核实**:**无独立图片搜索 API**(Responses 内建工具=web/file/computer;图像能力=生成 gpt-image-2+视觉理解)——搜图维持 Commons 直链主路+规范化。

### 78 卡片收藏夹持久化+☆收藏钮(2026-07-12 用户设计"卡片独立于会话存在")

**服务端持久化** `/api/assistant/voice-cards`(GET/add/del,`state/voice-cards/<uid>.json` 上限 200)——**独立文件=清空对话天然不清收藏**(冒烟验证隔离);卡片带**元数据** meta{file 书名, page 页, q 触发问题}(长回答离开会话也能自释语境)。前端:dock 全面接服务端(开页预载徽标/面板 GET/✕=del/拖入浮层=add);**☆ 收藏钮**(`__vcFavBtn`,「!」左侧)挂三处=侧栏信息卡/文字回复气泡/路由长文气泡(闭包快照防串轮);dock 面板条目显示元数据行(书·p页·「问题…」);**拖出改复制**(收藏是长期库,✕才删)。

### 79 路由长文统一"卡片+简介"逻辑(2026-07-12 用户设计)

路由回答与天气/新闻卡完全同构:**生成引擎在正文末尾附 `[[BRIEF]]` 一句概括**(route-text SSE 流式外发正文、BRIEF 段拦下不外流——尾部 hold-back 12 字防标记被 chunk 切两半;done.summary=真简介,没写则前 240 兜底);**回填给 2.1 的只有简介**("详答已显示,本轮结束,简介:X;想看全文用户会长按带入")=静默入库;**长按卡片=全文入脑**(_pinToggle 上限 700→2500;route 浮层卡/对话流气泡[voiceMsg opts.pin 经 __vcPinBind]/文字回复气泡全绑);fallback 路径同款(done summary 解析)。

### 80 播放钮重做+收藏夹时间线抽屉(2026-07-12 用户设计,授权自由发挥)

**①历史播放钮白块修**:SVG 在用户环境渲成白块→**纯字符方案**(仿「!」钮实现:▶/播放中◼)+全套状态动效——播放=紫呼吸闪光(vcClipBreath)/无录音=灰/生成录音中=琥珀快呼吸/按压=scale(.82) 特效。**②收藏夹重做**:收入区扩为**屏幕底边整条**(拖到最下方即收入,光晕改整条底边线性渐变);面板改**底部 26vh 时间线抽屉**——横向滚动、按加入时间排序、**天节点=轴上单线条+日期**、每卡上方竖线+具体时刻(用户设计元素);无按钮交互:**向上拖出 70px=复制**浮层卡(收藏保留);**删除模式**(「选择」开关):点卡=红✕角标多选→「删除所选」批量软删;删除模式下露**回收站**入口——服务端软删(deleted 时间戳,**1 天后清理**),回收站视图点卡=恢复。冒烟:批量软删→回收站→恢复全通。

### 83 收藏夹封面流+拖出修+TTS念钮+头部拖把手(2026-07-12 用户四点)

①**面板拖出失效根因**=横滚容器把竖直手势当滚动取消 pointer 流→卡片 `touch-action:pan-x`(横滑归滚动/竖滑归 JS)+setPointerCapture+阈值 50。②**封面流**(用户"圆筒"设计):scroll-snap center 停靠+滚动监听按距视口中心分 3 档改 **width**(330/180/112px)——内容随宽度自适应"变小变少"(line-clamp 9/3/1 行+远档隐 meta),非几何缩放;面板高度改随卡自适应(max 46vh)。③**长文卡 TTS 念钮**:浮层卡头部+侧栏气泡尾部 ▶(点=__vcSpeakText 现场念/再点=停,紫呼吸);**长文气泡☆撤**(用户裁定,浮层 fav 参数移除)。④**侧栏卡拖拽收藏改头部把手**(300ms 拎起实测与滚动矛盾):vc-if-hd 即时拖(touch-action:none 只在头部,不碍对话流滚动)+ghost 跟手+底边收入。

## 批次 88-90(2026-07-13)图片卡统一 + 工具回报开关 + 设置 Tab 化

**88 图片卡升格结构化卡**(用户:"所有的方块都要可以同时显示而且样式是一模一样的"):
- `renderImgs` 重构:不再走 threadMsg 图卡+浮层图卡两套旧路径,构 `{kind:'images', title:'配图 × N', data:{items:[{url,title,page,src,q}]}}` 直接调 `renderInfo`——对话流恒插卡+字幕模式浮层镜像+落库(log{card})+刷新回放,全部自动继承,零新管线。
- `_infoHtml`/`_infoText` 加 images 分支(图网格);`_igWire(root,card)` 事件委托:每图右上 ✕=从卡中移除;**点图=单选**(紫框 `vc-picked`,该图标题+链接进 `_pins` 带入上下文;点另一张自动切换;再点取消)。侧栏卡(`_infoCardEl`)与浮层卡(`_cardPush` 后)都绑=两模式共通。
- 溯源:webapp `_one` 记 `hitq`(实际命中词)+返回 `source`/`matched_query`;「!」详情加**搜索链路**行。

**89 工具回报开关 + 设置 Tab 化**(用户:配图完成后模型没静默;设置内容太多):
- 配图与搜索同构静默:voice-tool 后处理对 search_image 注入 `silent:true` + 静默 note(卡片已显示,本轮不发言)。**只在语音链路注入**,不碰文字助手的 markdown 插图语义。
- 新设置 `rt_tool_reply`(GPT 组 checkbox「工具完成后口头回报」,默认关):relay `silent and not rt_tool_reply → no_create`;前端 fallback 读 `localStorage rc-voice-toolreply`。开=展示型工具结果放行模型自由回答。
- 模型设置面板 Tab 化:`ams-tabs` 两 Tab——「阅读 AI 任务」(预设条+各环节模型)/「语音通话 · 朗读」(`_renderVoiceCfg` 懒加载进 tab2,点开才渲染,省首屏)。最小侵入:`container = pane1` 重绑,下方既有渲染代码原样进 tab1。

**90 回归修正**(用户截图):
- **☆收藏按钮删除**:87 给信息卡加的 `__vcFavBtn` 违反既定设计(85 已定:收藏唯一入口=拖动标题把手进底部收藏区),`_infoCardEl` 中调用删除。
- **链路问号根因**:88 只改了 `_one` 的返回,组装最终 `images` 列表时仍只挑 concept/image_url/page_url 三字段,source/matched_query 被丢——列表补齐两字段;前端源名映射可读(`commons→维基共享(Commons)`/`google→Google 图搜`/`openai→OpenAI 搜索`),空值显示「未记录(旧卡片)」,绝不显示问号。

## 批次 91(2026-07-13)感叹号⚙=环节直改面板

用户:"感叹号的调整各环节模型按钮毫无意义,不应该只是打开模型设置的快捷键,而是把所有跟这个环节相关的 AI 调用设置单独拿出来放在一个版面里直接修改。"

- **前端 `openActionSettings(actions, opts)`**(rc-assistant,导出 `__asstActionSettings`):迷你浮层复用 `_buildMsTask`(action-prefs 同一套行 UI/端点,改即保存全设备生效),只渲染传入环节;`opts.note` 说明行;`opts.voiceTab`=「🎙 打开语音 Tab」按钮(openModelSettings 后轮询点 tab2——renderModelSettings 是异步渲染,tabbar 晚到)。`__asstInfoBtn` ⚙:有 `info.actions` 走直改面板,否则退回总设置。
- **环节映射**:语音文字回复气泡=['deep']+voiceTab+note(主模型=GPT Realtime 在语音 Tab);路由详答=['route_text']+voiceTab;搜索卡 images=['img_norm'],weather/news/fact/general=['web_search']。
- **web_search / route_text 纳入 action-prefs**(此前固定 flash 无设置项,"直接修改"无从谈起):`_AP_ACTIONS`/`_AP_DEFAULTS`/`_AP_LABELS`/`backends_by_action`(都只 gemini)+ `_gemini_websearch(model=)` 接 `_resolve("web_search", ctx._uid)`、route-text 端点 `_gemini_stream(model=)`+兜底 `_gemini_text(model=)` 接 `_resolve("route_text", session.user_id)`。深度下拉对这两环节无效(grounding 恒不思考),行照渲不接线。
- **总面板补全**:tab1 新组「联网与语音文字环节」= web_search/route_text/img_norm(**img_norm 此前在总面板根本不显示**,77 只做了感叹号 focus 直达,白名单漏加——顺手修)。

## 批次 92(2026-07-13)通话语速 + 拖动实卡 ghost + 拖出到字幕浮层

- **通话语速 `rt_speed`**:语音 Tab GPT 组新滑条(0.5-1.5×,step .05,badge 实时);session `audio.output.speed`(官方 0.25-1.5,session 级,下次通话生效)。⚠ 通用 range 保存是 `parseInt`——按 `step` 含小数点分流 `parseFloat`(否则 1.25 存成 1)。
- **音色位置**(用户问"在哪设置"):早已存在——语音 Tab GPT 组「GPT 音色」下拉(_RTV 10 音色,默认 marin),91 Tab 化后好找了;后端 `rt_voice`→session `audio.output.voice`。
- **拖动 ghost 看不见修复**:`_dragToDock` 开头 `injectCss()` 保险——`.vc-drag-ghost` 样式由 rc-voicecall `injectCss` 注入,通话 UI 从没初始化过时侧栏拖动的 ghost 是无样式裸 div(position:static 看不见=「没有卡片的图像」);另 ghost 加 `color/font-size` 兜底(clone 到 body 后侧栏后代选择器样式丢失)。
- **拖出到字幕浮层**(用户设计):侧栏卡拖到阅读器区(`_sideOpen()` 且放手 x < 侧栏左缘-30,且不在收藏 dock 区)→ `_cardPush(raw,label,isHtml,force=true)`(新 force 参数绕过「侧栏开不弹」guard;容器 `#vc-cards` 在侧栏开时本就 display:none=`_cardsVisSync`,所以**放过去当下不可见**,关侧栏自然浮现——与「开侧栏卡片全隐」既定设计自洽)+ `_placeFx` 放置特效(紫色小卡从放手点飞向浮层堆叠位缩小淡出)+ toast「已放入字幕浮层(关闭侧栏可见)」。

## 批次 93(2026-07-13)双 call 并存事故(问一句答两句+不查页面)

**用户截图**:清空对话后马上提问「このページは何を書いてありますか?」→ 两条回答(一条讲"ホログラム"戛然而止,一条裸模型口吻"見えている情報がない,把 PDF 图片发给我")且没调 read_page。

**取证(relay 日志钉死)**:两个 P2 ctl 连续挂上(`rtc_u7_E0qkb`/`rtc_u7_E0qke`)+ 用户同一句话被**转写两次**(00:51:38/00:51:41 两条转写 usage)= **两个 RTC call 同时活着,两个模型各听各答**。一条被 cancel(「ホログラム」幻听大概率来自音频在两连接间错位),另一条在清空后的空白语境里决策失误没调工具。

**根因面**:`rtcStart` 没有并发单飞锁——清空重拨(`teardown→_connect(async fetch)→rtcStart`)与任何迟到的自动重连(`_rtcDead` 800ms setTimeout 只查 `!_userHung && !_rtc.on`,不查 `_connecting`)可各建一个 call;舞步中 `_rtc.on` 尚未置 true 的窗口有数百 ms~秒级(getUserMedia+SDP fetch)。

**修复(三层防御)**:
1. `rtcStart` 开头单飞锁:`if (_rtc.on || _connecting) return`(console.warn 留痕)——任何来路的第二次拨号物理进不来;`rtcTeardown` 补 `_connecting=false`(不经 teardown() 的 _rtcDead 路径也解锁,防锁死)。
2. `_rtcDead` 800ms 重连 guard 补 `!_connecting`。
3. relay `_RTC_CTL_LIVE[uid]` 取证:同 uid 新 ctl 挂上时旧的还在→`⚠ 同 uid 双 call 并存` 告警日志(不踢——iPad+PC 多设备并存合法,只留证据供下次诊断)。
4. SP 加**页面内容铁律**:被问"这页写什么"而上下文无页文本→必须先 read_page;答"我看不到/把内容发给我"=错误行为(治裸模型式回答,与 see_ink 铁律同构)。

## 批次 94-95(2026-07-13)Grok 第三引擎 + 卡片稳定编号去重

**94 Grok Voice 接入**(实测先行,能力边界钉死后按现实接):
- 实测:①`output_modalities:["text"]` 被无视仍出声(audio.delta×4)→ **恒纯语音,四态不适用**;②`input_image` 被静默收下但模型无视觉(纯红图答"无法查看图片",首测幻觉"一只猫")→ 视觉走文字转述;③function calling ✓(`response.function_call_arguments.done` 实测);④文本输入 ✓;⑤事件流=OpenAI GA 形制(响应/音频/字幕事件全同名)。
- 接法:`handle_openai(engine="grok")` **复用整条 GPT-WS relay 管线**——`XAI_RT_URL`+`~/.config/xai-grok.json`+model=grok-voice-latest;sess 裁掉 OpenAI 特有字段(truncation/max_output_tokens/noise_reduction/transcription/semantic_vad→server_vad)防 session.update 整条被拒;voice=`rt_grok_voice`(默认 eve);reasoning.effort=low(grok-voice-think 默认 high 慢)。视觉双 gate:`_want_vision`/直喂分支 `engine != "grok"`(恒本地文字转述)。分发:`rt_engine in ("openai","grok")`。
- 前端:引擎下拉加「Grok Voice(WebSocket·耳机推荐)」+ grok 专属组(音色 eve/ara/rex/sal/leo + 能力边界说明行);webapp 白名单 +rt_grok_voice。
- 已知边界:用户句转写暂缺(xAI 转写配置格式未知,裁掉了 transcription;看日志有无自动转写事件再补);WS 半双工外放可能回声(建议耳机)。

**95 卡片稳定编号(用户设计:同内容禁止重复入上下文)**:
- 根因:`_pins` 键=label,同 label 自动 `·2` 后缀=同一张卡的浮层/侧栏/收藏夹拖出三种实例以不同键共存,内容注入两遍(用户截图实锤)。
- 方案:每张卡出生发稳定编号 `_mkCid()`,跟随所有形态:`renderInfo` 卡(`card.cid`,落库自然带上→历史回放同号;旧卡回放时补发)、浮层镜像 `_cardPush(…,cid)` 与侧栏 `_infoCardEl`(dataset.vcCid)**同号**、收藏入夹 `rec.cid`、拖出复制保留原号、图片单选=`卡号#图序`。
- `_pinToggle`:选中时查 `_pins.cids[cid]`——同号已在上下文→toast「这张卡已在上下文中」+已选实例 pop 特效提示,拒绝重复注入;取消时清号。label `·2` 唯一化保留(服务**不同**编号的同名卡,如两次天气卡)。

## 批次 97(2026-07-13)Grok 官方文档定稿 + pin 全模式 + 录音根修 + TTS 可停

**97a Grok 按官方文档修正**(docs.x.ai voice-agent 全参数表,存档 references/xai-grok-assessment.md):
- **voice 在 session 顶层**(94 放 audio.output.voice 无效=一直默认 eve);`reasoning.effort` 取值只有 high|none(94 的 low 是无效值)→ none;`audio.output.speed` 0.7-1.5 官方支持=rt_speed 接上。
- **转写开启**:`audio.input.transcription={"model":"grok-transcribe","language_hint":rt_lang}`——设了才发事件;事件名 `.updated`(**累积全文可修正**,非 OpenAI 的 .delta;官方未记载 .completed)。relay:updated→前端 is_interim 覆盖显示+暂存,response.done 时定稿(is_interim:false+落库)。
- 其它文档事实:会话上限 **120min**(30min 是 resumption 历史过期);文本输入 $0.004/**每个 item.create**(拉模式注入每条都计费!);turn_detection 只有 server_vad|null(threshold 默认 .85)+xAI 特色 idle_timeout_ms(久不说话反复主动开口);**26 音色**(5 内置+21 旗舰:luna/cosmo/lumen 教育系、ursa 温暖助手系……前端下拉暂只列 5 内置,可扩);force_message(硬编码 TTS 不过模型,"IS the turn"不要再 create)/replace 发音映射/resumption 断线恢复(enabled+conversation_id 重连,30min)=可用未接。

**97b pin 全模式化**(用户设计):`_pinBind._fire` 去掉"仅通话中"gate——文字模式长按卡片同样带入;chips 本来就无通话 gate(输入框上方,×取消)✓;`_pinSync` 的 2.1 注入仍只在通话中;文字管线=send 时 `sentCtx.pinned`(`__vcPins()` 导出)→ webapp `_ctx_block` 尾部拼「用户长按带入的卡片」段(no_book 路径也拼——带入内容与书本开关独立)。

**97c 录音只响一声根修**(ffprobe 实锤字节/时长匹配=文件本来就短,推翻元数据假设):录的是 WebRTC **实时**流,`_recFinish` 在 response.done(数据推完)立即 mr.stop——但音频还要播好几秒→只录到已播的开头,回答越长丢越多。修:mr.stop **延迟到 aEnd(估算播放结束)+600ms**(上限 90s);`_recAbort` 改 flush 语义(已定稿的 onstop 照常上传,插话/挂断不再静默丢最后一轮)。另:clip POST 落盘后 ffmpeg -c copy remux+faststart(fMP4 兜底)、GET 改 send_file(conditional=True)(Range 支持,iOS <audio> 对无 Range 源易异常)、存量已批量 remux。

**97d TTS 播放钮全状态可停**(用户设计):busy(生成中)再点=`__vcTtsStop`(bargeIn:清本地队列+作废 relay 侧合成)→ capture 检测无播放自然收尾→blob 过小丢弃不入库;playing 再点=停(已有)。

## 批次 98-100(2026-07-13)视频卡统一 + Grok 首战修复 + WebRTC 桥(relay 端)

**98 视频结构卡**(用户:"参照图片的搜索卡片"):`renderVids` 重构=`kind:'videos'` 走 renderInfo 全管线(对话流+浮层同款/✕/单选带入/▶播放钮独立不选中/落库回放/「!」溯源=两源搜索词+pick_video 环节直改);search_video 静默(silent,与配图/搜索统一——"给你找到6个视频请自行点击"式废话轮=没静默的产物);`_bubDecor` 弱化:普通回复的整行「AI 回答」标题条退役→右上角小 ⠿ 把手(拖收藏保留,长按带入在气泡本体)——治"2.1 的回复被当成卡片"的视觉混淆,**只有真正的结构卡才有标题条**。

**100 Grok 首战两 bug**(用户戴耳机实测截图):
- **goto_page 无限重试风暴**:宽松 schema(properties 空)下参数名全靠模型猜——Grok 猜 `page_number`(我们要 `page`)→error→模型看到错误再调→几十连发。修:①goto_page 参数别名(page_number/pageNumber/p,68 批 read_page 同款教训);②**工具熔断**(WS+RTC 双版):同名同参连续失败≥3=回填熔断强提示「禁止再调用」;≥6=不再 response.create 硬断循环(用户说话重启)。
- **"你听不到我说话吗"**:xAI server_vad 默认 threshold 0.85 偏钝(耳机麦轻声不触发)→降 0.5(OpenAI 同款)。

**99 WebRTC 桥(外放回声治本,relay 端已落)**:实测 xAI `/v1/realtime/calls` 返回 403"Team is not authorized"(端点存在=灰度中,我们无权限)→按用户设计在 Pi 架桥。**纯音频面**:浏览器⇄aiortc(Pi)WebRTC(<audio> 播放=浏览器 AEC 参考,同 #280 原理),Pi 侧转引擎 WS;事件/字幕/控制照旧走现有 ws。relay 端已实装(handle_openai 内):`bridge_offer` 信令→aiortc pc(非 trickle,Tailscale 内 host candidates 直连);入向 track→AudioResampler 24k mono→`_feed_audio`(与 ws binary 同路);出向 `response.output_audio.delta`→960B 定长块队列→`_mk_bridge_track`(20ms 实时 pacing,空发静音);speech_started 清桥缓冲;finally 关 pc;建桥失败=前端回退 ws 音频。**前端未接**(bridge_offer 发送/pc 建立/ontrack 播放/耳机检测自动分流/设置三态)=下一批,aiortc 1.14.0 已装 mcp-venv。边界:v1 只挂 GPT-WS/Grok 引擎,豆包(binary 协议+16k)二期。

## 批次 99 收尾+101(2026-07-13)回声桥前端 + Grok 转写三重复根修

**99 收尾(前端桥+耳机自动分流)**:
- `_abridge` 状态机(rc-voicecall):ws open 后按三态偏好判定(`rc-voice-bridge`:auto/1/0,设置「回声桥」下拉,设备级)——**auto=检测到耳机直连,外放走桥**;`_headphonesIn()`=enumerateDevices label 匹配(airpod/headphone/耳机/イヤホン…,getUserMedia 授权后 label 可见,iOS 不列 audiooutput 靠 input label 兜)。
- 桥建立:pc.addTrack(mic)→non-trickle offer(等 ICE complete,Tailscale host candidates)→ws `bridge_offer`→relay answer(`bridge_answer` 事件)→ontrack=`<audio>` 播放(**AEC 参考=回声消除生效**);`connected` 才置 `_abridge.on`(onCap 停发 ws 音频,此前双路防黑洞)。
- **双侧回退**:前端 6s 没 connected/pc 断态→`_abridgeStop`(onCap 恢复);relay pc connectionstatechange failed/closed→`_bridge["q"]=None`(下行回 ws)。teardown 随挂断收桥。
- 边界:v1 只挂 GPT-WS/Grok 引擎(handle_openai);豆包(16k+binary 协议)二期;openai_rtc 引擎原生 WebRTC 不需要桥。

**101 Grok 一句变三句根修(实测铁证)**:时间线同秒 2-3 条 q=程序性重复;真音频喂 xAI 抓事件序列——**xAI 同一轮发 2-3 个 `conversation.item.input_audio_transcription.completed`(官方文档只记载 .updated,被实测推翻)**,叠加 97a 因"文档无 completed"而加的 response.done 定稿补丁=三条。修:completed 按 item_id 去重(`_tr_done` set,OpenAI 一轮一个去重无害)+updated 只做 interim 字幕+撤 done 定稿。"接通没响应"=VAD 0.85 太钝(100 已降 0.5)+goto_page 风暴堵死(100 已熔断)。

## 批次 102(2026-07-13)工具静默统一表

用户实测:关了 rt_tool_reply,goto_page 后模型仍口头回报——silent 此前是逐工具散落打标(搜索/配图/视频三个专段),其它动作型工具漏网。收敛为 voice-tool 后处理的**统一静默表** `_SILENT_ACT = {goto_page, highlight, auto_highlight, add_vocab, open_book}`:动作/展示型(结果用户已在界面看到)成功即打 silent+统一 note(「已在界面生效…本轮不要发言」);失败不静默(要告知)。三个专段(个性 note)保留同机制同区域。分类原则:**信息型**(read_page/lookup_word/see_*/recall_*/translate…结果=回答原料)绝不静默;**任务型**(make_anki/make_note)保留简短确认(有等待语设计)。relay/前端 gate(silent && !rt_tool_reply → no_create)本来就统一,无需动。

## 批次 103-104(2026-07-13)转圈超时兜底 + 图像 item 用后即焚

**103**:工具 running 状态 150s 没 done/error→自动标超时(relay 重启杀死进行中工具=永远转圈的防线);**103b** 回声桥下行改道必须等 pc **connected**(offer 即改道时桥连不成=音频进无人播放队列全哑,"Grok 听不到"根因;connected 前照走 ws,与前端判定一致)。

**104(用户设计:发完图最好压历史)**:直喂图像 item **用后即焚**——比压缩整个历史更精准:图的价值在"看的那一刻",模型的文字回答已保留结论,图留在历史=每轮重复携带几千 token(cached 价也不免费+吃 24k 上下文硬顶,挤爆触发 truncation 反而破坏缓存前缀)。实现(RTC+WS 双引擎):直喂时 item **自带 id**(客户端可指定,免监听 created)记账 `(话轮, id)`;`speech_started`(新话轮)时 `conversation.item.delete` 焚**上上轮及更早**的图——保留最近一轮供追问"图里第三格什么意思";更早的要再看=模型重新扣扳机取图(拉模式语义一致)。WS 版新增 `_turn` 话轮计数;RTC 版复用 epoch。Grok 无视觉不涉及。

