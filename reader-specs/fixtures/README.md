# 出向事件 fixture 与字段契约(v1)

一份**最小**事件序列,覆盖跨端消费方需要处理的全部形态。每行一个 JSON(JSONL)。
占位符 `dr_<16hex>` / `cmd_<12hex>` 表示"由服务端签发、内容不固定但格式固定"。

> 本 fixture **由真实服务代码验证**(`tests/test_outgoing_fixture.py` 用
> `FocusState` / `DrawingRevisions` / `DirectCommandService` 重放并逐字段比对),
> 所以它不会与实现漂移;改了实现而没改 fixture,测试会红。

## 字段契约

| type | 必备字段 | 语义 |
|---|---|---|
| `page` | `file` `page` `textAvailable` | 当前页锚点;`textAvailable=false` 时才允许退回页图 |
| `focus` / set | `kind` `ref` `seq` | 建立焦点。`kind` ∈ text/image/card/drawing/region |
| `focus` / replace | 同上 | 换对象;`seq` 必须递增 |
| `focus` / cancel | `cancelledObject` `seq` | **显式取消**,并告知被取消的是谁;不得靠字段消失表达 |
| `drawing` / pending | `file` `page` `drawingRevision=null` `ref=null` | 未稳定:**不给引用**,消费方不得使用旧版本 |
| `drawing` / stable | `drawingRevision` `ref` | 停笔约 1 秒后的稳定版本;版本号由内容派生 |
| `page.context` → `visual.drawing` | `freshness` `lastEditedAt` `freshWindowS` `inProgress` `ref` | 页正文事件**附带**的轻量绘图状态,见下 |

### `page_context.text` 里的正文锚定嵌入内容

高亮**不复制到页尾列表**,而是在原文范围内用边界标记包住 —— **正文只出现一次**：

```text
食文化とは、地域ごとの⟦HIGHLIGHT color="#ffd54a" note="重点"⟧気候風土⟦/HIGHLIGHT⟧、文明、民族…
```

卡片/便签等块状内容绑定的是页面元素而非正文字符,因此紧随正文之后,用类型明确的标记包住：

```text
⟦CARD_START type="weather" label="w1"⟧东京明天：27–37℃，晴转多云。⟦CARD_END⟧
```

`type` ∈ `note` / `card` / `anki` / `video`。**卡片是补充其绑定元素的内容**,不要理解成对前后整段的解释。

| 约束 | 说明 |
|---|---|
| **转义** | 正文原有的 `⟦` `⟧` 会转义成 `\⟦` `\⟧`;反斜杠自身转义为 `\\`。见到 `\⟦` 一律当普通字符 |
| **不交叉** | 重叠的高亮只保留先命中的那条,边界标记永不交叉 |
| **定位失败不入正文** | 找不到锚点的高亮**不塞进正文**(否则正文出现两次),改列入 `embeds.unanchored` 并带 `_reason` |

`embeds` = `{highlights, blocks, unanchored[]}`：`highlights` 是**成功锚定**的条数。
`_reason` ∈ `no_text` / `not_found_in_page_text` / `whitespace_mismatch` / `overlaps_earlier_highlight` / `empty_text`。

### `visual.drawing.freshness`(绘图三态)

⚠ **字段名是 `freshness` 不是 `state`** —— `drawing` 事件里的 `state` 已表示 pending/stable(稳定性),
两者值域不同,不要混用。

| 值 | 含义 | 消费方该做什么 |
|---|---|---|
| `none` | 该页无笔迹 | 永远不读综合图 |
| `recent` | `now - lastEditedAt <= freshWindowS`(默认 120s),含正在落笔(`inProgress=true`) | 问题涉及圈画/箭头/算式/手写解答/版面时**直接读最新图**,不要求用户再说一遍"我刚画了" |
| `stale` | 有旧笔迹但超出新鲜窗口 | 除非用户**明确**提到圈画/标注/手写,否则不读图 |

`lastEditedAt` 取最后一次**内容变化**时刻(不是升版本时刻),所以正在画时也报 `recent`。
`inProgress=true` 时 `drawingRevision`/`ref` 为 `null`——**未稳定不给引用**,消费方不得使用旧版本。
正文类问题一律不读绘图资源。
| `command`(独立成功) | `correlation` `ok=true` `emitsEvent=false` | **静默**:成功不产生任何事件 |
| `command-failed` | `correlation` `commandId` `taskId` `step` `retryable` `error` | 失败才发;按 `taskId` 路由回对应语音任务 |

## 消费方硬规则
1. `drawing.state != "stable"` → **没有可用绘图引用**,不要拿上一版顶替。
2. 收到 `focus.cancel` 后,在下一条 `focus.set` 之前**不存在当前焦点**。
3. 没有 `command-failed` **不代表**没执行;独立单步成功本来就是静默的。
4. `retryable=false` 的失败重发无意义(如参数/前置条件问题),应换方案或问用户。

## page.context(翻页稳定 / 页上即时操作)

| 字段 | 含义 |
|---|---|
| `type` / `event` | 都是 `page.context`。两个同名字段是给只认其一的消费方留的兼容位,不得只发一个 |
| `stable` | 恒为 `true`。产生点已经过停留判定,**连续翻页途中不会有这条事件** |
| `book_id` / `file` / `page` / `title` / `kind` | 定位当前页 |
| `text_available` | 顶层冗余一份,消费方不用深挖就能分流 |
| `page_context.text` | 该页**完整文字层**(上限 `PAGE_TEXT_LIMIT`=4000 字) |
| `page_context.text_source` | `pdf:字符层(已剔噪)` 或 `epub:章节段落` |
| `page_context.truncated` | 超限截断时为 `true`,消费方据此知道自己拿的不是整页 |
| `page_context.fallback_reason` | 取不到正文时**照发事件**并说明原因(扫描页/文件不可解析/提取异常);此时 `text_available=false` |
| `page_context.reason` | `dwell`(停留约 2.5s)或 `selection`(页上有选区,补齐整页背景) |
| `page_context.selection` | 仅 `reason=selection` 时存在:当前选中原文(≤400 字) |
| `page_context.visual` | **只给引用**:`page_image` 是页图 URL、`has_ink` 表示本页有无墨迹。永不内联图片字节 |

产生规则(共享便签 rev18):**连续翻页/滚动不逐页注入**;停在一页约 2.5 秒发一条 `reason=dwell`;
页上出现/改变/清空选区时立刻再发一条,带该页完整背景而非孤立选区。同一 (用户,书,页,选区) 只发一次。
