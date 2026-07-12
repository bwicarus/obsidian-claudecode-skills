# Grok Voice Realtime 完整技术说明(用户整理,2026-07-13)

> 本文档由用户调研整理提供,作为 Grok provider 的事实来源。落地状态见 references/doubao-realtime-voice.md 批次 94-117。
> 权威源:docs.x.ai/developers/rest-api-reference/inference/voice(协议参考,与指南冲突时以此+实际事件为准)

## 核心事实速查

- 端点 `wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-1.0&reasoning.effort=high|none`(high=默认,工具选择更可靠)
- 会话 120min 上限/100 并发/us-east-1;PCM16 LE mono,8k-48k(默认24k),**声明 rate 必须=实际采样率**
- 计费:音频 $0.05/min(收发,静音 PCM 照计);文字 item $0.004/条(**即使不触发回答**);`function_call_output`/`response.create`/`session.update` 不按文字 item 计费;搜索 $5/1k
- 转写:`grok-transcribe`+language_hint+keyterms(≤100×50字符);事件 `updated`(**可修订累计全文**,覆盖同气泡不追加不去重)+ **`completed`(最新协议正式提供,用它定稿)**;updated 的 debounce 只作无 completed 时的兜底
- 状态注入三法:①session.update.instructions(整体替换,低频人设/书名);②assistant role item 播种历史($0.004/条,真正需要进历史的事实);③**response.create.response.instructions(只影响本轮,之后自动恢复 session instructions,$0——页面状态最佳)**
- 并行工具:收齐本轮全部 function_call→并行执行→全部回填→**只发一次 response.create**;工具续答前等浏览器上段音频播放完(response arbiter)
- force_message:硬编码 TTS 不过模型,"本身就是一轮",不要跟 response.create;interruptible:false 时播放中丢弃来话
- response.cancel 只停服务器生成,客户端已到达的 PCM 要自己清
- resumption:enabled+conversation_id 重连(新连接需再发 resumption enabled),恢复 transcript/工具调用/回填;闲置 30min 失效;不能替代本地账本
- **官方示例审计**:WS 示例用已废弃 ScriptProcessor/持续上传静音/鉴权 subprotocol 滞后;"WebRTC"示例实为 DataChannel 传 base64 PCM(**非 RTP 媒体轨,不能证明 iPad AEC 可用**)——都不是生产基线。iPad 回声正确方向:短期半双工(恢复绑定真实播放状态),长期真远端音轨桥(我们的 aiortc 桥正是此形态)

## 实施优先级(P0-P4)

P0 协议正确性:固定型号/updated 覆盖/completed 定稿/采样率一致/idle_timeout null/key 不下发 ✅全部已落地
P1 费用:本地 VAD/静音不上传/状态存 Pi 下一轮注入/文字 item 计数/音频账本/搜索预算闸(除预算闸✅)
P2 工具仲裁:并行收齐单 create/播放完成确认/turn epoch/过期丢弃/抢话取消
P3 iPad:半双工/真实播放状态/WebRTC 远端音轨桥(✅已建)/真机 AEC 测试
P4:resumption 续接/xAI 搜索工具/纯文字模态实测/provider 自动选择/SQLite 账本

## 纯文字模态——已实测(2026-07-13):**不支持**

实测(grok-voice-think-fast-1.0):`response.create` 带 `output_modalities:["text"]` 或 `modalities:["text"]` 均被**静默忽略**——三轮全部照常输出音频(audio.delta×12-13),`response.text.delta`/`output_text.delta` 零事件,无 error。结论:**四态(stt/half/route)无法迁移到 Grok Voice,它恒纯语音**;text 档要 Grok 只能走普通 Grok Text/Responses API(另一条链路)。协议参考里的 text.delta 事件可能仅用于纯文本输入场景或未来能力。

(完整原文含代码示例见 git 历史与用户提供稿;本文件为工程速查版)
