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
