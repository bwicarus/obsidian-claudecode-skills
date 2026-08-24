# 前提 A/B + 命令信封 四方向调查结论（2026-08-24）

> 两节点复制（`references/reader-two-node-replication.md`）动工前的调查归档。
> 4 个并行 agent 全成功，**每条事实都带 文件:行号 且全部 verified**（无 unsure，
> 核实轮为空）。本文是消化后的版本；决定性结论已回写进主规格 §8.5。
> ⚠ 行号是 2026-08-24 分支 `codex/card-anchor-release-20260820` 上的，会漂移，
> 以符号名为准。

## 方向一：书身份在内容变化时的生命周期（前提 A 的核心风险）

**结论：三个候选锚点没有一个能直接当跨设备身份。选「配对时铸内容无关 GUID +
每设备存持久映射」，即把 `ReaderLibrarySyncLink` 的既有模式推广到 Windows⇄App。**

关键事实（全部 verified）：

1. **路径匹配优先于指纹**：本地书库扫描 `previousByPath[normalizedPath] ??
   uniqueRenameMatch`（`ReaderLocalLibrary.swift:533`）——插入页改了字节、
   指纹变了，只要 relativePath 没变，localbook-id **保留不变**。指纹匹配只做
   「路径消失 + 全库唯一 + 旧路径文件已不存在」的改名兜底（:520-532）。
   连本地书库自己都不拿指纹当身份。
2. **PDF mutation 前后 bookId 完全不变**：journal 强制 `localBookId === bookId`
   （`native-local-runtime.js:6286`），Swift 侧 guard `book.record.id ==
   request.localBookID`（`ReaderNativePDFMutation.swift:186`）。七类 App-owned
   记录键 `stateId = bookId+':'+kind` 不动，只有 payload 里的页锚原地迁移。
3. **20 个页锚域有逐域策略表**（`native-local-runtime.js:441-468`）：
   6 个 local-migrate、12 个 pi-preserve-*、2 个 native-ocr-migrate。
4. **摘要严格相等当门闩的教训**：`verifiedNativeRemoteBookBinding` 要求本地全文
   SHA == Pi 目录 contentSha256（`ReaderRemoteLibrary.swift:779-784`），插入页后
   绑定即 nil、联网页锚写入被拒直到重新上传。**复制通道不能沿用这个门闩**——
   插入页操作本身就是一条要复制的命令，对端重放后摘要自然重新会合；
   摘要分叉期必须继续传命令，只在重放失败/基线对不上时才升级为冲突。
5. **现成范式**：`ReaderLibrarySyncLink`（`ReaderRemoteLibrary.swift:535-547`，
   UserDefaults 键 `reader.library.syncLinks`，schema `reader-library-sync-links/1`）
   持久化 (localLibraryID, localBookID, remoteBookID, lastSyncedSha256)。
   链接按 id 复活、两端摘要分叉后仍存活；lastSyncedSha256 做三方合并基线
   （localNewer/piNewer/conflict 三态，:901-916）；无链接时按 kind:size 候选 +
   全 sha 相等做**首次配对**（:844-871）——配对后身份靠链接，不再看内容。
6. **user-state 409 用的是全文件流式 SHA256**（`pdf_reader.py:4692-4699` ←
   `reader_book_library.py::_stable_sha256`），不是采样指纹。App 记录里两级摘要：
   采样 contentFingerprint（扫描必算）+ 完整 contentSha256（惰性、指纹/大小/mtime
   任一变即失效清空，`ReaderLocalLibrary.swift:533-538,577-624`）。
   跨设备内容配对现有代码用的都是**完整 sha**。
7. **身份断裂边界**：改名 + 改字节在同一扫描间隔内发生 → 路径、指纹匹配双失败 →
   铸新 localbook-id，旧身份丢失（`ReaderLocalLibrary.swift:520-549`）。
   **复制映射表必须监听这种断裂**（旧 id 消失、新 id 出现），用 lastSyncedSha256/
   文件 size 做一次性重配对，否则这本书的复制链会静默断开。
8. Pi 的 `book_<32hex>` 生命周期与 localbook 同构（路径优先、字节变 id 不变、
   contentSha256 原地更新，`reader_book_library.py:344-394`），id 是 uuid4 随机数，
   **Pi 实例私有**。

## 方向二：各表面今天用什么字符串指代一本书

**结论：没有任何一个字符串能同时被两端直接解析；信封的 book 字段做成多元组，
两端各自用自己的映射表解析。**

| 表面 | 书引用形状 | 出处 |
|---|---|---|
| App 内部 documentId | `localbook-<64hex>`（掺每设备随机 libraryID → 前提 A） | `ReaderLocalLibrary.swift:631-647` |
| App 页面/快照/助手上下文 | `localbook:localbook-<64hex>` | `ReaderLocalRuntimeServer.swift:1166`、`native-local-runtime.js:346,653` |
| Pi sidecar | vault 相对路径（`pdf-highlights/<sha1(rel)>.json`）；另有 `vbook:<gid>`、`web:<url>` 前缀命名空间 | `pdf_reader.py:2921-2948,3164-3189` |
| Pi 书库 | `book_<32hex>` ↔ rel ↔ contentSha256，存 `state/reader-book-library/catalog.json` | `reader_book_library.py:236,374` |
| Windows AI 命令通道 | **只能是 vault 相对路径**——三处闸把含 `:` 的 file 整条拒掉 | `reader_bridge.py:290-294`、`bridge_client.py:474-482`、`rc-computer-voice.js:992-1008` |
| Windows 直连快照 | App 的 file 原样透传（即 `localbook:...`；拒 `vbook:`） | `DirectContextSnapshot.cs:588-592` |

三个缺口：
1. **从未与 Pi 交换过的纯本机书没有任何跨设备身份**——两节点复制必须规定
   「首次同步即注册身份」（配对即建链）。
2. **Windows 节点今天没有任何本地映射表**，只透传字符串；若信封带新形状的
   书引用，`reader_bridge` / `bridge_client` / `rc-computer-voice` 结果闸 /
   `DirectContextSnapshot` **至少 4 处冒号闸**要同步放行——先
   `python3 scripts/contract_sites.py` 数副本。
3. `syncLinks` 在 UserDefaults 而非可导出账本；若对端要推算映射，
   需纳入同步负载或改存 document store。
4. 另：Pi 服务端认识 localbook 的字面形状（正则校验）但从不尝试映射
   （`assistant.py:210,5817`）；manifest 的 `routes[].remoteBook.identities`
   已是「哪些请求字段是书引用」的机器可读清单。

## 方向三：前提 B 的完整改动面（高亮拆一条一记录）

**前提 B 属实且改动面已穷尽。** 现状：collection `native-document-highlights`
里 `id = bookId+':document-highlights'` 一条记录整册大数组；删除 = 数组过滤，
无墓碑（`native-local-runtime.js:652-668,4904`）。

七组数组形状消费点（改动面清单）：
1. 路由四分支 GET/POST/PATCH/DELETE（`native-local-runtime.js:4784-4953`）；
2. 助手直接高亮三记录同事务（highlights+undo+ops 整数组重写 + rev CAS，:4803-4881）；
3. **助手 undo 栈把整数组记录 rev 当 expectedRevision**（:9944-9950,10017-10023）——
   拆 per-item 后这个「整集修订号」契约是最大隐性改动；
4. Windows⇄App user-state 导出/导入按整数组（域摘要 = canonicalJSON(整数组) 的
   sha256；导入 = 整数组覆盖写，:1130-1134,1526-1546）；
5. PDF 插入页迁移（journal 携带整 before/after 数组，:6200-6274）；
6. 助手 authority snapshot + reader_query 工具（:8320-8340,12080-12086）；
7. 命令出箱回放白名单 `/pdf/api/highlights` POST/PATCH/DELETE（:11538-11550）——
   **只要路由语义不变此处零改动**。

基础设施已就位：
- indexeddb-store 原生支持 `deleted:true` 墓碑 + tombstone-dominates 防复活 +
  ifRev CAS + mutationId 幂等（`indexeddb-store.js:526,589,925-926`）；
- 照抄范式 = `document-note-repository.js:139` 的
  `'document-note-v1:'+documentId.length+':'+documentId+':'+noteId`（目前只被
  扩展 background 用，App runtime 未用）；
- 迁移先例两个：`bootstrapLegacyCards/importLegacySnapshot`（整包→per-item，
  missingOnly + 固定 mutationId 幂等）和 `recoverNativePDFMutationOnBoot`
  （journal 五阶段，beginBoot 恢复）。`METADATA_SCHEMA` 这个东西全树不存在。
- 每条高亮已有稳定 id（本地 `c_`/`h_`+hex，服务端同形状，PATCH 不改 id、
  不复用），但**删除无墓碑，同 c_ id 重放 POST 会复活**——正是拆记录要消灭的洞。

推荐做法（要点，动工时照此）：
- **只拆存储记录层，HTTP 路由形状原样保留**（GET 仍返整册排序数组）；
- per-item 键 `'document-highlight-v1:'+documentId.length+':'+documentId+':'+id`，
  删除走 store.remove 得墓碑，墓碑永远支配；
- 迁移放 beginBoot（recoverNativePDFMutationOnBoot 之后、ready 之前）：
  读 legacy 整数组 → 一次 batch 原子写 per-item + `highlights-split-done` 标记，
  标记提交前绝不清 legacy 记录，读路径按标记切换；装不进一个事务时照抄
  journal 阶段式 + 每条 `mutationId='hl-split:'+bookId+':'+item.id` 幂等；
- undo/authority 的整集修订号契约用一条轻量 `highlights-meta` 计数记录保全；
- data-registry 把 `document-highlights` 从 pending 翻 ready 时补
  recordSchema/conflictPolicy，预期 syncDigest 变化触发一次 reset-checkpoint；
- **epub-highlights 完全同构，一起拆**；ink 是整册一条 map（键=页号或 `u_` 段，
  `pages` 只是 HTTP 响应键名），按页拆同构且更简单，但 ink/closed-regions
  同源拆域逻辑（stroke.t==='region'）要保留；
- 顺序：先 PDF+EPUB 高亮，验证 user-state 摘要与助手 undo 后再动 ink。

## 方向四：命令信封与 Windows 接收端的既有约束

**结论：信封不需要发明任何新字段。**

- **命令体 = command-outbox/2 的 op 原形** `{mutationId: mut-v2-<32hex>, url,
  method, body}`——App 侧持久化、Pi 侧幂等、native 白名单三方共认。
- **批次级字段**：`{contract, deviceId, book 身份}`。origin 用 relay 的设备族格式
  `(native-app|pwa-install)-v1-<32hex>`，**不用 sourceInstanceId**（outbox 已证明
  它属于单个 WebView 生命周期、刻意不持久化，`ReaderRealtimeOutputOutbox.cs:301`）。
- **cursor 从发送信封删掉**：两套既有账本（C# outbox、Pi relay）游标都是
  **接收端落账时分配**的；发送端只在 pull 请求带自己的游标
  （relay `/exchange` 的 cursor/headCursor/oldestCursor/hasMore/resetRequired
  五件套可整体照抄，`reader_sync_relay.py:2134-2237`）。
- **单帧 ≤256KiB 双端硬校验 + HELLO 合同核对**（`DirectBridgeContract.cs:23`
  `MaximumMessageBytes`、`rc-computer-voice.js:15` `MAX_MESSAGE_BYTES=262144`）。
  批次按帧拆，**一帧一命令最稳**。
- **ack 沿用三值**：outcome ∈ {applied, replay, rejected}（离线合成 'queued'），
  error 当且仅当 rejected 存在；bindOutcome 副结果表达「送到了但没钉上」
  （`ReaderRealtimeOutput.cs:848-907`）。
- **Windows→App 方向已建**（`ReaderRealtimeOutputOutbox.cs`）：先入队后送达、
  ack=applied/replay/rejected 都销账、correlation 幂等、64 条/16MB/30 天上限、
  tmp+File.Move 原子写、SourceAttached 事件驱动重放、bind 未落实 MarkDeferred
  留队。**App→Windows 方向照抄这套 entry 形状与状态机。**
- **Windows 侧唯一现成服务面 = C# Direct 桥 WSS**（127.0.0.1:43128 经
  Tailscale serve 暴露，`bridge_core.py:41-48`）；readerpc Python 侧无任何
  HTTP/WS 监听；持久化全是 `%LOCALAPPDATA%\BWReader` 下 JSON 文件；
  **C# 工程零 NuGet 依赖、无 SQLite**。→ 落点：传输层放 C# Direct 桥
  （新 action/event 对，exact-shape 校验风格），账本层用 Python
  （sqlite3 标准库，readerpc 已有 supervisor 拉起 Python worker 的先例）；
  短期最小可用版可直接照抄 outbox 的 JSON 文件形状。
- **App 侧发送端 rc-outbox 一行不改**（`normalizeRequest` 强制同源，物理上
  发不出跨源请求，`rc-outbox.js:505-545`）。动刀点在 native-local-runtime 的
  `nativeSyncBatchOperation` 分发层（owner=local→localFetch、pi→nativePiFetch、
  其它→501，:11586-11624）。新命令路径必须同时进 `NATIVE_SYNC_BATCH_ENDPOINTS`
  白名单和 manifest 两处。
- ⚠ dim 4 提出的备选「owner=pi 分支经 Pi relay 中继给 Windows」**不采纳**——
  规格 §2.3 已拍板 Pi 不在这条链上，不重新论证。

## 综合：回写进主规格的决定

见 `references/reader-two-node-replication.md` §8.5（前提 A 设计定稿）与 §9。
一句话版：

- **前提 A**：铸 `replicationBookId`（配对时一次性、内容无关、铸后永不重算）+
  两端各存持久链接表（照 reader-library-sync-links/1 结构，含 lastSyncedSha256
  合并基线）；contentSha256 只作首次配对会合信号与版本校验，**绝不进身份**；
  摘要分叉不断链；监听身份断裂做一次性重配对。
- **前提 B**：按方向三的推荐做法拆。
- **信封**：按方向四定稿，cursor 出列、一帧一命令、ack 三值。
