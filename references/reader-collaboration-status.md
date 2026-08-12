# 阅读器主线协作状态

本文件记录阅读器主线的当前协作状态与交接事实；产品规则仍以
`reader-extension-handoff.md`、`reader-runtime-architecture.md` 和冲突登记为准。

## 协作约定

- 开始一个新的工作段之前，先读取本文件的当前状态。
- 完成一个工作段、遇到阻塞或移交时，更新本文件的当前状态。

（当前由用户手动选择活跃的 AI，不再维护写入所有权 / scope 登记表。）

## 当前事实

- 0.2.49 Windows 隔离测试已完成；未触碰日常 Chrome profile。
- 文档便签 repository、background port、facade 和共享便签 UI 已接通，定向合同测试通过。
- 已修复独立 document-notes Vault、Safari compat 漏包和 SPA 实时 URL 围栏。
- 0.2.50 普通网页真 Chromium 回归已通过：真实顶栏点击、零旧 notes HTTP、刷新/重启持久化、
  跨标签 CHANGE、同源 SPA 隔离和删除 tombstone。
- 0.2.50 不可变 Windows 包 SHA-256：
  `ee4b14351f2791bcb4163b8f9502c737f11d6d8105f66c2385931b600a3f2e08`。
  Linux 制品门禁和 Windows 解包后同一套隔离浏览器回归均通过；Windows 临时 profile/目录
  已清理，日常 Chrome 未启动、未修改。
- 暂留 `/pdf/api/notes` 兼容通道：旧 AI `notes_create/notes_edit` 仍依赖它；不能在本轮静默删除。

## 最近交接：0.2.50 文档便签本地优先

- 目标：把普通网页便签从旧 HTTP 路径迁到扩展本地、账户/文档隔离且可跨标签同步的唯一仓库。
- 改动范围：document-note repository、background `bw-document-notes/1`、facade、普通网页
  便签适配、共享 `rc-stickynote` repository 接口、Safari/Windows 门禁与对应合同/浏览器测试。
- 验证：runtime 合同 307/307；IndexedDB 40 项；文字命中 6 项；网络审计 0 新债务；
  handoff/release preflight 通过；Linux 与 Windows 的真实 Chromium 文档便签回归均通过。
- 剩余：线上 PWA runtime 比本地旧，`test_pwa_handoff.py` 以
  `BW_RUNTIME_PROVIDER_REGISTRY` fail closed；部署同版本 runtime 后再跑 PDF/EPUB/HTML/Favorite
  接管回归。
- 风险：PWA/旧 AI 的 notes HTTP fallback 仍存在，下一阶段要先迁移 AI
  `notes_create/notes_edit`，再删除兼容路径。
- 下一负责人：协调任务或下一位接手者；先读主交接，再决定是否部署整套 PWA runtime，
  不得只为通过测试放宽 provider registry。

## Claude 复核记录：0.2.50（2026-07-25，read-only）

- Owner：Claude（协作会话）；范围=review-only，未修改代码/未部署/未取实现 claim/仅临时 profile。
- **核对 ①（版本+SHA）通过**：manifest=0.2.50；Windows ZIP SHA-256 与本文件声明逐字节一致。
  观察：测试渠道 channel JSON 仍为 0.2.47（0.2.50 未发布渠道，与"隔离解包验证"描述一致，非缺陷）。
- **核对 ②（围栏审阅）通过，无阻断缺陷**：账户=按 namespace 物理分库+三重 fencing，切换不泄漏；
  documentId 只信 `sender.tab.url`、payload 三层剥离身份字段、隔离世界注入，页面无法伪造他人文档；
  跨标签 CHANGE 经 rev 幂等去重；tombstone 支配防复活；普通网页零旧 notes HTTP，PWA legacy
  网关限可信源、两路不串；vendor 与共享源逐字一致（仅 build 包裹）。
- **核对 ③（只读门禁重跑）3/3 通过**：`handoff_check.py`(quick) errors=0 READY（含 IndexedDB 40
  断言、文字命中 6、网络审计 0 新债务）；runtime 合同纯 node **308/308 全过**
  （`indexeddb-store.playwright.mjs` 因本机缺 Playwright **npm** 包跳挂＝环境缺依赖非产品缺陷，
  其覆盖由 quick 门禁等价确认；为守只读未安装依赖）；`test_web_notes_local.py` 真 Chromium
  临时 profile **PASS**（reload/restart、跨标签 CHANGE、tombstone、SPA 隔离、宿主点击）。
- **核对 ④**：未混测线上旧 PWA；`BW_RUNTIME_PROVIDER_REGISTRY` 保持 fail-closed 未触碰。
- 发现（非阻断，建议排期）：
  1. [Low-Med] 便签记录/tombstone 无 GC：IndexedDB `list()` 全量扫描后才分页、tombstone 永不
     物理删除，重度使用下 O(N) 增长隐患。
  2. [Low-Med] SPA 检测在无 Navigation API 浏览器（Safari/老 Firefox）且无 DOM 变化时有
     短暂盲点，建议补低频轮询兜底。
  3. [by-design?] hash 路由 SPA 共享同一 documentId（三层一致剥 hash）——需与产品意图确认。
  4. [Info] 导航中 CREATE 断线报 `outcomeUnknown` 但便签可能已落库（最多一次语义边界，可接受）。
- 建议下一 owner：先部署整套同版本 PWA runtime 再跑 PDF/EPUB/HTML/Favorite 接管回归
  （维持 registry fail-closed，不为过测试放宽）；随后排期发现 1/2，确认发现 3 的产品意图；
  下一阶段迁移 AI `notes_create/notes_edit` 后再删 `/pdf/api/notes` 兼容路径。


## Codex 今日工作审计（2026-07-25，Claude 只读重建）

- 证据方式：静态核验（文件存在/mtime/接线 grep/交接文档自述）+ 协议纯函数单测重跑；
  **未重跑今日新增测试套件**（本轮授权为只读检查）。今日全部工作**未提交 git**（工作区 234 项），
  存在易失风险，建议尽快提交快照。
- **已完成（可验证）**：
  1. **网页翻译升级全量接线**（原阶段 2-4 backlog）：`web-translate-upgrade-handoff.md` 重写为
     已接线版（三模式路由、Claude 无工具短时会话+压缩换会话、严格请求 schema、账户分区缓存、
     `/pdf/api/web-trcache` 退役 410、Codex 后端安全降级）。实物证据：`html_reader.py` 经
     `web_translate_protocol_module` 注入消费；WebApp 运行目录存在 `web_translate_protocol.py`
     （唯一源码原子部署映射已执行）；`scripts/reader_deploy_manifest.py`、`scripts/deploy_reader.sh`
     存在；协议单测扩至 23 项、重跑 **ALL PASS**；`tests/test_web_translate_upgrade.py`、
     `test_web_translate_mode_contract.py`、`test_reader_deploy_manifest.py` 今日新建。
  2. **卡片改进统一服务**（决定文档实施顺序 1+2）：`card_improvement_runtime.py` 新建，
     `pdf_reader.py`/`assistant.py` 已接线；provenance/service/api/runtime/action 五个测试今日新建。
  3. **ContextSelectionRegistry 与静态 Prompt Cache 前缀**（顺序 3）：合同测试
     `context-selection-registry.contract.test.mjs` + `test_assistant_static_prompt_cache.py`。
  4. **复习工作区卡片下按钮**（顺序 4，部分）：`rc-review.js` 今日改动、含 6 处改进入口——
     实现痕迹明确，真机验证情况未知。
  5. 其它：web_vocab 扫描调度解耦滚动（scheduler 合同）、see-ink 网页路、下划线刷新合同、
     `tool_registry.py` 本体+缓存合同、release/handoff 门禁更新、0.2.50 打包（已经本文件上节复核）。
- **Backlog 呈现完整性（对照 `reader-next-stage-decisions-2026-07-25.md` 实施顺序 7 项）：不完整**：
  - 顺序 5 **CardCandidateService：缺失**（全仓零命中）；
  - 顺序 7 **自动 KG 建点 + 直接设备同步：缺失**（零证据）；
  - 顺序 6 ToolRegistry：**仅本体+测试**，`assistant.py`/`voice.py` 零引用＝生产迁移未接线；
  - 顺序 1-4 已有产物（4 待真机确认）。
- **剩余/下一步**：提交今日工作快照；跑全量新增测试套件取得执行证据；完成顺序 5/6(迁移)/7；
  翻译线按其交接文档"已知边界"校准阈值；0.2.50 渠道发布与 PWA runtime 部署仍未做（见上节）。

## Codex 工作段：CardCandidateService（2026-07-26）

- **目标与结果**：已完成既定实施顺序 5。新增统一 `CardCandidateService`，将“当前来源直接生成、
  页面 KG、焦点词、材料图谱、到期卡”五路证据合并、去重和排序；关联卡不足时才由到期卡补齐，
  单路发现失败会降级而不会使复习队列整体失效。
- **接线范围**：`/pdf/api/review-queue` 保留 GET 到期队列兼容，同时新增 POST 上下文候选接口；
  普通网页/PWA 复习 UI 使用同一 `rc-review.js`。原文选择和可见正文只放 POST body，不进入 URL；
  扩展缓存通过 background 按已验证账户物理分区，PWA 继续使用本地 fallback。
- **验证结果**：
  - 候选服务、路由、卡片来源及改进动作 Python 回归：21/21；
  - 交互策略、扩展 provider、候选 UI Node 回归：50/50；
  - 网络审计：111 个文件、254 个调用、0 新债务；
  - 本地现有 LADR/Anki/KG 数据只读集成与真实本地 Anki POST 路由通过；
  - Linux `xvfb` 与 Windows 10 隔离 Chromium 均通过真实扩展回归：POST、账户隔离缓存、
    精确上下文缓存复用及宿主网页点击均正常；
  - `handoff_check.py`：errors=0、READY；唯一 warning 为既有脏工作区。
- **安全边界**：未部署，未改日常 Chrome；Windows 仅使用 Playwright Chromium 和随机临时
  profile，测试目录及测试进程已清理。测试未调用 Anki 排程写操作。
- **仍待处理**：线上 PWA runtime 仍旧于本地版本，继续保持 provider-registry fail-closed。
  按既定顺序，下一项是 6：把现有 `ToolRegistry` 从“本体+合同测试”迁入
  `assistant.py`/`voice.py` 的生产工具调用；之后才是 7（自动 KG 建点与设备直连同步）。

## Codex 工作段：ToolRegistry 生产迁移（2026-07-26）

- **目标与结果**：既定实施顺序 6 已完成。现有 52 个 handler 能力全部保留，按 9 个 namespace
  冻结为唯一、确定性排序的生产目录；catalog version=`12b2ec32c9267fb4`。没有启用会隐藏旧能力的
  强制渐进加载，也没有在缺少可靠模式状态时擅自新增 review/host gate。
- **生产接线**：
  - `assistant.py` 的 Claude/Gemini/Codex 文字编排、`/api/assistant/tools`、MCP
    `/tool`、语音 `/voice-tool` 和 WebRTC 直连都消费同一 registry；执行统一先过可信
    surface gate，再进入兼容 handler。
  - `voice.py` 的 CLI 工作者读取同一 catalog version/namespace，并仍通过
    `assistant_tools` → `assistant_call_tool` 发现和执行；使用惰性 import，未引入 app 注册循环。
  - `voice_realtime_relay.py` 的 OpenAI/Grok/豆包目录及参数 schema 改为按 surface 拉 registry
    投影，删除 relay 内重复的 schema 和虚拟工具定义。
  - 复合工具按钮、trace 回放和数据源预取也改走 registry 的 internal executor，不再直接绕过
    `TOOLS[name][1]`。
  - 保持原能力集合：assistant_text=49、mcp_worker=52、voice_execute=52、
    rtc_direct=51、realtime_ws=55、doubao_s2s=54。用户自定义 description 只叠加显示，
    不改变目录身份和排序。
- **部署边界**：reader manifest 已把 `tool_registry.py`、`voice.py`、`task_runtime.py` 与
  `assistant.py` 设为同批部署；部署脚本现在 fail-closed 校验 `voice-rt` 的真实源码路径，
  正常激活及 error/signal 回滚都会与 webapp 一起重启并检查 `voice-rt`。本工作段没有实际部署、
  没有重启服务、没有启动或修改日常 Chrome/profile。
- **验证结果**：
  - Python 全套：285/285 通过，7 项因本机无 PDF 或历史恢复通道按设计跳过；
  - ToolRegistry 生产/缓存、surface、防伪调用、Realtime schema、CLI/MCP、复合回放及部署事务
    定向合同通过；Python `py_compile`、`bash -n scripts/deploy_reader.sh` 通过；
  - `handoff_check.py`：errors=0、READY；统一 runtime 30 files、网络审计 0 新债务、
    IndexedDB 40 assertions、文字命中 6 assertions 均通过；
  - `handoff_check.py --full` 唯一错误仍是“本地新 runtime 与未部署旧 PWA 不可混测”，符合本段
    明确的不得部署边界，不是本地代码合同失败。
- **已知风险/未做事项**：
  - 动态垫话策略已不再改工具 JSON/schema，但仍会改变 Realtime `instructions`；策略翻转时完整
    prompt 前缀仍可能 miss cache，不能宣称缓存问题已完全解决。
  - 本地实现尚未在已部署 webapp/voice-rt 上激活或做真实语音冒烟；必须等用户另行授权部署后同批
    验证，不能拿旧 PWA 得出新 runtime 结论。
  - 线上 PWA runtime 旧版本边界及 `/pdf/api/notes` 兼容通道均保持原状；未放宽
    provider-registry fail-closed。
- **下一步**：按既定功能顺序进入 7（自动 KG 建点与设备直连同步）；发布/线上 PWA 与语音冒烟
  作为独立、需用户授权的激活工作，不与顺序 7 的设计擅自混在一起。

## 自动接力启动（2026-07-26 03:06 东京）

- 当前协调器已读取本状态；按既定顺序启动第 7 项本地实现：自动 KG 建点与设备直连同步。仅限本地/隔离测试，不部署、不提交、不推送、不修改日常 Chrome profile。

- 2026-07-26 04:30 JST：夜间协调器读取共享状态及既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中；已见 sync-coordinator 与 PWA lifecycle 定向 Node 合同回归通过，但 Codex 尚未登记完成或阻塞，Claude 仍空闲。未形成可交只读复审的完成段；未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 04:45 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中，最新可见扩展 provider、PWA service bridge 与 popup 定向 Node 合同测试已通过，但尚无完成、完整验证或阻塞记录；Claude 空闲。为避免打断实现，未提前交审或发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 04:50 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中；可见定向 Node 合同与更广的 reader_contract 回归均已通过，但 Codex 尚未登记完成、完整验证或阻塞。Claude 空闲，尚无可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 04:55 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中；最新可见改动涉及同步运行时所有权/断连处理、账户围栏、冲突集编号有界保留及 popup 状态呈现。尚无完成、完整验证或阻塞登记；Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 05:00 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中；最新可见一组扩展 provider、popup、PWA lifecycle、同步冲突控制等定向 Node 合同回归已全过（todo=0），但 Codex 尚未登记完成、完整验证或阻塞。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 05:05 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中；最新可见 Python 全量 unittest 回归通过（OK，skipped=7），Codex 正在继续完善暂停/恢复与所有权追踪，尚未登记完成、完整验证或阻塞。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
﻿- 2026-07-26 05:10 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现中；最新可见为同步冲突确认/变更覆盖逻辑继续完善，先前定向 Node 合同与 Python 全量 unittest（OK，skipped=7）已通过，但尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 05:15 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中：正在处理同步所有权、冲突语义、注册表合同、设置轮询竞态与 PageBrief 异常释放，尚无完成、完整验证或阻塞登记；Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 05:20 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前正在补同步直连握手与竞态的合同测试，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
﻿
- 2026-07-26 05:30 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前可见其正在补充同步错误码脱敏及 PWA/扩展状态呈现合同，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

﻿
- 2026-07-26 05:35 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；本轮可见全量 Node reader_contract 回归已通过（todo=0），Codex 正在补显式冲突的因果证明并准备统一构建/全量验证。尚无完成、完整验证或阻塞登记；Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 05:40 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前可见其正在定义 sync-v3 常量和合同检查，并审视修订号单调性，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 05:45 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；最新可见其正在完成同步协议滚动版本围栏与因果 revision/父状态收敛语义，自动 KG 建点定向回归 16/16 已通过，但尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 05:50 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；本轮定向 Node 同步传输/直连宿主/内容宿主合同回归已通过（todo=0），其正处理 sync-v3 协议滚动版本与因果状态收敛，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 05:55 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前正在核对 sync-coordinator 的测试差异、因果冲突与快照恢复语义，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。﻿
- 2026-07-26 06:00 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前可见其正处理 sync-v3 因果冲突的公开原因映射、KG Windows 跨进程锁与最终构建前审计，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

- 2026-07-26 06:05 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；发现其正在修复“扩展后台已记住账户但 PWA 注入禁用时可能双 owner”的真实所有权漏洞，并继续收紧 owner 激活条件、registry digest pinning 及失败路径合同测试。尚无完成、完整验证或阻塞登记；Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:10 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前正在为扩展与 PWA 的同步 checkpoint 绑定 IndexedDB 稳定代次，以修复 Vault/全局数据库重建后继续旧远端游标而漏历史的风险。尚无完成、完整验证或阻塞登记；Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:15 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前可见正在完善同步 owner lease API 与失效处理，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。- 2026-07-26 06:20 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；本轮可见部署清单与网络审计定向回归通过，网络审计为 121 文件、254 调用、0 新债务。Codex 尚未登记完成、完整验证或阻塞；Claude 空闲，未形成可交只读复审的完成段。为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:25 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前可见正在加强 PWA/扩展配对 deviceFamilyId 绑定、owner lease 销毁与运行时 claim reconciliation，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:30 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；当前可见正在为扩展/PWA owner lease 的 claim、renew、release 直连握手补合同覆盖，最新扩展 provider 定向 Node 回归通过（todo=0），但尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:35 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；最新可见 Node 全量 reader_contract 回归、扩展构建及相关语法检查已运行，Codex 尚未登记完成、完整验证或阻塞。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:40 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍在 Codex 本地实现收口中；已见扩展构建、全量 Node reader_contract、handoff quick 与 Linux xvfb 隔离烟测通过，Codex 正继续修正 owner lease 的本地安全窗口和续租定时边界，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
- 2026-07-26 06:45 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍由 Codex 本地实现收口；当前可见其正准备确定性制品打包及 Windows 隔离测试检查，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。
﻿- 2026-07-26 06:50 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项仍由 Codex 本地实现收口；最新可见扩展定向 Node 合同回归通过（todo=0），扩展构建及 vendor 一致性校验通过，Codex 正继续处理 owner lease 架构与收敛边界，尚无完成、完整验证或阻塞登记。Claude 空闲，未形成可交只读复审的完成段；为避免打断实现，未发送新任务。未部署、提交、推送、使用凭据或修改日常 Chrome profile。

## Codex 工作段：自动 KG 建点与设备直连同步（2026-07-26，本地未发布）

- **目标与结果**：既定实施顺序 7 已完成本地候选。PDF PageBrief 通过逐字 quote/概念证据门禁
  接入唯一 `ConceptNodeService`，使用稳定 node/evidence/mutation 身份、pending 重放及
  Windows/Pi 跨进程写锁。跨设备通用数据固定为 `sync-v3` +
  `record-parent-state/1` + `sync-gateway/2`，服务端始终做耐久备份；设备达到相同
  server baseline 后才允许 WebRTC 直连加速。
- **同步与所有权**：
  - 当前跨设备白名单只有 `user-settings`、`vocabulary-state`；派生缓存、页面几何、
    书籍 placement/墨迹及 PageBrief/KG 不上传。
  - PWA 与扩展使用同一持久 `deviceFamilyId`，服务端 `owner-lease/1` 只互斥同设备 family，
    不误伤真实多设备；PWA handoff 优先。exchange/snapshot/signal 在业务状态之前校验
    device/family/role/instance/generation/token，客户端异步调用前后再次围栏。
  - 本地租约从请求发出时最多保留 29 秒，并同时受墙钟/单调时钟限制；响应延迟、睡眠、
    时钟回退和迟到结果均不能延长写权限。checkpoint 绑定 IndexedDB Vault instance epoch。
  - PWA 关闭/隐藏会先暂停网络 owner 并释放租约；BFCache 恢复等待释放后复用同一 runtime
    重新领取。扩展普通网页直连不依赖同一 PWA 标签页持续打开。
- **主要改动区域**：`scripts/kg/concept_node_service.py`、`_server_deploy/pdf_reader.py`、
  `_server_deploy/reader_sync_relay.py`、`_server_deploy/static/reader-runtime/` 的
  DataRegistry/DataStore/SyncRuntime/直连/owner/PWA 生命周期，扩展 `background.js`、
  `content.js`、manifest/build/Safari 打包与生成的 runtime vendor，以及对应 Python/Node/
  真浏览器合同。`test_smoke.py` 仅增加显式跨平台临时浏览器路径参数，默认 Linux 行为不变。
- **验证结果**：
  - 全量 Node reader contract：442/442；
  - 全量 Python：341/341，按设计跳过 7（本段最后的增量仅为 JS/文档；relay + 真
    Node 客户端到 Flask 集成另复跑 32/32）；
  - owner + PWA + extension 聚焦：98/98；自动 KG 聚焦由最终门禁通过；
  - IndexedDB 真浏览器合同 79 assertions，文字命中 6 assertions，网络审计 0 新债务；
  - Windows/Safari 发布管线 14/14；`handoff_check.py` errors=0、READY，唯一 warning 为
    既有脏工作区；
  - Linux 临时 Chromium 与 Windows 10 Playwright Chromium 均通过完整网页 runtime、
    持久账户网络桥和跨站设置烟测。最终 Windows 临时 ZIP SHA-256 为
    `ff204830c8d7b22f8d6eff878485efaa257066fadc69472fc6f9182002b6039a`；
    测试后隔离 Chromium 残留进程为 0，两端临时包/profile/目录均已删除。
- **安全边界**：没有部署、发布、提交、推送或重启服务；没有启动/修改日常 Chrome profile。
  owner token 不写入页面可读或持久明文存储。
- **已知边界/下一步**：
  - MV3 worker 重启后可能 fail closed 等待旧 owner TTL 再领取；不能为缩短等待而持久化明文
    token。BFCache 的 release 请求若永久不结束，恢复页会保持暂停而不会双写，后续可加有界等待。
  - 已开始的本地存储提交若在内部失租，可能落原 Vault，但不会返回成功；调用方按
    outcome-unknown + mutation/因果冲突处理。
  - registry digest 跨版本迁移、两个真实设备的直连/服务端 fallback 端到端验收、
    PDF 重命名 provenance 与有界 KG ledger 长期重放仍需后续明确实施。
  - 下一负责人应先做只读复核或由用户决定是否部署整套同版本 PWA runtime/发布 0.2.51；
    不得只发布扩展后拿线上旧 PWA 混测，也不得放宽 provider/registry fail-closed。
- 2026-07-26 06:55 JST：夜间协调器复读共享状态并只读核对既有 codex/claude tmux 窗格。Codex 已完成第 7 项本地候选并登记完整验证（Node 442/442、Python 341/341、Node→Flask 32/32、Windows/Safari 管线 14/14、隔离 Chromium 与 handoff quick 均通过）；未部署、提交、推送、重启服务或触碰日常 Chrome。已向既有 Claude 会话发送中文只读审查任务，范围为第 7 项改动及测试证据；等待审查结论后再按结论接力。
- 2026-07-26 07:00 JST：夜间协调器复读共享状态并只读核对既有 codex/claude tmux 窗格。第 7 项本地候选已登记完成并处于 Claude 只读审查中；Claude 已启动同步所有权围栏与 KG/测试证据两项只读审阅。尚无审查结论或新增明确本地待办，为避免干扰未发送新任务。未部署、提交、推送、重启服务、使用凭据或修改日常 Chrome profile。
﻿
- 2026-07-26 07:05 JST：夜间协调器复读共享状态并只读检查既有 codex/claude tmux 窗格。第 7 项本地候选仍处于 Claude 两路只读审查中（同步所有权/围栏、KG/测试证据）；尚无审查结论或新增明确本地待办，未发送新任务。未部署、提交、推送、重启服务、使用凭据或修改日常 Chrome profile。

﻿
- 2026-07-26 07:09 JST：夜间协调器结束本轮轮询。Claude 已完成 KG/测试证据只读核验（无阻断，记录一个低优先级 skip 语义偏差及 Node 计数 +1），同步所有权/围栏深审仍在进行，尚无明确可执行本地待办；为避免干扰未发送新任务。未部署、提交、推送、重启服务、使用凭据或修改日常 Chrome profile。


- 2026-07-26 07:10 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。第 7 项本地候选已完成，Claude 的 KG/测试证据只读核验无阻断；同步所有权/围栏深审仍等待其后台审阅结论，暂无新增明确本地待办。未发送新任务，以免干扰既有审查；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome profile。


## Claude 只读审查：第 7 项 自动 KG 建点与设备直连同步（2026-07-26 07:13 JST）

- **范围与方法**：静态深审（relay/reader-runtime 同步全模块/background/KG 服务）+ 只读与隔离
  测试核验；未修改代码、未部署/提交/推送、未用凭据、未触碰日常 Chrome。
- **结论：发现 1 项阻断级正确性问题（F1），交 Codex 修复后方可发布 0.2.51；其余七个必验面
  （租约双时钟 29s、服务端校验链、同设备互斥/handoff/06:05 漏洞修复、跨账户围栏与 token
  零持久化、checkpoint Vault epoch、直连五关闭条件与无 STUN/TURN、PWA 生命周期）逐条通过，
  与三份声称文档对得上。KG 建点六条声称全部有代码支撑、无阻断。**
- **F1【阻断·正确性】墓碑支配与 record-parent-state/1 承诺互相矛盾，可永久卡死整账户同步**：
  relay `_push_locked` 的 tombstone 分支先于 causal 检查（`reader_sync_relay.py:909-918`），
  head=墓碑 + incoming=put 时**即使父证明与墓碑 head 精确一致**也判 `tombstone-dominates`
  conflict（`data-store.js:893-904` 客户端同构；`tests/test_reader_sync_relay.py:1130-1146`
  已把该行为固化为预期）。而 architecture §7 承诺"父状态精确一致即接受、真实分叉才 conflict"。
  叠加现行机制「conflict → `sync-runtime.js:213` pause('sync-conflict')、`resolveConflict`
  全仓零调用方、`sync-coordinator.js:383-401` 本地游标在未 ack mutation 处 break、worker
  重启后重推同一 mutation 再冲突」→ **一次"删除已迁移设置键后再写回"即永久打死该账户全部
  server 同步且无恢复 UI**。触发链生产代码已接通（`settings-sync.js:144-148` removeItem →
  `preference-store.js:654-658` remove → 墓碑），当前仅因无 UI 调用点而未爆。
  **最小复现**：对 relay 依次 push ① put k(rev1,parent=null) ② remove k(rev2,parent=①)
  ③ put k(rev3,parent=②墓碑) → ③ 被拒；前端等价 = 注入页对迁移键
  `localStorage.removeItem→setItem`，观察 sync 状态进入 blocked 后所有后续变更不再上传。
  **修复方向（Codex 二选一裁决）**：放行"父=墓碑精确匹配"的线性续写（墓碑支配只拦无父证明
  复活），或封死 sync collection 的 remove 路径（改 enabled/absent 语义 put）并同步文档；
  两层规则不能继续矛盾并存。
- **非阻断风险（建议排期，不阻发布判定）**：
  - F2 出站迁移缺口：v2 时代无 causal 字段的本地存量记录推送必 `causal-proof-missing`
    冲突（含填空缺方向）；建议迁移时一次性补铸父证明。
  - F3 pagehide 释放租约的 fetch 无 `keepalive:true`，BFCache/关页时常被中止 → 同 family
    另一端最多多等 30s TTL（fail-closed 无双写）；pageshow 等待释放亦无界（文档已自认）。
  - F4 内容脚本实际还持有 `accountProof`（HMAC(namespace) 永久确定值，无过期轮换），与
    "只拿不透明 deviceId/registryDigest"表述不符；无服务端换权能力、风险低，建议补文档或加代际盐。
  - KG-a AI 返回"合法但空心"JSON 时被**推断**为 skip 并永久持久化（`gen_page_brief.py:226-228`），
    违背"显式 page_type=skip 才持久"原文；触发面窄，建议区分推断/显式。
  - KG-b quote 校验大小写不对称（服务侧 `_text_key` 不 casefold vs 生成侧 casefold）→
    大小写漂移的 quote 落成 pending 永久重试（无 AI 调用、fail-closed 方向，纯浪费）。
  - KG-c 陈旧 char-cache 回退窗口、KG-d mutationId 含 pageTextSha 可能重复计 signal——均窄面。
  - 文档已自认边界（不计缺陷）：registry digest 永久 pin fail-closed、MV3 重启等旧 TTL、
    relay_events 无界增长（运营性观察）。
- **测试证据对数（声称 vs 实测）**：Python 全量 341/341(skip7) 一致；relay+Node→Flask 集成
  32/32 一致；handoff quick errors=0/IndexedDB 79 断言/网络审计 0 新债务 一致；发布管线 14/14
  一致（注意其输出含一行预期 fixture ✗ 文案，非真失败；且模块只能按 extensions 目录口径跑）；
  Node contract 实测 **443/443 全过** vs 声称 442（+1 良性计数偏差，零失败）；Windows ZIP
  `ff204830…` 按声明已清理，无法核对但与安全边界自洽。**未发现虚报。**
- **下一步建议**：F1 交 Codex 裁决修复并补双向合同测试（父=墓碑续写 或 remove 封死）；修复
  后重跑 sync 合同与 relay 集成；发布 0.2.51 前仍须遵守既有边界（不拿线上旧 PWA 混测、
  不放宽 provider/registry fail-closed）。非阻断项 F2-F4/KG-a..d 建议列入下批排期。

2026-07-26 07:15 JST：夜间协调器已读取共享状态及既有 tmux 窗格。Claude 只读审查确认第 7 项存在 F1 阻断正确性问题：墓碑 head 后父状态精确匹配的线性 put 被错误拒绝，可能使同步永久阻塞。已向既有 Codex 会话发送中文最小本地修复任务：放行精确墓碑父状态续写、保留无/错父证明复活拒绝，并补双向合同与 relay 集成回归；完成后登记并交 Claude 复审。未部署、提交、推送、重启服务、使用凭据或修改日常 Chrome profile。
2026-07-26 07:30 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。F1 阻断修复仍由 Codex 在既有会话中进行：正在设计墓碑后精确父状态线性续写的 relay、客户端与 IndexedDB 双向合同；尚无修复完成、完整回归或阻塞登记。Claude 空闲等待复审；为避免打断实现，未发送新任务。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

## Codex F1 修复：墓碑后的因果线性续写（2026-07-26 07:32 JST，待 Claude 只读复审）

- **结论**：F1 已按 `record-parent-state/1` 与架构 §7 的既定语义完成本地修复。当前 head
  是墓碑时，incoming `put` 只有在父证明有效且父状态精确等于当前墓碑 head 时才作为线性
  续写接受；缺少父证明、父证明非法或父状态不一致仍统一返回 `tombstone-dominates`。非因果
  collection 原有墓碑保护不变。
- **改动范围**：
  - `_server_deploy/reader_sync_relay.py`：服务端 relay 的墓碑分支改为只拦无效/不匹配父证明，
    精确墓碑父继续进入同一因果校验与单调 revision 接受路径。
  - `_server_deploy/static/reader-runtime/data-store.js` 与
    `indexeddb-store.js`：内存/WebStorage/IndexedDB 使用同构的
    `linearTombstoneChild` 判定；没有改动独立的 `document-note-repository.js` 墓碑规则。
  - 扩展通过正式 `build.py` 同步
    `vendor/reader-runtime-data-store.js` 与
    `vendor/reader-runtime-indexeddb-store.js`，两份生成副本分别与运行时源码逐字一致。
  - 修正 `tests/test_reader_sync_relay.py` 与
    `tests/reader_contract/data-store.contract.test.mjs` 中固化旧错误行为的预期；在
    `direct-sync-protocol.contract.test.mjs` 和 `indexeddb-store.browser.html` 补入
    “精确墓碑父接受、无父/旧活动父拒绝”的双向合同。
- **测试证据**：
  - F1 单点 relay：1/1 通过；
  - sync 定向 Node 合同（全部 `*sync*.test.mjs` 加 DataStore）：123/123 通过；
  - relay 全套与真实 Node 客户端到 Flask 集成：32/32 通过，并在
    `PYTHONWARNINGS=error::ResourceWarning` 下无资源警告；
  - IndexedDB 真浏览器合同：82 assertions 通过（F1 新增三项断言）；
  - 两份运行时源码及扩展 vendor 的 `node --check` 通过，源码/vendor `cmp` 一致，扩展正式
    build 通过。
- **风险与未做事项**：本段只修 F1，没有处理 Claude 记录的 F2-F4、KG-a..d，也没有新增
  冲突裁决 UI、双真设备验收或线上激活；没有运行第 7 项之外的新功能实现。未部署、发布、
  提交、推送、重启服务、使用凭据或启动/修改日常 Chrome/profile。
- **当前所有权/下一步**：Codex 已停止继续实现，现场交给 Claude 做只读复审。Claude 应重点
  复核 relay 与三类客户端规则是否同构、拒绝原因是否保持 `tombstone-dominates`，并核对上述
  定向证据；复审前不得发布 0.2.51。

- 2026-07-26 07:35 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。Codex 已登记 F1 修复与定向验证（sync Node 123/123、relay+Node→Flask 32/32、IndexedDB 82 assertions、vendor 一致性与正式 build）并停止实现。已向既有 Claude 会话发送中文只读复审任务，重点核对 relay、三类客户端和 vendor 的精确墓碑父放行/错误父拒绝同构规则及证据；等待审查结论。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。


## Claude 只读复审：F1 墓碑后因果线性续写修复（2026-07-26 07:39 JST）——**通过**

- **范围与方法**：静态同构核对（relay / data-store / indexeddb-store / 扩展 vendor / 便签域）+
  定向与全量测试重跑。未改代码、未部署/提交/推送、未用凭据、未碰日常 Chrome。
- **同构核对通过（四处规则一致）**：
  - relay `_push_locked` 墓碑分支（reader_sync_relay.py:909-928）改为仅拦「无效父证明 OR 父
    不匹配当前墓碑」，原因保持 `tombstone-dominates`；父证明有效且精确匹配墓碑时落入**同一条**
    正常因果链（causal 校验 → 溢出护栏 → 接受+单调 revision）。`_causal_parent_matches` 对
    父=墓碑按 deleted 标志判等（同 id 墓碑业务等价，:85 注释 by-design）。
  - data-store.js:891-907 与 indexeddb-store.js:655-672 同构 `linearTombstoneChild =
    causalRequired && proof.valid && causalParentMatches(current, proof)`；非因果 collection
    经 `causalRequired` 短路，原墓碑保护不变。
  - vendor 两份生成物 `cmp` 逐字一致；`document-note-repository.js`（便签域，非 sync）源与
    vendor 均零 `linearTombstoneChild`，未被波及，与登记一致。
- **双向合同确认**：relay 测试三向断言齐全（test_reader_sync_relay.py:1130-1199——无父复活拒
  /错父复活拒均为 `tombstone-dominates`、精确墓碑父接受且 acked/head 前进/snapshot 读回新值）；
  data-store 与 direct-sync-protocol 合同、indexeddb-store.browser.html 均含线性续写断言。
- **测试证据全对数（声称 vs 实测一致）**：relay 全套+Node→Flask 集成 **32/32 OK**
  （`PYTHONWARNINGS=error::ResourceWarning` 下无警告）；sync 定向 Node+DataStore **123/123**；
  Node 合同全量（除 playwright）**443/443 零回归**；`handoff_check.py` quick errors=0 READY、
  **IndexedDB 82 assertions**（F1 +3）、发布管线含预期负例通过。
- **已确认语义边界（非缺陷，记录在案）**：墓碑不分代际——“删→建→再删”后，携带**旧代**墓碑
  父证明的 put 亦按线性续写接受。两代墓碑业务状态等价（该键已删），收敛结果与基于新墓碑续写
  一致，符合“同业务值收敛元数据”的既定哲学，无数据错乱面。
- **剩余明确本地待办（均非本次修复引入）**：F2 v2 无 causal 存量记录出站迁移补铸；F3 pagehide
  释放加 `keepalive:true` + pageshow 有界等待；F4 accountProof 表述/代际盐；KG-a（推断 skip
  与显式 skip 区分）与 KG-b..d 窄面偏差；registry digest 跨版本迁移协议；冲突裁决 UI 与
  `resolveConflict` 接线；两台真实设备直连/回退端到端验收；0.2.51 发布与 PWA runtime 部署
  （需用户另行授权，不得拿线上旧 PWA 混测、不得放宽 provider/registry fail-closed）。

- 2026-07-26 07:40 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。Claude 已于 07:39 对 F1 修复只读复审通过；共享状态仍有明确未完成本地待办。已向既有 Codex 会话发送中文任务，仅推进 F2（为 v2 无 causal 字段的本地存量同步记录补铸因果证明迁移），要求 fail-closed、定向合同及 relay/Node 集成回归；完成或阻塞后登记并交 Claude 只读复审。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。


- 2026-07-26 07:46 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。F1 已获 Claude 只读复审通过；唯一启动的明确本地待办为 F2（v2 缺少 causal 字段的本地存量记录因果证明补铸）。已在既有 Codex 会话启动已发送的中文、范围受限任务；要求 fail-closed、补定向合同及 relay/Node 集成回归，完成或阻塞后登记并停下交 Claude 只读复审。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 07:48 JST：Codex 已读取最新共享状态、架构 §7 与当前 Git 现场，现仅认领 F2：
  精确 `sync-v2` checkpoint 升级时，为缺少 `causal` 的本地待上传同步记录执行一次性、可重试、
  可验证且 fail-closed 的因果证明补铸，并补定向合同及真实 Node→Flask 回归。F3/F4/KG 及部署、
  发布、提交、推送、服务重启、凭据和日常 Chrome/profile 均不在本工作段范围；完成或阻塞后交
  Claude 只读复审。

- 2026-07-26 07:55 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。F2（为 v2 缺少 causal 字段的本地待上传同步记录补铸因果证明）仍在 Codex 范围受限的本地设计/实现中；尚无完成、完整回归或阻塞登记，Claude 空闲等待可复审工作段。为避免重复或打断，未发送新任务。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
﻿- 2026-07-26 08:01 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。F2（sync-v2 本地存量因果证明补铸）仍由 Codex 在范围受限的本地实现/验证中进行；最新可见 data-store 定向 Node 合同已通过，但尚无完成、完整回归或阻塞登记。Claude 空闲等待可复审工作段，为避免重复或打断未发送新任务。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

## Codex F2：v2 本地存量因果证明补铸迁移（2026-07-26 08:06 JST，待 Claude 只读复审）

- **结论**：F2 已完成本地实现与验证。只有摘要精确等于已知 `sync-v2` 合同的旧 checkpoint
  才进入 `sync-v2-causal-migration/1`；普通 v3、未知摘要以及迁移失败现场不会获得补铸权限。
  迁移先只读检查本地待上传 journal，必要时读取游标精确等于旧
  `server.remoteCursor` 的冻结服务端完整快照作为可信 baseline，再为缺少 `causal` 的记录逐条
  重建父状态。已有有效证明不被覆盖；没有可证明父状态、revision 不连续、已有证明不一致、
  journal 被裁剪或快照游标漂移时均零写入并 fail closed。
- **原子性与顺序**：
  - DataStore/WebStorage 与 IndexedDB 均以单次原子提交同步更新 journal、仍为当前 head 的
    record，以及仍被保留的 mutation receipt；不新增 journal 事件，不改变 stable id、业务值、
    cursor、revision、time 或 device。
  - 本地补铸成功后必须先持久化升级后的 checkpoint，随后才允许第一次 server push；该轮直连
    peer 明确跳过。若 checkpoint 保存中断，重试会验证已落盘证明、保持幂等，不再次拉 baseline，
    也不会在失败轮提前 push。
  - 协调器严格校验 store 返回的 collection、游标及 examined/missing/migrated/verified 计数，
    避免错误实现被当成迁移成功。
- **改动范围**：
  - `_server_deploy/static/reader-runtime/data-store.js`、
    `indexeddb-store.js`：共享迁移规划器及内存/WebStorage/IndexedDB 原子迁移。
  - `_server_deploy/static/reader-runtime/sync-coordinator.js`：精确旧合同触发、冻结 baseline
    分页校验、server-first 围栏、checkpoint-before-push 与可观测迁移结果。
  - `extensions/bw-reader-webext/vendor/reader-runtime-{data-store,indexeddb-store,sync-coordinator}.js`
    通过正式 `build.py` 同步，三组源码/vendor 均逐字一致。
  - `tests/reader_contract/data-store.contract.test.mjs`、
    `sync-coordinator.contract.test.mjs`、`indexeddb-store.browser.html` 与
    `tests/test_sync_owner_lease_client_server_integration.py` 补成功、错误 baseline、journal
    缺口、崩溃重试、持久化重开及真实 Node→Flask 一次上传/重启不重复合同。
  - `references/reader-runtime-architecture.md` 的 §7 固化上述一次性迁移与 fail-closed 边界。
- **测试证据**：
  - 全量 Node reader contract：**447/447**；
  - sync 定向 Node 合同（全部 `*sync*.test.mjs` 加 DataStore）：**128/128**；
  - IndexedDB 真浏览器合同：**87 assertions**；
  - relay 全套与真实 Node 客户端到临时 Flask 集成：**33/33**，并在
    `PYTHONWARNINGS=error::ResourceWarning` 下无资源警告；
  - F2 新增的真实 Node→Flask 用例确认 relay SQLite 只产生 1 条事件，补铸的 root parent
    为 `null`，第二次运行不重复上传；
  - 三份运行时源码及 vendor `node --check`、`cmp`、扩展本地正式 build、目标文件
    `git diff --check` 均通过。
- **风险与未做事项**：
  - 迁移保留原 mutation id。若某条 proofless payload 曾在 v3 relay 上以同一 mutation id
    留下不同 payload hash，relay 会继续以 mutation-id-reuse fail closed；当前 0.2.51 尚未发布，
    正常升级路径不应存在该现场。本段没有越界设计 mutation id 重映射或服务端修复协议。
  - 没有处理 F3/F4/KG、registry 的通用跨版本迁移、冲突 UI 或双真实设备验收；没有部署、
    发布、提交、推送、重启服务、使用凭据或启动/修改日常 Chrome/profile。
- **当前所有权/下一步**：Codex 在此停止实现，现场交 Claude 做只读复审。复审应重点核对：
  旧 checkpoint 触发范围、快照游标/分页冻结、journal/head/mutation receipt 原子一致性、
  checkpoint-before-push/直连跳过、失败零写入与崩溃重试证据；复审通过前不得推进 F3/F4/KG
  或发布 0.2.51。


## Claude 只读复审：F2 sync-v2 因果证明补铸迁移（2026-07-26 09:17 JST）——**通过**

- **范围与方法**：静态深审（sync-coordinator / data-store / indexeddb-store / 四份合同测试 /
  架构 §7 新段）+ 只读与隔离测试重跑。未改代码/测试/制品、未部署/发布/提交/推送/重启、
  未用凭据、未碰日常 Chrome。
- **六项核对全部通过**：
  1. 仅精确旧 checkpoint 触发：`normalizeCheckpoint`（sync-coordinator.js:71-114）以
     `sync-v2:` 前缀摘要精确匹配挂不可枚举 marker，未知/普通 v3 摘要重置为空 checkpoint；
     `migrateLegacyCausal` 唯一调用点无 marker 即返回 null；失败轮持久摘要仍 legacy、
     成功前永不 push、无死循环。
  2. 冻结 baseline：首页游标必须精确等于旧 `remoteCursor`（漂移即
     `BASELINE_CHANGED` 不可重试）；分页 pageId/cursor 漂移、漏页/重页/停滞、resetRequired、
     页数/记录上限全 fail closed；baseline 重复记录由 planner 拒绝。
  3. 原子一致性：内存/WebStorage 单点整状态提交、IndexedDB 四 store 单事务（abort 整体回滚）；
     journal 按原 cursor 原位覆盖不新增事件；唯一改动=补 `causal` 字段，
     stable id/业务值/cursor/revision/time/device 写前逐字段复验；已有有效证明不覆盖
     （missing 分支才补铸，冲突/非法证明各自抛错）。
  4. checkpoint-before-push + 直连跳过：saveCheckpoint 成功后 server lane 才跑；迁移轮
     每个直连 peer 显式 `skipped`（合同断言 directPushes=0）；保存中断重试幂等
     （已落盘证明锚点链式复验、不重拉 baseline、失败轮零 push，均有合同覆盖）。
  5. 失败零写入/崩溃幂等/fail-closed：无证明父/rev 不连续、证明不一致、非法证明、journal
     裁剪缺口、快照漂移五类全部在 store 写入前抛错；“部分补铸”半状态在 store 层不可能存在
     （单事务），唯一半状态=已提交+checkpoint 未存，重试幂等验证。
  6. 协调器计数校验：contract/collections/游标/四计数/needsBaseline 严格校验，不合格
     `BW_SYNC_CAUSAL_MIGRATION_STORE` 拒收。
- **F1 未回归**：`linearTombstoneChild` 在 data-store:1218/indexeddb-store:760 原位，relay
  :914-927 规则未动；F2 为独立 planner/migrate 函数，未触碰 `applyChanges`；relay 无迁移特例
  （补铸纯客户端）。
- **验证证据（声称 vs 实测）**：vendor 三份 `cmp` 逐字一致；relay 全套+真实 Node→Flask 集成
  **33/33 OK**（`PYTHONWARNINGS=error::ResourceWarning` 无警告，含 F2 用例：relay SQLite 恰
  1 条事件、补铸 root parent=null、二轮不重传、checkpoint 摘要升级）；sync 定向 Node
  **128/128**；Node 合同全量（除 playwright）**448/448 零失败**（声称 447，+1 良性计数偏差，
  与 F1 轮 442→443 同款）；`handoff_check.py` quick errors=0 READY、**IndexedDB 87
  assertions**。**未发现虚报。**
- **非阻断记录（建议下批）**：① 协调器可补 `migrated===missing` 与
  `examined===verified+missing` 两条恒等式交叉校验（防谎报全零的错误 store 实现烧掉一次性
  迁移权限；下游 relay 仍 fail-closed 不会推错数据，故非阻断）；② 迁移失败轮
  `result.checkpoint` 观测视图为已 normalize 的 v3 摘要，与持久 legacy 摘要不一致（纯观测面）。
- **剩余待办（未变）**：F3（pagehide keepalive+有界等待）、F4（accountProof 表述/代际盐）、
  KG-a..d、registry digest 跨版本迁移协议、冲突裁决 UI、双真实设备端到端验收、
  0.2.51 发布与 PWA runtime 部署（需用户授权）。
- **下一负责人**：协调器。Claude 已停止并等待；未派 Codex，未开始 F3/F4/KG。

- 2026-07-26 09:21 JST：Codex 已完整读取最新共享状态、`reader-runtime-architecture.md`、
  当前 PWA owner-lease 生命周期实现与 Git 现场，现仅认领 F3：为 `pagehide` 的 owner release
  请求加入 `keepalive:true`，并把 BFCache `pageshow` 对旧 release 的等待改为有界等待；超时后
  仍须先重新 claim 成功才恢复持久同步与直连，旧 release 迟到不得影响新 generation。
  本工作段不处理 F4、KG-a..d、registry digest、冲突 UI 或双真实设备验收；不部署、发布、
  提交、推送、重启服务、使用凭据或启动/修改日常 Chrome/profile。完成或阻塞后交 Claude
  只读复审。

## Codex F3：pagehide keepalive 与 BFCache 有界恢复（2026-07-26 09:31 JST，待 Claude 只读复审）

- **结果与语义**：
  - `pagehide` 先同步暂停持久同步和直连；普通关页永久 `destroy`，BFCache 进入可恢复
    `stop`，两路仅在释放 owner lease 时显式使用 `keepalive:true`。普通 claim、renew 与非
    pagehide release 不强制 keepalive。
  - `pageshow` 最多等待旧 release **2 秒**。release 正常结束或等待超时后都只允许重新
    claim；只有新 claim 的 `onAcquired` 成功后才恢复持久同步和直连。若旧租约仍为 HELD，
    保持暂停并沿用已有 3 秒租约重试，不直接恢复、不产生双写。
  - 新增独立页面生命周期代次，并在等待后复验账户 lease、页面状态与扩展 marker。等待中
    再次 pagehide、切换账户或出现扩展接管标记时，旧 continuation 均不能 claim 或恢复。
    迟到旧 release 仍受服务端精确 generation/token 围栏，不能释放新 generation。
- **改动范围**：
  - `_server_deploy/static/reader-runtime/sync-owner-lease.js`：为释放链路增加显式请求选项
    透传；仅显式请求时给生产 fetch 设置 keepalive。
  - `_server_deploy/static/reader-runtime/pwa-runtime.js`：pagehide 两类释放、2 秒有界等待、
    页面生命周期围栏及 fail-closed 恢复。
  - `extensions/bw-reader-webext/vendor/reader-runtime-sync-owner-lease.js`：通过正式
    `build.py` 同步，和共享源码逐字一致。
  - `tests/reader_contract/sync-owner-lease.contract.test.mjs`、
    `pwa-runtime-lifecycle.contract.test.mjs`：覆盖普通关页、BFCache、pending release、
    HELD/安全重试、迟到成功/失败、重复 pagehide、账户切换、扩展 marker、无直连写入及
    keepalive 不污染普通请求。
  - `references/reader-runtime-architecture.md`、`reader-extension-handoff.md` 与
    `extensions/bw-reader-webext/README.md` 固化本次边界。
- **验证证据**：
  - F3 两份定向 Node 合同：**48/48**；
  - Node reader contract 全量：**453/453**，零失败/跳过/todo；
  - owner/handoff 隔离回归在新增账户用例前为 **140/140**；新增用例随后同时进入定向和
    全量回归并通过；
  - relay 全套与真实 Node→临时 Flask owner-lease 集成：**33/33**，并在
    `PYTHONWARNINGS=error::ResourceWarning` 下无资源警告；其中既有服务端合同继续证明旧
    generation/token 的 release 不能释放新 owner；
  - `handoff_check.py`：errors=0、READY，IndexedDB **87 assertions**、text range
    **6 assertions**、交互网络依赖 **0 new debt**；仅报告既有脏工作区警告；
  - 三份 JS `node --check`、共享源码/vendor `cmp`、目标文件 `git diff --check` 均通过；
    Codex 独立只读复核未发现阻断。
- **风险**：
  - 浏览器 keepalive 是尽力发送而非送达保证；释放未送达时，新 claim 可能在旧租约 TTL
    内返回 HELD，但客户端会继续暂停并安全重试，不以可用性换双写。
  - 本段按限制只做本地合同、隔离集成与构建门禁，未部署 PWA runtime，也未用真实日常浏览器
    验证原生 BFCache/关页网络行为；这仍需获准后的隔离浏览器或双真实设备验收。
- **明确未做**：F4、KG-a..d、registry digest 跨版本迁移、冲突 UI、双真实设备验收及
  0.2.51 发布/部署均未开始；没有提交、推送、重启服务、使用凭据或启动/修改日常
  Chrome/profile。
- **下一负责人**：Claude 仅做只读复审，重点核对 keepalive 作用域、2 秒等待后的
  claim-only 恢复、page/account/marker 三类失效、迟到 release 的 generation/token 围栏及
  无双写证据。Codex 在此停止实现，复审通过前不推进 F4/KG 或发布。

- 2026-07-26 10:16 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。Codex 已登记 F3（pagehide keepalive 与 BFCache 有界恢复）完成及完整本地验证；现已向既有 Claude 会话发送中文只读复审任务，重点核对 keepalive 作用域、claim-only 恢复、三类失效围栏、迟到 release generation/token 围栏与无双写证据。等待审查结论；未启动 F4/KG。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。


## Claude 只读复审：F3 pagehide keepalive 与 BFCache 有界恢复（2026-07-26 10:18 JST）——**通过**

- **范围与方法**：静态审（sync-owner-lease.js / pwa-runtime.js / 两份新合同）+ 只读测试重跑。
  未改代码、未部署/发布/提交/推送/重启、未用凭据、未碰日常 Chrome/profile。
- **五项核对全部通过**：
  1. **keepalive 作用域**：`httpRequest` 仅在 `requestOptions.keepalive === true` 时设置
     （sync-owner-lease.js:231-232）；全仓唯一调用点=pagehide 两路（BFCache stop
     pwa-runtime.js:1597-1599 / 普通关闭 destroy :1608-1610）显式传入；普通 claim/renew/
     非 pagehide release 无该选项，合同含"keepalive 不污染普通请求"断言（5 处 keepalive 断言）。
  2. **pageshow 2 秒有界 + claim-only 恢复**：`PAGE_OWNERSHIP_RELEASE_WAIT_MS=2000`，
     `waitForPageOwnershipRelease` 定时器超时 finish(false)、release 成败均 finish(true)、
     settled 防双结算（:545-563）；等待后只走 `startPwaSyncOwnerLease`（claim），
     **仅 `onAcquired`（:492→:432-433）才 resume 同步/直连**；HELD 时保持暂停走既有租约重试，
     不直接恢复。
  3. **三类失效围栏**：账户=`assertAccountLease`（fence error 静默退出）；页面=独立
     `pageOwnershipLifecycleGeneration` 代次 + `pwaNetworkRuntimeReady`（等待中再次 pagehide
     即失效旧 continuation）；扩展 marker=`reserveExtensionSyncOwner()` 在等待**前后各查一次**，
     命中即 pause 不恢复（:1613-1637）。
  4. **迟到 release 围栏**：relay `owner_release`（reader_sync_relay.py:1363,:1380）经
     `_owner_lease_row_matches` 逐字段（generation/role/instance/device + hmac token）匹配才
     释放——新 claim 已换 generation/token，旧 release 打不中新 owner（既有服务端合同持续证明）。
  5. **无双写证据**：pagehide 先同步 pause（代次+1 → 在途结果失效）再释放；恢复必经新 claim
     成功；失败路径统一 `pausePwaNetworkOwners('pageshow-owner-lease-error')` fail-closed。
     合同覆盖普通关页/BFCache/pending release/HELD 重试/迟到成功与失败/重复 pagehide/账户
     切换/扩展 marker/无直连写入。
- **验证证据（声称 vs 实测）**：F3 定向两份合同 **48/48**；Node 合同全量（除 playwright）
  **454/454 零失败**（声称 453，+1 良性计数偏差，与 F1/F2 轮同款模式）；relay 全套+真实
  Node→Flask 集成 **33/33 OK**（`PYTHONWARNINGS=error::ResourceWarning` 无警告）；
  `handoff_check.py` quick errors=0 READY、IndexedDB **87 assertions**；
  sync-owner-lease vendor `cmp` 逐字一致。**未发现虚报。**
- **已知边界（登记自认，合理）**：keepalive 是尽力发送非送达保证——未送达时新 claim 在旧
  TTL 内 HELD、继续暂停安全重试，不以可用性换双写；真实浏览器 BFCache/关页网络行为仍需
  获准后的隔离浏览器或双真实设备验收。
- **下一项明确本地待办**：F4（accountProof 文档表述/代际盐）与 KG-a（推断 skip 与显式 skip
  区分）；其后 KG-b..d、registry digest 跨版本迁移、冲突裁决 UI、双真实设备端到端验收、
  0.2.51 发布与 PWA runtime 部署（需用户授权）。
- **下一负责人**：协调器。Claude 已停止并等待；未派 Codex，未开始 F4/KG。

- 2026-07-26 10:21 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。F3 已获 Claude 只读复审通过；按状态中明确的下一项，已向既有 Codex 会话发送中文、范围受限的 F4（accountProof 表述与代际盐）本地实现/验证任务，要求完成或阻塞后登记并停下交 Claude 只读复审。已告知 Claude 当前无 F4 完成登记，保持只读等待。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 10:26 JST：夜间协调器重新读取共享状态与既有 codex/claude tmux。共享文件末尾仍仅有 10:21 JST 的 F4 派工，尚无 Codex F4 完成范围与验证证据登记；codex 会话当前仍在执行本地工作，未中断也未重复派工。因 claude 会话收到的“F4 已登记”提示与事实源不符，已立即用中文更正为停止任何 F4 审查、保持只读等待，直至完整登记出现。未开始 KG 或其他事项；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 10:31 JST：夜间协调器复读共享状态及既有 codex/claude tmux 窗格。共享事实源仍无 F4 完整完成范围与验证证据；既有 Codex 会话已收到仅 F4 的范围受限本地实现/验证指令，当前正在处理，未中断或重复派工。Claude 窗格收到的“F4 已登记”提示与共享事实源不符，已用中文再次更正为不得开始审查、保持只读等待，直至完整登记出现。未开始 KG 或其他事项；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。﻿
- 2026-07-26 10:36 JST：夜间协调器复读共享状态与既有 codex/claude tmux。共享事实源仍无 Codex F4 完整完成范围与验证证据；既有 Codex 会话正按 10:21 JST 已限定的 F4（accountProof 表述与代际盐）本地实现/验证指令工作，未中断、未重复派工。Claude 窗格出现的 F4 已登记完成提示不符合共享事实源，已发送中文更正：不得启动审查，保持只读等待，直至完整 F4 登记出现。未启动 KG 或其他事项；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 10:38 JST：Claude 状态确认——F1/F2/F3 三轮只读复审均已完成并登记（F1 通过 07:39、F2 通过 09:17、F3 通过 10:18）。当前保持只读等待：共享状态尚无 Codex F4 完整完成/验证登记，未开始 F4 审查、未修改任何项目文件；待完整登记出现后按既有范围复审。

- 2026-07-26 10:39 JST：Codex 已完整读取最新共享状态、账户/同步架构、信令 relay、RTC
  内容宿主与现有合同，现仅认领 F4。当前派工“表述与代际盐”按两项同时完成：文档明确内容
  宿主内存还持有不透明、无服务端换权能力的 `accountProof`；服务端以已验证且账户内固定的
  `registryDigest` 作为协议代际盐参与 HMAC，使证明只在同账户、同 registry 代际稳定。
  保留 `account-proof-v1-<64hex>` 不透明线框以兼容既有客户端；不使用不同设备无法共享的
  owner generation，也不引入会在运行中裂脑的时间桶、进程随机盐或新 DB 轮换协议。本工作段
  不处理 KG、registry digest 迁移、冲突 UI、双真实设备验收或发布/部署；完成或阻塞后登记并
  停在 Claude 只读复审。

- 2026-07-26 10:41 JST：夜间协调器复读共享状态与既有 codex/claude tmux。共享事实源目前仅登记 Codex 于 10:39 JST 开始 F4，尚无 F4 完整完成范围与验证证据；codex 窗格仍在本地实现中，未中断也未重复派工。claude 窗格出现的“Codex 已登记 F4 完成”提示与事实源不符，已用中文更正为不得启动审查，保持只读等待直至完整 F4 登记出现。未启动 KG 或其他事项；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 10:46 JST：夜间协调器复读共享状态与既有 codex/claude tmux。共享事实源仍只有 F4 开始记录，尚无 Codex F4 完整完成范围与验证证据；codex 窗格正在 F4 范围内补齐 accountProof 代际突变后的既有 RTC peer fail-closed 清理并进行本地验证，未中断也未重复派工。claude 窗格保持中文只读等待，未启动 F4 审查。未启动 KG 或其他事项；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

## Codex F4：accountProof 边界与 registry 代际盐（2026-07-26 10:49 JST，待 Claude 只读复审）

- **结果与协议边界**：
  - 服务端 `accountProof` 现为
    `HMAC(SECRET_KEY, domain-separator + registryDigest + namespace)`；只在认证 namespace、
    当前 owner 租约和账户固定 registry 均验证成功后生成。它在同账户、同 registry 代际稳定，
    跨账户或跨代际不同。
  - 保持 `account-proof-v1-<64hex>` 不透明线框；未使用 owner generation、设备 ID、时间桶或
    进程随机盐，避免真实设备间无法得到相同证明或运行中裂脑。证明只作 RTC 账户/代际相等性
    围栏，不能换取服务端权限、不得作为业务身份持久化，错误响应不回显 proof、namespace 或
    owner token。
  - 同一宿主内证明突变，以及信令响应缺失/畸形证明或合同，都会先关闭已有 RTC peer、移除
    runtime peer、清空待发信令，再以非重试错误停止直连；可靠服务端同步仍保留。
  - 文档已区分真实运行边界：扩展 content host 不持有 namespace/Bearer/owner token；无扩展
    PWA 的可信同源运行时为领取租约会在内存中持有 namespace 与 owner lease，但
    `direct-sync-host`、RTC 帧和 peer 仍只接触不透明 proof。
- **改动范围**：
  - `_server_deploy/reader_sync_relay.py`：加入 registry 代际盐，并把 proof 生成放在认证、
    owner lease 与 registry pin 之后。
  - `_server_deploy/static/reader-runtime/direct-sync-host.js`：proof 突变及信令证明/合同损坏时
    清理全部旧 peer 后 fail closed。
  - `extensions/bw-reader-webext/vendor/reader-runtime-direct-sync-host.js`：通过正式
    `build.py` 从共享源码同步，未手改 vendor。
  - `tests/test_reader_sync_relay.py`、
    `tests/reader_contract/direct-sync-host.contract.test.mjs`、
    `direct-sync-signal-transport.contract.test.mjs`、
    `extension-provider.contract.test.mjs`：覆盖同代稳定、跨账户/代际分离、验证顺序、错误不
    回显、内容宿主边界，以及在已有活跃 peer 后 proof 突变/缺失/畸形的双向清理路径。
  - `references/reader-runtime-architecture.md`、`reader-extension-handoff.md`、
    `reader-extension-ownership.md` 与 `extensions/bw-reader-webext/README.md`：固化实际边界、
    代际盐和 fail-closed 语义。
- **验证证据**：
  - direct-sync host 聚焦合同：**12/12**；
  - direct/PWA/extension/owner 定向合同：**140/140**；
  - Node reader contract 全量：**456/456**，零失败/跳过/todo；
  - relay 全套与真实 Node→临时 Flask 集成：**34/34**，并在
    `PYTHONWARNINGS=error::ResourceWarning` 下无资源警告；
  - `handoff_check.py`：errors=0、READY，IndexedDB **87 assertions**、text range
    **6 assertions**、交互网络依赖 **0 new debt**；仅报告既有脏工作区警告；
  - 两份 host JS `node --check`、共享源码/vendor `cmp`、目标文件 `git diff --check`
    均通过；
  - Codex 独立只读复核通过，无阻断；复核另跑 F4/直连相关 Node **120/120**、全量
    **456/456**、relay/Node→Flask **34/34**。
- **风险与边界**：
  - 首次发布新 derivation 或未来轮换 `SECRET_KEY` 时，旧活动 proof 会变化；宿主按设计关闭
    直连并保留服务端同步。若存在新旧 relay 进程混跑，proof 会来回变化并停用直连，因此正式
    发布应原子切换同一服务端版本，不能把双代兼容静默塞入本次修复。
  - `registryDigest` 跨版本升级/双代迁移仍未设计；当前继续 fail closed。现有一项旧合同名称
    写“proof、digest 或 session 不匹配”，测试数据只显式变造 proof；三者共用同一比较/关信道
    分支，本次所需 proof 路径已经完整实测，列为非阻断测试债。
  - 本段只做本地合同、临时 Flask 集成与构建门禁；未做双真实设备或真实浏览器验收。
- **明确未做**：KG-a..d、registry digest 迁移、冲突 UI、双真实设备验收、0.2.51
  发布/PWA runtime 部署均未开始；没有部署、发布、提交、推送、重启服务、使用/索取凭据或
  启动/修改日常 Chrome/profile。
- **下一负责人**：Claude 仅做只读复审，重点核对 HMAC 输入/生成顺序、PWA 与扩展的持有边界、
  旧 peer 在 proof 突变/缺失/畸形后的清理，以及发布时新旧 relay 混跑风险。Codex 在此停止
  实现；复审通过前不推进 KG、registry 迁移或发布。
- 2026-07-26 10:50 JST：夜间协调器复读共享状态，Codex 已登记 F4（accountProof 边界与 registry 代际盐）完成及本地验证；现已向既有 Claude 会话发送中文只读复审，聚焦 HMAC 输入/生成顺序、PWA/扩展持有边界、proof 异常时旧 RTC peer 清理与新旧 relay 混跑风险。等待复审结论；未启动 KG。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。


## Claude 只读复审：F4 accountProof 边界与 registry 代际盐（2026-07-26 10:55 JST）——**通过**

- **范围与方法**：静态审（reader_sync_relay.py / direct-sync-host.js / direct-sync-protocol.js /
  direct-sync-signal-transport.js / content host）+ 只读测试重跑。未改代码、未部署/发布/提交/
  推送/重启、未用凭据、未碰日常 Chrome/profile。
- **四项核对全部通过**：
  1. **HMAC 输入与生成顺序**：`_account_proof`（reader_sync_relay.py:1116-1145）=
     `HMAC(SECRET_KEY, "reader-direct-account-proof-v1\0registry-generation\0" + registryDigest
     + namespace)`，wire 形状保持 `account-proof-v1-<64hex>` 不透明；调用点默认空串（:1484），
     仅在 `_verify_owner_lease_locked` → `_pin_registry_digest_locked` **之后**（:1495 上下文）
     生成——认证/租约/registry pin 顺序与登记一致。未用 owner generation/设备 ID/时间桶/进程
     随机盐（docstring 说明合理：避免真实多设备无法互认或代际中裂脑）。
  2. **持有边界**：`src/direct-sync-content-host.js` 对 namespace/Bearer/ownerToken **零代码
     持有**（唯一命中是注释），甚至不出现 accountProof 字段名（仅透传后台受信 READY 配置与
     信令 payload）；无扩展 PWA 的同源运行时按登记在内存持 namespace+lease，但
     direct-sync-host/RTC 帧只接触不透明 proof（protocol :651 帧携带、:608-612/:515-519
     接收/握手侧仅做相等性比较）。
  3. **proof 突变/缺失/畸形清理与 fail-closed**：突变分支（direct-sync-host.js:818-831）
     `resetBaseline('account-proof-changed')` → 清空待发信令（`outgoing=[]`）→ 逐个
     `closePeer`（关 RTC + 移 runtime peer）→ 保留服务端通道（`scheduleServer`）→ 抛
     **retryable=false** 的 `BW_DIRECT_HOST_PROOF`（注释明确"已开信道由过时代际授权，必须
     先全部拆掉"）；缺失/畸形走 signal-transport 层 `BW_DIRECT_SIGNAL_INVALID/CONTRACT`
     （SignalError 第三参恒 false），host :892 按合同违规同样 resetBaseline 且 emit
     retryable=false。首次空 proof 只在 lease 未验证场景存在，且该场景 exchange 本身 409
     进不到采纳路径。
  4. **新旧 relay 混跑风险**：proof 由 SECRET_KEY+registryDigest+namespace 决定，混跑时响应
     proof 交替 → 每次变化触发突变分支 → 拆信道+non-retryable 停直连、服务端同步保留——
     **fail-closed 方向，无双写/错认面**；登记"正式发布须原子切换同一服务端版本、不塞双代
     兼容"与代码一致，属部署纪律要求而非代码缺陷。
- **验证证据（声称 vs 实测）**：direct-sync-host 聚焦 **12/12** 精确一致；direct/PWA/
  extension/owner 定向按我方更宽 glob 实测 **161/161 零失败**（≥声称 140，口径差异、只多
  不少）；Node 合同全量（除 playwright）**457/457 零失败**（声称 456，+1 良性，四轮同款
  模式）；relay 全套+真实 Node→Flask 集成 **34/34 OK**（`PYTHONWARNINGS=error::ResourceWarning`
  无警告）；`handoff_check.py` quick errors=0 READY、IndexedDB **87 assertions**；
  direct-sync-host vendor `cmp` 逐字一致。**未发现虚报。**
- **非阻断记录（登记已自认）**：旧合同名"proof、digest 或 session 不匹配"仅显式实测 proof
  变造路径（三者同分支，列为测试债）；registryDigest 跨版本双代迁移未设计、继续 fail closed；
  双真实设备/真实浏览器验收待授权。
- **下一负责人**：协调器。Claude 已停止并等待；未开始 KG-a..d、registry 迁移、冲突 UI 或
  发布事项。

-  \：夜间协调器复读共享状态及既有 codex/claude tmux。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过（无阻断）；但共享事实源未列出下一项明确授权的本地待办，KG-a..d 等仍标记未开始，故按边界停止自动接力。已纠正 Claude 窗格中无事实依据的 KG-a 复审提示，要求继续中文只读等待。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 10:56 JST：更正上一条时间字段：F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过（无阻断）；共享事实源未列出下一项明确授权的本地待办，KG-a..d 仍标记未开始，故按边界停止自动接力。Claude 已被要求继续中文只读等待。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 11:05 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux。F4 已获 Claude 只读复审通过；共享事实源没有登记下一项明确授权的本地待办，KG-a..d 仍为未开始。因此停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 11:11 JST：夜间协调器复读共享状态与既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过；共享事实源当前没有新的、明确授权的本地实现段，KG-a..d 仅列为未开始的后续事项，故按边界停止自动接力。未派发或启动实现/审查；未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 11:16 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源未登记新的明确授权本地实现段，KG-a..d 仍未开始。因此停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 11:20 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源仍未登记新的明确本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 11:26 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源仍未登记新的明确本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 11:41 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源仍未登记新的明确本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 11:46 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源未登记新的明确授权本地实现段，KG-a..d 仍仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 11:51 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 11:56 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 只读复审通过、无阻断；共享事实源仍未登记新的明确本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 12:00 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 12:05 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 12:10 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
﻿
- 2026-07-26 12:15 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。


- 2026-07-26 12:20 JST：夜间协调器读取共享状态并只读核对既有 codex/claude tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 12:25 JST：夜间协调器读取共享状态并只读核对既有 `codex`/`claude` tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 12:30 JST：夜间协调器读取共享状态并只读核对既有 `codex`/`claude` tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 12:35 JST：夜间协调器读取共享状态并只读核对既有 `codex`/`claude` tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。
- 2026-07-26 12:40 JST：夜间协调器读取共享状态并只读核对既有 `codex`/`claude` tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 12:45 JST：夜间协调器读取共享状态并只读核对既有 `codex`/`claude` tmux 窗格。F4 accountProof 边界与 registry 代际盐已获 Claude 中文只读复审通过、无阻断；共享事实源仍未登记新的明确授权本地实现段，KG-a..d 仅为未开始后续项。因此按授权边界停止自动接力，未派发或启动实现/审查。未部署、发布、提交、推送、重启服务、使用凭据或修改日常 Chrome/profile。

- 2026-07-26 12:53 JST：协调方已明确授权按顺序自动继续。Codex 已完整读取本文件，现仅认领
  KG-a：区分 AI 显式 `page_type=skip` 与缺失/非法页型下的推断结果；只有显式 skip 可持久化，
  全空/空心结果保持可重试，并只读盘点已有 sidecar 是否需要迁移、补齐定向合同。本段不处理
  KG-b..d、registry 迁移、冲突 UI 或双设备验收；完成本地与既有独立扩展环境验证后登记并交
  Claude 只读复审。不得使用协调器临时浏览器或修改日常浏览器配置/资料。

## KG-a：显式 skip 与推断空结果分流（2026-07-26 13:04 JST）——待 Claude 只读复审

- **目标与结论**：已完成 KG-a。`scripts/kg/gen_page_brief.py` 不再把缺失/非法
  `page_type` 且 `brief/tags` 全空的空心模型结果推断为 `skip`；该结果现在保留空页型，
  由现有阅读器空结果门禁拒绝持久化并允许后续重试。只有模型明确返回
  `page_type=skip` 才可持久化为“本页不建 KG”。缺失页型但存在 `brief` 或 `tags`
  的旧兼容路径仍推断为 `knowledge`，未扩大本段范围。
- **改动范围**：
  - `scripts/kg/gen_page_brief.py`：收紧 `_parse()` 的页型回退规则；
  - `tests/test_concept_node_service.py`：覆盖 `{}`、缺失页型、非法页型、显式 skip
    和带内容的兼容 knowledge 五条解析合同；
  - `tests/test_page_brief_kg_integration.py`：补显式 skip 只生成/持久化一次且永不进入
    KG 的集成路径；既有空心结果连续重试且不进入 `_none_pages` 的合同继续通过。
- **存量只读盘点**：现有 3 个 sidecar 共 34 条 PageBrief、其中 10 条 skip；
  “skip 且 brief/tags/concepts 全空”的歧义记录为 **0**，`_none_pages` 为 **0**，
  因而本段不写一次性迁移，也不改 `BRIEF_PROMPT_VER/_BRIEF_VER`。
- **验证证据**：
  - 定向 Python 合同：`python3 -m unittest -v tests.test_concept_node_service
    tests.test_page_brief_kg_integration` → **23/23 OK**；
  - Python 全量：`python3 -m unittest discover -s tests -p 'test_*.py'`
    → **345/345 OK，skipped=7**；另以 `py_compile` 和目标文件行尾空白检查通过；
  - 本地只构建、未部署的 Windows 候选包：
    `extensions/bw-reader-webext-0.2.51-windows-test.zip`，
    SHA-256 `09b1b3a492ed0df7eaecd73fe71ec15c0d1d5305ecc6e2735a57c2c1e08522c7`；
  - 用户既有独立 Windows 测试环境
    `%LOCALAPPDATA%\BWReaderExtensionTest\browser-profile-v2` 上以 Chrome
    `150.0.7871.182` 验证：扩展
    `jddhhakcblmihidgdobfkcejjinpigak` 报告版本 `0.2.51`；普通网页成功注入
    facade/web adapter/shell/sidebar/highlight/pin/ink/settings-sync，Shadow DOM
    与侧栏把手存在，网页原生按钮点击仍为 1 次。测试后仅终止该专用 profile 的进程，
    `127.0.0.1:9222` 已无监听、专用 Chrome 进程数为 0。
- **恢复信息**：固定环境原扩展保留为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\
  extension-backup-pre-0.2.51-20260726T040009Z`；未改测试 profile，
  `installed-version.txt` 仍为 `0.2.47`。没有触碰日常 Chrome/profile。
- **风险与边界**：浏览器验证是候选包/共享网页运行时的回归冒烟；KG-a 解析器属于服务侧
  执行路径，正式生效仍需后续服务部署，不能把本次浏览器冒烟当成生产 KG 验收。未做 PWA
  新旧 runtime 混用结论，也未绕过发布 preflight；未部署、发布、提交、推送或重启服务。
  模型“明确但语义错误地返回 skip”仍属另一项语义质量问题，不在 KG-a 范围。
- **下一负责人**：Claude 仅做只读复审，重点核对显式/推断分流、空结果可重试、
  显式 skip 单次持久化和“不 bump brief 版本/无需迁移”的结论。Claude 未通过前，
  Codex 不修改本段三个实现/测试文件；并行只允许处理与这些文件完全不重叠的明确待办。

- 2026-07-26 13:17 JST：用户授权并行流水。Codex 现仅认领 KG-b：只修改
  `scripts/kg/concept_node_service.py`，并新增独立 KG-b 定向测试文件；统一服务侧与生成侧
  已有的 `NFKC → 去空白 → casefold` 连续证据匹配，允许纯大小写/Unicode 兼容形式漂移，
  但继续拒绝改写、缺词、插词、重排和非连续匹配。KG-a 冻结的
  `scripts/kg/gen_page_brief.py`、`tests/test_concept_node_service.py`、
  `tests/test_page_brief_kg_integration.py` 不修改。另按用户授权只读审计 F1-F4 已审候选的
  部署可分离性；若服务重启会混入未审 KG-a/KG-b，则停在部署前。不得部署未审改动、发布、
  提交、推送、索取凭据或接触日常浏览器/profile。

## KG-b：Unicode/casefold 连续证据匹配（2026-07-26 13:19 JST）——待 Claude 只读复审

- **结果**：KG-b 已完成。`scripts/kg/concept_node_service.py` 新增唯一比较键
  `_evidence_match_key()`，与 PageBrief 生成侧现有语义一致：
  `NFKC → 去 Unicode 空白 → casefold`。quote 仍以普通子串方式在 sourceText 的同规则比较键
  中查找，因此只放行纯大小写、全角/兼容字符及 Unicode casefold 等价（例如
  `Straße/STRASSE`）；标点、词序和全部非空白字符继续保留，改写、缺词、插词、调序及非连续
  拼接仍 fail closed。概念名在 quote 内的门禁同步使用同一比较键。
- **身份边界**：持久化 quote、`quoteSha256`、`evidenceId` 的输入继续使用原先区分大小写的
  `_text_key()`，没有把本次比较规则扩散为证据身份迁移，也没有重写既有图、ledger 或 sidecar。
- **改动范围**：
  - 修改：`scripts/kg/concept_node_service.py`；
  - 新增独立合同：`tests/test_concept_node_service_kg_b.py`；
  - KG-a 冻结的 `scripts/kg/gen_page_brief.py`、
    `tests/test_concept_node_service.py`、`tests/test_page_brief_kg_integration.py`
    均未修改。
- **验证证据**：
  - KG-b 独立合同：**2/2 OK**；
  - KG-b + 既有 KG/KG-a 定向，且
    `PYTHONWARNINGS=error::ResourceWarning`：**25/25 OK**；
  - Python 全量：**347/347 OK，skipped=7**；全量仅出现既有非阻断 ResourceWarning，
    定向严格模式无警告；
  - 两个目标文件 AST 解析与行尾空白检查通过。
- **风险与未做事项**：Unicode `casefold` 按标准会把少数不同码点视为大小写等价（如
  `ß/ss`），这是本项明确目标；没有加入模糊匹配、分词匹配或编辑距离。未处理 KG-c/KG-d、
  registry 迁移、冲突 UI 或双设备验收；未部署、发布、提交、推送、重启服务或操作日常
  Chrome/profile。
- **下一负责人**：Claude 仅做只读复审，重点核对生成/服务规范化一致性、连续子串围栏、
  Unicode 扩展边界和证据身份未迁移。Codex 在此冻结 KG-b 两个文件并停止本段实现。

## F1-F4 已审候选部署可分离性审计（2026-07-26 13:19 JST）——部署前阻塞，未写生产

- **已确认候选/目标**：F1-F4 均已获 Claude 只读复审通过，目标产品版本为 `0.2.51`；
  既有部署流程会把 PWA/服务文件写到 `/home/bwicarus/webapp`、共享静态资源写到
  `/var/www/html/static`，随后原子重启 `webapp.service` 与 `voice-rt.service`；扩展渠道指针
  位于 `/var/www/html/static/pdf/bw-reader-webext-test-channel.json`。
- **当前生产与健康**：生产扩展渠道仍为 `0.2.47`
  （SHA-256 `d33b28ff0817f2abcda8296bdcd81d057929eccc79ddfe8a7f7d89261d680c3a`）；
  生产 `pdf_reader.py` 仍为 `_BRIEF_VER=1`；`webapp`、`voice-rt` 均为 active，
  `http://127.0.0.1:5000/login` 返回 200。最近现存回滚副本为
  `/home/bwicarus/deploy-backups/reader/20260725T165018-1550521`
  （manifest 77 条，SHA-256
  `7e3b71a3730286819ea77591219b619909ba7d56038a617f49497dd1dcb2e9d1`）；
  本次因部署前停止，未创建新的 prospective backup。
- **最小阻塞**：当前 F1-F4、基础 KG、KG-a/KG-b 都只存在于同一个大量未提交的共享工作区，
  没有可检出的独立 commit/tag/worktree 快照。项目既有 `scripts/deploy_reader.sh` 是完整
  manifest 部署，不支持只选 F1-F4；它会同时复制当前 `_server_deploy/pdf_reader.py`
  （`_BRIEF_VER=2`），该文件在运行时直接从
  `/home/bwicarus/claude/scripts/kg/{gen_page_brief,concept_node_service}.py`
  启动子进程。即使不把两个 KG 脚本列入 deploy manifest，服务重启后也会动态执行当前未审的
  KG-a/KG-b。`voice-rt` 同样直接运行共享工作区源码。故现在执行既有流程必然把未审改动混入
  激活面，不能满足用户的发布边界。
- **决定**：严格按授权停在部署前，没有调用部署脚本、没有改 channel、没有重启服务，也没有
  人工覆盖/暂存/回退用户工作区。最小安全下一步是先让 Claude 完成 KG-a、KG-b 只读复审，
  之后发布完整同版 `0.2.51`；若必须只发 F1-F4，则需先建立可复现的已审快照并让生产
  PageBrief/KG 依赖固定到该快照，这属于新的发布架构改动，必须另行实现和审查，不能现场拼装。

## 0.2.51 部署前实际检测与 F1-F4 发布边界复核（2026-07-26 13:46 JST）——本地候选就绪，部署前阻塞

- **候选与部署目标**：候选版本为 `0.2.51`；Windows 主包
  `extensions/bw-reader-webext-0.2.51-windows-test.zip` 共 74 文件、1,143,885 bytes，
  SHA-256 `09b1b3a492ed0df7eaecd73fe71ec15c0d1d5305ecc6e2735a57c2c1e08522c7`。
  本地 channel SHA-256
  `aa455f0652c99de13aa565b7027847b60995e045d9f79c4709329af7106a69e2`；
  launcher v6 脚本/双文件 ZIP SHA-256 分别为
  `26c369b5c6760ba10d40bddf75da115fb7617cd5ecc91a08e88ac9bfc54d9587` /
  `748ca383e64db0cc92faefc19dca3eeb84513db5027686e2c2c547c96be900fb`。
  服务/PWA 目标仍为 `/home/bwicarus/webapp` 与 `/var/www/html/static`，扩展生产 channel
  仍指向 `0.2.47`，未改为 `0.2.51`。
- **完整 manifest 与依赖预检**：`reader_deploy_manifest.py` 展开为 **96 项**
  （webapp 30、static 66），source/target 唯一且源码齐全；规范化 TSV SHA-256
  `dedf4bcd2d8df937d6cdbf260b8f185c1ba4ff0fbc4c6e2cf04660fd787dae90`。
  当前生产对比为 50 相同、29 变更、17 缺失，证明这是完整版本切换而非 F1-F4 小补丁。
  依赖盘点：Python 3.13.5、Node 22.22.2、Bash 5.2.37、Playwright 1.60.0、
  Flask 3.1.1、gunicorn 26.0.0、voice venv websockets 16.0；磁盘空间充足。
- **无副作用验证证据**：
  - `release_preflight.py --skip-browser` 的源码/包/合同检查通过：45 个 JS 文件语法、
    42 个统一 runtime 合同文件、IndexedDB 87 条、TextRange 6 条、自动 KG/同步 relay/
    部署 manifest、Windows/Safari 发布管线和网络审计
    （121 文件、254 个调用、233 条既有债、**0 条新增债**）均通过；
  - 部署脚本 `bash -n`、96 项 manifest 预写校验通过；部署前 Python 定向 **58/58**、
    vocab batch protocol **16/16**、Node handoff/service-worker **2/2** 通过；
  - 完整正式 preflight 在临时测试环境中完成到真实浏览器门禁前，最后仅因生产 PWA/
    服务仍为旧 runtime 而按设计阻断：`handoff result version=0.2.51 errors=1 warnings=1`，
    明确列出 `/home/bwicarus/webapp/{app,assistant,voice,pdf_reader,html_reader,...}`
    与候选不一致；生产 channel 未发生变化。该失败不是候选包内容错误，而是禁止
    `0.2.51` 扩展与 `0.2.47` PWA 混测的版本围栏。
- **生产健康与回滚**：13:46 JST 只读复查 `webapp.service`、`voice-rt.service` 均为
  `active (running)`，`http://127.0.0.1:5000/login` 返回 **200**。当前最近备份
  `/home/bwicarus/deploy-backups/reader/20260725T165018-1550521` 只有 77 项，
  不能单独充当本次 96 项切换的完整回滚点；既有部署脚本会在任何写入前创建新的 96 项
  精确备份，但本次因部署前停止，没有制造伪备份或改生产。
- **无法安全分离的硬阻塞**：现有 `deploy_reader.sh` 没有选择性部署/dry-run 模式，必定
  部署当前 `_server_deploy/pdf_reader.py`（`_BRIEF_VER=2`）。该服务文件会从共享脏工作区
  `/home/bwicarus/claude/scripts/kg/` 动态执行
  `gen_page_brief.py` 与 `concept_node_service.py`；`voice-rt.service` 也直接执行共享工作区
  的 relay，均未固定到发布快照。因此复制一个旧树或排除 KG 文件都不能保证运行时不加载
  KG-a/KG-b。更严格地说，生产旧 `pdf_reader.py` 本身已经引用该共享路径，故当前架构甚至
  无法证明“未部署时下一次 PageBrief 子进程也不会看到 KG-a”。
- **可复现点与为何仍不能直接发布**：已只读确认 F4 后、KG-a 前的完整 Git tree
  `001e85b76b4157aa337520294013a18833c6758e`；其中
  `gen_page_brief.py` / `concept_node_service.py` blob 分别为
  `891faca678f6051c901ae899ecb1f15722ac4107` /
  `d59c34a979b329330a31e23106638f1a968a7358`，当前则为
  `6d21555591888d6b698b65d6bf410ae1f64408e0` /
  `377f54505c9c4862d1399672d7bd1aca6cfd500b`。该 tree 能证明历史内容，却不能改变部署后
  仍回读当前共享路径的事实。
- **决定与精确后续步骤**：严格执行“绝不能包含/激活 KG-a/KG-b”的发布边界，已停在部署前；
  没有调用 `deploy_reader.sh`、没有发布 channel、没有重启服务、没有改日常浏览器/profile。
  最小安全解法只有二选一：
  1. 先把 KG 脚本和 voice relay 纳入不可变的版本化生产副本，并让服务只从该副本加载，
     完成本地合同/回归后再发 F1-F4；或
  2. 明确允许整版 `0.2.51` 包含当前 KG-a/KG-b。
  边界解决后，执行顺序必须是：重跑候选/manifest 校验 → 用既有流程部署服务/PWA并取得新的
  96 项回滚备份 → 验证两项 service、`/login` 与既有 E2E → 最后运行
  `publish_test_channel.py --deploy` 切换扩展 channel → 再核对生产 channel 版本、SHA 和
  独立扩展环境；不得先切扩展 channel。
- **下一负责人**：Codex 在发布边界得到明确解法后继续；Claude 的异步审查不是本段门禁。
  当前技术阻塞是运行依赖未固定到发布快照，而不是等待审查。


## Claude 只读复审：KG-a 显式 skip 与推断空结果分流（2026-07-26 13:59 JST）——**通过**

- **四项结论核对全部通过**（`scripts/kg/gen_page_brief.py::_parse`）：
  1. **只有显式 `page_type=skip` 可持久化**：页型回退收紧为
     `pt = "knowledge" if (brief or tags) else ""`，非法/缺失页型且内容全空时保留空页型，
     由 pdf_reader 既有全空门禁拒绝持久化——推断路径再也产生不了 skip。
  2. **空心/非法空结果保持可重试**：空页型 + 全空内容命中上游"临时失败"分支，不写
     `_none_pages`，下次读页重试；无 JSON/解析失败同样返回空页型。
  3. **显式 skip 单次持久化且不进 KG**：`pt != "knowledge"` 时 `concepts` 被清空，
     集成合同覆盖"只生成/持久化一次且永不进入 KG"。
  4. **兼容路径未扩大**：缺页型但有 `brief`/`tags` 仍推断 `knowledge`，与登记一致。
- **存量盘点复核（只读）**：实测 3 个 sidecar、**歧义"skip 且全空"= 0**、`_none_pages` = 0，
  与登记的关键判据一致 → **确认无需一次性迁移、无需 bump `BRIEF_PROMPT_VER/_BRIEF_VER`**。
  一处非阻断偏差：条目总数实测 **46**（登记写 34，`briefs` 顶层键口径差异）；skip=10 与登记
  相同，且迁移结论只取决于歧义数与 `_none_pages`，故不影响结论。
- **验证证据**：定向合同 `tests.test_concept_node_service` + `tests.test_page_brief_kg_integration`
  **23/23 OK**（与登记一致）；Python 全量 `discover -s tests` **347/347 OK, skipped=7**
  （登记 345/345，差异为 KG-a 新增用例后的自然增量，零失败）。
- **非阻断记录**：登记自认的"模型明确但语义错误地返回 skip"属另一项语义质量问题，本段范围外。

## Claude 排查：阅读器 MCP `reading_positions` 外部超时（2026-07-26 13:59 JST）——**本机链路无阻塞，未做修改**

- **复现结论：本机全链路健康，未复现超时，因此不存在可修的阻塞点。**
  - 后端接口带 Bearer 实测：`/pdf/api/reading-pos` **HTTP 200 / 7.9ms / 1664B**，
    比正常的 `/pdf/api/list-pdfs`（200 / 68ms / 9487B）**更快**；路由 GET 分支只读一个
    JSON（`pdf_reader.py:2572-2579`），无 DB/锁/外部调用（写锁只在 POST 分支）。
  - MCP 进程内直调两个工具：`list_books` 0.05s（25 本）、`reading_positions` **0.01s（14 本）**，
    后处理是纯内存排序，无阻塞面。
  - 服务与传输层正常：`mcp-server` active（PID 1136，`--http 8766`），nginx `/mcp` →
    127.0.0.1:8766 一致，端点即时响应（无凭据时 401 / 4ms，非挂起）。
    注：8765 被 Anki 占用属无关巧合，MCP 与 nginx 均不使用该端口。
- **可疑面（需外部客户端侧确认，本机无法判定）**：MCP 服务日志显示外部会话
  13:13:33-35 完成 initialize/ListTools/ListResources/ListPrompts 后**没有任何
  CallToolRequest 到达**（全量日志 CallTool 仅 2 条，均为 13:10 的其它调用）。即
  "超时"的调用很可能**未抵达服务端**，倾向外部客户端/网关侧（会话保持、`mcp-session-id`
  透传、SSE 长连接或客户端自身超时），而非阅读器 MCP。
- **未修改任何文件**；未部署/发布/提交/推送/重启服务；未改扩展、同步项目或浏览器配置；
  未索取或使用外部 MCP 凭据（端到端 MCP 会话探测因本机 token 与外部端点不同源而 401，
  按边界停止，不索凭据）。
- **建议下一步（交协调器/用户决定）**：① 由外部客户端侧抓取失败调用的请求/响应与超时值，
  确认是否发出及是否带 `mcp-session-id`；② 若确有请求到达而未被记录，再在 MCP 侧加调用日志
  定位；③ 本机侧无需改动，**不建议**盲目改 `httpx timeout=60.0` 或路由——那会掩盖真实原因。

## 0.2.51 Windows 既有专用 Chrome 普通网页真机冒烟（2026-07-26 14:05 JST）——通过

- **环境与制品核对**：按用户最新规则优先使用 Windows 真实浏览器，而非 Pi Chromium。
  仅使用固定专用 profile
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\browser-profile-v2` 与固定 unpacked
  目录 `...\BWReaderExtensionTest\extension`；Chrome 150.0.7871.182，扩展 ID
  `jddhhakcblmihidgdobfkcejjinpigak`。Windows 目录 74 个文件与
  `bw-reader-webext-0.2.51-windows-test.zip` 逐文件 SHA 一致，主包 SHA-256 仍为
  `09b1b3a492ed0df7eaecd73fe71ec15c0d1d5305ecc6e2735a57c2c1e08522c7`。
  未观察到日常 Chrome profile 进程。
- **加载边界**：首次连接时磁盘 manifest 已为 `0.2.51`，但固定 profile 的旧 service worker
  尚未 reload，`owner-lease/1` 依赖为 false；这与交接文档“替换 unpacked 后必须在
  `chrome://extensions` 重新加载”一致。只对专用 BW 扩展执行一次等价
  `chrome.runtime.reload()` 后，重新连接到新 worker，未修改日常 profile。
- **真机结果**：
  - popup 实测 ID/版本/名称正确；后台 IndexedDB、DataRegistry、`owner-lease/1`
    三项依赖全为 true；
  - `https://example.com/` 普通网页完整加载 facade、web adapter、共享 shell、侧栏/把手、
    highlights、pins、ink、notes 与 settings-sync；PWA bridge/provider-only 均为 false，
    书籍专属按钮隐藏禁用、网页按钮可见可用；
  - 页面只有 1 个 `#bw-reader-host` 和 1 个 `#bw-reader-pins`，开放 Shadow DOM；
    宿主 `pointer-events:none`，页面卡片层为文档坐标 `position:absolute`；
  - 原网页按钮真实点击恰为 1 次，输入框输入成功；点击空白未选中最近词、未打开查词框；
    原生滚轮一次滚动 900px；侧栏把手可打开，实测宽度 523px；
  - 页面 reload 后 host/pin 仍各 1 个，无重复注入；第二标签页独立注入正常；
    `pageerror=0`、console error=0。
- **清理与未做事项**：测试结束后只终止 `BW Codex Chrome Test` 计划任务启动的专用 Chrome；
  任务恢复 `Ready`，`127.0.0.1:9222` 已关闭，系统中 Chrome 进程为 0；SSH 隧道已停止。
  没有运行生产 channel 启动器（`installed-version.txt` 仍为 0.2.47，避免其把候选覆盖回
  线上 0.2.47），没有部署、发布或重启 Pi 服务。线上 PWA 仍为旧 runtime，故本轮没有混测
  PDF/EPUB/HTML/Favorite 接管；Surface Pen/真实触摸的硬件仲裁仍须按
  `windows/SURFACE-PEN-CHECKLIST.md` 人工操作，CDP 不能伪装成真笔验收。
- **下一步**：后续凡普通网页/扩展行为需要实际验证，继续优先使用这套 Windows 专用环境；
  四类 PWA 接管只在服务/PWA 与扩展同为 0.2.51 后测试。F1-F4 部署仍受上一节“运行依赖未固定
  到发布快照”的技术阻塞约束，本次真机通过不改变该发布决定。

- 2026-07-26 15:22 JST：用户解除 KG-a/KG-b 文件冻结并授权 Codex 独立连续完成 KG
  后续。Codex 现认领 KG-c：修复 `gen_page_brief.py` 在当前 PDF mtime 的 char-cache
  缺失/损坏时回退任意旧 mtime cache 的陈旧证据窗口；只接受当前 mtime 的有效 cache，
  否则回退当前 PDF 文字层。将新增独立定向合同，并重跑 KG-a/KG-b/KG-c 与必要全量回归。
  本段不部署、不发布、不提交/推送，不触碰日常 Chrome/profile；完成登记后立即继续 KG-d。

## KG-c：PageBrief 陈旧 char-cache 回退围栏（2026-07-26 15:31 JST）——完成

- **结果**：`scripts/kg/gen_page_brief.py::_char_cache_text()` 现在只读取精确匹配当前
  PDF `mtime` 的 char-cache；当前 mtime 无法取得、cache 缺失、损坏或内容为空时，继续由
  `_page_text()` 读取当前 PDF 文字层，不再退回同一页任意旧 mtime cache。这样替换 PDF、
  重做 OCR 或缓存清理后的旧证据不能再被 PageBrief/KG 复用；当前 mtime 的正常 cache
  仍优先使用，原有 ruby/换行清理未改。
- **改动范围**：
  - 修改 `scripts/kg/gen_page_brief.py`；
  - 新增独立合同 `tests/test_page_brief_char_cache_kg_c.py`，覆盖仅旧 cache、当前 cache、
    当前 cache 损坏且旧 cache 存在、mtime 不可证明四条路径。
- **验证证据**：
  - KG-a/KG-b/KG-c + PageBrief 集成定向，严格
    `PYTHONWARNINGS=error::ResourceWarning`：**29/29 OK**；
  - 目标文件 `py_compile` 与行尾空白检查通过；
  - 首次受限沙箱全量中，只有 4 个既有回环 socket 测试因 `PermissionError` 失败；
    按相同命令在允许绑定本机回环端口的隔离外环境复跑：
    `python3 -m unittest discover -s tests -p 'test_*.py'`
    → **365/365 OK，skipped=7**。
- **风险与边界**：本段只收紧缓存代际，不改变 `BRIEF_PROMPT_VER/_BRIEF_VER`，也不清理
  旧 cache；旧文件只是永远不再被当前 mtime 误读。没有部署、发布、提交/推送或触碰
  日常 Chrome/profile。服务正式生效仍取决于后续发布快照边界。

- 2026-07-26 15:31 JST：Codex 已完成并登记 KG-c，立即认领 KG-d。目标是移除
  PageBrief mutation/source 身份对整页 `pageTextSha256` 的不必要依赖，使同页仅因 OCR、
  空白或无关正文漂移时不会重复计 concept signal，同时继续逐字复核当前 sourceText，
  允许真实新增/变更 concept 被处理，并兼容已存在的旧 PageBrief provenance。将新增独立
  KG-d 合同并重跑 KG-a～d 与必要全量回归；本段仍不部署、不发布、不提交/推送。

## KG-d：PageBrief 来源身份与存量证据幂等（2026-07-26 15:38 JST）——完成

- **结果**：`promote_page_brief()` 的 mutation/source 身份现在由
  `file + page + semantic_brief` 派生，整页 `pageTextSha256` 只保留为当前证据复核输入，
  不再参与身份。相同语义结果遇到 OCR、空白、页眉页脚或无关正文漂移会直接 replay；
  `upsert_candidates()` 另增加仅限 `page-brief` 的存量兼容去重，在同一解析后节点内按
  `documentRef + page + NFKC/去空白/casefold 后的连续 quote` 识别同一页同一证据，
  因而旧版含 pageTextSha 的 sourceId 首次进入新算法只新增一条 mutation receipt，
  不重复增加节点 signal/provenance。
- **围栏保持**：
  - 当前 `sourceText` 的逐字证据校验仍发生在 mutation replay 之前，旧成功 receipt
    不能绕过当前页面证据；
  - mutation 未固定为 file+page，semantic brief 增加真实 concept 时仍产生新 mutation；
    旧 concept 同证据去重，新 concept 正常写入；
  - 真正不同 quote、不同 page/documentRef 仍分别计 signal；note 等非 PageBrief 来源完全
    沿用原 sourceId/evidenceId 规则；没有删除或自动回滚既有概念。
- **改动范围**：
  - 修改 `scripts/kg/concept_node_service.py`；
  - 新增 `tests/test_concept_node_service_kg_d.py`，共 9 条 KG-d 独立合同，含旧存量兼容、
    当前原文重验、语义扩展、真实 quote/页/书边界、非 PageBrief 隔离、只改 brief/tags
    与两个不同 mutation 并发同证据。
- **验证证据**：
  - KG-a/KG-b/KG-c/KG-d + PageBrief 集成严格定向：**38/38 OK**；
  - 目标文件 `py_compile` 与行尾空白检查通过；
  - 允许回环 socket 的隔离外 Python 全量：
    `python3 -m unittest discover -s tests -p 'test_*.py'`
    → **374/374 OK，skipped=7**。
- **风险与未做事项**：存量首次转换会占用一条新的 mutation receipt，但节点计数不变；
  receipt/provenance 有界淘汰后的长期重放、PDF 重命名后的 PageBrief/KG provenance
  仍是后续 KG 待办。没有部署、发布、提交/推送、重启服务或触碰日常 Chrome/profile。
  服务正式生效仍受不可变发布快照边界约束。

- 2026-07-26 15:47 JST：仓内没有既定 `KG-e` 名称；为连续推进，Codex 将架构中下一项
  明确 KG 待办“PDF 重命名后的 PageBrief/KG provenance 迁移”登记为 **KG-e** 并认领。
  协议边界为：`evidenceId/sourceId/nodeId/signal` 是不可变历史身份，不因路径改名重算；
  `documentRef` 与节点 `books` 是可迁移路径投影。重命名时在 ConceptNodeService 跨进程锁
  与 mutation journal 内迁移投影，PageBrief sidecar（含 pending/synced/skip）和书级开关
  走可回滚计划；目的冲突、在途生成或关键迁移失败必须 fail closed，不能返回伪成功或重跑
  AI/KG。将补服务合同、rename 集成合同、失败回滚及 KG-a～e/全量回归；本段不部署/发布。


## Claude 修复：阅读器高亮三 bug + 收敛为唯一实现（2026-07-26 15:33 JST）

- **根因（三者同源：高亮未收敛为唯一实现）**
  1. **助手高亮遮字**：`assistant.py:3205` `color = args.get("color") or "#fff59d"` 无格式校验，
     外层 agent 传 `color="yellow"`（MCP `add_highlight` docstring 未约束格式）；渲染端
     `reader.src/17-highlight.js:51 _hlRgba` 只对 `#rrggbb` 加 alpha，命名色 `return hex` 原样
     透传 → `background:yellow`（alpha=1）实色块。`.hl-layer` 的 `z-index:5` 造层叠上下文，
     `.hl-saved{mix-blend-mode:multiply}` 混不到底下页位图，**正文可读性完全依赖 alpha 0.4**，
     alpha=1 即遮字。手动路径走色板 hex 故正常。实证：用户 sidecar 里三条 `"color":"yellow"`。
  2. **iPad 三击**：`17-highlight.js` 把 `RC.highlight.gesture()` 实例建在 **per-rect 循环体内**，
     而一条高亮有多个**互相重叠**的 rect div（实测 rects 3~12 个且嵌套）→ 两次点落在不同 div
     各自算「第一击」；原生 dblclick 因 target 不同派发到公共祖先而无人接 → 恰好需要三击。
     EPUB（`epub-html.js:711`）早就是 document 委托单实例，无此问题。
  3. **圆点拉成细长条**：共享层 `rc-highlight.js` 的 `.rc-hl-sw-i` 抄漏了原生
     `pdf-styles.css:587 .swatch` 的 `flex-shrink:0`，`.rc-hl-sw` 漏 `flex-wrap`、`.rc-hl-pop`
     漏 `min-width` → 窄视口/色板变多时被等比压窄、高度不变 = 竖椭圆。
- **统一边界（本次收敛结果）**
  - **数据模型唯一**：服务端新增 `pdf_reader.py::HL_PALETTE` + `hl_norm_color()`，与前端
    `DEFAULT_HL_COLORS` 逐色一致；**创建/编辑/重做 4 个写入点全部经它归一化**，命名色/非法值
    一律映射到色板 hex —— 不透明色再也进不了 sidecar（助手、手动、MCP 同一规范）。
  - **交互路径唯一**：PDF 改为**模块级单例** `_hlGestureSingleton` + `_openHlEditorById(id)`
    按 id 反查（对齐 EPUB），并加 document 级 dblclick 委托兜底；`RC.highlight.gesture` 的
    `cancel()` 改为保留首击（iOS 滚动候选误吞）。
  - **渲染/样式唯一**：仍只有 `.hl-saved` 一个组件类与共享 `rc-hl-pop` 弹层；助手侧零自带
    CSS/DOM（门禁断言）。共享层补齐三条护栏：`flex:0 0 auto`+`min-width`+`aspect-ratio:1/1`
    （圆点 28px 触控盒）、`flex-wrap:wrap`、弹层 `min-width:min(280px,92vw)`。
- **验证证据**
  - 新增门禁 `tests/test_highlight_unified_contract.py` **14/14 OK**：色板服务端/前端逐色一致、
    禁止绕过归一化的写入点、命名色/空/非法值行为级归一、手势单例且循环内不得建实例、
    双击按 id 反查、dblclick 委托、reader.js 已重建含修复、圆点 shrink/aspect/触控盒/换行/
    弹层 min-width、助手侧无自带高亮 CSS、渲染统一加 alpha。
  - `build_pdf_reader_js.sh` 重建 reader.js（34 源文件）+ `check_pdf_reader_js.sh` ✅ JS OK；
    `rc-highlight.js` `node --check` 通过；`pdf_reader.py` 语法通过。
  - **端到端实测**：模拟助手写入 `color="yellow"` → 服务端返回 `"color":"#fff59d"`（已归一化，
    测试高亮已清理）；读回该书三条历史高亮颜色分布 `{'#fff59d': 3}`、非 hex 残留 0。
  - **存量脏数据已洗**：3 条 `yellow` → `#fff59d`（备份于 scratchpad `hl_backup.json`）。
  - 部署：`reader.js`/`rc-highlight.js` → nginx static（diff 一致）、`pdf_reader.py` → webapp
    并重启（重启前已确认 4 分钟内无活跃语音通话；服务 active、/login 200）。
- **范围与边界**：只改阅读器高亮相关（`pdf_reader.py` 高亮写入、`reader.src/17-highlight.js`、
  共享 `rc-highlight.js`）+ 新增门禁；**未改扩展或同步项目**，未提交/推送。
- **待用户在 iPad 真机确认**：双击弹编辑框（不再三击）、圆点为正圆且好点、助手高亮不遮字。
- **附带发现（未修，供排期）**：助手高亮卡的「重做」POST 丢 `page_w/page_h`
  （`reader.src/25-assistant.js:1140` 与 `:1194`），普通 PDF 无感，但**去边/vbook/旋转页会错位**；
  另 `html-reader.js:490` 直接 `background = h.color` 完全不加 alpha（同类洞，HTML 宿主）。


## Claude 修复：高亮编辑弹窗元素重叠（2026-07-26 15:56 JST）——**跨模块 CSS class 碰撞**

- **根因（与"圆点细长条"同源，且纠正上一轮的错误归因）**：
  `rc-turncard.js:155` 的**裸选择器** `.rc-hl-sw{width:10px;height:10px}` 与共享高亮弹层
  `rc-highlight.js:31` 的 `.rc-hl-pop .rc-hl-sw`（色板行**布局容器**）**同名**。后者特异性虽高，
  但它从不声明 width/height → turncard 那条**无人竞争地生效**，把应为 358×28 的容器压成 10×10。
  该 CSS 由 `_hlCss()` 在任何一次带编排的助手回合渲染后注入 → 解释了"刚开页正常、用过助手后坏"。
  实测（Playwright 768×1024）：色板行 `clientHeight=10 / scrollHeight=188`，溢出 178px，
  产生 **4 处真实重叠**（🎨标签↔textarea、圆点2↔textarea、圆点3/4↔按钮行），第 4 个圆点掉出弹层。
  **上一轮的归因"抄漏 flex-shrink"是错的**：`flex:0 0 auto`+`flex-wrap` 只是把"横向压扁"
  转成"纵向溢出"（放大器），病根一直是命名碰撞（git diff 证实该 CSS 在 HEAD 即存在）。
- **最小修复**：
  - **A（治本）** `rc-turncard.js:154-157`：`.rc-hl-row/.rc-hl-sw/.rc-hl-tx` 三条加祖先作用域
    `.rc-hlcard .*`；**`.rc-hl-b` 两条保持裸选择器**（⚙ 齿轮挂在 `.rc-flow-meta`、在 `.rc-hlcard`
    外，加作用域会掉样式）。连带修好高亮**列表**行被 turncard 的 `white-space:nowrap` 截断。
  - **B（对齐原生 + 纵深防御）** `rc-highlight.js`：`.rc-hl-pop .rc-hl-sw` 显式
    `width:auto;height:auto;min-height:0`（对同名弱选择器免疫）；`.rc-hl-note` 由固定
    `width:200px` 改为 `width:100%;min-height:48px;resize:vertical`（逐字对齐原生
    `pdf-styles.css:589 #hl-popover textarea`，顺带修 textarea 只占半宽）；删除 3 处恒不生效的
    `max-width:74vw`（父已有 max-width，vw 只会误导排查）。
- **回归门禁（`tests/test_highlight_unified_contract.py` 扩到 18 条，全通过）**：新增
  `HighlightPopoverNoCssCollision` 四条判据——① **其它 `rc-*.js` 不得以裸选择器使用弹层同名
  class**（`.rc-hl-b` 白名单例外）② 布局容器显式 auto ③ textarea 对齐原生且禁固定像素宽
  ④ 弹层后代禁用 vw 宽度。并**纠正测试 docstring 中 ③ 的错误归因**。
  **反向验证**：临时回退修复 A → 门禁失败并精确报 `rc-turncard.js: '.rc-hl-sw…' 未加祖先作用域`；
  恢复后通过 —— 证明门禁能真正抓到该 bug、非摆设。
- **验证与部署**：`check_pdf_reader_js.sh` ✅；Node 合同全量 **457/457**、Python 全量
  **383/383（skip 7）**、高亮门禁 **18/18**。`rc-highlight.js`/`rc-turncard.js` 已 cp 到
  `/var/www/html/static/pdf/`（diff 一致）；两者均在 `_PDF_SHARED_CACHE_ASSETS` 清单内，
  版本戳随 mtime 自动 bust（不踩 immutable 缓存盲区）。未改后端、未重启服务。
- **待用户 iPad 真机确认**：弹窗内元素不再重叠、备注框满宽、圆点成排可点。


## Claude 调研+最小实验：Pi→Windows 上下文快照（SSH 同步）（2026-07-26 16:06 JST）

### 定位（用户拍板，方案主轴）
Windows 上一份**唯一、长期保留**的 Markdown 快照，由 Pi 经 **SSH** 在状态变化后原子更新；图片作为
**同目录本地资产 + 相对链接**。本地 Codex **每轮开局先读快照**建立上下文（在读哪本/哪页/助手在聊
什么/有什么产物）。**快照不是唯一事实源、更不替代 MCP**：问下一页或精确内容、查实时选区、要翻页/
高亮/编辑/执行操作 —— 一律走阅读器 MCP。不把每个 MCP 接口当每轮常规依赖。

### 为什么是 SSH 不是 SSE（比较，非实施目标）
SSE 是**服务器→浏览器**的单向推流，落不了 Windows 文件系统，仍需本地常驻进程接收并写盘（多一个
故障点与鉴权面）；SSH 通路**已存在且免密可用**（`scripts/push_big_files_to_pc.py` 现成样板），
直接 scp+远程 rename 即可完成"落盘 + 原子切换"，无需在 Windows 常驻服务。SSE 仅在阅读器内部
（`reader_events.py` 总线）用作**变化触发源**，不作为跨机传输。

### 关键调研事实（决定方案边界）
- **服务端位置只有一个标量**：`state/reader-positions.json[rel] = {kind,pos,ts}`；PDF=1-based 页码、
  EPUB=0-based section idx，**均不含页内偏移**（偏移只在浏览器 localStorage）；**HTML 不参与**，
  只有 `state/web-last*.json` 的"上次打开的文件"。
- **"用户此刻看到的正文"只存在于前端 `ctx.visible_text`**（服务端唯一消费点 `assistant.py:5528`），
  服务端无法自取 → **这是快照的精度上限**，也正是必须保留 MCP 实时读取的根本原因。
- 当前页内容：PDF 有落盘 `state/pdf-page-brief/`（brief/tags/concepts/page_type）；**EPUB/HTML 无等价
  落盘物**（EPUB 只有现算 `summarize_section`）。
- 助手对话：`state/assistant-convo/*.json`，list of `{role,content,ts,page,file_rel,via}`，
  实测仅 20K/3 文件 → **可完整内嵌快照，无需压缩**（符合"尽量长保留"）。
- 标注数据分散于 `reader-notes`/`pdf-highlights`(sha40)/`epub-highlights`/`html-highlights`/
  `pdf-ink`/`epub-ink`/`reader-userpages` 等，命名规则不统一（sha16 vs sha40 vs book_sha）。
- **图片资产 `state/pdf-page-img` 实测 5.2G**，单页图 267–588KB → **禁止整目录同步**，快照只带
  "当前页那一张"。

### 最小端到端实验（已按授权实施，全部通过）
- 产物（**仅在 /tmp scratchpad，未进项目目录、未部署、未改任何现有文件/服务**）：
  `ctx_snapshot.py`（只读 state/ 生成 Markdown+资产）、`ctx_push.sh`（SSH 原子同步）、`verify.ps1`。
- **硬前提实测**：SSH 裸连 **0.49s**（ControlMaster 收益不显著，瓶颈在 Windows `cmd` 启动）；
  Windows 原子覆盖 `tmp → Move-Item -Force` **25 轮零损坏/零缺失**。
- **端到端结果**：Pi 生成 → SSH 同步 → Windows 落地，**耗时 2.0s**；
  SHA-256 `de2811acbf56ec44` **两端一致**、字节 4217 一致、图片相对链接 `assets/current-page.jpg`
  (238KB) 可解析、中文标题正常、**原子切换无 .tmp 残留**、MCP 分工声明已内嵌文档。
- 落地目录：`C:\Users\bwica\bw-reader-context\{context.md, assets/}`（探针目录
  `bw-reader-context-probe` 可清理）。**未碰日常 Chrome/PWA/扩展/同步项目**。
- 快照章节结构（已实现）：当前在读 → 当前页要点(PDF brief) → 当前页图 → 侧栏助手对话(完整) →
  最近书目 → **"何时改用 MCP"对照表**（精确正文/实时选区/操作/位置校验四类明确指向 MCP）。

### 后续分阶段计划（尚未实施，等确认）
1. **阶段1（已完成雏形）**：手动触发的快照生成+同步。
2. **阶段2**：接 `reader_events` 总线做变化触发 + debounce（建议 3–5s），常驻小进程或 systemd timer；
   失败重试与 PC 睡眠/断线时静默跳过（照 `push_big_files_to_pc.py` 的可达性探测惯例）。
3. **阶段3**：补 EPUB/HTML 宿主字段（EPUB 章节标题/进度、HTML 最近文件）、笔记/高亮/手写摘要段、
   任务与产物段；资产去重（按内容 sha 命名 + 保留窗口）。
4. **阶段4**：隐私与保留策略（只发当前账户、可配置排除书目）、断连恢复（本地保留最近 N 份）。
- **暂不做**：整目录/全量同步、生产部署 —— 按授权仅限最小实验通过后再议。

### 风险与回滚
- 回滚 = 删除 Windows 目录 + 停止调用脚本（脚本在 /tmp，不影响任何现有服务）。
- 已知风险：PC 睡眠/Tailscale 断线（需可达性探测）、图片长期累积（需保留窗口）、
  快照被误当实时真值（已在文档顶部与末尾双处声明 MCP 分工）。


## Claude 补正：Windows 原子写法修正 + SSH 通路调研补充（2026-07-26 16:13 JST）

### ⚠ 修正上一节的实验做法（重要，影响后续实施）
上节最小实验用 `Move-Item -Force` 做原子切换、25 轮通过——但那是在**没有读者持有文件**的条件下。
本轮补测证实 **Windows ≠ POSIX**：

| 条件 | `MoveFileEx(REPLACE_EXISTING\|WRITE_THROUGH)` | `Move-Item -Force` / `cmd move /Y` |
|---|---|---|
| 无读者 | OK | OK |
| **Codex 正持有文档读取** | **失败 win32=5（干净失败，目标保持旧版完整）** | "成功"，但走**先删后改名**的非原子路径 → 存在**文件消失窗口** |

即 `Move-Item -Force` 比干净失败**更危险**（读者可能撞上文件不存在）。**已把同步器改为
`MoveFileEx` P/Invoke + 重试 8×150ms**，并复测：
- 无读者：`ATOMIC_OK attempt=0`（首次即成功）；
- **读者持有中**：`ATOMIC_BUSY` 安全退让，目标文档保持旧版完整、**无 .tmp 残留**（`tmp_leftover=0`）；
- 读者释放后重推：`ATOMIC_OK attempt=0`。
- 失败即放弃本轮并置 dirty，下一轮全量覆盖——**快照语义，不需要 outbox 队列**（与 local-first
  写路径的关键区别，避免过度设计）。
- 另注：PowerShell 5.1 = .NET Framework，**无 .NET Core 的 3 参数 `File.Move`**；PS 脚本须
  UTF-8 BOM 落盘再传，heredoc 里的反引号会被吃掉。

### SSH 通路调研补充（实测数据）
- Tailscale **direct 局域网直连**（非 DERP），ICMP RTT ~1.0ms；冷连接中位 **511ms**，
  **ControlMaster 复用 292ms**；完整原子更新（scp+ssh）**540ms（复用）/ 928ms（不复用）**。
- **50KB 与 300KB 传输同价（265 vs 272ms）** → 成本在**往返次数**不在字节数；优化方向是合并
  SSH 调用（一轮压到 2 次往返），不是压缩内容。建议建 `~/.ssh/config` 启用 ControlMaster（当前无该文件）。
- **反向通路不存在**（PC→Pi 无免密）→ 只能 Pi 主动推；且 Pi 永远在线、掌握"何时有变化"，推模型本就更合理。
- ⚠ **SSH 会话身份是 Administrator=True** → 推送脚本必须**硬编码目标目录白名单**，书名等外部字符串
  拼进 Windows 路径前严格清洗（`..`/`:` 是注入面）。
- PC 不可达时**静默跳过**（照 `push_big_files_to_pc.py:34-40,57-59`）；注意失败探测会阻塞 ConnectTimeout
  秒，不可达应退避到 60s 一探，别每轮硬撞。

### 触发源关键缺口（决定阶段 2 架构）
`reader_events.py` SSE 总线**只覆盖结构/内容类变更**（ink/text/userpage-del/client-action/run/
assistant-history/fav*），**恰恰漏掉最需要跟随的阅读状态**：reading-pos、read-dwell、高亮 CRUD、
便签 CRUD **均无 publish**。且外部订阅 HTTP SSE 要占用户 per-uid 4 个名额之一 + 1 个 gthread，
还需自实现 120s 重连。
→ **阶段 2 建议改用 inotify 监听 Pi 本地 sidecar 文件**（`reader-positions.json`、`attention/dwell.jsonl`、
`reader-notes/`、`*-highlights/`、`assistant-convo/` 等，均为原子 replace 写入）：拿到 100% 信号、
零 webapp 耦合、不占任何 webapp 资源。项目已有 watchdog 先例（`_client/core/watcher.py`）。
建议 **debounce 10s + 单次在途 + 合并**（上游 reading-pos 本身 ≥5s 节流、dwell 事件间隔实测 6–20s；
10s 周期下 SSH 占空比约 5%）。

### 上下文字段补充（供快照扩展）
- 助手历史：`assistant-convo/&lt;uid&gt;.json` **按账户不分书**、硬上限 200 条、溢出归档到
  `assistant-convo-archive/*.jsonl`（保留 180 天）；压缩非自动（挂断通话触发、&lt;14 轮跳过、保留最近 6 轮）。
  ⚠ **EPUB 侧 `epub-convo/` 同样 200 条截断但无归档**——溢出即永久丢失，与 PDF 不对称（已知缺陷，非本次范围）。
- 任务/产物最佳信号是 `state/cli-tasks/*.json`（逐任务轨迹 kind/status/step/result）与
  `assistant-creations/&lt;uid&gt;.json`（创造物库）；`active_tasks.json` 只是通用残留。
- 讨论主旨可用 `attention/focus.json`（热点词带书页引用）+ `attention/dwell.jsonl`。
- `assistant_log_chat`（MCP → `/api/assistant/log`）是 **Codex 回写讨论要点的现成通道**。
- 图片：`pdf-page-img` 单文件 p50 **552KB**；`reader-toolshots/`（工具截图，sha1 命名）是"最新图像"的
  另一来源。建议快照资产目录常驻 **&lt; 5MB**（当前页图 1 张 + 最近 toolshot 1 张），沿用原文件名天然去重。

### 状态
最小实验链路已按修正后的原子写法跑通并复测；脚本仍在 /tmp scratchpad，**未进项目目录、未部署、
未接触扩展/同步项目/日常浏览器**。回滚 = 删 Windows 目录 + 停止调用。

## KG-e：PDF 重命名的 PageBrief/KG 路径事务（2026-07-26 16:32 JST）——完成

- **结果**：PDF 改名现在由 `pdf-page-brief-rename/1` 持久 intent 串联 PDF、PageBrief、
  书级开关与 ConceptNodeService 路径投影。`nodeId/evidenceId/sourceId/signal` 保持不变，
  只迁移 `documentRef` 和能被现存证据完整证明属于 PageBrief 的 `node.books`；混合
  `autonote`、非 PageBrief 来源或 provenance 已淘汰时保守保留旧书投影。
- **崩溃/回滚语义**：
  - intent 保存来源 PageBrief 的原始字节及摘要；回滚先逐字节恢复并复核唯一副本，再切回 PDF，
    不会因旧 sidecar 意外消失而删除目标唯一副本；
  - graph replace / commit append 窗口经节点服务 journal recovery 判定，`applied` 继续向前、
    `absent` 安全回滚、无法证明则保留 intent 并 fail closed；
  - 已提交重试只验证 PDF 身份、旧 sidecar 不再存在及当前目标 PageBrief/书级开关的路径结构，
    不冻结后续正常新增页、`kg_status` 更新或用户切换开关；
  - 旧高亮/便签/墨迹/缓存等 sidecar 改为无覆盖搬迁，目标冲突保留双方并 409；完成标记写回
    intent，后续 committed replay 不会再次用旧路径数据覆盖新路径。
- **改动范围**：`scripts/kg/concept_node_service.py`、`_server_deploy/pdf_reader.py`、
  `tests/test_concept_node_service_kg_e.py`、`tests/test_page_brief_rename_kg_e.py`，
  并同步更新 `reader-runtime-architecture.md`、`reader-extension-handoff.md`。
- **验证证据**：
  - KG-a～e、PageBrief 集成及 rename 事务定向：**63/63 OK**；
  - Python 全量（允许测试绑定本机临时回环端口）：
    `PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -p 'test_*.py'`
    → **403/403 OK，skipped=7**；
  - 目标文件 `py_compile`、`git diff --check` 通过。全量输出仍有两个既有测试资源未显式关闭的
    ResourceWarning 文本，但 unittest 结果为通过，本段未扩范围修复。
- **风险与边界**：当前生产为单 Gunicorn worker，现有锁序未见死锁；若未来多 worker/双实例共享
  state，`_brief_inflight` 仍只是进程内，需要升级为跨进程生成租约。PDF 身份目前使用
  size+mtimeNs，弱于全文件 hash/inode；旧 sidecar 的原子 no-clobber 文件搬迁依赖同一文件系统，
  跨文件系统会安全失败而不覆盖。没有部署、发布、提交/推送、重启服务或触碰日常 Chrome/profile。
- **下一负责人**：Codex 按用户授权立即继续下一项 KG 长期重放：解决 mutation receipt 与
  provenance 有界淘汰后，旧成功 mutation 的稳定重放、不同 payload 冲突与历史 evidence 去重。


## ✅ 生产部署完成:无 AI 直接命令 + 出向 focus/drawing + 事件日志(2026-07-28 15:2x JST)

用户明确授权本次生产部署。已按项目既有原子流程 `scripts/deploy_reader.sh` 完成,
仅重启该流程要求的服务;**旧 MCP / 旧 AI / 网页路径全部保留且实测可用**。

### Release 与回滚
- `reader=0.2.67` · `kg=kg-0.2.67-3a54cd45588e2d56f3f9`
- **回滚点**:`/home/bwicarus/deploy-backups/reader/20260728T062558Z-938239`(普通文件精确备份 + KG 状态取证快照;本次部署事务创建)
- 上线文件 sha256(前 16):
  `reader_card_contract.py=21dac6a1674667b5` · `reader_direct_commands.py=1ca2121716a660b1` ·
  `reader_direct_wire.py=4e1f5266f6257750` · `reader_outgoing_context.py=d35b9e285946fb1a` ·
  静态 `rc-core.js=7b857f672238f033`

### 预检拦下的三个真问题(**未放宽任何门禁**,全部补齐后才放行)
1. **三个新服务端模块不在部署清单** —— 清单是显式白名单,不补则 `pdf_reader` 会 import 到
   不存在的模块(上次 `reader_card_contract.py` 已踩过一次)。已补,并全量自查 pdf_reader 的
   本地依赖(另两个 `pdf_render_worker`/`youtube_subtitles` 是既有模块、已在生产、非本批)。
2. **6 条新路由缺 vbook 策略** —— `test_vbook_route_policy` 直接拦下,已按 `GLOBAL` 登记。
3. **网络审计 2 条新债务** —— 部署在替换生产文件**之前**干净中止(生产未改、服务全程 active)。
   补三条交互策略(`context.focus.report` / `context.drawing.revision` / `context.outgoing.journal`)
   + 两处 `@interaction` 标注后归零。
   ⚠ 我自己的疏忽:预检时用 `grep -o "[0-9]* new debt"` 读结果,把 `2 new debt` 误读成 0 —— 
   "用错误方式确认门禁通过"比门禁失败更危险,已改为读完整行。

### 生产事件源(Windows 消费入口)
- **端点**:`GET https://bwicarus.taile44d0c.ts.net/pdf/api/outgoing/journal?since=<cursor>&limit=<=500`
- **落盘**:`/home/bwicarus/webapp/data/reader-sidecars/by-user/<uid>/reader-outgoing-journal.jsonl`
  (账户分区;现网已有 7 行真实事件)
- **认证**:复用阅读器既有会话鉴权。⚠ **客户端必须禁用重定向跟随**:未认证时返回
  **302 → `/login`**;若跟随,会拿到登录页的 200,naive 客户端会读成"成功但没有事件" ——
  这是最难查的静默错误。客户端规则:仅当 `200 + JSON` 才解析,`302/401/403` 一律判为未认证。
- **事件形态**:`focus`(set/cancel)、`drawing`(stable)、`command-failed`,与
  `reader-specs/fixtures/outgoing-events.jsonl` 同构,同一解析器可用。
- **游标语义**:`seq` 单调;客户端持久化上次 `cursor`,以 `since=cursor` 增量拉取;
  同 `seq` 只处理一次即幂等。**保留上限 2000 条**;游标落后于保留窗口时返回 `gap: true` 并
  给 `head`,必须重新对齐而不是假装连续。**日志损坏返回 500 并说明,绝不静默返回空**。
- 最小客户端配置:base URL + 会话凭据 + 持久化 cursor + 禁跟随重定向 + 轮询间隔(建议 2–5s)。

### 部署后验证(生产,合成数据)
- 健康:webapp / voice-rt / nginx 均 active;本机 `/login` 200、HTTPS `/pdf/` 302;
  E2E 冒烟全过(开书 + chars + 点词 + 词典 + 返回书架 + 模型端点)。
- 落地:四个新模块与仓库**逐一 sha256 一致**;运行时 **direct 接线 16/16、未接线 0**;
  六条新路由全部就位。
- 真实登录冒烟(合成引用 `synthetic-smoke`,**未写任何真实书**):**全部通过** ——
  未认证被拒(302→/login)、journal 可读、焦点 set/cancel 落库、
  游标增量拉到两条事件且含 `v/seq/id/ts` 与 `taskId`、cursor 前进、无 gap、
  **重复拉取零重发**、确定性只读命令可用、回执三编号齐全、AI 动作被拒 400。
- 旧路径实测可用:`/pdf/api/reading-pos` 200、`/api/assistant/history` 200、
  `/pdf/api/notes` 路由在。

### 冒烟抓到的真 bug(已修并回归)
`search.book/all` 把用户输入**直接当 FTS5 查询语法**送进 `MATCH`:
`zzzz-not-exist-zzzz` 里的 `not` 被当作操作符 → `OperationalError: no such column: not`。
除报错外,`AND/OR/NEAR/*/"/:` 均可改变查询语义,属**查询注入面**。
已改为整串包成单个短语 + 内嵌引号双写(与 trigram 分词器设计一致),
并加回归测试锁住"不得把原始输入直接送进 MATCH"。
⚠ 该 bug **只有打真实生产索引才会暴露**:隔离测试的临时库没有 `pages_data` 表,
走的是"索引未构建"分支,根本到不了 MATCH。

### 上线后的真实限制(不夸大)
- **总开关默认关闭**:前端 focus/drawing 上报受既有「双向上下文同步」开关约束,
  关着时不发任何请求 → journal 不会有前端来源的新事件。要联调需先在设置里打开。
- **卡片/图片焦点**已接在统一选中出入口;**绘图区焦点**需手指长按(笔/橡皮不触发);
  **文字焦点**走选区漏斗。三者都只在开关开启时生效。
- `command-failed` 进 journal 是**在提交命令时镜像**的;若长时间无人提交命令,
  失败事件不会自行出现。
- journal 为**账户分区**,多用户环境下各自独立;当前仅 uid=1 有数据。
- 未提供 SSE 主动推;Windows 侧为**轮询**模型。


## Claude:任务书 rev18「十四·A」Pi 端交付(2026-07-28,**本地实现 + 隔离验证,未部署**)

范围=共享便签 revision 18 第十四节 A 小节(A1–A5)。全程未部署、未发布、未删除任何旧能力、
未触碰 Windows 侧;B 节(Windows 同步器/命令桥/事件注入器)不在本轮,也未代做。

### A1 现状审计 → `references/reader-agent-capability-audit.md`
`assistant.py` 的 52 个沙盒工具按"执行期是否再次调用 AI"分开:
- **会调 AI(23 个,新通道不得依赖)**:研究生成 7 + 视觉 4(`see_page`/`see_figure`/`see_ink`/
  `correct_dict`)+ 学习闭环判断 9 + 其它 3。⚠ 视觉四项是二次核验才归位的——首轮按调用符号
  初筛把它们误判成确定性,实际是把图像送模型。
- **确定性(29 个)**:读取/定位/标注/便签/页面/词典/制卡落盘等,均有明确底座。
- **MCP 门面整体排除**:`assistant_call_tool` 可代调任意 `_t_*`,故不能作执行依赖。
- 缺口已列:HTML 宿主无服务端正文源;`hlcard.items[]`/`cards[]` 条目字段未进契约;
  锚定只到书+页粒度;`/pdf/api/notes` 兼容通道**必须保留**(旧 AI 仍依赖)。

### A2 版本化规范库 → `reader-specs/` + `scripts/publish_reader_specs.py`
- 短入口 `AGENTS.md`(1841 B):职责边界 + 7 条单轮请求表 + 一次性卡片入口 + **扁平路由表**
  (`当前任务 → AGENTS.md → 一个多步文件`,只跳一层)。
- 4 份真正多步的规范:`highlight-flow` / `page-compose` / `anki-flow` / `result-envelope`,
  每份写清触发、所需上下文、**上游助手内部认知步骤**、要调的无 AI 命令、依赖顺序、
  成功条件、可否重试、最终结构化结果。
- 发布器:内容哈希派生版本 → 同内容不产生新版本;**先暂存 → 逐文件哈希自校验 → rename
  切 `current` 指针**,读者永远看到完整一版(避免 Windows 拉到"新 AGENTS.md + 旧 specs"
  这种半套组合——路由表指向的版本对不上,极难在对话里发现)。
- 本地产物在 `state/reader-specs-dist/`(未进生产)。

### A3 统一结果 envelope → `reader-specs/specs/result-envelope.md`
`reader-result/1` 一个信封,`kind`+`payload` 承载 weather/news/images/videos/fact/general/cards,
不为每类另造协议。kind 与字段**以现有前端渲染器为权威**(经 `reader_card_contract` 校验)。
锚定语义沿用现状:书/真实卷/页/选区属 `anchor` 元信息;高亮在正文原位、正文不重复。

### A4 无 AI 直接命令服务 → `_server_deploy/reader_direct_commands.py`(纯增量,未挂路由)
- 16 个确定性动作白名单;命令含 `correlation`/`target(anchor)`/`action`/`params`/
  `idempotency`/`dependencies`/`precondition`/`mode`;回执含关联编号、成败、结果或错误、
  是否可重试、各子步骤状态,**不返回聊天文本、不调用其它 AI**。
- 执行模式:独立单步**成功静默**;依赖多步把上一步结果传下去、失败即停链。
- `FailureBus`:只装失败事件,带 `voiceTask`/`correlation`/`step`/`retryable`,有游标可增量
  订阅、有上限防无人消费时膨胀。
- 依赖注入式 handlers → 本模块不 import 阅读器,可在隔离环境完整验证协议。

### A5 上下文事件:已有 vs 缺口(未虚报)
- **已有**:PDF/EPUB 页正文随快照同步并带 `text_available/text_source/fallback_reason`;
  选区建立与**清空**均即时上报;连续导航 1s 合并、其余即时;`reader_events` SSE 总线。
- **缺口(本轮未实现)**:`drawingRevision`(绘图稳定约 1s 后成版本、同页仅绘图变化不重发正文)
  与**焦点/取消焦点事件**在代码中均为 0 命中;EPUB/网页"视口 + 上下扩展"取正文亦未按本节口径实现。

### 验证证据(隔离)
- `tests/test_reader_direct_commands.py`:**22/22 OK**。含:白名单不含任何会调 AI 的能力、
  未知动作报可用清单、anchor 按动作校验必填、独立单步成功**零事件**、依赖模式确实传递上一步
  结果、独立模式各步互不可见、失败停链且事件带 voiceTask/step/retryable、前置条件不满足标
  不可重试、同幂等键不重复执行底层动作、事件队列游标与有界、manifest 四要素、发布幂等且
  `current` 为指针、逐文件哈希与 manifest 相符、不留暂存目录、AGENTS.md 短且只跳一层、
  每份多步规范八个必备小节齐全、envelope kind 与渲染器契约一致且不含虚构类型。
- 发布器实跑:version `c3295055a67fb5ad`、5 个文件、重复发布不新增 release、`current` 指针
  哈希全部对上。

### A4 接线完成(2026-07-28 续)→ `_server_deploy/reader_direct_wire.py`
- **16/16 确定性动作全部接上真实底座**:read.page/selection/pageimage、nav.goto/open、
  toc.get、search.book/all、dict.lookup、highlight.create/list、note.create/list、
  page.new/add、anki.draft。
- 在既有 blueprint 上**纯增量**挂两条 endpoint:`/pdf/api/direct-command`(提交)、
  `/pdf/api/direct-events`(失败事件订阅,带游标与 voiceTask 过滤)。接线包在 try 里,
  失败也不会拖垮阅读器主路径。
- **AI 能力双重拦截**:动作白名单本身不含任何审计 §1.1 的 23 个能力;接线时再用
  `_assert_no_ai()` 机器校验一次,防止日后有人往白名单里加。视觉类只给元数据
  (`read.pageimage` 返回是否有墨迹),判断仍归上游。
- 回执三编号并存:`commandId`(服务端签发,调用方不得伪造)、`taskId`(语音任务路由)、
  `correlation`(调用方对回原请求);另有 `ok` / `steps[]` 子步骤状态 / `error` / `retryable`。
  失败事件同样带 `commandId`。
- **不给假成功**:handlers ∪ missing 恰好覆盖全部动作且不重叠;未接线动作在路由层回 400
  并列出 `unwired`,服务层回「未注册处理器」。

#### 接线过程中实测暴露的两个问题(均已修)
1. `_effective_toc` 是 pdf_reader **更后面**才从 book_toc 导入的,接线发生在文件前半段 →
   `hasattr` 当时为 False,toc.get 一度接不上。改为**调用时惰性解析**。
   ECDICT 同理:`lookup_ecdict` 在 `scripts/vocab/dict_sources.py`,接线时 sys.path 未必含该目录。
2. 高亮/便签走**账户分区 sidecar**,需要已认证 session(`_reader_storage_identity_current` 会拒)。
   首版隔离测试没设登录身份,导致 `highlight.list` 静默失败、并因此多出一条事件 ——
   是测试缺身份,不是产品缺陷;已在测试内建用户并设 session 后复测通过。

#### 证据(全部隔离,未部署)
- `tests/test_reader_direct_commands.py`:**31/31 OK**(较上一轮 22 项新增 9 项接线/真实执行)。
- **真实底座执行**(隔离 sidecar + 真实 vault 书):读页取到真文字层;依赖多步
  `page.new → page.add` 第二步确实用到第一步返回的页锚点;同幂等键重复建高亮**只留一条**;
  坏 anchor 失败→事件入队且按 `voiceTask` 路由、带 commandId;成功**零事件**。
- **HTTP 端到端**(test_client,临时 WEBAPP_DATA):① 未登录 302;② 读页 200、
  `commandId=cmd_7342…`、文字层 True;③ `ai.summarize` 被拒 400 并列出可用动作;
  ④ 失败回执 `ok=False retryable=False`;⑤ `/direct-events?voiceTask=vtX` 取到 1 条、
  路由正确、带 commandId;⑥ 成功后新事件数 **0**;⑦ 旧路径 `/pdf/api/notes`、
  `/api/assistant/log`、`/pdf/api/reading-pos`、`/pdf/api/turn-ack` 全部仍在。

### A5 缺口补齐(2026-07-28 续)→ `_server_deploy/reader_outgoing_context.py`
纯增量、零 AI、**不改任何既有写路径**——绘图版本从墨迹 sidecar 的**内容摘要**推导(只读),
所以老的 ink 保存链路一行没动,也不会被本模块拖累。

- **drawingRevision**:内容摘要 + 静默计时。摘要一变就重新计时,静默满 1 秒才升版本。
  未稳定时 `stable=false`、`drawingRevision=null`、**不给 ref**——上游拿不到半截图。
  版本号由内容摘要派生,同一幅图反复观察不会来回换号(否则上游会误以为图变了)。
  为什么不用 mtime:连续落笔时 mtime 一直在变,任何一刻取到的都是半截。
- **focus**:支持 text/image/card/drawing/region 五类 + **显式取消**。四种状态可分辨:
  `never`(从未上报,≠没有焦点)/ `active` / `cancelled`(带 `cancelledObject`,明确告知
  此前对象已取消)/ `stale`(超 5 分钟未更新按未知处理,带 `lastObject`)。
  `seq` 单调递增,供上游"本地版本变了才注入"。
- 三条新 endpoint:`/pdf/api/outgoing/drawing`(GET)、`/pdf/api/outgoing/focus`(POST 设/取消)、
  `/pdf/api/outgoing/state`(GET 合并视图)。与 direct-command 的失败事件**契约名不同**
  (`reader-outgoing-context/1` vs `reader-direct-command/1`),互不干扰。

#### 证据(隔离)
- `tests/test_reader_outgoing_context.py`:**16/16 OK**。含:未稳定不给引用、0.5s 不算稳定
  而 1.0s 算、**继续画则旧版本立即失效**且新版本不同号、同图反复观察号不变、不同页版本不混同、
  五类焦点全接受、非法 kind 与空 ref 被拒、**取消是显式状态且带被取消对象**、
  「从未上报」与「没有焦点」可分辨、陈旧焦点不算当前、取消后可重新设焦点、seq 单调、
  模块无任何 AI 依赖、不调用任何既有写函数、契约名与命令总线可区分。
- **真实 HTTP**(test_client + 临时 sidecar):挂载成功、三条路由就位;未登录 302;
  空页绘图 `stable=false/revision=null`;焦点未上报为 `never`;设卡片焦点后 `active`;
  非法 kind 400;取消后 `cancelled` 且 `focus=null`、`cancelledObject={"cid":"c1"}`;
  旧路径 `/pdf/api/notes`、`/pdf/api/epub-ink`、`/api/assistant/log`、`/pdf/api/direct-command`
  全部仍在。

### A5 前端接线(2026-07-28 续)→ `rc-core.js::RC.outgoing` + 三宿主
唯一实现放共享层,PDF/EPUB/HTML 都调同一份;**复用既有总开关**(未新增 localStorage 键),
关着时一个字节都不发。

- **焦点**:`RC.outgoing.focus(kind, ref)` / `.cancel()`。五类 kind 与服务端一致。
  - **去重**:同一对象重复上报被丢弃 —— 选中态每次重绘都调也不会刷请求。
  - **取消后旧焦点不复活**:取消会清空本地签名,同一对象要再次 `focus()` 才会发;
    重复取消不发空请求。
  - 未登记的 kind 静默不发(宿主传错不会污染服务端状态)。
- **绘图**:`RC.outgoing.drawingTouched(file, page)` 挂在**既有墨迹保存漏斗**上
  (`pdf-tail.js::_inkSave` / `epub-html.js::_inkSave`),每一笔都可以调 —— 网络合并在共享层:
  1s trailing + 单次在途,停手后才去取一次 `/api/outgoing/drawing`;在途期间又画了,回来再取最新。
  **不逐笔发网络**。
- **宿主兼容与降级**:PDF 文字焦点接在选区漏斗、绘图接在 pdf-tail;EPUB 两者都接;
  **HTML 没有墨迹层 → 不调 drawingTouched**,能力缺失即自然降级,没有写宿主分支。

#### 证据(隔离)
- 前端行为(node 里加载**真实 rc-core.js**):**16/16 新增全过** —— 关闭态零请求;
  同一对象重复上报被丢弃、换对象才发(焦点请求恰好 2 条);未登记 kind 不发;
  取消恰好 1 条、重复取消不发、**取消后同一对象需重新 set 才发**;
  连画 8 笔**途中 0 次**取版本、停手约 1s 后**只取 1 次**且带正确 file/page。
  (harness 总计通过,含此前 ctxSync 各项。)
- 静态合同 `tests/test_reader_outgoing_context.py`:**21/21 OK**(较上一轮 16 项新增 5 项接线)——
  唯一实现在共享层、合并窗为 1000ms、三宿主都接焦点且**都有显式取消**、
  绘图只在有墨迹的宿主接线且 HTML 显式写明降级、绘图钩子挂在保存漏斗附近、
  复用同一开关且未新增 `eph-outgoing` 之类的第二个开关。

### A5 焦点宿主埋点(2026-07-28 续)→ `rc-voicecall.js` 统一选中出入口
**接在一处,不是每种交互各写一套**:`_pinRemember`(选中/替换)与 `_pinForget`(取消)
是卡片、图片、视频封面等所有可选中对象的**共同出入口**,焦点同步挂在这两个函数里,
因此新增对象类型时不会漏接。

- `_outgoingKindOf(spec, el)` 把既有对象类型映射成服务端登记的稳定 kind:
  `image-item`/`video-item` → `image`(服务端 kind 表无 video,按图片语义)、
  `ink`/`drawing` 或 DOM 命中 `.vc-ink`/`[data-ink-region]` → `drawing`、`region` → `region`、
  其余一律 `card`(它们都是卡片系统里的对象)。**不新增类型、不猜**。
- payload 只带稳定引用 + 最小语义:`id` / `cid` / `label` / `brief`(截断 160 字)。
  不含 innerHTML、dataURL、base64、strokes —— 测试对这四项做了否定断言。
- 取消选中 → **显式 cancel**,配合共享层"取消后旧焦点不复活"的签名清空。

#### 证据
`tests/test_reader_outgoing_context.py`:**26/26 OK**(较上轮 21 项新增 5 项)——
焦点只有**一个**设置点与**一个**取消点且都挂在统一出入口附近、取消接在 `_pinForget`、
kind 映射闭合且产生的 kind 全在服务端 `FOCUS_KINDS` 内、payload 最小且截断、
绘图出入口就绪。

### 边界与未完成
### 真实跨机联调:前端 → outgoing-event 隔离集成(2026-07-28 续,**未部署**)

在 Pi 上起了一个**临时端口的独立 Flask 实例**跑真实服务代码,用真实浏览器产生真实事件。
生产 webapp / nginx / 旧路径全程未动(联调期间 `webapp` 保持 active)。

#### 隔离与数据边界
- 临时 vault(**合成的最小 PDF `合成测试书.pdf`**)+ 临时 `WEBAPP_DATA` + 临时 sidecar 根
  + 随机空闲端口。**未使用任何真实用户 vault 数据**。
- 真实性说明(不夸大):**浏览器为真、`rc-core.js` 为真、真实指针长按输入为真、
  HTTP 端点与服务端状态机为真**;宿主页面是最小承载页 —— 完整阅读器页需真书与全套静态资源,
  不在本次隔离范围。因此这次证明的是「共享层 + 服务端」这条链路,不是整页阅读器回归。

#### 实测结果
1. 共享层加载并绑定:`bound`
2. 真实鼠标/指针长按 650ms(> 500ms 阈值)→ **1 次** `POST /pdf/api/outgoing/focus`
3. 服务端 `GET /pdf/api/outgoing/state` → `focus.state = active`,
   `kind=drawing`、`ref={file, page:1, drawingRevision:null, region:null}`、`seq=1`
   (长按时绘图尚未稳定,`drawingRevision` **如实为 null**,未假装有版本)
4. 写入临时墨迹后间隔 >1s 观察两次:`stable=False → True`,
   版本 `dr_e461f0e754fe7c2e`
5. 临时实例关闭、临时根删除;**另有一次早前崩溃运行遗留的 `/tmp/bw-integ-_hox775q` 也已清理**
   (内含合成书,无真实数据)

#### Windows 侧可消费的东西(命令与格式)
证据文件(带 manifest/version/hash,只读拉取即可):

```
# 事件流(JSONL,每行一个事件)
scp bwicarus@bwicarus.taile44d0c.ts.net:/home/bwicarus/claude/state/integration-evidence/integ-outgoing-events.jsonl .
# 清单(校验用)
scp bwicarus@bwicarus.taile44d0c.ts.net:/home/bwicarus/claude/state/integration-evidence/manifest.json .
```

- `manifest.json`:`contract=reader-integ-evidence/1`、`version=9d117993162965b5`、
  `files[0].sha256=9d117993162965b54fa6fb81feca644daddc170e217163348575b626e8cf4b12`、
  `bytes=534`、`events=3`。**拉取后请先核对 sha256 再解析。**
- 事件格式与字段语义以版本化规范库为准(同机可拉):
  `/home/bwicarus/claude/state/reader-specs-dist/current/fixtures/`
  (`outgoing-events.jsonl` = 完整形态样例,`README.md` = 字段契约 + 四条消费方硬规则;
  规范库当前 version `45487fc7ccea49bd`,7 个文件,`current` 为原子指针)。
- 本次三条真实事件的形态:`focus/set(kind=drawing)`、`drawing/pending`、`drawing/stable`。
  与 fixture 同构,可直接用同一套解析。

#### 边界
- 这是**一次性证据快照**,不是常驻端点;需要新数据就重跑隔离脚本。
- 未提供对外只读测试端点(会改动生产服务配置);改用"文件 + 清单 + scp"这条不碰生产的路径。
- 未覆盖 `command-failed` 的真机产出(它已有隔离与 HTTP 证据,见前文 A4 段)。

### 跨端事件 fixture 与字段契约(2026-07-28 续)→ `reader-specs/fixtures/`
把新增的三类出向接口(focus / drawing / command-failure)收口成**版本化、最小**的
JSONL fixture + 字段契约,供跨端消费方照着写解析。

- `outgoing-events.jsonl`(9 行,每行带 `v:1`)覆盖全部必备形态:`page` 锚点、
  focus **set / replace / cancel**、drawing **pending → stable revision**、
  独立成功**静默**(以 `emitsEvent:false` 显式表达"这里不会有事件")、
  失败事件含 `correlation` / `commandId` / `taskId` / `step` / `retryable` / `error`。
  签发类字段用格式占位符 `dr_<16hex>` / `cmd_<12hex>`。
- `fixtures/README.md` 是字段契约表 + 四条**消费方硬规则**:未稳定就没有可用绘图引用、
  取消后到下一次 set 之间不存在当前焦点、没有失败事件不代表没执行、
  `retryable=false` 重发无意义。

#### 关键做法:fixture 由真实代码验证,不靠人手维护
`tests/test_outgoing_fixture.py` 用 `FocusState` / `DrawingRevisions` /
`DirectCommandService` **真的跑一遍**,再与 fixture 逐字段比对 ——
改实现不改 fixture 会红,改 fixture 不改实现也会红。这样跨端消费方(照 fixture 写的那一侧)
不会因为我们这边悄悄改了字段而被动踩坑。

#### 过程中修掉的一个真问题
发布器原本只收 `*.md`,fixture 的 `.jsonl` **进不了 manifest** → Windows 拿不到带哈希的
确定性副本,版本化就白做了。我最初把它写成"已知边界",随后改为**修发布器**
(`SPEC_SUFFIXES = (".md", ".jsonl")`),并把测试改成断言 fixture 必须带 sha256 进 manifest。

#### 证据
- `tests/test_outgoing_fixture.py`:**10/10 OK** —— JSONL 合法且每行带版本、八种必备形态齐全、
  契约文档覆盖所有 type、真实代码重放 focus 三态(seq 递增、被取消对象逐字段一致)、
  drawing pending 不给版本/引用而 stable 版本号匹配 `^dr_[0-9a-f]{16}$`、
  独立成功**零事件**、失败事件五个字段与 fixture 一致且 `commandId` 匹配 `^cmd_[0-9a-f]{12}$`、
  绘图焦点引用字段集最小且不含 strokes/image/data、fixture 带哈希进 manifest。
- 规范库重新发布:version `45487fc7ccea49bd`、**7 个文件**(含 fixture 两份),
  `current` 指针原子切换,仅落本地 `state/reader-specs-dist/`。

### A5 绘图区焦点交互(2026-07-28 续)→ `RC.outgoing.bindDrawingFocus`
绘图区此前**没有**任何"选中/加入上下文"交互,本轮补一个最小的:**手指长按 = 设为当前焦点,
再长按 = 取消**。语义与项目既有「长按=加入上下文」(高亮/便签/图)一致。

- **不碰指针状态机**(rc-ink 三方分叉是既定铁律):只处理 `pointerType !== 'pen'` 的指针 ——
  笔与橡皮压根不经过这条路径;全程 `passive: true`,不 `preventDefault`、不 `stopPropagation`
  → 画笔/擦除/滚动手势零影响。
- 长按 500ms、位移阈值 10px:**移动即判为滚页**、提前抬手不触发,不会误吞滚动。
- **目标失效不设空焦点**:本页没有墨迹时长按什么都不做。
- **切页/换节自动丢弃**:接在既有翻页漏斗(PDF `_saveLastPosition` / EPUB `_reportPos`),
  且 `dropDrawingFocus()` 只在当前焦点确实是绘图时才发,不会误取消卡片焦点。
- **最小引用**:`{file, page, drawingRevision, region}`。未稳定时 `drawingRevision` 如实为 `null`,
  不假装有版本;**绝不携带笔画**(测试对 strokes/dataURL/base64/__inkStrokes 做否定断言)。
- 与 1 秒稳定更新兼容:焦点只由显式长按产生,画画本身不触发焦点事件(避免逐笔事件)。

#### 实现中修掉的一个真 bug
"再长按取消"首版失效:长按分支用 `file#page` 拼签名,而 `focus()` 用 `JSON.stringify(ref)`,
两套算法永远判不相等 → 第二次长按被当成新对象。已抽出共用 `_ogSig()`,测试固化
"一个定义 + 两个调用点,不许各算各的"。

#### 证据
- node harness 第 7 节:**13/13**(笔按下不设焦点、位移=滚页不设、长按设焦点且 kind=drawing、
  只带最小引用不带笔画、未稳定时 revision 如实为 null、**再长按取消**、提前抬手不触发、
  本页无墨迹不设、非绘图焦点时 dropDrawingFocus 不发)。
- 静态合同 `tests/test_reader_outgoing_context.py`:**32/32 OK**(新增 6 项)——
  笔/passive/无 preventDefault 与 stopPropagation、长按与位移阈值、签名单一算法、
  payload 无笔画、PDF 两条渲染路径都绑 + EPUB 已绑、两宿主切页均丢弃。

- 焦点埋点在 `rc-voicecall.js`(卡片系统所在),PDF/EPUB/HTML 三宿主都加载该文件,故三端一致;
  纯文字焦点仍由各自选区漏斗上报,两条通道并存不冲突。
- 原「卡片/图片/绘图区焦点尚无宿主埋点」条目:卡片与图片已完成,绘图区如上。:共享层已支持这三类 kind,但目前只有 `text` 有真实
  触发点(选区漏斗)。卡片焦点需在 `rc-turncard`/`rc-voicecall` 的卡片交互处调用,
  图片/绘图区焦点需在对应命中处调用 —— 本轮未做。
- 上一轮遗留项(服务端状态机与接口)本轮已接线,原「前端尚未上报焦点」条目作废。
- 原文保留:本轮只交付服务端确定性状态机与接口。焦点需要阅读器在选中文字/图片/
  卡片/绘图区时调 `/api/outgoing/focus`,该埋点未做(选区已有独立通道,不受影响)。
- 绘图稳定判定是**查询时计算**(observe 时判定),没有常驻定时器主动推送;上游需在需要时查询,
  或后续接入 SSE 主动推(未做)。
- **未部署**:以上全部只在仓库,生产未跑本次改动;`pdf_reader.py` 已挂载但**没有部署到 webapp**。
  接线时需注入 16 个确定性 handler,并确认 `translate` 走确定性后端(配置切 AI 后端则须退出白名单)。
- A5 两项缺口(drawingRevision、焦点事件)未实现,需单独授权。
- 规范库仅发布到本地 `state/reader-specs-dist/`;Windows 侧拉取属 B 节,未做。


## ⚠ Claude：生产处于「半装」状态 + 共享工作树互相干扰（2026-07-27 04:15 JST）

用户问「Cloud 是不是为了让自己的部署通过动了什么、Codex 那边的问题跟你有关吗」，逐项查证如下。
**第二问的答案是：有关，而且我这边有一个真缺陷已经影响生产。**

### 一、发布闸门没有被动过（可复核）
`deploy_reader.sh` / `reader_deploy_manifest.py` / `reader_kg_release.py` / `reader_e2e.py` /
`audit_reader_network.py` / `reader_network_audit_baseline.json` 六个文件 mtime **全部停在 07-26**，
2026-07-27 我一个都没修改。
唯一需要说明的动作：07-26 晚我曾把 `deploy_reader.sh` 复制成 `scripts/.deploy_frozen_claude.sh`
再执行——原因是当时 Codex 正在**运行中改写该脚本**，bash 按字节偏移读取会错行并报出
`line 1190: … No such file or directory` 这类假故障。副本逐字节相同、未删改任何检查、跑完即删，
现已不存在。今天 04:06 那次部署我也**没有绕过** `deploy-in-progress.json` 闸门，而是被它正确挡下。

### 二、⚠ 我的真缺陷：新模块漏注册进部署清单
`_server_deploy/reader_card_contract.py` 是新增文件，我**没有把它加进 `reader_deploy_manifest.py`
的白名单**（实测清单中 0 条匹配）。后果：
- 依赖它的 `assistant.py` 已随 Codex 的发布进入生产（生产 `assistant.py` 有 5 处
  `reader_card_contract` 引用，`pdf_reader.py` 有 `pdf_api_turn_ack`，静态 `rc-core.js` 有
  `ctxSync`、`rc-assistant.js` 有 `onHistoryEvent`）；
- 而该模块**永远不会被任何部署复制过去**（清单是显式白名单）；
- 实测生产环境 `import reader_card_contract` → **ModuleNotFoundError**。

**当前生产影响（已实测确认）**：两处 import 都在函数内，普通阅读与侧栏对话不受影响。
- 外部经桥接写 parts → `/api/assistant/log` 抛 ImportError → **500**；
- `_t_web_search` 那处有 `except Exception` 兜底 → 退回保守卡型集合，不崩，
  但**配图/视频卡仍会被吃掉**（我修的那个漂移 bug 在生产上尚未生效）。

这个坑与 Codex 无关，是我加新模块时漏了注册；只是它的部署把依赖方先带上去了，才变成半装。

### 三、共享工作树的双向干扰（结构性问题，不是谁的失误）
两个 agent 在**同一个工作树**上作业，于是：
- **它的部署会把我未完成的改动一起发布**——生产现在就有我今天写的、本该等我自己部署的代码；
- **我的操作也会打断它**：我为同步 vendor 跑过数次 `build.py`（单次重写约 30 个 `vendor/*.js`）、
  向 `tests/` 新增测试文件，而部署的验证摘要覆盖整个 `tests/` 目录与这些产物。
  我 03:41 那次失败（`验证合同/夹具在预检期间发生漂移`）正是被它 03:41:25 / 03:42:55 写
  `tests/reader_contract/*.mjs` 撞掉的；同一机制反过来成立，**它近期的多次失败很可能有我这一份**。

### 四、收口需要什么（我未动手，等指派）
1. 把 `_server_deploy/reader_card_contract.py` 加进 `reader_deploy_manifest.py` 清单。
   该文件属 Codex 认领的发布管线 → **由 Codex 改，或用户明确授权我改这一行**。
2. 之后跑一次完整 `deploy_reader.sh`，把后端契约模块与已在生产的依赖方补齐。
3. 在补齐前，生产保持「半装」：前端与路由是新的、后端契约模块缺失，外部桥接写 parts 会 500。
4. 建议（供讨论）：两 agent 若要并行，改用各自的 git worktree；否则「一方部署会发另一方半成品」
   这个风险每次都在，本次只是终于显形。

### 现场状态（只读核实）
webapp / voice-rt / reader-context-push / nginx 全部 active，`/login` 200、`/pdf/` 302；
KG `current → kg-0.2.57-6247b4799fb02b4699c7`；Codex 的 `deploy-in-progress.json`
（status=`current_switched`，写于 03:46:35）**仍未清理**，因此任何新部署都会被闸门拒绝。


## Claude：外部语音助手 → 侧边栏桥接闭环（2026-07-27，**仅实现+测试，未部署**）

> ⚠ **本段「未部署」的表述已被现实推翻**：Codex 后续的发布从共享工作树把本段大部分改动
> 带上了生产，但新增的 `reader_card_contract.py` 因未注册进部署清单而没跟上 → 生产半装。
> 详见上一段《生产处于「半装」状态 + 共享工作树互相干扰》。

发布权在 Codex，本段全部改动**只落仓库**：没有跑 deploy_reader.sh、没有重启、没有提交/推送、
没有碰 KG/扩展产物。生产上跑的仍是 0.2.52，本批要等发布负责人安排窗口。

### 一、卡片契约收敛为唯一来源（需求①）
此前"允许哪些卡"散在**三处手写白名单**并已实际漂移：`assistant.py` 的 web_search 网关只放行
weather/news/fact/general，`_EXT_CARD_KINDS` 放行 6 种，而前端渲染器早就会画 images/videos
——搜索返回配图卡会被网关默默吃掉。
新增 `_server_deploy/reader_card_contract.py`：**kind 不写死，从统一渲染器解析**
（part ← `rc-turncard.js::renderPart` 的 `p.kind === '…'`；card ← `rc-voicecall.js::_infoHtml`
的 `k === '…'` + else 兜底 general）。字段规格只在该文件声明一次；渲染器新增 kind 而规格没补时
`contract_gaps()` 非空并 **fail-closed**，明确报出缺口而不是放行一个前端画不出来的东西。
桥接器、`_sanitize_ext_parts`、web_search 网关全部改为引用它，三处白名单删除。
外部写入从"静默丢弃单条"改为**明确拒绝整包并回具体字段**（调用方原先永远不知道卡为什么没出现）。

### 二、assistant_turn 高层化（需求②）
payload 收敛为 `{text, cards?[], flashcards?[], result?, user_utterance?}`：
`cards`=工具卡（走契约校验），`flashcards`=Anki 草稿（两者分开，免得互相冒充）。
书名/页码/请求号由 `_active()` 自动取当前活动文档——**合并书自动落到真实卷与卷内页**；
调用方不拼路径、不编 parts 协议、不做 JSON 手工编码。

### 三、侧栏实时到达（需求③）
`assistant-history` 事件此前**发了没人听**（我上一批加的 publish，前端无监听）。
现在：`publish` 返回真实投递数并带 `turn_id`；`rc-assistant.js` 增 `onHistoryEvent`——
面板开着就当场追加，用的是与实时/回放**完全同一个** `RC.turnCard.renderTurn`（ADR 不变式①，
不存在第三条渲染路径）；PDF/EPUB 两宿主的既有 SSE 分派各接一行。
**没有新增任何对外服务**（测试断言侧栏不得自己 new EventSource / WebSocket）。
⚠ 实现坑：`rc-assistant.js` 有两个 IIFE，导出点最初挂错了 IIFE 会拿不到闭包（try/catch 还会把
ReferenceError 吞掉）——已改到同作用域并加了作用域自证测试。

### 四、分层回执（需求④）
`written`（已落库，刷新必可见）/ `delivery.published` + `subscribers`（SSE 推到几个在线侧栏）/
`delivery.rendered`（前端渲染完经新端点 `/pdf/api/turn-ack` 回执）。
0 订阅明确说明"不是失败，是没开侧栏"。失败路径带 where/字段/契约提示。

### 五、Windows 客户端（需求⑤）
新增 `scripts/bridge_client.py`（放 Windows 跑，零依赖）：OpenSSH **ControlMaster 复用已认证连接**
（ControlPath 按 `%C` 分键，不同主机不串）、`ControlPersist=60` 空闲 60s 自动关、
异常时 `_drop_master()` 后**只重连一次**，仍失败就报出首次/重连两段原因 + 排查清单。
`BatchMode=yes` 防止无人值守时弹密码框卡死。

### 六、context.md 分层 + 当前页正文 + 选区（需求⑥ 与两条后补需求）
- **分层**：`📌 当前上下文（判断现状只看这段）` 与 `🗄 历史归档（不代表当前状态）` 两区，
  完整历史保留、不做激进摘要，但明确标注不得用于推断现状。
- **正文优先**：新增第三节「当前页正文」，排在图像**之前**；PDF 走 `_page_text_clean`
  （剔噪字符层）、EPUB 走 `_epub_section_paragraphs`、HTML 暂无服务端正文源并如实说明。
  永远输出 `text_available / text_source / fallback_reason`——"有文字层却只给图"被测试禁止。
  图像段标题改为「版式/插图核对用；不是正文来源」。合并书取正文自动落到真实卷。
- **选区三态**：有选区 / **明确无（用户已取消）** / 未上报，三者可分辨。后端 `/api/active-reading`
  现在只要请求带 `selection` 键就落库（空串=显式清空），不再"空就跳过"——否则快照会留着旧选区。
  PDF 走 `checkSelection`、EPUB 走 `captureSel`，建立/改动/清空都即时上报。

### 七、同步时序合同（后补需求）
撤销"一刀切长防抖"：**默认即时推**，唯一合并的是连续导航。
前端 `RC.ctxSync` 分 `_CTX_NAV_MS=1000`（同书仅页码变）与 `_CTX_NOW_MS=0`（其余），
靠 `_ctxOnlyPosChanged` 逐字段比对把选区变化排除出导航；Pi 守护同理分
`NAV_DEBOUNCE_S=1.0` / `NOW_DEBOUNCE_S=0.15`，`_is_nav()` 跟上一次记录逐字段比对
——换书、选区建立/清空、笔记高亮一律即时，不被上一次翻页的 1s 窗拖住。

### 验证
- `tests/test_bridge_card_contract.py`：**24/24 OK**（契约来源自渲染器、三处白名单已删、
  契约缺口 fail-closed、6 种卡型全接受、未知/缺字段明确拒绝、多余字段剥离、零卡片合法、
  正文三字段与排序、选区三态、时序分类含守护 `_is_nav` 真值表、回执分层、实时追加复用同一渲染器、
  不新增对外服务、Windows 客户端复用/60s/只重连一次、context.md 两区）。
- `tests/test_context_sync_active.py`：**13/13 OK**（其中一条锚点随小节重编号更新，断言意图未变）。
- 离线功能验证：真实书页（费恩曼 p57）`text_available=True`、来源为剔噪字符层、正文可读；
  合并书经真实卷取到正文；桥接自动补出 `file=真实卷 / page=卷内页`；
  不合规卡回 `cards[0] 不合契约:card[weather].data:缺必填字段 hi`；未知卡型回"渲染器画不出来"。

### 隔离环境端到端(补做,发现并修掉两个真 bug)
用 Flask `test_client` + 临时 `WEBAPP_DATA` 跑完整写入链路,结果:
`/log` → `{appended:1, delivered:1, turn_id}` → SSE 事件带 turn_id → `/history` 按 turn_id
取回 `parts=[text, card, card]`、卡型 `[weather, images]`(证明 images 卡不再被旧网关吃掉)
→ 不合规写入回 **400** 且 error=`card[weather].data:缺必填字段 hi` → `turn-ack` 200。
过程中 E2E 抓出两个我自己写的 bug,均已修 + 补测试:
1. `pdf_api_turn_ack` 照搬了 assistant.py 的 `_logged_in()`,而 pdf_reader 里**没有这个函数**
   → NameError→500。改用本模块通行的 `session.get("user_id")`。
2. 契约违规原本冒泡成 **500**(调用方只看到"服务器错误",没法自查)。改为 **400 + 具体字段 +
   契约来源提示**。测试固化:不许再退回 500 或静默丢弃。

⚠ **事故与复原**:第一次跑 E2E 时我忽略了「助手会话历史存在 `CLAUDE_DIR/state/assistant-convo`,
**不受 `WEBAPP_DATA` 控制**」,导致一条测试轮写进了用户真实侧栏历史。已**先备份
(`state/assistant-convo/1.json.bak-1785090655`)再按索引只摘除该条**,其余 14 条逐条复验完好
(最后两条仍是用户的制卡对话)。E2E 脚本已改为显式隔离 `_CONVO_DIR`,并新增守卫测试
`TestIsolationGuardTest` 把这个前提固化,防止后来者重蹈。

### 未完成边界
- **未部署**：以上全部只在仓库。生产仍是 0.2.52，需 Codex 安排窗口后按正式流程发布。
- 侧栏实时追加、选区上报、时序分类都**没有真机浏览器验证**（需部署后才能测）。
- HTML 宿主：正文在前端 DOM，服务端无等价来源；快照如实标 `fallback_reason`，
  要补需前端上报正文块。HTML 宿主也尚未接选区上报。
- `_wait_ack` 用轮询本地 ack 文件（1.5s 上限）；多 worker 部署下 SSE 订阅数与 ack 的
  可见性需复核（当前单 worker + 线程模型下成立）。
- 契约只覆盖"卡片/part 的形状"。工具**行为**类白名单（`RC.execRemote` 的 client-action fn）
  仍是既有欠账，不在本批范围。


## Claude 二次回归定位：幽灵推送进程覆盖 + 合并书修复上线（2026-07-26 20:10 JST）

用户第二次回归失败（service 已 active、SSH 每次成功，但 Windows `context.md` 仍旧书；
且在 `state/` 下找不到 `reader-active.json` / `reader-context-sync.json`）。逐项核实结论：

1. **`state/` 下没有那两个文件是正常的**，不是故障。实时真值在账户分区
   `webapp/data/reader-sidecars/by-user/1/`：开关 `{"enabled":true,"ts":…}`（**19:34 由用户
   在真实浏览器里打开**，证明设置开关与 POST 通路本来就是好的）、活动记录亦在此。
2. **前端与静态资源无问题**：线上 `rc-core.js` 含 `ctxSync` 与第三方站点护栏、
   `rc-settings.js` 含 `set-ctx-sync`；nginx 日志显示用户真实会话在翻页时**每次都发了**上报。
3. **真凶 A（已修）：合并书被网关 501。** 用户读的是 `vbook:g_3e5d696e85`，
   `POST /pdf/api/active-reading` 全部 501（`vbook_unadapted`）。修复（放行名单 + 视图身份
   与真实卷解析）已于 20:00 前后上线，用户同一会话随后的上报**全部 200**，账户分区落库为
   `Excalidraw/费恩曼物理学讲义（第1卷）… p1`。
4. **真凶 B（已修）：幽灵推送进程覆盖。** 存在两个 `push_reader_context_to_pc.py`：
   systemd 的（19:40 起，新代码）+ 一个 **17:04 起就一直活着的旧代码进程（PID 77641）**，
   由今天早些时候的前台测试遗留、`PPID=1` 变成孤儿。两者都监听同一批路径、都往
   Windows 推，**谁最后写谁赢**——旧进程产出的是修复前格式（`- **书**：応用情報技術者 /
   最后阅读 2026-07-22`），于是 Windows 那份反复被“打回旧世界”。已 `kill 77641`，
   并 `systemctl restart reader-context-push` 让常驻进程加载最新代码。
   现 Windows `context.md` 恢复新格式，抬头有「双向上下文同步：🟢 已开启」，
   在无新鲜上报时如实写「当前在读：未知（已过期 N 分钟）」，不再冒充历史书。

### 教训（值得写进规范）
- **前台/后台跑过的常驻脚本必须确认已退出**，否则它会以孤儿身份长期存活并用旧代码覆盖
  正式服务的产物；症状极具迷惑性（服务 active、日志每次“已同步”，产物却是旧的）。
- 排查“产物是旧的”时，先 `ps -eo pid,lstart,args | grep <脚本名>` 数**有几个进程**，
  再看代码和部署。

### 部署过程说明
`deploy_reader.sh` 被 Codex 的 KG 发布闭环持续占用与改写（脚本在运行中被改写会让 bash
按字节偏移错行，出现 `line 1190: … No such file or directory` 这类假故障）。为避免中途改写，
我用**同一份脚本的冻结副本**（`scripts/.deploy_frozen_claude.sh`，仓库内、`dirname/..` 解析不变，
跑完即删）在锁空闲窗口完成了这次部署；未改动 Codex 任何 KG 文件，未做发布提交。
期间两次失败曾使 webapp/voice-rt 冻结（站点 502），均已 `systemctl start` 恢复并在启动前
验证 `import app` 正常；当前 webapp / voice-rt / nginx / reader-context-push 全部 active。

### 待用户确认的最后一步
请在阅读器里**翻一页**（同一浏览器会话，不用任何探针），随后核对：Pi 账户分区
`reader-active.json` 为该书该页 + Windows `context.md`「当前在读」为同一书页。


## Claude 紧急回归修复：发布遗漏 systemd 单元 + 合并书上报被 501（2026-07-26 19:55 JST）

### 用户报告
Windows `context.md` 19:35 仍是旧的《応用情報技術者》p43；Pi 上 `reader-context-push`
在 systemd 里**根本不存在**（`Unit could not be found`），单元文件只躺在
`references/systemd/`。→ 活动上报即使落库也不会同步到 Windows。

### 根因一：我的发布遗漏（已修复）
上一段实现只把单元文件写进 `references/systemd/reader-context-push.service`，**从未安装**。
「写了 unit 文件」被我当成了「服务已上线」。
- 处置：`sudo install -m 0644 -o root -g root` → `/etc/systemd/system/`、`daemon-reload`、
  `enable --now`。现状 **active + enabled（开机自启）**。
- 证据：`journalctl` → `watcher 启动：18 个目录，debounce 1.0s → C:\Users\bwica\bw-reader-context`
  → `✓ 已同步（快照 0.03s，总 1.73s）`。

### 根因二：合并书（vbook）上报被网关 501（代码已修，**尚未部署**）
nginx 日志显示用户 19:36–19:37 的真实上报**一直在打**，但全部 `501`：
`POST /pdf/api/active-reading … 501`，Referer 是 `?file=vbook%3Ag_3e5d696e85&page=31`。
即用户开的是**虚拟合并书**，而 `/api/active-reading` 未进 vbook 网关放行名单，
按 `vbook_unadapted` fail-closed 打回 → 活动状态一条都没落库。
- 已改（仓库内，测试通过，**未上线**）：把 `pdf_reader.pdf_api_active_reading` 加入
  `_VB_VIEW_OK`（同 `reading_pos`：合并书身份原样直通）；handler 记录**视图身份 + 全局页**
  并解析出真实卷 `member` / `member_pos`；快照展示合并书身份、内容类查询（页要点/插图/标注/对话）
  改用真实卷取数。新增回归 `VbookActiveTest` 2 项，`tests/test_context_sync_active.py` **13/13 OK**。

### 已验证可用（非合并书路径，现网真实链路）
真实上报 `资源/books/応用情報技術者.pdf` p123 → 守护进程 1.6s 内 SSH 同步 →
Windows `context.md` 显示「🟢 实时（1 秒前上报）/ 该书 / 123 页」，旧 p43 内容已消失。
**说明整条链（前端→Pi→快照→SSH→Windows）已经通**，仅合并书那一步卡在部署。

### ⚠ 部署撞车事故（两次导致 webapp/voice-rt 停摆，均已恢复）
`scripts/deploy_reader.sh` 目前**确定性失败**在第③步：
`BLOCKED: KG current changed concurrently: expected 'kg-0.2.52-…', found 'kg-0.2.51-…'`
——脚本把 `--expected` 设成了**本次新建的** release id，再拿去与切换前的 `current` 比对，
必然不等（compare-and-swap 的 expected 应是**旧**值）。这是 Codex 18:31 认领、正在建的
KG 发布闭环，按协作规则我**未改其文件**。
- 后果：失败路径会「保持服务冻结」，两次把 `webapp` + `voice-rt` 停住（站点 502）。
  我已两次 `systemctl start` 恢复，并在启动前先验证部署树 `import app` 正常（327 路由）。
  当前：webapp/voice-rt/nginx/reader-context-push 全部 **active**，`/login` 200、`/pdf/` 302。
- 回滚是干净的：`_server_deploy/pdf_reader.py` 与生产不一致（生产为旧版），`app.py`/`assistant.py`
  与仓库一致，无半装状态；`deploy-in-progress.json` 未遗留、`deploy.lock` 未被持有，
  **不阻塞 Codex 继续**。
- 我已停止重试 `deploy_reader.sh`（每次重试都会再停一次生产服务）。

### 下一步（需要谁做什么）
1. **Codex**：修复 KG 切换的 `--expected` 语义（应传切换前的 `current`），并让失败路径在
   回滚成功后**把 webapp/voice-rt 拉回来**，而不是留在冻结态——现在任何人部署失败都会导致站点 502。
2. **之后**：重跑一次 `bash scripts/deploy_reader.sh` 即可带上合并书修复（我方代码已就绪，无需再改）。
3. 在那之前：**合并书**的活动上报仍会 501（快照对合并书显示「当前在读：未知」，
   这是 fail-closed 的正确行为，不会冒充旧书）；普通 PDF/EPUB/HTML 一切正常。
4. 我为验证写入的测试记录（応用情報 p123）会在 180 秒新鲜窗口后自动过期，届时快照转为
   「未知（已过期）」，不会长期冒充当前。


## Claude 修复：快照「当前书/页」误判 + 双向上下文同步总开关（2026-07-26 18:55 JST）

### 用户提出的问题
快照说当前在读《応用情報技術者》p43（07-22），实时 positions 是费恩曼 p57，用户屏幕上其实
是第三本书——三者互不相符。追加硬性需求：实时上下文同步必须是**设置页显式开关、默认关闭**，
关闭时不 POST / 不轮询 / 不挂监听；且收敛为**一个「双向上下文同步」总开关**，同时管
①前端上报活动状态 ②Pi 生成并 SSH 更新 Windows 的 `context.md`。Windows→Pi 的
assistant_turn 回写属**被动显示**，不受此开关约束、也不需要前端为它轮询。

### 两个独立根因（第二个更隐蔽）
1. **把「续读位置表」当成「当前活动文档」**：`reader-positions.json` 是*每本书各自的*
   续读位置，快照取 `max(ts)` 只等于「最后一次翻过页的那本书」。一本两天前读完的书会
   永远霸榜，而此刻正开着却没翻页的书根本不在榜首。
2. **快照读的是认领时冻结的 legacy 副本**：webapp 早已把 reader sidecar 迁到
   `webapp/data/reader-sidecars/by-user/<uid>/`，`state/` 下那份是账户认领时
   **一次性复制、此后再不更新**（copy-only）。实测 legacy 最新是 07-22 的《応用情報技術者》p43，
   账户实时那份是 07-26 的费恩曼 p57——用户看到的「快照/实时/屏幕三者不一致」正由此而来。
   ⚠ 任何离线脚本读 `state/reader-*` 都有同样风险，不止本快照。

### 本次范围（已完成）
- **总开关（跨端唯一真值）**：`/pdf/api/context-sync` GET/POST → `reader-context-sync.json`
  （账户分区）。前端用 localStorage `eph-ctx-sync` 做**零网络镜像 gate**：默认关时连一次
  GET 都不发。关闭时服务端 fail-closed（`/api/active-reading` POST 返回 409），并**立即清空**
  活动状态——否则关掉之后快照还捧着最后一条当「当前」。Pi 推送守护进程读同一个文件，
  不会出现「前端以为关了、后台还在推」。
- **活动文档权威源**：`/pdf/api/active-reading` GET/POST → `reader-active.json` 单条记录
  （kind/file|url/pos/title/selection/ts）。GET 返回 `fresh` + `age_sec`，新鲜窗口 180s
  （= 前端可见时 60s 心跳的 3 倍）。
- **前端唯一上报器 `RC.ctxSync`**（放在 `rc-core.js`，三宿主都加载且**已在版本清单里**，
  从而避开「新增 live 文件漏进版本清单 → 真机永远旧代码」那个老坑）：gate / 1s trailing
  debounce / 单次在途 + 回来补发 / 换文档整份重置 / 可见性心跳 / pagehide beacon 全在这一份。
  PDF 与 EPUB 接的是**已存在的**位置漏斗（`_saveLastPosition` / `_reportPos`），**零新增监听**；
  HTML 宿主没有漏斗，只在开关已开时开机上报一次。
- **安全护栏**：扩展在第三方站点里跑时，相对路径会把书名/页码/标题 POST 给那个站点。
  故 `kind:'web'` 必须先 `RC.ctxSync.setBase(Pi 源)` 才允许上报，没设就不发；设置面板在
  web 宿主也不做开启后的即时上报。
- **快照**：`cur` 只认新鲜 active；不新鲜/无上报/开关关闭 → 明写「**当前在读：未知**」+ 原因，
  并显式警告「不要拿下面『历史记录』里的任何一本当成用户此刻在看的书」；下游的页要点/图像/
  标注/对话段一并留空，不拿旧书数据填充。第七段改名「历史记录（**不是当前在读**）」。
  抬头标注开关状态。新增 `sc()` 解析器：**账户实时优先、legacy 回退**，19 处读取点全部改用。
- **推送守护**：同一把开关 gate（关闭时不生成、不连 SSH）；监听清单加账户分区的
  `reader-active.json` / `reader-context-sync.json` / `reader-positions.json` / 高亮 / 便签
  ——只盯 legacy 会一辈子等不到变化。
- **交互策略登记**：`context.active.report` 故意 `offline: 'retain-local'` + `sync: 'none'`
  ——**不进 outbox 队列**：离线攒着、恢复网络后补发一条 20 分钟前的位置，会带着新鲜的服务端
  ts 落库，正好重新制造它要修的那个 bug。丢掉比补发正确。`context.sync.toggle` 为
  remote-required（写不成就回滚 UI）。两条 `/pdf` 路由已登记 vbook 策略 `GLOBAL`。

### 验证
- `tests/test_context_sync_active.py`：**11/11 OK**（快照当前书判定 6 项、账户分区优先 2 项、
  后端开关/fail-closed/校验/清空 1 项、前端契约 1 项、推送 gate 1 项）。
- `tests/ctx_sync/rc_ctxsync_harness.js`：在 node 里加载**真实 rc-core.js** 跑行为契约，
  **22/22 OK**（默认关=零请求零监听、10 次连翻只发 1 次且是最后一次、单次在途+补发、
  换书不粘连、第三方站点护栏、关闭后摘监听）。
- **反向验证成立**：把 `cur` 改回「历史表 max(ts)」，「当前在读」段立刻泄漏《応用情報技術者》
  且不再声明未知 → 新测试确实抓得住这个 bug。
- **真链路端到端**（真 webapp + 真 state + 真快照，铸签名 cookie，不碰密码/数据库）：
  **16/16 OK**——关闭态上报被拒 409 且磁盘无记录；开启后上报落库、服务端判新鲜、快照
  「🟢 实时 0 秒前上报 / 费恩曼 p57」且历史书未出现；关闭后活动状态被清空、快照回到「未知」
  且不点名任何历史书；现场恢复为默认关闭。
- **真浏览器节流证据**（Playwright + 真实 PDF 阅读器，走 nginx HTTPS 源）：开箱默认关闭、
  关闭态浏览 2.5s **0 次请求**；开启后连翻 8 页，翻页途中 **0 次**、停手 2s 后共 **1 次**，
  落库为最后一页 18。
- 网络依赖审计：**0 new debt**；`deploy_reader.sh` 契约预检 + 28 项静态壳测试 + E2E 冒烟全过
  （18:33 那次部署）。

### 阻塞与剩余边界
- ✅ **全部改动已部署并在真机复验**（2026-07-26 19:05 JST）。过程记录：18:42 那次 `deploy_reader.sh` 曾报
  「清单含未知目标组: kg_runtime」——彼时 Codex 正在改发布管线（18:31 认领），清单已发
  `kg_runtime` 行而逐文件安装循环尚未跳过整棵发布组；按协作规则我未改其文件，稍后重跑即通过。
  重跑时末尾的 `rm: Permission denied` 只发生在**临时探针目录清理**（新 KG release 目录是只读），
  部署本身已完成：两份静态文件 `diff -q` 与部署目标一致、真机 rc-core.js 含护栏代码、
  webapp/voice-rt active、`/login` 200、E2E 冒烟全过；我这两次运行的 `/tmp/bw-reader-deploy.*`
  残留已清理。→ 若 Codex 需要，可考虑在其 KG 探针清理前 `chmod -R u+w` 再删。
  部署后复验：真链路 E2E **16/16 OK**、真浏览器节流 **5/5 OK**（默认关 0 请求 / 连翻 8 页合并成 1 次 / 落库末页 18）。
- 扩展（web 宿主）尚未接线上报：需要在扩展侧调用 `RC.ctxSync.setBase(ORIGIN)` 并接一个
  活动页漏斗；护栏保证在此之前不会误发。
- `state/` 下的 legacy 副本仍在（copy-only 设计，未删）。其它离线脚本若也读 `state/reader-*`，
  同样会看到冻结数据——建议后续统一走 `reader_context_snapshot.sc()` 这类解析。
- 新鲜窗口固定 180s，未做可配置；EPUB/HTML 宿主暂未上报 selection。


## Claude 实现：Pi↔Windows 唯一 SSH 通道（快照 + 命令桥）（2026-07-26 17:15 JST）

### 中心约束（用户拍板）
跨机联系收敛为**唯一 SSH bridge 通道**，双向：
- **方向 A（Pi→Win）**：状态快照 → 固定 `C:\Users\bwica\bw-reader-context\{context.md, assets/}`
- **方向 B（Win→Pi）**：白名单 envelope → Pi 内部路由到阅读器真实接口/侧栏卡片入口

外部调用方**永不需要**知道 state 落盘位置、web API 路径、PDF/EPUB/HTML 宿主差异。
MCP 保留为"需要实时真值/页面控制"的能力层；跨机状态与命令协议唯一、固定、可审计。
快照是**优先上下文**，不替代 MCP。

### 方向 A：快照（已上线可用）
- `scripts/reader_context_snapshot.py`：单一完整快照。七段——当前在读 / 当前页要点(PDF brief) /
  图像 / 本书标注概况(高亮·便签·手写·插入页) / 侧栏助手对话(30 轮，PDF+EPUB 双命名空间) /
  **命令与任务**（见下）/ 最近书目 + 文末「何时必须改用 MCP」对照表。
- **命令与任务段**按用户要求保留**指令原文**：从 `cli-tasks/*.json` 提取
  `instruction`（指令原文，600 字）+ `steps[]`（每步 name/args/result/rationale）+ 状态/步骤/
  口播/结果/错误/前端动作 + `assistant-creations` 产物引用；分「进行中 / 最近完成（详细展开）/
  历史归档（`<details>` 折叠、一行一条、长期保留）」，**不做激进摘要**。
- `scripts/push_reader_context_to_pc.py`：**inotifywait**（系统自带，零新依赖）监听 13 个 state
  路径 → **1s trailing debounce** → 单次在途（在途变化置 dirty 结束后补一轮）→ 生成完整快照 →
  SSH 同步。不可达退避 60s。ControlMaster 连接复用。
- **原子替换**：scp 到唯一 `.part` → `MoveFileEx(REPLACE_EXISTING|WRITE_THROUGH)` + 重试 8×150ms。
  ⚠ **禁用 `Move-Item -Force`/`cmd move /Y`**（实测目标被读者持有时它们走"先删后改名"的非原子路径，
  有文件消失窗口；MoveFileEx 干净失败、目标保持旧版完整）。
- **实测**：连续 10 次变化 → **只同步 1 次**；停止变化后 **≈2.5s** 完整落地（1s debounce +
  1.5s 生成与传输）；资产 547KB（预算 <5MB，每轮重建 assets/ 防增长）。
- ⚠ 输出目录必须在 `state/` **之外**（`.reader-context-out/`）——曾因放在 state/ 内被自身
  inotify 触发而**自激循环**（每 3 秒推一次），已修复并复验。

### 方向 B：命令桥（已实现并验证）
- `scripts/reader_bridge.py`：**唯一命令入口**。envelope =
  `{version, request_id, kind, payload, file?, page?, context_ref?}`；
  kind 白名单 `assistant_turn / open_page / highlight / create_note / ping`。
- **`assistant_turn` 写回语义**（用户拍板）：一次 envelope 批量回写——
  `user_utterance?` + `assistant_text` + `result?` + **`cards` 可选 0..N** + artifacts。
  文本与卡片落在**同一条**助手消息的**同一个 parts 数组**里 → 一次 HTTP、不存在
  "文本写了卡片没写"的半状态；`request_id` **幂等**（重放返回 `idempotent_replay`，不重复写）。
  外部**不接触** `assistant_log_chat` 或任何内部写入点，全部封装在桥接器内。
- **真卡片，非伪装**：复用既有回放链路——`rc-assistant.js:2798` 对带 `parts` 的消息调
  `RC.turnCard.renderTurn` → **与实时同一个 `renderPart`**（`rc-turncard.js:89`）。零前端改动。
- **后端消毒器**（`assistant.py` 新增，随 `/api/assistant/log`）：把此前的隐式约定固化为常量表
  `_EXT_PART_KINDS={text,card,cards,hlcard}` / `_EXT_CARD_KINDS={weather,news,images,videos,fact,general}`
  / `_EXT_PART_FIELDS`；逐 part 白名单过滤，**`tool`/`meta` 外部不可写**（它们描述服务端执行轨迹、
  会驱动前端二次执行）。`cards` 的 **gid 由服务端 `_entity_reg_cards` 签发**，外部不得伪造编号。
- **验证结果（全通过）**：
  - ① **text-only**：1 个 part、**0 卡片**，无空卡/占位卡，协议未因无卡片失败；
  - ② **text+cards**：2 个 parts，卡片 `gid=card_6a118b`（服务端签发），
    `GET /pdf/api/entity/card_6a118b` 返回卡片数据 → 按钮动作（入库 Anki／评分／刷新恢复）
    接上既有状态机；
  - ③ **幂等**：重放同 request_id → `idempotent_replay:true`，库内仍 1 条；
  - ④ **安全五项全部正确拒绝**：非法 kind / 绝对路径 / `..` 穿越 / 缺 request_id /
    **外部夹带 `tool`+`meta` part 被消毒器丢弃，只留 `text`**；
  - ⑤ **可审计**：每条 envelope 落 `state/reader-bridge/audit.jsonl`（rid/kind/file/page/ok/耗时）。
  - 测试探针消息已从 `assistant-convo` 清理（17→14 条）。

### 启动 / 停止 / 回滚
- 启动：`sudo systemctl enable --now reader-context-push`（单元在
  `references/systemd/reader-context-push.service`，Type=simple/Restart=always）；
  前台调试 `python3 scripts/push_reader_context_to_pc.py`。
- 停止：`sudo systemctl stop reader-context-push`。
- 回滚：停 service + 删 Windows 目录即可；快照侧**只读** state/，不改阅读器任何数据。
  命令桥的回滚 = 不再调用（它只经既有 `/api/assistant/log` 等公开接口写入）。
- 外部用法：`ssh <pi> "python3 /home/bwicarus/claude/scripts/reader_bridge.py --json '<envelope>'"`。

### ⚠ 本轮踩到并已修复的部署事故（值得记录）
手工 `sudo cp assistant.py` 到 webapp 导致 **webapp 启动失败约 2 分钟**：
① `assistant.py` 依赖 Codex 新引入的 `tool_registry`，而 deploy manifest 明确要求
`assistant/tool_registry/voice/task_runtime/card_improvement_runtime` **同批原子部署**，我漏了依赖；
② 补 cp 后仍失败——`sudo cp` 使文件属 **root:root 0600**，gunicorn 读不到（PermissionError）。
修复：补齐同批文件 + `chown bwicarus:bwicarus` + `chmod 644`，服务恢复（active / login 200）。
**教训：改 webapp 侧 Python 必须走 `scripts/deploy_reader.sh`（有 manifest 校验、备份与回滚），
不要手工 cp 单文件。**

### 剩余待办
- 顺带发现的**独立安全欠账**（不属本通道，建议排期）：`RC.execRemote`（`rc-assistant.js:2429`）
  对 `client-action` 的 `window[fn]` **无白名单**，任何能发 client-action 的路径等于任意前端命令执行；
  建议固化为 `RC.REMOTE_FNS` 常量表（现约定的 8 个 fn）。
- 快照可扩展项：EPUB/HTML 宿主的更多特有字段、资产 LRU GC、真机长跑观察。
- 侧栏卡片的**真机点击验证**（入库 Anki→评分→刷新恢复三步）需在浏览器实际操作确认。

## KG-f：有界热缓存后的耐久重放、历史证据与审计日志（2026-07-26 18:16 JST）——本地实现完成

- **目标与结果**：ConceptNodeService 新增 `kg-node-history/1` 冷历史账本。热 mutation
  receipt/provenance 被有界淘汰后，同一请求仍能按 `mutationId + requestDigest +
  operationContract` 精确重放；同 mutationId 不同 payload、事务编号复用、损坏/截断/乱序
  journal、无法证明的旧 causal 记录均 fail closed。durable occurrence ledger 独立于展示用
  provenance 上限，`signal` 必须逐事务及当前图都等于对应 occurrence 数。
- **回滚与路径投影**：rollback 现在把目标 tx、receipt keys、before/after 节点快照、
  tombstone 和 PageBrief occurrence moves 全部绑定为目标事务的精确逆变换；禁止目标重绑、
  rollback-of-rollback、空 graph-only 假回滚和 node-upsert 携带 projection。v1 快照不再被
  猜作可认证回滚材料；旧 v1 rollback target 会固化进 baseline，无法证明的 hot receipt
  仅保留取证，不进入 cold replay，也不由 `mutation_status` 返回。
- **恢复与耐久性**：baseline、prepare、commit/abort 都在写前完成候选链验证；只按物理
  ASCII LF 切 JSONL，允许尾部断写修复但拒绝中段/Unicode 分隔伪造。journal、graph、
  edge-audit outbox 和 audit JSONL 的首次目录创建、replace/append/unlink 均补齐文件与父目录
  fsync。边审计先持久 stage outbox，再依据 mutation 状态幂等 flush；payload 摘要、文件名、
  mutation/entry 身份与全局 entry_id 冲突均严格验证。
- **改动范围**：
  `scripts/kg/concept_node_service.py`、`scripts/kg/promote_concepts.py`、
  `scripts/kg/propose_concept_notes.py`、`scripts/kg/audit_edges.py`、
  `tests/test_concept_node_service_kg_f.py`、`tests/test_kg_audit_log_outbox.py`、
  `tests/test_concept_graph_lifecycle.py`；同步修订
  `references/reader-runtime-architecture.md` 与 `references/reader-extension-handoff.md`。
- **验证证据**：
  - KG-a～f、PageBrief、rename、lifecycle、audit outbox 定向：**113/113 OK**；
  - KG-f/outbox 的恢复、目标重绑、signal=99、旧 receipt 隔离、Unicode、并发幂等与损坏
    对抗用例：**42/42 OK**；
  - Python 全量：
    `PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -p 'test_*.py'`
    → **446/446 OK，skipped=7**；
  - 真实 `emergent-graph.json + kg-node-mutations.jsonl` 只读复制到临时目录完成 v1
    baseline 补铸：16 行→17 行、baseline=1、occurrence=47、状态 `applied`；真实两文件
    SHA-256 前后保持
    `0e42b8ed…f6a2` / `8c694797…0175`，未写生产 state；
  - 目标文件 `py_compile` 通过；最终独立只读对抗复核未再发现可复现的数据损坏阻断。
- **风险/未做**：未进行真实断电故障注入、真实生产图补铸或跨进程 outbox 压测；全量输出仍有
  既有测试的 sqlite/file/socket ResourceWarning 文本，但 unittest 结果通过。**尚未部署**。
  部署审计发现现有 manifest 不包含 KG 文件，webapp/quick-sync 却直接从脏工作树执行它们，
  当前没有可声称的 KG 版本隔离或完整回滚点。
- **下一负责人**：Codex。先建立受部署 manifest 管理的不可变 KG runtime 与原子 `current`
  指针、无副作用 preflight、真实回滚点和健康探针；再构建 0.2.51、走 Windows 既有独立扩展
  测试环境，满足自身发布标准后部署并登记版本/回滚信息。

- 2026-07-26 18:31 JST：Codex 已按用户最新授权认领 KG-a～f 后的发布闭环。本段范围为：
  把全部生产 KG 调用从共享脏工作树迁到受 manifest 管理的不可变 runtime，补齐原子
  `current` 切换、无副作用 preflight、备份/回滚和只读健康探针；随后将候选升版为
  `0.2.52`（现有 `0.2.51` 不可变 ZIP 已与两项共享视觉修复不一致），运行 KG/全量/发布
  合同、Windows 既有独立扩展环境验收，再按项目唯一 `scripts/deploy_reader.sh` 流程部署。
  不覆盖旧 ZIP、不手工复制生产文件、不使用日常 Chrome/profile，也不等待 Cloud 审查。

## Codex：0.2.52 KG runtime 部署与并发事务收尾（2026-07-26 20:10 JST）

- **发布负责人/锁状态**：按用户协调决定，Codex 是当前唯一发布负责人。Codex 最后一次主动
  发起的部署事务为 A：`20260726T105944Z-235263`；候选摘要
  `e4a0accdac5a935fb49726cbe33dbaea4a084bc162150438acdbdc19af101712`，结果
  `complete`，当前不持有 `deploy.lock`。现在部署锁空闲，`deploy-in-progress.json`
  已不存在；本段收尾后没有启动新的 `deploy_reader.sh`。
- **事务 A**：完整 preflight、原子安装、KG current 切换和真实阅读器 E2E 均通过，
  部署为 `reader=0.2.52`、`kg=kg-0.2.52-99d8ecc046738a63d4fd`。回滚/取证目录：
  `/home/bwicarus/deploy-backups/reader/20260726T105944Z-235263`；其
  `previousKgRelease=kg-0.2.51-ceee0480f337df5b10ca`，普通文件和 KG 状态备份均保留。
- **并发事务 B 取证与收尾**：另一个进程曾以 UID `bwicarus`、工作目录
  `/home/bwicarus/claude` 发起 `20260726T110155Z-241815`；现有 OS 日志无法把该进程唯一
  归属到某个 AI，故不猜测发起者。B 的冻结候选摘要与 A 相同，payload 摘要为
  `5ae43cc07ac42294244c4e5f4c3cba7bb898e0560858fd88a2aa8a392de1202c`。
  B 在无进程、无持锁者时遗留 `current_switched` marker 和未写结果的备份目录；
  冻结 manifest 共 138 项，其中 108 个普通落地文件逐字比较 **108/108 一致**，
  KG release 验签通过，B 的 `kg-state-before.sha256` 与 E2E 后生成的
  `kg-state-after.sha256` 逐字一致。Codex 在唯一部署锁内对 marker 的
  deployId/candidate/payload/status 做 CAS 核对后，仅补写标准 `complete` 结果并移除
  stale marker，没有复制或覆盖产品文件。B 的回滚/取证目录：
  `/home/bwicarus/deploy-backups/reader/20260726T110155Z-241815`。
- **生产健康**：`webapp.service` PID `249635`、`voice-rt.service` PID `249639` 在 5 秒稳定
  窗口内不变，二者 `NRestarts=0`；直连 `/login` 返回 200，语音 WebSocket 握手返回 101。
  `bwicarus-quick-sync.timer`、`bwicarus-daily.timer` 均为 `active/waiting`；
  quick-sync 最近 `Result=success / ExecMainStatus=0`，服务完成后为 `inactive/dead`，
  无重入写；`concept-graph.timer` 按原生产边界保持关闭。
- **候选与验证证据**：KG-a～f 定向 **113/113**；最终 Python 全量 **515/515**
  （另 skip 7）；Node 全量 **42/42**；部署前冻结候选门禁 Python **107** 项、
  Node **28** 项及实际 `/usr/bin/python3` staged import/服务探针均通过。不可变 Windows
  扩展候选为 `0.2.52`、74 个文件，ZIP SHA-256
  `ea3e4fd6b24f80d8c03372e39675f122b038b07f10e7073d849ab52c06086bee`；
  固定独立 Windows Chrome 环境已通过功能烟测并恢复 `Ready`，9222 已关闭，未触碰日常
  Chrome/profile。
- **扩展发布边界**：生产测试通道仍为 `0.2.47`；0.2.52 通道发布器与逐字节回滚备份已
  只读预检通过，但本并发收尾段没有运行通道发布。`lslocks` 证明
  `/tmp/bw-webext-build-ffba9eacec69fb22.lock` 与
  `/tmp/bw-webext-deploy-f81c6d6d55a4a70d.lock` 均无人持有，也没有
  `publish_test_channel.py` 进程。等待协调方在确认 Cloud 已停止所有共享发布入口后，
  再由 Codex 单独决定是否激活 0.2.52 通道。
- **已知事故/回滚证据**：成功事务 A 之前的两次失败分别保留
  `/home/bwicarus/deploy-backups/reader/20260726T104243Z-188348` 与
  `/home/bwicarus/deploy-backups/reader/20260726T105438Z-220699`。前者暴露子 shell
  ERR 双回滚与 timer `Requires=` 问题，后者暴露健康探针误用缺 `fitz` 的 mcp venv；
  两项均已补合同并修复。第二次事务只回滚一次且普通文件恢复逐字一致；未发现 KG 或用户数据
  丢失。当前生产以 A/B 的同一冻结候选摘要和上述健康证据为准。

## Codex：0.2.52 扩展测试通道发布（2026-07-26 20:17 JST）

- **结果**：按用户明确指令，由唯一发布负责人 Codex 运行
  `python3 extensions/bw-reader-webext/publish_test_channel.py --deploy`，扩展测试通道已从
  `0.2.47` 原子激活到 `0.2.52`。本段没有再次运行 `deploy_reader.sh`，没有创建 Reader/KG
  部署事务，也没有启动或修改日常 Chrome/profile。
- **线上不可变资产**：
  - 主包 `bw-reader-webext-0.2.52-windows-test.zip`：
    `ea3e4fd6b24f80d8c03372e39675f122b038b07f10e7073d849ab52c06086bee`
    （1,148,434 bytes）；
  - channel JSON：
    `5a7b5a26700b8b4ab5c04d1672a5e403095c93b11486859ad7ba906aa1ccf9ca`；
  - launcher v6 PowerShell：
    `26c369b5c6760ba10d40bddf75da115fb7617cd5ecc91a08e88ac9bfc54d9587`；
  - launcher v6 ZIP：
    `748ca383e64db0cc92faefc19dca3eeb84513db5027686e2c2c547c96be900fb`。
- **发布门禁**：发布器重新完成 manifest/精确白名单、45 个 JS 语法、42 个 runtime 合同、
  网络债务、IndexedDB 87 assertions、Text Range 6 assertions、普通网页/PWA 接管与 fallback
  浏览器回归、卡片拖拽、侧栏、网页笔迹、便签和译句调度回归；`handoff` 为
  `errors=0 / READY`，唯一 warning 是保留中的既有脏工作区。版本单调门确认
  `0.2.47 → 0.2.52`。
- **线上复核**：服务器文件、`audit_deployed_baseline()` 和无缓存 HTTPS channel 返回均报告
  `version=0.2.52` 且主包/launcher 摘要完全一致。Windows 电脑用无 profile 的 PowerShell
  将线上 channel 和 ZIP 直接下载到内存复核：`version=0.2.52`、bytes=`1148434`、
  actual SHA 等于 expected SHA、`match=true`；没有启动浏览器或写测试目录。
- **回滚点**：
  `/home/bwicarus/deploy-backups/reader/webext-channel-20260726T111531Z-cogok4iy`。
  `channel-deploy.json` 状态为 `committed`；原 0.2.47 channel 字节保存在
  `channel.before`，原摘要
  `f397853d98b47fbb937996fe60f76c0860d798a83bb1900229654321ff053ce1`，
  本次 `rollback.attempted=false / result=not-needed`。如需回退，应以该耐久记录和原子发布器的
  逐字节校验语义操作，不手工覆盖不可变资产。
- **收尾健康/锁**：webapp 与 voice-rt 仍为 `active/running`、`NRestarts=0`，
  `/login` 直连 200；quick-sync 与 daily timer 均为 `active/waiting`。两个扩展发布锁均已释放，
  无 `publish_test_channel.py` 进程；Reader/KG `deploy.lock` 空闲且无 active marker。
- **独立只读复核与残余风险**：第二检查者确认生产磁盘、公开 HTTP 下载、回滚原始字节及四个
  摘要全部一致，两个锁可被 non-blocking `flock` 取得。唯一非阻断风险是 nginx 目前仍给可变
  channel JSON 返回 `Cache-Control: public, max-age=2592000, immutable`；launcher v6 每次请求
  都追加新的时间戳 query，并对 channel 加 no-cache header，Windows 实测已取得 0.2.52，
  所以当前更新路径不复用旧 URL。后续应为这个可变 JSON 单独配置 `no-cache/no-store`。

- 2026-07-26 21:07 JST：Codex 按用户新指令认领“助手 Tab 复习模式统一”工作段。范围为：
  将当前独立 `rc-review` 卡片区域嵌入既有助手 Tab 上方、保留下方完整聊天；普通助手与复习模式
  使用独立会话上下文、历史存储和清空语义；把旧 AI 卡片改进页的全部动作接入同一
  card-improvement 后端且保留旧入口；修复扩展模式卡片动作不可点击并淘汰临时
  “显示答案/评分”按钮；在统一模型设置中加入 `gpt-5.3-codex-spark`，并按 Codex 实时模型目录
  仅对支持 Fast tier 的 Codex 模型提供独立加速开关。预计涉及共享
  `rc-review/rc-assistant/rc-sidedrawer/rc-settings`、assistant/card-improvement 服务、扩展
  Shell/生成 vendor 与定向测试；不会手改 vendor，不会改日常 Chrome/profile。发布前仍走唯一
  manifest、独立 Windows 扩展环境和原子部署/通道流程。

## Codex：0.2.55 助手复习模式统一、Spark 目录修复与发布（2026-07-27 02:12 JST）

- **结果**：复习不再是独立的残缺页面。助手 Tab 上方是可折叠的相关 Anki 卡片区，下方保留
  完整聊天、输入和工具链；普通助手与复习助手按账户及 mode 使用独立历史、摘要、归档和清空
  事务。卡片恢复标准 Anki 流程：只显示正面，用户点击后才显示背面及
  “再来/困难/良好/简单”。“改进”折叠菜单接回旧 AI 改进页的详细/精炼、打开来源、
  更新笔记、生成新 Anki、全部更新、草稿预览和显式确认；旧页面仍保留，但二者调用同一
  owner-bound runtime。右上界面设置可分别控制上方 Tab 与下方快捷按钮。
- **投影与来源**：来源、原因、Local ID 和“问 AI”只在侧栏卡片投影中隐藏，原卡内容未删除；
  答案和 footer 同容器时保留答案。`source_ref/source_url` 与卡面可见来源链接统一提升；
  仅接纳安全 Obsidian/HTTP(S)/相对笔记路径，拒绝脚本/模板伪造、冲突 marker、userinfo、
  非法 hostname/port、Windows/UNC 绝对路径、控制字符和编码 traversal。可信点击只导航一次，
  不再把 `window.open(..., noopener)` 的空返回值误判为失败。
- **评分正确性**：跨页面时若服务端拒绝已乐观移出的评分，原上下文缓存会恢复该卡。服务端
  `/pdf/api/review-answer` 以每 `aid` 分片跨进程锁、全局账本锁和原子 pending/done receipt
  保证最多一次执行；同 `aid` 换 payload 返回 409，损坏账本 fail closed。AnkiConnect 没有
  mutation ID，因此“已进入 Anki、尚未写 done 即崩溃”的未知结果会永久 pending，当前选择
  是避免双评分，后续可用 revlog 对账恢复。
- **Codex/Spark**：主 `~/.codex/auth.json` 以原子、last-good 方式同步到阅读器专用
  `CODEX_HOME`；认证代次变化不会打断正在执行的多轮 thread。模型目录按实际 app-server
  进程认证代次构建并一次性发布不可变快照，缓存返回深拷贝。Windows 真环境确认
  `gpt-5.3-codex-spark` 为 `selectable=true / available=true`，普通 Spark 可用；该账户目录
  未声明 priority，故 Fast 独立开关保持关闭，而不是再显示“账号不能使用”。
- **主要改动范围**：共享
  `_server_deploy/static/pdf/rc-review.js`、`rc-assistant.js`、`rc-sidedrawer.js`，
  `_server_deploy/assistant.py`、`pdf_reader.py`，扩展 manifest/build 生成物及对应 Python/Node/
  Chromium 合同测试。扩展 `vendor/` 只由 `build.py` 生成，没有手改。
- **验证证据**：
  - Python 全量 **627/627 OK，skip 7**；Node reader contract **45 files / 477 tests 全过**；
  - 来源/provenance/candidate **20/20**，卡片改进/candidate **18/18**，评分幂等 **7/7**；
  - `handoff_check.py`：`version=0.2.55 / errors=0 / warnings=1 / READY`，唯一 warning 为既有
    脏工作区；发布器门禁再次通过 45 个 JS 语法、45 个 runtime 合同、网络审计 0 新债务、
    IndexedDB 87 assertions、Text Range 6 assertions 及普通网页/PWA 接管浏览器回归；
  - Windows 固定独立环境（Chrome 150、扩展 ID `jddhhakcblmihidgdobfkcejjinpigak`）真机通过：
    正面→翻面→四级评分、同容器 footer 投影、改进菜单、来源真实点击单页导航、卡片折叠、
    下方完整聊天、宿主点击和一次评分 POST；页面/console error 均为 0。收尾后计划任务
    `Ready`、专用 Chrome 进程 0、9222 与 SSH 隧道关闭；日常 Chrome/profile 未触碰。
- **PWA/服务端发布**：唯一 `scripts/deploy_reader.sh` 事务
  `20260726T164729Z-444957` 完成；`reader=0.2.55`，
  `kg=kg-0.2.55-161162792deec5b924e8`，candidate digest
  `76a2ee34abfc85848098de1e2a61237ba634c28d4e0fb18e11ebedeedb16b804`，payload digest
  `232f8dcca3ccb38b85567f5aa20a2f04f34ee48f8fe870344c0c72fc9d4e12d0`。回滚点
  `/home/bwicarus/deploy-backups/reader/20260726T164729Z-444957`，前一 KG
  `kg-0.2.53-6078741c10a7e78fe663`。webapp/voice-rt active、`NRestarts=0`、`/login` 200。
- **扩展测试渠道发布**：0.2.55 已从 0.2.52 原子激活；不可变 Windows ZIP 为
  `bw-reader-webext-0.2.55-windows-test.zip`，1,169,612 bytes，SHA-256
  `ce70f64f3d4bdc782680baa6a70ef0d778b9e565cde2ddf0e7e855ddf23eabb6`。无缓存 HTTPS
  channel 与 ZIP 下载复核版本/摘要一致；发布回滚点
  `/home/bwicarus/deploy-backups/reader/webext-channel-20260726T171152Z-sa30gnq3`，
  状态 `committed`、`rollback=not-needed`。扩展 build/channel 与 Reader 部署锁均已释放，
  quick-sync/daily timer active。
- **风险/未做**：未做真实 Surface Pen 物理笔验收；评分的未知 Anki 结果仍需未来 revlog 对账；
  复习模式电话/长按连续 Realtime 在 relay 能冻结 mode 前继续 fail closed 禁用；工作区仍有
  大量历史/用户未提交改动，禁止 reset/clean。下一负责人先读本文和
  `reader-extension-handoff.md`，不得把 0.2.53 的“正反面同时显示”回归重新引入。

- 2026-07-27 02:30 JST：Codex 认领“Anki 实体卡片统一”工作段。仅改共享卡片组合层、
  复习工作区与对应合同/扩展测试：以 `rc-voicecall` 为唯一视觉/拖放/收藏/长按外壳，
  `rc-flashcard` 为唯一正反面与评分视图，`rc-review` 继续独占队列、幂等提交和失败恢复；
  复习卡用真实 Anki card ID 派生稳定 `cid === gid`，避免同一来源实体多卡碰撞。不会重写
  placement、收藏或评分 transport，不手改扩展 vendor，也不触碰日常 Chrome/profile。

## Codex：0.2.57 Anki 实体卡统一、Windows 独立验收与工作入口整理（2026-07-27 04:10 JST）

- **结果**：Anki 制卡结果、复习卡、收藏夹卡与网页/PWA placement 现消费同一实体卡组合入口。
  `rc-voicecall` 唯一拥有视觉壳、拖放、收藏和长按；`rc-flashcard` 唯一拥有正反面、四级评分、
  快照与同 ID 状态；`rc-review` 继续唯一拥有队列、outbox、拒绝恢复；placement 继续由
  `rc-stickynote` / Web pins 管理。学习卡使用真实 Anki card ID 派生
  `id === cid === gid`，页面位置 ID 另算。侧栏、页面、收藏和复习区只是不同行为投影，
  source/note/card/entity/reason 等字段只在投影隐藏，没有从实体删除。
- **交互闭环**：复习卡恢复标准“先正面 → 翻面 → 再来/困难/良好/简单”；整卡长按导出
  `anki-card-context/1` 完整上下文，同 ID 的侧栏与页面副本同步高亮；拖到页面或收藏夹保留
  稳定身份与结构化完整快照。页面固定卡直接使用共享卡本体，不再套第二层窗口，并随文档原生
  滚动。助手 Tab 仍保持可折叠复习 workspace 在上、普通聊天在下。
- **写入正确性**：评分和 Anki 添加在客户端先持久化 pending。`/api/review-answer` 与
  `/api/anki-add-cards` 使用精确 card ID；Anki 添加增加按 aid 的跨进程锁、payload fingerprint、
  原子 pending/done receipt、legacy ledger 兼容和损坏 fail-closed。AnkiConnect 已可能执行但
  receipt 未完成的未知结果保持 pending，绝不盲目再次 `addNote`。
- **主要改动范围**：共享 `rc-flashcard.js`、`rc-review.js`、`rc-voicecall.js`、
  `rc-stickynote.js`、`rc-md.js` 及卡片调用方；扩展 `src/web-pins.js` 和 build 生成 vendor；
  `_server_deploy/assistant.py`、`pdf_reader.py`；卡片拖放、收藏、固定选择、review outcome 与
  Anki 添加幂等测试。扩展 vendor 只由 `build.py` 生成，没有手改。
- **本地验证**：
  - Node reader contract **495/495**；
  - Python 全量 **667/667**，skip 14；
  - 真实 Chromium
    `test_review_candidates.py`、`test_card_favorite_payload.py`、
    `test_pinned_anki_selection.py`、`test_card_drag.py` 全部通过；
  - `handoff_check.py` 为 `errors=0 / READY`，唯一 warning 是既有脏工作区；
  - release pipeline **18/18**，`release_preflight.py --artifact
    extensions/bw-reader-webext-0.2.57-windows-test.zip --skip-browser` 为 READY；
  - 最终 ZIP 内 74 个文件逐项与当前扩展目录摘要相等，关键 JS syntax、Python compile 和相关
    `git diff --check` 均通过。
- **不可变本地候选**：
  `extensions/bw-reader-webext-0.2.57-windows-test.zip`，74 files，SHA-256
  `45c9362ec8ffb9760f8a5a0eb376cb9e747c5f50df9d6a5afb92c1824cb90c9a`。
  0.2.56 是已废弃的未发布中间候选，禁止覆盖或激活。
- **Windows 固定独立环境**：Windows 端重算 ZIP 摘要一致；固定扩展 ID
  `jddhhakcblmihidgdobfkcejjinpigak` 的 service worker/manifest 均为 0.2.57。真实普通网页中
  验证了正面/翻面/四级评分、稳定 `anki_card_9257`、拖到页面后完整来源、整卡长按同步高亮、
  收藏 payload、原生滚动 180px、普通宿主点击、单 placement，以及复习 workspace + 下方聊天。
  page error 为 0。测试记录已清理；旧 0.2.55 unpacked 回滚目录为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.57-20260727T035308`。
  收尾后计划任务 `Ready`、专用 Chrome 进程 0、9222 监听 0、SSH/CDP 隧道关闭；日常
  Chrome/profile 未触碰。
- **发布边界**：本段没有部署 PWA/服务端，没有发布扩展 channel。生产 PWA/服务端与测试
  channel 仍为 **0.2.55**；固定 Windows unpacked 测试目录是 0.2.57，不代表线上 channel
  已更新。0.2.57 尚未做同版本四书 PWA 接管，不能与线上 0.2.55 混测后误报通过。
- **Codex 工作入口**：在既有 `AGENTS.md` 顶部补入 Reader 快速入口、源码/命令/硬边界和
  按需回查索引；新增 `references/codex-reader-context.md`，只提炼由代码、测试或发布记录
  证实的产品矩阵、协议、MCP/app-server/ToolRegistry 约定、当前候选与可靠性边界。原始
  `reader-runtime-architecture.md`、`reader-extension-ownership.md`、
  `reader-extension-handoff.md`、冲突登记、卡片、Codex 和 MCP 文档继续是可追溯规范，没有
  复制长篇内容。所有本地 Markdown 链接、脚本路径与 Python 入口已核验存在/可编译。
  入口明确标注了历史不一致：生产基线 0.2.55 与工作区候选 0.2.57 分开；旧 PDF 手工部署清单、
  固定 MCP 工具数/8765 端口和卡片文档“待修”段不得直接当现状。触及 conflict/pending、
  sync/owner lease、模型/Fast tier、MCP 权限、Surface Pen 或发布/回滚时仍必须回看原始实现、
  运行时目录与对应长文档。
- **风险与未做**：未做 Surface Pen 物理验收、两个真实设备直连、registry 跨代迁移或
  0.2.57 同版本 PWA 接管。未知 Anki mutation 仍需未来 revlog 对账；全局 Anki receipt 在
  多用户阶段必须按账户分区。工作区保留大量用户/历史改动，禁止 reset/clean。
- **下一门禁/负责人**：本工作段停止在未发布候选。未来真正准备部署时，Codex 先一次性交付
  完整人工浏览器验收清单；用户按顺序完成全部视觉/点击/拖拽/长按/输入/页面切换，Codex 只
  监控后台请求、数据流、日志与持久化。用户视觉验收和后台证据均通过后，才可由单一发布
  owner 提出并执行部署；任何异常先修复。

- 2026-07-27 04:23 JST：Codex 认领“固定卡正文长按、卡头蓄力拖拽与复习圆点翻页”工作段。
  仅修改共享卡片手势/分页组合层、PWA 与扩展 placement 适配和对应测试：展开正文长按选中
  整张卡，卡头保留短按形态但须按住达到激活阈值后才能拖动；复习队列复用 `rc-flashcard`
  唯一 scroll-snap/圆点 pager，仍保持每张 Anki 卡独立 `id === cid === gid`。不会部署、
  发布、重启、提交或推送，不手改扩展 vendor，也不触碰日常 Chrome/profile。实现完成后
  自行运行定向与必要回归并登记证据。

- 2026-07-27 04:44 JST：Codex 子任务认领“复习评分拒绝回插的 card-transition 边界”。
  仅修改 `_server_deploy/static/pdf/rc-review.js` 与
  `tests/reader_contract/review-candidate-ui.contract.test.mjs`：同 context 的异步拒绝回插
  在切回原卡前撤销旧卡选择、失效旧卡请求并重置答案/改进状态，渲染后重新激活当前卡选择；
  补后一张已翻面及改进草稿挂起的竞态合同。不会修改 pager、评分 transport、vendor 或发布物。

- 2026-07-27 04:46 JST：上述拒绝回插修复完成。`_restoreRejectedAnswer` 只在原 context
  仍为当前 context 时执行完整 card-transition：记忆并撤销离开卡的选择、推进 draft/commit
  epoch 且清空预览/忙态、恢复原队列位置并重置 answer/improve，重渲后激活恢复卡选择；
  跨 context 分支仍只修补对应快照，不触碰当前页。新增两条合同分别覆盖“下一张已翻面后
  409 拒绝”和“下一张 draft pending 时 409 拒绝且迟到草稿不得复活”。定向 Node
  `review-candidate-ui + flashcard-pager` 为 **22/22**，outcome-unknown Node 为 **5/5**，
  Python `test_review_answer_idempotency` 为 **8/8**，JS syntax 与 scoped
  `git diff --check` 均通过。未改 pager、transport、vendor、版本或发布物；下一 owner 为
  原“复习圆点翻页”工作段负责人，继续其整体回归与收尾。

## Codex：0.2.58 卡片正文选择、蓄力拖拽与复习圆点候选（2026-07-27 05:19 JST）

- **实现范围**：
  - 固定 Anki/HTML 工具卡以整卡作为稳定选择 owner，但只在展开正文长按；按钮、链接和卡头
    不触发上下文选择，同 `cid/gid` 的侧栏、收藏与页面投影同步高亮。`pinBind` 对每个 owner
    只保留一个真实 press target，从旧整卡迁到正文时先拆旧 listener/timer，避免一次长按
    双 toggle。
  - 共享 `rc-voicecall` 的侧栏拖出、浮卡拖动和 Web placement，以及 PWA
    `rc-stickynote` 卡头，统一为 420ms 蓄力、8px 蓄力前容差和 pointer capture。短点只执行
    一次原形态动作；蓄力前越界会取消并吞掉紧随的一次合成 click；`pointercancel`、失去捕获、
    blur、hidden 只回滚。PWA 卡头严格绑定首个 pointer，多指/笔+手指不能抢占或误落卡，
    teardown/取消会释放捕获并清 anchor/trash/favorite/transform 暂态；卡片不再进入旧便签
    EDIT。明确脱离 DOM、listener 环境失效或同 pointerId 新生命周期可由原 binding 回收 stale
    session；仍连接的真实活跃手势继续 fail closed 全局互斥。
  - 复习队列移除独立上一张/下一张按钮，复用 `rc-flashcard` 的原生横向 scroll-snap 和圆点
    pager。每张 slide 继续是独立 Anki 实体，保持 `id === cid === gid`。评分拒绝晚到时执行
    完整 card-transition，恢复原卡正面并清除下一卡答案、草稿和 busy，迟到草稿不能复活。
- **改动区域**：共享 `_server_deploy/static/pdf/rc-voicecall.js`、
  `rc-stickynote.js`、`rc-flashcard.js`、`rc-review.js`；扩展
  `src/web-pins.js`、对应 Chromium 测试及 `build.py` 生成的 vendor；新增/更新 charged drag、
  PWA card gesture、pinned selection、flashcard pager、review 竞态和旧 card-improvement
  Python 合同。manifest 已升到 `0.2.58`；vendor 只由 `build.py` 生成。
- **验证证据**：
  - Node reader contracts **512/512**；
  - Python 全量 **667/667，skip 14**；
  - 真实 Chromium：`test_review_candidates.py`、`test_card_favorite_payload.py`、
    `test_pinned_anki_selection.py`、`test_card_drag.py` 全部通过；其中复习拖卡在排除 Xvfb
    按住期间偶发 window blur 噪声后连续 6 次通过，运行时 blur 仍保持 fail-closed 取消；
  - `handoff_check.py`：`errors=0 / READY`，唯一 warning 为既有脏工作区；
  - release pipeline **18/18**；Reader `deploy_reader.sh --preflight-only` 的 114 项定向回归
    通过；`release_preflight.py --artifact ...0.2.58... --skip-browser` 为 READY；
  - JS/Python syntax、scoped `git diff --check`、vendor/source 逐字一致门禁通过。
- **不可变本地候选**：
  `extensions/bw-reader-webext-0.2.58-windows-test.zip`，74 files，1,191,389 bytes，
  SHA-256 `ff5bd67991bf622fd364c4a32fca7bde26edcc6f30900839d9b7f49583b24581`。
  本地及生产此前均无同名 0.2.58 主包，故没有覆盖旧不可变资产。
- **生产事实/事务收尾**：发现此前遗留的 Reader 事务
  `20260726T184446Z-521152` 已到 `current_switched` 且无进程、无持锁者。冻结候选
  `candidateDigest=bed0a686dbd3f056d9d685bdc5caf63c376fc8c0aed2924edf0a5c0a7cf9f466`、
  `payloadDigest=c6ca8d53ac7328f9b9d328af3b1d49a4f8e4564ac27d5b332913c4a467afec09`；
  108 个普通文件与 31 个 KG 文件逐项/验签均与生产一致。Codex 在唯一部署锁内以
  deployId/candidate/payload/status 的 CAS 核对，仅补写标准 `complete` result 并在 marker
  字节未变时移除 marker，没有复制或覆盖产品文件。生产现为
  `reader=0.2.57`、`kg=kg-0.2.57-6247b4799fb02b4699c7`；webapp PID `527839`、
  voice PID `527850` 稳定、`NRestarts=0`、`/login` 200、voice WS 101，quick-sync/daily
  timer active。当前无 Reader/webext 锁、marker 或发布进程。
- **发布边界与阻塞**：本段没有部署 0.2.58，也没有切扩展 channel；生产 PWA/服务端为
  0.2.57，扩展 channel 仍为 0.2.55。截至 05:19 JST，Windows 固定测试机在 Tailscale
  目录中仍显示 peer，但 `tailscale ping` 与 SSH 22 均超时，因此尚未将 0.2.58 装入固定
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`，未运行 Windows 工程验证，也未进行用户
  人工视觉/触控验收。向 `bwicarus-2` 发起 Taildrop 时命令退出 0，但目标明确警告
  `not replying`，故只可记为“已尝试发送”，不能声称 Windows 已收到。不得用 0.2.57 PWA
  与 0.2.58 扩展混测后声称同版本接管通过。
- **下一负责人/步骤**：Codex。Windows 恢复可达后，先按既有
  `BW Codex Chrome Test` 固定独立 profile 原子替换 verified 0.2.58 unpacked 候选并完成工程
  验证，不使用日常 Chrome。随后一次性交付完整人工清单，由用户操作普通网页和同版本
  0.2.58 PWA 四类真书；Codex 只监控请求、日志、持久化与单写语义。只有用户视觉验收和后台
  证据均通过，才由单一发布 owner 运行 `scripts/deploy_reader.sh`，验健康后再运行
  `publish_test_channel.py --deploy`；任一异常先修复，禁止部署。

## Codex：0.2.58 Windows 固定环境工程验证（2026-07-27 07:33 JST）

- **安装事实**：Windows 固定 `BW Codex Chrome Test` 环境已自行复算 ZIP 为
  `1,191,389` bytes、SHA-256
  `ff5bd67991bf622fd364c4a32fca7bde26edcc6f30900839d9b7f49583b24581`，随后原子替换
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`。扩展目录与
  `installed-version.txt` 均为 `0.2.58`，74 files；回滚目录为
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension-backup-pre-0.2.58-20260726T222237Z`。
  专用任务使用既有 `browser-profile-v2` 和 unpacked 路径启动，未修改日常 Chrome/profile。
- **Windows/CDP 工程证据**：Chrome `150.0.7871.182` 的目标扩展 worker 精确匹配
  `jddhhakcblmihidgdobfkcejjinpigak/background.js` 并报告 `0.2.58`；
  IndexedDB store、data registry、`owner-lease/1` 与 `interaction-policy/1` 均加载且合同
  自检通过。普通 HTTPS 页只有一个 `#bw-reader-host` 和一个 `#bw-reader-pins`，两棵 open
  shadow 的 header/sidebar/review workspace/selection toolbar/pin root 骨架完整；隔离世界
  为完整 `web` adapter，highlight/pins/ink/WebSocket bridge 存在，PWA/provider-only 均为
  false，网页控件可用而书籍控件隐藏。
- **非破坏行为回归**：临时宿主页按钮只触发一次，原生 input 保持唯一输入；
  点击纯空白区域没有文本选择、查词框、选择工具条或 `dict-quick` 请求。CDP 收到的两次真实
  wheel 事件均 `defaultPrevented=false`，页面滚动 `700px`；首次远程 wheel 注入偶发未进入
  renderer，但页面事件日志证明不是扩展取消，随后事件即时滚动。重载后 host/pins 仍各一个，
  第二独立标签页也各一个；pageerror、目标扩展 console error 均为 0。
- **当前现场与边界**：专用任务保持 Running，9222 仅有一个 listener，方便下一步由用户在同一
  固定窗口执行人工验收；不得把上述自动化当作视觉/触控验收。生产仍为 Reader/PWA `0.2.57`，
  extension channel `0.2.55`，所以尚不能用生产 PWA 与 0.2.58 扩展混测并宣称同版本接管通过，
  也尚未部署/发布 0.2.58。下一负责人仍为 Codex：先提供安全的同版本 PWA 验收入口并一次性交付
  完整人工清单；用户完成界面操作时 Codex 只监控请求、日志和持久化，双证据通过后才可发布。
- **已撤销的不安全 staging 尝试（07:37–07:39 JST）**：曾短暂把 worktree app 指向克隆
  `WEBAPP_DATA/sidecar` 并暴露在临时 HTTPS `:9443`；二次导入审计发现 `voice.py` 仍硬编码扫描
  生产 `state/cli-tasks`，因此在用户开始任何操作前立即关闭 Serve 和 transient gunicorn。
  该实例只收到登录页、书库 GET 和静态资源 GET；`state/cli-tasks` 在该时段无新 mtime，扫描后
  非终态记录为 0，现有 Claude/Codex 进程仍存活，生产 app.db 哈希未变，webapp/voice 继续 active、
  `/login` 200。当前 9443 handler、5058 listener 和 staging unit 均不存在；不得恢复该方案。
  后续 PWA 人工入口必须把 `WEBAPP_DATA/CLAUDE_PROJECT/OBSIDIAN_VAULT` 全部隔离并在 import 前
  stub voice/assistant，或使用等价的正式 origin 合成夹具，不能再 import stock `app:app`。

- 2026-07-27 08:03 JST：Codex 认领“卡片尺寸实体化、Anki 隐藏滚动槽与 Windows 固定环境
  版本门禁”工作段。卡片三连击/三连点只在非控件区域开启右下角缩放把手，尺寸按稳定
  `cid === gid` 持久化并同步到同实体的侧栏、收藏、页面与复习投影；placement ID 继续只管
  页面位置。Anki 两层滚动容器只隐藏滚动槽，不关闭滚轮/触摸/键盘滚动。Windows 长按异常已
  只读确认为在线 launcher 把固定环境从 0.2.58 回退到 0.2.55（manifest、installed-version
  与 isolated runtime 三证一致），不会据此误改 0.2.58 选择算法；将补固定环境实际版本和
  `bindCardSelection` 能力硬门禁。当前不部署、不发布、不操作日常 Chrome/profile。

## Codex：0.2.59 卡片尺寸与人工验收候选（2026-07-27 08:51 JST）

- **实现范围**：共享 `rc-voicecall` 为已登记实体卡加入“三次短点后显示右下角把手”的尺寸
  调整；拖动期间同 `cid === gid` 的全部投影实时同步，`pointercancel` 回滚，只有抬手成功才
  写一次。扩展使用 `cardPresentationV1:<verified-account-namespace>`，PWA 使用自己的
  device `ui-session/card-presentation-v1:<cid>`；尺寸不写 placement ID，也不进入服务器或
  跨设备同步。`rc-flashcard` 隐藏内外两层纵向滚动槽但继续保留 `overflow-y:auto`、滚轮、
  触摸惯性和键盘滚动。Web/PWA placement 只消费实体尺寸回调。
- **改动/测试**：运行时代码为 `_server_deploy/static/pdf/rc-voicecall.js`、
  `rc-stickynote.js`、`rc-flashcard.js`、扩展 `background.js` 与 `src/web-pins.js`；
  vendor 仅由 `build.py` 重建。新增尺寸动态合同，并补扩展账户 namespace 动态合同。
  相关定向合同 80/80、全部 Reader Node 合同 **521/521**、扩展 provider 单文件 57/57、
  release pipeline 18/18；`handoff_check.py` 为 `errors=0 / READY`，发布预检确认
  `0.2.55 → 0.2.59` 单调且未部署。唯一 warning 仍是既有脏工作区。
- **不可变候选**：`extensions/bw-reader-webext-0.2.59-windows-test.zip`，74 files，
  1,196,505 bytes，SHA-256
  `a1247bcd42ee9acc4580a35244d2e8ad681ce8aaaed096fcfcca2f8e08adf1f6`；
  旧 0.2.58 仍为
  `ff5bd67991bf622fd364c4a32fca7bde26edcc6f30900839d9b7f49583b24581`，未覆盖。
- **Windows 固定环境事实**：第一次原子安装后，一个已经在途的在线 launcher 又把目录覆盖
  成 channel 的 0.2.55；它结束后没有遗留 updater 进程。Codex 随后从已复算 SHA 的本地候选
  再次原子替换专用
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`，并使用既有 `BW Codex Chrome Test`
  任务和 `browser-profile-v2` 启动。等待后 `manifest`、`installed-version.txt` 与目标扩展
  worker `jddhhakcblmihidgdobfkcejjinpigak` 三者都报告 **0.2.59**，manifest 运行时包含
  `rc-voicecall`、`rc-stickynote`、`web-pins`；专用任务保持 Running。当前回滚点为
  `extension-backup-online-revert-0.2.55-20260726T234952Z`，另保留首次安装前备份
  `extension-backup-pre-0.2.59-20260726T233745Z`。未触碰日常 Chrome/profile。
- **边界/下一步**：本段没有执行浏览器视觉/交互操作，也没有部署 Reader 或发布 channel；
  生产仍是 PWA/服务端 0.2.57、扩展 channel 0.2.55。用户只需在当前已经打开的专用 Chrome
  人工复测：Windows 页面卡正文长按及天气卡同步高亮、Anki 隐藏滚动槽但仍可滚动、非控件区
  三击出现把手并调整尺寸、同编号多投影同步及刷新后恢复。测试期间不要运行桌面在线
  `BW Extension Test` launcher，否则它仍会按生产 channel 回退到 0.2.55。用户反馈整套通过
  前不得部署。

- 2026-07-27 09:02 JST：Codex 根据 0.2.59 人工验收认领局部修复。已确认四个具体问题：
  页面尺寸错误依赖已验证账户导致普通网页保存失败；尺寸错误广播到侧栏等非页面投影；
  Anki 页面 placement 未命中外层滚动槽隐藏规则且内部卡面仍被 300px 上限截断；三击手势
  错挂整卡并排除了 `.fc-face`，只能点边缘。修复范围收窄为页面 placement 的本机呈现：
  同 `cid` 页面副本可共享，侧栏/收藏/复习不读取也不应用；三击严格复用长按的正文
  `pressTarget`。当前不部署、不发布，完成新候选与本地验证后再交用户复测。

## Codex：0.2.60 页面卡片尺寸修复与 Windows 人工候选（2026-07-27 09:22 JST）

- **实现范围**：`rc-voicecall` 将尺寸能力从通用 `_pinReg` 拆到页面 placement 专属注册；
  三次短点和长按选中复用完全相同的正文 `pressTarget` 与 `_cardPressEligible`，Anki 正反面
  正文可用，卡头、按钮、链接、输入控件、评分和分页圆点均不可误触。尺寸只投影到同 `cid`
  的页面副本，侧栏、收藏夹和复习区不读取、不应用，也不生成调整把手。
- **本地存储与布局**：扩展新增顶层内容脚本专用
  `BW_PAGE_CARD_PRESENTATION_GET/SET`，后台在 `pageCardPresentationV1` 中按 `cid` 串行原子
  合并，不再依赖已验证账户，也不允许内容脚本整表覆盖；无扩展 PWA 继续使用 device
  `ui-session/page-card-presentation-v1:<cid>`。页面 Anki 自定义高度由共享卡壳逐层传到
  `fc-wrap/track/slide/fc-card`，解除真实卡面的旧 300px 上限；`fc-card`、横向 pager 和
  `vc-card-bd.fc-bare` 只隐藏滚动槽，原生滚动继续保留。
- **改动区域**：共享 `_server_deploy/static/pdf/rc-voicecall.js`、
  `rc-flashcard.js`；扩展 `background.js`、`src/facade.js`、manifest；对应
  `extension-provider`、`voice-card-resize`、`flashcard-pager`、pinned selection 与 charged
  drag 合同。扩展 `vendor/` 仅由 `build.py` 重建；未把尺寸写入页面 placement 数据或卡实体。
- **验证证据**：JavaScript 语法通过；Reader Node 合同 **526/526**；Python 全量
  **667/667，skip 14**；Windows/Safari 发布管线 **18/18**；`handoff_check.py`
  `errors=0 / READY`；`release_preflight --skip-browser` 为 READY，版本门
  `0.2.55 → 0.2.60`。Pi 上直接运行 headed Playwright 因无 X server 被环境拒绝，未把它
  记为浏览器行为失败，也未用临时浏览器替代用户视觉验收。
- **不可变候选**：`extensions/bw-reader-webext-0.2.60-windows-test.zip`，74 files，
  1,198,019 bytes，SHA-256
  `fb988e4aa0e25c1096568ad167416abab2b3658c880373e179862a0f7db457a7`。
- **Windows 固定环境**：Windows 端重新计算候选 SHA-256 一致、解包后 manifest 为
  `0.2.60` 且 74 files；已原子替换
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`，`installed-version.txt` 同为
  `0.2.60`。回滚目录为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.60-20260727T002137Z`。
  `BW Codex Chrome Test` 任务现为 Running，固定 `browser-profile-v2` 的 Chrome 进程已
  重新启动，CDP 可见目标扩展
  `jddhhakcblmihidgdobfkcejjinpigak/background.js`，只读 `Runtime.evaluate` 返回 manifest
  版本 `0.2.60`；未运行在线 launcher，未修改日常 Chrome/profile。
- **边界与下一步**：没有部署 Reader/PWA，也没有切生产扩展 channel；生产仍为
  Reader/PWA 0.2.57、扩展 channel 0.2.55。下一负责人为用户：只在当前固定测试窗口人工
  复测页面卡正文三击/把手、尺寸保存刷新、侧栏尺寸隔离、Anki 高度填充与隐藏滚动槽。
  用户反馈通过前不得部署或发布。

- 2026-07-27 10:04 JST：Codex 根据 0.2.60 人工复测认领“Anki 复习卡面投影与固定操作层”
  局部修复。范围仅为：识别 Anki rendered answer 中正式或经正面等价证明的匿名答案分隔，
  避免翻面重复正面；把来源/原因/卡片编号/旧问 AI 等 provenance 和已结构化的来源链接从
  卡面投影隐藏但保留原始实体字段；将补充信息折叠；让评分与改进操作在长答案下保持可达；
  页面卡缩放待命在卡外点击后立即退出且不吞宿主事件。会补 DOM/真实 Chromium 合同并构建
  新的 Windows 固定环境候选；当前不部署、不发布、不操作日常 Chrome/profile。

- 2026-07-27 10:50 JST：上述修复已进入扩展 `0.2.61` 本地候选阶段。共享
  `rc-review/rc-flashcard/rc-voicecall` 现将复习卡原始 Anki HTML 与显示投影分离：
  Vocab 的等价 FrontSide 只追加纯答案，Cloze/日语完整卡面使用替换揭示；严格尾部
  provenance、已证明的旧 `.url/.src` 来源、模板标签和原始 Anki 音频占位只从显示投影隐藏，
  原实体/编号不改，补充内容进入默认折叠。来源提升要求单一安全
  `/pdf/view?file=&page=`，正反面冲突、多链接、额外参数和编码路径穿越均 fail closed；
  专用“打开原笔记”经 adapter 的 `goToInBook`，不再直接使用旧卡内链接。词汇卡生成器同步
  改用 `urlencode`，覆盖 CJK、空格、`%/&/#/?` 文件名。长卡正文成为唯一滚动区，四档评分
  固定在卡面底栏，改进入口/面板位于卡面滚动层外；卡外点击立即撤销三击尺寸把手且不吞宿主
  事件。当前验证：Reader Node **536/536**；Python **669/669，skip 14**；相关定向合同
  **47/47 + 来源/固定操作新增 9/9**；JS/Python syntax 与 scoped `git diff --check`
  通过。manifest 已升至 `0.2.61`，vendor 仅由 `build.py` 重建。尚未生成不可变 ZIP、尚未
  安装 Windows 固定环境、尚未用户人工验收、未部署/发布；下一负责人仍为 Codex，继续
  handoff/release/preflight、固定测试环境安装与只读版本核验，然后把完整人工清单交用户。

## Codex：0.2.61 复习卡面与固定操作人工候选（2026-07-27 10:57 JST）

- **不可变候选与门禁**：
  `extensions/bw-reader-webext-0.2.61-windows-test.zip` 含 74 files、1,201,239 bytes，
  SHA-256
  `1b97639139b7b76e497f15f6ec90005a1e3772275cde7199663e19e3bd49e64c`。
  Reader Node **536/536**、Python **669/669（skip 14）**、发布管线 **18/18**；
  `handoff_check.py` 为 `errors=0 / READY`，唯一 warning 是既有脏工作区；
  `release_preflight --skip-browser` 为 READY，版本门 `0.2.55 → 0.2.61`。真实 Chromium
  Vocab/固定评分/改进夹具已经补入工程测试，但按人工验收分工未由 agent 模拟点击，不能把
  静态预检冒充视觉验收。
- **Windows 固定环境安装事实**：Windows 端重新计算 incoming ZIP 为 1,201,239 bytes、
  SHA-256 与上面一致，解包后 manifest 为 0.2.61、74 files。第一次事务在停止专用任务后因
  WMI filter 语法错误于交换前退出；只留下已验证 staging，原 0.2.60 目录未动，随后只读确认
  专用任务 Ready、专用 Chrome 进程 0。第二次从该 verified staging 原子把
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension` 换为 0.2.61，更新
  `installed-version.txt` 并重新启动既有 `BW Codex Chrome Test` 与
  `browser-profile-v2`。回滚目录：
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.61-20260727T015530Z`。
  任务现为 Running，manifest/installed-version/目标 service worker
  `jddhhakcblmihidgdobfkcejjinpigak` 三证均为 0.2.61；Chrome 150 专用 profile 有 11 个
  进程。普通测试页只读 CDP 确认一个 `#bw-reader-host`、一个 `#bw-reader-pins`，复习开关/
  workspace 已加载，并且运行时 CSS 精确包含新的 `.rv-card-extra` 与
  `.fc-review-footer`。没有点击、评分、改卡或改变扩展私有数据；未触碰日常 Chrome/profile。
- **发布边界/下一步**：生产 Reader/PWA 仍为 0.2.57，扩展 channel 仍为 0.2.55，本段没有
  部署、发布或重启生产服务。下一负责人为用户：只在当前固定测试窗口按一次性人工清单复测
  真实 Vocab/Basic/Cloze/日语卡面的正反面、来源、补充折叠、固定评分/改进操作，以及页面卡
  外点击撤销尺寸把手；Codex 仅核对用户反馈与后台证据。任一异常先修复，全部通过后才可提出
  生产部署。

- 2026-07-27 13:20 JST：Codex 根据 0.2.61 人工反馈认领 Anki 操作层收敛。已确认当前仅
  `controlled` 复习卡使用固定 `.fc-review-footer`，普通学习态 Anki 卡仍把“显示答案/四档
  掌握度”放在可滚动卡面内。范围锁定为共享 `rc-flashcard` 与 `rc-review`：所有学习态 Anki
  投影统一为“正文独立滚动、掌握度固定底栏”，复习模式“改进”入口/展开区继续位于卡面滚动层
  外并保持底部可达；不改变草稿入库、评分幂等、实体编号、页面尺寸或其他卡片类型。完成后生成
  新的 Windows 固定独立环境候选供用户人工验收；当前不部署、不发布、不操作日常 Chrome/profile。

## Codex：0.2.62 Anki 固定底部操作层人工候选（2026-07-27 13:30 JST）

- **实现范围**：共享 `rc-flashcard` 不再只为 `controlledReview` 拆分卡面与评分区；侧栏、
  收藏夹、页面 placement、普通学习卡和复习队列卡的学习态统一使用
  `.fc-review-scroll + .fc-review-footer`。翻面前只显示正面，翻面后卡面正文单独滚动，四档
  掌握度始终位于卡片底栏；草稿、预览、已复习状态及评分幂等语义未改。`rc-review` 把“改进”
  折叠入口和其工具区从具体卡面投影移到 pager 后的 `.rv-review-controls` 底部操作坞，长卡面
  与长改进结果各自有界滚动，不再需要滚过卡面寻找入口。
- **改动区域**：`_server_deploy/static/pdf/rc-flashcard.js`、`rc-review.js`、
  `tests/reader_contract/review-fixed-actions.contract.test.mjs`、扩展 manifest/handoff；扩展
  `vendor/rc-flashcard.js` 与 `vendor/rc-review.js` 仅由 `build.py` 重建。没有修改卡实体编号、
  placement 尺寸、评分 API、助手聊天记录或其他卡片类型。
- **验证证据**：定向卡片合同 **7/7**；Reader Node 全量 **536/536**；Python 全量
  **669/669（skip 14）**；Windows/Safari 发布管线 **18/18**；`handoff_check.py`
  `errors=0 / READY`，唯一 warning 是既有脏工作区；`release_preflight --skip-browser`
  为 READY，版本门 `0.2.55 → 0.2.62`。scoped `git diff --check` 通过。
- **不可变候选**：`extensions/bw-reader-webext-0.2.62-windows-test.zip`，74 files，
  1,201,300 bytes，SHA-256
  `893bf11c57a01d98ea769ec4b7b9056e5de98d7e58c725757b42af8bc4b54604`；旧 0.2.61 ZIP
  未覆盖。
- **Windows 固定环境**：Windows 端重新计算 ZIP SHA-256 一致、解包为 74 files、
  manifest 0.2.62；已原子替换
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension` 并重新启动既有
  `BW Codex Chrome Test`/`browser-profile-v2`。回滚目录为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.62-20260727T042857Z`。
  只读 CDP 确认目标扩展 worker `jddhhakcblmihidgdobfkcejjinpigak` 报告 0.2.62；普通测试页
  `#bw-reader-host=1`、`#bw-reader-pins=1`，运行时已载入 `.rv-review-controls` 与
  `.fc-review-footer`。未执行点击、翻面、评分或改进写入，未修改日常 Chrome/profile。
- **发布边界/下一负责人**：生产 Reader/PWA 仍为 0.2.57，扩展 channel 仍为 0.2.55；
  本段没有部署、发布或重启生产服务。下一负责人为用户：在当前专用窗口人工确认普通学习卡与
  复习卡翻面后四档按钮都固定在最下方、长正文仍可滚动，以及“改进”折叠入口/菜单固定可达。
  用户反馈通过前不得部署。

## Codex：0.2.63 页面 Anki 底栏可视区修复（2026-07-27 13:35 JST）

- **人工反馈与根因证据**：0.2.62 的页面 placement 翻面后 DOM 中确有四档评分栏，但只读
  CDP 测得默认外壳正文区约 201px、内部卡面仍按 300px 布局；评分栏 bottom 为
  628/666px，而对应外壳正文 bottom 仅 532/571px，因此被外层裁到可视区外。侧栏有完整高度链，
  所以同一组件在那里正常。
- **修复范围**：`rc-voicecall` 的页面 Anki 高度传递选择器不再要求 `.vc-user-sized`；
  所有 full 态页面 placement（默认尺寸及三击自定义尺寸）都把 `vc-card-bd` 的真实高度逐层
  传给 `fc-wrap/track/slide/fc-card`，并由卡面内部滚动区吸收超长内容。没有移动评分 DOM、
  改评分语义、改卡片尺寸记录或影响侧栏/收藏/复习投影。
- **验证证据**：相关卡片/尺寸合同 **17/17**；Reader Node 全量 **536/536**；发布管线
  **18/18**；`handoff_check.py` `errors=0 / READY`；`release_preflight --skip-browser`
  READY，版本门 `0.2.55 → 0.2.63`。0.2.62 已跑过 Python 全量 **669/669（skip 14）**，
  本次仅改共享 CSS 选择器、合同与版本生成物，没有后端/Python 变化。
- **不可变候选与 Windows 安装**：
  `extensions/bw-reader-webext-0.2.63-windows-test.zip`，74 files、1,201,357 bytes，
  SHA-256 `3c25d9c32e5e8a1a4a4c0047a0145ce224aebd8804031f7138255d7d9c6c49d7`。
  Windows 端复算一致并原子替换固定独立目录；目标 worker 报告 0.2.63，任务 Running。
  回滚目录为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.63-20260727T043427Z`。
  更新后只读 CDP 对当前两张页面 Anki 卡测得评分栏均为 4 个按钮，footer
  `647–673 / 609–634` 完整落在 body `440–673 / 402–634` 内，`inside=true`。
- **边界/下一负责人**：未代替用户点击、翻面、评分或操作改进菜单；未部署 Reader、未发布
  扩展 channel、未触碰日常 Chrome/profile。下一负责人为用户，在当前专用窗口确认页面卡
  翻面后的固定评分栏、长正文滚动及复习区固定“改进”菜单。通过前不得部署。

- 2026-07-27 14:04 JST：Codex 根据 0.2.63 人工反馈认领“页面 Anki 固定底栏始终存在”
  局部修复。只读检查确认四档评分按钮属于 `.fc-card` 内部的 `.fc-review-footer`，但旧结构
  在 `_showBack=false` 时完全不创建 footer，只把“点击显示答案”作为正文滚动区中的提示；
  因此页面卡正面态没有任何可见底部操作层。范围仅限共享 `rc-flashcard`、对应合同、扩展版本
  与生成 vendor：改为卡片底栏始终存在，正面态显示“显示答案”，翻面后原位替换为四档评分；
  不改评分语义、卡片身份、页面尺寸、复习队列或侧栏专属布局。当前不部署、不发布、不操作
  日常 Chrome/profile。

## Codex：0.2.64 页面 Anki 常驻底栏人工候选（2026-07-27 14:09 JST）

- **实现与根因**：四档掌握度原本确实位于卡片本体内，但 `.fc-review-footer` 只在
  `_showBack=true` 时创建；正面态只有正文滚动区中的“点击显示答案”提示。共享
  `rc-flashcard` 现让 footer 在所有学习态 Anki 投影中常驻：正面态底栏是独立“显示答案”
  按钮，翻面后同一底栏原位替换为“再来/困难/良好/简单”。正面正文仍保留可点翻面区域，
  以免破坏页面卡长按选中和既有触控合同；评分、实体 ID、页面尺寸和状态机均未改。
- **验证证据**：定向固定操作合同 **3/3**，Reader Node 全量 **536/536**，
  Windows/Safari 发布管线 **18/18**；`handoff_check.py` 为 `errors=0 / READY`，
  `release_preflight --skip-browser` 为 READY，版本门 `0.2.55 → 0.2.64`。只读 Windows
  CDP 在真实页面固定卡正面态确认 `.fc-review-footer=1`、`.fc-reveal=显示答案`、目标扩展
  worker 版本为 **0.2.64**；没有替用户点击、翻面或评分。
- **不可变候选与 Windows 固定环境**：
  `extensions/bw-reader-webext-0.2.64-windows-test.zip`，74 files、1,201,394 bytes，
  SHA-256 `6495794bed59293f011750e646f5413744eb968e5cddf764a885a4b02f209086`。
  Windows 端复算一致并原子替换
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`；`installed-version.txt`、manifest
  与目标 worker 均为 0.2.64，`BW Codex Chrome Test` 为 Running、9222 正常。
  回滚目录：
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.64-20260727T140903Z`。
  第一次替换尝试在终止已退出的专用 Chrome 进程时安全中止于交换前，旧目录未移动；第二次
  使用幂等终止后完成，没有混装。
- **边界与下一负责人**：未部署 Reader/PWA，未发布扩展 channel，未触碰日常 Chrome/profile。
  下一负责人为用户：在当前独立窗口确认页面固定 Anki 卡底部先显示“显示答案”，点击后同一
  位置出现四档掌握度且可评分。用户视觉验收通过前不得部署。

- 2026-07-27 15:27 JST：Codex 根据 0.2.64 截图与同页只读 CDP 重新认领页面 Anki
  可视高度链修复。现场证明翻面后的 `.fc-review-footer` 和四个按钮均已生成且 CSS visible，
  但页面 body 可视高度仅 201px，内部横向 `.fc-slide/.fc-card` 被内容撑到 309px，footer
  位于 body 裁剪边界下约 103px；右侧复习卡高度链正常。0.2.63/0.2.64 的百分比高度规则虽
  命中选择器，却因横向 flex 轨道的交叉轴高度不确定而解析为内容高度。修复范围收窄为
  `rc-voicecall` 页面 placement 布局与对应合同：用 `minmax(0,1fr)` 网格行确定轨道高度，
  slide 改为 cross-axis stretch，正文只在 `.fc-review-scroll` 内滚动；不改卡片状态和评分。
  当前不部署、不发布、不操作日常 Chrome/profile。

## Codex：0.2.65 页面 Anki 可视高度链人工候选（2026-07-27 15:31 JST）

- **实现**：页面 placement 的 `fc-wrap` 改为 `minmax(0,1fr) auto` 网格，第一行固定承载横向
  track，第二行保留多卡圆点；track 禁止第二条纵向滚动，slide 通过 cross-axis stretch
  精确占满第一行，真实 `.fc-review-card` 受 `max-height:100%` 约束。长卡内容仍仅在
  `.fc-review-scroll` 中滚动，卡片本体内的常驻 footer 不参与滚动。侧栏/收藏/复习布局和
  评分状态机未改。
- **真实现场证据**：0.2.64 翻面卡中 body 为 201.3px、slide/card 为 308.5px，footer
  `bottom=465.0` 而 body `bottom=361.8`，按钮被裁掉。相同页面先用可撤销诊断样式验证新规则，
  slide/card 收敛到 201.3px、footer `bottom=357.8`，`inside=true`。安装 0.2.65 并重启后，
  正面态 body/track/slide/card 四层均为 116px，footer `bottom=272.5`、body
  `bottom=276.5`，`inside=true`；目标 service worker 报告 0.2.65。未替用户点击或评分。
- **验证与候选**：相关卡片合同 **7/7**，Reader Node 全量 **536/536**，
  Windows/Safari 发布管线 **18/18**；`handoff_check.py` `errors=0 / READY`，
  `release_preflight --skip-browser` READY。不可变
  `extensions/bw-reader-webext-0.2.65-windows-test.zip` 含 74 files、1,201,517 bytes，
  SHA-256 `35f6c80883da127b131d993cd129eaa870830bea00256bbc6e939926b032c5aa`。
- **Windows 固定环境与回滚**：候选经 Windows 复算后原子替换
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`，任务 Running、9222 正常；
  回滚目录
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.65-20260727T153047Z`。
  未部署 Reader/PWA、未发布 channel、未触碰日常 Chrome/profile。下一负责人为用户：
  刷新当前测试页，确认页面卡点击“显示答案”后四档按钮固定可见且正文独立滚动；通过前不得部署。

- 2026-07-27 15:34 JST：Codex 根据最新人工反馈认领页面 placement 尺寸编辑手势修复：
  将正文“三次短点”改为鼠标双击/触屏双点，并修复触摸序列在每次抬手后的
  `pointerleave` 被整段清空、因而无法累计的问题。范围仅限共享 `rc-voicecall`、独立尺寸合同、
  扩展版本/生成 vendor 与交接记录；继续复用长按正文命中面及控件排除规则，不改卡片实体、
  尺寸持久化、长按选中、拖动或侧栏行为。当前不部署、不发布、不触碰日常 Chrome/profile。

## Codex：0.2.66 页面卡双击/触屏双点尺寸编辑候选（2026-07-27 15:45 JST）

- **实现与根因**：尺寸编辑由三次短点改为鼠标双击/触屏双点，仍只绑定长按正文
  `pressTarget` 并复用 `_cardPressEligible`；卡头、链接、按钮、输入框、评分键和分页圆点
  不触发。旧触摸链在第一点 `pointerup` 后收到自然 `pointerleave` 时会清空整段计数，现改为
  只中止仍在按下的未完成点；PointerEvent 与 Chromium 合成 click 只计一次，首个正常单击
  语义保留，完成双点后的第二个合成 click 被消费。页面卡正文加入
  `touch-action:manipulation`，继续允许滚动/捏合并避免双点缩放抢手势。尺寸 cid、持久化、
  把手拖动、卡外退出、长按选中和侧栏尺寸隔离均未改。
- **验证**：尺寸/Anki/pager 定向合同 **20/20**；Reader Node 全量 **536/536**；
  Python 全量 **669/669（skip 14）**。Python 首轮唯一失败是上一版按钮已改为“显示答案”而
  旧测试仍断言“点击显示答案”，仅更新该过期文案合同后定向 8/8、全量通过。Windows/Safari
  发布管线 **18/18**；`handoff_check.py` `errors=0 / READY`；
  `release_preflight --skip-browser` READY，版本门 `0.2.55 → 0.2.66`；scoped
  `git diff --check` 通过。
- **候选与 Windows 固定环境**：
  `extensions/bw-reader-webext-0.2.66-windows-test.zip`，74 files、1,202,121 bytes，
  SHA-256 `00424d10eb47fb15e24151a6861841863aedfaf6c0ac0d108abf78d1c41fec00`。
  Windows 端复算一致后原子替换
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension`；manifest、`installed-version.txt`
  和目标 service worker `jddhhakcblmihidgdobfkcejjinpigak` 均为 0.2.66，固定任务 Running、
  9222 正常。只读 CDP 确认普通测试页已载入扩展 host/pins，Shadow DOM 中存在新的
  `touch-action:manipulation` 规则。回滚目录：
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.66-20260727T064138Z`。
- **边界/下一负责人**：没有部署 Reader/PWA、没有发布生产 channel，也没有触碰日常
  Chrome/profile。自动合同无法代替物理触摸屏手势；下一负责人为用户，在已打开的专用窗口
  刷新后分别确认鼠标正文双击和触屏正文双点会显示右下角把手，单点/滚动不误触、控件区不误触，
  以及卡外点击会收起把手。人工反馈通过前不得部署。

## Codex：0.2.67 Reader、扩展渠道与 Windows 启动器正式发布（2026-07-27 16:42 JST）

- **人工门禁与最终版本**：用户已确认 0.2.66 整套视觉/交互验收全部通过。后台证据检查随后
  发现固定 Windows profile 一度出现 0.2.66 内容脚本配旧 service worker 的混合运行；
  页面卡尺寸写入会落到旧后台并报保存失败。0.2.67 不改变已验收的共享 UI，只增加后台字面
  构建版本与 launcher v10 的 loopback DevTools reload/版本核验，确保 worker 代次一致。
  产品名、action title、popup 和文档均统一为无空格的 **“BW网页伴读”**。
- **候选与验证**：
  `extensions/bw-reader-webext-0.2.67-windows-test.zip` 含 74 files，SHA-256
  `7265b7f6e09763d8f11a49ad31002ec7206bee5f01f033cef403497bfd71ff3a`。
  Reader Node 全量 **536/536**、Python 全量 **669/669（skip 14）**、发布管线
  **20/20**、handoff `errors=0 / READY`、浏览器发布合同与普通网页/PWA 接管矩阵全部通过。
  Windows 固定环境 live worker、manifest、`installed-version.txt` 和
  `worker-runtime-version.txt` 均为 0.2.67；manifest name 为“BW网页伴读”。真实后台
  `pageCardPresentationV1` 写入/读取 `{w:444,h:333}` 一致，探针记录随后已清理。
- **Reader/PWA 原子部署**：唯一事务 `20260727T073408Z-742032` 已完成，candidate digest
  `8fabde0d9d2a223ff8ef7f0a6fa0c5925fb1d24eb47ae00e29f08c072b3715f9`，
  payload digest
  `f9b88849f1581022e27690dfbcfb984024570bcd07a8ffa949610c85f512b4bb`；
  KG current 为 `kg-0.2.67-3a54cd45588e2d56f3f9`。回滚/取证目录：
  `/home/bwicarus/deploy-backups/reader/20260727T073408Z-742032`，前一 KG release 为
  `kg-0.2.57-6247b4799fb02b4699c7`。
- **扩展渠道原子发布**：生产 channel 已由 0.2.55 切换为 0.2.67，launcherVersion=10；
  线上 package、launcher 脚本及双文件 launcher ZIP 与本地候选逐字节同哈希。
  `audit_deployed_baseline()` 通过，无缓存官方 HTTPS channel 返回 0.2.67。渠道事务记录：
  `/home/bwicarus/deploy-backups/reader/webext-channel-20260727T073956Z-895brjms/channel-deploy.json`，
  status=`committed`、rollback=`not-needed`；`channel.before` 保存原 0.2.55 channel。
- **Windows 专用环境**：固定 unpacked 目录已是 0.2.67，回滚目录为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\extension-backup-pre-0.2.67-20260727T073234Z`。
  v10 启动器已按 SHA-256
  `68a2c3f1b7de50655c945c5e631c0eaa8b6e63263960d8579c02f6b4f04f4217`
  原子更新到专用目录，旧启动器备份为
  `C:\Users\bwica\AppData\Local\BWReaderExtensionTest\BW-Extension-Test.ps1.pre-v10`。
  未修改日常 Chrome/profile。
- **生产健康**：`webapp.service` 与 `voice-rt.service` 均 active/running、`NRestarts=0`；
  `bwicarus-quick-sync.timer`、`bwicarus-daily.timer` active，
  `concept-graph.timer` 按既定边界保持 inactive；`/login` 为 HTTP 200。Reader 发布锁空闲，
  `deploy-in-progress.json` 不存在，当前无 `deploy_reader.sh` 或
  `publish_test_channel.py` 进程。
- **并发核对**：共享状态没有记录 Claude 在本次发布窗口认领或执行发布；现存 Claude 会话以
  read-only/禁用 Edit、Write 启动。生产 `reader.js` 中看似额外的 PDF 选区即时同步来自
  7 月 16 日已有的 `reader.src`，不是本次并发写入；第一次 channel 发布尝试仅因工作区
  `reader.js` 生成物陈旧而被 fail-closed 门禁阻断，生产 channel 未变化。使用规定的
  `build_pdf_reader_js.sh` 重建生成物后，生产载荷、源码与门禁一致，再完成上述原子发布。
- **下一步**：本发布段已完成，无待部署载荷。后续新增功能必须升新版本并重新走人工视觉验收、
  后台证据、不可变候选和原子发布流程；不得覆盖 0.2.67 制品。

- 2026-07-27 20:03 JST：Codex 认领“实时共享便签”本地最小闭环。范围限定为现有
  `/pdf` 认证域内页面、账户隔离持久化、结构化写入合同、实时变更通知与独立测试；共享便签
  不替代本状态文件，也不进入 Reader 文档批注/sync-v3 集合。部署 manifest 在本段开始时为
  未跟踪且时间戳较新，为避免与发布入口活动冲突，本段不修改 manifest、不部署、不发布、
  不重启服务、不操作任何浏览器 profile。

## Codex：实时共享便签本地候选（2026-07-27 20:16 JST）

- **实现范围**：新增 `_server_deploy/shared_note.py` 与
  `_server_deploy/templates/shared_note.html`，在现有 `/pdf` 认证域内提供页面、
  `GET/POST /pdf/api/shared-note`、账户 namespace 隔离存储、文件/进程双锁与原子替换。
  `app.py` 只增加注册接线，`nav.js` 的既有全站抽屉 footer 增加“共享便签”入口，
  `vbook_route_policy.py` 把三条路由明确登记为账户级 `GLOBAL`，不把它解释成书内数据。
- **写入合同**：`reader-shared-note/1` 支持全文 `replace`、尾部 `append` 和唯一逐字连续
  片段 `replace-text`；所有 mutation 必须带当前 `baseRevision`，可选 `updateId` 的完全相同
  重试返回 `idempotent-replay`，ID 换载荷、旧 revision、缺失/不唯一目标均明确 409。
  回执包含 revision、updatedAt、source、updateId 与实时通知结果。正文上限 512 KiB，
  receipt ledger 不返回页面/API 读取方。
- **实时与草稿安全**：复用 `/pdf/api/reader-events`，只广播无正文、无 revision、无来源、
  无 updateId、无账户标识的 `shared-note` 失效通知；页面随后重新读取自己账户的权威状态。
  无本地修改时自动更新；存在未保存草稿时保留草稿并显示比较、载入远端或明确以远端 revision
  为基线继续的选择。读取期间收到的 SSE 会排队补拉，不漏竞态更新。
- **讨论稿与文档**：首次账户读取会原子创建 revision 1，正文包含当前“只取可见/选区/焦点
  附近最小上下文、DocumentHost 分层、整卡上下文去重但不删缓存、工具按需读取”的上下文注入
  讨论，并明确本便签不替代协作状态。稳定 API 说明在
  `references/reader-shared-note.md`。
- **部署清单边界**：只读复核确认 `scripts/reader_deploy_manifest.py` 自 16:56 JST 后未再变化，
  且无活动部署/认领；为防未来只发布 `app.py` 却漏掉其启动依赖，最终仅显式登记
  `shared_note.py`、`shared_note.html`、`nav.js` 三个 exact 条目。未执行部署、发布、服务
  重启、候选版本升级或浏览器操作。
- **验证证据**：共享便签/认证/并发/路由定向 **18/18**；真实 `app.py` 临时数据目录中验证
  session 与有效 Bearer 可读写、错误 Bearer 401；现有 reader-events 队列集成通过；部署
  manifest/事务/只读探针与本功能合同 **54/54**；Python 全量 **688/688（skip 14）**；
  Reader Node 全量 **536/536**；Python/页面内联 JS/`nav.js` 语法及 scoped
  `git diff --check` 均通过。并发双写实测为一个 200、一个带当前权威正文的 409。
- **风险与未做事项**：没有真实浏览器视觉/交互验收。既有 SSE 是进程内广播，事件本身不泄露
  私有字段，但未来多账户同时在线时会让其它账户标签页多做一次读取自己便签的 GET；当前单用户
  与单 worker 架构下可接受，若未来扩展多用户再改账户级订阅。没有把便签并入 sync-v3、
  Reader 文档批注或扩展本地卡片存储。
- **人工浏览器验收清单/下一负责人**：下一负责人为用户，在未来准备发布前依次确认：
  ① 从任一现有站内页左侧导航进入“共享便签”；② 修改正文并保存，revision、来源和时间更新；
  ③ 两个标签页同时打开，在 A 保存后 B 无刷新自动更新；④ B 有未保存草稿时 A 再保存，B
  草稿不被覆盖且出现比较/载入/保留选项；⑤ 分别执行载入远端与保留本地后保存，界面结果符合
  按钮说明；⑥ 窄屏/PWA 中编辑区、冲突面板和保存按钮均可操作。人工通过后才可升新版本并走
  不可变候选与原子部署；当前停在本地候选。

## Codex：实时共享便签已直接部署（2026-07-27 22:49 JST）

- **发布决策与范围**：用户明确改为“自动检查通过后直接部署、用户在生产环境测试并反馈问题”的
  工作方式；本次据此发布共享便签最小闭环。Reader/扩展产品版本仍为 **0.2.67**；没有构建或切换
  Windows 扩展渠道、没有修改日常 Chrome/profile。发布内容仅为已登记的共享便签接线、页面、
  导航与部署 manifest 精确条目，不激活任何未审的 KG 或其他工作区差异。
- **事务与回滚**：原子部署事务 `20260727T134728Z-854532` 已 `complete`；candidate digest
  `e52f2b3ff6947e337c4d28fd3ec1e6e2ace4e8126bb296ad7539f137963066f2`，payload digest
  `68167fe284a910d16e228a9d5de8c9e752cc335fa98ad0aa410a91f8eab94884`。取证与可回滚备份为
  `/home/bwicarus/deploy-backups/reader/20260727T134728Z-854532`；KG release 前后均为
  `kg-0.2.67-3a54cd45588e2d56f3f9`。
- **自动验证与健康**：部署前 preflight 成功；部署流程内 Reader 合同、Python/Node 回归及
  `reader_e2e.py` 均通过。部署后 `webapp.service`、`voice-rt.service` 均为 `active` 且
  `NRestarts=0`，没有部署锁或运行中的发布脚本。生产 `shared_note.py`、模板与导航资源的 SHA-256
  与候选源一致；未登录请求 `/pdf/shared-note` 返回登录跳转，符合认证边界，不是路由缺失。
- **下一步/风险**：下一负责人为用户，登录后刷新现有 `/pdf/shared-note` 页面即可测试真实页面、
  保存、双标签自动更新及草稿冲突提示。用户将生产环境反馈作为后续修复依据；目前没有已知部署阻塞。

## Codex：电脑客户端语音桥接协议本地 groundwork（2026-07-28 20:02 JST）

- **范围**：新建、隔离的电脑客户端桥接协议与合同测试；目标是“浏览器电话按钮请求 → 仅在 Windows
  桥接器主动报告 online + app-ready + 本机 opt-in 后执行一次性语音启动命令”。不修改现有
  `reader-context` 桥、`/voice-rt` 语音 relay、部署状态或任何浏览器 profile。
- **边界**：不开放 Windows 入站控制端口；不实现默认输出设备/全系统音频回退；Pi 只作设备认证、
  状态和 WebRTC 信令，绝不接收或落盘麦克风/应用输出。Windows 原生安装、仅目标进程输出捕获及
  首次配对密钥尚未开始，须等本段本地合同完成后再进入。
- **已完成的本地实现**：新增隔离的 `_server_deploy/computer_voice_bridge.py`，合同为
  `reader-computer-voice-bridge/1`。它不注册 HTTP/WebSocket 路由、不传音频、不接触现有桥；
  只提供未来认证 adapter 可调用的账户/设备状态机。Windows heartbeat 必须同时声明目标应用
  ready、本机 opt-in、快捷键已配、麦克风可用以及精确的 `process-only` 目标输出；任何
  `system-wide` 回退都明确拒绝。网页电话按钮才可产生 10 秒一次性 `start-voice-shortcut`
  命令，网页投影永不含 nonce、快捷键、进程 ID 或音频；认证后的出站桥取命令后仍需本机再次
  检查并按 commandId 至多执行一次。过期、离线、跨账户、错误 nonce、重复确认均 fail closed。
- **文档/验证**：新增 `references/reader-computer-audio-bridge.md` 与独立
  `tests/test_computer_voice_bridge_protocol.py`。定向 6/6 与共享便签隔离回归 14/14 通过，
  `py_compile`、scoped `git diff --check` 通过；没有部署、服务重启、凭据、浏览器或 Windows
  机器操作。
- **明确未做/下一负责人**：没有把“电脑客户端”放入现有语音设置，避免在未配对时发布一个
  永远离线且没有实际音频链路的选项；没有创建 device pairing secret、Windows 安装包、进程
  loopback、快捷键模拟、受认证设备通道或 WebRTC signalling。下一负责人需在 Windows 阶段
  确认可用的 ChatGPT/Codex Desktop 进程捕获/API Automation，再由用户在本机完成一次性配对；
  未确认前不得把音频降级为系统输出或暴露 Windows 入站端口。

## Codex：电脑客户端桥接 Windows 交互预检（2026-07-28 20:36 JST）

- **本段范围**：只读核验 Windows 前置条件，并新增未部署的
  `extensions/bw-reader-webext/windows/bw-computer-voice-preflight.ps1`。脚本不采集音频、不发快捷键、
  不读聊天/窗口内容、不建端口、不写配对密钥、不改系统设置；它只报告进程 loopback 与目标桌面进程
  是否具备进入后续显式授权步骤的条件。
- **实际证据**：Windows 版本为 10.0.26200.8875，满足 process-loopback 所需的最低 build；
  交互 Session 1 中已发现 OpenAI.Codex 包内 `ChatGPT.exe` 进程树，当前 Codex/ChatGPT 桌面程序
  正在运行。远程 SSH/PowerShell 属于 Session 0，不能安全读取或操作用户桌面，因此不会也没有
  从该会话模拟快捷键或尝试注入 UI。脚本已在 Windows PowerShell 语法解析通过；从 Session 0 实跑
  时按预期 fail-closed 返回 `non-interactive-session`。`git diff --check` 通过。
- **当前阻塞/最小下一步**：必须由用户在 Windows 的交互桌面运行一次预检，才能取得真实 Session 1
  结果；尚未在 Windows 持久化安装任何新桥接器，也未触碰旧 `reader-context`。截图中的旧启动器被
  PowerShell 执行策略阻止，可仅对该次命令使用 `-ExecutionPolicy Bypass`，不需要也不应修改全局
  ExecutionPolicy。下一负责人：用户执行交互桌面预检或授权把只读预检脚本复制至一个新的隔离目录；
  在此之前不得开始配对、音频捕获或快捷键控制。

## Codex：Windows 交互预检脚本已就位（2026-07-28 20:40 JST）

- **用户授权与落点**：用户明确授权复制只读预检脚本。已新建隔离目录
  `C:\Users\bwica\bw-computer-voice-bridge`，只放入
  `bw-computer-voice-preflight.ps1`；没有安装常驻桥接器、创建配对信息、修改执行策略或触碰旧
  `C:\Users\bwica\bw-reader-context`。
- **逐字节与语法证据**：Windows 文件长度 3977 bytes，SHA-256
  `8802ef3c5d1359567233b81f8a190a643436b4a17e11f1e4d0e7f37f4e083e8a`，与项目源文件一致；
  Windows PowerShell parser error count 为 0。从 SSH Session 0 对复制后文件做只读实跑，按合同
  返回 `currentSessionId=0`、`reason=non-interactive-session`、`bridgeReady=false`，证明错误
  会话仍 fail-closed；没有采集音频、发送快捷键、开放端口或进行桌面控制。
- **唯一待用户动作**：用户须在 Windows 交互桌面打开 PowerShell，运行
  `powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\bwica\bw-computer-voice-bridge\bw-computer-voice-preflight.ps1" -Json`
  并回传输出。`-ExecutionPolicy Bypass` 只作用于该次子进程，不改变系统策略。取得 Session 1
  结果后才可判断是否进入桥接器本机安装与显式配对；本段停在该安全门槛。

## Codex：电脑音频桥 + voice-typist 组合启动认领（2026-07-28 20:48 JST）

- **用户确认**：Windows 交互预检真实结果为 Session 1、OpenAI.Codex 的 `ChatGPT.exe`
  唯一根进程树可定位、process loopback eligible；用户进一步明确，Reader/扩展选择“电脑客户端”
  后点击电话按钮，应在启动麦克风/目标进程音频桥时一并启动现有 `voice-typist`。
- **本段认领**：仅修改新建的 `computer_voice_bridge` 协议、其独立测试与新的隔离 Windows
  supervisor/测试；先不改共享 Reader UI、部署接线或旧
  `C:\Users\bwica\bw-reader-context\reader-bridge`。设计为选择模型不产生副作用、电话按钮才产生
  一次性命令；supervisor 位于用户 Session 1、只主动向 Pi 连接，并以现有 launcher 的
  `Status`/`Start` 合同幂等启动 typist，已运行则复用。
- **并发与安全边界**：不启动/停止/修改旧 bridge，不读取或写出凭据，不采集音频、不发送快捷键、
  不部署、不注册开机项。用户所述 pid 12660 当前已不存在；对现有 launcher 的只读 `Status`
  核验也返回 `running=false`、无 panel、无暂停/急停，因此不能把历史 pid 当成当前在线证据。
  后续必须以 supervisor 的实时状态与命令回执为准。

## Codex：电脑音频桥组合启动核心完成（2026-07-28 21:00 JST）

- **真实前置条件**：用户回传的交互预检为
  `currentSessionId=1`、`interactiveDesktop=true`、`processLoopbackEligible=true`，
  OpenAI.Codex 包内 11 个 `ChatGPT.exe` 可归并到唯一根 PID 31180。Windows build
  26200 满足 `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK` 的系统要求。当前 typist 随后已
  重新运行，launcher 只读 `Status` 为 PID 21076、未暂停、未急停、queueDepth=0；本段没有调用
  Start/Stop/Pause/Resume。真实 `[[READER_SYNC]]` 已把该页完整正文、选区和图片引用注入本次
  对话，证明现有 journal→typist 链路可用。
- **服务端合同**：`_server_deploy/computer_voice_bridge.py` 的未部署合同改为
  `start-computer-voice` 组合命令。heartbeat 现在明确区分“具备启动能力”与“已激活”，增加
  `capture.active` 和固定 `voice-typist` companion 状态；电话请求可在两者尚未启动时排队，但
  只有目标进程音频/麦克风 active 且 typist running 后才接受 `started` ack。浏览器投影不含
  nonce、快捷键、PID、launcher 路径或音频。
- **Windows supervisor 核心**：新增
  `extensions/bw-reader-webext/windows/bw_computer_voice_supervisor.py`，尚未复制/安装到
  Windows。它固定调用本机既有 launcher，只允许 `Status`/`Start`；按当前 Session、精确
  `voice_typist.py` 路径与 PID 三重核对，PID 漂移、孤儿、多实例、暂停、急停均 fail closed。
  `already running` 文案本身不能算成功，必须经后置状态证明。组合顺序为应用就绪→仅目标进程音频
  与麦克风就绪→幂等确保 typist→原子落本地 commandId 回执→发送一次快捷键；挂断/异常永不
  Stop/Pause typist。服务端不能下发路径、动作或快捷键。
- **验证证据**：新 supervisor 与服务端桥合同定向 **20/20** 通过，覆盖正常启动、已运行复用、
  already-running 竞态、PID/孤儿/多实例、Session 0、暂停/急停、过期/重复 command、崩溃前先落
  回执、快捷键失败、capture 回滚、禁止 destructive action、禁止服务端覆盖本地路径，以及
  capture+typist 启动后置条件。两个 Python 模块 `py_compile` 与 scoped `git diff --check`
  通过。
- **未做/明确阻塞**：没有注册 Reader/PWA/扩展 UI、HTTP/设备通道或开机项，没有部署、配对、
  采音频、发快捷键、改执行策略或改旧 bridge。Windows 当前只有 .NET runtime、Python 3.13 和
  PowerShell，没有 .NET SDK、Visual Studio/MSBuild、Windows SDK/C++ 工具链，因而尚不能构建
  进程级 WASAPI helper；现有 typist 配置也没有 voice-start shortcut。下一步需要用户明确授权
  安装 .NET 8 SDK，并在 Codex/ChatGPT 中确定一个语音启动快捷键；随后才能构建/真机验证音频
  helper，再进入一次性设备配对和 Reader `computer_client` UI/路由接线。

## Cloud:page.context 产生点补齐(翻页稳定 → 整页正文 + 视觉引用,2026-07-28 未部署)

- **背景/缺陷**:Windows 注入器把 PAGE_CONTEXT 渲成 `PAGE | id | p页码`,语音侧完全不知道用户
  在看什么。经查**根因不在 formatter**:Pi 侧从来没有产生过 `page.context` 事件,也没有任何
  地方写 `page_context.json` —— 注入器的消费分支(第 854 行)和文件分支(第 526 行)都在等一个
  上游从未生产的东西。formatter 只是下游呈现。
- **验收要求(共享便签修订 18 / 任务书 A5)**:连续翻页/滚动**不逐页注入**;停在一页约 2-3 秒
  必须注入该页**完整文字层**并给出该页**本地综合视觉资源引用**;页上有即时操作(尤其选区)时
  立即更新该页**完整背景上下文**,而不是只发孤立选区。
- **产生点(三处,均为新增,零改动旧路径)**:
  1. `_server_deploy/static/pdf/rc-core.js` — `RC.ctxSync` 新增停留计时器
     (`_CTX_DWELL_MS=2500`、`_ctxArmDwell`)。导航上报后武装,**换页立即重置**,到点仍在同页
     才发一条一次性 `reason='dwell'` 上报。该请求刻意**不并进 `_ctxS.pend`**:reason 一旦进入
     合并状态就会粘住,之后每次翻页都带 `dwell`,服务端的"翻页不注入"闸门会被整个打穿。
  2. `_server_deploy/pdf_reader.py` — `pdf_api_active_reading` 落库后调
     `_maybe_emit_page_context()`:`reason!='dwell'` 且无选区 → 直接返回(纯翻页零事件);
     去重键为 `用户|书#页|选区指纹`,所以同一停留只发一次、而**选区一变就重发整页背景**;
     整段包在 `except Exception` 里 —— 出向通道故障绝不影响续读位置写入。
  3. `_server_deploy/reader_outgoing_context.py` — `build_page_context()`(纯确定性、零 AI):
     PDF 走 `_page_text_clean`、EPUB 走 `_epub_section_paragraphs`,上限
     `PAGE_TEXT_LIMIT=4000` 并显式标 `truncated`;取不到正文时**照发事件**并写
     `fallback_reason`(扫描页/文件不可解析/提取异常),消费方才能分清"这页没正文"和"事件丢了"。
     `visual` **只给引用**(`page_image` URL + `has_ink`),永不内联字节。
- **跨端兼容的关键一处**:journal 行本身是 `type:"page.context"`,但 Windows 注入器的归一化
  只认 `event`/`event_type` 的点分名(`type` 分支仅覆盖 focus/drawing/command-failed)。
  实测缺这一个字段整条事件会在消费端被 fail-closed 丢掉,因此 append 载荷显式同时带
  `event:"page.context"`。已用真实 journal 行 → 真注入器 `_journal_event` → `_format_injection_text`
  端到端跑过,并保留一条"去掉 event 就被丢弃"的对照。
- **Windows 侧(下游呈现,已就位未重启)**:
  `C:\Users\bwica\bw-reader-context\reader-bridge\reader-context-injector.py` 的
  PAGE_CONTEXT 分支现输出 `PAGE | id | p页 | why=dwell|selection | book=… | img=… ink=1` +
  `SELECTED |`(有选区时)+ `TEXT:`/`TEXT | none | why_missing=…`。`[[READER_SYNC]]` 静默包络、
  事件标识、TEXT/CLEAR 语义均未改。原件已重建为
  `reader-context-injector.py.bak-20260728`(可直接回滚);真机 `ast.parse` 通过,
  sha256 `7397671…7bb9aac2`(57322 B)与本地补丁逐字节一致。**进程未重启**,新 formatter
  在注入器下次启动后生效。
- **验证**:`tests/test_reader_outgoing_context.py`(62)、`tests/test_outgoing_fixture.py`(12)、
  `tests/test_context_sync_active.py`、`test_bridge_card_contract.py`、
  `test_reader_direct_commands.py` 合计 152 全过;前端 harness 在 node 里加载**真实**
  rc-core.js 验证"连翻 8 页途中 0 条 dwell / 停手后恰好 1 条 / 同页不重复 / 换页重新计时 /
  关开关后到点不发";`scripts/audit_reader_network.py --check` = **0 new debt**。
  `reader-specs/fixtures/outgoing-events.jsonl` 里原先的 `type:"page"` 草图已换成**真实产出
  形状**并补进 `fixtures/README.md` 字段契约。
- **已部署(2026-07-28 20:43 JST,用户授权)**:`reader=0.2.67`、
  `kg=kg-0.2.67-3a54cd45588e2d56f3f9`(KG 无变更),deployId `20260728T114316Z-1003620`,
  回滚备份与 KG 取证快照在 `/home/bwicarus/deploy-backups/reader/20260728T114316Z-1003620`。
  先跑 `--preflight-only` 全绿(合同 28/28、清单 145 项)再真部署;部署内 E2E 全过。
  部署后 `webapp`/`voice-rt` 均 `active` 且 `NRestarts=0`,写入侧两个定时器恢复 `active`,
  事务标记已自动清除,生产三个文件与仓库源逐字节一致(`rc-core.js`/`pdf_reader.py`/
  `reader_outgoing_context.py`)。
- **前置清障**:上一次失败事务 `20260728T094945Z-978317` 的 `deploy-in-progress.json` 会挡住
  任何新部署(设计如此)。清除前先证明回滚干净:115 条备份记录里 **108 普通文件 + 7 systemd
  单元逐字节一致、零差异零缺失**,KG `current` 仍指向 `kg-0.2.67-3a54cd45588e2d56f3f9`。
  另发现 `freeze_writers` 冻结后 `restore_active_units` 因回滚被判 blocked 而未执行,
  `bwicarus-quick-sync.timer` / `bwicarus-daily.timer` 自 18:50 起被停了约 2 小时(enabled 但
  inactive),已 `start` 恢复并确认下次触发时间正常。陈旧标记未直接删,归档为该事务取证目录下的
  `deploy-in-progress.stale.json`。
- **生产冒烟(合成数据 + 真实登录 cookie,直打 :5000)**:用一份合成 2 页 PDF
  (`_smoke-page-context.pdf`,冒烟后已删)验证四条合同全部成立 ——
  ① 纯翻页(无 reason 无选区)`page.context` **0 条**;② `reason=dwell` 出 **1 条**,
  `event=page.context`、`stable=true`、`text_available=true`、
  `text_source=pdf:字符层(已剔噪)`、`visual` 只有 `page_image` URL + `has_ink=false`;
  ③ 同页选区再出 1 条 `reason=selection`,**带选区且仍带整页正文**;④ 同一状态重复上报不重复发。
  随后 journal 出现**真实用户事件** `seq=748`:用户在 `资源/uploads/料理师part1.pdf` p2
  选中文字 → `why=selection` + **699 字整页正文**,证明选区路径在真机已生效(该路径不依赖前端
  新代码;`dwell` 路径要等浏览器加载新指纹的 `rc-core.js`,即用户下次刷新阅读页)。
  部署后 10 分钟内 webapp 日志无 traceback/exception。
- **明确未做/下一步**:Windows 注入器进程**未重启**,新 formatter 要等它下次启动才生效
  (回滚件 `reader-context-injector.py.bak-20260728` 就在同目录)。长轮询仍未上线。
  真机验收待用户刷新阅读页后停在一页 2-3 秒,确认 Windows `context.md` / 语音侧能看到整页正文。

## Codex：电脑客户端桥接器 Windows 隔离构建完成（2026-07-28 22:14 JST）

- **本段范围与安装事实**：按用户授权，仅在新的
  `C:\Users\bwica\bw-computer-voice-bridge` 安装官方 .NET SDK `8.0.423`、复制新 supervisor
  与本地配置、构建进程音频 helper；没有修改旧 `bw-reader-context`、服务、浏览器/profile、
  凭据或系统执行策略。官方 `dotnet-install.ps1` Authenticode 为 `Valid`，SHA-256
  `e8b873e18a81e5c4cd8ab69d84dac8fead291d50b3c44633cd7fddad709a13d6`；SDK 固定落在
  `...\dotnet8`，没有加入用户 PATH 或登记全局安装位置，`dotnet.exe` SHA-256
  `e94c17675054a5dec941971293f08c417756a9a4f78efb4b7f88e312272a07c2`。
- **实现与配置**：新增
  `extensions/bw-reader-webext/windows/ComputerVoiceAudio/**`（net8.0、零第三方依赖），固定
  `VAD\Process_Loopback` + `INCLUDE_TARGET_PROCESS_TREE`，补齐正确的 `IAudioClient` 12 槽与
  `IAudioCaptureClient` 3 槽。当前 CLI 只允许 `--describe`/`--self-test`，真实 capture
  仍硬拒绝且不存在系统输出回退。COM 复审发现异步 continuation 不能证明回调已经返回；已移除
  该路径对 operation RCW 的强制 `FinalReleaseComObject`，只释放已不再读取的参数 buffer，
  operation 交由活动回调/CLR 正常管理。新增本地配置样例并落到 Windows：
  `voiceStartShortcut=Ctrl+Shift+C`、`outputScope=process-only`、`localOptIn=false`；因此快捷键
  已记录但尚不能执行。
- **Windows 落点与回滚**：源码位于
  `C:\Users\bwica\bw-computer-voice-bridge\src\ComputerVoiceAudio`；一次替换前的完整可回滚目录
  为 `...\src\ComputerVoiceAudio-backup-20260728T131303Z`。supervisor 与配置的 Windows
  SHA-256 分别为
  `c567bded4d13fad412d063262ec41f1fd3f16809295c18215b0a36518e69c621`、
  `1b1167d3cd0344df72874964915278b8ff5aa4d7d8bd3e2609a27ca3cb2bfeae`，与项目源一致。
- **验证证据**：Python supervisor/服务端组合合同 **23/23**，`py_compile` 通过；Linux 隔离
  SDK 与 Windows 隔离 SDK 均为 build 0 warnings / 0 errors，C# 自检 **23/23** 且明确
  `audioActivated=false`。Windows DLL SHA-256
  `461cbaa2da19d8bc11ab8f49705754f36d954b2f52619c615d29b2c2059d990f`；配置结构与
  `Ctrl+Shift+C`/`process-only`/关闭 opt-in 的后置条件已在 Windows 只读核对。scoped
  `git diff --check` 通过。
- **没有执行/下一步**：本段没有启动语音、麦克风/应用音频采集、voice-typist、快捷键、设备
  配对、Reader 路由、WebRTC 或部署。下一负责人为 Codex：先实现有界 PCM capture、明确停止与
  回滚语义及浏览器直连媒体交接；随后才创建一次性配对和 `电脑客户端` UI。真实 capture、
  SendInput 和 pairing 均须继续保持 fail closed，不能因 helper 已能构建就报告“桥接器可用”。

## Codex：电脑客户端桥接最小端到端闭环完成（2026-07-28 23:52 JST，未部署）

- **改动范围**：补齐 Reader `computer_client` 模式、一次性配对/设备认证/状态/启动/ack/短期
  WebRTC 信令；扩展增加可信 offscreen 媒体宿主、Native Messaging、popup 配对与状态入口；
  Windows host 增加明确麦克风 + Codex/ChatGPT 进程树两条有界 48 kHz mono s16 PCM 管线、
  当前交互桌面应用就绪复核、typist companion 与一次性 `Ctrl+Shift+C`；安装器只允许明确
  extension ID、明确 Active 麦克风和逐字 `ENABLE` 后注册。Pi 仅传状态/命令/信令，不接收音频；
  Windows 无入站端口、无默认/全系统音频回退。
- **关键门禁**：选择模型不能启动；只有 Reader 电话按钮能创建 10 秒命令。offscreen 必须先
  证明配对、opt-in、native/app/mic/companion 就绪且 WebRTC 已连接，之后才允许 Native host
  开始 capture/typist/至多一次快捷键。token 只在可信扩展存储，服务端只存摘要，页面与 popup
  均拿不到。交互策略登记为 `computer-voice.bridge.request = remote-required/direct`，离线不
  排队、不乐观成功。
- **验证证据**：服务端/配对/路由/supervisor Python 合同 **48/48**；Native 协议、offscreen、
  Reader 集成、WebRTC Node 合同 **33/33**；popup/provider/语音门禁定向 **77/77**；interaction
  policy **9/9**；release pipeline **21/21**；部署 manifest、扩展 build、网络审计
  `0 new debt` 与完整 `handoff_check` 均通过。Windows 隔离自包含 host `--self-test`
  **68 项通过且 audioActivated=false**；Native Messaging 无音频冒烟得到
  `hello + capabilities`，错误 origin 被拒绝。
- **Windows 当前事实/回滚**：
  `C:\Users\bwica\bw-computer-voice-bridge` 中 host 已预置但
  `registered=false`、`localOptIn=false`、`extensionConfigured=false`；没有启动语音、采集、
  typist、配对或快捷键。exe SHA-256
  `8671ac8815e19a32e25f8bc515a0ccce6dff313c50762d6917db0596c84db4dc`；安装器和 helper 与
  项目源逐字节一致。未写注册表，因此当前回滚只需删除该新隔离目录；旧
  `bw-reader-context` 未改。
- **发布工具修复**：Windows 无音频测试会生成 `__pycache__`；精确源码审计现只忽略该缓存目录，
  仍拒绝其它额外目录/文件，并新增正反例。Safari 打包继续剥离 Windows-only
  `nativeMessaging/offscreen` 权限。
- **未做/风险/下一步**：本段按任务边界没有部署、发布或加载日常 Chrome。真实首次配对、麦克风
  权限、ICE 直连、应用音频听感和快捷键只可在候选部署/安装后由用户可见触发；当前无 TURN，
  直连失败必须明确报错。下一负责人仍为 Codex；下一阶段须先安装带该功能的候选扩展与服务端，
  再由用户在交互桌面选择麦克风并输入 `ENABLE`，最后执行一次电话按钮人工验收。

## Codex：0.2.68 与电脑客户端桥接候选已发布，停在本机显式授权（2026-07-29 00:15 JST）

- **Reader 原子部署**：使用项目正式 `scripts/deploy_reader.sh` 发布 Reader `0.2.68` 与
  KG `kg-0.2.68-0c46b32b64c447887694`；事务/回滚目录为
  `/home/bwicarus/deploy-backups/reader/20260728T150057Z-52227`。部署 E2E 全部通过，
  `webapp`、`voice-rt`、`bwicarus-quick-sync.timer`、`bwicarus-daily.timer` 均 active，
  两个服务 `NRestarts=0`。未认证访问电脑客户端设备接口会跳转登录，未暴露匿名写入或控制入口。
- **扩展渠道与回滚**：正式发布
  `extensions/bw-reader-webext-0.2.68-windows-test.zip`，SHA-256
  `57e9f21ad1da6c29c156acd945bf4e633077bf5e3a00e5de2c64179045688000`；测试渠道原子切换，
  回滚目录为
  `/home/bwicarus/deploy-backups/reader/webext-channel-20260728T150652Z-tjhvzulx`。
  `handoff_check.py --production` 为 `version=0.2.68 / errors=0 / READY`，生产 manifest、
  Reader/KG 文件、版本化 ZIP、渠道与 launcher 均逐字一致。
- **Windows 固定独立环境**：只更新
  `%LOCALAPPDATA%\BWReaderExtensionTest\extension` 与既有
  `browser-profile-v2`，没有操作日常 Chrome/profile。安装目录 manifest、版本标记均为
  `0.2.68`，名称为“BW网页伴读”，`nativeMessaging`/`offscreen` 权限与 offscreen 文件存在。
  发现目标 service worker 仍为 `0.2.67` 后，仅通过该独立环境的 CDP 调用
  `chrome.runtime.reload()`；复核运行 worker 已为 `0.2.68`，并写入
  `worker-runtime-version.txt=0.2.68`。既有 `BW Codex Chrome Test` 任务仍严格指向固定扩展
  目录与 `browser-profile-v2`，当前 Running。
- **本机桥接安装边界**：发布后再次核对
  `C:\Users\bwica\bw-computer-voice-bridge`：
  `hostPresent=true`、`typistHelperPresent=true`，但
  `registered=false`、`localOptIn=false`、`extensionConfigured=false`。已在用户交互
  Session 1 打开无自动触发器的 `BW Computer Voice Setup` 一次性可见配置窗口
  （启动时 PID 12184）；它当前只列出 Active 麦克风并等待用户选择，再要求逐字输入
  `ENABLE`。没有替用户选择、输入或完成注册，也没有配对、采音频、启动 typist、
  发送 `Ctrl+Shift+C` 或开始通话。
- **下一步/风险**：下一负责人为用户做首次显式授权：在已打开窗口选择耳机麦克风并输入
  `ENABLE`；之后在 Reader/扩展选择“电脑客户端”，按电话按钮进行首次配对和真实验收。
  当前无 TURN；WebRTC 直连失败必须明确失败，禁止退化为系统全局输出。用户完成本机授权前，
  桥接继续保持 fail-closed。

## Cloud:开发环境迁到 Windows(Pi 仍为唯一部署源,2026-07-29 03:30 JST)

- **范围**:只动开发侧与协作规则,**未碰生产、未部署**。Pi 依旧是部署真源与全部门禁所在。
- **做了什么**:①Pi 分支 `learning-loop-review-fixes`(领先 origin/main 497 提交)首次推送;
  ②`C:\claude` 从 `main`(7-12) 切到同一分支,切换前 stash 为
  `pre-windows-migration-20260729`(那 28 个脏文件已逐字节验证**全部被 Pi 历史吸收、零独有改动**);
  ③加 `.gitattributes` 钉死换行(`.sh`/systemd=LF、`.ps1`/`.cmd`=CRLF、vendor `-text`),
  实测 renormalize 影响面为零;④`.gitignore` 补 `webapp-data/`(含账号库 `app.db`,此前一直裸奔)
  与 `_server_deploy/static/pdfjs/`;⑤Windows 写了机器特定 `.env`(仅路径/URL,无密钥,已验
  `config.VAULT_ROOT=C:\obsidian`);⑥Claude Code 项目记忆 83 个文件从
  `-home-bwicarus-claude` 复制到 Windows 的 `C--claude`,旧的 15 条(5 月格式)备份为
  `memory-backup-20260729-pre-pi-sync`。
- **能力分层(实测 86 个测试模块,非估计)**:Windows 可跑 **44**;因 `fcntl` 结构性跑不了 **24**
  ——账户分区 sidecar 的并发锁是 POSIX-only,**用户已决定不改**(动生产并发锁风险不对等);
  只在 Pi 成立的部署/KG 门禁 **17**(在 Windows 失败是正确行为,不要去"修")。
  完整清单与理由见 `references/cross-machine-dev-setup.md`。
- **协作规则**:`AGENTS.md` 原"同一 checkout"那条假设只有一份工作副本,已补 4 条跨机硬边界
  (部署真源只有 Pi / 远程只 `merge --ff-only` **绝不远程切分支** / 跨机双写同文件要各开分支 /
  别把别人 WIP 收进自己的提交)。
- **共享检出安全闸** `scripts/deploy_remote_guard.sh`:Pi 工作树天生是脏的(daily 每晚重写
  `anki/records`+`dashboard.json`,加上另一 agent 的在制品),所以"脏就拒绝"不可用。闸门只拦
  **"本次要拉的提交恰好会改到别人正在改的文件"**,退出码 0/2/3/4 分流,自身只做一次 fetch
  不碰工作树(隔离仓库三场景实测)。
- **踩坑留档**:封装第一版用裸 IP,会退回 Windows 默认密钥而 Pi 授权表里没有它 →
  **整条链静默挂死在密码提示上,Pi 的 sshd 日志里连一条连接记录都没有**。正确做法是用
  Windows `~/.ssh/config` 既有的 `pi` 别名(专用密钥),**无需在 Pi 上新增任何授权密钥**。
  改用别名后 sshd 日志确认 publickey 已接受。
- **部署链已端到端验证(2026-07-29 03:5x,用户在 Windows 本机终端执行)**:
  `.\scripts\deploy_from_windows.ps1 -PreflightOnly` 四段全通 —— SSH(`pi` 别名)→ 安全闸
  → `merge --ff-only` → Pi 上 `deploy_reader.sh --preflight-only`,末尾
  `✅ 无副作用预检通过` + 封装的 `✅ 完成`。当时清单 150 项、KG `kg-0.2.68-0c46b32b64c447887694`。
- **⚠ 这条链不要由远程 agent 代跑**:从 Pi 经 `ssh → cmd → powershell` 嵌套驱动会因引号解析
  报 `The system cannot find the path specified.`,并在闸门之后失败;而**同样的命令在 Windows
  本机终端一次就过**。远程驱动还会留下孤儿进程(见下)。验证/部署请在 Windows 本机跑。
- **踩坑(排查方法论,值得记):`pgrep -f <模式>` 会匹配到发起它的那条 shell 命令行本身**。
  本轮因此①把"进程已启动"误判为真(实际从未启动),②`pkill -f` 只杀掉本地那半条,
  Windows 上累积了**三代孤儿** PowerShell+ssh(02:59/03:18/03:31,共 10 个 PID,已清理)。
  正确写法:把模式拆成变量拼接(`P='deploy_'"reader"`),或用不会出现在自身命令行里的模式。
  判据要看**副作用**(stage 目录、日志字节数),不要只看进程表。
- **给下一位**:Windows 侧现已可编辑 + 跑那 44 个模块;改到 sidecar/部署/KG 相关代码时,
  仍必须 ssh 回 Pi 验证。

## Codex：Reader ↔ Windows 电脑语音直连源码候选完成（2026-07-29 06:05 JST，未安装/未部署）

- **结论与拓扑**：电脑语音已改为
  `Reader/PWA ↔ wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1 ↔
  Tailscale Serve ↔ 127.0.0.1:43128 ↔ Windows C#`。Pi 仍只提供 Reader/书籍，不再参与
  新链路的配对、状态、启动、心跳、信令或音频。Reader 不再显示“生成一次性配对码”按钮；
  配对码只由 Windows EXE 在用户明确操作后生成。
- **旧故障根因**：外部旧 GUI 的 busy 路径引用了不存在的 `self.pair_button`，noconsole EXE
  把异常藏掉；旧扩展 offscreen/native host 实际不在线；Reader 的异步启动失败会遗留 active
  状态，延迟 `audio.play()` 也可能被浏览器拒绝。这解释了“打开 EXE 仍显示
  bridge-offline”“点通话自行停止且无声”，不是 Pi 音频中继本身能修好的问题。
- **Reader 与协议**：长期私钥为 IndexedDB 中不可导出的 ECDSA P-256；认证规范字节串固定为
  `reader-computer-voice-auth/1\nchallengeId\nnonce\norigin`。连接后 10 秒未认证、
  认证后 30 秒未 START 都释放唯一槽；active 后 5 秒 heartbeat、15 秒超时。PCM 固定
  48 kHz mono s16le、20 ms、1,956-byte frame，Reader 只播放 app-output。START 期间再次
  点击会立即 close；Windows 用唯一预取 ReceiveAsync 观察 close 并取消应用等待、capture 与
  快捷键链，非 close 消息只缓存一条并保持顺序。
- **Windows 服务与自动启动边界**：strict config 固定 loopback、Origin
  `https://bwicarus.taile44d0c.ts.net`、Tailscale identity `bwicarus@gmail.com`、
  `appKind=codex-desktop` 与 AUMID `OpenAI.Codex_2p2nqsd0c76g0!App`。只有认证 START 能打开
  Codex、启动 typist、显式麦克风与 process-only output、发送一次快捷键。登录 bootstrap
  空闲常驻但零采音；崩溃封顶退避重启。浏览器无法从零唤醒完全不存在的 listener，因此必须先
  一次显式安装/启用 bootstrap。
- **桌面控制安全收口**：任务仅在当前 SID、marker、唯一 trigger/action、exact EXE +
  `--bootstrap` 全部匹配时可 `/Run`；Serve 只接受固定 443 HTTPS host/path/backend，
  Funnel、TCP 转发、额外 handler 或混合配置全部 fail closed。进程终止在同一个
  QUERY+TERMINATE handle 上复核路径；停用与 bootstrap 启动竞态由 PID record 后置 config
  复核和 GUI 有界重查共同封闭。
- **本地候选**：
  `extensions/bw-reader-webext/windows/candidates/0.1.0/
  bw-computer-voice-direct-0.1.0-windows-x64.zip`，53,772,804 bytes，SHA-256
  `f05f50c663d0f0e459ef6c2de68006fa13d2cefbe9c4a0e672d3034d47f024ae`。ZIP 只含规范
  manifest、self-contained C# EXE、PyInstaller onefile/noconsole 桌面 EXE；包内双自检通过。
  固定 `PYTHONHASHSEED=0` / `SOURCE_DATE_EPOCH=315532800` 后，第二次独立构建的两个 payload
  哈希与 0.1.0 逐字一致；复现探针已删，只保留 0.1.0。候选 manifest 的 30 项源码哈希与当前
  工作树一致。
- **验证**：Reader 合同 **578/578**；C# Release 0 warning / 0 error、无音频 self-test
  **107/107**、`audioActivated=false`、format clean；桌面控制 **63/63**；打包安全
  **10/10**；旧电脑语音 Python **48/48**；release pipeline **21/21**（Windows 符号链接权限
  1 项 skip）；网络审计 `0 new debt`；`git diff --check` 通过。隔离浏览器已验证新设置文字、
  旧按钮缺失、无 audio 元素与零 console error。`handoff_check.py` 的 manifest/vendor/JS/
  browser/Reader contracts/网络/发布管线均通过，最终 `errors=2` 只来自 Windows 缺少项目明确
  保留为 POSIX-only 的 `fcntl` 三个导入链，未把它伪报为 READY。
- **当前机器后置事实**：`tailscale serve status --json` 仍为 `{}`；任务
  `BW Computer Voice Direct Bootstrap` 不存在；43128 无监听。外部旧安装目录仍有两个旧 GUI
  进程（PID 14396、23924），未结束或改动；它们不是新服务。
- **没有执行/下一步**：没有复制或覆盖外部安装、没有写 config/任务/Serve/注册表，没有启动
  C# direct server、Codex、typist、麦克风、进程音频或快捷键；没有提交、推送、部署，也没有
  改 Pi。真实 iPad/PWA 音频 E2E 仍未验收。下一步需用户醒后显式批准：备份并替换旧外部候选 →
  选择麦克风/启用 config → 安装 bootstrap → 应用 path-level Serve → 配对与电话人工验收；
  视觉与后台证据均通过后，才进入提交、推送、Windows 无副作用远程预检和正式部署。
- **保留 P2**：Task Scheduler CLI 的 query→按名 mutation、Tailscale Serve 的 query→CLI
  mutation 都没有 ETag/内核对象原子身份；当前以精确前后检查和混合状态 fail closed 缓解，
  同权限进程刻意并发替换仍需人工协调。

## Codex：Windows 0.1.0 直连桥接已替换安装（2026-07-29 09:40 JST，未启用/未部署）

- **授权与安装范围**：用户明确要求“安装替换”。本轮只替换固定安装根
  `C:\Users\bwica\bw-computer-voice-bridge` 下的
  `desktop-launcher\BW-Computer-Voice-Bridge.exe` 与
  `native-host\bw-computer-voice-audio.exe`；没有改快捷方式、旧配置、helper、注册表、计划
  任务或 Tailscale Serve。安装前精确目标进程已为 0，因此实际关闭进程数为 0。
- **安装前门禁**：0.1.0 ZIP 再次 `--verify` 与 `--self-test`，两者 exit 0；ZIP 为
  53,772,804 bytes，SHA-256
  `f05f50c663d0f0e459ef6c2de68006fa13d2cefbe9c4a0e672d3034d47f024ae`。安装根、目标目录、
  目标 EXE 与候选 payload 均为普通非 reparse 对象；43128 无监听、bootstrap 任务不存在、
  `tailscale serve status --json` 为 `{}`。桌面唯一相关快捷方式已经精确指向固定目标 EXE，
  无需重写。
- **备份与原子替换**：旧双 EXE 已备份到
  `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T003728326Z`；其中旧桌面/
  音频哈希分别为
  `4b15e58f5a0eeb0be250275de1f918d0954d6fce8779f48756cc698a30700436` /
  `8671ac8815e19a32e25f8bc515a0ccce6dff313c50762d6917db0596c84db4dc`，另保存候选
  `candidate-manifest.json`（SHA-256
  `7c766ffd409c6705a19062fe2a489aa3ff61647a150396c9ef8a707434864dad`）。首次
  `File.Replace` 使用空交换备份参数被 Windows 在写入前拒绝；复核旧哈希不变、零临时残留后，
  改用同目录明确交换备份路径完成原子替换，并在新哈希与独立备份均验证后只删除安装专用交换
  文件。
- **安装后证据**：已安装桌面 EXE SHA-256 为
  `c20ee9752cca1f22031bcd9e1c2d06d660379880cdb886a1f7c60b42f059292a`，音频 EXE 为
  `91fa7977d0481981eb64006bd0db19c5bb9585ed6be8714be6426040b865383a`，逐字匹配候选
  manifest。直接从固定安装路径运行两个 `--self-test` 均 exit 0；音频报告
  `ok=true`、`audioActivated=false`。既有 typist helper 哈希仍为
  `52ac819eae2b643bc828fd2d9785928554fc6b00115ed785a0b0497aadfc25d3`，与当前项目源一致。
- **最终安全状态/下一步**：direct config 与 runtime 目录仍不存在；目标进程 0、43128
  listener 0、bootstrap 任务不存在、Serve 仍为 `{}`、安装临时文件 0。也就是说候选已经安装，
  但尚未选择麦克风、启用 config、安装 bootstrap、应用 Serve、配对或通话；没有启动 Codex、
  typist、麦克风、应用音频或快捷键。Reader/PWA 新直连源码仍未提交、推送或部署，当前生产页
  不会因仅替换 Windows EXE 而切换到新协议。本轮未改 Pi、未提交、未推送、未部署。

## Codex：0.2.69 直连发布候选收口（2026-07-29 10:10 JST，待人工验收/未部署）

- **发布范围**：用户确认生产 Reader/PWA 不部署就无法使用新直连，因此进入提交、推送和受控
  远程预检准备；正式部署仍受“专用浏览器人工视觉/交互验收 + 后台证据”双门禁约束。Pi 只作为
  生产部署目标，未直接编辑其工作树或生产文件。
- **不可变候选**：扩展/共享 PWA runtime 版本为 `0.2.69`；Windows 测试包
  `bw-reader-webext-0.2.69-windows-test.zip` SHA-256 为
  `04a61ce051ec5b24e5c68bac07feb6b5a13a60610691a6ee5381f9e7046f8caf`。候选 channel
  精确指向该包。
- **launcher 版本围栏**：生产基线仍为 `0.2.68 / launcher v10`。跨机 `.gitattributes`
  契约使 Windows 原生 `.ps1/.cmd` 候选从生产 v10 的 LF 字节改为 CRLF；内容归一化后逐字相同，
  但不可变公开资产的原始字节已变化，因此未覆盖 v10，而是把 launcher 提升到 **v11**。
  `bw-reader-extension-test-v11.ps1` SHA-256 为
  `1c6a8b7bde4617fab271082e5cf23e183007a33cf6b2e59937050976f1527c91`；v11 双文件 ZIP
  SHA-256 为 `638b0f68fb94601c2460224042f3bdbeadb87f930c322a8a10c338865282f6a7`。
- **Windows 发布器兼容**：`publish_test_channel.py` 的临时发布进程锁在 Windows 精确使用
  `msvcrt` byte-0 kernel lock；只对 `EACCES/EAGAIN/EDEADLK` 每 50 ms 重试，其它 I/O
  错误 fail closed，非 Windows 保留原 `fcntl.flock`。真实双进程竞争、持锁进程被终止后的
  内核释放、空锁文件保持 0 bytes 均已覆盖；发布流水线 **24/24**，另有 1 项 Windows
  符号链接权限预期 skip。
- **候选门禁**：生产四件套基线已从官方 HTTPS 临时下载并逐项校验。候选版本单调递增、
  主包/channel/v11 脚本/双文件 ZIP 的白名单和 SHA-256 均通过；manifest、vendor、JS 语法、
  59 个 runtime 契约、网络审计 `0 new debt`、IndexedDB 87 assertions 与 text-range
  6 assertions 均通过。Windows 完整 `handoff_check.py` 最终仍为 `errors=2`，仅来自项目已
  明确保留为 Pi-only 的三条 `fcntl` 导入链；不得在 Windows 改写这些生产并发锁，须由推送后
  的 Pi 无副作用预检清除。
- **提交与首次远程预检**：直连、发布门禁和文档三笔提交
  `c7f30c8 / 68f7e3d / 2162772` 已精确推送到 `origin/learning-loop-review-fixes`。
  Windows 受控封装的首次 `-PreflightOnly` 证明：来袭 44 个文件与 Pi 的 15 个脏文件无交集，
  Pi `ff-only` 到 `2162772`；完整门禁生成 150 项清单与
  `kg-0.2.69-d1e3f7a2a02bccfa1393`，并明确输出“未创建生产备份、release 或 current”。
  后续若再追加证据类提交，正式部署前必须在新 HEAD 重跑同一预检。
- **尚未完成**：固定 `BW Codex Chrome Test` 独立环境仍是 0.2.68，尚未离线替换为 0.2.69
  并执行用户人工清单；尚未正式部署。真实 WSS 配对、自动打开 Codex、麦克风、应用输出听感和
  停止行为只能在部署并由用户明确启用 bootstrap/Serve 后做 iPad 真机 E2E。

## Codex：0.2.69 已直接部署；单用户阶段改为生产验收（2026-07-29 10:38 JST）

- 用户决定取消强制实验浏览器验收和独立重复预检：今后默认提交/推送后直接运行一次正式
  `deploy_from_windows.ps1`，保留脚本内置校验、原子备份、失败回滚，由用户在生产实际使用后反馈。
- Reader/PWA `0.2.69` 与 KG `kg-0.2.69-d1e3f7a2a02bccfa1393` 已部署，事务 exit 0、内置
  E2E 全过；回滚目录为 `/home/bwicarus/deploy-backups/reader/20260729T013506Z-20462`。
- 后验确认 Pi HEAD 与 `108d15b` 一致，webapp/voice-rt active、`NRestarts=0`、login 200、
  8767 监听、事务 complete；生产直连 UI 已生效且旧“生成一次性配对码”按钮不存在。
- Windows 服务当前仅本机 idle：43128 监听但 Serve `{}`、无 bootstrap、未连接 Reader、
  `captureActive=false`；下一步由用户直接在生产 PWA 反馈配对/通话结果。

## Codex：Windows 直连首次配对与登录守护完成（2026-07-29 11:18 JST）

- **生产配对已完成**：固定 Tailscale Serve 从空配置变为唯一 owned 映射
  `/reader-computer-voice/v1 → 127.0.0.1:43128/reader-computer-voice/v1`；一次性码明文仅在
  内存与生产 PWA 输入框中短暂存在，Windows 只落 SHA-256 摘要。配对后摘要与期限已清空，
  Windows 只保存 Reader 公钥/指纹；同一 PWA 刷新后及后台守护接管后都仅凭 IndexedDB 中
  不可导出 P-256 私钥自动认证，稳定显示“Windows 桥接器已就绪”，不再需要配对码。
- **真实超时根因与 C# 修复**：0.1.0 运行进程的 `/healthz`、直连路径和未知路径全为 404。
  `DirectBridgeServer` 把 fallback 404 注册成 terminal middleware，先于 endpoint 执行；改为
  `MapFallback` 后 healthz=200。直连 handler 又把 endpoint 路由保留的当前 Path 当成后缀而
  主动 404；删除重复判断后，精确路径普通 GET=426、额外后缀=404，Tailscale HTTPS 同路径
  也为 426，证明 WSS 请求已经到达固定 listener。
- **Windows 任务兼容修复**：Python UTF-8 mode 会把中文 Windows 的 `schtasks.exe` CP936
  输出误按 UTF-8 解码；runner 现先严格 UTF-8、失败再严格使用 `locale.getencoding()`。
  Task Scheduler 对无 BOM 的 UTF-8 XML 报“无法切换编码”，生成物已改为 UTF-16。Windows
  导入后会把 logon-trigger SID 规范化为当前账户名，并省略默认的 `Enabled=true` 与
  `RunLevel=LeastPrivilege`；ownership 现在只接受由同一次 `whoami` 验证的账户名或当前 SID，
  仍要求 principal SID、marker、唯一 trigger/action、固定 EXE/参数/工作目录及关键设置精确。
  桌面控制隔离测试 **65/65** 通过。
- **最终安装与守护**：不可变最终候选为本机忽略目录中的 `0.1.5`，包内双 self-test 通过。
  已安装 native SHA-256
  `d643ce7903ba0f06e36e6e52e14d8561c1fc15339190ea61e269cc1343828c87`，desktop SHA-256
  `d4b01b2060e8237ebc7ec318c633a4ce1a7a1216164f53f8368604d23f41d8ab`；最终桌面备份在
  `C:\Users\bwica\bw-computer-voice-bridge-backups\desktop-0.1.5-20260729T021513892Z`，
  此前每次 native/双 EXE 热修也均各有时间戳备份。任务
  `BW Computer Voice Direct Bootstrap` 已创建、ownership 通过且 Running；listener PID
  连续复核稳定、healthz=200、Serve 路径=426。
- **安全后验与剩余边界**：最终 `localOptIn=true`、paired client 存在、pending code 为空、
  service online、`readerConnected=false`（状态短连接已正常关闭）、`captureActive=false`。
  安装路径两个 self-test 均 exit 0，音频报告 `audioActivated=false`。本轮只做
  HELLO/PAIR/AUTH/STATUS，没有发送 START，没有启动通话、采音、typist、快捷键或应用输出。
  真实电话按钮的自动打开 Codex、麦克风/process-only output 和声音听感仍留给用户醒后生产
  实测；该真实音频 E2E 尚未冒充通过。

## Codex：0.2.70 / Windows 0.1.7 免配对直连已安装（2026-07-29 13:55 JST，待提交部署）

- **产品与协议变化**：Reader/PWA/扩展改用 direct v2，固定直连
  `wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1`；浏览器不再生成/输入配对码，
  不再保存设备私钥，也不显示 endpoint 配置。单用户实验模式仍要求 Tailscale 唯一登录
  `bwicarus@gmail.com`、本机 opt-in、单连接、显式已选麦克风与 Codex 进程树输出。旧 v1
  pairing 字段仅保留作缓存/回滚兼容，当前客户端不进入旧认证流程。Pi 只承载 Reader，
  不转发音频或电脑语音控制。
- **自动启动与 typist 生命周期**：登录 bootstrap 仅保持无采音 listener。只有 Reader 的
  START 才会打开/定位 Codex、启动本次任务所需 typist、初始化指定麦克风和 process-only
  output、发送一次语音快捷键。只有 helper 明确返回 `started` 时才登记精确 PID ownership；
  正常 STOP、断连、心跳/媒体异常、START 失败和 Dispose 都会经固定 launcher
  `Stop` 释放该 lease；`already-running`、竞态已运行或 PID 不匹配一律不停止别人的 typist。
  外部 `voice_typist.py` 未被本轮修改。
- **无声根因与修复**：旧 config 保存的是 registry 子项 GUID，不是
  `IMMDevice::GetId` endpoint；RDP 当前唯一 Active 麦克风实际为“远程音频”
  `{3.0.1.00000001}.{A3ED9185-1E02-411C-B11B-05D92F25CEF4}`。进程 loopback 虚拟设备又错误
  调用了不支持的 `GetMixFormat`，managed callback 也未满足 async activation 的 COM 生命周期。
  现改为 Core Audio 精确枚举、48 kHz stereo s16 固定 output format，以及实现
  `IUnknown + IActivateAudioInterfaceCompletionHandler + IAgileObject` 的 native vtable。
- **Windows 不可变候选与安装**：0.1.7 ZIP 为 53,806,365 bytes，SHA-256
  `526ccd38f8417c31e9fb6df2180260b03894d353822d94231780c6d6b3079e29`；四个 payload 已从
  manifest 校验后原子替换：native
  `1a339d7f7fa8b9c60b2cda8032884eb1cd06f015962df9dae2ddbf23439481f4`、desktop
  `0e974ed64e1fb8adfbfeec2f5ba190f64535f1225cdebb23e5c11ae138d57076`、typist helper
  `de030f73492c842e2ad12ce20f9cb5d7666eeb95d11f81ebda1bad21d5cf9fac`、supervisor
  `6b9a8e6724d50442618db8cc3322062b1d3ec4264c974bb68818c3c5e9f1e7d8`。永久安装备份与报告在
  `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T045416Z-8a362a1d`。
- **安装过程证据**：第一次操作脚本在首个 replace 前因 Windows 只读句柄 `fsync` 返回
  `EBADF`；四 payload 哈希确认未变后，从永久备份恢复 enabled config 并重启旧 listener，
  基线重新达到 online/Running/Serve ours，再修正为可写句柄重跑成功。旧孤儿 typist
  PID 19540 已经固定 launcher `Stop`，没有自动重启。最终任务 Running、Serve ours、
  listener PID 15184 online，`readerConnected=false`、`captureActive=false`。
- **无副作用实机音频证据**：安装路径双 self-test 通过；五轮
  `--diagnose-direct-audio-no-start` 全部 `ok=true`、`captureStarted=false`、
  `shortcutSent=false`。在当前 RDP Session 1 中，process output 初始化为
  48 kHz / 2ch / 16-bit、麦克风初始化为 44.1 kHz / 2ch / 32-bit，二者 HRESULT 均为 0。
  这证明 RDP 没有占死接口，但 RDP 会改变 endpoint 与最终听放路由；真实 START、双向可听、
  应用重启、断线/挂断仍必须由用户在生产页面点击电话后验收。
- **代码候选/发布边界**：共享 Reader/扩展候选为 0.2.70；合同 581/581、桌面 70/70、
  supervisor 17/17、helper 5/5、直连包 11/11、发布管线 24/24（另 1 项 Windows symlink
  权限 skip）均通过。Windows test ZIP SHA-256
  `4d2e0bab851f4d9fba10ce16ab394a69998d3fa70324a74e6af7c137b4acf149`，Safari/iOS ZIP
  SHA-256 `63a79101bfef3266b1d56aff2d6e35c9892116f4d64ffe7d9624f9351805e4ba`。
  本节登记时尚未提交、推送或部署；下一步只提交本轮精确文件，推送后从 Windows 运行一次正式
  `scripts\deploy_from_windows.ps1`，再由 Pi 官方测试 channel 发布器原子切换 0.2.70。

## Codex：0.2.72 / Windows 0.1.8 最终直连已安装部署（2026-07-29 15:21 JST，扩展 channel 待切换）

- **候选顺延**：0.2.70、0.2.71 与 0.1.7 已在后续安全/发布审查中退役；没有覆盖其既有
  不可变 ZIP。第一次正式部署在写生产前被网络审计拦住，原因只是两个固定 WSS 调用缺少
  `@interaction computer-voice.bridge.request` 声明；补齐并确认 `0 new debt` 后，最终
  Reader/扩展版本顺延为 **0.2.72**，Windows 直连包仍为 **0.1.8**。Pi 仍只承载
  Reader/PWA 与书籍，电脑语音控制和 PCM 始终为浏览器直连 Windows。
- **Reader/扩展收口**：PWA 只允许精确生产 Origin 直连；普通网页只能经 isolated content
  runtime → extension background → 固定 Windows WSS relay。电话按钮由 `rc-voicecall`
  闭包按 DOM 对象身份登记，伪造同 ID、clone、替换节点或 synthetic click 都不能取得 START
  lease。状态短连接会在真实 START 前完整让位；AudioContext blocked 时只保留最新 20 ms，
  正常播放中的合法 PCM 突发超过 400 ms 时丢弃旧 source 并重建排程，两者都不再自动关 WSS。
- **Windows 生命周期收口**：Origin 只接受生产 PWA、固定 Chrome 扩展 ID 和 canonical Safari
  Web Extension UUID。START 成功后才原子提交 capture、pump 与 typist 所有权；START 失败且
  第一次 typist 清理失败时保存 exact PID lease，下一次 START/Dispose 重试同一 PID。peer
  abort 不能取消已经确认 owner 的 STOP；服务单次 Dispose 内有界重试。`SendInput` 只要部分
  成功 1–5 项，就 best-effort 补发 `C↑/Shift↑/Ctrl↑` 并保持 START 失败，避免 modifier 残留。
- **最终不可变生成物**：Windows 0.1.8 ZIP 为 53,809,880 bytes，SHA-256
  `96f0fd6719c42f1f620537f72d655af4b696cc27f91976e7b3c40204479d7f2e`；payload 为 native
  `6ed8d45d1dc3e2bae5473f6ec0edb1b36533fadd7aa6dc58701e331d9bf35e04`、desktop
  `0e974ed64e1fb8adfbfeec2f5ba190f64535f1225cdebb23e5c11ae138d57076`、typist helper
  `de030f73492c842e2ad12ce20f9cb5d7666eeb95d11f81ebda1bad21d5cf9fac`、supervisor
  `6b9a8e6724d50442618db8cc3322062b1d3ec4264c974bb68818c3c5e9f1e7d8`。0.2.72 Windows
  测试 ZIP 为 1,238,489 bytes，SHA-256
  `b365fb2d8ba9d64dc622fd0dca66f4e67e999c168eaec715dba30cbd627b2960`；Safari/iOS ZIP 为
  1,225,895 bytes，SHA-256
  `71b0242c35d7de4ac0ecfea7bbc971c7551285a1b966f43f3040998273ed80d0`。
- **验证**：Reader 合同 **593/593**；C# Release 0 warning / 0 error、无启动 self-test
  **134/134**、`audioActivated=false`；桌面 **70/70**、supervisor **17/17**、typist helper
  **5/5**、直连包 **11/11**；发布流水线 **24/24**（其中 Windows symlink 权限 1 项预期
  skip）。两轮独立 C# 审查均未发现剩余 P0/P1；Reader network audit 为 **0 new debt**，
  `git diff --check` 通过。
- **Windows 安装事实**：0.1.8 四 payload 已经 manifest 校验后原子替换；永久备份和安装报告
  在
  `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T060742Z-687291f3`。
  安装后 config `localOptIn=true`，任务 ownership 通过且 `Running`，Serve 仍为唯一 owned
  映射，listener PID 11548 仅绑定 `127.0.0.1:43128`，healthz=200、状态 idle、
  `readerConnected=false`、`captureActive=false`、typist 进程 0。安装与验收未发送 START、
  未采音、未发快捷键。
- **RDP 与剩余真实验收**：13:54 Session 1 Active 时，无启动诊断曾证明“远程音频”麦克风和
  process output 均可初始化；13:58 RDP 断开后，当前 Session 1 为 `Disc`，活动麦克风枚举为
  0。RDP 不是独占 WASAPI，但其会话虚拟 endpoint 会随断开失效；因此真实通话必须先重新连接
  同一 RDP 音频会话，或在活动本地会话改选物理麦克风。当前协议没有 Reader/iPad 麦克风上行。
  真实可听双向 E2E 尚未验证。
- **STOP 边界/发布事实**：STOP 已可验证地停止两路 capture、pump、本次 owned typist 和 WSS；
  Codex 桌面尚无已验证的 ownership-safe Voice 退出 primitive，因此不会猜测第二次
  `Ctrl+Shift+C`，Voice UI 是否退出需在 Windows 人工确认。主提交 `98996ac` 与网络审计修订
  `72d8d35` 已推送；第一次正式部署在生产写入前被审计拦住，第二次正式部署 exit 0，Reader
  `0.2.72`、KG `kg-0.2.72-0961f9dc27dc8c96eda6` 与内置 E2E 均通过，回滚备份为
  `/home/bwicarus/deploy-backups/reader/20260729T061727Z-39246`。扩展 publisher 第一次也在
  写 channel 前 fail closed，因为 `reader-extension-handoff.md` 仍登记 0.2.69；本轮已把该
  唯一交接入口更新到 0.2.72，下一步仅需经正式脚本同步这份文档，再重试官方 channel 发布器。

## Codex：Reader 0.2.73 / Windows direct 0.1.11 候选收口（2026-07-29 JST，未安装部署）
- **改了什么**：Reader/PWA/扩展改为免配对固定 WSS 直连 Windows，双独立 Active eRender A/B、Reader 麦克风上行、Codex 进程树下行与 context→typist 生命周期接线完成；queue/3 receipts、owner 终止检测、per-user lifecycle Mutex 和停止态 durable queue status 关闭最终可靠性缺口。
- **怎么验的**：全量 Reader Node contracts、typist runtime、supervisor/helper/package、桌面测试、C# 无启动 self-test 与不可变 direct 0.1.11 package verify/self-test 均通过；独立最终复审无 P1/P2，所有诊断保持 `audioActivated=false`。
- **发布门禁**：0.2.73 本地 channel 候选与 launcher v12 生成通过；Windows release preflight 已通过版本单调、白名单、vendor、语法、合同和网络审计，随后只在文档已登记的 Windows `fcntl` 平台边界停止，生产 channel 未变化。
- **明确没做**：本机目前没有两根可用的独立虚拟音频线缆；未安装驱动、未选择 A/B、未启动真实 typist/采音/快捷键，未改日常 Chrome，未提交、推送、部署或改 Pi 工作树。
- **下一步**：用户通过驱动安装门后安装成熟已签名的两根虚拟线缆并明确选择 A/B，再按新功能清单做一次 Reader/PWA 人工通话验收；通过后才精确提交推送并走 Windows 官方部署入口。

## Codex：Windows direct 0.1.11 已安装并配置 A/B（2026-07-30 00:19 JST，未做真实 START）
- **改了什么**：重启后确认 VB-CABLE 与 VAC 均 Active；manifest 校验、永久备份、精确停启和失败回滚门禁通过后安装 0.1.11，并显式迁移 strict config `/1 → /3`；恢复点为 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T151602Z-04bc4eba`。
- **怎么验的**：安装路径双 self-test 通过；无启动诊断中 process output、A render、B endpoint 均 `ok=true`，且全程 `captureStarted=false`、未发快捷键；listener 已回到 `/2` 的 `idle / readerConnected=false / captureActive=false / lastError=null`。
- **应用路由**：Windows 音量混合器已把 ChatGPT 应用输出固定到 B=`Line 1 (Virtual Audio Cable)`、输入固定到 A 的录音端=`CABLE Output (VB-Audio Virtual Cable)`；未修改系统默认输入/输出。静态路由已保存，但 probe 只认正在发声的 Active session，当前无声时仍为 `OUTPUT_ROUTE_UNVERIFIED`。
- **明确没做**：没有启动真实通话、typist、采音或 `Ctrl+Shift+C`，未改日常 Chrome，未提交、推送、部署、发布 channel 或改 Pi；Reader/PWA 生产仍是 0.2.72，0.2.73 新链路尚未生产生效。
- **下一步**：先在隔离候选/人工清单中由用户点击一次 Reader 电话按钮，监控 START→双向音频→STOP 与 typist 生命周期；验收通过后再精确提交推送并按正式入口部署 Reader 0.2.73。

## Codex：隔离 Reader 0.2.73 已就绪待人工 START（2026-07-30 00:43 JST）
- **改了什么**：仅在 `%LOCALAPPDATA%\BWReaderExtensionTest` 原子替换本地 0.2.73 候选并启动 `BW Codex Chrome Test`；旧目录保存在 `extension-backup-pre-0.2.73-20260729T152645Z-b89c3522`，未访问仍为 0.2.72 的在线测试 channel。
- **怎么验的**：CDP 读取实际 extension service worker 为 0.2.73；生产 0.2.72 真书页显示 provider/extension=0.2.73、runtime=`pwa-extension-provider`、UI owner=`extension`，电脑客户端已选中且页面无配对码。
- **当前状态**：页面直连面板为“无需配对或填写地址”，只因尚无 ChatGPT Active 输出会话而显示 B 路由未验证；Windows 服务仍 `idle / captureActive=false / lastError=null`，typist=0。
- **明确没做**：未点击电话按钮、未申请麦克风权限、未发送 START/快捷键或启动 typist，未改日常 Chrome，未提交、推送、部署、发布或改 Pi。
- **下一步**：由用户在当前前台专用 Chrome 亲手点击电话按钮并允许本页麦克风；Codex 同步监控 START→A/B 双向音频→STOP、B Active-session 路由与 typist 生命周期。

## Codex：修复首次安装 typist 启动死锁（2026-07-30 01:08 JST，待重试）
- **根因**：0.1.11 首次安装无 `voice-typist.config.json`；helper 先跑 `Status`，其 `queue-status` 又先读配置，因而在能自动 `init-config` 的 `Start` 前失败为 `BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED`。
- **改了什么**：`queue-status` 仅在“配置、queue、ledger 全不存在”的全新状态下用默认 queue size 做只读空队列检查；已有 durable 状态却丢配置仍 fail closed；同一修复已同步到本机安装运行时。
- **怎么验的**：typist direct runtime 29 项、helper 14 项、supervisor 19 项与全量 Reader Node contracts 均通过；安装路径 `queue-status`/launcher `Status` exit 0 且未创建 config/state/log/pid。
- **安全状态**：失败后服务回到 `idle / captureActive=false`、typist=0、未发快捷键；未清除 lastError（只由下一次真实 START 成功清除），未改日常 Chrome、未提交、推送或部署。
- **下一步**：用户在已重新打开的隔离 Reader 0.2.73 再点击一次电话按钮；监控首次 `init-config`、typist owner lease、双向音频与 STOP 清理。

## Codex：修复快捷键前应用输出队列背压并安装 0.1.12（2026-07-30 01:26 JST，待人工重试）
- **根因/改动**：旧顺序先启动 process loopback、约 3 秒后 typist 才完成，而 32 包 output queue 在原子提交前没有消费者，静音包填满后 fail closed；现改为 `typist → A render → output capture → 端点/状态复检 → shortcut → commit/pump`，所有门禁与 exact typist lease 清理均保留。
- **验证**：C# Release 无启动 self-test（含新增“typist 完成前媒体不得 Start”合同）、直连包 15 项、0.1.12 manifest verify/包内 self-test 均通过；安装路径无启动诊断三路 `ok=true`，`captureStarted=false`、`shortcutSent=false`。
- **安装事实**：0.1.12 native SHA-256 `fe696a20832c004857223b2637783522276ad0d401172c0112bc20ed434b197c`；原子替换与失败回滚门禁通过，备份为 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T162547Z-63c54084`。
- **安全状态/下一步**：新服务 PID 30268 仅监听 `127.0.0.1:43128`，状态 `idle / readerConnected=false / captureActive=false / lastError=null`；未自动 START、采音或发快捷键，下一步仍由用户点击电话按钮做真实双向验收。

## Codex：改用 Codex OS-global 快捷键并安装 0.1.13（2026-07-30 01:49 JST，待人工重试）
- **根因/改动**：本机 `realtimeVoice=Ctrl+Shift+C` 由 Codex 注册为 `os-global`，旧桥却先抢前台；游戏前台与超长 foreground lock 使其在真正 `SendInput` 前失败。现删除前台切换/PID 门禁，直接模拟全局键，并把 exact child-tree 比较收敛为稳定 packaged-app root。
- **安全门禁**：typist/WASAPI 启动前与发送前均现读当前用户 keybindings，只接受唯一固定绑定；root 变化、配置缺失/重复/冲突、SendInput 非 6 项分别精确 fail closed，部分发送仍反向释放按键。
- **验证/安装**：Release build 与无启动 self-test、直连包 15 项、0.1.13 manifest verify/包内 self-test、安装路径 self-test/三路无启动诊断均通过；备份为 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T164824Z-85e8604e`。
- **安全状态/下一步**：新服务 PID 29908 唯一监听 `127.0.0.1:43128`，`idle / captureActive=false / lastError=null`，typist=0；未自动发键或采音，下一步由用户再点击一次电话按钮验证真实全局快捷键与双向音频。

## Codex：修复 SendInput ABI 并安装 0.1.14（2026-07-30 01:57 JST，待人工重试）
- **根因/改动**：0.1.13 已到达 `SendInput`，但 C# `INPUT` union 只含 `KEYBDINPUT`，使 x64 `cbSize=32` 而 Win32 要求 40；现补齐 mouse/keyboard/hardware 三成员，并按 pointer size 固定验证 native layout。
- **怎么验的**：本机修复前后实测 `32 → 40`；Release build、无启动 self-test（新增 ABI layout 合同）、直连包 15 项、0.1.14 manifest verify/包内与安装路径 self-test 均通过。
- **安装/安全状态**：installed SHA-256 `ef4f09b332167ed2e96205b766c457d90efc988a67b33d79a81a4ef1bab17dcd`，备份为 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T165720Z-e2ea7b7a`；服务 PID 25268 唯一监听 localhost，`idle / readerConnected=false / captureActive=false`，typist=0。
- **明确没做/下一步**：安装与诊断均 `captureStarted=false / shortcutSent=false`；未改日常 Chrome、未提交推送或部署，下一步由用户再次点击电话按钮验证真实快捷键与双向声音。

## Codex：Reader 0.2.73 / Windows 直连语音已正式部署（2026-07-30 02:18 JST）
- **改了什么**：BWAB、Claude 已完成的直接命令与 Reader/PWA/扩展/Windows 双向语音候选分两次精确提交，分支推送到 `b438a66`；Windows 原生仍为已安装的 0.1.14。
- **怎么验的**：隔离 Reader 的真实 START 已到 `active / readerConnected=true / captureActive=true`；Pi 无副作用预检与正式 `deploy_from_windows.ps1` 均 exit 0，内置 Linux 门禁、服务稳定性与 E2E 全过。
- **生产事实**：Reader `0.2.73`、KG `kg-0.2.73-923da8bd8a20f51b8e56`；回滚/取证目录 `/home/bwicarus/deploy-backups/reader/20260729T171607Z-91676`，部署后 webapp/voice-rt active、Reader HTTP 可达、voice TCP open。
- **已知项/下一步**：此前隔离会话约两分钟后记录过一次 `UPLINK_QUEUE_OVERFLOW`；按用户决定先用 iPad 生产真机测试再修。未发布扩展正式 channel，iPad PWA 不依赖该 channel。

## Codex：Reader 0.2.74 / Windows direct 0.1.16 快照 MCP 实验候选（2026-07-30 JST，未安装部署）
- **改了什么**：新增显式互斥的 `legacy-inject` / `snapshot-mcp`；实验模式由 PWA 经既有直连 WSS 把 Pi 活动阅读与增量事件折叠为 Windows 原子快照，常驻只读 MCP `reader_context_snapshot` 按需读取，旧文本注入代码与回滚入口完整保留。
- **安全边界**：快照后台连接不发 START、不启应用/音频/快捷键/typist；切换或关闭先清快照，超时正文 fail closed；既有语音链以用户真实验收为冻结基线，本轮没有重测真实音频。
- **怎么验的**：全量 Node、桌面/打包合同、C# 候选自测、官方 MCP SDK 持久进程/原子更新/损坏保留/过期清空测试及发布流水线通过；`git diff --check` 通过。
- **门禁事实**：0.2.74 Windows/Safari 与 0.1.16 不可变候选已生成；release preflight 的版本、摘要、白名单均通过，最终仅被既有 Windows `fcntl` 平台门禁及脏工作树拦住，生产未变化。
- **没做/下一步**：未安装、注册 MCP、提交、推送、部署或改日常 Chrome；下一步先交付完整 iPad 人工验收清单，通过后再按官方入口安装、注册并部署。

## Codex：Windows direct 0.1.16 与 reader_snapshot 已安装（2026-07-30 JST，Reader 未部署）
- **安装事实**：候选逐文件摘要校验后原子替换，配置迁移为 `/4 + snapshot-mcp`；永久回滚目录为 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-20260729T232653Z-f8a5f75c`。
- **MCP**：全局注册唯一 stdio 服务 `reader_snapshot`，命令固定指向安装路径 native EXE 与 `runtime\reader-context-snapshot.json`，仅暴露只读 `reader_context_snapshot`。
- **怎么验的**：安装路径双 self-test 通过；native/desktop 摘要与 0.1.16 manifest 一致，任务 Running、唯一 localhost listener 与状态 PID 一致，持续为 `idle / readerConnected=false / captureActive=false / lastError=null`，typist=0。
- **故障与恢复**：前三次热重启因尚未挂断的 PWA 自动重连为 `active`，安装器均 fail closed 并按摘要恢复 0.1.14；用户挂断后采用停机安装，最终启动验收通过，失败取证目录保留。
- **没做/下一步**：未发送 START、快捷键或采音，未改日常 Chrome，未提交、推送或部署 Reader 0.2.74；当前快照文件尚不存在，需新会话加载 MCP，并在 Reader 候选可用后做隔离人工验收。

## Codex：Reader 0.2.74 / Windows 快照 MCP 已正式部署（2026-07-30 JST）
- **发布事实**：快照 MCP 双模式源码精确提交并推送为 `f8bda39`；Windows 官方无副作用预检与正式部署均 exit 0，Reader `0.2.74`、KG `kg-0.2.74-91aaf94f2a8167582ec2`。
- **回滚**：普通文件备份与 KG 状态取证目录为 `/home/bwicarus/deploy-backups/reader/20260729T233604Z-115972`；部署脚本内置 E2E 全过且未触发回滚。
- **部署后验证**：Pi HEAD 与提交一致，webapp/voice-rt active；公网 Reader 与新版 `snapshot-mcp`、`context-open`、`context-clear` 静态资源均 HTTP 200。
- **Windows 状态**：0.1.16 仍为 `idle / readerConnected=false / captureActive=false / lastError=null`，配置 `snapshot-mcp`、`reader_snapshot` MCP 已注册，未自动发送 START、快捷键或采音。
- **下一步**：用户刷新 iPad PWA 并保持上下文同步开启，再新开 Codex 会话加载 MCP；首次页面快照出现后验证“当前页/当前选区”按需读取且输入框无注入。

## Codex：空墨迹阻断实时快照已修复并部署（2026-07-30 JST）
- **改了什么**：`None/{}/[]` 统一为确定性空墨迹，空态不再升出虚假绘图版本；新增跨字段一致性、清空旧引用和页类型合同测试。
- **怎么验的**：定向 Python 合同、全量 Node 合同、发布流水线及 Pi 原子部署门禁通过；生产快照恢复为 `ready`，第 26 页正文已写入 Windows。
- **发布事实**：提交 `86d1d68` 已推送并正式部署；webapp/voice-rt active，事务备份 `/home/bwicarus/deploy-backups/reader/20260730T050326Z-138413`。
- **没做/下一步**：未改语音音频链；旧 PWA 内存游标需完整刷新后从最新合法 `page.context` 引导，后续拆分实时页码/选区增量与本地书页解析。

## Codex：Windows 0.1.22 与 PWA 快照前台恢复已落地（2026-07-30 15:00 JST）
- **改了什么**：PWA 直传实时页码/选区，并在 `visibilitychange/pageshow/online` 后安全恢复 WSS；Windows 本地按书/页解析正文，查看器区分 AI 可用正文与仅诊断的陈旧缓存。
- **怎么验的**：前台恢复含延迟 `context-clear` ACK 竞态合同，全量 Reader Node 合同通过；Windows 不可变候选 verify/self-test 通过且 `audioActivated=false`。
- **发布事实**：Reader 提交到 `93a1f86`，预检与正式原子部署 exit 0，事务备份 `/home/bwicarus/deploy-backups/reader/20260730T055826Z-158334`；Windows 0.1.22 已安装并保留永久回滚目录。
- **当前现场/下一步**：Windows 仍停在 revision 1122 的陈旧第 26 页，缓存正文 1307 字可诊断查看；Pi 近五分钟无 iPad 请求，需 iPad 完整重开一次加载新脚本后验收 revision、正文与选区更新。

## Codex：快照身份、同源页图与首次语音 ready 修复已部署（2026-07-30 JST）
- **改了什么**：合并书 `focus/drawing` 与 active-reading 共用已验证 canonical 身份；Windows viewer 改为 localhost 本地 PDF 页图；START 在媒体和快捷键前有界等待 Codex voice capability ready。
- **安全语义**：无 canonical 的 vbook 事件 fail closed；页图不代理 Pi 凭据且失败不清正文/选区；voice 等待可取消，已 Active 不重发 `Ctrl+Shift+C`，超时为独立 retryable 错误。
- **安装事实**：Windows 0.1.23 双 EXE 摘要校验后原子替换，常驻 MCP PID 未变；服务 `healthz=200 / idle / captureActive=false`，回滚目录为 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.23-20260730T081047473Z-717eaa82`。
- **发布事实**：提交 `a7aff00` 已推送；正式部署 E2E 与部署后独立检查通过，webapp/voice-rt active 且 `NRestarts=0`，事务目录 `/home/bwicarus/deploy-backups/reader/20260730T081158Z-172576`。
- **下一步**：iPad 完整刷新后实测单击语音，以及正文、选区、页图、侧栏是否随当前页更新；本轮未主动 START、采音或发送快捷键。

## Codex：Reader 快照 MCP 收敛为 Windows bridge 单实例（2026-07-30 JST）
- **改了什么**：把只读 `/mcp` 合入既有 `--direct-serve`，stdio 入口仅作回滚；本机 Reader skill 改为直接调 MCP，不再逐轮启动 Python shell。
- **怎么验的**：C# build/self-test、Reader Node 合同、0.1.24 包 verify/self-test 通过；真实 Codex 两轮调用保持同一 bridge PID/instanceId，callSequence `1 → 2`。
- **安装/配置**：0.1.24 native 已原子替换，回滚目录 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.24-20260730T090722024Z-9c9fed26`；`reader_snapshot` 已改为 `http://127.0.0.1:43128/mcp`。
- **边界/下一步**：全程 `captureActive=false`，未发快捷键、未部署 Pi；Python/handoff 仍只有既有 Windows `fcntl`/Linux 路径失败。重启 Codex 后旧 stdio 子进程才退出，再复测 Desktop Process Manager 上游闪窗。

## Codex：Reader 与通话复用同一条 Windows WSS（2026-07-30 JST）
- **改了什么**：常驻快照连接原位晋升为通话连接，`close()` 等真实关闭且有界超时；PWA 与扩展按当前 UI owner 互斥持有唯一 WSS。
- **根因**：状态检查/START 在旧连接尚未释放时另建 WSS，以及两套运行时同时连单槽服务，均会命中 409 并被前端笼统显示为桥接器离线。
- **怎么验的**：相关合同测试、全量 Reader Node 合同及 Windows build/self-test 通过，新增同 session 晋升与 owner 切换断言。
- **既有基线**：全量 Python/handoff 仍受 Windows `fcntl`、Linux shell/路径与既有测试数据影响；本次无 Python 生产改动，定向合同无回归。
- **边界/下一步**：未主动 START、采音或发快捷键；Reader 部署后完整刷新 iPad PWA，用一次电话点击验收，扩展 vendor 仅随下次正式候选发布。

## Codex：Windows 0.1.25 修复通话数秒后断开（2026-07-30 JST）
- **根因/改动**：已证实断开由 200 ms 上行队列溢出触发；补 WASAPI `Start` 前静音预填、100 ms 首事件兜底，拥塞时丢最旧实时帧而不杀通话，协议/身份错误仍 fail closed。
- **连带加固**：runtime status 单次 I/O 写失败不再永久杀死五秒心跳，避免健康 listener 后续被监督器按陈旧状态重启。
- **验证/安装**：C# 无音频 self-test、桌面/打包定向测试及 Reader Node 合同全过；0.1.25 候选 verify/self-test 后原子安装，回滚目录 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.25-20260730T104200692Z-a3662911`。
- **当前状态/下一步**：任务 Running，唯一 listener/runtime/health 身份一致，双心跳周期同 PID/instance 且 `idle / captureActive=false`；未自动 START、采音或发键，下一步由用户在 iPad 真机确认通话持续与双向声音。

## Codex：修正 iPad 电话按钮首次单击与 WSS 晋升竞态（2026-07-30 JST）
- **根因更正**：真实点击顺序里 `setDialPending(true)` 与同值配置回执会先拆掉常驻快照 WSS，新连接撞 Windows 单连接门禁；初次配置尚未返回时还会丢失唯一用户手势，旧测试漏掉了这两步。
- **改了什么**：拨号原位认领常驻链，START 前只读探测死链并最多重建一次；设置 POST 使用独立 mutation token，普通 GET 不能越权解围；替代 WSS 必须等旧传输真正关闭，START 已发送后结果未知绝不重发。
- **怎么验的**：真实 `phoneClick → dialPending → config ACK → START`、迟到麦克风、静默 STATUS、stale-OPEN、慢关闭与 START-unknown 反例均通过；全量 Reader Node、生成物校验与发布流水线通过。
- **既有基线**：全量 Python/handoff 仍只命中相同的 Windows `fcntl`、Pi 路径与既有测试数据差异；本轮未改 Python 生产逻辑。
- **没做/下一步**：未主动 START、采音、发快捷键或部署；下一步精确提交、推送并走 Reader 原子部署，再由用户完整刷新 iPad 后单击一次电话按钮验收。

## Codex：绘图稳定状态不再永久卡 pending（2026-07-30 JST）
- **根因/改动**：停笔后的唯一查询只启动服务端稳定计时；前端收到 pending 后现在延迟约 1 秒仅确认一次，取得 stable revision 后停止，不做持续轮询。
- **怎么验的**：真实 `rc-core.js` harness 覆盖 pending→一次确认→stable→停止，全部通过；vendor 由 build.py 重生成。
- **边界/下一步**：本次只修稳定事件；MCP 仍只有绘图状态与引用，尚未传复合页图，因此 AI 仍不能视觉辨认笔迹内容。下一步接现有直接命令回写卡片，并单独补受控视觉读取。

## Codex：双向上下文新增旧注入 / 快照 MCP 可逆切换（2026-07-30 JST）
- **改了什么**：设置页新增单一模式开关；切换先取消在途拨号、结束当前通话/上下文 WSS，再由 Windows 原子保存 `legacy-inject` 或 `snapshot-mcp`，两条链互斥。
- **安全语义**：切回旧版前 Windows 必须清除快照；切换期间新的可信电话点击 fail closed，下一次通话才按新模式启动，桥接器常驻进程不退出。
- **怎么验的/安装**：生成物校验、Reader 定向合同与 Windows build/无音频 self-test 通过；0.1.27 已原子安装，健康为 `idle / captureActive=false`，回滚目录 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.27-20260730T131334217Z-552a090f`；Reader 部署后由 iPad 实测。

## Codex：Reader 0.2.75 / Windows direct 0.1.28 复用旧笔迹视觉并补齐回写（2026-07-30 JST）
- **改了什么**：快照 MCP 按需复用 `RC.captureInkRegion({page})` 返回正文与笔迹局部合成图；直接结果工具经既有认证 WSS 回写卡片，PWA 对话记录补回既有服务端备份。
- **安全语义**：文件、页码与绘图 revision 在截图前后及 Windows 收图后均需一致；双页只截请求页，图片分块有界，视觉读取不发 START、快捷键或采音。
- **怎么验的**：Reader Node 全量合同通过；0.1.28 包 verify/self-test、安装路径双 self-test、七文件摘要、新 PID/instance 与 `/healthz` 均通过。
- **安装/发布**：Windows 回滚目录 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.28-20260730T140929489Z-42d9ce75`；源码 `909205c` 已部署为 Reader 0.2.75，事务目录 `/home/bwicarus/deploy-backups/reader/20260730T141619Z-228416`。
- **部署后/下一步**：Pi HEAD 一致、webapp/voice-rt active，公网脚本含目标页视觉代码；iPad 完整刷新并重开 Codex 后实测笔迹识别、天气卡回写与连续对话落库。

## Codex：Windows MCP 正文与绘图按需拆分候选（2026-07-30 JST，未安装部署）
- **改了什么**：`reader_context_snapshot` 改为 assistant-context + JSON 两个纯文本块；新增无参只读 `reader_drawing_image`，只对 ready/stable/非空当前绘图取既有 Reader JPEG。
- **安全语义**：绘图年龄用 PWA 同事件相对间隔 + Windows 接收时钟，不做跨机墙钟相减；file/page/revision/ref 在取图前后都必须一致。
- **怎么验的**：Windows C# Release build 0 warning/0 error；无音频 self-test 通过且 `audioActivated=false`，覆盖普通正文不取图、pending 前门禁和取图后 revision 门禁。
- **没做/下一步**：未改 PWA 图像实现，未安装、部署或发布；下一位精确复审本提交后再决定候选打包与上线。

## Codex：快照 MCP 迁移旧语音上下文分流（2026-07-31 JST）
- **改了什么**：模型输出收敛为单一有序 Markdown，补选区所在上下文、显式焦点、EPUB 视口、高亮/卡片与自然语言近况；原始 JSON 仅留 Windows 内部。
- **笔迹语义**：笔迹段直接给无参看图入口，pending 时工具有界等待 PWA 稳定合成；成功看图后常驻进程记住当前笔迹，未变化旧图不再抢占模糊指代。
- **怎么验的**：Windows C# Release build 与无音频 self-test 通过；0.1.30 包 verify/self-test 及安装后真实 HTTP MCP 调用确认仅一段 Markdown、无 raw schema/revision。
- **安装/下一步**：已原子替换 Windows 七文件，回滚目录 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.30-20260730T154723418Z`；未改 Pi/PWA、未启动语音或采音，重开 Codex 后做四种真机问法。

## Codex：PWA 主动发布稳定页图并统一上下文接力（2026-07-31 JST）
- **改了什么**：停笔约两秒后 PWA 主动合成并经既有认证 WSS 发布当前页、笔迹和可见叠加层；Windows 按真实卷、页和笔迹版本缓存，换页/新笔/清空立即废止旧图。
- **上下文语义**：本地网页、Markdown 与 MCP 复用同一有序自然语言投影；选区从 `page.context` 直接折叠，内部 revision/ref/latestEvent 不再展示。
- **怎么验的**：Reader Node 全量合同、C# build/无音频 self-test、候选包 self-test 与部署预检通过；公网三份脚本均确认新发布标记。
- **安装/发布**：Windows 0.1.31 已原子安装，回滚目录 `C:\Users\bwica\bw-computer-voice-bridge-backups\install-0.1.31-20260730T163634875Z`；Reader 事务 `20260730T164027Z-246192` 完成，服务健康。
- **没做/下一步**：未启动语音、采音或发快捷键；用户完整刷新 iPad 后画一笔、停两秒，再检查本地页图、选区与模糊指代看图。

## Codex：0.2.76 独立电脑客户端按钮发布（2026-07-31 JST）
- **改了什么**：原麦克风位置改为独立电脑图标，普通电话只走豆包/GPT/Grok；桥状态、上下文同步和旧注入回退集中到新的“电脑客户端”设置标签。
- **可靠性**：普通电话切电脑时保留同一次 iOS 可信手势与 `play-and-record`，电脑切普通电话时先立即释放本地麦克风、PCM、AudioContext 与路由，再异步收 Windows STOP。
- **怎么验的**：专项合同 84 项和 Reader Node 全量通过，vendor/语法/网络门禁通过；全仓 Python/preflight 仅停在已登记的 Windows `fcntl`/Pi 环境基线。
- **验收/边界**：用户已确认电脑按钮、普通电话隔离、状态镜像及新设置页可用；agent 未点按钮、未申请麦克风、未 START/快捷键。
- **发布**：提交 `df27b0d` 已推送并部署；Reader 事务 `20260730T174111Z-257183` 完成，E2E 与服务健康检查全过，自动回滚快照已生成。
- **下一步**：用户在 iPad 完整刷新后继续实测；以后共享 UI 默认直接到生产 iPad 验收，不再插入独立扩展测试。

## Codex：电脑按钮改由 BWReader App 原生独占（2026-07-31 JST）
- **改了什么**：电脑图标在 App 内只向 Swift 发送一次 `toggle`；网页媒体/WSS 启动链被旁路，普通电话继续只负责豆包、GPT 或 Grok。
- **边界**：普通 Safari/PWA 的电脑按钮明确禁用；上下文后台同步保留，Swift 原生语音仍复用固定 Tailnet 直连。
- **怎么验的**：Reader Node 全量合同通过，生成的扩展 vendor 与真源同批更新。
- **没做/下一步**：未触发麦克风、采音或快捷键；部署后由用户直接在 iPad App 实测。

## Codex：电脑客户端 Codex / GPT Classic 目标选择发布（2026-08-01 JST）
- **改了什么**：电脑客户端页新增“语音与文字接力目标”；START 将固定 `appKind` 贯穿 Swift、WSS、Windows 应用/语音控制与旧版 typist。
- **上下文边界**：App 原生通话的 Reader `context` / `active-reading` 复用同一 WSS；快照 MCP 与旧版文字注入仍互斥，GPT Classic 文字接力须显式开启旧版模式。
- **怎么验的**：Reader 全量合同通过；Windows 0.1.38 保留 283 项完整自检、绘图/MCP、`/6` A/B 总线与 F24 broker，包校验及桌面 96 项测试通过，均未激活音频。
- **安装**：已备份并替换 native/typist 六项，清理 6 个占用旧 EXE 的 MCP 子进程后恢复监听；新 PID 35844 为 `idle`、`captureActive=false`，Swift 已直接写入 iCloud 工程。
- **发布/下一步**：提交至 `82519c3`，Reader 0.2.76 事务 `20260801T142225Z-343375` 的 E2E 与服务健康检查全过；未启动应用、发快捷键或采音，用户重开 iPad App 后分别实测两个目标。

## Codex：iOS 阅读器、原生语音与 Safari 扩展合并（2026-08-03 JST）
- **改了什么**：新增固定 XcodeGen 双 target 工程；`space.bwicarus.bwreader2` 容器承载最新版七个 Swift，内嵌 `space.bwicarus.bwreader2.Extension`，解决两个同 bundle App 互相覆盖。
- **构建合同**：Safari 仍由 `package_safari.py` 派生；App 为 iPad-only，扩展按 Apple 规则为 universal，归档逐项校验双 bundle、版本、handler、签名与扩展包根目录全部资源。
- **怎么验的**：Reader Node 合同全过；Windows Python/handoff 只保留既有 `fcntl` 与无关 fixture 基线；macOS build-only 完成模拟器编译、签名 archive、IPA 导出和资源逐字节校验。
- **发布**：`0.2.3 (2)` 已由同一分支工作流成功上传 TestFlight；旧 Swift Playgrounds 包冻结为本地回退参考，后续 iOS 发布源码以 `ios/BWReader/` 为准。
- **下一步**：等待 App Store Connect 处理完成后，用户在 iPad 安装并验收阅读器、电脑语音按钮及 Safari 扩展是否同时存在可用。

## Codex：Realtime 对话历史自回声去重（2026-08-03 JST）
- **改了什么**：本地已渲染的语音轮次在 `/api/assistant/log` 广播前登记 `turn_id`，当前页面不再把自己的 `assistant-history` 事件追加第二遍；其他设备的真实新轮次仍实时到达。
- **怎么验的**：新增广播前登记顺序合同并通过，Reader Node 全量合同通过；Windows 全文件 Python 仍只有已登记的 `fcntl` 环境基线。
- **发布**：App 与扩展版本线切到 `1.0.1`；远程 Reader 与 TestFlight 同批发布。
- **没做/下一步**：未主动启动语音或发送测试消息；用户刷新现有页面并在 iPad 实测连续两轮 Realtime 对话。

## Codex：多设备覆盖式上下文同步（2026-08-05 JST）
- **改了什么**：App、Safari 扩展与网页各自按本机“上下文同步”开关建立独立 `/reader-context/v1`；只在前台上报，Windows 按有效事件到达顺序覆盖同一快照。
- **语音边界**：snapshot-mcp 下的上下文连接不申请或抢占语音所有权，App 原生语音启停不再拆除它；legacy-inject 保留原生 voice WSS→typist 路径，Windows 已验收的 START 流程未改。
- **怎么验的**：共享/vendor 构建、JS 语法、专项合同与 Safari 打包通过；全量门禁仅保留该分支既有 popup 合同漂移及 Windows 缺 `fcntl` 基线。
- **没做/下一步**：未主动启动语音、采音或快捷键；发布后由用户在多台设备分别开启同步，切换前台页面确认快照最后写入覆盖且语音不断。

## Codex：App 缓存兼容与网页快照可靠接力（2026-08-05 JST）
- **改了什么**：App 精确兼容旧缓存的一字段 Codex 启动消息；网页快照先落扩展本地存储，通话页启动或恢复后读取，且只在 Windows 接受后推进去重标记。
- **怎么验的**：Swift/扩展专项合同、两份 JS 语法与 Safari iOS 打包通过；Reader 全量合同仅保留既有 popup harness 漂移。
- **没做/下一步**：未改 Windows 语音启动、音频路由或快捷键；TestFlight 更新后实测 App 启动及网页正文覆盖快照。

## Codex：Safari 网页快照存储接力兼容修复（2026-08-05 JST）
- **改了什么**：网页采集改走既有 `__bwExtensionStore` 网关读取同步开关并写入最新页；该快照键纳入后台白名单，页面恢复前台时重新读取设置后立即上报。
- **怎么验的**：JS 语法、电脑语音/上下文专项合同与 Safari 打包通过，包内已核对为新接力代码。
- **边界/下一步**：未改 Windows 0.1.86、语音 START、路由或 PCM；上传 TestFlight 后由用户确认普通网页可覆盖 PDF 快照。

## Codex：iOS 原生阅读能力五项整合（2026-08-05 JST）
- **改了什么**：App 新增 PencilKit 标注与 Pencil 手势设置、VisionKit 当前视口 OCR、iOS 18 系统翻译、App Intents/Spotlight，以及小/中号快速入口 Widget。
- **集成边界**：不接管 Windows 桥接、Safari 上下文或书籍墨迹合同；Widget 保留共享快照读取代码，但当前发布签名先不启用 Widget App Group，未共享时显示打开阅读器入口。
- **怎么验的**：macOS CI 完成三 target Swift 编译、固定 profile 签名、归档/包内校验和 IPA 导出；1.0.66(1) 因 Siri 描述含保留品牌词被拒，修正后的 1.0.67(1) 已上传且 App Store Connect API 返回 `processingState=VALID`（run 31004348587）。
- **下一步**：实机验收 Pencil 双击/挤压、OCR 质量、系统翻译语言包、快捷指令、Spotlight 与 Widget；Portal 一次性关联 App Group 后可恢复 Widget 最近阅读内容。

## Codex：App 原生 PencilKit 墨迹接入与公式批处理（2026-08-05 JST）
- **改了什么**：App 内 PDF/EPUB 由 PencilKit 实时采笔，映射回既有墨迹数组、持久化、绘图 revision 与合成图；外部 PWA 仍保留原 Canvas 画笔。
- **可靠性**：每笔带文档代际与幂等 operation ID，保存失败可重试而不重复落笔；未确认写入时阻止切书，并保留网页画笔作为布局未就绪时的 fallback。
- **公式**：设备端普通文字识别不变；公式先复用现有 DocLayout 公式框与 AI LaTeX 批处理，Core ML 版将逐页读取已下载书籍的本地页图，不重复下载整本书。
- **怎么验的**：JS 语法、专项墨迹故障重放与全量 Reader 合同完成；全量仅保留既有 extension-popup harness 漂移，Swift 类型检查交由 macOS CI。
- **没做/下一步**：未改 Windows 桥接；发布 1.0.68 后实机验收 PDF/EPUB 落笔、橡皮擦、AI 看合成笔迹及公式框批处理。

## Codex / Claude：Safari 紧凑通话页设置读取兼容（2026-08-05 JST）
- **改了什么**：`call.js` 同时兼容 callback 与 Promise storage；读取失败保持 unknown 并重试，错误经 frame 通道显示，不再静默当成用户关闭同步。
- **怎么验的**：JS 语法与上下文专项合同通过；统一 TestFlight 产物由 Codex 集成分支生成，Claude 不再单独出包。
- **边界/下一步**：Windows 0.1.90 与 App 逻辑未改；用户更新后开启同步并切普通网页，确认快照变为 `kind=web`。

## Codex：iOS 1.0.69 单一制品整合（2026-08-05 JST）
- **改了什么**：将 App 原生五项、PencilKit/公式接入与 Safari storage 兼容修复合入同一 TestFlight 分支，App、扩展、Widget 版本统一递增。
- **怎么验/下一步**：专项合同、JS 语法与 Safari 打包通过后上传；Windows 0.1.90 未改，用户只需验普通网页约两秒内覆盖为 `kind=web`。

## Codex / Claude：Safari 上下文同步门禁诊断（2026-08-05 JST）
- **改了什么**：紧凑通话页把偏好原始值、同步门状态与 context WSS 状态经既有 frame 通道显示；只在状态变化时报告，不改变同步决策。
- **怎么验/下一步**：JS 语法、上下文专项合同与归档校验通过后上传 1.0.70；用户回报三行实机状态后再修唯一阻断点，Windows 0.1.90 未改。

## Codex / Claude：Safari 网页上下文同页直投（2026-08-06 JST）
- **改了什么**：网页正文由 content script 直接投给同页 shadow 树中的通话 frame，再复用既有 ContextLink 写入 `/reader-context/v1`；不再依赖易被回收的 background、模式预查或偏好门。
- **边界/下一步**：仅可见前台页上报，runtime 消息保留为无 frame 兜底；专项合同与归档通过后上传 1.0.71，Windows 0.1.90 与原生 App 未改。

## Codex / Claude：Safari 网页快照一次性 POST（2026-08-06 JST）
- **改了什么**：同页 frame 将每次网页快照直接 POST 到 Windows `/reader-context/snapshot`，移除该主路径的 WSS 会话、握手与重连依赖；正文事件与 active-reading 同请求嵌套提交。
- **权限/验证**：Safari host 权限只新增具名 Windows Tailnet 主机并由精确合同锁定；桥 0.1.92 与 serve 路由已实测 204 覆盖快照，1.0.75 上传后验普通网页约两秒变为 `kind=web`。

## Codex / Claude：Safari POST 上游可见诊断（2026-08-06 JST）
- **改了什么**：临时诊断直接写普通网页 DOM，显示脚本加载、偏好、上报入口、shadow/frame 查找与投递结果；不再依赖正在被测的内嵌 frame，重复状态自动去重。
- **边界/下一步**：纯诊断、不改投递决策；1.0.76 实机回报左下角内容后立即撤除，Windows 桥保持 0.1.94。

## Codex / Claude：Safari 未知偏好不再阻断网页上报（2026-08-06 JST）
- **改了什么**：只有明确读取到关闭状态才停止网页快照；偏好尚未知、store 不可用或读取失败不再等同于用户关闭同步，可见页继续尝试一次性 POST。
- **怎么验/下一步**：专项合同锁定未知与关闭的语义差异；1.0.77 保留一版可见诊断，实机确认采集、找框、POST 与桥日志闭合后立即撤除。

## Codex / Claude：Safari 上报直接读取真实同步开关（2026-08-06 JST）
- **改了什么**：content script 优先调用同页 `RC.ctxSync.enabled()`，不再把另一套 `chrome.storage` 中从未写入的键当成真实开关；RC 缺失时才走旧存储回退。
- **怎么验/下一步**：合同锁定运行时开关优先于存储回退；1.0.78 保留诊断，实机同时确认 enabled、采集、投递和桥端 origin-ok 后撤除。

## Codex / Claude：Safari 同页直投解除 storage 前置（2026-08-06 JST）
- **改了什么**：采集后的快照先直接投给同页 frame，再异步写兼容缓存；storage 卡住或失败不再阻断投递与诊断，旧 runtime 消息仍只是无 frame 兜底。
- **怎么验/下一步**：合同锁定 `deliverToFrame` 必须早于 storage 写入；1.0.79 实机确认投递成功与桥端 origin-ok 后撤除临时诊断。

## Codex / Claude：上下文开关改为扩展级跨站镜像（2026-08-06 JST）
- **改了什么**：明确存在的站点开关值镜像到扩展存储；普通网站缺少本地键时读取镜像，不再把缺失值误当成关闭或覆盖已开启状态。
- **怎么验/下一步**：合同锁定只有原始值 `1/0` 才能改写镜像，且旧偏好仅在镜像为空时回退读取；1.0.81 保留可见诊断，先开一次 Reader 再切普通网页确认 POST 与 `kind=web`。

## Codex / Claude：Safari iframe POST 段可见诊断（2026-08-06 JST）
- **改了什么**：通话 frame 将收到页面、可见性/去重门、POST 开始及成功失败逐步回传同页探针；宿主仅接受实际嵌入 frame 的报告，避免网页伪造诊断。
- **怎么验/下一步**：专项合同与归档通过后上传 1.0.82；实机据最后一行区分未收消息、门禁跳过、fetch 挂起或 HTTP 失败，Windows 0.1.94 未改。

# 2026-08-06 iOS 可选 Obsidian 本地笔记（TestFlight 候选制作中）

- App 新增设备级 Vault 授权；App 与 Safari 扩展共享 `notes.create/status/list/read`，关闭时仍走 Pi。
- bookmark 仅由 App 持有；扩展写 App Group outbox 并立即更新共享投影，App 存活后幂等落盘。
- 本地开启后的错误不静默回落 Pi，避免重复写；Xcode 云构建、TestFlight 与文件提供器实测待完成。
- 未迁移书内便签/高亮/锚点或 sync-v3；下一步统一构建 1.0.72 并实机验收。

## Codex：App 原生 PencilKit 样式闭环（2026-08-06 JST）
- **改了什么**：阅读页每笔冻结并提交用户所选颜色/粗细，不再从 PencilKit 首采样点反读成红色/4；“标注当前视口”增加可见颜色、粗细、画笔和橡皮工具栏。
- **怎么验**：专项合同锁定 Swift 发送所选样式及两套原生画布不再硬编码 `systemRed/4`；Xcode Cloud 负责真 Swift 编译与归档。
- **没做什么/下一步**：未动 Windows 语音与桥接；合入网页正文提取和统一诊断后发布 1.0.84，用户实机验证蓝色与两种明显不同粗细。

## Codex / Claude：App 书页墨迹收敛为单一 PencilKit 所有者（2026-08-06 JST）
- **改了什么**：原生标志存在时，PDF/EPUB 旧网页层不再接书页 Pencil；便签仍保留独立网页笔迹，PWA fallback 不变；同时补回扩展正文提取的三个运行时函数。
- **怎么验**：PencilKit 专项合同通过；Reader 全量仅保留既有 popup fixture 漂移；Pi 原子预检、部署 E2E 与 iOS 签名归档全部通过。
- **发布**：Reader 事务 `20260806T055818Z-614633` 成功；统一 App/扩展 `1.0.85 (1)` 已上传 TestFlight。
- **边界/下一步**：未改 Windows 桥；用户重新打开书页，验证至少两种颜色与两种明显粗细，若完全不落笔再单独检查原生 layout 命中。

## Codex / Claude：Safari 前台网页与正文提取收敛（2026-08-06 JST）
- **改了什么**：正文改为逐个可见文本节点读取，排除隐藏祖先且不重复父子块；任何强制刷新都不能让失焦标签覆盖前台，签名去重原因可诊断，探针默认关闭。
- **怎么验/发布**：专项合同和 Safari 发布管线通过；全量 Reader 合同仅剩既有 4 个 popup fixture 失败；统一 `1.0.86 (1)` 已完成签名、归档校验并上传 TestFlight。
- **边界/下一步**：Windows 桥保持 `0.1.95`；用户在普通网页切换标签，确认快照保持当前焦点页且正文无隐藏菜单或重复段落。

## Codex / Claude：Safari 正文与实时位置分流（2026-08-06 JST）
- **改了什么**：正文变化时 POST `event + active`，未变时仅 POST `active`，避免占用重复正文并保留 Windows 稳定页文本；前台 60 秒心跳重申当前位置，后台焦点门不放宽。
- **怎么验/发布**：专项合同与 Safari 发布管线通过；统一 `1.0.87 (1)` 完成签名、归档校验与 TestFlight 上传。
- **边界/下一步**：App Reader 已有前台恢复和 60 秒 active-reading 心跳，未重复改动；用户验证跨标签/跨设备覆盖与正文持续可读。

## Codex：PencilKit 未接管时恢复网页墨迹回退（2026-08-06 JST）
- **改了什么**：PDF/EPUB 不再仅凭原生能力标志禁用网页画笔；PencilKit 未就绪或 hit-test 放行时由网页层继续绘制，并同步当前颜色、粗细与橡皮工具。
- **怎么验**：PencilKit 与电脑语音专项合同 28/28、JS 语法、diff 门禁及 Safari 发布管线通过；全量仅剩既有 popup fixture 与 Windows `fcntl` 环境失败。
- **边界/下一步**：Windows `0.1.95` 语音链路未动；发布 `1.0.88 (1)` 后实机验证多颜色、两种粗细与橡皮。

## Codex：网页响应式改宽不再清除笔迹（2026-08-06 JST）
- **改了什么**：普通网页宽度变化保留已提交笔迹；若恰在落笔，只取消未完成笔画并回滚在途橡皮，不再清空整层。
- **怎么验**：真实 Chromium 合同覆盖宽度 `1000→900` 后路径、颜色和粗细逐项不变；Reader Node 全量、发布管线、JS/Python 语法与 diff 门禁通过。
- **发布/边界**：统一 App/扩展 `1.0.90 (1)` 已签名并上传 TestFlight；Windows 语音启动链未改，全量 Python 仍仅有既有 Windows `fcntl` 基线失败。
- **下一步**：用户更新后验证网页改宽笔迹仍在；按需视觉工具另行复用现有局部/整页合成并接 Safari `captureVisibleTab`。

## Codex：跨 App/真书/Safari 闭合选区笔（2026-08-06 JST）
- **改了什么**：三端统一 `region` 元素、时间+动态序号、闭合填充与清晰边界；工具框跟随 Pencil 悬停或最后落笔，设置新增触屏双击的橡皮/选区/关闭三态。
- **可靠性**：选区数量不设专用上限，单条路径限 512 点；App 触屏双击以绝对工具状态回传 Swift，避免 Web/PencilKit 状态漂移。
- **怎么验**：真实 Chromium 手写回归、Reader 全量合同、JS/Python 语法与发布管线；Swift 编译交由统一 macOS CI。
- **边界**：Windows 语音链路未改；AI 按需获取选区附近/笔迹附近/当前视口合成图仍是下一阶段，不在文本快照中连续传图。
- **下一步**：上传统一 TestFlight 后实机验收 Pencil hover、双击三态、多个选区编号与 PDF/EPUB 持久化。

## Codex：按需视觉上下文与网页受限控制（2026-08-06 JST）
- **改了什么**：App/PWA/Safari 统一提供当前视口、笔迹附近与闭合选区附近的按需合成图；网页另提供全文语料、视口前后文和当前段标记。
- **工具边界**：浏览器只开放上下视口、滚到文字/标题/当前选区五种固定动作；选区 ID 只能取自当前快照索引，未知或过期 ID 拒绝。
- **可靠性**：全文按 Codex thread 首读一次，位置与正文分离；多设备同 URL 以 sourceInstanceId 隔离，重复 JSON 键与跨源混合均 fail closed；闭合选区序号持久且删除旧选区不重排。
- **怎么验**：Windows 编译/直连自检、Reader Node 全量、Python 门禁与发布管线；真实 iPad/Pencil/Safari 行为待统一 1.0.92 实机验收。
- **边界**：未改已验收的 Windows 语音 START/STOP/F24；快照只索引最近 128 个选区，更早选区暂不可按 ID 寻址。

## Codex：电脑客户端设置独立控制 Codex 语音（2026-08-06 JST）
- **改了什么**：设置页读取 Windows 的 Codex 语音状态，并以独立目标状态按钮开启或关闭；刷新只读，合成点击不能触发快捷键。
- **怎么验/边界**：电脑语音合同与 Reader 合同通过；本轮视觉/手写发布保留该功能，但没有改已验收的 Windows START/STOP/F24 链路。

## Codex：跨端视觉上下文统一发布（2026-08-07 JST）
- **改了什么**：按需合成图、稳定闭合选区、跟随式手写工具框、网页全文/视口上下文与受限浏览器控制已合入生产分支；iOS 工作流改用递增 run number 生成构建号。
- **怎么验**：Reader 合同 712/712、Windows 直连自检、发布管线与独立阻断复核通过；Pi 原子部署的 HTTP/WSS/E2E 与摘要门禁全部通过。
- **发布/边界**：Reader 已部署，统一 App/扩展 `1.0.92 (2)` 由 Actions run `31122152431` 成功上传 TestFlight；未重装或改动已验收的 Windows 语音链路。
- **下一步**：实机验收 Pencil 颜色/粗细/双击三态、多个稳定选区、改宽保留笔迹，以及 Safari 全文+视口和两项按需视觉工具。

## Codex：原生 Pencil 控件与设置链修复（2026-08-08 JST）
- **改了什么**：浮标改为独立持久位置并支持手指拖动/四角预设；工具面板仅在展开瞬间采样最近笔尖位置，展开后冻结；选区笔独占 Pencil 手势并显示置顶青色闭合轮廓。
- **便签/设置**：原生工具、颜色和粗细同步到便签；触屏双击三态进入原生设置；App 的 Codex 语音状态不再借用仅上下文连接，Windows START/STOP/F24 未改。
- **怎么验**：Reader 合同 713/713、专项 40/40、发布管线 24/24、diff 门禁及 macOS 模拟器/设备归档全部通过；Windows 全仓 Python 仅保留既有 Linux/环境基线失败。
- **发布**：统一 App/扩展 `1.0.93` 已由 Actions run `31241289112` 成功上传 TestFlight；共享 Reader 脚本按生产部署流程更新。
- **下一步**：实机验证浮标拖动不误开、面板冻结、Pencil 闭合选区、触屏双击三态、便签多色/多粗细/橡皮及 Codex 语音独立按钮。

## Codex：本机文件夹与 Pi 书库第一阶段（2026-08-08 JST）
- **改了什么**：App 可授权并递归索引本机 PDF/EPUB，按“本机/Pi/全部”浏览；Pi 提供认证目录、Range 下载和按内容幂等上传，纯本地书采用“上传并打开”复用同一 Reader。
- **一致性/安全**：下载不覆盖、上传目录固定、EPUB 防压缩炸弹、2 GiB 双层上限；持久同步关系区分本机更新/Pi 更新/冲突，同内容本机副本仍逐文件显示。
- **怎么验**：Reader 合同 716/716；书库/API/部署清单专项 34/34；diff 门禁通过；Windows handoff 仅被既有 Linux `fcntl` 环境基线阻断。
- **没做什么/下一步**：未提交、推送、部署或上传 TestFlight；macOS 编译与真机文件提供器验收后再发布，完全离线 EPUB/PDF 壳留到第二阶段。
## Codex：原生 App 两集合 Pi 显式同步闭环（2026-08-08 JST）
- **改了什么**：设置与词汇状态复用 sync-v3；Swift 私有持有账户 namespace/owner lease，页面只传公开变化与持久 checkpoint；设置区新增固定 Pi 登录 sheet。
- **怎么验的**：真实本机设置已从 PreferenceStore 进入 `user-settings` 并出现在 sync push；原生同步专项与 53 项 relay/打包/清单测试通过；Node 全量当前仅被并行本地壳 CSP 正则误报阻断，已交本地运行时负责人收口。
- **边界**：阅读进度、高亮、笔迹、便签与卡片仍明确 pending，未开放不稳定身份；未构建 Xcode/TestFlight，离线 ReaderBundle 打包因本机无 pinned archive cache 未执行。
- **下一步**：macOS 构建后实机用“登录或重新登录 Pi”建立登录态，再点“与 Pi 同步”验证两集合往返与冲突不覆盖；不得把 native owner 凭据下放页面。

## Codex：iOS App 本地优先离线 Reader 候选（2026-08-08 JST）
- **改了什么**：App 内置可复现 ReaderBundle，以固定 loopback 和不透明本机书 ID 直接打开 Files 文件夹中的 PDF/EPUB；默认进入本机书架，不再把上传 Pi 或远程 PWA 当阅读前置。
- **数据与同步**：App 本机数据库是默认真源；设置中的“与 Pi 同步”只显式处理书籍、`user-settings` 与 `vocabulary-state`，其余尚无稳定身份的文档域继续 fail closed。
- **安全边界**：壳、脚本与字体受 manifest 摘要和每次启动 token/nonce 约束；EPUB 实际解压字节有界，书籍/AI HTML 经 DOMPurify，本地首版不注入不受信书内 CSS。
- **怎么验的**：离线包两次生成逐文件一致，Reader 全量、书库/API/同步/打包与发布管线门禁通过；Windows handoff 只保留既有 Linux `fcntl` 环境基线。
- **发布边界**：必须先由 macOS CI 编译 Swift，再由 iPad 实机验收本地 PDF/EPUB、重开持久化、Pi 显式同步与 Safari 扩展不回归；提交、推送与上传结果另行追加，不从本地测试推断。

## Codex：本地优先离线 Reader 1.1.0 发布候选（2026-08-08 JST）
- **发布**：统一 App/扩展 `1.1.0 (132)` 由 Actions run `31253611580` 从提交 `73d3d1c8` 成功编译、签名并上传 TestFlight。
- **怎么验的**：确定性 ReaderBundle、macOS 模拟器编译、设备归档、App/扩展一致性校验、IPA 导出与 Apple 上传均通过；首轮暴露的两处 Swift 编译错误已局部修复。
- **没做什么**：未部署 Pi 书库/API 生产改动，未把尚无稳定身份的阅读进度、高亮、笔迹、便签或卡片伪装成已同步。
- **下一步**：等待 Apple 处理后实机验收本机 PDF/EPUB、重开持久化、无 Pi 阅读、Pi 登录/显式同步与 Safari 扩展回归。

## Codex：Pi 原始书库与原生同步生产部署（2026-08-08 JST）
- **改了什么**：补齐三个书库路由的 GLOBAL vbook 策略，并将目录、Range 下载、幂等上传及 native sync relay 按清单原子部署到 Pi。
- **怎么验的**：首次预检准确拦截漏声明；局部合同修复后完整预检、摘要门禁、生产安装、服务健康探针和 Reader E2E 全部通过。
- **生产状态**：`webapp`/`voice-rt` active，新书库路由与模块已落地，部署后错误日志为 0；回滚事务位于脚本输出的 deploy-backups 目录。
- **下一步**：App `1.1.0 (132)` 刷新 Pi 书库并验收目录、下载、上传及设置中的显式同步；本机离线阅读不依赖此服务。

## Codex：手动 Pi 书籍预处理服务端候选（2026-08-08 JST）
- **改了什么**：新增仅凭认证 `bookId + contentSha256` 启动的 PDF OCR 任务，支持页边界暂停/继续/取消/重试，原 PDF 永不覆盖；文字、分词、公式阶段与进度分开报告。
- **附件**：page-chars 与公式以 `reader-book-attachments/1`、`derived + immutable` 版本清单导出，下载只走具名 attachment ID、摘要和 revision 白名单。
- **怎么验的**：OCR/书库/API/附件/部署清单专项 43 项与 Reader 合同 758 项通过；部署清单确认 coordinator/worker 原子同行。
- **边界/下一步**：未碰 Swift/Reader JS，未提交、推送或部署；Windows 全量门禁仍被既有 `fcntl`、Linux 路径/命令与环境型基线阻断，下一位先做 macOS 客户端接线与 Pi 预检。

## Codex：旧笔迹与用户页账户隔离候选（2026-08-08 JST）
- **改了什么**：PDF/EPUB 笔迹和用户页统一由 verified `ReaderStorageIdentity` 读写、锁定、改名及导出；缺身份直接拒绝，不再访问全局旧目录。
- **迁移安全**：既有 owner claim 以固定三目录做增量只读备份与 copy-only 激活，最后写扩展标记；目标内容不同或来源竞态均 fail closed，旧文件不删不改。
- **怎么验的**：Sidecar 恢复/冲突、三 API 双账户隔离、exporter 拆分普通笔迹与闭合区、书库包专项及 diff 门禁通过；Windows 无提权符号链接用例按环境跳过。
- **没做什么/下一步**：未提交、推送或部署；Pi 预检前应核对生产旧 claim owner 与三目录清单，部署后由同一已验证 owner 首次访问完成扩展认领并检查 manifest/备份。

## Codex：本地 PDF 文字层、手动预处理与随书附件 1.1.1 候选（2026-08-08 JST）
- **改了什么**：本地 PDF 自带文字层立即提供选中/复制/搜索，不等待 OCR；无层页面可由用户显式启动 Apple 或 Pi 预处理，并分别显示文字、分词和公式进度。
- **附件与身份**：下载书时并行获取不可变 OCR 附件和账户级用户状态；整书 SHA、附件 revision、当前 Pi 账户及本机事务 revision 均需一致，冲突不覆盖且重复大附件不重下。
- **怎么验的**：Reader 合同全量通过，书库/OCR/账户迁移聚焦 Python 通过，离线 ReaderBundle 重建并验证；独立终审未发现新增 P0/P1。
- **发布/下一步**：候选已提交并推送；macOS CI 对提交 `72b20045` 完成模拟器编译、签名归档、三目标校验及 TestFlight 上传，版本 `1.1.1 (134)`。Pi 的新 OCR/用户状态路由尚未部署，先用 iPad 验收自带文字层与本机 Apple 预处理，再按原子流程上线服务端部分。

## Codex：本地 Reader 假更新提醒修复 1.1.2（2026-08-09 JST）
- **改了什么**：App 本地 Reader 不再运行只适用于在线 PWA 的服务器界面版本探针；线上页面的真实资源更新提示保持不变。
- **怎么验的**：本地壳专项、Reader 全量合同、离线 ReaderBundle 重建与完整校验通过；macOS CI 完成模拟器编译、签名归档及三目标校验。
- **发布/下一步**：提交 `07c70bfa` 已上传 TestFlight `1.1.2 (135)`；安装后确认本地 PDF 停留超过 15 秒及切回前台均不再显示假更新条。

## Codex：原生功能等价与本地最短路径候选（2026-08-09 JST）
- **改了什么**：以功能等价而非旧端口复刻为准，补齐本机 PDF/EPUB 助手上下文、搜索、裁边、页文字/叠层、目录、位置、改页事务、OCR/公式迁移及启动恢复；共享 UI 继续复用。
- **直连边界**：书体、文字、几何、批注和本地变更直接进入 App；Pi 只接收确需远端的翻译文本、AI/词典/卡片或已核验远端书身份，同摘要的不同本机书互不锁死。
- **验证与边界**：Reader 全量合同、网络审计、相关 Python 回归、发布管线及离线包复验通过；Windows 完整门禁仍被既有 `fcntl`/平台与旧合同漂移拦截，macOS Swift 编译和真机未验。
- **没做/下一步**：未提交、推送、部署或发布；先在 macOS/iPad 验收，再决定发布，此前登记的手写、语音常驻、图像上下文、浏览器工具及其余设置需求仍在后续队列。

## Codex：原生功能等价与本地最短路径 1.1.3 发布（2026-08-09 JST）
- **App**：提交 `5a32e150` 经 Actions run `31299665803` 完成模拟器编译、签名设备归档、App/扩展/Widget 校验及 Apple 上传；TestFlight 为 `1.1.3 (138)`。
- **Pi**：提交 `f5782ea8` 经原子流程部署为 Reader `0.2.76`，`webapp`/`voice-rt` active，E2E 与部署后合同通过；回滚快照见该次部署事务输出。
- **预检修复**：首次准确拦截两条 OCR adoption 路由漏登记并补为 GLOBAL；macOS 又拦截缺失 no-redirect delegate 与 Swift `min/max` 遮蔽，均局部修复后重跑通过。
- **边界**：Windows 语音后台源码随集成提交保存但未在本轮重装；TestFlight 处理完成后仍需 iPad 验收本机 PDF/EPUB、Pi 显式同步、OCR/公式、改页持久化与原有手写/语音/快照功能。
- **下一步**：以实机结果继续修正，不再为同一发布额外拆临时候选流程。

## Codex：Reader P0 文字选中、恢复与公式诊断发布（2026-08-09 JST）
- **改了什么**：坏字符不再抹掉整页文字层；Apple Vision 字框插值升级并让旧缓存按版本重算；书架链接按真实导航来源接管，前后台恢复同时覆盖 WebKit 内容进程终止。
- **公式门禁**：Pi worker 合并捕获检测器输出，只有唯一目标 sidecar 在本次运行中有效更新才报成功；零匹配、`ERROR`、陈旧或未更新结果全部显式失败。
- **怎么验的**：Reader Node 全量、OCR/书库 Python 专项、确定性离线 ReaderBundle、发布管线与差异检查通过；Windows handoff 仅保留既有 Linux `fcntl` 环境阻断。
- **发布**：提交 `d684a123` 经 Actions run `31306098989` 上传 TestFlight `1.1.3 (139)`；Pi 同提交经原子流程部署，回滚快照为 `20260809T101453Z-868860`，服务健康与 E2E 通过。
- **边界/下一步**：真机选字、书架返回和后台恢复仍需 iPad 验收；本轮旧 worker 的 LaTeX 结果为 partial 0/1109，新诊断只对部署后重试或新任务生效。

## Codex：文字层选择、持久高亮与 PDF 缩放 1.1.4 发布（2026-08-09 JST）
- **改了什么**：Pi/PC/Apple/原文文字层按不可变附件分层保存并可在书架显式切换；修复 PDF/EPUB 高亮的 WebKit IndexedDB 事务、EPUB 空图片、书名误开外部浏览器及整层切换未生效。
- **性能/方向**：PDF 缩放只重绘视口邻页，捏合预览只变换焦点页；iOS 后续以 PDFKit 承担基础渲染、缩放和原生选择，既有 DocumentHost 与共享笔迹/卡片/AI 叠层继续保留。
- **怎么验的**：Reader Node 全量 932 项、OCR/书库/PC worker Python 专项、离线 ReaderBundle、macOS 模拟器/设备归档与 Pi 完整门禁均通过；Windows handoff 仅被既有 Linux `fcntl` 环境阻断。
- **发布**：App 提交 `de58710c` 经 Actions run `31310652255` 上传 TestFlight `1.1.4 (140)`；Pi 最终提交 `5e52250d` 原子部署成功，回滚快照为 `20260809T113625Z-895412`；PC worker 已在线并接受任务。
- **边界/下一步**：仍需 iPad 验收文字层切换、高亮、EPUB、返回书架和缩放；随后分阶段以 PDFKit 替换基础 PDF 页面，并实现名为“ReaderPC 服务器”的统一托盘总控。

## Codex：PDF 高亮删除与 EPUB 滚动性能 1.1.5 发布（2026-08-09 JST）
- **改了什么**：App 补齐 WKWebView 的系统提示、确认和输入对话框，PDF 高亮删除可在原确认语义下继续；无原生展示器时删除仍 fail closed。
- **性能**：EPUB 停滚装饰从遍历全部历史已加载章节改为只量取当前视口邻域；没有可用装饰数据时不再创建延迟任务。
- **怎么验的**：新增对话框与 1000 章有界扫描行为合同，Reader Node 全量 935 项、macOS 模拟器/设备归档和 Pi 原子门禁通过；Windows 仅保留既有 Unix `fcntl` 环境阻断。
- **发布**：提交 `a40d30b0` 经 Actions run `31312326401` 上传 TestFlight `1.1.5 (141)`；Pi 部署 Reader `0.2.76`，回滚快照为 `20260809T120442Z-905559`。
- **边界/下一步**：PDFKit 基础渲染/缩放仍未实现，下一阶段单独替换 PDF 页面底座并保留 DocumentHost 与共享叠层；ReaderPC 托盘候选继续隔离。

## Codex：PDF 高亮单步删除 1.1.6 发布（2026-08-09 JST）
- **改了什么**：PDF 高亮编辑浮层里的“删除”改为最终动作，不再额外弹系统确认框；WKWebView 的通用提示、输入与其他确认能力保留。
- **怎么验的**：合同直接提取 `_hlDelete`，断言没有 `confirm()` 且仍执行 DELETE、内存投影更新与浮层关闭；Reader 生成物继续由部署和本地包脚本从 `reader.src` 构建。
- **发布**：提交 `8c71cc35` 经 Actions run `31314015846` 完成模拟器编译、签名归档、三目标校验并上传 TestFlight `1.1.6 (142)`；Pi 原子部署 Reader `0.2.76`，回滚快照为 `20260809T124455Z-915901`。
- **边界/下一步**：本次只修重复确认，不改高亮数据结构；已在隔离分支进入 PDFKit 原生页面底座阶段。

## Codex：本机 PDFKit 按页渲染 1.1.7 发布（2026-08-09 JST）
- **改了什么**：App 本机 PDF 改由设备端 PDFKit 按当前页/邻页渲染有界 JPEG，不再让 PDF.js 解析整本；24 张/96 MiB 内存缓存与浏览器不可变缓存共同复用页图。
- **兼容边界**：Web DocumentHost 仍负责文字层、高亮、笔迹、卡片、AI 与快照，页坐标合同不变；EPUB 和线上/Pi 阅读器行为不变。
- **怎么验的**：Reader 合同 937 项、离线 ReaderBundle 构建/复验、Pi handoff READY；macOS 完成模拟器编译、签名设备归档与三目标校验。全仓 Python 的电脑语音运行态测试另有既存环境失败，与本次文件无交集。
- **发布**：提交 `82a3f8ac` 经 Actions run `31314908879` 上传 TestFlight `1.1.7 (143)`；Pi 只快进源码并验证，未重启或部署生产服务。
- **下一步**：iPad 验收打开速度、连续滚动、捏合与叠层对齐；下一小阶段再评估 PDFView 原生手势接管，不与 ReaderPC 托盘总控混改。

## Codex：PDFKit 快路径恢复文字选中 1.1.8 发布（2026-08-09 JST）
- **根因/修复**：1.1.7 跳过 PDF.js 后也跳过了它登记内嵌字符层的副作用；现由当前书的 PDFKit processor 按需读取可见页字符并按书缓存，既有 Pi/PC/Apple 显式文字层仍优先。
- **边界**：本次只恢复 PDF 选中，不启动 Vision OCR、不切换用户选择的文字层，也不回退整本 PDF.js 解析；1.1.7 的打开速度路径保留。
- **怎么验的**：针对合同 103 项、Reader 全量 938 项、离线 ReaderBundle 与 macOS 模拟器/签名真机归档、App/扩展/Widget 校验均通过；Pi handoff READY。
- **发布**：提交 `45ab1f99` 经 Actions run `31317974166` 获 Apple `UPLOAD SUCCEEDED`，TestFlight 为 `1.1.8 (144)`；Pi 仅安全快进源码，未部署或重启生产服务，下一步由 iPad 验收真实拖选与高亮。

## Codex：TestFlight 1.1.8 可见性核验（2026-08-10 JST）
- **改了什么**：发布工作流新增独立只读状态模式，按版本/构建号查询 upload、processing、beta readiness、测试组与匿名状态计数；不会构建、上传或改分发。
- **实况**：Actions run `31320948949` 确认 `1.1.8 (144)` 为 `COMPLETE / VALID / IN_BETA_TESTING`，无错误/警告且无需出口合规；内部组 `me` 对全部构建开放，唯一测试者状态为 `INSTALLED`。
- **结论**：Apple 服务端已可测试，不需重复上传或补测试组；用户在 TestFlight 强制刷新/重开即可，仍不显示再核对设备 Apple ID 与 TestFlight 页面。
- **边界/下一步**：未读取测试者身份字段、未修改 App Store Connect；后续上传应以状态检查而非仅 `UPLOAD SUCCEEDED` 作为可见性证据。

## Codex：PDFKit 选择层身份竞态 1.1.9 发布（2026-08-10 JST）
- **根因/改动**：1.1.8 只覆盖已有完整摘要；普通本机 PDF 首次页文字请求会先得到 idle 且不再重试。现复用已验证 Pi 摘要；其余书在首屏完成后后台建立摘要，并让所有已请求页重建字符层。
- **性能边界**：不回退 PDF.js、不在打开前整本哈希；PDFKit 页图快路径保留，只有缺少身份时后台一次性补齐。
- **怎么验的**：新增真实 idle→whole update→ready 页字符合同；Reader Node 全量 940 项、ReaderBundle、macOS 模拟器/签名归档及三目标校验通过；Windows Python 仍为既有 `fcntl`/环境基线。
- **发布**：提交 `996408ae` 经 Actions run `31323387138` 上传 TestFlight `1.1.9 (148)`；状态 run `31323673268` 确认 `VALID / IN_BETA_TESTING`，无处理错误或警告。
- **下一步**：用户安装 1.1.9 后在同一本 PDF 拖选并创建/删除一次高亮；真机通过前不宣称交互验收完成。

## Claude/Codex：PDF 字符桥信任拒绝诊断 1.1.10（2026-08-10 JST）
- **设备证据**：1.1.9 每页均报 `BW_NATIVE_PAGE_TEXT_UNTRUSTED`；文字层在 App→网页信任门被整体拒绝，并非 OCR 质量或字符重试问题。
- **改了什么**：六项信任检查逐项返回安全错误后缀，只报告 scheme/host/port/path 类别与长度，不记录 capability token，也不放宽任何权限。
- **怎么验的**：Reader Node 全量 940 项、发布管线、macOS 模拟器编译、签名设备归档及 App/扩展/Widget 校验通过；Windows handoff 仅保留既有 `fcntl` 平台基线。
- **发布**：Claude 提交 `e89492a9`，候选 `d13295e2` 经 Actions run `31330789791` 上传 TestFlight `1.1.10 (151)`；状态 run `31331119662` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **下一步**：用户开启“显示调试日志”并翻页，回传带具体后缀的 `chars unavailable` 行；Claude 按实际失败项修根因。

## Claude/Codex：PDF 字符桥信任路径修复 1.1.11（2026-08-10 JST）
- **根因/改动**：`URL.path` 会剥掉能力基址尾斜杠，旧分支因此退化为路径完全相等并拒绝全部页面；现统一为“基址本身或以基址加 `/` 开头”，诊断与判定共用同一函数。
- **安全边界**：相似但不同的令牌路径仍拒绝；未放宽 scheme/host/port、主框或 WebView 身份检查，PDFKit 快速页图路径不变。
- **怎么验的**：新增五类路径边界合同；专项 15 项、Reader 全量 941 项、发布管线、macOS 模拟器编译、签名设备归档及三目标校验通过。
- **发布**：Claude 根因提交 `a1d3be5a`，候选 `8678dd67` 经 Actions run `31331437787` 上传 TestFlight `1.1.11 (154)`；状态 run `31331820423` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **下一步**：用户在同一本本机 PDF 验收拖选、创建高亮和删除高亮；真机通过前不宣称交互验收完成。

## Codex：换书时 WebKit IndexedDB 事务失活修复 1.1.12（2026-08-10 JST）
- **根因/改动**：换书导航压力下 WebKit 会在 IDB 成功回调与 Promise 续步之间提前提交 readwrite transaction，后续读取因此报“without an in-progress transaction”；现只在 WebKit 写事务存续期用无副作用缺失键读取维持活跃，完成即停止。
- **性能边界/验证**：不回退 PDF.js、不增加整本下载或扫描；严格 WebKit 生命周期模拟、浏览器 IndexedDB 合同、Reader Node 全量 942 项、ReaderBundle、macOS 模拟器/签名归档及三目标校验通过；Windows Python 仍只有既有平台/仓库基线。
- **发布**：提交 `560c73be` 经 Actions run `31332479072` 上传 TestFlight `1.1.12 (157)`，状态 run `31332957737` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **Pi**：原子部署完成，E2E 全过；回滚与取证快照 `/home/bwicarus/deploy-backups/reader/20260809T195429Z-952395`。
- **下一步**：用户不重启 App 连续切换多本 PDF/EPUB，确认不再长时间停在“打开 PDF”或出现 `BW_LOCAL_STORE_UNAVAILABLE`。

## Codex：本机图片模式换书启动门修复 1.1.13（2026-08-10 JST）
- **根因/改动**：干净 PDF 没有改页 journal 时，启动恢复仍会对整本文件重算 SHA-256，并把 `book-meta` 与 PDFKit 页图挡在后面；现先做常数时间 journal 探测，只有真实恢复或网页携带摘要证据时才读整本。
- **可见性**：现有调试框补齐 IndexedDB、PDF 恢复与运行时 ready 三段启动读数；无事务响应可明确不给内容摘要，不伪造文件身份。
- **怎么验的**：专项与 Reader 全量合同、离线 ReaderBundle、发布管线、macOS 模拟器编译、签名设备归档及 App/扩展/Widget 一致性校验通过；Windows handoff 仅保留既有 `fcntl` 平台基线。
- **发布**：提交 `a5ac3d2f` 经 Actions run `31345268820` 上传 TestFlight `1.1.13 (160)`；状态 run `31345617187` 确认 `COMPLETE / VALID / IN_BETA_TESTING`，无处理错误或警告。
- **下一步**：用户不重启 App 连续切换两本大 PDF；正常日志应依次出现“IndexedDB 已就绪 / PDF 恢复检查已完成 / 运行时已就绪”，随后立即显示页图。

## Codex：本地书显示与批注存储解耦 1.1.14（2026-08-10 JST）
- **根因/改动**：每次换书都先对文档库和设备库做写入/读回/删除体检，且 `book-meta` 与裁边读取共用该启动门；现删除重复体检，原生元数据、页图、字符层走只读快路径，裁边及其余状态随后接入。
- **边界**：IndexedDB 仍是高亮、笔迹、位置、便签等本地状态真源；本次只解除它对首屏的阻塞，不回退 PDF.js、不重复保存已本地化 PDF，也不放宽能力 URL 或接口清单。
- **怎么验的**：存储启动被故意挂起时 337 页元数据仍立即返回；Reader 全量 945 项、离线 ReaderBundle 311 文件、发布管线、macOS 模拟器/签名归档及 App/扩展/Widget 校验通过；Windows 仅既有 `fcntl` 基线。
- **发布**：提交 `28a6eee3` 经 Actions run `31346818906` 上传 TestFlight `1.1.14 (163)`；状态 run `31347190443` 确认 `COMPLETE / VALID / IN_BETA_TESTING`，零错误、零警告。
- **下一步**：用户安装后不重启 App 连续切换两本已本地化 PDF，确认页图先显示、文字可选，随后既有高亮与笔迹正常恢复。

## Codex：PC 文字层缓存自修复 1.1.15（2026-08-10 JST）
- **证据/根因**：PC 与 Pi 不可变附件均有 391 页结果（368 页非空、共 285750 字，截图第 6 页为 841 字）；App 的 v1 导入回执只核元数据且把 PC 错标为 Pi，坏的本地层会被永久复用，故显示 `0 char`。
- **改动**：PC 独立标为 `pc`；回执升级并保存逐页字数，以首/中/末/最密页有界抽样校验缓存；旧 PC 回执自动重导一次，“重新导入结果”改为真正强制下载，调试行显示 `[pc/pc-vision/1]`。
- **怎么验的**：专项 42 项、Reader Node 全量 949 项、相关 Python 74 项（1 跳过）、离线 ReaderBundle、发布管线、macOS 模拟器/签名归档与三目标校验通过；Windows 全量 Python 仅保留既有 `fcntl`/平台基线。
- **发布**：提交 `8ef1580a` 经 Actions run `31355330394` 上传 TestFlight `1.1.15 (167)`；状态 run `31355787793` 确认 `COMPLETE / VALID / IN_BETA_TESTING`，零处理错误或警告。
- **下一步**：用户安装后可直接打开触发旧 PC 回执的一次性修复；若仍为零，在书库点“重新导入结果”并选“PC 高质量预处理”，无需重新运行 PC 识别。

## Codex：App 本地阅读器 Realtime 2.1 恢复 1.1.16（2026-08-10 JST）
- **根因/改动**：本机书页把共享 `/voice-rt` 错连到 `127.0.0.1`，且 CSP 未放行真实中继；现仅向可信本地主框暴露固定生产 WSS 路由并只准麦克风采集，其他媒体类型、子框与路径继续拒绝。
- **扩展/服务**：扩展原有后台代理、账户围栏与严格 `/voice-rt` 路由保持不变；生产 WSS 以 App 的 loopback Origin 实测返回 `engine=openai / model=gpt-realtime-2.1-mini`，Pi 无需改动或重启。
- **怎么验的**：Realtime 专项 80 项、Reader Node 全量 949 项、相关 Python 60 项（1 跳过）、离线 ReaderBundle、扩展发布管线及 macOS 模拟器/签名设备归档与三目标校验通过；Windows handoff 仅既有 `fcntl` 平台阻断。
- **发布**：提交 `9b410dbe` 经 Actions run `31361125845` 上传 TestFlight `1.1.16 (169)`；状态 run `31361659716` 确认 `COMPLETE / VALID / IN_BETA_TESTING`，零处理错误或警告。
- **下一步**：iPad 分别在 App 书页与 Safari 网页点普通电话按钮验收采集、回复和挂断；电脑按钮是独立 Windows 语音功能，不作为本次 Realtime 结果。

## Codex：普通电话直连 Realtime 0.2.79 / 1.1.17 发布（2026-08-10 JST）
- **改了什么**：App 本机书与扩展普通电话以 Pi 签发的 90 秒临时凭据直连 OpenAI；长期 key/预算留在服务端，Pi 控制侧链继续承载页面、选区、笔迹、合成图、工具和重连补投。
- **怎么验的**：上下文/直连专项 25 项、Reader Node 全量 954 项、凭据 Python 3 项、扩展发布管线及完整真实浏览器矩阵通过；原生 ReaderBundle、模拟器、签名归档与三目标校验通过。
- **发布事实**：Pi Reader `0.2.79`、KG `kg-0.2.79-f36633bede5407c4d33c`，回滚目录 `20260810T074728Z-996878`；Windows 测试渠道 `0.2.79`，渠道回滚目录 `webext-channel-20260810T075211Z-4z_b5c0e`。
- **App 状态**：Actions run `31367641743` 上传 TestFlight `1.1.17 (172)`；只读状态 run `31368231881` 确认 `COMPLETE / VALID / IN_BETA_TESTING`，已进入内部组且无处理错误或警告。
- **没做/下一步**：未替用户主动开启麦克风或发起真实通话；安装 1.1.17 后分别验收普通电话的双向音频、当前页、选区与笔迹问答，电脑/Codex 语音按钮不属于本次链路。

## Codex：Realtime 选区与合成图注入恢复 0.2.80 / 1.1.18（2026-08-10 JST）
- **根因/改动**：直连 call 用短期 `ek_` 创建，长期项目 key 加入同一 sideband 实测 404；现控制链与挂断复用同一短期身份，凭据只在认证 WSS/HTTPS 消息体内存传递。
- **图像语义**：`see_ink` / `see_page` / `see_figure` 始终把真实合成图注入当前 Realtime 会话，不再只报告“有笔迹”，并从 HTTP 工具结果移除重复的数 MB base64。
- **怎么验的**：真实 API 同 call 对照、相关 Node/Python 合同、Pi 原子门禁/E2E、扩展真实浏览器矩阵及 macOS 编译签名归档均通过；Windows handoff 仅保留既有 `fcntl` 平台基线。
- **发布**：提交 `d513663f`；Pi Reader `0.2.80` 回滚目录 `20260810T101330Z-1016692`；扩展渠道回滚目录 `webext-channel-20260810T101643Z-35icjqy7`；Actions `31378457024` 上传 TestFlight `1.1.18 (174)`，状态 run `31379157070` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **边界/下一步**：未替用户发起真实语音；安装后在选中文字与画笔迹两种场景分别提问，确认模型读到选区并能描述实际合成图；完全脱离 Pi 的本地 Realtime 架构另行设计。

## Codex：App 本地优先与 Pi 可选 AI/API 化 0.2.83 / 1.1.21（2026-08-10 JST）
- **改了什么**：OpenAI 项目 key 只由 App 写入 Apple 共享 Keychain，App/扩展原生进程直接签发短期 Realtime key；页面、选区、笔迹/页图、笔记与书库启动均本地运行，Pi 只保留显式备份/同步及固定 AI/CLI 工具白名单。
- **发布阻断修复**：可选 Windows 快照 403/离线现在进入统一诊断与重试，不再从 Promise 成功回调逃成页面异常；没有放宽 Windows Origin、Tailscale 身份或扩展权限。
- **怎么验的**：Reader Node 全量 967 项、发布管线 24 项、ReaderBundle 311 文件、Pi Linux 全门禁/E2E及扩展 10 组真实 Chromium 矩阵均通过；macOS 模拟器、签名设备归档和三目标校验通过。
- **发布事实**：提交 `6b39bc55`；Pi Reader `0.2.83`、KG `kg-0.2.83-40c19fad934e83316576`，回滚目录 `20260810T140328Z-1059235`；扩展测试渠道 `0.2.83`，渠道备份 `webext-channel-20260810T140819Z-8dzikqzf`。
- **App/下一步**：Actions `31396551594` 上传 TestFlight `1.1.21 (178)`，状态 run `31397435944` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告；未代用户录入长期 key、发起通话或执行 Pi AI 工具，安装后先在 App 设置录入既有 key，再断开 Pi 验收阅读、选区、笔迹、笔记与普通电话。

## Codex：App 本机 Realtime 启动修复已发布 0.2.84 / 1.1.22（2026-08-11 JST）
- **根因/改动**：旧“保存成功”只证明 Keychain 写入且启动错误写入隐藏状态；现保存后真实签发临时凭证验证，失败按麦克风/凭证/建连阶段 toast 与调试留痕。
- **建连边界**：App 的 SDP 提交与 `Location` 读取下沉到签名原生桥，消除未覆盖的 WKWebView 跨域差异；扩展仍用原生短期凭证与受限后台直连，不恢复 Pi 前置。
- **安全/清理**：麦克风只放行同一可信本地主框；远端 call 建立后的无效响应会主动挂断，长期 key 仍不进入网页、日志或构建物。
- **怎么验/发布的**：相关合同 26 项、Reader Node 全量 967 项、Pi/Linux 全门禁与 E2E、扩展 10 组真实 Chromium、macOS 模拟器/签名归档/IPA 校验均通过；代码提交 `cc4c4e9b`，文档头 `9790e128`。
- **发布事实/下一步**：Reader `0.2.84`、KG `kg-0.2.84-94c13b438c3ce5cd5b73`，回滚目录 `20260810T152528Z-1076012`；扩展通道 `0.2.84`，备份 `webext-channel-20260810T153424Z-r3ku32e5`；Actions `31403498155` 上传 TestFlight `1.1.22 (181)`，`31404381682` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。安装后替换同一 key 完成联通验证，再点普通电话；若仍失败，界面会直接给出准确阶段与错误。

## Codex：App Realtime retention_ratio 精度修复已发布 1.1.23（2026-08-11 JST）
- **设备证据/根因**：1.1.22 已走到 OpenAI，但服务端明确拒绝 17 位小数的 `session.truncation.retention_ratio`；Swift `Double(0.8)` 在 Darwin JSON 中被展开为 `0.80000000000000004`。
- **改了什么**：保留官方语义 0.8，改用精确 `NSDecimalNumber(8×10^-1)`；请求发出前对排序后的 JSON 强制断言字面量恰为 `0.8`，否则本机失败并显示精度错误。
- **验证/发布**：Reader Node 全量 967 项及 macOS 模拟器、签名设备归档、三目标/IPA 校验均通过；提交 `02e50ef0`，Actions `31408803077` 上传 TestFlight `1.1.23 (183)`，`31409766301` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。未改 Key、模型、工具、麦克风或 Pi，也未重复部署 Reader/扩展通道。

## Codex：App Realtime 合成图工具修复已发布 1.1.24（2026-08-11 JST）
- **根因/改动**：App 用 90 秒 `ek_` 创建 call，却在每次 `see_page/see_ink` 时才用它新建 sideband，过期后必然失败；现 App 原生以 Keychain project key + 官方 multipart `sdp/session` 创建 call，并让图像 sideband 与 hangup 始终复用同一身份域。
- **安全边界**：网页只拿进程内随机 capability；call/key 绑定登记有 12 小时 TTL、8 条上限、精确 Location 路径和错误 capability fail-closed，项目 key 不进入 JS、日志或构建物。
- **验证/发布**：专项 6 项、Reader Node 全量 968 项、macOS 模拟器、签名归档、三目标与 IPA 校验均通过；提交 `416bf0ab`，Actions `31411761841` 上传 TestFlight `1.1.24 (186)`，`31412715670` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **未做/下一步**：未改 Reader/扩展通道或 Pi；Safari 扩展仍是既有 ephemeral 建连路径，不能据此宣称扩展同故障已修。安装后在 App 普通电话中依次验收 `see_page`、圈画后的 `see_ink`、挂断再拨后再次看图。

## Codex：本机笔迹合成图格式与本地缓存已发布 0.2.85 / 1.1.25（2026-08-11 JST）
- **根因/改动**：App-owned 笔迹使用 `pts`，合成图与旧裁图只读网页 `p`，本机笔迹因此被当成空；现统一消费两种字段，App/扩展合成图先写有界 App Group 缓存再由原生 API 直送当前 Realtime 会话。
- **怎么验**：`pts`/`p` 同几何裁剪与 Pi 兼容裁图专项、Reader Node 全量 969 项、发布管线、Pi 全门禁/E2E、扩展真实浏览器矩阵及 macOS 模拟器/签名归档均通过。
- **发布事实**：代码 `efc656db`、文档 `ead64fc3`；Reader `0.2.85`、KG `kg-0.2.85-534da6ae7f800dac0b02`，回滚目录 `20260810T180043Z-1107624`；扩展渠道备份 `webext-channel-20260810T180656Z-9q58kdva`；Actions `31417454942` 上传 TestFlight `1.1.25 (189)`，`31418113085` 确认 `VALID / IN_BETA_TESTING` 且零错误警告。
- **边界/下一步**：本机 Realtime 图像路径不经 Pi；未代用户发起真实通话，Windows 快照在线状态与本次普通电话 `see_ink` 修复分开验收。安装后在 App 与 Safari 各画一笔并调用 `see_ink`，确认工具卡成功且模型能描述真实笔迹。

## Claude / Codex：see_ink 阶段化失败已发布 0.2.86 / 1.1.26（2026-08-11 JST）
- **改了什么**：Claude 提交 `c074957c` 将页面合成、call 身份、sideband、本地保存与传输逐阶段作答；原生前置条件不再把凭证、媒体类型、编码或存储问题统一误报为图像过大，Codex 以 `d44b7e91` 补版本、vendor 与合同。
- **怎么验**：专项 13 项、Reader Node 全量 970 项、发布管线、ReaderBundle、Pi Linux 全门禁/E2E、扩展真实浏览器矩阵及 macOS 模拟器/签名归档均通过；Windows handoff 仅保留既有 `fcntl` 平台基线。
- **发布事实**：Reader `0.2.86`、KG `kg-0.2.86-59eb78f835eb67daec41`，回滚目录 `20260810T185623Z-1127105`；扩展渠道备份 `webext-channel-20260810T190123Z-zlnko09f`；Actions `31422033498` 上传 TestFlight `1.1.26 (192)`，`31422736822` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **边界/下一步**：本批只改善可见诊断，未声称修好原生覆盖层是否进入合成图；安装后在 App 与 Safari 各画一次并调用 `see_ink`，把完整阶段提示或 AI 实际描述交回 Claude，据此修真实断点。

## Claude / Codex：see_ink 路由与失败详情已发布 0.2.87 / 1.1.27（2026-08-11 JST）
- **根因/改动**：1.1.26 的失败详情其实被语音轮次卡丢弃，不能据“没有额外内容”推断走了 Pi；现失败也持久化完整结果，并报告 route/stage/call/sideband 与三个无凭据原生桥状态，非本机视觉路由直接拒绝。
- **核对/验证**：App 原生 handler 先于 document-start 脚本注册，成功的本机建连只会返回 `native_direct=true`；专项合同、Reader Node 全量 971 项、Pi Linux 原子预检/114 项/E2E、macOS 模拟器/签名归档与 IPA 校验通过。
- **发布事实**：代码 `522aee6e` + `fc6be004`；Reader `0.2.87`、KG `kg-0.2.87-321d5a230216b642f0ac`，事务 `/home/bwicarus/deploy-backups/reader/20260811T012606Z-1156381`；Actions `31449978738` 上传 TestFlight `1.1.27 (195)`，`31450447983` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **边界/下一步**：Windows 扩展 channel 因独立的网页便签刷新回归超时而保持 `0.2.86`，无半发布；TestFlight 已含 Safari 扩展。用户复现后展开 `see_ink(失败)` 流程并提交完整 JSON，再按真实 route/stage 修合成或传输。

## Claude / Codex：see_ink 可见步骤与有界等待已发布 0.2.88 / 1.1.28（2026-08-11 JST）
- **改了什么**：Claude 将 route、合成三条路径、图像大小、原生接收和最终结果逐步写入既有 `dlog`，并为裁图/视口、整页合成、原生请求设置 8/10/15 秒上界；Codex 修正“失败也报已接受”和 bridge 误判并补动态超时合同。
- **怎么验**：Reader Node 全量 972 项、发布管线、Pi 原子预检/114 项/E2E、macOS 模拟器/签名归档与 IPA 校验均通过；未改合成算法、信任路径或 `p/pts` 兼容。
- **发布事实**：代码 `85d4166a` + `0422d27c`；Reader `0.2.88`、KG `kg-0.2.88-600cb0ca5a6558b56e17`，事务 `/home/bwicarus/deploy-backups/reader/20260811T040740Z-1185177`；Actions `31457579351` 上传 TestFlight `1.1.28 (198)`，`31458055710` 确认 `VALID / IN_BETA_TESTING`。
- **边界/下一步**：本批消除无限“处理中”并暴露真实断点，不等于已修未知根因；安装后开“显示调试日志”，复现 `see_ink` 并提交从 `tool→` 到最后一行的左下角截图。

## Claude / Codex：App 原生合成图已发布 0.2.89 / 1.1.29（2026-08-11 JST）
- **改了什么**：屏内 `see_page/see_ink` 通过 WKWebView/PencilKit 公共父视图原生截取底页、笔迹与可见卡片；滚出视口的 PDF 笔迹改由 PDFKit 离屏渲页并叠加本机权威墨迹。
- **接口/安全**：共享前端已接 `scope=viewport/region/page`，空笔迹仍返回有效图；能力前缀、同书校验、JPEG 上限与 `X-BW-Reader-Error` 保持 fail closed，令牌不入日志。
- **验证/发布**：Reader 合同全量 979 项与发布管线通过；提交 `c7f0cfc4`，Actions `31466443968` 上传 TestFlight `1.1.29 (205)`，`31466889652` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **未做/下一步**：本轮 App 前端随 IPA 构建，未部署 Pi；用户安装后分别验收屏内笔迹（含卡片）、翻页后的屏外 PDF 笔迹（无卡片层）及失败日志的具体错误码。

## Claude / Codex：App 原生合成图直投已发布 0.2.90 / 1.1.30（2026-08-11 JST）
- **根因/改动**：1.1.29 已生成原生合成图，却仍把约 800KB JPEG 经 HTTP/base64/JS/WKWebView 桥送回 Swift 后才进 Realtime；现 `scope=viewport/region/page&deliver=realtime` 在原生层完成合成、本地保存与 Realtime 注入，网页只收小型完成收据。
- **兼容/安全**：直投 POST 只允许精确能力前缀路由、可信同书主框和三个视觉工具，正文只含 call 与进程内 capability 且不入日志；Safari/PWA 与非直投调用保留二进制 GET、网页合成和旧桥回退。
- **验证/发布**：专项 24 项、Reader Node 全量 981 项与发布管线通过；Windows Python 门禁仍仅有既有 Linux `fcntl`/机器环境基线，macOS 模拟器、签名归档、三目标与 IPA 校验通过；提交 `4019287f`，Actions `31468766372` 上传 TestFlight `1.1.30 (210)`，`31469324720` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **未做/下一步**：未部署 Pi 或浏览器正式渠道；安装后在 App 普通电话分别调用 `see_ink` 与 `see_page`，调试日志应出现“原生直投完成”，不再停在“图已就绪…送往原生通道”。

## Codex：修复原生 Realtime 图像回执并发布 0.2.91 / 1.1.31（2026-08-11 JST）
- **根因/改动**：1.1.30 实际收到当前 GA 的 `conversation.item.added/done`，Swift 却只等待旧 `created`，成功图像被误报为确认超时；现发送独立 `event_id/item.id`，按本次图片精确接受新旧回执并关联错误。
- **验证/发布**：专项与 Reader Node 全量、发布管线通过；Windows Python 仍只有既有 `fcntl` 基线。提交 `113883ec`，Actions `31472374708` 上传 TestFlight `1.1.31 (213)`，`31472948994` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **未做/下一步**：未部署 Pi 或浏览器正式渠道；用户更新后在 App 普通电话调用 `see_ink/see_page`，应出现“原生直投完成”且 Realtime 能实际描述合成图。

## Codex：修复 Realtime 图像条目 ID 超长并发布 0.2.92 / 1.1.32（2026-08-11 JST）
- **根因/改动**：原生直投把 14 字符前缀与 32 位 UUID 拼成 46 字符 `item.id`，超过 OpenAI 的 32 字符上限；现将 `event_id/item.id` 都固定为 4 字符类型前缀加 28 位随机值。
- **验证/发布**：专项 18 项、Reader Node 全量 981 项与发布管线通过；Windows 全量 Python/handoff 仍为既有平台与 fixture 基线。提交 `e7e58735`，Actions `31473624439` 上传 TestFlight `1.1.32 (216)`，`31474158807` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **未做/下一步**：未部署 Pi 或浏览器正式渠道；用户更新后在 App 普通电话调用 `see_ink/see_page`，应不再出现 `string_above_max_length`，并由模型实际描述合成图。

## Codex：Realtime 新笔迹按页单答已发布 0.2.93 / 1.1.33（2026-08-11 JST）
- **改了什么**：按通话、按页记录笔迹基线/fresh/seen；当前页新笔迹只调用一次 `see_ink`，别页状态保留，首次旧批注不冒充新笔迹；用户插话会取消旧回答并按回传 item ID 删除旧图。
- **怎么验**：专项 17 项、Reader Node 全量 982 项、发布管线、macOS 模拟器/签名归档/IPA 校验通过；Windows handoff 仅剩既有 `fcntl`/Linux fixture 基线。
- **发布事实**：提交 `ceeb68f8`；Actions `31479675109` 上传 TestFlight `1.1.33 (219)`，`31480548173` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **未做/下一步**：未部署 Pi 或浏览器正式渠道；下一步复用 App 本机合成图缓存，为 PC 实时快照提供短期引用和按需 GET，不持续推送大图。

## Codex：App 合成图已开放给 PC 实时快照 MCP（2026-08-11 JST）
- **根因/改动**：App→Windows 的原生取图/WSS/视觉 RPC 已存在，真正阻塞是 Codex 全局 `enabled_tools` 只允许文字快照且保留旧工具名；现本机开放 `reader_visual_image`，并更新 `reader-live-context` 为一次只选最窄视觉 scope。
- **怎么验**：已装 Windows direct `0.1.99` 的 MCP `tools/list` 返回快照/视觉/浏览控制三工具；`codex mcp get reader_snapshot` 确认视觉白名单生效，离线探针按合同返回 `visual-source-not-ready` 而非未知工具。
- **边界**：图片仅在 AI 调用时由精确在线 source 生成并分块回传，不进入文字快照、不暴露 App 路径或 capability、也不经过 Pi；当前支持视口、笔迹附近和闭合选区附近。
- **下一步**：新 Codex 会话刷新工具发现后，在 App 前台开启实时快照做真机图像验收；无需重发 1.1.33，也不修复旧主动推图缓存。

## Codex：Realtime 图像与页文字合并已发布 0.2.94 / 1.1.34（2026-08-11 JST）
- **根因/改动**：PDF `read_page` 仍读旧同步字段，原生/PC OCR 文字虽已渲染却返回空；现从页文字 provider 读取前页尾、当前页与后页头，并把冻结的选区、问题和同页合成图放进同一工具结果。
- **并发语义**：每个用户轮次只允许首个视觉工具取图并触发一次回答；新通话重置上下文指纹，换页无文字时不再沿用旧页文本。
- **验证/发布**：专项 15 项、Reader Node 全量 986 项、发布管线、ReaderBundle/Safari 包及 macOS 模拟器/签名归档/IPA 校验通过；提交 `03b3eca4`，Actions `31484292168` 上传 TestFlight `1.1.34 (224)`，`31484944351` 确认 `VALID / IN_BETA_TESTING`。
- **边界/下一步**：未部署 Pi 或浏览器正式渠道；安装后用 App 普通电话询问漫画对白及新圈选内容，预期一次看图、一次回答，并结合文字层与图像而非只描述像素。

## Codex：本机阅读窗口快照与 Codex 语音持续运行已发布 0.2.95 / 1.1.35（2026-08-12 JST）
- **改了什么**：App 本机 PDF/EPUB 持续发布“显示区域之前 / 当前显示区域（重点）/ 显示区域之后”的 `page.context`；App 与 Safari 扩展设置新增 Windows Codex 语音持续运行开关，调试浮层不再遮挡设置关闭控件。
- **Windows**：ReaderPC `0.1.9` 与原生桥 `0.1.102` 已原子安装；桌面快捷方式指向 0.1.9，真实 F24 关闭后状态从 inactive 自动恢复 active，修复提交 `ca97e7a8` 已推送。
- **怎么验**：Reader Node 全量 989 项、ReaderBundle 打包/复验、扩展发布管线、ReaderPC 20 项与包验证通过；Windows 全量 Python/handoff 仍只保留既有 `fcntl` 与 fixture 基线，未混入本批修复。
- **发布事实**：App/扩展提交 `2ec30973`；Actions `31518783808` 上传 TestFlight `1.1.35 (227)`，只读状态 `31519443838` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **边界/下一步**：本批 App/Safari 前端随 IPA 发布，未部署 Pi 或浏览器正式渠道；安装后验收本机书籍快照正文、设置中的持续运行开关和调试日志与设置并开时的触摸。

## Codex：可选视觉能力诊断去重已发布 0.2.96 / 1.1.36（2026-08-12 JST）
- **改了什么**：PDF/EPUB 缺少可选 `getVisualSurface` 时只在能力状态首次出现或真正变化时报告一次，不再被实时快照轮询反复刷屏；既有原生 `see_page` / `see_ink` 路径未改。
- **怎么验**：新增能力变化回归，Reader Node 全量 990 项与扩展发布管线通过；Windows handoff/Python 只重现既有 `fcntl`、fixture 与编码基线。
- **发布事实**：提交 `20cae364`；Actions `31543994114` 上传 TestFlight `1.1.36 (230)`，`31544550284` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **边界/下一步**：未部署 Pi、Windows 服务或浏览器正式渠道；安装后开启调试日志停留数秒，预期同一“adapter 未实现”最多一行，能力恢复仍可重新探测。
## Codex：ReaderPC 服务器 0.1.3 已安装（2026-08-10 JST）
- **改了什么**：新增独立托盘总控、统一本机状态、PC OCR 精确 PID 代次启停及版本化原子安装；语音/上下文/OCR 仍为独立子进程。
- **性能**：空闲只用 `nvidia-smi` 探测 GPU；每项重任务完成后 worker 退出并由托盘拉起轻量代次，实测由约 4.2 GB 私有内存回落至约 18 MB。
- **怎么验的**：定向测试、精确 payload/摘要和包内 EXE 自检通过；PC 实际完成一本 391 页 Vision 任务，服务端确认文字 391 页、公式 4/4、392 个不可变附件可下载。
- **安装/回退**：当前 release 为 `%LOCALAPPDATA%\BWReader\ReaderPC-Server\releases\0.1.3`，0.1.2/0.1.1/0.1.0 完整保留；未注册开机项、未替换现有 Windows 语音 0.1.99。
- **下一步**：App 刷新后自动导入附件，用户在该书“当前使用”中选择 PC 高质量文字层，再验收拖选。

## Codex：ReaderPC 服务器 0.1.4 已安装（2026-08-11 JST）
- **根因/改动**：0.1.3 的窗口 X 只隐藏、托盘退出只销毁界面；现最小化仍驻托盘，X 与托盘退出统一先停止 PC OCR 和电脑语音/上下文直连，停止失败则保留窗口报错。
- **入口/安装**：安装器同时原子更新开始菜单与桌面 `ReaderPC 服务器.lnk`；当前 release 为 `%LOCALAPPDATA%\BWReader\ReaderPC-Server\releases\0.1.4`，旧版本完整保留。
- **验证**：关闭/失败/竞态遗留 service record 与双快捷方式专项 15 项通过，候选 verify/包内 self-test 通过；真实 WM_CLOSE 后 ReaderPC/OCR 均为 0、43128 无监听且 service record 不存在，随后已从桌面快捷方式重开 0.1.4。
- **边界**：未按进程名清理 Codex 自有 `--reader-context-mcp`，未部署 Pi、App 或扩展；退出会停本次运行服务，PC 用户偏好保留供下次启动恢复。

## Codex：Codex 语音失效代次恢复已发布 0.2.97 / 1.1.37（2026-08-12 JST）
- **根因/改动**：同一 Codex 失效进程内重复 F24 不会恢复；首轮启动未确认时现精确重启该代次、等待新代次后只再启动一次，二次失败锁住自动恢复；设置页离线不再抹掉持续运行勾选。
- **Windows/验证**：原生桥 `0.1.103` 已原子安装，安装后 294 项自检、Reader Node 990 项、发布管线与 ReaderBundle 通过；服务状态新鲜且无错误，回退目录 `install-0.1.103-20260811T233355362Z`。Windows 全量 Python/handoff 仍仅保留既有 `fcntl`、Unix fixture 与项目基线失败。
- **发布事实**：提交 `ddfbd199`；Actions `31546858430` 上传 TestFlight `1.1.37 (233)`，`31547369807` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **边界/下一步**：未部署 Pi 或浏览器正式渠道；用户安装 1.1.37 后，以真实“Codex 语音启动失败”验收一次自动重启恢复及设置重开仍保留勾选。

## Codex：Codex 语音稳定启动时序已发布 0.2.98 / 1.1.38（2026-08-12 JST）
- **根因/改动**：旧版只等 5 秒且重启就绪后立即再按 F24；现每次只按一次，最长观察 20 秒、浮标出现后再稳定等待 8 秒，失败才精确重启一次、就绪后等 5 秒再单次重试，二次失败锁住守护。
- **Windows/验证**：原生桥 `0.1.104` 已原子安装，随后同源 `0.1.105` 叠加视觉修复且本批四个语音源码摘要未变；当前 295 项自检通过、`lastError=null`，麦克风代次跨观察窗口未变化且持续运行偏好仍开启；回退目录 `install-0.1.105-20260812T005319025Z` 内是完整 0.1.104。
- **发布事实**：提交 `8637aec8`；Actions `31551459703` 上传 TestFlight `1.1.38 (236)`，`31552022269` 确认 `COMPLETE / VALID / IN_BETA_TESTING`。
- **边界**：未部署 Pi 或浏览器正式渠道；Windows 全量 Python/handoff 仍只有既有 `fcntl` 平台阻断，工作树同期出现的 see_ink/快照在制改动未纳入本次提交。

## Codex：按需视觉来源与 PDF 整页图已发布 0.2.98 / 1.1.38（2026-08-12 JST）
- **根因/改动**：本机 `page.context` 没有单独 viewport 包，快照只在 active-reading 保留在线 source，导致 `see_ink` 报“图像来源没准备好”；现同规范页合并 source，且 `see_page` 对 PDF 走 PDFKit 离屏整页合成而非当前视口。
- **验证**：实时快照已确认 activeReading/currentPage 使用同一 `sourceInstanceId`；Windows 295 项自检、Reader Node 990 项与发布管线通过，Windows handoff 只剩既有 `fcntl` 平台基线。
- **发布事实**：提交 `14fb8bbd`；Actions `31552100884` 上传 TestFlight `1.1.38 (239)`，`31552582367` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。
- **边界/下一步**：未部署 Pi 或浏览器正式渠道；安装 build 239 后分别验收 `see_ink` 与 `see_page`，前者应绑定当前 App 页，后者应得到整张逻辑 PDF 页。

## Codex：Reader 本机输出与渐进能力说明已发布（2026-08-12 JST）
- **改了什么**：电脑语音聊天轮次进入最新快照精确指向的 App/扩展既有对话流；同一 WSS 新增严格的卡片、导航、高亮、工具状态输出，MCP 以短索引和八个按需资源说明 GET/命令。
- **边界**：现有 Realtime 与 CLI 调用、工具循环和委托完全不变；陈旧 source/revision/file/page 拒绝投递，Pi PWA 暂停新增且本批未部署 Pi，也未发布桌面浏览器正式渠道。
- **验证/Windows**：Reader Node 992 项、C# 297 项、聊天同步/打包/发布管线及 Safari/ReaderBundle 复验通过；Windows direct `0.1.107` 已原子安装，回退目录 `install-0.1.107-20260812T024527175Z`，服务 `idle / lastError=null`。
- **发布事实**：提交 `176d14d8`；Actions `31558051746` 上传 App `1.1.40 (244)` 与 Safari `0.2.100`，只读检查 `31558497053` 确认 `COMPLETE / VALID / IN_BETA_TESTING` 且零错误警告。

## Codex：ReaderPC 聊天同步常驻入口已接通（2026-08-12 JST）
- **改了什么**：把既有 capture-bound 历史同步器接入 ReaderPC 托盘生命周期；仅显式 `snapshot-mcp` 模式启用，单实例租约防止重复 worker，退出时完成收尾。
- **验证/安装**：相关 Python 39 项、候选摘要与包内自检通过；ReaderPC `0.1.10` 已切换运行，现场确认 worker 租约被托盘持有，电脑语音直连仍在线且未开启音频。
- **边界**：未改 Realtime、CLI、工具调用或语音建连；未部署 Pi PWA、App、Safari 或桌面浏览器渠道，旧 ReaderPC release 仍保留可回退。
- **下一步**：下次真实电脑语音通话后，App/扩展当前会话应出现新增的用户/助手轮次；路由仍由当前快照的 source/revision/file/page 严格限定。

## Codex：快照/语音恢复与 Reader MCP 单文件修复已发布（2026-08-12 JST）
- **改了什么**：App 快照专线保留用户意图并按 1/2/4/8/15 秒重连，电脑语音前后台恢复并重试明确可恢复错误；Windows 快照 revision 改为按 producer 实例仲裁，Direct 健康接口公开独立快照连接状态。
- **渐进能力**：Windows MCP 增加按需能力指南及五工具白名单，保留既有 Realtime/CLI；发布自检现真实启动单文件 stdio MCP 并调用快照与指南，修复发布后缺少 `TypeInfoResolver` 即崩溃。
- **验证/发布**：Reader Node、目标语音/快照合同、C# Release、包管理测试与真实包内/安装后 MCP 前向调用通过；TestFlight `1.1.41 (247)` 为 `VALID / IN_BETA_TESTING`，Windows Direct `0.1.112` 已原子安装并恢复服务。
- **现场边界**：Windows 服务健康但 App 尚未重连（`contextConnected=false`，快照为 stale）；需安装/重开 App 1.1.41 后验收自动回连，当前 Codex 会话的已关闭 MCP transport 需新会话重建。
- **未做**：未部署 Pi PWA 或浏览器正式渠道；交互式论文服务仍仅有能力说明，未配置可调用后端。
