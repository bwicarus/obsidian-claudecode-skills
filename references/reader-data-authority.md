# 阅读器数据权威与三方矛盾的调节（2026-08-23）

## 这份文档回答的问题

用户 2026-08-23：

> 「从长远的多前端的各种额外功能加入的角度来看，把文件放在 pi 上进行维护是最
> 合理的，但是 ai 的运作环境放在 windows 上是最快的，而就使用的角度上，肯定是
> 直接回传到正在使用的前端是最快的，就没有什么方法可以调节这个矛盾么」

先说结论：**这三件事不是同一件事在抢一个位置，所以本来就不必互斥。**
矛盾感来自把「谁保管」「谁计算」「谁显示」当成了同一个「文件放在哪」的问题。
拆开之后各有各的最优解，而且**其中两条链在这个仓库里已经建好并且在跑了**。

真正缺的那块不是"再修一条到 Pi 的复制通道"——那条通道早就有。缺的是
**AI 写出来的东西从来没有获得身份**，所以它没有资格进入那条通道。

## 一、现状盘点（全部实测，不是设想）

### 1.1 Pi 上已有一套完整的同步网关

`_server_deploy/reader_sync_relay.py`，契约 `sync-gateway/2`。它不是草稿：

| 能力 | 位置 |
|---|---|
| 账号分区的持久中继 | 模块 docstring |
| 服务器游标（cursor） | 同上 |
| **显式版本冲突**（不是后写覆盖） | 同上 |
| 一致性恢复快照 | 同上 |
| 因果父状态 `record-parent-state/1` | `CAUSAL_CONTRACT`、`_causal_parent_matches` |
| owner 租约 `owner-lease/1`（30s TTL） | `OWNER_LEASE_CONTRACT` |
| tombstone 语义相等判定 | `_records_semantically_equal` |
| 注册表摘要闸（防两端集合定义漂移） | `_require_registry_digest` |

⚠ 它跟 `/pdf/api/sync-batch`（CommandOutbox）**是两回事**，注释里专门写了。
CommandOutbox 是"客户端离线攒写操作，回来批量提交"；这个是"多前端之间收敛"。

### 1.2 三个前端**都已经**接在上面

```
ios/BWReader/App/ReaderNativePiSyncBridge.swift      ← App
extensions/bw-reader-webext/background.js            ← 扩展
_server_deploy/static/reader-runtime/*.js            ← 网页/桌面运行时
```

三边共用同一份注册表摘要常量，改一边不改另一边会被 `_require_registry_digest`
当场拒掉——这个闸是好的，正是它保证了"多前端"不会各说各话。

### 1.3 但是只有 4 个集合真的复制到 Pi

本地 `data-registry.js` 里有 **30+ 个集合**（`document-notes`、`ink`、
`anchors`、`user-pages`、`document-highlights`、`reading-position`…）。

而进入 Pi 同步摘要的只有 **4 个**：

```
card-entities  card-states  user-settings  vocabulary-state
```

**便签（`document-notes`）不在里面。** 这一条很重要，见下节。

### 1.4 AI 推来的卡片是「短命的」

语音链上 AI 推一张卡走 `client_action → _vcCardPush → _cardPush`
（`rc-voicecall.js:5807`）。这张卡：

- 活在 `_cards.list` 里，是**纯 UI**；
- 只有用户**手动钉进页面**时，才经 `RC.stickynote.createHtmlAt` 变成便签
  （`rc-voicecall.js:5674`）；
- 而便签（`document-notes`）**不复制到 Pi**。

所以用户那句「不然我们的 Windows 和 pi 本地化的文件还有传输链路就没有被利用上」
**字面上就是对的**：AI 写出来的东西从生到死都没有变成一条记录，
自然也没有任何通道可走。

⚠ 顺带说明一个同名不同物，别混：`card-repository.js` 里的 "card" 是
**Anki 制卡**（front/back/cloze/deck/tags，Anki note id 只能出现在
projection receipt 里）。语音浮层那张 "卡片" 是 **UI 信息卡**（视频卡、
文字回复卡）。两者共用"卡片"这个词，但不是一个东西，**不要**因为
`card-entities` 已经同步就以为语音卡也同步了。

## 二、矛盾的拆解

用户说的三件事，各自的最优位置其实不冲突：

| 用户的诉求 | 它真正要的是什么 | 最优位置 | 现状 |
|---|---|---|---|
| 「文件放在 pi 上维护最合理」 | **长期归属**：多前端收敛、冲突可判、能恢复 | Pi `sync-gateway/2` | ✅ 已建成，但只覆盖 4 个集合 |
| 「ai 的运作环境放在 windows 最快」 | **计算就近**：AI 读写不要跨网等 | Windows | ✅ 已是如此 |
| 「直接回传到正在使用的前端最快」 | **呈现即时**：用户马上看见 | 直推前端 | ✅ 已是如此，且现在两端都有队列 |

会觉得矛盾，是因为默认了"数据只能有一份、放哪就在哪算、在哪算就从哪显示"。
一旦承认**一条记录可以同时有权威副本、计算副本、显示副本**，三者就各就各位了。

调节的办法就一句话：

> **写入沿"就近落地 → 直推显示 → 异步收敛"三段走；读取沿"就近优先 → 降级远端"两段走。**

前两段已经在 2026-08-23 的三个提交里做完了（见下）。这份文档要定的是第三段。

## 三、已经落地的部分（本次之前）

| 步骤 | 提交 | 内容 |
|---|---|---|
| 抖动不再误杀连接 | `a30b570e` | `.inactive` 宽限 12s；心跳连续失败 2 次才判死 |
| 写入先落地再送达 | `17350c83` | Windows 侧拿到租约后失败不再丢；`correlation` 去重 |
| 读与浏览器控制的注册等待 | `6030b22a` | 读路径比写更脆，补同样的等待 |
| App 通话链的反向队列 | `1c507c8d` | Pi relay 此前是**裸** `await bws.send`，断连连累整条工具结果 |

## 四、第 4 步的设计（本文档要定的东西）

### 4.1 目标

让 **AI 写出来的东西一出生就有身份**，从而能走已有的收敛通道；
并且在连不上时，Windows / Pi 的本地文件真的被用上。

### 4.2 记录形状

沿用仓库里已经在用的原语，不发明新的：

```
id           稳定标识，形如 {kind3}_{hex6}；AI 侧生成，一次生成一次身份
correlation  一次意图的去重键（重试/补投同 correlation 视为同一件事）
rev          单调递增版本；冲突由 sync-gateway 显式报出，不静默后写覆盖
actor        user | ai | system     ← 冲突判定的依据
parent       因果父状态摘要，复用 record-parent-state/1
deletedAt    tombstone，不物理删
```

`actor` 目前在仓库里只有零星使用（`assistant.py:6744` 有一处 `actor="user"`，
注释说"查询词是**用户想找的东西**，AI 只是执行者"）。第 4 步要把它变成
**记录级必填字段**。

### 4.3 冲突规则：按 actor 判，用户永远赢

```
user  vs  ai      → user 赢，ai 那版降级为一条"被覆盖"的历史，不丢
user  vs  user    → 按 rev / parent 走标准冲突，报给用户选
ai    vs  ai      → 后写赢（AI 自己的两版之间没有需要保护的意图）
system vs  任何   → system 最低，永远让位
```

理由：**AI 的写是提议，用户的写是决定。** 任何让 AI 覆盖用户编辑的规则，
哪怕只在极少数时序下发生，代价都远高于它省下的复杂度。

### 4.4 分阶段，先不动权威归属

⚠ 这是我对用户的承诺：**第 4 步不在这一轮把权威搬去 Pi。**

- **4a（可以马上做）**：给 AI 推的卡片一个稳定 id + `actor:'ai'`，
  在前端落进一个本地集合。此时权威仍在本地，Pi 不参与。
  收益立刻兑现：断连时不再"什么都没发生"，重连补投按 id 幂等。
- **4b（需要单独评估）**：把这个集合加进 Pi 同步摘要。
  ⚠ 加集合会改 `registryDigest` 常量，**三处必须同步改**
  （Swift / background.js / reader-runtime），否则闸会当场拒。
  这一步要先想清楚"AI 的草稿要不要跨设备"——很可能答案是**不要**，
  只有用户钉住的才值得跨设备。
- **4c（更远）**：便签（`document-notes`）是否进同步摘要。
  这是用户数据里最有价值的一类，但也是量最大的一类，要先量化体积。

### 4.5 读路径降级

读不需要强一致。顺序：**本地 → Windows → Pi**，任一层命中即返回，
并且**明确告诉 AI 这条数据来自哪一层**——否则 AI 会把"本地暂时没有"
当成"这东西不存在"，然后据此做出错误判断。这一条比它看起来重要：
`references/silent-failure-lessons.md` 里那十处，一半都是这个形状。

## 五、明确不做的

- **不引入新的复制通道**。`sync-gateway/2` 够用，再修一条只会产生
  "两条通道谁说了算"的新问题。
- **不把 CommandOutbox 改造成双向**。方向语义不同，混用会让离线写和
  多端收敛互相污染。
- **不做"最后写入者赢"**。它在单人多设备下看起来没问题，直到某次
  AI 的补投盖掉用户刚改的东西——那时已经没有历史可以恢复了。

## 相关

- 传输韧性三提交：`a30b570e` / `17350c83` / `6030b22a` / `1c507c8d`
- 静默失败的形状：`references/silent-failure-lessons.md`
- 便签规格：`references/sticky-notes-design.md`
- 收藏夹/插入页：`references/reader-userpages-favorites.md`
