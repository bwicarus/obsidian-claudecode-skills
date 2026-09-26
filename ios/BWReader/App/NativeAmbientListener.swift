import AVFoundation
import Foundation
import Speech
import UserNotifications

/// 环境旁听（2026-09-26 用户：「持续录音并转写，用 FluidAudio 分离不同的人，让 jev 判断
/// 是否有意义 / 是否有即时危险 / 是否有要解决的疑问 / 是否需要记录」—— 先在 iPad 上做，手表眼镜以后）。
///
/// 链路（全部在 iPad 本机，只有「一段话的文字」出网）：
///
///   麦克风 ──┬─ SFSpeechRecognizer（强制本机识别）── 带时间戳的词段
///            ├─ 16 kHz → FluidAudio Sortformer ──── 谁在什么时候说话
///            └─ 16 kHz 环形缓冲（最近 4 分钟）───── 需要时存原声
///   词段 + 说话人 → 按人合并成句 → 按停顿切「窗口」→ POST 服务器 /api/ambient/judge（jev）
///   回来的动作：danger_record 持续录音 5 分钟 + 通知 / danger_watch 通知 / save_audio 存本窗原声；
///   记文字、交 AI 解答、第二层分类、滚动摘要都在服务器做（见 _server_deploy/ambient_jev.py）。
///
/// 语音识别的切段：本机识别只有在 `endAudio()` 之后才给带时间戳的定稿，所以检测到 1.2 秒静音
/// （或一段满 45 秒）就结束当前识别任务、立刻换新任务接着听。
///
/// 任何时候都在工作（用户 2026-09-26：「jev 实时判断要做成独立模块，任意时间工作，而不是只有语音开启时」）：
/// 平时用自己的麦克风引擎；电脑 / AI 通话开始时（NativeAudioEngine.callAudioDidChange）麦克风归通话，
/// 旁听就换成接通话引擎的上行帧（NativeAudioEngine.microphoneTap，已做回声消除）继续转写和判断，
/// 通话结束再换回自己的引擎。与语音有没有开没有任何依赖。
///
/// 诊断：所有提前退出、失败、判断结果都进 NativeAmbientLog（设置页可见 + 服务器 client-log）。
@MainActor
final class NativeAmbientListener: ObservableObject {
    static let shared = NativeAmbientListener()

    static let enabledKey = "bw.ambient.enabled"
    static let localeKey = "bw.ambient.locale"
    static let supportedLocales = ["zh-CN", "ja-JP", "en-US"]

    @Published private(set) var isEnabled = UserDefaults.standard.bool(forKey: enabledKey)
    @Published private(set) var state = "未开启"
    @Published private(set) var partialText = ""
    @Published private(set) var speakersNow = 0
    @Published private(set) var lastJudgment = ""
    @Published private(set) var dangerRecordingUntil: Date?
    @Published private(set) var feed: [[String: Any]] = []
    @Published var locale = UserDefaults.standard.string(forKey: localeKey) ?? "zh-CN" {
        didSet {
            UserDefaults.standard.set(locale, forKey: Self.localeKey)
            if isEnabled, oldValue != locale { restart(reason: "识别语言改为 \(locale)") }
        }
    }

    private var pipeline: NativeAmbientPipeline?
    private var callObserver: NSObjectProtocol?
    private var interruptionObserver: NSObjectProtocol?
    private var source: NativeAmbientPipeline.Source = .microphone
    private var feedTask: Task<Void, Never>?
    private var feedSince = 0

    private init() {
        callObserver = NotificationCenter.default.addObserver(
            forName: NativeAudioEngine.callAudioDidChange, object: nil, queue: .main
        ) { [weak self] note in
            let active = note.userInfo?["active"] as? Bool ?? false
            MainActor.assumeIsolated { self?.callAudioChanged(active: active) }
        }
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(), queue: .main
        ) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            MainActor.assumeIsolated { self?.interrupted(ended: raw == AVAudioSession.InterruptionType.ended.rawValue) }
        }
    }

    /// App 启动时调：上次开着就接着开（旁听是长期开关，不是一次性动作）。
    func resumeIfEnabled() {
        guard isEnabled, pipeline == nil else { return }
        Task { await startPipeline(reason: "App 启动时恢复") }
    }

    /// 当前该用哪个音频来源：有通话在跑就接通话的上行，否则用自己的麦克风。
    private var currentSource: NativeAmbientPipeline.Source {
        NativeAudioEngine.activeCallAudio > 0 ? .call : .microphone
    }

    func setEnabled(_ enabled: Bool) {
        isEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: Self.enabledKey)
        if enabled {
            Task { await startPipeline(reason: "用户开启") }
        } else {
            stopPipeline(reason: "用户关闭")
            state = "未开启"
        }
    }

    /// 声纹登记要独占麦克风：先停旁听，录完再恢复。
    func withMicrophoneReleased(_ work: () async throws -> Void) async rethrows {
        let wasRunning = pipeline != nil && source == .microphone
        if wasRunning { stopPipeline(reason: "登记声纹，暂时让出麦克风") }
        defer { if wasRunning && isEnabled { Task { await startPipeline(reason: "声纹登记结束") } } }
        try await work()
    }

    private func restart(reason: String) {
        stopPipeline(reason: reason)
        Task { await startPipeline(reason: reason) }
    }

    private func callAudioChanged(active: Bool) {
        guard isEnabled else { return }
        let wanted = currentSource
        guard pipeline == nil || wanted != source else { return }
        stopPipeline(reason: active ? "通话开始，麦克风归通话，改接通话上行" : "通话结束，换回自己的麦克风")
        Task {
            // 通话结束时等那边把音频会话交出来；开始时不用等（接的是它的帧，不碰会话）
            if !active { try? await Task.sleep(nanoseconds: 1_500_000_000) }
            await self.startPipeline(reason: active ? "通话中继续旁听" : "通话结束，接着旁听")
        }
    }

    private func interrupted(ended: Bool) {
        // 只管自己的麦克风引擎；通话来源的中断由通话那边处理
        guard isEnabled, currentSource == .microphone else { return }
        if ended, pipeline == nil {
            Task { await startPipeline(reason: "音频中断结束") }
        } else if !ended, pipeline != nil {
            stopPipeline(reason: "被系统音频中断（来电 / 别的 App 录音）")
            state = "被系统音频中断"
        }
    }

    // MARK: 启停

    private func startPipeline(reason: String) async {
        guard isEnabled, pipeline == nil else { return }
        let source = currentSource
        state = "准备中…"
        guard await Self.requestPermissions() else {
            state = "缺少权限（麦克风 / 语音识别）"
            NativeAmbientLog.note("旁听：没拿到麦克风或语音识别权限，未开始", level: "error")
            return
        }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale)) else {
            state = "不支持识别 \(locale)"
            NativeAmbientLog.note("旁听：系统不支持识别 \(locale)", level: "error")
            return
        }
        guard recognizer.supportsOnDeviceRecognition else {
            // 不退回苹果服务器识别：旁听的原声不该离开设备
            state = "\(locale) 不支持本机识别"
            NativeAmbientLog.note("旁听：\(locale) 没有本机识别模型（设置 → 通用 → 键盘 → 听写 里下载），未开始", level: "error")
            return
        }
        let pipeline = NativeAmbientPipeline(recognizer: recognizer, locale: locale, source: source) { [weak self] event in
            Task { @MainActor in self?.handle(event) }
        }
        do {
            try pipeline.start()
        } catch {
            state = "启动失败"
            NativeAmbientLog.note("旁听：启动失败 \(error.localizedDescription)", level: "error")
            return
        }
        self.pipeline = pipeline
        self.source = source
        state = source == .call ? "正在旁听（接通话上行）" : "正在旁听"
        NativeAmbientLog.note("旁听：已开始（\(reason)，\(source == .call ? "通话上行" : "自己的麦克风")，语言 \(locale)，声纹\(NativeVoiceprint.exists ? "已登记" : "未登记")）")
        startFeedPolling()
    }

    private func stopPipeline(reason: String) {
        feedTask?.cancel()
        feedTask = nil
        guard let pipeline else { return }
        self.pipeline = nil
        pipeline.stop(releaseSession: NativeAudioEngine.activeCallAudio == 0)
        partialText = ""
        speakersNow = 0
        NativeAmbientLog.note("旁听：已停止（\(reason)）")
    }

    static func requestPermissions() async -> Bool {
        let mic = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
        }
        let speech = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0 == .authorized) }
        }
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        return mic && speech
    }

    // MARK: 管线事件

    private func handle(_ event: NativeAmbientPipeline.Event) {
        switch event {
        case .partial(let text):
            partialText = text
        case .speakers(let count):
            speakersNow = count
        case .failed(let message):
            NativeAmbientLog.note("旁听：\(message)，5 秒后重启", level: "error")
            stopPipeline(reason: message)
            state = "出错，稍后重启"
            Task {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                await self.startPipeline(reason: "出错后重启")
            }
        case .window(let window):
            Task { await judge(window) }
        }
    }

    // MARK: 送判断

    private var outbox: [[String: Any]] = []

    private func judge(_ window: NativeAmbientPipeline.Window) async {
        let body = window.payload
        do {
            let reply = try await NativeAmbientServer.post("api/ambient/judge", body: body, timeout: 30)
            let actions = reply["actions"] as? [String] ?? []
            let judgments = reply["judgments"] as? [String: [String: Any]] ?? [:]
            let summary = ["meaningful", "danger", "question", "record"].compactMap { name -> String? in
                guard let choice = judgments[name]?["choice"] as? String else { return nil }
                return "\(name)=\(choice)"
            }.joined(separator: " ")
            lastJudgment = summary + (actions.isEmpty ? "" : " → " + actions.joined(separator: ","))
            NativeAmbientLog.note("旁听判断 \(window.id)（\(window.utteranceCount) 句 \(window.speakerCount) 人）：\(lastJudgment)")
            perform(actions, window: window)
            await flushOutbox()
        } catch {
            NativeAmbientLog.note("旁听判断失败 \(window.id)：\(error.localizedDescription)（本段留在本机待重发）", level: "error")
            outbox.append(body)
            if outbox.count > 50 { outbox.removeFirst(outbox.count - 50) }
            // 送不出去时原声先存下来：服务器没回话不等于这段不重要
            pipeline?.saveAudio(window, reason: "判断失败先留原声")
        }
    }

    private func flushOutbox() async {
        guard !outbox.isEmpty else { return }
        let pending = outbox
        outbox = []
        for body in pending {
            do { _ = try await NativeAmbientServer.post("api/ambient/judge", body: body, timeout: 30) }
            catch {
                outbox.append(body)
                NativeAmbientLog.note("旁听：补发积压窗口失败（还剩 \(outbox.count) 段）", level: "error")
                return
            }
        }
        NativeAmbientLog.note("旁听：积压的 \(pending.count) 段已补发（补发只记录，不再执行动作）")
    }

    private func perform(_ actions: [String], window: NativeAmbientPipeline.Window) {
        if actions.contains("danger_record") {
            let until = Date().addingTimeInterval(5 * 60)
            dangerRecordingUntil = until
            pipeline?.startDangerRecording(until: until)
            Self.notify(title: "旁听：可能有危险", body: "已开始持续录音 5 分钟。\n" + window.lastLine)
        } else if actions.contains("danger_watch") {
            Self.notify(title: "旁听：需要留意", body: window.lastLine)
        }
        if actions.contains("save_audio") {
            pipeline?.saveAudio(window, reason: actions.contains("danger_record") ? "危险" : "值得保存原声")
        }
    }

    static func notify(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = String(body.prefix(240))
        content.sound = .default
        let request = UNNotificationRequest(identifier: "ambient-" + UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error { NativeAmbientLog.note("旁听通知发不出：\(error.localizedDescription)", level: "error") }
        }
    }

    // MARK: 服务器侧产出（AI 解答 / 待办 / 摘要）

    private func startFeedPolling() {
        feedTask?.cancel()
        feedTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshFeed(notifyNew: true)
                try? await Task.sleep(nanoseconds: 60_000_000_000)
            }
        }
    }

    func refreshFeed(notifyNew: Bool = false) async {
        do {
            let reply = try await NativeAmbientServer.get("api/ambient/feed?since=\(feedSince)")
            let entries = reply["entries"] as? [[String: Any]] ?? []
            guard !entries.isEmpty else { return }
            let firstLoad = feedSince == 0
            feedSince = entries.compactMap { $0["t"] as? Int }.max() ?? feedSince
            feed = Array((feed + entries).suffix(80))
            guard notifyNew, !firstLoad else { return }
            for entry in entries {
                let text = entry["text"] as? String ?? ""
                switch entry["kind"] as? String {
                case "answer": Self.notify(title: "旁听：AI 解答", body: text)
                case "task": Self.notify(title: "旁听：记下一件待办", body: text)
                default: break
                }
            }
        } catch {
            NativeAmbientLog.note("旁听：取服务器记录失败 \(error.localizedDescription)", level: "error")
        }
    }
}

// MARK: - 管线（麦克风 / 识别 / 分离 / 分窗 / 存音频）

/// 只被 NativeAmbientListener 持有。内部三条线程：音频回调（tap）、`work` 串行队列（分离、合句、分窗、存盘）、
/// 识别回调队列（结果立刻转投 `work`）。所有可变状态只在 `work` 上改；tap 与识别器交接用 `tapLock`。
final class NativeAmbientPipeline: @unchecked Sendable {
    /// 音频来源：自己的麦克风引擎，或通话引擎的上行旁路（48 kHz 单声道 20 ms 帧）。
    enum Source {
        case microphone
        case call

        var wireName: String { self == .call ? "ipad-call" : "ipad-mic" }
    }

    enum Event {
        case partial(String)
        case speakers(Int)
        case window(Window)
        case failed(String)
    }

    struct Utterance {
        var speaker: Int?
        var text: String
        var start: Double
        var end: Double
    }

    struct Window {
        let id: String
        let startedAt: Date
        let endedAt: Date
        let streamStart: Double
        let streamEnd: Double
        let utterances: [[String: Any]]
        let speakerCount: Int
        let locale: String
        let source: String

        var utteranceCount: Int { utterances.count }
        var lastLine: String {
            guard let last = utterances.last else { return "" }
            return "\(last["speaker"] as? String ?? "?")：\(last["text"] as? String ?? "")"
        }
        var payload: [String: Any] {
            ["windowId": id, "startedAt": Int(startedAt.timeIntervalSince1970 * 1000),
             "endedAt": Int(endedAt.timeIntervalSince1970 * 1000), "speakerCount": speakerCount,
             "locale": locale, "source": source, "utterances": utterances]
        }
    }

    static let silenceToFinalize = 1.2          // 秒：静音这么久就结束当前识别任务拿定稿
    static let maxRecognitionSeconds = 45.0
    static let windowGap = 12.0                 // 秒：两句之间停这么久就切窗口
    static let windowMaxSeconds = 120.0
    static let windowMaxChars = 1500
    static let assignDelay = 1.5                // 秒：等分离结果定稿再给句子认人
    static let ringSeconds = 240.0
    static let diarizerRecycleSeconds = 20 * 60.0

    private let recognizer: SFSpeechRecognizer
    private let locale: String
    private let source: Source
    private let emit: (Event) -> Void
    private let engine = AVAudioEngine()
    private let work = DispatchQueue(label: "space.bwicarus.reader.ambient", qos: .utility)
    private let recognitionQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.qualityOfService = .utility
        return queue
    }()

    // tap ↔ 识别器交接
    private let tapLock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var streamFrames: Int64 = 0         // 原生采样率下已收到的帧数（整条管线的时钟）
    private var sampleRate: Double = 48_000
    private var running = false

    // 以下只在 work 队列上
    private var resampler = NativeResampler16k(sourceRate: 48_000)
    private var diarizer: NativeStreamDiarizer?
    private var diarizerOrigin = 0.0
    private var diarizerLoading = false
    private var ring: [Int16] = []
    private var ringStart = 0.0                 // ring[0] 对应的管线时刻
    private var task: SFSpeechRecognitionTask?
    private var taskGeneration = 0
    private var taskStarts: [Int: Double] = [:]  // 每个识别任务的起点（管线时刻）= 它的词段时间戳的零点
    private var rotationClock = 0.0             // 本任务开始计时的时刻（只用于「满 45 秒换任务」）
    private var lastText = ""
    private var lastTextAt = 0.0                // 识别文字最后一次变化的时刻：停顿判据
    private var heardTextInTask = false
    private var lastSpeakerCount = -1
    private var loggedDangerWriteFailure = false
    private var pendingFinals: [(ready: Double, segments: [(text: String, start: Double, end: Double)])] = []
    private var utterances: [Utterance] = []
    private var windowStartedAt = Date()
    private var dangerFile: AVAudioFile?
    private var dangerUntil = Date.distantPast
    private var tick: DispatchSourceTimer?
    private var loggedNoDiarizer = false

    init(recognizer: SFSpeechRecognizer, locale: String, source: Source, emit: @escaping (Event) -> Void) {
        self.recognizer = recognizer
        self.locale = locale
        self.source = source
        self.emit = emit
        recognizer.queue = recognitionQueue
    }

    private var now: Double {
        tapLock.lock(); defer { tapLock.unlock() }
        return Double(streamFrames) / sampleRate
    }

    private var currentSampleRate: Double {
        tapLock.lock(); defer { tapLock.unlock() }
        return sampleRate
    }

    private var isRunning: Bool {
        tapLock.lock(); defer { tapLock.unlock() }
        return running
    }

    func start() throws {
        if source == .call { return startFromCall() }
        let session = AVAudioSession.sharedInstance()
        // mixWithOthers：旁听不打断音乐 / 视频；不开语音处理：要听见周围所有人，而不只是近讲
        try session.setCategory(.playAndRecord, mode: .default,
                                options: [.mixWithOthers, .allowBluetoothHFP, .defaultToSpeaker])
        try session.setActive(true)
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate >= 8_000, format.channelCount >= 1 else {
            throw NSError(domain: "BWAmbient", code: 1, userInfo: [NSLocalizedDescriptionKey: "麦克风格式不可用"])
        }
        tapLock.lock()
        sampleRate = format.sampleRate
        streamFrames = 0
        running = true
        tapLock.unlock()
        work.sync {
            resampler = NativeResampler16k(sourceRate: format.sampleRate)
            beginRecognitionTask()
        }
        input.installTap(onBus: 0, bufferSize: 4_096, format: format) { [weak self] buffer, _ in
            self?.captured(buffer)
        }
        engine.prepare()
        do { try engine.start() }
        catch {
            input.removeTap(onBus: 0)
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw error
        }
        startTimerAndDiarizer()
    }

    /// 通话来源：不碰音频会话（归通话所有），只订阅通话引擎的上行帧。
    private func startFromCall() {
        tapLock.lock()
        sampleRate = NativeAudioEngine.sampleRate
        streamFrames = 0
        running = true
        tapLock.unlock()
        work.sync {
            resampler = NativeResampler16k(sourceRate: NativeAudioEngine.sampleRate)
            beginRecognitionTask()
        }
        NativeAudioEngine.microphoneTap.set { [weak self] frame in self?.callFrame(frame) }
        startTimerAndDiarizer()
    }

    private let callFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: NativeAudioEngine.sampleRate,
                                                channels: 1, interleaved: false)

    private func callFrame(_ frame: [Int16]) {
        guard let format = callFormat,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frame.count)),
              let channel = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(frame.count)
        for index in 0..<frame.count { channel[index] = Float(frame[index]) / 32_768 }
        captured(buffer)
    }

    private func startTimerAndDiarizer() {
        let timer = DispatchSource.makeTimerSource(queue: work)
        timer.schedule(deadline: .now() + 0.5, repeating: 0.5)
        timer.setEventHandler { [weak self] in self?.periodic() }
        timer.resume()
        tick = timer
        work.async { self.loadDiarizer() }
    }

    /// `releaseSession`：只有没有通话时才交还音频会话 —— 通话刚开始时停旁听，
    /// 这里若 setActive(false) 就会把通话刚激活的同一个会话一起关掉（会话是整个 App 共用的）。
    func stop(releaseSession: Bool) {
        tapLock.lock()
        running = false
        let current = request
        request = nil
        tapLock.unlock()
        if source == .call {
            NativeAudioEngine.microphoneTap.set(nil)
        } else {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        current?.endAudio()
        tick?.cancel()
        tick = nil
        work.async {
            self.task?.cancel()
            self.task = nil
            self.closeDangerFile()
            // 停下时手里还没送的句子也送出去，别丢
            self.flushWindow(force: true)
        }
        if source == .microphone && releaseSession {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    // MARK: 音频回调

    private func captured(_ buffer: AVAudioPCMBuffer) {
        tapLock.lock()
        guard running else { tapLock.unlock(); return }
        request?.append(buffer)
        streamFrames += Int64(buffer.frameLength)
        let end = Double(streamFrames) / sampleRate
        tapLock.unlock()
        guard let channel = buffer.floatChannelData?[0] else { return }
        let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        work.async { [weak self] in self?.consume(samples, end: end) }
        if let file = dangerFileForTap() {
            do { try file.write(from: buffer) }
            catch {
                dangerLock.lock()
                let first = !loggedDangerWriteFailure
                loggedDangerWriteFailure = true
                dangerLock.unlock()
                if first { NativeAmbientLog.note("旁听：危险录音写入失败 \(error.localizedDescription)", level: "error") }
            }
        }
    }

    private let dangerLock = NSLock()
    private var dangerFileShared: AVAudioFile?
    private func dangerFileForTap() -> AVAudioFile? {
        dangerLock.lock(); defer { dangerLock.unlock() }
        return dangerFileShared
    }

    private func consume(_ samples: [Float], end: Double) {
        let pcm16k = resampler.process(samples)
        // 环形缓冲（给「存原声」用）
        if ring.isEmpty { ringStart = end - Double(samples.count) / resampler.sourceRate }
        ring.append(contentsOf: pcm16k.map { Int16(max(-1, min(1, $0)) * 32_767) })
        let capacity = Int(Self.ringSeconds * NativeStreamDiarizer.sampleRate)
        if ring.count > capacity + 16_000 * 30 {
            let drop = ring.count - capacity
            ring.removeFirst(drop)
            ringStart += Double(drop) / NativeStreamDiarizer.sampleRate
        }
        // 说话人分离
        if let diarizer {
            do {
                if try diarizer.feed(pcm16k) != nil {
                    let speakers = diarizer.activeSpeakers(within: 15, minSpeech: 1).count
                    if speakers != lastSpeakerCount {
                        lastSpeakerCount = speakers
                        emit(.speakers(speakers))
                    }
                }
            } catch {
                NativeAmbientLog.note("旁听：说话人分离出错 \(error.localizedDescription)", level: "error")
            }
        }
    }

    // MARK: 说话人分离

    private func loadDiarizer() {
        guard diarizer == nil, !diarizerLoading else { return }
        diarizerLoading = true
        Task.detached(priority: .utility) { [weak self] in
            do {
                let models = try await NativeSortformerModels.shared.models()
                let voiceprint = NativeVoiceprint.load()
                self?.work.async {
                    guard let self else { return }
                    self.diarizerLoading = false
                    do {
                        self.diarizer = try NativeStreamDiarizer(models: models, voiceprint: voiceprint)
                        self.diarizerOrigin = self.ring.isEmpty ? 0 : self.ringStart + Double(self.ring.count) / NativeStreamDiarizer.sampleRate
                        NativeAmbientLog.note("旁听：说话人分离已接上（\(voiceprint == nil ? "没有声纹，说话人只按编号" : "用声纹认出「我」")）")
                    } catch {
                        NativeAmbientLog.note("旁听：说话人分离创建失败 \(error.localizedDescription)，只转写不分人", level: "error")
                    }
                }
            } catch {
                self?.work.async { self?.diarizerLoading = false }
                NativeAmbientLog.note("旁听：说话人分离模型不可用，只转写不分人（\(error.localizedDescription)）", level: "error")
            }
        }
    }

    private func speakerLabel(_ index: Int?) -> (String, Bool) {
        guard let index else { return ("?", false) }
        if index == diarizer?.userIndex { return ("我", true) }
        return ("说话人\(index + 1)", false)
    }

    // MARK: 语音识别（按停顿切任务）

    private func beginRecognitionTask() {
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        taskGeneration += 1
        let generation = taskGeneration
        let start = now
        taskStarts[generation] = start
        rotationClock = start
        heardTextInTask = false
        lastText = ""
        tapLock.lock()
        self.request = request
        tapLock.unlock()
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            self?.work.async { self?.recognized(result, error: error, generation: generation) }
        }
    }

    /// 结束当前任务（它会带着定稿回调 recognized），并立刻开新任务接着听。
    private func rotateRecognition() {
        tapLock.lock()
        let finishing = request
        tapLock.unlock()
        beginRecognitionTask()
        finishing?.endAudio()
    }

    private func recognized(_ result: SFSpeechRecognitionResult?, error: Error?, generation: Int) {
        if let result {
            let text = result.bestTranscription.formattedString
            if generation == taskGeneration, text != lastText {
                // 嘈杂环境里音量永远不低，拿音量判停顿会一直判不出；改看「识别文字多久没变」
                lastText = text
                lastTextAt = now
                heardTextInTask = heardTextInTask || !text.isEmpty
                emit(.partial(text))
            }
            if result.isFinal, let base = taskStarts[generation] {
                let segments = result.bestTranscription.segments.map {
                    (text: $0.substring, start: base + $0.timestamp, end: base + $0.timestamp + $0.duration)
                }
                if !segments.isEmpty { pendingFinals.append((ready: now + Self.assignDelay, segments: segments)) }
                taskStarts[generation] = nil
                if generation == taskGeneration { emit(.partial("")) }
            }
        }
        if let error {
            taskStarts[generation] = nil
            let code = (error as NSError).code
            // 1110 = 没检测到语音、216/203 = 任务被取消 / 结束：旋转时的正常结局，不算故障
            let benign = [1110, 216, 203, 301].contains(code)
            if !benign {
                NativeAmbientLog.note("旁听：识别任务出错 code=\(code) \(error.localizedDescription)", level: "error")
            }
            if generation == taskGeneration && isRunning {
                if benign { beginRecognitionTask() }
                else { emit(.failed("识别任务出错 code=\(code)")) }
            }
        }
    }

    // MARK: 周期（每 0.5 秒，在 work 上）

    private func periodic() {
        let t = now
        // 1) 切识别任务：识别出文字之后 1.2 秒没有新字，或本任务满 45 秒
        if heardTextInTask && t - lastTextAt >= Self.silenceToFinalize || t - rotationClock >= Self.maxRecognitionSeconds {
            if heardTextInTask { rotateRecognition() }
            else { rotationClock = t }  // 45 秒一个字都没有：不必换任务
        }
        // 2) 定稿满 1.5 秒的，按分离结果认人、合句
        while let first = pendingFinals.first, first.ready <= t {
            pendingFinals.removeFirst()
            assign(first.segments)
        }
        // 3) 切窗口
        flushWindow(force: false)
        // 4) 危险录音到点
        if dangerFile != nil, Date() >= dangerUntil { closeDangerFile() }
        // 5) 分离器定期换新（时间线会一直长；只在窗口刚清空时换，免得同一窗口里编号变）
        if let diarizer, utterances.isEmpty, pendingFinals.isEmpty, diarizer.elapsed > Self.diarizerRecycleSeconds {
            self.diarizer = nil
            loadDiarizer()
        }
    }

    private func assign(_ segments: [(text: String, start: Double, end: Double)]) {
        if diarizer == nil, !loggedNoDiarizer {
            loggedNoDiarizer = true
            NativeAmbientLog.note("旁听：分离器还没就绪，这些句子不分人")
        }
        for segment in segments {
            let speaker = diarizer?.dominantSpeaker(from: segment.start - diarizerOrigin, to: segment.end - diarizerOrigin)
            if var last = utterances.last, last.speaker == speaker, segment.start - last.end < 1.5 {
                last.text += segment.text
                last.end = segment.end
                utterances[utterances.count - 1] = last
            } else {
                utterances.append(Utterance(speaker: speaker, text: segment.text, start: segment.start, end: segment.end))
            }
        }
    }

    private func flushWindow(force: Bool) {
        guard let first = utterances.first, let last = utterances.last else {
            windowStartedAt = Date()
            return
        }
        let t = now
        let chars = utterances.reduce(0) { $0 + $1.text.count }
        let due = force || t - last.end >= Self.windowGap || last.end - first.start >= Self.windowMaxSeconds
            || chars >= Self.windowMaxChars
        guard due, pendingFinals.isEmpty || force else { return }
        let taken = utterances
        utterances = []
        let started = windowStartedAt
        windowStartedAt = Date()
        guard chars >= 4 else {
            NativeAmbientLog.note("旁听：一段只有 \(chars) 个字，丢弃不送")
            return
        }
        let rows: [[String: Any]] = taken.map { utterance in
            let (label, isUser) = speakerLabel(utterance.speaker)
            return ["speaker": label, "isUser": isUser, "text": utterance.text,
                    "t0": max(0, utterance.start - first.start), "t1": max(0, utterance.end - first.start)]
        }
        let speakers = Set(taken.compactMap(\.speaker)).count
        let window = Window(id: "amb-" + Self.stamp.string(from: started) + "-" + String(UUID().uuidString.prefix(4)),
                            startedAt: started, endedAt: Date(), streamStart: first.start, streamEnd: last.end,
                            utterances: rows, speakerCount: max(1, speakers), locale: locale,
                            source: source.wireName)
        emit(.window(window))
    }

    // MARK: 存音频

    private static var folder: URL {
        let base = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("环境旁听/" + day.string(from: Date()), isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base
    }

    /// 从环形缓冲里切出本窗（前后各留 1 秒）存成 WAV，旁边放同名转写。
    func saveAudio(_ window: Window, reason: String) {
        work.async {
            let from = max(self.ringStart, window.streamStart - 1)
            let to = min(self.ringStart + Double(self.ring.count) / NativeStreamDiarizer.sampleRate, window.streamEnd + 1)
            let a = Int((from - self.ringStart) * NativeStreamDiarizer.sampleRate)
            let b = Int((to - self.ringStart) * NativeStreamDiarizer.sampleRate)
            guard a >= 0, b > a, b <= self.ring.count else {
                NativeAmbientLog.note("旁听：本窗原声已滚出缓冲（超过 4 分钟），没能保存", level: "error")
                return
            }
            let url = Self.folder.appendingPathComponent(window.id + ".wav")
            do {
                try Self.writeWAV(Array(self.ring[a..<b]), to: url)
                let transcript = window.utterances.map { "\($0["speaker"] as? String ?? "?")：\($0["text"] as? String ?? "")" }
                try transcript.joined(separator: "\n").write(to: url.deletingPathExtension().appendingPathExtension("txt"),
                                                              atomically: true, encoding: .utf8)
                NativeAmbientLog.note(String(format: "旁听：已存原声 %@（%.0f 秒，%@）", url.lastPathComponent, to - from, reason))
            } catch {
                NativeAmbientLog.note("旁听：存原声失败 \(error.localizedDescription)", level: "error")
            }
        }
    }

    /// 危险：从现在起把麦克风原样写进 m4a，直到 `until`（再次触发会顺延）。先把缓冲里最近 30 秒补进同目录。
    func startDangerRecording(until: Date) {
        work.async {
            self.dangerUntil = max(self.dangerUntil, until)
            guard self.dangerFile == nil else {
                NativeAmbientLog.note("旁听：危险录音顺延到 \(Self.clock.string(from: self.dangerUntil))")
                return
            }
            let stamp = Self.stamp.string(from: Date())
            let lead = Int(30 * NativeStreamDiarizer.sampleRate)
            if !self.ring.isEmpty {
                try? Self.writeWAV(Array(self.ring.suffix(lead)), to: Self.folder.appendingPathComponent("危险-\(stamp)-之前30秒.wav"))
            }
            // 用管线自己的采样率：通话来源时没有自己的输入节点可问
            let rate = self.currentSampleRate
            let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: rate,
                                           AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 64_000]
            do {
                let file = try AVAudioFile(forWriting: Self.folder.appendingPathComponent("危险-\(stamp).m4a"),
                                           settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
                self.dangerFile = file
                self.dangerLock.lock(); self.dangerFileShared = file; self.dangerLock.unlock()
                NativeAmbientLog.note("旁听：⚠ 危险录音开始，到 \(Self.clock.string(from: self.dangerUntil))")
            } catch {
                NativeAmbientLog.note("旁听：危险录音开不起来 \(error.localizedDescription)", level: "error")
            }
        }
    }

    private func closeDangerFile() {
        guard dangerFile != nil else { return }
        dangerLock.lock(); dangerFileShared = nil; dangerLock.unlock()
        dangerFile = nil
        NativeAmbientLog.note("旁听：危险录音结束，文件在「文件 → BWReader → 环境旁听」")
    }

    static func writeWAV(_ samples: [Int16], to url: URL) throws {
        var data = Data()
        func put<T: FixedWidthInteger>(_ value: T) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        let rate = UInt32(NativeStreamDiarizer.sampleRate)
        let bytes = UInt32(samples.count * 2)
        data.append(contentsOf: Array("RIFF".utf8)); put(UInt32(36) + bytes)
        data.append(contentsOf: Array("WAVE".utf8))
        data.append(contentsOf: Array("fmt ".utf8)); put(UInt32(16)); put(UInt16(1)); put(UInt16(1))
        put(rate); put(rate * 2); put(UInt16(2)); put(UInt16(16))
        data.append(contentsOf: Array("data".utf8)); put(bytes)
        samples.withUnsafeBufferPointer { pointer in
            for value in pointer { put(value) }
        }
        try data.write(to: url, options: .atomic)
    }

    private static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HHmmss"
        return formatter
    }()
    private static let day: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
    private static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()
}
