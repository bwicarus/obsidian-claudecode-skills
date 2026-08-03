import Foundation

/// The fixed Reader -> Windows voice protocol constants.  Keep these values in
/// lock-step with DirectBridgeContract.cs and rc-computer-voice.js.
enum DirectVoiceProtocol {
    static let contract = "reader-computer-voice-direct/1"
    static let protocolVersion = 3

    static let endpoint = URL(
        string: "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1"
    )!
    static let origin = "https://bwicarus.taile44d0c.ts.net"

    static let maximumMessageBytes = 64 * 1024
    static let maximumPendingRequests = 16
    static let pcmHeaderBytes = 36
    static let pcmPayloadBytes = 1_920
    static let pcmFrameBytes = pcmHeaderBytes + pcmPayloadBytes
    static let pcmSamplesPerFrame = 960
    static let pcmSampleRate = 48_000
    static let pcmFrameDurationMicroseconds: UInt64 = 20_000
    static let pcmQueueLimitMilliseconds = 400
    static let uplinkQueueLimitMilliseconds = 200
    static let heartbeatIntervalMilliseconds = 5_000
    static let heartbeatTimeoutMilliseconds = 15_000

    static let openTimeoutNanoseconds: UInt64 = 6_000_000_000
    static let requestTimeoutNanoseconds: UInt64 = 7_000_000_000
    static let startTimeoutNanoseconds: UInt64 = 45_000_000_000
}

enum DirectVoiceTargetApp: String, Sendable, Equatable {
    case codexDesktop = "codex-desktop"
    case chatGPTClassic = "chatgpt-classic"

    var displayName: String {
        switch self {
        case .codexDesktop:
            return "Codex"
        case .chatGPTClassic:
            return "GPT Classic"
        }
    }
}

struct DirectVoiceConfiguration: Sendable, Equatable {
    static let production = DirectVoiceConfiguration()

    let endpoint: URL
    let origin: String

    private init() {
        endpoint = DirectVoiceProtocol.endpoint
        origin = DirectVoiceProtocol.origin
    }
}

enum DirectVoiceTrack: UInt8, Sendable, Equatable {
    case appOutput = 1
    case browserMicrophone = 3
}

enum DirectVoiceState: String, Sendable, Equatable {
    case disconnected
    case connecting
    case authenticating
    case ready
    case starting
    case active
    case stopping
    case failed
}

struct DirectVoiceSession: Sendable, Equatable {
    let id: String
    let startedAt: Date
}

struct DirectVoiceRuntimeStatus: Sendable, Equatable {
    let state: String
    let reason: String?
    let ready: Bool?
    let localOptIn: Bool?
    let hostReady: Bool?
    let captureActive: Bool?
}

struct DirectVoicePCMFrame: Sendable, Equatable {
    let track: DirectVoiceTrack
    let sequence: UInt32
    let timestampMicroseconds: UInt64
    /// Exactly 960 mono signed 16-bit little-endian samples.
    let payload: Data

    var samples: [Int16] {
        guard payload.count == DirectVoiceProtocol.pcmPayloadBytes else {
            return []
        }
        let bytes = [UInt8](payload)
        var result: [Int16] = []
        result.reserveCapacity(DirectVoiceProtocol.pcmSamplesPerFrame)
        for offset in stride(from: 0, to: bytes.count, by: 2) {
            let bits = UInt16(bytes[offset])
                | (UInt16(bytes[offset + 1]) << 8)
            result.append(Int16(bitPattern: bits))
        }
        return result
    }
}

struct DirectVoiceFailure: Error, Sendable, Equatable, LocalizedError {
    let code: String
    let message: String
    let retryable: Bool

    var errorDescription: String? { message }

    init(code: String, message: String, retryable: Bool) {
        self.code = Self.bounded(code, limit: 160)
        self.message = Self.bounded(message, limit: 1_024)
        self.retryable = retryable
    }

    private static func bounded(_ value: String, limit: Int) -> String {
        if value.count <= limit {
            return value
        }
        return String(value.prefix(limit))
    }
}

enum DirectVoiceEvent: Sendable, Equatable {
    case state(DirectVoiceState)
    case runtime(DirectVoiceRuntimeStatus)
    case downlinkPCM(DirectVoicePCMFrame)
    case error(DirectVoiceFailure)
}

// MARK: - Strict JSON value

/// A small Sendable JSON tree used so request correlation can remain actor-safe
/// without moving `[String: Any]` across isolation boundaries.
enum DirectJSONValue: Sendable, Equatable, Codable {
    case object([String: DirectJSONValue])
    case array([DirectJSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        if let keyed = try? decoder.container(
            keyedBy: DirectJSONCodingKey.self
        ) {
            var object: [String: DirectJSONValue] = [:]
            for key in keyed.allKeys {
                object[key.stringValue] = try keyed.decode(
                    DirectJSONValue.self,
                    forKey: key
                )
            }
            self = .object(object)
            return
        }

        if var unkeyed = try? decoder.unkeyedContainer() {
            var array: [DirectJSONValue] = []
            while !unkeyed.isAtEnd {
                array.append(try unkeyed.decode(DirectJSONValue.self))
            }
            self = .array(array)
            return
        }

        let single = try decoder.singleValueContainer()
        if single.decodeNil() {
            self = .null
        } else if let value = try? single.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? single.decode(Double.self) {
            guard value.isFinite else {
                throw DecodingError.dataCorruptedError(
                    in: single,
                    debugDescription: "JSON number must be finite"
                )
            }
            self = .number(value)
        } else if let value = try? single.decode(String.self) {
            self = .string(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: single,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        switch self {
        case .object(let object):
            var container = encoder.container(
                keyedBy: DirectJSONCodingKey.self
            )
            for (key, value) in object {
                try container.encode(
                    value,
                    forKey: DirectJSONCodingKey(stringValue: key)!
                )
            }
        case .array(let array):
            var container = encoder.unkeyedContainer()
            for value in array {
                try container.encode(value)
            }
        case .string(let value):
            var container = encoder.singleValueContainer()
            try container.encode(value)
        case .number(let value):
            var container = encoder.singleValueContainer()
            try container.encode(value)
        case .bool(let value):
            var container = encoder.singleValueContainer()
            try container.encode(value)
        case .null:
            var container = encoder.singleValueContainer()
            try container.encodeNil()
        }
    }

    var objectValue: [String: DirectJSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }

    var stringValue: String? {
        guard case .string(let value) = self else { return nil }
        return value
    }

    var boolValue: Bool? {
        guard case .bool(let value) = self else { return nil }
        return value
    }

    var unsignedIntegerValue: UInt64? {
        guard case .number(let value) = self,
              value >= 0,
              value.rounded(.towardZero) == value,
              // JSON is decoded through Double. Reject integers outside the
              // exactly representable range rather than risking a trapping
              // Double -> UInt64 conversion on hostile input.
              value <= 9_007_199_254_740_991 else {
            return nil
        }
        return UInt64(value)
    }
}

private struct DirectJSONCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

extension Dictionary where Key == String, Value == DirectJSONValue {
    func requireExactKeys(
        _ required: Set<String>,
        optional: Set<String> = []
    ) throws {
        let allowed = required.union(optional)
        guard Set(keys).isSubset(of: allowed),
              required.isSubset(of: Set(keys)) else {
            throw DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "Windows 桥接器消息字段不匹配",
                retryable: false
            )
        }
    }

    func requireString(_ key: String, maximum: Int = 1_024) throws -> String {
        guard let value = self[key]?.stringValue,
              !value.isEmpty,
              value.count <= maximum else {
            throw DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "\(key) 字段无效",
                retryable: false
            )
        }
        return value
    }

    func requireBool(_ key: String) throws -> Bool {
        guard let value = self[key]?.boolValue else {
            throw DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "\(key) 字段无效",
                retryable: false
            )
        }
        return value
    }

    func requireUInt32(_ key: String) throws -> UInt32 {
        guard let value = self[key]?.unsignedIntegerValue,
              value <= UInt64(UInt32.max) else {
            throw DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "\(key) 字段无效",
                retryable: false
            )
        }
        return UInt32(value)
    }

    func requireInt(_ key: String) throws -> Int {
        guard let value = self[key]?.unsignedIntegerValue,
              value <= UInt64(Int.max) else {
            throw DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "\(key) 字段无效",
                retryable: false
            )
        }
        return Int(value)
    }

    func requireObject(
        _ key: String
    ) throws -> [String: DirectJSONValue] {
        guard let value = self[key]?.objectValue else {
            throw DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "\(key) 字段无效",
                retryable: false
            )
        }
        return value
    }
}

func directVoiceSafeID(_ value: String) -> Bool {
    guard (1...160).contains(value.utf8.count) else {
        return false
    }
    return value.utf8.allSatisfy { byte in
        (byte >= 65 && byte <= 90)
            || (byte >= 97 && byte <= 122)
            || (byte >= 48 && byte <= 57)
            || byte == 46
            || byte == 95
            || byte == 58
            || byte == 45
    }
}
