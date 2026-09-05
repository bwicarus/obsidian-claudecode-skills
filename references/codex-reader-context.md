# Codex Reader Context

> 快速入口，更新时间：2026-07-27 05:19 JST。本文只收录已由代码、合同测试或发布记录证实的
> 事实，不替代原始规格。先读本文和
> [当前协作状态](reader-collaboration-status.md)；涉及裁决、发布或协议变更时按文末索引回看
> 原文。

## 1. 当前快照

> ⚠ **版本号不写死在本文里**——写死必然过期（此处曾长期停在 `0.2.58`，而工作区已到
> `0.2.69`，误导了后续接手者）。**每次开工现场查真值**：

```bash
python3 extensions/bw-reader-webext/handoff_check.py               # 工作区版本 + 能力归属 + errors
python3 extensions/bw-reader-webext/handoff_check.py --production  # 再比对 Pi 生产与测试渠道
tail -n 160 references/reader-collaboration-status.md              # 最新 owner / 冻结范围 / 发布事实
git -C . log --oneline -5
```

结构性事实（这些不随版本变）：

- 工作区 manifest 版本、生产 PWA/服务端 runtime 版本、扩展测试 channel 版本**是三个独立的数**，
  可以互不相同。**不能把不同版本的扩展与 PWA 混合后判定"接管通过"。**
- 本地 channel 元数据可以指向较新候选；**它不等于生产 channel 已激活**。
- 未发布的中间候选不得覆盖或激活；判断哪些属于此类看共享状态的发布登记。
- 最新 owner、冻结范围、发布事实和下一项**一律以**
  [共享状态](reader-collaboration-status.md) 为准，不以本文、聊天记录或记忆为准。
- 工作区长期脏且含未跟踪文件（多 agent 共享检出 + 每晚 daily 重写 `anki/records`、
  `dashboard.json`）。候选由文件摘要和测试证明，**不等于一个可由 Git commit 单独重建的发布点**。
- 2026-07-29 起 Windows 是第二份工作副本，Pi 仍是唯一部署源；跨机规则见
  [跨机开发](cross-machine-dev-setup.md)。

## 2. 产品与所有权

正式客户端只有本地优先 iOS App 与浏览器扩展；**PWA 阅读器界面已于 2026-08-14 退役**（`_server_deploy/reader_pwa_retirement.py` 让 `/pdf/`、`/pdf/search`、`/pdf/epub/view`、`/pdf/fav/view` 返回 410），下面几条关于 PWA 的分档只作历史参考：

- iOS App：安装包内置同一 Reader renderer/共享组件；Swift 拥有本机文件、数据、生命周期与
  系统能力，Pi 仅作显式同步、备份和联网服务，App 内不运行 PWA/扩展接管协议。
- 当前产品开发优先级是 iOS App 与浏览器扩展；Pi PWA 保留既有可用能力与兼容边界，但暂停新增
  功能。除非用户另行恢复该主线，不得把 App/扩展改动附带部署到 Pi PWA。
- 普通网页无扩展：无 BW 功能。
- 普通网页有扩展：扩展提供全部网页阅读功能。
- ~~真书 PWA 无扩展：PWA 提供完整 fallback。~~ ⚠ **已失效**：PWA 阅读器页面 2026-08-14 起返回 410（`_server_deploy/reader_pwa_retirement.py` 的 `RETIRED_PAGE_ENDPOINTS`：`/pdf/`、`/pdf/search`、`/pdf/epub/view`、`/pdf/fav/view`，由 `pdf_reader.py` 在 before_request 最前面拦），没有 PWA fallback。两处**没有**退役、仍在服务：`/pdf/api/*`（App 与扩展都在调）与 `/pdf/html/view?file=<vault 里的 .md/.html>`（App 书库只认 pdf/epub、扩展不开 vault 文件，一并退役等于把这类书删掉——见该文件里那段注释）。
- 真书 PWA 有扩展：扩展是唯一共享 UI/网络/通用数据 owner；PWA 保留原 renderer、
  `DocumentHost`、私有 anchor 和书籍数据。

真书只有 PDF、EPUB、导入 HTML/Markdown、收藏书。PWA 任意网页解析/proxy/RBI 已退役。
功能冲突先登记，不能为统一源码删掉现有差异。

源码所有权：

| 领域 | 唯一入口 |
|---|---|
| 视觉令牌/共享组件 | `_server_deploy/static/pdf/rc-ui.js`、`rc-*.js` |
| 共享运行时合同 | `_server_deploy/static/reader-runtime/*.js` |
| 扩展宿主/adapter | `extensions/bw-reader-webext/src/*.js` |
| iOS App 本地宿主 | `ios/BWReader/App/*.swift`、`_server_deploy/static/pdf/native-local-runtime.js` |
| PWA 真书宿主 | `_server_deploy/static/reader-runtime/book-host.js` |
| PWA 接管桥 | `_server_deploy/static/pdf/pwa-extension-bridge.js` |
| 后端路由/助手 | `_server_deploy/pdf_reader.py`、`assistant.py`、`voice.py` |

扩展 `vendor/` 只是 `build.py` 的生成物。
PDF `reader.js` 也是生成物，唯一源码在 `_server_deploy/static/pdf/reader.src/*.js`。

## 3. 已证实的核心协议

- 真书接管：`book-host/1` + `bw-reader-pwa/1`；精确四路由、`HELLO` 后初始化 Shell，
  `TAKEOVER` 成功后才隐藏 PWA 重复 UI；5 秒心跳、15 秒租约，断开后恢复 fallback。
- 通用数据：`DataRegistry` 是同步白名单；目前跨设备白名单只有 `user-settings` 与
  `vocabulary-state`。
- 同步：`sync-v3` + `record-parent-state/1` + `sync-gateway/2`；真实分叉显式 conflict，
  父状态、账户、registry digest 或 owner lease 不一致时 fail closed。
- 直连：WebRTC 只加速变化传输，服务端 relay 仍是持久备份；内容宿主只拿不透明
  `accountProof`，不得取得 namespace/Bearer/owner token。
- 普通网页墨迹：仅当前标签页会话；响应式正文宽度变化保留已提交笔迹、仅取消尚未完成的
  当前笔画，刷新或关闭标签页后清空，不把坐标误当长期内容锚。

深层同步、租约、BFCache、v2 因果迁移和 KG 规则不要从本文推导，必须回看
[统一架构](reader-runtime-architecture.md)。

## 4. Anki 实体卡与复习模式

唯一职责划分：

- `rc-voicecall.js`：视觉外壳、拖放、收藏、长按选择；
- `rc-flashcard.js`：正反面、四级评分、卡片状态与快照；
- `rc-review.js`：复习队列、提交、outbox、拒绝恢复；
- `rc-stickynote.js` / `src/web-pins.js`：PWA/Web placement。

不变量：

- 学习卡保持 `id === cid === gid`；页面 placement 使用另一个位置 ID。
- 侧栏、收藏夹、AI 结果、复习区和页面钉住是同一实体的不同投影。
- 来源、原因、note/card/entity ID 只可在某个投影中隐藏，不能从实体快照删除。
- 整卡长按选择导出 `anki-card-context/1`；同 ID 的所有投影同步高亮，整卡覆盖内部段落时
  registry 只导出最大节点。
- 标准 Anki 流程是先正面、再翻面、再四级评分；不能重新变成正反面同时显示。
- Reader 卡片确认与评分先写本地卡仓；Pi 只同步，ReaderPC AnkiConnect 与 AnkiMobile 只是
  可选导出。外部添加使用 at-most-once receipt；结果未知保持 pending，禁止盲重试。

详细行为和历史决定见 [复习合同](card-review-integration.md) 与
[Anki 卡片格式](anki-card-format.md)。

## 5. Codex、CLI、MCP 与桥接

- 阅读器文字助手主路是同一 Codex CLI 启动的常驻 `codex app-server`，用 thread ID 多轮续接；
  `codex exec` 是 app-server 未产出内容时的独立 fallback，不应把每轮历史重新拼回 system
  prompt。
- `_server_deploy/tool_registry.py` 是生产工具表的唯一合同；`assistant.py` 与 `voice.py`
  通过命名 surface/namespace 消费它。新增工具或改工具集必须补
  `tests/test_tool_registry_production.py` 与缓存合同，避免每轮重建工具表破坏前缀缓存。
- 卡片改进旧入口与复习模式必须调用同一个 card-improvement runtime；旧页面只是另一入口，
  不能保留另一套模型/单轮实现。
- 模型是否可用以实际 app-server 账户目录为准，不能用硬编码“账号不支持”；Fast 开关只在
  目录明确声明相应服务层时出现。
- `_server_deploy/mcp_server.py` 是薄门面，业务逻辑仍在应用 API/领域服务。MCP 客户端门禁与
  MCP→webapp 身份 token 职责不同，任何凭据都不得写进本文、页面或日志。
- Windows `snapshot-mcp` 是独立实验末端，不是 Pi `mcp_server.py` 的缓存：Pi 继续提供
  active/journal，PWA 经电脑语音的固定 WSS 直连更新 Windows 本地快照，客户端只通过
  `reader_context_snapshot` 按需读取。网页来源把“视口前文 / 当前可见正文 / 视口后文”分开，
  当前可见部分是结构化字段；完整网页正文只在一个 Codex 线程首次读取该文档版本时返回，
  后续只更新视口，避免重复灌满上下文。`reader_visual_image` 只在 AI 明确调用时向当前
  source/revision 请求视口、笔迹附近或闭合选区附近的“正文＋笔迹＋卡片/便签”JPEG；
  `reader_browser_control` 只允许前后滚动一屏及滚到可见文字、标题或自定义选区，禁止任意
  URL、CSS selector 和脚本。三者与
  旧 voice-typist 注入互斥。入口与回滚合同见
  [电脑直连音频桥](reader-computer-audio-bridge.md#实验上下文末端2026-07-30)。
- Windows `snapshot-mcp` 同时公布 `reader_capability_guide` 与按功能拆分的只读资源。普通快照、
  取图、滚动或输出直接调用对应工具，不先读文档；复杂研究、交互纸、报告核实或已保存任务只
  读取一个精确 topic，确实不知道 topic 时才读索引。结构化写回统一使用 `reader_command` 的
  `BWREADER/1 <kind> <JSON>`，只投递给最新快照精确指向的 App/扩展实例；Windows 电脑语音的
  聊天轮次由本机历史同步器进入既有 Reader 对话流。Windows 原生路径由当前 Codex 会话使用
  Skill、MCP、原生工具与必要的原生子代理组织任务，不再额外启动 CLI worker；现有 Realtime
  与旧 CLI 实现完整保留为兼容入口，不修改其调用方式、工具循环或委托语义。
- 内容脚本和页面只能调用固定 operation/schema 的桥；不得退化成任意
  URL/method/body fetch proxy。
- `reader_bridge.py` 的 `open_page` 当前只写续读位置，不会驱动已经打开的页面实时跳转；
  精确读值/翻页/高亮仍走 MCP 或真实 reader action。
- `RC.execRemote` 仍保留 `window[fn]` fallback，是待收紧的白名单安全欠账；新功能不得继续
  扩大该入口。
- `mcp-server.md` 中固定工具数量和 `CLAUDE.md` 的 8765 端口是历史信息。当前工具目录以
  `tool_registry.py`/生产测试为准；systemd MCP HTTP 端口是 8766，8765 是 AnkiConnect，
  探活必须验证响应体而不只看状态码。

协议、schema 探针和已知 CLI 陷阱见 [Codex 集成](codex-integration.md)；
MCP/OAuth/端口与 smoke 见 [MCP 服务](mcp-server.md)。

## 6. 当前验证证据与可靠性边界

不要在本文抄写候选版本、测试计数或固定哈希；它们会过期。现场证据用：

```bash
python3 extensions/bw-reader-webext/handoff_check.py
node --test tests/reader_contract/*.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
```

当前候选、人工验收、发布、回滚及真实设备边界只看
[当前协作状态](reader-collaboration-status.md) 的最新登记和
[扩展交接](reader-extension-handoff.md) 的现场命令。自动测试不能替代 Surface Pen、
双真实设备、registry 跨代迁移或需验收档的用户视觉与交互门禁。

这些边界不得写成“已完成”。

## 7. 常用安全命令

```bash
git status --short
python3 extensions/bw-reader-webext/build.py
node --test --test-reporter=spec tests/reader_contract/*.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
python3 extensions/bw-reader-webext/handoff_check.py
python3 extensions/bw-reader-webext/test_release_pipeline.py
python3 extensions/bw-reader-webext/release_preflight.py \
  --artifact extensions/bw-reader-webext-X.Y.Z-windows-test.zip \
  --skip-browser
```

浏览器测试按改动范围选择 `extensions/bw-reader-webext/test_*.py`。共享运行时改动后先 build，
再测生成后的扩展。禁止手改 `vendor/`。`release_preflight.py` 强制要求实际 `--artifact`；
上面的 `--skip-browser` 只是开发期元数据检查，不是发布批准。`publish_test_channel.py` 即使
没有 `--deploy` 也会改本地不可变包/channel，不能当只读预检。

部署不是普通验证命令。只有可复现候选、用户整套人工视觉验收、agent 后台证据、回滚点和单一
发布 owner 全部明确后，才能运行项目原子流程；独立浏览器自动化只能作为工程回归，不能替代
用户人工部署验收。

**怎么部署看 [部署流程](deployment-workflow.md)（唯一权威）**，一句话：先用
`python3 scripts/reader_deploy_manifest.py | cut -f1 | grep -F '<文件>'` 判断在不在清单里，
在清单内走 `scripts/deploy_reader.sh`（先 `--preflight-only`），清单外才 `cp` + restart。
**脚本内建摘要校验、原子安装、失败自动回滚和健康检查——不要手工再核一遍**；
[扩展交接 §11](reader-extension-handoff.md#11-部署门禁) 的文件列表只是重点提示，
漏没漏以 manifest 为准。

## 8. 原始资料索引

| 需要裁决的主题 | 回看原文 |
|---|---|
| 产品边界、DocumentHost、存储、sync/KG | [reader-runtime-architecture.md](reader-runtime-architecture.md) |
| PWA/扩展能力与数据归属 | [reader-extension-ownership.md](reader-extension-ownership.md) |
| 最新版本、Windows、测试、部署/回滚 | [reader-extension-handoff.md](reader-extension-handoff.md) |
| 当前 owner、现场与验证事实 | [reader-collaboration-status.md](reader-collaboration-status.md) |
| 尚未裁决的功能/视觉差异 | [reader-runtime-conflicts.md](reader-runtime-conflicts.md)、[reader-ui-conflicts.md](reader-ui-conflicts.md) |
| 已拍板的下一阶段顺序 | [reader-next-stage-decisions-2026-07-25.md](reader-next-stage-decisions-2026-07-25.md) |
| 复习、卡片、来源 | [card-review-integration.md](card-review-integration.md)、[anki-card-format.md](anki-card-format.md) |
| Codex CLI/app-server | [codex-integration.md](codex-integration.md) |
| MCP/OAuth/桥 | [mcp-server.md](mcp-server.md) |

Obsidian/Anki 自动化不是 Reader 运行时的一部分；相关工作再读根目录 `AGENTS.md` 的笔记章节与
对应 `.claude/skills/*.md`，不要把 Reader 的浏览器合同套进笔记批处理。

版本号、模型名、MCP 工具数量和历史“待修/已完成”段最容易陈旧：
`card-review-integration.md`/`anki-card-format.md` 主要提供用户意图与格式，当前行为以最新
共享状态、`rc-*` 实现和合同测试为准；Codex 模型/Fast tier 必须以运行时 `model/list` 为准。

## 9. 钉住的快照（2026-09-05）

**问题**：快照是持续覆盖的状态，命令是一个时刻。模型哪一刻去读，读到的就是那一刻 —— 用户说完话
之后翻页、改选区，模型看到的就不再是他说话时看的那页；用户也无法判断"它读到目标了没"，于是
AI 干活时不敢碰 App。

**用户定的形状**："不断固定我停止说话时刻的快照，AI 查看时看到的就是最后一次被固定的快照"；
打字发出的命令同理，在发出那一刻固定。要求方案对音频通道的变化不敏感。

**实现**（桥 0.1.287 起）：

- **一个原语**：`IDirectSnapshotContextAdapter.PinAsync(reason)` 把此刻的快照另存为
  `reader-context-pinned.json`（与主快照同目录，文件名只在 `FileDirectSnapshotContextAdapter.PinnedFileName`
  一处定义，MCP 用 `PinnedPathFor` 算路径）。不改主快照、不动 revision；副本带
  `pinned:{at, reason, revision}`。
- **触发一：说完话**。`DirectBridgeCoordinator.PushUplinkFrameAsync` 把设备麦克风的每一帧喂给
  `UplinkSpeechEndDetector`（纯能量法：自适应噪底，连续 120 ms 有声算开口，连续 700 ms 无声算说完，
  短于 300 ms 不算话），判定为说完就 `PinAsync("speech-end")`。判定放在上行帧这一层，音频从哪个
  通道进来都一样 —— 通道换了，只要还把帧推到协调器，这条就照常工作；Codex 语音模式自己的端点判定
  桥看不到，也不需要看到。
- **触发二：HTTP 口**。`POST /reader-context/snapshot` 带 `{"pin":{"reason":"text-send"}}`
  （只此一个字段，不与快照字段混发）。App 里打字发送、将来任何新通道，都调这一个口。
  reason 限 40 字以内的 `[A-Za-z0-9_-]`。
- **读取**：MCP `reader_context_snapshot` 的 payload 顶层多了 `basis`：
  `pinned` = 主体就是钉住那一版，新鲜度按**钉住时刻**算（否则两分钟前的页会被标 stale，模型不敢用），
  `pinned.ageSec` 说命令距今多久，`live.changed` 列钉住之后变了什么（页、选区）；
  `live` = 没有 5 分钟内的钉（`PinnedWindow`），主体是刚读到的实时版，`pinned` 为 null 或带 `expired:true`。
  工具描述里明说：this/here/选区按钉住的那版解，用户明说"现在"才看实时。

**边界**（如实写）：

- 在 Codex 桌面端直接打字的命令，桥看不到发出时刻，没有钉 → `basis=live`，模型第一次来读的那一刻
  就是它拿到的状态。人在电脑前打字时一般不在碰 iPad，实测偏差以秒计。
- 说完才画圈这种顺序颠倒的情况，`live.changed` 会报"selection changed"，指南要求指代词优先用它。
- 用户侧的"已交给 AI"回显还没做：HTTP 口的响应带了 `revision`，App 显示一行是下一步。

自检：`DirectBridgeSelfTest` 的 `speech-end-detector-*` / `speech-end-pins-*` / `snapshot-pin-*`；
契约：`tests/reader_contract/snapshot-pin.contract.test.mjs`。

### 9a. 双工诊断（2026-09-05，用户："AI 在说话时很难打断它，好像它说话时听不到我"）

先给数字再下结论。桥在上行帧上顺手数四个数：`uplinkFrames` / `uplinkVoicedFrames`（总帧数与有声帧数）、
`uplinkFramesDuringOutput` / `uplinkVoicedFramesDuringOutput`（AI 最近 300 ms 内出过声时的帧数与有声帧数），
外加噪底、最近一帧 RMS、钉住次数。看法：

```
curl -s https://bwicarus-2.taile44d0c.ts.net/reader-context/snapshot
```

判读：AI 出声期间你说了话，`uplinkVoicedFramesDuringOutput` 若跟着涨 → 你的声音到了桥，是 Codex 那头
没理（它自己的语音模式在出声时不听，桥管不着）；若几乎不涨 → 声音在到桥之前就没了，最可能是 iPad 的
系统语音处理（`.voiceChat` + `setVoiceProcessingEnabled(true)`）在外放时做的双讲抑制 —— 远端在讲，
近端被压。这一条戴耳机就能验：耳机没有声学回声，抑制基本消失。

### 9b. App 原生语音（relay agent 模式）下的钉住（2026-09-06）

用户问："现在可以使用 App 原生的语音对话了，不需要桥接也不需要电脑上进行语音，那快照固定怎么办？"
答：那条路**天然就是钉住的**，不需要桥上的 pinned 文件。链路是 App → `wss://…/voice-rt?mode=agent`
（Windows 上的 relay 只做 ASR/TTS）→ relay 把 utterance 终稿推回 App → 阅读器前端 `rc-voicecall.js`
的 `sendToAssistant` → `rc-assistant.js` 的 `send`，而 `send` 在**那一刻**从页面现取上下文
（`sentCtx`：页码、选区、选区周边句…）随请求发出。也就是说上下文在"说完"到达前端的瞬间被取走，
之后翻页、改选区都不影响这一轮 —— 与桥接语音里的 `basis=pinned` 同一语义，只是钉的动作发生在前端。
两条路各自钉自己的，互不依赖；桥的 pinned 文件只服务 Codex 桌面语音那条。
