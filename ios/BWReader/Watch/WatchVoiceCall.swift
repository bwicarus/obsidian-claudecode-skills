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
    /// 收到的音频峰值(0~1)。**摆在界面上是为了分辨"下行本来就轻"和
    /// "手表放不响"** —— 这两者修法完全不同,光听分不出来。
    @Published private(set) var inboundPeak: Float = 0
    /// 通话中收到的卡片。⚠ 只留最近几张:手表屏幕小,而且**通话中的卡片
    /// 是即时信息**,翻历史不是这个界面该干的事。
    @Published private(set) var liveCards: [LiveCard] = []

    struct LiveCard: Identifiable {
        let id = UUID()
        let title: String
        let text: String
    }

    private static let endpoint = URL(
        string: "wss://bwicarus.taile44d0c.ts.net:8443/watch-voice")!
    /// 多久没收到东西就认为断了。Pi 在通话中会持续回下行音频，
    /// 所以 3 秒的沉默已经不正常。
    private static let stallSeconds: TimeInterval = 3
    /// 重连中每隔多久再试一次。比 stallSeconds 长一点,给新连接握手的时间。
    private static let retrySeconds: TimeInterval = 6

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
    /// 这一通电话已经自动重来过一次了。**只重来一次** ——
    /// 无限重来会变成一个自己转圈的东西,而人看不出它在转圈。
    private var restartedOnce = false
    /// 路由与音量相关的实况。⚠ 摆在界面上：`.defaultToSpeaker` 在 watchOS
    /// 上是否被接受、实际走了哪个输出口 —— 这些**只能在真机上看见**。
    @Published private(set) var routeNote = ""

    /// socket 级重连的次数上限。⚠ 从 30 降到 5:实测掉线多半是 ENETDOWN
    /// (豁免没了),那种情况下重建 socket 注定失败 —— 试 30 次只是把"整体
    /// 重来"这个唯一有效的手段推迟了半分钟。
    private static let maximumReconnects = 5

    /// 音频会话模式。**做成可切是因为"哪个更响"只能在真机上比出来。**
    ///
    /// 实测背景（2026-08-28）：入声峰值 0.28（≈ -11 dBFS，语音的正常电平），
    /// 也就是**下行数据没问题**；而 4× 增益已经在削顶了。数字信号接近满刻度
    /// 还是小声 → 问题在物理输出路径，最可疑的就是 `.voiceChat` 那套自带 AGC
    /// 的语音处理链。
    ///
    /// ⚠ 每一档的代价都写出来，不然切了不知道自己换到了什么：
    enum Mode: String, CaseIterable, Identifiable {
        /// ✅ **实测最好的一档**（用户 2026-08-28）。带回声消除。
        /// ⚠ 去掉它，手表扬声器的声音会被自己的麦克风录回去，绕一圈送回
        /// 电脑 —— 轻则回声，重则啸叫。
        case voiceChat = "通话·有回声消除（推荐）"
        /// 不做语音处理。⚠ **没有回声消除** —— 外放时可能啸叫，戴耳机没事。
        case plain = "默认（无回声消除）"
        /// ⚠ **实测最小声的一档**，比 voiceChat 还轻。留着只是为了记录
        /// 「已经试过、更差」—— 删掉的话下次有人还会去试一遍。
        case videoChat = "视频通话档（实测最小声）"

        var id: String { rawValue }

        var session: AVAudioSession.Mode {
            switch self {
            case .voiceChat: return .voiceChat
            case .plain: return .default
            case .videoChat: return .videoChat
            }
        }
    }

    /// 当前档。⚠ 默认 `.voiceChat` 不只是"最安全"，**实测它也是最好的**
    /// （2026-08-28：videoChat 最小声，voiceChat + 3.2× 增益音量正常）。
    /// 所以这个默认值是有依据的，别为了"试试别的"随手改。
    @Published var mode: Mode = .voiceChat
    /// 播放增益。手表扬声器本来就小,语音的动态范围又窄。
    /// ⚠ 配合硬限幅使用 —— 削波的破音比小声更难听。
    /// ⚠ 实测入声峰值 0.28,乘 4 会到 1.12 —— **已经在削顶**。
    /// 削波的破音比小声更难听,所以退到刚好推满而不过头。
    /// 再想更响只能从输出路径（Mode）想办法,加增益已经没有余量了。
    private static let playbackGain: Float = 3.2

    // ── 开始 ──

    func start() {
        guard phase == .idle || isEnded else { return }
        phase = .connecting
        framesSent = 0
        framesPlayed = 0
        sequence = 0
        reconnects = 0
        interrupted = false
        restartedOnce = false
        routeNote = ""
        inboundPeak = 0
        liveCards = []
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

        // ⚠ **`.defaultToSpeaker` 在 watchOS 上不存在**（编译器直接拒绝，
        // 2026-08-28 验证）。所以「音量小是因为没走扬声器」这条猜测被排除了 ——
        // 不是"设了没生效"，是压根没有这个开关。
        //
        // 保留 `.voiceChat` 是有代价的取舍：它带回声消除，而手表的扬声器和
        // 麦克风挨着，去掉它会把自己的输出录回去、绕一圈送回电脑。
        // 宁可小声也不要啸叫 —— 音量靠下面的软件增益补。
        try session.setCategory(.playAndRecord, mode: mode.session)
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
        // 显式拉满 —— 默认就是 1.0,写出来是为了让"音量在哪被压低"这个问题
        // 少一个可疑对象。
        made.mainMixerNode.outputVolume = 1.0
        node.volume = 1.0
        try made.start()
        node.play()
        routeNote = (routeNote.isEmpty ? "" : routeNote + " · ")
            + "输出：" + session.currentRoute.outputs
                .map(\.portName).joined(separator: "/")

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

        // ⚠ **先量峰值再放大。**
        // 「声音太小」有两种完全不同的原因：下行音频**本身**就轻(Windows 那头
        // 采到的就小),或者手表的播放路径不对。两者修法完全不同,而光听是
        // 分不出来的 —— 所以把收到的峰值量出来摆在界面上。
        // 这跟这个项目一路走来的那条规则是同一件事：**先能分辨,再谈修。**
        var peak: Float = 0
        payload.withUnsafeBytes { raw in
            let source = raw.bindMemory(to: Int16.self)
            for index in 0..<samples {
                let sample = Float(Int16(littleEndian: source[index])) / 32768.0
                if abs(sample) > peak { peak = abs(sample) }
                // 软件增益。手表扬声器本来就小,而语音的动态范围窄,
                // 适度放大是常规做法。⚠ 必须硬限幅,否则削波的破音比小声更难听。
                let boosted = max(-1.0, min(1.0, sample * Self.playbackGain))
                for channel in 0..<Int(format.channelCount) {
                    channels[channel][index] = boosted
                }
            }
        }
        inboundPeak = max(inboundPeak * 0.95, peak)   // 慢衰减,便于人眼读
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
            let why = String(describing: error)
            // ⚠ **ENETDOWN 不是"网络抖了一下"** —— 它是 TN3135 拒绝低层网络的
            // 签名(POSIX 50 / "Network is down"),意味着音频会话那个豁免没了。
            // 那种情况下**重建 socket 永远没用**,只有重新激活音频会话才可能救。
            // 所以直接跳过 socket 级重试,整体重来。
            if why.contains("rawValue: 50") || why.contains("Network is down") {
                autoRestartIfPossible("低层网络被拒（音频豁免没了）")
                return
            }
            phase = .reconnecting(why)
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
        // 通话中 AI 发来的卡片。⚠ 它走的是**下行文本通道**,不是手机那条
        // WCSession 镜像 —— 手表单独在线时手机可能根本没开。
        // 两条路都保留:有手机时走手机(带缩略图),没手机时走这条(纯文字)。
        if let event = row["ev"] as? String, event == "card" {
            let card = row["card"] as? [String: Any] ?? [:]
            let title = (card["title"] as? String) ?? (card["kind"] as? String) ?? "卡片"
            let text = (card["text"] as? String)
                ?? (card["body"] as? String) ?? ""
            liveCards.insert(LiveCard(title: title, text: text), at: 0)
            if liveCards.count > 6 { liveCards.removeLast() }
            return
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
                // 轻量重连试到头了 —— 换成整个重来一次(前台才行)。
                autoRestartIfPossible("重连 \(reconnects) 次都没成")
            }
            return
        }
        guard let token = WatchTokenStore.load() else {
            phase = .ended("token 没了")
            teardown()
            return
        }
        reconnects += 1
        // ⚠ 必须重置:不重置的话 quiet 一直在涨,看门狗每秒都判"卡住"并再次
        // 重连,几秒就把重试额度烧光 —— 看上去像"试了很多次",其实一次都没
        // 给新连接握手的时间。
        lastInboundAt = Date()
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
                // ⚠ **重连中也要接着试。** 原来只在 .live 时重连,于是第一次
                // 重连失败之后 phase 停在 .reconnecting,看门狗从此不管它 ——
                // 表现就是"断了不会自己接回来"。
                // 这是同一个错误的第五次:**实验/实现里只试一次就放弃**,
                // 而网络恢复往往要几秒到几十秒。
                let stuck: Bool
                switch self.phase {
                case .live: stuck = quiet > Self.stallSeconds
                case .reconnecting: stuck = quiet > Self.retrySeconds
                default: stuck = false
                }
                if stuck { self.reconnect() }
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
                    // ⚠ 音频被打断在**后台**是不可恢复的(Apple 明说),
                // 但在前台整个重来是可以的 —— 所以交给 autoRestartIfPossible
                // 去分辨,而不是一律判死。
                self.autoRestartIfPossible("被系统打断了（Siri/来电等）")
            }
        }
    }

    // ── 停止 ──

    /// 掉线之后整个重来一次。
    ///
    /// ## ⚠ 为什么是「整个重来」而不是更聪明的重连
    ///
    /// 用户 2026-08-28 实测:分层重连(只重建 socket、保留音频会话)**没真的
    /// 接回来**,而他的判断是「断开后再自动开启一次就好了,不需要搞得很复杂」。
    /// 这是对的 —— 一个复杂的恢复路径,如果它自己也可能坏,那它只是多了一处
    /// 会静默失败的地方。
    ///
    /// ## ⚠ 但有一条边界:只在**前台**重来
    ///
    /// 整个重来意味着重新激活音频会话,而 Apple 明说 **watchOS 后台无法重新
    /// 激活录音**。所以在后台自动重来必然失败 —— 而且失败得悄无声息,
    /// 变成一个"看起来在重试、其实永远起不来"的循环。
    /// 后台就如实说「抬腕回到 App」,不假装能修。
    private func autoRestartIfPossible(_ reason: String) {
        // ⚠ 已经整体重来过一次还是不行 —— **必须说出来**。
        // 原来这里是 `guard !restartedOnce else { return }`,悄悄什么都不做,
        // phase 就永远停在「重连中」。用户 2026-08-28 看到的正是这个:
        // 界面写着在重连,实际上早就放弃了。
        // 这是同一个错误在这一天里的第六次:**放弃要出声。**
        guard !restartedOnce else {
            phase = .ended(reason + "，重来一次也没成 —— 手动再打一次试试")
            teardown()
            return
        }
        guard WKApplication.shared().applicationState == .active else {
            phase = .ended(reason + "（回到 App 重新开始）")
            return
        }
        restartedOnce = true
        phase = .reconnecting("断了，自动重来…")
        teardown()
        Task {
            // 给音频栈一点时间彻底放开,否则重新激活会撞上还没释放的会话。
            try? await Task.sleep(for: .seconds(1))
            self.restartedOnce = true          // teardown 不清它
            self.beginRestart()
        }
    }

    private func beginRestart() {
        // ⚠ 不清的话会叠成「输出: 扬声器 · 输出: 扬声器」——
        // 用户 2026-08-28 的截图正是靠这个重复才看出"重来确实发生过"。
        // 保留这条线索的价值,但让它以正确的形式出现:重来的次数单独记。
        routeNote = ""
        phase = .connecting
        framesSent = 0
        framesPlayed = 0
        sequence = 0
        reconnects = 0
        interrupted = false
        pending.removeAll(keepingCapacity: true)
        Task { await begin() }
    }

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
