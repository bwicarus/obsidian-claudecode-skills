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
