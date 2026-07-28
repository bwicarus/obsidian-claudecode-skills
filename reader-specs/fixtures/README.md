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
