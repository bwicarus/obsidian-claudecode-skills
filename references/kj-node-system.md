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
- 全量 dump 自己提取：`scripts/kj/wikidata_extract.py DUMP.bz2`。默认**按 bz2 块并行**（`bz2_parallel.py`：扫 48 位块魔数得位偏移，
  每块拼成独立小流多进程解，输出与顺序解压逐字节一致；单流 bz2 在 Python 里只有约 3 MB/s，20 核并行后瓶颈变成磁盘与 JSON 解析）。
  一趟同时产出全量规模统计（实体/关系/度分布/最简行字节）与 zh/ja 子集 `minimal-index.zhja.jsonl.gz`。
  `wikidata_watch_extract.py` 等文件就位自动开跑，脱离会话运行；进度在 `state/wikidata/extract-status.json`。
  公共目录独立成 `state/kj/kj-public.db`（ATTACH 为 `pub`），几百万条不撑私人账本；打开账本时会把拆库前遗留在主库的 `public_*` 表迁走
  （否则遮住 pub 同名表，导入写错地方 —— 2026-09-07 第一次全量导入实锤）。
- **2026-09-07 全量实测**（`wikidata-20260831`，103 GB bz2，20 进程 2 小时 50 分）：展开 1.84 TB（压缩比 17.9）；
  实体 121,505,373、实体值关系 881,654,057、单流 3,662,362 块；全量最简行 54.6 GB；有中/日文标签的子集 12,328,574 个实体（10.1%），
  最简行 5.05 GB、gzip 983 MB —— 这份子集就是导入本地公共目录的对象。度分布：45% 实体 ≤3 条关系、88% ≤10、99.4% ≤50。
- **子集已导入** `state/kj/kj-public.db`：12,328,574 实体、63,765,531 条关系（去掉 deprecated 与重复），导入 29 分钟，
  加三语标签索引后 13.9 GB。检索 `search_public`：① 精确标签（索引）② 前缀范围查询（索引）③ FTS trigram（≥3 字）④ 兜底 LIKE；
  候选再按 精确 > 前缀 > 包含 排序，`SEARCH_DEMOTED_CLASSES`（论文/文章/学位论文等 P31）压后 —— 有中文标签的实体里大半是论文标题，
  不压会把"向量空间"本体挤出前几名。实测延迟 3~30 ms（"Newton" 32 ms）。
- **够不够用 —— 50 词探针（2026-09-07，数学 15 / 物理 12 / 计算机 12 / 英语语法 6 / 日语语法 5）**：第一名正确 36/50 → 修后 45/50，
  前三名含正确 48/50，平均 6 ms。修的三处：① 目录里一大半条目只有一种字形的 `zh` 标签（導數/極限/編譯器/現在進行式 都没有 zh-hans），
  查询词用 `zhconv` 转 zh-cn/zh-tw/zh-hk 各查一遍（可选依赖，缺了退回原字形；导入时也把 dump 里的 zh 变体标签写进别名——
  二次导入 2026-09-07 跑完，在已有行上覆盖 62 分钟、库 14.3 GB，探针第一名正确 45→46/50——不定式 改出 不定詞，别名只是保险，查询侧变体才是主力）；
  ② 查询比标签长（拉格朗日乘数法 vs 拉格朗日乘数）→ 排序档位 2 = 标签是查询前缀，排在"含这串字的论文"前；③ 逐字缩短前缀召回
  原来只在候选全空时走，论文标题以查询词开头就把正确条目挡在候选外 → 改为没有精确同名就走。
  **结论**：数学/物理/计算机核心概念足够（39 词全有条目、38 个第一名正确）；给不了的是教学前置关系（设计上就不靠它）、
  大陆教学语法名（现在进行时/定语从句/过去完成时 在 Wikidata 是 進行時態/關係子句/過去完成時，常只有繁体或英日文标签，命中 3/6）、
  日语语法术语的中文标签（五段活用/連用形 只有日文）、教材私有概念（本来就该作本地节点不绑编号）、只有英文标签的条目（不在子集里，
  `--online` 取一次即缓存）。剩余漏的全是命名差异（安培环路定理=安培定律、定语从句=關係子句、五段动词=五段活用），
  对策是 AI 换英/日文原名再搜 —— 已写进 `kj_search` 工具说明与两份技能文件。子集噪声：310 万论文、158 万人物、120 万分类页、
  15.8 万消歧义页，靠 P31 压后处理。
- **它能当"挂点"，当不了"网"（2026-09-07 量过）**：探针 50 个概念在公共目录里彼此直接相连的只有 3 对；2 跳内（共享一个公共邻居）数学 15 个
  连成 4 块、物理 12 个 5 块、计算机 12 个 10 块、语法术语几乎全孤立。P279 往上一两层有用（排序算法/物理定律/语态），再往上进哲学本体
  （客體/實體/類）不汇到课程根；真正的领域挂钩是 facet_of / studied_by / part_of（线性代数、静电学、操作系统）。结论：Wikidata 提供的是
  **身份**（跨书跨语言判定同一概念、消歧）+ 粗定位；概念之间的网与前置只能从书来。属性映射扩充、共同挂点建议等不再投入。
- **编号的价值兑现在别名回填**：绑编号（建节点带 qid / `bind_qid`）时把实体的三语标签与别名写成节点别名，`origin='wikidata'`
  （`node_aliases.origin` 列，2026-09-07 迁移加；事件 `node.aliases_sync`），本名/过长/重复的跳过、最多 16 个；用户自填别名 origin 为空、
  `node.update` 只替换这部分；换绑/解绑时 wikidata 别名整体收回再回填新的；`rebuild` 可重放。这样第二本书用 eigenvalue / 固有値 来搜，本地节点搜得到。
- **给 AI 核对用的内容**：`search` 公共候选默认 5 个（`public_limit` 可加），每个带 `label_en` / `description`(100 字) / `description_en`(80 字，
  与 description 相同时不带) / `aliases`(≤4，样本) / `path`(1 层) / `local_node`；`node` 详情的 `public` 块也带 `label_en` / `description_en` / `aliases`(≤6)。
  用法是绑编号前对照书里的定义与所属领域，拿不准先不绑（编号随时可补），绑错 `bind_qid` 换绑。
- **离线 Token 复测**（o200k）：`search 向量空间` 8 个候选带 2 层分类路径 692 token → 精简为简述 100 字 + 1 层路径后 502；
  `node` 详情（含公共邻居）696，必需字段 189。默认路径一轮约 1200 token（在线版约 900，多出来的是本地目录给出的公共邻居与路径）。
- 属性映射（`wikidata.PROP_MAP`）：P279 subclass_of、P31 instance_of、P361/P527 part_of、P1542/P828 causes、P737 influenced_by、
  P2283 uses、P1269 facet_of、P1889 different_from、P2579/P2578 studied_by/studies、P1855 example、P3095 practiced_by。**没有 prereq**。
- 两端都绑了编号的公共关系 → 本地关系 `origin=wikidata`；换绑/解绑撤回旧的、生成新的；分类路径 `path_up` 只作定位线索。
- **Token 实测**（2026-09-06，`scripts/kj/measure_tokens.py`，tiktoken o200k_base，在线取数）：默认路径"直搜 + 读节点"约 900 token
  （node 只取必需字段约 560）；最差情况从"结构"逐层下钻到"向量空间"5 层、每层 20 候选合计 1610 token，累计上下文约 4375。
  高分支层（代数结构 93 个直接子类、向量空间 32 个）必然截断，分页策略仍未定案。SPARQL 端点要求带联系方式的 User-Agent。

## 7. Anki

- `card-make` 走 AnkiConnect `addNote` + `changeDeck` 归位（addNote 的 deckName 不生效，见 CLAUDE.md），tag `kj` + `kj::<节点id>`，绑定写账本。
- `anki-sync`：先吸收桥的绑定账本（下节），再对所有活跃绑卡 → `notesInfo → cardsInfo → fsrs_memory_map → card_mastery` → `anki.snapshot`（按卡+日幂等）→ 重算。
  Anki 不在线时绑定仍被吸收，只是报 `anki_unavailable`。`inbox` 子命令只做吸收。

### 7.1 阅读器制卡必须绑节点（2026-09-06 深夜，用户拍板"从根本上修"，fail-closed）

**症状**：Codex 用 `reader_anki_draft` 出草稿 → 用户在 App 确认 → 桥 `anki-add-cards-local` 写进桌面 Anki，整条链路没有任何一处要求节点，
"制卡自动关联节点"的约定只活在提示词里，一忘就漏。

**根本修法**：草稿载荷与入库请求都新增必填 `nodeIds`（1~8 个 `kj:XXXXXXXXXX`），每一层 fail-closed，缺了直接拒绝、不返回成功：

| 层 | 文件 | 规则 |
|---|---|---|
| 桥 MCP 工具 `reader_anki_draft` | `ReaderContextMcpServer.cs` schema/normalizer | `nodeIds` 必填；精确来源形态 `file/target/sourceText/cards/nodeIds`，通用形态 `cards/nodeIds` |
| 桥输出校验 | `ReaderRealtimeOutput.cs` `KjNodeIdRules` + `ValidateKjNodeIds` | 数组 1~8、格式 `^kj:[0-9A-HJKMNP-TV-Z]{10}$`、不重复 |
| App 入站闸 | `rc-computer-voice.js` `normalizeKjNodeIds` | 同上（白名单副本） |
| 本地卡仓 | `card-repository.js` `source.kjNodes` | 逗号分隔文本随卡持久化 |
| 确认入库（App→桥） | `rc-flashcard.js exportToComputerAnki` / `normalizeLocalAnkiAddRequest` / `DirectBridgeProtocol.HandleLocalAnkiAddAsync` | 请求必带 `nodeIds`；没有 → `BW_READER_ANKI_NODE_REQUIRED` |
| 写 Anki | `ReaderLocalAnki.cs` | tag `kj` + `kj::kj_XXXXXXXXXX`；成功后追加 `runtime/kj-card-bindings.jsonl`（`kj-card-binding/1`） |
| 侧栏助手 `make_anki` | `assistant.py _t_make_anki` | `node_ids` 必填并校验存在（`_kj_require_node_ids`）；结果带 `node_ids` → 卡仓 `kjNodes` |
| 服务端草稿登记 `/pdf/api/anki-draft` | `pdf_reader.py` | 可选 `nodeIds`，给了就校验存在并存进实体 |
| KJ 吸收 | `anki_sync.ingest_bridge_bindings` | 读绑定账本（游标存 meta）→ `card.bind`（card_key=`anki:<note>` 幂等）→ 给卡背面追加节点深链；节点不存在落 `state/kj/unresolved-bindings.jsonl` |

**AI 的固定流程**（写进工具描述与 Skill）：已有节点直接沿用；没绑定但库里有 → `kj_search` 找到；都没有 → `kj_register type=node` 建；然后把 id 传进 `reader_anki_draft` / `make_anki`。
**已知缝**：桥只校验编号格式，不校验存在（桥没有 KJ 账本的访问权）；编号不存在的绑定在吸收时落 unresolved 文件。
**滚动升级窗口**：新桥（带 nodeIds）× 旧 App 会在 App 入站闸被拒；新 App × 旧桥出的草稿没有 nodeIds、确认时被 App 挡下。两端要一起升。

### 7.2 前置关系怎么产生（2026-09-07 定稿）

Wikidata 没有教学前置（§6 量过：能当挂点当不了网），前置只有一个来源：**书里"定义/陈述 B 时用到了 A"的那句话**。分工：
语义判断归 AI（定义原文就在它眼前的那一刻），要依据、查环、查冗余、记账归程序。

- `definition` 接受 `uses`（编号或名称/别名，或 `{node, type: prereq|uses}`，默认 prereq）：程序解析到节点（`resolve_node_ref`，名称多义返回
  `ambiguous_uses`，找不到返回 `unresolved_uses`，**不自动建节点**），以定义原句（截 300 字）为 evidence、定义的书页为 source、`origin=definition`
  登 prereq；只是出现不构成门槛的登 `uses`，不进 readiness。
- **传递冗余**：`prereq_path`（`compute.py`）沿已有前置链能从 A 走到 B 时，直连 A→B 报 `prereq_redundant` 不加（`definition` 里进 `redundant` 桶，
  `relation` 直登返回该 code；确有必要 `allow_redundant=true`）。冗余边越多，节点被"前置薄弱"锁住越没道理。
- **成环**：`prereq_cycle` 除环路外带 `path_names` 与 `edges`（每条边的 relation_id / 依据 / 书页来源）——两本书教学顺序打架时一眼看出该撤哪条。
- **防漏清单**：`also_mentioned` = 定义原文里出现、但没申报的已有节点名/别名（`mentioned_nodes`：拉丁词整词、中日文子串、单字不匹配）。
  纯字串匹配，只当清单，不当判断。
- 不产生前置的：Wikidata 关系、阅读顺序、词频、卡片绑定、答题结果。

**下一步（未做）：按书全量通读。** 用户 2026-09-07 拍板：不做定义块模式匹配/排版/索引比对那套（不通用、复杂、会漏），改为 AI 通读整本书，
一次产出：节点+定义（定理/引理也建节点）、前置（uses）、公共编号绑定、记号与叫法作别名、书内位置与习题清单（记录，供出题当真题库）、
书里点明的坑（记录）、**每页与每章的内容标注**（页：干什么/出现哪些节点/关键记号；章：摘要/概念顺序/依赖前面哪些概念；存书侧边车并喂目录系统）。
不批量建卡。成本：一章 15~30 页输入 1~2.5 万 token，一本 300 页教材约 25~30 万，一次性，走凌晨闲时额度；按章断点续跑、重跑不重复。

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
