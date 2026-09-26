# 环境旁听 · 独立 jev 判断 · 嘈杂环境人声隔离（2026-09-26）

用户原话：
- 「持续录音并转写，用 FluidAudio 分离不同的人，让 jev 判断是否有意义 / 是否有即时危险 / 是否有需要解决的疑问 / 是否需要记录」
- 「嘈杂环境下我基本上无法使用 AI 对话……检测到多个人说话时自动开启（去掉其他人的声音）」
- 「jev 实时判断要做成一个独立的模块，它可以在任意时间工作，而不是只有在语音开启时」
- 「有可能为不同的人的声音建立特征然后标记名字么」

先做 iPad；手表、眼镜以后接同一个服务端入口（`source` 字段区分来源）。

## 一张图

```
iPad 麦克风（平时）/ 通话引擎上行旁路（通话中）
   ├─ SFSpeechRecognizer（强制本机识别，停顿 1.2 秒或满 45 秒换任务拿定稿）
   ├─ 16 kHz → FluidAudio Sortformer（fastV2_1，只回答「哪几段是同一个人」，同时在场最多 4 人；只预登记「我」）
   │     └─ 每个槽位说够 3 秒 → WeSpeaker 声纹特征（256 维）→ 与「我」和全部熟人比余弦距离 → 槽位 = 名字
   └─ 16 kHz 环形缓冲（最近 4 分钟，存原声 / 取熟人样本用）
→ 词段按说话人合句 →「我：… / 小王：… / 说话人3：…」
→ 停顿 12 秒 / 满 120 秒 / 满 1500 字切一个窗口
→ POST 服务器 /api/ambient/judge（设备令牌 Bearer）
→ jev_judge.predict：meaningful / danger / question / record 四题一次问完
→ 动作：danger_record（App 持续录音 5 分钟 + 通知）/ danger_watch（通知）/ save_audio（App 存本窗 WAV）
        save_transcript、note_task → Obsidian「AI助手专用/环境旁听/日期.md」
        route_question → 第二层 jev（knowledge / personal / unclear）→ knowledge 交 ai_client 写解答
        有意义片段攒 6 段或 1 小时 → AI 写滚动摘要 state/ambient/context.json → MCP voice_brief 带上
```

## 文件

| 位置 | 作用 |
|---|---|
| `_server_deploy/jev_judge.py` | **独立 jev 模块**：状态文本 + 选择题 → 概率。不依赖语音进程；密钥 `JEV_KEY_FILE` 或 `~/Desktop/jev api.txt` |
| `_server_deploy/ambient_jev.py` | `/api/ambient/judge·feed·context`；落盘 `state/ambient/{log,feed}.jsonl`、`context.json` |
| `ios/BWReader/App/NativeAmbientListener.swift` | 旁听外壳（MainActor，开关/来源切换/送判断/执行动作）+ 管线 |
| `ios/BWReader/App/NativeAmbientSupport.swift` | 日志出口、服务器请求、Sortformer 模型、**声纹特征比对 NativeSpeakerEmbedder**、流式分离、声纹与熟人、重采样 |
| `ios/BWReader/App/NativeNoisyVoiceGate.swift` | 通话上行闸门（多人才介入，只放行「我」） |
| `ios/BWReader/App/NativeAmbientSettingsView.swift` | 设置 →「旁听与降噪」分页 |
| `tests/test_ambient_jev.py` | 服务端判断 → 动作 → 落盘（jev / AI 打桩） |

## 必须知道的几件事

- **任何时候都工作**：旁听与语音无关。通话开始时麦克风归通话，旁听改接
  `NativeAudioEngine.microphoneTap`（回声消除后的上行，AI 的声音已被消掉），通话结束换回自己的引擎。
  ⚠ 通话开始时停旁听**不能** `setActive(false)` —— 音频会话全 App 共用，会把通话刚激活的会话一起关掉
  （`stop(releaseSession:)`）。
- **闸门只在多人时介入**：最近 15 秒 ≥2 人各说 ≥1 秒 → 隔离（0.6 秒延迟线，非「我」静音，
  分离结果没覆盖到的时刻放行）；30 秒只剩一人且用户没在说话 → 退出。系统「人声突显」只能用户在控制中心切，
  App 只能弹面板（每通电话一次）。闸门的音频队列只读模型队列拍的快照，不碰分离器本身（数据竞争）。
- **熟人 = 声纹特征比对**（用户「直接做成声纹特征比对」）：分离器不预占熟人槽位。每个槽位定稿语音够 3 秒，
  取它最近 ≤10 秒算 WeSpeaker 嵌入（`DiarizerManager.extractSpeakerEmbedding`），与「我」和**全部**熟人比
  `1 − cos`：最近距离 < 0.55 且比次近至少近 0.08 才认；再多说 10 秒复核一次（最多 3 次），没认出时保留已有结论。
  熟人存的是原声样本（≤30 秒），特征向量按需算、按 updatedAt 缓存 —— 换嵌入模型不用重新起名。
  阈值是起点：每次比对的最近 / 次近距离都进日志，照实测再调（`NativeSpeakerEmbedder.matchDistance / ambiguityMargin`）。
  起名：设置里「最近听到的人」→ 起名，从环形缓冲取他 ≥3 秒有效语音保存，当前槽位立即按名字标注。
- **原声不出设备**：只有转写文字发服务器；不支持本机识别的语言直接不启动（不退回苹果服务器识别）。
- **模型不打包**：Sortformer 与声纹嵌入模型（pyannote 分段 + WeSpeaker）首次使用时从 HuggingFace 下载，之后本机缓存。
- **危险录影没做**：iOS 不允许后台用摄像头，危险只做录音。
- **路由**：App 打的是 `https://<服务器>/api/ambient/*`。`/api/tokens` 已经走 tailscale serve → Flask，
  如果旁听判断报 `HTTP 404 page not found`（纯文本）= serve 没转发 `/api/ambient`，要补一条路由。

## 诊断

- iPad：设置 →「旁听与降噪」→ 日志（同时送 client-log，`surface=native-ambient`）。
- 服务器：`state/ambient/log.jsonl`（每次判断、路由、解答、摘要的成败）。

## 未做

手表 / 眼镜接入；通话降噪仍靠分离器里的「我」槽位（没用嵌入复核）；把旁听摘要主动注入 CLI 语音的 jev 上下文（现在只经 MCP `voice_brief`）；
服务端「第二层」目前只分三类，personal 类只记待跟进、不自动执行。
