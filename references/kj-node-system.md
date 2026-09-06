# KJ 知识节点系统（2026-09-06 首期实施）

> 设计依据：Obsidian `AI助手专用/已有项目/KJ知识点系统设计讨论.md`（用户与 Codex 的讨论检查点，
> 末尾 18:13 / 18:28 / 18:33 / 18:34 四节是拍板与交接）。本文是**实施侧**的规格与操作手册。
> 代码：`scripts/kj/`（业务）、`_server_deploy/kj_nodes.py`（HTTP）、`_server_deploy/assistant.py` 的 `kj_*` 工具、
> 测试 `tests/test_kj_*.py`。运行位置 = **Windows 本机**（账本、Markdown、服务都在 Windows；Pi 只备份）。

## 0. 一句话

**AI 负责理解与匹配，程序负责编号、校验、保存、计算。** 一切改动先成为 SQLite 账本里的一条事件；
掌握度与准备度由程序按时间顺序折叠得到；节点 Markdown 是程序生成、可重建的人可读视图，不是数据来源。

## 1. 已落实的决定 → 代码位置

| 讨论里的决定 | 落在哪 |
|---|---|
| 旧书图封存（LADR/EGIU 不迁移） | 不碰 `knowledge_graph/`、`link_and_mastery.py`；新系统独立账本 `state/kj/kj.db` |
| 笔记机制取消，笔记=依附节点的记录 | `record.add` 事件（kind=reading/note/handwriting/…），`[3hex]-*.md` 流水线不再扩展 |
| 数据库为唯一权威账本，Markdown 只放可重建摘要 | `store.Ledger`（events + 投影）；`markdown.VaultWriter` 写 `$OBSIDIAN_VAULT/KJ/节点/*.md` |
| 前置关系来自登记时的原文依据，不做整本预扫描 | `register.add_relation(type="prereq")` **必须带 evidence**；Wikidata 映射表**没有** prereq |
| 关系可随时登记与改变、追加式、程序校验成环、自动重算下游 | `add_relation / retract_relation / change_relation`；`compute.prereq_cycle_path`；`compute.recompute` 沿后继闭包重算 |
| 自评只瞬间改一次 | `self_assess` 事件：折叠时 `m ← value`，之后证据照常推动 |
| 出题即绑节点、逐题回传、程序返回结论与下一步 | `register_quiz / submit_results`；`service._quiz_conclusion` |
| 定义：已有同语境先返回旧文、AI 比较后决定 | `add_definition` 的 `definition_exists → decision=keep/supersede/add` |
| 记录：每次单独追加不查重；完整读取后归并 | `add_record` 无查重；`merge_records` 生成归并记录、原记录标 `merged_into` 不删 |
| 制卡必须有节点；只改卡记录不改 Anki 字段 | `bind_card / anki_sync.make_card`（deck `KJ`，tag `kj::<id>`）；`cards/card_nodes` 表 |
| Wikidata 为公共参照：本地对应表、自动关系、换绑撤旧接新、编号占用返回两者 | `wikidata.py` + `register.bind_qid / generate_auto_relations`（`qid_taken` 错误码） |
| 检索渐进式披露、本地优先、名称/别名/关键词直搜 | `query.search`（FTS5 trigram + LIKE 退化）、`browse`、`node_detail` |
| 掌握度公式复用旧设计 | 单卡沿用 `anki_status.card_mastery`；阈值 0.2/0.45/0.65/0.85/0.8 沿用旧 KG 分桶 |

## 2. 数据模型

**账本** `state/kj/kj.db`（SQLite，WAL，合同 `kj-ledger/1`）

- `events(seq, id, kind, occurred_at, registered_at, actor, source_json, payload_json, dedupe_key)` + `event_nodes(seq, node_id)`
  - `occurred_at`=事实发生时间（可空、不伪造），`registered_at`=登记时间，两者分开。
  - `dedupe_key` 幂等：Anki 快照 `anki:<card>:<日>`；Wikidata 自动关系 `wdrel:<from>:<to>:<type>:<qid>`。
- 事件类型：`node.create/update/merge/bind_qid/unbind_qid`、`definition.add`、`record.add/merge`、`relation.add/retract`、
  `card.bind/unbind`、`anki.snapshot`、`quiz.register/result`、`self_assess`。
- **投影**（全部可由 `Ledger.rebuild()` 重放得到）：`nodes / node_aliases / definitions / records / relations / cards / card_nodes /
  card_snapshots / quizzes / quiz_items / mastery / search_text / node_fts`。
- **公共目录**（不是投影，进货得到）：`public_entities(qid, 三语 label/desc, aliases_json)`、`public_claims(qid, prop, target, rank)`、`public_fts`。

**标识**：节点 `kj:` + 10 位 Crockford base32（6 位秒级时间 + 4 位随机），与名称无关，改名不改 id。
定义/记录/关系/卷/事件分别是 `def: / rec: / rel: / quiz: / ev:` 前缀。公共编号就是 Wikidata 的 `Q…`。

**节点 kind**：concept / person / method / object / event / problem / analysis / other（不是封闭集合，未知值落 other）。
**记录 kind**：reading / note / handwriting / conversation / observation / analysis / anki / other。
**关系 type**：prereq / part_of / subclass_of / instance_of / related / causes / solves / explains / uses / example / contrast /
influenced_by / facet_of / different_from / studied_by / studies / practiced_by / custom。`prereq` 语义：`from` 是 `to` 的前置。
**source**：`{kind: pdf|epub|web|conversation|audio|image|handwriting|manual|anki|wikidata|other, book?, sha?, page?, section?, url?, quote?, ref?}`，
定义必须带；定义的语境键默认 `kind:book|sha|url|ref`。

## 3. 计算（`compute.py`，常数手定、待数据校准）

- 每节点一个标量 m∈[0,1]，None=无证据。按事件顺序折叠：
  - `quiz.result`：s = correct 1 / partial 0.5 / wrong 0；`m ← m + 0.5·(s−m)`，首条从先验 0.5 出发（一题对 0.75、一题错 0.25）；
    unanswered / undetermined 记录但不动 m。**判分更正**：同一题只在首次位置生效一次、取最终结果 → 更正证据后重算。
  - `anki.snapshot`：卡表更新 → 信号=已绑卡均值；`m ← m + 0.3·(信号−m)`（首条直接取信号）。
  - `self_assess`：`m ← value`。
- 等级：无证据 0（没碰过）/1（有定义、记录或卡）；≥0.20→2，≥0.45→3，≥0.65→4，≥0.85→5；<0.20→1。
- progress：unseen / touched / in_progress / **mastered（m≥0.8 且证据≥2 条）**。
- availability 只看**有证据显示薄弱**的前置（m 不为 None 且 <0.2）→ locked；没有记录的前置只进 unknown 清单（"没有记录≠未掌握"）。
- readiness：`no_prereq_info` / `needs_basics` / `unknown_basics` / `ready`。state（兼容旧词汇）：mastered / locked / in_progress / unlockable。
- 关系变动、任何证据事件 → `recompute(目标 ∪ 后继闭包)`。
- 判分结论（`_quiz_conclusion`）：`prereq_weak`（有前置 <0.45）/ `prereqs_ok_target_stuck`（前置过、目标 <0.45）/ `all_passed` / `mixed`；
  题数 <2 的节点列入 `insufficient`。这些是行为分支代码，不是话术。

## 4. 接口

**CLI**（Skill 执行者用；输出一行 JSON）：`python scripts/kj/cli.py <cmd>`
`search / node / browse / neighbors / stats / register --json / node-create / definition / record / merge-records / relation /
relation-retract / relation-change / bind-qid / unbind-qid / merge-node / card-bind / card-make / anki-sync / quiz --json /
quiz-result QUIZ --json / self-assess / rebuild / rebuild-md / wikidata-import / wikidata-fetch`。
默认账本=`$CLAUDE_PROJECT/state/kj/kj.db`，没设环境变量就用**本 checkout**（`scripts/kj` 往上两级）；Markdown=`$OBSIDIAN_VAULT/KJ`。

**HTTP**（Windows Flask `127.0.0.1:5000`，前缀 `/kj` 已进 `PROTECTED_PREFIXES`，Bearer 与 session 都行）：
`GET /kj/api/search?q&limit&online` · `GET /kj/api/node/<id>` · `GET /kj/api/browse?parent` · `GET /kj/api/neighbors/<id>` · `GET /kj/api/stats` ·
`POST /kj/api/register {type,…}` · `POST /kj/api/quiz` · `POST /kj/api/quiz/<id>/result` · `POST /kj/api/relation {op:add|retract|change}` ·
`POST /kj/api/self-assess` · `POST /kj/api/anki-sync` · `POST /kj/api/rebuild-md` · `POST /kj/api/wikidata/fetch {qid}`。

**助手工具**（`assistant.py` 命名空间 `kj`，MCP 经 `assistant_call_tool` 可达）：
`kj_search / kj_node / kj_browse / kj_register / kj_relation / kj_quiz / kj_quiz_result / kj_self_assess`。

**统一登记 `register` 的 type**：node / node_update / merge_node / bind_qid / unbind_qid / definition / record / merge_records /
relation / relation_retract / relation_change / card / card_make / quiz / quiz_result / self_assess。

## 5. Markdown 页（`$OBSIDIAN_VAULT/KJ/`）

- 文件名 `<安全名称>·<短id>.md`；改名 → 旧文件删、新文件建，**所有邻居页链接由程序重写**；合并 → 被并节点页删除。
- frontmatter（2026-09-06 晚按用户反馈加厚）：`kj_id`、Obsidian 原生 `aliases` / `tags`（`kj`、`kj/<kind>`）、`kind`、
  `wikidata` / `wikidata_url` / `wikidata_label`、`mastery`（无证据写 `null`）/ `mastery_level` / `progress` / `availability` /
  `readiness` / `state` / `evidence_count`、`prereqs` / `successors` / `related` / `weak_prereqs` / `unknown_prereqs`（都是 `[[链接]]`）、
  `sources`（出处清单）、`definitions` / `records` / `cards` 计数、`obsidian_url`（本页深链）、`updated`。
- 正文：`# 名称`、别名、摘要、公共编号 → `## 定义`（原文多行保留 + 来源行 + 原文引用 blockquote + 语境键）→
  `## 关系`（前置/后续/其它，`[[链接]]`）→ `## 掌握与准备度` → `## 记录`（**多行排版保留**：首行跟 `- **日期** [kind]`，续行缩进两格；
  来源行、文件与页序、原文引用）→ `## 卡片`。
- 深链 `obsidian://open?vault=<OBSIDIAN_VAULT_NAME>&file=KJ/节点/<文件名>`：`kj_node` / `kj_search` 的返回里都带 `obsidian_url`；
  `card-make` 把绑定节点的深链写进卡片背面来源栏（复习卡 UI 的"来源"按钮与 Anki 桌面版都能点开）。
  ⚠ vault 名取 `OBSIDIAN_VAULT_NAME`（默认 `Obsidian Vault`），iPad 上 Obsidian 的 vault 名必须一致，否则打不开。
  侧栏 Markdown 的消毒器 `rc-md.js` 2026-09-06 起显式放行 `obsidian:` / `bwreader:`（此前 DOMPurify 默认表剥掉 href，链接看得见点不动）。
- `节点索引.md` 按 kind 列全部节点。手改无效：下次渲染会被覆盖；改动请走登记工具。

## 6. Wikidata 公共目录

- 两条进货路径：`wikidata-import <minimal-index.jsonl.gz>`（Codex 侧 `wikidata_measure.py` 的输出格式；**必须给 `--only-qids` / `--require-lang`
  过滤**，1 亿实体不能全灌）与 `wikidata-fetch Q…`（在线 Special:EntityData，按需、带缓存）。
- 属性映射（`wikidata.PROP_MAP`）：P279 subclass_of、P31 instance_of、P361/P527 part_of、P1542/P828 causes、P737 influenced_by、
  P2283 uses、P1269 facet_of、P1889 different_from、P2579/P2578 studied_by/studies、P1855 example、P3095 practiced_by。**没有 prereq**。
- 两端都绑了编号的公共关系 → 本地关系 `origin=wikidata`；换绑/解绑撤回旧的、生成新的；分类路径 `path_up` 只作定位线索。
- **Token 实测**（2026-09-06，`scripts/kj/measure_tokens.py`，tiktoken o200k_base，在线取数）：默认路径"直搜 + 读节点"约 900 token
  （node 只取必需字段约 560）；最差情况从"结构"逐层下钻到"向量空间"5 层、每层 20 候选合计 1610 token，累计上下文约 4375。
  高分支层（代数结构 93 个直接子类、向量空间 32 个）必然截断，分页策略仍未定案。SPARQL 端点要求带联系方式的 User-Agent。

## 7. Anki

- `card-make` 走 AnkiConnect `addNote` + `changeDeck` 归位（addNote 的 deckName 不生效，见 CLAUDE.md），tag `kj` + `kj::<节点id>`，绑定写账本。
- `anki-sync`：所有活跃绑卡 → `notesInfo → cardsInfo → fsrs_memory_map → card_mastery` → `anki.snapshot`（按卡+日幂等）→ 重算。
  **目前手动/按需跑**；定时挂载（Windows 计划任务）是下一步。

## 8. 维护

- `rebuild`：清空投影 → 重放事件 → 重算全部 → 重渲染全部页。任何时候都能从 events 表还原。
- `rebuild-md`：只重渲染 Markdown（含清理孤儿页：带 `kj_id:` 头但节点已不活跃）。
- 备份：`state/kj/kj.db`（连同 `-wal/-shm`）随 `state/` 一起进现有备份流程。

## 9. 首期未做（按讨论文档 18:34 节，接入时另做）

- 阅读器内"已关联"细线框 + 打开按钮 + 快捷键 + 侧栏展示节点页（App runtime + TestFlight）。
- 后台分析 → 提示板（快/慢板）两类通知出口的接线与值守（未授权开启值守）。
- 手写笔记场景串联（草稿页 → AI 读取 → `kj_search` → `record`），人物节点的统一属性模板，多终端展示卡复用。
- C# ReaderPC MCP 直接暴露 `kj_*`（现在 Codex 走 CLI Skill；Claude/外部 agent 走 `assistant_call_tool`）。
- Wikidata 全量提取完成后的批量导入策略（按 P31/P279 类别剪枝）与真实检索 Token 实测。
- 存量旧卡回填绑定到新节点（可选迁移）。
