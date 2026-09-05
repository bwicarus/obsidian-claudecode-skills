# PDF 阅读时建立 KG 节点的规范 —— 讨论稿 v0（2026-09-05）

> 状态：**讨论稿，未动工**。用户 2026-09-05："kg 相关的还没有最终定稿吧，先讨论后再做"；
> "我现在的想法是从现在开始慢慢完善不同信息渠道建立 kg 节点的规范，现在先完 pdf 阅读时的规范"。
>
> 这份稿子只写 **PDF 阅读**这一条渠道。网页阅读、对话、练习纸、Anki 以后各写一份，
> 共用 §1 的节点模型和 §2 的证据格式（§8 预留了接口）。
> 每一节末尾的「待拍板」是需要用户定的；其余是我对现有代码的归纳，不是新设计。

---

## 0. 先说清现状（不重造）

系统里已经有两套节点、三条"阅读 → 节点"的通道和一条铁律。规范要在它们之上写，
不是另起一套。

**两套节点**

| | authored KG | emergent 概念图 |
|---|---|---|
| 文件 | `knowledge_graph/<book>.json`（书内） | `state/attention/emergent-graph.json`（跨书） |
| 怎么来 | `scripts/kg/build_nodes.py`：L0/L1 直接用 PDF 目录（零 AI），L2 逐页 AI 抽取 | `promote_concepts.py` 从**真实学习活动**长出来，不预建整树 |
| 身份 | `ladr.l2.1A.1_1` 这类书内 id | `concept_node_service.py` 铸的稳定 id + canonical key + 别名表 |
| 写入口 | build_nodes / rescan_rolling / link_with_ai / link_and_mastery（多处） | **唯一写入口** `concept_node_service.py`：prepare journal → 原子替换 → commit；rollback 写 tombstone 不复活 |
| 证据 | `pages`、`note_ref`、`card_refs` | 每条证据带 `source_kind / document_ref / page / quote`；`page-brief` 与 `book-occurrence` 两类**必须带能在原文逐字复核的 quote** |
| 现状 | 只有 EGIU（语法，761 节点）与 LADR（线代，403 节点），7 月 18 日起没更新；09-02 搬迁没搬，09-05 才放到 Windows | 8 月 28 日起没动 |

**三条已有的"阅读 → 节点"通道**

1. `scripts/kg/gen_page_brief.py`：读到某页时后台生成「本页简述」，输出
   `concepts:[{name, evidence(原文逐字)}]`，可作为 `page-brief` 证据投给 concept_node_service。
2. `references/emergent-edge-algorithm.md` v3 主流程「阅读时生长」：注意力榜新词 → vocab 门
   （语言项绝不进概念图）→ 在本书**当前阅读位置之前**向前搜索 → 相关性 top-k → AI 只在有界
   候选上定关系。人工逐条确认已废除，改 AI 后台审计（`audit_edges.py`）。
3. `promote_concepts.py` 的种子规则：登记笔记标题 + 练习纸 `node_results` 是种子；
   **高亮太吵不当种子**；焦点榜/查词被词汇污染绝不当源；制卡是强信号（2026-08 用户设计）。

**一条铁律**（`node_evidence.py` 与 `mastery_overrides.py` 都写着）

> 读 / 查词 / 高亮 / 问答 = engagement 证据，只支撑"接触过 / 进度"，**不当掌握**；
> 只有练习纸判分 / Anki 卡 = mastery 证据。掌握度绝不自动改：AI 提候选，用户确认。

**页锚**：卡片系统已经有一套寻址（`page` + 字符区间 `from/to` + `text` + 文字层版本 `rev`，
以及正文里的 `[NN]` 块号）。OCR 重跑、重排之后按 `text` 就近重定位。证据的锚点直接复用它，
不另造。

---

## 1. 节点模型（规范的对象）

- **一个节点 = 一个概念**，跨书唯一身份归 emergent 图（canonical key + 别名）。
  authored L2 是这个概念在某本书里的**落点**（`pages`、`numeric_label`），不是另一个概念。
- **PDF 阅读产生的不是节点，是证据和候选。** 节点身份由 concept_node_service 决定，
  阅读渠道只投递。这条把"AI 读到什么就建什么"关在门外。
- **语言项永不进概念图**（单词、词组、语法点走 vocab / grammar-tracked）。EGIU 那种
  `kind: grammar` 的 authored 图是例外，它的节点是语法点，由 grammar 路由维护。

> 待拍板 ①：EGIU / LADR 两张旧 authored 图怎么并入 —— 作为 emergent 节点的
> `in_authored_kg` 挂接（一个概念一个身份，书内落点只是属性），还是保留双轨？

---

## 2. 证据格式（PDF 渠道的最小单位）

```json
{
  "source_kind": "pdf-reading/<subkind>",
  "document_ref": "资源/books/000-LADR/000-LADR4eChinese.pdf",
  "rev": "<文字层版本，快照里有>",
  "page": 12,
  "anchor": {"block": 3, "from": 10, "to": 22},
  "quote": "エネルギーは保存される",
  "actor": "user | ai | algo",
  "at": 1757046000000,
  "turn_id": "<对话轮次，AI 产生的证据才有>",
  "strength": "strong | weak"
}
```

- `quote` 必须能在该页文字层**逐字**找到；找不到就不是证据（沿用 concept_node_service 对
  `page-brief` 的要求，扩到整个渠道）。
- `anchor` 与卡片同一套寻址；文字层重跑后按 `quote` 重定位，重定位失败的证据标 `stale`，不删。
- `actor` 说清是谁的判断：用户亲手做的、AI 讲解时产生的、算法（页简述/注意力）产生的。
  三者权重不同，但**都要留**，因为审计要看来源分布。

**子类与强弱**

| subkind | 触发 | 强弱 | 依据 |
|---|---|---|---|
| `card-bind` | AI 用 `reader_card` 把讲解钉在某句上 | strong | 用户定的「制卡 = 概念网强信号」 |
| `highlight-note` | 用户高亮**并写了备注** | strong | 备注是主动表达 |
| `highlight` | 纯高亮 | weak | promote_concepts 的规则：高亮的是句子片段，太吵 |
| `sticky-note` | 便签 | strong | |
| `assistant-explain` | 对话里 AI 讲了某概念并引用了原文 | weak | 需另一条独立证据佐证 |
| `page-brief` | 页简述抽出的概念 + evidence | weak | 算法源 |
| `check-report` | 练习纸判分涉及的知识点 | strong | 唯一能进掌握态的 |
| dwell / 翻页 / 查词 | | **不产生节点证据** | 只算 engagement |

**成候选的门**：一条 strong，或两条**独立**的 weak（不同 subkind，或不同页）。

> 待拍板 ②：这张强弱表认不认？特别是「纯高亮不算」和「AI 讲解只算弱」。

---

## 3. 谁能建、什么时候建

- **AI 不直接建节点。** Codex / 侧栏助手只能投递「候选 = 名字 + 证据」，由确定性服务判重、
  聚合、按 §2 的门决定成不成节点。"这算不算一个知识点"不由 AI 在对话里拍板。
- **成节点自动进行，不逐条人工确认**（emergent v3 定的），AI 后台审计负责降级 / 摘除站不住的；
  用户可以在 App 知识点 tab 否决或合并 —— 否决写 tombstone，后续自动任务不能复活它。
- **掌握态：阅读渠道永不写。** 只有 `check-report` 走「判分 → AI 提案 → 用户确认 → override」；
  Anki 卡走 link_and_mastery。这是把"看过"和"会了"分开的那条线。

> 待拍板 ③：AI 只投递不建节点、掌握态只走练习纸 / Anki，这两条是否就此定下。

---

## 4. 节点与页的关系

- `pages` 由证据聚合：每条证据带页，节点的页集合 = 证据页集合。authored L2 的 `pages`
  来自 build_nodes，两者并存时以证据为准更新（或分成 `pages` / `evidence_pages` 两个字段）。
- 新书导入时建不建 authored 树？build_nodes 的 L2 是逐页 AI，贵；纯 emergent 则读到哪长到哪，
  没读的章节在图上是空的。建议：**L0/L1 目录层零成本先建**（确定性），L2 走阅读涌现，
  夜间 `rescan_rolling` 补漏。

> 待拍板 ④：新书建图策略按上面这条来，还是继续整本预建？

---

## 5. 展示与消费

- 阅读器抽屉「知识点」tab：本页节点 + 状态（`/pdf/api/page-nodes`），点开进技能树。
- 快照 `knowledge` 字段：本页在讲什么（`kg_page_index.py`，匹配必须唯一否则弃权）。
- **Codex 目前看不到任何节点** —— 这是它 09-05 说"系统不知道你会不会"的直接原因。
  建议给 MCP 加两个工具：`reader_kg_nodes`（只读：本页 / 本书节点与状态）和
  `reader_kg_evidence`（投递 §2 格式的候选证据）。写入仍走服务，工具不碰图文件。

> 待拍板 ⑤：Codex 的工具面就这两个，还是连"提掌握度变更提案"也给它？

---

## 6. 运行位置与数据

- **权威在 Windows**：`knowledge_graph/` 与 `state/attention/emergent-graph.json`；Pi 只备份。
  09-05 已把两张图放到 Windows 工作树，`pdf` 字段改写为 `C:/obsidian/...`
  （六处读该字段的代码用两种约定：四处 `split("/obsidian/")`、两处 `Path().is_absolute()`，
  这个写法两种都吃；以后 build_nodes 在 Windows 上重建时要保证写出同样形状）。
- **维护任务要跟数据同机**：prune、link_and_mastery、attention_profile、page-brief 消费、
  audit_edges。现在它们还在 Pi 的 systemd 上对着 09-02 之后不再更新的副本跑。
  落点两个选项：ReaderPC 的 tick（跟卡片渲染同一模式，随版本发）或 Windows 计划任务
  （草稿已写，未挂）。
- **掌握态的数据源仍在 Pi**：anki-headless + anki-sync-refresh 在 Pi 刷新 `anki/records`，
  link_and_mastery 从它推节点状态。Anki 搬不搬 Windows 是单独一件事。

> 待拍板 ⑥：维护任务落 ReaderPC tick 还是计划任务；Anki 记录先从 Pi 拉还是等 Anki 搬家。

---

## 7. 待拍板清单（汇总）

1. 旧 authored 图并入方式（§1）
2. 强弱证据表，尤其纯高亮不算、AI 讲解算弱（§2）
3. AI 只投递不建节点；掌握态只走练习纸 / Anki（§3）
4. 新书建图：目录层先建，L2 涌现 + 夜间补（§4）
5. Codex 工具面（§5）
6. 维护任务落点与 Anki 记录来源（§6）

---

## 8. 与其它渠道的接口（预留，以后各写一份）

- **网页阅读**：`document_ref` = URL + 抓取哈希；锚用网页版 page-chars（`page` 恒 1，带 `region`）。
- **对话**：锚是 `turn_id`；`quote` 是对话里引用的原文，引用不到原文的讲解只算 weak。
- **练习纸**：锚是检查报告 id；是唯一能提掌握态变更提案的渠道。
- **Anki**：锚是卡 id；掌握态的权威。

---

## 相关文档

- `references/emergent-edge-algorithm.md`（v3 定稿：阅读时生长 + 笔记为骨骼 + AI 审计）
- `references/attention-kb-design.md`（七渠道、页锚迁移、跨语言归一、学习闭环铁律）
- `references/skill-tree-system.md`（authored KG 文件结构、关联校验、UI）
- `references/card-anchor-footnote-design.md`（page-chars 寻址，证据锚点复用它）
- `references/evidence-quality-lessons.md`（采集不可重来：当时没存对的救不回来）
- `references/local-first-data-architecture.md` §9「KG 分四层」
