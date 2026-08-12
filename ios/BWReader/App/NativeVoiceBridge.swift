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
    private static let reconnectDelayNanoseconds: [UInt64] = [
        650_000_000,
        2_000_000_000,
        4_000_000_000,
        8_000_000_000,
        15_000_000_000,
    ]
    private static let confirmedRecoveryRejectionCodes: Set<String> = [
        "BW_COMPUTER_VOICE_DIRECT_BUSY",
        "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
        "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOP_UNCONFIRMED",
    ]

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
            publishSharedStatus()
        }
    }

    @Published private(set) var microphoneMuted = false
    @Published private(set) var socketState: DirectVoiceState = .disconnected
    @Published private(set) var networkSummary = "checking"
    @Published private(set) var diagnostics: [NativeVoiceDiagnosticEntry] = []

    private weak var reader: ReaderWebViewModel?
    private let audio = NativeAudioEngine()
    private let sharedStore = ReaderNativeBridgeStore()
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
    private var reconnectAttempt = 0
    private var appForeground = true
    private var audioInterrupted = false
    private var localAudioSuspended = false
    private var localAudioRecoveryTask: Task<Void, Never>?
    private var localAudioRecoveryAttempt = 0
    private var audioHealthTask: Task<Void, Never>?
    private var ignoreAudioRecoverySignalsUntil = Date.distantPast
    private var currentNetworkPath: NativeVoiceNetworkPath?
    private var recoveryInProgress = false
    private var recoveryDisconnectTask: Task<Void, Never>?
    private var safariWebContext: ReaderNativeWebContext?
    private var safariContextRevision = ""
    private var safariContextSequence: Int64 = 0
    private var safariContextTask: Task<Void, Never>?

    init() {
        audio.onFailure = { [weak self] error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                await self.handleLocalAudioFailure(error)
            }
        }
        audio.onInterruption = { [weak self] interruption in
            Task { @MainActor [weak self] in
                await self?.handleAudioInterruption(interruption)
            }
        }
        audio.onRecoveryNeeded = { [weak self] reason in
            Task { @MainActor [weak self] in
                self?.handleLocalAudioRecoverySignal(reason)
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
        publishSharedStatus()
    }

    var activeTargetName: String {
        activeAppKind.displayName
    }

    func bind(reader: ReaderWebViewModel) {
        self.reader = reader
        reader.bind(nativeVoiceBridge: self)
    }

    func setAppForeground(_ foreground: Bool) {
        guard appForeground != foreground else { return }
        appForeground = foreground
        recordDiagnostic(
            category: "app",
            message: foreground ? "App 回到前台" : "App 进入后台"
        )
        if foreground,
           desiredActive,
           localAudioSuspended,
           !audioInterrupted {
            scheduleLocalAudioRecovery(
                trigger: "app-foreground",
                immediate: true
            )
            return
        }
        guard foreground,
              desiredActive,
              resumeArmed,
              state.phase == .suspended else {
            return
        }
        reconnectTask?.cancel()
        reconnectTask = nil
        scheduleReconnect(
            trigger: "app-foreground",
            immediate: true
        )
    }

    private func publishSharedStatus() {
        let phase: String
        switch state.phase {
        case .idle:
            phase = "idle"
        case .preparing:
            phase = "preparing"
        case .connecting:
            phase = "connecting"
        case .starting:
            phase = "starting"
        case .active:
            phase = "active"
        case .suspended:
            phase = "suspended"
        case .stopping:
            phase = "stopping"
        case .failed:
            phase = "failed"
        }
        try? sharedStore.writeStatus(ReaderNativeVoiceStatus(
            phase: phase,
            active: state.isActive,
            busy: state.isBusy,
            sessionID: state.sessionId,
            appKind: activeAppKind.rawValue,
            detail: state.detail
        ))
    }

    func start(
        appKind: DirectVoiceTargetApp = .codexDesktop,
        safariWebContext: ReaderNativeWebContext? = nil
    ) async {
        guard !state.isActive, !state.isBusy else {
            return
        }

        operationGeneration &+= 1
        intentGeneration &+= 1
        let generation = operationGeneration
        reconnectTask?.cancel()
        reconnectTask = nil
        localAudioRecoveryTask?.cancel()
        localAudioRecoveryTask = nil
        localAudioRecoveryAttempt = 0
        audioHealthTask?.cancel()
        audioHealthTask = nil
        cleanupInProgress = false
        recoveryInProgress = false
        reconnectAttempt = 0
        desiredActive = true
        resumeArmed = false
        activeAppKind = appKind
        audioInterrupted = false
        localAudioSuspended = false
        self.safariWebContext = safariWebContext
        safariContextRevision = ""
        safariContextSequence = 0
        safariContextTask?.cancel()
        safariContextTask = nil
        recoveryDisconnectTask?.cancel()
        recoveryDisconnectTask = nil
        setMicrophoneMuted(false)
        recordDiagnostic(
            category: "control",
            message: "用户启动 \(appKind.displayName)"
        )
        state = NativeVoiceBridgeState(
            phase: .preparing,
            detail: "正在申请麦克风…"
        )

        do {
            if safariWebContext == nil {
                guard await requestMicrophonePermission() else {
                    throw BridgeFailure.microphoneDenied
                }
                try requireCurrent(generation)
            } else {
                guard await requestMicrophonePermission() else {
                    throw BridgeFailure.microphoneDenied
                }
                try requireCurrent(generation)
            }

            ignoreAudioRecoverySignalsUntil = Date().addingTimeInterval(2)
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
            let session = try await socket.start(
                appKind: appKind,
                takeover: true
            )
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
            reconnectAttempt = 0
            resumeArmed = true
            startAudioHealthWatchdog(generation: generation)
            if let context = safariWebContext {
                try await forwardSafariWebContext(
                    context,
                    socket: socket,
                    sessionId: session.id
                )
                startSafariContextPump(generation: generation)
            }
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
        localAudioRecoveryTask?.cancel()
        localAudioRecoveryTask = nil
        localAudioRecoveryAttempt = 0
        audioHealthTask?.cancel()
        audioHealthTask = nil
        desiredActive = false
        resumeArmed = false
        audioInterrupted = false
        localAudioSuspended = false
        recoveryInProgress = false
        reconnectAttempt = 0
        stopSafariContextPump(clearContext: true)
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
        guard (state.phase == .active || localAudioSuspended),
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

    private func startAudioHealthWatchdog(generation: UInt64) {
        audioHealthTask?.cancel()
        audioHealthTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: 5_000_000_000)
                } catch {
                    return
                }
                guard let self,
                      generation == self.operationGeneration,
                      self.desiredActive else {
                    return
                }
                if self.state.phase == .active,
                   !self.audioInterrupted,
                   !self.audio.isOperational {
                    self.handleLocalAudioRecoverySignal(
                        "App 音频健康检查发现引擎已停止"
                    )
                }
            }
        }
    }

    private func handleLocalAudioRecoverySignal(_ reason: String) {
        guard desiredActive,
              !cleanupInProgress,
              Date() >= ignoreAudioRecoverySignalsUntil else {
            return
        }
        if localAudioSuspended {
            if !audioInterrupted {
                scheduleLocalAudioRecovery(trigger: reason)
            }
            return
        }
        guard state.phase == .active else { return }
        recordDiagnostic(category: "audio", message: reason)
        suspendLocalAudio(
            detail: "iPad 音频暂时中断，Windows 通话保持连接并等待本地恢复…"
        )
        if !audioInterrupted {
            scheduleLocalAudioRecovery(trigger: reason)
        }
    }

    private func handleLocalAudioFailure(_ error: Error) async {
        if let failure = error as? NativeAudioEngine.AudioFailure,
           case .invalidPlaybackFrame = failure {
            await failActiveSession(
                error,
                generation: operationGeneration
            )
            return
        }
        guard desiredActive,
              state.phase == .active || localAudioSuspended else {
            await failActiveSession(
                error,
                generation: operationGeneration
            )
            return
        }
        handleLocalAudioRecoverySignal(
            "App 本地音频失败：\(error.localizedDescription)"
        )
    }

    private func suspendLocalAudio(detail: String) {
        guard desiredActive, state.phase == .active else { return }
        let sessionID = state.sessionId
        localAudioSuspended = true
        stopMicrophonePipeline()
        audio.stop()
        state = NativeVoiceBridgeState(
            phase: .suspended,
            detail: detail,
            sessionId: sessionID
        )
    }

    private func scheduleLocalAudioRecovery(
        trigger: String,
        immediate: Bool = false
    ) {
        guard desiredActive,
              localAudioSuspended,
              !audioInterrupted,
              !cleanupInProgress,
              !recoveryInProgress,
              socket != nil,
              socketState == .active,
              localAudioRecoveryTask == nil else {
            return
        }
        let generation = operationGeneration
        let delay: UInt64 = immediate ? 0 : Self.reconnectDelayNanoseconds[
            min(
                localAudioRecoveryAttempt,
                Self.reconnectDelayNanoseconds.count - 1
            )
        ]
        recordDiagnostic(
            category: "audio",
            message: "已安排 App 本地音频恢复：\(trigger)"
        )
        localAudioRecoveryTask = Task { @MainActor [weak self] in
            if delay > 0 {
                do {
                    try await Task.sleep(nanoseconds: delay)
                } catch {
                    return
                }
            }
            guard let self else { return }
            self.localAudioRecoveryTask = nil
            await self.performLocalAudioRecovery(
                trigger: trigger,
                generation: generation
            )
        }
    }

    private func performLocalAudioRecovery(
        trigger: String,
        generation: UInt64
    ) async {
        guard generation == operationGeneration,
              desiredActive,
              localAudioSuspended,
              !audioInterrupted,
              let socket,
              let sessionID = state.sessionId,
              socketState == .active else {
            return
        }
        do {
            ignoreAudioRecoverySignalsUntil = Date().addingTimeInterval(2)
            try audio.restart()
            guard generation == operationGeneration,
                  desiredActive,
                  localAudioSuspended,
                  !audioInterrupted else {
                audio.stop()
                return
            }
            try startMicrophonePipeline(
                socket: socket,
                generation: generation
            )
            localAudioSuspended = false
            localAudioRecoveryAttempt = 0
            state = NativeVoiceBridgeState(
                phase: .active,
                detail: "App 本地音频已自动恢复：\(trigger)",
                sessionId: sessionID
            )
            startAudioHealthWatchdog(generation: generation)
            recordDiagnostic(
                category: "audio",
                message: "复用现有 Windows 会话完成本地音频恢复"
            )
        } catch {
            localAudioRecoveryAttempt = min(
                localAudioRecoveryAttempt + 1,
                Int.max - 1
            )
            recordDiagnostic(
                category: "audio",
                message: "本地音频恢复失败，保持 WSS 并继续重试：\(error.localizedDescription)"
            )
            state = NativeVoiceBridgeState(
                phase: .suspended,
                detail: "iPad 音频仍在恢复；Windows 通话保持连接…",
                sessionId: sessionID
            )
            scheduleLocalAudioRecovery(trigger: "local-audio-recovery-failed")
        }
    }

    private func startSafariContextPump(generation: UInt64) {
        safariContextTask?.cancel()
        safariContextTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled,
                  generation == self.operationGeneration,
                  self.safariWebContext != nil {
                do {
                    if self.state.phase == .active,
                       let latest = try self.sharedStore
                        .readLatestWebContext(),
                       latest.revision != self.safariContextRevision,
                       let socket = self.socket,
                       let sessionId = self.state.sessionId {
                        self.safariWebContext = latest
                        try await self.forwardSafariWebContext(
                            latest,
                            socket: socket,
                            sessionId: sessionId
                        )
                    }
                } catch {
                    self.recordDiagnostic(
                        category: "context",
                        message: "Safari 网页上下文更新失败：\(error.localizedDescription)"
                    )
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
    }

    private func stopSafariContextPump(clearContext: Bool) {
        safariContextTask?.cancel()
        safariContextTask = nil
        if clearContext {
            safariWebContext = nil
            safariContextRevision = ""
            safariContextSequence = 0
        }
    }

    private func forwardSafariWebContext(
        _ context: ReaderNativeWebContext,
        socket: DirectVoiceSocket,
        sessionId: String
    ) async throws {
        guard context.isValid else {
            throw DirectVoiceFailure(
                code: "BW_NATIVE_WEB_CONTEXT_INVALID",
                message: "Safari 网页上下文无效",
                retryable: false
            )
        }
        safariContextSequence += 1
        let sequence = safariContextSequence
        let timestampMilliseconds = Int64(Date().timeIntervalSince1970 * 1_000)
        let timestampSeconds = timestampMilliseconds / 1_000
        let eventID = String(
            UUID().uuidString.lowercased()
                .replacingOccurrences(of: "-", with: "")
                .prefix(16)
        )
        var pageContext: [String: DirectJSONValue] = [
            "reason": .string(context.selection.isEmpty ? "page" : "selection"),
            "text": .string(context.visibleText),
            "text_available": .bool(!context.visibleText.isEmpty),
            "text_source": .string("safari-web-visible"),
            "fallback_reason": context.visibleText.isEmpty
                ? .string("no-visible-text") : .null,
            "truncated": .bool(context.visibleText.utf8.count >= 32_768),
        ]
        if !context.selection.isEmpty {
            pageContext["selection"] = .string(
                String(context.selection.prefix(400))
            )
        }
        let event: DirectJSONValue = .object([
            "v": .number(1),
            "seq": .number(Double(sequence)),
            "type": .string("page.context"),
            "ts": .number(Double(timestampSeconds)),
            "id": .string(eventID),
            "kind": .string("web"),
            "file": .string(context.url),
            "title": .string(context.title),
            "page": .number(1),
            "stable": .bool(true),
            "page_context": .object(pageContext),
        ])
        _ = try await socket.requestReaderContext(
            action: "context",
            fields: [
                "sessionId": .string(sessionId),
                "contextContract": .string("reader-outgoing-context/1"),
                "event": event,
            ]
        )
        safariContextRevision = context.revision

        // This enrichment exists only in snapshot-mcp mode. Legacy injection
        // already received the page.context above, so rejection is harmless.
        let selectionState = context.selection.isEmpty ? "cleared" : "active"
        _ = try? await socket.requestReaderContext(
            action: "active-reading",
            fields: [
                "sessionId": .string(sessionId),
                "activeContract": .string("reader-active-reading/1"),
                "active": .object([
                    "kind": .string("web"),
                    "file": .string(context.url),
                    "title": .string(context.title),
                    "page": .number(1),
                    "selectionState": .string(selectionState),
                    "selection": context.selection.isEmpty
                        ? .null
                        : .string(String(context.selection.prefix(400))),
                    "observedAtEpochMs": .number(
                        Double(timestampMilliseconds)
                    ),
                ]),
            ]
        )
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
                await handleLocalAudioFailure(error)
            }
        case .error(let failure):
            // connect()/start() propagate their own failure to the awaiting
            // start flow.  Runtime failures need an asynchronous full cleanup.
            recordDiagnostic(
                category: "error",
                message: "\(failure.code): \(failure.message)"
            )
            if state.phase == .active || localAudioSuspended {
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
        reconnectAttempt = 0
        reconnectTask?.cancel()
        reconnectTask = nil
        localAudioRecoveryTask?.cancel()
        localAudioRecoveryTask = nil
        localAudioRecoveryAttempt = 0
        audioHealthTask?.cancel()
        audioHealthTask = nil
        stopSafariContextPump(clearContext: true)
        audioInterrupted = false
        localAudioSuspended = false
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
        reconnectAttempt = 0
        reconnectTask?.cancel()
        reconnectTask = nil
        localAudioRecoveryTask?.cancel()
        localAudioRecoveryTask = nil
        localAudioRecoveryAttempt = 0
        audioHealthTask?.cancel()
        audioHealthTask = nil
        stopSafariContextPump(clearContext: true)
        audioInterrupted = false
        localAudioSuspended = false
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
            guard desiredActive,
                  state.phase == .active || localAudioSuspended else {
                return
            }
            audioInterrupted = true
            recordDiagnostic(
                category: "audio",
                message: "系统音频中断开始"
            )
            if localAudioSuspended {
                localAudioRecoveryTask?.cancel()
                localAudioRecoveryTask = nil
                return
            }
            suspendLocalAudio(
                detail: "来电、闹钟或其他 App 暂时占用音频，等待系统允许恢复…"
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
            if localAudioSuspended {
                scheduleLocalAudioRecovery(
                    trigger: shouldResume
                        ? "audio-interruption-ended"
                        : "audio-interruption-ended-without-resume-hint",
                    immediate: shouldResume
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
           (state.phase == .active || localAudioSuspended) {
            recordDiagnostic(
                category: "recovery",
                message: "可恢复连接故障：\(failure.code)"
            )
            if localAudioSuspended {
                localAudioRecoveryTask?.cancel()
                localAudioRecoveryTask = nil
            }
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
        let replacingLocalAudioSuspension =
            state.phase == .suspended && localAudioSuspended
        if state.phase == .suspended && !replacingLocalAudioSuspension {
            if stopAudio {
                audio.stop()
            }
            state = NativeVoiceBridgeState(
                phase: .suspended,
                detail: detail
            )
            return
        }
        guard state.phase == .active || replacingLocalAudioSuspension else {
            return
        }

        operationGeneration &+= 1
        localAudioRecoveryTask?.cancel()
        localAudioRecoveryTask = nil
        localAudioRecoveryAttempt = 0
        localAudioSuspended = false
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

    private func scheduleReconnect(
        trigger: String,
        immediate: Bool = false
    ) {
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
        let attempt = reconnectAttempt + 1
        let delay = immediate ? 0 : Self.reconnectDelayNanoseconds[
            min(
                reconnectAttempt,
                Self.reconnectDelayNanoseconds.count - 1
            )
        ]
        recordDiagnostic(
            category: "recovery",
            message: "已安排第 \(attempt) 次续接：\(trigger)"
        )
        reconnectTask = Task { @MainActor [weak self] in
            if delay > 0 {
                do {
                    try await Task.sleep(nanoseconds: delay)
                } catch {
                    return
                }
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

        var startRequestSent = false
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
            // automatic START if its result becomes unknown. Explicit
            // pre-start server rejections (busy/cleanup pending) are safe to
            // retry because Windows has confirmed that no new session began.
            resumeArmed = false
            startRequestSent = true
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
            reconnectAttempt = 0
            resumeArmed = true
            state = NativeVoiceBridgeState(
                phase: .active,
                detail: "已自动恢复：\(trigger)",
                sessionId: session.id
            )
            if let context = safariWebContext {
                try await forwardSafariWebContext(
                    context,
                    socket: newSocket,
                    sessionId: session.id
                )
                startSafariContextPump(generation: generation)
            }
        } catch {
            recoveryInProgress = false
            guard generation == operationGeneration,
                  intent == intentGeneration else {
                return
            }
            if recoveryCanRetry(
                error,
                startRequestSent: startRequestSent
            ) {
                let failedSocket = socket
                socket = nil
                await failedSocket?.disconnect()
                guard generation == operationGeneration,
                      intent == intentGeneration,
                      desiredActive else {
                    return
                }
                socketState = .disconnected
                resumeArmed = true
                reconnectAttempt = min(
                    reconnectAttempt + 1,
                    Int.max - 1
                )
                let code = (error as? DirectVoiceFailure)?.code
                    ?? "retryable-recovery-failure"
                state = NativeVoiceBridgeState(
                    phase: .suspended,
                    detail: "Windows 音频尚未释放，继续自动续接…"
                )
                recordDiagnostic(
                    category: "recovery",
                    message: "第 \(reconnectAttempt) 次续接未完成：\(code)"
                )
                scheduleReconnect(trigger: code)
                return
            }
            await failActiveSession(error, generation: generation)
        }
    }

    private func recoveryCanRetry(
        _ error: Error,
        startRequestSent: Bool
    ) -> Bool {
        guard let failure = error as? DirectVoiceFailure,
              failure.retryable else {
            return false
        }
        if !startRequestSent {
            return true
        }
        return Self.confirmedRecoveryRejectionCodes.contains(failure.code)
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
