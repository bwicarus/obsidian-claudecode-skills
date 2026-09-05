# 易错代码清除清单（2026-09-02 用户拍板）

用户原话：「或许我们应该从实际代码层面把经常容易出错的代码去除」。这份清单来自
2026-08-31 → 09-02 三天的事故谱系：每一项都不是一个 bug，而是**同一类错误反复
出现**的结构。按"删了就不会再犯"的标准归类，记录处置与未做的原因。

| # | 结构 | 本周实锤 | 处置 |
|---|------|----------|------|
| 1 | 双坐标系：`__charBoxes` 按基线**重排**后数组下标 ≠ `_oi` 源序 | 词组合并匹配不上、全页 w 收集圈错、bind 校验误拒、选区夹带左列（四次） | 不改重排本身（相邻扩展、竖排漫画列处理都依赖视觉序），而是**让选区不再依赖数组顺序**（见 3）；所有跨行/词组匹配一律按 `_oi` 源序建流（`_applyPhraseMergesLocal`、`_phraseExpandFromChar`、`_charRangeRegionFilter`） |
| 2 | 全局一次性变量传状态 | `_pendingSelKeep` 调用方早退即泄漏到下一次选中（"选区与所点的词完全不相干"） | 已删；盘点其余 `_pending*`/`_last*`（位置恢复、点击计数）都是合法状态机，非一次性传参 |
| 3 | "产生→过滤"式选区：先取 `[起,止]` 闭区间，各消费者再各自过滤 | 每加一层过滤（块/keep/区域）都是补丁 | **选区改为一份字符集合** `_charSel.keep`（`_selByCharRange` 唯一产生），词组高亮/解释高亮/保存高亮 rects+text/OCR bbox/AI 快照/扩展宿主六路消费者一律按集合取；`startIdx/endIdx` 只留给句子/段落扩展等边界消费者 |
| 4 | 手抄常量：CJK 正则 10 处、0.6 行高判据 12 处 | 各处口径漂移风险 | **暂不收拢**：契约测试按函数名逐个抽源码进 VM 运行（`fnBody(name)` 清单），顶层常量/共享 helper 抽不进去会 ReferenceError；收拢=同时改十几份测试抽取清单，收益不抵风险。若做，先给契约加"自动跟随依赖"的抽取器 |
| 5 | 静默改数据的"医生"机制 | 自愈两次吃掉用户意图（重绑回滚、解绑捡回） | 自愈整套已删（含 manual 补丁层）。审查其余后台写 note 路径：`reconcileConsolidatedWordCard` 只覆写 content 且源自用户发起的整理（保留）；`__pageBindRetry/_pageBindPending` 只服务 AI 直绑的"页未渲染先存着"，不触碰用户解绑（保留） |
| 6 | 多副本白名单 | C# 真闸缺 `_nativeReaderWordCardsConsolidate`，MCP 整理投递两天全被拒，无测试红 | 补漏 + 契约 `client-action-whitelist-parity`（JS 派发 fn 集合 == C# 真闸集合）。硬表↔manifest 已有 packager 校验；vendor/拼合已在默认档；runtime 与 rc-computer-voice 按 kind 放行不点名 fn，不构成副本 |

## 判据沉淀

- **契约按函数名抽源码进 VM** 是这个仓库的硬约束：新增的公共 helper 若被抽取函数
  引用，必须内嵌为局部函数或加进抽取清单（`selection-highlight-line-split`、
  `selection-span-blocks` 两次实锤）。`selection-span-blocks` 还按函数头起 1600
  字符窗口钉调用点，内嵌辅助要放在主体之后。
- **"看似只差一处"的白名单**（2026-08-19 五连咬、09-02 再咬）：任何名单先跑
  `python3 scripts/contract_sites.py <name> bind` 数副本，再加跨站契约。
- **`wmic`/`taskkill` 才是这台机上查杀进程的可靠手段**；PowerShell 工具内嵌
  `$_.CommandLine` 转义会静默失败（09-02 三次误判"进程已结束"）。
- **用 `wmic … like '%pattern%'` 找进程再 taskkill 时，pattern 会匹配到发出这条命令的
  shell 自己**（命令行里就含那串字）——09-02 一条提交链把自己杀了。写成
  `like '%handoff[_]check%'`（WQL 字符类拆开字面量）即可避免自伤；且查杀与后续长链
  分成两条命令，别把查杀放在长链开头。
- **进度、"正在…"这类会持续变化的状态，不能放进 SwiftUI `.alert`**：弹窗文案在呈现
  那一刻定格，改绑定字符串不刷新，再赋值等于重新呈现——用户看到的是"百分比不动、
  关一次弹一次"（09-02 书库上传）。持续状态画在它所属的那一行/那张卡上，弹窗只留给
  一次性的真阻塞。
- **同一资源的长操作要单飞**：闸/按钮再次触发时等已有的那次，不要再开一路。桥日志里
  同一秒三路 `BW_LIBRARY_WRITE_FAILED`，来源之一就是连点「打开」并发上传同一本书。
- **Git Bash 会把以 `/` 开头的参数改写成 Windows 路径**：`tailscale serve --set-path /reader-library/download …`
  被改成 `/C:/Users/…/git/2.54.0/reader-library/download`，serve 表里多出一条永远命不中的映射
  （09-02 实锤）。给这类命令加 `MSYS_NO_PATHCONV=1`，但同一条命令里就别再用 `/dev/null`
  （它同样不再被转换，curl 会写失败、exit 23）。改完 `tailscale serve status` 看一眼路径原文。
- **装桥会把 ReaderPC 一起带走，而且它留下的是「用户主动退出」标记**（09-02 晚实锤，桥停了
  25 分钟没人复活）：桥安装器 `stop_direct_service` 杀掉 Direct 进程，ReaderPC 作为属主随之走
  `request_exit` 退出并写 `readerpc-user-exit.json`，看门狗此后每 5 分钟看到标记就"不复活"。
  恢复：删标记 → `wscript.exe //B start-readerpc.vbs logon`（该脚本见到已有进程就退出，要换版本得直接
  起新版 exe，它会接管旧实例）。根治待做：安装器装完应自己拉起 ReaderPC，或 ReaderPC 把"属下服务被外部
  停掉"与"用户点了退出"分开对待。
- **换 ReaderPC 版本要么让新版接管、要么全杀再拉，不能各做一半**（09-02 晚）：直接起新版 exe 会走
  「启动接管」，它要求旧实例**正常退出**；我在接管进行中强杀了旧实例，新版判定"旧 ReaderPC 未完成
  正常退出；拒绝强制接管"而不接管服务，桥随旧实例一起死。正确顺序：`taskkill` 掉**全部** ReaderPC
  实例 → 删退出标记 → `wscript.exe //B start-readerpc.vbs logon`（无实例时它会启动 current.json 指向的版本）。
- **按命令行关键字杀进程前先想"我自己的 shell 命令行里有没有这个词"**（09-02 第二次实锤）：重启 Flask 时按
  `app.py` 匹配，把正在等 CI 的后台 bash（其提交信息里含 "python app.py"）一起杀了。匹配要落到**进程名 +
  精确脚本路径**（如 `local_supervisor.pyw`），不要用会出现在自己命令行/提交信息里的泛词。

## 2026-09-03 追加两条

### 只在罕见路径上才会跑到的校验层，等于没测过
换服务器主机 → App 同步检查点归零 → 服务器合法地要求 resetRequired → App 走
`pushLocalSnapshot`。这条路径 Pi 时代**一次都没触发过**，所以 Swift 桥白名单把
`cursor` 当必填的错误潜伏了整个开发期，直到迁 Windows 才以 `BW_NATIVE_SYNC_PAYLOAD`
整批拒收的形态露头。两个教训：
- 白名单/校验层要对着**所有生产者**写（这里 `snapshotChange` 合成的变更天生没有 cursor，
  relay 的 `_normalize_change` 也从不读它），而不是对着最常见的那一种载荷形状写。
- 拒收必须带出"哪条、哪个键"（`payloadRejectReason`）。这次前四轮全靠猜，
  是因为桥只回了一个码，`ReaderPiSyncCoordinator` 的 JS 片段又把 message 丢了。

### 又一次把自己杀了（第三次）
`Get-CimInstance Win32_Process | ? CommandLine -match 'handoff_check|reader_contract'` 想清残留
node，结果 pwsh 自己的命令行也含这些字串 → 连自己和几个 bash 一起杀（exit 255）。
本文件上面已写过两次同型事故。**硬规则**：杀进程只按 `Name -eq 'node.exe'` 之类的
精确进程名 + 命令行里的**精确脚本路径**两条同时成立，且先 `Where-Object ProcessId -ne $PID`。
- **Git Bash 里 `taskkill /PID` 也会被路径改写**（09-03 实锤）：`/PID` 被改成 `C:/Users/…/git/2.54.0/PID`，
  taskkill 报"无效参数"，而且是 GBK 乱码看不出原因。用 `MSYS_NO_PATHCONV=1 taskkill /PID <pid>` 或写成
  `taskkill //PID <pid>`；同一条 09-02 的 tailscale 教训，换了个命令又踩一次。
- **装桥与 ReaderPC 保活赛跑**（09-03 实锤）：桥安装器先停 Direct 再原子替换 exe，而 ReaderPC 的保活
  在"属下服务不在"后立刻重拉 Direct —— 第一次安装就撞上：替换时 exe 已被新拉起的 Direct 占用，
  `WinError 5 拒绝访问`，安装器自动回滚；第二次原样重跑碰巧赢了赛跑就成功了。这不是可靠流程。
  **已根治（2026-09-05）**：维护标记 —— 安装器在**停 Direct 之前**写
  `runtime/readerpc-direct-maintenance.json`（`bridge_core.write_direct_maintenance_hold`，
  唯一一份约定，安装器经 importlib 加载同一个 bridge_core），ReaderPC 保活见标记就等着不重拉，
  事务三条出口（成功/失败/回滚）都在 `finally` 里撤标记。
  三条安全性质缺一都会变成"语音永久起不来、而现场看起来服务全开"：① 标记 300s 过期；
  ② 带写入方 PID，进程没了立即作废；③ ReaderPC 认标记时出声（footer + boot log，同一条只说一次，
  撤销也说）。`read_direct_maintenance_hold` 内部一律 `except Exception: return None` ——
  这个函数**绝不能把异常抛进保活循环**，抛了就把语音的自愈能力一起搭上了。
  回执里多一行 `maintenanceHold`：false 就是退回了赛跑，下次安装失败先看它。
  契约：`tests/test_bridge_core.py::DirectMaintenanceHoldTests`（过期/死 PID/垃圾载荷/TTL 上限）
  + `test_readerpc_launcher.py::…keeps_the_keepalive_off_the_installer`
  + `test_computer_voice_direct_package.py` 的事件序列（`hold` 必须先于 `stop`，`release` 收尾）。
  ⚠ 装桥前**先把 ReaderPC 升到会认标记的版本**（0.1.124+），否则标记没人看。
  实测验收（0.1.277 装在活着的 ReaderPC 上）：`serviceRestartDeferredToReaderPC=true`、
  `maintenanceHold=true`、日志 `01:34:42 Direct 维护标记在,保活暂不重拉` →
  `01:35:17 Direct 维护标记已撤,保活恢复`，Direct 随即重起。不再需要"先退 ReaderPC"那套。
- **ReaderPC 换代接管在窗口隐藏后必然失败**（2026-09-05 实锤，同一天两次"旧 ReaderPC
  未完成正常退出；拒绝强制接管"）：接管靠 `taskkill /PID`，那是**发 WM_CLOSE**，
  而 ReaderPC 收进托盘后没有顶层窗口（`MainWindowHandle = 0`），WM_CLOSE 无处可去，
  taskkill 仍返回 0（它只管"请求已送出"）→ 等 60s 超时 → 拒绝接管 → 新旧两代同时在跑
  （这次就出现过两代都活着、心跳停摆、Direct 掉线，只能精确 kill 收场）。
  "刚启动接管成功、隔一小时就失败"不是随机的：窗口那时还在。
  修法：补一条**带外**通道 —— 接管方先写 `readerpc-exit-request.json`
  （`readerpc_services.write_readerpc_exit_request`），运行中的实例在自己的刷新循环里
  读到就走正常退出路径 `request_exit()`，两条通道并用。安全性质同维护标记：30s 过期、
  带请求方 PID、认到时出声，**先删文件再退出**（否则下一代会再消费一次），
  接管完成后清残留（留着会让本代一启动就自杀，表现是"双击没反应"）。
  ⚠ 判据：**任何"请对方自己停下"的通道都不能只依赖窗口消息** —— 托盘程序的窗口是
  会消失的，而 `taskkill` 的返回码只说"送出了"，不说"有人收到"。
  契约：`test_readerpc_services.py::ReaderPCExitRequestTests` +
  `test_readerpc_launcher.py::…asks_out_of_band_before_it_asks_the_window`。
- **装桥还有第三个 racer：Codex 自己 spawn 的 MCP 宿主**（2026-09-05）。维护标记管住了
  ReaderPC 的保活，但 `[mcp_servers.reader_snapshot]` 的宿主是 **Codex** 起的
  （`required = true`，断了立刻重连重起），而 Windows 上运行中的进程锁住自己的镜像文件 ——
  替换窗口里任何一次 spawn 都让 `os.replace` 变成 `WinError 5`，连回滚都跟着失败
  （无害：exe 压根没换掉，状态仍一致，但整次安装白跑）。
  修法用一个 Windows 事实：**运行中的 exe 不能被覆盖，但可以被改名**。换不动就先把旧文件
  改名让位（`.superseded-<txn>`）再放新文件；已在跑的进程继续用改名后那份，新 spawn 拿新版本。
  安装目录里那一堆 `.running-*`/`.superseded-*` 说明这手法本来就是这套装置的旧习惯，
  只是 `_replace_install_payload` 一直没用它。让位后仍放不进去就把旧文件挪回来 ——
  **留个空位比留旧版本坏得多**（下次 spawn 直接找不到程序）。
  副产品：`Transport closed` 的来源也是这个 —— 装桥必然掐断 Codex 那条 stdio 连接，
  重试即可（无法避免：stdio 子进程活不过自己镜像文件的替换）。
- **自己 new 的 `JsonSerializerOptions` 在这个 exe 里会抛，表现是空 500**（2026-09-05，
  展示板首次真机调用）：`root.ToJsonString(new JsonSerializerOptions { WriteIndented = true })`
  抛 `JsonSerializerOptions instance must specify a TypeInfoResolver setting` ——
  这个宿主的序列化配置里反射默认是关的，而**不传 options 反而没事**（用的是自带 resolver 的
  `JsonSerializerOptions.Default`）。要缩进就用 `Utf8JsonWriter` + `JsonNode.WriteTo`，
  它不需要 resolver。
  ⚠ 真正贵的不是这个 API 细节，是**没有兜底 catch 时 ASP.NET 把它变成一个空 500**：
  使用方（程序/AI）看到的只有"失败"，现场零线索。加了一段 `catch (Exception)` →
  回 `{"ok":false,"error":"BW_BOARD_CRASH","detail":"<类型>: <消息>"}` 之后，
  一次调用就看见了真因。**每个对外端点都要有这段兜底**，这跟
  `HandleWidgetSystemDataAsync` 里那句"兜底留痕"是同一条规矩。
- **从 Pi 搬过来的 `state/server-config.json` 里还有四条 Pi 路径**（09-04 实锤，App 日志「服务器例句中译失败…no result」）：
  `ai.claude_cli.command=/home/bwicarus/.local/bin/claude`、`ai.codex_cli.command=/usr/bin/codex`、
  `qa_vault_path`、`qa_index_dir`、`qa_anki_records_dir`。Windows 上 `WinError 2` 被 translate.py 的 try/except
  折成"无结果"，App 里所有走 AI CLI 的例句中译从迁移那天起全部静默失败。已改数据（备份
  `state/server-config.json.bak-20260904`），代码侧 `ai_backends._resolve_exec` 现在按「配置路径存在 → `.env.local`
  的 APP_CLAUDE/APP_CODEX → PATH 按名找」解析，配置来自别的机器也能跑。
- **claude.exe 登录过期时 exit 0，并把 `Failed to authenticate: OAuth session expired…` 打到 stdout**（同日）：
  调用方当成正常回答 → 会被当译文/词条**永久缓存**。`ClaudeCli.chat` 现在识别这类文本为失败并落 Gemini 兜底；
  真正的修复要在 PC 上重新 `claude login`（AI 不能替用户做登录）。
- **`translate_model` 是 Claude 命名，切别的 AI 后端时不能原样塞过去**（09-04）：`ai_backend` 切到 `codex_cli`
  后 `_ai_translate` 把 `haiku` 传给 Codex → 「Codex 型号不可用」。现在非 claude 后端沿用 `server-config.ai.<backend>`
  自己的 model/effort（当前 codex_cli = gpt-5.5 / low）。09-04 起服务器 AI 后端 = codex_cli（用户拍板，CLI 的 Claude
  登录已过期未修）；改回去只需把 `state/server-config.json` 的 `ai_backend` 设回 `claude_cli`。
- **Codex 三处 Windows 化**（09-04，例句中译回「请先把句子发我」实锤）：① `_codex_exec_text` 把 prompt 当命令行参数传给
  `codex.cmd`，cmd.exe 在第一个换行处截断，空行之后正文全丢 → 改 stdin（`-`）+ `cmd.exe /d /c`；② `~/.reader-codex/home/config.toml`
  的 `[projects."C:\Users\…"]` 双引号键含反斜杠 = TOML 非法转义，app-server 一直起不来、每次回落慢的 exec → 改单引号字面串，
  且模板变了就重写；③ Gemini key 路径写死 `/home/bwicarus/.config` → 改 `Path.home()`，**Windows 上要把两把 key 文件放到
  `C:\Users\bwica\.config\gemini-api-key(-free)`**，否则默认走 Gemini 的路由（翻译/解释/词典的出厂默认）在 Windows 上静默返空。
- **例句中译改走 App 的「翻译 / 例句」路由**（同日）：`/pdf/api/translate-sentence` `backend=ai` 现在按 `assistant._AI_ROUTES["translate"]`
  + 用户在 App 设置面板里的覆盖（当前 Codex 5.3 Spark / low）翻，缓存 ns `route-translate`；不再经 translate.py 的全局 `ai_backend`。
  两条老教训：`_ai_call` 会把 `_READER_SYS` 抢占系统位，翻译这类要自带 system 的调用直接走 `reader_ask(system=…)`。
- **PC 预处理整本 0/80「直接无法使用」= 服务器页字段白名单没放行 worker 新字段**（09-04，与 08-19 同坑）：
  9 月 3 日给 worker 页加了 `tokenizeSchema`，`reader_book_ocr._normalize_pc_page` 的拒绝式白名单没同步，
  worker 传第 1 页就 400 `invalid-worker-page`，退避重试永不前进。`tests/test_reader_book_ocr.py` 里
  `test_pc_worker_sidecar_keys_are_all_allowed` 就是为它写的 —— **改 worker sidecar 字段必须跑这个测试**；
  白名单副本有两份（服务端 + 该测试的字段清单），`contract_sites.py` 口径。
- **判 CI 成败只看 `IOS_BUILD_NUMBER` 是错的**（09-04 实锤，向用户误报「625 已上传」）：那一行来自「Clean up signing material」，
  是 `if: always()` 的收尾步骤，编译失败也会打印。判定必须用 `gh run view <id> --json conclusion` 或 `gh run watch --exit-status`
  的退出码（别把它接进 `| tail`，管道会吞掉退出码），再 grep `UPLOAD SUCCEEDED`。
- **用正则批量改写 Swift 赋值语句会拆断多行表达式**（同日）：`ocrErrorMessage = X` → `reportPanelError(X)` 把
  `remote.errorMessage\n ?? "…"` 与 `ReaderPiOCRError.x\n .localizedDescription` 两处拆成了语法错误。批量改写后
  至少扫一遍"下一行以 `??`/`.`/`+` 开头"的调用点，或者干脆逐处手改。
- **主机名迁移只改了一半，语音静默断了好几天**（09-04 实锤）：提交 `6f528708`「Pi 整体退出」把 App 侧
  `DirectVoiceProtocol.swift` 的语音 Origin 改成了 `bwicarus-2`，但 **没动桥的来源白名单**
  `DirectBridgeServer.cs::SingleUserReaderOrigin`（仍是 Pi 主机名）。结果：语音 WebSocket 每次握手都
  `origin-denied`，App 每 1-2 秒重试一次全被拒；而 `/reader-*` 那些 HTTP 路由**不校验 Origin**，
  所以「所有服务都开着、状态都正常」，`readerConnected:false` 是唯一线索，极难看出。
  同一个主机名在仓库里有多份副本，当时 `DirectSnapshotPresentation.ReaderOrigin` 改了、这份漏了 ——
  又一次 CLAUDE.md「改白名单前先数清楚有几份副本」。
  **排障入口**：桥的安全日志 `~/bw-computer-voice-bridge/runtime/computer-voice-direct.service.err.log`
  里有 `{"event":"origin-denied",...}` 行；用 `curl -H "Origin: …" -H "Upgrade: websocket" …` 打
  Tailscale URL 可直接判 101/403（直连 127.0.0.1 会因缺 `Tailscale-User-Login` 恒 403，别拿它当判据）。
- **同一个「块号」有两套编号，而两边各自都自洽**（09-04 实锤，用户转述 Codex：「绑定出错不是他的问题是程序的问题」）：
  卡片 `bind.block` 的解析端按 `bk` 连号数块，而助手在结构化投影（vision 高置信 manga/table）里读到的
  `[NN]` 是 `region.order + 1`。同一本书里两套按页共存：没有版面信息的页面走
  `blockLines`/`segments[].block`（= bk 连号），有版面的页面走区域号。撞上的表现极隐蔽 ——
  助手说「第 11 块」，那页 bk 连号只有 8 块 → `search(11)` 空 → 退回全页按文本找 →
  没给 from 时距离恒为 0 → **页内第一处胜出**，卡片钉到另一处一样的字上。
  更糟的另一半：`[NN]` 当时**只有分镜网格那一条路在印**，散文页与表格页一个都不印，
  而 `ReaderCapabilities/cards.md` 写着「正文每一行形如 `[NN] …`」—— 助手只能自己数行号。
  **面向 AI 的说明写反/漏写比没写更糟**（CLAUDE.md 那条的又一例）：它不会追问，它会照着编。
  修法：解析端两套都试（区域号优先，命中就把 `ois` 写回 bind，下次直接 exact-set）；
  四条版面路径统一走 `appendLocalRegionLabel` 印 `[NN]`；说明改成「抄页面上印的那个号，
  没有 `[NN]` 就别给 block」。契约钉在 `tests/reader_contract/page-chars-bind.contract.test.mjs`。
  **随后做了根治**（同日）：上面那套只是止血 —— 只要两处各自推导同一个地址，就还会有
  下一次撞车。现在编号只有一个来源 `native-local-runtime::blockNumberer(layout)`
  （有版面用 `region.order + 1`，没有才退回 bk 连号），语音快照投影、`blockLines`、
  `segments[].block` 三个出口全从它取号。行为契约用一个**故意让两套编号相反**的夹具钉住
  （区域号把第二个字排成 `[01]`，bk 连号会把第一个字排成 `[01]`），改错立刻红。
  **判据**：一个面向 AI 的地址，只能有一处推导它、一处解析它。两处各自算出来的
  "同一个号"是这个仓库反复出现的最难查的错型。
- **stale-while-revalidate 的第一跳被客户端存成了终局**（09-04 实锤，ヘルスプロモーション「明明是从英文来的但没标英文」）：
  服务端 `lookup_jp` 命中旧版本词条时先秒回（内部标 `stale_pv`）再后台按新 prompt 重生成 ——
  这套本来是对的。错在**那一跳被三层客户端缓存当正式结果收下**（会话内存 / localStorage
  `rc-wordpop-dict-cache` / 设备库 `dict-cache` + 桥留底 `/reader-dict-cache`），
  于是升级好的词条**永远没人去取**，表现成「服务端明明改好了、App 上永远是旧的」。
  桥留底还会把毒化条目传染给别的设备。修法：响应里显式带 `stale`（逐字段重建的响应不加就传不出去），
  客户端见 `stale` 一律不缓存（下次点击再问一次，服务端仍是缓存秒回，用户感觉不到），
  已毒化的条目靠缓存键 v2→v3 一次清掉。**通用形状**：任何「先给旧的、后台升级」的机制，
  下游每一层缓存都必须能分辨这一跳是不是终局 —— 不然升级链在第一层就断了。
- **「response invalid」把三种情况折成一句，聊天记录停了同步却查不出原因**（09-04 实锤）：
  语音线程 `thread/read` 回 35.3 MB，撞破 `MAX_CODEX_RESPONSE_BYTES=32 MB`，而报错只说
  "Codex app-server response invalid"（空行 / 超长 / JSON 坏了共用这一句）。失败后
  `_structured_baseline` 保持 None → `should_read` 恒真 → 每 15s（失败退避）重来一次，
  app-server 每次都要整读磁盘上 **157 MiB** 的 rollout —— 既是「聊天记录一直没同步到 app」的
  直接原因，也是用户抱怨的持续读盘的一部分。测得的构成：`mcpToolCall` 占 90.7% 字节，
  而投影只取用户消息 + final_answer + 工具摘要，也就是九成流量本来就要丢掉。
  修法：上限提到 128 MB、报错带上真实字节数与是哪一种、大线程加读取冷却（≥24 MB 至少隔 15s）。
  规则复述：**折成布尔（或折成一句话）之前先把原始值报出来**（silent-failure-lessons 第 2 条）。
  **随后按用户口径做了根治**（2026-09-05，用户：「设置上限就好，比如最多到目前为止 100 条之类的，
  还有就是 app 有清空按钮不是么，按下后之前的聊天记录就不需要了」）——⚠ 别再去想"轮换语音线程"，
  那条已被否掉（会断掉语音的对话连续性）：
  - **投影只看线程末尾 100 轮**（`MAX_CODEX_PROJECTED_TURNS`）。侧栏历史本就是有界列表，
    更早的轮次既不补发也不参与"哪些已发过"的比对。⚠ 切片后 `turn_index` 必须仍是**绝对**下标 ——
    没有 id 的轮次拿 `turn-<index>` 当身份，从 0 重新数会让同一轮随线程增长换身份 → 重复发。
  - **`MAX_CODEX_TURNS` 从 5000 提到 200000**，只当畸形载荷天花板。一条恒定增长的线程总会越过
    任何固定轮次上限，拿它当闸门只是把同一次停摆推迟几十天。
  - **读不到结构化源就降级，不停摆**：退回连续性文件那条路（有界，10 条滚动窗口）继续发，
    `last_result["degraded"]` 说明原因，只损失"窗口滚掉的轮次还能补回来"这一项。
    配 60s 失败退避（`STRUCTURED_READ_FAILURE_COOLDOWN_SECONDS`）—— 原来失败后 baseline 恒为 None、
    `should_read` 恒真，于是每 15s 重读一遍整条线程，而聊天记录一条都发不出去。
  - 清空按钮不需要额外配合：`state["published"]` 是持久去重，清空后不会把旧轮次再灌回来。
  判据：**一个会无限增长的外部对象，不能用固定上限当闸门** —— 要么只取它的尾部，要么在读不到时降级；
  拿上限拦住它只会把停摆推迟到下一次。

## 2026-09-05 补：Claude Code 的 Bash 工具在 Windows 上会吞掉 heredoc 里一层反斜杠

- 现象：用 `python - <<'EOF'` 喂进去的 Python 源码，字符串里写了两个反斜杠，Python 收到的只有一个；
  写三个收到两个。表现是 Python 报 `SyntaxWarning: invalid escape sequence`，而写出去的正则 /
  Swift 键路径少一层转义 —— 契约测试里的 `\(` 变成"未闭合的分组"。同一天踩了三次。
- 判据：**凡是内容里带反斜杠的编辑（正则、Swift 的 `\(` 插值与 `\.` 键路径、Windows 路径），
  不要经 Bash heredoc 写文件**，改用 Edit / Write 工具（不经 shell）。heredoc 只用来跑不含反斜杠的脚本。
- 顺带：改测试时先想它钉的是"形状"还是"字面"。`dataAge(board.updatedAtMs)` 这种字面钉，
  实现一改就得跟着改；钉行为（"数据时刻取最新"）的写法更耐重构。

## 2026-09-06 补：换了"给模型看的依据"，所有拿它当"此刻状态"的工具跟着一起换了

- 现象：钉住快照上线后（模型默认读说完话那一刻的版本），取笔迹合成图回「没有可用合成图」，
  控制网页滚动会等满五秒才报没到位。原因：`reader_visual_image` 与 `reader_browser_control`
  的 before/after 校验也走 `BuildToolPayload()`，于是它们比对的是一份永远不变的钉住版 ——
  用户说完才画的圈不在里面，快照也永远不"前进"。
- 判据：**给模型看的和拿来行动的不是同一个问题**。改一个"默认依据"之前，把它的每个调用点
  按"这是让模型理解，还是让代码等待/动手"分两栏；后一栏一律读实时。这次六个调用点里有两个属于
  后一栏，其余（高亮/建卡/查页/练习纸）恰恰应该跟模型同一份依据 —— 模型对着钉住版挑的目标，
  写回就得落在那一页。契约按函数切片钉住，不用全局计数（全局计数把该读钉住版的也拦了）。
