# 阅读器能力审计:认知/规划 vs 确定性操作(A1)

> 口径:任务书 rev18「十四、A1」。以**真实实现**为准,不设计新能力。
> 目的:确定新增的无 AI 直接命令通道**可以调什么**、**绝不能依赖什么**。

## 1. 旧 AI/Agent 工具总览

`_server_deploy/assistant.py` 内的沙盒工具共 **52 个**(`_t_*`)。按"执行时是否会再次调用 AI"分两类:

### 1.1 会再次调用 AI —— 新通道**不得依赖**(23 个)

| 分组 | 工具 |
|---|---|
| 研究/生成 | `web_search` `search_image` `search_video` `make_paper` `summarize_section` `do_task` `run_saved_task` |
| 视觉(图像送模型) | `see_page` `see_figure` `see_ink` `correct_dict` |
| 学习闭环判断 | `material_graph` `read_material` `relate_material` `learning_focus` `situation_feedback` `make_diagnostic` `mastery_proposal` `apply_mastery` `error_patterns` |
| 其它 | `read_check_report` `add_vocab` `auto_highlight` |

这些是**认知/规划**能力:它们接收上下文后要做研究、判断或规划,再决定触发什么动作。
按任务书九节,这些路径不能出现在无 AI 命令通道的执行依赖里。

### 1.2 确定性(执行期不调 AI)—— 可作为直接命令的底层能力(29 个)

| 分组 | 工具 | 对应确定性底座 |
|---|---|---|
| 读取 | `read_page` `read_selection` `search_book` `search_all_books` `toc` `page_vocab` | `_page_text_clean` / `_epub_section_paragraphs` / FTS5 索引 / `_effective_toc` |
| 定位/导航 | `goto_page` `open_book` `page_show` | 前端动作 + `/api/reading-pos` |
| 标注 | `highlight` `read_highlights` `find_highlights` | 高亮 sidecar + `hl_norm_color` |
| 便签 | `notes_query` `notes_read` `notes_create` `notes_edit` | `/pdf/api/notes` + `state/reader-notes/` |
| 页面 | `page_new` `page_add` | `/api/userpages` + `/api/pdf-insert-page` |
| 词典/翻译 | `lookup_word` `translate` | ECDICT / unidic / Google 翻译(非生成式 LLM) |
| 制卡/笔记落盘 | `make_anki` `make_note` | AnkiConnect / vault 写入 |
| 其它 | `recall_creation` `recall_notes` `undo_last` `save_intent_tool` `list_saved_tasks` `start_dictation` `remove_mastery` | 实体注册表 / 撤销栈 / 意图库 |

> 注:`translate` 默认走 Google 翻译链,**不是**生成式 LLM;若配置切到 AI 后端则退出本类,
> 直接命令通道调用它时必须显式指定确定性后端。

## 2. MCP 门面

`_server_deploy/mcp_server.py` 暴露 14 个工具,其中 `assistant_call_tool` 可**代调上面任意
`_t_*` 工具**——因此 MCP 门面整体**不能**作为新通道的执行依赖(它可间接触发 AI)。
新通道只直连 1.2 里的确定性底座,不经 MCP。

## 3. 现有渲染能力(结构化结果的权威)

卡片类型不是写死的,由 `_server_deploy/reader_card_contract.py` 从前端渲染器解析:
- 卡片 kind ← `static/pdf/rc-voicecall.js::_infoHtml`(weather/news/images/videos/fact + general 兜底)
- 轮次 part ← `static/pdf/rc-turncard.js::renderPart`(text/card/cards/hlcard/tool/meta)

## 4. 缺口与兼容边界

- **HTML 宿主**无服务端正文源(正文在前端 DOM),选区上报亦未接入。
- `hlcard.items[]` 与 `cards[]` 的**条目字段未进契约**,规范化散在 `_sanitize_ext_parts`。
- 锚定只到"书+页"粒度;`selection` 是轮次元字段,不与单张卡绑定。
- 旧 `/pdf/api/notes` 兼容通道**必须保留**(旧 AI `notes_create/notes_edit` 仍依赖)。
- 视觉类能力(看页/看图/看手写)**天然属 AI**,无确定性替代;直接命令只能提供
  "取页图/取墨迹原始数据",判断仍由上游助手完成。
