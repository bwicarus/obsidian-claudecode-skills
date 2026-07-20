# 融合复习卡系统(用户设计定稿 2026-07-21)

> Anki 卡片以**工具结果卡**为载体,贯穿"制卡→编辑→确认→学习→收纳→钉到页面"全生命周期;
> 字幕模式与侧边栏共用同一张卡。美术与现有工具卡(rc-turncard/voiceCard 形态)完全一致化。

## ⭐ 状态机(用户 2026-07-21 纠正定稿,取代早前误解)
```
draft 草稿(保留/修改):可编辑 + [🗑 删除] [✓ 入库到 Anki]
   点入库 = **直接进 Anki**(即使不再操作也没关系);拿 note_id
learn 学习态 = 普通 Anki 卡:正面 → 点击显示答案 → 四档(再来/困难/良好/简单,就是普通 Anki)
   评分 → /pdf/api/review-answer{note_id,ease} → answerCards 真 FSRS + 返回下次到期
collapsed 收起:长条 +「距下次复习 X」倒计时(Anki 冷却时间);可再点→圆球(B3)
```
**关键**:入库是唯一入库口(不是"完成→掌握确认"的三段式,那是我早前误解)。入库后即普通 Anki 卡。
卡片版面后续直接复用到 rc-review 复习页(用户说之后再讨论)。已上线:rc-flashcard 三态 +
review-answer 支持 note_id→card_id + 返回 cardsInfo.interval 倒计时。

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

## 分批实施计划

1. **B1 状态机+单卡**:defer_add 服务端改造;卡草稿→编辑→撤销→完成→学习(翻面设置)→确认入库;turnCard 样式套用
2. **B2 多卡滑动**:scroll-snap 容器;字幕模式/侧边栏共用同一卡实例
3. **B3 收纳链**:确认后长条+下次复习倒计时→圆球(接现有收纳语义)
4. **B4 拖出钉页**:圆球抓手拖拽,便签式内容坐标锚定,侧边栏+收藏夹两个来源
5. ⚙ 设置:翻面模式选项

## 待确认/开放点

- 倒计时数据源:确认(ease)后 Anki 返回的下次到期时间(answerCards 无返回间隔 →
  用 cardsInfo 补查 or FSRS 本地估算;B3 时定)
- 编辑卡的字段模型:front/back/cloze 三类(snippets 响应 anki_cards 已是此结构)
