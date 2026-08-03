import Combine
import Foundation

struct NativeVoiceBridgeState: Equatable {
    enum Phase: Equatable {
        case idle
        case preparing
        case connecting
        case starting
        case active
        case suspended
        case stopping
        case failed
    }

    let phase: Phase
    let detail: String?
    let sessionId: String?

    init(
        phase: Phase,
        detail: String?,
        sessionId: String? = nil
    ) {
        self.phase = phase
        self.detail = detail
        self.sessionId = sessionId
    }

    static let idle = NativeVoiceBridgeState(
        phase: .idle,
        detail: nil
    )

    var isActive: Bool {
        phase == .active || phase == .suspended
    }

    var isBusy: Bool {
        switch phase {
        case .preparing, .connecting, .starting, .stopping:
            return true
        case .idle, .active, .suspended, .failed:
            return false
        }
    }

    var title: String {
        switch phase {
        case .idle:
            return "电脑语音未连接"
        case .preparing:
            return "正在准备原生音频"
        case .connecting:
            return "正在连接 Windows"
        case .starting:
            return "正在启动电脑语音"
        case .active:
            return "电脑语音已连接"
        case .suspended:
            return "电脑语音等待恢复"
        case .stopping:
            return "正在停止电脑语音"
        case .failed:
            return "电脑语音连接失败"
        }
    }
}

@MainActor
final class NativeVoiceBridge: ObservableObject {
    private enum BridgeFailure: LocalizedError {
        case readerNotReady
        case microphoneDenied
        case microphonePipelineUnavailable

        var errorDescription: String? {
            switch self {
            case .readerNotReady:
                return "阅读器页面尚未准备好"
            case .microphoneDenied:
                return "没有获得 iPad 麦克风权限"
            case .microphonePipelineUnavailable:
                return "无法建立 iPad 麦克风传输队列"
            }
        }
    }

    @Published private(set) var state: NativeVoiceBridgeState = .idle {
        didSet {
            reader?.updateNativeVoiceButton(state: state)
            if oldValue != state {
                recordDiagnostic(
                    category: "state",
                    message: "\(state.phase): \(state.detail ?? state.title)"
                )
            }
            updateRemoteControls()
        }
    }

    @Published private(set) var microphoneMuted = false
    @Published private(set) var socketState: DirectVoiceState = .disconnected
    @Published private(set) var networkSummary = "checking"
    @Published private(set) var diagnostics: [NativeVoiceDiagnosticEntry] = []

    private weak var reader: ReaderWebViewModel?
    private let audio = NativeAudioEngine()
    private let pathMonitor = NativeVoicePathMonitor()
    private let remoteControls = NativeVoiceRemoteControls()
    private var socket: DirectVoiceSocket?
    private var microphoneConsumer: Task<Void, Never>?
    private var microphoneContinuation: AsyncStream<Data>.Continuation?
    private var operationGeneration: UInt64 = 0
    private var cleanupInProgress = false
    private var desiredActive = false
    private var activeAppKind: DirectVoiceTargetApp = .codexDesktop
    private var intentGeneration: UInt64 = 0
    private var resumeArmed = false
    private var reconnectTask: Task<Void, Never>?
    private var audioInterrupted = false
    private var currentNetworkPath: NativeVoiceNetworkPath?
    private var recoveryInProgress = false
    private var recoveryDisconnectTask: Task<Void, Never>?

    init() {
        audio.onFailure = { [weak self] error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                await self.failActiveSession(
                    error,
                    generation: self.operationGeneration
                )
            }
        }
        audio.onInterruption = { [weak self] interruption in
            Task { @MainActor [weak self] in
                await self?.handleAudioInterruption(interruption)
            }
        }
        audio.onInputMuteChanged = { [weak self] muted in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.microphoneMuted = muted
                self.recordDiagnostic(
                    category: "audio",
                    message: muted ? "麦克风已静音" : "麦克风已恢复"
                )
                self.updateRemoteControls()
            }
        }
        pathMonitor.onUpdate = { [weak self] path in
            Task { @MainActor [weak self] in
                await self?.handleNetworkPath(path)
            }
        }
        pathMonitor.start()
        remoteControls.onStop = { [weak self] in
            Task { @MainActor [weak self] in
                await self?.stop()
            }
        }
        remoteControls.onSetMuted = { [weak self] muted in
            Task { @MainActor [weak self] in
                self?.setMicrophoneMuted(muted)
            }
        }
        remoteControls.onToggleMuted = { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.setMicrophoneMuted(!self.microphoneMuted)
            }
        }
        recordDiagnostic(
            category: "app",
            message: "BWReaderNative \(nativeAppBuildVersion) 已启动"
        )
    }

    var activeTargetName: String {
        activeAppKind.displayName
    }

    func bind(reader: ReaderWebViewModel) {
        self.reader = reader
        reader.bind(nativeVoiceBridge: self)
    }

    func start(
        appKind: DirectVoiceTargetApp = .codexDesktop
    ) async {
        guard !state.isActive, !state.isBusy else {
            return
        }

        operationGeneration &+= 1
        intentGeneration &+= 1
        let generation = operationGeneration
        reconnectTask?.cancel()
        reconnectTask = nil
        cleanupInProgress = false
        recoveryInProgress = false
        desiredActive = true
        resumeArmed = false
        activeAppKind = appKind
        audioInterrupted = false
        recoveryDisconnectTask?.cancel()
        recoveryDisconnectTask = nil
        setMicrophoneMuted(false)
        recordDiagnostic(
            category: "control",
            message: "用户启动 \(appKind.displayName)"
        )
        state = NativeVoiceBridgeState(
            phase: .preparing,
            detail: "正在申请麦克风并交接 Reader 上下文…"
        )

        do {
            guard let reader else {
                throw BridgeFailure.readerNotReady
            }
            guard await requestMicrophonePermission() else {
                throw BridgeFailure.microphoneDenied
            }
            try requireCurrent(generation)
            try await reader.prepareForNativeVoice()
            try requireCurrent(generation)

            try audio.start()
            try requireCurrent(generation)

            state = NativeVoiceBridgeState(
                phase: .connecting,
                detail: "正在建立原生 WSS…"
            )
            let socket = makeSocket(generation: generation)
            self.socket = socket
            recordDiagnostic(category: "protocol", message: "→ HELLO")
            try await connectOnceWithOneRetry(
                socket,
                generation: generation
            )
            try requireCurrent(generation)

            state = NativeVoiceBridgeState(
                phase: .starting,
                detail: "正在等待 Windows 启动 \(appKind.displayName) 语音…"
            )
            recordDiagnostic(
                category: "protocol",
                message: "→ START \(appKind.rawValue)"
            )
            let session = try await socket.start(appKind: appKind)
            try requireCurrent(generation)

            try startMicrophonePipeline(
                socket: socket,
                generation: generation
            )
            state = NativeVoiceBridgeState(
                phase: .active,
                detail: "锁屏或切到后台后仍由原生音频会话保持",
                sessionId: session.id
            )
            resumeArmed = true
        } catch {
            guard generation == operationGeneration else {
                return
            }
            await failStart(error, generation: generation)
        }
    }

    func stop() async {
        guard state.phase != .idle else {
            return
        }

        operationGeneration &+= 1
        intentGeneration &+= 1
        let generation = operationGeneration
        reconnectTask?.cancel()
        reconnectTask = nil
        desiredActive = false
        resumeArmed = false
        audioInterrupted = false
        recoveryInProgress = false
        cleanupInProgress = true
        recordDiagnostic(category: "control", message: "用户挂断")
        state = NativeVoiceBridgeState(
            phase: .stopping,
            detail: "正在关闭 Windows 通话并归还上下文连接…"
        )

        stopMicrophonePipeline()
        audio.stop()
        setMicrophoneMuted(false)

        let pendingDisconnect = recoveryDisconnectTask
        recoveryDisconnectTask = nil
        await pendingDisconnect?.value

        let oldSocket = socket
        socket = nil
        do {
            recordDiagnostic(category: "protocol", message: "→ STOP")
            try await oldSocket?.stop(reason: "native-user-stop")
        } catch {
            await oldSocket?.disconnect()
        }
        guard generation == operationGeneration else {
            return
        }
        socketState = .disconnected
        cleanupInProgress = false
        state = .idle
    }

    func requestReaderContext(
        action: String,
        fields: [String: DirectJSONValue]
    ) async throws -> DirectJSONValue {
        guard state.phase == .active,
              let sessionId = state.sessionId,
              fields["sessionId"] == .string(sessionId),
              let socket else {
            throw DirectVoiceFailure(
                code: "BW_NATIVE_COMPUTER_CONTEXT_INACTIVE",
                message: "原生电脑语音会话未连接",
                retryable: true
            )
        }
        recordDiagnostic(category: "protocol", message: "→ \(action)")
        let result = try await socket.requestReaderContext(
            action: action,
            fields: fields
        )
        recordDiagnostic(category: "protocol", message: "← \(action) ok")
        return result
    }

    private func requestMicrophonePermission() async -> Bool {
        await withCheckedContinuation { continuation in
            audio.requestMicrophonePermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    private func makeSocket(generation: UInt64) -> DirectVoiceSocket {
        DirectVoiceSocket(configuration: .production) {
            [weak self] event in
            Task { @MainActor [weak self] in
                await self?.handle(event, generation: generation)
            }
        }
    }

    private func connectOnceWithOneRetry(
        _ socket: DirectVoiceSocket,
        generation: UInt64
    ) async throws {
        do {
            try await socket.connect()
        } catch let failure as DirectVoiceFailure
            where failure.retryable {
            try requireCurrent(generation)
            state = NativeVoiceBridgeState(
                phase: .connecting,
                detail: "Windows 连接暂未就绪，正在重试一次…"
            )
            await socket.disconnect()
            try await Task.sleep(nanoseconds: 1_000_000_000)
            try requireCurrent(generation)
            try await socket.connect()
        }
    }

    private func startMicrophonePipeline(
        socket: DirectVoiceSocket,
        generation: UInt64
    ) throws {
        var continuation: AsyncStream<Data>.Continuation?
        let stream = AsyncStream<Data>(
            bufferingPolicy: .bufferingNewest(10)
        ) {
            continuation = $0
        }
        guard let continuation else {
            throw BridgeFailure.microphonePipelineUnavailable
        }

        microphoneContinuation = continuation
        audio.onMicrophoneFrame = { samples in
            continuation.yield(Self.encodeMicrophoneSamples(samples))
        }
        microphoneConsumer = Task { [weak self] in
            for await frame in stream {
                guard !Task.isCancelled else { break }
                do {
                    try await socket.sendUplinkPCM(frame)
                } catch {
                    guard !Task.isCancelled else { return }
                    await self?.handleSessionFailure(
                        error,
                        generation: generation
                    )
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
        _ event: DirectVoiceEvent,
        generation: UInt64
    ) async {
        guard generation == operationGeneration else {
            return
        }
        switch event {
        case .state(let directState):
            socketState = directState
            recordDiagnostic(
                category: "socket",
                message: "WSS \(directState.rawValue)"
            )
            switch directState {
            case .connecting, .authenticating:
                if state.phase != .active {
                    state = NativeVoiceBridgeState(
                        phase: .connecting,
                        detail: directState == .authenticating
                            ? "正在校验 Windows 直连协议…"
                            : "正在建立原生 WSS…"
                    )
                }
            case .starting:
                state = NativeVoiceBridgeState(
                    phase: .starting,
                    detail: "正在等待 Windows 启动 Codex Voice…"
                )
            case .active:
                break
            case .stopping:
                state = NativeVoiceBridgeState(
                    phase: .stopping,
                    detail: "Windows 正在停止通话…"
                )
            case .disconnected, .ready, .failed:
                break
            }
        case .runtime(let runtime):
            recordDiagnostic(
                category: "runtime",
                message: "Windows \(runtime.state)"
                    + (runtime.reason.map { ": \($0)" } ?? "")
            )
            if state.isBusy {
                state = NativeVoiceBridgeState(
                    phase: state.phase,
                    detail: runtime.reason ?? "Windows：\(runtime.state)"
                )
            }
        case .downlinkPCM(let frame):
            do {
                try audio.enqueuePlayback(frame.samples)
            } catch {
                await failActiveSession(error, generation: generation)
            }
        case .error(let failure):
            // connect()/start() propagate their own failure to the awaiting
            // start flow.  Runtime failures need an asynchronous full cleanup.
            recordDiagnostic(
                category: "error",
                message: "\(failure.code): \(failure.message)"
            )
            if state.phase == .active {
                await handleSessionFailure(
                    failure,
                    generation: generation
                )
            }
        }
    }

    private func failStart(
        _ error: Error,
        generation: UInt64
    ) async {
        guard generation == operationGeneration else {
            return
        }
        desiredActive = false
        resumeArmed = false
        reconnectTask?.cancel()
        reconnectTask = nil
        audioInterrupted = false
        recoveryInProgress = false
        cleanupInProgress = true
        stopMicrophonePipeline()
        audio.stop()
        setMicrophoneMuted(false)
        let oldSocket = socket
        socket = nil
        await oldSocket?.disconnect()
        guard generation == operationGeneration else {
            return
        }
        socketState = .failed
        cleanupInProgress = false
        state = NativeVoiceBridgeState(
            phase: .failed,
            detail: displayMessage(for: error)
        )
    }

    private func failActiveSession(
        _ error: Error,
        generation: UInt64
    ) async {
        guard generation == operationGeneration,
              !cleanupInProgress else {
            return
        }
        operationGeneration &+= 1
        let cleanupGeneration = operationGeneration
        desiredActive = false
        resumeArmed = false
        reconnectTask?.cancel()
        reconnectTask = nil
        audioInterrupted = false
        recoveryInProgress = false
        cleanupInProgress = true
        stopMicrophonePipeline()
        audio.stop()
        setMicrophoneMuted(false)
        let pendingDisconnect = recoveryDisconnectTask
        recoveryDisconnectTask = nil
        await pendingDisconnect?.value
        let oldSocket = socket
        socket = nil
        await oldSocket?.disconnect()
        guard cleanupGeneration == operationGeneration else {
            return
        }
        socketState = .failed
        cleanupInProgress = false
        state = NativeVoiceBridgeState(
            phase: .failed,
            detail: displayMessage(for: error)
        )
    }

    private func handleAudioInterruption(
        _ interruption: NativeAudioEngine.Interruption
    ) async {
        switch interruption {
        case .began:
            guard desiredActive, state.phase == .active else {
                return
            }
            audioInterrupted = true
            recordDiagnostic(
                category: "audio",
                message: "系统音频中断开始"
            )
            await suspendActiveSession(
                detail: "来电、闹钟或其他 App 暂时占用音频，等待系统允许恢复…",
                stopAudio: true
            )
        case .ended(let shouldResume):
            guard audioInterrupted else {
                return
            }
            audioInterrupted = false
            recordDiagnostic(
                category: "audio",
                message: shouldResume
                    ? "系统允许恢复音频"
                    : "系统未允许自动恢复音频"
            )
            guard shouldResume else {
                await failActiveSession(
                    DirectVoiceFailure(
                        code: "BW_NATIVE_AUDIO_INTERRUPTION_NOT_RESUMABLE",
                        message: "系统音频中断结束，但未允许自动恢复",
                        retryable: true
                    ),
                    generation: operationGeneration
                )
                return
            }
            scheduleReconnect(trigger: "audio-interruption-ended")
        }
    }

    private func handleNetworkPath(
        _ path: NativeVoiceNetworkPath
    ) async {
        let previous = currentNetworkPath
        currentNetworkPath = path
        networkSummary = path.summary
        if previous != path {
            recordDiagnostic(
                category: "network",
                message: path.summary
            )
        }

        // NWPath is only a recovery gate. A satisfied callback never creates a
        // call by itself, and a short path wobble never tears down a healthy
        // WSS. Only a previously active, armed session may resume here.
        if path.available {
            scheduleReconnect(trigger: "network-path-satisfied")
        }
    }

    private func handleSessionFailure(
        _ error: Error,
        generation: UInt64
    ) async {
        guard generation == operationGeneration else {
            return
        }
        let recoverableCodes: Set<String> = [
            "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
            "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
            "BW_COMPUTER_VOICE_DIRECT_UPLINK_DISCONNECTED",
            "BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
            "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
        ]
        if let failure = error as? DirectVoiceFailure,
           failure.retryable,
           recoverableCodes.contains(failure.code),
           desiredActive,
           resumeArmed,
           state.phase == .active {
            recordDiagnostic(
                category: "recovery",
                message: "可恢复连接故障：\(failure.code)"
            )
            await suspendActiveSession(
                detail: "网络连接中断，等待可用路径后自动续接…",
                stopAudio: false
            )
            scheduleReconnect(trigger: failure.code)
            return
        }
        await failActiveSession(error, generation: generation)
    }

    private func suspendActiveSession(
        detail: String,
        stopAudio: Bool
    ) async {
        guard desiredActive else {
            return
        }
        if state.phase == .suspended {
            if stopAudio {
                audio.stop()
            }
            state = NativeVoiceBridgeState(
                phase: .suspended,
                detail: detail
            )
            return
        }
        guard state.phase == .active else {
            return
        }

        operationGeneration &+= 1
        recoveryInProgress = false
        reconnectTask?.cancel()
        reconnectTask = nil
        stopMicrophonePipeline()
        if stopAudio {
            audio.stop()
        }

        let oldSocket = socket
        socket = nil
        socketState = .disconnected
        state = NativeVoiceBridgeState(
            phase: .suspended,
            detail: detail
        )
        let disconnectTask: Task<Void, Never> = Task {
            guard let oldSocket else {
                return
            }
            await oldSocket.disconnect()
        }
        recoveryDisconnectTask = disconnectTask
    }

    private func scheduleReconnect(trigger: String) {
        guard desiredActive,
              resumeArmed,
              state.phase == .suspended,
              !audioInterrupted,
              currentNetworkPath?.available != false,
              !cleanupInProgress,
              !recoveryInProgress,
              reconnectTask == nil else {
            return
        }

        let intent = intentGeneration
        recordDiagnostic(
            category: "recovery",
            message: "已安排单次续接：\(trigger)"
        )
        reconnectTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(nanoseconds: 650_000_000)
            } catch {
                return
            }
            guard let self else {
                return
            }
            self.reconnectTask = nil
            guard self.intentGeneration == intent,
                  self.desiredActive,
                  self.resumeArmed,
                  self.state.phase == .suspended,
                  !self.audioInterrupted,
                  self.currentNetworkPath?.available != false else {
                return
            }
            await self.performRecovery(
                intent: intent,
                trigger: trigger
            )
        }
    }

    private func performRecovery(
        intent: UInt64,
        trigger: String
    ) async {
        guard intent == intentGeneration,
              desiredActive,
              resumeArmed,
              state.phase == .suspended,
              !audioInterrupted,
              currentNetworkPath?.available != false,
              !recoveryInProgress else {
            return
        }

        recoveryInProgress = true
        let generation = operationGeneration
        state = NativeVoiceBridgeState(
            phase: .connecting,
            detail: "正在恢复原生音频与 Windows WSS…"
        )

        let pendingDisconnect = recoveryDisconnectTask
        recoveryDisconnectTask = nil
        await pendingDisconnect?.value

        do {
            try requireRecoveryCurrent(
                intent: intent,
                generation: generation
            )
            try audio.start()
            try requireRecoveryCurrent(
                intent: intent,
                generation: generation
            )

            let newSocket = makeSocket(generation: generation)
            socket = newSocket
            recordDiagnostic(
                category: "protocol",
                message: "→ HELLO (recovery)"
            )
            try await connectOnceWithOneRetry(
                newSocket,
                generation: generation
            )
            try requireRecoveryCurrent(
                intent: intent,
                generation: generation
            )

            state = NativeVoiceBridgeState(
                phase: .starting,
                detail: "正在续接 \(activeAppKind.displayName) 语音…"
            )
            // From this point a new START has been sent. Never loop another
            // automatic START if its result becomes unknown.
            resumeArmed = false
            recordDiagnostic(
                category: "protocol",
                message: "→ START \(activeAppKind.rawValue) (recovery)"
            )
            let session = try await newSocket.start(appKind: activeAppKind)
            try requireRecoveryCurrent(
                intent: intent,
                generation: generation
            )
            try startMicrophonePipeline(
                socket: newSocket,
                generation: generation
            )
            recoveryInProgress = false
            resumeArmed = true
            state = NativeVoiceBridgeState(
                phase: .active,
                detail: "已自动恢复：\(trigger)",
                sessionId: session.id
            )
        } catch {
            recoveryInProgress = false
            guard generation == operationGeneration,
                  intent == intentGeneration else {
                return
            }
            await failActiveSession(error, generation: generation)
        }
    }

    private func requireRecoveryCurrent(
        intent: UInt64,
        generation: UInt64
    ) throws {
        guard intent == intentGeneration,
              generation == operationGeneration,
              desiredActive,
              !audioInterrupted,
              currentNetworkPath?.available != false else {
            throw CancellationError()
        }
    }

    func setMicrophoneMuted(_ muted: Bool) {
        do {
            try audio.setInputMuted(muted)
            microphoneMuted = audio.isInputMuted
            recordDiagnostic(
                category: "control",
                message: microphoneMuted ? "麦克风静音" : "麦克风恢复"
            )
            updateRemoteControls()
        } catch {
            recordDiagnostic(
                category: "error",
                message: "麦克风静音切换失败：\(error.localizedDescription)"
            )
        }
    }

    func clearDiagnostics() {
        diagnostics.removeAll(keepingCapacity: true)
        recordDiagnostic(category: "app", message: "诊断记录已清空")
    }

    var diagnosticReport: String {
        let formatter = ISO8601DateFormatter()
        var lines = [
            "BWReaderNative \(nativeAppBuildVersion)",
            "phase=\(state.phase)",
            "socket=\(socketState.rawValue)",
            "target=\(activeAppKind.rawValue)",
            "muted=\(microphoneMuted)",
            "network=\(networkSummary)",
            "detail=\(state.detail ?? "-")",
            "",
        ]
        lines.append(contentsOf: diagnostics.map {
            "\(formatter.string(from: $0.timestamp)) [\($0.category)] \($0.message)"
        })
        return lines.joined(separator: "\n")
    }

    private func recordDiagnostic(
        category: String,
        message: String
    ) {
        let bounded = message.count > 400
            ? String(message.prefix(400))
            : message
        diagnostics.append(NativeVoiceDiagnosticEntry(
            category: category,
            message: bounded
        ))
        if diagnostics.count > 80 {
            diagnostics.removeFirst(diagnostics.count - 80)
        }
    }

    private func updateRemoteControls() {
        let enabled = desiredActive
            && state.phase != .idle
            && state.phase != .failed
        remoteControls.update(
            enabled: enabled,
            muted: microphoneMuted,
            targetName: activeAppKind.displayName,
            status: state.title
        )
    }

    private func requireCurrent(_ generation: UInt64) throws {
        guard generation == operationGeneration else {
            throw CancellationError()
        }
    }

    private func displayMessage(for error: Error) -> String {
        if error is CancellationError {
            return "电脑语音启动已取消"
        }
        return error.localizedDescription
    }

    private nonisolated static func encodeMicrophoneSamples(
        _ samples: [Int16]
    ) -> Data {
        var bytes = [UInt8]()
        bytes.reserveCapacity(samples.count * 2)
        for sample in samples {
            let value = UInt16(bitPattern: sample)
            bytes.append(UInt8(truncatingIfNeeded: value))
            bytes.append(UInt8(truncatingIfNeeded: value >> 8))
        }
        return Data(bytes)
    }
}
