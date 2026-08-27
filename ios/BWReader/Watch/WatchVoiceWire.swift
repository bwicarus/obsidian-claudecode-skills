#if os(watchOS)

import Foundation

/// 手表 → Pi 的线格式。**权威定义在 `_server_deploy/watch_voice_wire.py`**，
/// 这里是它的 Swift 对应物。
///
/// ## ⚠ 两份实现，一份权威
///
/// 这是本仓库反复吃亏的那个模式（同一份约定散落在多处，改一处漏一处）。
/// 这里的做法是：**Swift 侧只做编码，解码由 Pi 负责**，而且字段全部照抄
/// Python 那份的偏移量。改任何一个数字，两边必须同步。
///
/// 布局（共 1956 字节）：
///
/// ```
///  [0:4]   magic  "BWWV"
///  [4]     version = 1
///  [5]     track   = 3           上行：Pi → Windows 的虚拟麦克风
///  [6:8]   flags   = 0           保留，恒零
///  [8:24]  streamId 的 16 字节原值（由 Pi 在 hello 里下发）
///  [24:28] sequence  u32 小端
///  [28:36] timestampUs u64 小端
///  [36:]   1920 字节 s16le / 48kHz / 单声道 / 20ms
/// ```
///
/// ⚠ `streamId` **每条连接一个新的**，由 Pi 下发；用上一条连接的会被判
/// FOREIGN 丢掉 —— 而且是静默丢，表现为「连着但对面听不见」。
enum WatchVoiceWire {
    static let magic: [UInt8] = Array("BWWV".utf8)
    static let version: UInt8 = 1
    static let track: UInt8 = 3
    static let headerBytes = 36
    static let payloadBytes = 1920
    static let frameBytes = headerBytes + payloadBytes      // 1956
    static let sampleRate = 48_000.0
    static let samplesPerFrame = 960                        // 20ms
    static let frameDurationUs: UInt64 = 20_000

    /// 把 `session-<base64url>` 还原成 16 字节。
    ///
    /// ⚠ 严格：长度不对就返回 nil，**不做补齐、不截断**。Pi 侧对同样的
    /// 输入做 round-trip 校验，这里放宽只会把错误推迟到对面变成一句
    /// 看不懂的拒绝。
    static func streamIdBytes(_ streamId: String) -> Data? {
        guard streamId.hasPrefix("session-") else { return nil }
        var raw = String(streamId.dropFirst("session-".count))
        raw = raw.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while raw.count % 4 != 0 { raw += "=" }
        guard let data = Data(base64Encoded: raw), data.count == 16 else { return nil }
        return data
    }

    /// 编一帧。`payload` 必须恰好 1920 字节。
    static func encode(
        streamId: Data, sequence: UInt32, timestampUs: UInt64, payload: Data
    ) -> Data? {
        guard streamId.count == 16, payload.count == payloadBytes else { return nil }
        var frame = Data(capacity: frameBytes)
        frame.append(contentsOf: magic)
        frame.append(version)
        frame.append(track)
        frame.append(contentsOf: [0, 0])                    // flags
        frame.append(streamId)
        withUnsafeBytes(of: sequence.littleEndian) { frame.append(contentsOf: $0) }
        withUnsafeBytes(of: timestampUs.littleEndian) { frame.append(contentsOf: $0) }
        frame.append(payload)
        return frame.count == frameBytes ? frame : nil
    }
}

#endif
