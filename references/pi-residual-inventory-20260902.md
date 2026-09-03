# Pi 残留盘点（2026-09-02，用户："查看下 pi 和 app 最近的通信往来，或许还有其他数据现在还留在 pi 上"）

结论一句话：**Pi 远没有退出。** App 每次开书仍向 Pi 打约 40 种接口，其中有 8 类用户数据的
**权威副本只在 Pi 上、并且今天还在写**（收藏词组、词汇掌握、查词日志、阅读停留、词典缓存、
助手对话/偏好、复习回执、语法跟踪）。书库那条线（09-02 已切到 Windows）只是其中一条。

## 一、接口层：iPad（100.127.156.60）09-02 打到 Pi 的接口（nginx access.log 聚合）

| 类别 | 接口（次数为 09-02 00:00–20:25 合计） | 说明 |
|---|---|---|
| 开书心跳 | `/pdf/api/ping` 808 | App 每次开书/切页探 Pi 可达性 |
| 页面数据 | `page-overlay` 727 · `page-vocab-marks` 622 · `page-nodes` 312 · `reader-events`(SSE) 304 · `char-offset` 24 · `book-figures` 25 | 生词下划线、KG 节点、事件总线全靠 Pi；`page-overlay` 清单里有本地分支但仍在打 |
| 注意力 | `read-dwell` 170 | 阅读停留上报 → Pi `state/attention/` |
| 词典/翻译 | `dict-quick` 419 · `translate-sentence` 39 · `translate-config` 31 | 词典 AI 生成在 Pi；翻译已三层（桥→直连→Pi 兜底） |
| 用户数据读写 | `favorites` 165 · `phrases` 42 · `phrase-mark` 35 · `vocab-mastery-map` 30 · `grammar-tracked` 49 · `grammar-books` 24 · `jp-vocab-mark` 2 · `review-answer/queue` 8 | **权威在 Pi** |
| 助手/语音 | `/api/assistant/voice-config` 121 · `history` 45 · `voice-cards` 36 · `prewarm` 18 · `pref-profiles` 25 · `action-prefs` 25 · `tool-prompt` 24 | 助手对话历史、偏好、语音卡片仍在 Pi |
| 预处理 | `library/ocr/status` 137 · `executors` 34 · `releases` 18 · `adoption-preview` 10 · `start` 2 · `attachments/...` 1 · `library/catalog` 35 | **Pi OCR 线仍在跑**（`state/reader-book-ocr` 20:25 还在写） |
| 登录 | `/login` 3 | |

清单口径：`native_reader_interface_manifest.json` owner=pi 共 109 条，runtime 有本地分支 10 条
（assistant/chat、voice-tool、active-reading、context-sync、epub-action、epub-assistant、
job-status、page-overlay、sync-batch、translate-sentence），**纯打 Pi 99 条**。

## 二、数据层：Pi 上仍在写 / 仅 Pi 有的数据

### A. 权威只在 Pi，且 09-02 还在写（迁移必做）

| 数据 | 位置（Pi） | 规模 / 最近写入 |
|---|---|---|
| **收藏词组** | `~/webapp/data/reader-sidecars/by-user/1/pdf-phrases.json` | 191 条 · 09-02 19:56（含今天的 トートマン/コチュジャン）。App 开书 GET 它填 `_phraseFavSet`，Pi 不在 = 词组全无 |
| 词组标记 | 同目录 `pdf-phrase-mark.json` | 08-14 |
| **词汇掌握/生词** | `~/claude/state/jp-vocab.json` | 1261 条 · 09-02 19:56 |
| 查词日志 | `~/claude/state/vocab-lookups.jsonl` | 4705 行 · 09-02 |
| 阅读停留/注意力 | `~/claude/state/attention/`（dwell.jsonl 4548 行、events.db 8.6M、focus.json） | 09-02 20:22 |
| 词典缓存 | `~/claude/state/dict-cache/` | 98M · 09-02 19:56（jp-/tr-/mw-/freedict-） |
| 助手对话/偏好 | `~/claude/state/assistant-convo`、`assistant-*-prefs.json`、`assistant-undo-log.json` | 对话 09-01 19:48 |
| 复习回执 | `~/claude/state/review-answer-receipts.json`、`review-answers-seen.json` | 09-01 |
| 语法跟踪 | `~/claude/state/grammar-tracked/`、`grammar-history/`、`grammar-cache/` | 06–07 月 |
| 收藏夹 | `~/claude/state/reader-favorites.json` + `reader-fav-*` | 07-09（1 条） |

### B. 权威已在 App 本地，Pi 只是中继副本（可当备份，已过期）

`by-user/1/` 下：`reader-positions.json`(08-23)、`reader-book-user-state/books/`(3 本，08 月)、
`pdf-ink/` `epub-ink/`(08-09)、`reader-userpages/`(08-09)、`reader-notes/`(08-08)、
`pdf-/epub-/html-highlights/`(07 月)、`assets/registry.json`(08-15)、`reader-outgoing-journal.jsonl`(3.9M，08-16)。

### C. 纯缓存 / 可重建（不用迁，Pi 停机即消失也无妨）

`pdf-page-img` 5.5G、`pdf-page-backups` 1.9G、`book-preprocess` 718M、`pdf-char-cache` 619M、
`backup-pdfs` 407M、`reader-book-ocr` 315M（Pi OCR 产物；PC 线已在）、`model3d` 272M、
`epub-extract` 195M、`pdf-compressed` 168M、`google-vision-ocr` 121M、`web-rescache` 111M、`kg` 108M。

## 三、建议的迁移顺序（Windows = 服务器，Pi = 纯备份）

1. **收藏词组**：App 本地为权威 + 镜像到 Windows；PC 预处理分词读 Windows 那份（用户 09-02 要求分词认收藏词组的前置）。
2. **词汇掌握 + 查词日志**：同上模式（`jp-vocab-mark` / `vocab-mastery-map` / `lookup-event`）。
3. **词典缓存**：App 本地登记（09-02 已做第一步）→ Windows 留底端点。
4. **阅读停留/注意力**：`read-dwell` 改投桥（桥已有注意力板 `HandleAttentionBoardAsync`）。
5. **助手对话/偏好、复习回执、语法跟踪**：随助手 CLI 搬桥一起走。
6. **页面数据三件**（`page-vocab-marks` / `page-nodes` / `reader-events`）与 `ping`：这几条决定了"Pi 不在时开书是什么体验"，要么本地化要么改指桥。
7. Pi OCR 线：PC 线已完整，可停 `book-ocr.service`，App 端"处理：Pi"那栏随之下线。

## 四、取证命令（下次复查用）

```bash
# 接口聚合（iPad IP 按需替换）
ssh pi "sudo awk -v ip=100.127.156.60 '\$1==ip {split(\$7,a,\"?\"); c[a[1]]++} END {for (k in c) print c[k], k}' /var/log/nginx/access.log | sort -rn | head -50"
# 7 天内仍在写的 state
ssh pi 'find ~/claude/state -maxdepth 2 -mtime -7 -type f | head -80'
# 账户级数据
ssh pi 'find ~/webapp/data/reader-sidecars/by-user/1 -type f -printf "%TY-%Tm-%Td %TH:%TM %8s %P\n" | sort -r'
```

## 五、当日决定与进展（2026-09-02 晚）

- 用户拍板：**Pi 的备份由 Windows 手动同步**，App 不再向 Pi 写任何用户数据；App 只在首次
  播种时从 Pi 读一次历史（收藏词组 191 条）。
- 已落地（待随 604 / 桥 0.1.269 / ReaderPC 0.1.114 发布）：
  - 收藏词组：App 设备库为权威（`phrase-favorites`），改动整表镜像到桥 `/reader-phrases`
    （经 Swift `/pdf/api/bridge-mirror`），落 `%LOCALAPPDATA%\BWReader\phrases.json`；
    PC 预处理分词读该文件，fugashi 切完后把命中的词组合并成一个 w。
  - 词典登记：设备库 `dict-cache` → miss 先问桥 `/reader-dict-cache` → 再 miss 才打 Pi；
    Pi 的成功结果同时推桥留底。
- 未动（仍打 Pi）：词汇掌握/查词日志、阅读停留、助手/复习/语法、页面数据三件与 ping、Pi OCR 线。
- **预处理线的真实形态**（09-02 晚核实）：PC worker（`scripts/reader_pc_preprocess_worker.py`）在 Windows
  上做识别与分词，但结果经 `/pdf/api/library/ocr/worker` **PUT 回 Pi**，由 Pi 发布 release、App 再从 Pi
  导入附件（文件头注释原话："The Pi remains the authenticated library/coordinator and the only publisher"）。
  所以「Pi PC 已完成」= Pi 协调、PC 执行；Pi 关机则预处理整条线停摆。Windows 本地的
  `%LOCALAPPDATA%\BWReader\pc-ocr-cache` 只是页图渲染缓存，不含字符层。迁移第 7 项因此不是"停 Pi OCR"
  那么简单，要把协调/发布搬到桥。
- 桥装完 ReaderPC 不复活的根因与恢复步骤见 `error-prone-code-removal-20260902.md` 末条。

## 六、整体搬迁（2026-09-02 深夜，用户："继续搬，全搬过去"）

做法不是逐条重写 99 条路由，而是**把 Pi 上那套 Flask 服务原样跑在 Windows**：

- 代码：`C:	mpeader-card-anchor-release`（与 Pi 同分支 `codex/card-anchor-release-20260820`），
  `_server_deploy/run_local.ps1` → 托盘守护 `local_supervisor.pyw` → `python app.py` 监听 `127.0.0.1:5000`；
  `.env.local` 在工作树根（本机新 SECRET_KEY，未搬 Pi 密钥；账号库 app.db 随 webapp/data 复制，密码照旧）。
  开机自启：HKCU Run `BwicarusLocal` → pythonw + local_supervisor.pyw（守护自己读 .env.local）。
- 数据：`~/webapp/data` → `webapp-data/`（30M）；`~/claude/state` 里用户数据与派生层（词汇/停留/词典缓存/
  助手/复习/语法/收藏/OCR 发布 315M/KG/FTS 索引…）→ `state/`。页图缓存等 10G 未搬（可重建）。
- 暴露：`tailscale serve` 把 `/pdf /api /login /logout /static /voice-rt /auth /profile /skilltree /insights
  /private /dashboard /control /register /history /admin` 前缀映射到 `127.0.0.1:5000`，与桥的 `/reader-*` 共存于
  `https://bwicarus-2.taile44d0c.ts.net`。Flask 自带 ProxyFix，认 X-Forwarded-Proto。
- 客户端：App 10 处 Pi 主机常量 → `bwicarus-2`（网关/登录面/同步桥/远端书库/Pi OCR/手表/语音）；
  ReaderPC `DEFAULT_PI_ORIGIN` → Windows（0.1.116）。App 换主机后需重新登录一次（同账号）。
- 仍待办：Pi 上的 `mcp-server`(8766)/`watch_*` 桥、spacy 语法常驻进程（Windows 未装 spacy-venv）、
  Windows→Pi 的手动备份脚本、验证 App 登录与预处理链路后停 Pi 的 webapp/book-ocr 服务。

### 六-2 独立服务与凭据（22:30 补记）

- Windows 上由 `scripts/windows_sidecar_services.py`（pythonw，HKCU Run `BwicarusSidecars`）托管四个独立服务：
  voice-rt :8767、watch-voice :8768、rbi :8769、mcp :8766；tailscale serve 已映射 `/voice-rt` `/rbi-ws` `/mcp`。
  日志在 `webapp-data/sidecar-<name>.log`。Flask 仍由 `local_supervisor.pyw`（HKCU Run `BwicarusLocal`）托管。
- spacy：工作树 `spacy-venv/`（spacy 3.8 + en_core_web_sm），`.env.local` 的 `SPACY_PYTHON` 指向它。
- 凭据：`~/.config/doubao-voice.json`、`openai-realtime.json`、`xai-grok.json` 已按用户要求（23:00）从 Pi 原样
  scp 到 Windows `C:\Users\bwica\.config\`（文件到文件，未读取内容）；语音中继重启后已加载。`watch-voice-token` 与
  `mcp-http-token` 在 Windows 本机新生成（手表经 App 重新领取即可；外部 MCP 客户端要换新令牌与新地址）。
- Pi 侧 `reader-context-push.service`（Pi→PC 推快照）已无意义（App 直接投桥），停 Pi 时一并 disable。
- 手动备份：`scripts/backup_windows_to_pi.ps1`（webapp-data + state + %LOCALAPPDATA%\BWReader 打包 scp 到
  Pi `~/backups/windows-server/`，保留 7 份）。

## 七、Windows 服务器的看护（2026-09-03 追加）

04:04 机器重启（用户侧发起）后，HKCU Run 里的 `BwicarusLocal` / `BwicarusSidecars`
**没有执行**（Shell-Core 日志里当次只跑了 BWAB-Visual / start-readerpc 等四项），服务器从
04:04 一直 502 到 10:16 才被手工拉起——App 端表现为「Pi 书库请求失败 (HTTP 502)」和
`BW_CARD_BOOTSTRAP_HTTP`。原因未定位（注册表项当时在、也没被「启动应用」开关禁用）。

现行看护（Run 项照旧保留，再加第二保险）：

- 计划任务 **`BwicarusServer`**（用户级，登录触发 + 每 5 分钟重拉，`StartWhenAvailable`）：
  两个动作 = `pythonw _server_deploy/local_supervisor.pyw`、`pythonw scripts/windows_sidecar_services.py`。
- 两个脚本都有单实例锁（`Local\bwicarus-local-supervisor` / `Local\bwicarus-sidecar-services`），
  重拉时已在跑就立即退出，不会起第二份、不会撞端口。
- 一眼看状态：

  ```powershell
  Get-ScheduledTaskInfo BwicarusServer | Select LastRunTime,LastTaskResult,NextRunTime
  curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:5000/login   # 应 200
  ```
