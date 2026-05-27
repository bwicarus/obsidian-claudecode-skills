# 单词系统 (Vocab)

**目标**：PDF 阅读时查词 → 多源字典融合 → vault 笔记自动生成（带音频）→ Anki 卡（手动/自动） → 阅读时根据掌握度下划线高亮。

## 1. 设计原则

- **vault 当数据库**：每个词一个 `.md`，frontmatter 存状态，正文存格式化字典内容
- **不用 AI**：所有词条信息从权威字典直接来，不靠 AI 编释义
- **三源融合**：ECDICT（离线，中文 + 词频）+ Free Dictionary（在线，例句 + 音频备份）+ Merriam-Webster Learner's（在线，高质量学习者例句 + 美音音频）
- **派生词聚合**：用 ECDICT exchange 表把 `constructs/constructed/constructing` 归到 `construct` 同一 `.md`
- **掌握度模型（重新设计 2026-05-27）**：
  - **默认 1.0 = 已掌握**（未查过的词隐式满分，不进 vocab dir）
  - 用户主动查 W → W.mastery 重置 0.0（"完全不会"）
  - 段落扫描：查 W 触发本地扫描，前文段落内已查过的词 +0.03（"读过没查 = 多一点证据它会"）
  - Anki review 反馈：again -0.15 / hard -0.05 / good +0.05 / easy +0.15
  - 双向：mastery ≥ 0.95 → AnkiConnect suspend；< 0.85 → unsuspend
- **保留用户备注**：脚本只更新 `<!-- USER NOTES BELOW -->` 之上内容

## 2. 文件位置

```
vault/
└── 资源/
    └── vocab/
        ├── a/
        │   └── algebra.md
        ├── c/
        │   ├── construct.md         ← lemma 主条目（constructed/constructing 都映射来）
        │   └── construction.md
        └── _audio/
            ├── algebra-us.mp3
            └── construction-us.mp3

claude/
├── scripts/vocab/
│   ├── dict_sources.py              三源融合 + cache
│   └── build_vocab_note.py          生成 / 更新 .md
├── data/
│   └── ecdict.db                    ~850MB SQLite
└── state/
    ├── dict-cache/<source>-<sha>.json   在线 API 缓存（TTL 30 天）
    ├── vocab-lookups.jsonl              每次查词写一行
    └── vocab-sources.json               { lemma: [{pdf,page,ctx,ts}...] }
```

## 3. 配置（`state/server-config.json`）

```json
{
  "dict": {
    "mw_key": "688460f6-...",          // Merriam-Webster Learner's API key
    "free_dict_enabled": true,
    "cache_dir": "state/dict-cache",
    "cache_ttl_days": 30
  },
  "vocab": {
    "vault_subdir": "资源/vocab",
    "audio_subdir": "资源/vocab/_audio",
    "lookups_log": "state/vocab-lookups.jsonl",
    "auto_anki_threshold": 0,          // > 0 → 查 N 次自动制卡（0 = 关闭）
    "show_underlines": true             // PDF reader 是否显示生词下划线（阶段 C）
  }
}
```

**API key 注册**：[dictionaryapi.com/register](https://dictionaryapi.com/register/index)，选 "Learner's Dictionary"，free 1000 req/day。

## 4. compose_entry(word) 数据结构

```python
{
  "word": "constructed",             # 用户输入
  "lemma": "construct",              # 屈折归原型
  "forms": ["construct", "constructs", "constructed", "constructing"],
  "phonetics": {"us": "/...//", "uk": "/.../"},
  "audio":     {"us": "<MW or wiktionary mp3 URL>", "uk": "..."},
  "pos": ["v.", "n."],
  "freq": {"bnc": 1442, "coc": 1415},
  "definitions": [
    {"pos": "v.", "zh": "建造", "source": "ecdict"},
    {"pos": "v.", "en": "to build or form by assembling parts.", "source": "wiktionary", "examples": ["..."]},
    {"pos": "n.", "en": "...", "source": "mw", "examples": ["...", "..."]},
  ],
  "examples": ["...", ...],          # 去重平铺
  "synonyms": [...], "antonyms": [...],
  "etymology": "...",                # Free Dict 给的
  "sources_hit": ["ecdict", "free_dict", "mw"],
}
```

**音标 / 音频优先级**：MW > Free Dict > ECDICT
**定义合并顺序**：ECDICT 中文 → MW (高质例句) → Wiktionary (Free Dict)

### MW 数据处理坑

`/api/v3/references/learners/json/<word>` 偶尔返回**完全不相关**的 related entries（实测查 `construction` 还返回了 `under:1`）。
**修复**：`_mw_unpack(raw, lemma=)` 用 `hwi.hw` 去 `*`/空格后 `.startswith(lemma)` 才采用。`construction paper` 这种合理派生保留。

MW 富文本标记 `{bc}` / `{it}` / `{wi}` / `{phrase}` 等用 `_mw_strip` 剥掉。

### MW 音频 URL 拼接

`hwi.prs[].sound.audio` 是文件名，URL 子目录规则：
- `bix*` → `bix/`
- `gg*` → `gg/`
- 数字/特殊符号开头 → `number/`
- 其他 → 首字母

完整：`https://media.merriam-webster.com/audio/prons/en/us/mp3/<sub>/<file>.mp3`

### ECDICT exchange 解析

`d:bettered/i:bettering/s:betters/p:bettered/3:betters/0:good/1:r`：

| key | 含义 |
|---|---|
| `0` | lemma（原型） |
| `d` | past tense |
| `i` | -ing 形 |
| `s` / `3` | 第三人称单 / 复数 |
| `p` | past participle |
| `r` | 比较级 |
| `t` | 最高级 |
| `1` | type indicator（如 `1:dp`、`1:r`）|

`lookup_ecdict(word)`：
1. 先 `word=?` 直接命中
2. 没命中 → `exchange LIKE '%/{word}%'` 反查 lemma
3. lemma ≠ word → 再用 lemma 查完整行

## 5. vocab `.md` 模板

```markdown
---
word: construction
lemma: construction
forms:
  - construction
  - constructions
phonetic_us: /kənˈstɹʌkʃən/
phonetic_uk:
pos:
  - n.
freq_bnc: 1442
freq_coc: 1415
audio_us: 资源/vocab/_audio/construction-us.mp3
audio_uk:
first_seen: 2026-05-27
last_lookup: 2026-05-27
lookup_count: 1
exposure_count: 0
mastery: 0.0
mastery_label: 新词
anki_card_id:
sources_hit:
  - ecdict
  - free_dict
  - mw
tags:
  - vocab
  - vocab/new
---

# construction

🔊 ![[construction-us.mp3]] · *US /kənˈstɹʌkʃən/* · BNC #1442

**词形**：construction / constructions

## 📖 朗道双语 / 21世纪大英汉
- **n.** 建筑, 构造, 建筑物

## 📚 Merriam-Webster Learner's
- **n.** the act or process of building something
  > Construction of the new bridge will begin in the spring.

## 🌐 Wiktionary
- **noun** Something that has been constructed.

## 💬 更多例句
> ...

**近义词**：building
**反义词**：destruction

## 📝 文中出现
### `000-LADR4eChinese.pdf` · p.18
> Let V be a real vector space. The construction of V ⊕ W is...
- [在 PDF 阅读器打开 →](https://bwicarus.space/pdf/view?file=资源/books/000-LADR/000-LADR4eChinese.pdf&page=18)
- [在 Obsidian 打开 →](obsidian://open?vault=obsidian&file=资源/books/000-LADR/000-LADR4eChinese.pdf#page=18)
- 时间：2026-05-27 12:57

## 💭 个人备注
（在 `<!-- USER NOTES BELOW -->` 之下写，不会被脚本覆盖）

<!-- USER NOTES BELOW -->
```

**重要约定**：脚本**只重写** `<!-- USER NOTES BELOW -->` 之上的内容；之下原样保留。`_load_existing(path)` 拆 frontmatter + user_notes 两段，渲染时合并。

`sources` 不在 frontmatter（避免 YAML 列表嵌套）；存独立 `state/vocab-sources.json`，按 lemma 分组，去重键 = `(pdf, note, page)`。

## 6. 后端 `/pdf/api/dict` 改造

入参（GET）：
- `word` — 必填
- `file` — PDF 相对路径（写 lookup 日志 + 笔记 "文中出现"）
- `page` — 页号
- `context` — 选中所在句（用 `_expandSentenceFromRange` 前端算）

副作用：
1. 同步追加 `state/vocab-lookups.jsonl`
2. 后台线程 `_trigger_vocab_note_async` → `update_word_note(word, add_source={pdf, page, context})`
3. 音频异步下载到 vault `_audio/`

返回 JSON（前端用）：
```json
{
  "ok": true,
  "word": "construct", "lemma": "construct",
  "forms": ["construct", "constructed", "constructing", "constructs"],
  "phonetic_us": "/kənˈstɹʌkt/", "phonetic_uk": "",
  "audio_us": "https://media.merriam-webster.com/.../constr14.mp3",
  "translation": "v. 建造\nv. 构筑",
  "definition":  "v. to build or form by assembling parts.\n...",
  "examples": ["...", "..."],
  "synonyms": [...], "antonyms": [...],
  "freq_bnc": 1442,
  "sources_hit": ["ecdict", "free_dict", "mw"],
  "vocab_note": "资源/vocab/c/construct.md"
}
```

## 7. PDF reader 字典 modal

升级版（替换原 ECDICT-only 渲染）：
```
US /kənˈstɹʌkʃən/ · BNC #1442 · [🔊]
原型：construct（construct/constructs/constructed/constructing）

n. 建筑, 构造, 建筑物
EN: a branch of mathematics ...
例句：
  • Construction of the new bridge ...
  • ...
同 building · 反 destruction
源：ecdict + free_dict + mw · [在 Obsidian 打开词条 →]
```

底部"在 Obsidian 打开词条"链接是 `obsidian://open?vault=obsidian&file=资源/vocab/c/construct.md`，跳词条详情页。

## 8. CLI 用法

```bash
# 仅查词 + 三源融合（调试用）
python3 scripts/vocab/dict_sources.py construction

# 离线只 ECDICT
python3 scripts/vocab/dict_sources.py construction --offline

# 生成 / 更新 vault 笔记 + 加来源
python3 scripts/vocab/build_vocab_note.py construction \
  --add-source-pdf '资源/books/000-LADR/X.pdf' \
  --page 18 \
  --context 'The construction of vector spaces...'

# lemma 自动归并（输入屈折形 → 写到 lemma 文件）
python3 scripts/vocab/build_vocab_note.py constructed
# → 写到 c/construct.md（lookup_count++）
```

## 9. 阶段进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **A** | 字典三源 + cache + vault 笔记生成 + audio 下载 + PDF reader 查词集成 | ✅ 完成（2026-05-27）|
| **B** | mastery 算法：Anki state + 查词次数 + 暴露但未查 + 衰减 + 用户手动标 | ✅ 完成（2026-05-27）|
| **C** | PDF 阅读时未掌握词**下划线着色**（橙=新/黄=见过/淡绿=熟/掌握不画） | ✅ 完成（2026-05-27）|
| **D** | 一键 Anki 卡（Obsidian-cloze 模板，纯字典数据不调 AI）+ 字典 modal [🎴 加入 Anki] | ✅ 完成（2026-05-27）|
| **E** | 跨 PDF 暴露计数 + 反向索引（vocab.md 加"📍 全文出现"section）| ✅ 完成（2026-05-27）|
| 例句中英 | MyMemory 翻译（free 无 key）+ 缓存；DeepL 可选升级 | ✅ 完成（2026-05-27）|

### 阶段 D：一键 Anki 卡

**入口**：
- PDF reader 字典 modal 底部「🎴 加入 Anki」按钮 → POST `/pdf/api/vocab-anki` body `{word}`
- CLI: `python3 scripts/vocab/anki_from_word.py construction [--force]`

**模板**：`Obsidian-cloze`（字段 Text + Extra）；deck `Vocab`（不存在自动建）

**字段映射**：
- `Text` (cloze 字段)：上下文句子，词形替换为 `{{c1::word}}`
  - 优先 PDF source（用户实际选中的句子）
  - 其次 MW/Wiktionary 含 lemma 的例句
- `Extra` (富文本聚合)：
  - `<h2>lemma</h2>` + 音标 + `[sound:vocab-<lemma>-us.mp3]`
  - 中文释义（朗道）+ 英文释义（MW 学习者）
  - 选中句的中文翻译
  - 更多例句（含中文翻译；用 `<details>` 折叠）
  - 词形 / 同义反义 / BNC 词频
  - 来源 PDF 链接

**音频**：通过 `storeMediaFile` 推 Anki 媒体库 `vocab-<lemma>-us.mp3`
**tag**：`vocab vocab/lemma::<lemma>`
**重复检测**：`duplicateScope: deck` + tag 反查；已存在则 `updateNoteFields`

**bug 修复历史**：
- 试 `Saladict Word` 模板报 "cannot create note because it is empty"（即使所有字段非空）。原因未深查；改用 `Obsidian-cloze`（Text 含 `{{c1::}}` 即满足 cloze 检测）

### 查询冷却期（2026-05-27 加）

**问题**：用户刚查过一个词，后续段落里又见到该词时被 paragraph_exposure 加 +0.03 → mastery 立刻反弹回升，违反"刚查 = 还没掌握"的直觉。

**修复**：`server-config.json` 新字段 `vocab.lookup_cooldown_hours` 默认 24。
- frontmatter 加 `last_lookup_ts: <unix>` 字段（int）
- `_bump_mastery(lemma, delta)` 内先调 `_in_cooldown(fm_text)`
- 冷却期内 + `delta > 0` → 直接 return `(old, old)` 不写盘
- `delta < 0`（Anki again/hard / 再次查询）总是允许
- cooldown_hours = 0 关闭机制

冷却检查优先 `last_lookup_ts`（unix int 秒级），fallback `last_lookup`（YYYY-MM-DD 日级）。

### 新 mastery 模型（2026-05-27 重新设计，覆盖原阶段 B）

**核心语义**：默认满分，事件驱动下降。

| 标签 | 阈值 | slug |
|---|---|---|
| 完全不会 | mastery < 0.10 | new |
| 学习中 | < 0.40 | learning |
| 见过 | < 0.70 | seen |
| 熟 | < 0.90 | known |
| 掌握 | ≥ 0.90 | mastered |

**事件类型**：

1. **查询事件**（用户在 PDF reader 选词查字典）：
   - W 创建/更新 vocab.md → mastery 强制 0.0
   - `paragraph_exposure.process_lookup(pdf, page, lemma)`：扫该页 chars 找 W 第一次出现位置 → 算所在段落（`_paragraph_bounds` 2.2× 行高判段）→ 找当前句子起点（向左到 `.!?`）→ 段落起 ~ 当前句起之间的所有词 → 命中 vocab_index 的 +0.03 mastery
   - 同段落同 lemma 只加一次（去重）

2. **Anki review 事件**（cron / 手动）：
   - `anki_sync.sync_from_anki(days=7)` 拉 `getReviewsOfCards` 近 N 天
   - 按 ease 累加 delta：`EASE_TO_DELTA = {1: -0.15, 2: -0.05, 3: 0.05, 4: 0.15}`
   - 写回 mastery，clamp 0~1

3. **mastery → Anki 反向**：
   - `anki_sync.sync_to_anki()`：
     - mastery ≥ 0.95 → `suspend` Anki 卡（停止复习负担）
     - mastery < 0.85 + 处于 suspended → `unsuspend`
     - 0.85~0.95 维持现状（防抖）

**触发链路**：
- PDF reader `/api/dict` 同步追加 lookup-log + 异步 `_trigger_vocab_note_async`（先生成 md）+ 异步 `_trigger_paragraph_exposure_async`（sleep 1.5s 等 md 写完再扫，避免新建词没在 vocab_index）
- daily（可选接入）：`anki_sync.py --days 7` 把过去一周 Anki review 反馈到 mastery，再反向 suspend

**`_bump_mastery(lemma, delta)`** 核心写回函数：
1. 用 `vocab_index` 找 `.md` 路径
2. 解析 frontmatter `mastery` 旧值
3. clamp `old + delta` 到 [0, 1]
4. 写回 + 同步 `mastery_label` + 加 `last_exposure: YYYY-MM-DD`

### 旧 mastery 算法（已废弃但代码保留 `compute_mastery.py`）

`scripts/vocab/compute_mastery.py`：扫所有 vocab/*.md 重算 mastery 写回 frontmatter。

```python
score = 0.50  # base

# 1. Anki 卡（深度集成 AnkiConnect cardsInfo）
if 该词在 Anki 有 card:
    if queue=review and avg_ease ≥ 2.5: score += 0.30
    elif queue=review:                   score += 0.15
    if 全新未学:                          score -= 0.05
    score -= min(0.20, lapses * 0.04)
else:
    score -= 0.05   # 没 Anki 卡

# 2. 查询次数（近 30 天，from vocab-lookups.jsonl）
if ≥5: score -= 0.25
elif ≥3: score -= 0.15
elif ≥2: score -= 0.05

# 3. 暴露但未查（核心信号；exposure - 查询次数）
exposed_without_lookup = exposure - lookups_recent
score += min(0.40, exposed_without_lookup * 0.05)

# 4. 时间衰减
if days_since_last_lookup > 90: score += 0.10
elif > 30:                       score += 0.05

# 5. 用户手动标 frontmatter.user_mark
if user_mark == "known":   score += 0.50
if user_mark == "unknown": score -= 0.50
```

阈值 → label：
- < 0.25 → 新词 (new)
- < 0.55 → 见过 (seen)
- < 0.85 → 熟 (known)
- ≥ 0.85 → 掌握 (mastered)

**Anki 数据拉取**：`load_anki_vocab_cards()`：
1. `findNotes deck:Vocab` → note ids
2. `notesInfo` → 每个 note 的 cards 列表
3. `cardsInfo` → 每个 card 的 queue/factor/reps/lapses
4. 按 note id 聚合

### 阶段 C：PDF 下划线高亮 + 单击直翻

**单击未掌握词 → 直接弹翻译**（2026-05-27 加）：
- 设置面板：`[点击未掌握单词直接显示翻译]` 复选框（默认开）
- localStorage `pdf-click-translate-unmastered`
- 实现：char-layer onEnd 单击分支内，`_clickCount === 1` 命中 vocab mark 且 `label_slug != mastered` → `setTimeout(onTranslate, 30)`（让 selByCharRange 先布工具栏，translate 关掉它弹字典）
- 三击/双击保持原行为（选行 / 选段）；选中范围跨多词不触发自动翻译

后端 `/api/page-chars` 新增 `vocab_marks: [{start,end,word,lemma,mastery,label_slug}]`：
1. 扫该页 chars 识别英文词边界（连续 `isalpha`/`'-`）
2. word lemma 化（不走 ECDICT！直接查 `vocab_index` —— index 已把所有 forms 都 cache）
3. 命中 vocab 且 `label_slug != mastered` → 标记

`scripts/vocab/vocab_index.py`：
- `index()` 返回 `{word_lower: {lemma, mastery, label_slug, lookup_count, freq_bnc, anki_card_id, path}}`
- 缓存 in-memory + vault mtime 检测（vocab 没改就用缓存）

前端 `renderVocabUnderlines(pw, marks)`：
- 给每个 word 在 `.vocab-layer` 画 `<div class="vocab-underline m-<slug>">`
- 跨行词自动分段画
- 设置面板「生词下划线」复选框（localStorage `pdf-vocab-underline`，默认开）

颜色：
- `.m-new` 橙色 #f59e0b 2.5px
- `.m-seen` 黄色 #facc15 2px
- `.m-known` 淡绿 #a3e635 1.5px opacity 0.65
- `.m-mastered` `display:none`

z-index：vocab-layer 在 char-layer 之下（z 2 vs 4），不影响选中事件。

### 阶段 E：暴露计数 + 反向索引

`scripts/vocab/build_exposure.py` 扫 vault 所有 PDF：

```python
form_to_lemma = {form: lemma for form, info in vocab_index.items()}
for pdf in vault.rglob("*.pdf"):
    for page in pdf:
        for word in page.get_text("words"):
            token = clean(word)
            if token in form_to_lemma:
                counts[form_to_lemma[token]][page] += 1
```

**性能优化**：不做 ECDICT lemma 化（每 token 1 次 sqlite 慢死），直接查 form_to_lemma 字典（vocab_index 已把 forms 全 cache）。实测 20 PDF / 6000 页 / 41s。

输出 `state/vocab-exposure.json`：
```json
{
  "algebra": {
    "total": 547,
    "pages": [
      {"pdf": "资源/books/Strang.../...pdf", "page": 1, "count": 3},
      {"pdf": "资源/books/Feynman.../...pdf", "page": 24, "count": 5},
      ...
    ]
  }
}
```

**反向索引**：`build_vocab_note.py` 渲染时拉 exposure 数据，加 `## 📍 全文出现 (N 处)` section：
- 按 PDF 分组列出页号
- 每个页号生成 PDF reader 链接
- 默认显示前 15 页 + "…+N" 提示

## 10. CLI 总览

```bash
# 字典查询（调试）
python3 scripts/vocab/dict_sources.py construction

# 生成/更新 vault 笔记
python3 scripts/vocab/build_vocab_note.py construction \
  --add-source-pdf 资源/books/.../X.pdf --page 18 --context '...'

# 翻译测试
python3 scripts/vocab/translate.py "Sentence to translate."

# 加 Anki 卡
python3 scripts/vocab/anki_from_word.py construction [--force]

# 重算 mastery（全量 / 单词）
python3 scripts/vocab/compute_mastery.py [--word construction] [--verbose] [--dry-run]

# 扫 vault 算 exposure
python3 scripts/vocab/build_exposure.py [--pdf 资源/books/X.pdf]

# 索引调试
python3 scripts/vocab/vocab_index.py construction algebra
```

## 11. daily 流程接入（可选）

未自动接入 `bwicarus-daily.timer`。建议每周跑一次：

```bash
# scripts/daily_anki_status.py 可加：
step("vocab-exposure", "python3 scripts/vocab/build_exposure.py")
step("vocab-mastery",  "python3 scripts/vocab/compute_mastery.py")
```

build_exposure ~40s（增量更新已扫过的 PDF），compute_mastery ~秒级。

## 12. 阶段 B-E 已知踩坑

| 坑 | 现象 | 修复 |
|---|---|---|
| Anki Saladict Word 模板拒绝 | "cannot create note because it is empty" 即使所有字段有内容 | 改用 `Obsidian-cloze` 模板（Text + Extra 两字段，cloze 类型）|
| AnkiConnect tag 含 `::` 不能直接 findNotes | `tag:vocab/lemma::xxx` 查不到 | 用 `findCards deck:Vocab` 然后 cardsInfo 拿 noteId，按需 |
| ECDICT lemma 化太慢 | 扫 vault 暴露时每 token 1 次 sqlite | `vocab_index` 已把 forms 全 cache，build_exposure 直接查字典命中 |
| YAML mastery 浮点漂移 | 0.45 写回后再读变 0.45000001 | `round(mastery, 3)` + 比较时 `abs() > 0.01` 阈值 |
| Anki 没卡也算分 | anki_no_card 扣分 -0.05 没写进 debug log | 仅 log 缺失；最终 score 正确 |
| YAML 空值变 list | `anki_card_id:` 加载后变 `[]` 而非 `""` | `_parse_simple_yaml` 空值默认 `""` |

## 13. 已知踩坑（原阶段 A）

## 10. 阶段 B 设计（mastery 算法草稿）

`scripts/vocab/compute_mastery.py`（待写）daily 04:00 跑，扫所有 vocab/*.md → 算新 mastery → 写回 frontmatter。

```python
def mastery(word, fm, anki_data, lookup_log, exposure):
    score = 0.0
    # 1. Anki 信号（权重最高）
    card = anki_data.get(fm['anki_card_id'])
    if card:
        if card['queue'] == 2 and card['ease'] >= 2.5: score += 0.35
        if card['queue'] == 0: score -= 0.10      # 新卡未学
        score -= min(0.2, card['lapses'] * 0.05)
    # 2. 查询次数（近 30 天）
    n_recent = sum(1 for L in lookup_log if L['lemma']==fm['lemma'] and within_30d(L['ts']))
    if n_recent >= 5: score -= 0.3
    elif n_recent >= 2: score -= 0.1
    # 3. 暴露但未查（核心信号）
    exposed_without_lookup = exposure[fm['lemma']]['count'] - n_recent
    score += min(0.4, exposed_without_lookup * 0.05)
    # 4. 时间衰减
    days = (today - last_lookup_date(fm)).days
    if days > 90: score += 0.10
    elif days > 30: score += 0.05
    # 用户标记
    if fm.get('user_mark') == 'known':   score += 0.5
    if fm.get('user_mark') == 'unknown': score -= 0.5
    return max(0.0, min(1.0, score + 0.5))   # bias 0.5

LABELS = [(0.2, "新词", "new"), (0.5, "见过", "seen"),
          (0.8, "熟", "known"), (1.01, "掌握", "mastered")]
```

**暴露计数**：每页 PDF 扫一遍所有英文 token，token 经 ECDICT lemma 化（exchange 表反查）后归类。这个扫描结果缓存到 `state/vocab-exposure.json`，文件级粒度：`{pdf: {page: {lemma: count}}}`。每次 PDF 渲染（或 daily）刷新。

## 13. 已知踩坑（原阶段 A）

| 坑 | 现象 | 修复 |
|---|---|---|
| MW 返回不相关词 | 查 `construction` 给出 `under:1` | `_mw_unpack(raw, lemma=)` 用 `hwi.hw.startswith(lemma)` 过滤 |
| MW pos 不规范 | `preposition` / `noun` 全名 | 映射表：noun→n. verb→v. ... |
| ECDICT pos 是比例字符串 | `n:100` 表示 100% 名词 | `re.findall(r"([a-z]+):\d+")` 提取 |
| YAML 空值变 list | `anki_card_id:` 加载后变 `[]` | `_parse_simple_yaml` 空 value 默认 `""`，仅在下一行出现 `- xxx` 时转 list |
| 派生形 lemma 化覆盖 | `constructs`、`constructed` 查询都写同一 `construct.md` | 期望行为；lookup_count 累加；sources 按 (pdf, page) 去重 |
| Obsidian PDF 跳转页号 | `obsidian://open?file=X.pdf#page=N` 需要 PDF.js 插件支持 | 给两个链接：obsidian:// 跳本机 + bwicarus.space/pdf/view 跳 web reader |
