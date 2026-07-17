# 通用知识库 / 注意力画像系统 —— 设计讨论稿(2026-07-17,状态:构想探讨中,未动工)

> 用户构想:多渠道字符流(阅读器行为/AI 识别/智能眼镜眼动)→ 加权分词统计 → 时间衰减排序
> = 当前学习焦点;通用词筛除;双时间线近似缩小到某书某页;跨材料同知识点自动关联(超阈值 AI 判定);
> 最终全自动调用各界面。本文 = 构想 ↔ 成熟技术的映射 + 研究扫描结论 + 架构草案。
> 研究全文(50+ 来源)见 workflow wf_a5c61a28 输出。

## 1. 构想 ↔ 成熟技术对照(用户独立想出的东西基本都有正名和现成轮子)

| 用户构想 | 学界/工业界正名 | 成熟做法 |
|---|---|---|
| 渠道加权词频+排序 | term weighting / user interest profiling | TF(衰减)× IDF;BM25 饱和防单日爆量 |
| 时间段输出→当前学习重点 | short/long-term interest + **burst detection** | 双画像:短半衰期 7-14d + 长 60-180d,α≈0.65 融合;burst=Kleinberg 或 z-score(7d vs 8周基线 >2) |
| 通用词/跨学科共通词筛除 | 停用词 + **IDF**(跨文档频率天然自适应,不必手工整理) | 各语言停用词表 + vault 全库 IDF 下限双保险 |
| 眼动不精确→收录范围内全部字符 | AOI(兴趣区)聚合 / gaze×语言先验(EyeLingo 范式) | 区域级停留时长聚合;区域内用词频/用户已知词表挑"卡壳词" |
| 双时间线近似缩小到某书某页 | temporal/session correlation | 事件流带 ref(book+page),时间窗 join |
| 跨材料同知识点关联+AI 判定 | entity/concept linking | **词面(FTS5 已有)→ 向量 → AI 确认**三层漏斗 |
| 全渠道捕获→索引→检索 | Rewind.ai / Windows Recall 同款架构 | 本地 SQLite + FTS(+向量双轨);Rewind 实测 26MB/小时,个人规模 SQLite 足够 |

**工程核心技巧**:流式衰减累加器——每词只存 (score, last_ts),新事件来时
`score = score·2^(-Δt/half_life) + weight`,O(1) 更新,Pi 零压力,不用全量重算。

## 2. 构想需要修正的三个点

1. **粒度:"单词"不够,要多词术语**。知识点常是名词短语("人口動態統計")。现成:JA=已装的
   unidic/fugashi + termextract(LRValue 复合名词重构)或 SudachiPy C 模式;EN=已有 spacy 常驻
   server 的 noun_chunks(或 YAKE);ZH=jieba.textrank。
2. **"跨学科交叉找共通词"就是 IDF 的定义**——不用自己造,文档频率直接算,且随库自适应
   (比人工停用词表强:能压掉"定义/例题/证明"这类自库高频泛词)。
3. **纯词面关联会漏同义表述**(汉字词 vs 假名写法、中日同形、译名差异)。2026 成熟解=轻量多语
   embedding:**Model2Vec potion-multilingual-128M**(bge-m3 蒸馏,CPU 比 sentence-transformers
   快 ~500x,盘上 8-100MB,Pi 上毫秒级查表)夜间批量嵌入;个人规模 numpy 暴力余弦即可,
   不需要向量数据库。**双闸防误连**:相似度>0.55 且共享≥1 个画像术语,模糊对儿夜间交 AI 复核
   (对 Obsidian Smart Connections 的精度改进)。

## 3. Maverick AI Pro 眼镜——现实检查(2026-07 已核实)

- 产品真实:Everysight(以色列,军工 AR 背景),CES 2026 发布,**Kickstarter 众筹中,
  预计 2026 Q4 发货**(AI Pro 档=相机+GazeIntent 眼动,早鸟 $359)。
- **SDK 今天没有 gaze API、没有相机帧 API、没有 AI 识别结果 API**(现售款本来无相机无眼动;
  厂商未承诺开放原始注视点,从"端上闭环"叙事推断更可能只给高层"gaze 选中"事件)。
  SDK 今天给的:镜片渲染 + IMU/触摸/环境光/接近事件(第三方 app 跑手机上,数据转发自己服务器零限制)。
- 盯更新:everysight.github.io/maverick_docs;GitHub org everysight-maverick。
- 对照:要相机帧今天选 Mentra Live(SDK 直接暴露拍照/直播)或 Brilliant Labs Halo(全开源);
  消费级"眼动+相机+全开放 SDK"三者兼得今天不存在;原始 gaze 流只有研究级(Pupil Labs Neon ~€6000)。
- **规划含义:眼镜是 2027 年的输入插件,不是地基。架构按"渠道可插拔"设计,先用现有渠道跑通。**

## 4. 眼动阅读研究的设计确认(用户"范围收录"直觉是对的)

- 词级 fixation↔词汇难度关系稳固(E-Z Reader/SWIFT,数十年数据):卡壳词注视更长、跳读更少。
  但**消费级精度(2-4°)横跨 1-3 行,词级/行级直接定位不可行**;区域级可行(2×2 格 88%,
  左右二分 98%),**横轴远好于纵轴**。
- **EyeLingo 范式**(arXiv 2502.10378):gaze 给粗区域 × 语言先验(词频/用户已知词表)在区域内
  反推卡壳词,97.6% acc——"收录范围内全部字符再统计"正是这条唯一现实路线。
- 必做:垂直漂移校正/行吸附(Carr et al. 2022,开源 Python);聚合用**停留时长**不是注视次数;
  连续隐式重标定(借点击/翻页锚点),否则越读越偏。
- **多信号融合>纯 gaze**:眼动+交互信号(滚动/点击/停留)read-level 87% vs 纯启发式 43-46%。
  本系统已有海量显式信号(点词/翻页/高亮/振假名点触)——gaze 是"再加一路弱先验",不是主信号。
- 走神/理解检测只比随机高一截(F1≈0.59)→ 只做软提示,不自动改学习状态。

## 5. 架构草案(六层,全部映射现有基建)

```
① 事件层  state/attention-events(SQLite):{ts, channel, weight, text, ref{book,page}}
          渠道与权重(草案):查词=1 高亮=3 AI问答=2 新建笔记=5 Anki lapse=2 检查报告错题=4
          眼镜(未来)=0.5;★原料已存在:vocab lookup 日志/高亮 sidecar/QA SQLite/检查报告/
          reading-pos——只差汇聚埋点
② 术语层  语言路由(已有 is_japanese)→ JA: unidic+termextract / EN: spacy noun_chunks / ZH: jieba
          停用词表 + vault IDF 下限
③ 画像层  流式衰减累加器×2(短 7d/长 90d),日频过 BM25 饱和(k1=1.2),
          score=(0.65·S_short+0.35·S_long)×IDF_vault
④ 焦点层  top-N + z-score burst → "当前学习焦点/注意力时间线" → /insights 新面板
⑤ 关联层  夜间批(挂 figures-describe 式 off-peak timer):新增高亮/摘录/笔记段 →
          potion-multilingual-128M 嵌入 → numpy 余弦 top-k → 双闸(>0.55+共享术语)→
          模糊对儿 AI 复核 → 回写 KG 链接 + 喂 review_priority 激活扩散
⑥ 呈现层  阅读器右抽屉"相关材料"(位子已有)/ 技能树挂接 / 仪表盘焦点面板 /
          "缩小到某书某页"=焦点术语×FTS5(已有 trigram 索引)×时间窗 join
```

## 5b. 地基已落地(2026-07-17,阶段 0+1 上线)

`scripts/attention_profile.py`(单文件引擎)+ quick_sync 第 5 步(每 15min)+ /insights「🎯 注意力焦点」卡片。

**关键决策(与草案的差异,均为实测驱动)**:
1. **零侵入导入 > webapp 埋点**:各渠道本来就有持久层(vocab-lookups.jsonl / 高亮 sidecar /
   assistant-convo / reader-check-reports),导入器按 src_key 幂等去重、每 15min 增量扫——
   **不需要在 webapp 里加任何埋点**,也就没有埋点漏埋/双写/事务问题。新渠道(眼镜)只需
   `add_event()` 或自己写一个导入器。
2. **全量重算 > 流式累加器**:2204 事件重算 2.3s。设计稿抄的流式累加器是百万级优化,
   个人规模用它只会引入状态 bug。等事件破 10 万再说。
3. **事件内术语稀释 w/√N**(实测必需):一条检查报告/长提问抽 12 个术语,不稀释的话
   少数几条近期事件会把几千条查词全淹没(首跑实测:榜单前 15 全是「判分/标准答案/帮我分析」)。
4. **check 只收纸标题**:report 正文是判分叙述+系统模板,不是学习内容(首跑污染源)。
5. **查词也要过分词器**(实测踩坑):`vocab-lookups.jsonl` 的 word 不干净——用户点到功能词
   (「という」「いう」)、点到整句(「全問未回答です」)。规则:ASCII 原样 / 纯汉字原样
   (「議事」别切碎)/ **含假名过日语词性**(功能词/动词自然出局,长短语抽出其中名词)。
6. QA 只收 user 消息(AI 回答不入,权重待议);沙盒路径全渠道排除。

**首跑成绩**:2676 查词 + 22 高亮 + 19 问答 + 5 检查 = 2204 事件(去重后),焦点榜实测有效:
議事/公衆/行政/貴族/弥生/feynman/衛生/感染/黄色ブドウ球菌/保健所——正是用户在学的
料理师公卫章 + 日本史 + 费恩曼物理。参数在文件头常量区(W 权重表/半衰期/ALPHA),改完
`--rebuild` 立即生效。

## 5c. 渠道现状与升级(2026-07-17 第二批)

| 渠道 | 权重 | 来源(零侵入导入) | 说明 |
|---|---|---|---|
| `lookup` 查词 | 1.0 | `state/vocab-lookups.jsonl` | 最干净的主力信号;**必须过分词器**(见 §5b.5) |
| `highlight` 高亮 | 3.0 | `pdf/epub/html-highlights/*.json` | 手动划=强意图 |
| `qa` 问 AI | 2.0 | `assistant-convo/*.json` + **归档** | 只收 user 消息(AI 回答不入,权重待议) |
| `check` 自测 | 4.0 | `reader-check-reports/*.json` | **只收纸标题**(正文是判分模板) |
| `read` 读页 | 0.5×(secs/30,封顶 2×) | `attention/dwell.jsonl` | 新增,见下 |
| `note` 新建笔记 | 5.0(预留) | — | 导入器待写 |
| Anki 答错 / 收藏 / 便签 / 手写 | — | — | 待接 |

### 对话归档(用户设计:对话删了,询问的痕迹要留)
- `assistant._convo_archive()`:**清空对话**(🗑)或**超 200 条被截断**时,把消息剥成纯文本行
  追加进 `state/assistant-convo-archive/<uid>.jsonl`(只留 role/content/ts/page/file_rel/book/via)。
- `_convo_drop_media()`:同时**级联删语音 clip**(`state/voice-clips/<id>.*`)——用户设计:
  文本留档、媒体跟对话一起消失。
- 保留期 `ARCHIVE_KEEP_D=180` 天,由 `import_convo_archive` 顺手裁剪(唯一清理点)。
- ⚠ 为什么必须归档(不是"事件表已经有了就够"):事件表确实只进不出,但 `--rebuild`
  (分词算法升级时重导全量)会重读源文件——源没了,历史就真丢了。归档=重建能力的保险。

### 读页 dwell(用户要求"很严谨的判断:是否真读过某一页")
三重排除**全在采集端**(`reader.src/30-dwell.js`,每秒 tick):
1. **卡加载排除**:当前页的页图 `img.complete && naturalWidth>0` 才计秒(渲染不出来=看不见=没读);
   自建页/canvas 走等价判据(`.up2-blocks`/`canvas`/`.textLayer` 存在)。
2. **快翻排除**:单页累计 <3s 的碎片**不上报**(翻过≠读过);服务端再设 `DWELL_MIN_S=15`(同页同日累计)。
3. **挂机排除**:60s 内无任何交互(scroll/touch/pointer/key/wheel)停表;`visibilityState!=visible` 停表。
上报:每 30s / 切后台(sendBeacon)flush → `POST /pdf/api/read-dwell` → **append-only 原始秒数**
(`state/attention/dwell.jsonl`)。**判定阈值留在服务端聚合器**(`DWELL_MIN_S`)——阈值想改就改,
历史原始数据可重放,不用重新采集。事件文本 = 该页正文前 400 字(离线从 `pdf-char-cache` 拼,
不依赖 webapp);权重随停留时长小幅加成(0.5×secs/30,封顶 2×)。沙盒书全程不采。

## 5d. 页码漂移 / 虚拟页码 / 双权重(2026-07-17 第三批,用户点破)

### 页锚迁移补全(P0 真 bug —— 违反了项目自己的铁律)
`pdf_reader.py` 顶部写着「⚠ 未来任何新增按 PDF 页号锚定的存储,必须在 PAGE_ANCHOR_MIGRATIONS
登记迁移器」,而画像系统写的**四个源全裸奔**(插/删页后记录永久错位):
| 源 | 删页时的语义(各不相同,按信息价值定) |
|---|---|
| `vocab-lookups.jsonl`(查词,画像主力源) | page→0(**词本身仍是有效学习信号**,只丢页锚) |
| `assistant-convo/*.json` + `-archive/*.jsonl` | page→0(对话内容仍有效) |
| `attention/dwell.jsonl`(读页停留) | **整条丢弃**(「读过某页」在那页没了之后无意义,同高亮语义) |
| `attention/events.db` | 派生数据 + **src_key 含 page**(增量修会产生重复)→ 落 `.rebuild-needed` 标记,
  `attention_profile.run()` 见到就 `--rebuild`(2.3s) |
新增 `_up_jsonl_plan()`(jsonl 版的 write plan,与 `_up_json_plan` 同协议、可回滚、坏行原样保留)。

### 虚拟页码(用户设计)——自建页永不漂移
自建页本来就有稳定 id(`u_xxxx`,插删都不变)。dwell 采集端遇到 `.pdf-upage` 时**记 uid 而非页码**
(`{upage:"u_xxx"}`),聚合键、事件、迁移器全线认它 → **天然免疫页码漂移**;正文从
`reader-userpages` sidecar 取(`_upage_text`)。
⚠ 权衡讲清楚:真实页救不了——插入页是**真插入 PDF 文件**(规格 v2,为了可移植+全文搜索),
物理页号必变,只能靠迁移器。要让真实页也免疫就得退回 v1 虚拟叠加,得不偿失。

### 双权重(用户设计:每个数据 = 基础权重 × 时间权重)
- **全局画像**(`rebuild_profile`)本来就是双权重:`w_渠道 × 2^(-Δt/半衰期)`(短 7d + 长 90d 融合)。
- **时间窗查询**(`focus_window`)原先只有基础权重(窗内平等)→ 补上**窗内衰减**:半衰期 = 窗长/3
  (窗越长半衰期越长;窗 ≤2 天不衰减——一天内先后没意义)。效果实测:「上个月」榜单从
  theorem 第一 → **議事**第一(月末学的压过月初的),更贴合「那段时间的焦点」。

## 5e. 跨语言归一(2026-07-17,用户要求;**数据驱动的分层结论**)

用户要求「相同含义的不同语言单词在数据库中被视为同一单词」。动手前先**用真实数据量了三件事**,
结论出人意料:

| 检验 | 实测 |
|---|---|
| 英文术语经 ECDICT 中文释义对上库里汉字术语 | 16 例,**过半是误连**(set↔结果、now↔刚才、concern↔有关) |
| 字形变体成对出现(証明 vs 证明) | **0 组** |
| 英文词形未归一(proof/proofs) | 1 组(且是 ECDICT 脏数据 also→conjurer) |

**根因**:学习域按语言分离——英文材料=数学/物理/英语语法(theorem/proof/feynman),
日文材料=料理师/应用情报(公衆衛生/議事),两边不讲同一件事,**语义归一现在没有可归的对象**;
而中日同形词(平均寿命/人口動態統計)字形相同,本来就天然归一。

### 落地:L1 字形层(做了)+ L2 语义层(暂缓)
- **`norm_key(term)`**:NFKC(全角半角)→ `_JP_ONLY` 表(62 字,只补 **opencc 覆盖不到的
  日本新字体特有字**:経/発/図/単/価…)→ **opencc t2s**(繁→简,几千字成熟表;pip 版没有
  jp2t 配置,所以两级分工)。实测 11/12 归一正确,且**不过度合并**(証明 ≠ 証明書 ✓)。
- **term/key 分离**(关键设计):事件表存原形不动;聚合(`rebuild_profile`/`focus_window`/
  `focus_of_text`)与 IDF 全部按 key 分组;**显示用组内最高频原形**(书里怎么写就怎么显示),
  `alt` 字段列出同键的其它写法。→ 归一算法改版只改 `norm_key`,历史数据不用重导。
- 实测合并到 1 组:**`変化`(日语书)+ `变化`(中文提问)** → 同一个词。
- **L2 语义层暂缓**:等真出现「同一知识点两种语言材料」再上多语 embedding + 双闸(§2.3);
  届时只换 `norm_key` 实现(key 机制已贯通),不动别处。ECDICT 释义映射**实测不可用**(误连过半)。

## 5f. 外部 AI 审查的处理(2026-07-17)——真金、幻觉与我的取舍

用户带来一份外部 AI 的详细建议(Hermes 记忆分层 / learning.db / 多粒度分词)。**逐条核实后**的处置:

### 属实并已修(它的真金:自己做了实测)
1. **分词路由 bug**(实锤,见 git 3d1bee3):纯汉字日语被 jieba 切碎。已按书语言路由 + 加接尾辞。
2. **`events.db` 直写会被 rebuild 删掉的矛盾**(实锤,我自己写的注释误导)→ 见下「账本」。

### 核实为幻觉/失真(**别当权威引用**)
- **「Hermes 的五层记忆分层(工作/原始事件/情节/语义/程序)」= 幻觉**。shallow clone 全仓库 grep:
  `episodic` 0 命中、`semantic memory` 0 命中、`five layer` 0 命中。Hermes 自述架构是
  **三件套**:两个记忆文件(MEMORY.md/USER.md)+ 会话搜索(SQLite FTS5)+ Skills。
  五个词里只有「程序性记忆」是它的原话(专指 Skills)。那个五层是把认知科学分类事后套上去的。
- **「OpenAI 官方建议保留完整 trace / 从真实失败案例建数据集做回归评测」= 转述失真**。
  三个链接都真实,但原文只说「先用 traces 调试行为 → 再转 datasets/eval runs 求可复现」,
  没有留存策略、没说从失败案例取数,还把「发现回归」从 trace grading 错挂到了 dataset。
- 属实的部分:Hermes Agent 真实存在(NousResearch/hermes-agent,MIT);
  「~1300 token 常驻(2200+1375 字符上限)+ 完整会话存 SQLite FTS5 按需搜索」**逐字对得上源码**;
  OpenAI sideband「工具/业务逻辑留服务端」属实。

### 决策一:**不引入 Hermes**,但抄它三个设计取舍
它是**完整 agent 平台**(271MB / 7405 文件 / desktop+CLI+各种 IM gateway),不是能 pip 进
Flask 的库;而它的记忆设计极简(两个有字符上限的 md + 一张 FTS5 表),自建成本远低于扛进平台。
值得抄的三条(**已在本项目对应物上生效或待用**):
1. **超限报错、不自动压缩** —— 逼 agent 自己合并/删条目(比静默截断诚实);
2. **frozen snapshot 保 prefix cache** —— session 中途改记忆立即落盘但下次 session 才进 prompt;
3. **常驻记忆(贵、恒定 token)与会话搜索(免费、按需)明确分工** —— 我们的对应物:
   `_sys_prompt` 常驻清单 vs `recall_creation`/`learning_focus`/FTS5 按需取回。**已是同款范式**。

### 决策二:**不建 learning.db**,改用 `raw-events.jsonl` 账本(更简单地解决同一个问题)
外部建议「建不可重建的 learning.db 三张表,events.db 降级派生索引」。诊断对,处方过重:
- **本项目 5 个渠道都有天然 append-only 源**(查词 jsonl / 高亮 sidecar / 对话+归档 / dwell /
  检查报告),零侵入导入器已在跑 —— 全量迁 learning.db 等于给每个源再造一份副本,双写+一致性全来了。
- 真正缺的只有一个口子:**没有天然源的新渠道**(眼镜 gaze / 外部 App / 工具调用)往哪写。
- → `state/attention/raw-events.jsonl`(**append-only,永不删,--rebuild 也重导它**)+
  `append_raw(channel, text, ...)` 统一入口 + `import_raw()` 导入器。**20 行解决,零新数据库**。
  实测:写账本 → `--rebuild` 删库 → 事件仍在 ✓。
- `add_event()` 降为内部函数(只给导入器用),注释写明「新渠道用 append_raw」。
- 何时才真需要 learning.db:事件破 10 万(全量重算不再秒级)、或要跨设备同步事务。**现在 2208 条**。

### 决策三:多粒度候选/term_mentions/C-value —— **暂不做**(YAGNI)
外部建议保留「人口動態統計 + 人口動態 + 統計」多候选再分级加权。但:
- 我们的产物是**给人看的焦点榜**,多粒度会让同一概念的碎片占满榜(它自己也承认这风险);
- 「用『統計』搜到『人口動態統計』」的需求 **FTS5 trigram 已经解决**(全文搜索另一条链路);
- C-value/NC-value 是**术语抽取系统**的方法,我们是注意力画像 —— 最长复合名词 + IDF 已够。
- 有价值但排后面:**领域词典从系统自己长出来**(KG 节点名/Anki 卡正面/书目录/用户查过的词
  → jieba `add_word` + fugashi 用户词典),等焦点榜用一段时间发现具体切错案例再做。

## 5g. 焦点=**下游关键数据**(2026-07-17 用户定调,推翻了此前的取舍前提)

> 用户原话:「我们的产物不是给人看的焦点榜,这个焦点结果是一个之后要被利用到各种地方的**关键数据**,
> 所以要保障数据的**可靠性、及时性、可回溯性**」。
> ⚠ 此前 §5f 驳回多粒度/term_mentions 的理由是「焦点榜会被碎片占满」——那是「给人看」的理由,
> 前提错了。三条标准重做如下(实现在 attention_profile.py 文件头常量区 + 三个函数)。

### ① 可靠性:不做不可逆截断 + 抽取器版本可追
- `TERMS_MAX 12→40`(实测原上限有 4 条事件撞顶=真丢词);`TEXT_MAX 500→4000`(派生索引截了也能
  从源重算);**账本 `append_raw` 原文一律不截**(它没有上游,截了就是永久损失)。
- `EXTRACTOR_VER`(现 4)写进 `events.xver`:**可查哪些事件是旧分词器抽的**;`add_event` 见到
  `xver < EXTRACTOR_VER` 自动重抽 → **升版即自动全量刷新**,不用手动 rebuild。
- 版本升级触发条件:改分词/归一/权重算法就 +1。

### ② 及时性:**读时保证新鲜**(不靠 15min timer)
- `_sources_fp()` = 所有源的 (mtime_ns, size) 指纹;`ensure_fresh()` 指纹没变 → 直接返回
  (**实测 4.7ms**,纯 stat);变了 → 增量导入。
- `focus_window` / `focus_of_text` / `explain` **查询前自动调** → 下游读到的永远是当下数据,
  零延迟(实测:append_raw 后立刻查,新词已在榜)。quick_sync 的 15min 降级为兜底 + 快照落盘。
- 增量导入不是全量重算的关键优化:`add_event` 对**同版本已存在的 src_key 先跳过再抽词**
  (分词是瓶颈,不是 SQL)。

### ③ 可回溯性:证据链 + 分数构成 + explain()
- 焦点条目新增:`by_channel`(分数按渠道拆)、`idf`、**`evidence`(贡献事件 id 列表,最多 20)**。
- **`explain(term, when?)`**:列出贡献这个词的每一条事件——事件 id / 时间 / 渠道 / 基础权重 /
  事件内稀释系数 / 书页 / **抽取器版本** / 原文片段 + 公式说明。下游或用户要审计焦点时调它。
- 公式:`score = Σ(渠道权重 × 事件内稀释 1/√N × 时间衰减) × IDF`。

### 附:同批修掉的**我自己引入的回归**(实锤)
修分词路由时让「纯汉字 + 书是日语书 → 日语分词器」,但**用中文提问日语书是常态** →
「这一页主要讲了什么东西啊」被 fugashi 整句吞成一个"术语",霸榜今天前几名。
修:**lang 语义按渠道分**(`add_event` docstring 已写明):
| 渠道 | lang | 理由 |
|---|---|---|
| read / highlight / check标题 / 查词 | `None`(按 file 查书语言) | 文本是**书的内容** |
| **qa** | **`[]`(按字形判)** | 文本是**用户说的话**,语言 ≠ 书语言 |
| 账本新渠道 | 显式传 | 调用方自己知道 |
效果:今天焦点头名从「读是我们亚洲人吗」变回「平均寿命」。

## 5h. 七渠道 + 账本协议 v2(2026-07-17;含对 learning.db 建议的最终判断)

### 渠道全表(接入 anki_lapse / note / tool 后)
| 渠道 | 权重 | 源 | 关键取舍 |
|---|---|---|---|
| lookup 查词 | 1.0 | `vocab-lookups.jsonl` | 主力(2161 条);word 必须过分词器 |
| **note 笔记** | **5.0** | vault 根 `*.md` | **只取标题+首段(600字)**:全文会把一篇笔记所有词拉进画像 |
| **anki_lapse 答错** | **2.0** | `collection.anki2` revlog(只读 uri) | **只收 ease=1**:全部 revlog 52174 条会淹没一切;近180天答错仅 39-51 条,量小信号最强。去 HTML/LaTeX |
| highlight | 3.0 | 高亮 sidecar | |
| qa 问 AI | 2.0 | convo + 归档 | **lang=[]**(用户话语的语言,不是书语言) |
| check 自测 | 4.0 | 检查报告 | 只收纸标题 |
| read 读页 | 0.5×(secs/30) | `dwell.jsonl` | 三重排除;自建页用虚拟页码 |
| **tool 查找** | **2.0** | **账本**(埋点) | 唯一需要埋点的渠道 —— 见下 |

**tool 渠道为什么必须埋点**(其余 6 个都是零侵入导入):工具调用**没有天然 append-only 源**
——convo 的 trace 只存编排 AI 的散文、不含工具参数;vtask 落盘只有 CLI 任务。所以走账本。
埋点位置:`assistant._creation_register` 旁(工具 done 的统一钩子,4 个调用点);
只收 `_ATTN_LOOKUP_TOOLS`(search_book/web_search/lookup_word…)的**查询词**(=用户想找什么),
失败/非查找类/沙盒都不记。

### 账本协议 v2(采纳外部建议的字段,但不采纳新数据库)
外部建议建 `learning.db`(learning_events/artifacts/memory_items 三表)。**字段设计我采纳,存储载体不采纳**:
> **协议改起来贵、存储换起来便宜** → 字段一次定对,载体先用 jsonl。

`append_raw()` 现在写:`v(schema)/ts/channel/actor(user|ai|system)/text/file/page/uid/`
`weight/hint/lang/session_id/turn_id/call_id/anchor/extra` —— 覆盖他 learning_events 的追溯字段
(turn_id/call_id/actor/schema_version)。`import_raw` 兼容 v1 老行。

**JSONL vs SQLite 的真实权衡**(他点出的多进程写入是真问题):
- POSIX 只保证 <PIPE_BUF(4096) 的 append 原子,而账本**故意不截原文** → 大行可能交错。
  实测「无锁并发写 6000B×60 行」这次没坏,但那是运气 → **加 `fcntl.flock`**(开销微秒)。
- **升级到 SQLite 的触发条件**(写死在此,别凭感觉):账本 > 10 万行(全扫变慢)、
  或需要 JOIN/索引查询、或多机写入。现在账本几十行、总事件 2373。
- `artifacts` 表**不需要**:产物本体在 vault/Anki/sidecar(那才是事实源),缺的只是「哪次学习产生了它」
  的关系 → `channel=artifact_created` + `extra={kind,uri}` 就够。
- `memory_items` 表**暂不建**:没有写入者(misconception 属学习闭环、用户明确跳过;preference/goal
  无需求)。建了就是空表。等第一个真实写入者出现再说。

### 同批修掉的实锤 bug:webapp 的路径靠环境变量猜
`webapp/.env` **只设了 CLAUDE_PROJECT、没设 OBSIDIAN_VAULT** → webapp 进程 import 本模块时
`VAULT_ROOT` 退回 config.py 的 Windows 默认 `C:\obsidian`(笔记渠道静默 0 条;账本路径也曾指向
`C:\claude/...`)。本模块被 **webapp / voice / 脚本三种进程** import,不能靠各自环境变量。
修:**路径自愈** —— 默认值不存在就按 `__file__` 位置推回项目根、vault 按同级 `obsidian/` 兜底;
顺手给 webapp/.env 补上 OBSIDIAN_VAULT(治本)。

## 5i. 跨语言概念归一 L2 上线 + 选中内容进 qa(2026-07-17,用户实锤两点)

### 我之前的判断被数据推翻(诚实记录)
§5f 说「学习域按语言分离,语义归一没有可归的对象」—— **接入 note 渠道后不成立**:
| | |
|---|---|
| 书内容里的英文术语 | 201 |
| 书内容里的日/汉术语 | 704 |
| **我的笔记+提问里的中文术语** | **1726** |
铁证:`proof`↔`证明`、`theorem`↔`定理`、`space`↔`空间`、`matrix`↔`矩阵` **同时在库里**
(书是英/日原文,笔记和提问是中文)—— **同一知识点的焦点被劈成两半**。用户的要求是对的。

### 方案:词典候选(高召回)→ 时间共现排序 → AI 判词义(高精度)
- **候选 ≠ 时间共现**(重要教训):按天共现的 top 候选是「feynman↔原子」「preface↔向量」
  —— 高频词互撞的垃圾。**时间只能排序,不能生成候选**(用户说的「靠保存时间建立联系」
  方向对,但必须配 AI 判词义,他自己也这么说)。
- **候选 = ECDICT 释义 ∩ 我的中文术语**(项目已有 340 万词条):131 对,含 change↔变化/
  compute↔计算 的真对应 + also↔并且 的噪声 = 高召回低精度,正合适当候选。
- **排序 = 共现天数 × 双边热度** → set↔集合、space↔空间、matrix↔矩阵 排到前面。
- **AI 判词义**(gemini 一次批量,夜间挂 daily『跨语言概念归一』步):结果写
  `state/attention/concepts.json` 永久缓存;`norm_key` **查表 O(1)**,热路径不调 AI。

### 防过度合并的三道闸(前两版都翻过车,记牢)
1. **按英文词分组、给书名语境、让 AI 在候选里选一个或全拒**。
   ⚠ 第一版逐对判 → `set→设定`(而《Book of Proof》里 set 是**集合**)、`element→成分`
   (数学里是**元素**)。一词多义时逐对撞运气必错。
2. **概念名必须原样取自候选**,不让 AI 自己起名(它起的「设定」在语境里就是错的)。
3. **只映射英文词 → 中文概念,中文词一律不动**。
   ⚠ 第一版把「显示」也映射到「指示」—— 近义中文词互相合并是过度合并的温床。
结果:20 条别名 / 18 个概念,`set→集合` `element→元素` 修对;噪声(set↔结果、alex↔系统、
theorem↔原理)全部正确拒绝;中文词零误伤。实测归一:`空间+space`、`element+元素`、
`set+集合`、`定义+定義`(中日)现在都是同一个词。

### 选中内容进 qa(用户实锤的真 bug)
convo 消息里**本来就有 `selection` 字段**(样例:选中「白蛤」提问),但导入器只取 `content`
—— **选中的原文整个被丢掉**。修:问句与选中**分开抽词**(两者语言常常不同):
问句 `lang=[]`(用户话语)、选中的原文 `lang=None`(按书语言,它是书的内容);
`src_key` 加 hint 区分(否则同 ts 同 file 会撞)。

## 6. 分阶段路线(建议,未拍板)

- ~~**阶段 0**:事件表 + 现有渠道接入~~ ✅ 2026-07-17(改为零侵入导入器,见 §5b)
- ~~**阶段 1**:术语+画像+焦点层 → /insights 面板~~ ✅ 2026-07-17
- **阶段 2**:关联层(词面→向量→AI 三层漏斗)→ KG/技能树挂接、阅读器"相关材料"、
  "这周焦点出一张卷"(这里自然接回学习闭环)。
- **阶段 3(2027,眼镜到货后)**:gaze 事件作为新渠道插入事件层;按 §4 的坑清单实施
  (区域聚合/漂移校正/语言先验反推/软提示)。

## 7. 待讨论的设计决策

1. 渠道权重表(§5① 是草案)+ 半衰期(短 7d?14d?)。
2. 事件层存 SQLite 单表 vs 复用/扩展现有 vocab-exposure 机制。
3. 关联写回 KG 的形态:新边类型?还是只做阅读器侧栏推荐不动 KG?
4. 焦点面板放 /insights 还是独立页。
5. embedding 走本地 Model2Vec 还是 gemini-embedding API(质量 vs 零依赖)。
