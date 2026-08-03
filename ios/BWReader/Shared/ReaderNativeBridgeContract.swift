import Foundation

enum ReaderNativeBridgeContract {
    static let name = "bw-reader-native/1"
    static let appGroupIdentifier = "group.space.bwicarus.bwreader2"
    static let containingAppIdentifier = "space.bwicarus.bwreader2"
    static let launchScheme = "bwreader"
    static let pendingCommandLifetime: TimeInterval = 30

    static let supportedActions = [
        "capabilities",
        "voice.status",
        "voice.toggle",
    ]
    static let supportedAppKinds = [
        "codex-desktop",
        "chatgpt-classic",
    ]

    static func isSafeRequestID(_ value: String) -> Bool {
        guard (8...128).contains(value.utf8.count) else {
            return false
        }
        return value.unicodeScalars.allSatisfy { scalar in
            switch scalar.value {
            case 45, 46, 48...57, 65...90, 95, 97...122:
                return true
            default:
                return false
            }
        }
    }

    static func launchURL(requestID: String) -> URL? {
        guard isSafeRequestID(requestID) else {
            return nil
        }
        var components = URLComponents()
        components.scheme = launchScheme
        components.host = "native-voice"
        components.queryItems = [
            URLQueryItem(name: "requestId", value: requestID),
        ]
        return components.url
    }
}

struct ReaderNativePendingVoiceCommand: Codable, Equatable {
    let contract: String
    let action: String
    let requestID: String
    let appKind: String
    let createdAtMilliseconds: Int64
    let sourceURL: String?
    let selectionText: String?

    init(
        requestID: String,
        appKind: String,
        sourceURL: String?,
        selectionText: String?,
        now: Date = Date()
    ) {
        contract = ReaderNativeBridgeContract.name
        action = "voice.toggle"
        self.requestID = requestID
        self.appKind = appKind
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
        self.sourceURL = sourceURL
        self.selectionText = selectionText
    }

    func isFresh(at date: Date = Date()) -> Bool {
        let createdAt = Date(
            timeIntervalSince1970: TimeInterval(createdAtMilliseconds) / 1_000
        )
        let age = date.timeIntervalSince(createdAt)
        return age >= -2 && age <= ReaderNativeBridgeContract.pendingCommandLifetime
    }
}

struct ReaderNativeVoiceStatus: Codable, Equatable {
    let phase: String
    let active: Bool
    let busy: Bool
    let sessionID: String?
    let appKind: String?
    let detail: String?
    let updatedAtMilliseconds: Int64

    init(
        phase: String,
        active: Bool,
        busy: Bool,
        sessionID: String?,
        appKind: String?,
        detail: String?,
        now: Date = Date()
    ) {
        self.phase = phase
        self.active = active
        self.busy = busy
        self.sessionID = sessionID
        self.appKind = appKind
        self.detail = detail
        updatedAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
    }

    static let idle = ReaderNativeVoiceStatus(
        phase: "idle",
        active: false,
        busy: false,
        sessionID: nil,
        appKind: nil,
        detail: nil
    )

    var responseDictionary: [String: Any] {
        [
            "phase": phase,
            "active": active,
            "busy": busy,
            "sessionId": sessionID ?? NSNull(),
            "appKind": appKind ?? NSNull(),
            "detail": detail ?? NSNull(),
            "updatedAt": updatedAtMilliseconds,
        ]
    }
}

enum ReaderNativeBridgeStoreError: LocalizedError {
    case appGroupUnavailable
    case malformedPendingCommand

    var errorDescription: String? {
        switch self {
        case .appGroupUnavailable:
            return "BWReader App Group 不可用"
        case .malformedPendingCommand:
            return "共享语音请求已损坏"
        }
    }
}

struct ReaderNativeBridgeStore {
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
            throw ReaderNativeBridgeStoreError.appGroupUnavailable
        }
        try fileManager.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true
        )
        return directoryURL
    }

    private func fileURL(named name: String) throws -> URL {
        try requireDirectory().appendingPathComponent(name, isDirectory: false)
    }

    func writePending(_ command: ReaderNativePendingVoiceCommand) throws {
        let data = try JSONEncoder().encode(command)
        try data.write(
            to: fileURL(named: "voice-pending.json"),
            options: [.atomic]
        )
    }

    func readPending() throws -> ReaderNativePendingVoiceCommand? {
        let url = try fileURL(named: "voice-pending.json")
        guard fileManager.fileExists(atPath: url.path) else {
            return nil
        }
        do {
            return try JSONDecoder().decode(
                ReaderNativePendingVoiceCommand.self,
                from: Data(contentsOf: url)
            )
        } catch {
            throw ReaderNativeBridgeStoreError.malformedPendingCommand
        }
    }

    /// Reads and removes one exact, fresh command before any voice side effect.
    /// A repeated deep link therefore cannot toggle the session twice.
    func consumePending(
        requestID: String,
        now: Date = Date()
    ) throws -> ReaderNativePendingVoiceCommand? {
        guard let command = try readPending() else {
            return nil
        }
        guard command.requestID == requestID else {
            return nil
        }
        let url = try fileURL(named: "voice-pending.json")
        guard command.isFresh(at: now) else {
            try? fileManager.removeItem(at: url)
            return nil
        }
        try fileManager.removeItem(at: url)
        return command
    }

    func writeStatus(_ status: ReaderNativeVoiceStatus) throws {
        let data = try JSONEncoder().encode(status)
        try data.write(
            to: fileURL(named: "voice-status.json"),
            options: [.atomic]
        )
    }

    func readStatus() throws -> ReaderNativeVoiceStatus? {
        let url = try fileURL(named: "voice-status.json")
        guard fileManager.fileExists(atPath: url.path) else {
            return nil
        }
        return try JSONDecoder().decode(
            ReaderNativeVoiceStatus.self,
            from: Data(contentsOf: url)
        )
    }
}
