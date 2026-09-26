import AVFoundation
import Foundation
import NaturalLanguage
import Speech

/// 分段转写（2026-09-26 用户：「在语音的非活跃时间切分，最好识别到每个人的开始和终止，
/// 对这一段声音进行分辨：登记了语言就用该语言，不能就需要确认」）。
///
/// 旁听管线先按说话人分离给出的「一人一段」切好 16 kHz 音频，再交到这里：
///   - 指定了语言 → 用该语言的本机识别器转一次；
///   - 没指定 → 在候选语言里各转一次，按「识别置信度 × 文字确实是这种语言的概率」挑最高的，
///     结果标成「推测」，由管线累计投票、由用户在人物页确认。
///
/// 一次只跑一个识别任务（串行），免得几段同时抢神经引擎。强制本机识别：原声不离开设备。
actor NativeSegmentTranscriber {
    static let shared = NativeSegmentTranscriber()

    struct Result: Sendable {
        let text: String
        let locale: String
        let confidence: Float
        /// 候选语言各自的得分（只有推测时才有），写进日志便于调判据。
        var scores: [String: Float] = [:]
    }

    static let allLocales = ["zh-CN", "ja-JP", "en-US", "ko-KR", "zh-TW"]
    static let candidatesKey = "bw.ambient.candidateLocales"

    /// 推测语言时依次尝试的候选（设置里可改），只保留本机支持离线识别的。
    static var candidateLocales: [String] {
        get {
            let stored = UserDefaults.standard.stringArray(forKey: candidatesKey) ?? ["zh-CN", "ja-JP", "en-US"]
            return stored.filter { allLocales.contains($0) }
        }
        set { UserDefaults.standard.set(newValue, forKey: candidatesKey) }
    }

    static func displayName(_ locale: String) -> String {
        switch locale {
        case "zh-CN": return "中文"
        case "zh-TW": return "繁體中文"
        case "ja-JP": return "日语"
        case "en-US": return "英语"
        case "ko-KR": return "韩语"
        default: return locale
        }
    }

    private var recognizers: [String: SFSpeechRecognizer] = [:]
    private var busy = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var unsupportedLogged: Set<String> = []

    private func recognizer(_ locale: String) -> SFSpeechRecognizer? {
        if let hit = recognizers[locale] { return hit }
        guard let made = SFSpeechRecognizer(locale: Locale(identifier: locale)), made.supportsOnDeviceRecognition else {
            if !unsupportedLogged.contains(locale) {
                unsupportedLogged.insert(locale)
                NativeAmbientLog.note("分段转写：\(Self.displayName(locale)) 没有本机识别模型（设置 → 通用 → 键盘 → 听写 里下载），跳过这种语言", level: "error")
            }
            return nil
        }
        recognizers[locale] = made
        return made
    }

    func supports(_ locale: String) -> Bool { recognizer(locale) != nil }

    // 串行门：actor 在 await 处可重入，所以要显式排队
    private func acquire() async {
        if !busy { busy = true; return }
        await withCheckedContinuation { waiters.append($0) }
    }

    private func release() {
        if waiters.isEmpty { busy = false } else { waiters.removeFirst().resume() }
    }

    /// 用指定语言转写。
    func transcribe(_ samples: [Float], locale: String) async -> Result? {
        await acquire()
        defer { release() }
        return await run(samples, locale: locale)
    }

    /// 没登记语言：候选语言各转一次，挑最可信的。
    func guess(_ samples: [Float]) async -> Result? {
        await acquire()
        defer { release() }
        var best: Result?
        var bestScore: Float = -1
        var scores: [String: Float] = [:]
        for locale in Self.candidateLocales {
            guard let result = await run(samples, locale: locale), !result.text.isEmpty else { continue }
            let score = result.confidence * Self.languageProbability(result.text, locale: locale)
            scores[locale] = (score * 100).rounded() / 100
            if score > bestScore { bestScore = score; best = result }
        }
        guard var chosen = best else { return nil }
        chosen.scores = scores
        return chosen
    }

    private func run(_ original: [Float], locale: String) async -> Result? {
        let samples = NativeAmbientGain.normalize(original)   // 远处的声音太小，识别器会判「没有语音」
        if #available(iOS 26.0, *), await NativeLiveTranscriber.supports(locale) {
            do {
                let value = try await NativeLiveTranscriber.transcribeOnce(samples, locale: locale)
                return value.text.isEmpty ? nil : Result(text: value.text, locale: locale, confidence: value.confidence)
            } catch {
                NativeAmbientLog.note("分段转写：新识别接口出错（\(Self.displayName(locale))）\(error.localizedDescription)，改用旧接口", level: "error")
            }
        }
        guard let recognizer = recognizer(locale),
              let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: NativeStreamDiarizer.sampleRate,
                                         channels: 1, interleaved: false),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = buffer.floatChannelData?[0] else { return nil }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { source in
            channel.update(from: source.baseAddress!, count: samples.count)
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        request.addsPunctuation = true
        request.taskHint = .dictation
        request.append(buffer)
        request.endAudio()

        final class Box: @unchecked Sendable {
            var done = false
            var task: SFSpeechRecognitionTask?
        }
        let box = Box()
        let result: Result? = await withCheckedContinuation { continuation in
            let lock = NSLock()
            func finish(_ value: Result?) {
                lock.lock()
                defer { lock.unlock() }
                guard !box.done else { return }
                box.done = true
                continuation.resume(returning: value)
            }
            box.task = recognizer.recognitionTask(with: request) { result, error in
                if let result, result.isFinal {
                    let segments = result.bestTranscription.segments
                    let confidence = segments.isEmpty ? 0 : segments.map(\.confidence).reduce(0, +) / Float(segments.count)
                    finish(Result(text: result.bestTranscription.formattedString, locale: locale, confidence: confidence))
                } else if let error {
                    let code = (error as NSError).code
                    // 1110 = 这段里没检测到语音：正常结局，不算故障
                    if code != 1110 {
                        NativeAmbientLog.note("分段转写：\(Self.displayName(locale)) 识别出错 code=\(code) \(error.localizedDescription)", level: "error")
                    }
                    finish(nil)
                }
            }
            // 兜底：识别器偶尔既不给结果也不报错
            DispatchQueue.global().asyncAfter(deadline: .now() + 20) {
                lock.lock()
                let pending = !box.done
                lock.unlock()
                if pending {
                    box.task?.cancel()
                    NativeAmbientLog.note("分段转写：\(Self.displayName(locale)) 20 秒没返回，放弃这一段", level: "error")
                    finish(nil)
                }
            }
        }
        return result
    }

    /// 文字确实是这种语言的概率（NaturalLanguage）。用错语言的识别器转出来的往往是这种语言的乱码，
    /// 置信度可能不低，但读起来不像这种语言 —— 两个数相乘才可靠。
    static func languageProbability(_ text: String, locale: String) -> Float {
        let recognizer = NLLanguageRecognizer()
        recognizer.processString(text)
        let hypotheses = recognizer.languageHypotheses(withMaximum: 6)
        let wanted: [NLLanguage]
        switch locale {
        case "zh-CN": wanted = [.simplifiedChinese, .traditionalChinese]
        case "zh-TW": wanted = [.traditionalChinese, .simplifiedChinese]
        case "ja-JP": wanted = [.japanese]
        case "ko-KR": wanted = [.korean]
        default: wanted = [.english]
        }
        let probability = wanted.compactMap { hypotheses[$0] }.max() ?? 0
        return Float(max(0.05, probability))
    }
}

// MARK: - 新识别接口（iOS 26+）：SpeechAnalyzer + SpeechTranscriber

/// 旧的 SFSpeechRecognizer 是为近讲听写设计的：2026-09-27 实测旁听（电影 / 远处多人），
/// 电平正常（平均 -20 dBFS）的 3 分钟里 15 个识别任务只出 2 个定稿，重转 9 段中日英三种语言全空。
/// SpeechTranscriber 是苹果为长时、远场、对话音频做的新接口：一条流连续识别不用每 45 秒换任务，
/// 带逐词时间（认人更准），不弹语音识别授权，模型由系统管理（首次按语言下载）。
@available(iOS 26.0, *)
final class NativeLiveTranscriber: @unchecked Sendable {
    typealias Segment = (text: String, start: Double, end: Double)

    private let lock = NSLock()
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var converter: AVAudioConverter?
    private var converterSource: AVAudioFormat?
    private var targetFormat: AVAudioFormat?
    private var backlog: [AVAudioPCMBuffer] = []   // 模型就绪前到的音频（最多约 20 秒）
    private var origin: Double?                    // 第一块音频对应的管线时刻（识别结果的时间从它算起）
    private var analyzer: SpeechAnalyzer?
    private var results: Task<Void, Never>?
    private var stopped = false

    static func supports(_ locale: String) async -> Bool {
        let wanted = Locale(identifier: locale).identifier(.bcp47)
        return await SpeechTranscriber.supportedLocales.contains { $0.identifier(.bcp47) == wanted }
    }

    /// 模型没装就装（首次需要联网）。
    static func prepare(_ transcriber: SpeechTranscriber, locale: String) async throws {
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            NativeAmbientLog.note("新识别接口：正在下载「\(NativeSegmentTranscriber.displayName(locale))」模型")
            try await request.downloadAndInstall()
            NativeAmbientLog.note("新识别接口：「\(NativeSegmentTranscriber.displayName(locale))」模型已装好")
        }
    }

    /// 开始连续识别。音频可以在这之前就开始 append（会先攒着）。
    func start(locale: String,
               onVolatile: @escaping @Sendable (String) -> Void,
               onFinal: @escaping @Sendable ([Segment]) -> Void,
               onError: @escaping @Sendable (Error) -> Void) async throws {
        let transcriber = SpeechTranscriber(locale: Locale(identifier: locale), transcriptionOptions: [],
                                            reportingOptions: [.volatileResults], attributeOptions: [.audioTimeRange])
        try await Self.prepare(transcriber, locale: locale)
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw NSError(domain: "BWAmbient", code: 2, userInfo: [NSLocalizedDescriptionKey: "新识别接口没有可用的音频格式"])
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        results = Task { [weak self] in
            do {
                for try await result in transcriber.results {
                    guard let self else { return }
                    let base = self.lock.withLock { self.origin } ?? 0
                    if result.isFinal {
                        var segments: [Segment] = []
                        for run in result.text.runs {
                            guard let range = run.audioTimeRange else { continue }
                            let text = String(result.text[run.range].characters)
                            if text.trimmingCharacters(in: .whitespaces).isEmpty { continue }
                            segments.append((text, base + range.start.seconds, base + range.end.seconds))
                        }
                        let whole = String(result.text.characters)
                        if segments.isEmpty, !whole.trimmingCharacters(in: .whitespaces).isEmpty {
                            segments = [(whole, base + result.range.start.seconds, base + result.range.end.seconds)]
                        }
                        if !segments.isEmpty { onFinal(segments) }
                    } else {
                        onVolatile(String(result.text.characters))
                    }
                }
            } catch {
                if !(error is CancellationError) { onError(error) }
            }
        }
        try await analyzer.start(inputSequence: stream)
        let pending: [AVAudioPCMBuffer] = lock.withLock {
            self.analyzer = analyzer
            self.targetFormat = format
            self.continuation = continuation
            defer { backlog = [] }
            return backlog
        }
        for buffer in pending { yield(buffer) }
    }

    /// 录音线程上调用。模型还没就绪时先攒着。
    func append(_ buffer: AVAudioPCMBuffer, streamTime: Double) {
        lock.lock()
        if stopped { lock.unlock(); return }
        if origin == nil { origin = streamTime }
        if continuation == nil {
            backlog.append(buffer)
            let seconds = backlog.reduce(0.0) { $0 + Double($1.frameLength) / $1.format.sampleRate }
            if seconds > 20 { backlog.removeFirst() }   // 模型迟迟没好：只保留最近 20 秒
            lock.unlock()
            return
        }
        lock.unlock()
        yield(buffer)
    }

    private func yield(_ buffer: AVAudioPCMBuffer) {
        lock.lock(); defer { lock.unlock() }
        guard let target = targetFormat, let continuation else { return }
        if converterSource != buffer.format || converter == nil {
            converter = AVAudioConverter(from: buffer.format, to: target)
            converterSource = buffer.format
        }
        guard let converter else { return }
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 256
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        if error == nil, out.frameLength > 0 { continuation.yield(AnalyzerInput(buffer: out)) }
    }

    /// 停止：把已收到的音频识别完再结束（最后一句也能出定稿）。
    func finish() async {
        let (analyzer, continuation): (SpeechAnalyzer?, AsyncStream<AnalyzerInput>.Continuation?) = lock.withLock {
            stopped = true
            return (self.analyzer, self.continuation)
        }
        continuation?.finish()
        if let analyzer { try? await analyzer.finalizeAndFinishThroughEndOfInput() }
        _ = await results?.value
    }

    /// 一整段（逐段重转用，16 kHz 单声道）：识别完返回文字与平均置信度。
    static func transcribeOnce(_ samples: [Float], locale: String) async throws -> (text: String, confidence: Float) {
        let transcriber = SpeechTranscriber(locale: Locale(identifier: locale), transcriptionOptions: [],
                                            reportingOptions: [], attributeOptions: [.transcriptionConfidence])
        try await prepare(transcriber, locale: locale)
        guard let target = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]),
              let source = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false),
              let input = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = input.floatChannelData?[0],
              let converter = AVAudioConverter(from: source, to: target),
              let out = AVAudioPCMBuffer(pcmFormat: target,
                                         frameCapacity: AVAudioFrameCount(Double(samples.count) * target.sampleRate / 16_000) + 256) else {
            throw NSError(domain: "BWAmbient", code: 3, userInfo: [NSLocalizedDescriptionKey: "音频格式转换失败"])
        }
        input.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { channel.update(from: $0.baseAddress!, count: samples.count) }
        var supplied = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if supplied { status.pointee = .endOfStream; return nil }
            supplied = true
            status.pointee = .haveData
            return input
        }
        if let error { throw error }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let collect = Task { () -> (String, Float) in
            var text = "", total: Double = 0, count = 0
            for try await result in transcriber.results where result.isFinal {
                text += String(result.text.characters)
                for run in result.text.runs { if let c = run.transcriptionConfidence { total += c; count += 1 } }
            }
            return (text, count > 0 ? Float(total / Double(count)) : 0.5)
        }
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        try await analyzer.start(inputSequence: stream)
        continuation.yield(AnalyzerInput(buffer: out))
        continuation.finish()
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        let (text, confidence) = try await collect.value
        return (text.trimmingCharacters(in: .whitespacesAndNewlines), confidence)
    }
}
