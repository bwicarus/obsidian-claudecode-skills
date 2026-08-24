# 阅读器数据的两节点复制（Windows ⇄ App）

> 2026-08-24 用户拍板。这份是**规格**，不是提案 —— 下面每条都有用户原话支撑。
> 实现进度见文末。

## 1. 一句话

**Windows 和 App 各存一份完整副本；改动以「操作」的形式在两边传播；
对方不在时排队等它回来。**

用户原话：

> 「windows 上 ai 写入的内容如果 app 未活跃就暂存等待 app 活跃时更新 app 的内容，
> app 上我手动进行的写入等如果服务器未活跃就等待服务器活跃时传入，
> 基本的内容在 windows 和 app 上都有相同的一份，冲突时就按照你之前那个思路进行就好」

## 2. 为什么是这个形状

### 2.1 传的是操作，不是数据

> 「这些同步都是事件性的，也就是说同步过去的不是处理好的数据而是处理本身，
> 处理过程在远程的端口进行」

命令复制，不是状态复制。仓库里已有这个形状的先例：`command-outbox/2` 的
`{mutationId, url, method, body}` —— 服务端用 `EnvironBuilder` +
`full_dispatch_request` **在进程内重放那次请求**，走正常路由
（`_server_deploy/pdf_reader.py::pdf_api_sync_batch`，注释原话
"Dispatch an owner-bound command-outbox/2 batch **through normal routes**"）。

**这一条顺带化掉了高亮的历史阻塞。** 注册表里 `document-highlights` 标着
`status: 'pending'`，理由是「先保持三种格式原 sidecar，**不改写锚点**」。
传状态时两端必须能解释同一个锚；**传命令时不必** —— 传过去的是
「在这段文字上建一条高亮」，每一端用自己的格式去解析和落库。

⚠ 但命令复制有一样东西是状态复制自带、它没有的：**状态复制会自我纠正**
（每次传完整状态，错了下次盖回来），**命令复制不会** —— 某一端漏执行或
执行结果不同，两边就永久分叉且无人知道。所以必须配**定期对账**，见 §6。

### 2.2 两条路径，延迟契约不同

> 「或许应该分为两条路，我本地直接操作需要实时出结果的，
> 还有 ai 在远程用他本地文件进行需要同步过来的」

| | 你在 App 上直接操作 | AI 在 Windows 上操作 |
|---|---|---|
| 权威写在哪 | **本地，立刻** | Windows |
| 延迟要求 | **绝不能等网络** | 异步到达即可 |
| 账本标记 | `actor: user` | `actor: ai` |

**同一条通道，两个方向。** 要区分的是来源和延迟契约，不是传输。

### 2.3 Pi 不在这条链上

> 「ai 远程操作现阶段不应该是 pi 而是 windows 上，因为现在 ai 运行在 windows 上，
> 未来可能会加入 macmini 这样的设备…但现在还没到那一步」
>
> 「不如现在开始直接把 pi 从链路中去除直接改为 windows 服务器和 app 客户端模式，
> 之后稳定了直接移植到 macmini 或者其他 windows 基站上」

这跟 CLAUDE.md 里 Pi 的定位一致（「① 把 CLI 调用抽象成 AI 的 API
② 设备间同步中继」—— **不是业务逻辑所在地**）。AI 既然跑在 Windows，
Pi 插在中间就是多一跳，而且正是它制造了「同一份数据两个真源」
（桌面写 Pi 的 sidecar、App 写本地）。

⚠ **服务端角色不绑机器**：命令带来源节点 id，任何节点都能产生命令。
将来换 Mac mini = 换一台跑同一个角色的机器，协议不动。

**Pi 暂时保持现状不迁** —— 它还在跑 nginx HTTPS/证书、Anki headless、
vault 同步、每晚备份、KG/看板/健身、book-OCR 等；那是独立的迁移工程，
跟本设计混做会互相拖住。

## 3. 同步范围（逐条拍板）

| 域 | 同步？ | 时机 |
|---|---|---|
| **高亮** | ✅ | **事件**：写入 / 取消时 |
| **便签** | ✅ | 事件（**内容和绑定都要**） |
| **插入页** | ✅ | 事件 |
| **墨迹** | ✅ | **静置后**：一定时间不变动才同步。记录粒度 = 一页的笔画，不是一笔 |
| 阅读进度 | ❌ | 用户明确不需要 |
| 选中 | ❌ | 临时行为 |

⚠ **别把「选中」和「高亮」混为一谈**：选中是高频、临时；高亮是离散、持久。
（我在推演中混过一次，用户纠正：「高亮写入或者取消时是需要事件同步的…
只是选中这种临时的行为不同步」。）

## 4. 传输：已经存在，不用新建

App 与 Windows **今天就在直连**：

```
Direct 桥端点  wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1
Windows 的 Tailscale 名  bwicarus-2.taile44d0c.ts.net   （Pi 是 bwicarus）
```

Tailscale MagicDNS + WSS + 配对认证 + 断线队列，**每天用语音就是走这条**。
Windows 侧的 `ReaderPC-Server` 已有服务壳与自启配置。

所以「App 怎么找到 Windows」不是待解问题。

## 5. 两个队列（都已有一半）

| 方向 | 对方不在时 | 现状 |
|---|---|---|
| Windows → App | Windows 暂存，App 活跃时补投 | ✅ `ReaderRealtimeOutput.cs` 的 outbox（2026-08-23 建）：先落地再投递、按 `correlation`/`cid` 幂等 |
| App → Windows | App 暂存，服务端活跃时推 | ⚠ **待建**。`command-outbox/2` 有形状但今天是「客户端 → 服务端离线队列」，不是跨设备通道 |

⚠ 补投的三条既有规矩（Pi 反向队列踩出来的，直接沿用）：
1. **只有带稳定 id 的操作能重放** —— 判据是"接收端能不能去重"，
   不是"这个操作重不重要"；
2. **补投前先删记录再投**，不是投完再删 —— 崩在中间宁可丢一次，
   也不要让同一批每次重连都涌出来；
3. **补投排在握手完成之后** —— 早发等于又丢一次。

## 6. 对账（命令复制的必需品）

命令复制不会自我纠正，所以要定期比对两边的状态摘要。

现成材料：`reader_book_user_state.py` 的每域 `digest` + `revision`
（`DOMAIN_NAMES` 已含 highlights / ink / notes / user-pages）。
比对不一致 → 出声，并允许一次显式的整域重同步。

⚠ **不一致必须出声**。静默分叉是这套机制唯一的致命伤。

## 7. 冲突

按 `references/reader-data-authority.md` 定的规则：

```
user  vs  ai      → user 赢，ai 那版降级为一条"被覆盖"的历史，不丢
user  vs  user    → 按 rev / parent 走标准冲突，报给用户选
ai    vs  ai      → 后写赢
system vs  任何   → 最低，永远让位
```

理由：**AI 的写是提议，用户的写是决定。**

## 8. 账本

服务端**接受命令的那一刻**记一条 `channel=mutate`
（`actor` / `client` / `device` / `{op, kind, target_id, before_hash}`）。
用户原话：「在同步发生时顺便进行记录」。

Pi 侧已有同构实现（`reader_sync_relay.py::_ledger_sync_mutation`，
2026-08-24 建），Windows 侧照它做。

⚠ 沿用两条已踩过的规矩：**`src_key` 绝不能掺 client/device**
（会让存量账本二次入库、权重翻倍）；**白名单挡高频集合**，
表外集合每个出一次声。

## 9. 落地顺序

1. 命令信封定稿（`{mutationId, origin, cursor, url, method, body}`）
2. Windows 侧接收 + 存 + 分发（扩 `ReaderPC-Server`）
3. App 侧本地优先 + 命令入队 + 可达即推
4. 对账摘要
5. 账本埋点
6. 四个域依次接上（高亮 → 便签 → 插入页 → 墨迹）

## 进度

- [x] 规格定稿（本文件）
- [ ] 1 命令信封
- [ ] 2 Windows 服务端
- [ ] 3 App 队列
- [ ] 4 对账
- [ ] 5 账本
- [ ] 6 四个域

## 相关

- `references/reader-data-authority.md` —— 冲突规则与三方矛盾的拆解
- `references/activity-ledger-design.md` §0 —— 同步范围表
- `references/reader-computer-direct-bridge.md` —— Direct 桥与两个队列的传输韧性
- `references/silent-failure-lessons.md` —— 静默分叉为什么是这套机制的致命伤
