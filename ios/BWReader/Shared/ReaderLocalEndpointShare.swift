import Foundation

/// App 把本机 Reader 服务的落点告诉 Safari 扩展。
///
/// 目的：让 iPad 上的扩展直接读写 App 的数据，而不是绕去 Pi。现在扩展调
/// `/pdf/api/highlights` 打到的是 Pi，而 App 的高亮在自己的 IndexedDB 里 ——
/// 同一件东西两份数据，用户在扩展里划的线，App 里看不见。
///
/// 传的是端口与 capability token。token 本身就是本机服务的门禁，所以这份
/// 记录只放在 App Group 容器里：扩展是随 App 分发的 App Extension，与宿主
/// 同签名同沙箱域，拿到它不比 App 自己拿到更危险；而容器外的任何进程都读不到。
///
/// **过期比缺失更危险**：App 重启会换端口和 token，旧值会让扩展一直打一个
/// 不存在的落点，且失败长得像"网络不好"。所以带 `pid` 与 `startedAt`，
/// 消费方能判断这份记录是不是当前这条命还在用的。
struct ReaderLocalEndpointShare: Codable, Equatable, Sendable {
    static let schema = 1
    static let fileName = "local-endpoint.json"

    let schemaVersion: Int
    let port: UInt16
    let capabilityToken: String
    /// 写下这份记录的 App 进程。进程没了，这份记录就该被当作过期。
    let processIdentifier: Int32
    let startedAtEpochSeconds: Int

    init(port: UInt16, capabilityToken: String, startedAt: Date = Date()) {
        self.schemaVersion = Self.schema
        self.port = port
        self.capabilityToken = capabilityToken
        self.processIdentifier = ProcessInfo.processInfo.processIdentifier
        self.startedAtEpochSeconds = Int(startedAt.timeIntervalSince1970)
    }

    /// 扩展侧要用的前缀。路径形状与 `ReaderLocalRuntimeServer` 的
    /// `/r/<token>/` 保持一致 —— 两处必须同时改，否则扩展会拿到 403。
    var baseURLString: String {
        "http://127.0.0.1:\(port)/r/\(capabilityToken)"
    }
}

enum ReaderLocalEndpointShareError: LocalizedError {
    case appGroupUnavailable
    case malformed

    var errorDescription: String? {
        switch self {
        case .appGroupUnavailable:
            return "BWReader App Group 不可用"
        case .malformed:
            return "共享的本机服务落点已损坏"
        }
    }
}

struct ReaderLocalEndpointShareStore {
    private let fileManager = FileManager.default

    private var directoryURL: URL? {
        fileManager
            .containerURL(
                forSecurityApplicationGroupIdentifier:
                    ReaderNativeBridgeContract.appGroupIdentifier
            )?
            .appendingPathComponent("NativeBridge", isDirectory: true)
    }

    private func requireDirectory() throws -> URL {
        guard let directoryURL else {
            throw ReaderLocalEndpointShareError.appGroupUnavailable
        }
        try fileManager.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true
        )
        return directoryURL
    }

    private func fileURL() throws -> URL {
        try requireDirectory()
            .appendingPathComponent(
                ReaderLocalEndpointShare.fileName,
                isDirectory: false
            )
    }

    /// App 侧：本机服务起来之后写一次。
    func write(_ share: ReaderLocalEndpointShare) throws {
        let url = try fileURL()
        let data = try JSONEncoder().encode(share)
        // 原子写：扩展可能正在读，半截文件会被当成损坏而不是过期，
        // 两者的处理完全不同。
        try data.write(to: url, options: [.atomic])
        try? fileManager.setAttributes(
            [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
            ofItemAtPath: url.path
        )
    }

    /// App 侧：服务停掉时清掉，别留下一个指向死端口的落点。
    func clear() {
        guard let url = try? fileURL() else { return }
        try? fileManager.removeItem(at: url)
    }

    /// 扩展侧：读当前落点。读不到就是读不到 —— 不猜一个默认端口，
    /// 因为猜错会让扩展打到别的进程上去。
    func read() throws -> ReaderLocalEndpointShare? {
        let url = try fileURL()
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let data = try Data(contentsOf: url)
        guard
            let share = try? JSONDecoder().decode(
                ReaderLocalEndpointShare.self,
                from: data
            ),
            share.schemaVersion == ReaderLocalEndpointShare.schema,
            share.port != 0,
            !share.capabilityToken.isEmpty
        else {
            throw ReaderLocalEndpointShareError.malformed
        }
        return share
    }
}
