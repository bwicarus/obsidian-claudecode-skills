import Foundation

/// The Reader `/voice-rt?mode=agent` contract.  Doubao remains the streaming
/// ASR/TTS transport; the existing Reader assistant is still the brain.  A
/// consumer must forward `didFinalizeUtterance` into the normal assistant send
/// path and feed assistant answer fragments back through `speak`.
struct NativeAgentVoiceContext: Sendable, Equatable {
    var fileRelativePath: String?
    var page: Int?

    init(fileRelativePath: String? = nil, page: Int? = nil) {
        self.fileRelativePath = fileRelativePath
        self.page = page
    }
}

struct NativeAgentVoiceConfiguration: Sendable, Equatable {
    static let production = NativeAgentVoiceConfiguration(
        endpoint: URL(
            string: "wss://bwicarus.taile44d0c.ts.net/voice-rt"
        )!,
        origin: "https://bwicarus.taile44d0c.ts.net"
    )

    let endpoint: URL
    let origin: String
    let additionalHTTPHeaders: [String: String]

    init(
        endpoint: URL,
        origin: String,
        additionalHTTPHeaders: [String: String] = [:]
    ) {
        self.endpoint = endpoint
        self.origin = origin
        self.additionalHTTPHeaders = additionalHTTPHeaders
    }

    func endpoint(for context: NativeAgentVoiceContext) -> URL {
        guard var components = URLComponents(
            url: endpoint,
            resolvingAgainstBaseURL: false
        ) else {
            return endpoint
        }
        var query = components.queryItems ?? []
        query.removeAll { item in
            item.name == "mode" || item.name == "file" || item.name == "page"
        }
        query.append(URLQueryItem(name: "mode", value: "agent"))
        if let file = context.fileRelativePath?
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !file.isEmpty {
            query.append(URLQueryItem(name: "file", value: file))
        }
        if let page = context.page, page > 0 {
            query.append(URLQueryItem(name: "page", value: String(page)))
        }
        components.queryItems = query
        return components.url ?? endpoint
    }
}

enum NativeAgentVoiceState: Sendable, Equatable {
    case idle
    case requestingMicrophone
    case connecting
    case listening
    case speaking
    case suspended
    case stopping
    case failed(String)
}

@MainActor
protocol NativeAgentVoiceSessionDelegate: AnyObject {
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didChangeState state: NativeAgentVoiceState
    )
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didUpdateTranscript text: String
    )
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didFinalizeUtterance text: String
    )
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didReceiveSpokenSegment text: String
    )
    func nativeAgentVoiceSessionDidFinishSpeaking(
        _ session: NativeAgentVoiceSession
    )
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didFail error: Error
    )
}

extension NativeAgentVoiceSessionDelegate {
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didChangeState state: NativeAgentVoiceState
    ) {}

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didUpdateTranscript text: String
    ) {}

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didFinalizeUtterance text: String
    ) {}

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didReceiveSpokenSegment text: String
    ) {}

    func nativeAgentVoiceSessionDidFinishSpeaking(
        _ session: NativeAgentVoiceSession
    ) {}

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didFail error: Error
    ) {}
}

/// Owns the native audio session and the agent-mode WSS.  This object is UI
/// agnostic: it does not create assistant turns itself and does not claim to
/// implement S2S, GPT RTC, or the Reader tool pipeline.
@MainActor
final class NativeAgentVoiceSession {
    enum SessionFailure: LocalizedError {
        case microphoneDenied
        case microphonePipelineUnavailable
        case invalidRelayMessage
        case relay(String)
        case disconnected(String)

        var errorDescription: String? {
            switch self {
            case .microphoneDenied:
                return "没有获得 iPad 麦克风权限"
            case .microphonePipelineUnavailable:
                return "无法建立原生语音上行队列"
            case .invalidRelayMessage:
                return "语音中继返回了无效消息"
            case .relay(let message):
                return message.isEmpty ? "语音中继返回错误" : message
            case .disconnected(let message):
                return message.isEmpty ? "语音连接已断开" : message
            }
        }
    }

    weak var delegate: NativeAgentVoiceSessionDelegate?

    private(set) var state: NativeAgentVoiceState = .idle {
        didSet {
            guard oldValue != state else { return }
            delegate?.nativeAgentVoiceSession(self, didChangeState: state)
        }
    }

    var isInputMuted: Bool { audio.isInputMuted }

    private let configuration: NativeAgentVoiceConfiguration
    private let audio = NativeAudioEngine()
    private var socket: NativeAgentVoiceSocket?
    private var microphoneConsumer: Task<Void, Never>?
    private var microphoneContinuation: AsyncStream<Data>.Continuation?
    private var playbackSamples24k: [Int16] = []
    private var playbackFrames48k: [[Int16]] = []
    private var playbackFrameIndex = 0
    private var playbackPump: Task<Void, Never>?
    private var pendingSpokenSegments: [String] = []
    private var generation: UInt64 = 0
    private var desiredActive = false
    private var speechActive = false
    private var relayTTSEnded = false
    private var interruptionWasSpeaking = false
    private var audioSuspended = false

    init(
        configuration: NativeAgentVoiceConfiguration = .production,
        delegate: NativeAgentVoiceSessionDelegate? = nil
    ) {
        self.configuration = configuration
        self.delegate = delegate

        audio.onFailure = { [weak self] error in
            Task { @MainActor [weak self] in
                await self?.fail(error)
            }
        }
        audio.onInterruption = { [weak self] interruption in
            Task { @MainActor [weak self] in
                await self?.handleInterruption(interruption)
            }
        }
    }

    func start(context: NativeAgentVoiceContext = .init()) async {
        guard state == .idle || isFailed else { return }

        generation &+= 1
        let currentGeneration = generation
        desiredActive = true
        speechActive = false
        relayTTSEnded = false
        audioSuspended = false
        interruptionWasSpeaking = false
        playbackSamples24k.removeAll(keepingCapacity: true)
        pendingSpokenSegments.removeAll(keepingCapacity: true)
        clearPlaybackQueue()
        state = .requestingMicrophone

        guard await requestMicrophonePermission() else {
            await fail(SessionFailure.microphoneDenied)
            return
        }
        guard currentGeneration == generation, desiredActive else { return }

        let socket = NativeAgentVoiceSocket(
            configuration: configuration,
            context: context
        ) { [weak self] event in
            Task { @MainActor [weak self] in
                await self?.handle(event, generation: currentGeneration)
            }
        }
        self.socket = socket

        do {
            state = .connecting
            try await socket.connect()
            guard currentGeneration == generation, desiredActive else {
                await socket.disconnect()
                return
            }
            try audio.start()
            try startMicrophonePipeline(
                socket: socket,
                generation: currentGeneration
            )
        } catch {
            guard currentGeneration == generation else { return }
            await fail(error)
        }
    }

    func stop() async {
        guard state != .idle else { return }

        generation &+= 1
        desiredActive = false
        state = .stopping
        stopMicrophonePipeline()
        playbackSamples24k.removeAll(keepingCapacity: false)
        pendingSpokenSegments.removeAll(keepingCapacity: false)
        clearPlaybackQueue()
        speechActive = false
        relayTTSEnded = false
        audioSuspended = false
        interruptionWasSpeaking = false
        audio.stop()
        let oldSocket = socket
        socket = nil
        await oldSocket?.finishAndDisconnect()
        state = .idle
    }

    /// Feeds one already-cleaned assistant answer fragment into the relay TTS.
    /// Call `finishSpeaking()` once the assistant has produced the full turn.
    func speak(_ text: String, mood: String? = nil) async throws {
        let bounded = text
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !bounded.isEmpty else { return }
        guard let socket, desiredActive else {
            throw SessionFailure.disconnected("语音连接尚未建立")
        }
        speechActive = true
        relayTTSEnded = false
        if !audioSuspended {
            state = .speaking
        }
        try await socket.speak(
            String(bounded.prefix(8_000)),
            mood: mood.map { String($0.prefix(80)) }
        )
    }

    func finishSpeaking() async throws {
        guard let socket, desiredActive else {
            throw SessionFailure.disconnected("语音连接尚未建立")
        }
        try await socket.finishSpeaking()
    }

    /// Immediately closes the relay-side TTS session.  The microphone and ASR
    /// WSS stay active, matching the existing browser agent-mode semantics.
    func cancelSpeaking() async {
        speechActive = false
        relayTTSEnded = false
        playbackSamples24k.removeAll(keepingCapacity: true)
        pendingSpokenSegments.removeAll(keepingCapacity: true)
        clearPlaybackQueue()
        if desiredActive, !audioSuspended {
            state = .listening
        }
        try? await socket?.cancelSpeaking()
    }

    func setInputMuted(_ muted: Bool) throws {
        try audio.setInputMuted(muted)
    }

    private var isFailed: Bool {
        if case .failed = state { return true }
        return false
    }

    private func requestMicrophonePermission() async -> Bool {
        await withCheckedContinuation { continuation in
            audio.requestMicrophonePermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    private func startMicrophonePipeline(
        socket: NativeAgentVoiceSocket,
        generation: UInt64
    ) throws {
        var continuation: AsyncStream<Data>.Continuation?
        let stream = AsyncStream<Data>(
            bufferingPolicy: .bufferingNewest(25)
        ) {
            continuation = $0
        }
        guard let continuation else {
            throw SessionFailure.microphonePipelineUnavailable
        }

        microphoneContinuation = continuation
        audio.onMicrophoneFrame = { samples48k in
            continuation.yield(Self.encodePCM16k(from48k: samples48k))
        }
        microphoneConsumer = Task { [weak self] in
            for await frame in stream {
                guard !Task.isCancelled else { break }
                do {
                    try await socket.sendMicrophonePCM(frame)
                } catch {
                    guard !Task.isCancelled else { return }
                    await self?.failIfCurrent(error, generation: generation)
                    return
                }
            }
        }
    }

    private func stopMicrophonePipeline() {
        audio.onMicrophoneFrame = nil
        microphoneContinuation?.finish()
        microphoneContinuation = nil
        microphoneConsumer?.cancel()
        microphoneConsumer = nil
    }

    private func handle(
        _ event: NativeAgentVoiceSocket.Event,
        generation currentGeneration: UInt64
    ) async {
        guard currentGeneration == generation, desiredActive else { return }

        switch event {
        case .agentReady:
            state = .listening
        case .transcript(let text):
            // Agent-mode barge-in: the first new ASR text cancels the old TTS
            // session, while the ASR WSS and microphone remain alive.
            if speechActive {
                await cancelSpeaking()
            }
            delegate?.nativeAgentVoiceSession(
                self,
                didUpdateTranscript: text
            )
        case .utterance(let text):
            delegate?.nativeAgentVoiceSession(
                self,
                didFinalizeUtterance: text
            )
        case .spokenSegment(let text):
            pendingSpokenSegments.append(text)
        case .audio24k(let data):
            do {
                speechActive = true
                if !audioSuspended {
                    state = .speaking
                }
                if !pendingSpokenSegments.isEmpty {
                    let text = pendingSpokenSegments.removeFirst()
                    delegate?.nativeAgentVoiceSession(
                        self,
                        didReceiveSpokenSegment: text
                    )
                }
                try enqueuePCM24k(data)
            } catch {
                await fail(error)
            }
        case .ttsEnded:
            flushPCM24kTail()
            relayTTSEnded = true
            completeSpeechIfDrained()
        case .relayError(let message):
            await fail(SessionFailure.relay(message))
        case .disconnected(let message):
            await fail(SessionFailure.disconnected(message))
        }
    }

    private func enqueuePCM24k(_ data: Data) throws {
        guard data.count.isMultiple(of: 2) else {
            throw SessionFailure.invalidRelayMessage
        }
        let bytes = [UInt8](data)
        playbackSamples24k.reserveCapacity(
            playbackSamples24k.count + bytes.count / 2
        )
        for offset in stride(from: 0, to: bytes.count, by: 2) {
            let bits = UInt16(bytes[offset])
                | (UInt16(bytes[offset + 1]) << 8)
            playbackSamples24k.append(Int16(bitPattern: bits))
        }

        // 480 samples = 20 ms at 24 kHz.  Exact 2x duplication preserves
        // duration and framing and avoids chunk-boundary state; the existing
        // NativeAudioEngine then schedules one 960-sample / 48 kHz frame.
        while playbackSamples24k.count >= 480 {
            let source = playbackSamples24k.prefix(480)
            playbackSamples24k.removeFirst(480)
            var output = [Int16]()
            output.reserveCapacity(960)
            for sample in source {
                output.append(sample)
                output.append(sample)
            }
            playbackFrames48k.append(output)
        }
        startPlaybackPumpIfNeeded()
    }

    /// Relay chunks are transport-sized, not guaranteed to end on our 20 ms
    /// playback boundary. Pad only the final partial frame so samples cannot
    /// leak into the next assistant turn.
    private func flushPCM24kTail() {
        guard !playbackSamples24k.isEmpty else { return }
        var output = [Int16]()
        output.reserveCapacity(960)
        for sample in playbackSamples24k.prefix(480) {
            output.append(sample)
            output.append(sample)
        }
        while output.count < NativeAudioEngine.samplesPerFrame {
            output.append(0)
        }
        playbackSamples24k.removeAll(keepingCapacity: true)
        playbackFrames48k.append(output)
        startPlaybackPumpIfNeeded()
    }

    private func startPlaybackPumpIfNeeded() {
        guard !audioSuspended,
              playbackPump == nil,
              playbackFrameIndex < playbackFrames48k.count else {
            completeSpeechIfDrained()
            return
        }
        let currentGeneration = generation
        playbackPump = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled,
                  currentGeneration == self.generation,
                  self.desiredActive,
                  self.playbackFrameIndex < self.playbackFrames48k.count {
                let frame = self.playbackFrames48k[self.playbackFrameIndex]
                self.playbackFrameIndex += 1
                do {
                    try self.audio.enqueuePlayback(frame)
                } catch {
                    self.playbackPump = nil
                    await self.fail(error)
                    return
                }
                do {
                    try await Task.sleep(nanoseconds: 20_000_000)
                } catch {
                    return
                }
            }
            guard currentGeneration == self.generation else { return }
            self.playbackPump = nil
            if self.playbackFrameIndex >= self.playbackFrames48k.count {
                self.playbackFrames48k.removeAll(keepingCapacity: true)
                self.playbackFrameIndex = 0
                self.completeSpeechIfDrained()
            } else {
                self.startPlaybackPumpIfNeeded()
            }
        }
    }

    private func clearPlaybackQueue() {
        playbackPump?.cancel()
        playbackPump = nil
        playbackFrames48k.removeAll(keepingCapacity: false)
        playbackFrameIndex = 0
    }

    private func completeSpeechIfDrained() {
        guard relayTTSEnded,
              playbackPump == nil,
              playbackFrameIndex >= playbackFrames48k.count else {
            return
        }
        speechActive = false
        relayTTSEnded = false
        if audioSuspended {
            interruptionWasSpeaking = false
        }
        if desiredActive, state != .suspended {
            state = .listening
        }
        delegate?.nativeAgentVoiceSessionDidFinishSpeaking(self)
    }

    private func handleInterruption(
        _ interruption: NativeAudioEngine.Interruption
    ) async {
        switch interruption {
        case .began:
            guard desiredActive else { return }
            interruptionWasSpeaking = speechActive
            audioSuspended = true
            playbackPump?.cancel()
            playbackPump = nil
            audio.stop()
            state = .suspended
        case .ended(let shouldResume):
            guard desiredActive, state == .suspended else { return }
            guard shouldResume else {
                await fail(SessionFailure.disconnected(
                    "系统音频中断结束，但未允许自动恢复"
                ))
                return
            }
            do {
                try audio.start()
                audioSuspended = false
                state = interruptionWasSpeaking ? .speaking : .listening
                interruptionWasSpeaking = false
                startPlaybackPumpIfNeeded()
            } catch {
                await fail(error)
            }
        }
    }

    private func failIfCurrent(
        _ error: Error,
        generation currentGeneration: UInt64
    ) async {
        guard currentGeneration == generation else { return }
        await fail(error)
    }

    private func fail(_ error: Error) async {
        guard state != .idle, !isFailed else { return }
        generation &+= 1
        desiredActive = false
        stopMicrophonePipeline()
        playbackSamples24k.removeAll(keepingCapacity: false)
        pendingSpokenSegments.removeAll(keepingCapacity: false)
        clearPlaybackQueue()
        speechActive = false
        relayTTSEnded = false
        audioSuspended = false
        interruptionWasSpeaking = false
        audio.stop()
        let oldSocket = socket
        socket = nil
        await oldSocket?.disconnect()
        state = .failed(error.localizedDescription)
        delegate?.nativeAgentVoiceSession(self, didFail: error)
    }

    /// NativeAudioEngine supplies 960 samples / 20 ms at 48 kHz.  A three-tap
    /// box filter provides a bounded anti-aliasing step before exact 3:1
    /// decimation, yielding the relay's 320 samples / 640 bytes at 16 kHz.
    private nonisolated static func encodePCM16k(
        from48k samples: [Int16]
    ) -> Data {
        guard samples.count == NativeAudioEngine.samplesPerFrame else {
            return Data()
        }
        var bytes = [UInt8]()
        bytes.reserveCapacity(640)
        for offset in stride(from: 0, to: samples.count, by: 3) {
            let sum = Int32(samples[offset])
                + Int32(samples[offset + 1])
                + Int32(samples[offset + 2])
            let filtered = Int16(clamping: sum / 3)
            let bits = UInt16(bitPattern: filtered)
            bytes.append(UInt8(truncatingIfNeeded: bits))
            bytes.append(UInt8(truncatingIfNeeded: bits >> 8))
        }
        return Data(bytes)
    }
}

private actor NativeAgentVoiceSocket {
    enum Event: Sendable, Equatable {
        case agentReady
        case transcript(String)
        case utterance(String)
        case spokenSegment(String)
        case audio24k(Data)
        case ttsEnded
        case relayError(String)
        case disconnected(String)
    }

    typealias EventHandler = @Sendable (Event) -> Void

    private struct RelayEnvelope: Decodable {
        struct Payload: Decodable {
            let text: String?
            let error: String?
        }

        enum EventCode: Decodable {
            case name(String)
            case number(Int)

            init(from decoder: Decoder) throws {
                let container = try decoder.singleValueContainer()
                if let name = try? container.decode(String.self) {
                    self = .name(name)
                    return
                }
                self = .number(try container.decode(Int.self))
            }
        }

        let event: EventCode
        let payload: Payload?
    }

    private let configuration: NativeAgentVoiceConfiguration
    private let context: NativeAgentVoiceContext
    private let eventHandler: EventHandler
    private let decoder = JSONDecoder()
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var intentionalClose = false

    init(
        configuration: NativeAgentVoiceConfiguration,
        context: NativeAgentVoiceContext,
        eventHandler: @escaping EventHandler
    ) {
        self.configuration = configuration
        self.context = context
        self.eventHandler = eventHandler
    }

    func connect() async throws {
        guard socket == nil else { return }

        intentionalClose = false
        var request = URLRequest(url: configuration.endpoint(for: context))
        request.setValue(configuration.origin, forHTTPHeaderField: "Origin")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        for (name, value) in configuration.additionalHTTPHeaders {
            request.setValue(value, forHTTPHeaderField: name)
        }
        request.timeoutInterval = 15

        let urlConfiguration = URLSessionConfiguration.ephemeral
        urlConfiguration.timeoutIntervalForRequest = 15
        urlConfiguration.timeoutIntervalForResource = 3_600
        urlConfiguration.waitsForConnectivity = false
        let session = URLSession(configuration: urlConfiguration)
        let socket = session.webSocketTask(with: request)
        socket.maximumMessageSize = 10 * 1_024 * 1_024
        self.session = session
        self.socket = socket
        socket.resume()

        receiveTask = Task { [weak self, weak socket] in
            guard let self, let socket else { return }
            await self.receiveLoop(socket)
        }
    }

    func sendMicrophonePCM(_ data: Data) async throws {
        guard let socket, data.count == 640 else {
            throw NativeAgentVoiceSession.SessionFailure.disconnected(
                "麦克风 PCM 帧或语音连接无效"
            )
        }
        try await socket.send(.data(data))
    }

    func speak(_ text: String, mood: String?) async throws {
        var payload: [String: Any] = [
            "type": "speak",
            "text": text,
        ]
        if let mood, !mood.isEmpty {
            payload["mood"] = mood
        }
        try await sendJSON(payload)
    }

    func finishSpeaking() async throws {
        try await sendJSON(["type": "speak_done"])
    }

    func cancelSpeaking() async throws {
        try await sendJSON(["type": "cancel"])
    }

    func finishAndDisconnect() async {
        if socket != nil {
            // `finish` is advisory; a half-dead network must never keep STOP
            // (and every later START in the serialized command tail) blocked.
            let finishTask = Task { [weak self] in
                try? await self?.sendJSON(["type": "finish"])
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
            finishTask.cancel()
        }
        await close(intentional: true)
    }

    func disconnect() async {
        await close(intentional: true)
    }

    private func sendJSON(_ object: [String: Any]) async throws {
        guard let socket else {
            throw NativeAgentVoiceSession.SessionFailure.disconnected(
                "语音连接尚未建立"
            )
        }
        let data = try JSONSerialization.data(
            withJSONObject: object,
            options: []
        )
        guard let text = String(data: data, encoding: .utf8) else {
            throw NativeAgentVoiceSession.SessionFailure.invalidRelayMessage
        }
        try await socket.send(.string(text))
    }

    private func receiveLoop(_ expectedSocket: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            do {
                let message = try await expectedSocket.receive()
                guard expectedSocket === socket else { return }
                switch message {
                case .data(let data):
                    guard data.count.isMultiple(of: 2) else {
                        throw NativeAgentVoiceSession.SessionFailure
                            .invalidRelayMessage
                    }
                    eventHandler(.audio24k(data))
                case .string(let text):
                    try handleText(text)
                @unknown default:
                    throw NativeAgentVoiceSession.SessionFailure
                        .invalidRelayMessage
                }
            } catch is CancellationError {
                return
            } catch {
                guard !intentionalClose, expectedSocket === socket else {
                    return
                }
                eventHandler(.disconnected(error.localizedDescription))
                await close(intentional: false)
                return
            }
        }
    }

    private func handleText(_ text: String) throws {
        guard let data = text.data(using: .utf8) else {
            throw NativeAgentVoiceSession.SessionFailure.invalidRelayMessage
        }
        let envelope = try decoder.decode(RelayEnvelope.self, from: data)
        let payload = envelope.payload

        switch envelope.event {
        case .name("agent_ready"):
            eventHandler(.agentReady)
        case .name("asr"):
            if let text = payload?.text, !text.isEmpty {
                eventHandler(.transcript(text))
            }
        case .name("utterance"):
            if let text = payload?.text, !text.isEmpty {
                eventHandler(.utterance(text))
            }
        case .name("tts_seg"):
            if let text = payload?.text, !text.isEmpty {
                eventHandler(.spokenSegment(text))
            }
        case .name("tts_end"):
            eventHandler(.ttsEnded)
        case .number(-1):
            eventHandler(.relayError(payload?.error ?? "语音中继返回错误"))
        case .name, .number:
            // Agent mode may grow additional informational events. Unknown
            // events are ignored, while malformed envelopes still fail closed.
            break
        }
    }

    private func close(intentional: Bool) async {
        intentionalClose = intentional
        receiveTask?.cancel()
        receiveTask = nil
        socket?.cancel(
            with: .normalClosure,
            reason: Data("native-agent-stop".utf8)
        )
        socket = nil
        session?.invalidateAndCancel()
        session = nil
    }
}
