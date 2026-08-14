# Obsidian 笔记管理项目

## Reader / PWA / 浏览器扩展：Codex 快速入口

Reader 主线先读 [Codex Reader Context](references/codex-reader-context.md)，再读
[当前协作状态](references/reader-collaboration-status.md)。这两个短入口不足以裁决时，才按
context 中的索引回看原始长文档。聊天记录、旧版本注释和历史测试不能覆盖当前代码、合同测试与
已登记的发布事实。

### 产品边界

| 页面 | 无扩展 | 有扩展 |
|---|---|---|
| 普通 HTTP/HTTPS 网页 | 无 BW 功能 | 扩展提供完整网页阅读功能 |
| PWA 真书（PDF/EPUB/导入 HTML/Markdown/收藏书） | PWA 完整 fallback | 扩展接管共享 UI、网络与通用数据；PWA 保留 renderer、私有 anchor 和书籍数据 |
| iOS App 本机真书 | App 内置 Reader、App-owned local store 与原生能力 | Safari 扩展不进入 App WKWebView，也不参与所有权 |

不得恢复 PWA 任意网页解析器，也不得把扩展退回只为 PWA 提供数据的 provider。功能有差异或
矛盾时先登记到 [运行时冲突](references/reader-runtime-conflicts.md) 或
[视觉冲突](references/reader-ui-conflicts.md)，不能擅自删掉一边。

iOS App 不内嵌远程 PWA：HTML/JS/CSS renderer 作为本地资源打包，Swift 拥有本机文件、数据、
生命周期与系统能力。App 内禁止启动 Service Worker、PWA install/cache、扩展 TAKEOVER 或
PWA owner lease；Pi 只作用户显式触发的同步、备份与联网 AI 网关，不能成为本机开书前置。

### 源码入口

- 共享视觉与组件：`_server_deploy/static/pdf/rc-ui.js`、`rc-*.js`
- 共享运行时合同：`_server_deploy/static/reader-runtime/*.js`
- 扩展宿主与 adapter：`extensions/bw-reader-webext/src/*.js`
- iOS App 本地宿主：`ios/BWReader/App/*.swift`、`_server_deploy/static/pdf/native-local-runtime.js`
- PWA 真书宿主/接管桥：`_server_deploy/static/reader-runtime/book-host.js`、
  `_server_deploy/static/pdf/pwa-extension-bridge.js`
- 服务端业务与桥接：`_server_deploy/pdf_reader.py`、`assistant.py`、`voice.py`、
  `tool_registry.py`
- 扩展 `vendor/` 是生成物，只能由 `extensions/bw-reader-webext/build.py` 更新，禁止手改。
- PDF `_server_deploy/static/pdf/reader.js` 也是生成物；唯一源码在 `reader.src/*.js`。

卡片职责保持单一：`rc-voicecall` 管视觉外壳、拖放、收藏和长按；`rc-flashcard` 管 Anki
正反面与学习状态；`rc-review` 管复习队列、提交和失败恢复；`rc-stickynote`/`web-pins`
管 placement。学习卡语义身份保持 `id === cid === gid`，placement ID 另算。

### 安全检查与常用命令

开始前：

```bash
git status --short
tail -n 160 references/reader-collaboration-status.md
python3 extensions/bw-reader-webext/handoff_check.py
```

📬 **Claude / Codex 协作统一走 BW AgentBridge Lite（BWAB）**。正式入口是桌面
“多AI协作终端-正式版”，它会给受管会话附加协作能力，不覆盖本文件或其他项目指令。
Codex 用 `$env:BWAB_CLI` 的 `send / inbox / wait / notify`；Claude 用 Channel 提供的
`reply / get_messages / notify_assistant / bridge_status`。**开工前、完工后、需要对方配合时各查一次**。
回复仍用四段式：**改了什么 / 验证了什么 / 没做什么 / 下一步谁做**；消息必须写清目标、范围和
是否只读。若当前会话没有 `BWAB_CLI` 或上述 Channel 工具，说明它不是由 BWAB 启动：
不要回退旧 SQLite 邮箱，改由正式入口重新打开。完整规则见
[`references/agent-collaboration.md`](references/agent-collaboration.md)。

共享源码变化后按范围选最短闭环，不再把整套命令每次全跑：

```bash
python3 extensions/bw-reader-webext/build.py
node --test --test-reporter=spec tests/reader_contract/<改动对应测试>.test.mjs
```

共享核心/协议改动再补全量 Reader Node（它很快）；Python 全量仅在改到 Python 或部署逻辑时跑；
`handoff_check.py` 只在交接/发布边界跑一次，已确认的 Windows `fcntl`/Pi-only 基线不重复追。
`test_release_pipeline.py`、扩展候选与 `release_preflight.py` 只用于扩展正式渠道或用户明确要求的
扩展候选，不再是 Reader/PWA 直接部署的前置步骤。

浏览器行为只有用户明确要求或目标无法在生产 iPad 验收时，才按范围选择
`extensions/bw-reader-webext/test_*.py`。Windows 只使用既有
`BW Codex Chrome Test` 独立环境及 `%LOCALAPPDATA%\BWReaderExtensionTest\browser-profile-v2`；
不得修改日常 Chrome/profile，也不得用在线 channel launcher 覆盖待测本地候选。

### 调试时限与协作

默认先做一次聚焦定位和最小修复；目标合同测试通过但真实行为仍不确定时，直接说明疑点并让用户
在 iPad 一起看，失败就回滚/修理，不为穷尽所有可能性追加无关全量检查或长时间死磕。
其他 AI 只在疑难点、交叉审查或用户明确希望时按需介入，不作为固定门禁。

⚠ Windows 上若运行 `handoff_check.py`，必须 `PYTHONUTF8=1`，否则它会因 GBK 输出崩溃：
它输出 `✓`(U+2713) 时撞 GBK 控制台，抛 `UnicodeEncodeError`——看起来像代码 bug，
其实是环境。终端另需 `chcp 65001`。加上后正常跑完（41s）。

### 部署

**完整流程见 [部署流程](references/deployment-workflow.md)（唯一权威，2026-07-29 收敛）。**
唯一部署机是 Pi。先用清单判断改动属于哪一类，**判据是清单不是目录**：

```bash
python3 scripts/reader_deploy_manifest.py | cut -f1 | grep -F '<你改的文件>'
```

- **命中**（`app.py`/`control.py`/`pdf_reader.py`/KG runtime/systemd unit/7 个阅读器模板…）→
  `bash scripts/deploy_reader.sh --preflight-only` 通过且人工验收通过后再去掉参数正式跑。
  Windows 侧统一入口是 `powershell -File scripts\deploy_from_windows.ps1 -PreflightOnly`
  （必须在 Windows 本机终端跑，经 `ssh→cmd→powershell` 嵌套会引号解析失败并留孤儿进程）。
- **未命中**（`insights.py`/`fitness*.py`/`qa_server.py`/`mcp_server.py`/`templates/control.html`…）→
  Pi 上先手工备份再 `cp` + `systemctl restart webapp`，**没有事务保护**。
- ⚠ `control.py` 在清单内、`control.html` 在清单外，改控制面板两条链都要走。
- ⚠ **不要用 `deploy_reader.sh --pc`**：它会把候选 tar 直接解到 Windows `C:/claude`，
  绕过 git 覆盖工作树，与跨机边界冲突。

**`deploy_reader.sh` 已经保证的事，调用方不要手工重做一遍**：四层摘要校验
（`verify_candidate_digest`/`verify_deploy_payload_digest`/`verify_validation_digest`/
`verify_checkout_inputs_match_candidate`，部署前后各一轮）、原子安装 + 时间戳备份 +
失败自动 `rollback_deploy`、KG 不可变 release 与"未意外写入 KG 状态"断言、写者 timer 冻结、
webapp/voice-rt 健康探针、E2E 冒烟。**脚本失败会自己回滚并报错**，所以证据是"退出 0 +
事务目录路径"，不是手算 SHA、逐字节比对源文件或手列回滚清单的散文。

### 硬边界

- 工作区长期包含用户和历史改动；禁止 `git reset --hard`、`git clean`、宽范围 checkout，
  也不得把无关差异带入当前工作。
- 协作时先读共享状态并认领有界 scope；同一 checkout 不允许两个 agent 同时改同一组件。
- **跨机(Pi + Windows 两份工作副本,2026-07-29 起)**:部署真源只有 Pi。Windows 不得直接改
  Pi 工作树或生产文件，源码只经 git 上游流动；`scripts/deploy_from_windows.ps1` 只做远程
  `merge --ff-only`，**绝不远程切分支**。
- Pi 的检出是共享的(多 agent + 每晚 daily 会重写 `anki/records`、`dashboard.json`)，
  所以"工作树脏"是常态。远程部署前先过 `scripts/deploy_remote_guard.sh`：只有当**本次要拉的
  提交会改到别人正在改的文件**时才拦，拦住就停下协调，不自动 stash / reset / checkout。
- 跨机在同一分支上双写同一文件 = 冲突源。要并行就各开分支 + worktree。
- 别把别人的 WIP 收进自己的提交：共享检出上全树 `git add -A` 会连带对方在制品
  (7-29 的快照 `4b3e84d` 就发生过，已在协作状态里登记)。
- PWA 私有 anchor、PDF 几何、EPUB reflow 等只由对应 `DocumentHost` 解释；扩展只调用白名单。
- token/namespace/owner token 不得进入页面、日志或文档；内容脚本不得获得明文凭据。
- 同步、评分、Anki 添加等未知 mutation 结果一律 fail closed；不能为了“可用”冒险重复写。
- 发布必须使用项目原子部署/通道脚本和可复现候选；**清单内**文件禁止手工 `cp` 覆盖生产。
- 生产文件清单以 `scripts/reader_deploy_manifest.py` 为唯一事实源，实际写入只走
  `scripts/deploy_reader.sh`；旧文档里的手工文件列表不能代替 manifest。
  清单外文件（`insights.py`/`fitness*.py`/`qa_server.py`/`control.html` 等）不在 reader
  release 边界内，仍是 `cp` + restart，但要自己先备份 —— 见 [部署流程](references/deployment-workflow.md)。
- **验收按改动类型分级**（2026-07-31 用户更新流程）：

  | 改动类型 | 验收时点 |
  |---|---|
  | Reader/PWA 共享的新功能、UI/视觉或交互变更（可由原子部署回滚） | 合同测试与无副作用预检通过后**直接部署到生产 iPad**，由用户在 iPad 验收；默认不插入独立扩展测试环境 |
  | 扩展专属改动 | 只有用户明确要求时才用 `BW Codex Chrome Test`；扩展**正式**渠道发布仍须发布前人工验收 |
  | 数据迁移、schema 变更、不可逆操作 | **必须部署前验收** |
  | 修 bug、重构、性能优化、纯文档 | 预检通过即可部署，不必等用户验收 |

  直接部署或免验收都要求：①目标行为有合同测试覆盖；②`deploy_reader.sh` 全程通过（它带自动回滚）；
  ③不含数据迁移/schema/不可逆操作。用户在生产 iPad 报错时，优先回滚到最近可用提交再修。
  部署后 agent **必须**主动报健康检查与关键端点结果，不能"部署完就完了"。
  用户已知悉直接 iPad 验收的取舍：可见回归可能先到生产，由原子回滚和 git 历史兜底。
- 独立浏览器自动化仅按需使用，不再作为 Reader/PWA 部署的默认中间步骤。
- 物理手写笔、双真实设备同步、registry 跨代迁移等未自动覆盖场景不得误报通过。

### 何时必须停下问用户，何时不要停

安全边界只覆盖**不可逆或触及用户设备/生产**的动作。把这份谨慎泛化到整个流程会让每一步都卡住。

**必须停**：数据迁移/schema/不可逆操作、发布扩展正式 channel、
首次安装/注册 native host 或开机项、首次配对、动用户的日常 Chrome/账号/已装扩展、
改 nginx 或 systemd、删除生产数据、跨机改到别人正在改的文件（安全闸退出码 2/3）。

**不要停**：本地 build、跑测试、`handoff_check.py`、`--preflight-only` 预检、
读日志与状态、`git status`/`fetch`，以及合同测试覆盖且可原子回滚的 Reader/PWA 直接部署；
既有 ReaderPC/Direct 的原子更新、候选分支推送与 TestFlight 内测上传也无需逐次确认，失败时
优先自动回滚或读取日志后重试。Pi 生产部署与桌面扩展正式 channel 不在这项持续授权内。
前述检查无副作用；直接部署由原子回滚保护，均做完后主动报结论。
用户已授权持续推进的大项目中，已确认范围内的小目标也按此处理：直接完成预检、原子部署与健康检查，
不为同一发布链的每个小步骤重复请求确认；只有落入上方“必须停”边界时才暂停。

### 维护你自己的项目认知（重要）

`~/.codex/memories/` 记的是**会话事实**（设备、踩坑、用户偏好），那套照旧。但**项目认知**
——版本、能力归属、流程、路径——在 git 管的 `AGENTS.md` 和 `references/*.md` 里，
**它们不会自己更新，是你的工作的一部分**。

**发现文档与现实不符时，就地修正，不要绕过它继续干活。** 判据很简单：如果你为了完成任务
不得不在心里"翻译"一遍文档说的东西（"这写的是 VPS，其实现在是 Pi"、"这版本号早过了"），
那就是文档该改了。绕过去的代价是下一个接手的人重踩一遍——包括未来的你。

**写成不会过期的形式**，这是关键：

| 别写 | 改写成 |
|---|---|
| `工作区 manifest：0.2.58` | `python3 extensions/bw-reader-webext/handoff_check.py` 现场查 |
| 手抄的部署文件列表 | `python3 scripts/reader_deploy_manifest.py`（唯一事实源） |
| `scp root@bwicarus.space:...` | 指向 [部署流程](references/deployment-workflow.md) |
| 具体 SHA-256、`74 files / 1,191,389 bytes` | 由脚本校验，不进文档 |

写死的数字总会过期；**指向命令或唯一事实源就不会**。
`references/codex-reader-context.md` §1 曾长期停在 `0.2.58`（实际已 `0.2.69`），
2026-07-29 已改成查询命令——**不要再往里填具体版本号**，发布事实写进
[协作状态](references/reader-collaboration-status.md) 即可。

改文档跟改代码同批提交，别攒着。文档改动不需要停下来问用户。

### 登记怎么写

往 [协作状态](references/reader-collaboration-status.md) 追加，**控制在 6 行内**，四件事：
改了什么 / 怎么验的（命令 + 结论）/ 明确没做什么 / 下一位做什么。

不要把命令输出复述进文档：`48/48`、`33/33` 这类计数、逐条 SHA-256、文件字节数，
终端里都有，写进登记只会淹没真正需要交接的信息。风险和未决项要写，但一句话说清即可。

### 必须回看原始资料的情况

- 产品归属、接管、存储或同步：`reader-runtime-architecture.md`、
  `reader-extension-ownership.md`
- 当前版本、Windows 环境、发布/回滚：`reader-extension-handoff.md`、
  `reader-collaboration-status.md`
- 跨机开发(哪些测试能在 Windows 跑、换行契约、编码坑、不入库清单)：
  `cross-machine-dev-setup.md`
- 卡片/复习语义：`card-review-integration.md`、`anki-card-format.md`
- Codex app-server、CLI、MCP：`codex-integration.md`、`mcp-server.md`
- Obsidian/Anki 自动化：对应 `.claude/skills/*.md` 与下文列出的笔记规范

## Vault 位置
- Vault 根目录：`C:\obsidian\`
- Windows 项目目录：`C:\claude\`（管理脚本和配置，不含笔记）
- Pi 项目目录：`/home/bwicarus/claude`

## 学习方向
自学：英语、日语、计算机科学（当前重心：大学数学、大学物理）

## 技能 (Skills)
| 指令 | 功能 |
|------|------|
| `/登记新笔记` | 自动扫描新增/已修改笔记，执行完整流程：PDF 标注 → 摘要入索引 → 关联链接 → Anki 制卡 |
| `/summarize` | 对指定笔记生成关键词+摘要，写入知识索引 |
| `/connect` | 在知识索引中查找关联笔记，在原文末尾插入 Obsidian 链接 |
| `/pdf-mark` | 提取 PDF 指定页面和像素区域的内容（文字层或 OCR） |
| `/anki` | 对指定笔记或目录生成 Anki 卡片：agent 判断制卡，脚本同步到 Anki |
| `/website` | 管理个人网站 bwicarus.space：部署页面、更新仪表板、nginx 配置 |
| `/openai-cli-chat` | Codex CLI 多轮对话实现模板：`codex_call()` + `--image` 附图 + 应用层历史拼接 |

## 关键文件

**索引**
- `index/knowledge-index.md` — 主索引（各科目条目数汇总）
- `index/{科目}.md` — 科目索引（各分支条目）
- `index/{科目}/{分支}.md` — 分支索引（条目超 30 条时自动拆分）

**参考规范**（各 skill 按需加载）
- `references/index-format.md` / `vault-structure.md` / `obsidian-syntax.md`
- `references/anki-selection-rules.md` / `anki-card-format.md`
- `references/pdf-annotation-format.md`

**脚本**
- `scripts/summarize_note.py` / `connect_note.py` / `annotate_note.py` / `pdf_extract.py`
- `scripts/pending_notes.py` — 扫描待登记笔记（新增/已修改，命名规则 `[0-9A-Fa-f]{3}-*.md`）
- `scripts/anki_from_note.py` / `anki_status.py` / `daily_anki_status.ps1`
- `scripts/review_priority.py` — 知识图谱复习优先级（激活扩散 + Anki 薄弱度）
- `scripts/note_state.py` — 各脚本共享的笔记内容哈希状态模块

**EXE 启动器**（双击运行，源码在 `launchers/`，编译产物在 `launchers/dist/`）
- `登记新笔记.exe` — 在 `C:\claude` 下运行 `Codex --dangerously-skip-permissions -p "/登记新笔记"`
- `上传网页.exe` — 依次执行仪表板同步四步：Anki 状态更新 → 复习优先级 → 生成 dashboard.json → SCP 推送到服务器

重新编译方法：
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe --onefile --console --distpath launchers/dist --workpath launchers/build --specpath launchers launchers\<脚本名>.py
```

**状态文件（仅脚本读写，不传给 AI）**
- `anki/records/*.json` — Anki 制卡记录（含 `section_hashes`：各节内容哈希，用于增量制卡）
- `state/note-states.json` — 各 skill 最后处理的内容哈希 + 时间戳

## 环境
- Python：`C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`
- 运行脚本时使用完整路径（`python` 命令未加入 PATH）

## 操作规范
- `/登记新笔记` 自动扫描 vault 根目录符合命名规则的待处理笔记；其余 skill 只对用户**明确指定**的文件操作
- 写入索引前检查是否已有同名条目，有则更新而非重复插入
- 所有 Obsidian 链接使用 `[[文件名]]` 格式，不带扩展名
- `anki/records/` 和 `state/note-states.json` 是脚本状态文件，只由脚本读写，**不要传给 AI**
- Anki 制卡按节（`#` 标题）追踪哈希；只有内容变动的节才送给 AI 判断，新卡追加到已有 record，旧卡不删除
- Anki 制卡默认不修改原 md；同步结果写入 `anki/records/`

## 自动化任务
- 任务名「Obsidian Anki 每日状态更新」，每天 04:00 运行
- 执行顺序：启动 Obsidian（等 5 分钟同步）→ 启动 Anki → 轮询 AnkiConnect（最多 2 分钟）→ 查询所有笔记掌握情况并写回 frontmatter 和 record → 计算复习优先级并写回 frontmatter 和 record
