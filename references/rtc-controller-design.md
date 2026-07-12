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

1. **P1 骨架**:handle_browser 分发 mode=rtc;handle_rtc_ctl 打开 sideband、镜像日志(只读不动作);
   前端 rtcStart 连控制 WS(失败静默=纯前端模式)。验证:journalctl 看 sideband 收到事件流。
2. **P2 工具接管**:relay _tool 复用接 sideband;前端 ctl=true 跳过工具;need_shot/shot 截图往返;
   client_action/tool_status 经控制 WS 下行(前端 dispatch/onToolStatus 现有处理)。
3. **P3 usage+注入接管**:relay 记账(前端跳过);拉模式注入搬 relay(page/state/ink 经控制 WS 上行,
   speech_started 时 relay 注入)。
4. **P4 response.create 接管**(四态+auto 在 relay)+承诺核查搬 relay。
5. **P5(=#285/#286)**:压缩状态机/预算硬闸/getStats 上报入库。

## 现有代码锚点

- relay:handle_openai :1307(工具循环 _tool :1368)、handle_browser :1776(mode 分发)、
  _oa_log_usage :1199、_fetch_book_ctx :253、up() page/state/ink 处理 ~:1990s
- 前端:rc-voicecall.js rtcStart/_rtcOnEvent/_rtcTool/_rtcFlushCtx/_captureView
- webapp:rtc-session/rtc-call(不变;call_id 已从 Location 取)

## 风险

- relay 重启=控制面断:前端 ctl 断线回退纯前端模式(P2 起必须实现回退开关)。
- sideband 与 dc 事件顺序无保证:显示(dc)与执行(sideband)解耦,无共享状态,安全。
- 双 response.create(前端漏改+relay 都发)→ 重复回答:P4 切换时前端分支必须同批部署。
