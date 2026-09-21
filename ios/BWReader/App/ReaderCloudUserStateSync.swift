import CloudKit
import Foundation

/// 本地状态的读写口。
///
/// ⚠ 全部是 `async` 且协议本身 `Sendable`：同步引擎是个 actor，而实现方
/// （`ReaderWebViewModel`）在主线程上。把边界写成"都得 await"，适配器里那次
/// 主线程跳转就藏不住，也就不会有人顺手在别的隔离域里直接读 WebView。
protocol ReaderCloudUserStateSource: AnyObject, Sendable {
    /// 这台设备上当前可同步的书（内容摘要，小写 64 位 hex）。
    func syncableContentDigests() async -> [String]
    /// 导出某本书的各域快照（`payloadJson` 是规范化 JSON）。
    func exportDomains(contentSHA256: String) async throws -> [ReaderCloudUserStateSync.DomainSnapshot]
    /// 把合并结果整域写回本地（内部走 apply-atomically 的乐观并发）。
    func applyDomains(_ domains: [ReaderCloudUserStateSync.DomainSnapshot],
                      contentSHA256: String) async throws
}

/// iCloud 同账号跨设备同步书籍用户状态（高亮 / 便签 / 笔迹 / 插入页 / 阅读位置…）。
///
/// 选型与依据见 `references/reader-native-storage-and-epub-20260921.md`：用
/// **CKSyncEngine**（iOS 17+）—— 本地存储仍归我们，它只管推拉/重试/批次的管线。
/// 这跟 `NSPersistentCloudKitContainer`（把数据模型交给 Core Data）是两条路；
/// 我们的数据层已经有稳定编号/变更日志/墓碑/outbox，正是 CKSyncEngine 要求调用方
/// 提供的东西，不需要为上 iCloud 重塑模型。
///
/// ⚠ 几条来自官方文档、漏掉就会出错的：
/// · 引擎自己有一份**不透明状态**，由我们负责持久化并在下次启动时交还它。
///   不存＝每次冷启动全量重来。
/// · 同步时机是**不确定**的（要电量、网络、已登录 iCloud）。要「现在就同步」得
///   显式调 `sendChanges` / `fetchChanges`。
/// · 保存成功后要 `encodeSystemFields` 存下来下次再用，否则每次保存都撞
///   `serverRecordChanged`。
/// · 不要用它同步 public database。
///
/// ⚠ **跨设备的书籍身份用内容摘要，不是 localBookId**：本机导入的书
/// （`localbook:`）在每台设备上 id 都不同，而同一个 PDF 的 sha256 一样。
/// 字节不同就是不同的书 —— 高亮锚在页坐标上，硬迁过去只会错位。
actor ReaderCloudUserStateSync {
    struct DomainSnapshot: Sendable, Equatable {
        let name: String
        let payloadJson: String
        let digest: String
        let revision: Int
        let empty: Bool
    }

    static let recordType = "ReaderUserStateDomain"
    static let zoneName = "ReaderUserState"
    /// ⚠ 必须与 entitlements 里那条一致；容器本身要在开发者后台建好，否则签名会
    ///   带着一个不存在的容器，运行时所有 CloudKit 调用都失败。
    static let containerIdentifier = "iCloud.space.bwicarus.bwreader2"

    static let domainNames = ["reading-position", "highlights", "ink", "closed-regions",
                              "notes", "user-pages", "card-placements", "entity-references"]

    private let container: CKContainer
    private let store: ReaderCloudSyncStore
    private let source: any ReaderCloudUserStateSource
    private var engine: CKSyncEngine?
    private var exportCache: [String: (at: Date, domains: [DomainSnapshot])] = [:]
    /// ⚠ 合并器带一个 JSContext，不是线程安全的。它只在这个 actor 里用 ——
    ///   actor 的串行执行就是它的保护。别把它递出去。
    private lazy var merger: ReaderUserStateMerge? = ReaderUserStateMerge()

    init(source: any ReaderCloudUserStateSource, store: ReaderCloudSyncStore) {
        self.container = CKContainer(identifier: Self.containerIdentifier)
        self.store = store
        self.source = source
    }

    /// 起引擎。没登录 iCloud 时**安静地不起** —— 那不是错误：本地优先本来就能用。
    func start() async {
        guard engine == nil else { return }
        guard let status = try? await container.accountStatus(), status == .available else { return }
        let configuration = CKSyncEngine.Configuration(
            database: container.privateCloudDatabase,
            stateSerialization: store.loadEngineState(),
            delegate: self)
        let engine = CKSyncEngine(configuration)
        self.engine = engine
        engine.state.add(pendingDatabaseChanges: [.saveZone(CKRecordZone(zoneName: Self.zoneName))])
        for digest in await source.syncableContentDigests() { markDirty(contentSHA256: digest) }
    }

    /// 本地写入后调它。⚠ 只登记「哪本书脏了」，不在这里导出 —— 写入很频繁
    /// （每一笔墨迹都算），每次都导出整包会把主线程压住。
    func markDirty(contentSHA256: String) {
        let digest = contentSHA256.lowercased()
        guard digest.count == 64, let engine else { return }
        let zone = CKRecordZone.ID(zoneName: Self.zoneName, ownerName: CKCurrentUserDefaultName)
        engine.state.add(pendingRecordZoneChanges: Self.domainNames.map { name in
            .saveRecord(CKRecord.ID(recordName: Self.recordName(digest: digest, domain: name),
                                    zoneID: zone))
        })
    }

    /// 「现在就同步」。官方说了调度不确定，所以需要即时性的地方要显式来一次。
    func synchronizeNow() async {
        guard let engine else { return }
        try? await engine.fetchChanges()
        try? await engine.sendChanges()
    }

    static func recordName(digest: String, domain: String) -> String {
        "b_" + digest + "|" + domain
    }

    static func parseRecordName(_ name: String) -> (digest: String, domain: String)? {
        let parts = name.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0].hasPrefix("b_") else { return nil }
        let digest = String(parts[0].dropFirst(2))
        let domain = String(parts[1])
        guard digest.count == 64, domainNames.contains(domain) else { return nil }
        return (digest, domain)
    }

    /// 域负载用 **CKAsset**：墨迹很容易超过单字段 1MB，写成字段会在真实的书上炸。
    static func fill(_ record: CKRecord, with snapshot: DomainSnapshot, digest: String) {
        record["domain"] = snapshot.name as CKRecordValue
        record["contentSHA256"] = digest as CKRecordValue
        record["revision"] = snapshot.revision as CKRecordValue
        record["updatedAt"] = Date() as CKRecordValue
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("bw-sync-" + UUID().uuidString + ".json")
        if let data = snapshot.payloadJson.data(using: .utf8), (try? data.write(to: url)) != nil {
            record["payload"] = CKAsset(fileURL: url)
        }
    }

    static func payloadJSON(of record: CKRecord) -> String? {
        guard let asset = record["payload"] as? CKAsset, let url = asset.fileURL,
              let data = try? Data(contentsOf: url) else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

// MARK: - CKSyncEngineDelegate

extension ReaderCloudUserStateSync: CKSyncEngineDelegate {
    func handleEvent(_ event: CKSyncEngine.Event, syncEngine: CKSyncEngine) async {
        switch event {
        case .stateUpdate(let update):
            // 不存＝每次冷启动全量重来（官方明确要求由我们持久化）。
            store.saveEngineState(update.stateSerialization)
        case .fetchedRecordZoneChanges(let changes):
            for modification in changes.modifications {
                await applyRemote(modification.record)
            }
            // 远端删除不动本地：域记录是整域快照，删掉它不代表用户要删掉这本书的
            // 批注。真要"清空"应该是一次内容为空的整域写入，那条走 modifications。
        case .sentRecordZoneChanges(let sent):
            for record in sent.savedRecords {
                store.saveSystemFields(record)
                if let parsed = Self.parseRecordName(record.recordID.recordName),
                   let payload = Self.payloadJSON(of: record) {
                    store.saveBase(digest: parsed.digest, domain: parsed.domain, json: payload)
                }
            }
            for failure in sent.failedRecordSaves {
                await handleFailedSave(failure, syncEngine: syncEngine)
            }
        case .accountChange:
            // 换账号/退出登录：基线属于上一个账号，留着会让下一次合并拿错祖先。
            store.clearAll()
        default:
            break
        }
    }

    func nextRecordZoneChangeBatch(
        _ context: CKSyncEngine.SendChangesContext, syncEngine: CKSyncEngine
    ) async -> CKSyncEngine.RecordZoneChangeBatch? {
        let pending = syncEngine.state.pendingRecordZoneChanges.filter { change in
            if case .saveRecord = change { return true }
            return false
        }
        guard !pending.isEmpty else { return nil }
        return await CKSyncEngine.RecordZoneChangeBatch(pendingChanges: pending) { id in
            await self.recordToSend(id, syncEngine: syncEngine)
        }
    }
}

// MARK: - 推送与合并

extension ReaderCloudUserStateSync {
    /// 组一条要发的记录。
    ///
    /// ⚠ 与本地基线一致的域**返回 nil**（引擎会把这条待办丢掉）：不这么做的话
    /// 每次同步都把八个域原样重发一遍，既烧配额又让别的设备白合一轮。
    private func recordToSend(_ id: CKRecord.ID, syncEngine: CKSyncEngine) async -> CKRecord? {
        guard let parsed = Self.parseRecordName(id.recordName),
              let snapshot = await snapshot(digest: parsed.digest, domain: parsed.domain) else {
            syncEngine.state.remove(pendingRecordZoneChanges: [.saveRecord(id)])
            return nil
        }
        if store.loadBase(digest: parsed.digest, domain: parsed.domain) == snapshot.payloadJson {
            syncEngine.state.remove(pendingRecordZoneChanges: [.saveRecord(id)])
            return nil
        }
        let record = store.loadSystemFields(id) ?? CKRecord(recordType: Self.recordType, recordID: id)
        Self.fill(record, with: snapshot, digest: parsed.digest)
        return record
    }

    /// 取某本书某个域的当前快照。
    ///
    /// ⚠ 带一个**极短命的缓存**：一次发送要问八个域，而导出是整包的
    /// （每个域都要规范化 JSON + 算一遍 sha256，还要穿过 WebView 那一跳）。
    /// 不缓存就是一次同步把整本书导出八遍。缓存只活两秒 —— 只为了覆盖同一趟
    /// 发送，绝不用来当"数据没变"的依据（那是 store.loadBase 的活）。
    private func snapshot(digest: String, domain: String) async -> DomainSnapshot? {
        if let cached = exportCache[digest], Date().timeIntervalSince(cached.at) < 2 {
            return cached.domains.first { $0.name == domain }
        }
        guard let domains = try? await source.exportDomains(contentSHA256: digest) else { return nil }
        exportCache[digest] = (Date(), domains)
        if exportCache.count > 8 {
            // 只留最近的几本：这是趟内缓存，不是仓库。
            let stale = exportCache.filter { Date().timeIntervalSince($0.value.at) >= 2 }.map(\.key)
            for key in stale { exportCache.removeValue(forKey: key) }
        }
        return domains.first { $0.name == domain }
    }

    /// 写回之后缓存立刻作废：否则同一趟里后面的域会拿到写入前那版。
    private func invalidateExportCache(_ digest: String) {
        exportCache.removeValue(forKey: digest)
    }

    /// 服务端那条更新了 → 三方合并后写回本地，必要时把合并结果再推上去。
    ///
    /// ⚠ 导出/写回只对**当前打开的那本书**有效（它们要穿过该书的本地 runtime）。
    /// 别的书的远端改动**不能就地丢掉** —— 引擎已经认为这条"取过了"，丢了就是
    /// 永久少一份。所以先落到待处理区，等那本书被打开时再合（`drainPending`）。
    private func applyRemote(_ record: CKRecord) async {
        guard let parsed = Self.parseRecordName(record.recordID.recordName),
              let theirs = Self.payloadJSON(of: record) else { return }
        store.saveSystemFields(record)
        let open = await source.syncableContentDigests()
        guard open.contains(parsed.digest) else {
            store.savePending(digest: parsed.digest, domain: parsed.domain, json: theirs)
            return
        }
        await merge(digest: parsed.digest, domain: parsed.domain, theirs: theirs,
                    recordID: record.recordID)
    }

    /// 某本书打开时把它攒下的远端改动合掉。
    func drainPending(contentSHA256: String) async {
        let digest = contentSHA256.lowercased()
        for (domain, theirs) in store.loadPending(digest: digest) {
            await merge(digest: digest, domain: domain, theirs: theirs, recordID: nil)
            store.clearPending(digest: digest, domain: domain)
        }
    }

    private func merge(digest: String, domain: String, theirs: String,
                       recordID: CKRecord.ID?) async {
        let parsed = (digest: digest, domain: domain)
        guard let mine = await snapshot(digest: parsed.digest, domain: parsed.domain),
              let merger else { return }
        let base = store.loadBase(digest: parsed.digest, domain: parsed.domain)
        guard let merged = try? merger.mergeJSON(domain: parsed.domain, baseJSON: base,
                                                 mineJSON: mine.payloadJson, theirsJSON: theirs) else {
            // ⚠ 合不出来就**什么都不做**：整域取一边等于静默丢掉另一台设备的改动。
            return
        }
        if merged.changed {
            let next = DomainSnapshot(name: parsed.domain, payloadJson: merged.json,
                                      digest: "", revision: mine.revision, empty: false)
            guard (try? await source.applyDomains([next], contentSHA256: parsed.digest)) != nil else { return }
            invalidateExportCache(parsed.digest)
        }
        store.saveBase(digest: parsed.digest, domain: parsed.domain, json: merged.json)
        if merged.json != theirs {
            // 合并结果与服务端那份不同 → 推回去，别让两台设备各留一半。
            let id = recordID ?? CKRecord.ID(
                recordName: Self.recordName(digest: parsed.digest, domain: parsed.domain),
                zoneID: CKRecordZone.ID(zoneName: Self.zoneName, ownerName: CKCurrentUserDefaultName))
            engine?.state.add(pendingRecordZoneChanges: [.saveRecord(id)])
        }
    }

    private func handleFailedSave(
        _ failure: CKSyncEngine.Event.SentRecordZoneChanges.FailedRecordSave,
        syncEngine: CKSyncEngine
    ) async {
        switch failure.error.code {
        case .serverRecordChanged:
            // 冲突：服务端那份才是共同的新起点。合完再发。
            if let server = failure.error.serverRecord {
                await applyRemote(server)
            }
        case .zoneNotFound:
            syncEngine.state.add(pendingDatabaseChanges: [.saveZone(CKRecordZone(zoneName: Self.zoneName))])
            syncEngine.state.add(pendingRecordZoneChanges: [.saveRecord(failure.record.recordID)])
        case .unknownItem:
            // 服务端没有这条：清掉本地 system fields，下次当新建发。
            store.clearSystemFields(failure.record.recordID)
            syncEngine.state.add(pendingRecordZoneChanges: [.saveRecord(failure.record.recordID)])
        default:
            break
        }
    }
}

/// 同步要持久化的三样东西：引擎状态、每条记录的 system fields、每个域的**基线**
/// （上次同步双方都认的那一版 —— 三方合并的祖先）。
///
/// ⚠ 全部放在 App 自己的沙盒里，不放 WKWebView 的站点数据：整条链的目的之一
/// 就是让这些东西不再依赖那个 WebView 活着。
///
/// ⚠ **不要把这里的"基线"跟 `ReaderBookUserStateBaselineStore` 合并**，
/// 看着像重复，其实存的不是一种东西：
/// · 那个是 Pi 同步用的，每个域只存 **digest**（够判断"谁更新"）；
/// · 这里存的是**祖先的内容本身** —— 三方合并没有祖先内容就做不了，
///   只有摘要的话只能退回"整域取一边"，也就是这条链上最贵的那种失败。
/// 两者的身份键也不同：那边是 (账号域, localBookId, remoteBookId)，
/// 这边是内容摘要（跨设备对得上的唯一东西）。
final class ReaderCloudSyncStore: @unchecked Sendable {
    private let root: URL
    private let defaults: UserDefaults
    private let engineStateKey = "reader.cloudSync.engineState.v1"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        root = base.appendingPathComponent("BWReader/cloud-sync", isDirectory: true)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    // MARK: 引擎状态（Serialization 是 Codable，用 JSON 存）

    func loadEngineState() -> CKSyncEngine.State.Serialization? {
        guard let data = defaults.data(forKey: engineStateKey) else { return nil }
        return try? JSONDecoder().decode(CKSyncEngine.State.Serialization.self, from: data)
    }

    func saveEngineState(_ state: CKSyncEngine.State.Serialization) {
        guard let data = try? JSONEncoder().encode(state) else { return }
        defaults.set(data, forKey: engineStateKey)
    }

    // MARK: 基线

    private func baseURL(digest: String, domain: String) -> URL {
        root.appendingPathComponent("base-" + digest + "-" + domain + ".json")
    }

    func loadBase(digest: String, domain: String) -> String? {
        try? String(contentsOf: baseURL(digest: digest, domain: domain), encoding: .utf8)
    }

    func saveBase(digest: String, domain: String, json: String) {
        try? json.write(to: baseURL(digest: digest, domain: domain), atomically: true, encoding: .utf8)
    }

    // MARK: system fields

    private func systemFieldsURL(_ id: CKRecord.ID) -> URL {
        let safe = id.recordName.replacingOccurrences(of: "|", with: "_")
        return root.appendingPathComponent("sys-" + safe + ".bin")
    }

    func loadSystemFields(_ id: CKRecord.ID) -> CKRecord? {
        guard let data = try? Data(contentsOf: systemFieldsURL(id)),
              let coder = try? NSKeyedUnarchiver(forReadingFrom: data) else { return nil }
        coder.requiresSecureCoding = true
        let record = CKRecord(coder: coder)
        coder.finishDecoding()
        return record
    }

    func saveSystemFields(_ record: CKRecord) {
        let coder = NSKeyedArchiver(requiringSecureCoding: true)
        record.encodeSystemFields(with: coder)
        coder.finishEncoding()
        try? coder.encodedData.write(to: systemFieldsURL(record.recordID))
    }

    func clearSystemFields(_ id: CKRecord.ID) {
        try? FileManager.default.removeItem(at: systemFieldsURL(id))
    }

    // MARK: 待处理区（别的书的远端改动）

    private func pendingURL(digest: String, domain: String) -> URL {
        root.appendingPathComponent("pending-" + digest + "-" + domain + ".json")
    }

    func savePending(digest: String, domain: String, json: String) {
        try? json.write(to: pendingURL(digest: digest, domain: domain),
                        atomically: true, encoding: .utf8)
    }

    func loadPending(digest: String) -> [(domain: String, json: String)] {
        ReaderCloudUserStateSync.domainNames.compactMap { domain in
            guard let json = try? String(contentsOf: pendingURL(digest: digest, domain: domain),
                                         encoding: .utf8) else { return nil }
            return (domain, json)
        }
    }

    func clearPending(digest: String, domain: String) {
        try? FileManager.default.removeItem(at: pendingURL(digest: digest, domain: domain))
    }

    /// 换账号时清空：基线属于上一个账号，留着会让下一次合并拿错祖先。
    func clearAll() {
        defaults.removeObject(forKey: engineStateKey)
        try? FileManager.default.removeItem(at: root)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
}
