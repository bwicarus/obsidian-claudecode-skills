# Skill: KJ 知识节点

不是命令，是 Claude session 里处理「知识节点 / 掌握度 / 学习排查 / 出题判分」时的固定工作规范。
规格全文：[`references/kj-node-system.md`](../../references/kj-node-system.md)。设计讨论：Obsidian `AI助手专用/已有项目/KJ知识点系统设计讨论.md`。

## 分工铁律

- **AI 只做理解与匹配；编号、校验、保存、计算全在程序里。** 不要自己算掌握度、不要自己判"已掌握"。
- 掌握度只由证据推动：答题判分（`quiz-result`）、Anki 复习快照（`anki-sync`）、用户明确自评（`self-assess`，只改一次）。
  阅读、查词、讨论都是**记录**，不是掌握证据。
- 关系里只有 `prereq` 影响准备度，且**必须带原文依据**（哪句话说明学 to 要先会 from）。Wikidata 的分类/组成只是线索。

## 入口

```bash
python scripts/kj/cli.py search "向量空间"            # 本地优先；--online 本地与公共目录都没有时再上网
#   中文搜不到/只出论文 → 换英文或日文原名再搜（三语标签都索引；安培环路定理=Ampère's circuital law、五段动词=五段活用、定语从句=relative clause）
#   绑编号前对照候选的 description / description_en / aliases / path 与书里的定义；拿不准先不绑，绑错 bind_qid 换绑。绑定后三语名称与别名自动回填成节点别名
python scripts/kj/cli.py node kj:XXXXXXXXXX          # 位置/前置/定义/记录摘要/卡/掌握/准备度/next_hint
python scripts/kj/cli.py register --json '{"type":"record","node_id":"kj:…","kind":"reading","text":"…","source":{"kind":"pdf","book":"LADR","page":12}}'
```

在 Windows 本机 Flask 里同一套能力是 `/kj/api/*`；书内侧栏助手与 MCP 是 `kj_*` 工具（`assistant_call_tool`）。

## 固定流程

**入库（读书 / 制卡 / 记笔记 触发）**
1. `search` 定位：有本地节点 → 用它；只有公共候选 → `node-create --qid Q… --fetch`；都没有 → 按原文与语境新建（`node-create`）。
2. 登记内容：定义用 `definition`（必带 source；同语境已有定义会先返回旧文，比较后带 `--decision keep|supersede`）；
   带 `--uses A,B` 申报**看懂它必须先会**的节点（编号或名称），程序以定义原句为依据登 prereq、查环、查冗余（传递可得的直连边报 `redundant` 不加）；
   返回的 `also_mentioned` 是定义里出现但没申报的节点名，逐条确认是漏了还是无关。前置只来自书里的句子，不从 Wikidata、阅读顺序、词频推；
   学习过程用 `record`（每次单独追加，不查重；不知道发生时间就不填 `--at`）。
3. 顺手登记依赖：`relation FROM TO prereq --evidence "原文…"`；成环会被拒绝并返回路径，按提示处理。
4. 制卡：阅读器路径 `reader_anki_draft` / 侧栏 `make_anki` 都**必带 nodeIds / node_ids**（已有节点沿用 → 没有就 search → 库里没有才建）；
   缺节点的草稿在桥和 App 两侧都会被拒绝。不经阅读器建卡用 `card-make --nodes kj:… --front … --back …`。
   确认入库后运行 `anki-sync`（或 `inbox`）把桥记下的卡↔节点绑定吸进账本并给卡背面补节点深链。

**学习排查（用户反复出错 / 主动求助）**
1. `node` 读目标：看 `readiness`、`weak_prereqs`、`unknown_prereqs`、`next_hint`。
2. `needs_basics` → 结合上下文提出先补这些前置；`unknown_basics` → 前置掌握未知，可提议检查，**不断言未掌握**；
   `no_prereq_info` → 关系资料缺失，帮用户梳理可能的基础，不伪造前置链。
3. 用户同意检查 → `quiz --json`（每题 `node_ids` 先绑好，`target_node` 填目标）→ 用户作答 → `quiz-result QUIZ --json`。
4. 按返回的 `conclusion` 接着做：`prereq_weak` 针对性学习；`prereqs_ok_target_stuck` 回到原问题继续讲解；`all_passed` 回到原问题；
   `insufficient` 的节点可补测。不用写死话术。

**读取记录后顺手归并**：完整读过某节点记录、发现重复表述 → `merge-records NODE --ids a,b --text "…" --occurrences n`（保留真实次数/时间/来源，原记录不删）。

## 维护

`python scripts/kj/cli.py stats` · `rebuild-md`（重渲染全部 Markdown）· `rebuild`（重放事件重建一切）· `anki-sync`（拉 Anki 复习进掌握度）。
测试：`python -m unittest tests.test_kj_ledger tests.test_kj_compute tests.test_kj_markdown_query tests.test_kj_wikidata_anki tests.test_kj_routes`。
