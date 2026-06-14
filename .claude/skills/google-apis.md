# Skill: google-apis — 直接 HTTP 调 Google AI/Cloud API

> 自包含、可移植。把这一份给任何 AI/agent,它就能直接用 HTTP 调 Google 的 OCR / 语音转文字 / Gemini / 翻译 / YouTube。
> 不依赖任何 SDK,只用 `requests`(或 `urllib`)+ 一个 API key。所有代码摘自一套已上线跑通的系统。

## 0. 最重要的两件事(踩过坑,先读)

### 0.1 两把 key,别混用

| key 形态 | 给谁用 | 计费走哪 |
|---|---|---|
| **`AIzaSy...`**(普通 API key) | Cloud **Vision** / **Speech-to-Text** / **Translation** / **YouTube Data** | **GCP Cloud Billing**(可烧 GCP 免费赠金 / Free Trial) |
| **`AQ.Ab...`**(AI Studio / service-account 绑定 key) | **Gemini**(`generativelanguage.googleapis.com`) | **AI Studio 独立 billing**(有免费档,但**跟 GCP 赠金不通**) |

- 这套系统把两把 key 存两个文件:`~/.config/gcp-vision-key`(AIzaSy*)、`~/.config/gemini-api-key`(AQ.Ab*)。换你自己的环境就放环境变量或任意安全位置。
- **`AIzaSy*` key 要在 GCP 控制台逐个「API 限制」放行**你要调的 API(Vision / Speech / Translation / YouTube 各自勾上)。没放行会 **403 `API_KEY_SERVICE_BLOCKED`**。

### 0.2 ⚠ 计费分流大坑

**Gemini API(`generativelanguage`)走 AI Studio 自己的 billing,跟 GCP 的 Free Trial 赠金完全不通。** 你 GCP 里有几百刀赠金,调 Gemini 一分钱都不扣赠金、走的是 Gemini 自己的免费额度/独立计费。想用 GCP 赠金跑 Gemini 得走 **Vertex AI**(`aiplatform.googleapis.com`,要 service-account JSON,不在本 skill 范围)。

### 0.3 认证方式

全部用 **`?key=<API_KEY>` query 参数**(最简单,无需 OAuth/token)。`key` 拼在 URL 后即可。

```python
import os, requests
GCP_KEY    = os.environ["GCP_API_KEY"]      # AIzaSy*  (Vision/Speech/Translation/YouTube)
GEMINI_KEY = os.environ["GEMINI_API_KEY"]   # AQ.Ab*   (Gemini)
```

---

## 1. Gemini —— 文本生成(翻译/分析/问答/JSON 抽取)

- 端点:`POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent?key=<GEMINI_KEY>`
- 常用 model:`gemini-2.5-flash`(快、便宜、免费档约 250 请求/天)、`gemini-2.5-pro`(强)。
- `thinkingConfig.thinkingBudget: 0` 关掉推理(翻译/简单任务更快更省);要深度推理就去掉或设大。

```python
def gemini(prompt: str, model: str = "gemini-2.5-flash", thinking: int = 0,
           temperature: float = 0.3, max_tokens: int = 32000) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": thinking},   # 0=不思考;省去则默认思考
        },
    }, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError("Gemini: " + data["error"].get("message", ""))
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p["text"] for p in (cand.get("content") or {}).get("parts", []) if "text" in p)
    if not text:
        raise RuntimeError(f"Gemini empty (finishReason={cand.get('finishReason')})")
    return text
```

- **多模态**(图/音频/视频):把 `parts` 里加一项 `{"inline_data": {"mime_type": "image/png", "data": "<base64>"}}` 或 `{"file_data": {...}}`。Gemini Flash 能直接读图/读音频 → 可当 OCR / STT 的替代(且常更准、更省事)。
- **结构化输出**:`generationConfig` 加 `"responseMimeType": "application/json"` + `"responseSchema": {...}` 强制返回合法 JSON。
- 免费档约 250 req/天(Flash);超了 429。

---

## 2. Cloud Speech-to-Text —— 语音转文字

- 端点:`POST https://speech.googleapis.com/v1/speech:recognize?key=<GCP_KEY>`
- 用 `AIzaSy*` key,走 GCP 赠金。`latest_long` + `useEnhanced` 质量高。
- 音频要 base64;同步接口单请求 ≤ ~1 分钟,长音频先用 ffmpeg 切块(`ffmpeg -i in.m4a -ar 16000 -ac 1 -f segment -segment_time 55 ... out_%03d.wav`)再逐块调。

```python
import base64
def stt(audio_path, language="zh-CN", sample_rate=16000, encoding="LINEAR16") -> list[dict]:
    content = base64.b64encode(open(audio_path, "rb").read()).decode()
    r = requests.post(f"https://speech.googleapis.com/v1/speech:recognize?key={GCP_KEY}", json={
        "config": {
            "encoding": encoding,            # LINEAR16(wav) / MP3 / OGG_OPUS / FLAC ...
            "sampleRateHertz": sample_rate,
            "languageCode": language,        # zh-CN / en-US / ja-JP ...
            "enableAutomaticPunctuation": True,
            "enableWordTimeOffsets": True,   # 要逐词时间戳就开
            "model": "latest_long", "useEnhanced": True,
        },
        "audio": {"content": content},
    }, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"STT HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError("STT: " + data["error"].get("message", ""))
    out = []
    for res in data.get("results", []):
        alt = (res.get("alternatives") or [{}])[0]
        out.append({"transcript": alt.get("transcript", ""),
                    "words": [{"word": w["word"],
                               "start": float(w["startTime"].rstrip("s")),
                               "end":   float(w["endTime"].rstrip("s"))}
                              for w in alt.get("words", [])]})
    return out
```

- 只要整段文字、不要时间戳 → `enableWordTimeOffsets:False`,读 `results[*].alternatives[0].transcript` 拼起来。
- Cloud STT **无固定免费日限**,按 Free Trial 赠金/Cloud Billing 计费(`latest_long` enhanced ≈ $0.024/min)。
- **替代方案**:直接把音频 base64 喂 Gemini Flash(`inline_data`,mime `audio/wav`)让它转录,常更省事且走 Gemini 免费档。

---

## 3. Cloud Vision —— OCR(图片/扫描 PDF 取文字)

- 端点:`POST https://vision.googleapis.com/v1/images:annotate?key=<GCP_KEY>`
- 用 `AIzaSy*` key。`DOCUMENT_TEXT_DETECTION`(密集排版/扫描页)比 `TEXT_DETECTION`(零散文字)更适合整页。
- 图片 base64;PDF 先用 PyMuPDF/pdf2image 渲染成 PNG 再逐页调。

```python
def ocr(png_bytes: bytes, feature="DOCUMENT_TEXT_DETECTION") -> dict:
    r = requests.post(f"https://vision.googleapis.com/v1/images:annotate?key={GCP_KEY}", json={
        "requests": [{
            "image": {"content": base64.b64encode(png_bytes).decode()},
            "features": [{"type": feature}],
            # "imageContext": {"languageHints": ["ja", "zh", "en"]},   # 可选:语言提示
        }],
    }, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Vision HTTP {r.status_code}: {r.text[:300]}")
    resp = (r.json().get("responses") or [{}])[0]
    if "error" in resp:
        raise RuntimeError("Vision: " + resp["error"].get("message", ""))
    full = resp.get("fullTextAnnotation", {})
    return {"text": full.get("text", ""), "pages": full.get("pages", [])}   # pages 里有逐字 bbox
```

- 整页纯文本读 `fullTextAnnotation.text`;要逐字/逐词 bbox 钻 `fullTextAnnotation.pages[].blocks[].paragraphs[].words[].symbols[]`(每个 symbol 带 `boundingBox.vertices`)。
- Vision 免费档约 1000 单元/月(之后按量),`AIzaSy*` 走 GCP 赠金。

---

## 4. Cloud Translation v2 —— 机器翻译

- 端点:`POST https://translation.googleapis.com/language/translate/v2`(参数走 form-urlencoded,含 `key`)
- 用 `AIzaSy*` key,走 GCP 赠金,~0.3s/请求,质量好。不指定 `source` → Google 自动检测源语言。

```python
import urllib.parse, urllib.request, json
def translate(text, target="zh-CN") -> str | None:
    data = urllib.parse.urlencode({"q": text, "target": target, "format": "text", "key": GCP_KEY}).encode()
    req = urllib.request.Request("https://translation.googleapis.com/language/translate/v2", data=data)
    with urllib.request.urlopen(req, timeout=8) as resp:
        d = json.loads(resp.read())
    trs = (d.get("data") or {}).get("translations") or []
    return (trs[0]["translatedText"].strip() if trs else None) or None

def translate_batch(texts, target="zh-CN", chunk_n=64, chunk_chars=3000):
    """一次多段(顺序对应)。v2 单请求 ≤128 段,这里按段数+字符数分块。返回等长译文列表。"""
    out = ["" for _ in texts]; i = 0
    while i < len(texts):
        batch, idxs, cc = [], [], 0
        while i < len(texts) and len(batch) < chunk_n and cc < chunk_chars:
            t = (texts[i] or "").strip()
            if t: batch.append(t); idxs.append(i); cc += len(t) + 1
            i += 1
        if not batch: continue
        params = [("q", t) for t in batch] + [("target", target), ("format", "text"), ("key", GCP_KEY)]
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request("https://translation.googleapis.com/language/translate/v2", data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                trs = (json.loads(resp.read()).get("data") or {}).get("translations") or []
            for k, idx in enumerate(idxs):
                if k < len(trs): out[idx] = (trs[k].get("translatedText", "") or "").strip()
        except Exception:
            pass   # 该块失败留空,调用方据空率决定是否回退别的翻译源
    return out
```

- `target` 中文用 `zh-CN`。403 `API_KEY_SERVICE_BLOCKED` = 没在控制台给 key 放行 Translation API。

---

## 5. YouTube Data v3 —— 找视频/取元数据(配额很省着花)

- 端点:`GET https://www.googleapis.com/youtube/v3/<resource>?...&key=<GCP_KEY>`(search/videos/channels/captions)
- 用 `AIzaSy*` key。**每天 10000 units 硬上限**,Pacific Time 午夜重置(≈北京时间 16:00 PDT / 17:00 PST)。

```python
def yt_search(q, max_results=5, **extra):
    p = {"part": "snippet", "q": q, "type": "video", "maxResults": max_results, "key": GCP_KEY, **extra}
    r = requests.get("https://www.googleapis.com/youtube/v3/search", params=p, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])
```

- **units 成本(贵的别乱调)**:`search.list`=**100**、`videos.list`=1、`channels.list`=1、`captions.list`=50、`captions.download`=200。一天就 10000,`search` 调 100 次就没了 → 缓存结果、能用 `videos.list` 别用 `search.list`。

---

## 6. 本地配额计数器(可选但强烈建议)

Google 不给实时配额查询。自己每次调用前记一笔到 SQLite,就能本地估算用了多少、还剩多少(尤其 YouTube 的 10000/天 硬限)。

```python
import sqlite3, time
DAILY_LIMITS = {"youtube": 10_000, "vision": 1_000, "gemini": 250, "stt": None}  # None=无固定日限
def log_usage(db_path, service, units, action, note=""):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS usage(ts INT, service TEXT, units INT, action TEXT, note TEXT)")
    con.execute("INSERT INTO usage VALUES(?,?,?,?,?)", (int(time.time()), service, units, action, note))
    con.commit(); con.close()
# YouTube 配额按 Pacific Time 午夜重置:统计"今天(PT)"的累计 units 跟 10000 比
```

---

## 7. 错误速查

| 现象 | 原因 / 解法 |
|---|---|
| `403 API_KEY_SERVICE_BLOCKED` | `AIzaSy*` key 的「API 限制」没放行该 API → GCP 控制台 → 凭据 → 编辑 key → 勾上对应 API。刚放行有几分钟传播抖动。 |
| `400 API key not valid` | key 错 / 用错把(Gemini 用了 AIzaSy*,或反之)。 |
| Gemini 不扣 GCP 赠金 | 正常。Gemini 走 AI Studio 独立 billing,跟 GCP 赠金不通(见 §0.2)。要烧赠金用 Vertex AI。 |
| `429 RESOURCE_EXHAUSTED` | 超免费档/配额。Gemini Flash 免费档约 250/天;YouTube 10000 units/天。 |
| Gemini 返回空 + `finishReason: MAX_TOKENS` | `maxOutputTokens` 太小,或 prompt 太长。调大或截断。 |
| Gemini 返回空 + `finishReason: SAFETY` | 被安全过滤。换措辞或加 `safetySettings` 放宽(BLOCK_NONE)。 |
| STT 返回 results 为空 | 音频编码/采样率跟 `config` 不符(最常见:实际是 mp3 却写 LINEAR16),或音频太长(同步接口 ≤1min)。 |

---

## 8. Gemini 进阶(多模态 / 结构化 / 流式 / 对话 / 向量)

### 8.1 多模态:直接喂图片 → 当 OCR / 看图问答用

Gemini Flash 能直接读图,常比 Vision OCR 更省事(一步出结构化结果),也走 Gemini 免费档。

```python
def gemini_vision(prompt: str, image_path: str, mime="image/png", model="gemini-2.5-flash") -> str:
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    r = requests.post(url, json={"contents": [{"parts": [
        {"text": prompt},                                  # 如「把这张图里的文字逐行提取出来」
        {"inline_data": {"mime_type": mime, "data": b64}},
    ]}]}, timeout=120)
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    return "".join(p.get("text","") for p in (cand.get("content") or {}).get("parts", []))
```

- 同理传音频(`mime_type:"audio/wav"`/`"audio/mp3"`)让它**转录**——可替代 Cloud STT。
- `inline_data` 适合 < ~20MB;更大用 Files API(`POST .../upload/v1beta/files`)拿 `file_uri` 再 `{"file_data":{"file_uri":...,"mime_type":...}}`。

### 8.2 结构化输出:强制返回合法 JSON(免解析地狱)

```python
def gemini_json(prompt: str, schema: dict, model="gemini-2.5-flash") -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,        # 见下
        },
    }, timeout=120)
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    txt = "".join(p.get("text","") for p in (cand.get("content") or {}).get("parts", []))
    return json.loads(txt)

# schema 例:抽取单词列表
SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "word": {"type": "string"}, "pos": {"type": "string"}, "zh": {"type": "string"}},
    "required": ["word", "zh"]}}
```

### 8.3 流式(边出边收,做实时朗读/打字机效果)

把 `generateContent` 换成 `streamGenerateContent?alt=sse&key=...`,响应是 SSE,逐块取 `candidates[0].content.parts[].text`。

```python
def gemini_stream(prompt, model="gemini-2.5-flash"):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={GEMINI_KEY}"
    with requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}]}, stream=True, timeout=300) as r:
        for line in r.iter_lines():
            if not line or not line.startswith(b"data: "): continue
            chunk = json.loads(line[6:])
            for p in (chunk.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
                if "text" in p: yield p["text"]
```

### 8.4 多轮对话 + 系统指令

`contents` 是消息数组,`role` 为 `"user"`/`"model"` 交替;系统指令单独放 `systemInstruction`。

```python
body = {
  "systemInstruction": {"parts": [{"text": "你是简洁的日语老师,只用中文回答。"}]},
  "contents": [
    {"role": "user",  "parts": [{"text": "「食べる」是什么意思?"}]},
    {"role": "model", "parts": [{"text": "吃。"}]},
    {"role": "user",  "parts": [{"text": "它的て形呢?"}]},   # 带着上文继续
  ],
}
```

### 8.5 放宽安全过滤(被 SAFETY 拦时)

```python
"safetySettings": [
  {"category": c, "threshold": "BLOCK_NONE"} for c in
  ["HARM_CATEGORY_HARASSMENT","HARM_CATEGORY_HATE_SPEECH",
   "HARM_CATEGORY_SEXUALLY_EXPLICIT","HARM_CATEGORY_DANGEROUS_CONTENT"]
]
```

### 8.6 文本向量(语义搜索 / 去重 / 聚类)

- 端点:`POST https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key=<GEMINI_KEY>`

```python
def embed(text: str) -> list[float]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_KEY}"
    r = requests.post(url, json={"content": {"parts": [{"text": text}]}}, timeout=30)
    r.raise_for_status()
    return r.json()["embedding"]["values"]   # 768 维向量
```

---

## 9. 其它常用端点

### 9.1 Translation —— 语言检测

```python
def detect_lang(text):
    data = urllib.parse.urlencode({"q": text, "key": GCP_KEY}).encode()
    req = urllib.request.Request("https://translation.googleapis.com/language/translate/v2/detect", data=data)
    with urllib.request.urlopen(req, timeout=8) as resp:
        d = json.loads(resp.read())
    return d["data"]["detections"][0][0]["language"]   # 'en' / 'ja' / 'zh-CN' ...
```

### 9.2 YouTube —— 字幕(captions)

```python
# 列字幕轨(50 units):
caps = requests.get("https://www.googleapis.com/youtube/v3/captions",
                    params={"part": "snippet", "videoId": vid, "key": GCP_KEY}).json()
# 注:captions.download(200 units)只能下自己上传的轨;别人的视频字幕一般用 youtube-transcript-api / yt-dlp 抓。
```

### 9.3 列可用模型

```bash
curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_KEY"   # 看有哪些 gemini-* 可用
```

---

## 10. curl 速测(给 key 探活,5 秒确认能不能用)

```bash
# Gemini
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"用一句话证明你在工作"}]}]}'

# Translation
curl -s "https://translation.googleapis.com/language/translate/v2" \
  --data-urlencode q="hello world" --data target=zh-CN --data format=text --data key=$GCP_API_KEY

# Vision(需先有 base64 图)/ STT(需 base64 音频):同上,贴 §3/§2 的 json body
```

---

## 11. 统一重试 + 限流封装(生产用)

```python
import time, random
def call_with_retry(fn, *a, tries=4, base=1.0, **kw):
    """429/5xx 指数退避重试;403/400 不重试(配置错,重试也没用)。"""
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except RuntimeError as e:
            msg = str(e)
            if any(c in msg for c in (" 429", " 500", " 502", " 503", " 504", "RESOURCE_EXHAUSTED")) and i < tries-1:
                time.sleep(base * (2**i) + random.random()); continue
            raise   # 403/400/其它 → 直接抛(别浪费重试)
```

---

## 12. "我该用哪个 API" 速查

| 任务 | 首选 | 备选 / 说明 |
|---|---|---|
| 看图取文字(扫描页/密排) | **Vision DOCUMENT_TEXT_DETECTION**(要逐字 bbox) | Gemini 多模态(要语义/版面理解、不要 bbox) |
| 看图问答 / 图表理解 | **Gemini 多模态** | — |
| 语音转文字(要逐词时间戳) | **Cloud STT** `latest_long` | — |
| 语音转文字(只要整段文字) | **Gemini 多模态**(喂音频) | Cloud STT |
| 机器翻译(大批量、快、便宜) | **Translation v2 batch** | Gemini(要意译/术语/语气) |
| 文本生成/分析/抽取/JSON | **Gemini Flash**(快省)/ **Pro**(难任务) | — |
| 语义搜索/相似度/去重 | **text-embedding-004** | — |
| 找视频/频道元数据 | **YouTube Data v3**(省着调,10k units/天) | — |

> 省钱原则:能用离线/免费(本地 OCR、离线词典、Gemini 免费档)就别烧 Cloud Billing;Cloud STT/Vision/Translation 烧 GCP 赠金;Gemini 烧 AI Studio 独立额度(两者不通,见 §0.2)。
