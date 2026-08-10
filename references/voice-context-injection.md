# 语音上下文注入 · 统一端口(设计定稿 2026-07-21)
> 用户拍板:『不要再因几种语音注入方式不同出问题——包装成统一端口』。本文=5-agent workflow 盘点四路注入点
> 的对照地图+设计稿。**实施前必读;加任何新注入只走 RC.voiceCtx,严禁直连某条传输。**
# 语音助手上下文注入统一端口设计稿

## 当前 Realtime 传输边界

- iOS App 本机书与浏览器扩展的普通电话在选择 `openai_rtc` 时，先向已认证的 Pi 端点申请
  短期 `ek_` client secret，再由设备把 SDP、麦克风和 `oai-events` DataChannel **直接连接**
  `https://api.openai.com/v1/realtime/calls`；长期项目 key 永远留在 Pi。
- 直连不等于删除 Pi：`/voice-rt?mode=rtc` 仍是同一个 `call_id` 的控制 sideband，只承载
  页码、选区、笔迹、截图请求、工具执行、单通话接管和 VAD 真伪裁决，不转发媒体。
- `RC.voiceCtx` 的 `rtc` transport 仍绑定浏览器自己的 DataChannel；`dc.onopen` 必须先回放历史，
  再 `flushPending('rtc')`。用户开口边沿必须先 `_requestSyncNow()`，再 `_rtcFlushCtx()`，保证
  当前视口而非上一帧被注入。重连后仍由 control sideband 清同步指纹并重推状态。
- PWA 无扩展路径暂保留服务端 SDP 代理作为兼容回退；不能据此把 App/扩展媒体重新绕回 Pi。

## 一、注入种类 × 路 对照表

路定义:**dc** = WebRTC 前端 data channel(rc-voicecall.js);**relay** = 豆包/openai-WS/grok 服务端(voice_realtime_relay.py);**文字** = 侧栏 send-ctx(rc-assistant.js → assistant.py)。✅=有,➖=设计上不需要,⚠=不对称/实测坑。

| kind | dc(WebRTC) | relay(豆包为主) | 文字侧栏 | 不对称 |
|---|---|---|---|---|
| 页码+页文本 | ✅ `_rtcFlushCtx`:2818 开口边沿快照,fp=`_sentCtxFp` | ✅ SP 中层 `_fetch_book_ctx` L257 + vtext 直供 L3539 | ✅ `ctx.visible_text`:1416 + 后端 `_page_text` | ⚠ dc 有 8s~12min dwell 注入门,且**压缩后 `_sentCtxFp` 不清**→删掉的 ctx 同状态不重推(:3188 只清无消费者的 `_pageFp`) |
| 总页数 | ✅ ctx 快照 page/total | ➖ 故意不进 SP(跨书报错数),工具结果携带 L389 | ✅ ctx | 设计差异,非 bug |
| 插图描述+本页生词 | ⚠ **无**(只能靠 read_page 工具) | ✅ voice-ctx 直塞 SP L268-275 | ✅ base ctx(可见KG/生词) | dc 路开口即答场景缺此素材 |
| 圈画 ink | ✅ ikHint 提示 see_ink(:2841);system 注入**归 relay** | ✅ 510 状态事件 `_state_event_text` L418 | ✅ `ctx.ink` + `_text_under_ink`:5011 | 已知哑路教训(:1925 dc 直发从没落地),现分工明确,不算坑 |
| 选中 sel / focus / figs_n | sel 注入归 relay;⚠ focus/figs **rtc-ctl 分支不消费** | ✅ 三字段 L3557-3566 | ✅ `ctx.selection`+focus_sel chip | ⚠ 统一上行时 focus/figs 在 rtc-ctl/openai 分支静默丢 |
| 长按带入 pins | ✅ `_pinSync`:1121 覆盖式+fp | ⚠ **无**(syncState 只上 sel/focus/figs,pins 不上行) | ✅ `ctx.pinned`:1849 每轮快照 | ⚠⚠ 豆包路模型不知道带入了什么;dc 路 **fp 在 `!_rtc.on` 早退前推进**(:1127)→通话外改 pin 下通电话漏快照 |
| 配图✕删除通告 | ✅ `_imgGoneNote`:1200(dc open 才发) | ⚠ **无** | ⚠ **无——`__vcRemovedImgs` 环全仓零消费者(死代码)** | ⚠⚠⚠ 今天这类 bug 的典型:文字路 AI 永远不知道删过图;dc 未 open 也丢,「环里留底」没人读 |
| 创造物告知 creations | ✅ rcHint:2832,15s SWR | ⚠ **无**(SP 只有工具目录,无 creations-brief) | ✅ 同源 `_creations_recent_line` | ⚠ 豆包路 recall_creation 无句柄可指 |
| 工具结果喂回 | ✅ function_call_output:3111,预算精简 | ✅ 502 external_rag L855,slim 管线 | ➖ agent 循环原生 | 机制各异但均有;精简规则(cards_brief/found_brief/截断)**三处重复实现** ⚠ |
| recent_tools 制卡语境 | ✅ :3098 环6条 | ✅ 随 voice-tool ctx | ⚠ **send 不上送**,制卡资料退化为最近6轮文本 | ⚠ 已知不对称 |
| 承诺核查 | ✅ :3313 dc 独有 | ➖(JSON 拦截制,模型说话≠出工具的场景不同) | ➖(agent 真调) | 路独有语义,不必统一 |
| 重连快照 | ✅ `_rtcInjectHistory`:3365 | ⚠ **无**(一连接=一通话,断线即终) | ✅ 服务端 `_convo_load` | 豆包补齐属 P3,暂不入本设计 |
| 会话内压缩 | ✅ `_rtcCompactNow`:3164 | ✅ qa_log 26轮压700字 L3722 | ✅ 恒6轮+/compact-history | 均有 |
| 打字用户消息 | ✅ 双投递(dc 真发+ctl 镜像做卫生) | ✅ 501 | ✅ send 拦截 | ⚠ 统一端口**严禁**让 relay 在 rtc 模式代发(双份) |
| 视口截图 | ✅ need_shot 按需:3057 | ✅ EPUB view_shot | ✅ want_viewshot 现拍 | 时机/条件三套,素材通道不在本次统一范围 |

**结论**:红色重灾区集中在「**告知类小消息**」(pins/removed_imgs/creations/未来的任何通告)——三路各自实现、各自的 open 判定、各自的 fp,一路做了另一路漏。而 page/sel/ink 三大件走 setPage/syncState/syncInk 上行遥测,有成熟指纹和两次翻车史(123/133),机制反而是收敛的。**统一端口首先收编告知类,三大件保持现状只做注册表挂靠。**

---

## 二、统一端口设计

### 2.1 核心抽象:两类语义

所有注入归两类,这是全设计的地基(与现有实践对齐):

- **state(状态类,覆盖式)**:同 kind 只有「当前值」,旧声明作废。注入措辞自带「以本条为准」。丢一条无所谓,下次注入自愈。例:pins、creations、页文本。
- **event(事件类,append-only)**:发生过就必须送达,不可被覆盖。例:removed_imgs、未来的「用户撤销了某操作」通告。

### 2.2 接口签名(前端共享层,新文件 `rc-voicectx.js`)

```js
RC.voiceCtx = {
  // 注册 kind——今后"加任何注入只写一处"指的就是这里
  register(kind, spec),

  state(kind, payload),                    // 覆盖式:只留最新
  event(kind, payload, {mergeMs=0}={}),    // append-only:mergeMs 窗口内同 kind 合并(替代 _goneBuf 800ms)

  invalidate(kinds=null),                  // 清已投递指纹→状态类下个边沿重推(压缩/重连后调)
  flushPending(route),                     // 通话建立后补投 pending(dc.onopen / ctl 连上)
  drainForSend(),                          // 文字路:send 时取走 {events:[...], states:{...}}
};

// spec 定义(注册表条目)
{
  cls: 'state' | 'event',
  fp(payload) -> string,          // 缺省 = 稳定 JSON hash
  text(payload, route) -> string, // 渲染注入文案;route ∈ 'rtc'|'relay'|'text',可差异化措辞
  budget: 800,                    // 截断上限(硬预算纪律沿用:数字进 spec 不散落)
  routes: {rtc:true, relay:true, text:true},
  relayCls: 'state510'|'event510' // relay 侧注入归类(见 §三)
}
```

### 2.3 kind 枚举(首批)

| kind | cls | fp | 现对应物 |
|---|---|---|---|
| `pins` | state | `sorted(labels).join('¦')` | `_pinSync` 覆盖式快照 |
| `removed_imgs` | event | —(event 无 fp,mergeMs=800) | `_imgGoneNote` + 死环 `__vcRemovedImgs` |
| `creations` | state | `n:first24` | rcHint |
| `page_ctx` | state | `page:total:vt.len:vt[:30]` | `_rtcFlushCtx` 页文本段(P2 挂靠) |
| `ink_hint` | state | `page:strokes.n` | ikHint(P2 挂靠) |

### 2.4 路由 adapter 职责(voiceCtx 内部,按当前通话模式分发)

**关键规则:voiceCtx 自己选通道,不经 shim `ws.send` 镜像**——shim 双投递是 text 消息的专属语义,note 若走 shim 会 dc+relay 各注一遍。

| 模式 | state 类 | event 类 |
|---|---|---|
| **WebRTC(`_rtc.on` 且 dc open)** | 标脏,由 `_rtcFlushCtx` 在开口/打字边沿统一拼装(快照=所有脏 state kind 的 `text()` 拼接,一条 system) | 立即 `_rtcSys`;`_dcSend` 返回成功才前移 fp/出环 |
| **WebRTC dc 未 open / 无通话** | 存 pending(只留最新) | 入 pending 环(≤16 条) |
| **relay(豆包/openai-WS/grok,真 ws)** | `ws.send({type:'note', kind, cls:'state', fp, text})`——**发渲染好的文案**,relay 保持薄门面 | `ws.send({type:'note', kind, cls:'event', text})` |
| **文字侧栏** | 不主动投;send 时 `drainForSend().states` 附 `ctx.announcements`(fp 不前移,每轮重发=现状哲学) | `drainForSend().events` 附上;POST 发出后出环 |

**补投时机**(参考重连快照先例):
- dc.onopen:先 `_rtcInjectHistory`,后 `flushPending('rtc')`(顺序保「延续语境」条在前);
- relay 路:ctl WS/真 WS 连上且 StartSession 完成后 flush;
- 无通话期间积累的 event,文字 send 也可消费(谁先来谁投,投完出环)——修「dc 没开时删图信息永久丢失」。

### 2.5 去重策略(统一 fp)

三条铁律,替代现在三套散装逻辑:

1. **fp 按 `(kind, channel)` 存**,channel ∈ {rtc, relay}(文字路无 fp)。同一状态在 dc 注过,不妨碍 relay 路(下通电话换豆包)再注。
2. **fp 只在投递成功处前移**:dc=`_dcSend` 验 readyState 成功后;relay=ctl ws send 不抛后(P2 可加 `note_ack` seq 回执)。采集处(`_pinToggle` 等)只更新 payload。→ 根治 pin「通话外推进 fp 下通漏快照」。
3. **`invalidate()` 是唯一的指纹清理入口**:`_rtcCompactNow` 尾部、重连处、`_chipEnd` 强刷都调它(带 kinds 参数)。→ 根治「压缩后 `_sentCtxFp` 不清 / 清了无消费者的 `_pageFp`」这类清错对象的坑;顺手删 `_pageFp`/`_inkFp` 残留符号(dc 路)。

去重三形态收敛为:state=fp 指纹;event=mergeMs 时间合并;时间冷却(15s SWR 等)留在采集侧(如 `_rtcCreFetch`),不进端口。

### 2.6 迁移清单(每个现有调用点 → 统一口)

| # | 现调用点 | 改法 | 批次 |
|---|---|---|---|
| 1 | `_pinSync` rc-voicecall.js:1121 | 采集层保留(_pins 状态中心/label 唯一化/cid 去重不动);注入段替换为 `voiceCtx.state('pins',{...})`;删自管 `_pins.fp` 与 `!_rtc.on` 早退 | P1 |
| 2 | `_imgGoneNote` :1200-1214 | 整函数替换为 `voiceCtx.event('removed_imgs',{items},{mergeMs:800})`;删死环 `__vcRemovedImgs`(pending 环即留底) | P1 |
| 3 | rc-assistant.js send :1848 | ctx 组装处加 `Object.assign(sentCtx, {announcements: RC.voiceCtx.drainForSend()})`;后端 assistant.py `_ctx_block` 加【系统通告】段(events)与【当前状态】段(states),no_book 分支保留 announcements | P1 |
| 4 | `_rtcInjectHistory` 完成后 :3378 附近 | 追加 `voiceCtx.flushPending('rtc')` | P1 |
| 5 | `_rtcCompactNow` :3188-3191 | 清 `_pageFp/_inkFp` 改为 `voiceCtx.invalidate()`(含 page_ctx 后自动覆盖 `_sentCtxFp` 语义) | P1(修已知坑) |
| 6 | rcHint 拼装 :2832-2839 + `_chipEnd`:663 | 注册 `creations`;`_chipEnd` 改调 `invalidate(['creations'])`;豆包路借统一上行首次获得 creations 告知 | P2 |
| 7 | `_rtcFlushCtx` :2818-2851 | registry 化:快照=遍历脏 state kind 拼 `text()`;`_sentCtxFp`=各 kind fp 拼接。行为必须保持「同状态开口零注入」 | P2 |
| 8 | ikHint :2841-2843 | 注册 `ink_hint`,随 #7 挂靠 | P2 |
| 9 | recent_tools 文字路 | `drainForSend()` 顺带导出 `window.__vcRecentTools` 环(-4)进 ctx | P2 |
| 10 | focus/figs 在 rtc-ctl/openai 分支 | 借 `note` 通用消息补齐(或 relay 分支补读)| P2 |

**不迁**(明确排除):setPage/syncState/syncInk 三大件上行(遥测,非注入,有成熟指纹与翻车史)、工具结果喂回(协议深度绑定各路:function_call_output vs 502)、承诺核查(dc 独有守卫)、压缩/重连快照本体(会话生命周期机制,只是挂 flush/invalidate 钩子)。

---

## 三、relay 侧改动(最小)

新增一种上行小消息 `{type:'note', kind, cls, fp?, text}`,四个接收分支各加 3-8 行:

1. **rtc-ctl `_up`(L3288 一带)**:`elif t=='note':` state 类 → `book.setdefault('_vc_state',{})[kind]=text`;event 类 → `book.setdefault('_vc_events',[]).append(text)`(环≤16);置 `_dirty3`。**注入复用现有 speech_started 边沿的 ink/sel system 注入点**:在同一处把 `_vc_state` 变更(fp 比对)与 `_vc_events` 全量 drain 拼进同一条 system,events 注入后清空。不新增注入时机=不新增打断/缓存风险。
2. **豆包 `up()`(L3373 handle_browser)**:state 类 → 并入 `_state_event_text` 的数据源(合并快照本来就是「(系统状态更新:…)」),走既有 `_push_state_debounced` 1.2s 防抖 + 510;event 类 → **单独一对 510**(`_inject_memory`)立即追加(event 不可被下一次覆盖式快照吃掉),text 截 500。SP 完全不碰——不扰动 md5/251 ack 确认制。
3. **openai-WS 通用分支**:`t=='note'` → 直接 dc `conversation.item.create` system(该分支已是增量注入模式,照抄 sel 注入的写法)。
4. **grok 分支**:只落 book,instructions 每轮重建时带上 `_vc_state` + drain `_vc_events`,然后 `continue`(120 坑:漏 continue=双通道双份钱)。

护栏:
- **rtc 模式下前端不发 note 上行**(voiceCtx 直走 dc),rtc-ctl 的 note 分支只服务「ctl 在、dc 注入失败回退」的将来扩展——P1 可以先只实现 1 号分支存储不注入,验证通路;
- relay 对 note.text 只存只转,**不解析不加工**(薄门面原则;渲染在前端 `text()` 已完成);
- 换页清 book 状态时(L3550 一带)**不清** `_vc_events`(事件跨页有效),`_vc_state` 按 kind 的 spec 决定(pins 跨页保留,page_ctx 类清)。

---

## 四、实施分批

### P1(最小可用:告知类三件 + 文字路消费 + pending 补投)

内容:`rc-voicectx.js` 端口本体(register/state/event/invalidate/flushPending/drainForSend)+ 迁移清单 #1-#5 + relay 分支 1(存储)+ assistant.py 通告段。

**直接修掉的实测 bug**:① `__vcRemovedImgs` 死环(文字路删图无所指);② dc 未 open 删图通告永久丢;③ pin 通话外 fp 推进漏快照;④ 压缩后 `_sentCtxFp` 不清。

回归风险点:
- **双投**:rtc 模式 note 严禁镜像 ctlWs;文字 send 消费了 event 后,若随即开语音通话,pending 里不能还有同一条(drainForSend 出环必须原子)。
- **注入顺序**:dc.onopen 补投必须在重连快照之后,否则「延续语境」条被顶散。
- **prompt 缓存纪律**:event 注入恒 append-only;任何 note 文案不得进 instructions/SP。
- **pin 行为微变**:fp 前移点后移→通话建立后会补投一条 pin 快照(这是期望修复),回归验「同一通话内 pin 没变=零注入」仍成立。
- **assistant.py no_book 分支**:announcements 须保留(删图通告与书无关)。
- 验证:照 memory 规矩走后端 test_client + `check_pdf_reader_js.sh`;真机各模式(WebRTC/豆包/文字)各做一遍「删图→问『刚删的那张』」「通话外 pin→开电话」。

### P2(拼装 registry 化 + relay 注入收编)

内容:#6-#10 + relay 分支 1 的注入段 + 分支 2/3/4。

回归风险点:
- **`_rtcFlushCtx` 重构**是 P2 最险一刀:必须保「同状态开口零注入」「dwell 注入门」「fp 含笔画数」逐项等价,建议先影子模式(新旧 fp 并算,日志比对一周)再切;
- 豆包 510 注入量新增(creations/pins)→ 轮轮计费,text() 预算要按豆包 12K 收紧(spec 的 route 参数就是为此);
- grok `continue` 漏了=双份钱(120 坑重演);
- openai-WS 分支 note 注入与既有 sel 注入的 fp 各自独立,别共用 `_sel_fp`;
- 换页清 book 时误清 `_vc_events`(事件丢失)或误留 page 级 state(陈旧注入)。

### P3(暂列不做)

豆包重连快照、ink/sel/page 三大件收编、工具喂回精简管线三处合一。均属高风险低增量,等 P1/P2 稳定后按需立项。

---

**一句话总纲**:统一端口只统一「**告知类注入**」的采集→选路→去重→补投四步,注入的**物理时机**仍尊重各路既有边沿(dc=开口快照/立即 system,豆包=510 防抖/立即 510,文字=send 快照);今后加一种注入 = `register()` 一个 spec + 在业务处调一行 `state()/event()`,三路+无通话补投自动全覆盖。

---

# 附:四路盘点 notes(每路机制与坑,点位详情见各路代码注释)

## WebRTC-dc 前端

总体机制:㊵ 拉模式(用户拍板『只在需要时读取』)——翻页/滚动/圈画/选中平时只更新本地状态零注入,内容在**用户开口/打字的瞬间**由 _rtcFlushCtx 合并成一条 system 快照经前端自己的 data channel 一跳直达 OpenAI(绕 relay 的旧路=OpenAI→Pi→OpenAI 跨海往返,短问题赶不上 VAD)。所有 dc 注入走 _rtcSys→_dcSend,而 _dcSend 在 dc 未 open 时**静默丢弃**——这是本路最大的坑(ink 直发从没落地过的哑路教训 :1925-1932;_imgGoneNote 因此显式验 readyState)。注入纪律:①instructions 恒定不含动态书页内容(跨会话 prompt 缓存命中 :3529);②append-only 不改历史(改前缀=缓存全失效);③大图绝不经 dc(SCTP Safari≈64KB 超限直接关 dc 通话哑死),走后端 rtc_call_id sideband;④预算硬数字:页文本1500/pin单项2500/工具喂回1800/摘要1500-2000/用户消息2000。去重三形态:fp 指纹(ctx 快照 _sentCtxFp、pin _pins.fp、ink _inkFp)、覆盖式快照(pin『以本条为准旧声明作废』)、时间冷却(承诺核查 scoldT 60s、压缩 90s、创造物 15s)。已知坑:压缩后 _sentCtxFp 不清而被清的 _pageFp 无消费者(残留符号)→ 删掉旧 ctx 注入后同状态不重推;pin fp 在通话外推进会让下通电话漏快照。与 relay 路分工(㊺P2):ctl WS 在时工具执行归 relay、ink/sel 的 system 注入归 relay(前端只在 ctx 快照给 ikHint 提示 see_ink),前端保底全套(ctl 断线回退);response.create 仲裁也归 relay,前端 6.5s 兜底防全哑。任务问的 inkText 符号在本文件不存在,笔迹状态实为 _rtc.ink/_inkFp/inkDirty。

**注入点清单**:
- **页码+页文本+圈画提示(ikHint)+创造物清单(rcHint) 合并一条 ctx 快照** @ _rtcFlushCtx rc-voicecall.js:2818-2851(_rtcSys 调用点 :2847) · 传输:dc conversation.item.create role=system(_rtcSys :1815-1816 → _dcSend :1814;dc 未 open 静默丢弃) · 去重:fp 指纹 _rtc._sentCtxFp(:2844-2846)= page/total : vt.length : vt[:30] : cre.length : cre[:24] : ink笔画数 · ⚠ 页文本优先 _rtc.pendText(前端可见文本截1500);空则走 _ptCache 服务端兜底,且有注入门:翻页后停留 8s~12min(pageTs dwell :2824-2830)窗外不注入(模型可自己 read_page)。⚠ 压缩(:3188)只清 _pageFp/_inkFp(前者全文件无消费者=残
- **页文本兜底缓存 _ptCache(填充,非直接注入)** @ _rtcFetchPageText :2809-2817;预拉 :1911-1914;flush 现场补拉 :2829 · 传输:fetch /api/assistant/voice-page-text → 存 _rtc._ptCache={k:file:page, t};内容只经 _rtcFlushCtx 进模型 · 去重:_ptBusy 同 key 只飞一发;cache 按 file:page 精确键 · ⚠ 为扫描书/图片模式设(前端 visible_text 采不到);文字/relay 路对应的是服务端 _page_text 直注,这里是异步缓存补位
- **pendText 状态维护(可见文本本地态)** @ :1918(shim 'page' 消息写入)、:3530(rtcStart 从 RC.adapter().getContext().visible_text 初始化截2000)、2s 恒推循环 :4232-4242 · 传输:纯本地状态零网络零 token(ws=_rtcShimWs :1885 顶替全局 ws,'page' 只更新 _rtc.*);同时上行镜像给 ctl WS 喂 relay(:1888-1889) · 去重:无(覆盖式本地态,恒推保持最新即可) · ⚠ 对比豆包路:豆包只在翻页时推、不带视口流(滚动流会打 dialog 缓存);dc 路可以恒推因为不落上下文
- **圈画/选中(ink/sel)——本地只记状态,系统消息注入已收归 relay** @ _rtcHandleUp 'ink'/'state' :1919-1936;ikHint :2841-2843;工具 ctx.ink/ctx.selection 上行 :3054-3055 · 传输:①dc 路只在 ctx 快照里给一句 ikHint 提示『先调 see_ink』;②笔迹本体随工具调用 ctx.ink 经 /voice-tool 上行;③独立 system 注入在 relay(voice_realtime_relay.p · 去重:_rtc._inkFp=page:strokes.length 边沿;inkDirty 在 see_ink/see_page/see_figure 成功后复位(:3089);ikHint 笔画数进 c · ⚠ 任务提到的 inkText 符号在本文件不存在(状态是 _rtc.ink/_inkFp/inkDirty)。已知不对称:dc 前端不注圈中文字(用户拍板几何提取不可靠),只提示 see_ink 看真实圈画;relay 路才有 sel/ink 的 system 注入
- **创造物库告知 rcHint(告知+#id 句柄,内容 recall_creation 取)** @ _rtcCreFetch :2802-2808 + 注入拼装 :2832-2839;工具完成强刷 _chipEnd :663 · 传输:随 ctx 快照同条 system(不单发) · 去重:进 ctx fp(cre.length+cre[:24]);15s 拉取节流 · ⚠ 与文字侧同源 /api/assistant/creations-brief(_creations_recent_line),替代旧 __lastCheckResult 专线——两路对称
- **长按带入(pin)覆盖式快照** @ _pinSync :1121-1136(_rtcSys 调用点 :1134) · 传输:dc system:『当前带入清单(以本条为准,旧声明作废)』或『已移除全部』 · 去重:覆盖式快照+fp:sorted(labels).join('|') 存 _pins.fp(:1126-1128),最终状态没变=零注入;同 cid 多实例(浮层/侧栏/收藏夹)不重复注入(:1157- · ⚠ fp 更新在 !_rtc.on 早退之前(:1127-1129)——通话外改 pin 会推进 fp 却不注入,下通电话模型可能不知已带入内容(除非再变一次);文字模式 pin 走 send 的 ctx.pinned 另一条管线(:1183)
- **配图✕删除通告** @ _imgGoneNote :1200-1214(_rtcSys :1212;触发点 :1237) · 传输:dc system append-only(用户设计:不改历史——改前缀=prompt 缓存全失效);dc 未 open 不发(哑路教训),环里留底 window.__vcRemovedImgs(-8) · 去重:_goneBuf 800ms 合并;注册表不删(编号永久可解析防裂图) · ⚠ dc 前端独有语义(『找错了/重新找』指代消解);dc 没开时只留本地底账不补发=重连后模型不知道删过
- **工具结果喂回(_resFeed 精简)** @ _rtcTool :2961-3115,主回填 :3111,精简 :3093-3097,上限截断 :3107-3108 · 传输:dc conversation.item.create type=function_call_output(→再 _rtcRespCreate;res.silent 且未开『工具口头回报』=静默入库不发言 :3112) · 去重:非指纹,是**预算精简**:cards 有 cards_brief 就删 cards 全文、images 有 found_brief 就删 images(图 URL 挤爆预算),整体截 1800;截断 · ⚠ res._vision 必删(:3105):图像由后端 sideband(body 带 rtc_call_id :3080)直注 OpenAI call,绝不经 dc——SCTP 单条上限 Safari≈64KB,超限发送直接关 dc=通话哑死。特殊短路:wait_for_user 回'{}'不 create(:296
- **承诺核查(宣称做卡/笔记却没调工具→系统代提交+告知)** @ response.done 分支 :3313-3338(_rtcSys :3335) · 传输:①程序直接 fetch /voice-tool 代提交 make_anki/make_note(fire-and-forget,60s abort,不回填 function_call_output);②dc system 告知『系统已代为提 · 去重:scoldT 60s 冷却;turnToolAny 用户轮作用域:speech_stopped/打字轮清(:3224/:1941),function_call_arguments.done **发起即 · ⚠ dc 前端独有守卫;种子=本轮 _lastU+AI 口头总结截1600+recentTools(-4),制卡内容由后台模型自己判断(语音模型只是扳机)
- **重连快照(历史回放:摘要+近几轮)** @ _rtcInjectHistory :3365-3379(_rtcSys :3378);挂点 dc.onopen :3551 · 传输:dc system 单条:『通话重新接通。[此前对话的摘要≤2000]+[最近10条,每条260字]——延续语境,不要重新打招呼』;来源 /api/assistant/history?compact=1(EPUB 经 __asstHistU · 去重:每连接一次;fresh 标志一次性消费(:3511) · ⚠ ㊲ 压缩视图替代全量原文灌注(官方 8.4 形态);与会话内压缩(下条)是两套:这条跨连接、下条同连接内
- **会话内压缩(摘要顶上+批量删旧轮)** @ _rtcCompactNow :3164-3191;触发 :3357-3358;账本 :3194-3196 · 传输:dc conversation.item.create previous_item_id='root' 插 sum_ 固定 id 的 system 摘要(≤1500)+ conversation.item.delete 批量删除近8条以外全 · 去重:lastCompact 90s 低频保护;items<12 不压;**无 summary(服务端 skip)绝不删**并重置冷却——否则历史归零没东西顶上;摘要不入 items 账本免排序错位 · ⚠ 压缩后清 _pageFp/_inkFp 期望 2s 轮询重推,但拉模式下轮询只更新本地态、真正的注入指纹 _sentCtxFp 没清——被删掉的 ctx 注入在状态不变时不会补(见第 1 条)
- **打字用户消息(侧栏输入直达实时模型)** @ __vcSendText :1876-1884 → shim → _rtcHandleUp 'text' :1937-1943 · 传输:先 _rtcInterrupt(response.cancel+output_audio_buffer.clear,防 conversation_already_has_active_response)→ _rtcFlushCtx → dc · 去重:无(每条都发);turnToolAny 清零标记新用户轮(:1941) · ⚠ 与语音轮对称:语音轮的 create 由 relay 仲裁(手动挡),ctl 断时前端 speech_stopped 直发/6.5s 兜底(:3223-3242);打字轮恒由前端自己 create
- **看图类工具的视口截图上行(注入素材,经后端 sideband 进模型)** @ _needShot 判定 :3057-3068;_captureView :2885-2909;ctx.view_image 上行 :3068 · 传输:html2canvas 截可见区(≤1600px 长边、base64≤900KB 质量阶梯)→ 随 /voice-tool 的 ctx 上行 → 后端带 rtc_call_id sideband 注入 OpenAI call(图绝不走 dc · 去重:无指纹(每次工具调用现截);尺寸/质量护栏防超服务端消息上限 · ⚠ EPUB 主路/PDF 兜底;文字侧栏是 EPUB 预拍,语音走 need_shot 按需

## doubao-relay 服务端 (voice_realtime_relay.py handle_browser, L3373-3761)

总体机制=「三层分频注入」(v3-⑭ 用户 transformer 洞察定调):①SP 只装整场不变+跟页走的内容(角色/协议/全量工具目录/页文本/插图描述/生词),经 StartSession(100) 首发 + UpdateConfig(201) 热更,md5 指纹+251 ack 确认制去重——SP 唯一变化源是翻页,因为改 SP 前缀=其后整个对话历史的前缀缓存全废;②易变状态(选中/笔迹三态/带入图/焦点)整段撤出 SP,合并成『(系统状态更新:…)』快照经 ConversationCreate(510) 追加到对话末尾(前缀不变缓存全保),1.2s 防抖+fp(文本+ink_ver) 去重,SP 里只留一段稳定说明文案『以最新一条为准/一条都没有=什么都没有』;③工具结果经 502 external_rag 喂回,slim 精简(剔 b64/cards→cards_brief/images→found_brief/_RAG_LIMIT 按工具限长)。工具触发=S2S 整轮只输出 {"tool":...} JSON,relay 550 增量检测完整即 fire(559 全文兜底,reply_fired 去重),确认语 relay 代播(PCM md5 缓存零费),JSON 段音频时序法静音丢到 359。已知坑:cards 全文截断残 JSON=模型不知道做过卡(→喂 brief、tool_status.result 给全供前端渲卡);tool_status.rag 截断=预览卡不弹根因;总页数不进 SP(跨书报错数)改由工具结果携带;510 注入轮轮计费所以限长(深思答案 500 字/摘要 700 字);豆包 S2S 不吃图,视觉全靠 see_* 工具经 webapp 视觉模型转述(OpenAI 路才有图像直喂);450 在豆包路只是 SP 兜底重推时机而非内容注入点。book 状态全部来自前端控制 WS 上行:t=page(页码+EPUB 视口文本直供)/t=state(sel/focus/figs_n)/t=ink(strokes+shot+ink_ver)/t=text(501)/t=cfg/t=tool_abort。

**注入点清单**:
- **SP(system prompt)构造:角色+工具协议纪律+全量工具目录+页码规则** @ _role_text L357-415(role+= 那段);目录 _fetch_tools_lines L896 开话拉一次+deep_think/recall_study 虚拟工具行 L3428-3430 · 传输:StartSession(100) L3436 首发;之后 UpdateConfig(201) _push_sp L3450-3465 热更 · 去重:md5 指纹 + 251 ConfigUpdated ack 确认制:confirmed 不推、pending 5s 内不重推、251 到达才前移指纹(L3443-3465,3628-3630);St · ⚠ v3-⑭ 铁律:易变状态整段撤出 SP(改 SP 尾一个字=其后全部历史前缀缓存报废);总页数故意不进 SP(跨书报错数根因),改由工具结果携带『全书总页数』L389-394
- **页文本(本页文字直塞 SP 中层)** @ _fetch_book_ctx L257-267(GET /pdf/api/page-text,1800字)→ SP L397-400;EPUB 视口文本前端直供 t=page 带 text L3539-3545(2000字) · 传输:进 SP → StartSession(100)/UpdateConfig(201) · 去重:随 SP 指纹走(页没变=SP 不变=不推) · ⚠ 豆包 12K 上下文所以 1800/2000 截断;OpenAI 路同款内容在 _oa_instructions
- **插图离线描述 + 本页未掌握生词(直塞 SP)** @ _fetch_book_ctx L268-275(GET /api/assistant/voice-ctx)→ SP L401-409(figures 前4张共1200字;vocab 前30词) · 传输:进 SP(100/201) · 去重:随 SP 指纹 · ⚠ 老 webapp 无 voice-ctx 端点时静默降级(try/except L268-277)
- **实时状态事件:选中/钉住焦点/带入图数/笔迹三态(v3-⑭ 状态撤出 SP 的替代通道)** @ _state_event_text L418-442 生成『(系统状态更新:…)』合并快照;_inject_state L3486-3493;_push_state_debounced L3495-3509;开话已有状态立即注 L3511-3513 · 传输:ConversationCreate(510) 追加 user+assistant 对到对话末尾(_inject_memory L3474-3479)——前缀不变历史缓存全保 · 去重:fp=状态文本+ink_ver(L3488-3491):字面相同但又画了新笔迹也重注;状态没变不注(省 token 防灌水) · ⚠ OpenAI 路同哲学用 conversation.item.create system 消息(L1220/2057);RTC 路自己的 _inject_state L3025
- **选中/焦点/chip 上行(book.sel/focus/figs_n)** @ up() t=='state' L3557-3566 · 传输:前端控制 WS JSON → relay book 字典(不直达豆包,经 510 状态事件+随工具 ctx) · 去重:双层:前端指纹 + relay any(book[k]!=v) · ⚠ sel 截 300 字、focus 200 字
- **实时墨迹 + EPUB 笔迹合成图上行(book.ink_strokes/view_shot/ink_ver)** @ up() t=='ink' L3567-3580;换页作废 L3550-3552(ink_strokes=[]/view_shot=None/ink_seen_ver=0) · 传输:前端 syncInk 控制 WS → book;strokes[:60] 存、shot 存 view_shot、ink_ver+1 · 去重:前端指纹去重,到达即真变化;ink_seen_ver 在 see_ink/see_page/see_figure 成功后记录『看过这版』L877-878 · ⚠ PDF 不设 view_shot(走服务端裁图);豆包 S2S 不吃图,墨迹只经 see_* 工具由 webapp 视觉模型转述——OpenAI 路有 ㉕ rt_image 图像直喂(L1705-1712),豆包路无此对等物
- **工具结果喂回(主路)** @ _run_voice_tool L855;精简管线 L842-854:剔 _ 前缀 b64/client_action → cards 全文换 cards_brief(全文截断残 JSON=『AI 不知道做过什么卡』根因)→ images 换 found_brief(图 URL 挤爆预算)→ _RAG_LIMIT 按工具限长 L696-701(视觉/阅读类 1600-2200,列表类 600-900,未知 1400)+『口头猜测一律作废』尾注 · 传输:dws 502 帧 {"external_rag": [{title,content}]}(T_FULL_CLIENT enc) · 去重:cacheable 只读工具按 _ckey=tool|args|page|ink_ver 缓存(≤20条 L744-750/870-876);命中时发『此前同样查询的结果直接复用』502 + clie · ⚠ OpenAI 路对等物=function_call_output;502 是豆包专属协议
- **工具异常路的 502:解析失败反馈自愈 / recall_study 空记录 / 用户中止通告** @ L824-825(feedback 喂回让 S2S 重出合法 JSON)/ L767-769(记录为空如实说)/ L3589-3591(t=tool_abort『用户手动取消,别道歉太多』) · 传输:同 502 external_rag 帧 · 去重:无(低频一次性) · ⚠ 无
- **tool_status 帧回前端(状态按钮+侧栏详情卡,v3-⑯ 全程可查)** @ running L566/808;done L634-638/860-869(含 cmd 原始指令/ctx_brief 页码墨迹选中概要/rag 喂回文本1600/vision 实际发给AI的图≤3张/result 制卡完整体 slim_full/result_brief 400);error L642/823/884;aborted L3594;cached 复用 L792-795 · 传输:bws JSON {event:'tool_status', payload:{...}} · 去重:缓存命中也重发 vision(#8:否则看图类复用时无图)L794 · ⚠ vision b64 <1.3MB 各≤3张才带 L836-839;rag 截断而 result 给全(rag 截断残 JSON=预览卡不弹的根因)
- **深度思考/学习回顾流式代播 + 答案记忆注入** @ _run_deep_think L544-651(调 /api/assistant/chat SSE,answer 按句 _SENT_SPLIT 切);_inject_500_memory L654-661;recall_study 分支 L759-782(_study_digest 学习记录拼进 question) · 传输:ChatTTSText(500) start/content/end 分片边生成边播(_say L553-560)+ 550 字幕同步;答案≤500字经 510 注入记忆(ChatTTSText 是否进上下文文档未明说→510 保追问不失忆 · 去重:无缓存(每次真跑);tool_status done/error 汇报 · ⚠ 深度模型选型:action-prefs deep 面板优先、凭证 deep_model/deep_effort 兜底 L570-584
- **工具确认语代播(v3-⑪ PCM 缓存)** @ _say_ack L669-690;_ACK_TEXT 固定集合 L516-522;录音窗口 350 开录 L3651-3652→359 存盘 L3664-3672 · 传输:首次 ChatTTSText(500) 合成并 tee 录下行 PCM 存 state/doubao-ack-pcm/<md5>.pcm;之后同句 relay 直接分片回放给前端(零合成费零延迟)+ 550 字幕补发 L755/763/80 · 去重:md5(文本).pcm 文件缓存;450 打断丢弃残缺录音防缓存半句 L3637;<0.1s 不存 L3667 · ⚠ 无
- **工具调用 ctx 直塞(不进 S2S,随 voice-tool POST 走)** @ L809-817:ctx.ink=ink_strokes / ctx.view_image=view_shot(EPUB)/ ctx.selection=sel / ctx.prev_ink_desc=last_ink_desc(see_ink 补笔对比 L816-817,回填 L879-880) · 传输:POST /api/assistant/voice-tool 的 ctx 字段 · 去重:无(每次带当前最新) · ⚠ 与侧栏助手同口径(see_ink 不再依赖 sidecar 保存时机)
- **开话历史/记忆注入** @ _start_session_payload L459-505:dialog_id 跨通话接续(最近20轮,DIALOG_ID_FILE;150 帧存 L3622-3626)+ dialog_context=助手最近3轮严格交替 QA 对(_qa_pairs L287-299,来自 /api/assistant/history L278-281);fresh=1(🧹新话题)清 dialog_id 且不带历史 L489-494 · 传输:StartSession(100) payload dialog 字段 · 去重:QA 对各截 400 字、只取 3 对 · ⚠ 豆包对话记忆仅 20 轮且感知不到自己丢了什么→recall_study 工具补(L526-528)
- **长对话摘要护栏(v3-⑩F)+ 用户输入记录** @ 559 自然轮记 qa_log L3722-3732:攒 26 轮→最旧 12 轮拼接压成 700 字经 510 注入;t=text 打字→501 ChatTextQuery L3529-3533;451 语音终稿→book.user_q L3639-3644 · 传输:510 ConversationCreate(摘要)/ 501(文本 query) · 去重:压缩后从 qa_log 移除(不重复压);拼接式不调外部模型 · ⚠ 豆包 12K 硬限已封顶费用,这条主要保认知连续
- **speech_started(豆包 450 用户开口)边沿动作** @ down() ev==450 L3635-3638 · 传输:触发 _push_sp(UpdateConfig 201)+ book.deep_abort=True + ack_rec=None · 去重:SP 指纹确认制:没变且已确认的开口不再推(v3-⑩B,原『每次开口无条件重推』改为『没确认送达才重推』) · ⚠ ⚠ 豆包路 450 只是 SP 兜底重推时机,不注入内容本体(内容注入在事件边沿:state/ink/page/工具完成)——grok 路是本地判定 _grok_turn_start L1818,OpenAI GA 路是 input_audio_buffer.speech_started L2147 且每轮 respo
- **静音/字幕重组(JSON 工具轮不外泄)** @ 550 增量 L3673-3702:JSON 开头一出现 suppress+drop_audio(时序法:JSON 固定在末尾,fire 后音频≈JSON 段丢到 359)+ 字幕撕裂前缀 hold 扣发 L3686-3693;350 tts_type 区分 chat_tts_text/external_rag 绝不丢 L3647-3650;_is_cmd_sent v1 兜底 L704-713(实测 350.text 恒空基本不触发) · 传输:前端 550 字幕由 relay 重组下发(原帧不转发);音频帧按 drop_audio 闸 · 去重:suppress/sent_len per reply_id,559 清理 L3733-3735 · ⚠ 无重连快照:handle_browser 一连接=一通话,断线即终(#290 重连快照在 RTC 路 book._over L2716)

## 文字侧栏 send-ctx (rc-assistant.js send → POST /pdf/assistant/chat SSE)

总体机制:文字侧栏是「send 时定格快照」模型——ctx() 每次发送现采(adapter.getContext 包 __voiceContext + 便签合并 + visible_text 补齐 + pins 快照 + 现拍 view_image),整包塞进 POST /chat 的 context 字段;后端把 _sys_prompt 按【当前页面】锚切两半,静态规则走 --system-prompt(prompt 缓存),动态 ctx 经 _ctx_block+_pinned_lines 拼进每轮 user message。与 dc 路(语音)的事件边沿注入(_pinSync 覆盖式快照+fp指纹、_imgGoneNote 删除通告)是两套哲学:文字路无指纹、靠每轮重发+「发完即清」(figures/notes)防重。三个坑:① __vcRemovedImgs 环只写不读——✕删图信息在文字路完全丢失(dc 未 open 时语音路也丢),注释里的「环里留底」是死代码;② recent_tools 文字路不上送,制卡资料块退化为最近6轮对话文本;③ pinned 不落历史、每轮进 ctx block,靠 _pins 状态中心(label 唯一化+cid 跨实例)在前端去重,后端 _pinned_lines 只做截断不去重。另注意 want_viewshot 是纯前端标志(send 前删除),no_book 剥离只删「书本大上下文」保留显式 chip。

**注入点清单**:
- **基础阅读器上下文(书/页/选中/选中所在句/focus_sel/可见KG节点/生词/langs/read_mode/page_offset)** @ rc-assistant.js:1408 ctx() → pdf-adapter.js:68 getContext()(只读包 __voiceContext,legacy 回退直连);send 定格在 rc-assistant.js:1848 · 传输:POST /chat body.context(send ctx 字段,JSON) · 去重:无,每 send 全量快照;隐式选中在 1866 升格为带✕的 focus_sel chip 让用户可见可取消 · ⚠ 后端 assistant.py:4893 _sys_prompt 拼【当前页面】/选中/所在句,_ctx_block:5181 只取尾段拼进 user message(静态规则走 --system-prompt 缓存)
- **页文本/视口焦点 visible_text** @ rc-assistant.js:1416 补 _visibleText()(视口相交页 __charBoxes 拼文本 ≤1000字) · 传输:ctx.visible_text · 去重:无(每轮重发);no_book 时 1859 删除 · ⚠ 后端 5000-5003 注入「★屏幕上正在看的部分」截 900 字;不落对话历史
- **圈画/墨迹 ink(当前页内存笔画)** @ 经 __voiceContext 进 ctx.ink;后端 assistant.py:5011 _text_under_ink 算圈下文字 + see_ink 引导 · 传输:ctx.ink(笔画数组) · 去重:无 · ⚠ 文字/语音两路均有;see_ink(2886) 优先吃前端 view_image 局部截图
- **带入的图 figures + 便签 notes(双击便签:无笔画走 notes 文字通道≤4,有笔画 kind:'note' 并入 figures≤6 视觉通道)** @ rc-assistant.js:1419-1432(读 __noteAttached);pdf-adapter.js:73 每图补 opaque ref{kind:'pdf',page,box} · 传输:ctx.figures / ctx.notes · 去重:发完即清=一次性携带,下一条不再重复;历史落库 meta.figures 只留6键(assistant.py:6461),回放走 _ctxCard(2629) · ⚠ 后端 fig_line 4923-4944/note_line 4946-4958;kind:'note' 由 see_figure 认 note_id → _note_composite_png 现场合成
- **长按带入卡片 pinned(97设计:文字模式同样入上下文)** @ rc-assistant.js:1849 读 window.__vcPins()(rc-voicecall.js:2164,导出 _pins.map)→ sentCtx.pinned ≤8项{label,text≤2500} · 传输:ctx.pinned → 后端 _pinned_lines(assistant.py:5199)拼「【用户长按带入的卡片】」label≤60/text≤2000;no_book 也保留(5192/5196) · 去重:_pins 状态中心(rc-voicecall.js:1118):label 唯一化(同名卡加·2)+ cid 卡片稳定编号跨实例去重(浮层/侧栏/收藏夹同卡处处高亮不重复注入);文字路**无 fp  · ⚠ 与 dc 路机制不对称但语义等价:dc=覆盖式声明,文字=每轮快照
- **覆盖式 pin 快照注入(dc 路,对照参考)** @ rc-voicecall.js:1121 _pinSync:防抖1.2s + fp指纹(sorted labels join'|')没变=零注入;变了发一条「当前带入清单,以本条为准旧声明作废」 · 传输:dc _rtcSys(conversation.item.create system)——非本路;仅 _rtc.on 时发 · 去重:fp 指纹 · ⚠ 文字侧栏不走此机制;_pinToggle(1176) 每次都调 _pinSync 但 _rtc.on=false 时静默返回
- **删除通告 removed_imgs(✕删配图)** @ rc-voicecall.js:1201 _imgGoneNote:800ms 合并连删 → dc open 时 _rtcSys 发「用户点✕移除了…别再展示」;同时写 window.__vcRemovedImgs 环 slice(-8) 留底 · 传输:dc _rtcSys(语音路)/ __vcRemovedImgs 环:仅写入 · 去重:环容量8;800ms 合并 · ⚠ **实锤不对称:__vcRemovedImgs 全仓无消费者**——rc-assistant.js send 不带、assistant.py 无 removed_imgs 字段。文字侧栏 AI 永远不知道用户删了哪张图(说「找错了/重新找」无所指);dc 未 open 时语音路也丢(注释自认『环里留底』但没人读)
- **视口截图 view_image(插入页/EPUB墨迹等服务端渲不出的内容)** @ rc-assistant.js:1888-1898:adapter 声明 want_viewshot(pdf-adapter.js:80 视口有 .pdf-upage 时)→ await captureShot(截具体元素)/captureView → sentCtx.view_image={media_type,b64};want_viewshot 标志 1898 删除不上送 · 传输:ctx.view_image(base64 内嵌 JSON POST) · 去重:每 send 现拍,不缓存不落历史 · ⚠ 后端消费:see_ink(2886 优先于服务端 _ink_focus_image)、_t_see 兜底(2743)、make_paper 自建页(1900)
- **工具结果 recent_tools(搜过的网页/配图随制卡走)** @ 后端 _card_extra(assistant.py:2161)读 ctx.recent_tools;上送点仅 rc-voicecall.js:3056(/voice-tool 的 make_anki/make_note)和 3330(/route-text) · 传输:语音路 ctx 字段;文字侧栏**不上送** · 去重:slice(-4) · ⚠ **已知不对称**:文字侧栏 send 不带 recent_tools;文字路制卡的资料块靠 _card_extra 的 _convo_load 最近6轮兜底(2173),刚搜的网页摘要/配图URL 若没进对话文本就进不了卡
- **no_book 剥离(「书页」开关点暗)** @ rc-assistant.js:1854-1862:删 current_section_idx/section/selection_sentence/selection_anchor/visible_text、page=0;保留 selection/figures/notes/focus_sel(带✕的 chip) · 传输:ctx.no_book=true(2008 重连前再补一次) · 去重:仅影响本次发送,历史旧消息 chip 不动 · ⚠ 后端 _ctx_block:5183 no_book 分支=通用助手 + _explicit_attach_lines(5154)把保留 chip 当独立片段注入
- **对话历史/落库** @ assistant.py:6455-6463:history=最近6轮(role/content/page/pages/book/file_rel/selection);user 消息进 agent 前 _convo_append,meta 带 page/pages/book/file_rel/selection/figures(6键白名单) · 传输:服务端自取(_convo_load),不经前端;_format_history 拼【最近对话】带 _loc_tag 位置标注(供『刚才那页』定位) · 去重:pinned/visible_text/view_image/notes **不落历史**;figures 落库只留定位键;答案落库剥[语气:XX](6414) · ⚠ 另有 /compact-history 滚动摘要(6527)供语音重连回放,文字路 history 恒6轮原文
- **传输总线/重连** @ rc-assistant.js:2009-2012 首连 body={message,context,rid,turn_id,media_prefer,force_effort,force_model,voice};重连={rid,from:evSeen};后端 6423 assistant_chat:detached _chat_worker + 事件缓冲,410=过期走历史恢复 · 传输:POST /pdf/assistant/chat → SSE(meta/tool/tool2/answer/actions/trace/undo/notice/done) · 去重:rid 幂等(已存在 job 直接续读)+ uid 校验(6469) · ⚠ media_prefer/voice 是 body 顶层字段,后端 6450-6452 回塞进 ctx(media_prefer→配图/视频偏好提示,voice_mode→口语化/语气标签段);_base/_uid 服务端注入(6453-6454)

## 前端→relay 上行 shim

总机制:同一套发送函数(setPage/syncState/syncInk/__vcSendText,均带指纹去重、仅 s2s 模式、2s 轮询+开口瞬间即时同步)对着「全局 ws」发四类 JSON 小消息;豆包/openai-WS 路 ws 是真 WebSocket 直达 relay 对应 handler,WebRTC(2.1)路 ws 被 _rtcShimWs 顶替——字符串①原样镜像给 _rtc.ctlWs(→relay handle_rtc_ctl._up,voice_realtime_relay.py:3288,这就是统一端口要复用的上行通道)②本地 _rtcHandleUp 翻译成 dc 动作(page=拉模式只记状态零注入、text=interrupt+flushCtx+item.create+response.create)。relay 三个接收器落 book 的字段:rtc-ctl 最纯(只存不注入:page/vtext/page_text/sel/ink_strokes/view_shot/_ink_fresh/_ink_fp/_dirty3,注入统一在 speech_started 边沿做);openai 通用分支存+dc system 增量注入(前缀不动保 cache);grok 单通道只存+_dirty(instructions 每轮免费注入,continue 拦掉通用注入防双份钱);豆包存+UpdateConfig/StateDebounce 热更 SP。核心坑:①发送侧模块级指纹跨通话/重连残留→第二通收不到(123/133 两次翻车,重连必须清 _syncedPage/_stateFp/_inkFp);②dc 未 open 时 _dcSend 静默丢→曾以为在注入实际从没落地(133),注入职责已收归 relay 别再走 dc;③ink 消息无 sel 字段别顺手动 book['sel'];④text 在 WebRTC 路是双投递(dc 真发+ctl 镜像做话轮卫生),统一端口时别让 relay 再代发一份;⑤rtc-ctl 的 _up 对 bytes 直接 continue(音频走 WebRTC 轨),复用此通道传二进制需另开路。

**注入点清单**:
- **shim 机制本体(顶替全局 ws)** @ rc-voicecall.js:1885-1893 _rtcShimWs(),:3594 ws=_rtcShimWs(),ctlWs 赋值 :3488/清理 :3481,:3632 · 传输:send(string) 双投递:① 原样镜像 _rtc.ctlWs.send(data)(控制 WS→relay handle_rtc_ctl,㊺P2:relay 是工具执行者,ctx 要最新笔迹/选中/页码);② JSON.parse→ · 去重:无(去重在各发送函数;shim 只转发) · ⚠ shim readyState 恒 1、close 为空——发送方以为总是连着;ctlWs 断了镜像 try/catch 静默丢
- **page(页码+总页数+视口文本)** @ 发送 rc-voicecall.js:3891-3906 setPage(page,vtext);relay 收:voice_realtime_relay.py:3297-3314(rtc-ctl)/2029-2048+1971-1998(openai/grok)/3534-3556(豆包) · 传输:{type:'page', page, total?, text?:视口文本≤2000}。rtc 模式恒带 visible_text(拉模式恒推保 pendText 最新);豆包只在翻页时发且不带视口流(滚动流会打 dialog 缓存)。r · 去重:发送侧 o._syncedPage + _vtFp(视口文本 长度+首尾40字 指纹);123 坑:去重键必须独立于 o.page 且连接成功时清零(否则 relay 重启重连后 page 永不再发, · ⚠ shim 本地 _rtcHandleUp(1904-1918)是拉模式:翻页只更新 _rtc.ctxPage/pendText 一个字不发,停留 8s 才预拉页文本(_ptPreT);total 字段只有 rtc 路消费(131:AI 答不出总页数),豆包/openai 分支不读 total
- **state(选中文字/焦点chip/带图数)** @ 发送 rc-voicecall.js:3911-3920 syncState;relay 收:voice_realtime_relay.py:3327-3329(rtc-ctl)/2049-2063(openai)/2000(grok)/3557-3566(豆包) · 传输:{type:'state', sel(调用处截≤500), focus, figs}。rtc-ctl:只落 book['sel']=sel[:400]+_dirty3(system 注入收归 relay,在 speech_started 时 · 去重:发送侧 _stateFp=JSON.stringify(state) 指纹;首轮全空不发(SP 本来就没有);relay 端再比对一层。133 坑:模块级指纹跨通话残留→同页面第二通电话 syncSt · ⚠ focus/figs 只有豆包分支消费;rtc-ctl 与 openai 分支只读 sel——统一端口复用时 focus/figs 会静默丢
- **ink(实时墨迹 strokes + EPUB 视口合成图 shot)** @ 发送 rc-voicecall.js:3950-3971 syncInk;relay 收:voice_realtime_relay.py:3330-3341(rtc-ctl)/2064-2097(openai)/2001-2008(grok)/3567-3580(豆包) · 传输:{type:'ink', page, strokes.slice(0,60), shot?:{media_type,b64}}。shot 仅 EPUB/HTML(reflow)且有笔画时经 RC.captureView 截视口合成图(后端拿 · 去重:发送侧 _inkFp=page:笔画数:JSON长度;首次空态只记指纹不发;换页 _inkFp='' 作废(setPage:3900)。relay 各路再各有一层指纹 · ⚠ ⚠ ink 消息没有 sel 字段,relay 严禁碰 book['sel'](2064 注释,曾出过bug);133 大坑:WebRTC 路曾想经 dc 直发 system 注入省 Pi 往返,实测**从没落地**(_dcSend 在 dc 未 open 时静默丢弃),注入职责已全部收归 relay,shim 本地(
- **text(通话中打字提问直达实时模型)** @ 发送 rc-voicecall.js:1876-1884 window.__vcSendText(rc-assistant.js:1840 send 拦截调用);shim 本地处理 1937-1943;relay 收:voice_realtime_relay.py:3318-3326(rtc-ctl)/2098-2107(openai)/3529-3533(豆包) · 传输:{type:'text', content}。**WebRTC 路是双投递语义**:真正投递走 shim 本地 _rtcHandleUp→先 _rtcInterrupt()(response.cancel+output_audio_buff · 去重:无(用户显式动作) · ⚠ 统一端口复用时注意:WebRTC 路 text 的模型投递在浏览器 dc,relay 只收镜像做卫生——若统一后 relay 也代发就会双份
- **辅助上行:cancel/tool_abort/cfg/shot/rtcstats/played_ms** @ rc-voicecall.js:1944-1946(cancel/tool_abort→_rtcInterrupt),4246 pushCfg;relay:voice_realtime_relay.py:2015-2028(openai played_ms 精确truncate+cancel),3581-3595(豆包 cfg→_push_sp 热更/tool_abort→掐 book['tool_task']+external_rag 502+tool_status 事件),3315-3317(rtc-ctl rtcstats→_vlog 学习时间线),3342-3346(rtc-ctl shot:前端回 {type:'shot',shot_id,b64,media_type} resolve shot_fut,无 id 兼容旧前端取唯一 pending) · 传输:各自 JSON 小消息;shot 是 relay 主动要截图(see_page 等工具)后前端的应答上行 · 去重:cfg 由 relay 侧指纹(含 tts)真变了才发 UpdateConfig · ⚠ rtc-ctl 分支没有 cfg 处理(WebRTC 路音色在会话配置里);统一端口要收编 cfg 得补分支
