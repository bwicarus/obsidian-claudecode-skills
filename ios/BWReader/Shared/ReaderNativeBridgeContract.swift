import Foundation

enum ReaderNativeBridgeContract {
    static let name = "bw-reader-native/1"
    static let appGroupIdentifier = "group.space.bwicarus.bwreader2"
    static let realtimeKeychainAccessGroup =
        "7MDVSLPV8F.space.bwicarus.bwreader2.realtime"
    static let containingAppIdentifier = "space.bwicarus.bwreader2"
    static let launchScheme = "bwreader"
    static let pendingCommandLifetime: TimeInterval = 30

    static let supportedActions = [
        "capabilities",
        "voice.status",
        "voice.toggle",
        "voice.context",
        "agent.status",
        "agent.toggle",
        "agent.events",
        "agent.command",
        "notes.status",
        "notes.list",
        "notes.read",
        "notes.create",
        "realtime.status",
        "realtime.mint",
        "realtime.image",
        "realtime.hangup",
        // App 登录后替扩展铸的服务器设备令牌（2026-09-06）：扩展自动登录靠它。
        "account.token",
        // 离线日语词典（C 组 #19）：扩展查词走 App 本地的 JMdict 而不是打 Pi。
        //   词典在 App Group 共享容器里，所以这两条**零前台依赖** ——
        //   native handler 跑在扩展进程里，App 在不在前台都不影响。
        "dict.status",
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

    static func launchURL(
        requestID: String,
        host: String = "native-voice"
    ) -> URL? {
        guard isSafeRequestID(requestID) else {
            return nil
        }
        guard host == "native-voice" || host == "native-agent" else {
            return nil
        }
        var components = URLComponents()
        components.scheme = launchScheme
        components.host = host
        components.queryItems = [
            URLQueryItem(name: "requestId", value: requestID),
        ]
        return components.url
    }
}

struct ReaderNativeWebContext: Codable, Equatable {
    let url: String
    let title: String
    let visibleText: String
    let selection: String
    let revision: String

    var isValid: Bool {
        guard
            let parsed = URL(string: url),
            let scheme = parsed.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            parsed.user == nil,
            parsed.password == nil,
            url.utf8.count <= 2_048,
            title.utf8.count <= 1_024,
            visibleText.utf8.count <= 32_768,
            selection.utf8.count <= 4_096,
            revision.count == 16
        else {
            return false
        }
        return revision.unicodeScalars.allSatisfy {
            (48...57).contains($0.value) || (97...102).contains($0.value)
        }
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
    let webContext: ReaderNativeWebContext?

    init(
        requestID: String,
        appKind: String,
        sourceURL: String?,
        selectionText: String?,
        webContext: ReaderNativeWebContext? = nil,
        now: Date = Date()
    ) {
        contract = ReaderNativeBridgeContract.name
        action = "voice.toggle"
        self.requestID = requestID
        self.appKind = appKind
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
        self.sourceURL = sourceURL
        self.selectionText = selectionText
        self.webContext = webContext
    }

    func isFresh(at date: Date = Date()) -> Bool {
        let createdAt = Date(
            timeIntervalSince1970: TimeInterval(createdAtMilliseconds) / 1_000
        )
        let age = date.timeIntervalSince(createdAt)
        return age >= -2 && age <= ReaderNativeBridgeContract.pendingCommandLifetime
    }
}

struct ReaderNativePendingAgentToggle: Codable, Equatable {
    let contract: String
    let action: String
    let requestID: String
    let command: String
    let createdAtMilliseconds: Int64
    let webContext: ReaderNativeWebContext?

    init(
        requestID: String,
        command: String,
        webContext: ReaderNativeWebContext?,
        now: Date = Date()
    ) {
        contract = ReaderNativeBridgeContract.name
        action = "agent.toggle"
        self.requestID = requestID
        self.command = command
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
        self.webContext = webContext
    }

    func isFresh(at date: Date = Date()) -> Bool {
        let createdAt = Date(
            timeIntervalSince1970: TimeInterval(createdAtMilliseconds) / 1_000
        )
        let age = date.timeIntervalSince(createdAt)
        return age >= -2 && age <= ReaderNativeBridgeContract.pendingCommandLifetime
    }
}

struct ReaderNativeAgentStatus: Codable, Equatable {
    let phase: String
    let active: Bool
    let busy: Bool
    let speaking: Bool
    let detail: String?
    let updatedAtMilliseconds: Int64

    init(
        phase: String,
        active: Bool,
        busy: Bool,
        speaking: Bool,
        detail: String?,
        now: Date = Date()
    ) {
        self.phase = phase
        self.active = active
        self.busy = busy
        self.speaking = speaking
        self.detail = detail
        updatedAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
    }

    static let idle = ReaderNativeAgentStatus(
        phase: "idle",
        active: false,
        busy: false,
        speaking: false,
        detail: nil
    )

    var responseDictionary: [String: Any] {
        [
            "phase": phase,
            "active": active,
            "busy": busy,
            "speaking": speaking,
            "detail": detail ?? NSNull(),
            "updatedAt": updatedAtMilliseconds,
        ]
    }
}

struct ReaderNativeAgentEventPayload: Codable, Equatable {
    var text: String?
    var error: String?
    var phase: String?
    var active: Bool?
    var busy: Bool?
    var speaking: Bool?
    var detail: String?

    init(
        text: String? = nil,
        error: String? = nil,
        phase: String? = nil,
        active: Bool? = nil,
        busy: Bool? = nil,
        speaking: Bool? = nil,
        detail: String? = nil
    ) {
        self.text = text
        self.error = error
        self.phase = phase
        self.active = active
        self.busy = busy
        self.speaking = speaking
        self.detail = detail
    }

    var responseDictionary: [String: Any] {
        var value: [String: Any] = [:]
        if let text { value["text"] = text }
        if let error { value["error"] = error }
        if let phase { value["phase"] = phase }
        if let active { value["active"] = active }
        if let busy { value["busy"] = busy }
        if let speaking { value["speaking"] = speaking }
        if let detail { value["detail"] = detail }
        return value
    }
}

struct ReaderNativeAgentEvent: Codable, Equatable {
    let sequence: Int64
    let event: String
    let payload: ReaderNativeAgentEventPayload

    var responseDictionary: [String: Any] {
        [
            "sequence": sequence,
            "event": event,
            "payload": payload.responseDictionary,
        ]
    }
}

struct ReaderNativeAgentControl: Codable, Equatable {
    let requestID: String
    let command: String
    let text: String?
    let mood: String?
    let createdAtMilliseconds: Int64

    init(
        requestID: String,
        command: String,
        text: String? = nil,
        mood: String? = nil,
        now: Date = Date()
    ) {
        self.requestID = requestID
        self.command = command
        self.text = text
        self.mood = mood
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
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
    case malformedAgentBridge

    var errorDescription: String? {
        switch self {
        case .appGroupUnavailable:
            return "BWReader App Group 不可用"
        case .malformedPendingCommand:
            return "共享语音请求已损坏"
        case .malformedAgentBridge:
            return "共享原生语音状态已损坏"
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

    func consumeAnyPendingVoice(
        now: Date = Date()
    ) throws -> ReaderNativePendingVoiceCommand? {
        guard let command = try readPending() else { return nil }
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

    func writeLatestWebContext(_ context: ReaderNativeWebContext) throws {
        guard context.isValid else {
            throw ReaderNativeBridgeStoreError.malformedPendingCommand
        }
        try JSONEncoder().encode(context).write(
            to: fileURL(named: "voice-web-context.json"),
            options: [.atomic]
        )
    }

    func readLatestWebContext() throws -> ReaderNativeWebContext? {
        let url = try fileURL(named: "voice-web-context.json")
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let value = try JSONDecoder().decode(
            ReaderNativeWebContext.self,
            from: Data(contentsOf: url)
        )
        guard value.isValid else {
            throw ReaderNativeBridgeStoreError.malformedPendingCommand
        }
        return value
    }

    func writePendingAgentToggle(
        _ command: ReaderNativePendingAgentToggle
    ) throws {
        try JSONEncoder().encode(command).write(
            to: fileURL(named: "agent-pending.json"),
            options: [.atomic]
        )
    }

    func consumePendingAgentToggle(
        requestID: String,
        now: Date = Date()
    ) throws -> ReaderNativePendingAgentToggle? {
        let url = try fileURL(named: "agent-pending.json")
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let value = try JSONDecoder().decode(
            ReaderNativePendingAgentToggle.self,
            from: Data(contentsOf: url)
        )
        guard value.requestID == requestID else { return nil }
        guard value.isFresh(at: now) else {
            try? fileManager.removeItem(at: url)
            return nil
        }
        try fileManager.removeItem(at: url)
        return value
    }

    func writeAgentStatus(_ status: ReaderNativeAgentStatus) throws {
        try JSONEncoder().encode(status).write(
            to: fileURL(named: "agent-status.json"),
            options: [.atomic]
        )
    }

    func readAgentStatus() throws -> ReaderNativeAgentStatus? {
        let url = try fileURL(named: "agent-status.json")
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        return try JSONDecoder().decode(
            ReaderNativeAgentStatus.self,
            from: Data(contentsOf: url)
        )
    }

    @discardableResult
    func appendAgentEvent(
        event: String,
        payload: ReaderNativeAgentEventPayload
    ) throws -> ReaderNativeAgentEvent {
        let url = try fileURL(named: "agent-events.json")
        let existing: [ReaderNativeAgentEvent]
        if fileManager.fileExists(atPath: url.path) {
            existing = try JSONDecoder().decode(
                [ReaderNativeAgentEvent].self,
                from: Data(contentsOf: url)
            )
        } else {
            existing = []
        }
        let next = ReaderNativeAgentEvent(
            sequence: (existing.last?.sequence ?? 0) + 1,
            event: event,
            payload: payload
        )
        let retained = Array((existing + [next]).suffix(64))
        try JSONEncoder().encode(retained).write(
            to: url,
            options: [.atomic]
        )
        return next
    }

    func readAgentEvents(after sequence: Int64) throws
        -> [ReaderNativeAgentEvent]
    {
        let url = try fileURL(named: "agent-events.json")
        guard fileManager.fileExists(atPath: url.path) else { return [] }
        return try JSONDecoder().decode(
            [ReaderNativeAgentEvent].self,
            from: Data(contentsOf: url)
        ).filter { $0.sequence > sequence }
    }

    func writeAgentControl(_ command: ReaderNativeAgentControl) throws {
        let directory = try requireDirectory()
            .appendingPathComponent("AgentCommands", isDirectory: true)
        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        let url = directory.appendingPathComponent(
            command.requestID + ".json",
            isDirectory: false
        )
        try JSONEncoder().encode(command).write(to: url, options: [.atomic])
    }

    func consumeAgentControls() throws -> [ReaderNativeAgentControl] {
        let directory = try requireDirectory()
            .appendingPathComponent("AgentCommands", isDirectory: true)
        guard fileManager.fileExists(atPath: directory.path) else { return [] }
        let urls = try fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ).filter { $0.pathExtension == "json" }
        var values: [ReaderNativeAgentControl] = []
        for url in urls {
            do {
                let value = try JSONDecoder().decode(
                    ReaderNativeAgentControl.self,
                    from: Data(contentsOf: url)
                )
                values.append(value)
                try fileManager.removeItem(at: url)
            } catch {
                try? fileManager.removeItem(at: url)
                throw ReaderNativeBridgeStoreError.malformedAgentBridge
            }
        }
        return values.sorted {
            if $0.createdAtMilliseconds == $1.createdAtMilliseconds {
                return $0.requestID < $1.requestID
            }
            return $0.createdAtMilliseconds < $1.createdAtMilliseconds
        }
    }
}
