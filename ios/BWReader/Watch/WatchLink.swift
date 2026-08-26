import Foundation
import WatchConnectivity

/// 手表侧的 WCSession 端。手机是唯一数据源（原因见
/// Shared/ReaderWatchPayload.swift 的文件头）。
///
/// 三条通路各司其职：
/// - **收快照**：`applicationContext` —— 合并、只留最新一份、**手表 app
///   没开着也送得到**。卡片/待办/语音状态都走它。
/// - **发指令**：`sendMessage` —— 需要手机可达，但即时。开关语音要的就是即时。
/// - **本地缓存**：收到的快照落盘，下次冷启动先显示旧的（并标明多旧），
///   而不是先给一屏空白。
@MainActor
final class WatchLink: NSObject, ObservableObject {
    static let shared = WatchLink()

    @Published private(set) var snapshot: ReaderWatchSnapshot = .empty
    @Published private(set) var reachable = false
    /// 上一条指令的结果。失败必须显示出来 —— 按了没反应又不说话是最糟的。
    @Published private(set) var lastCommandNote: String?

    private var session: WCSession?
    private var tick: Timer?

    override private init() {
        super.init()
        snapshot = WatchSnapshotCache.load() ?? .empty
    }

    func activate() {
        guard WCSession.isSupported() else {
            lastCommandNote = "这台设备不支持与手机通信"
            return
        }
        if session == nil {
            let created = WCSession.default
            created.delegate = self
            session = created
            created.activate()
        }
        refreshReachable()
        // 新鲜度文案每分钟自己走一格,否则"刚刚"会一直挂着骗人。
        if tick == nil {
            tick = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) {
                [weak self] _ in
                Task { @MainActor in self?.objectWillChange.send() }
            }
        }
        send(.refresh)
    }

    var freshnessText: String {
        if snapshot.generatedAtMs <= 0 { return "还没收到手机的数据" }
        return "手机数据 · " + WatchTime.age(snapshot.ageSeconds())
    }

    func send(_ command: ReaderWatchCommand) {
        guard let session, session.activationState == .activated else {
            lastCommandNote = "还没连上手机"
            return
        }
        guard session.isReachable else {
            // 不可达时**不要**静默排队：用户按了开关，得当场知道没发出去。
            lastCommandNote = "手机不在旁边，指令没发出去"
            return
        }
        lastCommandNote = nil
        session.sendMessage(
            [ReaderWatchCommand.key: command.rawValue],
            replyHandler: { [weak self] reply in
                Task { @MainActor in self?.apply(reply, from: "reply") }
            },
            errorHandler: { [weak self] error in
                Task { @MainActor in
                    self?.lastCommandNote = "发送失败：" + error.localizedDescription
                }
            })
    }

    private func refreshReachable() {
        reachable = session?.isReachable ?? false
    }

    fileprivate func apply(_ payload: [String: Any], from source: String) {
        guard let contract = payload[ReaderWatchPayload.Key.contract] as? String
        else { return }                       // 不是我们的载荷，安静忽略
        guard contract == ReaderWatchPayload.contract else {
            // 版本对不上就说出来，别渲染半份数据让人以为是真的。
            lastCommandNote = "手机与手表版本不匹配（\(contract)）"
            return
        }
        guard let decoded = WatchSnapshotCoder.decode(payload) else {
            lastCommandNote = "手机发来的数据解不开"
            return
        }
        snapshot = decoded
        WatchSnapshotCache.save(payload)
        if source == "reply" { lastCommandNote = nil }
    }
}

extension WatchLink: WCSessionDelegate {
    nonisolated func session(
        _ session: WCSession,
        activationDidCompleteWith state: WCSessionActivationState,
        error: Error?
    ) {
        Task { @MainActor in
            self.refreshReachable()
            if let error {
                self.lastCommandNote = "连接手机失败：" + error.localizedDescription
            }
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        Task { @MainActor in self.refreshReachable() }
    }

    nonisolated func session(
        _ session: WCSession,
        didReceiveApplicationContext context: [String: Any]
    ) {
        Task { @MainActor in self.apply(context, from: "context") }
    }
}

/// 快照的序列化。手机与手表各有一份**同名同形**的实现，
/// 两边都从 Shared/ 引用同一个 Codable 结构，所以不会各自漂。
enum WatchSnapshotCoder {
    static func decode(_ payload: [String: Any]) -> ReaderWatchSnapshot? {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload)
        else { return nil }
        return try? JSONDecoder().decode(ReaderWatchSnapshot.self, from: data)
    }

    static func encode(_ snapshot: ReaderWatchSnapshot) -> [String: Any]? {
        guard let data = try? JSONEncoder().encode(snapshot),
              let object = try? JSONSerialization.jsonObject(with: data),
              let dictionary = object as? [String: Any]
        else { return nil }
        return dictionary
    }
}

/// 冷启动先显示上次的（并标明多旧），比先给一屏空白诚实也好用。
enum WatchSnapshotCache {
    private static var url: URL? {
        FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask).first?
            .appendingPathComponent("watch-snapshot.json")
    }

    static func save(_ payload: [String: Any]) {
        guard let url,
              JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload)
        else { return }
        try? data.write(to: url, options: .atomic)
    }

    static func load() -> ReaderWatchSnapshot? {
        guard let url, let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data),
              let dictionary = object as? [String: Any]
        else { return nil }
        return WatchSnapshotCoder.decode(dictionary)
    }
}
