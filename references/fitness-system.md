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
| `fitness_ai_job` | AI 后台 job(异步,关页面不丢) | id PK,kind(suggest_plan/analyze_session),day_id,date,status(running/done/error),result_json,error |

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
| GET | `/api/fitness/recommend/<ex>` | Double Progression 推荐(weight/reps/sets/rir/rest + reason)+ `pr`(历史最佳)|
| GET | `/api/fitness/pr/<ex>` | 该动作 PR(best_weight / best_1rm Epley / best_reps_at_top,排除今天)|
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
- `auto`(默认):YT 自带 caption + 翻译(~1s 首次,秒出缓存)
- `stt`:Cloud Speech-to-Text 重新转录 + 翻译(~30-60s,烧赠金,质量高)

> 翻译链路(2026-05-31 起):`youtube_subtitles._translate_all` = **Google Cloud Translation 批量优先**(`translate.gtranslate_batch`,v2 一次多段、走 GCP 赠金、EN→ZH 质量高、整集一两秒)→ Gemini Flash(赠金常 429,基本失效)→ Claude 兜底。之前主用 Gemini,因「prepayment depleted」长期退化到慢的 Claude;Google Translation API 放行后改它首选。详见 [`google-cloud-apis.md`](google-cloud-apis.md)。

### AI 教练

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/fitness/ai/suggest_plan` body `{day_id}` | **异步**:建 job + daemon 线程跑 → 立即返回 `{job_id, status:running}` |
| POST | `/api/fitness/ai/analyze_session` body `{date, day_id}` | **异步**:同上,done 后落库 fitness_session_analysis |
| GET | `/api/fitness/ai/job/<job_id>` | 轮询 job 状态/结果(running/done/error;>15min 判僵死)|
| GET | `/api/fitness/ai/jobs?day_id=&kind=&date=` | 列最近 job(刷新/换设备恢复用)|
| POST | `/api/fitness/ai/coach_chat` body `{date, day_id, message}` | **教练复盘对话**:写 user 消息 + 启 coach_chat job(异步,**不 dedup**)→ `{job_id}` |
| GET | `/api/fitness/ai/coach_chat/messages?date=&day_id=` | 取该次训练的全部对话消息(刷新/换设备恢复)|
| POST | `/api/fitness/ai/coach_chat/apply` body `{proposal}` | 把对话产出的 proposal 落成 exercise_override(下次该日计划生效,source=`ai_chat`)|
| POST | `/api/fitness/ai/balance_check` | **全身平衡体检**:启 balance_check job(异步,跨所有训练日聚合)→ `{job_id}` |
| GET/POST/DELETE | `/api/fitness/exercise_override/<ex>` | 用户接受 AI 建议的落地 |
| GET | `/api/fitness/exercise_overrides` | 列所有 override(看哪些已调整)|

**全身平衡体检(2026-06-04)**:主页「🩺 全身平衡体检」按钮 → `balance_check` job(异步)→ 主页内渲染。
跨 Push/Pull/Legs 聚合**拮抗肌群/前后链**容量,算六大平衡比 + AI 解读失衡 + 纠正建议:
- `fitness_coach.MUSCLE_MAP`(exercise_id → 容量桶摊派权重):复合动作按权重摊给协同肌(卧推→前束0.5/三头0.3;
  划船→后束0.3-0.4;引体→二头0.4;face_pull→后束1.0),**否则后束/三头会被系统性算成 0 → 假失衡**。换动作要在此补一行。
- `_balance_profile()`:近 28 天跨日聚合到桶 → 周均组数 + 每动作 Epley est-1RM。六大比:拉:推 / 股四:腘绳 /
  后束:前束 / 垂直:水平拉 / 二头:三头 / 后链:前链。每比按循证区间算 green/yellow/red;**任一侧周均<3组 → insufficient
  (只展示不判定,防 0 组比值爆炸/误报)**。容量比阈值≠等速力量比,不混用。
- `balance_check()`:profile(客观数字)→ `BALANCE_PROMPT`(opus+max + LITERATURE_REF)→ AI 出
  `{overall_balance_grade, summary, imbalances[], corrective_actions[], do_not_overcorrect}`。
- 前端主页自包含轮询(非 log.html 的药丸框架):六比彩色圆点 + 失衡卡 + 纠正建议;关页面回来自动续接 running job。
- **3D 肌群图(2026-06-04,用户选真 3D 而非 2D)**:`/private/fitness/body`(`body.html`)用 **Three.js(自托管 `/var/www/html/static/three/three.module.min.js` + `OrbitControls.js`,r0.160;OrbitControls 内 `import 'three'` 已 sed 改相对路径)**程序化建半透明人体(头/躯干/四肢 capsule 外壳)+ 12 块独立肌群网格(胸/前中后束/二头三头/背/核心/臀/股四/腘绳/小腿,左右镜像),按强弱**上色**(green/yellow/red/gray)。OrbitControls 旋转+缩放、正/背面按钮、raycast 点肌肉看详情、图例。颜色数据走 `_muscle_status(profile)`(把六比+桶翻成每肌强弱:比值弱侧取比值 status,无拮抗肌按绝对周容量判)→ 同步快端点 `GET /api/fitness/balance_profile`(0.02s,无 AI,秒上色)。**没用真实解剖 GLB**(免费且分肌群可选中的难找+体积大+远端慢网吃力)→ 程序化几何体,轻、离线、零外部素材。换动作要同步 `MUSCLE_MAP` 才纳入聚合。
  - ⚠ Three.js 是 ES module:页面用 `<script type="module">` import 自托管路径;nginx `/static/three/*.js` 经 `location /` try_files 直供(mime application/javascript 正常)。

**教练复盘对话(2026-06-04)**:完成训练 modal 里「💬 跟教练聊聊这次训练」(`<details>` 折叠) →
补充实情(感受/酸痛/时间/器械)→ `coach_chat()` 多轮理解(信息不足先追问,够了出 proposal)→
气泡下「✓ 应用到下次计划」一键写 override。底层仍是**后台 job + 4s 轮询**(非 SSE,因 ai_client 是阻塞
subprocess;iPad 断连由 job+缓存兜住,刷新 `coachLoad` 读 messages + 续接 running job)。「📋 聊够了
直接出计划」按钮发强制指令让 AI 立即 ready=true 出 proposal(opus 偏谨慎,prompt 已加"尽快收敛")。
对话存独立表 `fitness_coach_chat`(一行一消息,按 (date,day_id) 标识一次会话)。

**AI 调用全异步(关页面不丢)**:点 🤖 调整计划/分析 → POST 拿 job_id →
右下角药丸「正在生成…(可关页面)」转圈 → AI 在服务器 daemon 线程跑,结果落
`fitness_ai_job` → 前端轮询(4s),done → 药丸「✓ 就绪 · 点击查看」打开 modal。
刷新/换设备 restoreAiJobs()(localStorage + /ai/jobs)续接。dedup 同
(kind,day_id) 不重复跑。参照 qa_browser card-update job 模式。

### 设置

| 方法 | 路径 |
|---|---|
| GET | `/api/fitness/settings` |
| POST | `/api/fitness/settings` body `{key1: value1, ...}`(白名单)|

---

## AI 教练 (fitness_coach.py)

### 模型/Effort

走 `ai_client.ask(prompt, claude_model="opus", claude_effort="max")`,默认从 `fitness_settings` 读。

- **Opus + max**:深度思考,~1-3 分钟(前端按钮文案,看 4 周历史 + 文献),~$0.3 / 次(设置面板里 max 选项标注「3-5 分钟」是上界)
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
  ↓  ~1-3 min Opus + max(前端文案)
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
- 三种 source(2026-06-04 重构):
  - `auto`(🇨🇳)= YT caption + `_translate_all`(Google 机翻优先,快免费)
  - `hq`(🎯HQ)= **优先 YouTube 英文字幕原文**(`_fetch_english`,准确)+ `_translate_hq` **AI 精翻**(Gemini→Claude 分批,Google 仅 AI 全挂兜底);**无字幕才退回 STT**。旧版 HQ 直接用 Cloud STT 转录→英文本身易错→翻译差(用户反馈"HQ效果差")。改后 HQ 用准确字幕原文 + AI 处理,质量大幅提升。cache namespace 从 `stt` 换成 `hq` → 自动绕开旧 STT 垃圾缓存。
  - `stt` = 纯 Cloud STT(仅 hq 在无字幕时内部 fallback;UI 不再直接用)
- **HQ 翻译走 AI**(用户明确要"参考原字幕用 AI 处理",机翻太生硬):Gemini 2.5 Flash 优先(~5s),但 ⚠ Gemini 现 429(AI Studio 预付费余额耗尽,见踩坑#1)→ 落 **Claude 分批**(每 60 段,246 段约 130s,在 nginx 300s 内;断连由 inflight+cache 兜住,缓存后秒出)。Claude 实测 30 段 13s、质量自然。修好 Gemini billing 后 HQ 自动用更快的 Gemini。`auto`(🇨🇳)仍用 `_translate_all`(Google 机翻优先,快)
- 注:YT caption 是按时间切的句子片段,逐行译必然碎(中英都碎),Claude 在每批 60 行上下文里尽量连贯;这是字幕固有特性,非 bug
- 缓存:全局共享 SQLite `WEBAPP_DATA/youtube_subtitles.db`,key (video_id, target_lang, source)
- 并发锁:同视频同 source 并发请求只翻 1 次

### Cloud Speech-to-Text 流程

`_server_deploy/youtube_speech.py`:
1. yt-dlp 拉 m4a(只要 audio,体积小)
2. ffmpeg 切 50s chunks + 转 **WAV(LINEAR16)** 16kHz mono(⚠ 不用 FLAC,见踩坑#7)
3. **并发 4 worker** 调 STT `recognize` sync API(每片 <60s 限制;encoding=LINEAR16)
4. word-level + chunk offset 合并
5. word → segments(max_sec=5,max_words=15,gap>0.5 切)

成本:`latest_long` enhanced $0.024/min。赠金 ¥47867 ≈ **跑 4700 小时视频**。

---

## 成熟 app 化 UX(参照 Hevy/Strong,2026-05-31)

- **首页推荐练哪天**:home() 取每 plan day 最近训练日期,选「最久没练/没练过」
  的那天高亮 +「今天」角标 + 每按钮显示上次日期(PPL 自然轮转)。
- **每组幽灵值 + 预填全部组**:set-row 加 `.prev-ghost` 显示上次该组 `10kg×8`;
  loadRec 用 last_sets 预填全部 5 组(推荐重量×目标 reps,非只第 1 组)。
- **PR 检测 + 庆祝**:后端 `_exercise_pr`(最大重量 / Epley 估 1RM,排除今天);
  saveSet 后 `checkPR` 破纪录 → 行高亮 + 🏆 角标 + 顶部绿 toast + 振动;本地累进
  更新避免同次重复报。
- **历史页进度曲线**:history.html 选单动作 → 纯 SVG 折线(无外部依赖),
  顶组重量 / 估1RM / 总容量 三指标可切,带刻度/日期/涨跌幅。

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
- **草稿 vs 确认两态分离(2026-06-03 修)**:autosave 只存**草稿**(`dataset.saved=1`,弱灰边框 `#3a4658`,按钮仍 `✓`,**不绿、不计进度**);只有点 `✓`(saveSet)才进**确认态**(`dataset.confirmed=1`,绿底「✓ 已存」+ 绿边框 + 计入进度/完成总结 + 触发休息倒计时/PR/propagateForward)。`markSaved(row, complete, confirmed)` 第三参区分;进度条(updateProgress)和完成总结(showFinishSummary)都数 `data-confirmed=1`。**根因**:旧版 autosave 复用了 ✓ 的绿色渲染 → 两框一填就变绿「已存」,显得没点对勾就完成了。
- **比计划做得更重 → 后续组建议跟涨(2026-06-03 加)**:`propagateForward(card, fromSetNo, W, R)` 在 autosave 完成 / 点 ✓ 后调,把**还没被用户碰过**的后续组(空 或 仍带 `prefilled` class)建议值上调到 `max(当前建议, 本组实际)`,只增不减;已落库(`dataset.saved=1`)和已手填(脱了 prefilled)的组不动。取代旧的"只复制到下一组且 `if(!w.value)` 被预填值挡掉"的逻辑。
- **刷新页面**:initEx → restoreToday → loadRec(空位置才 prefill 推荐)。DB 不存 confirmed 列,restoreToday 对两值齐全的历史组按"已确认"恢复(绿+计数,最佳努力)。

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
   ↓ ~1-3 min(前端文案)
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
3. **Opus + max effort 慢**:前端文案标 ~1-3 分钟(设置面板里 max 选项标到「3-5 分钟」上界)。设置 modal 可调成 high(~45s)。`proxy_read_timeout 300s` 是 **Pi nginx**(`/etc/nginx/sites-available/bwicarus` 的 `/api` location)上的设置,Pi 上 max effort 偶尔会撞超时;git 里 `_server_deploy/nginx/bwicarus.conf`(VPS 版)的 `/api` 块**没设** `proxy_read_timeout`(只有 `/stocks` 设了 600),照那份找 300s 会找不到。
4. **plan.json 顺序敏感**:`upgrade_fitness_plan.py` 只更新现有动作,顺序保留;新增动作走 `add_pullup_exercises.py`(`_insert: prepend/after/replace`)。
5. **YouTube Data API 配额 10k/天硬上限**:不能用赠金扩。**配额耗尽就换本地 `reorder_videos_by_keyword.py` 离线调整,不调 API**。
6. **fitness_log schema migration**:加 UNIQUE INDEX 时存量数据可能有冲突(同 date+ex+set_no 多行),迁移脚本保留 max id 删旧。已对 bwicarus 用户跑过。
8. **视频"刷新后首播黑屏/等很久"**(2026-06-04 改):刷新后 PLAYERS{} 丢失,首个视频要重新下 YouTube IFrame API + 建 player,在远端慢网下很慢、且黑屏期间无反馈像卡死。修:① 页头 `preconnect` YouTube 各域名提前握手;② 页面空闲(`requestIdleCallback`)**提前预载** IFrame API(不等点🎬),把"下载 API"挪出点击→播放关键路径;③ `YT.Player` 加 `width/height:'100%'`(否则默认 640×390 固定尺寸)+ `playerVars.playsinline:1`(iOS 必须,否则内联黑屏)+ `origin`;④ 播放器 box 加"⏳加载播放器…"提示层 + 8s 超时/onError 露出"▶在 YouTube 打开"兜底链接。
9. **in-session 下一组建议要"镜像实际"不是"取 max"**(2026-06-04 改):propagateForward 早期版用 `max(开局推荐, 实际)` → 你录 10×12 但开局推荐 11×6(DP 建议加重)时,后续组保留更高的 11(还有 reps 没跟上显示 11×6),反直觉。改为**直接用你刚录的实际值覆盖**未碰过的后续组 → 后续镜像你当前表现(做重→跟重,做轻→跟轻)。跨日 DP 仍由后端按落库数据重算,不受影响。
7. **★HQ 字幕(Cloud STT)切片必须用 WAV 不能用 FLAC★**(2026-06-03 修):`ffmpeg -f segment -c:a flac` 的 segment muxer 会把**整段**总时长写进**每片** FLAC 的 STREAMINFO `total_samples`(实测最后一片头报整段 130s,而真实仅 13s),STT sync `recognize` 据此判为 >60s → `400 Sync input too long. For audio longer than 1 min use LongRunningRecognize`。改用 `-segment_format wav -c:a pcm_s16le`(WAV 的 segment muxer finalize 时写正确的 per-chunk data 大小)+ STT config `encoding=LINEAR16` → 头时长准确,STT 200。WAV 16k mono 50s ≈1.6MB,base64 ~2.1MB,远低于 sync 10MB 限。ffprobe 验证:FLAC 末片头=130s(整段)/ WAV 三片头=50/50/30s(正确)。端到端实测 19s 视频 → 4 段转录 OK。

---

## 视频收藏置顶 + AI 要点 + 视频过滤修复 + 3D 模型（2026-06-07）

- **视频过滤修复**（`scripts/find_jeff_videos.py`）：用户报"视频跟动作对不上"。根因三连——① `MUST_CONTAIN` 键名 `db_lateral_raise` ≠ plan 里的 `cable_lateral_raise`（取不到过滤词 → 侧平举完全不过滤）；② 关键词太松（`["squat"]` 放行"保加利亚分腿蹲"）；③ 某频道匹配不够时用**跑偏视频凑数**到 5 个（划船被塞 Lat Pulldown/Rear Delt）。修：键名对齐 plan id + 新增 `MUST_EXCLUDE` 负向词（剔"另一个动作/另一块肌群"）+ **取消用跑偏视频凑数**（宁可少而准）+ 新增 `--prune-existing`（不调 API、按规则清洗现有 plan.json）。清洗了 plan（剔 45）+ 用户 3 个 override（per-user DB，剔 7）。`ocr_one_page` 无关，但 `google_vision_ocr.py` 也在本批改（见 pdf-reader §32）。
- **视频收藏置顶**：per-user 表 `fitness_video_favorite(exercise_id, video_id)`；`GET /api/fitness/videos/<id>` 收藏置顶（**稳定排序**，各组保原序）+ 标 `favorite`；`POST /api/fitness/videos/<id>/favorite {video_id, favorite}`。log 页控制条 ☆/⭐ 按钮（切换后本地稳定排序到首位 + 重渲）。
- **AI 要点 + 时间锚点**：`GET /api/fitness/summary/<vid>` → `youtube_subtitles.summarize_video`：拉字幕（复用 `get_or_translate` 缓存）→ 喂带 `[秒]` 时间戳的字幕给 AI → 输出"秒数 | 要点" → `_parse_points` 解析 → 缓存表 `youtube_summaries`。log 页 📌要点：列表每条 `[mm:ss] 要点`，点击 `player.seekTo(t)` 跳转。Gemini Flash 优先、**429 落 Claude**（webapp `.env` 有 `CLAUDE_PROJECT`；`youtube_subtitles` 顶部 `os.environ.setdefault("CLAUDE_PROJECT", PROJECT_ROOT)` 防独立脚本报 `C:\claude`）。实测练肩视频产出 8 条可读要点 + 准时间戳。
- **3D 肌肉模型**（task #203，`templates/fitness/body.html` + `static/fitness/body_muscles.glb` 2.1M + `static/three/` Three.js）：用户给的 C 盘"人体模型" → Blender headless 转 glTF（含模型自带头部，不用程序生成）→ Three.js GLTFLoader 渲染；按强度（est-1RM，同 modality 拮抗肌比值）算肌肉平衡（`fitness_coach.py` 的 `_compute_strength_ratios`/`_balance_profile`，非按训练量）。

## 相关 reference

- [`google-cloud-apis.md`](google-cloud-apis.md) — Vision / YouTube / STT / Gemini 集成 + 赠金计费
- [`webapp-development.md`](webapp-development.md) — Flask + nginx 部署框架（全站 PWA service worker 也在此）
- [`server-config-schema.md`](server-config-schema.md) — server 端配置(不含 fitness)
