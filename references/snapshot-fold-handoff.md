# 快照 MCP 字段对接交接（Claude → Codex，2026-07-30）

> ⚠ **已归档（2026-08-19）**：本文是 2026-07-30 那个时点的交接快照，按当时「Claude=Windows 桥 / Codex=快照 MCP 与 iOS」的分工写成。2026-08-16 用户拍板**全部工程由 Claude 负责**，文中「snapshot MCP 后续全部交给 Codex」「Claude 不再并行修改 A 类文件」等分工**已作废**；§6 的 BWAB 投递限制也是当时旧守护进程下的实测。下面内容只作历史存档与字段语义（`visual.drawing.freshness` / `embeds` / 正文内嵌标记转义）参考，**不要当现行分工或现行状态读**。

> 用户已确认：**snapshot MCP 后续全部交给 Codex 做**。本文是 Claude 侧全部资产与缺口定位。
> Claude 不再并行修改本文列出的 A 类文件，避免双写。

## 0. 先确认的三件（Claude 已独立核实，非转述）

- `codex mcp get reader_snapshot` 现场返回 `enabled=true / stdio / --reader-context-mcp --state …`，
  与 Codex 所述一致。**Claude 原判「MCP 未注册」是错的** —— 基于 README 手工命令行推断，
  没查运行时真值。
- `push_reader_context_to_pc.py:222-232` 的 `return` 与 `_legacy_push_enabled` 确认：
  snapshot 模式下不生成、不 SSH。
- `DirectBridgeAdapters.cs` 的 `StartTypist: contextDeliveryMode == LegacyInject` 确认：
  snapshot 模式彻底旁路 voice-typist。

## 1. 用户实测的两个症状 —— 根因是两条链的字段契约从未对齐

### ① 绘图后快照里没有相关内容

`DirectContextSnapshot.cs` 折叠的字段集只有 **`file` / `page` / `selection`**；
在该文件 grep `visual|drawing|ink|embeds|highlight` **零命中**。

Pi 侧 journal 事件其实**已有**这些数据（Claude 2026-07-29 提交 `9e8f816`，已在分支上），
但在 Windows fold 这一步被整体丢弃。

**fold 是收紧型白名单，设计正确。**问题只是上游新增字段必须显式加进白名单，
而 Codex 无从知道 Claude 加了哪些 —— 这是 Claude 的交接缺失，不是实现缺陷。

### ② 问天气后不知道把卡片更新到阅读器

MCP 只注册只读 `reader_context_snapshot`、明写不接受 mutation，AI 没有回写途径。
这是**设计缺口**而非 bug：任务书第五/六节要求「AI 只回结构化卡片字段、阅读器按注册表渲染」，
只读 MCP 做不到。

## 2. A 类资产：字段语义与样例（加 fold 时直接照抄）

| 资产 | 位置 |
|---|---|
| 字段契约（两节） | `reader-specs/fixtures/README.md` — 「page_context.text 里的正文锚定嵌入内容」与「visual.drawing.freshness（绘图三态）」，含标记语法、转义规则、5 种 `unanchored._reason` 值域、三态语义表 |
| 可重放样例 | `reader-specs/fixtures/outgoing-events.jsonl` 第 1 行 `page.context`，已含 `visual.drawing` 与 `embeds` 的完整形状 |
| 实现 | `_server_deploy/reader_outgoing_context.py` |
| 反转义参考实现 | 同文件 `unescape_marks` + `tests/test_mark_escaping.py`（8 项），即 Codex 07-29 07:37 冻结的合同；C# 侧可照抄用例对齐 |

### 字段形状

```
visual.drawing = {
  freshness: "none" | "recent" | "stale",   # ⚠ 字段名不是 state
  lastEditedAt, freshWindowS (默认 120),
  inProgress, stable, drawingRevision, ref, empty
}

embeds = { highlights, blocks, unanchored[] }

viewport = { center, from, to, total, pad }   # 仅 EPUB；PDF 无此字段
```

**⚠ 命名陷阱**：`freshness` 不是 `state` —— journal 的 `drawing` 事件里 `state` 已表示
`pending|stable`，两者值域不同、不可混用。`fixtures/README.md` 有显式警告。

### 正文内嵌标记

```text
食文化とは、地域ごとの⟦HIGHLIGHT color="#ffd54a" note="重点"⟧気候風土⟦/HIGHLIGHT⟧、文明…

⟦CARD_START type="weather" label="w1"⟧东京明天：27–37℃，晴转多云。⟦CARD_END⟧
```

`type ∈ note|card|anki|video`。正文原有的 `⟦` `⟧` 转义为 `\⟦` `\⟧`，反斜杠自身为 `\\`。
定位不到锚点的高亮**不进正文**（否则正文出现两次），改列入 `embeds.unanchored` 并带 `_reason`。

## 3. B 类资产：回写通道的现成底座

- `_server_deploy/reader_direct_commands.py` 现有 **20 个无 AI 动作**，接线在
  `reader_direct_wire.py`（`build_handlers` 实测 20/20 接线、`missing` 为空）。
- 与卡片回写直接相关：`page.new` / `page.add`、`anki.draft`、`highlight.create`、
  `note.create`、`section.read`、`recall.notes`、`recall.creation`、`vocab.add`。
- 规范：`reader-specs/specs/page-compose.md` 含「交互纸」一节（上游出题 → `page.new`+`page.add`；
  标准答案不写进纸里；留白靠空行元素；造纸这轮不预生成检查报告）。
  `result-envelope.md` 是结构化结果信封（`kind: weather|news|images|videos|fact|general|cards`）。
- ⚠ **该通道不得依赖 MCP**：`mcp_server.py` 的 `assistant_call_tool` 可代调会 AI 的旧工具，
  见 `reader-agent-capability-audit.md` §1.1。该表 07-29 被 Claude 误改过一次已撤回，
  并补 `tests/test_ai_boundary.py` 逐名钉住 —— **请勿再移除表内任何名字**。

## 4. 需要 Codex 做的三件（按依赖排）

1. **fold 白名单加字段**：`visual.drawing`、`embeds`、`viewport`。语义照 §2，不必新造。
   建议保持收紧风格：逐字段显式列出而非透传，新增项走同一套 schema 校验。
2. **定回写通道并接线**。两个选项，Claude 倾向 ①：
   - ① 走直接命令（20 个动作现成、无 AI、有幂等键与失败事件总线），MCP 保持纯只读 ——
     不破坏「只读 MCP」这个安全属性；
   - ② 给 MCP 加写工具 —— 打破 README 的 no-mutation 承诺，不建议。

   若选 ①，还需决定 Windows 侧怎么发直接命令（经既有 WSS 回 Pi，还是 Codex 直接调 Pi 路由）。
   **这一步涉及产品语义，建议先与用户确认再实现。**
3. **建一份共同字段清单**写进 Codex 侧合同，避免再次各做各的。`fixtures/README.md` 可作起点。

## 5. 边界

- Claude 本轮全程只读：未改代码、未跑测试、未部署、未推送（本文件是唯一新增产出）。
- A/B 类资产 Codex 可自由取用与修改，含 `reader_outgoing_context.py` 与 `reader-specs/**`；
  Claude 不再并行改这些文件。若需保留某些文件不动，在 BWAB 说明。
- 两个虚拟音频驱动已安装、重启 Windows 的验收门仍等用户点头 —— 与本交接无关。

## 6. BWAB 投递已知限制（本次实测，供参考）

| 限制 | 现象 |
|---|---|
| `notify_assistant` 不可用 | `unknown_message_type: claude_to_assistant` |
| `on_busy` 默认值不可用 | schema 标 `steer`，实际只认 `reject` |
| `reply` 无法主动发起 | 对方 `turnPhase: idle` 时报 `reply_target_expired`，即使不带 `reply_to` |
| Codex 侧多行 `--message` 被截断 | `bwab.cmd` 用 `%*` 转发，cmd 在首个换行处截断（Codex 已改用 Node CLI） |

前三项均因守护仍是旧进程；重启桥守护后应恢复。**长交接建议一律落文件、消息只给路径。**
