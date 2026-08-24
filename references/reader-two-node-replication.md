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

## 8. 账本 = 同步操作本身

> 用户 2026-08-24 追认：「**需要两端同步的操作结果正好可以拿来做我们的纪录**，
> 其他部分按照你说的做」。

这句话把账本的地位说清了：不是"同步时顺便记一笔"，而是**同步流上的操作
就是记录本身** —— 一条命令既是复制的载荷，也是活动账本的一行。
两者共享同一个事实来源，不可能分叉。

服务端**接受命令的那一刻**落账本 `channel=mutate`
（`actor` / `client` / `device` / `{op, kind, target_id, before_hash}`）。

> 用户 2026-08-24 补充：「当然 **ai 读取时看到的不是原始的纪录而是处理过的版本**」。
> 这正是账本既有的两层结构：`raw-events.jsonl`（append-only 原始层，永不给 AI）
> → `import_raw()` → `events.db` 派生索引 → 画像/聚合摘要（AI 消费层）。
> mutate 记录进的是原始层；AI 看到的永远是派生层 —— 新增消费面时**别把
> 原始 jsonl 直接喂给模型**，那既费上下文又绕过了压缩/聚合设计。

Pi 侧已有同构实现（`reader_sync_relay.py::_ledger_sync_mutation`，
2026-08-24 建），Windows 侧照它做。

⚠ 沿用两条已踩过的规矩：**`src_key` 绝不能掺 client/device**
（会让存量账本二次入库、权重翻倍）；**白名单挡高频集合**，
表外集合每个出一次声。

## 8.5 ⚠ 两个必须先解决的硬前提（2026-08-24 查实）

规格定稿后做的可行性调查找出两条**在身份和粒度层面就卡死**的东西。
它们排在信封定稿之前 —— 不解决，后面每一步都建在流沙上。

### 前提 A：同一本书在两台设备上 bookId 不同

```swift
// ios/BWReader/App/ReaderLocalLibrary.swift:631
hasher.update(data: Data("reader-local-book-instance/1 ".utf8))
hasher.update(data: Data(libraryID.utf8))       // ← 每台设备一个随机 UUID
hasher.update(data: Data(relativePath.utf8))
hasher.update(data: Data(contentFingerprint.utf8))
return "localbook-\(digest)"
```

`libraryID` 是存在 UserDefaults 里的 `UUID()`，**每台设备各不相同**。
于是按书作用域的数据（高亮/便签/墨迹/插入页）跨设备**在身份层面就对不上号** ——
不是同步没接，是接了也认不出是同一本书。

⚠ 注意这个 id 的**当前语义是有意的**：它标识「这台设备上这个书库里的这本书」
（函数名就叫 `stableBookID`，注释写着重命名要保持身份）。所以不能简单去掉
libraryID —— 要么另立一个**跨设备的书身份**（`contentFingerprint` 已经在
输入里，它是内容指纹，天然跨设备），要么建立两者的映射。

**设计定稿（2026-08-24 四方向调查后，依据见
`references/book-identity-investigation-20260824.md`）：选映射，不选内容派生。**

- `contentFingerprint` 不能当锚：不但随插入页漂移，且两端各自插入页后漂向
  **不同**的值，永远无法重新会合；连本地书库自己都不拿它当身份
  （扫描路径匹配优先，指纹只做改名兜底）。已核实**插入页后 localbook-id
  其实不变**（路径没变就沿用旧记录）——前提 A 的问题只剩「跨设备」一层。
- 方案 = 推广仓库已有两处的 `ReaderLibrarySyncLink` 模式：
  1. **配对时铸 `replicationBookId`**（内容无关随机 GUID，铸后永不重算）；
  2. 两端各存持久链接表（结构照 `reader-library-sync-links/1`：
     本端书 id ↔ replicationBookId + lastSyncedSha256 合并基线）；
  3. 首次配对用**全文件 contentSha256**（不是采样指纹）做会合信号，
     配对完成即降级为仅存档；
  4. **摘要分叉不断链**（吸取 `verifiedNativeRemoteBookBinding` 的教训——
     它要求两端 sha 逐字节相等，插入页后 Pi 通道断到重新上传为止）；
     分叉期继续传命令，重放失败/基线对不上才升级为冲突；
  5. **监听身份断裂**：改名+改字节同扫描间隔发生会铸新 localbook-id
     （旧 id 消失、新 id 出现）——用 lastSyncedSha256/size 做一次性重配对，
     否则复制链静默断开。
- ⚠ Windows 侧今天**没有任何映射表**，且命令通道的 file 闸在 ≥4 处拒绝
  带冒号的引用（reader_bridge / bridge_client / rc-computer-voice 结果闸 /
  DirectContextSnapshot）——信封若带新形状书引用，先数副本再动。

### 前提 B：App 本地高亮是「整册一条记录一个大数组」，删除不留墓碑

```js
// _server_deploy/static/pdf/native-local-runtime.js:652
function stateId(kind) { return bookId + ':' + kind; }   // 整册一条
stores.document.put('native-' + kind, { id: stateId(kind), payload: clone(payload), … })

// :4904 删除
items = items.filter(function (item) { return item && item.id !== request.id; });
```

这跟 §3 的规格**直接冲突**：
- 用户要「写入 / 取消时**事件**同步」→ 现在取消一条只是本地少一条，
  传不出「删了什么」，网关的 remove+tombstone 无处可挂；
- 用户要「**增量**而非全量」「**独立更新**而非打包」→ 现在改一条 = 整册数组一次 put，
  两台设备改同一本书的不同高亮会撞成整条记录冲突。

**范式是现成的**：便签仓库已经是一条便签一条记录
（`document-note-repository.js:139` 的
`storageIdFor = 'document-note-v1:' + documentId.length + ':' + documentId + ':' + noteId`）。
高亮照这个拆即可。

### 顺带纠正规格里的一处错

§2.1 我把便签当成「现成的同步范例」。**便签今天也没在同步**
（`data-registry.js:162` 是 `provider:false` 且无 `sync`；Swift
`unsupportedDomains` 明写「便签」）。它示范的只是**记录形状与不透明信封**，
不是同步链路。

### 另外三条已知的闸（改一处不改其余 = App 同步整体 fail closed）

1. `scope==='global'` 判据有**三份副本**：`data-registry.isSyncCollection`、
   `sync-coordinator.js:189`、`native-sync-bootstrap.js:24` 的硬编码
   `SYNC_COLLECTIONS`（668 行逐字比对，不等即抛）。
2. `document-note-repository.js:581` **反过来**要求 `provider===false` ——
   原地把便签改成 provider:true 会让扩展便签整体 fail closed。
   高亮要复用做法只能**新开集合**。
3. document 库 `causalCollections: []`（`native-local-runtime.js:576`）——
   中继 `_inspect_causal_proof` 对没有因果证明的一律判
   `causal-proof-missing`，不补这处推上去全变冲突。

## 9. 落地顺序

1. 命令信封定稿 —— 2026-08-24 调查后草案修正：命令体原样用 command-outbox/2
   的 op `{mutationId, url, method, body}`；批次级带 `{contract, deviceId
   (设备族格式，不用 sourceInstanceId), book 身份(replicationBookId)}`；
   **cursor 不进发送信封**（游标由接收端落账时分配，发送端只在 pull 带自己的
   游标）；单帧 ≤256KiB 双端硬校验，一帧一命令；ack 沿用
   outcome∈{applied,replay,rejected}(+离线合成 queued) + bindOutcome 副结果
2. Windows 侧接收 + 存 + 分发（扩 `ReaderPC-Server`）
3. App 侧本地优先 + 命令入队 + 可达即推
4. 对账摘要
5. 账本埋点
6. 四个域依次接上（高亮 → 便签 → 插入页 → 墨迹）

## 进度

- [x] 规格定稿（本文件）
- [x] 前提 A/B + 信封的四方向调查（结论归档
      `references/book-identity-investigation-20260824.md`，设计已回写 §8.5/§9）
- [ ] **A 跨设备书身份**（前提，见 §8.5；设计已定稿，待实现）
- [ ] **B 高亮拆成一条一记录 + 墓碑**（前提，见 §8.5；改动面清单在调查归档方向三）
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
