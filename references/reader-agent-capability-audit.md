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
| 条件性 | `read_check_report`(默认同步返回报告内容不调 AI;**仅 `verify:true`** 起查书子 agent → 保守归此类,直接命令若要用必须强制 `verify=false`) |
| 其它 | `add_vocab` `auto_highlight` |

这些是**认知/规划**能力:它们接收上下文后要做研究、判断或规划,再决定触发什么动作。
按任务书九节,这些路径不能出现在无 AI 命令通道的执行依赖里。

> **2026-07-29 一次误判与更正(留档,因为教训比结论重要)**
>
> 我曾把 `add_vocab` 与 `search_image` 从本表移出,理由是"逐行核对无 AI 调用"。**这是错的**,
> 已恢复。真实情况:
> · `search_image` 在常规搜索**落空时**会调 `_gemini_text` 把词规范化成 Commons 检索名;
> · `add_vocab` 的旧助手链会经在线例句翻译落到 AI 后端。
>
> 出错的原因很具体:核对时 grep 的模式是 `ask|_ai|claude|codex|prompt`,**漏了 `gemini`**,
> 于是"没搜到"被当成了"没有"。**排除 AI 调用不能靠一组关键词** —— 后端可能是任何供应商,
> 兜底分支也可能藏在正常路径之后。可靠做法是读完整条函数体,特别是 fallback 分支。
>
> 另一半教训:那次移除**本身就是多余的**。`_assert_no_ai` 比的是
> `action.split(".",1)[-1]`,新动作 `vocab.add` 的 tail 是 `add`,从来不会被 `add_vocab`
> 拦住。改黑名单前没验证过它到底拦不拦。
>
> **正确姿势**:旧工具名一律留在本表;要用某项能力,就像 `vocab.add` 那样**另拆一条
> 确定性路径**(`build_vocab_note` + `online=False`),而不是给旧工具开口子。

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

## 3.5 第八节的迁移目标(2026-07-29 补)

上面 1.1/1.2 回答的是"能不能调";第八节还要回答"**哪些委托必须消除**"。逐条读工具描述
后,真正把**决策权**交给下游 AI 的只有两个:

| 工具 | 描述原文 | 为什么必须迁移 |
|---|---|---|
| `do_task` | 「后台 agent(**自己规划、自己连着调工具**、干完回报一句话)」,一件事要 2 个以上工具就交给它 | 上游把规划整体外包,且**拿不到中间结果**;任务书目标链路要求规划留在上游 |
| `make_paper` | 「你没有别的造纸工具……**它会交给后台 CLI 造**」 | 出题=认知(归上游),写纸=确定性(应走 `page.new`/`page.add`) |

这两条迁移后,助手侧不再有"AI 调 AI"。

`summarize_section` 是认知/确定性的天然分界样本:描述原文「取当前页所在整章正文**交给你**
总结」—— 取整章正文是确定性的(但**直接命令缺这个**,`read.page` 只逐页),总结归上游。

### 直接命令 vs 助手工具的差集(截至 07-29)

`reader_direct_commands.py` 现有 **20 个**动作。与助手工具对比:

- **已补齐**(07-29):`recall.creation` `recall.notes` `vocab.add` `section.read`。
  注意 `vocab.add` 走的是**新拆的确定性路径**(`build_vocab_note` + `online=False`),
  与 1.1 里那个会调 AI 的旧工具 `add_vocab` 是两回事 —— 补能力的正确姿势是另拆一条
  确定性路径,不是给旧工具开黑名单口子。
- **不补**:`search_image` 及一切联网类(搜图/搜视频/天气/新闻)。上游助手自带联网,
  自己查更直接;直接命令只补**上游拿不到的本地数据**(任务书第七节亦有原话:
  "天气由 AI 自查后按天气卡字段输出")。
- **待定**:`read_check_report` 要等 `verify=false` 的强制形式确定后再说。
- **直接命令有、助手没有**:`read.pageimage` `toc.get` `dict.lookup` `highlight.create`
  `highlight.list` `note.list` `page.new` `page.add` —— 说明通道在**写与定位**上已比助手
  完整,缺的只是召回类读操作。

## 4. 缺口与兼容边界

- **HTML 宿主**无服务端正文源(正文在前端 DOM),选区上报亦未接入。
- `hlcard.items[]` 与 `cards[]` 的**条目字段未进契约**,规范化散在 `_sanitize_ext_parts`。
- 锚定只到"书+页"粒度;`selection` 是轮次元字段,不与单张卡绑定。
- 旧 `/pdf/api/notes` 兼容通道**必须保留**(旧 AI `notes_create/notes_edit` 仍依赖)。
- 视觉类能力(看页/看图/看手写)**天然属 AI**,无确定性替代;直接命令只能提供
  "取页图/取墨迹原始数据",判断仍由上游助手完成。
