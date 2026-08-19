# RtcController 设计(task#283:持久 sideband 服务端控制面)

> 2026-07-12 定稿。外部审核最高优先项;官方 server-side controls 形态
> (developers.openai.com/api/docs/guides/realtime-server-controls)。
> 实现中如与本文冲突,以运行时实测为准并回写本文。

## 目标

WebRTC 通话的控制面从浏览器搬到 Pi:relay 以 call_id 建**持久 sideband WS**
(`wss://api.openai.com/v1/realtime?call_id=X`),成为**唯一工具执行者**+usage 权威账本+
上下文注入者+(后续)压缩状态机与预算硬闸。浏览器只管媒体(WebRTC 直连)+UI(字幕/对话窗)+
页面状态上报+截图供给。

## 关键洞察

sideband 的事件流与 GPT-WS 模式**同一协议**——relay `handle_openai`(voice_realtime_relay.py:1307)
的工具循环 `_tool`、`_oa_log_usage`、`up()` 的 page/state/ink 消息处理**全部可复用**。
RtcController ≈ handle_openai 去掉音频转发(媒体走 WebRTC)+去掉 session 配置(rtc-session/rtc-call 流程不变)。

## 架构

```
浏览器
 ├─ WebRTC 媒体轨 ←→ OpenAI(音频不过 Pi)
 ├─ dc 'oai-events':只消费**显示类**事件(transcript delta/created/done/item.added 字幕对话窗)
 └─ 控制 WS → relay `?mode=rtc&call_id=X&file=&page=`
      ├─ 上行:page/state/ink 消息(handle_browser up() 现有格式)+ 截图响应(shot)
      └─ 下行:client_action / tool_status(现有事件格式)+ 截图请求(need_shot)

relay handle_rtc_ctl(bws, call_id, file, page)
 ├─ sideband = websockets.connect(OPENAI_RT_URL_CALL + call_id, Bearer key)
 ├─ 监听 sideband:function_call_arguments.done → _tool(复用)→ item.create(output)+response.create
 │    response.done → _oa_log_usage(复用)
 ├─ 上下文注入:拉模式——speech_started 时把 pend 状态注入(system item 经 sideband)
 ├─ see_ink/see_page:向前端控制 WS 发 need_shot → 前端 _captureView → shot 回传 → ctx.view_image
 └─ (后续)压缩状态机(#285)/预算硬闸/reply_text 拦截
```

## 双通道分工(防双执行)

前端 `_rtc.ctl` 标志:控制 WS 连接成功=true。
- ctl=true:`_rtcOnEvent` 的 function_call_arguments.done **跳过**(relay 执行);
  rtc-usage 上报**跳过**(relay 记账);_rtcFlushCtx 跳过(relay 注入);
  ink/state/page 经**控制 WS** 发(shim 改指向?不——控制 WS 独立,共享轮询双写或改写目标)。
- ctl=false(relay 不可用/断线):**回退现有前端全套**(渐进迁移的韧性保障,现有代码即 fallback)。

## response.create 与输出模态

㊿-53 的四态+auto 逻辑:ctl 模式下 speech_stopped 的 response.create 由谁发?
**relay 发**(它有 rt_voice_mode 配置=服务器持久化的,天然一致);前端 speech_stopped 分支在
ctl=true 时跳过。mixed 档的 src 判定在 relay(它知道这轮是否工具回填)。
tts 档的代念:relay 无法播——done 时经控制 WS 下行事件让前端 speak。

## 分步实施(每步可独立部署验证)

1. **P1 骨架** ✅(语音54,真机验证):handle_browser 分发 mode=rtc;handle_rtc_ctl 打开 sideband、镜像日志(只读不动作);
   前端 rtcStart 连控制 WS(失败静默=纯前端模式)。验证:journalctl 看 sideband 收到事件流。
2. **P2 工具接管** ✅(语音58):relay _tool 复用接 sideband;前端 ctl=true 跳过工具;need_shot/shot 截图往返;
   client_action/tool_status 经控制 WS 下行(前端 dispatch/onToolStatus 现有处理)。
   实现要点(以运行时为准):
   - relay `handle_rtc_ctl`:`_tool`(voice-tool 执行+client_action/tool_status 下行)+
     **工具缓存** `ck=name|args|page|ink指纹`(voice-tool 响应 `cacheable` 白名单才存,命中重放 client_action,治 read_page 同页重复调用=#287 提前落地)+
     `_need_shot()`(need_shot→前端 _captureView→shot 回填 Future,6s 超时无图继续)+
     `_resp_create()` 工具回填模态按 rt_voice_mode(mixed 档 tool 来源=text,512/2048 分级预算)。
   - 前端分工:function_call_arguments.done 在 ctl=true 时**只放行 reply_text/wait_for_user**(纯前端语义),其余 return(relay 执行);
     tool_status 下行时置 `turnTool=true`(承诺核查放行)+ see_* done 复位 `inkDirty`(边沿复位镜像,原复位点在前端 _rtcTool 不再经过);
     shim send 把 page/state/ink 上行**镜像**给控制 WS(relay 工具 ctx 要最新状态)。
   - ⚠ 坑:`rtc_call_id` 必须放 voice-tool **请求体顶层**(webapp 读 `body["rtc_call_id"]`),放 ctx 里图像 sideband 注入静默失效。
   - **版本握手(59,用户首测实锤双执行)**:旧页面 JS(部署前加载,无分工逻辑)+新 relay=同一工具两边各跑一遍
     (webapp 日志同秒两条 read_page:Safari UA=前端 / python-httpx=relay)+双 response.create 撞
     `conversation_already_has_active_response`。修=前端控制 WS URL 带 `fe=2`,relay 只对 fe≥2 接管工具,
     否则退回 P1 观察;凡改变双端分工的升级都必须走这种 capability 声明,不能假设前端已是新版。
     另:撞车被拒的 create 记 `pend`,response.done 时补发(否则工具结果永远无人回答)。
2.5 **P2.5 审核二轮落地** ✅(语音60):
   - **视觉链路真修**(P0):58 的 rtc_call_id 修复被 python replace 落到同形代码第一处(GPT-WS `_tool`,
     那里 call_id 是函数调用 ID)——撤销;P2 改用**自己持有的 sideband(ows)直喂 input_image**
     (与 WS 版同构,不传 rtc_call_id、webapp 不开第二条临时连接);webapp `_rtc_sideband_images`
     (仍服务前端 fallback)等每张 created 再关。
   - **turn epoch**(P0 抢话竞态):speech_started/打字=epoch+1;工具完成时纪元已变→只读换过期提示回填、
     写工具回填真实结果,**都不 response.create**;撞车补发带纪元检查。完整响应仲裁(create/cancel 全归 relay)=P4。
   - shot_id 配对(防两轮工具重叠错配)+截图尺寸上限(长边1600/质量阶梯≤900KB/serve 8MiB)
   - 缓存:sel/ink 全量 md5 进键;see_*/搜索类退出缓存;**写工具成功→tool_cache.clear()**(revision 体系的保守替身)。
   - reply_text 512 截断=前端抢救提示(治本 route_to_text=task#289)。
3. **P3 usage+注入接管**:relay 记账(前端跳过);拉模式注入搬 relay(page/state/ink 经控制 WS 上行,
   speech_started 时 relay 注入)。**前置=#284 SQLite 事件账本**(审核:JSON 账本无锁 read-modify-write
   且浏览器上报不可为硬闸权威;responses/tool_calls/usage_events 带 UNIQUE 幂等键,事务累计)。
4. **P4 response.create 接管**(四态+auto 在 relay)+承诺核查搬 relay+响应仲裁(所有 create/cancel 串行化)。
5. **P5(=#285/#286)**:压缩状态机/预算硬闸/getStats 上报入库。之后=控制 WS 可恢复重连(task#290:
   退避+ctl_ready 才转移所有权+快照重推+call 去重;在此之前"断线保持前端模式"比朴素重连安全)。

## 现有代码锚点

- relay:handle_openai :1307(工具循环 _tool :1368)、handle_browser :1776(mode 分发)、
  _oa_log_usage :1199、_fetch_book_ctx :253、up() page/state/ink 处理 ~:1990s
- 前端:rc-voicecall.js rtcStart/_rtcOnEvent/_rtcTool/_rtcFlushCtx/_captureView
- webapp:rtc-session/rtc-call(不变;call_id 已从 Location 取)

## 风险

- relay 重启=控制面断:前端 ctl 断线回退纯前端模式(P2 起必须实现回退开关)。
- sideband 与 dc 事件顺序无保证:显示(dc)与执行(sideband)解耦,无共享状态,安全。
- 双 response.create(前端漏改+relay 都发)→ 重复回答:P4 切换时前端分支必须同批部署。

## 踩坑:三条 relay 引擎的 tool_status 字段不同步(2026-07-21,用户实锤"制卡预览不显示")

**症状**:语音制卡后对话里只有 AI 自己写的一句描述("已帮你做成一组 Anki 草稿卡片啦…")+ 一个不起作用的 ▶,
从来看不到真正的卡片预览(正面/背面/入库按钮)。

**根因**:`voice_realtime_relay.py` 里其实有 **3 处**几乎相同的 `_tool()` 工具执行函数,对应 3 条历史遗留的
引擎路径:①`handle_browser`豆包 WS(~840 行)②`handle_openai` WS/grok 引擎(~1650 行)③`handle_rtc_ctl`
openai_rtc 控制面(~2800 行,**当前主力引擎**,`_ledger_tool("openai_rtc",...)` 可辨认)。三处各自独立组装
`tool_status` 的 payload。①处正确:把 `res` 拆成 `slim`(喂回模型的精简版,遇到 `cards`+`cards_brief` 并存时
剔掉 `cards` 全文)和 `slim_full`(完整版,塞进 `tool_status.result` 给前端渲预览卡)。②③处是后加的引擎,
**漏抄了这个模式**——既不带 `result` 字段,也不剔 `cards` 就直接 `json.dumps(slim)[:1600]` 塞进 `rag`。
制卡这类工具 `cards` 全文很容易超 1600 字符,被从中截断 → 前端 `_chipEnd`(rc-voicecall.js)
`p.result` 是 undefined 只能退回 `JSON.parse(p.rag)`,解析残缺 JSON 直接抛异常,被吞掉,
"制卡结果里有没有卡片数据"这个判断就此静默失败,退到最后的兜底分支(只留 AI 文字 + 空【流程】卡)。

**修法(2026-07-21 二阶段,用户"把能合并的都合并")**:三处 `_tool()` 里"结果预处理"这段逐字重复的代码
(剔控制字段 → cards/images 精简 → 限长兜底 → slim_full 完整体)**抽成模块级共享函数**,一次改、三处生效:
- `_prep_tool_result(res, tool_name) → (slim, slim_full, rag)`:slim=喂回模型精简版、slim_full=完整体(渲预览卡)、
  rag=按 `_RAG_LIMIT` 限长的串。三处调用一行 `slim, slim_full, out/content = _prep_tool_result(res, ...)`。
- `_tool_preview_result(slim_full)`:tool_status.result 字段(有 cards 才给完整体,否则 None),三处统一。
这不只是补齐②③,而是**从结构上消除**"三份手抄各自漏字段"的复发源:合并时还发现②③连 images/found_brief
剔除都漏了(只有①有)——现在三处统一都有,search_image 在 grok/rtc 引擎上也不再挤爆 token 预算。
**前端零改动**——`_chipEnd` 的提取逻辑本来就对,只是从未收到过它期待的字段。
⚠ block2/3 的 `slim_full = None` 初始化必须保留:它们的 `_tool()` 有多个短路分支(recall_study/deep_think/
route_to_text/cache 命中)不走 `_prep_tool_result`,而 tool_status 在 try/except 之后无条件发,不初始化会 NameError。

**验证**:纯 Python 模拟 slim/slim_full 构造(cards 剔除后 `out` 远小于 1600、`result.cards` 完整);
headless E2E 直接给 `RC.turnCard.addPart({kind:'cards',...})` 喂同形状数据,确认渲出 `.fc-card`
(正面文本+入库按钮+多卡圆点)。

**部署要点(2026-08-19 复核,结论与原文相反)**:`voice-rt.service` 跑的是**已安装副本** `/home/bwicarus/webapp/voice_realtime_relay.py`(见 `references/systemd/voice-rt.service` 的 ExecStart 与 WorkingDirectory),
**不是** checkout 里的 `_server_deploy/voice_realtime_relay.py`——只改 checkout 不部署,线上一个字都不会变;
该文件在 `scripts/reader_deploy_manifest.py` 清单内(dest `webapp/voice_realtime_relay.py`),**必须走 `scripts/deploy_reader.sh`**——脚本自带原子安装、`assert_voice_runtime_stable` 稳定性检查,并在装完断言 `systemctl cat voice-rt` 的 ExecStart 指向 `/home/bwicarus/webapp/voice_realtime_relay.py`(deploy_reader.sh:1493);不要手工 `cp` + `sudo systemctl restart voice-rt`。⚠ 重启会掐断进行中的通话,发布前仍需自行确认当前无人在通话中(原文引用的 `[[restart-voice-rt-check-active-call]]` 程序化 gate 全仓查无此物,是悬空链接,别指望它挡)。

**遗留**:①处的 cache 命中分支(`tool_cache[ck] = {"out":..., "ca":...}`)没存 `slim_full`,
命中缓存重放时同样会丢 `result`——但 make_anki 等写操作从不 cacheable,只有只读工具(如
`search_image`)理论上会撞上,当前未见用户报告,标记为已知小缺口,不在本次范围内修。
