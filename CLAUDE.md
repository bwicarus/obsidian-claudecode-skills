# Obsidian 笔记管理项目

## ⚡ 环境定向（新 session 先读这个）

**同一套代码跑在 3 个环境,路径/端口/服务管理方式都不同。先认清你在哪台机**(`hostname`):

| 环境 | 判定 | 项目根 | Vault | webapp | 服务管理 |
|---|---|---|---|---|---|
| **Windows PC**（主力开发机） | `sys.platform==win32` / 有 `C:\` | `C:\claude` | `C:\obsidian` | `run_local.ps1`→Flask `127.0.0.1:5000`(本地实例,托盘守护 `local_supervisor.pyw`) | Windows 计划任务 + 托盘 |
| **Pi**（hostname `bwicarus`,Tailscale-only,**当前主力服务器**） | Linux + 存在 `/home/bwicarus/claude` | `/home/bwicarus/claude` | `/home/bwicarus/obsidian` | **gunicorn `127.0.0.1:5000`** ← nginx HTTPS(`webapp.service`) | **systemd**(`systemctl`) |
| **VPS**（公网 `bwicarus.space`,⏸ **2026-06-10 起暂停**:只跑 webapp,自动化已 disable） | Linux + 存在 `/root/claude`(⚠ hostname 也是 `bwicarus`,跟 Pi 撞名,**别用 hostname 判定**) | `/root/claude` | `/root/obsidian` | 同 Pi,webapp 代码在 `/root/webapp` | **systemd** |

- ⚠ **本文档下方所有 `C:\...` 路径是 Windows 视角**;在 Linux(Pi/VPS)上换成上表对应根。Python:Windows=`C:\Users\bwica\...\Python313\python.exe`,Linux=`/usr/bin/python3`(env `APP_PYTHON`,见 `.env`)。
- 🔌 **webapp 本机端口恒为 `127.0.0.1:5000`(别猜)**:Linux 上是 gunicorn,外部经 nginx 走 HTTPS(VPS=`bwicarus.space`、Pi=`<host>.taile44d0c.ts.net`)。iPad 截图问答另在 `:9091`(daemon)/`:9090`(cmd_server)。
- 🩺 **一眼看当前状态(Linux)**:
  ```
  hostname; for s in xvfb-99 anki-headless obsidian-sync qa-server webapp bwicarus-daily.timer; do echo "$s=$(systemctl is-active $s)"; done
  curl -sI -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/login   # 应 200
  git -C "$CLAUDE_PROJECT" status -sb
  ```
- 🧭 **接续工作/部署细节**:服务器侧 Claude Code 工作流 → `references/server-side-claude-code.md`;Pi 部署 → `references/raspberry-pi-deployment.md`;VPS 迁移 → `references/linux-server-migration.md`;本地实例(Windows Flask) → `references/webapp-development.md`「本地实例」章。

## Vault 位置
> 见上「环境定向」表;下面按 Windows 主力机写,Linux 换对应根。
- Vault 根目录：`C:\obsidian\`（Pi=`/home/bwicarus/obsidian`、VPS=`/root/obsidian`）
- 本项目目录：`C:\claude\`（管理脚本和配置，不含笔记；Pi=`/home/bwicarus/claude`、VPS=`/root/claude`）

## Sync
- 用 `obsidian-headless`(npm 包)做后台 sync,不依赖 Obsidian GUI
- Windows 计划任务「Obsidian Headless Sync」开机/登录自启,启动器在 `bin/start_obsidian_sync.ps1`,日志写到 `state/logs/obsidian_sync.log`
- 远端 vault:Obsidian Sync(Plus 订阅),Asia 区。**单文件上限 200MB**(Standard 仅 5MB);>200MB 的文件 Sync 永不同步,走旁路 `scripts/push_big_files_to_pc.py`(Pi→PC scp,见「脚本」段)
- iPad 端写完几秒内 Windows 端就能看到,没有等待窗口期
- 注:PC 的「Obsidian Headless Sync」计划任务 2026-05-15 迁服务器时连同每日任务一起被禁,导致 PC vault 停更 3 周(非尺寸问题),2026-06-08 已重新 Enable+Start;托盘守护 `local_supervisor.pyw` 的 `ensure_obsidian_sync()` 现会看护该任务(被禁→Enable、没跑→Start)

## 学习方向
自学：英语、日语、计算机科学（当前重心：大学数学、大学物理）

英语已有一套**语法分析系统**（grammar KG + spacy 词性/依存 + PDF 阅读器 grammar 路由），详见 `references/grammar-analysis-system.md`。

## 技能 (Skills)
> 定义文件在 `.claude/skills/*.md`（项目级、git 管理；本项目**没有** `.claude/commands/` 目录，别去那找）。

| 指令 | 功能 |
|------|------|
| `/登记新笔记` | 自动扫描新增/已修改笔记，执行完整流程：PDF 标注 → 摘要入索引 → 关联链接 → Anki 制卡 |
| `/summarize` | 对指定笔记生成关键词+摘要，写入知识索引 |
| `/connect` | 在知识索引中查找关联笔记，在原文末尾插入 Obsidian 链接 |
| `/pdf-mark` | 提取 PDF 指定页面和像素区域的内容（文字层或 OCR） |
| `/img-mark` | 对笔记中的图片链接生成内容描述标注（脚本预处理 + Claude Code 读图分析 + 脚本写回） |
| `/anki` | 对指定笔记或目录生成 Anki 卡片：agent 判断制卡，脚本同步到 Anki |
| `/website` | 管理个人网站 bwicarus.space：部署页面、更新仪表板、nginx 配置 |
| `/openai-cli-chat` | Codex CLI 多轮对话实现模板：`codex_call()` + `--image` 附图 + 应用层历史拼接 |
| `/claude-quota` | 查 Claude Code 实时额度（5h/7d/Sonnet 使用率 + extra credits），零 token 消耗 |
| 截图问答 | （非斜杠命令，session 入口指南）QA Browser 系统怎么工作 / 如何改，指向 `references/qa-browser-features.md` |
| 技能树 | （非斜杠命令，session 入口指南）技能树 / 知识图谱（KG）怎么工作 / 如何改，指向 `references/skill-tree-system.md` |

## 关键文件

**索引**
- `index/knowledge-index.md` — 主索引（各科目条目数汇总）
- `index/{科目}.md` — 科目索引（各分支条目）
- `index/{科目}/{分支}.md` — 分支索引（条目超 30 条时自动拆分）

**参考规范**（各 skill 按需加载）
- `references/index-format.md` / `vault-structure.md` / `obsidian-syntax.md`
- `references/anki-selection-rules.md` / `anki-card-format.md`
- `references/pdf-annotation-format.md`
- `references/ipad-remote-qa.md` — iPad 远程截图问答操作指南（链路、快捷指令、URL 模板、排错）
- `references/linux-server-migration.md` — 2026-05-14 服务器迁移完整指南（systemd 服务、路径、踩坑速查）
- `references/systemd/*.service|*.timer` — 服务器 systemd unit 文件副本（xvfb-99 / anki-headless / obsidian-sync / qa-server / webapp / bwicarus-daily.service|timer / anki-sync-refresh.service|timer / bwicarus-quick-sync.service|timer / book-ocr.service / book-ocr-watchdog.service|timer（timer 加了 `OnActiveSec=15min`：只有 OnBootSec+OnUnitActiveSec 的 monotonic timer，开机很久后才 restart 会 Trigger:n/a 永不触发，`Persistent=` 只救 OnCalendar） / **bwicarus-backup.service|timer**（每日 03:30 跑 `scripts/backup_data.sh`：webapp/data 账号数据 + claude/state 学习状态 → `~/backups`，保留 14 份） / **push-big-files.service|timer**（仅 Pi：每 4h 把 vault 里 >200MB 文件推到 PC，OnBootSec=5min + OnUnitActiveSec=4h + Persistent，User=bwicarus））
- `references/server-side-claude-code.md` — 在服务器侧用 Claude Code 继续这个项目（tmux 会话、memory 同步、跨机器切换）
- `references/client-exe-development.md` — bwicarus-client.exe 开发指南（launcher + core 架构、版本号管理、跟 webapp 控制面板的职责对照）。注：早期文档提到的 `build_core.py` / `deploy_core.sh` 等构建脚本**实际不在仓库**（`_client/` 下只有 `core/` 和 `launcher/`，无 `build/` 目录），core zip / manifest 的真实打包方式以该文档修订后的说明为准
- `references/webapp-development.md` — bwicarus.space webapp 开发指南（Flask routes 清单、鉴权、SQLite schema、模板两套主题、nginx 反代、部署流程、改 control.py 流程）+ **本地实例（Windows PC 跑 Flask-only）**：`_server_deploy/run_local.ps1` 默认用 pythonw detached 拉起托盘守护进程 `local_supervisor.pyw`（关窗不宕、崩溃自愈+快失败熔断、Job Object 不残留占端口、单实例锁、代码变更自动重启、开机自启、看护「Obsidian Headless Sync」计划任务、托盘菜单）；`-Foreground` 保留前台 `python app.py` 调试。`app.py` `SESSION_COOKIE_SECURE` 默认仍 Secure，本地裸 http 由 `.env.local` 设 0；`control.py` 在 Windows 下 systemd 状态返回 `n/a`（control.html 显灰点不报警 / "本地实例正常"）
- `references/ipad-switch-to-server.md` — iPad 快捷指令切换到服务器的完整步骤
- `references/prompts/*.md` — AI prompt 模板（analyze / analyze_excalidraw / find_related / anki_cards / image_describe）
- `references/skill-tree-system.md` — 技能树/知识图谱（KG）完整系统：KG 结构、关联校验规则（_rejected_links）、UI 叠加面板架构、register 同步链路、回收站机制、**踩坑笔记（覆盖语义/source .env/subprocess buffer/两套 nginx）、renderAnim 调用点、面板布局算法、CSS class 速查、全局变量速查、ADR 架构决策、反向链接 propagate_back_links**
- `references/qa-browser-features.md` — 截图问答（QA Browser）功能详解：两种模式（普通 / cardCtx）、加号选中（真/假标题）、创建新笔记 `/api/create-note`、Anki 卡片 AI 改进、SSE 流式、**SQLite schema + 删除级联、快捷功能、MathJax 节流、AI 后端 adapter、控制面板交互**
- `references/server-config-schema.md` — `state/server-config.json` 字段完整对照（qa_* / ai_* / anki.* / scheduled_register / weak_card_refresh / card_antimodel / card_quality / card_qa）+ 字段流转图 + 修改方法
- `references/learning-dashboard.md` — 学习数据看板（`/insights`）完整文档：自包含 blueprint + 请求时实时聚合 + 120s **按用户分键**缓存、跨 Anki/生词/KG/PDF 阅读 的统一分析、手搓 SVG 图表（活跃度热力图/留存曲线/漏斗/到期/词汇分布/KG 进度）、**Anki 统计忽略旧卡片**（`_LEGACY_DECKS` 默认排除 新/漢字/意味 大批量日语沉浸牌组）、payload 结构、踩坑（stability 桶下界=0 / forecast 占位符顺序 / 缓存分键 / JST 时区）
- `references/pdf-reader.md` — 网页 PDF 阅读器完整文档：路由清单（page-chars/translate/explain/dict/highlights CRUD/snippets-to）、char-layer 选中机制（PyMuPDF rawdict）、**高亮编辑系统**（sidecar JSON、4 字段 color/sentence/body/note、no-color 虚框模式、popover 小框规则）、AI 草稿系统、**iOS Mail 风格 swipe-to-delete**（双处实现 + 三个关键 CSS 点）、设置面板、**踩坑总结**（y 翻转、thenn 空格、popover z-index、TypeError、PATCH 空 color 等）+ **§14 日语支持/性能/翻译批次**（日语词典点词:音调线+汉字 chip 拆解+完整字典页；整页单字=page-chars 缺 no-store 被 iOS Safari 缓存旧分词；disableStream 须配 disableAutoFetch 否则下整本；大 PDF 对象瘦身 `optimize_pdf.py`；模块作用域 vs 内联 onclick 全局必须 `window.`；点词竞态 `_wordPopSeq`；句子按句号断 `bkBreak+lineChanged`；翻译改干净浮层；日语 TTS 用 ja-JP speechSynthesis）+ **整本预热**（`/api/prewarm-async` detached 低优先级跑 `scripts/prewarm_pdf.py` + `/api/prewarm-status` 按已缓存页图算 percent；选书页「📥 预热」按钮 + 开书自动后台预热 `_maybeAutoPrewarm`）+ **三个根因修复**（加载遮罩盖住返回 → 遮罩内加「← 返回书架」+ `goPdfList` 即时反馈；`setupContinuousMode` 几千页同步建占位冻主线程 → 分批 CHUNK=80 + setTimeout(0) 让出事件循环；`_renderPageImg` 全页套 page1 高度致扫描书越往下错位 → ch 改按图真实宽高比）+ **页图宽度容差回退**（`/api/page-image` 缓存键含精确宽度而各设备请求的 w 都差一点 → 精确 miss 时回同页已缓存的其它宽度：≥请求宽回最小一张 immutable；≥70% 先即时回近似图 + 后台 `_spawn_exact_render` 补渲精确宽，`_render_page_jpg` 抽出共用）
- `references/vocab-system.md` — 单词系统完整文档：vault 当数据库（`资源/vocab/<首字母>/<lemma>.md` + `_audio/`）、**三源字典融合**（ECDICT 离线中文+词频 / Free Dictionary 例句+音频备份 / Merriam-Webster Learner's 高质例句+美音音频）、ECDICT exchange 表 lemma 化、MW 富文本 `{bc}/{it}` 剥除、MW 音频 URL 子目录规则、笔记模板（frontmatter + 各源分段 + 文中出现 + 用户备注保留区）、`/pdf/api/dict` 改造（写 lookup 日志 + 后台异步生成笔记）、阶段 A-E 路线 + mastery 算法草稿、**§14 中日词典/日语**（`is_japanese` 路由、`lookup_jp` AI 永久缓存 + **stale-while-revalidate**（旧版词条先秒回 + 后台 `_jp_regen_bg` 重新生成升级，`_jp_ai_fetch` 抽出）、`/api/dict-jp-ai` 深入讲解服务端按原形永久缓存（`_JP_EXPLAIN_VER` 版本化 + SSE 回放）、`_jp_reading_accent` unidic 离线读音+音调 aType、Tanaka 母语例句 `data/tanaka.db`+按句翻译缓存、KANJIDIC 汉字拆解 `data/kanjidic.json`、`/api/dict-jp`+`/api/dict-jp-ai`、预建脚本 `build_tanaka_index`/`build_kanjidic`/`prebuild_jp_dict`/`prebuild_jp_examples`、句子翻译 `translate.py` 两个 guard 坑[A-Za-z]/mymemory源=en）
- `references/fitness-system.md` — 健身系统完整文档：多用户 web 训练追踪 + **循证 AI 教练**（Claude **Opus + max effort + 25+ 篇文献**深度思考）、PPL 3 天 20 动作（拉伸位/RIR/MAV 循证）、Double Progression 推荐、autosave + 刷新恢复 + 休息倒计时、🏁 完成训练总结、Nippard + Cavaliere 双频道视频（`MUST_CONTAIN` 关键词过滤）、YT auto-caption + Cloud STT 双源字幕（Gemini Flash 翻译 fallback Claude）、`fitness_exercise_override` AI 调整后落库、`fitness_session_analysis` 反馈环、⚙ 设置面板（model/effort/auto_analyze/auto_suggest）、5 张 per-user 表 schema + 完整 API 清单 + 6 条踩坑
- `references/google-cloud-apis.md` — GCP API 集成（Vision/YouTube/STT/Gemini）+ ¥47867 Free Trial 赠金管理、双 API key 隔离（`AIzaSy*` GCP 服务 vs `AQ.Ab*` Gemini service-account 绑定）、**计费分流大坑**（Gemini API 走 AI Studio 独立 billing 跟 GCP 赠金不通）、本地配额计数器（`scripts/google_api_quota.py` + SQLite `state/google_api_quota.db`）、YouTube 每日 10k units 硬上限（耗尽走本地 reorder）、PT 重置时区、key regenerate 流程、**Cloud Translation API**（PDF 句子翻译 `_gtranslate`、`API_KEY_SERVICE_BLOCKED` 放行步骤+刚放行传播抖动、翻译质量评审结论 Google 3.9/4.5<AI、auto 链 `gtranslate→deepl→ai→mymemory`、CLI 冷启动~5s/热进程实验不值得的结论）
- `references/claude-code-quota-api.md` — Claude Code 额度查询 API（/claude-quota skill 的实现参考：端点 / 认证 header / 响应格式 + 共享模块 `scripts/lib/claude_quota.py`）
- `references/book-ocr-pipeline.md` — 日文扫描 PDF 双 OCR 流水线：mokuro manga-ocr + Google Vision 两条路径、`state/mokuro-ocr/<sha>/` 断点续传、不可见文字层 embed、book-ocr / book-ocr-watchdog systemd
- `references/grammar-analysis-system.md` — 英语语法分析系统：grammar KG（`scripts/kg/build_grammar_nodes.py` 三层抽取 + `grammar-nodes.json`）+ spacy 词性/依存（独立 spacy-venv，`spacy_parse.py --server` 常驻模式免每次加载模型，pdf_reader 经 `_spacy_worker_request` 锁串行+超时自愈调用）+ pdf_reader 的 grammar-* 路由（跟踪语法点分析；spaCy 结果存 sentence-only 缓存键、grammar-stream 有回放缓存）

**脚本**
- `scripts/config.py` — 集中管理路径和常量（其他脚本从这里读）
- `scripts/register_notes.py` — 笔记登记编排（批量或单步）；CLI: `--note PATH --only {pdf,img,summarize,connect,anki,all}`
- `scripts/summarize_note.py` / `connect_note.py` / `annotate_note.py` / `annotate_images.py` / `pdf_extract.py`
- `scripts/pending_notes.py` — 扫描待登记笔记（新增/已修改，命名规则 `[0-9A-Fa-f]{3}-*.md`）
- `scripts/anki_from_note.py` / `anki_status.py` / `daily_anki_status.ps1`
- `scripts/review_priority.py` — 知识图谱复习优先级（激活扩散 + Anki 薄弱度）
- `scripts/refresh_weak_cards.py` — 卡片 AI 维护，`--task` 多模式共用一套管道（原地 updateNoteFields 不破坏 FSRS + 冷却 + 回滚 record `_refresh.history` + 裸文本 LaTeX 校验 + dry-run 默认）：`weak` 薄弱卡 L1 重写问法/L2 拆删；`antimodel` 已掌握卡换角度重问防只记问法；`quality` 低质卡：静态启发式(答案过长/多知识点/指代不清) + 行为信号(again+hard 占比、答题耗时，来自 getReviewsOfCards) 扩召回 + 同 type P85 相对阈值 + 每晚随机采样兜底盲区 → AI 评分原地优化或建议拆，写 `state/quality_report.json`(ok/改/拆 + by_flag 趋势)上仪表盘。凌晨由 server-config `weak_card_refresh`/`card_antimodel`/`card_quality` 各自 enabled 控制
- `scripts/note_state.py` — 笔记内容哈希 + 失败追踪（连续失败 ≥3 次自动跳过）
- `scripts/backfill_back_links.py` — 一次性脚本，给存量笔记补全反向链接
- `scripts/push_big_files_to_pc.py` — Pi 把 vault 里 >200MB 文件 scp 推到 PC（超 Obsidian Sync Plus 单文件 200MB 上限的旁路；PC 可达才推、睡着静默跳过、同名同大小不重传；目前全 vault 仅 `応用情報技術者.pdf` 318M 触发）。由 Pi systemd timer `push-big-files.timer` 每 4h 跑
- `scripts/prewarm_pdf.py` — PDF 阅读器整本预热一把梭：先 `prewarm_pdf_pages.py`（按宽度渲全部页图）再 `prewarm_pdf_chars.py`（算全部字符层+振假名），各自 ProcessPoolExecutor 并行。被 `pdf_reader.py` 的 `/api/prewarm-async` 以 detached 低优先级子进程启动

**EXE 启动器**（双击运行，源码在 `launchers/`，编译产物在 `launchers/dist/`）
- `任务监视.exe` — 系统托盘 + 悬浮窗。开机启动后自动拉起 cmd_server / 截图问答 / relay；菜单可切换 AI 后端（Claude / GPT）
  **注：新架构下被 `_client/dist/bwicarus-client.exe` 替代，托盘 + 悬浮窗 + cmd_server 全部整合进去**
- `登记新笔记.exe` — 调用 `scripts/register_notes.py` 编排脚本，直接执行完整流程（不经 Claude CLI）
- `上传网页.exe` — 依次执行仪表板同步四步：Anki 状态更新 → 复习优先级 → 生成 dashboard.json → SCP 推送到服务器
  **注：被客户端的「刷新并上传网页」按钮替代（流程一致：anki_status → review_priority → export_dashboard → 上传 → export_history → 上传，POST 替代 SCP）**
- `cmd_server.exe` / `截图问答.exe` / `relay.exe` — 由任务监视统一启动，不直接双击
  **注：cmd_server.exe 因 PyInstaller 命名冲突 broken，且已整合进客户端为线程；截图问答整合进客户端 qa_browser 模块**

## bwicarus-client（多用户客户端）

源码：`_client/launcher/` + `_client/core/`。launcher 是 PyInstaller --onefile，core 是热更新 zip 包（每次启动自动拉服务器最新版）。

**架构两层**：

- **Launcher**（`bwicarus-client.exe`）— 大约 33MB，PyInstaller 单文件
  - 首次启动弹「选择数据保存位置」窗口，写指针文件 `%APPDATA%\bwicarus-client\datadir.txt`
  - 之后每次启动读 manifest 检查 core 版本，新版自动下载 + 解压 + 加载
- **Core**（`%LOCALAPPDATA%\bwicarus-client\core\<version>\`）— 业务逻辑 zip，可热更
  - 服务端 `static/client/core-<v>.zip` + `manifest.json`（launcher 只消费 `version` + `core_url`）。注：`_client/build/build_core.py` / `deploy_core.sh` 等构建脚本**实际不在仓库**（`_client/` 下只有 `core/` + `launcher/`），真实打包方式见 `references/client-exe-development.md`

**关键模块**（`_client/core/`）

| 文件 | 职责 |
|---|---|
| `gui.py` | customtkinter 主窗口，7 个 tab + 滚动布局 + 日志折叠 |
| `wizard.py` | 首次启动 4 步配置向导：登录 / Obsidian / Anki / AI |
| `auth.py` | device-link OAuth：浏览器登录 → loopback callback → token 自动配置 |
| `api_client.py` | 服务端 HTTP（`/api/upload/<dataset>` 等），bearer token |
| `uploader.py` | `upload_dataset(client, dir, "dashboard"|"history")`，白名单 JSON+图片，**不传 HTML/JS/CSS** |
| `cmd_server_thread.py` | 9090 端口 HTTP server。路由：`/list`、`/run/<cmd>`（iPad 触发 register/upload）、`/qa`（iPad 截图注入，转发到 qa_browser daemon `/api/inject-image`）|
| `qa_browser.py` | 截图问答。两种入口：(a) 本机 ctrl+shift+q `launch()` 临时启动；(b) `start_server_daemon()` 常驻 0.0.0.0:9091 让 iPad 通过 Tailscale 直连完整对话页（cfg `qa_remote_daemon` 控制）。`/api/chat` 支持 **SSE 流式**（`Accept: text/event-stream`，前端边接边渲染 + 节流 + 中止），旧 JSON 模式保留兼容。`/api/history/delete` 级联清理：SQLite + 截图 + **Obsidian 笔记**（解析 note 字段路径）+ 触发 `_export_history_to_webapp`。数据目录走 `paths.app_dir()`（服务端实例 = `state/qa-server-data/`，env `WEBAPP_HISTORY_DIR` 配了就把历史导出到 webapp `/history/`）|
| `runner.py` | `run_script(path, ..., python_exe)` subprocess 跑主项目脚本，所有 subprocess 自动 CREATE_NO_WINDOW |
| `paths.py` | 数据目录抽象：`app_dir()` 读 `BWICARUS_APP_DIR` / pointer / 默认；`derive_paths(project_root)` 从主项目根派生所有子路径 |
| `watcher.py` | watchdog 监听 vault，防抖+冷却后触发 register |
| `scheduler.py` | 每日定时任务（默认 04:00） |
| `floating_window.py` | 任务进度悬浮窗，半透明置顶，可拖动 / 鼠标穿透 |
| `tray.py` | pystray 托盘图标 + 菜单 |
| `hotkey.py` | 全局快捷键（默认 ctrl+shift+q 触发截图问答） |
| `startup.py` | 写 HKCU\\...\\Run 实现开机自启 |
| `ai_backends.py` | 5 个 AI 后端 adapter：claude_cli / codex_cli / claude_api / openai_api / ollama |

**主窗口 7 个 tab**

| Tab | 内容 |
|---|---|
| 基础 | server_url / api_token / 测试连接 / 一键登录 / 跑配置向导 / 数据目录显示 / 开机自启 |
| AI | 后端下拉 + settings（动态字段，secret 字段带 [粘贴][复制] 按钮） |
| Anki | exe_path / connect_url / ping / 启动 Anki / **AnkiConnect 不可达时自动重启** 开关 (`cfg.anki.auto_restart`) |
| 笔记登记 | vault_path / 立即运行登记 / **立即跑完整定时任务** / 刷新并上传网页 / 登记后自动上传开关 / 每日定时 + **完成后上传网页**子开关 / vault watcher |
| 任务监视 | 悬浮窗位置 / 鼠标穿透 / 远程触发（cmd_server 端口+密钥） |
| 截图问答 | 习题/错题子目录 / 浏览器路径 / 快捷键 / 远程访问 + 子开关「iPad 远程截图问答」(常驻 daemon :9091) |
| 高级 | 主项目根目录 + 7 个派生路径（默认从根派生，可单独覆盖）|

**与主项目 scripts 的关系**

客户端**调用**主项目脚本，不复制其逻辑。主项目脚本（`register_notes.py` / `anki_status.py` / `review_priority.py` / `build_review_deck.py` / `cleanup_orphans.py` / `export_dashboard.py` / `export_history.py`）通过 subprocess 调用。

**三个按钮职责清晰分开（0.9.32+）**：

| 按钮 / 触发 | 流程 | 含必复习计算？|
|---|---|---|
| 「立即运行登记新笔记」 | `register_notes.py` 单步 | 否 |
| 「刷新并上传网页」 | anki_status → review_priority → export_dashboard → upload dashboard → export_history → upload history | 否（只读 Anki）|
| **「立即跑完整定时任务」** / **凌晨定时** | ensure_alive → register → anki_status → review_priority → **build_review_deck** → cleanup_orphans → export_dashboard → (可选)upload → AnkiWeb sync | **是** |

**完整 daily 流程**由 `gui.py::_full_daily_pipeline` 实现，等价主项目 `daily_anki_status.ps1`。凌晨定时和「立即跑完整定时任务」按钮共用，仅 `force_restart` 入参不同：
- 凌晨：`force_restart = cfg.scheduled_register.wake_anki`（默认 True）
- 手动按钮：`force_restart = cfg.anki.auto_restart`（默认 False）

**关键 cfg 开关**：
- `auto_upload_after_register`（默认 False）— 「立即登记」结束后是否自动接「刷新并上传网页」
- `scheduled_register.upload_after`（默认 False）— 凌晨 / 「立即跑完整定时任务」结束后是否 upload dashboard + history
- `scheduled_register.wake_anki`（默认 True）— 凌晨触发时是否 force_restart Anki（杀僵尸 + 重启 + 轮询 ≤180s）
- `anki.auto_restart`（默认 False）— AnkiConnect 不可达时手动按钮场景是否自动 force_restart Anki

watcher 触发只调 `_run_register`（register 单步），**不**跑完整 daily。

**服务端**

`_server_deploy/` — Flask 多用户应用：

| 路由 | 用途 |
|---|---|
| `/login` / `/logout` / `/register` | 邀请码注册 + session 登录 |
| `/profile/` | 改密码 / 管理 API token（已生成的 token 可随时显示+复制）/ 下载客户端 .exe |
| `/admin/` | 邀请码管理 / 用户列表 |
| `/dashboard/` / `/history/` / `/private/` | 用户的私有目录，缺失文件回落到 `dashboard_template/` / `history_template/` |
| `/api/upload/<dataset>` | 客户端 POST 上传（bearer token），admin 用户上传的 HTML/CSS/JS 自动同步到 template |
| `/auth/device-link` | 客户端登录回调：登录后跳到 loopback `http://127.0.0.1:PORT/auth-cb?token=...` |
| ~~`/qa/` / `/qa/update`~~ | 已废弃。nginx `/qa` 反代 2026-05-11 删，路由保留但外部不可达。iPad 截图问答改走客户端本机 daemon（见下节）|

数据：`/root/webapp/data/users/<username>/{dashboard,history,private}/` + `/root/webapp/data/{dashboard_template,history_template}/`

## iPad 远程截图问答

iPad 通过 Tailscale 直接访问本机 qa_browser daemon —— **跟本地按 `ctrl+shift+q` 看到的页面 100% 一致**（同一份 HTML、同一组 API、同一个进程内的 state / SQLite / vault 访问）。**不经过 bwicarus.space 公网服务端**。

**架构**：

```
iPad 拍照 → POST cmd_server :9090/qa?key=<API_KEY>  body 含 base64 截图
              ↓
           cmd_server 转发到 daemon /api/inject-image
              ↓
           qa_browser daemon (0.0.0.0:9091) 解码（含 HEIC→PNG）→ 写文件 → 注入 state → session.reset()

iPad 浏览器 → http://<Tailscale-IP>:9091   完整 qa_browser HTML + 所有 /api/*
              （拿到截图、输入问题、Markdown + MathJax 渲染、保存到 vault、历史侧栏、AI 后端切换等全部功能）
```

**开关**（GUI 截图问答 Tab）：

```
远程访问 [✓] 允许局域网其他设备访问对话窗（监听 0.0.0.0）
            [✓] └─ iPad 远程截图问答（常驻 daemon :9091，cmd_server :9090 /qa 注入截图）
```

cfg 字段 `qa_remote_access`（父）+ `qa_remote_daemon`（子）。父开关关 → 子开关自动 disable。

**iPad 端配置**：见 `references/ipad-remote-qa.md`。要点：
- 拍照前快捷指令加「转换图像 → JPEG」（HEIC 也行，daemon 自动转 PNG，但 JPEG 体积更小）
- API key 在 `%LOCALAPPDATA%\bwicarus-client\cmd_server_key.txt`
- 浏览器 URL：`http://<Tailscale-IP>:9091`（直连 daemon，不经 cmd_server）

**state 共享隐患**：本机按 `ctrl+shift+q` 启的临时 server 跟 daemon **共用** 模块级 `state` 字典。同时操作会串扰（截图覆盖、session 混合）。单人多端通常不撞，遇到问题先各自结束当前会话再开新的。

**QA browser 两种模式 + 各自功能**（完整说明见 [`references/qa-browser-features.md`](references/qa-browser-features.md)）：
- **普通模式**（默认）：每个 AI 回答下方有 `＋ 选用整条回答` + 子标题旁 `+`（真标题级联 / 粗体段落假标题单段）。勾选任意 → 右上角 `📝 创建新笔记`：prompt 输笔记名 → AI 整理选中问答 + 截图 → `<VAULT>/<name>.md`（不带前缀，避开 register） + `attachments/<name>.png` → 完成后给 obsidian:// URL（后台 job + 前端轮询，防移动端断连）
- **cardCtx 模式**（`?card=<local_id>` 进入，Anki 卡复习链接）：同样的 + 选中，但右上角是「更新到笔记 / 修改 Anki / 全部」，AI 改写源笔记或生成新卡替代旧卡（async job 防移动端断连丢结果；`/api/card-delete` 删卡后的 AnkiWeb sync 改后台 fire-and-forget，返回 `synced:'pending'`，失败由 15min anki-sync-refresh 兜底）
- system prompt 强调 `数学公式严格用 $...$，禁止反引号包裹数学`（避免 ` ` 包数学被 markdown 当 inline code 灰底显示）

## 服务器侧自动化（多实例：VPS + Raspberry Pi）

2026-05-14 起整套工作流跑在 `bwicarus.space` VPS 上（Ubuntu 22.04，2 vCPU / 7.8GB RAM,曾升配）。**2026-05-15** 迁到 Raspberry Pi 5（Debian 13，8GB / NVMe，hostname `bwicarus`）,Pi 成为**唯一活跃实例**。⏸ **VPS 自 2026-06-10 起正式暂停**:自动化单元(xvfb-99/anki-headless/obsidian-sync/qa-server/bwicarus-daily.timer)已 `systemctl disable`(重启也不复活),只保留公网 `webapp.service`;代码停在 2026-05-28(落后 main,重新启用前必须先 `git pull` + 重部署)。git 仓库 + AnkiWeb + Obsidian Sync 是共享 source of truth。

| 实例 | hostname | 公网 | Tailscale IP | 角色 |
|---|---|---|---|---|
| VPS | ⚠ 实际也是 `bwicarus`(跟 Pi 撞名;Tailscale 设备名才是 `bwicarus-3`) | `bwicarus.space` ✅ | `100.110.193.39` | ⏸ 暂停中,只跑公网 webapp |
| Pi 5 | `bwicarus` | ❌ Tailscale only | `100.101.15.57` | **主力**:自有数据中心 + 全部自动化 |

**长期目标**：关 Windows EXE 客户端 → 工作主体迁服务器 / Pi → 在 SSH + tmux + Claude Code 模式下用。

**实例文档**：
- VPS 完整指南：[`references/linux-server-migration.md`](references/linux-server-migration.md)
- Pi 完整部署：[`references/raspberry-pi-deployment.md`](references/raspberry-pi-deployment.md)
- SSH + Claude Code 接续工作流：[`references/server-side-claude-code.md`](references/server-side-claude-code.md)
- SSH 客户端 Snippets：[`references/pi-snippets.md`](references/pi-snippets.md)

**Tailscale 接入**：两台机器都在 tailnet。MagicDNS hostname：
- VPS：`bwicarus-3.taile44d0c.ts.net`
- Pi： `bwicarus.taile44d0c.ts.net`

两边都通过 **Tailscale HTTPS Cert**（Let's Encrypt 真证书）签 SSL，浏览器无警告。iPad 通过 Tailscale 私网访问 qa_browser / cmd_server，**不走公网**。OpenVPN Access Server 在 :914/:943 跟 Tailscale 共存（之前确认过不冲突）。

**iPad 端口入口（Tailscale 内）**：

| 用途 | 端点 |
|---|---|
| 浏览器看截图问答页面 | `http://<Tailscale-IP>:9091/` |
| POST 截图注入 | `http://<Tailscale-IP>:9090/qa?key=<KEY>`（key 在 `/root/claude/state/qa-server-data/cmd_server_key.txt`） |
| 触发 register / daily / ankiweb-sync | `http://<Tailscale-IP>:9090/run/<cmd>?key=<KEY>` |

**关键设施**：
> 下表路径按 VPS（`/root/...`）写，Pi 换 `/home/bwicarus/...`；带 `bwicarus.space` 的 URL 在 Pi 上对应 `https://bwicarus.taile44d0c.ts.net/...`（VPS 暂停后日常都用 Pi 侧）。

| 项 | 路径 | 说明 |
|---|---|---|
| 主项目 | `/root/claude/` | git clone 自 GitHub，跟本机 `C:\claude\` 同步 |
| Vault | `/root/obsidian/` | obsidian-headless sync 拉，1175 笔记 |
| Anki | `/opt/anki-venv/` + `/root/.local/share/Anki2/User 1/` | aqt 25.2.7 + Xvfb 跑 GUI，5634 卡 |
| 环境变量 | `/root/claude/.env` + `/etc/profile.d/claude.sh` | `CLAUDE_PROJECT` / `OBSIDIAN_VAULT` / `APP_PYTHON` / `APP_CLAUDE` / `APP_CODEX` / `ANKI_CONNECT_URL` / `AI_SETTINGS_FILE` |
| **systemd 服务** | `/etc/systemd/system/` | `xvfb-99` + `anki-headless` + `obsidian-sync` + `qa-server` + `bwicarus-daily.timer` (01:00;原 04:00,73d8eb6 起提前错开时段) + `tailscaled` + `webapp` + `anki-sync-refresh.timer` (15min 拉手机复习数据) + `bwicarus-quick-sync.timer` (15min vault 状态同步) + `book-ocr.service` + `book-ocr-watchdog.timer` (日文 PDF OCR 后台 + 自检) + `bwicarus-backup.timer` (每日 03:30 `backup_data.sh` 备份 webapp/data + claude/state → `~/backups`，保留 14 份)。**仅 Pi** 另有 `push-big-files.timer` (每 4h 把 vault 里 >200MB 文件推到 PC，绕过 Obsidian Sync Plus 单文件 200MB 上限) |
| **控制面板** | `https://bwicarus.space/control/` | 替代 Windows 客户端 EXE。3-panel 布局：状态（系统+Daily）/ 操作（触发+日志）/ 设置（AI 后端+所有同步开关）+ 左侧滑出 drawer 含可编辑导航链接，需登录 |
| **qa-server daemon** | systemd `qa-server.service` | 跑 iPad 截图问答 daemon (`:9091`) + cmd_server (`:9090`)，复用 `_client/core/qa_browser.py` + `cmd_server_thread.py`，ExecStartPre sed 替换 jsdelivr CDN URL 为 `bwicarus.space/static/qa/` + 去掉 `--dangerously-skip-permissions` + 加 `--allowedTools Read`（这 3 个 patch 必须保留，git pull 覆盖后 service restart 时自动重新 patch）|
| **服务器侧配置** | `/root/claude/state/server-config.json` | 控制面板「设置」面板写入，所有 Windows EXE 客户端开关同步在此（sidebar_links 自定义链接、anki.auto_restart、auto_upload_after_register、scheduled_register.{wake_anki,upload_after}、weak_card_refresh.*、card_antimodel.*、card_quality.*、qa_remote_daemon、qa_exercises_subdir、qa_wrong_subdir）|
| **技能树 / KG** | `https://bwicarus.space/skilltree/<book>/` | 知识图谱可视化页。home 整体永远底层，左侧 focus 叠加面板（紧凑章带，仅 chain 节点）+ 右侧 detail，进页面定位 localStorage 最近学习节点。完整架构 + 关联校验规则（_rejected_links）+ 回收站 见 [`references/skill-tree-system.md`](references/skill-tree-system.md) |
| **PDF 阅读器** | `https://bwicarus.space/pdf/` | 网页 PDF 阅读器：PDF.js v4 + PyMuPDF char-bbox 选中（绕开 textLayer 偏移）+ AI 翻译/解释/问 AI（SSE 流式 + Markdown + MathJax）+ ECDICT 离线字典（单词秒查不耗 AI）+ **高亮编辑**（sidecar JSON、4 色板互斥激活态、点 cur 色取消颜色保留备注、popover 备注小框单行省略点击展开、iOS Mail 左滑右侧露出 🗑 删除）+ 草稿系统（AI 回答 + 选段 → 笔记/Anki）+ 设置面板（model/effort/debug/颜色管理）+ 右侧抽屉知识点关联 + **操作性 7 件套 F1-F7**（页码滑块 scrubber / 单页横滑翻页 / 整页翻译「译页」/ 全文搜索「🔍」/ 振假名+英文音标「あ」ruby 叠加 / 短多选→词组收藏作分词依据「📘词组」/ 日语生词下划线高亮）。完整文档 + 踩坑（含 §15 F1-F7）见 [`references/pdf-reader.md`](references/pdf-reader.md) |
| **学习数据看板** | `https://bwicarus.taile44d0c.ts.net/insights/` | 跨 Anki/生词/KG/PDF 阅读 的统一学习分析（区别于 `/dashboard` 的复习优先级，这里补**时间序列**+**新系统**维度）：活跃度热力图（GitHub 风格，Anki 复习+查词）/ Anki 留存曲线·漏斗·到期·FSRS 记忆强度 / 薄弱点 / 生词掌握分布+词汇量增长 / KG 每本书进度 / PDF 阅读进度。自包含 blueprint（`register_insights`）+ 请求时实时聚合 + 120s 按用户分键缓存 + 手搓 SVG 图表（无 CDN）。**Anki 统计忽略旧卡片**（`_LEGACY_DECKS` 默认排除 新/漢字/意味）。完整文档见 [`references/learning-dashboard.md`](references/learning-dashboard.md) |
| **健身系统** | `https://bwicarus.taile44d0c.ts.net/private/fitness/` | 多用户 web 训练追踪：PPL 3 天 / 20 动作 / 循证训练原则（拉伸位+RIR+MAV）。每动作 Double Progression 推荐 + autosave + 刷新恢复 + 休息倒计时（按 `prescribed.rest_seconds`）。🤖 **循证 AI 教练**（Claude Opus + max effort + 25+ 篇 hypertrophy meta 文献）手动触发调整 prescribed + 完成训练后分析（completion / RPE 估 / verdict / per-exercise next_action / insights / warnings）+ 反馈环（落库 `fitness_session_analysis` 下次 suggest 引用）。视频 carousel 5 Nippard + 5 Cavaliere 双频道（YouTube Data API + `MUST_CONTAIN` 关键词过滤）。字幕双源:YT auto-caption（Gemini Flash 翻译 ~5s）/ Cloud STT 重新转录（高质量 + 烧 GCP 赠金）。⚙ 设置面板可调 model/effort + 自动分析开关。完整架构 + API 清单 + 文献库 + 踩坑见 [`references/fitness-system.md`](references/fitness-system.md) + GCP 集成见 [`references/google-cloud-apis.md`](references/google-cloud-apis.md) |

**控制面板源码**（全部在 git，部署 = 纯 cp）：
- `_server_deploy/app.py` → 部署到 `/root/webapp/app.py`（含 `/api/nav-links` 路由、`register_control` 导入、`/control` 进 `PROTECTED_PREFIXES` / `NAV_INJECT_PREFIXES`）
- `_server_deploy/control.py` → 部署到 `/root/webapp/control.py`
- `_server_deploy/templates/control.html` → 部署到 `/root/webapp/templates/control.html`
- `_server_deploy/static/nav.js` → 部署到 `/var/www/html/static/nav.js`（全站通用左侧导航 + per-user 链接持久化）

**健身系统源码**（部署见 `references/fitness-system.md`,等价 `cp _server_deploy/{fitness,fitness_coach,youtube_subtitles,youtube_speech}.py /webapp/` + `cp templates/fitness/*.html /webapp/templates/fitness/` + `cp static/fitness-plan.json /webapp/static/` + restart）:
- `_server_deploy/fitness.py` → Flask blueprint(`/private/fitness/*` 页面 + `/api/fitness/*` API,21 个 endpoint)
- `_server_deploy/fitness_coach.py` → AI 教练(Claude Opus + max,25+ 篇文献 prompts)
- `_server_deploy/youtube_subtitles.py` → 字幕拉 + 翻译(Gemini Flash fallback Claude)
- `_server_deploy/youtube_speech.py` → Cloud Speech-to-Text 转录(yt-dlp + ffmpeg + 并发 4 worker)
- `_server_deploy/static/fitness-plan.json` → PPL 3 天 20 动作循证 plan
- `_server_deploy/templates/fitness/{_base,home,log,plan,history}.html` → 模板
- 相关脚本:`scripts/{upgrade_fitness_plan,add_pullup_exercises,find_jeff_videos,reorder_videos_by_keyword,google_api_quota}.py`
- `_server_deploy/nginx/bwicarus.conf` → **仅 VPS** 部署到 `/etc/nginx/sites-enabled/default`（含 `/`、`/control`、`/auth`、`/api` 等所有 location 块）。改完 `nginx -t && systemctl restart nginx`（注意：新增 location 块 `reload` 可能不完全生效，要 `restart`）
  - ⚠️ **Pi 实例 nginx 配置独立**：`/etc/nginx/sites-available/bwicarus`（Tailscale HTTPS Cert + 80/443 两 server 块，server_name 是 ts.net），**结构与 git 这份 VPS 版完全不同，绝不可 cp 覆盖**（会冲掉 Tailscale 证书配置全站挂）。Pi 改 nginx 只能手工 patch 该文件对应 server 块，git 这份只代表 VPS

**跨平台改动**（让脚本可在 Windows + Linux 跑）：
- `config.py` AI_SETTINGS_FILE 加 env 优先
- `ai_client.py` 改 import config，`_run_hidden` 跨平台，Linux 上 Claude CLI 不加 `--dangerously-skip-permissions`（root 禁用）
- `service_switch.py` 加 `WINDOWS = sys.platform == "win32"` 守卫
- `pending_notes.py` 路径从硬编码改 `from config import VAULT_ROOT`
- `scripts/daily_anki_status.py`（新增 Linux 版 daily 编排）

**Anki 25 headless 关键坑**（见 reference）：
- 必须 `QTWEBENGINE_DISABLE_SANDBOX=1` + `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox`（root 用户）
- profile 自动创建有 i18n backend 依赖问题，用 Python `ProfileManager._loadMeta + create("User 1")` 绕开
- obsidian-headless 必须 Node 22+（全局 `WebSocket` API）+ `npm rebuild better-sqlite3`
- 二进制名是 `ob`，子命令是 `sync-setup`（不是 `init`）

## 电源 / 屏幕守护

主项目客户端**不再**处理睡眠/屏幕策略——这事归独立项目 `C:\autoscreen\`（详见其 README）。

- 客户端跑着 ≠ 系统不睡（早期 0.9.23 的 antisleep 模块已撤回 0.9.24）
- 「合盖关屏 + 远程可达」由 autoscreen 用 `SetThreadExecutionState(ES_SYSTEM_REQUIRED)` 在内存里动态控制，不需要永久改 powercfg
- autoscreen 登录自启，托盘图标只剩 Pause/Resume 一个开关

## AI 后端
- 项目只有一个根目录 `C:\claude\`，AI 后端只是 `%LOCALAPPDATA%\截图问答\settings.json` 里的字段
- `scripts/ai_client.py` 的 `ask()` 每次调用都重新读取 settings，所以切换 AI 不需要重启服务
- 切换由 `service_switch.py switch <claude|gpt>` 完成，亦可在任务监视托盘菜单点选
  - `claude` → `{"backend": "auto-claude"}`，限流时降级到 Codex
  - `gpt` → `{"backend": "codex", "model": "gpt-5.5"}`，限流时降级到 Claude

重新编译方法：
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe --onefile --noconsole --distpath launchers/dist --workpath launchers/build --specpath launchers launchers\<脚本名>.py
```

**状态文件（仅脚本读写，不传给 AI）**
- `anki/records/*.json` — Anki 制卡记录（含 `section_hashes`：各节内容哈希，用于增量制卡）
- `state/note-states.json` — 各 skill 最后处理的内容哈希 + 失败追踪（`failure_count` / `last_error`）
- `state/active_tasks.json` — 任务追踪（task_tracker 写，任务监视读）
- `state/quality_report.json` — 卡片质量体检历史（refresh_weak_cards --task quality 写，export_dashboard 读 → 仪表盘「卡片体检」面板）
- `state/logs/ai_calls.log` — AI 调用日志（5MB 滚动）
- `state/backup/` — 每日 daily 流程自动备份 7 天的 note-states 和 anki-records（Windows=`daily_anki_status.ps1`，Linux=`daily_anki_status.py::rotate_backups`）

## 环境
- Python：`C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`
- 运行脚本时使用完整路径（`python` 命令未加入 PATH）

## 操作规范
- `/登记新笔记` 自动扫描 vault 根目录符合命名规则的待处理笔记；流程中自动调用 `annotate_images.py` 处理笔记内图片；其余 skill 只对用户**明确指定**的文件操作
- 写入索引前检查是否已有同名条目，有则更新而非重复插入
- 所有 Obsidian 链接使用 `[[文件名]]` 格式，不带扩展名
- `anki/records/` 和 `state/note-states.json` 是脚本状态文件，只由脚本读写，**不要传给 AI**
- Anki 制卡按节（`#` 标题）追踪哈希；只有内容变动的节才送给 AI 判断，新卡追加到已有 record，旧卡不删除
- Anki 制卡默认不修改原 md；同步结果写入 `anki/records/`

## 自动化任务

**三套等价的"每日凌晨任务"**（同一逻辑，三个运行环境;Windows 侧 04:00,服务器 timer **01:00**）：

| 任务 | 运行环境 | 实现 |
|---|---|---|
| Windows 计划任务「Obsidian Anki 每日状态更新」(04:00) | 主项目本机 PowerShell | `scripts/daily_anki_status.ps1` |
| bwicarus-client 凌晨定时（0.9.32+,默认 04:00） | 客户端进程 | `_client/core/gui.py::_full_daily_pipeline` |
| **服务器 systemd timer `bwicarus-daily.timer`**（2026-05-14+,OnCalendar **01:00**） | Pi（VPS 该 timer 已随暂停 disable） | `scripts/daily_anki_status.py` (Linux) |

两边都跑：`ensure_alive → register_notes → anki_status → review_priority → 薄弱卡改写/已掌握换问法/质量体检(三个 server-config 开关各自控制) → build_review_deck → cleanup_orphans → export_dashboard → (可选)upload → AnkiWeb sync`。

**Step 0 ensure_alive 行为**：
- ping AnkiConnect `/version` — 通则直接进入下一步
- 不通 + `force_restart=True` → `taskkill /F anki.exe` → 启动 Anki → 轮询上线 ≤ 180s
- 主项目 ps1 用 `Ensure-AnkiConnect` 函数（force_restart 始终 True），客户端用 `AnkiClient.ensure_alive`

**「Obsidian Headless Sync」**：登录时启动 sync daemon，持续后台运行（不在 daily 流程内）。

**15 分钟轻量周期任务**（服务器 systemd timer，跟 daily 重型流程互补，不调 AI）：

| timer | 脚本 | 作用 |
|---|---|---|
| `bwicarus-quick-sync.timer` (每 15min) | `scripts/quick_sync.py` | vault 状态同步：不调 Anki / 不调 AI，只跑 `cleanup_orphans` 索引/record 部分 + KG `containing_notes` prune + **PDF 全文搜索索引增量**（`build_search_index.py`，FTS5 trigram，供 `/pdf/search` 全局搜索），让重命名/删除/新书在 15 分钟内反映到 KG / 仪表盘 / 索引 / 全局搜索 |
| `anki-sync-refresh.timer` (每 15min) | `scripts/anki_sync_refresh.py` | 拉手机复习数据：AnkiConnect sync 拉 AnkiDroid 等设备的复习记录，今日复习数有变化才轻量刷仪表盘（anki_status → review_priority → export_dashboard → 部署）。只读卡片状态，**不改牌组**、不写 frontmatter |

**state 备份**（ps1 和 Linux daily py 都做，`rotate_backups`）：每天备份 `state/note-states.json` 和 `anki/records/` 到 `state/backup/`，保留 7 天。Pi 另有独立的 `bwicarus-backup.timer`（03:30 跑 `scripts/backup_data.sh`，webapp/data + claude/state → `~/backups`，保留 14 份）。

**注意**：daily_anki_status.ps1 必须保持 **UTF-8 with BOM**（Windows PowerShell 5.1 调 `-File X.ps1` 默认按 GBK 解码无 BOM 中文 → "字符串缺少终止符"）。Edit / Write 修改后立刻补 BOM。

### 2026-05-26 register / KG / daily 调整

**register_notes.py 修复**（详见 [`references/skill-tree-system.md`](references/skill-tree-system.md)）：
- 根因：`process_note` 返回字典缺 `"note"` 字段 → main() 用 `r.get("note")` 过滤永远为空 → `update_kg_for_processed` 静默跳过 → KG 同步从来没真跑过
- 修复：result 加 `"note": str(note_path)`；main 兼容 fallback；subprocess 用 `python -u`；所有 print `flush=True`
- 自动补救：跑完校验 `KG._note_to_covered_l2` 是否含 processed 笔记，缺漏写 `state/pending_kg_sync.json`；`daily_anki_status.py::run_kg_link_mastery` 启动时读取 + `touch` 笔记 mtime 强制纳入本次 `link_with_ai --since-days 7`，跑完清空

**link_with_ai 关联校验规则**（防笔记跳着关联到深层 locked 节点）：
- AI 判定后对每个 (note, node) 校验：node `unlockable/mastered` → ✅；node `locked` 且前置 ≥ 50% mastered → ✅；否则 ❌ 写入 `KG._rejected_links`
- 幂等：每次都重新评估规则（节点 state 变了自动放行），笔记 hash 没变只刷拒绝时间戳不计入新拒绝数

**audit_kg 7d 配额上限**：daily 内 `--budget-target-7d` 从 88 → 60（控制夜间消耗）

**额度日志**：`state/quota_log.json` 记 daily 各步前后 quota 快照；控制面板「额度消耗日志」按钮 + modal 可查
