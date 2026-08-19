# 阅读器上下文链路接入点地图（2026-07-30 建；⚠ 2026-08-16 起全部工程归 Claude，标题原有的「Claude → Codex」分工已作废）

> 用途：接续 snapshot MCP 字段对接工作时，不必再摸索"这一跳在哪个文件"。
> 所有行号为 2026-07-30 当日实测（`reader_outgoing_context.py` 与 `reader_direct_wire.py`
> 当天被 Claude 改过，旧文档里的行号已失效）。**符号名比行号可靠**，行号变了就 grep 符号。
> 配套阅读：[snapshot 字段交接](snapshot-fold-handoff.md)（缺口与待办）。

## 0. 全链路一眼

```
用户操作(翻页/选区/绘图)
  → ① 前端采集与防抖        rc-core.js / epub-html.js / pdf-adapter
  → ② 路由入口(App 内本地)   native-local-runtime.js 的 /pdf/api/active-reading 分支;桌面/扩展表面才走 Pi 的 pdf_reader.py
  → ③ 上下文构造与折叠        reader_outgoing_context.py
  → ④ 出向 journal           state/reader-outgoing-journal.jsonl
  → ⑤ App/扩展经固定 WSS 直连 rc-computer-voice.js → Windows(PWA 阅读页 2026-08-14 起 410,已不是交付表面)
  → ⑥ Windows 折叠成快照      DirectContextSnapshot.cs  (visual/drawing/embeds/viewport/knowledge 已由 CopyVisual/CopyEmbeds/CopyViewport/CopyKnowledge 保留)
  → ⑦ MCP 只读工具           ReaderContextMcpServer.cs  reader_context_snapshot
  → Codex 读到

回写方向（直接命令仍在；但 **MCP 面已不再只读** —— ReaderContextMcpServer.cs 现有 reader_command / reader_highlight_text / reader_note_create / reader_note_edit / reader_anki_draft / reader_undo_last 等写工具）
Codex 产出结构化结果
  → ⑧ 直接命令白名单          reader_direct_commands.py  ACTIONS
  → ⑨ handler 组装与执行      reader_direct_wire.py  build_handlers
  → ⑩ 确定性底座              pdf_reader.py 的 sidecar / upages / anki record
  → ⑪ 前端实时动作            reader_events.py publish → RC.execRemote
```

## 1. 正向链路（用户 → AI 看到）

### ① 前端采集与防抖 —— `_server_deploy/static/pdf/rc-core.js`

| 符号 | 行 | 职责 |
|---|---|---|
| `_CTX_LS` | 85 | localStorage 开关键 `eph-ctx-sync`；关闭时 `report()` 立即返回、零网络 |
| `_CTX_NAV_MS` / `_CTX_DWELL_MS` | 89 / 93 | 导航合并 1s；**停留 2.5s 才发 `reason='dwell'`** —— 连续翻页不逐页注入就靠它 |
| `_ctxSend` | 102 | 真正 POST 到 `/pdf/api/active-reading`，单次在途保护 + `keepalive` |
| `_ctxBeacon` | 116 | 切后台/关页用 `sendBeacon` 补最后一次 |
| `_ctxSameState` | 143 | "无变化就别发"判等。**`viewport` 与 `ts` 被显式排除** |
| `_ctxOnlyPosChanged` | 154 | 判定是否算"导航"（可合并）。同样排除 `viewport` |
| `_ctxSync.report` | 约 340 | **宿主唯一入口**。patch 全量合并，无字段白名单 —— 加字段直接带上即可 |

⚠ **加新字段时必读**：若新字段随滚动高频变化，必须像 `viewport` 一样排除出上面两个判等，
否则每次滚动都被判成状态变化而即时推送，把导航合并整个打穿（注释里记着真机实测
"连翻 6 页发了 4 次"）。`report()` 里有专门分支：只有视口变时更新 `pend` 但不排定发送。

宿主侧调用点：
- EPUB：`epub-html.js` 的 `_reportPos(idx)`（内含 `_viewportRatio(idx)`，节未加载完返回 `null`）
  与 `_ctxSelReport(txt)`（选区，`immediate: true`）
- 焦点/绘图：`RC.outgoing.focus(...)` / `cancel()` / `bindDrawingFocus(...)` / `dropDrawingFocus()`

### ② Pi 路由入口 —— `_server_deploy/pdf_reader.py`

| 符号 | 行 | 职责 |
|---|---|---|
| `/pdf/api/active-reading` POST | 约 2790-2860 | 写 `reader-active` sidecar；选区三态（空串=明确无选区，字段缺失=未上报） |
| `_maybe_emit_page_context(rec, body)` | 2868 | **dwell 或选区变化时才发整页上下文**；去重键含选区指纹 |
| `build_page_context` 调用 | 约 2901 | 传 `reason` 与 `viewport`（`body.viewport`） |

### ③ 上下文构造 —— `_server_deploy/reader_outgoing_context.py`

| 符号 | 行 | 职责 |
|---|---|---|
| `DRAW_STABLE_S=1.0` | 23 | 停笔多久算稳定 |
| `INK_FRESH_S=120.0` | 24 | 笔迹"近期"窗口 → `freshness` 三态的分界 |
| `EPUB_VIEWPORT_PAD=6` | 25 | 视口为中心上下各扩几段 |
| `FOCUS_FRESH_S=300.0` | 26 | 焦点新鲜窗口 |
| `class DrawingRevisions` | 36 | 内容摘要 + 静默计时；`observe()` 可重复安全调用 |
| `class FocusState` | 109 | 焦点 set/cancel/版本 |
| `class OutgoingJournal` | 166 | `append` / `since` / `wait_since`（长轮询） |
| `_DRAWINGS` | 269 | **进程内共享单例** —— 路由与上下文构造必须看同一份稳定期计时 |
| `MARK_L / MARK_R` | 275 | `⟦` `⟧` 边界标记 |
| `_escape_marks` / `unescape_marks` | 278 / 292 | 转义与单次扫描反转义（后者是 C# 侧应对齐的合同） |
| `annotate_page_text` | 335 | 高亮原位包裹 + 块状内容紧随；定位不到的进 `unanchored` |
| `_viewport_center` | 399 | `{para}` 或 `{ratio}` → 中心段号；坏值一律退回整章 |
| `_page_embeds` | 418 | 取该页高亮与块状内容（PDF 按 `page`，EPUB 按 `anchor.section`） |
| `build_page_context` | 470 | **正向链路的核心产出** |
| `register_outgoing_context` | 635 | 挂 `/api/outgoing/journal`、`/api/outgoing/drawing`、`/api/outgoing/focus`、`/api/outgoing/state`（**没有** `/api/drawing-state`、`/api/focus`，那只是代码注释里的旧叫法） |

`build_page_context` 的输出形状与字段语义见 `reader-specs/fixtures/README.md`，
可重放样例在 `reader-specs/fixtures/outgoing-events.jsonl` 第 1 行。

### ⑥ Windows 折叠 —— ⚠ 当前缺口就在这里

`extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectContextSnapshot.cs`
折叠时的字段集已不止 `file` / `page` / `selection`：`BuildPageContext` 另外调 `CopyVisual` / `CopyEmbeds` / `CopyViewport` / `CopyKnowledge`。
`visual`（含 `drawing`）/ `embeds` / `viewport` / `knowledge` 现在都会写进快照，这个缺口已经补上。

模式分流在 `DirectBridgeProtocol.cs:544`：
`LegacyInject → ForwardLegacyContextAsync`／else `→ ForwardSnapshotContextAsync`。
`HandleActiveReadingAsync` 强制 `SnapshotMcp`，否则抛
`BW_READER_CONTEXT_SNAPSHOT_MODE_REQUIRED`。

### ⑦ MCP 面（**已不再只读**：除 `reader_context_snapshot` 外还有一批写工具）

`ReaderContextMcpServer.cs`：`ToolName = "reader_context_snapshot"`(:9)、
`FreshnessWindow`(:12) 3 分钟 → `contextStatus=stale`、`RunAsync`(:54)。
注册事实用 `codex mcp get reader_snapshot` 现场查，不看 README。

## 2. 反向链路（AI 产出 → 阅读器）

### ⑧ 白名单 —— `_server_deploy/reader_direct_commands.py`

`ACTIONS`(:26) 现有 **24 个动作**，`MODES`(:57) = `independent` / `dependent`，
`validate()` 校验 `correlation/target/anchor/params/idempotency/dependencies/mode/steps`。

读：`read.page` `read.selection` `read.pageimage` `toc.get` `search.book` `search.all`
`dict.lookup` `highlight.list` `note.list` `section.read` `recall.creation` `recall.notes`
写：`highlight.create` `note.create` `page.new` `page.add` `anki.draft` `vocab.add`
导航：`nav.goto` `nav.open`；07-30 之后新增：读 `vocab.page`；写 `note.edit` `undo.last`；回写 `result.present`

### ⑨ handler 组装 —— `_server_deploy/reader_direct_wire.py`

| 符号 | 行 | 说明 |
|---|---|---|
| `_AI_TOOL_NAMES` | 22 | **会调 AI 的旧工具名，勿移除任何一项**（曾误移除 `add_vocab`/`search_image`，已撤回并由 `tests/test_ai_boundary.py` 逐名钉住） |
| `_assert_no_ai` | 44 | 比 `action.split(".",1)[-1]`；所以 `vocab.add` 的 tail 是 `add`，本就不会被 `add_vocab` 拦 |
| `build_handlers` | 51 | 可选注入项**都有 fallback**，不传 inject 也是 24/24、`missing` 为空 |
| `section_read` | 283 | EPUB 直接取章；PDF 按 TOC 切片，无目录退回单页并在 `section_title` 注明 |
| `recall_creation` | 358 | 直读 `state/assistant-creations/<uid>.json`；引用型只回 `ref` 不解引用 |
| `recall_notes` | 390 | 三源（索引/KG/Anki）；`query` 必填 ≤80、`limit` ≤8、不隐式继承 selection |
| `register_direct_commands` | 590 | 挂路由；未接线动作明确报错而非假成功 |
| `_mirror_failures` | 613 | **失败事件镜像进出向日志** —— Windows 只需订阅一个源 |

回写语义：**独立单步成功静默、失败才发事件**；结构化结果信封见
`reader-specs/specs/result-envelope.md`，`kind` 值域由 `reader_card_contract.py`
从前端渲染器解析（不是写死的，加新 kind 先看那里）。

### ⑪ 前端实时动作

`reader_events.py` 的 `publish("client-action", …)` → 前端 `RC.execRemote(action)`
（`rc-assistant.js`，PDF/EPUB 都加载）。执行顺序：
`adapter.execAction(fn,args)` → `window[fn]` → `_host.asst.goTo`。

## 3. 十四项设计各自落在哪

| 节 | 实现位置 | 状态 |
|---|---|---|
| 一 正文（PDF dwell / EPUB 视口） | `build_page_context` + `_viewport_center`；前端 `_reportPos`/`_viewportRatio` | Pi 侧完成 |
| 二 绘图三态 | `DrawingRevisions._snapshot` 的 `freshness` | 完成（fold 侧 `CopyVisual`→`CopyDrawing` 已保留） |
| 三 焦点 | `FocusState`；前端 `RC.outgoing.focus/cancel` | 通了（VAD 触发时机未做） |
| 四 正文锚定嵌入 | `annotate_page_text` + `_page_embeds` | 完成（fold 侧 `CopyEmbeds` 已保留） |
| 五/六 结构化回写 | 直接命令 ⑧⑨ + `result-envelope.md`；`result.present` 动作 + `pdf_reader.py:3084` 的 `register_direct_commands` | 已接线 |
| 七/十 助手入口与规范拆分 | `reader-specs/AGENTS.md` + `specs/*.md` | 完成 |
| 八 AI 委托迁移 | `reader-agent-capability-audit.md`；`do_task`/`make_paper` 待迁 | 部分 |
| 九 不依赖会调 AI 的 MCP | `_AI_TOOL_NAMES` + `_assert_no_ai` | 完成 |
| 十一 规范库发布 | `scripts/publish_reader_specs.py` | 完成 |
| 十二 独立/依赖模式与失败回传 | `MODES` + `_mirror_failures` | 完成 |

## 4. 已知陷阱（都是实际踩过的）

1. **`freshness` 不是 `state`**：journal 的 `drawing` 事件里 `state` 已表示 `pending|stable`，
   与三态值域不同，不可混用。
2. **判等排除**：高频变化的新字段必须排除出 `_ctxSameState`/`_ctxOnlyPosChanged`，否则打穿导航合并。
   另注：`String({a:1})` 与 `String({a:2})` 都是 `"[object Object]"` 会碰巧判等，那是巧合不可依赖。
3. **`_DRAWINGS` 必须单例**：两份实例会让同一页忽 `recent` 忽 `stale`。
4. **KG 路径是 `knowledge_graph/`**，`state/kg/` 是 page-cache；且要过滤
   `*.bak/.pre/.scan/.tmp/.old` 与 `_` 前缀，否则陈旧快照被当现状。
5. **KG 已学证据是并集**：`progress ∈ {mastered,in_progress}` ∪ `containing_notes` 非空 ∪
   `mastery(_level)` 正值。只看 `progress` 会漏掉 Pi 上真实存在的 `unseen` 但记过笔记的节点。
6. **Anki cloze 正文在 `text`**，不在 `front/back`；`tags` 也要搜；归属用真实 `source_note`。
7. **`knowledge-index.md` 只有科目汇总**，不是知识条目；分支索引在 `index/{科目}/{分支}.md` 需递归。
8. **源目录存在但不是目录时 `Path.glob()` 不抛异常、返回空** —— "源坏了"会被静默当成"没命中"。
9. **Windows 上必须 `PYTHONUTF8=1`**，否则 `handoff_check.py` 输出 `✓` 撞 GBK，0.2 秒崩。
10. **排除 AI 调用不能靠一组厂商关键词**：曾漏搜 `gemini`/`openai` 而误判 `search_image` 无 AI。
    必须读完整条函数体，特别是 fallback 分支。

## 5. 只读验证命令（不改任何状态）

```bash
# 直接命令接线情况（应 24/24、missing 为空）
python3 -c "import sys;sys.path.insert(0,'_server_deploy');import reader_direct_commands as D;print(len(D.ACTIONS))"

# 字段契约与样例
python3 -c "import json,io;d=json.loads(io.open('reader-specs/fixtures/outgoing-events.jsonl',encoding='utf-8').readline());print(list(d['page_context']))"

# 相关合同测试
python3 -m unittest tests.test_recall_notes tests.test_ai_boundary tests.test_mark_escaping tests.test_outgoing_fixture

# MCP 注册真值（不看 README）
codex mcp get reader_snapshot
```

`tests/test_reader_direct_commands.py` 在 Windows 上有 1 failure + 7 errors 是**基线**
（Pi 路径硬编码、临时文件锁），不是回归；判断标准是"与改动前逐条一致"。
