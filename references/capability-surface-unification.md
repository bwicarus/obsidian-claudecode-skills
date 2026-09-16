# 能力面统一：一份定义，多个表面（2026-09-16 用户拍板）

> 起因：语音链路上下文成本排查。**我们为注入瘦身折腾了一整天，省的是 400 字/次；
> 而 Codex 自带样板文一条 43,010 字里我们自己的指令只占 1,157 字。**
> 量级搞错会把力气全花在错的地方 —— 这份文档先钉住测得的数字，再定形态。

## 一、测得的数字（2026-09-16，从 Codex 落盘线程 `~/.codex/sessions/**/rollout-*.jsonl` 里量）

| 项 | 字符数 | 频率 |
|---|---|---|
| MCP 工具表（36 个：说明 27,197 + schema 33,368） | **60,565** | 每轮 |
| Codex Memory 说明 | 16,569 | 每轮（**已关**：`-c features.memories=false`） |
| Skills 目录（41 条，中位 273/条） | 12,089 | 每轮（**已减**：关 8 个无关插件 −9,123；归档 4 个 + 压 6 条描述 −2,700） |
| 我们的后台常驻指令 | 1,157 | 每轮 |
| 状态注入（事实版） | 185 | 每次开口 |
| 一次工具返回（日文，**转义修复前**） | 18,004（其中 15,102 是 `\uXXXX`） | 每次调用 |

修复后：日文不再转义（省 69%）；正文按页去重；注入只留事实。

## 二、工具真实使用频率（`runtime/mcp-tool-calls.jsonl`，4 天 154 次）

| 层 | 工具 | 占比 |
|---|---|---|
| 热 | `reader_context_snapshot` 54、`reader_card` 36、`reader_highlight_range` 18、`reader_page_card_read` 9、`reader_visual_image` 7、`reader_page_cards` 7、`reader_page_card_edit` 6、`reader_capability_guide` 6 | **93%** |
| 温 | `reader_page_text` 3、`reader_command` 3、`reader_page_card_delete` 2、`reader_anki_draft` 1、`reader_learning_cards` 1、`reader_flow_progress` 1 | 6% |
| 冷 | **22 个从未被调用**（含 schema 最大的 `reader_learning_card_edit` 7,333、`reader_paper_start` 3,446） | 0% |

## 三、形态（用户设计）

**一份定义 → 生成多个表面。底座直接用既有的 `bw-reader-skill-flow/1`（flow.json），不另起炉灶。**

它已经具备统一格式需要的一切：`steps[]`（每步恰好是 `command`/`tool`/`needs_ai`/`deliver` 之一）、
`{"$from": id, "path": …}` 引用上一步输出、`when` 条件、`$spread` 展开，
**引用只能指向更早的步骤**（lint 强制），外加 `bw_skill_build.py` 的 schema 校验 + 真实轨迹干跑。

在它之上补一段**描述头**即可（用户 2026-09-16）：

```
name / when（什么时候用）/ does（能做到什么）/ params（参数接口）/ tier（热温冷，由频率脚本改写）
```

⚠ 关键复用：`reader_flow_progress` 已经接好 —— 生成的 run.js 在每步前后调它，
侧栏据此画进度点。所以**一份 flow.json 同时是：可执行封装、进度来源、定时任务定义**
（`bw_flow_runner.py` 不经 Codex 直接按步跑）。

由它生成：

| 表面 | 内容 |
|---|---|
| MCP 热工具 | 完整 description + inputSchema |
| MCP 冷工具 | 名字 + 一行 `when` + 通用 `{args}` schema；真参数经 `reader_capability_guide(tool=…)` 取 |
| capability guide | 全量索引（一句话 + 参数） |
| skill 索引文件 | 分类存放的说明，供 AI 查询 |

**通用约定只写一次**（进后台常驻指令，不再在每个工具描述里重复）：
Markdown 输出格式、`page-chars` 首尾 + `[NN]` 块号定位、「卡只有经工具送出才存在／结果不明不重试」。
实测跨工具重复 1,184 字（`[NN]` 块号那段 371 字在两个工具里各写一份）。

**分层自动化**：脚本读 `mcp-tool-calls.jsonl` 近 N 天频率生成热/温/冷名单 →
MCP 服务器启动时读取。⚠ "改动只在对话重启后生效"**不需要额外实现** ——
MCP 工具表本来就只在会话开始时加载。

**能固化的流程直接封装成函数**，而不是让 AI 看着说明现场写指令：
`skill_kit/bw_skill_build.py` 那条路（flow.json → run.js，schema 校验 + 真实轨迹干跑）就是为此建的，
但**至今一次都没走通**（本地 17 个 skill 全是手写 SKILL.md，无一带 flow.json）。

## 四、插手点（file:line，2026-09-16）

| 要改的 | 位置 |
|---|---|
| 工具表生成（36 个手写 JsonObject 字面量） | `ComputerVoiceAudio/ReaderContextMcpServer.cs:580` `BuildToolList()` |
| 工具返回序列化（已改为不转义） | 同文件 `ModelJsonOptions`（13 处 `ToJsonString`） |
| 调用计数账本 | 同文件 `RecordToolCall` → `runtime/mcp-tool-calls.jsonl` |
| 后台常驻指令 | `computer-voice-desktop/voice_cli_runner.py` DEFAULTS `backendThreadInstructions` |
| codex 启动参数（已关 memories + 8 插件） | 同文件 `AppServer.launch` / `SLIM_PLUGINS` |
| skill 目录 | `~/.codex/skills/`（归档在 `~/.codex/skills-archive/`） |

## 五、分阶段（每步单独可验证）

1. **通用约定集中**：搬进常驻指令，删各工具描述里的重复段。
2. **冷工具折叠**：22 个从未调用的 → 一行 `when` + `{args}` schema；`reader_capability_guide` 支持按工具名取真参数。预计省 2 万字以上。
3. **定义抽成数据**：把 `BuildToolList()` 里的字面量抽成结构化定义，各表面由它生成（这一步之后才谈得上"在 MCP 与 skill 之间搬"）。
4. **名单自动化**：频率脚本 → 名单文件 → 启动时读。
5. **流程固化**：把跑通的多步流程编成 flow（配图卡 + 资料卡合并为"图文可选"的一个工具是第一个候选）。

⚠ 前两步会碰到自检里钉着的断言（`ContractSelfTest` / `DirectBridgeSelfTest` / `tests/reader_contract/*.mjs`）——
2026-09-16 当天已因此失败两次（record 成员顺序、调用点原样写法）。改一步跑一次
`python extensions/bw-reader-webext/handoff_check.py`。
