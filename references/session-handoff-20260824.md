# 会话交接 2026-08-24 —— 新会话从这里接手

> 上一段会话极长（两天、60+ 提交、多轮用户拍板）。这份是**唯一入口**：
> 按顺序读完本文件与它指向的三份规格，再动手。
> ⚠ 本文写作时的教训之一就是"只凭摘要接手会理解错"——所以下面把
> **最容易理解错的地方**放在最前面。

## 0. 五件最容易错的事（先读这个）

1. **工作树不是 C:\claude。** 全部工作在
   `C:\tmp\reader-card-anchor-release`（分支 `codex/card-anchor-release-20260820`）。
   C:\claude 是另一条分支的旧 checkout，在那里"文件不存在"不构成任何结论。
   第一步永远是 `cd C:\tmp\reader-card-anchor-release`。
2. **当前主任务是"两节点复制"（Windows ⇄ App），Pi 不在这条链上。**
   规格：`references/reader-two-node-replication.md`——那是**已拍板的规格**，
   不是提案，每条都有用户原话。不要重新论证要不要做、走不走 Pi。
3. **传的是操作不是数据**（命令复制）。不要滑回"传状态快照"的方案——
   那个方向被用户明确否掉过（我曾写过一个整域快照的 `import_domain`，已丢弃）。
4. **两个硬前提还没做**（规格 §8.5）：A 跨设备书身份、B 高亮拆成一条一记录+墓碑。
   它们排在命令信封之前。**下一步就是消化 §2 说的那个调查结果，然后做 A。**
5. **同步操作本身就是账本记录**（用户原话「需要两端同步的操作结果正好可以拿来
   做我们的纪录」），且 **AI 只读处理过的派生层**，永远不读原始 jsonl。

## 1. 当前任务：两节点复制，进度在规格文件尾部

`references/reader-two-node-replication.md` 的「进度」清单是权威。
写作本文时的状态：规格定稿 ✅，前提 A/B 与步骤 1-6 全部未动工。

配套规格（读的顺序）：
1. `references/reader-two-node-replication.md` —— 主规格
2. `references/reader-data-authority.md` —— 冲突规则（user 永远赢 ai）与三方矛盾拆解
3. `references/activity-ledger-design.md` —— 活动账本；§0 是同步范围表（高亮/便签/
   插入页=事件同步、墨迹=静置后、阅读进度/选中=不同步）

## 2. ⚠ 有一个调查在飞，结果落在磁盘上

交接时刚发出一个四方向调查（前提 A 的身份生命周期、各表面书引用形状、
前提 B 的改动面与迁移、命令信封的既有约束）。**新会话不会收到它的完成通知**，
但结果在磁盘上：

```
C:\Users\bwica\.claude\projects\C--claude\fc5f8953-8bae-4c7e-a5db-c32382b13bec\subagents\workflows\wf_62af4db4-c76\journal.jsonl
```

每行一个 `{"type":"result",...}` 是一个 agent 的完整返回。**先读它再动前提 A**——
如果读不到或没跑完，就按同样四个方向重新调查（问题定义在
`...\workflows\scripts\book-identity-and-envelope-wf_62af4db4-c76.js`）。

已知的关键悬念（调查要回答的）：
- `contentFingerprint` 是纯内容派生（SHA256 of 格式+大小+头尾各256KB，
  `ReaderLocalLibrary.swift::sampledContentFingerprint`），跨设备一致——
  **但插入页会真的改 PDF 字节**，指纹会漂。跨设备身份锚在哪，等调查结论。
- Pi 下载的书另有 `book_<32hex>` + contentSha256 身份。

## 3. 已投递的东西（全部真机可用，不要重做）

| 内容 | 表面 |
|---|---|
| 块寻址 `{page, block, text}`（bind 序号/原文二选一 + block 限定；how 四值回执） | 三表面全部 |
| 网页端加卡片/锁定元素（网页字符层 web-textlayer/web-bind/web-pagetext） | 扩展（真机 22/22） |
| 书页正文印 `[NN]` 块编号 | App |
| 自建页：AI 读全(md)/改/删 + 撤销卡 + `46-a` 位置标签 | App/Pi |
| 生成物冷归档 `scripts/artifact_lifecycle.py`（默认 dry-run，**定时器未接，等用户拍板**） | Pi |
| 账本 v3：`client`/`device` 字段 + dwell 全链路 + mutate 渠道 + 白名单闸 | Pi |
| 同步中继账本埋点 `_ledger_sync_mutation`（Windows 侧将照它做） | Pi |
| 传输韧性：.inactive 宽限、心跳连败判死、两条链各一个断线队列 | 全部 |

部署状态：Windows 桥 **0.1.197** 已装；Pi 部署到 `91dcacda` 之后只有 docs 提交
（无需再部署）；TestFlight 最后一次构建成功。门禁：Linux `handoff_check.py --full`
errors=0（`test_web_notes_local.py` 是**已知 flaky**，约 1/5 超时，
判法=做对照组，见 reader-extension-handoff.md §9.1）。

## 4. 用户决定速查（详情都在规格里，这里只防误解）

- 两条路径：用户在 App 直接操作=本地立刻出结果绝不等网络；AI 在 **Windows**
  （不是 Pi！）操作=异步同步过来。同一条通道两个方向。
- 服务端角色不绑机器（将来 Mac mini 接得上），命令带来源节点 id。
- Pi 现有其它服务（nginx/Anki/vault/备份/KG…）**暂不迁**，别混做。
- 墨迹粒度=一页的笔画；静置一段时间不变才同步。
- 冲突：user > ai（AI 的写是提议，用户的写是决定）。
- 传输不用新建：App 已直连 Windows（`wss://bwicarus-2.taile44d0c.ts.net/...`，
  Windows 的 Tailscale 名是 **bwicarus-2**，Pi 是 bwicarus）。

## 5. 操作纪律（这两天用真代价换的，新会话照守）

1. **改闭集清单前先数副本**：`python3 scripts/contract_sites.py <名字>`；
   bind kind 白名单有 17 份副本，最后选择是"不加 kind"。
2. **重建式处理器只放行不搬字段 = 静默丢弃**（dwell/active-reading 都是逐字段
   显式搬的）。放行了必须搬。
3. **heredoc 会吃掉 `\n` 的反斜杠**——往 JS/测试文件里写含转义的内容一律用
   Write/Edit 工具，别用 bash heredoc + Python 字符串（栽了三次）。
4. **写代码引用符号前先 grep 确认存在**（一天里写过 4 个幻觉 API，全靠核实抓回）。
5. **断言"符号出现过"没用，要断言"真的被用上"**（变异验证两次抓到这个形态）。
6. **测试写完跑变异验证**；真机红了先做对照组分清是代码错还是断言错。
7. **长跑命令一律 `timeout N`**；工具层超时**不保证杀进程**（有过 node --test
   孤儿跑 5h41m）。清孤儿前先抓命令行，别误伤 BWAgentBridgeLite/codex/mcp 常驻。
8. `src_key` 哈希绝不能掺 client/device（存量账本会二次入库、权重翻倍）。
9. 投递三表面互不相干：`python3 scripts/where_does_this_file_go.py --since <ref>`；
   路由归属先 `python3 scripts/where_does_this_route_run.py <路由>`。
10. iOS CI 预授权直接触发：`gh workflow run safari-extension-ios.yml --ref <分支> -f upload=true`。

## 6. 其它未完线头（不急，别丢）

- 生成物冷归档要不要接定时器、多久一跑 —— **等用户看过报告拍板**。
- 账本 `device` 字段的采集端未接（接哪个标识等用户定）。
- 活动台账 §3.2 的覆盖面缺口：用户手动删高亮/便签走 App 本地路由不出网，
  Pi 侧记不到 —— **两节点复制落地后自然补上**（Windows 服务端接受命令时记）。
- `ownerRole` 词汇表将需要为 AI 运行环境加一个值（别叫 windows，将来可能是 Mac mini）。
- 高亮/便签/墨迹/插入页今天在 App 上**没有异地副本**（backup_data.sh 只备 Pi）——
  两节点复制落地前，这个风险一直在。
