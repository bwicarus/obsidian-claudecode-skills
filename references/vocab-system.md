# 单词系统 (Vocab)

**目标**：PDF 阅读时查词 → 多源字典融合 → vault 笔记自动生成（带音频）→ Anki 卡（手动/自动） → 阅读时根据掌握度下划线高亮。

## 1. 设计原则

- **vault 当数据库**：每个词一个 `.md`，frontmatter 存状态，正文存格式化字典内容
- **不用 AI**：所有词条信息从权威字典直接来，不靠 AI 编释义
- **三源融合**：ECDICT（离线，中文 + 词频）+ Free Dictionary（在线，例句 + 音频备份）+ Merriam-Webster Learner's（在线，高质量学习者例句 + 美音音频）
- **派生词聚合**：用 ECDICT exchange 表把 `constructs/constructed/constructing` 归到 `construct` 同一 `.md`
- **掌握度多信号**：Anki state + 查词次数 + 暴露但未查 + 时间衰减
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
| B | mastery 算法：Anki review state + 查词次数 + 暴露但未查 + 衰减 | 待做 |
| C | PDF 阅读时未掌握词下划线（不同色按 mastery）| 待做 |
| D | 一键 Anki 卡（手动 + 阈值自动）；模板纯字典数据，不调 AI | 待做 |
| E | 词频阈值过滤（不标常用词）+ 跨 PDF 暴露计数 + 反向索引 | 待做 |

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

## 11. 已知踩坑

| 坑 | 现象 | 修复 |
|---|---|---|
| MW 返回不相关词 | 查 `construction` 给出 `under:1` | `_mw_unpack(raw, lemma=)` 用 `hwi.hw.startswith(lemma)` 过滤 |
| MW pos 不规范 | `preposition` / `noun` 全名 | 映射表：noun→n. verb→v. ... |
| ECDICT pos 是比例字符串 | `n:100` 表示 100% 名词 | `re.findall(r"([a-z]+):\d+")` 提取 |
| YAML 空值变 list | `anki_card_id:` 加载后变 `[]` | `_parse_simple_yaml` 空 value 默认 `""`，仅在下一行出现 `- xxx` 时转 list |
| 派生形 lemma 化覆盖 | `constructs`、`constructed` 查询都写同一 `construct.md` | 期望行为；lookup_count 累加；sources 按 (pdf, page) 去重 |
| Obsidian PDF 跳转页号 | `obsidian://open?file=X.pdf#page=N` 需要 PDF.js 插件支持 | 给两个链接：obsidian:// 跳本机 + bwicarus.space/pdf/view 跳 web reader |
