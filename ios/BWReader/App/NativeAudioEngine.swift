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

        var errorDescription: String? {
            switch self {
            case .microphoneDenied:
                return "没有获得 iPad 麦克风权限"
            case .microphoneFormatUnavailable:
                return "iPad 麦克风格式不可用"
            case .outputFormatUnavailable:
                return "无法建立 48 kHz 通话播放格式"
            case .invalidPlaybackFrame:
                return "Windows 返回的通话音频帧长度无效"
            }
        }
    }

    static let sampleRate: Double = 48_000
    static let samplesPerFrame = 960
    static let maximumScheduledFrames = 20

    var onMicrophoneFrame: (([Int16]) -> Void)?
    var onFailure: ((Error) -> Void)?
    var onInterruption: ((Interruption) -> Void)?
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
    private var inputMuteObserver: NSObjectProtocol?

    init() {
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: nil
        ) { [weak self] notification in
            self?.handleInterruption(notification)
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

    func start() throws {
        try controlQueue.sync {
            try startOnControlQueue()
        }
    }

    private func startOnControlQueue() throws {
        stateLock.lock()
        let alreadyRunning = running
        stateLock.unlock()
        if alreadyRunning {
            return
        }

        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.allowBluetoothHFP, .defaultToSpeaker]
        )
        try session.setPreferredSampleRate(Self.sampleRate)
        try session.setPreferredIOBufferDuration(0.02)
        try session.setActive(true)
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

    func stop() {
        controlQueue.sync {
            stopOnControlQueue()
        }
        processingQueue.sync {
            inputAccumulator.removeAll(keepingCapacity: false)
        }
    }

    var isInputMuted: Bool {
        AVAudioApplication.shared.isInputMuted
    }

    func setInputMuted(_ muted: Bool) throws {
        try AVAudioApplication.shared.setInputMuted(muted)
    }

    private func stopOnControlQueue() {
        stateLock.lock()
        let wasRunning = running
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

    func enqueuePlayback(_ samples: [Int16]) throws {
        try controlQueue.sync {
            try enqueuePlaybackOnControlQueue(samples)
        }
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
            onMicrophoneFrame?(output)
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
}
