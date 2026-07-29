# AI 委托流程盘点（任务书第八节 / 十四节 A1）

> 目标链路：**用户 → 上游助手完成认知与决策 → Pi/阅读器执行**，不再经过额外 AI。
> 本文以真实实现为准（2026-07-29 通读 `_server_deploy/*.py` + `scripts/`），不凭空设计。
> 结论是清单与判断，**不含任何实现改动**。

## 一、AI 调用点全景

| 类 | 位置 | 数量 | 性质 |
|---|---|---|---|
| A 单次问答 | `pdf_reader._ai_call` / `_ai_call_stream` | 4 种 action：`explain` `translate` `dict` `grammar` | 一问一答，无工具循环 |
| B agentic 助手 | `assistant.py` / `epub_assistant.py` | **17 个工具**，AI 自主决定调用顺序 | 有工具循环 |
| C 专项模块 | `card_improvement_runtime`(20) `grammar_reader`(11) `fitness_coach`(6) | 3 个 | 领域 prompt + 校验 |
| D 笔记自动化 | `register_notes`(6 处 `ask`) `refresh_weak_cards`(5) `pdf_extract`(6) 等 | 6 个脚本 | 批处理，跟 Reader 运行时无关 |
| E KG | `scripts/kg/link_with_ai.py` | 直接 exec `/usr/bin/claude` | 不走 `ai_client` |

**与新通道相关的只有 A、B。** C 是领域封装（有各自的校验与落库语义），D/E 是夜间批处理，都不在"用户提问 → 阅读器执行"这条链上。

## 二、B 类逐项拆分（第八节要求的认知/确定性分离）

### ① 必须迁移：把规划委托给下游 agent

| 工具 | 现状 | 判断 |
|---|---|---|
| `do_task` | 「后台 agent（**自己规划、自己连着调工具**、干完回报一句话）」，一件事要 2 个以上工具就交给它 | **这就是第八节要消除的模式。** 上游本就该自己规划并逐条发直接命令；`do_task` 等于把决策权让给下游 AI，且上游拿不到中间结果 |
| `make_paper` | 「你没有别的造纸工具……**它会交给后台 CLI 造**」 | 出题是认知工作，应由上游完成；写纸是确定性写入，应走 `page.new`/`page.add` |

这两条一旦迁移，B 类就不再有"AI 调 AI"。

### ② 认知/确定性可拆

| 工具 | 认知部分（归上游） | 确定性部分 | 直接命令覆盖 |
|---|---|---|---|
| `summarize_section` | 总结 | **取整章正文**（描述原文：「取…正文**交给你**总结」） | ❌ 缺（`read.page` 只逐页） |
| `translate` | 译文风格/术语取舍 | 机器翻译 | ⚠ 有 `/pdf/api/translate-sentence`，但直接命令表里没有 |
| `make_anki` | 判断做几张、正反面 | 写草稿批 | ✅ `anki.draft` |
| `make_note` | 整理成文 | 写 Obsidian 文件 | ❌ 缺 |

### ③ 纯确定性，却只有 AI 工具入口（最该补直接命令）

这几个描述里全是"取回/拿到/召回"，没有任何判断成分——它们是检索，不是认知：

| 工具 | 作用 | 直接命令 |
|---|---|---|
| `recall_creation` | 按句柄取回创造物全文（练习纸/报告/搜索/翻译/章节总结） | ❌ 缺 |
| `read_check_report` | 取练习纸检查报告（题目+答案+手写+判分） | ❌ 缺 |
| `recall_notes` | 召回知识索引+vault 笔记+KG 已学节点+Anki 卡 | ❌ 缺 |
| `add_vocab` | 加生词 | ❌ 缺（有 `dict.lookup` 但只查不写） |
| `search_image` | 图搜 | ❌ 缺 |

### ④ 反向差集：直接命令有、助手没有

`read.pageimage` `toc.get` `dict.lookup` `highlight.create` `highlight.list` `note.list` `page.new` `page.add`
—— 说明直接命令通道在**写与定位**上已比助手完整，缺的是**召回类**读操作。

## 三、A 类：4 个 action 的定性

| action | 认知含量 | 判断 |
|---|---|---|
| `explain` | 高 | 纯认知，本就该由上游做，不必保留服务端入口 |
| `dict`（日语 AI 讲解） | 中 | 有离线兜底（`dict.lookup` / ECDICT / unidic），AI 只做深入讲解 |
| `translate` | 中 | 可退确定性 API |
| `grammar` | 中 | spacy 依存分析是确定性的，AI 只做讲解层 |

共同点：**都有确定性底座，AI 只加解释层**。上游接管解释层后，底座可直接暴露为无 AI 命令。

## 四、建议的补齐顺序

1. **召回类五个直接命令**（`recall_creation` / `read_check_report` / `recall_notes` / `add_vocab` / `search_image`）——纯确定性，改动最小，且是上游"先取数据再判断"的前提。
2. **`section.read`**（取整章正文）——拆开 `summarize_section` 的第一步，让上游拿到原料自己总结。
3. **`do_task` / `make_paper` 的迁移**——工作量最大，且要跟"交互纸"的现有 CLI 产物格式对齐，建议单独立项。

## 五、明确不迁移的

- **C 类专项模块**（卡片改进 / 语法 / 健身教练）：有各自的领域 prompt、校验与落库事务，不在阅读器执行链上。
- **D/E 笔记自动化与 KG**：夜间批处理，用户不在场，没有"上游助手"可言。
- 便签第九节说的"MCP 内部会再次调用 AI"：确认属实——`assistant_call_tool` 桥接的正是 B 类工具集，因此**新通道不得依赖 MCP**，现有 `reader_direct_commands.py` 已符合（注释明写"不返回聊天文本，不调用其它 AI"）。
