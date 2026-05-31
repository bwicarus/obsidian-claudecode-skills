# 健身系统 (fitness)

多用户 web 训练追踪 + 循证 AI 教练 + YouTube 教学视频集成 + 高质量字幕。

部署:bwicarus.space VPS + Pi 共享一套代码,**per-user 数据**(SQLite + WEBAPP_DATA)。

> 入口:`https://bwicarus.taile44d0c.ts.net/private/fitness/`(需登录)

---

## 架构层次

```
浏览器(iPad / 桌面)
   │
   ▼  nginx HTTPS(:443) → 127.0.0.1:5000
Flask webapp (/home/bwicarus/webapp/app.py)
   │  fitness blueprint(/private/fitness/* + /api/fitness/*)
   ▼
fitness.py   ──  SQLite (per-user)
fitness_coach.py  ──  ai_client → Claude CLI Opus/Sonnet/Haiku
youtube_subtitles.py
youtube_speech.py ── Cloud STT  ──  GCP API key
                                   (走 GCP billing 烧赠金)
                  ── Gemini Flash ── AI Studio billing(独立)
                  ── youtube-transcript-api(免费)
```

---

## URL 清单

| 路径 | 用途 |
|---|---|
| `/private/fitness/` | 主页(选今日训练日 / 看最近 30 天)|
| `/private/fitness/log/<day_id>` | 训练日 log 页(`push` / `pull` / `legs`)|
| `/private/fitness/plan` | 完整 PPL 计划(只读 + mini carousel)|
| `/private/fitness/history` | 历史记录 + 单动作筛选 |
| `/api/fitness/*` | JSON API,见下 |

---

## SQLite 表结构(在 `WEBAPP_DATA/users/<u>/private/fitness.db`)

| 表 | 用途 | 关键列 |
|---|---|---|
| `fitness_log` | 每组训练记录(autosave 落地)| `(date, exercise_id, set_no)` UNIQUE,weight_kg / reps / day_id |
| `fitness_video_override` | 用户自定义视频列表(覆盖 plan.json)| exercise_id PK,videos_json |
| `fitness_exercise_override` | AI 调整后的 prescribed 覆盖 | exercise_id PK,prescribed_json,source(ai/manual),reasoning,change_summary |
| `fitness_session_analysis` | 每次完成训练 AI 分析 | (date, day_id) PK,analysis_json |
| `fitness_settings` | 用户级设置(AI 模型 / 自动开关) | key/value pairs |

### fitness_settings 默认值

```python
{
    "ai_model":                   "opus",   # opus | sonnet | haiku
    "ai_effort":                  "max",    # low|medium|high|xhigh|max
    "auto_analyze_after_finish":  "1",
    "auto_suggest_after_analyze": "1",
    "deload_check_weeks":         "6",
}
```

---

## Plan JSON schema(`_server_deploy/static/fitness-plan.json`)

PPL 3 天循环 + 20 个动作(2026-05-30 升级 v2)。每动作:

```json
{
  "id": "db_bench_flat",
  "name": "平板哑铃卧推",
  "sets": "4 × 6-10",                  // 显示用,从 prescribed 渲染
  "start_weight_hint": "10 kg 起",     // 显示用
  "prescribed": {                      // 结构化(API 用)
    "sets": 4,
    "rep_range": [6, 10],
    "rir_target": 2,
    "rest_seconds": 180,
    "start_weight_kg": 10,
    "weight_step_kg": 1.25
  },
  "evidence_note": "复合推,周容量 ~12-15 sets/胸足够 (Schoenfeld 2024 meta)...",
  "search_q": "dumbbell bench press technique",
  "tips": ["..."],
  "videos": [
    {"video_id": "hWbUlkb5Ms4", "title": "...", "channel": "Jeff Nippard"}
  ]
}
```

**20 个动作分布**:Push 7 / Pull 7 / Legs 6。Pull 含 chin_up(自重起),Push 含 dip(自重起),Legs 含 hanging_leg_raise。

**循证设计要点**(顶层 description / progression_rule):
- 拉伸位优先(Maeo 2023, Sato 2024)
- 周容量 12-18 sets/肌(Baz-Valle 2022)
- 复合 RIR 1-2,孤立 RIR 0-1(Refalo 2023)
- rep 范围 5-30 内增肌等效(Schoenfeld 2021)
- Double Progression:全 rep_range 上限 → 加 step;<下限 → 减 10%;中间 → +1 rep

---

## API 清单

### 训练数据

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/fitness/plan` | 返回 plan.json |
| POST | `/api/fitness/log` | upsert 一组(autosave / ✓ 都走这个)|
| GET | `/api/fitness/today_sets/<ex>?date=` | 拉某日某动作所有组(刷新恢复)|
| GET | `/api/fitness/workout_meta?date=&day_id=` | 训练 MIN(created_at) + count(已用时间起点)|
| GET | `/api/fitness/recommend/<ex>` | Double Progression 推荐(weight/reps/sets/rir/rest + reason)|
| GET | `/api/fitness/last/<ex>` | 该动作最近一次完整记录 |
| GET | `/api/fitness/history?exercise_id=&days=` | 全部历史(图表用)|
| DELETE | `/api/fitness/log/<id>` | 删一组 |

### 视频(per-user override)

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/fitness/videos/<ex>` | 合并 override + plan 默认 |
| POST | `/api/fitness/videos/<ex>` | 完整替换覆盖 |
| POST | `/api/fitness/videos/<ex>/reset` | 删 override 回默认 |

### 字幕(全局共享,因为是公开内容)

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/fitness/subtitles/<vid>?source=auto\|stt&force=` | 拉 + 翻 + 缓存 |
| GET | `/api/fitness/subtitles/<vid>/status?source=` | 只查 cache |

**source**:
- `auto`(默认):YT 自带 caption + Gemini Flash 翻译(~5s 首次,秒出缓存)
- `stt`:Cloud Speech-to-Text 重新转录 + 翻译(~30-60s,烧赠金,质量高)

### AI 教练

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/fitness/ai/suggest_plan` body `{day_id}` | Claude 看历史 + 上次 analysis → 调整 prescribed 建议(per-exercise) |
| POST | `/api/fitness/ai/analyze_session` body `{date, day_id}` | Claude 分析一次完成的训练,落库 fitness_session_analysis |
| GET/POST/DELETE | `/api/fitness/exercise_override/<ex>` | 用户接受 AI 建议的落地 |
| GET | `/api/fitness/exercise_overrides` | 列所有 override(看哪些已调整)|

### 设置

| 方法 | 路径 |
|---|---|
| GET | `/api/fitness/settings` |
| POST | `/api/fitness/settings` body `{key1: value1, ...}`(白名单)|

---

## AI 教练 (fitness_coach.py)

### 模型/Effort

走 `ai_client.ask(prompt, claude_model="opus", claude_effort="max")`,默认从 `fitness_settings` 读。

- **Opus + max**:深度思考,3-5 分钟,~$0.3 / 次
- 用户明说不在乎 Claude 限额,所以默认最强

### Prompt 文献库(`LITERATURE_REF`)

25+ 篇循证训练 meta-analyses + RCT,分 7 类:

| 类 | 关键 |
|---|---|
| 训练量/频率/容量 | Schoenfeld 17/19/24, Baz-Valle 22 (MAV 12-20), Heaselgrave 19, Aube 20, Mangine 15, Wernbom 07 |
| 力竭/RIR/RPE | Refalo 23(复合 1-2/孤立近力竭),Pelland 24(0-5 等效),Helms 24, Latella 19, Hackett 18, Krzysztofik 19, Carroll 17 |
| 拉伸位/ROM | Maeo 23(长头 +40%),Sato 24, Pedrosa 22, Pinto 12, Bloomquist 13(深蹲深位 +25%),Vargas 21, Kubo 19, Tsaklis 15, Andersen 14 |
| EMG/角度 | Lockie 17(上斜 15-30°),Rodríguez-Ridao 20, Coratella 20(cable lateral),Reinold 09, Youdas 10(反握引体 +20% 二头),Doma 13, Marcolin 18 |
| 休息 | Schoenfeld 16(3min > 1min)|
| 周期化 | Helms 18(deload 6-8w),Israetel MEV/MV/MAV/MRV, Helms 19, Pearson 21 |
| 营养/恢复 | Phillips 14(1.6-2.2g/kg),Burd 10(leucine),Walberg 88(睡眠),Murach 18, Mannarino 24 |

### 两个核心函数

```python
suggest_plan(db, day_id, plan_data, model=, effort=) → {
    overall_reasoning: "...",
    exercises: [
        {id, prescribed, change_summary: "+1.25kg" | "维持" | "-5% deload",
         reasoning: "上次 4 组全 ≥8 reps,Double Progression 加重"}
    ]
}

analyze_session(db, date, day_id, plan_data, model=, effort=) → {
    completion_rate_pct: 95,
    rpe_estimate: 7.5,
    verdict: "progress|stagnation|overreach|fatigue|deload_due",
    summary: "...",
    per_exercise: [{id, verdict, note, next_action, rationale_brief}],
    key_insights: ["..."],   # 必引用研究或趋势数据
    warnings: ["..."]
}
```

`next_action` enum:`+1.25kg / +2.5kg / +5kg / +1 rep / hold / -5% deload / -10% deload / swap_to:<id> / rest_more / add_set / drop_set`

### 流程

```
点 🤖 调整计划 (log 页顶部 banner)
  ↓  ~3-5 min Opus + max
modal: overall_reasoning + per-exercise diff
  ↓
[✓接受] → POST exercise_override → loadRec 刷新推荐
[✗拒绝] → DELETE override (回 plan.json 默认)

点 🏁 完成训练
  ↓ 显示总结 modal
  ↓ (if auto_analyze_after_finish):
🤖 AI 分析 (analyze_session) → 显示 verdict / insights / warnings
  ↓ (if auto_suggest_after_analyze):
🤖 自动调用 suggest_plan → "下次 push 计划已就绪"
```

---

## 视频系统

### 双频道搜索

`scripts/find_jeff_videos.py`(YouTube Data API v3):

| 频道 | channelId | 风格 |
|---|---|---|
| Jeff Nippard | `UC68TLK0mAEzUyHx5x5k-S1Q` | 循证增肌 |
| Jeff Cavaliere (ATHLEAN-X) | `UCe0TLA0EsQbE-MjuHXevj2A` | 动作机制 / 防伤 |

**关键词过滤**(`MUST_CONTAIN` dict):每动作有 must_contain 词表,搜回来的标题不含 → 排后面。例:
- `seated_row` → `["row"]`(剔除掉 lat pulldown 等误匹配)
- `db_calf_raise` → `["calf", "calve"]`(calves 含 calve 不含 calf)
- `hanging_leg_raise` → `["leg raise", "hanging", "knee raise"]`

每动作 over-search(per × 3 = 15)→ primary(命中)排前 + fallback 补足。

### 本地重排序

`scripts/reorder_videos_by_keyword.py`(**不调 API**,纯本地)。改 MUST_CONTAIN 后跑这个免费调整。配额耗尽时只能跑这个。

### Carousel UI

log 页每动作展开:
```
[◀ 1/10 ▶ 视频标题  🇨🇳 🎯HQ ➕ 🗑 💾 🔄]
[iframe YT.Player API embed]
[字幕条 .v-subtitle (中文大 + 英文小)]
```

按钮:
- `◀ ▶`:切上下个
- `🇨🇳`:YT auto-caption + Gemini Flash(~5s 首次,秒出 cache)
- `🎯HQ`:Cloud Speech-to-Text 转录 + Gemini Flash(~30-60s 首次,烧赠金)
- `➕`:粘 YouTube URL 添加(oEmbed 自动取 title)
- `🗑`:从列表删
- `💾`:保存当前列表为我的 override
- `🔄`:重置回 plan.json 默认

---

## 字幕系统

### 后端

`_server_deploy/youtube_subtitles.py`:
- 拉源:`youtube-transcript-api`(免费 + Pi 出口 IP 未被封)或 `youtube_speech.py` STT
- 翻译:**优先 Gemini 2.5 Flash**(~3-5s + 250 req/天 免费),失败 fallback Claude(~20s)
- 缓存:全局共享 SQLite `WEBAPP_DATA/youtube_subtitles.db`,key (video_id, target_lang, source)
- 并发锁:同视频同 source 并发请求只翻 1 次

### Cloud Speech-to-Text 流程

`_server_deploy/youtube_speech.py`:
1. yt-dlp 拉 m4a(只要 audio,体积小)
2. ffmpeg 切 50s chunks + 转 FLAC 16kHz mono
3. **并发 4 worker** 调 STT `recognize` sync API(每片 <60s 限制)
4. word-level + chunk offset 合并
5. word → segments(max_sec=5,max_words=15,gap>0.5 切)

成本:`latest_long` enhanced $0.024/min。赠金 ¥47867 ≈ **跑 4700 小时视频**。

---

## 训练 log 页核心交互

### 顶部进度 banner

```
⏱ 预计 52 分钟   📦 28 组   💪 7 动作   ✓ 0/28 组 · 0:00
[████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
                              [⚙] [🤖 调整计划] [🏁 完成训练]
```

- 预计时间 = `Σ(sets × 30s + (sets-1) × rest_seconds + 60s 切换)`
- 已用时间 = 从 `MIN(created_at)` 起算(刷新后从后端拉,准)

### 每动作 card

```
平板哑铃卧推 · 4 × 6-10
RIR 2 · 间歇 180s · 起步 10 kg
📊 推荐 10 kg × 6 reps × 4 组  [为什么]
上次(2026-05-29): 10kg×8 / 10kg×7 / 10kg×6 / 10kg×5
📚 证据 ▾ (evidence_note 折叠)
                                       🎬 教学

组  重量kg   次数   ✓
1   [10____] [8___]  ✓ 已存
2   [_____] [_____]  ✓
3   ...
4   ...
5   ...

[休息倒计时 (saveSet 成功后 show)]
   休息中 · 推荐 3:00
   3:00 ←大字 monospace
   [████████████████████████]
   [−30s][+30s][跳过]
```

### Autosave

- 任何 input 改 → 500ms 防抖 → POST `/api/fitness/log`(upsert)
- 服务器 UNIQUE INDEX (date, exercise_id, set_no) 保证同组覆盖
- weight_kg / reps 都允许 null(只填一半也保住)
- 边框变 `--accent-2` 绿色 = 已存
- 两都填了 → ✓ 按钮变 "✓ 已存" + 绿底
- **刷新页面**:initEx → restoreToday → loadRec(空位置才 prefill 推荐)

### 休息倒计时

- ✓ 按钮成功后启动,读 `prescribed.rest_seconds` 或 REC.recommendation.rest_seconds
- 大字 36px monospace 显示 mm:ss
- 最后 5 秒每秒 660Hz 短"嘀"提示
- 归零双音 880+1320Hz + `navigator.vibrate([200,100,200])` + 卡片变绿
- `[−30s][+30s][跳过]` 控制
- Web Audio API + iOS suspended state 自动 resume

### 完成训练 modal

```
训练完成 🎉
2026-05-30 · PUSH
┌──────────────────┬──────────────────┐
│ ⏱ 用时           │ ✅ 完成          │
│ 32:18            │ 28 / 28          │
├──────────────────┼──────────────────┤
│ 💪 总训练量      │ 📦 总次数        │
│ 5,420 kg         │ 168              │
└──────────────────┴──────────────────┘

📊 各动作明细 ▾  (每动作的所有组 w×r 一行)

[🤖 让 AI 分析这次训练] (auto_analyze 时自动点)
   ↓ ~3-5 min
   verdict badge + summary + per_exercise + insights + warnings
   ↓ (auto_suggest):
   "下次 push 计划已就绪 → 进 log 页点 🤖 调整计划 查看"

[继续录入] [看历史] [返回主页]
```

总训练量:`Σ weight × reps`,自重组(w=0)不计入 volume 但计入次数。

---

## 部署 / 文件位置

| 项 | 主项目 (git) | webapp 部署 |
|---|---|---|
| Flask blueprint | `_server_deploy/fitness.py` | `/home/bwicarus/webapp/fitness.py` |
| AI 教练 | `_server_deploy/fitness_coach.py` | `/home/bwicarus/webapp/fitness_coach.py` |
| YT 字幕 | `_server_deploy/youtube_subtitles.py` | `/home/bwicarus/webapp/youtube_subtitles.py` |
| Cloud STT | `_server_deploy/youtube_speech.py` | `/home/bwicarus/webapp/youtube_speech.py` |
| Plan 数据 | `_server_deploy/static/fitness-plan.json` | `/home/bwicarus/webapp/static/fitness-plan.json` |
| 模板 | `_server_deploy/templates/fitness/*.html` | `/home/bwicarus/webapp/templates/fitness/` |
| 用户 db | — | `/home/bwicarus/webapp/data/users/<u>/private/fitness.db` |
| 字幕 cache | — | `/home/bwicarus/webapp/data/youtube_subtitles.db` |

### 部署命令(标准三步)

```bash
cp _server_deploy/{fitness,fitness_coach,youtube_subtitles,youtube_speech}.py /home/bwicarus/webapp/
cp _server_deploy/templates/fitness/*.html /home/bwicarus/webapp/templates/fitness/
cp _server_deploy/static/fitness-plan.json /home/bwicarus/webapp/static/
sudo systemctl restart webapp
```

### app.py 注册(已加)

```python
# 健身页(/private/fitness)
from fitness import register as register_fitness
register_fitness(app)
```

---

## 依赖

| 包 | 安装 | 用途 |
|---|---|---|
| `youtube-transcript-api` 1.2.4 | `pip --break-system-packages` | YT auto-caption |
| `yt-dlp` 2026.3.17+ | pip | 下载视频 audio |
| `ffmpeg` 7.x | apt | 切 chunk + FLAC 转码 |
| `requests` | 已有 | STT / Gemini REST |

---

## 已知问题 / 踩坑

1. **Gemini billing 模式陷阱**:AI Studio 默认 free tier 250 req/天 OK;但如果触发了 "prepayment" 模式,prepay 余额 0 时就报 `prepayment depleted`。解决:`https://ai.studio/projects` 关掉 billing。
2. **Gemini API 不走 GCP 赠金**:`generativelanguage.googleapis.com` 即使在 GCP project 启用,计费走 AI Studio 独立账户,**赠金不通**。要烧赠金做 Gemini 得走 Vertex AI(需 service account)。
3. **Opus + max effort 慢**:3-5 分钟。设置 modal 可调成 high(~45s)。nginx /api proxy_read_timeout=300s,Pi 上 max effort 偶尔会撞超时。
4. **plan.json 顺序敏感**:`upgrade_fitness_plan.py` 只更新现有动作,顺序保留;新增动作走 `add_pullup_exercises.py`(`_insert: prepend/after/replace`)。
5. **YouTube Data API 配额 10k/天硬上限**:不能用赠金扩。**配额耗尽就换本地 `reorder_videos_by_keyword.py` 离线调整,不调 API**。
6. **fitness_log schema migration**:加 UNIQUE INDEX 时存量数据可能有冲突(同 date+ex+set_no 多行),迁移脚本保留 max id 删旧。已对 bwicarus 用户跑过。

---

## 相关 reference

- [`google-cloud-apis.md`](google-cloud-apis.md) — Vision / YouTube / STT / Gemini 集成 + 赠金计费
- [`webapp-development.md`](webapp-development.md) — Flask + nginx 部署框架
- [`server-config-schema.md`](server-config-schema.md) — server 端配置(不含 fitness)
