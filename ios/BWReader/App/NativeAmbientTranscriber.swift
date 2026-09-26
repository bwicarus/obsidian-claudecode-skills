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

    private func run(_ samples: [Float], locale: String) async -> Result? {
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
