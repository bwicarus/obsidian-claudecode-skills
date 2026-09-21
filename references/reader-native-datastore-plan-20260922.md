# 本机数据库：把存储从 IndexedDB 搬到 App 自己的沙盒 · 2026-09-22

> 用户 2026-09-22：「把数据库那边实现了以后出（包）」。
> 这份文件是动手前的形状确认 —— 先写清楚**要做的到底是什么**，因为形状错了
> 写一千行 Swift 也白写。

## 一、先看清现有分层（这决定了工作量）

翻代码之后发现分层比预想的好：

| 层 | 文件 | 行数 | 管什么 |
|---|---|---|---|
| **判据** | `reader-runtime/data-store.js` | 1415 | 记录构造、revision 校验、墓碑、causal 证明、批次规划 |
| **持久化机制** | `reader-runtime/indexeddb-store.js` | 1173 | 对象仓/索引/事务/journal 游标/mutation 记忆/裁剪/跨页广播 |

而且 `data-store.js` 把 `makePutRecord` / `makeRemoveRecord` / `assertExpectedRevision` /
`causalProofForParent` / `prepareBatch` 这些**全都导出了**。

**所以要做的不是"把 store 在 Swift 里重写一遍"**，而是：
换掉下面那一层的**后端**，判据原样留在 JS。

⚠ 这一点必须守住。把 `makePutRecord` 那套在 Swift 里照抄一遍，就等于让
「同一次写入该得到什么记录」有两个答案 —— 这种分歧只在两边真的都跑过同一条
数据时才暴露，而那时已经写进库里了。本项目反复吃这个亏（参见交接文件
「判据留在一处」那节）。

### 判据为什么不能搬进 Swift（2026-09-22 订正）

我一开始给的理由是「Chromium 上没有 Swift」和「扩展每次写入要多一次 native
messaging 往返」。用户退掉 Chromium 之后，第一条没了；查了代码之后第二条也**不
成立** —— 扩展现在只在两处找 App（网页显式请求 App 能力、取一次账户令牌），
它的日常写入根本不经过 App。

真正的理由是用户点出来的那条，比上面两条都硬：

> **扩展要能自己决定。** 它在网页上划线、查词、做标注时是**离线自洽**的 ——
> 不依赖 App 在不在、有没有登录。判据（这次写入该得到什么记录、rev 怎么涨、
> 墓碑怎么算）是它**本地决策的一部分**，不是可以远程请求的东西。搬进 Swift
> 就等于把「我能不能划这条线」变成一个需要问 App 的问题。

所以：**判据留在 JS 不是因为快慢，是因为自洽。** 数据层可以搬（App 的搬到
SQLite），但"怎么算一条记录"必须是两边手里各自都有的东西 —— 而那份东西只该有
一份源码，所以它在 `data-store.js`。

由此还定下一条：**扩展的数据不搬进 App 的 SQLite**。自洽就意味着它得有自己的
本地库；它跟 App 的关系是**按需同步**，不是共用一个库。

## 二、目标与不在目标内的

**做到**：
- 数据落在 **App 自己的沙盒**（SQLite 文件），不再是 WKWebView 的站点数据；
- **Swift 不开 WebView 也读得到**（这是"删掉旧层"的硬前提，也是 iCloud 同步
  真正可靠的前提）；
- 备份/迁移/排查都变成普通文件操作。

**不在目标内**（说清楚，免得被当成"做完了却没删掉旧层"）：
- **删不掉网页层**。调用方（document-host、card-repository、整个 runtime）和
  EPUB 正文仍是 JS。换存储买到的是上面三条，不是"WebView 可以拿掉"。

## 三、接口面

`createIndexedDBDataStore` 对外 13 个方法：

```
get  getMany  list  put  remove  batch  changes
migrateLegacyCausal  applyChanges  subscribe  instanceEpoch  status  close
```

底下真正碰存储的动作只有这些（四个对象仓）：

| 仓 | 键 | 读 | 写 |
|---|---|---|---|
| records | `collection\|id` | 单取、按 collection 列（按 `updatedAt,id` 排序） | put / 覆盖 |
| journal | `cursor` | 从某个游标起顺序读 | 追加、裁剪最旧 |
| mutations | `mutationId` | 单取（重放去重） | put、按 `rememberedAt` 裁剪 |
| meta | `key` | 单取 | put（游标、epoch、迁移标记） |

SQLite 表结构直接对应，四张表加两个索引就够。

## 四、事务怎么跨桥

IndexedDB 那边一次写入是「读当前 → 算新记录 → 写记录 + 写 journal + 记 mutation」
**在同一个事务里**。经桥的话这中间会有异步往返，事务保不住。

**不把事务拆开**：JS 先做一次**读**（当前记录 + 有没有重放过），在本地用
`data-store.js` 的判据算出新记录，然后**一次调用**把「记录 + journal 条目 +
mutation 备忘」整体交给 Swift 提交。并发用**乐观并发**兜底：提交时带上
`expectedRev`，Swift 在同一个 SQLite 事务里核对，对不上就拒绝，JS 重来一轮。

⚠ 这不是妥协出来的设计 —— `ifRev` / `assertExpectedRevision` 本来就在模型里，
乐观并发是它原生的语义。

## 五、怎么验（存储层不能靠"看文本"验）

**不新建 XCTest target**。仓库里已经有一条更合适的路子：
`ios/BWReader/Tests/{NativePDFCrop,JapaneseWordChain}/main.swift` —— 独立
`swiftc` 可执行文件，CI 直接编译并运行，不用模拟器、不用签名。
（2026-09-22 我差点另起炉灶加 XCTest target，看了一眼才发现现成的更好。）

为此 `ReaderNativeDataStore.swift` 必须能**单文件编译** —— 只依赖 Foundation
与 SQLite3，不 import UIKit/WebKit。这条约束对存储层本来就是好事。

JS 那侧的新实现（`native-store.js`）用一个**假的后端**在 node 里跑：
用例表尽量照搬 `indexeddb-store` 那套浏览器契约（87 条断言）的场景，
这样两个实现面对的是同一批问题。

## 六、分阶段（每阶段都能单独发版）

| 阶段 | 内容 | 能验到什么 |
|---|---|---|
| 1 | `ReaderNativeDataStore.swift`（SQLite）+ `Tests/NativeDataStore/main.swift` | 存储机制本身 |
| 2 | `reader-runtime/native-store.js`（13 个方法，判据复用 data-store）+ node 用例 | 语义与 IndexedDB 一致 |
| 3 | 桥：`bwNativeDataStore` 消息 + Swift 侧分发 | 端到端能通 |
| 4 | 在 `native-local-runtime.js` 里按开关选它；**默认关** | 不影响现有用户 |
| 5 | 迁移：把 IndexedDB 里已有的数据搬过来（一次性、可重入） | 老数据不丢 |

⚠ **出包的时机**：阶段 4 做完就可以出 —— 那时开关默认关，新存储随包发但不启用，
其余功能照常。真正切换（打开开关）应该在**装到设备上验过**之后，而不是
"全做完一次性切"。存储是唯一一类"错了就把数据弄没"的改动，不适合一步到位。

---

## 七、实际落地（2026-09-22 收尾）

五个阶段全部完成，外加两件**动手才发现**的事。

| 阶段 | 提交 | 备注 |
|---|---|---|
| 1 | SQLite 存储 + 15 组独立 Swift 用例 | 复合主键那个 bug 就是它抓出来的 |
| 2 | `native-store.js` | 判据全部复用 `data-store.js` |
| 3 | `bwNativeDataStore` 消息通道 + port | |
| 4 | 按开关选，默认关 | |
| 5 | 老数据搬家 | 翻页/对账/snapshotBaseline 三个坑 |

### 动手才发现的两件事

**① `applyChanges` 是开关的前置条件，不是"以后再说"。**
入站同步**只**走这个方法（sync-coordinator / direct-sync-protocol /
storage-router / 两个 repository 都调它，coordinator 还显式检查它在不在）。
`native-store.js` 原来没有它 —— 也就是说开关只要一打开，拉回来的东西一条也
写不进去。这不是测出来的，是把"谁在用这个 store"列一遍列出来的。

为此原生侧 `commitWithinTransaction` 的 `journalJSON` 改成可选：传 nil = 只写
记录和 mutation 备忘，不入队、不动游标。**journal 是"待发出"的队列**，把同步
拉回来的记录也塞进去就是回环：A 推给 B，B 原样再推回 A。

**② 开关够不着就等于没有。**
只认一个 `localStorage` 标记的话，iPad 上没有任何办法去翻它。现在是
App「阅读设置 → 存储」里的一项，documentStart 注入成
`window.__BW_NATIVE_DATA_STORE__`（**三态**：true / false / 没说 ——
只给 true/undefined 的话，App 里关着会被网页那侧的本地标记盖过去）。

### 三个刻意的决定，别回头改掉

**不搬老的 `instanceEpoch`。** 搬了，旧 checkpoint 就继续有效，同步以为"这库
已经同步过了"；万一迁移漏了什么，那份漏就被这句承诺永久盖住。不搬 → 新
epoch → checkpoint 作废 → 完整对账 → **漏掉的从服务器补回来**。
对账是这次设计里唯一的兜底，所以宁可多走一轮。

**迁移走 `applyChanges` 不走 `put`。** put 会把 rev 推高、updatedAt 换成现在，
整库都变成"刚刚改的"，同步会把它当成一库新改动推一遍。

**只有"搬家失败"这一处允许退回 IndexedDB。** 理由是那一刻老数据一条没动、
新库里也还没有用户写的东西，不存在"一半在这边一半在那边"。运行中的错仍然
不许静默回退 —— 那会造成两边单独看都自洽、合起来少一半，而且没人会发现。

### 搬家特有的三个坑（都长成「搬少了，而检查还说没问题」）

1. **翻页**。两边的 `list` 都是「缺省 200、上限 1000」。把一次 list 当成全部，
   表现是每个集合只搬走前 200 条 —— 而且旧库新库都只数到 200，**拿计数去校验
   根本发现不了**。按 id 续翻，不用 offset（offset 会随数据变动错位）。
2. **逐条对账**。每一条都必须落在 `applied` 或 `skipped` 里。比总数的话，
   "少搬了一批"和"两边都只数了一批"看起来完全一样。
3. **`snapshotBaseline`**。因果集合（卡片）的记录自带父版本证明，空库里没有父
   可对。少了这个开关，卡片一条也进不来，而且是**静静地全变成 conflicts**。

### 顺手修掉的一处静默数据劈裂

`maintainDeviceStoreOnBoot` 在新存储下**不跑**。它按名字删的是 IndexedDB 库、
重建出来的也是 IndexedDB store —— 在新存储上跑一次就把 device 偷偷换回
IndexedDB，而其余两个还在 SQLite。它本来是冲着 WebKit IndexedDB「旧版本页不
回收」那个病灶去的（2026-09-02 单库 16.47GB），SQLite 会复用空闲页，没这个病。
真要回收空间，原生侧的对应物是 `ReaderNativeDataStoreHost.resetDeviceStore()`。

### 还没做的

- **没在真机上跑过**。开关默认关，第一次打开要在设备上看着日志开。
- 桥 vs native messaging 的延迟实测（判据该不该搬 Swift 的那个问题，要数据）。
- iCloud 双设备真机验证。
