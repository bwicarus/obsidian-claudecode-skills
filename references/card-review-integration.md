# 融合复习卡系统(用户设计定稿 2026-07-21)

> Anki 卡片以**工具结果卡**为载体,贯穿"制卡→编辑→确认→学习→收纳→钉到页面"全生命周期;
> 字幕模式与侧边栏共用同一张卡。美术与现有工具卡(rc-turncard/voiceCard 形态)完全一致化。

## ⭐ 状态机(用户 2026-07-21 纠正定稿,取代早前误解)
```
draft 草稿(保留/修改):可编辑 + [🗑 删除] [✓ 入库到 Anki]
   点入库 = **直接进 Anki**(即使不再操作也没关系);拿 note_id
learn 学习态 = 普通 Anki 卡:正面 → 点击显示答案 → 四档(再来/困难/良好/简单,就是普通 Anki)
   评分 → /pdf/api/review-answer{note_id,ease} → answerCards 真 FSRS + 返回下次到期
done 已复习:bd 显示正反面+「距下次复习 X」;**形态收纳(圆vc-dot/长条vc-min/方块)一律交 vc-card 外壳原生三态**(制卡卡 _cardPush 加 opts.dot:true),评分后 dockToShell 把倒计时写进外壳 .vc-card-sum + 单卡自动 form('min')收长条。rc-flashcard 绝不自造形态
```
**关键**:入库是唯一入库口(不是"完成→掌握确认"的三段式,那是我早前误解)。入库后即普通 Anki 卡。
卡片版面后续直接复用到 rc-review 复习页(用户说之后再讨论)。已上线:rc-flashcard 三态 +
review-answer 支持 note_id→card_id + 返回 cardsInfo.interval 倒计时。

### ⚠ 状态持久化:入库/评分后刷新变回草稿(2026-07-21 用户实锤,已修)

**症状**:两张卡都点了「✓ 入库到 Anki」,刷新后又变回预览/草稿态。

**根因(两层)**:入库时 `addToAnki` 已调 `_stateSync` 把 `{_st:'learn',_nid}` PATCH 到
`/api/entity/<gid>` 服务端注册表(states),**服务端状态一直存着**(实测 registry 里 card_ 编号的
states 就是 `{0:learn,1:learn}`)。但**回放这条路读不到它**:turnCard 的 cards part 落库的是
**制卡时的草稿快照**(全 draft),刷新走 `RC.flashcard.mountDrafts` → 而 `mountDrafts` **无条件**
把每张卡设成 `_st:'draft'`,从不去 entity 拉已入库状态 → 服务端的 learn 被本地 draft 盖掉。

**修法**:rc-flashcard.js 加 `_restoreStates(container)`——凡 gid 是 `card_` 编号,mount 后异步
`fetch /api/entity/<gid>`,用 states 覆盖各卡 `_st/_nid/_next/_showBack` 并重渲 + broadcast 同 gid
其它宿主。三处 mount(mountDrafts/mountPreview/mountState)末尾都调它。entity states 为空(新制卡)=
保持草稿;有 learn/done=恢复。**后端零改动**(GET/PATCH `/api/entity` 早就对,只是前端从不 GET 恢复)。
这正是 #52 统一编号协议"编号卡处处同状态、跨会话不丢"该有的闭环,之前只做了写(_stateSync)没做读(恢复)。

**验证**:E2E 造真 card_ 编号→PATCH states=learn→mountDrafts(草稿)→挂载瞬间是 draft(有入库按钮)→
600ms 后 _restoreStates 拉回 learn(入库按钮消失、_nid 恢复、变可复习翻面卡)。用户那两张(card_22613b)
的 learn 状态服务端一直在,部署后刷新即恢复。⚠ 前提:gid 必须是稳定 card_(靠 tool_status.result 带
`id`,即上面 relay 三引擎合并修的那条)——fcg_ 本地随机编号的旧卡无服务端状态,无法追溯恢复。

## 待修:语音制卡消息重复(审计 wj62f99wj,scratchpad/card-audit.json)
根因(P0):turnTool 守卫在每个 response.created 复位,但一次制卡横跨两 response(工具在R1、AI汇报在R2)
→ ㊸b 承诺核查在 R2.done 误判"没调工具"→ 幻影补交第二个 make_anki → 双任务/双占位/一成一败并存。
修法:turnTool 改**用户轮作用域**(删 3058 无条件复位,新增 turnToolAny + cardTaskDone 单槽,派发瞬间置位);
㊸b 改判 !turnToolAny && !cardTaskDone + 收紧正则;占位每轮唯一、结果就地替换而非并排 addPart。

## ④ 天气卡形态 + 字幕模式(用户 2026-07-21 明确三点 + 摸底方案)
**用户要求**:① 字幕模式(浮层 vc-cards)只显卡片内容(像天气卡浮层,不套额外工具卡头);
② 侧边栏里卡片包在工具卡片中且**可独立选中**(天气卡的 _pinBind 长按选中);③ 审美与现有卡片一致(vc-if/vc-card 观感)。
**关键差异**(摸底 rc-voicecall):天气卡浮层 _cardPush 用 `_bd.innerHTML=text`(静态HTML);制卡卡是
rc-flashcard 的**可操作状态机**(草稿编辑/入库/四档评分的 DOM+事件)→ 不能只塞 HTML 字符串。
**落地方案**:
1. `_cardPush` 加 `opts.mount(bd)` 回调:建好 vc-card 外壳后,有 mount 就调它填 bd(承载状态机卡),否则原 innerHTML。小改。
2. 制卡结果走 renderInfo 式**双宿主**:侧边栏 turnCard(kind='cards',工具卡内,_pinBind 可选中)+ 字幕浮层 _cardPush(force + mount=挂 rc-flashcard)。现只进 turnCard,缺浮层镜像=字幕不出卡的根因。
3. rc-flashcard 审美:fc-card 观感换 vc-card/vc-if(主题色 --vc-tc、圆角、字号统一);或内容直塞 vc-card-bd(外壳已是天气卡审美)。
4. 可选中:vc-card 自带 _pinBind(长按选中,浮层/侧栏/收藏夹同 cid 同步),制卡卡进 vc-card 即得。

### 卡片身份不变式

- 普通/工具/信息卡的 `cid` 是卡片主键；从侧栏、字幕浮层、收藏夹或页面钉住处重新渲染，只能增加同 `cid` 的实例，不得重新编号。
- 学习卡组的 `gid` 是状态共享主键；其外壳 `cid` 必须与 `gid` 相同。翻面、编辑、入库、评分和选中态在各宿主间是同一份状态。
- 收藏和钉页是“同卡的新宿主”，不是新卡；只有用户明确创建新卡时才允许产生新编号。
**⚠ 风险**:改 rc-voicecall 浮层(通话核心)+ rc-turncard + rc-flashcard;这块历史反复返工(memory:
reuse-existing-cards-not-new / cli-paper-card-design / verify-innermost-child)。宜用新鲜上下文单独批次做。
制卡结果 kind='cards' 现只进侧栏 turnCard,缺 renderInfo 式浮层镜像 → 字幕模式不出卡。
__vcInfoCardEl(rc-voicecall:1256)是静态渲染器无编辑态;renderInfo(1270)双宿主(turnCard+浮层)。
接法待定:让制卡结果也镜像浮层 / 卡壳换 vc-if 天气卡观感。

## 卡片状态机(用户规格原文精神)

```
[生成] → 阅览修改模式(字幕模式+侧边栏同卡)
          · 内容可直接编辑
          · 可整卡撤销
          · ⚠ 未经确认的卡**不进 Anki 库**
   ↓ 点「完成」
[学习模式]
          · 显示正面 → 点击显示背面
          · 翻面方式(翻转只显背面 / 正反同显)= ⚙ 设置项
          · 下方出现掌握度确认按钮
   ↓ 确认
[已完成] → 显示"确认已完成" → 自动收起为**长条**,显示"距下次复习"倒计时
   ↓ 再点
[圆球](与现有工具卡收纳语义一致:卡→长条→圆球)
```

## 多卡批次

- 一次生成 N 张:**优先编辑与确认**;未确认不入库
- 字幕模式与侧边栏**共用一个工具结果卡**,左右滑动切卡
- 切换 = 吸附式:以每张卡**中线**为基准,松手时屏幕中线落在哪张卡就吸附到哪张
  → 实现选型:**CSS 原生 scroll-snap**(`scroll-snap-type: x mandatory` +
  `scroll-snap-align: center`)——正是用户描述的中线吸附,原生 GPU 平滑、iOS 惯性友好,零自研滚动数学

## 拖出钉页(补做:此前设计过但从未实现)

- 从侧边栏/收藏夹把卡**拖出固定到页面上**
- 参照**便签系统**(rc-stickynote 铁律:锚在内容坐标系,严禁 fixed+JS 跟滚)
- 抓手 = 卡片**左上角标志圆球**;松手时圆球位置即卡片最终左上角
- 锚定/挂载 per-reader 经 adapter hook(便签同款)

## 架构映射(复用清单)

| 环节 | 复用 |
|---|---|
| 卡片外观/收纳(卡→长条→圆球) | rc-turncard / voiceCard 标准形态(memory:复用现成卡片别造新的) |
| 字幕模式载体 | rc-voicecall 字幕/浮卡通道 |
| 编辑态 | 卡内 contenteditable + 撤销(assistant-write-action-undo-cards 语义) |
| 学习/掌握确认 | rc-review 的 ease 语义(倒计时数据=answerCards 响应或 review-queue) |
| 滑动切卡 | CSS scroll-snap(原生) |
| 钉到页面 | rc-stickynote 锚定管线,内容换卡片 |
| 草稿不入库 | **需服务端改造**:snippets-to 加 `defer_add`(只生成返回卡草稿,不 addNotes);
  确认后走新端点 `anki-add-cards`(批量、幂等 c_ id)——outbox/攒批天然兼容 |

## 分批实施进度(2026-07-21)
- ✅ **B1 状态机+单卡**:草稿[删除/入库]→入库直进Anki→学习态四档→收起倒计时(defer_add服务端+anki-add-cards幂等)
- ✅ **天气卡形态+字幕双宿主**:_cardPush加mount回调承载状态机卡;侧栏turnCard+字幕浮层镜像+pinBind选中;bare无双壳
- ✅ **B2 多卡 scroll-snap 中线吸附左右滑动**(替代‹›按钮):.fc-track横向滑轨+圆点指示+滚动跟踪idx+就地重渲不跳位。E2E:3卡滑轨/snap:x mandatory/无箭头/编辑不跳位
- ✅ **B3 收纳链(复用 vc-card 原生三态,不自造)**:圆vc-dot/长条vc-min/方块的形态收纳**完全交 vc-card 外壳**——制卡卡两处 push(rc-voicecall 680/706)加 `opts.dot:true, form:'full', icon:'🎴'` 即白得原生三态+`_cycleForm`单击循环(dot→min→full)。rc-flashcard 只管 bd 内容(draft/preview/done);评分后 `dockToShell` 把倒计时写进外壳 `.vc-card-sum` + 单卡自动 `RC.voiceCard.form(host,'min')` 收长条,侧栏/无外壳(closest 落空)优雅跳过。⚠ **我曾误在 rc-flashcard 手搓 fc-ball/fc-collapsed/cycle 一整套形态三态,被用户当场纠正"我们工具卡本就有三态"(第5次"复用现成别造新")** → 已删净改复用。E2E:hasdot/dotBtn/bornFull/cycleSeq=dot→min→full/评分后 autoMin+sum="🎴 已复习·距下次复习3天后"/noSelfBall
- ✅ **B4 拖出钉页(核心已上线,圆球拖拽手势留真机)**:复用 rc-stickynote 便签管线,不自造——note 加 `card` 内容类型(照 video 便签):后端 `/api/notes` POST/PATCH 加 card 字段(白名单 6807 邻位),buildCtl 加 `.rc-note-card` 容器 + `renderNoteCard` mount rc-flashcard(`.rc-note-hascard` 隐藏文字/工具/墨迹层),`createCardAt(x,y,cards,gid)` 照 `createVideoAt` 在 `anchorFromPoint` 内容锚建 card 便签。rc-flashcard 加 `mountState`(保留 _st/_nid/_next 的 mount → 钉页卡保持草稿/学习/已复习态)+ 📌 钉页入口 `pinToPage`(同 gid → 便签卡与原卡联动)。**拖出=按钮/坐标共用 `createCardAt`**,真机圆球拖拽只需接 vc-card 圆点松手坐标(`_bindCardDrag` up 分支 rc-voicecall:2452,松手落内容区→createCardAt)。E2E:createCardAt建便签/hasFcCard/mountState保留done态/文字区隐藏/后端存card+_st=done/清理干净leftover=0。
  - **⭐ 通用钉子模式(用户 2026-07-20 纠正:所有卡都要能钉,不只制卡卡)**:我第一版做窄了(只制卡卡卡内小 📌 + 只制卡卡便签),用户要 vc-card 全类型。重做:
    - vc-card **卡头加 📌 钉子按钮**(所有结果卡,rc-voicecall 卡头 innerHTML)。`pinCardToPage(c,x,y)` 分类型:制卡卡(`bd.__fc` 有 rc-flashcard)→`createCardAt`(保交互+同 gid 联动);其它(天气/搜索/图)→`createHtmlAt`(**html 便签**=vc-card body innerHTML 快照)。钉成功→`_cardClose` 浮层卡**转移**成页面便签(不并存)。
    - rc-stickynote 加第二种便签内容 **html**(`.rc-note-html`/renderNoteHtml/createHtmlAt,`.rc-note-hashtml` 隐藏文字/工具/墨迹);后端 `/api/notes` 再加 html 字段(白名单)。
    - **消失逻辑**:`_armAuto` 加 `c.pinned` 豁免(钉子模式不消失);浮层卡未钉=自动消失(现状即"浮动卡消失")。
    - **拖出即钉**:`_bindCardDrag` up 松手落正文→`pinCardToPage(松手点)`(非正文=anchorFromPoint 落空→不钉、继续落定),治"拖到页面就消失"。
    - 坐标:📌 按钮无坐标→卡位置 + **视野中央回退**(卡落页边/空白时,E2E 实锤根因);拖出有坐标只认该点。
    - E2E:天气卡📌→html便签+内容/制卡卡📌→card便签/浮层转移floatClosed/后端存 card+html/清理 leftover=0。
  - **⭐⭐ 第二轮纠正(2026-07-20)**:①"参照便签系统"=借**锚定机制**,钉住的仍是**卡**(不是变成白便签)→ createCardAt/createHtmlAt 底色改 `#0d1322` 暗色玻璃(applyColor+isDarkBg 自动适配前景,零新机制,观感=卡);②**拖出松手钉页只对 `c.pinMode` 卡**(收藏夹 dock 拖出复制设 `pinned+pinMode`;浮动卡拖动=挪位**不误钉**),钉入点=**卡左上角 rect**(非指针);③📌 按钮仍通用(卡位+视野中央回退)。E2E:钉入便签 darkBody/darkFg、浮动卡拖到正文松手 floatSurvives+floatNotPinned。
  - **⭐ 便签/卡片内容注入 AI 上下文(用户设计 2026-07-20)**:钉在页面上的便签/卡片有绑定对象 → 所有取页上下文的任务把内容插到绑定对象**所在句子末尾**,标「【便签内容：…】」/「【卡片：正面 → 背面】」。实现:`pdf_reader._pin_context_annotations(rel,page,text)`(锚 x,y→PyMuPDF words 最近词→文本中找词→句尾插入;定位不到→文末追加;html 便签剥标签),接在 `assistant._page_text` PDF 分支(一处接入:assistant/voice/make_anki/read_page 全生效,voice._page_text 委托 assistant)。E2E:锚"交换律"→标注恰在其句尾;删便签后零残留。⚠ EPUB 分支(epub_assistant section 文本)未接,待后续。
  - **⭐⭐⭐ 第三轮定稿(2026-07-20,'直接复用字幕模式的卡片代码')**:钉入渲染不再手工拼——`_cardPush` 的卡 DOM 构建段**机械抽出**为 `_cardDom`(形态class/--vc-tc/卡头+按钮/TTS/bd填充),浮层卡与钉入卡跑**同一段代码**;公开 `RC.voiceCard.renderInto(host,spec)`(spec={text,label,isHtml,type,icon,form,mount,onClose,onForm}):vc-pinned+vc-typed(有色磨砂)+钉子钮移除+bd里 vc-if-hd 剥掉(双标题)+✕=onClose(触发便签del)。rc-stickynote renderNoteCard/Html 只调它;便签壳全透明+resize白圆钮/外✕隐藏。**三态收纳同浮层**(dot:true→标记/长条/方块 `_cycleForm` 单击循环,头部点击=循环 2420 同规矩),`onForm`→html.form/card.form 落 sidecar 持久化(sig 排除 form 防重建丢状态),重挂按 form 恢复=**钉在页上的圆球闭环**。**卡宽按页面自适应**:createCardAt/HtmlAt 量 O.mount 容器宽×0.44(240-480),ghost 同式,body 高 auto。修过的坑:.rc-note-ink 手写canvas白块盖卡(内联样式,CSS !important+JS双保险)、卡头色需 vc-typed 消费 --vc-tc(215)。E2E:同源(hd▶/单标题/主题绿)+三态循环+storedForm/loadAll恢复+宽自适应+回归(浮层push/dot cycle)。
  - ⏳ 真机剩:拖出手感(pinMode 松手钉页逻辑已写、headless 测不了 touch)、侧栏 turnCard 制卡卡把手(拖出走 _dragToDock 的卡都通了;turnCard cards part 无把手)
- ✅ **双实例状态同步**:侧栏 turnCard + 浮层 vc-card 两份 rc-flashcard 按 **gid 卡组**联动——两处 push 生成同 `_gid`(rc-voicecall 680/706),侧栏 addPart 与浮层 mount 都带 gid,turncard renderPart 串 `p.gid`;rc-flashcard `_groups[gid]={cards,conts}`:同 gid 实例**共享同一批卡对象** + 状态变化(编辑/入库/评分/删除)`broadcast` 重渲其它实例(edit/单卡 updateSlide、del renderTrack;except self 防光标跳)。E2E:shared/editSync/A入库→B learn+B拿note_id/A评分→B done

## 卡片壳统一(2026-07-21 用户拍板"侧栏统一成字幕卡样子,含三态圆点")

**背景**:工具卡片在三种形态下壳分叉——浮层(`_cardPush`)+钉入(`_renderInto`)已共用 `_cardDom`,但**侧栏**是三套独立壳:工具卡 `mkInflow` 手抄 innerHTML、结果卡 `_infoCardEl` 用 `.vc-if`、制卡卡裸挂 turnCard `.rc-part`。壳分叉=每加一个行为(带 result、状态恢复、拖动…)都得在每条路各接一次,漏一条就是"单形态 bug"(制卡预览不弹、入库状态不恢复都栽在这)。

**方案**:`_cardDom` 已是纯内容壳,新增第三支适配层 **`_renderInflow(host, spec)`**(rc-voicecall,对标钉入的 `_renderInto`):`_cardDom` 建壳 + `vc-inflow`(relative/100%宽内联对话流)+ 去 📌▶✕ + 三态圆点头部循环 + append host;返回 `{el,bd}`,专属交互(pinBind/dragToDock/igWire/mount)由调用方拿 el 自挂。暴露 `RC.voiceCard.renderInflow`。

**三条侧栏路改造**(功能全平移保留):
- **结果卡** `_infoCardEl`:`.vc-if`+两态折叠 → `_renderInflow`(vc-card+三态)。保留 `_infoHtml` 正文、pinBind 选中、dragToDock 拖出、igWire 图✕/单选、`__vcCard` 拖图入卡;主题色按 kind 取(与浮层同源)。`__vcInfoCardEl` 所有调用者(turnCard `kind:card` + `_assetInline` #card)自动统一。
- **工具卡** `mkInflow`(rc-toolchip):手抄 innerHTML → `renderInflow`。保留 hdSplit(标题 `vc-hd-l`+数据流按钮 `vc-flowb`)、pinReg/pinBind、dragToDock;起手长条(form:'min')。
- **制卡卡** turnCard `kind:'cards'`:裸挂 `.rc-part` → `renderInflow`(🎴 制卡 label + 紫 `#b9a8ff` + 三态) + mount 回调塞 `mountDrafts`/`mountPreview`(gid 联动 + entity 状态恢复不变)。

**三态圆点解禁**:原 `_cardForm` 有 `if (f==='dot' && vc-inflow) f='min'`(旧"侧栏不要圆")已删;新增 CSS `.vc-card.vc-inflow.vc-dot{width:40px!important;height:40px}`(压过 `vc-inflow` 的 `width:100%`,圆点态在对话流里是 40×40 小圆)。

**验证**(headless E2E,4 个):结果卡 `__vcInfoCardEl`→vc-card+inflow+三态(full→dot→min)+图✕/单选/__vcCard;工具卡 `toolChip.create`→vc-card+hdSplit+选中+拖出+三态(min→full→dot);turnCard 真实回放路径 addPart(card/cards)→两类 part 全 vc-card,制卡卡入库按钮/多卡/三态不回归。**改动文件**:rc-voicecall.js(新增 `_renderInflow`+暴露+改 `_infoCardEl`+`_cardForm` 解禁+CSS)、rc-toolchip.js(`mkInflow`)、rc-turncard.js(`kind:cards`)。**未反向补**:分析发现侧栏无浮层缺的独占能力(选中/拖出/图/流程浮层都有),故只做壳统一,不新增行为。

## 分批实施计划(原始)

1. **B1 状态机+单卡**:defer_add 服务端改造;卡草稿→编辑→撤销→完成→学习(翻面设置)→确认入库;turnCard 样式套用
2. **B2 多卡滑动**:scroll-snap 容器;字幕模式/侧边栏共用同一卡实例
3. **B3 收纳链**:确认后长条+下次复习倒计时→圆球(接现有收纳语义)
4. **B4 拖出钉页**:圆球抓手拖拽,便签式内容坐标锚定,侧边栏+收藏夹两个来源
5. ⚙ 设置:翻面模式选项

## 待确认/开放点

- 倒计时数据源:确认(ease)后 Anki 返回的下次到期时间(answerCards 无返回间隔 →
  用 cardsInfo 补查 or FSRS 本地估算;B3 时定)
- 编辑卡的字段模型:front/back/cloze 三类(snippets 响应 anki_cards 已是此结构)
