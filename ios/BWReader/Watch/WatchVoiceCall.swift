#if os(watchOS)

import AVFoundation
import Combine
import Foundation
import Network
import WatchKit

/// 手表 ↔ Pi ↔ Windows 的连续双工通话。
///
/// ## 每一条设计都来自 2026-08-27/28 的真机实测，不是推断
///
/// | 做法 | 为什么 |
/// |---|---|
/// | 不用 CallKit | CallKit 会把屏幕锁进系统通话 UI，我们自己的界面一点都显示不出来（用户实测） |
/// | 异步 `activate(options:)` | `setActive()` **不抛错但不解禁** WebSocket —— 这个静默陷阱让所有人以为此路不通 |
/// | 边放边录 | 只录不放时息屏就断；边放边录能活。B/C 与 F 档的对照是干净的 |
/// | 重连**只重建 socket** | Apple 明说后台无法重新激活录音。音频会话一旦停掉就再也起不来 |
/// | 等到 `hello` 再开始发 | streamId 每条连接一个新的，用旧的会被 Pi 判 FOREIGN **静默丢掉** |
///
/// ## ⚠ 一条已知且已接受的限制
///
/// 音频被系统中断（Siri / 来电 / 健身）后，**watchOS 不允许在后台重新激活录音**
/// （Apple 框架工程师原话）。所以那种情况下通话不可恢复，必须回到 App 重新发起。
/// 这不是 bug 是平台边界 —— 所以要**当场说出来**，而不是让它悄悄停住。
@MainActor
final class WatchVoiceCall: ObservableObject {

    enum Phase: Equatable {
        case idle
        case connecting
        case live
        case reconnecting(String)
        /// 不可恢复。`reason` 要能直接显示给人看。
        case ended(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var framesSent = 0
    @Published private(set) var framesPlayed = 0
    /// 最后一次从 Pi 收到任何东西的时刻。陈旧必须看得见 —— 这条链路上
    /// 「悄悄不动了」是最常见的失败形态。
    @Published private(set) var silentSeconds = 0

    private static let endpoint = URL(
        string: "wss://bwicarus.taile44d0c.ts.net:8443/watch-voice")!
    /// 多久没收到东西就认为断了。Pi 在通话中会持续回下行音频，
    /// 所以 3 秒的沉默已经不正常。
    private static let stallSeconds: TimeInterval = 3

    private var connection: NWConnection?
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var converter: AVAudioConverter?
    private var streamId: Data?
    private var sequence: UInt32 = 0
    private var pending = Data()                 // 攒够 1920 字节再发
    private var lastInboundAt = Date()
    private var watchdog: Task<Void, Never>?
    private var reconnects = 0
    private var audioReady = false
    private var interrupted = false

    private static let maximumReconnects = 30

    // ── 开始 ──

    func start() {
        guard phase == .idle || isEnded else { return }
        phase = .connecting
        framesSent = 0
        framesPlayed = 0
        sequence = 0
        reconnects = 0
        interrupted = false
        pending.removeAll(keepingCapacity: true)
        Task { await begin() }
    }

    private var isEnded: Bool {
        if case .ended = phase { return true }
        return false
    }

    private func begin() async {
        guard let token = WatchTokenStore.load() else {
            phase = .ended("还没配语音 token —— 去「语音」屏点一次配给")
            return
        }
        do {
            try await prepareAudio()
        } catch {
            let why = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            phase = .ended("音频起不来：" + why)
            return
        }
        observeInterruptions()
        startWatchdog()
        openSocket(token: token)
    }

    /// 起音频。**整通电话里只做这一次。**
    ///
    /// ⚠ 顺序和 API 都不能改：
    /// - `.playAndRecord` + `.voiceChat`（回声消除）
    /// - **异步** `activate(options:)` —— 同步的 `setActive()` 不抛错但不解禁网络
    /// - 边录**边放**：只录不放的话息屏就断（实测）
    private func prepareAudio() async throws {
        let session = AVAudioSession.sharedInstance()
        let granted = await withCheckedContinuation { continuation in
            session.requestRecordPermission { continuation.resume(returning: $0) }
        }
        guard granted else { throw CallError.microphoneDenied }

        try session.setCategory(.playAndRecord, mode: .voiceChat)
        try await session.activate(options: [])

        let made = AVAudioEngine()
        let input = made.inputNode
        let inputFormat = input.outputFormat(forBus: 0)

        // 目标：48kHz / 单声道 / s16le —— Pi 和 Windows 两头都按这个算。
        guard let wireFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: WatchVoiceWire.sampleRate,
            channels: 1,
            interleaved: true)
        else { throw CallError.formatUnavailable }
        converter = AVAudioConverter(from: inputFormat, to: wireFormat)

        input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) {
            [weak self] buffer, _ in
            Task { @MainActor in self?.capture(buffer, to: wireFormat) }
        }

        // 播放侧。⚠ 必须真的在放 —— 保活靠的就是它。
        let node = AVAudioPlayerNode()
        made.attach(node)
        made.connect(node, to: made.mainMixerNode,
                     format: made.outputNode.inputFormat(forBus: 0))
        try made.start()
        node.play()

        engine = made
        player = node
        audioReady = true
    }

    // ── 采集 → 发送 ──

    private func capture(_ buffer: AVAudioPCMBuffer, to wireFormat: AVAudioFormat) {
        guard let converter, audioReady else { return }
        let ratio = wireFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let converted = AVAudioPCMBuffer(
            pcmFormat: wireFormat, frameCapacity: capacity) else { return }

        var supplied = false
        var error: NSError?
        converter.convert(to: converted, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, converted.frameLength > 0,
              let channel = converted.int16ChannelData else { return }

        let bytes = Int(converted.frameLength) * 2
        pending.append(UnsafeBufferPointer(
            start: UnsafeRawPointer(channel[0]).assumingMemoryBound(to: UInt8.self),
            count: bytes))

        // 攒够一帧就发。⚠ 不足一帧**不发** —— Windows 那头对帧长是精确要求的，
        // 短帧会被判违规而不是"少一点声音"。
        while pending.count >= WatchVoiceWire.payloadBytes {
            let payload = pending.prefix(WatchVoiceWire.payloadBytes)
            pending.removeFirst(WatchVoiceWire.payloadBytes)
            send(payload: Data(payload))
        }
    }

    private func send(payload: Data) {
        // 还没拿到 streamId 就丢掉这一帧。⚠ 发出去会被 Pi 判 FOREIGN
        // **静默丢弃** —— 「发了但对面收不到」比「没发」难查得多。
        guard let connection, let stream = streamId else { return }
        guard let frame = WatchVoiceWire.encode(
            streamId: stream,
            sequence: sequence,
            timestampUs: UInt64(sequence) * WatchVoiceWire.frameDurationUs,
            payload: payload)
        else { return }
        sequence &+= 1
        framesSent += 1

        let meta = NWProtocolWebSocket.Metadata(opcode: .binary)
        let context = NWConnection.ContentContext(identifier: "pcm", metadata: [meta])
        connection.send(content: frame, contentContext: context,
                        isComplete: true, completion: .contentProcessed { _ in })
    }

    // ── 接收 → 播放 ──

    private func play(_ payload: Data) {
        guard let player, let engine, engine.isRunning else { return }
        let format = engine.outputNode.inputFormat(forBus: 0)
        let samples = payload.count / 2
        guard samples > 0, let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(samples)) else { return }
        buffer.frameLength = AVAudioFrameCount(samples)
        guard let channels = buffer.floatChannelData else { return }

        payload.withUnsafeBytes { raw in
            let source = raw.bindMemory(to: Int16.self)
            for channel in 0..<Int(format.channelCount) {
                let out = channels[channel]
                for index in 0..<samples {
                    out[index] = Float(Int16(littleEndian: source[index])) / 32768.0
                }
            }
        }
        player.scheduleBuffer(buffer, completionHandler: nil)
        framesPlayed += 1
    }

    // ── 连接 ──

    private func openSocket(token: String) {
        let options = NWProtocolWebSocket.Options()
        options.autoReplyPing = true
        options.setAdditionalHeaders([("Authorization", "Bearer " + token)])

        let parameters: NWParameters = .tls
        parameters.defaultProtocolStack.applicationProtocols.insert(options, at: 0)
        parameters.serviceClass = .interactiveVoice

        let made = NWConnection(to: .url(Self.endpoint), using: parameters)
        connection = made
        made.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in self?.handle(state) }
        }
        made.start(queue: .main)
        receiveNext(on: made)
    }

    private func handle(_ state: NWConnection.State) {
        switch state {
        case .ready:
            // 连上了但**还不能发**：要等 hello 里的 streamId。
            lastInboundAt = Date()
            command("start")
        case .waiting(let error):
            phase = .reconnecting(String(describing: error))
        case .failed(let error):
            phase = .reconnecting(String(describing: error))
            reconnect()
        default:
            break
        }
    }

    private func receiveNext(on connection: NWConnection) {
        connection.receiveMessage { [weak self] data, context, _, error in
            Task { @MainActor in
                guard let self else { return }
                if error != nil { self.reconnect(); return }
                self.lastInboundAt = Date()

                let isText = (context?.protocolMetadata(
                    definition: NWProtocolWebSocket.definition)
                    as? NWProtocolWebSocket.Metadata)?.opcode == .text
                if let data {
                    if isText { self.handleText(data) } else { self.play(data) }
                }
                self.receiveNext(on: connection)
            }
        }
    }

    private func handleText(_ data: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: data),
              let row = object as? [String: Any] else { return }
        if let stream = row["streamId"] as? String {
            streamId = WatchVoiceWire.streamIdBytes(stream)
            if streamId != nil { phase = .live }
        }
        if let event = row["ev"] as? String, event == "error" {
            // 服务端的中文原文照抄 —— 它本来就写给人看，折成"失败"就把
            // 唯一能指导下一步的信息扔了。
            let message = (row["message"] as? String)
                ?? (row["code"] as? String) ?? "服务端拒绝"
            phase = .ended(message)
            teardown()
        }
    }

    private func command(_ op: String) {
        guard let connection else { return }
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "op", metadata: [meta])
        // ⚠ 精确单键：Pi 那边多一个字段就拒。刻意用字面量拼，
        // 不从任何可变结构生成 —— 这条链路的另一头能开麦克风。
        connection.send(content: Data("{\"op\":\"\(op)\"}".utf8),
                        contentContext: context, isComplete: true,
                        completion: .contentProcessed { _ in })
    }

    // ── 断线自愈 ──

    /// ⚠ **只重建 socket，绝不碰音频会话。**
    ///
    /// Apple 明说 watchOS 后台无法重新激活录音，所以音频会话一旦停掉就再也
    /// 起不来。而网络路径变化（手表息屏切网，实测约 5 秒）是另一回事，
    /// 它只需要重连。音频全程不动，豁免就一直在。
    private func reconnect() {
        guard !isEnded, reconnects < Self.maximumReconnects else {
            if reconnects >= Self.maximumReconnects {
                phase = .ended("重连 \(reconnects) 次都没成，先停下")
                teardown()
            }
            return
        }
        guard let token = WatchTokenStore.load() else {
            phase = .ended("token 没了")
            teardown()
            return
        }
        reconnects += 1
        phase = .reconnecting("第 \(reconnects) 次重连…")
        streamId = nil                        // 新连接会下发新的
        connection?.cancel()
        connection = nil
        openSocket(token: token)
    }

    private func startWatchdog() {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, !self.isEnded else { return }
                let quiet = Date().timeIntervalSince(self.lastInboundAt)
                self.silentSeconds = Int(quiet)
                if quiet > Self.stallSeconds, case .live = self.phase {
                    self.reconnect()
                }
            }
        }
    }

    /// 音频中断。**这条是平台红线，必须说出来。**
    private func observeInterruptions() {
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(), queue: .main
        ) { [weak self] note in
            Task { @MainActor in
                guard let self else { return }
                let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
                guard raw == AVAudioSession.InterruptionType.began.rawValue else { return }
                self.interrupted = true
                // ⚠ watchOS 不允许在后台重新激活录音（Apple 框架工程师原话），
                // 所以这里**不尝试恢复** —— 假装能恢复只会变成一个还连着、
                // 但永远没有声音的通话。如实说，让人回到 App 重新发起。
                self.phase = .ended("被系统打断了（Siri/来电等）——回到 App 重新开始")
                self.teardown()
            }
        }
    }

    // ── 停止 ──

    func stop() {
        command("stop")
        phase = .ended("已挂断")
        teardown()
    }

    private func teardown() {
        watchdog?.cancel()
        watchdog = nil
        connection?.cancel()
        connection = nil
        streamId = nil
        player?.stop()
        player = nil
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        engine = nil
        converter = nil
        audioReady = false
        NotificationCenter.default.removeObserver(
            self, name: AVAudioSession.interruptionNotification, object: nil)
        try? AVAudioSession.sharedInstance().setActive(false)
    }

    func reset() {
        teardown()
        phase = .idle
    }

    enum CallError: LocalizedError {
        case microphoneDenied
        case formatUnavailable
        var errorDescription: String? {
            switch self {
            case .microphoneDenied: return "没有麦克风权限，去手机的 Watch App 里开"
            case .formatUnavailable: return "建不出 48kHz 单声道格式"
            }
        }
    }
}

#endif
