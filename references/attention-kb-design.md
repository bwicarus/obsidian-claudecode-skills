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
