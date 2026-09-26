import AVFoundation
import Foundation

final class NativeAudioEngine {
    enum Interruption: Sendable {
        case began
        case ended(shouldResume: Bool)
    }

    enum AudioFailure: LocalizedError {
        case microphoneDenied
        case microphoneFormatUnavailable
        case outputFormatUnavailable
        case invalidPlaybackFrame
        case controlTimeout

        var errorDescription: String? {
            switch self {
            case .microphoneDenied:
                return "没有获得 iPad 麦克风权限"
            case .microphoneFormatUnavailable:
                return "iPad 麦克风格式不可用"
            case .outputFormatUnavailable:
                return "无法建立 48 kHz 通话播放格式"
            case .controlTimeout:
                return "iPad 音频系统没有响应（启动超时）"
            case .invalidPlaybackFrame:
                return "Windows 返回的通话音频帧长度无效"
            }
        }
    }

    static let sampleRate: Double = 48_000

    /// 现在是不是在一通 CallKit 通话里。
    ///
    /// ⚠ 通话期间**系统拥有音频会话**，我们不能自己 setCategory/setActive。
    /// 由 ReaderVoipCall 在接通/结束时翻这个牌子 —— 只用一个布尔，
    /// 是因为同时只可能有一通电话（provider 配置里 maximumCallGroups = 1）。
    static var isUnderSystemCall = false
    static let samplesPerFrame = 960
    static let maximumScheduledFrames = 20

    /// 通话音频起止（2026-09-26）：环境旁听据此让出麦克风，通话结束再接着听。
    static let callAudioDidChange = Notification.Name("space.bwicarus.reader.call-audio-did-change")
    /// 当前有几路通话音频在跑（语音桥与语音会话各一个引擎，理论上同时只有一路）。
    @MainActor static fileprivate(set) var activeCallAudio = 0

    /// 嘈杂环境人声隔离：多人说话时只放行用户自己的声音（设置里的开关关着时原样放行）。
    var noisyGate: NativeNoisyVoiceGate { callAudio.gate }
    /// ⚠ 单独一个对象而不是引擎自己的字段：deinit 里的 stop() 要异步回主线程播报「通话音频结束」，
    /// 那时不能再捕获 self。
    private let callAudio = CallAudioAnnouncer()

    private final class CallAudioAnnouncer {
        let gate = NativeNoisyVoiceGate()
        private var announced = false

        /// 只在真正起止时各发一次（start 可能对已在跑的引擎重复调用）。只在主线程调。
        func set(_ active: Bool) {
            guard active != announced else { return }
            announced = active
            if active { gate.begin() } else { gate.end() }
            MainActor.assumeIsolated {
                NativeAudioEngine.activeCallAudio = max(0, NativeAudioEngine.activeCallAudio + (active ? 1 : -1))
            }
            NotificationCenter.default.post(name: NativeAudioEngine.callAudioDidChange, object: nil,
                                            userInfo: ["active": active])
        }
    }

    /// 通话麦克风的旁路（2026-09-26）：环境旁听在通话期间不暂停，改为接这里的帧继续转写与 jev 判断。
    /// 取的是**过闸门之前**、经过系统回声消除的原始上行（AI 的声音已被消掉，周围的人声还在）。
    static let microphoneTap = NativeMicrophoneTap()

    var onMicrophoneFrame: (([Int16]) -> Void)?
    var onFailure: ((Error) -> Void)?
    var onInterruption: ((Interruption) -> Void)?
    var onRecoveryNeeded: ((String) -> Void)?
    var onInputMuteChanged: ((Bool) -> Void)?

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let controlQueue = DispatchQueue(
        label: "space.bwicarus.reader.native-audio-control",
        qos: .userInitiated
    )
    private let processingQueue = DispatchQueue(
        label: "space.bwicarus.reader.native-audio",
        qos: .userInitiated
    )
    private let stateLock = NSLock()
    private var running = false
    private var graphConfigured = false
    private var tapInstalled = false
    private var inputAccumulator: [Float] = []
    private var scheduledFrames = 0
    private var playbackGeneration: UInt64 = 0
    private var interruptionObserver: NSObjectProtocol?
    private var configurationChangeObserver: NSObjectProtocol?
    private var routeChangeObserver: NSObjectProtocol?
    private var mediaServicesResetObserver: NSObjectProtocol?
    private var inputMuteObserver: NSObjectProtocol?

    init() {
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: nil
        ) { [weak self] notification in
            self?.handleInterruption(notification)
        }
        configurationChangeObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: nil
        ) { [weak self] _ in
            self?.reportRecoveryNeeded("AVAudioEngine 配置已变化")
        }
        routeChangeObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: AVAudioSession.sharedInstance(),
            queue: nil
        ) { [weak self] _ in
            self?.reportRecoveryNeeded("系统音频路由已变化")
            // 耳机拔出的那一刻要立刻静（2026-09-08）。这是整个自动静音
            // 功能里唯一时间敏感的地方 —— 晚一秒声音就已经出去了。
            self?.applyPresenceMute()
        }
        mediaServicesResetObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: AVAudioSession.sharedInstance(),
            queue: nil
        ) { [weak self] _ in
            self?.reportRecoveryNeeded("系统音频服务已重置")
        }
        inputMuteObserver = NotificationCenter.default.addObserver(
            forName: AVAudioApplication.inputMuteStateChangeNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.onInputMuteChanged?(AVAudioApplication.shared.isInputMuted)
        }
    }

    deinit {
        if let interruptionObserver {
            NotificationCenter.default.removeObserver(interruptionObserver)
        }
        if let configurationChangeObserver {
            NotificationCenter.default.removeObserver(
                configurationChangeObserver
            )
        }
        if let routeChangeObserver {
            NotificationCenter.default.removeObserver(routeChangeObserver)
        }
        if let mediaServicesResetObserver {
            NotificationCenter.default.removeObserver(
                mediaServicesResetObserver
            )
        }
        if let inputMuteObserver {
            NotificationCenter.default.removeObserver(inputMuteObserver)
        }
        stop()
    }

    func requestMicrophonePermission(
        completion: @escaping (Bool) -> Void
    ) {
        if #available(iOS 17.0, *) {
            AVAudioApplication.requestRecordPermission { granted in
                DispatchQueue.main.async {
                    completion(granted)
                }
            }
        } else {
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                DispatchQueue.main.async {
                    completion(granted)
                }
            }
        }
    }

    /// ⚠ 调用方都在主线程（NativeVoiceBridge / NativeAgentVoiceSession）。控制队列里的
    /// `player.play()` 在音频链路异常时会等一个永远不来的 IO 周期 —— 974 实机：主线程
    /// `.sync` 陪着等过 10 s，被系统看门狗 0x8BADF00D 杀掉。所以主线程最多等
    /// `controlWaitSeconds`，超时抛 controlTimeout 交给既有恢复流程，绝不无限期陪等。
    static let controlWaitSeconds: TimeInterval = 4

    func start() throws {
        try runOnControlQueue { try self.startOnControlQueue() }
        callAudio.set(true)
    }

    private func runOnControlQueue(_ work: @escaping () throws -> Void) throws {
        final class Outcome: @unchecked Sendable { var error: Error? }
        let outcome = Outcome()
        let done = DispatchSemaphore(value: 0)
        controlQueue.async {
            do { try work() } catch { outcome.error = error }
            done.signal()
        }
        guard done.wait(timeout: .now() + Self.controlWaitSeconds) == .success else {
            throw AudioFailure.controlTimeout
        }
        if let error = outcome.error { throw error }
    }

    /// Rebuilds only the App-local audio graph. The caller keeps the existing
    /// Windows WSS/session, so a transient iOS route/configuration loss does
    /// not issue another START or request microphone permission again.
    func restart() throws {
        try runOnControlQueue {
            self.stopOnControlQueue()
            try self.startOnControlQueue()
        }
    }

    /// 健康检查每几秒问一次：不排控制队列（队列若卡在 play() 里，排队就等于陪着卡）。
    var isOperational: Bool {
        stateLock.lock()
        let markedRunning = running
        stateLock.unlock()
        return markedRunning && engine.isRunning && player.isPlaying
    }

    private func startOnControlQueue() throws {
        stateLock.lock()
        let alreadyRunning = running
        stateLock.unlock()
        if alreadyRunning {
            return
        }

        let session = AVAudioSession.sharedInstance()
        // ⚠ **CallKit 通话里不要自己配会话。**
        //
        // 通话期间系统才是音频会话的主人：它已经把 category/mode 设好，
        // 并会在 `provider(_:didActivate:)` 里把会话交给我们。这时再
        // setCategory / setActive 就是跟它抢 —— 表现是没声音或刚起就断，
        // 而这两种表现跟"麦克风坏了"看起来一模一样，会把人带去查错方向。
        //
        // 采样率和缓冲照样提：那只是偏好，系统会按通话的约束去裁。
        if !NativeAudioEngine.isUnderSystemCall {
            try session.setCategory(
                .playAndRecord,
                mode: .voiceChat,
                options: [.allowBluetoothHFP, .defaultToSpeaker]
            )
        }
        try session.setPreferredSampleRate(Self.sampleRate)
        try session.setPreferredIOBufferDuration(0.02)
        if !NativeAudioEngine.isUnderSystemCall {
            try session.setActive(true)
        }
        var startCompleted = false
        defer {
            if !startCompleted {
                try? session.setActive(
                    false,
                    options: .notifyOthersOnDeactivation
                )
            }
        }

        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Self.sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw AudioFailure.outputFormatUnavailable
        }

        if !graphConfigured {
            engine.attach(player)
            engine.connect(
                player,
                to: engine.mainMixerNode,
                format: outputFormat
            )
            graphConfigured = true
        }

        let inputNode = engine.inputNode
        if !inputNode.isVoiceProcessingEnabled {
            try inputNode.setVoiceProcessingEnabled(true)
        }
        let inputFormat = inputNode.outputFormat(forBus: 0)
        guard
            inputFormat.sampleRate >= 8_000,
            inputFormat.sampleRate <= 192_000,
            inputFormat.channelCount >= 1
        else {
            throw AudioFailure.microphoneFormatUnavailable
        }

        if tapInstalled {
            inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        inputAccumulator.removeAll(keepingCapacity: true)
        playbackGeneration &+= 1
        scheduledFrames = 0

        inputNode.installTap(
            onBus: 0,
            bufferSize: 1_024,
            format: inputFormat
        ) { [weak self] buffer, _ in
            self?.capture(buffer, sampleRate: inputFormat.sampleRate)
        }
        tapInstalled = true

        engine.prepare()
        do {
            try engine.start()
            player.play()
            // 开播时也判一次：只在路由变化时判会漏掉"出门之后才开始说话"
            // 这一种 —— 那时路由早就是扬声器了，不会再有变化事件。
            applyPresenceMute()
            stateLock.lock()
            running = true
            stateLock.unlock()
            startCompleted = true
        } catch {
            inputNode.removeTap(onBus: 0)
            tapInstalled = false
            engine.stop()
            try? session.setActive(
                false,
                options: .notifyOthersOnDeactivation
            )
            throw error
        }
    }

    /// 挂断要**立刻**生效：先把 running 置假（采音回调与播放入队当场停手），拆音频图放到
    /// 控制队列异步做。以前这里是 `.sync`，控制队列卡住时「结束通话」连 STOP 都发不出去。
    /// 之后的 start()/restart() 在同一串行队列上排在它后面，顺序不变。
    func stop() {
        stateLock.lock()
        let wasRunning = running
        running = false
        stateLock.unlock()
        let announcer = callAudio
        if Thread.isMainThread { announcer.set(false) }
        else { DispatchQueue.main.async { announcer.set(false) } }
        controlQueue.async { self.stopOnControlQueue(deactivateSession: wasRunning) }
        processingQueue.async { self.inputAccumulator.removeAll(keepingCapacity: false) }
    }

    var isInputMuted: Bool {
        AVAudioApplication.shared.isInputMuted
    }

    /// 输出静音（2026-09-08 用户：在外面没戴耳机时自动静音）。
    ///
    /// 改播放节点的音量而**不是**停播：会话照常、字幕照出、AI 还听得见他，
    /// 只是不出声。用户要的是"静音"，不是"挂断"。
    func setOutputMuted(_ muted: Bool) {
        controlQueue.async { [player] in
            player.volume = muted ? 0 : 1
        }
    }

    /// 问一次在场判断并应用。判断本身在 ReaderPresenceGuard 里（本机即时，
    /// 不出网），这里只负责把结论落到音量上。
    ///
    /// ⚠ 用**拉**而不是让 guard 推：同时存在两个 NativeAudioEngine
    /// （语音会话和语音桥各一个），推的一方要记住有几个订阅者，
    /// 而漏掉一个的表现是"有时静音有时不静"，极难查。
    private func applyPresenceMute() {
        Task { @MainActor [weak self] in
            let guardian = ReaderPresenceGuard.shared
            guardian.reevaluate(reason: "音频引擎询问")
            self?.setOutputMuted(guardian.shouldMuteVoiceOutput)
        }
    }

    func setInputMuted(_ muted: Bool) throws {
        try AVAudioApplication.shared.setInputMuted(muted)
    }

    private func stopOnControlQueue(deactivateSession: Bool = false) {
        stateLock.lock()
        let wasRunning = running || deactivateSession
        running = false
        stateLock.unlock()

        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        player.stop()
        engine.stop()
        engine.reset()
        playbackGeneration &+= 1
        scheduledFrames = 0

        if wasRunning {
            try? AVAudioSession.sharedInstance().setActive(
                false,
                options: .notifyOthersOnDeactivation
            )
        }
    }

    /// 主线程只校验、不排队等：播放入队在串行控制队列上按到达顺序执行。
    func enqueuePlayback(_ samples: [Int16]) throws {
        guard samples.count == Self.samplesPerFrame else { throw AudioFailure.invalidPlaybackFrame }
        controlQueue.async { try? self.enqueuePlaybackOnControlQueue(samples) }
    }

    private func enqueuePlaybackOnControlQueue(
        _ samples: [Int16]
    ) throws {
        guard samples.count == Self.samplesPerFrame else {
            throw AudioFailure.invalidPlaybackFrame
        }
        guard isRunning else {
            return
        }
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Self.sampleRate,
            channels: 1,
            interleaved: false
        ), let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(Self.samplesPerFrame)
        ), let destination = buffer.floatChannelData?[0] else {
            throw AudioFailure.outputFormatUnavailable
        }

        buffer.frameLength = AVAudioFrameCount(Self.samplesPerFrame)
        for index in 0..<Self.samplesPerFrame {
            let value = samples[index]
            destination[index] = value < 0
                ? Float(value) / 32_768.0
                : Float(value) / 32_767.0
        }

        // 引擎没在跑时 play() 会去等不存在的 IO 周期（卡死的起点）。交给健康检查重启。
        guard engine.isRunning else { return }
        let resetTimeline = scheduledFrames >= Self.maximumScheduledFrames
        if resetTimeline {
            player.stop()
            playbackGeneration &+= 1
            scheduledFrames = 0
            player.play()
        }
        let generation = playbackGeneration
        scheduledFrames += 1

        player.scheduleBuffer(buffer) { [weak self] in
            guard let self else {
                return
            }
            self.controlQueue.async {
                guard generation == self.playbackGeneration else {
                    return
                }
                self.scheduledFrames = max(0, self.scheduledFrames - 1)
            }
        }
        if !player.isPlaying {
            player.play()
        }
    }

    private var isRunning: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return running
    }

    private func capture(
        _ buffer: AVAudioPCMBuffer,
        sampleRate: Double
    ) {
        guard
            isRunning,
            let source = buffer.floatChannelData?[0]
        else {
            return
        }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else {
            return
        }
        let copied = Array(
            UnsafeBufferPointer(start: source, count: frameCount)
        )
        processingQueue.async { [weak self] in
            self?.consumeInput(copied, sampleRate: sampleRate)
        }
    }

    private func consumeInput(
        _ samples: [Float],
        sampleRate: Double
    ) {
        guard isRunning else {
            return
        }
        inputAccumulator.append(contentsOf: samples)
        let sourceFrameCount = max(1, Int((sampleRate * 0.02).rounded()))

        while inputAccumulator.count >= sourceFrameCount, isRunning {
            let source = Array(inputAccumulator.prefix(sourceFrameCount))
            inputAccumulator.removeFirst(sourceFrameCount)
            var output = [Int16](
                repeating: 0,
                count: Self.samplesPerFrame
            )

            for index in 0..<Self.samplesPerFrame {
                let position = Self.samplesPerFrame == 1
                    ? 0
                    : Double(index)
                        * Double(sourceFrameCount - 1)
                        / Double(Self.samplesPerFrame - 1)
                let left = Int(position.rounded(.down))
                let right = min(sourceFrameCount - 1, left + 1)
                let fraction = Float(position - Double(left))
                let interpolated =
                    source[left] + (source[right] - source[left]) * fraction
                let limited = max(-1, min(1, interpolated))
                output[index] = limited < 0
                    ? Int16((limited * 32_768).rounded())
                    : Int16((limited * 32_767).rounded())
            }
            Self.microphoneTap.deliver(output)
            onMicrophoneFrame?(noisyGate.process(output))
        }

        let maximumBufferedSamples = sourceFrameCount * 10
        if inputAccumulator.count > maximumBufferedSamples {
            inputAccumulator.removeFirst(
                inputAccumulator.count - maximumBufferedSamples
            )
        }
    }

    private func handleInterruption(_ notification: Notification) {
        guard
            let rawType = notification.userInfo?[
                AVAudioSessionInterruptionTypeKey
            ] as? UInt,
            let type = AVAudioSession.InterruptionType(rawValue: rawType)
        else {
            return
        }

        switch type {
        case .began:
            onInterruption?(.began)
        case .ended:
            let rawOptions = notification.userInfo?[
                AVAudioSessionInterruptionOptionKey
            ] as? UInt ?? 0
            let options = AVAudioSession.InterruptionOptions(
                rawValue: rawOptions
            )
            onInterruption?(.ended(
                shouldResume: options.contains(.shouldResume)
            ))
        @unknown default:
            break
        }
    }

    private func reportRecoveryNeeded(_ reason: String) {
        onRecoveryNeeded?(reason)
    }
}

/// 线程安全的单订阅者旁路：主线程设置、音频处理队列投递。
final class NativeMicrophoneTap: @unchecked Sendable {
    private let lock = NSLock()
    private var handler: (([Int16]) -> Void)?

    func set(_ handler: (([Int16]) -> Void)?) {
        lock.lock(); self.handler = handler; lock.unlock()
    }

    func deliver(_ frame: [Int16]) {
        lock.lock(); let handler = self.handler; lock.unlock()
        handler?(frame)
    }
}
