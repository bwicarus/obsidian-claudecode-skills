import AVFoundation
import Foundation

/// Owns only this App's audio engine. Network PCM is mono, signed 16-bit little-endian, 48 kHz.
final class NativeAudio {
    var onPCM: ((Data) -> Void)?
    var onInterrupted: (() -> Void)?

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let sampleRate = 48_000.0
    private let packetBytes = 1_920
    private var converter: AVAudioConverter?
    private var captureRemainder = Data()
    private let captureLock = NSLock()
    private var installedTap = false
    private var attachedPlayer = false
    private var sessionIsActive = false
    private(set) var isRunning = false
    private var interruptionObserver: NSObjectProtocol?

    static func requestPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in continuation.resume(returning: granted) }
        }
    }

    func start(managedBySystemCall: Bool = false) throws {
        guard !isRunning else { return }
        let session = AVAudioSession.sharedInstance()
        if !managedBySystemCall {
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetooth])
        }
        try session.setPreferredSampleRate(sampleRate)
        try session.setPreferredIOBufferDuration(0.02)
        // CallKit activates and deactivates its own session in provider callbacks.
        if !managedBySystemCall { try session.setActive(true) }
        sessionIsActive = !managedBySystemCall

        do {
            guard let playbackFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false) else {
                throw AppError.message("无法创建扬声器音频格式。")
            }
            if !attachedPlayer {
                engine.attach(player)
                attachedPlayer = true
            }
            engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
            let input = engine.inputNode
            // Voice processing provides echo cancellation for simultaneous capture and playback.
            if !input.isVoiceProcessingEnabled { try input.setVoiceProcessingEnabled(true) }
            let inputFormat = input.outputFormat(forBus: 0)
            guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
                  let captureFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true),
                  let converter = AVAudioConverter(from: inputFormat, to: captureFormat) else {
                throw AppError.message("当前音频设备不支持语音采集。")
            }
            self.converter = converter
            input.installTap(onBus: 0, bufferSize: 960, format: inputFormat) { [weak self] buffer, _ in
                self?.convertCapture(buffer, outputFormat: captureFormat, converter: converter)
            }
            installedTap = true
            engine.prepare()
            try engine.start()
            player.play()
            isRunning = true
            interruptionObserver = NotificationCenter.default.addObserver(
                forName: AVAudioSession.interruptionNotification, object: session, queue: .main
            ) { [weak self] notification in
                let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
                if raw == AVAudioSession.InterruptionType.began.rawValue { self?.onInterrupted?() }
            }
        } catch {
            stop()
            throw error
        }
    }

    private func convertCapture(_ input: AVAudioPCMBuffer, outputFormat: AVAudioFormat, converter: AVAudioConverter) {
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * sampleRate / input.format.sampleRate)) + 256
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return }
        var suppliedInput = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, state in
            if suppliedInput {
                state.pointee = .noDataNow
                return nil
            }
            suppliedInput = true
            state.pointee = .haveData
            return input
        }
        guard status != .error, conversionError == nil, output.frameLength > 0,
              let samples = output.int16ChannelData?[0] else { return }
        let bytes = Data(bytes: samples, count: Int(output.frameLength) * MemoryLayout<Int16>.size)
        var packets: [Data] = []
        captureLock.lock()
        captureRemainder.append(bytes)
        while captureRemainder.count >= packetBytes {
            packets.append(Data(captureRemainder.prefix(packetBytes)))
            captureRemainder.removeFirst(packetBytes)
        }
        captureLock.unlock()
        for packet in packets { onPCM?(packet) }
    }

    func play(_ bytes: Data) {
        guard isRunning, bytes.count >= 2, bytes.count.isMultiple(of: 2),
              let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(bytes.count / 2)),
              let channel = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = buffer.frameCapacity
        bytes.withUnsafeBytes { raw in
            // Network data may be unaligned; decode bytes rather than rebinding its memory.
            let values = raw.bindMemory(to: UInt8.self)
            for index in 0..<Int(buffer.frameLength) {
                let sample = UInt16(values[index * 2]) | (UInt16(values[index * 2 + 1]) << 8)
                channel[index] = Float(Int16(bitPattern: sample)) / 32768.0
            }
        }
        player.scheduleBuffer(buffer)
    }

    func stop() {
        isRunning = false
        if installedTap {
            engine.inputNode.removeTap(onBus: 0)
            installedTap = false
        }
        if attachedPlayer { player.stop() }
        engine.stop()
        converter = nil
        captureLock.lock()
        captureRemainder.removeAll(keepingCapacity: true)
        captureLock.unlock()
        if let interruptionObserver { NotificationCenter.default.removeObserver(interruptionObserver) }
        interruptionObserver = nil
        if sessionIsActive {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            sessionIsActive = false
        }
    }

    deinit {
        if let interruptionObserver { NotificationCenter.default.removeObserver(interruptionObserver) }
    }
}
