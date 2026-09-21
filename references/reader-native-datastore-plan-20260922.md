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
