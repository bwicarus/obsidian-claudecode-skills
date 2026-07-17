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

## 6. 分阶段路线(建议,未拍板)

- **阶段 0(零新硬件跑通全链路)**:事件表 + 现有渠道埋点(查词/高亮/QA/报告/阅读页)。
  多数渠道已在写日志,只是各写各的。
- **阶段 1**:术语+画像+焦点层 → /insights"注意力焦点"面板(第一个可见产出)。
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
