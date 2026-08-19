# ADR:轮次容器(Turn Container)—— 卡片从「事后拼接」改为「容器 + 多次注入」

日期:2026-07-14 · 状态:**已采纳,分阶段实施** · 提出者:用户

---

## 1. 背景:我们为什么会一直出同一类 bug

侧栏里一次「带工具的回答」在时间上天然是这样的:

```
用户说话 ──▶ ①AI 先说一句「我去查一下，稍等」(+ function_call)
              ②工具执行(参数 / 子步骤 / 喂给 AI 的图 / 结果)
              ③AI 拿着结果说出正答
              (④有些工具还自带结果卡:天气/搜索/配图/视频)
```

这**本来就是一个整体**。但旧实现是**事后拼接**:

- ①③ 各自长成一个 `.asst-msg` 气泡(按 `response.created` 断)
- ② 另外长一张工具卡(`RC.toolChip.create`)
- ④ 又长一张结果卡(`renderInfo`)
- 最后用 `absorb()` 把工具卡**追认**给某一张卡,再加个【流程】按钮

于是「谁先到、谁认领谁」变成了一场竞态。2026-07-14 一天之内因此产生的 bug:

| 现象 | 真因 |
|---|---|
| 天气卡和前置语被拆成两张卡 | `_chipStart` 抢先 absorb,`renderInfo` 的结果卡认领不到 chip |
| 前置语显示两遍 | 一轮里有多个 response,而气泡是「全量覆盖」语义;复制文本再另起正文 → 还在流的 delta 又写一遍 |
| 【流程】按钮变白块 | 按钮挂到了野生卡上,没走被验证过的结果卡路径 |
| **刷新后卡片退化成纯文本** | **拼接结果只存在于浏览器内存**;服务端手上有全部原始信息,却只落了个残缺的 `m.card` |

**根因是同一个**:结构是前端**临时拼**出来的,既不稳定,也不持久。

---

## 2. 决策

> **卡片 = 容器。内容靠多次注入。服务端是唯一真相源。**

- 一个**轮次(turn)** = 一次用户输入 + AI 为它产出的全部东西,对应侧栏里**一个容器**。
- 内容以 **part** 的形式**按序注入**容器,而不是事后拼接:

```jsonc
{ "turn_id": "t_ab12", "seq": 0, "kind": "text",  "text": "我去查一下东京明天的天气，稍等。" }
{ "turn_id": "t_ab12", "seq": 1, "kind": "tool",  "tool": "web_search", "label": "网络搜索",
  "args": {...}, "steps": [...], "vision": ["/pdf/api/toolshot/xxx.jpg"], "result": "...", "took_s": 3.9 }
{ "turn_id": "t_ab12", "seq": 2, "kind": "card",  "card": { "kind": "weather", ... } }
{ "turn_id": "t_ab12", "seq": 3, "kind": "text",  "text": "天气炎热且湿度较高，注意补水。" }
{ "turn_id": "t_ab12", "seq": 4, "kind": "meta",  "model": "...", "effort": "...", "voice_mode": "stt" }
```

- 前端只做一件事:**把 part 渲进对应 turn_id 的容器**。
  没有升格、没有顶替、没有 absorb 认领竞争 —— 这类 bug **在架构上不再可能发生**。
- **历史回放 = 取同一份 part 列表,走同一个渲染器。**
  实时与回放**不可能分叉** —— 这是本 ADR 最重要的不变式。

### 不变式(违反了就是 bug)

1. **渲染器唯一**:实时和历史回放**必须**调用同一个 `renderPart()`。任何"只在实时路径上做的处理"都是架构违规。
2. **part 只追加,不重写**:同一 `(turn_id, seq)` 的内容不可变。要改就追加一个新 part。
3. **服务端持有全量 part**:前端内存里的东西**永远不是**真相源。刷新后必须能 1:1 复原。

---

## 3. 为什么服务端"天然"就有这些信息

relay 的 sideband 收的是 OpenAI 的**完整事件流**(它就是靠这个执行工具的):

| part | 服务端从哪拿 |
|---|---|
| `text` | `response.done` 里带完整输出文本 |
| `tool` | relay **就是工具的执行者**:参数/子步骤/结果/喂给 AI 的图全在它手里 |
| `card` | 结果卡本来就是 relay 经 `client_action: renderInfoCard` 下发的 |
| `meta` | 档位/模型/effort 都是 relay 自己的配置 |
| 轮次边界 | **已经有了** —— 手动放行闸的 `_accept_turn`(133)就是权威的"新用户轮"信号 |

所以「服务端权威」不是要新造轮子,而是**把它已经知道的东西组织起来并落库**。

---

## 4. 缩略图(喂给 AI 的图)怎么存 —— 必须先决策

`see_ink` / `see_page` / `see_figure` 会把一张合成图喂给模型。要在卡里显示、且刷新后还在:

- ❌ **base64 塞进历史 JSON**:单张 10 万~50 万字节,几十轮就把历史文件撑爆。**不可行。**
- ✅ **relay 落盘 + part 里只带 URL**:`state/reader-toolshots/<sha1>.jpg` + webapp 路由 `/pdf/api/toolshot/<name>`。
  顺带把 ctl WS 的 payload 也瘦下来(现在是 b64 直传)。

**决策:走 URL。** 落盘按内容 sha1 去重;定期清理(与页图缓存同策略)。

---

## 5. 分阶段实施(**保证过程中不弄坏现在能跑的东西**)

### 阶段 1:前端容器 + 统一渲染器 + 持久化 ← 本次
- 新增 `rc-turncard.js`(共享层):`TurnCard.open(turn_id)` / `.addPart(part)` / `renderPart()`。
- 实时:把现有事件(text delta / tool_status / renderInfoCard)**映射成 part**,注入容器。
  → **删掉** `__asstVoiceCard` 的升格、`_chipStart` 的抢 absorb、`__asstVoiceMoveLead` 的搬运。
- 持久化:轮次结束时把 part 列表 POST 到 `/api/assistant/log`(新增 `parts` 字段)。
- 回放:历史里有 `parts` → 走**同一个** `renderPart()`;没有(旧数据)→ 回落到现有渲染,保证兼容。
- 缩略图改 URL(relay 落盘 + webapp 路由)。

**验收**:实时渲染出来的 DOM,与刷新后回放出来的 DOM,**结构一致**(可用 outerHTML 对比)。

### 阶段 2:服务端权威
- relay 直接产出 part 流(`{event:'turn_part'}`)并**自己落库**;前端退化为纯渲染器。
- 文字 delta 仍走浏览器本地渲染(保低延迟),但在 `response.done` 时由 relay 的权威 `text` part **对账**(内容不一致就以服务端为准)。
- 前端不再需要"组装"逻辑 → 不变式 3 从"约定"变成"结构保证"。

### 阶段 3(可选)
- TTS 背压的播放进度、语音 clip、撤销卡…… 都可以作为新的 part kind 接进同一套容器,不需要再动架构。

---

## 6. 明确不做的事

- **不为 EPUB 另造一套**。`rc-turncard.js` 是共享层(与 `rc-assistant`/`rc-toolchip` 同级),两个阅读器共用 —— 这条是项目铁律(见 `references/unified-control-layer.md`)。
- **不保留"两套渲染路径"**(⚠ 实现打了折扣:`RC.toolChip.absorb`(rc-toolchip.js:866,唯一调用点 rc-voicecall.js:3260)与 `if (!_tcOk)` 的独立落库(rc-voicecall.js:3264)**保留为「轮次容器不可用」时的极端兜底**;容器在时 `_hosts` 为空、absorb 自然 no-op)。阶段 1 落地后旧的"实时拼接"主路径必须删掉,不许留着当常规 fallback ——
  留着就等于允许分叉,今天这类 bug 就会回来。(历史**数据**的向后兼容是另一回事,那个要留。)
- **不把 base64 落进历史。** 见 §4。

---

## 7. 相关

- `references/qa-browser-features.md`(侧栏助手/卡片现状)
- `references/unified-control-layer.md`(共享层铁律:让中间层适应旧代码,不为不同阅读器另造上层建筑)
- memory `assistant-write-action-undo-cards`(撤销卡 —— 未来也应作为一种 part)
- commit `a377b93`(本 ADR 的直接导火索:三个卡片 bug 的修复)
