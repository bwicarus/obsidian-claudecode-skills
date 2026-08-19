# iOS Safari Web Extension + 原生容器能力地图(2026-07 调研定稿)

> 背景:bw-reader-webext(`extensions/bw-reader-webext/`)即将 TestFlight 上 iPad。
> 本文是 5 路并行调研(developer.apple.com / webkit.org 一手来源优先)的落地结论,
> 指导「本地 PDF」「语音直连」「扩展架构」三大决策。标 ⚠ 的是真机必验项。

## 一、本地 PDF 存哪层(⚠ 原定案「主屏 PWA 为主层」已作废:PWA 阅读器页面整体退役返 410,阅读器改由 iOS App 本地渲染的 ReaderBundle 承载;下表只当 iOS 各存储层的平台事实速查,别当选型结论)

| 层 | 配额 | 清除风险 | 判定 |
|---|---|---|---|
| **主屏 PWA(我们的阅读器加主屏)** | Safari 17+ 按磁盘算:单 origin 60%/全局 80%,整书库绰绰有余 | **七天不交互清除官方豁免**(主屏 web app 不属 Safari);`navigator.storage.persist()` 对主屏 app 更易授予→豁免 LRU 逐出 | ✅ **主层**。iOS 26 起加主屏零门槛即成 web app(无 manifest 要求) |
| 纯 Safari 标签页 | 同上 | ❌ ITP 七天不交互删全部 script-writable storage | 只当临时缓存 |
| 扩展 storage.local | 官方 10MB,unlimitedStorage 后引擎无上限,**但** iOS 18 有实锤 ~3MB regression(Apple 认账"not intentional",修复 ship 版本无通报)+ JSON/base64 + O(n) 写入劣化 | 清 Safari 历史**连坐**扩展存储(实测);SQLite 独立实现推断免 ITP(无官方保证) | ❌ 只放 token/设置/进度 |
| 扩展页 IndexedDB/OPFS | 实测 ~1.5GB 硬顶;⚠ 扩展 origin UUID 历史上逐会话重生成(存了也可能失联) | 同连坐 | ❌ 不押大文件 |
| **原生容器 App(App Group/Documents)** | 无 web 配额,仅设备空间 | **零系统主动清除**(Offload 也保留 data) | ✅ 后备/升级层。经 `sendNativeMessage`→`beginRequest` 中转喂扩展(≤64MB/条,分块;⚠吞吐待真机实测) |

**主屏 PWA 实施要点**:字节存 OPFS(iOS 15.2+,写文件须 Worker+`createSyncAccessHandle`;`createWritable` 要 Safari 26)或 Cache Storage;开机调 `persist()` 并在 UI 显示 `estimate()/persisted()` 状态;**Safari 与主屏 PWA 存储不互通**(加主屏只拷 cookies)。App 安装形态以本机书库和 sidecar 为 source of truth；Pi 只做显式同步/备份，不能成为打开本机书的前置。
**原生层红线**:用户经 document picker 授权的目录 bookmark **在扩展里 resolve 不了**(iOS 无 .withSecurityScope);但**容器 App 自己 resolve 得了**——现役实现就是存 bookmark 后 `startAccessingSecurityScopedResource()` **原位读**(`ios/BWReader/App/ReaderLocalLibrary.swift:181-210`,本机书标识 `localbook:`),**并没有**把文件物理搬进 App Group;iOS 容器 App 不能主动推消息给扩展 JS(macOS-only),只能扩展拉。

## 二、语音直连矩阵(修正版)

| 链路 | 直连可行? | 依据 |
|---|---|---|
| OpenAI Realtime(WebRTC) | ✅ App/扩展原生进程用共享 Keychain 铸 ephemeral token → 浏览器 WebRTC 直连 | App 与 Safari 不依赖 Pi |
| 豆包 S2S(wss) | ❌ 死路:鉴权=WS 握手自定义 header,浏览器 WebSocket 发不了 header(relay 文件头注释早有此结论) | Pi relay 保留 |
| 火山 veRTC 对话式 AI | ✅ 火山系唯一官方浏览器路径(Web SDK WebRTC 进房,Pi 只签 token) | 产品线迁移,单独评估 |
| Google STT 流式 | ❌ gRPC-only,浏览器无 bidi 流 | 走 Pi |
| Google STT 非流式 REST | ✅ 实测 CORS 全放开(any origin)+ API key | 60s/10MB 上限、key 暴露风险 → 只做兜底 |
| Web Speech(Siri 引擎) | ✅ 本项目 iPad 生产环境在用(主路) | Safari 26 起仅 https;flaky 护栏见 memory voice-assistant-arch |

**扩展内语音铁律**:一切语音状态(getUserMedia/WS/WebRTC/SpeechRecognition)放 **content script/页面上下文**;popup 是瞬态 sheet、background 是短命进程,都不可承载;锁屏/切后台即静音+冻结,语音会话实质要求亮屏前台。MediaRecorder 只出 audio/mp4(AAC)(Safari 26 加 ALAC/PCM),Google STT 不吃 AAC → 要 PCM 走 AudioWorklet。

## 三、扩展工程红线(已改/待办)

- ✅ **background 双 key**:`service_worker`(Chrome)+ `scripts`+`persistent:false`(Safari/iOS)并存——iOS 17.4-18.6 实锤 MV3 SW 永眠 bug(杀后唤不醒),MV2 风格可正常按需唤醒。已改 manifest。
- **downloads API 不存在**(Safari 全平台无此 API)→ "保存文件"用页面侧 Blob+`<a download>`(用户手势)或原生容器。
- 消息上限 64MB/条;iOS 页面内存墙 ~100-200MB(超了直接杀无回调)→ 大文件按页/块流转,用后即弃。
- `beginRequest` 真机 ~5% 挂起 → JS 侧超时+重试,handler 幂等。
- ⚠ **已作废**:高亮/笔记/词组/阅读位置的权威在 **App 本地**(`scripts/where_does_this_route_run.py /pdf/api/highlights` → owner=local + 有本地分支),Pi 只在同步中继时经手,不能假设「所有重要状态可从 Pi 重建」。扩展侧的 token/设置仍应当可丢缓存对待(清历史连坐/schema 迁移事故都发生过)。
- 审核:容器 App 别做纯空壳(4.2 最低功能),放书库管理/设置页即可;TestFlight 阶段无碍。
- ⚠ 真机首验清单:①扩展 origin 跨 Safari 重启稳定性;②unlimitedStorage 实际生效量;③清历史/放置7天后 storage.local 与 IDB 存活;④OPFS 扩展页内可用性;⑤native messaging 吞吐。

## 四、总架构分工(用户拍板 2026-07-20)

- **语音(纯 API)**:App/Safari 用共享 Keychain 由原生进程铸短期 token，再直连 OpenAI Realtime；
  页面/选区/笔迹/合成图/本机笔记均不经 Pi。没有 App 原生桥的客户端才使用兼容 relay。
- **CLI/API 型 AI**:制卡、联网搜索、深度思考、后台任务、造纸和长文生成可按需访问 Pi；它们
  是可选工具而非 App 前置。能在 App 内完成的笔记、书籍状态、设置与修改不得走这个通道。
- **PDF 本地化**(路线已换):不走 PWA 离线化,而是 `ios/BWReader/package_local_reader.py` 把阅读器烤成 ReaderBundle 随 App 包发(运行时零下载,环回 `127.0.0.1` 供壳),本机书走 `localbook:` + security-scoped bookmark 原位读;扩展不掺和(Safari 原生 PDF 预览不跑 content script,本地书必须经自家阅读器打开)。
- **扩展**:学习功能透镜(查词/翻译/助手/制卡)。⚠ 「重活全甩 Pi」已不成立——Safari 扩展与 App 同包、经 native messaging 拿 App 的能力,与 PC 的联系也走 App;Pi 只负责「把 Claude/Codex CLI 抽象成 AI 的 HTTP API」和设备间同步中继这两件事。
