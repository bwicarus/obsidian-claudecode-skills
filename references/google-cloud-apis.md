# Google Cloud APIs 集成

GCP project `My First Project` (project# 915753578532, org `bwicarus-org`) 启用了多个 API,
用于 OCR / 视频字幕 / 翻译 / 语音转录。**¥47,867 Free Trial 赠金** 垫底。

---

## 已启用 API + key 隔离

| API | endpoint | 用途 | 用哪个 key |
|---|---|---|---|
| Cloud Vision API | `vision.googleapis.com` | PDF OCR(`google_vision_ocr.py`)| `AIzaSy...` key |
| YouTube Data API v3 | `youtube.googleapis.com` | 频道视频搜索 | `AIzaSy...` key |
| Cloud Speech-to-Text API | `speech.googleapis.com` | 健身视频高质量字幕 | `AIzaSy...` key |
| Generative Language API(Gemini) | `generativelanguage.googleapis.com` | 字幕翻译 | `AQ.Ab...` service-account-绑定 key |

### Key 文件位置(Pi)

```
/home/bwicarus/.config/gcp-vision-key  600  # AIzaSy* (Vision + YouTube + STT)
/home/bwicarus/.config/gemini-api-key   600  # AQ.Ab*  (Gemini 专用)
```

### Key 配置规则

**`AIzaSy*` key(Vision + YouTube + STT)**:
- GCP Console → API 和服务 → 凭据
- API 限制(必须勾上):
  - Cloud Vision API
  - YouTube Data API v3
  - Cloud Speech-to-Text API

**`AQ.Ab*` key(Gemini)**:
- 服务账号绑定的 API key(新功能)
- 绑定 `claude@project-d3b91b36-5ecf-4822-8f9.iam.gserviceaccount.com`
- API 限制:只勾 Gemini API
- 即使泄露也只能调 Gemini

---

## 计费分流(关键踩坑)

| API | 计费走哪 | 用赠金? |
|---|---|---|
| **Cloud Vision** | GCP Cloud Billing | ✅ 烧赠金 |
| **YouTube Data API** | 不付费扩(每天 10k units 硬上限)| ❌ 不能 |
| **Cloud Speech-to-Text** | GCP Cloud Billing | ✅ 烧赠金 |
| **Gemini API**(`generativelanguage`) | **AI Studio 独立 billing** | ❌ **跟 GCP 赠金不通!** |
| Vertex AI Gemini(`aiplatform`) | GCP Cloud Billing | ✅ 烧赠金,但要 service account JSON |

### Gemini billing 详解

Google 故意把 `generativelanguage.googleapis.com` 跟 GCP billing 隔离 — [文档](https://ai.google.dev/gemini-api/docs/billing) 明说 "uses AI Studio's payment provider, not Cloud Billing"。

后果:
- AI Studio 有自己的 free tier(Gemini 2.5 Flash **250 req/天**,Pro **0**)
- 触发"prepayment"模式后,prepay 余额 0 时报 `prepayment depleted` 即使在 free tier 也无效
- 用户的 ¥47,867 赠金**不能用于这个 endpoint**

**实战策略**:字幕翻译这点用量(每天 5-10 视频),走 AI Studio free tier 完全够。赠金留给 Vision / STT 这种量大的。

### 赠金能跑多少(¥47,867 ≈ $6,800)

| 服务 | 单价 | 能跑多少 |
|---|---|---|
| Cloud Vision OCR | $1.50/k pages | **~440 万次** → 6500 本 679 页书 |
| Cloud STT `latest_long` enhanced | $0.024/min | **~4,700 小时**视频 |
| Cloud TTS WaveNet | $16/M chars | 4.25 亿字符 |
| Cloud Translation | $20/M chars | 3.4 亿字符 |

---

## 本地配额计数器

`scripts/google_api_quota.py`:

- SQLite `state/google_api_quota.db` 表 `quota_log`,记每次调用 (id 自增主键, service, units, action, note, ts_utc)
- 各调用脚本(find_jeff_videos / google_vision_ocr / youtube_speech / youtube_subtitles)都接了 `log_usage()`
- CLI:`python scripts/google_api_quota.py [youtube|vision]`(只 youtube/vision 有 `DAILY_LIMITS`;传 stt/gemini 会因 `used/limit*100` 除零崩溃,这俩只能在脚本里用 `report()`)

### 当前已知配额(各 service)

```python
DAILY_LIMITS = {
    "youtube": 10_000,    # search.list 每次 100 units;实际硬上限
    "vision":  1_000,     # 免费/月;超出 $1.50/1000(走赠金)
}
```

YouTube 重置:Pacific Time 午夜 = UTC 08:00。脚本里默认取 UTC 08:00 安全(PST 准 / PDT 早 1h)。

### 跑过的脚本接入

| 脚本 | log 内容 |
|---|---|
| `scripts/find_jeff_videos.py` | `_log_quota("youtube", 100, "search.list", note=q)` |
| `scripts/google_vision_ocr.py` | `_log_quota("vision", 1, "images:annotate")` |
| `_server_deploy/youtube_speech.py` | `_log_quota("stt", CHUNK_SEC, "speech:recognize")` |
| `_server_deploy/youtube_subtitles.py` | `_log_quota("gemini", 1, "generateContent:flash")` |

---

## 集成位置(代码路径)

### Vision OCR

- `scripts/google_vision_ocr.py` — 主调用模块,并发 worker
- `scripts/embed_google_ocr_to_pdf.py` — 把 sidecar JSON 嵌进 PDF 文字层
- 用法:`python scripts/google_vision_ocr.py <pdf> [--workers N]`
- sidecar 路径:`state/google-vision-ocr/<sha>/p<num>.json`
- 实测 679 页 IT 书:workers=15 时 ETA 11min,total ~3 美刀

### YouTube Data API 搜索

- `scripts/find_jeff_videos.py` — `--channels jeff_nippard,athlean_x --per 5`
- 内置 `MUST_CONTAIN` dict 过滤标题
- 配额:每动作每频道 1 次 search.list = 100 units(`OVERSEARCH=3` 只放大单次请求的 `maxResults`,不增加 API 调用次数,不乘进配额)
- 双频道 20 动作 ≈ 4000 units(10000/天上限内可跑约 2 次)
- 没配额了用 `scripts/reorder_videos_by_keyword.py`(**纯本地不调 API**)

### Cloud Speech-to-Text

- `_server_deploy/youtube_speech.py::transcribe_youtube(video_id)`
- 流程:yt-dlp → ffmpeg 切 50s chunks → 并发 STT sync recognize → words → segments
- 跟 `youtube_subtitles.get_or_translate(source="stt")` 集成

### Gemini Flash 翻译

- `_server_deploy/youtube_subtitles.py::_translate_gemini_flash()`
- REST 直调 `generateContent` + `thinkingConfig.thinkingBudget=0`(字幕不需要思考)
- maxOutputTokens=32000
- 失败 fallback Claude(`_translate_claude` 走 `ai_client.ask`)

---

## 操作清单

### 启用新 GCP API(用户做)

1. `https://console.cloud.google.com/apis/library/<service>.googleapis.com` 直接 URL
2. 点 **启用**
3. 凭据页编辑 API key → API 限制勾上新 API
4. **检查 org policy**:如果显示"组织政策不允许",bwicarus-org 拦了,需 org owner 改

### regenerate key(暴露后)

`AIzaSy*` 和 `AQ.Ab*` 都在 GCP Console → 凭据 → 选 key → "重新生成密钥"。新 key 替换文件:

```bash
echo -n "<new-key>" > /home/bwicarus/.config/gcp-vision-key  # or gemini-api-key
chmod 600 /home/bwicarus/.config/gcp-vision-key
```

所有脚本都从这俩文件读,无需改代码。

### 配额耗尽应急

| 服务 | 应急 |
|---|---|
| YouTube Data | 等明日 UTC 08:00 重置;期间用 `reorder_videos_by_keyword.py` |
| Vision | 通常不会(免费 1000/月 + 赠金垫底)|
| STT | 同上 |
| Gemini Flash | 等次日 PT 午夜重置;或切回 Claude(fallback 自动)|

---

## .gitignore 已加

```
*gcp-vision-key*
*google-vision-key*
.config/gcp-vision-key
```

> 注:`.gitignore` 实际只有上面 3 行。gemini key 文件在仓库外的 `~/.config/gemini-api-key`,本就不会被提交;但目前 `.gitignore` 里**没有** `*gemini-api-key*` / `.config/gemini-api-key` 规则——若日后把 key 文件挪进仓库内需补上。

---

## 安全提醒

`AIzaSy…` GCP API key **在聊天历史中暴露过**(2026-05-28),正常应该 regenerate。但因为已经 API 限制锁死只能调 Vision/YouTube/STT,即使被恶意使用也只能烧赠金,影响有限。当前(2026-05-31)暂不 regenerate。

`AQ.Ab…` Gemini key **2026-05-31 用户在聊天中贴出**,绑定 service account 只能调 Gemini API。建议 regenerate 时把新 key 写到 `/home/bwicarus/.config/gemini-api-key`。

> 文档不写完整 key 字符,避免触发 GitHub Push Protection。两个 key 的当前值都在 Pi 的 `/home/bwicarus/.config/*-key` 文件里(chmod 600)。

---

## Cloud Translation API(PDF 句子翻译,2026-05-31 接入)

PDF 阅读器多选句子的 🌐 翻译走 `scripts/vocab/translate.py`,链路 `gtranslate → deepl → ai → mymemory`(`server-config.dict.translate_backend = "auto"`)。

**`_gtranslate`**:Cloud Translation **v2** REST,POST `https://translation.googleapis.com/language/translate/v2`,form 参数 `q / target=zh-CN / format=text / key=<GCP key>`,**不传 source**(让 Google 自动检测,en/ja 都准),~0.3s、走赠金。key 复用 `~/.config/gcp-vision-key`。

**`gtranslate_batch(texts)`**:v2 批量(一次多段,按段数 64 + 累计 3000 字分块)。**视频字幕翻译**用它(`youtube_subtitles._translate_all` 首选 Google → Gemini → Claude;之前主用 Gemini 因 429 长期退化到慢 Claude)。整集字幕一两秒、EN→ZH 质量高,quota 日志记为 `translate` service。

### 踩坑:`API_KEY_SERVICE_BLOCKED`(key 的 API 限制没放行)
- 现象:403 `PERMISSION_DENIED` + `reason: API_KEY_SERVICE_BLOCKED` + `service: translate.googleapis.com`。**不是**配额/计费问题。
- 根因:该 key 的「API 限制」白名单里没有 Cloud Translation。
- 放行(GCP 控制台):① 启用 Cloud Translation API ② 凭据→该 key→API 限制勾上 Cloud Translation API(连同已有 Vision/YouTube/STT)。
- **传播抖动**:刚放行后几分钟内**间歇性** 403(边缘节点逐步生效)→ 同批句子随机失败几条,几分钟稳定。`auto` 链遇 gtranslate 失败自动回落,用户无感。

### 翻译质量评审结论(11-agent workflow,10 句真实日语)
| 引擎 | 准确度均 | 流畅度均 | 拿「最佳」 |
|---|---|---|---|
| **Google** | 3.9/5 | **4.5/5** | 3(+1 平手) |
| AI(sonnet) | ~4.6 | ~4.8 | **6** |
| MyMemory | ~3.2 | ~3.1 | 0 |
- **AI 最强 > Google 次之 >> MyMemory**。Google 流畅度稳,准确度起伏是短板。
- Google 高发翻车区:**考试术语**(午後問題→误"下午课程")、**关键时间点译反**(当日→"第二天")、长句限定/因果结构丢。简单/术语清晰句又快又准甚至胜 AI。
- 决策:`auto = gtranslate→deepl→ai→mymemory` —— **AI 排 MyMemory 前**(Google 抖动时回落到质量最高的 AI,不回落垫底的 MyMemory),MyMemory 仅 Google+AI 都挂时兜底。

### Claude CLI「冷启动」与「热进程」实验结论
- `ai_client` 每次 `subprocess.run(["claude",...])` **从零起进程**(Node 启动+鉴权+建连),~4-5s,**大头是启动不是模型** → haiku 反而 5.2s 比 sonnet 4.6s 慢(换模型无用)。
- 热进程实验(`claude -p --input-format stream-json --output-format stream-json --verbose` 常驻逐条喂):冷 5.2s / 热 3.1s / 第3条 5.0s(上下文累积变慢)。省的只是 Node 启动 ~2s,网络往返+agent 轮次开销消不掉,单一对话越喂越长越慢。**结论:不值得做**(收益 3-5s、复杂度高),用 Google/MyMemory(HTTP ~1s)更划算。

---

## 相关 reference

- [`fitness-system.md`](fitness-system.md) — 健身页面(主要消费者:YouTube/STT/Gemini)
- [`pdf-reader.md`](pdf-reader.md) — PDF 阅读器(主要消费者:Vision OCR)
- [`vocab-system.md`](vocab-system.md) — 单词/中日词典 + 句子翻译
