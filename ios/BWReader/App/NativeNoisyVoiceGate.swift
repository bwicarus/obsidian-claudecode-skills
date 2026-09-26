import AVFoundation
import FluidAudio
import Foundation

/// 嘈杂环境人声隔离（2026-09-26 用户：「嘈杂环境下我基本上无法使用 AI 对话，其他人的声音也会混进来」）。
///
/// 只在**检测到多人说话时**才介入，平时原样放行：
///   1. 通话期间把上行麦克风（48 kHz）降到 16 kHz 送 FluidAudio Sortformer 做流式说话人分离；
///      开通话时先用登记的声纹「我」预热，使用户固定落在同一个槽位。
///   2. 最近 15 秒里有 ≥2 人各说了 ≥1 秒 → 进入「隔离」：
///      - 上行加一段 0.6 秒的延迟线，每帧按分离结果决定：是「我」→ 放行；是别人 → 静音；
///        分离结果还没覆盖到的时刻 → 放行（宁可漏进一点别人，也不切掉用户自己的话）；
///      - 每通电话提示一次苹果系统的「人声突显」（麦克风模式只能由用户在控制中心切，App 只能把面板弹出来）。
///   3. 连续 30 秒只剩一个人 → 退出隔离（只在用户没在说话时退，免得切掉半句）。
///
/// 代价说清楚：隔离期间上行多 0.6 秒延迟；分离模型大约每 0.5 秒跑一次（神经引擎），耗电随之增加。
/// 没登记声纹时按「至今说话最多的人」当作用户，准确度明显差 —— 设置里会提示去登记。
///
/// 线程：`process` 在 NativeAudioEngine 的处理队列上逐帧调用；模型在自己的串行队列上跑；
/// 两边共享的判定状态都在 `lock` 里。
final class NativeNoisyVoiceGate: @unchecked Sendable {
    static let enabledKey = "bw.noisyGate.enabled"
    static let promptIsolationKey = "bw.noisyGate.promptIsolation"

    static var isEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: enabledKey) }
        set { UserDefaults.standard.set(newValue, forKey: enabledKey) }
    }

    static var promptsSystemIsolation: Bool {
        get { UserDefaults.standard.object(forKey: promptIsolationKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: promptIsolationKey) }
    }

    static let frameSeconds = 0.02
    static let delayFrames = 30                 // 0.6 s
    static let engageWindow = 15.0
    static let engageMinSpeech = 1.0
    static let releaseQuietSeconds = 30.0
    static let userPadding = 0.2

    /// 给设置页看的实时状态（经 NativeNoisyGateMonitor 发布）。
    struct Status: Equatable {
        var running = false
        var modelReady = false
        var engaged = false
        var speakers = 0
        var usesVoiceprint = false
    }
    private(set) var status = Status()

    private let lock = NSLock()
    private let modelQueue = DispatchQueue(label: "space.bwicarus.reader.noisy-gate", qos: .userInitiated)
    private var generation = 0
    private var diarizer: NativeStreamDiarizer?
    private var frameIndex = 0                  // 本通话已处理的上行帧数（= 时间轴）
    private var diarizerOrigin = 0.0            // 分离器时间 0 对应的通话时刻
    private var engaged = false
    private var lastMultiSpeakerAt = -1.0
    private var delayLine: [[Int16]] = []
    private var lastKept = true
    private var promptedThisCall = false
    private var userIndex: Int?
    private var loggedFallback = false
    private var failures = 0
    // 模型队列每次出结果后拍的快照（分离器时间轴）。音频队列只读这两个，绝不碰分离器本身 ——
    // 分离器的时间线正被模型队列改写，跨线程读就是数据竞争。
    private var userRanges: [ClosedRange<Double>] = []
    private var analyzedUntil = 0.0

    // MARK: 生命周期

    /// 通话音频开始时调。设置关着就什么都不做（出声一次，免得以为坏了）。
    func begin() {
        lock.lock()
        generation += 1
        let ticket = generation
        resetLocked()
        lock.unlock()
        guard Self.isEnabled else { return }
        publish { $0 = Status(running: true) }
        Task.detached(priority: .userInitiated) { [weak self] in
            do {
                let models = try await NativeSortformerModels.shared.models()
                let voiceprint = NativeVoiceprint.load()
                self?.modelQueue.async {
                    guard let self else { return }
                    do {
                        let diarizer = try NativeStreamDiarizer(models: models, voiceprint: voiceprint)
                        self.lock.lock()
                        guard ticket == self.generation else { self.lock.unlock(); return }
                        self.diarizer = diarizer
                        self.diarizerOrigin = Double(self.frameIndex) * Self.frameSeconds
                        self.userIndex = diarizer.userIndex
                        self.lock.unlock()
                        self.publish { $0.modelReady = true; $0.usesVoiceprint = diarizer.userIndex != nil }
                        NativeAmbientLog.note(diarizer.userIndex != nil
                            ? "通话降噪：已就绪（用登记的声纹认你）"
                            : "通话降噪：已就绪，但没登记声纹 —— 多人时按「说话最多的人」当作你，建议去设置登记")
                    } catch {
                        NativeAmbientLog.note("通话降噪：分离器创建失败 \(error.localizedDescription)，本通话原样放行", level: "error")
                    }
                }
            } catch {
                NativeAmbientLog.note("通话降噪：模型不可用，本通话原样放行（\(error.localizedDescription)）", level: "error")
            }
        }
    }

    func end() {
        lock.lock()
        generation += 1
        let wasEngaged = engaged
        resetLocked()
        lock.unlock()
        publish { $0 = Status() }
        if wasEngaged { NativeAmbientLog.note("通话降噪：通话结束，退出隔离") }
    }

    private func resetLocked() {
        diarizer = nil
        frameIndex = 0
        diarizerOrigin = 0
        engaged = false
        lastMultiSpeakerAt = -1
        delayLine = []
        lastKept = true
        promptedThisCall = false
        userIndex = nil
        loggedFallback = false
        failures = 0
        userRanges = []
        analyzedUntil = 0
    }

    // MARK: 逐帧

    /// 一进一出：未隔离时原样返回；隔离时返回 0.6 秒前那一帧（或其静音版）。
    func process(_ frame: [Int16]) -> [Int16] {
        lock.lock()
        let now = Double(frameIndex) * Self.frameSeconds
        frameIndex += 1
        let active = diarizer
        let ticket = generation
        let isEngaged = engaged
        lock.unlock()
        guard let active else { return frame }

        let samples = NativeResampler16k.from48kPCM(frame)
        modelQueue.async { [weak self] in self?.analyze(samples, diarizer: active, ticket: ticket) }

        guard isEngaged else { return frame }
        lock.lock()
        delayLine.append(frame)
        let output = delayLine.count > Self.delayFrames ? delayLine.removeFirst() : [Int16](repeating: 0, count: frame.count)
        let outputTime = now - Double(Self.delayFrames) * Self.frameSeconds
        let keep = shouldKeepLocked(at: outputTime)
        let previous = lastKept
        lastKept = keep
        lock.unlock()
        return Self.shape(output, keep: keep, wasKept: previous)
    }

    /// 在模型队列上：喂模型 → 更新「几个人在说话」→ 决定进入 / 退出隔离。
    private func analyze(_ samples: [Float], diarizer active: NativeStreamDiarizer, ticket: Int) {
        do {
            guard try active.feed(samples) != nil else { return }
        } catch {
            lock.lock(); failures += 1; let count = failures; lock.unlock()
            if count == 1 || count % 100 == 0 {
                NativeAmbientLog.note("通话降噪：分离模型出错（第 \(count) 次）\(error.localizedDescription)", level: "error")
            }
            return
        }
        let speakers = active.activeSpeakers(within: Self.engageWindow, minSpeech: Self.engageMinSpeech)
        let guess = speakers.count >= 2 ? active.mostTalkative() : nil
        let analyzed = active.analyzedUntil
        let elapsed = active.elapsed
        lock.lock()
        guard ticket == generation else { lock.unlock(); return }
        let now = Double(frameIndex) * Self.frameSeconds
        if userIndex == nil, let guess {
            // 没声纹：进入多人状态那一刻锁定「说话最多的人」为用户，之后不再换人（换人会来回切）
            userIndex = guess
            if !loggedFallback {
                loggedFallback = true
                NativeAmbientLog.note("通话降噪：没有声纹，把说话最多的 \(guess + 1) 号当作你")
            }
        }
        let user = userIndex
        lock.unlock()
        // 用户最近 6 秒的说话区间（分离器时间轴），在锁外算
        let ranges = user.map { active.speech(of: $0, from: elapsed - 6, to: elapsed + 1) } ?? []
        lock.lock()
        guard ticket == generation else { lock.unlock(); return }
        userRanges = ranges
        analyzedUntil = analyzed
        var event: String?
        if speakers.count >= 2 {
            lastMultiSpeakerAt = now
            if !engaged {
                engaged = true
                delayLine = Array(repeating: [Int16](repeating: 0, count: NativeAudioEngine.samplesPerFrame),
                                  count: Self.delayFrames)
                lastKept = true
                event = "engage"
            }
        } else if engaged, now - lastMultiSpeakerAt >= Self.releaseQuietSeconds,
                  !userSpeakingLocked(from: now - 1, to: now) {
            engaged = false
            delayLine = []
            event = "release"
        }
        let prompt = event == "engage" && !promptedThisCall && Self.promptsSystemIsolation
        if prompt { promptedThisCall = true }
        let isEngaged = engaged
        lock.unlock()

        publish { $0.engaged = isEngaged; $0.speakers = speakers.count }
        switch event {
        case "engage":
            NativeAmbientLog.note("通话降噪：检测到 \(speakers.count) 人在说话，进入隔离（只放行你的声音，上行多 0.6 秒延迟）")
            if prompt { Self.promptSystemVoiceIsolation() }
        case "release":
            NativeAmbientLog.note("通话降噪：30 秒只剩一个人，退出隔离")
        default: break
        }
    }

    // MARK: 判定

    private func shouldKeepLocked(at time: Double) -> Bool {
        let local = time - diarizerOrigin
        // 分离结果还没覆盖到这一刻：放行（宁可漏进别人，也不切掉用户）
        guard local <= analyzedUntil else { return true }
        guard userIndex != nil else { return true }
        let pad = Self.userPadding
        return userRanges.contains { $0.lowerBound - pad <= local && local <= $0.upperBound + pad }
    }

    private func userSpeakingLocked(from: Double, to: Double) -> Bool {
        let a = from - diarizerOrigin, b = to - diarizerOrigin
        return userRanges.contains { $0.lowerBound <= b && a <= $0.upperBound }
    }

    /// 切换时在一帧内做线性淡入淡出，免得「咔哒」一声。
    static func shape(_ frame: [Int16], keep: Bool, wasKept: Bool) -> [Int16] {
        if keep && wasKept { return frame }
        if !keep && !wasKept { return [Int16](repeating: 0, count: frame.count) }
        let count = frame.count
        var out = frame
        for index in 0..<count {
            let ramp = Double(index) / Double(max(1, count - 1))
            let gain = keep ? ramp : 1 - ramp
            out[index] = Int16((Double(frame[index]) * gain).rounded())
        }
        return out
    }

    // MARK: 苹果系统人声突显

    /// 麦克风模式只能由用户切（系统限制），这里只在「当前不是人声突显」时把控制中心面板弹出来。
    static func promptSystemVoiceIsolation() {
        DispatchQueue.main.async {
            let mode = AVCaptureDevice.activeMicrophoneMode
            guard mode != .voiceIsolation else {
                NativeAmbientLog.note("通话降噪：系统人声突显已经开着")
                return
            }
            NativeAmbientLog.note("通话降噪：当前麦克风模式不是人声突显，弹出系统面板请你切换")
            AVCaptureDevice.showSystemUserInterface(.microphoneModes)
        }
    }

    private func publish(_ change: (inout Status) -> Void) {
        lock.lock()
        change(&status)
        let snapshot = status
        lock.unlock()
        Task { @MainActor in NativeNoisyGateMonitor.shared.status = snapshot }
    }
}

/// 设置页看闸门状态用。两个音频引擎（语音桥 / 语音会话）各有一个闸门，但同时只会有一路通话，
/// 所以最后发布的那个就是当前的。
@MainActor
final class NativeNoisyGateMonitor: ObservableObject {
    static let shared = NativeNoisyGateMonitor()
    @Published var status = NativeNoisyVoiceGate.Status()
    private init() {}
}
