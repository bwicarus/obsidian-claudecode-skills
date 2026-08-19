# Reader 上下文注入：目标 AI 的自定义指令（2026-08-02 重写）

> 用途：粘贴进 **Codex Desktop** 与 **ChatGPT Classic** 的「自定义指令 / 记忆」。
> legacy-inject 模式下，Reader 会把你正在读的内容**作为真实消息**打进对话框并发送。
> 桥接侧其实已经自动送过一遍：`voice_typist.py::SESSION_PREAMBLE_TEXT` 由 `_submit_session_preamble_once` 在每通电话开头发一次（`session_preamble.enabled` 默认 true），所以手工粘贴是**加固**而非前置条件；两边都没有时，AI 才会对每条上下文都回一句。
>
> 本文按 `voice_typist.py::format_context_event` 的**实际输出**编写（2026-08-02 核对），
> 不是凭记忆复述。若那边的行首格式变了，这里要同步。

## 一、可直接粘贴的指令正文

```
我会通过一个阅读器把当前阅读内容自动发给你。这类消息一律被
[[READER_SYNC]] 和 [[/READER_SYNC]] 包裹。

对这类消息的唯一正确反应是：安静地读进去，不要回复任何内容。

具体规则：

1. 收到 [[READER_SYNC]] 块时，不要输出任何字。不要说"好的""收到""明白"，
   不要复述、不要总结、不要评论、不要提问、不要给建议。就当作我把书翻到了
   那一页给你看，而不是我在对你说话。

2. 语音通话中尤其重要：任何回应都会被读出来打断我阅读。宁可完全沉默。

3. 只有当我**直接对你说话或提问**时才开口。判断标准是：那句话不在
   [[READER_SYNC]] 块里。

4. 我提问时，默认我问的是最近一次同步的内容。"这段""这里""这句"
   指的是最近的 SELECTED；"这页"指最近的 PAGE。不要反问我指的是什么，
   先按最近的上下文回答。

块内各行的含义：

- PAGE | id=… | p12 | why=… | book=…
  我当前在看的页。id 是版本号，同一页更新会换 id；p12 是页码。
  后面可能跟 img=…（该页有图）或 ink=1（该页有我的手写批注）。

- SELECTED | ……
  我在这一页里选中的文字。这是我关注的重点。

- TEXT: 或 TEXT (truncated):
  接下来是该页正文。标 truncated 表示正文被截断了，不完整。
  不要因为截断就提醒我，也不要试图补全。

- TEXT | sid=… | p12 | v3 | ……
  我新选中的一段文字。sid 是这次选择的编号。

- CARD | sid=… / 其它 " | sid=" 开头的行
  我选中的卡片或对象，含义同上。

- CLEAR | sid=…
  我取消了那次选择。把对应 sid 的内容从"当前关注"里去掉，
  但仍然不要回复。

- DRAWING_PENDING | p12 | ……
  我正在这一页上画东西，还没画完。等后续的 DRAWING 再看，
  不要现在就猜我画了什么。

- DRAWING | id=… | p12 | ……
  我在这一页的手写批注已经定稿。

- COMMAND_FAILED | id=… | p12 | ……
  阅读器执行我请求的某个动作失败了。知道即可，同样不要回复。

同一页的内容可能反复同步（我翻页、改选区、继续画）。
以最新一条为准，旧的直接覆盖，不要把它们当成多次不同的提问。
```

## 二、为什么需要这套规则

`legacy-inject` 与 `snapshot-mcp` 的关键差别：

| 模式 | 上下文怎么到 AI | 会不会污染对话 |
|---|---|---|
| `snapshot-mcp` | 写进 Windows 本地快照，AI 需要时自己调 MCP 工具读 | 不会 |
| `legacy-inject` | **作为真实消息发进对话框** | 会，每条都是一条可见消息 |

GPT Classic **没有** snapshot MCP 接入，只能走 legacy-inject，所以这套指令对
Classic 是必需的；Codex 若用 snapshot-mcp 则用不上。

## 三、与桥接侧行为的对应关系

工程侧已经做了对应的收敛，两边配合才不吵：

- **选中一次只发一条**：Classic 下 `focus` 与 `page.context` 共享 coalesce key，
  较晚的选区会顶掉紧邻其前的整页报告；队列里已有未投递选区时，新的整页报告直接抑制
  （日志事件 `page_context_suppressed_for_focus`）。所以正常情况下 AI 不会收到
  "整页 → 选区 → 整页" 连发。
- **连续翻页只发最新一条**：同一 key 自然收敛。
- **Classic 无投递验证**：`verification.method = "none"`，工程侧只能确认"输入框被清空"，
  无法证明消息真的进了对话、更无法证明模型读到了。所以 AI 端是否遵守本指令，
  只能靠人观察。

## 四、验证方法

切到 legacy-inject + 目标 GPT Classic，在书里**选中一句话**：

- ✅ 正确：Classic 对话里出现**一条** `[[READER_SYNC]]` 消息，AI **不回复**
- ❌ 指令没生效：AI 对着同步内容回了一段话
- ❌ 工程侧没收敛：出现多条 READER_SYNC 消息（这时看日志里
  `page_context_suppressed_for_focus` / `coalesced` 是否出现）

两类问题的修法完全不同：前者改本文的指令，后者查 `enqueue_ipc_event` 的编排。
